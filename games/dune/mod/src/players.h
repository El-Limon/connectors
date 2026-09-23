// The live player registry: the plugin's answer to "who is this actor, and what can the sidecar
// join it against?".
//
// ---------------------------------------------------------------------------------------------
// THE IDENTITY PROBLEM, AND THE JOIN THIS FILE IMPLEMENTS
//
// Takaro's `gameId` for this connector is the **FLS id** (16 hex, `accounts."user"`), and the plugin
// cannot read it: lane L1b established that no UPROPERTY on `DunePlayerState` /
// `DunePlayerStateBase` / `DunePlayerConnectionInfo` carries an account id, and the FLS id is a
// string the map process never needs.
//
// What the live class layouts DO carry (lane L2, read off the running process with
// `/debug/functions?class=…`, no instances needed) is the persistence identity — and it lines up
// with the Postgres schema exactly:
//
//   DunePlayerControllerBase.m_DatabaseAccountId            Struct, 8 bytes @0xa68
//   DunePlayerControllerPersistenceComponent
//       .m_AccountID                 Struct 8B  ->  encrypted_player_state.account_id   (bigint)
//       .m_PlayerStateUniqueID       Struct 8B  ->  encrypted_player_state.player_state_id
//       .m_PlayerControllerUniqueID  Struct 8B  ->  encrypted_player_state.player_controller_id
//       .m_PlayerCharacterUniqueID   Struct 8B  ->  encrypted_player_state.player_pawn_id
//       .m_CharacterName             FString    ->  decrypt(encrypted_character_name)
//
// and in Postgres all three `*_id` columns are `bigint REFERENCES actors(id)`, while `account_id` is
// `bigint REFERENCES encrypted_accounts(id)` and `accounts` is the view that turns that row's `id`
// into the FLS id. So:
//
//   **plugin `accountId` --(accounts.id)--> accounts."user" == the FLS id == Takaro's gameId.**
//
// That is a ONE-HOP join on a primary key, which is why it is the primary route. The three actor ids
// are the independent cross-checks (`player_state.player_controller_id` is also what a GM command's
// `ByPlayerId` resolves against), and `characterName` is the human-readable fallback for a build
// where the persistence component has not been populated yet.
//
// CONFIDENCE, stated honestly: the property NAMES and their offsets are read live from the class's
// own FField chain, so the *addresses* are not guesses. What is **unproven until the first join** is
// (a) that each 8-byte struct is a plain 64-bit id (its inner layout is not reflected — the offsets
// only prove the struct is 8 bytes wide), and (b) that the values equal the Postgres rows. Every
// field is therefore reported with the offset it came from, a zero value is reported as **absent
// rather than 0**, and the sidecar treats a mismatch as "no join" instead of picking a player.
// ---------------------------------------------------------------------------------------------
//
// Thread rules: `NoteLogin`/`NoteLogout` run on the game thread from the PostLogin/Logout detours
// and do bounded, allocation-light reads. `Refresh()` runs on the game thread from a queued job.
// Everything else (the JSON, the lookups) runs on HTTP threads against a mutex-guarded copy of
// primitives — never against a live UObject.
#pragma once
#include "common.h"

#include "uobj.h"

namespace Players {

struct Entry {
    // Live pointers. Only ever compared, never dereferenced off the game thread.
    void* controller = nullptr;
    void* playerState = nullptr;
    void* pawn = nullptr;

    // The join keys. 0 means "not readable yet" and is serialised as `null`, never as 0.
    uint64_t accountId = 0;
    uint64_t playerStateId = 0;
    uint64_t controllerId = 0;
    uint64_t pawnId = 0;

    std::string characterName;  // persistence component's own copy
    std::string playerName;     // APlayerState::PlayerNamePrivate
    int32_t playerId = 0;       // APlayerState::PlayerId (per-session, NOT a DB id)

    U::Vec position;
    double pitch = 0, yaw = 0;
    bool havePosition = false;
    const char* positionSource = "unknown";  // "pawn" | "playerState" | "unknown"

    uint64_t firstSeenMs = 0;
    uint64_t lastUpdateMs = 0;
    uint64_t generation = 0;
    bool online = true;
    std::string identityHow;  // which properties produced the ids, for /health and the report

    /// The stable reference the sidecar and the event payloads use. Prefers the DB account id (the
    /// one-hop route to the FLS id), then the player-state actor id, then the character name.
    std::string Ref() const;
};

/// Called once, off the game thread, after the class index exists: warms every property offset this
/// module needs so the detours never walk a property list on the game thread.
void Init();

/// GAME THREAD. `controller` is PostLogin's `NewPlayer`.
void NoteLogin(void* controller);
/// GAME THREAD. `controller` is Logout's `Exiting`. Keeps the entry for `kLingerMs` so a death event
/// arriving just after the logout can still be attributed.
void NoteLogout(void* controller);

/// GAME THREAD. Re-reads ids that were not available at login, resolves the pawn and refreshes every
/// position. Bounded: at most `kMaxPlayers` entries, a handful of reads each.
void Refresh();

/// Drops entries whose logout linger expired. Off-thread safe.
void Expire();

/// Off-thread. True when this exact controller pointer is in the registry AND still marked online.
/// This is the dedupe seam between the PostLogin/Logout detours and the sweep-based reconciler
/// (`presence.cpp`): whichever mechanism sees an edge first records it here, and the other one then
/// refreshes instead of emitting a second event.
bool IsTrackedOnline(void* controller);

/// Off-thread. Identifies an actor seen in a death/kill event. Returns true and fills `out` when the
/// actor is one of our tracked controllers / player states / pawns.
///
/// ⚠️ PURE POINTER EQUALITY, and that is not enough on its own — see `IdentifyActor`.
bool Identify(void* actor, Entry& out);

/// The same question, but it does not depend on the registry's cached pawn pointer being current.
///
/// LANE L2c. `Identify` matches `Entry::{controller, playerState, pawn}` by pointer, and Dune gives a
/// player a BRAND NEW PAWN on every respawn. Between a respawn and the next `Refresh()` the live pawn
/// is therefore not in the registry, so a player who died was reported as an unidentified creature:
/// plugin `/events` seq 4 and seq 5 were `entity-killed` with `entityCode:
/// "BP_DunePlayerCharacter_C"` for `TakaroTest`'s own deaths (the victim pointers in
/// `/debug/deathlog` differ on every death, and seq 5's victim is literally seq 3's killer pawn).
///
/// So when the pointer match fails this walks the actor's OWN links back to a tracked controller —
/// `APawn::Controller`, then `APawn::PlayerState` — both of which survive a respawn. On success it
/// ADOPTS the pawn into the registry entry, so the registry self-heals and the next lookup is a plain
/// pointer match.
///
/// `how` (optional) receives a static string naming which route answered: "pointer", "Controller",
/// "PlayerState", or "" on failure. Reads only, all guarded; safe on the game thread and bounded at
/// two property reads.
bool IdentifyActor(void* actor, Entry& out, const char** how = nullptr);

/// Off-thread copies.
std::vector<Entry> All(bool includeOffline = false);
bool Find(const std::string& ref, Entry& out);

/// Snapshot bookkeeping for `/players`: the generation counter and how stale the newest refresh is.
uint64_t Generation();
uint64_t SnapshotAgeMs();
/// Requests a refresh if the cached snapshot is older than `ttlMs`. Returns false when the game
/// thread pump was unavailable (the caller then answers 503 rather than a stale position).
bool EnsureFresh(uint32_t ttlMs);

size_t Count();
std::string Json();  // /health.diagnostics.players

}  // namespace Players
