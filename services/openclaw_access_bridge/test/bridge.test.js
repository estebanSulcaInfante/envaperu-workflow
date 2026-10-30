import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { WebSocket, WebSocketServer } from 'ws';
import { createBridgeServer } from '../src/server.js';
import { authorizationIdentity } from '../src/manifest.js';
import { DownloadError } from '../src/native_artifacts.js';
import { DownloadStore } from '../src/download_store.js';
import { provisionNativeIdentity } from '../src/native_admin.js';

const identity = 'scm+aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@internal.envaperu.invalid';
const agentId = 'scm-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const hash = (value) => createHash('sha256').update(value).digest('hex');

const makeFixture = async () => {
  const root = mkdtempSync(path.join(tmpdir(), 'scm-bridge-'));
  const manifestPath = path.join(root, 'manifest.json');
  const dbPath = path.join(root, 'bridge.sqlite');
  writeFileSync(manifestPath, JSON.stringify({ entries: [{ identity, agent_id: agentId, actor_id: 7, role: 'person_a' }] }));
  const native = http.createServer((_req, res) => { res.writeHead(200); res.end('native'); });
  await new Promise((resolve) => native.listen(0, '127.0.0.1', resolve));
  const nativePort = native.address().port;
  const config = {
    host: '127.0.0.1', port: 0, dbPath, nativeManifestPath: manifestPath,
    serviceSecret: 's'.repeat(32),
    allowedOrigin: 'https://bridge.example.test', scmFormOrigin: 'https://scm.example.test',
    nativeHttpOrigin: `http://127.0.0.1:${nativePort}`, nativeWsUrl: 'ws://127.0.0.1:0',
    maxBodyBytes: 16 * 1024 * 1024, apiTimeoutMs: 1000, maxIdleSeconds: 900,
    maxSessionSeconds: 3600, cookieName: 'scm_personal_sid', maxWsPayload: 16 * 1024 * 1024,
  };
  const auth = (jti = `jti-${Date.now()}`, allowed = true) => ({
    identity: { auth_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', actor_id: 7, email: identity },
    agent_id: agentId, jti, expires_at: new Date(Date.now() + 60_000).toISOString(), allowed,
  });
  return { root, native, config, auth };
};

const start = async (bridge) => {
  await new Promise((resolve) => bridge.listen(0, '127.0.0.1', resolve));
  return bridge.server.address().port;
};

const stop = async (fixture, bridge) => {
  await bridge.close();
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
};

const cookieValue = (headers, name) => {
  const value = headers.get('set-cookie') || '';
  const match = value.match(new RegExp(`(?:^|,\\s*)(${name}=[^;,]*)`));
  return match ? match[1] : null;
};

const completeExchange = async (port, assertion, origin = 'https://scm.example.test', cookie = '') => {
  const post = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual',
    headers: { Origin: origin, Cookie: cookie, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion }),
  });
  if (post.status !== 303) return post;
  const pendingCookie = cookieValue(post.headers, '__Host-scm_personal_pending');
  const continuation = await fetch(`http://127.0.0.1:${port}/_scm/continue`, {
    redirect: 'manual', headers: { Cookie: [pendingCookie, cookie].filter(Boolean).join('; ') },
  });
  continuation.sessionCookie = cookieValue(continuation.headers, 'scm_personal_sid');
  continuation.ownerMarkerCookie = cookieValue(continuation.headers, '__Host-scm_personal_owner');
  continuation.pendingCookie = pendingCookie;
  return continuation;
};

test('presentation display name limit keeps the final Unicode code point intact', () => {
  const displayName = `${'a'.repeat(119)}😀overflow`;
  const canonical = authorizationIdentity({
    identity: {
      auth_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', actor_id: 7,
      email: identity, display_name: displayName,
    },
    agent_id: agentId,
  });
  assert.equal(Array.from(canonical.displayName).length, 120);
  assert.equal(canonical.displayName, `${'a'.repeat(119)}😀`);
});

test('exchange is same-origin form-only, opaque-cookie and replay-safe across restart', async () => {
  const fixture = await makeFixture();
  const assertion = 'signed-opaque-assertion';
  const scm = { authorize: async () => fixture.auth('stable-jti') };
  let bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]) });
  const port = await start(bridge);
  const firstPost = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion }),
  });
  const pendingCookie = cookieValue(firstPost.headers, '__Host-scm_personal_pending');
  const first = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(first.status, 303);
  assert.equal(first.headers.get('clear-site-data'), '"storage"');
  assert.equal(first.headers.get('location'), `/new?agent=${encodeURIComponent(agentId)}`);
  assert.match(first.headers.get('set-cookie'), /HttpOnly/);
  assert.match(first.headers.get('set-cookie'), /Secure/);
  assert.doesNotMatch(await first.text(), /stable-jti|signed-opaque/);
  const replay = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(replay.status, 403);
  const repost = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion }),
  });
  assert.equal(repost.status, 403);
  await bridge.close();
  bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]) });
  const port2 = await start(bridge);
  const replayAfterRestart = await fetch(`http://127.0.0.1:${port2}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(replayAfterRestart.status, 403);
  const repostAfterRestart = await fetch(`http://127.0.0.1:${port2}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion }),
  });
  assert.equal(repostAfterRestart.status, 403);
  await stop(fixture, bridge);
});

