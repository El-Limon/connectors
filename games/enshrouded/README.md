# Takaro Enshrouded Connector

A server-side-only connector (plugin + sidecar) that connects an Enshrouded dedicated
server to Takaro. Players do not install anything.

It is built against one exact server: game build **1024233** (Steam app 2278520, branch
`public`, Steam build 23178631) running under **GE-Proton10-30** in
`mornedhels/enshrouded-server:1.7.2-proton`. That is the build every result in the table
below was proven on. The plugin finds game code by shape rather than by fixed addresses, so
it loads on other builds too — but on another build the affected capabilities self-check as
`degraded` in `/health`, and nothing here says they work there. **Keep the game server on
the build the plugin was released for**, and read "A game update can switch a feature off"
under Known issues before you let anything update it.

The plugin alone cannot talk to Takaro, and the sidecar alone cannot read player positions,
inventories, items or entities. Install both.

## Install

Download the latest release: https://takaro.io/connectors/enshrouded

### 1. Before you start

You need:

- An **Enshrouded dedicated server** you can stop, start and copy files to. The tested setup is the
  **Linux Docker image `mornedhels/enshrouded-server`**, where `enshrouded_server.exe` runs under
  Wine/Proton. The plugin is a **Windows DLL**, so a native Windows dedicated server should work the
  same way, but that has never been tried — only the Linux/Wine container is verified.
