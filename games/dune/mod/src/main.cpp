// Entry point: a library constructor kicks off one init thread and returns immediately, so the
// server's own startup is never blocked or delayed by us.
#include "query.h"
#include "common.h"
#include "events.h"
#include "gamethread.h"
#include "hooks.h"
#include "livehooks.h"
#include "http.h"
#include "objpool.h"
#include "presence.h"
#include "reflect.h"
#include "state.h"
#include "resolve.h"

#include <unistd.h>

#include <cstdio>

#include <atomic>

namespace {

std::atomic<bool> g_stop{false};

void Sleep(unsigned ms) {
    struct timespec ts{(time_t)(ms / 1000), (long)(ms % 1000) * 1000000L};
    nanosleep(&ts, nullptr);
}

void* InitThread(void*) {
    PluginState::Get().SetCapability("gameThread", "degraded", "starting");
    PluginLog("takaro dune plugin %s starting (pid %d, bootId %s)", TAKARO_PLUGIN_VERSION, getpid(),
              BootId().c_str());
    PluginLog("maps: %s", MemMapsSelfSoLine().c_str());

    try {
        Resolve::Init();
    } catch (...) {
        PluginLog("sym: init threw; continuing with whatever resolved");
    }
    try {
        Reflect::Init();
    } catch (...) {
        PluginLog("reflect: init threw");
    }
    try {
        GameThread::Init();
    } catch (...) {
        PluginLog("gamethread: init threw");
    }
    Events::Init();
    Query::Init();
    // Hooks against the live, relocated process. This also installs the game-thread pump, because on
    // this build ProcessEvent is the only proven game-thread entry point (see gamethread.h).
    // AFTER Events::Init(), which registers the same capability names from the *offline* symbol table
    // and would otherwise overwrite a live `ok` with its own `unimplemented`.
    try {
        LiveHooks::Install();
    } catch (...) {
        PluginLog("livehooks: install threw; hooks stay uninstalled and the server is untouched");
    }
    Http::Start();

    // Boot validations need a live UObject world; wait for the pump, then run them once.
    for (int i = 0; i < 600 && !g_stop; i++) {
        if (GameThread::TickCount() > 0) break;
        Sleep(500);
    }
    if (GameThread::TickCount() == 0) {
        PluginState::Get().SetCapability("reflection", "degraded", "no engine tick observed; boot validation skipped");
        PluginLog("gamethread: no tick after 5 minutes - reflection validations skipped");
    } else {
        std::string ignored;
        if (!GameThread::RunJson([] { return Reflect::Validate(); }, ignored, 20000))
            PluginState::Get().SetCapability("reflection", "degraded", "boot validation job timed out");
    }

    // ---- live reflection ------------------------------------------------------------------------
    // Locating FNamePool and walking GUObjectArray are pure reads of immutable-once-written engine
    // data, so they run here rather than on the game thread. That matters: on this build the engine
    // Tick slot may not be hookable at all, and the whole name/class layer would otherwise be held
    // hostage by the pump. Every step reports itself; nothing degrades the server.
    // Retried, because the object pool is allocated by the engine and not by us: on one of the three
    // M0 boots the init thread reached GUObjectArray before `AllocateObjectPool` had filled in the
    // chunk table. 20 attempts x 3 s covers a cold Hagga Basin boot with room to spare.
    bool poolOk = false;
    for (int i = 0; i < 20 && !g_stop && !poolOk; i++) {
        poolOk = Pool::Init();
        if (!poolOk) Sleep(3000);
    }
    try {
        if (poolOk) {
            PluginState::Get().SetCapability("symbols.names", "ok",
                                            "FNamePool located and cross-checked on the live process");
            if (Classes::Build()) {
                char d[256];
                snprintf(d, sizeof d, "%zu live UClass objects indexed by decoded name", Classes::Count());
                PluginState::Get().SetCapability("classIndex", "ok", d);
            } else {
                PluginState::Get().SetCapability("classIndex", "degraded", "no UClass object could be named");
            }
            std::string fieldDetail;
            if (Pool::DiscoverFieldLayout(fieldDetail))
                PluginState::Get().SetCapability("reflectFields", "ok", fieldDetail);
            else
                PluginState::Get().SetCapability("reflectFields", "degraded", fieldDetail);
            // The heavy cross-check (N objects' class chains against their vtable identity) runs once
            // here so /health can serve it without doing work per request.
            // `reflection` is set from the LIVE cross-check, not from the ported Validate() path:
            // Validate() funnels through engine callables that do not exist on this build, so its
            // verdict says nothing about whether reflection works here.
            // LANE L2: the ProcessEvent name filter, the parameter plans, the NPC class set and the
            // player registry's property offsets all need the class index AND the discovered FField
            // layout, so they are initialised HERE and not next to Events::Init(). Housekeep() retries
            // if this still came too early.
            if (Events::InitLive()) PluginLog("events: live init done");
            Pool::VerifyJson();
            const Pool::VerifyResult& vr = Pool::CachedVerify();
            char rd[224];
            snprintf(rd, sizeof rd,
                     "%u live objects: class name from the pool matched the class identity from the RTTI "
                     "%u times, %u mismatches (%u names decoded)",
                     vr.objectsTested, vr.matched, vr.mismatched, vr.namesDecoded);
            PluginState::Get().SetCapability(
                "reflection", (vr.matched >= 50 && vr.mismatched == 0) ? "ok" : "degraded", rd);
        } else {
            PluginState::Get().SetCapability("symbols.names", "degraded",
                                             "FNamePool not located: " + Pool::F().error);
        }
    } catch (...) {
        PluginLog("pool: discovery threw; names stay degraded");
        PluginState::Get().SetCapability("symbols.names", "degraded", "FNamePool discovery threw");
    }


    while (!g_stop) {
        Sleep(2000);
        try {
            Events::Housekeep();
            Query::Housekeep();
            LiveHooks::Resweep();
            // The presence reconciler. Independent of the PostLogin/Logout slots on purpose: lane L6b-0
            // proved that a single vtable slot is the whole roster's single point of failure.
            Presence::Tick();
        } catch (...) {
        }
    }
    return nullptr;
}

}  // namespace

// The preload MUST be inert in every process that is not the map server.
//
// Dune's own launch path is a chain of non-game processes: the container entrypoint is
// `/home/dune/run.sh` (bash), which does `su dune -c "./DuneSandboxServer.sh …"` (su + runuser +
// bash again), and that shell finally execs the UE binary. An LD_PRELOAD set on the container is
// inherited by every one of them, plus by `lsof`, `sshd` and anything else run.sh calls. If the
// plugin started its init thread, opened its HTTP port or touched a vtable in any of those, the
// symptom would be a shell that hangs or a port that is already bound when the real server starts.
//
// So the gate is an exact basename match on /proc/self/exe, not a substring of the whole path (the
// path contains "DuneSandbox" for the .sh wrapper too, and `bash` running
// `DuneSandboxServer.sh` has /proc/self/exe == /usr/bin/bash but argv[0] mentioning DuneSandbox).
// Everything else returns before a single thread is created.
static const char* kServerBasename = "DuneSandboxServer-Linux-Shipping";

__attribute__((constructor)) static void TakaroPluginInit() {
    const std::string& exe = ExePath();
    size_t slash = exe.rfind('/');
    std::string base = slash == std::string::npos ? exe : exe.substr(slash + 1);
    if (base != kServerBasename) return;
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_create(&t, &attr, InitThread, nullptr);
    pthread_attr_destroy(&attr);
}

__attribute__((destructor)) static void TakaroPluginShutdown() {
    g_stop = true;
    Hooks::RestoreAll();
}
