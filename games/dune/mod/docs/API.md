# Takaro Dune: Awakening plugin — HTTP API (contract v0.1, lane L1)

`libtakaro-dune.so`, preloaded into the **map server process only**
(`DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping`). It serves a loopback HTTP API that
the Takaro sidecar polls.

## Status of this document

Written by lane L1 as the contract; **updated by lane L2 from the live process** (parameter layouts,
property offsets and the identity join below are measurements, not plans). Nothing here is a claim
that a feature *works*: with no Funcom token no client can join, so every death and connect hook is
**installed and unfired**, and `/health.diagnostics.eventSources.{hits,emitted}` are all `0`. Those
counters are the only evidence that would justify a claim — live proof lands in
`context/games/dune/evidence/`.

## Why this API is so much smaller than the VEIN plugin's

Dune has no RCON, but it *does* expose almost everything out of process: the roster, inventories and
positions in **Postgres**, and mutations over the game's own **RabbitMQ GM command bus** (`heartbeats`
exchange, gated by `ServerCommandsAuthToken`). The sidecar owns all of that, on a stock Funcom
install, with zero binary tampering.

So this plugin is **read-only and deliberately narrow**. It exists for the four things nothing
out-of-process can give:

| # | capability | why the sidecar cannot do it |
|---|---|---|
| a | live pawn location | `actors.transform` in Postgres is the **last-saved** position, not the live one |
| b | `entity-killed`, and killer/weapon attribution for `player-death` | the shipped SQL schema's `event_log` is currency-only (`LogCategoryType` has the single value `'solaris'`), so there is no DB kill feed at all |
| c | precise connect/disconnect | the Postgres online-state poll lags and cannot tell a map transfer from a disconnect |
| d | the `/entities` class list and the diagnostics | — |

**There is no mutating endpoint here at all.** `give`, `teleport`, `kick`, `message`, `ban`, `unban`,
`shutdown`, and the item/location/inventory catalogues are the sidecar's. A request to one of those
paths answers `404` with that explanation, so a misconfigured sidecar fails loudly instead of looking
like an unimplemented plugin feature.

## Transport and auth

- HTTP/1.1, one short-lived thread per connection, every response closes it (`Connection: close`).
  Bodies are JSON (`Content-Type: application/json`), UTF-8.
- Every request needs `Authorization: Bearer <token>`.
  - Token from env `TAKARO_PLUGIN_TOKEN`, else `<server binary dir>/takaro/plugin.json` `{"token":"..."}`.
  - Wrong or missing token: `401 {"error":"unauthorized"}`. No token configured at all:
    `401 {"error":"plugin token not configured"}`.
- Errors are always `{"error":"<message>"}`:
  - `400` bad body or missing field · `404` unknown path, player not online, or a sidecar-owned path ·
    `405` known path, wrong method · `413` body over 1 MiB ·
    `501 {"error":"...", "capability":"..."}` · `503` the game thread is unavailable.
- The listener binds **loopback by default** (`127.0.0.1:18890`) and retries every 5 s if the port is
  busy. `TAKARO_PLUGIN_BIND=0.0.0.0` exists for one concrete reason: the sidecar is a **separate
  container** with its own network namespace (it needs DNS for `postgres` and `game-rmq`), so a
  loopback-only listener is unreachable by the only client the plugin has. The relaxation is opt-in,
  does **not** publish the port (on the dev rig 18890 is reachable only from other containers on the
  private compose network), still requires the bearer token on every route, and is reported in
  `/health.diagnostics.http.bind` so it can never be a silent change.

## Configuration

| env | `plugin.json` key | default | meaning |
|---|---|---|---|
| `TAKARO_PLUGIN_TOKEN` | `token` | — | bearer token; without it every request is 401 |
| `TAKARO_PLUGIN_PORT` | `port` | `18890` | loopback port |
| `TAKARO_PLUGIN_DEBUG` | `debug` | off | enables `/debug/*` and per-request logging |
| `TAKARO_PLUGIN_DATA_DIR` | — | `<exe dir>/takaro` | `plugin.log`, `symcache.json`, `plugin.json` |
| `TAKARO_TICK_BUDGET_US` | `tickBudgetUs` | `500` | how many microseconds of one engine tick the queued-job pump may use. Leftover jobs wait for the next tick; one job always runs, so a slow job cannot starve the queue. Clamped to 50…33000 |
| `TAKARO_PRESENCE_SWEEP_MS` | `presenceSweepMs` | `2000` | how often the **presence reconciler** sweeps `GUObjectArray` for live `PlayerController`s holding a net connection. Clamped to 250…60000; `0` disables it and leaves presence depending on the PostLogin/Logout slots alone (not recommended — see below). The sweep times itself and doubles its own interval, up to 30 s, while a pass costs more than 50 ms |
| `TAKARO_SNAPSHOT_TTL_MS` | `snapshotTtlMs` | `500` | how long the player snapshot may be served from cache before the game thread is entered again. Lazy: with no requests there are no entries. Clamped to 1…30000 |
| `TAKARO_PROCESSEVENT_SLOT` | `processEventSlot` | — | **escape hatch.** Overrides the derived `UObject::ProcessEvent` vtable slot. Only for a human who has settled the slot in a debugger; recorded in `/health` as `how: env`. See *Symbol resolution* below |

Logs go to `<data dir>/plugin.log`. Every line passes through the redactor, which masks — in ini,
JSON and CLI form — `ServerCommandsAuthToken` (a leak of this is a full admin bypass),
`ServiceAuthToken` / `FuncomLiveServices__ServiceAuthToken` (the Funcom FLS JWT),
`Bgd.ServerLoginPassword`, `DatabasePassword`, `RMQ_HTTP_TOKEN_AUTH_SECRET`, and the stock UE
`?Password=` / `?Ticket=` / `?p=` login-URL forms.

## Identity

Per campaign plan Decision 3, Takaro's `gameId` for this connector is the **FLS id** (bare 16 hex) —
that is what the GM command bus and the chat exchanges use. `steamId` comes from
`accounts.platform_id` in Postgres and `platformId` is `steam:<id>`; `name` is
`player_state.character_name`.

The plugin's job is only to emit **something the sidecar can join on**. Its contract is therefore the
weakest useful one: every player object it reports carries at minimum a `characterName` and a
`playerStateId`, and it adds `gameId` (the FLS id) and `steamId` only when it could actually read
them. A field it could not read is **absent, never guessed** — the sidecar joins against Postgres,
which is authoritative.

## Symbol resolution — read this before trusting any address

`DuneSandboxServer-Linux-Shipping` is **stripped**: no `.symtab`, no DWARF, and not one engine or
game *function* symbol. What it does export is the C++ **RTTI**: **42 942 `_ZTV*` vtable symbols**
(`_ZTV7UObject`, `_ZTV6AActor`, `_ZTV14ADuneCharacter`, …) out of ~171 856 `.dynsym` entries. So the
unit of resolution is a **vtable slot**, not a function name, and `/health` reports which strategy
produced each address:

