// Live object enumeration and FName decoding for a binary with NO function symbols.
//
// WHY THIS FILE EXISTS
//
// `reflect.cpp` was ported from connectors whose server binary shipped a `.symtab`, so its whole
// object model funnels through resolved engine callables: `FName::ToString`, `UObject::FindFunction`,
// `UStruct::FindPropertyByName`, `GetObjectsOfClass`, `StaticFindObject`. On
// `DuneSandboxServer-Linux-Shipping` **none of those exist** — the binary is stripped and only the
// C++ RTTI survives — so on the live process every one of them resolves to nullptr and the entire
// reflection layer reports `degraded`. That was observed, not assumed: the first live load
// (bootId d568b3b7b5406079, 2026-09-21 13:40:58Z) logged
// `reflect: init ready=0 nameCtor=(nil) toString=(nil) findFunction=(nil) findProperty=(nil)`.
//
// So this module does the same jobs by **reading engine data structures directly**, with no call
// into the game at all:
//
//   * `Obj::` walks `GUObjectArray`'s chunked object array (layout proven from the inlined
//     `FChunkedFixedUObjectArray::GetObjectPtr`, see reflect.h).
//   * `Pool::` locates `FNamePool` on the LIVE process and decodes `FName` -> text itself.
//
// Nothing here is a constant lifted from a published offset table. The pool is *searched for* and
// every candidate must decode names that are already known from an independent source (the RTTI), so
// a wrong answer is rejected rather than trusted. If discovery fails, `Pool::F().ok` is false, the
// `symbols.names` capability stays degraded, and no name is ever read.
//
// HOW THE POOL IS FOUND (all of it live, none of it assumed)
//
//  1. Walk `GUObjectArray` for any object; follow `ClassPrivate` (+0x10) and then `UStruct::Super`
//     (+0x40) to the end of the chain. Every UE class chain terminates at `UObject`'s own `UClass`,
//     so the terminal node is `UObject`'s UClass **by construction** — no name needed to find it.
//  2. That UClass's `OuterPrivate` (+0x20) is the `/Script/CoreUObject` package. So we now hold a
//     `FName` whose text we know with certainty, and it is 19 characters long and unique —
//     exactly what a memory search needs. (`"Object"` and `"Class"`, also known, are too short to
//     search for, but they are perfect *cross-checks*.)
//  3. Scan every writable mapping for `FNameEntry`'s 16-bit header followed by those 19 bytes. The
//     header is a bitfield (`bIsWide:1, LowercaseProbeHash:5, Len:10`), so the search key is
//     "`Len == 19` and `bIsWide == 0`" — i.e. `(h >> 6) == 19 && (h & 1) == 0` — which makes the
//     probe-hash bits wildcards instead of unknowns. Reads go through `process_vm_readv`, so a
//     racing `munmap` yields `EFAULT` instead of a SIGSEGV inside the game process.
//  4. A hit gives the entry address E. `FNameEntryAllocator::Resolve` is
//     `Blocks[Handle.Block] + Stride * Handle.Offset` with `Stride == alignof(FNameEntry) == 2`, so
//     the block base is `E - 2 * (idx & 0xFFFF)`. That base is only accepted once `"Object"` and
//     `"Class"` (whose indices we also hold) decode correctly through it.
//  5. The `Blocks` array itself is then found by scanning for a pointer whose value IS that block
//     base; `&Blocks[0]` is that address minus `8 * Block`. Accepted only when the array's leading
//     entries are distinct, readable, and decode the same three known names.
//
// FNAME WIDTH. The binary carries the literal "…only when UE_FNAME_OUTLINE_NUMBER is set", so
// `sizeof(FName)` (8 vs 4) is a build switch. It is settled here empirically: the pool is driven
// from the low 32 bits at `NamePrivate+0`, and a successful decode of the known names — plus
// `OuterPrivate` at +0x20 still yielding a valid UObject — is what makes 8 bytes a measurement.
#pragma once
#include "common.h"

#include <functional>

namespace Obj {

// {base, chunkTable, numElements} read live from GUObjectArray. `ok` is false when GUObjectArray did
// not resolve or the two independent signatures disagreed.
struct ArrayFacts {
    bool ok = false;
    uint64_t base = 0;
    uint64_t chunkTable = 0;
    int32_t numElements = 0;
    std::string error;
};
ArrayFacts Array();

// The i-th live UObject, or nullptr for a free slot / unreadable chunk. Bounds-checked.
void* At(const ArrayFacts& a, int32_t i);

// Calls `fn` for every non-null object, newest last. Stops early when `fn` returns false.
// Safe on any thread: it only reads, and every dereference is guarded by MemReadable.
void ForEach(const std::function<bool(void*)>& fn, int32_t max = 0);

}  // namespace Obj

