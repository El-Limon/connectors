#include "query.h"

#include "events.h"
#include "gamethread.h"
#include "objpool.h"
#include "players.h"
#include "reflect.h"
#include "resolve.h"
#include "state.h"

#include <cstdlib>

namespace {

// One capability per query, so /health.capabilities is a per-feature answer rather than a single
// "plugin ok". The sidecar reads exactly these names to decide whether to use the plugin's location
// or fall back to chat-origin / the last-saved Postgres row.
struct Cap {
    const char* name;
    const char* needs;  // the live UClass whose absence degrades it
};
const Cap kCaps[] = {
    {"players", "DunePlayerController"},
    {"playerLocation", "SceneComponent"},
    {"entities", "DuneNpcCharacter"},
};

uint32_t SnapshotTtlMs() {
    static uint32_t cached = 0;
    if (!cached) {
        long v = strtol(ConfigValue("TAKARO_SNAPSHOT_TTL_MS", "snapshotTtlMs", "500").c_str(), nullptr, 10);
        if (v < 1) v = 1;
        if (v > 30000) v = 30000;
        cached = (uint32_t)v;
    }
    return cached;
}

std::string PosJson(const U::Vec& v) {
    return "{\"x\":" + JsonNum(v.x) + ",\"y\":" + JsonNum(v.y) + ",\"z\":" + JsonNum(v.z) + "}";
}

}  // namespace

void Query::Init() {
    auto& st = PluginState::Get();
    for (auto& c : kCaps) {
        bool have = Classes::ByName(c.needs) != nullptr;
        if (!have) {
            st.SetCapability(c.name, "degraded", std::string("no live UClass `") + c.needs + "`");
            continue;
        }
        // "ok" means the reflection path is resolved, NOT that an answer has ever been produced: with
        // no player able to join, /players is empty and that is reported as such.
        st.SetCapability(c.name, "ok",
                         std::string("reflected through the live `") + c.needs +
                             "` class; NOT proven until a player has been in it");
    }
}

void Query::Housekeep() {
    // Deliberately NOT a periodic game-thread sweep. The snapshot is lazy: /players and
    // /players/{ref}/location call Players::EnsureFresh(ttl), so with nobody asking, the game thread
    // is never entered at all (docs/gamethread-policy.md, memory: avoid-game-thread). What is done
    // here — on the housekeeping thread — is expiry of logged-out entries, which touches no UObject.
    ::Players::Expire();
}

Query::Result Query::Players() {
    bool fresh = ::Players::EnsureFresh(SnapshotTtlMs());
    auto rows = ::Players::All(false);
    std::string o = "{\"generation\":" + std::to_string(::Players::Generation());
    o += ",\"snapshotAgeMs\":" + std::to_string(::Players::SnapshotAgeMs());
    o += ",\"snapshotFresh\":" + std::string(fresh ? "true" : "false");
    // Said out loud: this is the list of players the PLUGIN has seen through PostLogin. It is not the
    // roster — Postgres is — and it carries no FLS id, because this process has none.
    o += ",\"authority\":\"plugin-observed (PostLogin); the roster and `gameId` are the sidecar's, from Postgres\"";
    o += ",\"count\":" + std::to_string(rows.size()) + ",\"players\":[";
    for (size_t i = 0; i < rows.size(); i++) {
        if (i) o += ",";
        const auto& e = rows[i];
        o += "{\"ref\":" + JsonStr(e.Ref());
        o += ",\"characterName\":" +
             (e.characterName.empty() ? (e.playerName.empty() ? std::string("null") : JsonStr(e.playerName))
                                      : JsonStr(e.characterName));
        o += ",\"accountId\":" + (e.accountId ? std::to_string(e.accountId) : std::string("null"));
        o += ",\"playerStateId\":" + (e.playerStateId ? std::to_string(e.playerStateId) : std::string("null"));
        o += ",\"playerControllerId\":" + (e.controllerId ? std::to_string(e.controllerId) : std::string("null"));
        o += ",\"playerPawnId\":" + (e.pawnId ? std::to_string(e.pawnId) : std::string("null"));
        o += ",\"position\":" + (e.havePosition ? PosJson(e.position) : std::string("null"));
        o += ",\"positionSource\":" + JsonStr(e.positionSource);
        o += ",\"ageMs\":" + std::to_string(e.lastUpdateMs ? NowMs() - e.lastUpdateMs : 0);
        o += ",\"identityHow\":" + JsonStr(e.identityHow) + "}";
    }
    return {200, o + "]}"};
}

