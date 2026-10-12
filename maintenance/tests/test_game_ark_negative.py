"""Fail-closed classification for ARK's isolated wrong-build verification."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from takaro_maint.games.ark import GAME
from takaro_maint.games.ark import verify as ark_verify


@pytest.mark.parametrize(
    "preload_stdout,expected",
    [
        ("native_state=absent\nfixture_exit=0\n", "pass"),
        ("fixture_exit=0\n", "fail"),
        ("unexpected_native_state\n", "fail"),
    ],
)
def test_wrong_target_requires_both_launcher_refusal_and_no_native_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preload_stdout: str,
    expected: str,
) -> None:
    data = tmp_path / "data/TakaroArk/TakaroArkNative"
    data.mkdir(parents=True)
    (data / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (data / "libtakaro-ark-native.so").write_bytes(b"fixture-library")
    output = tmp_path / "out"
    output.mkdir()
    calls: list[list[str]] = []

    def run_command(argv: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        calls.append(argv)
        if len(calls) == 1:
            return SimpleNamespace(returncode=7, stdout="", stderr="Unknown ARK executable; native connector refused\n")
        return SimpleNamespace(returncode=0, stdout=preload_stdout, stderr="")

    monkeypatch.setattr(ark_verify.subprocess, "run", run_command)
    monkeypatch.setattr(ark_verify, "docker_command", lambda: ["docker"])

    class FakeRun:
        data_dir = tmp_path / "data"
        out = output
        target = SimpleNamespace(
            record={
                "inputs": {
                    "server": {
                        "files": {
                            "ShooterGame/Binaries/Linux/ShooterGameServer": {"size": 4096},
                        }
                    }
                }
            }
        )
        resolved = {"containerRef": "node@sha256:pinned"}
        results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "negative-wrong-target"

        def record(self, result: object) -> None:
            self.results.append(result)

    fixture = FakeRun()
    asyncio.run(ark_verify.negative(fixture, None, "", {}))
    assert fixture.results[0].status == expected
    assert calls[1][0:7] == ["docker", "run", "--rm", "--pull", "never", "--network", "none"]
    if expected == "pass":
        assert "native_state=absent" in (output / "negative-wrong-target-preload.log").read_text()
    assert all(not key.startswith("TAKARO_") for key in calls[0][-1:])  # launcher argv carries no token


@pytest.mark.parametrize(
    "role,folder,files",
    [
        ("server-plugin", "TakaroArkNative", ["libtakaro-ark-native.so", "launch.sh"]),
    ],
)
def test_ark_release_artifact_deploy_contract(
    tmp_path: Path,
    role: str,
    folder: str,
    files: list[str],
) -> None:
    artifact = tmp_path / f"{role}.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in [*files, "uninstall-manifest.json", "takaro-target.json"]:
            archive.writestr(f"{folder}/{name}", b"fixture")
    GAME.after_deploy(tmp_path / "server", {"role": role, "installDir": "TakaroArk"}, artifact)
    deployed = tmp_path / "server/TakaroArk" / folder
    assert all((deployed / name).is_file() for name in files)
    if role == "server-plugin":
        assert (deployed / "launch.sh").stat().st_mode & 0o111


def test_ark_redeploy_keeps_the_operator_config(tmp_path: Path) -> None:
    folder = tmp_path / "server/TakaroArk/TakaroArkNative"
    folder.mkdir(parents=True)
    (folder / "takaro.cfg").write_text("TAKARO_REGISTRATION_TOKEN=operator\n", encoding="utf-8")
    artifact = tmp_path / "native.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("libtakaro-ark-native.so", "launch.sh", "uninstall-manifest.json", "takaro-target.json"):
            archive.writestr(f"TakaroArkNative/{name}", b"fixture")
    GAME.after_deploy(tmp_path / "server", {"role": "server-plugin", "installDir": "TakaroArk"}, artifact)
    assert (folder / "takaro.cfg").read_text() == "TAKARO_REGISTRATION_TOKEN=operator\n"


def test_ark_sidecar_role_is_no_longer_deployable(tmp_path: Path) -> None:
    artifact = tmp_path / "sidecar.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("TakaroArkSidecar/dist/index.js", b"fixture")
    with pytest.raises(Exception, match="unexpected ARK component role"):
        GAME.after_deploy(tmp_path / "server", {"role": "sidecar", "installDir": "TakaroArk"}, artifact)
