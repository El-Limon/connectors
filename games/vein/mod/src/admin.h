// Lane L3b: server-side admin grants.
//
// Why this exists: `[/Script/Vein.VeinGameSession] +AdminSteamIDs=` in Game.ini can never work.
// Lane L1 proved on the live class default object that the scalars in that same ini section load
// fine (ServerName, bPublic, HTTPPort, Password) while `AdminSteamIDs` and `SuperAdminSteamIDs`
// are `arrayNum: 0` on every boot regardless of syntax - the two TArray<FString> properties simply
// are not UPROPERTY(Config). So no configuration file can open the in-game admin panel.
//
// The route that does exist is the UFUNCTION `AVeinGameSession::SetAdmin(FString, bool)`
// (`execSetAdmin` is in the depot .sym), called through UObject::ProcessEvent on the *live* game
// session. TAKARO_ADMIN_STEAMIDS is applied that way, once the world exists, and re-applied for a
// player whose id is not in the session's array - which is read back by reflection
// (UStruct::FindPropertyByName), never at the 0x340 constant L1 measured.
//
// There is no super-admin setter: the .sym has AVeinGameSession::SetAdmin, AVeinPlayerState::SetAdmin
// and UAdminComponent::Server_SetAdmin, plus IsAdmin/IsSuperAdmin getters, but no SetSuperAdmin of
// any spelling. TAKARO_SUPERADMIN_STEAMIDS is therefore parsed and reported in /health as
// `superAdmins.configured` with `applied: []` and a reason; nothing is written. Populating
// SuperAdminSteamIDs would mean writing the TArray ourselves, which this lane does not do.
#pragma once
#include "common.h"

#include "actions.h"  // Actions::Result, the return type of the debug handler below

namespace Admin {

// Parses the env lists. Runs on the plugin init thread; touches no UObject.
void Init();

// Every 2 s from the housekeeping thread. Once the pump has ticked it hops onto the game thread,
// finds the live AVeinGameSession, reads the admin array by reflection and grants anything missing.
// Idempotent: a pass with nothing to do costs one property read.
void Housekeep();

// {configured, applied, sessionArrayNum, ...} for /health.diagnostics.admins. Served from a cached
// snapshot, so it is safe on an HTTP thread and never blocks on the pump.
std::string DiagnosticsJson();

// POST /debug/set-admin {gameId, admin} - token + TAKARO_PLUGIN_DEBUG gated by http.cpp.
Actions::Result SetAdminEndpoint(const JsonValue& body);

}  // namespace Admin
