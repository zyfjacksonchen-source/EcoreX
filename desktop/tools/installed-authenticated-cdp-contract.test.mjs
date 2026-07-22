import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./run-installed-authenticated-runtime-cdp.mjs", import.meta.url), "utf8");

test("installed authenticated CDP defaults to the complete real acceptance matrix", () => {
  assert.match(source, /const ALL_GROUPS = Object\.freeze\(\["attachments", "tools", "image", "models", "ui"\]\)/u);
  assert.match(source, /if \(!raw\) return new Set\(ALL_GROUPS\)/u);
  for (const tool of ["ocr", "vision", "read", "shell", "fetch", "cdp", "imagegen"]) {
    assert.match(source, new RegExp(`(?:runTool\\(page, models, "${tool}"|toolFacts\\(result\\.events, "${tool}")`, "u"));
  }
  assert.match(source, /Array\.from\(\{ length: 4 \}/u);
  assert.match(source, /normal_task_blocked_by_image_pool/u);
  assert.match(source, /artifact\.retouch\.requested/u);
  assert.match(source, /artifact\.retouch\.completed/u);
  assert.match(source, /model\.response_completed/u);
  assert.match(source, /progressiveDiscoveryScenario/u);
  assert.match(source, /toolFacts\(result\.events, "tool_search"\)/u);
  assert.match(source, /searchSequence < ocrSequence/u);
  assert.doesNotMatch(source, /snapshot\.models\.chat\.slice/u);
  assert.match(source, /model_ui_switch_failed/u);
  assert.match(source, /share_identity_not_unique/u);
  assert.match(source, /article\.message\.user/u);
  assert.match(source, /article\.message\.assistant/u);
  assert.match(source, /share_image_preview_invalid/u);
  assert.match(source, /theme_persistence_failed/u);
});

test("credentials are environment-only and Runtime secrets never enter the report", () => {
  assert.match(source, /requiredEnvironment\("ECOREX_ACCEPTANCE_IDENTIFIER"\)/u);
  assert.match(source, /requiredEnvironment\("ECOREX_ACCEPTANCE_PASSWORD"\)/u);
  assert.match(source, /delete process\.env\[name\]/u);
  assert.doesNotMatch(source, /--(?:identifier|password|username|credential|secret)/u);
  assert.doesNotMatch(source, /report\.(?:password|identifier|csrf|bearer)/u);
  assert.match(source, /bootstrap\.csrf_token/u);
  assert.match(source, /window\.__ECOREX_RUNTIME__\.bearerToken|bridge\.bearerToken/u);
  assert.match(source, /password = ""/u);
  assert.match(source, /thread_sha256/u);
  assert.match(source, /public_url_sha256/u);
});

test("success is based on durable facts rather than assistant claims", () => {
  assert.match(source, /event\.event_type === "tool\.call_requested"/u);
  assert.match(source, /event\.event_type === "tool\.result"/u);
  assert.match(source, /event\.payload\?\.activity\?\.status === "completed"/u);
  assert.match(source, /event\.event_type === "turn\.status_changed"/u);
  assert.match(source, /terminal\.payload\.to === "completed"/u);
  assert.match(source, /\/api\/v1\/input-attachments/u);
  assert.match(source, /authenticated_upload_thumbnail_missing/u);
  assert.match(source, /local_upload_thumbnail_missing/u);
  assert.match(source, /\/api\/v1\/artifacts\?thread_id=/u);
  assert.match(source, /\/retouch-workspaces\/\$\{encodeURIComponent\(workspace\.workspace_id\)\}\/submit/u);
});

test("the report uses an explicit bounded projection", () => {
  assert.match(source, /evidence_type: "ecorex-installed-authenticated-runtime-cdp"/u);
  assert.match(source, /version_sha256: digest/u);
  assert.match(source, /screenshot_sha256/u);
  assert.doesNotMatch(source, /report\.(?:url|path|prompt|response_text|raw)/u);
  assert.match(source, /process\.stderr\.write\(`\$\{JSON\.stringify\(\{ ok: false, error: code \}\)\}/u);
});
