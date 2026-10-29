import { mkdirSync, chmodSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export class SessionStore {
  constructor(dbPath) {
    mkdirSync(path.dirname(dbPath), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(dbPath);
    try { chmodSync(dbPath, 0o600); } catch { /* Windows does not expose POSIX modes. */ }
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA busy_timeout = 5000;
      CREATE TABLE IF NOT EXISTS consumed_assertions (
        jti_hash TEXT PRIMARY KEY,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sessions (
        sid_hash TEXT PRIMARY KEY,
        assertion TEXT NOT NULL,
        auth_user_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        idle_expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
      );
    `);
    this.purgeExpired();
  }

  consumeAssertion(jtiHash, expiresAt, now = Math.floor(Date.now() / 1000)) {
    this.purgeExpired(now);
    try {
      this.db.prepare(
        'INSERT INTO consumed_assertions (jti_hash, expires_at, consumed_at) VALUES (?, ?, ?)',
      ).run(jtiHash, expiresAt, now);
      return true;
    } catch (error) {
      if (String(error?.message || '').includes('UNIQUE')) return false;
      throw error;
    }
  }

  purgeExpired(now = Math.floor(Date.now() / 1000)) {
    this.db.prepare('DELETE FROM consumed_assertions WHERE expires_at <= ?').run(now);
    this.db.prepare('DELETE FROM sessions WHERE expires_at <= ? OR idle_expires_at <= ?').run(now, now);
  }

  createSession(session) {
    this.db.prepare(`
      INSERT INTO sessions
        (sid_hash, assertion, auth_user_id, actor_id, agent_id, expires_at, idle_expires_at, created_at, last_seen)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      session.sidHash,
      session.assertion,
      session.authUserId,
      String(session.actorId),
      session.agentId,
      session.expiresAt,
      session.idleExpiresAt,
      session.createdAt,
      session.createdAt,
    );
  }

  getSession(sidHash, now = Math.floor(Date.now() / 1000)) {
    const row = this.db.prepare('SELECT * FROM sessions WHERE sid_hash = ?').get(sidHash);
    if (!row) return null;
    if (row.expires_at <= now || row.idle_expires_at <= now) {
      this.db.prepare('DELETE FROM sessions WHERE sid_hash = ?').run(sidHash);
      return null;
    }
    return row;
  }

  touchSession(sidHash, idleExpiresAt, now = Math.floor(Date.now() / 1000)) {
    this.db.prepare(
      'UPDATE sessions SET idle_expires_at = ?, last_seen = ? WHERE sid_hash = ?',
    ).run(idleExpiresAt, now, sidHash);
  }

  deleteSession(sidHash) { this.db.prepare('DELETE FROM sessions WHERE sid_hash = ?').run(sidHash); }

  close() { this.db.close(); }
}
