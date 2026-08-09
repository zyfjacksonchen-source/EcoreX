import * as Dialog from "@radix-ui/react-dialog";
import {
  ArrowLeft,
  Blocks,
  BookOpenText,
  Bot,
  Braces,
  CheckCircle2,
  Database,
  Download,
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
import {
  ConnectorCatalogPanel,
  type ConnectorCatalogPanelProps,
} from "./ConnectorPopover.tsx";
import { UserMCPPanel } from "./UserMCPPanel.tsx";

import type {
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionProjection,
  MCPOAuthStatusProjection,
  SkillHubCardProjection,
  SkillHubDetailProjection,
} from "../api/contracts.ts";
import type { RuntimeClient } from "../api/runtimeClient.ts";
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
  openChannelsKey?: number;
  connectorRuntime: ConnectorCatalogPanelProps;
  mcpClient: RuntimeClient;
  snapshot: ExtensionCatalogSnapshot | null;
  loadState: ExtensionLoadState;
  error: string | null;
  operations: Record<string, ExtensionOperationState>;
  installBusy: boolean;
  onClearError: () => void;
  onRefresh: () => Promise<ExtensionCatalogSnapshot | null>;
  onAction: (extension: ExtensionProjection, actionId: ExtensionActionId) => Promise<boolean>;
  onConfigure: (extension: ExtensionProjection, values: Record<string, string>) => Promise<boolean>;
  onInstallLocalSkill: (file: File) => Promise<boolean>;
  mcpOAuthStatuses: Record<string, MCPOAuthStatusProjection>;
  mcpOAuthBusy: string | null;
  onRefreshMcpOAuth: () => Promise<Record<string, MCPOAuthStatusProjection>>;
  onBeginMcpOAuth: (serviceId: string) => Promise<boolean>;
  onClearMcpOAuth: (serviceId: string) => Promise<boolean>;
  hubItems: SkillHubCardProjection[];
  hubState: ExtensionLoadState;
  hubError: string | null;
  hubInstallingSlug: string | null;
  hubDownloadingSlug: string | null;
  hubDetail: SkillHubDetailProjection | null;
  hubDetailLoadingSlug: string | null;
  hubUploadBusy: boolean;
  onRefreshHub: (query?: string, category?: SkillHubCardProjection["category"] | null, tag?: string | null, source?: string | null, signal?: AbortSignal) => Promise<boolean>;
  onInstallHub: (card: SkillHubCardProjection) => Promise<boolean>;
  onDownloadHub: (card: SkillHubCardProjection) => Promise<boolean>;
  onLoadHubDetail: (card: SkillHubCardProjection) => Promise<boolean>;
  onClearHubDetail: () => void;
  onPublishHub: (slug: string, category: SkillHubCardProjection["category"], file: File) => Promise<boolean>;
}

