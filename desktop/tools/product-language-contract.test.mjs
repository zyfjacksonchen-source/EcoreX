import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const interaction = source("../src/v1/components/InteractionStack.tsx");
const app = source("../src/v1/AppV1.tsx");
const replay = source("../src/v1/components/ReplayDialog.tsx");
const retouch = source("../src/v1/components/RetouchWorkspace.tsx");
const extensionManager = source("../src/v1/components/ExtensionManagerDialog.tsx");
const settings = source("../src/v1/components/SettingsDialog.tsx");
const extensionSession = source("../src/v1/state/useExtensionSession.ts");
const extensionLabels = source("../src/v1/state/extensions.ts");

test("user-facing async failures use the controlled language boundary", () => {
  assert.match(interaction, /return userFacingError\(error\)/);
  assert.match(replay, /return userFacingError\(error\)/);
  assert.match(retouch, /return userFacingError\(error\)/);
  for (const candidate of [interaction, replay, retouch, extensionSession]) {
    assert.doesNotMatch(candidate, /\$\{error\.message\}|return error\.message/);
  }
});

test("default product copy hides implementation vocabulary and folds diagnostics", () => {
  const visibleComponents = [app, replay, extensionManager, settings];
  const forbiddenLiterals = [
    "从 Runtime 事件事实源",
    "Mock Replay",
    "Live Replay",
    "来源 Turn",
    "新 Turn",
    "Runtime 错误代码",
    "Runtime 未投影任何扩展",
    "MCP 服务",
    "SKILL.md",
    "策略租约",
    "正在同步 Runtime",
    "任务诊断与回放",
  ];
  for (const literal of forbiddenLiterals) {
    assert.equal(
      visibleComponents.some((candidate) => candidate.includes(literal)),
      false,
      `default product copy must not include ${literal}`,
    );
  }
  assert.match(replay, /<TechnicalDetails/);
  assert.match(extensionManager, /<TechnicalDetails/);
  assert.match(extensionLabels, /mcp_server: "扩展服务"/);
});
