import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createRuntimeBuildInfo, writeRuntimeBuildInfo } from "./generate-build-info.mjs";
import { verifyContext } from "./verify-context.mjs";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const metadataPath = path.join(packageRoot, "release-metadata.json");
const patchPath = path.join(packageRoot, "scm-personal-access.patch");
const metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8"));
const sourceRoot = path.resolve(process.env.OPENCLAW_SOURCE_ROOT ?? path.join(packageRoot, "../../../../personal-agent-simple-20261030/openclaw-source"));
const outputRoot = path.resolve(process.env.OPENCLAW_UI_CONTEXT ?? path.join(packageRoot, ".artifacts", metadata.overlay.buildId));
const changedFiles = [
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

function runGit(args, options = {}) {
  return execFileSync("git", args, { cwd: sourceRoot, encoding: options.encoding ?? "utf8", stdio: ["ignore", "pipe", "pipe"] });
}

function assertSourceAndPatch() {
  const head = runGit(["rev-parse", "HEAD"]).trim();
  if (head !== metadata.upstream.revision) throw new Error(`source HEAD ${head} does not equal pinned revision ${metadata.upstream.revision}`);
  const packageVersion = JSON.parse(fs.readFileSync(path.join(sourceRoot, "package.json"), "utf8")).version;
  if (packageVersion !== metadata.upstream.version) throw new Error(`source version ${packageVersion} does not equal ${metadata.upstream.version}`);
  const diff = runGit(["diff", "--binary", "HEAD", "--", ...changedFiles], { encoding: "buffer" });
  const patchBytes = fs.readFileSync(patchPath);
  if (!diff.equals(patchBytes)) throw new Error("source applied diff does not exactly match scm-personal-access.patch");
  const patchSha256 = createHash("sha256").update(patchBytes).digest("hex");
  if (patchSha256 !== metadata.upstream.patchSha256) throw new Error(`patch SHA ${patchSha256} does not match metadata`);
  const names = runGit(["diff", "--name-only", "HEAD", "--", ...changedFiles]).trim().split(/\r?\n/).filter(Boolean).sort();
  if (names.join("\n") !== [...changedFiles].sort().join("\n")) throw new Error("source diff changed an unexpected file");
  const status = runGit(["status", "--porcelain=v1", "--untracked-files=all"]).trimEnd();
  const expectedStatus = changedFiles.map((file) => ` M ${file}`).sort().join("\n");
  if (status !== expectedStatus) throw new Error("source checkout has changes outside the applied UI patch");
}

function buildUi() {
  const env = {
    ...process.env,
    VITE_SCM_PERSONAL_ACCESS: "true",
    GIT_COMMIT: metadata.upstream.revision,
    OPENCLAW_CONTROL_UI_BUILD_ID: metadata.overlay.buildId,
    OPENCLAW_BUILD_TIMESTAMP: metadata.overlay.builtAt,
    OPENCLAW_CONTROL_UI_RELEASE_BUILD: "1",
  };
  const command = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
  const spawnCommand = process.platform === "win32" ? (process.env.ComSpec ?? "cmd.exe") : command;
  const spawnArgs = process.platform === "win32" ? ["/d", "/s", "/c", `${command} ui:build`] : ["ui:build"];
  const result = spawnSync(spawnCommand, spawnArgs, {
    cwd: sourceRoot,
    env,
    stdio: "inherit",
  });
  if (result.error) throw new Error(`pnpm ui:build could not start: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`pnpm ui:build failed with exit ${result.status}`);
}

function copyTree() {
  const sourceDist = path.join(sourceRoot, "dist", "control-ui");
  if (!fs.existsSync(path.join(sourceDist, "index.html"))) throw new Error("UI build did not produce dist/control-ui/index.html");
  fs.rmSync(outputRoot, { recursive: true, force: true });
  fs.mkdirSync(path.join(outputRoot, "dist"), { recursive: true });
  fs.cpSync(sourceDist, path.join(outputRoot, "dist", "control-ui"), { recursive: true, dereference: true, errorOnExist: true });
  writeRuntimeBuildInfo(metadata, path.join(outputRoot, "dist", "build-info.json"));
  fs.copyFileSync(path.join(packageRoot, "Dockerfile"), path.join(outputRoot, "Dockerfile"));
  fs.copyFileSync(metadataPath, path.join(outputRoot, "release-metadata.json"));
}

function writeManifest() {
  const files = [];
  const visit = (current, relative) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const nextRelative = relative ? `${relative}/${entry.name}` : entry.name;
      const next = path.join(current, entry.name);
      if (entry.isDirectory()) visit(next, nextRelative);
      else files.push({
        path: nextRelative.replaceAll("\\", "/"),
        bytes: fs.statSync(next).size,
        sha256: createHash("sha256").update(fs.readFileSync(next)).digest("hex"),
      });
    }
  };
  visit(outputRoot, "");
  files.sort((left, right) => left.path.localeCompare(right.path));
  fs.writeFileSync(path.join(outputRoot, "artifact-manifest.json"), `${JSON.stringify({ schemaVersion: 1, files }, null, 2)}\n`);
}

assertSourceAndPatch();
buildUi();
copyTree();
writeManifest();
verifyContext(outputRoot, metadata);
process.stdout.write(`${JSON.stringify({ context: outputRoot, buildInfo: createRuntimeBuildInfo(metadata) }, null, 2)}\n`);
