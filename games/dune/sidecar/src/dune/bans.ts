import { logger } from '../logger.js';
import type { BanStore, PendingBan } from './banStore.js';
import type { TakaroBan, TakaroPlayer } from './types.js';

/**
 * Dune has no native ban. The server can kick, and that is all — so a ban in this connector is a connector-owned
 * record plus enforcement: the banned player is kicked the moment the ban is created and again every time they are
 * seen online, which the player experiences as "I cannot stay on this server". The README says exactly that; nothing
 * here pretends the game is refusing the login.
 *
 * Timed bans are lifted by us as well: Takaro never sends `unbanPlayer` when a ban expires (wire gotcha), so an
 * expired row is simply dropped from the store and stops being enforced and stops being listed.
 */
export interface DuneBanRecord extends PendingBan {
  /** Last-known player record, so `listBans` can answer a real IGamePlayer for someone who is offline. */
  player?: TakaroPlayer;
  /** `null`/absent means permanent. */
  expiresAt: string;
  reason?: string;
}

export interface BanEnforcerOptions {
  store?: BanStore;
  /** Kicks the player; resolves true when the kick was published. */
  kick: (gameId: string, reason: string) => Promise<boolean>;
  /** Minimum gap between two kick attempts for the same player. */
  kickCooldownMs?: number;
  now?: () => number;
}

/** A permanent ban is stored with this sentinel expiry so the on-disk shape stays one array of the same record. */
export const PERMANENT = '';

export class BanManager {
  private bans: DuneBanRecord[] = [];
  private cold = false;
  private readonly lastKick = new Map<string, number>();
  private sweeps = 0;
  private kicks = 0;
  private lastSweepAt: string | null = null;
  private lastKickAt: string | null = null;

  constructor(private readonly options: BanEnforcerOptions) {
    this.reload();
  }

  private now(): number {
    return this.options.now?.() ?? Date.now();
  }

  /**
   * Re-reads the persisted store. Called at construction AND at the top of every `listBans`, so the on-disk file —
   * not our in-memory copy — is the source of truth for `expiresAt` even on the first answer after a restart. A store
   * that exists but cannot be read marks the manager "cold": `listBans` then fails loudly, because reporting a timed
   * ban as permanent leaves an unmanaged `until: null` row in Takaro that survives our own lift (VEIN F16).
   */
  reload(): void {
    if (!this.options.store) {
      this.cold = false;
      return;
    }
    try {
      this.bans = this.options.store.load().map((b) => b as DuneBanRecord);
      this.cold = false;
    } catch (err) {
      this.cold = true;
      logger.warn(`Ban store unavailable: ${(err as Error).message}`);
    }
  }

  unavailable(): boolean {
    return this.cold;
  }

  all(): DuneBanRecord[] {
    return this.bans.map((b) => ({ ...b }));
  }

  isBanned(gameId: string, now = this.now()): DuneBanRecord | undefined {
    return this.bans.find((b) => b.gameId === gameId && !isExpired(b, now));
  }

  /** Adds or replaces a ban. `expiresAt` null/absent = permanent. */
  add(gameId: string, reason: string | null, expiresAt: string | null, player?: TakaroPlayer): DuneBanRecord {
    const record: DuneBanRecord = {
      gameId,
      expiresAt: expiresAt ?? PERMANENT,
      ...(reason ? { reason } : {}),
      ...(player ? { player } : {}),
    };
    this.save([...this.bans.filter((b) => b.gameId !== gameId), record]);
    return record;
  }

  remove(gameId: string): boolean {
    const before = this.bans.length;
    this.save(this.bans.filter((b) => b.gameId !== gameId));
    this.lastKick.delete(gameId);
    return this.bans.length !== before;
  }

  /** Drops every expired ban and reports the ids lifted, so the caller can log the lift. */
  sweep(now = this.now()): string[] {
    const expired = this.bans.filter((b) => isExpired(b, now)).map((b) => b.gameId);
    if (expired.length) {
      this.save(this.bans.filter((b) => !isExpired(b, now)));
      for (const id of expired) {
        this.lastKick.delete(id);
        logger.info(`Timed ban expired for ${id}; lifted by the connector (Takaro never sends unbanPlayer for these)`);
      }
    }
    return expired;
  }

  /**
   * Takaro's listBans. Expired rows are swept first and never returned — an expired ban that is still listed makes
   * Takaro's next `banCreate` for that player 409.
   */
  list(): TakaroBan[] {
    this.reload();
    if (this.cold) {
      throw new Error(
        'Cannot list bans: the ban store could not be read, and reporting a timed ban as permanent would leave an unmanaged ban in Takaro',
      );
    }
    this.sweep();
    return this.bans.map((b) => ({
      player: b.player ?? { gameId: b.gameId, name: b.gameId },
      reason: b.reason ?? '',
      expiresAt: b.expiresAt ? b.expiresAt : null,
    }));
  }

  /** Counters for `/health`, so "enforcement is alive" is observable and not just asserted. */
  status(): Record<string, unknown> {
    return {
      bans: this.bans.length,
      storeUnavailable: this.cold,
      sweeps: this.sweeps,
      kicks: this.kicks,
      lastSweepAt: this.lastSweepAt,
      lastKickAt: this.lastKickAt,
    };
  }

  /**
   * Kick-on-sight: called with the currently online players on every enforcement tick, and directly when a ban is
   * created. The store is re-read first, so a ban written by the local admin route — or by an operator editing
   * `bans.json` — is honoured without a restart.
   */
  async enforce(online: TakaroPlayer[], now = this.now()): Promise<string[]> {
    this.reload();
    this.sweeps += 1;
    this.lastSweepAt = new Date(now).toISOString();
    this.sweep(now);
    const cooldown = this.options.kickCooldownMs ?? 15_000;
    const kicked: string[] = [];
    for (const player of online) {
      const ban = this.isBanned(player.gameId, now);
      if (!ban) continue;
      const last = this.lastKick.get(player.gameId);
      // `has` rather than `?? 0`: "never kicked" must not look like "kicked at t=0".
      if (last !== undefined && now - last < cooldown) continue;
      this.lastKick.set(player.gameId, now);
      try {
        const ok = await this.options.kick(player.gameId, ban.reason || 'Banned');
        this.kicks += 1;
        this.lastKickAt = new Date(now).toISOString();
        if (ok) kicked.push(player.gameId);
        logger.info(`Ban enforcement: kicked ${player.name} (${player.gameId})${ok ? '' : ' — publish failed, will retry'}`);
      } catch (err) {
        logger.warn(`Ban enforcement kick for ${player.gameId} failed: ${(err as Error).message}`);
      }
    }
    return kicked;
  }

  private save(bans: DuneBanRecord[]): void {
    this.bans = bans;
    try {
      this.options.store?.save(bans);
    } catch (err) {
      logger.warn(`Could not persist bans: ${(err as Error).message}`);
    }
  }
}

export function isExpired(ban: Pick<DuneBanRecord, 'expiresAt'>, now: number): boolean {
  if (!ban.expiresAt) return false; // permanent
  const at = Date.parse(ban.expiresAt);
  return Number.isFinite(at) && at <= now;
}

/** Takaro sends `expiresAt` as an ISO string, a number, or an explicit JSON null (= permanent). */
export function expiryOf(value: unknown): string | null {
  // 0 / negative epochs are Takaro's "no expiry", not 1970.
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? new Date(value).toISOString() : null;
  if (typeof value !== 'string') return null;
  const s = value.trim();
  if (!s) return null;
  const parsed = Date.parse(s);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null;
}
