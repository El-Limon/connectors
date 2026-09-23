// Pure parsing helpers for the Dune: Awakening dedicated-server log.
//
// Everything here is a free function over std::string with no process state, no game pointers and
// no UObject touch, so tests/unit_test.cpp exercises the real code rather than a copy of it.
// events.cpp owns the file handle, the rotation logic and the emitting; this file owns the grammar.
//
// ⚠️ SCOPE IS MUCH SMALLER THAN THE VEIN TREE THIS WAS PORTED FROM. In this connector the log is a
// *diagnostic* source, not an event source:
//   * chat comes from the game's own RabbitMQ `chat.intercept` exchange, consumed by the sidecar —
//     never scraped out of the log,
//   * connect/disconnect come from PostLogin/Logout hooks here plus the Postgres online-state diff
//     in the sidecar,
//   * player-death comes from the Postgres `life_state` edge (Alive / Dead / DeadByCoriolis /
//     DeadBySandworm) plus this plugin's attribution.
// So there is no chat/join/character-select grammar in this file. What is here is what we can be
// sure of *before the server has ever booted*: the stock UE line shape, redaction, and a noise
// filter. Dune-specific grammar is added only once it has been observed in a real boot log
// (research note: the `Unreal Engine version: %s` line is the first thing to capture at M-boot).
#pragma once
#include "common.h"

