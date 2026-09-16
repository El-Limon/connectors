# Takaro Dragonwilds plugin: HTTP API (contract v0.1, lane L1)

The plugin is `libtakaro-dragonwilds.so`, loaded into `RSDragonwildsServer-Linux-Shipping` through
`LD_PRELOAD`. It serves this API on `127.0.0.1:18890`, inside the server's network namespace (the
container). The sidecar codes against this document.

Contract shape, status codes and the `/events` cursor semantics are the Takaro Enshrouded plugin's
`API.md` v0.4; identity, endpoints and diagnostics are adapted to Dragonwilds.

## Status of this document
- **Implemented (L1):** `GET /health`, `GET /events`, `GET /debug/*`.
- **Implemented and proven in game (L3, 2026-09-16):** `/players*`, `/items`, `/bans`,
  `POST /message|/teleport|/give|/kick|/ban|/unban|/command|/shutdown`. Per-capability proof:
  `context/games/dragonwilds/evidence/2026-09-16-plugin-actions.md`. `/entities` and `/locations`
  answer, but only with what the streaming world currently has loaded (see the action section).
- **Events (L2):** all six types are implemented; `entity-killed` is installed but unproven. Capabilities:
  `joinLeaveEvents`, `chatEvents`, `deathEvents`, `killEvents`, `logEvents`.

## Transport and auth
- HTTP/1.1, one short-lived thread per connection, every response closes it (`Connection: close`).
  Bodies are JSON (`Content-Type: application/json`), UTF-8.
- Every request needs `Authorization: Bearer <token>`.
  - Token from env `TAKARO_PLUGIN_TOKEN`, else `<server binary dir>/takaro/plugin.json` `{"token":"..."}`.
  - Wrong or missing token: `401 {"error":"unauthorized"}`. No token configured at all:
    `401 {"error":"plugin token not configured"}`.
- Errors are always `{"error":"<message>"}`:
  - `400` bad body or missing field · `404` unknown path or player not online · `405` known path,
    wrong method · `409` the game refused the action · `413` body over 1 MiB ·
    `501 {"error":"unimplemented", "capability":"..."}` · `503` the game thread is unavailable.
- POST bodies are parsed **before** the 501 check, so a malformed body is a `400` even for an
  action that is not wired yet.
- The listener binds loopback only and retries every 5 s if the port is busy.

## Configuration
| env | `plugin.json` key | default | meaning |
|---|---|---|---|
| `TAKARO_PLUGIN_TOKEN` | `token` | — | bearer token; without it every request is 401 |
| `TAKARO_PLUGIN_PORT` | `port` | `18890` | loopback port |
| `TAKARO_PLUGIN_DEBUG` | `debug` | off | enables `/debug/*` and per-request logging |
| `TAKARO_SYM_PATH` | `symPath` | `<exe>.sym` | symbol database |
| `TAKARO_PLUGIN_DATA_DIR` | — | `<exe dir>/takaro` | `plugin.log`, `symcache.json`, `plugin.json` |

Logs go to `<data dir>/plugin.log`. Every line is passed through the redactor, which masks
`WorldPassword`, `AdminPassword` and `Password=` values in ini, JSON and CLI form.

## Identity
`gameId` is the player's **EOS ProductUserId** (32 hex chars, shown at the bottom of the in-game
Settings screen). `steamId` is present when the platform id is a SteamID64; `platformId` is
`epic:<puid>` or `steam:<id>`. `name` is the in-game character name.
*(L3 owns the implementation; nothing about identity is proven yet.)*

## GET /health
```json
{"status":"ok","version":"0.1.0","bootId":"c07f7763cfbf47c9","pid":48,
 "gameBuild":"++dominion+staging-CL-240163","engineVersion":"5.6.1-240163",
 "buildId":"3b4ce30aed886594","uptimeMs":26709,
 "capabilities":{"gameThread":"ok","reflection":"ok","players":"unimplemented", ...},
 "capabilityDetails":{"players":"lane L3 not wired yet", ...},
 "symCache":{"path":"...","symPath":"...","buildId":"...","hit":true,"loadMs":0},
 "diagnostics":{
   "sym":{"resolved":74,"wanted":74,"processEventSlot":77},
   "selfChecks":[{"check":"_init: sym=0xdc2dd90 elfSection=0xdc2dd90","ok":true}, ...],
   "reflect":{"objName":24,"classDefaultObject":272, ...,"validations":[{"check":"findFunctionNative","ok":true,"detail":"..."}]},
   "gameThread":{"installed":true,"how":"...","alive":true,"tickCount":698,"tickThreadId":47,
                 "tickThreadChanges":0,"approxHz":28.8,"jobsRun":2,"jobsTimedOut":0,"maxJobMs":9},
   "http":{"port":18890,"requests":12,"unauthorized":2,"handlerErrors":0},
   "events":{"buffered":0,"latestSeq":0},
   "hooksInstalled":3,
   "resolved":[{"name":"UObject::ProcessEvent","rva":"0x4e3cf50","addr":"0x503cf50","how":"symcache",
                "hooked":false,"fired":0,"signature":"UObject::ProcessEvent(UFunction*, void*)",
                "records":37,"contiguous":true}, ...]}}
```
- `status` is `"ok"` only when every self-check passed and `reflection` is `ok`; otherwise `"degraded"`.
  A degraded plugin keeps serving; the server is never affected.
