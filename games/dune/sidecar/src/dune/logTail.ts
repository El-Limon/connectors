import fs from 'node:fs';
import { logger } from '../logger.js';

/**
 * Optional tail of the map-server log, forwarded as Takaro `log` events. Dune has no documented log grammar, so this
 * deliberately parses NOTHING: it forwards lines (redacted, noise-filtered) and nothing else. Connect/disconnect,
 * chat and death all come from Postgres and RabbitMQ, where the meaning is unambiguous.
 */
export interface LogTailOptions {
  file: string;
  onLine: (msg: string) => void;
  intervalMs?: number;
  onError?: (err: Error) => void;
  /** Start at the end of an existing file rather than replaying it into Takaro. */
  fromEnd?: boolean;
}

export class LogTailer {
  private timer: NodeJS.Timeout | null = null;
  private offset = 0;
  private inode: number | null = null;
  private started = false;

  constructor(private readonly options: LogTailOptions) {}

  active(): boolean {
    return this.timer !== null;
  }

  start(): void {
    if (this.timer || !this.options.file) return;
    this.timer = setInterval(() => this.tick(), this.options.intervalMs ?? 1000);
    this.timer.unref?.();
    this.tick();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private tick(): void {
    try {
      const stat = fs.statSync(this.options.file);
      if (!this.started) {
        this.started = true;
        this.inode = stat.ino;
        // A restart must not replay the whole existing log into Takaro's rate-limited `log` channel.
        this.offset = this.options.fromEnd === false ? 0 : stat.size;
        return;
      }
      // Truncated or rotated: start over from the top of the new file.
      if (stat.ino !== this.inode || stat.size < this.offset) {
        this.inode = stat.ino;
        this.offset = 0;
      }
      if (stat.size === this.offset) return;
      const fd = fs.openSync(this.options.file, 'r');
      try {
        const length = stat.size - this.offset;
        const buffer = Buffer.alloc(length);
        const read = fs.readSync(fd, buffer, 0, length, this.offset);
        this.offset += read;
        const text = buffer.subarray(0, read).toString('utf8');
        const lines = text.split(/\r?\n/);
        // A trailing partial line is left for the next tick.
        if (!text.endsWith('\n')) this.offset -= Buffer.byteLength(lines.pop() ?? '', 'utf8');
        for (const line of lines) {
          if (line.trim()) this.options.onLine(line);
        }
      } finally {
        fs.closeSync(fd);
      }
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return; // not created yet
      this.options.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  }
}

/**
 * Unreal chatter that is worthless to a server admin. Takaro rate-limits `log` per server (sustained ~50 per 30 s)
 * and a UE5 dedicated server prints thousands of HTTP / online-subsystem / streaming lines, which would burn the
 * whole budget before a single interesting line got through.
 */
export const NOISY_LOG = [
  /\bLogHttp\b/,
  /\bLogOnline(?:Session|Identity|Presence|Friend)?:\s*Verbose/,
  /\bLogEOS\w*\b/,
  /\bLogStreaming\b/,
  /\bLogNetTraffic\b/,
  /\bLogGarbage\b/,
  /\bLogSlateStyle\b/,
  /^\s*$/,
];

export function shouldForwardLog(mode: 'all' | 'filtered' | 'none', msg: string): boolean {
  if (mode === 'none') return false;
  if (mode === 'all') return true;
  return !NOISY_LOG.some((re) => re.test(msg));
}

/**
 * The battlegroup prints its own secrets: the FLS self-hosting token, the `ServerCommandsAuthToken`, the DB password
 * and the join password all appear on the map process's command line, which UE echoes at boot. Redaction is
 * unconditional and applies before the line can reach Takaro, a log file, or a DEBUG dump.
 */
const SECRET_KEYS = /(AuthToken|ServiceAuthToken|ServerCommandsAuthToken|DatabasePassword|ServerLoginPassword|Password|Secret|Token)/i;
const SECRET_ASSIGNMENT = /((?:\w*)(?:AuthToken|Password|Secret|Token))(\s*[=:]\s*)("?)([^\s",;]*)\3/gi;

export function redactLog(msg: string): string {
  let out = msg.replace(/(\?p=)[^?\s]*/gi, '$1[redacted]').replace(/(\bTicket=)[A-Za-z0-9+/=]*/gi, '$1[redacted]');
  if (!SECRET_KEYS.test(out)) return out;
  let replaced = false;
  out = out.replace(SECRET_ASSIGNMENT, (_m, key: string, sep: string) => {
    replaced = true;
    return `${key}${sep}[redacted]`;
  });
  // A secret mentioned without a `key=value` shape is dropped wholesale rather than leaked.
  return replaced ? out : '[redacted: line mentions a secret]';
}

export function makeLogForwarder(mode: 'all' | 'filtered' | 'none', emit: (msg: string) => void): (line: string) => void {
  return (line: string): void => {
    const clean = redactLog(line);
    if (!shouldForwardLog(mode, clean)) return;
    emit(clean);
  };
}

export function warnMissingLogFile(file: string): void {
  logger.warn(`DUNE_LOG_FILE=${file} does not exist yet; the log tail will start when it appears`);
}
