import { startBridge } from './app.js';
import { loadConfig } from './config.js';
import { logger } from './logger.js';
import { readTargetStamp } from './targetStamp.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const stamp = readTargetStamp();
  const bridge = await startBridge(config, { stamp });

  const stop = async (): Promise<void> => {
    await bridge.stop();
    setTimeout(() => process.exit(0), 100);
  };

  process.on('SIGINT', () => void stop());
  process.on('SIGTERM', () => void stop());
  process.on('uncaughtException', (err) => {
    logger.error(`Uncaught exception: ${err.stack || err.message}`);
  });
  process.on('unhandledRejection', (reason) => {
    logger.error(`Unhandled rejection: ${String(reason)}`);
  });
}

main().catch((err) => {
  logger.error(`Fatal startup error: ${err.stack || err.message}`);
  process.exit(1);
});
