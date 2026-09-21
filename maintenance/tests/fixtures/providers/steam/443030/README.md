# `app_info` of Steam app 443030 (Conan Exiles Dedicated Server)

Recorded 2026-09-21 with the pinned steamcmd container, which needs no Steam account:

```
docker run --rm \
  cm2network/steamcmd@sha256:e6b6b3503bf0e41feafe12dc709c90151afba193e1292cac55d28a7d470b1493 \
  ./steamcmd.sh +login anonymous +app_info_update 1 +app_info_print 443030 +quit > app_info.txt
```

`header.txt` is the `AppID :` line the tool prints just before the block, with the
terminal's colour escapes removed. `app_info.vdf` is the block itself, trimmed by hand so
a reader can see the whole thing:

- `common` keeps only `name`, `type`, `oslist` and `osarch`; `extended` and `config`
  (the Windows/Linux launch entries) are dropped — nothing reads them.
- `depots.1004/1005/1006` are kept exactly as recorded. They are Valve's Steamworks
  redistributables: they carry a `public` manifest **and** `depotfromapp 1007`, which is
  the shape that proves a reader tells another app's depot from this app's content even
  when it does have a manifest of its own.
- the four `2289xx` depots are kept as recorded: `depotfromapp` and no `manifests` at all.
- `depots.443031` (windows) and `depots.443032` (linux) keep both manifests each. The
  legacy branch's Linux manifest has `size 0` — the UE4 build ships no Linux content.
- `branches` keeps both branches as recorded, and `privatebranches` with it.

What Steam served that day:

| Fact | Value |
|---|---|
| `branches.public.buildid` | `25356024` (the build the catalog pins) |
| `depots.443032.manifests.public.gid` | `2572292872952587850` |
| `depots.1006.manifests.public.gid` | `4559160656493359681` |
| change number | `39047421` |
| `privatebranches` | `1` |

The catalog watches `443032` only: `1006` belongs to app 1007, so it is pinned in the
target (the proven install carries it) but never watched. The `conan-exiles-legacy`
channel is declared disabled — it is the UE4 server, a different product on the same app.

The fixture is data, not a snapshot to refresh on a schedule — the tests mutate a parsed
copy of it in memory (new build ids, moved manifests) rather than re-recording, so
re-recording is only ever needed if Steam changes the *shape* of the document.
