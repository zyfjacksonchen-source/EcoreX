import assert from "node:assert/strict";
import test from "node:test";

import {
  hasPendingRuntimeUpdate,
  isRuntimeUpdateInstalling,
  isVerifiedRuntimeUpdateReady,
  runtimeUpdateStatusText,
} from "./updatePresentation.ts";

const current = {
  current_version: "1.0.5",
  release_id: "release-stable-105",
  build_digest: "a".repeat(64),
  transaction_id: "transaction-105",
  can_activate: true,
  requires_refresh: false,
  error_code: null,
} as const;

test("same-version durable update state never becomes a visible update", () => {
  for (const state of ["available", "downloading", "awaiting_user", "activating", "failed"] as const) {
    assert.equal(hasPendingRuntimeUpdate({ ...current, state, target_version: "1.0.5" }), false);
  }
});

test("a newer target remains visible to the user", () => {
  assert.equal(
    hasPendingRuntimeUpdate({ ...current, state: "awaiting_user", target_version: "1.0.8" }),
    true,
  );
});

test("only a verified prepared update is eligible for the notification banner", () => {
  for (const state of ["available", "downloading", "activating", "failed"] as const) {
    assert.equal(
      isVerifiedRuntimeUpdateReady({ ...current, state, target_version: "1.0.8" }),
      false,
    );
  }
  assert.equal(
    isVerifiedRuntimeUpdateReady({ ...current, state: "awaiting_user", target_version: "1.0.8" }),
    true,
  );
  assert.equal(
    isVerifiedRuntimeUpdateReady({
      ...current,
      state: "awaiting_user",
      target_version: "1.0.8",
      can_activate: false,
    }),
    false,
  );
});

test("settings exposes truthful background and verified phases", () => {
  assert.match(
    runtimeUpdateStatusText({ ...current, state: "downloading", target_version: "1.0.8" }),
    /下载并校验/u,
  );
  assert.match(
    runtimeUpdateStatusText({ ...current, state: "awaiting_user", target_version: "1.0.8" }),
    /已下载并通过校验/u,
  );
});

test("failed discovery never masquerades as the latest available version", () => {
  for (const target_version of [null, "1.0.5", "1.0.8"]) {
    assert.match(
      runtimeUpdateStatusText({ ...current, state: "failed", target_version }),
      /更新检查失败/u,
    );
  }
});

test("install progress is visible only while a pending update is active", () => {
  assert.equal(
    isRuntimeUpdateInstalling({ ...current, state: "available", target_version: "1.0.8" }),
    false,
  );
  assert.equal(
    isRuntimeUpdateInstalling({ ...current, state: "available", target_version: "1.0.8" }, true),
    true,
  );
  assert.equal(
    isRuntimeUpdateInstalling({ ...current, state: "downloading", target_version: "1.0.8" }),
    true,
  );
  assert.equal(
    isRuntimeUpdateInstalling({ ...current, state: "failed", target_version: "1.0.8" }, true),
    false,
  );
});
