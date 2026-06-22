import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildBlockMap } from "app-builder-lib/out/targets/blockmap/blockmap.js";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptDir, "..");

function argValue(name) {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) {
    return process.argv[index + 1];
  }
  return "";
}

function desktopVersion() {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  return String(pkg.version || "").trim();
}

const version = argValue("--version") || process.env.ECOREX_VERSION || desktopVersion();
if (!version) {
  throw new Error("Unable to resolve EcoreX version");
}
const arch = (argValue("--arch") || process.env.ECOREX_WIN_ARCH || "x64").trim();
if (!["x64", "ia32"].includes(arch)) {
  throw new Error(`Unsupported Windows update arch: ${arch}`);
}

const setup = path.resolve(
  argValue("--setup") ||
    process.env.ECOREX_SETUP ||
    path.join(desktopRoot, "release", `EcoreX_${version}_${arch}-setup.exe`)
);
if (!fs.existsSync(setup)) {
  throw new Error(`Setup installer not found: ${setup}`);
}

const outDir = path.resolve(
  argValue("--out-dir") ||
    process.env.ECOREX_RELEASE_DIR ||
    (arch === "ia32" ? path.join(path.dirname(setup), "ia32") : path.dirname(setup))
);
fs.mkdirSync(outDir, { recursive: true });

const fileName = path.basename(setup);
const feedSetup = path.join(outDir, fileName);
if (path.resolve(setup) !== path.resolve(feedSetup)) {
  fs.copyFileSync(setup, feedSetup);
}
const blockmap = path.join(outDir, `${fileName}.blockmap`);
const result = await buildBlockMap(setup, "gzip", blockmap);
const latest = [
  `version: ${version}`,
  "files:",
  `  - url: ${fileName}`,
  `    sha512: ${result.sha512}`,
  `    size: ${result.size}`,
  `path: ${fileName}`,
  `sha512: ${result.sha512}`,
  `releaseDate: '${new Date().toISOString()}'`,
  "",
].join("\n");

const latestYml = path.join(outDir, "latest.yml");
fs.writeFileSync(latestYml, latest, "utf8");
console.log(
  JSON.stringify(
    {
      ok: true,
      version,
      arch,
      setup,
      blockmap,
      latestYml,
      size: result.size,
      sha512: result.sha512,
    },
    null,
    2
  )
);
