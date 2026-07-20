"use strict";

(() => {
  const API_BASE = "/api/v1/admin";
  const MAX_MANIFEST_BYTES = 16 * 1024 * 1024;
  const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
  const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$/;

  let adminToken = "";
  let adminRefreshToken = "";
  let adminLeaseId = "";
  let adminAccessExpiresAt = 0;
  let authGeneration = 0;
  let refreshPromise = null;
  let manifest = null;
  let manifestSha256 = null;
  let candidate = null;
  let rollout = null;
  let rollback = null;
  let users = [];
  let models = [];
  let editingUser = null;
  let editingModel = null;
  let userPasswordInputRevision = 0;
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
    loginIdentifier: byId("admin-identifier"),
    loginPassword: byId("admin-password"),
    loginButton: byId("login-button"),
    refreshStateButton: byId("refresh-state-button"),
    clearTokenButton: byId("clear-token-button"),
    sessionLabel: byId("session-label"),
    message: byId("global-message"),
    messageText: byId("global-message-text"),
    dismissMessage: byId("dismiss-message-button"),
    userFilterForm: byId("user-filter-form"),
    userQuery: byId("user-query"),
    userStatusFilter: byId("user-status-filter"),
    userOrganizationFilter: byId("user-organization-filter"),
    filterUsersButton: byId("filter-users-button"),
    createUserButton: byId("create-user-button"),
    userListSummary: byId("user-list-summary"),
    userTableBody: byId("user-table-body"),
    userDialog: byId("user-dialog"),
    userForm: byId("user-form"),
    userDialogTitle: byId("user-dialog-title"),
    userAccountId: byId("user-account-id"),
    userDisplayName: byId("user-display-name"),
    userEmail: byId("user-email"),
    userOrganization: byId("user-organization"),
    userStatus: byId("user-status"),
    userTokenLimit: byId("user-token-limit"),
    userImageLimit: byId("user-image-limit"),
    userPassword: byId("user-password"),
    userPasswordLabel: byId("user-password-label"),
    userPasswordHelp: byId("user-password-help"),
    userCredentialStatus: byId("user-credential-status"),
    userCancelButton: byId("user-cancel-button"),
    userSaveButton: byId("user-save-button"),
    usageUsers: byId("usage-users"),
    usageTokens: byId("usage-tokens"),
    usageImages: byId("usage-images"),
    usageCapturedAt: byId("usage-captured-at"),
    refreshUsageButton: byId("refresh-usage-button"),
    usageDialog: byId("usage-dialog"),
    usageForm: byId("usage-form"),
    usageAccountId: byId("usage-account-id"),
    usageUserRevision: byId("usage-user-revision"),
    usageTokenDelta: byId("usage-token-delta"),
    usageImageDelta: byId("usage-image-delta"),
    usageReason: byId("usage-reason"),
    usageCancelButton: byId("usage-cancel-button"),
    usageSaveButton: byId("usage-save-button"),
    createModelButton: byId("create-model-button"),
    modelListSummary: byId("model-list-summary"),
    modelTableBody: byId("model-table-body"),
    modelDialog: byId("model-dialog"),
    modelForm: byId("model-form"),
    modelDialogTitle: byId("model-dialog-title"),
    modelConfigId: byId("model-config-id"),
    modelActiveRevision: byId("model-active-revision"),
    modelModality: byId("model-modality"),
    modelLocalId: byId("model-local-id"),
    modelDisplayName: byId("model-display-name"),
    modelUpstreamId: byId("model-upstream-id"),
    modelProvider: byId("model-provider"),
    modelApiKey: byId("model-api-key"),
    modelDefault: byId("model-default"),
    modelEnabled: byId("model-enabled"),
    modelCancelButton: byId("model-cancel-button"),
    modelSaveButton: byId("model-save-button"),
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
    rolloutMode: byId("rollout-mode"),
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

  const boundedInteger = (value, field, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) => {
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
      throw new Error(`Control Plane 返回了无效数值：${field}`);
    }
    return number;
  };

  const normalizeUser = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的用户合同无效。");
    const status = requiredString(value.status, "status");
    const credentialState = requiredString(value.credential_state, "credential_state");
    if (!new Set(["active", "suspended"]).has(status)) {
      throw new Error("Control Plane 返回了未知用户状态。");
    }
    if (typeof value.password_configured !== "boolean"
      || !new Set(["configured", "missing"]).has(credentialState)
      || value.password_configured !== (credentialState === "configured")) {
      throw new Error("Control Plane 返回了无效登录凭据状态。");
    }
    return {
      account_id: requiredString(value.account_id, "account_id"),
      display_name: requiredString(value.display_name, "display_name"),
      email: optionalString(value.email, "email"),
      organization_id: optionalString(value.organization_id, "organization_id"),
      status,
      token_limit: boundedInteger(value.token_limit, "token_limit", 0, 10 ** 12),
      tokens_used: boundedInteger(value.tokens_used, "tokens_used", 0, 10 ** 12),
      image_limit: boundedInteger(value.image_limit, "image_limit", 0, 10 ** 9),
      images_used: boundedInteger(value.images_used, "images_used", 0, 10 ** 9),
      password_configured: value.password_configured,
      credential_state: credentialState,
      password_changed_at: optionalString(value.password_changed_at, "password_changed_at"),
      revision: boundedInteger(value.revision, "revision", 1),
      created_at: requiredString(value.created_at, "created_at"),
      updated_at: requiredString(value.updated_at, "updated_at"),
    };
  };

  const normalizeUserList = (value) => {
    if (!isRecord(value) || !Array.isArray(value.items) || value.items.length > 200) {
      throw new Error("Control Plane 返回的用户列表无效。");
    }
    const items = value.items.map(normalizeUser);
    if (new Set(items.map((item) => item.account_id)).size !== items.length) {
      throw new Error("Control Plane 返回了重复用户。");
    }
    return {
      items,
      total: boundedInteger(value.total, "total", items.length),
      offset: boundedInteger(value.offset, "offset"),
      limit: boundedInteger(value.limit, "limit", 1, 200),
    };
  };

  const normalizeUsageSummary = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的用量合同无效。");
    return {
      users_total: boundedInteger(value.users_total, "users_total"),
      users_active: boundedInteger(value.users_active, "users_active"),
      token_limit: boundedInteger(value.token_limit, "token_limit", 0, 10 ** 15),
      tokens_used: boundedInteger(value.tokens_used, "tokens_used", 0, 10 ** 15),
      image_limit: boundedInteger(value.image_limit, "image_limit", 0, 10 ** 12),
      images_used: boundedInteger(value.images_used, "images_used", 0, 10 ** 12),
      captured_at: requiredString(value.captured_at, "captured_at"),
    };
  };

  const normalizeModelRevision = (value) => {
    if (!isRecord(value) || Object.hasOwn(value, "api_key")) {
      throw new Error("Control Plane 返回的模型修订无效。");
    }
    const modality = requiredString(value.modality, "modality");
    const provider = requiredString(value.provider_preset, "provider_preset");
    const status = requiredString(value.status, "status");
    const testStatus = requiredString(value.test_status, "test_status");
    if (!new Set(["chat", "image_generation", "image_edit"]).has(modality)
      || !new Set(["responses", "openai_compatible_chat", "openai_compatible_image"]).has(provider)
      || !new Set(["draft", "testing", "active", "rejected", "superseded"]).has(status)
      || !new Set(["not_tested", "running", "passed", "failed"]).has(testStatus)) {
      throw new Error("Control Plane 返回了未知模型状态。");
    }
    if (typeof value.is_default !== "boolean" || typeof value.enabled !== "boolean"
      || typeof value.key_configured !== "boolean") {
      throw new Error("Control Plane 返回了无效模型开关。");
    }
    return {
      config_id: requiredString(value.config_id, "config_id"),
      revision: boundedInteger(value.revision, "revision", 1),
      local_model_id: requiredString(value.local_model_id, "local_model_id"),
      modality,
      display_name: requiredString(value.display_name, "display_name"),
      upstream_model_id: requiredString(value.upstream_model_id, "upstream_model_id"),
      provider_preset: provider,
      is_default: value.is_default,
      enabled: value.enabled,
      status,
      key_configured: value.key_configured,
      key_fingerprint: optionalString(value.key_fingerprint, "key_fingerprint"),
      test_id: optionalString(value.test_id, "test_id"),
      test_status: testStatus,
      test_error_code: optionalString(value.test_error_code, "test_error_code"),
      tested_at: optionalString(value.tested_at, "tested_at"),
      created_at: requiredString(value.created_at, "created_at"),
      updated_at: requiredString(value.updated_at, "updated_at"),
    };
  };

  const normalizeModelConfiguration = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的模型配置无效。");
    const configId = requiredString(value.config_id, "config_id");
    const active = value.active === null ? null : normalizeModelRevision(value.active);
    const draft = value.draft === null ? null : normalizeModelRevision(value.draft);
    if (active === null && draft === null) {
      throw new Error("Control Plane 返回了空模型配置。");
    }
    if ((active && active.config_id !== configId) || (draft && draft.config_id !== configId)) {
      throw new Error("Control Plane 返回了错配的模型修订。");
    }
    return { config_id: configId, active, draft };
  };

  const normalizeModelList = (value) => {
    if (!Array.isArray(value) || value.length > 100) {
      throw new Error("Control Plane 返回的模型列表无效。");
    }
    const items = value.map(normalizeModelConfiguration);
    if (new Set(items.map((item) => item.config_id)).size !== items.length) {
      throw new Error("Control Plane 返回了重复模型配置。");
    }
    return items;
  };

  const normalizeModelTest = (value) => {
    if (!isRecord(value)) throw new Error("Control Plane 返回的模型测试合同无效。");
    const status = requiredString(value.status, "status");
    if (!new Set(["running", "passed", "failed", "superseded"]).has(status)) {
      throw new Error("Control Plane 返回了未知测试状态。");
    }
    return {
      test_id: requiredString(value.test_id, "test_id"),
      config_id: requiredString(value.config_id, "config_id"),
      revision: boundedInteger(value.revision, "revision", 1),
      status,
      error_code: optionalString(value.error_code, "error_code"),
      active_revision: value.active_revision === null
        ? null
        : boundedInteger(value.active_revision, "active_revision", 1),
      completed_at: optionalString(value.completed_at, "completed_at"),
    };
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

  const deviceRequest = async (path, body, idempotencyKey) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(body),
    });
    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) throw new Error("登录响应超过大小限制。");
    let payload;
    try { payload = JSON.parse(text); } catch { throw new Error("登录服务返回无效数据。"); }
    if (!response.ok) {
      const parsed = parseApiError(payload, response.status);
      throw new AdminApiError(parsed.message, response.status, parsed.code);
    }
    return payload;
  };

  const accessExpiry = (token) => {
    try {
      const segments = token.split(".");
      if (segments.length !== 3 || segments[1].length > 16384) throw new Error();
      const normalized = segments[1].replaceAll("-", "+").replaceAll("_", "/");
      const claims = JSON.parse(atob(normalized + "=".repeat((4 - normalized.length % 4) % 4)));
      if (!Number.isSafeInteger(claims.exp) || claims.exp <= 0) throw new Error();
      return claims.exp * 1000;
    } catch {
      throw new Error("Control Plane access token 缺少有效到期时间。");
    }
  };

  const installDeviceGrant = (payload) => {
    if (!isRecord(payload) || payload.status !== "authorized" || !isRecord(payload.lease)
      || !isRecord(payload.lease.claims) || typeof payload.access_token !== "string"
      || typeof payload.refresh_token !== "string" || typeof payload.lease.claims.lease_id !== "string") {
      throw new Error("Control Plane 登录结果无效。");
    }
    adminToken = payload.access_token;
    adminRefreshToken = payload.refresh_token;
    adminLeaseId = payload.lease.claims.lease_id;
    adminAccessExpiresAt = accessExpiry(adminToken);
  };

  const refreshDeviceGrant = async () => {
    if (!adminRefreshToken || !adminLeaseId) return;
    if (refreshPromise) return refreshPromise;
    const generation = authGeneration;
    refreshPromise = (async () => {
      try {
        const payload = await deviceRequest(
          "/v1/device/token",
          {
            schema_version: 1,
            client_id: "ecorex-admin-web",
            grant_type: "refresh_token",
            lease_id: adminLeaseId,
            refresh_token: adminRefreshToken,
          },
          `admin-refresh:${adminLeaseId}`,
        );
        if (generation !== authGeneration) return;
        installDeviceGrant(payload);
      } catch (error) {
        if (generation === authGeneration && error instanceof AdminApiError
          && (error.code === "invalid_grant" || error.status === 401)) {
          clearSession();
          showMessage("error", "管理员会话已失效，请重新登录。");
        }
        throw error;
      } finally {
        refreshPromise = null;
      }
    })();
    return refreshPromise;
  };

  const ensureFreshToken = async () => {
    if (adminRefreshToken && adminAccessExpiresAt - Date.now() <= 60_000) {
      await refreshDeviceGrant();
    }
  };

  const passwordLogin = async () => {
    const identifier = elements.loginIdentifier.value.trim();
    let password = elements.loginPassword.value;
    if (!identifier || !password) {
      throw new Error("请输入管理员账号和密码。");
    }
    const generation = ++authGeneration;
    clearSession({ clearWorkflow: false, preserveGeneration: true });
    const requestId = `admin-password-login:${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
    try {
      const payload = await deviceRequest(
        "/v1/session/login",
        {
          schema_version: 1,
          client_id: "ecorex-admin-web",
          identifier,
          password,
        },
        requestId,
      );
      if (generation !== authGeneration) return;
      installDeviceGrant(payload);
      elements.loginPassword.value = "";
      // ``passwordLogin`` already owns the busy state.  Calling the public
      // button wrapper here would see that lock and silently skip the first
      // authenticated state load, leaving a valid session labelled as
      // disconnected.
      await restoreAdminResume();
      syncControls();
    } finally {
      // Never retain a password in a live form after a network outcome.
      password = "";
    }
  };

  const apiRequest = async (path, options = {}) => {
    if (!adminToken) throw new AdminApiError("管理员尚未登录。请先登录控制面。", 401);
    await ensureFreshToken();
    if (typeof path !== "string" || !path.startsWith("/") || path.includes("?") || path.includes("#")) {
      throw new Error("管理台拒绝了无效 API 路径。");
    }
    const method = options.method || "GET";
    const queryEntries = options.query === undefined
      ? []
      : Object.entries(options.query).filter(([, value]) => value !== null && value !== undefined && value !== "");
    if (queryEntries.length > 16 || queryEntries.some(([key, value]) => (
      !/^[a-z][a-z0-9_]{0,63}$/u.test(key)
      || typeof value !== "string"
      || value.length > 256
    ))) {
      throw new Error("管理台拒绝了无效筛选参数。");
    }
    const query = queryEntries.length === 0
      ? ""
      : "?" + queryEntries.map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&");
    const encoded = options.body === undefined ? null : JSON.stringify(options.body);
    if (encoded !== null && encoded.length > MAX_MANIFEST_BYTES + 1024 * 1024) {
      throw new Error("请求内容超过管理台限制。");
    }
    let response;
    try {
      response = await fetch(`${API_BASE}${path}${query}`, {
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

  const placeholderRow = (columns, text) => {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = columns;
    cell.textContent = text;
    row.append(cell);
    return row;
  };

  const clearManagement = () => {
    users = [];
    models = [];
    editingUser = null;
    editingModel = null;
    elements.userListSummary.textContent = "连接控制面后读取用户。";
    elements.userTableBody.replaceChildren(placeholderRow(6, "尚未加载"));
    elements.modelListSummary.textContent = "密钥不会返回页面；列表仅显示短指纹与测试状态。";
    elements.modelTableBody.replaceChildren(placeholderRow(6, "尚未加载"));
    elements.usageUsers.textContent = "—";
    elements.usageTokens.textContent = "—";
    elements.usageImages.textContent = "—";
    elements.usageCapturedAt.textContent = "—";
    for (const dialog of [elements.userDialog, elements.usageDialog, elements.modelDialog]) {
      if (dialog.open) dialog.close("session-cleared");
    }
  };

  const clearSession = ({ clearWorkflow = true, preserveGeneration = false } = {}) => {
    if (!preserveGeneration) authGeneration += 1;
    adminToken = "";
    adminRefreshToken = "";
    adminLeaseId = "";
    adminAccessExpiresAt = 0;
    refreshPromise = null;
    sessionConnected = false;
    requestIds.clear();
    elements.loginIdentifier.value = "";
    elements.loginPassword.value = "";
    elements.sessionLabel.textContent = "未连接";
    if (clearWorkflow) {
      manifest = null;
      manifestSha256 = null;
      elements.manifestFile.value = "";
      elements.manifestHelp.textContent = "仅接受不超过 16 MiB 的 JSON；文件只在本地解析后提交给 Control Plane。";
      clearProjection();
      clearDistribution();
      clearManagement();
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
    elements.loginButton.disabled = busy;
    elements.refreshStateButton.disabled = !hasToken || busy;
    elements.loginIdentifier.disabled = busy;
    elements.loginPassword.disabled = busy;
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
    for (const button of document.querySelectorAll("[data-session-control]")) {
      button.disabled = !connected || busy || button.dataset.controlLock === "true";
    }
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
        showMessage("error", `${message} 管理员会话已清除，请重新登录。`);
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

  const formatCount = (value) => new Intl.NumberFormat("zh-CN", {
    notation: value >= 1_000_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);

  const usageText = (used, limit) => `${formatCount(used)} / ${formatCount(limit)}`;

  const appendTextCell = (row, text, className = "") => {
    const cell = document.createElement("td");
    cell.textContent = text;
    if (className) cell.className = className;
    row.append(cell);
    return cell;
  };

  const openUserDialog = (user = null) => {
    editingUser = user;
    userPasswordInputRevision = 0;
    elements.userDialogTitle.textContent = user ? `编辑 ${user.display_name}` : "创建用户";
    elements.userAccountId.value = user?.account_id || "";
    elements.userAccountId.disabled = Boolean(user);
    elements.userDisplayName.value = user?.display_name || "";
    elements.userEmail.value = user?.email || "";
    elements.userOrganization.value = user?.organization_id || "";
    elements.userStatus.value = user?.status || "active";
    elements.userStatus.disabled = !user;
    elements.userTokenLimit.value = String(user?.token_limit ?? 0);
    elements.userImageLimit.value = String(user?.image_limit ?? 0);
    elements.userPassword.value = "";
    elements.userPassword.required = !user;
    elements.userPasswordLabel.textContent = user ? "重置密码（可选）" : "初始密码";
    elements.userPasswordHelp.textContent = user
      ? "留空表示保持原密码；输入至少 10 位的新密码会立即替换旧密码。"
      : "至少 10 位。创建后用户可直接使用账号或邮箱登录。";
    elements.userCredentialStatus.textContent = user
      ? (user.password_configured
        ? `当前已设置密码${user.password_changed_at ? `，更新于 ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(user.password_changed_at))}` : ""}。`
        : "当前未设置密码；请在本次保存时设置。")
      : "尚未创建登录凭据。";
    elements.userDialog.showModal();
    elements.userDisplayName.focus();
  };

  const openUsageDialog = (user) => {
    elements.usageAccountId.value = user.account_id;
    elements.usageUserRevision.value = String(user.revision);
    elements.usageTokenDelta.value = "0";
    elements.usageImageDelta.value = "0";
    elements.usageReason.value = "";
    elements.usageDialogTitle.textContent = `校正 ${user.display_name} 的用量`;
    elements.usageDialog.showModal();
    elements.usageTokenDelta.focus();
  };

  const renderUsers = (projection) => {
    users = projection.items;
    elements.userListSummary.textContent = `显示 ${projection.items.length} 位，共 ${projection.total} 位用户。`;
    const rows = projection.items.map((user) => {
      const row = document.createElement("tr");
      const identity = document.createElement("td");
      const name = document.createElement("strong");
      name.textContent = user.display_name;
      const detail = document.createElement("small");
      detail.textContent = `${user.email || user.account_id} · ${user.password_configured ? "密码已设置" : "密码未设置"}`;
      identity.append(name, detail);
      row.append(identity);
      appendTextCell(row, user.organization_id || "—");
      appendTextCell(row, usageText(user.tokens_used, user.token_limit), "number-cell");
      appendTextCell(row, usageText(user.images_used, user.image_limit), "number-cell");
      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      status.className = "status-badge";
      status.dataset.state = user.status;
      status.textContent = user.status === "active" ? "正常" : "已停用";
      statusCell.append(status);
      row.append(statusCell);
      const actions = document.createElement("td");
      actions.className = "table-actions compact-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "button compact";
      edit.textContent = "编辑";
      edit.dataset.sessionControl = "";
      edit.addEventListener("click", () => openUserDialog(user));
      const adjust = document.createElement("button");
      adjust.type = "button";
      adjust.className = "button compact";
      adjust.textContent = "校正用量";
      adjust.dataset.sessionControl = "";
      adjust.addEventListener("click", () => openUsageDialog(user));
      actions.append(edit, adjust);
      row.append(actions);
      return row;
    });
    elements.userTableBody.replaceChildren(
      ...(rows.length ? rows : [placeholderRow(6, "没有符合条件的用户。")]),
    );
  };

  const renderUsageSummary = (projection) => {
    elements.usageUsers.textContent = `${formatCount(projection.users_active)} / ${formatCount(projection.users_total)}`;
    elements.usageTokens.textContent = usageText(projection.tokens_used, projection.token_limit);
    elements.usageImages.textContent = usageText(projection.images_used, projection.image_limit);
    elements.usageCapturedAt.textContent = new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(projection.captured_at));
  };

  const modalityLabel = (modality) => ({
    chat: "主模型",
    image_generation: "生图模型",
    image_edit: "精修模型",
  })[modality] || "未知模型";

  const modelTestErrorLabel = (code) => ({
    provider_key_rejected: "Key 未通过服务商验证",
    provider_model_unavailable: "服务端未找到该模型名称",
    provider_test_unconfigured: "该接口类型尚未由平台允许",
    provider_test_timeout: "测试超时，请检查服务商状态",
    provider_test_unavailable: "暂时无法连接模型服务",
    provider_test_uncertain: "真实请求结果不确定，系统未自动重试或启用",
    provider_test_rate_limited: "模型服务限流，当前配置未启用",
    provider_test_protocol: "模型服务返回了不兼容的格式",
    provider_test_rejected: "模型服务拒绝了测试请求",
    provider_inference_rejected: "模型服务拒绝了真实推理请求",
    provider_test_cancelled: "测试已取消",
  })[code] || "模型测试未通过";

  const openModelDialog = (configuration = null) => {
    editingModel = configuration;
    const revision = configuration ? (configuration.draft || configuration.active) : null;
    elements.modelDialogTitle.textContent = configuration ? `编辑 ${revision.display_name}` : "添加模型";
    elements.modelConfigId.value = configuration?.config_id || "";
    elements.modelActiveRevision.value = configuration?.active ? String(configuration.active.revision) : "";
    elements.modelModality.value = revision?.modality || "chat";
    elements.modelLocalId.value = revision?.local_model_id || "";
    elements.modelModality.disabled = Boolean(configuration);
    elements.modelLocalId.disabled = Boolean(configuration);
    elements.modelDisplayName.value = revision?.display_name || "";
    elements.modelUpstreamId.value = revision?.upstream_model_id || "";
    elements.modelProvider.value = revision?.provider_preset || "responses";
    elements.modelDefault.checked = revision?.is_default ?? true;
    elements.modelEnabled.checked = revision?.enabled ?? true;
    elements.modelApiKey.value = "";
    elements.modelApiKey.required = !configuration;
    syncModelProvider();
    elements.modelDialog.showModal();
    elements.modelDisplayName.focus();
  };

  const renderModels = (items) => {
    models = items;
    elements.modelListSummary.textContent = `已配置 ${items.length} 个模型位。Key 只显示指纹，保存后不可读回。`;
    const rows = items.map((configuration) => {
      const revision = configuration.draft || configuration.active;
      const row = document.createElement("tr");
      appendTextCell(row, modalityLabel(revision.modality));
      const display = document.createElement("td");
      const name = document.createElement("strong");
      name.textContent = revision.display_name;
      const local = document.createElement("small");
      local.textContent = revision.local_model_id;
      display.append(name, local);
      row.append(display);
      appendTextCell(row, revision.upstream_model_id, "mono-cell");
      appendTextCell(row, revision.key_fingerprint ? `…${revision.key_fingerprint}` : "未配置", "mono-cell");
      const stateCell = document.createElement("td");
      const state = document.createElement("span");
      state.className = "status-badge";
      state.dataset.state = configuration.draft ? revision.test_status : revision.status;
      state.textContent = configuration.draft
        ? ({ not_tested: "待测试", running: "测试中", passed: "已通过", failed: "测试失败" })[revision.test_status]
        : "已生效";
      stateCell.append(state);
      if (revision.test_error_code) {
        const error = document.createElement("small");
        error.textContent = modelTestErrorLabel(revision.test_error_code);
        error.title = revision.test_error_code;
        stateCell.append(error);
      }
      row.append(stateCell);
      const actions = document.createElement("td");
      actions.className = "table-actions compact-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "button compact";
      edit.textContent = "编辑";
      edit.dataset.sessionControl = "";
      edit.addEventListener("click", () => openModelDialog(configuration));
      actions.append(edit);
      if (configuration.draft) {
        const activate = document.createElement("button");
        activate.type = "button";
        activate.className = "button compact primary";
        activate.textContent = revision.test_status === "running" ? "测试中" : "测试并启用";
        activate.dataset.sessionControl = "";
        activate.dataset.controlLock = revision.test_status === "running" ? "true" : "false";
        activate.addEventListener("click", () => {
          askConfirmation({
            title: `测试并启用 ${revision.display_name}？`,
            description: "系统会向对应服务发送一次真实请求，图片测试可能产生费用。只有返回可用结果时新修订才会生效；失败或结果不确定时保留线上配置且不会自动重试。",
            confirmLabel: "开始测试",
            operation: () => withBusy(activate, "测试中", async () => {
              const payload = await apiRequest(`/models/${safeSegment(configuration.config_id)}/test-and-activate`, {
                method: "POST",
                body: {
                  revision: revision.revision,
                  client_request_id: requestId(`model-test:${configuration.config_id}:${revision.revision}`),
                },
              });
              const result = normalizeModelTest(payload);
              if (result.status !== "passed") {
                throw new Error(result.error_code
                  ? modelTestErrorLabel(result.error_code)
                  : "模型测试未通过，现有配置未变更。");
              }
              requestIds.delete(`model-test:${configuration.config_id}:${revision.revision}`);
              await refreshModels();
              showMessage("info", `${revision.display_name} 已测试通过并对新请求生效。`);
            }),
          });
        });
        actions.append(activate);
      }
      row.append(actions);
      return row;
    });
    elements.modelTableBody.replaceChildren(
      ...(rows.length ? rows : [placeholderRow(6, "尚未配置托管模型。")]),
    );
  };

  const refreshUsers = async () => {
    const payload = await apiRequest("/users", {
      query: {
        query: elements.userQuery.value.trim(),
        status: elements.userStatusFilter.value,
        organization_id: elements.userOrganizationFilter.value.trim(),
        offset: "0",
        limit: "200",
      },
    });
    renderUsers(normalizeUserList(payload));
  };

  const refreshUsage = async () => {
    renderUsageSummary(normalizeUsageSummary(await apiRequest("/usage/summary")));
  };

  async function refreshModels() {
    renderModels(normalizeModelList(await apiRequest("/models")));
  }

  const refreshManagementData = async () => {
    const [userPayload, usagePayload, modelPayload] = await Promise.all([
      apiRequest("/users", { query: { offset: "0", limit: "200" } }),
      apiRequest("/usage/summary"),
      apiRequest("/models"),
    ]);
    renderUsers(normalizeUserList(userPayload));
    renderUsageSummary(normalizeUsageSummary(usagePayload));
    renderModels(normalizeModelList(modelPayload));
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

  const restoreAdminResume = async () => {
    sessionConnected = false;
    elements.sessionLabel.textContent = "正在连接";
    try {
      const payload = await apiRequest("/resume");
      const projection = normalizeResume(payload);
      sessionConnected = true;
      renderResume(projection);
      await refreshManagementData();
    } catch (error) {
      sessionConnected = false;
      elements.sessionLabel.textContent = "未连接 · 状态恢复失败";
      throw error;
    }
  };

  const refreshResume = (button = elements.refreshStateButton) => withBusy(
    button,
    "正在恢复",
    restoreAdminResume,
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
    if (!elements.loginIdentifier.value.trim() || !elements.loginPassword.value) {
      elements.loginIdentifier.setAttribute("aria-invalid", "true");
      elements.loginPassword.setAttribute("aria-invalid", "true");
      showMessage("error", "请输入管理员账号和密码。");
      elements.loginIdentifier.focus();
      return;
    }
    elements.loginIdentifier.removeAttribute("aria-invalid");
    elements.loginPassword.removeAttribute("aria-invalid");
    void withBusy(elements.loginButton, "登录中", passwordLogin);
  });

  elements.refreshStateButton.addEventListener("click", () => {
    void refreshResume();
  });

  elements.clearTokenButton.addEventListener("click", () => {
    clearSession();
    clearMessage();
    elements.loginIdentifier.focus();
  });

  elements.dismissMessage.addEventListener("click", clearMessage);

  elements.createUserButton.addEventListener("click", () => openUserDialog());

  elements.userFilterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void withBusy(elements.filterUsersButton, "筛选中", refreshUsers);
  });

  elements.userCancelButton.addEventListener("click", () => elements.userDialog.close("cancel"));
  elements.userDialog.addEventListener("close", () => {
    elements.userPassword.value = "";
  });
  elements.userPassword.addEventListener("input", () => {
    userPasswordInputRevision += 1;
  });

  elements.userForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const accountId = elements.userAccountId.value.trim();
    const displayName = elements.userDisplayName.value.trim();
    const email = elements.userEmail.value.trim() || null;
    const organizationId = elements.userOrganization.value.trim() || null;
    const tokenLimit = Number(elements.userTokenLimit.value);
    const imageLimit = Number(elements.userImageLimit.value);
    const password = elements.userPassword.value;
    if (!SAFE_SEGMENT.test(accountId) || !displayName
      || !Number.isSafeInteger(tokenLimit) || tokenLimit < 0 || tokenLimit > 10 ** 12
      || !Number.isSafeInteger(imageLimit) || imageLimit < 0 || imageLimit > 10 ** 9
      || (!editingUser && password.length < 10)
      || (password.length > 0 && password.length < 10)
      || password.length > 256) {
      showMessage("error", "用户资料不完整，或额度不在允许范围内。");
      return;
    }
    const source = editingUser;
    const key = source
      ? `user-update:${accountId}:${source.revision}:password-${userPasswordInputRevision}`
      : `user-create:${accountId}:password-${userPasswordInputRevision}`;
    void withBusy(elements.userSaveButton, "保存中", async () => {
      const base = {
        display_name: displayName,
        email,
        organization_id: organizationId,
        token_limit: tokenLimit,
        image_limit: imageLimit,
        ...(password ? { password } : {}),
        client_request_id: requestId(key),
      };
      const payload = source
        ? await apiRequest(`/users/${safeSegment(accountId)}`, {
            method: "PUT",
            body: {
              ...base,
              status: elements.userStatus.value,
              expected_revision: source.revision,
            },
          })
        : await apiRequest("/users", {
            method: "POST",
            body: { ...base, account_id: accountId },
          });
      const saved = normalizeUser(payload);
      requestIds.delete(key);
      elements.userDialog.close("saved");
      editingUser = null;
      await Promise.all([refreshUsers(), refreshUsage()]);
      showMessage("info", `${saved.display_name} 已保存。`);
    });
  });

  elements.usageCancelButton.addEventListener("click", () => elements.usageDialog.close("cancel"));

  elements.usageForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const accountId = elements.usageAccountId.value;
    const revision = Number(elements.usageUserRevision.value);
    const tokenDelta = Number(elements.usageTokenDelta.value);
    const imageDelta = Number(elements.usageImageDelta.value);
    const reason = elements.usageReason.value.trim();
    if (!SAFE_SEGMENT.test(accountId) || !Number.isSafeInteger(revision) || revision < 1
      || !Number.isSafeInteger(tokenDelta) || Math.abs(tokenDelta) > 10 ** 12
      || !Number.isSafeInteger(imageDelta) || Math.abs(imageDelta) > 10 ** 9
      || (tokenDelta === 0 && imageDelta === 0) || !reason) {
      showMessage("error", "请输入非零用量变化和可审计的原因。");
      return;
    }
    const key = `usage:${accountId}:${revision}:${tokenDelta}:${imageDelta}`;
    void withBusy(elements.usageSaveButton, "记录中", async () => {
      const saved = normalizeUser(await apiRequest(`/users/${safeSegment(accountId)}/usage-adjustments`, {
        method: "POST",
        body: {
          token_delta: tokenDelta,
          image_delta: imageDelta,
          reason,
          expected_revision: revision,
          client_request_id: requestId(key),
        },
      }));
      requestIds.delete(key);
      elements.usageDialog.close("saved");
      await Promise.all([refreshUsers(), refreshUsage()]);
      showMessage("info", `${saved.display_name} 的用量校正已记录。`);
    });
  });

  elements.refreshUsageButton.addEventListener("click", () => {
    void withBusy(elements.refreshUsageButton, "刷新中", refreshUsage);
  });

  elements.createModelButton.addEventListener("click", () => openModelDialog());
  elements.modelCancelButton.addEventListener("click", () => elements.modelDialog.close("cancel"));

  const modelSlotModalities = {
    "ecorex-chat": "chat",
    "ecorex-deepseek-v4-pro": "chat",
    "ecorex-gemini-3.1-pro": "chat",
    "ecorex-doubao-seed-2.0-pro": "chat",
    "gpt-image-2": "image_generation",
    "gpt-image-2-edit": "image_edit",
  };
  const modelSlotProtocols = {
    "ecorex-chat": "responses",
    "ecorex-deepseek-v4-pro": "openai_compatible_chat",
    "ecorex-gemini-3.1-pro": "openai_compatible_chat",
    "ecorex-doubao-seed-2.0-pro": "openai_compatible_chat",
    "gpt-image-2": "openai_compatible_image",
    "gpt-image-2-edit": "openai_compatible_image",
  };
  const syncModelProvider = () => {
    const modality = elements.modelModality.value;
    for (const option of elements.modelLocalId.options) {
      option.disabled = modelSlotModalities[option.value] !== modality;
    }
    if (modelSlotModalities[elements.modelLocalId.value] !== modality) {
      const first = Array.from(elements.modelLocalId.options).find((option) => !option.disabled);
      if (first) elements.modelLocalId.value = first.value;
    }
    elements.modelProvider.value = modelSlotProtocols[elements.modelLocalId.value];
  };
  elements.modelModality.addEventListener("change", syncModelProvider);
  elements.modelLocalId.addEventListener("change", syncModelProvider);

  elements.modelForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const source = editingModel;
    const localModelId = elements.modelLocalId.value.trim();
    const displayName = elements.modelDisplayName.value.trim();
    const upstreamModelId = elements.modelUpstreamId.value.trim();
    const apiKey = elements.modelApiKey.value;
    if (!SAFE_SEGMENT.test(localModelId) || !displayName || !upstreamModelId
      || (!source && apiKey.length < 8)) {
      showMessage("error", "请完整填写模型 ID、页面名称、服务端模型名和 Key。");
      return;
    }
    const expectedActive = source?.active?.revision ?? null;
    const key = source
      ? `model-stage:${source.config_id}:${expectedActive ?? "none"}`
      : `model-create:${localModelId}`;
    void withBusy(elements.modelSaveButton, "加密保存中", async () => {
      try {
        const common = {
          display_name: displayName,
          upstream_model_id: upstreamModelId,
          provider_preset: elements.modelProvider.value,
          is_default: elements.modelDefault.checked,
          enabled: elements.modelEnabled.checked,
          api_key: apiKey || null,
          client_request_id: requestId(key),
        };
        const payload = source
          ? await apiRequest(`/models/${safeSegment(source.config_id)}/draft`, {
              method: "PUT",
              body: { ...common, expected_active_revision: expectedActive },
            })
          : await apiRequest("/models", {
              method: "POST",
              body: {
                ...common,
                api_key: apiKey,
                local_model_id: localModelId,
                modality: elements.modelModality.value,
              },
            });
        const saved = normalizeModelConfiguration(payload);
        requestIds.delete(key);
        elements.modelDialog.close("saved");
        editingModel = null;
        renderModels(models.some((item) => item.config_id === saved.config_id)
          ? models.map((item) => item.config_id === saved.config_id ? saved : item)
          : [...models, saved]);
        showMessage("info", "模型草稿已加密保存。请点击“测试并启用”，通过后才会影响新请求。");
      } finally {
        elements.modelApiKey.value = "";
      }
    });
  });

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

  const syncRolloutMode = () => {
    const full = elements.rolloutMode.value === "full";
    elements.rolloutPercentage.disabled = full;
    elements.targetOrganizations.disabled = full;
    elements.targetAccounts.disabled = full;
    if (full) {
      elements.rolloutPercentage.value = "100";
      elements.targetOrganizations.value = "";
      elements.targetAccounts.value = "";
    } else if (elements.rolloutPercentage.value === "100") {
      elements.rolloutPercentage.value = "1";
    }
    elements.createRolloutButton.textContent = full ? "创建全量推送" : "创建灰度";
  };
  elements.rolloutMode.addEventListener("change", syncRolloutMode);

  elements.rolloutForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!candidate) return;
    try {
      const full = elements.rolloutMode.value === "full";
      const percentage = full ? 100 : Number(elements.rolloutPercentage.value);
      if (!Number.isInteger(percentage) || percentage < 1 || percentage > 100) {
        elements.rolloutPercentage.setAttribute("aria-invalid", "true");
        throw new Error("灰度比例必须是 1–100 的整数。");
      }
      elements.rolloutPercentage.removeAttribute("aria-invalid");
      const organizations = full ? [] : parseTargetList(elements.targetOrganizations.value, "组织 ID");
      const accounts = full ? [] : parseTargetList(elements.targetAccounts.value, "账号 ID");
      const minimum = elements.minimumVersion.value.trim() || null;
      const body = {
        release_id: candidate.release_id,
        percentage,
        target_organization_ids: organizations,
        target_account_ids: accounts,
        minimum_compatible_version: minimum,
      };
      askConfirmation({
        title: full ? "确认全量推送" : "确认创建灰度",
        description: full
          ? "将向全部符合兼容要求的用户推送该版本。创建后仍需显式激活。"
          : `灰度比例 ${percentage}%，目标组织 ${organizations.length} 个、账号 ${accounts.length} 个。创建后不会自动激活。`,
        confirmLabel: full ? "创建全量推送" : "创建灰度",
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
    adminRefreshToken = "";
    adminLeaseId = "";
    adminAccessExpiresAt = 0;
    authGeneration += 1;
    refreshPromise = null;
    requestIds.clear();
  });

  clearProjection();
  clearDistribution();
  clearManagement();
  syncModelProvider();
  syncRolloutMode();
  syncControls();
})();
