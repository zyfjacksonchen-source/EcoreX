import assert from "node:assert/strict";
import test from "node:test";

import { hasPendingRuntimeUpdate } from "./updatePresentation.ts";

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
    hasPendingRuntimeUpdate({ ...current, state: "awaiting_user", target_version: "1.0.7" }),
    true,
  );
});