Query::Result Query::Player(const std::string& ref) {
    ::Players::EnsureFresh(SnapshotTtlMs());
    ::Players::Entry e;
    if (!::Players::Find(ref, e))
        return {404,
                "{\"error\":\"no player with that ref is tracked by the plugin. Refs are acct:<account_id>, "
                "ps:<player_state_id>, a bare id, or the character name - the FLS id is the sidecar's and is "
                "unknown here.\"}"};
    std::string o = "{\"ref\":" + JsonStr(e.Ref()) + ",\"characterName\":" +
                    (e.characterName.empty() ? (e.playerName.empty() ? std::string("null") : JsonStr(e.playerName))
                                             : JsonStr(e.characterName));
    o += ",\"accountId\":" + (e.accountId ? std::to_string(e.accountId) : std::string("null"));
    o += ",\"playerStateId\":" + (e.playerStateId ? std::to_string(e.playerStateId) : std::string("null"));
    o += ",\"playerControllerId\":" + (e.controllerId ? std::to_string(e.controllerId) : std::string("null"));
    o += ",\"playerPawnId\":" + (e.pawnId ? std::to_string(e.pawnId) : std::string("null"));
    o += ",\"online\":" + std::string(e.online ? "true" : "false");
    o += ",\"position\":" + (e.havePosition ? PosJson(e.position) : std::string("null"));
    o += ",\"positionSource\":" + JsonStr(e.positionSource);
    o += ",\"identityHow\":" + JsonStr(e.identityHow) + "}";
    return {200, o};
}

Query::Result Query::PlayerLocation(const std::string& ref) {
    if (!::Players::EnsureFresh(SnapshotTtlMs()))
        return {503,
                "{\"error\":\"the game-thread pump did not run in time, so the only position available would be "
                "stale. The sidecar's fallback chain (chat origin -> last-saved actors.transform) applies.\"}"};
    ::Players::Entry e;
    if (!::Players::Find(ref, e)) return {404, "{\"error\":\"no player with that ref is tracked by the plugin\"}"};
    if (!e.havePosition)
        return {404,
                "{\"error\":\"the player is tracked but has no live pawn transform yet (no pawn, or the root "
                "component was unreadable)\",\"positionSource\":\"unknown\"}"};
    // `source` is ALWAYS present. A silently-stale answer would be worse than no answer, because the
    // sidecar's own fallback chain only engages on a 404/501/503.
    std::string o = "{\"x\":" + JsonNum(e.position.x) + ",\"y\":" + JsonNum(e.position.y) + ",\"z\":" +
                    JsonNum(e.position.z) + ",\"pitch\":" + JsonNum(e.pitch) + ",\"yaw\":" + JsonNum(e.yaw);
    o += ",\"source\":" + JsonStr(e.positionSource);
    o += ",\"generation\":" + std::to_string(e.generation);
    o += ",\"ageMs\":" + std::to_string(e.lastUpdateMs ? NowMs() - e.lastUpdateMs : 0);
    o += ",\"snapshotAgeMs\":" + std::to_string(::Players::SnapshotAgeMs());
    o += ",\"at\":" + JsonStr(IsoNowUtc()) + "}";
    return {200, o};
}

Query::Result Query::Entities() { return Events::Entities(); }

Query::Result Query::KillNearest(const JsonValue& body) {
    // Forwarded: the damage pipeline, the AI class set and the entity-killed hook are all one lane's,
    // so the implementation lives next to the hook it exists to prove.
    return Events::KillNearest(body);
}

std::string Query::DiagnosticsJson() {
    std::string o = "{\"mutations\":\"none by design - the sidecar mutates over the RabbitMQ GM command bus\"";
    o += ",\"snapshotTtlMs\":" + std::to_string(SnapshotTtlMs());
    o += ",\"capabilities\":[";
    bool first = true;
    for (auto& c : kCaps) {
        if (!first) o += ",";
        first = false;
        o += "{\"name\":" + JsonStr(c.name) + ",\"needs\":" + JsonStr(c.needs) + ",\"needsResolved\":" +
             (Classes::ByName(c.needs) ? "true" : "false") + ",\"status\":" +
             JsonStr(PluginState::Get().Capability(c.name)) + "}";
    }
    o += "]}";
    return o;
}
