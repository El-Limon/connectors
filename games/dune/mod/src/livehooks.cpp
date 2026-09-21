#include "livehooks.h"

#include "events.h"
#include "gamethread.h"
#include "hooks.h"
#include "objpool.h"
#include "perf.h"
#include "reflect.h"
#include "resolve.h"
#include "state.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>

namespace {

using FnProcessEvent = void (*)(void* self, void* func, void* params);
using FnPostLogin = void (*)(void* self, void* newPlayer);
using FnLogout = void (*)(void* self, void* exiting);

// One detour per *original*, because different classes override ProcessEvent with different bodies
// and each swapped slot must be able to call the right original back. Four slots is enough for this
// build: UObject's, AActor's, UActorComponent's and one spare.
struct PeSlot {
    std::atomic<FnProcessEvent> orig{nullptr};
    std::atomic<uint64_t> calls{0};
};
constexpr size_t kPeSlots = 4;
PeSlot g_pe[kPeSlots];
std::atomic<uint64_t>* g_peFired = nullptr;

// The pump. Kept deliberately tiny: read the clock, drain the budgeted queue, bump a counter. Every
// byte of work here is paid on the game thread by every ProcessEvent call in the process, which is
// why the budget (TAKARO_TICK_BUDGET_US) and the per-call timing in /health.diagnostics.perf exist.
inline void Filter(size_t idx, void* self, void* func, void* params) {
    // LANE L2: one 4-byte read of the UFunction's FName index plus a compare against a table of at
    // most 16 entries. Events::OnProcessEvent times the DECISION itself (Perf::RecordFilter) and,
    // separately, any work a hit causes (Perf::RecordHandler) — so "ns per ProcessEvent call" in
    // /health.diagnostics.perf stays the filter's own cost and is not inflated by a rare kill.
    try {
        Events::OnProcessEvent(self, func, params);
    } catch (...) {
    }
    if (g_pe[idx].calls.fetch_add(1, std::memory_order_relaxed) == 0) {
        // The ONLY real proof that slot 86 is ProcessEvent: a detour placed there actually ran.
        // Until this happens /health reports processEventSlotConfirmed:false.
        Resolve::NoteProcessEventFired();
    }
    if (g_peFired) g_peFired->fetch_add(1, std::memory_order_relaxed);
    // Pump at most once per N calls: ProcessEvent can fire thousands of times per frame and the
    // queue drain must not be paid that often. 64 is a compromise measured in the A/B below.
    static std::atomic<uint64_t> tickIn{0};
    if ((tickIn.fetch_add(1, std::memory_order_relaxed) & 0x3f) == 0) {
        try {
            GameThread::PumpOnce();
        } catch (...) {
        }
    }
}

template <size_t I>
void PeDetour(void* self, void* func, void* params) {
    FnProcessEvent o = g_pe[I].orig.load(std::memory_order_relaxed);
    if (o) o(self, func, params);
    // Our work happens AFTER the original, so a throw or a long frame inside the engine is never
    // attributed to us and our failure can never stop the engine's own dispatch. It also means the
    // death has already happened when we look: `m_LifeState` / `m_bIsDead` read post-fact, which is
    // what we want.
    try {
        Filter(I, self, func, params);
    } catch (...) {
    }
}
void* const kPeDetours[kPeSlots] = {(void*)&PeDetour<0>, (void*)&PeDetour<1>, (void*)&PeDetour<2>,
                                    (void*)&PeDetour<3>};

// ---- PostLogin / Logout ------------------------------------------------------------------------
std::atomic<FnPostLogin> g_origPostLogin{nullptr};
std::atomic<FnLogout> g_origLogout{nullptr};
std::atomic<uint64_t>* g_postLoginFired = nullptr;
std::atomic<uint64_t>* g_logoutFired = nullptr;
std::atomic<uint64_t> g_postLoginCalls{0}, g_logoutCalls{0};

void PostLoginDetour(void* self, void* newPlayer) {
    FnPostLogin o = g_origPostLogin.load(std::memory_order_relaxed);
    if (o) o(self, newPlayer);
    g_postLoginCalls.fetch_add(1, std::memory_order_relaxed);
    if (g_postLoginFired) g_postLoginFired->fetch_add(1, std::memory_order_relaxed);
    PluginLog("livehooks: PostLogin fired (controller %p)", newPlayer);
    // The original ran first, so the controller is fully initialised when we read its identity.
    try {
        Events::OnPostLogin(newPlayer);
    } catch (...) {
    }
}

void LogoutDetour(void* self, void* exiting) {
    g_logoutCalls.fetch_add(1, std::memory_order_relaxed);
    if (g_logoutFired) g_logoutFired->fetch_add(1, std::memory_order_relaxed);
    PluginLog("livehooks: Logout fired (controller %p)", exiting);
    // Read our state BEFORE the engine tears the player down — after Logout the controller's
    // PlayerState and pawn are gone, so the identity would be unreadable.
    try {
        Events::OnLogout(exiting);
    } catch (...) {
    }
    FnLogout o = g_origLogout.load(std::memory_order_relaxed);
    if (o) o(self, exiting);  // read our state BEFORE the engine tears the player down
}

// ---- bookkeeping -------------------------------------------------------------------------------
struct Installed {
    std::string what;
    std::string where;
    bool ok = false;
    std::string detail;
};
Mutex g_lock;
std::vector<Installed> g_log;
std::set<std::string> g_hookedTables;
bool g_done = false;
void* g_gameMode = nullptr;
std::string g_gameModeClass;

void Note(const std::string& what, const std::string& where, bool ok, const std::string& detail) {
    Guard g(g_lock);
    g_log.push_back({what, where, ok, detail});
}

// The classes whose ProcessEvent we want. Bases only, on purpose: a Blueprint subclass does NOT get
// a new C++ vtable (it gets a new UClass), so hooking the native base covers every BP derivative of
// it. What a BP *can* add is a brand-new native vtable when a plugin package streams in, which is
// what Resweep() is for.
const char* kPeClasses[] = {
    "_ZTV7UObject",             // everything that does not override it
    "_ZTV6AActor",              // this build DOES override ProcessEvent on AActor
    "_ZTV14ADuneCharacter",     //
    "_ZTV20ADunePlayerCharacter",
    "_ZTV16ADuneCritterBase",
    "_ZTV17ADuneNpcCharacter",
    "_ZTV13ASandwormPawn",
    "_ZTV24ADuneSandboxGameModeBase",
    "_ZTV6UWorld",
};

// Assigns a detour index per distinct original address, so each swapped slot calls the right body.
int DetourIndexFor(uint64_t orig) {
    static std::map<uint64_t, int> byOrig;
    auto it = byOrig.find(orig);
    if (it != byOrig.end()) return it->second;
    int idx = (int)byOrig.size();
    if (idx >= (int)kPeSlots) return -1;
    byOrig[orig] = idx;
    g_pe[idx].orig.store((FnProcessEvent)(uintptr_t)orig, std::memory_order_relaxed);
    return idx;
}

bool HookPeOn(const char* ztv, std::string& detail) {
    size_t slot = Resolve::ProcessEventSlot();
    if (slot == SIZE_MAX) { detail = "ProcessEvent slot not derived"; return false; }
    VTableInfo vt = VTable(ztv);
    if (!vt.ok) { detail = vt.error; return false; }
    if (slot >= vt.slots) {
        char b[128];
        snprintf(b, sizeof b, "slot %zu is beyond this vtable's %zu slots", slot, vt.slots);
        detail = b;
        return false;
    }
    // LIVE memory only. VTableSlotsLive() rejects an all-zero table, which is exactly what the file
    // image of this PIE looks like.
    std::vector<uint64_t> slots;
    std::string err;
    if (!VTableSlotsLive(vt, slot + 1, slots, err)) { detail = "vtable not live: " + err; return false; }
    uint64_t orig = slots[slot];
    if (!IsTextAddr(orig) || (EhFrameAvailable() && !IsFunctionEntry(orig))) {
        char b[160];
        snprintf(b, sizeof b, "slot %zu holds 0x%lx, which is not a known function entry", slot, orig);
        detail = b;
        return false;
    }
    int idx = DetourIndexFor(orig);
    if (idx < 0) { detail = "out of detour slots (more than 4 distinct ProcessEvent bodies)"; return false; }
    if (!Hooks::SwapVTableSlot(std::string("ProcessEvent@") + vt.className, (void*)(uintptr_t)vt.addr, slot,
                               kPeDetours[idx], nullptr, err)) {
        detail = err;
        return false;
    }
    char b[192];
    snprintf(b, sizeof b, "slot %zu, original 0x%lx, detour #%d", slot, orig, idx);
    detail = b;
    return true;
}

// Finds the live game-mode object.
//
// MEASURED CORRECTION: the first version took the first object whose vtable class name contained
// "GameMode" and got `AGameModeBase` — i.e. the **class default object**, not the running game mode.
// Hooking a CDO's vtable is not wrong (the slot lives in the shared class vtable) but it is the wrong
// class: the instantiated mode is a Dune subclass with its own vtable and its own copy of slot 313.
// So CDOs are skipped by name (`Default__*`, which the located name pool lets us read) and the most
// derived Dune game mode wins.
void* FindLiveGameMode(std::string& clsOut) {
    void* best = nullptr;
    int bestScore = -1;
    Obj::ForEach([&](void* o) {
        std::string cn = ClassNameByVTablePtr(o);
        if (cn.find("GameMode") == std::string::npos) return true;
        if (cn == "UClass") return true;
        // Skip class default objects; they are not the running mode.
        if (Pool::F().ok) {
            std::string on = Pool::NameAt(o, Reflect::Lay().objName);
            if (on.compare(0, 9, "Default__") == 0) return true;
        }
        // Prefer a Dune subclass over the engine base, and a longer name (more derived) over a
        // shorter one.
        int score = (cn.find("Dune") != std::string::npos ? 1000 : 0) + (int)cn.size();
        if (score > bestScore) { bestScore = score; best = o; clsOut = cn; }
        return true;
    });
    return best;
}

}  // namespace

