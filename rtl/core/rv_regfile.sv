// rv_regfile: 31 x 32-bit integer register file, two read ports and one write port.
//
// Timing: reads are combinational (ID stage); the write happens at the clock edge (WB stage).
// The read ports bypass the value being written in the same cycle, which is the flop-based
// equivalent of "write in the first half, read in the second half": an instruction in ID that
// reads a register being written back by WB gets the new value, so WB -> ID needs no stall.
//
// The registers are reset to zero so that RTL and ISS start from the same state; real cores
// usually skip this reset to save area, and the test start-up code zeroes them anyway.
module rv_regfile (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic [4:0]  raddr_a_i,
  output logic [31:0] rdata_a_o,
  input  logic [4:0]  raddr_b_i,
  output logic [31:0] rdata_b_o,
  input  logic        we_i,
  input  logic [4:0]  waddr_i,
  input  logic [31:0] wdata_i
);

  logic [31:0] regs_q [1:31];

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      for (int i = 1; i < 32; i++) regs_q[i] <= 32'b0;
    end else if (we_i && waddr_i != 5'd0) begin
      regs_q[waddr_i] <= wdata_i;
    end
  end

  function automatic logic [31:0] read_port(input logic [4:0] addr);
    if (addr == 5'd0)                       return 32'b0;
    else if (we_i && waddr_i == addr)       return wdata_i;
    else                                    return regs_q[addr];
  endfunction

  assign rdata_a_o = read_port(raddr_a_i);
  assign rdata_b_o = read_port(raddr_b_i);

endmodule
