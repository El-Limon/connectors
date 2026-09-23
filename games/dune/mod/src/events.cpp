#include "events.h"

#include "events_parse.h"
#include "gamethread.h"
#include "hooks.h"
#include "livehooks.h"
#include "objpool.h"
#include "perf.h"
#include "query.h"
#include "players.h"
#include "reflect.h"
#include "resolve.h"
#include "state.h"
#include "uobj.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>

namespace {

// ================================================================================================
// 1. THE FILTER TABLE
// ================================================================================================

enum Kind : uint8_t {
    K_None = 0,
    K_CritterDeathServer,       // DuneCritterBase::OnDeathOrDefeatOnServer  — self = victim
    K_CritterBPOnDeath,         // DuneCritterBase::BPOnDeath                — self = victim
    K_CharDeathOrDefeat,        // DuneCharacter::ReceiveMulticastDeathOrDefeat — self = victim
    K_KillCharacter,            // DuneCharacter::KillCharacter              — self = victim
    K_MulticastKill,            // DuneCharacter::ReceiveMulticastKill       — self = KILLER (hint only)
    K_Count
};

const char* KindName(uint8_t k) {
    switch (k) {
        case K_CritterDeathServer: return "OnDeathOrDefeatOnServer";
        case K_CritterBPOnDeath: return "BPOnDeath";
        case K_CharDeathOrDefeat: return "ReceiveMulticastDeathOrDefeat";
        case K_KillCharacter: return "KillCharacter";
        case K_MulticastKill: return "ReceiveMulticastKill";
        default: return "?";
    }
}
// Is `self` the victim for this kind? Only direction-unambiguous functions say yes; see events.h.
bool SelfIsVictim(uint8_t k) { return k != K_MulticastKill && k != K_None; }

struct WantedFn {
    const char* name;
    uint8_t kind;
    const char* classes[4];  // where to look for it; the FName index is global, the UFunction is not
};
const WantedFn kWanted[] = {
    {"OnDeathOrDefeatOnServer", K_CritterDeathServer, {"DuneCritterBase", nullptr}},
    {"BPOnDeath", K_CritterBPOnDeath, {"DuneCritterBase", nullptr}},
    {"ReceiveMulticastDeathOrDefeat", K_CharDeathOrDefeat, {"DuneCharacter", "DunePlayerCharacter", "DuneNpcCharacter", nullptr}},
    {"KillCharacter", K_KillCharacter, {"DuneCharacter", "DunePlayerCharacter", "DuneNpcCharacter", nullptr}},
    {"ReceiveMulticastKill", K_MulticastKill, {"DuneCharacter", "DunePlayerCharacter", "DuneNpcCharacter", nullptr}},
};
constexpr size_t kWantedCount = sizeof kWanted / sizeof kWanted[0];

// The hot table. `idx` is an FName comparison index; `kind` the enum above. Written once at Init
// (before any hit can be classified) and read without a lock on the game thread, which is safe
// because it is only ever written while g_filterCount is still 0 and published last.
struct FilterRow {
    uint32_t idx;
    uint8_t kind;
};
FilterRow g_filter[16];
std::atomic<size_t> g_filterCount{0};
uint32_t g_filterMin = 0xffffffffu, g_filterMax = 0;
uint32_t g_nameOff = 0x18;  // Reflect::Lay().objName, captured once

// ================================================================================================
// 2. PARAMETER PLANS
// ================================================================================================
//
// A UFunction is a UStruct, so its parameters are its own FField chain: name, type and offset, read
// with Pool::PropsOf. Roles are matched on the decoded parameter NAME, case-insensitively, in
// priority order. Nothing is positional and nothing is hardcoded — and when no parameter matches a
// role, that role is simply absent from the plan and the emitted field is null.

struct Role {
    int32_t off = -1;
    // How to READ it. `InstigatorInfo`'s members are WeakObjectProperty — an {index, serial} pair, not
    // a pointer — so the reader has to be chosen from the property's type, not assumed.
    bool weak = false;
    // LANE L2c. True for a `ClassProperty` and friends: the 8 bytes in the frame ARE a `UClass*`, not a
    // pointer to an instance of one. Reading "the class of the object at this pointer" therefore
    // answers "the class of a class" — which is exactly how every live death reported
    // `weaponCode: "BlueprintGeneratedClass"`. The pointed-to object's OWN name is the answer.
    bool holdsClass = false;
    char name[48] = {0};
};
struct ParamPlan {
    void* func = nullptr;
    uint8_t kind = K_None;
    uint32_t frameSize = 0;
    Role victim, killer, causer, weapon;
    // MEASURED ON THE LIVE BUILD (lane L2, first deploy): none of Dune's death UFunctions takes a bare
    // actor pointer. All four take `InstigatorInfo`, a 16-byte StructProperty at parameter offset 0,
    // plus a `bIsDeath` BoolProperty. So the plan carries:
    //   * `instigator`  — the offset of the InstigatorInfo struct in the parameter frame, and the
    //     offsets of the object-reference members INSIDE it, resolved from the UScriptStruct's own
    //     FField chain (found by a unique name sweep; see FindScriptStruct).
    //   * `isDeath`     — the offset of `bIsDeath`. This matters for correctness, not just detail:
    //     these functions fire for a DEFEAT (downed-but-not-out) as well as a death, and reporting a
    //     knock-down as a death would be wrong. `bIsDeath == false` is dropped.
    int32_t instigatorOff = -1;
    char instigatorName[48] = {0};
    char instigatorStruct[64] = {0};
    Role instKiller, instCauser, instWeapon;  // offsets WITHIN InstigatorInfo
    int32_t isDeathOff = -1;
    // LANE L2b. `bShouldEnterDbno` — "down but not out" — is the engine's own knock-down flag and the
    // only parameter on any of these functions that actually means "not dead". Only `KillCharacter`
    // carries it on this build; -1 everywhere else, which is why rule 3 of the decision table can
    // only ever refuse, never assert. See the long note in events_parse.h.
    int32_t dbnoOff = -1;
    Role instControllerRole;  // `m_Controller` — a player killer's controller IS the registry key
    uint32_t paramCount = 0;
    char summary[512] = {0};
    char instSummary[384] = {0};
    bool ready = false;
};
constexpr size_t kMaxPlans = 64;
ParamPlan g_plans[kMaxPlans];
std::atomic<size_t> g_planCount{0};

/// Off-thread only. Finds a `UScriptStruct` whose decoded name contains `fragment`, by sweeping
/// GUObjectArray for objects whose own class is `ScriptStruct`.
///
/// This is needed because a `StructProperty`'s inner `UScriptStruct` is NOT on the FField chain: the
/// chain gives the property's name, type and offset, and the pointer to the struct lives at an
/// FProperty offset this build gives us no way to derive. Sweeping for the struct by name is the
/// derivation that IS available — and it keeps the codebase's uniqueness rule: `count` is returned so
/// the caller can refuse anything that is not exactly one match, rather than taking the first.
void* FindScriptStruct(const char* fragment, size_t& count, std::string* nameOut) {
    count = 0;
    void* best = nullptr;
    std::string bestName;
    std::string frag = fragment;
    for (auto& c : frag) c = (char)tolower((unsigned char)c);
    Obj::ForEach([&](void* o) {
        void* cls = U::ReadPtr(o, Reflect::Lay().objClass);
        if (!cls) return true;
        std::string cn = Classes::NameOf(cls);
        if (cn != "ScriptStruct") return true;
        std::string n = U::NameOf(o);
        if (n.empty()) return true;
        std::string low = n;
        for (auto& c : low) c = (char)tolower((unsigned char)c);
        if (low.find(frag) == std::string::npos) return true;
        count++;
        if (!best) {
            best = o;
            bestName = n;
        }
        return true;
    });
    if (nameOut) *nameOut = bestName;
    return best;
}

/// Lists every ScriptStruct matching a fragment, with its properties. `/debug/scriptstructs`.
std::string ScriptStructsJson(const std::string& match, size_t limit) {
    std::string frag = match;
    for (auto& c : frag) c = (char)tolower((unsigned char)c);
    std::string o = "{\"match\":" + JsonStr(match) + ",\"structs\":[";
    size_t n = 0;
    Obj::ForEach([&](void* obj) {
        if (n >= limit) return false;
        void* cls = U::ReadPtr(obj, Reflect::Lay().objClass);
        if (!cls || Classes::NameOf(cls) != "ScriptStruct") return true;
        std::string name = U::NameOf(obj);
        if (name.empty()) return true;
        std::string low = name;
        for (auto& c : low) c = (char)tolower((unsigned char)c);
        if (!frag.empty() && low.find(frag) == std::string::npos) return true;
        if (n) o += ",";
        n++;
        bool ok = false;
        uint32_t size = U::StructPropertiesSize(obj, &ok);
        o += "{\"name\":" + JsonStr(name) + ",\"size\":" + std::to_string(size) + ",\"properties\":[";
        auto props = Pool::PropsOf(obj, 64);
        for (size_t i = 0; i < props.size(); i++) {
            if (i) o += ",";
            o += "{\"name\":" + JsonStr(props[i].name) + ",\"type\":" + JsonStr(props[i].type) +
                 ",\"offset\":" + std::to_string(props[i].offset) + "}";
        }
        o += "]}";
        return true;
    });
    return o + "],\"returned\":" + std::to_string(n) + "}";
}

/// Off-thread only (it allocates). Builds the plan for one UFunction.
void BuildPlan(ParamPlan& plan, void* func, uint8_t kind) {
    plan.func = func;
    plan.kind = kind;
    bool ok = false;
    plan.frameSize = U::StructPropertiesSize(func, &ok);
    auto raw = Pool::PropsOf(func, 64);
    plan.paramCount = (uint32_t)raw.size();
    std::vector<EventsParse::ParamProp> props;
    std::string summary;
    for (const auto& p : raw) {
        props.push_back({p.name, p.type, p.offset});
        if (!summary.empty()) summary += ", ";
        char b[96];
        snprintf(b, sizeof b, "%s:%s@0x%x", p.name.c_str(), p.type.c_str(), (unsigned)p.offset);
        summary += b;
    }
    // The role decision itself lives in events_parse.cpp, which is unit-tested on the host: getting
    // victim and killer the wrong way round is the one mistake here that an idle server cannot reveal.
    struct Hunt {
        Role* role;
        EventsParse::ParamRole which;
    };
    const Hunt hunts[] = {
        {&plan.victim, EventsParse::ParamRole::Victim},
        {&plan.killer, EventsParse::ParamRole::Killer},
        {&plan.causer, EventsParse::ParamRole::Causer},
        {&plan.weapon, EventsParse::ParamRole::Weapon},
    };
    for (const Hunt& h : hunts) {
        int i = EventsParse::MatchParamRole(props, h.which);
        if (i < 0) continue;
        h.role->off = props[(size_t)i].offset;
        h.role->weak = props[(size_t)i].type == "WeakObjectProperty";
        h.role->holdsClass = EventsParse::PropertyHoldsClassPointer(props[(size_t)i].type);
        snprintf(h.role->name, sizeof h.role->name, "%s", props[(size_t)i].name.c_str());
    }
    snprintf(plan.summary, sizeof plan.summary, "%s", summary.empty() ? "(no parameters)" : summary.c_str());

    // `bIsDeath` (a descriptor) and `bShouldEnterDbno` (the real knock-down gate).
    for (const auto& p : raw) {
        if (p.name == "bIsDeath" && p.offset >= 0) plan.isDeathOff = p.offset;
        if (p.name == "bShouldEnterDbno" && p.offset >= 0) plan.dbnoOff = p.offset;
    }

    // `InstigatorInfo`: the struct that actually carries the killer on this build.
    for (const auto& p : raw) {
        if (p.type != "StructProperty" || p.offset < 0) continue;
        std::string low = p.name;
        for (auto& c : low) c = (char)tolower((unsigned char)c);
        if (low.find("instigator") == std::string::npos) continue;
        plan.instigatorOff = p.offset;
        snprintf(plan.instigatorName, sizeof plan.instigatorName, "%s", p.name.c_str());
        size_t matches = 0;
        std::string structName;
        void* st = FindScriptStruct("instigatorinfo", matches, &structName);
        if (!st || matches != 1) {
            // The uniqueness rule: more than one candidate means we cannot say which struct this is,
            // so the killer stays unresolved and the event says `attribution: paramsUnresolved`.
            snprintf(plan.instSummary, sizeof plan.instSummary,
                     "%zu ScriptStructs matched 'InstigatorInfo' - refusing to pick one", matches);
            break;
        }
        snprintf(plan.instigatorStruct, sizeof plan.instigatorStruct, "%s", structName.c_str());
        auto members = Pool::PropsOf(st, 64);
        std::vector<EventsParse::ParamProp> mp;
        std::string ms;
        for (const auto& m : members) {
            mp.push_back({m.name, m.type, m.offset});
            if (!ms.empty()) ms += ", ";
            char b[96];
            snprintf(b, sizeof b, "%s:%s@0x%x", m.name.c_str(), m.type.c_str(), (unsigned)m.offset);
            ms += b;
        }
        snprintf(plan.instSummary, sizeof plan.instSummary, "%s {%s}", structName.c_str(),
                 ms.empty() ? "no members" : ms.c_str());
        // The same tested matcher, one level down. `killer` covers Instigator/Attacker names;
        // `causer`/`weapon` cover the damage source.
        const struct { Role* role; EventsParse::ParamRole which; } inner[] = {
            {&plan.instKiller, EventsParse::ParamRole::Killer},
            {&plan.instCauser, EventsParse::ParamRole::Causer},
            {&plan.instWeapon, EventsParse::ParamRole::Weapon},
        };
        // The controller is matched by NAME because it is the single best identity link there is: our
        // player registry is keyed on the APlayerController pointer that PostLogin handed us, so a
        // killer's `m_Controller` resolves to a tracked player with no further walking at all.
        for (const auto& m : members) {
            std::string ml = m.name;
            for (auto& c : ml) c = (char)tolower((unsigned char)c);
            if (m.offset < 0 || ml.find("controller") == std::string::npos) continue;
            plan.instControllerRole.off = m.offset;
            plan.instControllerRole.weak = m.type == "WeakObjectProperty";
            snprintf(plan.instControllerRole.name, sizeof plan.instControllerRole.name, "%s", m.name.c_str());
            break;
        }
        for (const auto& h : inner) {
            int i = EventsParse::MatchParamRole(mp, h.which);
            if (i < 0) continue;
            h.role->off = mp[(size_t)i].offset;
            h.role->weak = mp[(size_t)i].type == "WeakObjectProperty";
            snprintf(h.role->name, sizeof h.role->name, "%s", mp[(size_t)i].name.c_str());
        }
        // An InstigatorInfo whose members name no killer at all: fall back to the FIRST object
        // reference in it, because a one-object struct called InstigatorInfo has only one thing it can
        // be — and record that this is what happened, so the report says so rather than implying a
        // name match.
        if (plan.instKiller.off < 0) {
            for (const auto& m : members) {
                if (m.offset < 0 || !EventsParse::IsObjectPropertyType(m.type)) continue;
                plan.instKiller.off = m.offset;
                plan.instKiller.weak = m.type == "WeakObjectProperty";
                snprintf(plan.instKiller.name, sizeof plan.instKiller.name, "%s (first object member)", m.name.c_str());
                break;
            }
        }
        break;
    }
    (void)ok;
    plan.ready = true;
}

/// GAME THREAD. Linear scan over at most a couple of dozen entries, no allocation, no lock.
const ParamPlan* PlanFor(void* func) {
    size_t n = g_planCount.load(std::memory_order_acquire);
    for (size_t i = 0; i < n; i++)
        if (g_plans[i].func == func) return &g_plans[i];
    return nullptr;
}

// Functions the filter matched but had no plan for (a Blueprint override gets its own UFunction with
// the same name). Recorded here and turned into a plan by Housekeep(), so the SECOND such kill is
// fully attributed and the first one is reported honestly as "paramsUnresolved".
struct Unplanned {
    void* func;
    uint8_t kind;
};
std::atomic<size_t> g_unplannedCount{0};
Unplanned g_unplanned[32];
std::atomic<uint64_t> g_unplannedDropped{0};

// ================================================================================================
// 3. THE HIT RING (single writer: the game thread)
// ================================================================================================

struct RawHit {
    uint8_t kind;
    uint64_t tsMs;
    void* self;
    void* victim;
    void* killer;
    void* causer;
    void* weapon;
    void* killerController;  // InstigatorInfo::m_Controller — the player registry's own key
    uint32_t selfClassIdx, victimClassIdx, killerClassIdx, causerClassIdx, weaponClassIdx;
    uint32_t victimDevNameIdx;  // DuneNpcCharacter::m_Name, when present
    // LANE L2c. The damage-TYPE class's own name, read through the ClassProperty rule rather than as
    // "the class of the object at this pointer" (which is what produced `BlueprintGeneratedClass`).
    uint32_t damageTypeNameIdx;
    bool weaponIsClassPtr;  // the weapon role that answered was a ClassProperty
    // LANE L2d. The KILLER's wielded weapon, snapshotted as raw primitives on the game thread: two
    // FName comparison indices, two class POINTERS (only ever compared, never dereferenced) and two
    // flags. Nothing here is decoded or interpreted on this path, and no engine object is touched
    // again after the hit is enqueued — so a pawn that dies between the hook and the drain cannot be
    // dereferenced at all.
    uint32_t wieldedRangedNameIdx;  // WeaponActorComponent::m_WeaponName
    uint32_t wieldedMeleeNameIdx;   // m_CachedMeleeWeaponData.m_CachedMeleeWeaponName
    uint32_t wieldedRangedNameNum;  // the FName Number half; non-zero means a suffixed name -> refuse
    uint32_t wieldedMeleeNameNum;
    void* wieldedRangedDamageType;  // WeaponActorComponent::m_CachedWeaponDamageType
    void* wieldedMeleeDamageType;   // m_CachedMeleeWeaponData.m_DamageTypeClass
    void* deathDamageTypeCls;       // the damage-type CLASS the frame carried, for the pointer compare
    bool weaponInHand, weaponComponentActive;
    bool weaponCharacterFound;  // a DuneCharacter-derived killer was located to read from
    // LANE L2c. THE classification input: the victim's UClass Super-chain derives a player class. A
    // class chain is readable for as long as the object is and survives every respawn, which the
    // registry's cached pawn pointer does not — see events_parse.h and players.h for the live
    // evidence (/events seq 4 and seq 5).
    bool victimClassIsPlayer;
    const char* victimIdentityHow;  // static string from Players::IdentifyActor
    double x, y, z;
    bool havePos;
    bool paramsResolved;
    bool selfIsPlayer;
    bool victimIsPlayer;
    bool killerIsPlayer;
    uint8_t lifeState, deathReason;
    bool haveReason;
    // LANE L2b. The two bool parameters are carried as the RAW BYTE plus an ok flag, never as a
    // pre-interpreted bool: `EventsParse::ReadFrameBool` decides off-thread whether the byte is even
    // a bool (see the bitfield note in events_parse.h), and `/debug/deathlog` publishes the byte so a
    // wrong offset can be seen rather than guessed at.
    uint8_t isDeathRaw, dbnoRaw;
    bool isDeathOk, dbnoOk;
    // The victim's PlayerState, captured here so the drain can re-read `m_LifeState` a moment later
    // without re-entering the game thread or re-walking the registry.
    void* victimPlayerState;
    // A bounded copy of the parameter frame, for /debug/deathlog only. One memcpy of ≤64 bytes on a
    // path that fires a handful of times per minute; the frames on this build are 32-40 bytes.
    uint16_t rawLen;
    uint8_t rawParams[64];
};
constexpr size_t kRing = 512;
RawHit g_ring[kRing];
std::atomic<uint64_t> g_ringHead{0};  // written by the game thread
std::atomic<uint64_t> g_ringTail{0};  // written by the housekeeping thread
std::atomic<uint64_t> g_ringDropped{0};
std::atomic<uint64_t> g_hits[K_Count] = {};

// Cached offsets, all warmed off-thread at Init so the detour never walks a property list.
struct Warm {
    int32_t rootComponent = -1;
    int32_t relativeLocation = -1;
    int32_t npcDevName = -1;
    int32_t lifeState = -1;
    int32_t lastDeathReason = -1;
    void* playerStateCls = nullptr;
    void* dunePlayerCharacterCls = nullptr;
    void* dunePlayerControllerCls = nullptr;
    void* duneCharacterCls = nullptr;
    void* critterCls = nullptr;
    void* staticCritterCls = nullptr;
    void* npcCls = nullptr;
    void* npcCivilianCls = nullptr;
    void* actorCls = nullptr;
    // LANE L2d. The wielded-weapon offsets, all resolved by PROPERTY NAME off-thread at InitLive. A
    // name that is not on this build yields -1 and the corresponding fact stays false, so the weapon
    // degrades to null instead of reading a guessed address.
    void* weaponComponentCls = nullptr;
    int32_t weaponComponentOff = -1;    // ADuneCharacter::m_WeaponComponent
    int32_t meleeDataOff = -1;          // ADuneCharacter::m_CachedMeleeWeaponData (inline struct)
    int32_t hasWeaponInHandOff = -1;    // ADuneCharacter::m_bHasWeaponInHand
    // ⚠️ On this build `m_bHasWeaponInHand` and `m_bServerWantsWeaponInHand` reflect at the SAME byte
    // offset (7134), i.e. they are a PACKED C++ bitfield pair, and the FField chain here exposes no
    // ByteMask (see the bitfield note in events_parse.h). A whole-byte read therefore cannot tell the
    // two apart, so the byte is read as "either weapon-in-hand flag is set" and this flag says so.
    bool weaponInHandIsPackedPair = false;
    int32_t meleeNameOff = -1;          // ...within FCachedMeleeWeaponData
    int32_t meleeDamageTypeOff = -1;    // ...within FCachedMeleeWeaponData
    int32_t wacNameOff = -1;            // UWeaponActorComponent::m_WeaponName
    int32_t wacDamageTypeOff = -1;      // UWeaponActorComponent::m_CachedWeaponDamageType
    int32_t wacActiveOff = -1;          // UWeaponActorComponent::m_bActive
    const char* meleeStructNote = "not resolved";
};
Warm g_warm;
std::atomic<bool> g_warmReady{false};

inline uint32_t ClassIdxOf(const void* obj) {
    if (!obj) return 0;
    void* cls = U::ReadPtr(obj, Reflect::Lay().objClass);
    if (!cls) return 0;
    return U::ReadU32(cls, g_nameOff);
}

/// GAME THREAD. Bounded position read: 3 pointer/vector reads through warmed offsets.
inline bool QuickPos(const void* actor, double& x, double& y, double& z) {
    if (!actor || g_warm.rootComponent < 0 || g_warm.relativeLocation < 0) return false;
    void* root = U::ReadPtr(actor, (uint32_t)g_warm.rootComponent);
    if (!root) return false;
    U::Vec v;
    if (!U::ReadVec(root, (uint32_t)g_warm.relativeLocation, v)) return false;
    x = v.x;
    y = v.y;
    z = v.z;
    return true;
}

// ================================================================================================
// 4. CORRELATION AND DEDUPE (housekeeping thread only)
// ================================================================================================

// A single death fires several of these functions (multicast + the server-side one + KillCharacter),
// so a death is emitted ONCE per victim per window.
constexpr uint64_t kDedupeMs = 1500;
struct Recent {
    void* victim;
    uint64_t tsMs;
};
std::vector<Recent> g_recentDeaths;
struct KillHint {
    void* killer;
    void* victim;  // from the parameters, may be null
    void* weapon;
    uint32_t killerClassIdx, weaponClassIdx;
    bool killerIsPlayer;
    uint64_t tsMs;
};
std::vector<KillHint> g_killHints;

Mutex g_lock;
std::vector<std::pair<std::string, std::string>> g_notes;  // {what, detail} for /health
std::atomic<uint64_t> g_emitted[4] = {};                   // entity-killed, player-death, connect, disconnect
std::atomic<uint64_t> g_suppressedAmbiguous{0};
// `defeatsDropped` is KEPT so /health stays a superset of what L2 published, but it no longer counts
// anything: a defeat is a death on this build. It is replaced by the two counters below, which name
// the actual reason a call produced no event.
std::atomic<uint64_t> g_defeatsDropped{0};
std::atomic<uint64_t> g_knockDownsDropped{0};   // bShouldEnterDbno, or a player still Alive after the call
std::atomic<uint64_t> g_duplicatesDropped{0};   // the same victim inside the dedupe window
std::atomic<uint64_t> g_lifeStateReadbacks{0};  // rule-4 read-backs that produced a usable value
std::atomic<uint64_t> g_boolUnreadable{0};      // a bool byte that was neither 0 nor 1 (bitfield canary)
// LANE L2c. Each of these is a live occurrence of one of the four bugs this lane fixed, so a claim that
// the fix works has to point at a number rather than at an intention.
// `victimClassChainOnly` is THE one: a player death the class chain caught and the registry did not —
// i.e. exactly the case that used to be emitted as `entity-killed: BP_DunePlayerCharacter_C`.
std::atomic<uint64_t> g_victimClassChainOnly{0};
std::atomic<uint64_t> g_selfDeaths{0};          // killer == victim: a fall, dehydration, a storm
std::atomic<uint64_t> g_unnamedEntityKills{0};  // entity-killed with no display name -> dropUnnamed
std::atomic<uint64_t> g_connectsHeld{0};        // connects held back for an incomplete identity
std::atomic<uint64_t> g_connectRetries{0};      // retry passes over the pending list
std::atomic<uint64_t> g_connectsIncomplete{0};  // announced anyway after the grace expired
std::atomic<uint64_t> g_connectsRecovered{0};   // announced on a retry with a COMPLETE identity
std::atomic<uint64_t> g_connectsAbandoned{0};   // the player left before the identity ever completed
// LANE L2d. The wielded weapon: how often a real item template id was produced, and why not when not.
std::atomic<uint64_t> g_weaponResolved{0};          // an item template id reached the payload
std::atomic<uint64_t> g_weaponByDamageTypeMatch{0}; // ...of those, by the strong class-pointer compare
std::atomic<uint64_t> g_weaponNoCharacter{0};       // no ADuneCharacter-derived killer to read from
std::atomic<uint64_t> g_weaponNoEvidence{0};        // a killer character, but nothing it carried matched

// ------------------------------------------------------------------------------------------------
// /debug/deathlog — the last N calls the filter caught, with the RAW parameter bytes and the exact
// decision that was taken on them. This exists because L2 dropped every real death and kept no
// record of what it dropped, so the cause could only be guessed at. It is debug-gated and bounded.
constexpr size_t kDeathLog = 32;
struct DeathLogEntry {
    uint64_t seq = 0;
    uint64_t tsMs = 0;
    uint8_t kind = 0;
    std::string selfClass, victimClass, victimName, killerClass;
    bool victimIsPlayer = false;
    // LANE L2c. Kept side by side ON PURPOSE: `victimIsPlayer` is what the registry said and
    // `victimClassIsPlayer` is what the class chain said. The whole bug was the two disagreeing while
    // only the first was consulted, so /debug/deathlog now shows both and a recurrence is visible.
    bool victimClassIsPlayer = false;
    bool identityComplete = true;
    bool dropUnnamed = false;
    const char* classification = "";
    std::string victimIdentityHow;
    bool killerIsTrackedPlayer = false;
    std::string killerRef;
    const char* isDeath = "unreadable";
    const char* dbno = "unreadable";
    bool haveLifeState = false;
    bool lifeStateFresh = false;
    uint8_t lifeState = 0;
    uint64_t drainLagMs = 0;
    std::string rawHex;
    const char* verdict = "";
    const char* reason = "";
    const char* deathKind = "";
    std::string emittedAs;  // "entity-killed" | "player-death" | "" when nothing was emitted
    std::string attribution;
    // LANE L2d. The wielded-weapon audit trail: BOTH candidate names the killer carried, which one was
    // chosen and why. This is what makes "the weapon was wrong" a readable fact rather than a guess —
    // the same reason `victimIsPlayer`/`victimClassIsPlayer` sit side by side above.
    std::string weaponItemCode, weaponMeleeName, weaponRangedName;
    const char* weaponSource = "none";
    const char* weaponReason = "";
    int weaponConfidence = 0;
    bool weaponCharacterFound = false;
    bool weaponMeleeDamageTypeMatch = false;
    bool weaponRangedDamageTypeMatch = false;
};
DeathLogEntry g_deathLog[kDeathLog];
std::atomic<uint64_t> g_deathLogSeq{0};
Mutex g_deathLogLock;

std::string HexBytes(const uint8_t* p, size_t n) {
    static const char* kHex = "0123456789abcdef";
    std::string s;
    s.reserve(n * 3);
    for (size_t i = 0; i < n; i++) {
        if (i && (i % 8) == 0) s += ' ';
        s += kHex[p[i] >> 4];
        s += kHex[p[i] & 0xf];
    }
    return s;
}

const char* VerdictName(EventsParse::DeathVerdict v) {
    switch (v) {
        case EventsParse::DeathVerdict::Emit: return "emit";
        case EventsParse::DeathVerdict::Hint: return "hint";
        case EventsParse::DeathVerdict::DropKnockDown: return "drop:knockDown";
        case EventsParse::DeathVerdict::DropDuplicate: return "drop:duplicate";
    }
    return "?";
}

void DeathLogPush(DeathLogEntry e) {
    e.seq = ++g_deathLogSeq;
    Guard g(g_deathLogLock);
    g_deathLog[(e.seq - 1) % kDeathLog] = std::move(e);
}
// Corroboration counters for the one stock-UE assumption in the weak-pointer reader
// (FUObjectItem::SerialNumber at +0x10). A run where these are all `disagreed` means the offset is
// wrong on this build and the killer resolution should not be believed.
std::atomic<uint64_t> g_weakSerialAgreed{0}, g_weakSerialDisagreed{0};
bool g_inited = false;
std::string g_filterHow = "not built";

void Note(const std::string& what, const std::string& detail) {
    Guard g(g_lock);
    for (auto& n : g_notes)
        if (n.first == what) {
            n.second = detail;
            return;
        }
    g_notes.push_back({what, detail});
}

std::string DecodeName(uint32_t idx) { return idx ? Pool::Name(idx) : std::string(); }

/// Strips the UE class prefix so a code reads `DuneCritterBase`, not `ADuneCritterBase`.
std::string PosJson(double x, double y, double z) {
    return "{\"x\":" + JsonNum(x) + ",\"y\":" + JsonNum(y) + ",\"z\":" + JsonNum(z) + "}";
}

std::string OrNull(const std::string& s) { return s.empty() ? "null" : JsonStr(s); }

// ================================================================================================
// 5. THE DRAIN: raw hits -> events
// ================================================================================================

/// Returns the event type it emitted, and fills `logKillerRef` / `logKillerIsPlayer` for the death
/// log. `deathKind` is the descriptive death/defeat/unknown from the decision table.
std::string EmitDeath(const RawHit& h, void* killer, uint32_t killerClassIdx, bool killerIsPlayer, void* weapon,
                      uint32_t weaponClassIdx, const char* attribution, const char* deathKind,
                      std::string* logKillerRef = nullptr, bool* logKillerIsPlayer = nullptr,
                      EventsParse::VictimClassification* logClass = nullptr, DeathLogEntry* log = nullptr) {
    (void)weapon;
    std::string victimCode = DecodeName(h.victimClassIdx);
    std::string victimDev = DecodeName(h.victimDevNameIdx);
    std::string killerCode = DecodeName(killerClassIdx);
    std::string weaponCode = DecodeName(weaponClassIdx);
    std::string damageTypeCode = DecodeName(h.damageTypeNameIdx);

    Players::Entry victimPlayer, killerPlayer;
    // LANE L2c: the owner walk, so a freshly respawned pawn still resolves. See players.h.
    const char* victimHow = nullptr;
    bool victimIdentified = Players::IdentifyActor(h.victim, victimPlayer, &victimHow);
    // The controller first: the registry is keyed on the controller pointer PostLogin handed us, so
    // `InstigatorInfo::m_Controller` identifies a player killer directly. The actor is the fallback
    // (a pawn is also in the registry) and is what names an NPC killer.
    bool killerIsTrackedPlayer = (h.killerController && Players::Identify(h.killerController, killerPlayer)) ||
                                 (killer && Players::IdentifyActor(killer, killerPlayer));

    // ------------------------------------------------------------------------------------------
    // LANE L2c, RULE 1: CLASSIFY BY THE CLASS CHAIN, IDENTIFY SECOND.
    //
    // The two questions fail independently, and conflating them is what made two of TakaroTest's own
    // deaths arrive as `entity-killed` with `entityCode: "BP_DunePlayerCharacter_C"` (/events seq 4,
    // seq 5). A player victim is now ALWAYS a `player-death`, even when no identity could be resolved —
    // in which case `identityComplete: false` says so and the sidecar resolves the player its own way.
    EventsParse::VictimFacts vf;
    vf.classChainIsPlayer = h.victimClassIsPlayer;
    vf.identityResolved = victimIdentified;
    vf.haveDisplayName = !victimDev.empty() && EventsParse::LooksLikeDisplayName(victimDev);
    EventsParse::VictimClassification vc = EventsParse::ClassifyVictim(vf);
    if (logClass) *logClass = vc;
    if (vc.type == EventsParse::DeathEventType::PlayerDeath && !vc.identityComplete) g_victimClassChainOnly++;

    // ------------------------------------------------------------------------------------------
    // LANE L2c, RULE 2: SELF / ENVIRONMENT.
    //
    // The engine points `InstigatorInfo` at the victim when nothing else killed them, so
    // "killer == victim" is how a fall, dehydration, a sandworm or a Coriolis storm arrives. /events
    // seq 10 reported `killer: {"characterName":"TakaroTest"}` on TakaroTest's own death, which would
    // read in Takaro as a suicide for every environmental death.
    EventsParse::AttributionFacts af;
    af.haveKiller = killer != nullptr || h.killerController != nullptr;
    af.killerIsVictimActor =
        (killer && killer == h.victim) ||
        (victimIdentified && ((killer && (killer == victimPlayer.controller || killer == victimPlayer.playerState ||
                                          killer == victimPlayer.pawn)) ||
                              (h.killerController && h.killerController == victimPlayer.controller)));
    af.killerIsVictimPlayer =
        victimIdentified && killerIsTrackedPlayer && victimPlayer.Ref() == killerPlayer.Ref();
    if (EventsParse::IsSelfAttribution(af)) {
        attribution = EventsParse::kSelfAttribution;
        killer = nullptr;
        killerIsTrackedPlayer = false;
        killerIsPlayer = false;
        killerCode.clear();
        g_selfDeaths++;
    }

    // ------------------------------------------------------------------------------------------
    // LANE L2d, THE WIELDED WEAPON. Pure work on primitives the game thread snapshotted: two FName
    // decodes and two pointer compares. No engine object is touched here, so a killer who died,
    // logged out or was garbage-collected between the hook and this drain cannot be dereferenced.
    //
    // Deliberately AFTER the self-attribution rule: a fall or a Coriolis storm points the instigator
    // at the victim, and "killed himself with his own knife" is exactly the kind of confident wrong
    // sentence this connector must not publish.
    std::string wieldedMelee = h.wieldedMeleeNameIdx ? Pool::NameNumbered(h.wieldedMeleeNameIdx, h.wieldedMeleeNameNum)
                                                     : std::string();
    std::string wieldedRanged = h.wieldedRangedNameIdx
                                    ? Pool::NameNumbered(h.wieldedRangedNameIdx, h.wieldedRangedNameNum)
                                    : std::string();
    // `None` is UE's spelling of an unset FName and names no weapon.
    if (wieldedMelee == "None") wieldedMelee.clear();
    if (wieldedRanged == "None") wieldedRanged.clear();
    EventsParse::WieldedWeaponFacts wf;
    const bool weaponSuppressed = attribution == EventsParse::kSelfAttribution;
    if (!weaponSuppressed) {
        wf.haveMeleeName = !wieldedMelee.empty();
        wf.haveRangedName = !wieldedRanged.empty();
        wf.meleeDamageTypeMatches = h.deathDamageTypeCls && h.wieldedMeleeDamageType == h.deathDamageTypeCls;
        wf.rangedDamageTypeMatches = h.deathDamageTypeCls && h.wieldedRangedDamageType == h.deathDamageTypeCls;
        wf.weaponInHand = h.weaponInHand;
        wf.weaponComponentActive = h.weaponComponentActive;
        wf.damageTypeIsMelee = EventsParse::IsMeleeDamageTypeName(damageTypeCode);
    }
    EventsParse::WieldedWeapon ww = EventsParse::DecideWieldedWeapon(wf);
    std::string weaponItemCode;
    if (ww.source != EventsParse::WeaponSource::None) {
        weaponItemCode = ww.useMelee ? wieldedMelee : wieldedRanged;
        g_weaponResolved++;
        if (ww.confidence >= 2) g_weaponByDamageTypeMatch++;
    } else if (weaponSuppressed) {
        ww.reason = "a self or environmental death has no wielder; the weapon is deliberately not reported";
    } else if (!h.weaponCharacterFound) {
        g_weaponNoCharacter++;
        ww.reason = "no ADuneCharacter-derived killer was located, so there is nothing wielding anything";
    } else {
        g_weaponNoEvidence++;
    }
    if (log) {
        log->weaponItemCode = weaponItemCode;
        log->weaponMeleeName = wieldedMelee;
        log->weaponRangedName = wieldedRanged;
        log->weaponSource = EventsParse::WeaponSourceName(ww.source);
        log->weaponConfidence = ww.confidence;
        log->weaponReason = ww.reason;
        log->weaponCharacterFound = h.weaponCharacterFound;
        log->weaponMeleeDamageTypeMatch = wf.meleeDamageTypeMatches;
        log->weaponRangedDamageTypeMatch = wf.rangedDamageTypeMatches;
    }

    std::string killerJson = "null";
    if (killerIsTrackedPlayer) {
        killerJson = "{\"ref\":" + JsonStr(killerPlayer.Ref()) + ",\"characterName\":" +
                     OrNull(killerPlayer.characterName.empty() ? killerPlayer.playerName : killerPlayer.characterName) +
                     ",\"accountId\":" +
                     (killerPlayer.accountId ? std::to_string(killerPlayer.accountId) : std::string("null")) + "}";
    }

    if (logKillerRef) *logKillerRef = killerIsTrackedPlayer ? killerPlayer.Ref() : std::string();
    if (logKillerIsPlayer) *logKillerIsPlayer = killerIsTrackedPlayer;

    std::string common;
    // `weapon` must be a DISPLAY name (memory: catalogue-human-names). In process we only ever have
    // the causer's class, which is a dev name, so `weapon` stays null and `weaponCode` carries the
    // class for the sidecar to join against its catalogue.
    // LANE L2c: `weaponCode` is now a REAL class name. It used to read `BlueprintGeneratedClass`,
    // because `DeathDefeatCausingDamageType` is a ClassProperty and the old code read the class OF the
    // pointer rather than the class AT it. `damageTypeCode` is reported separately and always, so a
    // consumer can tell "shot with X" from "died of Y" instead of having them share one field.
    //
    // LANE L2d: `weaponCode` now carries the **item template id of the weapon the killer was
    // wielding** whenever one was resolved — the same code space as Postgres `items.template_id` and
    // the sidecar catalogue, so it joins to a real display name ("ScrapMetalKnife" -> "Scrap Metal
    // Knife"). When no weapon could be resolved it keeps exactly its previous value (the damage-type /
    // causer class), so no consumer loses anything. `weaponItemCode` carries ONLY the item template id
    // and is null otherwise, which is the unambiguous field for a new consumer to read.
    common += ",\"weapon\":null,\"weaponCode\":" + OrNull(weaponItemCode.empty() ? weaponCode : weaponItemCode);
    common += ",\"weaponItemCode\":" + OrNull(weaponItemCode);
    common += ",\"weaponSource\":" + JsonStr(EventsParse::WeaponSourceName(ww.source));
    common += ",\"weaponConfidence\":" + std::to_string(ww.confidence);
    common += ",\"weaponReason\":" + JsonStr(ww.reason);
    common += ",\"damageTypeCode\":" + OrNull(damageTypeCode);
    common += ",\"weaponNote\":\"weaponItemCode is an ITEM TEMPLATE ID (items.template_id); resolve it to a "
              "display name through the sidecar catalogue. damageTypeCode is a damage-type CLASS and is never a "
              "weapon name (memory: catalogue-human-names)\"";
    common += ",\"attribution\":" + JsonStr(attribution);
    common += ",\"via\":" + JsonStr(KindName(h.kind));
    // ADDITIVE ONLY (the /events contract stays backward compatible; docs/API.md §deathKind).
    // `deathKind` is descriptive, never a gate: on this build `defeat` is the ordinary lethal death
    // — it is what the game's own UI prints as DEFEATED — and `death` is the rarer bIsDeath=true
    // variant. A consumer that ignores the field gets exactly L2's intended behaviour.
    common += ",\"deathKind\":" + JsonStr(deathKind);
    common += ",\"isDeathFlag\":" +
              std::string(h.isDeathOk ? (h.isDeathRaw ? "true" : "false") : "null");
    common += ",\"ts\":" + JsonStr(IsoNowUtc());
    if (h.havePos) common += ",\"position\":" + PosJson(h.x, h.y, h.z);

    if (vc.type == EventsParse::DeathEventType::PlayerDeath) {
        // `ref` is "unknown" — the same convention `player-disconnected` already uses — when the class
        // chain proved a player died but no identity could be attached. That is strictly better than
        // the old behaviour, which reported the death as an unnamed creature kill by the victim.
        std::string data = "{\"ref\":" + JsonStr(victimIdentified ? victimPlayer.Ref() : std::string("unknown"));
        data += ",\"characterName\":" +
                OrNull(victimPlayer.characterName.empty() ? victimPlayer.playerName : victimPlayer.characterName);
        data += ",\"accountId\":" +
                (victimPlayer.accountId ? std::to_string(victimPlayer.accountId) : std::string("null"));
        data += ",\"playerStateId\":" +
                (victimPlayer.playerStateId ? std::to_string(victimPlayer.playerStateId) : std::string("null"));
        data += ",\"killer\":" + killerJson;
        // When the killer is not a tracked player it is an NPC: its class is the only name we have,
        // and a class name is not a display name, so it goes in `killerEntityCode`.
        data += ",\"killerEntityCode\":" + OrNull(killer && !killerIsTrackedPlayer ? killerCode : std::string());
        data += ",\"killerEntity\":null";
        // ADDITIVE (L2c): how this event was classified, and whether the identity is trustworthy.
        data += ",\"identityComplete\":" + std::string(vc.identityComplete ? "true" : "false");
        data += ",\"identityHow\":" + OrNull(victimHow ? std::string(victimHow) : std::string());
        data += ",\"victimClass\":" + OrNull(victimCode);
        data += ",\"classifiedBy\":" +
                JsonStr(h.victimClassIsPlayer ? "victim class chain derives a player class" : "player registry");
        // Always present now (null when unreadable) so a consumer never has to tell "absent" from
        // "zero". The enum's NAMES are not reflected on this build — a UEnum's `Names` array has no
        // derivable offset here — so the raw ordinal is reported and nothing is invented; the sidecar
        // has the authoritative cause from Postgres `life_state`.
        data += ",\"lifeStateCode\":" + (h.haveReason ? std::to_string(h.lifeState) : std::string("null"));
        data += ",\"deathReasonCode\":" + (h.haveReason ? std::to_string(h.deathReason) : std::string("null"));
        data += ",\"deathReason\":null";
        data += common + "}";
        PluginState::Get().EmitEvent("player-death", data);
        g_emitted[1]++;
        return "player-death";
    }

    // A NON-PLAYER victim: an NPC or creature died.
    //
    // MEASURED CORRECTION (lane L2, live sweep): `DuneNpcCharacter::m_Name` is NOT a data-table row
    // key — it holds real display names ("Mobula Gang Member", "Slaver Trapper", "Ariste Atreides").
    // The AI spawner's LOG speaks in row keys (`T3_Band_Slv_Reg_Marksman`), which is what made the
    // offline reading look dev-ish. So the catalogue rule is satisfiable here after all — but only
    // through the same predicate the sidecar's catalogue uses, never by assuming.
    // LANE L2c: reached ONLY for a non-player victim now — the class-chain rule above cannot fall
    // through to here for a player, whatever the registry says.
    bool victimNamed = vf.haveDisplayName;
    if (vc.dropUnnamed) g_unnamedEntityKills++;
    std::string data = "{\"entity\":" + (victimNamed ? JsonStr(victimDev) : std::string("null"));
    // `code` is the STABLE key: the display name when we have one (a class is shared by dozens of
    // differently-named NPCs), else the class.
    data += ",\"entityCode\":" + OrNull(victimNamed ? victimDev : victimCode);
    data += ",\"entityClass\":" + OrNull(victimCode);
    data += ",\"entityDevName\":" + OrNull(victimDev);
    data += ",\"nameIsClassName\":" + std::string(victimNamed ? "false" : "true");
    // ADDITIVE (L2c). `dropUnnamed` is an instruction, not a hint: with no human display name there is
    // nothing publishable here, and a class name must never become a creature's name in Takaro
    // (memory: catalogue-human-names). `entity` is null and the sidecar drops the event.
    data += ",\"dropUnnamed\":" + std::string(vc.dropUnnamed ? "true" : "false");
    data += ",\"classifiedBy\":" + JsonStr(vc.reason);
    data += ",\"player\":" + killerJson;
    data += ",\"killerEntityCode\":" + OrNull(killer && !killerIsTrackedPlayer ? killerCode : std::string());
    data += common + "}";
    // entity-killed is only interesting to Takaro when a PLAYER did it. An NPC killing an NPC is
    // emitted too (the sidecar decides), but it is flagged by `player: null`.
    PluginState::Get().EmitEvent("entity-killed", data);
    g_emitted[0]++;
    (void)killerIsPlayer;
    (void)h.selfIsPlayer;
    return "entity-killed";
}

void DrainRing() {
    uint64_t now = NowMs();
    // Expire the correlation windows first, so a hint from a previous kill can never be merged.
    g_recentDeaths.erase(std::remove_if(g_recentDeaths.begin(), g_recentDeaths.end(),
                                        [&](const Recent& r) { return now - r.tsMs > kDedupeMs; }),
                         g_recentDeaths.end());
    g_killHints.erase(std::remove_if(g_killHints.begin(), g_killHints.end(),
                                     [&](const KillHint& k) { return now - k.tsMs > kDedupeMs; }),
                      g_killHints.end());

    uint64_t head = g_ringHead.load(std::memory_order_acquire);
    uint64_t tail = g_ringTail.load(std::memory_order_relaxed);
    for (; tail < head; tail++) {
        RawHit h = g_ring[tail % kRing];

        // ------------------------------------------------------------------------------------
        // LANE L2b: assemble the facts, then let the (pure, unit-tested) table decide. Nothing in
        // this loop drops a call on its own any more; every drop has a named reason and lands in
        // /debug/deathlog together with the raw parameter bytes it was taken on.
        EventsParse::DeathFacts f;
        f.isMulticastKillHint = (h.kind == K_MulticastKill);
        f.victimIsPlayer = h.victimIsPlayer;
        f.isDeath = EventsParse::ReadFrameBool(h.isDeathRaw, h.isDeathOk);
        f.dbno = EventsParse::ReadFrameBool(h.dbnoRaw, h.dbnoOk);
        if ((h.isDeathOk && f.isDeath == EventsParse::BoolRead::Unreadable) ||
            (h.dbnoOk && f.dbno == EventsParse::BoolRead::Unreadable))
            g_boolUnreadable++;

        for (const auto& r : g_recentDeaths)
            if (r.victim == h.victim && h.tsMs - r.tsMs <= kDedupeMs) f.duplicate = true;

        // Rule 4's read-back. The pump has no guaranteed cadence, so the lag is measured and the
        // value is only trusted inside a short window — a read taken 50 s later would be about a
        // respawned player, not about this call.
        const uint64_t kLifeStateTrustMs = 5000;
        uint64_t lagMs = now > h.tsMs ? now - h.tsMs : 0;
        if (h.victimIsPlayer && h.victimPlayerState && g_warm.lifeState >= 0 &&
            MemReadable(h.victimPlayerState, (size_t)g_warm.lifeState + 1) &&
            LooksLikeUObject(h.victimPlayerState)) {
            bool okls = false;
            uint8_t ls = U::ReadU8(h.victimPlayerState, (uint32_t)g_warm.lifeState, &okls);
            if (okls) {
                f.haveLifeState = true;
                f.lifeState = ls;
                f.lifeStateFresh = lagMs <= kLifeStateTrustMs;
                g_lifeStateReadbacks++;
            }
        }

        EventsParse::DeathDecision dec = EventsParse::DecideDeath(f);

        DeathLogEntry le;
        le.tsMs = h.tsMs;
        le.kind = h.kind;
        le.selfClass = DecodeName(h.selfClassIdx);
        le.victimClass = DecodeName(h.victimClassIdx);
        le.victimName = DecodeName(h.victimDevNameIdx);
        le.killerClass = DecodeName(h.killerClassIdx);
        le.victimIsPlayer = h.victimIsPlayer;
        le.isDeath = EventsParse::BoolReadName(f.isDeath);
        le.dbno = EventsParse::BoolReadName(f.dbno);
        le.haveLifeState = f.haveLifeState;
        le.lifeStateFresh = f.lifeStateFresh;
        le.lifeState = f.lifeState;
        le.drainLagMs = lagMs;
        le.rawHex = HexBytes(h.rawParams, h.rawLen);
        le.verdict = VerdictName(dec.verdict);
        le.reason = dec.reason;
        le.deathKind = dec.deathKind;

        if (dec.verdict == EventsParse::DeathVerdict::Hint) {
            g_killHints.push_back({h.self, h.victim, h.weapon ? h.weapon : h.causer, h.selfClassIdx,
                                   h.weaponClassIdx ? h.weaponClassIdx : h.causerClassIdx, h.selfIsPlayer, h.tsMs});
            DeathLogPush(std::move(le));
            continue;
        }
        if (dec.verdict == EventsParse::DeathVerdict::DropDuplicate) {
            g_duplicatesDropped++;
            DeathLogPush(std::move(le));
            continue;
        }
        if (dec.verdict == EventsParse::DeathVerdict::DropKnockDown) {
            g_knockDownsDropped++;
            DeathLogPush(std::move(le));
            continue;
        }
        g_recentDeaths.push_back({h.victim, h.tsMs});

        void* killer = h.killer;
        uint32_t killerIdx = h.killerClassIdx;
        // LANE L2c. Three sources, in order of how much they actually say about the kill:
        //   1. a weapon/damage-type parameter that is an OBJECT reference — the real thing;
        //   2. the DamageCauser actor — the projectile/weapon/hazard actor that did it;
        //   3. the damage-TYPE class (a ClassProperty), which is a category, not a weapon.
        // The old code took (1) unconditionally, and on this build (1) is always the ClassProperty — so
        // `weaponCode` was the metaclass name `BlueprintGeneratedClass` on every single death.
        void* weapon = nullptr;
        uint32_t weaponIdx = 0;
        if (h.weapon && !h.weaponIsClassPtr) {
            weapon = h.weapon;
            weaponIdx = h.weaponClassIdx;
        } else if (h.causer) {
            weapon = h.causer;
            weaponIdx = h.causerClassIdx;
        } else if (h.weapon) {
            weapon = h.weapon;
            weaponIdx = h.damageTypeNameIdx;  // the class's OWN name, not the class of the class
        }
        const char* attribution = killer ? "params" : (h.paramsResolved ? "none" : "paramsUnresolved");
        bool killerIsPlayer = h.killerIsPlayer;

        if (!killer) {
            // Correlate with the ReceiveMulticastKill hints in the window. Unique or nothing.
            const KillHint* only = nullptr;
            size_t matches = 0;
            for (const auto& k : g_killHints) {
                if (k.victim && k.victim != h.victim) continue;
                if (k.killer == h.victim) continue;  // a self-kill hint is not attribution
                matches++;
                only = &k;
            }
            if (matches == 1 && only) {
                killer = only->killer;
                killerIdx = only->killerClassIdx;
                killerIsPlayer = only->killerIsPlayer;
                if (!weapon && only->weapon) {
                    weapon = only->weapon;
                    weaponIdx = only->weaponClassIdx;
                }
                attribution = only->victim ? "multicastKill:victimMatch" : "multicastKill:uniqueInWindow";
            } else if (matches > 1) {
                g_suppressedAmbiguous++;
                attribution = "ambiguous";
            }
        }
        le.attribution = attribution;
        EventsParse::VictimClassification vc;
        le.emittedAs = EmitDeath(h, killer, killerIdx, killerIsPlayer, weapon, weaponIdx, attribution,
                                 dec.deathKind, &le.killerRef, &le.killerIsTrackedPlayer, &vc, &le);
        le.victimClassIsPlayer = h.victimClassIsPlayer;
        le.identityComplete = vc.identityComplete;
        le.dropUnnamed = vc.dropUnnamed;
        le.classification = vc.reason;
        le.victimIdentityHow = h.victimIdentityHow ? h.victimIdentityHow : "";
        DeathLogPush(std::move(le));
    }
    g_ringTail.store(tail, std::memory_order_release);
}

// ================================================================================================
// 6. LIVE ACTOR SWEEPS (off the game thread: Obj::ForEach only reads, every read guarded)
// ================================================================================================

struct NpcRow {
    void* actor;
    std::string cls;
    std::string devName;
    bool haveDist = false;
    double dist = 0;
    bool havePos = false;
    double x = 0, y = 0, z = 0;
    bool dead = false;
    bool killable = false;  // derives DuneCharacter, so KillCharacter exists on it
};

bool IsCdoName(const void* obj) {
    std::string n = U::NameOf(obj);
    return n.compare(0, 9, "Default__") == 0;
}

/// Sweeps GUObjectArray for live NPC / creature actors. `origin` is optional.
std::vector<NpcRow> SweepNpcs(const U::Vec* origin, size_t limit) {
    std::vector<NpcRow> out;
    void* bases[] = {g_warm.critterCls, g_warm.staticCritterCls, g_warm.npcCls, g_warm.npcCivilianCls};
    Obj::ForEach([&](void* o) {
        if (out.size() >= limit) return false;
        void* cls = U::ReadPtr(o, Reflect::Lay().objClass);
        if (!cls) return true;
        bool match = false;
        for (void* b : bases)
            if (b && U::ClassChainHas(cls, b)) { match = true; break; }
        if (!match) return true;
        if (IsCdoName(o)) return true;  // class default objects are not live NPCs
        NpcRow r;
        r.actor = o;
        r.cls = U::ClassNameOf(o);
        if (g_warm.npcDevName >= 0 && g_warm.npcCls && U::ClassChainHas(cls, g_warm.npcCls))
            r.devName = U::ReadFName(o, (uint32_t)g_warm.npcDevName);
        r.killable = g_warm.duneCharacterCls && U::ClassChainHas(cls, g_warm.duneCharacterCls);
        int32_t offDead = U::PropOffset(cls, "m_bIsDead");
        if (offDead >= 0) r.dead = U::ReadU8(o, (uint32_t)offDead) != 0;
        double x, y, z;
        if (QuickPos(o, x, y, z)) {
            r.havePos = true;
            r.x = x; r.y = y; r.z = z;
            if (origin) {
                double dx = x - origin->x, dy = y - origin->y, dz = z - origin->z;
                r.dist = std::sqrt(dx * dx + dy * dy + dz * dz);
                r.haveDist = true;
            }
        }
        out.push_back(r);
        return true;
    });
    if (origin)
        std::sort(out.begin(), out.end(), [](const NpcRow& a, const NpcRow& b) {
            if (a.haveDist != b.haveDist) return a.haveDist;
            return a.dist < b.dist;
        });
    return out;
}

}  // namespace

