# Toolchain for building the Valheim connector on a host with no .NET SDK.
# games/valheim/scripts/setup-environment.sh needs curl/jq/unzip/tar/file plus a
# 32-bit-capable environment for SteamCMD; build-release.sh needs zip and the
# .NET SDK. Nothing in games/valheim/ is modified — this just supplies the tools.
FROM mcr.microsoft.com/dotnet/sdk:8.0

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl file jq ripgrep tar unzip zip \
        lib32gcc-s1 lib32stdc++6 \
    && rm -rf /var/lib/apt/lists/*
