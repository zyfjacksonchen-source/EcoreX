import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(path, import.meta.url), "utf8");

test("Astro Skill Hub stays pinned, same-origin, and e-Mate branded", async () => {
  const [layout, page, card, client, lock, app, productIndex] = await Promise.all([
    source("../skill-hub/src/layouts/Base.astro"),
    source("../skill-hub/src/pages/index.astro"),
    source("../skill-hub/src/components/SkillCard.astro"),
    source("../skill-hub/public/assets/skill-hub-client.unhashed-upstream0c214c3.js"),
    source("../../docs/v0.3.0/skill-hub/cow-skill-hub.lock.json"),
    source("../../ecorex/server/app.py"),
    source("../index.html"),
  ]);
  assert.equal(JSON.parse(lock).upstream.commit, "0c214c3a61f66f8c122111c23270bd146241001b");
  assert.match(card, /Adapted from Cow Skill Hub SkillCard\.astro/u);
  assert.match(layout, /<!--__ECOREX_RUNTIME_CONFIG__-->/u);
  assert.doesNotMatch(`${layout}${page}`, /CowAgent|Cow Skill Hub/u);
  assert.match(client, /\/api\/v1\/skill-hub\/skills/u);
  assert.match(client, /\/api\/v1\/bootstrap/u);
  assert.match(client, /X-EcoreX-CSRF/u);
  assert.doesNotMatch(client, /github\/callback|google\/callback|mysql|cloudflare|r2/iu);
  assert.match(app, /folded_path\.rstrip\("\/"\) == "ecorex-agent\/skills"/u);
  assert.match(productIndex, /skill-hub-page\.unhashed-upstream0c214c3\.json/u);
});
