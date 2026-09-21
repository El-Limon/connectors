"""Game adapters, discovered by module rather than by a hand-maintained registry."""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache

from ..exit_codes import UsageError
from .base import BuildResult, GameAdapter, common_env

__all__ = ["BuildResult", "GameAdapter", "adapter_for", "adapters", "common_env"]


@lru_cache(maxsize=1)
def adapters() -> dict[str, GameAdapter]:
    found: dict[str, GameAdapter] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        game = getattr(module, "GAME", None)
        if game is not None:
            found[game.id] = game
    return found


def adapter_for(game_id: str) -> GameAdapter:
    try:
        return adapters()[game_id]
    except KeyError:
        known = ", ".join(sorted(adapters())) or "<none>"
        raise UsageError(f"no game adapter for '{game_id}'; known adapters: {known}") from None
