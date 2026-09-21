"""A stand-in for ``steamcmd``, plus the helpers that bend its answer.

Run as a program it behaves like the real tool on a non-TTY: console noise, the ``AppID :``
header line, the app's KeyValues block, a trailer — and, when asked to, the failure modes
that matter (a truncated block, a connection failure, a hang, an app it knows nothing
about). Imported, it is how a test says "Steam moved the public branch to this build" or
"this branch now needs a password": the recorded 294420 document is parsed, mutated in
memory and written back through the same VDF writer, so no test hand-writes Steam's
spelling and every scenario goes through the real parser.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from takaro_maint.steam import vdf  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures/providers/steam"

ROOT_ENV = "FAKE_STEAMCMD_ROOT"
LOG_ENV = "FAKE_STEAMCMD_LOG"
TRUNCATE_ENV = "FAKE_STEAMCMD_TRUNCATE"
FAIL_ENV = "FAKE_STEAMCMD_FAIL"
HANG_ENV = "FAKE_STEAMCMD_HANG"

NOISE = (
    "Redirecting stderr to '/tmp/steam/logs/stderr.txt'\n"
    "[  0%] Checking for available updates...\n"
    "Steam Console Client (c) Valve Corporation - version 1758240593\n"
    "Logging in user 'anonymous' to Steam Public...\n"
    "Connecting anonymously to Steam Public...\x1b[0mOK\n"
    "\x1b[0mWaiting for user info...\x1b[0mOK\n"
    "\x1b[0m"
)
TRAILER = "Unloading Steam API...\x1b[0mOK\n\x1b[0m\n"

CHANGE_NUMBER = 39026857
LAST_CHANGE = "Mon Sep 21 12:53:46 2026"


# -- the fixture, as a document a test can bend --------------------------------
def recorded(app: int = 294420) -> dict[str, Any]:
    """The recorded app block, freshly parsed so each test owns its own copy."""
    text = (FIXTURES / str(app) / "app_info.vdf").read_text(encoding="utf-8")
    block: dict[str, Any] = vdf.parse(text)[str(app)]
    return block


def serve(root: Path, app: int, document: dict[str, Any]) -> Path:
    """Write ``document`` (one app block) where the fake will find it for ``app``."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{app}.vdf"
    path.write_text(vdf.dump({str(app): document}), encoding="utf-8")
    return path


def branches(document: dict[str, Any]) -> dict[str, Any]:
    return document.setdefault("depots", {}).setdefault("branches", {})


def move_head(
    document: dict[str, Any],
    label: str,
    buildid: int | str,
    manifests: dict[str, str] | None = None,
    timeupdated: int | str | None = None,
) -> dict[str, Any]:
    """Point an existing branch at another build, and its depots at other manifests."""
    branch = branches(document).setdefault(label, {})
    branch["buildid"] = str(buildid)
    if timeupdated is not None:
        branch["timeupdated"] = str(timeupdated)
    for depot, gid in (manifests or {}).items():
        set_manifest(document, depot, label, gid)
    return document


def add_branch(
    document: dict[str, Any],
    label: str,
    buildid: int | str,
    manifests: dict[str, str] | None = None,
    *,
    pwdrequired: bool = False,
    description: str | None = None,
    timeupdated: int | str | None = 1788200000,
    timebuildupdated: int | str | None = 1788100000,
) -> dict[str, Any]:
    """List a branch that was not there before — a new preview, or a protected one."""
    branch: dict[str, Any] = {"buildid": str(buildid)}
    if description is not None:
        branch["description"] = description
    if timeupdated is not None:
        branch["timeupdated"] = str(timeupdated)
    if timebuildupdated is not None:
        branch["timebuildupdated"] = str(timebuildupdated)
    if pwdrequired:
        branch["pwdrequired"] = "1"
    branches(document)[label] = branch
    for depot, gid in (manifests or {}).items():
        set_manifest(document, depot, label, gid, encrypted=pwdrequired)
    return document


