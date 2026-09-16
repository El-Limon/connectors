# Third-party Palworld <-> Takaro bridge, built from source at a pinned ref.
# Source: https://github.com/mad-001/Palworld-Bridge
#
# NOTE: we deliberately do NOT use the published release tarball. Its prebuilt
# dist/index.js does `require('./embedded-mod')`, but that file is not shipped
# and the scripts/ directory that generates it is absent too, so the release
# asset crashes on startup with MODULE_NOT_FOUND. Building from the repo runs
# the package's own `prebuild` (scripts/generate-embedded-mod.js) and produces
# a complete dist.
FROM node:22-slim

# Which checkout to build. Defaults reproduce the stock upstream v1.7.6 image.
# To build a fork branch instead:
#   --build-arg BRIDGE_REPO=El-Limon/Palworld-Bridge \
#   --build-arg BRIDGE_REF=fix/v1.7.6-hardtest \
#   --build-arg BRIDGE_REF_TYPE=heads
ARG BRIDGE_REPO=mad-001/Palworld-Bridge
ARG BRIDGE_REF=v1.7.6
# "tags" for a release tag, "heads" for a branch.
ARG BRIDGE_REF_TYPE=tags
# Optional: sha256 of the source tarball. When set, the download is verified.
ARG BRIDGE_SHA256=

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bridge

RUN curl -fsSL -o /tmp/bridge.tar.gz \
        "https://codeload.github.com/${BRIDGE_REPO}/tar.gz/refs/${BRIDGE_REF_TYPE}/${BRIDGE_REF}" \
    && if [ -n "${BRIDGE_SHA256}" ]; then \
           echo "${BRIDGE_SHA256}  /tmp/bridge.tar.gz" | sha256sum -c -; \
       fi \
    && tar -xzf /tmp/bridge.tar.gz -C /bridge --strip-components=1 \
    && rm /tmp/bridge.tar.gz \
    && npm install --no-audit --no-fund \
    && npm run build \
    && test -f dist/index.js \
    && test -f dist/embedded-mod.js \
    && npm prune --omit=dev \
    && apt-get purge -y curl && apt-get autoremove -y

# TakaroConfig.txt is bind-mounted by compose; the bridge reads it from cwd.
CMD ["node", "dist/index.js"]
