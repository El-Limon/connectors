"""FabricMC: the server launcher jar and the Maven artifacts a target builds against."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import net
from .base import Provider, ProviderResult


class FabricProvider(Provider):
    id = "fabric"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        if input_spec["kind"] not in ("fabric-launcher", "maven-artifact", "http-file"):
            return super().fetch_input(input_spec, source, dest, cache)
        blob = net.download(source["url"], net.Expectation(sha256=input_spec["sha256"]), cache)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError("Fabric loader/API observation arrives with #153/#154")


PROVIDER = FabricProvider()