// ================================================================================================
// 7. INIT
// ================================================================================================

void Events::Init() {
    auto& st = PluginState::Get();
    g_nameOff = Reflect::Lay().objName;
    // Nothing here can work before the live class index and the FField layout exist (both land ~60 s
    // into a cold boot, after the object pool is allocated). Say so, rather than reporting an empty
    // filter as if it were fine.
    const char* why =
        "waiting for the live FNamePool + UClass index and the discovered FField layout; retried every 2 s";
    for (const char* cap : {"entity-killed", "player-death", "entities"}) st.SetCapability(cap, "degraded", why);
    g_filterHow = why;
    PluginLog("events: registered, %s", why);
}

bool Events::InitLive() {
    if (g_inited) return true;
    auto& st = PluginState::Get();
    g_nameOff = Reflect::Lay().objName;

    if (!Pool::F().ok || Classes::Count() == 0) {
        g_filterHow = "the FName pool / UClass index is not available yet";
        return false;
    }
    g_inited = true;

    // ---- warm the classes and offsets ----------------------------------------------------------
    g_warm.critterCls = Classes::ByName("DuneCritterBase");
    g_warm.staticCritterCls = Classes::ByName("DuneStaticCritterBase");
    g_warm.npcCls = Classes::ByName("DuneNpcCharacter");
    g_warm.npcCivilianCls = Classes::ByName("DuneNpcCharacterCivilian");
    g_warm.duneCharacterCls = Classes::ByName("DuneCharacter");
    g_warm.dunePlayerCharacterCls = Classes::ByName("DunePlayerCharacter");
    g_warm.dunePlayerControllerCls = Classes::ByName("DunePlayerControllerBase");
    g_warm.playerStateCls = Classes::ByName("DunePlayerState");
    g_warm.actorCls = Classes::ByName("Actor");
    if (g_warm.actorCls) {
        g_warm.rootComponent = U::PropOffset(g_warm.actorCls, "RootComponent");
        void* sc = Classes::ByName("SceneComponent");
        if (sc) g_warm.relativeLocation = U::PropOffset(sc, "RelativeLocation");
    }
    if (g_warm.npcCls) g_warm.npcDevName = U::PropOffset(g_warm.npcCls, "m_Name");
    if (g_warm.playerStateCls) {
        g_warm.lifeState = U::PropOffset(g_warm.playerStateCls, "m_LifeState");
        g_warm.lastDeathReason = U::PropOffset(g_warm.playerStateCls, "m_LastDeathReason");
    }
    // LANE L2d. The wielded weapon. Every offset by name; a missing name leaves -1 and the weapon
    // simply stays null. `FCachedMeleeWeaponData` is an INLINE struct member of ADuneCharacter, and a
    // StructProperty's inner UScriptStruct is not on the FField chain, so its two member offsets come
    // from the same unique-name ScriptStruct sweep the InstigatorInfo plan already uses.
    if (g_warm.duneCharacterCls) {
        g_warm.weaponComponentOff = U::PropOffset(g_warm.duneCharacterCls, "m_WeaponComponent");
        g_warm.meleeDataOff = U::PropOffset(g_warm.duneCharacterCls, "m_CachedMeleeWeaponData");
        g_warm.hasWeaponInHandOff = U::PropOffset(g_warm.duneCharacterCls, "m_bHasWeaponInHand");
        int32_t wants = U::PropOffset(g_warm.duneCharacterCls, "m_bServerWantsWeaponInHand");
        g_warm.weaponInHandIsPackedPair = g_warm.hasWeaponInHandOff >= 0 && wants == g_warm.hasWeaponInHandOff;
    }
    g_warm.weaponComponentCls = Classes::ByName("WeaponActorComponent");
    if (g_warm.weaponComponentCls) {
        g_warm.wacNameOff = U::PropOffset(g_warm.weaponComponentCls, "m_WeaponName");
        g_warm.wacDamageTypeOff = U::PropOffset(g_warm.weaponComponentCls, "m_CachedWeaponDamageType");
        g_warm.wacActiveOff = U::PropOffset(g_warm.weaponComponentCls, "m_bActive");
    }
    if (g_warm.meleeDataOff >= 0) {
        size_t matches = 0;
        std::string structName;
        void* st = FindScriptStruct("cachedmeleeweapondata", matches, &structName);
        if (st && matches == 1) {
            for (const auto& m : Pool::PropsOf(st, 64)) {
                if (m.offset < 0) continue;
                if (m.name == "m_CachedMeleeWeaponName") g_warm.meleeNameOff = m.offset;
                if (m.name == "m_DamageTypeClass") g_warm.meleeDamageTypeOff = m.offset;
            }
            g_warm.meleeStructNote = "FCachedMeleeWeaponData resolved by unique-name ScriptStruct sweep";
        } else {
            // The uniqueness rule, same as InstigatorInfo's: more than one candidate means we cannot
            // say which struct this is, so the melee half stays unresolved rather than guessed.
            g_warm.meleeStructNote = matches ? "several ScriptStructs matched 'CachedMeleeWeaponData' - refusing to pick one"
                                             : "no ScriptStruct matched 'CachedMeleeWeaponData'";
        }
    }
    g_warmReady = true;

    // ---- build the filter table and the parameter plans -----------------------------------------
    size_t rows = 0, plans = 0;
    std::string how;
    for (size_t w = 0; w < kWantedCount; w++) {
        const WantedFn& want = kWanted[w];
        uint32_t idx = 0;
        // Every listed class is visited: a subclass override is a DIFFERENT UFunction object with the
        // same name and possibly different parameters, so each one needs its own plan. The FName
        // comparison index, in contrast, is global to the name — one filter row covers them all.
        for (size_t c = 0; want.classes[c]; c++) {
            void* cls = Classes::ByName(want.classes[c]);
            if (!cls) continue;
            for (const auto& fn : Pool::FunctionsOf(cls)) {
                if (fn.first != want.name) continue;
                // The index comes off the UFunction object itself, so the filter never needs an FName
                // *constructor* (which this build does not export).
                uint32_t i = U::ReadU32(fn.second, g_nameOff);
                if (i && !idx) idx = i;
                if (!PlanFor(fn.second) && plans < kMaxPlans) {
                    BuildPlan(g_plans[plans], fn.second, want.kind);
                    plans++;
                    g_planCount.store(plans, std::memory_order_release);
                }
                break;
            }
        }
        if (!idx) {
            how += std::string(how.empty() ? "" : "; ") + want.name + ": no live UFunction";
            continue;
        }
        if (rows < sizeof g_filter / sizeof g_filter[0]) {
            g_filter[rows].idx = idx;
            g_filter[rows].kind = want.kind;
            rows++;
            if (idx < g_filterMin) g_filterMin = idx;
            if (idx > g_filterMax) g_filterMax = idx;
            char b[96];
            snprintf(b, sizeof b, "%s=#%u", want.name, idx);
            how += std::string(how.empty() ? "" : ", ") + b;
        }
    }
    g_planCount.store(plans, std::memory_order_release);
    // Published LAST: until this store the detour classifies nothing, so a half-built table can
    // never be read by the game thread.
    g_filterCount.store(rows, std::memory_order_release);
    char b[256];
    snprintf(b, sizeof b, "%zu names in the filter [%u..%u], %zu parameter plans: ", rows, g_filterMin, g_filterMax,
             plans);
    g_filterHow = std::string(b) + how;
    PluginLog("events: %s", g_filterHow.c_str());
    Note("filter", g_filterHow);
    Note("multicastKillDirection",
         "ReceiveMulticastKill is recorded as a killer HINT and never emitted as an event: `self` is most likely "
         "the killer, and 'most likely' is how a kill feed ends up the wrong way round (F20). Its parameter layout "
         "is published under `functions` so a real kill can settle it.");

    // ---- capabilities --------------------------------------------------------------------------
    // "ok" here means resolved + filtered, never proven. `fired`/`emitted` in
    // /health.diagnostics.eventSources are the only evidence, and they start at 0.
    bool haveCritter = g_warm.critterCls != nullptr;
    bool haveChar = g_warm.duneCharacterCls != nullptr;
    if (rows == 0) {
        st.SetCapability("entity-killed", "degraded", "no death/kill UFunction name resolved: " + g_filterHow);
        st.SetCapability("player-death", "degraded", "no death/kill UFunction name resolved: " + g_filterHow);
    } else {
        st.SetCapability("entity-killed", "ok",
                         std::string("ProcessEvent name filter on ") + g_filterHow +
                             (haveCritter ? "" : " (no DuneCritterBase UClass live)") +
                             " - NOT proven until `emitted` is non-zero");
        st.SetCapability("player-death", "ok",
                         std::string("attribution from the same filter; the sidecar's Postgres life_state edge "
                                     "remains the baseline") +
                             (haveChar ? "" : " (no DuneCharacter UClass live)") +
                             " - NOT proven until `emitted` is non-zero");
    }
    st.SetCapability("entities", g_warm.critterCls || g_warm.npcCls ? "ok" : "degraded",
                     "GUObjectArray sweep by class chain; display names do NOT exist in this process, so every "
                     "row is nameIsClassName:true and the sidecar must name or drop it");
    st.SetCapability("log", "unimplemented",
                     "the sidecar reads the container log directly; a second redacted tail here would only "
                     "duplicate it");
    Players::Init();
    Query::Init();  // re-run now that the classes it reports on are indexable
    return true;
}