- `capabilities` values are `"ok"`, `"degraded"` or `"unimplemented"`; the reason for a degrade is in
  `capabilityDetails`. `"ok"` means resolved/hooked, **not** proven — live proof lives in
  `context/games/dragonwilds/evidence/`.
- `bootId` is random per server process. A different `bootId` means the server restarted: reset the
  event cursor to `0` even if `seq` is higher than yours.
- `diagnostics` is informational. Do not code against its exact shape.

## GET /events?since=\<seq\>[&limit=\<n\>]
```json
{"bootId":"c07f7763cfbf47c9","seq":328,"latestSeq":330,"truncated":false,
 "events":[{"seq":328,"type":"player-connected","data":{...},"ts":"2026-09-16T18:22:37.107Z"}]}
```
- Ring buffer of the last 5000 events. `seq` is monotonic, starts at 1, resets on restart.
- Returns events with `seq > since`, oldest first, capped at `limit` (default and maximum 5000).
- Pass the response `seq` back as the next `since`: it is the last returned event, or the current
  latest when nothing is new.
- `since > latestSeq` means the server restarted; reset to `0`.
- `truncated: true` means events between `since` and the oldest buffered event were dropped.
- Types: `player-connected`, `player-disconnected`, `chat-message`, `player-death`, `entity-killed`,
  `log`. All are wired (lane L2); `entity-killed` is hooked but has never fired yet.

### Event payloads (real output, build `++dominion+staging-CL-240163`)
The `player` object is `{gameId, name, characterName?, platformName?, characterGuid?, steamId?, platformId}`;
`gameId` is the bare 32-hex EOS ProductUserId.
```json
{"type":"player-connected","data":{"player":{"gameId":"0123456789ab…","name":"takarotester","characterName":"takarotester","characterGuid":"41C4B04F…","platformId":"epic:0123456789ab…"}}}
{"type":"player-disconnected","data":{"player":{…}}}
{"type":"chat-message","data":{"msg":"hello","channel":"global","player":{…}}}
{"type":"player-death","data":{"player":{…},"position":{"x":0,"y":0,"z":0},"attacker":{…}?,"killerEntity":"FallDamageActor","source":"telemetry|health-edge"}}
{"type":"entity-killed","data":{"entity":"Goblin_Scout","weapon":"","player":{…}}}
{"type":"log","data":{"msg":"[2026.09.16-17.46.33:165][355]LogNet: Login request: ?p=<redacted>…"}}
```
- `position` is omitted when the game did not fill the victim location; `attacker` only appears for a
  player killer, a creature/environment killer goes into `killerEntity`.
- `log` lines are redacted (`WorldPassword`, `AdminPassword`, `Password=`, `?p=<base64>`), UE noise
  categories are dropped, and at most 120 lines are emitted per 2 s cycle
  (`/health.diagnostics.eventSources.logDropped` counts the rest).
- Event sources and their hook state are in `/health.diagnostics.eventSources`
  (`{source, hooked, fired, emitted, note}`, plus `processEventVTables`, `trackedConnections`, `logPath`).

## Debug endpoints (need the token *and* `TAKARO_PLUGIN_DEBUG=1`; otherwise `404`)
| endpoint | returns |
|---|---|
| `GET /debug/gamethread` | `{"ranOnThreadId":47,"httpThreadId":123,"latencyMs":31,"stats":{...}}`. Runs a no-op job on the game thread. `503` if the pump never ticked. |
| `GET /debug/symbols` | the ELF facts, the symcache block and the full resolved table. |
| `GET /debug/object?path=/Script/Pkg.Name` or `?ptr=0x...` | the object's UPROPERTY tree up the class chain: `{name, type, offset, value}` per property, values decoded for primitives, `FString`, `FName` and object pointers. When the object is itself a `UClass`/`UScriptStruct` the properties it *declares* are listed under `declaredProperties`. `ptr` is refused unless it is inside a readable mapping. |
| `GET /debug/structs?name=X` | property table of a `UClass`/`UScriptStruct`. `X` is a full path (`/Script/JagexChatBackend.ChatMessageData`) or a plain name, which is resolved by scanning the live object array (so the owning module does not have to be known). |

