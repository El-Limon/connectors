"""Enshrouded's verification hooks: what the generic runner cannot know about this game.

The connector is two processes, so a run boots two containers. The game container runs the
Windows server under Proton with the native plugin loaded; a second container -- built from
the *shipped* sidecar zip, so what is proven is the release artifact and not the source
tree -- joins the game's network namespace and talks to the plugin's loopback API.

The claim this file exists to make is a compatibility claim, not a liveness one. The plugin
resolves game code by pinned signatures and keeps the server running when one of them no
longer matches; it only says so in ``/health``. So ``plugin-health`` fails on a degraded
capability, on a game build other than the pinned one and on the wrong Proton, even though
the server process is perfectly alive -- and ``--negative`` proves that failure happens by
booting a deliberately corrupted build.

The base ``connector-load``/``identify``/``heartbeat``/``players``/``catalog-*``/
``console``/``shutdown`` checks look for lines and answers that arrive from the *sidecar*
here, so they stay out of an Enshrouded run -- :data:`UNSUPPORTED_CHECKS` is what keeps
them out, and the runner applies it to every game -- and each ``sidecar-*`` check says
which one it replaces. That is why an Enshrouded report reaches ``startup`` and never
claims ``protocol``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ... import net, output
from ...verify import checks, checks_lifecycle
from ...verify.hooks import GameHooks
from ...verify.runner import Container, docker_command
from . import PLUGIN_DLL, SERVER_DIR, plugin_token

CHECK_IDS = (
    "plugin-health",
    "sidecar-identify",
    "sidecar-players",
    "sidecar-catalog",
    "sidecar-console",
    "action",
    "reconnect",
    "event",
    "stop",
    "negative-degraded-hooks",
)

# What the container log (supervisord + the game's own stdout) says at each moment.
READY_LINE = re.compile(r"\[Session\] 'HostOnline' \(up\)!")

#: Base checks this connector cannot satisfy, and the check that stands in for each.
#: A run that names no ``--checks`` excludes these rather than failing them.
UNSUPPORTED_CHECKS = {
    "connector-load": (
        "the connector lives in the sidecar, not the game container; `plugin-health` asserts the plugin"
    ),
    "identify": ("the sidecar identifies to Takaro; `sidecar-identify` asserts that frame"),
    "heartbeat": ("the sidecar answers Takaro, not the game container; `sidecar-identify` covers the link"),
    "players": ("the player list comes from the sidecar; `sidecar-players` asserts it"),
    "catalog-items": ("spot-checks a Minecraft item id; `sidecar-catalog` spot-checks an Enshrouded one"),
    "catalog-entities": ("spot-checks a Minecraft entity id; `sidecar-catalog` covers Enshrouded's entities"),
    "console": ("the base console check drives a Minecraft command; `sidecar-console` drives an Enshrouded one"),
    "shutdown": ("asserts an exit code this server's teardown does not give; `stop` asserts the shutdown"),
}

BUILD_LINE = re.compile(r"Game Version \(SVN\): (?P<build>\d+)")
SHUTDOWN_LINE = re.compile(r"\[app\] Trigger gameflow shutdown, exit: Ctrl_C")
SAVED_LINE = re.compile(r"\[server\] Saved")
RESPAWN_LINE = re.compile(r"spawned: 'enshrouded-server'")
# Any line that would mean the image updated the game underneath the pinned hooks.
DRIFT_LINE = re.compile(r"steamcmd|app_update|needs to be updated", re.IGNORECASE)

# What the sidecar says.
IDENTIFIED_LINE = re.compile(r"Identified with Takaro")
CLOSED_LINE = re.compile(r"Takaro WebSocket closed code=1001")

# What the plugin writes into <server>/takaro/plugin.log.
PLUGIN_LISTENING = re.compile(r"http: listening on 127\.0\.0\.1:18890")

PLUGIN_PORT = 18890
SIDECAR_PORT = 18891
PLUGIN_BUDGET = 120.0
IDENTIFY_BUDGET = 120.0
# The sidecar backs off from 2 s to a 60 s cap, so one full cycle has to fit.
RECONNECT_BUDGET = 90.0
EVENT_BUDGET = 60.0
STOP_BUDGET = 120.0
STOP_TIMEOUT = 120

PLUGIN_CONFIG = Path("takaro") / "plugin.json"
SIDECAR_FOLDER = Path("takaro") / "sidecar" / "TakaroEnshroudedSidecar"


# --------------------------------------------------------------------------- run setup


def before_boot(run: Any, takaro_env: dict[str, str]) -> Path:
    """This run's check selection and the plugin's only configuration, before the boot.

    ``before_boot`` is the first hook an Enshrouded run reaches and every check the default
    selection leaves out runs after it, so the selection is settled here.

    The token never reaches the docker command line: the plugin reads it from this file
    when ``TAKARO_PLUGIN_TOKEN`` is unset, and the sidecar is given the same derived value.
    """
    path = run.data_dir / PLUGIN_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": plugin_token(takaro_env)}) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    output.info(f"wrote {PLUGIN_CONFIG.as_posix()} for this run (mode 0600)")
    return path


def scan_runtime_identity(adapter: Any, log_file: Path) -> dict[str, Any]:
    """The game build that actually booted, and the Proton the image actually carries."""
    found = checks.find_line(log_file, BUILD_LINE)
    identity = dict(adapter.parse_runtime_identity(found[1]) or {}) if found else {}
    if identity:
        identity["loaderVersion"] = _proton_version(adapter.last_container_ref) or None
    return identity


def _proton_version(container_ref: str) -> str:
    """``/usr/local/bin/version`` of the pinned image: ``<epoch> GE-Proton10-30``."""
    if not container_ref:
        return ""
    completed = subprocess.run(
        [*docker_command(), "run", "--rm", "--entrypoint", "cat", container_ref, "/usr/local/bin/version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.split()[-1] if completed.stdout.split() else ""


# --------------------------------------------------------------------------- the sidecar


def sidecar_image(run: Any, suffix: str) -> str:
    return f"takaro-enshrouded-sidecar:tm-{run.options.run_id}{suffix}"


def start_sidecar(run: Any, fake: Any, *, suffix: str = "") -> Container:
    """Build the SHIPPED sidecar zip's folder and run it in the game's network namespace.

    A container that joins another's namespace cannot take ``--add-host``, so the sidecar
    is handed the bridge gateway address directly instead of ``host.docker.internal``.
    """
    source = run.data_dir / SIDECAR_FOLDER
    if not (source / "Dockerfile").is_file():
        raise RuntimeError(f"{source}/Dockerfile is missing; the shipped sidecar zip did not deploy")
    image = sidecar_image(run, suffix)
    build_log = run.out / "sidecar-build.log"
    completed = subprocess.run(
        [*docker_command(), "build", "-t", image, "--label", f"tm.run={run.options.run_id}", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    build_log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"the shipped sidecar image did not build; see {build_log.name}")

    takaro_env = run.takaro_env(f"ws://{fake.host}:{fake.port}/")
    token = plugin_token(takaro_env)
    name = f"{run.container.name}-sidecar"
    ttl = int(time.time()) + 3 * 3600
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
    argv += ["--network", f"container:{run.container.name}", "--memory", "512m"]
    environment = {
        "TAKARO_WS_URL": takaro_env["TAKARO_WS_URL"],
        "TAKARO_IDENTITY_TOKEN": takaro_env["TAKARO_IDENTITY_TOKEN"],
        "TAKARO_REGISTRATION_TOKEN": takaro_env["TAKARO_REGISTRATION_TOKEN"],
        "TAKARO_SERVER_NAME": f"takaro-verify-{run.options.run_id}",
        "TAKARO_PLUGIN_URL": f"http://127.0.0.1:{PLUGIN_PORT}",
        "TAKARO_PLUGIN_TOKEN": token,
        "TAKARO_CURSOR_FILE": "/data/event-cursor.json",
        "ENSHROUDED_LOG_FILE": f"{SERVER_DIR}/logs/enshrouded_server.log",
        "ENSHROUDED_LOG_EVENTS": "filtered",
        "SIDECAR_HEALTH_PORT": str(SIDECAR_PORT),
        "DEBUG": "1",
    }
    for key, value in sorted(environment.items()):
        argv += ["-e", f"{key}={value}"]
    sidecar_data = run.data_dir / ".takaro" / "runtime" / f"sidecar-data{suffix}"
    sidecar_data.mkdir(parents=True, exist_ok=True)
    argv += ["-v", f"{sidecar_data}:/data", "-v", f"{run.data_dir}:{SERVER_DIR}:ro", image]

    container = Container(
        name=name,
        argv=argv,
        log_file=run.out / f"sidecar{suffix}.log",
        docker_log=run.docker_log,
        secrets=[takaro_env["TAKARO_REGISTRATION_TOKEN"], token],
    )
    run.containers.append(container)
    run.extra_logs.append(container.log_file)
    container.start()
    return container


def connector_version(run: Any) -> str | None:
    """The version the loaded plugin should report, from the manifest the artifacts came with.

    The DLL stamps the version release-please writes into ``mod/src/common.h``, so a dev
    build of ``0.4.2`` still says ``0.4.2`` while the run itself is versioned
    ``0.4.2-dev.<sha>``. The release part is what the plugin can honestly be held to, and
    holding it to that still catches the failure that matters: an older DLL left behind by
    an earlier deploy.
    """
    manifest = run.options.artifacts / "build-manifest.json"
    if not manifest.is_file():
        return None
    try:
        version = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (json.JSONDecodeError, KeyError):
        return None
    return version.split("-", 1)[0]


def _exec_json(container_name: str, argv: list[str]) -> Any:
    """Run a command inside a container and parse its stdout as JSON.

    Used with the sidecar container, which shares the game's namespace and therefore
    reaches both loopback APIs. The bearer token is on this argv and nowhere else.
    """
    completed = subprocess.run(
        [*docker_command(), "exec", container_name, *argv], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker exec exited {completed.returncode}: {completed.stderr.strip()[:200]}")
    return json.loads(completed.stdout)


def _plugin_health(container_name: str, token: str) -> Any:
    return _exec_json(
        container_name,
        ["wget", "-qO-", "--header", f"Authorization: Bearer {token}", f"http://127.0.0.1:{PLUGIN_PORT}/health"],
    )


def _sidecar_health(container_name: str) -> Any:
    return _exec_json(container_name, ["wget", "-qO-", f"http://127.0.0.1:{SIDECAR_PORT}/health"])


# --------------------------------------------------------------------------- the claim


def classify_health(health: Any, expected_build: str, expected_version: str | None = None) -> dict[str, Any]:
    """Is this plugin compatible with the build it is running on? The whole claim, in one place.

    Everything that would let a mismatched plugin pass as healthy is a problem here: an
    overall status that is not ``ok``, a capability the plugin itself downgraded, a game
    build other than the one the signatures were derived on, and a plugin version other
    than the one this run built.
    """
    problems: list[str] = []
    if not isinstance(health, dict):
        return {"ok": False, "problems": [f"the plugin answered {health!r}, expected a health document"]}
    status = str(health.get("status"))
    if status != "ok":
        problems.append(f"plugin status is '{status}', expected 'ok'")
    build = str(health.get("gameBuild"))
    if build != str(expected_build):
        problems.append(f"the server reports game build {build}, the pinned target is {expected_build}")
    capabilities = health.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        # A health document that is JSON but not shaped like one is a failed claim, not a
        # crashed run: it means nothing readable can be said about the hooks.
        return {
            "ok": False,
            "problems": [f"the plugin reported capabilities as {type(capabilities).__name__}, expected an object"],
            "pluginVersion": str(health.get("version") or "") or None,
            "gameBuild": str(health.get("gameBuild")),
            "expectedBuild": str(expected_build),
            "capabilities": {},
            "degraded": [],
            "unimplemented": [],
        }
    degraded = sorted(name for name, value in capabilities.items() if str(value) == "degraded")
    unimplemented = sorted(name for name, value in capabilities.items() if str(value) == "unimplemented")
    known_states = ("ok", "degraded", "unimplemented")
    unknown = sorted(name for name, value in capabilities.items() if str(value) not in known_states)
    if degraded:
        problems.append("capabilities self-checked as degraded: " + ", ".join(degraded))
    if unknown:
        problems.append("capabilities in an unknown state: " + ", ".join(unknown))
    if not capabilities:
        problems.append("the plugin reported no capabilities at all")
    version = str(health.get("version") or "")
    if expected_version and version != expected_version:
        problems.append(f"the plugin reports version '{version}', this run built '{expected_version}'")
    return {
        "ok": not problems,
        "problems": problems,
        "pluginVersion": version or None,
        "gameBuild": build,
        "expectedBuild": str(expected_build),
        "capabilities": dict(capabilities),
        "degraded": degraded,
        "unimplemented": unimplemented,
    }


# --------------------------------------------------------------------------- local hooks


async def after_protocol(run: Any, fake: Any, alive: Any) -> None:
    sidecar: Container | None = None
    if run.wanted("plugin-health") or _any_sidecar_check(run):
        sidecar = await asyncio.to_thread(start_sidecar, run, fake)
    if run.wanted("plugin-health"):
        run.record(await asyncio.to_thread(_check_plugin_health, run, sidecar))
    else:
        run.skip("plugin-health", "not selected by --checks")

    for check_id, coroutine in (
        ("sidecar-identify", lambda: _check_identify(run, fake, sidecar, alive)),
        ("sidecar-players", lambda: _check_players(run, fake)),
        ("sidecar-catalog", lambda: _check_catalog(run, fake)),
        ("sidecar-console", lambda: _check_console(run, fake)),
        ("action", lambda: _check_action(run, fake)),
        ("reconnect", lambda: _check_reconnect(run, fake, sidecar, alive)),
    ):
        if run.wanted(check_id):
            run.record(await coroutine())
        else:
            run.skip(check_id, "not selected by --checks")


def _any_sidecar_check(run: Any) -> bool:
    return any(run.wanted(check) for check in CHECK_IDS if check.startswith("sidecar-")) or any(
        run.wanted(check) for check in ("action", "reconnect", "event")
    )


def _sidecar_name(run: Any, sidecar: Container | None) -> str:
    if sidecar is None:
        raise RuntimeError("the sidecar container was never started")
    return sidecar.name


def _check_plugin_health(run: Any, sidecar: Container | None) -> checks.CheckResult:
    """The compatibility claim: the hooks resolved, on the build they were proven on."""
    with checks._Timer() as timer:
        problems: list[str] = []
        verdict: dict[str, Any] = {}
        proton = ""
        expected_proton = str(run.target.record["runtime"]["container"].get("env", {}).get("TAKARO_PINNED_PROTON", ""))
        plugin_log = run.data_dir / "takaro" / "plugin.log"
        listening = checks.wait_for_line(plugin_log, PLUGIN_LISTENING, PLUGIN_BUDGET, run.container.alive)
        if not listening:
            problems.append(f"the plugin never logged that it was listening within {PLUGIN_BUDGET:.0f} s")
        version = connector_version(run)
        try:
            health = _plugin_health(_sidecar_name(run, sidecar), plugin_token(run.takaro_env("")))
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"the plugin's /health could not be read: {exc}")
        else:
            verdict = classify_health(health, str(run.target.record["revision"]), version)
            problems += list(verdict["problems"])
        if version and not checks.find_line(plugin_log, re.compile(rf"takaro enshrouded plugin {re.escape(version)} ")):
            problems.append(f"{plugin_log.name} never announced 'takaro enshrouded plugin {version} starting'")
        proton = _container_proton(run.container.name)
        if expected_proton and not proton.endswith(expected_proton):
            problems.append(f"the container carries Proton '{proton}', the target pins '{expected_proton}'")
    return checks.CheckResult(
        "plugin-health",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "pluginVersion": verdict.get("pluginVersion"),
            "gameBuild": verdict.get("gameBuild"),
            "expectedBuild": str(run.target.record["revision"]),
            "proton": proton,
            "expectedProton": expected_proton,
            "capabilities": verdict.get("capabilities", {}),
            "degraded": verdict.get("degraded", []),
            "unimplemented": verdict.get("unimplemented", []),
            "note": (
                "a compatibility claim, not a process check: a degraded capability or another "
                "game build fails this while the server keeps running"
            ),
            "problems": problems,
        },
        {"file": plugin_log.name, "line": listening[0]} if listening else {"file": plugin_log.name},
    )


def _container_proton(container_name: str) -> str:
    completed = subprocess.run(
        [*docker_command(), "exec", container_name, "cat", "/usr/local/bin/version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


async def _check_identify(run: Any, fake: Any, sidecar: Container | None, alive: Any) -> checks.CheckResult:
    """Enshrouded's ``identify``: the sidecar, not the game, registers with Takaro."""
    with checks._Timer() as timer:
        problems: list[str] = []
        health: Any = None
        try:
            await fake.wait_for_identify(IDENTIFY_BUDGET)
        except TimeoutError as exc:
            problems.append(str(exc))
        log_file = run.out / "sidecar.log"
        line = await asyncio.to_thread(checks.wait_for_line, log_file, IDENTIFIED_LINE, 30, alive)
        if not line:
            problems.append("the sidecar log never showed 'Identified with Takaro'")
        try:
            health = await asyncio.to_thread(_sidecar_health, _sidecar_name(run, sidecar))
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"the sidecar's /health could not be read: {exc}")
        if isinstance(health, dict) and health.get("takaroIdentified") is not True:
            problems.append(f"the sidecar reports takaroIdentified={health.get('takaroIdentified')!r}")
    return checks.CheckResult(
        "sidecar-identify",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "identifyFrames": fake.identify_count,
            "identifyKeys": sorted(fake.identified or {}),
            "sidecarHealth": _redacted_health(health),
            "note": "Enshrouded's `identify`: the sidecar registers, the game server never speaks to Takaro",
            "problems": problems,
        },
        {"file": log_file.name, "line": line[0]} if line else {"file": log_file.name},
    )


