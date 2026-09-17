import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { DragonwildsAdapter } from '../dragonwilds/adapter.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { MockPlugin, MOCK_PUID } from '../testing/mockPlugin.js';

let mock: MockPlugin;
const make = (opts: { senderName?: string; serverName?: string }) =>
  new DragonwildsAdapter(new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }), opts);

beforeEach(async () => {
  mock = new MockPlugin();
  await mock.start();
});
afterEach(async () => mock.stop());

describe('chat sender name precedence', () => {
  it('opts.senderNameOverride > TAKARO_SENDER_NAME > TAKARO_SERVER_NAME > "Server"', () => {
    expect(make({ senderName: 'Sender', serverName: 'ServerName' }).senderName('Override')).toBe('Override');
    expect(make({ senderName: 'Sender', serverName: 'ServerName' }).senderName(null)).toBe('Sender');
    expect(make({ serverName: 'ServerName' }).senderName(null)).toBe('ServerName');
    expect(make({}).senderName(null)).toBe('Server');
    expect(make({ senderName: '', serverName: '' }).senderName('')).toBe('Server');
  });

  it('sendMessage passes the resolved sender to the plugin, message text untouched', async () => {
    const adapter = make({ senderName: 'Takaro', serverName: 'Takaro Dev Dragonwilds' });
    await adapter.handleAction('sendMessage', { message: 'server restarting' });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'server restarting', senderName: 'Takaro' });

    await adapter.handleAction('sendMessage', { message: 'psst', opts: { senderNameOverride: 'Shop', recipient: { gameId: MOCK_PUID } } });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'psst', recipientGameId: MOCK_PUID, senderName: 'Shop' });
  });

  it('falls back to the server name when only TAKARO_SERVER_NAME is set', async () => {
    const adapter = make({ serverName: 'Takaro Dev Dragonwilds' });
    await adapter.handleAction('sendMessage', { message: 'hi' });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'hi', senderName: 'Takaro Dev Dragonwilds' });
  });
});
