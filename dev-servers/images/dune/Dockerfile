# Dune: Awakening self-hosted map server (Funcom "seabass-server") for the Takaro dev rig.
#
# The base image is NOT pulled from a registry: it is `docker load`ed out of the Steam
# tool package (app 4754530, linux depot 4754532) by dev-servers/scripts/install.sh.
# Nothing here downloads game files — the 10.8 GB image already contains them.
#
# What this layer adds:
#   * our own entrypoint, replacing Funcom's k8s-shaped /home/dune/run.sh (which asks the
#     Kubernetes API for its node's ExternalIP, starts sshd and PATCHes pod annotations)
#   * the operator-synthesised command line (-MultiHome/-ExternalAddress/-Port/-IGWPort/
#     -PartitionIndex) that nothing supplies outside k3s
#   * the UserEngine.ini / UserGame.ini overrides that turn the GM command seam on
#   * opt-in LD_PRELOAD of the Takaro plugin on the game binary ONLY
ARG DUNE_SERVER_IMAGE
FROM ${DUNE_SERVER_IMAGE}

USER root

COPY entrypoint.sh /usr/local/bin/takaro-entrypoint.sh
COPY healthcheck.sh /usr/local/bin/takaro-healthcheck.sh
RUN chmod 755 /usr/local/bin/takaro-entrypoint.sh /usr/local/bin/takaro-healthcheck.sh \
 && bash -n /usr/local/bin/takaro-entrypoint.sh \
 && bash -n /usr/local/bin/takaro-healthcheck.sh

ENV DUNE_SERVER_ROOT=/home/dune/server \
    DUNE_GAME_PORT=7777 \
    DUNE_IGW_PORT=7888 \
    DUNE_PARTITION_INDEX=1 \
    DUNE_FARM_REGION=Europe \
    DUNE_RMQ_GAME_HOST=game-rmq \
    DUNE_RMQ_GAME_PORT=5672 \
    DUNE_RMQ_ADMIN_HOST=admin-rmq \
    DUNE_RMQ_ADMIN_PORT=5672 \
    DUNE_RMQ_TLS_ENABLED=true \
    DUNE_EXTRA_GM_COMMANDS=KickPlayer,ServiceBroadcast,AwardXP,SpawnVehicleAt \
    DUNE_INI_VIA_ARGS=0 \
    TAKARO_PLUGIN_SO=""

WORKDIR /home/dune/server
ENTRYPOINT ["/usr/local/bin/takaro-entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=15m \
    CMD /usr/local/bin/takaro-healthcheck.sh
