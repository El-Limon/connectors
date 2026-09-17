"""``install`` — place a target's pinned inputs into a game directory, or refuse."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import shutil
from pathlib import Path
from typing import Any

from .. import __version__, net, output, paths
from ..catalog.loader import resolve
from ..exit_codes import OK, ConflictError, UsageError
from ..games import adapter_for
from ..install import plan_inputs
from ..install.ledger import check_ledger, read_ledger, write_ledger
from ..install.staging import StagedInstall
from . import add_selection_arguments, select_one


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("install", help="install a target's pinned inputs into a game directory")
    add_selection_arguments(parser)
    parser.add_argument("--dest", required=True, help="the game directory to install into")
    world = parser.add_mutually_exclusive_group()
    world.add_argument("--reuse-world", action="store_true", help="keep a world from another revision as-is")
    world.add_argument("--fresh-world", action="store_true", help="move an incompatible world aside first")
    parser.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    parser.add_argument(
        "--rollback", action="store_true", help="restore the previous install, where the game keeps one"
    )
    parser.set_defaults(handler=_install, op="install")


def tree_hash(root: Path) -> str:
    """A stable hash of every regular file under ``root`` — used to prove nothing changed."""
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(net.sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def _world_dirs(dest: Path, preserve: list[str]) -> list[Path]:
    del preserve
    return sorted(p for p in dest.glob("world*") if p.is_dir())


def _install(args: Any) -> int:
    catalog, target = select_one(args)
    resolved = resolve(catalog, target)
    game_record = catalog.game(target.game).record
    dest = Path(args.dest).expanduser().resolve()
    cache = paths.cache_dir()
    preserve = adapter_for(target.game).preserve_globs(resolved)

    if args.rollback:
        raise UsageError(
            f"game '{target.game}' keeps no previous install to roll back to; reinstall the target instead"
        )

    plans = plan_inputs(game_record, target.record)
    existing = read_ledger(dest)

    # World compatibility is decided before the fast path: a directory holding a world from
    # another game revision is not "already installed", whatever its ledger fingerprint says.
    worlds = _world_dirs(dest, preserve)
    world_revision = existing.world_revision if existing else target.revision
    moved_world: str | None = None
    if worlds and existing is not None and existing.world_revision != target.revision:
        if args.fresh_world:
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            moved_world = f".takaro/worlds/{existing.world_revision}-{stamp}"
            world_revision = target.revision
        elif args.reuse_world:
            output.warn(
                f"reusing a world created on {existing.world_revision} for {target.revision}; "
                "the game may migrate or refuse it"
            )
            world_revision = target.revision
        else:
            raise ConflictError(
                f"{dest} holds a world created on {existing.world_revision} but the target is {target.revision}; "
                "pass --fresh-world to move it aside or --reuse-world to keep it",
                worlds=[p.name for p in worlds],
            )

    # Nothing to do only when the bytes are already right and no world action was asked for.
    if (
        existing is not None
        and existing.fingerprint == target.fingerprint
        and not args.fresh_world
        and not args.reuse_world
    ):
        reasons = check_ledger(dest, target.record, target.fingerprint)
        if not reasons:
            output.info(f"{dest} already holds {target.id} ({target.fp16}); nothing to do")
            output.emit(
                "install",
                True,
                status="already-installed",
                game=target.game,
                target=target.id,
                fingerprint=target.fingerprint,
                dest=str(dest),
            )
            return OK
        output.info("ledger present but stale: " + "; ".join(reasons))

    if args.dry_run:
        output.emit(
            "install",
            True,
            status="dry-run",
            game=target.game,
            target=target.id,
            fingerprint=target.fingerprint,
            dest=str(dest),
            inputs=[{"name": p.name, "url": p.url, "installPath": p.install_path} for p in plans],
        )
        return OK

    before = tree_hash(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ledger_inputs: list[dict[str, Any]] = []
    try:
        with StagedInstall(dest=dest, fp16=target.fp16, preserve=preserve) as staging:
            for plan in plans:
                staged = staging.path_for(plan.install_path)
                output.info(f"fetching {plan.name} from {plan.url}")
                plan.fetch(staged, cache)
                digests = net.hash_file(staged)
                entry: dict[str, Any] = {
                    "name": plan.name,
                    "path": plan.install_path,
                    "size": int(digests["size"]),
                }
                expectation = plan.expectation()
                if expectation.get("sha256"):
                    entry["sha256"] = digests["sha256"]
                if expectation.get("sha1"):
                    entry["sha1"] = digests["sha1"]
                ledger_inputs.append(entry)

            if moved_world is not None:
                destination = dest / moved_world
                destination.parent.mkdir(parents=True, exist_ok=True)
                for world in worlds:
                    shutil.move(str(world), str(destination / world.name))
                output.info(f"moved {len(worlds)} world director(y|ies) to {moved_world}")

            placed = staging.commit()
    except BaseException:
        after = tree_hash(dest)
        if after != before:
            output.warn(f"{dest} changed during a failed install")
        else:
            output.info(f"{dest} is byte-identical to what it was before the failed install")
        raise

    ledger = {
        "schemaVersion": 1,
        "game": target.game,
        "target": target.id,
        "fingerprint": target.fingerprint,
        "installedAt": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "installedBy": f"takaro-maint/{__version__}",
        "inputs": ledger_inputs,
        "container": {
            "image": target.record["runtime"]["container"]["image"],
            "tag": target.record["runtime"]["container"]["tag"],
            "digest": target.record["runtime"]["container"]["digest"],
        },
        "world": {"revision": world_revision, "createdBy": target.id},
    }
    if existing is not None and existing.data.get("artifact") and existing.fingerprint == target.fingerprint:
        ledger["artifact"] = existing.data["artifact"]
    write_ledger(dest, ledger)

    output.emit(
        "install",
        True,
        status="installed",
        game=target.game,
        target=target.id,
        fingerprint=target.fingerprint,
        dest=str(dest),
        placed=placed,
        worldMovedTo=moved_world,
        inputs=ledger_inputs,
    )
    return OK
