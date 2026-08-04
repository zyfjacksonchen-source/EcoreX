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

test("update notification appears only after verified preparation", () => {
  assert.match(app, /update\.state === "awaiting_user"/u);
  assert.match(app, /update\.can_activate/u);
  assert.doesNotMatch(app, /正在后台准备 e-Mate/u);
  assert.match(settings, /runtimeUpdateStatusText/u);
  assert.match(settings, /onCheckUpdate/u);
});

test("activation waits for target health and replaces the stale document", () => {
  assert.match(session, /import\("\.\/updateActivationHandoff\.ts"\)/u);
  assert.match(handoff, /bootstrap\.update\.current_version === options\.targetVersion/u);
  assert.match(handoff, /options\.replace\(next\.toString\(\)\)/u);
});
