const MAX_INDEX_BYTES = 64 * 1024;
const VERSION = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$/;
const TARGETS = Object.freeze({
  "windows-x64": Object.freeze({ platform: "windows", architecture: "x64", label: "Windows x64" }),
  "macos-arm64": Object.freeze({ platform: "macos", architecture: "arm64", label: "macOS Apple Silicon" }),
  "macos-x64": Object.freeze({ platform: "macos", architecture: "x64", label: "macOS Intel" }),
});

function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 格式无效`);
  return value;
}

function exactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  expected = [...expected].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label} 字段无效`);
  }
}

function safeText(value, label, pattern) {
  if (typeof value !== "string" || !pattern.test(value)) throw new Error(`${label} 格式无效`);
  return value;
}

export function normalizeDownloadIndex(raw) {
  const index = object(raw, "下载索引");
  exactKeys(index, ["schema_version", "product", "version", "released_at", "downloads"], "下载索引");
  if (index.schema_version !== 1 || index.product !== "e-Mate") throw new Error("下载索引身份无效");
  safeText(index.version, "版本", VERSION);
  if (typeof index.released_at !== "string" || Number.isNaN(Date.parse(index.released_at))) {
    throw new Error("发布时间无效");
  }
  if (!Array.isArray(index.downloads) || index.downloads.length !== 3) throw new Error("下载目标不完整");
  const seen = new Set();
  const downloads = index.downloads.map((rawDownload) => {
    const download = object(rawDownload, "下载目标");
    exactKeys(download, ["target", "platform", "architecture", "file_name", "url", "size_bytes", "sha256"], "下载目标");
    const target = TARGETS[download.target];
    if (!target || seen.has(download.target)) throw new Error("下载目标重复或未知");
    seen.add(download.target);
    if (download.platform !== target.platform || download.architecture !== target.architecture) {
      throw new Error("下载目标平台不一致");
    }
    safeText(download.file_name, "下载文件名", SAFE_NAME);
    safeText(download.sha256, "下载摘要", SHA256);
    if (!Number.isSafeInteger(download.size_bytes) || download.size_bytes < 1) throw new Error("下载大小无效");
    const expectedUrl = `https://mvdcm.ecoremedia.net/e-mate/update/${download.file_name}`;
    if (download.url !== expectedUrl) throw new Error("下载地址无效");
    return Object.freeze({ ...download, label: target.label });
  });
  if (seen.size !== Object.keys(TARGETS).length) throw new Error("下载目标不完整");
  return Object.freeze({ ...index, downloads: Object.freeze(downloads) });
}

export function targetFromPlatformSignals({ source = "", architecture = "", renderer = "" }) {
  source = String(source).toLowerCase();
  architecture = String(architecture).toLowerCase();
  renderer = String(renderer).toLowerCase();
  if (/mac|iphone|ipad|ipod/.test(source)) {
    if (/(?:^|\W)(?:arm|arm64|aarch64)(?:\W|$)|apple silicon/.test(`${source} ${architecture}`)
      || /apple (?:m[1-9]|a[1-9][0-9])|apple gpu/.test(renderer)) return "macos-arm64";
    if (/x86_64|x86-64|amd64/.test(`${source} ${architecture}`)) return "macos-x64";
    return null;
  }
  return /win/.test(source) ? "windows-x64" : null;
}

async function detectTarget() {
  const source = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  let architecture = "";
  try {
    architecture = (await navigator.userAgentData?.getHighEntropyValues?.(["architecture"]))?.architecture || "";
  } catch {}
  let renderer = "";
  try {
    const context = document.createElement("canvas").getContext("webgl");
    const extension = context?.getExtension("WEBGL_debug_renderer_info");
    renderer = extension ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL) : "";
  } catch {}
  return targetFromPlatformSignals({ source, architecture, renderer });
}

async function fetchIndex(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8_000);
  try {
    const response = await fetch(url, {
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`download index HTTP ${response.status}`);
    const contentLength = Number(response.headers.get("content-length") || 0);
    if (contentLength > MAX_INDEX_BYTES) throw new Error("下载索引过大");
    const payload = await response.text();
    if (new TextEncoder().encode(payload).byteLength > MAX_INDEX_BYTES) throw new Error("下载索引过大");
    return normalizeDownloadIndex(JSON.parse(payload));
  } finally {
    clearTimeout(timeout);
  }
}

