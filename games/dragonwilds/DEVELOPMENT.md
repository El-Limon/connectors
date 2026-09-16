# Dragonwilds connector — development

Operator documentation is in [README.md](README.md). This file is for people who build,
change or re-verify the connector.

## Layout

```
mod/       C++17, builds libtakaro-dragonwilds.so (LD_PRELOAD into the dedicated server)
  src/       sym.cpp reflect.cpp gamethread.cpp hooks.cpp http.cpp actions.cpp events.cpp state.cpp common.cpp main.cpp
  docs/      API.md — the plugin's HTTP contract (the source of truth for both sides)
  tests/     host unit tests (.sym parser, JSON, ring buffer, redaction)
  tools/     symdump.py — offline .sym inspection
  build.sh, Dockerfile.build, Makefile
sidecar/   Node 22 + TypeScript, speaks the Takaro Generic Connector Protocol
scripts/   build-release.sh
docker-compose.example.yml, .env.example, version.txt, CHANGELOG.md
```

**Ownership rule: game truth lives in the plugin, Takaro protocol shape lives in the sidecar.**
Never parse Takaro DTOs in C++; never guess game state in TypeScript. Change `mod/docs/API.md`
first, then both sides.

## Architecture

```
RSDragonwildsServer-Linux-Shipping (UE 5.6.1, Linux dedicated server)
  └─ LD_PRELOAD=/opt/takaro/libtakaro-dragonwilds.so
       ├─ resolves engine/game functions by demangled name from the depot's own .sym (never fixed RVAs)
       ├─ reads every UPROPERTY offset via UStruct::FindPropertyByName at runtime
       ├─ hooks PostLogin / OnNetCleanup / PreLogout / Logout / ProcessEvent by vtable slot swap
       ├─ marshals all UObject work onto the engine tick (bounded job queue, 5 s timeout -> 503)
       └─ HTTP API on 127.0.0.1:18890, Bearer token — see mod/docs/API.md
sidecar/ (Node 22, TypeScript) — same network namespace as the game server
  ├─ polls /events with a persisted cursor + bootId (no replay after a restart)
  ├─ reconciles the online player set (emits disconnects after a server crash)
  ├─ tails the server log for join/leave grammar and log events (secrets redacted)
  ├─ lifts timed bans itself (Takaro sends no unbanPlayer at expiry)
  └─ outbound WebSocket to Takaro (identify, action request/response, gameEvent)
```

## Build and test

```bash
./mod/build.sh                 # debian:bookworm container -> mod/dist/libtakaro-dragonwilds.so + SHA256SUMS
./mod/build.sh --native        # on the host (needs g++ >= 10)
./mod/tests/run.sh             # plugin host unit tests
cd sidecar && npm ci && npm run typecheck && npm test && npm run build
./scripts/build-release.sh     # both, packaged into dist/ with SHA256SUMS
```

The toolchain image is `debian:bookworm` on purpose: it matches the glibc of the dedicated-server
image, so the `.so` loads without a symbol-version error. Flags are
`-std=c++17 -O2 -fPIC -fvisibility=hidden`, linked `-shared -pthread -ldl -static-libstdc++ -static-libgcc`.

`DEBUG_CORRUPT_SIG=<symbol> ./mod/build.sh` builds a `.so` with one resolution deliberately broken.
That build must still load, keep the server running, and report exactly that capability as
`degraded` in `/health` — it is how the degrade path is proven.

The plugin version is a compile-time constant in `mod/src/common.h`
(`// x-release-please-version`); release-please bumps it together with `version.txt` and the
changelog. Never hand-edit it.

## Symbol resolution (`.sym`) and reflection

The Dragonwilds depot ships `RSDragonwildsServer-Linux-Shipping.sym` next to the stripped server
binary. Format: `u32 N`, then N × 20-byte records `{u64 rva, u32 line, u32 fileOff, u32 nameOff}`
sorted by rva, then a `\n`-separated table of demangled names. **`vaddr = rva + the first
PT_LOAD p_vaddr`**, read from the ELF at runtime — never hard-coded.

