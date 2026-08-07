import assert from "node:assert/strict";
import test from "node:test";

import { handOffToUpdatedRuntime } from "./updateActivationHandoff.ts";

test("update handoff waits for target health then replaces the old document", async () => {
  const versions = ["0.3.2", "1.0.0"];
  let replaced = "";
  const ready = await handOffToUpdatedRuntime({
    readBootstrap: async () => ({ update: { current_version: versions.shift() ?? "1.0.0" } }),
    targetVersion: "1.0.0",
    initialDelayMs: 0,
    pollIntervalMs: 0,
    timeoutMs: 1_000,
    currentUrl: "http://127.0.0.1:8765/chat?thread=1",
    replace: (url) => { replaced = url; },
  });
  assert.equal(ready, true);
  assert.equal(versions.length, 0);
  assert.match(replaced, /emate_updated=1.0.0/u);
});

test("update handoff opens the reserved new window when available", async () => {
  let opened = "";
  let replaced = "";
  await handOffToUpdatedRuntime({
    readBootstrap: async () => ({ update: { current_version: "1.0.0" } }),
    targetVersion: "1.0.0",
    initialDelayMs: 0,
    pollIntervalMs: 0,
    timeoutMs: 1_000,
    currentUrl: "http://127.0.0.1:8765/chat",
    replace: (url) => { replaced = url; },
    openUpdatedRuntime: (url) => {
      opened = url;
      return true;
    },
  });
  assert.match(opened, /emate_updated=1.0.0/u);
  assert.equal(replaced, "");
});
