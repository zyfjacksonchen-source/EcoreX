const DOCUMENT_TYPE = "ecorex.public-bootstrap-discovery";
const TRUST = "untrusted-discovery-hint";
const CANONICAL_DISCOVERY_HOST = "dl.ecoremedia.net";
const RETIRED_DISCOVERY_HOST = "mvdcm.ecoremedia.net";
if (typeof window !== "undefined") {
  const discoveryPath = window.location.pathname.replace(/\/+$/, "");
  if (
    window.location.hostname === RETIRED_DISCOVERY_HOST
    && (discoveryPath === "/ecorex-agent" || discoveryPath === "/ecorex-agent/index.html")
  ) {
    window.location.replace(
      `https://${CANONICAL_DISCOVERY_HOST}${window.location.pathname}${window.location.search}${window.location.hash}`,
    );
  }
}
const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SAFE_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;
const ED25519_BASE64 = /^[A-Za-z0-9+/]{86}==$/;
const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;
const MAX_PUBLIC_INDEX_BYTES = 256 * 1024;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MANIFEST_FETCH_TIMEOUT_MS = 12_000;
const AUTHORITY_MAX_TTL_MS = 24 * 60 * 60 * 1000;
const AUTHORITY_FUTURE_SKEW_MS = 5 * 60 * 1000;
const AUTHORITY_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const TARGETS = [
  ["bootstrap-windows-x64", "windows", "x64"],
  ["bootstrap-macos-arm64", "macos", "arm64"],
  ["bootstrap-macos-x64", "macos", "x64"],
];
const SOURCE_ORDER = [
  ["github-cn-mirror", 0],
  ["github-release", 1],
  ["ecorex-cdn", 2],
];

function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 格式无效`);
  }
  return value;
}

function exactKeys(value, keys, label) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label} 字段不符合 v1 契约`);
  }
}

function text(value, label, pattern = null) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > 2048 ||
    (pattern && !pattern.test(value))
  ) {
    throw new Error(`${label} 格式无效`);
  }
  return value;
}

function signature(value, label) {
  const candidate = object(value, label);
  exactKeys(candidate, ["algorithm", "key_id", "value"], label);
  if (candidate.algorithm !== "ed25519") throw new Error(`${label} 算法无效`);
  text(candidate.key_id, `${label}.key_id`, SAFE_ID);
  text(candidate.value, `${label}.value`, ED25519_BASE64);
  return Object.freeze({
    algorithm: candidate.algorithm,
    keyId: candidate.key_id,
  });
}

function sourceList(value, label, fileName) {
  if (!Array.isArray(value) || value.length !== SOURCE_ORDER.length) {
    throw new Error(`${label} 必须包含三个有序下载源`);
  }
  const ids = new Set();
  return Object.freeze(value.map((raw, index) => {
    const candidate = object(raw, `${label}[${index}]`);
    exactKeys(candidate, ["source_id", "kind", "priority", "url"], `${label}[${index}]`);
    const [expectedKind, expectedPriority] = SOURCE_ORDER[index];
    if (candidate.kind !== expectedKind || candidate.priority !== expectedPriority) {
      throw new Error(`${label} 下载源顺序无效`);
    }
    const sourceId = text(candidate.source_id, `${label}.source_id`, SAFE_ID);
    if (ids.has(sourceId)) throw new Error(`${label} 下载源 ID 重复`);
    ids.add(sourceId);
    const rawUrl = text(candidate.url, `${label}.url`);
    const url = new URL(rawUrl);
    const suffix = `/${encodeURIComponent(fileName)}`;
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      !url.pathname.endsWith(suffix)
    ) {
      throw new Error(`${label} 只能使用无凭证 HTTPS URL`);
    }
    return Object.freeze({
      sourceId,
      kind: candidate.kind,
      priority: candidate.priority,
      url: url.href,
      baseUrl: url.href.slice(0, -suffix.length),
    });
  }));
}

