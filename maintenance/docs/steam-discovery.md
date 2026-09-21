# Steam discovery

How `takaro-maint` reads what Steam has published for a dedicated server, and why it reads
it the way it does.

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

The default timeout is 300 seconds, which is minutes rather than seconds because a cold
steamcmd downloads about 40 MB of itself before it answers anything at all. That is not a
generous margin on a guess: a cold container run of app 294420 takes about two minutes,
and a 120-second limit was measured timing out on it.

### The log

Every run is teed to `<cache>/steam/logs/app_info-<app>.log`, beside the DepotDownloader
logs, and every line is passed through the redactor first. No credential is ever an
argument of `app_info_print` — it is an anonymous read — but the log is redacted anyway,
because "this particular file happens to be safe" is not a property worth relying on.

## The watch block

A game becomes observable by growing a `watch` block on its Steam source. Nothing else in
the game record changes, and no Python changes at all — this is the whole configuration
surface for a Steam game.

```json
"steam": {
  "provider": "steam",
  "baseUrl": "https://store.steampowered.com",
  "watch": {
    "kind": "game",
    "component": "7d2d",
    "app": 294420,
    "os": "linux",
    "depots": ["294422"],
    "channels": {
      "public": {"branch": "public"},
      "latest_experimental": {"branch": "experimental", "enabled": false}
    },
    "knownBranches": ["regex:^v[0-9]+(\\.[0-9]+)*$", "regex:^alpha[0-9]+(\\.[0-9]+)*$"]
  }
}
```

| Key | Meaning |
|---|---|
| `kind` | always `game` for a dedicated server; it is the observation kind. |
| `component` | the marker component, conventionally the game id. |
| `app` | the Steam app id whose `app_info` is read. |
| `os` | `linux`, `windows` or `macos`: which build this identity is about. Two operating systems of one app are two identities, never one. |
| `depots` | the depot ids whose content manifests make the identity. Required, and a depot the app does not publish fails the source by name. |
| `channels` | key = the upstream branch label exactly as Steam spells it; `branch` = the marker branch name; `enabled: false` = declared and deliberately unwatched; `passwordEnv` = the variable a protected branch's password comes from. |
| `knownBranches` | labels that are known and never reviewed: exact strings or `regex:<pattern>`, the selector grammar `build.references` uses. |

The channel key and the branch name are two different things because they answer to two
different grammars. Steam labels its experimental branch `latest_experimental`; a marker
revision may not contain an underscore (`tracker/identity.py`), and a marker is an
identity forever. So the label stays verbatim on the left, the marker branch is written
out on the right, and a channel whose branch name is not `[A-Za-z0-9.+-]+` fails the
source rather than filing an issue nobody can find again.

`enabled: false` is not the same as leaving a branch out. A declared-but-disabled channel
is a decision that was taken: its branch is never observed and never offered for review.

## What a revision is

```
<buildid>[.<manifest digest>]+<branch>          24994542.4f2c1e90+public
<head>/rollback/<the head it came back from>    24994542.4f2c1e90+public/rollback/25100000.a1b2c3d4+public
```

Three things are folded in, and each is there because leaving it out loses a real event:

- **the build id**, which is what a maintainer and `steam pin` talk about;
- **the watched depots' manifests**, because a publisher can replace a depot's content
  under the same build id — without this, that publish is invisible;
- **the branch**, because the same build appearing on `public` after `experimental` is a
  new thing to support, and a single checkpoint per source has to tell them apart.

A branch under review whose manifests are encrypted has no digest to fold in, so its
revision is `<buildid>+<branch>`.

### Rollback, promotion, and the arithmetic that is never done

A build id is not a version number: branches are repointed backwards, and a *lower* build
id may be one nobody has ever seen. So no number is ever compared.

- **Rollback** is decided by the checkpoint alone (`channels.head_event`): the current head
  is already in `seen`, and some other head of the same branch was first seen later. It is
  filed once, under the `…/rollback/…` revision, and titled "rolled back to build N".
- **Promotion** is decided by identity: the same `<buildid>.<digest>` was seen on another
  branch. The new branch's observation carries `facts.promotedFrom`, and the branch it came
  from keeps its own issue untouched.
- **A build nobody has seen** is a new head, whatever its number is.

## Heads-only: what a Steam scan does not see

`app_info_print` publishes the *current* head of each branch and no history at all. A build
that was published and replaced between two scans was never observed, and nothing here
pretends otherwise: the source reports `history: "heads-only"`, every observation carries
`facts.observationLimit`, and the sentence is rendered in the issue body:

> heads-only: Steam app metadata exposes only the current head of each branch; builds
> published between two scans are not observed and are not claimed.

