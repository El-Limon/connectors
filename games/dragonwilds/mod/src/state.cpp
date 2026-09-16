#include "state.h"

#include <cctype>

PluginState& PluginState::Get() {
    static PluginState s;
    return s;
}

void PluginState::SetCapability(const std::string& name, const std::string& status, const std::string& detail) {
    Guard g(lock_);
    caps_[name] = {status, detail};
}

std::string PluginState::Capability(const std::string& name) const {
    Guard g(lock_);
    auto it = caps_.find(name);
    return it == caps_.end() ? std::string("unimplemented") : it->second.first;
}

std::string PluginState::CapabilitiesJson() const {
    Guard g(lock_);
    std::string o = "{";
    bool first = true;
    for (auto& kv : caps_) {
        if (!first) o += ",";
        first = false;
        o += JsonStr(kv.first) + ":" + JsonStr(kv.second.first);
    }
    return o + "}";
}

std::string PluginState::CapabilityDetailsJson() const {
    Guard g(lock_);
    std::string o = "{";
    bool first = true;
    for (auto& kv : caps_) {
        if (kv.second.second.empty()) continue;
        if (!first) o += ",";
        first = false;
        o += JsonStr(kv.first) + ":" + JsonStr(kv.second.second);
    }
    return o + "}";
}

void PluginState::EmitEvent(const std::string& type, const std::string& dataJson) {
    Guard g(lock_);
    EventRecord e{0, type, dataJson, IsoNowUtc()};
    e.seq = ++seq_;
    events_.push_back(std::move(e));
    while (events_.size() > kMaxEvents) events_.pop_front();
}

std::string PluginState::EventsJson(uint64_t since, size_t limit) const {
    Guard g(lock_);
    std::string items;
    size_t n = 0;
    uint64_t lastSeq = since;
    for (auto& e : events_) {
        if (e.seq <= since) continue;
        if (n >= limit) break;
        if (n) items += ",";
        items += "{\"seq\":" + std::to_string(e.seq) + ",\"type\":" + JsonStr(e.type) + ",\"data\":" + e.dataJson +
                 ",\"ts\":" + JsonStr(e.ts) + "}";
        lastSeq = e.seq;
        n++;
    }
    // `seq` is the cursor to pass back as `since`: the last returned event, or the current latest.
    uint64_t cursor = n ? lastSeq : (seq_ > since ? since : seq_);
    uint64_t oldest = events_.empty() ? seq_ + 1 : events_.front().seq;
    return "{\"bootId\":" + JsonStr(BootId()) + ",\"seq\":" + std::to_string(cursor) +
           ",\"latestSeq\":" + std::to_string(seq_) + ",\"truncated\":" +
           ((since + 1 < oldest && since < seq_) ? "true" : "false") + ",\"events\":[" + items + "]}";
}

uint64_t PluginState::LatestSeq() const {
    Guard g(lock_);
    return seq_;
}

size_t PluginState::Buffered() const {
    Guard g(lock_);
    return events_.size();
}

// ---------------------------------------------------------------------------------------------
// Injected-message markers (L3 writes, L2's chat hook reads).

namespace {
Mutex g_injLock;
std::deque<std::pair<std::string, uint64_t>> g_injected;  // {text, expiresAtMs}
const uint64_t kInjectedTtlMs = 10000;
}  // namespace

void state::MarkInjectedMessage(const std::string& text) {
    if (text.empty()) return;
    Guard g(g_injLock);
    uint64_t now = NowMs();
    while (!g_injected.empty() && g_injected.front().second <= now) g_injected.pop_front();
    if (g_injected.size() > 64) g_injected.pop_front();
    g_injected.push_back({text, now + kInjectedTtlMs});
}

