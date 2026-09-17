"""`verify`: the fake Takaro speaks the real protocol, and the runner never opens a port."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import websockets

from conftest import REPO_ROOT, make_jar, sha256

PROTOCOL = json.loads((REPO_ROOT / "games/7d2d/tests/fixtures/generic-protocol.json").read_text())


# --------------------------------------------------------------------------- protocol


def function_fixture(name: str) -> dict[str, Any]:
    return next(entry for entry in PROTOCOL["functions"] if entry["name"] == name)


async def connector_client(url: str, answers: dict[str, Any], seen: list[dict[str, Any]]) -> None:
    """Play the connector side of the protocol against the fake."""
    async with websockets.connect(url) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "identify",
                    "payload": {"identityToken": "an-identity", "registrationToken": "a-registration-token"},
                }
            )
        )
        async for raw in socket:
            frame = json.loads(raw)
            seen.append(frame)
            if frame.get("type") == "identifyResponse":
                continue
            if frame.get("type") == "request":
                action = frame["payload"]["action"]
                await socket.send(
                    json.dumps(
                        {
                            "type": "response",
                            "requestId": frame["requestId"],
                            "payload": answers.get(action),
                        }
                    )
                )


def test_the_fake_speaks_the_pinned_wire_envelope(tmp_path: Path) -> None:
    from takaro_maint.verify.fake_takaro import FakeTakaro

    seen: list[dict[str, Any]] = []
    answers = {entry["name"]: entry["response"]["payload"] for entry in PROTOCOL["functions"]}
    results: dict[str, Any] = {}

    async def scenario() -> None:
        fake = FakeTakaro(host="127.0.0.1", log_path=tmp_path / "fake-takaro.log")
        await fake.start()
        client = asyncio.create_task(connector_client(fake.url, answers, seen))
        identify = await fake.wait_for_identify(10)
        results["identify"] = identify
        results["gameServerId"] = fake.game_server_id
        results["players"] = await fake.request("getPlayers", {})
        results["items"] = await fake.request("listItems", {})
        results["reachability"] = await fake.request("testReachability", {})
        results["ping"] = await fake.ping(timeout=5)
        client.cancel()
        await fake.stop()

    asyncio.run(scenario())

    assert set(results["identify"]) == {"identityToken", "registrationToken"}
    identify_response = next(frame for frame in seen if frame["type"] == "identifyResponse")
    assert identify_response["payload"]["status"] == "authenticated"
    assert identify_response["payload"]["gameServerId"] == results["gameServerId"]

    request = next(frame for frame in seen if frame.get("type") == "request")
    envelope = PROTOCOL["wireEnvelope"]["request"]
    assert set(request) == set(envelope)
    assert set(request["payload"]) == set(envelope["payload"])
    assert isinstance(request["payload"]["args"], str)
    assert json.loads(request["payload"]["args"]) == {}

    assert results["players"] == function_fixture("getPlayers")["response"]["payload"]
    assert results["items"] == function_fixture("listItems")["response"]["payload"]
    assert results["reachability"] == {"connectable": True}
    assert results["ping"] >= 0


def test_every_frame_is_logged_as_jsonl(tmp_path: Path) -> None:
    from takaro_maint.verify.fake_takaro import FakeTakaro

    log = tmp_path / "fake-takaro.log"

    async def scenario() -> None:
        fake = FakeTakaro(host="127.0.0.1", log_path=log)
        await fake.start()
        client = asyncio.create_task(connector_client(fake.url, {"getPlayers": []}, []))
        await fake.wait_for_identify(10)
        await fake.request("getPlayers", {})
        client.cancel()
        await fake.stop()

    asyncio.run(scenario())

    lines = [json.loads(line) for line in log.read_text().splitlines()]
    directions = [line["direction"] for line in lines]
    kinds = [line["frame"]["type"] for line in lines]
    assert directions[:2] == ["in", "out"]
    assert kinds[:2] == ["identify", "identifyResponse"]
    assert "request" in kinds and "response" in kinds


def test_game_events_are_recorded(tmp_path: Path) -> None:
    from takaro_maint.verify.fake_takaro import FakeTakaro

    event = PROTOCOL["events"][0]["message"]
    recorded: dict[str, Any] = {}

    async def scenario() -> None:
        fake = FakeTakaro(host="127.0.0.1", log_path=tmp_path / "log.jsonl")
        port = await fake.start()
        async with websockets.connect(f"ws://127.0.0.1:{port}/") as socket:
            await socket.send(json.dumps({"type": "identify", "payload": {}}))
            await socket.recv()
            await socket.send(json.dumps(event))
            await asyncio.sleep(0.2)
        recorded["events"] = list(fake.events)
        await fake.stop()

    asyncio.run(scenario())

    assert recorded["events"] == [event["payload"]]


# --------------------------------------------------------------------------- checks


def test_the_catalogue_check_rejects_registry_ids() -> None:
    from takaro_maint.verify.checks import _catalogue_problems

    problems, detail = _catalogue_problems(
        [{"code": "minecraft:diamond_sword", "name": "minecraft:diamond_sword"}],
        ("minecraft:diamond_sword", "Diamond Sword"),
    )

    assert any("name equals code" in problem for problem in problems)
    assert detail["count"] == 1


def test_the_catalogue_check_rejects_translation_keys() -> None:
    from takaro_maint.verify.checks import _catalogue_problems

    problems, _ = _catalogue_problems(
        [{"code": "minecraft:zombie", "name": "entity.minecraft.zombie"}], ("minecraft:zombie", "Zombie")
    )

    assert any("translation key" in problem for problem in problems)


def test_the_catalogue_check_accepts_display_names() -> None:
    from takaro_maint.verify.checks import _catalogue_problems

    problems, detail = _catalogue_problems(
        [
            {"code": "minecraft:diamond_sword", "name": "Diamond Sword"},
            {"code": "minecraft:stone", "name": "Stone"},
        ],
        ("minecraft:diamond_sword", "Diamond Sword"),
    )

    assert problems == []
    assert detail["spotCheck"]["actual"] == "Diamond Sword"


def test_an_empty_catalogue_fails() -> None:
    from takaro_maint.verify.checks import _catalogue_problems

    problems, _ = _catalogue_problems([], ("minecraft:diamond_sword", "Diamond Sword"))

    assert problems


def test_the_level_is_the_highest_fully_passed_class() -> None:
    from takaro_maint.verify.report import level_for

    all_pass = [
        {"id": name, "status": "pass"}
        for name in (
            "build",
            "startup",
            "connector-load",
            "identify",
            "heartbeat",
            "players",
            "catalog-items",
            "catalog-entities",
            "console",
            "shutdown",
        )
    ]
    assert level_for(all_pass) == "protocol"

    no_console = [row if row["id"] != "console" else {"id": "console", "status": "fail"} for row in all_pass]
    assert level_for(no_console) == "startup"

    no_startup = [row if row["id"] != "startup" else {"id": "startup", "status": "fail"} for row in all_pass]
    assert level_for(no_startup) == "contract"

    assert level_for([{"id": "build", "status": "fail"}]) == "none"


def test_an_unobserved_class_is_not_reported_as_reached() -> None:
    from takaro_maint.verify.report import level_for, outcome_for

    # `verify --checks players`: one check ran, everything the level names did not.
    partial = [
        {"id": name, "status": "skip" if name != "players" else "pass"}
        for name in ("build", "startup", "players", "console")
    ]
    assert level_for(partial) == "none"
    assert outcome_for(partial) == "pass"


def test_a_run_that_observed_nothing_is_not_a_pass() -> None:
    from takaro_maint.verify.report import level_for, outcome_for

    skipped = [{"id": name, "status": "skip"} for name in ("build", "startup", "console")]
    assert level_for(skipped) == "none"
    assert outcome_for(skipped) == "fail"
    assert outcome_for([{"id": "build", "status": "pass"}, {"id": "startup", "status": "fail"}]) == "fail"


# --------------------------------------------------------------------------- runner


DOCKER_STUB = r"""
import json, os, subprocess, sys, threading, time

