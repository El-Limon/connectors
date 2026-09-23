#include "resolve.h"

#include "state.h"

#include <cxxabi.h>
#include <dlfcn.h>
#include <atomic>
#include <elf.h>
#include <fcntl.h>
#include <link.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cstring>
#include <unordered_map>
#include <unordered_set>

// ---------------------------------------------------------------------------------------------
// What we want, and how each thing can possibly be found on a stripped, RTTI-only binary.
//
// Three kinds of want:
//
//   VT(key)                  an exported `_ZTV…` vtable. `key` IS the mangled symbol. This is the
//                            bedrock: it resolves by dlsym, it cannot be wrong, and it gives us
//                            both hook targets and object identification.
//   SLOT(key, ztv, slot)     a virtual function, read out of a LIVE vtable at a fixed slot index.
//                            The index comes from the offline dissection
//                            (context/games/dune/research/2026-09-21-binary-dissection.md) and is
//                            re-validated at boot: the word must be a plausible code address, and
//                            for an override it must differ from the base class's word.
//   PESLOT(key, ztv)         the same, but at the *derived* ProcessEvent slot (see
//                            DeriveProcessEventSlot). Resolves only once that derivation succeeded.
//   GLOBAL(key, pat, xref, ripOff)
//                            a data global with no symbol and no vtable, found by a byte pattern
//                            that must match EXACTLY ONCE in .text and whose enclosing function
//                            must reference the UTF-16LE literal `xref`; `ripOff` is the offset of
//                            the disp32 inside the match, decoded RIP-relatively into the address.
//   CODE(key, pat, xref)     a function found by an exactly-once byte pattern (no vtable route).
//
// `required` names are the ones M0 insists on. Everything else degrades exactly one capability.
//
// ⚠️ SLOT INDICES ARE NOT FILLED IN YET where the offline dissection could not settle them; those
// entries carry kSlotUnknown and resolve to nothing, which shows up in /health as a degraded
// capability with the reason. That is deliberate: a guessed slot index on a 42 942-vtable binary is
// a crash, not an optimisation.
namespace {

const int kSlotUnknown = -1;
const int kSlotProcessEvent = -2;  // resolved from the derived ProcessEvent slot

enum WantKind { KVTable, KSlot, KGlobal, KCode, KDynSym };

struct Want {
    const char* key;
    WantKind kind;
    const char* ztv;      // KSlot: the vtable symbol the slot is read from
    int slot;             // KSlot: vtable index, kSlotUnknown, or kSlotProcessEvent
    bool required;
    bool data;            // data symbol (no .text plausibility check)
    const char* pattern;  // KGlobal/KCode: byte signature, "48 8B ?? E8 ?? ?? ?? ??"
    const char* xref;     // KGlobal/KCode: UTF-16LE literal the enclosing function must reference
    int ripOff;           // KGlobal: offset of the disp32 inside the match, -1 when unused
    int adjust;           // added to a code match address (e.g. to step back over a prologue)
};

#define VT(k)                     {k, KVTable, nullptr, kSlotUnknown, false, true,  nullptr, nullptr, -1, 0}
#define VTREQ(k)                  {k, KVTable, nullptr, kSlotUnknown, true,  true,  nullptr, nullptr, -1, 0}
#define SLOT(k, z, s, req)        {k, KSlot,   z,       s,            req,   false, nullptr, nullptr, -1, 0}
#define PESLOT(k, z, req)         {k, KSlot,   z,       kSlotProcessEvent, req, false, nullptr, nullptr, -1, 0}
#define GLOBAL(k, pat, xr, ro)    {k, KGlobal, nullptr, kSlotUnknown, false, true,  pat,     xr,      ro, 0}
#define CODE(k, pat, xr)          {k, KCode,   nullptr, kSlotUnknown, false, false, pat,     xr,      -1, 0}
#define DYN(k, req)               {k, KDynSym, nullptr, kSlotUnknown, req,   false, nullptr, nullptr, -1, 0}

// Slot indices, every one of them derived from THIS binary (offline dissection
// 2026-09-21-binary-dissection.md) and never from stock UE headers — the UE version is still
// unconfirmed, so a version-keyed table would be a guess. Each is re-validated at boot.
const size_t kSlotProcessEventOffline = 86;   // _ZTV7UObject[86]; [87]=GetFunctionCallspace, [88]=CallRemoteFunction
const int kSlotPreLogin = 310;                // AGameModeBase own virtuals start at 283
const int kSlotLogin = 311;
const int kSlotPostLogin = 313;
const int kSlotLogout = 315;
const int kSlotTakeDamage = 250;              // AActor[250]; APawn overrides it and every Dune pawn inherits that
const int kSlotEngineExec = 107;              // UEngine[107]; UGameEngine overrides it
const int kSlotCritterOnDeath = 367;          // ADuneCritterBase[367] reads the `BPOnDeath` FName
const int kSlotCritterDeathOuter = 409;       // calls [367]
const int kSlotPlayerCharDeathBroadcast = 240;// ADunePlayerCharacter[240] -> MulticastHandleDeath

const Want kWanted[] = {
    // --- anchors used by the boot cross-checks -------------------------------------------------
    // These three are the only *function* names that survive stripping (they are linker-generated
    // and land in .dynsym), and they are what proves the loadBase + slide arithmetic before any
    // game address is trusted.
    DYN("_init", false),
    DYN("_fini", false),
    DYN("_start", false),

    // --- the class vtables (strategy `dynsym-vtable`) -------------------------------------------
    // Engine base classes. `_ZTV7UObject` is REQUIRED: without it there is no ProcessEvent slot,
    // no object identification and therefore no plugin at all.
    // Slot counts in the comments are this build's `st_size / 8 - 2`, recorded so a game update that
    // changes them shows up as a diff rather than as mysterious behaviour.
    VTREQ("_ZTV7UObject"),                  // 97 slots
    VTREQ("_ZTV6AActor"),                   // 283
    VT("_ZTV5AInfo"),                       // 283 (adds no virtuals over AActor)
    VT("_ZTV5APawn"),                       // 346
    VT("_ZTV10ACharacter"),                 // 384
    VT("_ZTV11AController"),                // 350
    VT("_ZTV17APlayerController"),          // 601
    VT("_ZTV12APlayerState"),               // 310
    VT("_ZTV13AGameModeBase"),              // 349
    VT("_ZTV9AGameMode"),                   // 376
    VT("_ZTV14AGameStateBase"),             // 305
    VT("_ZTV12AGameSession"),               // 318
    VT("_ZTV6UWorld"),                      // 109
    VT("_ZTV7UEngine"),                     // 201
    VT("_ZTV11UGameEngine"),                // 201
    // The CONCRETE engine class on this build; `GEngine` points at one of these.
    VT("_ZTV15UDuneGameEngine"),
    VT("_ZTV13UGameInstance"),              // 156
    VT("_ZTV14UNetConnection"),             // 147
    VT("_ZTV11UDamageType"),                // 99

    // Dune classes. Every mangled name below was read out of this build's `.dynsym`, not guessed:
    // the length prefix is part of the symbol, so `ADuneGameModeBase` is `_ZTV17…` and
    // `UChatSubsystem` is `_ZTV14…`. Each is optional; a missing class degrades exactly the
    // capability that needed it and /health names it.
    VT("_ZTV14ADuneCharacter"),             // 802
    VT("_ZTV20ADunePlayerCharacter"),       // 1027
    VT("_ZTV17ADuneNpcCharacter"),          // 898
    VT("_ZTV21ADunePlayerController"),      // 756
    VT("_ZTV25ADunePlayerControllerBase"),  // 674
    VT("_ZTV16ADunePlayerState"),           // 328
    VT("_ZTV20ADunePlayerStateBase"),       // 310
    VT("_ZTV17ADuneGameModeBase"),          // 349
    VT("_ZTV24ADuneSandboxGameModeBase"),   // 364 — the live game mode is most likely THIS one
    VT("_ZTV14ADuneGameState"),             // 305
    VT("_ZTV18ADuneGameStateBase"),         // 305
    VT("_ZTV16ADuneGameSession"),           // 325
    VT("_ZTV16ADuneCritterBase"),           // 461
    VT("_ZTV13ASandwormPawn"),              // 357
    VT("_ZTV12ADuneVehicle"),               // 682
    VT("_ZTV11ADuneTurret"),                // 359
    VT("_ZTV16ADuneOrnithopter"),           // 690
    VT("_ZTV27UDuneServerCommandSubsystem"),// 111
    VT("_ZTV31UDuneServerCommandsCheatManager"),
    VT("_ZTV14UChatSubsystem"),             // 102
    VT("_ZTV15UDuneDamageType"),            // 99
    VT("_ZTV20UDuneDamageExecution"),       // 101
    VT("_ZTV20ADuneDamageableDummy"),       // 285

    // --- virtual functions, by slot (strategy `rtti-slot`) --------------------------------------
    // ProcessEvent. Slot 86, established offline by four converging pieces of evidence, the
    // strongest being that slot 86 issues `call [this_vtable + 0x2b8]` and then conditionally
    // `call [this_vtable + 0x2c0]` — i.e. slots 87 and 88 — which is verbatim UE's
    // `Callspace = GetFunctionCallspace(...); if (Callspace & Remote) CallRemoteFunction(...)`.
    // That triplet is re-checked at boot (see DeriveProcessEventSlot).
    //
    // ⚠️ `AActor` DOES override ProcessEvent on this build (0x12913180, reached by AInfo, APawn,
    // ACharacter, APlayerController, AGameModeBase …), and the UActorComponent family has its own
    // override. So a class-level hook on `_ZTV7UObject` alone would miss every actor — which is
    // exactly why each hookable class is declared separately here.
    PESLOT("UObject::ProcessEvent", "_ZTV7UObject", true),
    PESLOT("AActor::ProcessEvent", "_ZTV6AActor", false),
    PESLOT("ADuneCharacter::ProcessEvent", "_ZTV14ADuneCharacter", false),
    PESLOT("ADunePlayerCharacter::ProcessEvent", "_ZTV20ADunePlayerCharacter", false),
    PESLOT("ADuneNpcCharacter::ProcessEvent", "_ZTV17ADuneNpcCharacter", false),
    PESLOT("ADuneCritterBase::ProcessEvent", "_ZTV16ADuneCritterBase", false),
    PESLOT("ADuneSandboxGameModeBase::ProcessEvent", "_ZTV24ADuneSandboxGameModeBase", false),

    // Engine tick — the game-thread pump's anchor. gamethread.cpp re-reads the tick slot from the
    // LIVE engine object's own vtable (the live GEngine may be a Dune subclass of UGameEngine that
    // this table does not name); these entries exist as the cross-check and the fallback.
    SLOT("UEngine::Tick", "_ZTV7UEngine", kSlotUnknown, false),
    SLOT("UGameEngine::Tick", "_ZTV11UGameEngine", kSlotUnknown, false),
    SLOT("UDuneGameEngine::Tick", "_ZTV15UDuneGameEngine", kSlotUnknown, false),

    // Player lifecycle — precise connect/disconnect (plan capability (c)).
    // AGameModeBase's own virtuals begin at slot 283 (UObject 97 -> AActor 283 -> AInfo 283, which
    // adds none -> AGameModeBase 349). PreLogin/Login/PostLogin/Logout were identified by the
    // UFunction FName globals their bodies read (`K2_PostLogin`, `HandleStartingNewPlayer`,
    // `MustSpectate` for PostLogin; `K2_OnLogout` for Logout) and by PreLogin's
    // `ErrorMessage = GameSession->ApproveLogin(Options)` shape.
    SLOT("AGameModeBase::PreLogin", "_ZTV13AGameModeBase", kSlotPreLogin, false),
    SLOT("AGameModeBase::Login", "_ZTV13AGameModeBase", kSlotLogin, false),
    SLOT("AGameModeBase::PostLogin", "_ZTV13AGameModeBase", kSlotPostLogin, false),
    SLOT("AGameModeBase::Logout", "_ZTV13AGameModeBase", kSlotLogout, false),
    // The Dune overrides at the SAME indices. Both Dune game-mode classes share the same override
    // addresses for 313/315, so hooking either binds the same code — but the hook must still go on
    // the LIVE object's vtable, because which of ADuneSandboxGameModeBase /
    // ADuneSandboxNPEGameModeBase / ADuneSandboxChallengeRoomGameModeBase is actually in use is an
    // open question for M0.
    SLOT("ADuneGameModeBase::PostLogin", "_ZTV17ADuneGameModeBase", kSlotPostLogin, false),
    SLOT("ADuneGameModeBase::Logout", "_ZTV17ADuneGameModeBase", kSlotLogout, false),
    SLOT("ADuneSandboxGameModeBase::PostLogin", "_ZTV24ADuneSandboxGameModeBase", kSlotPostLogin, false),
    SLOT("ADuneSandboxGameModeBase::Logout", "_ZTV24ADuneSandboxGameModeBase", kSlotLogout, false),
    SLOT("ADuneSandboxGameModeBase::PreLogin", "_ZTV24ADuneSandboxGameModeBase", kSlotPreLogin, false),

    // Death / kill attribution (plan capability (b)).
    //
    // ⚠️ THE HONEST STATE OF THIS: Dune's health is a **GAS attribute set**
    // (UDuneCharacterAttributeSet / UDuneDamageMitigationAttributeSet); there is no
    // `*HealthComponent*` vtable anywhere in the binary, so the VEIN "one health component, one
    // death event" shape does not exist here. Three candidate chokepoints were found offline and
    // NONE of them is yet proven to catch every NPC death:
    //   1. The central death/kill handler at image-relative 0xdea4260 (8816 B) calls BOTH the
    //      `MulticastHandleDeath` and `MulticastHandleKill` wrappers. Best coverage — but it is in
    //      NO vtable and has no direct callers (its address is `lea`'d exactly once), so a vtable
    //      swap cannot reach it. It would need an inline hook, which this plugin does not do.
    //   2. ADuneCritterBase[367] (reads the `BPOnDeath` FName) and ADunePlayerCharacter[240]
    //      (broadcasts MulticastHandleDeath) ARE virtuals and ARE swappable — but per class, so
    //      every Dune pawn class needs its own swap and a late-loaded Blueprint subclass needs the
    //      Housekeep() re-sweep.
    //   3. AActor::TakeDamage (slot 250) is a single uniform hook, but it observes *damage*, and
    //      GAS damage very likely bypasses it entirely.
    // So these are declared, and lane L2 measures which of them actually fires. /health reports
    // `entity-killed` as degraded until one of them has demonstrably fired.
    SLOT("AActor::TakeDamage", "_ZTV6AActor", kSlotTakeDamage, false),
    SLOT("APawn::TakeDamage", "_ZTV5APawn", kSlotTakeDamage, false),
    SLOT("ADuneCharacter::TakeDamage", "_ZTV14ADuneCharacter", kSlotTakeDamage, false),
    SLOT("ADuneCritterBase::TakeDamage", "_ZTV16ADuneCritterBase", kSlotTakeDamage, false),
    SLOT("ADuneDamageableDummy::TakeDamage", "_ZTV20ADuneDamageableDummy", kSlotTakeDamage, false),
    SLOT("ADuneCritterBase::OnDeath", "_ZTV16ADuneCritterBase", kSlotCritterOnDeath, false),
    SLOT("ADuneCritterBase::DeathOuter", "_ZTV16ADuneCritterBase", kSlotCritterDeathOuter, false),
    SLOT("ADunePlayerCharacter::BroadcastDeath", "_ZTV20ADunePlayerCharacter",
         kSlotPlayerCharDeathBroadcast, false),

    // Optional `raw` console exec (plan capability (e)). Slot 107, identified offline as the only
    // function that RIP-relative-`lea`s both the UTF-16LE literals "DUMPTICKS" and "FLUSHLOG" —
    // UE's `UEngine::Exec` FParse::Command chain. UGameEngine overrides it; the plugin must read
    // the slot off the LIVE GEngine's vptr rather than assume which class it is.
    SLOT("UEngine::Exec", "_ZTV7UEngine", kSlotEngineExec, false),
    SLOT("UGameEngine::Exec", "_ZTV11UGameEngine", kSlotEngineExec, false),
    SLOT("UDuneGameEngine::Exec", "_ZTV15UDuneGameEngine", kSlotEngineExec, false),

    // --- globals with neither a symbol nor a vtable (strategy `string-xref`) ---------------------
    //
    // ⚠️ NOTE ON THE `xref` ANCHOR: it is deliberately nullptr for all of these. The offline work
    // established that **direct string xrefs are only ~69% reliable on this binary**: UE 5.2's
    // structured logging holds `UE_LOG` format strings in static `FStaticBasicLogRecord` structs in
    // `.data.rel.ro`, wired up by `R_X86_64_RELATIVE` relocations, so the code `lea`s the *record*,
    // never the string. Declaring a string anchor these patterns cannot satisfy would make the
    // resolver reject its own correct answers. The anchoring happened OFFLINE, through the log
    // records (each of which had exactly one referencing site — a sharper tool than a direct xref);
    // what survives into the plugin is the resulting byte pattern plus the exactly-once rule and the
    // "resolved address must be readable data outside .text" range check.
    //
    // GEngine. Anchor: `FEngineLoop::Init()`, found through the `FStaticBasicLogRecord` for
    // "Failed to load UnrealEd Engine class '%s'." and confirmed 0x60 bytes later by the
    // SCOPED_BOOT_TIMING literal "GEngine->ParseCommandline()". The match is the store itself:
    //   lea r13, [rip+disp]      <- &GEngine          (4C 8D 2D, disp32 at +3)
    //   mov qword [r13], r14     <- GEngine = NewObject<UEngine>(...)
    // On this build it resolves to image-relative 0x17629448.
    GLOBAL("GEngine", "4C 8D 2D ?? ?? ?? ?? 4D 89 75 00 48 8B 7D C0 48 85 FF 74 05", nullptr, 3),

    // GWorld: NOT resolved and deliberately not attempted. No `GWorld` literal exists in either
    // encoding, and statistical ranking of `.bss` globals is worthless here (third-party C libraries,
    // SQLite above all, dominate every load/store frequency ranking and produce very convincing false
    // positives). The plugin does not need it: on a dedicated server there is exactly one world, so
    // the route is GEngine -> GameInstance -> GetWorld() / GEngine->WorldContexts.
    GLOBAL("GWorld", nullptr, nullptr, -1),

    // GUObjectArray, resolved TWO independent ways so that a disagreement is detectable.
    //   `GUObjectArray`          the base, from UObjectBaseInit's tail call to
    //                            FUObjectArray::AllocateObjectPool (anchored offline on the
    //                            "Disabling permanent object pool ... gc.MaxObjectsNotConsideredByGC"
    //                            and "Max UObject count is invalid" log records).
    //   `GUObjectArray.Objects`  the `ObjObjects.Objects` chunk table at base+0x10, from the inlined
    //                            FChunkedFixedUObjectArray::GetObjectPtr.
    // SelfChecks() requires `GUObjectArray.Objects == GUObjectArray + 0x10`; if the two disagree the
    // enumeration capability is degraded rather than used.
    GLOBAL("GUObjectArray", "8B 75 D8 8B 55 DC 0F B6 4D E7 48 8D 3D ?? ?? ?? ?? E8", nullptr, 13),
    GLOBAL("GUObjectArray.Objects",
           "8B 43 0C 3B 05 ?? ?? ?? ?? 7D ?? 0F B7 C8 48 8B 15 ?? ?? ?? ?? C1 E8 10 48 8D 0C 49 C1 E1 03",
           nullptr, 17),

    // FNamePool: UNRESOLVED, and honestly so. The build definitely uses the UE 4.23+ FNamePool (the
    // literals "GNameBlocksDebug Valid - %i", "%d ansi FNames", "%d wide FNames" are all present),
    // but its address was not found offline. Two things make the published offset tables useless
    // here: they are Windows-derived, and on Linux `FRWLock` wraps a 56-byte `pthread_rwlock_t`
    // rather than the 8-byte Windows SRWLOCK, which shifts every field after it. Worse, the literal
    // "Dump all numbered FNames to a file (only when UE_FNAME_OUTLINE_NUMBER is set)" means the
    // `FName` size itself (8 bytes vs 4) is a build switch whose state we have not proven.
    // -> The plugin must NOT walk the pool on assumed offsets. The boot-time route is the honest
    //    one: take a live UObject from GUObjectArray, read NamePrivate at +0x18, and confirm the
    //    pool base and the FName width against it before anything reads a name.
    GLOBAL("FNamePool", nullptr, nullptr, -1),

    // --- functions with neither a symbol nor a vtable (strategy `string-xref`) -------------------
    // All optional. The plugin is written so that each of these has a symbol-free alternative:
    //   FName::ToString      -> read NamePrivate and resolve through the pool, once the pool and the
    //                           FName width have been confirmed at boot (see FNamePool above)
    //   GetObjectsOfClass    -> walk GUObjectArray ourselves; the layout needed for that IS proven:
    //                           ObjObjects.Objects @ base+0x10, NumElements @ base+0x24,
    //                           NumElementsPerChunk = 65536, sizeof(FUObjectItem) = 24,
    //                           FUObjectItem::Object @ +0, FUObjectItem::Flags @ +8
    //   StaticFindObject     -> not needed; objects are found by class-vtable comparison
    // They are declared so that /health *reports* whether a direct call is available, never so that
    // a capability silently depends on one.
    CODE("FName::ToString", nullptr, nullptr),
    CODE("FName::AppendString", nullptr, nullptr),
    CODE("GetObjectsOfClass", nullptr, nullptr),
    CODE("StaticFindObject", nullptr, nullptr),
    CODE("UStruct::FindPropertyByName", nullptr, nullptr),
    CODE("UObject::FindFunction", nullptr, nullptr),
    CODE("FMemory::Free", nullptr, nullptr),
    CODE("RequestEngineExit", nullptr, nullptr),
};
#undef VT
#undef VTREQ
#undef SLOT
#undef PESLOT
#undef GLOBAL
#undef CODE
#undef DYN

const size_t kWantedCount = sizeof(kWanted) / sizeof(kWanted[0]);

// Which L1-owned capability a wanted name backs. Everything else is diagnostic only. The names are
// namespaced under `symbols.` so lanes L2 and the sidecar can own the connector-facing names.
struct CapMap {
    const char* want;
    const char* capability;
};
const CapMap kCapabilities[] = {
    // Without the UObject vtable and the ProcessEvent slot there is no object model at all.
    {"_ZTV7UObject", "symbols.objectModel"},
    {"UObject::ProcessEvent", "symbols.objectModel"},
    // Object identification: the RTTI route. `_ZTV6AActor` proves the vtable map is usable for
    // "is this an actor" without any reflection.
    {"_ZTV6AActor", "symbols.classIdentity"},
    // Enumeration of live objects (the `/entities` class list and the NPC sweep).
    {"GUObjectArray", "symbols.enumeration"},
    {"GUObjectArray.Objects", "symbols.enumeration"},
    // FName -> text. Either the pool global (preferred: no call into the game) or a callable.
    {"FNamePool", "symbols.names"},
    // The engine object, and therefore the world, the game mode and the player list.
    {"GEngine", "symbols.engine"},
    // Player lifecycle hooks.
    {"_ZTV24ADuneSandboxGameModeBase", "symbols.playerLifecycle"},
    {"AGameModeBase::PostLogin", "symbols.playerLifecycle"},
    // Death / kill attribution.
    {"_ZTV14ADuneCharacter", "symbols.deathAttribution"},
    {"_ZTV16ADuneCritterBase", "symbols.deathAttribution"},
    // Optional raw console exec.
    {"UEngine::Exec", "symbols.console"},
};
const size_t kCapabilityCount = sizeof(kCapabilities) / sizeof(kCapabilities[0]);

std::vector<SymEntry> g_entries;
std::unordered_map<std::string, size_t> g_index;
bool g_ready = false;
bool g_cacheHit = false;
uint64_t g_scanMs = 0;
size_t g_processEventSlot = SIZE_MAX;
std::string g_processEventSlotHow = "unresolved";
std::atomic<bool> g_processEventConfirmed{false};
size_t g_resolved = 0, g_requiredWanted = 0, g_requiredResolved = 0;

// Per-strategy bookkeeping for /health. The chain order here IS the resolution order.
struct StrategyStat {
    const char* name;
    bool available = false;
    std::string detail;
    size_t resolved = 0;
    uint64_t ms = 0;
};
StrategyStat g_strategies[] = {
    // The only exact, cheap, guaranteed source on this build: ~42 942 exported `_ZTV*` symbols.
    {"dynsym-vtable", false, "not run", 0, 0},
    // Function addresses read out of those vtables at a slot index (live memory only).
    {"rtti-slot", false, "not run", 0, 0},
    // Byte patterns anchored on UTF-16LE string literals, accepted only when unique.
    {"string-xref", false, "not run", 0, 0},
    // Plain `.dynsym` name lookup. On this binary it only ever answers for _init/_fini/_start and
    // the statically linked third-party C libraries — it is kept because it costs nothing and it is
    // what proves the address arithmetic.
    {"dynsym", false, "not run", 0, 0},
};
const size_t kStrategyCount = sizeof(g_strategies) / sizeof(g_strategies[0]);

}  // namespace

