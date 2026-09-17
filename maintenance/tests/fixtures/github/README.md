# Tracker fixtures

Golden renderings of what the scan writes, compared byte for byte by the tests so a change
to an issue body is always a deliberate one.

- `support-issue-26.3.md` — the owned block of a stable-release support issue, rendered
  from the pinned observation and target table in `test_scan_support.py` with the clock
  frozen. The revision, hashes and release time are the real upstream values for Minecraft
  26.3.
- `dashboard.md` — a whole dashboard body: the human tables above the machine block, with
  one bootstrapped source and one filed piece of work.

Both are rendered from literals rather than from the live catalog, so adding a target
record cannot silently rewrite them. Regenerate them only when the body is meant to
change, and read the diff before committing it.
