#include "objpool.h"

#include "reflect.h"
#include "resolve.h"

#include <sys/uio.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>

namespace {

// ---- safe reads --------------------------------------------------------------------------------
//
// Everything in here dereferences pointers the engine owns, on a thread the engine does not know
// about. `MemReadable` (a cached /proc/self/maps lookup) is the first gate, but a cached map can be
// stale by microseconds, so the bulk scanner uses `process_vm_readv` on our own pid: an unmapped
// page makes it return EFAULT instead of raising SIGSEGV inside the game process. A plugin that
// crashes the server to answer a diagnostic question is worse than one that answers "unknown".
bool SafeRead(uint64_t addr, void* out, size_t len) {
    if (!addr || !len) return false;
    struct iovec local{out, len};
    struct iovec remote{(void*)(uintptr_t)addr, len};
    ssize_t n = process_vm_readv(getpid(), &local, 1, &remote, 1, 0);
    return n == (ssize_t)len;
}
bool ReadPtrSafe(uint64_t addr, uint64_t& out) { return SafeRead(addr, &out, 8); }
bool ReadU32Safe(uint64_t addr, uint32_t& out) { return SafeRead(addr, &out, 4); }

inline uint64_t Field(void* obj, uint32_t off) {
    uint64_t v = 0;
    if (!obj) return 0;
    if (!ReadPtrSafe((uint64_t)(uintptr_t)obj + off, v)) return 0;
    return v;
}

// ---- UE name prefixes --------------------------------------------------------------------------
// A UE class's C++ name carries a one-letter prefix that its FName does not: `UObject` is named
// "Object", `AActor` is named "Actor". This is the bridge between the RTTI (which gives us
// `_ZTV7UObject`) and the name pool (which gives us "Object"), and therefore the whole reason the
// pool can be validated without trusting it first.
std::string StripUePrefix(const std::string& cxx) {
    if (cxx.size() >= 2 && (cxx[0] == 'U' || cxx[0] == 'A' || cxx[0] == 'F' || cxx[0] == 'S' ||
                            cxx[0] == 'E' || cxx[0] == 'I') &&
        cxx[1] >= 'A' && cxx[1] <= 'Z')
        return cxx.substr(1);
    return cxx;
}

// ---- writable-region inventory -----------------------------------------------------------------
struct Region {
    uint64_t lo = 0, hi = 0;
};
std::vector<Region> WritableRegions() {
    std::vector<Region> out;
    FILE* f = fopen("/proc/self/maps", "re");
    if (!f) return out;
    char line[1024];
    while (fgets(line, sizeof line, f)) {
        uint64_t lo = 0, hi = 0;
        char perms[8] = {0};
        char path[512] = {0};
        int n = sscanf(line, "%lx-%lx %7s %*s %*s %*s %511[^\n]", &lo, &hi, perms, path);
        if (n < 3 || hi <= lo) continue;
        if (perms[0] != 'r' || perms[1] != 'w') continue;
        // Skip the kernel's own special mappings and our own writable data: the pool is a heap
        // allocation of the game's, never one of ours, and [vvar]/[vsyscall] are not safely readable.
        std::string p = path;
        if (p.find("[v") == 0 || p.find("[stack") == 0) continue;
        if (p.find("libtakaro-dune.so") != std::string::npos) continue;
        out.push_back({lo, hi});
    }
    fclose(f);
    return out;
}

// ---- FNameEntry --------------------------------------------------------------------------------
// FNameEntry is { FNameEntryHeader Header; union { ANSICHAR Ansi[]; WIDECHAR Wide[]; } }.
// FNameEntryHeader is a 16-bit bitfield laid out, LSB first, as
//     uint16 bIsWide : 1; uint16 LowercaseProbeHash : 5; uint16 Len : 10;
// which is why `Len` is the top 10 bits and `bIsWide` is bit 0. Both facts are *checked* rather than
// assumed: discovery only accepts a candidate whose header's Len equals the length of a string we
// already know from the RTTI, and Verify() re-checks Len against every decoded name.
constexpr uint32_t kLenShift = 6;
constexpr uint32_t kMaxNameLen = 1023;  // Len is 10 bits

bool DecodeEntry(uint64_t entryAddr, std::string& out, bool* wideOut = nullptr) {
    uint16_t h = 0;
    if (!SafeRead(entryAddr, &h, 2)) return false;
    uint32_t len = (uint32_t)(h >> kLenShift);
    bool wide = (h & 1) != 0;
    if (len == 0 || len > kMaxNameLen) return false;
    if (wideOut) *wideOut = wide;
    if (!wide) {
        std::vector<char> buf(len);
        if (!SafeRead(entryAddr + 2, buf.data(), len)) return false;
        for (uint32_t i = 0; i < len; i++) {
            unsigned char c = (unsigned char)buf[i];
            if (c < 0x20 || c == 0x7F) return false;  // an FName never contains a control character
        }
        out.assign(buf.data(), len);
        return true;
    }
    std::vector<char16_t> buf(len);
    if (!SafeRead(entryAddr + 2, buf.data(), (size_t)len * 2)) return false;
    out = Reflect::Utf16To8(buf.data(), (int)len);
    return !out.empty();
}

Pool::Facts g_f;
bool g_tried = false;

// Candidate block base for the block holding `idx`, given an entry address.
inline uint64_t BlockBaseFor(uint64_t entryAddr, uint32_t idx, uint32_t stride) {
    return entryAddr - (uint64_t)stride * (idx & 0xFFFF);
}

bool DecodeThroughArray(uint64_t blocksArray, uint32_t idx, std::string& out) {
    uint64_t blockPtr = 0;
    if (!ReadPtrSafe(blocksArray + 8ull * (idx >> 16), blockPtr)) return false;
    if (!blockPtr) return false;
    return DecodeEntry(blockPtr + (uint64_t)g_f.stride * (idx & 0xFFFF), out);
}

// ---- Classes:: state ---------------------------------------------------------------------------
Mutex g_clsLock;
std::map<std::string, void*> g_byName;
std::map<void*, std::string> g_nameOf;

}  // namespace

