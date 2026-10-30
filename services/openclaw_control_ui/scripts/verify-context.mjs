import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { createRuntimeBuildInfo } from "./generate-build-info.mjs";

const ALLOWED_ROOT_FILES = new Set(["Dockerfile", "release-metadata.json", "artifact-manifest.json"]);
const ALLOWED_DIST_FILE = /^dist\/control-ui\/.+/u;
const ALLOWED_RUNTIME_FILE = "dist/build-info.json";

function relativeFiles(root) {
  const found = [];
  const visit = (current, relative) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
      const child = path.join(current, entry.name);
      if (entry.isSymbolicLink()) {
        throw new Error(`symlinks are not allowed in build context: ${childRelative}`);
      }
      if (entry.isDirectory()) visit(child, childRelative);
      else found.push(childRelative.replaceAll("\\", "/"));
    }
  };
  visit(root, "");
  return found.sort((left, right) => left.localeCompare(right));
}

function sha256(filePath) {
  return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function verifyContext(contextRoot, metadata) {
  const files = relativeFiles(contextRoot);
  for (const file of files) {
    if (!ALLOWED_ROOT_FILES.has(file) && file !== ALLOWED_RUNTIME_FILE && !ALLOWED_DIST_FILE.test(file)) {
      throw new Error(`unexpected file outside build-context allowlist: ${file}`);
    }
  }
  for (const required of ["Dockerfile", "release-metadata.json", "artifact-manifest.json", ALLOWED_RUNTIME_FILE]) {
    if (!files.includes(required)) throw new Error(`required context file is missing: ${required}`);
  }
  if (!files.some((file) => ALLOWED_DIST_FILE.test(file))) {
    throw new Error("dist/control-ui must contain compiled assets");
  }

  const contextMetadata = JSON.parse(fs.readFileSync(path.join(contextRoot, "release-metadata.json"), "utf8"));
  if (!sameJson(contextMetadata, metadata)) throw new Error("context release metadata does not match package metadata");
  const expectedBuildInfo = createRuntimeBuildInfo(metadata);
  const buildInfo = JSON.parse(fs.readFileSync(path.join(contextRoot, ALLOWED_RUNTIME_FILE), "utf8"));
  if (!sameJson(buildInfo, expectedBuildInfo)) throw new Error("runtime build-info does not match overlay metadata");

  const dockerfile = fs.readFileSync(path.join(contextRoot, "Dockerfile"), "utf8");
  if (!dockerfile.includes(`ARG OPENCLAW_BASE_IMAGE=${metadata.baseImage}`)) throw new Error("Dockerfile base image is not pinned to metadata");
  if (!dockerfile.includes("COPY dist/control-ui/ /app/dist/control-ui/")) throw new Error("Dockerfile must copy only control-ui assets");
  if (!dockerfile.includes("COPY dist/build-info.json /app/dist/build-info.json")) throw new Error("Dockerfile must copy runtime build-info");
  if (/COPY\s+\.\s/u.test(dockerfile) || /COPY\s+config(?:\/|\s)/iu.test(dockerfile)) throw new Error("Dockerfile copies an unallowlisted tree");

  const manifest = JSON.parse(fs.readFileSync(path.join(contextRoot, "artifact-manifest.json"), "utf8"));
  if (manifest.schemaVersion !== 1 || !Array.isArray(manifest.files)) throw new Error("invalid artifact manifest");
  const actualFiles = files.filter((file) => file !== "artifact-manifest.json");
  const expectedEntries = actualFiles.map((file) => ({
    path: file,
    bytes: fs.statSync(path.join(contextRoot, file)).size,
    sha256: sha256(path.join(contextRoot, file)),
  }));
  if (!sameJson(manifest.files, expectedEntries)) throw new Error("artifact manifest does not match context bytes");
  return { files: actualFiles, manifest };
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname)) {
  const contextRoot = process.argv[2];
  const metadataPath = process.argv[3];
  if (!contextRoot || !metadataPath) throw new Error("Usage: node verify-context.mjs <context> <metadata.json>");
  verifyContext(contextRoot, JSON.parse(fs.readFileSync(metadataPath, "utf8")));
  process.stdout.write(`verified ${contextRoot}\n`);
}
