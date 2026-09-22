"""`deploy`: the right artifact, into the right installation, replacing the old one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import make_jar, sha256


@pytest.fixture
def installed(run: Any, wired: Any, tmp_path: Path) -> Path:
    dest = tmp_path / "server"
    code, payload, _ = run(
        "install", "--game", "minecraft", "--target", "fabric-26.2", "--dest", str(dest), repo=wired.root
    )
    assert code == 0, payload
    return dest


def build_dir(run: Any, wired: Any, tmp_path: Path, *, version: str = "0.1.1", target: str = "fabric-26.2") -> Path:
    """A hand-made build output: one stamped jar plus the manifest that describes it."""
    _, resolved, _ = run("targets", "resolve", "--game", "minecraft", "--target", "fabric-26.2", repo=wired.root)
    out = tmp_path / "dist"
    out.mkdir(parents=True, exist_ok=True)
    file_name = f"takaro-minecraft-mod-{target}-{version}.jar"
    jar = make_jar(
        out / file_name,
        target=target,
        fingerprint=resolved["fingerprint"],
        revision="26.2",
        version=version,
    )
    manifest = {
        "schemaVersion": 1,
        "connector": "minecraft",
        "version": version,
        "sourceRevision": "deadbeef",
        "dirty": False,
        "builtAt": "2026-09-17T00:00:00Z",
        "toolchain": {
            "image": "eclipse-temurin",
            "tag": "25-jdk",
            "digest": "sha256:" + "0" * 64,
            "mode": "container",
        },
        "artifacts": [
            {
                "role": "server-mod",
                "target": target,
                "fingerprint": resolved["fingerprint"],
                "file": file_name,
                "sha256": sha256(jar.read_bytes()),
                "size": jar.stat().st_size,
            }
        ],
    }
    (out / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    return out


def deploy(run: Any, wired: Any, dest: Path, out: Path) -> tuple[int, Any, str]:
    return run(
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


def test_deploy_places_the_artifact_and_records_it(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 0, payload
    jar = installed / "mods/takaro-minecraft-mod-fabric-26.2-0.1.1.jar"
    assert jar.is_file()
    ledger = json.loads((installed / ".takaro/installed-target.json").read_text())
    assert ledger["artifacts"][0]["path"] == "mods/takaro-minecraft-mod-fabric-26.2-0.1.1.jar"
    assert ledger["artifacts"][0]["connectorVersion"] == "0.1.1"
    assert ledger["artifacts"][0]["sha256"] == sha256(jar.read_bytes())


def test_an_older_takaro_jar_is_removed(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    mods = installed / "mods"
    (mods / "takaro-minecraft-mod-fabric-26.2-0.0.9.jar").write_bytes(b"an older build")
    (mods / "TakaroMinecraft.jar").write_bytes(b"a much older build")
    (mods / "takaro-fabric-0.1.0.jar").write_bytes(b"the pre-target asset name")
    out = build_dir(run, wired, tmp_path)

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 0
    assert sorted(p.name for p in mods.glob("takaro*")) == ["takaro-minecraft-mod-fabric-26.2-0.1.1.jar"]
    assert set(payload["removed"]) == {
        "mods/takaro-minecraft-mod-fabric-26.2-0.0.9.jar",
        "mods/TakaroMinecraft.jar",
        "mods/takaro-fabric-0.1.0.jar",
    }


def test_other_mods_are_left_alone(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    other = installed / "mods/some-other-mod.jar"
    other.write_bytes(b"not ours")
    out = build_dir(run, wired, tmp_path)

    deploy(run, wired, installed, out)

    assert other.read_bytes() == b"not ours"
    assert (installed / "mods/fabric-api-0.160.0+26.2.jar").is_file()


def test_a_manifest_for_another_target_is_a_conflict(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    manifest_file = out / "build-manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["artifacts"][0]["target"] = "fabric-26.1.2"
    manifest_file.write_text(json.dumps(manifest))

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 7
    assert "server-mod" in payload["error"]


def test_a_ledger_for_another_target_is_a_conflict(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    ledger_file = installed / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["fingerprint"] = "0" * 64
    ledger_file.write_text(json.dumps(ledger))

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 7
    assert "holds target" in payload["error"]


def test_a_directory_with_no_install_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()

    code, payload, _ = deploy(run, wired, empty, out)

    assert code == 7
    assert "install" in payload["error"]


def test_a_file_that_does_not_match_the_manifest_hash_is_a_conflict(
    run: Any, wired: Any, installed: Path, tmp_path: Path
) -> None:
    out = build_dir(run, wired, tmp_path)
    (out / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar").write_bytes(b"swapped after the manifest was written")

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 7
    assert "sha256" in payload["error"]


def test_a_missing_artifact_file_is_a_conflict(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    (out / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar").unlink()

    code, payload, _ = deploy(run, wired, installed, out)

    assert code == 7
    assert "missing next to it" in payload["error"]


def test_a_malformed_manifest_is_a_conflict(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    (out / "build-manifest.json").write_text("{not json")

    code, _, _ = deploy(run, wired, installed, out)

    assert code == 7


def test_a_manifest_row_that_names_a_file_outside_the_directory_is_a_conflict(
    run: Any, wired: Any, installed: Path, tmp_path: Path
) -> None:
    out = build_dir(run, wired, tmp_path)
    manifest_path = out / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    outside = tmp_path / "planted.jar"
    outside.write_bytes(b"not ours")
    manifest["artifacts"][0]["file"] = "../planted.jar"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    code, _, _ = deploy(run, wired, installed, out)

    assert code == 7
    assert not (installed / "mods/planted.jar").exists()


def test_ledger_check_passes_after_a_deploy(run: Any, wired: Any, installed: Path, tmp_path: Path) -> None:
    out = build_dir(run, wired, tmp_path)
    deploy(run, wired, installed, out)

    code, payload, _ = run(
        "ledger",
        "check",
        "--game",
        "minecraft",
        "--target",
        "fabric-26.2",
        "--dest",
        str(installed),
        repo=wired.root,
    )

    assert code == 0, payload["reasons"]


def test_ledger_check_fails_when_the_deployed_artifact_is_swapped(
    run: Any, wired: Any, installed: Path, tmp_path: Path
) -> None:
    out = build_dir(run, wired, tmp_path)
    deploy(run, wired, installed, out)
    (installed / "mods/takaro-minecraft-mod-fabric-26.2-0.1.1.jar").write_bytes(b"swapped in place")

    code, payload, _ = run(
        "ledger",
        "check",
        "--game",
        "minecraft",
        "--target",
        "fabric-26.2",
        "--dest",
        str(installed),
        repo=wired.root,
    )

    assert code == 7
    assert any("artifact" in reason for reason in payload["reasons"])


def test_an_interrupted_copy_leaves_the_previous_connector_in_place(
    run: Any, wired: Any, installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new file is staged before the old ones go, so a failed copy is not a server with no mod."""
    mods = installed / "mods"
    previous = mods / "takaro-minecraft-mod-fabric-26.2-0.0.9.jar"
    previous.write_bytes(b"an older build")
    out = build_dir(run, wired, tmp_path)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("takaro_maint.commands.deploy.shutil.copy2", explode)

    code, _, err = deploy(run, wired, installed, out)

    assert code != 0, err
    assert previous.read_bytes() == b"an older build"
    assert not (mods / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar").exists()
    assert not list(mods.glob("*.tmp"))
