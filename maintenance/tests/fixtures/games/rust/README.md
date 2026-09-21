# Rust fixtures

Everything `test_game_rust.py` serves. Nothing here is a real Rust build: the point is the
*shape* — two depots, a framework archive beside them, and a Steam document with six
branches — not the bytes, which are gigabytes and prove nothing a stand-in cannot.

## `depots/`

The DepotDownloader stand-in (`fake_depotdownloader.py`) reads
`<depot>/<manifest>/tree/**` and `head.json`, so a test says "Steam moved the public
branch" by pointing the record at the other manifest.

| Depot | Manifest | Stands for |
|---|---|---|
| `258552` | `3352454092778561960` | the pinned linux depot: `RustDedicated`, `runds.sh`, `steam_appid.txt` and six `RustDedicated_Data/Managed/*.dll` |
| `258552` | `3400000000000000001` | the same paths on a later build, with different bytes |
| `258554` | `4408100835840826754` | the pinned shared-content depot: two `Bundles/shared/*.bundle` |
| `258554` | `4500000000000000001` | the same paths on a later build |

`head.json` names the second manifest of each depot, which is what the stand-in serves when
a caller passes no `-manifest` — the branch head. Two of the Managed names
(`UnityEngine.dll`, `mscorlib.dll`) are there so the reference subset has something to
*exclude*: `mscorlib` is filtered out by name and `UnityEngine` is not declared in
`inputs.files`, which is how the tests tell "the selector matched" from "everything was
copied".

Each file's body names its own path, depot and build, so a hash collision between two
manifests is impossible and a wrong-tree assertion reads plainly when it fails.

## `carbon/Carbon.Linux.Release.tar.gz`

A 449-byte archive with the real one's layout — `Carbon.targets`, `carbon.sh`,
`libdoorstop.so`, `carbon/{plugins,configs,data,managed,tools}/` — including the four files
the adapter records as ledger witnesses (`carbon/managed/Carbon.dll`,
`carbon/managed/Carbon.Common.dll`, `libdoorstop.so`, `carbon/tools/environment.sh`).
Built by hand with a fixed mtime so its sha256 is stable; `*.sha256` carries that digest.
The test rig repins the target's `inputs.carbon` at it and serves it from the fake
upstream, so acquisition, verification and unpacking all run for real.

## `steam/app_info_258550.vdf`

The Steam document for app 258550, recorded 2026-09-21 with the pinned steamcmd container,
which needs no Steam account:

```
docker run --rm \
  cm2network/steamcmd@sha256:e6b6b3503bf0e41feafe12dc709c90151afba193e1292cac55d28a7d470b1493 \
  ./steamcmd.sh +login anonymous +app_info_update 1 +app_info_print 258550 +quit
```

`header.txt` is the `AppID :` line printed just before the block, with the terminal's
colour escapes removed. The block keeps `common` (name, type, oslist, osarch) and the whole
`depots` section; `extended` and `config` are cut because nothing reads them.

What Steam served that day:

| Branch | `buildid` | Note |
|---|---|---|
| `public` | `25353106` | the build the catalog pins |
| `release` | `25389592` | "Release, before public" — declared, disabled |
| `staging` | `25441769` | declared, disabled |
| `aux02` | `24494353` | "Up and coming" — a known branch |
| `debug` | `17041002` | a known branch |
| `last-month` | `24983602` | a known branch |

`depots.258552.manifests.public.gid` is `3352454092778561960` and
`depots.258554.manifests.public.gid` is `4408100835840826754`; `258551` is the Windows
depot, which the catalog does not pin. `privatebranches` is `1`, and the bare scalars
`overridescddb` / `markdlcdepots` are kept deliberately: they sit among the depot blocks
and are what proves the reader does not assume every child of `depots` is a mapping.

The fixture is data, not a snapshot to refresh on a schedule — the tests mutate a parsed
copy of it in memory (a moved public head, an extra branch) rather than re-recording.
