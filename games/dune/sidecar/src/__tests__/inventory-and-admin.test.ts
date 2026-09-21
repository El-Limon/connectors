import { readFileSync } from 'node:fs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { Catalogue, NON_CARRYABLE_CODE_RE, isGiveable } from '../dune/catalogue.js';
import { REPORTED_INVENTORY_TYPES, inventoryTypesOf, loadConfig } from '../dune/config.js';
import { weaponFromCode } from '../dune/mapping.js';
import { HealthServer, type AdminRoute } from '../healthServer.js';
import { DunePluginClient } from '../dune/pluginClient.js';
import { harness } from './helpers.js';

const tmpFiles: string[] = [];
afterEach(() => {
  for (const f of tmpFiles.splice(0)) fs.rmSync(f, { force: true });
});

function catalogueFile(rows: unknown[]): string {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'dune-cat-')), 'items.json');
  fs.writeFileSync(file, JSON.stringify({ source: 'test', rows }));
  tmpFiles.push(file);
  return file;
}

/**
 * Tester's inventory screen in Takaro showed ten `Emote_*` rows as things his character "has". They are unlockable
 * animations the game happens to store in `inventories`; they cannot be dropped, traded or meaningfully granted.
 */
describe('only physical carry inventories are reported', () => {
  it('the reported inventory types are the three carry containers, and they are configurable', () => {
    expect(REPORTED_INVENTORY_TYPES).toEqual([0, 1, 15]);
    expect(inventoryTypesOf({})).toEqual([0, 1, 15]);
    // 14 and 27 are emotes, 29 is a contract item: an operator can opt in, but never by accident.
    expect(inventoryTypesOf({ DUNE_INVENTORY_TYPES: '0,1,15,14,27' })).toEqual([0, 1, 15, 14, 27]);
    expect(() => inventoryTypesOf({ DUNE_INVENTORY_TYPES: 'emotes' })).toThrow(/no valid integers/);
  });

  it('an Emote_ code is refused by code as well, whatever inventory type it arrives in', () => {
    expect(NON_CARRYABLE_CODE_RE.test('Emote_Bow_01')).toBe(true);
    expect(NON_CARRYABLE_CODE_RE.test('emote_sit_01')).toBe(true);
    // Not over-eager: a real item whose name merely contains the word must survive.
    expect(NON_CARRYABLE_CODE_RE.test('EmoteStoneCarving')).toBe(false);
    expect(NON_CARRYABLE_CODE_RE.test('ScrapMetalKnife')).toBe(false);
  });
});

