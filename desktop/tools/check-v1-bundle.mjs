import { gzipSync } from "node:zlib";
import { spawnSync } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const MAX_ENTRY_BYTES = 128 * 1024;
const MAX_INITIAL_JS_BYTES = 475 * 1024;
const MAX_INITIAL_GZIP_BYTES = 150 * 1024;
const MAX_CHUNK_BYTES = 500 * 1024;
const FEATURE_STEMS = [
  "ArtifactPreviewDialog",
  "DeviceLoginCard",
  "ExtensionManagerDialog",
  "InteractionStack",
  "OfficeMarkdown",
  "ReplayDialog",
  "RetouchWorkspace",
  "SettingsDialog",
  "ShareDialog",
  "TimelineActivity",
];

export class BundleBudgetError extends Error {
  constructor(message) {
    super(message);
    this.name = "BundleBudgetError";
  }
}

function fail(message) {
  throw new BundleBudgetError(message);
}

function attribute(tag, name) {
  const match = new RegExp(`\\s${name}\\s*=\\s*(["'])([^"']+)\\1`, "iu").exec(tag);
  return match?.[2] ?? null;
}

function assetPath(reference) {
  const value = reference.replace(/^\.\//u, "");
  if (!/^assets\/[A-Za-z0-9._-]+\.js$/u.test(value)) {
    fail(`production JS reference is not a fixed local asset: ${JSON.stringify(reference)}`);
  }
  return value;
}

function formatKiB(bytes) {
  return `${(bytes / 1024).toFixed(2)} KiB`;
}

function assertJavaScriptSyntax(absolute, relative) {
  const result = spawnSync(process.execPath, ["--check", absolute], {
    encoding: "utf-8",
    maxBuffer: 64 * 1024,
    timeout: 5_000,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    fail(`${relative} is not valid JavaScript syntax after content addressing`);
  }
}

export async function inspectV1Bundle(distDirectory) {
  const root = path.resolve(distDirectory);
  const index = await readFile(path.join(root, "index.html"), "utf-8");
  const scriptTags = [...index.matchAll(/<script\b[^>]*>/giu)]
    .filter((match) => attribute(match[0], "src"));
  if (scriptTags.length !== 1) fail("production index must contain exactly one entry script");
  const entry = assetPath(attribute(scriptTags[0][0], "src"));

  const preloads = new Set();
  for (const match of index.matchAll(/<link\b[^>]*>/giu)) {
    if (attribute(match[0], "rel") !== "modulepreload") continue;
    const href = attribute(match[0], "href");
    if (!href) fail("modulepreload is missing href");
    preloads.add(assetPath(href));
  }

  const assetDirectory = path.join(root, "assets");
  const names = (await readdir(assetDirectory)).filter((name) => name.endsWith(".js")).sort();
  const records = new Map();
  for (const name of names) {
    const relative = `assets/${name}`;
    const absolute = path.join(assetDirectory, name);
    const content = await readFile(absolute);
    if (content.byteLength > MAX_CHUNK_BYTES) {
      fail(`${relative} exceeds the 500 KiB per-chunk advisory budget`);
    }
    // Rehashing runs after Rollup, so a successful Vite build cannot prove
    // that the final bytes still parse. Parse every emitted chunk from disk;
    // this is the generic release gate that catches corrupt rewrites beyond
    // any one regression pattern.
    assertJavaScriptSyntax(absolute, relative);
    records.set(relative, {
      bytes: content.byteLength,
      gzipBytes: gzipSync(content, { level: 9 }).byteLength,
      content,
    });
  }

  const entryRecord = records.get(entry);
  if (!entryRecord) fail("entry script is missing from production assets");
  if (entryRecord.bytes > MAX_ENTRY_BYTES) {
    fail(`workspace entry ${formatKiB(entryRecord.bytes)} exceeds ${formatKiB(MAX_ENTRY_BYTES)}`);
  }

  const initial = [entry, ...preloads];
  let initialBytes = 0;
  let initialGzipBytes = 0;
  for (const relative of initial) {
    const record = records.get(relative);
    if (!record) fail(`initial JS asset is missing: ${relative}`);
    initialBytes += record.bytes;
    initialGzipBytes += record.gzipBytes;
  }
  if (initialBytes > MAX_INITIAL_JS_BYTES) {
    fail(`initial JS ${formatKiB(initialBytes)} exceeds ${formatKiB(MAX_INITIAL_JS_BYTES)}`);
  }
  if (initialGzipBytes > MAX_INITIAL_GZIP_BYTES) {
    fail(`initial gzip JS ${formatKiB(initialGzipBytes)} exceeds ${formatKiB(MAX_INITIAL_GZIP_BYTES)}`);
  }

  const entrySource = entryRecord.content.toString("utf-8");
  const featureChunks = [];
  for (const stem of FEATURE_STEMS) {
    const matches = names.filter((name) => name.startsWith(`${stem}.`));
    if (matches.length !== 1) fail(`expected one deferred ${stem} chunk, found ${matches.length}`);
    const relative = `assets/${matches[0]}`;
    if (preloads.has(relative)) fail(`${stem} must not be fetched during initial workspace load`);
    if (!entrySource.includes(matches[0])) fail(`entry does not reference deferred ${stem} chunk`);
    featureChunks.push(relative);
  }

  const deferredBytes = featureChunks.reduce((total, relative) => total + records.get(relative).bytes, 0);
  const deferredGzipBytes = featureChunks.reduce(
    (total, relative) => total + records.get(relative).gzipBytes,
    0,
  );
  return {
    entry,
    entryBytes: entryRecord.bytes,
    entryGzipBytes: entryRecord.gzipBytes,
    initial,
    initialBytes,
    initialGzipBytes,
    featureChunks,
    deferredBytes,
    deferredGzipBytes,
    chunkCount: records.size,
  };
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null;
if (invokedPath && pathToFileURL(invokedPath).href === import.meta.url) {
  try {
    const result = await inspectV1Bundle(process.argv[2] ?? "dist");
    process.stdout.write(
      `Web bundle gate passed: entry ${formatKiB(result.entryBytes)} `
      + `(gzip ${formatKiB(result.entryGzipBytes)}), initial JS ${formatKiB(result.initialBytes)} `
      + `(gzip ${formatKiB(result.initialGzipBytes)}), deferred features ${formatKiB(result.deferredBytes)} `
      + `(gzip ${formatKiB(result.deferredGzipBytes)}), ${result.chunkCount} chunks.\n`,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "unknown bundle failure";
    process.stderr.write(`Web bundle gate failed: ${message}\n`);
    process.exitCode = 1;
  }
}
