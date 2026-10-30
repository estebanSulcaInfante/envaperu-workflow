import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const sourceRoot = process.env.OPENCLAW_SOURCE_ROOT?.trim();
if (!sourceRoot || !path.isAbsolute(sourceRoot)) {
  throw new Error("OPENCLAW_SOURCE_ROOT must be an absolute OpenClaw source checkout path");
}
const uiRoot = path.join(sourceRoot, "ui");
const sourceConfig = path.join(uiRoot, "vitest.config.ts");
if (!fs.existsSync(sourceConfig) || !fs.existsSync(path.join(uiRoot, "package.json"))) {
  throw new Error(`OPENCLAW_SOURCE_ROOT is not an OpenClaw checkout: ${sourceRoot}`);
}

const loaded = await import(pathToFileURL(sourceConfig).href);
export default loaded.default;
