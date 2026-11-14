/* Linker script for bare-metal programs on spectre-riscv (docs/SPEC.md, section 2).
 * One unified 1 MiB RAM at 0x8000_0000: code, data and stack. The stack grows down
 * from the top of RAM. Also used for riscv-tests (they only need .text.init first). */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY
{
  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 1M
}

SECTIONS
{
  .text : {
    *(.text.init)
    *(.text .text.*)
  } > RAM

  .rodata : ALIGN(16) {
    *(.rodata .rodata.* .srodata .srodata.*)
  } > RAM

  .data : ALIGN(16) {
    *(.data .data.*)
    __global_pointer$ = . + 0x800;
    *(.sdata .sdata.*)
  } > RAM

  .bss (NOLOAD) : ALIGN(16) {
    __bss_start = .;
    *(.sbss .sbss.* .bss .bss.* COMMON)
    . = ALIGN(4);
    __bss_end = .;
  } > RAM

  _end = .;
  __stack_top = ORIGIN(RAM) + LENGTH(RAM);
  /* Fail the link if the program leaves less than 64 KiB for the stack. */
  ASSERT(_end <= __stack_top - 0x10000, "program too large: less than 64 KiB left for the stack")

  /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.note .note.*) *(.eh_frame*) }
}
