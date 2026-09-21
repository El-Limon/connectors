Takaro Enshrouded Plugin @VERSION@

This is a server-side plugin only. Players do not install anything.
It is a dbghelp.dll proxy: the game server loads it instead of the system dbghelp.

It hooks the server by pinned code signatures and is proven on Enshrouded game build
1024233 (Steam app 2278520, branch public, build 23178631). On any other game build the
affected capabilities self-check as "degraded" at load and the plugin reports them in
/health; the server keeps running, but Takaro's actions for those capabilities do not.

Install:
1. Stop the Enshrouded dedicated server (a running server holds dbghelp.dll open).
2. Put dbghelp.dll next to enshrouded_server.exe.
   With the example compose file that is data/enshrouded/server/takaro/plugin/dbghelp.dll,
   which is bind-mounted read-only to /opt/enshrouded/server/dbghelp.dll.
3. Make the server prefer this DLL over the system one. In the Linux/Wine container that
   is WINEDLLOVERRIDES="dbghelp=n,b"; a native Windows server loads the local file already.
4. Give the plugin a long random shared secret, either as TAKARO_PLUGIN_TOKEN on the game
   server process or as {"token":"..."} in <server>/takaro/plugin.json, and give the
   sidecar the same value; without it the plugin rejects every request with 401.
5. Start the server and confirm <server>/takaro/plugin.log contains
   "takaro enshrouded plugin @VERSION@ starting (pid ...)".

The plugin's HTTP API listens on 127.0.0.1:18890 only and is never exposed to the host.
You also need the sidecar (takaro-enshrouded-sidecar-*.zip) - the plugin alone does not
talk to Takaro.
