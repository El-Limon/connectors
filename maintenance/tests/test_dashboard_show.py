"""``dashboard show``, and the two other places the same one-shot command is run from.

The command half of this module drives ``cli.main`` against the in-process fakes the scan
scenarios already use, so what is asserted is stdout and the exit code — including the
scenario a fresh ephemeral runner has to survive: a run interrupted mid-write, resumed
from tracker state alone, filing nothing twice.

The workflow, the container recipe and the two documents are checked as the files they
are. The scheduled run's gate is a shell literal inside the workflow, so rather than
restate it the test extracts that literal and executes it — against this branch, where it
must refuse, and against a copy that says ``enabled``, where it must allow.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import test_lifecycle as lifecycle
import test_readiness_transitions as rt
import test_scan_support as support
from conftest import REPO_ROOT
from takaro_maint import readiness

WORKFLOW = REPO_ROOT / ".github/workflows/maintenance.yml"
SCHEDULE = REPO_ROOT / "maintenance/config/schedule.yaml"
DOCKERFILE = REPO_ROOT / "maintenance/Dockerfile"
JUSTFILE = REPO_ROOT / "justfile"
README = REPO_ROOT / "maintenance/README.md"
OPERATIONS = REPO_ROOT / "maintenance/docs/operations.md"
INTEGRATION = REPO_ROOT / "maintenance/docs/integration.md"

SOURCES_HEADER = "| Source | Status | Head | Seen | Last success | Last error |"
WORK_HEADER = "| Work | Issue | State | Since |"


def api_url(harness: support.Rig) -> str:
    """The one tracker URL of whichever rig this is: the bare issue fake, or the composite."""
    tracker = getattr(harness, "tracker", None)
    return harness.fake.api_url if tracker is None else str(tracker.api_url)


def show(run: Any, harness: support.Rig, *flags: str) -> tuple[int, Any, str]:
    return run("dashboard", "show", "--repo", support.REPO, "--api-url", api_url(harness), *flags, repo=harness.root)


def one_shot(run: Any, harness: support.Rig, *flags: str) -> tuple[int, Any, str]:
    # A real invocation is one process, so the readiness registry starts empty; a rig that
    # drives several runs in one process has to say so, exactly as ``Rig.scan`` does.
    readiness.reset_registry()
    return run("run", "--repo", support.REPO, "--api-url", api_url(harness), *flags, repo=harness.root)


def test_show_without_a_dashboard_is_absent_and_lists_watched_sources_as_uninitialised(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracker nobody has scanned yet still answers, and says which sources are waiting."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = show(run, harness)

        assert code == 0, stderr
        assert payload["ok"] is True
        assert payload["issue"] is None
        assert payload["url"] is None
        assert payload["health"] == "absent"
        assert payload["sources"][support.SOURCE_KEY]["status"] == "uninitialized"
        assert payload["sources"][support.SOURCE_KEY]["checkpoint"] is None
        assert payload["counts"]["uninitialized"] >= 1
        assert payload["work"] == {}


def test_show_after_a_bootstrap_reports_last_success_heads_and_checkpoints(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything the board recorded is what the read reports — and the read writes nothing."""
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        issue = harness.dashboard_issue()
        assert issue is not None
        writes_before = harness.fake.writes

        code, payload, stderr = show(run, harness)

        assert code == 0, stderr
        assert payload["issue"] == issue["number"]
        assert payload["health"] == "ok"
        assert payload["lastSuccess"] == support.FROZEN_NOW
        state = harness.dashboard_state()
        entry = payload["sources"][support.SOURCE_KEY]
        assert entry["heads"] == state["sources"][support.SOURCE_KEY]["heads"]
        assert entry["checkpoint"]["seen"] == len(harness.checkpoint_ids())
        assert len(payload["work"]) == 1
        assert next(iter(payload["work"].values()))["state"] == "detected"
        assert payload["counts"]["work"] == 1
        assert harness.fake.writes == writes_before


