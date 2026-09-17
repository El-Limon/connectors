# libtakaro-dragonwilds — native server plugin (dev notes)

An `LD_PRELOAD` shared object for the RuneScape: Dragonwilds dedicated server
(`RSDragonwildsServer-Linux-Shipping`, UE 5.6.1). It exposes a loopback HTTP API
(`127.0.0.1:18890`, bearer token) that the TypeScript sidecar turns into the Takaro connector
protocol. The contract is in `docs/API.md`.

This is our own code, built on the Takaro Enshrouded plugin skeleton ported to Linux/POSIX.
Nothing is copied from third-party projects; C++17 standard library + POSIX only, no dependencies.

## Layout
```
src/  common.*      logging (redacted), JSON, config, time, file helpers
      sym.*         ELF reader, .sym resolver, symcache, /proc/self/maps guard
      reflect.*     UE object model: FName/FString/TArray, FindFunction, property walks, dumps
      hooks.*       vtable slot swaps + the resolve/hook/fire registry behind /health
      gamethread.*  engine Tick hook + RunOnGameThread job queue
      state.*       capability registry + event ring buffer
      http.*        loopback HTTP/1.1 server and routing
      events.cpp    lane L2 (event sources)      — stub with the interface documented in events.h
      actions.cpp   lane L3 (17 actions)         — stub with the interface documented in actions.h
      main.cpp      constructor -> init thread
tools/symdump.py    prints/records the wanted symbol table for a build
tests/              unit tests (sym parser fixture, JSON, ring buffer, redaction, capabilities)
docs/               API.md + symbols-<build-id>.md
```

## Build
```
./build.sh                 # debian:bookworm container -> dist/libtakaro-dragonwilds.so + SHA256SUMS
./build.sh --native        # host toolchain (g++ >= 10)
./build.sh --tests         # build, then run the unit tests
./tests/run.sh             # unit tests only
make symbols               # regenerate docs/symbols-<build-id>.md from the installed server
DEBUG_CORRUPT_SIG=x ./build.sh   # deliberately broken build, for the degrade proof
```
Flags: `-std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra`, linked
`-shared -pthread -ldl -static-libstdc++ -static-libgcc` so the .so does not depend on the image's
libstdc++.

## Deploying

The plugin file is mounted/copied read-only outside the Steam tree and `LD_PRELOAD` is applied to
the server launch line only (never to 32-bit steamcmd):

```
cp dist/libtakaro-dragonwilds.so <somewhere outside the Steam tree>/
LD_PRELOAD=<that path>/libtakaro-dragonwilds.so TAKARO_PLUGIN_TOKEN=<secret> ./RSDragonwildsServer.sh -log
curl -s -H "Authorization: Bearer $TAKARO_PLUGIN_TOKEN" http://127.0.0.1:18890/health
```

Runtime artefacts land in `<server>/RSDragonwilds/Binaries/Linux/takaro/`:
`plugin.log`, `symcache.json`, optional `plugin.json`.

## Rules that cost us a crash
- **Never call `FName::ToString` on an FName you have not validated.** It indexes the engine name
  pool and segfaults on a bogus index. Compare raw FName values instead (see `reflect.cpp`).
- **Hooking a base class vtable is not enough.** Derived classes carry their own copy of an
  inherited function pointer; sweep `_ZTV*` for the address (see `gamethread.cpp`).
- Every game pointer goes through `MemReadable()` before it is dereferenced.
- A capability that cannot be set up degrades with a reason; the server must never be affected.
