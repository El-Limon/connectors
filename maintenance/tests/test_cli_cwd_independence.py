"""The command must behave the same from any directory, with or without CI variables."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "maintenance" / "bin" / "takaro-maint"

HOSTILE_ENV = {
    "GITHUB_WORKSPACE": "/nonexistent",
    "GITHUB_REPOSITORY": "someone/else",
    "GITHUB_SHA": "deadbeef",
    "GITHUB_REF": "refs/heads/not-this-one",
    "RUNNER_TEMP": "/nonexistent",
    "RUNNER_OS": "Plan9",
    "CI": "true",
}


def run_module(cwd: Path, extra_env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the package the same way the launcher does, but without needing uv in the test."""
    env = {**os.environ, **extra_env, "PYTHONPATH": str(REPO_ROOT / "maintenance" / "src")}
    return subprocess.run(
        [sys.executable, "-m", "takaro_maint", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolve_is_identical_from_a_foreign_directory(tmp_path: Path) -> None:
    from_repo = run_module(REPO_ROOT, {}, "targets", "resolve", "--game", "minecraft", "--platform", "fabric")
    from_elsewhere = run_module(
        tmp_path, HOSTILE_ENV, "targets", "resolve", "--game", "minecraft", "--platform", "fabric"
    )

    assert from_repo.returncode == 0, from_repo.stderr
    assert from_elsewhere.returncode == 0, from_elsewhere.stderr
    assert json.loads(from_elsewhere.stdout) == json.loads(from_repo.stdout)


def test_list_is_identical_from_a_foreign_directory(tmp_path: Path) -> None:
    from_repo = run_module(REPO_ROOT, {}, "targets", "list")
    from_elsewhere = run_module(tmp_path, HOSTILE_ENV, "targets", "list")

    assert json.loads(from_elsewhere.stdout) == json.loads(from_repo.stdout)


def test_validation_works_from_a_foreign_directory(tmp_path: Path) -> None:
    result = run_module(tmp_path, HOSTILE_ENV, "catalog", "validate")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_no_source_file_reads_a_ci_only_variable() -> None:
    """Resolution comes from the catalog and explicit flags, never from the runner."""
    import re

    banned = ("GITHUB_WORKSPACE", "GITHUB_REPOSITORY", "GITHUB_SHA", "RUNNER_TEMP", "GITHUB_ENV")
    names = "|".join(banned)
    reads = re.compile(r"""(?:environ(?:\.get)?\(?\[?|getenv\()\s*["'](""" + names + r""")["']""")
    offenders = []
    for source in (REPO_ROOT / "maintenance" / "src").rglob("*.py"):
        for name in reads.findall(source.read_text(encoding="utf-8")):
            offenders.append(f"{source.name}: {name}")

    assert offenders == []


def test_the_launcher_refuses_clearly_without_uv(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    result = subprocess.run(
        [bash, str(LAUNCHER), "targets", "list"],
        cwd=tmp_path,
        env={"PATH": str(empty_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "uv" in result.stderr
