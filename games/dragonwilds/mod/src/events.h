// Event sources (lane L2). Everything here pushes into PluginState::EmitEvent(type, dataJson);
// GET /events serves the ring buffer and knows nothing about the sources.
//
// Contract for L2:
//   * Init() runs on the plugin init thread, after Sym/Reflect/GameThread are up. It may install
//     hooks (Hooks::HookProcessEvent / HookVTableSymbol) and must set one capability per event type
//     via PluginState::SetCapability(), degrading with a reason instead of failing.
//   * Housekeep() runs every 2 s on the housekeeping thread: re-hook late-loaded subclasses,
//     expire dedupe windows, poll the log tail. It must never touch UObjects directly - use
//     GameThread::Run().
//   * Emitted types and payloads are specified in docs/API.md (player-connected,
//     player-disconnected, chat-message, player-death, entity-killed, log).
#pragma once
#include "common.h"

namespace Events {
void Init();        // TODO(L2): install PostLogin / PreLogout / Logout / chat / death / kill hooks
void Housekeep();   // TODO(L2): log tail + re-hook sweep + dedupe expiry
std::string DiagnosticsJson();
// True when the PreLogin hook is installed, i.e. a ban refuses a rejoin on the running server.
bool BanEnforcementLive();
}  // namespace Events
