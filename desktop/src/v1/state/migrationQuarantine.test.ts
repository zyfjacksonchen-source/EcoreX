import assert from "node:assert/strict";
import test from "node:test";

import { validateMigrationQuarantineProjection } from "../components/migrationQuarantine.ts";


test("legacy credential projection accepts aggregate categories only", () => {
  const projection = validateMigrationQuarantineProjection({
    status: "available",
    entry_count: 2,
    can_delete: true,
    deleted_at: null,
    items: [{ kind: "api_key", origin: "product_configuration", count: 2 }],
  });
  assert.equal(projection.entry_count, 2);
  assert.deepEqual(Object.keys(projection.items[0]).sort(), ["count", "kind", "origin"]);
});


test("legacy credential projection rejects paths, count drift, and guessed categories", () => {
  for (const value of [
    {
      status: "available",
      entry_count: 1,
      can_delete: true,
      deleted_at: null,
      items: [{ kind: "api_key", origin: "product_configuration", count: 1, key_path: "secret" }],
    },
    {
      status: "available",
      entry_count: 2,
      can_delete: true,
      deleted_at: null,
      items: [{ kind: "api_key", origin: "product_configuration", count: 1 }],
    },
    {
      status: "available",
      entry_count: 1,
      can_delete: true,
      deleted_at: null,
      items: [{ kind: "provider_token", origin: "provider_x", count: 1 }],
    },
  ]) {
    assert.throws(() => validateMigrationQuarantineProjection(value), TypeError);
  }
});
