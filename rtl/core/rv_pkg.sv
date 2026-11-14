// rv_pkg: types, constants and small helper functions shared by the RV32I core.
//
// Everything here is combinational. The decoded-instruction struct is the contract between the
// decoder (ID) and the execute stage; pipe_t is the payload of the EX/MEM and MEM/WB registers.
package rv_pkg;

  // ---------------------------------------------------------------- opcodes (insn[6:0])
  localparam logic [6:0] OP_LUI    = 7'b0110111;
  localparam logic [6:0] OP_AUIPC  = 7'b0010111;
  localparam logic [6:0] OP_JAL    = 7'b1101111;
  localparam logic [6:0] OP_JALR   = 7'b1100111;
  localparam logic [6:0] OP_BRANCH = 7'b1100011;
  localparam logic [6:0] OP_LOAD   = 7'b0000011;
  localparam logic [6:0] OP_STORE  = 7'b0100011;
  localparam logic [6:0] OP_IMM    = 7'b0010011;
  localparam logic [6:0] OP_REG    = 7'b0110011;
  localparam logic [6:0] OP_FENCE  = 7'b0001111;
  localparam logic [6:0] OP_SYSTEM = 7'b1110011;

  // ---------------------------------------------------------------- CSR addresses (Zicntr)
  localparam logic [11:0] CSR_CYCLE    = 12'hC00;
  localparam logic [11:0] CSR_TIME     = 12'hC01;
  localparam logic [11:0] CSR_INSTRET  = 12'hC02;
  localparam logic [11:0] CSR_CYCLEH   = 12'hC80;
  localparam logic [11:0] CSR_TIMEH    = 12'hC81;
  localparam logic [11:0] CSR_INSTRETH = 12'hC82;
  localparam logic [11:0] CSR_MHARTID  = 12'hF14;

  // ---------------------------------------------------------------- decoded fields
  typedef enum logic [3:0] {
    ALU_ADD, ALU_SUB, ALU_SLL, ALU_SLT, ALU_SLTU, ALU_XOR, ALU_SRL, ALU_SRA, ALU_OR, ALU_AND
  } alu_op_e;

  // LUI is "0 + imm" and AUIPC is "pc + imm", so the ALU needs no pass-through operation.
  typedef enum logic [1:0] { OPA_RS1, OPA_PC, OPA_ZERO } opa_sel_e;
  typedef enum logic [0:0] { OPB_RS2, OPB_IMM } opb_sel_e;

  // Which value EX writes into the pipeline as the instruction result. Load data is not known
  // until WB, so loads are selected there by is_load, not here.
  typedef enum logic [1:0] { RES_ALU, RES_PC4, RES_CSR } res_sel_e;

  // Same encoding as funct3[1:0] of loads and stores.
  typedef enum logic [1:0] { MEM_B = 2'd0, MEM_H = 2'd1, MEM_W = 2'd2 } mem_size_e;

  // Control-flow kind, same encoding as upd_kind_i of the predictor (SPEC section 5).
  typedef enum logic [2:0] {
    CF_NONE = 3'd0, CF_BRANCH = 3'd1, CF_JAL = 3'd2, CF_JALR = 3'd3, CF_CALL = 3'd4, CF_RET = 3'd5
  } cf_kind_e;

  typedef struct packed {
    logic [4:0]  rs1;
    logic [4:0]  rs2;
    logic [4:0]  rd;
    logic        use_rs1;      // operand really read: drives hazards, forwarding and RVFI
    logic        use_rs2;
    logic        rd_we;        // writes a register other than x0
    logic [31:0] imm;
    alu_op_e     alu_op;
    opa_sel_e    opa_sel;
    opb_sel_e    opb_sel;
    res_sel_e    res_sel;
    logic        is_load;
    logic        is_store;
    mem_size_e   mem_size;
    logic        mem_unsigned;
    logic        is_branch;
    logic        is_jal;
    logic        is_jalr;
    logic [2:0]  br_funct3;
    cf_kind_e    cf_kind;
    logic [11:0] csr_addr;
    logic        is_fence_i;
    logic        illegal;
    logic        ecall;
    logic        ebreak;
  } decoded_t;

  // Payload of the EX/MEM and MEM/WB registers: everything the later stages and RVFI need.
  typedef struct packed {
    logic [31:0] pc;
    logic [31:0] insn;
    logic [31:0] npc;          // architectural next pc (RVFI pc_wdata)
    logic [4:0]  rs1;          // 0 when not used (RVFI convention)
    logic [4:0]  rs2;
    logic [31:0] rs1_val;      // operand values after forwarding, for RVFI
    logic [31:0] rs2_val;
    logic [4:0]  rd;
    logic        rd_we;
    logic [31:0] result;       // ALU / pc+4 / CSR value; replaced by load data in WB
    logic        is_load;
    logic        is_store;
    mem_size_e   mem_size;
    logic        mem_unsigned;
    logic [31:0] addr;         // byte address of the access
    logic [3:0]  be;           // byte lanes of the access
    logic [31:0] wdata;        // store data, already shifted to the byte lanes
    logic        trap;
  } pipe_t;

  // ---------------------------------------------------------------- helpers
  function automatic logic branch_taken(input logic [2:0] funct3, input logic [31:0] a,
                                        input logic [31:0] b);
    unique case (funct3)
      3'b000:  return a == b;                    // beq
      3'b001:  return a != b;                    // bne
      3'b100:  return $signed(a) <  $signed(b);  // blt
      3'b101:  return $signed(a) >= $signed(b);  // bge
      3'b110:  return a <  b;                    // bltu
      3'b111:  return a >= b;                    // bgeu
      default: return 1'b0;                      // never decoded as a branch
    endcase
  endfunction

  function automatic logic [3:0] byte_enable(input mem_size_e size, input logic [1:0] offset);
    unique case (size)
      MEM_B:   return 4'b0001 << offset;
      MEM_H:   return offset[1] ? 4'b1100 : 4'b0011;
      default: return 4'b1111;
    endcase
  endfunction

  function automatic logic misaligned(input mem_size_e size, input logic [1:0] offset);
    unique case (size)
      MEM_B:   return 1'b0;
      MEM_H:   return offset[0];
      default: return offset != 2'b00;
    endcase
  endfunction

  // Replicating the low bytes puts the data on the right lanes for every aligned offset, so no
  // shifter is needed; the byte enables pick the lanes that are really written.
  function automatic logic [31:0] store_lanes(input mem_size_e size, input logic [31:0] data);
    unique case (size)
      MEM_B:   return {4{data[7:0]}};
      MEM_H:   return {2{data[15:0]}};
      default: return data;
    endcase
  endfunction

  function automatic logic [31:0] load_extend(input mem_size_e size, input logic is_unsigned,
                                              input logic [1:0] offset, input logic [31:0] word);
    logic [7:0]  b;
    logic [15:0] h;
    b = word[8*offset +: 8];
    h = offset[1] ? word[31:16] : word[15:0];
    unique case (size)
      MEM_B:   return is_unsigned ? {24'b0, b} : {{24{b[7]}}, b};
      MEM_H:   return is_unsigned ? {16'b0, h} : {{16{h[15]}}, h};
      default: return word;
    endcase
  endfunction

endpackage
