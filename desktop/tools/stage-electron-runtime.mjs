import { chmod, copyFile, lstat, mkdir, readFile, rm, writeFile } from "node:fs/promises";
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
const bootstrapValue = process.env[targetBootstrapVariable] || process.env.EMATE_BOOTSTRAP_DIR || "";
const bootstrapRoot = path.resolve(bootstrapValue);
const releaseRoot = path.resolve(process.env.EMATE_RELEASE_DIR || "");
const destination = path.join(desktop, "runtime-bundle");
const windows = targetPlatform === "windows";
const sourceExecutable = path.join(
  bootstrapRoot,
  "bin",
  windows ? "ecorex-bootstrap.exe" : "ecorex-bootstrap",
);

async function requireDirectory(directory, label) {
  const metadata = await lstat(directory).catch(() => null);
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error(`${label} must be a real directory.`);
  }
}

async function requireFile(file, label) {
  const metadata = await lstat(file).catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`${label} must be a regular file.`);
  }
}

if (!bootstrapValue || !process.env.EMATE_RELEASE_DIR) {
  throw new Error(`${targetBootstrapVariable} (or EMATE_BOOTSTRAP_DIR) and EMATE_RELEASE_DIR are required.`);
}
await requireDirectory(bootstrapRoot, "Bootstrap directory");
await requireDirectory(releaseRoot, "Release directory");
await requireFile(sourceExecutable, "Signed Bootstrap executable");
await requireFile(path.join(bootstrapRoot, "bootstrap-config.json"), "Bootstrap configuration");
const releaseManifestPath = path.join(releaseRoot, "release-manifest.json");
await requireFile(releaseManifestPath, "Release manifest");
JSON.parse(await readFile(path.join(bootstrapRoot, "bootstrap-config.json"), "utf8"));
const manifest = JSON.parse(await readFile(releaseManifestPath, "utf8"));
if (!Array.isArray(manifest.artifacts)) throw new Error("Release manifest artifacts are invalid.");
if (typeof manifest.release_id !== "string" || !/^release-stable-[0-9a-f]{24}$/.test(manifest.release_id)) {
  throw new Error("Release manifest identity is invalid.");
}
const selectedFiles = manifest.artifacts
  .filter((artifact) => (
    artifact?.platform === "all" && artifact?.architecture === "all"
  ) || (
    artifact?.platform === targetPlatform && artifact?.architecture === targetArchitecture
  ))
  .map((artifact) => artifact.file_name);
if (!selectedFiles.length || selectedFiles.some((name) => typeof name !== "string" || path.basename(name) !== name)) {
  throw new Error("Release manifest does not select bounded desktop artifacts.");
}
for (const name of selectedFiles) await requireFile(path.join(releaseRoot, name), `Release artifact ${name}`);

await rm(destination, { recursive: true, force: true });
await mkdir(path.join(destination, "bin"), { recursive: true, mode: 0o700 });
await copyFile(
  sourceExecutable,
  path.join(destination, "bin", windows ? "emate-backend.exe" : "emate-backend"),
);
if (!windows) await chmod(path.join(destination, "bin", "emate-backend"), 0o700);
await copyFile(
  path.join(bootstrapRoot, "bootstrap-config.json"),
  path.join(destination, "bootstrap-config.json"),
);
if (windows) {
  await requireFile(
    path.join(bootstrapRoot, "bin", "ecorex-sandbox-host.exe"),
    "Bootstrap sandbox helper",
  );
  await copyFile(
    path.join(bootstrapRoot, "bin", "ecorex-sandbox-host.exe"),
    path.join(destination, "bin", "ecorex-sandbox-host.exe"),
  );
}
const stagedRelease = path.join(destination, "releases", manifest.release_id);
await mkdir(stagedRelease, { recursive: true, mode: 0o700 });
await copyFile(releaseManifestPath, path.join(stagedRelease, "release-manifest.json"));
const evidenceFiles = ["release-metadata.json", "sbom.cdx.json"];
const evidencePresent = await Promise.all(evidenceFiles.map(async (name) => (
  (await lstat(path.join(releaseRoot, name)).catch(() => null))?.isFile() === true
)));
if (evidencePresent.some(Boolean) && !evidencePresent.every(Boolean)) {
  throw new Error("Release evidence must include both metadata and SBOM.");
}
if (evidencePresent.every(Boolean)) {
  for (const name of evidenceFiles) {
    await requireFile(path.join(releaseRoot, name), `Release evidence ${name}`);
    await copyFile(path.join(releaseRoot, name), path.join(stagedRelease, name));
  }
}
for (const name of selectedFiles) {
  await copyFile(path.join(releaseRoot, name), path.join(stagedRelease, name));
}
await writeFile(path.join(destination, "current-release"), `${manifest.release_id}\n`, {
  encoding: "utf8",
  mode: 0o600,
});
console.log(`Staged signed e-Mate Runtime bundle at ${destination}`);
