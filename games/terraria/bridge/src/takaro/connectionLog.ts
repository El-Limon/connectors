import { logger } from '../logger.js';

/**
 * The two lines that say what the Takaro websocket is doing.
 *
 * They are not decoration. `takaro-maint verify` reads this bridge's log for
 * "Identified successfully" to prove the handshake, and counts it a second time to prove
 * a reconnect, so the leading text of both lines is a contract with the harness and not
 * something to reword. Everything else the bridge logs is about one request.
 */
export const IDENTIFIED_PREFIX = 'Identified successfully with Takaro';
export const DISCONNECTED_LINE = 'Takaro connection closed; reconnecting';

export const identifiedLine = (gameServerId?: string): string =>
  `${IDENTIFIED_PREFIX} (gameServerId=${gameServerId ?? '<unknown>'})`;

/** Anything that reports the connection state the way the Takaro client does. */
export interface ConnectionEvents {
  on(event: 'identified', listener: (gameServerId?: string) => void): unknown;
  on(event: 'disconnected', listener: () => void): unknown;
}

/** Wires the two lines onto a live client. Added beside the bridge's own handlers. */
export function logConnectionState(takaro: ConnectionEvents, log = logger): void {
  takaro.on('identified', (gameServerId?: string) => log.info(identifiedLine(gameServerId)));
  takaro.on('disconnected', () => log.warn(DISCONNECTED_LINE));
}
