# OpenClaw Control UI SCM overlay

This package derives the official pinned OpenClaw image by rebuilding only the
Control UI from the pinned source checkout. The runtime image receives exactly
`dist/control-ui/` and `dist/build-info.json`; no gateway configuration, secret,
scope, feature, TTL, or runtime argument is added.

The source checkout must be at `fc23bc864e4553c2d215e479eeec47b67a0bf943` and
its applied nine-file diff must match `scm-personal-access.patch` byte-for-byte.
The default source path is the sibling `personal-agent-simple-20261030/openclaw-source`
checkout. Override it without changing metadata with `OPENCLAW_SOURCE_ROOT`.

From the backend checkout:

```powershell
node services/openclaw_control_ui/scripts/build-context.mjs 2>&1 | Tee-Object services/openclaw_control_ui/build-context.log
node --test services/openclaw_control_ui/test/verifier.test.mjs
docker build --tag openclaw-control-ui-scm:2026.9.8 --file services/openclaw_control_ui/.artifacts/2026.9.8-scm-ec3ed1dd646d2c3e/Dockerfile services/openclaw_control_ui/.artifacts/2026.9.8-scm-ec3ed1dd646d2c3e
```

The verifier is portable and does not assume a sibling checkout. Set
`OPENCLAW_SOURCE_ROOT` when running its optional source-status assertion or the
build-context integration; the build script then checks the pinned HEAD and
the exact nine-file patch before compiling.

The reproducible UI regression command uses the versioned harness and the
full source checkout (no personal path is embedded in the harness):

```powershell
$env:VITE_SCM_PERSONAL_ACCESS='false'
$env:OPENCLAW_SOURCE_ROOT = (Resolve-Path ..\..\personal-agent-simple-20261030\openclaw-source).Path
pnpm --dir $env:OPENCLAW_SOURCE_ROOT\ui exec vitest run --config $PWD\services\openclaw_control_ui\test\vitest.scm.config.mts --project unit --project unit-mock-registry src/app/browser-auth-recovery.test.ts src/components/login-gate.test.ts src/components/app-sidebar-session-catalog-live.test.ts src/pages/chat/chat-pane-pull-requests.test.ts
```

For the opt-in SCM browser recovery proof, run the complete harness again with
the flag enabled:

```powershell
$env:VITE_SCM_PERSONAL_ACCESS='true'
$env:OPENCLAW_SOURCE_ROOT = (Resolve-Path ..\..\personal-agent-simple-20261030\openclaw-source).Path
pnpm --dir $env:OPENCLAW_SOURCE_ROOT\ui exec vitest run --config $PWD\services\openclaw_control_ui\test\vitest.scm.config.mts --project unit --project unit-mock-registry src/app/browser-auth-recovery.test.ts src/components/login-gate.test.ts src/components/app-sidebar-session-catalog-live.test.ts src/pages/chat/chat-pane-pull-requests.test.ts
```

The opt-in HTTP test and the nine legacy iframe tests are explicitly skipped
by their flag conditions; all remaining regression tests still run.

The build context is emitted at `.artifacts/2026.9.8-scm-ec3ed1dd646d2c3e/`.
Its `release-metadata.json` records the base-image digest, upstream revision,
patch SHA, overlay build ID, and fixed build timestamp. `artifact-manifest.json`
hashes every allowlisted context byte. Docker labels repeat the upstream and
overlay identity; the runtime `build-info.json` uses the same `buildId` and
`builtAt`, so the UI overlay is distinguishable from the pinned image in the
Gateway hello/build identity without changing Gateway methods or scopes.
