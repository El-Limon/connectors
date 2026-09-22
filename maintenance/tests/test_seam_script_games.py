"""The seams a script-built, non-Minecraft game needs from the shared maintenance core.

Everything here is exercised through a stub game that is discovered exactly like a real
one: its adapter package is dropped on ``takaro_maint.games.__path__`` and its records are
written into a copy of the catalog. Nothing in the shared core names it.
"""

from __future__ import annotations

import importlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

GAME_ID = "stubgame"
TARGET_ID = "linux-1.0"

ADAPTER_SOURCE = '''
"""A minimal game adapter, used by the seam tests only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from takaro_maint import output
from takaro_maint.exit_codes import OK
from takaro_maint.games.base import BaseAdapter

class StubAdapter(BaseAdapter):
    id = "stubgame"

    def env(self, resolved: dict[str, Any], prefix: str) -> dict[str, str]:
        return {f"{prefix}_TARGET": str(resolved["id"]), f"{prefix}_FP16": str(resolved["fp16"])}

    def artifact_paths(self, resolved: dict[str, Any], version: str, repo_root: Path) -> dict[str, Path]:
        return {"server-mod": repo_root / f"takaro-stubgame-mod-{version}.zip"}

    def build(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("the stub game is never built")

    def runtime_env(self, resolved: dict[str, Any], takaro: dict[str, str]) -> dict[str, str]:
        return {"STUB_GAME": "1", **takaro}

    def parse_runtime_identity(self, log_line: str) -> dict[str, Any] | None:
        return None

    def preserve_globs(self, resolved: dict[str, Any]) -> list[str]:
        return list(resolved.get("preserve", []))

    def container_mounts(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        return [f"{data_dir}:/srv/game", f"{data_dir}/.takaro/logs:/srv/log"]

    def install(self, catalog: Any, target: Any, resolved: dict[str, Any], args: Any) -> int:
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "installed-by-the-adapter").write_text(target.id, encoding="utf-8")
        output.emit(
            "install",
            True,
            status="installed-by-the-adapter",
            game=target.game,
            target=target.id,
            fingerprint=target.fingerprint,
            dest=str(dest),
        )
        return OK


GAME = StubAdapter()
'''

HOOKS_SOURCE = '''
"""Verification hooks for the stub game."""

from __future__ import annotations

import re
from typing import Any

from takaro_maint.verify.hooks import GameHooks

READY_LINE = re.compile(r"INF StartGame done")
BOOTED: list[dict[str, str]] = []


def before_boot(run: Any, takaro_env: dict[str, str]) -> None:
    BOOTED.append(dict(takaro_env))
    marker = run.data_dir / "config-written-before-boot"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(takaro_env["TAKARO_WS_URL"], encoding="utf-8")


HOOKS = GameHooks(ready_line=READY_LINE, before_boot=before_boot)
'''

HOOKLESS_SOURCE = '''
"""A verification module that forgot to assemble its hooks."""

from __future__ import annotations

import re

READY_LINE = re.compile(r"INF StartGame done")
'''

DOCKER_STUB = """
import os, pathlib, sys

argv = sys.argv[1:]
log = pathlib.Path(os.environ["SEAM_DOCKER_LOG"])
if argv[:1] == ["network"]:
    print("127.0.0.1")
    raise SystemExit(0)
if argv[:1] == ["run"]:
    marker = pathlib.Path(os.environ["SEAM_MARKER"])
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"run config-written={marker.is_file()}\\n")
    print("stub-container")
    raise SystemExit(0)
print("")
"""

STEAM_DEPOTS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/gettakaro/connectors/catalog/schema/v1/inputs/steam-depots.schema.json",
    "title": "Steam depot set (seam-test stand-in)",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "source",
        "hashOrigin",
        "app",
        "branch",
        "buildid",
        "os",
        "arch",
        "depots",
        "files",
        "credentials",
    ],
    "properties": {
        "kind": {"const": "steam-depots"},
        "source": {"type": "string", "minLength": 1},
        "hashOrigin": {"enum": ["upstream", "self-recorded"]},
        "app": {"type": "integer", "minimum": 1},
        "branch": {"type": "string", "minLength": 1},
        "buildid": {"type": "integer", "minimum": 1},
        "os": {"enum": ["linux", "windows", "macos"]},
        "arch": {"enum": ["64", "32"]},
        "depots": {"type": "object", "minProperties": 1, "additionalProperties": True},
        "files": {"type": "object", "minProperties": 1, "additionalProperties": True},
        "credentials": {"type": ["object", "null"]},
    },
}

