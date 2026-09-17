"""The promise: one observation is one issue, however often the scan runs.

Every scenario here is a way the naive implementation would file a second issue — the
dashboard lost the checkpoint, the search index missed, a human rewrote the body, the
issue was closed, a previous run died between the issue write and the checkpoint write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import test_scan_support as support
from takaro_maint.github import GitHub
from takaro_maint.tracker import identity, issues

MARKER = identity.render_marker(
    {"kind": "support", "provider": "mojang", "component": "minecraft", "branch": "release", "rev": "26.3"}
)


def test_a_repeated_scan_creates_no_duplicate(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, first, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        assert [entry["action"] for entry in first["applied"]] == ["create-issue", "create-dashboard"]

        for _ in range(2):
            harness.forget("26.3")
            code, payload, stderr = harness.scan(run, "--publish")

            assert code == 0, stderr
            actions = [(entry["action"], entry.get("reason")) for entry in payload["applied"]]
            assert actions == [("noop", "tracked"), ("update-dashboard", None)]

        assert len(harness.support_issues()) == 1
        assert "26.3" in harness.checkpoint_ids()


def test_a_human_edit_outside_the_owned_block_survives(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        filed = harness.support_issues()[0]
        filed["title"] = "Minecraft 26.3 (Niek is on this)"
        filed["body"] = (
            str(filed["body"]).replace(
                issues.OWNED_BEGIN, "My own note: Paper matters more than Fabric here.\n\n" + issues.OWNED_BEGIN
            )
            + "\n## My plan\n\nPaper first.\n"
        )
        edited_body = str(filed["body"])
        support.add_candidate_target(catalog_copy, "26.3", platform="paper")
        harness.forget("26.3")

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 0, stderr
        assert [entry["action"] for entry in payload["applied"]] == ["update-issue", "update-dashboard"]
        assert len(harness.support_issues()) == 1
        body = str(harness.support_issues()[0]["body"])
        assert harness.support_issues()[0]["title"] == "Minecraft 26.3 (Niek is on this)"
        assert body.splitlines()[0] == MARKER
        assert "My own note: Paper matters more than Fabric here." in body
        assert body.endswith("\n## My plan\n\nPaper first.\n")
        assert body != edited_body
        assert body.count(issues.OWNED_BEGIN) == 1
        assert "| paper | `paper-26.3` | 26.3 | candidate | yes |" in body


def test_a_changed_observation_rewrites_only_the_owned_block(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        before = str(harness.support_issues()[0]["body"])
        support.add_candidate_target(catalog_copy, "26.9", platform="neoforge")
        harness.forget("26.3")

        code, payload, stderr = harness.scan(run, "--publish")

        after = str(harness.support_issues()[0]["body"])
        assert code == 0, stderr
        assert [entry["action"] for entry in payload["applied"]] == ["update-issue", "update-dashboard"]
        assert "| neoforge | `neoforge-26.9` | 26.9 | candidate | no |" in after
        assert after.partition(issues.OWNED_BEGIN)[0] == before.partition(issues.OWNED_BEGIN)[0]
        assert after.partition(issues.OWNED_END)[2] == before.partition(issues.OWNED_END)[2]


def test_lookup_paginates_across_open_and_closed_issues(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search misses entirely; the paginated open+closed listing is what finds the issue."""
    monkeypatch.setattr(GitHub, "issues_search", lambda _self, _query: [])
    with support.rig(catalog_copy, monkeypatch) as harness:
        for index in range(259):
            harness.seed_issue(
                identity.render_marker(
                    {
                        "kind": "support",
                        "provider": "mojang",
                        "component": "minecraft",
                        "branch": "release",
                        "rev": f"9.{index}",
                    }
                )
                + "\n",
                title=f"noise {index}",
                state="open" if index % 2 == 0 else "closed",
                state_reason=None if index % 2 == 0 else "completed",
            )
        harness.seed_issue(MARKER + "\n", title="the real one")
        real = harness.fake.issues[-1]["number"]

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 0, stderr
        assert [entry["action"] for entry in payload["applied"]] == ["update-issue", "create-dashboard"]
        assert payload["applied"][0]["issue"] == real
        assert len(harness.support_issues()) == 260
        assert len(harness.listing_requests()) >= 130


def test_search_hit_short_circuits_the_listing(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        harness.forget("26.3")
        harness.fake.requests.clear()

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 0, stderr
        assert payload["applied"][0]["action"] in ("noop", "update-issue")
        assert harness.listing_requests() == []


def test_a_closed_completed_issue_is_not_reopened(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        closed = harness.seed_issue(MARKER + "\n", title="done already", state="closed", state_reason="completed")

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 0, stderr
        applied = [(entry["action"], entry.get("reason")) for entry in payload["applied"]]
        assert applied == [("noop", "closed-completed"), ("create-dashboard", None)]
        assert closed["state"] == "closed"
        assert closed["body"] == MARKER + "\n"
        assert len(harness.support_issues()) == 1
        assert "26.3" in harness.checkpoint_ids()


def test_a_declined_issue_stays_declined(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        declined = harness.seed_issue(MARKER + "\n", title="we will not", state="closed", state_reason="not_planned")

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 0, stderr
        applied = [(entry["action"], entry.get("reason")) for entry in payload["applied"]]
        assert applied == [("noop", "declined"), ("create-dashboard", None)]
        assert declined["state"] == "closed"
        assert len(harness.support_issues()) == 1


def test_an_interrupted_run_resumes_without_a_duplicate(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        harness.fake.fail_after(1)  # the issue is created, the checkpoint write is not

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 9, stderr
        assert payload["ok"] is False
        assert [entry["action"] for entry in payload["applied"]] == ["create-issue"]
        assert len(harness.support_issues()) == 1
        assert harness.dashboard_issue() is None

        harness.fake.fail_after(1000)  # the tracker is healthy again
        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 0, stderr
        applied = [(entry["action"], entry.get("reason")) for entry in payload["applied"]]
        assert applied == [("noop", "tracked"), ("create-dashboard", None)]
        assert len(harness.support_issues()) == 1
        assert "26.3" in harness.checkpoint_ids()


def test_a_tracker_write_failure_exits_nine_and_keeps_the_checkpoint(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        body_before = str(harness.dashboard_issue()["body"])  # type: ignore[index]
        harness.fake.fail_on(r"/issues/\d+$")

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 9, stderr
        assert payload["ok"] is False
        assert str(harness.dashboard_issue()["body"]) == body_before  # type: ignore[index]
        assert "26.3" in harness.checkpoint_ids()


def test_text_above_the_marker_line_loses_the_identity(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented limit of marker identity, stated rather than discovered.

    Only the first non-empty line is parsed \u2014 that is what stops a marker quoted further
    down from hijacking an issue. The cost is that pushing the marker off line one makes
    the issue unrecognisable, so the intro sentence in every filed body says so.
    """
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap", "--publish")
        assert code == 0, stderr
        filed = harness.support_issues()[0]
        filed["body"] = "I typed above the marker.\n\n" + str(filed["body"])
        harness.forget("26.3")

        code, payload, stderr = harness.scan(run, "--publish")

        assert code == 0, stderr
        assert [entry["action"] for entry in payload["applied"]] == ["create-issue", "update-dashboard"]
        assert issues.INTRO.endswith("that marker is how this issue is recognised.")
