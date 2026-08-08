import { Activity, AlertCircle, Blocks, BookOpenText, Brain, Camera, FolderOutput, KeyRound, RefreshCw, RotateCcw, Shield, UserRound, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type {
  BootstrapResponse,
  ExtensionCatalogSnapshot,
  MemorySnapshot,
  MigrationCredentialKind,
  MigrationCredentialOrigin,
  MigrationQuarantineProjection,
  OutputLocationAlias,
  OutputLocationOption,
  OutputPreference,
  SystemHealthSample,
} from "../api/contracts.ts";
import {
  createClientRequestId,
  type RuntimeClient,
} from "../api/runtimeClient.ts";
import {
  extensionCatalogSummary,
  type ExtensionLoadState,
} from "../state/extensions.ts";
import { userFacingError } from "../state/userLanguage.ts";
import {
  isRuntimeUpdateInstalling,
  isVerifiedRuntimeUpdateReady,
  runtimeUpdateStatusText,
} from "../state/updatePresentation.ts";

interface SettingsDialogProps {
  open: boolean;
  bootstrap: BootstrapResponse | null;
  onOpenChange: (open: boolean) => void;
  permissionUpdating: boolean;
  permissionError: string | null;
  onClearPermissionError: () => void;
  onPermissionChange: (profile: "default" | "full_access") => Promise<boolean>;
  extensions: ExtensionCatalogSnapshot | null;
  extensionLoadState: ExtensionLoadState;
  onManageExtensions: () => void;
  onManageKnowledge: () => void;
  profileAvatar: string | null;
  onProfileAvatarChange: (value: string | null) => void;
  memory: MemorySnapshot | null;
  memoryLoadState: "loading" | "ready" | "error";
  memoryBusy: boolean;
  memoryError: string | null;
  onClearMemoryError: () => void;
  onRefreshMemory: () => void;
  onResetMemory: () => Promise<boolean>;
  onUndoMemoryReset: (resetId: string) => Promise<boolean>;
  client: RuntimeClient;
  outputLocations: OutputLocationOption[];
  outputPreference: OutputPreference | null;
  outputLoadState: "loading" | "ready" | "error";
  outputBusy: boolean;
  outputError: string | null;
  onClearOutputError: () => void;
  onRefreshOutput: () => void;
  onOutputLocationChange: (location: OutputLocationAlias) => Promise<boolean>;
  onPickOutputLocation: () => Promise<boolean>;
  systemHealth: SystemHealthSample | null;
  systemHealthLoadState: "loading" | "ready" | "error";
  systemHealthError: string | null;
  onClearSystemHealthError: () => void;
  onRefreshSystemHealth: () => void;
  onLoadSystemTechnicalHealth: () => Promise<SystemHealthSample>;
  updateBusy: boolean;
  updateError: string | null;
  onClearUpdateError: () => void;
  onCheckUpdate: () => Promise<boolean>;
  onActivateUpdate: () => Promise<boolean>;
}

export function SettingsDialog({
  open,
  bootstrap,
  onOpenChange,
  permissionUpdating,
  permissionError,
  onClearPermissionError,
  onPermissionChange,
  extensions,
  extensionLoadState,
  onManageExtensions,
  onManageKnowledge,
  profileAvatar,
  onProfileAvatarChange,
  memory,
  memoryLoadState,
  memoryBusy,
  memoryError,
  onClearMemoryError,
  onRefreshMemory,
  onResetMemory,
  onUndoMemoryReset,
  client,
  outputLocations,
  outputPreference,
  outputLoadState,
  outputBusy,
  outputError,
  onClearOutputError,
  onRefreshOutput,
  onOutputLocationChange,
  onPickOutputLocation,
  systemHealth,
  systemHealthLoadState,
  systemHealthError,
  onClearSystemHealthError,
  onRefreshSystemHealth,
  onLoadSystemTechnicalHealth,
  updateBusy,
  updateError,
  onClearUpdateError,
  onCheckUpdate,
  onActivateUpdate,
}: SettingsDialogProps) {
  const authenticated = bootstrap?.login.authenticated === true;
  const fullAccess = bootstrap?.permissions.profile === "full_access";
  const [confirmElevation, setConfirmElevation] = useState(false);
  const [confirmMemoryReset, setConfirmMemoryReset] = useState(false);
  const [confirmQuarantineDelete, setConfirmQuarantineDelete] = useState(false);
  const [migrationQuarantine, setMigrationQuarantine] = useState<MigrationQuarantineProjection | null>(null);
  const [migrationQuarantineLoadState, setMigrationQuarantineLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [migrationQuarantineBusy, setMigrationQuarantineBusy] = useState(false);
  const [migrationQuarantineError, setMigrationQuarantineError] = useState<string | null>(null);
  const pendingMigrationQuarantineDelete = useRef<string | null>(null);
  const [technicalHealth, setTechnicalHealth] = useState<SystemHealthSample | null>(null);
  const [technicalHealthLoading, setTechnicalHealthLoading] = useState(false);
  const [technicalHealthError, setTechnicalHealthError] = useState<string | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const passwordRequestId = useRef<string | null>(null);
  const extensionSummary = extensionCatalogSummary(extensions);
  const updateReady = isVerifiedRuntimeUpdateReady(bootstrap?.update);
  const updateAvailable = bootstrap?.update.state === "available";
  const updateInstalling = isRuntimeUpdateInstalling(bootstrap?.update, updateBusy);

  const refreshMigrationQuarantine = useCallback(async (signal?: AbortSignal) => {
    setMigrationQuarantineLoadState((current) => current === "ready" ? current : "loading");
    try {
      const projection = await client.migrationQuarantine(signal);
      if (signal?.aborted) return false;
      setMigrationQuarantine(projection);
      setMigrationQuarantineLoadState("ready");
      setMigrationQuarantineError(null);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return false;
      setMigrationQuarantineLoadState("error");
      setMigrationQuarantineError(userFacingError(error));
      return false;
    }
  }, [client]);

  const deleteMigrationQuarantine = useCallback(async () => {
    if (migrationQuarantineBusy || migrationQuarantine?.can_delete !== true) return false;
    const clientRequestId = pendingMigrationQuarantineDelete.current
      ?? createClientRequestId("delete_migration_quarantine");
    pendingMigrationQuarantineDelete.current = clientRequestId;
    setMigrationQuarantineBusy(true);
    setMigrationQuarantineError(null);
    try {
      const projection = await client.deleteMigrationQuarantine(clientRequestId);
      pendingMigrationQuarantineDelete.current = null;
      setMigrationQuarantine(projection);
      setMigrationQuarantineLoadState("ready");
      return true;
    } catch (error) {
      setMigrationQuarantineError(userFacingError(error));
      return false;
    } finally {
      setMigrationQuarantineBusy(false);
    }
  }, [client, migrationQuarantine?.can_delete, migrationQuarantineBusy]);

  useEffect(() => {
    if (!open || fullAccess) setConfirmElevation(false);
  }, [fullAccess, open]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void refreshMigrationQuarantine(controller.signal);
    return () => controller.abort();
  }, [open, refreshMigrationQuarantine]);

  useEffect(() => {
    if (!open) {
      setConfirmMemoryReset(false);
      setConfirmQuarantineDelete(false);
      setTechnicalHealth(null);
      setTechnicalHealthLoading(false);
      setTechnicalHealthError(null);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordError(null);
      setAvatarError(null);
      passwordRequestId.current = null;
    }
  }, [open]);

  const applyPermission = async (profile: "default" | "full_access") => {
    const applied = await onPermissionChange(profile);
    if (applied) setConfirmElevation(false);
  };

  const changePassword = async () => {
    if (passwordBusy) return;
    if (newPassword.length < 10 || newPassword.length > 256) {
      setPasswordError("新密码需为 10–256 个字符。");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("两次输入的新密码不一致。");
      return;
    }
    const requestId = passwordRequestId.current ?? createClientRequestId("session_password");
    passwordRequestId.current = requestId;
    setPasswordBusy(true);
    setPasswordError(null);
    try {
      await client.changeSessionPassword(currentPassword, newPassword, requestId);
      passwordRequestId.current = null;
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordError("密码已更新，正在返回登录页…");
      window.setTimeout(() => window.location.reload(), 1_500);
    } catch (error) {
      setPasswordError(userFacingError(error));
    } finally {
      setPasswordBusy(false);
    }
  };

  const chooseAvatar = (file: File | undefined) => {
    if (!file) return;
    if (!file.type.match(/^image\/(?:png|jpeg|webp)$/u)) {
      setAvatarError("请选择 PNG、JPEG 或 WebP 图片。");
      return;
    }
    if (file.size > 512 * 1024) {
      setAvatarError("头像不能超过 512 KB。");
      return;
    }
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (typeof reader.result !== "string" || !/^data:image\/(?:png|jpeg|webp);base64,/u.test(reader.result)) {
        setAvatarError("头像读取失败，请重试。");
        return;
      }
      onProfileAvatarChange(reader.result);
      setAvatarError(null);
    }, { once: true });
    reader.addEventListener("error", () => setAvatarError("头像读取失败，请重试。"), { once: true });
    reader.readAsDataURL(file);
  };

  const credentialKindLabel = (kind: MigrationCredentialKind) => ({
    api_key: "模型或服务 API Key",
    refresh_token: "刷新令牌",
    access_token: "访问令牌",
    password: "旧版密码",
    cryptographic_key: "加密密钥",
    client_secret: "应用密钥",
    credential: "旧版凭证",
  })[kind];
  const credentialOriginLabel = (origin: MigrationCredentialOrigin) => ({
    product_configuration: "e-Mate 旧版设置",
    mcp_configuration: "扩展服务与文档连接设置",
    skill_configuration: "旧版技能设置",
    permission_configuration: "旧版权限设置",
  })[origin];

  if (!open) return null;

  return (
    <section className="ex-settings-workspace" aria-label="设置" data-testid="settings-workspace">
      <header className="ex-settings-page-header">
        <div><h1>设置</h1><p id="ecorex-settings-description">管理个人资料、常规设置、知识和记忆。</p></div>
        <button className="ex-icon-button" type="button" aria-label="关闭设置" onClick={() => onOpenChange(false)}><X aria-hidden="true" /></button>
      </header>
      <div className="ex-settings-page-layout">
        <nav className="ex-settings-page-nav" aria-label="设置分区">
          <a href="#settings-profile">个人资料</a>
          <a href="#settings-general">常规设置</a>
          <a href="#settings-knowledge">知识</a>
          <a href="#settings-memory">记忆</a>
        </nav>
        <div className="ex-settings-page-content">

          <section className="ex-settings-section" id="settings-profile">
            <h2>个人资料</h2>
            <div className="ex-profile-avatar-row">
              <span className="ex-profile-avatar-preview">
                {profileAvatar ? <img src={profileAvatar} alt="当前头像" /> : <UserRound aria-hidden="true" />}
              </span>
              <div><strong>头像</strong><p>仅保存在此设备，不会上传或改变企业账号资料。</p></div>
              <label className="ex-button ex-profile-avatar-button">
                <Camera aria-hidden="true" /><span>选择图片</span>
                <input type="file" aria-label="选择头像图片" accept="image/png,image/jpeg,image/webp" onChange={(event) => chooseAvatar(event.target.files?.[0])} />
              </label>
              {profileAvatar ? <button className="ex-button" type="button" onClick={() => onProfileAvatarChange(null)}>移除</button> : null}
            </div>
            {avatarError ? <p className="ex-settings-note is-error" role="status">{avatarError}</p> : null}
            <div className="ex-settings-row">
              <div>
                <strong>{authenticated ? bootstrap?.login.display_name || "已登录账号" : "需要登录"}</strong>
                <p>
                  {authenticated
                    ? [bootstrap?.login.organization_id, ...(bootstrap?.login.roles ?? [])]
                      .filter(Boolean)
                      .join("，") || "账户状态已验证"
                    : "模型任务已暂停。历史记录、本地产物和更新仍可使用。"}
                </p>
              </div>
            </div>
            <p className="ex-settings-note">
              {authenticated
                ? bootstrap?.policy_lease
                  ? `账户授权有效至 ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(bootstrap.policy_lease.expires_at))}。`
                  : "账户授权已过期，模型任务已暂停。"
                : bootstrap?.login_service.state === "ready"
                  ? "请关闭设置，在工作区登录卡中完成安全设备登录；授权后 e-Mate 会受控重启并刷新页面。"
                  : "设备登录暂不可用，请稍后重试或联系管理员。"}
            </p>
            {authenticated ? (
              <form className="ex-password-form" onSubmit={(event) => { event.preventDefault(); void changePassword(); }}>
                <label className="ex-field ex-password-current"><span>当前密码</span><input type="password" autoComplete="current-password" value={currentPassword} disabled={passwordBusy} minLength={8} maxLength={256} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
                <label className="ex-field"><span>新密码</span><input type="password" autoComplete="new-password" value={newPassword} disabled={passwordBusy} minLength={10} maxLength={256} onChange={(event) => setNewPassword(event.target.value)} /></label>
                <label className="ex-field"><span>确认新密码</span><input type="password" autoComplete="new-password" value={confirmPassword} disabled={passwordBusy} minLength={10} maxLength={256} onChange={(event) => setConfirmPassword(event.target.value)} /></label>
                <div className="ex-password-form-actions">
                  <p className={`ex-field-help${passwordError ? " is-error" : ""}`} role={passwordError ? "status" : undefined}>{passwordError ?? "修改后所有设备会退出登录，请使用新密码重新登录。"}</p>
                  <button className="ex-button is-primary" type="submit" disabled={passwordBusy || !currentPassword || !newPassword || !confirmPassword}>{passwordBusy ? "正在修改" : "修改密码"}</button>
                </div>
              </form>
            ) : null}
          </section>

          <section className="ex-settings-section" id="settings-general">
            <h2>常规设置</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><FolderOutput aria-hidden="true" /></span>
              <div>
                <strong>默认保存位置</strong>
                <p>
                  {outputLoadState === "loading"
                    ? "正在读取保存位置"
                    : "下载产物时由 e-Mate 安全保存到这里"}
                </p>
              </div>
              {outputPreference ? (
                <div className="ex-output-location-controls">
                  <select
                    className="ex-settings-select"
                    aria-label="默认产物保存位置"
                    value={outputPreference.location_alias}
                    disabled={outputBusy}
                    onChange={(event) => {
                      void onOutputLocationChange(event.target.value as OutputLocationAlias);
                    }}
                  >
                    {outputLocations.map((location) => (
                      <option
                        key={location.alias}
                        value={location.alias}
                        disabled={!location.available}
                      >
                        {location.alias === "documents"
                          ? "文档"
                          : location.alias === "downloads"
                            ? "下载"
                            : "自定义文件夹"}
                        {!location.available ? "（未选择）" : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    className="ex-button ex-button-subtle"
                    type="button"
                    disabled={outputBusy}
                    onClick={() => void onPickOutputLocation()}
                  >
                    {outputBusy ? "正在选择" : "选择文件夹"}
                  </button>
                </div>
              ) : (
                <button
                  className="ex-button ex-permission-change"
                  type="button"
                  disabled={outputLoadState === "loading"}
                  onClick={onRefreshOutput}
                >
                  重新读取
                </button>
              )}
            </div>
            <p className="ex-settings-note">
              新位置只用于之后新建的任务。已经开始的任务会继续使用开始时的位置，长任务不会中途换目录。
            </p>
            {outputError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{outputError}</span>
                <button
                  className="ex-icon-button"
                  type="button"
                  aria-label="关闭产物位置错误"
                  onClick={onClearOutputError}
                >
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </section>

          <section className="ex-settings-section">
            <h2>系统状态</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><Activity aria-hidden="true" /></span>
              <div>
                <strong aria-live="polite">
                  {systemHealth?.summary
                    ?? (systemHealthLoadState === "loading" ? "正在检查 e-Mate" : "暂时无法读取系统状态")}
                </strong>
                <p>
                  {systemHealth
                    ? `最近检查：${new Intl.DateTimeFormat("zh-CN", { timeStyle: "medium" }).format(new Date(systemHealth.sampled_at))}`
                    : "任务和本地产物不会因状态页不可用而停止。"}
                </p>
              </div>
              <button
                className="ex-button ex-permission-change"
                type="button"
                disabled={systemHealthLoadState === "loading"}
                onClick={onRefreshSystemHealth}
              >
                <RefreshCw aria-hidden="true" />
                重新检查
              </button>
            </div>
            {systemHealth?.components.length ? (
              <div className="ex-system-health-components">
                {systemHealth.components.map((component) => (
                  <div key={component.component_id}>
                    <span className="ex-system-health-status" data-status={component.status}>
                      {component.status === "healthy" ? "正常" : component.status === "degraded" ? "需留意" : "需处理"}
                    </span>
                    <strong>{component.label}</strong>
                    <p>{component.message}</p>
                  </div>
                ))}
              </div>
            ) : null}
            {systemHealthError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{systemHealthError}</span>
                <button
                  className="ex-icon-button"
                  type="button"
                  aria-label="关闭系统状态错误"
                  onClick={onClearSystemHealthError}
                >
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
            <details
              className="ex-system-technical"
              onToggle={(event) => {
                if (!event.currentTarget.open || technicalHealth || technicalHealthLoading) return;
                setTechnicalHealthLoading(true);
                setTechnicalHealthError(null);
                void onLoadSystemTechnicalHealth()
                  .then(setTechnicalHealth)
                  .catch((error: unknown) => {
                    setTechnicalHealthError(userFacingError(error));
                  })
                  .finally(() => setTechnicalHealthLoading(false));
              }}
            >
              <summary>技术详情</summary>
              {technicalHealthLoading ? <p>正在读取诊断指标…</p> : null}
              {technicalHealthError ? <p role="alert">{technicalHealthError}</p> : null}
              {technicalHealth?.metrics ? (
                <pre>{JSON.stringify(technicalHealth.metrics, null, 2)}</pre>
              ) : null}
            </details>
          </section>

          <section className="ex-settings-section">
            <h2>扩展</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><Blocks aria-hidden="true" /></span>
              <div>
                <strong>技能与扩展能力</strong>
                <p aria-live="polite">
                  {extensionLoadState === "loading"
                    ? "正在同步扩展目录"
                    : `${extensionSummary.total} 个扩展，${extensionSummary.enabled} 个已启用`}
                </p>
              </div>
              <button
                className="ex-button ex-permission-change"
                type="button"
                disabled={!bootstrap}
                onClick={onManageExtensions}
              >
                管理扩展
              </button>
            </div>
            <p className="ex-settings-note">
              {extensionSummary.quarantined
                ? `${extensionSummary.quarantined} 个扩展已被隔离。打开管理页查看原因和可用操作。`
                : extensionLoadState === "error"
                  ? "扩展目录尚未同步。打开管理页即可刷新。"
                  : "e-Mate 会在启用前检查来源、依赖、健康状态和权限影响。"}
            </p>
          </section>

          <section className="ex-settings-section" id="settings-knowledge">
            <h2>知识</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><BookOpenText aria-hidden="true" /></span>
              <div><strong>知识库</strong><p>由 Runtime 的知识能力读取、整理和更新，不在设置页保存副本。</p></div>
              <button className="ex-button ex-permission-change" type="button" onClick={onManageKnowledge}>在会话中管理</button>
            </div>
            <p className="ex-settings-note">点击后会进入当前会话，由小芯先读取真实知识目录，再按你的确认执行修改。</p>
          </section>

          <section className="ex-settings-section" id="settings-memory">
            <h2>记忆</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><Brain aria-hidden="true" /></span>
              <div>
                <strong>学习记忆</strong>
                <p aria-live="polite">
                  {memoryLoadState === "loading"
                    ? "正在读取记忆状态"
                    : memory
                      ? `${memory.resettable_count} 项可重置的偏好和资料记忆`
                      : "暂时无法读取记忆状态"}
                </p>
              </div>
              <button
                className="ex-button ex-permission-change"
                type="button"
                disabled={memoryBusy || memoryLoadState === "loading" || memory?.resettable_count === 0}
                onClick={() => setConfirmMemoryReset(true)}
              >
                一键重置
              </button>
            </div>
            <p className="ex-settings-note">
              只重置 e-Mate 从使用中学到的偏好和旧版导入记忆。不会删除内置知识、任务、消息或产物。
            </p>
            {memory?.latest_reset?.can_undo ? (
              <div className="ex-memory-undo">
                <div>
                  <strong>最近一次重置可以撤销</strong>
                  <p>
                    已重置 {memory.latest_reset.affected_records + memory.latest_reset.affected_files} 项，
                    可在 {new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(memory.latest_reset.undo_until))} 前恢复。
                  </p>
                </div>
                <button
                  className="ex-button"
                  type="button"
                  disabled={memoryBusy}
                  onClick={() => void onUndoMemoryReset(memory.latest_reset!.reset_id)}
                >
                  <RotateCcw aria-hidden="true" />
                  撤销重置
                </button>
              </div>
            ) : null}
            {confirmMemoryReset ? (
              <div className="ex-permission-confirm" role="group" aria-label="确认重置学习记忆">
                <div>
                  <strong>确认重置学习记忆？</strong>
                  <p>重置会立即停止使用这些记忆，并保留 24 小时撤销时间。</p>
                </div>
                <div className="ex-permission-confirm-actions">
                  <button className="ex-button" type="button" disabled={memoryBusy} onClick={() => setConfirmMemoryReset(false)}>
                    取消
                  </button>
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={memoryBusy}
                    aria-busy={memoryBusy}
                    onClick={() => {
                      void onResetMemory().then((reset) => {
                        if (reset) setConfirmMemoryReset(false);
                      });
                    }}
                  >
                    {memoryBusy ? "正在重置" : "确认重置"}
                  </button>
                </div>
              </div>
            ) : null}
            {memoryLoadState === "error" && !memoryError ? (
              <button className="ex-button ex-settings-retry" type="button" onClick={onRefreshMemory}>
                重新读取记忆状态
              </button>
            ) : null}
            {memoryError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{memoryError}</span>
                <button className="ex-icon-button" type="button" aria-label="关闭记忆错误" onClick={onClearMemoryError}>
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </section>

          <section className="ex-settings-section">
            <h2>旧版凭证</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><KeyRound aria-hidden="true" /></span>
              <div>
                <strong aria-live="polite">
                  {migrationQuarantineLoadState === "loading"
                    ? "正在检查旧版凭证备份"
                    : migrationQuarantine?.status === "available"
                      ? `本机保留 ${migrationQuarantine.entry_count} 项加密旧版凭证`
                      : migrationQuarantine?.status === "deleted"
                        ? "旧版凭证备份已删除"
                        : "没有旧版凭证备份"}
                </strong>
                <p>
                  {migrationQuarantine?.status === "available"
                    ? "这些内容未被激活，也没有上传到云端。"
                    : migrationQuarantine?.status === "deleted" && migrationQuarantine.deleted_at
                      ? `删除于 ${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(migrationQuarantine.deleted_at))}`
                      : "当前连接器和托管模型不使用旧版密钥。"}
                </p>
              </div>
              {migrationQuarantine?.can_delete ? (
                <button
                  className="ex-button is-danger ex-permission-change"
                  type="button"
                  disabled={migrationQuarantineBusy}
                  onClick={() => setConfirmQuarantineDelete(true)}
                >
                  一键删除
                </button>
              ) : migrationQuarantineLoadState === "error" ? (
                <button
                  className="ex-button ex-permission-change"
                  type="button"
                  onClick={() => void refreshMigrationQuarantine()}
                >
                  重新读取
                </button>
              ) : null}
            </div>
            {migrationQuarantine?.items.length ? (
              <ul className="ex-settings-note" aria-label="旧版凭证种类和来源">
                {migrationQuarantine.items.map((item) => (
                  <li key={`${item.kind}:${item.origin}`}>
                    {credentialKindLabel(item.kind)} · {credentialOriginLabel(item.origin)} · {item.count} 项
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="ex-settings-note">
              删除只移除 e-Mate 保存的本地加密迁移备份，不影响当前任务和已经重新连接的应用；删除后不可撤销。
            </p>
            {confirmQuarantineDelete ? (
              <div className="ex-permission-confirm" role="group" aria-label="确认删除旧版凭证备份">
                <div>
                  <strong>确认永久删除旧版凭证备份？</strong>
                  <p>删除后无法从 e-Mate 恢复。当前托管模型和连接器不会受到影响。</p>
                </div>
                <div className="ex-permission-confirm-actions">
                  <button
                    className="ex-button"
                    type="button"
                    disabled={migrationQuarantineBusy}
                    onClick={() => setConfirmQuarantineDelete(false)}
                  >
                    取消
                  </button>
                  <button
                    className="ex-button is-danger"
                    type="button"
                    disabled={migrationQuarantineBusy}
                    aria-busy={migrationQuarantineBusy}
                    onClick={() => {
                      void deleteMigrationQuarantine().then((deleted) => {
                        if (deleted) setConfirmQuarantineDelete(false);
                      });
                    }}
                  >
                    {migrationQuarantineBusy ? "正在删除" : "永久删除"}
                  </button>
                </div>
              </div>
            ) : null}
            {migrationQuarantineError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{migrationQuarantineError}</span>
                <button
                  className="ex-icon-button"
                  type="button"
                  aria-label="关闭旧版凭证错误"
                  onClick={() => setMigrationQuarantineError(null)}
                >
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </section>

          <section className="ex-settings-section">
            <h2>权限</h2>
            <div className="ex-settings-row">
              <span className="ex-settings-icon"><Shield aria-hidden="true" /></span>
              <div>
                <strong>{fullAccess ? "完全访问" : "默认权限"}</strong>
                <p>
                  {fullAccess
                    ? "可在已授权位置执行操作，不再逐项询问"
                    : "主要在工作区内操作，敏感步骤会先询问"}
                </p>
              </div>
              <button
                className="ex-skill-switch ex-permission-switch"
                type="button"
                role="switch"
                aria-checked={fullAccess}
                aria-label={fullAccess ? "关闭完全访问" : "开启完全访问"}
                disabled={!bootstrap || !authenticated || permissionUpdating}
                aria-busy={permissionUpdating}
                title={fullAccess ? "切换为默认权限" : "切换为完全访问"}
                onClick={() => {
                  if (fullAccess) {
                    void applyPermission("default");
                  } else {
                    setConfirmElevation(true);
                  }
                }}
              ><span /></button>
            </div>
            <p className="ex-settings-note" aria-live="polite">
              {fullAccess
                ? "完全访问会跳过一般工具审批；可随时一键撤销，管理员硬限制仍然生效。"
                : authenticated
                  ? "默认权限在工具需要更高权限或外部写入时向你确认。"
                  : "登录托管账号并重启 e-Mate 后才能修改执行权限。"}
            </p>
            {bootstrap?.permissions.admin_hard_denies.length ? (
              <p className="ex-settings-note">
                组织策略限制了 {bootstrap.permissions.admin_hard_denies.length} 项高风险操作。
              </p>
            ) : null}
            {confirmElevation ? (
              <div
                className="ex-permission-confirm"
                role="group"
                aria-label="确认启用完全访问"
              >
                <div>
                  <strong>确认启用完全访问？</strong>
                  <p>
                    e-Mate 将可在本机执行命令并写入工作区，不再逐项请求一般工具审批；管理员限制仍不可绕过。
                  </p>
                </div>
                <div className="ex-permission-confirm-actions">
                  <button
                    className="ex-button"
                    type="button"
                    disabled={permissionUpdating}
                    onClick={() => setConfirmElevation(false)}
                  >
                    取消
                  </button>
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={permissionUpdating}
                    aria-busy={permissionUpdating}
                    onClick={() => void applyPermission("full_access")}
                  >
                    {permissionUpdating ? "正在应用" : "确认启用"}
                  </button>
                </div>
              </div>
            ) : null}
            {permissionError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{permissionError}</span>
                <button
                  className="ex-icon-button"
                  type="button"
                  aria-label="关闭权限错误"
                  onClick={onClearPermissionError}
                >
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </section>

          <section className="ex-settings-section">
            <h2>版本</h2>
            <div className="ex-settings-row">
              <div>
                <strong>e-Mate {bootstrap?.update.current_version ?? "版本未读取"}</strong>
                <p>
                  {runtimeUpdateStatusText(bootstrap?.update, updateBusy)}
                </p>
              </div>
              {updateAvailable || updateReady || updateInstalling ? (
                <button
                  className="ex-button is-primary ex-permission-change"
                  type="button"
                  disabled={updateBusy || updateInstalling}
                  aria-busy={updateInstalling}
                  onClick={() => void onActivateUpdate()}
                >
                  {bootstrap?.update.state === "activating"
                    ? "正在打开新版"
                    : updateInstalling
                      ? "正在下载并安装"
                      : updateAvailable
                        ? "下载并安装"
                        : "立即安装"}
                </button>
              ) : (
                <button
                  className="ex-button ex-permission-change"
                  type="button"
                  disabled={!bootstrap || updateBusy}
                  onClick={() => void onCheckUpdate()}
                >
                  {updateBusy ? "正在检查" : "检查更新"}
                </button>
              )}
            </div>
            {updateError ? (
              <div className="ex-settings-error" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{updateError}</span>
                <button
                  className="ex-icon-button"
                  type="button"
                  aria-label="关闭更新错误"
                  onClick={onClearUpdateError}
                >
                  <X aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </section>
        </div>
      </div>
    </section>
  );
}