// ---------------------------------------------------------------------------------------------
// ELF

namespace {
struct MappedFile {
    const uint8_t* p = nullptr;
    size_t len = 0;
    int fd = -1;
    bool open(const std::string& path) {
        fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) return false;
        struct stat st;
        if (fstat(fd, &st) != 0 || st.st_size <= 0) { ::close(fd); fd = -1; return false; }
        len = (size_t)st.st_size;
        void* m = mmap(nullptr, len, PROT_READ, MAP_PRIVATE, fd, 0);
        if (m == MAP_FAILED) { ::close(fd); fd = -1; return false; }
        p = (const uint8_t*)m;
        return true;
    }
    void close() {
        if (p) munmap((void*)p, len);
        if (fd >= 0) ::close(fd);
        p = nullptr; fd = -1; len = 0;
    }
    ~MappedFile() { close(); }
};

// The runtime slide of the main executable. 0 for a non-PIE image (ET_EXEC).
uint64_t MainImageSlide() {
    // dl_iterate_phdr's first entry is always the main program.
    struct Ctx { uint64_t base = 0; bool got = false; } ctx;
    dl_iterate_phdr(
        [](struct dl_phdr_info* info, size_t, void* data) -> int {
            auto* c = (Ctx*)data;
            c->base = info->dlpi_addr;
            c->got = true;
            return 1;  // stop after the first (the main executable)
        },
        &ctx);
    return ctx.got ? ctx.base : 0;
}
}  // namespace

