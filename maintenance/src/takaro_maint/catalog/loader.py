"""Reading the catalog tree and selecting a target from it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import fingerprint as fp
from .. import paths
from ..exit_codes import TargetError, UsageError
from . import ids


@dataclass(frozen=True)
class Target:
    """One target record plus where it came from."""

    record: dict[str, Any]
    path: Path

    @property
    def id(self) -> str:
        return str(self.record["id"])

    @property
    def game(self) -> str:
        return str(self.record["game"])

    @property
    def platform(self) -> str:
        return str(self.record["platform"])

    @property
    def revision(self) -> str:
        return str(self.record["revision"])

    @property
    def status(self) -> str:
        return str(self.record["support"]["status"])

    @property
    def is_default(self) -> bool:
        return bool(self.record.get("default", False))

    @property
    def fingerprint(self) -> str:
        return fp.fingerprint(self.record)

    @property
    def fp16(self) -> str:
        return self.fingerprint[:16]

    @property
    def rig_game(self) -> str | None:
        dev = self.record.get("devServers") or {}
        value = dev.get("gameId")
        return str(value) if value else None

    def summary(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "id": self.id,
            "platform": self.platform,
            "revision": self.revision,
            "fp16": self.fp16,
            "status": self.status,
        }


@dataclass(frozen=True)
class Game:
    record: dict[str, Any]
    path: Path
    targets: list[Target] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.record["id"])


@dataclass(frozen=True)
class Catalog:
    root: Path
    games: dict[str, Game]

    def game(self, game_id: str) -> Game:
        if game_id not in self.games:
            known = ", ".join(sorted(self.games)) or "<none>"
            raise UsageError(f"unknown game '{game_id}'; the catalog holds: {known}")
        return self.games[game_id]

    def all_targets(self) -> list[Target]:
        return [target for game in self.games.values() for target in game.targets]

    def select(
        self,
        game_id: str,
        *,
        target_id: str | None = None,
        platform: str | None = None,
    ) -> Target:
        """``--target`` wins; else the single ``default: true`` record of the game, or of ``platform``."""
        game = self.game(game_id)
        candidates = [t for t in game.targets if platform is None or t.platform == platform]
        if target_id is not None:
            for target in candidates:
                if target.id == target_id:
                    return target
            known = ", ".join(sorted(t.id for t in candidates)) or "<none>"
            raise TargetError(f"no target '{target_id}' for game '{game_id}'; known targets: {known}")
        defaults = [t for t in candidates if t.is_default]
        if len(defaults) == 1:
            return defaults[0]
        scope = f"game '{game_id}'" + (f" platform '{platform}'" if platform else "")
        if not defaults:
            raise TargetError(f"{scope} declares no default target; pass --target")
        names = ", ".join(sorted(t.id for t in defaults))
        hint = "pass --target" if platform else "pass --target, or --platform to take one platform's default"
        raise TargetError(f"{scope} declares {len(defaults)} default targets ({names}); {hint}")

    def selectable(
        self,
        game_id: str,
        *,
        target_ids: list[str] | None = None,
        all_targets: bool = False,
        platform: str | None = None,
    ) -> list[Target]:
        if all_targets:
            game = self.game(game_id)
            chosen = [t for t in game.targets if t.status != "retired" and (platform is None or t.platform == platform)]
            if not chosen:
                raise TargetError(f"game '{game_id}' has no candidate or maintained targets")
            return chosen
        if target_ids:
            return [self.select(game_id, target_id=tid, platform=platform) for tid in target_ids]
        return [self.select(game_id, platform=platform)]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: invalid JSON ({exc})") from exc


def load(root: Path | None = None) -> Catalog:
    """Read every ``<game>/game.json`` and its ``targets/*.json``."""
    catalog_root = root if root is not None else paths.catalog_root()
    if not catalog_root.is_dir():
        raise UsageError(f"no catalog at {catalog_root}")
    games: dict[str, Game] = {}
    for game_file in sorted(catalog_root.glob("*/game.json")):
        record = _read_json(game_file)
        targets: list[Target] = []
        for target_file in sorted((game_file.parent / "targets").glob("*.json")):
            targets.append(Target(record=_read_json(target_file), path=target_file))
        game = Game(record=record, path=game_file, targets=targets)
        games[game.id] = game
    return Catalog(root=catalog_root, games=games)


def resolve(catalog: Catalog, target: Target, *, prefix: str = "TAKARO_TARGET") -> dict[str, Any]:
    """The full resolution every entry point consumes."""
    from ..games import adapter_for

    game = catalog.game(target.game)
    record = target.record
    urls: dict[str, str] = {}
    for name, spec in record["inputs"].items():
        if spec["kind"] == "mojang-version":
            urls[f"{name}.manifest"] = ids.resolved_url(game.record, spec["source"], spec["manifest"]["path"])
            urls[f"{name}.server"] = ids.resolved_url(game.record, spec["server"]["source"], spec["server"]["path"])
        else:
            urls[name] = ids.resolved_url(game.record, spec["source"], spec["path"])

    resolved: dict[str, Any] = dict(record)
    resolved["fingerprint"] = target.fingerprint
    resolved["fp16"] = target.fp16
    resolved["artifactFileNames"] = ids.artifact_file_names(record)
    resolved["containerRef"] = ids.container_ref(record["runtime"]["container"])
    resolved["toolchainRef"] = ids.container_ref(record["build"]["toolchain"])
    resolved["resolvedUrls"] = urls
    resolved["env"] = adapter_for(target.game).env(resolved, prefix)
    return resolved
