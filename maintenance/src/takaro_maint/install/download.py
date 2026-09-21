"""Turning a target's ``inputs`` into concrete downloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog import ids
from ..exit_codes import UsageError
from ..providers import provider_for


@dataclass(frozen=True)
class InputPlan:
    """One input the install has to place on disk."""

    name: str
    kind: str
    provider: str
    source: dict[str, Any]
    spec: dict[str, Any]
    install_path: str
    url: str

    def fetch(self, dest: Path, cache: Path) -> Path:
        return provider_for(self.provider).fetch_input(self.spec, self.source, dest, cache)

    def expectation(self) -> dict[str, Any]:
        if self.kind == "mojang-version":
            return {
                "sha1": self.spec["server"]["sha1"],
                "size": int(self.spec["server"]["size"]),
            }
        return {"sha256": self.spec["sha256"]}


def plan_inputs(game_record: dict[str, Any], target_record: dict[str, Any]) -> list[InputPlan]:
    """Every input with an ``installPath``, in a stable order."""
    plans: list[InputPlan] = []
    for name, spec in target_record["inputs"].items():
        kind = spec["kind"]
        if kind == "mojang-version":
            server = spec["server"]
            manifest_url = ids.resolved_url(game_record, spec["source"], spec["manifest"]["path"])
            server_url = ids.resolved_url(game_record, server["source"], server["path"])
            source = {
                **ids.source_of(game_record, spec["source"]),
                "manifestUrl": manifest_url,
                "serverUrl": server_url,
            }
            plans.append(
                InputPlan(
                    name=name,
                    kind=kind,
                    provider=source["provider"],
                    source=source,
                    spec=spec,
                    install_path=server["installPath"],
                    url=server_url,
                )
            )
            continue
        install_path = spec.get("installPath")
        if not install_path:
            continue  # build-only input
        # Not every input kind is a path under its source's baseUrl: a GitHub release asset
        # is addressed by repo/tag/asset, a Thunderstore package by namespace/name/version.
        # ``input_url`` asks the source's provider whenever the input names no ``path``.
        url = ids.input_url(game_record, spec)
        if not url:
            raise UsageError(
                f"input '{name}' of kind '{kind}' asks to be installed at '{install_path}', "
                f"but provider '{ids.source_of(game_record, spec['source'])['provider']}' names no URL for it"
            )
        source = {**ids.source_of(game_record, spec["source"]), "url": url}
        plans.append(
            InputPlan(
                name=name,
                kind=kind,
                provider=source["provider"],
                source=source,
                spec=spec,
                install_path=install_path,
                url=url,
            )
        )
    plans.sort(key=lambda plan: plan.name)
    return plans
