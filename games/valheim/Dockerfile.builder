# The toolchain the catalog target pins, plus the handful of command-line tools the release
# script uses. TOOLCHAIN arrives as <image>:<tag>@<digest> from the resolved target, so the
# SDK this builds with is the one the record names and nothing else.
ARG TOOLCHAIN
FROM ${TOOLCHAIN}

# No i386 runtime and no SteamCMD: the server files this plugin compiles against are fetched
# by `takaro-maint steam references` from the pinned depot manifests, on the host, before the
# build ever enters this image.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl file jq ripgrep unzip zip \
  && rm -rf /var/lib/apt/lists/*

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    NUGET_PACKAGES=/tmp/nuget

WORKDIR /app
CMD ["bash", "-c", "echo 'Builder container ready' && tail -f /dev/null"]
