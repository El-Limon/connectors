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

namespace {
// gameId keys are case-normalised: the FLS id is 16 hex and reaches us from several places (a
// PlayerState read, a log line, an HTTP query) with inconsistent case.
std::string LowerId(const std::string& s) {
    std::string o = s;
    for (char& c : o)
        if (c >= 'A' && c <= 'Z') c += 32;
    return o;
}
Mutex g_nameLock;
std::map<std::string, std::string> g_charNames;
}  // namespace

// The plugin-side ban list and the injected-message marker that the VEIN tree had here are gone on
// purpose. In this connector **bans are the sidecar's** (plan Decision 6: a connector-owned
// `bans.json` plus kick-on-sight over the RabbitMQ GM bus, because Dune's own bans are FLS
// account-level and a self-hoster cannot set one), and **the plugin sends no chat at all**, so there
// is no injected message that a chat hook could echo back. Keeping either here would have been dead
// code that a later lane would have mistaken for the real mechanism.

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

// ---- readable creature names (lane L3c) --------------------------------------------------------

namespace {
Mutex g_entityNameLock;
std::map<std::string, std::string> g_entityNames;  // Blueprint class name -> AIName display text
}  // namespace

void state::NoteEntityName(const std::string& code, const std::string& name) {
    if (code.empty() || name.empty()) return;
    Guard g(g_entityNameLock);
    auto it = g_entityNames.find(code);
    if (it != g_entityNames.end() && it->second == name) return;
    if (g_entityNames.size() > 512) g_entityNames.clear();
    g_entityNames[code] = name;
    PluginLog("state: entity %s is named '%s' (FText read off a live actor)", code.c_str(), name.c_str());
}

std::string state::EntityName(const std::string& code) {
    Guard g(g_entityNameLock);
    auto it = g_entityNames.find(code);
    return it == g_entityNames.end() ? std::string() : it->second;
}

std::map<std::string, std::string> state::EntityNames() {
    Guard g(g_entityNameLock);
    return g_entityNames;
}