function sameSources(reference, candidate, label) {
  if (candidate.some((source, index) => (
    source.sourceId !== reference[index].sourceId ||
    source.kind !== reference[index].kind ||
    source.priority !== reference[index].priority ||
    source.baseUrl !== reference[index].baseUrl
  ))) {
    throw new Error(`${label} 下载源身份与清单不一致`);
  }
}

export function normalizePublicIndex(raw, now = new Date()) {
  const root = object(raw, "公开索引");
  exactKeys(root, ["schema_version", "document_type", "trust", "status", "authority", "freshness", "release"], "公开索引");
  if (root.schema_version !== 1 || root.document_type !== DOCUMENT_TYPE || root.trust !== TRUST) {
    throw new Error("公开索引不是 EcoreX v1 discovery hint");
  }
  if (root.status === "unpublished") {
    if (root.authority !== null || root.freshness !== null || root.release !== null) throw new Error("未发布索引不得包含签名权威或下载信息");
    return Object.freeze({ status: "unpublished", trust: TRUST });
  }
  if (root.status !== "published") throw new Error("公开索引状态无效");

  const release = object(root.release, "release");
  exactKeys(
    release,
    [
      "release_id",
      "version",
      "channel",
      "created_at",
      "build_digest",
      "publication_receipt_sha256",
      "manifest",
      "bootstrap_artifacts",
    ],
    "release",
  );
  text(release.release_id, "release.release_id", /^release-stable-[0-9a-f]{24}$/);
  text(release.version, "release.version", /^1\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$/);
  if (release.channel !== "stable") throw new Error("公开索引只能呈现 stable");
  text(release.created_at, "release.created_at", RFC3339);
  if (Number.isNaN(Date.parse(release.created_at))) throw new Error("release.created_at 格式无效");
  text(release.build_digest, "release.build_digest", SHA256);
  text(release.publication_receipt_sha256, "release.publication_receipt_sha256", SHA256);

  const manifest = object(release.manifest, "release.manifest");
  exactKeys(manifest, ["file_name", "sha256", "signature", "sources"], "release.manifest");
  if (manifest.file_name !== "release-manifest.json") throw new Error("签名清单文件名无效");
  text(manifest.sha256, "release.manifest.sha256", SHA256);
  const manifestSignature = signature(manifest.signature, "release.manifest.signature");
  const manifestSources = sourceList(
    manifest.sources,
    "release.manifest.sources",
    manifest.file_name,
  );

  const authority = object(root.authority, "authority");
  exactKeys(
    authority,
    ["sequence", "revision", "target", "signature"],
    "authority",
  );
  const versionParts = release.version.split(".").map((value) => Number(value));
  const expectedSequence = versionParts[1] * 1_000_000 + versionParts[2] + 1;
  if (
    !Number.isSafeInteger(authority.sequence)
    || authority.sequence !== expectedSequence
    || authority.sequence < 1
    || authority.sequence > 999_999_999_999
    || authority.revision !== release.release_id
  ) {
    throw new Error("公开索引单调序列无效");
  }
  const freshness = object(root.freshness, "freshness");
  exactKeys(
    freshness,
    ["authority_sha256", "issued_at", "expires_at", "signature"],
    "freshness",
  );
  text(freshness.authority_sha256, "freshness.authority_sha256", SHA256);
  text(freshness.issued_at, "freshness.issued_at", AUTHORITY_TIME);
  text(freshness.expires_at, "freshness.expires_at", AUTHORITY_TIME);
  const observedNow = now instanceof Date ? now.getTime() : Number.NaN;
  const issuedAt = Date.parse(freshness.issued_at);
  const expiresAt = Date.parse(freshness.expires_at);
  if (
    !Number.isFinite(observedNow) ||
    !Number.isFinite(issuedAt) ||
    !Number.isFinite(expiresAt) ||
    expiresAt <= issuedAt ||
    expiresAt - issuedAt > AUTHORITY_MAX_TTL_MS ||
    issuedAt > observedNow + AUTHORITY_FUTURE_SKEW_MS ||
    observedNow >= expiresAt
  ) {
    throw new Error("公开索引签名新鲜度无效");
  }
  const freshnessSignature = signature(freshness.signature, "freshness.signature");
  const authorityTarget = object(authority.target, "authority.target");
  exactKeys(
    authorityTarget,
    ["manifest_sha256", "release_id", "version", "build_digest"],
    "authority.target",
  );
  if (
    authorityTarget.manifest_sha256 !== manifest.sha256
    || authorityTarget.release_id !== release.release_id
    || authorityTarget.version !== release.version
    || authorityTarget.build_digest !== release.build_digest
  ) {
    throw new Error("公开索引签名目标与发布不一致");
  }
  const authoritySignature = signature(authority.signature, "authority.signature");

  if (!Array.isArray(release.bootstrap_artifacts) || release.bootstrap_artifacts.length !== TARGETS.length) {
    throw new Error("stable 必须同时提供三个 Bootstrap 目标");
  }
  const artifacts = Object.freeze(release.bootstrap_artifacts.map((rawArtifact, index) => {
    const artifact = object(rawArtifact, `bootstrap_artifacts[${index}]`);
    exactKeys(
      artifact,
      [
        "artifact_id",
        "platform",
        "architecture",
        "file_name",
        "size_bytes",
        "sha256",
        "signature",
        "sources",
      ],
      `bootstrap_artifacts[${index}]`,
    );
    const [artifactId, platform, architecture] = TARGETS[index];
    if (
      artifact.artifact_id !== artifactId ||
      artifact.platform !== platform ||
      artifact.architecture !== architecture
    ) {
      throw new Error("Bootstrap 平台集合或顺序无效");
    }
    text(artifact.file_name, `${artifactId}.file_name`, SAFE_FILE_NAME);
    if (
      !Number.isInteger(artifact.size_bytes) ||
      artifact.size_bytes < 1 ||
      artifact.size_bytes > 10 * 1024 * 1024
    ) {
      throw new Error(`${artifactId} 大小无效`);
    }
    text(artifact.sha256, `${artifactId}.sha256`, SHA256);
    const artifactSignature = signature(artifact.signature, `${artifactId}.signature`);
    const sources = sourceList(artifact.sources, `${artifactId}.sources`, artifact.file_name);
    sameSources(manifestSources, sources, artifactId);
    return Object.freeze({
      artifactId,
      platform,
      architecture,
      fileName: artifact.file_name,
      sizeBytes: artifact.size_bytes,
      sha256: artifact.sha256,
      signature: artifactSignature,
      sources,
    });
  }));

  return Object.freeze({
    status: "published",
    trust: TRUST,
    authority: Object.freeze({
      sequence: authority.sequence,
      revision: authority.revision,
      target: Object.freeze({ ...authorityTarget }),
      signature: authoritySignature,
    }),
    freshness: Object.freeze({
      authoritySha256: freshness.authority_sha256,
      issuedAt: freshness.issued_at,
      expiresAt: freshness.expires_at,
      signature: freshnessSignature,
    }),
    release: Object.freeze({
      releaseId: release.release_id,
      version: release.version,
      createdAt: release.created_at,
      buildDigest: release.build_digest,
      receiptSha256: release.publication_receipt_sha256,
      manifest: Object.freeze({
        fileName: manifest.file_name,
        sha256: manifest.sha256,
        signature: manifestSignature,
        sources: manifestSources,
      }),
      artifacts,
    }),
  });
}

