// sim_mem: simulation-only memory system for tb_top (SPEC section 2).
//
// Two ports (instruction, data) into one 1 MiB RAM at RAM_BASE, plus MMIO at 0x1000_0000.
//
// Timing
//   A request is accepted in the cycle req && gnt. Reads and writes take effect at that clock
//   edge, so accesses on the two ports are ordered by grant time (needed for self-modifying
//   code: a store granted before a FENCE.I leaves EX is visible to every later fetch).
//   The response (rvalid, rdata) comes back at least one cycle later, in order, one per cycle.
//   With +stall=P (0..100) each cycle gnt is withheld with probability P% and a ready response
//   is held back with probability P%, independently on each port; +seed=N picks the sequence.
//
// Errors (err_o, and a message): data access outside RAM and MMIO, response queue overflow, and
// protocol violations by the core (a request withdrawn or changed before its grant).
// Instruction fetches outside RAM are not errors: a wrong-path fetch may go anywhere. They
// read 0, an illegal instruction, so if such a fetch ever retires it traps.
//
// MMIO: PUTCHAR prints its low byte here. TOHOST and HALT are decoded by the C++ harness from
// the RVFI port, so the run ends exactly when the store retires.
module sim_mem #(
  parameter logic [31:0] RAM_BASE  = 32'h8000_0000,
  parameter int unsigned RAM_BYTES = 1 << 20
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  input  logic        imem_req_i,
  input  logic [31:0] imem_addr_i,
  output logic        imem_gnt_o,
  output logic        imem_rvalid_o,
  output logic [31:0] imem_rdata_o,

  input  logic        dmem_req_i,
  input  logic [31:0] dmem_addr_i,
  input  logic        dmem_we_i,
  input  logic [3:0]  dmem_be_i,
  input  logic [31:0] dmem_wdata_i,
  output logic        dmem_gnt_o,
  output logic        dmem_rvalid_o,
  output logic [31:0] dmem_rdata_o,

  output logic        err_o
);

  localparam int unsigned WORDS = RAM_BYTES / 4;
  localparam int unsigned AW    = $clog2(WORDS);
  localparam logic [31:0] MMIO_TOHOST  = 32'h1000_0000;
  localparam logic [31:0] MMIO_PUTCHAR = 32'h1000_0004;
  localparam logic [31:0] MMIO_HALT    = 32'h1000_0008;
  localparam int unsigned QD = 8;    // response queue depth per port

  logic [31:0] ram [WORDS];

  int unsigned stall_pct;
  bit          quiet;
  int unsigned rng_seed;
  int unsigned rng_q;

  initial begin
    int unsigned seed;
    stall_pct = 0;
    seed      = 1;
    if ($value$plusargs("stall=%d", stall_pct)) begin
      if (stall_pct > 100) stall_pct = 100;
    end
    void'($value$plusargs("seed=%d", seed));
    quiet = $test$plusargs("quiet") != 0;
    rng_seed = seed * 32'h9E37_79B9 + 32'h7F4A_7C15;
    if (rng_seed == 0) rng_seed = 1;
    for (int i = 0; i < WORDS; i++) ram[i] = 32'b0;
  end

  // ---------------------------------------------------------------- ELF loader backdoor
  export "DPI-C" function simmem_write_byte;
  function automatic void simmem_write_byte(input int unsigned addr, input byte unsigned data);
    int unsigned off;
    off = addr - RAM_BASE;
    ram[off >> 2][8 * (off & 3) +: 8] = data;
  endfunction

  // ---------------------------------------------------------------- randomness
  function automatic int unsigned xorshift32(input int unsigned x);
    int unsigned y;
    y = x;
    y = y ^ (y << 13);
    y = y ^ (y >> 17);
    y = y ^ (y << 5);
    return y;
  endfunction

  // A fresh 32-bit number each cycle; its four bytes give four independent decisions.
  logic ig_block_q, ir_block_q, dg_block_q, dr_block_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      rng_q      <= rng_seed;
      ig_block_q <= 1'b0;
      ir_block_q <= 1'b0;
      dg_block_q <= 1'b0;
      dr_block_q <= 1'b0;
    end else begin
      int unsigned r;
      r = xorshift32(rng_q);
      rng_q      <= r;
      ig_block_q <= ((r         & 32'hFF) * 100 / 256) < stall_pct;
      ir_block_q <= (((r >> 8)  & 32'hFF) * 100 / 256) < stall_pct;
      dg_block_q <= (((r >> 16) & 32'hFF) * 100 / 256) < stall_pct;
      dr_block_q <= (((r >> 24) & 32'hFF) * 100 / 256) < stall_pct;
    end
  end

  function automatic logic in_ram(input logic [31:0] a);
    return (a - RAM_BASE) < RAM_BYTES;
  endfunction

  function automatic logic in_mmio(input logic [31:0] a);
    return a == MMIO_TOHOST || a == MMIO_PUTCHAR || a == MMIO_HALT;
  endfunction

  function automatic logic [AW-1:0] widx(input logic [31:0] a);
    return AW'((a - RAM_BASE) >> 2);
  endfunction

  // ---------------------------------------------------------------- handshakes
  assign imem_gnt_o = imem_req_i && !ig_block_q;
  assign dmem_gnt_o = dmem_req_i && !dg_block_q;

  logic [31:0] iq_data [QD];
  logic [31:0] dq_data [QD];
  int unsigned iq_head, iq_cnt, dq_head, dq_cnt;

  assign imem_rvalid_o = (iq_cnt != 0) && !ir_block_q;
  assign imem_rdata_o  = imem_rvalid_o ? iq_data[iq_head] : 32'hDEAD_BEEF;
  assign dmem_rvalid_o = (dq_cnt != 0) && !dr_block_q;
  assign dmem_rdata_o  = dmem_rvalid_o ? dq_data[dq_head] : 32'hDEAD_BEEF;

  // Previous cycle's request, for the protocol checker.
  logic        ipend_q, dpend_q;
  logic [31:0] ipend_addr_q, dpend_addr_q, dpend_wdata_q;
  logic        dpend_we_q;
  logic [3:0]  dpend_be_q;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      iq_head <= 0;
      iq_cnt  <= 0;
      dq_head <= 0;
      dq_cnt  <= 0;
      err_o   <= 1'b0;
      ipend_q <= 1'b0;
      dpend_q <= 1'b0;
    end else begin
      int unsigned icnt, dcnt;
      icnt = iq_cnt;
      dcnt = dq_cnt;

      // Responses leave
      if (imem_rvalid_o) begin
        iq_head <= (iq_head + 1) % QD;
        icnt    = icnt - 1;
      end
      if (dmem_rvalid_o) begin
        dq_head <= (dq_head + 1) % QD;
        dcnt    = dcnt - 1;
      end

      // Instruction request accepted
      if (imem_req_i && imem_gnt_o) begin
        if (icnt >= QD) begin
          $display("sim_mem: ERROR instruction response queue overflow");
          err_o <= 1'b1;
        end
        iq_data[(iq_head + iq_cnt) % QD] <= in_ram(imem_addr_i) ? ram[widx(imem_addr_i)] : 32'b0;
        icnt = icnt + 1;
      end

      // Data request accepted
      if (dmem_req_i && dmem_gnt_o) begin
        logic [31:0] rd;
        rd = 32'b0;
        if (dcnt >= QD) begin
          $display("sim_mem: ERROR data response queue overflow");
          err_o <= 1'b1;
        end
        if (in_ram(dmem_addr_i)) begin
          rd = ram[widx(dmem_addr_i)];
          if (dmem_we_i) begin
            for (int b = 0; b < 4; b++)
              if (dmem_be_i[b]) ram[widx(dmem_addr_i)][8*b +: 8] <= dmem_wdata_i[8*b +: 8];
          end
        end else if (in_mmio(dmem_addr_i)) begin
          if (dmem_we_i && dmem_addr_i == MMIO_PUTCHAR && dmem_be_i[0] && !quiet) begin
            $write("%c", dmem_wdata_i[7:0]);
          end
        end else begin
          $display("sim_mem: ERROR %s outside RAM/MMIO at %h", dmem_we_i ? "store" : "load",
                   dmem_addr_i);
          err_o <= 1'b1;
        end
        dq_data[(dq_head + dq_cnt) % QD] <= rd;
        dcnt = dcnt + 1;
      end

      iq_cnt <= icnt;
      dq_cnt <= dcnt;

      // Protocol: once a request is presented it stays, unchanged, until granted.
      if (ipend_q && (!imem_req_i || imem_addr_i != ipend_addr_q)) begin
        $display("sim_mem: ERROR instruction request changed before its grant");
        err_o <= 1'b1;
      end
      if (dpend_q && (!dmem_req_i || dmem_addr_i != dpend_addr_q || dmem_we_i != dpend_we_q ||
                      dmem_be_i != dpend_be_q || (dmem_we_i && dmem_wdata_i != dpend_wdata_q))) begin
        $display("sim_mem: ERROR data request changed before its grant");
        err_o <= 1'b1;
      end
      if (imem_req_i && imem_addr_i[1:0] != 2'b00) begin
        $display("sim_mem: ERROR instruction address %h not word aligned", imem_addr_i);
        err_o <= 1'b1;
      end
      ipend_q       <= imem_req_i && !imem_gnt_o;
      ipend_addr_q  <= imem_addr_i;
      dpend_q       <= dmem_req_i && !dmem_gnt_o;
      dpend_addr_q  <= dmem_addr_i;
      dpend_we_q    <= dmem_we_i;
      dpend_be_q    <= dmem_be_i;
      dpend_wdata_q <= dmem_wdata_i;
    end
  end

endmodule
