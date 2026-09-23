// Event sources. Everything here pushes into PluginState::EmitEvent(type, dataJson); GET /events
// serves the ring buffer (cursor + bootId) and knows nothing about the sources.
//
// ---------------------------------------------------------------------------------------------
// WHAT THIS PLUGIN EMITS, AND WHY ONLY THESE
//
// The sidecar already gets chat (RabbitMQ `chat.intercept`), the player roster and the
// player-death *edge* (Postgres `player_state.life_state` ∈ {Alive, Dead, DeadByCoriolis,
// DeadBySandworm}) out of process. The plugin exists for the events those sources cannot produce:
//
//   entity-killed         An NPC / creature died. Needs the killer and, where derivable, the
//                         weapon. There is NO out-of-process source for this at all.
//   player-death          Emitted with **attribution** (killer + cause). The sidecar can see *that*
//                         a player died from the life_state edge; only the plugin can say by whom.
//   player-connected /    PostLogin / Logout. PRECISE HINTS ONLY: the sidecar's Postgres presence
//   player-disconnected   poller stays authoritative for identity, because the FLS id the connector
//                         uses as `gameId` does not exist in this process (see players.h).
//   log                   Redacted server-log tail, diagnostic only.
//
// ---------------------------------------------------------------------------------------------
// HOW DEATHS ARE SEEN ON THIS BUILD (lane L2, measured)
//
// Every death/kill candidate is a UFUNCTION, so all of them arrive through `UObject::ProcessEvent`,
// slot 86 — the detour lane L1b already installed and proved. L2 therefore adds a NAME FILTER, not a
// new hook. The filter is a comparison of the UFunction's `FName` comparison index against a tiny
// sorted table, which is why it costs tens of nanoseconds on a path that runs ~6 000×/s.
//
// The functions, and what each one authoritatively tells us:
//
//   | UFunction                               | class            | `self` is             | used for |
//   |-----------------------------------------|------------------|-----------------------|----------|
//   | `OnDeathOrDefeatOnServer`               | DuneCritterBase  | the dying critter     | victim   |
//   | `BPOnDeath`                             | DuneCritterBase  | the dying critter     | victim   |
//   | `KillCharacter`                         | DuneCharacter    | the character killed  | victim   |
//   | `ReceiveMulticastDeathOrDefeat`         | DuneCharacter    | the character that died| victim  |
//   | `ReceiveMulticastKill`                  | DuneCharacter    | ⚠️ the KILLER (probably)| hint only|
//
// ⚠️ **The F20 rule (memory: hard-test-no-overclaim) decides the last row.** "ReceiveMulticastKill"
// is symmetric to "ReceiveMulticastDeathOrDefeat", so `self` is most likely the killer rather than
// the victim — but "most likely" is exactly how a victim and a killer end up the wrong way round in
// a Discord kill feed. So **no event is ever emitted from `ReceiveMulticastKill`.** It is recorded as
// a *killer hint* and merged into a death that another, direction-unambiguous function reported, and
// only when the merge is unique inside the dedupe window. If it is ambiguous, `killer` stays null and
// `attribution` says so. Its observed parameters are published in
// `/health.diagnostics.eventSources.functions` so the direction can be settled from a real kill
// rather than from an opinion.
//
// Parameters are read by walking the UFunction's OWN parameter properties (`Pool::PropsOf` on the
// UFunction, which is a UStruct) and matching their decoded names. No parameter offset is hardcoded,
// and a function whose parameters name neither a victim nor a killer yields null fields, never a
// guess.
//
// ---------------------------------------------------------------------------------------------
// THE DISCIPLINES THAT ARE NOT NEGOTIABLE (docs/gamethread-policy.md, memory: avoid-game-thread)
//
//  1. HOOKS GO ON LIVE OBJECTS, NEVER ON A FILE-IMAGE VTABLE (this binary is a PIE).
//  2. VALIDATE BEFORE READING: every engine pointer goes through LooksLikeUObject() first.
//  3. THE GAME THREAD DOES ONLY WHAT THE ENGINE FORCES THERE. The detour decides whether it cares,
//     copies out primitives (pointers, FName indices, a position, an enum byte) into a lock-free
//     single-writer ring, and returns. Name decoding, dedupe, correlation and JSON all happen on the
//     housekeeping thread.
//  4. EVERY CAPABILITY DEGRADES, NOTHING FAILS.
#pragma once
#include "common.h"