namespace {
bool g_lifecycleDone = false;

// PostLogin/Logout live on the game-mode object, which does not exist yet when the library
// constructor's init thread runs: the first live attempt reported "the live game-mode object was not
// found" because Install() ran ~2 s into a boot whose world takes ~60 s to come up. So this half is
// retried from the housekeeping loop until the object appears.
bool InstallLifecycle();
}  // namespace

void LiveHooks::Install() {
    if (g_done) return;
    g_done = true;
    auto& st = PluginState::Get();

    // ---- 1. the ProcessEvent filter, which is also the pump ------------------------------------
    size_t peHooked = 0;
    g_peFired = Hooks::FiredCounter("UObject::ProcessEvent");
    for (const char* ztv : kPeClasses) {
        std::string detail;
        if (HookPeOn(ztv, detail)) {
            peHooked++;
            g_hookedTables.insert(ztv);
            Note("processEventFilter", ztv, true, detail);
        } else {
            Note("processEventFilter", ztv, false, detail);
        }
    }
    if (peHooked) {
        char how[160];
        snprintf(how, sizeof how, "UObject::ProcessEvent slot %zu on %zu live class vtables",
                 Resolve::ProcessEventSlot(), peHooked);
        GameThread::MarkPumpAvailable(how);
        st.SetCapability("processEventFilter", "ok", how);
    } else {
        st.SetCapability("processEventFilter", "degraded",
                         "no class vtable accepted the ProcessEvent slot swap; see "
                         "/health.diagnostics.liveHooks");
    }

    // ---- 2. PostLogin / Logout on the LIVE game-mode object -------------------------------------
    InstallLifecycle();
}

