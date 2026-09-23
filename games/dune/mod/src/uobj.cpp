#include "uobj.h"

#include "objpool.h"
#include "reflect.h"
#include "resolve.h"

#include <cmath>
#include <cstring>

namespace {
Mutex g_cacheLock;
// (UClass*, property name) -> offset. A miss is cached as -1 too: a name that is not on the class is
// a permanent fact for that class, and re-walking a 500-property chain on every kill would be paid
// on the game thread.
std::map<std::pair<const void*, std::string>, int32_t> g_propCache;
uint64_t g_misses = 0;
}  // namespace

bool U::ReadBytes(const void* obj, uint32_t off, void* dst, size_t n) {
    if (!obj) return false;
    const char* p = (const char*)obj + off;
    if (!MemReadable(p, n)) return false;
    memcpy(dst, p, n);
    return true;
}

void* U::ReadPtr(const void* obj, uint32_t off) {
    void* v = nullptr;
    if (!ReadBytes(obj, off, &v, sizeof v)) return nullptr;
    return v;
}

uint64_t U::ReadU64(const void* obj, uint32_t off, bool* ok) {
    uint64_t v = 0;
    bool r = ReadBytes(obj, off, &v, sizeof v);
    if (ok) *ok = r;
    return r ? v : 0;
}

uint32_t U::ReadU32(const void* obj, uint32_t off, bool* ok) {
    uint32_t v = 0;
    bool r = ReadBytes(obj, off, &v, sizeof v);
    if (ok) *ok = r;
    return r ? v : 0;
}

uint8_t U::ReadU8(const void* obj, uint32_t off, bool* ok) {
    uint8_t v = 0;
    bool r = ReadBytes(obj, off, &v, sizeof v);
    if (ok) *ok = r;
    return r ? v : 0;
}

bool U::Vec::Finite() const { return std::isfinite(x) && std::isfinite(y) && std::isfinite(z); }

bool U::ReadVec(const void* obj, uint32_t off, Vec& out) {
    double v[3] = {0, 0, 0};
    if (!ReadBytes(obj, off, v, sizeof v)) return false;
    out.x = v[0];
    out.y = v[1];
    out.z = v[2];
    // A non-finite or absurd transform means we are reading the wrong thing; say so rather than
    // shipping a position Takaro would plot in the middle of nowhere. Arrakis is ~ ±10^7 cm.
    if (!out.Finite()) return false;
    const double kLimit = 1e9;
    return std::fabs(out.x) < kLimit && std::fabs(out.y) < kLimit && std::fabs(out.z) < kLimit;
}

void* U::ResolveWeak(const void* obj, uint32_t off, bool* serialMatched) {
    if (serialMatched) *serialMatched = false;
    struct Weak {
        int32_t index;
        int32_t serial;
    } w{0, 0};
    if (!ReadBytes(obj, off, &w, sizeof w)) return nullptr;
    if (w.index <= 0) return nullptr;  // 0 is the null/CDO slot; negative is invalid
    Obj::ArrayFacts a = Obj::Array();
    if (!a.ok || w.index >= a.numElements) return nullptr;
    void* o = Obj::At(a, w.index);
    if (!o || !LooksLikeUObject(o)) return nullptr;
    // Corroboration only: FUObjectItem::SerialNumber at +0x10 is the stock UE 5.x layout, and unlike
    // the four offsets lane L1b read out of the disassembly it was not proven on this binary.
    uint64_t chunk = 0;
    if (MemReadable((const char*)a.chunkTable + 8 * (w.index >> 16), 8)) {
        memcpy(&chunk, (const char*)a.chunkTable + 8 * (w.index >> 16), 8);
        const char* item = (const char*)chunk + (size_t)(w.index & 0xFFFF) * UObjArray::kItemSize;
        int32_t serial = 0;
        if (chunk && ReadBytes(item, 0x10, &serial, sizeof serial) && serial == w.serial && serialMatched)
            *serialMatched = true;
    }
    return o;
}

