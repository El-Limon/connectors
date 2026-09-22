"""The 7 Days to Die target end to end: catalog, resolution, pin, references, build, deploy.

Everything here runs the real command against the DepotDownloader stand-in and a stub
build script, so the assertions are about what a maintainer, the rig and CI observe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

import fake_depotdownloader as fake
from takaro_maint.games import adapter_for
from takaro_maint.games.seven_days import verify as hooks
from takaro_maint.publish.manifest import artifact_row, write_manifest, write_meta

TARGET = fake.TARGET_ID
GAME = "7d2d"
VERSION = "0.1.6-dev.abc1234"
ZIP_NAME = f"takaro-7d2d-mod-{TARGET}-{VERSION}.zip"
MANAGED = "7DaysToDieServer_Data/Managed"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return fake.make_repo(tmp_path)


@pytest.fixture
def dd_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "depotdownloader-argv.jsonl"
    for key, value in fake.environment(tmp_path, log).items():
        monkeypatch.setenv(key, value)
    return log


def resolve(run: Any, repo: Path) -> dict[str, Any]:
    code, payload, err = run("targets", "resolve", "--game", GAME, "--target", TARGET, repo=repo)
    assert code == 0, err
    return dict(payload)


# -- catalog -----------------------------------------------------------------------------


def test_catalog_validate_accepts_the_7d2d_target(run: Any) -> None:
    code, payload, _ = run("catalog", "validate")

    assert code == 0, payload
    rows = [check for check in payload["checks"] if check["file"].endswith(f"{TARGET}.json")]
    assert {check["id"] for check in rows} >= {"input-kind-schema", "build-system-schema", "build-script-exists"}
    assert all(check["status"] == "pass" for check in rows), rows


def test_targets_resolve_env_for_7d2d(run: Any) -> None:
    code, payload, _ = run("targets", "resolve", "--game", GAME, "--target", TARGET, "--prefix", "SEVEND2D")

    assert code == 0, payload
    env = payload["env"]
    assert env["SEVEND2D_STEAM_DEPOTS"] == "294422:1633674551820196085"
    assert env["SEVEND2D_STEAM_APP"] == "294420"
    assert env["SEVEND2D_STEAM_BRANCH"] == "public"
    assert env["SEVEND2D_ARTIFACT"] == "takaro-7d2d-mod-linux-3.2.0.b10-{version}.zip"
    assert env["SEVEND2D_REFERENCES_DIR"].endswith(payload["fp16"])
    assert not any(key.endswith("_JAVA") for key in env)
    assert payload["resolvedUrls"]["server"].startswith("steam://app/294420/branch/public/")


# -- steam pin ---------------------------------------------------------------------------


def test_steam_pin_reports_the_head_and_flags_a_changed_manifest(run: Any, repo: Path, dd_log: Path) -> None:
    code, payload, _ = run("steam", "pin", "--game", GAME, "--target", TARGET, repo=repo)

    assert code == 0, payload
    assert payload["changed"] == [fake.DEPOT]
    assert payload["depots"][fake.DEPOT]["manifest"] == fake.HEAD_MANIFEST
    assert payload["snippet"]["depots"][fake.DEPOT]["manifest"] == fake.HEAD_MANIFEST
    assert "-manifest-only" in fake.argv_log(dd_log)[0]


def test_steam_pin_refuses_to_write_hashes_it_has_not_recorded(run: Any, repo: Path, dd_log: Path) -> None:
    code, payload, _ = run("steam", "pin", "--game", GAME, "--target", TARGET, "--write", repo=repo)

    assert code == 7, payload
    assert "--record-files" in payload["error"]
    assert fake.read_target(repo)["inputs"]["server"]["depots"][fake.DEPOT]["manifest"] == fake.PINNED_MANIFEST


def test_steam_pin_writes_only_the_input_it_re_pinned(run: Any, repo: Path, dd_log: Path) -> None:
    before = fake.read_target(repo)
    declared = list(before["inputs"]["server"]["files"])
    record_args: list[str] = []
    for path in declared:
        record_args += ["--record-files", path]

    code, payload, _ = run(
        "steam", "pin", "--game", GAME, "--target", TARGET, "--buildid", "25000001", "--write", *record_args, repo=repo
    )

    assert code == 0, payload
    after = fake.read_target(repo)
    assert after["inputs"]["server"]["depots"][fake.DEPOT]["manifest"] == fake.HEAD_MANIFEST
    assert after["inputs"]["server"]["buildid"] == 25000001
    assert (
        after["inputs"]["server"]["files"][f"{MANAGED}/Assembly-CSharp.dll"]["sha256"]
        != (before["inputs"]["server"]["files"][f"{MANAGED}/Assembly-CSharp.dll"]["sha256"])
    )
    assert {key: value for key, value in after.items() if key != "inputs"} == {
        key: value for key, value in before.items() if key != "inputs"
    }


def test_steam_pin_reports_an_unavailable_manifest_as_upstream(
    run: Any, repo: Path, dd_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DD_UNAVAILABLE", fake.HEAD_MANIFEST)

    code, payload, _ = run("steam", "pin", "--game", GAME, "--target", TARGET, repo=repo)

    assert code == 4, payload
    assert "not falling back to branch head" in payload["error"]


# -- steam references --------------------------------------------------------------------


def references(run: Any, repo: Path, dest: Path, *extra: str) -> tuple[int, Any, str]:
    return run("steam", "references", "--game", GAME, "--target", TARGET, "--dest", str(dest), *extra, repo=repo)


def test_steam_references_downloads_only_the_subset_and_records_it(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "7dtd-binaries" / "fp"

    code, payload, _ = references(run, repo, dest)

    assert code == 0, payload
    assert payload["status"] == "fetched"
    argv = fake.argv_log(dd_log)[0]
    assert "-filelist" in argv
    assert sorted(path.name for path in dest.iterdir() if path.is_file()) == [
        "0Harmony.dll",
        "Assembly-CSharp-firstpass.dll",
        "Assembly-CSharp.dll",
        "UnityEngine.dll",
    ]
    marker = json.loads((dest / ".takaro" / "references.json").read_text())
    assert marker["fingerprint"] == resolve(run, repo)["fingerprint"]
    assert {row["path"] for row in marker["files"]} == {
        "0Harmony.dll",
        "Assembly-CSharp-firstpass.dll",
        "Assembly-CSharp.dll",
        "UnityEngine.dll",
    }

    code, payload, _ = references(run, repo, dest)
    assert code == 0
    assert payload["status"] == "up-to-date"


def test_a_references_directory_for_another_fingerprint_is_refused(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "7dtd-binaries" / "fp"
    assert references(run, repo, dest)[0] == 0
    marker_path = dest / ".takaro" / "references.json"
    marker = json.loads(marker_path.read_text())
    marker["fingerprint"] = "9" * 64
    marker_path.write_text(json.dumps(marker))

    code, payload, _ = references(run, repo, dest)

    assert code == 7, payload
    assert "stale reference cache" in payload["error"]
    assert references(run, repo, dest, "--force")[0] == 0


def test_an_altered_reference_assembly_is_refused(run: Any, repo: Path, dd_log: Path, tmp_path: Path) -> None:
    dest = tmp_path / "7dtd-binaries" / "fp"
    assert references(run, repo, dest)[0] == 0
    (dest / "Assembly-CSharp.dll").write_bytes(b"someone rebuilt this by hand")

    code, payload, _ = references(run, repo, dest)

    assert code == 5, payload


# -- build and deploy --------------------------------------------------------------------


def write_build_stub(repo: Path, fingerprint: str, *, name: str = ZIP_NAME) -> None:
    """A stand-in for the real release script: the same contract, none of the Mono."""
    script = repo / fake.BUILD_SCRIPT
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'version="$1"; out="$2"\n'
        'mkdir -p "$out" "$out/stage/Takaro"\n'
        'printf \'<xml><Version value="%s" /></xml>\\n\' "$version" > "$out/stage/Takaro/ModInfo.xml"\n'
        "printf 'assembly\\n' > \"$out/stage/Takaro/Takaro.dll\"\n"
        '( cd "$out/stage" && python3 -c '
        "\"import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', '.', 'Takaro')\" "
        f'"$out/{name[:-4]}" )\n'
        f'cat > "$out/{name}.meta.json" <<JSON\n'
        f'{{"target": "{TARGET}", "fingerprint": "{fingerprint}", "connectorVersion": "$version", '
        f'"game": "7d2d", "platform": "linux", "revision": "3.2.0.b10"}}\n'
        "JSON\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_build_selects_the_exact_zip_name_and_meta(run: Any, repo: Path, tmp_path: Path) -> None:
    resolved = resolve(run, repo)
    write_build_stub(repo, resolved["fingerprint"])
    out = tmp_path / "dist"

    code, payload, err = run(
        "build", "--game", GAME, "--target", TARGET, "--version", VERSION, "--out", str(out), repo=repo
    )

    assert code == 0, f"{err}\n{payload}"
    assert [row["role"] for row in payload["artifacts"]] == ["server-mod"]
    assert payload["artifacts"][0]["file"] == ZIP_NAME
    assert (out / ZIP_NAME).is_file()
    assert (out / f"{ZIP_NAME}.meta.json").is_file()
    manifest = json.loads((out / "build-manifest.json").read_text())
    assert manifest["artifacts"][0]["fingerprint"] == resolved["fingerprint"]


def test_a_build_that_writes_the_legacy_name_is_refused(run: Any, repo: Path, tmp_path: Path) -> None:
    resolved = resolve(run, repo)
    write_build_stub(repo, resolved["fingerprint"], name="takaro-7d2d-mod.zip")

    code, payload, _ = run(
        "build", "--game", GAME, "--target", TARGET, "--version", VERSION, "--out", str(tmp_path / "dist"), repo=repo
    )

    assert code == 7, payload
    assert "did not produce" in payload["error"]


def _installed(run: Any, repo: Path, dest: Path) -> None:
    code, payload, err = run("install", "--game", GAME, "--target", TARGET, "--dest", str(dest), repo=repo)
    assert code == 0, f"{err}\n{payload}"


def _mod_zip(path: Path, *, version: str = VERSION, escape: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takaro/ModInfo.xml", f'<xml><Version value="{version}" /></xml>\n')
        archive.writestr("Takaro/Takaro.dll", "assembly")
        if escape:
            archive.writestr("../escaped.txt", "nope")


def _manifest_for(run: Any, repo: Path, directory: Path, zip_path: Path) -> Path:
    resolved = resolve(run, repo)
    row = artifact_row("server-mod", TARGET, resolved["fingerprint"], zip_path)
    write_meta(directory, row, connector=GAME, version=VERSION, revision="deadbeef")
    return write_manifest(
        directory,
        connector=GAME,
        version=VERSION,
        revision="deadbeef",
        dirty=False,
        toolchain=resolved["build"]["toolchain"],
        mode="container",
        artifacts=[row],
    )


def test_deploy_unpacks_the_mod_folder_and_removes_older_zips(
    run: Any, repo: Path, dd_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "ServerFiles"
    _installed(run, repo, dest)
    (dest / "Mods" / "takaro-7d2d-mod-linux-3.2.0.b10-0.1.5.zip").write_bytes(b"an older deploy")
    directory = tmp_path / "dist"
    _mod_zip(directory / ZIP_NAME)
    manifest = _manifest_for(run, repo, directory, directory / ZIP_NAME)

    code, payload, err = run(
        "deploy", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--from", str(manifest), repo=repo
    )

    assert code == 0, f"{err}\n{payload}"
    assert (dest / "Mods" / "Takaro" / "ModInfo.xml").is_file()
    assert VERSION in (dest / "Mods" / "Takaro" / "ModInfo.xml").read_text()
    assert not (dest / "Mods" / "takaro-7d2d-mod-linux-3.2.0.b10-0.1.5.zip").exists()
    ledger = json.loads((dest / ".takaro" / "installed-target.json").read_text())
    assert ledger["artifacts"][0]["path"] == f"Mods/{ZIP_NAME}"


def test_a_zip_that_escapes_the_mod_folder_is_refused(run: Any, repo: Path, dd_log: Path, tmp_path: Path) -> None:
    dest = tmp_path / "ServerFiles"
    _installed(run, repo, dest)
    directory = tmp_path / "dist"
    _mod_zip(directory / ZIP_NAME, escape=True)
    manifest = _manifest_for(run, repo, directory, directory / ZIP_NAME)

    code, payload, _ = run(
        "deploy", "--game", GAME, "--target", TARGET, "--dest", str(dest), "--from", str(manifest), repo=repo
    )

    assert code == 7, payload
    assert not (dest / "Mods" / "Takaro").exists()
    assert not (tmp_path / "escaped.txt").exists()


# -- verification hooks ------------------------------------------------------------------


def test_verify_hooks_render_config_and_use_the_7d2d_ready_line(tmp_path: Path) -> None:
    written = hooks.render_config(
        tmp_path,
        {
            "TAKARO_WS_URL": "ws://host.docker.internal:34567/",
            "TAKARO_IDENTITY_TOKEN": "takaro-verify-tm148-157",
            "TAKARO_REGISTRATION_TOKEN": "a-throwaway-registration-token",
        },
    )

    assert written == tmp_path / "Takaro" / "Config.xml"
    assert oct(written.stat().st_mode)[-3:] == "600"
    body = written.read_text()
    assert "<Url>ws://host.docker.internal:34567/</Url>" in body
    assert "<ReconnectIntervalSeconds>30</ReconnectIntervalSeconds>" in body

    assert hooks.READY_LINE.search("2026-09-21T10:00:00 12.345 INF StartGame done")
    assert hooks.HANDSHAKE_LINE.search(
        "2026-09-21T10:00:05 17.1 [Takaro] *INFO* WebSocket connection confirmed (inbound 'identifyResponse' frame)"
    )
    loaded = hooks.LOADED_LINE.search("2026-09-21T09:59:00 1.2 [MODS]     Loaded Mod: Takaro (0.1.6-dev.abc1234)")
    assert loaded and loaded.group("version") == "0.1.6-dev.abc1234"
    assert hooks.QUIT_LINE.search("2026-09-21T10:05:00 INF Preparing quit")
    assert hooks.RECONNECT_BUDGET >= 120.0


def test_the_runtime_identity_comes_from_the_server_banner() -> None:
    adapter = adapter_for(GAME)
    mono = {"loader": "mono", "loaderVersion": None}

    # The two lines this server writes about itself, as captured from a real boot.
    assert adapter.parse_runtime_identity("2026-09-21T11:03:39 1.413 INF Last played version: V 3.2.0") == {
        "gameVersion": "3.2.0",
        **mono,
    }
    assert adapter.parse_runtime_identity("GamePref.GameVersion = V 3.2.0") == {"gameVersion": "3.2.0", **mono}
    assert adapter.parse_runtime_identity("INF Last played version: V 3.2.0 (b10)") == {
        "gameVersion": "3.2.0.b10",
        **mono,
    }
    # A line about another version entirely: the world's, not the server's.
    assert adapter.parse_runtime_identity("INF Loaded world file from different version: 'V 4.0 (b8)'") is None
    assert adapter.parse_runtime_identity("nothing to see here") is None


def test_the_check_ids_add_the_7d2d_lifecycle_checks() -> None:
    from takaro_maint.verify.runner import check_ids

    ids = check_ids(GAME)

    assert {"handshake", "action", "reconnect", "stop"} <= set(ids)


# -- the release record ------------------------------------------------------------------


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


def test_compat_record_carries_the_steam_pin(run: Any, repo: Path, tmp_path: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    resolved = resolve(run, repo)
    directory = tmp_path / "dist" / TARGET
    _mod_zip(directory / ZIP_NAME)
    row = artifact_row("server-mod", TARGET, resolved["fingerprint"], directory / ZIP_NAME)
    write_meta(directory, row, connector=GAME, version=VERSION, revision=commit)
    write_manifest(
        directory,
        connector=GAME,
        version=VERSION,
        revision=commit,
        dirty=False,
        toolchain=resolved["build"]["toolchain"],
        mode="container",
        artifacts=[row],
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
    assert entry["verification"] == {
        "required": "contract",
        "executed": None,
        "report": None,
        "outcome": None,
        "takaro": None,
    }
    url = entry["inputs"]["server"]["url"]
    assert url.startswith("steam://app/294420/")
    assert re.search(r"manifest/[0-9]+", url)
    assert (out / ZIP_NAME).is_file()
    assert (out / "takaro-7d2d-mod.zip").read_bytes() == (out / ZIP_NAME).read_bytes()


# -- the dev-servers split ---------------------------------------------------------------

DS_ROOT = Path(__file__).resolve().parents[2] / "dev-servers"
REGISTRY_FIXTURE = Path(__file__).parent / "fixtures" / "games" / "7d2d" / "dev-servers-registry.expected"

# The one deliberate behaviour change in this PR: 7D2D's deployed artifact now depends on
# its catalog target, so the target record is part of its source fingerprint. Everything
# else in the registry has to come out of the split byte for byte.
SEVEND2D_SOURCES = "games/7d2d/mod/src games/7d2d/mod/Takaro.csproj games/7d2d/mod/ModInfo.xml games/7d2d/version.txt"


def bash(script: str) -> str:
    completed = subprocess.run(["bash", "-c", script], cwd=DS_ROOT.parent, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_dev_servers_registry_is_byte_identical_after_the_split() -> None:
    expected = REGISTRY_FIXTURE.read_text().replace(SEVEND2D_SOURCES, f"{SEVEND2D_SOURCES} catalog/7d2d")

    actual = bash('. dev-servers/lib/common.sh; ds_registry; for g in $(ds_game_ids); do ds_source_paths "$g"; done')

    assert actual == expected


def test_dev_servers_scripts_parse_and_dispatch() -> None:
    scripts = sorted(DS_ROOT.glob("lib/**/*.sh")) + sorted(DS_ROOT.glob("scripts/*.sh"))
    for script in scripts:
        completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, f"{script.name}: {completed.stderr}"

    assert bash(". dev-servers/lib/common.sh; ds_target_prefix 7d2d").strip() == "SEVEND2D"
    assert bash(". dev-servers/lib/common.sh; ds_target_dest 7d2d").strip().endswith("/7d2d/ServerFiles")
    assert bash(". dev-servers/lib/common.sh; ds_target_prefix minecraft-fabric").strip() == "MC_FABRIC"

    for game in bash(". dev-servers/lib/common.sh; ds_game_ids").split():
        found = bash(f'. dev-servers/lib/common.sh; declare -F "install_$(ds_fn_id {game})" >/dev/null && echo yes')
        assert found.strip() == "yes", f"{game} has no install step"

    refused = subprocess.run(
        ["bash", "-c", ". dev-servers/lib/common.sh; ds_dispatch install nope"],
        cwd=DS_ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "no install step defined for nope" in refused.stderr
