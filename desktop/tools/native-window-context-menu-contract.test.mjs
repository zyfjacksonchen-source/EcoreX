import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (relative) => readFile(path.join(desktop, relative), "utf8");

test("desktop chrome uses native controls without renderer traffic lights", async () => {
  const [main, preload, layout] = await Promise.all([
    load("electron/main.cjs"),
    load("electron/preload.cjs"),
    load("src/v1/styles/layout.css"),
  ]);
  assert.match(main, /titleBarStyle: "hiddenInset"/);
  assert.match(main, /trafficLightPosition: \{ x: 14, y: 18 \}/);
  assert.match(main, /if \(process\.platform === "win32"\)[\s\S]*titleBarOverlay/u);
  assert.doesNotMatch(main, /frame:\s*false/u);
  assert.match(preload, /dataset\.desktopPlatform = process\.platform/);
  assert.match(layout, /-webkit-app-region: drag/);
  assert.match(layout, /-webkit-app-region: no-drag/);
  assert.match(layout, /data-desktop-platform="darwin"[\s\S]*\.ex-emate-logo,[\s\S]*data-desktop-platform="win32"[\s\S]*\.ex-emate-logo \{\s*display: block;\s*width: 112px;\s*height: 26px;/u);
  assert.match(layout, /data-desktop-platform="darwin"[\s\S]*\.ex-emate-mark-image,[\s\S]*data-desktop-platform="win32"[\s\S]*\.ex-emate-mark-image \{\s*display: none;/u);
  assert.doesNotMatch(layout, /traffic-light|window-controls/u);
});

test("native context menu copies text, images, links, and only verified user outputs", async () => {
  const [main, preload, app, artifacts] = await Promise.all([
    load("electron/main.cjs"),
    load("electron/preload.cjs"),
    load("src/v1/AppV1.tsx"),
    load("src/v1/components/ArtifactShelf.tsx"),
  ]);
  assert.match(main, /webContents\.on\("context-menu"/);
  assert.match(main, /setImmediate\(\(\) => popupPendingContextMenu/);
  assert.match(main, /ipcMain\.on\("emate:context-target"[\s\S]*popupPendingContextMenu\(event\.sender\)/u);
  assert.match(main, /role: "copy"/);
  assert.match(main, /copyImageAt\(params\.x, params\.y\)/);
  assert.match(main, /label: "复制链接地址"/);
  assert.match(main, /label: "保存并复制文件路径"/);
  assert.match(main, /event\.sender !== mainWindow\?\.webContents/);
  assert.match(main, /path\.join\(app\.getPath\("documents"\), "EcoreX"\)/);
  assert.match(main, /path\.join\(app\.getPath\("downloads"\), "EcoreX"\)/);
  assert.doesNotMatch(main, /app\.getPath\("home"\)/);
  assert.match(main, /stat\.isFile\(\) && !stat\.isSymbolicLink\(\)/);
  assert.match(preload, /closest\("\[data-emate-artifact-id\]"\)/);
  assert.match(preload, /ipcRenderer\.invoke\("emate:copy-materialized-path", receipt\)/);
  assert.match(app, /runtime\.downloadArtifact\(artifact\)/);
  assert.match(app, /copyMaterializedPath/);
  assert.match(artifacts, /data-emate-artifact-id=\{artifact\.artifact_id\}/);
  assert.match(artifacts, /data-emate-artifact-revision=\{artifact\.revision_id\}/);
});
