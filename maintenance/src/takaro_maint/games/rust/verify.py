"""Rust's verification hooks: what the generic runner cannot know about this game.

The differences from a Minecraft run are all about Carbon. The server says it is up with
its own line. The connector is not a jar that either loaded or did not: it is a ``.cs``
file the framework *compiles* at load, so the one check nothing else can stand in for is
that the compile succeeded and the plugin the manifest built is the plugin that loaded.
The runtime identity arrives on two lines rather than one — Rust prints its wire protocol,
Carbon prints its own version — so it is scanned and merged instead of parsed from the
first match. And Rust's catalogues are this game's own, so ``items`` and ``entities``
replace the Minecraft-shaped ``catalog-items``/``catalog-entities`` and spot-check Rust
names.

``connector-load``, ``catalog-items``, ``catalog-entities`` and the base ``shutdown`` are
never selected by default: the first looks for a target-check line only the Minecraft
connector writes, the two catalogue checks spot-check Minecraft names, and ``shutdown``
gates on an exit code that Rust's Unity teardown does not give. :data:`UNSUPPORTED_CHECKS`
is what keeps them out; the runner applies it to every game. The coverage boundary this run
proves is in games/rust/README.md.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ...install.ledger import artifacts_of, read_ledger
from ...verify import checks, checks_lifecycle
from ...verify.hooks import GameHooks

PLUGIN_ROLE = "plugin"

CHECK_IDS = ("carbon-compile", "items", "entities", "action", "reconnect", "stop")

#: "Server startup complete" is the last line of a successful boot; everything before it
#: can appear on a boot that then dies.
READY_LINE = re.compile(r"Server startup complete")

#: Base checks this connector cannot satisfy, and the check that stands in for each.
#: A run that names no ``--checks`` excludes these rather than failing them.
UNSUPPORTED_CHECKS = {
    "connector-load": ("the load line is a Minecraft connector's; `carbon-compile` asserts Carbon loaded the plugin"),
    "catalog-items": ("spot-checks a Minecraft item id; `items` spot-checks a Rust one"),
    "catalog-entities": ("spot-checks a Minecraft entity id; `entities` spot-checks a Rust one"),
    "shutdown": ("asserts an exit code this Unity teardown does not give; `stop` asserts the shutdown"),
}

LOADED_LINE = re.compile(r"Loaded plugin TakaroConnector v(?P<version>[^ ]+) by Takaro \[(?P<ms>[0-9]+)ms\]")
COMPILE_FAILED = re.compile(r"Failed compiling '?TakaroConnector|error CS[0-9]{4}")
PROTOCOL_LINE = re.compile(r"^Protocol:\s*[0-9][0-9.]*")
CARBON_LINE = re.compile(r"Initialized Carbon\.Startup [0-9]+(?:\.[0-9]+)+")
#: Carbon's self-updater announces itself before it replaces anything. The hard guard is
#: `startup.inputsIntact` over the ledger witnesses; this line is what names the cause.
#:
#: Carbon talks about self-updating on every boot, so the pattern has to match only the
#: boots on which it happened. The three lines a real boot writes are
#: "… is out of date and now self-updating - Production […] [2.0.257 -> 2.0.259]",
#: "Updating Carbon…" and "… finished self-updating 76 files."; the two it writes when it
#: does not are "Skipped self-updating process as it's disabled in the config." and
#: "… is up to date, no self-updating necessary."
SELF_UPDATE_LINE = re.compile(
    r"now self-updating|finished self-updating|^\s*Updating Carbon\b|Downloading Carbon", re.I
)

#: What a finished shutdown looks like in the console, in the order Rust writes it.
SAVED_LINE = re.compile(r"^Saving complete")
QUIT_LINE = re.compile(r"\[Raknet\] Server Shutting Down \(quit\)|^Quitting")
UNLOADED_LINE = re.compile(r"Unloaded plugin TakaroConnector")

COMPILE_BUDGET = 180.0
ACTION_BUDGET = 30.0
QUIT_BUDGET = 120.0

ITEM_SPOT = ("rifle.ak", "Assault Rifle")
ENTITY_SPOT = ("bear", "Bear")


def scan_runtime_identity(adapter: Any, log_file: Path) -> dict[str, Any]:
    """The booted identity, merged from the two lines that each carry half of it."""
    identity: dict[str, Any] = {}
    if not log_file.is_file():
        return identity
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not (PROTOCOL_LINE.search(line.strip()) or CARBON_LINE.search(line)):
                continue
            parsed = adapter.parse_runtime_identity(line) or {}
            for key, value in parsed.items():
                if value is not None and identity.get(key) is None:
                    identity[key] = value
    return identity


#: Oxide's ``VersionNumber`` -- which Carbon uses for ``[Info]`` -- parses a version as
#: three integers, so the build stamps the numeric head of the connector version into the
#: attribute ("0.0.5-dev.abc1234" -> "0.0.5") and a dev build is loadable at all. The check
#: compares like with like by reducing the recorded version the same way.
_NUMERIC_HEAD = re.compile(r"^[0-9]+(?:\.[0-9]+)*")


def plugin_version(connector_version: str) -> str:
    """What ``[Info]`` can carry for this connector version: three integers, or 0.0.0."""
    found = _NUMERIC_HEAD.match(connector_version)
    parts = found.group(0).split(".") if found else []
    parts = (parts + ["0", "0", "0"])[:3]
    return ".".join(parts)


def _deployed_version(run: Any) -> str | None:
    """The plugin version `deploy`'s record implies, which is what Carbon must have loaded."""
    ledger = read_ledger(run.data_dir)
    if ledger is None:
        return None
    # Rust has one component role; its row is the plugin's.
    rows = [row for row in artifacts_of(ledger.data) if row.get("role") == PLUGIN_ROLE]
    version = rows[0].get("connectorVersion") if rows else None
    return plugin_version(str(version)) if version else None


