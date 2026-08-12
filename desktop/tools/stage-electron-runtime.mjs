import { spawn } from "node:child_process";
import { lstat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const option = (name) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
};
const targetPlatform = option("--platform") || (process.platform === "win32" ? "windows" : "macos");
const targetArchitecture = option("--arch") || (process.arch === "x64" ? "x64" : "arm64");
if (!new Set(["windows", "macos"]).has(targetPlatform) || !new Set(["x64", "arm64"]).has(targetArchitecture)) {
  throw new Error("Desktop Runtime target is invalid.");
}
if (targetPlatform === "windows" && targetArchitecture !== "x64") {
  throw new Error("Windows desktop supports x64 only.");
}

const targetBootstrapVariable = `EMATE_BOOTSTRAP_DIR_${targetPlatform.toUpperCase()}_${targetArchitecture.toUpperCase()}`;
const bootstrapRoot = path.resolve(process.env[targetBootstrapVariable] || process.env.EMATE_BOOTSTRAP_DIR || "");
const releaseRoot = path.resolve(process.env.EMATE_RELEASE_DIR || "");
const bootstrapConfig = path.join(bootstrapRoot, "bootstrap-config.json");
const destination = path.join(desktop, "runtime-bundle");
const requireFile = async (file, label) => {
  const metadata = await lstat(file).catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink()) throw new Error(`${label} must be a regular file.`);
};
if (!process.env[targetBootstrapVariable] && !process.env.EMATE_BOOTSTRAP_DIR) {
  throw new Error(`${targetBootstrapVariable} (or EMATE_BOOTSTRAP_DIR) is required.`);
}
if (!process.env.EMATE_RELEASE_DIR) throw new Error("EMATE_RELEASE_DIR is required.");
await requireFile(bootstrapConfig, "Bootstrap trust configuration");
await requireFile(path.join(releaseRoot, "release-manifest.json"), "Release manifest");

const python = process.env.EMATE_PYTHON || (process.platform === "win32" ? "python" : "python3");
const arguments_ = [
  path.join(desktop, "tools", "stage-direct-runtime.py"),
  "--platform", targetPlatform,
  "--architecture", targetArchitecture,
  "--bootstrap-config", bootstrapConfig,
  "--release-dir", releaseRoot,
  "--destination", destination,
];
const exitCode = await new Promise((resolve, reject) => {
  const child = spawn(python, arguments_, { cwd: path.dirname(desktop), stdio: "inherit" });
  child.once("error", reject);
  child.once("exit", (code, signal) => signal ? reject(new Error(`Runtime staging stopped by ${signal}.`)) : resolve(code));
});
if (exitCode !== 0) throw new Error(`Runtime staging failed with exit code ${exitCode}.`);
