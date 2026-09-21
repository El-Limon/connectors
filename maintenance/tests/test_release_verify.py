"""`release verify`: what is on the release page, re-downloaded and hashed, is the set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fake_github_releases import FakeReleases, serving
from test_release_assemble import CONNECTOR, REPO, VERSION, Inputs, assemble_default, build_inputs
from test_release_publish import TAG, publish, stable_draft


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    with serving(monkeypatch, REPO) as server:
        yield server


@pytest.fixture
def inputs(run: Any, catalog_copy: Path, tmp_path: Path) -> Inputs:
    return build_inputs(run, catalog_copy, tmp_path)


@pytest.fixture
def assembled(run: Any, inputs: Inputs, tmp_path: Path) -> tuple[Path, dict[str, Any], Inputs]:
    return assemble_default(run, inputs, tmp_path)


@pytest.fixture
def published(run: Any, fake: FakeReleases, assembled: tuple[Path, dict[str, Any], Inputs]) -> tuple[Path, Inputs]:
    directory, _, built = assembled
    stable_draft(fake, built.commit)
    code, _, err = publish(run, fake, directory, built.root, "--target-commit", built.commit)
    assert code == 0, err
    return directory, built


def verify(run: Any, fake: FakeReleases, root: Path, *extra: str) -> tuple[int, Any, str]:
    return run(
        "release",
        "verify",
        "--tag",
        TAG,
        "--connector",
        CONNECTOR,
        "--repo",
        REPO,
        "--api-url",
        fake.api_url,
        *extra,
        repo=root,
    )


def test_a_complete_release_verifies_and_writes_a_report(
    run: Any, fake: FakeReleases, published: tuple[Path, Inputs], tmp_path: Path
) -> None:
    directory, built = published
    out = tmp_path / "evidence"

    code, payload, err = verify(run, fake, built.root, "--out", str(out))

    assert code == 0, err
    assert payload["version"] == VERSION
    assert payload["draft"] is False
    assert payload["tagCommit"] == built.commit
    assert {asset["name"] for asset in payload["assets"]} == {path.name for path in directory.iterdir()}
    assert {asset["action"] for asset in payload["assets"]} == {"verified"}

    document = json.loads((out / f"release-verify-{TAG}.json").read_text())
    assert document["op"] == "release verify"
    assert document["assets"] == payload["assets"]


def test_a_missing_asset_exits_eight(run: Any, fake: FakeReleases, published: tuple[Path, Inputs]) -> None:
    _, built = published
    release = fake.release_for(TAG)
    gone = built.targets["fabric-26.1.2"]["file"]
    release["assets"] = [asset for asset in release["assets"] if asset["name"] != gone]

    code, payload, _ = verify(run, fake, built.root)

    assert code == 8
    assert payload["missing"] == [gone]


def test_an_asset_with_different_bytes_exits_seven(
    run: Any, fake: FakeReleases, published: tuple[Path, Inputs]
) -> None:
    _, built = published
    fake.corrupt_download(built.targets["fabric-26.2"]["file"])

    code, payload, _ = verify(run, fake, built.root)

    assert code == 7
    assert payload["asset"] == built.targets["fabric-26.2"]["file"]


def test_a_draft_release_is_found_through_the_listing(
    run: Any, fake: FakeReleases, assembled: tuple[Path, dict[str, Any], Inputs]
) -> None:
    directory, _, built = assembled
    stable_draft(fake, built.commit)
    assert publish(run, fake, directory, built.root, "--target-commit", built.commit, "--no-finalize")[0] == 0

    code, payload, err = verify(run, fake, built.root)

    assert code == 0, err
    assert payload["draft"] is True
    assert ("GET", f"/repos/{REPO}/releases/tags/{TAG}") in fake.requests


def test_expect_dir_must_match_byte_for_byte(
    run: Any, fake: FakeReleases, published: tuple[Path, Inputs], tmp_path: Path
) -> None:
    directory, built = published
    assert verify(run, fake, built.root, "--expect", str(directory))[0] == 0

    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for path in directory.iterdir():
        (tampered / path.name).write_bytes(path.read_bytes())
    (tampered / built.targets["fabric-26.2"]["file"]).write_bytes(b"a local file that is not it")

    code, payload, _ = verify(run, fake, built.root, "--expect", str(tampered))

    assert code == 7
    assert payload["asset"] == built.targets["fabric-26.2"]["file"]


def test_extra_assets_are_reported_not_fatal(run: Any, fake: FakeReleases, published: tuple[Path, Inputs]) -> None:
    _, built = published
    fake.add_asset(fake.release_for(TAG), "someone-elses-note.txt", b"hello")

    code, payload, err = verify(run, fake, built.root)

    assert code == 0, err
    assert payload["unexpectedAssets"] == ["someone-elses-note.txt"]


def test_a_tag_with_no_release_exits_eight(run: Any, fake: FakeReleases, catalog_copy: Path) -> None:
    code, payload, _ = verify(run, fake, catalog_copy)

    assert code == 8
    assert payload["tag"] == TAG
