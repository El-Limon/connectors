# Takaro Rust Connector

A server-side-only plugin that connects a Rust dedicated server to Takaro. It is written for the
**Oxide/uMod** plugin API (`Oxide.Plugins` / `RustPlugin`) and is developed and verified on
**Carbon**, which runs the same plugins. Players do not install anything.

**Built and verified against Rust public build 25353106 (2026-09-16) with Carbon v2.0.259.** Other
Rust builds and other Carbon builds are unverified — the plugin will very likely still load, but
nothing here was checked against them.

## Install

### 1. Before you start

You need:

- A **Rust dedicated server** (Linux or Windows) that you can stop, start and copy files to, with
  either **[Carbon](https://carbonmod.gg/)** or **[Oxide/uMod](https://umod.org/games/rust)**
  installed.
- Access to the server's **plugin folder** (`carbon/plugins/` on Carbon, `oxide/plugins/` on Oxide).
- The ability to set **environment variables** on the server process — this plugin is configured
  through the environment, not through a config file (see step 4).
- A **Takaro account** with a game server created of type **Generic**, and its **registration
  token** (Takaro shows it when you create the game server).

### 2. Download the plugin

From the connector's release, download either name — they are the same bytes:

- **`takaro-rust-plugin-carbon-25353106-<version>.cs`** — the build's own name, which says exactly
  which Rust build and which Carbon it was verified against.
- **`TakaroConnector.cs`** — the same file under the name the framework loads.

`SHA256SUMS` is published beside them if you want to check the download.

If there is no tagged `rust-v*` release yet, take the rolling pre-release from
<https://github.com/gettakaro/connectors/releases/tag/rust-dev> (rebuilt on every push to `main`),
or copy `games/rust/mod/TakaroConnector.cs` straight out of the repository. There is nothing to
compile — Carbon and Oxide compile `.cs` plugins at runtime.

### 3. Copy it into place

Save the file **as `TakaroConnector.cs`** in your framework's plugin folder — the framework loads a
plugin by its class-named file, so the name matters:

```
# Carbon
<server>/carbon/plugins/TakaroConnector.cs

# Oxide / uMod
<server>/oxide/plugins/TakaroConnector.cs
```

Examples:

- Linux (Carbon): `/home/steam/rust/carbon/plugins/TakaroConnector.cs`
- Windows (Oxide): `C:\RustServer\oxide\plugins\TakaroConnector.cs`

The framework picks the file up and compiles it on the spot; a dropped-in plugin does not need a
server restart, but the environment variables in step 4 do.

### 4. Configure

**The plugin writes no config file.** It reads four environment variables from the server process
when it loads:

| Variable | What it is | Default |
|---|---|---|
| `TAKARO_REGISTRATION_TOKEN` | Your Takaro registration token. **Required.** | (none) |
| `TAKARO_WS_URL` | Takaro WebSocket endpoint. Leave as is. | `wss://connect.takaro.io/` |
| `TAKARO_IDENTITY_TOKEN` | A unique name for this server. Use a different one per server. | (empty) |
| `TAKARO_DEBUG` | `true` to log every message sent and received. | `false` |

Set them where your server process gets its environment — for example in the systemd unit, in the
start script before launching `RustDedicated`, or as `environment:` entries in Docker Compose:

```bash
export TAKARO_REGISTRATION_TOKEN="your-registration-token-here"
export TAKARO_IDENTITY_TOKEN="my-rust-server-1"
```

Then restart the server so the process picks up the new environment.

### 5. Check that it worked

In the server console / Carbon or Oxide log, the plugin prints lines prefixed with `[Takaro]`:

```
Loaded plugin TakaroConnector v<version> by Takaro [1667ms]
[Takaro] Connecting to wss://connect.takaro.io/
[Takaro] WebSocket connected
[Takaro] Identified successfully, server ID: <id>
```

And in Takaro, the game server shows as **online**.

If instead you see:

```
TAKARO_REGISTRATION_TOKEN not set. Plugin will not connect.
```

then the server process did not get the environment variable — re-check step 4. If it connects but
never identifies, the registration token is the first thing to re-check.

### 6. Upgrading

Replace `TakaroConnector.cs` in the plugin folder with the new file. Carbon and Oxide notice the
changed file and reload the plugin by themselves; no server restart is needed. Your environment
variables are untouched by the upgrade, so the server keeps its identity.

## What works, what doesn't

✅ means it was proven by an automated run against the exact pinned target (Rust build 25353106,
Carbon 2.0.259) with **no game client**: the server really booted in its pinned container, Carbon
really compiled and loaded this plugin, and the protocol harness really asked for each of these.
⚠️ means the plugin implements it but nothing has confirmed it — everything a real player is
needed for is in that group. ❌ means it is not implemented or not supported.

| What | | Notes |
|---|---|---|
| Plugin compiles and loads | ✅ | Carbon compiles the `.cs` at load; the loaded version is the version that was built. |
| Connection & identify | ✅ | Connects outbound over WebSocket and identifies to Takaro. |
| Heartbeat / reachability | ✅ | Answers Takaro's reachability check. |
| Server restart / reconnect | ✅ | Reconnects on its own after the connection drops, with exponential backoff (5 s up to 5 min), and identifies again. |
| Player list | ✅ | Answers with the connected players. Proven on an empty server only — the shape is verified, a populated list is not. |
| Item catalogue | ✅ | Every item definition the server knows, with its display name (e.g. `rifle.ak` → "Assault Rifle"). |
| Entity catalogue | ✅ | Built from the server's prefab manifest, with corpses and ragdolls filtered out, and display names derived from the prefab name (e.g. `bear` → "Bear"). |
| Run a console command | ✅ | Runs as a server console command and returns the output or the error. The connector logs each command it runs, because Rust's console does not echo them. |
| Broadcast a message | ✅ | Sent to everyone in the server chat, and logged by the connector. Proven to reach the server; that a player sees it is not, because the automated run has no client. |
| Shut the server down | ✅ | Runs the server's `quit` command: the world is saved, the plugin is unloaded and the server quits. Rust's own process then sometimes crashes inside Unity's teardown *after* all of that, so its exit code means nothing either way. |
| Single player lookup | ⚠️ | Finds connected and sleeping players by Steam id. Implemented, not verified — needs a client. |
| Player location | ⚠️ | Returns the player's position, falling back to the last known position when they are offline. Implemented, not verified — needs a client. |
| Player inventory | ⚠️ | Main inventory, hotbar and worn items. Item quality is always empty. Implemented, not verified — needs a client. |
| Locations / points of interest | ⚠️ | Returns the map's monuments. Implemented, not verified. |
| Chat messages from players | ⚠️ | Player chat is forwarded with the player and the chat channel attached. Implemented, not verified — needs a client. |
| Whisper a player | ⚠️ | Sent to the named player's chat only. Implemented, not verified — needs a client. |
| Give an item | ⚠️ | Goes into the player's inventory; if there is no room it drops at their feet. Implemented, not verified — needs a client. |
| Teleport a player | ⚠️ | Moves the player to the exact coordinates given, with no ground snapping. Implemented, not verified — needs a client. |
| Kick | ⚠️ | Drops the player with the reason shown. Implemented, not verified — needs a client. |
| Ban (timed and permanent) | ⚠️ | Timed bans are enforced by the plugin's own ban record. Permanent bans go into the server's own ban list; either way the player is kicked. Implemented, not verified. |
| Unban | ⚠️ | Clears both the plugin's ban record and the server's ban list. Implemented, not verified. |
| Ban list | ⚠️ | Returns the server's banned users (no expiry) plus the plugin's timed bans with their real expiry. Implemented, not verified. |
| Player joined event | ⚠️ | Sent when a player connects. Implemented, not verified — needs a client. |
| Player left event | ⚠️ | Sent when a player disconnects. Implemented, not verified — needs a client. |
| Player chat event | ⚠️ | See "Chat messages from players". |
| Player death event | ⚠️ | Sent when a player dies. Implemented, not verified — needs a client. |
| Entity kill event | ⚠️ | Sent when an entity is killed, with the weapon used where it can be read. Implemented, not verified — needs a client. |
| Log events | ⚠️ | Server console lines are forwarded, minus Carbon's and the plugin's own. Takaro does not store server log lines as events, so they cannot be searched or used in modules. |
| Oxide / uMod | ⚠️ | The file is written against the Oxide plugin API and Oxide loads it, but only Carbon has been booted. |
| Map info | ❌ | The plugin does not implement it. |
| Map tiles | ❌ | Not supported by Takaro for Generic-connector servers. |
| Discord chat bridge | ⚠️ | Nothing in the plugin blocks it — chat in and chat out are both implemented — but the bridge has never been tried on Rust in either direction. |
| Shop & economy | ⚠️ | Rests on give-item, chat commands and console commands; no shop purchase has been run on Rust. |

### Known issues

- **No player has ever played through it.** Everything marked ⚠️ above that says "needs a client"
  is exactly that: the automated run has no game client, so player hooks, give/teleport/kick/ban
  and the events around them are unproven. Expect to find things.
- **No map.** Takaro's API does not support map tiles for Generic-connector servers, and the plugin
  does not answer map-info requests either.
- **Item quality is not reported.** Inventory entries always come back with an empty quality field.
- **Entity names are derived, not localised.** Rust ships no display name for entity prefabs, so
  the catalogue turns `scientistnpc_heavy` into "Scientistnpc Heavy". The untouched short name is
  always in the entry's `code`.

---

Developers: see [DEVELOPMENT.md](DEVELOPMENT.md).
