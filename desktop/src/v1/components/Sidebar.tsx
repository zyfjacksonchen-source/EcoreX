import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  Copy,
  FolderOpen,
  LoaderCircle,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Settings2,
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
  threads: ThreadProjection[];
  projects: ProjectProjection[];
  projectCatalogState: LoadState;
  projectCatalogError: string | null;
  projectPickerBusy: boolean;
  catalogState: LoadState;
  catalogError: string | null;
  switchingThreadId: string | null;
  mutationKey: string | null;
  onClose: () => void;
  onNewTask: (project?: ProjectProjection | null) => void;
  onPickProject: () => Promise<ProjectProjection | null>;
  onClearProjectError: () => void;
  onOpenThread: (threadId: string) => Promise<boolean>;
  onRenameThread: (threadId: string, title: string) => Promise<boolean>;
  onArchiveThread: (threadId: string) => Promise<boolean>;
  onRestoreThread: (threadId: string) => Promise<boolean>;
  onRefreshThreads: () => void;
  onClearCatalogError: () => void;
  onOpenSettings: () => void;
}

export function Sidebar({
  open,
  modal,
  currentThreadId,
  threads = [],
  projects = [],
  projectCatalogState,
  projectCatalogError,
  projectPickerBusy,
  catalogState,
  catalogError,
  switchingThreadId,
  mutationKey,
  onClose,
  onNewTask,
  onPickProject,
  onClearProjectError,
  onOpenThread,
  onRenameThread,
  onArchiveThread,
  onRestoreThread,
  onRefreshThreads,
  onClearCatalogError,
  onOpenSettings,
}: SidebarProps) {
  const [renameTarget, setRenameTarget] = useState<ThreadProjection | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [continueOpen, setContinueOpen] = useState(false);
  const [continueValue, setContinueValue] = useState("");
  const [continueError, setContinueError] = useState<string | null>(null);
  const [continuing, setContinuing] = useState(false);
  const [threadIdNotice, setThreadIdNotice] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const renameInputRef = useRef<HTMLInputElement>(null);
  const continueInputRef = useRef<HTMLInputElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const renaming = renameTarget ? mutationKey === `rename:${renameTarget.thread_id}` : false;
  const activeThreads = threads.filter((thread) => thread.status === "active");
  const archivedThreads = threads.filter((thread) => thread.status === "archived");
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase("zh-CN");
  const visibleThreads = activeThreads.filter((thread) => (
    !normalizedSearch
    || (thread.title || "未命名会话").toLocaleLowerCase("zh-CN").includes(normalizedSearch)
  ));
  const projectIdOf = (thread: ThreadProjection) => (
    typeof thread.metadata.project_id === "string" ? thread.metadata.project_id : null
  );
  const generalThreads = visibleThreads.filter((thread) => !projectIdOf(thread));

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
    const label = thread.title || "未命名会话";
    return (
      <div className="ex-task-entry" key={thread.thread_id}>
        <button
          className={`ex-task-row${current ? " is-current" : ""}`}
          type="button"
          title={label}
          aria-current={current ? "page" : undefined}
          aria-label={`打开任务：${label}`}
          disabled={switching || archiving}
          onClick={() => void onOpenThread(thread.thread_id)}
        >
          {switching
            ? <LoaderCircle className="ex-spin" aria-hidden="true" />
            : <MessageSquare aria-hidden="true" />}
          <span>{label}</span>
        </button>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <IconButton
              className="ex-task-more"
              label={`管理任务：${label}`}
              disabled={Boolean(switchingThreadId) || archiving}
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
          <span className="ex-brand-mark" aria-hidden="true">E</span>
          <span>EcoreX</span>
          <IconButton ref={closeButtonRef} className="ex-sidebar-close" label="关闭任务导航" onClick={onClose}>
            <X aria-hidden="true" />
          </IconButton>
        </div>

        <button className="ex-new-task" type="button" aria-label="新建任务" onClick={() => onNewTask(null)}>
          <Plus aria-hidden="true" />
          <span>新对话</span>
        </button>
        <div className="ex-session-search">
          <input
            value={searchQuery}
            aria-label="搜索会话"
            placeholder="搜索会话"
            onChange={(event) => setSearchQuery(event.target.value)}
          />
          <IconButton label="按任务 ID 继续" onClick={() => setContinueOpen(true)}>
            <MessageSquare aria-hidden="true" />
          </IconButton>
        </div>

        <nav className="ex-task-nav" aria-label="会话与项目">
          <section className="ex-sidebar-section" aria-label="项目">
            <div className="ex-nav-heading">
              <button
                className="ex-sidebar-action ex-section-toggle"
                type="button"
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
                    const projectThreads = visibleThreads.filter((thread) => projectIdOf(thread) === project.project_id);
                    const collapsed = Boolean(collapsedProjects[project.project_id]) && !normalizedSearch;
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
                          <div className="ex-project-session-list" aria-label={`${project.name} 的会话`}>
                            {projectThreads.length
                              ? projectThreads.map(renderThreadEntry)
                              : <button className="ex-sidebar-action ex-project-session-empty" type="button" onClick={() => onNewTask(project)}>新建项目会话</button>}
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
              <button className="ex-sidebar-action ex-section-toggle" type="button" aria-expanded={!sessionsCollapsed} onClick={() => setSessionsCollapsed((value) => !value)}>
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
              ) : <div className="ex-task-list">{generalThreads.map(renderThreadEntry)}</div>
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
                  return (
                    <div className="ex-task-entry is-archived" key={thread.thread_id}>
                      <div className="ex-task-row" title={label}>
                        <Archive aria-hidden="true" />
                        <span>{label}</span>
                      </div>
                      <IconButton
                        className="ex-task-more"
                        label={`恢复任务：${label}`}
                        disabled={Boolean(switchingThreadId) || restoring}
                        tooltipSide="right"
                        onClick={() => void onRestoreThread(thread.thread_id)}
                      >
                        {restoring
                          ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                          : <ArchiveRestore aria-hidden="true" />}
                      </IconButton>
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
            aria-label="设置"
            data-ecorex-feature-trigger="settings"
            onClick={onOpenSettings}
          >
            <Settings2 aria-hidden="true" />
            <span>设置</span>
          </button>
        </div>
      </aside>

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
                  EcoreX 会从本机完整恢复原任务和上下文，从上次的位置继续。
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
