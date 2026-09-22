# Changelog

## [0.2.0](https://github.com/gettakaro/connectors/compare/dragonwilds-v0.1.0...dragonwilds-v0.2.0) (2026-09-22)


### Features

* **dragonwilds:** RuneScape Dragonwilds connector (LD_PRELOAD plugin + sidecar) ([#179](https://github.com/gettakaro/connectors/issues/179)) ([15baea9](https://github.com/gettakaro/connectors/commit/15baea9986f98c680a00f5e3179f728ad03d62b9))


### Documentation

* make connector READMEs the source of takaro.io game docs ([#218](https://github.com/gettakaro/connectors/issues/218)) ([b45d6cd](https://github.com/gettakaro/connectors/commit/b45d6cd682a61b11d485b1724e79b1e0b61e5305))

## 0.1.0 (2026-09-16)

### Features

* **dragonwilds:** initial RuneScape: Dragonwilds connector — `libtakaro-dragonwilds.so`
  (LD_PRELOAD native plugin resolving the server's own `.sym` symbols, loopback HTTP API) plus a
  TypeScript sidecar speaking the Takaro Generic Connector Protocol. Players, positions,
  inventories, items, entities, chat, teleports, gives, kicks, bans, shutdown and the
  join/leave/chat/death/kill events. See README.md for what is proven and what is not.
