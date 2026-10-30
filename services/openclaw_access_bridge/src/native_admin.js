import { readFileSync } from 'node:fs';
import { isIP } from 'node:net';
import { WebSocket } from 'ws';
import { authorizationIdentity } from './manifest.js';

const SESSION_SCOPES = ['operator.sessions.read', 'operator.sessions.write'];
const SAFE_TOOLS = ['enva_corpus_read', 'enva_scm_sql_read'];
const PERSONAL_AGENT_NAME = 'Mi agente';
const normalizeClientAddress = (value) => {
  const address = String(value || '').trim();
  const normalized = address.toLowerCase().startsWith('::ffff:') ? address.slice(7) : address;
  if (!isIP(normalized)) throw new Error('native bootstrap client address is unavailable or invalid');
  return normalized;
};

const rpc = (socket, method, params, pending, sequence) => new Promise((resolve, reject) => {
  const id = `bridge-${sequence.next++}`;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`native RPC timeout: ${method}`)); }, 5000);
  pending.set(id, {
    resolve: (value) => { clearTimeout(timer); resolve(value); },
    reject: (error) => { clearTimeout(timer); reject(error); },
  });
  socket.send(JSON.stringify({ type: 'req', id, method, params }));
});

const connect = async ({ url, headers, client, auth, scopes }) => {
  const socket = new WebSocket(url, { headers, maxPayload: 16 * 1024 * 1024 });
  const pending = new Map();
  const sequence = { next: 1 };
  const ready = new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => { settled = true; reject(new Error('native connect timeout')); }, 5000);
    const fail = (error) => {
      if (!settled) { settled = true; clearTimeout(timer); reject(error); }
      for (const request of pending.values()) request.reject(error);
      pending.clear();
    };
    socket.on('error', fail);
    socket.on('close', () => fail(new Error('native socket closed')));
    socket.on('message', (raw) => {
      let frame;
      try { frame = JSON.parse(raw.toString('utf8')); } catch { return; }
      if (frame.event === 'connect.challenge') {
        socket.send(JSON.stringify({ type: 'req', id: 'bridge-connect', method: 'connect', params: {
          minProtocol: 4, maxProtocol: 4, client, role: 'operator', scopes,
          caps: [], commands: [], permissions: {}, auth, locale: 'en-US',
        } }));
      } else if (frame.type === 'res' && frame.id === 'bridge-connect') {
        clearTimeout(timer);
        if (!frame.ok) { settled = true; reject(new Error('native connect denied')); }
        else { settled = true; resolve(); }
      } else if (frame.type === 'res') {
        const request = pending.get(frame.id);
        if (request) { pending.delete(frame.id); request.resolve(frame); }
      }
    });
  });
  try { await ready; } catch (error) { socket.close(); throw error; }
  return {
    socket,
    rpc: (method, params) => rpc(socket, method, params, pending, sequence),
    close: () => new Promise((resolve) => {
      for (const request of pending.values()) request.reject(new Error('native socket closed'));
      pending.clear();
      if (socket.readyState === WebSocket.CLOSED) return resolve();
      const timer = setTimeout(() => { socket.terminate(); resolve(); }, 250);
      socket.once('close', () => { clearTimeout(timer); resolve(); });
      socket.close();
    }),
  };
};

const exactProfile = (profiles, email) => {
  const matches = profiles.filter((profile) => Array.isArray(profile?.emails) && profile.emails.includes(email));
  if (matches.length !== 1) throw new Error('native identity profile is missing or ambiguous');
  return matches[0];
};

const configOf = (result) => result?.payload?.config || result?.config || result?.payload || result || {};
const resultHash = (result) => result?.payload?.hash || result?.payload?.configHash || result?.hash;
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const validateNativeBase = (payload) => {
  const config = configOf(payload);
  const gateway = config?.gateway;
  const roles = gateway?.roles?.definitions;
  if (gateway?.bind !== 'loopback'
    || gateway?.reload?.mode !== 'hybrid'
    || gateway?.auth?.mode !== 'trusted-proxy'
    || gateway?.auth?.token
    || gateway?.auth?.trustedProxy?.userHeader !== 'x-scm-openclaw-user'
    || !gateway?.auth?.trustedProxy?.requiredHeaders?.includes('x-forwarded-proto')
    || !gateway?.auth?.trustedProxy?.requiredHeaders?.includes('x-forwarded-host')
    || !gateway?.trustedProxies?.some((item) => ['127.0.0.1', '::1'].includes(item))
    || gateway?.roles?.default !== 'deny_all'
    || !roles?.deny_all
    || roles.deny_all.scopes?.length
    || roles.deny_all.agents?.length
    || roles.deny_all.sessions?.others !== 'none'
    || !same(config?.tools?.allow, SAFE_TOOLS)
    || config?.tools?.elevated?.enabled !== false) throw new Error('native config trust boundary is invalid');
  return config;
};

