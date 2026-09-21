// Symbol resolution for the VEIN dedicated server binary (VeinServer-Linux-Test, UE 5.6.1).
//
// Unlike the Dragonwilds depot, we do not know up front which symbol source this binary ships, so
// resolution is a *strategy chain*. Every wanted name is tried against each strategy in turn and the
// first hit wins; the strategy that produced the address is recorded as `how` and reported in
// /health.diagnostics.resolved so that no capability ever claims more than it can prove.
//
//   symtab     ELF .symtab of /proc/self/exe. Names are mangled, so each candidate is demangled
//              with abi::__cxa_demangle and matched against the same demangled signatures the
//              Dragonwilds .sym used. Cheap pre-filter: the Itanium mangling embeds the identifier
//              literally, so a strstr on the base name rejects ~99.9% of symbols before demangling.
//   depotsym   A `.sym` file next to the binary in the Dragonwilds depot format:
//              `u32 N`, N x 20-byte {u64 rva, u32 line, u32 fileOff, u32 nameOff} sorted by rva,
//              then a '\n'-separated table of *demangled* names. Used when the depot ships one.
//   dynsym     ELF .dynsym (exported symbols only: vtables, typeinfo and whatever the game exports).
//              Tried through dlsym first, then by parsing the on-disk image.
//   signature  A byte pattern (with `??` wildcards) over .text, optionally anchored by a string
//              cross-reference. A signature is only accepted when it matches **exactly once**.
//
// vaddr = rva + the first PT_LOAD p_vaddr, read from the ELF at load time and never hard-coded.
// Results are cached in <PluginDataDir>/symcache.json keyed on `.note.gnu.build-id`, so a game
// update invalidates the cache by itself. TAKARO_SYM_PATH overrides the depot `.sym` path.
#pragma once
#include "common.h"

#include <functional>

struct ElfInfo {
    bool ok = false;
    std::string error;
    uint64_t loadBase = 0;                 // first PT_LOAD p_vaddr
    uint64_t textAddr = 0, textSize = 0;   // .text
    uint64_t textOff = 0;                  // file offset of .text
    uint64_t initAddr = 0, finiAddr = 0;   // .init / .fini section addresses (cross-check anchors)
    uint64_t rodataAddr = 0, rodataSize = 0, rodataOff = 0;
    bool hasSymtab = false, hasDynsym = false;
    size_t symtabCount = 0, dynsymCount = 0;
    bool pie = false;                      // ET_DYN: addresses need the runtime slide
    uint64_t slide = 0;                    // runtime load address - loadBase (0 for non-PIE)
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

// Every exported `_ZTV*` symbol {name, {address, size}}. Read once from the on-disk ELF; used to
// find *all* vtables holding a given virtual function, including derived classes that inherit the
// base implementation, so hooking the declaring class alone would never fire.
const std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>>& VTableSymbols();

struct SymEntry {
    std::string name;      // the key we asked for
    std::string signature; // the full demangled line we matched
    uint64_t rva = 0;
    uint64_t addr = 0;     // rva + loadBase (+ slide on a PIE image)
    std::string how;       // "symtab" | "depotsym" | "dynsym" | "signature" | "symcache" | ""
    bool contiguous = true;
    uint32_t records = 0;
    bool required = false; // counts towards the M0 self-check
    bool data = false;     // a data symbol (GEngine); not checked against .text
};

namespace Resolve {
// Resolves every wanted name. Safe to call once, from the init thread. Never throws.
void Init();
bool Ready();
uint64_t Addr(const char* name);  // 0 when unresolved
const SymEntry* Entry(const char* name);
const std::vector<SymEntry>& All();
// {resolved, wanted, required, requiredResolved, processEventSlot, strategies:[...]}
std::string StatsJson();
std::string CacheJson();  // symCache block for /health
// The boot cross-checks (see plan M0). Each returns a human-readable line; `ok` is false on failure.
std::vector<std::pair<std::string, bool>> SelfChecks();
size_t ProcessEventSlot();  // index of UObject::ProcessEvent in the UObject vtable, SIZE_MAX if unknown
void SetProcessEventSlot(size_t slot, const char* how);  // set from a live vtable measurement
// Sets the L1-owned `symbols.*` capabilities from what actually resolved. Called from Init().
void ApplyCapabilities();
}  // namespace Resolve

// ---- testable core (no process state; exercised by tests/unit_test.cpp) -------------------------
namespace ResolveCore {

// A byte signature: 0..255 is a literal byte, -1 is a wildcard (`?` or `??`).
struct Signature {
    std::vector<int> bytes;
    bool empty() const { return bytes.empty(); }
};
// "48 8B ?? E8 ?? ?? ?? ??" -> Signature. Returns false with `err` set on malformed input.
bool ParseSignature(const std::string& pattern, Signature& out, std::string& err);
// Counts matches of `sig` in [hay, hay+len). Stops counting at `maxHits` (2 is enough for the
// exactly-once rule). `firstOff` receives the offset of the first match.
size_t ScanSignature(const uint8_t* hay, size_t len, const Signature& sig, size_t& firstOff,
                     size_t maxHits = 2);

// abi::__cxa_demangle wrapper; returns "" when `mangled` is not an Itanium C++ name.
std::string Demangle(const char* mangled);
// "UObject::ProcessEvent" -> "ProcessEvent"; "RequestEngineExit" -> "RequestEngineExit".
std::string BaseName(const std::string& key);
// True when the demangled line `line` is the wanted symbol: either exactly `sig` (when non-empty)
// or the text before '(' equals `key`.
bool NameMatches(const std::string& line, const char* key, const char* sig);

struct SymbolRec {
    const char* name = nullptr;
    uint64_t value = 0;
    uint64_t size = 0;
    unsigned char info = 0;
};
// Parses an ELF64 image held in memory. Fills every field of ElfInfo it can.
bool ParseElfBuffer(const uint8_t* p, size_t len, ElfInfo& out);
// Walks .symtab (dynamic=false) or .dynsym (dynamic=true) of an in-memory ELF64 image and calls
// `fn` for every entry. Returns the number of entries visited, or 0 when the table is absent.
size_t ForEachSymbol(const uint8_t* p, size_t len, bool dynamic, const std::function<void(const SymbolRec&)>& fn);

}  // namespace ResolveCore

// ---- readable-memory guard (cached /proc/self/maps) --------------------------------------------
// True when [addr, addr+len) is entirely inside a readable mapping. Used before every dereference
// of a game pointer that we did not produce ourselves.
bool MemReadable(const void* addr, size_t len);
bool MemWritable(const void* addr, size_t len);
void MemMapsRefresh();
std::string MemMapsSelfSoLine();  // the /proc/self/maps line(s) for our own .so (proof helper)
