// Hooks installed against the LIVE, relocated process — lane L1b's M0 deliverable.
//
// Three things are installed here, and nothing else:
//
//  1. **The ProcessEvent filter**, slot 86, on the class vtables we actually care about. This is
//     also the **game-thread pump**: see the long note in gamethread.h for why an engine Tick hook is
//     not available on this build. The detour is a filter, not a handler — it times itself, drains
//     the budgeted job queue, and hands nothing to L2 yet.
//  2. **AGameModeBase::PostLogin (slot 313) and Logout (slot 315)** on the **live game-mode
//     object's own vtable pointer**, never on a vtable read out of the file image (on a PIE every
//     slot in the image is 0, and calling it jumps to address 0 on the game thread).
//  3. Nothing on a class whose live vtable fails its own checks. A failed validator degrades exactly
//     that capability and leaves the server running.
//
// With no Funcom token no client can join, so PostLogin/Logout will report `hooked:true, fired:0`.
// That is stated as such: `fired` is the only evidence a hook works, and it is not available yet.
#pragma once
#include "common.h"

namespace LiveHooks {

// Installs everything that the resolved symbols allow. Idempotent, never throws, never aborts the
// server. Call from the init thread AFTER Resolve::Init().
void Install();

// Re-sweeps for class vtables that appeared since the last pass (a streamed-in package can add a
// native class vtable that was not mapped at boot). Cheap; called from the housekeeping loop.
void Resweep();

/// GAME THREAD ONLY. Dispatches `func` on `obj` through the engine's OWN `UObject::ProcessEvent`.
///
/// This is what lets `/debug/kill-nearest` drive the game's real kill path on a binary with no
/// function symbols at all: the object's live vtable slot 86 either holds one of our detours — in
/// which case the matching original is called, so the game sees exactly the call it would have made
/// — or it holds the engine's own body, which is called directly. Refuses (false + `err`) when the
/// slot is not a known function entry, so a wrong slot can never be jumped to.
bool CallProcessEvent(void* obj, void* func, void* params, std::string& err);

std::string Json();  // /health.diagnostics.liveHooks

}  // namespace LiveHooks
