// bp_ind: indirect target table, for jumps and calls that go to more than one target.
//
// The BTB remembers only the last target of a jump. That is right for JAL and for a JALR with
// one target, and wrong almost every time for a switch dispatch or a function-pointer call whose
// target changes (the interp benchmark: 95% of its dispatch JALRs mispredicted). The target
// such a jump goes to usually depends on where the previous indirect jumps went (in an
// interpreter: on the previous bytecodes), so this table is indexed by the PC xor a short path
// history of indirect targets (Chang, Hao, Patt, "Target prediction for indirect jumps", 1997):
//
//   index = pc[2 +: IW] ^ path_q                     IW = log2(ENTRIES)
//   entry = {valid, target[31:2]}
//   path_q <= (path_q << 2) ^ target[2 +: IW]        at every training write
//
// bpu uses the entry only when the BTB says the PC is a jump or call whose "polymorphic" bit is
// set (bp_btb), and the entry is valid; otherwise the BTB target stands.
//
// Training and path history are updated at resolve time (upd_en_i), with the real target,
// so they are not speculative: no checkpoint in the meta and no repair. The price is that a
// prediction made while an older indirect jump is still in flight sees the history without
// it; the training of that prediction then uses the history at resolve time, which may index a
// different entry. That only costs accuracy, and in this pipeline (resolve in EX, two stages
// after IF) two indirect jumps are almost never that close.
//
// Timing: predict path is one XOR plus an IW-level read mux, in parallel with the BTB; bpu adds
// one 2:1 mux on the target.
module bp_ind #(
  parameter int unsigned ENTRIES = 32
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  input  logic [31:0] pred_pc_i,
  output logic        pred_hit_o,
  output logic [31:0] pred_target_o,

  input  logic        upd_en_i,       // a polymorphic jump or call resolved: train
  input  logic [31:0] upd_pc_i,
  input  logic [31:0] upd_target_i
);
  import bp_pkg::*;

  localparam int unsigned IW = $clog2(ENTRIES);
  localparam int unsigned EW = 1 + PC_W;

  logic [IW-1:0] path_q, pred_idx, upd_idx;
  logic [EW-1:0] pred_e, unused_rb;

  assign pred_idx = pred_pc_i[2 +: IW] ^ path_q;
  assign upd_idx  = upd_pc_i[2 +: IW] ^ path_q;

  bp_ram #(.DEPTH(ENTRIES), .WIDTH(EW), .RESET_VAL('0)) u_table (
    .clk_i, .rst_ni,
    .ra_addr_i (pred_idx), .ra_data_o (pred_e),
    .rb_addr_i (upd_idx),  .rb_data_o (unused_rb),
    .we_i      (upd_en_i), .wa_addr_i (upd_idx), .wa_data_i ({1'b1, upd_target_i[31:2]})
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni)       path_q <= '0;
    else if (upd_en_i) path_q <= IW'({path_q, 2'b00}) ^ upd_target_i[2 +: IW];
  end

  assign pred_hit_o    = pred_e[EW-1];
  assign pred_target_o = {pred_e[PC_W-1:0], 2'b00};

  // Only the index bits of the PCs are used, and training never reads the table.
  logic unused_bits;
  assign unused_bits = ^{pred_pc_i[31:IW+2], pred_pc_i[1:0], upd_pc_i[31:IW+2], upd_pc_i[1:0],
                         upd_target_i[1:0], unused_rb};

endmodule
