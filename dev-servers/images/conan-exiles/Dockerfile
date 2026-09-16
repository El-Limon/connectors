# Conan Exiles dedicated server (Steam app 443030) using the native Linux
# launcher documented in games/conan-exiles/README.md. Game files are downloaded into
# the mounted volume on first boot, not baked into the image, so rebuilding the
# image never re-downloads ~35 GB.
# The non-root variant: SteamCMD fails with a misleading
# "Failed to install app (Missing file permissions)" when run as root.
# Its `steam` user is uid 1000, which matches the host user owning _data.
FROM cm2network/steamcmd:latest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        procps \
        xdg-user-dirs \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER steam

ENV CONAN_APP_ID=443030 \
    CONAN_INSTALL_DIR=/conan \
    CONAN_GAME_PORT=7777 \
    CONAN_QUERY_PORT=27015 \
    CONAN_RCON_PORT=25575

EXPOSE 7777/udp 7778/udp 27015/udp 25575/tcp

ENTRYPOINT ["/entrypoint.sh"]
