# Game-thread policy

> **The rule (Tester, 2026-09-17):** *avoid the game thread if we can.*

A dedicated server has exactly one game thread and it is the server's frame budget. Every microsecond
this plugin spends there is a microsecond the server does not spend simulating the world. So the game
thread is treated as a scarce, non-renewable resource: we use it only where UE gives us no choice, and
we measure what we use.

**On Dune this rule bites harder than on any previous connector**, for two reasons:

1. Funcom's own figure for the Hagga Basin (Survival_1) map process is a **12 GiB** memory limit, and
   the map simulates sandworms, sandstorms and a persistent world. It is not a small tick.
2. This plugin is **read-only** — every mutation goes over the RabbitMQ GM command bus in the sidecar,
   out of process. So item 3 below ("queued mutations") has **no work at all** in this connector. The
   only legitimate game-thread work here is event capture inside a hook, and one small player/pawn
   snapshot on demand. If you find yourself adding a third kind, stop: it probably belongs in the
   sidecar.

## What may run on the game thread

1. **Event capture inside a hook we installed.** A `ProcessEvent` detour or a `PostLogin`/`Logout`
   detour is already *on* the engine's own call path — there is no "off-thread" version of it. The
   detour body is therefore held to: a few pointer reads, raw `FName` comparisons, and handing the
   result on. No `FName::ToString` in the filter, no string compares per call, no JSON building, no
   HTTP, no allocation-heavy work.
2. **One small state copy, at a low rate, on demand.** Live player state (identity, ping, pawn
   class, position) can only be read from the game thread. It is copied into plain-data rows at most
   once per `TAKARO_SNAPSHOT_TTL_MS` (default 500 ms), **and only when a request actually needs it**.
   No request, no entry.
3. **Queued jobs, under a strict per-tick budget.** Anything that must call engine code is queued and
   drained from the Tick hook under `TAKARO_TICK_BUDGET_US` (default 500 µs). Leftovers wait for the
   next tick; one job always runs so a slow job cannot starve the queue. In *this* connector the only
   such jobs are the snapshot refresh, the boot validations and the debug-only `kill-nearest`.

## What must NOT run on the game thread

JSON building · HTTP serving · catalogue and entity lists (built once at boot or on demand, then
served from an immutable cache) · log tailing and log parsing · name humanising · dedupe and
aggregation · redaction · `/proc` reads · **the symbol/vtable resolution and the ProcessEvent slot
derivation** (they run once, on the plugin's own init thread, before the world exists) · anything that
can be answered from the last snapshot.

**If a read can be served from the last snapshot, serve it, and do not touch the game thread.**

## Concrete limits in this plugin

| Thing | Limit | Where |
|---|---|---|
| Job queue drain | `TAKARO_TICK_BUDGET_US` µs/tick (default 500), ≥1 job/tick, ≤16 jobs/tick | `gamethread.cpp` |
| Player/pawn snapshot | lazy, TTL `TAKARO_SNAPSHOT_TTL_MS` (default 500 ms) | `query.cpp` |
| Late-subclass re-hook sweep | every 2 s on the housekeeping thread; the game-thread part is resumable, ≤1 ms per entry | `events.cpp` |
| Entity catalogue | cached 5 min | `query.cpp` |
| `ProcessEvent` filter | `UFunction*` → decision cache; raw `FName` compare on a miss only. **Never** `FName::ToString` in the filter | `events.cpp` |
| Object validation before any read | `LooksLikeUObject`: readable, 8-aligned vptr, slot 0 in `.text`, vtable is one of the 42 942 exported. Three pointer reads and a binary search — no engine call | `resolve.cpp` |
| Memory-range checks | lock-free `/proc/self/maps` snapshot; no mutex, no re-read on the hot path | `resolve.cpp` |

## How it is measured

Every one of those is counted. `GET /debug/perf` (and `/health.diagnostics.perf`) reports per-tick
pump time (avg / p50 / p99 / max, over the last 1000 ticks), jobs per second, **game-thread entries
per second** — the number this rule is actually about — the `ProcessEvent` filter's calls/s and
average nanoseconds, and every named sweep's rate, average and maximum. `GET /debug/perf?reset=1`
starts a fresh window.

A change to anything in the table above is expected to come with a before/after from that endpoint,
measured on the live rig (lane L9's A/B). The baseline to beat is "boot time and tick rate unchanged
with the plugin loaded", which is a gate at M0.

### One Dune-specific measurement that is not optional

`ADuneCharacter`'s vtable has **802** slots and `ADunePlayerCharacter`'s has **1027**; the map runs
sandworms, critters and vehicles. A `TakeDamage` hook on a class this hot is a per-damage-tick detour,
not a per-death one. Before any damage-path hook is left enabled, `/debug/perf`'s filter counters must
show its cost on a populated server — the A/B is the justification for keeping it, and "it seemed
cheap" is not a measurement.
