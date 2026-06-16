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
  },
  "webui-windows-x64": {
    icon: "Web",
    title: "Windows 网页版",
    body: "网页版，在本机一键安装并在网页内直接部署启动，完成后自动打开 EcoreX。",
  },
  "webui-macos-universal": {
    icon: "Web",
    title: "macOS 网页版",
    body: "网页版，在本机一键安装并在网页内直接部署启动，完成后自动打开 EcoreX。",
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

function ready(artifact) {
  return artifact?.status === "ready" || artifact?.status === "ready-unsigned";
}

function artifactMeta(artifact) {
  if (!artifact) return "";
  return `
    <div class="meta">
      <span>${artifact.variant || artifact.platform}</span>
      <span>${formatSize(artifact.size)}</span>
      <span title="${artifact.sha256 || ""}">SHA256: ${shortSha(artifact.sha256)}</span>
    </div>
  `;
}

function buttonForArtifact(artifact, label = "下载") {
  if (!artifact) {
    return `<span class="download-link is-disabled">待发布</span>`;
  }
  if (ready(artifact)) {
    const suffix = artifact.status === "ready-unsigned" ? "（未公证）" : "";
    return `<a class="download-link" href="./${artifact.href}" download title="${artifact.source || ""}">${label}${suffix}</a>`;
  }
  const pendingText = artifact.status === "pending-signature" ? "待签名" : "待验证";
  return `<span class="download-link is-disabled" title="${artifact.source || ""}">${pendingText}</span>`;
}

function architectureSelector(cardId, artifacts) {
  const options = artifacts.filter(Boolean);
  if (options.length <= 1) {
    return `${artifactMeta(options[0])}${buttonForArtifact(options[0])}`;
  }

  const initial = options.find(ready) || options[0];
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
  cardOrder.forEach((cardId) => {
    const copy = cardCopy[cardId];
    const card = document.createElement("article");
    card.className = "download-card";
    card.innerHTML = `
      <span class="platform-icon">${copy.icon}</span>
      <h3>${copy.title}</h3>
      <p>${copy.body}</p>
      ${architectureSelector(cardId, grouped[cardId] || [])}
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
