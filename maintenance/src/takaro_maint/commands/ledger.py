"""``ledger check`` — does this directory really hold the target we think it does?"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import output
from ..exit_codes import CONFLICT, OK
from ..install.ledger import check_ledger
from . import add_selection_arguments, select_one


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("ledger", help="inspect the installed-target ledger")
    inner = parser.add_subparsers(dest="ledger_command", metavar="<subcommand>")
    check = inner.add_parser("check", help="verify a game directory against the catalog")
    add_selection_arguments(check)
    check.add_argument("--dest", required=True)
    check.set_defaults(handler=_check, op="ledger check")
    parser.set_defaults(handler=None, op="ledger")


def _check(args: Any) -> int:
    _, target = select_one(args)
    dest = Path(args.dest).expanduser().resolve()
    reasons = check_ledger(dest, target.record, target.fingerprint)
    ok = not reasons
    if not ok:
        for reason in reasons:
            output.error(reason)
    output.emit(
        "ledger check",
        ok,
        game=target.game,
        target=target.id,
        fingerprint=target.fingerprint,
        dest=str(dest),
        reasons=reasons,
    )
    return OK if ok else CONFLICT
