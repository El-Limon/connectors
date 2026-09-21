"""Steam discovery: running ``app_info_print`` and reading what it published.

The tool is replaced by ``fake_steamcmd.py`` — a real subprocess, invoked through the real
``TAKARO_MAINT_STEAMCMD`` override — so what is exercised here is the command line, the
retry, the log and the parse, not a mocked return value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import fake_steamcmd
from takaro_maint.exit_codes import UPSTREAM, MaintError
from takaro_maint.steam import steamcmd

APP = 294420


@pytest.fixture
def steam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The fake steamcmd, serving a mutable copy of the recorded 294420 document."""

    class Rig:
        def __init__(self) -> None:
            self.root = tmp_path / "steam-root"
            self.log = tmp_path / "steamcmd-argv.jsonl"
            self.tool_log = tmp_path / "logs" / f"app_info-{APP}.log"
            self.document = fake_steamcmd.recorded(APP)
            self.root.mkdir(parents=True, exist_ok=True)
            self.serve()

        def serve(self) -> None:
            fake_steamcmd.serve(self.root, APP, self.document)

        def env(self, **extra: str) -> None:
            for name, value in fake_steamcmd.environment(self.root, self.log, **extra).items():
                monkeypatch.setenv(name, value)

        @property
        def calls(self) -> list[list[str]]:
            return fake_steamcmd.argv_log(self.log)

    rig = Rig()
    rig.env()
    return rig


def test_the_recorded_app_is_read_through_the_real_command_line(steam: Any) -> None:
    info = steamcmd.app_info(APP, log=steam.tool_log)

    assert info.app == APP
    assert info.name == "7 Days to Die Dedicated Server"
    assert info.change_number == 39026857
    assert info.private_branches is True
    assert info.branches["public"].buildid == 24994542
    assert info.branches["public"].timeupdated == 1788196235
    assert info.depots["294422"].manifests["public"].gid == "1633674551820196085"
    assert info.depots["294422"].oslist == "linux"

    assert steam.calls == [
        [
            str(Path(fake_steamcmd.__file__).resolve()),
            "+login",
            "anonymous",
            "+app_info_update",
            "1",
            "+app_info_print",
            str(APP),
            "+quit",
        ]
    ], "the argv is the documented read-only, anonymous invocation"


def test_only_pinnable_depots_are_reported(steam: Any) -> None:
    """Shared depots and the bare scalars beside them are not content this can pin."""
    info = steamcmd.app_info(APP, log=steam.tool_log)

    assert sorted(info.depots) == ["294421", "294422"]
    assert "branches" not in info.depots
    assert "overridescddb" not in info.depots


def test_a_branch_without_timeupdated_falls_back_to_the_build_time(steam: Any) -> None:
    info = steamcmd.app_info(APP, log=steam.tool_log)

    old = info.branches["alpha12.5"]
    assert old.timeupdated is None
    assert old.published_at == old.timebuildupdated == 1440323524
    assert steamcmd.iso(old.published_at or 0) == "2015-08-23T09:52:04Z"


def test_a_protected_branch_is_reported_as_one(steam: Any) -> None:
    fake_steamcmd.add_branch(
        steam.document, "latest_experimental", 25200000, {"294422": "3000000000000000001"}, pwdrequired=True
    )
    steam.serve()

    info = steamcmd.app_info(APP, log=steam.tool_log)

    branch = info.branches["latest_experimental"]
    assert branch.pwdrequired is True
    assert "latest_experimental" in info.depots["294422"].encrypted
    assert "latest_experimental" not in info.depots["294422"].manifests


# -- the documented non-TTY truncation -----------------------------------------
def test_a_truncated_first_answer_is_retried_exactly_once(steam: Any) -> None:
    steam.env(FAKE_STEAMCMD_TRUNCATE="1")

    info = steamcmd.app_info(APP, log=steam.tool_log)

    assert info.branches["public"].buildid == 24994542
    assert len(steam.calls) == 2


def test_two_truncated_answers_are_an_upstream_failure_and_not_a_third_attempt(steam: Any) -> None:
    steam.env(FAKE_STEAMCMD_TRUNCATE="5")

    with pytest.raises(MaintError) as raised:
        steamcmd.app_info(APP, log=steam.tool_log)

    assert raised.value.code == UPSTREAM
    assert "after 2 attempts" in raised.value.message
    assert len(steam.calls) == 2, "a broken upstream is reported, not hammered"


# -- the other ways the tool fails ---------------------------------------------
def test_a_failing_steamcmd_is_an_upstream_failure_with_its_log(steam: Any) -> None:
    steam.env(FAKE_STEAMCMD_FAIL="1")

    with pytest.raises(MaintError) as raised:
        steamcmd.app_info(APP, log=steam.tool_log)

    assert raised.value.code == UPSTREAM
    assert "exited 1" in raised.value.message
    assert steam.tool_log.name in raised.value.message
    assert "Connection failed" in steam.tool_log.read_text(encoding="utf-8")
    assert len(steam.calls) == 1, "a non-zero exit is not the retryable failure"


