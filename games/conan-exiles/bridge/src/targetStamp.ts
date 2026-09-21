import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { logger } from './logger.js';

/**
 * The catalog identity the release script wrote into the package.
 *
 * A running sidecar is otherwise anonymous: the operator downloaded a zip, unpacked it and
 * started it, and nothing in the process says which server build it was released against.
 * `takaro-target.json` is that answer, written once at build time and never edited, so a
 * log line and `/health` can both name it.
 */
export interface TargetStamp {
  target: string;
  fingerprint: string;
  game: string;
  platform: string;
  revision: string;
  connectorVersion: string;
  sourceRevision: string;
}

/** The package root: one level up from the compiled `dist/` this module lives in. */
export function defaultPackageRoot(): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
}

/**
 * Read the stamp beside the package, or `null` when there is none.
 *
 * A development tree has no stamp and that is not an error — it is simply unstamped, and
 * `describeStamp` says so. A stamp that cannot be parsed is warned about once and treated
 * the same way: a broken identity file must never stop a server's connector from running.
 */
export function readTargetStamp(packageRoot: string = defaultPackageRoot()): TargetStamp | null {
  const file = path.join(packageRoot, 'takaro-target.json');
  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<TargetStamp>;
    if (!parsed || typeof parsed.target !== 'string' || typeof parsed.fingerprint !== 'string') {
      throw new Error('takaro-target.json names no target and fingerprint');
    }
    return {
      target: parsed.target,
      fingerprint: parsed.fingerprint,
      game: parsed.game ?? 'conan-exiles',
      platform: parsed.platform ?? 'linux',
      revision: parsed.revision ?? 'unknown',
      connectorVersion: parsed.connectorVersion ?? 'unknown',
      sourceRevision: parsed.sourceRevision ?? 'unknown',
    };
  } catch (err) {
    logger.warn(`Ignoring unreadable ${file}: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}

/** The one line the bridge logs about itself, and the one a verification run greps for. */
export function describeStamp(stamp: TargetStamp | null): string {
  if (!stamp) return 'Takaro target: unstamped (development tree)';
  return (
    `Takaro target: ${stamp.target} (${stamp.fingerprint.slice(0, 16)}) ` +
    `revision ${stamp.revision} connector ${stamp.connectorVersion} source ${stamp.sourceRevision}`
  );
}