// ==================================================================================================
// Obj
// ==================================================================================================

Obj::ArrayFacts Obj::Array() {
    ArrayFacts a;
    uint64_t base = Resolve::Addr("GUObjectArray");
    uint64_t objs = Resolve::Addr("GUObjectArray.Objects");
    if (!base) { a.error = "GUObjectArray unresolved"; return a; }
    // The two signatures came from two different functions; if they disagree the address is wrong and
    // iterating it would walk garbage. Refuse instead.
    if (objs && objs != base + UObjArray::kObjects) {
        char b[160];
        snprintf(b, sizeof b, "GUObjectArray 0x%lx and .Objects 0x%lx differ by 0x%lx, not 0x%x", base, objs,
                 objs - base, UObjArray::kObjects);
        a.error = b;
        return a;
    }
    a.base = base;
    if (!ReadPtrSafe(base + UObjArray::kObjects, a.chunkTable) || !a.chunkTable) {
        a.error = "chunk table pointer unreadable";
        return a;
    }
    uint32_t n = 0;
    if (!ReadU32Safe(base + UObjArray::kNumElements, n)) { a.error = "NumElements unreadable"; return a; }
    a.numElements = (int32_t)n;
    if (a.numElements <= 0 || a.numElements > 200 * 1000 * 1000) {
        char b[96];
        snprintf(b, sizeof b, "NumElements = %d is implausible", a.numElements);
        a.error = b;
        return a;
    }
    a.ok = true;
    return a;
}

void* Obj::At(const ArrayFacts& a, int32_t i) {
    if (!a.ok || i < 0 || i >= a.numElements) return nullptr;
    uint64_t chunk = 0;
    if (!ReadPtrSafe(a.chunkTable + 8ull * ((uint32_t)i / UObjArray::kElementsPerChunk), chunk)) return nullptr;
    if (!chunk) return nullptr;
    uint64_t item = chunk + (uint64_t)UObjArray::kItemSize * ((uint32_t)i % UObjArray::kElementsPerChunk);
    uint64_t obj = 0;
    if (!ReadPtrSafe(item + UObjArray::kItemObject, obj)) return nullptr;
    return (void*)(uintptr_t)obj;
}

void Obj::ForEach(const std::function<bool(void*)>& fn, int32_t max) {
    ArrayFacts a = Array();
    if (!a.ok) return;
    int32_t n = a.numElements;
    if (max > 0 && max < n) n = max;
    for (int32_t i = 0; i < n; i++) {
        void* o = At(a, i);
        if (!o) continue;
        if (!fn(o)) return;
    }
}

// ==================================================================================================
// Pool
// ==================================================================================================

const Pool::Facts& Pool::F() { return g_f; }

