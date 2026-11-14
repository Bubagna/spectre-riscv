// rv_core: 5-stage in-order RV32I core (IF, ID, EX, MEM, WB) with a dynamic branch predictor.
//
//   IF   rv_fetch: fetch PC, predictor lookup, instruction port, fetch queue
//   ID   decode, register file read (with WB bypass), load-use hazard check
//   EX   forwarding, ALU, branch/jump resolution, misprediction and trap detection, CSR read,
//        address and byte lanes of loads and stores
//   MEM  data request (req/gnt)
//   WB   data response (rvalid), load extension, register write, retirement and RVFI
//
// Every redirect (misprediction, FENCE.I, trap) is decided in EX, in the cycle the instruction
// leaves EX. Since EX holds the oldest instruction that can redirect, a redirect only ever kills
// IF and ID, and older instructions in MEM and WB always complete: precise by construction.
//
// Stalls flow backwards through three conditions (see docs/ARCHITECTURE.md):
//   wb_wait   WB holds a load/store whose response has not arrived
//   MEM waits for gnt on its request
//   load-use  ID needs the result of a load that is in EX
// The first two freeze EX, MEM and (for wb_wait) WB; the third inserts a bubble into EX.
//
// See docs/SPEC.md for the interface contract.
module rv_core
  import rv_pkg::*;
#(
  parameter              BP_TYPE     = "GSHARE",
  parameter int unsigned BTB_ENTRIES = 64,
  parameter int unsigned BHT_ENTRIES = 512,
  parameter int unsigned GHR_BITS    = 9,
  parameter int unsigned RAS_DEPTH   = 4,
  parameter logic [31:0] BOOT_ADDR   = 32'h8000_0000,
  parameter int unsigned FETCH_DEPTH = 4
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  // Instruction port
  output logic        imem_req_o,
  output logic [31:0] imem_addr_o,
  input  logic        imem_gnt_i,
  input  logic        imem_rvalid_i,
  input  logic [31:0] imem_rdata_i,

  // Data port
  output logic        dmem_req_o,
  output logic [31:0] dmem_addr_o,
  output logic        dmem_we_o,
  output logic [3:0]  dmem_be_o,
  output logic [31:0] dmem_wdata_o,
  input  logic        dmem_gnt_i,
  input  logic        dmem_rvalid_i,
  input  logic [31:0] dmem_rdata_i,

  output logic        halt_o,

  // RVFI (riscv-formal names, NRET = 1)
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

  // Predictor update port, exported read-only for statistics
  output logic        bp_upd_valid_o,
  output logic [31:0] bp_upd_pc_o,
  output logic [2:0]  bp_upd_kind_o,
  output logic        bp_upd_taken_o,
  output logic [31:0] bp_upd_target_o,
  output logic        bp_upd_mispredict_o
);

`ifdef RV_BPU_STUB
  localparam int unsigned META_W = 32;
`else
  localparam int unsigned META_W = bp_pkg::meta_w(GHR_BITS, RAS_DEPTH);
`endif

  // ================================================================ global control
  logic        redirect;       // from EX: kill IF/ID, refetch from redirect_pc
  logic [31:0] redirect_pc;
  logic        halting_q;      // a trap has left EX: fetch nothing more
  logic        halt_q;         // the trap has retired

  logic        wb_wait;        // WB waits for its data response
  logic        adv_mem;        // MEM -> WB transfer happens this cycle (also EX -> MEM)
  logic        adv_ex;
  logic        load_use;

  // ================================================================ predictor
  logic              pred_valid, pred_taken, pred_fire;
  logic [31:0]       pred_pc, pred_target;
  logic [META_W-1:0] pred_meta;

  logic              upd_valid, upd_taken, upd_mispredict, bp_flush;
  logic [31:0]       upd_pc, upd_target;
  logic [2:0]        upd_kind;
  logic [META_W-1:0] upd_meta;

`ifdef RV_BPU_STUB
  bpu_stub #(
