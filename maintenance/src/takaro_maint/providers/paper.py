"""PaperMC: the server jar for one (project, game version, build).

Fill v3 serves every build by a content-addressed URL whose path segment *is* the jar's
sha256, so the pin and the address are the same fact. Nothing here consults a build listing:
the build number was resolved once, at record time, and a listing lookup would silently
follow upstream to a different jar.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import net
from ..exit_codes import UsageError
from .base import Provider, ProviderResult

_OBJECT_PATH = re.compile(r"^/v1/objects/(?P<sha256>[0-9a-f]{64})/")


class PaperProvider(Provider):
    id = "paper"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        if input_spec["kind"] != "paper-build":
            return super().fetch_input(input_spec, source, dest, cache)
        # The URL carries the hash. A record whose two halves disagree is a broken record,
        # not a broken download, so it is refused before anything is requested.
        match = _OBJECT_PATH.match(str(input_spec["path"]))
        addressed = match.group("sha256") if match else None
        if addressed != input_spec["sha256"]:
            raise UsageError(
                f"the Paper input contradicts itself: the content-addressed path names {addressed} "
                f"but sha256 is {input_spec['sha256']}; nothing was downloaded",
                path=input_spec["path"],
                sha256=input_spec["sha256"],
            )
        blob = net.download(
            source["url"],
            net.Expectation(sha256=input_spec["sha256"], size=input_spec.get("size")),
            cache,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError("Paper observation arrives with #154")


PROVIDER = PaperProvider()
