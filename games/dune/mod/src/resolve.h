// Symbol resolution for the Dune: Awakening dedicated server binary
// (DuneSandboxServer-Linux-Shipping, **UE 5.2.1** — proven offline, see below).
//
// The version is not embedded as a plain version string: only the *format* string
// "Unreal Engine version: %s" is in `.rodata` (narrow, @0x5e9b9b4). It is referenced exactly once,
// and 0x24 bytes earlier the same function loads the wide literal "5.2.1" as the `%s` argument.
// Corroborated by the wide literal "Unreal Engine 5.2.1" and by a cvar help text that names
// "UE 5.2.1"; and structurally by this build using UE 5.2's structured-logging mechanism.
//
// ---------------------------------------------------------------------------------------------
// WHY THIS FILE LOOKS NOTHING LIKE THE VEIN ONE IT WAS PORTED FROM
//
// VEIN and Dragonwilds shipped function symbols (.symtab / a depot `.sym`), so the resolver was
// "look up a demangled signature, get an address". Dune ships neither:
//
//   * `DuneSandboxServer-Linux-Shipping` is **stripped**: no `.symtab`, no `.debug_*`, and the
//     `.gnu_debuglink` points at a companion file that is not in the image.
//   * `.dynsym` is nevertheless huge (~171 856 entries / 14.9 MB `.dynstr`) because the C++ **RTTI**
//     is exported: **42 942 `_ZTV*` vtables** plus their `_ZTI`/`_ZTS`. The 22 844 STT_FUNC entries
//     are statically linked third-party C libraries (OpenSSL & co), not engine or game code.
//   * Every engine function name greps to **zero**: `UObject::ProcessEvent`, `FName::ToString`,
//     `StaticFindObject`, `GEngine`, `GWorld`, `GUObjectArray` — none of them exist as symbols.
//
// So the unit of resolution here is not a function name but a **vtable slot**, and the strategy
// chain is:
//
//   symcache       <PluginDataDir>/symcache.json keyed on `.note.gnu.build-id`. A game update
//                  changes the build id and therefore invalidates the cache by itself.
//   dynsym-vtable  dlsym / `.dynsym` lookup of a mangled `_ZTV…` name. This is the *only* strategy
//                  that is cheap, exact and guaranteed on this build, and it is what makes the
//                  binary tractable at all: an object's first word is its vtable pointer, so
//                  "which class is this object?" becomes an exact address comparison against a
//                  named `_ZTV` symbol — strictly better than what a symtab would have given us.
//   rtti-slot      a function address read out of a vtable at a slot index. The slot index is
//                  either a constant we derived offline (research/2026-09-21-binary-dissection.md)
//                  or, for ProcessEvent, **derived at boot** by differential vtable analysis plus a
//                  code fingerprint, requiring exactly one surviving candidate.
//   string-xref    a byte pattern over `.text`, anchored by a **UTF-16LE** string literal that the
//                  enclosing function must reference. Accepted only when it matches **exactly once**.
//                  This is how the globals with no symbol and no vtable (GEngine, GUObjectArray,
//                  the FName pool) are found; `ripOperandOffset` decodes the RIP-relative
//                  displacement inside the match into the global's address.
//
// ---------------------------------------------------------------------------------------------
// THE RULE THAT MATTERS MOST: NEVER READ VTABLE SLOTS FROM THE FILE IMAGE
//
// This binary is a **PIE** (ET_DYN). Every vtable slot in the on-disk image is therefore **0x0000**;
// the real target lives in an `R_X86_64_RELATIVE` addend in `.rela.dyn` (68 MB of them) and is only
// written into memory by the dynamic loader. Offline analysis must read the RELA addends — the
// plugin, which runs in-process, must read the **live, relocated, slid** vtable and nothing else.
// A "vtable read" that returned 0 for every slot was the first trap this binary set; the guard is
// `VTableSlotsLive()`, which refuses to operate on anything that is not a readable mapping and
// rejects an all-zero table with an explicit error instead of quietly resolving nothing.
//
// The same rule is why hooks go on **live objects' own vtable pointers** (`Hooks::HookObjectVTable`)
// or on a live class vtable in memory — never on a base vtable read out of the file image.
//
// vaddr = rva + the first PT_LOAD p_vaddr + the runtime slide; nothing is ever hard-coded.
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
    // Read-only PT_LOAD ranges that carry string literals. `.rodata` alone is not enough on this
    // build: the UTF-16LE literals are spread over several read-only sections.
    std::vector<std::pair<uint64_t, uint64_t>> rodataRanges;
};
// True when `addr` falls inside an executable segment of the image. ⚠️ This is a WEAK test on this
// binary: it has a single R+X PT_LOAD covering vaddr 0 .. 0x153690d0, which also contains .dynsym,
// .dynstr, .rela.dyn, .rodata and .eh_frame. Prefer IsTextAddr / IsFunctionEntry.
bool IsExecutableAddr(uint64_t addr);
// True when `addr` is inside `.text` proper (0x9aff000 .. 0x153672ae on this build, plus the slide).
bool IsTextAddr(uint64_t addr);
// True when `addr` is the entry point of a function according to the `.eh_frame_hdr` binary-search
// table. On a stripped binary this is the closest thing to a symbol table that survives — nothing
// can strip it, because the unwinder needs it — and it gives 918 821 known function entries on this
// build. It is the strongest validator available for "did this slot really point at a function?".
// Returns false when the table could not be parsed; ask EhFrameAvailable() to tell the two apart.
bool IsFunctionEntry(uint64_t addr);
bool EhFrameAvailable();
std::string EhFrameDetail();
const ElfInfo& Elf();

