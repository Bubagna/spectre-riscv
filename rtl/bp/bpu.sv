// bpu: branch prediction unit (top). Interface and semantics: docs/SPEC.md section 5,
// design notes: docs/BRANCH_PREDICTOR.md.
//
// Four parts:
//   - BTB (bp_btb): which PCs are control flow, their kind and last taken target.
//   - direction predictor for conditional branches, chosen by BP_TYPE:
//       STATIC_NT   always not taken (jumps found in the BTB are still predicted taken)
//       BIMODAL     2-bit counters indexed by PC
//       GSHARE      2-bit counters indexed by PC xor global history
//       TOURNAMENT  bimodal + gshare + per-PC chooser
//       RANDOM      simulation only: LFSR-driven, often wrong on purpose, also redirects
//                   ordinary instructions and corrupts jump targets, to stress the core
//   - return address stack (bp_ras, RAS_DEPTH = 0 removes it) for BTB kind "ret".
//   - indirect target table (bp_ind, IND_ENTRIES = 0 removes it) for jumps and calls the BTB
//     has seen go to more than one target; trained at resolve time, not speculative.
//
// Timing, one cycle:
//   - predict: combinational from pred_pc_i (and the current GHR/RAS) to pred_taken_o,
//     pred_target_o and pred_meta_o. No path from the update or flush inputs to the
//     prediction outputs: a repair is visible from the next cycle.
//   - pred_fire_i (with pred_valid_i) at the clock edge: shift the predicted direction into the
//     GHR (BTB kind branch) or push/pop the RAS (BTB kind call/ret). The outputs that count are
//     the ones of the fire cycle.
//   - upd_valid_i at the clock edge: train BTB, counters and the indirect table
//     (non-speculative). With
//     upd_mispredict_i, also restore GHR and RAS from upd_meta_i plus this instruction's real
//     outcome.
//   - flush_i at the clock edge: restore GHR and RAS from flush_meta_i, as they were before the
//     instruction that meta belongs to.
//   Priority for the speculative state: flush_i > upd mispredict > pred_fire_i.
//
// pred_target_o is the predicted next fetch PC: the target when pred_taken_o, else pc+4.