| `how` | meaning |
|---|---|
| `dynsym-vtable` | a `_ZTV…` symbol looked up by `dlsym`. Exact and guaranteed; this is the bedrock |
| `rtti-slot` | a function address read out of a **live** vtable at a slot index derived offline and re-validated at boot |
| `string-xref` | a byte pattern over `.text`, anchored on a **UTF-16LE** string literal, accepted only when it matches **exactly once**. Used for the globals that have neither a symbol nor a vtable |
| `dynsym` | a plain exported name. On this build only `_init`/`_fini`/`_start` — which is precisely why they are the self-check anchors |
| `symcache:<strategy>` | cached from a previous boot of the **same `.note.gnu.build-id`**; a game update invalidates the cache by itself |
| `rejected` | resolved to an address that failed validation, and was therefore discarded rather than used |

Two guards that matter more here than on any previous connector:

1. **This binary is a PIE, so every vtable slot in the on-disk image is `0`** — the real target lives
   in an `R_X86_64_RELATIVE` addend in `.rela.dyn`. The plugin reads vtables from **live, relocated
   memory only**, and refuses an all-zero table with an explicit error instead of quietly resolving
   nothing. Hooks go on live objects' own vtable pointers, never on a file-image base vtable.
2. **"Inside an executable segment" is a useless test here**: the binary has a single R+X `PT_LOAD`
   spanning vaddr `0`…`0x153690d0`, which also contains `.dynsym`, `.rodata` and `.rela.dyn`. Every
   resolved code address is instead checked against `.text` proper **and** against the
   `.eh_frame_hdr` function-entry table (918 821 entries on this build — nothing can strip it,
   because the unwinder needs it). That second check is what catches an off-by-one slot index, which
   a byte-value check cannot see.

### The ProcessEvent slot

There is no `UObject::ProcessEvent` symbol, so the slot index is **derived at boot** and reported in
`/health.diagnostics.resolve.processEventSlotHow`. The decisive test is self-referential: slot *N* is
ProcessEvent only if its body issues an indirect `call [vptr + 8*(N+1)]` and then a conditional
`call [vptr + 8*(N+2)]` (displacements measured from **slot 0**, not from the `_ZTV` symbol) — UE's
`Callspace = GetFunctionCallspace(...); if (Callspace & Remote) CallRemoteFunction(...)`. A wrong slot
cannot fake that. The offline dissection puts it at **slot 86** on this build (`_ZTV7UObject[86] = 0x1023ace0`, with
`call [reg+0x2b8]` / `call [reg+0x2c0]` present in its body); the derivation treats
that as a hypothesis to re-prove, never as an answer.

`processEventSlotConfirmed` is separate and is the only honest proof: it goes `true` when a detour has
actually fired.

## GET /health

```json
{"status":"ok","version":"0.1.0","bootId":"c07f7763cfbf47c9","pid":48,
 "gameBuild":"","engineVersion":"","buildId":"525ee460a821c18f2a8543dfe98522320e82ea57",
 "uptimeMs":26709,
 "capabilities":{"gameThread":"ok","reflection":"ok","players":"unimplemented",
                 "symbols.objectModel":"ok","symbols.enumeration":"degraded", ...},
 "capabilityDetails":{"symbols.enumeration":"unresolved: GUObjectArray", ...},
 "symCache":{"path":"...","buildId":"525ee460...","hit":true,"loadMs":0},
 "diagnostics":{
   "resolve":{"resolved":48,"wanted":71,"required":4,"requiredResolved":4,
              "processEventSlot":86,
              "processEventSlotHow":"derived: slot 86 (callspace triplet [0x2b8,0x2c0] + invariant across 5 non-actor vtables + fingerprint >=3/4; exactly one candidate). agrees with the offline dissection's slot 86.",
              "processEventSlotConfirmed":false,
              "exportedVTables":42942,
              "strategies":[{"name":"dynsym-vtable","available":true,"resolved":41,"ms":—,"detail":"42942 exported vtables; 41 wanted found, 0 absent, 0 rejected"},
                            {"name":"rtti-slot","available":true,"resolved":—,"ms":—,"detail":"..."},
                            {"name":"string-xref","available":true,"resolved":0,"ms":—,"detail":"..."},
                            {"name":"dynsym","available":true,"resolved":3,"ms":—,"detail":"3/3 exported names found"}]},
   "selfChecks":[{"check":"_init: sym=0x... elfSection+slide=0x...","ok":true},
                 {"check":"exportedVTables: 42942 `_ZTV*` symbols in .dynsym ...","ok":true},
                 {"check":"_ZTV7UObject: addr=0x... size=792 slots=97 typeinfo=0x...","ok":true},
                 {"check":"vtableIsLive: 97 slots read from live memory","ok":true},
                 {"check":"processEventSlot: 86 (derived: ...)","ok":true}, ...],
   "reflect":{...},
   "gameThread":{"installed":true,"alive":true,"tickCount":698,"approxHz":28.8, ...},
   "perf":{...},
   "http":{"port":18890,"requests":12,"unauthorized":2,"handlerErrors":0},
   "eventSources":{"sources":[{"event":"entity-killed","status":"degraded","needs":"ADuneCritterBase::OnDeath","needsResolved":true,"mechanism":"..."}, ...],
                   "hooks":[...],"processEventSlot":86,"processEventConfirmed":false},
   "events":{"buffered":0,"latestSeq":0},
   "queries":{"mutations":"none by design - the sidecar mutates over the RabbitMQ GM command bus",
              "capabilities":[{"name":"playerLocation","needs":"_ZTV14ADuneCharacter","needsResolved":true,"status":"unimplemented"}]},
   "hooksInstalled":1,
   "resolved":[...]}}
```

- `status` is `"ok"` only when every self-check passed and `reflection` is `ok`. A degraded plugin
  keeps serving and the server is never affected.
- `capabilities` values are `"ok"`, `"degraded"` or `"unimplemented"`; the reason for a degrade is in
  `capabilityDetails` and **names the missing symbol** (`"unresolved: GUObjectArray"`), because
  "degraded" on its own tells nobody what to fix.
- `"ok"` means resolved and hooked, **not proven**.
- `gameBuild` / `engineVersion` are `""` until the world exists. ⚠️ The UE version is **not embedded
  in this binary** — only the format string `"Unreal Engine version: %s"` is — so it comes from the
  boot log and nowhere else.
- `bootId` is random per server process. A different `bootId` means the server restarted: reset the
  event cursor to `0` even if `seq` is higher than yours.
- `diagnostics` is informational. Do not code against its exact shape.

## GET /events?since=\<seq\>[&limit=\<n\>]