DIGEST = "sha256:" + "a" * 64


def _game_record() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "id": GAME_ID,
        "name": "Stub Game",
        "connector": GAME_ID,
        "platforms": ["linux"],
        "sources": {"steam": {"provider": "steam", "baseUrl": "https://store.steampowered.com"}},
        "build": {"system": "script", "projectDir": f"games/{GAME_ID}"},
        "componentRoles": {
            "server-mod": {
                "description": "Server-side mod folder the dedicated server loads",
                "artifactPattern": "takaro-stubgame-mod-{version}.zip",
            }
        },
        "legacyAssetAliases": {},
        "devServers": {"composeFile": f"{GAME_ID}.yml"},
    }


def _target_record() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "id": TARGET_ID,
        "game": GAME_ID,
        "platform": "linux",
        "revision": "1.0",
        "default": True,
        "support": {"status": "candidate", "since": "2026-09-21", "evidence": ["the seam tests"]},
        "inputs": {
            "server": {
                "kind": "steam-depots",
                "source": "steam",
                "hashOrigin": "self-recorded",
                "app": 294420,
                "branch": "public",
                "buildid": 24994542,
                "os": "linux",
                "arch": "64",
                "depots": {
                    "294423": {"manifest": "2222222222222222222"},
                    "294422": {"manifest": "1111111111111111111"},
                },
                "files": {
                    "bin/server": {"sha256": "b" * 64, "size": 14800},
                    "lib/Managed.dll": {"sha256": "c" * 64, "size": 2048},
                },
                "credentials": None,
            }
        },
        "runtime": {
            "java": None,
            "container": {"kind": "container-image", "image": "example/server", "tag": "v0.9.3", "digest": DIGEST},
        },
        "build": {
            "system": "script",
            "script": f"games/{GAME_ID}/scripts/build-release.sh",
            "toolchain": {"kind": "container-image", "image": "mono", "tag": "6.12.0.182-slim", "digest": DIGEST},
            "references": ["regex:^lib/.*\\\\.dll$"],
            "deps": {"websocket-sharp": {"coordinate": "github:sta/websocket-sharp:f7904e6", "sha256": "d" * 64}},
        },
        "components": [{"role": "server-mod", "artifact": "takaro-stubgame-mod-{version}.zip", "installDir": "Mods"}],
        "verification": {"required": "contract", "separate": []},
        "preserve": ["Takaro"],
    }


@pytest.fixture
def stub_game(catalog_copy: Path, tmp_path: Path) -> Any:
    """A discovered game adapter with verification hooks, plus its catalog records."""
    from takaro_maint import games

    package_root = tmp_path / "games-extra"
    package = package_root / GAME_ID
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(ADAPTER_SOURCE, encoding="utf-8")
    (package / "verify.py").write_text(HOOKS_SOURCE, encoding="utf-8")

    games.__path__.append(str(package_root))
    games.adapters.cache_clear()
    importlib.invalidate_caches()

    write_game(catalog_copy, _game_record())
    write_target(catalog_copy, _target_record())
    write_schema(catalog_copy, STEAM_DEPOTS_SCHEMA)
    write_build_script(catalog_copy)

    try:
        yield catalog_copy
    finally:
        games.__path__.remove(str(package_root))
        games.adapters.cache_clear()
        for name in [n for n in sys.modules if n.startswith(f"takaro_maint.games.{GAME_ID}")]:
            del sys.modules[name]


