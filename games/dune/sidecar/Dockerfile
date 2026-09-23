# syntax=docker/dockerfile:1
# Runtime image for the RELEASE tarball, where dist/ is already built.
#
# NOTE: this file is authored in the public repository, not mirrored from the campaign source —
# the published image regenerates the item catalogue instead of bundling it (data/ATTRIBUTION.md).
FROM node:22-alpine
ENV NODE_ENV=production \
    TAKARO_DATA_DIR=/data \
    DUNE_ITEMS_FILE=/app/data/items.json \
    DUNE_ENTITIES_FILE=/app/data/entities.json
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY dist ./dist
COPY data ./data
COPY scripts ./scripts
COPY docker-entrypoint.sh ./
# Build the full item catalogue from the community wiki API. It needs outbound HTTPS; if the
# build has none, the placeholder stays and the entrypoint retries on first start.
RUN node scripts/gen-catalogue.mjs --out /app/data \
    || echo "catalogue: not generated at build time, the entrypoint will retry on first start"
RUN chmod +x docker-entrypoint.sh && mkdir -p /data && chown -R node:node /data /app/data
USER node
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:${SIDECAR_HEALTH_PORT:-18891}/health >/dev/null || exit 1
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["node", "dist/index.js"]
