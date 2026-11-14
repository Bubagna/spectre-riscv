// bp_bimodal: one 2-bit saturating counter per table entry, indexed by the PC alone.
//
// Learns the bias of each branch (loops, error checks) but cannot learn a pattern that depends
// on what other branches did. Index = pc[2 +: IW]; branches whose PCs differ only above the
// index bits share a counter (aliasing), there is no tag.
//
// Prediction reads port A combinationally; training is a read-modify-write on port B at update
// time, so the counter used for training is the current one, not a stale copy carried from IF.
// upd_pred_o is that current counter's prediction, used by the tournament chooser.
module bp_bimodal #(
  parameter int unsigned ENTRIES = 512
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  input  logic [31:0] pred_pc_i,
  output logic        pred_taken_o,

  input  logic        upd_en_i,     // a conditional branch resolved
  input  logic [31:0] upd_pc_i,
  input  logic        upd_taken_i,
  output logic        upd_pred_o
);
  localparam int unsigned IW = $clog2(ENTRIES);

  logic [1:0] pred_ctr, upd_ctr;

  // Reset value 1 = weakly not taken: a branch never seen before costs one taken outcome to
  // flip, the same cost as the static not-taken baseline.
  bp_ram #(.DEPTH(ENTRIES), .WIDTH(2), .RESET_VAL(2'd1)) u_table (
    .clk_i, .rst_ni,
    .ra_addr_i (pred_pc_i[2 +: IW]), .ra_data_o (pred_ctr),
    .rb_addr_i (upd_pc_i[2 +: IW]),  .rb_data_o (upd_ctr),
    .we_i      (upd_en_i),
    .wa_addr_i (upd_pc_i[2 +: IW]),
    .wa_data_i (bp_pkg::ctr_next(upd_ctr, upd_taken_i))
  );

  assign pred_taken_o = pred_ctr[1];
  assign upd_pred_o   = upd_ctr[1];

  // Only the index bits of the PC matter here, and a prediction needs only the counter MSB.
  logic unused_pc;
  assign unused_pc = ^{pred_pc_i[31:IW+2], pred_pc_i[1:0], upd_pc_i[31:IW+2], upd_pc_i[1:0],
                       pred_ctr[0]};

endmodule
