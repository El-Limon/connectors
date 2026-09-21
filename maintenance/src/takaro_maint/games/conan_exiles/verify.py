"""Conan Exiles' verification hooks: the sidecar is the connector, so the run boots two.

The generic runner boots one container and asks the connector inside it questions. Here
the connector is not inside the server at all: the dedicated server speaks RCON and
nothing else, and a Node process beside it is what talks to Takaro. So these hooks write
the server's RCON settings before it starts, then start a second container -- the
artifact, unpacked by ``deploy``, installed and run exactly the way the operator README
says to -- and drive the checks through it.

The base ``identify``, ``connector-load``, ``heartbeat``, ``players``, ``catalog-*`` and
``console`` rows are not selected for this game: they either read Minecraft log lines or
run before the sidecar exists. ``bridge-identify`` is this game's equivalent of identify
plus connector-load, and says so in its detail -- which is why a Conan report reaches
``startup`` and never claims ``protocol``.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

from ... import net, output
from ...exit_codes import UpstreamUnavailable
from ...verify import checks, checks_lifecycle
from ...verify.runner import Container, docker_command
from . import BRIDGE_FOLDER

CHECK_IDS = (
    "bridge-identify",
    "bridge-reachability",
    "bridge-players",
    "bridge-console",
    "bridge-reconnect",
    "bridge-stop",
)

READY_LINE = re.compile(r"LogInit: Display: Engine is initialized\. Leaving FEngineLoop::Init\(\)")
RCON_READY_LINE = re.compile(r"LogRcon: Display: Rcon is ready for client connections on 0\.0\.0\.0:(?P<port>\d+)")
BANNER_MARKERS = ("LogInit: Build:", "LogInit: Engine Version:")

IDENTIFIED_LINE = re.compile(r"Identified with Takaro as gameServerId=")
CLOSED_LINE = re.compile(r"Takaro WebSocket closed code=1001")
STAMP_LINE = re.compile(
    r"Takaro target: (?P<target>\S+) \((?P<fp16>[0-9a-f]{16})\) revision (?P<revision>\S+) connector (?P<version>\S+)"
)

#: In ``ConanSandbox/Saved/Logs/RconCommandLog.log``, which is where Conan logs RCON
#: traffic -- never to stdout.
RCON_COMMAND_LINE = re.compile(r"used rcon command: (?P<command>.+)$")
RCON_COMMAND_LOG = Path("ConanSandbox") / "Saved" / "Logs" / "RconCommandLog.log"
SERVER_LOG = Path("ConanSandbox") / "Saved" / "Logs" / "ConanSandbox.log"

#: The bridge's own backoff starts at 3 s and doubles; 20 s is too tight after a 1001
#: close on a box that is also running a 10 GB game server.
RECONNECT_BUDGET = 60.0
BRIDGE_STOP_TIMEOUT = 30
RCON_LOG_BUDGET = 30.0

GAME_INI_RELATIVE = Path("ConanSandbox") / "Saved" / "Config" / "LinuxServer" / "Game.ini"
RCON_PASSWORD_RELATIVE = Path(".takaro") / "runtime" / "rcon-password"
BRIDGE_CONFIG_RELATIVE = Path(".takaro") / "runtime" / "bridge" / "TakaroConfig.txt"

#: An existing ``[RconPlugin]`` block, up to the next section header or the end of the file.
RCON_SECTION_BLOCK = re.compile(r"^\[RconPlugin\]\r?\n.*?(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
GAME_INI_TEMPLATE = """
[RconPlugin]
RconEnabled=1
RconPassword={password}
RconPort={port}
RconMaxKarma=1000
"""

BRIDGE_CONFIG_TEMPLATE = """# Written by takaro-maint verify for one run; never committed, never reused.
registrationToken={registration}
identityToken={identity}
serverName={server_name}
takaroWsUrl={url}
rconHost={rcon_host}
rconPort={rcon_port}
rconPassword={rcon_password}
rconCommandGapMs=1000
httpPort=3010
pollIntervalMs=10000
enableLogEvents=true
requireModSourceAttribution=false
databasePath=
itemCatalogPath=
logFiles=/bridge/logs/ConanSandbox.log
"""


# --------------------------------------------------------------------------- before boot


def write_game_ini(data_dir: Path, password: str, *, port: int = 25575) -> Path:
    """The server's only RCON configuration, written the way the rig's entrypoint does.

    A ``[RconPlugin]`` section already in the file is replaced rather than left alone.
    ``ConanSandbox/Saved/`` survives an install, so a second verify run finds the previous
    run's section there; keeping it would leave the server on the old password while the
    sidecar is handed the new one, and every RCON check would fail at authentication.
    """
    path = data_dir / GAME_INI_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    body = RCON_SECTION_BLOCK.sub("", existing).rstrip("\n")
    path.write_text(body + GAME_INI_TEMPLATE.format(password=password, port=port), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def before_boot(run: Any, takaro_env: dict[str, str]) -> None:
    """Give the server an RCON password it alone knows, and somewhere to call HOME.

    The fake Takaro's url is kept here too: the sidecar is started later, from a hook the
    runner calls without it, and this is the one place the run hands it over.
    """
    run.ws_url = takaro_env["TAKARO_WS_URL"]
    password = secrets.token_urlsafe(18)
    write_game_ini(run.data_dir, password)
    secret = run.data_dir / RCON_PASSWORD_RELATIVE
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(password + "\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    (run.data_dir / ".takaro" / "home").mkdir(parents=True, exist_ok=True)
    output.info(f"wrote {GAME_INI_RELATIVE.as_posix()} with a per-run RCON password (mode 0600)")


def rcon_password(data_dir: Path) -> str:
    return (data_dir / RCON_PASSWORD_RELATIVE).read_text(encoding="utf-8").strip()


def scan_runtime_identity(adapter: Any, log_file: Path) -> dict[str, Any]:
    """The build and the engine that actually booted, merged from the two banner lines."""
    identity: dict[str, Any] = {}
    if not log_file.is_file():
        return identity
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not any(marker in line for marker in BANNER_MARKERS):
                continue
            parsed = adapter.parse_runtime_identity(line)
            if not parsed:
                continue
            for key, value in parsed.items():
                if value is not None and identity.get(key) is None:
                    identity[key] = value
            if identity.get("gameVersion") and identity.get("loaderVersion"):
                break
    return identity


# --------------------------------------------------------------------------- the sidecar


def render_bridge_config(
    data_dir: Path,
    *,
    registration: str,
    identity: str,
    server_name: str,
    url: str,
    rcon_host: str,
    rcon_pw: str,
    rcon_port: int = 25575,
) -> Path:
    """The sidecar's only configuration, written where the bridge container mounts it."""
    path = data_dir / BRIDGE_CONFIG_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        BRIDGE_CONFIG_TEMPLATE.format(
            registration=registration,
            identity=identity,
            server_name=server_name,
            url=url,
            rcon_host=rcon_host,
            rcon_port=rcon_port,
            rcon_password=rcon_pw,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def bridge_install_dir(resolved: dict[str, Any]) -> str:
    """Where ``deploy`` put the artifact, as the target record names it."""
    for component in resolved["components"]:
        if component["role"] == "bridge":
            return str(component["installDir"])
    raise RuntimeError("the resolved target declares no 'bridge' component")


def game_container_ip(name: str) -> str:
    """The address the sidecar reaches the server on: the bridge network, never the host."""
    completed = subprocess.run(
        [*docker_command(), "inspect", "-f", "{{.NetworkSettings.IPAddress}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    address = completed.stdout.strip()
    if not address:
        # Without it the rendered config has no rconHost and the sidecar dies on a
        # "missing required config" that says nothing about why.
        raise UpstreamUnavailable(
            f"could not read the address of container {name}: {completed.stderr.strip() or 'no output'}"
        )
    return address


def start_bridge(run: Any, ws_url: str) -> Container:
    """Install and run the artifact the way the operator README says to, in its own container."""
    password = rcon_password(run.data_dir)
    render_bridge_config(
        run.data_dir,
        registration=run.registration_token,
        identity=f"takaro-verify-{run.options.run_id}",
        server_name=f"takaro-verify-{run.options.run_id}",
        url=ws_url,
        rcon_host=game_container_ip(run.container_name),
        rcon_pw=password,
    )
    name = f"{run.container_name}-bridge"
    ttl = int(time.time()) + 3 * 3600
    bridge_dir = run.data_dir / bridge_install_dir(run.resolved) / BRIDGE_FOLDER
    argv = [
        *docker_command(),
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"tm.run={run.options.run_id}",
        "--label",
        f"tm.ttl={ttl}",
    ]
    for label in run.options.labels:
        argv += ["--label", label]
    argv += [
        "--add-host",
        "host.docker.internal:host-gateway",
        "--memory",
        "1g",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-e",
        "npm_config_cache=/tmp/npm-cache",
        "-e",
        "BRIDGE_CONFIG=/bridge/config/TakaroConfig.txt",
        "-e",
        "NODE_ENV=production",
        "-v",
        f"{bridge_dir}:/bridge/app",
        "-v",
        f"{run.data_dir / BRIDGE_CONFIG_RELATIVE.parent}:/bridge/config:ro",
        "-v",
        f"{run.data_dir / 'ConanSandbox' / 'Saved' / 'Logs'}:/bridge/logs:ro",
        "-w",
        "/bridge/app",
        str(run.resolved["toolchainRef"]),
        "sh",
        "-c",
        "npm ci --omit=dev --ignore-scripts --no-audit --no-fund && exec node dist/index.js",
    ]
    bridge = Container(
        name=name,
        argv=argv,
        log_file=run.out / "bridge.log",
        docker_log=run.docker_log,
        secrets=[run.registration_token, password],
    )
    run.containers.append(bridge)
    run.extra_logs.append(bridge.log_file)
    run.bridge = bridge
    bridge.start()
    return bridge


def rcon_commands_seen(data_dir: Path) -> list[str]:
    """Every command the server logged an RCON client running, in order."""
    path = data_dir / RCON_COMMAND_LOG
    if not path.is_file():
        return []
    found = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = RCON_COMMAND_LINE.search(line)
            if match:
                found.append(match.group("command").strip())
    return found


def _wait_for_rcon_command(data_dir: Path, command: str, timeout: float, alive: Any) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        seen = rcon_commands_seen(data_dir)
        if any(entry == command or entry.startswith(command) for entry in seen):
            return seen
        if time.monotonic() >= deadline or not alive():
            return seen
        time.sleep(2)


# --------------------------------------------------------------------------- local hooks


async def after_protocol(run: Any, fake: Any, alive: Any) -> None:
    # The sidecar is this game's connector, so it is started only when something is going
    # to ask it a question; a `--checks build,startup` run has no use for it.
    if not any(run.wanted(check_id) for check_id in CHECK_IDS):
        for check_id in CHECK_IDS:
            run.skip(check_id, "not selected by --checks")
        return
    bridge = await asyncio.to_thread(start_bridge, run, run.ws_url)
    bridge_alive = bridge.alive
    for check_id, coroutine in (
        ("bridge-identify", lambda: _check_identify(run, fake, bridge, bridge_alive)),
        ("bridge-reachability", lambda: _check_reachability(run, fake, alive)),
        ("bridge-players", lambda: _check_players(run, fake, alive)),
        ("bridge-console", lambda: _check_console(run, fake)),
        ("bridge-reconnect", lambda: _check_reconnect(run, fake, bridge_alive)),
    ):
        if run.wanted(check_id):
            run.record(await coroutine())
        else:
            run.skip(check_id, "not selected by --checks")


async def _check_identify(run: Any, fake: Any, bridge: Container, alive: Any) -> checks.CheckResult:
    """The sidecar identified with Takaro, and says which catalog target it was built for."""
    with checks._Timer() as timer:
        problems: list[str] = []
        reidentify = await checks_lifecycle.identify_within(fake, 1, checks_lifecycle.IDENTIFY_BUDGET, alive)
        if reidentify is None:
            problems.append("the bridge never sent an identify frame")
        confirmed = await asyncio.to_thread(
            checks.wait_for_line, bridge.log_file, IDENTIFIED_LINE, checks_lifecycle.IDENTIFY_BUDGET, alive
        )
        if not confirmed:
            problems.append("the bridge log never confirmed the inbound identifyResponse frame")
        stamped = await asyncio.to_thread(checks.find_line, bridge.log_file, STAMP_LINE)
        stamp: dict[str, Any] | None = None
        if stamped is None:
            problems.append("the bridge logged no 'Takaro target:' stamp; the package carries no identity")
        else:
            found = STAMP_LINE.search(stamped[1])
            assert found is not None
            stamp = found.groupdict()
            if stamp["target"] != run.target.id:
                problems.append(f"the bridge is stamped {stamp['target']}, expected {run.target.id}")
            if stamp["fp16"] != run.target.fp16:
                problems.append(f"the bridge stamp carries fingerprint {stamp['fp16']}, expected {run.target.fp16}")
    return checks.CheckResult(
        "bridge-identify",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "identifyFrames": fake.identify_count,
            "stamp": stamp,
            "note": (
                "Conan's equivalent of identify + connector-load: the sidecar logs Takaro's "
                "identifyResponse and its own catalog target stamp"
            ),
            "problems": problems,
        },
        {"file": bridge.log_file.name, "line": confirmed[0]} if confirmed else {"file": bridge.log_file.name},
    )


async def _check_reachability(run: Any, fake: Any, alive: Any) -> checks.CheckResult:
    """The server opened its RCON port and the sidecar reached it with a real command."""
    with checks._Timer() as timer:
        problems: list[str] = []
        ready = await asyncio.to_thread(checks.find_line, run.server_log, RCON_READY_LINE)
        port = None
        if ready is None:
            problems.append("the server never logged 'Rcon is ready for client connections'")
        else:
            found = RCON_READY_LINE.search(ready[1])
            port = int(found.group("port")) if found else None
        result: Any = None
        try:
            result = await fake.request("testReachability", {})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"testReachability failed: {exc}")
        if not isinstance(result, dict) or result.get("connectable") is not True:
            problems.append(f"testReachability returned {result!r}, expected connectable true")
        seen = await asyncio.to_thread(_wait_for_rcon_command, run.data_dir, "help", RCON_LOG_BUDGET, alive)
        if "help" not in seen:
            problems.append("RconCommandLog.log never recorded the 'help' the bridge says it ran")
    return checks.CheckResult(
        "bridge-reachability",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {"rconPort": port, "testReachability": result, "rconCommandsSeen": seen, "problems": problems},
        {"file": run.server_log.name, "line": ready[0]} if ready else {"file": run.server_log.name},
    )


async def _check_players(run: Any, fake: Any, alive: Any) -> checks.CheckResult:
    """An empty server answers with an empty list, and the RCON command that produced it is logged."""
    with checks._Timer() as timer:
        problems: list[str] = []
        players: Any = None
        try:
            players = await fake.request("getPlayers", {})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"getPlayers failed: {exc}")
        if not isinstance(players, list):
            problems.append(f"getPlayers returned {players!r}, expected a list")
        elif players:
            problems.append(f"getPlayers returned {len(players)} player(s) on a server nobody joined")
        seen = await asyncio.to_thread(_wait_for_rcon_command, run.data_dir, "listplayers", RCON_LOG_BUDGET, alive)
        if "listplayers" not in seen:
            problems.append("RconCommandLog.log never recorded a 'listplayers'")
    return checks.CheckResult(
        "bridge-players",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {"players": players, "rconCommandsSeen": seen, "problems": problems},
        {"file": run.server_log.name},
    )


