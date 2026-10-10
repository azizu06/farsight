/** Shared desktop/XR headline badge. A failed health poll cannot erase REPLAY. */
export class ReplayBadge {
  replay = false;
  busy = false;

  async refresh(fetcher = fetch) {
    if (this.busy) return;
    this.busy = true;
    try {
      const response = await fetcher('/health', { cache: 'no-store', signal: AbortSignal.timeout(2000) });
      if (response.ok) {
        const health = await response.json();
        if (typeof health.replay === 'boolean') this.replay = health.replay;
      }
    } catch { /* Retain the last confirmed flag while disconnected. */ }
    finally { this.busy = false; }
  }

  label(source = '') {
    return this.replay || source.startsWith('REPLAY:') ? 'REPLAY · ' : '';
  }
}
