# syntax=docker/dockerfile:1
# The maintenance tool container: uv, a managed Python 3.12, steamcmd and the lock-pinned
# DepotDownloader. It runs the checkout mounted at /repo through the same launcher as a host
# or CI run; it contains no code of its own beyond a copy of maintenance/ for the venv.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /uvx /usr/local/bin/

ARG UID=1000
ARG GID=1000
ENV UV_CACHE_DIR=/opt/uv/cache \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PROJECT_ENVIRONMENT=/opt/takaro-maint/venv \
    UV_LINK_MODE=copy \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    HOME=/home/maint

# lib32gcc-s1 / lib32stdc++6 are what the 32-bit steamcmd binary needs; on trixie amd64 they
# install without adding the i386 architecture.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git lib32gcc-s1 lib32stdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g "$GID" maint && useradd -m -u "$UID" -g "$GID" maint \
 && mkdir -p /opt/uv /opt/takaro-maint /opt/steamcmd /repo \
 && chown -R maint:maint /opt/uv /opt/takaro-maint /opt/steamcmd /repo \
 && printf '#!/bin/sh\nexec /opt/steamcmd/steamcmd.sh "$@"\n' > /usr/local/bin/steamcmd \
 && chmod 0755 /usr/local/bin/steamcmd

USER maint
# Valve publishes one unversioned tarball (no checksum; it self-updates on first run). The first
# run happens here so a container run does not spend two minutes updating before it answers.
RUN curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xz -C /opt/steamcmd \
 && steamcmd +quit

COPY --chown=maint:maint . /repo/maintenance
WORKDIR /repo/maintenance
RUN uv python install && uv sync --frozen
# DepotDownloader through the command's own lock-checked acquisition: a changed upstream zip
# (tools.lock.json size/sha256 mismatch) fails this build instead of shipping unknown bytes.
RUN uv run --frozen python -c "from pathlib import Path; from takaro_maint.steam import depotdownloader; print(depotdownloader.ensure(Path.home() / '.cache' / 'takaro-maint'))"

# Set only after the venv exists, so the two builds above still sync. At run time the mounted
# checkout has different timestamps from the baked copy, which would make uv rebuild the
# editable package on every single run and write four lines of its own onto the command's
# stderr. The environment is already exactly what uv.lock describes; rebuild the image when
# uv.lock or tools.lock.json changes.
ENV UV_NO_SYNC=1

WORKDIR /repo
ENTRYPOINT ["/repo/maintenance/bin/takaro-maint"]