type WorkspaceTab = "discover" | "installed" | "custom";
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
  collaboration: "通道",
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
  openChannelsKey = 0,
  connectorRuntime,
  mcpClient,
  snapshot,
  loadState,
  error,
  operations,
  installBusy,
  onClearError,
  onRefresh,
  onAction,
  onConfigure,
  onInstallLocalSkill,
  mcpOAuthStatuses,
  mcpOAuthBusy,
  onRefreshMcpOAuth,
  onBeginMcpOAuth,
  onClearMcpOAuth,
  hubItems,
  hubState,
  hubError,
  hubInstallingSlug,
  hubDownloadingSlug,
  hubDetail,
  hubDetailLoadingSlug,
  hubUploadBusy,
  onRefreshHub,
  onInstallHub,
  onDownloadHub,
  onLoadHubDetail,
  onClearHubDetail,
  onPublishHub,
}: SkillsWorkspaceProps) {
  const [tab, setTab] = useState<WorkspaceTab>("discover");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<SkillCategory | "all">("all");
  const [hubCategory, setHubCategory] = useState<SkillHubCardProjection["category"] | "all">("all");
  const [hubTag, setHubTag] = useState("");
  const [hubSource, setHubSource] = useState("");
  const [pending, setPending] = useState<{ extension: ExtensionProjection; actionId: ExtensionActionId } | null>(null);
  const [configuration, setConfiguration] = useState<Record<string, string>>({});
  const [localBundle, setLocalBundle] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [hubUploadSlug, setHubUploadSlug] = useState("");
  const [hubUploadCategory, setHubUploadCategory] = useState<SkillHubCardProjection["category"]>("office_productivity");
  const [hubUploadBundle, setHubUploadBundle] = useState<File | null>(null);
  const [selectedHubCard, setSelectedHubCard] = useState<SkillHubCardProjection | null>(null);
  const [hubUploadFileKey, setHubUploadFileKey] = useState(0);
  const items = snapshot?.items ?? [];
  const operationBusy = Object.keys(operations).length > 0;
  const catalogReady = loadState === "ready";
  const selected = selectedId ? items.find((item) => item.extension_id === selectedId) ?? null : null;
  const selectedMcpOAuth = selected ? mcpOAuthStatuses[selected.extension_id] ?? null : null;
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
    void onRefreshMcpOAuth();
  }, [onRefresh, onRefreshMcpOAuth]);

  useEffect(() => {
    if (!openChannelsKey) return;
    setTab("installed");
    setCategory("collaboration");
    setSelectedId(null);
  }, [openChannelsKey]);

  useEffect(() => {
    if (tab !== "discover") return;
    const controller = new AbortController();
    void onRefreshHub(
      query,
      hubCategory === "all" ? null : hubCategory,
      hubTag.trim() || null,
      hubSource.trim() || null,
      controller.signal,
    );
    return () => controller.abort();
  }, [hubCategory, hubSource, hubTag, onRefreshHub, query, tab]);

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
    const uninstallAction = selected.actions.find((candidate) => candidate.action_id === "uninstall") ?? null;
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
          <div className="ex-skill-detail-actions">
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
            {uninstallAction ? (
              <button
                className="ex-button is-danger"
                type="button"
                disabled={!catalogReady || operationBusy || !uninstallAction.enabled}
                title={extensionActionDisabledReason(uninstallAction) || undefined}
                onClick={() => requestAction(selected, "uninstall")}
              >
                卸载
              </button>
            ) : null}
          </div>
        </div>
        <div className="ex-skill-detail-grid">
          <section>
            <h2>状态</h2>
            <dl>
              <div><dt>当前状态</dt><dd>{extensionStatusLabel(selected.status)}</dd></div>
              <div><dt>健康状态</dt><dd>{extensionHealthLabel(selected.health)}</dd></div>
              <div><dt>可用状态</dt><dd>{{ ready: "可使用", needs_configuration: "需要配置", missing_runtime: "缺少运行环境", unsupported: "当前设备不支持" }[selected.readiness]}</dd></div>
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
          {selectedMcpOAuth ? (
            <section className="ex-mcp-oauth" aria-live="polite">
              <div>
                <h2>远程服务授权</h2>
                <p>{selectedMcpOAuth.state === "authorized"
                  ? "已通过标准 OAuth 安全连接，令牌保存在系统凭据库。"
                  : selectedMcpOAuth.state === "authorizing"
                    ? "授权窗口已打开，完成登录后会自动同步。"
                    : "此远程服务需要登录授权，不需要手动复制令牌。"}</p>
                {selectedMcpOAuth.scope ? <small>授权范围：{selectedMcpOAuth.scope}</small> : null}
              </div>
              <div className="ex-mcp-oauth-actions">
                <button className="ex-button" type="button" disabled={mcpOAuthBusy === selected.extension_id} onClick={() => void onRefreshMcpOAuth()}>
                  <RefreshCw className={mcpOAuthBusy === selected.extension_id ? "ex-spin" : ""} aria-hidden="true" />刷新授权状态
                </button>
                {selectedMcpOAuth.state === "authorized" ? (
                  <button className="ex-button is-danger" type="button" disabled={Boolean(mcpOAuthBusy)} onClick={() => void onClearMcpOAuth(selected.extension_id)}>
                    {mcpOAuthBusy === selected.extension_id ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}取消授权
                  </button>
                ) : (
                  <button className="ex-button is-primary" type="button" disabled={Boolean(mcpOAuthBusy)} onClick={() => void onBeginMcpOAuth(selected.extension_id)}>
                    {mcpOAuthBusy === selected.extension_id ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}去授权
                  </button>
                )}
              </div>
            </section>
          ) : null}
          {selected.readiness === "needs_configuration" ? (
            <section className="ex-skill-configuration">
              <h2>配置</h2>
              {selected.requirements.filter((item) => item.startsWith("environment:")).map((item) => {
                const key = item.slice("environment:".length);
                return (
                  <label key={key}>
                    <span>{key}</span>
                    <input
                      type="password"
                      autoComplete="off"
                      value={configuration[key] ?? ""}
                      onChange={(event) => setConfiguration((current) => ({ ...current, [key]: event.target.value }))}
                    />
                  </label>
                );
              })}
              <button
                className="ex-button is-primary"
                type="button"
                disabled={operationBusy || selected.requirements.some((item) => item.startsWith("environment:") && !configuration[item.slice("environment:".length)])}
                onClick={() => void onConfigure(selected, configuration).then((saved) => { if (saved) setConfiguration({}); })}
              >
                {operations[selected.extension_id] ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}
                保存配置
              </button>
            </section>
          ) : null}
        </div>
      </section>
    );
  }

  return (
    <section className="ex-skills-workspace" aria-label="能力中心">
      <header className="ex-skills-header">
        <div>
          <h1>能力中心</h1>
          <p>发现、安装并管理 e-Mate 的办公能力。</p>
        </div>
        <IconButton label={loadState === "loading" ? "正在刷新技能" : "刷新技能"} disabled={loadState === "loading" || operationBusy} onClick={() => void onRefresh()}>
          <RefreshCw className={loadState === "loading" ? "ex-spin" : ""} aria-hidden="true" />
        </IconButton>
      </header>

      <div className="ex-skills-tabs" role="tablist" aria-label="能力中心页面">
        <button type="button" role="tab" aria-selected={tab === "discover"} onClick={() => setTab("discover")}>发现</button>
        <button type="button" role="tab" aria-selected={tab === "installed"} onClick={() => setTab("installed")}>
          已安装 <span>{items.length}</span>
        </button>
        <button type="button" role="tab" aria-selected={tab === "custom"} onClick={() => setTab("custom")}>导入</button>
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
          </div>
          <div className="ex-skill-category-filter" role="group" aria-label="技能分类">
            <button className="ex-button" type="button" aria-pressed={category === "all"} onClick={() => setCategory("all")}>
              <Blocks aria-hidden="true" /><span>全部分类</span>
            </button>
            {CATEGORY_ORDER.map((item) => (
              <button className="ex-button" type="button" key={item} aria-pressed={category === item} onClick={() => setCategory(item)}>
                {categoryIcon(item)}<span>{CATEGORY_LABEL[item]}</span>
              </button>
            ))}
          </div>
          {category === "collaboration" ? (
            <>
              <section aria-label="能力中心通道" data-testid="capability-channels">
                <ConnectorCatalogPanel {...connectorRuntime} />
              </section>
              <UserMCPPanel
                client={mcpClient}
                oauthStatuses={mcpOAuthStatuses}
                oauthBusy={mcpOAuthBusy}
                onRefreshOAuth={onRefreshMcpOAuth}
                onBeginOAuth={onBeginMcpOAuth}
                onClearOAuth={onClearMcpOAuth}
              />
            </>
          ) : null}
          {loadState === "loading" && !snapshot ? <p className="ex-skills-loading"><LoaderCircle className="ex-spin" aria-hidden="true" />正在读取技能目录…</p> : null}
          {visibleItems.length ? <div className="ex-skill-grid">{visibleItems.map(renderCard)}</div> : snapshot && category !== "collaboration" ? (
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
      ) : tab === "custom" ? (
        <div className="ex-skills-content ex-skill-market">
          <section className="ex-local-skill-install" aria-label="安装本地技能包">
            <div>
              <FileArchive aria-hidden="true" />
              <span><strong>导入本地 Skill</strong><small>选择不超过 10 MB 的 ZIP；e-Mate 会自动识别名称并完成安全检查。</small></span>
            </div>
            <label><span>Skill 安装包</span><input key={fileInputKey} type="file" accept=".zip,application/zip" disabled={installBusy || operationBusy} onChange={(event) => setLocalBundle(event.target.files?.[0] ?? null)} /></label>
            <button
              className="ex-button is-primary"
              type="button"
              disabled={!catalogReady || installBusy || operationBusy || !localBundle}
              onClick={() => {
                if (!localBundle) return;
                void onInstallLocalSkill(localBundle).then((installed) => {
                  if (!installed) return;
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
          <details className="ex-skill-advanced">
            <summary>高级操作</summary>
            <section className="ex-local-skill-install" aria-label="发布 Skill 到 e-Mate">
              <div>
                <Upload aria-hidden="true" />
                <span><strong>发布到 e-Mate Skill Hub</strong><small>通过检查后自动公开；同一 slug 与版本不可覆盖。</small></span>
              </div>
              <label><span>Skill slug</span><input value={hubUploadSlug} placeholder="office-helper" disabled={hubUploadBusy} onChange={(event) => setHubUploadSlug(event.target.value)} /></label>
              <label><span>市场分类</span><select value={hubUploadCategory} disabled={hubUploadBusy} onChange={(event) => setHubUploadCategory(event.target.value as SkillHubCardProjection["category"])}><option value="third_party">第三方</option><option value="content_creation">内容创作</option><option value="office_productivity">办公效率</option></select></label>
              <label><span>选择发布包</span><input key={hubUploadFileKey} type="file" accept=".zip,application/zip" disabled={hubUploadBusy} onChange={(event) => setHubUploadBundle(event.target.files?.[0] ?? null)} /></label>
              <button
                className="ex-button is-primary"
                type="button"
                disabled={hubUploadBusy || !hubUploadSlug.trim() || !hubUploadBundle}
                onClick={() => {
                  if (!hubUploadBundle) return;
                  void onPublishHub(hubUploadSlug, hubUploadCategory, hubUploadBundle).then((published) => {
                    if (!published) return;
                    setHubUploadSlug("");
                    setHubUploadBundle(null);
                    setHubUploadFileKey((value) => value + 1);
                    setTab("discover");
                  });
                }}
              >
                {hubUploadBusy ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Upload aria-hidden="true" />}
                {hubUploadBusy ? "正在发布" : "验证并发布"}
              </button>
            </section>
          </details>
        </div>
      ) : (
        <div className="ex-skills-content">
          <div className="ex-skills-filter-row">
            <label className="ex-skills-search">
              <Search aria-hidden="true" /><input type="search" value={query} placeholder="搜索 Skill Hub" onChange={(event) => setQuery(event.target.value)} />
            </label>
            <select aria-label="市场分类" value={hubCategory} onChange={(event) => setHubCategory(event.target.value as typeof hubCategory)}>
              <option value="all">全部市场</option>
              <option value="third_party">第三方</option>
              <option value="content_creation">内容创作</option>
              <option value="office_productivity">办公效率</option>
            </select>
            <input aria-label="按标签筛选" value={hubTag} placeholder="标签" onChange={(event) => setHubTag(event.target.value)} />
            <input aria-label="按原始来源筛选" value={hubSource} placeholder="原始来源" onChange={(event) => setHubSource(event.target.value)} />
            <button className="ex-button" type="button" onClick={() => setTab("custom")}><Upload aria-hidden="true" />上传 Skill</button>
          </div>
          {hubError ? <div className="ex-skills-error" role="alert"><span>{hubError}</span></div> : null}
          {hubState === "loading" && !hubItems.length ? <p className="ex-skills-loading"><LoaderCircle className="ex-spin" aria-hidden="true" />正在读取 e-Mate Skill Hub…</p> : null}
          {hubItems.length ? (
            <div className="ex-hub-grid">
              {hubItems.map((card) => {
                const installed = card.installation_status === "installed_enabled";
                const busy = hubInstallingSlug === card.slug;
                return (
                  <article className="ex-hub-card" key={`${card.slug}:${card.version}`}>
                    <div className="ex-hub-card-head"><PackageSearch aria-hidden="true" /><span><strong>{card.title}</strong><small>{card.slug}</small></span></div>
                    <p>{card.summary}</p>
                    <div className="ex-hub-tags">{card.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                    <footer>
                      <span>v{card.version} · e-Mate · {card.uploader.nickname}</span>
                      <div className="ex-hub-actions">
                        <button className="ex-button" type="button" disabled={hubDetailLoadingSlug === card.slug} onClick={() => { setSelectedHubCard(card); void onLoadHubDetail(card); }}>
                          {hubDetailLoadingSlug === card.slug ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}查看详情
                        </button>
                        <button className={`ex-button${installed ? "" : " is-primary"}`} type="button" disabled={busy || installed || card.readiness === "unsupported"} onClick={() => void onInstallHub(card)}>
                          {busy ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : null}{installed ? "已启用" : busy ? "安装中" : "安装并启用"}
                        </button>
                      </div>
                    </footer>
                  </article>
                );
              })}
            </div>
          ) : hubState === "ready" ? <div className="ex-skills-empty"><PackageSearch aria-hidden="true" /><strong>没有匹配的 Skill</strong><p>换个关键词或市场分类再试。</p></div> : null}
        </div>
      )}

      <Dialog.Root open={Boolean(selectedHubCard)} onOpenChange={(open) => { if (!open) { setSelectedHubCard(null); onClearHubDetail(); } }}>
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content className="ex-dialog ex-skill-detail" aria-describedby="ex-hub-detail-description">
            <Dialog.Title>{selectedHubCard?.title ?? "Skill 详情"}</Dialog.Title>
            <Dialog.Description id="ex-hub-detail-description">{selectedHubCard?.summary ?? ""}</Dialog.Description>
            {selectedHubCard ? (
              <>
                <dl className="ex-skill-detail-list">
                  <div><dt>slug</dt><dd>{selectedHubCard.slug}</dd></div>
                  <div><dt>版本</dt><dd>{selectedHubCard.version}</dd></div>
                  <div><dt>作者</dt><dd>{selectedHubCard.uploader.nickname}</dd></div>
                  <div><dt>就绪状态</dt><dd>{selectedHubCard.readiness}</dd></div>
                  <div><dt>内容摘要</dt><dd><code>{selectedHubCard.package_sha256}</code></dd></div>
                  <div><dt>原始来源</dt><dd>{selectedHubCard.provenance.original_url ? <a href={selectedHubCard.provenance.original_url} target="_blank" rel="noreferrer">{selectedHubCard.provenance.original_platform ?? "查看来源"}</a> : "e-Mate"}</dd></div>
                </dl>
                {hubDetail ? (
                  <div className="ex-hub-versions">
                    <strong>版本历史</strong>
                    <ul>{hubDetail.versions.map((item) => <li key={item.version}><span>v{item.version}</span><code>{item.package_sha256.slice(0, 12)}…</code></li>)}</ul>
                  </div>
                ) : hubDetailLoadingSlug ? <p className="ex-skills-loading"><LoaderCircle className="ex-spin" aria-hidden="true" />正在读取版本历史…</p> : null}
                <p className="ex-hub-install-note">WebUI 会在当前设备 Runtime 内创建绑定账号、版本和摘要的单次安装意图，无需桌面协议。</p>
                <div className="ex-dialog-actions">
                  <button className="ex-button" type="button" disabled={hubDownloadingSlug === selectedHubCard.slug} onClick={() => void onDownloadHub(selectedHubCard)}>
                    {hubDownloadingSlug === selectedHubCard.slug ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
                    {hubDownloadingSlug === selectedHubCard.slug ? "下载中" : "下载 ZIP"}
                  </button>
                  <button className="ex-button is-primary" type="button" disabled={selectedHubCard.installation_status === "installed_enabled" || hubInstallingSlug === selectedHubCard.slug || selectedHubCard.readiness === "unsupported"} onClick={() => void onInstallHub(selectedHubCard)}>
                    {selectedHubCard.installation_status === "installed_enabled" ? "已启用" : hubInstallingSlug === selectedHubCard.slug ? "安装中" : "安装并启用"}
                  </button>
                </div>
              </>
            ) : null}
            <Dialog.Close className="ex-dialog-close" aria-label="关闭 Skill 详情"><X aria-hidden="true" /></Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

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
