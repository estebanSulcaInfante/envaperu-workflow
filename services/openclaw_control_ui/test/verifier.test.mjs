import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createRuntimeBuildInfo } from "../scripts/generate-build-info.mjs";
import { verifyContext } from "../scripts/verify-context.mjs";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const metadata = JSON.parse(fs.readFileSync(path.join(packageRoot, "release-metadata.json"), "utf8"));
const patchedFiles = [
  "ui/src/app/browser-auth-recovery.test.ts",
  "ui/src/app/browser-auth-recovery.ts",
  "ui/src/components/app-sidebar-session-catalog-live.test.ts",
  "ui/src/components/app-sidebar-session-catalog-live.ts",
  "ui/src/components/login-gate.test.ts",
  "ui/src/components/login-gate.ts",
  "ui/src/i18n/locales/en-login.ts",
  "ui/src/pages/chat/chat-pane-render.ts",
  "ui/src/pages/chat/chat-pane-pull-requests.test.ts",
];

test("runtime build info preserves the overlay identity", () => {
  assert.deepEqual(createRuntimeBuildInfo(metadata), {
    version: "2026.9.8",
    commit: "fc23bc864e4553c2d215e479eeec47b67a0bf943",
    builtAt: "2026-10-30T20:13:38.041Z",
    buildId: "2026.9.8-scm-ec3ed1dd646d2c3e",
  });
});

test("context verifier rejects an unallowlisted runtime file", () => {
  const context = fs.mkdtempSync(path.join(packageRoot, ".test-context-"));
  try {
    fs.mkdirSync(path.join(context, "dist", "control-ui"), { recursive: true });
    fs.writeFileSync(path.join(context, "Dockerfile"), "COPY dist/control-ui/ /app/dist/control-ui/\nCOPY dist/build-info.json /app/dist/build-info.json\n");
    fs.writeFileSync(path.join(context, "dist", "build-info.json"), JSON.stringify(createRuntimeBuildInfo(metadata)));
    fs.writeFileSync(path.join(context, "release-metadata.json"), JSON.stringify(metadata));
    fs.writeFileSync(path.join(context, "artifact-manifest.json"), "{}\n");
    fs.writeFileSync(path.join(context, "unexpected-config.json"), "{}\n");
    assert.throws(() => verifyContext(context, metadata), /allowlist|unexpected/i);
  } finally {
    fs.rmSync(context, { recursive: true, force: true });
  }
});

test("patch metadata identifies the supplied diff", () => {
  assert.equal(metadata.upstream.patchSha256, "ec3ed1dd646d2c3e4f2c6092e308f18c4200972df35d4f9928b794487a3b25d1");
  assert.equal(metadata.overlay.buildId, `2026.9.8-scm-${metadata.upstream.patchSha256.slice(0, 16)}`);
  const patch = fs.readFileSync(path.join(packageRoot, "scm-personal-access.patch"));
  assert.equal(createHash("sha256").update(patch).digest("hex"), metadata.upstream.patchSha256);
});

test("source checkout contains no changes outside the supplied UI patch when configured", (t) => {
  const sourceRoot = process.env.OPENCLAW_SOURCE_ROOT;
  if (!sourceRoot) {
    t.skip("set OPENCLAW_SOURCE_ROOT for the source-checkout integration assertion");
    return;
  }
  const status = execFileSync("git", ["status", "--porcelain=v1", "--untracked-files=all"], {
    cwd: path.resolve(sourceRoot),
    encoding: "utf8",
  }).trimEnd();
  const expected = patchedFiles.map((file) => ` M ${file}`).sort().join("\n");
  assert.equal(status, expected);
});
