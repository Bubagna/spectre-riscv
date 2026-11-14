/* Linker script for the core's hand-written tests: code then data, all in RAM at 0x8000_0000. */
OUTPUT_ARCH("riscv")
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 1M }
SECTIONS
{
  .text : { *(.text.init) *(.text .text.*) } > RAM
  .data : ALIGN(64) { *(.rodata .rodata.*) *(.data .data.*) *(.bss .bss.*) } > RAM
  _stack_top = ORIGIN(RAM) + LENGTH(RAM);
  /DISCARD/ : { *(.riscv.attributes) *(.note .note.*) }
}