describe('the catalogue keeps non-giveable rows resolvable but out of listItems', () => {
  it('emotes and cosmetics resolve to a human name yet never reach the item picker', () => {
    const file = catalogueFile([
      { code: 'ScrapMetal', name: 'Scrap Metal' },
      { code: 'Emote_Bow_01', name: 'Emote: Bow', category: 'emote', giveable: false },
      { code: 'Social_Choam_MaulaCastOffs01_Gloves', name: 'Maula Cast-Offs Gloves', category: 'cosmetic', giveable: false },
      { code: 'SolarisCoin', name: 'Solari', category: 'currency', giveable: true },
    ]);
    const catalogue = new Catalogue({ itemsFile: file, entitiesFile: '' });
    catalogue.load();

    expect(catalogue.listItems().map((i) => i.code).sort()).toEqual(['ScrapMetal', 'SolarisCoin']);
    // Still resolvable, so an inventory or a log line shows the human name rather than the asset id.
    expect(catalogue.displayName('Emote_Bow_01')).toBe('Emote: Bow');
    expect(catalogue.displayName('Social_Choam_MaulaCastOffs01_Gloves')).toBe('Maula Cast-Offs Gloves');
    expect(catalogue.displayName('SolarisCoin')).toBe('Solari');
    expect(catalogue.status()).toMatchObject({ items: 2, itemsNotGiveable: 2, itemsResolvable: 4 });
  });

  it('an Emote_ row is withheld even when the file forgot to flag it', () => {
    const file = catalogueFile([{ code: 'Emote_Yes_01', name: 'Emote: Yes' }]);
    const catalogue = new Catalogue({ itemsFile: file, entitiesFile: '' });
    catalogue.load();
    expect(catalogue.listItems()).toEqual([]);
    expect(catalogue.displayName('Emote_Yes_01')).toBe('Emote: Yes');
  });

  it('the giveable rule covers every non-giveable category', () => {
    expect(isGiveable({ code: 'X' })).toBe(true);
    expect(isGiveable({ code: 'X', giveable: false })).toBe(false);
    for (const category of ['emote', 'Contract', 'QUEST', 'cosmetic']) {
      expect(isGiveable({ code: 'X', category })).toBe(false);
    }
    expect(isGiveable({ code: 'X', category: 'weapon' })).toBe(true);
  });

  // Only where the FULL catalogue has been generated (`npm run catalogue`): the published tree
  // ships a placeholder of our own rows, which by design does not name the wiki-sourced codes.
  const full = (() => {
    try {
      const f = path.resolve(import.meta.dirname, '..', '..', 'data', 'items.json');
      return JSON.parse(readFileSync(f, 'utf8')).placeholder !== true;
    } catch {
      return false;
    }
  })();
  it.runIf(full)('the shipped catalogue names every code seen in the live database', () => {
    const catalogue = new Catalogue({
      itemsFile: path.resolve(import.meta.dirname, '..', '..', 'data', 'items.json'),
      entitiesFile: '',
    });
    catalogue.load();
    // Every template_id observed on TakaroTest's pawn, 2026-09-21. `sftnj`/`wy1ll` are wiki-generated codes for a real
    // item and a contract item; they resolve, which is what matters for display.
    const live = [
      'Ammo', 'AzuriteOre', 'Crysknife', 'Emote_Bow_01', 'Emote_Clap_01', 'Emote_Follow_01', 'Emote_IxianSecret_01',
      'Emote_No_01', 'Emote_Point_01', 'Emote_ShakeOffSand_01', 'Emote_Sit_01', 'Emote_Threaten_01', 'Emote_Yes_01',
      'Literjon', 'MiningTool_1h_Standard', 'Oil', 'PowerPack', 'ScrapMetal', 'ScrapMetalKnife',
      'Social_Choam_MaulaCastOffs01_Bottom', 'Social_Choam_MaulaCastOffs01_Gloves', 'Social_Choam_MaulaCastOffs01_Shoes',
      'Social_Choam_MaulaCastOffs01_Top_Fremkit', 'SolarisCoin', 'Stone',
    ];
    // Every code resolves…
    expect(live.filter((code) => !catalogue.displayName(code))).toEqual([]);
    // …and none of the ones that used to read as an asset id still does. `Literjon` legitimately IS its own display
    // name (a single plain English word), so `name === code` is only a defect for the asset-shaped codes.
    const stillDevNamed = live.filter((code) => /_|[a-z][A-Z]/.test(code) && catalogue.displayName(code) === code);
    expect(stillDevNamed).toEqual([]);
    // And not one of them is offered as giveable if it is an emote.
    expect(catalogue.listItems().filter((i) => NON_CARRYABLE_CODE_RE.test(i.code))).toEqual([]);
  });
});

describe('the plugin’s weaponCode bug does not reach Takaro', () => {
  it('BlueprintGeneratedClass names no weapon', () => {
    expect(weaponFromCode('BlueprintGeneratedClass')).toBeNull();
  });

  it('a dev/asset id is refused too, and a plain name is kept', () => {
    expect(weaponFromCode('BP_Dart_C')).toBeNull();
    expect(weaponFromCode('SK_Kindjal')).toBeNull();
    expect(weaponFromCode('MiningTool_1h_Standard')).toBeNull();
    expect(weaponFromCode('DuneCritterBase')).toBeNull();
    expect(weaponFromCode('Crysknife')).toBe('Crysknife');
    expect(weaponFromCode('')).toBeNull();
    expect(weaponFromCode(null)).toBeNull();
  });
});

