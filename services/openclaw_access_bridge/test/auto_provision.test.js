import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { WebSocketServer } from 'ws';
import { provisionNativeIdentity } from '../src/native_admin.js';

const makeIdentity = (letter) => `scm+${letter.repeat(8)}-${letter.repeat(4)}-${letter.repeat(4)}-${letter.repeat(4)}-${letter.repeat(12)}@internal.envaperu.invalid`;
const identityA = makeIdentity('a');
const identityB = makeIdentity('b');
const identityC = makeIdentity('c');
const agentA = `scm-${'a'.repeat(32)}`;
const agentB = `scm-${'b'.repeat(32)}`;
const agentC = `scm-${'c'.repeat(32)}`;

test('auto provision is idempotent for two identities and retries an optimistic config conflict', async () => {
  const root = mkdtempSync(path.join(tmpdir(), 'scm-auto-'));
  const passwordFile = path.join(root, 'admin-password');
  writeFileSync(passwordFile, 'gateway-password');
  const native = new WebSocketServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve) => native.once('listening', resolve));
  const profiles = new Map([[identityA, { id: 'a', emails: [identityA], role: 'deny_all' }], [identityB, { id: 'b', emails: [identityB], role: 'deny_all' }], [identityC, { id: 'c', emails: [identityC], role: 'deny_all' }]]);
  const state = { hash: 'h0', conflict: true, creates: 0, patches: 0, updates: 0, displayUpdates: 0, failUpdate: false, failDisplayName: false, config: {
    gateway: { bind: 'loopback', reload: { mode: 'hybrid' }, auth: { mode: 'trusted-proxy', trustedProxy: { userHeader: 'x-scm-openclaw-user', requiredHeaders: ['x-forwarded-proto', 'x-forwarded-host'] }, identityScopes: {} }, trustedProxies: ['127.0.0.1'], roles: { default: 'deny_all', definitions: { deny_all: { sessions: { others: 'none' }, agents: [], scopes: [] } } } },
    agents: { entries: { 'scm-personal': { workspace: '/template', agentDir: '/template/agent', model: { name: 'approved' } } } },
    tools: { allow: ['enva_corpus_read', 'enva_scm_sql_read'], elevated: { enabled: false } },
  } };
  native.on('connection', (socket) => {
    socket.send(JSON.stringify({ event: 'connect.challenge' }));
    socket.on('message', (raw) => {
      const frame = JSON.parse(raw.toString());
      if (frame.method === 'connect') return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      if (frame.method === 'users.list') return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { profiles: [...profiles.values()] } }));
      if (frame.method === 'config.get') return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { hash: state.hash, config: state.config } }));
      if (frame.method === 'agents.create') {
        state.creates += 1;
        const { name, workspace } = frame.params;
        state.config.agents.entries[name] = { name, workspace, agentDir: `/state/agents/${name}/agent` };
        return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { agent: { id: name } } }));
      }
      if (frame.method === 'agents.update') {
        state.updates += 1;
        if (state.failUpdate) return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: { code: 'DENIED' } }));
        const entry = state.config.agents.entries[frame.params.agentId];
        if (entry) entry.name = frame.params.name;
        return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { agent: entry } }));
      }
      if (frame.method === 'config.patch') {
        state.patches += 1;
        if (state.conflict) { state.conflict = false; return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: { code: 'HASH_CONFLICT' } })); }
        const patch = JSON.parse(frame.params.raw);
        Object.assign(state.config.gateway.roles.definitions, patch.gateway.roles.definitions);
        Object.assign(state.config.gateway.auth.identityScopes, patch.gateway.auth.identityScopes);
        state.hash = `h${state.patches}`;
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
        setTimeout(() => socket.close(), 0);
        return undefined;
      }
      if (frame.method === 'users.setRole') {
        [...profiles.values()].find((item) => item.id === frame.params.profileId).role = frame.params.role;
        return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true }));
      }
      if (frame.method === 'users.setDisplayName') {
        state.displayUpdates += 1;
        if (state.failDisplayName) return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: { code: 'DENIED' } }));
        const profile = [...profiles.values()].find((item) => item.id === frame.params.profileId);
        if (profile) profile.displayName = frame.params.displayName;
        return socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: { profile: { displayName: frame.params.displayName } } }));
      }
      socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false }));
    });
  });
  const config = { allowedOrigin: 'https://bridge.example.test', nativeWsUrl: `ws://127.0.0.1:${native.address().port}`, nativeAdminPasswordFile: passwordFile, provisionMode: 'auto', templateAgentId: 'scm-personal', workspaceRoot: '/state/scm-personal/workspaces', sharedCorpus: '/corpus', stateRoot: '/state' };
  const auth = (email, agent, actor) => ({ identity: { auth_user_id: email.slice(4, 40), actor_id: actor, email }, agent_id: agent });
  await Promise.all([provisionNativeIdentity({ config, authorization: auth(identityA, agentA, 1), clientAddress: '192.0.2.10' }), provisionNativeIdentity({ config, authorization: auth(identityB, agentB, 2), clientAddress: '192.0.2.11' })]);
  state.config.agents.entries[agentA].name = agentA;
  await provisionNativeIdentity({ config, authorization: auth(identityA, agentA, 1), clientAddress: '192.0.2.10' });
  assert.equal(state.creates, 2);
  assert.equal(state.updates, 3);
  assert.equal(state.displayUpdates, 2);
  assert.ok(state.patches >= 2);
  assert.equal(state.config.agents.entries[agentA].name, 'Mi agente');
  assert.equal(state.config.agents.entries[agentB].name, 'Mi agente');
  assert.equal(state.config.agents.entries[agentA].agentDir, `/state/agents/${agentA}/agent`);
  assert.equal(profiles.get(identityA).role, `scm_person_${agentA.slice(4)}`);
  assert.equal(profiles.get(identityB).role, `scm_person_${agentB.slice(4)}`);
  assert.equal(profiles.get(identityA).displayName, 'Usuario SCM');
  assert.equal(profiles.get(identityB).displayName, 'Usuario SCM');
  state.failUpdate = true;
  await assert.rejects(
    provisionNativeIdentity({ config, authorization: auth(identityC, agentC, 3), clientAddress: '192.0.2.12' }),
    /native agents\.update denied/,
  );
  assert.equal(profiles.get(identityC).role, 'deny_all');
  assert.equal(state.config.gateway.roles.definitions[`scm_person_${agentC.slice(4)}`], undefined);
  state.failUpdate = false;
  state.failDisplayName = true;
  await assert.rejects(
    provisionNativeIdentity({ config, authorization: auth(identityC, agentC, 3), clientAddress: '192.0.2.12' }),
    /native users\.setDisplayName denied/,
  );
  assert.equal(profiles.get(identityC).role, 'deny_all');
  assert.equal(state.config.gateway.roles.definitions[`scm_person_${agentC.slice(4)}`], undefined);
  await new Promise((resolve) => native.close(resolve));
  rmSync(root, { recursive: true, force: true });
});
