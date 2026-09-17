"""The command line itself: a foreign directory, a bare environment, no leaked token."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import test_scan_support as support
from test_cli_cwd_independence import HOSTILE_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]


def _without_clocks(payload: dict[str, Any]) -> dict[str, Any]:
    """The report minus the only value two runs cannot share: when they ran."""
    document = json.loads(json.dumps(payload))
    for observation in document["observations"]:
        observation.pop("observedAt", None)
    return document  # type: ignore[no-any-return]


def test_scan_runs_from_a_foreign_cwd_with_a_minimal_environment(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing about the report comes from the working directory or from CI variables."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        in_process_code, in_process, stderr = harness.scan(run, "--bootstrap")
        assert in_process_code == 0, stderr

        environment = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "PYTHONPATH": str(REPO_ROOT / "maintenance" / "src"),
            "GH_TOKEN": support.TOKEN,
            "TAKARO_MAINT_CACHE": str(tmp_path / "cache"),
            **HOSTILE_ENV,
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "takaro_maint",
                "--repo-root",
                str(catalog_copy),
                "scan",
                "--bootstrap",
                "--repo",
                support.REPO,
                "--api-url",
                harness.fake.api_url,
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        subprocess_payload = json.loads(result.stdout)
        assert subprocess_payload["repo"] == support.REPO
        assert _without_clocks(subprocess_payload) == _without_clocks(in_process)
        assert harness.fake.writes == 0


def test_out_writes_the_same_document_with_mode_0600(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "reports" / "scan.json"
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap", "--out", str(destination))

        assert code == 0, stderr
        assert json.loads(destination.read_text(encoding="utf-8")) == payload
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_the_token_never_reaches_stdout_or_stderr(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "s3cret-token-please-do-not-print"  # noqa: S105 - a test value, not a credential
    destination = tmp_path / "scan.json"
    with support.rig(catalog_copy, monkeypatch) as harness:
        monkeypatch.setenv("GH_TOKEN", secret)

        code, payload, stderr = run(
            "--verbose",
            "scan",
            "--bootstrap",
            "--repo",
            support.REPO,
            "--api-url",
            harness.fake.api_url,
            "--out",
            str(destination),
            repo=harness.root,
        )

        assert code == 0, stderr
        assert secret not in json.dumps(payload)
        assert secret not in stderr
        assert secret not in destination.read_text(encoding="utf-8")
        assert harness.fake.authorizations  # the token really was used
        assert all(f"Bearer {secret}" == value for value in harness.fake.authorizations)


def test_source_selection_filters_to_one_source(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        second = support.add_second_watch_source(catalog_copy, harness.upstream)

        code, both, stderr = harness.scan(run, "--bootstrap")
        assert code == 0, stderr
        assert sorted(both["sources"]) == sorted([support.SOURCE_KEY, second])

        code, one, stderr = harness.scan(run, "--bootstrap", "--source", "mojang-meta")

        assert code == 0, stderr
        assert list(one["sources"]) == [support.SOURCE_KEY]
        assert harness.requested(support.SECOND_MANIFEST_PATH) == 1  # only from the first run
