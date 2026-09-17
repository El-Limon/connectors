# Changelog

## 0.1.0 (2026-09-17)

### Features

* **vein:** initial VEIN connector — `libtakaro-vein.so` (LD_PRELOAD native plugin for the Linux
  dedicated server, resolving the game's own code by symbol table / depot symbols / signature scan
  and serving a loopback HTTP API) plus a TypeScript sidecar speaking the Takaro Generic Connector
  Protocol. Players, positions, inventories, items, entities, chat, broadcasts and whispers,
  gives, teleports, kicks, timed and permanent bans, shutdown, a connector-provided console
  command set, and the join/leave/chat/death/kill events. See README.md for what is proven and
  what is not.
