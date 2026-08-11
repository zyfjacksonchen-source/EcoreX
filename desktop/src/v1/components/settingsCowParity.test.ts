import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(name: string): string {
  return readFileSync(new URL(name, import.meta.url), "utf8");
}

test("settings keep CowAgent local configuration semantics", () => {
  const app = source("../AppV1.tsx");
  const client = source("../api/runtimeClient.ts");
  const settings = source("./SettingsDialog.tsx");
  const workspace = source("./WorkspaceContentSettings.tsx");

  assert.doesNotMatch(settings, /permissionUpdating|onPermissionChange|完全访问|默认权限/u);
  assert.doesNotMatch(app, /permissionLabel|onOpenPermissionSettings/u);
  assert.doesNotMatch(client, /updatePermission\s*\(/u);

  assert.doesNotMatch(workspace, /resettable_count|一键重置|onResetMemory|onUndoMemoryReset/u);
  assert.match(workspace, /MEMORY\.md/u);
  assert.match(workspace, /每日记忆/u);
  assert.match(settings, /本地 Skill/u);
  assert.match(settings, /MCP/u);
  assert.match(settings, /消息通道/u);
});
