import { createHash, randomBytes } from 'node:crypto';

const digest = (value) => createHash('sha256').update(value).digest('hex');

export class DownloadStore {
  constructor({ maxEntries = 1024, maxPerSid = 16 } = {}) {
    this.maxEntries = maxEntries;
    this.maxPerSid = maxPerSid;
    this.entries = new Map();
  }

  issue(entry) {
    this.purge();
    const sameSid = [...this.entries.entries()].filter(([, item]) => item.sidHash === entry.sidHash);
    while (sameSid.length >= this.maxPerSid) {
      const [oldest] = sameSid.shift();
      this.entries.delete(oldest);
    }
    while (this.entries.size >= this.maxEntries) this.entries.delete(this.entries.keys().next().value);
    const handle = randomBytes(32).toString('base64url');
    this.entries.set(digest(handle), Object.freeze({ ...entry, expiresAt: Math.min(entry.expiresAt, Date.now() + 5 * 60 * 1000) }));
    return handle;
  }

  get(handle) {
    if (typeof handle !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(handle)) return null;
    const entry = this.entries.get(digest(handle));
    if (!entry || entry.expiresAt <= Date.now()) {
      if (entry) this.entries.delete(digest(handle));
      return null;
    }
    return entry;
  }

  purge() {
    const now = Date.now();
    for (const [key, entry] of this.entries) if (entry.expiresAt <= now) this.entries.delete(key);
  }

  clear() { this.entries.clear(); }
}
