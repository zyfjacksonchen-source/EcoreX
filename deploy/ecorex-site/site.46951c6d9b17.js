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

export function sourceList(value, label, fileName) {
  if (
    !Array.isArray(value)
    || value.length < 1
    || value.length > SOURCE_ORDER.length
  ) {
    throw new Error(`${label} 必须包含一至三个有序下载源`);
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
  if (
    candidate.length !== reference.length
    || candidate.some((source, index) => (
    source.sourceId !== reference[index].sourceId ||
    source.kind !== reference[index].kind ||
    source.priority !== reference[index].priority ||
    source.baseUrl !== reference[index].baseUrl
    ))
  ) {
    throw new Error(`${label} 下载源身份与清单不一致`);
  }
}

export function normalizePublicIndex(raw, now = new Date()) {
  const root = object(raw, "公开索引");
  exactKeys(root, ["schema_version", "document_type", "trust", "status", "authority", "freshness", "release"], "公开索引");
  if (root.schema_version !== 1 || root.document_type !== DOCUMENT_TYPE || root.trust !== TRUST) {
    throw new Error("公开索引不是 e-Mate v1 discovery hint");
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
  throw new Error("所有发布源的签名清单均未通过 exact SHA-256 字节核对");
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

export function wrappedCarouselIndex(index, itemCount) {
  if (!Number.isInteger(index) || !Number.isInteger(itemCount) || itemCount < 1) return 0;
  return ((index % itemCount) + itemCount) % itemCount;
}

function setupRobotCarousel() {
  const carousel = document.querySelector("[data-robot-carousel]");
  const viewport = carousel?.querySelector("[data-carousel-viewport]");
  const choices = [...(carousel?.querySelectorAll("[data-robot-index]") || [])];
  const copies = [...(carousel?.querySelectorAll("[data-robot-copy]") || [])];
  const dots = [...(carousel?.querySelectorAll("[data-carousel-dots] button") || [])];
  const previous = carousel?.querySelector("[data-carousel-prev]");
  const next = carousel?.querySelector("[data-carousel-next]");
  if (!viewport || choices.length !== 5 || copies.length !== choices.length || dots.length !== choices.length || !previous || !next) return;

  let activeIndex = 2;
  let drag = null;
  let suppressClick = false;
  const update = (index) => {
    activeIndex = wrappedCarouselIndex(index, choices.length);
    choices.forEach((choice, choiceIndex) => {
      let position = wrappedCarouselIndex(choiceIndex - activeIndex, choices.length);
      if (position > Math.floor(choices.length / 2)) position -= choices.length;
      choice.dataset.position = String(position);
      choice.setAttribute("aria-pressed", String(choiceIndex === activeIndex));
    });
    copies.forEach((copy, copyIndex) => {
      const active = copyIndex === activeIndex;
      copy.hidden = !active;
      copy.toggleAttribute("inert", !active);
    });
    dots.forEach((dot, dotIndex) => {
      if (dotIndex === activeIndex) dot.setAttribute("aria-current", "true");
      else dot.removeAttribute("aria-current");
    });
  };

  previous.addEventListener("click", () => update(activeIndex - 1));
  next.addEventListener("click", () => update(activeIndex + 1));
  dots.forEach((dot, dotIndex) => dot.addEventListener("click", () => update(dotIndex)));
  choices.forEach((choice, choiceIndex) => choice.addEventListener("click", (event) => {
    if (suppressClick) {
      event.preventDefault();
      return;
    }
    update(choiceIndex);
  }));
  viewport.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      update(activeIndex + (event.key === "ArrowLeft" ? -1 : 1));
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      update(event.key === "Home" ? 0 : choices.length - 1);
    }
  });
  viewport.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
    drag = { pointerId: event.pointerId, startX: event.clientX, moved: false };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("is-dragging");
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (Math.abs(event.clientX - drag.startX) > 8) drag.moved = true;
  });
  const finishDrag = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const distance = event.clientX - drag.startX;
    if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
    viewport.classList.remove("is-dragging");
    suppressClick = drag.moved;
    drag = null;
    if (Math.abs(distance) >= 36) update(activeIndex + (distance < 0 ? 1 : -1));
    setTimeout(() => { suppressClick = false; }, 0);
  };
  viewport.addEventListener("pointerup", finishDrag);
  viewport.addEventListener("pointercancel", finishDrag);
  update(activeIndex);
}

