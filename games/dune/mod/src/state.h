// Plugin state: capability registry + the event ring buffer served by GET /events.
#pragma once
#include "common.h"

#include <deque>

struct EventRecord {
    uint64_t seq;
    std::string type;
    std::string dataJson;
    std::string ts;
};

class PluginState {
public:
    static PluginState& Get();

    // status: "ok" | "degraded" | "unimplemented". `detail` is free text shown in capabilityDetails.
    void SetCapability(const std::string& name, const std::string& status, const std::string& detail = "");
    std::string Capability(const std::string& name) const;
    std::string CapabilitiesJson() const;
    std::string CapabilityDetailsJson() const;

    // Thread-safe. Used by L2's event sources.
    void EmitEvent(const std::string& type, const std::string& dataJson);
    // Events with seq > since, oldest first, at most `limit`.
    std::string EventsJson(uint64_t since, size_t limit) const;
    uint64_t LatestSeq() const;
    size_t Buffered() const;

    static const size_t kMaxEvents = 5000;

private:
    PluginState() = default;
    mutable Mutex lock_;
    std::map<std::string, std::pair<std::string, std::string>> caps_;
    std::deque<EventRecord> events_;
    uint64_t seq_ = 0;
};

namespace state {
// NOTE ON WHAT IS DELIBERATELY ABSENT
//
// The VEIN tree carried a plugin-side ban list and an "injected message" marker here. Neither
// belongs in this connector:
//   * bans are the sidecar's (plan Decision 6) — enforced by kick-on-sight over the RabbitMQ GM
//     command bus, because Dune's native bans are Funcom-account bans issued by FLS and a
//     self-hoster cannot set one,
//   * the plugin sends no chat (chat in and out are both RabbitMQ exchanges the sidecar owns), so
//     there is no echo to suppress.

// Character names observed for a gameId, used when the reflected getters come back empty. The
// authoritative source is Postgres `player_state.character_name` in the sidecar; this is a
// best-effort in-process cache so an event payload is not nameless.
void NoteCharacterName(const std::string& gameId, const std::string& name);
std::string CharacterName(const std::string& gameId);

// Readable creature names. A Dune creature's display name is not in the server paks at all (the
// server build ships no `Content/Localization/`, confirmed by the image probe), so the only names we
// can get in-process are whatever an FText on the spawned actor happens to carry. That is why every
// entry is cached the first time an NPC of that class is seen or killed, and why GET /entities marks
// an entry `nameIsClassName` when all we have is `BP_Something_C`. Catalogue rule
// (memory: catalogue-human-names): a dev/class name must never be shipped to Takaro as a display
// name — the sidecar joins these against the wiki-derived catalogue instead.
void NoteEntityName(const std::string& code, const std::string& name);
std::string EntityName(const std::string& code);
std::map<std::string, std::string> EntityNames();
}  // namespace state