def test_a_missing_tool_names_the_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAKARO_MAINT_STEAMCMD", str(tmp_path / "there-is-no-steamcmd-here"))

    with pytest.raises(MaintError) as raised:
        steamcmd.app_info(APP)

    assert raised.value.code == UPSTREAM
    assert "TAKARO_MAINT_STEAMCMD" in raised.value.message


def test_a_hanging_tool_times_out(steam: Any) -> None:
    steam.env(FAKE_STEAMCMD_HANG="5")

    with pytest.raises(MaintError) as raised:
        steamcmd.app_info(APP, timeout=0.5, log=steam.tool_log)

    assert raised.value.code == UPSTREAM
    assert "timed out" in raised.value.message


def test_an_unknown_app_prints_no_branch_data(steam: Any) -> None:
    with pytest.raises(MaintError) as raised:
        steamcmd.app_info(999999, log=steam.tool_log.parent / "app_info-999999.log")

    assert raised.value.code == UPSTREAM
    assert "999999" in raised.value.message


# -- what the run leaves behind ------------------------------------------------
def test_the_log_records_the_command_and_its_exit(steam: Any) -> None:
    steamcmd.app_info(APP, log=steam.tool_log)

    recorded = steam.tool_log.read_text(encoding="utf-8")
    assert "+app_info_print 294420" in recorded
    assert "-- exit 0" in recorded