// ================================================================================================
// 8. THE HOT PATH
// ================================================================================================

bool Events::OnProcessEvent(void* self, void* func, void* params) {
    const uint64_t tFilter = Perf::NowNs();
    size_t n = g_filterCount.load(std::memory_order_acquire);
    if (!n || !func) {
        Perf::RecordFilter(Perf::NowNs() - tFilter, false, false);
        return false;
    }
    // One 4-byte read. `func` was just dereferenced by the engine's own ProcessEvent (it reads
    // PropertiesSize out of it to size the parameter frame), so it is a live UFunction and needs no
    // MemReadable probe on this path — which is what keeps the filter in the tens of nanoseconds.
    uint32_t idx;
    memcpy(&idx, (const char*)func + g_nameOff, sizeof idx);
    // Two integer compares reject the overwhelming majority of the ~6 000 calls/s.
    if (idx < g_filterMin || idx > g_filterMax) {
        Perf::RecordFilter(Perf::NowNs() - tFilter, false, false);
        return false;
    }
    uint8_t kind = K_None;
    for (size_t i = 0; i < n; i++)
        if (g_filter[i].idx == idx) {
            kind = g_filter[i].kind;
            break;
        }
    Perf::RecordFilter(Perf::NowNs() - tFilter, kind != K_None, false);
    if (kind == K_None) return false;

    g_hits[kind]++;
    // Past this point we are a genuine hit: a handful of guarded reads, then enqueue. Everything
    // else — decoding, dedupe, correlation, JSON — is the housekeeping thread's. Timed separately as
    // an event handler so the filter metric stays the filter's.
    uint64_t t0 = Perf::NowNs();
    RawHit h;
    memset(&h, 0, sizeof h);
    h.kind = kind;
    h.tsMs = NowMs();
    h.self = self;
    h.selfClassIdx = ClassIdxOf(self);

    const ParamPlan* plan = PlanFor(func);
    if (plan) {
        h.paramsResolved = true;
        if (params) {
            auto read = [&](const Role& r, const void* base) -> void* {
                if (r.off < 0) return nullptr;
                return r.weak ? U::ResolveWeak(base, (uint32_t)r.off) : U::ReadPtr(base, (uint32_t)r.off);
            };
            h.victim = read(plan->victim, params);
            h.killer = read(plan->killer, params);
            h.causer = read(plan->causer, params);
            h.weapon = read(plan->weapon, params);
            if (h.weapon) h.weaponIsClassPtr = plan->weapon.holdsClass;
            // The two bool parameters, as RAW BYTES. What they mean is decided off-thread by
            // EventsParse::DecideDeath; nothing on this path interprets them, and nothing on this
            // path drops a call. (L2 gated here on `bIsDeath != 0` and lost every real death.)
            if (plan->isDeathOff >= 0) {
                bool okd = false;
                h.isDeathRaw = U::ReadU8(params, (uint32_t)plan->isDeathOff, &okd);
                h.isDeathOk = okd;
            }
            if (plan->dbnoOff >= 0) {
                bool okn = false;
                h.dbnoRaw = U::ReadU8(params, (uint32_t)plan->dbnoOff, &okn);
                h.dbnoOk = okn;
            }
            // A bounded copy of the frame, for /debug/deathlog. Sized from the UFunction's own
            // PropertiesSize, clamped, so a build with a larger frame truncates instead of over-reading.
            uint32_t want = plan->frameSize;
            if (want > sizeof h.rawParams) want = sizeof h.rawParams;
            if (want && MemReadable(params, want)) {
                memcpy(h.rawParams, params, want);
                h.rawLen = (uint16_t)want;
            }
            // The killer lives inside `InstigatorInfo`, not in a bare parameter, on every death
            // UFunction this build has. The offsets come from that UScriptStruct's own FField chain.
            if (plan->instigatorOff >= 0) {
                const void* inst = (const char*)params + plan->instigatorOff;
                bool serialOk = false;
                if (!h.killer) h.killer = read(plan->instKiller, inst);
                if (!h.causer) h.causer = read(plan->instCauser, inst);
                if (!h.weapon) {
                    h.weapon = read(plan->instWeapon, inst);
                    if (h.weapon) h.weaponIsClassPtr = plan->instWeapon.holdsClass;
                }
                // The killer's CONTROLLER is the strongest identity link we have: the player registry
                // is keyed on exactly that pointer.
                if (plan->instControllerRole.off >= 0) {
                    void* ctrl = plan->instControllerRole.weak
                                     ? U::ResolveWeak(inst, (uint32_t)plan->instControllerRole.off, &serialOk)
                                     : U::ReadPtr(inst, (uint32_t)plan->instControllerRole.off);
                    if (ctrl) {
                        h.killerController = ctrl;
                        if (serialOk) g_weakSerialAgreed++;
                        else g_weakSerialDisagreed++;
                    }
                }
                // Every pointer that came out of a struct is re-validated before its class is read: on
                // a build with no function symbols a wrong pointer is a crash, not a wrong answer.
                if (h.killer && !LooksLikeUObject(h.killer)) h.killer = nullptr;
                if (h.causer && !LooksLikeUObject(h.causer)) h.causer = nullptr;
                if (h.weapon && !LooksLikeUObject(h.weapon)) {
                    h.weapon = nullptr;
                    h.weaponIsClassPtr = false;
                }
            }
        }
    } else {
        // First time we see this UFunction object (a Blueprint override). Record it; Housekeep()
        // builds the plan off-thread and the next one is fully attributed. Never guess offsets.
        size_t u = g_unplannedCount.load(std::memory_order_relaxed);
        if (u < sizeof g_unplanned / sizeof g_unplanned[0]) {
            g_unplanned[u] = {func, kind};
            g_unplannedCount.store(u + 1, std::memory_order_release);
        } else {
            g_unplannedDropped++;
        }
    }

    // Direction. Only a direction-unambiguous function sets the victim from `self`.
    if (SelfIsVictim(kind)) {
        if (!h.victim) h.victim = self;
    }
    if (h.victim == self) h.victimClassIdx = h.selfClassIdx;
    else h.victimClassIdx = ClassIdxOf(h.victim);
    h.killerClassIdx = ClassIdxOf(h.killer);
    h.causerClassIdx = ClassIdxOf(h.causer);
    h.weaponClassIdx = ClassIdxOf(h.weapon);
    if (h.victim) {
        void* vcls = U::ReadPtr(h.victim, Reflect::Lay().objClass);
        if (g_warm.npcDevName >= 0 && g_warm.npcCls && U::ClassChainHas(vcls, g_warm.npcCls))
            h.victimDevNameIdx = U::ReadU32(h.victim, (uint32_t)g_warm.npcDevName);
        // LANE L2c. THE fix for the misclassification: a player victim is decided HERE, from the class
        // chain, and nothing downstream may turn it into an `entity-killed`. Three classes because the
        // death functions could in principle be called on any of them; `DunePlayerCharacter` is the one
        // this build actually uses (`BP_DunePlayerCharacter_C` derives it).
        void* playerBases[] = {g_warm.dunePlayerCharacterCls, g_warm.dunePlayerControllerCls,
                               g_warm.playerStateCls};
        if (vcls) {
            for (void* b : playerBases)
                if (b && U::ClassChainHas(vcls, b)) {
                    h.victimClassIsPlayer = true;
                    break;
                }
        }
    }
    // LANE L2c. The weapon/damage-type name, read according to the property's own type. A ClassProperty
    // holds the class itself, so its OWN name is the answer.
    if (h.weapon && h.weaponIsClassPtr) {
        h.damageTypeNameIdx = U::ReadU32(h.weapon, g_nameOff);
        // LANE L2d. The damage-type CLASS pointer itself, kept for the pointer compare against the
        // killer's two cached damage-type classes. Never dereferenced off the game thread.
        h.deathDamageTypeCls = h.weapon;
    }
    if (QuickPos(h.victim, h.x, h.y, h.z)) h.havePos = true;

    // The death reason lives on the victim's PlayerState, so it is only read for a player.
    //
    // LANE L2c: `IdentifyActor`, not `Identify`. A respawn gives the player a new pawn, so pointer
    // equality against the registry's cached pawn misses the CURRENT one; the owner walk
    // (pawn -> Controller / PlayerState) is what still resolves it. Two guarded property reads.
    Players::Entry pe;
    if (Players::IdentifyActor(h.victim, pe, &h.victimIdentityHow)) {
        h.victimIsPlayer = true;
        // Kept so the drain can re-read `m_LifeState` a moment later (decision-table rule 4). Only a
        // pointer is stored; it is re-validated before that read.
        h.victimPlayerState = pe.playerState;
        if (pe.playerState && g_warm.lifeState >= 0) {
            bool ok1 = false;
            h.lifeState = U::ReadU8(pe.playerState, (uint32_t)g_warm.lifeState, &ok1);
            if (g_warm.lastDeathReason >= 0) h.deathReason = U::ReadU8(pe.playerState, (uint32_t)g_warm.lastDeathReason);
            h.haveReason = ok1;
        }
    }
    void* killerPawn = nullptr;
    if (h.killer && Players::IdentifyActor(h.killer, pe)) {
        h.killerIsPlayer = true;
        killerPawn = pe.pawn;
    }
    if (Players::IdentifyActor(self, pe)) h.selfIsPlayer = true;

    // ------------------------------------------------------------------------------------------
    // LANE L2d. THE WIELDED WEAPON, snapshotted as primitives.
    //
    // The killer arrives as whatever `InstigatorInfo` held — a character, a controller or a player
    // state — so the character is located first: the actor itself when its class chain derives
    // ADuneCharacter, else the registry's live pawn for a player killer. An NPC killer is already the
    // character, which is what makes the NPC-kills-player direction work with the same reads.
    //
    // Cost: at most nine guarded reads, only on a call that already passed the name filter (a handful
    // per minute), and NOT in the filter itself (memory: avoid-game-thread). Everything after this is
    // pointer compares and FName decoding on the housekeeping thread.
    if (g_warm.duneCharacterCls) {
        void* killerChar = nullptr;
        if (h.killer) {
            void* kcls = U::ReadPtr(h.killer, Reflect::Lay().objClass);
            if (kcls && U::ClassChainHas(kcls, g_warm.duneCharacterCls)) killerChar = h.killer;
        }
        if (!killerChar && killerPawn && LooksLikeUObject(killerPawn)) {
            void* pcls = U::ReadPtr(killerPawn, Reflect::Lay().objClass);
            if (pcls && U::ClassChainHas(pcls, g_warm.duneCharacterCls)) killerChar = killerPawn;
        }
        if (killerChar) {
            h.weaponCharacterFound = true;
            if (g_warm.meleeDataOff >= 0) {
                if (g_warm.meleeNameOff >= 0) {
                    uint32_t off = (uint32_t)g_warm.meleeDataOff + (uint32_t)g_warm.meleeNameOff;
                    h.wieldedMeleeNameIdx = U::ReadU32(killerChar, off);
                    h.wieldedMeleeNameNum = U::ReadU32(killerChar, off + 4);
                }
                if (g_warm.meleeDamageTypeOff >= 0)
                    h.wieldedMeleeDamageType =
                        U::ReadPtr(killerChar, (uint32_t)g_warm.meleeDataOff + (uint32_t)g_warm.meleeDamageTypeOff);
            }
            if (g_warm.hasWeaponInHandOff >= 0) {
                bool okw = false;
                uint8_t b = U::ReadU8(killerChar, (uint32_t)g_warm.hasWeaponInHandOff, &okw);
                // A packed pair means the byte holds BOTH `m_bHasWeaponInHand` and
                // `m_bServerWantsWeaponInHand`, so "non-zero" is the strongest honest reading; an
                // unpacked build gets the exact bool. Either way this flag is only ever CORROBORATING
                // evidence in rule 4 and can never refuse a weapon a damage-type match already proved.
                h.weaponInHand = okw && (g_warm.weaponInHandIsPackedPair ? b != 0 : b == 1);
            }
            if (g_warm.weaponComponentOff >= 0) {
                void* wac = U::ReadPtr(killerChar, (uint32_t)g_warm.weaponComponentOff);
                // The component is a UObject like any other, so it goes through the same validator
                // every engine pointer here does before it is read.
                if (wac && LooksLikeUObject(wac)) {
                    if (g_warm.wacNameOff >= 0) {
                        h.wieldedRangedNameIdx = U::ReadU32(wac, (uint32_t)g_warm.wacNameOff);
                        h.wieldedRangedNameNum = U::ReadU32(wac, (uint32_t)g_warm.wacNameOff + 4);
                    }
                    if (g_warm.wacDamageTypeOff >= 0)
                        h.wieldedRangedDamageType = U::ReadPtr(wac, (uint32_t)g_warm.wacDamageTypeOff);
                    if (g_warm.wacActiveOff >= 0) {
                        bool oka = false;
                        h.weaponComponentActive = U::ReadU8(wac, (uint32_t)g_warm.wacActiveOff, &oka) == 1 && oka;
                    }
                }
            }
        }
    }

    uint64_t head = g_ringHead.load(std::memory_order_relaxed);
    if (head - g_ringTail.load(std::memory_order_acquire) >= kRing) {
        g_ringDropped++;
    } else {
        g_ring[head % kRing] = h;
        g_ringHead.store(head + 1, std::memory_order_release);
    }
    Perf::RecordHandler(Perf::NowNs() - t0);
    return true;
}

