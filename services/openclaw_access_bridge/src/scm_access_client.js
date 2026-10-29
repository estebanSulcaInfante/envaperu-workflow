export class ScmAccessError extends Error {
  constructor(message, status = 503) { super(message); this.name = 'ScmAccessError'; this.status = status; }
}

export class ScmAccessClient {
  constructor(config, fetchImpl = fetch) { this.config = config; this.fetch = fetchImpl; }

  async authorize(assertion, phase, parentSignal) {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), this.config.apiTimeoutMs);
    const abort = () => timeout.abort();
    parentSignal?.addEventListener('abort', abort, { once: true });
    try {
      const response = await this.fetch(this.config.authorizationPath, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.config.serviceSecret}`,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify({ assertion, phase }),
        signal: timeout.signal,
      });
      let payload = null;
      try { payload = await response.json(); } catch { /* handled as failed closed below */ }
      if (!response.ok || !payload?.identity || !payload?.agent_id || !payload?.jti || !payload?.expires_at) {
        throw new ScmAccessError('SCM authorization denied', response.status || 503);
      }
      return payload;
    } catch (error) {
      if (error instanceof ScmAccessError) throw error;
      throw new ScmAccessError('SCM authorization unavailable', 503);
    } finally {
      clearTimeout(timer);
      parentSignal?.removeEventListener('abort', abort);
    }
  }
}