bool ResolveCore::ParseElfBuffer(const uint8_t* p, size_t len, ElfInfo& out) {
    if (len < sizeof(Elf64_Ehdr) || memcmp(p, ELFMAG, SELFMAG) != 0) { out.error = "not an ELF"; return false; }
    auto* eh = (const Elf64_Ehdr*)p;
    if (eh->e_ident[EI_CLASS] != ELFCLASS64) { out.error = "not ELF64"; return false; }
    out.pie = eh->e_type == ET_DYN;
    if (eh->e_phoff + (size_t)eh->e_phnum * eh->e_phentsize > len) { out.error = "bad phdr table"; return false; }
    bool haveBase = false;
    for (int i = 0; i < eh->e_phnum; i++) {
        auto* ph = (const Elf64_Phdr*)(p + eh->e_phoff + (size_t)i * eh->e_phentsize);
        if (ph->p_type != PT_LOAD) continue;
        if (!haveBase) { out.loadBase = ph->p_vaddr; haveBase = true; }
        if (ph->p_flags & PF_X) out.execRanges.push_back({ph->p_vaddr, ph->p_vaddr + ph->p_memsz});
        // Read-only, non-executable: this is where the UTF-16LE string literals live. `.rodata`
        // alone is not enough on this build, the literals are spread over several such segments.
        else if ((ph->p_flags & PF_R) && !(ph->p_flags & PF_W))
            out.rodataRanges.push_back({ph->p_vaddr, ph->p_vaddr + ph->p_filesz});
    }
    if (eh->e_shoff == 0 || eh->e_shoff + (size_t)eh->e_shnum * eh->e_shentsize > len) {
        out.error = "no section headers";
        return haveBase;  // still usable, just without the cross-check anchors
    }
    auto sec = [&](int i) { return (const Elf64_Shdr*)(p + eh->e_shoff + (size_t)i * eh->e_shentsize); };
    if (eh->e_shstrndx >= eh->e_shnum) { out.error = "bad shstrndx"; return haveBase; }
    const Elf64_Shdr* shstr = sec(eh->e_shstrndx);
    if (shstr->sh_offset + shstr->sh_size > len) { out.error = "bad shstrtab"; return haveBase; }
    const char* names = (const char*)(p + shstr->sh_offset);
    for (int i = 0; i < eh->e_shnum; i++) {
        const Elf64_Shdr* s = sec(i);
        if (s->sh_name >= shstr->sh_size) continue;
        const char* n = names + s->sh_name;
        if (!strcmp(n, ".text")) { out.textAddr = s->sh_addr; out.textSize = s->sh_size; out.textOff = s->sh_offset; }
        else if (!strcmp(n, ".init")) out.initAddr = s->sh_addr;
        else if (!strcmp(n, ".fini")) out.finiAddr = s->sh_addr;
        else if (!strcmp(n, ".rodata")) { out.rodataAddr = s->sh_addr; out.rodataSize = s->sh_size; out.rodataOff = s->sh_offset; }
        else if (!strcmp(n, ".symtab")) { out.hasSymtab = true; out.symtabCount = s->sh_entsize ? s->sh_size / s->sh_entsize : 0; }
        else if (!strcmp(n, ".dynsym")) { out.hasDynsym = true; out.dynsymCount = s->sh_entsize ? s->sh_size / s->sh_entsize : 0; }
        else if (!strcmp(n, ".note.gnu.build-id") && s->sh_offset + s->sh_size <= len && s->sh_size >= sizeof(Elf64_Nhdr)) {
            auto* nh = (const Elf64_Nhdr*)(p + s->sh_offset);
            const uint8_t* desc = (const uint8_t*)(nh + 1) + ((nh->n_namesz + 3) & ~3u);
            static const char* hex = "0123456789abcdef";
            out.buildId.clear();
            for (uint32_t k = 0; k < nh->n_descsz && k < 64; k++) {
                out.buildId += hex[desc[k] >> 4];
                out.buildId += hex[desc[k] & 0xf];
            }
        }
    }
    out.ok = haveBase && out.textAddr != 0;
    if (!out.ok && out.error.empty()) out.error = "missing PT_LOAD or .text";
    return out.ok;
}

size_t ResolveCore::ForEachSymbol(const uint8_t* p, size_t len, bool dynamic,
                                  const std::function<void(const SymbolRec&)>& fn) {
    if (len < sizeof(Elf64_Ehdr) || memcmp(p, ELFMAG, SELFMAG) != 0) return 0;
    auto* eh = (const Elf64_Ehdr*)p;
    if (eh->e_shoff == 0 || eh->e_shoff + (size_t)eh->e_shnum * eh->e_shentsize > len) return 0;
    auto sec = [&](int i) { return (const Elf64_Shdr*)(p + eh->e_shoff + (size_t)i * eh->e_shentsize); };
    if (eh->e_shstrndx >= eh->e_shnum) return 0;
    const Elf64_Shdr* shstr = sec(eh->e_shstrndx);
    if (shstr->sh_offset + shstr->sh_size > len) return 0;
    const char* names = (const char*)(p + shstr->sh_offset);
    const char* symName = dynamic ? ".dynsym" : ".symtab";
    const char* strName = dynamic ? ".dynstr" : ".strtab";
    const Elf64_Shdr *symtab = nullptr, *strtab = nullptr;
    for (int i = 0; i < eh->e_shnum; i++) {
        const Elf64_Shdr* s = sec(i);
        if (s->sh_name >= shstr->sh_size) continue;
        const char* n = names + s->sh_name;
        if (!strcmp(n, symName)) symtab = s;
        else if (!strcmp(n, strName)) strtab = s;
    }
    if (!symtab || !strtab || !symtab->sh_entsize) return 0;
    if (symtab->sh_offset + symtab->sh_size > len || strtab->sh_offset + strtab->sh_size > len) return 0;
    const char* strs = (const char*)(p + strtab->sh_offset);
    auto* syms = (const Elf64_Sym*)(p + symtab->sh_offset);
    size_t n = symtab->sh_size / sizeof(Elf64_Sym);
    for (size_t i = 0; i < n; i++) {
        if (syms[i].st_name >= strtab->sh_size) continue;
        SymbolRec r;
        r.name = strs + syms[i].st_name;
        r.value = syms[i].st_value;
        r.size = syms[i].st_size;
        r.info = syms[i].st_info;
        fn(r);
    }
    return n;
}

const ElfInfo& Elf() {
    static const ElfInfo info = [] {
        ElfInfo e;
        MappedFile f;
        if (!f.open(ExePath())) {
            e.error = "cannot mmap /proc/self/exe";
            return e;
        }
        ResolveCore::ParseElfBuffer(f.p, f.len, e);
        if (e.pie) e.slide = MainImageSlide();
        return e;
    }();
    return info;
}

bool IsExecutableAddr(uint64_t addr) {
    const ElfInfo& e = Elf();
    for (auto& r : e.execRanges)
        if (addr >= r.first + e.slide && addr < r.second + e.slide) return true;
    return false;
}

bool IsTextAddr(uint64_t addr) {
    const ElfInfo& e = Elf();
    return e.textSize && addr >= e.textAddr + e.slide && addr < e.textAddr + e.textSize + e.slide;
}

// ---------------------------------------------------------------------------------------------
// `.eh_frame_hdr` function-entry oracle.
//
// On a stripped binary this is the closest thing to a symbol table that still exists, and nothing
// can strip it: the unwinder needs it. `.eh_frame_hdr` ends in a **sorted binary-search table** of
// (initial_location, fde_ptr) pairs — one per function with unwind info, 918 821 of them on this
// build. So "is this address the entry point of a real function?" becomes an exact lookup, which is
// a far stronger validator than "it is in .text and does not start with 00/CC".
//
// We support exactly the encodings this binary uses (table_enc 0x3b = DW_EH_PE_datarel|sdata4,
// fde_count_enc 0x03 = udata4) and refuse to guess at anything else — a misparsed table would turn
// the validator into a random filter, which is worse than having none.
namespace {
struct EhFrameTable {
    bool ok = false;
    std::string error;
    const uint8_t* table = nullptr;  // start of the (loc, fde) pairs, in the mapped file image
    uint32_t count = 0;
    uint64_t base = 0;              // datarel base == the .eh_frame_hdr vaddr (image-relative)
};

const EhFrameTable& EhFrame() {
    static const EhFrameTable t = [] {
        EhFrameTable e;
        // Intentionally leaked: the table below points into this mapping and is consulted for the
        // life of the process, so the mapping must outlive every caller.
        auto* f = new MappedFile();
        if (!f->open(ExePath())) { e.error = "cannot mmap the exe"; return e; }
        // Locate .eh_frame_hdr by section header (PT_GNU_EH_FRAME would do too; the section table
        // is present on this build, only the symbol table is stripped).
        auto* eh = (const Elf64_Ehdr*)f->p;
        if (f->len < sizeof(Elf64_Ehdr) || !eh->e_shoff || eh->e_shstrndx >= eh->e_shnum) {
            e.error = "no section headers";
            return e;
        }
        auto sec = [&](int i) { return (const Elf64_Shdr*)(f->p + eh->e_shoff + (size_t)i * eh->e_shentsize); };
        const Elf64_Shdr* shstr = sec(eh->e_shstrndx);
        const char* names = (const char*)(f->p + shstr->sh_offset);
        const Elf64_Shdr* hdr = nullptr;
        for (int i = 0; i < eh->e_shnum; i++)
            if (!strcmp(names + sec(i)->sh_name, ".eh_frame_hdr")) hdr = sec(i);
        if (!hdr || hdr->sh_offset + 12 > f->len) { e.error = ".eh_frame_hdr absent"; return e; }
        const uint8_t* p = f->p + hdr->sh_offset;
        uint8_t version = p[0], ehPtrEnc = p[1], countEnc = p[2], tableEnc = p[3];
        if (version != 1 || countEnc != 0x03 || tableEnc != 0x3b) {
            char b[128];
            snprintf(b, sizeof b, "unsupported encodings v%u eh=0x%02x count=0x%02x table=0x%02x", version,
                     ehPtrEnc, countEnc, tableEnc);
            e.error = b;
            return e;
        }
        memcpy(&e.count, p + 8, 4);
        e.table = p + 12;
        e.base = hdr->sh_addr;
        if ((size_t)(hdr->sh_offset + 12) + (size_t)e.count * 8 > f->len) {
            e.error = "table does not fit the file";
            return e;
        }
        e.ok = e.count > 0;
        if (!e.ok) e.error = "empty table";
        PluginLog("resolve: .eh_frame_hdr has %u function entries (datarel base 0x%llx)", e.count,
                  (unsigned long long)e.base);
        return e;
    }();
    return t;
}
}  // namespace