bool Pool::Init() {
    // Latch only on SUCCESS. Failure must stay retryable: `GUObjectArray.ObjObjects.Objects` is
    // allocated by `FUObjectArray::AllocateObjectPool` during engine init, and the very first live
    // load showed the init thread getting there first ("chunk table pointer unreadable") on one boot
    // and not on the next. A one-shot Init() turned that race into a permanently degraded name
    // capability.
    if (g_f.ok) return true;
    g_f = Facts{};
    g_tried = true;
    uint64_t t0 = NowMs();
    const Reflect::Layout& lay = Reflect::Lay();

    // ---- step 1: UObject's own UClass, found structurally (no name required) --------------------
    Obj::ArrayFacts arr = Obj::Array();
    if (!arr.ok) { g_f.error = "object array unusable: " + arr.error; return false; }

    void* rootClass = nullptr;
    uint32_t chainMax = 0;
    Obj::ForEach([&](void* o) {
        if (!LooksLikeUObject(o)) return true;
        void* c = (void*)(uintptr_t)Field(o, lay.objClass);
        if (!LooksLikeUObject(c)) return true;
        // Walk Super to the end. Every UE class chain terminates at UObject's UClass.
        uint32_t steps = 0;
        void* prev = c;
        for (; steps < 64; steps++) {
            void* s = (void*)(uintptr_t)Field(prev, lay.structSuper);
            if (!s || !LooksLikeUObject(s)) break;
            prev = s;
        }
        if (steps < 2) return true;  // too shallow to be trustworthy
        if (steps > chainMax) { chainMax = steps; rootClass = prev; }
        return chainMax < 6;  // a chain of 6+ is conclusive; stop sweeping
    }, 20000);

    if (!rootClass) { g_f.error = "could not reach the root UClass through ClassPrivate/Super"; return false; }

    // ---- step 2: the anchors ---------------------------------------------------------------------
    // rootClass is UObject's UClass -> its name is "Object"; its ClassPrivate is UClass's UClass ->
    // "Class"; its OuterPrivate is the /Script/CoreUObject package -> a 19-character unique string.
    uint64_t pkg = Field(rootClass, lay.objOuter);
    uint64_t clsOfRoot = Field(rootClass, lay.objClass);
    if (!pkg || !clsOfRoot) { g_f.error = "root UClass has no Outer/Class - layout hypothesis wrong"; return false; }
    if (!ReadU32Safe((uint64_t)(uintptr_t)rootClass + lay.objName, g_f.idxObject) ||
        !ReadU32Safe(clsOfRoot + lay.objName, g_f.idxClass) ||
        !ReadU32Safe(pkg + lay.objName, g_f.idxPackage)) {
        g_f.error = "NamePrivate unreadable on an anchor object";
        return false;
    }
    const char* kPkgName = "/Script/CoreUObject";
    const uint32_t kPkgLen = 19;

    // ---- step 3+4: find candidate entries, and from each one a candidate block base -------------
    //
    // MEASURED, NOT ASSUMED: on the live process the three anchors do NOT share a block —
    // "/Script/CoreUObject" came back as index 164207 (block 2, offset 32943) while "Object" (502)
    // and "Class" (532) are in block 0. The first implementation demanded that a candidate decode a
    // second known name out of the SAME block, which is unsatisfiable here, so it rejected every
    // candidate and reported the pool unresolved. The cross-check therefore happens one step later,
    // through the Blocks[] array, where a candidate can be checked against names in any block.
    std::vector<Region> regs = WritableRegions();
    const size_t kChunk = 4u << 20;
    std::vector<uint8_t> buf(kChunk + 64);
    std::vector<uint64_t> candidateBases;
    for (const Region& r : regs) {
        for (uint64_t a = r.lo; a < r.hi;) {
            size_t want = (size_t)std::min<uint64_t>(kChunk, r.hi - a);
            if (!SafeRead(a, buf.data(), want)) { a += want; g_f.scannedBytes += want; continue; }
            g_f.scannedBytes += want;
            for (size_t i = 0; i + 2 + kPkgLen <= want; i++) {
                if (buf[i + 2] != '/') continue;
                uint16_t h = (uint16_t)(buf[i] | (buf[i + 1] << 8));
                if ((h >> kLenShift) != kPkgLen || (h & 1)) continue;
                if (memcmp(buf.data() + i + 2, kPkgName, kPkgLen) != 0) continue;
                g_f.candidateEntries++;
                uint64_t base = BlockBaseFor(a + i, g_f.idxPackage, g_f.stride);
                if (std::find(candidateBases.begin(), candidateBases.end(), base) == candidateBases.end())
                    candidateBases.push_back(base);
                if (candidateBases.size() >= 64) break;  // more than this means the key is not selective
            }
            if (want < kChunk) break;
            a += want - (2 + kPkgLen);
        }
        if (candidateBases.size() >= 64) break;
    }
    if (candidateBases.empty()) {
        g_f.error = "no FNameEntry with header Len=19,wide=0 followed by '/Script/CoreUObject' in " +
                    std::to_string(g_f.scannedBytes >> 20) + " MiB of writable memory";
        return false;
    }

    // ---- step 5: the Blocks[] array ---------------------------------------------------------------
    // One pass: any 8-byte word equal to ANY candidate block base is a possible `&Blocks[Block]`.
    // The candidate array is then accepted only when all three known names decode through it AND its
    // leading block pointers are distinct and readable.
    uint32_t block = g_f.idxPackage >> 16;
    for (const Region& r : regs) {
        if (g_f.blocksArray) break;
        for (uint64_t a = r.lo; a < r.hi && !g_f.blocksArray;) {
            size_t want = (size_t)std::min<uint64_t>(kChunk, r.hi - a);
            if (!SafeRead(a, buf.data(), want)) { a += want; continue; }
            for (size_t i = 0; i + 8 <= want; i += 8) {
                uint64_t v;
                memcpy(&v, buf.data() + i, 8);
                if (std::find(candidateBases.begin(), candidateBases.end(), v) == candidateBases.end()) continue;
                g_f.candidateArrays++;
                uint64_t cand = (a + i) - 8ull * block;
                std::string s1, s2, s3;
                bool ok = DecodeThroughArray(cand, g_f.idxPackage, s1) && s1 == kPkgName &&
                          DecodeThroughArray(cand, g_f.idxObject, s2) && s2 == "Object" &&
                          DecodeThroughArray(cand, g_f.idxClass, s3) && s3 == "Class";
                if (!ok) continue;
                uint32_t readable = 0;
                std::vector<uint64_t> seen;
                for (uint32_t k = 0; k < 4096; k++) {
                    uint64_t bp = 0;
                    if (!ReadPtrSafe(cand + 8ull * k, bp) || !bp) break;
                    uint16_t probe = 0;
                    if (!SafeRead(bp, &probe, 2)) break;
                    if (std::find(seen.begin(), seen.end(), bp) != seen.end()) break;
                    seen.push_back(bp);
                    readable++;
                }
                // The array must at least span the block our anchor lives in, otherwise it is a
                // coincidental pointer rather than the allocator's block table.
                if (readable <= block) continue;
                g_f.blocksArray = cand;
                g_f.block0 = seen.empty() ? 0 : seen[0];
                g_f.blocksReadable = readable;
                break;
            }
            if (want < kChunk) break;
            a += want - 8;
        }
    }
    if (!g_f.blocksArray) {
        g_f.error = "found " + std::to_string(candidateBases.size()) +
                    " candidate block base(s) for '/Script/CoreUObject' but no Blocks[] array that "
                    "decodes 'Object' and 'Class' too";
        return false;
    }

    g_f.headerBits = kLenShift;
    g_f.fnameWidth = 8;  // upgraded to a measurement by Verify()
    g_f.ok = true;
    g_f.discoverMs = NowMs() - t0;
    char how[512];
    snprintf(how, sizeof how,
             "live search: root UClass by Super-chain terminal -> Outer = '/Script/CoreUObject' (idx %u) "
             "-> FNameEntry header Len=19,wide=0 in %llu MiB of rw memory (%u candidate entries) "
             "-> block base cross-checked against 'Object' (idx %u) and 'Class' (idx %u) "
             "-> Blocks[] by pointer-to-block scan (%u candidates), %u readable blocks, stride %u",
             g_f.idxPackage, (unsigned long long)(g_f.scannedBytes >> 20), g_f.candidateEntries, g_f.idxObject,
             g_f.idxClass, g_f.candidateArrays, g_f.blocksReadable, g_f.stride);
    g_f.how = how;
    PluginLog("pool: %s (%llu ms)", g_f.how.c_str(), (unsigned long long)g_f.discoverMs);
    return true;
}

