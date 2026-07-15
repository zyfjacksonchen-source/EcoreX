import { createHash, randomUUID } from "node:crypto";
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  writeFile
} from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const RUNTIME_MARKER = "<!--__ECOREX_RUNTIME_CONFIG__-->";
const HASH_PREFIX_LENGTH = 16;
const MAX_FILES = 4096;
const MAX_FILE_BYTES = 150 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 150 * 1024 * 1024;
const ALLOWED_SUFFIXES = new Set([
  ".avif",
  ".css",
  ".eot",
  ".gif",
  ".ico",
  ".jpeg",
  ".jpg",
  ".js",
  ".json",
  ".mp3",
  ".mp4",
  ".ogg",
  ".otf",
  ".png",
  ".svg",
  ".ttf",
  ".wasm",
  ".wav",
  ".webm",
  ".webmanifest",
  ".webp",
  ".woff",
  ".woff2"
]);
const TEXT_SUFFIXES = new Set([".css", ".js", ".json", ".svg", ".webmanifest"]);
const LEGACY_MARKERS = [
  "chat.html",
  "channel/web/",
  "dist-electron",
  "ecorex-v029-",
  "ecorex-v030-",
  "webui-overlay",
  "/static/app/"
];
const WINDOWS_RESERVED = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;
// Match a complete string literal using its own opening quote. The old
// character class rejected the *other* quote styles too, so a minified chunk
// containing mixed strings could skip a later dynamic-import asset entirely
// and misclassify it as an orphan.
const REFERENCE_TOKEN = /(["'`])((?:\\.|(?!\1)[^\\\r\n]){1,2048})\1|url\(\s*([^\)\r\n]{1,2048})\s*\)/gi;
const LOCAL_ASSET_LITERAL = /(["'`])((?:\.{1,2}\/|\/?assets\/)[^"'`\r\n]{1,2048})\1/gi;
const UTF8 = new TextDecoder("utf-8", { fatal: true });

export class ProductionAssetError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProductionAssetError";
  }
}

function fail(message) {
  throw new ProductionAssetError(message);
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function portableSegment(value, label) {
  if (
    !value ||
    value === "." ||
    value === ".." ||
    value.startsWith(".") ||
    /[\u0000-\u001f<>:"/\\|?*]/u.test(value) ||
    /[ .]$/u.test(value) ||
    WINDOWS_RESERVED.test(value)
  ) {
    fail(`${label} is not a portable production path segment: ${JSON.stringify(value)}`);
  }
}

function portableAssetPath(value) {
  if (value.includes("\\") || path.posix.normalize(value) !== value) {
    fail(`asset path is not normalized: ${JSON.stringify(value)}`);
  }
  const segments = value.split("/");
  if (segments[0] !== "assets" || segments.length < 2) {
    fail(`asset path must remain below assets/: ${JSON.stringify(value)}`);
  }
  for (const segment of segments) portableSegment(segment, "asset path");
}

async function requireDirectory(target, label) {
  let metadata;
  try {
    metadata = await lstat(target);
  } catch {
    fail(`${label} does not exist`);
  }
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    fail(`${label} must be a real directory`);
  }
  return metadata;
}

async function collectAssetFiles(root) {
  const rootEntries = await readdir(root, { withFileTypes: true });
  const rootNames = rootEntries.map((entry) => entry.name).sort();
  if (rootNames.length !== 2 || rootNames[0] !== "assets" || rootNames[1] !== "index.html") {
    fail("dist must contain exactly index.html and assets/");
  }
  const indexEntry = rootEntries.find((entry) => entry.name === "index.html");
  const assetsEntry = rootEntries.find((entry) => entry.name === "assets");
  if (!indexEntry?.isFile() || indexEntry.isSymbolicLink()) {
    fail("index.html must be a real regular file");
  }
  if (!assetsEntry?.isDirectory() || assetsEntry.isSymbolicLink()) {
    fail("assets must be a real directory");
  }

  const rootReal = await realpath(root);
  const pending = [path.join(root, "assets")];
  const records = new Map();
  let totalBytes = 0;
  while (pending.length) {
    const directory = pending.pop();
    const entries = await readdir(directory, { withFileTypes: true });
    if (!entries.length) fail("assets cannot contain an empty directory");
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      portableSegment(entry.name, "asset path");
      const absolute = path.join(directory, entry.name);
      const metadata = await lstat(absolute);
      if (entry.isSymbolicLink() || metadata.isSymbolicLink()) {
        fail("assets cannot contain links or reparse points");
      }
      if (entry.isDirectory() && metadata.isDirectory()) {
        const resolved = await realpath(absolute);
        if (resolved !== rootReal && !resolved.startsWith(`${rootReal}${path.sep}`)) {
          fail("asset directory escapes the production dist");
        }
        pending.push(absolute);
        continue;
      }
      if (!entry.isFile() || !metadata.isFile()) {
        fail("assets can contain only regular files and directories");
      }
      if (records.size >= MAX_FILES) fail("production Web bundle contains too many files");
      if (metadata.size < 1 || metadata.size > MAX_FILE_BYTES) {
        fail("production asset is empty or exceeds the per-file size limit");
      }
      totalBytes += metadata.size;
      if (totalBytes > MAX_BUNDLE_BYTES) fail("production Web bundle exceeds 150 MiB");
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      portableAssetPath(relative);
      const suffix = path.posix.extname(relative).toLowerCase();
      if (!ALLOWED_SUFFIXES.has(suffix)) {
        fail(`asset type is not allowed in production: ${JSON.stringify(relative)}`);
      }
      const content = await readFile(absolute);
      records.set(relative, { relative, suffix, content });
    }
  }
  if (!records.size) fail("production Web bundle must contain at least one asset");
  return records;
}

function decodeText(content, source) {
  try {
    return UTF8.decode(content);
  } catch {
    fail(`text asset must be valid UTF-8: ${JSON.stringify(source)}`);
  }
}

function splitReference(raw) {
  const query = raw.indexOf("?");
  const fragment = raw.indexOf("#");
  let boundary = raw.length;
  if (query >= 0) boundary = Math.min(boundary, query);
  if (fragment >= 0) boundary = Math.min(boundary, fragment);
  return { pathname: raw.slice(0, boundary), suffix: raw.slice(boundary) };
}

function referenceCandidate(source, raw) {
  if (!raw || raw.startsWith("#") || raw.startsWith("//")) return null;
  if (/^[a-z][a-z0-9+.-]*:/iu.test(raw)) return null;
  if (raw.includes("\\") || /[\u0000-\u001f]/u.test(raw)) {
    const unsafePath = splitReference(raw.replaceAll("\\", "/")).pathname;
    const unsafeSuffix = path.posix.extname(unsafePath).toLowerCase();
    if (
      ALLOWED_SUFFIXES.has(unsafeSuffix) &&
      (raw.startsWith(".") || raw.startsWith("/") || raw.toLowerCase().includes("assets") || raw.includes("/"))
    ) {
      fail(`unsafe local asset reference in ${JSON.stringify(source)}`);
    }
    return null;
  }
  const { pathname, suffix } = splitReference(raw);
  if (!pathname) return null;
  let candidate;
  let style;
  if (pathname.startsWith("/assets/")) {
    candidate = pathname.slice(1);
    style = "absolute";
  } else if (pathname.startsWith("/")) {
    candidate = pathname.slice(1);
    style = "absolute";
  } else if (pathname.startsWith("assets/")) {
    candidate = pathname;
    style = "root-relative";
  } else {
    candidate = path.posix.normalize(path.posix.join(path.posix.dirname(source), pathname));
    style = pathname.startsWith("./") ? "dot-relative" : "relative";
  }
  return { candidate, pathname, suffix, style };
}

function isMissingAssetReference(raw, candidate, isUrlFunction) {
  const suffix = path.posix.extname(candidate).toLowerCase();
  return (
    ALLOWED_SUFFIXES.has(suffix) &&
    (isUrlFunction || raw.startsWith(".") || raw.startsWith("/") || raw.startsWith("assets/") || raw.includes("/"))
  );
}

function renderReference(source, parsed, target) {
  let rewritten;
  if (parsed.style === "absolute") {
    rewritten = `/${target}`;
  } else if (parsed.style === "root-relative") {
    rewritten = target;
  } else {
    rewritten = path.posix.relative(path.posix.dirname(source), target);
    if (!rewritten) fail("asset cannot reference itself through an empty path");
    if (parsed.style === "dot-relative" && !rewritten.startsWith(".")) {
      rewritten = `./${rewritten}`;
    }
  }
  return `${rewritten}${parsed.suffix}`;
}

function analyzeReferences(source, content, knownPaths, rewrittenPaths = null) {
  const dependencies = new Set();
  const missing = new Set();
  let cursor = 0;
  let output = "";
  REFERENCE_TOKEN.lastIndex = 0;
  LOCAL_ASSET_LITERAL.lastIndex = 0;
  const specificMatches = [...content.matchAll(LOCAL_ASSET_LITERAL)];
  const matches = [
    ...specificMatches,
    ...[...content.matchAll(REFERENCE_TOKEN)].filter((candidate) => {
      const start = candidate.index;
      const end = start + candidate[0].length;
      // A generic string token can start or end on the quote that also
      // delimits a local asset literal in minified JavaScript. Keeping both
      // matches makes the rewrite cursor move backwards and duplicates that
      // quote (for example import(""./chunk.js"")). Local asset literals are
      // more specific, so discard every generic token whose half-open range
      // overlaps one of them, including a one-character quote overlap.
      return !specificMatches.some((specific) => {
        const specificStart = specific.index;
        const specificEnd = specificStart + specific[0].length;
        return start < specificEnd && specificStart < end;
      });
    }),
  ].sort((left, right) => left.index - right.index);
  for (const match of matches) {
    const quote = match[1] || null;
    let raw = quote ? match[2] : match[3].trim();
    let urlQuote = "";
    if (!quote && raw.length >= 2 && raw[0] === raw.at(-1) && (raw[0] === '"' || raw[0] === "'")) {
      urlQuote = raw[0];
      raw = raw.slice(1, -1).trim();
    }
    const parsed = referenceCandidate(source, raw);
    let replacement = match[0];
    if (parsed && knownPaths.has(parsed.candidate)) {
      dependencies.add(parsed.candidate);
      if (rewrittenPaths) {
        const target = rewrittenPaths.get(parsed.candidate);
        if (!target) fail(`dependency was not content-addressed before ${JSON.stringify(source)}`);
        const rendered = renderReference(source, parsed, target);
        replacement = quote
          ? `${quote}${rendered}${quote}`
          : `url(${urlQuote}${rendered}${urlQuote})`;
      }
    } else if (parsed && isMissingAssetReference(raw, parsed.candidate, !quote)) {
      missing.add(parsed.candidate);
    }
    if (rewrittenPaths) {
      output += content.slice(cursor, match.index) + replacement;
      cursor = match.index + match[0].length;
    }
  }
  if (missing.size) {
    fail(
      `${JSON.stringify(source)} references missing production assets: ${JSON.stringify([...missing].sort())}`
    );
  }
  if (rewrittenPaths) output += content.slice(cursor);
  return { dependencies, output: rewrittenPaths ? output : content };
}

function assertNoLegacyContent(source, content) {
  const folded = content.toLowerCase();
  if (LEGACY_MARKERS.some((marker) => folded.includes(marker))) {
    fail(`${JSON.stringify(source)} contains a legacy bundle or overlay reference`);
  }
}

function validateIndex(index, knownPaths) {
  if (index.split(RUNTIME_MARKER).length - 1 !== 1) {
    fail("index.html must contain exactly one EcoreX runtime marker");
  }
  assertNoLegacyContent("index.html", index);
  const folded = index.toLowerCase();
  const headStart = folded.search(/<head(?:\s|>)/u);
  const headEnd = folded.indexOf("</head>", headStart + 1);
  const markerAt = index.indexOf(RUNTIME_MARKER);
  if (headStart < 0 || headEnd < 0 || markerAt < headStart || markerAt > headEnd) {
    fail("EcoreX runtime marker must be inside <head>");
  }
  if (
    /<style(?:\s|>)/iu.test(index) ||
    /\sstyle\s*=/iu.test(index) ||
    /\son[a-z0-9_-]+\s*=/iu.test(index) ||
    /<(?:base|embed|frame|iframe|object)(?:\s|>)/iu.test(index) ||
    /<meta\b[^>]*http-equiv\s*=\s*["']?(?:content-security-policy|refresh)/iu.test(index)
  ) {
    fail("index.html contains CSP-incompatible inline or embedded content");
  }
  const openingScripts = index.match(/<script(?:\s|>)/giu) ?? [];
  const scripts = [...index.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/giu)];
  if (!openingScripts.length || scripts.length !== openingScripts.length) {
    fail("index.html must contain closed external script elements");
  }
  for (const script of scripts) {
    if (!/\ssrc\s*=\s*(["'])[^"']+\1/iu.test(script[1]) || script[2].trim()) {
      fail("index.html cannot contain inline script content");
    }
  }
  const resourceAttributes = new Map([
    ["audio", ["src"]],
    ["image", ["href", "xlink:href"]],
    ["img", ["src"]],
    ["input", ["src"]],
    ["link", ["href"]],
    ["script", ["src"]],
    ["source", ["src"]],
    ["track", ["src"]],
    ["use", ["href", "xlink:href"]],
    ["video", ["poster", "src"]]
  ]);
  const direct = new Set();
  for (const tagMatch of index.matchAll(/<([a-z][a-z0-9:-]*)\b([^>]*)>/giu)) {
    const tag = tagMatch[1].toLowerCase();
    const allowed = resourceAttributes.get(tag);
    if (!allowed) continue;
    for (const attribute of allowed) {
      const escaped = attribute.replace(":", "\\:");
      const attributePattern = new RegExp(
        `(?:^|\\s)${escaped}\\s*=\\s*(["'])([^"']+)\\1`,
        "iu"
      );
      const attributeMatch = attributePattern.exec(tagMatch[2]);
      if (!attributeMatch) continue;
      const parsed = referenceCandidate("index.html", attributeMatch[2].trim());
      if (!parsed || parsed.suffix || !knownPaths.has(parsed.candidate)) {
        fail(`index.html ${tag} ${attribute} must reference one fixed production asset`);
      }
      direct.add(parsed.candidate);
    }
  }
  analyzeReferences("index.html", index, knownPaths);
  if (!direct.size) fail("index.html must reference production assets");
  return direct;
}

function assertReachable(records, graph, direct) {
  const reachable = new Set(direct);
  const pending = [...direct];
  while (pending.length) {
    const current = pending.pop();
    for (const dependency of graph.get(current) ?? []) {
      if (!reachable.has(dependency)) {
        reachable.add(dependency);
        pending.push(dependency);
      }
    }
  }
  const orphaned = [...records.keys()].filter((candidate) => !reachable.has(candidate)).sort();
  if (orphaned.length) {
    fail(`production dist contains orphaned assets: ${JSON.stringify(orphaned)}`);
  }
}

function logicalStem(relative, suffix) {
  const base = path.posix.basename(relative, suffix);
  const shaMatch = /^(.*)\.[0-9a-f]{16}$/iu.exec(base);
  const rollupMatch = /^(.*)\.unhashed-[a-z0-9_-]{8,64}$/iu.exec(base);
  if (!shaMatch && !rollupMatch) {
    fail(
      `asset is missing the explicit Rollup staging marker or final SHA-256 prefix: ${JSON.stringify(relative)}`
    );
  }
  const stem = (shaMatch?.[1] ?? rollupMatch?.[1] ?? "").trim();
  if (!stem) fail(`asset has an empty logical name: ${JSON.stringify(relative)}`);
  portableSegment(`${stem}${suffix}`, "content-addressed asset name");
  return stem;
}

function contentAddressedPath(relative, suffix, digest) {
  const directory = path.posix.dirname(relative);
  const filename = `${logicalStem(relative, suffix)}.${digest.slice(0, HASH_PREFIX_LENGTH)}${suffix}`;
  const result = directory === "." ? filename : `${directory}/${filename}`;
  portableAssetPath(result);
  return result;
}

function createBuildPlan(index, records) {
  const knownPaths = new Set(records.keys());
  const graph = new Map();
  const decoded = new Map();
  for (const record of records.values()) {
    if (!TEXT_SUFFIXES.has(record.suffix)) {
      graph.set(record.relative, new Set());
      continue;
    }
    const content = decodeText(record.content, record.relative);
    assertNoLegacyContent(record.relative, content);
    decoded.set(record.relative, content);
    graph.set(record.relative, analyzeReferences(record.relative, content, knownPaths).dependencies);
  }
  const direct = validateIndex(index, knownPaths);
  assertReachable(records, graph, direct);

  const state = new Map();
  const targetPaths = new Map();
  const finalContents = new Map();
  const claimedTargets = new Map();
  const visit = (relative, stack) => {
    const current = state.get(relative);
    if (current === "done") return;
    if (current === "visiting") {
      fail(`cyclic asset references cannot be content-addressed safely: ${[...stack, relative].join(" -> ")}`);
    }
    state.set(relative, "visiting");
    const nextStack = [...stack, relative];
    for (const dependency of [...(graph.get(relative) ?? [])].sort()) visit(dependency, nextStack);
    const record = records.get(relative);
    let content = record.content;
    if (decoded.has(relative)) {
      const rewritten = analyzeReferences(relative, decoded.get(relative), knownPaths, targetPaths).output;
      content = Buffer.from(rewritten, "utf-8");
    }
    const digest = sha256(content);
    const target = contentAddressedPath(relative, record.suffix, digest);
    const owner = claimedTargets.get(target);
    if (owner && owner !== relative) {
      fail(`two production assets collapse to the same content-addressed path: ${JSON.stringify(target)}`);
    }
    claimedTargets.set(target, relative);
    targetPaths.set(relative, target);
    finalContents.set(relative, content);
    state.set(relative, "done");
  };
  for (const relative of [...records.keys()].sort()) visit(relative, []);

  const rewrittenIndex = analyzeReferences("index.html", index, knownPaths, targetPaths).output;
  const finalKnown = new Set(targetPaths.values());
  const finalGraph = new Map();
  for (const [source, content] of finalContents) {
    const target = targetPaths.get(source);
    if (TEXT_SUFFIXES.has(records.get(source).suffix)) {
      const text = decodeText(content, target);
      assertNoLegacyContent(target, text);
      finalGraph.set(target, analyzeReferences(target, text, finalKnown).dependencies);
    } else {
      finalGraph.set(target, new Set());
    }
    const digest = sha256(content);
    if (!path.posix.basename(target).toLowerCase().includes(digest.slice(0, HASH_PREFIX_LENGTH))) {
      fail(`final asset name does not contain its SHA-256 prefix: ${JSON.stringify(target)}`);
    }
  }
  const finalDirect = validateIndex(rewrittenIndex, finalKnown);
  const finalRecords = new Map([...finalKnown].map((relative) => [relative, true]));
  assertReachable(finalRecords, finalGraph, finalDirect);
  return { rewrittenIndex, targetPaths, finalContents };
}

async function writePlan(staging, plan) {
  await mkdir(path.join(staging, "assets"), { recursive: false });
  for (const source of [...plan.targetPaths.keys()].sort()) {
    const relative = plan.targetPaths.get(source);
    const target = path.join(staging, ...relative.split("/"));
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, plan.finalContents.get(source), { flag: "wx" });
  }
  await writeFile(path.join(staging, "index.html"), plan.rewrittenIndex, { encoding: "utf-8", flag: "wx" });
}

async function swapDirectory(root, staging) {
  const parent = path.dirname(root);
  const backup = path.join(parent, `.${path.basename(root)}.pre-rehash-${randomUUID()}`);
  await rename(root, backup);
  try {
    await rename(staging, root);
  } catch (error) {
    try {
      await rename(backup, root);
    } catch {
      fail("failed to activate the hashed dist and failed to restore the original dist");
    }
    throw error;
  }
  await rm(backup, { recursive: true, force: true });
}

export async function rehashProductionDist(distDirectory) {
  if (typeof distDirectory !== "string" || !distDirectory.trim()) {
    fail("dist directory must be a non-empty path");
  }
  const root = path.resolve(distDirectory);
  await requireDirectory(root, "production dist");
  const parent = path.dirname(root);
  await requireDirectory(parent, "production dist parent");
  const rootReal = await realpath(root);
  const parentReal = await realpath(parent);
  if (path.dirname(rootReal) !== parentReal) {
    fail("production dist cannot be reached through a link or reparse point");
  }
  const indexBytes = await readFile(path.join(root, "index.html"));
  if (indexBytes.length < 1 || indexBytes.length > MAX_FILE_BYTES) {
    fail("index.html is empty or exceeds the per-file size limit");
  }
  const index = decodeText(indexBytes, "index.html");
  const records = await collectAssetFiles(root);
  const plan = createBuildPlan(index, records);
  const staging = await mkdtemp(path.join(parent, `.${path.basename(root)}.rehash-`));
  let activated = false;
  try {
    await writePlan(staging, plan);
    await swapDirectory(root, staging);
    activated = true;
  } finally {
    if (!activated) await rm(staging, { recursive: true, force: true });
  }
  return {
    assetCount: plan.targetPaths.size,
    assets: [...plan.targetPaths.values()].sort()
  };
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null;
if (invokedPath && pathToFileURL(invokedPath).href === import.meta.url) {
  try {
    const result = await rehashProductionDist(process.argv[2] ?? "dist");
    process.stdout.write(`Content-addressed ${result.assetCount} production Web assets.\n`);
  } catch (error) {
    const message = error instanceof Error ? error.message : "unknown build failure";
    process.stderr.write(`Production Web asset gate failed: ${message}\n`);
    process.exitCode = 1;
  }
}
