import { verifyNativeOwnSession, withNativeArtifactDownload } from './native_admin.js';
import { authorizationIdentity } from './manifest.js';

const QUERY_KEYS = new Set(['sessionKey', 'runId', 'agentId', 'messageRole', 'artifactId', 'transport']);
const SAFE_MIMES = new Set([
  'application/pdf', 'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/octet-stream', 'text/csv', 'image/png', 'image/jpeg', 'image/webp',
]);

export class DownloadError extends Error {
  constructor(message, status = 404) { super(message); this.status = status; }
}

const fail = (message, status = 404) => { throw new DownloadError(message, status); };
const ownSessionKey = (value, agentId) => typeof value === 'string' && value.startsWith(`agent:${agentId}:`);
const closedParams = (params, allowed) => {
  if (!params || typeof params !== 'object' || Array.isArray(params)) fail('download_params_invalid', 400);
  for (const key of Object.keys(params)) if (!allowed.has(key)) fail('download_params_invalid', 400);
  for (const key of Object.keys(params)) if (Array.isArray(params[key])) fail('download_params_invalid', 400);
};
const validText = (value, max = 512) => typeof value === 'string' && value.length > 0 && value.length <= max;
const decodeBase64 = (value) => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) fail('inline_download_invalid', 502);
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value) fail('inline_download_invalid', 502);
  return bytes;
};
const summaryOf = (payload) => payload?.artifact || payload?.payload?.artifact;

const validateSummary = (summary, expected) => {
  if (!summary || summary.id !== expected.artifactId || summary.sessionKey !== expected.sessionKey
    || (summary.type && typeof summary.type !== 'string') || (summary.sizeBytes !== undefined && (!Number.isInteger(summary.sizeBytes) || summary.sizeBytes < 0))) {
    fail('artifact_not_owned', 404);
  }
  for (const key of ['title', 'source', 'messageSeq']) {
    if (expected[key] !== undefined && summary[key] !== expected[key]) fail('artifact_changed', 404);
  }
  if (summary.mimeType && !SAFE_MIMES.has(String(summary.mimeType).toLowerCase())) fail('artifact_mime_not_allowed', 415);
  if (expected.type !== undefined && summary.type !== expected.type) fail('artifact_changed', 404);
  if (expected.mimeType !== undefined && summary.mimeType !== expected.mimeType) fail('artifact_changed', 404);
  if (summary.download?.mode === 'unsupported') fail('artifact_unsupported', 404);
  return summary;
};

const nativeUrl = (value, config, expected) => {
  if (!validText(value, 2048) || !String(value).startsWith('/') || String(value).startsWith('//')) fail('native_url_invalid', 502);
  let parsed;
  try { parsed = new URL(String(value), config.nativeHttpOrigin); } catch { fail('native_url_invalid', 502); }
  if (parsed.origin !== config.nativeHttpOrigin || parsed.username || parsed.password || parsed.hash) fail('native_url_invalid', 502);
  if (/%2e|%2f|%5c/i.test(parsed.pathname)) fail('native_url_invalid', 502);
  const primary = /^\/api\/artifacts\/download\/[^/]+\/[^/]+$/.test(parsed.pathname) && parsed.search === '';
  const outgoing = /^\/api\/chat\/media\/outgoing\/([^/]+)\/([^/]+)\/full$/.exec(parsed.pathname);
  if (primary) return parsed;
  if (outgoing && parsed.searchParams.getAll('mediaTicket').length === 1 && parsed.searchParams.size === 1 && validText(parsed.searchParams.get('mediaTicket'), 512)) {
    if (decodeURIComponent(outgoing[1]) !== expected.sessionKey) fail('native_url_session_mismatch', 502);
    return parsed;
  }
  fail('native_url_invalid', 502);
};

const rpcFailure = (result) => !result?.ok && fail('native_download_denied', 404);

export class NativeArtifactBroker {
  constructor({ config, store, fetchImpl = fetch, verifyOwnSession = verifyNativeOwnSession, downloadExact = withNativeArtifactDownload }) {
    this.config = config;
    this.store = store;
    this.fetchImpl = fetchImpl;
    this.verifyOwnSession = verifyOwnSession;
    this.downloadExact = downloadExact;
    this.active = 0;
    this.activeBySid = new Map();
  }

  acquire(sidHash = 'unknown') {
    const sidCount = this.activeBySid.get(sidHash) || 0;
    if (this.active >= 8 || sidCount >= 2) fail('download_capacity_exhausted', 429);
    this.active += 1;
    this.activeBySid.set(sidHash, sidCount + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.active = Math.max(0, this.active - 1);
      const next = (this.activeBySid.get(sidHash) || 1) - 1;
      if (next <= 0) this.activeBySid.delete(sidHash); else this.activeBySid.set(sidHash, next);
    };
  }

  async downloadRpc({ frame, authorization, clientAddress, reauthorize, sidHash }) {
    const release = this.acquire(sidHash || authorization.identity?.auth_user_id || 'unknown');
    try { return await this._downloadRpc({ frame, authorization, clientAddress, reauthorize }); }
    finally { release(); }
  }

