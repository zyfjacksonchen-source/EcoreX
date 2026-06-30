const root = document.documentElement;
const apiBase = "./api";
const state = {
  data: {
    users: [],
    usageByUser: [],
    logs: [],
    logUsers: [],
    capabilities: [],
    capabilityPolicy: { mirror: "https://pypi.org/simple", mode: "ask", offlineCache: "未配置" },
    globalModel: null,
    modelCredentials: [],
    summary: {},
    runtimeAudit: {
      summary: {},
      eventTypeCounts: {},
      sourceCounts: {},
      statusCounts: {},
      requests: [],
      recentEvents: [],
      privacy: {},
    },
    version: "0.2.5",
  },
  connected: false,
};

const storedTheme = localStorage.getItem("ecorex-admin-theme");
root.dataset.theme = storedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

function $(selector, base = document) {
  return base.querySelector(selector);
}

function $all(selector, base = document) {
  return Array.from(base.querySelectorAll(selector));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
}

function trimUnitDecimal(value) {
  return value.replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1");
}

function formatToken(value) {
  const number = Number(value) || 0;
  const abs = Math.abs(number);
  if (abs >= 1_000_000) return `${trimUnitDecimal((number / 1_000_000).toFixed(abs >= 10_000_000 ? 1 : 2))}m`;
  if (abs >= 1_000) return `${trimUnitDecimal((number / 1_000).toFixed(abs >= 10_000 ? 1 : 2))}k`;
  return formatNumber(number);
}

function tokenTitle(value) {
  return `${formatNumber(value)} Token`;
}

function formatTokenLimit(value) {
  return value ? formatToken(value) : "不限";
}

