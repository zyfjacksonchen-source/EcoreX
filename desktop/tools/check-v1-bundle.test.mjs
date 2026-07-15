import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { BundleBudgetError, inspectV1Bundle } from "./check-v1-bundle.mjs";

const FEATURES = [
  "ArtifactPreviewDialog",
  "ComposerModelSelector",
  "DeviceLoginCard",
  "ExtensionManagerDialog",
  "InteractionStack",
  "NewConversationProjectSelector",
  "OfficeMarkdown",
  "ReplayDialog",
  "RetouchWorkspace",
  "SettingsDialog",
  "ShareDialog",
  "TimelineActivity",
];

async function fixture({
  preloadFeature = false,
  entryBytes = null,
  invalidFeatureSyntax = false,
} = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "ecorex-bundle-test-"));
  const assets = path.join(root, "assets");
  await mkdir(assets);
  const featureFiles = FEATURES.map((stem) => `${stem}.1234567890abcdef.js`);
  const imports = featureFiles.map((name) => `import("./${name}")`).join(";\n");
  const entry = entryBytes === null
    ? Buffer.from(imports)
    : Buffer.concat([Buffer.from(imports), Buffer.alloc(entryBytes, 32)]);
  await writeFile(path.join(assets, "index.1234567890abcdef.js"), entry);
  await writeFile(path.join(assets, "vendor-runtime.1234567890abcdef.js"), "export const ready=true");
  for (const [index, name] of featureFiles.entries()) {
    await writeFile(
      path.join(assets, name),
      invalidFeatureSyntax && index === 0 ? "const = ;" : "export default true",
    );
  }
  const optionalPreload = preloadFeature
    ? `<link rel="modulepreload" href="./assets/${featureFiles[0]}">`
    : "";
  await writeFile(
    path.join(root, "index.html"),
    `<html><head><script type="module" src="./assets/index.1234567890abcdef.js"></script>`
      + `<link rel="modulepreload" href="./assets/vendor-runtime.1234567890abcdef.js">`
      + `${optionalPreload}</head></html>`,
  );
  return root;
}

test("accepts a bounded entry with every declared deferred UI chunk", async (t) => {
  const root = await fixture();
  t.after(() => rm(root, { recursive: true, force: true }));
  const result = await inspectV1Bundle(root);
  assert.equal(result.featureChunks.length, FEATURES.length);
  assert.equal(result.initial.length, 2);
});

test("rejects a feature chunk that leaks into initial modulepreload", async (t) => {
  const root = await fixture({ preloadFeature: true });
  t.after(() => rm(root, { recursive: true, force: true }));
  await assert.rejects(
    inspectV1Bundle(root),
    (error) => error instanceof BundleBudgetError && /must not be fetched/u.test(error.message),
  );
});

test("rejects workspace entry growth beyond its explicit budget", async (t) => {
  const root = await fixture({ entryBytes: 129 * 1024 });
  t.after(() => rm(root, { recursive: true, force: true }));
  await assert.rejects(
    inspectV1Bundle(root),
    (error) => error instanceof BundleBudgetError && /workspace entry/u.test(error.message),
  );
});

test("rejects a syntactically corrupt content-addressed chunk", async (t) => {
  const root = await fixture({ invalidFeatureSyntax: true });
  t.after(() => rm(root, { recursive: true, force: true }));
  await assert.rejects(
    inspectV1Bundle(root),
    (error) => error instanceof BundleBudgetError && /not valid JavaScript syntax/u.test(error.message),
  );
});
