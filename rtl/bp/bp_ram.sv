// bp_ram: small predictor table, two combinational read ports and one synchronous write port.
//
// Every predictor table (BTB, counter tables, chooser) goes through this module, so turning the
// flops into an SRAM macro later touches only this file and the timing of its users.
// Port A serves the prediction lookup in IF, port B the read-modify-write of training at update
// time. A write becomes visible to both read ports in the next cycle (no write-to-read bypass),
// and the golden model tb/bp/bp_model.py assumes exactly that.
//
// Timing: read address to data is one DEPTH:1 mux per port (about log2(DEPTH) mux levels).
// An SRAM version would need a registered address, i.e. the lookup moves one cycle earlier.
module bp_ram #(
  parameter int unsigned      DEPTH     = 64,
  parameter int unsigned      WIDTH     = 2,
  parameter logic [WIDTH-1:0] RESET_VAL = '0,
  localparam int unsigned     AW        = $clog2(DEPTH)
) (
  input  logic             clk_i,
  input  logic             rst_ni,

  input  logic [AW-1:0]    ra_addr_i,
  output logic [WIDTH-1:0] ra_data_o,

  input  logic [AW-1:0]    rb_addr_i,
  output logic [WIDTH-1:0] rb_data_o,

  input  logic             we_i,
  input  logic [AW-1:0]    wa_addr_i,
  input  logic [WIDTH-1:0] wa_data_i
);

  logic [WIDTH-1:0] mem_q [DEPTH];

  // Reset every entry so the predictor starts from a known state and the RTL matches the model
  // cycle by cycle. An SRAM has no reset: it would need an init walk or a valid bit per entry.
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      for (int i = 0; i < DEPTH; i++) mem_q[i] <= RESET_VAL;
    end else if (we_i) begin
      mem_q[wa_addr_i] <= wa_data_i;
    end
  end

  assign ra_data_o = mem_q[ra_addr_i];
  assign rb_data_o = mem_q[rb_addr_i];

endmodule
