"""The lifecycle checks the base run does not carry, and the hosted Takaro client.

``reconnect``, ``restart`` and ``negative-wrong-target`` all need something the ten base
checks never do: a second boot on the same data dir, or a socket closed from Takaro's side.
The algorithms are game-agnostic; a game's ``verify`` hooks module supplies the specifics
(which sibling target exists, what its loader says when it refuses a jar, what a hosted
gameserver is called).
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import net, redact
from ..install.ledger import ledger_path
from . import checks

CHECK_IDS = ("reconnect", "restart", "negative-wrong-target", "hosted-registration")

RECONNECT_BUDGET = 20.0
IDENTIFY_BUDGET = 180.0
SHUTDOWN_BUDGET = 120.0
REGISTRATION_BUDGET = 60.0
# A server that refused an artifact exits on its own; this is how long that is given.
EXIT_BUDGET = 30.0

CLOSED_LINE = re.compile(r"WebSocket closed \(code=1001")
IDENTIFIED_LINE = re.compile(r"Identified successfully")
UUID_ANYWHERE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class Refusal:
    """The log signatures that prove a server refused an artifact built for another target."""

    loader: re.Pattern[str]
    detail: re.Pattern[str]
    guard: re.Pattern[str]


# --------------------------------------------------------------------------- log helpers


def count_lines(log_file: Path, pattern: re.Pattern[str]) -> int:
    if not log_file.is_file():
        return 0
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if pattern.search(line))


def wait_for_count(log_file: Path, pattern: re.Pattern[str], minimum: int, timeout: float, alive: Any) -> int:
    """Poll until the pattern has been logged ``minimum`` times, and report how often it was."""
    deadline = time.monotonic() + timeout
    seen = count_lines(log_file, pattern)
    while seen < minimum and time.monotonic() < deadline:
        if not alive():
            return count_lines(log_file, pattern)
        time.sleep(0.5)
        seen = count_lines(log_file, pattern)
    return seen


async def identify_within(fake: Any, minimum: int, timeout: float, alive: Any | None = None) -> float | None:
    """Milliseconds until the ``minimum``-th identify frame, or ``None`` when it never came.

    A container that has exited will never identify, so the wait ends there rather than
    burning the whole budget.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    checked_alive = started
    while fake.identify_count < minimum:
        now = loop.time()
        if now >= deadline:
            return None
        if alive is not None and now - checked_alive >= 2.0:
            checked_alive = now
            if not await asyncio.to_thread(alive) and fake.identify_count < minimum:
                return None
        await asyncio.sleep(0.2)
    return (loop.time() - started) * 1000


# --------------------------------------------------------------------------- reconnect


async def check_reconnect(run: Any, fake: Any, alive: Any) -> checks.CheckResult:
    """Takaro drops the socket; the connector must come back and be usable again."""
    with checks._Timer() as timer:
        problems: list[str] = []
        before = fake.identify_count
        await fake.disconnect(1001, "going away")
        reidentify_ms = await identify_within(fake, before + 1, RECONNECT_BUDGET, alive)
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
        closed = await asyncio.to_thread(checks.wait_for_line, run.server_log, CLOSED_LINE, 15, alive)
        if not closed:
            problems.append("the server log never reported the close with code 1001")
        identified = await asyncio.to_thread(wait_for_count, run.server_log, IDENTIFIED_LINE, 2, 15, alive)
        if identified < 2:
            problems.append(f"the server logged 'Identified successfully' {identified} time(s), expected at least 2")
    return checks.CheckResult(
        "reconnect",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "closeCode": 1001,
            "reidentifyMs": int(reidentify_ms) if reidentify_ms is not None else None,
            "identifyCountBefore": before,
            "identifyCountAfter": fake.identify_count,
            "identifiedLines": identified,
            "testReachability": reachable,
            "problems": problems,
        },
        {"file": run.server_log.name, "line": closed[0]} if closed else {"file": run.server_log.name},
    )


# --------------------------------------------------------------------------- restart


