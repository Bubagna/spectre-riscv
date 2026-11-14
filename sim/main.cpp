// Verilator harness for tb_top (docs/SPEC.md sections 6 and 7).
//
//   Vtb_top +elf=<file> [+trace=<file>] [+max_cycles=N] [+stall=0..100] [+seed=N]
//           [+vcd=<file>] [+quiet]
//
// Loads the ELF into sim_mem through a DPI backdoor, releases reset and runs until a store to
// TOHOST/HALT retires (PASS / FAIL n), an instruction traps (TRAP), a simulator error (ERROR ...)
// or max_cycles (TIMEOUT). Everything is observed on the RVFI port, so the end of the run, the
// trace and the PUTCHAR output follow program order exactly.
//
// It also checks the predictor update port against retirement: every retired control-flow
// instruction must have produced exactly one update, in program order, with the right outcome.
// Wrong-path instructions must produce none.
//
// Exit status, same as the ISS: 0 PASS, 1 FAIL, 2 TRAP, 3 TIMEOUT, 4 ERROR.

#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <string>

#include "Vtb_top.h"
#include "Vtb_top__Dpi.h"
#include "elf_loader.h"
#include "svdpi.h"
#include "verilated.h"
#if VM_TRACE_VCD
#include "verilated_vcd_c.h"
#endif

namespace {

constexpr uint32_t RAM_BASE = 0x80000000u;
constexpr uint32_t RAM_SIZE = 1u << 20;
constexpr uint32_t MMIO_TOHOST = 0x10000000u;
constexpr uint32_t MMIO_PUTCHAR = 0x10000004u;
constexpr uint32_t MMIO_HALT = 0x10000008u;

enum Kind { K_NONE = 0, K_BRANCH = 1, K_JAL = 2, K_JALR = 3, K_CALL = 4, K_RET = 5 };

struct Args {
    std::string elf, trace, vcd;
    uint64_t max_cycles = 50'000'000;
    bool quiet = false;
};

bool plusarg(const char *arg, const char *name, std::string &val) {
    const size_t n = std::strlen(name);
    if (std::strncmp(arg, name, n) == 0 && arg[n] == '=') {
        val = arg + n + 1;
        return true;
    }
    return false;
}

// Same classification as the decoder and the ISS (SPEC section 5).
int cf_kind(uint32_t insn) {
    const uint32_t op = insn & 0x7f, rd = (insn >> 7) & 0x1f, rs1 = (insn >> 15) & 0x1f;
    const bool rd_link = rd == 1 || rd == 5, rs1_link = rs1 == 1 || rs1 == 5;
    if (op == 0x63) return K_BRANCH;
    if (op == 0x6f) return rd_link ? K_CALL : K_JAL;
    if (op == 0x67) return rd_link ? K_CALL : ((rs1_link && rd == 0) ? K_RET : K_JALR);
    return K_NONE;
}

// Counter reads (not mhartid) get " csr": their value legitimately differs from the ISS.
bool is_counter_csr(uint32_t insn) {
    if ((insn & 0x7f) != 0x73) return false;
    const uint32_t f3 = (insn >> 12) & 7, csr = insn >> 20;
    if (f3 == 0 || f3 == 4) return false;
    return csr == 0xC00 || csr == 0xC01 || csr == 0xC02 || csr == 0xC80 || csr == 0xC81 ||
           csr == 0xC82;
}

uint32_t lane_mask(uint32_t m) {
    uint32_t r = 0;
    for (int b = 0; b < 4; b++)
        if (m & (1u << b)) r |= 0xffu << (8 * b);
    return r;
}

// Value written by a store, shifted back down from its byte lanes.
uint32_t store_value(uint32_t wdata, uint32_t wmask) {
    if (wmask == 0) return 0;
    int lo = 0;
    while (!(wmask & (1u << lo))) lo++;
    return (wdata & lane_mask(wmask)) >> (8 * lo);
}

struct Upd {
    uint32_t pc, target;
    int kind;
    bool taken, mispredict;
};

}  // namespace

