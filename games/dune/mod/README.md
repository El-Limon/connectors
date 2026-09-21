# libtakaro-dune — native server plugin (dev notes)

An `LD_PRELOAD` shared object for the Dune: Awakening self-hosted dedicated server
(`DuneSandboxServer-Linux-Shipping`, Unreal Engine 5.2/5.3 — the exact version is only in the boot
log; Steam tool app 4754530). It exposes a loopback HTTP API (`127.0.0.1:18890`, bearer token) that
the TypeScript sidecar turns into the Takaro connector protocol. The contract is `docs/API.md`.

This is our own code: the Takaro VEIN plugin (also ours, itself descended from our Dragonwilds and
Enshrouded plugins) is the skeleton, and the symbol layer was **rewritten from scratch** for a binary
that ships no function symbols at all. Nothing is copied from third-party projects. C++17 standard
library + POSIX only, no dependencies.

## The plugin is read-only, and small on purpose

Dune has no RCON, but the sidecar reaches almost everything out of process: the roster, inventories
and saved positions in **Postgres**, and mutations over the game's own **RabbitMQ GM command bus**.
That covers ~45 of the 52 goal cells on a stock Funcom install with zero binary tampering.

This plugin exists for the four things nothing out-of-process can give: **live pawn location**,
**`entity-killed` plus death attribution**, **precise connect/disconnect**, and the `/entities` class
list. There is **no mutating endpoint here** — no give, teleport, kick, message, ban or shutdown. If
you are about to add one, it belongs in the sidecar.

## The binary, and why the symbol layer looks the way it does

`DuneSandboxServer-Linux-Shipping` is 370 MiB, a **PIE**, and **stripped**: no `.symtab`, no DWARF,
and not one engine or game *function* symbol (`UObject::ProcessEvent`, `FName::ToString`, `GEngine`,
`GUObjectArray` all grep to zero). What it *does* export is the C++ **RTTI**: **42 942 `_ZTV*` vtable
symbols** out of ~171 856 `.dynsym` entries.

So the unit of resolution is a **vtable slot**, and that turns out to be *better* than a symbol table
for the thing we most need: "what class is this object?" becomes an exact address comparison of the
object's first word against a named `_ZTV` symbol — no reflection, no `FName` read, no engine call.

Three traps this binary sets, all of which the code guards against explicitly:

1. **Every vtable slot in the file image is `0`.** It is a PIE; the real target is an
   `R_X86_64_RELATIVE` addend in `.rela.dyn` (2 867 943 of them), written by the loader. So vtables are
   read from **live, relocated memory only**, and an all-zero table is a loud error, not a silent zero.
2. **One `_ZTV` symbol can pack several sub-vtables** (multiple inheritance).
   `st_size / 8 - 2` overestimates the primary slot count; the scan stops at the first embedded
   `[offset-to-top][typeinfo]` pair. Relatedly, `_ZTI6AActor` is a `__vmi_class_type_info`, so an
   `si`-only RTTI walk misses the entire Actor branch.
3. **"Inside an executable `PT_LOAD`" is meaningless here** — the single R+X segment spans vaddr
   `0`…`0x153690d0` and also contains `.dynsym`, `.rodata` and `.rela.dyn`. Addresses are validated
   against `.text` proper *and* against the `.eh_frame_hdr` function-entry table (918 821 entries,
   unstrippable because the unwinder needs it), which is what catches an off-by-one slot index.
   `__cxa_pure_virtual` is inside `.text` and starts with `0x50`, so it must be compared against
   explicitly — 19 526 slots point at it.

Full evidence: `context/games/dune/research/2026-09-21-binary-dissection.md`.

## What M0 changed (2026-09-21, first live load)

The paragraphs above were written from **offline** analysis. Running inside the live process
disproved a load-bearing assumption: `reflect.cpp` was ported from connectors whose binary shipped a
`.symtab`, and its whole object model funnels through `FName::ToString`, `UObject::FindFunction`,
`UStruct::FindPropertyByName`, `GetObjectsOfClass` and `StaticFindObject` — **none of which exist on
this build**, so on the live process every one of them is `nullptr`.

Two modules were added to do those jobs by reading engine data structures directly:

- **`src/objpool.{h,cpp}`** — walks `GUObjectArray` ourselves, **locates `FNamePool` on the live
  process** (searched for, cross-checked against names the RTTI already proves, accepted only when
  the answer is unique), decodes `FName` -> text with no engine call, indexes every live `UClass` by
  decoded name, and **discovers** the `UStruct`/`FField`/`FProperty` offsets with structural oracles
  instead of a version table.
