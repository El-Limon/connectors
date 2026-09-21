import http, { type IncomingMessage, type ServerResponse } from 'node:http';
import type { PluginEvent, PluginHealth, PluginInventoryItem, PluginPlayer, Position } from '../vein/types.js';

export interface MockRequest {
  method: string;
  path: string;
  query: Record<string, string>;
  body?: any;
  auth?: string;
}

/** Vein identity is the SteamID64: gameId === steamId, platformId `steam:<id64>`. */
export const MOCK_STEAMID = '76561198000000001';
export const MOCK_STEAMID_2 = '76561198000000002';

/** In-memory implementation of the Vein plugin HTTP contract, for tests and local dry runs. */
export class MockPlugin {
  token = 'test-token';
  health: PluginHealth = {
    status: 'ok',
    version: '0.1.0-mock',
    gameBuild: 'Vein-Win64-Test buildid 25034670',
    engineVersion: '5.6.1',
    capabilities: { players: 'ok', gameThread: 'ok', chatEvents: 'ok', logEvents: 'ok', teleport: 'ok', entities: 'unimplemented' },
  };
  players: PluginPlayer[] = [
    {
      gameId: MOCK_STEAMID,
      name: 'Tester',
      characterName: 'Tester',
      steamId: MOCK_STEAMID,
      platformId: `steam:${MOCK_STEAMID}`,
      ip: '10.0.0.5',
      ping: 24,
      spawned: true,
      online: true,
      position: { x: 10.5, y: 20, z: -3 },
    },
    {
      gameId: MOCK_STEAMID_2,
      name: 'Guest',
      characterName: 'Guest',
      steamId: MOCK_STEAMID_2,
      platformId: `steam:${MOCK_STEAMID_2}`,
      ping: 40,
      spawned: false,
      online: true,
    },
  ];
  locations: Record<string, Position> = { [MOCK_STEAMID]: { x: 10.5, y: 20, z: -3 }, [MOCK_STEAMID_2]: { x: 1, y: 2, z: 3 } };
  inventories: Record<string, PluginInventoryItem[]> = {
    [MOCK_STEAMID]: [
      { code: 'Item_Wood_Plank', name: 'Wooden Plank', amount: 25 },
      { code: 'Item_Weapon_Pistol', name: 'Pistol', amount: 1 },
    ],
  };
  items = [
    { code: 'Item_Wood_Plank', name: 'Wooden Plank', description: 'Basic material', category: 'Resource' },
    { code: 'Item_Weapon_Pistol', name: 'Pistol', category: 'Weapon' },
  ];
  entities = [
    { code: 'BP_Zombie_Base', name: 'Zombie', type: 'hostile' },
    { code: 'BP_Deer', name: 'Deer', type: 'animal' },
    { code: 'BP_Trader', name: 'Trader', type: 'npc' },
  ];
  locationsList = [{ code: 'vein_town_square', name: 'Town Square', position: { x: 0, y: 100, z: 0 }, radius: 50 }];
  bans: Array<{ gameId: string; steamId?: string; name?: string; characterName?: string; reason?: string; expiresAt?: string | null }> = [];
  events: PluginEvent[] = [];
  seqBase = 0;
  bootId: string | undefined = undefined;
  truncated = false;
  unimplemented = new Set<string>();
  requests: MockRequest[] = [];
  commandHandler: (command: string) => { success: boolean; output: string } = (command) => ({ success: true, output: `ran ${command}` });
  /** When set, POST /command answers this HTTP status instead (the plugin rejects an unknown command with 400). */
  commandStatus: number | null = null;

  private server: http.Server | null = null;

  pushEvent(type: string, data: unknown): PluginEvent {
    const last = this.events.length ? this.events[this.events.length - 1].seq : this.seqBase;
    const event = { seq: last + 1, type, data, ts: new Date().toISOString() };
    this.events.push(event);
    return event;
  }

  url(): string {
    const addr = this.server?.address();
    if (!addr || typeof addr !== 'object') throw new Error('mock plugin not listening');
    return `http://127.0.0.1:${addr.port}`;
  }

  lastRequest(method: string, path: string): MockRequest | undefined {
    return [...this.requests].reverse().find((r) => r.method === method && r.path === path);
  }

  start(port = 0): Promise<string> {
    this.server = http.createServer((req, res) => void this.route(req, res));
    return new Promise((resolve) => this.server!.listen(port, '127.0.0.1', () => resolve(this.url())));
  }

  stop(): Promise<void> {
    const s = this.server;
    this.server = null;
    return new Promise((resolve) => (s ? s.close(() => resolve()) : resolve()));
  }

