#include "players.h"

#include "events_parse.h"
#include "gamethread.h"
#include "objpool.h"
#include "perf.h"
#include "reflect.h"
#include "resolve.h"
#include "state.h"

#include <atomic>
#include <cstdio>
#include <cstring>

namespace {

constexpr size_t kMaxPlayers = 128;
// How long a logged-out entry is kept so a death that arrives a frame after Logout can still be
// attributed, and so /players can answer a `getPlayer` for someone who just left.
constexpr uint64_t kLingerMs = 60'000;

Mutex g_lock;
std::vector<Players::Entry> g_players;
uint64_t g_generation = 0;
uint64_t g_lastRefreshMs = 0;
uint64_t g_refreshes = 0, g_refreshErrors = 0;
// LANE L2c diagnostics. `pawnReresolves` counts how often the controller's live pawn differed from the
// one the registry had cached — i.e. how often the stale-pawn bug would have fired.
// Atomic because ReadLocation runs off the registry lock (NoteLogin and Refresh both call it before
// taking it) while /health reads them from an HTTP thread.
std::atomic<uint64_t> g_pawnReresolves{0}, g_pawnsCollected{0};
std::atomic<uint64_t> g_identifyByOwner{0}, g_pawnAdoptions{0};
std::atomic<uint64_t> g_originPositionsRejected{0}, g_positionsNone{0};
bool g_warmed = false;
std::string g_warmNote;

// ---- the property plan -------------------------------------------------------------------------
// Names only. Offsets are looked up per class, by name, through U::PropOffset — so a build that
// renames one of these degrades that one field instead of reading a wrong address.
const char* kPersistenceComponentNames[] = {"m_PlayerPersistenceComponent", "m_PlayerControllerPersistenceComponent",
                                            "m_PersistenceComponent"};
const char* kPawnNames[] = {"Pawn", "PawnPrivate"};
const char* kPlayerStateNames[] = {"PlayerState", "PlayerStatePrivate"};

struct IdPlan {
    const char* prop;
    uint64_t Players::Entry::*field;
};
const IdPlan kPersistenceIds[] = {
    {"m_AccountID", &Players::Entry::accountId},
    {"m_PlayerStateUniqueID", &Players::Entry::playerStateId},
    {"m_PlayerControllerUniqueID", &Players::Entry::controllerId},
    {"m_PlayerCharacterUniqueID", &Players::Entry::pawnId},
};

/// Reads the one 64-bit value out of an 8-byte id struct. The struct's inner layout is NOT reflected
/// (a StructProperty's inner UScriptStruct is not on the FField chain), so the only thing the live
/// process proves is that the field is 8 bytes wide — the offsets of the four consecutive ids differ
/// by exactly 8. Reading it as one unsigned 64-bit integer is therefore a HYPOTHESIS, and it is
/// falsifiable the moment a player joins: the value must equal the Postgres row. A value that is 0
/// is reported as absent.
uint64_t ReadIdStruct(const void* obj, int32_t off) {
    if (!obj || off < 0) return 0;
    bool ok = false;
    uint64_t v = U::ReadU64(obj, (uint32_t)off, &ok);
    return ok ? v : 0;
}

/// GAME THREAD. Fills whatever of the identity is readable right now. Returns a short description of
/// what worked, which /health and the report quote verbatim.
std::string ReadIdentity(Players::Entry& e) {
    std::string how;
    void* controller = e.controller;
    if (!controller || !LooksLikeUObject(controller)) return "controller pointer failed LooksLikeUObject";
    void* cls = U::ClassOf(controller);
    if (!cls) return "controller class unreadable";

    // (1) the account id straight off the controller — `DunePlayerControllerBase.m_DatabaseAccountId`.
    int32_t offAcct = U::PropOffset(cls, "m_DatabaseAccountId");
    if (offAcct >= 0) {
        uint64_t v = ReadIdStruct(controller, offAcct);
        if (v) {
            e.accountId = v;
            char b[96];
            snprintf(b, sizeof b, "m_DatabaseAccountId@0x%x", (unsigned)offAcct);
            how += b;
        }
    }

    // (2) the persistence component: all four ids plus the character name.
    auto pc = U::FirstProp(cls, kPersistenceComponentNames,
                           sizeof kPersistenceComponentNames / sizeof kPersistenceComponentNames[0]);
    if (pc.second >= 0) {
        void* comp = U::ReadPtr(controller, (uint32_t)pc.second);
        if (comp && LooksLikeUObject(comp)) {
            void* ccls = U::ClassOf(comp);
            std::string cname = U::ClassNameOf(comp);
            // Validate before touching: the component must actually be a persistence component, or
            // its property offsets mean nothing.
            if (ccls && (cname.find("Persistence") != std::string::npos)) {
                for (const IdPlan& p : kPersistenceIds) {
                    int32_t off = U::PropOffset(ccls, p.prop);
                    uint64_t v = ReadIdStruct(comp, off);
                    if (v) e.*(p.field) = v;
                }
                int32_t offName = U::PropOffset(ccls, "m_CharacterName");
                if (offName >= 0) {
                    std::string n = U::ReadFString(comp, (uint32_t)offName, 64);
                    if (!n.empty()) e.characterName = n;
                }
                if (!how.empty()) how += " + ";
                char b[192];
                snprintf(b, sizeof b, "%s@0x%x -> %s", pc.first, (unsigned)pc.second, cname.c_str());
                how += b;
            } else if (!cname.empty()) {
                if (!how.empty()) how += " + ";
                how += "persistence component is a " + cname + " (no Persistence in the name; refused)";
            }
        }
    }

    // (3) APlayerState: the engine's own name and per-session id. Not a DB key; a corroboration.
    auto ps = U::FirstProp(cls, kPlayerStateNames, sizeof kPlayerStateNames / sizeof kPlayerStateNames[0]);
    if (ps.second >= 0) {
        void* state = U::ReadPtr(controller, (uint32_t)ps.second);
        if (state && LooksLikeUObject(state)) {
            e.playerState = state;
            void* scls = U::ClassOf(state);
            int32_t offNm = U::PropOffset(scls, "PlayerNamePrivate");
            if (offNm >= 0) {
                std::string n = U::ReadFString(state, (uint32_t)offNm, 64);
                if (!n.empty()) e.playerName = n;
            }
            int32_t offPid = U::PropOffset(scls, "PlayerId");
            if (offPid >= 0) e.playerId = (int32_t)U::ReadU32(state, (uint32_t)offPid);
        }
    }

    // (4) the pawn, for the live location. Absent at PostLogin time on most builds.
    auto pw = U::FirstProp(cls, kPawnNames, sizeof kPawnNames / sizeof kPawnNames[0]);
    if (pw.second >= 0) {
        void* pawn = U::ReadPtr(controller, (uint32_t)pw.second);
        if (pawn && LooksLikeUObject(pawn)) e.pawn = pawn;
    }
    if (!e.pawn && e.playerState) {
        int32_t off = U::PropOffset(U::ClassOf(e.playerState), "PawnPrivate");
        if (off >= 0) {
            void* pawn = U::ReadPtr(e.playerState, (uint32_t)off);
            if (pawn && LooksLikeUObject(pawn)) e.pawn = pawn;
        }
    }
    if (how.empty()) how = "no identity property on " + U::ClassNameOf(controller);
    return how;
}

/// GAME THREAD. The controller's CURRENT pawn, read live rather than trusted from the cache.
///
/// LANE L2c: `Entry::pawn` was only ever filled when it was null (`Refresh()` re-read the identity
/// solely for missing fields), so after a respawn it kept pointing at the previous life's pawn. That
/// single staleness caused BOTH of the bugs this lane exists for: the victim of a death stopped being
/// recognised as a player, and the location fell through to the PlayerState at the world origin (the
/// sidecar's 24x `plugin-origin-rejected:playerState`). A pawn is a PER-LIFE object on this build;
/// treating it as stable identity was the mistake.
void* ResolveCurrentPawn(const Players::Entry& e) {
    if (e.controller && LooksLikeUObject(e.controller)) {
        void* cls = U::ClassOf(e.controller);
        if (cls) {
            auto pw = U::FirstProp(cls, kPawnNames, sizeof kPawnNames / sizeof kPawnNames[0]);
            if (pw.second >= 0) {
                void* p = U::ReadPtr(e.controller, (uint32_t)pw.second);
                if (p && LooksLikeUObject(p)) return p;
            }
        }
    }
    // The PlayerState's own copy covers the frame in which the controller's has not been written yet.
    if (e.playerState && LooksLikeUObject(e.playerState)) {
        int32_t off = U::PropOffset(U::ClassOf(e.playerState), "PawnPrivate");
        if (off >= 0) {
            void* p = U::ReadPtr(e.playerState, (uint32_t)off);
            if (p && LooksLikeUObject(p)) return p;
        }
    }
    return nullptr;
}

/// One actor's world transform, entirely by reflected offsets:
/// AActor::RootComponent -> USceneComponent::RelativeLocation / RelativeRotation.
struct Spatial {
    U::Vec pos;
    double pitch = 0, yaw = 0;
    bool ok = false;
};
Spatial ReadActorSpatial(void* actor) {
    Spatial s;
    if (!actor || !LooksLikeUObject(actor)) return s;
    int32_t offRoot = U::PropOffsetOf(actor, "RootComponent");
    if (offRoot < 0) return s;
    void* root = U::ReadPtr(actor, (uint32_t)offRoot);
    if (!root || !LooksLikeUObject(root)) return s;
    void* rcls = U::ClassOf(root);
    int32_t offLoc = U::PropOffset(rcls, "RelativeLocation");
    if (offLoc < 0) return s;
    if (!U::ReadVec(root, (uint32_t)offLoc, s.pos)) return s;
    s.ok = true;
    int32_t offRot = U::PropOffset(rcls, "RelativeRotation");
    if (offRot >= 0) {
        U::Vec r;
        if (U::ReadVec(root, (uint32_t)offRot, r)) {
            s.pitch = r.x;
            s.yaw = r.y;
        }
    }
    return s;
}

/// GAME THREAD. Live location, with the pawn RE-RESOLVED every call and the source decided by the
/// pure, unit-tested rule in events_parse.cpp (pawn always wins; a PlayerState position is only
/// reported when it is not at the world origin).
bool ReadLocation(Players::Entry& e) {
    void* pawn = ResolveCurrentPawn(e);
    if (pawn) {
        if (e.pawn && pawn != e.pawn) g_pawnReresolves++;
        e.pawn = pawn;
    } else if (e.pawn && !LooksLikeUObject(e.pawn)) {
        // The previous life's pawn has been collected. Keeping the pointer would only invite a read
        // of freed memory and a pointer match against a recycled address.
        e.pawn = nullptr;
        g_pawnsCollected++;
    }

    Spatial p = ReadActorSpatial(e.pawn);
    Spatial ps;
    if (!p.ok) ps = ReadActorSpatial(e.playerState);
    bool psAtOrigin = ps.ok && EventsParse::IsOriginPosition(ps.pos.x, ps.pos.y, ps.pos.z);
    EventsParse::PositionSource src = EventsParse::DecidePositionSource(p.ok, ps.ok, psAtOrigin);
    e.positionSource = EventsParse::PositionSourceName(src);
    if (src == EventsParse::PositionSource::None) {
        // Honest: no position at all, rather than a syntactically valid (0,0,0) the sidecar has to
        // second-guess. `havePosition` is cleared so a stale one cannot linger either.
        e.havePosition = false;
        if (psAtOrigin) g_originPositionsRejected++;
        else g_positionsNone++;
        return false;
    }
    const Spatial& c = (src == EventsParse::PositionSource::Pawn) ? p : ps;
    e.position = c.pos;
    e.pitch = c.pitch;
    e.yaw = c.yaw;
    e.havePosition = true;
    return true;
}

Players::Entry* FindByController(void* controller) {
    for (auto& e : g_players)
        if (e.controller == controller) return &e;
    return nullptr;
}

std::string IdJson(const char* key, uint64_t v) {
    std::string o = std::string(",\"") + key + "\":";
    // 0 is "not readable", NOT an id. Serialising it as 0 would invite a join against actor row 0.
    if (!v) return o + "null";
    char b[32];
    snprintf(b, sizeof b, "%llu", (unsigned long long)v);
    return o + b;
}

std::string EntryJson(const Players::Entry& e, uint64_t now) {
    std::string o = "{\"ref\":" + JsonStr(e.Ref());
    o += ",\"characterName\":" + (e.characterName.empty() ? (e.playerName.empty() ? std::string("null") : JsonStr(e.playerName))
                                                          : JsonStr(e.characterName));
    o += ",\"playerName\":" + (e.playerName.empty() ? std::string("null") : JsonStr(e.playerName));
    o += IdJson("accountId", e.accountId);
    o += IdJson("playerStateId", e.playerStateId);
    o += IdJson("playerControllerId", e.controllerId);
    o += IdJson("playerPawnId", e.pawnId);
    o += ",\"sessionPlayerId\":" + std::to_string(e.playerId);
    o += ",\"online\":" + std::string(e.online ? "true" : "false");
    if (e.havePosition) {
        o += ",\"position\":{\"x\":" + JsonNum(e.position.x) + ",\"y\":" + JsonNum(e.position.y) + ",\"z\":" +
             JsonNum(e.position.z) + "}";
        o += ",\"pitch\":" + JsonNum(e.pitch) + ",\"yaw\":" + JsonNum(e.yaw);
    } else {
        o += ",\"position\":null";
    }
    o += ",\"positionSource\":" + JsonStr(e.positionSource);
    o += ",\"generation\":" + std::to_string(e.generation);
    o += ",\"ageMs\":" + std::to_string(e.lastUpdateMs ? now - e.lastUpdateMs : 0);
    o += ",\"identityHow\":" + JsonStr(e.identityHow);
    return o + "}";
}

}  // namespace

