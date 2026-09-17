import type { GameEventType } from '../takaro/protocol.js';
import type { CursorStore } from './cursorStore.js';
import { mapPluginEvent } from './mapping.js';
import type { PluginEventsResponse } from './types.js';

/**
 * Result of handing one event to the Takaro sink:
 * - `true`/`undefined` — delivered to an open, identified socket; the persisted cursor may advance past it.
 * - `'queued'`  — kept in the sidecar's pending queue (Takaro unreachable); the scan position advances so the event is
 *   not read twice from the plugin ring, but the PERSISTED cursor stays put, so a sidecar restart replays it.
 * - `false`     — not accepted at all; stop this poll and retry from the same seq next tick.
 */
export type EmitResult = boolean | 'queued' | void;
export type EmitFn = (type: GameEventType, data: unknown, seq?: number) => EmitResult;

export interface EventPollerOptions {
  getEvents: (since: number) => Promise<PluginEventsResponse>;
  emit: EmitFn;
  store: CursorStore;
  /** Called when the plugin reports a different bootId than the stored cursor (game server restarted). */
  onRestart?: () => void;
  /** Event types that must not be forwarded from the plugin (e.g. while the log tail owns them). */
  suppress?: () => ReadonlySet<string>;
  onError?: (err: Error) => void;
  intervalMs?: number;
}

export class EventPoller {
  private timer: NodeJS.Timeout | null = null;
  private running = false;
  /** Scan position: the highest seq read from the plugin (delivered OR queued). */
  private seq: number;
  /** Persisted cursor: the highest seq actually delivered to Takaro. Never runs ahead of a queued event. */
  private persisted: number;
  private bootId: string | undefined;

  constructor(private readonly options: EventPollerOptions) {
    const state = options.store.load();
    this.seq = state.seq;
    this.persisted = state.seq;
    this.bootId = state.bootId;
  }

  /** The persisted cursor (what a restart would resume from). */
  cursor(): number {
    return this.persisted;
  }

  /** The in-memory scan position; ahead of `cursor()` while events sit in the pending queue. */
  scanCursor(): number {
    return this.seq;
  }

  /**
   * Called when a previously queued event with this seq finally reached Takaro (or was dropped for good).
   * Advances and persists the cursor; never moves it backwards.
   */
  markDelivered(seq: number): void {
    if (seq <= this.persisted) return;
    this.persisted = seq;
    this.save(seq);
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.tick(), this.options.intervalMs ?? 1000);
    void this.tick();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** One poll. Advances + persists the cursor only past events that were delivered (emit !== false). */
  async pollOnce(): Promise<number> {
    const response = await this.options.getEvents(this.seq);
    const events = Array.isArray(response.events) ? [...response.events].sort((a, b) => a.seq - b.seq) : [];

    // Plugin restarted: a different bootId (plugin >= 0.4.1, reliable even if the new process already passed our
    // seq), or its seq went backwards below our cursor. Start over from its beginning.
    const newBoot = typeof response.bootId === 'string' && response.bootId ? response.bootId : undefined;
    const bootChanged = newBoot !== undefined && this.bootId !== undefined && newBoot !== this.bootId;
    const seqWentBack = this.seq > 0 && typeof response.seq === 'number' && response.seq >= 0 && response.seq < this.seq;
    if (bootChanged || seqWentBack) {
      this.seq = 0;
      this.persisted = 0;
      this.bootId = newBoot;
      this.save(0);
      this.options.onRestart?.();
      return this.pollOnce();
    }
    if (newBoot !== undefined && this.bootId === undefined) {
      this.bootId = newBoot;
      if (this.seq === 0) this.save(0);
    }

    const suppressed = this.options.suppress?.() ?? new Set<string>();
    let advanced = this.seq;
    let delivered = this.persisted;
    // Once one event is only queued, everything after it is behind it in the queue too: the persisted cursor must not
    // jump over the hole, or a sidecar restart would skip the queued events for good (finding F10).
    let holed = this.persisted !== this.seq;
    for (const event of events) {
      if (event.seq <= advanced) continue;
      let mapped = null;
      try {
        mapped = mapPluginEvent(event);
      } catch (err) {
        this.options.onError?.(new Error(`Dropping malformed plugin event seq=${event.seq}: ${(err as Error).message}`));
      }
      if (mapped && !suppressed.has(mapped.type)) {
        const result = this.options.emit(mapped.type, mapped.data, event.seq);
        if (result === false) break; // Takaro offline and the queue is full: retry from here next tick
        if (result === 'queued') holed = true;
      }
      advanced = event.seq;
      if (!holed) delivered = event.seq;
    }
    this.seq = advanced;
    // markDelivered() may have moved the cursor from under us while flushing the queue; never move it backwards.
    if (delivered > this.persisted) {
      this.persisted = delivered;
      this.save(delivered);
    }
    return this.seq;
  }

  private save(seq: number): void {
    this.options.store.save(this.bootId ? { seq, bootId: this.bootId } : { seq });
  }

  private async tick(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      await this.pollOnce();
    } catch (err) {
      this.options.onError?.(err instanceof Error ? err : new Error(String(err)));
    } finally {
      this.running = false;
    }
  }
}