export function indexSources({ hostname = location.hostname, pathname = location.pathname } = {}) {
  if (/^(?:localhost|127\.0\.0\.1|\[::1\])$/.test(hostname)) return ["./download-index.json"];
  if (hostname === "mvdcm.ecoremedia.net" || pathname.startsWith("/e-mate/")) {
    return ["/e-mate/update/download-index.json"];
  }
  return ["https://mvdcm.ecoremedia.net/e-mate/update/download-index.json"];
}

async function loadIndex() {
  for (const source of indexSources()) {
    try { return await fetchIndex(source); } catch {}
  }
  throw new Error("下载信息暂时不可用");
}

function formatBytes(value) {
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value / 1024 / 1024)} MB`;
}

function preferredPlatform(target) {
  if (target?.startsWith("windows")) return "windows";
  if (target?.startsWith("macos")) return "macos";
  return /mac|iphone|ipad|ipod/i.test(`${navigator.platform || ""} ${navigator.userAgent || ""}`) ? "macos" : "windows";
}

function setPlatform(platform) {
  document.querySelectorAll("[data-platform]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.platform === platform));
  });
  document.querySelectorAll("[data-download-target]").forEach((card) => {
    card.hidden = card.dataset.downloadPlatform !== platform;
  });
}

function setPrimary(index, target) {
  const link = document.querySelector("[data-primary-download]");
  const detail = document.querySelector("[data-primary-detail]");
  if (!link || !detail) return;
  const download = index.downloads.find((item) => item.target === target);
  link.classList.remove("is-disabled");
  link.removeAttribute("aria-disabled");
  if (!download) {
    link.textContent = "选择下载版本";
    link.href = "#download-options";
    link.removeAttribute("download");
    detail.textContent = "请选择与你的电脑匹配的系统和芯片";
    return;
  }
  link.textContent = "免费下载";
  link.href = download.url;
  link.download = download.file_name;
  detail.textContent = `已为你识别 ${download.label} · ${index.version}`;
}

function renderIndex(index, target) {
  const [major, minor] = index.version.split(".");
  const featureNav = document.querySelector("[data-feature-nav]");
  if (featureNav) featureNav.textContent = `${major}.${minor} 新功能`;
  const releaseLabel = document.querySelector("[data-release-label]");
  if (releaseLabel) releaseLabel.textContent = `当前版本 ${index.version} · ${index.released_at.slice(0, 10)}`;
  const grid = document.querySelector("[data-downloads]");
  if (!grid) return;
  grid.replaceChildren();
  for (const download of index.downloads) {
    const card = document.createElement("article");
    card.className = `download-card${download.target === target ? " is-recommended" : ""}`;
    card.dataset.downloadTarget = download.target;
    card.dataset.downloadPlatform = download.platform;
    const title = document.createElement("h3");
    title.textContent = download.label;
    const meta = document.createElement("small");
    meta.textContent = `${index.version} · ${formatBytes(download.size_bytes)}`;
    const body = document.createElement("p");
    body.textContent = download.architecture === "arm64" ? "适用于 Apple 芯片 Mac。" : download.platform === "macos" ? "适用于 Intel 芯片 Mac。" : "适用于 64 位 Windows 电脑。";
    const link = document.createElement("a");
    link.className = "download-link";
    link.href = download.url;
    link.download = download.file_name;
    link.textContent = "下载安装包";
    link.setAttribute("aria-label", `下载 ${download.label}`);
    card.append(title, meta, body, link);
    grid.append(card);
  }
  const platform = preferredPlatform(target);
  setPlatform(platform);
  setPrimary(index, target);
  document.querySelectorAll("[data-platform]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextPlatform = button.dataset.platform;
      setPlatform(nextPlatform);
      const nextTarget = nextPlatform === "windows" ? "windows-x64" : target?.startsWith("macos") ? target : null;
      setPrimary(index, nextTarget);
      document.querySelector("#download-options")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

function renderFailure() {
  const primary = document.querySelector("[data-primary-download]");
  if (primary) { primary.textContent = "暂时无法下载"; primary.classList.add("is-disabled"); primary.setAttribute("aria-disabled", "true"); }
  const detail = document.querySelector("[data-primary-detail]");
  if (detail) detail.textContent = "发布信息尚未准备好，请稍后刷新";
  const grid = document.querySelector("[data-downloads]");
  if (grid) grid.innerHTML = '<article class="download-card"><h3>下载信息暂不可用</h3><p>请稍后刷新页面。</p></article>';
}

if (typeof document !== "undefined") {
  Promise.all([loadIndex(), detectTarget()]).then(([index, target]) => renderIndex(index, target)).catch(renderFailure);
}
