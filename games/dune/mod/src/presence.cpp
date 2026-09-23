#include "presence.h"

#include "events.h"
#include "objpool.h"
#include "players.h"
#include "presence_diff.h"
#include "reflect.h"
#include "resolve.h"
#include "state.h"
#include "uobj.h"

#include <cstdio>
#include <cstdlib>

namespace {

// The property names that mean "this controller has a client on the other end". Names only — the
// offsets come from each object's own class chain, so a build that renames one degrades to the other
// instead of reading a wrong address.
const char* kConnProps[] = {"NetConnection", "Player"};

Mutex g_lock;
std::vector<PresenceDiff::Tracked> g_tracked;

bool g_ready = false;
uint64_t g_lastSweepMs = 0;
uint64_t g_sweeps = 0, g_skippedNoClassIndex = 0;
uint64_t g_lastDurationUs = 0, g_maxDurationUs = 0;
uint32_t g_lastSwept = 0, g_lastControllers = 0, g_lastConnected = 0, g_lastCdosSkipped = 0;
uint64_t g_connectsAnnounced = 0, g_disconnectsAnnounced = 0, g_connectRetries = 0, g_debounced = 0;
uint32_t g_intervalMs = 0;  // the effective cadence, after any self-backoff
std::string g_note;

uint32_t ConfiguredIntervalMs() {
    static uint32_t cached = 0;
    static bool done = false;
    if (done) return cached;
    done = true;
    long v = strtol(ConfigValue("TAKARO_PRESENCE_SWEEP_MS", "presenceSweepMs", "2000").c_str(), nullptr, 10);
    if (v == 0) {
        cached = 0;  // explicitly disabled
        return cached;
    }
    if (v < 250) v = 250;
    if (v > 60000) v = 60000;
    cached = (uint32_t)v;
    return cached;
}

/// Housekeeping thread. Pure guarded reads: no call into the game, no game-thread entry.
/// Returns the controllers that currently hold a client connection.
std::vector<const void*> SweepConnectedControllers(uint32_t& sweptOut, uint32_t& controllersOut,
                                                   uint32_t& cdosOut) {
    std::vector<const void*> out;
    uint32_t swept = 0, controllers = 0, cdos = 0;
    const uint32_t objNameOff = Reflect::Lay().objName;
    const bool poolOk = Pool::F().ok;
    Obj::ForEach([&](void* o) {
        swept++;
        if (!o || !LooksLikeUObject(o)) return true;
        if (!U::IsA(o, "PlayerController")) return true;
        // A class default object is not a player. `Default__*` is readable because the name pool was
        // located; without it we would rather skip the whole sweep than guess.
        if (poolOk) {
            std::string on = Pool::NameAt(o, objNameOff);
            if (on.compare(0, 9, "Default__") == 0) {
                cdos++;
                return true;
            }
        }
        controllers++;
        void* cls = U::ClassOf(o);
        if (!cls) return true;
        for (const char* p : kConnProps) {
            int32_t off = U::PropOffset(cls, p);
            if (off < 0) continue;
            void* conn = U::ReadPtr(o, (uint32_t)off);
            if (conn && LooksLikeUObject(conn)) {
                out.push_back(o);
                break;
            }
        }
        return true;
    });
    sweptOut = swept;
    controllersOut = controllers;
    cdosOut = cdos;
    return out;
}

}  // namespace

