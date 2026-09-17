"""Mojang piston: the version manifest and the dedicated server jar it names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import net
from ..exit_codes import IntegrityError
from .base import Provider, ProviderResult


class MojangProvider(Provider):
    id = "mojang"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        """Fetch the server jar, having confirmed the manifest that vouches for it."""
        if input_spec["kind"] != "mojang-version":
            return super().fetch_input(input_spec, source, dest, cache)
        manifest_url = source["manifestUrl"]
        manifest_blob = net.download(
            manifest_url,
            net.Expectation(sha1=input_spec["manifest"]["sha1"]),
            cache,
        )
        manifest = json.loads(manifest_blob.read_text(encoding="utf-8"))
        declared = manifest["downloads"]["server"]
        expected = input_spec["server"]
        if declared["sha1"] != expected["sha1"] or int(declared["size"]) != int(expected["size"]):
            raise IntegrityError(
                f"{manifest_url}: manifest names server sha1 {declared['sha1']} size {declared['size']}, "
                f"the catalog pins sha1 {expected['sha1']} size {expected['size']}",
                url=manifest_url,
            )
        blob = net.download(
            source["serverUrl"],
            net.Expectation(sha1=expected["sha1"], size=int(expected["size"])),
            cache,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError("Mojang release observation arrives with #153/#154")


PROVIDER = MojangProvider()