def write_game(root: Path, record: dict[str, Any]) -> None:
    path = root / "catalog" / GAME_ID / "game.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def write_target(root: Path, record: dict[str, Any]) -> Path:
    path = root / "catalog" / GAME_ID / "targets" / f"{TARGET_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def write_schema(root: Path, document: dict[str, Any]) -> None:
    path = root / "catalog/schema/v1/inputs/steam-depots.schema.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def write_build_script(root: Path) -> Path:
    path = root / "games" / GAME_ID / "scripts" / "build-release.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def failures(payload: dict[str, Any]) -> set[str]:
    return {check["id"] for check in payload["failures"]}


def passed(payload: dict[str, Any], check_id: str) -> list[dict[str, Any]]:
    return [c for c in payload["checks"] if c["id"] == check_id and c["status"] == "pass"]


# --------------------------------------------------------------------------- catalog


def test_a_script_built_target_with_no_java_validates(run: Any, stub_game: Path) -> None:
    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 0, payload["failures"]
    assert passed(payload, "build-system-schema")
    assert [c for c in payload["checks"] if c["id"] == "build-script-exists" and c["status"] == "pass"]


def test_a_java_version_below_the_floor_is_still_rejected(run: Any, stub_game: Path) -> None:
    record = _target_record()
    record["runtime"]["java"] = 7
    write_target(stub_game, record)

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "target-schema" in failures(payload)


def test_an_unknown_build_system_is_rejected(run: Any, stub_game: Path) -> None:
    """The target schema does not enumerate the build systems, so this is the only gate."""
    record = _target_record()
    record["build"]["system"] = "not-a-real-system"
    write_target(stub_game, record)

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "build-system-schema" in failures(payload)
    assert any("not-a-real-system" in check["detail"] for check in payload["failures"])


def test_a_script_build_must_match_its_system_schema(run: Any, stub_game: Path) -> None:
    record = _target_record()
    del record["build"]["toolchain"]
    write_target(stub_game, record)

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "build-system-schema" in failures(payload)


def test_a_build_script_that_is_not_in_the_repository_is_rejected(run: Any, stub_game: Path) -> None:
    (stub_game / "games" / GAME_ID / "scripts" / "build-release.sh").unlink()

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "build-script-exists" in failures(payload)


def test_every_file_of_a_multi_file_input_must_be_hashed(run: Any, stub_game: Path) -> None:
    record = _target_record()
    record["inputs"]["server"]["files"]["lib/Managed.dll"]["sha256"] = None
    write_target(stub_game, record)

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "no-null-hash" in failures(payload)
    assert any("inputs.server.files.lib/Managed.dll.sha256" in c["detail"] for c in payload["failures"])


def test_an_image_tag_may_carry_a_leading_v_but_not_a_floating_word(run: Any, stub_game: Path) -> None:
    code, payload, _ = run("catalog", "validate", repo=stub_game)
    assert code == 0, payload["failures"]

    record = _target_record()
    record["runtime"]["container"]["tag"] = "latest"
    write_target(stub_game, record)

    code, payload, _ = run("catalog", "validate", repo=stub_game)

    assert code == 2
    assert "immutable-tag-and-digest" in failures(payload)


# --------------------------------------------------------------------------- resolution


def test_a_depot_input_resolves_to_the_manifests_it_pins(run: Any, stub_game: Path) -> None:
    code, payload, _ = run("targets", "resolve", "--game", GAME_ID, repo=stub_game)

    assert code == 0, payload
    assert payload["resolvedUrls"] == {
        "server": (
            "steam://app/294420/branch/public/build/24994542"
            "/depot/294422/manifest/1111111111111111111;294423/manifest/2222222222222222222"
        )
    }


def test_the_gha_resolution_of_a_script_build_names_no_gradle_project(run: Any, stub_game: Path) -> None:
    code, payload, _ = run("targets", "resolve", "--game", GAME_ID, "--format", "gha", repo=stub_game)

    assert code == 0, payload
    fields = dict(line.split("=", 1) for line in str(payload).strip().splitlines())
    assert fields["build_system"] == "script"
    assert fields["java"] == ""
    assert "gradle_project" not in fields


# --------------------------------------------------------------------------- install and deploy


def test_install_delegates_to_a_game_that_owns_its_installation(run: Any, stub_game: Path, tmp_path: Path) -> None:
    dest = tmp_path / "server"

    code, payload, _ = run("install", "--game", GAME_ID, "--dest", str(dest), repo=stub_game)

    assert code == 0, payload
    assert payload["status"] == "installed-by-the-adapter"
    assert (dest / "installed-by-the-adapter").read_text() == TARGET_ID


