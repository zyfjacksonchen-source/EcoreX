import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/v1/AppV1.tsx", import.meta.url), "utf8");
const client = await readFile(
  new URL("../src/v1/api/runtimeClient.ts", import.meta.url),
  "utf8",
);

test("candidate Runtime window is explicit and cannot be mistaken for production", () => {
  assert.match(client, /mode\?: "standard" \| "acceptance-preview"/u);
  assert.match(app, /mode === "acceptance-preview"/u);
  assert.match(app, /data-runtime-mode="acceptance-preview"/u);
  assert.match(app, /写入隔离副本/u);
});
