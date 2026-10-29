import path from 'node:path';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);
const boundedInt = (value, name, min, max, fallback) => {
  const parsed = value === undefined || value === '' ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) throw new Error(`${name} must be an integer between ${min} and ${max}`);
  return parsed;
};

const asOrigin = (value, name) => {
  let parsed;
  try { parsed = new URL(String(value)); } catch { throw new Error(`${name} must be an absolute URL`); }
  if (!['http:', 'https:'].includes(parsed.protocol)
    || parsed.username || parsed.password || parsed.pathname !== '/'
    || parsed.search || parsed.hash) throw new Error(`${name} must be an origin`);
  return parsed.origin;
};

const allowHttpLoopback = (parsed, env) => parsed.protocol === 'http:'
  && (env.TESTING === true || env.TESTING === 'true')
  && LOOPBACK_HOSTS.has(parsed.hostname);

const asApiUrl = (value, env) => {
  let parsed;
  try { parsed = new URL(String(value)); } catch { throw new Error('SCM_API_URL must be an absolute URL'); }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password
    || parsed.search || parsed.hash || parsed.pathname === '/') throw new Error('SCM_API_URL must be a fixed API base URL');
  if (parsed.protocol !== 'https:' && !allowHttpLoopback(parsed, env)) throw new Error('SCM_API_URL must use HTTPS');
  return parsed.toString().replace(/\/+$/, '');
};

const asPublicOrigin = (value, name, env) => {
  const parsed = new URL(asOrigin(value, name));
  if (parsed.protocol !== 'https:' && !allowHttpLoopback(parsed, env)) throw new Error(`${name} must use HTTPS`);
  return parsed.origin;
};

const asNativeHttpOrigin = (value, trustedHosts = '') => {
  const origin = asOrigin(value, 'SCM_NATIVE_HTTP_ORIGIN');
  const host = new URL(origin).hostname;
  const parsed = new URL(origin);
  if (parsed.protocol !== 'https:' && !LOOPBACK_HOSTS.has(host)) throw new Error('native HTTP must use loopback HTTP or HTTPS');
  if (!LOOPBACK_HOSTS.has(host) && !trustedHosts.split(',').map((item) => item.trim()).includes(host)) {
    throw new Error('SCM_NATIVE_HTTP_ORIGIN must be fixed to a trusted native host');
  }
  return origin;
};

