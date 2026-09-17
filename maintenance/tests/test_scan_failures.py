"""What a failure costs: a nonzero exit, and never a checkpoint that skipped work.

A source that could not be read keeps the checkpoint it had, so the revisions it did not
see this time are still unseen next time. A tracker that moved under the run is refused
rather than overwritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import test_scan_support as support
from takaro_maint.github import GitHub


def test_an_unreachable_manifest_exits_four_with_the_checkpoint_retained(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        second = support.add_second_watch_source(catalog_copy, harness.upstream)
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        seen_before = sorted(harness.checkpoint_ids())

        support.add_release(harness.upstream, "26.4", release_time="2026-12-01T10:00:00+00:00")
        support.mirror_manifest(harness.upstream)
        harness.upstream.status_overrides[support.MANIFEST_PATH] = 503

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 4, stderr
        assert payload["ok"] is False
        broken = payload["sources"][support.SOURCE_KEY]
        assert broken["status"] == "failed"
        assert "503" in str(broken["error"])
        assert payload["sources"][second]["status"] == "ok"

        state = harness.dashboard_state()
        assert "503" in str(state["sources"][support.SOURCE_KEY]["lastError"])
        assert state["sources"][second]["lastError"] is None
        # The failed source kept exactly what it had; the healthy one moved on.
        assert sorted(harness.checkpoint_ids()) == seen_before
        assert "26.4" in harness.checkpoint_ids(second)
        assert state["lastSuccess"] is None or state["lastSuccess"] == state["sources"][second]["lastSuccess"]
        assert len(harness.support_issues()) == 2


def test_a_version_json_with_the_wrong_sha1_fails_the_source(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        path = harness.served["26.3"]
        harness.upstream.add(path, harness.upstream.files[path] + b"\n// tampered\n")

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 4, stderr
        assert payload["sources"][support.SOURCE_KEY]["status"] == "failed"
        assert "sha1 expected" in str(payload["sources"][support.SOURCE_KEY]["error"])
        assert harness.support_issues() == []
        assert harness.dashboard_state()["sources"][support.SOURCE_KEY]["checkpoint"] is None


def test_a_dashboard_conflict_exits_nine(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        body_before = str(harness.dashboard_issue()["body"])  # type: ignore[index]

        original = GitHub.issue_get

        def mutating(client: GitHub, number: int) -> Any:
            """Someone edits the dashboard between the scan's read and its write."""
            for issue in harness.fake.issues:
                if issue["number"] == number:
                    issue["body"] = str(issue["body"]) + "\nsomeone else typed here\n"
            return original(client, number)

        monkeypatch.setattr(GitHub, "issue_get", mutating)
        harness.fake.requests.clear()

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 9, stderr
        assert "changed during the scan" in str(payload["error"])
        assert [method for method, _ in harness.fake.requests if method == "PATCH"] == []
        assert str(harness.dashboard_issue()["body"]) == body_before + "\nsomeone else typed here\n"  # type: ignore[index]


def test_an_unknown_source_exits_two(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap", "--source", "not-a-source")

        assert code == 2, stderr
        assert "not-a-source" in str(payload["error"])
        assert harness.fake.requests == []


def test_an_unknown_game_exits_two(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap", "--game", "not-a-game")

        assert code == 2, stderr
        assert "not-a-game" in str(payload["error"])


def test_a_game_without_watch_sources_exits_two(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        support.drop_watch(catalog_copy)

        code, payload, stderr = harness.scan(run, "--bootstrap", "--game", "minecraft")

        assert code == 2, stderr
        assert "watch block" in str(payload["error"])
        assert harness.fake.requests == []
