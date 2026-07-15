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
const composer = source("../src/v1/components/Composer.tsx");
const modelSelector = source("../src/v1/components/ComposerModelSelector.tsx");
const timeline = source("../src/v1/components/Timeline.tsx");
const projectSelector = source("../src/v1/components/NewConversationProjectSelector.tsx");
const features = source("../src/v1/styles/features.css");
const layout = source("../src/v1/styles/layout.css");
const tokens = source("../src/styles/tokens.css");

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

test("Composer uses automatic intent routing with truthful paste and model controls", () => {
  assert.match(composer, /placeholder="给小芯发送消息，支持粘贴图片或文件"/u);
  assert.match(composer, /onPaste=/u);
  assert.match(modelSelector, /aria-label="选择模型"/u);
  assert.match(modelSelector, /图片模型 <small>按意图自动调用<\/small>/u);
  assert.doesNotMatch(composer, /aria-label="任务类型"|ex-mode-switch|onModeChange|TaskMode/u);
  assert.doesNotMatch(composer, />\s*办公\s*</u);
});

test("new conversation choices stay bounded and operational copy is progressive", () => {
  assert.match(projectSelector, /className="ex-menu ex-project-menu"/u);
  assert.match(projectSelector, /aria-label="选择项目会话"/u);
  assert.match(projectSelector, /添加项目文件夹…/u);
  assert.doesNotMatch(timeline, /ex-new-project-options/u);
  assert.match(composer, /<Tooltip\.Root delayDuration=\{900\}>/u);
  assert.match(composer, /需要权限或信息时会询问；长任务可排队，重启后继续。/u);
  assert.doesNotMatch(composer, /modelAvailable\s*\?\s*"需要权限或信息时会询问/u);
});

test("workspace chrome keeps the share action right aligned and removes the Composer divider", () => {
  assert.match(layout, /\.ex-workspace-header\s*\{[\s\S]*?grid-template-columns:\s*minmax\(160px, 1fr\) auto;/u);
  const composerRegion = features.match(/(?:^|\n)\.ex-composer-region\s*\{([^}]*)\}/u)?.[1] ?? "";
  assert.doesNotMatch(composerRegion, /border-top/u);
  assert.match(features, /\.ex-new-conversation-start\s*>\s*h1\s*\{[\s\S]*?font-size:\s*var\(--text-heading-size\);/u);
});

test("Codex theme values and EcoreX brand actions are separate semantic tokens", () => {
  assert.match(tokens, /--color-surface:\s*oklch\(1 0 0\);/u);
  assert.match(tokens, /--color-ink:\s*oklch\(0\.225591 0\.006566 258\.364\);/u);
  assert.match(tokens, /--color-accent:\s*oklch\(0\.682034 0\.173444 251\.11\);/u);
  assert.match(tokens, /--color-surface:\s*oklch\(0\.177638 0 0\);/u);
  assert.match(tokens, /--color-ink:\s*oklch\(0\.991069 0 0\);/u);
  assert.match(tokens, /--color-accent:\s*oklch\(0\.528649 0\.173447 254\.975\);/u);
  assert.match(tokens, /--color-brand:\s*oklch\(/u);
});
