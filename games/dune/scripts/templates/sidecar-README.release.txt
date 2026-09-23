Takaro Dune: Awakening sidecar @VERSION@
Built for the self-hosted server package @REVISION@ (catalog target @TARGET@; see takaro-target.json).

This is the part that talks to Takaro, and on its own it already covers almost everything: it
reaches the battlegroup's Postgres (read-only), the GAME RabbitMQ for chat, and the GM command bus
for every write. The plugin archive is optional.

Before it can write anything, EVERY map server process needs both gates:
  -ini:engine:[ConsoleVariables]:server.NotificationSystem.Enabled=true
  -ini:engine:[FuncomLiveServices]:ServerCommandsAuthToken=<same value as DUNE_GM_AUTH_TOKEN>
and UserGame.ini needs the two commands the game does not allow by default:
  [AdminSetting.Global]
  +Allowed_GM_Commands=KickPlayer
  +Allowed_GM_Commands=ServiceBroadcast

Option A - Docker (what docker-compose.example.yml does):
1. Unpack this folder next to docker-compose.example.yml and rename it to "sidecar"
   so the compose service's "build: ./sidecar" finds it.
2. docker compose -f docker-compose.example.yml --env-file .env up -d --build

Option B - plain Node.js 22 on the host:
1. npm ci --omit=dev
2. Set at least TAKARO_REGISTRATION_TOKEN, TAKARO_IDENTITY_TOKEN, DUNE_PG_URL, DUNE_RMQ_URL,
   DUNE_GM_AUTH_TOKEN and DUNE_GM_PUBLISHER (see .env.example).
3. npm run catalogue      # builds the item catalogue; see data/ATTRIBUTION.md
4. node dist/index.js

Verify: the sidecar log prints "Identified with Takaro (gameServerId=...)", the server shows as
online in Takaro, and curl http://127.0.0.1:18891/health returns status "ok".

Never commit or share live registration tokens, GM auth tokens or plugin tokens.
