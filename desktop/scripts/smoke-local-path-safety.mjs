#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(desktopRoot, "..");
const outputPath = path.resolve(process.argv[2] || path.join(repoRoot, "docs", "v0.1.18", "local-path-safety-smoke.json"));

const { PermissionManager } = await import(pathToFileURL(path.join(desktopRoot, "dist-electron", "permissions.js")).href);
const { openLocalPath, statLocalPath } = await import(pathToFileURL(path.join(desktopRoot, "dist-electron", "localPathBroker.js")).href);

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ecorex-local-path-safety-"));
const userData = path.join(tmpRoot, "user-data");
const home = path.join(tmpRoot, "home");
const appData = path.join(tmpRoot, "app-data");
const managedRoot = path.join(home, "EcoreX");
const selectedRoot = path.join(tmpRoot, "selected 项目");
const outsideRoot = path.join(tmpRoot, "outside");
await fsp.mkdir(userData, { recursive: true });
await fsp.mkdir(managedRoot, { recursive: true });
await fsp.mkdir(selectedRoot, { recursive: true });
await fsp.mkdir(outsideRoot, { recursive: true });

const managedChinese = path.join(managedRoot, "中文 空格.txt");
const selectedFile = path.join(selectedRoot, "chosen file.txt");
const outsideFile = path.join(outsideRoot, "outside.txt");
const dangerousFile = path.join(managedRoot, "run.ps1");
const realDangerous = path.join(outsideRoot, "real-script.ps1");
const symlinkToDangerous = path.join(managedRoot, "looks-safe.txt");
const missingFile = path.join(managedRoot, "missing.txt");
await fsp.writeFile(managedChinese, "hello", "utf8");
await fsp.writeFile(selectedFile, "selected", "utf8");
await fsp.writeFile(outsideFile, "outside", "utf8");
await fsp.writeFile(dangerousFile, "Write-Host unsafe", "utf8");
await fsp.writeFile(realDangerous, "Write-Host real unsafe", "utf8");
let symlinkAvailable = true;
let symlinkEvidence = "";
try {
  await fsp.symlink(realDangerous, symlinkToDangerous, "file");
  symlinkEvidence = `${symlinkToDangerous} -> ${realDangerous}`;
} catch (error) {
  symlinkAvailable = false;
  symlinkEvidence = `file symlink unavailable: ${error instanceof Error ? error.message : String(error)}`;
}

const permissions = new PermissionManager({
  appGetPath: (name) => {
    if (name === "userData") return userData;
    if (name === "home") return home;
    if (name === "appData") return appData;
    return tmpRoot;
  },
  browserWindowFromWebContents: () => undefined,
  showMessageBox: async () => ({ response: 2, checkboxChecked: false })
});
const shellBroker = {
  showItemInFolder: () => undefined,
  openPath: async () => "",
  openWith: async () => undefined
};

await permissions.setMode("smart-ask");
await permissions.rememberSelectedPaths([selectedRoot]);

const managedStat = await statLocalPath(permissions, managedChinese);
assert.equal(managedStat.status, "success", "managed Chinese/space path should be stat-readable");

const selectedStat = await statLocalPath(permissions, selectedFile);
assert.equal(selectedStat.status, "success", "previously selected path should be stat-readable");

const outsideStat = await statLocalPath(permissions, outsideFile);
assert.equal(outsideStat.status, "denied", "untrusted outside path should be denied for stat metadata");

const missingStat = await statLocalPath(permissions, missingFile);
assert.equal(missingStat.status, "missing", "managed missing path should report missing after auth check");

const relativeStat = await statLocalPath(permissions, "relative.txt");
assert.equal(relativeStat.status, "error", "relative stat path should be invalid");

const dangerousOpen = await openLocalPath(permissions, shellBroker, { sender: {} }, dangerousFile, "open");
assert.match(dangerousOpen, /^blocked:/, "dangerous extension should be blocked before open");

const dangerousReveal = await openLocalPath(permissions, shellBroker, { sender: {} }, dangerousFile, "reveal");
assert.equal(dangerousReveal, "", "dangerous extension may be revealed in folder");

const outsideOpen = await openLocalPath(permissions, shellBroker, { sender: {} }, outsideFile, "open");
assert.match(outsideOpen, /^denied:/, "untrusted outside open should be denied");

const relativeOpen = await openLocalPath(permissions, shellBroker, { sender: {} }, "relative.txt", "open");
assert.equal(relativeOpen, "invalid path", "relative open path should be invalid");

const missingOpen = await openLocalPath(permissions, shellBroker, { sender: {} }, missingFile, "open");
assert.equal(missingOpen, "path not found", "missing open path should report not found");

if (symlinkAvailable) {
  const symlinkOpen = await openLocalPath(permissions, shellBroker, { sender: {} }, symlinkToDangerous, "open");
  assert.match(symlinkOpen, /^blocked:/, "symlink realpath dangerous extension should be blocked");
}

await permissions.setMode("full-access");
const fullAccessOutsideStat = await statLocalPath(permissions, outsideFile);
assert.equal(fullAccessOutsideStat.status, "success", "full-access should allow outside stat metadata");

const sourcePath = path.join(desktopRoot, "electron", "localPathBroker.ts");
const distPath = path.join(desktopRoot, "dist-electron", "localPathBroker.js");
const sourceStat = await fsp.stat(sourcePath);
const distStat = await fsp.stat(distPath);
const distFresh = distStat.mtimeMs >= sourceStat.mtimeMs;
assert(distFresh, "dist-electron/localPathBroker.js must be newer than localPathBroker.ts; run npm run build:electron");

const symlinkStatus = symlinkAvailable ? "pass" : "fail";
const payload = {
  status: symlinkAvailable ? "pass" : "fail",
  version: "0.1.18",
  generatedAt: new Date().toISOString(),
  changeIds: ["STAB-003"],
  distFresh,
  checks: [
    { name: "managed Chinese and space path", status: "pass", evidence: managedChinese },
    { name: "selected path stat", status: "pass", evidence: selectedFile },
    { name: "outside path denied", status: "pass", evidence: outsideFile },
    { name: "missing path", status: "pass", evidence: missingFile },
    { name: "relative path invalid", status: "pass", evidence: "relative.txt" },
    { name: "dangerous open blocked", status: "pass", evidence: dangerousFile },
    { name: "dangerous reveal allowed", status: "pass", evidence: dangerousFile },
    { name: "symlink realpath guard", status: symlinkStatus, evidence: symlinkEvidence, symlinkAvailable },
    { name: "full-access stat allowed", status: "pass", evidence: outsideFile }
  ]
};

fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(JSON.stringify(payload, null, 2));

fs.rmSync(tmpRoot, { recursive: true, force: true });