`else
  bpu #(
`endif
    .BP_TYPE     (BP_TYPE),
    .BTB_ENTRIES (BTB_ENTRIES),
    .BHT_ENTRIES (BHT_ENTRIES),
    .GHR_BITS    (GHR_BITS),
    .RAS_DEPTH   (RAS_DEPTH),
    .META_W      (META_W)
  ) u_bpu (
    .clk_i            (clk_i),
    .rst_ni           (rst_ni),
    .pred_valid_i     (pred_valid),
    .pred_pc_i        (pred_pc),
    .pred_taken_o     (pred_taken),
    .pred_target_o    (pred_target),
    .pred_meta_o      (pred_meta),
    .pred_fire_i      (pred_fire),
    .upd_valid_i      (upd_valid),
    .upd_pc_i         (upd_pc),
    .upd_kind_i       (upd_kind),
    .upd_taken_i      (upd_taken),
    .upd_target_i     (upd_target),
    .upd_mispredict_i (upd_mispredict),
    .upd_meta_i       (upd_meta),
    .flush_i          (bp_flush),
    .flush_meta_i     (upd_meta)
  );

  // ================================================================ IF
  logic              id_valid, id_ready;
  logic [31:0]       id_pc, id_insn, id_pred_npc;
  logic [META_W-1:0] id_meta;

  rv_fetch #(
    .BOOT_ADDR (BOOT_ADDR),
    .META_W    (META_W),
    .DEPTH     (FETCH_DEPTH)
  ) u_fetch (
    .clk_i         (clk_i),
    .rst_ni        (rst_ni),
    .redirect_i    (redirect),
    .redirect_pc_i (redirect_pc),
    .halt_i        (halting_q),
    .imem_req_o    (imem_req_o),
    .imem_addr_o   (imem_addr_o),
    .imem_gnt_i    (imem_gnt_i),
    .imem_rvalid_i (imem_rvalid_i),
    .imem_rdata_i  (imem_rdata_i),
    .pred_valid_o  (pred_valid),
    .pred_pc_o     (pred_pc),
    .pred_taken_i  (pred_taken),
    .pred_target_i (pred_target),
    .pred_meta_i   (pred_meta),
    .pred_fire_o   (pred_fire),
    .id_valid_o    (id_valid),
    .id_pc_o       (id_pc),
    .id_insn_o     (id_insn),
    .id_pred_npc_o (id_pred_npc),
    .id_meta_o     (id_meta),
    .id_ready_i    (id_ready)
  );

  // ================================================================ ID
  decoded_t    id_dec;
  logic [31:0] id_rs1_val, id_rs2_val;

  // WB write port, driven in the WB section
  logic        rf_we;
  logic [4:0]  rf_waddr;
  logic [31:0] rf_wdata;

  rv_decoder u_dec (
    .insn_i (id_insn),
    .dec_o  (id_dec)
  );

  rv_regfile u_rf (
    .clk_i     (clk_i),
    .rst_ni    (rst_ni),
    .raddr_a_i (id_dec.rs1),
    .rdata_a_o (id_rs1_val),
    .raddr_b_i (id_dec.rs2),
    .rdata_b_o (id_rs2_val),
    .we_i      (rf_we),
    .waddr_i   (rf_waddr),
    .wdata_i   (rf_wdata)
  );

  // ID/EX register
  logic              ex_valid_q;
  logic [31:0]       ex_pc_q, ex_insn_q, ex_pred_npc_q;
  decoded_t          ex_dec_q;
  logic [31:0]       ex_rs1_q, ex_rs2_q;
  logic [META_W-1:0] ex_meta_q;

  // Load data only exists in WB, one cycle too late for an instruction entering EX right behind
  // the load: hold it in ID for one cycle. Every other dependency is covered by forwarding.
  assign load_use = id_valid && ex_valid_q && ex_dec_q.is_load && ex_dec_q.rd_we &&
                    ((id_dec.use_rs1 && id_dec.rs1 == ex_dec_q.rd) ||
                     (id_dec.use_rs2 && id_dec.rs2 == ex_dec_q.rd));

  logic id_take;
  assign id_take  = id_valid && adv_ex && !load_use && !redirect;
  assign id_ready = id_take;

  // ================================================================ EX
  logic        mem_valid_q;
  pipe_t       mem_q;
  logic        wb_valid_q;
  pipe_t       wb_q;
  logic [31:0] wb_value;       // value WB writes back (load data or result)

  // Forwarding. MEM is newer than WB, so it wins. A load in MEM is never a source here: the
  // load-use stall keeps its consumer out of EX until the load is in WB.
  logic fwd_mem_ok, fwd_wb_ok;
  assign fwd_mem_ok = mem_valid_q && mem_q.rd_we;
  assign fwd_wb_ok  = wb_valid_q && wb_q.rd_we;

  function automatic logic [31:0] forward(input logic [4:0] rs, input logic [31:0] id_val);
    if (fwd_mem_ok && mem_q.rd == rs)     return mem_q.result;
    else if (fwd_wb_ok && wb_q.rd == rs)  return wb_value;
    else                                  return id_val;
  endfunction

  logic [31:0] ex_rs1, ex_rs2;
  assign ex_rs1 = forward(ex_dec_q.rs1, ex_rs1_q);
  assign ex_rs2 = forward(ex_dec_q.rs2, ex_rs2_q);

  logic [31:0] alu_a, alu_b, alu_res;
  always_comb begin
    unique case (ex_dec_q.opa_sel)
      OPA_PC:   alu_a = ex_pc_q;
      OPA_ZERO: alu_a = 32'b0;
      default:  alu_a = ex_rs1;
    endcase
    alu_b = (ex_dec_q.opb_sel == OPB_IMM) ? ex_dec_q.imm : ex_rs2;
  end

  rv_alu u_alu (
    .op_i     (ex_dec_q.alu_op),
    .a_i      (alu_a),
    .b_i      (alu_b),
    .result_o (alu_res)
  );

  logic [31:0] csr_rdata;
  logic        wb_retire;

  rv_csr u_csr (
    .clk_i    (clk_i),
    .rst_ni   (rst_ni),
    .retire_i (wb_retire),
    // Only used when EX fires; then WB retires this cycle (no wb_wait), so both still count.
    .inflight_i ({1'b0, mem_valid_q} + {1'b0, wb_valid_q}),
    .raddr_i  (ex_dec_q.csr_addr),
    .rdata_o  (csr_rdata)
  );

  // Control flow. The ALU computes rs1 + imm for JALR, a separate adder pc + imm for JAL and
  // branches, so the comparison and the target are computed in parallel.
  logic        ex_taken, ex_tgt_misaligned, ex_mem_misaligned, ex_trap, ex_mispredict;
  logic [31:0] ex_pc4, ex_target, ex_npc, ex_result;

  assign ex_pc4    = ex_pc_q + 32'd4;
  assign ex_target = ex_dec_q.is_jalr ? {alu_res[31:1], 1'b0} : ex_pc_q + ex_dec_q.imm;
  assign ex_taken  = ex_dec_q.is_jal || ex_dec_q.is_jalr ||
                     (ex_dec_q.is_branch && branch_taken(ex_dec_q.br_funct3, ex_rs1, ex_rs2));
  assign ex_npc    = ex_taken ? ex_target : ex_pc4;

  // Without the C extension a target must be 4-byte aligned (bit 0 is already 0 here).
  assign ex_tgt_misaligned = ex_taken && ex_target[1];
  assign ex_mem_misaligned = (ex_dec_q.is_load || ex_dec_q.is_store) &&
                             misaligned(ex_dec_q.mem_size, alu_res[1:0]);
  assign ex_trap = ex_dec_q.illegal || ex_dec_q.ecall || ex_dec_q.ebreak ||
                   ex_tgt_misaligned || ex_mem_misaligned;

  // One comparison covers every case: wrong direction, wrong target, and a non-control-flow
  // instruction that the predictor sent somewhere else.
  assign ex_mispredict = ex_pred_npc_q != ex_npc;

  always_comb begin
    unique case (ex_dec_q.res_sel)
      RES_PC4: ex_result = ex_pc4;
      RES_CSR: ex_result = csr_rdata;
      default: ex_result = alu_res;
    endcase
  end

  // Redirect and predictor update happen once, in the cycle the instruction leaves EX: if EX is
  // frozen by a memory stall, its operands may not be final yet (they can come from a load in
  // WB still waiting for data).
  logic ex_fire;
  assign ex_fire     = ex_valid_q && adv_ex;
  assign redirect    = ex_fire && (ex_trap || ex_mispredict || ex_dec_q.is_fence_i);
  assign redirect_pc = ex_npc;   // FENCE.I is not control flow, so this is pc + 4 for it

  // Update: once per control-flow instruction, in order (EX is in order and fires once per
  // instruction). A non-control-flow instruction that was predicted taken also sends one, with
  // kind CF_NONE, so the predictor can repair its speculative state and forget that PC.
  // A trapping instruction sends a flush instead: the program stops there.
  assign upd_valid      = ex_fire && !ex_trap && (ex_dec_q.cf_kind != CF_NONE || ex_mispredict);
  assign upd_pc         = ex_pc_q;
  assign upd_kind       = ex_dec_q.cf_kind;
  assign upd_taken      = ex_taken;
  assign upd_target     = (ex_dec_q.cf_kind == CF_NONE) ? ex_pc4 : ex_target;
  assign upd_mispredict = ex_mispredict;
  assign upd_meta       = ex_meta_q;
  // At most one repair per cycle: FENCE.I that was also mispredicted is repaired by the update.
  assign bp_flush = ex_fire && (ex_trap || (ex_dec_q.is_fence_i && !ex_mispredict));

  assign bp_upd_valid_o      = upd_valid;
  assign bp_upd_pc_o         = upd_pc;
  assign bp_upd_kind_o       = upd_kind;
  assign bp_upd_taken_o      = upd_taken;
  assign bp_upd_target_o     = upd_target;
  assign bp_upd_mispredict_o = upd_mispredict;

  // EX/MEM payload
  pipe_t ex_out;
  always_comb begin
    ex_out              = '0;
    ex_out.pc           = ex_pc_q;
    ex_out.insn         = ex_insn_q;
    ex_out.npc          = ex_npc;
    ex_out.rs1          = ex_dec_q.use_rs1 ? ex_dec_q.rs1 : 5'd0;
    ex_out.rs2          = ex_dec_q.use_rs2 ? ex_dec_q.rs2 : 5'd0;
    ex_out.rs1_val      = ex_dec_q.use_rs1 ? ex_rs1 : 32'b0;
    ex_out.rs2_val      = ex_dec_q.use_rs2 ? ex_rs2 : 32'b0;
    ex_out.rd           = ex_dec_q.rd;
    ex_out.rd_we        = ex_dec_q.rd_we && !ex_trap;
    ex_out.result       = ex_result;
    ex_out.is_load      = ex_dec_q.is_load && !ex_trap;
    ex_out.is_store     = ex_dec_q.is_store && !ex_trap;
    ex_out.mem_size     = ex_dec_q.mem_size;
    ex_out.mem_unsigned = ex_dec_q.mem_unsigned;
    ex_out.addr         = alu_res;
    ex_out.be           = byte_enable(ex_dec_q.mem_size, alu_res[1:0]);
    ex_out.wdata        = store_lanes(ex_dec_q.mem_size, ex_rs2);
    ex_out.trap         = ex_trap;
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      ex_valid_q    <= 1'b0;
      ex_pc_q       <= 32'b0;
      ex_insn_q     <= 32'b0;
      ex_pred_npc_q <= 32'b0;
      ex_dec_q      <= '0;
      ex_rs1_q      <= 32'b0;
      ex_rs2_q      <= 32'b0;
      ex_meta_q     <= '0;
    end else if (adv_ex) begin
      ex_valid_q <= id_take;
      if (id_take) begin
        ex_pc_q       <= id_pc;
        ex_insn_q     <= id_insn;
        ex_pred_npc_q <= id_pred_npc;
        ex_dec_q      <= id_dec;
        ex_rs1_q      <= id_rs1_val;
        ex_rs2_q      <= id_rs2_val;
        ex_meta_q     <= id_meta;
      end
    end else begin
      // EX is frozen, but WB may retire meanwhile and take a forwarding source with it.
      // Capturing the forwarded values every frozen cycle keeps them.
      ex_rs1_q <= ex_rs1;
      ex_rs2_q <= ex_rs2;
    end
  end

  // ================================================================ MEM
  logic mem_memop, wb_memop;
  assign mem_memop = mem_valid_q && (mem_q.is_load || mem_q.is_store);
  assign wb_memop  = wb_valid_q && (wb_q.is_load || wb_q.is_store);

  // At most one data access in flight: MEM may request once WB no longer waits for its own
  // response (it may arrive in this very cycle). Then the response always belongs to WB.
  assign dmem_req_o   = mem_memop && (!wb_memop || dmem_rvalid_i);
  assign dmem_addr_o  = {mem_q.addr[31:2], 2'b00};
  assign dmem_we_o    = mem_q.is_store;
  assign dmem_be_o    = mem_q.be;
  assign dmem_wdata_o = mem_q.wdata;

  assign wb_wait = wb_memop && !dmem_rvalid_i;
  assign adv_mem = !wb_wait && (!mem_memop || dmem_gnt_i);
  assign adv_ex  = adv_mem;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      mem_valid_q <= 1'b0;
      mem_q       <= '0;
    end else if (adv_mem) begin
      mem_valid_q <= ex_valid_q;
      if (ex_valid_q) mem_q <= ex_out;
    end
  end

  // ================================================================ WB
  assign wb_value  = wb_q.is_load ?
                     load_extend(wb_q.mem_size, wb_q.mem_unsigned, wb_q.addr[1:0], dmem_rdata_i) :
                     wb_q.result;
  assign wb_retire = wb_valid_q && !wb_wait;

  assign rf_we    = wb_retire && wb_q.rd_we;
  assign rf_waddr = wb_q.rd;
  assign rf_wdata = wb_value;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      wb_valid_q <= 1'b0;
      wb_q       <= '0;
    end else if (!wb_wait) begin
      // If MEM is waiting for gnt, WB still retires and takes a bubble.
      wb_valid_q <= adv_mem && mem_valid_q;
      if (adv_mem && mem_valid_q) wb_q <= mem_q;
    end
  end

  // ================================================================ halt
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      halting_q <= 1'b0;
      halt_q    <= 1'b0;
    end else begin
      if (ex_fire && ex_trap)       halting_q <= 1'b1;
      if (wb_retire && wb_q.trap)   halt_q    <= 1'b1;
    end
  end
  assign halt_o = halt_q;

  // ================================================================ RVFI
  logic [63:0] order_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni)        order_q <= 64'd0;
    else if (wb_retire) order_q <= order_q + 64'd1;
  end

  assign rvfi_valid     = wb_retire;
  assign rvfi_order     = order_q;
  assign rvfi_insn      = wb_q.insn;
  assign rvfi_trap      = wb_q.trap;
  assign rvfi_halt      = wb_q.trap;
  assign rvfi_intr      = 1'b0;
  assign rvfi_mode      = 2'd3;
  assign rvfi_ixl       = 2'd1;
  assign rvfi_rs1_addr  = wb_q.rs1;
  assign rvfi_rs2_addr  = wb_q.rs2;
  assign rvfi_rs1_rdata = wb_q.rs1_val;
  assign rvfi_rs2_rdata = wb_q.rs2_val;
  assign rvfi_rd_addr   = wb_q.rd_we ? wb_q.rd : 5'd0;
  assign rvfi_rd_wdata  = wb_q.rd_we ? wb_value : 32'b0;
  assign rvfi_pc_rdata  = wb_q.pc;
  assign rvfi_pc_wdata  = wb_q.npc;
  assign rvfi_mem_addr  = wb_memop ? {wb_q.addr[31:2], 2'b00} : 32'b0;
  assign rvfi_mem_rmask = wb_q.is_load  ? wb_q.be : 4'b0;
  assign rvfi_mem_wmask = wb_q.is_store ? wb_q.be : 4'b0;
  assign rvfi_mem_rdata = wb_q.is_load  ? dmem_rdata_i : 32'b0;
  // The port replicates store data on every lane; RVFI shows only the lanes written.
  assign rvfi_mem_wdata = wb_q.is_store ?
                          wb_q.wdata & {{8{wb_q.be[3]}}, {8{wb_q.be[2]}}, {8{wb_q.be[1]}},
                                        {8{wb_q.be[0]}}} : 32'b0;

`ifndef SYNTHESIS
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (rst_ni) begin
      if (dmem_rvalid_i && !wb_memop)
        $error("rv_core: data response with no access in WB");
      if (ex_valid_q && mem_valid_q && mem_q.is_load && mem_q.rd_we &&
          ((ex_dec_q.use_rs1 && ex_dec_q.rs1 == mem_q.rd) ||
           (ex_dec_q.use_rs2 && ex_dec_q.rs2 == mem_q.rd)))
        $error("rv_core: load in MEM feeds EX, the load-use stall failed");
    end
  end
`endif

endmodule