void* U::ClassOf(const void* obj) {
    if (!obj) return nullptr;
    return ReadPtr(obj, Reflect::Lay().objClass);
}

uint32_t U::NameIdx(const void* obj) {
    if (!obj) return 0;
    return ReadU32(obj, Reflect::Lay().objName);
}

std::string U::NameOf(const void* obj) {
    if (!obj || !Pool::F().ok) return "";
    return Pool::NameAt((void*)obj, Reflect::Lay().objName);
}

std::string U::ClassNameOf(const void* obj) {
    void* cls = ClassOf(obj);
    if (!cls) return "";
    std::string n = Classes::NameOf(cls);
    if (!n.empty()) return n;
    return NameOf(cls);
}

bool U::ClassChainHas(void* cls, void* want) {
    if (!cls || !want) return false;
    void* c = cls;
    for (int i = 0; c && i < 64; i++) {
        if (c == want) return true;
        c = Reflect::SuperStruct(c);
    }
    return false;
}

bool U::IsA(const void* obj, const char* className) {
    void* want = Classes::ByName(className);
    if (!want) return false;
    return ClassChainHas(ClassOf(obj), want);
}

int32_t U::PropOffset(void* cls, const char* name) {
    if (!cls || !name || !*name) return -1;
    std::pair<const void*, std::string> key{cls, name};
    {
        Guard g(g_cacheLock);
        auto it = g_propCache.find(key);
        if (it != g_propCache.end()) return it->second;
    }
    int32_t found = -1;
    void* c = cls;
    for (int i = 0; c && i < 64 && found < 0; i++) {
        for (const auto& p : Pool::PropsOf(c)) {
            if (p.name == name) {
                found = p.offset;
                break;
            }
        }
        c = Reflect::SuperStruct(c);
    }
    Guard g(g_cacheLock);
    g_propCache[key] = found;
    if (found < 0) g_misses++;
    return found;
}

int32_t U::PropOffsetOf(const void* obj, const char* name) { return PropOffset(ClassOf(obj), name); }

std::pair<const char*, int32_t> U::FirstProp(void* cls, const char* const* names, size_t count) {
    for (size_t i = 0; i < count; i++) {
        int32_t off = PropOffset(cls, names[i]);
        if (off >= 0) return {names[i], off};
    }
    return {nullptr, -1};
}

std::string U::ReadFString(const void* obj, uint32_t off, size_t maxChars) {
    struct Raw {
        char16_t* data;
        int32_t num;
        int32_t max;
    } raw{nullptr, 0, 0};
    if (!ReadBytes(obj, off, &raw, sizeof raw)) return "";
    if (!raw.data || raw.num <= 1 || raw.num > (int32_t)maxChars + 1) return "";
    // `Num` counts the terminator.
    size_t chars = (size_t)raw.num - 1;
    std::vector<char16_t> buf(chars);
    if (!MemReadable(raw.data, chars * sizeof(char16_t))) return "";
    memcpy(buf.data(), raw.data, chars * sizeof(char16_t));
    return Reflect::Utf16To8(buf.data(), (int)chars);
}

std::string U::ReadFName(const void* obj, uint32_t off) {
    if (!Pool::F().ok) return "";
    return Pool::NameAt((void*)obj, off);
}

uint32_t U::StructPropertiesSize(const void* strct, bool* ok) {
    return ReadU32(strct, Reflect::Lay().structPropertiesSize, ok);
}

uint8_t U::StructMinAlignment(const void* strct) {
    uint8_t v = ReadU8(strct, Reflect::Lay().structMinAlignment);
    return v ? v : 8;
}

void U::CacheStats(uint64_t& entries, uint64_t& misses) {
    Guard g(g_cacheLock);
    entries = g_propCache.size();
    misses = g_misses;
}