test('continuation preserves same-person storage and clears it for altered or rotated owner markers', async () => {
  const fixture = await makeFixture();
  const scm = { authorize: async () => fixture.auth(`marker-${Date.now()}`) };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const first = await completeExchange(port, 'marker-a');
  assert.equal(first.headers.get('clear-site-data'), '"storage"');
  const samePerson = await completeExchange(port, 'marker-b', fixture.config.scmFormOrigin, `${first.sessionCookie}; ${first.ownerMarkerCookie}`);
  assert.equal(samePerson.status, 303);
  assert.equal(samePerson.headers.get('clear-site-data'), null);
  const altered = await completeExchange(port, 'marker-c', fixture.config.scmFormOrigin, `${first.sessionCookie}; __Host-scm_personal_owner=altered`);
  assert.equal(altered.status, 303);
  assert.equal(altered.headers.get('clear-site-data'), '"storage"');
  await bridge.close();

  const rotatedConfig = { ...fixture.config, serviceSecret: 'r'.repeat(32) };
  const rotatedBridge = createBridgeServer(rotatedConfig, { scm, provisioner: async () => {}, manifest });
  const rotatedPort = await start(rotatedBridge);
  const rotated = await completeExchange(rotatedPort, 'marker-d', fixture.config.scmFormOrigin, first.ownerMarkerCookie);
  assert.equal(rotated.status, 303);
  assert.equal(rotated.headers.get('clear-site-data'), '"storage"');
  await stop(fixture, rotatedBridge);
});

test('continuation is a bounded one-shot and temporary authorization preserves retry', async () => {
  const fixture = await makeFixture();
  let mode = 'temporary';
  let sessionCalls = 0;
  let consumeCalls = 0;
  const scm = { authorize: async (_assertion, phase) => {
    if (phase === 'session' && ++sessionCalls > 1 && mode === 'temporary') throw Object.assign(new Error('temporary'), { status: 503 });
    return fixture.auth('continuation-retry');
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const originalConsume = bridge.store.consumeAssertion.bind(bridge.store);
  bridge.store.consumeAssertion = (...args) => { consumeCalls += 1; return originalConsume(...args); };
  const port = await start(bridge);
  const post = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: fixture.config.scmFormOrigin, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion: 'continuation-retry' }),
  });
  assert.equal(post.status, 303);
  assert.equal(post.headers.get('clear-site-data'), null);
  const pendingCookie = cookieValue(post.headers, '__Host-scm_personal_pending');
  assert.match(post.headers.get('set-cookie'), /__Host-scm_personal_pending=.*HttpOnly/);
  assert.match(post.headers.get('set-cookie'), /Max-Age=0/);
  const unavailable = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(unavailable.status, 503);
  assert.equal(unavailable.headers.get('set-cookie'), null);
  assert.equal(unavailable.headers.get('clear-site-data'), null);
  assert.equal(consumeCalls, 1);
  mode = 'ok';
  const recovered = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(recovered.status, 303);
  assert.equal(consumeCalls, 2);
  const replay = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(replay.status, 403);
  assert.equal(replay.headers.get('clear-site-data'), null);
  await stop(fixture, bridge);
});

test('continuation SID Max-Age is capped by the row and fresh authorization expiry', async () => {
  const fixture = await makeFixture();
  let sessionCalls = 0;
  const scm = { authorize: async (_assertion, phase) => {
    const response = fixture.auth('continuation-max-age');
    if (phase === 'session') {
      sessionCalls += 1;
      response.expires_at = new Date(Date.now() + (sessionCalls === 1 ? 300_000 : 20_000)).toISOString();
    }
    return response;
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'continuation-max-age');
  assert.equal(handoff.status, 303);
  const maxAge = Number(handoff.headers.get('set-cookie').match(/scm_personal_sid=[^;]+; Max-Age=(\d+)/)?.[1]);
  assert.ok(Number.isInteger(maxAge) && maxAge > 0 && maxAge <= 20, `unexpected SID Max-Age ${maxAge}`);
  await stop(fixture, bridge);
});

test('continuation rejects a fresh identity binding change before consuming or issuing SID', async () => {
  const fixture = await makeFixture();
  let sessionCalls = 0;
  const scm = { authorize: async (_assertion, phase) => {
    const response = fixture.auth('continuation-binding');
    if (phase === 'session' && ++sessionCalls > 1) response.identity.actor_id = 8;
    return response;
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const noPending = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual' });
  assert.equal(noPending.status, 403);
  assert.equal(noPending.headers.get('set-cookie'), null);
  const post = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: fixture.config.scmFormOrigin, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion: 'continuation-binding' }),
  });
  const pendingCookie = cookieValue(post.headers, '__Host-scm_personal_pending');
  const denied = await fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } });
  assert.equal(denied.status, 403);
  assert.equal(denied.headers.get('clear-site-data'), null);
  assert.equal(cookieValue(denied.headers, 'scm_personal_sid'), null);
  await stop(fixture, bridge);
});

test('continuation rejects concurrent reuse and does not bypass authorization before GET', async () => {
  const fixture = await makeFixture();
  const scm = { authorize: async () => fixture.auth('continuation-concurrent') };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const post = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: fixture.config.scmFormOrigin, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion: 'continuation-concurrent' }),
  });
  const pendingCookie = cookieValue(post.headers, '__Host-scm_personal_pending');
  const before = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: pendingCookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(before.status, 401);
  assert.equal(cookieValue(before.headers, 'scm_personal_sid'), 'scm_personal_sid=');
  const results = await Promise.all([
    fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } }),
    fetch(`http://127.0.0.1:${port}/_scm/continue`, { redirect: 'manual', headers: { Cookie: pendingCookie } }),
  ]);
  assert.deepEqual(results.map((response) => response.status).sort(), [303, 403]);
  assert.equal(results.filter((response) => response.headers.get('clear-site-data') === '"storage"').length, 1);
  const winner = results.find((response) => response.status === 303);
  const loser = results.find((response) => response.status === 403);
  const winnerCookie = cookieValue(winner.headers, 'scm_personal_sid');
  assert.match(winnerCookie, /^scm_personal_sid=[A-Za-z0-9_-]{32,128}$/);
  assert.match(loser.headers.get('set-cookie'), /__Host-scm_personal_pending=.*Max-Age=0/);
  assert.equal(loser.headers.get('clear-site-data'), null);
  const after = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: winnerCookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(after.status, 200);
  await stop(fixture, bridge);
});

