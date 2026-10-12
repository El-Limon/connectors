"""Exact-build ARK verification of the shipped native artifact.

The plugin inside the game process holds the Takaro WebSocket itself, so every check
talks to it through the fake Takaro, and the plugin's own ``status.json`` in the owned
state directory is the independent boot/identify oracle. A bare run can prove startup,
readiness, Generic identify, empty roster, outbound acknowledgement, reconnect, saves,
shutdown and reload. Inbound chat and player-specific location or inventory need a real
client; those checks fail with that limitation rather than report empty synthetic data.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ... import net
from ...verify import checks
from ...verify.hooks import GameHooks
from ...verify.runner import docker_command
from .readonly_base import prepare as prepare_readonly_base

CHECK_IDS = (
    "native-health",
    "plugin-identify",
    "ark-heartbeat",
    "roster",
    "location",
    "chat-in",
    "chat-out",
    "reconnect",
    "inventory",
    "catalog",
    "entities",
    "ark-empty-broadcast",
    "ark-targeted-offline",
    "ark-console",
    "ark-saveworld",
    # After SaveWorld: the outage check appends a line to ShooterGame.log, and ARK writes
    # at its own file offset (no O_APPEND), so its next lines overwrite the injected bytes
    # behind the plugin's tail position. No later check depends on the game log.
    "ark-outage-replay",
    "ark-freeze-resume",
    "native-shutdown",
    "owned-save-reload",
    "event",
    "stop",
    "negative-wrong-target",
)
READY_LINE = re.compile(r"ARK_NATIVE_DIAG .*main-loop-tick count=1")
SHUTDOWN_SAVE_LINE = re.compile(r"native-shutdown-synchronous-save-completed-before-ack")
SHUTDOWN_REQUEST_LINE = re.compile(r"ARK_NATIVE_SHUTDOWN native-exit-requested$")
SAVEWORLD_COMPLETE_LINE = re.compile(r"World Save Complete[.] Took [0-9.]+$")
SHUTDOWN_MARKER_TIMEOUT = 8.0
_IN_ACCESS = 0x00000001
_IN_CLOSE_NOWRITE = 0x00000010
_IN_OPEN = 0x00000020
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_INOTIFY_EVENT = struct.Struct("iIII")
_OWNED_PATH_PROBE_SCRIPT = (
    "for exe in /proc/[0-9]*/exe; do "
    "pid=${exe#/proc/}; pid=${pid%/exe}; "
    'target=$(readlink -f "$exe" 2>/dev/null) || continue; '
    'if [ "${target##*/}" = ShooterGameServer ]; then '
    'printf \'game %s %s\\n\' "$pid" "$target"; fi; '
    "done; printf 'saved %s\\n' \"$(readlink -f /ark/ShooterGame/Saved)\""
)
UNSUPPORTED_CHECKS = {
    "connector-load": "ARK native readiness is checked by native-health",
    "identify": "the native plugin identify frame is checked by plugin-identify",
    "heartbeat": "the native plugin WebSocket ping/pong and reachability are checked by ark-heartbeat",
    "players": "ARK roster is checked by roster",
    "catalog-items": "ARK item catalog is checked by catalog",
    "catalog-entities": "ARK entity catalog is checked by entities",
    "console": "ARK console support is checked by ark-console",
    "shutdown": "ARK native shutdown acknowledgement and process exit are checked by native-shutdown",
}
STATUS_FILE = Path(".takaro/ark-native/status.json")
OFFLINE_STEAM_ID = "76561198009999999"
GAME_LOG = Path("ShooterGame/Saved/Logs/ShooterGame.log")
#: Seconds the whole game container is frozen (cgroup freezer) in ark-freeze-resume.
FREEZE_SECONDS = 180.0
#: Seconds to wait for the replayed outage event after the plugin re-identifies.
OUTAGE_DELIVERY_TIMEOUT = 30.0
#: Seconds native-health waits for the status file to report game-thread readiness.
STATUS_READY_TIMEOUT = 60.0
CONSOLE_SAFETY_PROBES = (
    ("Exit", "Use the dedicated shutdown action"),
    ("  qUiT extra", "Use the dedicated shutdown action"),
    ("DoExit", "Use the dedicated shutdown action"),
    ("cheat DoExit", "Use the dedicated shutdown action"),
    ("GetAll Foo; Quit", "Use the dedicated shutdown action"),
    ("\u00a0Exit", "Unsupported command whitespace"),
)


def read_status(run: Any) -> dict[str, Any]:
    """The plugin's own state snapshot (written every few seconds, never a secret)."""
    try:
        value = json.loads((run.data_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _wait_status(run: Any, predicate: Any, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status = await asyncio.to_thread(read_status, run)
        if predicate(status) or time.monotonic() >= deadline:
            return status
        await asyncio.sleep(1)


async def _withheld(fake: Any, action: str, args: dict[str, Any], *, timeout: float = 12.0) -> bool:
    """True when the plugin sent no response frame (the fail-closed Generic contract)."""
    try:
        await fake.request(action, args, timeout=timeout)
    except TimeoutError:
        return True
    return False


async def _fresh_saveworld_events(fake: Any, baseline: int, *, timeout: float = 10.0) -> dict[str, str | None]:
    """Observe ordered native log events delivered to Takaro after this request began."""
    deadline = time.monotonic() + timeout
    while True:
        lines = [
            event["data"]["msg"]
            for event in fake.events[baseline:]
            if isinstance(event, dict)
            and event.get("type") == "log"
            and isinstance(event.get("data"), dict)
            and isinstance(event["data"].get("msg"), str)
        ]
        start = next((i for i, line in enumerate(lines) if "Saving world..." in line), None)
        complete = next(
            (
                line
                for i, line in enumerate(lines)
                if start is not None and i > start and SAVEWORLD_COMPLETE_LINE.search(line)
            ),
            None,
        )
        if complete or time.monotonic() >= deadline:
            return {"start": lines[start] if start is not None else None, "complete": complete}
        await asyncio.sleep(0.2)


def _result(check_id: str, problems: list[str], started: float, **detail: Any) -> checks.CheckResult:
    return checks.CheckResult(
        check_id,
        "fail" if problems else "pass",
        int((time.monotonic() - started) * 1000),
        {**detail, "problems": problems},
    )


def _fresh_shutdown_markers(log_file: Path, device: int, inode: int, offset: int) -> dict[str, str | None]:
    """Find both shutdown markers only in bytes appended after this request began."""
    deadline = time.monotonic() + SHUTDOWN_MARKER_TIMEOUT
    while True:
        stat = log_file.stat()
        if (stat.st_dev, stat.st_ino) != (device, inode) or stat.st_size < offset:
            raise RuntimeError("server log rotated or truncated during native shutdown")
        with log_file.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (device, inode) or opened.st_size < offset:
                raise RuntimeError("server log rotated or truncated during native shutdown")
            stream.seek(offset)
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        save = next((line for line in lines if SHUTDOWN_SAVE_LINE.search(line)), None)
        request_line = next((line for line in lines if SHUTDOWN_REQUEST_LINE.search(line)), None)
        if (save and request_line) or time.monotonic() >= deadline:
            return {"save": save, "request": request_line}
        time.sleep(0.2)


def _native_protocol_ready(reach: Any) -> bool:
    """Zero-player readiness: roster, item and entity catalogs (chat needs a real controller)."""
    if not isinstance(reach, dict) or reach.get("connectable") is not True:
        return False
    reason = reach.get("reason")
    return reason is None or (isinstance(reason, str) and "items=" not in reason and "entities=" not in reason)


def _owned_runtime_paths_valid(lines: list[str]) -> bool:
    games = [line.split(" ", 2) for line in lines if line.startswith("game ")]
    saved = [line.removeprefix("saved ") for line in lines if line.startswith("saved ")]
    return (
        len(games) == 1
        and len(games[0]) == 3
        and games[0][1].isdigit()
        and int(games[0][1]) > 1
        and games[0][2] == "/ark/ShooterGame/Binaries/Linux/ShooterGameServer"
        and saved == ["/ark/ShooterGame/Saved"]
        and len(lines) == 2
    )


class _OwnedSaveReadWatch:
    """Watch the exact owned save inode while only the second game boot can read it."""

    def __init__(self, save: Path) -> None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd
        mask = _IN_ACCESS | _IN_OPEN | _IN_CLOSE_NOWRITE | _IN_DELETE_SELF | _IN_MOVE_SELF
        self.wd = libc.inotify_add_watch(fd, os.fsencode(save), mask)
        if self.wd < 0:
            error = ctypes.get_errno()
            os.close(fd)
            raise OSError(error, f"cannot watch owned save {save}")

    def close(self) -> None:
        os.close(self.fd)

    def read_evidence(self) -> dict[str, Any]:
        masks: list[int] = []
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            offset = 0
            while offset < len(chunk):
                wd, mask, _, name_length = _INOTIFY_EVENT.unpack_from(chunk, offset)
                offset += _INOTIFY_EVENT.size + name_length
                if wd == self.wd or mask & _IN_Q_OVERFLOW:
                    masks.append(mask)
        invalid = any(mask & (_IN_DELETE_SELF | _IN_MOVE_SELF | _IN_Q_OVERFLOW) for mask in masks)
        stage = 0
        for mask in masks:
            if stage == 0 and mask & _IN_OPEN:
                stage = 1
            elif stage == 1 and mask & _IN_ACCESS:
                stage = 2
            elif stage == 2 and mask & _IN_CLOSE_NOWRITE:
                stage = 3
        return {"openedReadClosed": stage == 3 and not invalid, "eventMasks": masks, "invalidated": invalid}


async def _wait_native_protocol_ready(fake: Any, alive: Any, *, timeout: float = 90, poll_interval: float = 2) -> Any:
    """Wait until the plugin reports zero-player protocol readiness through Takaro."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            last = await fake.request("testReachability", {}, timeout=10)
        except (TimeoutError, RuntimeError) as exc:
            last = {"probeError": str(exc)}
        if _native_protocol_ready(last) or time.monotonic() >= deadline or not alive():
            return last
        await asyncio.sleep(poll_interval)


async def _outage_replay(run: Any, fake: Any) -> dict[str, Any]:
    """A native event created while Takaro is unreachable arrives exactly once after it returns.

    The event is an allowlisted engine save line appended to the owned game log (the plugin's
    log tail turns it into a native ring event); Takaro is down because the fake server stops
    accepting connections, so the plugin must keep the event and replay it on reconnect.
    """
    marker = f"World Save Complete. Took 0.{int(time.time() * 1000) % 1000000:06d}"
    log = run.data_dir / GAME_LOG
    detail: dict[str, Any] = {"marker": marker}
    before = fake.identify_count
    await fake.stop()
    await fake.disconnect(1012, "service restart")
    try:
        await asyncio.sleep(2)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"[{time.strftime('%Y.%m.%d-%H.%M.%S:000', time.gmtime())}][  0]{marker}\n")
        detail["appendedWhileDownUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await asyncio.sleep(8)  # several reconnect attempts fail against the closed port
    finally:
        await fake.start()
    await fake.wait_for_identify(120, minimum=before + 1)
    deadline = time.monotonic() + OUTAGE_DELIVERY_TIMEOUT
    copies: list[Any] = []
    while time.monotonic() < deadline:
        copies = [
            event
            for event in fake.events
            if isinstance(event, dict)
            and event.get("type") == "log"
            and isinstance(event.get("data"), dict)
            and event["data"].get("msg") == marker
        ]
        if copies:
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(3)  # a duplicate would arrive with the same pump pass
    copies = [
        event
        for event in fake.events
        if isinstance(event, dict)
        and event.get("type") == "log"
        and isinstance(event.get("data"), dict)
        and event["data"].get("msg") == marker
    ]
    detail["deliveredCopies"] = len(copies)
    detail["identifyCount"] = fake.identify_count
    return detail


async def _freeze_resume(run: Any, fake: Any, seconds: float) -> dict[str, Any]:
    """Freeze the whole game container, resume it, and require the same boot to serve Takaro again."""
    status_before = await asyncio.to_thread(read_status, run)
    before = fake.identify_count
    detail: dict[str, Any] = {"frozenSeconds": seconds, "bootIdBefore": status_before.get("bootId")}
    pause = await asyncio.to_thread(
        subprocess.run, [*docker_command(), "pause", run.container.name], capture_output=True, text=True, check=False
    )
    if pause.returncode:
        raise RuntimeError(f"docker pause failed: {pause.stderr.strip()[:160]}")
    try:
        await asyncio.sleep(seconds)
    finally:
        await asyncio.to_thread(
            subprocess.run,
            [*docker_command(), "unpause", run.container.name],
            capture_output=True,
            text=True,
            check=False,
        )
    await fake.wait_for_identify(180, minimum=before + 1)
    reach = await fake.request("testReachability", {})
    await asyncio.sleep(10)
    status_after = await _wait_status(run, lambda value: value.get("identified") is True)
    detail.update(
        {
            "identifyCount": fake.identify_count,
            "reachability": reach,
            "bootIdAfter": status_after.get("bootId"),
            "serverAlive": await asyncio.to_thread(run.container.alive),
        }
    )
    return detail


async def after_protocol(run: Any, fake: Any, alive: Any) -> None:
    identify_error: str | None = None
    try:
        await fake.wait_for_identify(180)
    except Exception as exc:  # every check below then fails with this reason
        identify_error = str(exc)
    for check_id in CHECK_IDS:
        if check_id in ("event", "native-shutdown", "owned-save-reload", "stop", "negative-wrong-target"):
            continue
        if not run.wanted(check_id):
            run.skip(check_id, "not selected by --checks")
            continue
        started = time.monotonic()
        problems: list[str] = []
        detail: dict[str, Any] = {}
        try:
            if identify_error is not None:
                raise RuntimeError(f"native plugin never identified with Takaro: {identify_error}")
            if check_id == "native-health":
                reach = await _wait_native_protocol_ready(fake, alive)
                detail["reachability"] = reach
                status = await _wait_status(
                    run,
                    lambda value: bool(value.get("bootId")) and value.get("gameReady") is True,
                    timeout=STATUS_READY_TIMEOUT,
                )
                detail["status"] = status
                if run.options.ark_readonly_base is not None:
                    if run.container is None:
                        raise RuntimeError("read-only ARK game container was not started")
                    paths = await asyncio.to_thread(
                        subprocess.run,
                        [
                            *docker_command(),
                            "exec",
                            run.container.name,
                            "bash",
                            "-lc",
                            _OWNED_PATH_PROBE_SCRIPT,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    detail["readOnlyPaths"] = paths.stdout.splitlines()
                    if paths.returncode or not _owned_runtime_paths_valid(paths.stdout.splitlines()):
                        problems.append("ARK process executable or Saved path escapes the owned runtime tree")
                if not _native_protocol_ready(reach):
                    problems.append("native plugin did not reach zero-player protocol readiness")
                if not status.get("bootId") or status.get("gameReady") is not True:
                    problems.append("native status file reports no boot ID or no game-thread readiness")
                detail["clientDependentCapabilities"] = ["chat", "sendMessage"]
            elif check_id == "plugin-identify":
                detail["identify"] = {
                    key: value for key, value in (fake.identified or {}).items() if key not in ("registrationToken",)
                }
                status = await _wait_status(run, lambda value: value.get("identified") is True)
                detail["status"] = status
                if status.get("identified") is not True or status.get("gameServerId") != fake.game_server_id:
                    problems.append("native status file does not report the fake Takaro identification")
            elif check_id == "ark-heartbeat":
                heartbeat = await checks.check_heartbeat(fake)
                detail.update({key: value for key, value in heartbeat.detail.items() if key != "problems"})
                problems.extend(heartbeat.detail.get("problems", []))
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
            elif check_id in ("catalog", "entities"):
                method = "listItems" if check_id == "catalog" else "listEntities"
                items = await fake.request(method, {})
                detail["count"] = len(items) if isinstance(items, list) else None
                if (
                    not isinstance(items, list)
                    or not items
                    or any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("code"), str)
                        or not item["code"].strip()
                        or not isinstance(item.get("name"), str)
                        or not item["name"].strip()
                        for item in items
                    )
                ):
                    problems.append(f"native {method} catalog is missing or malformed")
            elif check_id == "ark-empty-broadcast":
                takaro_players = await fake.request("getPlayers", {})
                detail["takaroPlayersBefore"] = takaro_players
                status = await asyncio.to_thread(read_status, run)
                detail["statusPlayersBefore"] = status.get("onlinePlayers")
                if takaro_players != [] or status.get("onlinePlayers") != []:
                    problems.append("zero-recipient broadcast requires a verified empty native and Takaro roster")
                else:
                    marker = f"ARK isolated empty broadcast {run.options.run_id}"
                    response = await fake.request("sendMessage", {"message": marker})
                    detail["generic"] = response
                    if response != {}:
                        problems.append("Generic empty broadcast did not acknowledge the native no-op")
                    detail["takaroPlayersAfter"] = await fake.request("getPlayers", {})
                    if detail["takaroPlayersAfter"] != []:
                        problems.append("empty broadcast changed the zero-player roster")
            elif check_id == "ark-targeted-offline":
                players = await fake.request("getPlayers", {})
                detail["takaroPlayers"] = players
                if not isinstance(players, list) or any(
                    isinstance(player, dict) and player.get("gameId") == OFFLINE_STEAM_ID for player in players
                ):
                    problems.append("offline target was not verified absent from the roster")
                else:
                    withheld = await _withheld(
                        fake,
                        "sendMessage",
                        {"message": "offline target must fail", "opts": {"recipient": {"gameId": OFFLINE_STEAM_ID}}},
                    )
                    detail["responseWithheld"] = withheld
                    if not withheld:
                        problems.append("targeted message to an offline player answered instead of failing closed")
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
                baseline = await asyncio.to_thread(read_status, run)
                boot_id = baseline.get("bootId")
                if not boot_id:
                    problems.append("native bootId was unavailable before console safety probes")
                safety: list[dict[str, Any]] = []
                for command, error_message in CONSOLE_SAFETY_PROBES:
                    rejection = await fake.request("executeConsoleCommand", {"command": command})
                    still_alive = bool(run.container and await asyncio.to_thread(run.container.alive))
                    status_after = await asyncio.to_thread(read_status, run)
                    row = {
                        "command": command,
                        "rejection": rejection,
                        "bootIdAfter": status_after.get("bootId"),
                        "serverAlive": still_alive,
                    }
                    safety.append(row)
                    if rejection != {"success": False, "rawResult": "", "errorMessage": error_message}:
                        problems.append(f"native console did not structurally reject {command!r}")
                    if not still_alive or row["bootIdAfter"] != boot_id:
                        problems.append(f"native boot changed or stopped after console rejection {command!r}")
                detail["safetyProbes"] = safety
                detail["bootIdBefore"] = boot_id
                result = await fake.request(
                    "executeConsoleCommand", {"command": "GetAll ShooterPlayerState PlayerName"}
                )
                detail["result"] = result
                if (
                    not isinstance(result, dict)
                    or result.get("success") is not True
                    or not isinstance(result.get("rawResult"), str)
                    or result.get("errorMessage") is not None
                ):
                    problems.append("native GetAll player-name query was not reported handled with a valid result")
            elif check_id == "ark-saveworld":
                if run.container is None:
                    raise RuntimeError("game container was not started")
                save = run.data_dir / "ShooterGame/Saved/SavedArks/TheIsland.ark"
                before = save.stat() if save.is_file() else None
                event_baseline = len(fake.events)
                detail["eventIndexBefore"] = event_baseline
                status_before = await asyncio.to_thread(read_status, run)
                result = await fake.request("executeConsoleCommand", {"command": "SaveWorld"})
                detail["result"] = result
                if result != {"success": True, "rawResult": "", "errorMessage": None}:
                    problems.append("Generic SaveWorld was not acknowledged as a completed native save")
                markers = await _fresh_saveworld_events(fake, event_baseline)
                detail["freshSaveMarkers"] = markers
                if not markers["start"] or not markers["complete"]:
                    problems.append("fresh native SaveWorld log events were not delivered to Generic Takaro")
                after = save.stat() if save.is_file() else None
                detail["ownedSave"] = {
                    "sizeBefore": before.st_size if before else None,
                    "mtimeNsBefore": before.st_mtime_ns if before else None,
                    "sizeAfter": after.st_size if after else None,
                    "mtimeNsAfter": after.st_mtime_ns if after else None,
                    "sha256After": await asyncio.to_thread(net.sha256_file, save) if after else None,
                }
                if not after or after.st_size <= 0 or (before and after.st_mtime_ns <= before.st_mtime_ns):
                    problems.append("owned world save was not freshly written by SaveWorld")
                status_after = await asyncio.to_thread(read_status, run)
                detail["bootIdBefore"] = status_before.get("bootId")
                detail["bootIdAfter"] = status_after.get("bootId")
                if (
                    not await asyncio.to_thread(run.container.alive)
                    or not detail["bootIdBefore"]
                    or (detail["bootIdAfter"] != detail["bootIdBefore"])
                ):
                    problems.append("game stopped or native boot changed after SaveWorld")
            elif check_id == "reconnect":
                before = fake.identify_count
                await fake.disconnect(1001, "going away")
                await fake.wait_for_identify(90, minimum=before + 1)
                reach = await fake.request("testReachability", {})
                detail["identifyCount"] = fake.identify_count
                if not isinstance(reach, dict) or reach.get("connectable") is not True:
                    problems.append("reconnected plugin cannot report native reachability")
            elif check_id == "ark-outage-replay":
                detail.update(await _outage_replay(run, fake))
                if detail["deliveredCopies"] != 1:
                    problems.append("event created during the Takaro outage was not delivered exactly once")
            elif check_id == "ark-freeze-resume":
                if run.container is None:
                    raise RuntimeError("game container was not started")
                detail.update(await _freeze_resume(run, fake, FREEZE_SECONDS))
                if detail["serverAlive"] is not True:
                    problems.append("game process did not survive the freeze and resume")
                if not detail["bootIdBefore"] or detail["bootIdAfter"] != detail["bootIdBefore"]:
                    problems.append("native boot changed across the freeze")
                reach = detail["reachability"]
                if not isinstance(reach, dict) or reach.get("connectable") is not True:
                    problems.append("plugin did not serve Takaro again after the freeze")
        except Exception as exc:  # report a failed capability, never a fake pass
            problems.append(str(exc))
        run.record(_result(check_id, problems, started, **detail))


async def _reload_owned_save(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    problems: list[str] = []
    detail: dict[str, Any] = {}
    try:
        first_shutdown = next((row for row in run.results if row.id == "native-shutdown"), None)
        first_health = next((row for row in run.results if row.id == "native-health"), None)
        if first_shutdown is None or first_shutdown.status != "pass":
            raise RuntimeError("owned save reload requires a passing native-shutdown check")
        if first_health is None or first_health.status != "pass":
            raise RuntimeError("owned save reload requires a passing first native-health check")
        first_boot_id = first_health.detail.get("status", {}).get("bootId")
        if not isinstance(first_boot_id, str) or not first_boot_id:
            raise RuntimeError("first native boot ID is missing")
        detail["firstBootId"] = first_boot_id
        save = run.data_dir / "ShooterGame/Saved/SavedArks/TheIsland.ark"
        if save.is_symlink() or not save.resolve().is_relative_to(run.data_dir.resolve()):
            raise RuntimeError("reload save is not contained in the owned runtime tree")
        before = await asyncio.to_thread(net.hash_file, save)
        if before["size"] <= 0:
            raise RuntimeError("owned save is empty before reload")
        detail["saveBeforeReload"] = {**before, "mtimeNs": save.stat().st_mtime_ns}

        if run.container is None:
            raise RuntimeError("first game container is missing")
        identified_before = fake.identify_count

        # Hash before arming the watch; no runner code reads this file until the
        # second game has booted. OPEN+ACCESS+CLOSE_NOWRITE on this exact inode
        # therefore belongs to the isolated second server's world-load window.
        watch = _OwnedSaveReadWatch(save)
        try:
            run.extra_logs.append(run.out / "server-reload.log")
            second = await asyncio.to_thread(run.boot, ws_url, suffix="-reload", log_name="server-reload.log")
            startup = await asyncio.to_thread(
                checks.check_startup,
                run.out / "server-reload.log",
                run.startup_timeout,
                second.alive,
                run.options.ark_readonly_base or run.data_dir,
                ledger_inputs,
                run.ready_line(),
            )
            detail["reloadStartup"] = startup.detail
            if startup.status != "pass":
                raise RuntimeError("second game startup failed")
            await fake.wait_for_identify(180, minimum=identified_before + 1)
            reach = await _wait_native_protocol_ready(fake, second.alive)
            detail["reloadReachability"] = reach
            if not _native_protocol_ready(reach):
                raise RuntimeError("second native boot did not reach zero-player readiness")
            status = await _wait_status(
                run, lambda value: bool(value.get("bootId")) and value.get("bootId") != first_boot_id
            )
            detail["reloadStatus"] = status
            second_boot_id = status.get("bootId")
            if not second_boot_id or second_boot_id == first_boot_id:
                raise RuntimeError("second native boot did not publish a new boot ID")
            detail["secondBootId"] = second_boot_id
            detail["readEvidence"] = watch.read_evidence()
            if not detail["readEvidence"]["openedReadClosed"]:
                raise RuntimeError("second game boot did not open and read the exact owned save")
        finally:
            watch.close()

        after = await asyncio.to_thread(net.hash_file, save)
        detail["saveAfterReload"] = {**after, "mtimeNs": save.stat().st_mtime_ns}
        if after != before:
            raise RuntimeError("owned save changed during reload before a new shutdown save")

        reload_log = run.out / "server-reload.log"
        log_stat = reload_log.stat()
        ack = await fake.request("shutdown", {})
        detail["reloadShutdownAcknowledgement"] = ack
        if ack != {}:
            raise RuntimeError("second Generic shutdown was not acknowledged")
        exit_code = await asyncio.to_thread(second.wait_for_exit, 45)
        detail["reloadExitCode"] = exit_code
        markers = await asyncio.to_thread(
            _fresh_shutdown_markers, reload_log, log_stat.st_dev, log_stat.st_ino, log_stat.st_size
        )
        detail["reloadShutdownMarkers"] = markers
        if exit_code != 134 or not all(markers.values()):
            raise RuntimeError("second native shutdown lacked fresh save/exit-request markers or exit 134")
        saved_again = await asyncio.to_thread(net.hash_file, save)
        detail["saveAfterSecondShutdown"] = {**saved_again, "mtimeNs": save.stat().st_mtime_ns}
        if saved_again["size"] <= 0:
            raise RuntimeError("second shutdown left an empty owned save")
    except Exception as exc:
        problems.append(str(exc))
    run.record(_result("owned-save-reload", problems, started, **detail))


async def after_shutdown(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
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
    if run.wanted("native-shutdown"):
        started = time.monotonic()
        shutdown_problems: list[str] = []
        shutdown_detail: dict[str, Any] = {}
        try:
            if run.container is None:
                raise RuntimeError("game container was not started")
            log_stat = run.server_log.stat()
            log_identity = (log_stat.st_dev, log_stat.st_ino, log_stat.st_size)
            ack = await fake.request("shutdown", {})
            shutdown_detail["acknowledgement"] = ack
            if ack != {}:
                shutdown_problems.append("Generic shutdown did not return the native acknowledgement payload")
            else:
                exit_code = await asyncio.to_thread(run.container.wait_for_exit, 45)
                shutdown_detail["exitCode"] = exit_code
                # This exact build requests exit after a flushed ACK. With the
                # ARK process behind Docker init, the engine aborts with 134.
                # Fresh markers distinguish that path from an unrelated abort.
                markers = await asyncio.to_thread(_fresh_shutdown_markers, run.server_log, *log_identity)
                for label, marker in markers.items():
                    shutdown_detail[f"{label}Marker"] = marker
                    if not marker:
                        shutdown_problems.append(f"native shutdown {label} completion marker is missing")
                if exit_code != 134:
                    shutdown_problems.append(f"native shutdown exit status {exit_code} != expected 134")
                if getattr(run, "data_dir", None) is not None:
                    save = run.data_dir / "ShooterGame/Saved/SavedArks/TheIsland.ark"
                    saved = save.stat() if save.is_file() else None
                    shutdown_detail["ownedSave"] = {
                        "path": "ShooterGame/Saved/SavedArks/TheIsland.ark",
                        "size": saved.st_size if saved else None,
                        "sha256": await asyncio.to_thread(net.sha256_file, save) if saved else None,
                        "mtimeNs": saved.st_mtime_ns if saved else None,
                    }
                    if not saved or saved.st_size <= 0:
                        shutdown_problems.append("native shutdown left no TheIsland.ark in the owned Saved tree")
        except Exception as exc:
            shutdown_problems.append(str(exc))
        run.record(_result("native-shutdown", shutdown_problems, started, **shutdown_detail))
    else:
        run.skip("native-shutdown", "not selected by --checks")
    if run.wanted("owned-save-reload"):
        await _reload_owned_save(run, fake, ws_url, ledger_inputs)
    else:
        run.skip("owned-save-reload", "not selected by --checks")
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
            path = (run.options.ark_readonly_base or run.data_dir) / entry["path"]
            if not path.is_file() or net.hash_file(path).get("sha256") != entry.get("sha256"):
                stop_problems.append(f"pinned input changed or disappeared: {entry['path']}")
            if run.options.ark_readonly_base is not None:
                owned = run.data_dir / entry["path"]
                if (
                    owned.is_symlink()
                    or not owned.is_file()
                    or net.hash_file(owned).get("sha256") != entry.get("sha256")
                ):
                    stop_problems.append(f"owned pinned input changed or disappeared: {entry['path']}")
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
                env={key: value for key, value in os.environ.items() if not key.startswith("TAKARO_")},
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
            # hash: the plugin must return before creating state or connecting.
            script = (
                "set -eu\n"
                "TAKARO_REGISTRATION_TOKEN=isolated-fixture TAKARO_WS_URL=ws://127.0.0.1:9/ "
                "TAKARO_STATE_DIR=/tmp/ark-native-state "
                "LD_PRELOAD=/fixture/plugin/libtakaro-ark-native.so "
                "/fixture/server/ShooterGame/Binaries/Linux/ShooterGameServer 2\n"
                "if [ -e /tmp/ark-native-state ]; then echo unexpected_native_state; exit 1; fi\n"
                "echo native_state=absent\necho fixture_exit=0\n"
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
            if direct.returncode or direct.stdout != "native_state=absent\nfixture_exit=0\n":
                problems.append("native preload guard did not return safely before any connector state")
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
    prepare_readonly_base=prepare_readonly_base,
    negative=negative,
)
