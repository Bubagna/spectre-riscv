// bp_btb: direct-mapped branch target buffer.
//
// Answers in IF, before the instruction is decoded: "is this PC a control-flow instruction, of
// which kind, and where does it go if taken". Index = pc[2 +: IW], tag = the remaining upper PC
// bits (full tag, so two different PCs never share an entry by accident; a false hit can only
// come from code that was rewritten).
//
// Allocation happens at update time, never speculatively:
//   - taken control flow (taken branch, any jump): write {valid, tag, kind, target}. A not-taken
//     branch that misses is not allocated: fetching pc+4 was already right, so it would only
//     evict a useful entry.
//   - CF_NONE update whose PC hits: the entry is stale (the PC is not control flow any more),
//     invalidate it so the same wrong redirect does not repeat.
//   - not-taken branch that hits: no change, the direction predictor handles it.
//
// With POLY = 1 each entry also has a "polymorphic" bit for the indirect target table (bp_ind):
// it is set when a jump or call that hits its entry goes to a different target than the stored
// one, and stays set until the entry is reallocated. A JAL never changes target, so in practice
// only JALRs with several targets (switch tables, function pointers) get it. upd_poly_o is the
// value written with the current update, so bpu can train bp_ind in the same cycle.
//
// Timing: predict path is the port A read (IW-level mux) plus a TW-bit tag compare. The target
// compare for the poly bit is on the update side only.
module bp_btb #(
  parameter int unsigned ENTRIES = 64,
  parameter bit          POLY    = 1'b0
) (
  input  logic                 clk_i,
  input  logic                 rst_ni,

  input  logic [31:0]          pred_pc_i,
  output logic                 pred_hit_o,
  output bp_pkg::btb_kind_e    pred_kind_o,
  output logic [31:0]          pred_target_o,
  output logic                 pred_poly_o,

  input  logic                 upd_valid_i,
  input  logic [31:0]          upd_pc_i,
  input  logic [2:0]           upd_kind_i,
  input  logic                 upd_taken_i,
  input  logic [31:0]          upd_target_i,
  output logic                 upd_poly_o
);
  import bp_pkg::*;

  localparam int unsigned IW = $clog2(ENTRIES);
  localparam int unsigned TW = PC_W - IW;
  localparam int unsigned EW = 1 + TW + 2 + PC_W + 1;  // valid, tag, kind, target, poly

  typedef struct packed {
    logic            valid;
    logic [TW-1:0]   tag;
    btb_kind_e       kind;
    logic [PC_W-1:0] target;
    logic            poly;
  } btb_entry_t;

  btb_entry_t pred_e, upd_e, wr_e;
  logic [EW-1:0] pred_raw, upd_raw;
  logic [IW-1:0] pred_idx, upd_idx;
  logic [TW-1:0] pred_tag, upd_tag;
  logic          we, upd_hit;

  assign pred_idx = pred_pc_i[2 +: IW];
  assign pred_tag = pred_pc_i[31 -: TW];
  assign upd_idx  = upd_pc_i[2 +: IW];
  assign upd_tag  = upd_pc_i[31 -: TW];

  bp_ram #(.DEPTH(ENTRIES), .WIDTH(EW), .RESET_VAL('0)) u_table (
    .clk_i, .rst_ni,
    .ra_addr_i (pred_idx), .ra_data_o (pred_raw),
    .rb_addr_i (upd_idx),  .rb_data_o (upd_raw),
    .we_i      (we),       .wa_addr_i (upd_idx), .wa_data_i (wr_e)
  );

  assign pred_e = btb_entry_t'(pred_raw);
  assign upd_e  = btb_entry_t'(upd_raw);

  assign pred_hit_o    = pred_e.valid && (pred_e.tag == pred_tag);
  assign pred_kind_o   = pred_e.kind;
  assign pred_target_o = {pred_e.target, 2'b00};
  assign pred_poly_o   = pred_e.poly;
  assign upd_hit       = upd_e.valid && (upd_e.tag == upd_tag);

  always_comb begin
    we          = 1'b0;
    wr_e        = '0;
    wr_e.valid  = 1'b1;
    wr_e.tag    = upd_tag;
    wr_e.target = upd_target_i[31:2];
    wr_e.kind   = BTB_JUMP;
    // Without POLY the bit is constant 0, so synthesis removes its flops.
    wr_e.poly   = POLY && (upd_kind_i inside {CF_JAL, CF_JALR, CF_CALL}) && upd_hit &&
                  (upd_e.poly || upd_e.target != upd_target_i[31:2]);
    unique case (upd_kind_i)
      CF_BRANCH:       wr_e.kind = BTB_BRANCH;
      CF_JAL, CF_JALR: wr_e.kind = BTB_JUMP;
      CF_CALL:         wr_e.kind = BTB_CALL;
      CF_RET:          wr_e.kind = BTB_RET;
      default:         wr_e.kind = BTB_JUMP;
    endcase

    if (upd_valid_i) begin
      if (upd_kind_i inside {CF_BRANCH, CF_JAL, CF_JALR, CF_CALL, CF_RET}) begin
        we = upd_taken_i;
      end else if (upd_hit) begin
        // Kind 0 (and the unused encodings 6, 7) mean "not control flow": drop the entry.
        we   = 1'b1;
        wr_e = '0;
      end
    end
  end

  assign upd_poly_o = wr_e.poly;

  // PCs and taken targets are word aligned (a misaligned target traps in the core).
  // The update read port is needed for the tag check of an invalidation and for the poly bit.
  logic unused_bits;
  assign unused_bits = ^{pred_pc_i[1:0], upd_pc_i[1:0], upd_target_i[1:0], upd_e.kind};

endmodule
