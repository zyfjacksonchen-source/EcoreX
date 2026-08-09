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
const extensionManager = source("../src/v1/components/SkillsWorkspace.tsx");
const settings = source("../src/v1/components/SettingsDialog.tsx");
const extensionSession = source("../src/v1/state/useExtensionSession.ts");
const extensionLabels = source("../src/v1/state/extensions.ts");
const composer = source("../src/v1/components/Composer.tsx");
const homeDashboard = source("../src/v1/components/HomeDashboard.tsx");
const runtimeSession = source("../src/v1/state/useRuntimeSession.ts");
const modelSelector = source("../src/v1/components/ComposerModelSelector.tsx");
const connectorPopover = source("../src/v1/components/ConnectorPopover.tsx");
const timeline = source("../src/v1/components/Timeline.tsx");
const sidebar = source("../src/v1/components/Sidebar.tsx");
const login = source("../src/v1/components/LoginPage.tsx");
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
  assert.match(extensionManager, /selected\.exports/);
  assert.match(extensionManager, /selected\.dependencies/);
  assert.match(extensionLabels, /mcp_server: "扩展服务"/);
});

test("Composer uses automatic intent routing with truthful paste and model controls", () => {
  assert.match(composer, /placeholder="给小芯发送消息，支持粘贴图片或文件"/u);
  assert.match(composer, /onPaste=/u);
  assert.match(modelSelector, /aria-label="选择模型"/u);
  assert.match(modelSelector, /图片模型 <small>按意图自动调用<\/small>/u);
  assert.doesNotMatch(composer, /aria-label="任务类型"|ex-mode-switch|onModeChange|TaskMode/u);
  assert.doesNotMatch(composer, />\s*办公\s*</u);
  assert.match(composer, /modelId !== chatModel\) setDisposition\("queue"\)/u);
  assert.match(composer, /modelId !== imageModel\) setDisposition\("queue"\)/u);
  assert.match(runtimeSession, /reconcileModelSelection\(current, bootstrap\.models\.chat\)/u);
  assert.match(runtimeSession, /reconcileModelSelection\(current, bootstrap\.models\.image\)/u);
  assert.match(composer, /!draft\.trim\(\) && attachments\.length === 0/u);
  assert.match(runtimeSession, /rawInput\.trim\(\) \|\| \(attachments\.length > 0/u);
});