namespace {

// ------------------------------------------------------------------------------------------------
// LANE L2c: the delayed connect announce.
//
// Live evidence: `/events` seq 1, 7 and 9 all announced `characterName: "Tester"` — the account persona
// from `APlayerState::PlayerNamePrivate` — with every actor id null, while `/players` a moment later
// reported the real character `TakaroTest`. At `PostLogin` the controller's persistence component has
// not been populated, so `m_CharacterName` is empty and the payload fell through to the session name.
//
// The chosen fix is the simpler of the two the campaign offered: HOLD the announce and retry, rather
// than emit a correction event the sidecar would have to reconcile. `Housekeep()` already runs every
// ~2 s on the housekeeping thread, and the grace is bounded at 10 s — after that the connect is
// emitted with whatever is readable and `identityComplete: false`. A connect is never lost.
struct PendingConnect {
    void* controller = nullptr;
    std::string source;
    uint64_t firstSeenMs = 0;
};
Mutex g_pendingLock;
std::vector<PendingConnect> g_pendingConnects;

/// Builds and emits the `player-connected` payload for a controller the registry already holds.
///
/// Returns false when nothing was emitted. `*held` then says WHY: true means "the identity is not
/// complete yet and the grace has not expired, try again", false means the registry does not hold this
/// controller at all.
bool EmitConnected(void* controller, const char* source, uint64_t firstSeenMs, bool* held = nullptr) {
    if (held) *held = false;
    Players::Entry e;
    if (!Players::Identify(controller, e)) return false;

    EventsParse::ConnectIdentityFacts cf;
    cf.haveAccountId = e.accountId != 0;
    // The PERSISTENCE component's name, deliberately — not `playerName`, which is the account persona
    // and is exactly what made the old payload misleading.
    cf.havePersistenceCharacterName = !e.characterName.empty();
    cf.ageMs = firstSeenMs && NowMs() > firstSeenMs ? NowMs() - firstSeenMs : 0;
    EventsParse::ConnectAnnounce act = EventsParse::DecideConnectAnnounce(cf);
    if (act == EventsParse::ConnectAnnounce::Retry) {
        if (held) *held = true;
        return false;
    }
    const bool complete = act == EventsParse::ConnectAnnounce::Announce;
    if (!complete) g_connectsIncomplete++;

    std::string data = "{\"ref\":" + JsonStr(e.Ref());
    data += ",\"characterName\":" + OrNull(e.characterName.empty() ? e.playerName : e.characterName);
    // ADDITIVE (L2c). The two names are now distinguishable instead of silently interchangeable:
    // `playerName` is the ACCOUNT persona and `characterNameSource` says which one `characterName` is.
    data += ",\"playerName\":" + OrNull(e.playerName);
    data += ",\"characterNameSource\":" +
            JsonStr(!e.characterName.empty() ? "persistence:m_CharacterName"
                                             : (e.playerName.empty() ? "none" : "playerState:PlayerNamePrivate"));
    data += ",\"accountId\":" + (e.accountId ? std::to_string(e.accountId) : std::string("null"));
    data += ",\"playerStateId\":" + (e.playerStateId ? std::to_string(e.playerStateId) : std::string("null"));
    data += ",\"playerControllerId\":" + (e.controllerId ? std::to_string(e.controllerId) : std::string("null"));
    data += ",\"playerPawnId\":" + (e.pawnId ? std::to_string(e.pawnId) : std::string("null"));
    data += ",\"identityHow\":" + JsonStr(e.identityHow);
    data += ",\"identityComplete\":" + std::string(complete ? "true" : "false");
    data += ",\"announceDelayMs\":" + std::to_string(cf.ageMs);
    // Said out loud in the payload: this is a precise EDGE, not an identity. The sidecar's Postgres
    // presence poller owns `gameId` (the FLS id), which does not exist in this process.
    data += ",\"hint\":true,\"identityAuthority\":\"sidecar/postgres\"";
    data += ",\"source\":" + JsonStr(source);
    data += ",\"ts\":" + JsonStr(IsoNowUtc()) + "}";
    PluginState::Get().EmitEvent("player-connected", data);
    g_emitted[2]++;
    PluginState::Get().SetCapability("player-connected", "ok",
                                     std::string("a connect was observed and emitted (source: ") + source +
                                         (complete ? ", identity complete)" : ", identity incomplete)"));
    return true;
}

/// Records a connect whose identity was not readable yet. Idempotent per controller; keeps the first
/// sighting's timestamp, which is what bounds the grace.
void PendConnect(void* controller, const char* source) {
    Guard g(g_pendingLock);
    for (auto& p : g_pendingConnects)
        if (p.controller == controller) return;
    if (g_pendingConnects.size() >= 128) return;  // the same cap the registry has
    g_pendingConnects.push_back({controller, source, NowMs()});
    g_connectsHeld++;
}

/// Removes a controller from the pending list. Returns true when it WAS pending — i.e. when no
/// `player-connected` was ever emitted for it, so no `player-disconnected` should be either.
bool DropPendingConnect(void* controller) {
    Guard g(g_pendingLock);
    for (size_t i = 0; i < g_pendingConnects.size(); i++)
        if (g_pendingConnects[i].controller == controller) {
            g_pendingConnects.erase(g_pendingConnects.begin() + (long)i);
            return true;
        }
    return false;
}

/// HOUSEKEEPING THREAD, every ~2 s. Retries every held connect.
void RetryPendingConnects() {
    std::vector<PendingConnect> pend;
    {
        Guard g(g_pendingLock);
        pend = g_pendingConnects;
    }
    if (pend.empty()) return;
    g_connectRetries++;
    // The identity has to be RE-READ on the game thread, or every retry would look at the same
    // half-populated registry entry it was held for. Bounded: one queued job per 2 s, and only while
    // a connect is actually pending.
    Players::EnsureFresh(0);
    for (const PendingConnect& p : pend) {
        bool held = false;
        bool announced = EmitConnected(p.controller, p.source.c_str(), p.firstSeenMs, &held);
        if (held) continue;  // still inside the grace window
        if (announced) {
            uint64_t age = NowMs() > p.firstSeenMs ? NowMs() - p.firstSeenMs : 0;
            if (age < EventsParse::kConnectGraceMs) g_connectsRecovered++;
        } else {
            // The registry no longer holds this controller: the player left before the identity ever
            // became readable. Nothing was announced, so nothing has to be retracted.
            g_connectsAbandoned++;
        }
        DropPendingConnect(p.controller);
    }
}

std::string PendingConnectsJson() {
    Guard g(g_pendingLock);
    std::string o = "{\"mechanism\":\"a connect whose persistence identity is not readable yet is HELD and "
                    "retried on the ~2 s housekeeping pass; after TAKARO grace (10 s) it is emitted with "
                    "identityComplete:false. At PostLogin m_CharacterName is empty and the old payload "
                    "announced the ACCOUNT persona instead (live: characterName 'Tester' vs 'TakaroTest')\"";
    o += ",\"graceMs\":" + std::to_string(EventsParse::kConnectGraceMs);
    o += ",\"pending\":" + std::to_string(g_pendingConnects.size());
    o += ",\"held\":" + std::to_string(g_connectsHeld.load());
    o += ",\"retryPasses\":" + std::to_string(g_connectRetries.load());
    o += ",\"recoveredWithinGrace\":" + std::to_string(g_connectsRecovered.load());
    o += ",\"announcedIncomplete\":" + std::to_string(g_connectsIncomplete.load());
    o += ",\"abandoned\":" + std::to_string(g_connectsAbandoned.load());
    return o + "}";
}

void EmitDisconnected(void* controller, const char* source, const char* reason) {
    Players::Entry e;
    bool known = Players::Identify(controller, e);
    Players::NoteLogout(controller);
    std::string data = "{\"ref\":" + JsonStr(known ? e.Ref() : std::string("unknown"));
    data += ",\"characterName\":" + OrNull(known ? (e.characterName.empty() ? e.playerName : e.characterName) : std::string());
    data += ",\"accountId\":" + (known && e.accountId ? std::to_string(e.accountId) : std::string("null"));
    data += ",\"reason\":" + JsonStr(reason);
    data += ",\"hint\":true,\"identityAuthority\":\"sidecar/postgres\"";
    data += ",\"source\":" + JsonStr(source);
    data += ",\"ts\":" + JsonStr(IsoNowUtc()) + "}";
    PluginState::Get().EmitEvent("player-disconnected", data);
    g_emitted[3]++;
    PluginState::Get().SetCapability("player-disconnected", "ok",
                                     std::string("a disconnect was observed and emitted (source: ") + source + ")");
}

}  // namespace