namespace {

bool InstallLifecycle() {
    if (g_lifecycleDone) return true;
    auto& st = PluginState::Get();
    g_gameMode = FindLiveGameMode(g_gameModeClass);
    if (!g_gameMode) {
        st.SetCapability("player-connected", "degraded",
                         "the live game-mode object does not exist yet; retrying every 2 s");
        st.SetCapability("player-disconnected", "degraded",
                         "the live game-mode object does not exist yet; retrying every 2 s");
        return false;
    }
    g_lifecycleDone = true;
    struct LifecycleHook {
        const char* wanted;
        const char* capability;
        void* detour;
    };
    const LifecycleHook kLife[] = {
        {"AGameModeBase::PostLogin", "player-connected", (void*)&PostLoginDetour},
        {"AGameModeBase::Logout", "player-disconnected", (void*)&LogoutDetour},
    };
    for (const LifecycleHook& h : kLife) {
        const SymEntry* e = Resolve::Entry(h.wanted);
        if (!e || !e->addr || e->slot == SIZE_MAX) {
            Note(h.wanted, g_gameModeClass, false, "no slot index resolved");
            st.SetCapability(h.capability, "degraded", std::string("`") + h.wanted + "` has no resolved slot");
            continue;
        }
        // The slot is read off the live object's OWN vptr, which is the whole point: which Dune
        // game-mode subclass is instantiated is a runtime fact, and its vtable carries its own copy
        // of the inherited slot.
        void* vt = nullptr;
        if (!MemReadable(g_gameMode, 8)) {
            Note(h.wanted, g_gameModeClass, false, "game-mode object became unreadable");
            continue;
        }
        memcpy(&vt, g_gameMode, 8);
        uint64_t live = 0;
        if (!MemReadable((const char*)vt + 8 * e->slot, 8)) {
            Note(h.wanted, g_gameModeClass, false, "slot is outside the live vtable's readable range");
            st.SetCapability(h.capability, "degraded", "slot outside the live game-mode vtable");
            continue;
        }
        memcpy(&live, (const char*)vt + 8 * e->slot, 8);
        // Self-check before touching anything: the live slot must hold the SAME address the offline
        // dissection attributed to this slot, or at least a real function entry. A mismatch means the
        // slot index is wrong for this subclass and the hook is refused.
        bool sameAsResolved = (live == e->addr);
        if (!IsTextAddr(live) || (EhFrameAvailable() && !IsFunctionEntry(live))) {
            char b[160];
            snprintf(b, sizeof b, "live slot %zu holds 0x%lx, not a function entry - refusing", e->slot, live);
            Note(h.wanted, g_gameModeClass, false, b);
            st.SetCapability(h.capability, "degraded", b);
            continue;
        }
        std::string err;
        if (!Hooks::HookObjectVTable(h.wanted, g_gameMode, e->slot, h.detour, nullptr, err)) {
            Note(h.wanted, g_gameModeClass, false, err);
            st.SetCapability(h.capability, "degraded", err);
            continue;
        }
        if (strcmp(h.wanted, "AGameModeBase::PostLogin") == 0) {
            g_origPostLogin.store((FnPostLogin)(uintptr_t)live, std::memory_order_relaxed);
            g_postLoginFired = Hooks::FiredCounter(h.wanted);
        } else {
            g_origLogout.store((FnLogout)(uintptr_t)live, std::memory_order_relaxed);
            g_logoutFired = Hooks::FiredCounter(h.wanted);
        }
        char b[256];
        snprintf(b, sizeof b, "slot %zu on the live %s object %p, original 0x%lx (%s the offline address)",
                 e->slot, g_gameModeClass.c_str(), g_gameMode, live,
                 sameAsResolved ? "==" : "!=");
        Note(h.wanted, g_gameModeClass, true, b);
        // NOT `ok`: the detour has never fired, and `fired` is the only evidence that would justify
        // `ok`. It is no longer the only mechanism, though — lane L6b-0 added the sweep-based
        // reconciler in presence.cpp precisely so this slot is a fast path and not a dependency, and
        // the capability text says which of the two is armed.
        st.SetCapability(h.capability, "degraded",
                         std::string("hook installed (") + b +
                             ") but never fired; the sweep-based presence reconciler "
                             "(/health.diagnostics.presence) is the independent mechanism and does not "
                             "depend on this slot");
    }
    return true;
}

}  // namespace