- **`src/livehooks.{h,cpp}`** — the hooks that go on the live, relocated process: the
  `UObject::ProcessEvent` slot-86 filter on nine class vtables (which is also the **game-thread
  pump**, because no engine `Tick` is reachable here — see `gamethread.h`), and
  `AGameModeBase::PostLogin`/`Logout` on the **live game-mode object's own vptr**, retried until that
  object exists.

Evidence and the full list of what the live process disproved:
`context/games/dune/research/2026-09-21-plugin-m0.md`. Probe wrapper:
`context/games/dune/scripts/plugin-probe.sh`.

## Layout

```
src/  common.*      logging (redacted), JSON, config, time, file helpers, HandlerResult
      resolve.*     ELF reader; the strategy chain
                      dynsym-vtable -> rtti-slot -> string-xref -> dynsym
                    ProcessEvent slot derivation, .eh_frame_hdr function-entry oracle,
                    vtable readers (live memory only), object identification by vtable pointer,
                    symcache keyed on .note.gnu.build-id, /proc/self/maps guard
      reflect.*     UE object model: FName/FString/TArray, property walks, dumps; every offset is a
                    hypothesis that Validate() re-proves on the live process
      hooks.*       vtable slot swaps + the resolve/hook/fire registry behind /health
      gamethread.*  engine Tick hook + budgeted job pump
      state.*       capability registry + event ring buffer
      http.*        loopback HTTP/1.1 server and routing
      objpool.*     GUObjectArray walk, live FNamePool discovery + FName decoding, the live class
                    index, and the FField/FProperty layout discoverers (all symbol-free)
      livehooks.*   the hooks installed against the live process: the ProcessEvent filter (which is
                    also the game-thread pump) and PostLogin/Logout on the live game-mode object
      events.*      event sources (connect/disconnect, death attribution, entity-killed) — lane L2;
                    L1 delivers the capability registry, the hook plan and the guards
      query.*       the read-only handlers (/players, /players/{id}/location, /entities) — lane L2
      main.cpp      constructor -> init thread, and the basename gate that keeps the preload inert
tools/vtprobe.py    offline twin of resolve.cpp: dumps vtables and slots from a stripped PIE by
                    indexing .rela.dyn, and diffs a class against its base to find overrides
tools/sigderive.py  derives a byte signature for an address and proves it is unique in .text,
                    with UTF-16LE string-xref anchoring
tests/              unit tests (ELF/.symtab walker, signature matcher, vtable-name demangling,
                    UTF-16LE literal search, RIP-relative decoding, the ProcessEvent fingerprint,
                    JSON, ring buffer, log parsing, Dune secret redaction)
docs/               API.md, events-design.md, gamethread-policy.md
```

## Build

```
./build.sh                 # debian:bookworm container -> dist/libtakaro-dune.so + SHA256SUMS
./build.sh --native        # host toolchain (g++ >= 10)
./build.sh --tests         # build, then run the unit tests
./tests/run.sh             # unit tests only
DEBUG_CORRUPT_SIG='_ZTV16ADuneCritterBase' ./build.sh
                           # degrade proof: that one name is discarded at boot, so exactly the
                           # capability it backs goes `degraded` and the server keeps running
```

Flags: `-std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra`, linked
`-shared -pthread -ldl -static-libstdc++ -static-libgcc`.

**Why debian:bookworm and not the server's own Ubuntu 24.04:** the game image has **glibc 2.39**, and
building *on* 2.39 would produce a `.so` that refuses to load anywhere older. Bookworm's glibc 2.36
links upward fine. Verified on the real image
(`registry.funcom.com/funcom/self-hosting/seabass-server:2118731-0-shipping`): our `.so` needs at most
`GLIBC_2.36` and only `libc.so.6` + the loader, and preloading it into `/bin/echo` and `/bin/sh`
inside that image leaves them untouched.

## What lane L2 added (2026-09-21), and what it proves

The four gaps this plugin exists for are now implemented. **None of them needed a new hook**: every
death candidate on this build is a `UFUNCTION`, so all of them arrive through the
`UObject::ProcessEvent` detour L1b already proved, and L2 added a **name filter** on it.

| feature | mechanism | proven? |
|---|---|---|
| `entity-killed` / `player-death` attribution | ProcessEvent name filter on `OnDeathOrDefeatOnServer`, `BPOnDeath`, `ReceiveMulticastDeathOrDefeat`, `KillCharacter` (+ `ReceiveMulticastKill` as a hint only) | see `.runtime/L2-report.md` |
| identity | `DunePlayerControllerPersistenceComponent` → the Postgres row ids; the sidecar joins to the FLS id | **needs a join to confirm the values** |
| live location | `AActor::RootComponent` → `USceneComponent::RelativeLocation`, by reflected offset | needs a pawn |
| `/entities` | `GUObjectArray` sweep by class chain, named from `DuneNpcCharacter::m_Name` | live sweep works |