def _redacted_health(health: Any) -> Any:
    if not isinstance(health, dict):
        return health
    return {key: value for key, value in health.items() if "token" not in key.lower()}


async def _check_players(run: Any, fake: Any) -> checks.CheckResult:
    """Enshrouded's ``players``, plus the reachability answer the degraded case changes."""
    del run
    with checks._Timer() as timer:
        problems: list[str] = []
        reachable: Any = None
        players: Any = None
        try:
            reachable = await fake.request("testReachability", {})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"testReachability failed: {exc}")
        if not isinstance(reachable, dict) or reachable.get("connectable") is not True:
            problems.append(f"testReachability returned {reachable!r}, expected connectable true")
        elif reachable.get("reason") is not None:
            problems.append(f"testReachability is connectable but reports reason {reachable['reason']!r}")
        try:
            players = await fake.request("getPlayers", {})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"getPlayers failed: {exc}")
        if not isinstance(players, list):
            problems.append(f"getPlayers returned {players!r}, expected a list")
        elif players:
            problems.append(f"getPlayers returned {len(players)} player(s); nobody joins a verification run")
    return checks.CheckResult(
        "sidecar-players",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "testReachability": reachable,
            "players": players,
            "note": "Enshrouded's `players`; a non-null reachability reason names the degraded capabilities",
            "problems": problems,
        },
    )


