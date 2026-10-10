/** A bounded request window; each fragment keeps its own monotonic age bound. */
export class VideoRequests {
  constructor(limit = 2) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 3) throw new Error("Invalid video request window");
    this.limit = limit;
    this.nextId = 0;
    this.pending = new Map();
  }
  reserve(now) {
    const ids = [];
    while (this.pending.size < this.limit) {
      const id = ++this.nextId;
      this.pending.set(id, now);
      ids.push(id);
    }
    return ids;
  }
  has(id) { return this.pending.has(id); }
  complete(id, captureAge, now, serverWait = 0) {
    const sent = this.pending.get(id);
    if (sent === undefined || ![captureAge, now].every(Number.isFinite) || captureAge < 0 || now < sent)
      throw new Error("Invalid or unsolicited video response");
    this.pending.delete(id);
    const elapsed = now - sent;
    // Only measured rover queue wait can be removed; network time stays bounded
    // by the remaining complete request interval. Invalid metadata uses the full bound.
    const wait = Number.isFinite(serverWait) && serverWait >= 0 && serverWait <= elapsed ? serverWait : 0;
    return captureAge + elapsed - wait;
  }
  expired(now, timeout = 1000) {
    return [...this.pending.values()].some(sent => now - sent > timeout);
  }
  clear() { this.pending.clear(); }
}
