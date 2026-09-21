// The live half of the presence reconciler: one low-rate sweep of `GUObjectArray` that answers
// "which PlayerControllers on this server hold a client net connection right now?".
//
// WHY (lane L6b-0, 2026-09-21)
//
// `player-connected` / `player-disconnected` used to depend entirely on two vtable slots on one live
// object (`AGameModeBase::PostLogin` = slot 313 and `Logout` = slot 315 on the live
// `ADuneSandboxGameModeBase`). Those hooks are installed and validated, but a hook that has never
// fired is not evidence, and a roster that depends on a single slot fails silently and completely:
// no exception, no degraded capability, just an empty server forever. This module is the independent
// mechanism that makes the hooks an optimisation instead of a dependency.
//
// WHAT "CONNECTED" MEANS HERE, AND WHY THESE TWO PROPERTIES
//
// Both are reflected UPROPERTYs on `APlayerController`, read live off this build (offsets from the
// class's own FField chain, never constants):
//
//   `Player`        ObjectProperty @0x3e8 — the `UPlayer` driving this controller. On a dedicated
//                                           server a remote client's `UPlayer` *is* its
//                                           `UNetConnection` (UNetConnection derives UPlayer).
//   `NetConnection` ObjectProperty @0x5d0 — the connection itself.
//
// A controller with neither is not a connected client: it is a leftover, a spectator shell being torn
// down, or an AI controller that happens to derive PlayerController. Either one present is accepted,
// because which of the two the engine populates first is a frame-ordering detail we do not control.
//
// COST, AND THE GAME-THREAD RULE (memory: avoid-game-thread)
//
// The sweep itself runs on the HOUSEKEEPING thread: it only reads, every dereference goes through
// `MemReadable`/`LooksLikeUObject`, and it calls nothing in the game — exactly like `/entities`.
// The only game-thread work is reading a NEW player's identity (`Players::NoteLogin`), which is
// queued through `GameThread::Run` and happens at most once per join. The sweep times itself and
// backs its own interval off when it gets expensive, so a world that grows to hundreds of thousands
// of objects cannot turn this into a per-2-second stall.
#pragma once
#include "common.h"

namespace Presence {

/// Called from the housekeeping loop every ~2 s. Rate-limits itself to the configured cadence and
/// returns immediately when the class index is not up yet. Never throws.
void Tick();

/// The reconciler's own diagnostics, for /health.diagnostics.presence.
std::string Json();

/// True once at least one sweep has completed. Used by the capability text so `/health` can say the
/// reconciler is armed without claiming a player was ever seen.
bool Ready();

}  // namespace Presence