// Exported symbol (vtables etc). 0 when absent. Returns a **slid, runtime** address.
uint64_t DynSymAddr(const char* name);

// Every exported `_ZTV*` symbol {name, {runtime address, size}}, sorted by address. Read once from
// the on-disk ELF. On this build there are ~42 942 of them and they are the whole symbol story:
//   * they name every class we may want to hook or identify,
//   * `size` gives the vtable's slot count — `(size - 16) / 8` — which is how we know how far a
//     class's virtual table runs and therefore how far a slot comparison is meaningful.
const std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>>& VTableSymbols();

// ---- vtable helpers (LIVE MEMORY ONLY; see the header comment) ---------------------------------
struct VTableInfo {
    bool ok = false;
    std::string error;
    std::string ztv;        // mangled symbol, e.g. "_ZTV7UObject"
    std::string className;  // demangled, e.g. "UObject"
    uint64_t addr = 0;      // runtime address of the _ZTV symbol (the two header words)
    uint64_t size = 0;      // symbol size in bytes
    size_t slots = 0;       // (size - 16) / 8
    uint64_t typeInfo = 0;  // the `typeinfo` pointer in header word 1
    int64_t offsetToTop = 0;// header word 0
};
// Looks the vtable up in `.dynsym` and validates the two header words against live memory.
VTableInfo VTable(const char* ztvName);
// Reads `count` slots (slot 0 == the first virtual function, i.e. the word at addr+16) out of LIVE
// memory. Returns false with `err` set when the table is unreadable **or entirely zero** (which
// means the caller is looking at the file image, not the relocated one).
bool VTableSlotsLive(const VTableInfo& vt, size_t count, std::vector<uint64_t>& out, std::string& err);
// One slot; 0 on any failure.
uint64_t VTableSlot(const char* ztvName, size_t slot);

// The class whose exported vtable an object's first word points at, demangled ("ADuneCharacter"),
// or "" when the pointer matches no exported vtable. This is the cheap, exact object-identification
// primitive that the RTTI-rich `.dynsym` buys us; it needs no FName read and no reflection, so it is
// also the *validator* that runs before any FName read on an untrusted pointer.
std::string ClassNameByVTablePtr(const void* obj);
// True when `obj` looks like a UObject: readable, its first word is a known exported vtable, and
// that vtable's class is `UObject` or derives from it via the recorded RTTI chain.
bool LooksLikeUObject(const void* obj);

