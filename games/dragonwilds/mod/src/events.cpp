// Event sources (lane L2). Our own code.
//
// Six event types feed the ring buffer in `state`:
//   player-connected     ADominionGameMode::PostLogin vtable-slot hook + a pending-join resolver
//   player-disconnected  ADominionGameMode::PreLogout + AGameModeBase::Logout, deduped per connection
//   chat-message         ProcessEvent slot hook on the live UPlayerChatComponent vtables
//   player-death         ProcessEvent (Client_SendDeathEventTelemetry) + a health-edge fallback
//   entity-killed        ProcessEvent (UProgressComponent::OnAIKilled)
//   log                  rotation-aware tail of Saved/Logs/RSDragonwilds.log, redacted
//
// Rules obeyed here:
//   * every UObject touch happens on the game thread (inside a hook, which already runs there, or
//     inside GameThread::Run from the housekeeping thread);
//   * no FName is stringified before the boot validations confirmed the layout, and never from an
//     unvalidated pointer - raw FName values are compared instead;
//   * every UPROPERTY offset comes from UStruct::FindPropertyByName at runtime;
//   * every dereference of a game pointer is behind MemReadable();
//   * a failure degrades the capability and is reported in /health, it never throws out of a hook.

#include "events.h"

#include "actions.h"

#include "gamethread.h"
#include "hooks.h"
#include "reflect.h"
#include "state.h"
#include "sym.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <deque>
#include <set>

using namespace UE;

