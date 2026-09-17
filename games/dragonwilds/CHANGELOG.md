# Changelog

## 0.1.0 (2026-09-16)

### Features

* **dragonwilds:** initial RuneScape: Dragonwilds connector — `libtakaro-dragonwilds.so`
  (LD_PRELOAD native plugin resolving the server's own `.sym` symbols, loopback HTTP API) plus a
  TypeScript sidecar speaking the Takaro Generic Connector Protocol. Players, positions,
  inventories, items, entities, chat, teleports, gives, kicks, bans, shutdown and the
  join/leave/chat/death/kill events. See README.md for what is proven and what is not.
