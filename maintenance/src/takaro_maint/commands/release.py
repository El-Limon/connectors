"""``release`` — assemble a connector's complete target set, publish it, and prove it landed."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from .. import github, output, paths
from ..exit_codes import OK, ConflictError, IntegrityError, UsageError, VerificationFailed
from ..publish import assemble as assemble_mod
from ..publish import channels, compat_record
from ..publish.manifest import source_revision
from ..publish.release_client import ReleaseClient
from . import load_catalog


def _watched(connector: str) -> list[str]:
    """What makes this connector's build dirty.

    ``scripts`` and ``maintenance`` are in the list because they decide how the artifact is
    packaged and published, not only what goes into it.
    """
    return [f"games/{connector}", f"catalog/{connector}", "maintenance", "scripts"]


def _commit_time(repo_root: Path, commit: str) -> int | None:
    """The committer time of ``commit``, or ``None`` if this checkout cannot answer.

    The compatibility record is stamped from this rather than from the clock: it is a property
    of the commit being released, so every assembly of that commit agrees on it however long
    afterwards it runs, and a recovery rerun re-assembles byte for byte what it is retrying.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", commit],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - no git on the machine doing the assembly
        return None
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("release", help="assemble, publish and verify a connector's complete release")
    parser.set_defaults(handler=None, op="release")
    subcommands = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")
    _register_assemble(subcommands)
    _register_publish(subcommands)
    _register_verify(subcommands)


def _add_github_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=None, help="owner/repo (default: $TAKARO_MAINT_REPO, else the origin remote)")
    parser.add_argument("--api-url", default=None, help="GitHub API base URL")
    parser.add_argument("--token", default=None, help="GitHub token (default: $GH_TOKEN, else `gh auth token`)")


# -- assemble ----------------------------------------------------------------------------


