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
    release: {
      current: {},
      staged: [],
      promotion: {},
    },
    summary: {},
    runtimeAudit: {
      summary: {},
      actionTypeCounts: {},
      actionTypeLabels: {},
      userActions: [],
      effectiveArtifacts: [],
      feedbackTraces: [],
      eventTypeCounts: {},
      sourceCounts: {},
      statusCounts: {},
      requests: [],
      recentEvents: [],
      privacy: {},
    },
    version: "0.3.0",
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

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size <= 0) return "未记录";
  if (size >= 1024 * 1024) return `${trimUnitDecimal((size / 1024 / 1024).toFixed(1))} MB`;
  if (size >= 1024) return `${trimUnitDecimal((size / 1024).toFixed(1))} KB`;
  return `${formatNumber(size)} B`;
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

function validationLabel(status) {
  return status === "pass" ? "通过" : status === "not-configured" ? "未配置" : "失败";
}

function releaseIndexLabel(index) {
  if (!index) return "未检查";
  if (index.status === "pass") return "可信";
  if (index.status === "not-configured" && !index.required) return "未启用";
  return "不可信";
}

function releaseRiskStatus(risks = []) {
  if (risks.some((item) => item.severity === "high")) return "failed";
  if (risks.some((item) => item.severity === "medium")) return "obsolete";
  return risks.length ? "current" : "active";
}

function releaseFailureDetails(validation, releaseIndex) {
  const failures = [
    ...(Array.isArray(validation?.failures) ? validation.failures : []),
    ...(Array.isArray(releaseIndex?.failures) ? releaseIndex.failures.map((item) => `release-index: ${item}`) : []),
  ].filter(Boolean).slice(0, 8);
  if (!failures.length) return "";
  return `<details class="release-failures"><summary>校验明细</summary>${failures.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</details>`;
}