#: A dev token that has no business in a name an operator reads. The same set the mod's
#: own corpus test asserts over ``kEntities``/``kLocations`` (``mod/tests/names_test.cpp``),
#: applied here to what the running server actually answered.
_DEV_TOKENS = frozenset(
    {
        "ag2",
        "deprecated",
        "depricated",
        "placement",
        "noui",
        "healthbar",
        "unused",
        "hasbugs",
        "test",
        "lvlxx",
    }
)
_BARE_TIER = re.compile(r"^[Tt]\d$")


def _looks_like_a_dev_name(name: str, code: str) -> bool:
    """A template code handed back rather than a name, by the mod's own rules.

    Item, entity and location names are all derived from the codes, because the dedicated
    server ships no localisation to read them from. That derivation is only worth anything
    if its output is actually readable, so every predicate the C++ corpus test asserts over
    the shipped table is asserted here over the live answer too -- otherwise a mod that
    regressed to opening underscores would still pass this check.
    """
    if not name or name[0] == " " or "_" in name:
        return True
    words = name.split()
    if words and words[0].isdigit():
        return True
    if any(word[:1].islower() for word in words):
        return True
    if name == code.replace("_", " "):
        return True
    return any(word.lower() in _DEV_TOKENS or _BARE_TIER.match(word) for word in words)


