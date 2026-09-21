# VEIN dedicated server (Steam app 2131400, Linux native) for the Takaro dev rig.
#
# Base is cm2network/steamcmd:root pinned by digest: the entrypoint needs root to
# fix ownership of the bind-mounted data dir, then drops to the `steam` user (uid
# 1000) before exec'ing the game.
#
# LD_PRELOAD discipline: the Takaro plugin is preloaded ONLY on the final `exec`
# of the game binary, never around SteamCMD (which runs 32-bit helpers and would
# spew "wrong ELF class" for our 64-bit .so).
FROM cm2network/steamcmd@sha256:e6b6b3503bf0e41feafe12dc709c90151afba193e1292cac55d28a7d470b1493

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3-minimal ca-certificates procps curl; \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY render-config.py /usr/local/bin/render-config.py
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/render-config.py \
 && bash -n /usr/local/bin/entrypoint.sh \
 && python3 -c "compile(open('/usr/local/bin/render-config.py').read(),'x','exec')"

ENV HOME=/home/steam \
    VEIN_APPID=2131400 \
    VEIN_DIR=/home/steam/vein \
    VEIN_AUTO_UPDATE=false \
    VEIN_PORT=7777 \
    VEIN_QUERY_PORT=27015 \
    VEIN_HTTP_PORT=8080 \
    VEIN_SERVER_NAME="Takaro Dev Vein" \
    VEIN_SERVER_DESCRIPTION="Takaro connector development rig" \
    VEIN_MAXPLAYERS=8 \
    VEIN_PUBLIC=true \
    VEIN_SERVER_PASSWORD="" \
    VEIN_ADMIN_STEAMIDS="" \
    VEIN_EXTRA_ARGS="" \
    TAKARO_PLUGIN_SO=""

WORKDIR /home/steam/vein
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