namespace {

// ---------------------------------------------------------------------------------------------
// small shared bits

struct Ident {
    std::string gameId;        // EOS ProductUserId, bare 32-hex
    std::string name;          // character display name, else the platform name
    std::string platformName;  // APlayerState::PlayerNamePrivate ("Limon")
    std::string characterGuid;
    std::string steamId;
    bool valid() const { return !gameId.empty(); }
};

std::string PlayerJson(const Ident& id) {
    std::string o = "{\"gameId\":" + JsonStr(id.gameId) + ",\"name\":" + JsonStr(id.name.empty() ? id.platformName : id.name);
    if (!id.name.empty()) o += ",\"characterName\":" + JsonStr(id.name);
    if (!id.platformName.empty()) o += ",\"platformName\":" + JsonStr(id.platformName);
    if (!id.characterGuid.empty()) o += ",\"characterGuid\":" + JsonStr(id.characterGuid);
    if (!id.steamId.empty()) o += ",\"steamId\":" + JsonStr(id.steamId);
    if (!id.gameId.empty()) o += ",\"platformId\":" + JsonStr("epic:" + id.gameId);
    return o + "}";
}

// Diagnostics, all cheap counters shown under /health.diagnostics.eventSources.
struct SourceStat {
    std::atomic<uint64_t> fired{0};
    std::atomic<uint64_t> emitted{0};
    std::atomic<bool> hooked{false};
    std::string note;
};
SourceStat g_join, g_leave, g_chat, g_death, g_kill, g_log;
Mutex g_noteLock;
void Note(SourceStat& s, const std::string& n) {
    Guard g(g_noteLock);
    s.note = n;
}
std::string NoteOf(SourceStat& s) {
    Guard g(g_noteLock);
    return s.note;
}

void Degrade(const char* cap, SourceStat& s, const std::string& why) {
    Note(s, why);
    PluginState::Get().SetCapability(cap, "degraded", why);
    PluginLog("events: %s degraded: %s", cap, why.c_str());
}
void Ok(const char* cap, SourceStat& s, const std::string& how) {
    Note(s, how);
    PluginState::Get().SetCapability(cap, "ok", "");
    PluginLog("events: %s ok (%s)", cap, how.c_str());
}

// ---------------------------------------------------------------------------------------------
// reflection helpers (game thread only)

// Cached property offset lookup: (struct, name) -> Offset_Internal, -1 when absent.
Mutex g_offLock;
std::map<std::pair<void*, std::string>, int32_t> g_offCache;
int32_t PropOff(void* strct, const char* name) {
    if (!strct) return -1;
    std::pair<void*, std::string> key{strct, name};
    {
        Guard g(g_offLock);
        auto it = g_offCache.find(key);
        if (it != g_offCache.end()) return it->second;
    }
    void* p = Reflect::FindProperty(strct, name);
    int32_t off = p ? Reflect::PropertyOffset(p) : -1;
    if (off < 0 || off > 0x100000) off = -1;
    Guard g(g_offLock);
    g_offCache[key] = off;
    return off;
}
int32_t PropOffOf(void* obj, const char* name) { return PropOff(Reflect::ObjClass(obj), name); }

template <typename T>
bool ReadAt(void* base, int32_t off, T& out) {
    if (!base || off < 0) return false;
    const void* p = (const char*)base + off;
    if (!MemReadable(p, sizeof(T))) return false;
    memcpy(&out, p, sizeof(T));
    return true;
}

std::string ReadFStringAt(void* base, int32_t off) {
    FString s;
    if (!ReadAt(base, off, s)) return "";
    if (s.Num <= 0 || s.Num > 65536) return "";
    return Reflect::Utf16To8(s.Data, s.Num);
}

// Walks the FField chain of one UStruct (declaration order).
void WalkProps(void* strct, std::vector<void*>& out, size_t max) {
    const auto& lay = Reflect::Lay();
    void* f = nullptr;
    if (!ReadAt(strct, (int32_t)lay.structChildProperties, f)) return;
    for (size_t i = 0; f && i < max; i++) {
        out.push_back(f);
        void* nxt = nullptr;
        if (!ReadAt(f, (int32_t)lay.fieldNext, nxt)) break;
        f = nxt;
    }
}

std::string FieldName(void* field) {
    const auto& lay = Reflect::Lay();
    FName n;
    if (!ReadAt(field, (int32_t)lay.fieldName, n)) return "";
    return Reflect::NameToString(n);
}

// Cached class pointers used for identity tests. Comparing *pointers* never stringifies anything,
// which is what killed the server once already (SIGSEGV inside FName::ToString).
void* g_clsUObject = nullptr;
void* g_clsUClass = nullptr;
void* g_clsPlayerState = nullptr;
void* g_clsHealthComponent = nullptr;
void* g_clsAiCharacter = nullptr;

// True when `obj` really looks like a live UObject: its Class pointer resolves and the class chain
// terminates at UClass(UObject). Nothing here reads an FName, so a bogus pointer cannot fault the
// engine name pool. Every ObjName()/ClassName() call in this file is gated on this.
bool ValidObject(void* obj) {
    if (!obj || !MemReadable(obj, 0x40)) return false;
    void* cls = Reflect::ObjClass(obj);
    if (!cls || !MemReadable(cls, 0x120) || !g_clsUObject) return false;
    for (int i = 0; cls && i < 32; i++) {
        if (cls == g_clsUObject) return true;
        void* sup = Reflect::SuperStruct(cls);
        if (sup == cls) return false;
        cls = sup;
    }
    return false;
}

std::string SafeObjName(void* obj) { return ValidObject(obj) ? Reflect::ObjName(obj) : std::string(); }
std::string SafeClassName(void* obj) { return ValidObject(obj) ? Reflect::ClassName(obj) : std::string(); }

// Raw FName of a UObject (no stringification) - the safe way to identify a UFunction in a hot hook.
bool ObjNameRaw(void* obj, FName& out) {
    return ReadAt(obj, (int32_t)Reflect::Lay().objName, out);
}

// UScriptStruct / UClass by name, trying the packages we know from /debug/structs.
void* FindStructByName(const char* name) {
    static const char* kPkgs[] = {"/Script/Dominion", "/Script/JagexChatBackend", "/Script/Engine",
                                  "/Script/CoreUObject", "/Script/DominionRuntime"};
    for (auto* p : kPkgs) {
        void* s = Reflect::FindObjectByPath(p, name);
        if (s) return s;
    }
    return nullptr;
}

// Calls a UFunction that takes no parameters and returns an FString (e.g. GetCharacterDisplayName).
using FnProcessEvent = void (*)(void* self, void* func, void* params);
FnProcessEvent g_processEvent = nullptr;


// CRASHED THE SERVER - DO NOT CALL THESE.
// ADominionPlayerState::GetCharacterDisplayName() and, behind it,
// UDisplayNameComponent::GetCharacterDisplayName(FDomOwnerGuid const&, FString const&) fault inside
// UDisplayNameComponent::DoOwnerGuidsMatch -> operator==(FGuid const&, FGuid const&) when the
// display-name registry has no entry for the player state yet (SIGSEGV at 0x0, rig log
// RSDragonwilds-backup-2026.09.16-19.50.28.log, callstack straight through this plugin). The
// symbols stay resolved for the record, but the character name is read from the reflected property
// and from the server log's join line instead - neither can fault.

// ---------------------------------------------------------------------------------------------
// identity

void* g_eosVptr = nullptr;  // _ZTV15FUniqueNetIdEOS + 2 words
using FnEosToString = void (*)(FString* ret, const void* self);
FnEosToString g_eosToString = nullptr;

std::string StripRedpoint(const std::string& s) {
    const char* kPrefix = "RedpointEOS:";
    if (s.size() > 12 && s.compare(0, 12, kPrefix) == 0) return s.substr(12);
    size_t colon = s.rfind(':');
    if (colon != std::string::npos && s.size() - colon - 1 == 32) return s.substr(colon + 1);
    return s;
}

bool LooksLikePuid(const std::string& s) {
    if (s.size() != 32) return false;
    for (char c : s)
        if (!isxdigit((unsigned char)c)) return false;
    return true;
}

// APlayerState::UniqueID is an FUniqueNetIdRepl; its shared pointer sits somewhere in the first few
// words. We do not assume the offset: we scan the struct for a pointer whose vtable is exactly
// _ZTV15FUniqueNetIdEOS and call FUniqueNetIdEOS::ToString on that.
// Scans an FUniqueNetIdRepl at `base + off` for the shared FUniqueNetIdEOS and stringifies it.
std::string PuidFromNetIdRepl(void* base, int32_t off) {
    if (!base || off < 0 || !g_eosVptr || !g_eosToString) return "";
    for (int i = 0; i < 8; i++) {
        void* cand = nullptr;
        if (!ReadAt(base, off + i * 8, cand)) continue;
        if (!cand || !MemReadable(cand, 8)) continue;
        void* vptr = *(void**)cand;
        if (vptr != g_eosVptr) continue;
        FString ret{};
        g_eosToString(&ret, cand);
        std::string s = Reflect::ToStd(ret, true);
        s = StripRedpoint(s);
        if (LooksLikePuid(s)) {
            for (auto& c : s) c = (char)tolower((unsigned char)c);
            return s;
        }
    }
    return "";
}

std::string PuidFromPlayerState(void* state) {
    if (!state) return "";
    int32_t off = PropOffOf(state, "UniqueID");
    if (off < 0) off = PropOffOf(state, "UniqueId");
    return PuidFromNetIdRepl(state, off);
}

std::string GuidFromStruct(void* base, int32_t off) {
    uint32_t g[4];
    if (off < 0 || !base || !MemReadable((const char*)base + off, 16)) return "";
    memcpy(g, (const char*)base + off, 16);
    if (!(g[0] | g[1] | g[2] | g[3])) return "";
    char b[40];
    snprintf(b, sizeof b, "%08X%08X%08X%08X", g[0], g[1], g[2], g[3]);
    return b;
}

// The chat component, pawn or controller -> the APlayerState that owns it.
void* PlayerStateOf(void* actor) {
    if (!ValidObject(actor)) return nullptr;
    if (g_clsPlayerState && Reflect::IsA(actor, g_clsPlayerState)) return actor;
    int32_t off = PropOffOf(actor, "PlayerState");
    void* st = nullptr;
    if (off >= 0 && ReadAt(actor, off, st) && ValidObject(st)) return st;
    // Components: the owning actor is the Outer.
    void* outer = Reflect::ObjOuter(actor);
    if (outer && outer != actor && ValidObject(outer)) {
        if (g_clsPlayerState && Reflect::IsA(outer, g_clsPlayerState)) return outer;
        off = PropOffOf(outer, "PlayerState");
        if (off >= 0 && ReadAt(outer, off, st) && ValidObject(st)) return st;
    }
    return nullptr;
}

bool IdentFromPlayerState(void* state, Ident& out) {
    if (!ValidObject(state)) return false;
    out.gameId = PuidFromPlayerState(state);
    out.platformName = ReadFStringAt(state, PropOffOf(state, "PlayerNamePrivate"));
    out.name = ReadFStringAt(state, PropOffOf(state, "CharacterName"));
    if (out.name.empty()) out.name = ::state::CharacterName(out.gameId);
    out.characterGuid = GuidFromStruct(state, PropOffOf(state, "OwnerGuid"));
    if (!out.gameId.empty() && !out.name.empty()) ::state::NoteCharacterName(out.gameId, out.name);
    if (out.name.empty()) out.name = out.platformName;
    return out.valid();
}

Ident IdentFromController(void* controller) {
    Ident id;
    void* st = PlayerStateOf(controller);
    if (st) IdentFromPlayerState(st, id);
    return id;
}

// ---------------------------------------------------------------------------------------------
// connection tracking

Mutex g_healthLock;
// gameId -> last observed liveness. The first sample only seeds the state: right after a join the
// pawn's replicated health is still 0, which would otherwise look like a death.
enum class Live { Unknown, Alive, Dead };
std::map<std::string, Live> g_liveness;

struct Conn {
    void* controller = nullptr;
    void* playerState = nullptr;
    Ident id;
    uint64_t firstSeenMs = 0;
    bool announced = false;
    bool left = false;
};

Mutex g_connLock;
std::vector<Conn> g_conns;  // small (max 6 players)

Conn* FindConn(void* c) {
    for (auto& k : g_conns)
        if (k.controller == c) return &k;
    return nullptr;
}

void ClearLeaveMarker(const std::string& gameId);

void EmitJoin(const Ident& id) {
    ClearLeaveMarker(id.gameId);
    PluginState::Get().EmitEvent("player-connected", "{\"player\":" + PlayerJson(id) + "}");
    g_join.emitted++;
    PluginLog("events: player-connected %s (%s)", id.gameId.c_str(), id.name.c_str());
}

void EmitLeave(const Ident& id) {
    PluginState::Get().EmitEvent("player-disconnected", "{\"player\":" + PlayerJson(id) + "}");
    g_leave.emitted++;
    PluginLog("events: player-disconnected %s (%s)", id.gameId.c_str(), id.name.c_str());
}

// ---------------------------------------------------------------------------------------------
// game-mode hooks: PostLogin / PreLogout / Logout
//
// Swaps the slot holding `symName` in *every* exported vtable that contains it. Hooking only the
// declaring class never fires when the live object is a subclass carrying its own vtable (the
// lesson from the engine Tick hook in L1).
size_t SwapEverywhere(const char* symName, void* detour, size_t& slotOut, std::string& tablesOut) {
    uint64_t target = Sym::Addr(symName);
    if (!target) return 0;
    size_t hooked = 0;
    std::string tables;
    for (auto& vt : VTableSymbols()) {
        uint64_t addr = vt.second.first, vsize = vt.second.second;
        auto* words = (const uint64_t*)(uintptr_t)addr;
        size_t count = (size_t)(vsize / 8);
        if (count < 3 || !MemReadable(words, (size_t)vsize)) continue;
        for (size_t i = 2; i < count; i++) {
            if (words[i] != target) continue;
            std::string err;
            if (Hooks::SwapVTableSlot(symName, (void*)(uintptr_t)addr, i - 2, detour, nullptr, err)) {
                hooked++;
                slotOut = i - 2;
                if (tables.size() < 160) tables += (tables.empty() ? "" : ",") + vt.first;
            } else {
                PluginLog("events: %s slot %zu in %s not swapped: %s", symName, i - 2, vt.first.c_str(), err.c_str());
            }
        }
    }
    tablesOut = tables;
    return hooked;
}

using FnPostLogin = void (*)(void* self, void* pc);
using FnLogout = void (*)(void* self, void* controller);
// AGameModeBase::PreLogin(FString const& Options, FString const& Address, FUniqueNetIdRepl const&,
// FString& ErrorMessage). A non-empty ErrorMessage is what makes the engine log
// "LogNet: PreLogin failure: <msg>" and refuse the connection.
using FnPreLogin = void (*)(void* self, const FString* options, const FString* address, const void* netId,
                            FString* errorMessage);
using FnNetCleanup = void (*)(void* self, void* connection);
using FnDestroyed = void (*)(void* self);
FnPostLogin g_origPostLogin = nullptr;
FnLogout g_origPreLogout = nullptr;
FnLogout g_origLogout = nullptr;
FnLogout g_origLogoutGm = nullptr;
FnPreLogin g_origPreLoginDom = nullptr;   // ADominionGameMode::PreLogin (what the live vtable holds)
FnPreLogin g_origPreLoginBase = nullptr;  // AGameModeBase::PreLogin (game modes that do not override)
FnNetCleanup g_origNetCleanup = nullptr;
FnDestroyed g_origDestroyed = nullptr;
std::atomic<uint64_t> g_banRefusals{0};
std::atomic<bool> g_preLoginHooked{false};

// The refusal code the game uses itself for a banned login; the client renders it as a ban notice.
const char* kBannedError = "PLogBanned";

// An FString the engine may own and free (FMemory::Malloc, like the rest of the game's strings).
bool MakeGameFString(const std::string& text, FString& out) {
    using FnMalloc = void* (*)(size_t, uint32_t);
    auto malloc_ = (FnMalloc)(uintptr_t)Sym::Addr("FMemory::Malloc");
    if (!malloc_) return false;
    std::vector<char16_t> w = Reflect::Utf8To16(text);  // NUL-terminated
    void* mem = malloc_(w.size() * sizeof(char16_t), 8);
    if (!mem) return false;
    memcpy(mem, w.data(), w.size() * sizeof(char16_t));
    out.Data = (char16_t*)mem;
    out.Num = (int32_t)w.size();
    out.Max = out.Num;
    return true;
}

// Live ban enforcement. The game only reads its own ban list into the login path at start-up, so a
// ban taken on a running server did nothing until a restart (lane L3's caveat). This refuses the
// login itself, from the plugin's ban list, which POST /ban keeps in step with the game's list.
void PreLoginBanGate(const void* netId, FString* errorMessage) {
    try {
        // Never overwrite a refusal the game already made (it wins, and it is already correct).
        if (!errorMessage || !MemWritable(errorMessage, sizeof(FString))) return;
        if (errorMessage->Num > 0) return;
        std::string id = PuidFromNetIdRepl((void*)netId, 0);
        if (id.empty() || !::state::IsBanned(id)) return;
        FString msg{};
        if (!MakeGameFString(kBannedError, msg)) {
            PluginLog("events: PreLogin wanted to refuse %s but could not allocate the error string", id.c_str());
            return;
        }
        memcpy(errorMessage, &msg, sizeof msg);
        g_banRefusals++;
        PluginLog("events: PreLogin refused banned player %s (%s)", id.c_str(), kBannedError);
    } catch (...) {
    }
}

// One detour per declaring class: the slot of a game mode that overrides PreLogin holds the
// override, so each vtable must be given back its own original.
void DetourPreLoginDom(void* self, const FString* options, const FString* address, const void* netId,
                       FString* errorMessage) {
    if (g_origPreLoginDom) g_origPreLoginDom(self, options, address, netId, errorMessage);
    Hooks::MarkFired("ADominionGameMode::PreLogin");
    PreLoginBanGate(netId, errorMessage);
}

void DetourPreLoginBase(void* self, const FString* options, const FString* address, const void* netId,
                        FString* errorMessage) {
    if (g_origPreLoginBase) g_origPreLoginBase(self, options, address, netId, errorMessage);
    Hooks::MarkFired("AGameModeBase::PreLogin");
    PreLoginBanGate(netId, errorMessage);
}

// gameId -> the millisecond we last emitted a disconnect for it. Several paths can report the same
// disconnect (OnNetCleanup, Destroyed, the reaper); only the first one emits.
std::map<std::string, uint64_t> g_recentLeaves;
const uint64_t kLeaveDedupeMs = 30000;

void ClearLeaveMarker(const std::string& gameId) {
    Guard g(g_connLock);
    g_recentLeaves.erase(gameId);
}

bool LeaveAlreadyEmitted(const std::string& gameId) {
    Guard g(g_connLock);
    uint64_t now = NowMs();
    for (auto it = g_recentLeaves.begin(); it != g_recentLeaves.end();)
        it = (now - it->second > kLeaveDedupeMs) ? g_recentLeaves.erase(it) : ++it;
    return g_recentLeaves.find(gameId) != g_recentLeaves.end();
}

void MarkLeft(const std::string& gameId) {
    Guard g(g_connLock);
    g_recentLeaves[gameId] = NowMs();
}

void OnLeave(void* controller, const char* which) {
    Ident id;
    bool announced = false;
    {
        Guard g(g_connLock);
        Conn* c = FindConn(controller);
        if (c) {
            if (c->left) return;  // deduped: PreLogout and Logout both fire for one connection
            c->left = true;
            id = c->id;
            announced = c->announced;
        }
    }
    if (!id.valid()) id = IdentFromController(controller);
    if (!id.valid()) {
        if (DebugEnabled())
            PluginLog("events: %s for controller %p with no resolvable identity", which, controller);
        return;
    }
    if (LeaveAlreadyEmitted(id.gameId)) {
        if (DebugEnabled()) PluginLog("events: %s for %s already reported", which, id.gameId.c_str());
        return;
    }
    PluginLog("events: disconnect for %s reported by %s", id.gameId.c_str(), which);
    {
        Guard g(g_healthLock);
        g_liveness.erase(id.gameId);
    }
    if (!announced) EmitJoin(id);  // never report a leave for a player we never announced
    EmitLeave(id);
    MarkLeft(id.gameId);
    Guard g(g_connLock);
    for (size_t i = 0; i < g_conns.size(); i++)
        if (g_conns[i].controller == controller) {
            g_conns.erase(g_conns.begin() + (long)i);
            break;
        }
}

void DetourPostLogin(void* self, void* pc) {
    if (g_origPostLogin) g_origPostLogin(self, pc);
    Hooks::MarkFired("ADominionGameMode::PostLogin");
    g_join.fired++;
    try {
        Guard g(g_connLock);
        if (!FindConn(pc)) {
            Conn c;
            c.controller = pc;
            c.firstSeenMs = NowMs();
            g_conns.push_back(c);
        }
    } catch (...) {
    }
}

void DetourPreLogout(void* self, void* controller) {
    try {
        Hooks::MarkFired("ADominionGameMode::PreLogout");
        g_leave.fired++;
        OnLeave(controller, "PreLogout");
    } catch (...) {
    }
    if (g_origPreLogout) g_origPreLogout(self, controller);
}

void DetourLogoutGm(void* self, void* controller) {
    try {
        Hooks::MarkFired("AGameMode::Logout");
        g_leave.fired++;
        OnLeave(controller, "AGameMode::Logout");
    } catch (...) {
    }
    if (g_origLogoutGm) g_origLogoutGm(self, controller);
}

void DetourLogout(void* self, void* controller) {
    try {
        Hooks::MarkFired("AGameModeBase::Logout");
        g_leave.fired++;
        OnLeave(controller, "Logout");
    } catch (...) {
    }
    if (g_origLogout) g_origLogout(self, controller);
}

// The disconnect path that actually fires on this build. ADominionGameMode inherits
// AGameMode::Logout, but the Dominion player controller overrides OnNetCleanup and completes its own
// logout asynchronously (ADominionPlayerController::HandleLogout / OnClientDisconnectMeSaveComplete),
// so neither Logout override is ever reached. OnNetCleanup is reached for every disconnect, clean or
// not, and Destroyed() backs it up. The connection reaper is kept as a third line of defence.
void DetourNetCleanup(void* self, void* connection) {
    try {
        Hooks::MarkFired("ADominionPlayerController::OnNetCleanup");
        g_leave.fired++;
        OnLeave(self, "OnNetCleanup");
    } catch (...) {
    }
    if (g_origNetCleanup) g_origNetCleanup(self, connection);
}

void DetourControllerDestroyed(void* self) {
    try {
        Hooks::MarkFired("ADominionPlayerController::Destroyed");
        OnLeave(self, "Destroyed");
    } catch (...) {
    }
    if (g_origDestroyed) g_origDestroyed(self);
}

// The exported-vtable sweep does not bind the live game mode's PreLogin slot on this build (the
// slot's contents are not what the file image says), so the live object's own vtable is scanned for
// either PreLogin address and that slot is swapped directly - the same technique the chat hook uses.
// Runs on the game thread, retried from housekeeping until it succeeds.
void HookLivePreLogin() {
    if (g_preLoginHooked.load()) return;
    uint64_t dom = Sym::Addr("ADominionGameMode::PreLogin");
    uint64_t base = Sym::Addr("AGameModeBase::PreLogin");
    if (!dom && !base) return;
    void* cls = Reflect::FindObjectByPath("/Script/Dominion", "DominionGameMode");
    if (!cls) cls = Reflect::FindObjectByPath("/Script/Engine", "GameModeBase");
    if (!cls) return;
    std::vector<void*> modes;
    if (!Reflect::GetObjectsOfClass(cls, modes, true)) return;
    for (void* gm : modes) {
        if (!ValidObject(gm)) continue;
        void* vt = *(void**)gm;
        if (!vt || !MemReadable(vt, 8 * 512)) continue;
        auto* words = (const uint64_t*)vt;
        for (size_t i = 0; i < 512; i++) {
            bool isDom = dom && words[i] == dom;
            bool isBase = base && words[i] == base;
            if (!isDom && !isBase) continue;
            void* orig = nullptr;
            std::string err;
            const char* name = isDom ? "ADominionGameMode::PreLogin" : "AGameModeBase::PreLogin";
            if (!Hooks::HookObjectVTable(name, gm, i, isDom ? (void*)&DetourPreLoginDom : (void*)&DetourPreLoginBase,
                                         &orig, err)) {
                PluginLog("events: live PreLogin hook failed at slot %zu: %s", i, err.c_str());
                break;
            }
            if (isDom) g_origPreLoginDom = (FnPreLogin)orig;
            else g_origPreLoginBase = (FnPreLogin)orig;
            g_preLoginHooked = true;
            PluginLog("events: live PreLogin hooked on %s at slot %zu (orig=%p)", name, i, orig);
            return;
        }
    }
}

// Resolves pending joins: the PUID is only valid once the client finished login, and the character
// name arrives a few seconds later still (see research/log-grammar.md).
const uint64_t kNameGraceMs = 12000;
const uint64_t kJoinGiveUpMs = 120000;

void ResolvePendingJoins() {
    std::vector<std::pair<void*, Ident>> toAnnounce;
    std::vector<void*> drop;
    std::vector<std::string> banned;
    {
        Guard g(g_connLock);
        for (auto& c : g_conns) {
            if (c.announced || c.left) continue;
            Ident id = IdentFromController(c.controller);
            uint64_t age = NowMs() - c.firstSeenMs;
            if (!id.valid()) {
                if (age > kJoinGiveUpMs) drop.push_back(c.controller);
                continue;
            }
            // Wait a little for the character name, but never block the event on it.
            if (id.name.empty() && age < kNameGraceMs) continue;
            if (id.name == id.platformName && !id.platformName.empty() && age < kNameGraceMs) continue;
            c.id = id;
            c.playerState = PlayerStateOf(c.controller);
            c.announced = true;
            toAnnounce.push_back({c.controller, id});
            if (::state::IsBanned(id.gameId)) banned.push_back(id.gameId);
        }
        for (void* d : drop)
            for (size_t i = 0; i < g_conns.size(); i++)
                if (g_conns[i].controller == d) {
                    PluginLog("events: giving up on join for controller %p (no identity after %llums)", d,
                              (unsigned long long)kJoinGiveUpMs);
                    g_conns.erase(g_conns.begin() + (long)i);
                    break;
                }
    }
    for (auto& a : toAnnounce) EmitJoin(a.second);
    // Belt and braces: PreLogin should have refused these, so reaching here is worth a log line.
    for (auto& id : banned) {
        PluginLog("events: banned player %s got past PreLogin; kicking", id.c_str());
        Actions::KickBanned(id);
    }
}

// Safety net for player-disconnected: if the APlayerState we saw at join is no longer in the live
// object array, the connection is gone even when no Logout hook fired (hard drop, or a game-mode
// subclass we did not bind). Runs on the game thread.
void ReapGoneConnections() {
    std::vector<std::pair<void*, void*>> announced;  // {controller, playerState}
    {
        Guard g(g_connLock);
        for (auto& c : g_conns)
            if (c.announced && !c.left && c.playerState) announced.push_back({c.controller, c.playerState});
    }
    if (announced.empty()) return;
    if (!g_clsPlayerState) return;
    std::vector<void*> states;
    if (!Reflect::GetObjectsOfClass(g_clsPlayerState, states, true)) return;
    for (auto& a : announced) {
        bool alive = false;
        for (void* s : states)
            if (s == a.second) { alive = true; break; }
        if (!alive) OnLeave(a.first, "player state gone");
    }
}

// ---------------------------------------------------------------------------------------------
// ProcessEvent hooks (chat / death / kill)

struct PeEntry {
    void* vtable = nullptr;
    void* orig = nullptr;
};
PeEntry g_pe[128];
std::atomic<size_t> g_peCount{0};

// Raw FNames of the UFunctions we care about (filled on the game thread once).
FName g_fnChat, g_fnDeath, g_fnAiKilled, g_fnPlayerEvent, g_fnBpOnDeath, g_fnOnDeathEvent;
bool g_fnNamesReady = false;

void* g_chatDataStruct = nullptr;   // FChatMessageData
void* g_damageStruct = nullptr;     // FDominionDamageEvent
int32_t g_msgBodyOff = -1;

Mutex g_deathLock;
std::map<std::string, uint64_t> g_lastDeathMs;  // gameId -> ms, 3 s dedupe
const uint64_t kDeathDedupeMs = 3000;

bool DeathAllowed(const std::string& gameId) {
    Guard g(g_deathLock);
    uint64_t now = NowMs();
    auto it = g_lastDeathMs.find(gameId);
    if (it != g_lastDeathMs.end() && now - it->second < kDeathDedupeMs) return false;
    g_lastDeathMs[gameId] = now;
    return true;
}

void EmitDeath(const Ident& id, double x, double y, double z, bool havePos, const std::string& attackerJson,
               const std::string& killerEntity, const char* source) {
    if (!id.valid() || !DeathAllowed(id.gameId)) return;
    std::string o = "{\"player\":" + PlayerJson(id);
    if (havePos)
        o += ",\"position\":{\"x\":" + JsonNum(x) + ",\"y\":" + JsonNum(y) + ",\"z\":" + JsonNum(z) + "}";
    if (!attackerJson.empty()) o += ",\"attacker\":" + attackerJson;
    if (!killerEntity.empty()) o += ",\"killerEntity\":" + JsonStr(killerEntity);
    o += ",\"source\":" + JsonStr(source) + "}";
    PluginState::Get().EmitEvent("player-death", o);
    g_death.emitted++;
    PluginLog("events: player-death %s via %s (killer '%s')", id.gameId.c_str(), source, killerEntity.c_str());
}

void HandleChat(void* self, void* params) {
    if (g_msgBodyOff < 0 || !params) return;
    std::string msg = ReadFStringAt(params, g_msgBodyOff);
    if (msg.empty()) return;
    if (state::ConsumeInjectedMessage(msg)) {
        PluginLog("events: ignoring our own injected chat message");
        return;
    }
    void* st = PlayerStateOf(self);
    Ident id;
    if (st) IdentFromPlayerState(st, id);
    std::string o = "{\"msg\":" + JsonStr(msg) + ",\"channel\":\"global\"";
    if (id.valid()) o += ",\"player\":" + PlayerJson(id);
    o += "}";
    PluginState::Get().EmitEvent("chat-message", o);
    g_chat.emitted++;
    PluginLog("events: chat-message from %s: %s", id.gameId.c_str(), msg.c_str());
}

void HandleDeathTelemetry(void* self, void* func, void* params) {
    // The single parameter is an FDominionDamageEvent; its offset in the params block is the
    // parameter property's own offset (0 in practice, but read it reflectively anyway).
    int32_t base = 0;
    std::vector<void*> props;
    WalkProps(func, props, 16);
    if (!props.empty()) base = Reflect::PropertyOffset(props[0]);
    if (base < 0) base = 0;
    void* dmg = (char*)params + base;

    double x = 0, y = 0, z = 0;
    bool havePos = false;
    std::string killerEntity, attackerJson;
    if (g_damageStruct) {
        int32_t locOff = PropOff(g_damageStruct, "VictimLocation");
        if (locOff >= 0 && MemReadable((const char*)dmg + locOff, 24)) {
            const double* v = (const double*)((const char*)dmg + locOff);
            x = v[0]; y = v[1]; z = v[2];
            havePos = (x != 0.0 || y != 0.0 || z != 0.0);  // the struct is zero for some death causes
        }
        int32_t instOff = PropOff(g_damageStruct, "Instigator");
        void* inst = nullptr;
        if (instOff >= 0 && ReadAt(dmg, instOff, inst) && ValidObject(inst)) {
            Ident attacker;
            void* st = PlayerStateOf(inst);
            if (st) IdentFromPlayerState(st, attacker);
            if (attacker.valid()) attackerJson = PlayerJson(attacker);
            else killerEntity = SafeClassName(inst);
        }
    }
    Ident victim;
    void* st = PlayerStateOf(self);
    if (st) IdentFromPlayerState(st, victim);
    if (!victim.valid()) return;
    if (havePos && !MemReadable(dmg, 8)) havePos = false;
    EmitDeath(victim, x, y, z, havePos, attackerJson, killerEntity, "telemetry");
}

void HandleAiKilled(void* self, void* func, void* params) {
    // Killer = the owner of the UProgressComponent (a player). Entity = the first object parameter's
    // class/asset name; the parameter list is discovered reflectively and logged once.
    Ident killer;
    void* st = PlayerStateOf(self);
    if (st) IdentFromPlayerState(st, killer);

    std::string entity, weapon;
    std::vector<void*> props;
    WalkProps(func, props, 16);
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true)) {
        std::string list;
        for (void* p : props)
            list += FieldName(p) + ":" + Reflect::PropertyTypeName(p) + "@" +
                    std::to_string(Reflect::PropertyOffset(p)) + " ";
        PluginLog("events: OnAIKilled params = %s", list.c_str());
    }
    for (void* p : props) {
        std::string type = Reflect::PropertyTypeName(p);
        int32_t off = Reflect::PropertyOffset(p);
        std::string pname = FieldName(p);
        if (entity.empty() && (type == "ObjectProperty" || type == "ClassProperty" || type == "ObjectPtrProperty" ||
                               type == "SoftObjectProperty" || type == "WeakObjectProperty")) {
            void* o = nullptr;
            if (ReadAt(params, off, o) && ValidObject(o)) {
                entity = SafeObjName(o);
                if (entity.empty()) entity = SafeClassName(o);
            }
        } else if (entity.empty() && type == "NameProperty") {
            FName n;
            if (ReadAt(params, off, n)) entity = Reflect::NameToString(n);
        } else if (entity.empty() && type == "StrProperty") {
            entity = ReadFStringAt(params, off);
        }
        if (weapon.empty() && pname.find("Weapon") != std::string::npos) {
            void* o = nullptr;
            if (type.find("Object") != std::string::npos && ReadAt(params, off, o) && ValidObject(o))
                weapon = SafeObjName(o);
        }
    }
    if (entity.empty()) entity = "unknown";
    std::string o = "{\"entity\":" + JsonStr(entity) + ",\"weapon\":" + JsonStr(weapon);
    if (killer.valid()) o += ",\"player\":" + PlayerJson(killer);
    o += "}";
    PluginState::Get().EmitEvent("entity-killed", o);
    g_kill.emitted++;
    PluginLog("events: entity-killed '%s' by %s", entity.c_str(), killer.gameId.c_str());
}

