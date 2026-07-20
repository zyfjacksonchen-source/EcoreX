import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import {
  AlertCircle,
  Copy,
  Ellipsis,
  Folder,
  History,
  Menu,
  Moon,
  RefreshCw,
  Settings2,
  Share2,
  Sun,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import type { ArtifactProjection } from "./api/contracts.ts";

import { Composer } from "./components/Composer.tsx";
import { IconButton } from "./components/IconButton.tsx";
import { LazyFeatureBoundary } from "./components/LazyFeatureBoundary.tsx";
import { LoginPage } from "./components/LoginPage.tsx";
import { Timeline } from "./components/Timeline.tsx";
import { useRuntimeSession } from "./state/useRuntimeSession.ts";
import {
  resolveThemePreference,
  THEME_PREFERENCE_KEY,
  type ThemePreference,
} from "./state/themePreference.ts";
import { serviceReasonMessage } from "./state/userLanguage.ts";
import { hasPendingRuntimeUpdate } from "./state/updatePresentation.ts";
import "./styles/primitives.css";
import "./styles/layout.css";
import "./styles/features.css";
import "./styles/plain-language.css";

const loadArtifactPreviewDialog = () => import("./components/ArtifactPreviewDialog.tsx");
const loadSidebar = () => import("./components/Sidebar.tsx");
const loadSkillsWorkspace = () => import("./components/SkillsWorkspace.tsx");
const loadInteractionStack = () => import("./components/InteractionStack.tsx");
const loadReplayDialog = () => import("./components/ReplayDialog.tsx");
const loadRetouchWorkspace = () => import("./components/RetouchWorkspace.tsx");
const loadSettingsDialog = () => import("./components/SettingsDialog.tsx");
const loadShareDialog = () => import("./components/ShareDialog.tsx");

const ArtifactPreviewDialog = lazy(async () => ({
  default: (await loadArtifactPreviewDialog()).ArtifactPreviewDialog,
}));
const Sidebar = lazy(async () => ({
  default: (await loadSidebar()).Sidebar,
}));
const SkillsWorkspace = lazy(async () => ({
  default: (await loadSkillsWorkspace()).SkillsWorkspace,
}));
const InteractionStack = lazy(async () => ({
  default: (await loadInteractionStack()).InteractionStack,
}));
const ReplayDialog = lazy(async () => ({
  default: (await loadReplayDialog()).ReplayDialog,
}));
const RetouchWorkspace = lazy(async () => ({
  default: (await loadRetouchWorkspace()).RetouchWorkspace,
}));
const SettingsDialog = lazy(async () => ({
  default: (await loadSettingsDialog()).SettingsDialog,
}));
const ShareDialog = lazy(async () => ({
  default: (await loadShareDialog()).ShareDialog,
}));

function warmFeature(loader: () => Promise<unknown>): void {
  void loader().catch(() => undefined);
}

function modelUnavailableMessage(authenticated: boolean, reason: string | null | undefined): string {
  if (!authenticated) {
    return "尚未登录。登录后可使用模型；历史和本地产物仍可查看。";
  }
  if (reason === "managed_session_unavailable") {
    return "账号授权已过期，请重新登录。";
  }
  if (reason === "managed_gateway_not_configured") {
    return "模型服务未就绪，请联系管理员。";
  }
  if (reason === "signed_model_allowlist_empty") {
    return "无可用模型，请联系管理员。";
  }
  return "模型暂不可用；历史和本地产物仍可使用。";
}

function outputLocationLabel(alias: "documents" | "downloads" | "workspace"): string {
  if (alias === "documents") return "文档";
  if (alias === "downloads") return "下载";
  return "当前工作区";
}

function useMediaMatch(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

const DISMISSED_UPDATE_BANNERS_KEY = "ecorex-dismissed-update-banners";

function initialDismissedUpdateBanners(): string[] {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(DISMISSED_UPDATE_BANNERS_KEY) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return [];
  }
}

export function AppV1() {
  const runtime = useRuntimeSession();
  const mobileNavigation = useMediaMatch("(max-width: 839px)");
  const [theme, setTheme] = useState<ThemePreference>(() => {
    try {
      return resolveThemePreference(window.localStorage.getItem(THEME_PREFERENCE_KEY));
    } catch {
      return "dark";
    }
  });
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsReturnFocusRef = useRef<HTMLElement | null>(null);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const shareReturnFocusRef = useRef<HTMLElement | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const replayReturnFocusRef = useRef<HTMLElement | null>(null);
  const [previewArtifact, setPreviewArtifact] = useState<ArtifactProjection | null>(null);
  const previewReturnFocusRef = useRef<HTMLElement | null>(null);
  const [retouchArtifact, setRetouchArtifact] = useState<ArtifactProjection | null>(null);
  const retouchReturnFocusRef = useRef<HTMLElement | null>(null);
  const [artifactNotice, setArtifactNotice] = useState<string | null>(null);
  const [dismissedUpdateBanners, setDismissedUpdateBanners] = useState(
    initialDismissedUpdateBanners,
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(THEME_PREFERENCE_KEY, theme);
    } catch {
      // The current session remains themed even when persistent storage is unavailable.
    }
  }, [theme]);

  const currentThreadId = runtime.state.thread?.thread_id ?? null;
  const copyCurrentThreadId = async () => {
    if (!currentThreadId) return;
    try {
      await navigator.clipboard.writeText(currentThreadId);
      setArtifactNotice("任务 ID 已复制。");
    } catch {
      setArtifactNotice(`未能自动复制。任务 ID：${currentThreadId}`);
    }
  };
  useEffect(() => {
    setPreviewArtifact(null);
    setRetouchArtifact(null);
    setArtifactNotice(null);
    previewReturnFocusRef.current = null;
    settingsReturnFocusRef.current = null;
    shareReturnFocusRef.current = null;
    replayReturnFocusRef.current = null;
    retouchReturnFocusRef.current = null;
    setReplayOpen(false);
    if (!currentThreadId) setShareOpen(false);
  }, [currentThreadId]);

  const captureFeatureTrigger = (target: { current: HTMLElement | null }) => {
    target.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  };

  const focusFeatureTarget = (candidate: HTMLElement | null): boolean => {
    if (
      !candidate?.isConnected
      || candidate === document.body
      || candidate === document.documentElement
      || candidate.matches(":disabled, [aria-disabled=\"true\"]")
      || candidate.closest("[inert], [hidden], [aria-hidden=\"true\"]")
    ) return false;
    try {
      candidate.focus({ preventScroll: true });
    } catch {
      return false;
    }
    return document.activeElement === candidate;
  };

  const restoreFeatureFocus = (
    previous: HTMLElement | null,
    fallbackSelectors: readonly string[],
  ) => {
    if (focusFeatureTarget(previous)) return;
    for (const selector of fallbackSelectors) {
      const candidates = document.querySelectorAll<HTMLElement>(selector);
      for (const candidate of candidates) {
        if (focusFeatureTarget(candidate)) return;
      }
    }
  };

  const scheduleFeatureFocusRestore = (
    target: { current: HTMLElement | null },
    fallbackSelectors: readonly string[] = [],
  ) => {
    const previous = target.current;
    target.current = null;
    window.requestAnimationFrame(() => {
      restoreFeatureFocus(previous, fallbackSelectors);
    });
  };

  const closeSettings = () => {
    setSettingsOpen(false);
    scheduleFeatureFocusRestore(
      settingsReturnFocusRef,
      [
        '[data-ecorex-feature-trigger="task-menu"]',
        '[data-ecorex-feature-trigger="navigation"]',
      ],
    );
  };

  const closeShare = () => {
    setShareOpen(false);
    scheduleFeatureFocusRestore(
      shareReturnFocusRef,
      [
        '[data-ecorex-feature-trigger="share"]',
        '[data-ecorex-feature-trigger="task-menu"]',
        '[data-ecorex-feature-trigger="navigation"]',
      ],
    );
  };

  const closeReplay = () => {
    setReplayOpen(false);
    scheduleFeatureFocusRestore(
      replayReturnFocusRef,
      [
        '[data-ecorex-feature-trigger="task-menu"]',
        '[data-ecorex-feature-trigger="navigation"]',
      ],
    );
  };

  const closeRetouch = () => {
    setRetouchArtifact(null);
    scheduleFeatureFocusRestore(
      retouchReturnFocusRef,
      [
        '[data-ecorex-feature-trigger="task-menu"]',
        '[data-ecorex-feature-trigger="navigation"]',
      ],
    );
  };

  const closeArtifactPreview = () => {
    setPreviewArtifact(null);
  };

  const restoreArtifactPreviewFocus = () => {
    const target = previewReturnFocusRef.current;
    previewReturnFocusRef.current = null;
    window.requestAnimationFrame(() => {
      restoreFeatureFocus(
        target,
        [
          '[data-ecorex-feature-trigger="task-menu"]',
          '[data-ecorex-feature-trigger="navigation"]',
        ],
      );
    });
  };

  const closeArtifactPreviewFallback = () => {
    closeArtifactPreview();
    restoreArtifactPreviewFocus();
  };

  const bootstrap = runtime.state.bootstrap;
  if (runtime.loadState === "loading" && !bootstrap) {
    return (
      <main className="ex-boot" aria-busy="true">
        <span className="ex-brand-mark" aria-hidden="true">E</span>
        <div>
          <strong>正在启动 EcoreX</strong>
          <p>正在准备模型、权限和常用连接…</p>
        </div>
      </main>
    );
  }

  if (runtime.loadState === "error" && !bootstrap) {
    return (
      <main className="ex-boot is-error">
        <AlertCircle aria-hidden="true" />
        <div>
          <strong>EcoreX 未能启动</strong>
          <p>{runtime.transportError}</p>
          <button className="ex-button is-primary" type="button" onClick={runtime.retryBootstrap}>
            <RefreshCw aria-hidden="true" />
            重新连接
          </button>
        </div>
      </main>
    );
  }

  const authenticated = bootstrap?.login.authenticated === true;
  if (bootstrap && !authenticated) {
    return (
      <LoginPage
        busy={runtime.sessionBusy}
        error={runtime.sessionError}
        onClearError={runtime.clearSessionError}
        onLogin={runtime.loginSession}
      />
    );
  }

  const connected = runtime.state.streamState === "open" || !runtime.state.thread;
  const modelServiceReady = authenticated && bootstrap?.model_service.state === "ready";
  const modelAvailable = modelServiceReady && Boolean(bootstrap?.models.chat.length);
  const modelUnavailable = modelServiceReady && !bootstrap?.models.chat.length
    ? "无可用 Agent 模型，请联系管理员。"
    : modelUnavailableMessage(authenticated, bootstrap?.model_service.reason);
  const accessLabel = bootstrap?.permissions.full_access ? "完全访问" : "默认权限";
  const accessDescription = bootstrap?.permissions.full_access
    ? "完全访问：可访问本机和网络，可随时在设置中关闭。"
    : "默认权限：仅修改工作区文件，扩大范围前会询问。";
  const update = bootstrap?.update;
  const shareUnavailableReason = !authenticated
    ? "登录后才能分享任务。"
    : !runtime.state.thread
    ? "发送第一条消息后才能分享任务。"
    : bootstrap?.share_service.state !== "ready"
      ? serviceReasonMessage(
          bootstrap?.share_service.reason,
          "分享服务暂时不可用，请稍后重试。",
        )
      : null;
  const retouchUnavailableReason = serviceReasonMessage(
    bootstrap?.retouch_service.reason,
    "精准修图暂时不可用，请稍后重试。",
  );
  const hasPendingUpdate = hasPendingRuntimeUpdate(update);
  const updateMessage = hasPendingUpdate && update?.state === "awaiting_user"
    ? `EcoreX ${update.target_version ?? "新版"} 已准备好。`
    : hasPendingUpdate && update?.state === "activating"
      ? "正在启用新版，页面会自动刷新…"
      : hasPendingUpdate && update?.state === "failed"
        ? "新版未能启用，当前版本可继续使用。"
        : hasPendingUpdate && (update?.state === "available" || update?.state === "downloading")
          ? `正在后台准备 EcoreX ${update.target_version ?? "新版"}…`
          : null;
  const updateBannerKey = updateMessage && update
    ? `${update.target_version ?? "unknown"}:${update.state}`
    : null;
  const updateBannerVisible = Boolean(
    updateMessage
    && updateBannerKey
    && !dismissedUpdateBanners.includes(updateBannerKey),
  );
  const dismissUpdateBanner = () => {
    if (!updateBannerKey) return;
    setDismissedUpdateBanners((current) => {
      if (current.includes(updateBannerKey)) return current;
      const next = [...current, updateBannerKey];
      try {
        window.sessionStorage.setItem(DISMISSED_UPDATE_BANNERS_KEY, JSON.stringify(next));
      } catch {
        // In-memory dismissal still works when storage is unavailable.
      }
      return next;
    });
  };
  const isNewConversation = !runtime.state.thread;

  const handleArtifactAction = async (artifact: ArtifactProjection, action: string) => {
    if (action === "thumbs_up" || action === "thumbs_down") {
      await runtime.feedbackArtifact(artifact, action);
      return;
    }
    if (action === "download") {
      setArtifactNotice(null);
      const receipt = await runtime.downloadArtifact(artifact);
      if (receipt) {
        setArtifactNotice(
          `已保存到${outputLocationLabel(receipt.location_alias)}：${receipt.display_name}`,
        );
      }
      return;
    }
    if (action === "open" || action === "reveal") {
      setArtifactNotice(null);
      const completed = await runtime.performArtifactExternalAction(artifact, action);
      if (completed) {
        setArtifactNotice(
          action === "open"
            ? `已交给系统打开“${artifact.display_name}”。`
            : `已在文件夹中定位“${artifact.display_name}”。`,
        );
      }
      return;
    }
    if (action === "preview") {
      previewReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      warmFeature(loadArtifactPreviewDialog);
      setPreviewArtifact(artifact);
      return;
    }
    if (action === "precise_retouch") {
      captureFeatureTrigger(retouchReturnFocusRef);
      warmFeature(loadRetouchWorkspace);
      setRetouchArtifact(artifact);
    }
  };

  const taskMenu = (
    <DropdownMenu.Root>
      <Tooltip.Root delayDuration={850}>
        <Tooltip.Trigger asChild>
          <DropdownMenu.Trigger
            type="button"
            className="ex-icon-button ex-title-menu"
            aria-label="打开任务更多菜单"
            data-ecorex-feature-trigger="task-menu"
          >
            <Ellipsis aria-hidden="true" />
          </DropdownMenu.Trigger>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content className="ex-tooltip" side="top" sideOffset={8}>
            更多
            <Tooltip.Arrow className="ex-tooltip-arrow" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="ex-menu" sideOffset={8} align="start">
          <DropdownMenu.Item
            className="ex-menu-item"
            disabled={!currentThreadId}
            onSelect={() => void copyCurrentThreadId()}
          >
            <Copy aria-hidden="true" />
            复制任务 ID
          </DropdownMenu.Item>
          {currentThreadId ? (
            <DropdownMenu.Label className="ex-menu-note">{currentThreadId}</DropdownMenu.Label>
          ) : null}
          <DropdownMenu.Separator className="ex-menu-separator" />
          <DropdownMenu.Item
            className="ex-menu-item"
            disabled={!currentThreadId}
            onFocus={() => warmFeature(loadReplayDialog)}
            onPointerEnter={() => warmFeature(loadReplayDialog)}
            onSelect={() => {
              captureFeatureTrigger(replayReturnFocusRef);
              warmFeature(loadReplayDialog);
              setReplayOpen(true);
            }}
          >
            <History aria-hidden="true" />
            任务检查与重新运行
          </DropdownMenu.Item>
          {!currentThreadId ? (
            <DropdownMenu.Label className="ex-menu-note">发送首条消息后可查看任务记录。</DropdownMenu.Label>
          ) : null}
          <DropdownMenu.Separator className="ex-menu-separator" />
          <DropdownMenu.Item
            className="ex-menu-item"
            onFocus={() => warmFeature(loadSettingsDialog)}
            onPointerEnter={() => warmFeature(loadSettingsDialog)}
            onSelect={() => {
              captureFeatureTrigger(settingsReturnFocusRef);
              warmFeature(loadSettingsDialog);
              setSettingsOpen(true);
            }}
          >
            <Settings2 aria-hidden="true" />
            设置
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );

  const composer = (
    <Composer
      connectors={runtime.connectorCatalog}
      connectorLoadState={runtime.connectorCatalogState}
      connectorError={runtime.connectorError}
      connectorNotice={runtime.connectorNotice}
      connectorOperations={runtime.connectorOperations}
      onRefreshConnectors={runtime.refreshConnectors}
      onConnectConnector={runtime.connectConnector}
      onReconnectConnector={runtime.reconnectConnector}
      onCheckConnector={runtime.refreshConnectorHealth}
      onDisconnectConnector={runtime.disconnectConnector}
      onClearConnectorError={runtime.clearConnectorError}
      onClearConnectorNotice={runtime.clearConnectorNotice}
      active={runtime.activeTurn !== null}
      submitting={runtime.submitting || Boolean(runtime.switchingThreadId)}
      modelAvailable={modelAvailable}
      sendUnavailableReason={modelAvailable ? null : modelUnavailable}
      chatModels={bootstrap?.models.chat || []}
      imageModels={bootstrap?.models.image || []}
      chatModel={runtime.chatModel}
      imageModel={runtime.imageModel}
      quota={bootstrap?.quota || null}
      usage={runtime.conversationUsage}
      permissionLabel={accessLabel}
      permissionDescription={accessDescription}
      onChatModelChange={runtime.setChatModel}
      onImageModelChange={runtime.setImageModel}
      onOpenPermissionSettings={() => {
        captureFeatureTrigger(settingsReturnFocusRef);
        warmFeature(loadSettingsDialog);
        setSettingsOpen(true);
      }}
      onSend={runtime.sendMessage}
      onUploadAttachment={runtime.uploadInputAttachment}
      onInterrupt={() => void runtime.interrupt()}
    />
  );

  return (
    <Tooltip.Provider skipDelayDuration={250}>
      <div className="ex-app-shell">
        <Suspense fallback={<aside className="ex-sidebar is-loading" aria-label="正在加载任务导航" />}>
          <Sidebar
          open={sidebarOpen}
          modal={sidebarOpen && mobileNavigation}
          currentThreadId={currentThreadId}
          version={bootstrap?.update.current_version || window.__ECOREX_RUNTIME__?.version || "未知"}
          threads={runtime.threads}
          projects={runtime.projects}
          projectCatalogState={runtime.projectCatalogState}
          projectCatalogError={runtime.projectCatalogError}
          projectPickerBusy={runtime.projectPickerBusy}
          catalogState={runtime.threadCatalogState}
          catalogError={runtime.threadCatalogError}
          switchingThreadId={runtime.switchingThreadId}
          mutationKey={runtime.threadMutationKey}
          authenticated={Boolean(bootstrap?.login.authenticated)}
          accountDisplayName={bootstrap?.login.display_name ?? null}
          skillsActive={skillsOpen}
          sessionBusy={runtime.sessionBusy}
          sessionError={runtime.sessionError}
          onClose={() => setSidebarOpen(false)}
          onNewTask={(project) => {
            setSkillsOpen(false);
            runtime.newTask(project ?? null);
            setSidebarOpen(false);
          }}
          onPickProject={runtime.pickProject}
          onClearProjectError={runtime.clearProjectCatalogError}
          onOpenThread={async (threadId) => {
            const opened = await runtime.openThread(threadId);
            if (opened) {
              setSkillsOpen(false);
              setSidebarOpen(false);
            }
            return opened;
          }}
          onOpenSkills={() => {
            warmFeature(loadSkillsWorkspace);
            setSkillsOpen(true);
            setSidebarOpen(false);
          }}
          onRenameThread={runtime.renameThread}
          onPinThread={runtime.pinThread}
          onUnpinThread={runtime.unpinThread}
          onArchiveThread={runtime.archiveThread}
          onRestoreThread={runtime.restoreThread}
          onRefreshThreads={runtime.refreshThreads}
          onClearCatalogError={runtime.clearThreadCatalogError}
          onOpenSettings={() => {
            captureFeatureTrigger(settingsReturnFocusRef);
            warmFeature(loadSettingsDialog);
            setSettingsOpen(true);
            setSidebarOpen(false);
          }}
          onClearSessionError={runtime.clearSessionError}
          onLogout={runtime.logoutSession}
          />
        </Suspense>
        {sidebarOpen ? (
          <button
            className="ex-sidebar-scrim"
            type="button"
            aria-label="关闭任务导航"
            aria-hidden="true"
            tabIndex={-1}
            onClick={() => setSidebarOpen(false)}
          />
        ) : null}

        <main className={`ex-workspace${skillsOpen ? " is-skills" : ""}`} inert={sidebarOpen && mobileNavigation ? true : undefined}>
          {skillsOpen ? (
            <Suspense fallback={<section className="ex-skills-loading" role="status">正在打开技能…</section>}>
              <SkillsWorkspace
                snapshot={runtime.extensionSnapshot}
                loadState={runtime.extensionCatalogState}
                error={runtime.extensionError}
                operations={runtime.extensionOperations}
                installBusy={runtime.extensionInstallBusy}
                onClearError={runtime.clearExtensionError}
                onRefresh={runtime.refreshExtensions}
                onAction={runtime.mutateExtension}
                onInstallLocalSkill={runtime.installLocalSkill}
              />
            </Suspense>
          ) : (
          <>
          <header className="ex-workspace-header">
            <div className="ex-header-title">
              <IconButton
                className="ex-mobile-menu"
                label="打开任务导航"
                data-ecorex-feature-trigger="navigation"
                onClick={() => setSidebarOpen(true)}
              >
                <Menu aria-hidden="true" />
              </IconButton>
              <Folder className="ex-workspace-symbol" aria-hidden="true" />
              <div className="ex-header-copy">
                <div className="ex-title-row">
                  <h1>{runtime.state.thread?.title || "新任务"}</h1>
                  {taskMenu}
                </div>
                <span className={`ex-connection is-${connected ? "online" : "retrying"}`}>
                  {connected ? <Wifi aria-hidden="true" /> : <WifiOff aria-hidden="true" />}
                  {connected ? "本机已连接" : "连接中，正在恢复"}
                </span>
              </div>
            </div>

            <div className="ex-header-actions">
              <IconButton
                label={theme === "dark" ? "切换到明亮模式" : "切换到暗色模式"}
                onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}
              >
                {theme === "dark" ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
              </IconButton>
              <IconButton
                label={shareUnavailableReason || "分享当前任务"}
                data-ecorex-feature-trigger="share"
                disabled={Boolean(shareUnavailableReason)}
                onFocus={() => warmFeature(loadShareDialog)}
                onPointerEnter={() => warmFeature(loadShareDialog)}
                onClick={() => {
                  captureFeatureTrigger(shareReturnFocusRef);
                  setShareOpen(true);
                }}
              >
                <Share2 aria-hidden="true" />
              </IconButton>
            </div>
          </header>

          <div className="ex-status-stack">
            {updateBannerVisible ? (
              <section className="ex-update-banner" aria-live="polite">
                <span>{updateMessage}</span>
                {update?.state === "awaiting_user" && update.can_activate ? (
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={runtime.updateBusy}
                    onClick={() => void runtime.activateUpdate()}
                  >
                    {runtime.updateBusy ? "正在更新" : "更新并刷新"}
                  </button>
                ) : null}
                {update?.state === "failed" ? (
                  <button
                    className="ex-button"
                    type="button"
                    disabled={runtime.updateBusy}
                    onClick={() => void runtime.checkUpdate()}
                  >
                    重试检查
                  </button>
                ) : null}
                <IconButton label="关闭更新提示" onClick={dismissUpdateBanner}>
                  <X aria-hidden="true" />
                </IconButton>
              </section>
            ) : null}

            {runtime.updateError ? (
              <section className="ex-error-banner" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{runtime.updateError}</span>
                <IconButton label="关闭更新错误" onClick={runtime.clearUpdateError}>
                  <X aria-hidden="true" />
                </IconButton>
              </section>
            ) : null}

            {bootstrap && authenticated && !modelServiceReady ? (
              <section className="ex-error-banner" role="status">
                <WifiOff aria-hidden="true" />
                <span>{modelUnavailable}</span>
              </section>
            ) : null}

            {runtime.transportError ? (
              <section className="ex-error-banner" role="alert">
                <AlertCircle aria-hidden="true" />
                <span>{runtime.transportError}</span>
                <IconButton label="关闭错误提示" onClick={runtime.clearTransportError}>
                  <X aria-hidden="true" />
                </IconButton>
              </section>
            ) : null}

            {artifactNotice ? (
              <section className="ex-update-banner" aria-live="polite">
                <span>{artifactNotice}</span>
                <IconButton label="关闭产物提示" onClick={() => setArtifactNotice(null)}>
                  <X aria-hidden="true" />
                </IconButton>
              </section>
            ) : null}
          </div>

          <section className="ex-timeline" aria-label="对话">
            <Timeline
              items={runtime.items}
              turns={runtime.turns}
              chatModels={bootstrap?.models.chat || []}
              activeTurn={runtime.activeTurn}
              isThinking={runtime.isThinking}
              visibleReasoning={runtime.visibleReasoning}
              artifacts={runtime.artifacts}
              artifactPreviewUrls={runtime.artifactPreviewUrls}
              onArtifactAction={(artifact, action) => void handleArtifactAction(artifact, action)}
              onArtifactPreviewVisible={runtime.prefetchArtifactPreview}
              retouchAvailable={authenticated && bootstrap?.retouch_service.state === "ready"}
              retouchUnavailableReason={retouchUnavailableReason}
              projects={runtime.projects}
              newConversationProject={runtime.newConversationProject}
              projectPickerBusy={runtime.projectPickerBusy}
              onSelectConversationProject={runtime.newTask}
              onPickProject={runtime.pickProject}
              newConversationComposer={isNewConversation ? composer : null}
            />
          </section>

          {!isNewConversation ? (
            <div className="ex-workspace-bottom">
              {runtime.pendingInteractions.length ? (
                <Suspense fallback={(
                  <section
                    className="ex-interaction-stack is-loading"
                    role="status"
                    aria-live="polite"
                    aria-busy="true"
                  >
                    正在准备需要你确认的内容…
                  </section>
                )}>
                  <InteractionStack
                    interactions={runtime.pendingInteractions}
                    connectorRuntime={runtime}
                    onRespond={runtime.respondInteraction}
                  />
                </Suspense>
              ) : null}
              {composer}
            </div>
          ) : null}
          </>
          )}
        </main>

        <LazyFeatureBoundary
          active={settingsOpen}
          label="设置"
          onClose={closeSettings}
        >
          <SettingsDialog
            open={settingsOpen}
            bootstrap={bootstrap}
            onOpenChange={(open) => {
              if (open) setSettingsOpen(true);
              else closeSettings();
            }}
            permissionUpdating={runtime.permissionUpdating}
            permissionError={runtime.permissionError}
            onClearPermissionError={runtime.clearPermissionError}
            onPermissionChange={runtime.updatePermission}
            extensions={runtime.extensionSnapshot}
            extensionLoadState={runtime.extensionCatalogState}
            onManageExtensions={() => {
              warmFeature(loadSkillsWorkspace);
              setSettingsOpen(false);
              setSkillsOpen(true);
            }}
            memory={runtime.memory}
            memoryLoadState={runtime.memoryLoadState}
            memoryBusy={runtime.memoryBusy}
            memoryError={runtime.memoryError}
            onClearMemoryError={runtime.clearMemoryError}
            onRefreshMemory={runtime.refreshMemory}
            onResetMemory={runtime.resetLearnedMemory}
            onUndoMemoryReset={runtime.undoLearnedMemoryReset}
            client={runtime.client}
            outputLocations={runtime.outputLocations}
            outputPreference={runtime.outputPreference}
            outputLoadState={runtime.outputLoadState}
            outputBusy={runtime.outputBusy}
            outputError={runtime.outputError}
            onClearOutputError={runtime.clearOutputError}
            onRefreshOutput={runtime.refreshOutput}
            onOutputLocationChange={runtime.updateOutputLocation}
            onPickOutputLocation={runtime.pickOutputLocation}
            systemHealth={runtime.systemHealth}
            systemHealthLoadState={runtime.systemHealthLoadState}
            systemHealthError={runtime.systemHealthError}
            onClearSystemHealthError={runtime.clearSystemHealthError}
            onRefreshSystemHealth={runtime.refreshSystemHealth}
            onLoadSystemTechnicalHealth={runtime.loadSystemTechnicalHealth}
            updateBusy={runtime.updateBusy}
            updateError={runtime.updateError}
            onClearUpdateError={runtime.clearUpdateError}
            onCheckUpdate={runtime.checkUpdate}
            onActivateUpdate={runtime.activateUpdate}
          />
        </LazyFeatureBoundary>
        <LazyFeatureBoundary active={shareOpen} label="任务分享" onClose={closeShare}>
          <ShareDialog
            open={shareOpen}
            thread={runtime.state.thread}
            serviceState={bootstrap?.share_service.state ?? "unavailable"}
            unavailableReason={bootstrap?.share_service.reason ?? null}
            onOpenChange={(open) => {
              if (open) setShareOpen(true);
              else closeShare();
            }}
            onList={runtime.listShares}
            onCreate={runtime.createShare}
            onGet={runtime.getShare}
            onRevoke={runtime.revokeShare}
          />
        </LazyFeatureBoundary>
        <LazyFeatureBoundary
          active={replayOpen}
          label="任务检查与重新运行"
          onClose={closeReplay}
        >
          <ReplayDialog
            open={replayOpen}
            thread={runtime.state.thread}
            onOpenChange={(open) => {
              if (open) setReplayOpen(true);
              else closeReplay();
            }}
            onMockReplay={runtime.mockReplay}
            onLiveReplay={runtime.liveReplay}
          />
        </LazyFeatureBoundary>
        <LazyFeatureBoundary
          active={previewArtifact !== null}
          label="产物预览"
          onClose={closeArtifactPreviewFallback}
        >
          <ArtifactPreviewDialog
            artifact={previewArtifact}
            onClose={closeArtifactPreview}
            onRestoreFocus={restoreArtifactPreviewFocus}
            onDownload={(artifact) => void handleArtifactAction(artifact, "download")}
            onLoadPreview={runtime.loadArtifactPreview}
          />
        </LazyFeatureBoundary>
        <LazyFeatureBoundary
          active={retouchArtifact !== null}
          label="精准修图"
          onClose={closeRetouch}
        >
          <RetouchWorkspace
            artifact={retouchArtifact}
            artifacts={runtime.artifacts}
            artifactPreviewUrls={runtime.artifactPreviewUrls}
            onClose={closeRetouch}
            onOpenWorkspace={runtime.openRetouchWorkspace}
            onGetWorkspace={runtime.getRetouchWorkspace}
            onSaveWorkspace={runtime.saveRetouchWorkspace}
            onSubmitWorkspace={async (workspace) => {
              const submitted = await runtime.submitRetouchWorkspace(workspace);
              setArtifactNotice("精准修图已排队。完成后会显示新图片，并保留草稿用于对比。");
              return submitted;
            }}
            onReopenWorkspace={runtime.reopenRetouchWorkspace}
            onLoadBlob={runtime.loadRetouchWorkspaceBlob}
            onOpenResult={(result) => {
              previewReturnFocusRef.current = document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
              warmFeature(loadArtifactPreviewDialog);
              setPreviewArtifact(result);
            }}
            onContinueResult={(result) => setRetouchArtifact(result)}
          />
        </LazyFeatureBoundary>
      </div>
    </Tooltip.Provider>
  );
}
