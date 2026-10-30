import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { Readable } from 'node:stream';
import { WebSocket, WebSocketServer } from 'ws';
import { loadConfig } from './config.js';
import { loadManifest, manifestAllows } from './manifest.js';
import { ScmAccessClient, ScmAccessError } from './scm_access_client.js';
import { SessionStore } from './session_store.js';
import { provisionNativeIdentity } from './native_admin.js';
import { DownloadStore } from './download_store.js';
import { DownloadError, NativeArtifactBroker } from './native_artifacts.js';

const hash = (value) => createHash('sha256').update(value).digest('hex');
const now = () => Math.floor(Date.now() / 1000);
const PENDING_COOKIE_NAME = '__Host-scm_personal_pending';
const OWNER_MARKER_COOKIE_NAME = '__Host-scm_personal_owner';
const PENDING_MAX_SECONDS = 120;
const OWNER_MARKER_MAX_AGE = 30 * 24 * 60 * 60;

const hmac = (secret, value) => createHmac('sha256', secret).update(value).digest('base64url');
const constantTimeEqual = (left, right) => {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  const leftBytes = Buffer.from(left);
  const rightBytes = Buffer.from(right);
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
};
const pendingSignature = (secret, sid, expiration) => hmac(secret, `pending|${sid}|${expiration}`);
const ownerMarker = (config, authUserId) => hmac(
  config.serviceSecret,
  `v1|${new URL(config.allowedOrigin).origin}|${authUserId}`,
);

const parseCookies = (header = '') => {
  try {
    return Object.fromEntries(
      header.split(';').map((part) => part.trim().split('='))
        .filter(([key, value]) => key && value)
        .map(([key, ...value]) => [key, decodeURIComponent(value.join('='))]),
    );
  } catch { return {}; }
};

const securityHeaders = (headers = {}) => ({
  'Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
  ...headers,
});

const send = (res, status, body, headers = {}) => {
  const payload = Buffer.from(body);
  res.writeHead(status, securityHeaders({ 'Content-Length': payload.length, ...headers }));
  res.end(payload);
};

const sendJson = (res, status, body) => send(res, status, JSON.stringify(body), { 'Content-Type': 'application/json' });
const sendRedirect = (res, location) => {
  res.writeHead(303, securityHeaders({ Location: location, 'Content-Length': 0 }));
  res.end();
};

const recoveryPage = (message = 'La sesión del agente no está disponible. Vuelve a SCM y reintenta.', recoveryHref = null) => (
  `<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Acceso no disponible</title></head>`
  + `<body><main><h1>Acceso no disponible</h1><p>${message}</p>`
  + (recoveryHref ? `<p><a href="${recoveryHref}">Continuar en SCM</a></p>` : '')
  + `</main></body></html>`
);

const originAllowed = (req, config, { form = false } = {}) => {
  const origin = req.headers.origin;
  if (!origin) return false;
  return origin === (form ? (config.scmFormOrigin || config.allowedOrigin) : config.allowedOrigin);
};

const CLIENT_PROXY_HEADERS = new Set([
  'accept', 'accept-language', 'cache-control', 'content-type', 'if-none-match',
  'if-modified-since', 'range', 'user-agent', 'x-requested-with',
]);