std::string Pool::Name(uint32_t idx) {
    if (!g_f.ok) return "";
    std::string s;
    if (!DecodeThroughArray(g_f.blocksArray, idx, s)) return "";
    return s;
}

std::string Pool::NameNumbered(uint32_t idx, uint32_t number) {
    std::string s = Name(idx);
    if (s.empty() || number == 0) return s;
    return s + "_" + std::to_string(number - 1);
}

std::string Pool::NameAt(void* obj, uint32_t off) {
    if (!obj || !g_f.ok) return "";
    uint32_t idx = 0, num = 0;
    if (!ReadU32Safe((uint64_t)(uintptr_t)obj + off, idx)) return "";
    if (!ReadU32Safe((uint64_t)(uintptr_t)obj + off + 4, num)) num = 0;
    if (num > 0xFFFFFF) num = 0;  // a bogus Number means the field is not an FName; ignore it
    return NameNumbered(idx, num);
}

std::string Pool::Json() {
    std::string o = "{\"ok\":" + std::string(g_f.ok ? "true" : "false");
    char b[256];
    snprintf(b, sizeof b,
             ",\"blocksArray\":\"0x%lx\",\"block0\":\"0x%lx\",\"stride\":%u,\"blocksReadable\":%u"
             ",\"fnameWidth\":%u,\"lenShift\":%u,\"scannedMiB\":%llu,\"discoverMs\":%llu"
             ",\"candidateEntries\":%u,\"candidateArrays\":%u",
             g_f.blocksArray, g_f.block0, g_f.stride, g_f.blocksReadable, g_f.fnameWidth, g_f.headerBits,
             (unsigned long long)(g_f.scannedBytes >> 20), (unsigned long long)g_f.discoverMs,
             g_f.candidateEntries, g_f.candidateArrays);
    o += b;
    o += ",\"anchors\":{\"/Script/CoreUObject\":" + std::to_string(g_f.idxPackage) +
         ",\"Object\":" + std::to_string(g_f.idxObject) + ",\"Class\":" + std::to_string(g_f.idxClass) + "}";
    o += ",\"how\":" + JsonStr(g_f.how);
    if (!g_f.error.empty()) o += ",\"error\":" + JsonStr(g_f.error);
    return o + "}";
}

