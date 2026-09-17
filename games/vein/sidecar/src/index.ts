import { Bridge } from './bridge.js';
import { loadConfig } from './config.js';
import { FileBanStore } from './vein/banStore.js';
import { FileCursorStore } from './vein/cursorStore.js';
import { FileKnownPlayerStore } from './vein/knownStore.js';
import { FileOnlineStore } from './vein/onlineStore.js';
import { VeinHttpApi } from './vein/httpApi.js';
import { buildGrammar } from './vein/logTail.js';
import { VeinPluginClient } from './vein/pluginClient.js';
import { HealthServer } from './healthServer.js';
import { logger } from './logger.js';
import { TakaroWsClient } from './takaro/client.js';
import type { WsMessage } from './takaro/protocol.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const plugin = new VeinPluginClient({
    baseUrl: config.pluginBaseUrl,
    token: config.pluginToken,
    timeoutMs: config.pluginTimeoutMs,
  });
  const gameApi = new VeinHttpApi({ baseUrl: config.httpApiUrl });
  const takaro = new TakaroWsClient(
    config.takaroWsUrl,
    { identityToken: config.identityToken, registrationToken: config.registrationToken, serverName: config.serverName },
    { baseReconnectMs: config.reconnectBaseMs, maxReconnectMs: config.reconnectMaxMs },
  );
  const bridge = new Bridge({
    plugin,
    takaro,
    cursorStore: new FileCursorStore(config.cursorFile),
    onlineStore: new FileOnlineStore(config.onlineFile),
    logFile: config.logFile,
    logGrammar: buildGrammar(config.logGrammarOverrides),
    logTailMode: config.logTailMode,
    logEvents: config.logEvents,
    // 45 s: short enough that a game-container restart heals itself well inside a hard-test cell, long enough to
    // ride out a normal server restart's plugin gap without flapping (F17).
    exitAfterUnreachableMs: Number.parseInt(process.env.SIDECAR_EXIT_AFTER_UNREACHABLE_MS ?? '45000', 10) || 0,
    exitAfterPluginLossMs: Number.parseInt(process.env.SIDECAR_EXIT_AFTER_PLUGIN_LOSS_MS ?? '180000', 10) || 0,
    gameReachable: async () => (await gameApi.status()).reachable,
    pollIntervalMs: config.pollIntervalMs,
    adapter: {
      banStore: new FileBanStore(config.banFile),
      knownStore: new FileKnownPlayerStore(config.knownPlayersFile),
      senderName: config.senderName,
      serverName: config.serverName,
      httpPlayers: () => gameApi.getPlayers(),
    },
  });
  const health = new HealthServer(config.healthPort, config.healthHost, async () => ({
    ok: true,
    takaroIdentified: takaro.identified(),
    gameServerId: takaro.getGameServerId(),
    pluginHealth: bridge.pluginHealth(),
    logTailActive: bridge.isLogTailActive(),
    eventCursor: bridge.poller.cursor(),
    eventScanCursor: bridge.poller.scanCursor(),
    pendingEvents: bridge.pending().length,
    unconfirmedEvents: bridge.unconfirmed().length,
    lastConfirmedSendId: takaro.lastConfirmedId(),
    droppedEvents: bridge.dropped(),
    pendingTimedBans: bridge.adapter.pendingBans().length,
    serverReady: bridge.tailer.serverReady(),
    gameHttpApi: await gameApi.status(),
  }));

  takaro.on('request', (message: WsMessage) => void bridge.handleRequest(message));
  takaro.on('identified', () => void bridge.startEvents());
  takaro.on('disconnected', () => bridge.stopEvents());

  await health.start();
  logger.info(`Sidecar health on http://${config.healthHost}:${config.healthPort}/health; plugin ${config.pluginBaseUrl}`);
  bridge.startHealthWatch();
  takaro.connect();

  const stop = async (): Promise<void> => {
    logger.info('Shutting down Vein Takaro sidecar');
    bridge.stopEvents();
    bridge.stopHealthWatch();
    takaro.shutdown();
    await health.stop();
    setTimeout(() => process.exit(0), 100);
  };
  process.on('SIGINT', () => void stop());
  process.on('SIGTERM', () => void stop());
}

main().catch((err) => {
  logger.error(`Fatal startup error: ${err instanceof Error ? err.stack || err.message : String(err)}`);
  process.exit(1);
});
