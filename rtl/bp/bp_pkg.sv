// bp_pkg: types and sizing functions shared by the branch prediction unit (bpu) and the core.
//
// The core imports this package to size the pipeline registers that carry pred_meta_o:
//   localparam int unsigned BP_META_W = bp_pkg::meta_w(GHR_BITS, RAS_DEPTH);
// META_W does not depend on BP_TYPE on purpose: a predictor that has no global history drives
// the history field of the meta with constant zeros, so synthesis removes those pipeline flops
// and the core never has to know which predictor it is paired with.
package bp_pkg;

  // Control-flow kind on the update port (docs/SPEC.md section 5). CF_NONE on an update means
  // "this PC is not a control-flow instruction", which the core needs when the predictor sent
  // fetch somewhere for a PC that turned out to be an ordinary instruction.
  typedef enum logic [2:0] {
    CF_NONE   = 3'd0,
    CF_BRANCH = 3'd1,
    CF_JAL    = 3'd2,
    CF_JALR   = 3'd3,
    CF_CALL   = 3'd4,
    CF_RET    = 3'd5
  } cf_kind_e;

  // What the BTB remembers about a PC. Fetch only needs to know which source gives the next PC,
  // so JAL and JALR collapse into one kind and the field costs 2 bits instead of 3.
  typedef enum logic [1:0] {
    BTB_BRANCH = 2'd0,  // conditional: the direction predictor decides
    BTB_JUMP   = 2'd1,  // always taken to the stored target
    BTB_CALL   = 2'd2,  // always taken to the stored target, pushes pc+4 on the RAS
    BTB_RET    = 2'd3   // always taken, target from the RAS top
  } btb_kind_e;

  // Instruction addresses are word aligned (RV32I, no C extension), so every stored PC drops the
  // two zero bits.
  localparam int unsigned PC_W = 30;

  function automatic int unsigned ras_ptr_w(input int unsigned ras_depth);
    return (ras_depth > 1) ? $clog2(ras_depth) : 0;
  endfunction

  // Meta layout, LSB first: {ras_top[PC_W], ras_ptr[ras_ptr_w], ghr[ghr_bits]}.
  // At least 1 bit so the port is never zero width.
  function automatic int unsigned meta_w(input int unsigned ghr_bits, input int unsigned ras_depth);
    int unsigned w;
    w = ghr_bits + ((ras_depth > 0) ? ras_ptr_w(ras_depth) + PC_W : 0);
    return (w > 0) ? w : 1;
  endfunction

  // 2-bit saturating counter: 0 strongly not taken, 1 weakly not taken, 2 weakly taken,
  // 3 strongly taken. Two wrong outcomes in a row are needed to flip a strong prediction,
  // which is what makes a loop branch cost one misprediction per loop exit instead of two.
  function automatic logic [1:0] ctr_next(input logic [1:0] ctr, input logic up);
    if (up) return (ctr == 2'd3) ? 2'd3 : ctr + 2'd1;
    else    return (ctr == 2'd0) ? 2'd0 : ctr - 2'd1;
  endfunction

endpackage