const STATIC_PREFIXES = ['/assets/', '/themes/', '/fonts/', '/app-art/', '/community-art/', '/file-icons/', '/provider-icons/', '/cloud-provider-icons/'];
const STATIC_EXTENSIONS = new Set(['.js', '.mjs', '.css', '.map', '.json', '.webmanifest', '.wasm', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf', '.otf']);
const MEDIA_PARAMS = new Set(['meta', 'source', 'sessionKey', 'agentId', 'mediaTicket', 'filename', 'playback']);
// Fixed native control-ui route-navigation.ts contract; values are navigation hints only.
const SPA_PARAM_AGENT = 'agent';
const SPA_PARAM_SESSION_KEY = 'sessionKey';
const SPA_PARAM_OPENCLAW_SESSION_KEY = '__openclawSessionKey';
const SPA_PARAM_SESSION_FACE_PREFERENCE = '__openclawSessionFacePreference';
const SPA_PARAM_COMPOSER_FOCUS = '__openclawComposerFocus';
const SPA_PARAM_DASHBOARD = 'dashboard';
const SPA_PARAM_MOUNT_RECOVERY = 'openclaw_mount_recovery';
const SPA_PARAMS = new Set([
  SPA_PARAM_AGENT, SPA_PARAM_SESSION_KEY, SPA_PARAM_OPENCLAW_SESSION_KEY,
  SPA_PARAM_SESSION_FACE_PREFERENCE, SPA_PARAM_COMPOSER_FOCUS, SPA_PARAM_DASHBOARD,
  SPA_PARAM_MOUNT_RECOVERY,
]);
const MAX_QUERY_BYTES = 8192;
const MAX_QUERY_VALUE = 2048;

const hasPathTraversal = (rawUrl) => /(?:^|[\\/])\.\.?(?:[\\/]|$)|%2e|%2f|%5c/i.test(String(rawUrl || '').split('?')[0]);
const ownSessionKey = (value, agentId) => typeof value === 'string' && value.startsWith(`agent:${agentId}:`);
const validQueryValue = (value) => typeof value === 'string'
  && value.length <= MAX_QUERY_VALUE
  && !/[\u0000-\u001f\u007f]/.test(value);
const singletonQuery = (url, agentId, allowed) => {
  if (Buffer.byteLength(url.search, 'utf8') > MAX_QUERY_BYTES) return false;
  for (const key of url.searchParams.keys()) if (!allowed.has(key)) return false;
  for (const key of allowed) {
    const values = url.searchParams.getAll(key);
    if (values.length > 1 || values.some((value) => !validQueryValue(value))) return false;
  }
  if (url.searchParams.has(SPA_PARAM_AGENT) && url.searchParams.get(SPA_PARAM_AGENT) !== agentId) return false;
  if (url.searchParams.has('agentId') && url.searchParams.get('agentId') !== agentId) return false;
  const sessionKey = url.searchParams.get(SPA_PARAM_SESSION_KEY);
  const nativeSessionKey = url.searchParams.get(SPA_PARAM_OPENCLAW_SESSION_KEY);
  if (url.searchParams.has(SPA_PARAM_SESSION_KEY) && !ownSessionKey(sessionKey, agentId)) return false;
  if (url.searchParams.has(SPA_PARAM_OPENCLAW_SESSION_KEY) && !ownSessionKey(nativeSessionKey, agentId)) return false;
  if (url.searchParams.has(SPA_PARAM_SESSION_KEY) && url.searchParams.has(SPA_PARAM_OPENCLAW_SESSION_KEY)
    && sessionKey !== nativeSessionKey) return false;
  return true;
};

const validSpaRouteQuery = (url, ownAgentId) => {
  if (!singletonQuery(url, ownAgentId, SPA_PARAMS)) return false;
  if (url.searchParams.has(SPA_PARAM_MOUNT_RECOVERY)
    && !/^\d{13}$/.test(url.searchParams.get(SPA_PARAM_MOUNT_RECOVERY))) return false;
  if (url.searchParams.has(SPA_PARAM_SESSION_FACE_PREFERENCE)
    && url.searchParams.get(SPA_PARAM_SESSION_FACE_PREFERENCE) !== '1') return false;
  if (url.searchParams.has(SPA_PARAM_COMPOSER_FOCUS)
    && url.searchParams.get(SPA_PARAM_COMPOSER_FOCUS) !== '1') return false;
  if (url.searchParams.has(SPA_PARAM_DASHBOARD)
    && url.searchParams.get(SPA_PARAM_DASHBOARD) !== 'expanded') return false;
  return true;
};

const validStaticQuery = (url) => {
  if (!url.search) return true;
  const values = url.searchParams.getAll('v');
  return url.searchParams.size === 1
    && values.length === 1
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/.test(values[0]);
};

const allowedNativeRoute = (method, target, ownAgentId, rawUrl) => {
  if (!['GET', 'HEAD'].includes(method) || hasPathTraversal(rawUrl)) return false;
  const pathname = target.pathname;
  if (pathname === '/__openclaw__/assistant-media') {
    if (!singletonQuery(target, ownAgentId, MEDIA_PARAMS)
      || target.searchParams.get('agentId') !== ownAgentId
      || !ownSessionKey(target.searchParams.get('sessionKey'), ownAgentId)) return false;
    return true;
  }
  if (pathname === '/' || pathname === '/new' || pathname === '/chat' || pathname.startsWith('/chat/')) {
    if (!validSpaRouteQuery(target, ownAgentId)) return false;
    const chatId = pathname.startsWith('/chat/') ? pathname.slice('/chat/'.length).split('/')[0] : '';
    return !chatId || !chatId.startsWith('scm-') || chatId === ownAgentId;
  }
  if (pathname === '/control-ui-config.json' || pathname === '/__openclaw/control-ui-config.json'
    || pathname === '/favicon.ico' || pathname === '/favicon.svg' || pathname === '/favicon-32.png'
    || pathname === '/apple-touch-icon.png' || pathname === '/manifest.json'
    || pathname === '/manifest.webmanifest' || pathname === '/asset-manifest.json') return target.search === '';
  if (pathname === '/sw.js') return validStaticQuery(target);
  if (STATIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    const extension = pathname.slice(pathname.lastIndexOf('/')).match(/\.[^.]+$/)?.[0]?.toLowerCase();
    return Boolean(extension && STATIC_EXTENSIONS.has(extension) && validStaticQuery(target));
  }
  return false;
};

const readBody = async (req, maxBytes) => {
  const contentEncoding = String(req.headers['content-encoding'] || '').trim().toLowerCase();
  if (contentEncoding && contentEncoding !== 'identity') throw Object.assign(new Error('compressed request body is not accepted'), { status: 415 });
  const expected = Number(req.headers['content-length'] || 0);
  if (expected > maxBytes) throw Object.assign(new Error('request too large'), { status: 413 });
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > maxBytes) throw Object.assign(new Error('request too large'), { status: 413 });
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
};

const sessionBinding = (authorization) => ({
  authUserId: authorization.identity.auth_user_id,
  actorId: authorization.identity.actor_id,
  agentId: authorization.agent_id,
});

const safeQuery = (url) => {
  for (const key of ['sid', 'session', 'token', 'access_token', 'authorization', 'agent_id']) {
    if (url.searchParams.has(key)) return false;
  }
  return true;
};

const frameTargetsOtherAgent = (data, ownAgentId) => {
  let frame;
  try { frame = JSON.parse(Buffer.isBuffer(data) ? data.toString('utf8') : String(data)); } catch { return false; }
  const params = frame?.params;
  if (!params || typeof params !== 'object') return false;
  if (typeof params.agentId === 'string' && params.agentId !== ownAgentId) return true;
  const keys = [params.sessionKey, params.key, params.session?.key];
  return keys.some((key) => typeof key === 'string' && key.startsWith('agent:') && !key.startsWith(`agent:${ownAgentId}:`));
};

const filterNativeFrame = (data, ownAgentId) => {
  let frame;
  try { frame = JSON.parse(Buffer.isBuffer(data) ? data.toString('utf8') : String(data)); } catch { return data; }
  const defaults = frame?.type === 'res' && frame?.ok && frame?.payload?.snapshot?.sessionDefaults;
  if (defaults) {
    defaults.defaultAgentId = ownAgentId;
    defaults.mainSessionKey = `agent:${ownAgentId}:${defaults.mainKey || 'main'}`;
    return JSON.stringify(frame);
  }
  if (frame?.method !== 'agents.list' && !(frame?.type === 'res' && frame?.payload?.agents)) return data;
  if (Array.isArray(frame?.payload?.agents)) {
    frame.payload.agents = frame.payload.agents.filter((agent) => agent?.id === ownAgentId);
    frame.payload.defaultId = ownAgentId;
    frame.payload.selectionRequired = false;
  }
  return JSON.stringify(frame);
};

const validPairingRequestId = (value) => typeof value === 'string'
  && value.length > 0 && value.length <= 128 && !/[\u0000-\u001f\u007f-\u009f]/.test(value);
const PAIRING_MAX_PAYLOAD_BYTES = 16 * 1024;
const definitiveScmStatus = (error) => error?.status === 401 || error?.status === 403 ? error.status : 503;
const provisioningStatus = (error) => error instanceof ScmAccessError && (error.status === 401 || error.status === 403)
  ? error.status : 503;

export const createBridgeServer = (config, dependencies = {}) => {
  const store = dependencies.store || new SessionStore(config.dbPath);
  const scm = dependencies.scm || new ScmAccessClient(config, dependencies.fetchImpl || fetch);
  const manifest = dependencies.manifest
    || (config.provisionMode === 'manifest' ? loadManifest(config.nativeManifestPath) : null);
  const provisioner = dependencies.provisioner || provisionNativeIdentity;
  const provisioning = new Map();
  const purgeTimer = setInterval(() => store.purgeExpired?.(), 60_000);
  purgeTimer.unref?.();
  const downloadStore = dependencies.downloadStore || new DownloadStore();
  const artifactBroker = dependencies.artifactBroker || new NativeArtifactBroker({ config, store: downloadStore, fetchImpl: dependencies.fetchImpl || fetch });
  const wsServer = new WebSocketServer({ noServer: true, maxPayload: config.maxWsPayload });

  const handleExpiredUpgrade = (req, socket, head) => {
    wsServer.handleUpgrade(req, socket, head, (client) => {
      let settled = false;
      const finish = () => {
        clearTimeout(timer);
        client.off('message', onMessage);
        client.off('close', finish);
        client.off('error', onError);
      };
      const closeUnexpected = (code, reason) => {
        if (settled) return;
        settled = true;
        finish();
        if (client.readyState === WebSocket.OPEN) client.close(code, reason);
      };
      const onMessage = (data, isBinary) => {
        if (settled) return;
        if (isBinary) return closeUnexpected(1003, 'Binario no permitido');
        if (Buffer.byteLength(data) > PAIRING_MAX_PAYLOAD_BYTES) return closeUnexpected(1009, 'Solicitud demasiado grande');
        let frame;
        try { frame = JSON.parse(data.toString('utf8')); } catch { return closeUnexpected(1008, 'Solicitud no permitida'); }
        if (frame?.type !== 'req' || frame.method !== 'connect' || !validPairingRequestId(frame.id)) {
          return closeUnexpected(1008, 'Solicitud no permitida');
        }
        settled = true;
        const response = JSON.stringify({
          type: 'res', id: frame.id, ok: false,
          error: {
            code: 'NOT_PAIRED',
            message: 'Tu sesión de SCM venció. Vuelve a SCM para continuar.',
            retryable: false,
            details: { code: 'AUTH_VERIFIED_USER_REQUIRED' },
          },
        });
        finish();
        try {
          client.send(response, () => { if (client.readyState === WebSocket.OPEN) client.close(1008, 'Sesión SCM vencida'); });
        } catch { if (client.readyState === WebSocket.OPEN) client.close(1008, 'Sesión SCM vencida'); }
      };
      const onError = () => closeUnexpected(1008, 'Sesión SCM vencida');
      const timer = setTimeout(() => closeUnexpected(1008, 'Sesión SCM vencida'), 5000);
      client.on('message', onMessage);
      client.once('close', finish);
      client.once('error', onError);
      try {
        client.send(JSON.stringify({ event: 'connect.challenge', nonce: randomBytes(16).toString('base64url'), ts: Date.now() }));
      } catch { closeUnexpected(1008, 'Sesión SCM vencida'); }
    });
  };

  const getAuthorizedSession = async (req) => {
    const sid = parseCookies(req.headers.cookie || '')[config.cookieName];
    if (!sid || !/^[A-Za-z0-9_-]{32,128}$/.test(sid)) return { kind: 'invalid' };
    const row = store.getSession(hash(sid));
    if (!row) return { kind: 'invalid' };
    let authorization;
    try { authorization = await scm.authorize(row.assertion, 'session'); } catch (error) {
      if (error?.status === 401 || error?.status === 403) {
        store.deleteSession(row.sid_hash);
        return { kind: 'invalid' };
      }
      return { kind: 'temporary', status: 503 };
    }
    const binding = sessionBinding(authorization);
    if (binding.authUserId !== row.auth_user_id
      || String(binding.actorId) !== String(row.actor_id)
      || binding.agentId !== row.agent_id
      || !manifestAllows(manifest, authorization)) {
      store.deleteSession(row.sid_hash);
      return { kind: 'invalid' };
    }
    const nextIdle = Math.min(now() + config.maxIdleSeconds, row.expires_at);
    store.touchSession(row.sid_hash, nextIdle);
    return { kind: 'authorized', row, authorization, sid };
  };

  const ensureProvisioned = async (authorization, clientAddress) => {
    const email = authorization.identity.email
      || `scm+${authorization.identity.auth_user_id}@internal.envaperu.invalid`;
    if (provisioning.has(email)) return provisioning.get(email);
    const task = provisioner({ config, authorization, manifest, clientAddress })
      .finally(() => provisioning.delete(email));
    provisioning.set(email, task);
    return task;
  };

  const upstreamHeaders = (req, authorization) => {
    const headers = {};
    for (const [key, value] of Object.entries(req.headers)) {
      const lower = key.toLowerCase();
      if (!CLIENT_PROXY_HEADERS.has(lower)) continue;
      if (value !== undefined) headers[lower] = value;
    }
    headers['x-scm-openclaw-user'] = authorization.identity.email
      || `scm+${authorization.identity.auth_user_id}@internal.envaperu.invalid`;
    headers['x-openclaw-scopes'] = 'operator.sessions.read,operator.sessions.write';
    headers['x-forwarded-for'] = req.socket.remoteAddress || '';
    headers['x-forwarded-proto'] = 'https';
    headers['x-forwarded-host'] = new URL(config.allowedOrigin).host;
    headers.origin = config.allowedOrigin;
    return headers;
  };

  const exchange = async (req, res) => {
    if (!originAllowed(req, config, { form: true })) return sendJson(res, 403, { error: 'origin_not_allowed' });
    let body;
    try { body = await readBody(req, 64 * 1024); } catch (error) { return sendJson(res, error.status || 413, { error: 'request_invalid' }); }
    if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/x-www-form-urlencoded')) {
      return sendJson(res, 415, { error: 'form_required' });
    }
    const params = new URLSearchParams(body.toString('utf8'));
    if (Array.from(params.keys()).some((key) => key !== 'assertion')
      || params.getAll('assertion').length !== 1
      || !params.get('assertion')) {
      return sendJson(res, 400, { error: 'assertion_required' });
    }
    const assertion = params.get('assertion');
    let authorization;
    try {
      authorization = await scm.authorize(assertion, 'exchange');
      if (!manifestAllows(manifest, authorization)) throw new ScmAccessError('manifest denied', 403);
    } catch (error) {
      const status = definitiveScmStatus(error);
      if (status === 503) return send(res, status, recoveryPage('SCM no está disponible. Reintenta en unos segundos.', config.scmFormOrigin ? `${config.scmFormOrigin}/mi-agente` : null), { 'Content-Type': 'text/html; charset=utf-8' });
      return sendJson(res, status, { error: 'exchange_denied' });
    }
    try {
      await ensureProvisioned(authorization, req.socket?.remoteAddress);
    } catch (error) {
      const status = provisioningStatus(error);
      if (status === 503) return send(res, status, recoveryPage('El agente no está disponible. Reintenta en unos segundos.', config.scmFormOrigin ? `${config.scmFormOrigin}/mi-agente` : null), { 'Content-Type': 'text/html; charset=utf-8' });
      return sendJson(res, status, { error: 'provisioning_denied' });
    }
    try {
      const refreshed = await scm.authorize(assertion, 'session');
      if (!manifestAllows(manifest, refreshed)
        || refreshed.identity.auth_user_id !== authorization.identity.auth_user_id
        || String(refreshed.identity.actor_id) !== String(authorization.identity.actor_id)
        || refreshed.agent_id !== authorization.agent_id) throw new ScmAccessError('session binding changed', 403);
      authorization = refreshed;
    } catch (error) {
      const status = definitiveScmStatus(error);
      if (status === 503) return send(res, status, recoveryPage('SCM no está disponible. Reintenta en unos segundos.', config.scmFormOrigin ? `${config.scmFormOrigin}/mi-agente` : null), { 'Content-Type': 'text/html; charset=utf-8' });
      return sendJson(res, status, { error: 'provisioning_denied' });
    }
    const expiration = Math.min(
      Math.floor(Date.parse(authorization.expires_at) / 1000),
      now() + config.maxSessionSeconds,
    );
    const created = now();
    if (!Number.isFinite(expiration) || expiration <= created) {
      return sendJson(res, 403, { error: 'assertion_expired_or_consumed' });
    }
    if (!store.consumeAssertion(hash(authorization.jti), expiration)) {
      return sendJson(res, 403, { error: 'assertion_expired_or_consumed' });
    }
    const sid = randomBytes(32).toString('base64url');
    const pendingExpiration = Math.min(expiration, created + PENDING_MAX_SECONDS);
    const pendingToken = `${sid}.${pendingExpiration}.${pendingSignature(config.serviceSecret, sid, pendingExpiration)}`;
    const previousSid = parseCookies(req.headers.cookie || '')[config.cookieName];
    if (previousSid && /^[A-Za-z0-9_-]{32,128}$/.test(previousSid)) store.deleteSession(hash(previousSid));
    const previousPending = parseCookies(req.headers.cookie || '')[PENDING_COOKIE_NAME];
    if (previousPending) {
      const [previousPendingSid] = previousPending.split('.');
      if (/^[A-Za-z0-9_-]{32,128}$/.test(previousPendingSid)) store.deleteSession(hash(previousPendingSid));
    }
    store.createSession({
      sidHash: hash(sid),
      assertion,
      ...sessionBinding(authorization),
      expiresAt: expiration,
      idleExpiresAt: Math.min(created + config.maxIdleSeconds, expiration),
      createdAt: created,
    });
    const pendingCookie = `${PENDING_COOKIE_NAME}=${encodeURIComponent(pendingToken)}; Max-Age=${PENDING_MAX_SECONDS}; Path=/; HttpOnly; Secure; SameSite=Lax`;
    const expiredSessionCookie = `${config.cookieName}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`;
    return send(res, 303, '', {
      Location: '/_scm/continue',
      'Set-Cookie': [expiredSessionCookie, pendingCookie],
    });
  };

  const continueExchange = async (req, res) => {
    if (req.method !== 'GET' || req.url !== '/_scm/continue') return sendJson(res, 404, { error: 'continuation_not_found' });
    const cookies = parseCookies(req.headers.cookie || '');
    const token = cookies[PENDING_COOKIE_NAME];
    const parts = typeof token === 'string' ? token.split('.') : [];
    const pendingSid = parts[0];
    const expiration = Number(parts[1]);
    const signature = parts[2];
    const expectedSignature = /^[A-Za-z0-9_-]{32,128}$/.test(pendingSid) && Number.isInteger(expiration)
      ? pendingSignature(config.serviceSecret, pendingSid, expiration) : '';
    const signatureEqual = signature && expectedSignature
      && Buffer.byteLength(signature) === Buffer.byteLength(expectedSignature)
      && timingSafeEqual(Buffer.from(signature), Buffer.from(expectedSignature));
    const expirePending = () => ({ 'Set-Cookie': `${PENDING_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax` });
    if (parts.length !== 3 || !signatureEqual || expiration <= now()) {
      return send(res, 403, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...(token ? expirePending() : {}) });
    }
    const pending = store.getSession(hash(pendingSid), now());
    if (!pending || expiration > pending.expires_at) return send(res, 403, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...expirePending() });
    let authorization;
    try {
      authorization = await scm.authorize(pending.assertion, 'session');
    } catch (error) {
      if (error?.status !== 401 && error?.status !== 403) {
        return send(res, 503, recoveryPage('SCM no está disponible. Reintenta en unos segundos.', config.scmFormOrigin ? `${config.scmFormOrigin}/mi-agente` : null), { 'Content-Type': 'text/html; charset=utf-8' });
      }
      store.deleteSession(pending.sid_hash);
      return send(res, error.status, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...expirePending() });
    }
    const binding = sessionBinding(authorization);
    if (binding.authUserId !== pending.auth_user_id
      || String(binding.actorId) !== String(pending.actor_id)
      || binding.agentId !== pending.agent_id
      || !manifestAllows(manifest, authorization)) {
      store.deleteSession(pending.sid_hash);
      return send(res, 403, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...expirePending() });
    }
    const sessionExpiration = Math.min(
      pending.expires_at,
      Math.floor(Date.parse(authorization.expires_at) / 1000),
      now() + config.maxSessionSeconds,
    );
    if (!Number.isFinite(sessionExpiration) || sessionExpiration <= now()) {
      store.deleteSession(pending.sid_hash);
      return send(res, 403, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...expirePending() });
    }
    if (!store.consumeAssertion(hash(`continue:${token}`), pending.expires_at)) {
      return send(res, 403, JSON.stringify({ error: 'continuation_denied' }), { 'Content-Type': 'application/json', ...expirePending() });
    }
    const created = now();
    const marker = ownerMarker(config, binding.authUserId);
    const headers = {
      Location: `/new?agent=${encodeURIComponent(binding.agentId)}`,
      'Set-Cookie': [
        `${config.cookieName}=${encodeURIComponent(pendingSid)}; Max-Age=${Math.max(1, sessionExpiration - created)}; Path=/; HttpOnly; Secure; SameSite=Lax`,
        `${PENDING_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`,
        `${OWNER_MARKER_COOKIE_NAME}=${encodeURIComponent(marker)}; Max-Age=${OWNER_MARKER_MAX_AGE}; Path=/; HttpOnly; Secure; SameSite=Lax`,
      ],
    };
    if (!constantTimeEqual(cookies[OWNER_MARKER_COOKIE_NAME], marker)) headers['Clear-Site-Data'] = '"storage"';
    return send(res, 303, '', headers);
  };

  const proxyHttp = async (req, res, session) => {
    let target;
    try { target = new URL(req.url, config.nativeHttpOrigin); } catch { return sendJson(res, 400, { error: 'target_not_allowed' }); }
    if (target.origin !== config.nativeHttpOrigin || !safeQuery(target)) return sendJson(res, 400, { error: 'target_not_allowed' });
    if (!allowedNativeRoute(req.method, target, session.authorization.agent_id, req.url)) return sendJson(res, 404, { error: 'route_not_allowed' });
    let body;
    try { body = await readBody(req, config.maxBodyBytes); } catch (error) { return sendJson(res, error.status || 413, { error: 'request_too_large' }); }
    try {
      const response = await fetch(target, {
        method: req.method,
        headers: upstreamHeaders(req, session.authorization),
        body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
        signal: AbortSignal.timeout(config.apiTimeoutMs),
      });
      const responseHeaders = {};
      response.headers.forEach((value, key) => {
        if (!['set-cookie', 'content-length', 'content-encoding', 'connection', 'transfer-encoding'].includes(key)) responseHeaders[key] = value;
      });
      responseHeaders['Cache-Control'] = 'no-store';
      res.writeHead(response.status, securityHeaders(responseHeaders));
      if (response.body) Readable.fromWeb(response.body).pipe(res); else res.end();
    } catch {
      sendJson(res, 502, { error: 'native_unavailable' });
    }
  };

  const handleDownloadRoute = async (req, res, session, requestUrl) => {
    const match = /^\/api\/artifacts\/download\/scm\/([A-Za-z0-9_-]{32,128})$/.exec(requestUrl.pathname);
    if (!match || requestUrl.search) { sendJson(res, 404, { error: 'download_not_found' }); return true; }
    if (!['GET', 'HEAD'].includes(req.method)) { sendJson(res, 405, { error: 'download_method_not_allowed' }); return true; }
    const allowedHeaders = new Set(['range', 'if-range', 'if-none-match']);
    const seenHeaders = new Set();
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      const key = req.rawHeaders[i].toLowerCase();
      if (allowedHeaders.has(key)) {
        if (seenHeaders.has(key)) { sendJson(res, 400, { error: 'duplicate_download_header' }); return true; }
        seenHeaders.add(key);
      }
    }
    const entry = downloadStore.get(match[1]);
    if (!entry || entry.sidHash !== session.row.sid_hash || entry.agentId !== session.authorization.agent_id) {
      return sendJson(res, 404, { error: 'download_not_found' });
    }
    try {
      await artifactBroker.serveHandle({
        entry, req, res, authorization: session.authorization, sidHash: session.row.sid_hash, clientAddress: req.socket?.remoteAddress,
        reauthorize: async () => {
          const fresh = await getAuthorizedSession(req);
          if (fresh.kind === 'temporary') return fresh;
          return fresh.kind === 'authorized' && fresh.row.sid_hash === session.row.sid_hash ? fresh : null;
        },
      });
    } catch (error) {
      if (!res.headersSent) sendJson(res, error instanceof DownloadError ? error.status : 502, { error: error instanceof DownloadError ? error.message : 'download_unavailable' });
      else res.destroy();
    }
    return true;
  };

  const onRequest = async (req, res) => {
    res.setHeader('Referrer-Policy', 'no-referrer');
    if (req.url === '/healthz' && req.method === 'GET') return sendJson(res, 200, { status: 'ok' });
    if (req.url === '/_scm/exchange' && req.method === 'POST') return exchange(req, res);
    if (req.url === '/_scm/continue') return continueExchange(req, res);
    if (req.url === '/_scm/logout' && req.method === 'POST') {
      if (!originAllowed(req, config)) return sendJson(res, 403, { error: 'logout_denied' });
      const logoutCookies = parseCookies(req.headers.cookie || '');
      const logoutSession = await getAuthorizedSession(req);
      if (logoutSession.kind === 'temporary') return sendJson(res, 503, { error: 'scm_unavailable' });
      const sid = logoutCookies[config.cookieName];
      if (sid && /^[A-Za-z0-9_-]{32,128}$/.test(sid)) store.deleteSession(hash(sid));
      const pending = logoutCookies[PENDING_COOKIE_NAME];
      const [pendingSid] = typeof pending === 'string' ? pending.split('.') : [];
      if (pendingSid && /^[A-Za-z0-9_-]{32,128}$/.test(pendingSid)) store.deleteSession(hash(pendingSid));
      if (logoutSession.kind !== 'authorized' && !pending) return sendJson(res, 403, { error: 'logout_denied' });
      res.setHeader('Set-Cookie', [
        `${config.cookieName}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`,
        `${PENDING_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`,
        `${OWNER_MARKER_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`,
      ]);
      return sendJson(res, 200, { status: 'signed_out' });
    }
    const authorizationResult = await getAuthorizedSession(req);
    if (authorizationResult.kind === 'temporary') {
      return req.method === 'GET' ? send(res, 503, recoveryPage('SCM no está disponible. Reintenta en unos segundos.'))
        : sendJson(res, 503, { error: 'scm_unavailable' });
    }
    if (authorizationResult.kind !== 'authorized') {
      if (config.scmFormOrigin && ['GET', 'HEAD'].includes(req.method)) {
        let requestUrl;
        try { requestUrl = new URL(req.url, config.allowedOrigin); } catch { requestUrl = null; }
        const spaEntry = requestUrl && !hasPathTraversal(req.url)
          && (requestUrl.pathname === '/' || requestUrl.pathname === '/new' || requestUrl.pathname === '/chat' || requestUrl.pathname.startsWith('/chat/'));
        const configEntry = requestUrl
          && (requestUrl.pathname === '/control-ui-config.json' || requestUrl.pathname === '/__openclaw/control-ui-config.json');
        if (spaEntry || configEntry) {
          return sendRedirect(res, `${config.scmFormOrigin}/mi-agente`);
        }
      }
      res.setHeader('Set-Cookie', `${config.cookieName}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`);
      if (req.method === 'GET') return send(res, 401, recoveryPage());
      return sendJson(res, 401, { error: 'session_expired' });
    }
    const session = authorizationResult;
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method) && req.headers.origin !== config.allowedOrigin) {
      return sendJson(res, 403, { error: 'origin_not_allowed' });
    }
    let requestUrl;
    try { requestUrl = new URL(req.url, config.allowedOrigin); } catch { return sendJson(res, 400, { error: 'request_invalid' }); }
    if (requestUrl.pathname.startsWith('/api/artifacts/download/scm/')) return handleDownloadRoute(req, res, session, requestUrl);
    return proxyHttp(req, res, session);
  };

  const onUpgrade = async (req, socket, head) => {
    let requestUrl;
    try { requestUrl = new URL(req.url, config.allowedOrigin); } catch { socket.destroy(); return; }
    if (requestUrl.search || !originAllowed(req, config) || requestUrl.pathname === '/_scm/exchange') {
      socket.destroy(); return;
    }
    const authorizationResult = await getAuthorizedSession(req);
    if (authorizationResult.kind === 'temporary') {
      wsServer.handleUpgrade(req, socket, head, (client) => client.close(1013, 'SCM no disponible'));
      return;
    }
    if (authorizationResult.kind !== 'authorized') return handleExpiredUpgrade(req, socket, head);
    const session = authorizationResult;
    const clientHeaders = upstreamHeaders(req, session.authorization);
    wsServer.handleUpgrade(req, socket, head, (client) => {
      const upstream = new WebSocket(config.nativeWsUrl, {
        headers: clientHeaders,
        maxPayload: config.maxWsPayload,
        origin: config.allowedOrigin,
      });
      let opened = false;
      const pending = [];
      const deny = () => {
        if (client.readyState === WebSocket.OPEN) client.close(4403, 'Acceso retirado');
        if (upstream.readyState === WebSocket.OPEN || upstream.readyState === WebSocket.CONNECTING) upstream.close(4403, 'Acceso retirado');
      };
      const forward = async (data, isBinary) => {
        if (isBinary) { client.close(1003, 'Binario no permitido'); upstream.close(); return; }
        let fresh;
        try { fresh = await getAuthorizedSession(req); } catch { fresh = { kind: 'temporary' }; }
        if (fresh.kind === 'temporary') {
          client.close(1013, 'SCM no disponible');
          if (upstream.readyState === WebSocket.OPEN || upstream.readyState === WebSocket.CONNECTING) upstream.close(1013, 'SCM no disponible');
          return;
        }
        if (fresh.kind !== 'authorized' || fresh.row.sid_hash !== session.row.sid_hash) { deny(); return; }
        if (frameTargetsOtherAgent(data, fresh.authorization.agent_id)) { deny(); return; }
        let frame;
        try { frame = JSON.parse(Buffer.isBuffer(data) ? data.toString('utf8') : String(data)); } catch { frame = null; }
        if (frame?.type === 'req' && frame.method === 'artifacts.download') {
          try {
            const result = await artifactBroker.downloadRpc({
              frame, authorization: fresh.authorization, sidHash: fresh.row.sid_hash, clientAddress: req.socket?.remoteAddress,
              reauthorize: async () => {
                const latest = await getAuthorizedSession(req);
                if (latest.kind === 'temporary') return latest;
                return latest.kind === 'authorized' && latest.row.sid_hash === session.row.sid_hash ? latest : null;
              },
            });
            if (client.readyState === WebSocket.OPEN) client.send(JSON.stringify(result));
          } catch (error) {
            if (client.readyState === WebSocket.OPEN) {
              const downloadError = error instanceof DownloadError;
              const responseError = { code: downloadError ? error.message : 'artifact_download_denied' };
              responseError.message = downloadError && error.status === 503
                ? 'SCM no responde temporalmente. Reintenta la descarga.'
                : 'No se pudo descargar este archivo.';
              if (downloadError && error.status === 503) responseError.retryable = true;
              client.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: responseError }));
            }
          }
          return;
        }
        if (!opened) {
          if (pending.length >= 1) { client.close(1009, 'Demasiados frames pendientes'); upstream.close(); return; }
          pending.push(data); return;
        }
        if (upstream.readyState !== WebSocket.OPEN) return;
        upstream.send(data);
      };
      let frameChain = Promise.resolve();
      client.on('message', (data, isBinary) => {
        frameChain = frameChain.then(() => forward(data, isBinary)).catch(() => deny());
      });
      client.on('close', () => { if (upstream.readyState === WebSocket.OPEN) upstream.close(); });
      client.on('error', () => { if (upstream.readyState === WebSocket.OPEN) upstream.close(); });
      upstream.on('open', () => {
        opened = true;
        for (const data of pending.splice(0)) forward(data, false);
      });
      upstream.on('message', (data, isBinary) => {
        if (client.readyState !== WebSocket.OPEN) return;
        client.send(isBinary ? data : filterNativeFrame(data, session.authorization.agent_id), { binary: isBinary });
      });
      upstream.on('close', (code, reason) => { if (client.readyState === WebSocket.OPEN) client.close(code, reason); });
      upstream.on('error', () => { if (client.readyState === WebSocket.OPEN) client.close(1011, 'Nativo no disponible'); });
    });
  };

  const server = http.createServer((req, res) => {
    onRequest(req, res).catch(() => {
      if (!res.headersSent) sendJson(res, 503, { error: 'bridge_unavailable' });
      else res.destroy();
    });
  });
  server.on('upgrade', (req, socket, head) => {
    onUpgrade(req, socket, head).catch(() => socket.destroy());
  });
  return {
    server,
    store,
    listen: (...args) => server.listen(...args),
    close: async () => {
      for (const client of wsServer.clients) client.close(1001, 'Bridge shutting down');
      wsServer.close();
      clearInterval(purgeTimer);
      downloadStore.clear?.();
      await new Promise((resolve) => server.close(resolve));
      store.close();
    },
  };
};

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const config = loadConfig();
  const bridge = createBridgeServer(config);
  bridge.listen(config.port, config.host);
}
