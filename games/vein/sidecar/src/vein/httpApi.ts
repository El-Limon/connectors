import { asRecord } from '../takaro/protocol.js';
import { str } from './mapping.js';
import type { FetchImpl } from './pluginClient.js';
import type { PluginPlayer } from './types.js';

export interface HttpApiStatus {
  url: string;
  reachable: boolean;
  players: number | null;
  error?: string;
}

/**
 * Vein's OWN built-in read-only HTTP API (`Game.ini [/Script/Vein.VeinGameSession] HTTPPort`, default 8080).
 * It is a cross-check for `/health` and a players fallback while our plugin is unreachable — never the source of
 * truth for identity or actions. Its exact JSON shape is UNVERIFIED, so parsing is deliberately tolerant
 * (array, `{players:[...]}`, `{data:[...]}`; id from steamId/steamID64/id/uniqueId; name from name/playerName/persona).
 */
export class VeinHttpApi {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: FetchImpl;

  constructor(options: { baseUrl: string; timeoutMs?: number; fetchImpl?: FetchImpl }) {
    this.baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
    this.timeoutMs = options.timeoutMs ?? 3000;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  enabled(): boolean {
    return Boolean(this.baseUrl);
  }

  /** Online players as the game itself reports them, or null when the API is disabled/unreachable/unparseable. */
  async getPlayers(): Promise<PluginPlayer[] | null> {
    if (!this.baseUrl) return null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(`${this.baseUrl}/players`, { signal: controller.signal });
      if (!res.ok) return null;
      return parsePlayers(await res.text());
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Shape for the sidecar `/health` body; absence of the API is reported, never fatal. */
  async status(): Promise<HttpApiStatus> {
    if (!this.baseUrl) return { url: '', reachable: false, players: null, error: 'disabled' };
    const players = await this.getPlayers();
    return players === null
      ? { url: this.baseUrl, reachable: false, players: null, error: 'unreachable or unparseable' }
      : { url: this.baseUrl, reachable: true, players: players.length };
  }
}

export function parsePlayers(raw: string): PluginPlayer[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  const rec = asRecord(parsed);
  const list = Array.isArray(parsed) ? parsed : ['players', 'Players', 'data'].map((k) => rec[k]).find(Array.isArray);
  if (!Array.isArray(list)) return null;
  const out: PluginPlayer[] = [];
  for (const entry of list) {
    const p = asRecord(entry);
    const gameId = str(p.steamId) ?? str(p.steamID) ?? str(p.steamID64) ?? str(p.SteamId) ?? str(p.uniqueId) ?? str(p.id) ?? str(p.gameId);
    if (!gameId) continue;
    const name = str(p.name) ?? str(p.playerName) ?? str(p.Name) ?? str(p.persona);
    const ping = typeof p.ping === 'number' ? p.ping : undefined;
    out.push({ gameId, ...(name ? { name } : {}), steamId: gameId, platformId: `steam:${gameId}`, ...(ping !== undefined ? { ping } : {}), online: true });
  }
  return out;
}
