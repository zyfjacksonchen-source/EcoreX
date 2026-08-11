import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import {
  AlertCircle,
  ArchiveRestore,
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

import { IconButton } from "./components/IconButton.tsx";
import { LazyFeatureBoundary } from "./components/LazyFeatureBoundary.tsx";
import { LoginPage } from "./components/LoginPage.tsx";
import { useRuntimeSession } from "./state/useRuntimeSession.ts";
import {
  resolveThemePreference,
  THEME_PREFERENCE_KEY,
  type ThemePreference,
} from "./state/themePreference.ts";
import {
  hasPendingRuntimeUpdate,
  isRuntimeUpdateInstalling,
  isVerifiedRuntimeUpdateReady,
  runtimeUpdateStatusText,
} from "./state/updatePresentation.ts";
import { serviceReasonMessage } from "./state/userLanguage.ts";
import "./styles/primitives.css";
import "./styles/layout.css";
import "./styles/features.css";
import "./styles/plain-language.css";

const loadArtifactPreviewDialog = () => import("./components/ArtifactPreviewDialog.tsx");
const loadComposer = () => import("./components/Composer.tsx");
const loadHomeDashboard = () => import("./components/HomeDashboard.tsx");
const loadSidebar = () => import("./components/Sidebar.tsx");
const loadSkillsWorkspace = () => import("./components/SkillsWorkspace.tsx");
const loadInteractionStack = () => import("./components/InteractionStack.tsx");
const loadReplayDialog = () => import("./components/ReplayDialog.tsx");
const loadRetouchWorkspace = () => import("./components/RetouchWorkspace.tsx");
const loadSettingsDialog = () => import("./components/SettingsDialog.tsx");
const loadShareDialog = () => import("./components/ShareDialog.tsx");
const loadTimeline = () => import("./components/Timeline.tsx");
const loadTaskListBlock = () => import("./components/TaskListBlock.tsx");

const ArtifactPreviewDialog = lazy(async () => ({
  default: (await loadArtifactPreviewDialog()).ArtifactPreviewDialog,
}));
const Composer = lazy(async () => ({
  default: (await loadComposer()).Composer,
}));
const HomeDashboard = lazy(async () => ({
  default: (await loadHomeDashboard()).HomeDashboard,
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
const Timeline = lazy(async () => ({
  default: (await loadTimeline()).Timeline,
}));
const TaskListBlock = lazy(loadTaskListBlock);

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
const PROFILE_AVATAR_KEY = "emate-profile-avatar";
const DESKTOP_THREAD_ID = /^thr_[A-Za-z0-9._:-]{1,252}$/u;

type DesktopUpdateStatus =
  | { state: "checking" | "not-available"; userInitiated: boolean }
  | { state: "available"; version: string; platform: "windows" | "macos"; manualInstall: boolean; userInitiated: boolean }
  | { state: "downloading"; version: string | null; percent: number }
  | { state: "downloaded"; version: string }
  | { state: "error"; version: string | null; message: string; userInitiated: boolean };

declare global {
  interface Window {
    eMateDesktop?: {
      checkForUpdates?: () => Promise<void>;
      openUpdatePage?: () => Promise<void>;
      downloadDesktopUpdate?: () => Promise<void>;
      installDesktopUpdate?: () => Promise<void>;
      desktopUpdateStatus?: () => Promise<DesktopUpdateStatus | null>;
      onDesktopUpdateStatus?: (callback: (status: DesktopUpdateStatus) => void) => () => void;
      onOpenThread?: (callback: (threadId: string) => void) => () => void;
    };
  }
}

function initialDismissedUpdateBanners(): string[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(DISMISSED_UPDATE_BANNERS_KEY) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return [];
  }
}

function initialProfileAvatar(): string | null {
  try {
    const value = window.localStorage.getItem(PROFILE_AVATAR_KEY);
    return value && /^data:image\/(?:png|jpeg|webp);base64,/u.test(value) && value.length <= 700_000
      ? value
      : null;
  } catch {
    return null;
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
  const [schedulesOpen, setSchedulesOpen] = useState(false);
  const [openChannelsKey, setOpenChannelsKey] = useState(0);
  const [profileAvatar, setProfileAvatar] = useState(initialProfileAvatar);
  const [shareOpen, setShareOpen] = useState(false);
  const shareReturnFocusRef = useRef<HTMLElement | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const replayReturnFocusRef = useRef<HTMLElement | null>(null);
  const [previewArtifact, setPreviewArtifact] = useState<ArtifactProjection | null>(null);
  const previewReturnFocusRef = useRef<HTMLElement | null>(null);
  const [retouchArtifact, setRetouchArtifact] = useState<ArtifactProjection | null>(null);
  const retouchReturnFocusRef = useRef<HTMLElement | null>(null);
  const [artifactNotice, setArtifactNotice] = useState<string | null>(null);
  const [desktopUpdate, setDesktopUpdate] = useState<DesktopUpdateStatus | null>(null);
  const [desktopUpdateBusy, setDesktopUpdateBusy] = useState(false);
  const [dismissedDesktopUpdateVersion, setDismissedDesktopUpdateVersion] = useState<string | null>(null);
  const [dismissedUpdateBanners, setDismissedUpdateBanners] = useState(
    initialDismissedUpdateBanners,
  );
  const [composerPrefill, setComposerPrefill] = useState<{ key: string; text: string } | null>(null);
  const [composerDraft, setComposerDraft] = useState("");

  useEffect(() => window.eMateDesktop?.onOpenThread?.((threadId) => {
    if (!DESKTOP_THREAD_ID.test(threadId)) return;
    void runtime.openThread(threadId).then((opened) => {
      if (!opened) return;
      setSkillsOpen(false);
      setSchedulesOpen(false);
      setSettingsOpen(false);
      setSidebarOpen(false);
    });
  }), [runtime.openThread]);

  useEffect(() => {
    const desktop = window.eMateDesktop;
    if (!desktop) return undefined;
    let active = true;
    const accept = (status: DesktopUpdateStatus) => {
      if (!active) return;
      if (status.state === "available" && status.userInitiated) {
        setDismissedDesktopUpdateVersion(null);
      }
      setDesktopUpdateBusy(false);
      setDesktopUpdate(status);
    };
    const unsubscribe = desktop.onDesktopUpdateStatus?.(accept);
    void desktop.desktopUpdateStatus?.().then((status) => {
      if (status) accept(status);
    }).catch(() => undefined);
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, []);

  const updateProfileAvatar = (value: string | null) => {
    setProfileAvatar(value);
    try {
      if (value) window.localStorage.setItem(PROFILE_AVATAR_KEY, value);
      else window.localStorage.removeItem(PROFILE_AVATAR_KEY);
    } catch {
      // The selected avatar remains active for this session if storage is unavailable.
    }
  };

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
    const artifactId = target?.dataset.artifactPreviewTrigger ?? null;
    window.requestAnimationFrame(() => {
      restoreFeatureFocus(
        target,
        artifactId ? [
          `[data-artifact-preview-trigger="${CSS.escape(artifactId)}"]`,
          '[data-ecorex-feature-trigger="task-menu"]',
          '[data-ecorex-feature-trigger="navigation"]',
        ] : [
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
        <span className="ex-boot-mark" aria-hidden="true" />
        <div>
          <strong>正在启动 e-Mate</strong>
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
          <strong>e-Mate 未能启动</strong>
          <p>{runtime.transportError}</p>
          <button className="ex-button is-primary" type="button" onClick={runtime.retryBootstrap}>
            <RefreshCw aria-hidden="true" />
            重新连接
          </button>
        </div>
      </main>
    );
  }

  const authenticated = !!bootstrap?.login.authenticated;
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
  const updatePending = hasPendingRuntimeUpdate(update) && update?.state !== "failed";
  const updateReady = isVerifiedRuntimeUpdateReady(update);
  const updateInstalling = isRuntimeUpdateInstalling(update, runtime.updateBusy);
  const updateActionable = update?.state === "available" || updateReady;
  const updateMessage = updatePending
    ? runtimeUpdateStatusText(update, runtime.updateBusy)
    : null;
  const updateBannerKey = updateMessage && update
    ? (update.release_id && update.build_digest
        ? `${update.release_id}:${update.build_digest}`
        : update.release_id ?? update.build_digest)
      ?? update.target_version
      ?? update.transaction_id
      ?? "unknown-update"
    : null;
  const updateBannerVisible = Boolean(
    updateMessage
    && updateBannerKey
    && !dismissedUpdateBanners.includes(updateBannerKey),
  );
  const desktopUpdateVersion = desktopUpdate && "version" in desktopUpdate
    ? desktopUpdate.version
    : null;
  const desktopUpdateMessage = (() => {
    switch (desktopUpdate?.state) {
      case "available":
        return desktopUpdate.manualInstall
          ? `e-Mate ${desktopUpdate.version} 已发布。当前 macOS 版本未签名，请打开官方下载页，并按安装图解或信任命令手动更新。`
          : `e-Mate ${desktopUpdate.version} 已发布，已签名 Windows 安装包可供下载。`;
      case "downloading":
        return `正在下载并验证 e-Mate ${desktopUpdate.version ?? "新版"}（${desktopUpdate.percent}%）`;
      case "downloaded":
        return `e-Mate ${desktopUpdate.version} 已下载并验证，重启后完成更新。`;
      case "error":
        return "桌面更新失败，当前版本不受影响，请稍后重试。";
      default:
        return null;
    }
  })();
  const desktopUpdateVisible = Boolean(
    desktopUpdateMessage
    && (desktopUpdate?.state !== "error" || desktopUpdate.userInitiated)
    && dismissedDesktopUpdateVersion !== (desktopUpdateVersion ?? "update-error"),
  );
  const downloadDesktopUpdate = async () => {
    setDesktopUpdateBusy(true);
    try {
      await window.eMateDesktop?.downloadDesktopUpdate?.();
    } finally {
      setDesktopUpdateBusy(false);
    }
  };
  const dismissUpdateBanner = () => {
    if (!updateBannerKey) return;
    setDismissedUpdateBanners((current) => {
      if (current.includes(updateBannerKey)) return current;
      const next = [...current, updateBannerKey].slice(-32);
      try {
        window.localStorage.setItem(DISMISSED_UPDATE_BANNERS_KEY, JSON.stringify(next));
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
              window.requestAnimationFrame(() => setReplayOpen(true));
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
              window.requestAnimationFrame(() => setSettingsOpen(true));
            }}
          >
            <Settings2 aria-hidden="true" />
            设置
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );

  const taskListTurnId = runtime.activeTurn?.turn_id ?? runtime.turns.at(-1)?.turn_id ?? null;
  const taskListItem = [...runtime.items].reverse().find((item) => (
    item.kind === "task_list" && item.turn_id === taskListTurnId
  ));
  const taskListTurn = taskListItem
    ? runtime.turns.find((turn) => turn.turn_id === taskListItem.turn_id)
    : null;
  const composer = (
    <Suspense fallback={<section className="ex-home-loading" role="status">正在准备输入框…</section>}>
      <Composer
        taskList={taskListItem ? (
          <Suspense fallback={<div className="ex-runtime-task-list-loading" role="status">正在载入任务清单…</div>}>
            <TaskListBlock
              item={taskListItem}
              interrupted={Boolean(taskListTurn?.status.match(/failed|cancelled|interrupted|superseded/u))}
            />
          </Suspense>
        ) : null}
        prefillRequest={composerPrefill}
        onPrefillConsumed={() => setComposerPrefill(null)}
        draft={composerDraft}
        onDraftChange={setComposerDraft}
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
        capabilityMentions={runtime.capabilityMentions}
        capabilityMentionState={runtime.capabilityMentionState}
        onChatModelChange={runtime.setChatModel}
        onImageModelChange={runtime.setImageModel}
        onOpenConnections={() => {
          warmFeature(loadSkillsWorkspace);
          setSettingsOpen(false);
          setSchedulesOpen(false);
          setOpenChannelsKey((value) => value + 1);
          setSkillsOpen(true);
        }}
        onSend={runtime.sendMessage}
        onRefreshCapabilityMentions={() => runtime.refreshCapabilityMentions()}
        onUploadAttachment={runtime.uploadInputAttachment}
        onLoadAttachment={runtime.loadInputAttachment}
        onLoadAttachmentThumbnail={runtime.loadInputAttachmentThumbnail}
        onInterrupt={() => void runtime.interrupt()}
      />
    </Suspense>
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
          profileAvatar={profileAvatar}
          skillsActive={skillsOpen}
          schedulesActive={schedulesOpen}
          homeActive={isNewConversation && !skillsOpen && !schedulesOpen}
          sessionBusy={runtime.sessionBusy}
          sessionError={runtime.sessionError}
          onClose={() => setSidebarOpen(false)}
          onNewTask={(project) => {
            setSkillsOpen(false);
            setSchedulesOpen(false);
            setSettingsOpen(false);
            runtime.newTask(project ?? null);
            setSidebarOpen(false);
          }}
          onPickProject={runtime.pickProject}
          onClearProjectError={runtime.clearProjectCatalogError}
          onOpenThread={async (threadId) => {
            const opened = await runtime.openThread(threadId);
            if (opened) {
              setSkillsOpen(false);
              setSchedulesOpen(false);
              setSettingsOpen(false);
              setSidebarOpen(false);
            }
            return opened;
          }}
          onOpenSkills={() => {
            warmFeature(loadSkillsWorkspace);
            setSkillsOpen(true);
            setSchedulesOpen(false);
            setSettingsOpen(false);
            setSidebarOpen(false);
          }}
          onOpenSchedules={() => {
            setSkillsOpen(false);
            setSettingsOpen(false);
            setSchedulesOpen(true);
            setSidebarOpen(false);
          }}
          onRenameThread={runtime.renameThread}
          onPinThread={runtime.pinThread}
          onUnpinThread={runtime.unpinThread}
          onArchiveThread={runtime.archiveThread}
          onRestoreThread={runtime.restoreThread}
          onDeleteThread={runtime.deleteThread}
          onRefreshThreads={runtime.refreshThreads}
          onClearCatalogError={runtime.clearThreadCatalogError}
          onOpenSettings={() => {
            captureFeatureTrigger(settingsReturnFocusRef);
            warmFeature(loadSettingsDialog);
            setSkillsOpen(false);
            setSchedulesOpen(false);
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

        <main className={`ex-workspace${skillsOpen ? " is-skills" : ""}${isNewConversation || schedulesOpen ? " is-home" : ""}`} inert={sidebarOpen && mobileNavigation ? true : undefined}>
          {skillsOpen ? (
            <Suspense fallback={<section className="ex-skills-loading" role="status">正在打开技能…</section>}>
              <SkillsWorkspace
                openChannelsKey={openChannelsKey}
                mcpClient={runtime.client}
                connectorRuntime={{
                  catalog: runtime.connectorCatalog,
                  channelCatalog: runtime.channelConnectorCatalog,
                  loadState: runtime.connectorCatalogState,
                  error: runtime.connectorError,
                  notice: runtime.connectorNotice,
                  operations: runtime.connectorOperations,
                  onRefresh: runtime.refreshConnectors,
                  onConnect: runtime.connectConnector,
                  onReconnect: runtime.reconnectConnector,
                  onHealthCheck: runtime.refreshConnectorHealth,
                  onDisconnect: runtime.disconnectConnector,
                  onSaveConfiguration: runtime.saveChannelConnector,
                  onChannelAction: runtime.mutateChannelConnector,
                  onChannelDisconnect: runtime.disconnectChannelConnector,
                  deviceAuthorizations: runtime.channelDeviceAuthorizations,
                  onBeginDeviceAuthorization: runtime.beginChannelDeviceAuthorization,
                  onDeviceAuthorizationAction: runtime.mutateChannelDeviceAuthorization,
                  onClearError: runtime.clearConnectorError,
                  onClearNotice: runtime.clearConnectorNotice,
                }}
                snapshot={runtime.extensionSnapshot}
                loadState={runtime.extensionCatalogState}
                error={runtime.extensionError}
                operations={runtime.extensionOperations}
                installBusy={runtime.extensionInstallBusy}
                onClearError={runtime.clearExtensionError}
                onRefresh={runtime.refreshExtensions}
                onAction={runtime.mutateExtension}
                onConfigure={runtime.configureSkill}
                onInstallLocalSkill={runtime.installLocalSkill}
                mcpOAuthStatuses={runtime.mcpOAuthStatuses}
                mcpOAuthBusy={runtime.mcpOAuthBusy}
                onRefreshMcpOAuth={runtime.refreshMcpOAuth}
                onBeginMcpOAuth={runtime.beginMcpOAuth}
                onClearMcpOAuth={runtime.clearMcpOAuth}
                hubItems={runtime.skillHubItems}
                hubState={runtime.skillHubState}
                hubError={runtime.skillHubError}
                hubInstallingSlug={runtime.skillHubInstallingSlug}
                hubDownloadingSlug={runtime.skillHubDownloadingSlug}
                hubDetail={runtime.skillHubDetail}
                hubDetailLoadingSlug={runtime.skillHubDetailLoadingSlug}
                hubUploadBusy={runtime.skillHubUploadBusy}
                onRefreshHub={runtime.refreshSkillHub}
                onInstallHub={runtime.installHubSkill}
                onDownloadHub={runtime.downloadHubSkill}
                onLoadHubDetail={runtime.loadHubSkillDetail}
                onClearHubDetail={runtime.clearHubSkillDetail}
                onPublishHub={runtime.publishHubSkill}
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
              {isNewConversation || schedulesOpen ? (
                <span
                  className={`ex-home-runtime-dot is-${connected ? "online" : "retrying"}`}
                  aria-label={connected ? "运行时已连接" : "运行时正在重连"}
                  role="status"
                />
              ) : null}
              <IconButton
                label={theme === "dark" ? "切换到明亮模式" : "切换到暗色模式"}
                onClick={() => setTheme((current) => current === "dark" ? "light" : "dark")}
              >
                {theme === "dark" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}
              </IconButton>
              {isNewConversation || schedulesOpen ? (
                <IconButton
                  label="打开设置"
                  onClick={() => {
                    captureFeatureTrigger(settingsReturnFocusRef);
                    warmFeature(loadSettingsDialog);
                    setSettingsOpen(true);
                  }}
                >
                  <Settings2 aria-hidden="true" />
                </IconButton>
              ) : (
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
              )}
            </div>
          </header>

          <div className="ex-status-stack">
            {window.__ECOREX_RUNTIME__?.mode === "acceptance-preview" ? (
              <section className="ex-update-banner" role="status" data-runtime-mode="acceptance-preview">
                <span>
                  这是新版候选验收窗口。消息、生图和本地工具写入隔离副本；登录、更新、外部分享不会影响当前正式版本。
                </span>
              </section>
            ) : null}
            {desktopUpdateVisible && desktopUpdateMessage ? (
              <section className="ex-update-banner" aria-live="polite" data-desktop-update-state={desktopUpdate?.state}>
                <div className="ex-update-copy">
                  <span>{desktopUpdateMessage}</span>
                  {desktopUpdate?.state === "downloading" ? (
                    <progress aria-label="桌面更新下载进度" max={100} value={desktopUpdate.percent} />
                  ) : null}
                </div>
                {desktopUpdate?.state === "available" ? (
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={desktopUpdateBusy}
                    onClick={() => void (desktopUpdate.manualInstall
                      ? window.eMateDesktop?.openUpdatePage?.()
                      : downloadDesktopUpdate())}
                  >
                    {desktopUpdate.manualInstall
                      ? "打开官方下载页"
                      : desktopUpdateBusy ? "正在准备下载" : "下载更新"}
                  </button>
                ) : null}
                {desktopUpdate?.state === "downloaded" ? (
                  <button className="ex-button is-primary" type="button" onClick={() => void window.eMateDesktop?.installDesktopUpdate?.()}>
                    重启并更新
                  </button>
                ) : null}
                {desktopUpdate?.state === "error" ? (
                  <button className="ex-button" type="button" onClick={() => void window.eMateDesktop?.checkForUpdates?.()}>
                    重新检查
                  </button>
                ) : null}
                <IconButton
                  label="关闭桌面更新提示"
                  onClick={() => setDismissedDesktopUpdateVersion(desktopUpdateVersion ?? "update-error")}
                >
                  <X aria-hidden="true" />
                </IconButton>
              </section>
            ) : null}
            {updateBannerVisible ? (
              <section className="ex-update-banner" aria-live="polite">
                <div className="ex-update-copy">
                  <span>{updateMessage}</span>
                  {updateInstalling ? (
                    <progress aria-label="新版下载与安装进度" />
                  ) : null}
                </div>
                {updateActionable ? (
                  <button
                    className="ex-button is-primary"
                    type="button"
                    disabled={runtime.updateBusy}
                    aria-busy={runtime.updateBusy}
                    onClick={() => void runtime.activateUpdate()}
                  >
                    {runtime.updateBusy
                      ? "正在下载并安装"
                      : update?.state === "available"
                        ? "下载并安装"
                        : "立即安装"}
                  </button>
                ) : null}
                {!updateInstalling ? (
                  <IconButton label="关闭更新提示" onClick={dismissUpdateBanner}>
                    <X aria-hidden="true" />
                  </IconButton>
                ) : null}
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

          <section className="ex-timeline" aria-label={isNewConversation ? "e-Mate 首页" : "对话"}>
            {isNewConversation || schedulesOpen ? (
              <Suspense fallback={<section className="ex-home-loading" role="status">正在打开 e-Mate 首页…</section>}>
                <HomeDashboard
                  mode={schedulesOpen ? "schedules" : "home"}
                  composer={composer}
                  threads={runtime.threads}
                  projects={runtime.projects}
                  selectedProject={runtime.newConversationProject}
                  projectPickerBusy={runtime.projectPickerBusy}
                  usage={runtime.accountUsage}
                  onSelectProject={runtime.newTask}
                  onPickProject={runtime.pickProject}
                  onOpenThread={(threadId) => {
                    void runtime.openThread(threadId).then((opened) => {
                      if (!opened) return;
                      setSkillsOpen(false);
                      setSchedulesOpen(false);
                      setSidebarOpen(false);
                    });
                  }}
                  onTemplate={(text) => {
                    setComposerPrefill({ key: crypto.randomUUID(), text });
                    setSchedulesOpen(false);
                  }}
                />
              </Suspense>
            ) : (
              <Suspense fallback={<section className="ex-home-loading" role="status">正在加载对话…</section>}>
                <Timeline
                  items={runtime.items}
                  turns={runtime.turns}
                  interactions={runtime.interactions}
                  chatModels={bootstrap?.models.chat || []}
                  serverClockOffsetMs={runtime.serverClockOffsetMs}
                  activeTurn={runtime.activeTurn}
                  isThinking={runtime.isThinking}
                  artifacts={runtime.artifacts}
                  imageBatchFailures={runtime.imageBatchFailures}
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
                  newConversationComposer={null}
                  onLoadAttachment={runtime.loadInputAttachment}
                  onLoadAttachmentThumbnail={runtime.loadInputAttachmentThumbnail}
                />
              </Suspense>
            )}
          </section>

          {!isNewConversation && !schedulesOpen ? (
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
              {runtime.state.thread?.status === "archived" ? (
                <section className="ex-archived-readonly" role="status">
                  <span>此任务已归档，仅可查看。</span>
                  <button
                    className="ex-button"
                    type="button"
                    disabled={runtime.threadMutationKey === `restore:${currentThreadId}`}
                    onClick={() => currentThreadId && void runtime.restoreThread(currentThreadId)}
                  >
                    <ArchiveRestore aria-hidden="true" />恢复后继续
                  </button>
                </section>
              ) : composer}
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
            onManageExtensions={() => {
              warmFeature(loadSkillsWorkspace);
              setSettingsOpen(false);
              setSchedulesOpen(false);
              setSkillsOpen(true);
            }}
            profileAvatar={profileAvatar}
            onProfileAvatarChange={updateProfileAvatar}
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
