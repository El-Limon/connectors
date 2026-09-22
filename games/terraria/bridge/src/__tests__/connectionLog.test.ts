import assert from 'node:assert/strict';
import { test } from 'node:test';
import { WebSocketServer, WebSocket } from 'ws';
import { TakaroWsClient } from '../takaro/client.js';
import type { WsMessage } from '../takaro/protocol.js';
import { DISCONNECTED_LINE, IDENTIFIED_PREFIX, logConnectionState } from '../takaro/connectionLog.js';

const waitFor = async (predicate: () => boolean, timeoutMs = 5000): Promise<void> => {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('timed out');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
};

// The line is a contract with `takaro-maint verify`, which greps this bridge's log for it
// to prove the handshake and counts it again to prove a reconnect. So the assertion is
// driven by a real client against a real socket, not by calling the formatter.
test('the bridge logs one identify line per Takaro session and one line when the socket drops', async () => {
  const server = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise<void>((resolve) => server.once('listening', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('server did not bind');

  server.on('connection', (socket) => {
    socket.on('message', (raw) => {
      const message = JSON.parse(raw.toString()) as WsMessage;
      if (message.type === 'identify') {
        socket.send(JSON.stringify({ type: 'identifyResponse', payload: { gameServerId: 'gs_terraria' } }));
      }
    });
  });

  const info: string[] = [];
  const warn: string[] = [];
  const client = new TakaroWsClient(
    `ws://127.0.0.1:${address.port}`,
    { identityToken: 'identity', registrationToken: 'registration', name: 'Terraria Test' },
    50,
  );
  logConnectionState(client, {
    info: (line: string) => info.push(line),
    warn: (line: string) => warn.push(line),
    error: () => undefined,
  });

  try {
    client.connect();
    await waitFor(() => info.length === 1);
    assert.equal(info[0], `${IDENTIFIED_PREFIX} (gameServerId=gs_terraria)`);

    // Takaro going away, and the bridge coming back: the second identify line is what the
    // reconnect check counts.
    for (const socket of server.clients) socket.close(1001, 'going away');
    await waitFor(() => warn.length === 1);
    assert.equal(warn[0], DISCONNECTED_LINE);
    await waitFor(() => info.length === 2);
    assert.match(info[1], new RegExp(`^${IDENTIFIED_PREFIX}`));
  } finally {
    client.shutdown();
    for (const socket of server.clients) {
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CLOSING) socket.terminate();
    }
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