  private find(id: unknown): PluginPlayer | undefined {
    const needle = String(id ?? '').toLowerCase();
    return this.players.find(
      (p) =>
        p.gameId.toLowerCase() === needle ||
        p.steamId?.toLowerCase() === needle ||
        p.platformId?.toLowerCase() === needle,
    );
  }

  private async route(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? '/', 'http://x');
    const path = url.pathname;
    const method = req.method ?? 'GET';
    const body = method === 'POST' ? await readBody(req) : undefined;
    const record: MockRequest = { method, path, query: Object.fromEntries(url.searchParams), body, auth: req.headers.authorization };
    this.requests.push(record);

    if (req.headers.authorization !== `Bearer ${this.token}`) return json(res, 401, { error: 'unauthorized' });

    const routeKey = `${method} ${path.replace(/^\/players\/[^/]+/, '/players/:id')}`;
    if (this.unimplemented.has(routeKey)) return json(res, 501, { error: 'not implemented in this build' });

    const playerMatch = /^\/players\/([^/]+)(\/location|\/inventory)?$/.exec(path);
    if (method === 'GET' && playerMatch) {
      const player = this.find(decodeURIComponent(playerMatch[1]));
      if (!player) return json(res, 404, { error: 'player not found' });
      if (playerMatch[2] === '/location') {
        const pos = this.locations[player.gameId] ?? player.position;
        return pos ? json(res, 200, pos) : json(res, 503, { error: 'player has no pawn' });
      }
      if (playerMatch[2] === '/inventory') return json(res, 200, this.inventories[player.gameId] ?? []);
      return json(res, 200, player);
    }

    switch (`${method} ${path}`) {
      case 'GET /health':
        return json(res, 200, this.health);
      case 'GET /players':
        return json(res, 200, this.players);
      case 'GET /events': {
        const since = Number(url.searchParams.get('since') ?? 0);
        const limit = Number(url.searchParams.get('limit') ?? 0) || undefined;
        const seq = this.events.length ? this.events[this.events.length - 1].seq : this.seqBase;
        let events = this.events.filter((e) => e.seq > since);
        if (limit) events = events.slice(0, limit);
        return json(res, 200, { ...(this.bootId ? { bootId: this.bootId } : {}), ...(this.truncated ? { truncated: true } : {}), seq, events });
      }
      case 'POST /message':
        if (!body?.text) return json(res, 400, { error: 'text required' });
        if (body.recipientGameId && !this.find(body.recipientGameId)) return json(res, 404, { error: 'player not online' });
        return json(res, 200, { ok: true });
      case 'POST /teleport':
      case 'POST /give':
      case 'POST /kick': {
        const player = this.find(body?.gameId);
        if (!player) return json(res, 404, { error: 'player not online' });
        if (path === '/teleport') this.locations[player.gameId] = { x: body.x, y: body.y, z: body.z };
        if (path === '/kick') player.online = false;
        return json(res, 200, { ok: true });
      }
      case 'POST /ban': {
        const player = this.find(body?.gameId);
        this.bans.push({
          gameId: player?.gameId ?? body.gameId,
          steamId: player?.steamId,
          name: player?.name ?? body.gameId,
          characterName: player?.characterName,
          reason: body.reason,
          expiresAt: null,
        });
        return json(res, 200, { ok: true });
      }
      case 'POST /unban':
        this.bans = this.bans.filter((b) => b.gameId !== body?.gameId && b.steamId !== body?.gameId);
        return json(res, 200, { ok: true });
      case 'GET /bans':
        return json(res, 200, this.bans);
      case 'GET /items': {
        const search = (url.searchParams.get('search') ?? '').toLowerCase();
        return json(res, 200, search ? this.items.filter((i) => `${i.code} ${i.name}`.toLowerCase().includes(search)) : this.items);
      }
      case 'GET /entities':
        return json(res, 200, this.entities);
      case 'GET /locations':
        return json(res, 200, this.locationsList);
      case 'POST /command':
        if (this.commandStatus) return json(res, this.commandStatus, { error: `unknown command '${String(body?.command ?? '')}'` });
        return json(res, 200, this.commandHandler(String(body?.command ?? '')));
      case 'POST /shutdown':
        return json(res, 200, { ok: true });
      default:
        return json(res, 404, { error: `no route ${method} ${path}` });
    }
  }
}

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

async function readBody(req: IncomingMessage): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  const raw = Buffer.concat(chunks).toString('utf8');
  return raw ? JSON.parse(raw) : {};
}