function formatTime(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function statusLabel(status) {
  return { active: "可用", invited: "待首次登录", disabled: "禁用" }[status] || status || "未知";
}

function roleLabel(role) {
  return role === "admin" ? "管理员" : "成员";
}

function modeLabel(mode) {
  return { ask: "首次使用询问安装", preinstall: "管理员预置", disabled: "禁用普通用户安装" }[mode] || "首次使用询问安装";
}

function setApiStatus(connected, message = "") {
  const node = $("[data-api-status]");
  if (!node) return;
  node.dataset.connected = connected ? "true" : "false";
  node.textContent = connected ? "已连接" : "离线";
  node.title = message || (connected ? "正在使用服务器管理数据" : "管理 API 不可用");
}

function showNotice(message, tone = "warn") {
  const node = $("[data-admin-notice]");
  if (!node) return;
  node.hidden = false;
  node.dataset.tone = tone;
  node.textContent = message;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => {
    node.hidden = true;
  }, tone === "error" ? 8000 : 4200);
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function mergeState(payload) {
  state.data = {
    ...state.data,
    ...payload,
    capabilityPolicy: { ...state.data.capabilityPolicy, ...(payload.capabilityPolicy || {}) },
    summary: { ...state.data.summary, ...(payload.summary || {}) },
    runtimeAudit: { ...state.data.runtimeAudit, ...(payload.runtimeAudit || {}) },
  };
}

function setMetric(name, value) {
  const node = $(`[data-metric="${name}"]`);
  if (node) node.textContent = value;
}

function renderUsers() {
  const target = $("[data-users]");
  if (!target) return;
  target.innerHTML = state.data.users
    .map(
      (user) => `
        <article class="row user-row">
          <div><strong>${escapeHtml(user.name)}</strong><span>${escapeHtml(user.email)}</span></div>
          <span>${roleLabel(user.role)}</span>
          <span class="pill" data-status="${escapeHtml(user.status)}">${statusLabel(user.status)}</span>
          <span title="日 ${tokenTitle(user.dailyTokenLimit)} / 周 ${tokenTitle(user.weeklyTokenLimit)}">日 ${formatTokenLimit(user.dailyTokenLimit)} / 周 ${formatTokenLimit(user.weeklyTokenLimit)}</span>
          <span>${formatTime(user.lastLoginAt)}</span>
          <div class="row-actions">
            <button type="button" data-user-edit="${escapeHtml(user.id)}">编辑</button>
            <button type="button" data-user-reset="${escapeHtml(user.id)}">重置密码</button>
            <button type="button" data-user-delete="${escapeHtml(user.id)}">删除</button>
          </div>
        </article>
      `,
    )
    .join("") || `<p class="empty">还没有用户。请先创建一个登录账号。</p>`;
}

function renderUsage() {
  const target = $("[data-usage-users]");
  if (!target) return;
  target.innerHTML = state.data.usageByUser
    .map((item) => {
      const dailyPct = item.dailyTokenLimit ? Math.min(100, Math.round((item.dailyTokens / item.dailyTokenLimit) * 100)) : 0;
      const weeklyPct = item.weeklyTokenLimit ? Math.min(100, Math.round((item.weeklyTokens / item.weeklyTokenLimit) * 100)) : 0;
      return `
        <article class="usage-card">
          <header><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.email)}</span></header>
          <div class="quota-line"><span>今日</span><div class="bar"><i style="width:${dailyPct}%"></i></div><b title="${escapeHtml(tokenTitle(item.dailyTokens))} / ${item.dailyTokenLimit ? escapeHtml(tokenTitle(item.dailyTokenLimit)) : "不限"}">${formatToken(item.dailyTokens)} / ${formatTokenLimit(item.dailyTokenLimit)}</b></div>
          <div class="quota-line"><span>本周</span><div class="bar"><i style="width:${weeklyPct}%"></i></div><b title="${escapeHtml(tokenTitle(item.weeklyTokens))} / ${item.weeklyTokenLimit ? escapeHtml(tokenTitle(item.weeklyTokenLimit)) : "不限"}">${formatToken(item.weeklyTokens)} / ${formatTokenLimit(item.weeklyTokenLimit)}</b></div>
          <footer><span title="${escapeHtml(tokenTitle(item.totalTokens))}">累计 ${formatToken(item.totalTokens)} Token</span><span class="pill" data-status="${item.overDaily || item.overWeekly ? "disabled" : "active"}">${item.overDaily || item.overWeekly ? "已超限" : "正常"}</span></footer>
        </article>
      `;
    })
    .join("") || `<p class="empty">暂无用量数据。</p>`;
}

function renderLogFilters() {
  const select = $("[data-log-user-filter]");
  if (!select) return;
  const current = select.value;
  const users = state.data.logUsers?.length ? state.data.logUsers : state.data.users;
  select.innerHTML = `<option value="">全部用户</option>${users.map((u) => `<option value="${escapeHtml(u.email)}">${escapeHtml(u.name)} / ${escapeHtml(u.email)}${u.deletedAt ? "（已删除）" : ""}</option>`).join("")}`;
  select.value = current;
}

function renderLogs() {
  const target = $("[data-logs]");
  if (!target) return;
  target.innerHTML = state.data.logs
    .map((log) => {
      const detail = JSON.stringify(log.detail || {}, null, 2);
      return `
        <article class="log-item" data-level="${escapeHtml(log.level)}">
          <span class="pill">${escapeHtml(log.level)}</span>
          <div>
            <strong>${escapeHtml(log.message)}</strong>
            <span>${escapeHtml([log.userEmail || "未知用户", log.deviceId || "未知设备", log.sessionId].filter(Boolean).join(" / "))}</span>
          </div>
          <span>${escapeHtml(log.source)}</span>
          <span>${formatTime(log.time)}</span>
          <button type="button" data-log-detail="${escapeHtml(log.id)}" data-detail="${escapeHtml(detail)}">详情</button>
        </article>
      `;
    })
    .join("") || `<p class="empty">暂无错误日志。</p>`;
}

function renderRuntimeAudit() {
  const audit = state.data.runtimeAudit || {};
  const summary = audit.summary || {};
  const summaryTarget = $("[data-runtime-audit-summary]");
  if (summaryTarget) {
    const cards = [
      ["Events", summary.events],
      ["Requests", summary.requests],
      ["Sessions", summary.sessions],
      ["Artifacts", summary.artifacts],
      ["Messages", summary.messages],
      ["Terminal", summary.terminalEvents],
      ["Policy blocked", summary.capabilityPolicyBlocked],
      ["Unknown types", summary.unknownEventTypes],
    ];
    summaryTarget.innerHTML = cards
      .map(([label, value]) => `<article><span>${escapeHtml(label)}</span><strong>${formatNumber(value || 0)}</strong></article>`)
      .join("");
  }

  const typeTarget = $("[data-runtime-audit-types]");
  if (typeTarget) {
    const entries = Object.entries(audit.eventTypeCounts || {}).sort((a, b) => b[1] - a[1]);
    typeTarget.innerHTML = entries
      .map(([type, count]) => `
        <article class="audit-count-row">
          <strong>${escapeHtml(type)}</strong>
          <span>${formatNumber(count)}</span>
        </article>
      `)
      .join("") || `<p class="empty">No runtime events synced yet.</p>`;
  }

  const requestTarget = $("[data-runtime-audit-requests]");
  if (requestTarget) {
    requestTarget.innerHTML = (audit.requests || [])
      .map((item) => `
        <article class="audit-request-row">
          <div>
            <strong>req:${escapeHtml(item.requestHash || "none")}</strong>
            <span>session:${escapeHtml(item.sessionHash || "none")} / user:${escapeHtml(item.userHash || "none")}</span>
          </div>
          <span>${formatNumber(item.eventCount || 0)} events</span>
          <span>${formatNumber(item.artifactCount || 0)} artifacts</span>
          <span>${formatNumber(item.messageCount || 0)} messages</span>
          <span>${formatTime(item.lastIngestedAt || item.lastEventAt)}</span>
        </article>
      `)
      .join("") || `<p class="empty">No request projection available.</p>`;
  }

  const eventTarget = $("[data-runtime-audit-events]");
  if (eventTarget) {
    eventTarget.innerHTML = (audit.recentEvents || [])
      .map((item) => {
        const detail = item.detail || {};
        const detailMeta = [
          detail.keyCount ? `${detail.keyCount} keys` : "",
          detail.unknownKeyCount ? `${detail.unknownKeyCount} redacted` : "",
          Array.isArray(detail.keys) && detail.keys.length ? `known: ${detail.keys.join(", ")}` : "",
        ].filter(Boolean).join(" / ");
        return `
          <article class="audit-event-row">
            <span class="pill" data-status="${escapeHtml(item.status || "active")}">${escapeHtml(item.eventType || "unknown")}</span>
            <div>
              <strong>${escapeHtml(item.eventHash || "event")}</strong>
              <span>req:${escapeHtml(item.requestHash || "none")} / session:${escapeHtml(item.sessionHash || "none")}</span>
            </div>
            <span>${escapeHtml(item.status || "unknown")}</span>
            <span>${escapeHtml(item.source || "unknown")}</span>
            <span>${formatTime(item.ingestedAt || item.createdAt)}</span>
            <code>${escapeHtml(detailMeta || "redacted detail")}</code>
          </article>
        `;
      })
      .join("") || `<p class="empty">No recent runtime events.</p>`;
  }
}

function renderModel() {
  const target = $("[data-global-model]");
  if (!target) return;
  const model = state.data.globalModel || state.data.modelCredentials?.[0];
  if (!model) {
    target.innerHTML = `
      <article class="model-empty">
        <strong>尚未配置企业模型</strong>
        <span>点击“编辑模型”配置 Base URL、API Key 和模型名称后，用户端即可开箱连接。</span>
      </article>
    `;
    return;
  }
  target.innerHTML = `
    <article class="model-summary">
      <div><span>名称</span><strong>${escapeHtml(model.name)}</strong></div>
      <div><span>供应商</span><strong>${escapeHtml(model.provider)}</strong></div>
      <div><span>模型</span><strong>${escapeHtml(model.model)}</strong></div>
      <div><span>Base URL</span><strong>${escapeHtml(model.baseUrl)}</strong></div>
      <div><span>API Key</span><strong>${escapeHtml(model.apiKeyMask || "已保存")}</strong></div>
      <div><span>状态</span><strong>${model.enabled ? "已启用" : "未启用"}</strong></div>
    </article>
  `;
}

function renderCapabilities() {
  const policy = $("[data-capability-policy]");
  if (policy) {
    policy.innerHTML = `
      <article><span>镜像源</span><strong>${escapeHtml(state.data.capabilityPolicy.mirror)}</strong></article>
      <article><span>默认策略</span><strong>${modeLabel(state.data.capabilityPolicy.mode)}</strong></article>
      <article><span>离线缓存</span><strong>${escapeHtml(state.data.capabilityPolicy.offlineCache)}</strong></article>
    `;
  }
  const target = $("[data-capabilities]");
  if (!target) return;
  target.innerHTML = state.data.capabilities
    .map(
      (item) => `
        <article class="capability-item">
          <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.id)}</span></div>
          <span>${escapeHtml(item.mode)}</span>
          <span>${escapeHtml(item.size)}</span>
          <span class="pill">${escapeHtml(item.status)}</span>
        </article>
      `,
    )
    .join("");
}

function renderRelease() {
  fetch("../manifest.json", { cache: "no-store" })
    .then((response) => response.json())
    .then((manifest) => {
      setMetric("version", manifest.version || "0.2.5");
      const target = $("[data-release]");
      target.innerHTML = manifest.artifacts
        .map(
          (item) => `
            <article class="release-item">
              <strong>${escapeHtml(item.platform)} ${escapeHtml(item.variant)}</strong>
              <span>${escapeHtml(item.fileName)}</span>
              <span title="${escapeHtml(item.sha256)}">${escapeHtml(item.status)}</span>
            </article>
          `,
        )
        .join("");
    })
    .catch(() => {
      const target = $("[data-release]");
      target.innerHTML = `<p class="empty">发布清单暂不可用。</p>`;
    });
}

function renderMetrics() {
  const summary = state.data.summary || {};
  const totalTokens = summary.tokens ?? summary.monthlyCalls ?? 0;
  setMetric("users", formatNumber(summary.users ?? state.data.users.length));
  setMetric("tokens", formatToken(totalTokens));
  const tokenMetric = $(`[data-metric="tokens"]`);
  if (tokenMetric) tokenMetric.title = tokenTitle(totalTokens);
  setMetric("errors", formatNumber(summary.errors ?? 0));
  setMetric("capabilities", formatNumber(summary.capabilities ?? 0));
  setMetric("modelCredentials", formatNumber(summary.modelCredentials ?? (state.data.globalModel ? 1 : 0)));
  setMetric("version", state.data.version || summary.version || "0.2.5");
}

function render() {
  renderMetrics();
  renderUsers();
  renderUsage();
  renderLogFilters();
  renderLogs();
  renderRuntimeAudit();
  renderModel();
  renderCapabilities();
  renderRelease();
}

async function loadState(query = "") {
  try {
    const payload = await request(`/state${query}`);
    mergeState(payload);
    state.connected = true;
    setApiStatus(true);
  } catch (error) {
    state.connected = false;
    setApiStatus(false, error.message);
  }
  render();
}

async function mutate(path, options) {
  try {
    const payload = await request(path, options);
    mergeState(payload.state || payload);
    setApiStatus(true);
    render();
    showNotice("操作已保存", "info");
    return payload;
  } catch (error) {
    setApiStatus(false, error.message);
    showNotice(error.message || "操作失败，请检查权限或稍后重试。", "error");
    throw error;
  }
}

function showModal(title, templateName, setup) {
  const backdrop = $("[data-modal]");
  const body = $("[data-modal-body]");
  const template = $(`[data-template="${templateName}"]`);
  $("[data-modal-title]").textContent = title;
  body.innerHTML = "";
  body.append(template.content.cloneNode(true));
  setup?.(body);
  backdrop.hidden = false;
}

function renderModelTestResult(target, result, runningLabel = "") {
  if (!target) return;
  if (runningLabel) {
    target.dataset.tone = "running";
    target.innerHTML = `<strong>${escapeHtml(runningLabel)}</strong><span>正在向上游模型发送测试请求，请稍候。</span>`;
    return;
  }
  const ok = Boolean(result?.ok);
  target.dataset.tone = ok ? "ok" : "error";
  const meta = [
    result?.endpoint ? `端点：${result.endpoint}` : "",
    result?.statusCode ? `HTTP ${result.statusCode}` : "",
    result?.latencyMs ? `${result.latencyMs} ms` : "",
  ].filter(Boolean).join(" / ");
  target.innerHTML = `
    <strong>${ok ? "测试通过" : "测试失败"}</strong>
    <span>${escapeHtml(result?.message || "没有返回测试结果")}</span>
    ${result?.replyPreview ? `<code>${escapeHtml(result.replyPreview)}</code>` : ""}
    ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
  `;
}

function closeModal() {
  const backdrop = $("[data-modal]");
  if (backdrop) backdrop.hidden = true;
}

$("[data-theme-toggle]")?.addEventListener("click", () => {
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("ecorex-admin-theme", root.dataset.theme);
});

$all("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => {
    $all("[data-panel]").forEach((item) => item.classList.remove("active"));
    $all("[data-panel-view]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $(`[data-panel-view="${button.dataset.panel}"]`)?.classList.add("active");
  });
});

