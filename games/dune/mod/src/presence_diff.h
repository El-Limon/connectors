// The pure half of the presence reconciler: "given the set of controllers that look connected right
// now, which connects and disconnects should be announced?"
//
// WHY THIS IS A SEPARATE, GAME-FREE FILE
//
// Lane L6b-0 established (evidence:
// `.runtime/dune-evidence/2026-09-21-first-join-diag/`) that presence must NOT hang off a single
// vtable slot. `AGameModeBase::PostLogin` (slot 313 on the live `ADuneSandboxGameModeBase`) had
// `fired: 0` while a real client was in our world — and the reason turned out to be that the client
// was never on our map server at all (the Dune prologue map `NPE2_Main` runs `NetMode: Standalone`
// on the client). The hook may well be correct. But "the only evidence we have is one slot" is a
// single point of failure for the whole `player-connected` / `player-disconnected` pair, and a
// connector whose roster silently stays empty is worse than one that is a second late.
//
// So the plugin now has two independent mechanisms:
//
//   * the PostLogin/Logout detours — a precise, same-frame *hint* (fast path), and
//   * a low-rate sweep of live `PlayerController`s that hold a net connection — the *reconciler*,
//     which is what actually defines the connected set.
//
// The diff between sweeps is pure bookkeeping, so it lives here where a host unit test can drive it
// without a game process. The live sweep (which objects, which properties) is `presence.cpp`.
//
// DEBOUNCE. A controller that vanishes from one sweep is not immediately a disconnect: a sweep can
// miss an object that is momentarily unreadable, and a controller is also briefly connection-less
// during a map transfer. `missesBeforeDisconnect` consecutive absences are required, which at the
// default 2-second cadence makes a disconnect at most ~4 s late and an unreadable frame harmless.
#pragma once
#include "common.h"

#include <functional>

namespace PresenceDiff {

struct Tracked {
    const void* controller = nullptr;
    uint32_t misses = 0;    // consecutive sweeps in which this controller was not connected
    bool announced = false; // a `player-connected` has been emitted for it (by either mechanism)
};

struct Options {
    // Consecutive absences required before a disconnect is announced. 1 would make a single
    // unreadable sweep look like a logout.
    uint32_t missesBeforeDisconnect = 2;
    // Hard cap on the tracked set, mirroring the player registry's own cap.
    size_t maxTracked = 128;
};

struct Result {
    // Controllers that are connected and have not been announced yet. The caller announces them and
    // then calls MarkAnnounced for each one it actually managed to announce — an announce that could
    // not read an identity is deliberately left for the next sweep rather than emitted empty.
    std::vector<const void*> connects;
    // Controllers whose absence is now confirmed AND that were announced by us. Already removed from
    // `tracked` by Apply.
    std::vector<const void*> disconnects;
    // Confirmed-absent controllers that were never announced (nothing to say about them) — counted
    // only so the diagnostics can show the debounce working.
    uint32_t droppedUnannounced = 0;
};

/// Folds `connectedNow` into `tracked` and returns the diff. `preAnnounced(controller)` answers
/// "does something else already consider this controller online?" — it is how the sweep and the
/// PostLogin/Logout detours stay idempotent instead of double-emitting: a controller the player
/// registry already holds online is adopted as already-announced, and a controller the registry has
/// already marked offline (i.e. the Logout detour got there first) is dropped without a second
/// `player-disconnected`.
Result Apply(std::vector<Tracked>& tracked, const std::vector<const void*>& connectedNow, const Options& opt,
             const std::function<bool(const void*)>& preAnnounced);

/// Records that `controller` was successfully announced. Safe to call for an unknown pointer.
void MarkAnnounced(std::vector<Tracked>& tracked, const void* controller);

}  // namespace PresenceDiff
