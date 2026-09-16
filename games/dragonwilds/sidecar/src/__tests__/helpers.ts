/** Drops the `timestamp` the poller stamps on every forwarded event, so payload assertions stay readable. */
export function noTs(data: unknown): unknown {
  if (!data || typeof data !== 'object') return data;
  const { timestamp, ...rest } = data as Record<string, unknown>;
  void timestamp;
  return rest;
}
