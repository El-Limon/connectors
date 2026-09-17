# Takaro ↔ VEIN sidecar (dev)

Node 22 / TypeScript. Speaks the Takaro generic-connector WebSocket protocol on one side and the
`libtakaro-vein.so` plugin's loopback HTTP API (`../plugin/docs/API.md`) on the other. All 17 actions + 6 events.
Game truth lives in the plugin; only Takaro protocol shape lives here.

## Identity (campaign Decision 3)

`gameId` **is** the SteamID64 string, `steamId` is the same value, `platformId` is `steam:<id64>`. No EOS/Epic
fields are ever emitted. `name` comes from the plugin; `ping`/`ip` when the plugin has them.

## Run

```bash
npm ci
npm run typecheck && npm test
npm run mock-plugin          # in-memory plugin on :18890 for local dry runs
cp .env.example .env && npm run dev
docker build -t takaro-vein-sidecar:dev .
```

Health: `GET http://127.0.0.1:18891/health` (identify state, plugin health, cursor, pending events/bans,
`serverReady`, and the game HTTP API cross-check).

## Env

See `.env.example`. Required: `TAKARO_PLUGIN_TOKEN`, `TAKARO_REGISTRATION_TOKEN`, `TAKARO_IDENTITY_TOKEN`.
Self-healing: `SIDECAR_EXIT_AFTER_UNREACHABLE_MS` (default **45000**) and `SIDECAR_EXIT_AFTER_PLUGIN_LOSS_MS`
(default `180000`) — see "Self-restart after a game-container restart" below.
Vein-specific: `VEIN_LOG_FILE`, `VEIN_HTTP_API` (Vein's own read-only API, optional — cross-check + `/players`
fallback), `VEIN_LOG_TAIL`, `VEIN_LOG_EVENTS`, `VEIN_LOG_*_RE`.

## Self-restart after a game-container restart (F17)

`network_mode: service:vein` pins this container to the network namespace the game container had **when the sidecar
was created**. Any restart of the game container — `docker restart`, `docker compose restart vein`, a crash loop,
a `shutdownAction` — gives the game a *new* namespace and leaves the sidecar in the old, dead one: no plugin, no
DNS, and the Takaro socket never comes back.

The sidecar detects this itself: the plugin health probe runs on its own timer that is **not** stopped when the
Takaro socket drops (that was the actual bug — the probe used to die with the socket, so nothing was left to
notice), and when both the plugin *and* the game's own HTTP API (`VEIN_HTTP_API`) have been unreachable for
`SIDECAR_EXIT_AFTER_UNREACHABLE_MS` it logs and exits(1). `restart: unless-stopped` then re-creates it in the
live namespace. **Proven on the rig 2026-09-17**: `docker compose restart vein` at 10:06:46Z → self-exit at
~10:07:31Z → back up 10:07:33Z (`RestartCount 1`), same netns as the game (`net:[4026532572]`), re-identified and
`/health` green — with no operator action. `restart: unless-stopped` on the sidecar service is therefore
**required**, not optional.

## Timed bans and `listBans` (F16)

`listBans` re-reads `timed-bans.json` from disk on every call, so the first answer after a restart carries the real
`expiresAt`; if that file exists but cannot be read or parsed, `listBans` **fails** rather than reporting a live
timed ban as permanent. Note the Takaro-side half of F16, which the connector cannot fix: a `syncBans` job stores a
connector-reported ban as `until: null, takaroManaged: false` even when the connector's frame carries `expiresAt`
(observed 2026-09-17, see `evidence/2026-09-17-l4b-sidecar-fixes.md`). The row is removed by the next `syncBans`
after the connector lifts the ban.

## Log tail — UNVERIFIED grammar

The tailer is a fallback only, active while the plugin's `players` capability is not `ok`. Its grammar is a small
pluggable table in `src/vein/logTail.ts` (`DEFAULT_GRAMMAR`), overridable per deployment with `VEIN_LOG_*_RE`.

| Line | Status |
|---|---|
| `readyLine` — `Created session GameSession.` | **verified** (pterodactyl egg startup marker) |
| `loginLine` — `LogVein: PlayerState ID changed to <SteamID64>` (id only) or UE `LogNet: Login request: ?Name=… userId: Steam:<id64>` | **observed on the client**, not yet on the dedicated server |
| `joinLine` — `LogVein: [] Player <persona> selected character <32hex> (aka <char>)`, or UE `LogNet: Join succeeded: <name>` | **observed on the client**, not yet on the dedicated server |
| `chatLine` — `LogVeinChat: [<SteamID64>] <persona> (aka <char>): <msg>` | **observed on the client**; channel is not logged → reported as `global`. The plugin hook stays the real chat source |
| `leaveLine` — `LogNet: UNetConnection::Close` / `UChannel::CleanUp` with `UniqueId: Steam:<id64>` | **unverified** (UE standard); a half-open connection logs `UniqueId: NULL:gamer-<32hex>` and is never a key |

Observed lines are banked in `context/games/vein/research/2026-09-17-log-grammar.md` (lane L0b, from the vanilla
client's own log in a local session — same game code, so same categories, but server-side confirmation is still
pending). Log noise (`LogHttp`, verbose `LogOnline*`/`LogSteamShared`, `LogEOS*`, `LogStreaming`, `LogNetTraffic`,
`LogGarbage`) is dropped in `filtered` mode; `Password=`, `?p=` and the Steam auth `Ticket=` are redacted
unconditionally.

## Banked fixes kept here (do not regress)

- Explicit JSON `null` optional args tolerated everywhere (`dimension`, `reason`, `expiresAt`, `quality`, `opts`).
- gameEvents only on an **open and identified** socket; queued otherwise and flushed in order after
  `identifyResponse`; the persisted cursor never advances on `ws.send` alone (F10).
- `bootId` change → cursor reset + online reconcile.
- Plugin-loss self-exit so the container re-attaches to the game's network namespace (45 s when the game's own
  HTTP API is gone too, 180 s when only the plugin is down).
- Timed bans are lifted by the sidecar from a persisted ban store.
- Unknown console command → `{success:false}`, never an error frame.
- `getPlayer` for an offline/unknown id → last-known record (`online:false`) or a minimal synthesised record,
  never `{}`, `null` or an error frame (F7).
- Sender-name precedence: per-message override → `TAKARO_SENDER_NAME` → `TAKARO_SERVER_NAME` → `Server`.