function cardCopy(artifact) {
  if (artifact.artifactId === "bootstrap-windows-x64") {
    return ["Win", "Windows x64", "适用于 Windows 10/11 的 64 位电脑。"];
  }
  if (artifact.artifactId === "bootstrap-macos-arm64") {
    return ["Mac", "macOS Apple Silicon", "适用于配备 Apple 芯片的 Mac。"];
  }
  return ["Mac", "macOS Intel", "适用于配备 Intel 芯片的 Mac。"];
}

function powershellLiteral(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function shellLiteral(value) {
  return `'${String(value).replaceAll("'", `'\"'\"'`)}'`;
}

async function copyText(value) {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

export function terminalCommand(artifact) {
  const sourceUrls = artifact.sources.map((source) => source.url);
  if (artifact.platform === "windows") {
    const urls = sourceUrls.map(powershellLiteral).join(",");
    return [
      "$ErrorActionPreference='Stop'",
      "$d=Join-Path $env:TEMP ('e-Mate-'+[guid]::NewGuid())",
      "New-Item -ItemType Directory -Path $d | Out-Null",
      "$z=Join-Path $d 'e-Mate.zip'",
      `$urls=@(${urls})`,
      "$ok=$false",
      "$i=0",
      "Write-Host ''",
      "Write-Host 'e-Mate 安装准备' -ForegroundColor Cyan",
      "foreach($u in $urls){$i++; Remove-Item -LiteralPath $z -Force -ErrorAction SilentlyContinue; Write-Host (('[下载] 下载源 {0}/{1}，将显示百分比、速度和剩余时间' -f $i,$urls.Count)); & curl.exe --fail --location --retry 4 --retry-all-errors --connect-timeout 15 --output $z $u; if($LASTEXITCODE -eq 0 -and (Get-FileHash $z -Algorithm SHA256).Hash.ToLowerInvariant() -eq " + powershellLiteral(artifact.sha256) + "){$ok=$true; break}; Write-Host '[切换] 当前下载源未完成，正在尝试下一来源'}",
      "if(-not $ok){throw 'e-Mate 安装文件下载或校验失败'}",
      "Write-Host '[校验] 下载文件已通过完整性检查'",
      "Write-Host '[解压] 正在准备启动组件'",
      "Expand-Archive -LiteralPath $z -DestinationPath $d",
      "Write-Host '[启动] 后续 e-Mate 组件会继续显示实时进度'",
      "& (Join-Path $d 'bin\\ecorex-bootstrap.exe')",
    ].join("; ");
  }
  const urls = sourceUrls.map(shellLiteral).join(" ");
  const digest = shellLiteral(artifact.sha256);
  return [
    'd="$(mktemp -d)"',
    'z="$d/e-Mate.zip"',
    `urls=(${urls})`,
    "ok=0",
    "i=0",
    "printf '\\ne-Mate 安装准备\\n'",
    "for u in \"${urls[@]}\"; do i=$((i+1)); rm -f \"$z\"; printf '[下载] 下载源 %s/%s，将显示百分比、速度和剩余时间\\n' \"$i\" \"${#urls[@]}\"; if curl --fail --location --retry 4 --retry-all-errors --connect-timeout 15 --output \"$z\" \"$u\" && printf '%s  %s\\n' " + digest + " \"$z\" | shasum -a 256 -c -; then ok=1; break; fi; printf '[切换] 当前下载源未完成，正在尝试下一来源\\n'; done",
    "test \"$ok\" -eq 1",
    "printf '[校验] 下载文件已通过完整性检查\\n'",
    "printf '[解压] 正在准备启动组件\\n'",
    'ditto -x -k "$z" "$d"',
    'chmod +x "$d/bin/ecorex-bootstrap"',
    "printf '[启动] 后续 e-Mate 组件会继续显示实时进度\\n'",
    '"$d/bin/ecorex-bootstrap"',
  ].join(" && ");
}

function appendTerminalCommand(article, artifact) {
  const block = createElement("div", "command-block is-primary");
  block.append(createElement(
    "span",
    "",
    artifact.platform === "windows" ? "复制后在 PowerShell 中粘贴执行" : "复制后在终端中粘贴执行",
  ));
  const command = terminalCommand(artifact);
  block.append(createElement("code", "", command));
  const copy = createElement("button", "", "复制命令");
  copy.type = "button";
  copy.setAttribute("aria-label", `${artifact.platform === "windows" ? "Windows" : "Mac"} 一键安装命令`);
  copy.setAttribute("aria-live", "polite");
  copy.addEventListener("click", async () => {
    const original = copy.textContent;
    copy.disabled = true;
    try {
      await copyText(command);
      copy.textContent = "已复制，可粘贴执行";
    } catch {
      copy.textContent = "复制失败，请手动选择命令";
    }
    window.setTimeout(() => {
      copy.textContent = original;
      copy.disabled = false;
    }, 1600);
  });
  block.append(copy);
  article.append(block);
}

function renderArtifactCard(grid, release, artifact, recommended) {
  const [icon, title, body] = cardCopy(artifact);
  const article = createElement("article", `download-card${recommended ? " is-recommended" : ""}`);
  if (recommended) article.append(createElement("span", "recommend-badge", "当前设备"));
  article.append(createElement("span", "platform-icon", icon));
  article.append(createElement("h3", "", title));
  article.append(createElement("small", "card-version", `v${release.version}`));
  article.append(createElement("p", "", body));
  article.append(createElement(
    "p",
    "progress-promise",
    "命令会自动下载、检查、安装，并显示速度、进度和当前阶段。",
  ));
  appendTerminalCommand(article, artifact);

  grid.append(article);
}

function renderIndex(index, manifestCheck = null) {
  const grid = document.querySelector("[data-downloads]");
  if (!grid) return;
  grid.replaceChildren();
  if (index.status !== "published") {
    const card = createElement("article", "download-card");
    card.append(createElement("span", "platform-icon", "···"));
    card.append(createElement("h3", "", "e-Mate 正在准备中"));
    card.append(createElement("p", "", "正式版本准备好后，Windows 和 Mac 下载会在这里开放。"));
    card.append(createElement("span", "download-link is-disabled", "不可下载"));
    grid.append(card);
    return;
  }

  const release = index.release;
  const manifestCheckNode = document.querySelector("[data-manifest-check]");
  if (manifestCheckNode) {
    manifestCheckNode.textContent = manifestCheck
      ? `字节摘要已核对（${manifestCheck.kind}）`
      : "正在验证下载文件";
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
  card.append(createElement("h3", "", "暂时无法下载"));
  card.append(createElement("p", "", "下载信息暂时没有准备好，请稍后刷新页面。"));
  card.append(createElement("span", "download-link is-disabled", "不可下载"));
  const details = createElement("details", "download-error-details");
  details.append(createElement("summary", "", "下载遇到问题"));
  details.append(createElement(
    "p",
    "download-error-detail",
    error instanceof Error ? error.message : "请稍后刷新。",
  ));
  card.append(details);
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
  setupRobotCarousel();
  loadIndex()
    .then((index) => {
      // Discovery must remain usable when a release origin does not grant
      // browser CORS. The downloaded Bootstrap is the signature authority.
      renderIndex(index, null);
      if (index.status === "published") {
        verifyManifestBytes(index.release.manifest)
          .then((manifestCheck) => {
            const node = document.querySelector("[data-manifest-check]");
            if (node) node.textContent = `字节摘要已核对（${manifestCheck.kind}）`;
          })
          .catch(() => {
            const node = document.querySelector("[data-manifest-check]");
            if (node) node.textContent = "安装程序会自动验证";
          });
      }
    })
    .catch(renderFailure);
}
