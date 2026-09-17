# syntax=docker/dockerfile:1
# Runtime image for the RELEASE tarball, where dist/ is already built.
# Building from a source checkout instead? Use Dockerfile.dev.
FROM node:22-alpine
ENV NODE_ENV=production \
    TAKARO_PLUGIN_URL=http://127.0.0.1:18890 \
    TAKARO_CURSOR_FILE=/data/event-cursor.json
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY dist ./dist
RUN mkdir -p /data && chown node:node /data
USER node
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:${SIDECAR_HEALTH_PORT:-18891}/health >/dev/null || exit 1
CMD ["node", "dist/index.js"]