const exactRole = (config, identity, agentId, roleName) => {
  const role = config?.gateway?.roles?.definitions?.[roleName];
  const scopes = config?.gateway?.auth?.identityScopes?.[identity];
  if (!role || !same(role.scopes, SESSION_SCOPES) || role.sessions?.others !== 'none'
    || !same(role.agents, [agentId]) || !same(scopes, SESSION_SCOPES)) {
    throw new Error('native personal role config is invalid');
  }
};

const templateEntry = (config, templateId) => {
  const entry = config?.agents?.entries?.[templateId];
  if (!entry || !entry.workspace || !entry.agentDir) throw new Error('native approved agent template is missing');
  if (entry.model !== undefined && config?.agents?.defaults?.model !== undefined
    && !same(entry.model, config.agents.defaults.model)) throw new Error('native default model diverges from approved template');
  return entry;
};

const expectedAgentEntry = ({ agentId, workspace, template, stateRoot }) => {
  const entry = {
    name: PERSONAL_AGENT_NAME,
    workspace,
    agentDir: `${stateRoot}/agents/${agentId}/agent`,
  };
  if (template.model !== undefined) entry.model = clone(template.model);
  if (template.agentRuntime !== undefined) entry.agentRuntime = clone(template.agentRuntime);
  return entry;
};

const verifyAgent = (config, agentId, expected, template) => {
  const actual = config?.agents?.entries?.[agentId];
  if (!actual) return false;
  if (actual.workspace !== expected.workspace || actual.agentDir !== expected.agentDir) throw new Error('native personal agent workspace diverges');
  if (actual.name !== undefined && actual.name !== null && actual.name !== agentId && actual.name !== PERSONAL_AGENT_NAME) throw new Error('native personal agent name diverges');
  if (actual.tools?.allow !== undefined && !same(actual.tools.allow, SAFE_TOOLS)) throw new Error('native personal agent tools diverge');
  if (actual.tools?.elevated?.enabled !== undefined && actual.tools.elevated.enabled !== false) throw new Error('native personal agent elevation diverges');
  if (template.model !== undefined && actual.model !== undefined && !same(actual.model, expected.model)) throw new Error('native personal model diverges');
  if (template.agentRuntime !== undefined && actual.agentRuntime !== undefined && !same(actual.agentRuntime, expected.agentRuntime)) throw new Error('native personal runtime diverges');
  return true;
};

const convergeAgentName = async (admin, nativeConfig, configResult, agentId) => {
  const actual = nativeConfig?.agents?.entries?.[agentId];
  if (!actual) throw new Error('native personal agent is missing');
  if (actual.name !== undefined && actual.name !== null && actual.name !== agentId && actual.name !== PERSONAL_AGENT_NAME) throw new Error('native personal agent name diverges');
  if (actual.name === PERSONAL_AGENT_NAME) return { nativeConfig, configResult };
  const updated = await admin.rpc('agents.update', { agentId, name: PERSONAL_AGENT_NAME });
  if (!updated.ok) throw new Error('native agents.update denied');
  const refreshed = await admin.rpc('config.get', {});
  if (!refreshed.ok) throw new Error('native config.get denied');
  const refreshedConfig = validateNativeBase(refreshed.payload);
  const refreshedAgent = refreshedConfig?.agents?.entries?.[agentId];
  if (!refreshedAgent || refreshedAgent.name !== PERSONAL_AGENT_NAME) throw new Error('native personal agent name did not converge');
  return { nativeConfig: refreshedConfig, configResult: refreshed };
};

const convergeProfileDisplayName = async (admin, profile, displayName) => {
  if (!profile?.id) throw new Error('native identity profile id is missing');
  const current = profile.displayName ?? profile.display_name;
  if (current === displayName) return;
  const updated = await admin.rpc('users.setDisplayName', { profileId: profile.id, displayName });
  if (!updated.ok) throw new Error('native users.setDisplayName denied');
  const returned = updated.payload?.profile?.displayName
    ?? updated.payload?.profile?.display_name
    ?? updated.payload?.displayName
    ?? updated.payload?.display_name;
  if (returned !== undefined && returned !== displayName) throw new Error('native profile display name did not converge');
};