Three findings worth carrying to the next UE connector:

1. **Assume nothing about a UFunction's parameters.** Dune's death functions take no actor pointer at
   all: the killer is inside an `InstigatorInfo` struct whose two members are **`FWeakObjectPtr`**
   (`{int32 index; int32 serial}`), not pointers. Reading them as pointers loses every killer
   *silently*, because the validity guard correctly rejects the garbage. Resolve the index through
   `GUObjectArray`.
2. **`bIsDeath` is a correctness gate.** These functions fire for a *defeat* (downed) as well as a
   death; without reading that flag, every knock-down is reported as a death.
3. **A display name may exist where the offline reading says it does not.**
   `DuneNpcCharacter::m_Name` holds "Mobula Gang Member", not a row key — the row keys are in the AI
   spawner's *log*. It is still put through a display-name predicate rather than trusted.

## Deploying

The `.so` is mounted read-only outside the game tree and `LD_PRELOAD` is applied to the **map server
process only**:

```
cp dist/libtakaro-dune.so <somewhere outside the game tree>/
LD_PRELOAD=<that path>/libtakaro-dune.so TAKARO_PLUGIN_TOKEN=<secret> \
    /home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping DuneSandbox ...
curl -s -H "Authorization: Bearer $TAKARO_PLUGIN_TOKEN" http://127.0.0.1:18890/health
```

On the dev rig this is done by `dev-servers/scripts/deploy-connector.sh **dune-plugin**`, which
builds outside the rig lock, refuses an artefact with undefined symbols, and then — under the lock, as
one command — swaps the file and recreates **only** the survival container. It never calls `stop.sh`,
so postgres, both brokers, the gateway and the text-router are untouched, and a plugin build failure
cannot block `deploy-connector.sh dune-sidecar`.

⚠️ **The sidecar is a separate container**, so it cannot reach a loopback-only listener. Set
`TAKARO_PLUGIN_BIND=0.0.0.0` (the default stays `127.0.0.1`) and point the sidecar at
`http://survival:18890`. The port is **not** published to the host, and every route still requires the
bearer token; `/health.diagnostics.http.bind` reports which address is in use.

Runtime artefacts land in `<server>/DuneSandbox/Binaries/Linux/takaro/`: `plugin.log`,
`symcache.json`, optional `plugin.json`.

⚠️ **Setting `LD_PRELOAD` on the container is not the same as setting it on the server.** Dune's launch
chain is `run.sh` (bash) → `su dune -c` → `runuser` → `bash` → `DuneSandboxServer.sh` → the ELF, and
`run.sh` also invokes `lsof` and starts `sshd`. A container-wide `LD_PRELOAD` reaches every one of
them. The plugin therefore gates itself on `/proc/self/exe`'s **basename** being exactly
`DuneSandboxServer-Linux-Shipping` and returns from its library constructor before creating a thread
in anything else — the shells and helpers pass through untouched.

## Anti-cheat

`BattlEye` **is** shipped server-side (`DuneSandbox/Binaries/Linux/BattlEye/BEServer_x64.so`), but the
shipped `DuneSandbox/Config/DefaultEngine.ini` sets `BattlEye.Enabled=false`. Before the plugin is
preloaded, the rig must assert that this is still false **and** that `BEServer_x64.so` is absent from
`/proc/<pid>/maps`.

## Rules that cost us a crash (on this or an earlier connector)

- **Never read a vtable slot from the file image.** On a PIE it is 0, and calling it jumps to address
  0 on the game thread.
- **Validate before reading.** `LooksLikeUObject()` — readable, 8-aligned vptr, slot 0 in `.text`, and
  the vtable is one of the 42 942 exported — runs before any `FName` or property read on a pointer the
  engine handed us. On a binary with no function symbols, a wrong pointer is a crash, not a wrong
  answer.
- **Never call `FName::ToString` on an unvalidated FName.** It indexes the engine name pool and
  segfaults on a bogus index. Compare raw FName values instead.
- **Hooking a base class vtable is not enough.** A derived class carries its own copy of an inherited
  function pointer, and a Blueprint subclass gets a brand-new vtable when its package streams in —
  long after boot. Hook live objects, and re-sweep in `Housekeep()`.
- **A byte signature that matches more than once is not a symbol.** `resolve.cpp` rejects it. A
  signature whose declared UTF-16LE anchor string is not referenced near the match is rejected too.
- **`/health` capability status derives from `hooked`/`fired`,** never from a resolved address. The
  derived ProcessEvent slot is reported separately from `processEventSlotConfirmed`, which only goes
  `true` once a detour has actually fired.
- A capability that cannot be set up degrades **with the missing symbol named**; the server is never
  affected.
