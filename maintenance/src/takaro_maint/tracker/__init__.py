"""The GitHub tracker as durable state: marker identity, issues, dashboard, checkpoints.

Nothing here keeps state on disk. What the scan knows lives in the dashboard issue, so an
ephemeral runner with an empty home directory resumes exactly where the last run stopped.
"""

from . import checkpoints, dashboard, identity, issues

__all__ = ["checkpoints", "dashboard", "identity", "issues"]