// The actor a component belongs to (components are Outered to their owner).
void* OwnerOf(void* comp) {
    if (!ValidObject(comp)) return nullptr;
    int32_t ownerOff = PropOffOf(comp, "Owner");
    void* o = nullptr;
    if (ownerOff >= 0 && ReadAt(comp, ownerOff, o) && ValidObject(o)) return o;
    void* outer = Reflect::ObjOuter(comp);
    return ValidObject(outer) ? outer : nullptr;
}

Mutex g_killLock;
std::map<void*, uint64_t> g_lastKillMs;  // AI actor -> ms; BP_OnDeath and OnDeathEvent both fire
const uint64_t kKillDedupeMs = 5000;
std::atomic<bool> g_aiDumped{false};

bool KillAllowed(void* actor) {
    Guard g(g_killLock);
    uint64_t now = NowMs();
    for (auto it = g_lastKillMs.begin(); it != g_lastKillMs.end();)
        it = (now - it->second > kKillDedupeMs) ? g_lastKillMs.erase(it) : ++it;
    if (g_lastKillMs.count(actor)) return false;
    g_lastKillMs[actor] = now;
    return true;
}

// Who killed it. In order of confidence:
//   1. an object property on the AI or its components that names an instigator / killer / causer and
//      resolves to a player's APlayerState;
//   2. the AI's replicated CurrentTarget (what it was fighting when it died);
//   3. the only player in the world, when there is exactly one.
// The method used is reported in the event, so nothing here is presented as more than it is.
Ident KillerOf(void* actor, std::string& how) {
    Ident id;
    static const char* kHints[] = {"Instigator", "Killer", "LastDamage", "Causer", "DamageDealer"};
    std::vector<void*> props;
    WalkProps(Reflect::ObjClass(actor), props, 256);
    for (void* cls = Reflect::ObjClass(actor); cls; cls = Reflect::SuperStruct(cls)) {
        if (cls == Reflect::ObjClass(actor)) continue;
        WalkProps(cls, props, 256);
    }
    for (void* p : props) {
        std::string pname = FieldName(p);
        bool hinted = false;
        for (const char* h : kHints) hinted = hinted || pname.find(h) != std::string::npos;
        if (!hinted) continue;
        std::string type = Reflect::PropertyTypeName(p);
        if (type.find("Object") == std::string::npos) continue;
        void* o = nullptr;
        if (!ReadAt(actor, Reflect::PropertyOffset(p), o) || !ValidObject(o)) continue;
        void* st = PlayerStateOf(o);
        if (st && IdentFromPlayerState(st, id) && id.valid()) {
            how = "damage instigator (" + pname + ")";
            return id;
        }
    }
    int32_t tgtOff = PropOffOf(actor, "CurrentTarget");
    void* tgt = nullptr;
    if (tgtOff >= 0 && ReadAt(actor, tgtOff, tgt) && ValidObject(tgt)) {
        void* st = PlayerStateOf(tgt);
        if (st && IdentFromPlayerState(st, id) && id.valid()) {
            how = "the AI's current target";
            return id;
        }
    }
    // Exactly one player online -> it was them.
    if (g_clsPlayerState) {
        std::vector<void*> states;
        if (Reflect::GetObjectsOfClass(g_clsPlayerState, states, true)) {
            std::vector<Ident> online;
            for (void* st : states) {
                Ident k;
                if (IdentFromPlayerState(st, k) && k.valid()) online.push_back(k);
            }
            if (online.size() == 1) {
                how = "the only player in the world";
                return online[0];
            }
        }
    }
    how = "unattributed";
    return Ident();
}

