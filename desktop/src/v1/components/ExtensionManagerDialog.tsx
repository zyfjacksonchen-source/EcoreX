import * as Dialog from "@radix-ui/react-dialog";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  PackageSearch,
  RefreshCw,
  Search,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";

import type {
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionProjection,
} from "../api/contracts.ts";
import {
  extensionActionConfirmation,
  extensionActionDisabledReason,
  extensionActionLabel,
  extensionCatalogSummary,
  extensionExportKindLabel,
  extensionExposureLabel,
  extensionHealthLabel,
  extensionKindLabel,
  extensionPermissionEffectLabel,
  extensionSourceLabel,
  extensionStatusLabel,
  extensionTrustLabel,
  filterExtensions,
  type ExtensionKindFilter,
  type ExtensionLoadState,
  type ExtensionOperationState,
  type ExtensionStatusFilter,
} from "../state/extensions.ts";
import { TechnicalDetails } from "./TechnicalDetails.tsx";

interface ExtensionManagerDialogProps {
  open: boolean;
  snapshot: ExtensionCatalogSnapshot | null;
  loadState: ExtensionLoadState;
  error: string | null;
  operations: Record<string, ExtensionOperationState>;
  installBusy: boolean;
  onOpenChange: (open: boolean) => void;
  onClearError: () => void;
  onRefresh: () => Promise<ExtensionCatalogSnapshot | null>;
  onAction: (
    extension: ExtensionProjection,
    actionId: ExtensionActionId,
  ) => Promise<boolean>;
  onInstallLocalSkill: (extensionId: string, file: File) => Promise<boolean>;
}

interface PendingConfirmation {
  extensionId: string;
  actionId: ExtensionActionId;
}

function ExtensionHealthIcon({ extension }: { extension: ExtensionProjection }) {
  if (extension.health === "healthy") return <CheckCircle2 aria-hidden="true" />;
  if (
    extension.status === "quarantined"
    || extension.health === "unhealthy"
    || extension.health === "circuit_open"
  ) {
    return <AlertCircle aria-hidden="true" />;
  }
  return <Activity aria-hidden="true" />;
}

