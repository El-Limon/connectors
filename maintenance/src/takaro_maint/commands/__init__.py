"""Sub-commands. Each module exposes ``register(subparsers)`` and is discovered, not listed."""

from __future__ import annotations

import argparse
from typing import Any

from .. import catalog as catalog_pkg
from ..catalog.loader import Catalog, Target


def add_selection_arguments(parser: argparse.ArgumentParser, *, multiple: bool = False) -> None:
    """The selection flags every target-aware command shares."""
    parser.add_argument("--game", required=True, help="catalog game id, e.g. minecraft")
    if multiple:
        parser.add_argument(
            "--target",
            action="append",
            default=None,
            dest="target",
            help="target id; repeatable. Beats the declared default.",
        )
        parser.add_argument(
            "--all-targets",
            action="store_true",
            help="every candidate or maintained target of the game",
        )
    else:
        parser.add_argument("--target", default=None, help="target id; beats the declared default")
    parser.add_argument("--platform", default=None, help="restrict the default lookup to one platform")


def load_catalog() -> Catalog:
    return catalog_pkg.load()


def select_one(args: Any) -> tuple[Catalog, Target]:
    catalog = load_catalog()
    target_id = args.target
    if isinstance(target_id, list):
        target_id = target_id[0] if target_id else None
    return catalog, catalog.select(args.game, target_id=target_id, platform=args.platform)


def select_many(args: Any) -> tuple[Catalog, list[Target]]:
    catalog = load_catalog()
    targets = catalog.selectable(
        args.game,
        target_ids=args.target,
        all_targets=getattr(args, "all_targets", False),
        platform=args.platform,
    )
    return catalog, targets