// A death reported by a hook that carries no damage event: BP_OnDeath on the AI character itself, or
// UHealthComponent::OnDeathEvent on any actor's health component. Player deaths keep going through
// the telemetry/health-edge paths, so only AI deaths are turned into entity-killed here.
void HandleActorDeath(void* actor, const char* via) {
    if (!ValidObject(actor)) return;
    if (!g_clsAiCharacter || !Reflect::IsA(actor, g_clsAiCharacter)) return;  // players: other paths
    if (!KillAllowed(actor)) return;

    // Once per boot, write the dying AI's whole property tree to plugin.log: the next lane can pick
    // the exact instigator property out of it instead of guessing again.
    if (DebugEnabled() && !g_aiDumped.exchange(true))
        PluginLog("events: first AI death, %s properties = %s", SafeClassName(actor).c_str(),
                  Reflect::DumpObject(actor, 256).c_str());

    std::string cls = SafeClassName(actor);
    std::string entity = cls;
    // The AI data asset carries the readable name when the actor has one.
    for (const char* n : {"AIData", "AiData", "AIDataAsset", "AiDataAsset", "DataAsset"}) {
        int32_t off = PropOffOf(actor, n);
        void* asset = nullptr;
        if (off < 0 || !ReadAt(actor, off, asset) || !ValidObject(asset)) continue;
        std::string an = SafeObjName(asset);
        if (!an.empty()) { entity = an; break; }
    }
    std::string how;
    Ident killer = KillerOf(actor, how);
    std::string o = "{\"entity\":" + JsonStr(entity) + ",\"entityClass\":" + JsonStr(cls) +
                    ",\"weapon\":\"\",\"source\":" + JsonStr(via) + ",\"attribution\":" + JsonStr(how);
    if (killer.valid()) o += ",\"player\":" + PlayerJson(killer);
    o += "}";
    PluginState::Get().EmitEvent("entity-killed", o);
    g_kill.emitted++;
    PluginLog("events: entity-killed '%s' (%s) via %s, killer %s [%s]", entity.c_str(), cls.c_str(), via,
              killer.gameId.c_str(), how.c_str());
}