```json
{"bootId":"c07f7763cfbf47c9","seq":328,"latestSeq":330,"truncated":false,
 "events":[{"seq":328,"type":"player-connected","data":{...},"ts":"2026-09-21T18:22:37.107Z"}]}
```

- Ring buffer of the last 5000 events. `seq` is monotonic, starts at 1, resets on restart.
- Returns events with `seq > since`, oldest first, capped at `limit` (default and maximum 5000).
- Pass the response `seq` back as the next `since`.
- `since > latestSeq` means the server restarted; reset to `0`.
- `truncated: true` means events between `since` and the oldest buffered event were dropped.
- Types: `player-connected`, `player-disconnected`, `player-death`, `entity-killed`, `log`.
  **`chat-message` is deliberately not here** — chat comes from the game's own `chat.intercept`
  RabbitMQ exchange, which the sidecar consumes, and which also carries the sender's live position.

### Event payloads (lane L2 — implemented; **fired: 0**, no client can join yet)

⚠️ **There is no `gameId` in any of these payloads, and there never will be.** The FLS id that this
connector uses as Takaro's `gameId` does not exist anywhere in the map server process — lane L1b
checked every UPROPERTY on `DunePlayerState`, `DunePlayerStateBase` and `DunePlayerConnectionInfo`.
What the process *does* hold is the **Postgres identity**, and the sidecar performs the join. See
*Identity, and the join the sidecar performs* below.

```jsonc
// player-connected / player-disconnected — a PRECISE EDGE, not an identity.
{"ref":"acct:12",                 // the plugin's own handle: acct:<id> | ps:<id> | name:<characterName>
 "characterName":"Takaro Tester", // null when the persistence component has not been populated yet
 "accountId":12,                  // encrypted_player_state.account_id  -> accounts.id -> the FLS id
 "playerStateId":9814,            // encrypted_player_state.player_state_id       -> actors.id
 "playerControllerId":9813,       // encrypted_player_state.player_controller_id  -> actors.id
 "playerPawnId":9815,             // encrypted_player_state.player_pawn_id        -> actors.id
 "identityHow":"m_DatabaseAccountId@0xa68 + m_PlayerPersistenceComponent@0x11b0 -> DunePlayerControllerPersistenceComponent",
 "hint":true,                     // ALWAYS true: the sidecar's Postgres poller owns presence
 "identityAuthority":"sidecar/postgres",
 "reason":"logout",               // disconnected only
 "ts":"2026-09-21T18:22:37.107Z"}
```

**A null id is `null`, never `0`.** A zero would invite a join against `actors` row 0.

```jsonc
// player-death — the ATTRIBUTION is what this plugin adds. The sidecar independently sees the
// Postgres life_state edge and COALESCES the two, so exactly one player-death reaches Takaro.
{"ref":"acct:12","characterName":"...","accountId":12,"playerStateId":9814,
 "position":{"x":0,"y":0,"z":0},
 "killer":{"ref":"acct:13","characterName":"...","accountId":13},  // null for an unattributed death
 "killerEntityCode":"DuneNpcCharacter",  // set when the killer is NOT a tracked player; a DEV name
 "killerEntity":null,                    // a display name, which this process never has
 "weapon":null,                          // ditto — see the catalogue rule below
 "weaponCode":"BP_Dart_C",
 "lifeStateCode":2,"deathReasonCode":3,  // raw EDunePlayerLifeState / m_LastDeathReason ORDINALS
 "attribution":"params",                 // ALWAYS present; see the table below
 "via":"ReceiveMulticastDeathOrDefeat",  // which UFunction reported it
 "ts":"..."}

// entity-killed — plugin-only; there is no other source for this at all.
{"entity":null,                  // a DISPLAY name, or null — this process has none (see below)
 "entityCode":"DuneCritterBase", // the victim's UE class
 "entityDevName":"T3_Band_Slv_Reg_Marksman",  // DuneNpcCharacter::m_Name, a data-table row name
 "nameIsClassName":true,
 "player":{"ref":"acct:12",...}, // the KILLER, null when an NPC killed an NPC
 "killerEntityCode":"...",
 "weapon":null,"weaponCode":"...","attribution":"...","via":"...","ts":"..."}
```

`attribution` is always present and is one of:

| value | meaning |
|---|---|
| `params` | the killer came out of the UFunction's own parameters (`InstigatorInfo`) |
| `multicastKill:victimMatch` | correlated with a `ReceiveMulticastKill` whose victim parameter matched |
| `multicastKill:uniqueInWindow` | correlated with the ONLY `ReceiveMulticastKill` in the 1500 ms window |
| `ambiguous` | more than one candidate killer in the window → **`killer` stays null** |
| `none` | the parameters resolved and carried no killer (an environmental death) |
| `paramsUnresolved` | first sighting of a Blueprint override, whose parameter plan is built off-thread; the NEXT one is attributed |

### How a death is seen, and the one thing that is deliberately not trusted

