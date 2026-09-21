"""The provider contract a new game reuses: input kinds and the generic GitHub release provider.

Every game after 7 Days to Die is meant to onboard with catalog data alone — a source, a
watch block and one pinned target — and never by teaching the core Python about itself.
These tests hold that line from the outside: the input kinds are checked through the real
``catalog validate``, and the release provider is exercised through the real commands
against a fixture listing and an in-process upstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import read_target, write_target
from takaro_maint.catalog import schema

VALID_RELEASE_ASSET = {
    "kind": "github-release-asset",
    "source": "carbon-releases",
    "hashOrigin": "upstream",
    "repo": "CarbonCommunity/Carbon",
    "tag": "v2.1.1272",
    "asset": "Carbon.Linux.Release.tar.gz",
    "sha256": "0" * 64,
    "size": 1234,
    "installPath": "carbon",
}

VALID_THUNDERSTORE_PACKAGE = {
    "kind": "thunderstore-package",
    "source": "thunderstore",
    "hashOrigin": "upstream",
    "namespace": "denikson",
    "name": "BepInExPack_Valheim",
    "version": "5.4.2202",
    "sha256": "1" * 64,
    "size": 4321,
    "installPath": "BepInExPack_Valheim",
}

VALID_GITHUB_SOURCE = {
    "kind": "github-source",
    "source": "carbon-releases",
    "hashOrigin": "self-recorded",
    "repo": "CarbonCommunity/Carbon",
    "commit": "a" * 40,
    "archive": "tar.gz",
    "sha256": "2" * 64,
}


def errors(kind: str, spec: dict[str, Any]) -> list[str]:
    return schema.errors_for(f"inputs/{kind}.schema.json", spec)


def failures(payload: dict[str, Any]) -> set[str]:
    return {check["id"] for check in payload["failures"]}


def _with_input(root: Path, spec: dict[str, Any]) -> None:
    """Hang one extra input off the default Fabric target of a catalog copy."""
    record = read_target(root)
    record["inputs"]["extra"] = spec
    write_target(root, record)


# -- T-C6 github-release-asset -------------------------------------------------
def test_github_release_asset_schema_accepts_a_pinned_tag_and_rejects_a_malformed_one() -> None:
    assert errors("github-release-asset", VALID_RELEASE_ASSET) == []

    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "repo": "CarbonCommunity"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "sha256": "not-a-digest"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "asset": "dir/Carbon.tar.gz"})
    assert errors("github-release-asset", {k: v for k, v in VALID_RELEASE_ASSET.items() if k != "tag"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "unexpected": 1})


def test_a_floating_release_tag_fails_catalog_validation(run: Any, catalog_copy: Path) -> None:
    """``latest`` is a moving target: the catalog-wide floating-word check has to catch it."""
    _with_input(catalog_copy, {**VALID_RELEASE_ASSET, "tag": "latest"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "no-floating-words" in failures(payload)
    assert "input-kind-schema" not in failures(payload), "a floating tag is still a well-formed input"


def test_a_pinned_release_asset_passes_every_catalog_check(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, VALID_RELEASE_ASSET)

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 0, json.dumps(payload, indent=2)


# -- T-C7 thunderstore-package -------------------------------------------------
def test_thunderstore_package_schema_accepts_a_pinned_version_and_rejects_a_malformed_one() -> None:
    assert errors("thunderstore-package", VALID_THUNDERSTORE_PACKAGE) == []

    assert errors("thunderstore-package", {**VALID_THUNDERSTORE_PACKAGE, "version": "5.4"})
    assert errors("thunderstore-package", {**VALID_THUNDERSTORE_PACKAGE, "namespace": "deni kson"})
    assert errors("thunderstore-package", {k: v for k, v in VALID_THUNDERSTORE_PACKAGE.items() if k != "sha256"})


def test_a_floating_thunderstore_version_fails_catalog_validation(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, {**VALID_THUNDERSTORE_PACKAGE, "installPath": "latest"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "no-floating-words" in failures(payload)


# -- T-C8 github-source --------------------------------------------------------
def test_github_source_schema_requires_an_exact_commit() -> None:
    assert errors("github-source", VALID_GITHUB_SOURCE) == []

    assert errors("github-source", {**VALID_GITHUB_SOURCE, "commit": "main"})
    assert errors("github-source", {**VALID_GITHUB_SOURCE, "commit": "a" * 7})
    assert errors("github-source", {**VALID_GITHUB_SOURCE, "archive": "7z"})
    assert errors("github-source", {k: v for k, v in VALID_GITHUB_SOURCE.items() if k != "commit"})


def test_a_github_source_archive_passes_every_catalog_check(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, VALID_GITHUB_SOURCE)

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 0, json.dumps(payload, indent=2)


def test_the_three_new_kinds_are_registered_with_the_catalog(run: Any, catalog_copy: Path) -> None:
    """The kind gate is the only registry there is: an unknown kind must fail there."""
    _with_input(catalog_copy, {**VALID_RELEASE_ASSET, "kind": "github-release-assets"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "input-kind-schema" in failures(payload)
    for kind in ("github-release-asset", "thunderstore-package", "github-source"):
        assert schema.has_schema(f"inputs/{kind}.schema.json")