namespace {

std::atomic<uint64_t> g_gmRevalidations{0}, g_gmReplacements{0}, g_gmSlotRepairs{0};
uint64_t g_lastGmScanMs = 0;

// Is our PostLogin detour still sitting in the live game-mode object's own vtable?
bool OurDetourStillInstalled() {
    if (!g_gameMode || !MemReadable(g_gameMode, 8)) return false;
    const SymEntry* e = Resolve::Entry("AGameModeBase::PostLogin");
    if (!e || e->slot == SIZE_MAX) return false;
    void* vt = nullptr;
    memcpy(&vt, g_gameMode, 8);
    if (!MemReadable((const char*)vt + 8 * e->slot, 8)) return false;
    uint64_t live = 0;
    memcpy(&live, (const char*)vt + 8 * e->slot, 8);
    return live == (uint64_t)(uintptr_t)&PostLoginDetour;
}

// Re-validates the hooked object against the game mode that is actually live now.
//
// WHY: `InstallLifecycle()` used to latch `g_lifecycleDone` forever. If the world is reloaded or the
// map process ever instantiates a second game mode, the hook would still be on the OLD object and
// `fired` would stay 0 with every self-check still reporting green — which is exactly the failure
// mode lane L6b-0 was sent to explain. The cheap check (is our detour still in the slot?) runs every
// housekeeping pass; the expensive one (sweep for the live game mode) is rate-limited to 30 s.
void RevalidateGameMode() {
    if (!g_lifecycleDone) return;
    g_gmRevalidations.fetch_add(1, std::memory_order_relaxed);
    bool installed = OurDetourStillInstalled();
    bool timeForScan = !g_lastGmScanMs || NowMs() - g_lastGmScanMs > 30000;
    if (installed && !timeForScan) return;
    if (!installed) {
        // Something replaced the slot (or the object went away). Re-install from scratch.
        g_gmSlotRepairs.fetch_add(1, std::memory_order_relaxed);
        g_lifecycleDone = false;
        g_gameMode = nullptr;
        InstallLifecycle();
        return;
    }
    g_lastGmScanMs = NowMs();
    std::string cls;
    void* live = FindLiveGameMode(cls);
    if (live && live != g_gameMode) {
        PluginLog("livehooks: the live game mode changed (%p %s -> %p %s); re-installing the lifecycle hooks",
                  g_gameMode, g_gameModeClass.c_str(), live, cls.c_str());
        g_gmReplacements.fetch_add(1, std::memory_order_relaxed);
        g_lifecycleDone = false;
        g_gameMode = nullptr;
        InstallLifecycle();
    }
}

}  // namespace

