# Operating and recovering the maintenance system

The scheduled publisher watches every upstream source this repository depends on, files a
maintenance issue when something new appears and closes it once a stable release demonstrably
ships the target. This is the page you open when one of those runs is red, the board says
`degraded`, or a release did not finish.

Nothing here restates the reference pages. Every command below is defined in
[the command reference](../README.md), [operations](operations.md), [releases](release.md),
[lifecycle](lifecycle.md) or [Steam exact install](steam-install.md), and each section links to
the one that owns it.

## What you see and what it means

| Symptom | Exit | Where it shows | What to do |
|---|---|---|---|
| A source's upstream is down or refused | 4 | Red workflow run; `dashboard show` → `degraded`, that source `failed` with a `lastError` | Nothing. The next scheduled run retries it. If it persists, rerun by hand and fix the source. |
| An input's bytes are not what the catalog pins | 5 | Red run, `lastError` naming the file | Treat as upstream tampering or a re-upload: re-pin deliberately, never relax the hash. |
| The artifact or ledger belongs to another target | 7 | Red run or red release job | Wrong build reached the step. See [what blocks a release](release.md#what-blocks-a-release). |
| A release asset already exists with different bytes | 7 | Red release job, `conflicts` names the asset | [Recovering a release](#recovering-a-release). Nothing was uploaded. |
| Evidence is missing, failed, or below the target's level | 8 | Red release job, `target` names the record | The target's verification report has to pass at its `verification.required` first. |
| The dashboard moved under the run | 9 | Red run, `error` contains `changed during the scan` | Rerun it. See [the scheduled publisher](#the-scheduled-publisher). |
| No credential, or the tracker refused | 9 | Red run, nothing written | Check the token's `Issues: read & write`; see [credentials](operations.md#credentials). |
| A board nobody has scanned | 0 | `dashboard show` → `health: absent`, sources `uninitialized` | One bootstrap run; see [running it by hand](#running-it-by-hand). |

`dashboard show` itself exits 0 whatever the health: failing a process because a source failed is
`run`'s job, not the reader's.

## The scheduled publisher

The workflow runs on `cron: '17 */6 * * *'` — four times a day — under
`concurrency: {group: maintenance-publisher, cancel-in-progress: false}`, so there is exactly one
publisher at a time and a running one is never cancelled. A cancelled run could leave issues filed
without their checkpoint, which is the one state worth avoiding.

Two properties make a rerun always safe, and they are why almost every recovery below is "run it
again":

* **Compare-and-swap.** The dashboard is re-read immediately before it is written and the run
  refuses if it moved, exiting 9 with `changed during the scan`. A second writer makes a run stop
  rather than overwrite someone's checkpoints.
* **Identity lookup, not bookkeeping.** An issue is found by the identity marker in its body,
  across open *and* closed issues, so a run that filed an issue and died before recording it finds
  that same issue next time instead of filing a second one.

GitHub runs `schedule` events only on the default branch, so the cron does not fire from a branch.

## Running it by hand

```
gh workflow run maintenance.yml -f mode=publish
gh workflow run maintenance.yml -f mode=publish -f bootstrap=true -f sources="<game>/<source>"
```

`mode=read-only` (the default) prints the plan and writes nothing; `bootstrap=true` initialises
sources that have no checkpoint yet, recording what is already published as seen and filing only
the current heads no target covers. `sources` and `game` narrow the run.

From a checkout or the tool container, the same run is:

```
takaro-maint run --publish --repo <owner>/<repo>
```

See [one command, three places](operations.md#one-command-three-places) for the container
invocation and the environment it needs.

## Dashboard health

```
takaro-maint dashboard show --format table
```

`health` is derived when you ask: `absent` (no board), `ok` (every listed source `ok` and
`lastSuccess` set), otherwise `degraded`. The field-by-field meaning is in
[dashboard health](operations.md#dashboard-health).

Reading it after a red run tells you which half failed: a `failed` source with a `lastError` is an
upstream problem, an unchanged `lastSuccess` with every source `ok` is a tracker problem, and
`uninitialized` sources mean a bootstrap was never run for them.

A failed source keeps its checkpoint exactly where it was, so the retry re-reads the whole source
rather than skipping what it never managed to record. Every other source's checkpoint advances in
the same run.

## Pausing publication

Scheduled publication is gated on one tracked line in `../config/schedule.yaml`:

```yaml
publish_schedule: enabled
```

Set it to `disabled` and merge: a scheduled run then exits 0 after printing a `::notice::` and does
nothing at all. It is a reviewed change on the default branch rather than a repository setting
somebody can flip unseen, and it is the correct first move when upstream is in a state that would
file noise. Manual `workflow_dispatch` runs are unaffected — the gate only applies to `schedule`
events. The migration order for moving scheduling elsewhere is in
[schedule](operations.md#schedule); never leave two publishers running.

## Recovering a release

A release that lost its assets — a cancelled run, a runner that died mid-upload — is recovered by
dispatching the connector's own workflow against the existing tag:

```
gh workflow run <connector>.yml --ref main -f tag=<connector>-v<version> -f version=<version>
```

The run checks out that tag, so the artifacts are rebuilt from the source the tag names. The
publisher then compares every remote asset before its first upload:

* identical bytes are `skipped-identical` — no upload, no delete;
* different bytes are a conflict: exit 7 and **nothing** is uploaded, so a release that is already
  public is left exactly as it was;
* a published release stays published, and no published release or tag is ever deleted.

`release verify --tag <tag>` re-downloads the set and proves it is what its compatibility record
describes. The full contract, including the rolling/PR staging swap and its single failure window,
is [recovery](release.md#recovery).

**Legacy-mode connectors** are not yet byte-reproducible, so a rebuild can differ from the
interrupted run's bytes and stop with exit 7. Because a stable release is a draft until the
publisher finishes it, the way forward is to delete the *draft's* partial assets and dispatch
again — the exact commands are in [recovery](release.md#recovery). Never delete assets from a
release that is no longer a draft.

## Recovering an installation

An installation is staged and swapped, and the ledger is written last, so a failed preparation
leaves the previous install byte-identical rather than half-updated.

```
takaro-maint ledger check --game <game> --dest <dir>
takaro-maint install --game <game> --rollback --dest <dir>
```

`ledger check` answers "does this directory really hold that target, with the recorded bytes
intact?". `--rollback` restores the previous install kept beside it. Preserved paths — saves,
configs, plugin state — survive both an upgrade and a rollback; which paths those are per game is
the target record's `preserve` block, and the mechanics are in
[the install](steam-install.md#the-install).

An install never falls back to a branch head when a pinned input has been withdrawn: it exits 4 and
stops. The answer is a new, reviewed target, not a looser pin — see
[upstream-only retention](support-policy.md#upstream-only-retention).

## Issues a human touched

The reconcile writes only inside its own markers, so anything a person typed in an issue body
survives every run. Beyond that:

* An issue closed as **not planned** is declined, and declined is terminal: it is never re-filed
  and never reopened, however many times the head is observed again.
* An issue closed as **completed** is reported as `released` and left alone.
* A state a human set by hand inside the owned block is read back on the next run; the run reports
  `review` rather than overwriting a state it cannot justify from evidence.
* The durable fact behind every state — a pull request, the catalog on `main`, a stable release —
  is listed in [the states](lifecycle.md#the-states-and-the-durable-fact-behind-each-one). Moving an
  issue forward means producing that fact, not editing the body.

## What is kept, and for how long

| What | Where | For how long |
|---|---|---|
| Checkpoints, filed identities, per-source heads | The dashboard issue body | Until superseded; the body is capped, so history is bounded by design |
| Maintenance issues | The tracker | Indefinitely, open or closed; identity lookup reads both |
| Workflow diagnostics (`result.json`, `stderr.log`, Steam logs) | The run's artifact | 14 days |
| Release assets | GitHub Releases | Stable: indefinitely. Rolling: replaced atomically. PR builds: deleted when the PR closes |
| Downloaded upstream bytes | The local cache | It is a cache; deleting it costs a re-download and nothing else |
| Credentials | Nowhere | Read from the environment only, redacted from every output |

A fresh runner needs none of this locally: every checkpoint and every filed identity lives in the
dashboard issue, so an ephemeral runner, a container and a laptop all resume from the same place.
What the repository deliberately does **not** keep is any copy of upstream bytes — see
[upstream-only retention](support-policy.md#upstream-only-retention).
