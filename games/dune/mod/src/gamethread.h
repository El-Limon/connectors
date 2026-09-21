// Game-thread pump.
//
// The engine object's Tick is hooked through its class vtable (exported as _ZTV14UJgxGameEngine /
// _ZTV11UGameEngine). The detour calls the original first, then drains at most kJobsPerTick queued
// jobs. Every piece of UObject work in this plugin runs from there; HTTP threads only enqueue.
#pragma once
#include "common.h"

#include <functional>

namespace GameThread {

static const size_t kJobsPerTick = 16;

// Installs the Tick hook. Safe to call before the engine exists (the vtable is static data).
void Init();

// ---- the foreign-detour pump -------------------------------------------------------------------
// On DuneSandboxServer-Linux-Shipping there is NO way to reach `UEngine::Tick`: the binary is
// stripped, the Tick slot index could not be derived (unlike ProcessEvent, Tick's body has no
// self-identifying fingerprint), and probing candidate slots with a void-returning detour would
// corrupt the return value of whatever the slot really holds. So the pump is driven from the ONE
// game-thread entry point this build does prove: `UObject::ProcessEvent`, slot 86.
//
// `MarkPumpAvailable` is called once at install time so queued jobs are accepted; `PumpOnce` is
// called from the detour and must only ever run on the game thread.
void MarkPumpAvailable(const char* how);
void PumpOnce();

// Runs `fn` on the game thread and waits. Returns false on timeout or when the pump is unavailable;
// on timeout the job is cancelled if it has not started, otherwise it is left to finish.
bool Run(std::function<void()> fn, uint32_t timeoutMs = 5000);
// Convenience for handlers that produce a JSON string.
bool RunJson(std::function<std::string()> fn, std::string& out, uint32_t timeoutMs = 5000);

bool Alive();          // a tick was seen in the last 5 s
uint64_t TickCount();
std::string StatsJson();

}  // namespace GameThread
