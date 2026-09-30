/** Backend UTC anchor plus monotonic elapsed time; never the device wall clock. */
export function createServerClock() {
  let anchor: { serverMs: number; receivedAt: number } | undefined;
  return {
    sync(serverNow?: string, receivedAt = performance.now()) {
      // Refuse timezone-less values rather than interpreting them in local time.
      if (!serverNow || !/(Z|[+-]\d{2}:\d{2})$/i.test(serverNow)) return;
      const serverMs = Date.parse(serverNow);
      if (!Number.isFinite(serverMs) || !Number.isFinite(receivedAt)) return;
      // A delayed response must not replace a more recent server observation.
      if (anchor && (receivedAt < anchor.receivedAt || serverMs < anchor.serverMs)) return;
      anchor = { serverMs, receivedAt };
    },
    now(): number | null {
      return anchor ? anchor.serverMs + Math.max(0, performance.now() - anchor.receivedAt) : null;
    },
  };
}