// Default size of the indirect target table. rv_core does not pass IND_ENTRIES, so the core
// gets this value; define BP_IND_ENTRIES at build time (e.g. -DBP_IND_ENTRIES=0) to compare
// configurations without touching the core.
`ifndef BP_IND_ENTRIES
`define BP_IND_ENTRIES 32
`endif

module bpu #(
  parameter              BP_TYPE     = "GSHARE",
  parameter int unsigned BTB_ENTRIES = 64,
  parameter int unsigned BHT_ENTRIES = 512,
  parameter int unsigned GHR_BITS    = 9,
  parameter int unsigned RAS_DEPTH   = 4,
  parameter int unsigned IND_ENTRIES = `BP_IND_ENTRIES,
  // Derived. Exposed as a parameter only so an instantiation may state it; a wrong value stops
  // elaboration. The core should use bp_pkg::meta_w(GHR_BITS, RAS_DEPTH).
  parameter int unsigned META_W      = bp_pkg::meta_w(GHR_BITS, RAS_DEPTH)
) (
  input  logic              clk_i,
  input  logic              rst_ni,

  input  logic              pred_valid_i,
  input  logic [31:0]       pred_pc_i,
  output logic              pred_taken_o,
  output logic [31:0]       pred_target_o,
  output logic [META_W-1:0] pred_meta_o,
  input  logic              pred_fire_i,

  input  logic              upd_valid_i,
  input  logic [31:0]       upd_pc_i,
  input  logic [2:0]        upd_kind_i,
  input  logic              upd_taken_i,
  input  logic [31:0]       upd_target_i,
  input  logic              upd_mispredict_i,
  input  logic [META_W-1:0] upd_meta_i,

  input  logic              flush_i,
  input  logic [META_W-1:0] flush_meta_i
);
  import bp_pkg::*;

  // ---------------------------------------------------------------------------------------------
  // Configuration and elaboration checks

  // BP_TYPE is untyped like in rv_core (SPEC.md section 3); the explicit casts make every
  // comparison the same width, whatever the length of the name.
  localparam logic [127:0] TYPE_S = 128'(BP_TYPE);
  localparam bit IS_STATIC = (TYPE_S == 128'("STATIC_NT"));
  localparam bit IS_BIM    = (TYPE_S == 128'("BIMODAL"));
  localparam bit IS_GSHARE = (TYPE_S == 128'("GSHARE"));
  localparam bit IS_TOURN  = (TYPE_S == 128'("TOURNAMENT"));
  localparam bit IS_RANDOM = (TYPE_S == 128'("RANDOM"));
  localparam bit USE_GHR   = IS_GSHARE || IS_TOURN;
  localparam bit RAS_ON    = (RAS_DEPTH > 0);
  localparam bit IND_ON    = (IND_ENTRIES > 0);

  localparam int unsigned GB  = GHR_BITS;             // history field width in the meta
  localparam int unsigned PB  = ras_ptr_w(RAS_DEPTH); // RAS pointer field width in the meta
  localparam int unsigned TB  = RAS_ON ? PC_W : 0;    // RAS top field width in the meta
  localparam int unsigned GW  = (GB > 0) ? GB : 1;    // internal widths, never zero
  localparam int unsigned PW  = (PB > 0) ? PB : 1;
  localparam int unsigned BIW = $clog2(BHT_ENTRIES);

  if (!(IS_STATIC || IS_BIM || IS_GSHARE || IS_TOURN || IS_RANDOM)) begin : g_err_type
    $error("bpu: unknown BP_TYPE");
  end
  if (META_W != meta_w(GHR_BITS, RAS_DEPTH)) begin : g_err_meta
    $error("bpu: META_W must equal bp_pkg::meta_w(GHR_BITS, RAS_DEPTH)");
  end
  if (BTB_ENTRIES < 2 || (BTB_ENTRIES & (BTB_ENTRIES - 1)) != 0) begin : g_err_btb
    $error("bpu: BTB_ENTRIES must be a power of two >= 2");
  end
  if (BHT_ENTRIES < 2 || (BHT_ENTRIES & (BHT_ENTRIES - 1)) != 0) begin : g_err_bht
    $error("bpu: BHT_ENTRIES must be a power of two >= 2");
  end
  if (RAS_DEPTH == 1 || (RAS_DEPTH & (RAS_DEPTH - 1)) != 0) begin : g_err_ras
    $error("bpu: RAS_DEPTH must be 0 or a power of two >= 2");
  end
  if (IND_ENTRIES == 1 || (IND_ENTRIES & (IND_ENTRIES - 1)) != 0) begin : g_err_ind
    $error("bpu: IND_ENTRIES must be 0 or a power of two >= 2");
  end
  if (USE_GHR && (GHR_BITS < 1 || GHR_BITS > BIW)) begin : g_err_ghr
    $error("bpu: GHR_BITS must be in 1..log2(BHT_ENTRIES) for GSHARE and TOURNAMENT");
  end
`ifdef SYNTHESIS
  // RANDOM exists to stress the core in simulation; it must never reach a netlist by accident.
  if (IS_RANDOM) begin : g_err_random
    $error("bpu: BP_TYPE RANDOM is simulation only");
  end
`endif

  // ---------------------------------------------------------------------------------------------
  // Meta: {ras_top[TB], ras_ptr[PB], ghr[GB]}, LSB first. A field of width 0 is left out, and
  // with no field at all the meta is a single constant 0 bit.

  logic [GW-1:0]   ghr_q, upd_ghr, flush_ghr;
  logic [PW-1:0]   ras_ptr, upd_ptr, flush_ptr;
  logic [PC_W-1:0] ras_top, upd_top, flush_top;

  if (GB > 0) begin : g_meta_ghr
    assign pred_meta_o[0 +: GB] = ghr_q;
    assign upd_ghr              = upd_meta_i[0 +: GB];
    assign flush_ghr            = flush_meta_i[0 +: GB];
  end else begin : g_meta_no_ghr
    assign upd_ghr   = '0;
    assign flush_ghr = '0;
  end

  if (PB > 0) begin : g_meta_ptr
    assign pred_meta_o[GB +: PB] = ras_ptr;
    assign upd_ptr               = upd_meta_i[GB +: PB];
    assign flush_ptr             = flush_meta_i[GB +: PB];
  end else begin : g_meta_no_ptr
    assign upd_ptr   = '0;
    assign flush_ptr = '0;
  end

  if (TB > 0) begin : g_meta_top
    assign pred_meta_o[GB+PB +: TB] = ras_top;
    assign upd_top                  = upd_meta_i[GB+PB +: TB];
    assign flush_top                = flush_meta_i[GB+PB +: TB];
  end else begin : g_meta_no_top
    assign upd_top   = '0;
    assign flush_top = '0;
  end

  if (GB + PB + TB == 0) begin : g_meta_empty
    assign pred_meta_o = '0;
  end

  // ---------------------------------------------------------------------------------------------
  // Event decode

  logic fire, upd_restore, dir_upd_en;
  logic btb_hit, btb_poly, btb_upd_poly;
  btb_kind_e btb_kind;
  logic [31:0] btb_target, pc_plus4;

  assign fire        = pred_valid_i && pred_fire_i;
  assign upd_restore = upd_valid_i && upd_mispredict_i && !flush_i;
  assign dir_upd_en  = upd_valid_i && (upd_kind_i == CF_BRANCH);
  assign pc_plus4    = pred_pc_i + 32'd4;

  // ---------------------------------------------------------------------------------------------
  // BTB

  bp_btb #(.ENTRIES(BTB_ENTRIES), .POLY(IND_ON)) u_btb (
    .clk_i, .rst_ni,
    .pred_pc_i,
    .pred_hit_o    (btb_hit),
    .pred_kind_o   (btb_kind),
    .pred_target_o (btb_target),
    .pred_poly_o   (btb_poly),
    .upd_valid_i, .upd_pc_i, .upd_kind_i, .upd_taken_i, .upd_target_i,
    .upd_poly_o    (btb_upd_poly)
  );

  // ---------------------------------------------------------------------------------------------
  // Indirect target table

  logic        ind_hit;
  logic [31:0] ind_target;

  if (IND_ON) begin : g_ind
    bp_ind #(.ENTRIES(IND_ENTRIES)) u_ind (
      .clk_i, .rst_ni,
      .pred_pc_i,
      .pred_hit_o    (ind_hit),
      .pred_target_o (ind_target),
      // btb_upd_poly is only set for taken jal/jalr/call updates that hit their entry.
      .upd_en_i      (upd_valid_i && upd_taken_i && btb_upd_poly),
      .upd_pc_i,
      .upd_target_i
    );
  end else begin : g_no_ind
    assign ind_hit    = 1'b0;
    assign ind_target = '0;
  end

  // ---------------------------------------------------------------------------------------------
  // LFSR for RANDOM (x^16 + x^14 + x^13 + x^11 + 1, Galois form, maximal length). It advances
  // once per fired prediction, so the sequence depends on the instruction stream and not on
  // stalls, which keeps runs reproducible and lets the trace model mirror it.

  logic [15:0] rnd;

  if (IS_RANDOM) begin : g_lfsr
`ifndef SYNTHESIS
    logic [15:0] lfsr_q;
    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni)   lfsr_q <= 16'hACE1;
      else if (fire) lfsr_q <= {1'b0, lfsr_q[15:1]} ^ (lfsr_q[0] ? 16'hB400 : 16'h0000);
    end
    assign rnd = lfsr_q;
`else
    assign rnd = '0;
`endif
  end else begin : g_no_lfsr
    assign rnd = '0;
  end

  // ---------------------------------------------------------------------------------------------
  // Direction predictor

  logic dir_taken;

  if (IS_BIM) begin : g_bimodal
    logic unused_upd_pred;
    bp_bimodal #(.ENTRIES(BHT_ENTRIES)) u_dir (
      .clk_i, .rst_ni,
      .pred_pc_i, .pred_taken_o (dir_taken),
      .upd_en_i (dir_upd_en), .upd_pc_i, .upd_taken_i, .upd_pred_o (unused_upd_pred)
    );
  end else if (IS_GSHARE) begin : g_gshare
    logic unused_upd_pred;
    bp_gshare #(.ENTRIES(BHT_ENTRIES), .GHR_BITS(GW)) u_dir (
      .clk_i, .rst_ni,
      .pred_pc_i, .pred_ghr_i (ghr_q), .pred_taken_o (dir_taken),
      .upd_en_i (dir_upd_en), .upd_pc_i, .upd_ghr_i (upd_ghr), .upd_taken_i,
      .upd_pred_o (unused_upd_pred)
    );
  end else if (IS_TOURN) begin : g_tournament
    bp_tournament #(.ENTRIES(BHT_ENTRIES), .GHR_BITS(GW)) u_dir (
      .clk_i, .rst_ni,
      .pred_pc_i, .pred_ghr_i (ghr_q), .pred_taken_o (dir_taken),
      .upd_en_i (dir_upd_en), .upd_pc_i, .upd_ghr_i (upd_ghr), .upd_taken_i
    );
  end else if (IS_RANDOM) begin : g_random
    assign dir_taken = rnd[0];
  end else begin : g_static
    assign dir_taken = 1'b0;
  end

  // ---------------------------------------------------------------------------------------------
  // Prediction

  logic        taken;
  logic [31:0] target, garbage;

  // RANDOM's wrong targets stay a few words ahead of the PC, inside the RAM the program lives
  // in, so a wrong-path fetch never leaves the memory map.
  assign garbage = pc_plus4 + {26'd0, rnd[7:4], 2'b00};

  always_comb begin
    target = (RAS_ON && btb_kind == BTB_RET) ? {ras_top, 2'b00} : btb_target;
    if (IND_ON && btb_poly && ind_hit && (btb_kind == BTB_JUMP || btb_kind == BTB_CALL)) begin
      target = ind_target;
    end
    taken  = 1'b0;
    if (btb_hit) taken = (btb_kind == BTB_BRANCH) ? dir_taken : 1'b1;
    if (IS_RANDOM) begin
      if (btb_hit && btb_kind != BTB_BRANCH) begin
        if (rnd[3:1] == 3'd0) target = garbage;
        else if (rnd[3:1] == 3'd1) taken = 1'b0;
      end else if (!btb_hit && rnd[11:8] == 4'd0) begin
        taken  = 1'b1;
        target = garbage;
      end
    end
  end

  assign pred_taken_o  = pred_valid_i && taken;
  assign pred_target_o = pred_taken_o ? target : pc_plus4;

  // ---------------------------------------------------------------------------------------------
  // Global history register (speculative)

  if (USE_GHR) begin : g_ghr
    logic [GW-1:0] ghr_d;
    always_comb begin
      ghr_d = ghr_q;
      if (flush_i) begin
        ghr_d = flush_ghr;
      end else if (upd_restore) begin
        // History before this instruction, plus its real direction if it is a branch.
        ghr_d = (upd_kind_i == CF_BRANCH) ? GW'({upd_ghr, upd_taken_i}) : upd_ghr;
      end else if (fire && btb_hit && btb_kind == BTB_BRANCH) begin
        ghr_d = GW'({ghr_q, pred_taken_o});
      end
    end
    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) ghr_q <= '0;
      else         ghr_q <= ghr_d;
    end
  end else begin : g_no_ghr
    assign ghr_q = '0;
  end

  // ---------------------------------------------------------------------------------------------
  // Return address stack (speculative)

  if (RAS_ON) begin : g_ras
    logic [31:0] upd_pc_plus4;
    assign upd_pc_plus4 = upd_pc_i + 32'd4;
    bp_ras #(.DEPTH(RAS_DEPTH)) u_ras (
      .clk_i, .rst_ni,
      .top_o               (ras_top),
      .ptr_o               (ras_ptr),
      .fire_push_i         (fire && btb_hit && btb_kind == BTB_CALL),
      .fire_pop_i          (fire && btb_hit && btb_kind == BTB_RET),
      .fire_push_addr_i    (pc_plus4[31:2]),
      .restore_i           (flush_i || upd_restore),
      .restore_ptr_i       (flush_i ? flush_ptr : upd_ptr),
      .restore_top_i       (flush_i ? flush_top : upd_top),
      .restore_push_i      (!flush_i && upd_kind_i == CF_CALL),
      .restore_pop_i       (!flush_i && upd_kind_i == CF_RET),
      .restore_push_addr_i (upd_pc_plus4[31:2])
    );
    logic unused_ras;
    assign unused_ras = ^upd_pc_plus4[1:0];
  end else begin : g_no_ras
    assign ras_top = '0;
    assign ras_ptr = '0;
  end

  // Bits that some configurations do not use (meta of a type without history, RAS fields when
  // RAS_DEPTH = 0, the random bits outside RANDOM). Listed here instead of a lint waiver.
  logic unused_cfg;
  assign unused_cfg = ^{upd_meta_i, flush_meta_i, upd_ghr, flush_ghr, upd_ptr, flush_ptr,
                        upd_top, flush_top, rnd, ras_ptr, dir_upd_en,
                        fire, upd_restore, ghr_q, btb_poly, btb_upd_poly, ind_hit, ind_target};

endmodule