int main(int argc, char **argv) {
    auto ctx = std::make_unique<VerilatedContext>();
    ctx->commandArgs(argc, argv);  // sim_mem and bpu_stub read +stall/+seed themselves
    ctx->fatalOnError(false);      // $error in a checker: report it as ERROR and stop cleanly

    Args a;
    for (int i = 1; i < argc; i++) {
        std::string v;
        if (plusarg(argv[i], "+elf", v)) a.elf = v;
        else if (plusarg(argv[i], "+trace", v)) a.trace = v;
        else if (plusarg(argv[i], "+vcd", v)) a.vcd = v;
        else if (plusarg(argv[i], "+max_cycles", v)) a.max_cycles = std::strtoull(v.c_str(), nullptr, 0);
        else if (!std::strcmp(argv[i], "+quiet")) a.quiet = true;
    }
    if (a.elf.empty()) {
        std::fprintf(stderr,
                     "usage: %s +elf=<file> [+trace=<file>] [+max_cycles=N] [+stall=0..100] "
                     "[+seed=N] [+vcd=<file>] [+quiet]\n",
                     argv[0]);
        return 4;
    }

    auto top = std::make_unique<Vtb_top>(ctx.get(), "TOP");

#if VM_TRACE_VCD
    std::unique_ptr<VerilatedVcdC> wave;
    if (!a.vcd.empty()) {
        ctx->traceEverOn(true);
        wave = std::make_unique<VerilatedVcdC>();
        top->trace(wave.get(), 99);
        wave->open(a.vcd.c_str());
    }
#else
    if (!a.vcd.empty()) std::fprintf(stderr, "warning: built without --trace-vcd, +vcd ignored\n");
#endif
    auto dump = [&]() {
#if VM_TRACE_VCD
        if (wave) wave->dump(ctx->time());
#endif
    };

    // Reset asserted; the first eval runs the initial blocks (sim_mem clears its RAM there), so
    // the program is loaded after it.
    top->clk_i = 0;
    top->rst_ni = 0;
    top->eval();

    svSetScope(svGetScopeFromName("TOP.tb_top.u_mem"));
    ElfInfo info;
    std::string err;
    if (!load_elf(a.elf, RAM_BASE, RAM_SIZE,
                  [](uint32_t addr, uint8_t b) { simmem_write_byte(addr, b); }, info, err)) {
        std::printf("ERROR %s\n", err.c_str());
        return 4;
    }
    if (info.entry != RAM_BASE)
        std::fprintf(stderr, "warning: ELF entry 0x%08x, but the core always starts at 0x%08x\n",
                     info.entry, RAM_BASE);

    FILE *tf = nullptr;
    if (!a.trace.empty()) {
        tf = std::fopen(a.trace.c_str(), "w");
        if (!tf) {
            std::printf("ERROR cannot open %s\n", a.trace.c_str());
            return 4;
        }
    }

    auto tick = [&]() {
        top->clk_i = 1;
        top->eval();
        ctx->timeInc(1);
        dump();
        top->clk_i = 0;
        top->eval();
        ctx->timeInc(1);
        dump();
    };
    for (int i = 0; i < 4; i++) tick();
    top->rst_ni = 1;
    top->eval();

    std::string status;  // PASS, FAIL n, TRAP, TIMEOUT, ERROR ...
    int exit_code = 0;
    uint64_t cycles = 0, instret = 0;
    uint64_t branches = 0, br_mispred = 0, jumps = 0, jump_mispred = 0, nocf_mispred = 0;
    std::deque<Upd> upds;  // updates whose instruction has not retired yet
    int last_char = '\n';   // last PUTCHAR byte, to end the program output with a newline
    char line[160];

    auto fail_check = [&](const std::string &msg) {
        if (status.empty()) {
            status = "ERROR " + msg;
            exit_code = 4;
        }
    };

    while (status.empty()) {
        if (cycles >= a.max_cycles) {
            status = "TIMEOUT";
            exit_code = 3;
            break;
        }
        // Clock is low and the outputs show this cycle: sample, then clock edge.
        if (top->bp_upd_valid_o) {
            Upd u{top->bp_upd_pc_o, top->bp_upd_target_o, top->bp_upd_kind_o,
                  static_cast<bool>(top->bp_upd_taken_o),
                  static_cast<bool>(top->bp_upd_mispredict_o)};
            upds.push_back(u);
        }
        // Statistics count only updates of instructions that retire.
        auto count = [&](const Upd &u) {
            if (u.kind == K_BRANCH) {
                branches++;
                br_mispred += u.mispredict;
            } else if (u.kind != K_NONE) {
                jumps++;
                jump_mispred += u.mispredict;
            } else {
                nocf_mispred += u.mispredict;
            }
        };

        if (top->rvfi_valid) {
            instret++;
            const uint32_t pc = top->rvfi_pc_rdata, insn = top->rvfi_insn;
            const uint32_t wmask = top->rvfi_mem_wmask, rmask = top->rvfi_mem_rmask;
            const uint32_t maddr = top->rvfi_mem_addr;
            if (top->rvfi_trap) {
                std::snprintf(line, sizeof line, "%08x %08x trap", pc, insn);
            } else {
                int n = std::snprintf(line, sizeof line, "%08x %08x", pc, insn);
                if (top->rvfi_rd_addr)
                    n += std::snprintf(line + n, sizeof line - n, " x%02u %08x",
                                       static_cast<unsigned>(top->rvfi_rd_addr), top->rvfi_rd_wdata);
                if (wmask)
                    n += std::snprintf(line + n, sizeof line - n, " st %08x %x %08x", maddr, wmask,
                                       top->rvfi_mem_wdata & lane_mask(wmask));
                if (rmask) n += std::snprintf(line + n, sizeof line - n, " ld %08x", maddr);
                if (is_counter_csr(insn)) std::snprintf(line + n, sizeof line - n, " csr");
            }
            if (tf) std::fprintf(tf, "%s\n", line);

            // Predictor updates against retirement.
            const int kind = cf_kind(insn);
            if (!top->rvfi_trap && kind != K_NONE) {
                if (upds.empty() || upds.front().pc != pc) {
                    char m[96];
                    std::snprintf(m, sizeof m, "no_predictor_update_for_pc_%08x", pc);
                    fail_check(m);
                } else {
                    const Upd u = upds.front();
                    upds.pop_front();
                    count(u);
                    const uint32_t npc = top->rvfi_pc_wdata;
                    const bool ok = u.kind == kind && (u.taken ? npc == u.target : npc == pc + 4);
                    if (!ok) {
                        char m[96];
                        std::snprintf(m, sizeof m, "wrong_predictor_update_for_pc_%08x", pc);
                        fail_check(m);
                    }
                }
            } else if (!upds.empty() && upds.front().pc == pc && upds.front().kind == K_NONE) {
                count(upds.front());
                upds.pop_front();
            } else if (!upds.empty() && upds.front().pc == pc && !top->rvfi_trap) {
                char m[96];
                std::snprintf(m, sizeof m, "predictor_update_kind_for_pc_%08x", pc);
                fail_check(m);
            }

            if (wmask & 1 && maddr == MMIO_PUTCHAR) last_char = top->rvfi_mem_wdata & 0xff;

            if (top->rvfi_trap) {
                if (status.empty()) {
                    status = "TRAP";
                    exit_code = 2;
                }
            } else if (wmask && (maddr == MMIO_TOHOST || maddr == MMIO_HALT)) {
                const uint32_t v = store_value(top->rvfi_mem_wdata, wmask);
                const bool pass = (maddr == MMIO_TOHOST) ? v == 1 : v == 0;
                if (status.empty()) {
                    if (pass) {
                        status = "PASS";
                        exit_code = 0;
                    } else {
                        status = "FAIL " + std::to_string(maddr == MMIO_TOHOST ? v >> 1 : v);
                        exit_code = 1;
                    }
                }
            }
        }

        if (top->mem_err_o) fail_check("memory_error");
        if (ctx->gotError()) fail_check("assertion_failed");

        tick();
        cycles++;
        if (top->halt_o && status.empty()) fail_check("halt_without_trap_retired");
    }

    if (tf) std::fclose(tf);
#if VM_TRACE_VCD
    if (wave) wave->close();
#endif
    top->final();
    std::fflush(stdout);

    if (last_char != '\n' && !a.quiet) std::printf("\n");
    std::printf("%s\n", status.c_str());
    std::printf("cycles=%" PRIu64 " instret=%" PRIu64 " cpi=%.3f branches=%" PRIu64
                " br_mispred=%" PRIu64 " jumps=%" PRIu64 " jump_mispred=%" PRIu64 "\n",
                cycles, instret, instret ? static_cast<double>(cycles) / instret : 0.0, branches,
                br_mispred, jumps, jump_mispred);
    if (nocf_mispred) std::printf("info: nocf_mispred=%" PRIu64 "\n", nocf_mispred);
    return exit_code;
}