bool state::ConsumeInjectedMessage(const std::string& text) {
    Guard g(g_injLock);
    uint64_t now = NowMs();
    while (!g_injected.empty() && g_injected.front().second <= now) g_injected.pop_front();
    for (size_t i = 0; i < g_injected.size(); i++) {
        if (g_injected[i].first != text) continue;
        g_injected.erase(g_injected.begin() + (long)i);
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------------------------
// Plugin-side ban list (lane L3b), persisted to <PluginDataDir>/bans.json.

namespace {
Mutex g_banLock;
std::map<std::string, state::BanRecord> g_bans;  // key = lower-cased gameId
bool g_bansLoaded = false;
std::string g_bansPath;

std::string LowerId(const std::string& s) {
    std::string o;
    for (char c : s) o += (char)tolower((unsigned char)c);
    return o;
}

// Callers hold g_banLock.
bool BansSaveLocked() {
    std::string o = "{\"version\":1,\"bans\":[";
    bool first = true;
    for (auto& kv : g_bans) {
        const state::BanRecord& b = kv.second;
        if (!first) o += ",";
        first = false;
        o += "{\"gameId\":" + JsonStr(b.gameId) + ",\"name\":" + JsonStr(b.name) + ",\"reason\":" + JsonStr(b.reason) +
             ",\"createdAt\":" + JsonStr(b.createdAt) +
             ",\"expiresAt\":" + (b.expiresAt.empty() ? std::string("null") : JsonStr(b.expiresAt)) + "}";
    }
    o += "]}\n";
    if (WriteFileAtomic(g_bansPath, o)) return true;
    PluginLog("state: could not write %s", g_bansPath.c_str());
    return false;
}
}  // namespace

std::string state::BansPath() {
    Guard g(g_banLock);
    if (g_bansPath.empty()) g_bansPath = PluginDataDir() + "/bans.json";
    return g_bansPath;
}

void state::BansLoad() {
    Guard g(g_banLock);
    if (g_bansLoaded) return;
    g_bansLoaded = true;
    if (g_bansPath.empty()) g_bansPath = PluginDataDir() + "/bans.json";
    std::string text;
    if (!ReadFile(g_bansPath, text)) {
        PluginLog("state: no plugin ban list at %s (starting empty)", g_bansPath.c_str());
        return;
    }
    JsonValue v;
    if (!JsonParse(text, v)) {
        PluginLog("state: %s is not valid JSON; ignoring it", g_bansPath.c_str());
        return;
    }
    const JsonValue* arr = v.get("bans");
    if (!arr || arr->type != JsonValue::Array) return;
    for (const JsonValue& e : arr->arr) {
        const JsonValue* id = e.get("gameId");
        if (!id || !id->isStr() || id->str.empty()) continue;
        BanRecord b;
        b.gameId = LowerId(id->str);
        const JsonValue* n = e.get("name");
        const JsonValue* r = e.get("reason");
        const JsonValue* c = e.get("createdAt");
        const JsonValue* x = e.get("expiresAt");
        if (n && n->isStr()) b.name = n->str;
        if (r && r->isStr()) b.reason = r->str;
        if (c && c->isStr()) b.createdAt = c->str;
        if (x && x->isStr()) b.expiresAt = x->str;
        g_bans[b.gameId] = b;
    }
    PluginLog("state: loaded %zu plugin ban(s) from %s", g_bans.size(), g_bansPath.c_str());
}

bool state::IsBanned(const std::string& gameId) {
    if (gameId.empty()) return false;
    Guard g(g_banLock);
    return g_bans.find(LowerId(gameId)) != g_bans.end();
}

bool state::BanAdd(const state::BanRecord& r) {
    if (r.gameId.empty()) return false;
    Guard g(g_banLock);
    BanRecord b = r;
    b.gameId = LowerId(b.gameId);
    if (b.createdAt.empty()) b.createdAt = IsoNowUtc();
    auto it = g_bans.find(b.gameId);
    if (it != g_bans.end() && b.name.empty()) b.name = it->second.name;
    g_bans[b.gameId] = b;
    return BansSaveLocked();
}

bool state::BanRemove(const std::string& gameId) {
    Guard g(g_banLock);
    auto it = g_bans.find(LowerId(gameId));
    if (it == g_bans.end()) return false;
    g_bans.erase(it);
    BansSaveLocked();
    return true;
}

std::vector<state::BanRecord> state::BanList() {
    Guard g(g_banLock);
    std::vector<BanRecord> out;
    for (auto& kv : g_bans) out.push_back(kv.second);
    return out;
}

// ---------------------------------------------------------------------------------------------
// Character names seen in the server log.

namespace {
Mutex g_nameLock;
std::map<std::string, std::string> g_charNames;  // gameId -> character name
}  // namespace

void state::NoteCharacterName(const std::string& gameId, const std::string& name) {
    if (gameId.empty() || name.empty()) return;
    Guard g(g_nameLock);
    std::string key = LowerId(gameId);
    auto it = g_charNames.find(key);
    if (it != g_charNames.end() && it->second == name) return;
    if (g_charNames.size() > 256) g_charNames.clear();
    g_charNames[key] = name;
    PluginLog("state: character name for %s is %s (from the server log)", key.c_str(), name.c_str());
}

std::string state::CharacterName(const std::string& gameId) {
    Guard g(g_nameLock);
    auto it = g_charNames.find(LowerId(gameId));
    return it == g_charNames.end() ? std::string() : it->second;
}