Pool::VerifyResult Pool::Verify(uint32_t wantSamples) {
    VerifyResult v;
    if (!g_f.ok) return v;
    const Reflect::Layout& lay = Reflect::Lay();
    const auto& vts = VTableSymbols();
    // vtable runtime address -> demangled class name, for exact O(log n) identification.
    static std::map<uint64_t, std::string> byAddr;
    if (byAddr.empty())
        for (const auto& e : vts) {
            std::string cn = ResolveCore::VTableClassName(e.first.c_str());
            // A `_ZTV` symbol addresses the two header words; an object's vptr points at SLOT 0,
            // i.e. `_ZTV + 16`. Getting this wrong is exactly why the first live Verify() run tested
            // 0 objects: every comparison missed by 16 bytes.
            if (!cn.empty()) byAddr[e.second.first + 16] = cn;
        }

    // One sample per DISTINCT vtable class, so the proof is diverse rather than 240 repetitions of
    // UClass and UPackage (which is what the first run produced when it stopped on a plain count).
    std::set<std::string> seenClasses;
    Obj::ForEach([&](void* o) {
        if (v.objectsTested >= 200000) return false;
        uint64_t vptr = Field(o, 0);
        auto it = byAddr.find(vptr);
        if (it == byAddr.end()) return true;
        void* c = (void*)(uintptr_t)Field(o, lay.objClass);
        if (!LooksLikeUObject(c)) return true;
        v.objectsTested++;
        std::string want = StripUePrefix(it->second);
        // Decode the whole Class->Super chain and look for `want` in it.
        std::string chain;
        bool hit = false;
        uint32_t steps = 0;
        for (void* s = c; s && steps < 64; steps++) {
            uint32_t idx = 0, num = 0;
            if (!ReadU32Safe((uint64_t)(uintptr_t)s + lay.objName, idx)) break;
            ReadU32Safe((uint64_t)(uintptr_t)s + lay.objName + 4, num);
            std::string n = Name(idx);
            if (n.empty()) break;
            v.namesDecoded++;
            if (num) v.numberedNames++;
            if (!chain.empty()) chain += "<-";
            chain += n;
            if (n == want) hit = true;
            void* nx = (void*)(uintptr_t)Field(s, lay.structSuper);
            if (!nx || !LooksLikeUObject(nx)) { s = nullptr; break; }
            s = nx;
        }
        if (steps > v.maxChainLen) v.maxChainLen = steps;
        if (chain.empty()) {
            v.inconclusive++;
        } else if (hit) {
            v.matched++;
            if (v.samples.size() < wantSamples && seenClasses.insert(it->second).second)
                v.samples.push_back({it->second, chain});
        } else {
            v.mismatched++;
            if (v.failures.size() < 12) v.failures.push_back(it->second + " -> " + chain);
        }
        // Stop once the evidence is both plentiful and varied.
        return !(seenClasses.size() >= wantSamples && v.matched >= wantSamples * 4);
    });

    // Wide-entry census over the first block. It walks the block ENTRY BY ENTRY, advancing by each
    // entry's own encoded length, because probing every stride multiple lands in the middle of
    // entries and "decodes" garbage — the first run reported 1955 wide entries that way, which is
    // nonsense for a block whose names are all ASCII class and package names.
    uint64_t off = 0;
    for (uint32_t k = 0; k < 20000 && g_f.block0; k++) {
        uint16_t h = 0;
        if (!SafeRead(g_f.block0 + off, &h, 2)) break;
        uint32_t len = (uint32_t)(h >> kLenShift);
        bool wide = (h & 1) != 0;
        if (len == 0 || len > kMaxNameLen) break;  // end of the written part of the block
        std::string s;
        if (DecodeEntry(g_f.block0 + off, s, nullptr)) {
            v.entriesWalked++;
            if (wide) v.wideEntries++;
        }
        uint64_t bytes = 2 + (uint64_t)len * (wide ? 2 : 1);
        off += (bytes + g_f.stride - 1) / g_f.stride * g_f.stride;  // entries are stride-aligned
    }
    return v;
}

