# Mojang provider fixtures

Recorded from `https://piston-meta.mojang.com/mc/game/version_manifest_v2.json` with:

```
uv run --frozen --project maintenance python maintenance/tests/record_fixture.py mojang \
    --out maintenance/tests/fixtures/providers/mojang
```

- `version_manifest_v2.json` — `latest` plus the newest 12 entries and the release
  entries for 26.2, 26.1.2, 26.1.1, 26.1, 1.21.11. Releases in the fixture: 1.21.11, 26.1, 26.1.1, 26.1.2, 26.2, 26.3.
- `26.3.json`, `26.2.json`, `26.1.2.json` — per-version documents trimmed to the keys the
  provider reads (`id`, `type`, `time`, `releaseTime`, `javaVersion`, `downloads.server`).

Trimming changes the bytes, so the `sha1` and `url` of each per-version entry in the manifest
fixture no longer describe the file next to it. `test_scan_support.py` recomputes both when it
serves the fixture, which is also what keeps the integrity check in the provider under test.

Recorded 26.3 as the current release head.
