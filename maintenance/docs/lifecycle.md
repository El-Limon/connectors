# Lifecycle: from a maintenance issue to a verified release

`takaro-maint scan` files an issue when upstream publishes something. `takaro-maint
reconcile` answers the other half of the question: where has that piece of work got to, and
is it finished? It recomputes every maintenance issue's state from scratch on every run,
writes that state back, and closes an issue only once the latest stable release demonstrably
carries what the issue asked for.

It never merges anything, never publishes anything, never posts a comment, never reopens an
issue, never edits a closed one, and never creates a dashboard. The reviewed Release PR
process is untouched: this command reads releases, it does not make them.

## The commands

```
takaro-maint reconcile [--publish] [--game G] [--issue N]... [--catalog-ref REF] \
                       [--repo OWNER/NAME] [--api-url URL] [--out FILE]

takaro-maint run [--publish] [--bootstrap] [--game G] [--source ID]... \
                 [--catalog-ref REF] [--repo OWNER/NAME] [--api-url URL] [--out FILE]
```

| Flag | Effect |
|---|---|
| *(none)* | Read-only. Prints the plan it would carry out and performs zero tracker writes. |
| `--publish` | Actually write the state token, the Lifecycle block, the close and the dashboard. |
| `--game` | Restrict the run to one catalog game. An unknown name is a usage error. |
| `--issue N` | Restrict the run to these issue numbers; repeatable. An unknown or unmarked number is reported `not-a-support-issue`, not an error. |
| `--catalog-ref` | The ref whose catalog counts as published. Default `main`. |
| `--repo`, `--api-url` | Which tracker to talk to. Also `TAKARO_MAINT_REPO` and `TAKARO_MAINT_GITHUB_API_URL`. |
| `--out FILE` | Write the same JSON report to a file as well, mode `0600`. |

A token is required in **both** modes, because read-only still reads the tracker. It comes
from `GH_TOKEN`, or from `gh auth token`; there is no `--token` flag, so a credential never
appears in a command line or a process listing.

stdout is one JSON document (`{"schemaVersion": 1, "op": "reconcile", …}`), diagnostics go
to stderr, and any value that looks like a credential is replaced with `<redacted>`.

## The states, and the durable fact behind each one

| State | What is true |
|---|---|
| `detected` | The issue exists. No framework has reported yet. |
| `blocked-upstream` | Readiness rows exist and no platform can build this game version yet. |
| `ready-for-agent` | Some platform's framework has shipped for this game version. |
| `implementation-pr` | A pull request references the issue and is open, or merged somewhere the target has not reached `--catalog-ref` from. |
| `awaiting-release` | A non-retired catalog target on `--catalog-ref` matches the issue's identity, and the latest stable release does not yet prove it shipped. |
| `released` | The latest stable release proves every matched target shipped. The issue is closed as completed. |

Three more states are reported and never written by this command: `declined` (closed as not
planned — terminal unless a human reopens it), `superseded` (a preview the release promoted)
and `review` (a branch-review issue).

The precedence is strict and only ever looks forward:

1. the release proves every matched target → `released`;
2. otherwise a matched target on the ref → `awaiting-release`;
3. otherwise an open or merged pull request → `implementation-pr`;
4. otherwise readiness recomputes `blocked-upstream` / `ready-for-agent`, or `detected` when
   no framework has reported.

Step 4 recomputes readiness from the rows alone, deliberately ignoring the state already in
the body. That is what lets an issue walk *back* when a pull request is closed without
merging: nothing later is true any more, so nothing later should be claimed. A scan cannot
walk an issue backwards, because `readiness.OWNED_STATES` stops it overwriting anything past
`ready-for-agent`.

## The two blocks

The **state token** — `<!-- takaro-maint:state=… -->` — lives inside the scan's owned block
and is the single machine-read state, exactly as platform readiness designed it.