std::string Players::Entry::Ref() const {
    char b[32];
    if (accountId) {
        snprintf(b, sizeof b, "acct:%llu", (unsigned long long)accountId);
        return b;
    }
    if (playerStateId) {
        snprintf(b, sizeof b, "ps:%llu", (unsigned long long)playerStateId);
        return b;
    }
    if (!characterName.empty()) return "name:" + characterName;
    if (!playerName.empty()) return "name:" + playerName;
    snprintf(b, sizeof b, "session:%d", playerId);
    return b;
}

void Players::Init() {
    // Warm the property offsets we need, so the PostLogin detour never walks a property list on the
    // game thread. This runs on the init/housekeeping thread: UClass FField chains are static data.
    std::string note;
    struct Warm {
        const char* cls;
        const char* props[8];
    };
    const Warm kWarm[] = {
        {"DunePlayerController", {"m_PlayerPersistenceComponent", nullptr}},
        {"DunePlayerControllerBase", {"m_DatabaseAccountId", nullptr}},
        {"DunePlayerControllerPersistenceComponent",
         {"m_AccountID", "m_PlayerStateUniqueID", "m_PlayerControllerUniqueID", "m_PlayerCharacterUniqueID",
          "m_CharacterName", nullptr}},
        {"PlayerState", {"PlayerNamePrivate", "PlayerId", "PawnPrivate", nullptr}},
        {"Controller", {"Pawn", "PlayerState", nullptr}},
        {"Actor", {"RootComponent", nullptr}},
        {"SceneComponent", {"RelativeLocation", "RelativeRotation", nullptr}},
    };
    size_t ok = 0, missing = 0;
    for (const Warm& w : kWarm) {
        void* cls = Classes::ByName(w.cls);
        if (!cls) {
            note += std::string(note.empty() ? "" : "; ") + "no live UClass " + w.cls;
            continue;
        }
        for (size_t i = 0; w.props[i]; i++) {
            if (U::PropOffset(cls, w.props[i]) >= 0) {
                ok++;
            } else {
                missing++;
                note += std::string(note.empty() ? "" : "; ") + w.cls + "." + w.props[i] + " absent";
            }
        }
    }
    Guard g(g_lock);
    g_warmed = true;
    char b[128];
    snprintf(b, sizeof b, "%zu identity/location offsets resolved, %zu absent", ok, missing);
    g_warmNote = std::string(b) + (note.empty() ? "" : " (" + note + ")");
    PluginLog("players: %s", g_warmNote.c_str());
}

