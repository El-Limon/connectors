# Discovery: from an upstream release to a maintenance issue

`takaro-maint scan` reads the sources the catalog watches, works out which revisions have
never been seen before, and reconciles each one into exactly one GitHub issue that says
what was published, which targets it affects, and what to do next. It is a one-shot
command: it reads no CI variables, depends on no working directory, keeps nothing on disk,
and writes nothing at all unless `--publish` is passed. Everything it knows between runs
lives in one dashboard issue, so a fresh runner with an empty home directory resumes
exactly where the last run stopped.

An observation is a statement of fact, nothing more. The scan never edits a catalog
record, never adds or promotes a target, and never claims a framework is ready.

## The command

```
takaro-maint scan [--publish] [--bootstrap] [--game G] [--source ID]... \
                  [--repo OWNER/NAME] [--api-url URL] [--out FILE]
```

| Flag | Effect |
|---|---|
| *(none)* | Read-only. Prints the plan it would carry out and performs zero tracker writes. |
| `--publish` | Actually create and update issues, and write the dashboard. |
| `--bootstrap` | Initialise a source that has no checkpoint yet (see [Bootstrap](#bootstrap-and-history)). |
| `--game`, `--source` | Restrict the run. An unknown name is a usage error, not a silent no-op. |
| `--repo`, `--api-url` | Which tracker to talk to. Also `TAKARO_MAINT_REPO` and `TAKARO_MAINT_GITHUB_API_URL`. |
| `--out FILE` | Write the same JSON report to a file as well, mode `0600`. |

A token is required in **both** modes, because read-only still reads the tracker to find
out what is already filed. It comes from `GH_TOKEN`, or from `gh auth token`; there is no
`--token` flag, so a credential never appears in a command line or a process listing.

stdout is one JSON document (`{"schemaVersion": 1, "op": "scan", …}`), diagnostics go to
stderr, and any environment value that looks like a credential is replaced with
`<redacted>` in everything the command writes.

The label the scan puts on the issues it files must already exist in the repository: the
scan never creates labels. It also has to stay on them, because the fallback listing that
guarantees no duplicates is filtered by that label.

## Identity: markers, not titles

Every issue the command owns carries an HTML-comment marker on the **first line** of its
body:

```
<!-- takaro-maint: kind=support provider=mojang component=minecraft branch=release rev=26.3 -->
```

The lookup for one marker is three steps, in order:

1. `label:… "takaro-maint" <revision>` through the issue search. GitHub's search ignores
   punctuation, so this over-matches and can also lag behind reality — it is only ever a
   pre-filter.
2. Each candidate's first line is parsed and compared as a set of key/value pairs. Token
   order does not matter, so a human who reorders the marker breaks nothing.
3. If the search found nothing, a paginated listing of **open and closed** issues with the
   label is read once per run and indexed by marker. This step is what makes "no
   duplicate, ever" true rather than likely.

Only the first non-empty line is parsed. That is deliberate: a marker quoted further down
(in a paste of another issue, say) must never be able to claim an identity. The cost is
that pushing the marker off the first line makes the issue unrecognisable and the next
scan files a fresh one — which is why the sentence under the marker in every filed issue
says to leave that line alone.

GitHub is read-after-write eventually consistent for both the search and the issue
listing, so a run started seconds after another one can fail to see what that run filed.
The scan therefore re-checks with a fresh, uncached lookup immediately before it would
create the dashboard, and stops with exit 9 ("appeared during the scan; rerun") rather than
splitting the state across two dashboards. A support issue in that same window is found by
the next run through its marker, so it is at worst filed late, never filed twice by a
retry of the same run.

A closed issue is never edited and never reopened. Closed as completed means the work
happened; closed as not planned means it was declined; either way the revision is recorded
as seen and the scan moves on. (Reopening on new evidence belongs to the lifecycle work in
#156.)

## The owned block

Between these two markers the body belongs to the command and is rewritten on every run:

```
<!-- takaro-maint:owned:begin -->
…
<!-- takaro-maint:owned:end -->
```

Everything else — the sentence above it, the title, your notes, anything you append — is
preserved byte for byte. If you delete the block entirely, the next run appends one fresh
copy and never a second. If you delete only the closing marker, everything from the
opening marker to the end of the body is treated as the generated block and replaced, so
delete both or neither. A `<!-- takaro-maint:state=… -->` line inside the block is carried
over rather than reset, so a lifecycle state written by a later command survives a rescan.

The block holds the observation (revision, release time, the source links, the upstream
hashes and sizes, the Java major version), the affected-targets table taken from the
catalog, a readiness line that says readiness is *not* assessed here, the reproducible
`takaro-maint` next steps, the evidence a fix is expected to produce, and an acceptance
checklist.

## The dashboard issue

One issue per repository, marked `kind=dashboard v=1`, holds the state:

- a human table of every source (status, head, how much it has seen, last success, last
  error) and of every piece of work filed (identity, issue, state, since);
- a fenced JSON block with the machine state: per-source `heads`, `checkpoint`, `lastError`,
  the `work` map keyed by `provider=… component=… branch=… rev=…`, and a snapshot of the
  catalog's targets.

Text outside the dashboard markers is preserved, exactly as in a support issue.

Writes are compare-and-swap: the body is re-read immediately before the update and
compared with what the run started from (and `updated_at` too, when both sides carry it).
Any difference stops the run with exit 9 and writes nothing, so two runners can never
silently overwrite each other's checkpoints. The dashboard is written once, at the end of
a run — a failure part-way through therefore leaves it untouched, and the next run picks up
from the marker rather than filing anything twice.

Bodies are capped well under GitHub's 65536-character limit. When a checkpoint outgrows the
cap, its oldest revision ids are dropped and its `floor` is raised to the newest dropped
release time. Anything released at or before the floor still counts as seen, so trimming
saves space without ever re-filing history.

## Bootstrap and history

A source with no checkpoint is *uninitialised*. In read-only mode that is reported and the
run still exits 0; with `--publish` it is a usage error, because guessing what to file on a
first run is how a tracker gets flooded.

`--bootstrap` initialises it deliberately:

- everything the source has already published is recorded as seen — no issue, no
  per-revision fetch;
- the exception is a **current head** that no catalog target covers (no target of the game
  with that revision and a `candidate` or `maintained` status): that, and only that, gets
  an issue.

After initialisation the checkpoint does the work: any revision the source reports that is
not seen is processed exactly once, whether it is newer than the head or a gap in history a
provider only now reports. A checkpoint advances only for observations that were
successfully reconciled to the tracker, and a provider failure leaves that source's
checkpoint exactly where it was — all or nothing per source.

## Exit codes

| Code | When |
|---|---|
| 0 | Everything scanned; `ok` is true. |
| 2 | Usage: an unknown game or source, or `--publish` on a source that needs `--bootstrap` first. |
| 4 | At least one source failed (unreachable, unparseable, or an integrity mismatch). Other sources still advanced. |
| 9 | Tracker failure, missing authentication, an unreadable dashboard, or a compare-and-swap conflict. |

## The provider convention

A source is scanned when its game record gives it a `watch` block:

```json
"mojang-meta": {
  "provider": "mojang",
  "baseUrl": "https://piston-meta.mojang.com",
  "watch": {
    "kind": "game",
    "component": "minecraft",
    "manifestPath": "/mc/game/version_manifest_v2.json",
    "channels": {"release": {"branch": "release", "types": ["release"]}}
  }
}
```

`kind` becomes the observation's kind, `component` the marker's component, and `channels`
maps a branch name to the upstream version types it accepts. A channel with
`"enabled": false` is skipped, and a provider ignores channel keys it does not understand,
so a new channel can be added without touching provider code.

The scan hands a provider the source record enriched with three keys — `id`, `game` and
`checkpoint` (the current checkpoint as JSON, or `null` when uninitialised) — and calls two
methods:

- `observe(source) -> ProviderResult` is the cheap step: one request, the heads it can
  report, and a lightweight observation per candidate revision built from that response
  alone. A provider that can enumerate its whole history reports `history: "full"` and may
  ignore the `checkpoint`; one that cannot (a Steam branch, say) uses it to limit what it
  asks upstream for.
- `enrich(observation, source) -> Observation` is the per-revision step and runs **only**
  for revisions the run actually reconciles, so a first scan of a decade of releases makes
  one request, not hundreds.

For Mojang, `observe()` reads the version manifest and never looks at `latest` except to
report the heads; ordering and floors use `releaseTime`, never `time` (which is a
last-modified stamp and moves when Mojang republishes an old version). `enrich()` fetches
the per-version document — by the URL *path* from the manifest entry joined to the source's
`baseUrl`, so a test upstream can serve it — enforces the entry's sha1, and adds the
dedicated server jar and the Java major version.

Every observation is validated against `catalog/schema/v1/observation.schema.json` before
it reaches the tracker.

## Recording the fixtures

The scan's scenarios run against a recorded manifest rather than the live one:

```
uv run --frozen --project maintenance python maintenance/tests/record_fixture.py mojang \
    --out maintenance/tests/fixtures/providers/mojang
```

The fixture is trimmed (the newest dozen entries, a handful of older releases, and
per-version documents cut to the keys the provider reads), so the test helper recomputes
each entry's `url` and `sha1` when it serves them. See the READMEs next to the fixtures.

## What this does not do

| Not here | Arrives with |
|---|---|
| Snapshot/preview channels, and framework (Fabric, Paper, NeoForge) readiness | #154 |
| Lifecycle transitions, `reconcile`, and a `run` that chains the commands | #156 |
| Steam branch observation | #158 |
| `dashboard show`, the CI workflow and the portable container | #165 |
| Filing real issues on the public tracker on a schedule | #166 |