def test_a_failed_source_makes_run_exit_four_and_show_says_degraded(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source is down: the process fails, the other checkpoints advance, the board says so."""
    with rt.rig(catalog_copy, monkeypatch) as harness:
        rt._seed_support_issue(harness, "26.2")
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        before = show(run, harness)[1]
        fabric_before = before["sources"][rt.FABRIC_KEY]["checkpoint"]["seen"]

        rt.add_fabric_api(harness.upstream, "0.161.0+26.3")
        harness.upstream.status_overrides[rt.PAPER_PROJECT_PATH] = 503

        code, payload, stderr = one_shot(run, harness, "--publish")
        assert code == 4, stderr
        assert payload["exitCodes"]["scan"] == 4

        code, after, stderr = show(run, harness)
        assert code == 0, stderr
        assert after["sources"][rt.PAPER_KEY]["status"] == "failed"
        assert "503" in str(after["sources"][rt.PAPER_KEY]["lastError"])
        assert after["sources"][rt.FABRIC_KEY]["status"] == "ok"
        assert after["sources"][rt.FABRIC_KEY]["checkpoint"]["seen"] > fabric_before
        assert after["lastSuccess"] == before["lastSuccess"]
        assert after["health"] == "degraded"
        assert after["counts"]["failed"] == 1


def test_a_tracker_write_failure_exits_nine_and_show_reflects_nothing_new(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard write is the commit point: it fails, so nothing about the run happened."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        seen_before = show(run, harness)[1]["sources"][support.SOURCE_KEY]["checkpoint"]["seen"]
        issue = harness.dashboard_issue()
        assert issue is not None

        support.add_release(harness.upstream, "26.4", release_time="2026-09-18T08:00:00Z")
        harness.fake.fail_on(rf"/issues/{issue['number']}$")

        code, payload, stderr = one_shot(run, harness, "--publish")
        assert code == 9, stderr
        assert payload["exitCodes"] == {"scan": 9, "reconcile": None}

        harness.fake.fail_on(r"(?!)")  # a pattern that matches nothing: the fault is cleared
        code, after, stderr = show(run, harness)
        assert code == 0, stderr
        assert after["sources"][support.SOURCE_KEY]["checkpoint"]["seen"] == seen_before


def test_an_interrupted_run_resumes_and_show_lists_the_work_once(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: a fresh runner with no disk state resumes from the tracker and files nothing twice.

    The composite tracker, because this is the only scenario here that needs a whole ``run`` to
    succeed: ``reconcile`` lists the repository's releases, which the bare issue fake cannot
    serve. One source, so what "filed once" means is countable.
    """
    with lifecycle.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        harness.fake.fail_after(1)

        code, _payload, stderr = one_shot(run, harness, "--bootstrap", "--publish", "--source", "mojang-meta")
        assert code == 9, stderr
        assert len(harness.support_issues()) == 1
        assert harness.dashboard_issue() is None

        harness.fake.fail_after(10**6)
        code, _payload, stderr = one_shot(run, harness, "--bootstrap", "--publish", "--source", "mojang-meta")
        assert code == 0, stderr

        code, after, stderr = show(run, harness)
        assert code == 0, stderr
        assert len(after["work"]) == 1
        assert after["counts"]["work"] == 1
        assert len(harness.support_issues()) == 1


def test_show_table_format_prints_the_two_tables(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--format table`` is the dashboard's own two tables, not a JSON document."""
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        issue = harness.dashboard_issue()
        assert issue is not None

        code, payload, stderr = show(run, harness, "--format", "table")

        assert code == 0, stderr
        assert isinstance(payload, str)
        assert SOURCES_HEADER in payload
        assert WORK_HEADER in payload
        assert support.SOURCE_KEY in payload
        assert f"#{issue['number']}" in payload
        with pytest.raises(json.JSONDecodeError):
            json.loads(payload)


def test_show_out_writes_the_document_with_mode_0600(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``--out`` copy is the stdout document, readable only by the user who ran it."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        destination = tmp_path / "reports" / "dashboard.json"

        code, payload, stderr = show(run, harness, "--out", str(destination))

        assert code == 0, stderr
        assert json.loads(destination.read_text(encoding="utf-8")) == payload
        assert destination.stat().st_mode & 0o777 == 0o600


def test_show_without_a_token_exits_nine(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reading the tracker still needs a credential, and the report says which one."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("PATH", str(empty))

        code, payload, stderr = show(run, harness)

        assert code == 9, stderr
        assert payload["ok"] is False
        assert "GH_TOKEN" in payload["error"]


def _run_block_lines(text: str) -> set[int]:
    """The line numbers that belong to a ``run:`` script, by indentation."""
    inside: set[int] = set()
    block_indent: int | None = None
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if stripped and indent <= block_indent:
                block_indent = None
            else:
                inside.add(number)
                continue
        if stripped.startswith("run:"):
            inside.add(number)
            block_indent = indent
    return inside


def test_the_maintenance_workflow_is_the_same_command_in_a_container() -> None:
    """The dispatched run builds the pinned image and hands it the argument list unchanged."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "cron: '17 */6 * * *'",
        "group: maintenance-publisher",
        "cancel-in-progress: false",
        "-f maintenance/Dockerfile maintenance",
        'takaro-maint:ci "${args[@]}"',
        'args=(run --repo "$TRACKER_REPO" --out /out/result.json)',
        "retention-days: 14",
        "GH_TOKEN: ${{ steps.app-token.outputs.token || github.token }}",
        "create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1",
    ):
        assert needle in text, needle

    # A secret interpolated into a shell script is a secret in the process table and in every
    # `set -x` line; the only places one may appear are the action input and the presence flag.
    inside = _run_block_lines(text)
    for number, line in enumerate(text.splitlines()):
        if "${{ secrets." not in line:
            continue
        assert number not in inside, f"line {number + 1} interpolates a secret into a run: block"
        assert line.strip().startswith(("app-id:", "private-key:", "HAS_APP:")), line


def test_the_schedule_gate_is_the_tracked_config(tmp_path: Path) -> None:
    """The gate is one grep on one tracked file — so the test runs that very grep."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"grep -qxE '[^']+' maintenance/config/schedule\.yaml", text)
    assert match, "the workflow no longer gates scheduled publication on the tracked file"
    literal = match.group(0)

    disabled = subprocess.run(["bash", "-c", literal], cwd=REPO_ROOT, check=False)
    assert disabled.returncode == 1, "scheduled publication is enabled on this branch"

    staged = tmp_path / "maintenance" / "config"
    staged.mkdir(parents=True)
    (staged / "schedule.yaml").write_text(
        SCHEDULE.read_text(encoding="utf-8").replace("publish_schedule: disabled", "publish_schedule: enabled"),
        encoding="utf-8",
    )
    enabled = subprocess.run(["bash", "-c", literal], cwd=tmp_path, check=False)
    assert enabled.returncode == 0, "the gate does not open when the file says enabled"

    assert "publish_schedule: disabled" in SCHEDULE.read_text(encoding="utf-8").splitlines()


def test_the_container_recipe_is_pinned_and_carries_no_credentials() -> None:
    """Every layer is pinned by digest or by the lock, and nothing credential-shaped is baked."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    for needle in (
        "FROM python:3.12-slim@sha256:",
        "ghcr.io/astral-sh/uv:0.12.15@sha256:",
        "uv sync --frozen",
        "depotdownloader.ensure(",
        'ENTRYPOINT ["/repo/maintenance/bin/takaro-maint"]',
    ):
        assert needle in text, needle

    # Continuations joined first: an ENV is one statement however many lines it is spread over.
    for statement in re.sub(r"\\\n", " ", text).splitlines():
        if statement.startswith(("ENV ", "ARG ")):
            assert not re.search(r"token|password|secret", statement, re.IGNORECASE), statement

    just = JUSTFILE.read_text(encoding="utf-8")
    assert "maint-container-build" in just
    assert re.search(r"^maint-container \*args:", just, re.MULTILINE), "the justfile has no maint-container recipe"
    assert "maintenance/Dockerfile" in just
    # `-e GH_TOKEN` with no `=`: docker forwards the variable only if it is set, and its value
    # never appears on a command line.
    assert re.search(r"-e GH_TOKEN(?!=)", just)


def test_the_docs_index_lists_both_documents() -> None:
    """Both documents exist, say what they promise, and are reachable from the README."""
    readme = README.read_text(encoding="utf-8")
    assert "docs/operations.md" in readme
    assert "docs/integration.md" in readme
    assert "`dashboard show" in readme

    operations = OPERATIONS.read_text(encoding="utf-8")
    assert "## Credentials" in operations
    assert "## Schedule" in operations

    integration = INTEGRATION.read_text(encoding="utf-8")
    assert "## Resolve" in integration
    assert "## The compatibility record" in integration