def test_a_secret_in_the_environment_never_reaches_the_log(
    steam: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No credential is an argument of steamcmd, but the log is redacted regardless."""
    monkeypatch.setenv("TAKARO_MAINT_STEAM_BRANCH_PASSWORD__294420__X", "hunter2-secret-value")
    root = tmp_path / "leaky"
    document = fake_steamcmd.recorded(APP)
    fake_steamcmd.add_branch(document, "leak", 1, {"294422": "1"}, description="hunter2-secret-value")
    fake_steamcmd.serve(root, APP, document)
    monkeypatch.setenv(fake_steamcmd.ROOT_ENV, str(root))

    steamcmd.app_info(APP, log=steam.tool_log)

    assert "hunter2-secret-value" not in steam.tool_log.read_text(encoding="utf-8")


# -- the revision digest -------------------------------------------------------
def test_the_manifest_digest_binds_the_whole_depot_set() -> None:
    one = steamcmd.manifest_digest({"294422": "1633674551820196085"})
    two = steamcmd.manifest_digest({"294422": "1633674551820196085", "294421": "1646211645575803407"})

    assert len(one) == 8 and one != two
    assert one == steamcmd.manifest_digest({"294422": "1633674551820196085"}), "stable across calls"
    assert two == steamcmd.manifest_digest({"294421": "1646211645575803407", "294422": "1633674551820196085"}), (
        "and independent of the order the depots were listed in"
    )
    assert steamcmd.manifest_digest({"294422": "2089214172134382404"}) != one


def test_the_fake_serves_a_document_a_test_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture is bent in memory and written back through the real VDF writer."""
    root = tmp_path / "root"
    document = fake_steamcmd.recorded(APP)
    fake_steamcmd.move_head(document, "public", 25100000, {"294422": "2089214172134382404"}, timeupdated=1788300000)
    fake_steamcmd.set_private_branches(document, False)
    fake_steamcmd.remove_branch(document, "alpha12.5")
    fake_steamcmd.serve(root, APP, document)
    for name, value in fake_steamcmd.environment(root, tmp_path / "argv.jsonl").items():
        monkeypatch.setenv(name, value)

    info = steamcmd.app_info(APP)

    assert info.branches["public"].buildid == 25100000
    assert info.branches["public"].timeupdated == 1788300000
    assert info.depots["294422"].manifests["public"].gid == "2089214172134382404"
    assert info.private_branches is False
    assert "alpha12.5" not in info.branches
    assert json.dumps(sorted(info.branches)) == '["alpha21.2", "public", "v3.1.0", "v3.2.0"]'


# -- `steam branches` and `steam pin --metadata` -------------------------------
import fake_depotdownloader as fake_dd  # noqa: E402

GAME = "7d2d"


@pytest.fixture
def repo(tmp_path: Path, steam: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository copy with the DepotDownloader stand-in wired up beside the steamcmd one."""
    root = fake_dd.make_repo(tmp_path)
    for name, value in fake_dd.environment(tmp_path, tmp_path / "dd-argv.jsonl").items():
        monkeypatch.setenv(name, value)
    return root


def watch_7d2d(root: Path, watch: dict[str, Any]) -> None:
    """Give the copied 7 Days to Die record a Steam watch block."""
    path = root / "catalog" / GAME / "game.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["sources"]["steam"]["watch"] = watch
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def test_steam_branches_lists_every_branch_the_app_publishes(run: Any, steam: Any, repo: Path) -> None:
    fake_steamcmd.add_branch(steam.document, "beta_test", 25300000, {"294422": "3000000000000000001"})
    steam.serve()

    code, payload, err = run("steam", "branches", "--game", GAME, "--app", "294420", repo=repo)

    assert code == 0, err
    assert payload["app"] == 294420
    assert payload["name"] == "7 Days to Die Dedicated Server"
    assert payload["privateBranches"] is True
    assert payload["changeNumber"] == 39026857
    labels = [row["label"] for row in payload["branches"]]
    assert labels == sorted(labels), "sorted by label, so two runs are diffable"
    assert "beta_test" in labels

    public = next(row for row in payload["branches"] if row["label"] == "public")
    assert public["buildid"] == 24994542
    assert public["timeupdated"] == "2026-08-31T17:10:35Z"
    assert public["manifests"]["294422"]["gid"] == "1633674551820196085"
    assert public["pwdrequired"] is False
    assert [depot["id"] for depot in payload["depots"]] == ["294421", "294422"]


def test_steam_branches_classifies_against_the_watch_block(run: Any, steam: Any, repo: Path) -> None:
    fake_steamcmd.add_branch(
        steam.document, "latest_experimental", 25200000, {"294422": "3000000000000000001"}, pwdrequired=True
    )
    fake_steamcmd.add_branch(steam.document, "beta_test", 25300000, {"294422": "4000000000000000001"})
    steam.serve()
    watch_7d2d(
        repo,
        {
            "kind": "game",
            "component": GAME,
            "app": 294420,
            "os": "linux",
            "depots": ["294422"],
            "channels": {
                "public": {"branch": "public"},
                "latest_experimental": {"branch": "experimental", "enabled": False},
            },
            "knownBranches": ["regex:^v[0-9]+(\\.[0-9]+)*$", "regex:^alpha[0-9]+(\\.[0-9]+)*$"],
        },
    )

    code, payload, err = run("steam", "branches", "--game", GAME, repo=repo)

    assert code == 0, err
    by_label = {row["label"]: row for row in payload["branches"]}
    assert by_label["public"]["classification"] == "watched"
    assert by_label["public"]["branch"] == "public"
    assert by_label["latest_experimental"]["classification"] == "declared"
    assert by_label["latest_experimental"]["pwdrequired"] is True
    assert by_label["latest_experimental"]["manifests"] is None
    assert by_label["latest_experimental"]["encrypted"] == ["294422"]
    assert by_label["v3.2.0"]["classification"] == "known"
    assert by_label["alpha12.5"]["classification"] == "known"
    assert by_label["beta_test"]["classification"] == "unfamiliar"


def test_steam_branches_without_an_app_is_a_usage_error(run: Any, steam: Any, repo: Path) -> None:
    code, payload, _ = run("steam", "branches", "--game", GAME, repo=repo)

    assert code == 2
    assert "--app" in json.dumps(payload)


def test_steam_branches_reports_a_failing_tool_as_upstream(run: Any, steam: Any, repo: Path) -> None:
    steam.env(FAKE_STEAMCMD_FAIL="1")

    code, payload, _ = run("steam", "branches", "--game", GAME, "--app", "294420", repo=repo)

    assert code == 4
    assert "steamcmd" in json.dumps(payload)


def test_steam_pin_metadata_fills_the_buildid_from_app_info(run: Any, steam: Any, repo: Path) -> None:
    """The build id is in the metadata and nowhere else: DepotDownloader never sees one."""
    fake_steamcmd.set_manifest(steam.document, "294422", "public", fake_dd.HEAD_MANIFEST)
    steam.serve()

    code, payload, err = run("steam", "pin", "--game", GAME, "--target", fake_dd.TARGET_ID, "--metadata", repo=repo)

    assert code == 0, err
    assert payload["metadata"] == {
        "buildid": 24994542,
        "timeupdated": "2026-08-31T17:10:35Z",
        "description": None,
        "changeNumber": 39026857,
    }
    assert payload["buildid"] == 24994542
    assert payload["snippet"]["buildid"] == 24994542
    assert payload["depots"]["294422"]["manifest"] == fake_dd.HEAD_MANIFEST


def test_a_publish_in_flight_is_a_retry_not_a_record(run: Any, steam: Any, repo: Path) -> None:
    """The metadata and the depot are read a moment apart; disagreeing means retry."""
    code, payload, _ = run("steam", "pin", "--game", GAME, "--target", fake_dd.TARGET_ID, "--metadata", repo=repo)

    assert code == 4
    message = json.dumps(payload)
    assert "disagree" in message
    assert "a publish is in flight" in message


def test_metadata_and_an_explicit_buildid_are_a_usage_error(run: Any, steam: Any, repo: Path) -> None:
    code, payload, _ = run(
        "steam", "pin", "--game", GAME, "--target", fake_dd.TARGET_ID, "--metadata", "--buildid", "1", repo=repo
    )

    assert code == 2
    assert "--metadata" in json.dumps(payload)


def test_steam_pin_without_metadata_is_unchanged(run: Any, steam: Any, repo: Path) -> None:
    """The existing behaviour is untouched: no app_info call, no metadata block."""
    code, payload, err = run("steam", "pin", "--game", GAME, "--target", fake_dd.TARGET_ID, repo=repo)

    assert code == 0, err
    assert payload["metadata"] is None
    assert steam.calls == [], "steamcmd is not consulted unless --metadata asks for it"