state = os.environ["STUB_STATE"]
argv = sys.argv[1:]
with open(state + "/argv.jsonl", "a") as handle:
    handle.write(json.dumps(argv) + "\n")

def env_of(args):
    values = {}
    for index, item in enumerate(args):
        if item == "-e" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            values[key] = value
    return values

if argv[:1] == ["network"]:
    print("127.0.0.1")
    sys.exit(0)

if argv[:1] == ["run"]:
    values = env_of(argv)
    with open(state + "/env.json", "w") as handle:
        json.dump(values, handle)
    subprocess.Popen([sys.executable, os.environ["STUB_SERVER"], values["TAKARO_WS_URL"]],
                     stdout=open(state + "/server.out", "w"), stderr=subprocess.STDOUT)
    print("stub-container-id")
    sys.exit(0)

if argv[:1] == ["logs"]:
    # Follow the file the stub server writes.
    path = state + "/server.out"
    while not os.path.exists(path):
        time.sleep(0.05)
    with open(path) as handle:
        while True:
            line = handle.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                if os.path.exists(state + "/exited"):
                    break
                time.sleep(0.1)
    sys.exit(0)

if argv[:1] == ["inspect"]:
    fmt = argv[argv.index("-f") + 1]
    if "PortBindings" in fmt:
        print("{}")
    elif "Running" in fmt and "ExitCode" in fmt:
        print("false|0" if os.path.exists(state + "/exited") else "true|0")
    elif "Running" in fmt:
        print("false" if os.path.exists(state + "/exited") else "true")
    sys.exit(0)