namespace EventsParse {

// One UE log line split into its parts. The first ~20 lines of a log file are written before
// LogTimes initialises and carry no `[timestamp][frame]` prefix, so `hasPrefix` may be false while
// the line is still perfectly good.
struct LogLine {
    bool hasPrefix = false;
    std::string timestamp;  // "2026.09.21-05.58.10:857", empty without a prefix
    std::string frame;      // "27", empty without a prefix
    std::string category;   // "LogChat", empty when the line has no `Category: ` part
    std::string message;    // everything after "Category: "
};
LogLine SplitLogLine(const std::string& raw);

// `Unreal Engine version: 5.3.2-12345+++UE5+Release-5.3`. The version is NOT embedded as a string
// literal in this binary (offline probe: only the *format* string is there), so the boot log is the
// only place it exists. Returns "" when the line is not that line.
std::string ParseEngineVersionLine(const std::string& raw);

// Redaction on top of common.cpp's Redact(). Dune's own secrets that can appear in a log line:
//   * `ServerCommandsAuthToken` — the GM-command bus token; a leak here is a full admin bypass,
//   * `Bgd.ServerLoginPassword` — the join password,
//   * `DatabasePassword` / the Postgres DSN, and `ServiceAuthToken` (the Funcom FLS JWT),
//   * the stock UE login URL forms `?Password=` / `?Ticket=` / `?p=`.
// All of them are masked before a line can reach the ring buffer.
std::string RedactLogLine(const std::string& in);

// UE categories that are pure noise on a dedicated server; dropping them keeps the `log` capability
// from drowning the sidecar's poller. Dune ships `[Core.Log]` with LogChat=Verbose,
// LogWorldPersistenceSubsystem=Verbose and LogAuto=Verbose already turned up, so this matters more
// here than it did on VEIN.
bool IsNoise(const std::string& line);

// True for a 17-digit decimal SteamID64 starting with 7656119. Steam ids reach us through Postgres
// (`accounts.platform_id`), but the shape check is here because it is also how a log line's id is
// validated before it is trusted.
bool LooksLikeSteamId64(const std::string& s);
// True for a bare 16-hex FLS id (Takaro's `gameId` for this connector, per plan Decision 3).
bool LooksLikeFlsId(const std::string& s);

/// True when `s` reads like a player-facing DISPLAY name rather than a developer identifier.
/// See the implementation for the rules and for why this is a live question on this build.
bool LooksLikeDisplayName(const std::string& s);

// ---------------------------------------------------------------------------------------------
// LANE L2: the victim / killer / weapon role matcher.
//
// A death UFunction's parameters are discovered at runtime (name, type, offset from the UFunction's
// own FField chain), and which parameter is the victim and which is the killer is decided HERE, by
// name, so that the decision is unit-testable without a game process. That matters more than usual:
// getting it backwards is exactly the failure this campaign must not ship (memory:
// hard-take-no-overclaim, F20 — victim/killer/weapon must be the right way round), and a wrong role
// is invisible on an idle server.
//
// The rules:
//   * a victim, killer or causer must be an OBJECT reference — a float named `Target` is not a victim;
//   * keys are tried in priority order, most specific first, so `DamageCauser` never lands in the
//     killer slot and `InstigatorController` beats a bare `Instigator`;
//   * no match means NO field, never a positional guess.
struct ParamProp {
    std::string name;
    std::string type;
    int32_t offset = -1;
};
enum class ParamRole { Victim, Killer, Causer, Weapon };

// True for the FProperty class names that hold an object reference.
bool IsObjectPropertyType(const std::string& type);
// Index into `props` of the parameter that fills `role`, or -1. Stable and total: the same input
// always yields the same answer, and an empty parameter list yields -1 for every role.
int MatchParamRole(const std::vector<ParamProp>& props, ParamRole role);

// ---------------------------------------------------------------------------------------------
// LANE L2b: reading a bool out of a parameter frame, and the death decision table.
//
// WHY THIS EXISTS. L2 gated every death on `bIsDeath != 0` and on the live server that gate threw
// away 100 % of the real deaths (`defeatsDropped == hits`, `emitted.entity-killed == 0`), including
// a player death the game's own UI announced as "DEFEATED - Environment". Two hypotheses had to be
// separated, and only one of them survived:
//
//  (a) BITFIELD MISREAD. A UE `FBoolProperty` can be a C++ bitfield, in which case the byte at the
//      property's offset is shared and only `ByteMask` selects the bit — so a whole-byte read can
//      return a neighbouring flag's value. FALSIFIED on this build by the reflected layout itself:
//      `ReceiveMulticastDeathOrDefeat` reports `bIsDeath@0x10, bForceKeepInventoryOnDeath@0x11,
//      ShouldReportTelemetry@0x20` and `KillCharacter` reports `bShouldEnterDbno@0x10,
//      bIsDeath@0x11, bForceKeepInventoryOnDeath@0x12, bShouldSendDeathEventMessage@0x13`. Packed
//      bitfields SHARE one offset and differ only by mask; these occupy DISTINCT CONSECUTIVE BYTES,
//      which is exactly what UHT emits for function parameters (a parameter is always a whole
//      `bool`, never a bitfield). The whole-byte read is therefore the correct read here.
//      `ReadFrameBool` still refuses any byte that is neither 0 nor 1 — that is the only shape a
//      misaligned or bitfield-packed read can take, so a future build that does pack them degrades
//      to `Unreadable` instead of silently inventing an answer.
//
//  (b) SEMANTICS. `bIsDeath` does not mean "this character is now dead". The function's own name is
//      `DeathOrDefeat` and its sibling parameter on `KillCharacter` is `bShouldEnterDbno` — DBNO,
//      "down but not out", which IS the engine's knock-down flag. So the down-but-not-out case has
//      its own dedicated parameter, and a "defeat" (`bIsDeath == false`) is an ordinary lethal
//      Dune death: the one the UI prints as DEFEATED. This is the surviving hypothesis.
//
// The table below therefore gates on `bShouldEnterDbno`, keeps `bIsDeath` as a *descriptor*
// (`deathKind: death | defeat | unknown`) rather than a gate, and for a PLAYER victim corroborates
// with a life-state read-back a short time later — because a player is the one victim type that has
// a revive mechanic at all, and because that read is the authoritative "is this character dead now"
// signal. An NPC (`DuneNpcCharacter`) has no dead flag and no down-but-not-out state on this build,
// so for an NPC the event firing IS the death.
enum class BoolRead { False, True, Unreadable };

/// `raw` is the byte at the BoolProperty's parameter offset, `readOk` whether the read was in-bounds
/// and mapped. Anything other than a clean 0 or 1 is `Unreadable` (see (a) above).
BoolRead ReadFrameBool(uint8_t raw, bool readOk);
const char* BoolReadName(BoolRead b);

/// `EDunePlayerLifeState::Alive`. The enum's *names* are not reflected on this build, but Alive is
/// ordinal 0 (a live, un-downed player reads 0 in `/debug/players`), and this is only ever used to
/// refuse a death, never to assert one.
constexpr uint8_t kLifeStateAlive = 0;

enum class DeathVerdict {
    Emit,           // a real death: entity-killed / player-death
    Hint,           // ReceiveMulticastKill — killer hint, never an event
    DropKnockDown,  // down-but-not-out, correctly not a death
    DropDuplicate,  // the same victim already reported inside the dedupe window
};

struct DeathFacts {
    bool isMulticastKillHint = false;  // the call was ReceiveMulticastKill
    bool duplicate = false;            // this victim is already in the dedupe window
    bool victimIsPlayer = false;
    BoolRead dbno = BoolRead::Unreadable;     // `bShouldEnterDbno`, only KillCharacter has it
    BoolRead isDeath = BoolRead::Unreadable;  // `bIsDeath`
    // The life-state read-back, done on the housekeeping thread when the hit is drained. `fresh` is
    // false when the drain ran too late for the read to mean anything (the pump has no guaranteed
    // cadence and has been measured stalling for 50 s), in which case it is ignored entirely.
    bool haveLifeState = false;
    bool lifeStateFresh = false;
    uint8_t lifeState = 0;
};

struct DeathDecision {
    DeathVerdict verdict = DeathVerdict::Emit;
    const char* reason = "";
    // "death" | "defeat" | "unknown" — descriptive, from `bIsDeath`. Carried in the event meta as
    // `deathKind` so a consumer can tell the two apart without this plugin having to decide that
    // one of them is not a death.
    const char* deathKind = "unknown";
};

/// Total and pure: the same facts always give the same decision, and every branch names its reason.
DeathDecision DecideDeath(const DeathFacts& f);

// ---------------------------------------------------------------------------------------------
// LANE L2c: victim CLASSIFICATION, self/environment attribution, class-property name reads, the
// delayed connect announce, and the position-source rule.
//
// WHY THIS EXISTS. L2b decided "is this a player?" with a single question — did the player registry
// recognise this exact pointer? — and used the answer for BOTH the event type and the identity. On
// the live server (plugin `/events` seq 4 at 17:35:27Z and seq 5 at 17:45:51Z) that produced
//
//   {"type":"entity-killed","entity":null,"entityCode":"BP_DunePlayerCharacter_C",
//    "nameIsClassName":true,"player":{"characterName":"TakaroTest"}}
//
// for two of the PLAYER'S OWN deaths: the victim was `TakaroTest`'s pawn, the killer was himself, and
// the event said an unnamed creature had been killed by him. Compare seq 1 (16:59:56Z) and seq 9
// (17:59:23Z), which were `player-death`.
//
// ROOT CAUSE, from `/debug/deathlog` (the raw frames were still in the ring): the victim pointer in
// the first 8 bytes of `rawParams` is a DIFFERENT object on every death —
// `fa0b1700b6040900`, `72f71700d2860900`, `4e581700fb8d0900`, `52471600659b0900` — because Dune
// gives the player a NEW pawn on every respawn. `Players::Identify` was pure pointer equality against
// `Entry::{controller, playerState, pawn}`, and `Entry::pawn` is only refreshed by
// `Players::Refresh()`. So between a respawn and the next refresh the CURRENT pawn is not in the
// registry, and the victim silently became "not a player". The KILLER did not suffer this, which is
// exactly why the events looked half-right: the killer is resolved through
// `InstigatorInfo::m_Controller`, and a controller pointer survives a respawn. Seq 5's victim pointer
// `72f71700d2860900` is literally seq 3's killer pointer — the same pawn, recognised as the killer
// five minutes earlier and unrecognised as the victim.
//
// THE RULE THIS FILE NOW ENCODES: classification and identity are TWO INDEPENDENT STEPS, because
// they fail independently.
//   * CLASSIFICATION is by the victim's CLASS CHAIN. A class chain is readable for as long as the
//     object is, needs no registry, and survives every respawn: deriving `DunePlayerCharacter` (or
//     `DunePlayerController` / `DunePlayerState`) means a PLAYER died, and that is `player-death` —
//     never `entity-killed`, whatever the identity step manages.
//   * IDENTITY is best-effort on top, and says so in the payload (`identityComplete`).
//   * `entity-killed` stays reserved for a NON-player victim, and may only carry a human display
//     name. With no name it is emitted with `entity: null` and `dropUnnamed: true` so the sidecar
//     drops it — a class name is never published as a creature's name (memory: catalogue-human-names).
enum class DeathEventType { PlayerDeath, EntityKilled };

struct VictimFacts {
    /// The victim's UClass Super-chain contains one of the player classes. Read live off the object.
    bool classChainIsPlayer = false;
    /// The player registry resolved this actor — by pointer, or by walking the pawn's own
    /// `Controller`/`PlayerState` link back to a tracked controller.
    bool identityResolved = false;
    /// `DuneNpcCharacter::m_Name` produced a string that passes LooksLikeDisplayName.
    bool haveDisplayName = false;
};

struct VictimClassification {
    DeathEventType type = DeathEventType::EntityKilled;
    /// `player-death` only: false when the class chain says "a player died" but no identity could be
    /// resolved. The event is still emitted — a death with a weak ref beats a death reported as a
    /// creature kill — and the flag tells the sidecar to resolve the player its own way.
    bool identityComplete = true;
    /// `entity-killed` only: no human display name, so `entity` is null and the sidecar must DROP it.
    bool dropUnnamed = false;
    const char* reason = "";
};

/// Total and pure. Class chain first, identity second, name third.
VictimClassification ClassifyVictim(const VictimFacts& f);

// --- self / environment attribution -----------------------------------------------------------
// Live evidence: `/events` seq 10 carried `killer: {"ref":"acct:1","characterName":"TakaroTest"}` on
// TakaroTest's OWN death. The engine points `InstigatorInfo` at the victim when nothing else killed
// them (a fall, dehydration, a Coriolis storm), so "the killer is the victim" is the engine's way of
// saying "the environment did it" — and reporting the victim as their own killer would show up in
// Takaro as a suicide leaderboard entry for every environmental death.
struct AttributionFacts {
    bool haveKiller = false;            // a killer actor or controller was resolved at all
    bool killerIsVictimActor = false;   // the killer pointer IS the victim actor / its pawn / controller / playerState
    bool killerIsVictimPlayer = false;  // killer and victim resolved to the SAME tracked player
};
/// True when the resolved killer is the victim itself, i.e. this is a self or environmental death and
/// `killer` must be null.
bool IsSelfAttribution(const AttributionFacts& f);
/// The `attribution` string for that case. Kept as one constant so the plugin and the sidecar cannot
/// drift apart on the spelling.
extern const char* const kSelfAttribution;

// --- ClassProperty: the value IS a class -------------------------------------------------------
// Live evidence: every death carried `weaponCode: "BlueprintGeneratedClass"`, which is not a weapon —
// it is the METACLASS. `DeathDefeatCausingDamageType` is a `ClassProperty`, so the 8 bytes in the
// frame are a `UClass*`, not a pointer to an instance. Reading "the class of the thing at this
// pointer" therefore answers "the class of a class". The pointed-to object's OWN name is the answer.
bool PropertyHoldsClassPointer(const std::string& type);

// --- the WIELDED WEAPON (lane L2d) -------------------------------------------------------------
//
// Live evidence (Tester's two melee kills, /events seq 10 and 11): `weapon: null` and
// `weaponCode: "BP_DmgType_Melee_Quick_C"`, i.e. Takaro showed `weapon: ""`. The death frame
// (`ReceiveMulticastDeathOrDefeat`) carries ONLY the damage-TYPE class and no damage causer, so the
// weapon can never come from the frame. It has to be read off the KILLER.
//
// What the live process models (read with /debug/object on a real NPC, 2026-09-21):
//
//   ADuneCharacter::m_WeaponComponent          ObjectProperty @0x10b0 -> UWeaponActorComponent
//   UWeaponActorComponent::m_WeaponName        NameProperty   @0x718  -> "ChoamSda2"
//   UWeaponActorComponent::m_CachedWeaponDamageType  ClassPtrProperty @0x148
//   UWeaponActorComponent::m_bActive           BoolProperty   @0x224
//   ADuneCharacter::m_CachedMeleeWeaponData    StructProperty @0xab0  -> FCachedMeleeWeaponData
//   FCachedMeleeWeaponData::m_CachedMeleeWeaponName  NameProperty  @0x14
//   FCachedMeleeWeaponData::m_DamageTypeClass        ClassProperty @0x110
//   ADuneCharacter::m_bHasWeaponInHand         BoolProperty   @0x1bce
//
// **`m_WeaponName` is in the ITEM TEMPLATE ID space**, which is the one that matters: the live NPC's
// `"ChoamSda2"` is a verbatim `items.template_id` and the sidecar catalogue maps it to "Maula Pistol".
// So a weapon read this way joins to a real display name, which a damage-type class never can.
//
// The DISCRIMINATOR is the damage type, not the slot. A character holds both a cached melee weapon
// and a weapon component at all times, so "which one killed" is decided by comparing the death's
// damage-type CLASS POINTER against the two cached damage-type class pointers — a pointer compare, no
// dereference, no name match. Only when neither matches does it fall back to weaker evidence, and it
// refuses outright rather than naming the gun in a player's hand for a knife kill.
struct WieldedWeaponFacts {
    bool haveMeleeName = false;           // m_CachedMeleeWeaponData.m_CachedMeleeWeaponName decoded non-empty
    bool meleeDamageTypeMatches = false;  // ...m_DamageTypeClass == the death's damage-type class pointer
    bool haveRangedName = false;          // WeaponActorComponent::m_WeaponName decoded non-empty
    bool rangedDamageTypeMatches = false; // m_CachedWeaponDamageType == the death's damage-type class pointer
    bool weaponInHand = false;            // ADuneCharacter::m_bHasWeaponInHand
    bool weaponComponentActive = false;   // UWeaponActorComponent::m_bActive
    bool damageTypeIsMelee = false;       // the damage-type class NAME says melee (see IsMeleeDamageTypeName)
};
enum class WeaponSource {
    None,
    MeleeDamageTypeMatch,   // strongest: the melee cache's own damage-type class IS the one that killed
    RangedDamageTypeMatch,  // strongest: the weapon component's cached damage type IS the one that killed
    MeleeCategory,          // the damage type only says "melee", and a melee weapon name is cached
    WeaponInHand,           // a non-melee damage type and a weapon actually in hand
};
struct WieldedWeapon {
    WeaponSource source = WeaponSource::None;
    bool useMelee = false;  // which of the two names the caller should publish
    /// 2 = the damage-type class pointers matched, 1 = category/in-hand evidence only, 0 = nothing.
    int confidence = 0;
    const char* reason = "no weapon evidence on the killer";
};
/// Pure, total, unit-tested. Never invents a name and never crosses the two slots: a melee damage type
/// with no cached melee name yields `None` rather than the gun the killer also carries.
WieldedWeapon DecideWieldedWeapon(const WieldedWeaponFacts& f);
const char* WeaponSourceName(WeaponSource s);
/// True when a damage-type class name says melee. `BP_DmgType_Melee_Quick_C`,
/// `BP_DmgType_Melee_Slow_Unshielded_C` — both live values — and anything else carrying `Melee`.
bool IsMeleeDamageTypeName(const std::string& damageTypeClassName);

// --- the delayed connect announce --------------------------------------------------------------
// Live evidence: `/events` seq 1/7/9 all announced `characterName: "Tester"` — the ACCOUNT persona
// from `APlayerState::PlayerNamePrivate` — with `accountId` set but all three actor ids null, while
// `/players` later reported the real character `TakaroTest`. At `PostLogin` the controller's
// persistence component has not been populated yet, so `m_CharacterName` is empty and the payload
// fell through to the engine's session name. The sidecar keys on `accountId`, so this is cosmetic —
// but a connect announcing the wrong name is exactly the kind of thing that is believed later.
//
// The fix is the one the reconciler already implements for an unannounceable connect: hold it and
// retry on the next housekeeping pass, bounded, then emit what we have and SAY it is incomplete.
struct ConnectIdentityFacts {
    bool haveAccountId = false;
    /// The persistence component's own `m_CharacterName` — NOT the PlayerState session name.
    bool havePersistenceCharacterName = false;
    uint64_t ageMs = 0;  // since the connect was first seen
};
enum class ConnectAnnounce { Announce, Retry, AnnounceIncomplete };
/// Long enough for the persistence component to populate, short enough that a connect is never lost.
constexpr uint64_t kConnectGraceMs = 10'000;
ConnectAnnounce DecideConnectAnnounce(const ConnectIdentityFacts& f, uint64_t graceMs = kConnectGraceMs);
const char* ConnectAnnounceName(ConnectAnnounce a);

// --- the position source rule ------------------------------------------------------------------
// Live evidence: the sidecar logged `plugin-origin-rejected:playerState` 24 times. `ReadLocation`
// fell back to the PlayerState actor whenever `Entry::pawn` was null or stale — the same staleness
// that caused the misclassification above — and an `APlayerState` is a non-spatial actor whose root
// component sits at the world origin. So the fallback produced a syntactically valid position at
// (0,0,0) that the sidecar was right to reject.
//
// The rule: a pawn's position always wins, and a PlayerState position is only reported when it is
// NOT at the origin. Otherwise there is no position at all, which is an honest answer.
enum class PositionSource { Pawn, PlayerState, None };
PositionSource DecidePositionSource(bool pawnHasPosition, bool playerStateHasPosition, bool playerStateAtOrigin);
const char* PositionSourceName(PositionSource s);
/// True when all three components are within `eps` of zero. Dune's world coordinates are centimetres
/// and a real player is hundreds of metres from the origin, so a 1 cm box can only be "unset".
bool IsOriginPosition(double x, double y, double z, double eps = 1.0);

}  // namespace EventsParse
