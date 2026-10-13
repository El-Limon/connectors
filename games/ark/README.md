# Takaro ARK: Survival Evolved Connector

A server-side plugin for the **Linux** ARK: Survival Evolved dedicated server. It connects to Takaro by itself;
there is no sidecar and players install nothing.

## What works

Proven live on staging Takaro with a real client on the pinned build (see the acceptance ledger in the PR).

| Feature | Status | Notes |
| --- | --- | --- |
| Connect to Takaro, reachability | ✅ | Reports not reachable while the game is still starting |
| Player list, player identity | ✅ | Steam ID, in-game name |
| Join / leave events | ✅ | |
| Chat events | ✅ | Global chat |
| Send message (all / one player) | ✅ | A message to an offline player is refused, never broadcast |
| Player death events | ✅ | With position; attacker only when the killer is a player |
| Kill events (entity-killed) | ✅ | Creature and the held weapon (empty when unarmed) |
| Log events | ✅ | World save and startup lines only |
| Player location, teleport | ✅ | Matches the in-game GPS |
| Map | ✅ | The Island declared with its bounds; upload a map image in Takaro to see tiles |
| Locations (spawn points) | ✅ | Needs Takaro's Generic protocol v1 |
| Inventory, give item | ✅ | Amounts above one stack are delivered in rounds |
| Item and creature lists | ✅ | Player-facing names |
| Console commands | ✅ | `DoExit` is refused; use Takaro's shutdown |
| Kick, ban, unban, timed bans | ✅ | Bans survive restarts; timed bans are lifted by the plugin |
| Shutdown | ✅ | Saves the world before Takaro gets the answer |
| Modules, shop, economy, Discord | ✅ | Through Takaro as usual (use `claim` delivery for shop listings) |
| Other ARK maps | ⚠️ | Work, but only The Island has map bounds |
| Windows servers, other ARK builds | ❌ | Not supported; the plugin refuses to load on any other executable |

## Install

You need an ARK: Survival Evolved **Linux** dedicated server on public build **21241282** (Steam app 376030) and
control over its start command.

1. Download `takaro-ark-native-linux-21241282-<version>.zip` from the
   [releases](https://github.com/gettakaro/connectors/releases?q=ark) page.
2. Unzip it into your ARK install folder (the folder that contains `ShooterGame/`). You get
   `TakaroArk/TakaroArkNative/`.
3. Copy `TakaroArk/TakaroArkNative/takaro.cfg.example` to `takaro.cfg` in the same folder and set
   `TAKARO_REGISTRATION_TOKEN` (Takaro → Game servers → Add → Generic). The other keys are optional.
4. Start the server through the launcher instead of `ShooterGameServer`:

   ```sh
   /path/to/ark/TakaroArk/TakaroArkNative/launch.sh /path/to/ark 'TheIsland?listen?SessionName=My-ARK?Port=7777?QueryPort=27015' -server -log
   ```

   The launcher checks the server executable and refuses any other ARK build. In Docker, run the container with
   `--init` (Compose `init: true`).
5. The server shows up in Takaro within a minute. Keep the folder `<ark install>/.takaro/ark-native` when you
   update: it holds the server identity, ban reasons and expiry dates.

To update, replace `TakaroArk/TakaroArkNative/` and keep your `takaro.cfg`. To uninstall, stop the server, delete
`TakaroArk/` and start `ShooterGameServer` directly again.