void Players::NoteLogin(void* controller) {
    if (!controller) return;
    Entry e;
    e.controller = controller;
    e.firstSeenMs = NowMs();
    e.identityHow = ReadIdentity(e);
    ReadLocation(e);
    e.lastUpdateMs = NowMs();
    Guard g(g_lock);
    Entry* existing = FindByController(controller);
    if (existing) {
        // A reconnect onto a recycled controller pointer: replace, keep the first-seen stamp.
        e.firstSeenMs = existing->firstSeenMs;
        *existing = e;
        existing->generation = ++g_generation;
        return;
    }
    if (g_players.size() >= kMaxPlayers) return;
    e.generation = ++g_generation;
    g_players.push_back(e);
}

void Players::NoteLogout(void* controller) {
    Guard g(g_lock);
    Entry* e = FindByController(controller);
    if (!e) return;
    e->online = false;
    e->lastUpdateMs = NowMs();
    e->generation = ++g_generation;
}

void Players::Refresh() {
    Perf::Scope scope("players.snapshot");
    std::vector<Entry> copy;
    {
        Guard g(g_lock);
        copy = g_players;
    }
    for (auto& e : copy) {
        if (!e.online) continue;
        // Anything still missing is re-read: the persistence component is populated some frames after
        // PostLogin, and the pawn only exists once the character has spawned.
        if (!e.accountId || !e.playerStateId || !e.controllerId || !e.pawnId || e.characterName.empty() || !e.pawn)
            e.identityHow = ReadIdentity(e);
        ReadLocation(e);
        e.lastUpdateMs = NowMs();
    }
    Guard g(g_lock);
    for (auto& fresh : copy) {
        Entry* cur = FindByController(fresh.controller);
        if (!cur) continue;
        bool moved = !cur->havePosition || cur->position.x != fresh.position.x || cur->position.y != fresh.position.y ||
                     cur->position.z != fresh.position.z;
        bool wasOnline = cur->online;
        fresh.online = wasOnline;
        fresh.generation = moved ? ++g_generation : cur->generation;
        *cur = fresh;
    }
    g_lastRefreshMs = NowMs();
    g_refreshes++;
}

