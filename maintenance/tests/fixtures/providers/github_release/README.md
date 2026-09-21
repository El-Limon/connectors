# `GET /repos/CarbonCommunity/Carbon/releases` (Carbon, the Rust modding framework)

Recorded 2026-09-21, unauthenticated, with the tool's own user agent:

```
curl -A "takaro-connectors-maint/0.1.0 (+https://github.com/gettakaro/connectors)" \
  "https://api.github.com/repos/CarbonCommunity/Carbon/releases?per_page=4" > carbon-releases.json
```

`carbon-releases.json` keeps only the fields the provider reads — `tag_name`, `name`,
`prerelease`, `draft`, `published_at`, and per asset `name`, `size`, `digest`,
`updated_at`, `browser_download_url` — and only the Linux assets. Everything else (author,
uploader, reactions, the API's own URLs, the Windows assets) is cut.

Carbon is in the fixture because it is the awkward shape, not the easy one:

- its tags are **rolling**. `production_build`, `edge_build` and `rustbeta_staging_build`
  are re-uploaded in place, so the tag alone is not a revision and a scan that trusted it
  would never notice a new build. The asset `digest` is what distinguishes two uploads.
- one release carries **several** matching-looking assets (`Carbon.Linux.Release.tar.gz`,
  `Carbon.Linux.Minimal.tar.gz`, `Carbon.Linux.Debug.tar.gz` and their `.info` siblings),
  so an asset pattern that matches more than one has to be an error rather than a guess.
- the display name carries the real version (`Production Build — v2.0.259`) while the tag
  does not, which is what `versionPattern` is for.

What GitHub served that day: `production_build` = `Production Build — v2.0.259`, not a
prerelease, published 2026-09-06, with `Carbon.Linux.Release.tar.gz` at digest
`sha256:bfc3cf3d…` (21387905 bytes).