Example (real output, build `++dominion+staging-CL-240163`):
```
GET /debug/structs?name=ChatMessageData
{"package":"/Script/JagexChatBackend","name":"ChatMessageData","class":"ScriptStruct",
 "propertiesSize":136,"super":"",
 "properties":[{"name":"SenderData","type":"StructProperty","offset":0},
               {"name":"MessageBody","type":"StrProperty","offset":120}]}
```

## Actions (lane L3) — real shapes

Identity: `gameId` is the bare 32-hex EOS ProductUserId, lower-cased (a `RedpointEOS:` prefix is
stripped). `GET /players/{id}`, `/give`, `/teleport`, `/kick`, `/ban`, `/unban` also accept the
character name or the platform name for convenience.

| endpoint | body (required in bold) | response |
|---|---|---|
| `GET /players` | — | `[{gameId,name,characterName,platformName,epicOnlineServicesId,platformId:"epic:<puid>",steamId?,ping,spawned,online:true,connectedAt}]` |
| `GET /players/{id}` | — | one player object, else `404 {"error":"player not online"}` |
| `GET /players/{id}/location` | — | `{x,y,z,yaw,pitch}` (UE cm, doubles); `503` when the player has no pawn yet |
| `GET /players/{id}/inventory` | — | `[{code,name,amount,inventory,slot}]` — one entry per item across every `UInventoryComponent` under the pawn and the controller (`inventory` is the component name, e.g. `BP_Components_Inventory`, `BP_Components_Loadout`) |
| `GET /items[?search=]` | — | `[{code,name,description,category}]`, 1536 entries on this build; `search` matches code or name, case-insensitive |
| `GET /entities` | — | `[{code,name,type:"hostile",description}]` — the AI character classes and `UAIDataAsset` assets currently loaded (AI content streams in on demand, so this is not the full bestiary) |
| `GET /locations` | — | `[{code,name,position:{x,y,z}}]` — lodestone actors currently streamed in |
| `GET /bans` | — | `[{gameId,name,reason,expiresAt,createdAt,enforcedBy}]` — the union of the game's own `KnownPlayerList` (`bIsBanned=True`) and the plugin ban list in `<serverdir>/takaro/bans.json`. The game stores neither a reason nor an expiry, so those come from the plugin list; timed bans are the sidecar's job. `enforcedBy` is `"plugin"` when the `PreLogin` hook is installed and the entry is in the plugin list (the rejoin is refused immediately) and `"game"` for an entry only a restarted server would honour |
| `POST /message` | **text**, `recipientGameId?`, `senderName?` | `{success:true,delivered:<n>}`. Without a recipient the message goes to everyone. The client renders it under the *receiving* player's name, so the sender is prefixed: `[<senderName>] <text>`. `senderName` defaults to `TAKARO_SENDER_NAME`, else `Server` |
| `POST /teleport` | **gameId**, **x**,**y**,**z** *or* **target**, `yaw?` | `{success:true,position:{x,y,z}}`; `404` unknown target, `409` when the game refuses the destination |
| `POST /give` | **gameId**, **code**, `amount` (default 1) | `{success:true,code,name,amount}`; `404` unknown/ambiguous code, `409` when the game refuses (full inventory) |
| `POST /kick` | **gameId**, `reason?` | `{success:true,gameId,online:true}`; `404` when the player is not online |
| `POST /ban` | **gameId**, `reason?` | `{success:true,gameId,online,persisted,pluginList,enforcedBy,detail}`. Three things happen: the id goes into the plugin ban list (`takaro/bans.json`), which the `PreLogin` hook refuses a rejoin with **immediately, with no restart** (the client sees the game's own `PLogBanned` refusal); an online player is disconnected through `ADominionGameSession::BanPlayer`; and `KnownPlayerList[…].bIsBanned` is set and saved to `DedicatedServer.ini` so the game itself keeps refusing the login after a restart. `persisted:false` with `pluginList:true` means the server has never seen that player, so only the plugin list carries the ban — which is enough |
| `POST /unban` | **gameId** | `{success:true,gameId,pluginList,persisted,detail}`; removes the id from the plugin ban list and clears `bIsBanned` in the game's list, then saves. The player can rejoin at once |
| `POST /command` | **command** | `{success,output}` — `output` is a string (JSON text for the list commands) |
| `POST /shutdown` | — | `{success:true,"detail":"saving, then SIGTERM"}`, then `CanSave` → `RequestSaveGame` → `SIGTERM` to our own pid; the container restarts under its compose policy |

### `POST /command` command set
```
help | players | bans | items [query] | entities | locations
say <msg> | whisper <gameId> <msg>
give <gameId> <code> [amount] | tp <gameId> <x> <y> <z>
kick <gameId> [reason] | ban <gameId> [reason] | unban <gameId>
save | shutdown
raw <console command>        # UEngine::Exec with our own FOutputDevice; output is captured
cheat <gameId> <dom command> # 501: the Shipping dedicated server creates no CheatManager
```
`raw` only reaches commands that survive into a Shipping build (`log list` works; `obj list`,
`stat unit` and `memreport` answer `(command not recognised by the engine)`).

## Implementation notes (for capability owners)
- **Symbols** (`src/sym.cpp`): every address comes from the depot's `<exe>.sym` at runtime; no RVA is
  ever hard-coded. Format: `u32 N`, `N` x 20-byte `{u64 rva, u32 line, u32 fileOff, u32 nameOff}`
  sorted by rva, then a `\n`-separated name table shared by source paths and demangled signatures.
  A function's address is the **lowest rva of its record run**; `vaddr = rva + the first PT_LOAD
  p_vaddr` read from the ELF (`0x200000` on this build). A wanted entry either pins one exact
  signature or matches by base name (lowest rva across overloads). Results are cached in
  `<data dir>/symcache.json` keyed on `.note.gnu.build-id`, so a game update invalidates it.
  Addresses outside an executable segment, or starting with `0x00`/`0xCC`, are rejected.
- **Boot self-checks** (`/health.diagnostics.selfChecks`): `_init`/`_fini` from `.sym` must equal the
  ELF section addresses; `UObject::ProcessEvent` must appear **exactly once** in
  `dlsym("_ZTV7UObject")` (that match is the ProcessEvent slot index, 77 on this build); at least 40
  names must resolve.
- **Reflection** (`src/reflect.cpp`): UE 5.6 offsets are declared in one `Layout` struct and
  *validated against the live process*; on mismatch they are re-discovered by scanning, and on
  failure the capability degrades. Confirmed on this build: `UObject::NamePrivate 0x18`,
  `UClass::ClassDefaultObject 0x110`, `UFunction::Func 0xD8`, `UStruct::ChildProperties 0x50`,
  `FField::Next 0x18`, `FField::NamePrivate 0x20`, `FProperty::Offset_Internal 0x44`.
  The decisive check is `FindFunction(CDO PlayerChatComponent,"Server_SendChatMessage")->Func ==
  sym("UPlayerChatComponent::execServer_SendChatMessage")`.
  **Never stringify an unvalidated `FName`**: `FName::ToString` indexes the engine name pool and
  segfaults on a bogus index. Discovery compares raw `FName` values (via `FindPropertyByName`) and
  only converts to text once the offset is confirmed.
  All game-struct property offsets come from `FindPropertyByName` at runtime, never from constants.
- **Game thread** (`src/gamethread.cpp`): the engine `Tick` is hooked by vtable slot swap. Hooking
  the declaring class is not enough — a derived class has its own vtable holding the *same* inherited
  pointer. The plugin therefore sweeps every exported `_ZTV*` symbol (44 800 of them) and swaps every
  slot holding the target. On this build the live engine is `UDomGameEngine`, whose slot 98 holds
  `UJgxGameEngine::Tick`; hooking only `_ZTV14UJgxGameEngine` never fires. The detour calls the
  original first, then drains at most 16 queued jobs. HTTP threads only enqueue and wait (5 s → 503).
- **Hooks** (`src/hooks.cpp`): vtable slot swaps only, with `mprotect` + restore on unload. No inline
  detours. `/health.diagnostics.resolved` reports `hooked` and `fired` per name.
- **Actions** (`src/actions.cpp`): every UObject touch runs inside `GameThread::Run`. The world is
  found through `GetObjectsOfClass(UWorld)`; players come from `GameState.PlayerArray`; the EOS id is
  found by scanning the first eight words of `APlayerState::UniqueID` for a pointer whose vtable is
  `_ZTV15FUniqueNetIdEOS` (never assume the `TSharedPtr` offset, and never stringify an unvalidated
  `FName`). `UTeleportationSubsystem::GetTargetLocationNames()` must **not** be called: its return
  convention does not match a by-value `TArray<FName>` here and it crashed the server once.
  Moderation state is `UDedicatedServerSettings::KnownPlayerList[…].bIsBanned` + `PerformConfigSave`,
  not `BannedUserList`/`SetBannedUsers`, which the game ignores on this build.
- **Safety**: every handler is wrapped in `try/catch(...)`; every game pointer is checked against a
  cached `/proc/self/maps` before dereference; a failed capability degrades with a reason and the
  server keeps running.
