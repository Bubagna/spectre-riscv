// rv_decoder: RV32I + Zicntr instruction decoder (ID stage).
//
// Purely combinational: insn_i -> dec_o in the same cycle. Every encoding that is not one of the
// 37 RV32I instructions, FENCE.I or a legal counter read sets dec_o.illegal and clears every
// field that would have a side effect, so a wrong-path or garbage word can never write anything
// even before EX turns it into a trap.
module rv_decoder
  import rv_pkg::*;
(
  input  logic [31:0] insn_i,
  output decoded_t    dec_o
);

  logic [6:0] opcode;
  logic [2:0] funct3;
  logic [6:0] funct7;
  logic [4:0] rs1, rs2, rd;

  assign opcode = insn_i[6:0];
  assign funct3 = insn_i[14:12];
  assign funct7 = insn_i[31:25];
  assign rs1    = insn_i[19:15];
  assign rs2    = insn_i[24:20];
  assign rd     = insn_i[11:7];

  logic [31:0] imm_i, imm_s, imm_b, imm_u, imm_j;
  assign imm_i = {{20{insn_i[31]}}, insn_i[31:20]};
  assign imm_s = {{20{insn_i[31]}}, insn_i[31:25], insn_i[11:7]};
  assign imm_b = {{19{insn_i[31]}}, insn_i[31], insn_i[7], insn_i[30:25], insn_i[11:8], 1'b0};
  assign imm_u = {insn_i[31:12], 12'b0};
  assign imm_j = {{11{insn_i[31]}}, insn_i[31], insn_i[19:12], insn_i[20], insn_i[30:21], 1'b0};

  // RAS hints (SPEC section 5): x1 and x5 are the link registers.
  logic rd_link, rs1_link;
  assign rd_link  = (rd == 5'd1) || (rd == 5'd5);
  assign rs1_link = (rs1 == 5'd1) || (rs1 == 5'd5);

  logic csr_known, csr_writes;
  always_comb begin
    unique case (insn_i[31:20])
      CSR_CYCLE, CSR_TIME, CSR_INSTRET, CSR_CYCLEH, CSR_TIMEH, CSR_INSTRETH, CSR_MHARTID:
        csr_known = 1'b1;
      default: csr_known = 1'b0;
    endcase
    // csrrw/csrrwi always write; the set/clear forms write unless the source is x0 / uimm 0.
    // The rs1 field is the uimm in the immediate forms, so one test covers both.
    csr_writes = (funct3[1:0] == 2'b01) || (rs1 != 5'd0);
  end

  decoded_t d;

  always_comb begin
    d              = '0;
    d.rs1          = rs1;
    d.rs2          = rs2;
    d.rd           = rd;
    d.alu_op       = ALU_ADD;
    d.opa_sel      = OPA_RS1;
    d.opb_sel      = OPB_IMM;
    d.res_sel      = RES_ALU;
    d.mem_size     = mem_size_e'(funct3[1:0]);
    d.mem_unsigned = funct3[2];
    d.br_funct3    = funct3;
    d.cf_kind      = CF_NONE;
    d.csr_addr     = insn_i[31:20];

    unique case (opcode)
      OP_LUI: begin
        d.rd_we   = 1'b1;
        d.imm     = imm_u;
        d.opa_sel = OPA_ZERO;
      end

      OP_AUIPC: begin
        d.rd_we   = 1'b1;
        d.imm     = imm_u;
        d.opa_sel = OPA_PC;
      end

      OP_JAL: begin
        d.rd_we   = 1'b1;
        d.imm     = imm_j;
        d.res_sel = RES_PC4;
        d.is_jal  = 1'b1;
        d.cf_kind = rd_link ? CF_CALL : CF_JAL;
      end

      OP_JALR: begin
        if (funct3 != 3'b000) d.illegal = 1'b1;
        d.rd_we   = 1'b1;
        d.use_rs1 = 1'b1;
        d.imm     = imm_i;
        d.res_sel = RES_PC4;
        d.is_jalr = 1'b1;
        d.cf_kind = rd_link ? CF_CALL : ((rs1_link && rd == 5'd0) ? CF_RET : CF_JALR);
      end

      OP_BRANCH: begin
        if (funct3[2:1] == 2'b01) d.illegal = 1'b1;
        d.use_rs1   = 1'b1;
        d.use_rs2   = 1'b1;
        d.imm       = imm_b;
        d.is_branch = 1'b1;
        d.cf_kind   = CF_BRANCH;
      end

      OP_LOAD: begin
        // lb lh lw lbu lhu; funct3 011, 110, 111 are not RV32I.
        if (funct3 == 3'b011 || funct3[2:1] == 2'b11) d.illegal = 1'b1;
        d.rd_we   = 1'b1;
        d.use_rs1 = 1'b1;
        d.imm     = imm_i;
        d.is_load = 1'b1;
      end

      OP_STORE: begin
        if (funct3[2] || funct3[1:0] == 2'b11) d.illegal = 1'b1;
        d.use_rs1  = 1'b1;
        d.use_rs2  = 1'b1;
        d.imm      = imm_s;
        d.is_store = 1'b1;
      end

      OP_IMM: begin
        d.rd_we   = 1'b1;
        d.use_rs1 = 1'b1;
        d.imm     = imm_i;
        unique case (funct3)
          3'b000: d.alu_op = ALU_ADD;
          3'b010: d.alu_op = ALU_SLT;
          3'b011: d.alu_op = ALU_SLTU;
          3'b100: d.alu_op = ALU_XOR;
          3'b110: d.alu_op = ALU_OR;
          3'b111: d.alu_op = ALU_AND;
          3'b001: begin
            d.alu_op = ALU_SLL;
            if (funct7 != 7'b0000000) d.illegal = 1'b1;
          end
          default: begin  // 3'b101
            d.alu_op = (funct7 == 7'b0100000) ? ALU_SRA : ALU_SRL;
            if (funct7 != 7'b0000000 && funct7 != 7'b0100000) d.illegal = 1'b1;
          end
        endcase
      end

      OP_REG: begin
        d.rd_we   = 1'b1;
        d.use_rs1 = 1'b1;
        d.use_rs2 = 1'b1;
        d.opb_sel = OPB_RS2;
        unique case (funct3)
          3'b000:  d.alu_op = funct7[5] ? ALU_SUB : ALU_ADD;
          3'b001:  d.alu_op = ALU_SLL;
          3'b010:  d.alu_op = ALU_SLT;
          3'b011:  d.alu_op = ALU_SLTU;
          3'b100:  d.alu_op = ALU_XOR;
          3'b101:  d.alu_op = funct7[5] ? ALU_SRA : ALU_SRL;
          3'b110:  d.alu_op = ALU_OR;
          default: d.alu_op = ALU_AND;
        endcase
        // Only sub and sra have funct7 = 0100000; everything else needs 0000000.
        if (!(funct7 == 7'b0000000 ||
              (funct7 == 7'b0100000 && (funct3 == 3'b000 || funct3 == 3'b101))))
          d.illegal = 1'b1;
      end

      OP_FENCE: begin
        // FENCE is a NOP: one in-order hart and an uncached memory are already ordered.
        // Reserved fields are ignored, as the ISA asks.
        if (funct3 == 3'b001)      d.is_fence_i = 1'b1;
        else if (funct3 != 3'b000) d.illegal    = 1'b1;
      end

      OP_SYSTEM: begin
        if (funct3 == 3'b000) begin
          if (insn_i == 32'h0000_0073)      d.ecall   = 1'b1;
          else if (insn_i == 32'h0010_0073) d.ebreak  = 1'b1;
          else                              d.illegal = 1'b1;  // mret, wfi, ... : no privilege
        end else if (funct3 == 3'b100 || !csr_known || csr_writes) begin
          d.illegal = 1'b1;
        end else begin
          // Writes are illegal, so a legal CSR instruction never reads rs1 (it is x0).
          d.rd_we   = 1'b1;
          d.res_sel = RES_CSR;
        end
      end

      default: d.illegal = 1'b1;
    endcase

    if (rd == 5'd0) d.rd_we = 1'b0;

    // An illegal word must not look like anything with a side effect.
    if (d.illegal) begin
      d.rd_we      = 1'b0;
      d.use_rs1    = 1'b0;
      d.use_rs2    = 1'b0;
      d.is_load    = 1'b0;
      d.is_store   = 1'b0;
      d.is_branch  = 1'b0;
      d.is_jal     = 1'b0;
      d.is_jalr    = 1'b0;
      d.cf_kind    = CF_NONE;
      d.is_fence_i = 1'b0;
    end
  end

  assign dec_o = d;

endmodule
