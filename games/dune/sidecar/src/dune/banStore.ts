import fs from 'node:fs';
import path from 'node:path';

export interface PendingBan {
  gameId: string;
  /** ISO timestamp at which the connector must stop enforcing the ban; '' = permanent. */
  expiresAt: string;
  reason?: string;
}

/**
 * Dune has no native ban at all, and Takaro never sends `unbanPlayer` when a timed ban expires: it expects the
 * connector to lift it. So the whole ban list — permanent and timed alike — is persisted here next to the event
 * cursor and swept by `bans.ts`. `expiresAt: ''` means permanent.
 */
export interface BanStore {
  load(): PendingBan[];
  save(bans: PendingBan[]): void;
}

export class FileBanStore implements BanStore {
  constructor(private readonly file: string) {}

  /**
   * Reads the persisted timed bans. A missing file means "no timed bans" and is the normal first-boot state, but a
   * file that exists and cannot be read or parsed THROWS: answering a `listBans` from an empty store would report a
   * live timed ban as permanent, and Takaro then creates an unmanaged `until: null` ban row that survives our own
   * lift (finding F16). Callers must fail the request instead.
   */
  load(): PendingBan[] {
    let raw: string;
    try {
      raw = fs.readFileSync(this.file, 'utf8');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return [];
      throw new Error(`Cannot read ban store ${this.file}: ${(err as Error).message}`);
    }
    if (!raw.trim()) return [];
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch (err) {
      throw new Error(`Ban store ${this.file} is corrupt: ${(err as Error).message}`);
    }
    if (!Array.isArray(parsed)) throw new Error(`Ban store ${this.file} is not a JSON array`);
    return parsed.filter((b): b is PendingBan => typeof b?.gameId === 'string' && typeof b?.expiresAt === 'string');
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