struct SymEntry {
    std::string name;      // the key we asked for
    std::string signature; // the vtable+slot, byte pattern, or demangled line we matched
    uint64_t rva = 0;
    uint64_t addr = 0;     // rva + loadBase (+ slide on a PIE image)
    std::string how;       // "dynsym-vtable" | "rtti-slot" | "string-xref" | "dynsym" | "env" | "symcache:*"
    bool contiguous = true;
    uint32_t records = 0;
    bool required = false; // counts towards the M0 self-check
    bool data = false;     // a data symbol (GEngine); not checked against .text
    size_t slot = SIZE_MAX;// for rtti-slot: the vtable index it came from
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

// ---- ProcessEvent -----------------------------------------------------------------------------
// There is no `UObject::ProcessEvent` symbol on this build, so the slot is *derived*. See
// DeriveProcessEventSlot() in resolve.cpp for the three tests and the exactly-one rule; the
// shortlist and the reason are reported verbatim in /health.diagnostics.resolve.processEventSlotHow
// so that a failure is a measurement and never a silent fallback to a guessed constant.
size_t ProcessEventSlot();  // SIZE_MAX when unknown
void SetProcessEventSlot(size_t slot, const char* how);
// Set to true by the events lane once our detour has actually fired, which is the only real proof
// that the derived slot was the right one.
void NoteProcessEventFired();
bool ProcessEventConfirmed();

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
// "_ZTV7UObject" -> "UObject". The demangler renders a vtable symbol as "vtable for UObject", so
// this strips that prefix; returns "" when `mangled` is not a vtable symbol.
std::string VTableClassName(const char* mangled);
// "UObject::ProcessEvent" -> "ProcessEvent"; "RequestEngineExit" -> "RequestEngineExit".
std::string BaseName(const std::string& key);
// True when the demangled line `line` is the wanted symbol: either exactly `sig` (when non-empty)
// or the text before '(' equals `key`.
bool NameMatches(const std::string& line, const char* key, const char* sig);

// ---- UTF-16LE string search (this binary's literals are wide) ----------------------------------
// Encodes an ASCII string as UTF-16LE bytes, so a literal can be searched for in `.rodata`.
std::vector<uint8_t> Utf16Bytes(const std::string& ascii);
// Finds every occurrence of `needle` in [hay, hay+len). Used to locate a string literal before
// looking for the instruction that references it.
std::vector<size_t> FindAll(const uint8_t* hay, size_t len, const uint8_t* needle, size_t nlen,
                            size_t maxHits = 8);
// Decodes the 4-byte little-endian signed displacement at `disp32Off` inside a matched instruction
// and returns the RIP-relative target: `matchVa + instrEnd + disp`, where `instrEnd` is
// `disp32Off + 4` (x86-64 RIP is the address of the *next* instruction).
uint64_t RipTarget(const uint8_t* match, uint64_t matchVa, size_t disp32Off);

// ---- ProcessEvent code fingerprint ------------------------------------------------------------
// Scores a candidate function body for the things `UObject::ProcessEvent` provably does, without
// needing a disassembler in-process. Each test that passes adds a point; the caller requires a
// minimum score AND uniqueness, never a score alone.
struct PeFingerprint {
    bool bigFrame = false;      // `sub rsp, imm32` (the FFrame + params buffer) or an `and rsp,-16` alloca
    bool funcNativeTest = false;// an immediate 0x400 (FUNC_Native) compared/tested against a dword
    bool indirectCall = false;  // `call qword ptr [reg+disp]` — UFunction::Func / the native thunk
    bool longEnough = false;    // >= 512 bytes before the first plausible function end
    int score() const { return (int)bigFrame + funcNativeTest + indirectCall + longEnough; }
};
// `body` is up to `len` bytes read from the candidate's entry point.
PeFingerprint FingerprintProcessEvent(const uint8_t* body, size_t len);

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