const asNativeWsUrl = (value, trustedHosts = '') => {
  let parsed;
  try { parsed = new URL(String(value)); } catch { throw new Error('SCM_NATIVE_WS_URL must be an absolute URL'); }
  if (!['ws:', 'wss:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('SCM_NATIVE_WS_URL must be a fixed websocket URL');
  }
  if (parsed.protocol !== 'wss:' && !LOOPBACK_HOSTS.has(parsed.hostname)) throw new Error('native WS must use loopback WS or WSS');
  if (!LOOPBACK_HOSTS.has(parsed.hostname)
    && !trustedHosts.split(',').map((item) => item.trim()).includes(parsed.hostname)) {
    throw new Error('SCM_NATIVE_WS_URL must be fixed to a trusted native host');
  }
  return parsed.toString();
};

export const loadConfig = (env = process.env) => {
  const serviceSecret = String(env.SCM_PERSONAL_AGENT_SERVICE_SECRET || '').trim();
  if (serviceSecret.length < 32) throw new Error('SCM_PERSONAL_AGENT_SERVICE_SECRET is required');
  const apiUrl = asApiUrl(env.SCM_API_URL || 'http://127.0.0.1:5000/api', env);
  const allowedOrigin = asPublicOrigin(
    env.SCM_ALLOWED_ORIGIN || env.SCM_PERSONAL_AGENT_BRIDGE_ORIGIN || 'https://openclaw.example.test',
    'SCM_ALLOWED_ORIGIN',
    env,
  );
  const provisionMode = String(env.SCM_NATIVE_PROVISION_MODE || 'auto').trim().toLowerCase();
  if (!['auto', 'manifest'].includes(provisionMode)) throw new Error('SCM_NATIVE_PROVISION_MODE must be auto or manifest');
  const manifestPath = env.SCM_NATIVE_MANIFEST
    ? path.resolve(env.SCM_NATIVE_MANIFEST)
    : '';
  if (provisionMode === 'manifest' && !manifestPath) throw new Error('SCM_NATIVE_MANIFEST is required in manifest mode');
  if (!env.SCM_NATIVE_ADMIN_PASSWORD_FILE) throw new Error('SCM_NATIVE_ADMIN_PASSWORD_FILE is required');
  const templateAgentId = String(env.SCM_NATIVE_TEMPLATE_AGENT_ID || 'scm-personal').trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(templateAgentId)) throw new Error('SCM_NATIVE_TEMPLATE_AGENT_ID is invalid');
  const workspaceRoot = String(env.SCM_NATIVE_WORKSPACE_ROOT || '/state/scm-personal/workspaces').trim();
  if (!workspaceRoot || workspaceRoot.includes('..') || !workspaceRoot.startsWith('/')) throw new Error('SCM_NATIVE_WORKSPACE_ROOT must be an absolute path');
  const sharedCorpus = String(env.SCM_NATIVE_SHARED_CORPUS || '/state/scm-personal/corpus').trim();
  if (!sharedCorpus || sharedCorpus.includes('..') || !sharedCorpus.startsWith('/')) throw new Error('SCM_NATIVE_SHARED_CORPUS must be an absolute path');
  const stateRoot = String(env.SCM_NATIVE_STATE_ROOT || '/state').trim().replace(/\/$/, '');
  if (!stateRoot || stateRoot.includes('..') || !stateRoot.startsWith('/')) throw new Error('SCM_NATIVE_STATE_ROOT must be an absolute path');
  const maxBodyBytes = boundedInt(env.SCM_BRIDGE_MAX_BODY_BYTES, 'SCM_BRIDGE_MAX_BODY_BYTES', 1024, 16 * 1024 * 1024, 16 * 1024 * 1024);
  const apiTimeoutMs = boundedInt(env.SCM_BRIDGE_API_TIMEOUT_MS, 'SCM_BRIDGE_API_TIMEOUT_MS', 100, 30000, 5000);
  const maxIdleSeconds = boundedInt(env.SCM_PERSONAL_AGENT_MAX_IDLE_SECONDS, 'SCM_PERSONAL_AGENT_MAX_IDLE_SECONDS', 30, 86400, 900);
  return {
    host: env.SCM_BRIDGE_HOST || '127.0.0.1',
    port: boundedInt(env.SCM_BRIDGE_PORT, 'SCM_BRIDGE_PORT', 1, 65535, 5182),
    apiUrl,
    authorizationPath: `${apiUrl}/scm/v1/agente/autorizacion`,
    serviceSecret,
    allowedOrigin,
    scmFormOrigin: env.SCM_SCM_ORIGIN ? asPublicOrigin(env.SCM_SCM_ORIGIN, 'SCM_SCM_ORIGIN', env) : null,
    nativeHttpOrigin: asNativeHttpOrigin(env.SCM_NATIVE_HTTP_ORIGIN || 'http://127.0.0.1:18789', env.SCM_NATIVE_TRUSTED_HOSTS || ''),
    nativeWsUrl: asNativeWsUrl(env.SCM_NATIVE_WS_URL || 'ws://127.0.0.1:18789', env.SCM_NATIVE_TRUSTED_HOSTS || ''),
    nativeAdminPasswordFile: path.resolve(env.SCM_NATIVE_ADMIN_PASSWORD_FILE),
    nativeManifestPath: manifestPath,
    provisionMode,
    templateAgentId,
    workspaceRoot,
    sharedCorpus,
    stateRoot,
    dbPath: path.resolve(env.SCM_BRIDGE_DB_PATH || '/var/lib/scm-openclaw-bridge/bridge.sqlite'),
    maxBodyBytes,
    apiTimeoutMs,
    maxIdleSeconds,
    maxSessionSeconds: boundedInt(env.SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS, 'SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS', 30, 86400, 3600),
    cookieName: 'scm_personal_sid',
    maxWsPayload: 16 * 1024 * 1024,
  };
};

export { LOOPBACK_HOSTS };