if argv[:1] == ["rm"]:
    with open(state + "/removed", "w") as handle:
        handle.write(" ".join(argv))
    sys.exit(0)

sys.exit(0)
"""

STUB_SERVER = r"""
import asyncio, json, os, sys
import websockets

STATE = os.environ["STUB_STATE"]
RUN_ID = os.environ["STUB_RUN_ID"]
FAIL = os.environ.get("STUB_FAIL", "")

ITEMS = [{"code": "minecraft:diamond_sword", "name": "Diamond Sword", "description": ""},
         {"code": "minecraft:stone", "name": "Stone", "description": ""}]
ENTITIES = [{"code": "minecraft:zombie", "name": "Zombie", "description": "", "type": "hostile"},
            {"code": "minecraft:cow", "name": "Cow", "description": "", "type": "friendly"}]

def log(line):
    print(line, flush=True)

async def main():
    # The runner hands the container host.docker.internal; on this side of the stub that
    # is the loopback address the fake Takaro is bound to.
    url = sys.argv[1].replace("host.docker.internal", "127.0.0.1")
    log("[Server thread/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.5")
    log('[Server thread/INFO]: Done (12.345s)! For help, type "help"')
    check = {"target": "fabric-26.2", "fingerprint": os.environ["STUB_FINGERPRINT"],
             "connectorVersion": "0.1.1",
             "runtime": {"gameVersion": "26.1.2" if FAIL == "connector-load" else "26.2",
                         "loader": "fabric", "loaderVersion": "0.19.5", "java": 25},
             "policy": "enforce", "result": "ok", "reasons": []}
    log("[Server thread/INFO]: Takaro target-check: " + json.dumps(check))
    async with websockets.connect(url) as socket:
        await socket.send(json.dumps({"type": "identify",
                                      "payload": {"identityToken": os.environ.get("STUB_IDENTITY", "x"),
                                                  "registrationToken": "y"}}))
        async for raw in socket:
            frame = json.loads(raw)
            if frame.get("type") == "identifyResponse":
                log("[Server thread/INFO]: Identified successfully")
                continue
            if frame.get("type") != "request":
                continue
            action = frame["payload"]["action"]
            args = json.loads(frame["payload"]["args"] or "{}")
            if action == "testReachability":
                payload = {"connectable": True}
            elif action == "getPlayers":
                payload = []
            elif action == "listItems":
                payload = ITEMS
            elif action == "listEntities":
                payload = ENTITIES
            elif action == "executeConsoleCommand":
                log("[Server thread/INFO]: [Server] " + args["command"].split(" ", 1)[1])
                payload = {"rawResult": "", "success": True}
            elif action == "shutdown":
                payload = None
            else:
                payload = None
            await socket.send(json.dumps({"type": "response", "requestId": frame["requestId"],
                                          "payload": payload}))
            if action == "shutdown":
                open(STATE + "/exited", "w").close()
                return

