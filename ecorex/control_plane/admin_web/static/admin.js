"use strict";

(() => {
  const API_BASE = "/api/v1/admin";
  const MAX_MANIFEST_BYTES = 16 * 1024 * 1024;
  const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
  const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

  let adminToken = "";
  let manifest = null;
  let manifestSha256 = null;
  let candidate = null;
  let rollout = null;
  let rollback = null;
  let sessionConnected = false;
  let busy = false;
  let fileReadGeneration = 0;
  let pendingConfirmation = null;
  const requestIds = new Map();
  const channelStates = new Map([
    ["canary", "unknown"],
    ["stable", "unknown"],
  ]);

  const byId = (id) => {
    const element = document.getElementById(id);
    if (!element) throw new Error(`管理台缺少必要节点：${id}`);
    return element;
  };

  const sha256Hex = async (bytes) => {
    if (!globalThis.crypto?.subtle) {
      throw new Error("当前浏览器无法校验 Manifest 摘要");
    }
    const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
    return Array.from(digest, (value) => value.toString(16).padStart(2, "0")).join("");
  };

  const elements = {
    authForm: byId("auth-form"),
    tokenInput: byId("admin-token"),
    connectButton: byId("connect-button"),
    refreshStateButton: byId("refresh-state-button"),
    clearTokenButton: byId("clear-token-button"),
    sessionLabel: byId("session-label"),
    message: byId("global-message"),
    messageText: byId("global-message-text"),
    dismissMessage: byId("dismiss-message-button"),
    manifestForm: byId("manifest-form"),
    manifestFile: byId("manifest-file"),
    manifestHelp: byId("manifest-help"),
    createCandidateButton: byId("create-candidate-button"),
    candidateStatus: byId("candidate-status"),
    candidateProjection: byId("candidate-projection"),
    candidateReleaseId: byId("candidate-release-id"),
    candidateVersion: byId("candidate-version"),
    candidateChannel: byId("candidate-channel"),
    candidateDigest: byId("candidate-digest"),
    gateRegion: byId("gate-region"),
    gateSummary: byId("gate-summary"),
    gateTableBody: byId("gate-table-body"),
    publishButton: byId("publish-button"),
    rolloutForm: byId("rollout-form"),
    rolloutReleaseId: byId("rollout-release-id"),
    rolloutPercentage: byId("rollout-percentage"),
    minimumVersion: byId("minimum-version"),
    targetOrganizations: byId("target-organizations"),
    targetAccounts: byId("target-accounts"),
    createRolloutButton: byId("create-rollout-button"),
    rolloutStatus: byId("rollout-status"),
    rolloutProjection: byId("rollout-projection"),
    rolloutId: byId("rollout-id"),
    rolloutRelease: byId("rollout-release"),
    rolloutPercent: byId("rollout-percent"),
    rolloutCreatedAt: byId("rollout-created-at"),
    rolloutActions: byId("rollout-actions"),
    rollbackForm: byId("rollback-form"),
    rollbackSourceReleaseId: byId("rollback-source-release-id"),
    rollbackTargetReleaseId: byId("rollback-target-release-id"),
    rollbackPercentage: byId("rollback-percentage"),
    rollbackTtl: byId("rollback-ttl"),
    rollbackOrganizations: byId("rollback-organizations"),
    rollbackAccounts: byId("rollback-accounts"),
    createRollbackButton: byId("create-rollback-button"),
    rollbackStatus: byId("rollback-status"),
    rollbackProjection: byId("rollback-projection"),
    rollbackId: byId("rollback-id"),
    rollbackSource: byId("rollback-source"),
    rollbackTarget: byId("rollback-target"),
    rollbackPercent: byId("rollback-percent"),
    rollbackAuthTtl: byId("rollback-auth-ttl"),
    rollbackActions: byId("rollback-actions"),
    refreshDistributionButton: byId("refresh-distribution-button"),
    distributionSummary: byId("distribution-summary"),
    distributionTableBody: byId("distribution-table-body"),
    confirmDialog: byId("confirm-dialog"),
    confirmTitle: byId("confirm-title"),
    confirmDescription: byId("confirm-description"),
    confirmCancel: byId("confirm-cancel-button"),
    confirmSubmit: byId("confirm-submit-button"),
  };

  class AdminApiError extends Error {
    constructor(message, status, code = null) {
      super(message);
      this.name = "AdminApiError";
      this.status = status;
      this.code = code;
    }
  }

  const isRecord = (value) => (
    value !== null && typeof value === "object" && !Array.isArray(value)
  );

  const requiredString = (value, field) => {
    if (typeof value !== "string" || value.length === 0 || value.length > 4096) {
      throw new Error(`Control Plane 返回了无效字段：${field}`);
    }
    return value;
  };

  const optionalString = (value, field) => {
    if (value === null || value === undefined) return null;
    return requiredString(value, field);
  };

  const stringList = (value, field) => {
    if (!Array.isArray(value) || value.length > 1000) {
      throw new Error(`Control Plane 返回了无效列表：${field}`);
    }
    return value.map((entry, index) => requiredString(entry, `${field}[${index}]`));
  };

  const normalizeCandidate = (value) => {
    if (!isRecord(value) || !isRecord(value.gates)) {
      throw new Error("Control Plane 返回的候选合同无效。");
    }
    const gates = {};
    const entries = Object.entries(value.gates);
    if (entries.length > 100) throw new Error("Control Plane 返回的门禁数量超过限制。");
    for (const [name, status] of entries) {
      if (!SAFE_SEGMENT.test(name)) throw new Error("Control Plane 返回了无效门禁名称。");
      gates[name] = requiredString(status, `gates.${name}`);
    }
    return {
      release_id: requiredString(value.release_id, "release_id"),
      version: requiredString(value.version, "version"),
      build_digest: requiredString(value.build_digest, "build_digest"),
      channel: requiredString(value.channel, "channel"),
      status: requiredString(value.status, "status"),
      gates,
      missing_gates: stringList(value.missing_gates, "missing_gates"),
    };
  };

  const normalizeRollout = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的灰度合同无效。");
    const percentage = Number(value.percentage);
    if (!Number.isInteger(percentage) || percentage < 1 || percentage > 100) {
      throw new Error("Control Plane 返回了无效灰度比例。");
    }
    return {
      rollout_id: requiredString(value.rollout_id, "rollout_id"),
      release_id: requiredString(value.release_id, "release_id"),
      channel: requiredString(value.channel, "channel"),
      status: requiredString(value.status, "status"),
      percentage,
      target_organization_ids: stringList(value.target_organization_ids, "target_organization_ids"),
      target_account_ids: stringList(value.target_account_ids, "target_account_ids"),
      minimum_compatible_version: optionalString(value.minimum_compatible_version, "minimum_compatible_version"),
      created_at: requiredString(value.created_at, "created_at"),
    };
  };

  const normalizeRollback = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的回滚合同无效。");
    const percentage = Number(value.percentage);
    const ttl = Number(value.authorization_ttl_seconds);
    if (!Number.isInteger(percentage) || percentage < 1 || percentage > 100
      || !Number.isInteger(ttl) || ttl < 60 || ttl > 900) {
      throw new Error("Control Plane 返回了无效回滚范围。");
    }
    return {
      rollback_id: requiredString(value.rollback_id, "rollback_id"),
      source_release_id: requiredString(value.source_release_id, "source_release_id"),
      target_release_id: requiredString(value.target_release_id, "target_release_id"),
      channel: requiredString(value.channel, "channel"),
      status: requiredString(value.status, "status"),
      percentage,
      target_organization_ids: stringList(value.target_organization_ids, "target_organization_ids"),
      target_account_ids: stringList(value.target_account_ids, "target_account_ids"),
      authorization_ttl_seconds: ttl,
      created_at: requiredString(value.created_at, "created_at"),
    };
  };

  const normalizeDistribution = (value) => {
    if (!isRecord(value) || !isRecord(value.versions) || !isRecord(value.update_states)) {
      throw new Error("Control Plane 返回的客户端分布合同无效。");
    }
    const total = Number(value.total_clients);
    if (!Number.isInteger(total) || total < 0) {
      throw new Error("Control Plane 返回了无效客户端总数。");
    }
    const counts = (record, field) => Object.entries(record).map(([key, raw]) => {
      const count = Number(raw);
      if (!key || key.length > 256 || !Number.isInteger(count) || count < 0) {
        throw new Error(`Control Plane 返回了无效分布：${field}`);
      }
      return [key, count];
    });
    return {
      total_clients: total,
      versions: counts(value.versions, "versions"),
      update_states: counts(value.update_states, "update_states"),
    };
  };

  const normalizeKillSwitch = (value) => {
    if (!isRecord(value) || typeof value.kill_switch_active !== "boolean") {
      throw new Error("Control Plane 返回的 Kill switch 合同无效。");
    }
    const channel = requiredString(value.channel, "channel");
    if (channel !== "stable" && channel !== "canary") {
      throw new Error("Control Plane 返回了无效发布通道。");
    }
    return {
      channel,
      kill_switch_active: value.kill_switch_active,
      halted_rollout_ids: stringList(value.halted_rollout_ids, "halted_rollout_ids"),
    };
  };

  const normalizeResume = (value) => {
    if (!isRecord(value) || value.schema_version !== 1) {
      throw new Error("Control Plane 返回的恢复合同版本无效。");
    }
    if (!Array.isArray(value.candidates) || value.candidates.length > 200) {
      throw new Error("Control Plane 返回的候选恢复列表无效。");
    }
    if (!Array.isArray(value.rollouts) || value.rollouts.length > 500) {
      throw new Error("Control Plane 返回的灰度恢复列表无效。");
    }
    if (!Array.isArray(value.channel_kill_switches) || value.channel_kill_switches.length !== 2) {
      throw new Error("Control Plane 返回的发布通道恢复列表无效。");
    }

    const candidates = value.candidates.map(normalizeCandidate);
    const rollouts = value.rollouts.map(normalizeRollout);
    const killSwitches = value.channel_kill_switches.map(normalizeKillSwitch);
    const candidateIds = new Set(candidates.map((item) => item.release_id));
    const rolloutIds = new Set(rollouts.map((item) => item.rollout_id));
    if (candidateIds.size !== candidates.length || rolloutIds.size !== rollouts.length) {
      throw new Error("Control Plane 返回的恢复事实包含重复 ID。");
    }

    const latestCandidateId = optionalString(value.latest_candidate_id, "latest_candidate_id");
    const latestRolloutId = optionalString(value.latest_rollout_id, "latest_rollout_id");
    if ((candidates.length === 0) !== (latestCandidateId === null)
      || (latestCandidateId !== null && !candidateIds.has(latestCandidateId))) {
      throw new Error("Control Plane 未显式指定有效的当前候选。");
    }
    if ((rollouts.length === 0) !== (latestRolloutId === null)
      || (latestRolloutId !== null && !rolloutIds.has(latestRolloutId))) {
      throw new Error("Control Plane 未显式指定有效的当前灰度。");
    }

    const channels = new Set(killSwitches.map((item) => item.channel));
    if (channels.size !== 2 || !channels.has("stable") || !channels.has("canary")) {
      throw new Error("Control Plane 必须分别返回 stable 与 canary 通道事实。");
    }
    const capturedAt = requiredString(value.captured_at, "captured_at");
    if (!/(?:Z|[+-]\d{2}:\d{2})$/u.test(capturedAt) || !Number.isFinite(Date.parse(capturedAt))) {
      throw new Error("Control Plane 返回的恢复时间无效。");
    }
    return {
      schema_version: 1,
      candidates,
      latest_candidate_id: latestCandidateId,
      rollouts,
      latest_rollout_id: latestRolloutId,
      channel_kill_switches: killSwitches,
      distribution: normalizeDistribution(value.distribution),
      captured_at: capturedAt,
    };
  };

  const safeSegment = (value) => {
    if (typeof value !== "string" || !SAFE_SEGMENT.test(value)) {
      throw new Error("资源标识不符合 Control Plane 路径合同。");
    }
    return encodeURIComponent(value);
  };

  const parseApiError = (payload, status) => {
    const fallback = `Control Plane 请求失败（${status}）。`;
    if (!isRecord(payload)) return { message: fallback, code: null };
    const detail = payload.detail;
    if (typeof detail === "string" && detail.length <= 512) {
      return { message: detail, code: null };
    }
    if (isRecord(detail)) {
      const message = typeof detail.message === "string" && detail.message.length <= 512
        ? detail.message
        : fallback;
      const code = typeof detail.code === "string" && detail.code.length <= 128
        ? detail.code
        : null;
      return { message, code };
    }
    return { message: fallback, code: null };
  };

  const apiRequest = async (path, options = {}) => {
    if (!adminToken) throw new AdminApiError("管理员令牌未连接。请先连接控制面。", 401);
    if (typeof path !== "string" || !path.startsWith("/") || path.includes("?") || path.includes("#")) {
      throw new Error("管理台拒绝了无效 API 路径。");
    }
    const method = options.method || "GET";
    const encoded = options.body === undefined ? null : JSON.stringify(options.body);
    if (encoded !== null && encoded.length > MAX_MANIFEST_BYTES + 1024 * 1024) {
      throw new Error("请求内容超过管理台限制。");
    }
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${adminToken}`,
          ...(encoded === null ? {} : { "Content-Type": "application/json" }),
        },
        body: encoded,
      });
    } catch {
      throw new AdminApiError("无法连接 Control Plane。请检查网络和服务状态后重试。", 0);
    }
    const declared = response.headers.get("content-length");
    if (declared !== null) {
      const declaredSize = Number(declared);
      if (!Number.isInteger(declaredSize) || declaredSize < 0 || declaredSize > MAX_RESPONSE_BYTES) {
        throw new AdminApiError("Control Plane 响应大小无效。", 502);
      }
    }
    const mediaType = (response.headers.get("content-type") || "").split(";", 1)[0].trim();
    if (mediaType !== "application/json") {
      throw new AdminApiError("Control Plane 返回了不支持的响应类型。", 502);
    }
    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) {
      throw new AdminApiError("Control Plane 响应超过大小限制。", 502);
    }
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new AdminApiError("Control Plane 返回了无效 JSON。", 502);
    }
    if (!response.ok) {
      const parsed = parseApiError(payload, response.status);
      throw new AdminApiError(parsed.message, response.status, parsed.code);
    }
    return payload;
  };

  const requestId = (key) => {
    const existing = requestIds.get(key);
    if (existing) return existing;
    if (!globalThis.crypto || typeof globalThis.crypto.randomUUID !== "function") {
      throw new Error("当前浏览器无法生成安全请求标识，管理台已停止变更操作。");
    }
    const value = `admin_${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
    requestIds.set(key, value);
    return value;
  };

  const showMessage = (kind, text) => {
    elements.message.dataset.kind = kind;
    elements.message.setAttribute("role", kind === "error" ? "alert" : "status");
    elements.messageText.textContent = text;
    elements.message.hidden = false;
  };

  const clearMessage = () => {
    elements.message.hidden = true;
    elements.messageText.textContent = "";
    delete elements.message.dataset.kind;
  };

  const setBadge = (element, label, state) => {
    element.textContent = label;
    element.dataset.state = state.toLowerCase();
  };

  const clearCandidateProjection = () => {
    candidate = null;
    elements.candidateProjection.hidden = true;
    elements.gateRegion.hidden = true;
    elements.gateTableBody.replaceChildren();
    elements.rolloutReleaseId.value = "";
    setBadge(elements.candidateStatus, "尚未创建", "idle");
  };

  const clearRolloutProjection = () => {
    rollout = null;
    elements.rolloutProjection.hidden = true;
    elements.rolloutActions.hidden = true;
    setBadge(elements.rolloutStatus, "尚未创建", "idle");
  };

  const clearRollbackProjection = () => {
    rollback = null;
    elements.rollbackProjection.hidden = true;
    elements.rollbackActions.hidden = true;
    setBadge(elements.rollbackStatus, "尚未创建", "idle");
  };

  const clearProjection = () => {
    clearCandidateProjection();
    clearRolloutProjection();
    clearRollbackProjection();
  };

  const clearDistribution = () => {
    elements.distributionSummary.textContent = "连接控制面后读取当前分布。";
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "尚未加载";
    row.append(cell);
    elements.distributionTableBody.replaceChildren(row);
  };

  const clearSession = ({ clearWorkflow = true } = {}) => {
    adminToken = "";
    sessionConnected = false;
    requestIds.clear();
    elements.tokenInput.value = "";
    elements.sessionLabel.textContent = "未连接";
    if (clearWorkflow) {
      manifest = null;
      manifestSha256 = null;
      elements.manifestFile.value = "";
      elements.manifestHelp.textContent = "仅接受不超过 16 MiB 的 JSON；文件只在本地解析后提交给 Control Plane。";
      clearProjection();
      clearDistribution();
      byId("canary-kill-state").textContent = "尚未返回";
      byId("stable-kill-state").textContent = "尚未返回";
      channelStates.set("canary", "unknown");
      channelStates.set("stable", "unknown");
    }
    syncControls();
  };

  const syncControls = () => {
    const hasToken = adminToken.length > 0;
    const connected = hasToken && sessionConnected;
    elements.connectButton.disabled = busy;
    elements.refreshStateButton.disabled = !hasToken || busy;
    elements.tokenInput.disabled = busy;
    elements.manifestFile.disabled = busy;
    elements.clearTokenButton.disabled = !hasToken || busy;
    elements.refreshDistributionButton.disabled = !connected || busy;
    elements.createCandidateButton.disabled =
      !connected || !manifest || !manifestSha256 || busy;
    elements.publishButton.disabled = !connected
      || !candidate
      || candidate.missing_gates.length > 0
      || candidate.status === "published"
      || busy;
    elements.createRolloutButton.disabled = !connected
      || !candidate
      || candidate.status !== "published"
      || busy;
    elements.createRollbackButton.disabled = !connected || busy;
    for (const button of document.querySelectorAll("[data-rollout-action], [data-rollback-action], [data-kill-action]")) {
      button.disabled = !connected
        || busy
        || (button.hasAttribute("data-rollout-action") && !rollout)
        || (button.hasAttribute("data-rollback-action") && !rollback);
    }
  };

  const withBusy = async (button, busyLabel, operation) => {
    if (busy) return;
    busy = true;
    clearMessage();
    const originalLabel = button.textContent;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyLabel;
    delete button.dataset.state;
    syncControls();
    try {
      await operation();
      button.dataset.state = "success";
      window.setTimeout(() => delete button.dataset.state, 1500);
    } catch (error) {
      button.dataset.state = "error";
      const message = error instanceof Error && error.message
        ? error.message
        : "管理操作未完成。请检查 Control Plane 后重试。";
      showMessage("error", message);
      if (error instanceof AdminApiError && (error.status === 401 || error.status === 403)) {
        clearSession();
        showMessage("error", `${message} 管理员令牌已从页面内存清除，请重新连接。`);
      }
    } finally {
      busy = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
      syncControls();
    }
  };

  const askConfirmation = ({ title, description, confirmLabel, danger = false, operation }) => {
    if (busy || typeof operation !== "function") return;
    pendingConfirmation = operation;
    elements.confirmTitle.textContent = title;
    elements.confirmDescription.textContent = description;
    elements.confirmSubmit.textContent = confirmLabel;
    elements.confirmSubmit.className = danger ? "button danger" : "button primary";
    elements.confirmDialog.showModal();
    elements.confirmCancel.focus();
  };

  const renderCandidate = (projection) => {
    candidate = projection;
    setBadge(elements.candidateStatus, projection.status, projection.status);
    elements.candidateReleaseId.textContent = projection.release_id;
    elements.candidateVersion.textContent = projection.version;
    elements.candidateChannel.textContent = projection.channel;
    elements.candidateDigest.textContent = projection.build_digest;
    elements.candidateProjection.hidden = false;
    elements.rolloutReleaseId.value = projection.release_id;

    const missing = new Set(projection.missing_gates);
    const gates = Object.entries(projection.gates).sort(([left], [right]) => left.localeCompare(right));
    const passed = gates.filter(([, status]) => status === "passed").length;
    elements.gateSummary.textContent = `${passed}/${gates.length} 已通过 · ${missing.size} 项仍缺失 · 由签名 Candidate 门禁包导入`;
    const rows = gates.map(([gateName, gateStatus]) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("th");
      nameCell.scope = "row";
      nameCell.textContent = gateName;

      const currentCell = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "status-badge";
      badge.textContent = gateStatus;
      badge.dataset.state = gateStatus.toLowerCase();
      currentCell.append(badge);

      row.append(nameCell, currentCell);
      return row;
    });
    elements.gateTableBody.replaceChildren(...rows);
    elements.gateRegion.hidden = false;
    syncControls();
  };

  const renderRollout = (projection) => {
    rollout = projection;
    setBadge(elements.rolloutStatus, projection.status, projection.status);
    elements.rolloutId.textContent = projection.rollout_id;
    elements.rolloutRelease.textContent = projection.release_id;
    elements.rolloutPercent.textContent = `${projection.percentage}%`;
    elements.rolloutCreatedAt.textContent = projection.created_at;
    elements.rolloutProjection.hidden = false;
    elements.rolloutActions.hidden = false;
    syncControls();
  };

  const renderRollback = (projection) => {
    rollback = projection;
    setBadge(elements.rollbackStatus, projection.status, projection.status);
    elements.rollbackId.textContent = projection.rollback_id;
    elements.rollbackSource.textContent = projection.source_release_id;
    elements.rollbackTarget.textContent = projection.target_release_id;
    elements.rollbackPercent.textContent = `${projection.percentage}%`;
    elements.rollbackAuthTtl.textContent = `${projection.authorization_ttl_seconds} 秒`;
    elements.rollbackProjection.hidden = false;
    elements.rollbackActions.hidden = false;
    syncControls();
  };

  const renderKillSwitch = (projection) => {
    channelStates.set(projection.channel, projection.kill_switch_active);
    const target = byId(`${projection.channel}-kill-state`);
    target.textContent = projection.kill_switch_active
      ? `已启用；停止 ${projection.halted_rollout_ids.length} 个灰度`
      : "未启用";
  };

  const renderDistribution = (projection) => {
    elements.distributionSummary.textContent = `当前共 ${projection.total_clients} 个客户端。`;
    const rows = [];
    for (const [value, count] of projection.versions.sort(([left], [right]) => left.localeCompare(right))) {
      const row = document.createElement("tr");
      for (const text of ["版本", value, String(count)]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      }
      rows.push(row);
    }
    for (const [value, count] of projection.update_states.sort(([left], [right]) => left.localeCompare(right))) {
      const row = document.createElement("tr");
      for (const text of ["更新状态", value, String(count)]) {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      }
      rows.push(row);
    }
    if (rows.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "API 当前未返回客户端版本或更新状态。";
      row.append(cell);
      rows.push(row);
    }
    elements.distributionTableBody.replaceChildren(...rows);
  };

  const renderResume = (projection) => {
    if (projection.latest_candidate_id === null) {
      clearCandidateProjection();
    } else {
      const selectedCandidate = projection.candidates.find(
        (item) => item.release_id === projection.latest_candidate_id,
      );
      renderCandidate(selectedCandidate);
    }
    if (projection.latest_rollout_id === null) {
      clearRolloutProjection();
    } else {
      const selectedRollout = projection.rollouts.find(
        (item) => item.rollout_id === projection.latest_rollout_id,
      );
      renderRollout(selectedRollout);
    }
    for (const killSwitch of projection.channel_kill_switches) {
      renderKillSwitch(killSwitch);
    }
    renderDistribution(projection.distribution);
    elements.sessionLabel.textContent = `已连接 · ${projection.captured_at}`;
    syncControls();
  };

  const refreshResume = (button = elements.refreshStateButton) => withBusy(
    button,
    "正在恢复",
    async () => {
      sessionConnected = false;
      elements.sessionLabel.textContent = "正在连接";
      try {
        const payload = await apiRequest("/resume");
        const projection = normalizeResume(payload);
        sessionConnected = true;
        renderResume(projection);
      } catch (error) {
        sessionConnected = false;
        elements.sessionLabel.textContent = "未连接 · 状态恢复失败";
        throw error;
      }
    },
  );

  const refreshDistribution = (button = elements.refreshDistributionButton) => withBusy(
    button,
    "正在读取",
    async () => {
      const payload = await apiRequest("/distribution");
      renderDistribution(normalizeDistribution(payload));
      elements.sessionLabel.textContent = "已连接";
    },
  );

  const parseTargetList = (value, label) => {
    const values = value.split(/[\r\n,]+/u).map((entry) => entry.trim()).filter(Boolean);
    if (values.length > 500 || values.some((entry) => entry.length > 256)) {
      throw new Error(`${label} 最多 500 个，且每项不能超过 256 个字符。`);
    }
    return values;
  };

  elements.authForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const token = elements.tokenInput.value.trim();
    if (!/^[\x21-\x7e]{24,4096}$/u.test(token)) {
      elements.tokenInput.setAttribute("aria-invalid", "true");
      showMessage("error", "管理员令牌格式无效。请输入 24–4096 个可打印 ASCII 字符。令牌未被保存。");
      elements.tokenInput.focus();
      return;
    }
    elements.tokenInput.removeAttribute("aria-invalid");
    adminToken = token;
    sessionConnected = false;
    elements.tokenInput.value = "";
    void refreshResume(elements.connectButton);
    syncControls();
  });

  elements.refreshStateButton.addEventListener("click", () => {
    void refreshResume();
  });

  elements.clearTokenButton.addEventListener("click", () => {
    clearSession();
    clearMessage();
    elements.tokenInput.focus();
  });

  elements.dismissMessage.addEventListener("click", clearMessage);

  elements.manifestFile.addEventListener("change", async () => {
    const generation = ++fileReadGeneration;
    const file = elements.manifestFile.files?.[0] || null;
    manifest = null;
    manifestSha256 = null;
    clearProjection();
    if (!file) {
      elements.manifestHelp.textContent = "请选择 Release Manifest JSON。";
      syncControls();
      return;
    }
    if (file.size < 1 || file.size > MAX_MANIFEST_BYTES) {
      elements.manifestFile.setAttribute("aria-invalid", "true");
      elements.manifestHelp.textContent = "Manifest 为空或超过 16 MiB，请重新选择。";
      showMessage("error", "Manifest 未载入：文件为空或超过 16 MiB。请选择有效 JSON。");
      syncControls();
      return;
    }
    try {
      const bytes = await file.arrayBuffer();
      const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      const digest = await sha256Hex(bytes);
      if (generation !== fileReadGeneration) return;
      if (!isRecord(parsed)) throw new Error("Manifest 顶层必须是 JSON 对象。");
      manifest = parsed;
      manifestSha256 = digest;
      elements.manifestFile.removeAttribute("aria-invalid");
      const releaseId = typeof parsed.release_id === "string" ? parsed.release_id : "等待 API 校验";
      const version = typeof parsed.version === "string" ? parsed.version : "未知版本";
      elements.manifestHelp.textContent = `已载入 ${releaseId} · ${version}；创建前仍由 Control Plane 完整校验签名与合同。`;
      clearMessage();
    } catch (error) {
      if (generation !== fileReadGeneration) return;
      elements.manifestFile.setAttribute("aria-invalid", "true");
      const reason = error instanceof Error ? error.message : "JSON 无效";
      elements.manifestHelp.textContent = "Manifest 未载入。";
      showMessage("error", `Manifest 解析失败：${reason} 请修复 JSON 后重新选择。`);
    }
    syncControls();
  });

  elements.manifestForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!manifest || !manifestSha256) return;
    const releaseId = typeof manifest.release_id === "string" ? manifest.release_id : "未标识候选";
    askConfirmation({
      title: "确认创建发布候选",
      description: `将 ${releaseId} 提交给 Control Plane 校验。相同请求会复用稳定请求标识。`,
      confirmLabel: "创建候选",
      operation: () => withBusy(elements.createCandidateButton, "正在创建", async () => {
        const fingerprint = [manifest.release_id, manifest.version, manifest.build_digest].map(String).join(":");
        const payload = await apiRequest("/releases", {
          method: "POST",
          body: {
            manifest,
            manifest_sha256: manifestSha256,
            client_request_id: requestId(`candidate:${fingerprint}`),
          },
        });
        renderCandidate(normalizeCandidate(payload));
      }),
    });
  });

  elements.publishButton.addEventListener("click", () => {
    if (!candidate) return;
    askConfirmation({
      title: "确认发布候选",
      description: `${candidate.release_id} 将进入已发布状态。Control Plane 会再次校验全部门禁。`,
      confirmLabel: "发布候选",
      operation: () => withBusy(elements.publishButton, "正在发布", async () => {
        const releaseId = safeSegment(candidate.release_id);
        const payload = await apiRequest(`/releases/${releaseId}/publish`, {
          method: "POST",
          body: { client_request_id: requestId(`publish:${candidate.release_id}`) },
        });
        renderCandidate(normalizeCandidate(payload));
      }),
    });
  });

  elements.rolloutForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!candidate) return;
    try {
      const percentage = Number(elements.rolloutPercentage.value);
      if (!Number.isInteger(percentage) || percentage < 1 || percentage > 100) {
        elements.rolloutPercentage.setAttribute("aria-invalid", "true");
        throw new Error("灰度比例必须是 1–100 的整数。");
      }
      elements.rolloutPercentage.removeAttribute("aria-invalid");
      const organizations = parseTargetList(elements.targetOrganizations.value, "组织 ID");
      const accounts = parseTargetList(elements.targetAccounts.value, "账号 ID");
      const minimum = elements.minimumVersion.value.trim() || null;
      const body = {
        release_id: candidate.release_id,
        percentage,
        target_organization_ids: organizations,
        target_account_ids: accounts,
        minimum_compatible_version: minimum,
      };
      askConfirmation({
        title: "确认创建灰度",
        description: `灰度比例 ${percentage}%，目标组织 ${organizations.length} 个、账号 ${accounts.length} 个。创建后不会自动激活。`,
        confirmLabel: "创建灰度",
        operation: () => withBusy(elements.createRolloutButton, "正在创建", async () => {
          const fingerprint = JSON.stringify(body);
          const payload = await apiRequest("/rollouts", {
            method: "POST",
            body: {
              ...body,
              client_request_id: requestId(`rollout:${fingerprint}`),
            },
          });
          renderRollout(normalizeRollout(payload));
        }),
      });
    } catch (error) {
      showMessage("error", error instanceof Error ? error.message : "灰度目标无效，请检查后重试。");
    }
  });

  for (const button of document.querySelectorAll("[data-rollout-action]")) {
    button.addEventListener("click", () => {
      if (!rollout) return;
      const action = button.dataset.rolloutAction;
      if (!new Set(["activate", "pause", "halt"]).has(action)) return;
      const labels = {
        activate: ["确认激活灰度", "激活灰度", false],
        pause: ["确认暂停灰度", "暂停灰度", false],
        halt: ["确认终止灰度", "终止灰度", true],
      };
      const [title, confirmLabel, danger] = labels[action];
      const sourceStatus = rollout.status;
      askConfirmation({
        title,
        description: `${rollout.rollout_id} 将执行 ${action}；最终状态由 Control Plane 返回。`,
        confirmLabel,
        danger,
        operation: () => withBusy(button, "正在提交", async () => {
          const rolloutId = safeSegment(rollout.rollout_id);
          const payload = await apiRequest(`/rollouts/${rolloutId}/${action}`, {
            method: "POST",
            body: { client_request_id: requestId(`rollout-action:${rollout.rollout_id}:${sourceStatus}:${action}`) },
          });
          renderRollout(normalizeRollout(payload));
        }),
      });
    });
  }

  elements.rollbackForm.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const sourceReleaseId = elements.rollbackSourceReleaseId.value.trim();
      const targetReleaseId = elements.rollbackTargetReleaseId.value.trim();
      const percentage = Number(elements.rollbackPercentage.value);
      const ttl = Number(elements.rollbackTtl.value);
      if (!SAFE_SEGMENT.test(sourceReleaseId) || !SAFE_SEGMENT.test(targetReleaseId)) {
        throw new Error("当前与目标 Release ID 必须使用安全标识。");
      }
      if (!Number.isInteger(percentage) || percentage < 1 || percentage > 100) {
        throw new Error("回滚比例必须是 1–100 的整数。");
      }
      if (!Number.isInteger(ttl) || ttl < 60 || ttl > 900) {
        throw new Error("回滚授权有效期必须介于 60–900 秒。");
      }
      const organizations = parseTargetList(elements.rollbackOrganizations.value, "组织 ID");
      const accounts = parseTargetList(elements.rollbackAccounts.value, "账号 ID");
      const body = {
        source_release_id: sourceReleaseId,
        target_release_id: targetReleaseId,
        percentage,
        target_organization_ids: organizations,
        target_account_ids: accounts,
        authorization_ttl_seconds: ttl,
      };
      askConfirmation({
        title: "确认创建紧急回滚",
        description: `${sourceReleaseId} 将回滚至 ${targetReleaseId}，比例 ${percentage}%。Control Plane 会复验已知良好记录与平台矩阵。`,
        confirmLabel: "创建回滚",
        danger: true,
        operation: () => withBusy(elements.createRollbackButton, "正在创建", async () => {
          const fingerprint = JSON.stringify(body);
          const payload = await apiRequest("/rollbacks", {
            method: "POST",
            body: {
              ...body,
              client_request_id: requestId(`rollback:${fingerprint}`),
            },
          });
          renderRollback(normalizeRollback(payload));
        }),
      });
    } catch (error) {
      showMessage("error", error instanceof Error ? error.message : "回滚目标无效，请检查后重试。");
    }
  });

  for (const button of document.querySelectorAll("[data-rollback-action]")) {
    button.addEventListener("click", () => {
      if (!rollback) return;
      const action = button.dataset.rollbackAction;
      if (!new Set(["activate", "pause", "halt"]).has(action)) return;
      const labels = {
        activate: ["确认激活回滚", "激活回滚", true],
        pause: ["确认暂停回滚", "暂停回滚", false],
        halt: ["确认终止回滚", "终止回滚", false],
      };
      const [title, confirmLabel, danger] = labels[action];
      const sourceStatus = rollback.status;
      askConfirmation({
        title,
        description: `${rollback.rollback_id} 将执行 ${action}；客户端仅会接受短时、签名且绑定当前 build 的回滚授权。`,
        confirmLabel,
        danger,
        operation: () => withBusy(button, "正在提交", async () => {
          const rollbackId = safeSegment(rollback.rollback_id);
          const payload = await apiRequest(`/rollbacks/${rollbackId}/${action}`, {
            method: "POST",
            body: { client_request_id: requestId(`rollback-action:${rollback.rollback_id}:${sourceStatus}:${action}`) },
          });
          renderRollback(normalizeRollback(payload));
        }),
      });
    });
  }

  for (const button of document.querySelectorAll("[data-kill-action]")) {
    button.addEventListener("click", () => {
      const channel = button.dataset.channel;
      const action = button.dataset.killAction;
      if (!new Set(["stable", "canary"]).has(channel) || !new Set(["set", "clear"]).has(action)) return;
      const setting = action === "set";
      askConfirmation({
        title: setting ? `确认停止 ${channel} 通道` : `确认清除 ${channel} 止损`,
        description: setting
          ? "该通道的活跃灰度会被停止；客户端仍以签名发布源为最终权威。"
          : "清除后不会自动恢复灰度；后续激活仍由 Control Plane 校验。",
        confirmLabel: setting ? "设置 Kill switch" : "清除 Kill switch",
        danger: setting,
        operation: () => withBusy(button, "正在提交", async () => {
          const suffix = setting ? "kill-switch" : "kill-switch/clear";
          const sourceState = channelStates.get(channel) || "unknown";
          const payload = await apiRequest(`/channels/${safeSegment(channel)}/${suffix}`, {
            method: "POST",
            body: { client_request_id: requestId(`kill:${channel}:${sourceState}:${action}`) },
          });
          const projection = normalizeKillSwitch(payload);
          renderKillSwitch(projection);
        }),
      });
    });
  }

  elements.refreshDistributionButton.addEventListener("click", () => {
    void refreshDistribution();
  });

  elements.confirmCancel.addEventListener("click", () => {
    pendingConfirmation = null;
    elements.confirmDialog.close("cancel");
  });

  elements.confirmSubmit.addEventListener("click", () => {
    const operation = pendingConfirmation;
    pendingConfirmation = null;
    elements.confirmDialog.close("confirm");
    if (operation) void operation();
  });

  elements.confirmDialog.addEventListener("cancel", () => {
    pendingConfirmation = null;
  });

  window.addEventListener("beforeunload", () => {
    adminToken = "";
    requestIds.clear();
  });

  clearProjection();
  clearDistribution();
  syncControls();
})();
