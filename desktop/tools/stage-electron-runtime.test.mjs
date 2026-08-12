import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("desktop Runtime is expanded at build time instead of installed on first launch", async () => {
  const staging = await readFile(path.join(desktop, "tools", "stage-electron-runtime.mjs"), "utf8");
  assert.match(staging, /stage-direct-runtime\.py/);
  assert.doesNotMatch(staging, /ecorex-bootstrap/);
  assert.doesNotMatch(staging, /current-release/);
  assert.doesNotMatch(staging, /releases/);
});