def _shared_names(entries: list[Any]) -> list[tuple[str, list[str]]]:
    """Names that two different codes both answer with, worst first.

    The plugin derives one name per code and re-derives the colliding ones at a more
    detailed level until they separate, so a collision in the live answer means the
    derivation lost that guarantee and an operator can no longer tell two templates apart.
    """
    by_name: dict[str, list[str]] = {}
    for entry in entries:
        name, code = str(entry["name"]), str(entry["code"])
        codes = by_name.setdefault(name, [])
        if code not in codes:
            codes.append(code)
    return sorted(((n, c) for n, c in by_name.items() if len(c) > 1), key=lambda pair: (-len(pair[1]), pair[0]))


async def _check_catalog(run: Any, fake: Any) -> checks.CheckResult:
    """Enshrouded's ``catalog-items``/``catalog-entities``, read out of the server's own kfc."""
    del run
    with checks._Timer() as timer:
        problems: list[str] = []
        samples: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for action in ("listItems", "listEntities", "listLocations"):
            entries: Any = None
            try:
                entries = await fake.request(action, {})
            except Exception as exc:  # noqa: BLE001 - reported as a check failure
                problems.append(f"{action} failed: {exc}")
                continue
            if not isinstance(entries, list) or not entries:
                problems.append(f"{action} returned {entries!r}, expected a non-empty list")
                continue
            counts[action] = len(entries)
            samples[action] = [str(entry.get("name")) for entry in entries[:3] if isinstance(entry, dict)]
            malformed = next(
                (e for e in entries if not isinstance(e, dict) or not e.get("code") or not e.get("name")), None
            )
            if malformed is not None:
                problems.append(f"{action} holds an entry without a code and a name: {malformed!r}")
                continue
            same = [e for e in entries if e["name"] == e["code"]]
            if same:
                problems.append(
                    f"{action}: {len(same)} of {len(entries)} names are the code itself, "
                    f"e.g. {', '.join(str(e['code']) for e in same[:3])}"
                )
            offenders = [e for e in entries if _looks_like_a_dev_name(str(e["name"]), str(e["code"]))]
            if offenders:
                problems.append(
                    f"{action}: {len(offenders)} of {len(entries)} names are dev codes rather than "
                    f"display names, e.g. " + ", ".join(f"{e['code']} -> {e['name']}" for e in offenders[:3])
                )
            shared = _shared_names(entries)
            if shared:
                examples = "; ".join(f"{name} <- {', '.join(codes)}" for name, codes in shared[:3])
                problems.append(f"{action}: {len(shared)} names are shared by more than one code, e.g. {examples}")
    return checks.CheckResult(
        "sidecar-catalog",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "counts": counts,
            "firstNames": samples,
            "note": (
                "Enshrouded's `catalog-items`/`catalog-entities`; item, entity and location names "
                "are derived from the template codes because the dedicated server ships no "
                "localisation, and every distinct code gets a distinct name"
            ),
            "problems": problems,
        },
    )


