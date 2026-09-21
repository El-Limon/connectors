# Steam discovery

How `takaro-maint` reads what Steam has published for a dedicated server, and why it reads
it the way it does.

> **Status.** This document covers what is implemented today: the reader, the runner and
> the recorded fixture. Turning a branch head into a maintenance issue is the next step on
> the same foundation and is not described here yet.

## Where the facts come from

Steam publishes no "what is the current build of this branch" endpoint. The store page is
HTML, the Web API answers about apps rather than about branches, and none of it mentions a
depot's content manifest. The one published source is the tool the exact installs already
depend on:

```
steamcmd +login anonymous +app_info_update 1 +app_info_print <app> +quit
```

It is an anonymous, read-only call. `app_info_update 1` forces the metadata to be
refreshed rather than answered from whatever the local cache happens to hold — a stale
answer is indistinguishable from "nothing changed", which is the one mistake a discovery
run must not make.

The output is a Valve KeyValues document wrapped in console noise:

```
Redirecting stderr to '/root/Steam/logs/stderr.txt'
[  0%] Checking for available updates...
…
AppID : 294420, change number : 39026857/39026857, last change : Mon Sep 21 12:53:46 2026
"294420"
{
	"common"    { "name"  "7 Days to Die Dedicated Server"  … }
	"depots"
	{
		"294422" { "config" { "oslist" "linux" } "manifests" { "public" { "gid" "1633674551820196085" … } } }
		"branches" { "public" { "buildid" "24994542" "timeupdated" "1788196235" } … }
		"privatebranches"  "1"
	}
}
Unloading Steam API...OK
```

## The reader (`takaro_maint.steam.vdf`)

`parse()` turns KeyValues text into nested dictionaries; `dump()` writes them back in
Steam's own spelling; `extract_app(text, app)` finds the app's block inside a whole
capture and also reads the `AppID :` header line.

It is a parser and not a set of regular expressions on purpose. A manifest id looks the
same wherever it appears, so a pattern match over the raw capture can quite happily read
one branch's manifest while reporting another branch's build id. Parsing makes the
structure — *which* branch, *which* depot — part of the answer.

Three shapes in the real document are worth knowing about, because a naive reader trips on
each of them and the recorded fixture keeps all three:

- **Depots with no manifests.** `228983`, `228988` and `228989` are borrowed from another
  app through `depotfromapp` and pin no content of their own.
- **A scalar among the blocks.** `overridescddb` is a bare `"1"` sitting in the middle of
  the depot blocks, and `privatebranches` is another.
- **A branch with no `timeupdated`.** `alpha12.5` carries only `timebuildupdated`; an old
  branch that has not been repointed since Steam started recording the pointer.

## The runner (`takaro_maint.steam.steamcmd`)

`app_info(app, …)` runs the command, records it, and returns typed `Branch` and `Depot`
values. Only depots that actually name content (a `manifests` or `encryptedmanifests`
mapping) are returned; the borrowed depots and the scalars above are not depots this can
pin and are dropped.

### The truncation retry

`app_info_print` is documented — and observed — to truncate when it is not attached to a
terminal: it exits **0** having printed an app block with nothing in it. Anything that
greps the output reads that as a real answer, which is why the block is parsed instead,
the empty one is recognised, and the whole command is retried **exactly once**.

Never twice. A second empty answer is a broken upstream, and a scan that retried a
two-minute command five times would take ten minutes to say so. Every other failure — a
non-zero exit, a missing tool, a timeout — is reported immediately, because none of them
is transient in a way a retry would fix.

### Timeouts

The default timeout is 120 seconds, which is minutes rather than seconds because a cold
steamcmd downloads about 40 MB of itself before it answers anything at all.

### The log

Every run is teed to `<cache>/steam/logs/app_info-<app>.log`, beside the DepotDownloader
logs, and every line is passed through the redactor first. No credential is ever an
argument of `app_info_print` — it is an anonymous read — but the log is redacted anyway,
because "this particular file happens to be safe" is not a property worth relying on.

## Configuration

| Variable | Meaning |
|---|---|
| `TAKARO_MAINT_STEAMCMD` | The command line that runs steamcmd — a command line, not a path, like `TAKARO_MAINT_GRADLE`. Unset means plain `steamcmd` on `PATH`. |

The host running a scan does not need steamcmd installed. Pointing the variable at a
container works because the variable is a whole command line:

```
export TAKARO_MAINT_STEAMCMD="docker run --rm cm2network/steamcmd@sha256:<digest> ./steamcmd.sh"
```

The same mechanism is what the tests use: they point it at `tests/fake_steamcmd.py`, which
is a real subprocess printing a real document, so the command line, the retry, the log and
the parse are all exercised rather than mocked.

## Identity

A Steam build is **not** identified by its build id alone. A publisher can replace a
depot's content under the same build id, and two operating systems' depots live under one.
`manifest_digest({depot: manifest, …})` folds the watched depots' manifest ids into eight
hex characters, so either of those is a different identity — short enough to read in an
issue title, and independent of the order the depots were listed in.

## Recording the fixture

`maintenance/tests/fixtures/providers/steam/294420/` holds a trimmed capture of the real
app, and its `README.md` gives the exact command that recorded it and lists what was cut.
Re-recording is only needed if Steam changes the *shape* of the document: the tests bend a
parsed copy of the fixture in memory — new build ids, extra branches, encrypted manifests
— and write it back through `vdf.dump`, so no scenario depends on a fresh capture.

## Exit codes

| Situation | Code |
|---|---|
| steamcmd missing, failing, timing out, or truncating twice | `4` (upstream unavailable) |
