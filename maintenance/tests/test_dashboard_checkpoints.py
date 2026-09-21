"""The dashboard issue is the state, so its body and its checkpoints are pinned here.

A checkpoint answers "have I filed this before?". It has to survive a body that GitHub
would refuse for being too long, which is why it keeps a floor as well as a list of ids:
the ids can be dropped, the floor cannot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import test_scan_support as support
from fake_github import FakeGitHub
from takaro_maint.github import GitHub
from takaro_maint.tracker import checkpoints, dashboard, identity

FROZEN = support.FROZEN_NOW
KEY = support.SOURCE_KEY


def test_a_fresh_dashboard_renders_the_golden_body() -> None:
    body = dashboard.render(support.golden_dashboard())

    assert body == (support.GITHUB_FIXTURES / "dashboard.md").read_text(encoding="utf-8")
    assert body.splitlines()[0] == identity.render_marker(identity.DASHBOARD_MARKER)


def test_an_empty_dashboard_still_renders_readable_tables() -> None:
    body = dashboard.render(dashboard.Dashboard.empty())

    assert "| (none) |" in body
    assert dashboard.BEGIN in body and dashboard.END in body


def test_load_round_trips_save() -> None:
    board = support.golden_dashboard()
    with FakeGitHub() as fake:
        client = GitHub(support.REPO, support.TOKEN, fake.api_url)

        number, created = dashboard.save(client, board)
        loaded = dashboard.load(client)

        assert created is True
        assert loaded.issue == number
        assert loaded.data == board.data
        assert loaded.checkpoint(KEY) is not None
        assert loaded.checkpoint(KEY).ids == {"26.3", "26.2"}  # type: ignore[union-attr]
        assert fake.issues[0]["labels"] == [identity.LABEL]
        assert fake.issues[0]["title"] == dashboard.TITLE


def test_text_outside_the_dashboard_markers_is_preserved() -> None:
    board = support.golden_dashboard()
    with FakeGitHub() as fake:
        client = GitHub(support.REPO, support.TOKEN, fake.api_url)
        dashboard.save(client, board)
        fake.issues[0]["body"] = str(fake.issues[0]["body"]) + "\n## Notes from Hendrik\n\nDo not close this.\n"

        reloaded = dashboard.load(client)
        reloaded.data["updatedAt"] = "2026-09-18T00:00:00Z"
        dashboard.save(client, reloaded)

        body = str(fake.issues[0]["body"])
        assert body.endswith("## Notes from Hendrik\n\nDo not close this.\n")
        assert '"updatedAt": "2026-09-18T00:00:00Z"' in body
        assert body.count(dashboard.BEGIN) == 1


def test_a_concurrent_edit_refuses_the_write() -> None:
    board = support.golden_dashboard()
    with FakeGitHub() as fake:
        client = GitHub(support.REPO, support.TOKEN, fake.api_url)
        dashboard.save(client, board)
        loaded = dashboard.load(client)
        fake.issues[0]["body"] = "someone else rewrote this\n"
        writes_before = fake.writes

        with pytest.raises(Exception, match="changed during the scan"):
            dashboard.save(client, loaded)

        assert fake.writes == writes_before


def test_is_seen_uses_ids_then_the_floor() -> None:
    checkpoint = checkpoints.Checkpoint(
        seen=[("26.3", "2026-09-15T11:23:02+00:00")],
        floor="2026-01-01T00:00:00+00:00",
        at=FROZEN,
    )

    assert checkpoints.is_seen(checkpoint, "26.3", "2026-09-15T11:23:02+00:00") is True
    assert checkpoints.is_seen(checkpoint, "1.0", "2011-11-17T22:00:00+00:00") is True
    assert checkpoints.is_seen(checkpoint, "26.4", "2026-09-20T00:00:00+00:00") is False


def test_seed_marks_every_candidate_seen() -> None:
    entries = [("26.1", "2026-02-01T00:00:00+00:00"), ("26.3", "2026-09-15T00:00:00+00:00")]

    checkpoint = checkpoints.seed(entries, at=FROZEN)

    assert checkpoint.ids == {"26.1", "26.3"}
    assert checkpoint.seen[0][0] == "26.3"  # newest first
    assert checkpoint.floor is None
    assert all(checkpoints.is_seen(checkpoint, rev, released) for rev, released in entries)


def test_advance_never_duplicates_an_id() -> None:
    checkpoint = checkpoints.seed([("26.2", "2026-06-16T12:03:33+00:00")], at=FROZEN)

    advanced = checkpoints.advance(
        checkpoint,
        [("26.2", "2026-06-16T12:03:33+00:00"), ("26.3", "2026-09-15T11:23:02+00:00")],
        at=FROZEN,
    )

    assert [rev for rev, _ in advanced.seen] == ["26.3", "26.2"]


def test_a_checkpoint_round_trips_through_json() -> None:
    checkpoint = checkpoints.Checkpoint(seen=[("26.3", "2026-09-15T11:23:02+00:00")], floor=None, at=FROZEN)

    assert checkpoints.from_json(checkpoints.to_json(checkpoint)) == checkpoint
    assert checkpoints.from_json(None) is None
    assert checkpoints.from_json({"seen": ["26.3"], "floor": None, "at": FROZEN}) is not None


def test_the_body_never_exceeds_the_cap() -> None:
    base = datetime(2011, 11, 17, 22, 0, 0, tzinfo=UTC)
    entries = [(f"r{index}", (base + timedelta(days=index)).isoformat()) for index in range(5000)]
    board = support.golden_dashboard()
    board.source(KEY)["checkpoint"] = checkpoints.to_json(checkpoints.seed(entries, at=FROZEN))

    dashboard.cap(board)

    body = dashboard.render(board)
    trimmed = checkpoints.from_json(board.source(KEY)["checkpoint"])
    assert trimmed is not None
    kept = [rev for rev, _ in trimmed.seen]
    assert len(body) <= dashboard.MAX_BODY
    assert 0 < len(kept) < 5000
    assert kept == [f"r{index}" for index in range(4999, 4999 - len(kept), -1)]
    assert trimmed.floor == entries[4999 - len(kept)][1]
    assert checkpoints.is_seen(trimmed, "r0", entries[0][1]) is True
    assert checkpoints.is_seen(trimmed, "r4999", entries[4999][1]) is True


def test_an_unreadable_dashboard_exits_nine(run: object, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        harness.seed_issue(
            identity.render_marker(identity.DASHBOARD_MARKER)
            + "\n\n"
            + dashboard.BEGIN
            + "\n```json\n{not json at all\n```\n"
            + dashboard.END
            + "\n",
            title=dashboard.TITLE,
        )

        code, payload, stderr = harness.scan(run, "--bootstrap", "--publish")

        assert code == 9, stderr
        assert payload["ok"] is False
        assert "fix it by hand" in payload["error"]
        assert harness.fake.writes == 0
