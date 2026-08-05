import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Archive,
  ArchiveRestore,
  Blocks,
  ChevronDown,
  Copy,
  FolderOpen,
  LoaderCircle,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { ProjectProjection, ThreadProjection } from "../api/contracts.ts";
import type { LoadState } from "../state/useRuntimeSession.ts";
import { IconButton } from "./IconButton.tsx";

interface SidebarProps {
  open: boolean;
  modal: boolean;
  currentThreadId: string | null;
  version: string;
  threads: ThreadProjection[];
  projects: ProjectProjection[];
  projectCatalogState: LoadState;
  projectCatalogError: string | null;
  projectPickerBusy: boolean;
  catalogState: LoadState;
  catalogError: string | null;
  switchingThreadId: string | null;
  mutationKey: string | null;
  authenticated: boolean;
  accountDisplayName: string | null;
  skillsActive: boolean;
  creativeActive: boolean;
  homeActive: boolean;
  sessionBusy: boolean;
  sessionError: string | null;
  onClose: () => void;
  onNewTask: (project?: ProjectProjection | null) => void;
  onOpenSkills: () => void;
  onOpenCreative: () => void;
  onPickProject: () => Promise<ProjectProjection | null>;
  onClearProjectError: () => void;
  onOpenThread: (threadId: string) => Promise<boolean>;
  onRenameThread: (threadId: string, title: string) => Promise<boolean>;
  onPinThread: (threadId: string) => Promise<boolean>;
  onUnpinThread: (threadId: string) => Promise<boolean>;
  onArchiveThread: (threadId: string) => Promise<boolean>;
  onRestoreThread: (threadId: string) => Promise<boolean>;
  onDeleteThread: (threadId: string) => Promise<boolean>;
  onRefreshThreads: () => void;
  onClearCatalogError: () => void;
  onOpenSettings: () => void;
  onClearSessionError: () => void;
  onLogout: () => Promise<{ restart_scheduled: boolean } | null>;
}

const COLLAPSED_THREAD_LIMIT = 8;

