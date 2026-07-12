import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/v1/AppV1.tsx", import.meta.url), "utf-8");
const boundary = await readFile(
  new URL("../src/v1/components/LazyFeatureBoundary.tsx", import.meta.url),
  "utf-8",
);
const preview = await readFile(
  new URL("../src/v1/components/ArtifactPreviewDialog.tsx", import.meta.url),
  "utf-8",
);
const sidebar = await readFile(
  new URL("../src/v1/components/Sidebar.tsx", import.meta.url),
  "utf-8",
);
const settings = await readFile(
  new URL("../src/v1/components/SettingsDialog.tsx", import.meta.url),
  "utf-8",
);
const vite = await readFile(new URL("../vite.config.ts", import.meta.url), "utf-8");

const FEATURES = [
  "ArtifactPreviewDialog",
  "ExtensionManagerDialog",
  "ReplayDialog",
  "RetouchWorkspace",
  "SettingsDialog",
  "ShareDialog",
];
const INLINE_FEATURES = ["DeviceLoginCard", "InteractionStack"];

test("low-frequency features are dynamic imports behind persistent Suspense boundaries", () => {
  for (const feature of FEATURES) {
    assert.doesNotMatch(app, new RegExp(`import\\s*\\{[^}]*${feature}[^}]*\\}\\s*from`, "u"));
    assert.match(app, new RegExp(`import\\(\"\\./components/${feature}\\.tsx\"\\)`, "u"));
    assert.match(app, new RegExp(`default:\\s*\\(await load${feature}\\(\\)\\)\\.${feature}`, "u"));
  }
  assert.ok((app.match(/<LazyFeatureBoundary/gu) ?? []).length >= FEATURES.length);
  assert.match(boundary, /<Suspense/u);
  assert.match(boundary, /openedOnce/u);
  assert.match(boundary, /class FeatureErrorBoundary/u);
});

test("login and HITL code stay deferred but keep an inline accessible fallback", () => {
  for (const feature of INLINE_FEATURES) {
    assert.doesNotMatch(app, new RegExp(`import\\s*\\{[^}]*${feature}[^}]*\\}\\s*from`, "u"));
    assert.match(app, new RegExp(`load${feature} = \\(\\) => import\\("\\./components/${feature}\\.tsx"\\)`, "u"));
    assert.match(app, new RegExp(`default:\\s*\\(await load${feature}\\(\\)\\)\\.${feature}`, "u"));
  }
  assert.match(app, /<Suspense fallback=/u);
  assert.match(app, /aria-live="polite"/u);
  assert.match(app, /aria-busy="true"/u);
});

test("lazy loading and error surfaces use the shared modal accessibility primitive", () => {
  assert.match(boundary, /import \* as Dialog from "@radix-ui\/react-dialog"/u);
  assert.match(boundary, /<Dialog\.Root open modal onOpenChange=\{handleOpenChange\}>/u);
  assert.match(boundary, /<Dialog\.Portal>/u);
  assert.match(boundary, /<Dialog\.Overlay className="ex-dialog-overlay" \/>/u);
  assert.match(boundary, /<Dialog\.Content/u);
  assert.match(boundary, /role=\{failed \? "alertdialog" : "dialog"\}/u);
  assert.match(boundary, /<Dialog\.Title>/u);
  assert.match(boundary, /<Dialog\.Description>/u);
  assert.match(boundary, /onOpenAutoFocus=\{\(event\) => \{/u);
  assert.match(boundary, /button\.focus\(\{ preventScroll: true \}\)/u);
  assert.match(boundary, /onCloseAutoFocus=\{\(event\) => \{/u);
  assert.match(boundary, /<Dialog\.Close asChild>/u);
  assert.match(boundary, /if \(!open\) onClose\(\);/u);
  assert.doesNotMatch(boundary, /aria-modal=/u);
});

test("Settings lazy boundary preserves system health contract", () => {
  assert.match(app, /systemHealth=\{runtime\.systemHealth\}/u);
  assert.match(app, /systemHealthLoadState=\{runtime\.systemHealthLoadState\}/u);
  assert.match(app, /onLoadSystemTechnicalHealth=\{runtime\.loadSystemTechnicalHealth\}/u);
});

test("Settings exposes only aggregate legacy credential categories and confirmed deletion", () => {
  assert.match(app, /client=\{runtime\.client\}/u);
  assert.match(settings, /旧版凭证/u);
  assert.match(settings, /migrationQuarantine\.items\.map/u);
  assert.match(settings, /确认永久删除旧版凭证备份/u);
  assert.match(settings, /client\.deleteMigrationQuarantine\(clientRequestId\)/u);
  assert.match(settings, /pendingMigrationQuarantineDelete\.current/u);
  assert.doesNotMatch(settings, /key_path|source_relative_path|ciphertext|nonce/u);
});

test("lazy dialogs restore focus only after the browser accepts the candidate", () => {
  assert.match(app, /candidate\.focus\(\{ preventScroll: true \}\)/u);
  assert.match(app, /document\.activeElement === candidate/u);
  assert.match(app, /candidate\.closest\("\[inert\], \[hidden\], \[aria-hidden=/u);
  assert.match(app, /document\.querySelectorAll<HTMLElement>\(selector\)/u);
  assert.match(app, /data-ecorex-feature-trigger="navigation"/u);
});

test("Settings and Extensions have a visible task-menu fallback instead of hidden Sidebar focus", () => {
  assert.match(sidebar, /data-ecorex-feature-trigger="settings"/u);
  const settingsClose = app.slice(app.indexOf("const closeSettings"), app.indexOf("const closeExtensions"));
  const extensionsClose = app.slice(app.indexOf("const closeExtensions"), app.indexOf("const closeShare"));
  for (const closeContract of [settingsClose, extensionsClose]) {
    assert.match(closeContract, /data-ecorex-feature-trigger="task-menu"/u);
    assert.match(closeContract, /data-ecorex-feature-trigger="navigation"/u);
    assert.doesNotMatch(closeContract, /data-ecorex-feature-trigger="settings"/u);
  }
});

test("artifact preview owns Radix close autofocus and delegates deterministic restoration", () => {
  assert.match(preview, /onCloseAutoFocus=\{\(event\) => \{/u);
  assert.match(preview, /event\.preventDefault\(\);\s*onRestoreFocus\(\);/u);
  assert.match(app, /onRestoreFocus=\{restoreArtifactPreviewFocus\}/u);
  const previewRestore = app.slice(
    app.indexOf("const restoreArtifactPreviewFocus"),
    app.indexOf("const closeArtifactPreviewFallback"),
  );
  assert.match(previewRestore, /window\.requestAnimationFrame/u);
  assert.match(previewRestore, /restoreFeatureFocus/u);
});

test("chunk policy uses stable architecture layers rather than feature-name manual chunks", () => {
  assert.match(vite, /manualChunks:\s*productionChunk/u);
  assert.match(vite, /\/src\/v1\/api\//u);
  assert.match(vite, /\/src\/v1\/state\//u);
  for (const feature of FEATURES) assert.doesNotMatch(vite, new RegExp(feature, "u"));
});