const trustedConnect = (config, canonical, clientAddress) => connect({
  url: config.nativeWsUrl,
  headers: {
    'x-scm-openclaw-user': canonical.email,
    'x-forwarded-for': normalizeClientAddress(clientAddress),
    'x-forwarded-proto': new URL(config.allowedOrigin).protocol.replace(':', ''),
    'x-forwarded-host': new URL(config.allowedOrigin).host,
    'x-openclaw-scopes': SESSION_SCOPES.join(','),
    origin: config.allowedOrigin,
  },
  client: { id: 'openclaw-control-ui', version: 'bridge', platform: 'web', mode: 'webchat' },
  auth: {}, scopes: SESSION_SCOPES,
});

const adminConnect = (config) => {
  const password = readFileSync(config.nativeAdminPasswordFile, 'utf8').trim();
  if (!password) throw new Error('native admin password is empty');
  return connect({ url: config.nativeWsUrl, headers: {}, client: {
    id: 'gateway-client', version: 'bridge', platform: 'linux', mode: 'backend',
  }, auth: { password }, scopes: ['operator.admin'] });
};

export const verifyNativeOwnSession = async ({ config, canonical, sessionKey, clientAddress }) => {
  const user = await trustedConnect(config, canonical, clientAddress);
  try {
    const result = await user.rpc('chat.history', { sessionKey, agentId: canonical.agentId, limit: 1, maxBytes: 1024, maxChars: 1 });
    return Boolean(result.ok);
  } finally { await user.close(); }
};

export const withNativeArtifactDownload = async ({ config, params, consume }) => {
  const admin = await adminConnect(config);
  try {
    const result = await admin.rpc('artifacts.download', params);
    return await consume(result);
  }
  finally { await admin.close(); }
};

const patchRole = async (admin, identity, agentId, roleName, baseHash) => {
  const result = await admin.rpc('config.patch', { raw: JSON.stringify({ gateway: {
    roles: { definitions: { [roleName]: { sessions: { others: 'none' }, agents: [agentId], scopes: SESSION_SCOPES } } },
    auth: { identityScopes: { [identity]: SESSION_SCOPES } },
  } }), baseHash });
  if (!result.ok) {
    const code = String(result.error?.code || result.error?.message || '').toLowerCase();
    if (code.includes('hash') || code.includes('conflict') || code.includes('version')) return false;
    throw new Error('native config.patch denied');
  }
  return true;
};

const provisionManifestIdentity = async ({ config, authorization, manifest, clientAddress }) => {
  const canonical = authorizationIdentity(authorization);
  if (!canonical || !manifest?.has(canonical.email)) throw new Error('manifest identity is not authorized');
  const entry = manifest.get(canonical.email);
  const user = await trustedConnect(config, canonical, clientAddress);
  user.close();
  let admin = await adminConnect(config);
  try {
    const listed = await admin.rpc('users.list', {});
    const configResult = await admin.rpc('config.get', {});
    if (!configResult.ok) throw new Error('native config.get denied');
    let nativeConfig = validateNativeBase(configResult.payload);
    const agent = nativeConfig.agents?.entries?.[canonical.agentId];
    if (!agent) throw new Error('native manifest agent is not provisioned');
    exactRole(nativeConfig, canonical.email, canonical.agentId, entry.role);
    ({ nativeConfig } = await convergeAgentName(admin, nativeConfig, configResult, canonical.agentId));
    if (!listed.ok || !Array.isArray(listed.payload?.profiles)) throw new Error('native users.list denied');
    const profile = exactProfile(listed.payload.profiles, canonical.email);
    await convergeProfileDisplayName(admin, profile, canonical.displayName);
    const currentRole = profile.role || profile.assignedRole || profile.roleName || 'deny_all';
    if (currentRole !== 'deny_all' && currentRole !== entry.role) throw new Error('native profile role diverges');
    if (currentRole !== entry.role) {
      const assigned = await admin.rpc('users.setRole', { profileId: profile.id, role: entry.role });
      if (!assigned.ok) throw new Error('native users.setRole denied');
    }
  } finally { admin.close(); }
};