// True when `addr` (a slid runtime address) is the entry point of a function with unwind info.
// Returns false — never "true by default" — when the table could not be parsed; callers treat that
// as "no opinion" and fall back to the .text bound, which is what EhFrameAvailable() is for.
bool IsFunctionEntry(uint64_t addr) {
    const EhFrameTable& t = EhFrame();
    if (!t.ok) return false;
    uint64_t want = addr - Elf().slide;  // the table is image-relative
    uint32_t lo = 0, hi = t.count;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        int32_t rel = 0;
        memcpy(&rel, t.table + (size_t)mid * 8, 4);
        uint64_t loc = t.base + (int64_t)rel;
        if (loc == want) return true;
        if (loc < want) lo = mid + 1;
        else hi = mid;
    }
    return false;
}
bool EhFrameAvailable() { return EhFrame().ok; }
std::string EhFrameDetail() {
    const EhFrameTable& t = EhFrame();
    return t.ok ? std::to_string(t.count) + " function entries" : ("unavailable: " + t.error);
}

// Enumerates .dynsym once and keeps every `_ZTV*` entry (runtime addresses).
const std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>>& VTableSymbols() {
    static const std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>> tables = [] {
        std::vector<std::pair<std::string, std::pair<uint64_t, uint64_t>>> out;
        MappedFile f;
        if (!f.open(ExePath())) return out;
        uint64_t slide = Elf().slide;
        ResolveCore::ForEachSymbol(f.p, f.len, true, [&](const ResolveCore::SymbolRec& s) {
            if (!s.value || s.size < 16) return;
            if (strncmp(s.name, "_ZTV", 4) != 0) return;
            out.push_back({s.name, {s.value + slide, s.size}});
        });
        PluginLog("resolve: %zu exported vtables", out.size());
        return out;
    }();
    return tables;
}

uint64_t DynSymAddr(const char* name) {
    if (void* p = dlsym(RTLD_DEFAULT, name)) return (uint64_t)(uintptr_t)p;
    MappedFile f;
    if (!f.open(ExePath())) return 0;
    uint64_t found = 0, slide = Elf().slide;
    ResolveCore::ForEachSymbol(f.p, f.len, true, [&](const ResolveCore::SymbolRec& s) {
        if (!found && s.value && !strcmp(s.name, name)) found = s.value + slide;
    });
    return found;
}

// ---------------------------------------------------------------------------------------------
// name matching / demangling

std::string ResolveCore::Demangle(const char* mangled) {
    if (!mangled || mangled[0] != '_' || mangled[1] != 'Z') return "";
    int status = 0;
    char* d = abi::__cxa_demangle(mangled, nullptr, nullptr, &status);
    if (status != 0 || !d) {
        if (d) free(d);
        return "";
    }
    std::string out(d);
    free(d);
    return out;
}

std::string ResolveCore::BaseName(const std::string& key) {
    size_t c = key.rfind("::");
    return c == std::string::npos ? key : key.substr(c + 2);
}

bool ResolveCore::NameMatches(const std::string& line, const char* key, const char* sig) {
    if (sig && *sig) return line == sig;
    size_t par = line.find('(');
    std::string base = par == std::string::npos ? line : line.substr(0, par);
    // Strip a trailing " const" that a parameterless const method would not have, and any
    // return type the demangler prefixed (it does not for functions, only for some templates).
    return base == key;
}

// ---------------------------------------------------------------------------------------------
// signatures