describe('DUNE_GLOBAL_MESSAGE_MODE defaults to both', () => {
  it('because only the ServiceBroadcast variant is proven to render in game', () => {
    expect(loadConfig({ DUNE_PG_URL: 'postgres://x/y' }).globalMessageMode).toBe('both');
    expect(loadConfig({ DUNE_PG_URL: 'postgres://x/y', DUNE_GLOBAL_MESSAGE_MODE: 'chat' }).globalMessageMode).toBe('chat');
    expect(() => loadConfig({ DUNE_PG_URL: 'postgres://x/y', DUNE_GLOBAL_MESSAGE_MODE: 'yell' })).toThrow(/must be chat\|broadcast\|both/);
  });

  it('the admin token falls back to DUNE_PLUGIN_TOKEN so the rig needs no new secret', () => {
    expect(loadConfig({ DUNE_PG_URL: 'postgres://x/y' }).adminToken).toBe('');
    expect(loadConfig({ DUNE_PG_URL: 'postgres://x/y', DUNE_PLUGIN_TOKEN: 'plug' }).adminToken).toBe('plug');
    expect(loadConfig({ DUNE_PG_URL: 'postgres://x/y', DUNE_PLUGIN_TOKEN: 'plug', SIDECAR_ADMIN_TOKEN: 'own' }).adminToken).toBe('own');
  });
});

/**
 * The local admin routes are the connector's only control plane while Takaro is unreachable. They are also, therefore,
 * the one part of the sidecar an attacker on the compose network could use to ban players — so the auth tests matter
 * as much as the happy path.
 */
describe('the local admin endpoint', () => {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const routes: AdminRoute[] = [
    {
      method: 'POST',
      path: '/admin/ban',
      doc: 'ban somebody',
      handle: (body) => {
        calls.push({ path: '/admin/ban', body });
        if (!body.gameId) throw new Error("'gameId' is required");
        return { banned: body.gameId };
      },
    },
    { method: 'GET', path: '/admin/bans', doc: 'list bans', handle: () => ({ bans: [] }) },
  ];

  async function server(adminToken?: string): Promise<{ url: string; stop: () => Promise<void> }> {
    const s = new HealthServer(0, '127.0.0.1', () => ({ ok: true }), { ...(adminToken ? { adminToken } : {}), routes });
    await s.start();
    return { url: `http://127.0.0.1:${s.address()}`, stop: () => s.stop() };
  }

  it('serves /health unchanged and requires a bearer token for /admin', async () => {
    const s = await server('sekrit');
    try {
      expect((await fetch(`${s.url}/health`)).status).toBe(200);

      // No token, wrong token, and a token of the wrong length are all 401 — and say nothing about the routes.
      const attempts: Record<string, string>[] = [{}, { authorization: 'Bearer nope!!' }, { authorization: 'Bearer sek' }];
      for (const headers of attempts) {
        const res = await fetch(`${s.url}/admin/ban`, { method: 'POST', headers, body: '{}' });
        expect(res.status).toBe(401);
        expect(await res.json()).toEqual({ ok: false, error: 'unauthorized' });
      }
      expect(calls).toEqual([]);

      const ok = await fetch(`${s.url}/admin/ban`, {
        method: 'POST',
        headers: { authorization: 'Bearer sekrit', 'content-type': 'application/json' },
        body: JSON.stringify({ gameId: 'A1B2C3D4E5F60718', reason: 'proof' }),
      });
      expect(ok.status).toBe(200);
      expect(await ok.json()).toEqual({ ok: true, result: { banned: 'A1B2C3D4E5F60718' } });
      expect(calls.at(-1)?.body).toEqual({ gameId: 'A1B2C3D4E5F60718', reason: 'proof' });
    } finally {
      await s.stop();
    }
  });

  it('is entirely absent when no token is configured, so a default deployment exposes nothing', async () => {
    const s = await server();
    try {
      const res = await fetch(`${s.url}/admin/bans`, { headers: { authorization: 'Bearer sekrit' } });
      expect(res.status).toBe(404);
      expect((await fetch(`${s.url}/health`)).status).toBe(200);
    } finally {
      await s.stop();
    }
  });

  it('documents itself, enforces the method, and rejects a bad body without calling the handler', async () => {
    const s = await server('sekrit');
    const auth = { authorization: 'Bearer sekrit' };
    try {
      const index = (await (await fetch(`${s.url}/admin`, { headers: auth })).json()) as { routes: { path: string }[] };
      expect(index.routes.map((r) => r.path).sort()).toEqual(['/admin/ban', '/admin/bans']);

      expect((await fetch(`${s.url}/admin/bans`, { method: 'POST', headers: auth, body: '{}' })).status).toBe(405);
      expect((await fetch(`${s.url}/admin/nope`, { method: 'POST', headers: auth, body: '{}' })).status).toBe(404);

      const before = calls.length;
      const bad = await fetch(`${s.url}/admin/ban`, { method: 'POST', headers: auth, body: 'not json' });
      expect(bad.status).toBe(400);
      expect(calls).toHaveLength(before);

      // A handler that throws becomes a 400 with its message, not a 500 with a stack.
      const missing = await fetch(`${s.url}/admin/ban`, { method: 'POST', headers: auth, body: '{}' });
      expect(missing.status).toBe(400);
      expect(await missing.json()).toEqual({ ok: false, error: "'gameId' is required" });
    } finally {
      await s.stop();
    }
  });
});