void Events::OnPostLogin(void* controller) {
    // Idempotence against the reconciler: if the sweep already announced this controller, PostLogin is
    // only a refresh. Checked BEFORE NoteLogin, which is what would make the registry say "online".
    bool already = Players::IsTrackedOnline(controller);
    Players::NoteLogin(controller);
    if (already) return;
    // LANE L2c: PostLogin is the EARLIEST possible moment, which is precisely why the identity is not
    // there yet. Hold it and let Housekeep retry rather than announce the account persona.
    bool held = false;
    if (!EmitConnected(controller, "hook:PostLogin", NowMs(), &held)) PendConnect(controller, "hook:PostLogin");
}

void Events::OnLogout(void* controller) {
    // LANE L2c: a connect that was held and never announced needs no disconnect. Retracting an event
    // nobody ever saw would be a second lie on top of the first.
    if (DropPendingConnect(controller)) {
        Players::NoteLogout(controller);
        return;
    }
    if (!Players::IsTrackedOnline(controller)) {
        // Already retired (the reconciler confirmed the absence first). Keep the registry consistent
        // and stay silent rather than emitting a second `player-disconnected`.
        Players::NoteLogout(controller);
        return;
    }
    EmitDisconnected(controller, "hook:Logout", "logout");
}

bool Events::OnPresenceConnect(void* controller, const char* source) {
    if (!controller) return false;
    if (!Players::IsTrackedOnline(controller)) {
        // The identity read walks live UObjects, so it belongs on the game thread. One queued job per
        // join, never per sweep.
        void* c = controller;
        if (!GameThread::Run([c] { Players::NoteLogin(c); }, 5000)) return false;
    }
    bool held = false;
    if (EmitConnected(controller, source, NowMs(), &held)) return true;
    // LANE L2c: a held connect is OWNED by the pending list from here, so the sweep must treat it as
    // announced — otherwise both mechanisms would retry the same controller and the reconciler would
    // also keep it out of its tracked set.
    if (held) {
        PendConnect(controller, source);
        return true;
    }
    return false;
}