async def check_restart(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> checks.CheckResult:
    """The same data dir boots a second time, unchanged, and the connector comes back."""
    with checks._Timer() as timer:
        problems: list[str] = []
        ledger_before = net.sha256_file(ledger_path(run.data_dir))
        before = fake.identify_count
        if run.container is not None:
            run.container.remove()

        container = run.boot(ws_url, suffix="-restart", log_name="server-restart.log")
        run.extra_logs.append(container.log_file)
        startup = await asyncio.to_thread(
            checks.check_startup,
            container.log_file,
            run.options.startup_timeout,
            container.alive,
            run.data_dir,
            ledger_inputs,
        )
        if startup.status != "pass":
            problems.append(
                "the second boot did not finish: " + "; ".join(startup.detail.get("replaced") or ["no Done line"])
            )

        boot_ms = await identify_within(fake, before + 1, IDENTIFY_BUDGET, container.alive)
        if boot_ms is None:
            problems.append(f"the connector never identified within {IDENTIFY_BUDGET:.0f} s of the second boot")

        ledger_after = net.sha256_file(ledger_path(run.data_dir))
        if ledger_after != ledger_before:
            problems.append("the install ledger changed across the restart")

        try:
            await fake.request("shutdown", {}, timeout=30)
        except Exception:  # noqa: BLE001 - the socket closing first is normal here
            pass
        code = await asyncio.to_thread(container.wait_for_exit, SHUTDOWN_BUDGET)
        if code != 0:
            problems.append(f"the restarted container exited {code}, expected 0")
        container.remove()
    return checks.CheckResult(
        "restart",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "bootDurationMs": int(boot_ms) if boot_ms is not None else None,
            "ledgerSha256": ledger_after,
            "ledgerUnchanged": ledger_after == ledger_before,
            "startup": startup.detail,
            "exitCode": code,
            "problems": problems,
        },
        {"file": container.log_file.name, "line": startup.log.get("line") if startup.log else None},
    )


# --------------------------------------------------------------------------- negative


async def check_negative_wrong_target(
    run: Any,
    fake: Any,
    ws_url: str,
    manifest: dict[str, Any],
    *,
    sibling: Any,
    refusal: Refusal,
) -> checks.CheckResult:
    """The sibling target's artifact must be refused by this server, never accepted."""
    rows = [row for row in manifest["artifacts"] if row["target"] == sibling.id]
    source = run.options.artifacts / rows[0]["file"] if rows else None
    if not rows or source is None or not source.is_file():
        # What a CI leg sees: it downloads its own target's dist and nothing else.
        return checks.CheckResult(
            "negative-wrong-target",
            "skip",
            0,
            {"reason": f"sibling artifact {sibling.id} not in the build manifest", "siblingTarget": sibling.id},
        )

    with checks._Timer() as timer:
        ledger = json.loads(ledger_path(run.data_dir).read_text(encoding="utf-8"))
        deployed = run.data_dir / ledger["artifact"]["path"]
        aside = run.data_dir / ".takaro" / "negative-aside"
        aside.mkdir(parents=True, exist_ok=True)
        shutil.move(str(deployed), str(aside / deployed.name))
        wrong = deployed.parent / rows[0]["file"]
        shutil.copy2(source, wrong)

        before = fake.identify_count
        container = run.boot(ws_url, suffix="-negative", log_name="server-negative.log")
        run.extra_logs.append(container.log_file)
        observed = await asyncio.to_thread(
            _watch_for_refusal, container, refusal, run.options.startup_timeout, lambda: fake.identify_count
        )
        refused_by, refusal_line, line_number, exit_code = observed
        during = fake.identify_count - before
        problems: list[str] = []
        if during:
            problems.append(f"the wrong artifact identified {during} time(s); it must be refused")
        if refused_by is None:
            problems.append("neither the loader nor the connector guard refused the sibling artifact")
        if container.alive():
            container.remove()

        wrong.unlink(missing_ok=True)
        shutil.move(str(aside / deployed.name), str(deployed))
    return checks.CheckResult(
        "negative-wrong-target",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "siblingTarget": sibling.id,
            "siblingArtifact": rows[0]["file"],
            "refusedBy": refused_by,
            "refusalLine": refusal_line,
            "identifyFramesDuringNegative": during,
            "exitCode": exit_code,
            "problems": problems,
        },
        {"file": container.log_file.name, "line": line_number} if line_number else {"file": container.log_file.name},
    )