# --------------------------------------------------------------------------- local hooks


def before_boot(run: Any, takaro_env: dict[str, str]) -> None:
    """Settle this run's check selection before anything the selection governs runs.

    ``build`` is recorded before the boot and is in the default selection either way; every
    other check this narrows runs after this hook. Rust needs nothing else on disk here --
    the connector reads its Takaro credentials from the container's environment.
    """
    del takaro_env


async def after_protocol(run: Any, fake: Any, alive: Any) -> None:
    for check_id, coroutine in (
        ("carbon-compile", lambda: _check_carbon_compile(run, alive)),
        ("items", lambda: checks.check_catalog(fake, "listItems", "items", ITEM_SPOT)),
        ("entities", lambda: checks.check_catalog(fake, "listEntities", "entities", ENTITY_SPOT)),
        ("action", lambda: _check_action(run, fake, alive)),
        ("reconnect", lambda: checks_lifecycle.check_reconnect(run, fake, alive)),
    ):
        if run.wanted(check_id):
            run.record(await coroutine())
        else:
            run.skip(check_id, "not selected by --checks")


async def _check_carbon_compile(run: Any, alive: Any) -> checks.CheckResult:
    """Carbon compiled the deployed source, loaded it, and did not swap itself out first."""
    with checks._Timer() as timer:
        problems: list[str] = []
        loaded = await asyncio.to_thread(checks.wait_for_line, run.server_log, LOADED_LINE, COMPILE_BUDGET, alive)
        version = None
        compile_ms = None
        if loaded is None:
            problems.append(f"the server never logged 'Loaded plugin TakaroConnector' within {COMPILE_BUDGET:.0f} s")
        else:
            found = LOADED_LINE.search(loaded[1])
            if found:
                version = found.group("version")
                compile_ms = int(found.group("ms"))
        expected = _deployed_version(run)
        if version is not None and expected is not None and version != expected:
            problems.append(f"the server loaded TakaroConnector v{version}, the build manifest implies {expected}")
        failed = await asyncio.to_thread(checks.find_line, run.server_log, COMPILE_FAILED)
        if failed:
            problems.append(f"a compile error is in the log: {failed[1].strip()[:200]}")
        updated = await asyncio.to_thread(checks.find_line, run.server_log, SELF_UPDATE_LINE)
        if updated:
            problems.append(f"Carbon reported a self-update: {updated[1].strip()[:200]}")
    return checks.CheckResult(
        "carbon-compile",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "modVersion": version,
            "expectedVersion": expected,
            "compileMs": compile_ms,
            "note": "Carbon compiles carbon/plugins/TakaroConnector.cs at load; this is the runtime build",
            "problems": problems,
        },
        {"file": run.server_log.name, "line": loaded[0]} if loaded else {"file": run.server_log.name},
    )


