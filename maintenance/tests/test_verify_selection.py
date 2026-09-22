"""What a ``takaro-maint verify`` with no ``--checks`` selects for every game.

The base ladder is written for the Minecraft connector. Each other game declares only
base checks that cannot apply to it and names the game-specific check standing in. A
game's own checks run by default, including checks that truthfully report a known
limitation, except checks declared for an opt-in mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _run(game: str, tmp_path: Path, options: Any = None) -> Any:
    from takaro_maint.catalog.loader import load
    from takaro_maint.verify.runner import RunOptions, TargetRun

    catalog = load()
    options = options or RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection")
    return TargetRun(catalog, catalog.select(game), options)


def _games() -> list[str]:
    from takaro_maint.games import adapters

    return sorted(adapters())


@pytest.mark.parametrize("game", _games())
def test_a_default_run_selects_the_ladder_minus_what_the_game_cannot_pass(game: str, tmp_path: Path) -> None:
    from takaro_maint.verify.runner import check_ids, game_hooks

    run = _run(game, tmp_path)
    try:
        assert run.options.only is None
        run._select_default_checks()

        unsupported = game_hooks(game).unsupported_checks
        if not unsupported:
            # Minecraft: the ladder was written for it, so there is nothing to narrow and
            # `None` -- every check -- is what a bare run means.
            assert run.options.only is None
            return
        conditional = set(game_hooks(game).negative_check_ids) | set(game_hooks(game).hosted_check_ids)
        assert run.options.only == [
            check for check in check_ids(game) if check not in unsupported and check not in conditional
        ]
        assert set(run.options.only).isdisjoint(unsupported)
        assert set(run.options.only).isdisjoint(conditional)
    finally:
        run.cleanup()


def test_a_game_with_nothing_to_narrow_is_left_alone(tmp_path: Path) -> None:
    from takaro_maint.verify.runner import game_hooks

    assert game_hooks("minecraft").unsupported_checks == {}
    run = _run("minecraft", tmp_path)
    try:
        run._select_default_checks()
        assert run.options.only is None
    finally:
        run.cleanup()


@pytest.mark.parametrize("game", _games())
def test_exclusions_are_base_checks_never_a_games_own_checks(game: str) -> None:
    import re

    from takaro_maint.verify.runner import CHECK_IDS, game_hooks

    hooks = game_hooks(game)
    assert set(hooks.unsupported_checks) <= set(CHECK_IDS)
    assert set(hooks.unsupported_checks).isdisjoint(hooks.check_ids)
    forbidden_plan_marker = r"\bF\d\b|follow-" r"up|planning " r"note"
    for reason in hooks.unsupported_checks.values():
        assert not re.search(forbidden_plan_marker, reason, re.IGNORECASE)


def test_an_explicit_checks_list_is_left_byte_identical(tmp_path: Path) -> None:
    """Naming a check is asking for it even when the game normally substitutes another."""
    from takaro_maint.verify.runner import RunOptions, game_hooks

    asked = ["shutdown", "build", "catalog-items"]
    assert set(asked) & set(game_hooks("rust").unsupported_checks)
    options = RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection", only=list(asked))
    run = _run("rust", tmp_path, options)
    try:
        run._select_default_checks()
        assert run.options.only == asked
    finally:
        run.cleanup()


def test_an_explicit_negative_check_is_selected_without_the_convenience_flag(tmp_path: Path) -> None:
    """Naming an opt-in check invokes it; a forgotten result becomes a failure row."""
    from takaro_maint.verify.checks import CheckResult
    from takaro_maint.verify.runner import RunOptions

    asked = ["build", "negative-degraded-hooks"]
    options = RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection", only=asked)
    run = _run("enshrouded", tmp_path, options)
    try:
        assert run.options.negative is False
        assert run.wanted("negative-degraded-hooks") is True
        assert run.selected_check_ids() == tuple(asked)
        run.record(CheckResult("build", "pass", 0))

        run._record_missing_selected_checks()

        missing = next(result for result in run.results if result.id == "negative-degraded-hooks")
        assert missing.status == "fail"
        assert "produced no result" in " ".join(missing.detail["problems"])
    finally:
        run.cleanup()


def test_negative_checks_are_opt_in_for_a_bare_run(tmp_path: Path) -> None:
    from takaro_maint.verify.runner import RunOptions

    plain = _run("enshrouded", tmp_path)
    negative = _run(
        "enshrouded",
        tmp_path,
        RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection", negative=True),
    )
    try:
        plain._select_default_checks()
        negative._select_default_checks()
        assert "negative-degraded-hooks" not in plain.selected_check_ids()
        assert "negative-degraded-hooks" in negative.selected_check_ids()
    finally:
        plain.cleanup()
        negative.cleanup()


def test_one_targets_default_does_not_narrow_the_next_ones(tmp_path: Path) -> None:
    """One ``RunOptions`` is shared by every target of a command; narrowing replaces it."""
    from takaro_maint.verify.runner import RunOptions

    shared = RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection")
    first = _run("rust", tmp_path, shared)
    try:
        first._select_default_checks()
        assert shared.only is None
        assert first.options is not shared
    finally:
        first.cleanup()

    second = _run("terraria", tmp_path, shared)
    try:
        assert second.options.only is None
        second._select_default_checks()
        assert second.options.only != first.options.only
    finally:
        second.cleanup()


def test_startup_budget_prefers_the_cli_then_the_game_then_the_common_default(tmp_path: Path) -> None:
    from takaro_maint.verify.runner import RunOptions

    terraria = _run("terraria", tmp_path)
    minecraft = _run("minecraft", tmp_path)
    explicit = _run(
        "terraria",
        tmp_path,
        RunOptions(artifacts=tmp_path / "dist", out=tmp_path / "out", run_id="selection", startup_timeout=12.0),
    )
    try:
        assert terraria.startup_timeout == 900.0
        assert minecraft.startup_timeout == 300.0
        assert explicit.startup_timeout == 12.0
    finally:
        terraria.cleanup()
        minecraft.cleanup()
        explicit.cleanup()


def test_every_dropped_check_says_why_and_what_stands_in_for_it(tmp_path: Path, capsys: Any) -> None:
    from takaro_maint.verify.runner import game_hooks

    run = _run("valheim", tmp_path)
    try:
        capsys.readouterr()
        run._select_default_checks()
    finally:
        run.cleanup()

    printed = capsys.readouterr().err
    for check, reason in game_hooks("valheim").unsupported_checks.items():
        assert f"not running {check}: {reason}" in printed


def test_a_games_extra_container_options_come_after_the_runners_own(tmp_path: Path) -> None:
    """Docker takes the last value of a repeated option, so a game's own has to win."""
    run = _run("minecraft", tmp_path)
    try:

        class Louder:
            def __init__(self, wrapped: Any) -> None:
                self._wrapped = wrapped

            def container_options(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
                return ["--memory", "14g", "--user", "1:1"]

            def __getattr__(self, name: str) -> Any:
                return getattr(self._wrapped, name)

        run.adapter = Louder(run.adapter)
        argv = run.container_argv("ws://host.docker.internal:1/")
    finally:
        run.cleanup()

    memory = [index for index, item in enumerate(argv) if item == "--memory"]
    assert len(memory) == 2
    assert argv[memory[0] + 1] == "3g"
    assert argv[memory[1] + 1] == "14g"
    assert argv[memory[1] + 2 : memory[1] + 4] == ["--user", "1:1"]
