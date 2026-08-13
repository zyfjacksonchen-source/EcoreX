const MAX_INDEX_BYTES = 64 * 1024;
const VERSION = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/;
const SHA256 = /^[0-9a-f]{64}$/;
const CERTIFICATE_THUMBPRINT = /^[0-9A-F]{40}$/;
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
  const manual = index.schema_version === 2;
  exactKeys(index, manual
    ? ["schema_version", "product", "version", "distribution_mode", "released_at", "downloads"]
    : ["schema_version", "product", "version", "released_at", "downloads"], "下载索引");
  if (![1, 2].includes(index.schema_version) || index.product !== "e-Mate") throw new Error("下载索引身份无效");
  if (manual && index.distribution_mode !== "unsigned-manual") throw new Error("发布模式无效");
  safeText(index.version, "版本", VERSION);
  if (typeof index.released_at !== "string" || Number.isNaN(Date.parse(index.released_at))) {
    throw new Error("发布时间无效");
  }
  if (!Array.isArray(index.downloads) || index.downloads.length !== 3) throw new Error("下载目标不完整");
  const seen = new Set();
  const downloads = index.downloads.map((rawDownload) => {
    const download = object(rawDownload, "下载目标");
    const authenticode = manual && download.target === "windows-x64" && "authenticode" in download;
    exactKeys(download, ["target", "platform", "architecture", "file_name", "url", "size_bytes", "sha256", ...(authenticode ? ["authenticode"] : [])], "下载目标");
    const target = TARGETS[download.target];
    if (!target || seen.has(download.target)) throw new Error("下载目标重复或未知");
    seen.add(download.target);
    if (download.platform !== target.platform || download.architecture !== target.architecture) {
      throw new Error("下载目标平台不一致");
    }
    safeText(download.file_name, "下载文件名", SAFE_NAME);
    safeText(download.sha256, "下载摘要", SHA256);
    if (!Number.isSafeInteger(download.size_bytes) || download.size_bytes < 1) throw new Error("下载大小无效");
    if (authenticode) {
      const evidence = object(download.authenticode, "Windows 签名");
      exactKeys(evidence, ["status", "signer_certificate_thumbprint"], "Windows 签名");
      if (evidence.status !== "verified") throw new Error("Windows 签名状态无效");
      safeText(evidence.signer_certificate_thumbprint, "Windows 签名证书", CERTIFICATE_THUMBPRINT);
    }
    const expectedUrl = `https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/desktop/v${index.version}/${download.file_name}`;
    if (download.url !== expectedUrl) throw new Error("下载地址无效");
    return Object.freeze({ ...download, label: target.label });
  });
  if (seen.size !== Object.keys(TARGETS).length) throw new Error("下载目标不完整");
  return Object.freeze({ ...index, distribution_mode: manual ? "unsigned-manual" : "signed-automatic", downloads: Object.freeze(downloads) });
}

export function installationTrustCopy(index) {
  if (index.distribution_mode !== "unsigned-manual") return null;
  const windowsSigned = index.downloads.some((item) => item.target === "windows-x64" && item.authenticode?.status === "verified");
  return windowsSigned
    ? Object.freeze({ release: "Windows 已签名 · macOS 手动安装（未签名）", help: "Windows 安装包已验证数字签名；macOS 暂未签名，请按系统提示允许打开。" })
    : Object.freeze({ release: "手动安装（未签名）", help: "当前候选暂未签名，请按系统提示允许打开。" });
}

export function downloadSources(index, target) {
  const download = index.downloads.find((item) => item.target === target);
  return Object.freeze(download ? [download.url] : []);
}

export function targetFromPlatformSignals({ source = "", architecture = "", renderer = "" }) {
  source = String(source).toLowerCase();
  architecture = String(architecture).toLowerCase();
  renderer = String(renderer).toLowerCase();
  if (/iphone|ipad|ipod/.test(source)) return null;
  if (/mac/.test(source)) {
    if (/(?:^|\W)(?:arm|arm64|aarch64)(?:\W|$)|apple silicon/.test(`${source} ${architecture}`)
      || /apple (?:m[1-9]|a[1-9][0-9])|apple gpu/.test(renderer)) return "macos-arm64";
    if (/x86_64|x86-64|amd64/.test(`${source} ${architecture}`)) return "macos-x64";
    return null;
  }
  return /win/.test(source) ? "windows-x64" : null;
}

