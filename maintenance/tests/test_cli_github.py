"""The GitHub client the tracker issues will build on, against an in-process fake."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fake_github import FakeGitHub
from takaro_maint.exit_codes import TrackerError
from takaro_maint.github import GitHub, resolve_repo, resolve_token


@pytest.fixture
def fake() -> Any:
    with FakeGitHub() as server:
        yield server


@pytest.fixture
def client(fake: Any) -> GitHub:
    return GitHub("gettakaro/connectors", "token-for-tests", fake.api_url)


def test_the_token_comes_from_the_environment_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-the-environment")

    assert resolve_token() == "from-the-environment"


def test_an_explicit_token_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-the-environment")

    assert resolve_token("explicit") == "explicit"


def test_the_gh_cli_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("takaro_maint.github.shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        "takaro_maint.github.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "from-gh-cli\n", ""),
    )

    assert resolve_token() == "from-gh-cli"


def test_a_token_from_the_gh_cli_is_redacted_from_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from takaro_maint import output

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("takaro_maint.github.shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        "takaro_maint.github.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "from-gh-cli-token-value\n", ""),
    )
    GitHub("gettakaro/connectors", resolve_token(), "http://127.0.0.1:9")

    output.error("the token is from-gh-cli-token-value")

    assert "from-gh-cli-token-value" not in capsys.readouterr().err


def test_no_token_anywhere_exits_nine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("takaro_maint.github.shutil.which", lambda name: None)

    with pytest.raises(TrackerError) as raised:
        resolve_token()

    assert raised.value.code == 9


def test_the_repository_never_comes_from_a_ci_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/else")
    monkeypatch.setenv("TAKARO_MAINT_REPO", "gettakaro/connectors")

    assert resolve_repo(None, tmp_path) == "gettakaro/connectors"


def test_an_explicit_repository_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAKARO_MAINT_REPO", "gettakaro/connectors")

    assert resolve_repo("other/repo", tmp_path) == "other/repo"


def test_the_origin_remote_is_the_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TAKARO_MAINT_REPO", raising=False)
    monkeypatch.setattr(
        "takaro_maint.github.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "git@github.com:gettakaro/connectors.git\n", ""),
    )

    assert resolve_repo(None, tmp_path) == "gettakaro/connectors"


def test_every_request_carries_the_token(client: GitHub, fake: Any) -> None:
    fake.issues.append({"number": 1, "title": "one", "state": "open"})

    client.issue_get(1)

    assert fake.authorizations[-1] == "Bearer token-for-tests"


def test_issues_are_created_updated_and_closed(client: GitHub, fake: Any) -> None:
    created = client.issue_create("a title", "a body", ["connector-maintenance"])
    assert created["title"] == "a title"

    updated = client.issue_update(created["number"], title="a better title")
    assert updated["title"] == "a better title"

    closed = client.issue_close(created["number"])
    assert closed["state"] == "closed"
    assert closed["state_reason"] == "completed"


def test_pagination_follows_every_link(client: GitHub, fake: Any) -> None:
    fake.issues.extend({"number": n, "title": f"issue {n}", "state": "open"} for n in range(1, 6))

    issues = client.issues_list()

    assert [issue["number"] for issue in issues] == [1, 2, 3, 4, 5]


def test_pull_requests_are_excluded_from_the_issue_list(client: GitHub, fake: Any) -> None:
    fake.issues.extend(
        [
            {"number": 1, "title": "an issue", "state": "open"},
            {"number": 2, "title": "a pull request", "state": "open", "pull_request": {"url": "x"}},
        ]
    )

    assert [issue["number"] for issue in client.issues_list()] == [1]


def test_searching_issues_returns_the_items(client: GitHub, fake: Any) -> None:
    fake.issues.append({"number": 7, "title": "catalog-fabric", "state": "open"})

    assert [issue["number"] for issue in client.issues_search("catalog-fabric")] == [7]


def test_releases_and_assets_are_readable(client: GitHub, fake: Any) -> None:
    fake.releases.append({"id": 42, "tag_name": "minecraft-v0.1.1"})
    fake.assets[42] = [{"id": 1, "name": "takaro-minecraft-mod-fabric-26.2-0.1.1.jar"}]

    release = client.release_by_tag("minecraft-v0.1.1")
    assets = client.assets(release["id"])

    assert release["id"] == 42
    assert [asset["name"] for asset in assets] == ["takaro-minecraft-mod-fabric-26.2-0.1.1.jar"]


def test_a_missing_release_surfaces_as_a_tracker_error(client: GitHub) -> None:
    with pytest.raises(TrackerError) as raised:
        client.release_by_tag("nope")

    assert raised.value.code == 9


def test_deleting_an_asset_removes_it(client: GitHub, fake: Any) -> None:
    fake.releases.append({"id": 42, "tag_name": "minecraft-v0.1.1"})
    fake.assets[42] = [{"id": 1, "name": "old.jar"}, {"id": 2, "name": "new.jar"}]

    client.delete_asset(1)

    assert [asset["name"] for asset in client.assets(42)] == ["new.jar"]


def test_contents_are_readable_at_a_ref(client: GitHub, fake: Any) -> None:
    fake.contents["catalog/minecraft/game.json"] = "e30="

    payload = client.contents("catalog/minecraft/game.json", ref="main")

    assert payload["path"] == "catalog/minecraft/game.json"
    assert ("GET", "/repos/gettakaro/connectors/contents/catalog/minecraft/game.json?ref=main") in fake.requests


def test_fail_after_surfaces_as_a_tracker_error(client: GitHub, fake: Any) -> None:
    fake.fail_after(1)
    client.issue_create("first", "body")

    with pytest.raises(TrackerError) as raised:
        client.issue_create("second", "body")

    assert raised.value.code == 9


def test_fail_on_surfaces_as_a_tracker_error(client: GitHub, fake: Any) -> None:
    fake.fail_on(r"/issues")

    with pytest.raises(TrackerError):
        client.issues_list()


def test_an_unreachable_api_surfaces_as_a_tracker_error() -> None:
    client = GitHub("gettakaro/connectors", "token", "http://127.0.0.1:9")

    with pytest.raises(TrackerError) as raised:
        client.issues_list()

    assert raised.value.code == 9
