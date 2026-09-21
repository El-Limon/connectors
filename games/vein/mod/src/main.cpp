// Entry point: a library constructor kicks off one init thread and returns immediately, so the
// server's own startup is never blocked or delayed by us.
#include "actions.h"
#include "admin.h"
#include "common.h"
#include "events.h"
#include "gamethread.h"
#include "hooks.h"
#include "http.h"
#include "reflect.h"
#include "state.h"
#include "resolve.h"

#include <unistd.h>

#include <atomic>

namespace {

std::atomic<bool> g_stop{false};

void Sleep(unsigned ms) {
    struct timespec ts{(time_t)(ms / 1000), (long)(ms % 1000) * 1000000L};
    nanosleep(&ts, nullptr);
}

void* InitThread(void*) {
    PluginState::Get().SetCapability("gameThread", "degraded", "starting");
    PluginLog("takaro vein plugin %s starting (pid %d, bootId %s)", TAKARO_PLUGIN_VERSION, getpid(),
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
    Actions::Init();
    Admin::Init();  // lane L3b: TAKARO_ADMIN_STEAMIDS
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

    while (!g_stop) {
        Sleep(2000);
        try {
            Events::Housekeep();
            Actions::Housekeep();
            Admin::Housekeep();
        } catch (...) {
        }
    }
    return nullptr;
}

}  // namespace

__attribute__((constructor)) static void TakaroPluginInit() {
    // Only attach to the dedicated server binary; steamcmd and helper processes must be untouched.
    const std::string& exe = ExePath();
    if (exe.find("VeinServer") == std::string::npos) return;
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
