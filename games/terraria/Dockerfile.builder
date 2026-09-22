# The catalog target's build.toolchain, by tag and digest. `takaro-maint targets resolve`
# prints the same reference as TERRARIA_TOOLCHAIN, and a test compares the two: the image
# this file names and the image the catalog pins are one image or CI fails.
FROM mcr.microsoft.com/dotnet/sdk:9.0.318-bookworm-slim@sha256:01fabc4758d1d74e39eda700c8463dae6241a61481f973683692ddcb59a5eeb7

WORKDIR /app

# zip only: the release archives are packaged in here because the host has no zip and the
# bytes must not depend on who built them (scripts/lib/package.sh).
RUN apt-get update \
  && apt-get install -y --no-install-recommends zip \
  && rm -rf /var/lib/apt/lists/*

CMD ["bash", "-c", "echo 'Builder container ready' && tail -f /dev/null"]