- **Docker** with the Compose plugin on the same host (the sidecar runs as a container that shares
  the game container's network). Node.js 22 on the host works too — see step 3.
- A **Takaro account** with a game server created of type **Generic**, and its **registration
  token** (Takaro shows it when you create the game server).

Nothing has to be compiled: both parts are published as release assets.

### 2. Download

From the latest `enshrouded-vX.Y.Z` release on the releases page:

> https://github.com/gettakaro/connectors/releases

Download both files:

- **`takaro-enshrouded-plugin-proton-1024233-<version>.zip`** — the game-server plugin
  (`dbghelp.dll` proxy)
- **`takaro-enshrouded-sidecar-proton-1024233-<version>.zip`** — the sidecar that talks to
  Takaro

The name in the middle is the server the connector was built against: Proton, game build
1024233. `takaro-enshrouded-plugin.zip` and `takaro-enshrouded-sidecar.zip` are still
published next to them and are the same bytes, so an old bookmark keeps working.

Direct link pattern:
`https://github.com/gettakaro/connectors/releases/download/enshrouded-v<version>/takaro-enshrouded-plugin-proton-1024233-<version>.zip`

The two **"Source code (zip/tar.gz)"** links GitHub adds to every release are an archive of this
whole repository, not the connector — do not download those. Also do not use the `enshrouded-dev`
pre-release or a `pr-<number>-enshrouded` build; those are untested rolling builds.

### 3. Copy it into place

Stop the game server first — a running server holds `dbghelp.dll` open.

**Plugin.** The plugin zip contains one folder:

```
TakaroEnshrouded/
    dbghelp.dll
    README.txt
```

The game server loads `dbghelp.dll` as a **dbghelp proxy**, so it has to sit next to
`enshrouded_server.exe` and the server has to be told to prefer it over the system copy. With
`docker-compose.example.yml`, copy **the DLL itself** (not the folder around it) to
`data/enshrouded/server/takaro/plugin/dbghelp.dll`.

**Sidecar.** The sidecar zip contains one folder, `TakaroEnshroudedSidecar/`, with
`dist/`, `package.json`, `package-lock.json`, `Dockerfile`, `.dockerignore`, `.env.example` and
`README.release.txt`. `docker-compose.example.yml` builds the sidecar image from `./sidecar`, so
unzip it next to the compose file and **rename the folder to `sidecar`**:

```
<your compose dir>/
    docker-compose.example.yml
    .env
    server/
        enshrouded-updater     <- from this folder in the repository (see below)
    sidecar/                   <- TakaroEnshroudedSidecar renamed
        Dockerfile
        dist/
        package.json
        package-lock.json
    data/
        enshrouded/
            server/            (game server data, created by the container)
                takaro/
                    plugin/
                        dbghelp.dll   <- from the plugin zip
        enshrouded-sidecar/    (sidecar cursor/online state, created by the container)
```

```bash
mkdir -p data/enshrouded/server/takaro/plugin data/enshrouded-sidecar
cp /path/to/TakaroEnshrouded/dbghelp.dll data/enshrouded/server/takaro/plugin/
mv /path/to/TakaroEnshroudedSidecar ./sidecar
```

**The updater override.** The image runs its own updater program at every container start:
it asks Steam what the branch head is and, when that differs from the installed build, runs
`steamcmd +app_update` over the install. On a plugin pinned to one build that is exactly
what must not happen, so `docker-compose.example.yml` bind-mounts
[`server/enshrouded-updater`](server/enshrouded-updater) (tracked next to this README) read-only over
the image's copy. It starts the server and does nothing else. Copy that one file next to
your compose file, keep it executable, and never run
`supervisorctl start enshrouded-force-update` inside the container.

If you would rather not use Docker for the sidecar, run it with Node.js 22 instead:
`npm ci --omit=dev && node dist/index.js`, with the same environment variables the compose service
sets (see `README.release.txt` inside the zip). It must reach the plugin on
`http://127.0.0.1:18890`, i.e. run on the game server's network.

The compose file bind-mounts the DLL **read-only** to `/opt/enshrouded/server/dbghelp.dll`, so
nothing inside the container can overwrite or delete it, and sets
`WINEDLLOVERRIDES: "dbghelp=n,b"` on the game container so the server loads this DLL instead of the
system one. It also pins the image by digest
(`mornedhels/enshrouded-server:1.7.2-proton@sha256:85978a10…`), because a newer image carries a
different Proton. If you use your own compose file or a native Windows server, you must reproduce
all of that yourself.

### 4. Configure

Copy the example environment file and fill it in:

```bash
cp .env.example .env
```

`games/enshrouded/.env` keys:

| Key | What to put there |
|---|---|
| `TAKARO_REGISTRATION_TOKEN` | Your Takaro registration token. Required. |
| `TAKARO_IDENTITY_ENSHROUDED` | A name that identifies this server to Takaro, e.g. `my-enshrouded-server`. |
| `TAKARO_ENSHROUDED_PLUGIN_TOKEN` | A long random shared secret, e.g. `openssl rand -hex 32`. The same value is passed to the game container (as `TAKARO_PLUGIN_TOKEN`) and to the sidecar; without it the plugin rejects every request with 401. Required. |
| `ENSHROUDED_ADMIN_PASSWORD` | Password for the server's Admins role. Required. |
| `ENSHROUDED_PLAYER_PASSWORD` | Password for the Friends role. Required. |
| `ENSHROUDED_GUEST_PASSWORD` | Password for the Guests role. Required. |

Everything else already has a working default in `docker-compose.example.yml`
(`TAKARO_WS_URL` = `wss://connect.takaro.io/`, `TAKARO_PLUGIN_URL` = `http://127.0.0.1:18890`,
log tailing, cursor file, health port 18891). Change `SERVER_NAME`, `SERVER_SLOT_COUNT`,
`TAKARO_SERVER_NAME` and `TZ` in the compose file to taste.

UDP port **15637** is published for game clients. The plugin's HTTP API stays on loopback inside the
container and is never exposed to the host.

### 5. Check that it worked

Start everything:

```bash
docker compose -f docker-compose.example.yml --env-file .env up -d --build
docker compose -f docker-compose.example.yml logs -f
```

In the plugin's own log, `data/enshrouded/server/takaro/plugin.log`:

```
takaro enshrouded plugin <version> starting (pid ...)
http: token from env TAKARO_PLUGIN_TOKEN
http: listening on 127.0.0.1:18890
game build: 1024233
capabilities: {...}
```

`<version>` is the release you downloaded — that line is how you confirm which plugin build is
actually loaded.

If you see `http: WARNING no token configured; all requests will be rejected with 401`, the shared
secret did not reach the game container — re-check `TAKARO_ENSHROUDED_PLUGIN_TOKEN` in `.env`.

In the sidecar log (`docker logs enshrouded-takaro-sidecar`):

```
Connecting to Takaro at wss://connect.takaro.io/
Takaro WebSocket open, sending identify
Takaro confirmed WebSocket connection
Identified with Takaro (gameServerId=...)
Sidecar health on http://127.0.0.1:18891/health; plugin http://127.0.0.1:18890
```

And in Takaro, the game server shows as **online**. If it stays offline, the registration token in
`.env` is the first thing to re-check. From inside the container,
`curl http://127.0.0.1:18891/health` shows per-capability status; anything reported as `degraded`
means a game update moved code the plugin hooks (see Known issues).

### 6. Upgrading

**Stop the game container first** — the running server holds `dbghelp.dll` open.

```bash
docker compose -f docker-compose.example.yml stop enshrouded
# replace the DLL in place, keeping the bind mount valid
cp /path/to/new/TakaroEnshrouded/dbghelp.dll data/enshrouded/server/takaro/plugin/dbghelp.dll
# replace the sidecar folder with the new one, then rebuild
rm -rf sidecar && mv /path/to/new/TakaroEnshroudedSidecar ./sidecar
docker compose -f docker-compose.example.yml up -d --build
```

Download both zips from the same release and upgrade them together. Your `.env`, the world in
`data/enshrouded/server/savegame/` and the sidecar state in `data/enshrouded-sidecar/` (event
cursor, online players) survive the upgrade — keep the cursor file so events are not replayed.
Confirm the new version in the `takaro enshrouded plugin <version> starting` log line.

A release is named for the server build it was made for. If a release's name carries a game
build other than the one your server runs, update the server to that build in the same
maintenance window — plugin and game build belong together.

## What works, what doesn't

Verified end to end on **2026-09-13/14** against a real dedicated server (game build **1024233**,
plugin **0.4.2**) with a real game client connected.
✅ = works, ⚠️ = works with a caveat or was not verified live, ❌ = does not work.

| What | | Notes |
|---|---|---|
| Connection & heartbeat | ✅ | The sidecar keeps an outbound WebSocket to Takaro and reconnects by itself. |
| Server restart / reconnect | ✅ | Comes back on its own after a game-container restart; takes up to ~3 minutes. Players still online when the server died are reconciled and reported as disconnected. |
| Player list | ✅ | Names are the Steam persona names, not the in-game character names — the server never exposes those. `gameId` is the SteamID64. |
| Single player lookup | ✅ | Same data as the player list. |
| Player location | ✅ | Matches the in-game position within 0.2 m and follows teleports. |
| Player inventory | ✅ | Matches the in-game backpack. Takaro's inventory history only records changes, so unchanged starting equipment never shows up there. |
| Item catalogue | ✅ | 3,609 items synced. |
| Entity catalogue | ✅ | 979 creature/NPC templates (Takaro stored 977 — two share a code). This is the fixed template list, not live creatures. |
| Locations / points of interest | ⚠️ | The plugin has 1,031 map locations and the sidecar serves them, but Takaro never asks for them, so this cannot be checked end to end. |
| Chat messages from players | ✅ | Real player chat reaches Takaro with the player attached. |
| Broadcast a message | ✅ | Shown to everyone in the server chat, but under the character name of an online player — Enshrouded has no "server" sender. |
| Whisper a player | ⚠️ | The message reaches the intended player. Only one player account was available, so "nobody else sees it" was never confirmed. |
| Give an item | ✅ | Works for normal players and for Admins-group players. The game ignores the count, so the plugin splits the request into stacks of at most 64. |
| Teleport a player | ✅ | Takaro rounds the coordinates to whole numbers and the game nudges the player to the nearest free spot. |
| Run a console command | ✅ | Enshrouded has no console; the plugin provides its own set: `help`, `players`, `say`, `whisper`, `location`, `teleport`/`tp`, `inventory`, `give`, `item`, `kick`, `save-and-shutdown`. There is no time-of-day command. |
| Kick | ✅ | The player must be online. Also works on Admins-group players: the game normally refuses that, and plugin 0.4.2 lifts the check for the one call. Plugin 0.4.1 and older silently did nothing for those players. |
| Ban (timed and permanent) | ⚠️ | Bans work on **online** players only, and are always permanent until unbanned — the game has no ban reason and no expiry, so a timed ban from Takaro becomes a permanent one. |
| Unban | ✅ | The player must have been seen online at least once while the connector was running (the game identifies accounts by a hash, not the SteamID). |
| Ban list | ✅ | Matches the server's own banned-accounts list. |
| Shut the server down | ✅ | Saves first, then quits; the container restart policy brings it back. |
| Player joined event | ✅ | From real joins; hooks fire from it. |
| Player left event | ✅ | From real leaves, and from sidecar reconciliation after the server was killed with a player online. |
| Player chat event | ✅ | See "Chat messages from players". |
| Player death event | ⚠️ | Falls and deaths by a creature both reach Takaro. The creature name is now included in the message text; that change was only unit-tested, never re-checked live. |
| Entity kill event | ✅ | Real kills reach Takaro. The weapon field stays empty, and destroyed props/voxels are filtered out. |
| Log events | ⚠️ | The sidecar sends them, but Takaro does not store server log lines as events, so they cannot be searched or used in modules. |
| Map info | ⚠️ | Not verified in a live test; nothing in the evidence records a map-info call for this connector. |
| Map tiles | ❌ | The Takaro API does not support map tiles for Generic-connector servers. |
| Discord chat bridge | ⚠️ | Game → Discord, Discord → game (from a real human account) and join/leave notices were all seen. The built-in `chatBridge` also posts Takaro's own server messages back to Discord (a Takaro-core echo affecting every game); use the `chatBridgeNoEcho` fork, which skips player-less chat and was proven to relay player chat without the echo. |
| Shop & economy | ⚠️ | Buying in game, ordering through Takaro and claiming later, refusal when the balance is too low, and balance/top-list commands were all exercised in the 2026-09-14 hard test, but there is no evidence file for that run in the workspace. Currency transfer was only tried player-to-self (one account), and in-game `@claim` needs the player's account linked to Takaro. A failed purchase shows the generic in-game message "Oops, something went wrong". |

### Known issues

- **Bans cannot expire and need the player online.** Enshrouded has no ban reason or end date, so
  every ban is permanent until you unban; unban needs the player to have joined once while the
  connector was running.
- **Whispers are not proven private.** They reach the right player, but with a single test account
  it was never confirmed that others do not see them.
- **Discord bridge echoes with the stock module.** Takaro stores its own outgoing server messages as
  chat and `chatBridge` relays them to Discord. Use `chatBridgeNoEcho`.
- **A game update can switch a feature off.** The plugin finds game code by shape and self-checks at
  load; after a game update a mismatching feature reports `degraded` in `/health` and in Takaro's
  reachability reason, while the server and everything else keep running. It then needs a new
  plugin build with re-derived signatures. The compose file in this folder is set up so the game
  can only move when you decide it does: the image is pinned by digest, the install comes from
  exactly the pinned Steam manifests, and the image's own updater program is replaced by
  `server/enshrouded-updater`. If you host the server some other way, keep automatic game updates
  off there too.
- **Player names are Steam persona names**, not the in-game character names.

---

Developers: see [DEVELOPMENT.md](DEVELOPMENT.md).