void DetourProcessEvent(void* self, void* func, void* params) {
    FnProcessEvent orig = nullptr;
    if (self && MemReadable(self, 8)) {
        void* vt = *(void**)self;
        size_t n = g_peCount.load();
        for (size_t i = 0; i < n && i < 128; i++)
            if (g_pe[i].vtable == vt) { orig = (FnProcessEvent)g_pe[i].orig; break; }
    }
    if (!orig) orig = g_processEvent;

    // Read what we need *before* the call: RPC parameter buffers do not survive it.
    int what = 0;  // 1 chat, 2 death, 3 ai-killed
    try {
        if (g_fnNamesReady && func) {
            FName fn;
            if (ObjNameRaw(func, fn)) {
                if (fn == g_fnChat) what = 1;
                else if (fn == g_fnDeath) what = 2;
                else if (fn == g_fnAiKilled) what = 3;
                else if (fn == g_fnBpOnDeath) what = 4;
                else if (fn == g_fnOnDeathEvent) what = 5;
            }
        }
        if (what == 1) { g_chat.fired++; HandleChat(self, params); }
        else if (what == 2) { g_death.fired++; HandleDeathTelemetry(self, func, params); }
        else if (what == 3) { g_kill.fired++; HandleAiKilled(self, func, params); }
        else if (what == 4) { g_kill.fired++; HandleActorDeath(self, "BP_OnDeath"); }
        else if (what == 5) { g_kill.fired++; HandleActorDeath(OwnerOf(self), "OnDeathEvent"); }
    } catch (...) {
        PluginLog("events: ProcessEvent handler threw (what=%d)", what);
    }
    if (orig) orig(self, func, params);
}

