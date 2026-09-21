// Read-only query handlers.
//
// The Dune connector is split differently from VEIN's. There, the plugin owned everything; here the
// **sidecar** owns all state that Postgres and RabbitMQ already expose, and it owns every mutation:
// give / teleport / kick / message / ban / shutdown all go out over the game's own RabbitMQ GM
// command bus (`heartbeats` exchange, `ServerCommandsAuthToken`), which needs no binary tampering at
// all. Deliberately, therefore, **this plugin has no mutating endpoint**. Nothing here writes to a
// UObject, and there is no `/give`, `/teleport`, `/kick`, `/message`, `/ban` or `/shutdown` route.
//
// What the plugin exists for is the four things no out-of-process path gives (campaign plan,
// Decision 1):
//
//   (a) live pawn location per player, and a player list keyed so the sidecar can join it against
//       Postgres (character name + the PlayerState unique id at minimum; the FLS id / SteamID when
//       they are readable),
//   (b) entity-killed (NPC/creature death with killer and weapon where derivable) and player-death
//       attribution,
//   (c) precise connect/disconnect (PostLogin / Logout / OnNetCleanup), which the Postgres
//       online-state poll can only approximate,
//   (d) the `/entities` class list and the diagnostics.
//
// (b) and (c) are events and live in events.h. This file is (a) and (d).
//
// Contract:
//   * Init() runs on the plugin init thread and registers one capability per query with
//     PluginState::SetCapability, so /health tells the sidecar exactly which of them to trust.
//   * Every handler returns {status, body} and never throws. Anything that touches a UObject does it
//     inside GameThread::RunJson() and returns 503 when the pump is unavailable — the HTTP threads
//     never dereference a game pointer themselves.
//   * Snapshot() runs on the game thread, at most once per second, and caches the player table so
//     GET /players never waits on the pump at all.
#pragma once
#include "common.h"

namespace Query {

using Result = HandlerResult;

void Init();
void Housekeep();

// GET /players            -> {"players":[{gameId,name,characterName,playerStateId,position?,...}]}
// GET /players/{id}       -> one player, or 404
// GET /players/{id}/location -> {"x","y","z","pitch","yaw","partition","at","source"}
//
// `source` is always reported and is never omitted: "pawn" (the live pawn's actor location, what
// this plugin is for), "playerState" (a replicated fallback) or "unknown". The sidecar's fallback
// chain (last chat.intercept origin -> the `actors.transform` last-saved row, flagged stale) only
// kicks in when this returns 404/501, so a silently-stale answer here would be worse than no answer.
Result Players();
Result Player(const std::string& gameId);
Result PlayerLocation(const std::string& gameId);

// GET /entities -> the NPC / creature / vehicle classes seen alive, with a human-readable name where
// one is derivable. Catalogue rule (memory: catalogue-human-names): `name` must be a display name,
// never a dev/class name; when only the class name is available the entry carries
// `"nameIsClassName": true` so the sidecar can decide to drop it rather than ship `BP_Critter_C` to
// Takaro as if it were a creature's name.
Result Entities();

// POST /debug/kill-nearest {gameId?, radius?} — debug-gated (token + TAKARO_PLUGIN_DEBUG=1) by
// http.cpp. Exists so entity-killed can be proven without a human swinging a weapon. It is the one
// endpoint here that changes game state, which is precisely why it is debug-only and why it is
// forwarded to events.cpp (the damage pipeline and the kill hook are the same lane's).
Result KillNearest(const JsonValue& body);

// GET /debug/inventories, /debug/nearby etc. are NOT provided: inventory is Postgres' job in this
// connector. /debug/object and /debug/structs live in http.cpp on top of Reflect.

std::string DiagnosticsJson();

}  // namespace Query