$("[data-modal-close]")?.addEventListener("click", closeModal);
$("[data-modal]")?.addEventListener("click", (event) => {
  if (event.target.matches("[data-modal]")) closeModal();
});

$all("[data-refresh]").forEach((button) => {
  button.addEventListener("click", () => loadState());
});

$("[data-user-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.dailyTokenLimit = Number(payload.dailyTokenLimit || 0);
  payload.weeklyTokenLimit = Number(payload.weeklyTokenLimit || 0);
  try {
    await mutate("/users", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
  } catch {
    // mutate already surfaced the error.
  }
});

$("[data-users]")?.addEventListener("click", (event) => {
  const edit = event.target.closest("[data-user-edit]");
  const reset = event.target.closest("[data-user-reset]");
  const remove = event.target.closest("[data-user-delete]");
  if (edit) {
    const user = state.data.users.find((item) => item.id === edit.dataset.userEdit);
    if (!user) return;
    showModal("编辑用户", "edit-user", (body) => {
      const form = $("[data-edit-user-form]", body);
      form.elements.id.value = user.id;
      form.elements.name.value = user.name;
      form.elements.email.value = user.email;
      form.elements.role.value = user.role;
      form.elements.status.value = user.status;
      form.elements.dailyTokenLimit.value = user.dailyTokenLimit || 0;
      form.elements.weeklyTokenLimit.value = user.weeklyTokenLimit || 0;
      form.addEventListener("submit", async (submitEvent) => {
        submitEvent.preventDefault();
        const payload = Object.fromEntries(new FormData(form).entries());
        payload.dailyTokenLimit = Number(payload.dailyTokenLimit || 0);
        payload.weeklyTokenLimit = Number(payload.weeklyTokenLimit || 0);
        try {
          await mutate(`/users/${encodeURIComponent(user.id)}`, { method: "PATCH", body: JSON.stringify(payload) });
          closeModal();
        } catch {
          // mutate already surfaced the error.
        }
      });
    });
  }
  if (reset) {
    const user = state.data.users.find((item) => item.id === reset.dataset.userReset);
    if (!user) return;
    showModal(`重置密码：${user.name}`, "reset-password", (body) => {
      const form = $("[data-reset-password-form]", body);
      form.elements.id.value = user.id;
      form.addEventListener("submit", async (submitEvent) => {
        submitEvent.preventDefault();
        try {
          await mutate(`/users/${encodeURIComponent(user.id)}/reset-password`, {
            method: "POST",
            body: JSON.stringify({ password: form.elements.password.value }),
          });
          closeModal();
        } catch {
          // mutate already surfaced the error.
        }
      });
    });
  }
  if (remove) {
    const user = state.data.users.find((item) => item.id === remove.dataset.userDelete);
    if (!user || !confirm(`确认删除/禁用用户 ${user.name}？历史用量和错误记录会保留。`)) return;
    mutate(`/users/${encodeURIComponent(user.id)}`, { method: "DELETE" }).catch(() => undefined);
  }
});

