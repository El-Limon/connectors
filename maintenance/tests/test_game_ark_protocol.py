"""ARK protocol level needs native evidence for each semantic capability."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from takaro_maint.games.ark import verify as ark_verify
from takaro_maint.verify.report import GAME_PROTOCOL_CHECKS, build_report, level_for


def _rows(status: str = "pass") -> list[dict[str, str]]:
    return [{"id": check_id, "status": status} for check_id in ("build", "startup", *GAME_PROTOCOL_CHECKS["ark"])]


def test_ark_protocol_level_requires_every_native_semantic_check() -> None:
    rows = _rows()
    assert level_for(rows, "ark") == "protocol"
    assert level_for(rows) == "startup"  # The Minecraft protocol ladder is unchanged.
    for required in GAME_PROTOCOL_CHECKS["ark"]:
        missing = [row for row in rows if row["id"] != required]
        assert level_for(missing, "ark") == "startup"
        skipped = [row | {"status": "skip"} if row["id"] == required else row for row in rows]
        assert level_for(skipped, "ark") == "startup"


def test_ark_report_keeps_artifact_and_runner_revisions_separate(tmp_path: Path) -> None:
    target = SimpleNamespace(
        game="ark",
        id="pinned-ark",
        fingerprint="a" * 64,
        record={
            "inputs": {},
            "runtime": {"container": {"image": "server", "tag": "pinned", "digest": "sha256:" + "b" * 64}},
        },
    )
    report = build_report(
        target=target,
        game_record={},
        manifest={"version": "1.0", "sourceRevision": "artifact-commit", "dirty": False, "artifacts": []},
        artifacts_dir=tmp_path,
        runtime={"readOnlyBase": {"readOnlyMount": True, "steamBuild": "21241282"}},
        checks=_rows(),
        started_at="2026-09-24T00:00:00Z",
        logs=[],
        repo_root=tmp_path,
    )
    assert report["source"] == {"repo": "unknown", "revision": "artifact-commit", "dirty": False}
    assert report["coverage"]["verificationRunner"] == {"revision": "unknown", "dirty": True}
    assert report["coverage"]["readOnlyBase"] == {"readOnlyMount": True, "steamBuild": "21241282"}


@pytest.mark.parametrize(
    ("ack", "exit_code", "expected"),
    [({}, 134, "pass"), ({"success": True}, 134, "fail"), ({}, 0, "fail"), ({}, 139, "fail")],
)
def test_ark_native_shutdown_requires_sidecar_ack_and_clean_native_exit(
    tmp_path: Path, ack: dict[str, object], exit_code: int, expected: str
) -> None:
    log = tmp_path / "server.log"
    log.write_text("", encoding="utf-8")

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> dict[str, object]:
            assert (name, args) == ("shutdown", {})
            with log.open("a", encoding="utf-8") as stream:
                stream.write(
                    "ARK_NATIVE_DIAG native-shutdown-synchronous-save-completed-before-ack\n"
                    "ARK_NATIVE_SHUTDOWN engine-exit-handled\n"
                )
            return ack

    class FakeRun:
        container = SimpleNamespace(wait_for_exit=lambda timeout: exit_code)
        server_log = log

        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "native-shutdown"

        def record(self, result: object) -> None:
            self.results.append(result)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_shutdown(run, FakeTakaro(), "", []))
    assert len(run.results) == 1
    assert run.results[0].id == "native-shutdown"
    assert run.results[0].status == expected, run.results[0].detail


def test_ark_native_shutdown_rejects_unacknowledged_request(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text("", encoding="utf-8")

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> None:
            del name, args
            raise TimeoutError("native response was not acknowledged")

    class FakeRun:
        container = SimpleNamespace(wait_for_exit=lambda timeout: 0)
        server_log = log

        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "native-shutdown"

        def record(self, result: object) -> None:
            self.results.append(result)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_shutdown(run, FakeTakaro(), "", []))
    assert run.results[0].status == "fail"
    assert "not acknowledged" in run.results[0].detail["problems"][0]


def test_ark_shutdown_rejects_unattributed_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "server.log"
    log.write_text(
        "ARK_NATIVE_DIAG native-shutdown-synchronous-save-completed-before-ack\n"
        "ARK_NATIVE_SHUTDOWN engine-exit-handled\n"
    )
    monkeypatch.setattr(ark_verify, "SHUTDOWN_MARKER_TIMEOUT", 0)

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> dict[str, object]:
            del name, args
            return {}

    class FakeRun:
        container = SimpleNamespace(wait_for_exit=lambda timeout: 134)
        server_log = log

        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "native-shutdown"

        def record(self, result: object) -> None:
            self.results.append(result)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_shutdown(run, FakeTakaro(), "", []))
    assert run.results[0].status == "fail"
    assert "native shutdown save completion marker is missing" in run.results[0].detail["problems"]
    assert "native shutdown exit completion marker is missing" in run.results[0].detail["problems"]


def test_ark_shutdown_log_rotation_fails_closed(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text("before shutdown\n")
    before = log.stat()
    log.rename(tmp_path / "server.log.1")
    log.write_text(
        "ARK_NATIVE_DIAG native-shutdown-synchronous-save-completed-before-ack\n"
        "ARK_NATIVE_SHUTDOWN engine-exit-handled\n"
    )
    with pytest.raises(RuntimeError, match="rotated or truncated"):
        ark_verify._fresh_shutdown_markers(log, before.st_dev, before.st_ino, before.st_size)


@pytest.mark.parametrize(("write_save", "expected"), [(True, "pass"), (False, "fail")])
def test_ark_readonly_shutdown_requires_fresh_owned_save(tmp_path: Path, write_save: bool, expected: str) -> None:
    log = tmp_path / "server.log"
    log.write_text("")
    save = tmp_path / "ShooterGame/Saved/SavedArks/TheIsland.ark"

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> dict[str, object]:
            assert (name, args) == ("shutdown", {})
            with log.open("a") as stream:
                stream.write(
                    "ARK_NATIVE_DIAG native-shutdown-synchronous-save-completed-before-ack\n"
                    "ARK_NATIVE_SHUTDOWN engine-exit-handled\n"
                )
            if write_save:
                save.parent.mkdir(parents=True)
                save.write_bytes(b"fresh-world")
            return {}

    class FakeRun:
        container = SimpleNamespace(wait_for_exit=lambda timeout: 134)
        server_log = log
        data_dir = tmp_path
        options = SimpleNamespace(ark_readonly_base=tmp_path / "base")

        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "native-shutdown"

        def record(self, result: object) -> None:
            self.results.append(result)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_shutdown(run, FakeTakaro(), "", []))
    assert run.results[0].status == expected, run.results[0].detail


@pytest.mark.parametrize(
    ("entities", "expected"),
    [([{"code": "/Game/Dino_C", "name": "Dino"}], "pass"), ([], "fail")],
)
def test_ark_entity_check_queries_real_generic_catalog(
    monkeypatch: pytest.MonkeyPatch, entities: list[dict[str, str]], expected: str
) -> None:
    monkeypatch.setattr(ark_verify, "start_sidecar", lambda run, fake: SimpleNamespace(name="sidecar"))

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> list[dict[str, str]]:
            assert (name, args) == ("listEntities", {})
            return entities

    class FakeRun:
        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "entities"

        def record(self, result: object) -> None:
            self.results.append(result)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_protocol(run, FakeTakaro(), lambda: True))
    assert len(run.results) == 1
    assert run.results[0].id == "entities"
    assert run.results[0].status == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"success": True, "rawResult": "", "errorMessage": None}, "pass"),
        ({"success": False, "rawResult": "", "errorMessage": "unhandled"}, "fail"),
        ({"success": True, "rawResult": None, "errorMessage": None}, "fail"),
    ],
)
def test_ark_console_requires_native_handled_result(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, object], expected: str
) -> None:
    monkeypatch.setattr(ark_verify, "start_sidecar", lambda run, fake: SimpleNamespace(name="sidecar"))

    class FakeTakaro:
        async def request(self, name: str, args: dict[str, object]) -> dict[str, object]:
            assert (name, args) == (
                "executeConsoleCommand",
                {"command": "GetAll ShooterPlayerState PlayerName"},
            )
            return result

    class FakeRun:
        def __init__(self) -> None:
            self.results: list[object] = []

        def wanted(self, name: str) -> bool:
            return name == "ark-console"

        def record(self, check: object) -> None:
            self.results.append(check)

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    run = FakeRun()
    asyncio.run(ark_verify.after_protocol(run, FakeTakaro(), lambda: True))
    assert len(run.results) == 1
    assert run.results[0].id == "ark-console"
    assert run.results[0].status == expected


def test_ark_shutdown_only_selection_starts_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        ark_verify,
        "start_sidecar",
        lambda run, fake: started.append(True) or SimpleNamespace(name="sidecar"),
    )

    class FakeRun:
        def wanted(self, name: str) -> bool:
            return name == "native-shutdown"

        def skip(self, name: str, reason: str) -> None:
            del name, reason

    asyncio.run(ark_verify.after_protocol(FakeRun(), object(), lambda: True))
    assert started == [True]