  async _downloadRpc({ frame, authorization, clientAddress, reauthorize }) {
    const params = frame?.params;
    closedParams(params, QUERY_KEYS);
    if (!validText(params.sessionKey) || !ownSessionKey(params.sessionKey, authorization.agent_id)
      || !validText(params.artifactId) || params.runId !== undefined
      || (params.agentId !== undefined && params.agentId !== authorization.agent_id)
      || (params.messageRole !== undefined && params.messageRole !== 'assistant') || (params.transport !== undefined && params.transport !== 'http')) {
      fail('artifact_ownership_required', 404);
    }
    const canonical = authorizationIdentity(authorization);
    if (!canonical || !(await this.verifyOwnSession({ config: this.config, canonical, sessionKey: params.sessionKey, clientAddress }))) fail('artifact_session_not_owned', 404);
    return this.downloadExact({ config: this.config, params: {
      sessionKey: params.sessionKey, agentId: authorization.agent_id, artifactId: params.artifactId,
      messageRole: 'assistant', ...(params.transport ? { transport: 'http' } : {}),
    }, consume: async (native) => {
      rpcFailure(native);
      const summary = validateSummary(summaryOf(native), params);
      const payload = { ...(native.payload || {}) };
      if (validText(payload.url, 2048)) {
      nativeUrl(payload.url, this.config, params);
      const parsedNativeExpiry = payload.expiresAt ? Date.parse(payload.expiresAt) : NaN;
      const nativeExpiry = Number.isFinite(parsedNativeExpiry) ? parsedNativeExpiry : Date.now() + 5 * 60 * 1000;
      const authExpiry = Date.parse(authorization.expires_at);
      if (!Number.isFinite(authExpiry)) fail('download_expiry_invalid', 502);
      const expiresAt = Math.min(nativeExpiry, authExpiry, Date.now() + 5 * 60 * 1000);
      const fresh = await reauthorize();
      if (!fresh || fresh.row.agent_id !== authorization.agent_id) fail('download_revoked', 404);
      const handle = this.store.issue({ sidHash: fresh.row.sid_hash, agentId: authorization.agent_id, sessionKey: params.sessionKey, artifactId: params.artifactId, summary, expiresAt });
      delete payload.data;
      delete payload.url;
      payload.url = `/api/artifacts/download/scm/${handle}`;
      payload.expiresAt = new Date(expiresAt).toISOString();
      } else if (payload.encoding === 'base64' && validText(payload.data, this.config.maxInlineBytes * 2)) {
        let bytes;
        try { bytes = decodeBase64(payload.data); } catch (error) { if (error instanceof DownloadError) throw error; fail('inline_download_invalid', 502); }
        if (bytes.length > this.config.maxInlineBytes) fail('inline_download_too_large', 413);
        if (summary.sizeBytes !== undefined && bytes.length !== summary.sizeBytes) fail('inline_download_size_invalid', 502);
        const fresh = await reauthorize();
        if (!fresh || fresh.row.agent_id !== authorization.agent_id) fail('download_revoked', 404);
      } else fail('inline_download_invalid', 502);
      return { type: 'res', id: frame.id, ok: true, payload };
    } });
  }

  async serveHandle({ entry, req, res, authorization, clientAddress, reauthorize, sidHash }) {
    const release = this.acquire(sidHash || entry.sidHash);
    try { return await this._serveHandle({ entry, req, res, authorization, clientAddress, reauthorize }); }
    finally { release(); }
  }