def _register_assemble(subcommands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subcommands.add_parser("assemble", help="turn per-target builds and reports into one release directory")
    parser.add_argument("--connector", required=True, help="connector id, e.g. minecraft")
    parser.add_argument("--version", required=True, help="connector version being released")
    parser.add_argument("--channel", required=True, choices=list(channels.CHANNELS))
    parser.add_argument("--tag", required=True, help="the release tag the assets will hang off")
    parser.add_argument("--dist", required=True, help="the build output: one directory per target, or one manifest")
    parser.add_argument("--out", required=True, help="directory the complete set is written to; must be empty")
    parser.add_argument("--reports", default=None, help="directory holding the per-target verification reports")
    parser.add_argument(
        "--mode", default=None, choices=["catalog", "legacy"], help="default: catalog when the connector has one"
    )
    parser.add_argument("--source-commit", default=None, help="the commit the artifacts were built from")
    parser.add_argument("--allow-dirty", action="store_true", help="assemble from a dirty tree (never for stable)")
    parser.add_argument("--repo", default=None, help="owner/repo, for the download links in the record")
    parser.add_argument("files", nargs="*", help="legacy mode only: the exact files to publish")
    parser.set_defaults(handler=_assemble, op="release assemble")


def _assemble(args: Any) -> int:
    repo_root = paths.repo_root()
    has_catalog = (paths.catalog_root() / args.connector / "game.json").is_file()
    mode = args.mode or ("catalog" if has_catalog else "legacy")
    if mode == "catalog" and not has_catalog:
        raise UsageError(f"--mode catalog needs catalog/{args.connector}/game.json, which does not exist")
    if mode == "catalog" and args.files:
        raise UsageError("positional files are legacy mode only; a catalog release takes its files from --dist")
    if args.allow_dirty and args.channel == "stable":
        raise UsageError("--allow-dirty is never accepted for a stable release")

    head, dirty = source_revision(repo_root, _watched(args.connector))
    source_commit = args.source_commit or head
    if dirty and not args.allow_dirty:
        raise ConflictError(
            "the working tree is dirty, so the artifacts cannot be attributed to a commit; "
            "commit first, or pass --allow-dirty for a non-stable channel",
            sourceCommit=source_commit,
        )
    repo = github.resolve_repo(args.repo, repo_root)
    out = Path(args.out).expanduser().resolve()
    dist = Path(args.dist).expanduser().resolve()
    stamp = compat_record.generated_at(_commit_time(repo_root, source_commit))

    if mode == "catalog":
        result = assemble_mod.assemble_catalog(
            load_catalog(),
            connector=args.connector,
            version=args.version,
            channel=args.channel,
            tag=args.tag,
            dist=dist,
            reports=Path(args.reports).expanduser().resolve() if args.reports else None,
            out=out,
            repo=repo,
            source_commit=source_commit,
            dirty=dirty,
            allow_dirty=args.allow_dirty,
            stamp=stamp,
        )
    else:
        result = assemble_mod.assemble_legacy(
            connector=args.connector,
            version=args.version,
            channel=args.channel,
            tag=args.tag,
            dist=dist,
            out=out,
            files=[Path(f).expanduser().resolve() for f in args.files],
            repo=repo,
            source_commit=source_commit,
            dirty=dirty,
            stamp=stamp,
        )
    output.emit("release assemble", True, **result)
    return OK


# -- publish -----------------------------------------------------------------------------


def _register_publish(subcommands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subcommands.add_parser("publish", help="upload an assembled set and finalise the release")
    parser.add_argument("--connector", required=True)
    parser.add_argument("--channel", required=True, choices=list(channels.CHANNELS))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assembled", required=True, help="the directory `release assemble` wrote")
    _add_github_arguments(parser)
    parser.add_argument("--target-commit", default=None, help="the commit this publication is for")
    parser.add_argument("--run-id", default=None, help="identifies the staging draft of this run")
    parser.add_argument("--pr-number", default=None, help="pull request number, for the pr channel's defaults")
    parser.add_argument("--title", default=None, help="release title (rolling/pr only; stable keeps its own)")
    parser.add_argument("--notes-file", default=None, help="file holding the release notes (rolling/pr only)")
    parser.add_argument("--no-finalize", action="store_true", help="stable: upload and verify, but leave the draft")
    parser.set_defaults(handler=_publish, op="release publish")


def _local_set(
    assembled: Path, *, connector: str, channel: str, tag: str, repo: str, target_commit: str
) -> tuple[list[channels.LocalAsset], dict[str, Any]]:
    """Everything that can be checked before a single request goes out."""
    if not assembled.is_dir():
        raise UsageError(f"--assembled {assembled} is not a directory")
    records = sorted(assembled.glob(f"takaro-{connector}-*.compat.json"))
    if len(records) != 1:
        raise UsageError(
            f"{assembled} holds {len(records)} compatibility records for '{connector}', expected exactly one"
        )
    record = compat_record.read(records[0])
    for field, expected in (("tag", tag), ("channel", channel), ("connector", connector)):
        if record[field] != expected:
            raise ConflictError(
                f"the assembled set is for {field} '{record[field]}', this publication is for '{expected}'"
            )
    # The record is the release's own account of what it is: every download link in it, and the
    # provenance a consumer reads back, is written against one repository and one commit. Publish
    # it anywhere else and the tag would be honest while everything hanging off it lies.
    source = record["source"]
    for field, expected, what in (("repo", repo, "repository"), ("commit", target_commit, "source commit")):
        if source[field] != expected:
            raise ConflictError(
                f"the assembled set was built for {what} '{source[field]}', this publication is for "
                f"'{expected}'; reassemble against this checkout",
                **{field: source[field]},
            )

    sums_path = assembled / "SHA256SUMS"
    if not sums_path.is_file():
        raise UsageError(f"{assembled} has no SHA256SUMS")
    sums = channels.parse_checksums(sums_path.read_bytes())
    assets = channels.local_assets(assembled)
    for asset in assets:
        if asset.name == "SHA256SUMS":
            continue
        if asset.name not in sums:
            raise IntegrityError(f"{asset.name} is in the assembled set but not in SHA256SUMS", asset=asset.name)
        if sums[asset.name] != asset.sha256:
            raise IntegrityError(
                f"{asset.name} hashes to {asset.sha256}, SHA256SUMS says {sums[asset.name]}", asset=asset.name
            )
    return assets, record


def _game_record(connector: str) -> dict[str, Any] | None:
    path = paths.catalog_root() / connector / "game.json"
    if not path.is_file():
        return None
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _publish(args: Any) -> int:
    repo_root = paths.repo_root()
    assembled = Path(args.assembled).expanduser().resolve()
    repo = github.resolve_repo(args.repo, repo_root)
    target_commit = args.target_commit or source_revision(repo_root, [])[0]
    assets, record = _local_set(
        assembled,
        connector=args.connector,
        channel=args.channel,
        tag=args.tag,
        repo=repo,
        target_commit=target_commit,
    )

    client = ReleaseClient(github.GitHub(repo, github.resolve_token(args.token), args.api_url))
    run_id = args.run_id or "local-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    if args.channel == "stable":
        result = channels.publish_stable(
            client,
            tag=args.tag,
            assets=assets,
            record=record,
            target_commit=target_commit,
            finalize=not args.no_finalize,
        )
    else:
        name = channels.display_name(record, _game_record(args.connector))
        notes = (
            Path(args.notes_file).expanduser().read_text(encoding="utf-8")
            if args.notes_file
            else channels.default_notes(args.channel, str(record["version"]), args.pr_number)
        )
        result = channels.publish_staged(
            client,
            channel=args.channel,
            tag=args.tag,
            run_id=run_id,
            assets=assets,
            record=record,
            target_commit=target_commit,
            title=args.title or channels.default_title(args.channel, name, args.pr_number),
            notes=notes,
        )

    output.emit(
        "release publish",
        True,
        channel=args.channel,
        tag=args.tag,
        repo=repo,
        targetCommit=target_commit,
        **result,
    )
    return OK


# -- verify ------------------------------------------------------------------------------


def _register_verify(subcommands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subcommands.add_parser("verify", help="re-read a release from GitHub and prove its set is complete")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--connector", default=None, help="narrows which compatibility record is expected")
    parser.add_argument("--expect", default=None, help="an assembled directory the release must match byte for byte")
    _add_github_arguments(parser)
    parser.add_argument("--out", default=None, help="directory the verification document is written to")
    parser.set_defaults(handler=_verify, op="release verify")


def _verify(args: Any) -> int:
    repo = github.resolve_repo(args.repo, paths.repo_root())
    client = ReleaseClient(github.GitHub(repo, github.resolve_token(args.token), args.api_url))
    release = client.find_release(args.tag)
    if release is None:
        raise VerificationFailed(f"no release for tag {args.tag} on {repo}", tag=args.tag)

    expect: dict[str, Path] | None = None
    if args.expect:
        directory = Path(args.expect).expanduser().resolve()
        expect = {path.name: path for path in sorted(directory.iterdir()) if path.is_file()}

    document = channels.verify_release(client, release, connector=args.connector, expect=expect)
    record = document.pop("record")
    document["repo"] = repo
    document["tagCommit"] = client.tag_commit(args.tag)
    document["sourceCommit"] = str(record["source"]["commit"])
    document["version"] = str(record["version"])
    # The tag is the only thing a consumer starts from, so it has to agree with the commit the
    # record says these bytes were built from. A tag that has been moved or reused since the
    # release was published is exactly the case this catches.
    if document["tagCommit"] is not None and document["tagCommit"] != document["sourceCommit"]:
        raise ConflictError(
            f"tag {args.tag} points at {document['tagCommit']}, the compatibility record on it was "
            f"built from {document['sourceCommit']}",
            tag=args.tag,
            tagCommit=document["tagCommit"],
            sourceCommit=document["sourceCommit"],
        )

    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        safe = args.tag.replace("/", "_")
        payload = {"schemaVersion": 1, "op": "release verify", "ok": True, **document}
        (out / f"release-verify-{safe}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    output.emit("release verify", True, **document)
    return OK
