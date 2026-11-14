// tb_top: simulation top for Verilator. rv_core + sim_mem; clock and reset come from the C++
// harness (sim/main.cpp), which also reads the RVFI and predictor-update ports to write the
// trace, decide when the program has ended and count mispredictions.
module tb_top #(
  parameter BP_TYPE = "STATIC_NT"
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  output logic        halt_o,
  output logic        mem_err_o,

  output logic        rvfi_valid,
  output logic [63:0] rvfi_order,
  output logic [31:0] rvfi_insn,
  output logic        rvfi_trap,
  output logic        rvfi_halt,
  output logic        rvfi_intr,
  output logic [1:0]  rvfi_mode,
  output logic [1:0]  rvfi_ixl,
  output logic [4:0]  rvfi_rs1_addr,
  output logic [4:0]  rvfi_rs2_addr,
  output logic [31:0] rvfi_rs1_rdata,
  output logic [31:0] rvfi_rs2_rdata,
  output logic [4:0]  rvfi_rd_addr,
  output logic [31:0] rvfi_rd_wdata,
  output logic [31:0] rvfi_pc_rdata,
  output logic [31:0] rvfi_pc_wdata,
  output logic [31:0] rvfi_mem_addr,
  output logic [3:0]  rvfi_mem_rmask,
  output logic [3:0]  rvfi_mem_wmask,
  output logic [31:0] rvfi_mem_rdata,
  output logic [31:0] rvfi_mem_wdata,

  output logic        bp_upd_valid_o,
  output logic [31:0] bp_upd_pc_o,
  output logic [2:0]  bp_upd_kind_o,
  output logic        bp_upd_taken_o,
  output logic [31:0] bp_upd_target_o,
  output logic        bp_upd_mispredict_o
);

  logic        imem_req, imem_gnt, imem_rvalid;
  logic [31:0] imem_addr, imem_rdata;
  logic        dmem_req, dmem_we, dmem_gnt, dmem_rvalid;
  logic [3:0]  dmem_be;
  logic [31:0] dmem_addr, dmem_wdata, dmem_rdata;

  rv_core #(
    .BP_TYPE (BP_TYPE)
  ) u_core (
    .clk_i               (clk_i),
    .rst_ni              (rst_ni),
    .imem_req_o          (imem_req),
    .imem_addr_o         (imem_addr),
    .imem_gnt_i          (imem_gnt),
    .imem_rvalid_i       (imem_rvalid),
    .imem_rdata_i        (imem_rdata),
    .dmem_req_o          (dmem_req),
    .dmem_addr_o         (dmem_addr),
    .dmem_we_o           (dmem_we),
    .dmem_be_o           (dmem_be),
    .dmem_wdata_o        (dmem_wdata),
    .dmem_gnt_i          (dmem_gnt),
    .dmem_rvalid_i       (dmem_rvalid),
    .dmem_rdata_i        (dmem_rdata),
    .halt_o              (halt_o),
    .rvfi_valid          (rvfi_valid),
    .rvfi_order          (rvfi_order),
    .rvfi_insn           (rvfi_insn),
    .rvfi_trap           (rvfi_trap),
    .rvfi_halt           (rvfi_halt),
    .rvfi_intr           (rvfi_intr),
    .rvfi_mode           (rvfi_mode),
    .rvfi_ixl            (rvfi_ixl),
    .rvfi_rs1_addr       (rvfi_rs1_addr),
    .rvfi_rs2_addr       (rvfi_rs2_addr),
    .rvfi_rs1_rdata      (rvfi_rs1_rdata),
    .rvfi_rs2_rdata      (rvfi_rs2_rdata),
    .rvfi_rd_addr        (rvfi_rd_addr),
    .rvfi_rd_wdata       (rvfi_rd_wdata),
    .rvfi_pc_rdata       (rvfi_pc_rdata),
    .rvfi_pc_wdata       (rvfi_pc_wdata),
    .rvfi_mem_addr       (rvfi_mem_addr),
    .rvfi_mem_rmask      (rvfi_mem_rmask),
    .rvfi_mem_wmask      (rvfi_mem_wmask),
    .rvfi_mem_rdata      (rvfi_mem_rdata),
    .rvfi_mem_wdata      (rvfi_mem_wdata),
    .bp_upd_valid_o      (bp_upd_valid_o),
    .bp_upd_pc_o         (bp_upd_pc_o),
    .bp_upd_kind_o       (bp_upd_kind_o),
    .bp_upd_taken_o      (bp_upd_taken_o),
    .bp_upd_target_o     (bp_upd_target_o),
    .bp_upd_mispredict_o (bp_upd_mispredict_o)
  );

  sim_mem u_mem (
    .clk_i         (clk_i),
    .rst_ni        (rst_ni),
    .imem_req_i    (imem_req),
    .imem_addr_i   (imem_addr),
    .imem_gnt_o    (imem_gnt),
    .imem_rvalid_o (imem_rvalid),
    .imem_rdata_o  (imem_rdata),
    .dmem_req_i    (dmem_req),
    .dmem_addr_i   (dmem_addr),
    .dmem_we_i     (dmem_we),
    .dmem_be_i     (dmem_be),
    .dmem_wdata_i  (dmem_wdata),
    .dmem_gnt_o    (dmem_gnt),
    .dmem_rvalid_o (dmem_rvalid),
    .dmem_rdata_o  (dmem_rdata),
    .err_o         (mem_err_o)
  );

endmodule
