# DayZ dedicated server (Steam app 223350, Linux depot) for the Takaro dev fleet.
#
# The DayZ server depot is NOT available to `login anonymous` (BI T179224), so this
# image never tries to log in by itself: game files are expected to be pre-fetched
# into the mounted volume (`_data/dayz/server` -> /dayz). SteamCMD is present only
# as an opt-in update path for when STEAM_USER is supplied.
#
# cm2network/steamcmd's `steam` user is uid 1000, matching the host user that owns
# _data — the same reason images/conan-exiles uses it.
FROM cm2network/steamcmd:latest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        procps \
        tcpdump \
        libcap2-bin \
        locales \
    && rm -rf /var/lib/apt/lists/* \
    # tcpdump inside the container netns is the "did RestApi open a socket to the
    # sidecar?" oracle for the M0 ladder; give it capture rights without root.
    && setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump || true

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER steam

ENV DAYZ_APP_ID=223350 \
    DAYZ_INSTALL_DIR=/dayz \
    DAYZ_PORT=2302 \
    DAYZ_QUERY_PORT=27016 \
    DAYZ_RCON_PORT=2310 \
    DAYZ_MAX_PLAYERS=8 \
    DAYZ_BATTLEYE=1 \
    DAYZ_SERVERMOD=@takarointegration \
    DAYZ_EXTRA_ARGS="-dologs -adminlog -netlog -freezecheck -limitFPS=60" \
    DAYZ_CPU_COUNT=4 \
    LD_LIBRARY_PATH=/dayz:/dayz/bin

EXPOSE 2302-2306/udp 27016/udp 2310/udp

ENTRYPOINT ["/entrypoint.sh"]