void Events::OnPresenceDisconnect(void* controller, const char* source) {
    if (!controller) return;
    // `connection-lost` rather than `logout`: the reconciler saw the connection go away, which is not
    // the same statement as "the game called Logout".
    EmitDisconnected(controller, source, "connection-lost");
}

// ================================================================================================
// 9. HOUSEKEEPING
// ================================================================================================

void Events::Housekeep() {
    // LANE L2c: held connects are retried FIRST and unconditionally — a pending announce must not be
    // blocked by the live-init retry below, which can bail out for unrelated reasons.
    RetryPendingConnects();
    // The live class index appears ~60 s into a cold boot, so the real initialisation is retried here
    // rather than being a one-shot that silently lost the race.
    if (!g_inited && !InitLive()) return;
    // 1. Learn the parameter plans for UFunctions the filter met for the first time. This allocates,
    //    which is exactly why it is here and not in the detour.
    size_t u = g_unplannedCount.load(std::memory_order_acquire);
    if (u) {
        size_t plans = g_planCount.load(std::memory_order_acquire);
        for (size_t i = 0; i < u && plans < kMaxPlans; i++) {
            if (PlanFor(g_unplanned[i].func)) continue;
            BuildPlan(g_plans[plans], g_unplanned[i].func, g_unplanned[i].kind);
            plans++;
            g_planCount.store(plans, std::memory_order_release);
        }
        g_unplannedCount.store(0, std::memory_order_release);
    }
    // 2. Turn raw hits into events.
    DrainRing();
    // 3. Retire logged-out players whose linger expired.
    Players::Expire();
}

