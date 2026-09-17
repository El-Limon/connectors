# Takaro VEIN Connector

A server-side-only connector (plugin + sidecar) that connects a VEIN **Linux dedicated server**
to Takaro. Players install nothing.

The plugin alone cannot talk to Takaro, and the sidecar alone cannot read players, positions,
inventories, items or entities. Install both.

## Install

### 1. Before you start

You need:

- A **VEIN Linux dedicated server** (Steam app **2131400**, anonymous SteamCMD login) that you can
  stop, start and copy files to, and the ability to change how the server binary is launched. This
  connector is loaded with `LD_PRELOAD` on the server's launch command; VEIN has no mod folder.
- **Docker** with the Compose plugin on the same host (the sidecar runs as a container that shares
  the game container's network namespace), or **Node.js 22** on the game server host, next to the
  server. `docker-compose.example.yml` in this folder shows the Docker shape.
- A **Takaro account** with a game server created of type **Generic**, and its **registration
  token** (Takaro shows it when you create the game server).

Nothing has to be compiled: both parts are published as release assets.

### 2. Download

From the latest `vein-vX.Y.Z` release on the releases page:

> https://github.com/gettakaro/connectors/releases

Download both files:

- **`takaro-vein-plugin.tar.gz`** — the game-server plugin (`libtakaro-vein.so`)
- **`takaro-vein-sidecar.tar.gz`** — the sidecar that talks to Takaro

Direct link pattern:
`https://github.com/gettakaro/connectors/releases/download/vein-v<version>/takaro-vein-plugin.tar.gz`

The two **"Source code (zip/tar.gz)"** links GitHub adds to every release are an archive of this
whole repository, not the connector — do not download those. Do not use the `vein-dev`
pre-release or a `pr-<number>-vein` build either; those are untested rolling builds.

### 3. Copy it into place

Stop the game server first.

**Plugin.** `takaro-vein-plugin.tar.gz` contains one folder:

```
TakaroVein/
    libtakaro-vein.so
    README.txt
```

Put the `.so` **outside the Steam/game tree** — a SteamCMD `app_update … validate` deletes files it
does not know about, and that includes this one. With `docker-compose.example.yml` that is
`data/vein-plugin/libtakaro-vein.so` on the host, with the **directory** bind-mounted read-only to
`/opt/takaro` in the game container. On a plain (non-Docker) server, use a directory next to the
Steam tree, e.g. `<server>/takaro/libtakaro-vein.so`.

**Sidecar.** `takaro-vein-sidecar.tar.gz` contains one folder, `TakaroVeinSidecar/`, with `dist/`,
`package.json`, `package-lock.json`, `Dockerfile`, `.dockerignore` and `.env.example`.
`docker-compose.example.yml` builds the sidecar image from `./sidecar`, so unpack it next to the
compose file and **rename the folder to `sidecar`**:

```
<your compose dir>/
    docker-compose.example.yml
    .env
    sidecar/                  <- TakaroVeinSidecar renamed
    data/
        vein/                 (game server data, created by the container)
        vein-plugin/
            libtakaro-vein.so
        vein-sidecar/         (event cursor and online state, created by the container)
```

```bash
mkdir -p data/vein-plugin data/vein-sidecar
tar -xzf takaro-vein-plugin.tar.gz
cp TakaroVein/libtakaro-vein.so data/vein-plugin/
tar -xzf takaro-vein-sidecar.tar.gz && mv TakaroVeinSidecar ./sidecar
```

### 4. Load the plugin into the server

The plugin is loaded with `LD_PRELOAD`, and **only onto the game binary**. SteamCMD is 32-bit and
fails outright if it sees a 64-bit preload, so never set `LD_PRELOAD` globally for the container,
the user account or the service — put it on the launch line of the server binary itself:

```bash
LD_PRELOAD=/opt/takaro/libtakaro-vein.so \
TAKARO_PLUGIN_TOKEN=<your shared secret> \
  ./Vein/Binaries/Linux/VeinServer-Linux-Test -Port=7777 -QueryPort=27015 -log
```

Pterodactyl / Pelican — put the same two variables in front of the server binary in the egg's
startup command (add `TAKARO_PLUGIN_TOKEN` as an egg variable, and leave the SteamCMD install
script untouched):

```
LD_PRELOAD=/home/container/takaro/libtakaro-vein.so TAKARO_PLUGIN_TOKEN={{TAKARO_PLUGIN_TOKEN}} ./Vein/Binaries/Linux/VeinServer-Linux-Test -Port=7777 -QueryPort=27015 -log
```

Docker — `docker-compose.example.yml` shows the shape: the image's entrypoint must apply
`LD_PRELOAD="${TAKARO_PLUGIN_SO}"` to the server launch line only.

To confirm the plugin is really loaded: `grep libtakaro /proc/<server pid>/maps`.

### 5. Configure

Copy the example environment file and fill it in:

```bash
cp .env.example .env
```

| Key | Where | What to put there |
|---|---|---|
| `TAKARO_PLUGIN_TOKEN` | game server **and** sidecar | A long random shared secret, e.g. `openssl rand -hex 32`. Without it the plugin rejects every request with 401. Required. |
| `TAKARO_REGISTRATION_TOKEN` | sidecar | Your Takaro registration token for a **Generic** game server. Required. |
| `TAKARO_IDENTITY_TOKEN` | sidecar | A name that identifies this server to Takaro, e.g. `my-vein-server`. Required. |
| `TAKARO_ADMIN_STEAMIDS` | game server | Comma-separated SteamID64s the plugin grants in-game admin to. VEIN has no `AdminSteamIDs` config key, so this is how the in-game admin panel is opened. |
| `VEIN_LOG_FILE` | sidecar | The server's log, e.g. `/home/steam/vein/Vein/Saved/Logs/Vein.log`. Used for join/leave lines and log events. |

Other useful keys are documented in `.env.example`: `TAKARO_PLUGIN_PORT` (default `18890`),
`TAKARO_PLUGIN_URL`, `TAKARO_SENDER_NAME`, `TAKARO_ITEM_NAMES`, `TAKARO_BROADCAST_VIA`,
`TAKARO_CURSOR_FILE` and `VEIN_HTTP_API`.

**The sidecar must reach the plugin on loopback.** The plugin's HTTP API binds `127.0.0.1` only and
is never exposed to the network, so the sidecar has to share the game server's network namespace
(compose: `network_mode: "service:vein"`) or run directly on the game server host.

### 6. Check that it worked

Start everything:

```bash
docker compose -f docker-compose.example.yml --env-file .env up -d --build
```

In the plugin's own log, `<serverdir>/takaro/plugin.log`:

```
takaro vein plugin <version> starting (pid ..., bootId ...)
http: listening on 127.0.0.1:18890
```

If you see a warning that no token is configured, `TAKARO_PLUGIN_TOKEN` did not reach the game
process. From the sidecar (or the game host):

```bash
curl -H "Authorization: Bearer $TAKARO_PLUGIN_TOKEN" http://127.0.0.1:18890/health
```

must answer `"status": "ok"`. The sidecar's own health endpoint,
`curl http://127.0.0.1:18891/health`, answers `"status": "ok"` too and lists each capability; a
capability reported as `degraded` means a game update moved code the plugin uses (see Known issues)
— everything else keeps working.

In the sidecar log (`docker logs vein-takaro`):

```
Takaro WebSocket open, sending identify
Identified with Takaro (gameServerId=...)
```

And in **Takaro the game server shows as online**. If it stays offline, the registration token is
the first thing to re-check.

### 7. Upgrading

**After a game update** the plugin notices the new build id, throws away its resolution cache and
looks the game's code up again on the next start. Check `/health` afterwards — if a capability
reports `degraded`, the game moved or renamed something and that one feature needs a new plugin
build. Keep the server and the clients on the same build: VEIN is version-locked, so players on an
older client cannot join.

**To upgrade the connector**, stop the game server (it holds the `.so` open), replace
`data/vein-plugin/libtakaro-vein.so`, replace the `sidecar/` folder with the new one, and start
again with `--build`. Take both files from the same release. Your `.env`, the world and the sidecar
state in `data/vein-sidecar/` (event cursor, online players) survive the upgrade — keep the cursor
file so events are not replayed. Confirm the new version in the `takaro vein plugin <version>
starting` log line.

## What works, what doesn't

Verified end to end on **2026-09-17** against a VEIN Linux dedicated server on game build **25035268
(v0.024h8)**, plugin **0.1.0**, with a real game client in the world. Every row below was re-checked on
that final build — after a death and respawn, after the player left and rejoined, and after a full server
restart.
✅ = works, ⚠️ = works with a caveat, ❌ = does not work / is not supported.

<!-- REGENERATE FROM capabilities.json AT L7b -->

| What | | Notes |
|---|---|---|
| Connection & heartbeat | ✅ | The sidecar keeps an outbound WebSocket to Takaro and the server shows as reachable while it is up. |
| Player list | ✅ | Name, ping and whether the player has spawned. `gameId` is the player’s SteamID64 and `platformId` is `steam:<that id>`. |
| Single player lookup | ✅ | Same data as the player list, for one player. An offline player still answers with a last-known record instead of an error. |
| Player location | ✅ | The player’s position in the world, read straight from the pawn and following walking and teleports. |
| Player inventory | ✅ | What the player is carrying, read from the character’s inventory. Items VEIN keeps as one object per unit (corn, MREs, insulin…) are reported as a single row with the summed count, the way the game’s own bag shows them. While the player is dead the inventory is empty and no corpse loot leaks in. |
| Give an item | ✅ | The item appears in the player’s inventory without a relog. For an item VEIN keeps as one object per unit, asking for N gives N separate objects, which the bag then groups. |
| Item catalogue | ✅ | Every item class the server has loaded (1350 on the tested build), synced into Takaro’s item list. Names are the internal class names unless `TAKARO_ITEM_NAMES=1` is set, which asks the game for the readable name at start-up. |
| Entity catalogue | ⚠️ | The zombie and animal types the server has loaded are synced into Takaro, but Takaro’s catalogue also keeps a few rows from earlier game builds, because its sync only adds and updates and never deletes. |
| Locations / points of interest | ⚠️ | The connector serves the location markers currently streamed in, but Takaro never asks for them, so it cannot be checked end to end. |
| Run a console command | ✅ | VEIN has no operator console, so the connector provides its own set (`help`, `players`, `say`, `whisper`, `give`, `tp`, `kick`, `ban`, `unban`, `bans`, `save`, `shutdown`). An unknown command comes back as a failure with the reason. |
| Broadcast a message | ✅ | Everyone sees the message. It renders as a chat line prefixed with the server name, because VEIN has no separate "server" chat sender. |
| Whisper a player | ✅ | Reaches the one player, but as an on-screen notification rather than a chat line — VEIN cannot target a chat message at a single client. |
| Teleport a player | ✅ | The player is moved to the requested position. |
| Kick a player | ✅ | The player is dropped from the server and can rejoin afterwards. |
| Ban a player (timed and permanent) | ✅ | The player is disconnected and refused on rejoin. Bans go into VEIN’s own ban list and into the connector’s, so an offline player can be banned too. Timed bans are lifted by the connector when they expire; the game has no expiry of its own. |
| Unban a player | ✅ | Clears the ban in the game and in the connector, and the player can rejoin at once. |
| Ban list | ✅ | The game’s own bans unioned with the connector’s. Reason and expiry are kept by the connector, because the game stores neither. |
| Shut the server down | ✅ | Saves the world first, then quits cleanly; your restart policy brings it back. |
| Player joined event | ✅ | Arrives in Takaro on every join, with the player’s SteamID64. |
| Player left event | ✅ | Arrives on a clean quit and after a server crash by reconciliation. |
| Player chat event | ✅ | Real player chat reaches Takaro; messages the connector itself sent are not echoed back. |
| Player death event | ✅ | Reaches Takaro when a player dies, with the position and what killed them. A death with no killer — a fall or drowning — is reported with no attacker rather than blaming the victim. |
| Entity kill event | ✅ | A creature killed by a player reaches Takaro with the creature’s name, the player and the item they were holding. Kills the game’s own AI makes among itself are not reported, because no player was involved. |
| Log events | ⚠️ | The connector forwards server log lines (passwords and Steam tickets redacted, checked live), but Takaro does not store log lines as events, so they cannot be searched or used in modules. |
| Map info | ❌ | Takaro does not support map info for Generic game servers, so there is nothing for the connector to serve. |
| Map tiles | ❌ | Takaro does not support map tiles for Generic game servers. There is no map view for a VEIN server. |
| Modules: chat commands | ✅ | In-game chat commands with the domain’s prefix reach the module and answer in chat. |
| Modules: hooks | ✅ | Chat and join hooks fire and run their module code. |
| Modules: cronjobs | ✅ | Scheduled module runs fire and can message the server. |
| Modules: teleports (`@settp`, `@tp`, …) | ✅ | The teleports module’s in-game commands move the player. |
| Modules: server messages / onboarding | ✅ | Timed server messages and the welcome message on join are delivered in game. |
| Shop: buy in game | ✅ | Buying from the shop with the in-game chat command. |
| Shop: order in Takaro and claim in game | ✅ | An order placed in Takaro delivers the items to the player. |
| Shop: bundle of several items | ✅ | One claim delivers every item in the listing. |
| Shop: order while offline, claim later | ✅ | The claim is refused while the player is offline and succeeds after rejoining. |
| Shop: not enough currency | ✅ | The purchase is refused and the balance is unchanged. |
| Economy: currency | ✅ | Balances are set, read and debited by Takaro. |
| Economy: balance / top list in game | ✅ | The in-game economy commands answer in chat. |
| Discord: game chat → Discord | ✅ | In-game chat is relayed to the linked Discord channel. |
| Discord: Discord → game chat | ⚠️ | The path is wired and every other direction works, but the chat-bridge module ignores messages from bots, so this direction can only be confirmed by a real human post in the linked channel — that one check is still outstanding. |
| Discord: module hook / cronjob posts | ✅ | Module hooks and cronjobs can post to Discord and edit their own messages. |
| Discord: join/leave notices | ✅ | Join and leave notices posted to Discord by the chat-bridge module. |
| Discord: no echo of server messages | ✅ | The stock `chatBridge` module re-posts Takaro’s own server messages to Discord (a Takaro-core echo affecting every game); the `chatBridgeNoEcho` fork does not. |
| Events while the Takaro connection is down | ✅ | Events that happen while Takaro is unreachable are kept and delivered in order once the connection is back. An event counts as delivered only when Takaro’s heartbeat confirms it, so nothing is lost at the moment the connection dies. |
| Reconnects after a server or container restart | ✅ | The connector comes back and re-identifies on its own, and players who were online are reported as disconnected. When it shares the game container’s network it restarts itself after a game-container restart, so keep its restart policy on. |
| No duplicate events after a connector restart | ✅ | The event cursor is persisted, so a sidecar restart replays nothing. |
| Survives a network drop to Takaro | ✅ | The WebSocket reconnects by itself with a backoff of 2 to 60 seconds and re-identifies as the same server. |
| Timed bans expire on their own | ✅ | The connector lifts a timed ban when it runs out, including when it was restarted in between. |
| Keeps running after a game update breaks a feature | ✅ | The plugin self-checks at load and a feature it can no longer find reports `degraded` in `/health` and in Takaro’s reachability reason while the server and everything else keep running. |

<!-- END REGENERATE -->

### Known issues

- **`LD_PRELOAD` must be set on the game binary only.** SteamCMD is a 32-bit program and fails
  immediately if it inherits a 64-bit preload, so never set it for the whole container, user or
  service — only on the line that starts `VeinServer-Linux-Test`.
- **A SteamCMD `validate` deletes the plugin if it lives in the Steam tree.** Keep
  `libtakaro-vein.so` in a directory outside the game install (mounted read-only in Docker).
- **Client and server are version-locked.** After a game update, players on the old client cannot
  join. Update the server and the clients together, and keep automatic updates off if you want to
  choose the moment.
- **The server writes the join password and players' Steam session tickets into its own log in
  cleartext.** The connector redacts both before anything is forwarded to Takaro, but the file on
  disk still contains them — do not paste raw server logs into public issues.
- **A broadcast renders as a chat line prefixed with the server name.** VEIN has no separate
  "server" chat sender, so a broadcast goes out as a normal chat line reading
  `[<sender name>] <text>`. Set `TAKARO_SENDER_NAME` to choose the prefix.
- **A whisper renders as an on-screen notification, not a chat line.** VEIN cannot target a chat
  message at one client, so a message to a single player is delivered as that player's notification
  instead. It reaches the right player and Takaro stores it as a whisper.
- **Local and global chat cannot be told apart on VEIN 0.024h8.** The connector reports the chat
  channel the game gives it, but a line typed with the selector on **Local** still arrives as
  *all*, so a module option like "only global chat" will also see proximity chat.
- **Item names are internal class names by default.** The readable names only exist on each item's
  class default object, so the connector reports the class name unless `TAKARO_ITEM_NAMES=1` is set,
  which makes it ask the game for the readable name at start-up (a little slower to boot).
- **`AdminSteamIDs` in `Game.ini` is ignored by the game.** Whatever you put there, no player ends
  up admin — this is the game's own behaviour, not the connector's. Use `TAKARO_ADMIN_STEAMIDS`
  instead: the plugin grants those SteamID64s admin through the game's own path once the player is
  in the world.
- **Takaro's item and entity lists only grow.** The connector reports what the server currently has
  loaded, but Takaro's sync adds and updates rows and never deletes them, so items or creatures that
  a game update renamed or removed stay in Takaro's lists. Everything the live server has is correct;
  there can be extra old rows next to it.
- **A character does not survive a server restart.** After the server restarts, players come back to
  the new-character screen and the previous character's items are gone. This is VEIN's own behaviour
  on this build and there is nothing the connector can do about it — warn your players before a
  restart.
- **The connector container must keep `restart: unless-stopped`.** It shares the game container's network
  (`network_mode: service:vein`), and every restart of the game container gives the game a new network
  namespace while the connector keeps the old, dead one. The connector notices (no plugin *and* no game HTTP
  API for 45 seconds), stops itself, and Docker's restart policy brings it back in the right namespace. Remove
  the restart policy and the connector stays dead until you restart it by hand.
- **A timed ban shows up in Takaro as permanent while it is live** (gettakaro/takaro#3981). The connector reports the real expiry, but
  Takaro's ban sync stores a connector-reported ban with no "until" and marked as not managed by Takaro. The
  ban is still lifted on time in game, and the row disappears at the next ban sync after that. This one is on
  Takaro's side, not the connector's.
- **Server visibility and the Steam query port.** A server only appears in VEIN's own list when
  `bPublic` is on and the Steam query port (`-QueryPort=`, `27015` by default) is reachable from the
  outside. Note that with `bPublic` on, the server's heartbeat announces the **port it sees itself
  on**, so behind Docker or any port mapping it advertises the internal port and the listing entry
  will not be connectable. Neither is needed for Takaro — the connector never uses the query port — but if your
  server is missing from the list, that pair is the thing to check. Direct connect works either way.
- **There is no map.** Takaro does not support map info or map tiles for Generic game servers, so a
  VEIN server has no map view in Takaro.
- **Some items are one object per unit, not a stack.** VEIN keeps corn, MREs, insulin and similar
  "pseudo-stackable" items as a separate object per unit and only groups them on screen. Asking for
  three corn therefore hands the player three separate corn, and the connector reports one row per
  item type with the units summed — the same number the player sees in their bag.
- **Kill events name the victim and the killer's held item.** A creature kill is reported with the
  creature's name, the player who made it and the item they were holding. Kills the game's own AI
  makes among itself are **not** reported at all: no player was involved, and guessing a killer from
  "who is online" would fill Takaro's kill leaderboard with kills nobody made.
- **A fall or drowning death has no attacker.** VEIN names the victim as their own killer for
  environmental damage, so the connector drops that attacker rather than scoring the death as PvP.
  The cause still appears in the death message.
- **`unknown` and `debug` can appear as the weapon.** Takaro requires every kill event to carry a
  weapon string, so when the killer held nothing identifiable the connector sends `unknown`
  (a punch and an item it cannot resolve look the same on this build), and `debug` for a kill
  triggered through the plugin's own debug endpoint.
- **A rare duplicate event is possible around a connection loss.** Events are only marked delivered
  once Takaro's heartbeat confirms them, which is what stops events being lost when the connection
  dies. If the confirmation itself is lost after Takaro already stored an event, that event is sent
  again and stored twice. The window is one heartbeat (a few seconds); Takaro's protocol has no
  per-event acknowledgement, so this is the honest trade — duplicates are tolerable, silent loss is not.
- **A game update can switch a feature off.** The plugin finds the game's code at load and
  self-checks every address before it is used; after a game update a feature it can no longer find
  reports `degraded` in `/health` and in Takaro's reachability reason, while the server and
  everything else keep running. That feature then needs a new plugin build.

---

Developers: see [DEVELOPMENT.md](DEVELOPMENT.md).