export function isMacDesktop({ source = "" } = {}) {
  source = String(source).toLowerCase();
  return /mac/.test(source) && !/iphone|ipad|ipod/.test(source);
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
  return ["/e-mate/update/download-index.json"];
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
  const source = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  if (/iphone|ipad|ipod/i.test(source)) return null;
  if (/mac/i.test(source)) return "macos";
  return /win/i.test(source) ? "windows" : null;
}

function setPlatform(platform) {
  const known = platform === "macos" || platform === "windows";
  document.querySelectorAll("[data-platform]").forEach((button) => {
    button.setAttribute("aria-pressed", String(known && button.dataset.platform === platform));
  });
  document.querySelectorAll("[data-download-target]").forEach((card) => {
    card.hidden = known && card.dataset.downloadPlatform !== platform;
  });
}

function setPrimary(index, target) {
  const link = document.querySelector("[data-primary-download]");
  const label = link?.querySelector("[data-primary-label]");
  const detail = document.querySelector("[data-primary-detail]");
  if (!link || !label || !detail) return;
  const download = index.downloads.find((item) => item.target === target);
  link.classList.remove("is-disabled");
  link.removeAttribute("aria-disabled");
  if (!download) {
    label.textContent = "选择下载版本";
    link.href = "#download-options";
    link.removeAttribute("download");
    detail.textContent = "请选择与你的电脑匹配的系统和芯片";
    return;
  }
  const [preferred] = downloadSources(index, target);
  label.textContent = "立即下载";
  link.href = preferred;
  link.download = download.file_name;
  detail.textContent = `已为你识别 ${download.label} · ${index.version}`;
}

function renderIndex(index, target) {
  const [major, minor] = index.version.split(".");
  const featureNav = document.querySelector("[data-feature-nav]");
  if (featureNav) featureNav.textContent = `${major}.${minor} 新功能`;
  const trustCopy = installationTrustCopy(index);
  const releaseLabel = document.querySelector("[data-release-label]");
  if (releaseLabel) releaseLabel.textContent = `当前版本 ${index.version} · ${index.released_at.slice(0, 10)}${trustCopy ? ` · ${trustCopy.release}` : ""}`;
  const firstLaunchHelp = document.querySelector("[data-first-launch-help]");
  if (firstLaunchHelp && trustCopy) firstLaunchHelp.textContent = trustCopy.help;
  const grid = document.querySelector("[data-downloads]");
  if (!grid) return;
  grid.replaceChildren();
  for (const download of index.downloads) {
    const sources = downloadSources(index, download.target);
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
    link.href = sources[0];
    link.download = download.file_name;
    link.textContent = "下载安装包";
    link.setAttribute("aria-label", `下载 ${download.label}`);
    card.append(title, meta, body);
    if (download.platform === "macos") {
      const digest = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "核对 SHA-256";
      const value = document.createElement("code");
      value.textContent = download.sha256;
      digest.append(summary, value);
      card.append(digest);
    }
    card.append(link);
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
  const label = primary?.querySelector("[data-primary-label]");
  if (primary && label) { label.textContent = "暂时无法下载"; primary.classList.add("is-disabled"); primary.setAttribute("aria-disabled", "true"); }
  const detail = document.querySelector("[data-primary-detail]");
  if (detail) detail.textContent = "发布信息尚未准备好，请稍后刷新";
  const grid = document.querySelector("[data-downloads]");
  if (grid) grid.innerHTML = '<article class="download-card"><h3>下载信息暂不可用</h3><p>请稍后刷新页面。</p></article>';
}

if (typeof document !== "undefined") {
  const source = `${navigator.userAgentData?.platform || ""} ${navigator.platform || ""} ${navigator.userAgent || ""}`;
  document.querySelectorAll("[data-mac-install-guide]").forEach((link) => { link.hidden = !isMacDesktop({ source }); });
  const copy = document.querySelector("[data-copy-macos-command]");
  copy?.addEventListener("click", async () => {
    const status = document.querySelector("[data-copy-status]");
    const command = [...document.querySelectorAll("[data-macos-command-line]")].map((line) => line.textContent).join("\n");
    try {
      await navigator.clipboard.writeText(command);
      if (status) status.textContent = "已复制，请粘贴到终端运行。";
    } catch {
      if (status) status.textContent = "复制失败，请手动选择上方两行命令。";
    }
  });
  if (document.querySelector("[data-downloads]")) {
    Promise.all([loadIndex(), detectTarget()]).then(([index, target]) => renderIndex(index, target)).catch(renderFailure);
  }
}
