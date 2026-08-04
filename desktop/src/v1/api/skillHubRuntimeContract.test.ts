import assert from "node:assert/strict";
import test from "node:test";

import { validateSkillHubDetailProjection } from "./skillHubRuntimeContract.ts";

const card = {
  slug: "office-helper",
  title: "Office Helper",
  summary: "Helps with office work.",
  version: "1.0.0",
  package_sha256: "a".repeat(64),
  package_size_bytes: 42,
  tags: ["office"],
  category: "office_productivity",
  uploader: { nickname: "e-Mate 用户", author_ref: "author_0123456789abcdef01234567" },
  provenance: { brand: "e-Mate", original_platform: null, original_url: null },
  installation_status: "not_installed",
  readiness: "ready",
} as const;

const reject = (message: string): never => { throw new Error(message); };

test("deferred Skill Hub boundary validates version identity", () => {
  assert.equal(validateSkillHubDetailProjection({ schema_version: 1, skill: card, versions: [card] }, reject).skill.slug, card.slug);
  assert.throws(
    () => validateSkillHubDetailProjection({ schema_version: 1, skill: card, versions: [{ ...card, slug: "other-skill" }] }, reject),
    /invalid Skill Hub detail/,
  );
});
