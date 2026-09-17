#include "gamethread.h"

#include "hooks.h"
#include "state.h"
#include "sym.h"

#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <deque>
#include <memory>

namespace {

struct Job {
    std::function<void()> fn;
    bool done = false;
    bool started = false;
    bool cancelled = false;
};

Mutex g_q;
pthread_cond_t g_cv = PTHREAD_COND_INITIALIZER;
std::deque<std::shared_ptr<Job>> g_jobs;

std::atomic<uint64_t> g_ticks{0};
std::atomic<long> g_tickTid{0};
std::atomic<uint64_t> g_tickChanges{0};
std::atomic<uint64_t> g_lastTickMs{0};
std::atomic<uint64_t> g_firstTickMs{0};
std::atomic<uint64_t> g_jobsRun{0};
std::atomic<uint64_t> g_jobsTimedOut{0};
std::atomic<uint64_t> g_maxDrainMs{0};
bool g_installed = false;
std::string g_how = "not installed";

void OnTick() {
    uint64_t now = NowMs();
    long tid = (long)syscall(SYS_gettid);
    long prev = g_tickTid.exchange(tid);
    if (prev && prev != tid) g_tickChanges++;
    if (!g_ticks++) {
        g_firstTickMs = now;
        PluginLog("gamethread: first tick on thread %ld", tid);
        PluginState::Get().SetCapability("gameThread", "ok", "");
    }
    g_lastTickMs = now;

    for (size_t n = 0; n < GameThread::kJobsPerTick; n++) {
        std::shared_ptr<Job> job;
        {
            Guard g(g_q);
            if (g_jobs.empty()) break;
            job = g_jobs.front();
            g_jobs.pop_front();
            if (job->cancelled) continue;
            job->started = true;
        }
        uint64_t t0 = NowMs();
        try {
            job->fn();
        } catch (const std::exception& e) {
            PluginLog("gamethread: job threw: %s", e.what());
        } catch (...) {
            PluginLog("gamethread: job threw a non-standard exception");
        }
        uint64_t took = NowMs() - t0;
        if (took > g_maxDrainMs) g_maxDrainMs = took;
        g_jobsRun++;
        {
            Guard g(g_q);
            job->done = true;
            pthread_cond_broadcast(&g_cv);
        }
    }
}

using FnTick = void (*)(void* self, float dt, bool idle);
FnTick g_origJgx = nullptr;
FnTick g_origGame = nullptr;

void TickJgx(void* self, float dt, bool idle) {
    if (g_origJgx) g_origJgx(self, dt, idle);
    Hooks::MarkFired("UJgxGameEngine::Tick");
    try { OnTick(); } catch (...) {}
}
void TickGame(void* self, float dt, bool idle) {
    if (g_origGame) g_origGame(self, dt, idle);
    Hooks::MarkFired("UGameEngine::Tick");
    try { OnTick(); } catch (...) {}
}

// Swaps every exported vtable slot that holds `target`.
//
// Hooking only the declaring class is not enough: a derived class that inherits the implementation
// gets its own vtable with its own copy of the same function pointer. On this build the live engine
// is UDomGameEngine, whose slot 98 holds UJgxGameEngine::Tick - swapping only _ZTV14UJgxGameEngine
// never fires. So we sweep .dynsym for every `_ZTV*` that contains the address.
size_t SwapEverywhere(const char* symName, void* detour, size_t& slotOut, std::string& howOut) {
    uint64_t target = Sym::Addr(symName);
    if (!target) return 0;
    size_t hooked = 0;
    std::string tables;
    for (auto& vt : VTableSymbols()) {
        uint64_t addr = vt.second.first;
        uint64_t vsize = vt.second.second;
        auto* words = (const uint64_t*)(uintptr_t)addr;
        size_t count = (size_t)(vsize / 8);
        if (count < 3 || !MemReadable(words, (size_t)vsize)) continue;
        for (size_t i = 2; i < count; i++) {
            if (words[i] != target) continue;
            std::string err;
            if (Hooks::SwapVTableSlot(symName, (void*)(uintptr_t)addr, i - 2, detour, nullptr, err)) {
                hooked++;
                slotOut = i - 2;
                if (tables.size() < 200) tables += (tables.empty() ? "" : ",") + vt.first;
            } else {
                PluginLog("gamethread: %s slot %zu in %s not swapped: %s", symName, i - 2, vt.first.c_str(),
                          err.c_str());
            }
        }
    }
    howOut = tables;
    return hooked;
}

bool InstallOne(const char* symName, void* detour, FnTick* orig) {
    uint64_t addr = Sym::Addr(symName);
    if (!addr) {
        PluginLog("gamethread: %s unresolved", symName);
        return false;
    }
    // The original is the symbol itself: every swapped slot held exactly that address.
    *orig = (FnTick)(uintptr_t)addr;
    size_t slot = SIZE_MAX;
    std::string tables;
    size_t n = SwapEverywhere(symName, detour, slot, tables);
    if (!n) return false;
    PluginLog("gamethread: %s hooked in %zu vtables at slot %zu (%s)", symName, n, slot, tables.c_str());
    g_how += std::string(g_how.empty() ? "" : "; ") + symName + " x" + std::to_string(n) + " slot " +
             std::to_string(slot) + " [" + tables + "]";
    return true;
}

}  // namespace

