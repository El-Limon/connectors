"""Exact-build ARK verification using the shipped native and Generic sidecar artifacts.

A bare run can prove startup, native health, Generic identify, empty roster, outbound
request acknowledgement, and reconnect. Inbound chat and player-specific location or
inventory need a real client; those checks fail with that limitation rather than report
empty synthetic data as success. No Minecraft-specific base command is reused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ... import net
from ...verify import checks
from ...verify.hooks import GameHooks
from ...verify.runner import Container, docker_command

CHECK_IDS = (
    "native-health",
    "sidecar-identify",
    "roster",
    "location",
    "chat-in",
    "chat-out",
    "reconnect",
    "inventory",
    "catalog",
    "ark-console",
    "event",
    "stop",
    "negative-wrong-target",
)
READY_LINE = re.compile(r"ARK_NATIVE_DIAG .*main-loop-tick count=1")
UNSUPPORTED_CHECKS = {
    "connector-load": "ARK native /health is checked by native-health",
    "identify": "the Generic sidecar identify frame is checked by sidecar-identify",
    "heartbeat": "the Generic sidecar WebSocket is checked by reconnect",
    "players": "ARK roster is checked by roster",
    "catalog-items": "ARK item catalog is checked by catalog",
    "catalog-entities": "ARK entity catalog is checked by catalog",
    "console": "ARK console support is checked by ark-console",
    "shutdown": "ARK process shutdown and file integrity are checked by stop",
}
SIDECAR_FOLDER = Path("TakaroArk/TakaroArkSidecar")
NATIVE_PORT = 18891
SIDECAR_PORT = 18892


def native_token(registration_token: str) -> str:
    return hashlib.sha256(b"takaro-ark-native:" + registration_token.encode()).hexdigest()


def start_sidecar(run: Any, fake: Any) -> Container:
    source = run.data_dir / SIDECAR_FOLDER
    required = ("Dockerfile", "dist/index.js", "package-lock.json")
    if any(not (source / name).is_file() for name in required):
        raise RuntimeError("the shipped ARK sidecar folder is incomplete")
    image = f"takaro-ark-sidecar:tm-{run.options.run_id}"
    build = subprocess.run(
        [
            *docker_command(),
            "build",
            "-f",
            "Dockerfile",
            "-t",
            image,
            "--label",
            f"tm.run={run.options.run_id}",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    (run.out / "sidecar-build.log").write_text(build.stdout + build.stderr, encoding="utf-8")
    if build.returncode:
        raise RuntimeError("shipped ARK sidecar image build failed; see sidecar-build.log")
    takaro = run.takaro_env(f"ws://{fake.host}:{fake.port}/")
    token = native_token(takaro["TAKARO_REGISTRATION_TOKEN"])
    name = f"{run.container.name}-sidecar"
    argv = [
        *docker_command(),
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"tm.run={run.options.run_id}",
        "--label",
        f"tm.ttl={int(time.time()) + 10800}",
        "--network",
        f"container:{run.container.name}",
        "--memory",
        "512m",
    ]
    for label in run.options.labels:
        argv += ["--label", label]
    env = {
        "TAKARO_WS_URL": takaro["TAKARO_WS_URL"],
        "TAKARO_IDENTITY_TOKEN": takaro["TAKARO_IDENTITY_TOKEN"],
        "TAKARO_REGISTRATION_TOKEN": takaro["TAKARO_REGISTRATION_TOKEN"],
        "TAKARO_SERVER_NAME": f"takaro-verify-{run.options.run_id}",
        "ARK_NATIVE_URL": f"http://127.0.0.1:{NATIVE_PORT}",
        "ARK_NATIVE_TOKEN": token,
        "TAKARO_CURSOR_FILE": "/data/event-cursor.json",
        "SIDECAR_HEALTH_PORT": str(SIDECAR_PORT),
    }
    for key, value in sorted(env.items()):
        argv += ["-e", f"{key}={value}"]
    sidecar_data = run.data_dir / ".takaro/runtime/sidecar-data"
    sidecar_data.mkdir(parents=True, exist_ok=True)
    argv += ["-v", f"{sidecar_data}:/data", image]
    container = Container(
        name=name,
        argv=argv,
        log_file=run.out / "sidecar.log",
        docker_log=run.docker_log,
        secrets=[takaro["TAKARO_REGISTRATION_TOKEN"], token],
    )
    run.containers.append(container)
    run.extra_logs.append(container.log_file)
    container.start()
    return container


def _get_json(container: str, url: str, *, native: bool = False) -> Any:
    # Run inside the sidecar namespace. Token stays in that process environment and is
    # never interpolated into the docker argv or logs.
    script = (
        (
            "fetch(process.argv[1],{headers:{Authorization:'Bearer '+process.env.ARK_NATIVE_TOKEN}})"
            ".then(async r=>{if(!r.ok)throw Error('HTTP '+r.status);console.log(await r.text())})"
            ".catch(e=>{console.error(e.message);process.exitCode=1})"
        )
        if native
        else (
            "fetch(process.argv[1]).then(async r=>{if(!r.ok)throw Error('HTTP '+r.status);"
            "console.log(await r.text())}).catch(e=>{console.error(e.message);process.exitCode=1})"
        )
    )
    proc = subprocess.run(
        [*docker_command(), "exec", container, "node", "-e", script, url], capture_output=True, text=True, check=False
    )
    if proc.returncode:
        raise RuntimeError(f"sidecar namespace HTTP request failed: {proc.stderr.strip()[:160]}")
    return json.loads(proc.stdout)


def _wait_json(container: str, url: str, *, native: bool = False, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _get_json(container, url, native=native)
        except (RuntimeError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def _result(check_id: str, problems: list[str], started: float, **detail: Any) -> checks.CheckResult:
    return checks.CheckResult(
        check_id,
        "fail" if problems else "pass",
        int((time.monotonic() - started) * 1000),
        {**detail, "problems": problems},
    )


async def after_protocol(run: Any, fake: Any, alive: Any) -> None:
    selected = [name for name in CHECK_IDS if name not in ("negative-wrong-target", "stop") and run.wanted(name)]
    sidecar: Container | None = None
    launch_error: str | None = None
    if selected:
        try:
            sidecar = await asyncio.to_thread(start_sidecar, run, fake)
        except Exception as exc:
            launch_error = str(exc)
    for check_id in CHECK_IDS:
        if check_id in ("event", "stop", "negative-wrong-target"):
            continue
        if not run.wanted(check_id):
            run.skip(check_id, "not selected by --checks")
            continue
        started = time.monotonic()
        problems: list[str] = []
        detail: dict[str, Any] = {}
        try:
            if sidecar is None:
                raise RuntimeError(f"shipped sidecar did not start: {launch_error}")
            if check_id == "native-health":
                health = await asyncio.to_thread(
                    _wait_json, sidecar.name, f"http://127.0.0.1:{NATIVE_PORT}/health", native=True
                )
                detail["health"] = health
                if health.get("status") != "ok" or health.get("build") != str(run.target.record["revision"]):
                    problems.append("native status/build does not match the pinned target")
                if not health.get("bootId"):
                    problems.append("native bootId is missing")
                capabilities = health.get("capabilities")
                if not isinstance(capabilities, dict) or any(
                    capabilities.get(name) != "ok" for name in ("chat", "sendMessage")
                ):
                    problems.append("native chat/sendMessage capabilities are not healthy")
            elif check_id == "sidecar-identify":
                await fake.wait_for_identify(120)
                health = await asyncio.to_thread(_wait_json, sidecar.name, f"http://127.0.0.1:{SIDECAR_PORT}/health")
                detail["health"] = health
                if health.get("takaroIdentified") is not True:
                    problems.append("sidecar did not report identified")
            elif check_id == "roster":
                result = await fake.request("getPlayers", {})
                detail["players"] = result
                if not isinstance(result, list):
                    problems.append("getPlayers did not return a list")
                reach = await fake.request("testReachability", {})
                detail["reachability"] = reach
                if not isinstance(reach, dict) or reach.get("connectable") is not True:
                    problems.append("testReachability did not confirm native reachability")
            elif check_id == "location":
                players = await fake.request("getPlayers", {})
                if not players:
                    problems.append("a real connected player is required to verify finite location")
                else:
                    player = players[0]
                    position = await fake.request("getPlayerLocation", {"gameId": player["gameId"]})
                    detail["position"] = position
                    if not isinstance(position, dict) or any(
                        isinstance(position.get(k), bool)
                        or not isinstance(position.get(k), (float, int))
                        or not math.isfinite(position[k])
                        for k in ("x", "y", "z")
                    ):
                        problems.append("location did not contain numeric x/y/z")
            elif check_id == "inventory":
                players = await fake.request("getPlayers", {})
                if not players:
                    problems.append("a real connected player is required to verify inventory")
                else:
                    inventory = await fake.request("getPlayerInventory", {"gameId": players[0]["gameId"]})
                    detail["count"] = len(inventory) if isinstance(inventory, list) else None
                    if not isinstance(inventory, list):
                        problems.append("inventory did not return a list")
            elif check_id == "catalog":
                items = await fake.request("listItems", {})
                detail["count"] = len(items) if isinstance(items, list) else None
                if (
                    not isinstance(items, list)
                    or not items
                    or any(not isinstance(item, dict) or not item.get("code") or not item.get("name") for item in items)
                ):
                    problems.append("native item catalog is missing or malformed")
            elif check_id == "chat-out":
                marker = f"Takaro ARK verify {run.options.run_id}"
                detail["message"] = marker
                response = await fake.request("sendMessage", {"message": marker})
                detail["response"] = response
                if response != {}:
                    problems.append("sendMessage did not return the Generic success payload")
            elif check_id == "chat-in":
                await asyncio.sleep(1)
                events = [
                    event
                    for event in fake.events
                    if isinstance(event, dict)
                    and event.get("type") == "chat-message"
                    and isinstance(event.get("data"), dict)
                    and event["data"].get("player")
                    and event["data"].get("msg")
                ]
                detail["chatEvents"] = len(events)
                if not events:
                    problems.append("no real client chat event reached Takaro; external client input required")
            elif check_id == "ark-console":
                await fake.request("executeConsoleCommand", {"command": "listplayers"})
                problems.append("console responded, but no ARK result contract is defined yet")
            elif check_id == "reconnect":
                before = fake.identify_count
                await fake.disconnect(1001, "going away")
                await fake.wait_for_identify(90, minimum=before + 1)
                reach = await fake.request("testReachability", {})
                detail["identifyCount"] = fake.identify_count
                if not isinstance(reach, dict) or reach.get("connectable") is not True:
                    problems.append("reconnected sidecar cannot reach native")
        except Exception as exc:  # report a failed capability, never a fake pass
            problems.append(str(exc))
        run.record(_result(check_id, problems, started, **detail))


async def after_shutdown(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
    del ws_url
    if run.wanted("event"):
        events = [
            event
            for event in fake.events
            if isinstance(event, dict)
            and event.get("type") in ("chat-message", "player-connected", "player-disconnected")
        ]
        problems = [] if events else ["no real client chat/join/leave event reached Takaro"]
        run.record(
            checks.CheckResult("event", "fail" if problems else "pass", 0, {"count": len(events), "problems": problems})
        )
    else:
        run.skip("event", "not selected by --checks")
    if run.wanted("stop"):
        started = time.monotonic()
        stop_problems: list[str] = []
        if run.container is None:
            stop_problems.append("game container was not started")
        else:
            proc = await asyncio.to_thread(
                subprocess.run,
                [*docker_command(), "stop", "-t", "30", run.container.name],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode:
                stop_problems.append(f"docker stop failed: {proc.stderr.strip()[:160]}")
        for entry in ledger_inputs:
            path = run.data_dir / entry["path"]
            if not path.is_file() or net.hash_file(path).get("sha256") != entry.get("sha256"):
                stop_problems.append(f"pinned input changed or disappeared: {entry['path']}")
        run.record(_result("stop", stop_problems, started))
    else:
        run.skip("stop", "not selected by --checks")


async def _negative_impl(run: Any, fake: Any, ws_url: str, manifest: dict[str, Any]) -> None:
    del fake, ws_url, manifest
    if not run.wanted("negative-wrong-target"):
        run.skip("negative-wrong-target", "not selected by --checks")
        return
    started = time.monotonic()
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    source = run.data_dir / "TakaroArk/TakaroArkNative"
    plugin, launcher = source / "libtakaro-ark-native.so", source / "launch.sh"
    if not plugin.is_file() or not launcher.is_file():
        problems.append("the installed native artifact is incomplete")
    else:
        evidence["artifactSha256"] = net.hash_file(plugin)["sha256"]
        expected_size = run.target.record["inputs"]["server"]["files"]["ShooterGame/Binaries/Linux/ShooterGameServer"][
            "size"
        ]
        with tempfile.TemporaryDirectory(prefix="takaro-ark-wrong-target-") as temporary:
            root = Path(temporary)
            binary = root / "server/ShooterGame/Binaries/Linux/ShooterGameServer"
            binary.parent.mkdir(parents=True)
            shutil.copy2("/bin/sleep", binary)
            with binary.open("r+b") as stream:
                stream.truncate(expected_size)
            fixture_plugin = root / "plugin"
            fixture_plugin.mkdir()
            shutil.copy2(plugin, fixture_plugin / plugin.name)
            fixture_launcher = fixture_plugin / "launch.sh"
            shutil.copy2(launcher, fixture_launcher)
            fixture_launcher.chmod(0o755)
            evidence["fixtureSha256"] = net.hash_file(binary)["sha256"]
            evidence["fixtureSize"] = expected_size
            launcher_proc = subprocess.run(
                [str(fixture_launcher), str(root / "server"), "TheIsland?listen", "-server"],
                env={**os.environ, "ARK_NATIVE_TOKEN": "isolated-fixture"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            launcher_output = launcher_proc.stdout + launcher_proc.stderr
            (run.out / "negative-wrong-target-launcher.log").write_text(launcher_output, encoding="utf-8")
            evidence["launcherExit"] = launcher_proc.returncode
            if launcher_proc.returncode != 7 or launcher_output != "Unknown ARK executable; native connector refused\n":
                problems.append("shipped launcher did not reject the wrong executable hash")

            # Direct preload tests the library's own constructor guard. The
            # executable has the expected byte size and basename but a wrong
            # hash; the isolated container cannot reach the live game port.
            script = (
                "set -eu\n"
                "ARK_NATIVE_TOKEN=isolated-fixture LD_PRELOAD=/fixture/plugin/libtakaro-ark-native.so "
                "/fixture/server/ShooterGame/Binaries/Linux/ShooterGameServer 2 &\n"
                "pid=$!\n"
                "for sample in 1 2 3 4 5; do\n"
                "  sleep 0.25\n"
                "  if (echo >/dev/tcp/127.0.0.1/18891) 2>/dev/null; "
                "then echo unexpected_native_listener; exit 1; fi\n"
                "done\n"
                'echo native_listener=absent\nwait "$pid"\necho fixture_exit=0\n'
            )
            image = run.resolved["containerRef"]
            direct = subprocess.run(
                [
                    *docker_command(),
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    "none",
                    "--memory",
                    "512m",
                    "-v",
                    f"{root}:/fixture:ro",
                    image,
                    "bash",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            direct_output = direct.stdout + direct.stderr
            (run.out / "negative-wrong-target-preload.log").write_text(direct_output, encoding="utf-8")
            evidence["preloadExit"] = direct.returncode
            evidence["network"] = "none"
            if direct.returncode or direct.stdout != "native_listener=absent\nfixture_exit=0\n":
                problems.append("native preload guard did not return safely without a listener")
    evidence["problems"] = problems
    run.record(
        checks.CheckResult(
            "negative-wrong-target",
            "pass" if not problems else "fail",
            int((time.monotonic() - started) * 1000),
            evidence,
        )
    )


async def negative(run: Any, fake: Any, ws_url: str, manifest: dict[str, Any]) -> None:
    if not run.wanted("negative-wrong-target"):
        run.skip("negative-wrong-target", "not selected by --checks")
        return
    try:
        await _negative_impl(run, fake, ws_url, manifest)
    except (OSError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
        run.record(
            checks.CheckResult(
                "negative-wrong-target",
                "fail",
                0,
                {
                    "problems": [f"isolated wrong-target fixture failed: {type(exc).__name__}: {exc}"],
                },
            )
        )


HOOKS = GameHooks(
    ready_line=READY_LINE,
    startup_timeout=900,
    check_ids=CHECK_IDS,
    negative_check_ids=("negative-wrong-target",),
    unsupported_checks=UNSUPPORTED_CHECKS,
    after_protocol=after_protocol,
    after_shutdown=after_shutdown,
    negative=negative,
)
