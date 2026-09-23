// UE object model for the Dune server build.
//
// ⚠️ THE UE VERSION IS UNCONFIRMED. The binary embeds no `++UE5…` version string (only the *format*
// string `"Unreal Engine version: %s"`), so the version exists only in the boot log. The offline
// bound is UE 5.2/5.3 (IoStore toc v5, which 5.4 bumps to 6). Therefore **no offset here is a
// version-keyed constant**: each is a hypothesis that Validate() re-proves against the live process,
// and a failed validation degrades the affected capability instead of being trusted.
//
// What the offline dissection of `ProcessEvent` and `AGameModeBase::PreLogin` DID corroborate on
// this exact binary (disassembly citations in research/2026-09-21-binary-dissection.md):
//   * `UStruct::PropertiesSize` @ **0x58** and `MinAlignment` @ **0x5C** — ProcessEvent allocas the
//     parameter frame with `movsxd rdx,[r14+0x58]` / `movsxd r13,[r14+0x5c]`, which is verbatim UE's
//     `FMemory_Alloca_Aligned(Function->PropertiesSize, Function->GetMinAlignment())`.
//   * `UFunction::FunctionFlags` @ **0xB0** — the flag tests are `test byte [rsi+0xb1],4`
//     (0x400 = FUNC_Native), `test byte [r14+0xb1],0x80` (0x8000) and `test byte [r14+0xb2],0x40`
//     (0x400000 = FUNC_HasOutParms).
//   * `UFunction::RepOffset` @ **0xB8** (compared against 0xFFFF).
//   * `AGameModeBase::GameSession` @ **0x3A0** — PreLogin loads it and calls ApproveLogin on it.
//   * `UObjectBase::InternalIndex` @ **0x0C**, `NamePrivate` @ **0x18**, `OuterPrivate` @ **0x20**
//     — from the chunked-object-array walk and a `cmp qword [rbx+0x18], r12` that compares the whole
//     FName as one 64-bit unit (which is also weak evidence that `FName` is 8 bytes here, i.e.
//     `UE_FNAME_OUTLINE_NUMBER` is off — LIKELY, must be confirmed at M0).
// Everything NOT in that list is marked below as unvalidated and must survive Validate().
//
// Property offsets of game structs are never constants: they come from UStruct::FindPropertyByName,
// or — since this build exports no such function — from our own walk of UStruct::ChildProperties.
#pragma once
#include "common.h"

namespace UE {

struct FName {
    uint32_t Comparison = 0;
    uint32_t Number = 0;
    bool operator==(const FName& o) const { return Comparison == o.Comparison && Number == o.Number; }
    bool operator!=(const FName& o) const { return !(*this == o); }
};
// ⚠️ NOT proven on this build. The binary contains the literal "Dump all numbered FNames to a file
// (only when UE_FNAME_OUTLINE_NUMBER is set)", so the FName width is a build switch whose state we
// have not established. The 64-bit `cmp qword [obj+0x18], reg` that compares a whole NamePrivate is
// evidence for 8 bytes, but it is evidence, not proof. Validate() must confirm it before any name is
// read, and the `names` capability stays degraded until it does.
static_assert(sizeof(FName) == 8, "FName is assumed 8 bytes (UE_FNAME_OUTLINE_NUMBER off) - boot-validated");

struct FString {
    char16_t* Data = nullptr;
    int32_t Num = 0;
    int32_t Max = 0;
};

template <typename T>
struct TArray {
    T* Data = nullptr;
    int32_t Num = 0;
    int32_t Max = 0;
};

struct FTopLevelAssetPath {
    FName PackageName;
    FName AssetName;
};

using UObjectPtr = void*;

}  // namespace UE

// ---- FUObjectArray: the layout needed to enumerate live objects ourselves -----------------------
//
// There is no `GetObjectsOfClass` symbol on this build, so enumeration is our own walk of
// `GUObjectArray`. Every constant here was PROVEN from the disassembly of the inlined
// `FChunkedFixedUObjectArray::GetObjectPtr` on THIS binary:
//
//     mov   eax, [rbx+0x0c]      ; UObjectBase::InternalIndex
//     cmp   eax, [GUObjectArray+0x24]   ; ObjObjects.NumElements
//     movzx ecx, ax              ; Offset = Index & 0xFFFF      -> 65536 elements per chunk
//     mov   rdx, [GUObjectArray+0x10]   ; ObjObjects.Objects (the chunk table)
//     shr   eax, 0x10            ; Chunk = Index >> 16
//     lea   rcx, [rcx+rcx*2]     ; *3
//     shl   ecx, 0x03            ; *8   -> sizeof(FUObjectItem) == 24
//     add   rcx, [rdx+rax*8]     ; + chunk base
//     mov   eax, [rcx+8]         ; FUObjectItem::Flags
//
// The fields NOT in that trace (ObjFirstGCIndex, MaxElements, NumChunks, …) are stock-UE-5.2
// assumptions and are deliberately absent here: nothing needs them, so nothing may assume them.
namespace UObjArray {
constexpr uint32_t kObjects = 0x10;       // FUObjectItem** chunk table          PROVEN
constexpr uint32_t kNumElements = 0x24;   // int32                                PROVEN
constexpr uint32_t kElementsPerChunk = 65536;                                  // PROVEN
constexpr uint32_t kItemSize = 24;        // sizeof(FUObjectItem)                 PROVEN
constexpr uint32_t kItemObject = 0x00;    // UObject*                             PROVEN
constexpr uint32_t kItemFlags = 0x08;     // int32 internal flags                 PROVEN
}  // namespace UObjArray

