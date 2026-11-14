// bp_ras: return address stack, circular, DEPTH entries (power of two, >= 2).
//
// ptr_q points at the top of stack (TOS). A call pushes pc+4 (ptr+1, then write), a return
// predicts TOS and pops (ptr-1). On overflow the pointer wraps and overwrites the oldest entry;
// on underflow it wraps and returns a stale entry. Both only cost a misprediction, never
// correctness, so there is no counter of valid entries.
//
// Push and pop happen speculatively at prediction time (fire_*). Each prediction checkpoints
// {TOS value, ptr} in its meta. Repair (restore_i) puts back ptr and the TOS entry, then
// replays the effect of the resolving instruction itself (restore_push_i / restore_pop_i).
// Restoring the TOS entry fixes the common wrong-path sequence "return (pop) then call (push)",
// which overwrites the slot that was TOS at the checkpoint. Deeper wrong-path damage (two pops
// then pushes) is not repaired; the spec accepts that (SPEC.md section 5).
//
// Restore has priority over fire: in a repair cycle the fetched instruction is wrong path.
// The repair writes two different entries in the same cycle (TOS and TOS+1), so the stack is
// plain flops with two write ports; at 4-8 entries an SRAM would not pay off anyway.
module bp_ras #(
  parameter int unsigned  DEPTH = 4,
  localparam int unsigned PW    = $clog2(DEPTH),
  localparam int unsigned AW    = bp_pkg::PC_W
) (
  input  logic          clk_i,
  input  logic          rst_ni,

  output logic [AW-1:0] top_o,
  output logic [PW-1:0] ptr_o,

  input  logic          fire_push_i,
  input  logic          fire_pop_i,
  input  logic [AW-1:0] fire_push_addr_i,

  input  logic          restore_i,
  input  logic [PW-1:0] restore_ptr_i,
  input  logic [AW-1:0] restore_top_i,
  input  logic          restore_push_i,
  input  logic          restore_pop_i,
  input  logic [AW-1:0] restore_push_addr_i
);

  logic [AW-1:0] stack_q [DEPTH];
  logic [PW-1:0] ptr_q, ptr_d;

  // Two write ports: A restores the checkpointed TOS, B is the push (speculative or replayed).
  logic          wa_en, wb_en;
  logic [PW-1:0] wa_idx, wb_idx;
  logic [AW-1:0] wb_data;

  always_comb begin
    ptr_d   = ptr_q;
    wa_en   = 1'b0;
    wa_idx  = restore_ptr_i;
    wb_en   = 1'b0;
    wb_idx  = ptr_q + PW'(1);
    wb_data = fire_push_addr_i;
    if (restore_i) begin
      wa_en = 1'b1;
      if (restore_push_i) begin
        ptr_d   = restore_ptr_i + PW'(1);
        wb_en   = 1'b1;
        wb_idx  = restore_ptr_i + PW'(1);
        wb_data = restore_push_addr_i;
      end else if (restore_pop_i) begin
        ptr_d = restore_ptr_i - PW'(1);
      end else begin
        ptr_d = restore_ptr_i;
      end
    end else if (fire_push_i) begin
      ptr_d = ptr_q + PW'(1);
      wb_en = 1'b1;
    end else if (fire_pop_i) begin
      ptr_d = ptr_q - PW'(1);
    end
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      ptr_q <= '0;
      for (int i = 0; i < DEPTH; i++) stack_q[i] <= '0;
    end else begin
      ptr_q <= ptr_d;
      // wa_idx != wb_idx whenever both are enabled (TOS and TOS+1, DEPTH >= 2).
      if (wa_en) stack_q[wa_idx] <= restore_top_i;
      if (wb_en) stack_q[wb_idx] <= wb_data;
    end
  end

  assign top_o = stack_q[ptr_q];
  assign ptr_o = ptr_q;

endmodule