void GameThread::Init() {
    if (g_installed) return;
    g_how.clear();
    bool a = InstallOne("UJgxGameEngine::Tick", (void*)&TickJgx, &g_origJgx);
    bool b = InstallOne("UGameEngine::Tick", (void*)&TickGame, &g_origGame);
    g_installed = a || b;
    if (g_how.empty()) g_how = "not installed";
    if (!g_installed) {
        PluginState::Get().SetCapability("gameThread", "degraded", "no engine Tick vtable slot could be hooked");
        PluginLog("gamethread: NOT installed - all UObject work will return 503");
    } else {
        PluginState::Get().SetCapability("gameThread", "degraded", "hook installed, waiting for the first tick");
        PluginLog("gamethread: installed (%s)", g_how.c_str());
    }
}

bool GameThread::Alive() {
    uint64_t last = g_lastTickMs.load();
    return last && NowMs() - last < 5000;
}

uint64_t GameThread::TickCount() { return g_ticks.load(); }

bool GameThread::Run(std::function<void()> fn, uint32_t timeoutMs) {
    if (!g_installed) return false;
    auto job = std::make_shared<Job>();
    job->fn = std::move(fn);
    {
        Guard g(g_q);
        if (g_jobs.size() > 256) return false;  // the pump is stuck; do not pile up
        g_jobs.push_back(job);
    }
    uint64_t deadline = NowMs() + timeoutMs;
    Guard g(g_q);
    while (!job->done) {
        uint64_t now = NowMs();
        if (now >= deadline) {
            if (!job->started) job->cancelled = true;
            g_jobsTimedOut++;
            return false;
        }
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        uint64_t wait = deadline - now;
        ts.tv_sec += (time_t)(wait / 1000);
        ts.tv_nsec += (long)((wait % 1000) * 1000000);
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        pthread_cond_timedwait(&g_cv, g_q.raw(), &ts);
    }
    return true;
}

bool GameThread::RunJson(std::function<std::string()> fn, std::string& out, uint32_t timeoutMs) {
    std::string result;
    bool ok = Run([&] {
        try {
            result = fn();
        } catch (...) {
            result = "{\"error\":\"handler threw on the game thread\"}";
        }
    }, timeoutMs);
    if (ok) out = result;
    return ok;
}

std::string GameThread::StatsJson() {
    uint64_t last = g_lastTickMs.load(), first = g_firstTickMs.load(), ticks = g_ticks.load();
    double hz = (ticks > 1 && last > first) ? (double)(ticks - 1) * 1000.0 / (double)(last - first) : 0.0;
    size_t queued;
    {
        Guard g(g_q);
        queued = g_jobs.size();
    }
    return "{\"installed\":" + std::string(g_installed ? "true" : "false") + ",\"how\":" + JsonStr(g_how) +
           ",\"alive\":" + (Alive() ? "true" : "false") + ",\"tickCount\":" + std::to_string(ticks) +
           ",\"tickThreadId\":" + std::to_string(g_tickTid.load()) +
           ",\"tickThreadChanges\":" + std::to_string(g_tickChanges.load()) +
           ",\"approxHz\":" + JsonNum(hz) +
           ",\"msSinceLastTick\":" + std::to_string(last ? NowMs() - last : 0) +
           ",\"jobsRun\":" + std::to_string(g_jobsRun.load()) +
           ",\"jobsTimedOut\":" + std::to_string(g_jobsTimedOut.load()) +
           ",\"jobsQueued\":" + std::to_string(queued) +
           ",\"maxJobMs\":" + std::to_string(g_maxDrainMs.load()) + "}";
}
