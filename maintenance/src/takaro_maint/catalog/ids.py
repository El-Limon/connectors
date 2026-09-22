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


def input_url(game: dict[str, Any], spec: dict[str, Any]) -> str | None:
    """The single URL an input names, or ``None`` when its mechanism names none.

    An input that carries a ``path`` is one HTTP GET against its source's ``baseUrl``.
    Anything else is asked of the source's provider, so an acquisition mechanism that is
    not a plain download ships its own URL grammar next to its downloader instead of
    being listed here.
    """
    if "path" in spec:
        return resolved_url(game, spec["source"], spec["path"])
    from ..providers import provider_for

    source = source_of(game, spec["source"])
    url = provider_for(str(source["provider"])).input_url(spec, source)
    return str(url) if url else None


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
