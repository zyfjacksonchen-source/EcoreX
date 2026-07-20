import assert from "node:assert/strict";
import test from "node:test";

import { resolveThemePreference } from "./themePreference.ts";

test("theme defaults to dark and honors only an explicit light preference", () => {
  assert.equal(resolveThemePreference(null), "dark");
  assert.equal(resolveThemePreference("dark"), "dark");
  assert.equal(resolveThemePreference("unexpected"), "dark");
  assert.equal(resolveThemePreference("light"), "light");
});