async def _check_console(run: Any, fake: Any) -> checks.CheckResult:
    """Enshrouded's ``console``: the plugin's own ``version`` command, answered in-band."""
    with checks._Timer() as timer:
        problems: list[str] = []
        result: Any = None
        try:
            result = await fake.request("executeConsoleCommand", {"command": "version"})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"executeConsoleCommand failed: {exc}")
        if not isinstance(result, dict) or result.get("success") is not True:
            problems.append(f"executeConsoleCommand returned {result!r}, expected success true")
        else:
            raw = str(result.get("rawResult") or result.get("output") or "")
            build = str(run.target.record["revision"])
            if build not in raw:
                problems.append(f"the console answer {raw!r} does not name game build {build}")
    return checks.CheckResult(
        "sidecar-console",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "command": "version",
            "result": result,
            "note": "Enshrouded's `console`: the plugin answers, the game has no console of its own",
            "problems": problems,
        },
    )


async def _check_action(run: Any, fake: Any) -> checks.CheckResult:
    """A representative action end to end: Takaro asks, the plugin acts, the answer returns."""
    marker = f"takaro-verify-{run.options.run_id}-action"
    with checks._Timer() as timer:
        problems: list[str] = []
        result: Any = None
        try:
            result = await fake.request("sendMessage", {"message": marker})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"sendMessage failed: {exc}")
    return checks.CheckResult(
        "action",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "action": "sendMessage",
            "message": marker,
            "result": result,
            "note": (
                "with nobody online the plugin broadcasts to no one and answers success: what this "
                "proves is the request/response path through the sidecar into the game, not delivery"
            ),
            "problems": problems,
        },
    )


