"""``docs render`` — keep the target table in a connector README generated, not typed."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import output, paths
from ..exit_codes import OK, UsageError
from . import load_catalog

BEGIN = "<!-- takaro-maint:targets:begin -->"
END = "<!-- takaro-maint:targets:end -->"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("docs", help="render generated documentation blocks")
    inner = parser.add_subparsers(dest="docs_command", metavar="<subcommand>")
    render = inner.add_parser("render", help="render the target table for a game README")
    render.add_argument("--game", required=True)
    render.add_argument("--write", action="store_true", help="rewrite the README instead of printing the block")
    render.set_defaults(handler=_render, op="docs render")
    parser.set_defaults(handler=None, op="docs")


def _loader_cell(record: dict[str, Any]) -> str:
    loader = record["inputs"].get("loader")
    api = record["inputs"].get("fabricApi")
    parts = []
    if loader:
        parts.append(f"loader {loader['loaderVersion']}")
    if api:
        parts.append(f"API {api['version']}")
    return " / ".join(parts) or "—"


def render_block(game_id: str) -> str:
    catalog = load_catalog()
    targets = sorted(
        (t for t in catalog.game(game_id).targets if t.status != "retired"),
        key=lambda t: t.id,
    )
    rows = [
        "| Target | Game version | Platform | Loader / API | Java | Support | Verified level |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for target in targets:
        record = target.record
        rows.append(
            f"| `{target.id}` | {target.revision} | {target.platform} | {_loader_cell(record)} "
            f"| {record['runtime']['java']} | {target.status} | {record['verification']['required']} |"
        )
    return "\n".join(rows)


def _render(args: Any) -> int:
    block = render_block(args.game)
    readme = paths.repo_root() / "games" / args.game / "README.md"
    if not readme.is_file():
        raise UsageError(f"no README at {readme.relative_to(paths.repo_root()).as_posix()}")
    text = readme.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise UsageError(f"{readme.name} has no '{BEGIN}' / '{END}' markers; add them where the target table belongs")
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    new_text = text[:start] + "\n" + block + "\n" + text[end:]
    changed = new_text != text
    if args.write and changed:
        readme.write_text(new_text, encoding="utf-8")
    output.emit(
        "docs render",
        True,
        game=args.game,
        file=str(Path("games") / args.game / "README.md"),
        changed=bool(changed and args.write),
        wouldChange=changed,
        block=block,
    )
    return OK
