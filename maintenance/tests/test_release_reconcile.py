"""What the latest stable release proves, and what it only appears to prove.

These go through ``latest_stable`` and ``verdict`` directly rather than through the command,
because the interesting cases are the shapes a release can take — a draft, a prerelease, a
legacy set with no record, a record that belongs to another tag — and staging each of them as
a whole lifecycle run would say less about the rule and more about the rig.

The rig itself is ``test_lifecycle``'s: the same composite tracker, the same release builder,
so a release these tests call complete is the same bytes the end-to-end scenario closes an
issue on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import test_lifecycle as lifecycle_tests
import test_scan_support as support
from takaro_maint import github as github_module
from takaro_maint.publish.release_client import ReleaseClient
from takaro_maint.tracker import lifecycle
from takaro_maint.tracker.release_reconcile import latest_stable, verdict

CONNECTOR = "minecraft"
VERSION = "0.2.0"
TAG = f"{CONNECTOR}-v{VERSION}"


def client_for(harness: lifecycle_tests.Rig) -> ReleaseClient:
    return ReleaseClient(github_module.GitHub(support.REPO, support.TOKEN, harness.front.api_url))


def main_target(root: Path, *, revision: str = "26.3", required: str | None = None) -> lifecycle.MainTarget:
    record = lifecycle_tests.target_for(root, revision, status="maintained")
    if required is not None:
        record["verification"]["required"] = required
    return lifecycle.MainTarget(
        id=str(record["id"]),
        status="maintained",
        revision=revision,
        platform=str(record["platform"]),
        record=record,
        ref="main",
    )


def test_latest_stable_ignores_drafts_prereleases_and_other_connectors(
    catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        releases = harness.front.releases
        published = releases.add_release("minecraft-v0.1.1")
        published["published_at"] = "2026-09-16T09:58:21Z"
        releases.add_release("minecraft-v0.1.2", draft=True)
        releases.add_release("minecraft-dev", prerelease=True)
        seven = releases.add_release("7d2d-v0.1.6")
        seven["published_at"] = "2026-09-20T10:00:00Z"

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert (facts.tag, facts.version) == ("minecraft-v0.1.1", "0.1.1")

        # A newer stable release of the same connector wins on published_at.
        newer = releases.add_release("minecraft-v0.2.0")
        newer["published_at"] = "2026-09-20T10:00:00Z"
        assert latest_stable(client_for(harness), CONNECTOR).tag == "minecraft-v0.2.0"  # type: ignore[union-attr]


def test_no_stable_release_is_none(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        harness.front.releases.add_release("minecraft-v0.9.0", draft=True)

        assert latest_stable(client_for(harness), CONNECTOR) is None

        target = main_target(catalog_copy)
        answer = verdict(None, target, connector=CONNECTOR)
        assert (answer.ok, answer.reasons) == (False, ["no stable release for minecraft yet"])


def test_a_release_without_a_record_or_sums_is_a_note(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape the connector's own 0.1.1 release has today: three jars and nothing else."""
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        release = harness.front.releases.add_release("minecraft-v0.1.1")
        release["published_at"] = "2026-09-16T09:58:21Z"
        for platform in ("fabric", "neoforge", "paper"):
            harness.front.releases.add_asset(release, f"takaro-{platform}-0.1.1.jar", b"legacy jar\n")

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert facts.note == "minecraft-v0.1.1 carries no compatibility record"
        assert facts.record is None
        answer = verdict(facts, main_target(catalog_copy), connector=CONNECTOR)
        assert (answer.ok, answer.reasons) == (False, ["minecraft-v0.1.1 carries no compatibility record"])


def test_a_release_with_a_record_but_no_checksums_is_a_note(
    catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record})
        release = harness.front.releases.release_for(TAG)
        assert release is not None
        release["assets"] = [asset for asset in release["assets"] if asset["name"] != "SHA256SUMS"]

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert facts.note == f"{TAG} carries no SHA256SUMS"


def test_a_record_that_disagrees_with_sha256sums_is_a_note(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record})
        record_name = f"takaro-{CONNECTOR}-{VERSION}.compat.json"
        harness.front.releases.corrupt_download(record_name)

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert facts.note == f"{record_name} does not match SHA256SUMS"


def test_a_record_for_another_tag_or_repo_is_a_note(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record})
        release = harness.front.releases.release_for(TAG)
        assert release is not None
        release["tag_name"] = "minecraft-v0.2.1"

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert facts.note == (f"takaro-{CONNECTOR}-{VERSION}.compat.json describes tag {TAG}, not minecraft-v0.2.1")