async def _check_reconnect(run: Any, fake: Any, sidecar: Container | None, alive: Any) -> checks.CheckResult:
    """Takaro drops the socket; the sidecar comes back and is usable again."""
    log_file = run.out / "sidecar.log"
    with checks._Timer() as timer:
        problems: list[str] = []
        reachable: Any = None
        before = fake.identify_count
        await fake.disconnect(1001, "going away")
        reidentify_ms = await checks_lifecycle.identify_within(fake, before + 1, RECONNECT_BUDGET, alive)
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
        closed = await asyncio.to_thread(checks.wait_for_line, log_file, CLOSED_LINE, 30, alive)
        if not closed:
            problems.append("the sidecar never logged the 1001 close")
        confirmations = await asyncio.to_thread(
            checks_lifecycle.wait_for_count, log_file, IDENTIFIED_LINE, 2, 30, alive
        )
        if confirmations < 2:
            problems.append(f"the sidecar identified {confirmations} time(s), expected at least 2")
        del sidecar
    return checks.CheckResult(
        "reconnect",
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
        {"file": log_file.name},
    )


# --------------------------------------------------------------------------- shutdown


async def after_shutdown(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
    """Ask the server to shut down, then read what that produced.

    The request is made here rather than inside one of the checks, because both of them
    read its consequences: ``event`` waits for the log line the plugin forwards, ``stop``
    waits for the save and the respawn. Selecting one without the other used to mean
    waiting out three budgets for a shutdown nobody had asked for.
    """
    del ws_url
    note = "shutdown requested"
    if run.wanted("event") or run.wanted("stop"):
        try:
            await fake.request("shutdown", {}, timeout=30)
        except Exception as exc:  # noqa: BLE001 - the socket closing first is normal here
            note = f"the connection closed before the shutdown response arrived ({exc})"
    if run.wanted("event"):
        run.record(await _check_event(run, fake, note))
    else:
        run.skip("event", "not selected by --checks")
    if run.wanted("stop"):
        run.record(await _check_stop(run, ledger_inputs))
    else:
        run.skip("stop", "not selected by --checks")


async def _check_event(run: Any, fake: Any, note: str) -> checks.CheckResult:
    """The game speaks: a log line the plugin emits reaches Takaro as a gameEvent.

    The shutdown ``after_shutdown`` already requested is what makes the server write
    something worth forwarding; this waits for that line to arrive as a ``gameEvent``.
    """
    with checks._Timer() as timer:
        problems: list[str] = []
        matched: dict[str, Any] | None = None
        deadline = time.monotonic() + EVENT_BUDGET
        while time.monotonic() < deadline and matched is None:
            for payload in list(fake.events):
                data = payload.get("data") if isinstance(payload, dict) else None
                message = str((data or {}).get("msg") or "")
                named = "Trigger gameflow shutdown" in message or "Start Saving" in message
                if payload.get("type") == "log" and named:
                    matched = payload
                    break
            if matched is None:
                await asyncio.sleep(2)
        if matched is None:
            problems.append(f"no forwarded log event named the shutdown within {EVENT_BUDGET:.0f} s")
    return checks.CheckResult(
        "event",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "event": matched,
            "eventCount": len(fake.events),
            "note": note,
            "problems": problems,
        },
        {"file": run.fake_log.name},
    )


