// rv_alu: RV32I integer ALU (EX stage). Purely combinational.
//
// One adder serves add/sub (b inverted, carry in) and the address computation of loads, stores
// and JALR. Shifts use b[4:0] only, which is also the shamt field of the immediate forms.
module rv_alu
  import rv_pkg::*;
(
  input  alu_op_e     op_i,
  input  logic [31:0] a_i,
  input  logic [31:0] b_i,
  output logic [31:0] result_o
);

  logic [4:0] shamt;
  assign shamt = b_i[4:0];

  always_comb begin
    unique case (op_i)
      ALU_ADD:  result_o = a_i + b_i;
      ALU_SUB:  result_o = a_i - b_i;
      ALU_SLL:  result_o = a_i << shamt;
      ALU_SLT:  result_o = {31'b0, $signed(a_i) < $signed(b_i)};
      ALU_SLTU: result_o = {31'b0, a_i < b_i};
      ALU_XOR:  result_o = a_i ^ b_i;
      ALU_SRL:  result_o = a_i >> shamt;
      ALU_SRA:  result_o = $unsigned($signed(a_i) >>> shamt);
      ALU_OR:   result_o = a_i | b_i;
      ALU_AND:  result_o = a_i & b_i;
      default:  result_o = 32'b0;
    endcase
  end

endmodule