async def _check_console(run: Any, fake: Any) -> checks.CheckResult:
    """A console command runs; a chat message says, in a structured way, that it cannot.

    ``sendMessage`` is here as a coverage statement rather than a pass condition: without
    Enhanced Pippi and the helper polling ``/mod/poll`` there is no chat path at all, and
    the bridge is supposed to say so instead of pretending a vanilla RCON broadcast is
    the same thing. The row fails if the console command fails, or if ``sendMessage``
    answers with anything but a structured refusal.
    """
    marker = f"takaro-verify-{run.options.run_id}"
    with checks._Timer() as timer:
        problems: list[str] = []
        console: Any = None
        try:
            console = await fake.request("executeConsoleCommand", {"command": "listplayers"})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"executeConsoleCommand failed: {exc}")
        if not isinstance(console, dict) or console.get("success") is not True:
            problems.append(f"executeConsoleCommand returned {console!r}, expected success true")
        elif not isinstance(console.get("rawResult"), str):
            problems.append("executeConsoleCommand returned no rawResult string")
        chat: Any = None
        try:
            chat = await fake.request("sendMessage", {"message": marker})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"sendMessage raised instead of answering: {exc}")
        if not isinstance(chat, dict) or chat.get("success") is not False or not chat.get("error"):
            problems.append(f"sendMessage returned {chat!r}, expected a structured success:false with a reason")
    return checks.CheckResult(
        "bridge-console",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "executeConsoleCommand": console,
            "sendMessageWithoutHelper": chat,
            "note": (
                "chat is not covered by this run: without Enhanced Pippi and the mod helper the "
                "bridge has no chat path and is expected to refuse rather than broadcast over RCON"
            ),
            "problems": problems,
        },
        {"file": run.server_log.name},
    )


