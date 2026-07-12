import assert from "node:assert/strict";
import test from "node:test";

import {
  devicePollSeconds,
  deviceStatusRefreshDelay,
  safeDeviceVerificationUrl,
} from "./deviceLogin.ts";

test("device verification links are HTTPS-only and never carry credentials", () => {
  assert.equal(safeDeviceVerificationUrl("https://login.ecorex.example/device"), "https://login.ecorex.example/device");
  assert.equal(safeDeviceVerificationUrl("http://login.ecorex.example/device"), null);
  assert.equal(safeDeviceVerificationUrl("https://user:secret@login.ecorex.example/device"), null);
  assert.equal(safeDeviceVerificationUrl("javascript:alert(1)"), null);
  assert.equal(safeDeviceVerificationUrl("not a url"), null);
});

test("device poll countdown and status refresh respect server timing bounds", () => {
  const now = Date.parse("2026-07-10T00:00:00Z");
  assert.equal(devicePollSeconds("2026-07-10T00:00:05Z", now), 5);
  assert.equal(devicePollSeconds("2026-07-09T23:59:59Z", now), 0);
  assert.equal(deviceStatusRefreshDelay("2026-07-10T00:00:05Z", 5, now), 5_000);
  assert.equal(deviceStatusRefreshDelay("2026-07-10T00:01:00Z", 5, now), 30_000);
  assert.equal(deviceStatusRefreshDelay("invalid", 7, now), 7_000);
});