test('temporary exchange authorization and provisioning failures are recoverable without consuming the grant', async () => {
  const fixture = await makeFixture();
  let mode = 'initial-temporary';
  let provisionCalls = 0;
  let consumeCalls = 0;
  const scm = { authorize: async (_assertion, phase) => {
    if (mode === 'initial-temporary' && phase === 'exchange') throw Object.assign(new Error('SCM unavailable'), { status: 503 });
    if (mode === 'post-temporary' && phase === 'session') throw Object.assign(new Error('SCM timeout'), { status: 503 });
    return fixture.auth('recoverable-grant');
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, {
    scm,
    provisioner: async () => { provisionCalls += 1; },
    manifest,
  });
  const originalConsume = bridge.store.consumeAssertion.bind(bridge.store);
  bridge.store.consumeAssertion = (...args) => { consumeCalls += 1; return originalConsume(...args); };
  const port = await start(bridge);
  const exchange = () => completeExchange(port, 'same-grant');

  const initialFailure = await exchange();
  assert.equal(initialFailure.status, 503);
  assert.equal(initialFailure.headers.get('set-cookie'), null);
  const initialHtml = await initialFailure.text();
  assert.match(initialHtml, /href="https:\/\/scm\.example\.test\/mi-agente"/);
  assert.doesNotMatch(initialHtml, /same-grant/);
  assert.equal(consumeCalls, 0);

  mode = 'post-temporary';
  const postFailure = await exchange();
  assert.equal(postFailure.status, 503);
  assert.equal(postFailure.headers.get('set-cookie'), null);
  const postHtml = await postFailure.text();
  assert.match(postHtml, /href="https:\/\/scm\.example\.test\/mi-agente"/);
  assert.equal(consumeCalls, 0);

  mode = 'ok';
  const recovered = await exchange();
  assert.equal(recovered.status, 303);
  assert.match(recovered.headers.get('set-cookie'), /scm_personal_sid=/);
  assert.equal(consumeCalls, 2);
  assert.equal(provisionCalls, 2);
  await stop(fixture, bridge);
});

test('generic provisioning timeout is 503 while actor binding divergence is a stable 403', async () => {
  const fixture = await makeFixture();
  let provisionMode = 'timeout';
  let mismatchOnSession = false;
  const scm = { authorize: async (_assertion, phase) => {
    const response = fixture.auth(`binding-${phase}`);
    response.identity.actor_id = mismatchOnSession && phase === 'session' ? 8 : 7;
    return response;
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, {
    scm,
    provisioner: async () => {
      if (provisionMode === 'timeout') throw new Error('native admin timeout');
    },
    manifest,
  });
  let consumeCalls = 0;
  const originalConsume = bridge.store.consumeAssertion.bind(bridge.store);
  bridge.store.consumeAssertion = (...args) => { consumeCalls += 1; return originalConsume(...args); };
  const port = await start(bridge);
  const exchange = () => completeExchange(port, 'binding-grant');
  const provisioningFailure = await exchange();
  assert.equal(provisioningFailure.status, 503);
  const provisioningHtml = await provisioningFailure.text();
  assert.match(provisioningHtml, /href="https:\/\/scm\.example\.test\/mi-agente"/);
  provisionMode = 'ok';
  mismatchOnSession = true;
  const mismatch = await exchange();
  assert.equal(mismatch.status, 403);
  assert.deepEqual(await mismatch.json(), { error: 'provisioning_denied' });
  assert.equal(consumeCalls, 0);
  await stop(fixture, bridge);
});

test('temporary download reauthorization keeps the same SID recoverable without bytes or idle touch', async () => {
  const fixture = await makeFixture();
  let failNextReauthorize = false;
  let failArtifactCallback = true;
  const scm = { authorize: async () => {
    if (failNextReauthorize) {
      failNextReauthorize = false;
      throw Object.assign(new Error('SCM timeout'), { status: 503 });
    }
    return fixture.auth('download-retry-jti');
  } };
  const downloadStore = new DownloadStore();
  const artifactBroker = {
    serveHandle: async ({ reauthorize, res }) => {
      if (failArtifactCallback) {
        failArtifactCallback = false;
        failNextReauthorize = true;
      }
      const fresh = await reauthorize();
      if (fresh.kind === 'temporary') throw new DownloadError('scm_unavailable', 503);
      res.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="x.pdf"' });
      res.end('PDF');
    },
  };
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: null, downloadStore, artifactBroker });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'download-retry', fixture.config.scmFormOrigin);
  const cookie = handoff.sessionCookie;
  const sid = decodeURIComponent(cookie.split('=')[1]);
  const handle = downloadStore.issue({ sidHash: hash(sid), agentId, sessionKey: `agent:${agentId}:s`, artifactId: 'artifact-pdf', summary: { id: 'artifact-pdf', sessionKey: `agent:${agentId}:s`, mimeType: 'application/pdf', sizeBytes: 3 }, expiresAt: Date.now() + 60_000 });
  const touches = [];
  const touchSession = bridge.store.touchSession.bind(bridge.store);
  bridge.store.touchSession = (...args) => { touches.push(args); return touchSession(...args); };
  const temporary = await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${handle}`, { headers: { Cookie: cookie } });
  assert.equal(temporary.status, 503);
  assert.deepEqual(await temporary.json(), { error: 'scm_unavailable' });
  assert.equal(touches.length, 1);
  const recovered = await fetch(`http://127.0.0.1:${port}/api/artifacts/download/scm/${handle}`, { headers: { Cookie: cookie } });
  assert.equal(recovered.status, 200);
  assert.equal(await recovered.text(), 'PDF');
  assert.equal(touches.length, 3);
  await stop(fixture, bridge);
});