async def _check_reconnect(run: Any, fake: Any, alive: Any) -> checks.CheckResult:
    """Takaro drops the socket; the sidecar comes back and is usable again."""
    bridge = run.bridge
    with checks._Timer() as timer:
        problems: list[str] = []
        before = fake.identify_count
        await fake.disconnect(1001, "going away")
        reidentify_ms = await checks_lifecycle.identify_within(fake, before + 1, RECONNECT_BUDGET, alive)
        reachable: Any = None
        if reidentify_ms is None:
            problems.append(f"no identify frame arrived within {RECONNECT_BUDGET:.0f} s of the close")
        else:
            try:
                await fake.ping(timeout=5)
            except (TimeoutError, RuntimeError) as exc:
                problems.append(f"the reconnected socket did not answer a ping: {exc}")
            try:
                reachable = await fake.request("testReachability", {})
            except Exception as exc:  # noqa: BLE001 - reported as a check failure
                problems.append(f"testReachability failed on the reconnected socket: {exc}")
            if not isinstance(reachable, dict) or reachable.get("connectable") is not True:
                problems.append(f"testReachability returned {reachable!r}, expected connectable true")
        closed = await asyncio.to_thread(checks.find_line, bridge.log_file, CLOSED_LINE)
        if closed is None:
            problems.append("the bridge never logged the 1001 close")
        confirmations = await asyncio.to_thread(
            checks_lifecycle.wait_for_count, bridge.log_file, IDENTIFIED_LINE, 2, 30, alive
        )
        if confirmations < 2:
            problems.append(f"the bridge identified {confirmations} time(s), expected at least 2")
    return checks.CheckResult(
        "bridge-reconnect",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "closeCode": 1001,
            "reidentifyMs": int(reidentify_ms) if reidentify_ms is not None else None,
            "identifyCountBefore": before,
            "identifyCountAfter": fake.identify_count,
            "identifyLines": confirmations,
            "testReachability": reachable,
            "budgetSeconds": RECONNECT_BUDGET,
            "problems": problems,
        },
        {"file": bridge.log_file.name},
    )


