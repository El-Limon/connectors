# `takaro-maint`

One command that turns a catalog record into a running, verified connector: resolve a target,
install its exact inputs, build the artifact, deploy it, and prove it works.

It is the same command everywhere — a laptop, the dev rig, CI — so all three resolve the same
bytes instead of three scripts that drift.

```
maintenance/bin/takaro-maint targets list
maintenance/bin/takaro-maint targets resolve --game minecraft --target fabric-26.2
maintenance/bin/takaro-maint install --game minecraft --dest /srv/minecraft
maintenance/bin/takaro-maint build --game minecraft --version 0.1.1 --out dist
maintenance/bin/takaro-maint deploy --game minecraft --dest /srv/minecraft --from dist/build-manifest.json
maintenance/bin/takaro-maint verify --game minecraft --artifacts dist --out reports
```

`just maint <args>` is the same thing from the repository root.

## Requirements

[uv](https://docs.astral.sh/uv/) 0.12.x, and nothing else — the launcher runs the command in a
frozen, managed Python 3.12 environment. Docker is needed for `verify` and for
`build --toolchain container`.

## Commands

| Command | What it does |
|---|---|
| `catalog validate [--online]` | Check every catalog invariant. `--online` re-downloads each pinned input and compares hashes. |
| `catalog record-hash --game G --target ID --field F` | Download an input twice and record the sha256 both downloads agree on. |
| `targets list [--game G] [--platform P] [--status …] [--rig-game ID] [--format json\|table\|gha]` | List targets. `gha` prints a build matrix. |
| `targets resolve --game G [--target ID] [--format json\|env\|gha] [--prefix P] [--out FILE]` | The full resolution: fingerprint, image refs, artifact names, URLs and deployment environment. |
| `install --game G [--target ID] --dest DIR` | Download and verify every pinned input, stage it, swap it into place, write the ledger. |
| `steam pin --game G [--target ID] [--metadata] [--record-files PATH…] [--write]` | Read what Steam serves on the branch now, report which depots moved, and re-pin the target. `--metadata` takes the build id from Steam's app metadata and cross-checks it against the depots. |
| `steam branches --game G [--app N] [--depot D…]` | List every branch the app publishes — build id, publish time, depot manifests — and say which ones the catalog watches, declares, knows or has never decided about. |
| `steam references --game G [--target ID] --dest DIR` | Fetch only the assemblies the build compiles against from the pinned manifests, into a per-fingerprint directory. |
| `ledger check --game G [--target ID] --dest DIR` | Does this directory really hold that target, with the recorded bytes intact? |
| `build --game G [--target ID \| --all-targets] --version V --out DIR [--toolchain host\|container]` | Build the artifacts, validate their identity, write `build-manifest.json`, `SHA256SUMS` and per-file `.meta.json`. |
| `artifact validate --game G --target ID FILE…` | Does this file carry that target's identity? |
| `deploy --game G [--target ID] --dest DIR --from build-manifest.json` | Put the built artifact into an installed game directory and record it in the ledger. |
| `verify --game G [--target ID] --artifacts DIR --out DIR` | Boot the target in a container against a local fake Takaro and run the checks; writes a verification report. |
| `docs render --game G [--write]` | Regenerate the target table in `games/<g>/README.md`. |
| `release assemble --connector C --version V --channel stable\|rolling\|pr --tag TAG --dist DIR --out DIR [--reports DIR]` | Turn the per-target builds and verification reports into one complete release directory: artifacts, legacy aliases, reports, `SHA256SUMS` and the compatibility record. |
| `release publish --connector C --channel … --tag TAG --assembled DIR` | Upload that directory, read it back from GitHub, and finalise the release. Never clobbers: identical bytes are skipped, conflicting bytes stop the run. |
| `release verify --tag TAG [--connector C] [--expect DIR] [--out DIR]` | Download every asset of a release and prove it is the set its compatibility record describes. |

Selection is the same everywhere: `--target` wins; without it the single `default: true` record
for the game (and `--platform`, when given) is used. Zero or two defaults is an error rather than
a guess.

## Output and exit codes

stdout is exactly one JSON document per run (`{"schemaVersion": 1, "op": …, "ok": …, …}`) unless
`--format env|gha|table` asks for something else. Progress and diagnostics go to stderr, where
`--quiet` and `--verbose` apply. Any value of an environment variable that looks like a
credential is replaced with `<redacted>` in everything the command writes.

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | unexpected error (a traceback is printed) |
| 2 | usage, configuration or catalog invalid |
| 3 | target not found, or an ambiguous default |
| 4 | an upstream input or provider is unavailable |
| 5 | integrity mismatch — the bytes are not what the catalog pins |
| 6 | the build failed |
| 7 | artifact, ledger or asset conflict (wrong target, stale fingerprint, incomplete set) |
| 8 | verification failed, or its evidence is missing |
| 9 | tracker/GitHub failure, or missing authentication |
| 130 | interrupted |

## Configuration

Resolution never depends on the working directory or on CI-only variables, so the same command
line behaves the same from anywhere.

| Variable | Effect |
|---|---|
| `TAKARO_MAINT_CACHE` | Download cache (default `~/.cache/takaro-maint`). Also `--cache-dir`. |
| `TAKARO_MAINT_GRADLE` | Override the build command (tests use a stub). |
| `TAKARO_MAINT_DOCKER` | Override the docker binary (tests use a stub). |
| `TAKARO_MAINT_DEPOTDOWNLOADER` | Override the pinned DepotDownloader executable (tests use a stub). |
| `TAKARO_MAINT_STEAMCMD` | Override the steamcmd command line — a command line, so it can be a `docker run …` (tests use a stub). |
| `TAKARO_MAINT_STEAM_BRANCH_PASSWORD__<app>__<LABEL>` | The password of one protected Steam branch, read only from the environment and never written anywhere. |
| `TAKARO_MAINT_REPO` | Repository for GitHub operations. Also `--repo`. |
| `TAKARO_MAINT_GITHUB_API_URL` | GitHub API base. Also `--api-url`. |
| `GH_TOKEN` | GitHub token; otherwise `gh auth token` is tried. |

`--repo-root PATH` points the command at another checkout.

## Development

```
uv run --frozen --project maintenance pytest -q maintenance
uv run --frozen --project maintenance ruff check maintenance
uv run --frozen --project maintenance mypy maintenance/src
```

Tests drive the real command and assert on exit codes and the JSON it prints, against in-process
fakes for upstream, GitHub and docker. Dependencies are pinned by `uv.lock` and installed with
`--frozen`, so a run here and a run in CI resolve the same versions.

## Docs index

<!-- Later maintenance work adds one line each as its area lands. -->

- [The catalog](../catalog/README.md) — what a target record is and how to add one.
- [Discovery](docs/discovery.md) — how `scan` turns upstream releases into deduplicated maintenance issues.
- [Runtime verification](docs/verify.md) — what `verify` boots, checks and reports, locally and in CI.
- [Steam exact install](docs/steam-install.md) — pinned DepotDownloader, depot manifests, staged swap and rollback for Steam-delivered servers.
- [Steam discovery](docs/steam-discovery.md) — reading Steam branch heads and depot manifests through `app_info_print`.
- [Adding a game](docs/adding-a-game.md) — the provider contract: what a new connector adds to the catalog and what it never touches.
- [Releases](docs/release.md) — the release channels, the asset names, the compatibility record and how a release is recovered.