void Presence::Tick() {
    uint32_t configured = ConfiguredIntervalMs();
    if (!configured) {
        Guard g(g_lock);
        g_note = "disabled by TAKARO_PRESENCE_SWEEP_MS=0";
        return;
    }
    {
        Guard g(g_lock);
        if (!g_intervalMs) g_intervalMs = configured;
        if (g_lastSweepMs && NowMs() - g_lastSweepMs < g_intervalMs) return;
    }
    // Without the class index there is no way to ask "is this a PlayerController", and guessing is
    // exactly what this module exists to avoid.
    if (!Classes::ByName("PlayerController")) {
        Guard g(g_lock);
        g_skippedNoClassIndex++;
        g_note = "waiting for the live class index (no UClass 'PlayerController' yet)";
        return;
    }

    uint64_t t0 = NowMs();
    uint32_t swept = 0, controllers = 0, cdos = 0;
    std::vector<const void*> connected = SweepConnectedControllers(swept, controllers, cdos);
    uint64_t durMs = NowMs() - t0;

    PresenceDiff::Options opt;
    PresenceDiff::Result diff;
    {
        Guard g(g_lock);
        diff = PresenceDiff::Apply(g_tracked, connected, opt,
                                   [](const void* c) { return Players::IsTrackedOnline((void*)c); });
    }

    // Announce OUTSIDE the lock: a connect enters the game thread to read the identity, and holding a
    // mutex across that is how a plugin deadlocks a game.
    size_t announced = 0, retried = 0;
    for (const void* c : diff.connects) {
        if (Events::OnPresenceConnect((void*)c, "sweep")) {
            Guard g(g_lock);
            PresenceDiff::MarkAnnounced(g_tracked, c);
            announced++;
        } else {
            // The game thread was unavailable or the identity was unreadable: leave it unannounced so
            // the next sweep tries again, rather than emitting a player with no ref.
            retried++;
        }
    }
    for (const void* c : diff.disconnects) Events::OnPresenceDisconnect((void*)c, "sweep");

    Guard g(g_lock);
    g_ready = true;
    g_sweeps++;
    g_lastSweepMs = NowMs();
    g_lastDurationUs = durMs * 1000;
    if (g_lastDurationUs > g_maxDurationUs) g_maxDurationUs = g_lastDurationUs;
    g_lastSwept = swept;
    g_lastControllers = controllers;
    g_lastConnected = (uint32_t)connected.size();
    g_lastCdosSkipped = cdos;
    g_connectsAnnounced += announced;
    g_connectRetries += retried;
    g_disconnectsAnnounced += diff.disconnects.size();
    g_debounced += diff.droppedUnannounced;
    // Self-backoff: a sweep that costs more than 50 ms doubles its own interval (capped at 30 s)
    // rather than paying that every cadence on a busy world. It recovers as soon as it is cheap again.
    if (durMs > 50 && g_intervalMs < 30000) {
        g_intervalMs = g_intervalMs * 2 > 30000 ? 30000 : g_intervalMs * 2;
    } else if (durMs <= 20 && g_intervalMs > configured) {
        g_intervalMs = configured;
    }
    char b[224];
    snprintf(b, sizeof b, "%u objects swept, %u PlayerControllers (%u CDOs skipped), %u connected, %llu ms",
             swept, controllers, cdos, g_lastConnected, (unsigned long long)durMs);
    g_note = b;
}

bool Presence::Ready() {
    Guard g(g_lock);
    return g_ready;
}

std::string Presence::Json() {
    Guard g(g_lock);
    std::string o = "{\"mechanism\":\"low-rate GUObjectArray sweep of live PlayerControllers holding "
                    "APlayerController::{NetConnection,Player}; the PostLogin/Logout detours are a "
                    "same-frame fast path, not a dependency\"";
    o += ",\"enabled\":" + std::string(ConfiguredIntervalMs() ? "true" : "false");
    o += ",\"intervalMs\":" + std::to_string(g_intervalMs);
    o += ",\"sweeps\":" + std::to_string(g_sweeps);
    o += ",\"skippedNoClassIndex\":" + std::to_string(g_skippedNoClassIndex);
    o += ",\"lastSweep\":{\"objects\":" + std::to_string(g_lastSwept) + ",\"playerControllers\":" +
         std::to_string(g_lastControllers) + ",\"cdosSkipped\":" + std::to_string(g_lastCdosSkipped) +
         ",\"connected\":" + std::to_string(g_lastConnected) + ",\"durationUs\":" +
         std::to_string(g_lastDurationUs) + "}";
    o += ",\"maxDurationUs\":" + std::to_string(g_maxDurationUs);
    o += ",\"tracked\":" + std::to_string(g_tracked.size());
    o += ",\"connectsAnnounced\":" + std::to_string(g_connectsAnnounced);
    o += ",\"disconnectsAnnounced\":" + std::to_string(g_disconnectsAnnounced);
    o += ",\"connectAnnounceRetries\":" + std::to_string(g_connectRetries);
    o += ",\"debouncedAbsences\":" + std::to_string(g_debounced);
    o += ",\"note\":" + JsonStr(g_note);
    return o + "}";
}
