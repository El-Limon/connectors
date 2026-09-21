<!-- takaro-maint:owned:begin -->
## Observation

| Field | Value |
| --- | --- |
| Provider / component / branch | mojang / minecraft / release |
| Revision | `26.3` |
| Released | 2026-09-15T11:23:02+00:00 |
| Version list | https://piston-meta.mojang.com/mc/game/version_manifest_v2.json |
| Version manifest | https://piston-meta.mojang.com/v1/packages/96c00d95a31328714d3811cfade2804bb050e455/26.3.json (sha1 `96c00d95a31328714d3811cfade2804bb050e455`) |
| Server jar | https://piston-data.mojang.com/v1/objects/33680f5f2ac32864d6d7cf5e56a705fdb3e05f4c/server.jar (sha1 `33680f5f2ac32864d6d7cf5e56a705fdb3e05f4c`, 62294556 bytes) |
| Java | 25 |
| Observed | 2026-09-17T12:00:00Z by takaro-maint scan |

## Affected targets

| Platform | Target | Revision | Support | Covers 26.3 |
| --- | --- | --- | --- | --- |
| fabric | `fabric-26.2` | 26.2 | maintained | no |
| neoforge | — | — | — | no |
| paper | — | — | — | no |

## Readiness

Framework readiness is not assessed by this scan; it arrives with platform-readiness tracking. Nothing here changes a target's support status.

<!-- takaro-maint:state=detected -->

## Next steps (reproducible)

1. `maintenance/bin/takaro-maint targets list --game minecraft`
2. Add `catalog/minecraft/targets/<platform>-26.3.json` following `catalog/README.md` → "Adding a target" (pin manifest sha1 `96c00d95a31328714d3811cfade2804bb050e455`, server sha1 `33680f5f2ac32864d6d7cf5e56a705fdb3e05f4c` size `62294556`).
3. `maintenance/bin/takaro-maint catalog validate --online`
4. `maintenance/bin/takaro-maint build --game minecraft --target <platform>-26.3 --version <version> --out dist`
5. `maintenance/bin/takaro-maint verify --game minecraft --target <platform>-26.3 --artifacts dist --out reports`

## Expected evidence

- `catalog validate --online` exit 0 on the new record
- `reports/<target>/report.json` with `level` ≥ `protocol` and every check `pass`
- a pull request whose body contains `Refs #<this issue>`

## Acceptance

- [ ] a catalog target for 26.3 exists with `support.status: candidate` and upstream hashes
- [ ] the connector builds and verifies against it (report attached or linked)
- [ ] the target is promoted to `maintained` with the evidence cited in `support.evidence`
<!-- takaro-maint:owned:end -->