test("one update identity stays dismissed across its download states", () => {
  assert.match(app, /window\.localStorage\.getItem\(DISMISSED_UPDATE_BANNERS_KEY\)/u);
  assert.match(app, /update\.release_id && update\.build_digest/u);
  assert.doesNotMatch(app, /`\$\{update\.target_version[^`]*\}:\$\{update\.state\}`/u);
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

test("scheduled task actions return to the preserved Composer and only fill an empty draft", () => {
  assert.match(app, /onTemplate=\{\(text\) => \{[\s\S]*?setComposerPrefill\([\s\S]*?setSchedulesOpen\(false\);/u);
  assert.match(homeDashboard, /Runtime Scheduler[\s\S]*?操作将在当前会话中执行，由 Runtime 返回真实结果/u);
  assert.doesNotMatch(homeDashboard, /Creative Center|创意中心/u);
  assert.match(app, /draft=\{composerDraft\}[\s\S]*?onDraftChange=\{setComposerDraft\}/u);
  assert.match(composer, /if \(!draft\.trim\(\)\) onDraftChange\(prefillRequest\.text\);/u);
  assert.match(composer, /onPrefillConsumed\?\.\(\);/u);
  assert.match(app, /onPrefillConsumed=\{\(\) => setComposerPrefill\(null\)\}/u);
});

test("settings and external connections stay on real product contracts", () => {
  assert.match(settings, /data-testid="settings-workspace"[\s\S]*?settings-profile[\s\S]*?settings-general[\s\S]*?settings-knowledge[\s\S]*?settings-memory/u);
  assert.match(settings, /仅保存在此设备，不会上传或改变企业账号资料/u);
  assert.match(settings, /changeSessionPassword\(currentPassword, newPassword, requestId\)/u);
  assert.match(settings, /onPermissionChange\(profile\)/u);
  assert.match(composer, /data-testid="composer-connections"[\s\S]*?onClick=\{onOpenConnections\}/u);
  assert.match(app, /setOpenChannelsKey\(\(value\) => value \+ 1\)[\s\S]*?setSkillsOpen\(true\)/u);
  assert.match(extensionManager, /data-testid="capability-channels"/u);
  assert.match(connectorPopover, /微信中的发送者名称来自所登录账号，请先将账号名称设为 e-Mate。/u);
  assert.match(connectorPopover, /外部软件显示名由对应平台的应用或机器人资料决定；连接前请将名称设为 e-Mate。/u);
  assert.doesNotMatch(connectorPopover, /CowAgent/u);
  assert.doesNotMatch(connectorPopover, /修改微信账号名/u);
  assert.doesNotMatch(app, /<IconButton label="通知">/u);
});

test("Feishu document OAuth and message Bot remain separate user flows", () => {
  assert.match(connectorPopover, /<strong>文档与云空间授权<\/strong>/u);
  assert.match(connectorPopover, /<strong>消息 Bot<\/strong>/u);
  assert.match(connectorPopover, /isFeishu && !connectorUnavailable/u);
  assert.match(connectorPopover, /selfServiceChannel\.adapter_available \|\| selfServiceChannel\.instance/u);
  assert.match(connectorPopover, /App ID 与 App Secret/u);
});

test("e-Mate theme values and brand actions are separate semantic tokens", () => {
  assert.match(tokens, /--color-canvas:\s*oklch\(0\.966318 0\.003973 106\.474\);/u);
  assert.match(tokens, /--color-workspace-surface:\s*oklch\(0\.984548 0\.002637 106\.448\);/u);
  assert.match(tokens, /--color-surface:\s*oklch\(0\.966318 0\.003973 106\.474\);/u);
  assert.match(tokens, /--color-composer-surface:\s*var\(--color-workspace-surface\);/u);
  assert.match(tokens, /--color-session-emphasis:\s*oklch\(0\.94007 0 0\);/u);
  assert.match(tokens, /--scrollbar-thumb:\s*oklch\(0\.921906 0 0\);/u);
  assert.match(tokens, /--color-ink:\s*oklch\(0\.296426 0\.00355 106\.614\);/u);
  assert.match(tokens, /--color-accent:\s*oklch\(0\.682034 0\.173444 251\.11\);/u);
  assert.match(tokens, /--color-canvas:\s*oklch\(0\.296426 0\.00355 106\.614\);/u);
  assert.match(tokens, /--color-workspace-surface:\s*oklch\(0\.177638 0 0\);/u);
  assert.match(tokens, /--color-surface:\s*oklch\(0\.243535 0 0\);/u);
  assert.match(tokens, /--color-session-emphasis:\s*oklch\(0\.268618 0 0\);/u);
  assert.match(tokens, /--color-composer-surface:\s*var\(--color-session-emphasis\);/u);
  assert.match(tokens, /--scrollbar-thumb:\s*var\(--color-session-emphasis\);/u);
  assert.match(tokens, /--color-ink:\s*oklch\(0\.981562 0\.002639 106\.448\);/u);
  assert.match(tokens, /--color-accent:\s*oklch\(0\.528649 0\.173447 254\.975\);/u);
  assert.match(tokens, /--color-brand:\s*oklch\(0\.649203 0\.180912 42\.881\);/u);
});

test("all user-visible workspace brand copy and lockups use e-Mate", () => {
  for (const candidate of [app, composer, timeline, sidebar, login, settings]) {
    assert.doesNotMatch(candidate, />[^<]*EcoreX[^<]*</u);
    assert.doesNotMatch(candidate, /["'`]([^"'`]*\s)?EcoreX(?:\s|[？。…]|$)/u);
  }
  assert.match(layout, /url\("\.\.\/assets\/emate-logo\.png"\)/u);
  assert.match(layout, /url\("\.\.\/assets\/emate-mark\.png"\)/u);
  assert.match(sidebar, /e-Mate v\{version\}/u);
  assert.match(login, /className="ex-login-logo"/u);
  assert.match(layout, /:root\[data-theme="dark"\] \.ex-emate-logo/u);
});

test("Workbench surface roles map the references without component-level colors", () => {
  assert.match(layout, /\.ex-sidebar\s*\{[\s\S]*?background:\s*var\(--color-canvas\);/u);
  assert.match(layout, /\.ex-workspace\s*\{[\s\S]*?background:\s*var\(--color-workspace-surface\);/u);
  assert.match(layout, /\.ex-timeline\s*\{[\s\S]*?background:\s*var\(--color-workspace-surface\);/u);
  assert.match(features, /\.ex-composer-region\s*\{[\s\S]*?background:\s*var\(--color-workspace-surface\);/u);
  assert.match(features, /\.ex-composer\s*\{[\s\S]*?background:\s*var\(--color-composer-surface\);/u);
  assert.match(layout, /\*::-webkit-scrollbar-thumb\s*\{[\s\S]*?background:\s*var\(--scrollbar-thumb\);/u);
});