test('temporary artifact RPC reauthorization returns a retryable error without forwarding upstream', async () => {
  const fixture = await makeFixture();
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  let upstreamMessages = 0;
  nativeWs.on('connection', (socket) => socket.on('message', () => { upstreamMessages += 1; }));
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  let failNextReauthorize = false;
  const scm = { authorize: async () => {
    if (failNextReauthorize) {
      failNextReauthorize = false;
      throw Object.assign(new Error('SCM timeout'), { status: 503 });
    }
    return fixture.auth('rpc-download-retry-jti');
  } };
  const artifactBroker = {
    downloadRpc: async ({ reauthorize }) => {
      failNextReauthorize = true;
      const fresh = await reauthorize();
      if (fresh.kind === 'temporary') throw new DownloadError('scm_unavailable', 503);
      throw new Error('unexpected authorization recovery');
    },
  };
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: null, artifactBroker });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'rpc-download-retry', fixture.config.scmFormOrigin);
  const cookie = handoff.sessionCookie;
  const client = new WebSocket(`ws://127.0.0.1:${port}/chat`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  await new Promise((resolve, reject) => { client.once('open', resolve); client.once('error', reject); });
  const response = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  client.send(JSON.stringify({ type: 'req', id: 'download-1', method: 'artifacts.download', params: { sessionKey: `agent:${agentId}:s`, artifactId: 'artifact-pdf' } }));
  const result = await response;
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'scm_unavailable');
  assert.equal(result.error.retryable, true);
  assert.equal(result.error.message, 'SCM no responde temporalmente. Reintenta la descarga.');
  artifactBroker.downloadRpc = async () => { throw new Error('private native detail'); };
  const deniedResponse = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  client.send(JSON.stringify({ type: 'req', id: 'download-2', method: 'artifacts.download', params: { sessionKey: `agent:${agentId}:s`, artifactId: 'artifact-pdf' } }));
  const denied = await deniedResponse;
  assert.equal(denied.ok, false);
  assert.deepEqual(denied.error, { code: 'artifact_download_denied', message: 'No se pudo descargar este archivo.' });
  assert.doesNotMatch(JSON.stringify(denied), /private native detail/);
  assert.equal(upstreamMessages, 0);
  client.close();
  await bridge.close();
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('revocation is checked before every websocket data frame and closes 4403', async () => {
  const fixture = await makeFixture();
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  const nativePort = nativeWs.address().port;
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativePort}`;
  let allowed = true;
  let forwarded = 0;
  nativeWs.on('connection', (socket) => socket.on('message', () => { forwarded += 1; socket.send('echo'); }));
  const scm = { authorize: async (_assertion, phase) => {
    if (phase === 'session' && !allowed) throw Object.assign(new Error('revoked'), { status: 403 });
    return fixture.auth('ws-jti');
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const client = new WebSocket(`ws://127.0.0.1:${port}/chat`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  await new Promise((resolve, reject) => { client.once('open', resolve); client.once('error', reject); });
  client.send('first');
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(forwarded, 1);
  allowed = false;
  const closed = new Promise((resolve) => client.once('close', (code) => resolve(code)));
  client.send('second');
  assert.equal(await closed, 4403);
  assert.equal(forwarded, 1);
  await bridge.close();
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('temporary SCM authorization failure keeps the SID and does not touch or forward HTTP', async () => {
  const fixture = await makeFixture();
  let nativeRequests = 0;
  await new Promise((resolve) => fixture.native.close(resolve));
  fixture.native = http.createServer((_req, res) => { nativeRequests += 1; res.writeHead(200); res.end('native'); });
  await new Promise((resolve) => fixture.native.listen(0, '127.0.0.1', resolve));
  fixture.config.nativeHttpOrigin = `http://127.0.0.1:${fixture.native.address().port}`;
  let mode = 'ok';
  const scm = { authorize: async (assertion) => {
    if (mode === 'temporary') throw Object.assign(new Error('SCM unavailable'), { status: 503 });
    if (mode === 'deny') throw Object.assign(new Error('revoked'), { status: 403 });
    if (mode === 'deny401') throw Object.assign(new Error('expired'), { status: 401 });
    return fixture.auth(`temporary-http-${assertion}`);
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const touches = [];
  const deletes = [];
  const touchSession = bridge.store.touchSession.bind(bridge.store);
  const deleteSession = bridge.store.deleteSession.bind(bridge.store);
  bridge.store.touchSession = (...args) => { touches.push(args); return touchSession(...args); };
  bridge.store.deleteSession = (...args) => { deletes.push(args); return deleteSession(...args); };
  mode = 'temporary';
  const unavailable = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(unavailable.status, 503);
  assert.match(await unavailable.text(), /SCM no está disponible/);
  assert.equal(unavailable.headers.get('set-cookie'), null);
  assert.equal(nativeRequests, 0);
  assert.equal(touches.length, 0);
  assert.equal(deletes.length, 0);
  const unavailableRoot = await fetch(`http://127.0.0.1:${port}/`, { redirect: 'manual', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(unavailableRoot.status, 503);
  assert.equal(unavailableRoot.headers.get('location'), null);
  assert.equal(unavailableRoot.headers.get('set-cookie'), null);
  const unavailableChat = await fetch(`http://127.0.0.1:${port}/chat/${agentId}`, { redirect: 'manual', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(unavailableChat.status, 503);
  assert.equal(unavailableChat.headers.get('location'), null);
  assert.equal(unavailableChat.headers.get('set-cookie'), null);
  mode = 'ok';
  const recovered = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(recovered.status, 200);
  assert.equal(nativeRequests, 1);
  assert.equal(touches.length, 1);
  assert.equal(deletes.length, 0);
  mode = 'deny';
  const revoked = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(revoked.status, 401);
  assert.match(revoked.headers.get('set-cookie'), /Max-Age=0/);
  assert.equal(deletes.length, 1);
  mode = 'ok';
  const secondHandoff = await completeExchange(port, 'y');
  const secondCookie = secondHandoff.sessionCookie;
  mode = 'deny401';
  const expired = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: secondCookie, Origin: fixture.config.allowedOrigin } });
  assert.equal(expired.status, 401);
  assert.match(expired.headers.get('set-cookie'), /Max-Age=0/);
  assert.equal(deletes.length, 2);
  await stop(fixture, bridge);
});

test('temporary SCM authorization failure closes websocket with retry code and the same SID reconnects', async () => {
  const fixture = await makeFixture();
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  let mode = 'ok';
  let forwarded = 0;
  nativeWs.on('connection', (socket) => socket.on('message', () => { forwarded += 1; socket.send('echo'); }));
  const scm = { authorize: async () => {
    if (mode === 'temporary') throw Object.assign(new Error('SCM timeout'), { status: 503 });
    return fixture.auth('temporary-ws-jti');
  } };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const connect = () => new WebSocket(`ws://127.0.0.1:${port}/chat`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  const first = connect();
  await new Promise((resolve, reject) => { first.once('open', resolve); first.once('error', reject); });
  mode = 'temporary';
  const firstClosed = new Promise((resolve) => first.once('close', (code) => resolve(code)));
  first.send('not-forwarded');
  assert.equal(await firstClosed, 1013);
  assert.equal(forwarded, 0);
  mode = 'ok';
  const second = connect();
  await new Promise((resolve, reject) => { second.once('open', resolve); second.once('error', reject); });
  const echoed = new Promise((resolve) => second.once('message', (message) => resolve(message.toString())));
  second.send('forwarded-after-retry');
  assert.equal(await echoed, 'echo');
  assert.equal(forwarded, 1);
  second.close();
  await bridge.close();
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('expired websocket SID receives local native pairing rejection without upstream access', async () => {
  const fixture = await makeFixture();
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  let upstreamConnections = 0;
  nativeWs.on('connection', () => { upstreamConnections += 1; });
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  const bridge = createBridgeServer(fixture.config, {
    scm: { authorize: async () => fixture.auth('expired-ws-jti') },
    provisioner: async () => {},
    manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]),
  });
  const port = await start(bridge);
  const client = new WebSocket(`ws://127.0.0.1:${port}/chat`, { headers: { Origin: fixture.config.allowedOrigin } });
  const challenge = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  await new Promise((resolve, reject) => { client.once('open', resolve); client.once('error', reject); });
  const challengeFrame = await challenge;
  assert.equal(challengeFrame.event, 'connect.challenge');
  assert.equal(typeof challengeFrame.nonce, 'string');
  assert.equal(typeof challengeFrame.ts, 'number');
  const rejection = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  const closed = new Promise((resolve) => client.once('close', (code) => resolve(code)));
  client.send(JSON.stringify({ type: 'req', id: 'pair-1', method: 'connect', params: {} }));
  const response = await rejection;
  assert.deepEqual(response, {
    type: 'res', id: 'pair-1', ok: false,
    error: { code: 'NOT_PAIRED', message: 'Tu sesión de SCM venció. Vuelve a SCM para continuar.', retryable: false, details: { code: 'AUTH_VERIFIED_USER_REQUIRED' } },
  });
  assert.equal(await closed, 1008);
  assert.equal(upstreamConnections, 0);
  const rejectedHandshake = (url, origin) => new Promise((resolve) => {
    const rejected = new WebSocket(url, { headers: { Origin: origin } });
    let settled = false;
    const finish = () => { if (settled) return; settled = true; clearTimeout(timer); rejected.terminate(); resolve(true); };
    const timer = setTimeout(() => { if (!settled) { settled = true; rejected.terminate(); resolve(false); } }, 500);
    rejected.once('error', finish);
    rejected.once('close', finish);
  });
  assert.equal(await rejectedHandshake(`ws://127.0.0.1:${port}/chat?extra=1`, fixture.config.allowedOrigin), true);
  assert.equal(await rejectedHandshake(`ws://127.0.0.1:${port}/chat`, 'https://evil.example.test'), true);
  assert.equal(upstreamConnections, 0);
  await bridge.close();
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('invalid protected HTTP entry redirects only root and config routes to fixed SCM form URL', async () => {
  const fixture = await makeFixture();
  const bridge = createBridgeServer(fixture.config, {
    scm: { authorize: async () => fixture.auth('expired-http-jti') },
    provisioner: async () => {},
    manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]),
  });
  const port = await start(bridge);
  const get = (pathname) => fetch(`http://127.0.0.1:${port}${pathname}`, { redirect: 'manual' });
  for (const pathname of ['/?returnTo=https%3A%2F%2Fevil.example', '/control-ui-config.json?clientURL=https%3A%2F%2Fevil.example', '/__openclaw/control-ui-config.json']) {
    const response = await get(pathname);
    assert.equal(response.status, 303);
    assert.equal(response.headers.get('location'), 'https://scm.example.test/mi-agente');
  }
  for (const pathname of ['/new', '/chat', `/chat/${agentId}?returnTo=https%3A%2F%2Fevil.example`]) {
    const response = await get(pathname);
    assert.equal(response.status, 303);
    assert.equal(response.headers.get('location'), 'https://scm.example.test/mi-agente');
  }
  const head = await fetch(`http://127.0.0.1:${port}/`, { method: 'HEAD', redirect: 'manual' });
  assert.equal(head.status, 303);
  assert.equal(head.headers.get('location'), 'https://scm.example.test/mi-agente');
  const other = await get('/new');
  assert.equal(other.status, 303);
  assert.equal(other.headers.get('location'), 'https://scm.example.test/mi-agente');
  const api = await get('/api/openapi.json');
  assert.equal(api.status, 401);
  assert.match(api.headers.get('set-cookie'), /Max-Age=0/);
  fixture.config.scmFormOrigin = null;
  const withoutOrigin = await get('/');
  assert.equal(withoutOrigin.status, 401);
  await stop(fixture, bridge);
});

test('spoofed identity and scopes are overwritten before native proxying', async () => {
  const fixture = await makeFixture();
  const seen = {};
  await new Promise((resolve) => fixture.native.close(resolve));
  fixture.native = http.createServer((req, res) => { Object.assign(seen, req.headers); res.writeHead(200); res.end('ok'); });
  await new Promise((resolve) => fixture.native.listen(0, '127.0.0.1', resolve));
  fixture.config.nativeHttpOrigin = `http://127.0.0.1:${fixture.native.address().port}`;
  const scm = { authorize: async () => fixture.auth('headers-jti') };
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]) });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const response = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { headers: { Cookie: cookie, Authorization: 'Bearer evil', 'X-SCM-OpenClaw-User': 'scm:evil', 'X-OpenClaw-Scopes': 'operator.admin' } });
  assert.equal(response.status, 200);
  assert.equal(seen.authorization, undefined);
  assert.equal(seen['x-scm-openclaw-user'], identity);
  assert.equal(seen['x-openclaw-scopes'], 'operator.sessions.read,operator.sessions.write');
  const deniedMutation = await fetch(`http://127.0.0.1:${port}/mutate`, {
    method: 'POST', headers: { Cookie: cookie, Origin: 'https://evil.example.test', 'Content-Type': 'application/json' }, body: '{}',
  });
  assert.equal(deniedMutation.status, 403);
  await stop(fixture, bridge);
});