A bootstrap therefore seeds exactly the current heads — one entry per branch, never one per
manifest the app happens to list — and files only the heads no target already ships.
"Already ships" is decided on Steam's terms (`covers()`): a target's `steam-depots` input
has to pin the same app, branch, build id, operating system and every watched depot's
manifest. A target named `linux-3.2.0.b10` covers a head; its *name* never matches it.

## Protected and hidden branches

A branch with `pwdrequired 1` publishes its manifests encrypted, and `privatebranches 1`
means the app has branches an anonymous login is not shown at all. Both are deliberate
failures rather than quiet successes:

| Situation | What happens |
|---|---|
| an enabled channel whose label needs a password, with no variable set | the source fails (exit 4) naming the variable it wants |
| an enabled channel whose label is not listed at all | the source fails, and says `privatebranches=1` when that is why |
| the variable is set | the manifests are resolved through DepotDownloader `-manifest-only -branch <label> -branchpassword`, and the observation succeeds |

The variable is `TAKARO_MAINT_STEAM_BRANCH_PASSWORD__<app>__<LABEL>` (label upper-cased,
every character outside `[A-Z0-9]` replaced by `_`), or whatever the channel's
`passwordEnv` names instead.

**The value is never written anywhere.** What is recorded in `facts.credentialsEnv`,
rendered in the issue and printed in an error is the variable's NAME. The provider never
puts the value into a string, DepotDownloader hides it in its own log whatever its length,
and the redactor hides every environment value whose name looks like a secret on top of
that. The tests prove it for a long password and for a four-character one, over stdout,
stderr, `--out`, every issue title and body, the dashboard and every file under
`<cache>/steam/logs/`.

## Branches nobody declared

Every label the app publishes that is neither a declared channel (enabled or not) nor
matched by a `knownBranches` selector becomes a `branch-review` observation and exactly one
review issue per `(branch, revision)`. Closing that issue as *not planned* declines the
branch for good; it is never re-filed. A new build on the same undeclared branch is a new
revision, and therefore a new question.

The label is folded into the marker alphabet for the branch name — `beta_test` becomes
`beta-test` — because a revision with an underscore in it fails the observation schema.

## Partial failure

Sources are independent. A Steam source that fails leaves its checkpoint exactly where it
was while every other source in the run advances; the run exits 4, and the next run retries
the whole source. Nothing is quarantined and nothing is half-recorded: a source's
checkpoint only ever moves when everything it observed was reconciled.

## Configuration

| Variable | Meaning |
|---|---|
| `TAKARO_MAINT_STEAMCMD` | The command line that runs steamcmd — a command line, not a path, like `TAKARO_MAINT_GRADLE`. Unset means plain `steamcmd` on `PATH`. |
| `TAKARO_MAINT_STEAM_BRANCH_PASSWORD__<app>__<LABEL>` | The password of one protected branch, read only from the environment. A channel may name a different variable with `passwordEnv`. |

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

## Commands

### `steam branches --game G [--app N] [--depot D…]`

Everything the app publishes, sorted by label and shaped for diffing between two runs:
build id, publish time, description, whether the branch needs a password, and the depot
manifests it currently points at. Each label is also classified against the game's watch
block:

| Classification | Meaning |
|---|---|
| `watched` | an enabled channel declares it |
| `declared` | a channel declares it with `enabled: false` — known, deliberately unwatched |
| `known` | a `knownBranches` selector covers it (the tagged version branches, say) |
| `unfamiliar` | nobody has decided about it yet |

No credential is read here: a protected branch is reported as `pwdrequired: true` with its
manifests listed under `encrypted`, which is exactly the information needed to decide
whether to go and get a password.

### `steam pin --metadata`

The build id is not in a depot manifest — DepotDownloader never sees one — so `--metadata`
reads it from the app metadata and cross-checks every watched depot's manifest against
what the depot itself just served.

The two reads are a moment apart. If they disagree, a publish is in flight between them,
and recording that pair would pin a build id to manifests that never shipped under it.
That is reported as a retry (exit 4), not recorded. `--metadata` together with an explicit
`--buildid` is a usage error: one of them is the answer, not both.

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
| a branch the app does not list, or metadata and depot disagreeing on the head | `4` (upstream unavailable) |
| a protected branch with no password in the environment | `4` (upstream unavailable) |
| a watch block missing a key, during a scan | `4` — `scan` reports every provider failure as a failed source, and the message names the key |
| `--metadata` together with `--buildid`, or a game with no app and no `--app` | `2` (usage) |

## What the issue marker does not carry

The support marker is `kind provider component branch rev` and nothing else, so the app id
is not in it: `component` and app are one-to-one for a game, and the app is in the body and
in `facts` where a reader needs it. Adding a key to the marker would change the identity of
every issue already filed.