def _watch_for_refusal(
    container: Any, refusal: Refusal, timeout: float, identify_count: Any
) -> tuple[str | None, str | None, int | None, int | None]:
    """Wait for a refusal, an identify or the container's exit, whichever comes first."""
    deadline = time.monotonic() + timeout
    started_identifies = identify_count()
    refused = _refusal_in(container, refusal)
    while refused is None and time.monotonic() < deadline:
        if identify_count() > started_identifies:
            # The wrong jar was accepted: no exit code to wait for, and the row fails.
            return None, None, None, None
        if not container.alive():
            break
        time.sleep(1)
        refused = _refusal_in(container, refusal)
    # A refused server is on its way out; its exit code is part of the evidence, and the
    # log is complete once it is gone, so look once more either way.
    code = container.wait_for_exit(EXIT_BUDGET)
    refused = _refusal_in(container, refusal) or refused
    if refused is None:
        return None, None, None, code
    refused_by, (line_number, line) = refused
    return refused_by, line, line_number, code


def _refusal_in(container: Any, refusal: Refusal) -> tuple[str, tuple[int, str]] | None:
    """Whichever refusal the log carries so far: the loader's, or the connector guard's."""
    found = checks.find_line(container.log_file, refusal.loader)
    if found:
        return "loader", (checks.find_line(container.log_file, refusal.detail) or found)
    guard = checks.find_line(container.log_file, refusal.guard)
    return ("target-guard", guard) if guard else None


# --------------------------------------------------------------------------- redaction


def redact_retained(out_dir: Path, extra: list[str], *, names: set[str] | None = None) -> list[str]:
    """Rewrite the retained files so no host, id or token survives in the evidence.

    ``names`` narrows the pass to a single file. A hosted run redacts its logs before the
    report is built, so ``logs[].sha256`` describes the bytes that were actually kept, and
    redacts the report itself afterwards.
    """
    rewritten: list[str] = []
    for path in sorted(out_dir.glob("*")):
        if not path.is_file() or (names is not None and path.name not in names):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = UUID_ANYWHERE.sub("<uuid>", redact.redact(text, extra))
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")
            rewritten.append(path.name)
    return rewritten


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


# --------------------------------------------------------------------------- hosted Takaro


class HostedTakaro:
    """The handful of Takaro API calls a hosted verification needs, over stdlib urllib."""

    def __init__(self, host: str, username: str, password: str, domain_id: str, *, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self._username = username
        self._password = password
        self._domain_id = domain_id
        self._timeout = timeout
        self._bearer: str | None = None
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {
            "User-Agent": net.USER_AGENT,
            "Accept": "application/json",
            "X-Takaro-Domain": self._domain_id,
        }
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.host}{path}", data=data, headers=headers, method=method)
        with self._opener.open(request, timeout=self._timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def login(self) -> None:
        payload = self._call("POST", "/login", {"username": self._username, "password": self._password})
        token = (payload.get("data") or {}).get("token") if isinstance(payload, dict) else None
        if isinstance(token, str) and token:
            self._bearer = token

    def find(self, identity: str) -> list[str]:
        payload = self._call("POST", "/gameserver/search", {"filters": {"identityToken": [identity]}})
        rows = payload.get("data") or [] if isinstance(payload, dict) else []
        return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]

    def reachability(self, server_id: str) -> bool:
        payload = self._call("POST", f"/gameserver/{server_id}/reachability", {})
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        return bool(data.get("connectable"))

    def players(self, server_id: str) -> list[Any]:
        payload = self._call("GET", f"/gameserver/{server_id}/players")
        data = payload.get("data") if isinstance(payload, dict) else None
        return list(data) if isinstance(data, list) else []

    def shutdown(self, server_id: str) -> None:
        self._call("POST", f"/gameserver/{server_id}/shutdown", {})

    def delete(self, server_id: str) -> None:
        try:
            self._call("DELETE", f"/gameserver/{server_id}")
        except urllib.error.URLError:
            pass