namespace Events {

/// Early registration, on the init thread, before the live class index exists. Every event
/// capability is set to `degraded` with "waiting for the live class index" so a /health read in the
/// first minute of a boot is honest rather than empty.
void Init();

/// The real initialisation: the ProcessEvent name filter, the parameter plans, the NPC class set and
/// the player registry's property offsets. **Must run after `Classes::Build()` and after
/// `Pool::DiscoverFieldLayout()`** — the first gives it UClass pointers by name, the second is what
/// makes `Pool::PropsOf` able to walk an FField chain at all. Idempotent; retried from Housekeep()
/// until the class index exists, because on a cold Hagga Basin boot it appears ~60 s in.
/// Returns true once it has run.
bool InitLive();

// Every 2 s on the housekeeping thread: drain the hit ring into real events, expire dedupe windows,
// re-learn parameter plans for UFunctions seen for the first time, poll the log tail.
void Housekeep();

/// GAME THREAD, hot path. Called by the ProcessEvent detour for every dispatched UFunction.
/// Returns true when the call was one we care about (used by the perf counters' `hit` flag).
/// Must not allocate, must not lock, and must not call into the game.
bool OnProcessEvent(void* self, void* func, void* params);

/// GAME THREAD. From the PostLogin / Logout detours. Both are idempotent against the sweep-based
/// reconciler: a controller the player registry already holds online is refreshed, not re-announced.
void OnPostLogin(void* controller);
void OnLogout(void* controller);

/// HOUSEKEEPING THREAD, from the presence reconciler (`presence.cpp`). `source` names the mechanism
/// and is carried in the event payload, so a connector operator can always tell which of the two saw
/// the join.
///
/// `OnPresenceConnect` enters the game thread once to read the new player's identity and returns
/// **false** when it could not (pump unavailable, identity unreadable). A false answer means "not
/// announced" — the reconciler retries on its next sweep rather than emitting a player with no ref,
/// because Takaro rejects a player without a gameId and an empty event is worse than a late one.
bool OnPresenceConnect(void* controller, const char* source);
void OnPresenceDisconnect(void* controller, const char* source);

std::string DiagnosticsJson();

// ---- debug / proof tooling (token + TAKARO_PLUGIN_DEBUG=1, gated in http.cpp) -------------------

/// POST /debug/kill-nearest {player?, ref?, radius?, dryRun?}
/// Kills the nearest killable NPC to a player through the game's OWN `KillCharacter` UFUNCTION,
/// dispatched with the engine's own `ProcessEvent` — never by writing a health field. See the long
/// comment on the implementation for what is and is not provable this way.
HandlerResult KillNearest(const JsonValue& body);

/// GET /debug/npcs[?ref=<player>&limit=n] — every live NPC/creature actor, with its class, its dev
/// name and (when a player is given) its distance. This is the human-correlatable fallback for a
/// build where no kill path is safely callable, and it is also how we know whether the idle world
/// contains any killable entity at all.
HandlerResult Npcs(const std::string& ref, size_t limit);

/// GET /entities — the NPC/creature classes seen alive. Catalogue rule (memory:
/// catalogue-human-names): `name` is a display name or absent; a dev/class name goes in `code` with
/// `nameIsClassName: true` so the sidecar drops it rather than shipping it to Takaro as a name.
HandlerResult Entities();

/// GET /debug/scriptstructs?match=<fragment> — every live `UScriptStruct` whose name contains the
/// fragment, with its members. A `StructProperty`'s inner struct is NOT on the FField chain, so this
/// sweep is how `InstigatorInfo`'s layout — and therefore the killer — is derived on this build.
HandlerResult ScriptStructs(const std::string& match, size_t limit);

/// GET /debug/deathlog[?limit=n] — the last N calls the death filter caught, newest first, with the
/// raw parameter frame in hex and the exact decision taken on it (verdict + reason + the bool bytes
/// + the player life-state read-back). This is the tool that ends the guessing about why a death did
/// or did not become an event; L2 had no such record, which is why its `bIsDeath` gate silently
/// discarded every real death on the live server.
HandlerResult DeathLog(size_t limit);

/// GET /debug/params?class=X&func=Y — one UFunction's parameter properties, which is how the role
/// mapping above gets settled against a real build instead of an assumption.
HandlerResult Params(const std::string& cls, const std::string& func);

}  // namespace Events
