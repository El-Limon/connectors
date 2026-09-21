"""Fabric specifics: the env the rig and the container need, and how a Fabric jar is built."""

from __future__ import annotations

import json
import re
from typing import Any

# "Loading Minecraft 26.2 with Fabric Loader 0.19.5"
_LOADING = re.compile(r"Loading Minecraft (?P<game>\S+) with Fabric Loader (?P<loader>\S+)")
TARGET_CHECK_PREFIX = "Takaro target-check: "


def env(resolved: dict[str, Any], prefix: str) -> dict[str, str]:
    """Fabric adds the loader/launcher coordinates the itzg image needs."""
    loader = resolved["inputs"]["loader"]
    return {
        f"{prefix}_VERSION": str(resolved["revision"]),
        f"{prefix}_LOADER_VERSION": str(loader["loaderVersion"]),
        f"{prefix}_LAUNCHER_VERSION": str(loader["launcherVersion"]),
        f"{prefix}_LAUNCHER": str(loader["installPath"]),
    }


def runtime_env(resolved: dict[str, Any]) -> dict[str, str]:
    """The container environment recorded on the target itself."""
    return dict(resolved["runtime"]["container"].get("env", {}))


def parse_runtime_identity(log_line: str) -> dict[str, Any] | None:
    """Either the Fabric banner or the connector's own target-check line."""
    match = _LOADING.search(log_line)
    if match:
        return {"gameVersion": match.group("game"), "loader": "fabric", "loaderVersion": match.group("loader")}
    index = log_line.find(TARGET_CHECK_PREFIX)
    if index >= 0:
        try:
            payload = json.loads(log_line[index + len(TARGET_CHECK_PREFIX) :].strip())
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return {"targetCheck": payload}
    return None