The **Lifecycle block** sits *after* `<!-- takaro-maint:owned:end -->`:

```
<!-- takaro-maint:lifecycle:begin -->
## Lifecycle

| Field | Value |
| --- | --- |
| State | `awaiting-release` since 2026-09-21T12:00:00Z |
| Implementation | #12 (merged into main at 2026-09-21T11:00:00Z) |
| Catalog on main | `fabric-26.3` (maintained, fingerprint `0123456789abcdef`) |
| Release | — |
| Why not further | minecraft-v0.1.1 carries no compatibility record |

<!-- takaro-maint:lifecycle={…} -->
<!-- takaro-maint:lifecycle:end -->
```

A Lifecycle block that begins and never ends is treated as unrecognisable, like a body with
no state token: rewriting it to end-of-body would silently delete whatever a person wrote
below the damaged marker.

It sits outside the owned block because **the scan regenerates the owned block in full on
every run**: anything written inside it other than the state token is erased by the next
scan. Everything outside both blocks belongs to whoever typed it and is preserved byte for
byte.

The block carries no run timestamp. `since` is the only time in it, and it only moves when
the state moves — so a decision that has not changed renders byte for byte identically and
produces no write at all.

## Pull requests

An implementation pull request should say `Refs #n`, never `Closes #n`. GitHub closes an
issue itself when a pull request with a closing keyword merges, and that would close the
issue before anything was released. The tool cannot prevent it; it warns instead, once per
run, for every open pull request that would do it:

> PR #12 will close #156 on merge; maintenance PRs should use Refs #156

An issue closed as completed whose state is not `released` is reported
`closed-before-release`. It is never reopened — reopen it by hand and the next run picks it
up like any other open issue.

Pull requests are found with one paginated `GET /pulls?state=all` per run, never with the
search API: search lags by seconds to minutes and ignores punctuation, so `Refs #12` and a
bare `12` look the same to it. The cost is eventual consistency — a pull request opened
moments before a run may only be seen by the next one — and that is free, because the state
is recomputed from scratch every time.

A pull request closed without merging counts for nothing.

## The catalog on `main`

Targets are read through the contents API at `--catalog-ref`, not from the checkout. The
branch the command runs from is precisely the branch that is not merged yet, so a target that
exists only there has not been published to anyone. A missing directory is a note
(`catalog/minecraft/targets is not on main`), not a failure.

A target matches an issue when its support status is not `retired`, its `game` matches, and
the first of its inputs whose kind can identify a target agrees with the marker:

| input kind | match rule |
|---|---|
| `mojang-version` | marker provider `mojang`, `input.version` == marker `rev`, and the record's own `revision` == marker `rev` |
| `paper-build`, `neoforge-installer` | marker provider `mojang`, `input.gameVersion` == marker `rev`, and the record's own `revision` == marker `rev` |
| `steam-depots` | marker provider `steam`, and `app`, `branch` and `buildid` all match the marker |
| anything else (`fabric-launcher`, `maven-artifact`, `http-file`, …) | never matches — a loader or library is a build detail |

A retired match is reported (`fabric-26.3 is retired on main`) and does not count. Several
matches — one per platform — are all reported, and the issue only reaches `released` when
*every* one of them shipped.

## What `released` verifies

The latest **published, non-prerelease** release tagged `<connector>-v…` is the only one
consulted. An older release that shipped a target the newest one dropped does not make that
target published today.

Two small files are downloaded: `SHA256SUMS` and the compatibility record. **The artifacts
are never downloaded** — GitHub publishes each asset's `size` and `digest`, and combined with
a `SHA256SUMS` whose entry for the record is checked against the record's own bytes, fetching
hundreds of megabytes of jars would prove nothing more.

A *note* stops the check for the whole connector, with that one reason on every issue:

- `<tag> carries no compatibility record` — the release predates the record, or was built by
  hand. (This is checked before `SHA256SUMS`, because a release with no record cannot be
  checked at all and that is the more useful thing to say.)
