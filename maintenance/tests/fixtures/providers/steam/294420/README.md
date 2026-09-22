# `app_info` of Steam app 294420 (7 Days to Die Dedicated Server)

Recorded 2026-09-21 with the pinned steamcmd container, which needs no Steam account:

```
docker run --rm \
  cm2network/steamcmd@sha256:e6b6b3503bf0e41feafe12dc709c90151afba193e1292cac55d28a7d470b1493 \
  ./steamcmd.sh +login anonymous +app_info_update 1 +app_info_print 294420 +quit > app_info.txt
```

`header.txt` is the `AppID :` line the tool prints just before the block, with the
terminal's colour escapes removed. `app_info.vdf` is the block itself, trimmed by hand so
a reader can see the whole thing:

- `common` keeps only `name`, `type`, `oslist`, `parent` and `osarch`.
- `depots.294421` (windows) and `depots.294422` (linux) keep only the `public`, `v3.2.0`
  and `v3.1.0` manifests; the older ones are dropped.
- the three `228xxx` depots are kept exactly as recorded: they carry `depotfromapp` and no
  `manifests` at all, and they are what proves the reader skips a depot it cannot pin.
- `overridescddb` is kept for the same reason: it is a bare scalar sitting among the depot
  blocks, so a reader that assumes every child of `depots` is a mapping breaks on it.
- `branches` keeps `public`, `v3.2.0`, `v3.1.0`, `alpha21.2` and `alpha12.5`. The last one
  has **no** `timeupdated`, only `timebuildupdated` — an old branch nobody has repointed.
- `privatebranches` is kept as recorded.

What Steam served that day:

| Fact | Value |
|---|---|
| `branches.public.buildid` | `24994542` (the build the catalog pins) |
| `depots.294422.manifests.public.gid` | `1633674551820196085` |
| change number | `39026857` |
| `privatebranches` | `1` |

No `latest_experimental` branch was listed. With `privatebranches 1` that means the
experimental branch exists but is hidden from an anonymous login, which is why the catalog
ships that channel disabled: enabling it without a branch password is a visible failure.

The fixture is data, not a snapshot to refresh on a schedule — the tests mutate a parsed
copy of it in memory (new build ids, extra branches, encrypted manifests) rather than
re-recording, so re-recording is only ever needed if Steam changes the *shape* of the
document.