  async _serveHandle({ entry, req, res, authorization, clientAddress, reauthorize }) {
    const canonical = authorizationIdentity(authorization);
    if (!canonical || entry.agentId !== authorization.agent_id) fail('download_revoked', 404);
    if (!(await this.verifyOwnSession({ config: this.config, canonical, sessionKey: entry.sessionKey, clientAddress }))) fail('download_session_not_owned', 404);
    return this.downloadExact({ config: this.config, params: {
      sessionKey: entry.sessionKey, agentId: entry.agentId, artifactId: entry.artifactId, messageRole: 'assistant', transport: 'http',
    }, consume: async (native) => {
    rpcFailure(native);
    const summary = validateSummary(summaryOf(native), { ...entry.summary, ...entry });
    if (summary.mimeType !== entry.summary.mimeType || summary.sizeBytes !== entry.summary.sizeBytes) fail('artifact_changed', 404);
    const fresh = await reauthorize();
    if (!fresh || fresh.row.sid_hash !== entry.sidHash) fail('download_revoked', 404);
    if (req.method === 'GET' || req.method === 'HEAD') {
      const allowed = new Set(['range', 'if-range', 'if-none-match']);
      const seen = new Set();
      for (let i = 0; i < req.rawHeaders.length; i += 2) {
        const key = req.rawHeaders[i].toLowerCase();
        if (allowed.has(key)) { if (seen.has(key)) fail('duplicate_download_header', 400); seen.add(key); }
      }
    }
    const requestHeaders = {};
    for (const key of ['range', 'if-range', 'if-none-match']) if (req.headers[key]) requestHeaders[key] = req.headers[key];
    const url = nativeUrl(native.payload?.url, this.config, entry);
    const abort = new AbortController();
    const downloadSignal = AbortSignal.any([abort.signal, AbortSignal.timeout(this.config.apiTimeoutMs)]);
    const stop = () => abort.abort();
    req.once('aborted', stop); res.once('close', stop);
    let response;
    try {
      response = await this.fetchImpl(url, { method: req.method, headers: requestHeaders, redirect: 'error', signal: downloadSignal });
    } catch { fail('native_download_unavailable', 502); }
    if (![200, 206, 304, 416].includes(response.status)) fail('native_download_invalid_status', 502);
    const disposition = response.headers.get('content-disposition') || '';
    const mime = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (![304, 416].includes(response.status)) {
      if (!/^attachment(?:;|$)/i.test(disposition)) fail('native_download_disposition_invalid', 502);
      if (!SAFE_MIMES.has(mime) || mime !== String(entry.summary.mimeType).toLowerCase()) fail('native_download_mime_invalid', 502);
    }
    const rawLength = response.headers.get('content-length');
    if (rawLength !== null && (!/^\d+$/.test(rawLength) || !Number.isSafeInteger(Number(rawLength)))) fail('native_download_length_invalid', 502);
    const length = rawLength === null ? null : Number(rawLength);
    if (length > this.config.maxDownloadBytes || (entry.summary.sizeBytes !== undefined && length > entry.summary.sizeBytes)) fail('native_download_too_large', 413);
    const contentRange = response.headers.get('content-range');
    let expectedTotal = entry.summary.sizeBytes;
    let expectedBody = response.status === 200 ? expectedTotal : undefined;
    if (response.status === 206) {
      const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange || '');
      if (!match || Number(match[2]) < Number(match[1]) || (length !== null && length !== Number(match[2]) - Number(match[1]) + 1)) fail('native_download_range_invalid', 502);
      expectedTotal = Number(match[3]);
      expectedBody = Number(match[2]) - Number(match[1]) + 1;
      if (!Number.isSafeInteger(expectedTotal) || Number(match[2]) >= expectedTotal) fail('native_download_range_invalid', 502);
      if (entry.summary.sizeBytes !== undefined && expectedTotal !== entry.summary.sizeBytes) fail('artifact_changed', 404);
    }
    if (response.status === 416) {
      const match = /^bytes \*\/(\d+)$/.exec(contentRange || '');
      if (!match || (entry.summary.sizeBytes !== undefined && Number(match[1]) !== entry.summary.sizeBytes)) fail('native_download_range_invalid', 502);
    }
    if (expectedTotal !== undefined && expectedTotal > this.config.maxDownloadBytes) fail('native_download_too_large', 413);
    const headers = { ...(mime ? { 'Content-Type': mime } : {}), ...(disposition ? { 'Content-Disposition': disposition } : {}), 'Cache-Control': 'private, no-store', 'Content-Security-Policy': "default-src 'none'; sandbox", 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer' };
    for (const key of ['accept-ranges', 'content-range', 'etag', 'last-modified']) if (response.headers.get(key)) headers[key] = response.headers.get(key);
    if (response.headers.get('content-length')) headers['Content-Length'] = response.headers.get('content-length');
    res.writeHead(response.status, headers);
    if (req.method === 'HEAD' || [304, 416].includes(response.status) || !response.body) {
      req.removeListener('aborted', stop);
      res.removeListener('close', stop);
      return res.end();
    }
    const reader = response.body.getReader();
    let total = 0;
    const waitWritable = () => new Promise((resolve) => {
      if (res.destroyed || req.aborted || downloadSignal.aborted) return resolve(false);
      let settled = false;
      const finish = (writable) => {
        if (settled) return;
        settled = true;
        res.removeListener('drain', onDrain);
        res.removeListener('close', onClose);
        res.removeListener('error', onError);
        req.removeListener('aborted', onAbort);
        downloadSignal.removeEventListener('abort', onAbort);
        resolve(writable);
      };
      const onDrain = () => finish(true);
      const onClose = () => finish(false);
      const onError = () => finish(false);
      const onAbort = () => finish(false);
      res.once('drain', onDrain);
      res.once('close', onClose);
      res.once('error', onError);
      req.once('aborted', onAbort);
      downloadSignal.addEventListener('abort', onAbort, { once: true });
    });
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        total += part.value.byteLength;
        if (total > this.config.maxDownloadBytes || (expectedBody !== undefined && total > expectedBody)) { abort.abort(); res.destroy(); return; }
        if (!res.write(Buffer.from(part.value)) && !(await waitWritable())) { abort.abort(); res.destroy(); return; }
      }
      if (expectedBody !== undefined && total !== expectedBody) { res.destroy(); return; }
      res.end();
    } catch { if (!res.destroyed) res.destroy(); }
    finally {
      req.removeListener('aborted', stop);
      res.removeListener('close', stop);
    }
    } });
  }
}
