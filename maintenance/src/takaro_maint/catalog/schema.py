"""Loading and applying the JSON Schemas under ``catalog/schema/v1``."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .. import paths


@cache
def _registry(schema_dir: str) -> Registry:  # type: ignore[type-arg]
    directory = Path(schema_dir)
    resources = []
    for file in sorted(directory.rglob("*.schema.json")):
        document = json.loads(file.read_text(encoding="utf-8"))
        resource = Resource.from_contents(document, default_specification=DRAFT202012)
        resources.append((document["$id"], resource))
    return Registry().with_resources(resources)  # type: ignore[return-value,arg-type]


def load_schema(name: str) -> dict[str, Any]:
    """``name`` is a path under ``catalog/schema/v1`` such as ``target.schema.json``."""
    return json.loads((paths.schema_dir() / name).read_text(encoding="utf-8"))


def has_schema(name: str) -> bool:
    """Whether ``catalog/schema/v1/<name>`` exists — the kind gate asks before dispatching."""
    return (paths.schema_dir() / name).is_file()


def validator_for(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    return Draft202012Validator(schema, registry=_registry(str(paths.schema_dir())))


def errors_for(name: str, instance: Any) -> list[str]:
    """Human-readable schema errors for ``instance``; empty when it validates."""
    validator = validator_for(name)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]