const provisionAutoIdentity = async ({ config, authorization, clientAddress }) => {
  const canonical = authorizationIdentity(authorization);
  if (!canonical) throw new Error('authorization identity is not canonical');
  const roleName = `scm_person_${canonical.agentId.slice(4)}`;
  const workspace = `${config.workspaceRoot.replace(/\/$/, '')}/${canonical.agentId.slice(4)}`;
  const user = await trustedConnect(config, canonical, clientAddress);
  user.close();
  let admin = await adminConnect(config);
  try {
    const listed = await admin.rpc('users.list', {});
    if (!listed.ok || !Array.isArray(listed.payload?.profiles)) throw new Error('native users.list denied');
    const profile = exactProfile(listed.payload.profiles, canonical.email);
    const currentRole = profile.role || profile.assignedRole || profile.roleName || 'deny_all';
    if (currentRole !== 'deny_all' && currentRole !== roleName) throw new Error('native profile role diverges');
    let configResult = await admin.rpc('config.get', {});
    if (!configResult.ok) throw new Error('native config.get denied');
    let nativeConfig = validateNativeBase(configResult.payload);
    const template = templateEntry(nativeConfig, config.templateAgentId);
    const expected = expectedAgentEntry({ agentId: canonical.agentId, workspace, template, stateRoot: config.stateRoot || '/state' });
    if (!verifyAgent(nativeConfig, canonical.agentId, expected, template)) {
      const created = await admin.rpc('agents.create', { name: canonical.agentId, workspace });
      if (!created.ok) throw new Error('native agents.create denied');
      const createdId = created.payload?.agent?.id || created.payload?.agentId || created.payload?.id;
      if (createdId && createdId !== canonical.agentId) throw new Error('native agents.create returned a divergent id');
      configResult = await admin.rpc('config.get', {});
      if (!configResult.ok) throw new Error('native config.get denied');
      nativeConfig = validateNativeBase(configResult.payload);
      verifyAgent(nativeConfig, canonical.agentId, expected, template);
    }
    ({ nativeConfig, configResult } = await convergeAgentName(admin, nativeConfig, configResult, canonical.agentId));
    await convergeProfileDisplayName(admin, profile, canonical.displayName);
    let converged = false;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const currentRoleEntry = nativeConfig.gateway.roles.definitions?.[roleName];
      const currentScopes = nativeConfig.gateway.auth.identityScopes?.[canonical.email];
      if (currentRoleEntry) {
        if (currentRoleEntry.agents !== undefined && !same(currentRoleEntry.agents, [canonical.agentId])) throw new Error('native personal role agent diverges');
        if (currentRoleEntry.sessions?.others !== undefined && currentRoleEntry.sessions.others !== 'none') throw new Error('native personal role session policy diverges');
        if (currentRoleEntry.scopes !== undefined && !same(currentRoleEntry.scopes, SESSION_SCOPES)) throw new Error('native personal role scopes diverge');
      }
      if (currentScopes && !same(currentScopes, SESSION_SCOPES)) throw new Error('native identity scopes diverge');
      const agentConverged = verifyAgent(nativeConfig, canonical.agentId, expected, template);
      if (currentRoleEntry?.agents !== undefined && currentRoleEntry?.sessions?.others !== undefined
        && currentRoleEntry?.scopes !== undefined && currentScopes && agentConverged) { converged = true; break; }
      const baseHash = resultHash(configResult);
      if (!baseHash) throw new Error('native config.get did not return a base hash');
      if (await patchRole(admin, canonical.email, canonical.agentId, roleName, baseHash)) {
        admin.close();
        admin = await adminConnect(config);
        converged = true;
        break;
      }
      configResult = await admin.rpc('config.get', {});
      if (!configResult.ok) throw new Error('native config.get denied');
      nativeConfig = validateNativeBase(configResult.payload);
    }
    if (!converged) throw new Error('native config.patch conflict did not converge');
    configResult = await admin.rpc('config.get', {});
    if (!configResult.ok) throw new Error('native config.get denied');
    nativeConfig = validateNativeBase(configResult.payload);
    ({ nativeConfig, configResult } = await convergeAgentName(admin, nativeConfig, configResult, canonical.agentId));
    verifyAgent(nativeConfig, canonical.agentId, expected, template);
    exactRole(nativeConfig, canonical.email, canonical.agentId, roleName);
    if (currentRole !== roleName) {
      const assigned = await admin.rpc('users.setRole', { profileId: profile.id, role: roleName });
      if (!assigned.ok) throw new Error('native users.setRole denied');
    }
  } finally { admin.close(); }
};

export const provisionNativeIdentity = async ({ config, authorization, manifest, clientAddress }) => {
  if (config.provisionMode === 'manifest' || manifest) return provisionManifestIdentity({ config, authorization, manifest, clientAddress });
  return provisionAutoIdentity({ config, authorization, clientAddress });
};