The resolved set is cached in `<serverdir>/takaro/symcache.json`, keyed by the ELF
`.note.gnu.build-id`, so a game update invalidates the cache automatically and the plugin
re-resolves on the next boot. Before any resolved address is called, boot self-checks must pass:
`_init`/`_fini` against the section addresses, `UObject::ProcessEvent` appearing exactly once in
`_ZTV7UObject` (which also yields its vtable slot), every address inside `.text` and not
`0x00`/`0xCC`, and `FindFunction(CDO, "Server_SendChatMessage")->Func == sym(execServer_SendChatMessage)`.
All UPROPERTY offsets come from `FindPropertyByName` at runtime; only the UE 5.6 fixed struct
facts are constants, and each is validated at boot.

Debug endpoints (Bearer token **and** `TAKARO_PLUGIN_DEBUG=1`):

| Endpoint | Use |
|---|---|
| `GET /debug/symbols` | what resolved, how, from cache or a fresh parse |
| `GET /debug/gamethread` | job queue depth, tick hook state, last tick age |
| `GET /debug/object?ptr=…\|path=…` | dump a live UObject's property tree — the discovery tool for new fields |
| `GET /debug/structs?name=…` | resolved struct layout (e.g. `ChatMessageData`) |
| `GET /debug/nearby` | actors around a player |
| `POST /debug/kill-nearest` | drive a creature kill through the game's damage pipeline (entity-killed proof without a human) |

`GET /health` is the operator-facing view of the same thing: plugin version, game build, engine
version, per-capability `ok`/`degraded`, `diagnostics.resolved[{name, rva, how, hooked, fired}]`
and the symcache state. A capability must self-report from `hooked`, not from symbol resolution —
reporting `ok` while the hook never bound was a real bug (a kill hook said `ok` with `hooked:false`).

## After a game update

1. The game's `.sym` ships with the update, so start the server with the plugin and read `/health`.
   Everything that still resolves keeps working; anything that does not is `degraded` and is also
   listed in Takaro's reachability reason. The server itself never fails to start because of this.
2. `GET /debug/symbols` shows which names disappeared or moved. UE/engine names are stable across
   patches; game-specific ones (`ADominion*`, `UDominion*`) are the ones that get renamed.
3. Re-verify the hooks that fire from player actions (`hooked`/`fired` counters in `/health`) with a
   real client join, one chat line, one death and one creature kill — hooks bind to the *live*
   object's vtable, and a new subclass can make a hook silently stop firing.
4. Run the sidecar test suite; it pins the Takaro wire shapes, not the game.

## Degrade semantics

Resolution and validation failures degrade one capability, they never crash the server and never
abort load. Concretely: the boot validation for a feature fails → that capability is marked
`degraded` with a reason → the matching HTTP endpoint answers `501`/`503` → the sidecar reports the
action as unsupported and the reason surfaces in Takaro's reachability text. Every handler is
wrapped in `try/catch(...)` with a readable-memory guard. Never call `FName::ToString` on an
unvalidated `FName`: that crash-looped the server during development.

## Dev rig

The connector is developed against a disposable dedicated server in the repo's `dev-servers/`
harness (its own world, its own port, auto-update off because client and server are version
locked). Deploy = build the `.so`, copy it to the host directory that is bind-mounted read-only
into the game container, rebuild the sidecar image, restart. Never test against a server anyone
plays on: several cells (ban, shutdown, restart) are destructive.

## Gotchas

- `LD_PRELOAD` goes on the **game binary only** — SteamCMD is 32-bit and fails with it set. Patch
  exactly the server launch line of the image's entrypoint and fail the build if it does not match.
- SteamCMD `validate` wipes the Steam tree: keep the `.so` outside it, mounted read-only.
- The game rewrites `DedicatedServer.ini` on shutdown, so ban edits go through
  `SetBannedUsers` + `PerformConfigSave`, never a text edit; the running server's login check uses a
  start-up snapshot, so the plugin also enforces bans at `PostLogin`.
- The server prints `WorldPassword` in cleartext into its log — redact in the plugin, the sidecar
  and anything you paste into a report.
- Hooking a base-class vtable does nothing: live objects carry their own vtables (the engine object
  is a `UDomGameEngine`). Hook the live object's vtable and verify `fired` before believing it.
- Takaro modules send explicit JSON `null` for optional arguments (`dimension`, `reason`,
  `expiresAt`, `quality`, `opts`) where the API docs simply omit the key. Handle absent, `null` and
  wrong type.
- Player-mutating actions arrive with a full nested `player` object, not a flat id.
