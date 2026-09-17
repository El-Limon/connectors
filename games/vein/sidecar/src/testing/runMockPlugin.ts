import { MockPlugin } from './mockPlugin.js';

const mock = new MockPlugin();
mock.token = process.env.TAKARO_PLUGIN_TOKEN || 'dev-token';
const port = Number(process.env.MOCK_PLUGIN_PORT || 18890);
await mock.start(port);
console.log(`Mock Vein plugin on ${mock.url()} (token ${mock.token})`);
mock.pushEvent('chat-message', { player: mock.players[0], msg: 'mock boot', channel: 'global' });
setInterval(() => mock.pushEvent('log', { msg: `LogVein: mock heartbeat ${new Date().toISOString()}` }), 30000);