// ================================================================================================
// 10. DEBUG / PROOF TOOLING
// ================================================================================================

HandlerResult Events::ScriptStructs(const std::string& match, size_t limit) {
    if (!Pool::F().ok || Classes::Count() == 0)
        return {503, "{\"error\":\"the FName pool / UClass index is not available yet\"}"};
    return {200, ScriptStructsJson(match, limit ? limit : 50)};
}

HandlerResult Events::DeathLog(size_t limit) {
    if (!limit || limit > kDeathLog) limit = kDeathLog;
    std::vector<DeathLogEntry> rows;
    {
        Guard g(g_deathLogLock);
        uint64_t seq = g_deathLogSeq.load();
        uint64_t first = seq > limit ? seq - limit : 0;
        for (uint64_t s = seq; s > first; s--) {  // newest first
            const DeathLogEntry& e = g_deathLog[(s - 1) % kDeathLog];
            if (e.seq == s) rows.push_back(e);
        }
    }
    std::string o =
        "{\"note\":\"the last " + std::to_string(kDeathLog) +
        " calls the ProcessEvent death filter caught, newest first, with the RAW parameter frame and "
        "the exact decision taken on it. `rawParams` is hex; cross-check a bool against the offsets in "
        "/health.diagnostics.eventSources.functions[].params. This endpoint exists because L2 dropped "
        "every real death and kept no record of what it dropped.\"";
    o += ",\"gate\":\"bShouldEnterDbno is the knock-down gate; bIsDeath is descriptive only\"";
    o += ",\"total\":" + std::to_string(g_deathLogSeq.load()) + ",\"entries\":[";
    bool first = true;
    for (const auto& e : rows) {
        if (!first) o += ",";
        first = false;
        o += "{\"seq\":" + std::to_string(e.seq);
        o += ",\"tsMs\":" + std::to_string(e.tsMs);
        o += ",\"function\":" + JsonStr(KindName(e.kind));
        o += ",\"selfClass\":" + OrNull(e.selfClass);
        o += ",\"victimClass\":" + OrNull(e.victimClass);
        o += ",\"victimName\":" + OrNull(e.victimName);
        o += ",\"victimIsPlayer\":" + std::string(e.victimIsPlayer ? "true" : "false");
        o += ",\"victimClassIsPlayer\":" + std::string(e.victimClassIsPlayer ? "true" : "false");
        o += ",\"identityComplete\":" + std::string(e.identityComplete ? "true" : "false");
        o += ",\"dropUnnamed\":" + std::string(e.dropUnnamed ? "true" : "false");
        o += ",\"victimIdentityHow\":" + OrNull(e.victimIdentityHow);
        o += ",\"classification\":" + OrNull(std::string(e.classification));
        // LANE L2d.
        o += ",\"weapon\":{\"itemCode\":" + OrNull(e.weaponItemCode) + ",\"source\":" + JsonStr(e.weaponSource) +
             ",\"confidence\":" + std::to_string(e.weaponConfidence) + ",\"reason\":" + JsonStr(e.weaponReason) +
             ",\"killerCharacterFound\":" + std::string(e.weaponCharacterFound ? "true" : "false") +
             ",\"meleeCacheName\":" + OrNull(e.weaponMeleeName) + ",\"weaponComponentName\":" +
             OrNull(e.weaponRangedName) + ",\"meleeDamageTypeMatch\":" +
             std::string(e.weaponMeleeDamageTypeMatch ? "true" : "false") + ",\"rangedDamageTypeMatch\":" +
             std::string(e.weaponRangedDamageTypeMatch ? "true" : "false") + "}";
        o += ",\"killerClass\":" + OrNull(e.killerClass);
        o += ",\"killerRef\":" + OrNull(e.killerRef);
        o += ",\"killerIsTrackedPlayer\":" + std::string(e.killerIsTrackedPlayer ? "true" : "false");
        o += ",\"bIsDeath\":" + JsonStr(e.isDeath);
        o += ",\"bShouldEnterDbno\":" + JsonStr(e.dbno);
        o += ",\"lifeStateAtDrain\":" + (e.haveLifeState ? std::to_string(e.lifeState) : std::string("null"));
        o += ",\"lifeStateFresh\":" + std::string(e.lifeStateFresh ? "true" : "false");
        o += ",\"drainLagMs\":" + std::to_string(e.drainLagMs);
        o += ",\"rawParams\":" + OrNull(e.rawHex);
        o += ",\"decision\":" + JsonStr(e.verdict);
        o += ",\"reason\":" + JsonStr(e.reason);
        o += ",\"deathKind\":" + JsonStr(e.deathKind);
        o += ",\"attribution\":" + OrNull(e.attribution);
        o += ",\"emitted\":" + OrNull(e.emittedAs) + "}";
    }
    o += "]}";
    return {200, o};
}

HandlerResult Events::Params(const std::string& cls, const std::string& func) {
    if (!Pool::F().ok) return {503, "{\"error\":\"the FName pool was not located\"}"};
    void* c = Classes::ByName(cls);
    if (!c) return {404, "{\"error\":\"no live UClass with that decoded name\"}"};
    for (const auto& fn : Pool::FunctionsOf(c)) {
        if (fn.first != func) continue;
        ParamPlan plan;
        BuildPlan(plan, fn.second, K_None);
        std::string o = "{\"class\":" + JsonStr(cls) + ",\"function\":" + JsonStr(func);
        char b[32];
        snprintf(b, sizeof b, "0x%llx", (unsigned long long)(uintptr_t)fn.second);
        o += ",\"ufunction\":\"" + std::string(b) + "\",\"frameSize\":" + std::to_string(plan.frameSize);
        o += ",\"paramCount\":" + std::to_string(plan.paramCount);
        o += ",\"params\":" + JsonStr(plan.summary);
        o += ",\"roles\":{\"victim\":" + OrNull(plan.victim.name) + ",\"killer\":" + OrNull(plan.killer.name) +
             ",\"causer\":" + OrNull(plan.causer.name) + ",\"weapon\":" + OrNull(plan.weapon.name) + "}}";
        return {200, o};
    }
    return {404, "{\"error\":\"that class has no UFunction with that name (a subclass may)\"}"};
}

HandlerResult Events::Npcs(const std::string& ref, size_t limit) {
    if (!g_warmReady.load()) return {503, "{\"error\":\"the class warm-up has not finished yet\"}"};
    U::Vec origin;
    bool haveOrigin = false;
    std::string originNote = "none";
    if (!ref.empty()) {
        Players::EnsureFresh(500);
        Players::Entry e;
        if (Players::Find(ref, e) && e.havePosition) {
            origin = e.position;
            haveOrigin = true;
            originNote = e.Ref();
        } else {
            originNote = "player '" + ref + "' not found or has no live position";
        }
    }
    uint64_t t0 = NowMs();
    auto rows = SweepNpcs(haveOrigin ? &origin : nullptr, limit ? limit : 200);
    std::string o = "{\"origin\":" + JsonStr(originNote) + ",\"sweepMs\":" + std::to_string(NowMs() - t0) +
                    ",\"returned\":" + std::to_string(rows.size()) + ",\"npcs\":[";
    for (size_t i = 0; i < rows.size(); i++) {
        if (i) o += ",";
        char b[32];
        snprintf(b, sizeof b, "0x%llx", (unsigned long long)(uintptr_t)rows[i].actor);
        o += "{\"ptr\":\"" + std::string(b) + "\",\"class\":" + JsonStr(rows[i].cls) + ",\"devName\":" +
             OrNull(rows[i].devName) + ",\"dead\":" + (rows[i].dead ? "true" : "false") + ",\"killable\":" +
             (rows[i].killable ? "true" : "false");
        if (rows[i].havePos) o += ",\"position\":" + PosJson(rows[i].x, rows[i].y, rows[i].z);
        if (rows[i].haveDist) o += ",\"distance\":" + JsonNum(rows[i].dist);
        o += "}";
    }
    return {200, o + "]}"};
}

HandlerResult Events::Entities() {
    if (!g_warmReady.load()) return {503, "{\"error\":\"the class warm-up has not finished yet\"}"};
    auto rows = SweepNpcs(nullptr, 4000);
    // One row per DISTINCT ENTITY, not per actor. The key is the display name when the actor has one
    // (`DuneNpcCharacter::m_Name` really does hold "Mobula Gang Member" on this build), else the UE
    // class — because a class is shared by dozens of differently-named NPCs and shipping the class as
    // a name is the catalogue failure this campaign does not repeat (memory: catalogue-human-names).
    struct Agg {
        size_t count = 0;
        std::string cls;
        bool named = false;
    };
    std::map<std::string, Agg> byKey;
    size_t named = 0, unnamed = 0;
    for (const auto& r : rows) {
        bool isName = !r.devName.empty() && EventsParse::LooksLikeDisplayName(r.devName);
        std::string key = isName ? r.devName : r.cls;
        auto& a = byKey[key];
        a.count++;
        a.named = isName;
        if (a.cls.empty()) a.cls = r.cls;
        if (isName) named++; else unnamed++;
    }
    std::string o = "{\"liveActors\":" + std::to_string(rows.size()) + ",\"entitiesReturned\":" +
                    std::to_string(byKey.size()) + ",\"namedActors\":" + std::to_string(named) +
                    ",\"unnamedActors\":" + std::to_string(unnamed) +
                    ",\"nameSource\":\"DuneNpcCharacter::m_Name (a NameProperty holding a DISPLAY name on this "
                    "build, verified live). Rows with no such name carry name:null + nameIsClassName:true and the "
                    "sidecar must name them from its own catalogue or DROP them.\",\"entities\":[";
    bool first = true;
    for (const auto& kv : byKey) {
        if (!first) o += ",";
        first = false;
        o += "{\"code\":" + JsonStr(kv.first) + ",\"name\":" +
             (kv.second.named ? JsonStr(kv.first) : std::string("null")) + ",\"nameIsClassName\":" +
             (kv.second.named ? "false" : "true") + ",\"class\":" + JsonStr(kv.second.cls) + ",\"liveCount\":" +
             std::to_string(kv.second.count) + "}";
    }
    return {200, o + "]}"};
}

