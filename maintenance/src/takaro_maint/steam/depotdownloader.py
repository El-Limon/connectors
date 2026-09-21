"""The pinned DepotDownloader: obtained by hash, run with pinned arguments, never guessed.

Every Steam acquisition goes through one tool at one version, recorded in
``maintenance/tools.lock.json`` by url, size and sha256. Bytes that do not match the
lock are refused rather than run, and a manifest Steam will not serve is an upstream
failure -- never a silent fall back to whatever the branch head has become.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import net, output, paths, redact
from ..exit_codes import UpstreamUnavailable, UsageError

LOCK_RELATIVE = Path("maintenance") / "tools.lock.json"
TOOL_ID = "depotdownloader"

# The line every classified failure carries, so a caller (and a test) can tell this
# apart from any other non-zero exit.
NO_FALLBACK = "not falling back to branch head"

_MANIFEST_ID = re.compile(r"Manifest ID / date\s*:\s*(?P<id>[0-9]+)\s*/\s*(?P<date>.*)")
_TOTAL_FILES = re.compile(r"Total number of files\s*:\s*(?P<value>[0-9]+)")
_TOTAL_BYTES = re.compile(r"Total bytes on disk\s*:\s*(?P<value>[0-9]+)")
# "        14800      1 c2eb0…(40 hex) 0 7DaysToDieServer.x86_64"
_FILE_ROW = re.compile(
    r"^\s*(?P<size>[0-9]+)\s+(?P<chunks>[0-9]+)\s+(?P<sha1>[0-9a-f]{40})\s+(?P<flags>[0-9]+)\s+(?P<name>\S.*)$"
)

_UNAVAILABLE_MARKERS = (
    "unable to get manifest request code",
    "is not available",
    "not available for depot",
    "depot key",
    "invalid depot",
    "access denied",
    "no subscription",
    "login failure",
    "failed to login",
    "rate limit",
)


@dataclass(frozen=True)
class ToolSpec:
    """One entry of ``tools.lock.json``."""

    version: str
    platform: str
    executable: str
    url: str
    size: int
    sha256: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ManifestInfo:
    """What ``-manifest-only`` reported for one depot."""

    depot: str
    manifest: str
    date: str
    files: int
    bytes_on_disk: int
    entries: dict[str, dict[str, Any]]


def lock_path() -> Path:
    return paths.repo_root() / LOCK_RELATIVE


def read_lock() -> ToolSpec:
    """The pinned tool, or a usage error naming what is wrong with the lock."""
    path = lock_path()
    if not path.is_file():
        raise UsageError(f"no tool lock at {path}; Steam acquisition needs a pinned DepotDownloader")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entry = document["tools"][TOOL_ID]
        return ToolSpec(
            version=str(entry["version"]),
            platform=str(entry["platform"]),
            executable=str(entry["executable"]),
            url=str(entry["url"]),
            size=int(entry["size"]),
            sha256=str(entry["sha256"]),
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise UsageError(f"{path} does not pin '{TOOL_ID}' ({exc})") from exc


def ensure(cache: Path) -> Path:
    """The executable, downloaded and unpacked once, verified against the lock every time.

    ``TAKARO_MAINT_DEPOTDOWNLOADER`` replaces it, the way ``TAKARO_MAINT_DOCKER`` replaces
    docker: tests drive a stub through the same code path the real tool takes.
    """
    override = os.environ.get("TAKARO_MAINT_DEPOTDOWNLOADER")
    if override:
        exe = Path(override).expanduser()
        if not exe.is_file():
            raise UsageError(f"TAKARO_MAINT_DEPOTDOWNLOADER={override} is not a file")
        return exe

    spec = read_lock()
    installed = cache / "tools" / TOOL_ID / spec.version
    exe = installed / spec.executable
    if exe.is_file():
        return exe

    # `net.download` refuses bytes that do not match the lock (exit 5) and an unreachable
    # release (exit 4); nothing below ever sees the wrong archive.
    blob = net.download(spec.url, net.Expectation(sha256=spec.sha256, size=spec.size), cache)
    staging = installed.parent / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(blob) as archive:
            for member in archive.namelist():
                # A zip may name any path it likes; the lock pins the bytes, not the layout.
                paths.safe_relative(member, field="DepotDownloader archive entry")
            archive.extractall(staging)
        extracted = staging / spec.executable
        if not extracted.is_file():
            raise UsageError(f"{spec.url} does not contain {spec.executable}")
        extracted.chmod(0o755)
        installed.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, installed)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    output.info(f"using DepotDownloader {spec.version} ({spec.platform})")
    return exe


def _classify(argv: list[str], completed: subprocess.CompletedProcess[str], log: Path, secrets: list[str]) -> None:
    """A non-zero DepotDownloader is always an upstream failure, never a fall back."""
    if completed.returncode == 0:
        return
    text = (completed.stdout or "") + (completed.stderr or "")
    lowered = text.lower()
    reason = next((marker for marker in _UNAVAILABLE_MARKERS if marker in lowered), None)
    tail = "\n".join(redact.redact(line, secrets) for line in text.strip().splitlines()[-20:])
    what = f"DepotDownloader exited {completed.returncode}"
    if reason:
        what += f" ({reason})"
    raise UpstreamUnavailable(
        f"{what}; {NO_FALLBACK}. See {log.name}:\n{tail}",
        argv=[redact.redact(part, secrets) for part in argv[1:]],
    )


def run(
    args: list[str],
    *,
    cwd: Path,
    log: Path,
    cache: Path,
    secrets: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the pinned tool, tee stdout+stderr into ``log`` and classify any failure."""
    exe = ensure(cache)
    spec_env = read_lock().env if not os.environ.get("TAKARO_MAINT_DEPOTDOWNLOADER") else {}
    argv = [str(exe), *args]
    cwd.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    hidden = list(secrets or [])
    output.debug("exec " + redact.redact(" ".join(argv), hidden))
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        env={**os.environ, **spec_env},
        capture_output=True,
        text=True,
        check=False,
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write(redact.redact(" ".join(argv[1:]), hidden) + "\n")
        handle.write(redact.redact((completed.stdout or "") + (completed.stderr or ""), hidden))
        handle.write(f"\n-- exit {completed.returncode}\n")
    _classify(argv, completed, log, hidden)
    return completed


