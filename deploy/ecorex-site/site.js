const root = document.documentElement;
const themeButton = document.querySelector("[data-theme-toggle]");
const themeIcon = document.querySelector("[data-theme-icon]");

function setTheme(theme) {
  root.dataset.theme = theme;
  localStorage.setItem("ecorex-site-theme", theme);
  if (themeIcon) themeIcon.textContent = theme === "dark" ? "☀" : "☾";
}

const preferredTheme =
  localStorage.getItem("ecorex-site-theme") ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
setTheme(preferredTheme);

themeButton?.addEventListener("click", () => {
  setTheme(root.dataset.theme === "dark" ? "light" : "dark");
});

function svgFallback(kind) {
  const isIcon = kind === "icon";
  const width = isIcon ? 256 : 1600;
  const height = isIcon ? 256 : kind === "hub" ? 900 : 980;
  const title = isIcon ? "EX" : kind === "hub" ? "EcoreX 能力中心" : "EcoreX";
  const subtitle = kind === "hub"
    ? "会话 / 文件 / Skill / MCP / 管理后台"
    : "桌面端和网页版统一体验";
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
      <defs>
        <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0" stop-color="#15110e"/>
          <stop offset="1" stop-color="#3a2116"/>
        </linearGradient>
      </defs>
      <rect width="100%" height="100%" fill="url(#bg)"/>
      <rect x="${isIcon ? 20 : 96}" y="${isIcon ? 20 : 86}" width="${isIcon ? 216 : width - 192}" height="${isIcon ? 216 : height - 172}" rx="${isIcon ? 44 : 34}" fill="#211a15" stroke="#5f3722" stroke-width="${isIcon ? 8 : 3}"/>
      <text x="50%" y="${isIcon ? 150 : height * 0.45}" text-anchor="middle" font-family="Arial, sans-serif" font-size="${isIcon ? 120 : 96}" font-weight="800" fill="#ff7a2f">${isIcon ? "X" : title}</text>
      ${isIcon ? "" : `<text x="50%" y="${height * 0.57}" text-anchor="middle" font-family="Arial, sans-serif" font-size="34" fill="#e9d9ca">${subtitle}</text>`}
    </svg>
  `;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

document.querySelectorAll("img[data-fallback]").forEach((image) => {
  image.addEventListener("error", () => {
    if (image.dataset.fallbackApplied === "true") return;
    image.dataset.fallbackApplied = "true";
    image.src = svgFallback(image.dataset.fallback || "preview");
  });
});

const cardOrder = ["windows-x64", "macos-dmg", "webui-windows-x64", "webui-macos-universal"];

function detectVisitorDevice() {
  const platform = String(
    (navigator.userAgentData && navigator.userAgentData.platform) ||
    navigator.platform ||
    navigator.userAgent ||
    ""
  ).toLowerCase();
  const ua = String(navigator.userAgent || "").toLowerCase();
  const source = `${platform} ${ua}`;
  const isMac = /mac|iphone|ipad|ipod/.test(source);
  const isWindows = /win/.test(source);
  let arch = "";
  if (/arm64|aarch64|apple silicon/.test(source)) arch = "arm64";
  if (/x86_64|x64|wow64|win64|amd64|intel/.test(source)) arch = "x64";
  return {
    platform: isMac ? "darwin" : isWindows ? "win32" : "web",
    arch
  };
}

function recommendedForDevice(manifest, device) {
  const map = manifest.recommendedDownloads || {};
  if (device.platform === "win32") return map.win32 || {};
  if (device.platform === "darwin") return map.darwin || {};
  if (/mac/i.test(navigator.userAgent || "")) {
    return { webui: map.web?.macos };
  }
  if (/win/i.test(navigator.userAgent || "")) {
    return { webui: map.web?.windows };
  }
  return map.web || {};
}

function orderForRecommendation(recommended) {
  const priority = [];
  if (recommended.primary) priority.push(cardIdForArtifactId(recommended.primary));
  if (recommended.webui) priority.push(cardIdForArtifactId(recommended.webui));
  return [
    ...priority.filter((id, index) => id && priority.indexOf(id) === index),
    ...cardOrder.filter((id) => !priority.includes(id))
  ];
}

function cardIdForArtifactId(id) {
  if (id === "macos-arm64-dmg" || id === "macos-x64-dmg") return "macos-dmg";
  return id || "";
}

function preferredArtifactId(cardId, recommended, device) {
  if (cardId === "macos-dmg") {
    if (device.arch === "x64") return recommended.intel || "macos-x64-dmg";
    return recommended.primary || "macos-arm64-dmg";
  }
  if (recommended.primary === cardId) return cardId;
  if (recommended.webui === cardId) return cardId;
  return "";
}

function isRecommendedCard(cardId, recommended) {
  return cardId === cardIdForArtifactId(recommended.primary) || cardId === cardIdForArtifactId(recommended.webui);
}

const cardCopy = {
  "windows-x64": {
    icon: "Win",
    title: "Windows",
    body: "桌面端正式安装版，适用于 Windows 10/11。安装完成后从开始菜单启动 EcoreX。",
  },
  "macos-dmg": {
    icon: "Mac",
    title: "macOS",
    body: "桌面端 DMG 安装版。点击下载后选择 Apple Silicon 或 Intel 版本。",
    note: "如果 macOS 提示无法验证开发者，请打开“系统设置 → 隐私与安全性”，在 EcoreX 提示下点“仍要打开”。",
  },
  "webui-windows-x64": {
    icon: "Web",
    title: "Windows 网页版",
    body: "网页版，在本机一键安装并在网页内直接部署启动，完成后自动打开 EcoreX。",
  },
  "webui-macos-universal": {
    icon: "Web",
    title: "macOS 网页版",
    body: "下载 ZIP 后解压，双击 Install EcoreX WebUI.app。安装器会在后台启动本地 WebUI，完成后自动打开 EcoreX。",
  },
};

function formatSize(size) {
  if (typeof size === "number" && Number.isFinite(size) && size > 0) {
    const mib = size / 1024 / 1024;
    return `${mib.toFixed(mib >= 100 ? 1 : 2)} MiB`;
  }
  return "待发布";
}

function shortSha(sha256) {
  if (!sha256 || sha256 === "pending") return "待生成";
  return `${sha256.slice(0, 12)}...`;
}

function installSmokeReady(artifact) {
  const smoke = artifact?.installSmoke || artifact?.install_smoke;
  return Boolean(smoke
    && smoke.status === "pass"
    && smoke.version === manifest.version
    && String(smoke.sha256 || "").toUpperCase() === String(artifact.sha256 || "").toUpperCase()
    && (smoke.runId || smoke.run_id || smoke.evidenceUrl || smoke.evidence_url || smoke.evidence));
}

function ready(artifact) {
  if (artifact?.id === "windows-x64" && artifact.signature !== "Valid") return false;
  if (String(artifact?.id || "").startsWith("macos-") && artifact.signature === "unsigned" && artifact.status === "ready") return false;
  if (artifact?.status === "ready") return true;
  return artifact?.status === "ready-unsigned"
    && String(artifact.id || "").startsWith("macos-")
    && artifact.signature === "unsigned"
    && installSmokeReady(artifact);
}

function isExternalHref(href) {
  return /^https?:\/\//i.test(String(href || ""));
}

function artifactHref(artifact) {
  const href = artifact?.href || "";
  return isExternalHref(href) ? href : `./${href}`;
}

function artifactMeta(artifact) {
  if (!artifact) return "";
  const signature = artifact.signature === "unsigned" ? "<span>unsigned</span>" : "";
  const smoke = artifact.installSmoke || artifact.install_smoke;
  const smokeBadge = smoke?.status === "pass" ? "<span>install smoke passed</span>" : "";
  return `
    <div class="meta">
      <span>${artifact.variant || artifact.platform}</span>
      <span>${formatSize(artifact.size)}</span>
      <span title="${artifact.sha256 || ""}">SHA256: ${shortSha(artifact.sha256)}</span>
      ${signature}
      ${smokeBadge}
    </div>
  `;
}

function buttonForArtifact(artifact, label = "下载") {
  if (!artifact) {
    return `<span class="download-link is-disabled">待发布</span>`;
  }
  if (ready(artifact)) {
    const downloadAttr = isExternalHref(artifact.href) ? "" : " download";
    return `<a class="download-link" href="${artifactHref(artifact)}"${downloadAttr} title="${artifact.source || ""}">${label}</a>`;
  }
  const pendingText = artifact.status === "pending-signature" ? "待签名" : "待验证";
  return `<span class="download-link is-disabled" title="${artifact.source || ""}">${pendingText}</span>`;
}

function architectureSelector(cardId, artifacts, preferredId = "") {
  const options = artifacts.filter(Boolean);
  if (options.length <= 1) {
    return `${artifactMeta(options[0])}${buttonForArtifact(options[0])}`;
  }

  const initial = options.find((item) => item.id === preferredId) || options.find(ready) || options[0];
  const selectId = `${cardId}-select`;
  const encoded = encodeURIComponent(JSON.stringify(options));
  return `
    <label class="arch-select">
      <span>选择版本</span>
      <select id="${selectId}" data-arch-options="${encoded}">
        ${options.map((item) => `
          <option value="${item.id}" ${item.id === initial.id ? "selected" : ""}>
            ${item.variant || item.platform}
          </option>
        `).join("")}
      </select>
    </label>
    <div data-arch-meta>${artifactMeta(initial)}</div>
    <div data-arch-action>${buttonForArtifact(initial)}</div>
  `;
}

function cardNote(copy) {
  return copy.note ? `<p class="download-note">${copy.note}</p>` : "";
}

function collectCards(artifacts) {
  const byId = Object.fromEntries(artifacts.map((artifact) => [artifact.id, artifact]));
  return {
    "windows-x64": [byId["windows-x64"]],
    "macos-dmg": [byId["macos-arm64-dmg"], byId["macos-x64-dmg"]],
    "webui-windows-x64": [byId["webui-windows-x64"]],
    "webui-macos-universal": [byId["webui-macos-universal"]],
  };
}

function wireSelectors(grid) {
  grid.querySelectorAll("select[data-arch-options]").forEach((select) => {
    const options = JSON.parse(decodeURIComponent(select.dataset.archOptions || "[]"));
    select.addEventListener("change", () => {
      const card = select.closest(".download-card");
      const selected = options.find((item) => item.id === select.value) || options[0];
      card.querySelector("[data-arch-meta]").innerHTML = artifactMeta(selected);
      card.querySelector("[data-arch-action]").innerHTML = buttonForArtifact(selected);
    });
  });
}

function renderDownloads(manifest) {
  document.querySelector("[data-version]").textContent = manifest.version;
  document.querySelector("[data-updated]").textContent = manifest.updatedAt;

  const grid = document.querySelector("[data-downloads]");
  grid.innerHTML = "";

  const grouped = collectCards(manifest.artifacts || []);
  const device = detectVisitorDevice();
  const recommended = recommendedForDevice(manifest, device);
  orderForRecommendation(recommended).forEach((cardId) => {
    const copy = cardCopy[cardId];
    if (!copy) return;
    const card = document.createElement("article");
    const recommendedCard = isRecommendedCard(cardId, recommended);
    card.className = `download-card${recommendedCard ? " is-recommended" : ""}`;
    card.innerHTML = `
      ${recommendedCard ? `<span class="recommend-badge">推荐</span>` : ""}
      <span class="platform-icon">${copy.icon}</span>
      <h3>${copy.title}</h3>
      <small class="card-version">v${manifest.version}</small>
      <p>${copy.body}</p>
      ${cardNote(copy)}
      ${architectureSelector(cardId, grouped[cardId] || [], preferredArtifactId(cardId, recommended, device))}
    `;
    grid.appendChild(card);
  });
  wireSelectors(grid);
}

fetch("./manifest.json", { cache: "no-store" })
  .then((response) => response.json())
  .then(renderDownloads)
  .catch(() => {
    const grid = document.querySelector("[data-downloads]");
    grid.innerHTML = `
      <article class="download-card">
        <span class="platform-icon">!</span>
        <h3>版本清单读取失败</h3>
        <p>请稍后刷新页面，或联系管理员确认发布目录。</p>
      </article>
    `;
  });
