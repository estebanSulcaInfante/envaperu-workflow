import { createHash, randomBytes } from 'node:crypto';
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

const hash = (value) => createHash('sha256').update(value).digest('hex');
const now = () => Math.floor(Date.now() / 1000);

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

const recoveryPage = (message = 'La sesión del agente no está disponible. Vuelve a SCM y reintenta.') => (
  `<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Acceso no disponible</title></head>`
  + `<body><main><h1>Acceso no disponible</h1><p>${message}</p></main></body></html>`
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
const MAX_QUERY_BYTES = 8192;
const MAX_QUERY_VALUE = 2048;

const hasPathTraversal = (rawUrl) => /(?:^|[\\/])\.\.?(?:[\\/]|$)|%2e|%2f|%5c/i.test(String(rawUrl || '').split('?')[0]);
const ownSessionKey = (value, agentId) => typeof value === 'string' && value.startsWith(`agent:${agentId}:`);
const singletonQuery = (url, agentId, allowed) => {
  if (Buffer.byteLength(url.search, 'utf8') > MAX_QUERY_BYTES) return false;
  for (const key of url.searchParams.keys()) if (!allowed.has(key)) return false;
  for (const key of allowed) {
    const values = url.searchParams.getAll(key);
    if (values.length > 1 || values.some((value) => value.length > MAX_QUERY_VALUE)) return false;
  }
  if (url.searchParams.has('agent') && url.searchParams.get('agent') !== agentId) return false;
  if (url.searchParams.has('agentId') && url.searchParams.get('agentId') !== agentId) return false;
  if (url.searchParams.has('sessionKey') && !ownSessionKey(url.searchParams.get('sessionKey'), agentId)) return false;
  return true;
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
    if (!singletonQuery(target, ownAgentId, new Set(['agent', 'sessionKey']))) return false;
    const chatId = pathname.startsWith('/chat/') ? pathname.slice('/chat/'.length).split('/')[0] : '';
    return !chatId || !chatId.startsWith('scm-') || chatId === ownAgentId;
  }
  if (pathname === '/control-ui-config.json' || pathname === '/__openclaw/control-ui-config.json'
    || pathname === '/favicon.ico' || pathname === '/favicon.svg' || pathname === '/favicon-32.png'
    || pathname === '/apple-touch-icon.png' || pathname === '/manifest.json'
    || pathname === '/manifest.webmanifest' || pathname === '/sw.js' || pathname === '/asset-manifest.json') return target.search === '';
  if (STATIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    const extension = pathname.slice(pathname.lastIndexOf('/')).match(/\.[^.]+$/)?.[0]?.toLowerCase();
    return Boolean(extension && STATIC_EXTENSIONS.has(extension) && target.search === '');
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
  if (frame?.method !== 'agents.list' && !(frame?.type === 'res' && frame?.payload?.agents)) return data;
  if (Array.isArray(frame?.payload?.agents)) {
    frame.payload.agents = frame.payload.agents.filter((agent) => agent?.id === ownAgentId);
    if (Object.prototype.hasOwnProperty.call(frame.payload, 'defaultId')) frame.payload.defaultId = ownAgentId;
  }
  return JSON.stringify(frame);
};

export const createBridgeServer = (config, dependencies = {}) => {
  const store = dependencies.store || new SessionStore(config.dbPath);
  const scm = dependencies.scm || new ScmAccessClient(config, dependencies.fetchImpl || fetch);
  const manifest = dependencies.manifest
    || (config.provisionMode === 'manifest' ? loadManifest(config.nativeManifestPath) : null);
  const provisioner = dependencies.provisioner || provisionNativeIdentity;
  const provisioning = new Map();
  const purgeTimer = setInterval(() => store.purgeExpired?.(), 60_000);
  purgeTimer.unref?.();
  const wsServer = new WebSocketServer({ noServer: true, maxPayload: config.maxWsPayload });

  const getAuthorizedSession = async (req) => {
    const sid = parseCookies(req.headers.cookie || '')[config.cookieName];
    if (!sid || !/^[A-Za-z0-9_-]{32,128}$/.test(sid)) return null;
    const row = store.getSession(hash(sid));
    if (!row) return null;
    let authorization;
    try { authorization = await scm.authorize(row.assertion, 'session'); } catch {
      store.deleteSession(row.sid_hash);
      return null;
    }
    const binding = sessionBinding(authorization);
    if (binding.authUserId !== row.auth_user_id
      || String(binding.actorId) !== String(row.actor_id)
      || binding.agentId !== row.agent_id
      || !manifestAllows(manifest, authorization)) {
      store.deleteSession(row.sid_hash);
      return null;
    }
    const nextIdle = Math.min(now() + config.maxIdleSeconds, row.expires_at);
    store.touchSession(row.sid_hash, nextIdle);
    return { row, authorization, sid };
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
      return sendJson(res, error.status === 401 ? 401 : 403, { error: 'exchange_denied' });
    }
    try {
      await ensureProvisioned(authorization, req.socket?.remoteAddress);
      const refreshed = await scm.authorize(assertion, 'session');
      if (!manifestAllows(manifest, refreshed)
        || refreshed.identity.auth_user_id !== authorization.identity.auth_user_id
        || refreshed.agent_id !== authorization.agent_id) throw new ScmAccessError('session binding changed', 403);
      authorization = refreshed;
    } catch (error) {
      return sendJson(res, error.status === 401 ? 401 : 403, { error: 'provisioning_denied' });
    }
    const expiration = Math.min(
      Math.floor(Date.parse(authorization.expires_at) / 1000),
      now() + config.maxSessionSeconds,
    );
    if (!Number.isFinite(expiration) || expiration <= now() || !store.consumeAssertion(hash(authorization.jti), expiration)) {
      return sendJson(res, 403, { error: 'assertion_expired_or_consumed' });
    }
    const sid = randomBytes(32).toString('base64url');
    const created = now();
    store.createSession({
      sidHash: hash(sid),
      assertion,
      ...sessionBinding(authorization),
      expiresAt: expiration,
      idleExpiresAt: Math.min(created + config.maxIdleSeconds, expiration),
      createdAt: created,
    });
    const maxAge = Math.max(1, expiration - created);
    const cookie = `${config.cookieName}=${encodeURIComponent(sid)}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;
    return send(res, 303, '', {
      Location: `/new?agent=${encodeURIComponent(authorization.agent_id)}`,
      'Set-Cookie': cookie,
    });
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

  const onRequest = async (req, res) => {
    res.setHeader('Referrer-Policy', 'no-referrer');
    if (req.url === '/healthz' && req.method === 'GET') return sendJson(res, 200, { status: 'ok' });
    if (req.url === '/_scm/exchange' && req.method === 'POST') return exchange(req, res);
    if (req.url === '/_scm/logout' && req.method === 'POST') {
      if (!originAllowed(req, config) || !(await getAuthorizedSession(req))) return sendJson(res, 403, { error: 'logout_denied' });
      const sid = parseCookies(req.headers.cookie || '')[config.cookieName];
      store.deleteSession(hash(sid));
      res.setHeader('Set-Cookie', `${config.cookieName}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`);
      return sendJson(res, 200, { status: 'signed_out' });
    }
    const session = await getAuthorizedSession(req);
    if (!session) {
      res.setHeader('Set-Cookie', `${config.cookieName}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`);
      if (req.method === 'GET') return send(res, 401, recoveryPage());
      return sendJson(res, 401, { error: 'session_expired' });
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method) && req.headers.origin !== config.allowedOrigin) {
      return sendJson(res, 403, { error: 'origin_not_allowed' });
    }
    return proxyHttp(req, res, session);
  };

  const onUpgrade = async (req, socket, head) => {
    let requestUrl;
    try { requestUrl = new URL(req.url, config.allowedOrigin); } catch { socket.destroy(); return; }
    if (requestUrl.search || !originAllowed(req, config) || requestUrl.pathname === '/_scm/exchange') {
      socket.destroy(); return;
    }
    const session = await getAuthorizedSession(req);
    if (!session) { socket.destroy(); return; }
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
        try { fresh = await getAuthorizedSession(req); } catch { fresh = null; }
        if (!fresh || fresh.row.sid_hash !== session.row.sid_hash) { deny(); return; }
        if (frameTargetsOtherAgent(data, fresh.authorization.agent_id)) { deny(); return; }
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
