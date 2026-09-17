"""`docs render`: the target table in a connector README is generated, not typed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

BEGIN = "<!-- takaro-maint:targets:begin -->"
END = "<!-- takaro-maint:targets:end -->"


def test_the_block_lists_every_live_target(run: Any, catalog_copy: Path) -> None:
    code, payload, _ = run("docs", "render", "--game", "minecraft", repo=catalog_copy)

    assert code == 0, payload
    assert (
        "| `fabric-26.2` | 26.2 | fabric | loader 0.19.5 / API 0.160.0+26.2 | 25 | maintained | protocol |"
        in (payload["block"])
    )


def test_the_block_never_names_a_connector_version(run: Any, catalog_copy: Path) -> None:
    _, payload, _ = run("docs", "render", "--game", "minecraft", repo=catalog_copy)

    assert "0.1.1" not in payload["block"]
    assert "Connector version" not in payload["block"]


def test_a_retired_target_is_left_out(run: Any, catalog_copy: Path) -> None:
    from conftest import read_target, write_target

    record = read_target(catalog_copy)
    retired = dict(record)
    retired["id"] = "fabric-26.1.2"
    retired["revision"] = "26.1.2"
    retired["default"] = False
    retired["support"] = dict(record["support"])
    retired["support"]["status"] = "retired"
    write_target(catalog_copy, retired, "fabric-26.1.2")

    _, payload, _ = run("docs", "render", "--game", "minecraft", repo=catalog_copy)

    assert "fabric-26.1.2" not in payload["block"]


def test_write_updates_the_readme_and_is_idempotent(run: Any, catalog_copy: Path) -> None:
    readme = catalog_copy / "games/minecraft/README.md"

    code, payload, _ = run("docs", "render", "--game", "minecraft", "--write", repo=catalog_copy)
    assert code == 0
    assert payload["changed"] is True
    first = readme.read_text()
    assert "fabric-26.2" in first
    assert first.startswith("# Minecraft")

    code, payload, _ = run("docs", "render", "--game", "minecraft", "--write", repo=catalog_copy)
    assert code == 0
    assert payload["changed"] is False
    assert readme.read_text() == first


def test_text_outside_the_markers_is_untouched(run: Any, catalog_copy: Path) -> None:
    readme = catalog_copy / "games/minecraft/README.md"
    readme.write_text(f"# Minecraft\n\nBefore.\n\n{BEGIN}\nstale\n{END}\n\nAfter.\n")

    run("docs", "render", "--game", "minecraft", "--write", repo=catalog_copy)

    text = readme.read_text()
    assert text.startswith("# Minecraft\n\nBefore.\n")
    assert text.endswith("\nAfter.\n")
    assert "stale" not in text


def test_missing_markers_exit_two(run: Any, catalog_copy: Path) -> None:
    (catalog_copy / "games/minecraft/README.md").write_text("# Minecraft\n\nNo markers here.\n")

    code, payload, _ = run("docs", "render", "--game", "minecraft", "--write", repo=catalog_copy)

    assert code == 2
    assert "markers" in payload["error"]


def test_a_missing_readme_exits_two(run: Any, catalog_copy: Path) -> None:
    (catalog_copy / "games/minecraft/README.md").unlink()

    code, _, _ = run("docs", "render", "--game", "minecraft", repo=catalog_copy)

    assert code == 2


def test_the_repository_readme_block_is_up_to_date(run: Any) -> None:
    """The committed README must already contain what `docs render` would write."""
    code, payload, _ = run("docs", "render", "--game", "minecraft")

    assert code == 0
    assert payload["wouldChange"] is False
