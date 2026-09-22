"""The Enshrouded target end to end: catalog, discovery, install, build, deploy, verify.

Everything here drives the real command against the DepotDownloader and steamcmd stand-ins
and a stub build script, so what is asserted is what a maintainer, the rig and CI observe:
exit codes, the JSON on stdout, the argv the tools were handed and the bytes on disk.

The pieces that cannot be exercised without a Windows server under Proton -- the plugin's
signature self-check and the sidecar's socket -- are covered here by their *classification*
(``classify_health``) and their recorded log lines. Booting the real thing is what
``takaro-maint verify --game enshrouded`` does, and its evidence lives outside this repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

import fake_depotdownloader as fake_dd
import fake_steamcmd
from takaro_maint.catalog import ids
from takaro_maint.games import adapter_for
from takaro_maint.games.enshrouded import verify as hooks
from takaro_maint.publish.manifest import artifact_row, write_manifest, write_meta

GAME = "enshrouded"
TARGET = "proton-1024233"
APP = 2278520
GAME_DEPOT = "2278521"
REDIST_DEPOT = "1004"
PINNED = {GAME_DEPOT: "2174935030716737236", REDIST_DEPOT: "7604377918839582995"}
MOVED = {GAME_DEPOT: "2900000000000000001", REDIST_DEPOT: "7900000000000000001"}
VERSION = "0.4.3-dev.abc1234"
PLUGIN_ZIP = f"takaro-enshrouded-plugin-{TARGET}-{VERSION}.zip"
SIDECAR_ZIP = f"takaro-enshrouded-sidecar-{TARGET}-{VERSION}.zip"

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "games" / "enshrouded"
DEPOTS = FIXTURES / "depots"
BUILD_SCRIPT = "games/enshrouded/scripts/build-release.sh"
UPDATER = "games/enshrouded/server/enshrouded-updater"


# --------------------------------------------------------------------------- the repo copy


def _tree(manifests: dict[str, str], depot: str) -> Path:
    return DEPOTS / depot / manifests[depot] / "tree"


def repin(root: Path, manifests: dict[str, str] = PINNED) -> dict[str, Any]:
    """Point the copied Enshrouded target at the fixture depots and record their hashes."""
    path = root / "catalog" / GAME / "targets" / f"{TARGET}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    server = record["inputs"]["server"]
    server["depots"] = {
        depot: {
            "manifest": manifests[depot],
            "size": sum(f.stat().st_size for f in _tree(manifests, depot).rglob("*") if f.is_file()),
            "files": len([f for f in _tree(manifests, depot).rglob("*") if f.is_file()]),
        }
        for depot in sorted(manifests)
    }
    declared = {}
    for name in sorted(server["files"]):
        candidates = (_tree(manifests, depot) / name for depot in manifests)
        found = next((path for path in candidates if path.is_file()), None)
        assert found is not None, f"no fixture depot holds the declared file {name}"
        declared[name] = {
            "sha256": hashlib.sha256(found.read_bytes()).hexdigest(),
            "size": found.stat().st_size,
        }
    server["files"] = declared
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def read_target(root: Path) -> dict[str, Any]:
    path = root / "catalog" / GAME / "targets" / f"{TARGET}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def write_target(root: Path, record: dict[str, Any]) -> Path:
    path = root / "catalog" / GAME / "targets" / f"{TARGET}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def make_repo(tmp_path: Path, *, repin_target: bool = True) -> Path:
    """A repository copy holding the real catalog, the tool lock and the tracked scripts."""
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "catalog", root / "catalog")
    (root / "maintenance").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "maintenance" / "tools.lock.json", root / "maintenance" / "tools.lock.json")
    for relative in (BUILD_SCRIPT, UPDATER):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    if repin_target:
        repin(root)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def dd_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "depotdownloader-argv.jsonl"
    for key, value in fake_dd.environment(tmp_path, log, FAKE_DD_ROOT=str(DEPOTS)).items():
        monkeypatch.setenv(key, value)
    return log


def resolve(run: Any, repo: Path) -> dict[str, Any]:
    code, payload, err = run("targets", "resolve", "--game", GAME, "--target", TARGET, repo=repo)
    assert code == 0, err
    return dict(payload)


# --------------------------------------------------------------------------- 1-2 catalog


def test_catalog_validate_accepts_the_enshrouded_target(run: Any) -> None:
    code, payload, _ = run("catalog", "validate")

    assert code == 0, payload
    rows = [check for check in payload["checks"] if check["file"].endswith(f"{TARGET}.json")]
    assert {check["id"] for check in rows} >= {
        "input-kind-schema",
        "build-system-schema",
        "build-script-exists",
        "no-null-hash",
        "immutable-tag-and-digest",
        "no-floating-words",
    }
    assert all(check["status"] == "pass" for check in rows), rows


def test_targets_resolve_env_for_enshrouded(run: Any) -> None:
    code, payload, _ = run("targets", "resolve", "--game", GAME, "--target", TARGET, "--prefix", "ENSHROUDED")

    assert code == 0, payload
    env = payload["env"]
    assert env["ENSHROUDED_STEAM_DEPOTS"] == "1004:7604377918839582995;2278521:2174935030716737236"
    assert env["ENSHROUDED_STEAM_APP"] == str(APP)
    assert env["ENSHROUDED_STEAM_BRANCH"] == "public"
    assert env["ENSHROUDED_STEAM_BUILDID"] == "23178631"
    assert env["ENSHROUDED_ARTIFACT_SERVER_PLUGIN"] == f"takaro-enshrouded-plugin-{TARGET}-{{version}}.zip"
    assert env["ENSHROUDED_ARTIFACT_SIDECAR"] == f"takaro-enshrouded-sidecar-{TARGET}-{{version}}.zip"
    assert env["ENSHROUDED_SIDECAR_RUNTIME"].startswith("node:22-alpine@sha256:")
    assert env["ENSHROUDED_PROTON"] == "GE-Proton10-30"
    # The hook-compatibility statement, and the fact that it is the pinned build itself.
    assert env["ENSHROUDED_HOOKS_PROVEN_BUILD"] == env["ENSHROUDED_REVISION"] == "1024233"
    assert not any(key.endswith("_JAVA") for key in env)
    assert payload["resolvedUrls"]["server"].startswith(f"steam://app/{APP}/branch/public/build/23178631/")


# --------------------------------------------------------------------------- 3 discovery


def _steam_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Serve the recorded 2278520 probe through the real steamcmd command line."""
    root = tmp_path / "steam-root"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / "app_info_2278520.vdf", root / f"{APP}.vdf")
    for key, value in fake_steamcmd.environment(root, tmp_path / "steamcmd-argv.jsonl").items():
        monkeypatch.setenv(key, value)
    return root