void Players::Expire() {
    uint64_t now = NowMs();
    Guard g(g_lock);
    for (size_t i = 0; i < g_players.size();) {
        if (!g_players[i].online && now - g_players[i].lastUpdateMs > kLingerMs) {
            g_players.erase(g_players.begin() + (long)i);
        } else {
            i++;
        }
    }
}

bool Players::IsTrackedOnline(void* controller) {
    if (!controller) return false;
    Guard g(g_lock);
    Entry* e = FindByController(controller);
    return e && e->online;
}

bool Players::Identify(void* actor, Entry& out) {
    if (!actor) return false;
    Guard g(g_lock);
    for (const auto& e : g_players) {
        if (e.controller == actor || e.playerState == actor || e.pawn == actor) {
            out = e;
            return true;
        }
    }
    return false;
}

bool Players::IdentifyActor(void* actor, Entry& out, const char** how) {
    if (how) *how = "";
    if (!actor) return false;
    if (Identify(actor, out)) {
        if (how) *how = "pointer";
        return true;
    }
    if (!LooksLikeUObject(actor)) return false;
    void* cls = U::ClassOf(actor);
    if (!cls) return false;
    // A pawn knows its own controller and player state, and both survive the respawn that replaces the
    // pawn. Names only; the offsets come from this actor's own class chain.
    static const char* kOwnerProps[] = {"Controller", "PlayerState"};
    for (const char* prop : kOwnerProps) {
        int32_t off = U::PropOffset(cls, prop);
        if (off < 0) continue;
        void* owner = U::ReadPtr(actor, (uint32_t)off);
        if (!owner || !LooksLikeUObject(owner)) continue;
        if (!Identify(owner, out)) continue;
        // Self-heal, so the registry stops being wrong about this player: adopt the live pawn.
        {
            Guard g(g_lock);
            Entry* cur = FindByController(out.controller);
            if (cur && cur->pawn != actor) {
                cur->pawn = actor;
                cur->generation = ++g_generation;
                g_pawnAdoptions++;
            }
        }
        out.pawn = actor;
        g_identifyByOwner++;
        if (how) *how = prop;
        return true;
    }
    return false;
}

