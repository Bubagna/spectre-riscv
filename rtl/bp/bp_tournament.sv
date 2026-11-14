// bp_tournament: bimodal + gshare + a per-PC chooser (McFarling's combining predictor).
//
// Some branches are predicted best by their own bias (bimodal), others by the recent path
// (gshare), and gshare needs more time to warm up because each branch spreads over many
// counters. The chooser is a table of 2-bit counters indexed by PC that learns, per branch,
// which component to trust: counter >= 2 selects gshare.
//
// Training at update time, all three tables read on port B before the write:
//   - bimodal and gshare counters move towards the outcome every time;
//   - the chooser moves only when the two components disagreed, towards the one that was right
//     (when both are right or both wrong there is nothing to learn about which is better).
// Both component predictions used for the chooser are re-read at update time, not carried from
// IF; the golden model does the same.
//
// Timing: the two component lookups and the chooser lookup run in parallel, then one 2:1 mux.
module bp_tournament #(
  parameter int unsigned ENTRIES  = 512,
  parameter int unsigned GHR_BITS = 9
) (
  input  logic                clk_i,
  input  logic                rst_ni,

  input  logic [31:0]         pred_pc_i,
  input  logic [GHR_BITS-1:0] pred_ghr_i,
  output logic                pred_taken_o,

  input  logic                upd_en_i,
  input  logic [31:0]         upd_pc_i,
  input  logic [GHR_BITS-1:0] upd_ghr_i,
  input  logic                upd_taken_i
);
  localparam int unsigned IW = $clog2(ENTRIES);

  logic       bim_pred, gsh_pred, bim_upd, gsh_upd;
  logic [1:0] cho_pred, cho_upd;
  logic       cho_we;

  bp_bimodal #(.ENTRIES(ENTRIES)) u_bimodal (
    .clk_i, .rst_ni,
    .pred_pc_i, .pred_taken_o (bim_pred),
    .upd_en_i, .upd_pc_i, .upd_taken_i, .upd_pred_o (bim_upd)
  );

  bp_gshare #(.ENTRIES(ENTRIES), .GHR_BITS(GHR_BITS)) u_gshare (
    .clk_i, .rst_ni,
    .pred_pc_i, .pred_ghr_i, .pred_taken_o (gsh_pred),
    .upd_en_i, .upd_pc_i, .upd_ghr_i, .upd_taken_i, .upd_pred_o (gsh_upd)
  );

  // Reset value 1 = weakly prefer bimodal, which is right sooner while gshare warms up.
  assign cho_we = upd_en_i && (bim_upd != gsh_upd);

  bp_ram #(.DEPTH(ENTRIES), .WIDTH(2), .RESET_VAL(2'd1)) u_chooser (
    .clk_i, .rst_ni,
    .ra_addr_i (pred_pc_i[2 +: IW]), .ra_data_o (cho_pred),
    .rb_addr_i (upd_pc_i[2 +: IW]),  .rb_data_o (cho_upd),
    .we_i      (cho_we),
    .wa_addr_i (upd_pc_i[2 +: IW]),
    .wa_data_i (bp_pkg::ctr_next(cho_upd, gsh_upd == upd_taken_i))
  );

  assign pred_taken_o = cho_pred[1] ? gsh_pred : bim_pred;

  logic unused_pc;
  assign unused_pc = ^{pred_pc_i[31:IW+2], pred_pc_i[1:0], upd_pc_i[31:IW+2], upd_pc_i[1:0],
                       cho_pred[0]};

endmodule
