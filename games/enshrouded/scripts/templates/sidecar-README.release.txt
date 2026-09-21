Takaro Enshrouded Sidecar @VERSION@

The sidecar is the part that talks to Takaro. It reads the plugin's loopback API
(http://127.0.0.1:18890) and the game server log, so it must share the game server's
network namespace (compose: network_mode: "service:enshrouded") or run on the same host.

Option A - Docker (what docker-compose.example.yml does):
1. Unzip this folder next to docker-compose.example.yml and rename it to "sidecar"
   so the compose service's "build: ./sidecar" finds it.
2. docker compose -f docker-compose.example.yml --env-file .env up -d --build
   (The image is built from the Dockerfile in this folder, which installs the published
   dependencies and runs the dist/ in this zip. It needs no sources.)

Option B - plain Node.js 22 on the host:
1. npm ci --omit=dev
2. Set the environment variables (see .env.example), at minimum
   TAKARO_REGISTRATION_TOKEN, TAKARO_IDENTITY_TOKEN, TAKARO_PLUGIN_URL and
   TAKARO_PLUGIN_TOKEN (the same shared secret the plugin got).
3. node dist/index.js

Verify: the sidecar log prints "Identified with Takaro (gameServerId=...)" and the
server shows as online in Takaro. Health: http://127.0.0.1:18891/health.

Do not commit or share live registration tokens or plugin tokens.