void LiveHooks::Resweep() {
    // A native class vtable that appears after boot (a streamed-in plugin package) is the one way a
    // ProcessEvent hook can silently stop covering something. Re-hooking the declared list is cheap
    // and idempotent: SwapVTableSlot on an already-swapped slot sees our own detour there and is a
    // no-op, so this cannot double-wrap.
    if (!g_done) return;
    InstallLifecycle();
    RevalidateGameMode();
    for (const char* ztv : kPeClasses) {
        if (g_hookedTables.count(ztv)) continue;
        std::string detail;
        if (HookPeOn(ztv, detail)) {
            Guard g(g_lock);
            g_hookedTables.insert(ztv);
        }
    }
}

bool LiveHooks::CallProcessEvent(void* obj, void* func, void* params, std::string& err) {
    size_t slot = Resolve::ProcessEventSlot();
    if (slot == SIZE_MAX) { err = "the ProcessEvent slot was never derived"; return false; }
    if (!obj || !func || !MemReadable(obj, 8)) { err = "object or function pointer is not readable"; return false; }
    void* vt = nullptr;
    memcpy(&vt, obj, 8);
    if (!MemReadable((const char*)vt + 8 * slot, 8)) { err = "slot is outside the object's live vtable"; return false; }
    uint64_t target = 0;
    memcpy(&target, (const char*)vt + 8 * slot, 8);
    // If our own detour sits there, call the ORIGINAL it wraps: re-entering the detour would count a
    // synthetic call as a real one and could recurse.
    for (size_t i = 0; i < kPeSlots; i++) {
        if (target == (uint64_t)(uintptr_t)kPeDetours[i]) {
            FnProcessEvent o = g_pe[i].orig.load(std::memory_order_relaxed);
            if (!o) { err = "our detour is installed but its original was not recorded"; return false; }
            o(obj, func, params);
            return true;
        }
    }
    if (!IsTextAddr(target) || (EhFrameAvailable() && !IsFunctionEntry(target))) {
        char b[160];
        snprintf(b, sizeof b, "live slot %zu holds 0x%lx, which is not a known function entry - refusing to call it",
                 slot, target);
        err = b;
        return false;
    }
    ((FnProcessEvent)(uintptr_t)target)(obj, func, params);
    return true;
}

std::string LiveHooks::Json() {
    std::string o = "{\"gameMode\":{\"class\":" + JsonStr(g_gameModeClass) + ",\"found\":" +
                    (g_gameMode ? "true" : "false") + "}";
    o += ",\"processEvent\":{\"slot\":" +
         (Resolve::ProcessEventSlot() == SIZE_MAX ? std::string("null")
                                                  : std::to_string(Resolve::ProcessEventSlot())) +
         ",\"distinctOriginals\":[";
    uint64_t total = 0;
    for (size_t i = 0; i < kPeSlots; i++) {
        uint64_t c = g_pe[i].calls.load();
        if (!g_pe[i].orig.load()) continue;
        if (total) o += ",";
        char b[96];
        snprintf(b, sizeof b, "{\"detour\":%zu,\"calls\":%llu}", i, (unsigned long long)c);
        o += b;
        total += c ? c : 1;
    }
    o += "]}";
    o += ",\"lifecycle\":{\"postLoginFired\":" + std::to_string(g_postLoginCalls.load()) +
         ",\"logoutFired\":" + std::to_string(g_logoutCalls.load()) +
         ",\"gameModeRevalidations\":" + std::to_string(g_gmRevalidations.load()) +
         ",\"gameModeReplacements\":" + std::to_string(g_gmReplacements.load()) +
         ",\"slotRepairs\":" + std::to_string(g_gmSlotRepairs.load()) +
         ",\"detourStillInstalled\":" + (OurDetourStillInstalled() ? "true" : "false") + "}";
    o += ",\"installs\":[";
    {
        Guard g(g_lock);
        for (size_t i = 0; i < g_log.size(); i++) {
            if (i) o += ",";
            o += "{\"what\":" + JsonStr(g_log[i].what) + ",\"where\":" + JsonStr(g_log[i].where) +
                 ",\"ok\":" + (g_log[i].ok ? "true" : "false") + ",\"detail\":" + JsonStr(g_log[i].detail) + "}";
        }
    }
    return o + "]}";
}
