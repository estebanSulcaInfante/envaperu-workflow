import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';
import { createBridgeServer } from '../src/server.js';
import { DownloadStore } from '../src/download_store.js';
import { NativeArtifactBroker } from '../src/native_artifacts.js';

const identity = 'scm+aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@internal.envaperu.invalid';
const agentId = 'scm-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const hash = (value) => createHash('sha256').update(value).digest('hex');

const configFor = (root) => ({
  host: '127.0.0.1', port: 0, dbPath: path.join(root, 'bridge.sqlite'), nativeManifestPath: path.join(root, 'manifest.json'),
  provisionMode: 'auto', allowedOrigin: 'https://bridge.example.test', scmFormOrigin: 'https://scm.example.test',
  nativeHttpOrigin: 'http://127.0.0.1:18789', nativeWsUrl: 'ws://127.0.0.1:18789', maxBodyBytes: 16 * 1024 * 1024,
  maxDownloadBytes: 16 * 1024 * 1024, maxInlineBytes: 8 * 1024 * 1024, apiTimeoutMs: 1000, maxIdleSeconds: 900,
  maxSessionSeconds: 3600, cookieName: 'scm_personal_sid', maxWsPayload: 16 * 1024 * 1024,
});

test('download handles are opaque, SID-bound and revocable without storing bytes', async () => {
  const root = mkdtempSync(path.join(tmpdir(), 'scm-download-'));
  writeFileSync(path.join(root, 'manifest.json'), JSON.stringify({ entries: [] }));
  const config = configFor(root);
  const store = new DownloadStore();
  let explode = false;
  const artifactBroker = { serveHandle: async ({ res }) => { if (explode) throw new Error('private path'); res.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="x.pdf"' }); res.end('PDF'); } };
  const auth = () => ({ identity: { auth_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', actor_id: 7, email: identity }, agent_id: agentId, jti: 'download-jti', expires_at: new Date(Date.now() + 60_000).toISOString() });
  const bridge = createBridgeServer(config, { downloadStore: store, artifactBroker, scm: { authorize: async () => auth() }, provisioner: async () => {}, manifest: null });
  await new Promise((resolve) => bridge.listen(0, '127.0.0.1', resolve));
  const port = bridge.server.address().port;
  const handoff = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, { method: 'POST', redirect: 'manual', headers: { Origin: config.scmFormOrigin, 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion: 'opaque' }) });
  assert.equal(handoff.status, 303, await handoff.text());
  const cookie = handoff.headers.get('set-cookie').split(';')[0];
  const sid = decodeURIComponent(cookie.split('=')[1]);
  const handle = store.issue({ sidHash: hash(sid), agentId, sessionKey: `agent:${agentId}:s`, artifactId: 'artifact-pdf', summary: { id: 'artifact-pdf', sessionKey: `agent:${agentId}:s`, mimeType: 'application/pdf', sizeBytes: 3 }, expiresAt: Date.now() + 60_000 });
  assert.match(handle, /^[A-Za-z0-9_-]{43}$/);
  const response = await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${handle}`, { headers: { Cookie: cookie } });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'PDF');
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${handle}`, { method: 'POST', headers: { Cookie: cookie, Origin: config.allowedOrigin } })).status, 405);
  const tampered = `${handle.slice(0, -1)}${handle.endsWith('A') ? 'B' : 'A'}`;
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${tampered}`, { headers: { Cookie: cookie } })).status, 404);
  store.clear();
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${handle}`, { headers: { Cookie: cookie } })).status, 404);
  const errorHandle = store.issue({ sidHash: hash(sid), agentId, sessionKey: `agent:${agentId}:s`, artifactId: 'artifact-pdf', summary: { id: 'artifact-pdf', sessionKey: `agent:${agentId}:s`, mimeType: 'application/pdf', sizeBytes: 3 }, expiresAt: Date.now() + 60_000 });
  explode = true;
  const errorResponse = await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${errorHandle}`, { headers: { Cookie: cookie } });
  assert.equal(errorResponse.status, 502);
  assert.doesNotMatch(await errorResponse.text(), /private path/);
  await bridge.close();
  rmSync(root, { recursive: true, force: true });
});

test('artifact download parser rejects foreign session, runId and unknown fields before native access', async () => {
  const broker = new NativeArtifactBroker({ config: { maxInlineBytes: 8 * 1024 * 1024 } , store: new DownloadStore() });
  const authorization = { identity: { auth_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', actor_id: 7, email: identity }, agent_id: agentId };
  for (const params of [
    { sessionKey: 'not-owned', artifactId: 'a' },
    { sessionKey: `agent:${agentId}:s`, artifactId: 'a', runId: 'r', messageRole: 'assistant' },
    { sessionKey: 'agent:scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:s', artifactId: 'a', messageRole: 'assistant' },
    { sessionKey: `agent:${agentId}:s`, artifactId: 'a', messageRole: 'assistant', extra: true },
  ]) {
    await assert.rejects(() => broker.downloadRpc({ frame: { id: 'x', params }, authorization, clientAddress: '192.0.2.10', reauthorize: async () => null }), /download_|artifact_/);
  }
});

test('download store evicts per SID and expires handles without retaining bytes', () => {
  const store = new DownloadStore({ maxEntries: 2, maxPerSid: 1 });
  const base = { agentId, sessionKey: `agent:${agentId}:s`, artifactId: 'a', summary: { id: 'a' } };
  const first = store.issue({ ...base, sidHash: 'sid-a', expiresAt: Date.now() + 60_000 });
  const second = store.issue({ ...base, sidHash: 'sid-a', artifactId: 'b', summary: { id: 'b' }, expiresAt: Date.now() + 60_000 });
  assert.equal(store.get(first), null);
  assert.ok(store.get(second));
  const third = store.issue({ ...base, sidHash: 'sid-b', artifactId: 'c', summary: { id: 'c' }, expiresAt: Date.now() - 1 });
  assert.equal(store.get(third), null);
});
import { EventEmitter } from 'node:events';
const sessionKey = `agent:${agentId}:s`;
const authorization = { identity: { auth_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', actor_id: 7, email: identity }, agent_id: agentId, expires_at: new Date(Date.now() + 60000).toISOString() };
const summary = { id: 'artifact-pdf', sessionKey, type: 'file', title: 'x.pdf', source: 'session-transcript', messageSeq: 1, mimeType: 'application/pdf', sizeBytes: 3, download: { mode: 'url' } };
const entry = { sidHash: 'sid', agentId, sessionKey, artifactId: 'artifact-pdf', summary };
const fresh = async () => ({ row: { sid_hash: 'sid', agent_id: agentId } });
const frame = { id: 'request', params: { sessionKey, artifactId: 'artifact-pdf' } };
function harness({ own = true, payload, response, fetchError = false } = {}) {
  let closed = 0, opened = 0;
  const broker = new NativeArtifactBroker({
    config: { nativeHttpOrigin: 'http://127.0.0.1:18789', maxInlineBytes: 8, maxDownloadBytes: 16, apiTimeoutMs: 1000 }, store: new DownloadStore(),
    verifyOwnSession: async () => own,
    downloadExact: async ({ consume, params }) => { opened++; assert.equal(params.messageRole, 'assistant'); try { return await consume({ ok: true, payload: payload || { artifact: summary, url: '/api/artifacts/download/c/t' } }); } finally { closed++; } },
    fetchImpl: async (_url, options) => { assert.equal(options.redirect, 'error'); if (fetchError) throw Error('private host'); return response || new Response('PDF', { headers: { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="x.pdf"', 'Content-Length': '3' } }); },
  });
  return { broker, counts: () => ({ opened, closed }) };
}
function io(backpressure = false) {
  const req = Object.assign(new EventEmitter(), { method: 'GET', rawHeaders: [], headers: {}, aborted: false });
  const res = Object.assign(new EventEmitter(), { destroyed: false, chunks: [], ended: false,
    writeHead(status, headers) { this.status = status; this.headers = headers; },
    write(chunk) { this.chunks.push(chunk); if (backpressure) queueMicrotask(() => this.destroy()); return !backpressure; },
    end() { this.ended = true; }, destroy() { this.destroyed = true; this.emit('close'); },
  });
  return { req, res };
}
const consume = (h, pair = io()) => h.broker.serveHandle({ entry, ...pair, authorization, clientAddress: '192.0.2.1', reauthorize: fresh });

test('native UI two-field request returns bound URL; hidden session never reaches admin', async () => {
  const h = harness();
  const result = await h.broker.downloadRpc({ frame, authorization, clientAddress: '192.0.2.1', reauthorize: fresh, sidHash: 'sid' });
  assert.match(result.payload.url, /^\/api\/artifacts\/download\/scm\/[A-Za-z0-9_-]{43}$/);
  assert.equal(h.counts().closed, 1); assert.equal(h.broker.active, 0);
  const hidden = harness({ own: false }); await assert.rejects(() => consume(hidden), /not_owned/);
  assert.deepEqual(hidden.counts(), { opened: 0, closed: 0 });
});

test('backpressure disconnect releases admin and concurrency slot', async () => {
  const h = harness(); const pair = io(true);
  await Promise.race([consume(h, pair), new Promise((_, reject) => setTimeout(() => reject(Error('stream stuck')), 1000))]);
  assert.equal(pair.res.destroyed, true); assert.equal(pair.res.ended, false);
  assert.equal(h.broker.active, 0); assert.equal(h.broker.activeBySid.size, 0);
  assert.deepEqual(h.counts(), { opened: 1, closed: 1 });
});

test('concurrency limits are per SID and global; release is idempotent', () => {
  const { broker } = harness(); const slots = [broker.acquire('a'), broker.acquire('a')];
  assert.throws(() => broker.acquire('a'), /capacity/);
  for (let n = 0; n < 6; n++) slots.push(broker.acquire('sid-' + n));
  assert.throws(() => broker.acquire('other'), /capacity/);
  for (const release of slots) { release(); release(); }
  assert.equal(broker.active, 0); assert.equal(broker.activeBySid.size, 0);
});

test('artifact snapshot and native URL cannot be substituted', async () => {
  for (const changed of [{ title: 'other.pdf' }, { source: 'other' }, { type: 'image' }, { messageSeq: 2 }]) {
    const h = harness({ payload: { artifact: { ...summary, ...changed }, url: '/api/artifacts/download/c/t' } });
    await assert.rejects(() => consume(h), /artifact_changed/); assert.equal(h.counts().closed, 1);
  }
  for (const url of ['https://evil.invalid/file', 'http://127.0.0.1:18789/api/artifacts/download/c/t', '//evil.invalid/file', '/api/artifacts/download/c/t?token=x', `/api/chat/media/outgoing/${encodeURIComponent(sessionKey)}/a/full?mediaTicket=a&mediaTicket=b`]) {
    const h = harness({ payload: { artifact: summary, url } });
    await assert.rejects(() => consume(h), /native_url/); assert.equal(h.broker.active, 0);
  }
});

test('base64 must be canonical and exactly match native summary', async () => {
  for (const data of ['UEQ=', 'UE RG', 'UE==']) {
    const h = harness({ payload: { artifact: summary, encoding: 'base64', data } });
    await assert.rejects(() => h.broker.downloadRpc({ frame, authorization, reauthorize: fresh }), /inline_download/);
    assert.equal(h.broker.active, 0);
  }
});

test('native response length, MIME and range are validated before success', async () => {
  const defaults = { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="x.pdf"' };
  for (const headers of [{ 'Content-Length': '-1' }, { 'Content-Length': 'NaN' }, { 'Content-Type': 'image/png' }]) {
    const h = harness({ response: new Response('PDF', { headers: { ...defaults, ...headers } }) });
    await assert.rejects(() => consume(h), /native_download/); assert.equal(h.broker.active, 0);
  }
  const truncated = harness({ response: new Response('P', { status: 206, headers: { ...defaults, 'Content-Range': 'bytes 0-1/3' } }) });
  const pair = io(); await consume(truncated, pair); assert.equal(pair.res.destroyed, true); assert.equal(pair.res.ended, false);
  for (const [status, headers] of [[304, {}], [416, { 'Content-Range': 'bytes */3' }]]) {
    const h = harness({ response: new Response(null, { status, headers }) }); const pair = io();
    await consume(h, pair); assert.equal(pair.res.status, status); assert.equal(pair.res.ended, true); assert.equal(h.counts().closed, 1);
  }
  const failed = harness({ fetchError: true }); await assert.rejects(() => consume(failed), /unavailable/); assert.equal(failed.counts().closed, 1);
});

test('stalled backpressure also releases on the transfer deadline without client close', async () => {
  const h = harness(); h.broker.config.apiTimeoutMs = 25;
  const pair = io(); pair.res.write = () => false;
  await Promise.race([consume(h, pair), new Promise((_, reject) => setTimeout(() => reject(Error('deadline ignored')), 500))]);
  assert.equal(h.broker.active, 0); assert.equal(h.counts().closed, 1);
  assert.equal(pair.res.ended, false);
});
