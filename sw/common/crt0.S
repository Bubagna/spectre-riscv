# Start-up code for C programs (docs/SPEC.md, sections 2 and 4).
# Sets sp and gp, clears .bss, calls main() and stores its return value to HALT
# (0 = success). There is no trap handler: anything unexpected traps and ends
# the simulation.

  .section .text.init
  .globl _start
_start:
  # Registers are zeroed so the RTL and the ISS start from the same state
  # whatever the register file resets to.
  .irp r, 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
  li x\r, 0
  .endr
  la sp, __stack_top
  # gp is set without relaxation, otherwise the assembler would compute it from gp itself.
  .option push
  .option norelax
  la gp, __global_pointer$
  .option pop

  la t0, __bss_start
  la t1, __bss_end
1:
  bgeu t0, t1, 2f
  sw zero, 0(t0)
  addi t0, t0, 4
  j 1b
2:
  call main

  .globl _exit
_exit:
  li t0, 0x10000008
  sw a0, 0(t0)
  j .
