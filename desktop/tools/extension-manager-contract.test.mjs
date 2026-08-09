import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(
  new URL("../src/v1/components/SkillsWorkspace.tsx", import.meta.url),
  "utf8",
);
const userMcp = readFileSync(
  new URL("../src/v1/components/UserMCPPanel.tsx", import.meta.url),
  "utf8",
);
const settings = readFileSync(
  new URL("../src/v1/components/SettingsDialog.tsx", import.meta.url),
  "utf8",
);
const state = readFileSync(
  new URL("../src/v1/state/extensions.ts", import.meta.url),
  "utf8",
);
const session = readFileSync(
  new URL("../src/v1/state/useExtensionSession.ts", import.meta.url),
  "utf8",
);
const css = readFileSync(
  new URL("../src/v1/styles/features.css", import.meta.url),
  "utf8",
);
const extensionCssStart = css.indexOf(".ex-workspace.is-skills");
const extensionCssEnd = css.indexOf(".ex-update-banner", extensionCssStart);
const extensionCss = css.slice(extensionCssStart, extensionCssEnd);

test("skill workspace renders only backend-projected actions and reasons", () => {
  assert.match(component, /extension\.actions\.find/);
  assert.match(component, /action\?\.enabled/);
  assert.match(state, /action\.disabled_reason/);
  assert.match(component, /requires_confirmation/);
  assert.match(component, /!catalogReady/);
  assert.match(component, /!action\?\.enabled/);
  assert.match(component, /registryBusy/);
  assert.match(component, /loadState === "ready"/);
  assert.match(component, /aria-busy/);
  assert.match(component, /role="alert"/);
  assert.match(component, /selected\.dependencies/);
  assert.match(component, /selected\.exports/);
  assert.match(component, /CATEGORY_ORDER/);
  assert.match(component, /protectedExtension/);
  assert.match(component, /extensionPermissionEffectLabel/);
  assert.match(component, /ready: "可使用"/u);
  assert.doesNotMatch(component, /ready: "可运行"/u);
  assert.match(settings, /管理扩展/);
});

test("skill workspace exposes configuration keys without execution internals or stored secrets", () => {
  for (const forbidden of [
    "active_digest",
    "active_revision_id",
    "command",
    "secret",
    "path",
    "canonical_manifest_base64",
    "/extensions/install",
  ]) {
    assert.equal(
      component.includes(forbidden),
      false,
      `component must not render or submit ${forbidden}`,
    );
  }
  assert.match(component, /type="password"/);
  assert.match(component, /autoComplete="off"/);
  assert.match(component, /if \(saved\) setConfiguration\(\{\}\)/);
  assert.doesNotMatch(component, /localStorage|sessionStorage/);
});

test("extension session fences stale catalogs and refreshes conflicts without replaying them", () => {
  assert.match(session, /operationLocks\.current\.size/);
  assert.match(session, /requestIds\.current\.get\(requestKey\)/);
  assert.match(session, /error\.status === 409/);
  assert.match(session, /requestIds\.current\.delete\(requestKey\)/);
  assert.match(session, /await client\.extensionCatalog\(\)/);
  assert.equal((session.match(/await client\.mutateExtension\(/g) ?? []).length, 1);
  assert.doesNotMatch(session, /setExtensionSnapshot\([^\n]*(?:enabled|disabled|quarantined)/);
});

test("WebUI Skill Hub exposes real detail, download, upload, and Runtime install paths", () => {
  assert.match(component, /查看详情/);
  assert.match(component, /版本历史/);
  assert.match(component, /下载 ZIP/);
  assert.match(component, /单次安装意图/);
  assert.match(component, /onPublishHub/);
  assert.match(component, /按标签筛选/);
  assert.match(component, /按原始来源筛选/);
  assert.match(component, /requestAction\(selected, "uninstall"\)/);
  assert.match(session, /client\.skillHubDetail/);
  assert.match(session, /client\.downloadHubSkillPackage/);
  assert.match(session, /client\.skillHubCatalog\(query, category, tag, source/);
  assert.doesNotMatch(component, /emate:\/\//);
});

test("unconfigured MCP is an explicit empty state rather than a fake capability", () => {
  assert.match(component, /<UserMCPPanel/u);
  assert.match(userMcp, /client\.userMcpServers/u);
  assert.match(userMcp, /loaded && !items\.length/u);
  assert.match(userMcp, /尚未添加远程 MCP/u);
  assert.match(userMcp, /测试通过后即可启用工具/u);
});

test("local Skill import derives identity and keeps Hub publishing advanced", () => {
  assert.match(component, /e-Mate 会自动识别名称/u);
  assert.doesNotMatch(component, /localExtensionId|<span>扩展 ID<\/span>/u);
  assert.match(component, /<details className="ex-skill-advanced">/u);
  assert.match(component, /<summary>高级操作<\/summary>/u);
  assert.match(session, /client\.installLocalSkill\(\s*bundleBase64,\s*clientRequestId/u);
});

test("extension feature CSS stays inside the locked EcoreX token system", () => {
  assert.notEqual(extensionCss.length, 0);
  assert.doesNotMatch(extensionCss, /#[0-9a-f]{3,8}\b/i);
  assert.doesNotMatch(extensionCss, /\b(?:rgb|hsl|oklch)\(/i);
  for (const declaration of extensionCss.match(/border-radius:[^;]+/gi) ?? []) {
    assert.match(declaration, /var\(--radius-/);
  }
  for (const declaration of extensionCss.match(/box-shadow:[^;]+/gi) ?? []) {
    assert.match(declaration, /var\(--/);
  }
  assert.doesNotMatch(extensionCss, /transition:\s*all\b/i);
  assert.doesNotMatch(extensionCss, /z-index:\s*\d+/i);
  assert.match(extensionCss, /@media \(max-width: 639px\)/);
});
