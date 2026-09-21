"""stdout is exactly one JSON document per run; diagnostics go to stderr."""

from __future__ import annotations

import json
import sys
from typing import Any

from . import redact

_QUIET = False
_VERBOSE = False


def configure(*, quiet: bool = False, verbose: bool = False) -> None:
    global _QUIET, _VERBOSE
    _QUIET = quiet
    _VERBOSE = verbose


def emit(op: str, ok: bool, **fields: Any) -> None:
    """Write the single stdout JSON document for a command."""
    document: dict[str, Any] = {"schemaVersion": 1, "op": op, "ok": ok}
    document.update(fields)
    sys.stdout.write(redact.redact(json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False)) + "\n")
    sys.stdout.flush()


def raw(text: str) -> None:
    """Write a non-JSON stdout format (env, gha, table)."""
    sys.stdout.write(redact.redact(text))
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def info(message: str) -> None:
    if not _QUIET:
        sys.stderr.write(redact.redact(message) + "\n")
        sys.stderr.flush()


def warn(message: str) -> None:
    sys.stderr.write(redact.redact("warning: " + message) + "\n")
    sys.stderr.flush()


def debug(message: str) -> None:
    if _VERBOSE:
        sys.stderr.write(redact.redact("debug: " + message) + "\n")
        sys.stderr.flush()


def error(message: str) -> None:
    sys.stderr.write(redact.redact("error: " + message) + "\n")
    sys.stderr.flush()
