# libtakaro-vein — native server plugin (dev notes)

An `LD_PRELOAD` shared object for the VEIN dedicated server
(`VeinServer-Linux-Test`, Unreal Engine 5.6.1, Steam app 2131400). It exposes a loopback HTTP API
(`127.0.0.1:18890`, bearer token) that the TypeScript sidecar turns into the Takaro connector
protocol. The contract is in `docs/API.md`.

This is our own code: the Takaro Dragonwilds plugin (also ours) is the skeleton, and the symbol
layer was rewritten for a binary whose symbol situation is not known up front. Nothing is copied
from third-party projects - `Alustrial/UE4SS-Vein` and `Alustrial/VeinToolkit` are reference
reading only (which classes and UFunctions exist, and a layout ini to cross-check against).
C++17 standard library + POSIX only, no dependencies.

## Layout
```
src/  common.*      logging (redacted), JSON, config, time, file helpers
      resolve.*     ELF reader, strategy chain (symtab | depotsym | dynsym | signature),
                    symcache keyed on the build id, /proc/self/maps guard
      reflect.*     UE object model: FName/FString/TArray, FindFunction, property walks, dumps
      hooks.*       vtable slot swaps + the resolve/hook/fire registry behind /health
      gamethread.*  engine Tick hook + RunOnGameThread job queue
      state.*       capability registry + event ring buffer
      http.*        loopback HTTP/1.1 server and routing
      events.cpp    lane L2 (event sources)      — stub with the interface documented in events.h
      actions.cpp   lane L3 (17 actions)         — stub with the interface documented in actions.h
      main.cpp      constructor -> init thread
tools/symprobe.py   offline twin of resolve.cpp: which strategy resolves what on a given binary
tools/sigderive.py  derives a byte signature for an address and proves it is unique in .text
tests/              unit tests (ELF/.symtab walker, signature matcher, depot .sym fixture, JSON,
                    ring buffer, ban list, redaction, capabilities)
docs/               API.md + symbols-<build-id>.md
```

## Build
```
./build.sh                 # debian:bookworm container -> dist/libtakaro-vein.so + SHA256SUMS
./build.sh --native        # host toolchain (g++ >= 10)
./build.sh --tests         # build, then run the unit tests
./tests/run.sh             # unit tests only
make symbols               # VEIN_SERVER_BINARY=<path> -> docs/symbols-<build-id>.md
DEBUG_CORRUPT_SIG='UObjectBaseUtility::GetPathName' ./build.sh
                           # degrade proof: that one symbol is discarded at boot, so exactly the
                           # capability it backs goes `degraded` and the server keeps running
```
Flags: `-std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra`, linked
`-shared -pthread -ldl -static-libstdc++ -static-libgcc` so the .so does not depend on the image's
libstdc++.

## Deploying

The plugin file is mounted/copied read-only outside the Steam tree and `LD_PRELOAD` is applied to
the server launch line only (never to 32-bit steamcmd):

```
cp dist/libtakaro-vein.so <somewhere outside the Steam tree>/
LD_PRELOAD=<that path>/libtakaro-vein.so TAKARO_PLUGIN_TOKEN=<secret> \
    ./Vein/Binaries/Linux/VeinServer-Linux-Test -Port=7777 -QueryPort=27015 -log
curl -s -H "Authorization: Bearer $TAKARO_PLUGIN_TOKEN" http://127.0.0.1:18890/health
```

Runtime artefacts land in `<server>/Vein/Binaries/Linux/takaro/`:
`plugin.log`, `symcache.json`, optional `plugin.json`.

## Rules that cost us a crash
- **Never call `FName::ToString` on an FName you have not validated.** It indexes the engine name
  pool and segfaults on a bogus index. Compare raw FName values instead (see `reflect.cpp`).
- **Hooking a base class vtable is not enough.** Derived classes carry their own copy of an
  inherited function pointer; sweep `_ZTV*` for the address, and when the image exports no vtables
  take the vtable off the **live** engine object instead (see `gamethread.cpp`).
- **A byte signature that matches more than once is not a symbol.** `resolve.cpp` rejects it.
- **`/health` capability status derives from `hooked`/`fired`,** never from a resolved symbol.
- Every game pointer goes through `MemReadable()` before it is dereferenced.
- A capability that cannot be set up degrades with a reason; the server must never be affected.
