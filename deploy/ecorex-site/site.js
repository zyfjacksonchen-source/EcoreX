const root = document.documentElement;
const themeButton = document.querySelector("[data-theme-toggle]");
const themeIcon = document.querySelector("[data-theme-icon]");

function setTheme(theme) {
  root.dataset.theme = theme;
  localStorage.setItem("ecorex-site-theme", theme);
  if (themeIcon) themeIcon.textContent = theme === "dark" ? "☾" : "☀";
}

const preferredTheme =
  localStorage.getItem("ecorex-site-theme") ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
setTheme(preferredTheme);

themeButton?.addEventListener("click", () => {
  setTheme(root.dataset.theme === "dark" ? "light" : "dark");
});

const platformCopy = {
  "windows-x64": {
    icon: "Win",
    body: "适用于 Windows 10/11，安装完成后从开始菜单启动 EcoreX。",
  },
  "macos-arm64-dmg": {
    icon: "M",
    body: "适用于 Apple Silicon Mac，下载 DMG 后拖入 Applications。",
  },
  "macos-x64-dmg": {
    icon: "Mac",
    body: "适用于 Intel Mac，下载 DMG 后拖入 Applications。",
  },
};

function formatSize(size) {
  if (typeof size === "number" && Number.isFinite(size)) {
    const mib = size / 1024 / 1024;
    return `${mib.toFixed(mib >= 100 ? 1 : 2)} MiB`;
  }
  return size || "待发布";
}

function shortSha(sha256) {
  if (!sha256 || sha256 === "pending") return "待生成";
  return `${sha256.slice(0, 12)}...`;
}

function artifactAction(artifact) {
  if (artifact.status === "ready") {
    return `<a class="download-link" href="./${artifact.href}" download>下载</a>`;
  }
  if (artifact.status === "ready-unsigned") {
    return `<a class="download-link" href="./${artifact.href}" download title="${artifact.source || "未签名或未公证产物"}">下载（未公证）</a>`;
  }
  if (artifact.status === "pending-signature") {
    return `<span class="download-link is-disabled" title="${artifact.source || "Windows 签名待完成"}">待签名</span>`;
  }
  return `<span class="download-link is-disabled" title="${artifact.source || "该平台产物仍在准备中"}">待验证</span>`;
}

function renderDownloads(manifest) {
  document.querySelector("[data-version]").textContent = manifest.version;
  document.querySelector("[data-updated]").textContent = manifest.updatedAt;

  const grid = document.querySelector("[data-downloads]");
  grid.innerHTML = "";

  manifest.artifacts.forEach((artifact) => {
    const copy = platformCopy[artifact.id] || { icon: "EX", body: "选择适合你的系统版本。" };
    const card = document.createElement("article");
    card.className = "download-card";
    card.innerHTML = `
      <span class="platform-icon">${copy.icon}</span>
      <h3>${artifact.platform}</h3>
      <p>${copy.body}</p>
      <div class="meta">
        <span>${artifact.variant}</span>
        <span>${formatSize(artifact.size)}</span>
        <span title="${artifact.sha256}">SHA256: ${shortSha(artifact.sha256)}</span>
      </div>
      ${artifactAction(artifact)}
    `;
    grid.appendChild(card);
  });
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
