"""The local verification harness: a fake Takaro, a container runner and the checks."""

from .fake_takaro import FakeTakaro
from .report import build_report, level_for, write_report

__all__ = ["FakeTakaro", "build_report", "level_for", "write_report"]
