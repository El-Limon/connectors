import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import {
  RCON_AUTH_RESPONSE,
  RCON_EXEC_COMMAND,
  decodePacket,
  encodePacket,
  sendRconCommand,
} from '../rcon/client.js';
import { startFakeRconServer } from './helpers/fakeRcon.js';

const servers: { close(): Promise<void> }[] = [];

after(async () => {
  await Promise.all(servers.map((server) => server.close()));
});

async function fakeRcon(...args: Parameters<typeof startFakeRconServer>) {
  const server = await startFakeRconServer(...args);
  servers.push(server);
  return server;
}

test('encodes and decodes RCON packets', () => {
  const encoded = encodePacket({ id: 42, type: RCON_EXEC_COMMAND, body: 'listplayers' });
  const decoded = decodePacket(encoded);

  assert.equal(decoded.bytesRead, encoded.length);
  assert.deepEqual(decoded.packet, {
    id: 42,
    type: RCON_EXEC_COMMAND,
    body: 'listplayers',
  });
});

test('returns null packet when buffer is incomplete', () => {
  const encoded = encodePacket({ id: 1, type: RCON_EXEC_COMMAND, body: 'help' });

  assert.equal(decodePacket(encoded.subarray(0, 6)).packet, null);
});

test('authenticates and executes a command against an RCON server', async () => {
  const server = await fakeRcon('secret', {
    listplayers: '0. Alice | 76561198000000001',
  });

  const response = await sendRconCommand({
    host: '127.0.0.1',
    port: server.port,
    password: 'secret',
    command: 'listplayers',
    timeoutMs: 1000,
  });

  assert.equal(response, '0. Alice | 76561198000000001');
});

test('accepts Conan-style auth response type before executing a command', async () => {
  const server = await fakeRcon('secret', {
    help: 'Commands: listplayers',
  }, RCON_AUTH_RESPONSE, 0, 'auth');

  const response = await sendRconCommand({
    host: '127.0.0.1',
    port: server.port,
    password: 'secret',
    command: 'help',
    timeoutMs: 1000,
  });

  assert.equal(response, 'Commands: listplayers');
});

test('rejects invalid RCON credentials', async () => {
  const server = await fakeRcon('secret', {});

  await assert.rejects(
    () =>
      sendRconCommand({
        host: '127.0.0.1',
        port: server.port,
        password: 'wrong',
        command: 'help',
        timeoutMs: 1000,
      }),
    /RCON authentication failed/,
  );
});
