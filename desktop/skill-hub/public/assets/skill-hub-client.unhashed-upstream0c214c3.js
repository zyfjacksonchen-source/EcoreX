(() => {
  "use strict";
  const runtime = window.__ECOREX_RUNTIME__ || {};
  const bearer = runtime.bearerToken || "";
  let csrf = "";
  let cards = [];
  let selected = null;
  let category = "";
  const one = (selector) => document.querySelector(selector);
  const status = one("[data-status]");
  const template = one("[data-skill-card]");
  const grid = one("[data-grid]");
  const headers = (mutation = false) => {
    const value = { Accept: "application/json" };
    if (bearer) value.Authorization = `Bearer ${bearer}`;
    if (mutation && csrf) value["X-EcoreX-CSRF"] = csrf;
    return value;
  };
  const api = async (path, init = {}, mutation = false) => {
    const response = await fetch(path, { ...init, headers: { ...headers(mutation), ...(init.headers || {}) }, credentials: "same-origin", cache: "no-store" });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.detail || `请求失败 (${response.status})`);
    return payload;
  };
  const loadBootstrap = async () => {
    const payload = await api("/api/v1/bootstrap");
    csrf = payload.csrf_token || "";
  };
  const label = (value) => ({ not_installed: "未安装", installed_enabled: "已启用", installed_disabled: "已关闭", uninstalled: "未安装" }[value] || value);
  const render = () => {
    grid.querySelectorAll(".skill-card:not([data-skill-card])").forEach((node) => node.remove());
    for (const card of cards) {
      const node = template.cloneNode(true);
      node.removeAttribute("data-skill-card"); node.hidden = false;
      node.querySelector("[data-title]").textContent = card.title;
      const slug = node.querySelector("[data-copy-slug]"); slug.textContent = card.slug;
      slug.addEventListener("click", () => navigator.clipboard.writeText(card.slug));
      node.querySelector("[data-state]").textContent = label(card.installation_status);
      node.querySelector("[data-summary]").textContent = card.summary;
      const tags = node.querySelector("[data-tags]");
      for (const tag of card.tags.slice(0, 4)) { const item = document.createElement("span"); item.textContent = tag; tags.append(item); }
      node.querySelector("[data-meta]").textContent = `v${card.version} · e-Mate · ${card.uploader.nickname}`;
      node.querySelector("[data-detail]").addEventListener("click", () => showDetail(card));
      const install = node.querySelector("[data-install]");
      install.disabled = card.installation_status === "installed_enabled" || card.readiness === "unsupported";
      if (card.installation_status === "installed_enabled") install.textContent = "已启用";
      install.addEventListener("click", () => installCard(card, install));
      grid.append(node);
    }
    status.textContent = cards.length ? `共 ${cards.length} 个 Skill` : "没有匹配的 Skill";
  };
  const load = async () => {
    status.textContent = "正在读取 e-Mate Skill Hub…";
    const params = new URLSearchParams({ query: one("[data-search]").value.trim(), limit: "100" });
    if (category) params.set("category", category);
    try { cards = (await api(`/api/v1/skill-hub/skills?${params}`)).items || []; render(); }
    catch (error) { status.textContent = error.message; }
  };
  const installCard = async (card, button) => {
    button.disabled = true; button.textContent = "安装中…";
    try {
      await api(`/api/v1/skill-hub/skills/${encodeURIComponent(card.slug)}/install`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: card.version, package_sha256: card.package_sha256, client_request_id: `hub_${crypto.randomUUID()}` }) }, true);
      await load();
    } catch (error) { status.textContent = error.message; button.disabled = false; button.textContent = "重试安装"; }
  };
  const showDetail = async (card) => {
    selected = card;
    one("[data-detail-title]").textContent = card.title;
    one("[data-detail-summary]").textContent = card.summary;
    const list = one("[data-detail-list]"); list.replaceChildren();
    const rows = [["slug", card.slug], ["版本", card.version], ["作者", `${card.uploader.nickname} · ${card.uploader.author_ref}`], ["就绪状态", card.readiness], ["内容摘要", card.package_sha256], ["原始来源", card.provenance.original_url || "e-Mate"]];
    for (const [term, value] of rows) { const row = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = term; dd.textContent = value; row.append(dt, dd); list.append(row); }
    const install = one("[data-detail-install]"); install.disabled = card.installation_status === "installed_enabled" || card.readiness === "unsupported"; install.textContent = card.installation_status === "installed_enabled" ? "已启用" : "安装并启用";
    one("[data-detail-dialog]").showModal();
  };
  const download = async () => {
    if (!selected) return;
    const response = await fetch(`/api/v1/skill-hub/skills/${encodeURIComponent(selected.slug)}/versions/${encodeURIComponent(selected.version)}/package`, { headers: headers(), credentials: "same-origin", cache: "no-store" });
    if (!response.ok || response.headers.get("x-skill-content-sha256") !== selected.package_sha256) { status.textContent = "Skill 包身份校验失败"; return; }
    const url = URL.createObjectURL(await response.blob()); const link = document.createElement("a"); link.href = url; link.download = `${selected.slug}-${selected.version}.zip`; link.click(); URL.revokeObjectURL(url);
  };
  const base64 = (file) => new Promise((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error("无法读取 ZIP")); reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || ""); reader.readAsDataURL(file); });
  one("[data-upload-form]").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const file = form.get("bundle");
    if (!(file instanceof File)) return;
    const submit = event.currentTarget.querySelector("button[type=submit]"); submit.disabled = true; submit.textContent = "验证中…";
    try { await api("/api/v1/skill-hub/skills", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug: form.get("slug"), category: form.get("category"), bundle_base64: await base64(file), client_request_id: `hub_upload_${crypto.randomUUID()}` }) }, true); one("[data-upload-dialog]").close(); event.currentTarget.reset(); await load(); }
    catch (error) { status.textContent = error.message; }
    finally { submit.disabled = false; submit.textContent = "验证并发布"; }
  });
  one("[data-search]").addEventListener("input", (() => { let timer; return () => { clearTimeout(timer); timer = setTimeout(load, 180); }; })());
  document.querySelectorAll("[data-category]").forEach((button) => button.addEventListener("click", () => { category = button.dataset.category; document.querySelectorAll("[data-category]").forEach((item) => item.classList.toggle("active", item === button)); load(); }));
  one("[data-open-upload]").addEventListener("click", () => one("[data-upload-dialog]").showModal());
  one("[data-close-upload]").addEventListener("click", () => one("[data-upload-dialog]").close());
  one("[data-close-detail]").addEventListener("click", () => one("[data-detail-dialog]").close());
  one("[data-detail-download]").addEventListener("click", download);
  one("[data-detail-install]").addEventListener("click", (event) => selected && installCard(selected, event.currentTarget));
  one("[data-theme]").addEventListener("click", () => { const root = document.documentElement; root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark"; localStorage.setItem("emate-theme", root.dataset.theme); });
  document.documentElement.dataset.theme = localStorage.getItem("emate-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  loadBootstrap().then(load).catch((error) => { status.textContent = error.message; });
})();