const MODEL_PROVIDER_BOT_TYPES = {
  openai: "openai",
  deepseek: "deepseek",
  zhipu: "zhipuai",
  moonshot: "moonshot",
  doubao: "doubao",
  qianfan: "qianfan",
  gemini: "gemini",
  claude: "claudeapi",
  custom: "custom"
};

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
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
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
  const actionLabels = audit.actionTypeLabels || {};
  const summaryTarget = $("[data-runtime-audit-summary]");
  if (summaryTarget) {
    const cards = [
      ["用户动作", summary.userActions],
      ["图片处理", summary.imageProcessingActions],
      ["有效产物", summary.effectiveArtifacts],
      ["下拇指", summary.thumbsDownArtifacts],
      ["可回溯", summary.feedbackTraceCount],
      ["策略拦截", summary.capabilityPolicyBlocked],
      ["请求", summary.requests],
      ["最近同步", summary.lastIngestedAt ? formatTime(summary.lastIngestedAt) : "未记录"],
    ];
    summaryTarget.innerHTML = cards
      .map(([label, value]) => {
        const display = typeof value === "number" ? formatNumber(value || 0) : value || "未记录";
        return `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></article>`;
      })
      .join("");
  }

  const typeTarget = $("[data-runtime-audit-types]");
  if (typeTarget) {
    const entries = Object.entries(audit.actionTypeCounts || {}).sort((a, b) => b[1] - a[1]);
    typeTarget.innerHTML = entries
      .map(([type, count]) => `
        <article class="audit-count-row">
          <strong>${escapeHtml(actionLabels[type] || type)}</strong>
          <small>${escapeHtml(type)}</small>
          <span>${formatNumber(count)}</span>
        </article>
      `)
      .join("") || `<p class="empty">暂无用户动作同步。</p>`;
  }

  const actionTarget = $("[data-runtime-audit-actions]");
  if (actionTarget) {
    actionTarget.innerHTML = (audit.userActions || [])
      .map((item) => `
        <article class="audit-action-row">
          <span class="pill" data-status="${escapeHtml(item.status || "active")}">${escapeHtml(item.actionLabel || item.actionType || "动作")}</span>
          <div>
            <strong>${escapeHtml(item.eventHash || "event")}</strong>
            <span>req:${escapeHtml(item.requestHash || "none")} / session:${escapeHtml(item.sessionHash || "none")}</span>
          </div>
          <span>${escapeHtml(item.status || "unknown")}</span>
          <span>${formatTime(item.ingestedAt || item.occurredAt)}</span>
        </article>
      `)
      .join("") || `<p class="empty">暂无可展示的用户动作。</p>`;
  }

  const artifactTarget = $("[data-runtime-audit-effective-artifacts]");
  if (artifactTarget) {
    artifactTarget.innerHTML = (audit.effectiveArtifacts || [])
      .map((item) => {
        const title = item.artifactTitle || `${item.kind || "artifact"} ${item.pathExt || item.artifactHash || ""}`.trim();
        const meta = [
          item.pathExt,
          formatBytes(item.sizeBytes),
          item.artifactFeedbackSignal === "thumbs_up" ? "上拇指" : "默认有效",
        ].filter(Boolean).join(" / ");
        return `
          <article class="audit-artifact-row">
            <div>
              <strong>${escapeHtml(title || "有效产物")}</strong>
              <span>${escapeHtml(meta)}</span>
            </div>
            <span class="pill" data-status="${escapeHtml(item.artifactValidity || "valid")}">${escapeHtml(item.artifactValidity || "valid")}</span>
            <span>${formatTime(item.ingestedAt || item.createdAt)}</span>
            <code>${escapeHtml(item.artifactHash || "artifact")}</code>
          </article>
        `;
      })
      .join("") || `<p class="empty">暂无自动识别的有效产物。</p>`;
  }

  const feedbackTarget = $("[data-runtime-audit-feedback-traces]");
  if (feedbackTarget) {
    feedbackTarget.innerHTML = (audit.feedbackTraces || [])
      .map((item) => {
        const title = item.artifactTitle || `${item.kind || "artifact"} ${item.pathExt || item.artifactHash || ""}`.trim();
        const user = [item.userName, item.userEmail].filter(Boolean).join(" / ") || "未知用户";
        const traceLink = item.feedbackShareUrl
          ? `<a class="soft-link compact" href="${escapeHtml(item.feedbackShareUrl)}" target="_blank" rel="noopener">回看会话</a>`
          : `<span class="audit-muted">暂无分享链接</span>`;
        return `
          <article class="audit-feedback-row">
            <div>
              <strong>${escapeHtml(title || "下拇指产物")}</strong>
              <span>${escapeHtml(user)}</span>
            </div>
            <span>${formatTime(item.artifactFeedbackAt || item.ingestedAt || item.createdAt)}</span>
            <code>${escapeHtml(item.artifactHash || "artifact")}</code>
            ${traceLink}
          </article>
        `;
      })
      .join("") || `<p class="empty">暂无下拇指回溯记录。</p>`;
  }

  const eventTypeTarget = $("[data-runtime-audit-event-types]");
  if (eventTypeTarget) {
    const entries = Object.entries(audit.eventTypeCounts || {}).sort((a, b) => b[1] - a[1]);
    eventTypeTarget.innerHTML = entries
      .map(([type, count]) => `
        <article class="audit-count-row">
          <strong>${escapeHtml(type)}</strong>
          <span>${formatNumber(count)}</span>
        </article>
      `)
      .join("") || `<p class="empty">暂无技术事件。</p>`;
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
      .join("") || `<p class="empty">暂无请求投影。</p>`;
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
      .join("") || `<p class="empty">暂无最近运行事件。</p>`;
  }
}