async def _check_action(run: Any, fake: Any, alive: Any) -> checks.CheckResult:
    """A representative action: a Takaro broadcast reaches the server and it says so."""
    marker = f"takaro-verify-{run.options.run_id}-action"
    with checks._Timer() as timer:
        problems: list[str] = []
        result: Any = None
        try:
            result = await fake.request("sendMessage", {"message": marker})
        except Exception as exc:  # noqa: BLE001 - reported as a check failure
            problems.append(f"sendMessage failed: {exc}")
        found = await asyncio.to_thread(
            checks.wait_for_line, run.server_log, re.compile(re.escape(marker)), ACTION_BUDGET, alive
        )
        if not found:
            problems.append(f"the server log never echoed the broadcast '{marker}'")
    return checks.CheckResult(
        "action",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "action": "sendMessage",
            "message": marker,
            "result": result,
            "echo": found[1].strip() if found else None,
            "problems": problems,
        },
        {"file": run.server_log.name, "line": found[0]} if found else {"file": run.server_log.name},
    )


async def after_shutdown(run: Any, fake: Any, ws_url: str, ledger_inputs: list[dict[str, Any]]) -> None:
    del ws_url, ledger_inputs
    if not run.wanted("stop"):
        run.skip("stop", "not selected by --checks")
        return
    run.record(await _check_stop(run, fake))


async def _check_stop(run: Any, fake: Any) -> checks.CheckResult:
    """What Rust's shutdown really leaves behind, since its exit code says nothing.

    The base ``shutdown`` check gates on the container's exit code, and Rust's is not a
    signal: the Unity player segfaults inside its own teardown on some runs (139) *after*
    it has saved the world, unloaded the plugins and printed ``Quitting``, and exits 0 on
    others with the same lines in the same order. So what is asserted here is the sequence
    the server actually writes -- Takaro's ``shutdown`` reached it, it saved, it unloaded
    the connector, it quit -- and that the process is gone afterwards. The exit code is
    recorded rather than judged.
    """
    with checks._Timer() as timer:
        problems: list[str] = []
        note = "shutdown response received"
        try:
            # This check replaces the base `shutdown`, so it is what asks the server to go.
            await fake.request("shutdown", {}, timeout=30)
        except Exception as exc:  # noqa: BLE001 - the socket closing first is normal here
            note = f"the connection closed before the response arrived ({exc}); the log lines are the gate"
        container = run.container
        alive = container.alive if container is not None else (lambda: False)
        found: dict[str, int | None] = {}
        for name, pattern, required in (
            ("saved", SAVED_LINE, True),
            ("unloadedConnector", UNLOADED_LINE, True),
            ("quit", QUIT_LINE, True),
        ):
            hit = await asyncio.to_thread(checks.wait_for_line, run.server_log, pattern, QUIT_BUDGET, alive)
            found[name] = hit[0] if hit else None
            if hit is None and required:
                problems.append(f"the server never logged its {name} line within {QUIT_BUDGET:.0f} s")
        code: int | None = None
        if container is not None:
            code = await asyncio.to_thread(container.wait_for_exit, QUIT_BUDGET)
            if code is None:
                problems.append(f"the server was still running {QUIT_BUDGET:.0f} s after it said it was quitting")
    return checks.CheckResult(
        "stop",
        "pass" if not problems else "fail",
        timer.elapsed_ms,
        {
            "exitCode": code,
            "lines": found,
            "shutdownRequest": note,
            "note": (
                "Rust's exit code is not a shutdown signal: the Unity player segfaults in its own "
                "teardown on some runs, after saving, unloading and quitting. The lines are the gate."
            ),
            "problems": problems,
        },
        _where(run, found.get("quit")),
    )


def _where(run: Any, line: int | None) -> dict[str, Any]:
    """The report's ``log`` pointer, which carries a line number only when there is one."""
    where: dict[str, Any] = {"file": run.server_log.name}
    if line is not None:
        where["line"] = line
    return where


#: What this game contributes to a verification run; the runner reads nothing else.
HOOKS = GameHooks(
    ready_line=READY_LINE,
    check_ids=CHECK_IDS,
    unsupported_checks=UNSUPPORTED_CHECKS,
    before_boot=before_boot,
    after_protocol=after_protocol,
    after_shutdown=after_shutdown,
    scan_runtime_identity=scan_runtime_identity,
)