def remove_branch(document: dict[str, Any], label: str) -> dict[str, Any]:
    """Stop listing a branch: what an anonymous login sees of a branch gone private."""
    branches(document).pop(label, None)
    for depot in document.get("depots", {}).values():
        if isinstance(depot, dict):
            (depot.get("manifests") or {}).pop(label, None)
            (depot.get("encryptedmanifests") or {}).pop(label, None)
    return document


def set_private_branches(document: dict[str, Any], flag: bool) -> dict[str, Any]:
    document.setdefault("depots", {})["privatebranches"] = "1" if flag else "0"
    return document


def set_manifest(
    document: dict[str, Any],
    depot: str,
    label: str,
    gid: str,
    *,
    size: int = 17607545324,
    download: int = 14354376816,
    encrypted: bool = False,
) -> dict[str, Any]:
    """Give one depot a content manifest on one branch, plainly or behind a password."""
    entry = document.setdefault("depots", {}).setdefault(str(depot), {})
    where = "encryptedmanifests" if encrypted else "manifests"
    other = "manifests" if encrypted else "encryptedmanifests"
    if isinstance(entry.get(other), dict):
        entry[other].pop(label, None)
    entry.setdefault(where, {})[label] = (
        {"gid": str(gid)} if encrypted else {"gid": str(gid), "size": str(size), "download": str(download)}
    )
    return document


def environment(root: Path, log: Path | None = None, **extra: str) -> dict[str, str]:
    """The environment that points ``takaro-maint`` at this fake instead of steamcmd."""
    env = {
        "TAKARO_MAINT_STEAMCMD": f"{sys.executable} {Path(__file__).resolve()}",
        ROOT_ENV: str(root),
    }
    if log is not None:
        env[LOG_ENV] = str(log)
    env.update(extra)
    return env


def argv_log(path: Path) -> list[list[str]]:
    """Every call the fake was asked to make, oldest first."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- the program ---------------------------------------------------------------
def _app_of(argv: list[str]) -> int | None:
    for index, part in enumerate(argv):
        if part == "+app_info_print" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                return None
    return None


def _document_for(root: Path, app: int) -> str | None:
    """The block this fake serves for ``app``: a test's copy first, the recording second."""
    for candidate in (root / f"{app}.vdf", root / str(app) / "app_info.vdf", FIXTURES / str(app) / "app_info.vdf"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


def _count(log: Path | None) -> int:
    """How many times this fake has been called, kept beside the argv log."""
    counter = Path(str(log) + ".count") if log is not None else None
    if counter is None:
        return 1
    seen = int(counter.read_text(encoding="utf-8").strip() or "0") if counter.is_file() else 0
    seen += 1
    counter.write_text(str(seen), encoding="utf-8")
    return seen


def main(argv: list[str]) -> int:
    log = Path(os.environ[LOG_ENV]) if os.environ.get(LOG_ENV) else None
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    call = _count(log)

    if os.environ.get(HANG_ENV):
        time.sleep(float(os.environ[HANG_ENV]))

    sys.stdout.write(NOISE)
    if os.environ.get(FAIL_ENV):
        sys.stdout.write("Connecting anonymously to Steam Public...FAILED (Connection failed)\n")
        return 1

    app = _app_of(argv)
    if app is None:
        sys.stdout.write("Usage: +app_info_print <appid>\n")
        return 1

    root = Path(os.environ.get(ROOT_ENV) or FIXTURES)
    header = f"AppID : {app}, change number : {CHANGE_NUMBER}/{CHANGE_NUMBER}, last change : {LAST_CHANGE}\n"

    truncate = int(os.environ.get(TRUNCATE_ENV) or 0)
    if call <= truncate:
        sys.stdout.write(header + f'"{app}"\n{{\n}}\n' + TRAILER)
        return 0

    text = _document_for(root, app)
    if text is None:
        sys.stdout.write(f"No app info for AppID {app} found, requesting...\n")
        sys.stdout.write(header + f'"{app}"\n{{\n}}\n' + TRAILER)
        return 0

    sys.stdout.write(header + text + TRAILER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