Every death candidate on this build is a `UFUNCTION`, so all of them arrive through the
`UObject::ProcessEvent` detour lane L1b already proved. L2 adds a **name filter** (one 4-byte read of
the UFunction's `FName` comparison index, then a compare against ≤16 entries). Measured live:

| UFunction | class | `self` is | emits? |
|---|---|---|---|
| `OnDeathOrDefeatOnServer` | `DuneCritterBase` | the dying critter | ✅ victim |
| `BPOnDeath` | `DuneCritterBase` | the dying critter | ✅ victim |
| `ReceiveMulticastDeathOrDefeat` | `DuneCharacter` | the character that died | ✅ victim |
| `KillCharacter` | `DuneCharacter` | the character being killed | ✅ victim |
| `ReceiveMulticastKill` | `DuneCharacter` | ⚠️ **probably the killer** | ❌ **hint only** |

⚠️ `ReceiveMulticastKill` **never emits an event.** It is symmetric to
`ReceiveMulticastDeathOrDefeat`, so `self` is most likely the *killer* — and "most likely" is exactly
how a victim and a killer end up the wrong way round in a Discord kill feed (`memory:
hard-test-no-overclaim`, F20). It is recorded as a killer hint, merged only when unique, and its
observed parameter layout is published in `/health.diagnostics.eventSources.functions` so a real kill
can settle the direction instead of an opinion.

**Parameters, as measured on this build (not as assumed):** none of these functions takes a bare actor
pointer. All four take

```
InstigatorInfo : StructProperty @0x00   (16 bytes — the killer lives IN HERE)
bIsDeath       : BoolProperty   @0x10
DeathDefeatCausingDamageType : ClassProperty @0x18
```

so the killer's offset is derived from `InstigatorInfo`'s own `UScriptStruct` (found by a
name-unique sweep of `GUObjectArray`, since a `StructProperty`'s inner struct is not on the FField
chain — `GET /debug/scriptstructs?match=InstigatorInfo` shows it), and every pointer read out of it is
put through `LooksLikeUObject` before its class is touched.

### ⚠️ `bIsDeath` is NOT the death gate (corrected in lane L2b, from live evidence)

L2 shipped `bIsDeath == false → drop`, reasoning that a "defeat" was a downed-but-not-out
transition. **On the live server that gate discarded 100 % of real deaths**: after a player's own
environmental death and an NPC kill, `/health` read
`hits.ReceiveMulticastDeathOrDefeat: 2`, `defeatsDropped: 2`, `emitted.entity-killed: 0`. Both
hypotheses were tested:

* **Bitfield misread — FALSIFIED.** A UE `FBoolProperty` *can* be a C++ bitfield, where the byte at
  the property offset is shared and only `ByteMask` selects the bit, so a whole-byte read returns a
  neighbour's value. But the reflected layout shows these bools at **distinct consecutive byte
  offsets** — `ReceiveMulticastDeathOrDefeat`: `bIsDeath@0x10, bForceKeepInventoryOnDeath@0x11,
  ShouldReportTelemetry@0x20`; `KillCharacter`: `bShouldEnterDbno@0x10, bIsDeath@0x11,
  bForceKeepInventoryOnDeath@0x12, bShouldSendDeathEventMessage@0x13`. Packed bitfields **share**
  one offset and differ only by mask. These do not, which is exactly what UHT emits for function
  parameters (a parameter is always a whole `bool`). The whole-byte read was correct.
* **Semantics — CONFIRMED.** The knock-down case has its **own dedicated parameter**:
  `bShouldEnterDbno` — DBNO, "down but not out". So `bIsDeath == false` is not a knock-down; it is
  Dune's ordinary lethal death, the one the game's own UI prints as **DEFEATED**.

**The gate is therefore `bShouldEnterDbno`, plus a player-only life-state read-back**, and
`bIsDeath` is kept as a descriptor. The decision table (pure and unit-tested in
`events_parse.cpp::DecideDeath`, 8 cases + an exhaustive totality sweep):

| # | condition | verdict |
|---|---|---|
| 1 | the call is `ReceiveMulticastKill` | **hint** — never an event; direction unproven (F20) |
| 2 | the victim is already in the 1500 ms dedupe window | **drop:duplicate** |
| 3 | `bShouldEnterDbno == true` | **drop:knockDown** — down but not out, unconditional |
| 4 | victim is a player, `bIsDeath != true`, and its `PlayerState.m_LifeState` still reads `Alive` on a **fresh** (≤5 s) read-back at drain time | **drop:knockDown** |
| 5 | otherwise | **emit**, `deathKind` = `death` / `defeat` / `unknown` |

Rule 4 can only ever *refuse*, never assert: an unreadable or stale life state is ignored, and
`bIsDeath == true` overrides it. NPCs are never gated on it — `DuneNpcCharacter` has no dead flag
and no revive mechanic on this build, so the event firing *is* the death.

**Counters.** `defeatsDropped` is retained at `0` so `/health` stays a superset of L2's shape; the
real reasons are `knockDownsDropped`, `duplicatesDropped`, `lifeStateReadbacks` and
`boolBytesUnreadable` (the bitfield canary — a bool byte that was neither 0 nor 1; a non-zero value
here means a future build *did* pack them and the offsets must be re-derived).

### `/events` payload additions (backward compatible)

`player-death` and `entity-killed` gained two **additive** fields. Every pre-existing field keeps
its name, type and meaning, so a consumer that ignores these gets exactly L2's intended behaviour:

| field | type | meaning |
|---|---|---|
| `deathKind` | `"death" \| "defeat" \| "unknown"` | descriptive only. On this build `defeat` is the ordinary lethal death |
| `isDeathFlag` | `true \| false \| null` | the raw `bIsDeath` parameter, `null` when absent or unreadable |

⚠️ **Catalogue rule** (`memory: catalogue-human-names`): `entity` must be a display name, never a
dev/class name. **Measured, and better than expected:** `DuneNpcCharacter::m_Name`
(`NameProperty@0x2280`) holds a real display name on this build — the live sweep returned
*Mobula Gang Member*, *Slaver Trapper*, *Ariste Atreides*. (The AI spawner's LOG speaks in row keys
like `T3_Band_Slv_Reg_Marksman`, which is what made the offline reading look dev-ish.) So the plugin
sends `entity: <that name>` with `nameIsClassName: false` **only when the string passes the same
display-name predicate the sidecar's catalogue generator uses**; otherwise it sends `entity: null`,
`nameIsClassName: true`, and the sidecar names it from its own catalogue or drops it. `weapon` has no
such source, which is why only `weaponCode` is ever populated.

⚠️ **Enum names are not reflected.** `m_LifeState` / `m_LastDeathReason` are read as raw ordinals
(`lifeStateCode` / `deathReasonCode`), because a `UEnum`'s `Names` array has no derived offset on this
build. The sidecar already has the authoritative cause from Postgres `life_state`, so nothing is lost
and nothing is invented.

### ⚠️ A player victim is decided by the CLASS CHAIN (lane L2c, from live evidence)

**The bug.** `/events` **seq 4** (17:35:27Z) and **seq 5** (17:45:51Z) were `TakaroTest`'s own deaths
and were emitted as:

```json
{"type":"entity-killed","entity":null,"entityCode":"BP_DunePlayerCharacter_C",
 "nameIsClassName":true,"player":{"ref":"acct:1","characterName":"TakaroTest"}}
```

i.e. *an unnamed creature was killed by TakaroTest*, when in fact **TakaroTest died**. Seq 1 and
seq 9 — the same function, the same build — were correct `player-death`s.

**The root cause**, from `/debug/deathlog` (the raw frames were still in the ring). The victim pointer
in the first 8 bytes of `rawParams` is a **different object on every death**:

| plugin seq | victim pointer | `victimIsPlayer` | emitted |
|---|---|---|---|
| 1 (16:59:56Z) | `fa0b1700b6040900` | **true** | `player-death` ✅ |
| 3 (17:30:12Z) | `72f71700d2860900`*(killer)* | n/a — NPC victim | `entity-killed` ✅ |
| 5 (17:35:27Z) | `72f71700d2860900` | **false** | `entity-killed` ❌ |
| 7 (17:45:51Z) | `4e581700fb8d0900` | **false** | `entity-killed` ❌ |
| 9 (17:59:23Z) | `52471600659b0900` | **true** | `player-death` ✅ |

`Players::Identify` was **pure pointer equality** against `Entry::{controller, playerState, pawn}`,
and `Entry::pawn` went stale on respawn (see *`positionSource` semantics* above). Note row 5: its
victim pointer is **literally row 3's killer pointer** — the same pawn, recognised as the killer five
minutes earlier and unrecognised as the victim. The killer never suffered this because a killer is
resolved through `InstigatorInfo::m_Controller`, and **a controller pointer survives a respawn**.
That asymmetry is exactly why the events looked half-right.

**The rule now implemented** (pure and unit-tested: `EventsParse::ClassifyVictim`):

| # | condition | verdict |
|---|---|---|
| 1 | the victim's **UClass Super-chain** derives `DunePlayerCharacter` / `DunePlayerControllerBase` / `DunePlayerState` | **`player-death`**, always. `identityComplete` = whether an identity was also resolved |
| 2 | the chain did not say player, but the **registry** resolved the victim | **`player-death`**, `identityComplete: true` |
| 3 | otherwise (a genuine non-player victim) **with** a display name | **`entity-killed`**, `dropUnnamed: false` |
| 4 | otherwise **without** a display name | **`entity-killed`**, `entity: null`, **`dropUnnamed: true`** |

A class chain is readable for as long as the object is, needs no registry and survives every respawn,
which is why it — and never the registry — decides the event type. **Identity is a separate,
best-effort second step**, and it now has a fallback of its own: `Players::IdentifyActor` walks a
pawn's own `Controller` / `PlayerState` links back to a tracked controller and **adopts the live pawn
into the registry**, so the registry self-heals.

**A player victim can therefore never be emitted as `entity-killed` again**, whatever the registry
says. `/health.diagnostics.eventSources.classification.victimClassChainOnly` counts the cases where
the class chain caught a player death the registry missed — the exact case above.

### `/events` additions from lane L2c (all additive)

| event | field | type | meaning |
|---|---|---|---|
| `player-death` | `identityComplete` | `bool` | `false` = the class chain proved a player died but no identity attached. `ref` is then `"unknown"` and **the sidecar must resolve the player from its own Postgres presence**. The event is still real |
| `player-death` | `identityHow` | `"pointer" \| "Controller" \| "PlayerState" \| null` | which route identified the victim |
| `player-death` | `victimClass` | `string \| null` | the victim's class, for diagnostics |
| `player-death` | `classifiedBy` | `string` | `"victim class chain derives a player class"` or `"player registry"` |
| `player-death` | `lifeStateCode` / `deathReasonCode` | `int \| null` | **now always present** (`null` when unreadable) instead of absent |
| `player-death` | `deathReason` | `null` | reserved. Always `null` — see the enum note below |
| `entity-killed` | `dropUnnamed` | `bool` | **an instruction, not a hint.** `true` ⇒ no human display name exists ⇒ **DROP the event**. A class name must never become a creature's name in Takaro (`memory: catalogue-human-names`) |
| `entity-killed` | `classifiedBy` | `string` | why this was classified as a non-player victim |
| both | `damageTypeCode` | `string \| null` | the damage-**type** class's own name |
| both | `weaponNote` | `string` | how to read `weaponItemCode` vs `damageTypeCode` |
| both | `weaponItemCode` | `string \| null` | **lane L2d.** The **item template id** of the weapon the KILLER was wielding — the same code space as Postgres `items.template_id` and the sidecar catalogue. `null` when no weapon could be resolved |
| both | `weaponSource` | `string` | which read answered: `meleeCache:damageTypeClassMatch`, `weaponComponent:damageTypeClassMatch`, `meleeCache:meleeDamageType`, `weaponComponent:inHand`, `none` |
| both | `weaponConfidence` | `0 \| 1 \| 2` | `2` = the damage-type **class pointers matched**; `1` = category / in-hand evidence only; `0` = no weapon |
| both | `weaponReason` | `string` | the sentence behind that decision, verbatim from the pure decider |
| `player-connected` | `playerName` | `string \| null` | the **account persona** (`APlayerState::PlayerNamePrivate`), reported separately now |
| `player-connected` | `characterNameSource` | `"persistence:m_CharacterName" \| "playerState:PlayerNamePrivate" \| "none"` | which of the two `characterName` actually is |
| `player-connected` | `identityComplete` | `bool` | `false` = announced after the grace expired with a partial identity |
| `player-connected` | `announceDelayMs` | `int` | how long the announce was held |

### ⚠️ Self and environmental deaths carry `killer: null` (lane L2c)

`/events` **seq 10** reported `killer: {"ref":"acct:1","characterName":"TakaroTest"}` on
`TakaroTest`'s **own** death. The engine points `InstigatorInfo` at the victim when nothing else
killed them, so **"the killer is the victim" is the engine's way of saying "the environment did it"** —
a fall, dehydration, a sandworm, a Coriolis storm. Reported literally it would have produced a suicide
for every environmental death.

So when the resolved killer is the victim itself — the same actor, the victim's own pawn / controller /
player state, or a killer that resolves to the **same tracked player** — the plugin emits:

```json
{"killer":null,"killerEntityCode":null,"attribution":"self-or-environment"}
```

`attribution: "self-or-environment"` is the exact string (one constant,
`EventsParse::kSelfAttribution`, so the plugin and the sidecar cannot drift). Counted as
`classification.selfOrEnvironmentDeaths`.

**The cause is NOT decoded to a name.** `deathReasonCode` carries the raw
`DunePlayerState::m_LastDeathReason` ordinal and `deathReason` is always `null`, because a `UEnum`'s
`Names` array has no derivable offset on this build (see the enum note above) — and the sidecar
already has the authoritative cause from Postgres `life_state`
(`Alive` / `Dead` / `DeadByCoriolis` / `DeadBySandworm`). Inventing a mapping from an undecoded
ordinal is exactly the kind of claim this connector does not make.

### ⚠️ `weaponCode` was the METACLASS, not a weapon (lane L2c)

Every live death carried `weaponCode: "BlueprintGeneratedClass"`.
`DeathDefeatCausingDamageType` is a **`ClassProperty`**, so the 8 bytes in the parameter frame **are a
`UClass*`**, not a pointer to an instance of one. Reading "the class of the object at this pointer"
therefore answered "the class of a class".

The plan now records, per role, whether the property **holds a class**
(`EventsParse::PropertyHoldsClassPointer`), and the name is read accordingly. `weaponCode` is filled
from three sources in descending order of how much they say about the kill:

1. a weapon/damage-type parameter that is an **object** reference — the real thing;
2. the **`DamageCauser`** actor's class — the projectile / weapon / hazard actor that did it;
3. the damage-**type** class's **own** name (a category, e.g. a fall damage type).

`damageTypeCode` always carries (3) separately, so "shot with X" and "died of Y" no longer share one
field. **`weapon` stays `null`**: no weapon display name exists in this process, and per
`memory: catalogue-human-names` a dev name must never be published as a display name — the sidecar
joins `weaponCode` / `damageTypeCode` against its own catalogue.

### The wielded weapon, read off the KILLER (lane L2d)

**The defect.** Takaro showed `weapon: ""` on every kill. Tester's two melee kills came through as
`weaponCode: "BP_DmgType_Melee_Quick_C"` / `"BP_DmgType_Melee_Slow_Unshielded_C"` with
`damageCauserCode: null` — because `ReceiveMulticastDeathOrDefeat` carries **only the damage-TYPE
class** and there is no damage causer in the frame at all. No amount of frame parsing can produce a
weapon; it has to be read off the **wielder**.

**What the live process models** (read with `/debug/object` on a real NPC while a player was online,
2026-09-21 — all offsets are resolved by property NAME at init, never hardcoded):

| class | property | type | live value |
|---|---|---|---|
| `ADuneCharacter` | `m_WeaponComponent` | ObjectProperty @0x10b0 | → `UWeaponActorComponent` named "Weapon" |
| `UWeaponActorComponent` | `m_WeaponName` | NameProperty @0x718 | **`"ChoamSda2"`** |
| `UWeaponActorComponent` | `m_CachedWeaponDamageType` | ClassPtrProperty @0x148 | the damage type this weapon deals |
| `UWeaponActorComponent` | `m_bActive` | BoolProperty @0x224 | `true` |
| `ADuneCharacter` | `m_CachedMeleeWeaponData` | StructProperty @0xab0 | inline `FCachedMeleeWeaponData` |
| `FCachedMeleeWeaponData` | `m_CachedMeleeWeaponName` | NameProperty @0x14 | the melee weapon |
| `FCachedMeleeWeaponData` | `m_DamageTypeClass` | ClassProperty @0x110 | the damage type that melee deals |
| `ADuneCharacter` | `m_bHasWeaponInHand` | BoolProperty @0x1bce | ⚠️ **a packed bitfield pair** — see below |

**`m_WeaponName` is an ITEM TEMPLATE ID.** The live NPC's `"ChoamSda2"` is a verbatim
`items.template_id`, and the sidecar catalogue maps it to **"Maula Pistol"**. That is what makes this
worth doing: a damage-type class can never become a display name, and an item template id always can.

**The discriminator is the damage type, not the slot.** A character carries a cached melee weapon AND
a weapon component at all times, so "which one killed" is decided by comparing the death frame's
damage-type **class pointer** against the two cached damage-type class pointers — a pointer compare, no
dereference, no name matching. The full decision is pure and unit-tested
(`EventsParse::DecideWieldedWeapon`, 128-case totality sweep):

| # | condition | result |
|---|---|---|
| 1 | the melee cache's damage-type class **is** the one that killed, and it has a name | the melee weapon, confidence **2** |
| 2 | the weapon component's cached damage type **is** the one that killed, and it has a name | that weapon, confidence **2** |
| 3 | the damage type only *says* melee, and a melee name is cached | the melee weapon, confidence **1** |
| 4 | a non-melee damage type and a weapon actually in hand / active | that weapon, confidence **1** |
| 5 | otherwise | **no weapon**, with the reason |

Rule 3's `else` branch is the one that matters most: **a melee damage type with no cached melee name
yields nothing rather than the firearm the killer also carries.** Naming the wrong weapon confidently
is worse than the empty string this lane set out to fix.

**Thread discipline** (memory: avoid-game-thread). The hook path copies out primitives only — two FName
`{index, number}` pairs, two class **pointers** (only ever compared) and two flags, at most nine guarded
reads, and **nothing new in the ~6 000 calls/s filter**. Off-thread the drain decodes the FNames and
compares pointers; **no engine object is dereferenced again after the hit is enqueued**, so a killer who
died, logged out or was collected in between cannot be touched at all.

**The bitfield canary, again.** `m_bHasWeaponInHand` and `m_bServerWantsWeaponInHand` reflect at the
**same byte offset** (7134) and the FField chain here exposes no `ByteMask`, so the two cannot be told
apart by a whole-byte read (the same trap L2b checked for and cleared on the death bools — this time it
is real). The collision is detected at init by comparing the two resolved offsets, reported as
`weapon.weaponInHandIsPackedBitfieldPair`, and the byte is then read as *"either weapon-in-hand flag is
set"*. That flag is only ever **corroborating** evidence in rule 4 and can never refuse a weapon a
damage-type class match already proved, so the ambiguity cannot cost a correct answer.

**A self or environmental death reports no weapon**, deliberately: the instigator is the victim, and
"killed himself with his own knife" is not a sentence this connector will publish.

`/health.diagnostics.eventSources.weapon` reports `resolved`, `byDamageTypeClassMatch`,
`noKillerCharacter`, `noEvidence`, every resolved `offsets.*` (`-1` = this build renamed it and the
weapon degrades to `null`) and `meleeStruct`. `/debug/deathlog` rows gain a `weapon` object carrying
**both** candidate names side by side with the two match flags — the same "show what disagreed" rule the
`victimIsPlayer` / `victimClassIsPlayer` pair follows.

**`weaponCode` compatibility.** It now carries the item template id **when one was resolved**, and
otherwise keeps exactly its previous value (the damage-type / causer class). `weaponItemCode` is the
unambiguous field: it is *only* ever an item template id. `weapon` is still always `null` — the plugin
does not own display names (memory: catalogue-human-names).

### The delayed `player-connected` announce (lane L2c)

`/events` seq 1, 7 and 9 all announced `characterName: "Tester"` — the **account persona** — with every
actor id `null`, while `/players` a moment later reported the real character `TakaroTest`. At
`PostLogin` the controller's persistence component has not been populated, so `m_CharacterName` is
empty and the payload fell through to `APlayerState::PlayerNamePrivate`.

The announce is now **held and retried** (the simpler of the two options; no correction event for the
sidecar to reconcile):

| condition | action |
|---|---|
| `accountId` **and** the persistence `m_CharacterName` are both readable | **announce**, `identityComplete: true` |
| not yet, and the connect is **younger than 10 s** | **hold**; retried on the ~2 s housekeeping pass, each retry forcing a game-thread identity re-read |
| not yet, and **10 s have passed** | **announce anyway** with whatever is readable, `identityComplete: false` |

**A connect is never lost** — only ever delayed by at most 10 s or flagged. A player who leaves while
still held gets **no** `player-connected` *and* **no** `player-disconnected`, rather than a disconnect
for a connect nobody ever saw (counted as `connectAnnounce.abandoned`). The sidecar keys on
`accountId`, so this whole area is cosmetic for the join — but a connect announcing the wrong name is
exactly the kind of thing that gets believed later. Diagnostics:
`/health.diagnostics.eventSources.connectAnnounce`.

## Identity, and the join the sidecar performs

`DunePlayerControllerPersistenceComponent` (read live, no instance needed) carries exactly the
Postgres identity:

| plugin property | offset | Postgres column | type |
|---|---|---|---|
| `DunePlayerControllerBase.m_DatabaseAccountId` | `0xa68` | `encrypted_player_state.account_id` | `bigint` → `encrypted_accounts.id` |
| `m_AccountID` | `0x1b0` | same | `bigint` |
| `m_PlayerStateUniqueID` | `0x1b8` | `player_state_id` | `bigint` → `actors.id` |
| `m_PlayerControllerUniqueID` | `0x1c0` | `player_controller_id` | `bigint` → `actors.id` |
| `m_PlayerCharacterUniqueID` | `0x1c8` | `player_pawn_id` | `bigint` → `actors.id` |
| `m_CharacterName` | `0x1e8` | `decrypt(encrypted_character_name)` | `FString` |

`accounts` is the view that turns an `encrypted_accounts.id` into the FLS id in the clear, so the
primary route is **one hop on a primary key**: plugin `accountId` → roster row → `flsId`. Every offset
above is read from the class's own FField chain by property NAME, so none of them is a constant.

**What is still unproven until the first join:** that each 8-byte id struct really holds that bigint
(the offsets only prove the field is 8 bytes wide — the struct's inner layout is not reflected), and
that the values equal the Postgres rows. The sidecar's join therefore cross-checks `accountId` against
the character name and **resolves to nothing when the two disagree**, rather than picking a player.

## Presence: two mechanisms, one of which is not a vtable slot

`player-connected` / `player-disconnected` used to come only from `AGameModeBase::PostLogin` (slot
313) and `Logout` (slot 315) on the live `ADuneSandboxGameModeBase` object. Lane **L6b-0** changed
that, because a roster that depends on one slot fails **silently and completely**: no exception, no
degraded capability, just an empty server forever. (What L6b-0 was sent to explain turned out to be
something else entirely — the client was never on the map server at all, because Dune's prologue map
`NPE2_Main` runs `NetMode: Standalone` on the client. The hooks may be perfectly correct. The
single-point-of-failure is a problem either way.)

So there are now two independent mechanisms, and the **reconciler**, not the hook, defines the
connected set:

| mechanism | what it is | latency |
|---|---|---|
| `hook:PostLogin` / `hook:Logout` | the vtable detours — a same-frame, precise **hint** (fast path) | same frame |
| `sweep` | a low-rate `GUObjectArray` sweep for live `PlayerController`s whose reflected `APlayerController::NetConnection` **or** `Player` is a valid UObject | ≤ `TAKARO_PRESENCE_SWEEP_MS` for a connect, ≤ 2× that for a disconnect (debounce) |

Both write through the same player registry, and the registry is the dedupe seam: whichever sees an
edge first records it, and the other one refreshes instead of emitting a second event. Every
`player-connected` / `player-disconnected` payload therefore carries a `source` field naming the
mechanism that saw it, and a disconnect the sweep confirmed says `"reason":"connection-lost"` rather
than `"logout"` — the sweep saw the connection go away, which is not the same statement as "the game
called Logout".

The sweep runs on the **housekeeping** thread (guarded reads only, no call into the game, like
`/entities`). The only game-thread work is reading a NEW player's identity, queued once per join; if
the pump is unavailable the connect is **not** announced and is retried on the next sweep, because a
player with no ref is worse than a player a second late.

Two absences are required before a disconnect, so one unreadable sweep — or the moment during a
partition hand-off when a controller is briefly connection-less — cannot produce a
disconnect/reconnect flap.

`/health.diagnostics.presence` reports it:

```json
{"mechanism":"low-rate GUObjectArray sweep of live PlayerControllers holding APlayerController::{NetConnection,Player}; ...",
 "enabled":true,"intervalMs":2000,"sweeps":412,"skippedNoClassIndex":18,
 "lastSweep":{"objects":37275,"playerControllers":1,"cdosSkipped":3,"connected":1,"durationUs":21000},
 "maxDurationUs":48000,"tracked":1,"connectsAnnounced":1,"disconnectsAnnounced":0,
 "connectAnnounceRetries":0,"debouncedAbsences":0,"note":"..."}
```

and `/health.diagnostics.liveHooks.lifecycle` now also reports whether the hook is still where we put
it: `gameModeRevalidations`, `gameModeReplacements`, `slotRepairs`, `detourStillInstalled`. The
lifecycle install used to latch forever; it is now re-checked every housekeeping pass (is our detour
still in the slot?) and the live game mode is re-located every 30 s, so a world reload that
instantiates a new game-mode object re-hooks instead of going quietly dead.

## GET /players

```json
{"generation":7,"snapshotAgeMs":142,"snapshotFresh":true,
 "authority":"plugin-observed (PostLogin); the roster and `gameId` are the sidecar's, from Postgres",
 "count":1,
 "players":[{"ref":"acct:12","characterName":"Takaro Tester","accountId":12,"playerStateId":9814,
             "playerControllerId":9813,"playerPawnId":9815,
             "position":{"x":0,"y":0,"z":0},"positionSource":"pawn","ageMs":142,
             "identityHow":"..."}]}
```

The snapshot is **lazy and generation-stamped**: it is refreshed on demand when it is older than
`TAKARO_SNAPSHOT_TTL_MS`, so with nobody asking, the game thread is never entered at all. `generation`
bumps only when something moved, and `snapshotAgeMs` / `ageMs` are always reported — the pump has no
guaranteed cadence (see *Pump cadence* below), so staleness is stated rather than assumed away.

## GET /players/{ref}

One player, or `404`. `ref` may be the plugin's own ref, any of the four ids on its own, or the
character name. **An FLS id is a `404` by design** — the plugin has never seen one, and the sidecar
resolves the ref first (`pluginJoin.ts`) instead of getting a wrong player back.

## GET /players/{ref}/location

```json
{"x":0,"y":0,"z":0,"pitch":0,"yaw":0,"source":"pawn","generation":7,"ageMs":31,
 "snapshotAgeMs":31,"at":"2026-09-21T18:22:37.107Z"}
```

`source` is **always** present: `pawn` (the live pawn's root-component transform — the reason this
plugin exists), `playerState` (a weaker fallback) or `unknown`. The position comes from
`AActor::RootComponent` → `USceneComponent::RelativeLocation`, both by reflected offset;
`RelativeLocation@0x1c8` and `RelativeRotation@0x1e0` being exactly 24 bytes apart is also what proves
`FVector` is three **doubles** on this build. A `404`/`503` is what makes the sidecar fall back to the
chat origin and then to the last-saved Postgres row, so a stale answer is never returned here.

### ⚠️ `positionSource` semantics — corrected in lane L2c, and now safe for the sidecar to trust

The sidecar logged **`plugin-origin-rejected:playerState` 24 times**, and it was right to. The cause
was not the fallback itself but **a stale pawn pointer**:

* `Entry::pawn` was captured at `PostLogin` and `Refresh()` only re-read it **when it was null**.
* Dune gives a player a **brand new pawn on every respawn**, so after the first death the cached
  pointer referred to the previous life's object.
* `ReadLocation` then fell through to `e.playerState` — and an `APlayerState` is a **non-spatial
  actor whose root component sits at the world origin**. The result was a syntactically valid
  `{"position":{"x":0,"y":0,"z":0},"positionSource":"playerState"}`.

The same staleness was the root cause of the death misclassification below, which is why one fix
serves both.

**The rule now implemented** (pure and unit-tested: `EventsParse::DecidePositionSource`):

| pawn has a position | playerState has a position | that position is the origin | `positionSource` | `position` |
|---|---|---|---|---|
| yes | — | — | `pawn` | the pawn's |
| no | yes | **no** | `playerState` | the playerState's |
| no | yes | **yes** | `unknown` | **`null`** |
| no | no | — | `unknown` | **`null`** |

and, underneath it, **the pawn is re-resolved from `AController::Pawn` (then
`APlayerState::PawnPrivate`) on every refresh** rather than cached across lives.

**What the sidecar may now assume:**

1. `positionSource: "pawn"` — trust it. A pawn's position always wins when a pawn answered at all.
2. `positionSource: "playerState"` — a real, **non-origin** position. It is weaker (it is whatever
   actor the engine put there), but it is no longer the origin artefact, so **rejecting it on the
   grounds of being at the origin is now dead code**. Keep the sidecar's own sanity bound if it wants
   one; it will not fire for this reason.
3. `positionSource: "unknown"` — there is **no** `position` field value to read (`null`). This is the
   honest replacement for the origin answer, and it is what the sidecar should treat as "ask
   Postgres".

`/health.diagnostics.players` reports `pawn.reresolves` (how often the live pawn differed from the
cached one — i.e. how often the old bug would have fired), `pawn.adoptedViaOwnerWalk`,
`position.playerStateOriginRejected` and `position.unreadable`.

## GET /entities

```json
{"liveActors":10,"entitiesReturned":6,"namedActors":10,"unnamedActors":0,
 "nameSource":"DuneNpcCharacter::m_Name (a NameProperty holding a DISPLAY name on this build, verified live) ...",
 "entities":[{"code":"Mobula Gang Member","name":"Mobula Gang Member","nameIsClassName":false,
              "class":"BP_Npc_SoldierBase_Character_Baked_C","liveCount":2}]}
```

A `GUObjectArray` sweep by class chain (`DuneCritterBase`, `DuneStaticCritterBase`,
`DuneNpcCharacter`, `DuneNpcCharacterCivilian`), class default objects excluded, aggregated **per
distinct entity rather than per class** — one UE class is shared by dozens of differently-named NPCs,
so the class is the wrong key for a catalogue. It runs **off the game thread** (pure guarded reads),
so it costs the engine nothing.

## Pump cadence, and why nothing depends on it

The game-thread pump rides `ProcessEvent`, so it has no guaranteed cadence: lane L1b measured a
**17 960 ms** gap while the engine was flushing level streaming. Nothing in the plugin depends on
cadence for correctness — events are timestamped and enqueued **on the hook path itself** into a
lock-free single-writer ring, and `/players` reports its own staleness. The stalls are measured
anyway, in `/health.diagnostics.gameThread.watchdog`:

```json
{"maxStallMs":17960,"lastStallMs":17960,"stallsOver1s":3,"stallsOver5s":1,
 "cadenceIsNotRequired":"events are timestamped and enqueued on the hook path; /players reports snapshot staleness"}
```

## Debug endpoints

All need the token **and** `TAKARO_PLUGIN_DEBUG=1`; otherwise `404`.

| endpoint | purpose |
|---|---|
| `GET /debug/gamethread` | runs a no-op job on the game thread and reports the thread id and latency — proof that the pump works |
| `GET /debug/perf[?reset=1]` | the game-thread performance counters (see `docs/gamethread-policy.md`) |
| `GET /debug/symbols` | the full resolved table with `how`, slot index and validation state |
| `GET /debug/object?path=/Script/DuneSandbox.X` or `?ptr=0x…` | JSON dump of a UObject's property tree. A `ptr` is validated (`MemReadable`, then the vtable-identity check) before it is dereferenced |
| `GET /debug/structs?name=…` | dump a `UStruct`/`UClass` by name |
| `GET /debug/players` | the player registry with its raw pointers and the identity audit trail |
| `GET /debug/deathlog[?limit=n]` | the last 32 calls the death filter caught, newest first, with the **raw parameter frame in hex** and the exact decision taken on it: `bIsDeath`, `bShouldEnterDbno`, `lifeStateAtDrain`, `drainLagMs`, `decision`, `reason`, `deathKind`, `attribution`, `emitted` — plus, from lane L2c, **`victimIsPlayer` and `victimClassIsPlayer` side by side** (what the registry said vs what the class chain said; the whole misclassification was those two disagreeing while only the first was consulted), `victimIdentityHow`, `identityComplete`, `dropUnnamed` and `classification`. This is the audit trail L2 did not have — it is what turns "the death was dropped" into "the death was dropped *for this reason, on these bytes*" |
| `GET /debug/params?class=X&func=Y` | one UFunction's parameter properties — how the victim/killer/weapon roles get settled against the real build instead of an assumption |
| `GET /debug/scriptstructs?match=Instigator` | every live `UScriptStruct` matching a fragment, with its members. This is how `InstigatorInfo`'s layout — and therefore the killer — is derived |
| `GET /debug/npcs[?ref=<player>&limit=n]` | every live NPC/creature actor with its class, dev name, `dead`, `killable` and (with `ref`) its distance. The human-correlatable kill-proof route, and the answer to "does the idle world contain any killable entity at all" |
| `POST /debug/kill-nearest {ref? \| origin:{x,y,z} \| any:true, radius?, dryRun?}` | kills the nearest killable NPC through the game's **own `KillCharacter` UFUNCTION**, dispatched with the engine's **own `ProcessEvent`** — never by writing a health field. `origin`/`any` exist so the DETECTION half is provable on an **empty server**. ⚠️ The parameter frame is zero-filled, so the kill has **no instigator**: it proves detection, never attribution. `ADuneCritterBase` does not derive `ADuneCharacter` and so has no `KillCharacter` — for critters, use `/debug/npcs` and have a human kill one |

## Degrade behaviour

- A missing vtable or an unresolved slot degrades **exactly one** capability, with the reason in
  `capabilityDetails`, and the server keeps running.
- `DEBUG_CORRUPT_SIG=<wanted name>` produces a deliberately broken build (`./build.sh` passes it into
  the container) that discards exactly that one name. That is the degrade proof: one capability goes
  `degraded`, everything else stays up, the server survives.
- The preload is **inert in every process that is not the map server**: the library constructor
  returns before creating a thread unless `/proc/self/exe`'s basename is exactly
  `DuneSandboxServer-Linux-Shipping`. Dune's launch chain is `run.sh` → `su` → `runuser` → `bash` →
  `DuneSandboxServer.sh` → the ELF, and `run.sh` also invokes `lsof` and `sshd`; an `LD_PRELOAD` set
  on the container reaches all of them.
