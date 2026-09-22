# From an upstream release to a published target

One upstream release followed all the way to a closed issue, with the command that moves each
step, the exit code it returns and what the reader would see.

## The example

**This example is hypothetical.** Minecraft 26.3 on Fabric is used because it is the shape every
Minecraft target has, not because it shipped: at the time of writing the maintained Fabric target
is `fabric-26.2`. Substitute the real revision when you follow it. The Steam variant at the end is
hypothetical in the same way.

Who acts changes per step, and that is the point of the table:

| Step | Who acts | What produces the state |
|---|---|---|
| 1 Observed | the scheduler | an upstream head no target covers |
| 2 Ready | the scheduler | a framework listing for that game version |
| 3 Implemented | an agent or a human | a pull request that `Refs` the issue |
| 4 Awaiting release | a reviewer merging it | a maintained target on `main` |
| 5 Released | the release workflow | a stable release that carries the target |
| 6 Closed | the scheduler | the release proving every matched target shipped |

Every command is defined in [the command reference](../README.md); discovery in
[discovery](discovery.md), the states in [lifecycle](lifecycle.md), verification in
[runtime verification](verify.md), publication in [releases](release.md).

## 1. Observed

The six-hourly publisher runs `run --publish`. Mojang's manifest now lists `26.3` as the release
head, and no catalog target has `revision: "26.3"`, so the scan reports an observation and plans
one issue:

```json
{"observations": [{"provider": "mojang", "component": "minecraft", "branch": "release", "rev": "26.3"}],
 "plan": [{"action": "create-issue", "identity": "provider=mojang component=minecraft branch=release rev=26.3",
           "title": "Minecraft 26.3: new stable release needs a target"}]}
```

Exit `0`. The filed issue's first line is its identity — the thing that makes a rerun find it
again instead of filing a second one:

```
<!-- takaro-maint: kind=support provider=mojang component=minecraft branch=release rev=26.3 -->
```

