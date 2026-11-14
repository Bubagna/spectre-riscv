// Test wrapper: rv_decoder with the decoded struct split into ports, so cocotb can read fields
// by name instead of by bit position.
module decoder_wrap
  import rv_pkg::*;
(
  input  logic [31:0] insn_i,
  output logic [4:0]  rs1, rs2, rd,
  output logic        use_rs1, use_rs2, rd_we,
  output logic [31:0] imm,
  output logic [3:0]  alu_op,
  output logic [1:0]  opa_sel,
  output logic        opb_sel,
  output logic [1:0]  res_sel,
  output logic        is_load, is_store,
  output logic [1:0]  mem_size,
  output logic        mem_unsigned,
  output logic        is_branch, is_jal, is_jalr,
  output logic [2:0]  br_funct3,
  output logic [2:0]  cf_kind,
  output logic [11:0] csr_addr,
  output logic        is_fence_i, illegal, ecall, ebreak
);
  decoded_t d;
  rv_decoder u_dec (.insn_i(insn_i), .dec_o(d));
  assign rs1 = d.rs1;  assign rs2 = d.rs2;  assign rd = d.rd;
  assign use_rs1 = d.use_rs1;  assign use_rs2 = d.use_rs2;  assign rd_we = d.rd_we;
  assign imm = d.imm;  assign alu_op = d.alu_op;  assign opa_sel = d.opa_sel;
  assign opb_sel = d.opb_sel;  assign res_sel = d.res_sel;
  assign is_load = d.is_load;  assign is_store = d.is_store;  assign mem_size = d.mem_size;
  assign mem_unsigned = d.mem_unsigned;  assign is_branch = d.is_branch;
  assign is_jal = d.is_jal;  assign is_jalr = d.is_jalr;  assign br_funct3 = d.br_funct3;
  assign cf_kind = d.cf_kind;  assign csr_addr = d.csr_addr;  assign is_fence_i = d.is_fence_i;
  assign illegal = d.illegal;  assign ecall = d.ecall;  assign ebreak = d.ebreak;
endmodule
