import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/v1/AppV1.tsx", import.meta.url), "utf8");
const settings = await readFile(
  new URL("../src/v1/components/SettingsDialog.tsx", import.meta.url),
  "utf8",
);
const session = await readFile(
  new URL("../src/v1/state/useRuntimeSession.ts", import.meta.url),
  "utf8",
);
const handoff = await readFile(
  new URL("../src/v1/state/updateActivationHandoff.ts", import.meta.url),
  "utf8",
);

test("update notification keeps discovery and install behind one user action", () => {
  assert.match(app, /hasPendingRuntimeUpdate/u);
  assert.match(app, /update\?\.state !== "failed"/u);
  assert.match(app, /<progress aria-label="新版下载与安装进度"/u);
  assert.match(app, /下载并安装/u);
  assert.match(settings, /runtimeUpdateStatusText/u);
  assert.match(settings, /onCheckUpdate/u);
  assert.match(settings, /onActivateUpdate/u);
});

test("activation opens the healthy target in a new window with in-place fallback", () => {
  assert.match(session, /`emate-updated-runtime-\$\{crypto\.randomUUID\(\)\}`/u);
  assert.match(session, /import\("\.\/updateActivationHandoff\.ts"\)/u);
  assert.match(handoff, /bootstrap\.update\.current_version === options\.targetVersion/u);
  assert.match(handoff, /options\.openUpdatedRuntime/u);
  assert.match(handoff, /options\.replace\(next\.toString\(\)\)/u);
});