std::vector<Players::Entry> Players::All(bool includeOffline) {
    Guard g(g_lock);
    if (includeOffline) return g_players;
    std::vector<Entry> out;
    for (const auto& e : g_players)
        if (e.online) out.push_back(e);
    return out;
}

bool Players::Find(const std::string& ref, Entry& out) {
    Guard g(g_lock);
    // Exact ref first, then every id, then the names. The sidecar may address a player by the FLS id
    // it resolved out of Postgres, which the plugin has never seen — that is a 404 by design, and the
    // sidecar then falls back to its own chain rather than being handed the wrong player.
    for (const auto& e : g_players)
        if (e.Ref() == ref) {
            out = e;
            return true;
        }
    for (const auto& e : g_players) {
        char b[32];
        snprintf(b, sizeof b, "%llu", (unsigned long long)e.accountId);
        if (e.accountId && ref == b) { out = e; return true; }
        snprintf(b, sizeof b, "%llu", (unsigned long long)e.playerStateId);
        if (e.playerStateId && ref == b) { out = e; return true; }
        snprintf(b, sizeof b, "%llu", (unsigned long long)e.controllerId);
        if (e.controllerId && ref == b) { out = e; return true; }
        snprintf(b, sizeof b, "%llu", (unsigned long long)e.pawnId);
        if (e.pawnId && ref == b) { out = e; return true; }
        if (!e.characterName.empty() && ref == e.characterName) { out = e; return true; }
        if (!e.playerName.empty() && ref == e.playerName) { out = e; return true; }
    }
    return false;
}