export async function sha256Hex(payload, cryptoImpl = globalThis.crypto) {
  if (!(payload instanceof ArrayBuffer) || !cryptoImpl?.subtle) {
    throw new Error("当前浏览器无法执行 SHA-256 字节核对");
  }
  const digest = await cryptoImpl.subtle.digest("SHA-256", payload);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function readBoundedBody(response, maximumBytes) {
  if (!response.body?.getReader) {
    const payload = await response.arrayBuffer();
    if (payload.byteLength < 1 || payload.byteLength > maximumBytes) {
      throw new Error("签名清单大小无效");
    }
    return payload;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new Error("签名清单响应无效");
      total += value.byteLength;
      if (total > maximumBytes) {
        await reader.cancel("manifest exceeds byte limit");
        throw new Error("签名清单超过 1 MiB 上限");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  if (total < 1) throw new Error("签名清单为空");
  const payload = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    payload.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return payload.buffer;
}

export async function verifyManifestBytes(
  manifest,
  { fetchImpl = globalThis.fetch, cryptoImpl = globalThis.crypto } = {},
) {
  if (typeof fetchImpl !== "function") {
    throw new Error("当前浏览器无法读取签名清单");
  }
  for (const source of manifest.sources) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), MANIFEST_FETCH_TIMEOUT_MS);
    try {
      const response = await fetchImpl(source.url, {
        cache: "no-store",
        credentials: "omit",
        redirect: "follow",
        signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (!response.ok) continue;
      const finalUrl = new URL(response.url || source.url);
      if (finalUrl.protocol !== "https:" || finalUrl.username || finalUrl.password) continue;
      const contentLength = response.headers.get("content-length");
      if (contentLength !== null) {
        if (!/^[0-9]+$/.test(contentLength) || Number(contentLength) > MAX_MANIFEST_BYTES) {
          continue;
        }
      }
      const payload = await readBoundedBody(response, MAX_MANIFEST_BYTES);
      if (await sha256Hex(payload, cryptoImpl) !== manifest.sha256) continue;
      return Object.freeze({
        sourceId: source.sourceId,
        kind: source.kind,
        sha256: manifest.sha256,
      });
    } catch {
      // A source may be offline or reject browser CORS. Try the next exact origin.
    } finally {
      clearTimeout(timeout);
    }
  }
  throw new Error("三个来源的签名清单均未通过 exact SHA-256 字节核对");
}

function createElement(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("ecorex-site-theme", theme);
  } catch {
    // Theme persistence is optional; the release path remains available.
  }
  const icon = document.querySelector("[data-theme-icon]");
  if (icon) icon.textContent = theme === "dark" ? "☀" : "◐";
}

function detectTarget() {
  const source = `${navigator.platform || ""} ${navigator.userAgent || ""}`.toLowerCase();
  if (/mac|iphone|ipad|ipod/.test(source)) {
    if (/arm64|aarch64|apple silicon/.test(source)) return "bootstrap-macos-arm64";
    if (/x86_64|x86-64|intel mac/.test(source)) return "bootstrap-macos-x64";
    return null;
  }
  return /win/.test(source) ? "bootstrap-windows-x64" : null;
}

function formatSize(sizeBytes) {
  return `${(sizeBytes / 1024 / 1024).toFixed(2)} MiB`;
}

function shortSha(value) {
  return `${value.slice(0, 12)}…`;
}

function cardCopy(artifact) {
  if (artifact.artifactId === "bootstrap-windows-x64") {
    return ["Win", "Windows x64", "Windows 10/11 x64 的签名 Bootstrap。"];
  }
  if (artifact.artifactId === "bootstrap-macos-arm64") {
    return ["Mac", "macOS Apple Silicon", "M 系列 Mac 的签名 Bootstrap。"];
  }
  return ["Mac", "macOS Intel", "Intel Mac 的签名 Bootstrap。"];
}

function renderArtifactCard(grid, release, artifact, recommended) {
  const [icon, title, body] = cardCopy(artifact);
  const article = createElement("article", `download-card${recommended ? " is-recommended" : ""}`);
  if (recommended) article.append(createElement("span", "recommend-badge", "当前设备"));
  article.append(createElement("span", "platform-icon", icon));
  article.append(createElement("h3", "", title));
  article.append(createElement("small", "card-version", `v${release.version}`));
  article.append(createElement("p", "", `${body} 启动后会先验签发布清单，再安装 Core。`));

  const meta = createElement("div", "meta");
  meta.append(createElement("span", "", formatSize(artifact.sizeBytes)));
  const digest = createElement("span", "", `SHA-256 提示: ${shortSha(artifact.sha256)}`);
  digest.title = artifact.sha256;
  meta.append(digest);
  meta.append(createElement("span", "", `Ed25519 key hint: ${artifact.signature.keyId}`));
  article.append(meta);

  const primary = createElement("a", "download-link", "下载 Bootstrap（启动后验签）");
  primary.href = artifact.sources[0].url;
  primary.rel = "noopener noreferrer";
  primary.download = artifact.fileName;
  article.append(primary);

  const details = createElement("details", "source-fallbacks");
  details.append(createElement("summary", "", "备用下载源"));
  const sourceLabels = ["国内 GitHub 镜像", "GitHub Releases", "EcoreX CDN"];
  artifact.sources.forEach((source, index) => {
    const link = createElement("a", "", sourceLabels[index]);
    link.href = source.url;
    link.rel = "noopener noreferrer";
    link.download = artifact.fileName;
    details.append(link);
  });
  article.append(details);
  grid.append(article);
}

function renderIndex(index, manifestCheck = null) {
  const grid = document.querySelector("[data-downloads]");
  if (!grid) return;
  grid.replaceChildren();
  if (index.status !== "published") {
    const card = createElement("article", "download-card");
    card.append(createElement("span", "platform-icon", "···"));
    card.append(createElement("h3", "", "v1.0 Bootstrap 尚未发布"));
    card.append(createElement("p", "", "三平台制品和三个发布源全部通过签名与一致性门禁后，下载才会开启。"));
    card.append(createElement("span", "download-link is-disabled", "不可下载"));
    grid.append(card);
    return;
  }

  const release = index.release;
  const manifestCheckNode = document.querySelector("[data-manifest-check]");
  if (manifestCheckNode) {
    manifestCheckNode.textContent = manifestCheck
      ? `字节摘要已核对（${manifestCheck.kind}）`
      : "等待 Bootstrap 验签";
  }
  document.querySelectorAll("[data-version]").forEach((node) => {
    node.textContent = release.version;
  });
  const siteVersion = document.querySelector("[data-site-version]");
  if (siteVersion) siteVersion.textContent = `v${release.version}`;
  const updated = document.querySelector("[data-updated]");
  if (updated) updated.textContent = release.createdAt.slice(0, 10);
  const manifestLink = document.querySelector("[data-manifest-link]");
  if (manifestLink) {
    manifestLink.href = release.manifest.sources[0].url;
    manifestLink.hidden = false;
  }
  const recommended = detectTarget();
  const ordered = [...release.artifacts].sort((left, right) => (
    left.artifactId === recommended ? -1 : right.artifactId === recommended ? 1 : 0
  ));
  ordered.forEach((artifact) => {
    renderArtifactCard(grid, release, artifact, artifact.artifactId === recommended);
  });
}

async function loadIndex() {
  const response = await fetch("./public-bootstrap-index.json", {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`public index HTTP ${response.status}`);
  const length = Number(response.headers.get("content-length") || 0);
  if (!Number.isInteger(length) || length > MAX_PUBLIC_INDEX_BYTES) {
    throw new Error("公开索引 Content-Length 无效");
  }
  const payload = await response.text();
  if (new TextEncoder().encode(payload).byteLength > MAX_PUBLIC_INDEX_BYTES) {
    throw new Error("公开索引超过 256 KiB 上限");
  }
  return normalizePublicIndex(JSON.parse(payload));
}

function renderFailure(error) {
  const grid = document.querySelector("[data-downloads]");
  if (!grid) return;
  grid.replaceChildren();
  const card = createElement("article", "download-card");
  card.append(createElement("span", "platform-icon", "!"));
  card.append(createElement("h3", "", "发布索引不可用"));
  card.append(createElement("p", "", error instanceof Error ? error.message : "请稍后刷新。"));
  card.append(createElement("span", "download-link is-disabled", "不可下载"));
  grid.append(card);
}

if (typeof document !== "undefined") {
  let preferred = "light";
  try {
    preferred = localStorage.getItem("ecorex-site-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  } catch {
    preferred = "light";
  }
  setTheme(preferred);
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  loadIndex()
    .then(async (index) => {
      const manifestCheck = index.status === "published"
        ? await verifyManifestBytes(index.release.manifest)
        : null;
      renderIndex(index, manifestCheck);
    })
    .catch(renderFailure);
}
