"""Reading a Steam app's published metadata through ``steamcmd``.

Steam has no "what is the current build of this branch" endpoint. The store page is HTML,
the web API answers about apps and not about branches, and the only published source of a
branch's build id and its depots' content manifests is ``steamcmd +app_info_print`` — the
same tool the installs already depend on. So discovery runs it, anonymously, read-only,
and turns the KeyValues document it prints into the small typed shapes below.

Two things about that tool shape this module:

*It is slow and it self-updates.* A cold run downloads ~40 MB of steamcmd before it says
anything, which is why the timeout is minutes rather than seconds.

*It truncates when it is not attached to a terminal.* A run can exit 0 having printed an
app block with nothing in it. That is indistinguishable from a real answer to anything
that greps the output, so the block is parsed, the empty one is recognised, and the whole
command is retried exactly once. Never twice: a second empty answer is a broken upstream,
and hammering it with retries would only make a scan take ten minutes to say so.

No credential is ever an argument here: ``app_info_print`` is an anonymous read. A branch
that needs a password is resolved through DepotDownloader by the provider, which already
knows how to pass one without printing it.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import output, redact
from ..exit_codes import UpstreamUnavailable
from . import vdf

#: A command line, not a path: the live runs point this at a container, tests at a fake.
COMMAND_ENV = "TAKARO_MAINT_STEAMCMD"
DEFAULT_COMMAND = "steamcmd"

#: Minutes, because a cold steamcmd downloads itself before it answers. Measured: a cold
#: container run of app 294420 takes about two minutes, and 120 s was not enough for it.
DEFAULT_TIMEOUT = 300.0
DEFAULT_ATTEMPTS = 2


@dataclass(frozen=True)
class Manifest:
    """One depot's content on one branch: the id that pins the bytes, and their size."""

    gid: str
    size: int | None = None
    download: int | None = None


@dataclass(frozen=True)
class Depot:
    """A depot that can be pinned. Shared depots, which carry no manifests, are not here."""

    id: str
    oslist: str | None = None
    manifests: dict[str, Manifest] = field(default_factory=dict)
    encrypted: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Branch:
    """One published branch of an app, exactly as Steam labels it."""

    label: str
    buildid: int
    timeupdated: int | None = None
    timebuildupdated: int | None = None
    description: str | None = None
    pwdrequired: bool = False

    @property
    def published_at(self) -> int | None:
        """When this branch last moved, preferring the branch pointer over the build."""
        return self.timeupdated if self.timeupdated is not None else self.timebuildupdated


@dataclass(frozen=True)
class AppInfo:
    """What one ``app_info_print`` run said about an app."""

    app: int
    name: str | None
    change_number: int | None
    last_change: str | None
    private_branches: bool
    branches: dict[str, Branch]
    depots: dict[str, Depot]
    raw: dict[str, Any] = field(default_factory=dict)


def command() -> list[str]:
    """The command line that runs steamcmd, overridable like the other external tools."""
    return shlex.split(os.environ.get(COMMAND_ENV) or DEFAULT_COMMAND)