namespace Reflect {

// Offsets, either the UE 5.6 default or the value discovered at boot.
struct Layout {
    // `V` marks an offset corroborated by disassembly of THIS binary; the rest are hypotheses that
    // Validate() must re-prove on the live process before anything reads through them.
    uint32_t objFlags = 0x08;
    uint32_t objIndex = 0x0C;              // V
    uint32_t objClass = 0x10;
    uint32_t objName = 0x18;               // V
    uint32_t objOuter = 0x20;              // V
    uint32_t structSuper = 0x40;
    uint32_t structChildProperties = 0x50;  // FField* chain (UPROPERTYs)
    uint32_t structChildren = 0x48;        // UField* chain (UFunctions); discovered live
    uint32_t structPropertiesSize = 0x58;  // V
    uint32_t structMinAlignment = 0x5C;    // V
    // ⚠️ UE 5.2 puts this at 0x118, not the 0x110 the UE 5.6 tree this file came from used. Both are
    // hypotheses here; Validate() decides.
    uint32_t classDefaultObject = 0x118;
    uint32_t funcFunctionFlags = 0xB0;     // V
    uint32_t funcRepOffset = 0xB8;         // V
    uint32_t funcFunc = 0xD8;
    uint32_t fieldNext = 0x20;
    uint32_t fieldName = 0x28;
    uint32_t fieldClass = 0x08;  // FFieldClass*, whose FName is at +0
    uint32_t propOffsetInternal = 0x44;
    // FProperty::ElementSize. There is no DWARF on this build to check it against (the VEIN tree
    // took this from the depot's own debug info), so it is a hypothesis like the rest. Nothing reads
    // it yet; when something does, it must be validated first.
    uint32_t propElementSize = 0x30;
};
const Layout& Lay();
// Mutable access, for the boot-time discoverers that DECIDE an offset on the live process
// (objpool.cpp's DiscoverFieldLayout). Nothing else may write these.
Layout& LayMut();

// Resolves the function pointers. Cheap, no game calls. Safe on any thread.
void Init();
bool Ready();

// Runs the boot validations. MUST be called on the game thread. Idempotent; returns a JSON array of
// {check, ok, detail}. Sets the `reflect*` capabilities.
std::string Validate();
bool Validated();

// ---- string helpers ----
std::string Utf16To8(const char16_t* s, int len);
std::vector<char16_t> Utf8To16(const std::string& s);  // NUL-terminated
std::string ToStd(UE::FString& s, bool freeIt);        // consumes (and optionally frees) an FString
std::string NameToString(const UE::FName& n);
UE::FName MakeName(const std::string& s, bool add = false);  // FNAME_Find by default

// ---- object helpers (game thread only unless noted) ----
void* ObjClass(void* obj);
void* ObjOuter(void* obj);
std::string ObjName(void* obj);
std::string ObjPathName(void* obj);
std::string ClassName(void* obj);          // name of obj's class
void* SuperStruct(void* structPtr);
void* ClassDefaultObject(void* cls);
bool IsA(void* obj, void* cls);            // walks the class chain
void* FindFunction(void* obj, const std::string& name);
void* FunctionNative(void* func);          // UFunction::Func
void* FindProperty(void* structPtr, const std::string& name);
int32_t PropertyOffset(void* prop);
std::string PropertyTypeName(void* prop);

// UClass* for a class named by its exported vtable symbol ("_ZTV14ADuneCharacter").
//
// This replaces the VEIN tree's `StaticClass(symName)`: there are no `X::StaticClass` thunks to
// resolve on a stripped binary. Instead the class is identified the way this binary actually allows
// — by vtable pointer. The UClass* is found by walking GUObjectArray for the object whose own
// `ClassPrivate` chain terminates at the class whose CDO carries that vtable. nullptr when the
// vtable is not exported or GUObjectArray has not resolved.
void* ClassByVTable(const char* ztvName);
// StaticFindObject(nullptr, FTopLevelAssetPath{pkg, name}, false)
void* FindObjectByPath(const std::string& package, const std::string& name);

bool GetObjectsOfClass(void* cls, std::vector<void*>& out, bool includeDerived = true);
bool GetObjectsWithOuter(void* outer, std::vector<void*>& out, bool includeNested = true);

// ---- diagnostics ----
// JSON dump of an object's UPROPERTY tree, walking up the class chain.
std::string DumpObject(void* obj, int maxProps = 512);
// JSON dump of a UStruct/UClass found by name (package optional, "" = search a few known packages).
std::string DumpStruct(const std::string& name);
std::string LayoutJson();

// Cached build strings; filled by Validate() on the game thread, "" until then.
std::string GameBuild();
std::string EngineVersion();

}  // namespace Reflect
