"""Every standalone shell harness is part of the pytest gate too."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

HARNESSES = tuple(sorted((REPO_ROOT / "maintenance/tests").glob("test_*.sh")))


@pytest.mark.parametrize("script", HARNESSES, ids=lambda path: path.name)
def test_shell_harness(script: Path) -> None:
    if script.name == "test_package_determinism.sh" and shutil.which("zip") is None:
        pytest.skip("test_package_determinism.sh needs the zip command")

    completed = subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-40:])
    assert completed.returncode == 0, f"{script.name} exited {completed.returncode}\n{tail}"