// ==================================================================================================
// Classes
// ==================================================================================================

bool Classes::Build() {
    if (!Pool::F().ok) return false;
    uint64_t ztvClass = DynSymAddr("_ZTV6UClass");
    if (!ztvClass) return false;
    ztvClass += 16;  // objects point at slot 0, not at the _ZTV header words
    std::map<std::string, void*> byName;
    std::map<void*, std::string> nameOf;
    Obj::ForEach([&](void* o) {
        if (Field(o, 0) != ztvClass) return true;
        std::string n = Pool::NameAt(o, Reflect::Lay().objName);
        if (n.empty()) return true;
        nameOf[o] = n;
        // First writer wins: a duplicate name means two packages define the same class name, and the
        // one registered first is the engine/game class rather than a transient.
        byName.emplace(n, o);
        return true;
    });
    if (byName.empty()) return false;
    Guard g(g_clsLock);
    g_byName.swap(byName);
    g_nameOf.swap(nameOf);
    PluginLog("classes: %zu live UClass objects indexed by decoded name", g_byName.size());
    return true;
}

size_t Classes::Count() {
    Guard g(g_clsLock);
    return g_byName.size();
}

void* Classes::ByName(const std::string& name) {
    Guard g(g_clsLock);
    auto it = g_byName.find(name);
    return it == g_byName.end() ? nullptr : it->second;
}

std::string Classes::NameOf(void* cls) {
    Guard g(g_clsLock);
    auto it = g_nameOf.find(cls);
    return it == g_nameOf.end() ? std::string() : it->second;
}

std::vector<std::pair<std::string, void*>> Classes::WithPrefix(const std::string& prefix, size_t limit) {
    std::vector<std::pair<std::string, void*>> out;
    Guard g(g_clsLock);
    for (auto it = g_byName.lower_bound(prefix); it != g_byName.end(); ++it) {
        if (it->first.compare(0, prefix.size(), prefix) != 0) break;
        out.push_back(*it);
        if (out.size() >= limit) break;
    }
    return out;
}

// ==================================================================================================
// Field / property layout, decided on the live process
// ==================================================================================================

namespace {

std::string g_verifyJson = "{}";
uint32_t g_fieldNextUField = 0;
std::string g_propOffsetHow = "not attempted";

// Scores one candidate (chainHead, nameOff, nextOff) triple: how many consecutive nodes decode to a
// pool-resident identifier. A wrong offset produces indices whose FNameEntry header fails its own
// length/charset check, so it scores 0. `clean` means the chain ended on a null Next rather than on
// a decode failure, which is what distinguishes the real field list from a lucky prefix.
size_t ScoreChain(void* head, uint32_t nameOff, uint32_t nextOff, bool& clean, std::string& firstName) {
    clean = false;
    size_t n = 0;
    void* f = head;
    for (; f && n < 4096; n++) {
        uint32_t idx = 0;
        if (!ReadU32Safe((uint64_t)(uintptr_t)f + nameOff, idx)) break;
        std::string s = Pool::Name(idx);
        if (s.empty()) break;
        if (n == 0) firstName = s;
        uint64_t nx = Field(f, nextOff);
        if (!nx) { n++; clean = true; break; }
        if (nx == (uint64_t)(uintptr_t)f) break;
        f = (void*)(uintptr_t)nx;
    }
    return n;
}

}  // namespace

