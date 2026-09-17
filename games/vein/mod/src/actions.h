// Action handlers (lane L3). Each returns an HTTP status plus a JSON body; the HTTP layer does the
// routing, auth, body validation and the try/catch. Handlers that touch UObjects must do their work
// inside GameThread::RunJson() and return 503 when it returns false.
//
// Contract for L3:
//   * Init() runs on the plugin init thread; register one capability per action with
//     PluginState::SetCapability (players, playerLocation, playerInventory, giveItem, listItems,
//     listEntities, listLocations, sendMessage, teleport, kick, ban, unban, listBans,
//     executeCommand, shutdown).
//   * Snapshot() runs on the game thread once per tick (when L3 enables it) and caches the player
//     table so that GET /players never blocks on the pump.
//   * Every handler returns {status, body} and never throws.
#pragma once
#include "common.h"

namespace Actions {

struct Result {
    int status = 501;
    std::string body = "{\"error\":\"unimplemented\"}";
};

void Init();
void Housekeep();

// TODO(L3): implement. Until then each returns 501 with a message naming the missing capability.
Result Players();
Result Player(const std::string& gameId);
Result PlayerLocation(const std::string& gameId);
Result PlayerInventory(const std::string& gameId);
Result Items(const std::string& search);
Result Entities();
Result Locations();
Result Bans();
Result Message(const JsonValue& body);
Result Teleport(const JsonValue& body);
Result Give(const JsonValue& body);
Result Kick(const JsonValue& body);
Result Ban(const JsonValue& body);
Result Unban(const JsonValue& body);
Result Command(const JsonValue& body);
Result Shutdown();

// Kicks an online player that the plugin's ban list refuses. Must be called on the game thread
// (lane L2's join resolver already is); a no-op when the player is not online.
bool KickBanned(const std::string& gameId);

// Debug only (TAKARO_PLUGIN_DEBUG=1): kills the nearest AI character to a player through the game's
// own damage pipeline, with that player as the instigator. It exists so that entity-killed can be
// proven without a human swinging a sword.
Result KillNearest(const JsonValue& body);

}  // namespace Actions
