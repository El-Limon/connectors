// Symbol resolution for the stripped, non-PIE Dragonwilds server binary.
//
// Two independent sources, both read at runtime, nothing hard-coded:
//   1. `<exe>.sym`  — the depot's symbol database: `u32 N`, N x 20-byte records
//      `{u64 rva, u32 line, u32 fileOff, u32 nameOff}` sorted by rva, then a '\n'-separated
//      name table (file paths and demangled function signatures share the table).
//      A function's address is the lowest rva of its contiguous run of records.
//      vaddr = rva + the first PT_LOAD p_vaddr read from the ELF (never hard-coded).
//   2. `.dynsym`    — exported vtables/typeinfo (`_ZTV7UObject`, ...), via dlsym with an
//      ELF-parsing fallback.
//
// Results are cached in <PluginDataDir>/symcache.json keyed on `.note.gnu.build-id`, so a game
// update invalidates the cache automatically.
#pragma once
#include "common.h"

struct ElfInfo {
    bool ok = false;
    std::string error;
    uint64_t loadBase = 0;                 // first PT_LOAD p_vaddr
    uint64_t textAddr = 0, textSize = 0;   // .text
    uint64_t initAddr = 0, finiAddr = 0;   // .init / .fini section addresses (cross-check anchors)
    uint64_t rodataAddr = 0, rodataSize = 0;
    std::string buildId;                   // hex
    // Executable PT_LOAD ranges; used to sanity-check a resolved address (.init/.fini live
    // outside .text, so ".text only" would wrongly reject them).
    std::vector<std::pair<uint64_t, uint64_t>> execRanges;
};
// True when `addr` falls inside an executable segment of the image.
bool IsExecutableAddr(uint64_t addr);
const ElfInfo& Elf();

// Exported symbol (vtables etc). 0 when absent.
uint64_t DynSymAddr(const char* name);

// Every exported `_ZTV*` symbol {name, address, size}. Read once from the on-disk ELF; used to
// find *all* vtables holding a given virtual function, including derived classes that inherit the
// base implementation (e.g. UDomGameEngine inherits UJgxGameEngine::Tick, so hooking the base
// class vtable alone would never fire).
const std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>>& VTableSymbols();

struct SymEntry {
    std::string name;      // the key we asked for
    std::string signature; // the full demangled line we matched
    uint64_t rva = 0;
    uint64_t addr = 0;     // rva + loadBase
    std::string how;       // "symcache" | "sym-scan" | "dynsym" | "" (unresolved)
    bool contiguous = true;
    uint32_t records = 0;
};

namespace Sym {
// Resolves every wanted name. Safe to call once, from the init thread. Never throws.
void Init();
bool Ready();
uint64_t Addr(const char* name);  // 0 when unresolved
const SymEntry* Entry(const char* name);
const std::vector<SymEntry>& All();
// {resolved, wanted, source, symPath, cacheHit, scanMs}
std::string StatsJson();
std::string CacheJson();  // symCache block for /health
// The boot cross-checks (see plan M0). Each returns a human-readable line; `ok` is set to false on failure.
std::vector<std::pair<std::string, bool>> SelfChecks();
size_t ProcessEventSlot();  // index of UObject::ProcessEvent in _ZTV7UObject, SIZE_MAX if unknown
}  // namespace Sym

// ---- readable-memory guard (cached /proc/self/maps) ----
// True when [addr, addr+len) is entirely inside a readable mapping. Used before every dereference
// of a game pointer that we did not produce ourselves.
bool MemReadable(const void* addr, size_t len);
bool MemWritable(const void* addr, size_t len);
void MemMapsRefresh();
std::string MemMapsSelfSoLine();  // the /proc/self/maps line(s) for our own .so (proof helper)
