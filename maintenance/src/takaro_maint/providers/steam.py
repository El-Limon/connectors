"""Steam: a placeholder so the Steam-based connectors have somewhere to land."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Provider, ProviderResult


class SteamProvider(Provider):
    id = "steam"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        raise NotImplementedError("Steam depot downloads arrive with the 7 Days to Die target issue")

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        return ProviderResult(source_id=str(source.get("id", "steam")), status="failed", error="not-implemented")


PROVIDER = SteamProvider()