async def after_shutdown(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
    del fake, ws_url
    _retain_server_logs(run)
    if not run.wanted("bridge-stop"):
        run.skip("bridge-stop", "not selected by --checks")
        return
    run.record(await _check_stop(run, ledger_inputs))


def _retain_server_logs(run: Any) -> None:
    """Keep the two logs the server writes inside its own tree, where the report can cite them."""
    for relative in (SERVER_LOG, RCON_COMMAND_LOG):
        source = run.data_dir / relative
        if not source.is_file():
            continue
        destination = run.out / relative.name
        destination.write_bytes(source.read_bytes())
        if destination not in run.extra_logs:
            run.extra_logs.append(destination)


async def _check_stop(run: Any, ledger_inputs: list[dict[str, Any]]) -> checks.CheckResult:
    """The sidecar stops cleanly on SIGTERM and the pinned bytes are unchanged afterwards."""
    bridge = getattr(run, "bridge", None)
    with checks._Timer() as timer:
        problems: list[str] = []
        code = -1
        if bridge is None:
            problems.append("no bridge container was started")
        else:
            completed = await asyncio.to_thread(
                subprocess.run,
                [*docker_command(), "stop", "-t", str(BRIDGE_STOP_TIMEOUT), bridge.name],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                problems.append(f"docker stop failed: {completed.stderr.strip()}")
            code = await asyncio.to_thread(bridge.wait_for_exit, float(BRIDGE_STOP_TIMEOUT))
            if code != 0:
                problems.append(f"the bridge container exited {code}, expected 0")
        intact, changed = _rehash(run.data_dir, ledger_inputs)
        if changed:
            problems.append("the pinned inputs changed during the run: " + "; ".join(changed))
    return checks.CheckResult(
        "bridge-stop",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "exitCode": code,
            "inputsIntactAfterStop": not changed,
            "intact": intact,
            "rconCommandsSeen": rcon_commands_seen(run.data_dir),
            "problems": problems,
        },
        {"file": bridge.log_file.name} if bridge is not None else None,
    )


def _rehash(data_dir: Path, ledger_inputs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    intact: list[str] = []
    changed: list[str] = []
    for entry in ledger_inputs:
        path = data_dir / entry["path"]
        if not path.is_file():
            changed.append(f"{entry['path']} is missing")
            continue
        if entry.get("sha256") and net.hash_file(path)["sha256"] != entry["sha256"]:
            changed.append(f"{entry['path']} sha256 changed")
        else:
            intact.append(entry["path"])
    return intact, changed
