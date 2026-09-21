"""The maintenance CI workflow's gates are wired to the configuration they claim to enforce."""

from __future__ import annotations

from conftest import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github/workflows/maintenance-ci.yml"


def test_the_types_gate_loads_the_project_config() -> None:
    """mypy discovers its configuration from the cwd; the workflow runs from the repository root."""
    text = WORKFLOW.read_text()
    assert "mypy --config-file maintenance/pyproject.toml maintenance/src" in text
