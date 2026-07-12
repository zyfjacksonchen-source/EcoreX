import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(
  new URL("../src/v1/components/ExtensionManagerDialog.tsx", import.meta.url),
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
const extensionCssStart = css.indexOf(".ex-extension-dialog");
const extensionCssEnd = css.indexOf("@container workspace", extensionCssStart);
const extensionCss = css.slice(extensionCssStart, extensionCssEnd);

test("extension manager renders only backend-projected actions and reasons", () => {
  assert.match(component, /extension\.actions\.map/);
  assert.match(component, /action\.enabled/);
  assert.match(state, /action\.disabled_reason/);
  assert.match(component, /requires_confirmation/);
  assert.match(component, /!catalogReady/);
  assert.match(component, /!action\.enabled/);
  assert.match(component, /registryOperationBusy/);
  assert.match(component, /loadState === "ready"/);
  assert.match(component, /上次已验证目录已过期/);
  assert.match(component, /aria-busy/);
  assert.match(component, /role="alert"/);
  assert.match(component, /extension\.dependencies/);
  assert.match(component, /extension\.exports/);
  assert.match(component, /filterExtensions/);
  assert.match(component, /extensionPermissionEffectLabel/);
  assert.match(settings, /管理扩展/);
});

test("extension manager does not expose execution internals or local installation", () => {
  for (const forbidden of [
    "active_digest",
    "active_revision_id",
    "command",
    "environment",
    "env",
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
  assert.match(extensionCss, /@media \(min-width: 640px\)/);
});
