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

// Messages this plugin injects into the game (lane L3's sendMessage) are marked here so that the
// chat hook (lane L2) never re-emits them as a player chat-message. Entries expire after 10 s.
namespace state {
void MarkInjectedMessage(const std::string& text);
// True (and consumes the marker) when `text` was injected by us within the expiry window.
bool ConsumeInjectedMessage(const std::string& text);

// -----------------------------------------------------------------------------------------------
// Plugin-side ban list (lane L3b).
//
// The game's own list (UDedicatedServerSettings::KnownPlayerList[].bIsBanned) is only consulted by
// the login path at start-up, and it can only hold players the server has already seen. This list
// is ours: it is persisted to <PluginDataDir>/bans.json, it accepts any gameId, and it is what the
// PreLogin hook in events.cpp refuses a rejoin with. Both lists are kept in sync by POST /ban.
struct BanRecord {
    std::string gameId;  // bare 32-hex EOS ProductUserId, lower-cased
    std::string name;
    std::string reason;
    std::string createdAt;   // ISO-8601 UTC
    std::string expiresAt;   // ISO-8601 UTC, empty when permanent (the sidecar owns expiry)
};

// Reads bans.json. Safe to call before the game thread exists; called once from the plugin init.
void BansLoad();
// Cheap, thread-safe. Called from the PreLogin detour on the game thread.
bool IsBanned(const std::string& gameId);
bool BanAdd(const BanRecord& r);                // upserts and persists; false only on a write error
bool BanRemove(const std::string& gameId);      // false when there was no such entry
std::vector<BanRecord> BanList();
std::string BansPath();

// Character names observed in the server log (`PlayerChar entered world [Account[XP:<puid>]
// Character Name[<name>]`), used when the reflected/native getters come back empty.
void NoteCharacterName(const std::string& gameId, const std::string& name);
std::string CharacterName(const std::string& gameId);

// Readable creature names (lane L3c). `AVeinAICharacter::AIName` is an FText that only exists on
// a spawned AI (the cooked UAIDataAsset it comes from is streamed in on demand), so the name is
// cached the first time an AI of that Blueprint class is seen or killed. GET /entities and the
// entity-killed event both read this map, so both report the same name for the same `code`.
void NoteEntityName(const std::string& code, const std::string& name);
std::string EntityName(const std::string& code);
std::map<std::string, std::string> EntityNames();
}  // namespace state
