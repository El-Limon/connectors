"""A new game-specific async verification body must gain a direct pytest driver."""

from __future__ import annotations

import ast

import pytest

from conftest import REPO_ROOT


@pytest.mark.parametrize(
    ("module", "test_name"),
    [
        ("conan_exiles", "conan_exiles"),
        ("enshrouded", "enshrouded"),
        ("rust", "rust"),
        ("seven_days", "7d2d"),
        ("terraria", "terraria"),
        ("valheim", "valheim"),
        ("zomboid", "zomboid"),
    ],
)
def test_every_async_verification_body_has_a_game_test_driver(module: str, test_name: str) -> None:
    source_path = REPO_ROOT / f"maintenance/src/takaro_maint/games/{module}/verify.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    bodies = [node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
    tests = (REPO_ROOT / f"maintenance/tests/test_game_{test_name}.py").read_text(encoding="utf-8")

    missing = [name for name in bodies if f"hooks.{name}" not in tests]
    assert not missing, f"{module} async verification bodies without direct drivers: {missing}"