def _source(repo: Path) -> dict[str, Any]:
    game = json.loads((repo / "catalog" / GAME / "game.json").read_text(encoding="utf-8"))
    return {**game["sources"]["steam"], "id": "steam", "game": GAME, "checkpoint": None}


def test_scan_covers_the_pinned_head_and_files_a_moved_one(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint import paths
    from takaro_maint.catalog.loader import load
    from takaro_maint.providers.steam import PROVIDER

    root = _steam_rig(tmp_path, monkeypatch)
    paths.set_repo_root(repo)
    targets = load().game(GAME).targets

    result = PROVIDER.observe(_source(repo))

    heads = [obs for obs in result.observations if obs.kind == "game"]
    assert [obs.branch for obs in heads] == ["public"], "the disabled experimental channel is never observed"
    # No branch-review observation either: Steam lists only `public` for this app.
    assert [obs.kind for obs in result.observations] == ["game"]
    assert heads[0].facts["buildid"] == 23178631
    assert heads[0].facts["depots"][GAME_DEPOT]["manifest"] == PINNED[GAME_DEPOT]
    assert PROVIDER.covers(heads[0], targets) is True, "the pinned head needs no issue"

    # Steam moves the branch: a new build id and new manifests on the same branch.
    source_doc = _served(root)
    fake_steamcmd.move_head(source_doc, "public", 23999999, {GAME_DEPOT: MOVED[GAME_DEPOT]})
    fake_steamcmd.set_manifest(source_doc, REDIST_DEPOT, "public", MOVED[REDIST_DEPOT])
    fake_steamcmd.serve(root, APP, source_doc)

    result = PROVIDER.observe(_source(repo))
    head = [obs for obs in result.observations if obs.kind == "game"][0]

    assert PROVIDER.covers(head, targets) is False, "a moved head is not covered by the pin"
    issue = PROVIDER.presentation(head, "Enshrouded")
    assert issue is not None
    assert issue["title"] == "Enshrouded public: build 23999999 needs a target"
    # `watch.readinessNote` is what the issue carries for a build moving under the pinned
    # code signatures; the generic sentence would be wrong here.
    note = json.loads((repo / "catalog" / GAME / "game.json").read_text())["sources"]["steam"]["watch"]["readinessNote"]
    assert head.facts["readinessNote"] == note
    assert issue["readinessLines"] == [note]
    assert "no framework layer" not in " ".join(issue["readinessLines"])


def _served(root: Path) -> dict[str, Any]:
    from takaro_maint.steam import vdf

    return vdf.parse((root / f"{APP}.vdf").read_text(encoding="utf-8"))[str(APP)]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- 4-8 install


def install(run: Any, repo: Path, dest: Path, *extra: str) -> tuple[int, Any, str]:
    return run("install", "--game", GAME, "--target", TARGET, "--dest", str(dest), *extra, repo=repo)


def tree_hash(root: Path) -> str:
    from takaro_maint.commands.install import tree_hash as _tree_hash

    return _tree_hash(root)  # type: ignore[no-any-return]


def test_install_pins_both_depots_and_prepares_the_pinned_layout(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "server"

    code, payload, err = install(run, repo, dest)

    assert code == 0, f"{err}\n{payload}"
    assert payload["status"] == "installed"
    calls = fake_dd.argv_log(dd_log)
    for depot, manifest in PINNED.items():
        matching = [argv for argv in calls if "-depot" in argv and argv[argv.index("-depot") + 1] == depot]
        assert len(matching) == 1, f"depot {depot} was fetched {len(matching)} time(s)"
        argv = matching[0]
        assert argv[argv.index("-manifest") + 1] == manifest
        assert argv[argv.index("-os") + 1] == "windows"
        assert argv[argv.index("-osarch") + 1] == "64"
        assert "-validate" in argv
    # A branch without a manifest is how a "pinned" install silently becomes the head.
    assert all("-manifest" in argv for argv in calls if "-branch" in argv)

    assert (dest / "steamapps" / "compatdata" / str(APP)).is_dir()
    assert (dest / "takaro" / "plugin").is_dir()
    assert (dest / "takaro" / "sidecar").is_dir()
    assert "never runs SteamCMD" in (dest / "takaro" / "PINNED.txt").read_text()
    # Both depots' contents really landed in one tree.
    assert (dest / "enshrouded_server.exe").is_file()
    assert (dest / "steamclient64.dll").is_file()

    ledger = json.loads((dest / ".takaro" / "installed-target.json").read_text())
    declared = read_target(repo)["inputs"]["server"]["files"]
    assert {row["path"] for row in ledger["inputs"]} == set(declared)
    assert all(row["sha256"] == declared[row["path"]]["sha256"] for row in ledger["inputs"])

    before = len(fake_dd.argv_log(dd_log))
    code, payload, _ = install(run, repo, dest)
    assert code == 0
    assert payload["status"] == "already-installed"
    assert len(fake_dd.argv_log(dd_log)) == before, "a satisfied install calls no tool at all"


def test_an_unavailable_manifest_exits_four_with_no_fallback(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DD_UNAVAILABLE", PINNED[GAME_DEPOT])
    dest = tmp_path / "server"

    code, payload, _ = install(run, repo, dest)

    assert code == 4, payload
    assert "not falling back to branch head" in payload["error"]
    assert not dest.exists()
    assert not list(tmp_path.glob("server.staging-*"))


def test_a_wrong_declared_hash_exits_five_and_leaves_the_install_untouched(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "server"
    assert install(run, repo, dest)[0] == 0
    before = tree_hash(dest)

    record = read_target(repo)
    record["inputs"]["server"]["files"]["enshrouded_server.exe"]["sha256"] = "9" * 64
    record["inputs"]["server"]["buildid"] = 23178632  # a different fingerprint, so it reinstalls
    write_target(repo, record)

    code, payload, _ = install(run, repo, dest)

    assert code == 5, payload
    assert tree_hash(dest) == before, "the install that was in service is byte-identical"
    assert not list(tmp_path.glob("server.staging-*"))


def test_a_stale_depot_cache_is_rehashed_and_refetched_once(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint import paths

    dest = tmp_path / "server"
    assert install(run, repo, dest)[0] == 0
    cached = paths.cache_dir() / "steam" / str(APP) / GAME_DEPOT / PINNED[GAME_DEPOT] / "enshrouded_server.exe"
    good = cached.read_bytes()
    cached.write_bytes(b"someone edited the cache")
    shutil.rmtree(dest)
    before = len(fake_dd.argv_log(dd_log))

    code, payload, err = install(run, repo, dest)

    assert code == 0, f"{err}\n{payload}"
    calls = fake_dd.argv_log(dd_log)[before:]
    refetched = [argv for argv in calls if "-depot" in argv and argv[argv.index("-depot") + 1] == GAME_DEPOT]
    assert len(refetched) == 1, "a corrupt cache costs exactly one re-download"
    assert cached.read_bytes() == good

    # Corruption the tool itself serves is a disagreement with the record, not a cache miss.
    shutil.rmtree(dest)
    shutil.rmtree(cached.parent)
    monkeypatch.setenv("FAKE_DD_CORRUPT", "enshrouded_server.exe")

    code, payload, _ = install(run, repo, dest)

    assert code == 5, payload
    assert not dest.exists()


def test_preserve_keeps_config_saves_and_plugin_state_across_an_upgrade_and_rollback_restores(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "server"
    assert install(run, repo, dest)[0] == 0
    kept = {
        "enshrouded_server.json": '{"name":"mine"}',
        "savegame/3ad85aea": "a world",
        "takaro/plugin/dbghelp.dll": "the deployed plugin",
        "takaro/plugin.json": '{"token":"x"}',
        f"steamapps/compatdata/{APP}/pfx/marker": "a wine prefix",
    }
    for relative, text in kept.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    repin(repo, MOVED)  # the same target id, re-pinned at another build's bytes
    code, payload, err = install(run, repo, dest)

    assert code == 0, f"{err}\n{payload}"
    assert sorted(payload["preserved"]) == sorted(
        ["enshrouded_server.json", "savegame", "logs", "backups", "steamapps/compatdata", "takaro", ".takaro"]
    )
    for relative, text in kept.items():
        assert (dest / relative).read_text() == text, relative
    previous = dest.with_name(dest.name + ".previous")
    assert previous.is_dir()

    repin(repo, PINNED)
    code, payload, err = run(
        "install", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--rollback", repo=repo
    )

    assert code == 0, f"{err}\n{payload}"
    assert payload["status"] == "rolled-back"
    for relative, text in kept.items():
        assert (dest / relative).read_text() == text, relative

    shutil.rmtree(dest.with_name(dest.name + ".previous"), ignore_errors=True)
    code, payload, _ = run("install", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--rollback", repo=repo)
    assert code == 7, payload


# --------------------------------------------------------------------------- 9-10 build, deploy


def write_build_stub(repo: Path, fingerprint: str, *, produces: tuple[str, ...] = (PLUGIN_ZIP, SIDECAR_ZIP)) -> None:
    """A stand-in for the real release script: the same contract, no zig and no Node."""
    script = repo / BUILD_SCRIPT
    body = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'version="$1"; out="$2"',
        'mkdir -p "$out"',
    ]
    for name in produces:
        folder = "TakaroEnshrouded" if "plugin" in name else "TakaroEnshroudedSidecar"
        body += [
            f'stage="$out/stage-{folder}"',
            f'mkdir -p "$stage/{folder}"',
            (
                f"printf 'the dll\\n' > \"$stage/{folder}/dbghelp.dll\""
                if folder == "TakaroEnshrouded"
                else f"mkdir -p \"$stage/{folder}/dist\" && printf 'console.log(1)\\n' > "
                f'"$stage/{folder}/dist/index.js" && printf \'FROM scratch\\n\' > "$stage/{folder}/Dockerfile"'
            ),
            (
                f'printf \'Takaro Enshrouded Plugin %s\\n\' "$version" > "$stage/{folder}/README.txt"'
                if folder == "TakaroEnshrouded"
                else ":"
            ),
            f"( cd \"$stage\" && python3 -c \"import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', '.', "
            f'\'{folder}\')" "$out/{name[:-4]}" )',
            f'cat > "$out/{name}.meta.json" <<JSON',
            f'{{"target": "{TARGET}", "fingerprint": "{fingerprint}", "connectorVersion": "$version", '
            f'"game": "enshrouded", "platform": "proton", "revision": "1024233"}}',
            "JSON",
        ]
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(0o755)


def test_build_produces_both_roles_by_exact_name(run: Any, repo: Path, tmp_path: Path) -> None:
    resolved = resolve(run, repo)
    write_build_stub(repo, resolved["fingerprint"])
    out = tmp_path / "dist"

    code, payload, err = run(
        "build", "--game", GAME, "--target", TARGET, "--version", VERSION, "--out", str(out), repo=repo
    )

    assert code == 0, f"{err}\n{payload}"
    assert sorted(row["role"] for row in payload["artifacts"]) == ["server-plugin", "sidecar"]
    assert sorted(row["file"] for row in payload["artifacts"]) == sorted([PLUGIN_ZIP, SIDECAR_ZIP])
    for name in (PLUGIN_ZIP, SIDECAR_ZIP):
        assert (out / name).is_file()
        assert (out / f"{name}.meta.json").is_file()

    # A build that produces one role is a broken release, not a partial success.
    write_build_stub(repo, resolved["fingerprint"], produces=(PLUGIN_ZIP,))
    code, payload, _ = run(
        "build", "--game", GAME, "--target", TARGET, "--version", VERSION, "--out", str(tmp_path / "half"), repo=repo
    )
    assert code == 7, payload
    assert "did not produce" in payload["error"]

    # Nor is one that falls back to the legacy, target-less name.
    write_build_stub(repo, resolved["fingerprint"], produces=("takaro-enshrouded-plugin.zip", SIDECAR_ZIP))
    code, payload, _ = run(
        "build", "--game", GAME, "--target", TARGET, "--version", VERSION, "--out", str(tmp_path / "legacy"), repo=repo
    )
    assert code == 7, payload


def _plugin_zip(path: Path, *, version: str = VERSION, escape: bool = False, no_dll: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if not no_dll:
            archive.writestr("TakaroEnshrouded/dbghelp.dll", "the dll")
        archive.writestr("TakaroEnshrouded/README.txt", f"Takaro Enshrouded Plugin {version}\n")
        if escape:
            archive.writestr("../escaped.txt", "nope")


def _sidecar_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("TakaroEnshroudedSidecar/dist/index.js", "console.log(1)\n")
        archive.writestr("TakaroEnshroudedSidecar/Dockerfile", "FROM scratch\n")
        archive.writestr("TakaroEnshroudedSidecar/package.json", "{}\n")
        # The real zip ships these two, and a docker build from the folder needs them.
        archive.writestr("TakaroEnshroudedSidecar/.dockerignore", "node_modules\n")
        archive.writestr("TakaroEnshroudedSidecar/.env.example", "TAKARO_WS_URL=\n")


def _manifest_for(run: Any, repo: Path, directory: Path, files: dict[str, Path]) -> Path:
    resolved = resolve(run, repo)
    rows = [artifact_row(role, TARGET, resolved["fingerprint"], path) for role, path in sorted(files.items())]
    for row in rows:
        write_meta(directory, row, connector=GAME, version=VERSION, revision="deadbeef")
    return write_manifest(
        directory,
        connector=GAME,
        version=VERSION,
        revision="deadbeef",
        dirty=False,
        toolchain=resolved["build"]["toolchain"],
        mode="container",
        artifacts=rows,
    )


def test_deploy_places_the_dll_and_the_sidecar_folder_and_refuses_escapes(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "server"
    assert install(run, repo, dest)[0] == 0
    stale = dest / "takaro" / "plugin" / f"takaro-enshrouded-plugin-{TARGET}-0.4.1.zip"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"an older deploy")
    directory = tmp_path / "dist"
    _plugin_zip(directory / PLUGIN_ZIP)
    _sidecar_zip(directory / SIDECAR_ZIP)
    manifest = _manifest_for(
        run, repo, directory, {"server-plugin": directory / PLUGIN_ZIP, "sidecar": directory / SIDECAR_ZIP}
    )

    code, payload, err = run(
        "deploy", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--from", str(manifest), repo=repo
    )

    assert code == 0, f"{err}\n{payload}"
    dll = dest / "takaro" / "plugin" / "dbghelp.dll"
    assert dll.read_bytes() == b"the dll"
    assert oct(dll.stat().st_mode)[-3:] == "644"
    assert not stale.exists()
    unpacked = dest / "takaro" / "sidecar" / "TakaroEnshroudedSidecar"
    assert (unpacked / "dist" / "index.js").is_file()
    assert (unpacked / "Dockerfile").is_file()
    # The dotfiles a `docker build` from this folder needs: they are ordinary zip entries,
    # not the record-supplied install paths whose first character has to be alphanumeric.
    assert (unpacked / ".dockerignore").is_file()
    assert (unpacked / ".env.example").is_file()
    # The unpack stages beside the live folder and swaps; no staging is left behind.
    assert not [p.name for p in unpacked.parent.iterdir() if p.name.startswith(".")]
    ledger = json.loads((dest / ".takaro" / "installed-target.json").read_text())
    by_role = {row["role"]: row["path"] for row in ledger["artifacts"]}
    assert by_role["server-plugin"].endswith(PLUGIN_ZIP)
    assert by_role["sidecar"].endswith(SIDECAR_ZIP)

    for broken in ({"escape": True}, {"no_dll": True}):
        shutil.rmtree(dest / "takaro" / "plugin")
        bad = tmp_path / f"bad-{'escape' if 'escape' in broken else 'nodll'}"
        _plugin_zip(bad / PLUGIN_ZIP, **broken)  # type: ignore[arg-type]
        _sidecar_zip(bad / SIDECAR_ZIP)
        bad_manifest = _manifest_for(run, repo, bad, {"server-plugin": bad / PLUGIN_ZIP, "sidecar": bad / SIDECAR_ZIP})

        code, payload, _ = run(
            "deploy", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--from", str(bad_manifest), repo=repo
        )

        assert code == 7, payload
        assert not (dest / "takaro" / "plugin" / "dbghelp.dll").exists()
        assert not (tmp_path / "escaped.txt").exists()


# --------------------------------------------------------------------------- 11-12 verify hooks


def _health(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_verify_hooks_classify_plugin_health() -> None:
    """The compatibility claim, in the one function both the check and the negative use."""
    ok = hooks.classify_health(_health("plugin-health-ok.json"), "1024233", "0.4.2")
    assert ok["ok"] is True, ok["problems"]
    assert ok["degraded"] == []

    # A capability the plugin itself downgraded: the server is alive, the claim is not.
    degraded = hooks.classify_health(_health("plugin-health-degraded.json"), "1024233", "0.4.2")
    assert degraded["ok"] is False
    assert degraded["degraded"] == ["teleport"]
    assert any("teleport" in problem for problem in degraded["problems"])

    overall = {**_health("plugin-health-ok.json"), "status": "degraded"}
    assert hooks.classify_health(overall, "1024233", "0.4.2")["ok"] is False

    moved = {**_health("plugin-health-ok.json"), "gameBuild": "1030000"}
    verdict = hooks.classify_health(moved, "1024233", "0.4.2")
    assert verdict["ok"] is False
    assert any("1024233" in problem and "1030000" in problem for problem in verdict["problems"])

    # `unimplemented` is a capability nobody claimed, not a broken one.
    partial = _health("plugin-health-ok.json")
    partial["capabilities"]["listLocations"] = "unimplemented"
    verdict = hooks.classify_health(partial, "1024233", "0.4.2")
    assert verdict["ok"] is True
    assert verdict["unimplemented"] == ["listLocations"]

    assert hooks.classify_health(_health("plugin-health-ok.json"), "1024233", "9.9.9")["ok"] is False
    assert hooks.classify_health("not a document", "1024233")["ok"] is False


def test_the_plugin_is_held_to_the_release_part_of_the_built_version(tmp_path: Path) -> None:
    """A dev build of 0.4.2 still stamps 0.4.2 into the DLL, so that is what is compared."""

    class Options:
        artifacts = tmp_path

    class Run:
        options = Options()

    assert hooks.connector_version(Run()) is None, "no manifest, nothing to hold the plugin to"

    (tmp_path / "build-manifest.json").write_text(json.dumps({"version": "0.4.2-dev.abc1234"}), encoding="utf-8")
    assert hooks.connector_version(Run()) == "0.4.2"

    (tmp_path / "build-manifest.json").write_text(json.dumps({"version": "0.4.3"}), encoding="utf-8")
    assert hooks.connector_version(Run()) == "0.4.3"

    # An older DLL left behind by an earlier deploy is still caught.
    stale = hooks.classify_health(_health("plugin-health-ok.json"), "1024233", "0.4.3")
    assert stale["ok"] is False
    assert any("0.4.2" in problem for problem in stale["problems"])


def test_a_health_document_that_is_not_shaped_like_one_fails_rather_than_raises() -> None:
    """A malformed answer is a failed compatibility claim, not a crashed verification run."""
    for broken in ({"status": "ok", "gameBuild": "1024233", "capabilities": []}, "not a document", None):
        verdict = hooks.classify_health(broken, "1024233")
        assert verdict["ok"] is False, broken
        assert verdict["problems"], broken

    # No capabilities at all is a failure too: nothing was self-checked.
    empty = hooks.classify_health({"status": "ok", "gameBuild": "1024233", "capabilities": {}}, "1024233")
    assert empty["ok"] is False
    assert any("no capabilities" in problem for problem in empty["problems"])


def test_verify_hooks_prepare_the_run_and_match_the_recorded_lines(tmp_path: Path) -> None:
    from takaro_maint import paths
    from takaro_maint.exit_codes import ConflictError
    from takaro_maint.games.enshrouded import plugin_token
    from takaro_maint.verify.runner import RunOptions, check_ids, game_hooks

    class Target:
        record = _record()

    class Run:
        data_dir = tmp_path
        target = Target()

        def __init__(self, only: list[str] | None = None) -> None:
            self.options = RunOptions(artifacts=tmp_path, out=tmp_path, run_id="test", only=only)

    takaro_env = {"TAKARO_REGISTRATION_TOKEN": "a-throwaway-registration-token"}
    run = Run()
    written = hooks.before_boot(run, takaro_env)

    # A bare `verify --game enshrouded` runs this target's own checks and nothing else: the
    # base protocol ladder watches the game container for a connector that is in the sidecar.
    # The runner narrows the selection; these hooks owe the declaration.
    declared = game_hooks("enshrouded")
    assert set(declared.unsupported_checks) == {
        "connector-load",
        "identify",
        "heartbeat",
        "players",
        "catalog-items",
        "catalog-entities",
        "console",
        "shutdown",
    }
    selected = [check for check in check_ids("enshrouded") if check not in declared.unsupported_checks]
    # The record and the hooks cannot drift: what a bare run selects is what the target says
    # it verifies separately, plus the two rows every game climbs.
    assert selected == ["build", "startup", *hooks.CHECK_IDS]
    assert selected == ["build", *Target.record["verification"]["separate"]]

    assert written == tmp_path / "takaro" / "plugin.json"
    assert oct(written.stat().st_mode)[-3:] == "600"
    token = json.loads(written.read_text())["token"]
    assert token == plugin_token(takaro_env), "every boot of one run derives the same token"
    assert token != takaro_env["TAKARO_REGISTRATION_TOKEN"]

    boot = (FIXTURES / "docker-log-boot.txt").read_text()
    assert hooks.READY_LINE.search(boot)
    assert hooks.SHUTDOWN_LINE.search(boot)
    assert hooks.SAVED_LINE.search(boot)
    assert hooks.RESPAWN_LINE.search(boot)
    build = hooks.BUILD_LINE.search(boot)
    assert build and build.group("build") == "1024233"
    # The drift guard: this boot ran no update path, and it is the absence that is checked.
    assert not hooks.DRIFT_LINE.search(boot)
    assert hooks.DRIFT_LINE.search("INFO - Enshrouded server needs to be updated")

    sidecar = (FIXTURES / "sidecar-log.txt").read_text()
    assert len(hooks.IDENTIFIED_LINE.findall(sidecar)) == 2
    assert hooks.CLOSED_LINE.search(sidecar)
    assert hooks.PLUGIN_LISTENING.search((FIXTURES / "plugin-log.txt").read_text())

    adapter = adapter_for(GAME)
    assert adapter.parse_runtime_identity(
        "2026-09-13T18:05:06.551Z [app] Enshrouded Server, Game Version (SVN): 1024233"
    ) == {"gameVersion": "1024233", "loader": "proton", "loaderVersion": None}
    assert adapter.parse_runtime_identity("nothing to see here") is None

    paths.set_repo_root(REPO_ROOT)
    resolved = {"containerRef": "example/image:1.0@sha256:" + "a" * 64}
    data = tmp_path / "data"
    (data / "takaro" / "plugin").mkdir(parents=True)
    with pytest.raises(ConflictError):
        adapter.container_mounts(resolved, data)
    (data / "takaro" / "plugin" / "dbghelp.dll").write_bytes(b"x")
    mounts = adapter.container_mounts(resolved, data)
    assert mounts == [
        f"{data}:/opt/enshrouded/server",
        f"{data}/takaro/plugin/dbghelp.dll:/opt/enshrouded/server/dbghelp.dll:ro",
        f"{REPO_ROOT}/{UPDATER}:/usr/local/etc/enshrouded/enshrouded-updater:ro",
    ]

    assert set(hooks.CHECK_IDS) <= set(check_ids(GAME))


# --------------------------------------------------------------------------- 13-14 selectors


def _record() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "catalog" / GAME / "targets" / f"{TARGET}.json").read_text(encoding="utf-8"))


def test_every_container_selector_is_pinned_and_never_schedules_updates() -> None:
    record = _record()
    image = ids.container_ref(record["runtime"]["container"])
    sidecar_runtime = record["build"]["deps"]["sidecar-runtime"]["resolvedCoordinate"]
    toolchain = ids.container_ref(record["build"]["toolchain"])
    forbidden = re.compile(r"UPDATE_CRON|RESTART_CRON|GAME_BRANCH|STEAMCMD_ARGS|:latest")

    def settings(text: str) -> str:
        """The file without its comments: naming what is absent is what the comments are for."""
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    def dll_mount(source: str) -> str:
        """The plugin bind, long syntax: compose must refuse a missing DLL, not create one.

        A short-syntax bind lets the docker daemon create the source, so a compose up on a
        tree `takaro-maint install`/`deploy` has not laid down yet makes a DIRECTORY named
        dbghelp.dll and the server boots with no plugin loaded.
        """
        return (
            "      - type: bind\n"
            f"        source: {source}\n"
            "        target: /opt/enshrouded/server/dbghelp.dll\n"
            "        read_only: true\n"
            "        bind:\n"
            "          create_host_path: false\n"
        )

    for relative, mounts in (
        (
            "dev-servers/compose/enshrouded.yml",
            (
                dll_mount("../_data/enshrouded/server/takaro/plugin/dbghelp.dll"),
                "../../games/enshrouded/server/enshrouded-updater:/usr/local/etc/enshrouded/enshrouded-updater:ro",
            ),
        ),
        (
            "games/enshrouded/docker-compose.example.yml",
            (
                dll_mount("./data/enshrouded/server/takaro/plugin/dbghelp.dll"),
                "./server/enshrouded-updater:/usr/local/etc/enshrouded/enshrouded-updater:ro",
            ),
        ),
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert image in text, f"{relative} does not name the catalog's image"
        for mount in mounts:
            assert mount in text, f"{relative} is missing the mount {mount}"
        found = forbidden.search(settings(text))
        assert not found, f"{relative} can still update the game underneath the pinned hooks: {found}"

    # The rig reads the resolved target when it has one and the pinned image when it does not.
    rig = (REPO_ROOT / "dev-servers/compose/enshrouded.yml").read_text(encoding="utf-8")
    assert f'"${{ENSHROUDED_IMAGE:-{image}}}"' in rig

    # No bind on the install tree may create its own source: an empty tree is not an install.
    for relative, sources in (
        ("dev-servers/compose/enshrouded.yml", ("../_data/enshrouded/server",)),
        ("games/enshrouded/docker-compose.example.yml", ("./data/enshrouded/server",)),
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for source in sources:
            blocks = [b for b in text.split("- type: bind") if f"source: {source}\n" in b]
            assert blocks, f"{relative} does not bind {source} in long syntax"
            for block in blocks:
                assert "create_host_path: false" in block, f"{relative}: {source} may create its own source"

    for relative in ("games/enshrouded/sidecar/Dockerfile", "games/enshrouded/sidecar/Dockerfile.release"):
        froms = [line.split()[1] for line in (REPO_ROOT / relative).read_text().splitlines() if line.startswith("FROM")]
        assert froms and all(base == sidecar_runtime for base in froms), f"{relative}: {froms}"

    builder = (REPO_ROOT / "games/enshrouded/Dockerfile.builder").read_text(encoding="utf-8")
    assert "ARG TOOLCHAIN\nFROM ${TOOLCHAIN}\n" in builder
    assert toolchain not in builder
    build_script = (REPO_ROOT / "games/enshrouded/scripts/lib-target.sh").read_text(encoding="utf-8")
    assert '--build-arg "TOOLCHAIN=${ENSHROUDED_TOOLCHAIN:?resolve the target first}"' in build_script
    zig = record["build"]["deps"]["zig"]
    assert f"ARG ZIG_URL={zig['resolvedCoordinate']}\n" in builder
    assert f"ARG ZIG_SHA256={zig['sha256']}\n" in builder

    assert "gcc:14@sha256:" in (REPO_ROOT / "games/enshrouded/mod/tests/run.sh").read_text(encoding="utf-8")

    workflow = (REPO_ROOT / ".github/workflows/enshrouded.yml").read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/connector-release.yml" in workflow
    assert "runtime: false" in workflow
    assert "ziglang.org" not in workflow, "the release pipeline installs no toolchain of its own"


def test_the_updater_override_never_calls_steamcmd(tmp_path: Path) -> None:
    """The one file that stands between a pinned install and a boot-time game update."""
    override = REPO_ROOT / UPDATER
    body = override.read_text(encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(override)], capture_output=True).returncode == 0
    executable = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    assert not re.search(r"steamcmd|app_update|api\.steamcmd\.net|curl", "\n".join(executable))

    # Run it the way supervisord does, against stubs for what the image supplies.
    home = tmp_path / "etc"
    home.mkdir()
    shutil.copy2(override, home / "enshrouded-updater")
    (home / "enshrouded-updater").chmod(0o755)
    install_path = tmp_path / "server"
    install_path.mkdir()
    (home / "common").write_text(
        "\n".join(
            [
                f"install_path={install_path}",
                f"steam_app_id={APP}",
                'info() { echo "INFO - $*"; }',
                'checkRunning() { status=$(supervisorctl status "$1"); [[ "$status" == *RUNNING* ]]; }',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "defaults").write_text("SERVER_NAME=${SERVER_NAME:-stub}\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "supervisorctl-calls.txt"

    def run_override(state: str) -> str:
        (bin_dir / "supervisorctl").write_text(
            f'#!/usr/bin/env bash\necho "$@" >> {calls}\n[ "$1" = status ] && echo "$2 {state}"\nexit 0\n',
            encoding="utf-8",
        )
        (bin_dir / "supervisorctl").chmod(0o755)
        calls.write_text("", encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(home / "enshrouded-updater")],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return calls.read_text(encoding="utf-8")

    stopped = run_override("STOPPED")
    assert stopped.count("start enshrouded-server") == 1
    assert (install_path / "steamapps" / "compatdata" / str(APP)).is_dir()

    running = run_override("RUNNING")
    assert "start enshrouded-server" not in running


# --------------------------------------------------------------------------- 15 the release record


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env).stdout.strip()


def test_compat_record_carries_both_roles_and_the_steam_pin(run: Any, repo: Path, tmp_path: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    resolved = resolve(run, repo)
    directory = tmp_path / "dist" / TARGET
    _plugin_zip(directory / PLUGIN_ZIP)
    _sidecar_zip(directory / SIDECAR_ZIP)
    rows = [
        artifact_row("server-plugin", TARGET, resolved["fingerprint"], directory / PLUGIN_ZIP),
        artifact_row("sidecar", TARGET, resolved["fingerprint"], directory / SIDECAR_ZIP),
    ]
    for row in rows:
        write_meta(directory, row, connector=GAME, version=VERSION, revision=commit)
    write_manifest(
        directory,
        connector=GAME,
        version=VERSION,
        revision=commit,
        dirty=False,
        toolchain=resolved["build"]["toolchain"],
        mode="container",
        artifacts=rows,
    )
    out = tmp_path / "assembled"

    code, payload, err = run(
        "release",
        "assemble",
        "--connector",
        GAME,
        "--version",
        VERSION,
        "--channel",
        "pr",
        "--tag",
        f"pr-1-{GAME}",
        "--dist",
        str(tmp_path / "dist"),
        "--out",
        str(out),
        "--repo",
        "o/r",
        repo=repo,
    )

    assert code == 0, f"{err}\n{payload}"
    record = json.loads((out / f"takaro-{GAME}-{VERSION}.compat.json").read_text())
    entry = record["targets"][TARGET]
    assert sorted(row["role"] for row in entry["artifacts"]) == ["server-plugin", "sidecar"]
    assert sorted(row["name"] for row in entry["artifacts"]) == sorted([PLUGIN_ZIP, SIDECAR_ZIP])
    assert entry["verification"] == {
        "required": "contract",
        "executed": None,
        "report": None,
        "outcome": None,
        "takaro": None,
    }
    url = entry["inputs"]["server"]["url"]
    assert url.startswith(f"steam://app/{APP}/branch/public/build/23178631/")
    assert "1004/manifest/7604377918839582995" in url
    assert "2278521/manifest/2174935030716737236" in url

    # Both legacy names still resolve, to exactly the bytes their target-named files carry.
    assert (out / "takaro-enshrouded-plugin.zip").read_bytes() == (out / PLUGIN_ZIP).read_bytes()
    assert (out / "takaro-enshrouded-sidecar.zip").read_bytes() == (out / SIDECAR_ZIP).read_bytes()
    assert (out / "SHA256SUMS").is_file()


# ------------------------------------------------------- the catalogue check, directly


class _CatalogFake:
    """Only what `_check_catalog` reaches for: one canned answer per action."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers

    async def request(self, action: str, params: Any, timeout: float | None = None) -> Any:
        del params, timeout
        return self.answers[action]


def _catalog(answers: dict[str, Any]) -> Any:
    import asyncio

    return asyncio.run(hooks._check_catalog(None, _CatalogFake(answers)))


def _derived_answers() -> dict[str, Any]:
    """What the mod answers: every name derived from its template code by names.cpp."""
    return {
        "listItems": [
            {"code": "Block_T3_Stone_CityWall_REWARD", "name": "Stone City Wall Reward (Tier 3)"},
            {"code": "Food_T7_raw_fruit_Artichoke", "name": "Raw Fruit Artichoke (Tier 7)"},
            {"code": "Weapon_T4_2H_GreatSwordEpic_01", "name": "2H Great Sword Epic (Tier 4)"},
            {"code": "Z_Prop_NPC_Cat_Totem_DEPRECATED", "name": "NPC Cat Totem"},
        ],
        "listEntities": [
            {"code": "Enemy_Skeleton_Heavy", "name": "Skeleton Heavy"},
            {"code": "Animal_Baby_T1_Goat", "name": "Baby Goat (Tier 1)"},
            {"code": "1_Player_AG2", "name": "Player"},
        ],
        "listLocations": [
            {"code": "8kMapLabel_deepforest_Town_07_Whitewind", "name": "Whitewind"},
            {"code": "Prop_OpenWorld_SavePoint", "name": "Open World Save Point"},
        ],
    }


def test_the_catalog_check_passes_on_derived_display_names() -> None:
    result = _catalog(_derived_answers())

    assert result.status == "pass", result.detail["problems"]
    assert result.detail["counts"] == {"listItems": 4, "listEntities": 3, "listLocations": 2}


def test_the_catalog_check_fails_on_a_formatted_dev_code() -> None:
    """A name that is the code with its underscores opened is refused, not reported as a name."""
    answers = _derived_answers()
    answers["listEntities"] = [
        {"code": "Enemy_Skeleton_Heavy", "name": "Skeleton Heavy"},
        {"code": "1_Player_AG2", "name": "1 Player AG2"},
    ]

    result = _catalog(answers)

    assert result.status == "fail"
    problems = " ".join(result.detail["problems"])
    assert "1 of 2" in problems
    assert "1_Player_AG2 -> 1 Player AG2" in problems


@pytest.mark.parametrize(
    "name",
    [
        "Cat Black AG2",
        "Baby T1 Goat",
        "Placement Helper Pet Cat",
        "Skeleton_Heavy",
        "8k Map Label deepforest Whitewind",
        "01 Huntress Camp",
        "Arrow Bone UNUSED",
    ],
)
def test_every_predicate_the_corpus_test_asserts_is_asserted_on_the_live_answer(name: str) -> None:
    """The C++ test proves the shipped table; this proves the server did not regress past it."""
    answers = _derived_answers()
    answers["listLocations"] = [{"code": "Some_Template_Code", "name": name}]

    result = _catalog(answers)

    assert result.status == "fail", name
    assert "listLocations" in " ".join(result.detail["problems"])


def test_an_item_name_that_is_still_a_dev_code_is_named() -> None:
    answers = _derived_answers()
    answers["listItems"] = [{"code": "Sword_Bronze", "name": "Sword_Bronze"}]

    result = _catalog(answers)

    assert result.status == "fail"
    problems = " ".join(result.detail["problems"])
    assert "names are the code itself" in problems


def test_an_item_name_that_still_carries_an_underscore_is_refused() -> None:
    """The same predicate the C++ corpus test applies, applied to the live item answer."""
    answers = _derived_answers()
    answers["listItems"] = [{"code": "Block_T3_Stone_CityWall", "name": "Stone_City_Wall"}]

    result = _catalog(answers)

    assert result.status == "fail"
    assert "listItems" in " ".join(result.detail["problems"])


def test_an_item_name_that_is_the_opened_code_is_refused() -> None:
    answers = _derived_answers()
    answers["listItems"] = [{"code": "Prop_Decoration_T5_Cupboard_Large", "name": "Prop Decoration T5 Cupboard Large"}]

    result = _catalog(answers)

    assert result.status == "fail"
    problems = " ".join(result.detail["problems"])
    assert "dev codes rather than display names" in problems


@pytest.mark.parametrize("word", ["UNUSED", "hasbugs", "LVLXX", "TEST"])
def test_the_item_noise_words_the_plugin_drops_are_refused_in_the_answer(word: str) -> None:
    answers = _derived_answers()
    answers["listItems"] = [{"code": "Ammo_T5_Arrow_Bone", "name": f"Arrow Bone {word}"}]

    result = _catalog(answers)

    assert result.status == "fail", word
    assert "listItems" in " ".join(result.detail["problems"])


@pytest.mark.parametrize("action", ["listItems", "listEntities", "listLocations"])
def test_one_name_held_by_two_codes_fails_for_every_action(action: str) -> None:
    """DistinctNames promises one name per code; a collision means that promise broke."""
    answers = _derived_answers()
    answers[action] = [
        {"code": "Enemy_Scavenger_Melee01_Night_Patrol_Guard", "name": "Scavenger Melee Night Patrol Guard"},
        {"code": "Enemy_Scavenger_Melee02_Night_Patrol_Guard", "name": "Scavenger Melee Night Patrol Guard"},
    ]

    result = _catalog(answers)

    assert result.status == "fail"
    problems = " ".join(result.detail["problems"])
    assert f"{action}: 1 names are shared by more than one code" in problems
    assert "Scavenger Melee Night Patrol Guard <- Enemy_Scavenger_Melee01_Night_Patrol_Guard" in problems


def test_two_rows_carrying_the_same_code_may_share_one_name() -> None:
    """The entity table holds 979 rows for 977 codes; the duplicates are not a collision."""
    answers = _derived_answers()
    answers["listEntities"] = [
        {"code": "Enemy_Skeleton_Heavy", "name": "Skeleton Heavy"},
        {"code": "Enemy_Skeleton_Heavy", "name": "Skeleton Heavy"},
    ]

    result = _catalog(answers)

    assert result.status == "pass", result.detail["problems"]
