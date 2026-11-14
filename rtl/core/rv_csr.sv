// rv_csr: read-only counters of Zicntr (cycle, time, instret and their high halves) + mhartid.
//
// Timing: the read is combinational and happens in EX; the counters update at the clock edge.
// cycle counts every clock since reset; time is the same counter (no separate timer here).
// instret counts retirements in WB. A read happens in EX, while up to two older instructions are
// still in MEM and WB; none of them can be cancelled any more, so the core passes their number
// in inflight_i and the read returns exactly the number of instructions retired before the
// reading one, as if the core executed one instruction at a time (same value as the ISS).
// The decoder has already turned every write and every unknown address into an illegal
// instruction, so there is no write port.
module rv_csr
  import rv_pkg::*;
(
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic        retire_i,
  input  logic [1:0]  inflight_i,   // valid older instructions in MEM and WB
  input  logic [11:0] raddr_i,
  output logic [31:0] rdata_o
);

  logic [63:0] cycle_q, instret_q, instret_rd;
  assign instret_rd = instret_q + 64'(inflight_i);

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      cycle_q   <= 64'd0;
      instret_q <= 64'd0;
    end else begin
      cycle_q <= cycle_q + 64'd1;
      if (retire_i) instret_q <= instret_q + 64'd1;
    end
  end

  always_comb begin
    unique case (raddr_i)
      CSR_CYCLE, CSR_TIME:   rdata_o = cycle_q[31:0];
      CSR_CYCLEH, CSR_TIMEH: rdata_o = cycle_q[63:32];
      CSR_INSTRET:           rdata_o = instret_rd[31:0];
      CSR_INSTRETH:          rdata_o = instret_rd[63:32];
      default:               rdata_o = 32'b0;  // mhartid = 0; other addresses never get here
    endcase
  end

endmodule