async def _check_stop(run: Any, ledger_inputs: list[dict[str, Any]]) -> checks.CheckResult:
    """The server saves, supervisord respawns it, the container stops, the bytes are unchanged.

    ``autorestart=true`` is the image's contract, so a game exit is a respawn rather than a
    container exit and the base ``shutdown`` check cannot be used. The container is stopped
    afterwards, and the pinned files are re-hashed to prove no boot replaced them.
    """
    with checks._Timer() as timer:
        problems: list[str] = []
        container = run.container
        alive = container.alive if container is not None else (lambda: False)
        stages: dict[str, bool] = {}
        for name, pattern in (("shutdown", SHUTDOWN_LINE), ("saved", SAVED_LINE)):
            found = checks.wait_for_line(run.server_log, pattern, STOP_BUDGET, alive)
            stages[name] = bool(found)
            if not found:
                problems.append(f"the server log never showed the '{name}' line within {STOP_BUDGET:.0f} s")
        # The image spawns the server once at boot, so the *first* spawn line proves nothing
        # about a respawn. supervisord's `autorestart=true` is the contract under test, and
        # what shows it is a second one after the save.
        spawns = checks_lifecycle.wait_for_count(run.server_log, RESPAWN_LINE, 2, STOP_BUDGET, alive)
        stages["respawned"] = spawns >= 2
        if spawns < 2:
            problems.append(
                f"the server was spawned {spawns} time(s); supervisord should have respawned it "
                f"after the save, within {STOP_BUDGET:.0f} s"
            )
        drift = checks.find_line(run.server_log, DRIFT_LINE)
        if drift:
            problems.append(f"the boot ran an update path: {run.server_log.name}:{drift[0]} {drift[1][:120]}")
        code = -1
        if container is not None:
            completed = await asyncio.to_thread(
                subprocess.run,
                [*docker_command(), "stop", "-t", str(STOP_TIMEOUT), container.name],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                problems.append(f"docker stop failed: {completed.stderr.strip()}")
            code = await asyncio.to_thread(container.wait_for_exit, float(STOP_TIMEOUT))
            if code != 0:
                problems.append(f"the server container exited {code}, expected 0")
        intact, changed = _rehash(run.data_dir, ledger_inputs)
        if changed:
            problems.append("the pinned inputs changed during the run: " + "; ".join(changed))
        # The sidecars go first: an image cannot be removed while a container still holds it.
        for other in run.containers:
            if other is not container:
                other.remove()
        _remove_images(run)
    return checks.CheckResult(
        "stop",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "exitCode": code,
            "respawned": stages.get("respawned", False),
            "stages": stages,
            "noUpdatePathInBoot": drift is None,
            "inputsIntactAfterStop": not changed,
            "intact": intact,
            "problems": problems,
        },
        {"file": run.server_log.name},
    )


