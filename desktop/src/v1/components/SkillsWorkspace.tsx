import * as Dialog from "@radix-ui/react-dialog";
import {
  ArrowLeft,
  Blocks,
  BookOpenText,
  Bot,
  Braces,
  CheckCircle2,
  Database,
  FileArchive,
  Image,
  LoaderCircle,
  Network,
  PackageSearch,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import tencentDocsIcon from "../../../public/assets/logos/tencent-docs.png";

import type {
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionProjection,
} from "../api/contracts.ts";
import {
  extensionActionDisabledReason,
  extensionActionLabel,
  extensionExportKindLabel,
  extensionExposureLabel,
  extensionHealthLabel,
  extensionKindLabel,
  extensionPermissionEffectLabel,
  extensionSourceLabel,
  extensionStatusLabel,
  extensionTrustLabel,
  type ExtensionLoadState,
  type ExtensionOperationState,
} from "../state/extensions.ts";
import { IconButton } from "./IconButton.tsx";

interface SkillsWorkspaceProps {
  snapshot: ExtensionCatalogSnapshot | null;
  loadState: ExtensionLoadState;
  error: string | null;
  operations: Record<string, ExtensionOperationState>;
  installBusy: boolean;
  onClearError: () => void;
  onRefresh: () => Promise<ExtensionCatalogSnapshot | null>;
  onAction: (extension: ExtensionProjection, actionId: ExtensionActionId) => Promise<boolean>;
  onInstallLocalSkill: (extensionId: string, file: File) => Promise<boolean>;
}

type WorkspaceTab = "market" | "installed";
type SkillCategory = ExtensionProjection["category"];

const CATEGORY_ORDER: SkillCategory[] = [
  "system",
  "office",
  "image_media",
  "collaboration",
  "data",
  "development",
  "automation",
  "general",
];

const CATEGORY_LABEL: Record<SkillCategory, string> = {
  system: "系统能力",
  office: "办公能力",
  image_media: "图像 / 媒体",
  collaboration: "协作连接",
  data: "数据能力",
  development: "开发能力",
  automation: "自动化",
  general: "通用能力",
};

function categoryIcon(category: SkillCategory): ReactNode {
  if (category === "system") return <Bot aria-hidden="true" />;
  if (category === "office") return <BookOpenText aria-hidden="true" />;
  if (category === "image_media") return <Image aria-hidden="true" />;
  if (category === "collaboration") return <Network aria-hidden="true" />;
  if (category === "data") return <Database aria-hidden="true" />;
  if (category === "development") return <Braces aria-hidden="true" />;
  if (category === "automation") return <Workflow aria-hidden="true" />;
  return <Sparkles aria-hidden="true" />;
}

function actionForStatus(extension: ExtensionProjection) {
  const actionId: ExtensionActionId = extension.status === "enabled" ? "disable" : "enable";
  const action = extension.actions.find((candidate) => candidate.action_id === actionId) ?? null;
  return { actionId, action };
}

function protectedExtension(extension: ExtensionProjection): boolean {
  if (extension.status !== "enabled") return false;
  const disable = extension.actions.find((action) => action.action_id === "disable");
  return disable?.disabled_reason === "extension_required_by_product";
}

function SkillAvatar({ extension }: { extension: ExtensionProjection }) {
  return (
    <span className={`ex-skill-avatar is-${extension.category}`}>
      {extension.icon_key === "tencent"
        ? <img src={tencentDocsIcon} alt="" />
        : categoryIcon(extension.category)}
    </span>
  );
}

function SkillSwitch({
  extension,
  operation,
  registryBusy,
  catalogReady,
  onRequest,
}: {
  extension: ExtensionProjection;
  operation: ExtensionOperationState | undefined;
  registryBusy: boolean;
  catalogReady: boolean;
  onRequest: (extension: ExtensionProjection, actionId: ExtensionActionId) => void;
}) {
  const { actionId, action } = actionForStatus(extension);
  const reason = action ? extensionActionDisabledReason(action) : "运行服务未提供这项操作。";
  const busy = Boolean(operation);
  return (
    <button
      className="ex-skill-switch"
      type="button"
      role="switch"
      aria-checked={extension.status === "enabled"}
      aria-label={`${extension.status === "enabled" ? "停用" : "启用"}${extension.display_name}`}
      aria-busy={busy}
      disabled={!catalogReady || registryBusy || busy || !action?.enabled}
      title={reason || undefined}
      onClick={(event) => {
        event.stopPropagation();
        onRequest(extension, actionId);
      }}
    >
      <span />
    </button>
  );
}

export function SkillsWorkspace({
  snapshot,
  loadState,
  error,
  operations,
  installBusy,
  onClearError,
  onRefresh,
  onAction,
  onInstallLocalSkill,
}: SkillsWorkspaceProps) {
  const [tab, setTab] = useState<WorkspaceTab>("installed");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<SkillCategory | "all">("all");
  const [pending, setPending] = useState<{ extension: ExtensionProjection; actionId: ExtensionActionId } | null>(null);
  const [localExtensionId, setLocalExtensionId] = useState("");
  const [localBundle, setLocalBundle] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const items = snapshot?.items ?? [];
  const operationBusy = Object.keys(operations).length > 0;
  const catalogReady = loadState === "ready";
  const selected = selectedId ? items.find((item) => item.extension_id === selectedId) ?? null : null;
  const protectedItems = items.filter(protectedExtension);
  const installedItems = items.filter((item) => !protectedExtension(item));
  const visibleItems = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("zh-CN");
    return installedItems.filter((extension) => (
      (category === "all" || extension.category === category)
      && (!needle || [extension.display_name, extension.extension_id, extension.description]
        .join(" ").toLocaleLowerCase("zh-CN").includes(needle))
    ));
  }, [category, installedItems, query]);

  useEffect(() => {
    void onRefresh();
  }, [onRefresh]);

  useEffect(() => {
    if (selectedId && !items.some((item) => item.extension_id === selectedId)) setSelectedId(null);
  }, [items, selectedId]);

  const requestAction = (extension: ExtensionProjection, actionId: ExtensionActionId) => {
    const action = extension.actions.find((candidate) => candidate.action_id === actionId);
    if (!catalogReady || !action?.enabled) return;
    if (action.requires_confirmation) {
      setPending({ extension, actionId });
      return;
    }
    void onAction(extension, actionId);
  };

  const renderCard = (extension: ExtensionProjection) => (
    <article className="ex-skill-card" key={extension.extension_id} data-status={extension.status}>
      <button className="ex-skill-card-main" type="button" onClick={() => setSelectedId(extension.extension_id)}>
        <SkillAvatar extension={extension} />
        <span className="ex-skill-card-copy">
          <strong>{extension.display_name}</strong>
          <small>{extension.description}</small>
          <em>{CATEGORY_LABEL[extension.category]} · {extensionKindLabel(extension.kind)}</em>
        </span>
      </button>
      <SkillSwitch
        extension={extension}
        operation={operations[extension.extension_id]}
        registryBusy={operationBusy}
        catalogReady={catalogReady}
        onRequest={requestAction}
      />
    </article>
  );

  if (selected) {
    const { actionId, action } = actionForStatus(selected);
    const updated = new Date(selected.updated_at);
    return (
      <section className="ex-skills-workspace ex-skill-detail" aria-label="技能详情">
        <header className="ex-skills-header">
          <button className="ex-skills-back" type="button" onClick={() => setSelectedId(null)}>
            <ArrowLeft aria-hidden="true" />返回技能
          </button>
        </header>
        <div className="ex-skill-detail-hero">
          <SkillAvatar extension={selected} />
          <div>
            <h1>{selected.display_name}</h1>
            <p>{selected.description}</p>
            <span>{extensionKindLabel(selected.kind)} · {CATEGORY_LABEL[selected.category]}</span>
          </div>
          <button
            className="ex-button is-primary"
            type="button"
            disabled={!catalogReady || operationBusy || !action?.enabled}
            title={action ? extensionActionDisabledReason(action) || undefined : "运行服务未提供这项操作。"}
            onClick={() => requestAction(selected, actionId)}
          >
            {operations[selected.extension_id] ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}
            {selected.status === "enabled" ? "停用" : "启用"}
          </button>
        </div>
        <div className="ex-skill-detail-grid">
          <section>
            <h2>状态</h2>
            <dl>
              <div><dt>当前状态</dt><dd>{extensionStatusLabel(selected.status)}</dd></div>
              <div><dt>健康状态</dt><dd>{extensionHealthLabel(selected.health)}</dd></div>
              <div><dt>版本</dt><dd>{selected.active_version ?? "尚未激活"}</dd></div>
              <div><dt>来源</dt><dd>{extensionSourceLabel(selected.source)}</dd></div>
              <div><dt>信任</dt><dd>{extensionTrustLabel(selected.trust)}</dd></div>
              <div><dt>更新时间</dt><dd>{Number.isFinite(updated.getTime()) ? updated.toLocaleString("zh-CN") : "未提供"}</dd></div>
            </dl>
          </section>
          <section>
            <h2>提供的能力</h2>
            {selected.exports.length ? (
              <ul className="ex-skill-capability-list">
                {selected.exports.map((item) => (
                  <li key={`${item.kind}:${item.export_id}`}>
                    <strong>{extensionExportKindLabel(item.kind)} · {item.export_id}</strong>
                    <span>{extensionExposureLabel(item.exposure)} · {item.permission_effects.length
                      ? item.permission_effects.map(extensionPermissionEffectLabel).join("、")
                      : "无额外权限"}</span>
                  </li>
                ))}
              </ul>
            ) : <p>当前版本未导出用户能力。</p>}
          </section>
          <section>
            <h2>依赖</h2>
            {selected.dependencies.length ? (
              <ul className="ex-skill-capability-list">
                {selected.dependencies.map((dependency) => (
                  <li key={`${dependency.extension_id}:${dependency.version_range}`}>
                    <strong>{dependency.extension_id}</strong><span>{dependency.version_range}</span>
                  </li>
                ))}
              </ul>
            ) : <p>没有其他扩展依赖。</p>}
          </section>
        </div>
      </section>
    );
  }

  return (
    <section className="ex-skills-workspace" aria-label="技能">
      <header className="ex-skills-header">
        <div>
          <h1>技能</h1>
          <p>统一管理技能、MCP、工具组件和能力包。</p>
        </div>
        <IconButton label={loadState === "loading" ? "正在刷新技能" : "刷新技能"} disabled={loadState === "loading" || operationBusy} onClick={() => void onRefresh()}>
          <RefreshCw className={loadState === "loading" ? "ex-spin" : ""} aria-hidden="true" />
        </IconButton>
      </header>

      <div className="ex-skills-tabs" role="tablist" aria-label="技能页面">
        <button type="button" role="tab" aria-selected={tab === "market"} onClick={() => setTab("market")}>技能市场</button>
        <button type="button" role="tab" aria-selected={tab === "installed"} onClick={() => setTab("installed")}>
          已安装 <span>{items.length}</span>
        </button>
      </div>

      {error ? (
        <div className="ex-skills-error" role="alert">
          <span>{error}</span><IconButton label="关闭技能错误" onClick={onClearError}><X aria-hidden="true" /></IconButton>
        </div>
      ) : null}

      {tab === "installed" ? (
        <div className="ex-skills-content">
          <div className="ex-skills-filter-row">
            <label className="ex-skills-search">
              <Search aria-hidden="true" /><input type="search" value={query} placeholder="搜索已安装技能" onChange={(event) => setQuery(event.target.value)} />
            </label>
            <select aria-label="技能分类" value={category} onChange={(event) => setCategory(event.target.value as SkillCategory | "all")}>
              <option value="all">全部分类</option>
              {CATEGORY_ORDER.map((item) => <option value={item} key={item}>{CATEGORY_LABEL[item]}</option>)}
            </select>
          </div>
          {loadState === "loading" && !snapshot ? <p className="ex-skills-loading"><LoaderCircle className="ex-spin" aria-hidden="true" />正在读取技能目录…</p> : null}
          {visibleItems.length ? <div className="ex-skill-grid">{visibleItems.map(renderCard)}</div> : snapshot ? (
            <div className="ex-skills-empty"><PackageSearch aria-hidden="true" /><strong>没有匹配的技能</strong><p>清除搜索或切换分类后再查看。</p></div>
          ) : null}
          {protectedItems.length ? (
            <details className="ex-protected-skills">
              <summary><ShieldCheck aria-hidden="true" /><span>系统必需技能</span><small>{protectedItems.length}</small></summary>
              <p>这些能力随 e-Mate 核心运行，不能由用户关闭。</p>
              <div className="ex-skill-grid">{protectedItems.map(renderCard)}</div>
            </details>
          ) : null}
        </div>
      ) : (
        <div className="ex-skills-content ex-skill-market">
          <div className="ex-skill-category-grid" aria-label="技能分类">
            {CATEGORY_ORDER.map((item) => (
              <button className={category === item ? "is-selected" : ""} type="button" key={item} onClick={() => setCategory(item)}>
                <span>{categoryIcon(item)}</span><strong>{CATEGORY_LABEL[item]}</strong>
                <small>{items.filter((extension) => extension.category === item).length} 个已安装</small>
              </button>
            ))}
          </div>
          <section className="ex-local-skill-install" aria-label="安装本地技能包">
            <div>
              <FileArchive aria-hidden="true" />
              <span><strong>安装本地技能包</strong><small>仅接受不超过 10 MB 的 ZIP；Runtime 会验证清单、内容和权限后再登记。</small></span>
            </div>
            <label><span>扩展 ID</span><input value={localExtensionId} placeholder="local.office-helper" disabled={installBusy || operationBusy} onChange={(event) => setLocalExtensionId(event.target.value)} /></label>
            <label><span>选择安装包</span><input key={fileInputKey} type="file" accept=".zip,application/zip" disabled={installBusy || operationBusy} onChange={(event) => setLocalBundle(event.target.files?.[0] ?? null)} /></label>
            <button
              className="ex-button is-primary"
              type="button"
              disabled={!catalogReady || installBusy || operationBusy || !localExtensionId.trim() || !localBundle}
              onClick={() => {
                if (!localBundle) return;
                void onInstallLocalSkill(localExtensionId, localBundle).then((installed) => {
                  if (!installed) return;
                  setLocalExtensionId("");
                  setLocalBundle(null);
                  setFileInputKey((value) => value + 1);
                  setTab("installed");
                });
              }}
            >
              {installBusy ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Upload aria-hidden="true" />}
              {installBusy ? "正在验证" : "验证并安装"}
            </button>
          </section>
        </div>
      )}

      <Dialog.Root open={Boolean(pending)} onOpenChange={(open) => { if (!open) setPending(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content className="ex-dialog ex-confirm-dialog" aria-describedby="ex-skill-action-description">
            <Dialog.Title>确认{pending ? extensionActionLabel(pending.actionId) : "操作"}</Dialog.Title>
            <Dialog.Description id="ex-skill-action-description">
              {pending ? `${pending.extension.display_name} 的来源、依赖、权限和当前版本会由 Runtime 再次检查。` : ""}
            </Dialog.Description>
            <div className="ex-dialog-actions">
              <Dialog.Close className="ex-button" type="button">取消</Dialog.Close>
              <button className="ex-button is-primary" type="button" onClick={() => {
                if (!pending) return;
                const current = pending;
                void onAction(current.extension, current.actionId).then((completed) => {
                  if (completed) setPending(null);
                });
              }}>
                {pending ? `确认${extensionActionLabel(pending.actionId)}` : "确认"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