export function Sidebar({
  open,
  modal,
  currentThreadId,
  version,
  threads = [],
  projects = [],
  projectCatalogState,
  projectCatalogError,
  projectPickerBusy,
  catalogState,
  catalogError,
  switchingThreadId,
  mutationKey,
  authenticated,
  accountDisplayName,
  skillsActive,
  creativeActive,
  homeActive,
  sessionBusy,
  sessionError,
  onClose,
  onNewTask,
  onOpenSkills,
  onOpenCreative,
  onPickProject,
  onClearProjectError,
  onOpenThread,
  onRenameThread,
  onPinThread,
  onUnpinThread,
  onArchiveThread,
  onRestoreThread,
  onDeleteThread,
  onRefreshThreads,
  onClearCatalogError,
  onOpenSettings,
  onClearSessionError,
  onLogout,
}: SidebarProps) {
  const [renameTarget, setRenameTarget] = useState<ThreadProjection | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [continueOpen, setContinueOpen] = useState(false);
  const [continueValue, setContinueValue] = useState("");
  const [continueError, setContinueError] = useState<string | null>(null);
  const [continuing, setContinuing] = useState(false);
  const [threadIdNotice, setThreadIdNotice] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [allGeneralSessionsVisible, setAllGeneralSessionsVisible] = useState(false);
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ThreadProjection | null>(null);
  const [logoutComplete, setLogoutComplete] = useState<string | null>(null);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [expandedProjectSessions, setExpandedProjectSessions] = useState<Record<string, boolean>>({});
  const renameInputRef = useRef<HTMLInputElement>(null);
  const continueInputRef = useRef<HTMLInputElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const renaming = renameTarget ? mutationKey === `rename:${renameTarget.thread_id}` : false;
  const activeThreads = threads
    .filter((thread) => thread.status === "active")
    .sort((left, right) => Number(right.pinned) - Number(left.pinned));
  const archivedThreads = threads.filter((thread) => thread.status === "archived");
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase("zh-CN");
  const searchResults = activeThreads.filter((thread) => (
    !normalizedSearch
    || (thread.title || "未命名会话").toLocaleLowerCase("zh-CN").includes(normalizedSearch)
  ));
  const projectIdOf = (thread: ThreadProjection) => (
    typeof thread.metadata.project_id === "string" ? thread.metadata.project_id : null
  );
  const generalThreads = activeThreads.filter((thread) => !projectIdOf(thread));
  const collapsedThreadSet = (items: ThreadProjection[], expanded: boolean) => {
    if (expanded || items.length <= COLLAPSED_THREAD_LIMIT) return items;
    const required = new Set(items.filter((thread) => (
      thread.pinned
      || thread.thread_id === currentThreadId
      || thread.active_turn_status !== null
    )).map((thread) => thread.thread_id));
    for (const thread of items) {
      if (required.size >= COLLAPSED_THREAD_LIMIT) break;
      required.add(thread.thread_id);
    }
    return items.filter((thread) => required.has(thread.thread_id));
  };
  const visibleGeneralThreads = collapsedThreadSet(generalThreads, allGeneralSessionsVisible);

  useEffect(() => {
    if (!renameTarget) return;
    setRenameValue(renameTarget.title || "");
    setRenameError(null);
  }, [renameTarget]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open || !modal) return;
    const sidebar = sidebarRef.current;
    if (!sidebar) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusable = () => [...sidebar.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
    )].filter((element) => element.offsetParent !== null && element.getAttribute("aria-hidden") !== "true");
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const targets = focusable();
      const first = targets[0];
      const last = targets.at(-1);
      if (!first || !last) {
        event.preventDefault();
        return;
      }
      if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    sidebar.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      sidebar.removeEventListener("keydown", handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [modal, open]);

  const saveRename = async () => {
    if (!renameTarget || renaming) return;
    const title = renameValue.trim();
    if (!title) {
      setRenameError("任务名称不能为空。");
      renameInputRef.current?.focus();
      return;
    }
    const saved = await onRenameThread(renameTarget.thread_id, title);
    if (saved) {
      setRenameTarget(null);
      return;
    }
    setRenameError("任务名称未保存，请重试。");
  };

  const copyThreadId = async (threadId: string) => {
    try {
      await navigator.clipboard.writeText(threadId);
      setThreadIdNotice("任务 ID 已复制。");
    } catch {
      setThreadIdNotice(`未能自动复制。任务 ID：${threadId}`);
    }
  };

  const continueById = async () => {
    const target = continueValue.trim();
    if (!target) {
      setContinueError("请输入任务 ID。");
      continueInputRef.current?.focus();
      return;
    }
    if (target.length > 256 || !target.startsWith("thr_")) {
      setContinueError("任务 ID 格式错误，请粘贴完整的 thr_ 开头 ID。");
      continueInputRef.current?.focus();
      return;
    }
    setContinuing(true);
    setContinueError(null);
    const opened = await onOpenThread(target);
    setContinuing(false);
    if (opened) {
      setContinueOpen(false);
      setContinueValue("");
      return;
    }
    setContinueError("未找到任务，或记录暂不可读。请核对 ID。");
  };

  const renderThreadEntry = (thread: ThreadProjection) => {
    const current = thread.thread_id === currentThreadId;
    const switching = switchingThreadId === thread.thread_id;
    const archiving = mutationKey === `archive:${thread.thread_id}`;
    const pinning = mutationKey === `${thread.pinned ? "unpin" : "pin"}:${thread.thread_id}`;
    const running = thread.active_turn_status !== null;
    const waiting = thread.active_turn_status === "waiting_human";
    const label = thread.title || "未命名会话";
    return (
      <div className="ex-task-entry" key={thread.thread_id}>
        <button
          className={`ex-task-row${current ? " is-current" : ""}`}
          type="button"
          title={label}
          aria-current={current ? "page" : undefined}
          aria-label={`打开任务：${label}`}
          disabled={switching || archiving || pinning}
          onClick={() => void onOpenThread(thread.thread_id)}
        >
          {switching
            ? <LoaderCircle className="ex-spin" aria-hidden="true" />
            : thread.pinned
              ? <Pin className="ex-task-pin" aria-hidden="true" />
              : <MessageSquare className="ex-task-compact-icon" aria-hidden="true" />}
          <span>{label}</span>
          {running ? (
            <LoaderCircle
              className={`ex-thread-running${waiting ? " is-waiting" : " ex-spin"}`}
              aria-label={waiting ? "等待你确认" : "任务正在进行"}
            />
          ) : null}
        </button>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <IconButton
              className="ex-task-more"
              label={`管理任务：${label}`}
              disabled={Boolean(switchingThreadId) || archiving || pinning}
              tooltipSide="right"
            >
              <MoreHorizontal aria-hidden="true" />
            </IconButton>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content className="ex-menu" side="right" align="start" sideOffset={8} collisionPadding={12}>
              <DropdownMenu.Item className="ex-menu-item" onSelect={() => void copyThreadId(thread.thread_id)}>
                <Copy aria-hidden="true" />复制任务 ID
              </DropdownMenu.Item>
              <DropdownMenu.Item className="ex-menu-item" onSelect={() => setRenameTarget(thread)}>
                <Pencil aria-hidden="true" />重命名
              </DropdownMenu.Item>
              <DropdownMenu.Item
                className="ex-menu-item"
                onSelect={() => void (thread.pinned
                  ? onUnpinThread(thread.thread_id)
                  : onPinThread(thread.thread_id))}
              >
                {thread.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
                {thread.pinned ? "取消置顶" : "置顶会话"}
              </DropdownMenu.Item>
              <DropdownMenu.Item className="ex-menu-item is-danger" onSelect={() => void onArchiveThread(thread.thread_id)}>
                <Archive aria-hidden="true" />归档
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
    );
  };

  return (
    <>
      <aside
        ref={sidebarRef}
        className={`ex-sidebar${open ? " is-open" : ""}`}
        aria-label="任务导航"
        role={modal ? "dialog" : undefined}
        aria-modal={modal || undefined}
      >
        <div className="ex-sidebar-brand">
          <span className="ex-emate-lockup">
            <span className="ex-visually-hidden">e-Mate v{version}</span>
            <span className="ex-emate-logo" aria-hidden="true" />
            <span className="ex-emate-mark-image" aria-hidden="true" />
            <small className="ex-emate-version" aria-hidden="true">v{version}</small>
          </span>
          <IconButton className="ex-sidebar-search" label="搜索会话" onClick={() => setSearchOpen(true)}>
            <Search aria-hidden="true" />
          </IconButton>
          <IconButton ref={closeButtonRef} className="ex-sidebar-close" label="关闭任务导航" onClick={onClose}>
            <X aria-hidden="true" />
          </IconButton>
        </div>

        <button className={`ex-new-task${homeActive ? " is-current" : ""}`} type="button" aria-label="新建任务" onClick={() => onNewTask(null)}>
          <Plus aria-hidden="true" />
          <span>新任务</span>
        </button>
        <button
          className={`ex-sidebar-action ex-sidebar-skill-link${creativeActive ? " is-current" : ""}`}
          type="button"
          aria-label="创意中心"
          aria-current={creativeActive ? "page" : undefined}
          onClick={onOpenCreative}
        >
          <Sparkles aria-hidden="true" />
          <span>创意中心</span>
        </button>
        <button
          className={`ex-sidebar-action ex-sidebar-skill-link${skillsActive ? " is-current" : ""}`}
          type="button"
          aria-label="能力中心"
          aria-current={skillsActive ? "page" : undefined}
          onClick={onOpenSkills}
        >
          <Blocks aria-hidden="true" />
          <span>能力中心</span>
        </button>
        <nav className="ex-task-nav" aria-label="会话与项目">
          <section className="ex-sidebar-section" aria-label="项目">
            <div className="ex-nav-heading">
              <button
                className="ex-sidebar-action ex-section-toggle"
                type="button"
                aria-label={projectsCollapsed ? "展开项目" : "折叠项目"}
                aria-expanded={!projectsCollapsed}
                onClick={() => setProjectsCollapsed((value) => !value)}
              >
                <ChevronDown className={projectsCollapsed ? "is-collapsed" : ""} aria-hidden="true" />
                <span>项目</span>
              </button>
              <IconButton
                className="ex-task-refresh"
                label={projectPickerBusy ? "正在选择项目文件夹" : "添加项目文件夹"}
                disabled={projectPickerBusy}
                onClick={() => void onPickProject()}
              >
                {projectPickerBusy
                  ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                  : <FolderOpen aria-hidden="true" />}
              </IconButton>
            </div>
            {projectCatalogError ? (
              <div className="ex-task-catalog-error" role="alert">
                <span>{projectCatalogError}</span>
                <IconButton label="关闭项目错误" onClick={onClearProjectError}><X aria-hidden="true" /></IconButton>
              </div>
            ) : null}
            {!projectsCollapsed ? (
              projectCatalogState === "loading" && projects.length === 0 ? (
                <p className="ex-task-loading" role="status"><LoaderCircle className="ex-spin" aria-hidden="true" /><span>正在加载项目…</span></p>
              ) : projects.length === 0 ? (
                <button className="ex-sidebar-action ex-project-empty" type="button" onClick={() => void onPickProject()} disabled={projectPickerBusy}>
                  <FolderOpen aria-hidden="true" /><span>添加项目文件夹</span>
                </button>
              ) : (
                <div className="ex-project-list">
                  {projects.map((project) => {
                    const projectThreads = activeThreads.filter((thread) => projectIdOf(thread) === project.project_id);
                    const allProjectSessionsVisible = Boolean(expandedProjectSessions[project.project_id]);
                    const visibleProjectThreads = collapsedThreadSet(projectThreads, allProjectSessionsVisible);
                    const collapsed = Boolean(collapsedProjects[project.project_id]);
                    return (
                      <div className="ex-project-group" key={project.project_id}>
                        <div className="ex-project-row">
                          <button
                            className="ex-sidebar-action ex-project-main"
                            type="button"
                            title={`${project.name}\n${project.project_path}`}
                            onClick={() => projectThreads[0]
                              ? void onOpenThread(projectThreads[0].thread_id)
                              : onNewTask(project)}
                          >
                            <FolderOpen aria-hidden="true" /><span>{project.name}</span>
                          </button>
                          <IconButton
                            label={collapsed ? `展开 ${project.name} 会话` : `折叠 ${project.name} 会话`}
                            onClick={() => setCollapsedProjects((current) => ({ ...current, [project.project_id]: !collapsed }))}
                          >
                            <ChevronDown className={collapsed ? "is-collapsed" : ""} aria-hidden="true" />
                          </IconButton>
                          <IconButton label={`为 ${project.name} 创建新会话`} onClick={() => onNewTask(project)}>
                            <Plus aria-hidden="true" />
                          </IconButton>
                        </div>
                        {!collapsed ? (
                          <div className="ex-project-session-list" role="group" aria-label={`${project.name} 的会话`}>
                            {projectThreads.length
                              ? visibleProjectThreads.map(renderThreadEntry)
                              : (
                                <button
                                  className="ex-sidebar-action ex-project-session-empty"
                                  type="button"
                                  aria-label={`为 ${project.name} 创建新会话`}
                                  onClick={() => onNewTask(project)}
                                >
                                  <Plus aria-hidden="true" /><span>新建项目会话</span>
                                </button>
                              )}
                            {projectThreads.length > COLLAPSED_THREAD_LIMIT ? (
                              <button
                                className="ex-sidebar-action ex-show-more-sessions"
                                type="button"
                                onClick={() => setExpandedProjectSessions((current) => ({
                                  ...current,
                                  [project.project_id]: !allProjectSessionsVisible,
                                }))}
                              >
                                {allProjectSessionsVisible ? "收起" : `查看更多（${projectThreads.length - visibleProjectThreads.length}）`}
                              </button>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              )
            ) : null}
          </section>

          {catalogError ? (
            <div className="ex-task-catalog-error" role="alert">
              <span>{catalogError}</span>
              <IconButton label="关闭任务目录错误" onClick={onClearCatalogError}>
                <X aria-hidden="true" />
              </IconButton>
            </div>
          ) : null}
          {threadIdNotice ? (
            <div className="ex-task-id-notice" role="status">
              <span>{threadIdNotice}</span>
              <IconButton label="关闭任务 ID 提示" onClick={() => setThreadIdNotice(null)}>
                <X aria-hidden="true" />
              </IconButton>
            </div>
          ) : null}

          <section className="ex-sidebar-section" aria-label="会话">
            <div className="ex-nav-heading">
              <button
                className="ex-sidebar-action ex-section-toggle"
                type="button"
                aria-label={sessionsCollapsed ? "展开会话" : "折叠会话"}
                aria-expanded={!sessionsCollapsed}
                onClick={() => setSessionsCollapsed((value) => !value)}
              >
                <ChevronDown className={sessionsCollapsed ? "is-collapsed" : ""} aria-hidden="true" />
                <span>会话</span><small>{generalThreads.length}</small>
              </button>
              <IconButton className="ex-task-refresh" label="刷新会话" disabled={catalogState === "loading"} onClick={onRefreshThreads}>
                <RefreshCw className={catalogState === "loading" ? "ex-spin" : ""} aria-hidden="true" />
              </IconButton>
            </div>
            {!sessionsCollapsed ? (
              catalogState === "loading" && threads.length === 0 ? (
                <p className="ex-task-loading" role="status"><LoaderCircle className="ex-spin" aria-hidden="true" /><span>正在加载会话…</span></p>
              ) : generalThreads.length === 0 ? (
                <p className="ex-nav-empty">暂无会话</p>
              ) : (
                <div className="ex-task-list">
                  {visibleGeneralThreads.map(renderThreadEntry)}
                  {generalThreads.length > COLLAPSED_THREAD_LIMIT ? (
                    <button
                      className="ex-sidebar-action ex-show-more-sessions"
                      type="button"
                      onClick={() => setAllGeneralSessionsVisible((value) => !value)}
                    >
                      {allGeneralSessionsVisible ? "收起" : `查看更多（${generalThreads.length - visibleGeneralThreads.length}）`}
                    </button>
                  ) : null}
                </div>
              )
            ) : null}
          </section>
          {archivedThreads.length ? (
            <details className="ex-archived-tasks">
              <summary>
                <ArchiveRestore aria-hidden="true" />
                <span className="ex-archived-label">已归档任务</span>
                <span className="ex-archived-count">{archivedThreads.length}</span>
              </summary>
              <div className="ex-task-list">
                {archivedThreads.map((thread) => {
                  const label = thread.title || "未命名任务";
                  const restoring = mutationKey === `restore:${thread.thread_id}`;
                  const deleting = mutationKey === `delete:${thread.thread_id}`;
                  return (
                    <div className="ex-task-entry is-archived" key={thread.thread_id}>
                      <button
                        className="ex-task-row"
                        type="button"
                        title={label}
                        aria-label={`查看已归档任务：${label}`}
                        disabled={Boolean(switchingThreadId) || restoring || deleting}
                        onClick={() => void onOpenThread(thread.thread_id)}
                      >
                        <Archive aria-hidden="true" />
                        <span>{label}</span>
                      </button>
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <IconButton
                            className="ex-task-more"
                            label={`管理已归档任务：${label}`}
                            disabled={Boolean(switchingThreadId) || restoring || deleting}
                            tooltipSide="right"
                          >
                            {restoring || deleting
                              ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                              : <MoreHorizontal aria-hidden="true" />}
                          </IconButton>
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content className="ex-menu" side="right" align="start" sideOffset={8} collisionPadding={12}>
                            <DropdownMenu.Item className="ex-menu-item" onSelect={() => void onRestoreThread(thread.thread_id)}>
                              <ArchiveRestore aria-hidden="true" />恢复任务
                            </DropdownMenu.Item>
                            <DropdownMenu.Item className="ex-menu-item is-danger" onSelect={() => setDeleteTarget(thread)}>
                              <Trash2 aria-hidden="true" />删除任务
                            </DropdownMenu.Item>
                          </DropdownMenu.Content>
                        </DropdownMenu.Portal>
                      </DropdownMenu.Root>
                    </div>
                  );
                })}
              </div>
            </details>
          ) : null}
        </nav>

        <div className="ex-sidebar-footer">
          <button
            className="ex-sidebar-action"
            type="button"
            data-ecorex-feature-trigger="settings"
            onClick={onOpenSettings}
          >
            <Settings2 aria-hidden="true" />
            <span>设置</span>
          </button>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                className="ex-sidebar-action ex-account-trigger"
                type="button"
                aria-label={`用户中心，${authenticated ? accountDisplayName || "已登录账号" : "未登录"}`}
              >
                <UserRound aria-hidden="true" />
                <span>用户中心</span>
                <ChevronDown className="ex-account-chevron" aria-hidden="true" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                className="ex-menu ex-account-menu"
                side="top"
                align="start"
                sideOffset={6}
                collisionPadding={12}
              >
                <DropdownMenu.Label className="ex-menu-note">
                  {authenticated ? accountDisplayName || "已登录账号" : "未登录"}
                </DropdownMenu.Label>
                {authenticated ? (
                  <DropdownMenu.Item
                    className="ex-menu-item is-danger"
                    disabled={sessionBusy}
                    onSelect={() => {
                      setLogoutComplete(null);
                      onClearSessionError();
                      setLogoutConfirmOpen(true);
                    }}
                  >
                    {sessionBusy
                      ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                      : <LogOut aria-hidden="true" />}
                    退出登录
                  </DropdownMenu.Item>
                ) : null}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </div>
      </aside>

      <Dialog.Root
        open={deleteTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !mutationKey?.startsWith("delete:")) setDeleteTarget(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content className="ex-dialog ex-confirm-dialog" aria-describedby="ex-delete-thread-description">
            <Dialog.Title>删除已归档任务？</Dialog.Title>
            <Dialog.Description id="ex-delete-thread-description">
              “{deleteTarget?.title || "未命名任务"}”将从任务列表移除且无法恢复，审计记录仍会保留。
            </Dialog.Description>
            <div className="ex-dialog-actions">
              <Dialog.Close className="ex-button" type="button" disabled={Boolean(mutationKey?.startsWith("delete:"))}>取消</Dialog.Close>
              <button
                className="ex-button is-danger"
                type="button"
                disabled={Boolean(mutationKey?.startsWith("delete:"))}
                onClick={async () => {
                  if (!deleteTarget) return;
                  if (await onDeleteThread(deleteTarget.thread_id)) setDeleteTarget(null);
                }}
              >
                {mutationKey?.startsWith("delete:") ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
                {mutationKey?.startsWith("delete:") ? "正在删除" : "确认删除"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={logoutConfirmOpen}
        onOpenChange={(nextOpen) => {
          setLogoutConfirmOpen(nextOpen);
          if (!nextOpen) {
            setLogoutComplete(null);
            onClearSessionError();
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content className="ex-dialog ex-confirm-dialog" aria-describedby="ex-logout-description">
            <Dialog.Title>{logoutComplete ? "已安全退出" : "退出 e-Mate？"}</Dialog.Title>
            <Dialog.Description id="ex-logout-description">
              {logoutComplete || "会话和本地产物会保留；托管凭证会从安全存储中撤销。"}
            </Dialog.Description>
            {sessionError ? <p className="ex-inline-error" role="alert">{sessionError}</p> : null}
            <div className="ex-dialog-actions">
              {logoutComplete ? (
                <Dialog.Close className="ex-button" type="button">关闭</Dialog.Close>
              ) : (
                <>
                  <Dialog.Close className="ex-button" type="button" disabled={sessionBusy}>取消</Dialog.Close>
                  <button
                    className="ex-button is-danger"
                    type="button"
                    disabled={sessionBusy}
                    aria-busy={sessionBusy}
                    onClick={async () => {
                      const receipt = await onLogout();
                      if (!receipt) return;
                      setLogoutComplete(receipt.restart_scheduled
                        ? "凭证已撤销，正在重启服务并刷新页面。"
                        : "凭证已撤销。运行服务重启后刷新页面即可重新登录。");
                    }}
                  >
                    {sessionBusy ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <LogOut aria-hidden="true" />}
                    {sessionBusy ? "正在退出" : "退出登录"}
                  </button>
                </>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={searchOpen}
        onOpenChange={(nextOpen) => {
          setSearchOpen(nextOpen);
          if (!nextOpen) setSearchQuery("");
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content className="ex-dialog ex-search-dialog" aria-describedby="ex-search-description">
            <Dialog.Title className="ex-visually-hidden">搜索会话</Dialog.Title>
            <Dialog.Description className="ex-visually-hidden" id="ex-search-description">
              按名称查找本机项目会话或通用会话。
            </Dialog.Description>
            <div className="ex-search-field">
              <Search aria-hidden="true" />
              <input
                autoFocus
                value={searchQuery}
                aria-label="搜索会话"
                placeholder="搜索会话"
                onChange={(event) => setSearchQuery(event.target.value)}
              />
              <Dialog.Close asChild>
                <IconButton label="关闭搜索"><X aria-hidden="true" /></IconButton>
              </Dialog.Close>
            </div>
            <div className="ex-search-results" role="list" aria-label="会话搜索结果">
              {searchResults.length ? searchResults.map((thread) => {
                const projectId = projectIdOf(thread);
                const project = projectId ? projects.find((item) => item.project_id === projectId) : null;
                return (
                  <button
                    className="ex-search-result"
                    type="button"
                    role="listitem"
                    key={thread.thread_id}
                    onClick={() => {
                      void onOpenThread(thread.thread_id).then((opened) => {
                        if (opened) setSearchOpen(false);
                      });
                    }}
                  >
                    {thread.pinned ? <Pin aria-hidden="true" /> : <MessageSquare aria-hidden="true" />}
                    <span><strong>{thread.title || "未命名会话"}</strong><small>{project?.name || "通用会话"}</small></span>
                  </button>
                );
              }) : <p className="ex-search-empty">没有匹配的会话</p>}
            </div>
            <button
              className="ex-search-continue"
              type="button"
              onClick={() => {
                setSearchOpen(false);
                setContinueOpen(true);
              }}
            >
              <MessageSquare aria-hidden="true" />
              <span><strong>按任务 ID 继续</strong><small>读取另一页面复制的 thr_ 任务 ID</small></span>
            </button>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={renameTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !renaming) setRenameTarget(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content
            className="ex-dialog ex-rename-dialog"
            aria-describedby="ex-rename-description"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              renameInputRef.current?.focus();
              renameInputRef.current?.select();
            }}
          >
            <div className="ex-dialog-heading">
              <div>
                <Dialog.Title>重命名任务</Dialog.Title>
                <Dialog.Description id="ex-rename-description">
                  新名称会保存在本机，并立即显示在任务列表中。
                </Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <IconButton label="关闭重命名" disabled={renaming}>
                  <X aria-hidden="true" />
                </IconButton>
              </Dialog.Close>
            </div>
            <label className="ex-field" htmlFor="ex-thread-title">
              <span>任务名称</span>
              <input
                ref={renameInputRef}
                id="ex-thread-title"
                value={renameValue}
                maxLength={200}
                disabled={renaming}
                aria-invalid={renameError ? "true" : undefined}
                aria-describedby="ex-thread-title-help"
                onChange={(event) => {
                  setRenameValue(event.target.value);
                  if (renameError) setRenameError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void saveRename();
                  }
                }}
              />
            </label>
            <p
              id="ex-thread-title-help"
              className={`ex-field-help${renameError ? " is-error" : ""}`}
              role={renameError ? "alert" : undefined}
            >
              {renameError || "最多 200 个字符。"}
            </p>
            <div className="ex-dialog-actions">
              <Dialog.Close asChild>
                <button className="ex-button" type="button" disabled={renaming}>取消</button>
              </Dialog.Close>
              <button
                className="ex-button is-primary"
                type="button"
                disabled={renaming || !renameValue.trim()}
                aria-busy={renaming}
                onClick={() => void saveRename()}
              >
                {renaming ? <LoaderCircle className="ex-spin" aria-hidden="true" /> : <Pencil aria-hidden="true" />}
                {renaming ? "正在保存" : "保存名称"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={continueOpen}
        onOpenChange={(nextOpen) => {
          if (continuing) return;
          setContinueOpen(nextOpen);
          if (!nextOpen) {
            setContinueError(null);
            setContinueValue("");
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="ex-dialog-overlay" />
          <Dialog.Content
            className="ex-dialog ex-rename-dialog"
            aria-describedby="ex-continue-thread-description"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              continueInputRef.current?.focus();
            }}
          >
            <div className="ex-dialog-heading">
              <div>
                <Dialog.Title>继续已有任务</Dialog.Title>
                <Dialog.Description id="ex-continue-thread-description">
                  e-Mate 会从本机完整恢复原任务和上下文，从上次的位置继续。
                </Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <IconButton label="关闭继续任务" disabled={continuing}>
                  <X aria-hidden="true" />
                </IconButton>
              </Dialog.Close>
            </div>
            <label className="ex-field" htmlFor="ex-thread-id">
              <span>任务 ID</span>
              <input
                ref={continueInputRef}
                id="ex-thread-id"
                value={continueValue}
                maxLength={256}
                disabled={continuing}
                autoComplete="off"
                spellCheck={false}
                placeholder="thr_..."
                aria-invalid={continueError ? "true" : undefined}
                aria-describedby="ex-thread-id-help"
                onChange={(event) => {
                  setContinueValue(event.target.value);
                  if (continueError) setContinueError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void continueById();
                  }
                }}
              />
            </label>
            <p
              id="ex-thread-id-help"
              className={`ex-field-help${continueError ? " is-error" : ""}`}
              role={continueError ? "alert" : undefined}
            >
              {continueError || "可从任务菜单复制完整 ID，再在其他页面打开。"}
            </p>
            <div className="ex-dialog-actions">
              <Dialog.Close className="ex-button" disabled={continuing}>取消</Dialog.Close>
              <button
                className="ex-button is-primary"
                type="button"
                disabled={continuing}
                aria-busy={continuing}
                onClick={() => void continueById()}
              >
                {continuing ? "正在读取" : "读取并继续"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