Its state token is `<!-- takaro-maint:state=detected -->`. The issue carries the facts about the
release and no catalog change and no readiness claim yet. Everything already published is recorded
as seen in the same run, so the other hundred-odd versions in that manifest are never filed —
see [bootstrap and history](discovery.md#bootstrap-and-history).

## 2. Ready

Fabric ships for 26.3: `meta.fabricmc.net` lists the game version and there is a `fabric-api`
build in its bucket. The next scheduled run rewrites the issue's readiness table and moves the
state token to `ready-for-agent`:

```
| Platform | Readiness | Framework release | Artifact | sha256 | Observed |
| --- | --- | --- | --- | --- | --- |
| fabric | ready | `0.161.0+26.3` | fabric-api-0.161.0+26.3.jar | `1720e31a…` | 2026-09-18T06:17:00Z |
| neoforge | missing | — | — | — | — |
| paper | missing | — | — | — | — |
```

Exit `0`. One ready row is enough: `ready-for-agent` when any row is ready,
`blocked-upstream` otherwise. The row semantics are
[frameworks and readiness](discovery.md#frameworks-and-readiness).

## 3. Implemented

A human or an agent opens a pull request whose body says `Refs #<n>` — never `Closes #<n>`, which
would close the issue before anything shipped. It adds two things:

* `catalog/minecraft/targets/fabric-26.3.json`, the target record: `id`, `platform`, `revision`,
  `support.status: "candidate"`, the pinned `inputs` (game manifest and server jar by sha1, the
  Fabric launcher and fabric-api by sha256), the `runtime` container by digest, the `build`
  project and its dependency pins, the `components` it produces and `verification.required`. The
  field-by-field shape is [the catalog](../../catalog/README.md);
* `games/minecraft/mod/targets/fabric-26.3/build.gradle.kts`, the build project the record names.

Then, in order:

```
takaro-maint catalog record-hash --game minecraft --target fabric-26.3 --field loader
takaro-maint catalog validate --online                 # 0
takaro-maint install --game minecraft --target fabric-26.3 --dest <dir>
takaro-maint build   --game minecraft --target fabric-26.3 --version <v> --out <dir>
takaro-maint artifact validate --game minecraft --target fabric-26.3 <file>
takaro-maint deploy  --game minecraft --target fabric-26.3 --dest <dir> --from build-manifest.json
takaro-maint verify  --game minecraft --target fabric-26.3 --artifacts <dir> --out <dir>
```

`record-hash` downloads an input twice and writes the sha256 both downloads agree on.
`catalog validate --online` re-downloads every pinned input and compares hashes; it has to exit
`0`. `verify` writes a report whose `level` must reach the record's `verification.required` —
`protocol` for a Minecraft target — with `outcome: pass`. A report is not evidence unless both are
true; see
[levels and outcome are not the same thing](verify.md#levels-and-outcome-are-not-the-same-thing).

The next reconcile sees the open pull request and moves the issue to `implementation-pr`, with
`| Implementation | #12 (open) |` in the Lifecycle block. Exit `0`.

## 4. Awaiting release

The pull request is reviewed and merged, and the record lands on `main` with
`support.status: "maintained"` citing that verification report in `support.evidence`. The next
reconcile matches the issue's identity to the record on `--catalog-ref`:

```
| State | `awaiting-release` since 2026-09-19T12:17:00Z |
| Implementation | #12 (merged into main at 2026-09-19T11:04:00Z) |
| Catalog on main | `fabric-26.3` (maintained, fingerprint `d0dac4c4c62f4f29`) |
| Release | — |
| Why not further | no stable release for minecraft yet |
```

Exit `0`, and the issue stays **open**. A merge is not a release.

## 5. Released

The release pull request merges. That creates a draft release and forces the tag, and
`.github/workflows/minecraft.yml` calls the shared `connector-release.yml`, which builds every
target twice (byte-identical), verifies each one, and then:

```
takaro-maint release assemble --connector minecraft --version <v> --channel stable \
  --tag minecraft-v<v> --dist dist --reports reports --out assembled
takaro-maint release publish  --connector minecraft --channel stable --tag minecraft-v<v> \
  --assembled assembled --target-commit <sha>
takaro-maint release verify   --tag minecraft-v<v> --connector minecraft
```

`assemble` exits `8` if `fabric-26.3` has no passing report at `protocol` — that is the gate that
stops an unproven target shipping. On `0` it writes the artifacts, the legacy aliases, one
verification report per target, `SHA256SUMS` and the compatibility record. `publish` uploads them
to the draft, re-reads them from GitHub and only then clears `draft`. A name that is already there
with identical bytes is skipped; with different bytes it is exit `7` and nothing is uploaded.
The whole contract is [releases](release.md).

## 6. Closed

The next scheduled run's reconcile finds the stable release, reads its compatibility record, and
sees that every target matched to this issue shipped, verified at or above its required level. It
writes the Lifecycle block and closes the issue as completed, in one PATCH:

```
<!-- takaro-maint:lifecycle:begin -->
## Lifecycle

| Field | Value |
| --- | --- |
| State | `released` since 2026-09-20T18:17:00Z |
| Implementation | #12 (merged into main at 2026-09-19T11:04:00Z) |
| Catalog on main | `fabric-26.3` (maintained, fingerprint `d0dac4c4c62f4f29`) |
| Release | minecraft-v<v>: takaro-minecraft-mod-fabric-26.3-<v>.jar `762d185d8991`; verified protocol |
| Why not further | — |

<!-- takaro-maint:lifecycle:end -->
```

Exit `0`. An identical rerun applies nothing and reports the issue as `closed-completed`; a later
scan leaves the closed issue exactly as it is. What `released` actually checks — the record, the
hashes, every matched target — is
[what `released` verifies](lifecycle.md#what-released-verifies).

## The same path for a Steam game

A Steam-delivered server has no manifest of versions, only a branch head, so steps 1 and 3 differ
and the rest is identical.

**1. Observed.** The scan reads the app's branch heads and their depot manifests through
`app_info_print`. When the watched branch moves to a build no target pins, it files:

```
7 Days to Die public: build <n> needs a target
```

with `provider=steam component=7d2d branch=public` in its identity marker. There is no history
behind a Steam head — [Steam discovery](steam-discovery.md) explains why the source is
heads-only.

**2. Ready.** There is no framework listing to wait for, so a Steam game's issue has no readiness
table and stays `detected` until an implementation pull request appears.

**3. Implemented.** Instead of hand-writing the pins:

```
takaro-maint steam pin --game 7d2d --metadata --record-files <path>… --write
```

reads what Steam serves on the branch now, takes the build id from the app metadata, cross-checks
it against the depots and writes a record — conventionally `linux-<version>` — pinning each depot
manifest and the sha256 of every declared file. It refuses to write hashes it has not recorded.
See [`steam pin`](steam-install.md#re-pinning-steam-pin).

**4–6** are the same: merge the record, ship it in a stable release, and the next run closes the
issue as completed.
