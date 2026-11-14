"""Bit-exact golden model of rtl/bp/bpu.sv.

One object = one bpu instance. The model is cycle based like the RTL:

    out = m.predict(pc, valid)        # combinational outputs for this cycle, no state change
    m.tick(pred_valid, pred_pc, pred_fire, upd=..., flush_meta=...)   # one rising clock edge

Everything the RTL reads in a cycle (tables, GHR, RAS, LFSR) is read before anything is written,
so the order of the steps inside tick() does not matter as long as all reads use the old state.
Index and tag selection, counter updates, the meta layout, the repair rules and the indirect
target table are the same as in the RTL; docs/BRANCH_PREDICTOR.md explains them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Control-flow kinds on the update port (bp_pkg::cf_kind_e).
CF_NONE, CF_BRANCH, CF_JAL, CF_JALR, CF_CALL, CF_RET = range(6)
KIND_NAMES = {CF_NONE: "none", CF_BRANCH: "branch", CF_JAL: "jal", CF_JALR: "jalr",
              CF_CALL: "call", CF_RET: "ret"}

# Kinds stored in the BTB (bp_pkg::btb_kind_e).
BTB_BRANCH, BTB_JUMP, BTB_CALL, BTB_RET = range(4)
_CF_TO_BTB = {CF_BRANCH: BTB_BRANCH, CF_JAL: BTB_JUMP, CF_JALR: BTB_JUMP,
              CF_CALL: BTB_CALL, CF_RET: BTB_RET}

TYPES = ("STATIC_NT", "BIMODAL", "GSHARE", "TOURNAMENT", "RANDOM")
PC_W = 30
# Update kinds that may train the indirect target table (the RAS handles returns).
_IND_KINDS = (CF_JAL, CF_JALR, CF_CALL)
IND_ENTRIES_DEFAULT = 32  # `BP_IND_ENTRIES default in bpu.sv
M32 = 0xFFFF_FFFF


def clog2(n: int) -> int:
    return (n - 1).bit_length() if n > 1 else 0


def ras_ptr_w(ras_depth: int) -> int:
    return clog2(ras_depth) if ras_depth > 1 else 0


def meta_w(ghr_bits: int, ras_depth: int) -> int:
    """Same as bp_pkg::meta_w."""
    w = ghr_bits + ((ras_ptr_w(ras_depth) + PC_W) if ras_depth > 0 else 0)
    return max(w, 1)


def ctr_next(ctr: int, up: bool) -> int:
    """2-bit saturating counter, same as bp_pkg::ctr_next."""
    return min(ctr + 1, 3) if up else max(ctr - 1, 0)


def _pow2(n: int) -> bool:
    return n >= 1 and (n & (n - 1)) == 0


@dataclass(frozen=True)
class Config:
    bp_type: str = "GSHARE"
    btb_entries: int = 64
    bht_entries: int = 512
    ghr_bits: int = 9
    ras_depth: int = 4
    ind_entries: int = IND_ENTRIES_DEFAULT  # 0 = no indirect target table

    def check(self) -> None:
        if self.bp_type not in TYPES:
            raise ValueError(f"unknown BP_TYPE {self.bp_type}")
        if not (_pow2(self.btb_entries) and self.btb_entries >= 2):
            raise ValueError("BTB_ENTRIES must be a power of two >= 2")
        if not (_pow2(self.bht_entries) and self.bht_entries >= 2):
            raise ValueError("BHT_ENTRIES must be a power of two >= 2")
        if self.ras_depth == 1 or (self.ras_depth and not _pow2(self.ras_depth)):
            raise ValueError("RAS_DEPTH must be 0 or a power of two >= 2")
        if self.ind_entries == 1 or (self.ind_entries and not _pow2(self.ind_entries)):
            raise ValueError("IND_ENTRIES must be 0 or a power of two >= 2")
        if self.uses_ghr and not 1 <= self.ghr_bits <= clog2(self.bht_entries):
            raise ValueError("GHR_BITS must be in 1..log2(BHT_ENTRIES)")

    @property
    def uses_ghr(self) -> bool:
        return self.bp_type in ("GSHARE", "TOURNAMENT")

    @property
    def meta_w(self) -> int:
        return meta_w(self.ghr_bits, self.ras_depth)

    def short(self) -> str:
        return (f"{self.bp_type}_btb{self.btb_entries}_bht{self.bht_entries}"
                f"_g{self.ghr_bits}_ras{self.ras_depth}_ind{self.ind_entries}")


@dataclass(frozen=True)
class Prediction:
    taken: int
    target: int     # predicted next fetch PC (target if taken, else pc+4)
    meta: int
    btb_hit: int    # not a port, handy for tests
    btb_kind: int


@dataclass(frozen=True)
class Update:
    pc: int
    kind: int
    taken: int
    target: int
    mispredict: int
    meta: int


def flop_bits(cfg: Config) -> dict[str, int]:
    """Storage of one bpu configuration, in flip-flops (see docs/BRANCH_PREDICTOR.md)."""
    iw = clog2(cfg.btb_entries)
    bits = {"btb": cfg.btb_entries * (1 + (PC_W - iw) + 2 + PC_W)}
    tbl = cfg.bht_entries * 2
    if cfg.bp_type == "BIMODAL":
        bits["counters"] = tbl
    elif cfg.bp_type == "GSHARE":
        bits["counters"] = tbl
        bits["ghr"] = cfg.ghr_bits
    elif cfg.bp_type == "TOURNAMENT":
        bits["counters"] = 3 * tbl
        bits["ghr"] = cfg.ghr_bits
    elif cfg.bp_type == "RANDOM":
        bits["lfsr"] = 16
    if cfg.ras_depth:
        bits["ras"] = cfg.ras_depth * PC_W + ras_ptr_w(cfg.ras_depth)
    if cfg.ind_entries:
        # valid + target per entry, the path history, and the "polymorphic" bit per BTB entry.
        bits["ind"] = (cfg.ind_entries * (1 + PC_W) + clog2(cfg.ind_entries)
                       + cfg.btb_entries)
    bits["total"] = sum(bits.values())
    # Meta bits the core really has to carry: fields that are constant 0 get optimised away.
    used_meta = (cfg.ghr_bits if cfg.uses_ghr else 0)
    if cfg.ras_depth:
        used_meta += ras_ptr_w(cfg.ras_depth) + PC_W
    bits["meta_carried"] = used_meta
    return bits


class BranchPredictorModel:
    def __init__(self, cfg: Config | None = None, **kw):
        self.cfg = cfg if cfg is not None else Config(**kw)
        self.cfg.check()
        c = self.cfg
        self.btb_iw = clog2(c.btb_entries)
        self.btb_tw = PC_W - self.btb_iw
        self.bht_iw = clog2(c.bht_entries)
        self.gb = c.ghr_bits
        self.pb = ras_ptr_w(c.ras_depth)
        self.tb = PC_W if c.ras_depth else 0
        self.ind_iw = clog2(c.ind_entries) if c.ind_entries else 0
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        c = self.cfg
        # BTB entry: (valid, tag, kind, target30, poly). poly = this jump has gone to more than
        # one target since it was allocated; it is always 0 without the indirect table.
        self.btb = [(0, 0, 0, 0, 0)] * c.btb_entries
        # Indirect target table entry: (valid, target30); path = history of indirect targets.
        self.ind = [(0, 0)] * max(c.ind_entries, 1)
        self.path = 0
        self.bim = [1] * c.bht_entries   # bimodal counters (also the tournament's bimodal half)
        self.gsh = [1] * c.bht_entries   # gshare counters
        self.cho = [1] * c.bht_entries   # tournament chooser
        self.ghr = 0
        self.ras = [0] * max(c.ras_depth, 1)
        self.ras_ptr = 0
        self.lfsr = 0xACE1

    # ------------------------------------------------------------------ meta

    def pack_meta(self, ghr: int, ptr: int, top: int) -> int:
        m = 0
        if self.gb:
            m |= ghr & ((1 << self.gb) - 1)
        if self.pb:
            m |= (ptr & ((1 << self.pb) - 1)) << self.gb
        if self.tb:
            m |= (top & ((1 << PC_W) - 1)) << (self.gb + self.pb)
        return m

    def unpack_meta(self, meta: int) -> tuple[int, int, int]:
        ghr = meta & ((1 << self.gb) - 1) if self.gb else 0
        ptr = (meta >> self.gb) & ((1 << self.pb) - 1) if self.pb else 0
        top = (meta >> (self.gb + self.pb)) & ((1 << PC_W) - 1) if self.tb else 0
        return ghr, ptr, top

    # ------------------------------------------------------------------ indexing

    def _btb_idx_tag(self, pc: int) -> tuple[int, int]:
        return (pc >> 2) & (self.cfg.btb_entries - 1), pc >> (2 + self.btb_iw)

    def _bim_idx(self, pc: int) -> int:
        return (pc >> 2) & (self.cfg.bht_entries - 1)

    def _gsh_idx(self, pc: int, ghr: int) -> int:
        return ((pc >> 2) ^ ghr) & (self.cfg.bht_entries - 1)

    def _ind_idx(self, pc: int) -> int:
        return ((pc >> 2) ^ self.path) & (self.cfg.ind_entries - 1)

    def _ghr_live(self) -> int:
        return self.ghr if self.cfg.uses_ghr else 0

    def _ras_top(self) -> int:
        return self.ras[self.ras_ptr] if self.cfg.ras_depth else 0

    def _ghr_shift(self, ghr: int, bit: int) -> int:
        return ((ghr << 1) | bit) & ((1 << self.gb) - 1)

    # ------------------------------------------------------------------ predict

    def _direction(self, pc: int) -> int:
        t = self.cfg.bp_type
        if t == "BIMODAL":
            return self.bim[self._bim_idx(pc)] >> 1
        if t == "GSHARE":
            return self.gsh[self._gsh_idx(pc, self.ghr)] >> 1
        if t == "TOURNAMENT":
            i = self._bim_idx(pc)
            use_g = self.cho[i] >> 1
            return (self.gsh[self._gsh_idx(pc, self.ghr)] if use_g else self.bim[i]) >> 1
        if t == "RANDOM":
            return self.lfsr & 1
        return 0

    def predict(self, pc: int, valid: int = 1) -> Prediction:
        pc &= M32
        idx, tag = self._btb_idx_tag(pc)
        v, etag, kind, etgt, poly = self.btb[idx]
        hit = int(bool(v) and etag == tag)
        pc4 = (pc + 4) & M32
        target = (self._ras_top() << 2) if (self.cfg.ras_depth and kind == BTB_RET) else (etgt << 2)
        if self.cfg.ind_entries and poly and kind in (BTB_JUMP, BTB_CALL):
            iv, itgt = self.ind[self._ind_idx(pc)]
            if iv:
                target = itgt << 2
        taken = 0
        if hit:
            taken = self._direction(pc) if kind == BTB_BRANCH else 1
        if self.cfg.bp_type == "RANDOM":
            r = self.lfsr
            garbage = (pc4 + (((r >> 4) & 0xF) << 2)) & M32
            sel = (r >> 1) & 7
            if hit and kind != BTB_BRANCH:
                if sel == 0:
                    target = garbage
                elif sel == 1:
                    taken = 0
            elif not hit and ((r >> 8) & 0xF) == 0:
                taken, target = 1, garbage
        taken = int(bool(valid) and taken)
        meta = self.pack_meta(self._ghr_live(), self.ras_ptr if self.cfg.ras_depth else 0,
                              self._ras_top())
        return Prediction(taken, target if taken else pc4, meta, hit, kind)

    # ------------------------------------------------------------------ clock edge

    def tick(self, pred_valid: int = 0, pred_pc: int = 0, pred_fire: int = 0,
             upd: Update | None = None, flush_meta: int | None = None) -> Prediction:
        """Apply one rising edge. Returns the prediction of this cycle (pre-edge outputs)."""
        c = self.cfg
        pred = self.predict(pred_pc, pred_valid)
        fire = bool(pred_valid and pred_fire)
        flush = flush_meta is not None
        restore = upd is not None and bool(upd.mispredict) and not flush

        # --- training (non-speculative), all reads before writes
        btb_write = None
        dir_write = []
        ind_write = None
        path_n = self.path
        if upd is not None:
            upc = upd.pc & M32
            uidx, utag = self._btb_idx_tag(upc)
            v, etag, _, etgt, epoly = self.btb[uidx]
            uhit = bool(v) and etag == utag
            if upd.kind in _CF_TO_BTB:
                if upd.taken:
                    tgt30 = (upd.target & M32) >> 2
                    poly = int(bool(c.ind_entries) and upd.kind in _IND_KINDS and uhit
                               and bool(epoly or etgt != tgt30))
                    btb_write = (uidx, (1, utag, _CF_TO_BTB[upd.kind], tgt30, poly))
                    if poly:
                        ind_write = (self._ind_idx(upc), (1, tgt30))
                        path_n = ((self.path << 2) ^ tgt30) & (c.ind_entries - 1)
            else:
                if uhit:
                    btb_write = (uidx, (0, 0, 0, 0, 0))
            if upd.kind == CF_BRANCH:
                ughr = self.unpack_meta(upd.meta)[0]
                bi = self._bim_idx(upc)
                gi = self._gsh_idx(upc, ughr)
                if c.bp_type in ("BIMODAL", "TOURNAMENT"):
                    dir_write.append((self.bim, bi, ctr_next(self.bim[bi], upd.taken)))
                if c.bp_type in ("GSHARE", "TOURNAMENT"):
                    dir_write.append((self.gsh, gi, ctr_next(self.gsh[gi], upd.taken)))
                if c.bp_type == "TOURNAMENT":
                    b_ok = self.bim[bi] >> 1
                    g_ok = self.gsh[gi] >> 1
                    if b_ok != g_ok:
                        dir_write.append((self.cho, bi, ctr_next(self.cho[bi], g_ok == upd.taken)))

        # --- speculative state
        ghr_n = self.ghr
        ras_writes = []
        ptr_n = self.ras_ptr
        if flush:
            fghr, fptr, ftop = self.unpack_meta(flush_meta)
            ghr_n = fghr
            if c.ras_depth:
                ras_writes.append((fptr, ftop))
                ptr_n = fptr
        elif restore:
            ughr, uptr, utop = self.unpack_meta(upd.meta)
            ghr_n = self._ghr_shift(ughr, upd.taken) if upd.kind == CF_BRANCH else ughr
            if c.ras_depth:
                d = c.ras_depth
                ras_writes.append((uptr, utop))
                if upd.kind == CF_CALL:
                    ptr_n = (uptr + 1) % d
                    ras_writes.append((ptr_n, ((upd.pc + 4) & M32) >> 2))
                elif upd.kind == CF_RET:
                    ptr_n = (uptr - 1) % d
                else:
                    ptr_n = uptr
        elif fire and pred.btb_hit:
            if pred.btb_kind == BTB_BRANCH:
                ghr_n = self._ghr_shift(self.ghr, pred.taken)
            if c.ras_depth:
                d = c.ras_depth
                if pred.btb_kind == BTB_CALL:
                    ptr_n = (self.ras_ptr + 1) % d
                    ras_writes.append((ptr_n, ((pred_pc + 4) & M32) >> 2))
                elif pred.btb_kind == BTB_RET:
                    ptr_n = (self.ras_ptr - 1) % d

        lfsr_n = self.lfsr
        if c.bp_type == "RANDOM" and fire:
            lsb = self.lfsr & 1
            lfsr_n = (self.lfsr >> 1) ^ (0xB400 if lsb else 0)

        # --- commit
        if btb_write is not None:
            self.btb[btb_write[0]] = btb_write[1]
        if ind_write is not None:
            self.ind[ind_write[0]] = ind_write[1]
        self.path = path_n
        for table, i, val in dir_write:
            table[i] = val
        if c.uses_ghr:
            self.ghr = ghr_n
        for i, val in ras_writes:
            self.ras[i] = val
        if c.ras_depth:
            self.ras_ptr = ptr_n
        self.lfsr = lfsr_n
        return pred

    # ------------------------------------------------------------------ convenience

    def fire(self, pc: int) -> Prediction:
        """Predict and fire one fetch (one cycle, no update, no flush)."""
        return self.tick(1, pc, 1)

    def update(self, upd: Update) -> Prediction:
        """Resolve one instruction (one cycle, no prediction fired)."""
        return self.tick(0, 0, 0, upd=upd)
