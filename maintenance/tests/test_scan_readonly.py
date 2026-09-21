"""Read-only is the default, and it means it: a scan that writes nothing at all.

Everything here runs the real command against the fake tracker and then asserts on the
fake's write counter and request log, not on an intention expressed in the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import test_scan_support as support
from takaro_maint import github, observations


def test_read_only_writes_nothing(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap")

        assert code == 0, stderr
        assert payload["ok"] is True
        assert payload["mode"] == "read-only"
        assert payload["bootstrap"] is True
        assert payload["repo"] == support.REPO
        assert payload["dashboard"] == {"issue": None, "created": False}
        assert payload["applied"] == []

        actions = [entry["action"] for entry in payload["plan"]]
        assert actions == ["create-dashboard", "bootstrap-checkpoint", "create-issue"]
        bootstrap = payload["plan"][1]
        assert bootstrap == {
            "action": "bootstrap-checkpoint",
            "source": support.SOURCE_KEY,
            "seeded": 5,  # the six fixture releases minus the head that gets an issue
            "heads": {"release": "26.3"},
        }
        assert payload["plan"][2]["identity"] == "provider=mojang component=minecraft branch=release rev=26.3"
        assert payload["plan"][2]["title"] == "Minecraft 26.3: new stable release needs a target"

        source = payload["sources"][support.SOURCE_KEY]
        assert source["status"] == "ok"
        assert source["history"] == "full"
        assert source["heads"] == {"release": "26.3"}
        assert source["checkpoint"] == {"before": None, "after": {"seen": 6, "floor": None}}

        assert harness.fake.writes == 0
        assert harness.fake.issues == []
        assert {method for method, _ in harness.fake.requests} == {"GET"}


def test_the_observation_validates_against_its_schema(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.frozen_clock(monkeypatch)
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap")

        assert code == 0, stderr
        assert len(payload["observations"]) == 1
        document = payload["observations"][0]
        assert observations.validate(document) == []
        assert document["rev"] == "26.3"
        assert document["kind"] == "game"
        assert document["branch"] == "release"
        assert document["source"] == {"game": "minecraft", "id": "mojang-meta"}
        assert document["observedAt"] == support.FROZEN_NOW
        facts = document["facts"]
        assert set(facts) == {"id", "type", "releaseTime", "manifestList", "manifest", "server", "javaMajor"}
        assert facts["releaseTime"] == "2026-09-15T11:23:02+00:00"
        assert facts["javaMajor"] == 25
        assert facts["server"]["sha1"] == "33680f5f2ac32864d6d7cf5e56a705fdb3e05f4c"
        assert facts["server"]["size"] == 62294556
        assert set(facts["manifest"]) == {"url", "sha1"}


def test_only_the_head_version_document_is_fetched(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six releases in the manifest, one per-version fetch: bootstrap files only the head."""
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, _, stderr = harness.scan(run, "--bootstrap")

        assert code == 0, stderr
        assert harness.requested(support.MANIFEST_PATH) == 1
        assert harness.requested(harness.served["26.3"]) == 1
        assert harness.requested(harness.served["26.2"]) == 0
        assert harness.requested(harness.served["26.1.2"]) == 0


def test_read_only_needs_no_ci_variables(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "GITHUB_REPOSITORY": "someone/else",
        "GITHUB_WORKSPACE": "/nonexistent",
        "GITHUB_SHA": "deadbeef",
        "RUNNER_TEMP": "/nonexistent",
        "CI": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TAKARO_MAINT_REPO", raising=False)

    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run, "--bootstrap")

        assert code == 0, stderr
        assert payload["repo"] == support.REPO
        assert harness.fake.writes == 0


def test_an_uninitialised_source_is_reported_not_an_error_in_read_only(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.scan(run)

        assert code == 0, stderr
        assert payload["sources"][support.SOURCE_KEY]["status"] == "uninitialized"
        assert payload["plan"] == [
            {"action": "create-dashboard"},
            {"action": "bootstrap-required", "source": support.SOURCE_KEY},
        ]
        assert payload["observations"] == []
        assert harness.fake.writes == 0
        assert harness.requested(support.MANIFEST_PATH) == 0


def test_no_token_exits_nine(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.rig(catalog_copy, monkeypatch) as harness:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(github.shutil, "which", lambda _name: None)

        code, payload, stderr = harness.scan(run, "--bootstrap")

        assert code == 9
        assert payload["ok"] is False
        assert "GH_TOKEN" in payload["error"]
        assert harness.fake.writes == 0
        assert harness.fake.requests == []
