// ELF32 loader for the Verilator harness: copies PT_LOAD segments into simulated RAM.
#pragma once

#include <cstdint>
#include <functional>
#include <string>

struct ElfInfo {
    uint32_t entry = 0;
    uint32_t bytes_loaded = 0;
};

// Calls write_byte(addr, value) for every byte of every PT_LOAD segment (file bytes, then
// zeros up to p_memsz), at the segment's physical address, like the ISS does.
// Checks that every segment lies in [ram_base, ram_base + ram_size).
// Returns false and fills err on any problem.
bool load_elf(const std::string &path, uint32_t ram_base, uint32_t ram_size,
              const std::function<void(uint32_t, uint8_t)> &write_byte, ElfInfo &info,
              std::string &err);
