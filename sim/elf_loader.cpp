// ELF32 little-endian RISC-V loader. Only what bare-metal programs need: PT_LOAD segments.
#include "elf_loader.h"

#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

namespace {

uint16_t rd16(const std::vector<uint8_t> &d, size_t off) {
    return static_cast<uint16_t>(d[off] | (d[off + 1] << 8));
}

uint32_t rd32(const std::vector<uint8_t> &d, size_t off) {
    return static_cast<uint32_t>(d[off]) | (static_cast<uint32_t>(d[off + 1]) << 8) |
           (static_cast<uint32_t>(d[off + 2]) << 16) | (static_cast<uint32_t>(d[off + 3]) << 24);
}

constexpr uint16_t EM_RISCV = 243;
constexpr uint32_t PT_LOAD = 1;

}  // namespace

bool load_elf(const std::string &path, uint32_t ram_base, uint32_t ram_size,
              const std::function<void(uint32_t, uint8_t)> &write_byte, ElfInfo &info,
              std::string &err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        err = "cannot open " + path;
        return false;
    }
    std::vector<uint8_t> d((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (d.size() < 52 || std::memcmp(d.data(), "\x7f" "ELF", 4) != 0) {
        err = path + ": not an ELF file";
        return false;
    }
    if (d[4] != 1 || d[5] != 1) {
        err = path + ": not a 32-bit little-endian ELF";
        return false;
    }
    if (rd16(d, 18) != EM_RISCV) {
        err = path + ": not a RISC-V ELF";
        return false;
    }
    info.entry = rd32(d, 24);
    const uint32_t phoff = rd32(d, 28);
    const uint16_t phentsize = rd16(d, 42);
    const uint16_t phnum = rd16(d, 44);

    for (unsigned i = 0; i < phnum; i++) {
        const size_t ph = static_cast<size_t>(phoff) + static_cast<size_t>(i) * phentsize;
        if (ph + 32 > d.size()) {
            err = path + ": truncated program header table";
            return false;
        }
        const uint32_t type = rd32(d, ph + 0);
        const uint32_t offset = rd32(d, ph + 4);
        const uint32_t paddr = rd32(d, ph + 12);
        const uint32_t filesz = rd32(d, ph + 16);
        const uint32_t memsz = rd32(d, ph + 20);
        if (type != PT_LOAD || memsz == 0) continue;
        const uint64_t start = static_cast<uint64_t>(paddr) - ram_base;
        if (paddr < ram_base || start + memsz > ram_size) {
            char buf[128];
            std::snprintf(buf, sizeof buf, ": segment at 0x%08x (+0x%x) is outside RAM", paddr,
                          memsz);
            err = path + buf;
            return false;
        }
        if (static_cast<uint64_t>(offset) + filesz > d.size() || filesz > memsz) {
            err = path + ": segment data outside the file";
            return false;
        }
        for (uint32_t b = 0; b < memsz; b++)
            write_byte(paddr + b, b < filesz ? d[offset + b] : 0);
        info.bytes_loaded += memsz;
    }
    return true;
}