asyncio.run(main())
"""


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = tmp_path / "stub"
    state.mkdir()
    stub = tmp_path / "docker_stub.py"
    stub.write_text(DOCKER_STUB)
    server = tmp_path / "stub_server.py"
    server.write_text(STUB_SERVER)
    monkeypatch.setenv("STUB_STATE", str(state))
    monkeypatch.setenv("STUB_SERVER", str(server))
    monkeypatch.setenv("STUB_RUN_ID", "t1")
    monkeypatch.setenv("TAKARO_MAINT_DOCKER", f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}")
    return state


def artifacts_for(run: Any, wired: Any, tmp_path: Path) -> Path:
    _, resolved, _ = run("targets", "resolve", "--game", "minecraft", "--target", "fabric-26.2", repo=wired.root)
    os.environ["STUB_FINGERPRINT"] = resolved["fingerprint"]
    out = tmp_path / "dist"
    out.mkdir(parents=True, exist_ok=True)
    name = "takaro-minecraft-mod-fabric-26.2-0.1.1.jar"
    jar = make_jar(out / name, target="fabric-26.2", fingerprint=resolved["fingerprint"], revision="26.2")
    (out / "build-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "connector": "minecraft",
                "version": "0.1.1",
                "sourceRevision": "deadbeef",
                "dirty": False,
                "builtAt": "2026-09-17T00:00:00Z",
                "toolchain": {
                    "image": "eclipse-temurin",
                    "tag": "25-jdk",
                    "digest": "sha256:" + "0" * 64,
                    "mode": "container",
                },
                "artifacts": [
                    {
                        "role": "server-mod",
                        "target": "fabric-26.2",
                        "fingerprint": resolved["fingerprint"],
                        "file": name,
                        "sha256": sha256(jar.read_bytes()),
                        "size": jar.stat().st_size,
                    }
                ],
            }
        )
    )
    return out


def test_a_full_stubbed_run_reaches_protocol_level(
    run: Any, wired: Any, tmp_path: Path, docker_stub: Path, repo_root: Path
) -> None:
    from jsonschema import Draft202012Validator

    artifacts = artifacts_for(run, wired, tmp_path)
    out = tmp_path / "reports"

    code, payload, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--target",
        "fabric-26.2",
        "--artifacts",
        str(artifacts),
        "--out",
        str(out),
        "--run-id",
        "t1",
        "--label",
        "tm.issue=149",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert code == 0, payload
    report = json.loads((out / "fabric-26.2" / "report.json").read_text())
    schema = json.loads((repo_root / "catalog/schema/v1/verify-report.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(report)) == []
    assert report["level"] == "protocol"
    assert report["outcome"] == "pass"
    assert {check["id"] for check in report["checks"] if check["status"] == "pass"} >= {
        "build",
        "startup",
        "connector-load",
        "identify",
        "heartbeat",
        "players",
        "catalog-items",
        "catalog-entities",
        "console",
        "shutdown",
    }
    items = next(check for check in report["checks"] if check["id"] == "catalog-items")
    assert items["detail"]["spotCheck"] == {
        "code": "minecraft:diamond_sword",
        "expected": "Diamond Sword",
        "actual": "Diamond Sword",
    }


def test_the_container_publishes_no_host_ports(run: Any, wired: Any, tmp_path: Path, docker_stub: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)
    run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(tmp_path / "reports"),
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    calls = [json.loads(line) for line in (docker_stub / "argv.jsonl").read_text().splitlines()]
    run_call = next(call for call in calls if call[:1] == ["run"])
    assert "-p" not in run_call
    assert "--publish" not in run_call
    assert "--publish-all" not in run_call


def test_the_container_carries_the_run_labels(run: Any, wired: Any, tmp_path: Path, docker_stub: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)
    run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(tmp_path / "reports"),
        "--run-id",
        "t1",
        "--label",
        "tm.issue=149",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    calls = [json.loads(line) for line in (docker_stub / "argv.jsonl").read_text().splitlines()]
    run_call = next(call for call in calls if call[:1] == ["run"])
    labels = [run_call[i + 1] for i, item in enumerate(run_call) if item == "--label"]
    assert "tm.run=t1" in labels
    assert "tm.issue=149" in labels
    assert any(label.startswith("tm.ttl=") for label in labels)


def test_the_container_is_always_removed(run: Any, wired: Any, tmp_path: Path, docker_stub: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)
    run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(tmp_path / "reports"),
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert (docker_stub / "removed").is_file()
    assert "rm -f" in (docker_stub / "removed").read_text().replace("-f ", "-f ")


def test_a_failing_check_exits_eight_and_still_writes_a_report(
    run: Any, wired: Any, tmp_path: Path, docker_stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_FAIL", "connector-load")
    artifacts = artifacts_for(run, wired, tmp_path)
    out = tmp_path / "reports"

    code, payload, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(out),
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert code == 8
    report = json.loads((out / "fabric-26.2" / "report.json").read_text())
    assert report["outcome"] == "fail"
    assert report["level"] == "startup"
    assert (docker_stub / "removed").is_file()


def test_a_subset_of_checks_marks_the_rest_skipped(run: Any, wired: Any, tmp_path: Path, docker_stub: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)
    out = tmp_path / "reports"

    code, _, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(out),
        "--checks",
        "build",
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert code == 0
    report = json.loads((out / "fabric-26.2" / "report.json").read_text())
    statuses = {check["id"]: check["status"] for check in report["checks"]}
    assert statuses["build"] == "pass"
    assert statuses["console"] == "skip"
    assert report["level"] == "contract"


def test_a_subset_that_skips_the_build_check_reports_no_level(
    run: Any, wired: Any, tmp_path: Path, docker_stub: Path, repo_root: Path
) -> None:
    from jsonschema import Draft202012Validator

    artifacts = artifacts_for(run, wired, tmp_path)
    out = tmp_path / "reports"

    code, _, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(out),
        "--checks",
        "startup",
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert code == 0
    report = json.loads((out / "fabric-26.2" / "report.json").read_text())
    schema = json.loads((repo_root / "catalog/schema/v1/verify-report.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(report)) == []
    statuses = {check["id"]: check["status"] for check in report["checks"]}
    assert statuses["startup"] == "pass"
    assert statuses["build"] == "skip"
    # The artifact was never checked, so the report may not claim the build class.
    assert report["level"] == "none"


def test_an_unknown_check_exits_two(run: Any, wired: Any, tmp_path: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)

    code, payload, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(tmp_path / "reports"),
        "--checks",
        "nonsense",
        repo=wired.root,
    )

    assert code == 2
    assert "nonsense" in payload["error"]


def test_hosted_mode_is_reserved(run: Any, wired: Any, tmp_path: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)

    code, payload, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(tmp_path / "reports"),
        "--takaro",
        "hosted",
        repo=wired.root,
    )

    assert code == 2
    assert "#152" in payload["error"]


def test_a_missing_build_manifest_exits_two(run: Any, wired: Any, tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()

    code, payload, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(empty),
        "--out",
        str(tmp_path / "reports"),
        repo=wired.root,
    )

    assert code == 2
    assert "build-manifest.json" in payload["error"]


def test_the_negative_checks_are_reported_as_skipped(run: Any, wired: Any, tmp_path: Path, docker_stub: Path) -> None:
    artifacts = artifacts_for(run, wired, tmp_path)
    out = tmp_path / "reports"

    code, _, _ = run(
        "verify",
        "--game",
        "minecraft",
        "--artifacts",
        str(artifacts),
        "--out",
        str(out),
        "--negative",
        "--run-id",
        "t1",
        "--startup-timeout",
        "60",
        repo=wired.root,
    )

    assert code == 0
    report = json.loads((out / "fabric-26.2" / "report.json").read_text())
    negative = next(check for check in report["checks"] if check["id"] == "negative")
    assert negative["status"] == "skip"
    assert "#152" in negative["detail"]["reason"]