/**
 * A live Dune `TeleportToExact` takes 19–20 s to reach `Now running ServerCommand`. Takaro abandons a connector
 * request after `ws.requestTimeoutMs` — **10 s** by default (`app-connector/src/config.ts`, env
 * `T_WEBSOCKET_REQUEST_TIMEOUT_MS`) — and ignores a reply that arrives later. So a 30 s read-back can never reach
 * anybody: the operator only ever sees `Request timed out after 10000ms : teleportPlayer`.
 */
describe('a read-back never outlives Takaro’s own request timeout', () => {
  it('the 30 s teleport window is capped under the 10 s budget and the answer says PENDING, not failed', async () => {
    // A plugin that only ever answers the map origin from its weak source: the read-back cannot succeed, so the
    // reason it gives is the whole point of the test.
    const plugin = new DunePluginClient({
      baseUrl: 'http://plugin',
      token: 't',
      fetchImpl: (async (url: string) =>
        new Response(
          JSON.stringify(
            String(url).includes('/location')
              ? { x: 0, y: 0, z: 0, source: 'playerState' }
              : { count: 1, players: [{ ref: 'acct:1', characterName: 'Tester', accountId: 1 }] },
          ),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )) as unknown as typeof fetch,
    });
    // Same arithmetic as the live 10 000/1 500 ms pair, scaled down so the test does not actually poll for 8.5 s.
    const { adapter } = await harness({ plugin, teleportVerifyWindowMs: 30_000, takaroRequestTimeoutMs: 1_000, takaroRequestMarginMs: 500 });
    expect(adapter.teleportReadBackMs()).toEqual({ ms: 500, cappedBy: true, takaroTimeoutMs: 1_000 });

    const out = await adapter.teleportPlayer({ gameId: '6FF6498F4074E3DE', x: 1, y: 2, z: 3, dimension: null });
    expect(out.verified).toBe(false);
    // "pending" and "not verified" are very different statements to an operator, so they are different answers.
    expect(out.pending).toBe(true);
    expect(String(out.reason)).toMatch(/still PENDING/);
    expect(String(out.reason)).toMatch(/1000 ms/);
    expect(String(out.reason)).toMatch(/T_WEBSOCKET_REQUEST_TIMEOUT_MS/);
  });

  it('a window that already fits is left alone, and the answer is a plain verification result', async () => {
    const { adapter } = await harness({ teleportVerifyWindowMs: 3_000, takaroRequestTimeoutMs: 10_000 });
    expect(adapter.teleportReadBackMs()).toEqual({ ms: 3_000, cappedBy: false, takaroTimeoutMs: 10_000 });
  });

  it('a Takaro timeout of 0 means "no budget known" and the configured window stands', async () => {
    const { adapter } = await harness({ teleportVerifyWindowMs: 30_000, takaroRequestTimeoutMs: 0 });
    expect(adapter.teleportReadBackMs()).toEqual({ ms: 30_000, cappedBy: false, takaroTimeoutMs: 0 });
  });

  it('the cap never collapses below one verify interval, so there is always time for one poll', async () => {
    const { adapter } = await harness({ teleportVerifyWindowMs: 30_000, takaroRequestTimeoutMs: 100, takaroRequestMarginMs: 1_500 });
    // The margin alone exceeds the budget; the floor is the verify interval (20 ms in this harness).
    expect(adapter.teleportReadBackMs().ms).toBe(20);
  });
});
