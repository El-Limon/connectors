"""Provider registry, discovered by module."""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache

from ..exit_codes import UsageError
from .base import Observation, Provider, ProviderResult

__all__ = ["Observation", "Provider", "ProviderResult", "provider_for", "providers"]


@lru_cache(maxsize=1)
def providers() -> dict[str, Provider]:
    found: dict[str, Provider] = {}
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        provider = getattr(module, "PROVIDER", None)
        if provider is not None:
            found[provider.id] = provider
    return found


def provider_for(provider_id: str) -> Provider:
    try:
        return providers()[provider_id]
    except KeyError:
        known = ", ".join(sorted(providers())) or "<none>"
        raise UsageError(f"no provider '{provider_id}'; known providers: {known}") from None