test('filters native agent metadata and denies explicit foreign agent targets', async () => {
  const fixture = await makeFixture();
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  let forwarded = 0;
  nativeWs.on('connection', (socket) => socket.on('message', (raw) => {
    forwarded += 1;
    const request = JSON.parse(raw.toString());
    if (request.method === 'connect') socket.send(JSON.stringify({ type: 'res', ok: true, id: request.id, payload: {
      snapshot: { sessionDefaults: { defaultAgentId: 'scm-personal', mainKey: 'main', mainSessionKey: 'agent:scm-personal:main', ownership: 'explicit', selectionRequired: false } },
      auth: { scopes: ['operator.sessions.read', 'operator.sessions.write'] },
    } }));
    if (request.method === 'agents.list') socket.send(JSON.stringify({ type: 'res', id: request.id, payload: {
      agents: [{ id: agentId }, { id: 'scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }],
    } }));
  }));
  const scm = { authorize: async () => fixture.auth('filter-jti') };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const client = new WebSocket(`ws://127.0.0.1:${port}/chat`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  await new Promise((resolve, reject) => { client.once('open', resolve); client.once('error', reject); });
  const metadata = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  client.send(JSON.stringify({ type: 'req', id: '1', method: 'agents.list', params: {} }));
  const response = await metadata;
  assert.deepEqual(response.payload.agents, [{ id: agentId }]);
  assert.equal(response.payload.defaultId, agentId);
  assert.equal(response.payload.selectionRequired, false);
  const hello = new Promise((resolve) => client.once('message', (message) => resolve(JSON.parse(message.toString()))));
  client.send(JSON.stringify({ type: 'req', id: 'hello', method: 'connect', params: {} }));
  const snapshot = await hello;
  assert.equal(snapshot.payload.snapshot.sessionDefaults.defaultAgentId, agentId);
  assert.equal(snapshot.payload.snapshot.sessionDefaults.mainSessionKey, `agent:${agentId}:main`);
  assert.equal(snapshot.payload.snapshot.sessionDefaults.ownership, 'explicit');
  assert.deepEqual(snapshot.payload.auth.scopes, ['operator.sessions.read', 'operator.sessions.write']);
  const closed = new Promise((resolve) => client.once('close', (code) => resolve(code)));
  client.send(JSON.stringify({ type: 'req', id: '2', method: 'sessions.list', params: { agentId: 'scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' } }));
  assert.equal(await closed, 4403);
  assert.equal(forwarded, 2);
  await bridge.close();
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('HTTP proxy allowlist limits SPA/assets/media to the bound agent', async () => {
  const fixture = await makeFixture();
  const scm = { authorize: async () => fixture.auth('routes-jti') };
  const manifest = new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]);
  const bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest });
  const port = await start(bridge);
  const handoff = await completeExchange(port, 'x');
  const cookie = handoff.sessionCookie;
  const get = (path) => fetch(`http://127.0.0.1:${port}${path}`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  const head = (path) => fetch(`http://127.0.0.1:${port}${path}`, { method: 'HEAD', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal((await get('/')).status, 200);
  assert.equal((await get('/?openclaw_mount_recovery=1793313894362')).status, 200);
  assert.equal((await get(`/new?agent=${agentId}`)).status, 200);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await head(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await head(`/chat?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await get(`/chat/${agentId}?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  const historySessionKey = `agent:${agentId}:dashboard:123e4567-e89b-12d3-a456-426614174000`;
  const historyQuery = `__openclawSessionKey=${encodeURIComponent(historySessionKey)}&__openclawSessionFacePreference=1&__openclawComposerFocus=1&dashboard=expanded`;
  assert.equal((await get(`/chat/${agentId}?${historyQuery}`)).status, 200);
  assert.equal((await head(`/chat/${agentId}?${historyQuery}`)).status, 200);
  assert.equal((await get(`/chat/${agentId}?${historyQuery}&sessionKey=${encodeURIComponent(historySessionKey)}`)).status, 200);
  assert.equal((await get(`/new?agent=${agentId}&agent=${agentId}`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362&openclaw_mount_recovery=1793313894363`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362&openclaw%5fmount_recovery=1793313894363`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=%00`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=%01`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=not-a-timestamp`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=123`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=17933138943620`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&unknown=1`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&catalog=default`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&group=personal`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&model=provider%2Fmodel`)).status, 404);
  assert.equal((await get('/new?agent=scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&openclaw_mount_recovery=1793313894362')).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&sessionKey=agent:scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:s&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=agent:scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:dashboard:s&dashboard=expanded`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=${encodeURIComponent(historySessionKey)}&__openclawSessionKey=${encodeURIComponent(historySessionKey)}`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=${encodeURIComponent(historySessionKey)}&%5f%5fopenclawSessionKey=${encodeURIComponent(historySessionKey)}`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?sessionKey=${encodeURIComponent(historySessionKey)}&__openclawSessionKey=agent:${agentId}:dashboard:other&dashboard=expanded`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=${encodeURIComponent(historySessionKey)}&__openclawSessionFacePreference=0&dashboard=expanded`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=${encodeURIComponent(historySessionKey)}&__openclawComposerFocus=true&dashboard=expanded`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?__openclawSessionKey=${encodeURIComponent(historySessionKey)}&dashboard=compact`)).status, 404);
  assert.equal((await get(`/chat/${agentId}?${historyQuery}&unknown=1`)).status, 404);
  assert.equal((await get(`/chat?agent=scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/chat/scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/assets/main.js?openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/assets/main.js?${historyQuery}`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&__openclawSessionKey=${encodeURIComponent(historySessionKey)}`)).status, 404);
  assert.equal((await get('/control-ui-config.json?openclaw_mount_recovery=1793313894362')).status, 404);
  const nativeBuild = '2026.9.8-fc23bc864e45-2026-10-02T18-21-06.010Z-987738c52d34b655508e269302eef45d770c63ff77a096e6ee6ee2016233811d';
  assert.equal((await get(`/fonts/instrument-sans.css?v=${nativeBuild}`)).status, 200);
  assert.equal((await head(`/fonts/instrument-sans.css?v=${nativeBuild}`)).status, 200);
  assert.equal((await get(`/fonts/jetbrains-mono.css?v=${nativeBuild}`)).status, 200);
  assert.equal((await get(`/provider-icons/codex.png?v=${nativeBuild}`)).status, 200);
  assert.equal((await get(`/assets/main.js?v=${nativeBuild}`)).status, 200);
  assert.equal((await get(`/assets/main.js?v=${'x'.repeat(160)}`)).status, 200);
  assert.equal((await get(`/assets/main.js?v=a%2Fb`)).status, 404);
  assert.equal((await get(`/assets/main.js?v=%C3%A9`)).status, 404);
  assert.equal((await get(`/new?agent=${agentId}&v=${nativeBuild}`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&v=${nativeBuild}`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=one&v=two`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?%76=one&v=two`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=%00`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=${'x'.repeat(161)}`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=${nativeBuild}&unknown=1`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=${nativeBuild}&agent=${agentId}`)).status, 404);
  assert.equal((await get(`/fonts/instrument-sans.css?v=${nativeBuild}&agent=scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`)).status, 404);
  assert.equal((await get(`/api/${nativeBuild}.css?v=${nativeBuild}`)).status, 404);
  assert.equal((await get(`/sw.js?v=${nativeBuild}`)).status, 200);
  assert.equal((await head(`/sw.js?v=${nativeBuild}`)).status, 200);
  assert.equal((await get('/sw.js')).status, 200);
  assert.equal((await get(`/sw.js?v=one&v=two`)).status, 404);
  assert.equal((await get(`/sw.js?%76=one&v=two`)).status, 404);
  assert.equal((await get(`/sw.js?v=${nativeBuild}&unknown=1`)).status, 404);
  assert.equal((await get(`/sw.js?v=${nativeBuild}&agent=${agentId}`)).status, 404);
  assert.equal((await get('/sw.js?v=a%2Fb')).status, 404);
  assert.equal((await get('/sw.js?v=%C3%A9')).status, 404);
  assert.equal((await get('/sw.js?v=')).status, 404);
  assert.equal((await get(`/sw.js?v=${'x'.repeat(161)}`)).status, 404);
  assert.equal((await get('/sw.js?openclaw_mount_recovery=1793313894362')).status, 404);
  assert.equal((await get(`/fonts/%2e%2e/instrument-sans.css?v=${nativeBuild}`)).status, 404);
  assert.equal((await get('/assets/main.js')).status, 200);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&meta=1`)).status, 200);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&agentId=${agentId}&sessionKey=agent:${agentId}:s`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&sessionKey=agent:${agentId}:t`)).status, 404);
  assert.equal((await get('/api/openapi.json')).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&sessionKey=agent:scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:s`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&unknown=1`)).status, 404);
  assert.equal((await get('/assets/%2e%2e/secret.js')).status, 404);
  const post = await fetch(`http://127.0.0.1:${port}/assets/main.js`, { method: 'POST', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin }, body: 'x' });
  assert.equal(post.status, 404);
  const postNew = await fetch(`http://127.0.0.1:${port}/new?agent=${agentId}&openclaw_mount_recovery=1793313894362`, { method: 'POST', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin }, body: 'x' });
  assert.equal(postNew.status, 404);
  const postStatic = await fetch(`http://127.0.0.1:${port}/fonts/instrument-sans.css?v=${nativeBuild}`, { method: 'POST', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin }, body: 'x' });
  assert.equal(postStatic.status, 404);
  await stop(fixture, bridge);
});

test('native bootstrap uses trusted connect, exact profile email and manifest role', async () => {
  const fixture = await makeFixture();
  const passwordFile = path.join(fixture.root, 'admin-password');
  writeFileSync(passwordFile, 'gateway-password');
  const nativeWs = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => nativeWs.once('listening', resolve));
  const observed = [];
  let agentName;
  let profileDisplayName = 'Nombre anterior';
  nativeWs.on('connection', (socket, request) => {
    observed.push({ headers: request.headers });
    socket.send(JSON.stringify({ event: 'connect.challenge' }));
    socket.on('message', (raw) => {
      const frame = JSON.parse(raw.toString());
      if (frame.method === 'connect') {
        observed[observed.length - 1].connect = frame.params;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      } else if (frame.method === 'users.list') {
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { profiles: [{ id: 'profile-a', emails: [identity], role: 'deny_all', displayName: profileDisplayName }] } }));
      } else if (frame.method === 'config.get') {
        const nativeConfig = {
          gateway: {
            bind: 'loopback',
            reload: { mode: 'hybrid' },
            auth: {
              mode: 'trusted-proxy',
              trustedProxy: {
                userHeader: 'x-scm-openclaw-user',
                requiredHeaders: ['x-forwarded-proto', 'x-forwarded-host'],
              },
              identityScopes: { [identity]: ['operator.sessions.read', 'operator.sessions.write'] },
            },
            trustedProxies: ['127.0.0.1'],
            roles: {
              default: 'deny_all',
              definitions: {
                deny_all: { sessions: { others: 'none' }, agents: [], scopes: [] },
                scm_person_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: {
                  sessions: { others: 'none' }, agents: [agentId], scopes: ['operator.sessions.read', 'operator.sessions.write'],
                },
              },
            },
          },
          agents: { entries: { [agentId]: { name: agentName, workspace: '/tmp/a', agentDir: '/state/agents/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/agent' } } },
          tools: { allow: ['enva_corpus_read', 'enva_scm_sql_read'], elevated: { enabled: false } },
        };
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { config: nativeConfig } }));
      } else if (frame.method === 'users.setRole') {
        observed[observed.length - 1].setRole = frame.params;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      } else if (frame.method === 'users.setDisplayName') {
        observed[observed.length - 1].setDisplayName = frame.params;
        profileDisplayName = frame.params.displayName;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { profile: { displayName: profileDisplayName } } }));
      } else if (frame.method === 'agents.update') {
        observed[observed.length - 1].agentUpdate = frame.params;
        agentName = frame.params.name;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { agent: { id: frame.params.agentId, name: agentName } } }));
      }
    });
  });
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  fixture.config.nativeAdminPasswordFile = passwordFile;
  const bootstrapAuthorization = fixture.auth('bootstrap-jti');
  bootstrapAuthorization.identity.display_name = 'Operador A';
  await provisionNativeIdentity({ config: fixture.config, authorization: bootstrapAuthorization, clientAddress: '192.0.2.10', manifest: new Map([[identity, { identity, agent_id: agentId, role: 'scm_person_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }]]) });
  assert.equal(observed.length, 2);
  assert.equal(observed[0].connect.client.id, 'openclaw-control-ui');
  assert.equal(observed[0].connect.client.mode, 'webchat');
  assert.equal(observed[0].headers.origin, fixture.config.allowedOrigin);
  assert.equal(observed[0].headers['x-forwarded-for'], '192.0.2.10');
  assert.equal(observed[1].connect.client.id, 'gateway-client');
  assert.equal(observed[1].connect.auth.password, 'gateway-password');
  assert.equal(observed[1].headers.origin, undefined);
  assert.deepEqual(observed[1].setRole, { profileId: 'profile-a', role: 'scm_person_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' });
  assert.deepEqual(observed[1].setDisplayName, { profileId: 'profile-a', displayName: 'Operador A' });
  assert.deepEqual(observed[1].agentUpdate, { agentId, name: 'Mi agente' });
  await new Promise((resolve) => nativeWs.close(resolve));
  await new Promise((resolve) => fixture.native.close(resolve));
  rmSync(fixture.root, { recursive: true, force: true });
});

test('CLI entrypoint binds configured TCP host/port and serves health', async () => {
  const root = mkdtempSync(path.join(tmpdir(), 'scm-bridge-cli-'));
  const manifestPath = path.join(root, 'manifest.json');
  const passwordPath = path.join(root, 'admin-password');
  writeFileSync(manifestPath, JSON.stringify({ entries: [] }));
  writeFileSync(passwordPath, 'password');
  const port = 19000 + Math.floor(Math.random() * 1000);
  const child = spawn(process.execPath, ['src/server.js'], {
    cwd: path.resolve('.'),
    env: {
      ...process.env,
      TESTING: 'true',
      SCM_BRIDGE_HOST: '127.0.0.1',
      SCM_BRIDGE_PORT: String(port),
      SCM_PERSONAL_AGENT_SERVICE_SECRET: 's'.repeat(32),
      SCM_API_URL: 'http://127.0.0.1:5999/api',
      SCM_ALLOWED_ORIGIN: 'http://127.0.0.1:5182',
      SCM_SCM_ORIGIN: 'http://127.0.0.1:5173',
      SCM_NATIVE_HTTP_ORIGIN: 'http://127.0.0.1:18789',
      SCM_NATIVE_WS_URL: 'ws://127.0.0.1:18789',
      SCM_NATIVE_MANIFEST: manifestPath,
      SCM_NATIVE_ADMIN_PASSWORD_FILE: passwordPath,
      SCM_BRIDGE_DB_PATH: path.join(root, 'bridge.sqlite'),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let childError = '';
  child.stderr.on('data', (chunk) => { childError += chunk.toString(); });
  try {
    let response;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      try { response = await fetch(`http://127.0.0.1:${port}/healthz`); if (response.ok) break; } catch { /* wait for bind */ }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(response?.status, 200, childError);
  } finally {
    child.kill();
    await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 1000))]);
    rmSync(root, { recursive: true, force: true });
  }
});
