import fs from 'node:fs';
import path from 'node:path';

export interface PendingBan {
  gameId: string;
  /** ISO timestamp at which the sidecar must call the plugin's /unban. */
  expiresAt: string;
  reason?: string;
}

/**
 * Takaro never sends `unbanPlayer` for a timed ban: it expects the connector to lift the ban itself when the
 * expiry passes. The game's own ban list has no expiry field, so the sidecar persists `{gameId, expiresAt}`
 * next to the event cursor and sweeps it.
 */
export interface BanStore {
  load(): PendingBan[];
  save(bans: PendingBan[]): void;
}

export class FileBanStore implements BanStore {
  constructor(private readonly file: string) {}

  load(): PendingBan[] {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.file, 'utf8')) as unknown;
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((b): b is PendingBan => typeof b?.gameId === 'string' && typeof b?.expiresAt === 'string');
    } catch {
      return [];
    }
  }

  save(bans: PendingBan[]): void {
    fs.mkdirSync(path.dirname(path.resolve(this.file)), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(bans));
    fs.renameSync(tmp, this.file);
  }
}

export class MemoryBanStore implements BanStore {
  constructor(public bans: PendingBan[] = []) {}
  load(): PendingBan[] {
    return this.bans.map((b) => ({ ...b }));
  }
  save(bans: PendingBan[]): void {
    this.bans = bans.map((b) => ({ ...b }));
  }
}