HandlerResult Events::KillNearest(const JsonValue& body) {
    // ------------------------------------------------------------------------------------------
    // Is a kill path safely callable WITHOUT engine function symbols? YES, and this is why:
    //
    // `KillCharacter` is a UFUNCTION on `ADuneCharacter` (confirmed live in the class's own function
    // list), so it is dispatched by `UObject::ProcessEvent` — the one engine entry point this build
    // proves, whose original we hold. We therefore drive the game's OWN kill path with the game's own
    // dispatcher, and we never write a health field or a `m_bIsDead` flag.
    //
    // The honest limits, stated in the response too:
    //   * The parameter frame is zero-filled to `UFunction::PropertiesSize`, so any instigator
    //     parameter is null. The resulting kill therefore has NO killer, which is exactly right for
    //     a synthetic proof and exactly wrong as a stand-in for a player kill. Attribution is only
    //     provable by a real client kill; this proves the DETECTION half.
    //   * `ADuneCritterBase` does NOT derive `ADuneCharacter`, so critters have no `KillCharacter`.
    //     For those, `/debug/npcs` gives the actor list and distances so a human kill can be
    //     correlated.
    // ------------------------------------------------------------------------------------------
    std::string ref;
    double radius = 5000.0;
    bool dryRun = false;
    if (body.type == JsonValue::Object) {
        if (const JsonValue* v = body.get("ref")) if (v->isStr()) ref = v->str;
        if (const JsonValue* v = body.get("player")) if (v->isStr() && ref.empty()) ref = v->str;
        if (const JsonValue* v = body.get("radius")) if (v->isNum()) radius = v->num;
        if (const JsonValue* v = body.get("dryRun")) if (v->type == JsonValue::Bool) dryRun = v->b;
    }
    if (!g_warmReady.load()) return {503, "{\"error\":\"the class warm-up has not finished yet\"}"};

    U::Vec origin;
    bool haveOrigin = false;
    std::string originNote;
    if (!ref.empty()) {
        Players::EnsureFresh(500);
        Players::Entry e;
        if (Players::Find(ref, e) && e.havePosition) {
            origin = e.position;
            haveOrigin = true;
            originNote = e.Ref();
        }
    }
    if (!haveOrigin) {
        auto online = Players::All(false);
        for (const auto& e : online)
            if (e.havePosition) {
                origin = e.position;
                haveOrigin = true;
                originNote = e.Ref();
                break;
            }
    }
    // An explicit origin, so the DETECTION half can be proven on an EMPTY server. Without this the
    // whole entity-killed chain would stay untested until a human could join, and "it will work when
    // someone joins" is precisely the kind of claim this campaign does not make.
    if (!haveOrigin && body.type == JsonValue::Object) {
        const JsonValue* o = body.get("origin");
        if (o && o->type == JsonValue::Object) {
            const JsonValue *x = o->get("x"), *y = o->get("y"), *z = o->get("z");
            if (x && y && z && x->isNum() && y->isNum() && z->isNum()) {
                origin = {x->num, y->num, z->num};
                haveOrigin = true;
                originNote = "explicit origin (no player)";
            }
        }
        const JsonValue* any = body.get("any");
        if (!haveOrigin && any && any->type == JsonValue::Bool && any->b) {
            // No origin at all: take the first killable NPC in the sweep. Distances are meaningless
            // here and the response says so.
            originNote = "any (no origin; distances are not meaningful)";
        }
    }
    if (!haveOrigin && originNote.empty())
        return {404,
                "{\"error\":\"no player with a live position to measure from. Pass {\\\"ref\\\":\\\"acct:N\\\"} for a "
                "tracked player, {\\\"origin\\\":{\\\"x\\\":..,\\\"y\\\":..,\\\"z\\\":..}} to measure from a point, or "
                "{\\\"any\\\":true} to take the first killable NPC. GET /debug/npcs lists them.\"}"};

    auto rows = SweepNpcs(haveOrigin ? &origin : nullptr, 2000);
    const NpcRow* target = nullptr;
    for (const auto& r : rows) {
        if (r.dead || !r.killable) continue;
        if (haveOrigin) {
            if (!r.haveDist) continue;
            if (r.dist > radius) break;  // the sweep is distance-sorted
        }
        target = &r;
        break;
    }
    std::string listed = "{\"origin\":" + JsonStr(originNote) + ",\"candidates\":" + std::to_string(rows.size()) +
                         ",\"radius\":" + JsonNum(radius);
    if (!target)
        return {404, listed +
                         ",\"error\":\"no killable (ADuneCharacter-derived) live NPC inside the radius. Creatures "
                         "derived from ADuneCritterBase have no KillCharacter UFUNCTION; list them with GET "
                         "/debug/npcs and have a human kill one.\"}"};

    std::string note = "{\"target\":{\"class\":" + JsonStr(target->cls) + ",\"name\":" + OrNull(target->devName) +
                       ",\"distance\":" + JsonNum(target->dist) + "}" +
                       ",\"how\":\"the game's own KillCharacter UFUNCTION dispatched through the engine's own "
                       "ProcessEvent; no health field is written\"" +
                       ",\"attributionCaveat\":\"the parameter frame is zero-filled, so this kill has no "
                       "instigator - it proves DETECTION, never attribution\"";
    if (dryRun) return {200, listed + "," + note.substr(1) + ",\"dryRun\":true}"};

    void* actor = target->actor;
    std::string cls = target->cls;
    std::string err;
    bool called = false;
    int32_t deathFlagOff = -1;
    bool planned = true;
    bool ok = GameThread::Run([&] {
        // Re-validate on the game thread: the sweep ran off-thread and the actor may have died since.
        if (!LooksLikeUObject(actor)) { err = "the target actor is no longer a live UObject"; return; }
        void* acls = U::ReadPtr(actor, Reflect::Lay().objClass);
        void* fn = nullptr;
        for (void* c = acls; c && !fn;) {
            for (const auto& f : Pool::FunctionsOf(c))
                if (f.first == "KillCharacter") { fn = f.second; break; }
            c = Reflect::SuperStruct(c);
        }
        if (!fn) { err = "no KillCharacter UFunction on the target's class chain"; return; }
        uint32_t frame = U::StructPropertiesSize(fn);
        if (frame > 4096) { err = "KillCharacter's parameter frame is implausibly large; refusing"; return; }
        alignas(16) unsigned char params[4096];
        memset(params, 0, frame ? frame : 1);
        // A ZERO-FILLED FRAME ASKS FOR A DEFEAT, NOT A DEATH. `KillCharacter` takes `bIsDeath`, and
        // zero means false — the first live attempt dispatched successfully and produced nothing at
        // all for exactly this reason. So the one byte the game's own signature says means "this is a
        // death" is set, from the offset the UFunction's own FField chain gives us. Everything else
        // stays zero, which is what keeps the kill unattributed and honest.
        const ParamPlan* kp = PlanFor(fn);
        if (kp && kp->isDeathOff >= 0 && (uint32_t)kp->isDeathOff < frame) {
            params[kp->isDeathOff] = 1;
            deathFlagOff = kp->isDeathOff;
        } else {
            planned = false;
        }
        called = LiveHooks::CallProcessEvent(actor, fn, frame ? params : nullptr, err);
    }, 5000);
    if (!ok) return {503, "{\"error\":\"game thread unavailable\"}"};
    if (!called) return {500, listed + "," + note.substr(1) + ",\"error\":" + JsonStr(err) + "}"};
    std::string flag = deathFlagOff >= 0 ? ("bIsDeath set at parameter offset " + std::to_string(deathFlagOff))
                                         : (planned ? "this KillCharacter has no bIsDeath parameter"
                                                    : "no parameter plan for this class's KillCharacter, so bIsDeath "
                                                      "could NOT be set - the call most likely asked for a DEFEAT");
    return {200, listed + "," + note.substr(1) + ",\"deathFlag\":" + JsonStr(flag) +
                     ",\"dispatched\":true,\"next\":\"poll GET /events for entity-killed; the victim class should be " +
                     JsonEscape(cls) + "\"}"};
}

// ================================================================================================
// 11. DIAGNOSTICS
// ================================================================================================

std::string Events::DiagnosticsJson() {
    auto& st = PluginState::Get();
    std::string o = "{\"filter\":" + JsonStr(g_filterHow);
    o += ",\"filterNames\":" + std::to_string(g_filterCount.load());
    o += ",\"sources\":[";
    struct Src {
        const char* event;
        const char* mechanism;
    };
    const Src kSrc[] = {
        {"entity-killed",
         "ProcessEvent name filter: DuneCritterBase::{OnDeathOrDefeatOnServer,BPOnDeath} and "
         "DuneCharacter::{ReceiveMulticastDeathOrDefeat,KillCharacter} - for all four `self` IS the victim, which "
         "is why they and not ReceiveMulticastKill are allowed to emit"},
        {"player-death",
         "the same filter; killer/weapon from the UFunction's own parameter properties, else a UNIQUE "
         "ReceiveMulticastKill hint inside 1500 ms, else killer:null with attribution saying why"},
        {"player-connected", "AGameModeBase::PostLogin on the live game-mode object (a precise hint; identity stays "
                             "with the sidecar's Postgres poller)"},
        {"player-disconnected", "AGameModeBase::Logout, same"},
        {"log", "not emitted: the sidecar reads the container log itself"},
    };
    for (size_t i = 0; i < sizeof kSrc / sizeof kSrc[0]; i++) {
        if (i) o += ",";
        o += "{\"event\":" + JsonStr(kSrc[i].event) + ",\"status\":" + JsonStr(st.Capability(kSrc[i].event)) +
             ",\"mechanism\":" + JsonStr(kSrc[i].mechanism) + "}";
    }
    o += "],\"hits\":{";
    for (uint8_t k = 1; k < K_Count; k++) {
        if (k > 1) o += ",";
        o += JsonStr(KindName(k)) + ":" + std::to_string(g_hits[k].load());
    }
    o += "},\"emitted\":{\"entity-killed\":" + std::to_string(g_emitted[0].load()) + ",\"player-death\":" +
         std::to_string(g_emitted[1].load()) + ",\"player-connected\":" + std::to_string(g_emitted[2].load()) +
         ",\"player-disconnected\":" + std::to_string(g_emitted[3].load()) + "}";
    o += ",\"ring\":{\"enqueued\":" + std::to_string(g_ringHead.load()) + ",\"drained\":" +
         std::to_string(g_ringTail.load()) + ",\"dropped\":" + std::to_string(g_ringDropped.load()) + ",\"size\":" +
         std::to_string(kRing) + "}";
    o += ",\"ambiguousKillsSuppressed\":" + std::to_string(g_suppressedAmbiguous.load());
    // `defeatsDropped` is retained at 0 for contract compatibility with L2's /health; it is
    // superseded by knockDownsDropped/duplicatesDropped, which name the real reason.
    o += ",\"defeatsDropped\":" + std::to_string(g_defeatsDropped.load());
    o += ",\"knockDownsDropped\":" + std::to_string(g_knockDownsDropped.load());
    o += ",\"duplicatesDropped\":" + std::to_string(g_duplicatesDropped.load());
    o += ",\"lifeStateReadbacks\":" + std::to_string(g_lifeStateReadbacks.load());
    o += ",\"boolBytesUnreadable\":" + std::to_string(g_boolUnreadable.load());
    o += ",\"deathLogEntries\":" + std::to_string(g_deathLogSeq.load());
    // LANE L2c. `victimClassChainOnly` is the counter that proves the classification fix is doing work:
    // it counts player deaths the class chain caught while the registry could not resolve the victim —
    // the exact case that used to be emitted as `entity-killed: BP_DunePlayerCharacter_C`.
    o += ",\"classification\":{\"victimClassChainOnly\":" + std::to_string(g_victimClassChainOnly.load()) +
         ",\"selfOrEnvironmentDeaths\":" + std::to_string(g_selfDeaths.load()) +
         ",\"unnamedEntityKillsFlagged\":" + std::to_string(g_unnamedEntityKills.load()) +
         ",\"rule\":\"the victim's CLASS CHAIN decides player-death vs entity-killed; identity is a "
         "second, best-effort step reported as identityComplete. entity-killed is emitted only for a "
         "non-player victim, and with no human display name it carries entity:null + dropUnnamed:true "
         "for the sidecar to drop\"}";
    // LANE L2d. Whether the weapon read is actually working, and which offsets it got. `resolved` going
    // non-zero with `byDamageTypeClassMatch` alongside it is the proof; all-zero `offsets` would mean
    // this build renamed the properties and the weapon degraded to null rather than guessed.
    o += ",\"weapon\":{\"resolved\":" + std::to_string(g_weaponResolved.load()) + ",\"byDamageTypeClassMatch\":" +
         std::to_string(g_weaponByDamageTypeMatch.load()) + ",\"noKillerCharacter\":" +
         std::to_string(g_weaponNoCharacter.load()) + ",\"noEvidence\":" + std::to_string(g_weaponNoEvidence.load()) +
         ",\"offsets\":{\"m_WeaponComponent\":" + std::to_string(g_warm.weaponComponentOff) +
         ",\"m_CachedMeleeWeaponData\":" + std::to_string(g_warm.meleeDataOff) + ",\"m_bHasWeaponInHand\":" +
         std::to_string(g_warm.hasWeaponInHandOff) + ",\"m_CachedMeleeWeaponName\":" +
         std::to_string(g_warm.meleeNameOff) + ",\"m_DamageTypeClass\":" + std::to_string(g_warm.meleeDamageTypeOff) +
         ",\"m_WeaponName\":" + std::to_string(g_warm.wacNameOff) + ",\"m_CachedWeaponDamageType\":" +
         std::to_string(g_warm.wacDamageTypeOff) + ",\"m_bActive\":" + std::to_string(g_warm.wacActiveOff) + "}" +
         ",\"meleeStruct\":" + JsonStr(g_warm.meleeStructNote) + ",\"weaponInHandIsPackedBitfieldPair\":" +
         std::string(g_warm.weaponInHandIsPackedPair ? "true" : "false") +
         ",\"weaponInHandNote\":\"m_bHasWeaponInHand and m_bServerWantsWeaponInHand reflect at the same byte "
         "offset on this build and the FField chain exposes no ByteMask, so the byte is read as 'either "
         "weapon-in-hand flag is set'. It is corroborating evidence only and never refuses a weapon\"" +
         ",\"rule\":\"the weapon is read off the KILLER (the wielder), because the death frame carries only a "
         "damage-TYPE class and no damage causer. Which of the killer's two cached weapons dealt the damage is "
         "decided by comparing damage-type CLASS POINTERS, never by a name match; weaponItemCode is an item "
         "template id, in the same code space as items.template_id\"}";
    o += ",\"connectAnnounce\":" + PendingConnectsJson();
    o += ",\"deathGate\":\"bShouldEnterDbno (down-but-not-out) + a player life-state read-back; "
         "bIsDeath is DESCRIPTIVE only - on this build a 'defeat' is a lethal death (L2b)\"";
    o += ",\"weakPtrSerial\":{\"agreed\":" + std::to_string(g_weakSerialAgreed.load()) + ",\"disagreed\":" +
         std::to_string(g_weakSerialDisagreed.load()) +
         ",\"note\":\"FUObjectItem::SerialNumber@0x10 is the one stock-UE assumption in the weak-pointer "
         "reader; resolution does not depend on it, this is its corroboration\"}";
    o += ",\"unplannedFunctionsDropped\":" + std::to_string(g_unplannedDropped.load());
    // The discovered parameter layouts, verbatim. This is what settles ReceiveMulticastKill's
    // direction from a real build instead of from an assumption.
    o += ",\"functions\":[";
    size_t n = g_planCount.load(std::memory_order_acquire);
    for (size_t i = 0; i < n; i++) {
        if (i) o += ",";
        char b[32];
        snprintf(b, sizeof b, "0x%llx", (unsigned long long)(uintptr_t)g_plans[i].func);
        o += "{\"name\":" + JsonStr(KindName(g_plans[i].kind)) + ",\"ufunction\":\"" + b + "\",\"frameSize\":" +
             std::to_string(g_plans[i].frameSize) + ",\"selfIsVictim\":" +
             (SelfIsVictim(g_plans[i].kind) ? "true" : "false") + ",\"params\":" + JsonStr(g_plans[i].summary) +
             ",\"roles\":{\"victim\":" + OrNull(g_plans[i].victim.name) + ",\"killer\":" +
             OrNull(g_plans[i].killer.name) + ",\"causer\":" + OrNull(g_plans[i].causer.name) + ",\"weapon\":" +
             OrNull(g_plans[i].weapon.name) + "}" +
             ",\"isDeathOffset\":" + (g_plans[i].isDeathOff >= 0 ? std::to_string(g_plans[i].isDeathOff) : "null") +
             ",\"instigator\":{\"param\":" + OrNull(g_plans[i].instigatorName) + ",\"offset\":" +
             (g_plans[i].instigatorOff >= 0 ? std::to_string(g_plans[i].instigatorOff) : "null") + ",\"struct\":" +
             JsonStr(g_plans[i].instSummary) + ",\"killer\":" + OrNull(g_plans[i].instKiller.name) +
             ",\"causer\":" + OrNull(g_plans[i].instCauser.name) + ",\"weapon\":" +
             OrNull(g_plans[i].instWeapon.name) + ",\"controller\":" +
             OrNull(g_plans[i].instControllerRole.name) + "}}";
    }
    o += "],\"notes\":[";
    {
        Guard g(g_lock);
        for (size_t i = 0; i < g_notes.size(); i++) {
            if (i) o += ",";
            o += "{\"what\":" + JsonStr(g_notes[i].first) + ",\"detail\":" + JsonStr(g_notes[i].second) + "}";
        }
    }
    o += "],\"processEventSlot\":" +
         (Resolve::ProcessEventSlot() == SIZE_MAX ? std::string("null") : std::to_string(Resolve::ProcessEventSlot())) +
         ",\"processEventConfirmed\":" + (Resolve::ProcessEventConfirmed() ? "true" : "false") + "}";
    return o;
}
