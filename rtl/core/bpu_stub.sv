// bpu_stub: stand-in for the branch prediction unit, same ports as bpu (SPEC section 5).
//
// Used when the core is built with `define RV_BPU_STUB, so the core can be built and verified
// independently of rtl/bp/. Two modes:
//   STATIC_NT  never predicts taken. Every taken branch and every jump is a misprediction.
//   RANDOM     simulation-only stress mode. Each cycle an LFSR picks one of:
//                - not taken;
//                - taken to the target last seen for this PC in a tiny table (so correct taken
//                  predictions, which redirect fetch without a flush, are exercised too);
//                - taken to a garbage target near the PC (wrong-path code with real loads,
//                  stores and branches, which must never have an effect);
//                - taken to a garbage target anywhere in RAM, sometimes not word aligned.
//              It also predicts taken for ordinary instructions. The core must stay correct.
//
// In RANDOM mode the meta is the predicted PC, so the stub can check that the core returns the
// right meta with each update: a cheap end-to-end test of the meta plumbing down the pipeline.
// It also checks that pred_fire is never asserted together with a repair.
//
// Timing: prediction combinational from pred_pc_i and flops; everything else at the clock edge.
module bpu_stub #(
  parameter              BP_TYPE     = "STATIC_NT",
  parameter int unsigned BTB_ENTRIES = 64,    // unused, kept for interface compatibility
  parameter int unsigned BHT_ENTRIES = 512,   // unused
  parameter int unsigned GHR_BITS    = 9,     // unused
  parameter int unsigned RAS_DEPTH   = 4,     // unused
  parameter int unsigned META_W      = 32
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

  localparam logic [127:0] TYPE_S = 128'(BP_TYPE);
  localparam bit IS_RANDOM = (TYPE_S == 128'("RANDOM"));

  localparam int unsigned TBL = 16;

  logic [31:0] lfsr_q;
  logic        tbl_valid_q  [TBL];
  logic [31:0] tbl_pc_q     [TBL];
  logic [31:0] tbl_target_q [TBL];

  logic [3:0] pidx, uidx;
  assign pidx = pred_pc_i[5:2];
  assign uidx = upd_pc_i[5:2];

  logic        tbl_hit;
  logic [31:0] garbage_near, garbage_far;
  assign tbl_hit      = tbl_valid_q[pidx] && tbl_pc_q[pidx] == pred_pc_i;
  assign garbage_near = pred_pc_i + {{21{lfsr_q[17]}}, lfsr_q[16:8], 2'b00};
  assign garbage_far  = {12'h800, lfsr_q[27:10], lfsr_q[1:0]};

  always_comb begin
    pred_taken_o  = 1'b0;
    pred_target_o = pred_pc_i + 32'd4;
    pred_meta_o   = META_W'(pred_pc_i);
    if (IS_RANDOM) begin
      unique case (lfsr_q[31:29])
        3'd0, 3'd1, 3'd2: ;                                      // not taken
        3'd3, 3'd4, 3'd5: if (tbl_hit) begin                     // remembered target
          pred_taken_o  = 1'b1;
          pred_target_o = tbl_target_q[pidx];
        end
        3'd6:    begin pred_taken_o = 1'b1; pred_target_o = garbage_near; end
        default: begin pred_taken_o = 1'b1; pred_target_o = garbage_far;  end
      endcase
    end
  end

  // Galois LFSR, maximal length (taps 32, 22, 2, 1). Advanced every cycle, so the predictions
  // also change with memory stalls.
  logic [31:0] seed;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      lfsr_q <= seed;
    end else begin
      lfsr_q <= {1'b0, lfsr_q[31:1]} ^ (lfsr_q[0] ? 32'h8020_0003 : 32'h0);
    end
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      for (int i = 0; i < TBL; i++) tbl_valid_q[i] <= 1'b0;
    end else if (upd_valid_i && upd_taken_i) begin
      tbl_valid_q[uidx] <= 1'b1;
    end
  end

  always_ff @(posedge clk_i) begin
    if (upd_valid_i && upd_taken_i) begin
      tbl_pc_q[uidx]     <= upd_pc_i;
      tbl_target_q[uidx] <= upd_target_i;
    end
  end

`ifndef SYNTHESIS
  // RANDOM is simulation only, so its seed may come from a plusarg.
  initial begin
    int unsigned s;
    seed = 32'h1234_5678;
    if ($value$plusargs("seed=%d", s)) seed = 32'h1234_5678 ^ (s * 32'h9E37_79B9);
    if (seed == 32'b0) seed = 32'h1;
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (rst_ni) begin
      if (pred_fire_i && !pred_valid_i)
        $error("bpu_stub: pred_fire without pred_valid");
      if (pred_fire_i && (flush_i || (upd_valid_i && upd_mispredict_i)))
        $error("bpu_stub: pred_fire in the same cycle as a repair");
      if (flush_i && upd_valid_i && upd_mispredict_i)
        $error("bpu_stub: flush and mispredict update in the same cycle");
      if (upd_valid_i && upd_kind_i > 3'd5)
        $error("bpu_stub: bad update kind %0d", upd_kind_i);
      if (META_W >= 32 && upd_valid_i && upd_meta_i != META_W'(upd_pc_i))
        $error("bpu_stub: update meta %h does not belong to pc %h", upd_meta_i, upd_pc_i);
    end
  end
`else
  assign seed = 32'h1234_5678;
`endif

  // Ports the stub does not need; kept so it is a drop-in replacement for bpu.
  logic unused;
  assign unused = ^{upd_kind_i, upd_mispredict_i, upd_meta_i, flush_i, flush_meta_i,
                    pred_valid_i, pred_fire_i, BTB_ENTRIES[0], BHT_ENTRIES[0], GHR_BITS[0],
                    RAS_DEPTH[0]};

endmodule
