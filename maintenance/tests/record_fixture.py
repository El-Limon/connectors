#!/usr/bin/env python3
"""Record the provider fixtures the scan tests serve. Run by hand; the output is committed.

    uv run --frozen --project maintenance python maintenance/tests/record_fixture.py mojang \
        --out maintenance/tests/fixtures/providers/mojang

Mojang's real manifest lists hundreds of versions and each version JSON is tens of
kilobytes, so the fixture is trimmed to what the scenarios need: the ``latest`` block, the
newest dozen entries (a release plus the snapshots around it) and the handful of older
releases the tests reason about, plus the trimmed per-version documents. Trimming changes
the bytes, so the sha1 values inside the manifest fixture are recomputed by the test
helper rather than trusted from upstream.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
HEAD_ENTRIES = 12
EXTRA_RELEASES = ("26.2", "26.1.2", "26.1.1", "26.1", "1.21.11")
VERSION_DOCUMENTS = ("26.3", "26.2", "26.1.2")
TIMEOUT = 60


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310 - a pinned https URL
        return json.loads(response.read())


def _trim_version(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document["id"],
        "type": document["type"],
        "time": document["time"],
        "releaseTime": document["releaseTime"],
        "javaVersion": document["javaVersion"],
        "downloads": {"server": document["downloads"]["server"]},
    }


def record_mojang(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    manifest = _get(MANIFEST_URL)
    versions: list[dict[str, Any]] = list(manifest["versions"])
    kept = versions[:HEAD_ENTRIES]
    kept_ids = {entry["id"] for entry in kept}
    for entry in versions:
        if entry["id"] in EXTRA_RELEASES and entry["id"] not in kept_ids:
            kept.append(entry)
            kept_ids.add(entry["id"])
    trimmed = {"latest": manifest["latest"], "versions": kept}
    (out / "version_manifest_v2.json").write_text(json.dumps(trimmed, indent=1) + "\n", encoding="utf-8")

    by_id = {entry["id"]: entry for entry in kept}
    for version in VERSION_DOCUMENTS:
        document = _trim_version(_get(by_id[version]["url"]))
        (out / f"{version}.json").write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")

    releases = sorted(entry["id"] for entry in kept if entry["type"] == "release")
    (out / "README.md").write_text(
        "\n".join(
            [
                "# Mojang provider fixtures",
                "",
                f"Recorded from `{MANIFEST_URL}` with:",
                "",
                "```",
                "uv run --frozen --project maintenance python maintenance/tests/record_fixture.py mojang \\",
                "    --out maintenance/tests/fixtures/providers/mojang",
                "```",
                "",
                f"- `version_manifest_v2.json` — `latest` plus the newest {HEAD_ENTRIES} entries and the release",
                f"  entries for {', '.join(EXTRA_RELEASES)}. Releases in the fixture: {', '.join(releases)}.",
                f"- `{'.json`, `'.join(VERSION_DOCUMENTS)}.json` — per-version documents trimmed to the keys the",
                "  provider reads (`id`, `type`, `time`, `releaseTime`, `javaVersion`, `downloads.server`).",
                "",
                "Trimming changes the bytes, so the `sha1` and `url` of each per-version entry in the manifest",
                "fixture no longer describe the file next to it. `test_scan_support.py` recomputes both when it",
                "serves the fixture, which is also what keeps the integrity check in the provider under test.",
                "",
                f"Recorded {manifest['latest']['release']} as the current release head.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["mojang"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return record_mojang(args.out)


if __name__ == "__main__":
    sys.exit(main())
