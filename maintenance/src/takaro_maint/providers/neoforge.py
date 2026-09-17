"""NeoForge: the server installer and the universal jar, gated by upstream's own sidecars.

Every artifact under maven.neoforged.net is published next to a ``.sha256`` file. That
sidecar is fetched first and compared with the catalog, so a re-published version is
refused before its bytes are pulled at all.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from .. import net
from ..exit_codes import IntegrityError, UpstreamUnavailable
from .base import Provider, ProviderResult

_SIDECAR = re.compile(r"^[0-9a-f]{64}")


class NeoForgeProvider(Provider):
    id = "neoforge"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        if input_spec["kind"] not in ("neoforge-installer", "http-file"):
            return super().fetch_input(input_spec, source, dest, cache)
        url = str(source["url"])
        expected = input_spec["sha256"]
        upstream = self._sidecar_hash(url)
        if upstream != expected:
            raise IntegrityError(
                f"{url}.sha256 disagrees with the catalog: upstream says {upstream}, the record says {expected}",
                url=f"{url}.sha256",
                sidecar=upstream,
                catalog=expected,
            )
        blob = net.download(url, net.Expectation(sha256=expected, size=input_spec.get("size")), cache)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    @staticmethod
    def _sidecar_hash(url: str) -> str:
        """The sha256 upstream publishes beside ``url``. Never cached: it is the freshness check."""
        sidecar_url = f"{url}.sha256"
        with tempfile.TemporaryDirectory(prefix="takaro-maint-sidecar-") as tmp:
            sidecar = Path(tmp) / "sha256"
            net.fetch(sidecar_url, sidecar, net.Expectation(), no_cache=True)
            text = sidecar.read_text(encoding="utf-8", errors="replace").strip()
        match = _SIDECAR.match(text)
        if not match:
            raise UpstreamUnavailable(f"{sidecar_url} is not a sha256 sidecar", url=sidecar_url)
        return match.group(0)

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError("NeoForge observation arrives with #154")


PROVIDER = NeoForgeProvider()