def _parse_manifest_file(path: Path, depot: str) -> ManifestInfo:
    manifest = ""
    date = ""
    files = 0
    total = 0
    entries: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        found = _MANIFEST_ID.search(line)
        if found:
            manifest = found.group("id")
            date = found.group("date").strip()
            continue
        found = _TOTAL_FILES.search(line)
        if found:
            files = int(found.group("value"))
            continue
        found = _TOTAL_BYTES.search(line)
        if found:
            total = int(found.group("value"))
            continue
        row = _FILE_ROW.match(line)
        if row:
            entries[row.group("name").strip().replace("\\", "/")] = {
                "size": int(row.group("size")),
                "sha1": row.group("sha1"),
            }
    if not manifest:
        # The filename carries the id too: `manifest_<depot>_<id>.txt`.
        stem = path.stem.split("_")
        manifest = stem[-1] if stem else ""
    return ManifestInfo(depot=depot, manifest=manifest, date=date, files=files, bytes_on_disk=total, entries=entries)


def manifest_only(
    app: int,
    depot: str,
    branch: str,
    *,
    manifest: str | None,
    os_: str,
    arch: str,
    out: Path,
    cache: Path,
    log: Path,
    credentials: dict[str, str] | None = None,
) -> ManifestInfo:
    """Read one depot's manifest listing without downloading any content."""
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "-app",
        str(app),
        "-depot",
        str(depot),
        "-branch",
        branch,
        "-os",
        os_,
        "-osarch",
        arch,
        "-manifest-only",
    ]
    if manifest:
        args += ["-manifest", str(manifest)]
    secrets = _apply_credentials(args, credentials)
    # DepotDownloader writes the listing under its own install directory
    # (``depots/<depot>/<manifest>/``) rather than next to the process, so the whole
    # output directory is searched rather than only its top level.
    before = {path for path in out.rglob("manifest_*.txt")}
    run(args, cwd=out, log=log, cache=cache, secrets=secrets)
    produced = sorted(path for path in out.rglob(f"manifest_{depot}_*.txt") if path not in before)
    if not produced:
        produced = sorted(out.rglob(f"manifest_{depot}_*.txt"))
    if not produced:
        raise UpstreamUnavailable(
            f"DepotDownloader wrote no manifest listing for depot {depot}; {NO_FALLBACK}",
        )
    return _parse_manifest_file(produced[-1], str(depot))


def download(
    app: int,
    depot: str,
    manifest: str,
    *,
    branch: str,
    os_: str,
    arch: str,
    dir: Path,
    cache: Path,
    log: Path,
    filelist: list[str] | None = None,
    validate: bool = True,
    credentials: dict[str, str] | None = None,
) -> None:
    """Download exactly one depot manifest. A pinned manifest is never omitted."""
    if not manifest:
        raise UsageError(f"depot {depot} has no pinned manifest; {NO_FALLBACK}")
    dir.mkdir(parents=True, exist_ok=True)
    args = [
        "-app",
        str(app),
        "-depot",
        str(depot),
        "-manifest",
        str(manifest),
        "-branch",
        branch,
        "-os",
        os_,
        "-osarch",
        arch,
        "-dir",
        str(dir),
        "-max-downloads",
        "8",
    ]
    if validate:
        args.append("-validate")
    with tempfile.TemporaryDirectory(prefix="takaro-steam-filelist-") as tmp:
        if filelist:
            selector_file = Path(tmp) / "filelist.txt"
            selector_file.write_text("\n".join(filelist) + "\n", encoding="utf-8")
            args += ["-filelist", str(selector_file)]
        secrets = _apply_credentials(args, credentials)
        output.info(f"downloading depot {depot} manifest {manifest} (app {app}, branch {branch})")
        run(args, cwd=dir, log=log, cache=cache, secrets=secrets)


def _apply_credentials(args: list[str], credentials: dict[str, str] | None) -> list[str]:
    """Append the credential arguments a target's record asks for, from the environment only."""
    if not credentials:
        return []
    if "branchPasswordEnv" in credentials:
        name = credentials["branchPasswordEnv"]
        value = os.environ.get(name)
        if not value:
            raise UsageError(f"this target's branch needs a password; set {name} in the environment")
        args += ["-branchpassword", value]
        return [value]
    if "accountEnv" in credentials:
        name = credentials["accountEnv"]
        value = os.environ.get(name)
        if not value:
            raise UsageError(f"this target needs a Steam account; set {name} in the environment")
        args += ["-username", value]
        return [value]
    return []
