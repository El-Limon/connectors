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

    def input_url(self, input_spec: dict[str, Any], source: dict[str, Any]) -> str | None:
        """The pseudo-URL that names exactly the depot manifests an input pins.

        Steam serves no download link: a build is an app on a branch plus one content
        manifest per depot. Spelling that out as a URL gives reports, compat records and
        ledgers one string that identifies the bytes, and it is stable because the
        manifest ids are.
        """
        del source
        if input_spec.get("kind") != "steam-depots":
            return None
        depots = input_spec.get("depots") or {}
        if not depots:
            return None
        pinned = ";".join(f"{depot}/manifest/{depots[depot]['manifest']}" for depot in sorted(depots))
        return (
            f"steam://app/{input_spec['app']}/branch/{input_spec['branch']}"
            f"/build/{input_spec['buildid']}/depot/{pinned}"
        )

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        return ProviderResult(source_id=str(source.get("id", "steam")), status="failed", error="not-implemented")


PROVIDER = SteamProvider()
