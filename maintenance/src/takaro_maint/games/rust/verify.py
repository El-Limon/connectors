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

``connector-load`` is never selected: it looks for a target-check line only the Minecraft
connector writes. The coverage boundary this run proves is in games/rust/README.md.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ...install.ledger import read_ledger
from ...verify import checks, checks_lifecycle

CHECK_IDS = ("carbon-compile", "items", "entities", "action", "reconnect")

#: "Server startup complete" is the last line of a successful boot; everything before it
#: can appear on a boot that then dies.
READY_LINE = re.compile(r"Server startup complete")
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

COMPILE_BUDGET = 180.0
ACTION_BUDGET = 30.0

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
    artifact = ledger.data.get("artifact") or {}
    version = artifact.get("connectorVersion")
    return plugin_version(str(version)) if version else None


# --------------------------------------------------------------------------- local hooks


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
            problems.append(
                f"the server loaded TakaroConnector v{version}, the build manifest implies {expected}"
            )
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
