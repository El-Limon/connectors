"""Argument parsing and the single place every exit code is produced."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import signal
import sys
import traceback
from typing import Any

from . import __version__, exit_codes, output, paths
from .exit_codes import MaintError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="takaro-maint",
        description="Catalog-driven maintenance for the Takaro game connectors.",
    )
    parser.add_argument("--version", action="version", version=f"takaro-maint {__version__}")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository to operate on (default: the checkout this command was installed from)",
    )
    parser.add_argument("--cache-dir", default=None, help="download cache (default: $TAKARO_MAINT_CACHE)")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress on stderr")
    parser.add_argument("-v", "--verbose", action="store_true", help="explain every upstream call on stderr")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    from . import commands

    for module_info in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{commands.__name__}.{module_info.name}")
        register = getattr(module, "register", None)
        if register is not None:
            register(subparsers)
    return parser


def _on_sigint(signum: int, frame: Any) -> None:
    output.error("interrupted")
    raise SystemExit(exit_codes.INTERRUPTED)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _on_sigint)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "handler", None) is None:
        parser.print_help(sys.stderr)
        return exit_codes.USAGE

    output.configure(quiet=args.quiet, verbose=args.verbose)
    paths.set_repo_root(args.repo_root)
    if args.cache_dir:
        import os

        os.environ["TAKARO_MAINT_CACHE"] = args.cache_dir

    try:
        return int(args.handler(args))
    except MaintError as exc:
        output.error(exc.message)
        output.emit(getattr(args, "op", args.command or "unknown"), False, error=exc.message, **exc.detail)
        return exc.code
    except KeyboardInterrupt:
        output.error("interrupted")
        return exit_codes.INTERRUPTED
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001 - the documented "unexpected" exit
        traceback.print_exc()
        return exit_codes.UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