// Hooks the ProcessEvent slot of one live object's vtable, once per distinct vtable.
Mutex g_vtLock;
std::set<void*> g_hookedVts;

bool HookObjectProcessEvent(const std::string& name, void* obj) {
    if (!obj || !MemReadable(obj, 8)) return false;
    void* vt = *(void**)obj;
    if (!vt) return false;
    {
        Guard g(g_vtLock);
        if (g_hookedVts.count(vt)) return false;
    }
    size_t slot = Sym::ProcessEventSlot();
    if (slot == SIZE_MAX) return false;
    void* orig = nullptr;
    std::string err;
    if (!Hooks::HookObjectVTable(name, obj, slot, (void*)&DetourProcessEvent, &orig, err)) {
        PluginLog("events: ProcessEvent hook on %s failed: %s", name.c_str(), err.c_str());
        return false;
    }
    size_t idx = g_peCount.load();
    if (idx < 128) {
        g_pe[idx].vtable = vt;
        g_pe[idx].orig = orig;
        g_peCount.store(idx + 1);
    }
    Guard g(g_vtLock);
    g_hookedVts.insert(vt);
    PluginLog("events: ProcessEvent hooked on %s (vtable %p, orig %p)", name.c_str(), vt, orig);
    return true;
}

// Sweeps the live object array for the classes we hook and hooks any vtable we have not seen yet.
struct ClassTarget {
    const char* pkg;
    const char* cls;
    const char* label;
    void* resolved = nullptr;
    size_t hooked = 0;
};
ClassTarget g_targets[] = {
    {"/Script/JagexChatBackend", "PlayerChatComponent", "UPlayerChatComponent::ProcessEvent"},
    {"/Script/Dominion", "DominionPlayerCharacter", "ADominionPlayerCharacter::ProcessEvent"},
    {"/Script/Dominion", "ProgressComponent", "UProgressComponent::ProcessEvent"},
    // entity-killed: UProgressComponent::OnAIKilled is a UFUNCTION but it is invoked as a plain C++
    // delegate callback, so ProcessEvent never sees it (hooked, fired:0 through lane L2's whole run).
    // The two dispatches that *do* go through ProcessEvent on a kill are
    // ADominionAICharacter::BP_OnDeath (a BlueprintImplementableEvent - its thunk is nothing but a
    // ProcessEvent call) and UHealthComponent::OnDeathEvent (a dynamic-delegate handler). Blueprint
    // subclasses (BP_AI_Kebbit_Character_02_C ...) do not generate their own C++ vtable, so hooking
    // the live objects covers the whole bestiary.
    {"/Script/Dominion", "DominionAICharacter", "ADominionAICharacter::ProcessEvent"},
    {"/Script/Dominion", "HealthComponent", "UHealthComponent::ProcessEvent"},
};

// A capability may only claim "ok" when a hook is actually installed - a resolved symbol proves
// nothing (lane L6ab's finding F6).
void RefreshKillCapability() {
    size_t ai = 0, health = 0, progress = 0;
    for (auto& t : g_targets) {
        std::string l = t.label;
        if (l.rfind("ADominionAICharacter", 0) == 0) ai = t.hooked;
        else if (l.rfind("UHealthComponent", 0) == 0) health = t.hooked;
        else if (l.rfind("UProgressComponent", 0) == 0) progress = t.hooked;
    }
    if (ai || health) {
        std::string how = "ProcessEvent on " + std::to_string(ai) + " AI-character and " +
                          std::to_string(health) + " health-component vtable(s) (BP_OnDeath / OnDeathEvent)";
        if (g_kill.emitted.load() == 0) how += "; not yet observed firing";
        Ok("killEvents", g_kill, how);
    } else {
        Degrade("killEvents", g_kill,
                "no live ADominionAICharacter or UHealthComponent to hook yet (AI streams in around a "
                "player); retried every 2 s. UProgressComponent::OnAIKilled is hooked (" +
                    std::to_string(progress) + " vtable(s)) but the game calls it as a plain C++ delegate, "
                    "so ProcessEvent never sees it");
    }
}

void SweepProcessEventTargets() {
    for (auto& t : g_targets) {
        if (!t.resolved) {
            t.resolved = Reflect::FindObjectByPath(t.pkg, t.cls);
            if (!t.resolved) t.resolved = FindStructByName(t.cls);
            if (!t.resolved) continue;
        }
        std::vector<void*> objs;
        if (!Reflect::GetObjectsOfClass(t.resolved, objs, true)) continue;
        for (void* o : objs) {
            if (HookObjectProcessEvent(t.label, o)) t.hooked++;
        }
    }
}

// ---------------------------------------------------------------------------------------------
// death fallback: health edge per connected player


void PollHealthEdges() {
    std::vector<std::pair<void*, Ident>> conns;
    {
        Guard g(g_connLock);
        for (auto& c : g_conns)
            if (c.announced && !c.left) conns.push_back({c.controller, c.id});
    }
    for (auto& c : conns) {
        void* st = PlayerStateOf(c.first);
        if (!st) continue;
        void* pawn = nullptr;
        int32_t pawnOff = PropOffOf(st, "PawnPrivate");
        if (pawnOff < 0 || !ReadAt(st, pawnOff, pawn) || !ValidObject(pawn)) continue;
        if (!g_clsHealthComponent) continue;  // without the class pointer we would have to compare names
        std::vector<void*> comps;
        if (!Reflect::GetObjectsWithOuter(pawn, comps, true)) continue;
        for (void* comp : comps) {
            if (!ValidObject(comp) || !Reflect::IsA(comp, g_clsHealthComponent)) continue;
            int32_t hpOff = PropOffOf(comp, "AuthoritativeHealth");
            int32_t maxOff = PropOffOf(comp, "MaxHealth");
            float hp = 0.0f, maxHp = 0.0f;
            if (hpOff < 0 || !ReadAt(comp, hpOff, hp)) break;
            if (maxOff >= 0) ReadAt(comp, maxOff, maxHp);
            if (!(maxHp > 0.0f)) break;              // health not replicated yet
            Live now = hp > 0.0f ? Live::Alive : Live::Dead;
            Live prev = Live::Unknown;
            {
                Guard g(g_healthLock);
                auto it = g_liveness.find(c.second.gameId);
                if (it != g_liveness.end()) prev = it->second;
                g_liveness[c.second.gameId] = now;
            }
            // Only a genuine alive -> dead transition is a death; the first sample just seeds.
            if (now == Live::Dead && prev == Live::Alive)
                EmitDeath(c.second, 0, 0, 0, false, "", "", "health-edge");
            break;
        }
    }
}

// ---------------------------------------------------------------------------------------------
// log tail

std::string g_logPath;
int g_logFd = -1;
uint64_t g_logInode = 0;
off_t g_logOffset = 0;
std::string g_logPartial;
std::atomic<uint64_t> g_logDropped{0};
uint64_t g_logStartMs = 0;  // Init() time; a rotation inside the grace window is the game's own
                            // boot rotation, so we attach at EOF instead of replaying the boot log.
