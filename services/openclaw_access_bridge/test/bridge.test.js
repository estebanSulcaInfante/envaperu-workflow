import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { WebSocket, WebSocketServer } from 'ws';
import { createBridgeServer } from '../src/server.js';
import { provisionNativeIdentity } from '../src/native_admin.js';

const identity = 'scm+aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@internal.envaperu.invalid';
const agentId = 'scm-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

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

test('exchange is same-origin form-only, opaque-cookie and replay-safe across restart', async () => {
  const fixture = await makeFixture();
  const assertion = 'signed-opaque-assertion';
  const scm = { authorize: async () => fixture.auth('stable-jti') };
  let bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]) });
  const port = await start(bridge);
  const exchange = () => fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ assertion }),
  });
  const first = await exchange();
  assert.equal(first.status, 303);
  assert.equal(first.headers.get('clear-site-data'), '"storage"');
  assert.equal(first.headers.get('location'), `/new?agent=${encodeURIComponent(agentId)}`);
  assert.match(first.headers.get('set-cookie'), /HttpOnly/);
  assert.match(first.headers.get('set-cookie'), /Secure/);
  assert.doesNotMatch(await first.text(), /stable-jti|signed-opaque/);
  const replay = await exchange();
  assert.equal(replay.status, 403);
  await bridge.close();
  bridge = createBridgeServer(fixture.config, { scm, provisioner: async () => {}, manifest: new Map([[identity, { identity, agent_id: agentId, actor_id: 7, role: 'person_a' }]]) });
  const port2 = await start(bridge);
  const replayAfterRestart = await fetch(`http://127.0.0.1:${port2}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion }),
  });
  assert.equal(replayAfterRestart.status, 403);
  await stop(fixture, bridge);
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
  const handoff = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, {
    method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion: 'x' }),
  });
  const cookie = handoff.headers.get('set-cookie').split(';')[0];
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
  const handoff = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, { method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion: 'x' }) });
  const cookie = handoff.headers.get('set-cookie').split(';')[0];
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
  const handoff = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, { method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion: 'x' }) });
  const cookie = handoff.headers.get('set-cookie').split(';')[0];
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
  const handoff = await fetch(`http://127.0.0.1:${port}/_scm/exchange`, { method: 'POST', redirect: 'manual', headers: { Origin: 'https://scm.example.test', 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ assertion: 'x' }) });
  const cookie = handoff.headers.get('set-cookie').split(';')[0];
  const get = (path) => fetch(`http://127.0.0.1:${port}${path}`, { headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  const head = (path) => fetch(`http://127.0.0.1:${port}${path}`, { method: 'HEAD', headers: { Cookie: cookie, Origin: fixture.config.allowedOrigin } });
  assert.equal((await get('/')).status, 200);
  assert.equal((await get('/?openclaw_mount_recovery=1793313894362')).status, 200);
  assert.equal((await get(`/new?agent=${agentId}`)).status, 200);
  assert.equal((await get(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await head(`/new?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await head(`/chat?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
  assert.equal((await get(`/chat/${agentId}?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 200);
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
  assert.equal((await get(`/chat?agent=scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/chat/scm-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb?agent=${agentId}&openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/assets/main.js?openclaw_mount_recovery=1793313894362`)).status, 404);
  assert.equal((await get(`/__openclaw__/assistant-media?agentId=${agentId}&sessionKey=agent:${agentId}:s&openclaw_mount_recovery=1793313894362`)).status, 404);
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
  assert.equal((await get(`/sw.js?v=${nativeBuild}`)).status, 404);
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
  nativeWs.on('connection', (socket, request) => {
    observed.push({ headers: request.headers });
    socket.send(JSON.stringify({ event: 'connect.challenge' }));
    socket.on('message', (raw) => {
      const frame = JSON.parse(raw.toString());
      if (frame.method === 'connect') {
        observed[observed.length - 1].connect = frame.params;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      } else if (frame.method === 'users.list') {
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { profiles: [{ id: 'profile-a', emails: [identity], role: 'deny_all' }] } }));
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
          agents: { entries: { [agentId]: { workspace: '/tmp/a', agentDir: '/state/agents/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/agent' } } },
          tools: { allow: ['enva_corpus_read', 'enva_scm_sql_read'], elevated: { enabled: false } },
        };
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { config: nativeConfig } }));
      } else if (frame.method === 'users.setRole') {
        observed[observed.length - 1].setRole = frame.params;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      }
    });
  });
  fixture.config.nativeWsUrl = `ws://127.0.0.1:${nativeWs.address().port}`;
  fixture.config.nativeAdminPasswordFile = passwordFile;
  await provisionNativeIdentity({ config: fixture.config, authorization: fixture.auth('bootstrap-jti'), clientAddress: '192.0.2.10', manifest: new Map([[identity, { identity, agent_id: agentId, role: 'scm_person_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }]]) });
  assert.equal(observed.length, 2);
  assert.equal(observed[0].connect.client.id, 'openclaw-control-ui');
  assert.equal(observed[0].connect.client.mode, 'webchat');
  assert.equal(observed[0].headers.origin, fixture.config.allowedOrigin);
  assert.equal(observed[0].headers['x-forwarded-for'], '192.0.2.10');
  assert.equal(observed[1].connect.client.id, 'gateway-client');
  assert.equal(observed[1].connect.auth.password, 'gateway-password');
  assert.equal(observed[1].headers.origin, undefined);
  assert.deepEqual(observed[1].setRole, { profileId: 'profile-a', role: 'scm_person_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' });
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
