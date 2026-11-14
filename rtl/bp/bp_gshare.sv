// bp_gshare: 2-bit counters indexed by PC xor global history (McFarling, 1993).
//
// The global history register (GHR) holds the directions of the last GHR_BITS predicted
// conditional branches, newest in bit 0. XOR-ing it into the PC index gives each (branch,
// recent path) pair its own counter, so patterns like T,N,T,N or "same outcome as the branch
// before" become learnable. The GHR itself lives in bpu (it is speculative state); this module
// only receives the history value to use for each access:
//   - predict: the current speculative GHR
//   - update:  the GHR that was current when this branch was predicted (from its meta), so
//              training touches exactly the counter that made the prediction.
//
// Index = pc[2 +: IW] ^ zero_extend(ghr), GHR_BITS <= IW (checked in bpu).
// Timing: XOR in front of the table read adds one gate level to the bimodal path.
module bp_gshare #(
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
  input  logic                upd_taken_i,
  output logic                upd_pred_o
);
  localparam int unsigned IW = $clog2(ENTRIES);

  logic [IW-1:0] pred_idx, upd_idx;
  logic [1:0]    pred_ctr, upd_ctr;

  assign pred_idx = pred_pc_i[2 +: IW] ^ IW'(pred_ghr_i);
  assign upd_idx  = upd_pc_i[2 +: IW]  ^ IW'(upd_ghr_i);

  bp_ram #(.DEPTH(ENTRIES), .WIDTH(2), .RESET_VAL(2'd1)) u_table (
    .clk_i, .rst_ni,
    .ra_addr_i (pred_idx), .ra_data_o (pred_ctr),
    .rb_addr_i (upd_idx),  .rb_data_o (upd_ctr),
    .we_i      (upd_en_i),
    .wa_addr_i (upd_idx),
    .wa_data_i (bp_pkg::ctr_next(upd_ctr, upd_taken_i))
  );

  assign pred_taken_o = pred_ctr[1];
  assign upd_pred_o   = upd_ctr[1];

  logic unused_pc;
  assign unused_pc = ^{pred_pc_i[31:IW+2], pred_pc_i[1:0], upd_pc_i[31:IW+2], upd_pc_i[1:0],
                       pred_ctr[0]};

endmodule