bool Pool::DiscoverFieldLayout(std::string& detail) {
    if (!g_f.ok) { detail = "name pool unresolved"; return false; }
    Reflect::Layout& lay = Reflect::LayMut();
    // AActor's class is the anchor: it exists on every UE build and has dozens of UPROPERTYs and
    // ~165 UFUNCTIONs, so a correct triple scores high and a wrong one cannot.
    void* cls = Classes::ByName("Actor");
    if (!cls) cls = Classes::ByName("Object");
    if (!cls) { detail = "neither the Actor nor the Object UClass was found by name"; return false; }

    // A UStruct carries TWO chains and they must be told apart, because the first live run conflated
    // them: an `FProperty` also has a vtable pointer whose `_ZTV` this binary exports, so
    // "is the head a UObject?" classified the FField chain as a UField chain and the property offsets
    // were never found (`childProperties=0x0`). The reliable discriminator is `FField::ClassPrivate`
    // at +0x08: it points at an `FFieldClass` whose first member is an FName that decodes to a type
    // name ending in "Property" ("BoolProperty", "ObjectProperty", ...). A UField has no such field.
    auto looksLikeFField = [&](void* head) {
        uint64_t fc = Field(head, 0x08);
        if (!fc) return false;
        std::string t = Pool::NameAt((void*)(uintptr_t)fc, 0);
        return t.size() > 8 && t.compare(t.size() - 8, 8, "Property") == 0;
    };

    struct Best { uint32_t child = 0, name = 0, next = 0; size_t len = 0; std::string first; };
    Best bestProps, bestChildren;
    for (uint32_t childOff = 0x38; childOff <= 0x78; childOff += 8) {
        void* head = (void*)(uintptr_t)Field(cls, childOff);
        if (!head || !MemReadable(head, 0x40)) continue;
        bool isField = looksLikeFField(head);
        bool isUField = !isField && LooksLikeUObject(head);
        if (!isField && !isUField) continue;
        for (uint32_t nameOff = 0x10; nameOff <= 0x40; nameOff += 8) {
            for (uint32_t nextOff = 0x08; nextOff <= 0x40; nextOff += 8) {
                if (nextOff == nameOff) continue;
                bool clean = false;
                std::string first;
                size_t len = ScoreChain(head, nameOff, nextOff, clean, first);
                if (!clean || len < 3) continue;
                Best& slot = isField ? bestProps : bestChildren;
                if (len > slot.len) slot = {childOff, nameOff, nextOff, len, first};
            }
        }
    }
    // `FProperty::Offset_Internal` is NOT at the 0x44 this tree inherited: on UE 5.2 `FField` is
    // 0x38 bytes (vtable, ClassPrivate, a 16-byte FFieldVariant Owner, Next, NamePrivate, Flags) and
    // FProperty then adds ArrayDim/ElementSize/PropertyFlags/RepIndex before Offset_Internal. The
    // first live dump returned offset -1 for every property because 0x44 lands in the middle of the
    // 64-bit PropertyFlags. So the offset field is discovered too, with a purely structural oracle:
    // the right candidate makes every property of the class land inside [0, UStruct::PropertiesSize)
    // and produces distinct, mostly ascending values.
    if (bestProps.len) {
        uint32_t propsSize = 0;
        ReadU32Safe((uint64_t)(uintptr_t)cls + lay.structPropertiesSize, propsSize);
        uint32_t bestOff = 0, bestScore = 0;
        if (propsSize && propsSize < 0x100000) {
            for (uint32_t cand = 0x38; cand <= 0x60; cand += 4) {
                uint32_t inRange = 0, total = 0, ascending = 0;
                int64_t prev = -1;
                void* f = (void*)(uintptr_t)Field(cls, bestProps.child);
                for (size_t i = 0; f && i < bestProps.len; i++) {
                    uint32_t v = 0;
                    if (ReadU32Safe((uint64_t)(uintptr_t)f + cand, v)) {
                        total++;
                        if (v < propsSize) inRange++;
                        if ((int64_t)v > prev) ascending++;
                        prev = (int64_t)v;
                    }
                    uint64_t nx = Field(f, bestProps.next);
                    if (!nx) break;
                    f = (void*)(uintptr_t)nx;
                }
                if (!total) continue;
                uint32_t score = inRange * 2 + ascending;
                if (inRange == total && score > bestScore) { bestScore = score; bestOff = cand; }
            }
        }
        if (bestOff) lay.propOffsetInternal = bestOff;
        g_propOffsetHow = bestOff ? ("discovered: all " + std::to_string(bestProps.len) +
                                     " properties inside PropertiesSize=" + std::to_string(propsSize))
                                  : "NOT discovered; offsets will report -1";
    }
    char b[512];
    if (bestProps.len) {
        lay.structChildProperties = bestProps.child;
        lay.fieldName = bestProps.name;
        lay.fieldNext = bestProps.next;
    }
    if (bestChildren.len) {
        lay.structChildren = bestChildren.child;
        g_fieldNextUField = bestChildren.next;
    }
    snprintf(b, sizeof b,
             "FField chain: childProperties=0x%x fieldName=0x%x fieldNext=0x%x (%zu properties, first '%s'); "
             "UField chain: children=0x%x next=0x%x (%zu entries, first '%s'); "
             "propOffsetInternal=0x%x (%s)",
             bestProps.child, bestProps.name, bestProps.next, bestProps.len, bestProps.first.c_str(),
             bestChildren.child, bestChildren.next, bestChildren.len, bestChildren.first.c_str(),
             Reflect::Lay().propOffsetInternal, g_propOffsetHow.c_str());
    detail = b;
    return bestProps.len > 0 && bestChildren.len > 0;
}

