Takaro Dune: Awakening plugin @VERSION@
Built for the self-hosted server package @REVISION@ (catalog target @TARGET@; see takaro-target.json).

OPTIONAL. Server-side only; players install nothing. The connector works without this plugin —
it adds kill attribution, live player position and precise connect/disconnect. Without it,
entity-killed is unavailable, deaths are reported without a killer, and position falls back to
the last position the game saved.

The plugin is loaded into ONE process only: the MAP server
(DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping), with LD_PRELOAD. It serves a
loopback HTTP API on 127.0.0.1:18890 for the sidecar.

Install:
1. Stop the map server.
2. Put libtakaro-dune.so OUTSIDE the Steam/game tree - validating the server installation
   deletes files it does not know about. With the example compose file that is
   data/dune-plugin/libtakaro-dune.so, bind-mounted read-only to /opt/takaro/.
3. Start the MAP BINARY ONLY with
     LD_PRELOAD=/opt/takaro/libtakaro-dune.so .../DuneSandboxServer-Linux-Shipping ...
   Never set LD_PRELOAD for the container, the user, a service, or SteamCMD: the other
   battlegroup services must not inherit it.
4. Set TAKARO_PLUGIN_TOKEN on the map server process to a long random shared secret and give the
   sidecar the same value as DUNE_PLUGIN_TOKEN. Without a token every request answers 401.
5. Start the server and confirm the log contains
     "takaro dune plugin @VERSION@ starting"
   followed by the resolver's self-check summary.

The server binary ships no function symbols, so the plugin resolves the game's own code through
RTTI and vtables and re-validates every address at each boot. This build is proven on the server
package above; on another one it may report one capability as "degraded" in /health rather than
guessing, and the map server and every other feature keep working.

You also need the sidecar archive - the plugin alone does not talk to Takaro.