uint64_t Players::Generation() {
    Guard g(g_lock);
    return g_generation;
}

uint64_t Players::SnapshotAgeMs() {
    Guard g(g_lock);
    return g_lastRefreshMs ? NowMs() - g_lastRefreshMs : 0;
}

bool Players::EnsureFresh(uint32_t ttlMs) {
    {
        Guard g(g_lock);
        if (g_players.empty()) return true;  // nothing to refresh; not a pump failure
        if (g_lastRefreshMs && NowMs() - g_lastRefreshMs < ttlMs) return true;
    }
    if (!GameThread::Run([] { Refresh(); }, 5000)) {
        Guard g(g_lock);
        g_refreshErrors++;
        return false;
    }
    return true;
}

size_t Players::Count() {
    Guard g(g_lock);
    return g_players.size();
}

std::string Players::Json() {
    uint64_t now = NowMs();
    Guard g(g_lock);
    std::string o = "{\"tracked\":" + std::to_string(g_players.size()) + ",\"generation\":" +
                    std::to_string(g_generation) + ",\"snapshotAgeMs\":" +
                    std::to_string(g_lastRefreshMs ? now - g_lastRefreshMs : 0) + ",\"refreshes\":" +
                    std::to_string(g_refreshes) + ",\"refreshFailures\":" + std::to_string(g_refreshErrors) +
                    ",\"offsetsWarmed\":" + (g_warmed ? "true" : "false") + ",\"warmNote\":" + JsonStr(g_warmNote);
    // LANE L2c. `pawnReresolves` is the smoking gun of the stale-pawn bug: every one of these is an
    // occasion on which the registry's cached pawn was NOT the live one, i.e. an occasion on which a
    // death would have been misclassified and a position would have come from the world origin.
    o += ",\"pawn\":{\"reresolves\":" + std::to_string(g_pawnReresolves.load()) + ",\"collected\":" +
         std::to_string(g_pawnsCollected.load()) + ",\"adoptedViaOwnerWalk\":" +
         std::to_string(g_pawnAdoptions.load()) + ",\"identifiedViaOwnerWalk\":" +
         std::to_string(g_identifyByOwner.load()) +
         ",\"note\":\"the pawn is re-resolved from the controller on every refresh; a respawn replaces it\"}";
    o += ",\"position\":{\"playerStateOriginRejected\":" + std::to_string(g_originPositionsRejected.load()) +
         ",\"unreadable\":" + std::to_string(g_positionsNone.load()) +
         ",\"rule\":\"pawn always wins; a playerState position is reported only when it is NOT at the "
         "world origin, else positionSource=unknown and there is no position\"}";
    uint64_t entries = 0, misses = 0;
    U::CacheStats(entries, misses);
    o += ",\"propertyCache\":{\"entries\":" + std::to_string(entries) + ",\"misses\":" + std::to_string(misses) + "}";
    o += ",\"players\":[";
    for (size_t i = 0; i < g_players.size(); i++) {
        if (i) o += ",";
        o += EntryJson(g_players[i], now);
    }
    return o + "]}";
}