std::vector<Pool::PropInfo> Pool::PropsOf(void* strct, size_t max) {
    std::vector<PropInfo> out;
    if (!g_f.ok || !strct) return out;
    const Reflect::Layout& lay = Reflect::Lay();
    void* f = (void*)(uintptr_t)Field(strct, lay.structChildProperties);
    for (size_t i = 0; f && i < max; i++) {
        PropInfo p;
        p.name = NameAt(f, lay.fieldName);
        if (p.name.empty()) break;
        // FField::ClassPrivate is an FFieldClass*, whose first member is its FName ("BoolProperty").
        uint64_t fc = Field(f, lay.fieldClass);
        if (fc) p.type = NameAt((void*)(uintptr_t)fc, 0);
        uint32_t off = 0;
        if (ReadU32Safe((uint64_t)(uintptr_t)f + lay.propOffsetInternal, off) && off < 0x100000)
            p.offset = (int32_t)off;
        out.push_back(p);
        uint64_t nx = Field(f, lay.fieldNext);
        if (!nx || nx == (uint64_t)(uintptr_t)f) break;
        f = (void*)(uintptr_t)nx;
    }
    return out;
}

std::vector<std::pair<std::string, void*>> Pool::FunctionsOf(void* cls) {
    std::vector<std::pair<std::string, void*>> out;
    if (!g_f.ok || !cls) return out;
    const Reflect::Layout& lay = Reflect::Lay();
    // UField::Next is at the UObject-derived layout's +0x28 on UE5, but it is not assumed: the chain
    // is followed through whichever offset yields decodable names, same oracle as above.
    void* f = (void*)(uintptr_t)Field(cls, lay.structChildren);
    uint32_t nextOff = g_fieldNextUField;
    if (!nextOff && f) {
        for (uint32_t n = 0x18; n <= 0x40 && !nextOff; n += 8) {
            bool clean = false;
            std::string first;
            if (ScoreChain(f, lay.objName, n, clean, first) >= 3 && clean) nextOff = n;
        }
    }
    if (!nextOff) return out;
    for (size_t i = 0; f && i < 4096; i++) {
        std::string n = NameAt(f, lay.objName);
        if (n.empty()) break;
        out.push_back({n, f});
        uint64_t nx = Field(f, nextOff);
        if (!nx || nx == (uint64_t)(uintptr_t)f) break;
        f = (void*)(uintptr_t)nx;
    }
    return out;
}

const Pool::VerifyResult& Pool::CachedVerify() {
    static VerifyResult cached;
    static bool done = false;
    if (!done && g_f.ok) { cached = Verify(60); done = true; }
    return cached;
}

std::string Pool::VerifyJson() {
    if (g_verifyJson != "{}") return g_verifyJson;
    if (!g_f.ok) return "{\"ok\":false,\"error\":\"name pool unresolved\"}";
    const VerifyResult& v = CachedVerify();
    std::string o = "{\"objectsTested\":" + std::to_string(v.objectsTested) +
                    ",\"classNameMatchedVTableIdentity\":" + std::to_string(v.matched) +
                    ",\"mismatched\":" + std::to_string(v.mismatched) +
                    ",\"inconclusive\":" + std::to_string(v.inconclusive) +
                    ",\"namesDecoded\":" + std::to_string(v.namesDecoded) +
                    ",\"numberedNames\":" + std::to_string(v.numberedNames) +
                    ",\"block0EntriesWalked\":" + std::to_string(v.entriesWalked) +
                    ",\"wideEntriesInBlock0\":" + std::to_string(v.wideEntries) +
                    ",\"maxSuperChain\":" + std::to_string(v.maxChainLen) + ",\"samples\":[";
    for (size_t i = 0; i < v.samples.size(); i++) {
        if (i) o += ",";
        o += "{\"vtable\":" + JsonStr(v.samples[i].first) + ",\"chain\":" + JsonStr(v.samples[i].second) + "}";
    }
    o += "],\"failures\":[";
    for (size_t i = 0; i < v.failures.size(); i++) {
        if (i) o += ",";
        o += JsonStr(v.failures[i]);
    }
    o += "]}";
    g_verifyJson = o;
    return o;
}