$("[data-log-filter]")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(event.currentTarget).entries()) {
    if (value) params.set(key, value);
  }
  loadState(params.toString() ? `?${params}` : "");
});

$("[data-logs]")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-log-detail]");
  if (!button) return;
  showModal("错误详情", "reset-password", (body) => {
    body.innerHTML = `<pre class="detail-pre">${escapeHtml(button.dataset.detail || "{}")}</pre>`;
  });
});

$("[data-clear-errors]")?.addEventListener("click", () => mutate("/logs/mark-read", { method: "POST", body: JSON.stringify({}) }).catch(() => undefined));

$("[data-model-edit]")?.addEventListener("click", () => {
  const model = state.data.globalModel || {};
  showModal("编辑企业全局模型", "edit-model", (body) => {
    const form = $("[data-model-form]", body);
    const resultTarget = $("[data-model-test-result]", body);
    form.elements.name.value = model.name || "EcoreX 企业模型";
    form.elements.provider.value = model.provider || "custom";
    form.elements.model.value = model.model || "";
    form.elements.baseUrl.value = model.baseUrl || "";
    if (form.elements.botType) {
      form.elements.botType.value = model.botType || (model.provider === "openai" ? "openai" : "custom");
    }
    $all("[data-model-test]", body).forEach((button) => {
      button.addEventListener("click", async () => {
        const payload = Object.fromEntries(new FormData(form).entries());
        payload.action = button.dataset.modelTest;
        renderModelTestResult(resultTarget, null, button.textContent.trim());
        button.disabled = true;
        try {
          const result = await request("/model-credentials/global/test", { method: "POST", body: JSON.stringify(payload) });
          renderModelTestResult(resultTarget, result);
        } catch (error) {
          renderModelTestResult(resultTarget, { ok: false, message: error.message || "模型测试失败" });
        } finally {
          button.disabled = false;
        }
      });
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = Object.fromEntries(new FormData(form).entries());
      try {
        await mutate("/model-credentials/global", { method: "POST", body: JSON.stringify(payload) });
        closeModal();
      } catch {
        // mutate already surfaced the error.
      }
    });
  });
});

$("[data-capability-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  await mutate("/capability-policy", { method: "POST", body: JSON.stringify(payload) }).catch(() => undefined);
});

loadState();