function ExtensionRow({
  extension,
  operation,
  registryOperationBusy,
  catalogReady,
  catalogDisabledReason,
  detailsOpen,
  pendingConfirmation,
  onToggleDetails,
  onRequestAction,
  onCancelConfirmation,
  onConfirmAction,
}: {
  extension: ExtensionProjection;
  operation: ExtensionOperationState | undefined;
  registryOperationBusy: boolean;
  catalogReady: boolean;
  catalogDisabledReason: string | null;
  detailsOpen: boolean;
  pendingConfirmation: PendingConfirmation | null;
  onToggleDetails: () => void;
  onRequestAction: (extension: ExtensionProjection, actionId: ExtensionActionId) => void;
  onCancelConfirmation: () => void;
  onConfirmAction: (extension: ExtensionProjection, actionId: ExtensionActionId) => void;
}) {
  const detailId = useId();
  const pendingAction = pendingConfirmation?.extensionId === extension.extension_id
    ? extension.actions.find((action) => action.action_id === pendingConfirmation.actionId)
    : null;
  const updatedDate = new Date(extension.updated_at);
  const updatedAt = Number.isFinite(updatedDate.getTime())
    ? new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(updatedDate)
    : "未提供有效时间";

  return (
    <article className="ex-extension-row" data-status={extension.status}>
      <div className="ex-extension-row-main">
        <div className="ex-extension-heading">
          <span className={`ex-extension-health is-${extension.health}`}>
            <ExtensionHealthIcon extension={extension} />
          </span>
          <div>
            <div className="ex-extension-title-line">
              <h3>{extension.display_name}</h3>
              <span className="ex-extension-status">{extensionStatusLabel(extension.status)}</span>
            </div>
            <div className="ex-extension-facts">
              <span>{extensionKindLabel(extension.kind)}</span>
              <span>版本 {extension.active_version ?? "未激活"}</span>
              <span><ShieldCheck aria-hidden="true" />{extensionTrustLabel(extension.trust)}</span>
              <span>{extensionHealthLabel(extension.health)}</span>
            </div>
          </div>
        </div>

        {extension.actions.length ? (
          <div className="ex-extension-actions" aria-label={`${extension.display_name} 的可用操作`}>
            {extension.actions.map((action) => {
              const reasonId = `${detailId}-${action.action_id}-reason`;
              const disabledReason = catalogDisabledReason
                ?? (registryOperationBusy && !operation
                  ? "另一项扩展操作正在执行；完成后才能操作此扩展。"
                  : extensionActionDisabledReason(action));
              const actionBusy = operation?.actionId === action.action_id;
              return (
                <div className="ex-extension-action" key={action.action_id}>
                  <button
                    className={`ex-button${action.action_id === "enable" ? " is-primary" : ""}`}
                    type="button"
                    disabled={
                      !catalogReady
                      || !action.enabled
                      || registryOperationBusy
                      || Boolean(operation)
                    }
                    aria-busy={actionBusy}
                    aria-describedby={disabledReason ? reasonId : undefined}
                    onClick={() => onRequestAction(extension, action.action_id)}
                  >
                    {actionBusy ? "正在执行" : extensionActionLabel(action.action_id)}
                  </button>
                  {disabledReason ? (
                    <span className="ex-extension-action-reason" id={reasonId}>{disabledReason}</span>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : null}

        <button
          className="ex-button ex-extension-detail-toggle"
          type="button"
          aria-expanded={detailsOpen}
          aria-controls={detailId}
          onClick={onToggleDetails}
        >
          {detailsOpen ? "收起详情" : "查看详情"}
        </button>
      </div>

      {detailsOpen ? (
        <section className="ex-extension-detail" id={detailId} aria-label={`${extension.display_name} 的详情`}>
          <div className="ex-extension-title-line">
            <strong>扩展详情</strong>
            <span>{extensionSourceLabel(extension.source)} · 更新于 {updatedAt}</span>
          </div>
          <p>{extension.description}</p>
          <div className="ex-extension-relations">
            <section aria-label={`${extension.display_name} 的依赖`}>
              <h4>依赖</h4>
              {extension.dependencies.length ? (
                <ul>
                  {extension.dependencies.map((dependency) => (
                    <li key={`${dependency.extension_id}:${dependency.version_range}`}>
                      <span>{dependency.extension_id}</span>
                      <code>{dependency.version_range}</code>
                    </li>
                  ))}
                </ul>
              ) : <p>无扩展依赖</p>}
            </section>
            <section aria-label={`${extension.display_name} 的导出能力`}>
              <h4>导出</h4>
              {extension.exports.length ? (
                <ul>
                  {extension.exports.map((exported) => (
                    <li key={`${exported.kind}:${exported.export_id}`}>
                      <span>
                        {extensionExportKindLabel(exported.kind)} · {exported.export_id}
                      </span>
                      <small>
                        {extensionExposureLabel(exported.exposure)} · {
                          exported.permission_effects.length
                            ? exported.permission_effects
                              .map(extensionPermissionEffectLabel)
                              .join("、")
                            : "无权限影响"
                        }
                      </small>
                    </li>
                  ))}
                </ul>
              ) : <p>未导出用户能力</p>}
            </section>
          </div>

          {extension.last_error_code ? (
            <TechnicalDetails
              summary="错误详情"
              entries={[{ label: "错误代码", value: extension.last_error_code }]}
            />
          ) : null}
        </section>
      ) : null}

      {catalogReady && pendingAction?.enabled && pendingAction.requires_confirmation ? (
        <div className="ex-extension-confirm" role="group" aria-label="确认扩展操作">
          <p>{extensionActionConfirmation(extension, pendingAction.action_id)}</p>
          <div>
            <button className="ex-button" type="button" onClick={onCancelConfirmation}>
              取消
            </button>
            <button
              className="ex-button is-primary"
              type="button"
              disabled={Boolean(operation)}
              aria-busy={operation?.actionId === pendingAction.action_id}
              onClick={() => onConfirmAction(extension, pendingAction.action_id)}
            >
              {operation?.actionId === pendingAction.action_id
                ? "正在执行"
                : `确认${extensionActionLabel(pendingAction.action_id)}`}
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

export function ExtensionManagerDialog({
  open,
  snapshot,
  loadState,
  error,
  operations,
  installBusy,
  onOpenChange,
  onClearError,
  onRefresh,
  onAction,
  onInstallLocalSkill,
}: ExtensionManagerDialogProps) {
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [selectedExtensionId, setSelectedExtensionId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<ExtensionKindFilter>("all");
  const [statusFilter, setStatusFilter] = useState<ExtensionStatusFilter>("all");
  const [localExtensionId, setLocalExtensionId] = useState("");
  const [localBundle, setLocalBundle] = useState<File | null>(null);
  const [installInputKey, setInstallInputKey] = useState(0);
  const summary = useMemo(() => extensionCatalogSummary(snapshot), [snapshot]);
  const visibleExtensions = useMemo(() => filterExtensions(
    snapshot?.items ?? [],
    query,
    kindFilter,
    statusFilter,
  ), [kindFilter, query, snapshot, statusFilter]);
  const operationBusy = Object.keys(operations).length > 0;
  const catalogReady = loadState === "ready";
  const catalogDisabledReason = loadState === "loading"
    ? "目录正在重新验证；完成前不会执行扩展操作。"
    : loadState === "error"
      ? "上次验证的目录已过期；刷新成功前不会执行扩展操作。"
      : loadState === "idle"
        ? "扩展目录尚未验证；刷新成功前不会执行扩展操作。"
        : null;
  const catalogFreshness = loadState === "ready"
    ? "目录已验证"
    : loadState === "loading"
      ? snapshot ? "上次已验证目录 · 正在重新验证" : "正在验证目录"
      : loadState === "error"
        ? snapshot ? "上次已验证目录已过期" : "目录验证失败"
        : "目录尚未验证";

  useEffect(() => {
    if (!open) {
      setPendingConfirmation(null);
      setSelectedExtensionId(null);
      setQuery("");
      setKindFilter("all");
      setStatusFilter("all");
      setLocalExtensionId("");
      setLocalBundle(null);
      setInstallInputKey((value) => value + 1);
      return;
    }
    void onRefresh();
  }, [open, onRefresh]);

  useEffect(() => {
    if (!catalogReady) setPendingConfirmation(null);
  }, [catalogReady]);

  useEffect(() => {
    if (
      selectedExtensionId
      && !snapshot?.items.some((extension) => extension.extension_id === selectedExtensionId)
    ) {
      setSelectedExtensionId(null);
    }
  }, [selectedExtensionId, snapshot]);

  const requestAction = (extension: ExtensionProjection, actionId: ExtensionActionId) => {
    if (!catalogReady) return;
    const action = extension.actions.find((candidate) => candidate.action_id === actionId);
    if (!action?.enabled) return;
    if (action.requires_confirmation) {
      setPendingConfirmation({ extensionId: extension.extension_id, actionId });
      return;
    }
    void onAction(extension, actionId);
  };

  const confirmAction = (extension: ExtensionProjection, actionId: ExtensionActionId) => {
    if (!catalogReady) return;
    void onAction(extension, actionId).then((completed) => {
      if (completed) setPendingConfirmation(null);
    });
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-extension-dialog"
          aria-describedby="ecorex-extension-manager-description"
          aria-busy={loadState === "loading"}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>扩展管理</Dialog.Title>
              <Dialog.Description id="ecorex-extension-manager-description">
                技能、扩展服务、工具和连接组件由 EcoreX 统一检查、启停和维护。
              </Dialog.Description>
            </div>
            <Dialog.Close className="ex-icon-button" aria-label="关闭扩展管理">
              <X aria-hidden="true" />
            </Dialog.Close>
          </div>

          <div className="ex-extension-toolbar">
            <div>
              <p aria-live="polite">
                {summary.total} 个扩展 · {summary.enabled} 个已启用
                {summary.quarantined ? ` · ${summary.quarantined} 个已隔离` : ""}
              </p>
              <span className={`ex-extension-freshness is-${loadState}`}>{catalogFreshness}</span>
              {operationBusy ? (
                <span className="ex-extension-freshness" id="extension-refresh-disabled-reason">
                  扩展操作执行中，完成后才能刷新目录。
                </span>
              ) : null}
            </div>
            <button
              className="ex-button"
              type="button"
              disabled={loadState === "loading" || operationBusy}
              aria-busy={loadState === "loading"}
              aria-describedby={operationBusy ? "extension-refresh-disabled-reason" : undefined}
              onClick={() => void onRefresh()}
            >
              <RefreshCw aria-hidden="true" />
              {loadState === "loading" ? "正在刷新" : "刷新目录"}
            </button>
          </div>

          <section className="ex-extension-install" aria-label="安装本地技能包">
            <div>
              <strong>安装本地技能包</strong>
              <p>只接受不超过 10 MB 的离线扩展包；EcoreX 会检查配置说明和文件类型，再安全保存。</p>
            </div>
            <label>
              <span>扩展 ID</span>
              <input
                type="text"
                value={localExtensionId}
                placeholder="local.office-helper"
                autoComplete="off"
                spellCheck={false}
                disabled={installBusy || operationBusy}
                onChange={(event) => setLocalExtensionId(event.target.value)}
              />
            </label>
            <label className="ex-extension-file">
              <span>技能安装包</span>
              <input
                key={installInputKey}
                type="file"
                accept=".zip,application/zip"
                disabled={installBusy || operationBusy}
                onChange={(event) => setLocalBundle(event.target.files?.[0] ?? null)}
              />
            </label>
            <button
              className="ex-button"
              type="button"
              disabled={
                !catalogReady
                || installBusy
                || operationBusy
                || !localExtensionId.trim()
                || !localBundle
              }
              aria-busy={installBusy}
              onClick={() => {
                if (!localBundle) return;
                void onInstallLocalSkill(localExtensionId, localBundle).then((installed) => {
                  if (!installed) return;
                  setLocalExtensionId("");
                  setLocalBundle(null);
                  setInstallInputKey((value) => value + 1);
                });
              }}
            >
              <Upload aria-hidden="true" />
              {installBusy ? "正在验证" : "验证并安装"}
            </button>
          </section>

          {snapshot?.items.length ? (
            <div className="ex-extension-filters" role="search" aria-label="筛选扩展目录">
              <label className="ex-extension-search">
                <span>搜索扩展</span>
                <span>
                  <Search aria-hidden="true" />
                  <input
                    type="search"
                    value={query}
                    placeholder="名称或扩展 ID"
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </span>
              </label>
              <label>
                <span>类型</span>
                <select
                  value={kindFilter}
                  onChange={(event) => setKindFilter(event.target.value as ExtensionKindFilter)}
                >
                  <option value="all">全部类型</option>
                  <option value="skill">技能</option>
                  <option value="mcp_server">扩展服务</option>
                  <option value="tool_provider">工具组件</option>
                  <option value="connector_provider">连接组件</option>
                  <option value="capability_pack">能力组件</option>
                </select>
              </label>
              <label>
                <span>状态</span>
                <select
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value as ExtensionStatusFilter)}
                >
                  <option value="all">全部状态</option>
                  <option value="staged">已暂存</option>
                  <option value="enabled">已启用</option>
                  <option value="disabled">已停用</option>
                  <option value="quarantined">已隔离</option>
                </select>
              </label>
              <span className="ex-extension-result-count" aria-live="polite">
                显示 {visibleExtensions.length} 个
              </span>
            </div>
          ) : null}

          {error ? (
            <div className="ex-settings-error ex-extension-error" role="alert">
              <AlertCircle aria-hidden="true" />
              <span>{error}</span>
              <button
                className="ex-icon-button"
                type="button"
                aria-label="关闭扩展错误"
                onClick={onClearError}
              >
                <X aria-hidden="true" />
              </button>
            </div>
          ) : null}

          {!snapshot && loadState === "loading" ? (
            <div className="ex-extension-skeleton" aria-label="正在加载扩展目录">
              {[0, 1, 2].map((item) => <span key={item} />)}
            </div>
          ) : null}

          {snapshot && visibleExtensions.length ? (
            <div className="ex-extension-list">
              {visibleExtensions.map((extension) => (
                <ExtensionRow
                  key={extension.extension_id}
                  extension={extension}
                  operation={operations[extension.extension_id]}
                  registryOperationBusy={operationBusy}
                  catalogReady={catalogReady}
                  catalogDisabledReason={catalogDisabledReason}
                  detailsOpen={selectedExtensionId === extension.extension_id}
                  pendingConfirmation={pendingConfirmation}
                  onToggleDetails={() => setSelectedExtensionId((current) => (
                    current === extension.extension_id ? null : extension.extension_id
                  ))}
                  onRequestAction={requestAction}
                  onCancelConfirmation={() => setPendingConfirmation(null)}
                  onConfirmAction={confirmAction}
                />
              ))}
            </div>
          ) : null}

          {snapshot?.items.length && !visibleExtensions.length ? (
            <div className="ex-extension-empty ex-extension-filter-empty">
              <PackageSearch aria-hidden="true" />
              <strong>没有匹配的扩展</strong>
              <p>搜索会匹配扩展名称和标识；筛选会匹配所选类型与状态。</p>
              <button
                className="ex-button"
                type="button"
                onClick={() => {
                  setQuery("");
                  setKindFilter("all");
                  setStatusFilter("all");
                }}
              >
                清除筛选
              </button>
            </div>
          ) : null}

          {snapshot && !snapshot.items.length && loadState !== "loading" ? (
            <div className="ex-extension-empty">
              <PackageSearch aria-hidden="true" />
              <strong>当前没有可管理的扩展</strong>
              <p>扩展只能通过经过验证的核心包、托管发布或迁移流程进入目录。</p>
              <button className="ex-button" type="button" onClick={() => void onRefresh()}>
                刷新目录
              </button>
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
