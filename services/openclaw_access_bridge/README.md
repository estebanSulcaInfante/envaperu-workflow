# SCM OpenClaw access bridge

This standalone Node 24 service converts a short-lived SCM handoff assertion
into an opaque, server-side session and proxies only to the fixed native
OpenClaw HTTP/WebSocket origin. It calls the SCM authorization endpoint for
the exchange and before every protected HTTP request and WebSocket data frame.

Run locally with `npm ci` and `SCM_*` values from `config.example.env`; the
manifest must be supplied out of band and contain no PII. The SQLite path is
created with a private directory and file mode where supported. The service
does not accept Gateway tokens, Supabase JWTs, client identity headers, or
arbitrary upstream URLs.

The default bootstrap is per-person and derived only from the verified SCM
authorization identity. The bridge performs a trusted-proxy `connect`, then a
local admin `users.list`, `agents.create`, optimistic `config.patch`, and
`users.setRole` reconciliation. It validates the native deny-by-default role,
identity scope, agent workspace, model/runtime template, and read-only tool
allowlist before issuing the browser session. Until that reconciliation
succeeds, access remains denied. Set `SCM_NATIVE_PROVISION_MODE=manifest` only
for the compatibility path that uses the explicit manifest.

The HTTP proxy is a conservative native UI allowlist: unknown APIs,
outgoing/plugin/avatar/file routes, and HTTP uploads are blocked. Media is
accepted only for the bound agent and its own `agent:<id>:` session key;
attachments use the authenticated WebSocket `chat.send` path.

Known native 2026.9.8 limitation: the personal role intentionally grants only
session scopes. Secondary UI may report `missing scope: operator.read`; HTTP
legacy HTTP media previews and outgoing/plugin file routes remain blocked;
managed assistant artifacts use the reviewed, SID-bound download bridge. Do
not add global `operator.read` to hide this warning: native administrative/file
surfaces are broader than personal chat. Office/ZIP analysis and arbitrary
file generation require a separate reviewed slice.


Successful handoffs clear same-origin browser storage (not cookies) and normalize
hello/agent defaults to the bound personal agent. A browser profile/origin has
one active account: switching accounts in the same tab removes the previous
native UI cache. Use separate browser profiles for simultaneous accounts; this
is not a promise to revoke another already-open tab or profile.
