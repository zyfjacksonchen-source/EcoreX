import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { ProductionAssetError, rehashProductionDist } from "./rehash-dist.mjs";

const MARKER = "<!--__ECOREX_RUNTIME_CONFIG__-->";

async function fixture(files, index) {
  const root = await mkdtemp(path.join(os.tmpdir(), "ecorex-rehash-test-"));
  const dist = path.join(root, "dist");
  await mkdir(path.join(dist, "assets"), { recursive: true });
  for (const [relative, content] of Object.entries(files)) {
    const target = path.join(dist, ...relative.split("/"));
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content);
  }
  await writeFile(path.join(dist, "index.html"), index, "utf-8");
  return { root, dist };
}

async function treeDigest(root) {
  const values = [];
  const visit = async (directory) => {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(absolute);
      else {
        const content = await readFile(absolute);
        values.push([
          path.relative(root, absolute).split(path.sep).join("/"),
          createHash("sha256").update(content).digest("hex")
        ]);
      }
    }
  };
  await visit(root);
  return values;
}

test("content-addresses a dependency DAG, rewrites references, and is idempotent", async (t) => {
  const { root, dist } = await fixture(
    {
      "assets/app.unhashed-OLDhash1.js": 'import("./lazy.unhashed-OLDhash2.js");\n',
      "assets/lazy.unhashed-OLDhash2.js": 'export const ready = true;\n',
      "assets/theme.unhashed-OLDhash3.css": 'body{background:url("./pixel.unhashed-OLDhash4.png")}\n',
      "assets/pixel.unhashed-OLDhash4.png": Buffer.from([1, 2, 3, 4])
    },
    `<!doctype html><html><head>${MARKER}<link rel="stylesheet" href="./assets/theme.unhashed-OLDhash3.css"><script type="module" src="./assets/app.unhashed-OLDhash1.js"></script></head><body></body></html>`
  );
  t.after(() => rm(root, { recursive: true, force: true }));

  const first = await rehashProductionDist(dist);
  assert.equal(first.assetCount, 4);
  const firstTree = await treeDigest(dist);
  for (const [relative, digest] of firstTree.filter(([relative]) => relative !== "index.html")) {
    assert.match(relative, new RegExp(`\\.${digest.slice(0, 16)}\\.[^.]+$`, "i"));
  }
  const index = await readFile(path.join(dist, "index.html"), "utf-8");
  assert.doesNotMatch(index, /OLDhash/u);
  const appPath = first.assets.find((value) => value.endsWith(".js") && value.includes("/app."));
  const app = await readFile(path.join(dist, ...appPath.split("/")), "utf-8");
  assert.match(app, /lazy\.[0-9a-f]{16}\.js/u);
  const cssPath = first.assets.find((value) => value.endsWith(".css"));
  const css = await readFile(path.join(dist, ...cssPath.split("/")), "utf-8");
  assert.match(css, /pixel\.[0-9a-f]{16}\.png/u);

  await rehashProductionDist(dist);
  assert.deepEqual(await treeDigest(dist), firstTree);
});

test("finds a dynamic asset inside minified template interpolation", async (t) => {
  const { root, dist } = await fixture(
    {
      "assets/app.unhashed-AAAAAAAA.js": "const load=`${()=>import(\"./clientOperationOutbox.unhashed-BBBBBBBB.js\")}`;export{load};\n",
      "assets/clientOperationOutbox.unhashed-BBBBBBBB.js": "export const ready=true;\n"
    },
    `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`
  );
  t.after(() => rm(root, { recursive: true, force: true }));

  const result = await rehashProductionDist(dist);
  const appPath = result.assets.find((value) => value.includes("/app."));
  const app = await readFile(path.join(dist, ...appPath.split("/")), "utf-8");
  assert.match(app, /clientOperationOutbox\.[0-9a-f]{16}\.js/u);
});

test("does not duplicate quotes around adjacent minified dynamic imports", async (t) => {
  const { root, dist } = await fixture(
    {
      "assets/app.unhashed-AAAAAAAA.js": [
        'const before="ordinary";',
        'const first=()=>import("./first.unhashed-BBBBBBBB.js");',
        'const second=()=>import("./second.unhashed-CCCCCCCC.js");',
        'export{first,second};\n'
      ].join(""),
      "assets/first.unhashed-BBBBBBBB.js": "export const first=true;\n",
      "assets/second.unhashed-CCCCCCCC.js": "export const second=true;\n"
    },
    `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`
  );
  t.after(() => rm(root, { recursive: true, force: true }));

  const result = await rehashProductionDist(dist);
  const appPath = result.assets.find((value) => value.includes("/app."));
  const app = await readFile(path.join(dist, ...appPath.split("/")), "utf-8");
  assert.doesNotMatch(app, /import\(["']{2}/u);
  assert.doesNotMatch(app, /\.js["']{2}\)/u);
  assert.match(app, /import\("\.\/first\.[0-9a-f]{16}\.js"\)/u);
  assert.match(app, /import\("\.\/second\.[0-9a-f]{16}\.js"\)/u);
});

test("rejects cyclic asset references without changing the original dist", async (t) => {
  const { root, dist } = await fixture(
    {
      "assets/app.unhashed-AAAAAAAA.js": 'import("./lazy.unhashed-BBBBBBBB.js");\n',
      "assets/lazy.unhashed-BBBBBBBB.js": 'import("./app.unhashed-AAAAAAAA.js");\n'
    },
    `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`
  );
  t.after(() => rm(root, { recursive: true, force: true }));
  const before = await treeDigest(dist);

  await assert.rejects(
    rehashProductionDist(dist),
    (error) => error instanceof ProductionAssetError && /cyclic/u.test(error.message)
  );
  assert.deepEqual(await treeDigest(dist), before);
});

test("fails closed for missing dependencies, inline code, legacy content, and orphan assets", async (t) => {
  const cases = [
    {
      files: { "assets/app.unhashed-AAAAAAAA.js": 'import("./missing.unhashed-BBBBBBBB.js");\n' },
      index: `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`,
      expected: /missing/u
    },
    {
      files: { "assets/app.unhashed-AAAAAAAA.js": "export {};\n" },
      index: `<html><head>${MARKER}<script>window.bad=true</script><script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`,
      expected: /inline/u
    },
    {
      files: { "assets/app.unhashed-AAAAAAAA.js": "export {};\n" },
      index: `<html><head>${MARKER}<script src="https://example.invalid/app.js"></script></head></html>`,
      expected: /fixed production asset/u
    },
    {
      files: { "assets/plain.js": "export {};\n" },
      index: `<html><head>${MARKER}<script src="./assets/plain.js"></script></head></html>`,
      expected: /staging marker/u
    },
    {
      files: { "assets/app.unhashed-AAAAAAAA.js": 'window.old="webui-overlay";\n' },
      index: `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`,
      expected: /legacy/u
    },
    {
      files: {
        "assets/app.unhashed-AAAAAAAA.js": "export {};\n",
        "assets/stale.unhashed-BBBBBBBB.js": "export const stale=true;\n"
      },
      index: `<html><head>${MARKER}<script src="./assets/app.unhashed-AAAAAAAA.js"></script></head></html>`,
      expected: /orphaned/u
    }
  ];
  const roots = [];
  t.after(async () => {
    await Promise.all(roots.map((root) => rm(root, { recursive: true, force: true })));
  });
  for (const value of cases) {
    const created = await fixture(value.files, value.index);
    roots.push(created.root);
    await assert.rejects(rehashProductionDist(created.dist), value.expected);
  }
});