def _remove_images(run: Any) -> None:
    for suffix in ("", "-degraded"):
        subprocess.run(
            [*docker_command(), "rmi", sidecar_image(run, suffix)], capture_output=True, text=True, check=False
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


# --------------------------------------------------------------------------- the negative


async def negative(run: Any, fake: Any, ws_url: str, manifest: dict[str, Any]) -> None:
    """Boot a deliberately corrupted plugin and prove the compatibility claim fails.

    Without this the whole ``plugin-health`` check is unfalsifiable: a build where every
    signature resolved and a build where none did both leave the server alive.
    """
    del manifest
    if not run.wanted("negative-degraded-hooks"):
        run.skip("negative-degraded-hooks", "not selected by --checks")
        return
    zig = os.environ.get("TAKARO_MAINT_ZIG") or ""
    if not zig or not Path(zig).is_file():
        run.skip("negative-degraded-hooks", "no zig on this host (set TAKARO_MAINT_ZIG)")
        return
    run.record(await _check_negative(run, fake, ws_url, zig))


async def _check_negative(run: Any, fake: Any, ws_url: str, zig: str) -> checks.CheckResult:
    from ... import paths as maint_paths

    dll = run.data_dir / "takaro" / "plugin" / PLUGIN_DLL
    kept = dll.with_name(PLUGIN_DLL + ".release")
    corrupted = "addComponent"
    mod = maint_paths.repo_root() / "games" / "enshrouded" / "mod"
    with checks._Timer() as timer:
        problems: list[str] = []
        health: Any = None
        verdict: dict[str, Any] = {}
        reachable: Any = None
        ready = False
        try:
            built = await asyncio.to_thread(_build_degraded, mod, zig, corrupted, run.out)
            dll.replace(kept)
            dll.write_bytes(built.read_bytes())
            os.chmod(dll, 0o644)
            container = run.boot(ws_url, suffix="-degraded", log_name="server-degraded.log")
            ready = bool(
                await asyncio.to_thread(
                    checks.wait_for_line,
                    run.out / "server-degraded.log",
                    READY_LINE,
                    run.options.startup_timeout,
                    container.alive,
                )
            )
            if not ready:
                problems.append("the server never reached HostOnline with the degraded plugin")
            before = fake.identify_count
            sidecar = await asyncio.to_thread(start_sidecar, run, fake, suffix="-degraded")
            await asyncio.to_thread(
                checks.wait_for_line,
                run.data_dir / "takaro" / "plugin.log",
                PLUGIN_LISTENING,
                PLUGIN_BUDGET,
                container.alive,
            )
            # The first run's sidecar was torn down with the first container, so the fake has
            # nobody to ask until this one has identified. Without the wait, every request
            # below answers "no connector is connected" and says nothing about the plugin.
            if await checks_lifecycle.identify_within(fake, before + 1, IDENTIFY_BUDGET, container.alive) is None:
                problems.append(f"the degraded run's sidecar never identified within {IDENTIFY_BUDGET:.0f} s")
            health = await asyncio.to_thread(_plugin_health, sidecar.name, plugin_token(run.takaro_env("")))
            verdict = classify_health(health, str(run.target.record["revision"]), None)
            if verdict["ok"]:
                problems.append("a plugin built with a corrupted signature was reported healthy")
            if not verdict.get("degraded"):
                problems.append("the corrupted build reported no degraded capability at all")
            try:
                reachable = await fake.request("testReachability", {})
            except Exception as exc:  # noqa: BLE001 - reported as a check failure
                problems.append(f"testReachability failed against the degraded plugin: {exc}")
            # The sidecar deliberately still reports the server connectable when the plugin's
            # overall status is ok and only some capabilities self-checked as degraded --
            # the server IS reachable, those actions are not. The reason is what has to name
            # them, so the reason is what this asserts.
            if not isinstance(reachable, dict):
                problems.append(f"testReachability returned {reachable!r}, expected a reachability document")
            elif "degraded" not in str(reachable.get("reason") or ""):
                reason = reachable.get("reason")
                problems.append(f"the reachability reason {reason!r} names no degraded capability")
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"the degraded boot could not be completed: {exc}")
        finally:
            if kept.is_file():
                kept.replace(dll)
    return checks.CheckResult(
        "negative-degraded-hooks",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "corruptedSignature": corrupted,
            "serverReachedReady": ready,
            "degraded": verdict.get("degraded", []),
            "classification": "fail" if verdict and not verdict["ok"] else "pass",
            "testReachability": reachable,
            "note": (
                "a corrupted signature must fail the compatibility claim while the server stays "
                "alive; that is what makes plugin-health falsifiable"
            ),
            "problems": problems,
        },
        {"file": "server-degraded.log"},
    )


def _build_degraded(mod: Path, zig: str, signature: str, out: Path) -> Path:
    """The same plugin, built with one signature deliberately corrupted."""
    completed = subprocess.run(
        ["bash", str(mod / "build.sh")],
        cwd=str(mod),
        capture_output=True,
        text=True,
        env={**os.environ, "ZIG": zig, "DEBUG_CORRUPT_SIG": signature},
        check=False,
    )
    (out / "plugin-degraded-build.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"mod/build.sh (DEBUG_CORRUPT_SIG={signature}) exited {completed.returncode}")
    built = mod / "build-debug" / PLUGIN_DLL
    if not built.is_file():
        raise RuntimeError(f"{built} was not produced")
    return built


#: What this game contributes to a verification run; the runner reads nothing else.
HOOKS = GameHooks(
    ready_line=READY_LINE,
    check_ids=CHECK_IDS,
    unsupported_checks=UNSUPPORTED_CHECKS,
    before_boot=before_boot,
    after_protocol=after_protocol,
    after_shutdown=after_shutdown,
    negative=negative,
    scan_runtime_identity=scan_runtime_identity,
)