def test_deploy_hands_the_placed_artifact_to_the_game(run: Any, wired: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from takaro_maint.games import adapter_for
    from test_cli_deploy import build_dir

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        type(adapter_for("minecraft")),
        "after_deploy",
        lambda self, dest, component, path: calls.append((str(dest), component["role"], path.name)),
        raising=False,
    )

    dest = tmp_path / "server"
    code, payload, _ = run(
        "install", "--game", "minecraft", "--target", "fabric-26.2", "--dest", str(dest), repo=wired.root
    )
    assert code == 0, payload
    out = build_dir(run, wired, tmp_path)

    code, payload, _ = run(
        "deploy",
        "--game",
        "minecraft",
        "--target",
        "fabric-26.2",
        "--dest",
        str(dest),
        "--from",
        str(out / "build-manifest.json"),
        repo=wired.root,
    )

    assert code == 0, payload
    assert calls == [(str(dest.resolve()), "server-mod", "takaro-minecraft-mod-fabric-26.2-0.1.1.jar")]
    assert (dest / "mods" / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar").is_file()


# --------------------------------------------------------------------------- verification


def target_run(root: Path, tmp_path: Path, game: str = GAME_ID, target: str | None = None) -> Any:
    from takaro_maint import paths
    from takaro_maint.catalog.loader import load
    from takaro_maint.verify.runner import RunOptions, TargetRun

    paths.set_repo_root(root)
    catalog = load()
    options = RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="seam")
    return TargetRun(catalog, catalog.select(game, target_id=target), options)


def test_the_hooks_are_found_next_to_the_adapter(stub_game: Path) -> None:
    from takaro_maint.verify.hooks import GameHooks
    from takaro_maint.verify.runner import game_hooks

    module = importlib.import_module(f"takaro_maint.games.{GAME_ID}.verify")
    assert game_hooks(GAME_ID) is module.HOOKS
    assert game_hooks("minecraft") is importlib.import_module("takaro_maint.games.minecraft.verify").HOOKS
    assert game_hooks("no-such-game") == GameHooks()


def test_a_verify_module_without_hooks_is_refused(stub_game: Path) -> None:
    """A module that ships hooks but never assembles them is a mistake, not a game with none."""
    from takaro_maint.exit_codes import UsageError
    from takaro_maint.verify.runner import game_hooks

    package = Path(str(importlib.import_module(f"takaro_maint.games.{GAME_ID}").__file__)).parent
    (package / "verify.py").write_text(HOOKLESS_SOURCE, encoding="utf-8")
    sys.modules.pop(f"takaro_maint.games.{GAME_ID}.verify", None)
    importlib.invalidate_caches()
    try:
        with pytest.raises(UsageError) as caught:
            game_hooks(GAME_ID)
    finally:
        (package / "verify.py").write_text(HOOKS_SOURCE, encoding="utf-8")
        sys.modules.pop(f"takaro_maint.games.{GAME_ID}.verify", None)
        importlib.invalidate_caches()

    assert f"takaro_maint.games.{GAME_ID}.verify" in str(caught.value)


def test_common_env_names_a_runtime_without_a_jvm(stub_game: Path, tmp_path: Path) -> None:
    """``str(None)`` would have written the literal ``JAVA=None`` into the rig's env file."""
    from takaro_maint import paths
    from takaro_maint.catalog.loader import load, resolve
    from takaro_maint.games.base import common_env

    paths.set_repo_root(stub_game)
    catalog = load()
    stub = resolve(catalog, catalog.select(GAME_ID))
    fabric = resolve(catalog, catalog.select("minecraft", target_id="fabric-26.2"))

    assert stub["runtime"]["java"] is None
    assert "STUB_JAVA" not in common_env(stub, "STUB")
    assert common_env(fabric, "MC")["MC_JAVA"] == str(fabric["runtime"]["java"])


