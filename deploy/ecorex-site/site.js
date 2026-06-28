const root = document.documentElement;
const themeButton = document.querySelector("[data-theme-toggle]");
const themeIcon = document.querySelector("[data-theme-icon]");

const INSTALL_COMMANDS = {
  win32:
    'powershell -ExecutionPolicy Bypass -NoProfile -Command "iwr https://mvdcm.ecoremedia.net/ecorex-agent/install-webui.ps1 -UseB | iex"',
  darwin:
    "curl -fsSL https://mvdcm.ecoremedia.net/ecorex-agent/install-webui.sh | bash",
};

function setTheme(theme) {
  root.dataset.theme = theme;
  localStorage.setItem("ecorex-site-theme", theme);
  if (themeIcon) themeIcon.textContent = theme === "dark" ? "☀" : "◐";
}

const preferredTheme =
  localStorage.getItem("ecorex-site-theme") ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
setTheme(preferredTheme);

themeButton?.addEventListener("click", () => {
  setTheme(root.dataset.theme === "dark" ? "light" : "dark");
});

function detectVisitorDevice() {
  const platform = String(
    (navigator.userAgentData && navigator.userAgentData.platform) ||
      navigator.platform ||
      navigator.userAgent ||
      ""
  ).toLowerCase();
  const ua = String(navigator.userAgent || "").toLowerCase();
  const source = `${platform} ${ua}`;
  return /mac|iphone|ipad|ipod/.test(source)
    ? "darwin"
    : /win/.test(source)
      ? "win32"
      : "web";
}

function formatSize(size) {
  if (typeof size !== "number" || !Number.isFinite(size) || size <= 0) return "待发布";
  const mib = size / 1024 / 1024;
  return `${mib.toFixed(mib >= 100 ? 1 : 2)} MiB`;
}

function shortSha(sha256) {
  return sha256 && sha256 !== "pending" ? `${sha256.slice(0, 12)}...` : "待生成";
}

function joinHref(href) {
  const value = String(href || "");
  return /^https?:\/\//i.test(value) ? value : `./${value.replace(/^\.\//, "")}`;
}

function ready(artifact) {
  return Boolean(artifact && artifact.status === "ready" && artifact.size && artifact.sha256);
}

function artifactById(manifest, id) {
  return (manifest.artifacts || []).find((artifact) => artifact.id === id);
}

function commandForCard(cardId) {
  return cardId === "webui-macos-universal" ? INSTALL_COMMANDS.darwin : INSTALL_COMMANDS.win32;
}

function cardCopy(cardId) {
  if (cardId === "webui-macos-universal") {
    return {
      icon: "Mac",
      title: "macOS WebUI",
      body: "一键安装本地 WebUI 服务，桌面会生成网页入口。后续更新走 manifest 校验包，不需要手动下载桌面端。",
      commandLabel: "macOS 一键安装/更新",
    };
  }
  return {
    icon: "Win",
    title: "Windows WebUI",
    body: "一键安装本地 WebUI 服务，桌面会生成网页入口。后续更新走 manifest 校验包，不需要手动下载桌面端。",
    commandLabel: "Windows 一键安装/更新",
  };
}

async function copyText(text) {
  if (!navigator.clipboard?.writeText) {
    throw new Error("Clipboard API is unavailable");
  }
  await navigator.clipboard.writeText(text);
}

function bindCommandCopyButton(button, command) {
  button.addEventListener("click", async () => {
    const ok = await copyText(command).then(() => true).catch(() => false);
    button.textContent = ok ? "已复制" : "复制失败";
    setTimeout(() => {
      button.textContent = "复制命令";
    }, 1400);
  });
}

function setupInstallGuideCopyButtons() {
  document.querySelectorAll("[data-copy-install-command]").forEach((button) => {
    const commandKey = button.getAttribute("data-copy-install-command");
    const command = INSTALL_COMMANDS[commandKey];
    if (!command) return;
    bindCommandCopyButton(button, command);
  });
}

function renderCard(grid, manifest, cardId, recommended) {
  const artifact = artifactById(manifest, cardId);
  const copy = cardCopy(cardId);
  const command = commandForCard(cardId);
  const isReady = ready(artifact);
  const article = document.createElement("article");
  article.className = `download-card${recommended ? " is-recommended" : ""}`;
  article.innerHTML = `
    ${recommended ? '<span class="recommend-badge">推荐</span>' : ""}
    <span class="platform-icon">${copy.icon}</span>
    <h3>${copy.title}</h3>
    <small class="card-version">v${manifest.version || ""}</small>
    <p>${copy.body}</p>
    <div class="meta">
      <span>${artifact?.variant || copy.title}</span>
      <span>${formatSize(artifact?.size)}</span>
      <span title="${artifact?.sha256 || ""}">SHA256: ${shortSha(artifact?.sha256)}</span>
    </div>
    <div class="command-block">
      <span>${copy.commandLabel}</span>
      <code>${command}</code>
      <button type="button" data-copy-command>复制命令</button>
    </div>
    ${
      isReady
        ? `<a class="download-link" href="${joinHref(artifact.href)}" download title="${artifact.source || ""}">下载安装包</a>`
        : '<span class="download-link is-disabled">待发布</span>'
    }
  `;
  const button = article.querySelector("[data-copy-command]");
  if (button) bindCommandCopyButton(button, command);
  grid.appendChild(article);
}

function renderDownloads(manifest) {
  document.querySelector("[data-version]").textContent = manifest.version || "";
  const brandVersion = document.querySelector("[data-site-version]");
  if (brandVersion) brandVersion.textContent = manifest.version ? `v${manifest.version}` : "";
  document.querySelector("[data-updated]").textContent = manifest.updatedAt || "";

  const grid = document.querySelector("[data-downloads]");
  grid.innerHTML = "";
  const device = detectVisitorDevice();
  const order =
    device === "darwin"
      ? ["webui-macos-universal", "webui-windows-x64"]
      : ["webui-windows-x64", "webui-macos-universal"];
  order.forEach((cardId, index) => renderCard(grid, manifest, cardId, index === 0));
}

setupInstallGuideCopyButtons();

fetch("./manifest.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`manifest HTTP ${response.status}`);
    return response.json();
  })
  .then(renderDownloads)
  .catch((error) => {
    const grid = document.querySelector("[data-downloads]");
    grid.innerHTML = `
      <article class="download-card">
        <span class="platform-icon">!</span>
        <h3>版本清单读取失败</h3>
        <p>${error instanceof Error ? error.message : "请稍后刷新页面。"}</p>
      </article>
    `;
  });
