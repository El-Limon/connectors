#!/usr/bin/env python3
"""A DepotDownloader stand-in, plus the fixtures that pin a target at it.

Run as a program it is the tool: ``TAKARO_MAINT_DEPOTDOWNLOADER`` points the real code
path at this file, so every test drives the same ``steam`` package the rig does, with no
network and no 14 GB download. Imported it builds the repository copy those tests pin.

Environment it reads:
  FAKE_DD_ROOT            fixture root: ``<depot>/<manifest>/tree/**`` plus ``head.json``
  FAKE_DD_LOG             append one JSON line per invocation (argv), for argv assertions
  FAKE_DD_UNAVAILABLE     a manifest id Steam refuses to serve
  FAKE_DD_LICENSE_DENIED  the account owns no licence for the app
  FAKE_DD_CORRUPT         a depot-relative path served with altered bytes
  FAKE_DD_IGNORE_MANIFEST a tool that quietly serves the branch head and still exits 0
  FAKE_DD_BRANCHES        {"<branch>": {"depots": {"<depot>": "<manifest>"}, "protected": bool}}
  FAKE_DD_NO_LISTING      -manifest-only exits 0 having written no listing file
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
DEPOTS = FIXTURES / "games" / "7d2d" / "depots"
PINNED_MANIFEST = "1633674551820196085"
HEAD_MANIFEST = "1900000000000000001"
DEPOT = "294422"
APP = 294420


# --------------------------------------------------------------------------- the tool


def _flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            return argv[index + 1]
        return ""
    return None


def _tree(root: Path, depot: str, manifest: str) -> Path:
    return root / depot / manifest / "tree"


def _listing(tree: Path) -> list[tuple[str, int, str]]:
    rows = []
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        payload = path.read_bytes()
        rows.append((path.relative_to(tree).as_posix(), len(payload), hashlib.sha1(payload).hexdigest()))
    return rows


def _selected(name: str, selectors: list[str]) -> bool:
    """The real tool's file-list semantics: an exact, case-insensitive path match.

    DepotDownloader does not treat a plain entry as a directory prefix -- only a
    `regex:` entry matches more than one path, and it too is case-insensitive. A fake
    that matched prefixes let a file list naming a directory pass while the real tool
    would have downloaded nothing.
    """
    if not selectors:
        return True
    for selector in selectors:
        if selector.startswith("regex:"):
            if re.search(selector[len("regex:") :], name, re.IGNORECASE):
                return True
        elif name.lower() == selector.lower():
            return True
    return False


def _branches() -> dict[str, Any]:
    """``FAKE_DD_BRANCHES``: ``{"<branch>": {"depots": {...}, "protected": true}}``."""
    raw = os.environ.get("FAKE_DD_BRANCHES")
    return json.loads(raw) if raw else {}


def main(argv: list[str]) -> int:
    root = Path(os.environ.get("FAKE_DD_ROOT", DEPOTS))
    log = os.environ.get("FAKE_DD_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")

    depot = _flag(argv, "-depot") or DEPOT
    manifest = _flag(argv, "-manifest")
    branch = _flag(argv, "-branch") or "public"
    declared = _branches().get(branch)
    if declared is not None and declared.get("protected") and not _flag(argv, "-branchpassword"):
        # What Steam answers for a password-protected branch with no password: the head
        # is not served at all, so nothing downstream may treat it as observed.
        print(f"Password required for branch {branch} (result: AccessDenied)")
        return 1
    if not manifest or os.environ.get("FAKE_DD_IGNORE_MANIFEST"):
        if declared is not None and str(depot) in declared.get("depots", {}):
            manifest = str(declared["depots"][str(depot)])
        else:
            head = json.loads((root / "head.json").read_text(encoding="utf-8"))
            manifest = str(head[depot])
        print(f"Using branch head manifest {manifest} for depot {depot}")

    if os.environ.get("FAKE_DD_LICENSE_DENIED"):
        print(f"No subscription for app {_flag(argv, '-app')}")
        return 1
    unavailable = os.environ.get("FAKE_DD_UNAVAILABLE")
    if unavailable and unavailable == manifest:
        print(f"Unable to get manifest request code for depot {depot} manifest {manifest} (result: AccessDenied)")
        return 1

    tree = _tree(root, depot, manifest)
    if not tree.is_dir() and declared is not None and str(depot) in declared.get("depots", {}):
        # A branch whose head the fixtures do not carry a tree for still has content:
        # the rows are the public tree's, published under the branch's own manifest id.
        head = json.loads((root / "head.json").read_text(encoding="utf-8"))
        tree = _tree(root, depot, str(head[depot]))
    if not tree.is_dir():
        print(f"Depot {depot} manifest {manifest} is not available")
        return 1

    rows = _listing(tree)
    if "-manifest-only" in argv:
        total = sum(size for _, size, _ in rows)
        lines = [
            f"Content Manifest for Depot {depot}",
            "",
            f"Manifest ID / date     : {manifest} / 28/08/2026 14:27:10",
            f"Total number of files  : {len(rows)}",
            f"Total number of chunks : {len(rows)}",
            f"Total bytes on disk    : {total}",
            f"Total bytes compressed : {total}",
            "",
            "          Size Chunks File SHA                                 Flags Name",
        ]
        lines += [f"{size:>14} {1:>6} {sha1} {0:>5} {name}" for name, size, sha1 in rows]
        # Where the real tool puts it: under its install directory, not next to the process.
        listing = Path(_flag(argv, "-dir") or Path.cwd() / "depots" / depot / manifest)
        listing.mkdir(parents=True, exist_ok=True)
        if not os.environ.get("FAKE_DD_NO_LISTING"):
            (listing / f"manifest_{depot}_{manifest}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return 0

    target = Path(_flag(argv, "-dir") or ".")
    selectors: list[str] = []
    filelist = _flag(argv, "-filelist")
    if filelist:
        selectors = [line.strip() for line in Path(filelist).read_text(encoding="utf-8").splitlines() if line.strip()]
    corrupt = os.environ.get("FAKE_DD_CORRUPT")
    copied = 0
    for name, _, _ in rows:
        if not _selected(name, selectors):
            continue
        source = tree / name
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if corrupt and name == corrupt:
            destination.write_bytes(source.read_bytes() + b"tampered")
        copied += 1
    # The real tool leaves its own bookkeeping in the download directory; anything that
    # reads the result has to tell those files apart from the depot's.
    bookkeeping = target / ".DepotDownloader"
    bookkeeping.mkdir(parents=True, exist_ok=True)
    (bookkeeping / f"{depot}_{manifest}.manifest").write_bytes(b"depot manifest bookkeeping")
    (bookkeeping / f"{depot}_{manifest}.manifest.sha").write_text("0" * 40 + "\n", encoding="utf-8")
    print(f"Downloaded {copied} files from depot {depot} manifest {manifest}")
    return 0


# --------------------------------------------------------------------------- fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_ID = "linux-3.2.0.b10"
BUILD_SCRIPT = "games/7d2d/scripts/build-release.sh"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repin(root: Path, *, manifest: str = PINNED_MANIFEST, depots: Path | None = None) -> dict[str, Any]:
    """Point the copied 7D2D target at the fixture depot and record its real hashes."""
    tree = _tree(depots or DEPOTS, DEPOT, manifest)
    path = root / "catalog" / "7d2d" / "targets" / f"{TARGET_ID}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    server = record["inputs"]["server"]
    server["depots"] = {
        DEPOT: {
            "manifest": manifest,
            "size": sum(f.stat().st_size for f in tree.rglob("*") if f.is_file()),
            "files": len([f for f in tree.rglob("*") if f.is_file()]),
        }
    }
    server["files"] = {
        name: {"sha256": sha256_of(tree / name), "size": (tree / name).stat().st_size}
        for name in sorted(server["files"])
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def write_target(root: Path, record: dict[str, Any]) -> Path:
    path = root / "catalog" / "7d2d" / "targets" / f"{TARGET_ID}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def read_target(root: Path) -> dict[str, Any]:
    path = root / "catalog" / "7d2d" / "targets" / f"{TARGET_ID}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def make_repo(tmp_path: Path, *, repin_target: bool = True) -> Path:
    """A repository copy holding the real catalog, the tool lock and the build script."""
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "catalog", root / "catalog")
    (root / "maintenance").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "maintenance" / "tools.lock.json", root / "maintenance" / "tools.lock.json")
    script = root / BUILD_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    if repin_target:
        repin(root)
    return root


def environment(root: Path, log: Path, **extra: str) -> dict[str, str]:
    """Environment entries that make the real steam package drive this stub."""
    del root
    env = {
        "TAKARO_MAINT_DEPOTDOWNLOADER": str(Path(__file__).resolve()),
        "FAKE_DD_ROOT": str(DEPOTS),
        "FAKE_DD_LOG": str(log),
    }
    env.update(extra)
    return env


def argv_log(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
