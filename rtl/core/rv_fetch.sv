// rv_fetch: instruction fetch (IF stage) with branch prediction and a small fetch queue.
//
// Role
//   Keeps the fetch PC, asks the predictor about it every cycle, sends requests on the
//   instruction port and hands instructions to ID in program order together with the PC fetch
//   predicted to come next (pred_npc) and the predictor meta.
//
// Timing (memory with the default 1-cycle latency)
//   cycle t   : request for pc_q (req && gnt); the predictor looks up pc_q in the same cycle and
//               pc_q becomes the predicted next PC at the edge.
//   cycle t+1 : the response arrives (rvalid) and is decoded by ID in the same cycle: the read
//               data bypasses the queue when the queue is empty, so the queue costs no latency.
//   A redirect from EX in cycle t makes the request for the new PC go out in cycle t+1 and its
//   instruction reach ID in t+2: two bubbles for a mispredicted branch, as in the textbook
//   5-stage pipeline.
//
// Queue
//   A circular buffer of DEPTH entries. An entry is allocated when a request is granted (PC,
//   pred_npc, meta), filled when its response arrives, and freed when ID takes it. Pointers:
//   head (next to ID) <= fill (next to receive data) <= tail (next to allocate). Requests are
//   only sent while the queue has a free entry, so a response always has a place to go and ID
//   stalls never need to stall the memory.
//
// Redirects and responses already in flight
//   Responses come back in order and cannot be cancelled, so a redirect turns every request
//   still outstanding into a response to drop: drop_q counts them, and the next drop_q
//   responses are thrown away before any new one is accepted.
//   The protocol also requires a request that is not yet granted to stay unchanged until it is
//   (same rule as OBI), so a redirect cannot simply change the address of a waiting request. That
//   request becomes "stale": it is kept on the port with its old address, and once granted its
//   response is dropped like the others. No new request is sent until the stale one is granted.
//
// The request depends only on flops (stale_q, queue occupancy, outstanding count, halt), never
// combinationally on EX or on gnt: the redirect has a full cycle to reach pc_q.
module rv_fetch #(
  parameter logic [31:0] BOOT_ADDR = 32'h8000_0000,
  parameter int unsigned META_W    = 1,
  parameter int unsigned DEPTH     = 4   // power of two, >= 2 for full throughput
) (
  input  logic              clk_i,
  input  logic              rst_ni,

  // Redirect from EX (misprediction, FENCE.I, trap): kill everything fetched so far.
  input  logic              redirect_i,
  input  logic [31:0]       redirect_pc_i,
  // A trap is on its way to WB: stop sending new requests.
  input  logic              halt_i,

  // Instruction memory port (SPEC section 2)
  output logic              imem_req_o,
  output logic [31:0]       imem_addr_o,
  input  logic              imem_gnt_i,
  input  logic              imem_rvalid_i,
  input  logic [31:0]       imem_rdata_i,

  // Branch predictor, predict side (SPEC section 5)
  output logic              pred_valid_o,
  output logic [31:0]       pred_pc_o,
  input  logic              pred_taken_i,
  input  logic [31:0]       pred_target_i,
  input  logic [META_W-1:0] pred_meta_i,
  output logic              pred_fire_o,

  // To ID
  output logic              id_valid_o,
  output logic [31:0]       id_pc_o,
  output logic [31:0]       id_insn_o,
  output logic [31:0]       id_pred_npc_o,
  output logic [META_W-1:0] id_meta_o,
  input  logic              id_ready_i
);

  localparam int unsigned PTR_W = $clog2(DEPTH);
  localparam int unsigned CNT_W = $clog2(DEPTH + 1);

  typedef logic [PTR_W:0]   ptr_t;   // one extra bit tells full from empty
  typedef logic [CNT_W-1:0] cnt_t;

  logic [31:0] pc_q;
  logic        stale_q;
  logic [31:0] stale_addr_q;
  cnt_t        outst_q;              // granted requests whose response has not arrived yet
  cnt_t        drop_q;               // how many of the oldest outstanding responses to drop
  ptr_t        head_q, fill_q, tail_q;

  logic [31:0]       buf_pc_q   [DEPTH];
  logic [31:0]       buf_npc_q  [DEPTH];
  logic [META_W-1:0] buf_meta_q [DEPTH];
  logic [31:0]       buf_insn_q [DEPTH];

  // ---------------------------------------------------------------- request
  ptr_t occ;
  logic new_req;
  logic hs, hs_new;

  assign occ     = tail_q - head_q;
  assign new_req = !stale_q && !halt_i && (occ < ptr_t'(DEPTH)) && (outst_q < cnt_t'(DEPTH));

  assign imem_req_o  = stale_q || new_req;
  assign imem_addr_o = stale_q ? stale_addr_q : pc_q;
  assign hs          = imem_req_o && imem_gnt_i;
  assign hs_new      = hs && !stale_q;

  // The predictor sees the PC being requested; its answer is used only on the handshake.
  // pred_fire is withheld in a redirect cycle: that fetch is about to be killed, and the
  // predictor is restoring its speculative state from the redirecting instruction's meta.
  logic [31:0] pred_npc;
  assign pred_valid_o = new_req;
  assign pred_pc_o    = pc_q;
  assign pred_fire_o  = hs_new && !redirect_i;
  // No C extension: a target with low bits set can never be right. Aligning it keeps the port
  // word aligned; EX sees pred_npc != real next PC and repairs it like any misprediction.
  assign pred_npc     = pred_taken_i ? {pred_target_i[31:2], 2'b00} : pc_q + 32'd4;
  logic unused_target_lsbs;
  assign unused_target_lsbs = ^pred_target_i[1:0];

  // ---------------------------------------------------------------- response and ID side
  logic rv_keep, has_data, pop;
  logic [PTR_W-1:0] head_idx, fill_idx, tail_idx;

  assign head_idx = head_q[PTR_W-1:0];
  assign fill_idx = fill_q[PTR_W-1:0];
  assign tail_idx = tail_q[PTR_W-1:0];

  assign rv_keep  = imem_rvalid_i && (drop_q == '0);
  assign has_data = (fill_q != head_q);

  // When the queue holds no data yet, the response being received is for the head entry and
  // goes straight to ID.
  assign id_valid_o    = has_data || rv_keep;
  assign id_insn_o     = has_data ? buf_insn_q[head_idx] : imem_rdata_i;
  assign id_pc_o       = buf_pc_q[head_idx];
  assign id_pred_npc_o = buf_npc_q[head_idx];
  assign id_meta_o     = buf_meta_q[head_idx];
  assign pop           = id_valid_o && id_ready_i;

  // ---------------------------------------------------------------- counters
  cnt_t outst_d, drop_d;

  always_comb begin
    outst_d = outst_q + cnt_t'(hs) - cnt_t'(imem_rvalid_i);
    if (redirect_i)
      drop_d = outst_d;
    else
      drop_d = drop_q - cnt_t'(imem_rvalid_i && drop_q != '0) + cnt_t'(hs && stale_q);
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      pc_q         <= BOOT_ADDR;
      stale_q      <= 1'b0;
      stale_addr_q <= 32'b0;
      outst_q      <= '0;
      drop_q       <= '0;
      head_q       <= '0;
      fill_q       <= '0;
      tail_q       <= '0;
    end else begin
      outst_q <= outst_d;
      drop_q  <= drop_d;

      if (redirect_i && imem_req_o && !imem_gnt_i) begin
        stale_q      <= 1'b1;
        stale_addr_q <= imem_addr_o;
      end else if (stale_q && imem_gnt_i) begin
        stale_q      <= 1'b0;
      end

      if (redirect_i) begin
        pc_q   <= redirect_pc_i;
        head_q <= '0;
        fill_q <= '0;
        tail_q <= '0;
      end else begin
        if (hs_new)  pc_q   <= pred_npc;
        if (hs_new)  tail_q <= tail_q + ptr_t'(1);
        if (rv_keep) fill_q <= fill_q + ptr_t'(1);
        if (pop)     head_q <= head_q + ptr_t'(1);
      end
    end
  end

  // Queue payload: no reset needed, the pointers say which entries are meaningful.
  always_ff @(posedge clk_i) begin
    if (hs_new && !redirect_i) begin
      buf_pc_q[tail_idx]   <= pc_q;
      buf_npc_q[tail_idx]  <= pred_npc;
      buf_meta_q[tail_idx] <= pred_meta_i;
    end
    if (rv_keep && !redirect_i) buf_insn_q[fill_idx] <= imem_rdata_i;
  end

`ifndef SYNTHESIS
  // Protocol self-checks (simulation only).
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (rst_ni) begin
      if (imem_rvalid_i && outst_q == '0)
        $error("rv_fetch: rvalid with no outstanding request");
      if (rv_keep && fill_q == tail_q)
        $error("rv_fetch: response kept but no queue entry is waiting for it");
      if (drop_q > outst_q)
        $error("rv_fetch: more responses to drop than outstanding");
    end
  end
`endif

endmodule
