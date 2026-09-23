// Small, safe UObject graph readers on top of objpool.cpp's live FNamePool and class index.
//
// WHY THIS EXISTS (lane L2)
//
// `reflect.cpp` funnels everything through engine callables (`UObject::FindFunction`,
// `UStruct::FindPropertyByName`, `FName::ToString`) and NONE of them exist on this stripped build —
// lane L1b measured that. `objpool.cpp` replaced the *enumeration* half (walk GUObjectArray, decode
// FNames, index UClasses, list a UStruct's FField chain). What was still missing, and what every L2
// feature needs, is the *reading* half: "give me the offset of the UPROPERTY called
// `m_DatabaseAccountId` on this object's class, then read it".
//
// The rules this file keeps, which are the lane's non-negotiables:
//
//  1. **NO OFFSET IS A CONSTANT.** Every property offset comes from the class's own FField chain
//     (`Pool::PropsOf`), looked up by the property's decoded name, and cached per (UClass, name).
//     A name that is not on the class chain yields -1 and the caller reports a null field rather
//     than reading a guessed address. The only numbers here are the UObject/UStruct offsets that
//     L1b *decided* on the live process and that live in `Reflect::Lay()`.
//  2. **EVERY DEREFERENCE IS GUARDED.** Reads go through `MemReadable`, and an object pointer is
//     checked with `LooksLikeUObject` before its class is touched. On a build with no function
//     symbols a wrong pointer is not a wrong answer, it is a crash on the game thread.
//  3. **NOTHING HERE ALLOCATES ON THE HOT PATH.** `PropOffset` allocates while it is *learning* an
//     offset (it walks a property list), so callers on the game thread must have warmed it at init
//     or off-thread. `ReadPtr`/`ReadU64`/`ReadVec` do not allocate at all and are the only things a
//     detour calls.
#pragma once
#include "common.h"

namespace U {

// ---- raw, guarded reads (no allocation, safe on the game thread) --------------------------------
bool ReadBytes(const void* obj, uint32_t off, void* dst, size_t n);
void* ReadPtr(const void* obj, uint32_t off);
uint64_t ReadU64(const void* obj, uint32_t off, bool* ok = nullptr);
uint32_t ReadU32(const void* obj, uint32_t off, bool* ok = nullptr);
uint8_t ReadU8(const void* obj, uint32_t off, bool* ok = nullptr);

/// FVector is three doubles on this build. That is not assumed: `SceneComponent::RelativeLocation`
/// sits at 0x1C8 and `RelativeRotation` at 0x1E0 in the live FField chain, i.e. exactly 24 bytes
/// apart, which only holds for the 64-bit FVector3d of UE5.
struct Vec {
    double x = 0, y = 0, z = 0;
    bool Finite() const;
};
bool ReadVec(const void* obj, uint32_t off, Vec& out);

// ---- identity ----------------------------------------------------------------------------------
void* ClassOf(const void* obj);            // UObject::ClassPrivate, nullptr when unreadable
uint32_t NameIdx(const void* obj);         // the FName comparison index of obj's own name, 0 on failure
std::string NameOf(const void* obj);       // decoded object name
std::string ClassNameOf(const void* obj);  // decoded name of obj's UClass ("" when unknown)
/// True when `cls`'s Super chain contains `want`. Bounded at 64 links.
bool ClassChainHas(void* cls, void* want);
/// True when obj's class chain contains the UClass named `className` (looked up in the class index).
bool IsA(const void* obj, const char* className);

/// Resolves an `FWeakObjectPtr` stored at `obj + off`.
///
/// **This is not optional decoration.** Dune's death UFunctions carry the killer inside
/// `InstigatorInfo`, whose two members are `m_Actor` and `m_Controller` — both
/// `WeakObjectProperty`, i.e. `{int32 ObjectIndex; int32 ObjectSerialNumber}`, **not pointers**.
/// Reading those 8 bytes as a pointer yields an integer pair that is not an address, so a naive read
/// loses every killer (and, without the LooksLikeUObject guard, would dereference garbage on the game
/// thread). The index is looked up in `GUObjectArray`'s chunk table — the same walk `Obj::At` uses,
/// whose layout lane L1b proved from the inlined `FChunkedFixedUObjectArray::GetObjectPtr`.
///
/// `serialMatched` (optional) reports whether the serial number at `FUObjectItem + 0x10` equalled the
/// weak pointer's. That offset is the ONE stock-UE assumption here, so it is treated as a
/// corroboration to be counted rather than as a validity test to gate on: resolution succeeds on the
/// index plus `LooksLikeUObject`, and the serial agreement is reported so it can be believed (or not)
/// from real data.
void* ResolveWeak(const void* obj, uint32_t off, bool* serialMatched = nullptr);

// ---- properties --------------------------------------------------------------------------------
/// Byte offset of the UPROPERTY `name` on `cls` or any of its supers, or -1.
/// Cached per (cls, name); the miss path walks the FField chain and allocates, so warm it off the
/// game thread. Thread-safe.
int32_t PropOffset(void* cls, const char* name);
/// Same, keyed by an object (uses its class). -1 when the object or its class is unreadable.
int32_t PropOffsetOf(const void* obj, const char* name);
/// The first of `names` that exists on the class, with its offset; {nullptr,-1} when none does.
std::pair<const char*, int32_t> FirstProp(void* cls, const char* const* names, size_t count);

/// Reads an FString UPROPERTY (`{char16_t* Data; int32 Num; int32 Max}`), UTF-8 encoded.
/// "" on any failure; never reads more than `maxChars` code units.
std::string ReadFString(const void* obj, uint32_t off, size_t maxChars = 256);
/// Reads an FName UPROPERTY and decodes it through the located pool. "" on failure.
std::string ReadFName(const void* obj, uint32_t off);

/// `UStruct::PropertiesSize` — the size of a UFunction's parameter frame.
uint32_t StructPropertiesSize(const void* strct, bool* ok = nullptr);
uint8_t StructMinAlignment(const void* strct);

/// How many (UClass, name) pairs have been learned, and how many of those were misses. Reported in
/// /health so a systematically wrong property name shows up as a miss count instead of silence.
void CacheStats(uint64_t& entries, uint64_t& misses);

}  // namespace U
