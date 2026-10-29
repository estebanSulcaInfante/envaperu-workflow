import { readFileSync } from 'node:fs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const authorizationIdentity = (authorization) => {
  const identity = authorization?.identity;
  const rawAuthUserId = String(identity?.auth_user_id || '');
  const authUserId = rawAuthUserId.toLowerCase();
  const actorId = identity?.actor_id;
  if (rawAuthUserId !== authUserId || !UUID.test(authUserId)
    || !Number.isInteger(Number(actorId)) || Number(actorId) <= 0) return null;
  const agentId = `scm-${authUserId.replaceAll('-', '')}`;
  const email = `scm+${authUserId}@internal.envaperu.invalid`;
  if (identity.email !== undefined && identity.email !== email) return null;
  if (authorization.agent_id !== agentId) return null;
  return { authUserId, actorId: Number(actorId), email, agentId };
};

export const loadManifest = (filePath) => {
  let value;
  try { value = JSON.parse(readFileSync(filePath, 'utf8')); } catch { throw new Error('SCM_NATIVE_MANIFEST cannot be read'); }
  if (!Array.isArray(value?.entries)) throw new Error('SCM_NATIVE_MANIFEST.entries is required');
  const entries = new Map();
  for (const entry of value.entries) {
    const identityMatch = entry && typeof entry.identity === 'string'
      ? /^scm\+([0-9a-f-]{36})@internal\.envaperu\.invalid$/.exec(entry.identity)
      : null;
    if (!entry || typeof entry.identity !== 'string' || typeof entry.agent_id !== 'string'
      || typeof entry.role !== 'string' || !entry.identity.startsWith('scm+')
      || !entry.identity.endsWith('@internal.envaperu.invalid')
      || !/^scm-[0-9a-f]{32}$/.test(entry.agent_id)
      || !identityMatch
      || entry.agent_id !== `scm-${identityMatch[1].replaceAll('-', '')}`
      || entry.role !== `scm_person_${entry.agent_id.slice(4)}`) {
      throw new Error('SCM_NATIVE_MANIFEST contains an invalid entry');
    }
    if (entries.has(entry.identity)) throw new Error('SCM_NATIVE_MANIFEST has duplicate identity');
    entries.set(entry.identity, Object.freeze({ ...entry }));
  }
  return entries;
};

export const manifestAllows = (manifest, authorization) => {
  const canonical = authorizationIdentity(authorization);
  if (!canonical) return false;
  if (!manifest) return true;
  const entry = manifest.get(canonical.email);
  return Boolean(entry && entry.agent_id === canonical.agentId
    && String(canonical.actorId) === String(entry.actor_id ?? canonical.actorId));
};