namespace Pool {

struct Facts {
    bool ok = false;
    uint64_t blocksArray = 0;   // &Blocks[0]
    uint64_t block0 = 0;        // Blocks[0]
    uint32_t stride = 2;        // FNameEntryAllocator::Stride == alignof(FNameEntry)
    uint32_t blocksReadable = 0;// how many leading Blocks[] entries point at readable memory
    uint32_t fnameWidth = 0;    // 8 once a decode proved ComparisonIndex is the low dword of 8 bytes
    uint32_t headerBits = 0;    // Len shift, i.e. 6 — recorded so the layout claim is explicit
    // The anchors the search was driven by, with the index that produced them.
    uint32_t idxPackage = 0, idxObject = 0, idxClass = 0;
    uint64_t scannedBytes = 0;
    uint32_t candidateEntries = 0, candidateArrays = 0;
    uint64_t discoverMs = 0;
    std::string how;
    std::string error;
};
const Facts& F();

// One-shot discovery. Never throws, never calls into the game. Returns F().ok.
// Safe off the game thread (FNameEntry contents are immutable once written; only the tail of the
// current block ever changes, and a half-written entry fails its own length check).
bool Init();

// Decodes a comparison index to text. "" when the pool is unknown or the entry fails validation.
std::string Name(uint32_t comparisonIndex);
// UE renders `FName{idx, Number}` as "<base>_<Number-1>" for Number > 0.
std::string NameNumbered(uint32_t comparisonIndex, uint32_t number);
// The FName stored at `obj + off` (8 bytes), decoded. "" on any failure.
std::string NameAt(void* obj, uint32_t off);

std::string Json();  // the Facts block for /health.diagnostics.namePool

// ---- validation -------------------------------------------------------------------------------
// Decodes the class chain of live objects and checks it against what the RTTI already says the class
// is. For an object whose first word is the exported `_ZTV<C>` of class C, the decoded
// ClassPrivate->Super chain MUST contain C's name with its UE prefix stripped; if the pool were
// wrong this would essentially never hold. Returns the JSON report and fills the counters.
struct VerifyResult {
    uint32_t objectsTested = 0, matched = 0, mismatched = 0, inconclusive = 0;
    uint32_t namesDecoded = 0, wideEntries = 0, numberedNames = 0, entriesWalked = 0;
    uint32_t maxChainLen = 0;
    std::vector<std::pair<std::string, std::string>> samples;  // {vtable class, decoded chain}
    std::vector<std::string> failures;
};
VerifyResult Verify(uint32_t wantSamples = 60);
// Runs Verify() once and caches both the report and its JSON.
const VerifyResult& CachedVerify();
std::string VerifyJson();

// Decides `structChildProperties`, `fieldName` and `fieldNext` on the LIVE process, writing the
// winners into Reflect::Lay(). The VEIN tree did this by round-tripping candidate FNames through
// `UStruct::FindPropertyByName`; there is no such callable here, so the oracle is the name pool
// itself: the right offset triple is the one whose FField chain decodes to a run of **valid,
// pool-resident identifiers** that terminates cleanly. A wrong offset yields garbage indices that
// fail their own entry-header check, so it scores 0 and loses.
bool DiscoverFieldLayout(std::string& detail);

// Every UFunction owned by a class, decoded name -> UFunction*. Walks UStruct::Children (the UField
// chain) rather than ChildProperties; the offset is discovered the same validator-driven way.
std::vector<std::pair<std::string, void*>> FunctionsOf(void* cls);
// Property {name, type, offset} of one UStruct, from the FField chain.
struct PropInfo {
    std::string name, type;
    int32_t offset = -1;
};
std::vector<PropInfo> PropsOf(void* strct, size_t max = 512);

}  // namespace Pool

namespace Classes {
// Every live `UClass` object, keyed by decoded name. Built by one GUObjectArray sweep; a UClass is
// identified by its own first word being the exported `_ZTV6UClass`, so no name is needed to find
// them and a pool failure cannot produce a bogus entry (it produces an empty map instead).
bool Build();
size_t Count();
void* ByName(const std::string& name);
// Names (and pointers) whose name starts with `prefix`, at most `limit`.
std::vector<std::pair<std::string, void*>> WithPrefix(const std::string& prefix, size_t limit);
// The decoded name of a UClass*, or "".
std::string NameOf(void* cls);
}  // namespace Classes