function renderModel() {
  const target = $("[data-global-model]");
  if (!target) return;
  const models = Array.isArray(state.data.modelCredentials) ? state.data.modelCredentials : [];
  if (!models.length) {
    target.innerHTML = `
      <article class="model-empty">
        <strong>尚未配置企业模型</strong>
        <span>点击“编辑模型”配置 Base URL、API Key 和模型名称后，用户端即可开箱连接。</span>
      </article>
    `;
    return;
  }
  target.innerHTML = `
    ${models.map((model, index) => `
      <article class="model-summary">
        <div><span>${index === 0 ? "默认" : "名称"}</span><strong>${escapeHtml(model.name)}</strong></div>
        <div><span>供应商</span><strong>${escapeHtml(model.provider)}</strong></div>
        <div><span>模型</span><strong>${escapeHtml(model.model)}</strong></div>
        <div><span>Base URL</span><strong>${escapeHtml(model.baseUrl)}</strong></div>
        <div><span>API Key</span><strong>${escapeHtml(model.apiKeyMask || "已保存")}</strong></div>
        <div><span>状态</span><strong>${model.enabled ? "已启用" : "未启用"}</strong></div>
        <div class="row-actions"><button type="button" data-model-edit-id="${escapeHtml(model.id)}">编辑</button></div>
      </article>
    `).join("")}
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
  const target = $("[data-release]");
  const summaryTarget = $("[data-release-summary]");
  const controlsTarget = $("[data-release-controls]");
  if (!target) return;
  const release = state.data.release || {};
  const current = release.current || {};
  const staged = Array.isArray(release.staged) ? release.staged : [];
  const policy = current.updatePolicy || {};
  const rollout = policy.rollout || {};
  const killSwitch = policy.killSwitch || {};
  const rollback = policy.rollback || {};
  const backgroundUpdate = policy.backgroundUpdate || {};
  const connectorHealthCheck = policy.connectorHealthCheck || {};
  const releaseIndex = current.releaseIndex || {};
  const risks = Array.isArray(release.risks) ? release.risks : [];
  if (summaryTarget) {
    summaryTarget.innerHTML = `
      <article><span>当前 stable</span><strong>${escapeHtml(current.version || "未发布")}</strong></article>
      <article><span>候选版本</span><strong>${formatNumber(staged.length)}</strong></article>
      <article><span>Release Index</span><strong>${releaseIndexLabel(releaseIndex)}</strong></article>
      <article><span>风险</span><strong>${risks.length ? `${formatNumber(risks.length)} 项` : "清洁"}</strong></article>
    `;
  }
  if (controlsTarget) {
    const stateMachine = Array.isArray(policy.stateMachine) && policy.stateMachine.length
      ? policy.stateMachine.join(" → ")
      : "available → downloading → verified → staged → deferred → installed → activated → rollback";
    controlsTarget.innerHTML = `
      <article>
        <span>发布策略</span>
        <strong>${escapeHtml(policy.channel || "stable")} / ${escapeHtml(policy.promotion || "admin-gated")}</strong>
        <small>${escapeHtml(backgroundUpdate.activationPolicy || "prompt-soft-refresh-existing-tab")}</small>
      </article>
      <article>
        <span>灰度</span>
        <strong>${formatNumber(rollout.percent || 0)}%</strong>
        <small>${escapeHtml(rollout.strategy || "progressive")} · 健康 ${formatNumber(rollout.minimumHealthyMinutes || 0)} 分钟</small>
      </article>
      <article>
        <span>Kill-switch</span>
        <strong class="${killSwitch.enabled ? "danger-text" : ""}">${killSwitch.enabled ? "已开启" : "关闭"}</strong>
        <small>${escapeHtml(killSwitch.reason || killSwitch.updatedAt || "无阻断")}</small>
      </article>
      <article>
        <span>回滚</span>
        <strong>${rollback.enabled ? "可回滚" : "未配置"}</strong>
        <small>${escapeHtml(rollback.healthCheck || backgroundUpdate.healthCheck || "/api/version")}</small>
      </article>
      <article class="wide">
        <span>在线更新状态机</span>
        <strong>${escapeHtml(stateMachine)}</strong>
        <small>${escapeHtml(backgroundUpdate.idleGate || "/api/active-requests")} · ${escapeHtml(backgroundUpdate.stateFile || "update-state.json")}</small>
      </article>
      <article class="wide">
        <span>外部连接保护</span>
        <strong>${connectorHealthCheck.required ? "已启用" : "未启用"} · ${escapeHtml(connectorHealthCheck.failureAction || "defer-or-rollback")}</strong>
        <small>${escapeHtml((connectorHealthCheck.preserve || ["configured", "connected", "callable"]).join(", "))} · ${escapeHtml((connectorHealthCheck.endpoints || []).join(" / ") || "/api/external-connections")}</small>
      </article>
      <article class="wide">
        <span>管理风险</span>
        ${
          risks.length
            ? risks.map((item) => `<b class="release-risk" data-severity="${escapeHtml(item.severity)}">${escapeHtml(item.message)}</b>`).join("")
            : `<b class="release-risk" data-severity="ok">发布链路暂无阻断</b>`
        }
      </article>
    `;
  }
  const currentRow = current.exists ? `
    <article class="release-item is-current" data-release-health="${escapeHtml(releaseRiskStatus(risks))}">
      <div><strong>当前 stable</strong><span>${escapeHtml(current.id || "current")}</span></div>
      <span>${escapeHtml(current.version || "未知版本")}</span>
      <span title="${escapeHtml(current.releaseIndex?.commit || "")}">Index ${escapeHtml(releaseIndexLabel(current.releaseIndex))}</span>
      <span>${escapeHtml(current.updatedAt || "未记录")}</span>
      <div class="row-actions">
        <span class="pill" data-status="${current.validation?.status === "pass" ? "current" : "disabled"}">${current.validation?.status === "pass" ? "已发布" : "需检查"}</span>
        <button type="button" data-release-promote="${escapeHtml(current.version || "")}" data-release-staged-id="" data-release-action="notify" data-release-can-promote="0" data-release-can-notify="${current.canNotify ? "1" : "0"}" data-release-disabled-reason="${escapeHtml(current.validation?.status === "pass" ? "" : "当前 stable 校验未通过，不能通知用户")}" aria-disabled="${current.canNotify ? "false" : "true"}" title="通知已安装用户检查更新">通知用户</button>
      </div>
      ${releaseFailureDetails(current.validation, current.releaseIndex)}
    </article>
  ` : "";
  const stagedRows = staged
    .map((item) => {
      const valid = item.validation?.status === "pass";
      const canPromote = Boolean(item.canPromote);
      const disabledReason = item.promoteDisabledReason || (canPromote ? "" : "当前候选不可发布");
      const canNotify = !canPromote && disabledReason.includes("当前 stable");
      let pillStatus = canPromote ? "active" : "disabled";
      let pillText = canPromote ? "可发布" : "不可发布";
      let buttonText = canPromote ? "发布新版" : "查看原因";
      let action = canPromote ? "promote" : "reason";
      if (!canPromote && disabledReason.includes("当前 stable")) {
        pillStatus = "current";
        pillText = "已发布";
        buttonText = "通知用户";
        action = "notify";
      } else if (!canPromote && disabledReason.includes("低于当前 stable")) {
        pillStatus = "obsolete";
        pillText = "旧版本";
      } else if (!canPromote && !valid) {
        pillText = "校验失败";
      }
      return `
        <article class="release-item">
          <div><strong>${escapeHtml(item.version || "未知版本")}</strong><span>${escapeHtml(item.id || "staged")}</span></div>
          <span>${escapeHtml(item.updatedAt || "未记录")}</span>
          <span title="${escapeHtml(item.releaseIndex?.commit || "")}">Index ${escapeHtml(releaseIndexLabel(item.releaseIndex))}</span>
          <span>${formatNumber(item.readyArtifactCount || 0)} ready</span>
          <div class="row-actions">
            <span class="pill" data-status="${pillStatus}" title="${escapeHtml(disabledReason)}">${pillText}</span>
            <button type="button" data-release-promote="${escapeHtml(item.version || "")}" data-release-staged-id="${escapeHtml(item.id || "")}" data-release-action="${action}" data-release-can-promote="${canPromote ? "1" : "0"}" data-release-can-notify="${canNotify ? "1" : "0"}" data-release-disabled-reason="${escapeHtml(disabledReason)}" aria-disabled="${canPromote || canNotify ? "false" : "true"}" title="${escapeHtml(canPromote ? "发布到 stable" : (canNotify ? "重新通知已安装用户检查更新" : disabledReason))}">${buttonText}</button>
          </div>
          ${releaseFailureDetails(item.validation, item.releaseIndex)}
        </article>
      `;
    })
    .join("");
  target.innerHTML = currentRow + stagedRows || `<p class="empty">发布清单暂不可用。</p>`;
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
  setMetric("version", state.data.version || summary.version || "0.3.0");
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
    setApiStatus(Boolean(error.status), error.message);
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

$("[data-release-refresh]")?.addEventListener("click", async () => {
  try {
    const payload = await request("/release/state");
    mergeState(payload);
    renderRelease();
    setApiStatus(true);
    showNotice("发布状态已刷新", "info");
  } catch (error) {
    setApiStatus(false, error.message);
    showNotice(error.message || "发布状态刷新失败", "error");
  }
});

document.addEventListener("click", async (event) => {
  const clicked = event.target instanceof Element ? event.target : null;
  const button = clicked?.closest("[data-release-promote]");
  if (!button) return;
  const version = button.dataset.releasePromote || "";
  const stagedId = button.dataset.releaseStagedId || "";
  if (!version) {
    showNotice("候选版本缺失，刷新发布状态后再试。", "error");
    return;
  }
  if (button.dataset.releaseAction === "notify" && button.dataset.releaseCanNotify !== "1") {
    showNotice(button.dataset.releaseDisabledReason || "当前版本不能通知用户。", "warn");
    return;
  }
  if (button.dataset.releaseAction === "notify" || button.dataset.releaseCanNotify === "1") {
    if (!window.confirm(`${version} 已经是 stable。现在重新通知已安装用户检查更新？`)) return;
    button.disabled = true;
    button.textContent = "通知中...";
    showNotice(`正在通知用户检查 EcoreX ${version}，请稍候。`, "info");
    try {
      const payload = await mutate("/release/notify", {
        method: "POST",
        body: JSON.stringify({ version, stagedId, actor: "admin-ui" }),
      });
      if (payload.release) {
        mergeState({ release: payload.release });
        renderRelease();
      }
      showNotice(payload.message || `已通知用户检查 EcoreX ${version}`, "info");
    } catch (_) {
      button.disabled = false;
      button.textContent = "通知用户";
    }
    return;
  }
  if (button.dataset.releaseCanPromote !== "1") {
    showNotice(button.dataset.releaseDisabledReason || "当前候选不可发布。", "warn");
    return;
  }
  if (!window.confirm(`发布 ${version} 到 stable，并让用户本机在线更新可见？`)) return;
  button.disabled = true;
  button.textContent = "发布中...";
  showNotice(`正在发布 ${version}，请稍候。`, "info");
  try {
    const payload = await mutate("/release/promote", {
      method: "POST",
      body: JSON.stringify({ version, stagedId, actor: "admin-ui" }),
    });
    if (payload.release) {
      mergeState({ release: payload.release });
      renderRelease();
    }
    showNotice(`已发布 ${version}`, "info");
  } catch (_) {
    button.disabled = false;
    button.textContent = "发布新版";
  }
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

function openModelEditor(model = {}, endpoint = "/model-credentials/global", method = "POST", title = "编辑企业全局模型") {
  showModal(title, "edit-model", (body) => {
    const form = $("[data-model-form]", body);
    const resultTarget = $("[data-model-test-result]", body);
    form.elements.name.value = model.name || "EcoreX 企业模型";
    form.elements.provider.value = model.provider || "custom";
    form.elements.model.value = model.model || "";
    form.elements.baseUrl.value = model.baseUrl || "";
    if (form.elements.botType) {
      form.elements.botType.value = model.botType || MODEL_PROVIDER_BOT_TYPES[form.elements.provider.value] || "custom";
      form.elements.provider.addEventListener("change", () => {
        if (!form.elements.botType.dataset.userEdited) {
          form.elements.botType.value = MODEL_PROVIDER_BOT_TYPES[form.elements.provider.value] || "custom";
        }
      });
      form.elements.botType.addEventListener("change", () => {
        form.elements.botType.dataset.userEdited = "true";
      });
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
        await mutate(endpoint, { method, body: JSON.stringify(payload) });
        closeModal();
      } catch {
        // mutate already surfaced the error.
      }
    });
  });
}

$("[data-model-edit]")?.addEventListener("click", () => {
  const model = state.data.globalModel || {};
  openModelEditor(model);
});

$("[data-model-add]")?.addEventListener("click", () => {
  openModelEditor(
    { name: "新增模型凭证", provider: "custom", model: "", baseUrl: "", botType: "custom" },
    "/model-credentials",
    "POST",
    "新增模型凭证"
  );
});

$("[data-global-model]")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-model-edit-id]");
  if (!button) return;
  const id = button.dataset.modelEditId;
  const model = (state.data.modelCredentials || []).find((item) => item.id === id) || {};
  openModelEditor(model, `/model-credentials/${encodeURIComponent(id)}`, "PUT", "编辑模型凭证");
});

$("[data-capability-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  await mutate("/capability-policy", { method: "POST", body: JSON.stringify(payload) }).catch(() => undefined);
});

loadState();
