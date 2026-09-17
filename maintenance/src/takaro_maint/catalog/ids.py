"""Identifier and URL grammar shared by every command."""

from __future__ import annotations

from typing import Any


def qualified_id(game: str, target_id: str) -> str:
    return f"{game}/{target_id}"


def expected_target_id(record: dict[str, Any]) -> str:
    return f"{record.get('platform')}-{record.get('revision')}"


def source_of(game: dict[str, Any], source_id: str) -> dict[str, Any]:
    sources = game.get("sources", {})
    if source_id not in sources:
        raise KeyError(f"unknown source '{source_id}' in game '{game.get('id')}'")
    return dict(sources[source_id])


def base_url(game: dict[str, Any], source_id: str) -> str:
    return str(source_of(game, source_id)["baseUrl"]).rstrip("/")


def resolved_url(game: dict[str, Any], source_id: str, path: str) -> str:
    return base_url(game, source_id) + path


def maven_path(group: str, artifact: str, version: str) -> str:
    return f"/{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"


def artifact_file_names(record: dict[str, Any]) -> dict[str, str]:
    """role -> artifact file name, still carrying the ``{version}`` placeholder."""
    return {component["role"]: component["artifact"] for component in record.get("components", [])}


def artifact_file_name(record: dict[str, Any], role: str, version: str) -> str:
    for component in record.get("components", []):
        if component["role"] == role:
            return str(component["artifact"]).replace("{version}", version)
    raise KeyError(f"target '{record.get('id')}' has no component role '{role}'")


def container_ref(image: dict[str, Any]) -> str:
    return f"{image['image']}:{image['tag']}@{image['digest']}"