bool ResolveCore::ParseSignature(const std::string& pattern, Signature& out, std::string& err) {
    out.bytes.clear();
    size_t i = 0;
    while (i < pattern.size()) {
        char c = pattern[i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { i++; continue; }
        if (c == '?') {
            out.bytes.push_back(-1);
            i++;
            if (i < pattern.size() && pattern[i] == '?') i++;
            continue;
        }
        auto hex = [](char h, int& v) {
            if (h >= '0' && h <= '9') { v = h - '0'; return true; }
            if (h >= 'a' && h <= 'f') { v = h - 'a' + 10; return true; }
            if (h >= 'A' && h <= 'F') { v = h - 'A' + 10; return true; }
            return false;
        };
        int hi = 0, lo = 0;
        if (i + 1 >= pattern.size() || !hex(pattern[i], hi) || !hex(pattern[i + 1], lo)) {
            err = "not a hex byte pair at offset " + std::to_string(i);
            out.bytes.clear();
            return false;
        }
        out.bytes.push_back((hi << 4) | lo);
        i += 2;
    }
    if (out.bytes.empty()) { err = "empty pattern"; return false; }
    // A pattern that starts or ends with a wildcard hides how specific it really is.
    if (out.bytes.front() < 0) { err = "pattern must not start with a wildcard"; out.bytes.clear(); return false; }
    return true;
}

size_t ResolveCore::ScanSignature(const uint8_t* hay, size_t len, const Signature& sig, size_t& firstOff,
                                  size_t maxHits) {
    firstOff = 0;
    if (sig.bytes.empty() || len < sig.bytes.size()) return 0;
    size_t hits = 0;
    const size_t n = sig.bytes.size();
    const uint8_t first = (uint8_t)sig.bytes[0];
    for (size_t i = 0; i + n <= len; i++) {
        if (hay[i] != first) continue;
        size_t k = 1;
        for (; k < n; k++) {
            int want = sig.bytes[k];
            if (want >= 0 && hay[i + k] != (uint8_t)want) break;
        }
        if (k != n) continue;
        if (!hits) firstOff = i;
        if (++hits >= maxHits) break;
    }
    return hits;
}

// ---------------------------------------------------------------------------------------------
// ResolveCore additions for a stripped, RTTI-only binary

std::string ResolveCore::VTableClassName(const char* mangled) {
    if (!mangled || strncmp(mangled, "_ZTV", 4) != 0) return "";
    std::string d = Demangle(mangled);
    const char* kPrefix = "vtable for ";
    if (d.rfind(kPrefix, 0) == 0) return d.substr(strlen(kPrefix));
    return d;
}

std::vector<uint8_t> ResolveCore::Utf16Bytes(const std::string& ascii) {
    std::vector<uint8_t> out;
    out.reserve(ascii.size() * 2);
    for (unsigned char c : ascii) {
        out.push_back(c);
        out.push_back(0);
    }
    return out;
}

std::vector<size_t> ResolveCore::FindAll(const uint8_t* hay, size_t len, const uint8_t* needle, size_t nlen,
                                         size_t maxHits) {
    std::vector<size_t> out;
    if (!hay || !needle || !nlen || len < nlen) return out;
    for (size_t i = 0; i + nlen <= len; i++) {
        if (hay[i] != needle[0]) continue;
        if (memcmp(hay + i, needle, nlen) == 0) {
            out.push_back(i);
            if (out.size() >= maxHits) break;
        }
    }
    return out;
}

uint64_t ResolveCore::RipTarget(const uint8_t* match, uint64_t matchVa, size_t disp32Off) {
    if (!match) return 0;
    int32_t disp = 0;
    memcpy(&disp, match + disp32Off, 4);
    return matchVa + disp32Off + 4 + (int64_t)disp;
}

ResolveCore::PeFingerprint ResolveCore::FingerprintProcessEvent(const uint8_t* body, size_t len) {
    PeFingerprint fp;
    if (!body || len < 16) return fp;
    fp.longEnough = len >= 512;
    for (size_t i = 0; i + 8 <= len; i++) {
        // `sub rsp, imm32`  = 48 81 EC <imm32>.  ProcessEvent reserves an FFrame plus the callee's
        // parameter block, so the frame is far larger than a normal virtual's.
        if (body[i] == 0x48 && body[i + 1] == 0x81 && body[i + 2] == 0xEC) {
            uint32_t imm = 0;
            memcpy(&imm, body + i + 3, 4);
            if (imm >= 0x40) fp.bigFrame = true;
        }
        // `and rsp, imm8` = 48 83 E4 xx — the FMemory_Alloca realignment.
        if (body[i] == 0x48 && body[i + 1] == 0x83 && body[i + 2] == 0xE4) fp.bigFrame = true;
        // An immediate 0x00000400 (FUNC_Native) tested or compared against a dword. Encodings we
        // accept: `test dword [r+d], 400h` (F7 /0), `and eax, 400h` (25), `test eax,400h` (A9),
        // `cmp dword [r+d], 400h` (81 /7). All of them carry the literal 00 04 00 00.
        if (body[i] == 0x00 && body[i + 1] == 0x04 && body[i + 2] == 0x00 && body[i + 3] == 0x00 && i >= 2) {
            uint8_t op = body[i - 2], op2 = body[i - 1];
            if (op == 0xF7 || op == 0x81 || op2 == 0xF7 || op2 == 0x81 || op2 == 0x25 || op2 == 0xA9)
                fp.funcNativeTest = true;
        }
        // `call qword ptr [reg+disp8]`  = FF 5x dd   /  `call qword ptr [reg+disp32]` = FF 9x dddddddd.
        // This is the UFunction::Func / native-thunk dispatch at the heart of ProcessEvent.
        if (body[i] == 0xFF && ((body[i + 1] & 0xF8) == 0x50 || (body[i + 1] & 0xF8) == 0x90))
            fp.indirectCall = true;
    }
    return fp;
}

// ---------------------------------------------------------------------------------------------
// vtables (LIVE MEMORY ONLY — see the header comment)

namespace {
// addr -> mangled name, sorted, built once from VTableSymbols().
const std::vector<std::pair<uint64_t, const std::string*>>& VTableByAddr() {
    static const std::vector<std::pair<uint64_t, const std::string*>> v = [] {
        std::vector<std::pair<uint64_t, const std::string*>> out;
        for (auto& kv : VTableSymbols()) out.push_back({kv.second.first, &kv.first});
        std::sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        return out;
    }();
    return v;
}
std::unordered_map<std::string, std::pair<uint64_t, uint64_t>>& VTableByName() {
    static std::unordered_map<std::string, std::pair<uint64_t, uint64_t>> m = [] {
        std::unordered_map<std::string, std::pair<uint64_t, uint64_t>> out;
        for (auto& kv : VTableSymbols()) out.emplace(kv.first, kv.second);
        return out;
    }();
    return m;
}
}  // namespace

VTableInfo VTable(const char* ztvName) {
    VTableInfo vt;
    if (!ztvName || strncmp(ztvName, "_ZTV", 4) != 0) {
        vt.error = "not a vtable symbol name";
        return vt;
    }
    vt.ztv = ztvName;
    vt.className = ResolveCore::VTableClassName(ztvName);
    auto& byName = VTableByName();
    auto it = byName.find(ztvName);
    if (it != byName.end()) {
        vt.addr = it->second.first;
        vt.size = it->second.second;
    } else {
        // dlsym is the cheaper path when the symbol table scan has not been primed yet.
        vt.addr = DynSymAddr(ztvName);
        if (!vt.addr) {
            vt.error = "not exported in .dynsym";
            return vt;
        }
    }
    // Two header words: offset-to-top (0 for a primary vtable) and the typeinfo pointer. Both are
    // relocated, so a live read of them is also the proof that we are NOT looking at the file image.
    if (!MemReadable((const void*)(uintptr_t)vt.addr, 16)) {
        vt.error = "vtable address is not in a readable mapping";
        return vt;
    }
    auto* w = (const uint64_t*)(uintptr_t)vt.addr;
    vt.offsetToTop = (int64_t)w[0];
    vt.typeInfo = w[1];
    if (vt.size >= 16) vt.slots = (size_t)((vt.size - 16) / 8);
    if (vt.offsetToTop != 0) {
        // A secondary vtable of a multiply-inheriting class. We never hook through one of those:
        // the `this` adjustment would be wrong.
        vt.error = "offset-to-top is not 0 (secondary vtable)";
        return vt;
    }
    if (!vt.typeInfo) {
        vt.error = "typeinfo pointer is 0 — this looks like the unrelocated file image";
        return vt;
    }
    vt.ok = true;
    return vt;
}

bool VTableSlotsLive(const VTableInfo& vt, size_t count, std::vector<uint64_t>& out, std::string& err) {
    out.clear();
    if (!vt.ok || !vt.addr) {
        err = vt.error.empty() ? "vtable not resolved" : vt.error;
        return false;
    }
    if (vt.slots && count > vt.slots) count = vt.slots;
    if (!count) {
        err = "vtable slot count is 0 (no symbol size)";
        return false;
    }
    const void* first = (const void*)(uintptr_t)(vt.addr + 16);
    if (!MemReadable(first, count * 8)) {
        err = "slots are not inside a readable mapping";
        return false;
    }
    auto* w = (const uint64_t*)first;
    out.assign(w, w + count);
    // MULTIPLE INHERITANCE: a single `_ZTV` symbol can pack several sub-vtables, each introduced by
    // its own `[offset-to-top][typeinfo]` header pair. On this build 10 542 slot positions across the
    // binary hold such a header instead of a function, and `_ZTI20ADunePlayerCharacter` appears 27
    // times inside its own `_ZTV`. So `st_size / 8 - 2` OVERESTIMATES the primary slot count for any
    // MI class, and a scan that runs past the first embedded header is reading typeinfo pointers as
    // if they were code. Stop at the first `[0][<the class's own typeinfo>]` pair.
    for (size_t i = 0; i + 1 < out.size(); i++) {
        if (out[i] == 0 && out[i + 1] == vt.typeInfo) {
            out.resize(i);
            break;
        }
    }
    // An all-zero table means the caller is reading the ON-DISK image of a PIE, where every slot is
    // an R_X86_64_RELATIVE relocation that the loader has not applied. Fail loudly: silently
    // "resolving" 0 for every slot is how a plugin ends up jumping to address 0 on the game thread.
    bool anyNonZero = false;
    for (uint64_t v : out) anyNonZero = anyNonZero || v != 0;
    if (!anyNonZero) {
        err = "every slot is 0 — unrelocated file image, not live memory";
        out.clear();
        return false;
    }
    return true;
}

uint64_t VTableSlot(const char* ztvName, size_t slot) {
    VTableInfo vt = VTable(ztvName);
    std::vector<uint64_t> slots;
    std::string err;
    if (!VTableSlotsLive(vt, slot + 1, slots, err)) return 0;
    return slot < slots.size() ? slots[slot] : 0;
}

std::string ClassNameByVTablePtr(const void* obj) {
    if (!MemReadable(obj, 8)) return "";
    uint64_t vp = *(const uint64_t*)obj;
    if (!vp) return "";
    const auto& v = VTableByAddr();
    // The object's vtable pointer points at slot 0, i.e. `_ZTV… + 16`.
    uint64_t want = vp >= 16 ? vp - 16 : 0;
    auto it = std::lower_bound(v.begin(), v.end(), want,
                               [](const std::pair<uint64_t, const std::string*>& a, uint64_t x) { return a.first < x; });
    if (it == v.end() || it->first != want) return "";
    return ResolveCore::VTableClassName(it->second->c_str());
}

bool LooksLikeUObject(const void* obj) {
    // Three cheap, independent tests, all of which must pass before any FName or property read.
    // None of them calls into the game.
    if (!MemReadable(obj, 0x40)) return false;
    uint64_t vp = *(const uint64_t*)obj;
    if (!vp || (vp & 7) != 0) return false;          // a vtable pointer is 8-byte aligned
    if (!MemReadable((const void*)(uintptr_t)vp, 8)) return false;
    // Slot 0 of any live vtable must be inside .text (see the note on PlausibleCode: the single
    // R+X PT_LOAD of this binary also covers .rodata and .rela.dyn, so "executable segment" is not
    // a useful bound here).
    uint64_t slot0 = *(const uint64_t*)(uintptr_t)vp;
    if (!IsTextAddr(slot0)) return false;
    // And the vtable must be one this binary exports, i.e. a class we can name.
    return !ClassNameByVTablePtr(obj).empty();
}

// ---------------------------------------------------------------------------------------------
// strategies

namespace {

std::string CachePath() { return PluginDataDir() + "/symcache.json"; }

bool Unresolved(const SymEntry& e) { return e.addr == 0; }

// ---- strategy `dynsym-vtable` ------------------------------------------------------------------
// The only exact, guaranteed source on this build. Resolves every KVTable want, and is also what
// every KSlot want depends on.
size_t ScanVTables(std::string& detail) {
    size_t hits = 0, absent = 0, bad = 0;
    for (size_t i = 0; i < g_entries.size(); i++) {
        if (kWanted[i].kind != KVTable || !Unresolved(g_entries[i])) continue;
        VTableInfo vt = VTable(kWanted[i].key);
        if (!vt.ok) {
            if (vt.error.find("not exported") != std::string::npos) absent++;
            else bad++;
            PluginLog("resolve: vtable %s (%s): %s", kWanted[i].key,
                      vt.className.empty() ? "?" : vt.className.c_str(), vt.error.c_str());
            continue;
        }
        SymEntry& e = g_entries[i];
        e.addr = vt.addr;
        e.rva = vt.addr - Elf().slide;
        e.how = "dynsym-vtable";
        e.signature = vt.className + " (" + std::to_string(vt.slots) + " slots)";
        e.records = 1;
        hits++;
    }
    detail = std::to_string(VTableSymbols().size()) + " exported vtables; " + std::to_string(hits) +
             " wanted found, " + std::to_string(absent) + " absent, " + std::to_string(bad) + " rejected";
    return hits;
}

// An address must sit in an executable segment, be readable and not start with 0x00 / 0xCC.
// ⚠️ "inside an executable PT_LOAD" is a WEAK test on this binary: it has a single R+X PT_LOAD
// spanning vaddr 0x0 .. 0x153690d0, which also contains `.dynsym`, `.dynstr`, `.rela.dyn`, `.rodata`
// and `.eh_frame`. So a typeinfo pointer or a string address would pass it. The real bound is
// `.text` itself, and the strongest available oracle is "this address is a function entry according
// to `.eh_frame_hdr`" (918 821 FDEs on this build) — which needs no symbols and works at runtime.
bool PlausibleCode(uint64_t addr) {
    if (!addr) return false;
    const ElfInfo& e = Elf();
    bool inText = e.textSize && addr >= e.textAddr + e.slide && addr < e.textAddr + e.textSize + e.slide;
    if (!inText) return false;
    if (!MemReadable((const void*)(uintptr_t)addr, 1)) return false;
    uint8_t b = *(const uint8_t*)(uintptr_t)addr;
    if (b == 0x00 || b == 0xCC) return false;
    // When the unwind table parsed, insist that the address really is a function entry. This is what
    // catches "the slot index is off by one and we are pointing into the middle of a function" —
    // the failure mode that a byte-value check cannot see.
    if (EhFrameAvailable() && !IsFunctionEntry(addr)) {
        PluginLog("resolve: 0x%llx is in .text but is not an .eh_frame function entry", (unsigned long long)addr);
        return false;
    }
    return true;
}

// `__cxa_pure_virtual` fills the slot of any pure virtual. A slot holding it is not an override and
// must never be hooked or called.
uint64_t PureVirtualAddr() {
    static uint64_t a = [] {
        void* p = dlsym(RTLD_DEFAULT, "__cxa_pure_virtual");
        return (uint64_t)(uintptr_t)p;
    }();
    return a;
}

// ---- deriving the ProcessEvent slot ------------------------------------------------------------
//
// There is no `UObject::ProcessEvent` symbol, so the slot index is derived, at boot, on the live
// process. Three independent tests, and the exactly-one rule:
//
//   T1 plausibility  the slot holds a readable code address in an executable segment that is not
//                    `__cxa_pure_virtual` and does not begin 00/CC.
//   T2 invariance    `UObject::ProcessEvent` is not overridden by AActor, UEngine, UGameInstance or
//                    the Dune classes in stock UE 5.x, so the word at the ProcessEvent slot must be
//                    IDENTICAL in every probed vtable that is long enough. This is the strong test:
//                    it eliminates the destructor pair, Serialize, PostInitProperties,
//                    GetLifetimeReplicatedProps and every other slot the subclasses do override.
//   T3 fingerprint   the candidate's code must look like ProcessEvent: a large stack frame, an
//                    immediate 0x400 (FUNC_Native) test, an indirect call through a member pointer,
//                    and at least 512 bytes of body (see ResolveCore::FingerprintProcessEvent).
//
// If exactly one slot survives, that is the slot. If zero or several do, the slot stays UNRESOLVED
// and /health reports the shortlist — the plugin then runs without ProcessEvent-based events rather
// than hooking a guess. `TAKARO_PROCESSEVENT_SLOT` overrides the derivation (recorded as `how=env`)
// so that a human who has settled it in a debugger can hand us the answer.
//
// Finally: even a derived slot is only *proven* once our detour has fired. Resolve::
// NoteProcessEventFired() records that, and /health exposes it as processEventSlotConfirmed.
// The structural test that actually identifies ProcessEvent on this build, and the reason the
// derivation is trustworthy at all.
//
// `UObject::ProcessEvent` begins its dispatch with UE's remote-call decision:
//     int32 Callspace = GetFunctionCallspace(Function, nullptr);   // the NEXT vtable slot
//     if (Callspace & Remote) CallRemoteFunction(...);             // the slot after that
//     if (!(Callspace & Local)) return;
// Compiled, that is an indirect `call [reg + 8*(N+1)]` followed by a conditional indirect
// `call [reg + 8*(N+2)]` on the same object, where `reg` holds the object's **vptr**.
//
// ⚠️ The displacement is measured from the vptr, i.e. from SLOT 0 — not from the `_ZTV` symbol,
// which sits 16 bytes earlier behind the [offset-to-top][typeinfo] header. Getting that wrong makes
// the test silently never fire, which was the first thing this function got wrong and which the
// offline verification against the real bytes caught: on this build N=86 gives 8*87=0x2b8 and
// 8*88=0x2c0, and both are present at 0x1023ace0.
//
// This is a far stronger signal than any generic fingerprint because it is *self-referential*: it
// ties the candidate's own slot index to the code at that slot. A wrong slot cannot fake it.
bool HasCallspaceTriplet(const uint8_t* body, size_t len, size_t slot) {
    if (!body || len < 16) return false;
    const uint32_t d1 = (uint32_t)(8 * (slot + 1));
    const uint32_t d2 = (uint32_t)(8 * (slot + 2));
    bool f1 = false, f2 = false;
    for (size_t i = 0; i + 7 <= len; i++) {
        // `call qword ptr [reg+disp32]` = FF 9x dd dd dd dd  (also FF 94 xx with a SIB byte)
        if (body[i] != 0xFF) continue;
        uint8_t modrm = body[i + 1];
        if ((modrm & 0xF8) != 0x90) continue;
        size_t dispAt = i + 2 + ((modrm & 7) == 4 ? 1 : 0);  // skip a SIB byte
        if (dispAt + 4 > len) continue;
        uint32_t disp = 0;
        memcpy(&disp, body + dispAt, 4);
        if (disp == d1) f1 = true;
        if (disp == d2) f2 = true;
    }
    return f1 && f2;
}

void DeriveProcessEventSlot() {
    std::string envSlot = ConfigValue("TAKARO_PROCESSEVENT_SLOT", "processEventSlot", "");
    if (!envSlot.empty()) {
        char* end = nullptr;
        unsigned long v = strtoul(envSlot.c_str(), &end, 0);
        if (end && !*end && v < 1024) {
            g_processEventSlot = (size_t)v;
            g_processEventSlotHow = "env TAKARO_PROCESSEVENT_SLOT=" + envSlot;
            PluginLog("resolve: ProcessEvent slot %zu taken from the environment", g_processEventSlot);
            return;
        }
        PluginLog("resolve: TAKARO_PROCESSEVENT_SLOT='%s' is not a slot index; deriving instead", envSlot.c_str());
    }

    VTableInfo base = VTable("_ZTV7UObject");
    if (!base.ok) {
        g_processEventSlotHow = "_ZTV7UObject: " + base.error;
        return;
    }
    size_t window = base.slots ? base.slots : 97;
    if (window > 160) window = 160;
    std::vector<uint64_t> baseSlots;
    std::string err;
    if (!VTableSlotsLive(base, window, baseSlots, err)) {
        g_processEventSlotHow = "_ZTV7UObject slots: " + err;
        return;
    }

    // The invariance probe set.
    //
    // ⚠️ AActor, AInfo, APawn, ACharacter, APlayerController and AGameModeBase all **DO** override
    // ProcessEvent on this build (they share 0x12913180), and the UActorComponent family has its own
    // override — so they are useless as invariance probes and were removed. The classes that keep
    // UObject's implementation are the non-actor ones: UEngine, UGameEngine, UGameInstance, UWorld.
    // Invariance across those is a genuine, if weaker, filter; the triplet test below is what
    // actually decides.
    static const char* kProbes[] = {"_ZTV7UEngine", "_ZTV11UGameEngine", "_ZTV13UGameInstance", "_ZTV6UWorld",
                                    "_ZTV11UDamageType"};
    std::vector<std::pair<std::string, std::vector<uint64_t>>> probes;
    for (const char* z : kProbes) {
        VTableInfo v = VTable(z);
        if (!v.ok) continue;
        std::vector<uint64_t> sl;
        std::string e2;
        if (!VTableSlotsLive(v, window, sl, e2)) continue;
        if (sl.size() < baseSlots.size()) continue;
        probes.push_back({v.className, std::move(sl)});
    }

    const uint64_t pure = PureVirtualAddr();
    std::vector<size_t> candidates;
    std::string rejectLog;
    for (size_t slot = 0; slot < baseSlots.size(); slot++) {
        uint64_t a = baseSlots[slot];
        if (!PlausibleCode(a) || (pure && a == pure)) continue;               // T1
        bool invariant = true;
        for (auto& p : probes) invariant = invariant && p.second[slot] == a;
        if (!invariant) continue;                                            // T2
        size_t len = 4096;
        while (len > 64 && !MemReadable((const void*)(uintptr_t)a, len)) len /= 2;
        if (len <= 64) continue;
        const uint8_t* body = (const uint8_t*)(uintptr_t)a;
        if (!HasCallspaceTriplet(body, len, slot)) {                         // T3 — the decisive one
            continue;
        }
        auto fp = ResolveCore::FingerprintProcessEvent(body, len);           // T4 — corroboration
        if (fp.score() < 3) {
            char bb[160];
            snprintf(bb, sizeof bb, "slot %zu: triplet ok but fingerprint %d/4; ", slot, fp.score());
            rejectLog += bb;
            continue;
        }
        candidates.push_back(slot);
    }

    char detail[640];
    if (candidates.size() == 1) {
        g_processEventSlot = candidates[0];
        const char* agrees = candidates[0] == kSlotProcessEventOffline ? "agrees with" : "DISAGREES WITH";
        snprintf(detail, sizeof detail,
                 "derived: slot %zu (callspace triplet [0x%zx,0x%zx] + invariant across %zu non-actor "
                 "vtables + fingerprint >=3/4; exactly one candidate). %s the offline dissection's slot %zu.",
                 candidates[0], 8 * (candidates[0] + 1), 8 * (candidates[0] + 2), probes.size(), agrees,
                 kSlotProcessEventOffline);
        g_processEventSlotHow = detail;
        PluginLog("resolve: %s", detail);
        return;
    }

    // Zero or several candidates. Fall back to the offline-derived slot ONLY if it survives the
    // triplet test on its own — i.e. the constant is treated as a hypothesis to be re-proven here,
    // never as an answer. Anything else stays unresolved.
    std::string list;
    for (size_t sl : candidates) list += (list.empty() ? "" : ",") + std::to_string(sl);
    if (kSlotProcessEventOffline < baseSlots.size()) {
        uint64_t a = baseSlots[kSlotProcessEventOffline];
        size_t len = 4096;
        while (len > 64 && !MemReadable((const void*)(uintptr_t)a, len)) len /= 2;
        if (len > 64 && PlausibleCode(a) && (!pure || a != pure) &&
            HasCallspaceTriplet((const uint8_t*)(uintptr_t)a, len, kSlotProcessEventOffline)) {
            g_processEventSlot = kSlotProcessEventOffline;
            snprintf(detail, sizeof detail,
                     "offline slot %zu re-proven here by the callspace triplet (the open derivation "
                     "produced %zu candidates [%s], so it did not decide on its own)",
                     kSlotProcessEventOffline, candidates.size(), list.c_str());
            g_processEventSlotHow = detail;
            PluginLog("resolve: %s", detail);
            return;
        }
    }
    snprintf(detail, sizeof detail,
             "UNRESOLVED: %zu candidates [%s] over %zu slots against %zu probe vtables (exactly one "
             "required), and the offline slot %zu did not pass the callspace-triplet test either. "
             "Near misses: %s. Set TAKARO_PROCESSEVENT_SLOT to settle it.",
             candidates.size(), list.c_str(), baseSlots.size(), probes.size(), kSlotProcessEventOffline,
             rejectLog.empty() ? "none" : rejectLog.c_str());
    g_processEventSlotHow = detail;
    PluginLog("resolve: %s", detail);
}

// ---- strategy `rtti-slot` ----------------------------------------------------------------------
// Reads a function address out of a LIVE vtable. Every read is validated: the word must be
// plausible code and must not be `__cxa_pure_virtual`. For an override we also record whether the
// word differs from the base class's, because "the subclass does not actually override this" is the
// single most common way a vtable hook ends up bound to a slot that never fires.
size_t ScanSlots(std::string& detail) {
    size_t hits = 0, noSlot = 0, rejected = 0;
    const uint64_t pure = PureVirtualAddr();
    for (size_t i = 0; i < g_entries.size(); i++) {
        if (kWanted[i].kind != KSlot || !Unresolved(g_entries[i])) continue;
        int want = kWanted[i].slot;
        size_t slot;
        if (want == kSlotProcessEvent) {
            if (g_processEventSlot == SIZE_MAX) { noSlot++; continue; }
            slot = g_processEventSlot;
        } else if (want < 0) {
            noSlot++;  // slot index not established by the offline dissection yet
            continue;
        } else {
            slot = (size_t)want;
        }
        VTableInfo vt = VTable(kWanted[i].ztv);
        if (!vt.ok) { noSlot++; continue; }
        std::vector<uint64_t> slots;
        std::string err;
        if (!VTableSlotsLive(vt, slot + 1, slots, err) || slot >= slots.size()) {
            PluginLog("resolve: %s: %s slot %zu unreadable (%s)", kWanted[i].key, kWanted[i].ztv, slot, err.c_str());
            rejected++;
            continue;
        }
        uint64_t a = slots[slot];
        if (!PlausibleCode(a) || (pure && a == pure)) {
            PluginLog("resolve: %s: %s slot %zu = 0x%llx is not plausible code%s", kWanted[i].key, kWanted[i].ztv,
                      slot, (unsigned long long)a, (pure && a == pure) ? " (__cxa_pure_virtual)" : "");
            rejected++;
            continue;
        }
        SymEntry& e = g_entries[i];
        e.addr = a;
        e.rva = a - Elf().slide;
        e.how = "rtti-slot";
        e.slot = slot;
        e.signature = std::string(kWanted[i].ztv) + "[" + std::to_string(slot) + "]";
        e.records = 1;
        hits++;
    }
    detail = std::to_string(hits) + " slots read, " + std::to_string(noSlot) + " without a known slot index, " +
             std::to_string(rejected) + " rejected";
    return hits;
}

// ---- strategy `string-xref` --------------------------------------------------------------------
// A byte pattern over `.text`, accepted only when it matches EXACTLY ONCE. When `xref` is given the
// literal must additionally be present in the image as a **UTF-16LE** string (this build's literals
// are wide; a narrow search silently finds nothing and every signature would be "unique" by
// accident) and the match must be within kXrefWindow bytes of an instruction that references it.
// `ripOff` turns the match into a data address by decoding the RIP-relative disp32.
const size_t kXrefWindow = 0x1000;

// Virtual addresses at which `literal` occurs as a UTF-16LE string in a read-only segment.
std::vector<uint64_t> Utf16LiteralAddrs(const uint8_t* img, size_t imgLen, const std::string& literal) {
    std::vector<uint64_t> out;
    const ElfInfo& e = Elf();
    std::vector<uint8_t> needle = ResolveCore::Utf16Bytes(literal);
    if (needle.empty()) return out;
    // `.rodata` first; then every other read-only PT_LOAD range the ELF declared.
    std::vector<std::pair<uint64_t, std::pair<uint64_t, uint64_t>>> areas;  // off -> {addr, size}
    if (e.rodataSize) areas.push_back({e.rodataOff, {e.rodataAddr, e.rodataSize}});
    for (auto& r : e.rodataRanges) {
        if (r.first == e.rodataAddr) continue;
        areas.push_back({r.first, {r.first, r.second - r.first}});  // vaddr == offset only for a 1:1 map
    }
    for (auto& a : areas) {
        uint64_t off = a.first, addr = a.second.first, size = a.second.second;
        if (!size || off + size > imgLen) continue;
        for (size_t hit : ResolveCore::FindAll(img + off, size, needle.data(), needle.size(), 16))
            out.push_back(addr + hit);
    }
    return out;
}

size_t ScanStringXrefs(std::string& detail) {
    size_t wanted = 0;
    for (size_t i = 0; i < g_entries.size(); i++)
        if (Unresolved(g_entries[i]) && kWanted[i].pattern) wanted++;
    if (!wanted) {
        detail = "no byte patterns declared for the unresolved names (see research/"
                 "2026-09-21-binary-dissection.md: a pattern is only added once it is verified unique)";
        return 0;
    }
    const ElfInfo& elf = Elf();
    MappedFile f;
    if (!f.open(ExePath()) || !elf.textSize || elf.textOff + elf.textSize > f.len) {
        detail = "cannot map .text";
        return 0;
    }
    const uint8_t* text = f.p + elf.textOff;
    size_t hits = 0, rejected = 0;
    for (size_t i = 0; i < g_entries.size(); i++) {
        if (!Unresolved(g_entries[i]) || !kWanted[i].pattern) continue;
        const Want& w = kWanted[i];
        ResolveCore::Signature sig;
        std::string err;
        if (!ResolveCore::ParseSignature(w.pattern, sig, err)) {
            PluginLog("resolve: pattern for %s is malformed: %s", w.key, err.c_str());
            rejected++;
            continue;
        }
        size_t off = 0;
        size_t count = ResolveCore::ScanSignature(text, elf.textSize, sig, off, 2);
        if (count != 1) {
            rejected++;
            PluginLog("resolve: pattern for %s matched %zu times (exactly one required) - rejected", w.key, count);
            continue;
        }
        uint64_t matchVa = elf.textAddr + off;
        // The xref check. A pattern without a verified anchor is still accepted (it was unique), but
        // an anchor that is *declared and not satisfied* is a rejection: it means the pattern landed
        // somewhere other than the function we reasoned about.
        if (w.xref && *w.xref) {
            std::vector<uint64_t> lits = Utf16LiteralAddrs(f.p, f.len, w.xref);
            if (lits.empty()) {
                PluginLog("resolve: xref literal '%s' for %s is not in the image as UTF-16LE - rejected", w.xref,
                          w.key);
                rejected++;
                continue;
            }
            // Look for any instruction inside the window around the match whose RIP-relative target
            // is one of the literal addresses. A `lea reg,[rip+d]` is 48 8D xx dddddddd.
            bool seen = false;
            size_t lo = off > kXrefWindow ? off - kXrefWindow : 0;
            size_t hi = off + kXrefWindow < elf.textSize ? off + kXrefWindow : elf.textSize - 7;
            for (size_t k = lo; k + 7 <= hi && !seen; k++) {
                if (text[k] != 0x48 || text[k + 1] != 0x8D) continue;
                uint64_t target = ResolveCore::RipTarget(text + k, elf.textAddr + k, 3);
                for (uint64_t l : lits) seen = seen || target == l;
            }
            if (!seen) {
                PluginLog("resolve: %s matched uniquely at 0x%llx but nothing within 0x%zx bytes references '%s' "
                          "- rejected", w.key, (unsigned long long)matchVa, kXrefWindow, w.xref);
                rejected++;
                continue;
            }
        }
        SymEntry& e = g_entries[i];
        if (w.kind == KGlobal) {
            if (w.ripOff < 0) {
                PluginLog("resolve: %s is a global but declares no ripOff - rejected", w.key);
                rejected++;
                continue;
            }
            uint64_t data = ResolveCore::RipTarget(text + off, matchVa, (size_t)w.ripOff);
            // A global must NOT be in an executable segment, and it must be readable once slid.
            if (IsTextAddr(data + elf.slide) ||
                !MemReadable((const void*)(uintptr_t)(data + elf.slide), 8)) {
                PluginLog("resolve: %s decoded to 0x%llx which is not a readable data address - rejected", w.key,
                          (unsigned long long)(data + elf.slide));
                rejected++;
                continue;
            }
            e.rva = data;
            e.addr = data + elf.slide;
        } else {
            e.rva = matchVa + (uint64_t)(int64_t)w.adjust;
            e.addr = e.rva + elf.slide;
        }
        e.how = "string-xref";
        e.signature = std::string(w.pattern) + (w.xref ? std::string(" @ \"") + w.xref + "\"" : "");
        e.records = 1;
        hits++;
    }
    detail = std::to_string(wanted) + " patterns declared, " + std::to_string(rejected) + " rejected (not unique, "
             "anchor unsatisfied, or implausible target)";
    return hits;
}

// ---- strategy `dynsym` -------------------------------------------------------------------------
// Plain exported-name lookup. On this build it only answers for _init/_fini/_start; it is kept
// because those three are what prove the loadBase + slide arithmetic in the M0 self-checks.
size_t ScanDynSymNames(std::string& detail) {
    size_t hits = 0, wanted = 0;
    for (size_t i = 0; i < g_entries.size(); i++) {
        if (kWanted[i].kind != KDynSym || !Unresolved(g_entries[i])) continue;
        wanted++;
        uint64_t a = DynSymAddr(kWanted[i].key);
        if (!a) continue;
        SymEntry& e = g_entries[i];
        e.addr = a;
        e.rva = a - Elf().slide;
        e.how = "dynsym";
        e.signature = kWanted[i].key;
        e.records = 1;
        hits++;
    }
    detail = std::to_string(hits) + "/" + std::to_string(wanted) + " exported names found";
    return hits;
}

// ---- symcache ----------------------------------------------------------------------------------
// Keyed on `.note.gnu.build-id`, so a game update invalidates it by itself. RVAs are stored (never
// slid addresses), and the ProcessEvent slot is cached too — the derivation is the expensive part.
bool LoadCache() {
    std::string text;
    if (!ReadFile(CachePath(), text)) return false;
    JsonValue root;
    if (!JsonParse(text, root)) return false;
    auto* bid = root.get("buildId");
    if (!bid || !bid->isStr() || Elf().buildId.empty() || bid->str != Elf().buildId) return false;
    // A cache written by a different plugin build may predate names we now want; the coverage check
    // below catches that, but pinning the version makes the log say so.
    auto* pv = root.get("pluginVersion");
    if (pv && pv->isStr() && pv->str != TAKARO_PLUGIN_VERSION) return false;
    auto* names = root.get("names");
    if (!names || names->type != JsonValue::Object) return false;
    size_t hits = 0;
    const ElfInfo& elf = Elf();
    for (auto& e : g_entries) {
        auto* v = names->get(e.name);
        if (!v || v->type != JsonValue::Object) continue;
        auto* r = v->get("rva");
        if (!r || !r->isNum()) continue;
        uint64_t rva = (uint64_t)strtoull(r->str.c_str(), nullptr, 10);
        if (!rva) { hits++; continue; }  // remembered as unresolved; still counts as covered
        auto* h = v->get("how");
        std::string how = h && h->isStr() ? h->str : "";
        e.rva = rva;
        e.addr = rva + elf.slide;
        auto* s = v->get("sig");
        e.signature = s && s->isStr() ? s->str : "";
        auto* sl = v->get("slot");
        e.slot = sl && sl->isNum() ? (size_t)sl->num : SIZE_MAX;
        e.records = 1;
        e.how = how.empty() ? "symcache" : ("symcache:" + how);
        hits++;
    }
    auto* pes = root.get("processEventSlot");
    if (pes && pes->isNum()) {
        g_processEventSlot = (size_t)pes->num;
        auto* pesh = root.get("processEventSlotHow");
        g_processEventSlotHow = "symcache: " + std::string(pesh && pesh->isStr() ? pesh->str : "derived earlier");
    }
    // The cache must cover *every* wanted name: a partial hit means a half-written file or a plugin
    // build that wants names the cache predates, and both must force a rescan.
    return hits == g_entries.size();
}

void SaveCache() {
    std::string o = "{\"buildId\":" + JsonStr(Elf().buildId) + ",\"loadBase\":" + std::to_string(Elf().loadBase) +
                    ",\"pluginVersion\":\"" TAKARO_PLUGIN_VERSION "\"" +
                    ",\"processEventSlot\":" +
                    (g_processEventSlot == SIZE_MAX ? "null" : std::to_string(g_processEventSlot)) +
                    ",\"processEventSlotHow\":" + JsonStr(g_processEventSlotHow) +
                    ",\"writtenAt\":" + JsonStr(IsoNowUtc()) + ",\"names\":{";
    bool first = true;
    for (auto& e : g_entries) {
        if (!first) o += ",";
        first = false;
        std::string how = e.how;
        if (how.rfind("symcache:", 0) == 0) how = how.substr(9);
        o += JsonStr(e.name) + ":{\"rva\":" + std::to_string(e.rva) + ",\"how\":" + JsonStr(how) +
             ",\"sig\":" + JsonStr(e.signature) +
             ",\"slot\":" + (e.slot == SIZE_MAX ? "null" : std::to_string(e.slot)) + "}";
    }
    o += "}}";
    if (!WriteFileAtomic(CachePath(), o)) PluginLog("resolve: could not write %s", CachePath().c_str());
}

}  // namespace

// ---------------------------------------------------------------------------------------------

void Resolve::Init() {
    if (g_ready) return;
    g_entries.clear();
    g_entries.reserve(kWantedCount);
    for (size_t i = 0; i < kWantedCount; i++) {
        SymEntry e;
        e.name = kWanted[i].key;
        e.required = kWanted[i].required;
        e.data = kWanted[i].data;
        g_entries.push_back(e);
        g_index[e.name] = i;
        if (e.required) g_requiredWanted++;
    }
    const ElfInfo& elf = Elf();
    PluginLog("resolve: exe=%s type=%s loadBase=0x%llx slide=0x%llx .text=0x%llx+0x%llx buildId=%s "
              "symtab=%zu dynsym=%zu",
              ExePath().c_str(), elf.pie ? "PIE" : "EXEC", (unsigned long long)elf.loadBase,
              (unsigned long long)elf.slide, (unsigned long long)elf.textAddr, (unsigned long long)elf.textSize,
              elf.buildId.c_str(), elf.symtabCount, elf.dynsymCount);
    if (!elf.ok) {
        PluginLog("resolve: ELF parse failed: %s", elf.error.c_str());
        g_ready = true;
        ApplyCapabilities();
        return;
    }
    uint64_t t0 = NowMs();

    // The chain. Order matters and is not negotiable:
    //   1. dynsym-vtable must run first because every rtti-slot want depends on a resolved vtable.
    //   2. the ProcessEvent slot is derived between the two, because it IS a slot index the
    //      rtti-slot strategy then consumes.
    //   3. string-xref runs last: it is the only strategy that can be wrong, so it only ever sees
    //      names the exact strategies could not supply.
    if (LoadCache()) {
        g_cacheHit = true;
        PluginLog("resolve: symcache hit (%s)", CachePath().c_str());
    } else {
        g_strategies[0].available = elf.hasDynsym && elf.dynsymCount > 0;
        g_strategies[1].available = g_strategies[0].available;
        g_strategies[2].available = elf.textSize > 0;
        g_strategies[3].available = g_strategies[0].available;

        for (size_t s = 0; s < kStrategyCount; s++) {
            uint64_t ts = NowMs();
            std::string detail = "skipped";
            size_t got = 0;
            if (g_strategies[s].available) {
                try {
                    if (s == 0) {
                        got = ScanVTables(detail);
                    } else if (s == 1) {
                        // Derive the ProcessEvent slot, then read every wanted slot.
                        DeriveProcessEventSlot();
                        got = ScanSlots(detail);
                    } else if (s == 2) {
                        got = ScanStringXrefs(detail);
                    } else {
                        got = ScanDynSymNames(detail);
                    }
                } catch (...) {
                    detail = "strategy threw";
                }
            } else {
                detail = s == 2 ? ".text absent" : ".dynsym absent";
            }
            g_strategies[s].resolved = got;
            g_strategies[s].detail = detail;
            g_strategies[s].ms = NowMs() - ts;
            PluginLog("resolve: strategy %-13s -> %zu names in %llums (%s)", g_strategies[s].name, got,
                      (unsigned long long)g_strategies[s].ms, detail.c_str());
        }
        SaveCache();
    }
    g_scanMs = NowMs() - t0;

#ifdef TAKARO_DEBUG_CORRUPT_SIG
    // Degrade proof: the named symbol is forcibly discarded, so exactly the capability it backs goes
    // `degraded` while the server keeps running. Never defined in a release build.
    {
        auto it = g_index.find(TAKARO_DEBUG_CORRUPT_SIG);
        if (it != g_index.end()) {
            g_entries[it->second].addr = 0;
            g_entries[it->second].rva = 0;
            g_entries[it->second].how = "debug-corrupted";
            PluginLog("resolve: DEBUG_CORRUPT_SIG discarded %s", TAKARO_DEBUG_CORRUPT_SIG);
        } else {
            PluginLog("resolve: DEBUG_CORRUPT_SIG names '%s' which is not a wanted symbol",
                      TAKARO_DEBUG_CORRUPT_SIG);
        }
    }
#endif

    // Drop anything implausible rather than handing a bad pointer to a caller.
    g_resolved = 0;
    g_requiredResolved = 0;
    for (auto& e : g_entries) {
        if (!e.addr) continue;
        if (!e.data && !PlausibleCode(e.addr)) {
            PluginLog("resolve: REJECT %s addr=0x%llx (outside an executable segment, or padding)", e.name.c_str(),
                      (unsigned long long)e.addr);
            e.addr = 0;
            e.how = "rejected";
            continue;
        }
        g_resolved++;
        if (e.required) g_requiredResolved++;
    }

    PluginLog("resolve: %zu/%zu names (%zu/%zu required) in %llums (%s)", g_resolved, g_entries.size(),
              g_requiredResolved, g_requiredWanted, (unsigned long long)g_scanMs, g_cacheHit ? "cache" : "scan");
    g_ready = true;
    ApplyCapabilities();
}

void Resolve::ApplyCapabilities() {
    std::unordered_map<std::string, std::vector<std::string>> missing;
    std::unordered_set<std::string> all;
    for (size_t i = 0; i < kCapabilityCount; i++) {
        all.insert(kCapabilities[i].capability);
        if (!Resolve::Addr(kCapabilities[i].want)) missing[kCapabilities[i].capability].push_back(kCapabilities[i].want);
    }
    auto& st = PluginState::Get();
    for (auto& cap : all) {
        auto it = missing.find(cap);
        if (it == missing.end()) {
            st.SetCapability(cap, "ok", "");
            continue;
        }
        std::string names;
        for (auto& n : it->second) names += (names.empty() ? "" : ", ") + n;
        st.SetCapability(cap, "degraded", "unresolved: " + names);
    }
    st.SetCapability("symbolResolution",
                     g_requiredResolved == g_requiredWanted && g_requiredWanted > 0 ? "ok" : "degraded",
                     std::to_string(g_requiredResolved) + "/" + std::to_string(g_requiredWanted) +
                         " required symbols resolved");
}

bool Resolve::Ready() { return g_ready; }

uint64_t Resolve::Addr(const char* name) {
    auto it = g_index.find(name);
    if (it == g_index.end()) return 0;
    return g_entries[it->second].addr;
}

const SymEntry* Resolve::Entry(const char* name) {
    auto it = g_index.find(name);
    if (it == g_index.end()) return nullptr;
    return &g_entries[it->second];
}

const std::vector<SymEntry>& Resolve::All() { return g_entries; }

size_t Resolve::ProcessEventSlot() { return g_processEventSlot; }

void Resolve::NoteProcessEventFired() { g_processEventConfirmed.store(true, std::memory_order_relaxed); }
bool Resolve::ProcessEventConfirmed() { return g_processEventConfirmed.load(std::memory_order_relaxed); }

void Resolve::SetProcessEventSlot(size_t slot, const char* how) {
    if (slot == SIZE_MAX) return;
    g_processEventSlot = slot;
    g_processEventSlotHow = how ? how : "live vtable";
    PluginLog("resolve: ProcessEvent vtable slot = %zu (%s)", slot, g_processEventSlotHow.c_str());
}

std::string Resolve::StatsJson() {
    std::string strat = "[";
    for (size_t i = 0; i < kStrategyCount; i++) {
        if (i) strat += ",";
        strat += "{\"name\":\"" + std::string(g_strategies[i].name) + "\",\"available\":" +
                 (g_strategies[i].available ? "true" : "false") +
                 ",\"resolved\":" + std::to_string(g_strategies[i].resolved) +
                 ",\"ms\":" + std::to_string(g_strategies[i].ms) + ",\"detail\":" + JsonStr(g_strategies[i].detail) +
                 "}";
    }
    strat += "]";
    return "{\"resolved\":" + std::to_string(g_resolved) + ",\"wanted\":" + std::to_string(g_entries.size()) +
           ",\"required\":" + std::to_string(g_requiredWanted) +
           ",\"requiredResolved\":" + std::to_string(g_requiredResolved) + ",\"processEventSlot\":" +
           (g_processEventSlot == SIZE_MAX ? "null" : std::to_string(g_processEventSlot)) +
           ",\"processEventSlotHow\":" + JsonStr(g_processEventSlotHow) +
           ",\"processEventSlotConfirmed\":" + (g_processEventConfirmed.load() ? "true" : "false") +
           ",\"exportedVTables\":" + std::to_string(VTableSymbols().size()) + ",\"strategies\":" + strat + "}";
}

std::string Resolve::CacheJson() {
    return "{\"path\":" + JsonStr(CachePath()) +
           ",\"buildId\":" + JsonStr(Elf().buildId) + ",\"hit\":" + (g_cacheHit ? "true" : "false") +
           ",\"loadMs\":" + std::to_string(g_scanMs) + "}";
}

std::vector<std::pair<std::string, bool>> Resolve::SelfChecks() {
    std::vector<std::pair<std::string, bool>> out;
    auto add = [&](const std::string& s, bool ok) { out.push_back({s, ok}); };
    const ElfInfo& e = Elf();
    char b[512];

    snprintf(b, sizeof b, "elf: %s loadBase=0x%llx slide=0x%llx text=0x%llx+0x%llx buildId=%s dynsym=%zu",
             e.pie ? "PIE" : "EXEC", (unsigned long long)e.loadBase, (unsigned long long)e.slide,
             (unsigned long long)e.textAddr, (unsigned long long)e.textSize, e.buildId.c_str(), e.dynsymCount);
    add(b, e.ok);

    // _init / _fini vs the section headers: proves the address arithmetic (loadBase + slide) before
    // a single game address is trusted. On a stripped binary these are the ONLY function names left,
    // which is exactly why they are the anchor.
    uint64_t initA = Resolve::Addr("_init"), finiA = Resolve::Addr("_fini");
    bool ok = initA && e.initAddr && initA == e.initAddr + e.slide;
    snprintf(b, sizeof b, "_init: sym=0x%llx elfSection+slide=0x%llx", (unsigned long long)initA,
             (unsigned long long)(e.initAddr + e.slide));
    add(b, ok);
    ok = finiA && e.finiAddr && finiA == e.finiAddr + e.slide;
    snprintf(b, sizeof b, "_fini: sym=0x%llx elfSection+slide=0x%llx", (unsigned long long)finiA,
             (unsigned long long)(e.finiAddr + e.slide));
    add(b, ok);

    // The RTTI bedrock. Everything else on this build is built on top of these two numbers.
    size_t vts = VTableSymbols().size();
    snprintf(b, sizeof b, "exportedVTables: %zu `_ZTV*` symbols in .dynsym (this build is stripped; RTTI is the "
             "whole symbol story)", vts);
    add(b, vts > 1000);

    VTableInfo uo = VTable("_ZTV7UObject");
    snprintf(b, sizeof b, "_ZTV7UObject: addr=0x%llx size=%llu slots=%zu typeinfo=0x%llx%s%s",
             (unsigned long long)uo.addr, (unsigned long long)uo.size, uo.slots,
             (unsigned long long)uo.typeInfo, uo.ok ? "" : " ERROR: ", uo.ok ? "" : uo.error.c_str());
    add(b, uo.ok && uo.slots > 4);

    // The live-vs-file-image guard. If this check fails the plugin is reading unrelocated data and
    // every slot would resolve to 0 — the single most dangerous failure mode on a PIE.
    std::vector<uint64_t> slots;
    std::string err;
    bool live = VTableSlotsLive(uo, uo.slots ? uo.slots : 32, slots, err);
    snprintf(b, sizeof b, "vtableIsLive: %zu slots read from live memory%s%s", slots.size(),
             live ? "" : " — ", live ? "" : err.c_str());
    add(b, live);

    ok = g_processEventSlot != SIZE_MAX;
    snprintf(b, sizeof b, "processEventSlot: %s (%s)", ok ? std::to_string(g_processEventSlot).c_str() : "unresolved",
             g_processEventSlotHow.c_str());
    add(b, ok);

    // Not a failure at boot (nothing has called into the engine yet), but it is the only real proof
    // that the derived slot was right, so it is reported from the first request onwards.
    snprintf(b, sizeof b, "processEventSlotConfirmed: %s (a detour has%s fired)",
             g_processEventConfirmed.load() ? "yes" : "not yet", g_processEventConfirmed.load() ? "" : " not");
    add(b, true);

    // The `.eh_frame_hdr` oracle. Reported even when unavailable, because "we validated every
    // address against 918 821 known function entries" and "we only checked it was in .text" are very
    // different claims and /health must not blur them.
    snprintf(b, sizeof b, "ehFrameOracle: %s", EhFrameDetail().c_str());
    add(b, EhFrameAvailable());

    // GUObjectArray was resolved by two independent byte patterns, from two different functions
    // (UObjectBaseInit's call to AllocateObjectPool, and the inlined
    // FChunkedFixedUObjectArray::GetObjectPtr). The second yields `ObjObjects.Objects`, which is the
    // base + 0x10. If they disagree, at least one pattern matched the wrong place and the
    // enumeration capability must NOT be used — a wrong GUObjectArray means iterating garbage
    // pointers as if they were UObjects.
    uint64_t gua = Resolve::Addr("GUObjectArray"), guaObjs = Resolve::Addr("GUObjectArray.Objects");
    if (gua && guaObjs) {
        ok = guaObjs == gua + 0x10;
        snprintf(b, sizeof b, "GUObjectArray: base=0x%llx ObjObjects.Objects=0x%llx (two independent "
                 "signatures; must differ by exactly 0x10)",
                 (unsigned long long)gua, (unsigned long long)guaObjs);
    } else {
        ok = false;
        snprintf(b, sizeof b, "GUObjectArray: base=%s ObjObjects.Objects=%s (both signatures must resolve)",
                 gua ? "ok" : "unresolved", guaObjs ? "ok" : "unresolved");
    }
    add(b, ok);

    ok = g_requiredWanted > 0 && g_requiredResolved == g_requiredWanted;
    snprintf(b, sizeof b, "requiredSymbols: %zu of %zu required names resolved", g_requiredResolved, g_requiredWanted);
    add(b, ok);

    std::string strategies;
    for (size_t i = 0; i < kStrategyCount; i++)
        if (g_strategies[i].resolved)
            strategies += (strategies.empty() ? "" : ", ") + std::string(g_strategies[i].name) + "=" +
                          std::to_string(g_strategies[i].resolved);
    if (g_cacheHit) strategies = "symcache (build id " + e.buildId + ")";
    snprintf(b, sizeof b, "strategy: %zu of %zu names resolved via %s", g_resolved, g_entries.size(),
             strategies.empty() ? "nothing" : strategies.c_str());
    add(b, g_resolved > 0);
    return out;
}

// ---------------------------------------------------------------------------------------------
// /proc/self/maps guard

namespace {
struct MapRange {
    uintptr_t lo, hi;
    bool r, w;
    std::string path;
};
Mutex g_mapsLock;
std::vector<MapRange> g_maps;
uint64_t g_mapsAt = 0;

// LANE L9 (performance): the hot paths - our ProcessEvent detour and every reflected read behind it
// - call MemReadable several times per engine RPC. Taking a mutex and re-reading /proc/self/maps
// there cost 2.9 us per ProcessEvent call on the live rig. The authoritative table below is still
// rebuilt under the mutex, but each rebuild *publishes an immutable copy* that readers consult
// without any lock. A snapshot is never freed (a few KB, rebuilt only when the map layout changes),
// so a reader can hold the pointer for as long as it likes.
struct MapSnapshot {
    std::vector<MapRange> ranges;
};
std::atomic<const MapSnapshot*> g_snap{nullptr};

void PublishSnapshotLocked() {
    auto* snap = new MapSnapshot();
    snap->ranges = g_maps;
    g_snap.store(snap, std::memory_order_release);
}

// Lock-free lookup in the published snapshot. `false` only means "not in this snapshot"; the caller
// decides whether that is worth a locked refresh.
bool SnapshotHas(uintptr_t a, size_t len, bool needWrite) {
    const MapSnapshot* snap = g_snap.load(std::memory_order_acquire);
    if (!snap) return false;
    const std::vector<MapRange>& v = snap->ranges;
    auto it = std::upper_bound(v.begin(), v.end(), a,
                               [](uintptr_t x, const MapRange& m) { return x < m.lo; });
    if (it == v.begin()) return false;
    --it;
    return a >= it->lo && a + len <= it->hi && it->r && (!needWrite || it->w);
}

void ReloadMapsLocked() {
    std::string text;
    if (!ReadFile("/proc/self/maps", text)) return;
    std::vector<MapRange> out;
    size_t pos = 0;
    while (pos < text.size()) {
        size_t e = text.find('\n', pos);
        std::string line = text.substr(pos, e == std::string::npos ? std::string::npos : e - pos);
        pos = e == std::string::npos ? text.size() : e + 1;
        unsigned long long lo = 0, hi = 0;
        char perms[8] = {0};
        char pathbuf[1024] = {0};
        int got = sscanf(line.c_str(), "%llx-%llx %7s %*s %*s %*s %1023[^\n]", &lo, &hi, perms, pathbuf);
        if (got < 3) continue;
        out.push_back({(uintptr_t)lo, (uintptr_t)hi, perms[0] == 'r', perms[1] == 'w', pathbuf});
    }
    std::sort(out.begin(), out.end(), [](const MapRange& a, const MapRange& b) { return a.lo < b.lo; });
    g_maps.swap(out);
    g_mapsAt = NowMs();
    PublishSnapshotLocked();
}

bool Check(const void* addr, size_t len, bool needWrite) {
    if (!addr || !len) return false;
    uintptr_t a = (uintptr_t)addr;
    if (a + len < a) return false;
    if (SnapshotHas(a, len, needWrite)) return true;  // lock-free fast path (the overwhelming case)
    Guard g(g_mapsLock);
    for (int attempt = 0; attempt < 2; attempt++) {
        if (g_maps.empty() || (attempt == 1)) ReloadMapsLocked();
        auto it = std::upper_bound(g_maps.begin(), g_maps.end(), a,
                                   [](uintptr_t v, const MapRange& m) { return v < m.lo; });
        if (it != g_maps.begin()) {
            --it;
            if (a >= it->lo && a + len <= it->hi && it->r && (!needWrite || it->w)) return true;
        }
        // A miss may just mean a stale cache; refresh once (rate-limited) and retry.
        if (attempt == 0 && NowMs() - g_mapsAt < 250) return false;
    }
    return false;
}
}  // namespace

bool MemReadable(const void* addr, size_t len) { return Check(addr, len, false); }
bool MemWritable(const void* addr, size_t len) { return Check(addr, len, true); }
void MemMapsRefresh() {
    Guard g(g_mapsLock);
    ReloadMapsLocked();
}

std::string MemMapsSelfSoLine() {
    Dl_info info{};
    if (!dladdr((void*)&MemMapsSelfSoLine, &info) || !info.dli_fname) return "";
    std::string text, out;
    if (!ReadFile("/proc/self/maps", text)) return "";
    size_t pos = 0;
    while (pos < text.size()) {
        size_t e = text.find('\n', pos);
        std::string line = text.substr(pos, e == std::string::npos ? std::string::npos : e - pos);
        pos = e == std::string::npos ? text.size() : e + 1;
        if (line.find(info.dli_fname) != std::string::npos) out += line + "\n";
    }
    return out;
}
