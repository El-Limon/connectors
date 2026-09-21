# `ghcr.io/pryaxis/tshock`, as the registry served it

Recorded 2026-09-21, anonymously. ghcr.io answers a public repository only after a bearer
challenge, so every call below carries a token obtained first:

```
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:pryaxis/tshock:pull" | jq -r .token)

curl -sH "Authorization: Bearer $TOKEN" \
  "https://ghcr.io/v2/pryaxis/tshock/tags/list?n=1000" > tags-list.json

for tag in 6.1.0 6.0.0 stable; do
  curl -sH "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "https://ghcr.io/v2/pryaxis/tshock/manifests/$tag" > "manifests/$tag.index.json"
done

curl -sH "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  "https://ghcr.io/v2/pryaxis/tshock/manifests/sha256:29f877e0…" > manifests/29f877e0.manifest.json

curl -sLH "Authorization: Bearer $TOKEN" \
  "https://ghcr.io/v2/pryaxis/tshock/blobs/sha256:0a40c4aa…" > blobs/0a40c4aa.config.json
```

## Why the manifests are raw bytes

`manifests/*.json` and `blobs/*.json` are **byte-for-byte** what the registry served and must not
be reformatted. A manifest's digest is the sha256 of exactly those bytes — it is the identity the
catalog pins and the provider re-computes — so a pretty-printed copy would be a different image.
`test_game_terraria.py` asserts the recorded digests before it asserts anything else:

| file | sha256 |
| --- | --- |
| `manifests/6.1.0.index.json` | `911459f0ce02014a64c197647a16e9ee57e4d16695de8cfda1f1b552af56ab43` |
| `manifests/6.0.0.index.json` | `5131efd03a96fc048500a75afcc32248d204242156ef50bd6ad8e4750e28a45a` |
| `manifests/stable.index.json` | `b40db2c722aabcf4a3a757f761f9b85f0d7208da824f77d03e4ae183871a35fc` |
| `manifests/29f877e0.manifest.json` | the linux/amd64 manifest of `6.1.0` |
| `blobs/0a40c4aa.config.json` | that manifest's config blob, which carries the image labels |

`stable.index.json` is here because it is the tag this connector used to run on. It resolves to a
different digest than `6.1.0` while being built from the same source revision, which is exactly
why a floating tag is not a pin — and no test ever lets the provider fetch it.

## What was trimmed

`tags-list.json` keeps the version tags, the floating ones (`latest`, `stable`, `6`, `6.1`) and
the branch tags, and drops the `sha256-*` signature tags — 259 entries down to 40. The floating
tags are kept on purpose: "never observed" is worth asserting only against a listing that offers
them. Nothing else in any file was changed.
