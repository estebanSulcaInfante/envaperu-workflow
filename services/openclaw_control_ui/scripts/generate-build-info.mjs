import fs from "node:fs";
import path from "node:path";

const FULL_SHA = /^[0-9a-f]{40}$/u;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u;

export function createRuntimeBuildInfo(metadata) {
  const version = metadata?.upstream?.version;
  const commit = metadata?.upstream?.revision;
  const builtAt = metadata?.overlay?.builtAt;
  const buildId = metadata?.overlay?.buildId;
  if (typeof version !== "string" || !version.trim()) {
    throw new Error("metadata.upstream.version is required");
  }
  if (typeof commit !== "string" || !FULL_SHA.test(commit)) {
    throw new Error("metadata.upstream.revision must be a full Git SHA");
  }
  if (typeof builtAt !== "string" || !UTC_TIMESTAMP.test(builtAt) || Number.isNaN(Date.parse(builtAt))) {
    throw new Error("metadata.overlay.builtAt must be a UTC ISO timestamp with milliseconds");
  }
  if (typeof buildId !== "string" || !buildId.trim()) {
    throw new Error("metadata.overlay.buildId is required");
  }
  return { version, commit, builtAt, buildId };
}

export function writeRuntimeBuildInfo(metadata, outputPath) {
  const buildInfo = createRuntimeBuildInfo(metadata);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(buildInfo, null, 2)}\n`);
  return buildInfo;
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname)) {
  const metadataPath = process.argv[2];
  const outputPath = process.argv[3];
  if (!metadataPath || !outputPath) {
    throw new Error("Usage: node generate-build-info.mjs <metadata.json> <output.json>");
  }
  writeRuntimeBuildInfo(JSON.parse(fs.readFileSync(metadataPath, "utf8")), outputPath);
}