def test_the_startup_check_waits_for_the_line_it_is_given(tmp_path: Path) -> None:
    from takaro_maint.verify import checks

    log = tmp_path / "server.log"
    log.write_text("2026-09-21 INF StartGame done\n", encoding="utf-8")

    with_default = checks.check_startup(log, 0.1, lambda: False, tmp_path, [])
    with_game_line = checks.check_startup(log, 0.1, lambda: False, tmp_path, [], re.compile("INF StartGame done"))

    assert with_default.status == "fail"
    assert with_game_line.status == "pass"
    assert with_game_line.detail["line"] == "2026-09-21 INF StartGame done"


def test_the_runner_takes_its_ready_line_from_the_game(stub_game: Path, tmp_path: Path) -> None:
    from takaro_maint.verify import checks

    run = target_run(stub_game, tmp_path)
    try:
        assert run.ready_line().pattern == "INF StartGame done"
    finally:
        run.cleanup()

    minecraft = target_run(stub_game, tmp_path, game="minecraft", target="fabric-26.2")
    try:
        assert minecraft.ready_line() is checks.DONE_LINE
    finally:
        minecraft.cleanup()


def test_the_runner_mounts_what_the_adapter_asks_for(stub_game: Path, tmp_path: Path) -> None:
    run = target_run(stub_game, tmp_path)
    try:
        argv = run.container_argv("ws://host.docker.internal:1/")
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]
        assert mounts == [f"{run.data_dir}:/srv/game", f"{run.data_dir}/.takaro/logs:/srv/log"]
        assert argv[-1] == run.resolved["containerRef"]
    finally:
        run.cleanup()


def test_the_game_writes_its_config_before_the_container_starts(
    stub_game: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    stub = tmp_path / "docker-stub.py"
    stub.write_text(DOCKER_STUB, encoding="utf-8")
    docker_log = tmp_path / "docker-calls.log"
    monkeypatch.setenv("TAKARO_MAINT_DOCKER", f"{sys.executable} {stub}")
    monkeypatch.setenv("SEAM_DOCKER_LOG", str(docker_log))

    run = target_run(stub_game, tmp_path)
    monkeypatch.setenv("SEAM_MARKER", str(run.data_dir / "config-written-before-boot"))
    try:
        run.boot("ws://host.docker.internal:1/")
        hooks = sys.modules[f"takaro_maint.games.{GAME_ID}.verify"]
        assert hooks.BOOTED[-1]["TAKARO_WS_URL"] == "ws://host.docker.internal:1/"
        assert hooks.BOOTED[-1]["TAKARO_REGISTRATION_TOKEN"] == run.registration_token
        assert (run.data_dir / "config-written-before-boot").is_file()
        assert "run config-written=True" in docker_log.read_text()
    finally:
        run.cleanup()


def test_a_report_records_every_file_a_depot_input_pins(stub_game: Path, tmp_path: Path) -> None:
    from takaro_maint import paths
    from takaro_maint.catalog.loader import load
    from takaro_maint.verify.report import build_report, write_report

    paths.set_repo_root(stub_game)
    catalog = load()
    target = catalog.select(GAME_ID)
    manifest = {"version": "0.1.1", "artifacts": []}
    report = build_report(
        target=target,
        game_record=catalog.game(GAME_ID).record,
        manifest=manifest,
        artifacts_dir=tmp_path,
        runtime={"gameVersion": None, "loader": None, "loaderVersion": None, "java": None},
        checks=[{"id": "startup", "status": "pass", "durationMs": 1, "detail": {}}],
        started_at="2026-09-21T00:00:00Z",
        logs=[],
        repo_root=stub_game,
    )

    pinned = (
        "steam://app/294420/branch/public/build/24994542"
        "/depot/294422/manifest/1111111111111111111;294423/manifest/2222222222222222222"
    )
    assert report["target"]["inputs"] == {
        "server:bin/server": {"url": f"{pinned}/bin/server", "sha256": "b" * 64, "size": 14800},
        "server:lib/Managed.dll": {"url": f"{pinned}/lib/Managed.dll", "sha256": "c" * 64, "size": 2048},
    }
    # The schema is the gate: a report that does not validate is never written.
    assert write_report(tmp_path / "out", report).is_file()
