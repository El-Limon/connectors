import { Bridge } from './bridge.js';
import { loadConfig } from './config.js';
import { FileBanStore } from './dragonwilds/banStore.js';
import { FileCursorStore } from './dragonwilds/cursorStore.js';
import { FileKnownPlayerStore } from './dragonwilds/knownStore.js';
import { FileOnlineStore } from './dragonwilds/onlineStore.js';
import { DragonwildsPluginClient } from './dragonwilds/pluginClient.js';
import { HealthServer } from './healthServer.js';
import { logger } from './logger.js';
import { TakaroWsClient } from './takaro/client.js';
import type { WsMessage } from './takaro/protocol.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const plugin = new DragonwildsPluginClient({
    baseUrl: config.pluginBaseUrl,
    token: config.pluginToken,
    timeoutMs: config.pluginTimeoutMs,
  });
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
    logTailMode: config.logTailMode,
    logEvents: config.logEvents,
    exitAfterUnreachableMs: Number.parseInt(process.env.SIDECAR_EXIT_AFTER_UNREACHABLE_MS ?? '180000', 10) || 0,
    pollIntervalMs: config.pollIntervalMs,
    adapter: {
      banStore: new FileBanStore(config.banFile),
      knownStore: new FileKnownPlayerStore(config.knownPlayersFile),
      senderName: config.senderName,
      serverName: config.serverName,
    },
  });
  const health = new HealthServer(config.healthPort, config.healthHost, () => ({
    ok: true,
    takaroIdentified: takaro.identified(),
    gameServerId: takaro.getGameServerId(),
    pluginHealth: bridge.pluginHealth(),
    logTailActive: bridge.isLogTailActive(),
    eventCursor: bridge.poller.cursor(),
    eventScanCursor: bridge.poller.scanCursor(),
    pendingEvents: bridge.pending().length,
    droppedEvents: bridge.dropped(),
    pendingTimedBans: bridge.adapter.pendingBans().length,
  }));

  takaro.on('request', (message: WsMessage) => void bridge.handleRequest(message));
  takaro.on('identified', () => void bridge.startEvents());
  takaro.on('disconnected', () => bridge.stopEvents());

  await health.start();
  logger.info(`Sidecar health on http://${config.healthHost}:${config.healthPort}/health; plugin ${config.pluginBaseUrl}`);
  takaro.connect();

  const stop = async (): Promise<void> => {
    logger.info('Shutting down Dragonwilds Takaro sidecar');
    bridge.stopEvents();
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