def iso(epoch: int) -> str:
    """A Steam timestamp as the one clock format observations and checkpoints use."""
    return datetime.fromtimestamp(int(epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_digest(manifests: dict[str, str]) -> str:
    """Eight hex characters binding one ``{depot: manifest}`` set, for a revision id.

    A build id alone is not an identity: a publisher can replace a depot's content under
    the same build, and two operating systems' depots share one. Folding the watched
    depots' manifest ids into the revision makes either of those a new revision, and
    keeps the revision short enough to read in an issue title.
    """
    payload = ";".join(f"{depot}:{manifests[depot]}" for depot in sorted(manifests))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _manifests(block: dict[str, Any]) -> dict[str, Manifest]:
    found: dict[str, Manifest] = {}
    for label, entry in (block.get("manifests") or {}).items():
        if not isinstance(entry, dict) or not entry.get("gid"):
            continue
        found[str(label)] = Manifest(
            gid=str(entry["gid"]),
            size=_int(entry.get("size")),
            download=_int(entry.get("download")),
        )
    return found


def _depots(block: dict[str, Any]) -> dict[str, Depot]:
    """Every depot whose content can be pinned, keyed by id.

    ``depots`` is a mixed bag: real depots, depots borrowed from another app through
    ``depotfromapp`` (no manifests of their own), the ``branches`` block, and bare scalars
    such as ``overridescddb`` and ``privatebranches``. Only the ones that actually name
    content are returned.
    """
    found: dict[str, Depot] = {}
    for depot_id, entry in block.items():
        if depot_id in ("branches", "privatebranches") or not isinstance(entry, dict):
            continue
        manifests = _manifests(entry)
        encrypted = {str(label) for label in (entry.get("encryptedmanifests") or {})}
        if not manifests and not encrypted:
            continue
        config = entry.get("config")
        oslist = str(config["oslist"]) if isinstance(config, dict) and config.get("oslist") else None
        found[str(depot_id)] = Depot(
            id=str(depot_id),
            oslist=oslist,
            manifests=manifests,
            encrypted=frozenset(encrypted),
        )
    return found


def _branches(block: dict[str, Any]) -> dict[str, Branch]:
    found: dict[str, Branch] = {}
    for label, entry in (block.get("branches") or {}).items():
        if not isinstance(entry, dict):
            continue
        buildid = _int(entry.get("buildid"))
        if buildid is None:
            continue
        found[str(label)] = Branch(
            label=str(label),
            buildid=buildid,
            timeupdated=_int(entry.get("timeupdated")),
            timebuildupdated=_int(entry.get("timebuildupdated")),
            description=str(entry["description"]) if entry.get("description") else None,
            pwdrequired=str(entry.get("pwdrequired") or "0") == "1",
        )
    return found


def parse_app_info(text: str, app: int) -> AppInfo:
    """One capture of ``app_info_print`` as the typed view of it. Raises ``VdfError``."""
    block, header = vdf.extract_app(text, app)
    depots_block = block.get("depots")
    depots_block = depots_block if isinstance(depots_block, dict) else {}
    common = block.get("common")
    common = common if isinstance(common, dict) else {}
    return AppInfo(
        app=app,
        name=str(common["name"]) if common.get("name") else None,
        change_number=header.get("changeNumber"),
        last_change=header.get("lastChange"),
        private_branches=str(depots_block.get("privatebranches") or "0") == "1",
        branches=_branches(depots_block),
        depots=_depots(depots_block),
        raw=block,
    )


def _argv(app: int) -> list[str]:
    return [*command(), "+login", "anonymous", "+app_info_update", "1", "+app_info_print", str(app), "+quit"]


def _record(log: Path | None, argv: list[str], text: str, exit_code: int | str) -> None:
    if log is None:
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(redact.redact(" ".join(argv)) + "\n")
        handle.write(redact.redact(text))
        handle.write(f"\n-- exit {exit_code}\n")


def app_info(
    app: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    log: Path | None = None,
) -> AppInfo:
    """Run ``app_info_print`` for ``app`` and return what it published.

    Retries only the one failure that is known to be transient: a zero exit with a
    truncated block. A non-zero exit, a missing tool and a timeout are reported straight
    away, so a scan fails its Steam source in seconds rather than in minutes.
    """
    argv = _argv(app)
    last: str = ""
    for attempt in range(1, max(1, attempts) + 1):
        output.debug(f"exec {' '.join(argv)} (attempt {attempt})")
        try:
            completed = subprocess.run(  # noqa: S603 - the command line is operator-supplied
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise UpstreamUnavailable(
                f"steamcmd not found: install it or set {COMMAND_ENV} to the command that runs it"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            _record(log, argv, "", "timeout")
            raise UpstreamUnavailable(f"steamcmd timed out after {timeout:.0f}s for app {app}" + _see(log)) from exc

        text = (completed.stdout or "") + (completed.stderr or "")
        _record(log, argv, text, completed.returncode)
        if completed.returncode != 0:
            raise UpstreamUnavailable(f"steamcmd exited {completed.returncode} for app {app}" + _see(log))
        try:
            return parse_app_info(text, app)
        except vdf.VdfError as exc:
            last = str(exc)
            output.debug(f"app {app}: {last}; attempt {attempt} of {attempts}")

    raise UpstreamUnavailable(
        f"app_info_print printed no branch data for {app} after {max(1, attempts)} attempts "
        f"(known non-TTY truncation: {last})" + _see(log)
    )


def _see(log: Path | None) -> str:
    return f"; see {log.name}" if log is not None else ""