const uint64_t kBootRotationGraceMs = 180000;
const size_t kMaxLogPerCycle = 120;
const size_t kMaxReadPerCycle = 512 * 1024;

// Additional redaction on top of common.cpp's Redact(): the join/login URL carries the world
// password base64-encoded as ?p=<...>.
// `PlayerChar entered world [Account[XP:<puid>] Character Name[<name>] Guid[DCG:<guid>] Type[0]]`
// is the only place the server prints the character name, and it arrives before the replicated
// property is readable - so the log tail doubles as a name source.
void NoteJoinLine(const std::string& line) {
    static const char* kMark = "PlayerChar entered world";
    if (line.find(kMark) == std::string::npos) return;
    size_t a = line.find("Account[XP:");
    size_t n = line.find("Character Name[");
    if (a == std::string::npos || n == std::string::npos) return;
    a += strlen("Account[XP:");
    size_t aEnd = line.find(']', a);
    n += strlen("Character Name[");
    size_t nEnd = line.find(']', n);
    if (aEnd == std::string::npos || nEnd == std::string::npos) return;
    std::string id = line.substr(a, aEnd - a);
    std::string name = line.substr(n, nEnd - n);
    if (!LooksLikePuid(id) || name.empty() || name.size() > 64) return;
    for (auto& c : id) c = (char)tolower((unsigned char)c);
    ::state::NoteCharacterName(id, name);
}

std::string RedactLogLine(const std::string& in) {
    std::string s = Redact(in);
    size_t pos = 0;
    while ((pos = s.find("?p=", pos)) != std::string::npos) {
        size_t end = pos + 3;
        while (end < s.size() && s[end] != '?' && s[end] != ' ' && s[end] != '\t') end++;
        s.replace(pos + 3, end - (pos + 3), "<redacted>");
        pos += 3 + 10;
    }
    return s;
}

bool IsNoise(const std::string& line) {
    static const char* kNoise[] = {"LogRedpointEOS: Verbose", "LogEOSHTTP", "SendBackendEvent", "LogEOSAnalytics",
                                   "LogEOSNetworkAuth", "LogRedpointEOSHTTP"};
    for (auto* n : kNoise)
        if (line.find(n) != std::string::npos) return true;
    return line.empty();
}

std::string DefaultLogPath() {
    std::string cfg = ConfigValue("TAKARO_LOG_PATH", "logPath", "");
    if (!cfg.empty()) return cfg;
    // <exe dir> = <game>/Binaries/Linux -> <game>/Saved/Logs/RSDragonwilds.log
    std::string dir = ExeDir();
    size_t bin = dir.rfind("/Binaries/");
    if (bin != std::string::npos) return dir.substr(0, bin) + "/Saved/Logs/RSDragonwilds.log";
    return dir + "/../../Saved/Logs/RSDragonwilds.log";
}

void OpenLog(bool fromEof) {
    if (g_logFd >= 0) { close(g_logFd); g_logFd = -1; }
    g_logFd = open(g_logPath.c_str(), O_RDONLY);
    if (g_logFd < 0) return;
    struct stat stt;
    if (fstat(g_logFd, &stt) == 0) {
        g_logInode = (uint64_t)stt.st_ino;
        g_logOffset = fromEof ? stt.st_size : 0;
    }
    g_logPartial.clear();
    PluginLog("events: log tail attached to %s (inode %llu, offset %lld)", g_logPath.c_str(),
              (unsigned long long)g_logInode, (long long)g_logOffset);
}

void PollLog() {
    if (g_logPath.empty()) return;
    struct stat stt;
    if (stat(g_logPath.c_str(), &stt) != 0) return;
    if (g_logFd < 0 || (uint64_t)stt.st_ino != g_logInode || stt.st_size < g_logOffset) {
        // Rotated or truncated. Mid-run that means "read the new file from the start"; during the
        // first three minutes it is the server opening its own log after our constructor ran, and
        // replaying the whole boot log would flood the ring buffer.
        bool boot = g_logStartMs && NowMs() - g_logStartMs < kBootRotationGraceMs && g_log.emitted.load() == 0;
        OpenLog(boot);
        if (g_logFd < 0) return;
    }
    std::vector<char> buf(65536);
    size_t emitted = 0, read_total = 0;
    while (read_total < kMaxReadPerCycle) {
        ssize_t n = pread(g_logFd, buf.data(), buf.size(), g_logOffset);
        if (n <= 0) break;
        g_logOffset += n;
        read_total += (size_t)n;
        g_logPartial.append(buf.data(), (size_t)n);
        size_t start = 0, nl;
        while ((nl = g_logPartial.find('\n', start)) != std::string::npos) {
            std::string line = g_logPartial.substr(start, nl - start);
            start = nl + 1;
            while (!line.empty() && (line.back() == '\r')) line.pop_back();
            NoteJoinLine(line);
            if (IsNoise(line)) continue;
            if (emitted >= kMaxLogPerCycle) { g_logDropped++; continue; }
            PluginState::Get().EmitEvent("log", "{\"msg\":" + JsonStr(RedactLogLine(line)) + "}");
            emitted++;
        }
        g_logPartial.erase(0, start);
        if (g_logPartial.size() > 1 << 20) g_logPartial.clear();
        if ((size_t)n < buf.size()) break;
    }
    if (emitted) g_log.emitted += emitted;
}

// ---------------------------------------------------------------------------------------------
// init

std::atomic<bool> g_bootDone{false};

// Names the step the housekeeping job is in, so that the last plugin.log line before a crash says
// whether the fault was ours (the server died once inside FName::ToString before this existed).
void Phase(const char* p) {
    if (DebugEnabled()) PluginLog("events: housekeep phase %s", p);
}

void GameThreadInit() {
    g_clsUObject = Reflect::StaticClass("UObject::StaticClass");
    g_clsUClass = Reflect::StaticClass("UClass::StaticClass");
    g_clsPlayerState = Reflect::FindObjectByPath("/Script/Engine", "PlayerState");
    g_clsHealthComponent = Reflect::FindObjectByPath("/Script/Dominion", "HealthComponent");
    PluginLog("events: class pointers UObject=%p UClass=%p PlayerState=%p HealthComponent=%p", g_clsUObject,
              g_clsUClass, g_clsPlayerState, g_clsHealthComponent);
    g_processEvent = (FnProcessEvent)(uintptr_t)Sym::Addr("UObject::ProcessEvent");
    g_eosToString = (FnEosToString)(uintptr_t)Sym::Addr("FUniqueNetIdEOS::ToString");
    uint64_t ztv = DynSymAddr("_ZTV15FUniqueNetIdEOS");
    g_eosVptr = ztv ? (void*)(uintptr_t)(ztv + 2 * sizeof(void*)) : nullptr;

    g_fnChat = Reflect::MakeName("Server_SendChatMessage");
    g_fnDeath = Reflect::MakeName("Client_SendDeathEventTelemetry");
    g_fnAiKilled = Reflect::MakeName("OnAIKilled");
    g_fnPlayerEvent = Reflect::MakeName("Client_ReceivePlayerEvent");
    g_fnBpOnDeath = Reflect::MakeName("BP_OnDeath");
    g_fnOnDeathEvent = Reflect::MakeName("OnDeathEvent");
    g_clsAiCharacter = Reflect::FindObjectByPath("/Script/Dominion", "DominionAICharacter");
    g_fnNamesReady = g_fnChat.Comparison != 0;

    g_chatDataStruct = FindStructByName("ChatMessageData");
    g_damageStruct = FindStructByName("DominionDamageEvent");
    g_msgBodyOff = g_chatDataStruct ? PropOff(g_chatDataStruct, "MessageBody") : -1;

    SweepProcessEventTargets();
    HookLivePreLogin();

    // capabilities
    if (!g_fnNamesReady)
        Degrade("chatEvents", g_chat, "the engine name pool has no 'Server_SendChatMessage'");
    else if (g_msgBodyOff < 0)
        Degrade("chatEvents", g_chat, "FChatMessageData::MessageBody offset not found by reflection");
    else if (!g_targets[0].hooked)
        Degrade("chatEvents", g_chat, "no live UPlayerChatComponent to hook yet; retried every 2 s");
    else
        Ok("chatEvents", g_chat, "ProcessEvent slot hook on " + std::to_string(g_targets[0].hooked) + " chat vtable(s)");

    if (!g_damageStruct)
        Degrade("deathEvents", g_death, "FDominionDamageEvent not found; falling back to the health edge");
    else
        Ok("deathEvents", g_death, "Client_SendDeathEventTelemetry via ProcessEvent + health-edge fallback");

    RefreshKillCapability();

    if (!g_eosVptr || !g_eosToString)
        Degrade("joinLeaveEvents", g_join, "FUniqueNetIdEOS::ToString or its vtable is unavailable");
    g_bootDone = true;
}

}  // namespace

