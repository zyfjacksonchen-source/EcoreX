import assert from "node:assert/strict";
import test from "node:test";

import type {
  ExtensionCatalogSnapshot,
  ExtensionProjection,
} from "../api/contracts.ts";
import {
  extensionAction,
  extensionActionDisabledReason,
  extensionActionLabel,
  extensionCatalogSummary,
  extensionPermissionEffectLabel,
  extensionRequestKey,
  filterExtensions,
} from "./extensions.ts";

const extension: ExtensionProjection = {
  extension_id: "office-tools",
  display_name: "办公工具",
  description: "Runtime 托管的办公工具集合。",
  kind: "tool_provider",
  active_revision_id: "rev_2",
  active_version: "1.2.0",
  active_digest: "a".repeat(64),
  source: "signed_release",
  trust: "verified_publisher",
  status: "enabled",
  health: "degraded",
  dependencies: [],
  exports: [],
  actions: [
    {
      action_id: "health_check",
      enabled: true,
      disabled_reason: null,
      requires_confirmation: false,
    },
    {
      action_id: "rollback",
      enabled: false,
      disabled_reason: "没有可回滚的已知良好版本。",
      requires_confirmation: true,
    },
  ],
  last_error_code: "provider_timeout",
  revision: 7,
  updated_at: "2026-07-10T10:00:00Z",
};

test("extension summary counts only explicit backend status and health values", () => {
  const snapshot: ExtensionCatalogSnapshot = {
    snapshot_id: "ext_snapshot_1",
    contract_version: "1.0",
    items: [
      extension,
      { ...extension, extension_id: "quarantined", status: "quarantined", health: "unhealthy" },
      { ...extension, extension_id: "circuit", status: "disabled", health: "circuit_open" },
    ],
  };

  assert.deepEqual(extensionCatalogSummary(snapshot), {
    total: 3,
    enabled: 1,
    quarantined: 1,
    degraded: 1,
    unhealthy: 1,
    circuitOpen: 1,
  });
  assert.deepEqual(extensionCatalogSummary(null), {
    total: 0,
    enabled: 0,
    quarantined: 0,
    degraded: 0,
    unhealthy: 0,
    circuitOpen: 0,
  });
});

test("extension actions and retry identity remain bound to backend revision", () => {
  const action = extensionAction(extension, "rollback");
  assert.equal(action?.requires_confirmation, true);
  assert.equal(extensionActionDisabledReason(action!), "没有可回滚的已知良好版本。");
  assert.equal(extensionActionLabel("health_check"), "检查健康");
  assert.equal(extensionRequestKey(extension, "health_check"), "office-tools:health_check:7");
  assert.equal(
    extensionRequestKey({ ...extension, revision: 8 }, "health_check"),
    "office-tools:health_check:8",
  );
});

test("disabled actions never gain a frontend-inferred reason", () => {
  assert.equal(extensionActionDisabledReason({
    action_id: "enable",
    enabled: false,
    disabled_reason: null,
    requires_confirmation: false,
  }), "当前版本没有提供这项操作不可用的原因。");
  assert.equal(extensionActionDisabledReason({
    action_id: "disable",
    enabled: true,
    disabled_reason: "ignored",
    requires_confirmation: false,
  }), null);
});

test("search and filters only select exact backend projection fields", () => {
  const items = [
    extension,
    { ...extension, extension_id: "image-skill", display_name: "图片技能", kind: "skill" as const, status: "disabled" as const },
  ];
  assert.deepEqual(filterExtensions(items, "office", "all", "all").map((item) => item.extension_id), ["office-tools"]);
  assert.deepEqual(filterExtensions(items, "图片", "skill", "disabled").map((item) => item.extension_id), ["image-skill"]);
  assert.deepEqual(filterExtensions(items, "", "tool_provider", "disabled"), []);
});

test("permission effects are rendered as Chinese product labels without exposing unknown literals", () => {
  assert.equal(extensionPermissionEffectLabel("read"), "读取数据");
  assert.equal(extensionPermissionEffectLabel("filesystem-write"), "修改工作区文件");
  assert.equal(extensionPermissionEffectLabel("vendor.super_secret_scope"), "其他受控权限");
  assert.equal(extensionPermissionEffectLabel("读取日历"), "读取日历");
});