- `<tag> carries N compatibility records`
- `<tag> carries no SHA256SUMS`
- `<record> does not match SHA256SUMS`
- `<record> is not a valid compatibility record: …`
- `<record> describes tag|connector|repo X, not Y`

Otherwise, per matched target, every one of these is a named reason on the issue:

- `<tag> does not list <id>`
- `<tag> ships <id> with fingerprint <a>, main has <b>`
- `<tag> ships <id> as candidate; promote it to maintained`
- `<id> verified only to <executed>, requires <required>` — `required` comes from the record
  in the catalog, not from the release's claim about itself
- `<tag> ships no <role> artifact for <id>` — a target's `components` are **not** part of its
  fingerprint, so a target that gained a role on the ref still matches a release that only
  ever shipped the old set; the roles are checked by name as well
- per artifact: `asset missing: <name>`, `SHA256SUMS disagrees for <name>`,
  `size differs for <name>`, `GitHub digest differs for <name>`
- the same presence and checksum test for the verification report the record names

`released` additionally requires every matched target to be `maintained` **on the ref**, not
only in the release record. Support status is deliberately outside the fingerprint, so a
release claiming `maintained` cannot stand in for the catalog promotion the issue's own
acceptance checklist ends with.

No reasons means `released`.

## Idempotence and recovery

- **Read-only writes nothing.** Not one non-`GET` request.
- **The close is one PATCH.** Before it, the issue is re-read: still open, same marker,
  `updated_at` unchanged since the listing. Anything else is reported `moved-during-run` and
  left for the next run. Body and `state`/`state_reason` go in that single PATCH, so an
  interrupted run never leaves a body that says `released` on an open issue — and if the run
  dies before reaching the issue at all, the next one reaches the same conclusion.
- **An identical rerun writes nothing.** The block is byte-identical, so there is no PATCH,
  and the dashboard is only written when its data actually changed.
- **The dashboard is compare-and-swap.** A concurrent edit exits 9 rather than overwriting
  someone's checkpoints. Issue writes already made in that run stand, and the rerun is a
  no-op for them — but it still mirrors their state into the dashboard, so the entry the
  failed run never managed to save is repaired rather than left stale forever.
- **No comments are ever posted**, so "no duplicate comments" holds by construction.
- **`declined` is terminal.** A closed-as-not-planned issue is never written, even when the
  release facts would otherwise close it.

## `run`

`takaro-maint run` is `scan` and then `reconcile` in one process and one JSON document:

```json
{"schemaVersion": 1, "op": "run", "ok": true,
 "scan": {…}, "reconcile": {…}, "exitCodes": {"scan": 0, "reconcile": 0}}
```

The exit code is the scan's when it is nonzero, otherwise the reconcile's. A scan that failed
with exit 9 — a broken token, a broken API, a dashboard that moved — skips the reconcile
entirely, because the second half would only repeat the same failure. Any other scan failure
still lets the reconcile run: an upstream that is down says nothing about where the existing
issues have got to. The reconcile loads the dashboard *after* the scan saved it, so it reads
the fresh state rather than the one the scan started from.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The run completed. A mismatch between the catalog and a release is a *state*, not a failure. |
| 2 | Usage: an unknown `--game`. |
| 9 | Tracker: no token, an HTTP failure, or a dashboard that moved under the run. |

## Not yet proven

No real maintenance issue has been walked through to `released` yet. This command is proven
on in-process fakes end to end — discovery, blocked prerequisites, an open pull request, a
merge that deliberately does *not* close the issue, three kinds of mismatched release
metadata, the close, an identical rerun and a recovered interrupted close — and read-only
against the live repository, where it correctly reports that the current stable Minecraft
release carries no compatibility record. The first real transition will be possible once a
Minecraft release ships a compatibility record; until then, treat `released` as verified
logic on unverified ground.