// ---------------------------------------------------------------------------------------------

void Events::Init() {
    auto& st = PluginState::Get();
    for (const char* cap : {"logEvents", "joinLeaveEvents", "connectEvents", "chatEvents", "deathEvents", "killEvents"})
        st.SetCapability(cap, "degraded", "starting");

    // 1. game-mode hooks (static vtables; safe before the world exists).
    size_t slot = SIZE_MAX;
    std::string tables;
    g_origPostLogin = (FnPostLogin)(uintptr_t)Sym::Addr("ADominionGameMode::PostLogin");
    size_t n = SwapEverywhere("ADominionGameMode::PostLogin", (void*)&DetourPostLogin, slot, tables);
    g_join.hooked = n > 0;
    if (!n) Degrade("joinLeaveEvents", g_join, "ADominionGameMode::PostLogin is in no exported vtable");

    g_origPreLogout = (FnLogout)(uintptr_t)Sym::Addr("ADominionGameMode::PreLogout");
    size_t nPre = SwapEverywhere("ADominionGameMode::PreLogout", (void*)&DetourPreLogout, slot, tables);
    g_origLogout = (FnLogout)(uintptr_t)Sym::Addr("AGameModeBase::Logout");
    size_t nOut = SwapEverywhere("AGameModeBase::Logout", (void*)&DetourLogout, slot, tables);
    g_origLogoutGm = (FnLogout)(uintptr_t)Sym::Addr("AGameMode::Logout");
    size_t nOutGm = SwapEverywhere("AGameMode::Logout", (void*)&DetourLogoutGm, slot, tables);
    nOut += nOutGm;

    // The path that actually fires on this build (see DetourNetCleanup).
    g_origNetCleanup = (FnNetCleanup)(uintptr_t)Sym::Addr("ADominionPlayerController::OnNetCleanup");
    size_t nNet = SwapEverywhere("ADominionPlayerController::OnNetCleanup", (void*)&DetourNetCleanup, slot, tables);
    g_origDestroyed = (FnDestroyed)(uintptr_t)Sym::Addr("ADominionPlayerController::Destroyed");
    size_t nDest = SwapEverywhere("ADominionPlayerController::Destroyed", (void*)&DetourControllerDestroyed, slot, tables);
    PluginLog("events: disconnect hooks OnNetCleanup x%zu Destroyed x%zu", nNet, nDest);
    nOut += nNet + nDest;
    g_leave.hooked = (nPre + nOut) > 0;
    PluginLog("events: gamemode hooks PostLogin x%zu PreLogout x%zu Logout x%zu (AGameMode::Logout x%zu)", n, nPre,
              nOut, nOutGm);
    if (n && (nPre || nOut)) {
        std::string how = "PostLogin x" + std::to_string(n) + ", PreLogout x" + std::to_string(nPre) + ", Logout x" +
                          std::to_string(nOut);
        Ok("joinLeaveEvents", g_join, how);
        PluginState::Get().SetCapability("connectEvents", "ok", "");
        Note(g_leave, how);
    } else if (!nPre && !nOut) {
        Degrade("joinLeaveEvents", g_join, "no logout hook could be installed");
    }

    // 1b. live ban enforcement: refuse a banned login in PreLogin, without a restart.
    ::state::BansLoad();
    g_origPreLoginDom = (FnPreLogin)(uintptr_t)Sym::Addr("ADominionGameMode::PreLogin");
    size_t nDom = SwapEverywhere("ADominionGameMode::PreLogin", (void*)&DetourPreLoginDom, slot, tables);
    g_origPreLoginBase = (FnPreLogin)(uintptr_t)Sym::Addr("AGameModeBase::PreLogin");
    size_t nBase = SwapEverywhere("AGameModeBase::PreLogin", (void*)&DetourPreLoginBase, slot, tables);
    g_preLoginHooked = nDom > 0;  // only the live game mode's own slot counts; see HookLivePreLogin
    PluginLog("events: PreLogin hooks ADominionGameMode x%zu AGameModeBase x%zu (live ban enforcement %s)", nDom,
              nBase, g_preLoginHooked.load() ? "on" : "OFF");

    // 2. log tail, from EOF so a restart does not replay the whole file.
    g_logPath = DefaultLogPath();
    g_logStartMs = NowMs();
    OpenLog(true);
    if (g_logFd < 0) Degrade("logEvents", g_log, "cannot open " + g_logPath);
    else {
        g_log.hooked = true;
        Ok("logEvents", g_log, "tailing " + g_logPath + " from EOF, rotation-aware, redacted");
    }

    // 3. everything that needs live UObjects runs on the game thread, once it ticks.
    PluginLog("events: init done (UObject work deferred to the game thread)");
}

void Events::Housekeep() {
    try {
        PollLog();
    } catch (...) {
    }
    if (!Reflect::Validated()) return;  // never touch UObjects before the layout is confirmed
    GameThread::Run(
        [] {
            try {
                if (!g_bootDone) { Phase("boot"); GameThreadInit(); }
                Phase("sweep");
                SweepProcessEventTargets();
                HookLivePreLogin();
                Phase("joins");
                ResolvePendingJoins();
                Phase("reap");
                ReapGoneConnections();
                Phase("health");
                PollHealthEdges();
                Phase("idle");
            } catch (...) {
                PluginLog("events: housekeeping job threw");
            }
        },
        5000);
    if (g_bootDone) GameThread::Run([] { RefreshKillCapability(); }, 2000);
    // A chat vtable may only appear once a player connects; flip the capability when it does.
    if (g_bootDone && g_targets[0].hooked && PluginState::Get().Capability("chatEvents") != "ok" && g_msgBodyOff >= 0)
        Ok("chatEvents", g_chat, "ProcessEvent slot hook on " + std::to_string(g_targets[0].hooked) + " chat vtable(s)");
}

bool Events::BanEnforcementLive() { return g_preLoginHooked.load(); }

std::string Events::DiagnosticsJson() {
    auto one = [](const char* name, SourceStat& s, bool hooked) {
        return "{\"source\":" + JsonStr(name) + ",\"hooked\":" + (hooked ? "true" : "false") +
               ",\"fired\":" + std::to_string(s.fired.load()) + ",\"emitted\":" + std::to_string(s.emitted.load()) +
               ",\"note\":" + JsonStr(NoteOf(s)) + "}";
    };
    size_t conns;
    {
        Guard g(g_connLock);
        conns = g_conns.size();
    }
    std::string o = "{\"sources\":[";
    o += one("player-connected", g_join, g_join.hooked.load()) + ",";
    o += one("player-disconnected", g_leave, g_leave.hooked.load()) + ",";
    o += one("chat-message", g_chat, g_peCount.load() > 0) + ",";
    o += one("player-death", g_death, g_peCount.load() > 0) + ",";
    o += one("entity-killed", g_kill, g_peCount.load() > 0) + ",";
    o += one("log", g_log, g_log.hooked.load());
    o += "],\"banEnforcement\":{\"preLoginHooked\":" + std::string(g_preLoginHooked.load() ? "true" : "false") +
         ",\"refusals\":" + std::to_string(g_banRefusals.load()) + ",\"bansFile\":" + JsonStr(::state::BansPath()) +
         ",\"pluginBans\":" + std::to_string(::state::BanList().size()) + "}";
    o += ",\"processEventVTables\":" + std::to_string(g_peCount.load());
    o += ",\"trackedConnections\":" + std::to_string(conns);
    o += ",\"logPath\":" + JsonStr(g_logPath) + ",\"logDropped\":" + std::to_string(g_logDropped.load());
    o += ",\"classesHooked\":[";
    for (size_t i = 0; i < sizeof(g_targets) / sizeof(g_targets[0]); i++)
        o += std::string(i ? "," : "") + "{\"class\":" + JsonStr(g_targets[i].cls) + ",\"resolved\":" +
             (g_targets[i].resolved ? "true" : "false") + ",\"vtables\":" + std::to_string(g_targets[i].hooked) + "}";
    o += "]}";
    return o;
}