def test_an_invalid_record_is_a_note(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record that does not validate is reported, never guessed at."""
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record})
        record_name = f"takaro-{CONNECTOR}-{VERSION}.compat.json"
        release = harness.front.releases.release_for(TAG)
        assert release is not None
        broken = json.dumps({"schemaVersion": 1, "kind": "compat-record"}).encode("utf-8")
        _replace_asset(harness, release, record_name, broken)
        _relist_sums(harness, release)

        facts = latest_stable(client_for(harness), CONNECTOR)

        assert facts is not None
        assert facts.note is not None
        assert facts.note.startswith(f"{record_name} is not a valid compatibility record: ")


def _replace_asset(harness: lifecycle_tests.Rig, release: dict[str, Any], name: str, payload: bytes) -> None:
    release["assets"] = [asset for asset in release["assets"] if asset["name"] != name]
    harness.front.releases.add_asset(release, name, payload)


def _relist_sums(harness: lifecycle_tests.Rig, release: dict[str, Any]) -> None:
    """Rewrite SHA256SUMS over whatever the release now holds, so only one thing is wrong."""
    import hashlib

    blobs = harness.front.releases.blobs
    sums = {
        str(asset["name"]): hashlib.sha256(blobs[asset["id"]]).hexdigest()
        for asset in release["assets"]
        if asset["name"] != "SHA256SUMS"
    }
    payload = "".join(f"{sums[name]}  {name}\n" for name in sorted(sums)).encode("utf-8")
    _replace_asset(harness, release, "SHA256SUMS", payload)


def test_a_complete_target_passes_without_downloading_artifacts(
    catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record})
        mark = len(harness.front.releases.requests)

        facts = latest_stable(client_for(harness), CONNECTOR)
        answer = verdict(facts, target, connector=CONNECTOR)

        assert (answer.ok, answer.reasons) == (True, [])
        assert answer.release is not None
        assert answer.release["tag"] == TAG
        assert [item["name"] for item in answer.release["artifacts"]] == [
            lifecycle_tests._artifact_names(target.record, VERSION)[0][1]
        ]
        downloaded = _downloaded_names(harness, mark)
        assert downloaded == {"SHA256SUMS", f"takaro-{CONNECTOR}-{VERSION}.compat.json"}


def _downloaded_names(harness: lifecycle_tests.Rig, mark: int) -> set[str]:
    """The assets whose bytes were actually fetched, by name."""
    ids_by_name = {
        int(asset["id"]): str(asset["name"])
        for release in harness.front.releases.releases
        for asset in release["assets"]
    }
    names: set[str] = set()
    for _, path in harness.front.releases.requests[mark:]:
        _, _, tail = path.partition("/releases/assets/")
        if tail.isdigit():
            names.add(ids_by_name[int(tail)])
    return names


@pytest.mark.parametrize("tamper", lifecycle_tests.TAMPERS)
def test_each_mismatch_is_named(tamper: str, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {
        "drop-target": f"{TAG} does not list fabric-26.3",
        "wrong-sum": "SHA256SUMS disagrees for ",
        "missing-asset": "asset missing: ",
        "underlevel": "fabric-26.3 verified only to startup, requires protocol",
        "candidate": f"{TAG} ships fabric-26.3 as candidate; promote it to maintained",
        "no-record": f"{TAG} carries no compatibility record",
        "stale-fingerprint": f"{TAG} ships fabric-26.3 with fingerprint {'0' * 16}, main has ",
    }[tamper]
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy)
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record}, tamper=tamper)

        answer = verdict(latest_stable(client_for(harness), CONNECTOR), target, connector=CONNECTOR)

        assert answer.ok is False
        assert any(reason.startswith(expected) or expected in reason for reason in answer.reasons), answer.reasons


def test_the_verdict_reads_required_from_the_main_record(catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``required`` is the catalog's demand, not the release's claim about itself."""
    with lifecycle_tests.lifecycle_rig(catalog_copy, monkeypatch) as harness:
        target = main_target(catalog_copy, required="startup")
        lifecycle_tests.stable_release(harness, TAG, VERSION, {target.id: target.record}, executed="startup")

        answer = verdict(latest_stable(client_for(harness), CONNECTOR), target, connector=CONNECTOR)

        assert (answer.ok, answer.reasons) == (True, [])
