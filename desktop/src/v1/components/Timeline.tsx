import { lazy, memo, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { CircleDashed, FileText, FolderOpen, Image, Workflow, WandSparkles } from "lucide-react";

import type {
  ArtifactProjection,
  ItemProjection,
  InputAttachmentProjection,
  ProjectProjection,
  TurnProjection,
} from "../api/contracts.ts";
import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";
import { retouchPresentation } from "../state/retouchPresentation.ts";
import { mergeArtifactProjections } from "../state/artifactActions.ts";
import {
  earlierTimelineAnchor,
  newerTimelineAnchor,
  selectTimelineWindow,
  TIMELINE_WINDOW_SIZE,
} from "../state/timelineWindow.ts";
import { ArtifactShelf } from "./ArtifactShelf.tsx";

const OfficeMarkdown = lazy(() => import("./OfficeMarkdown.tsx"));
const TimelineActivity = lazy(() => import("./TimelineActivity.tsx"));

interface TimelineProps {
  items: ItemProjection[];
  activeTurn: TurnProjection | null;
  isThinking: boolean;
  visibleReasoning: ItemProjection | null;
  artifacts: ArtifactProjection[];
  artifactPreviewUrls: Record<string, string>;
  onArtifactAction: (artifact: ArtifactProjection, action: string) => void;
  onArtifactPreviewVisible: (artifact: ArtifactProjection) => void;
  retouchAvailable: boolean;
  retouchUnavailableReason: string | null;
  projects: ProjectProjection[];
  newConversationProject: ProjectProjection | null;
  projectPickerBusy: boolean;
  onSelectConversationProject: (project: ProjectProjection | null) => void;
  onPickProject: () => Promise<ProjectProjection | null>;
}

function role(item: ItemProjection): string {
  return typeof item.content.role === "string" ? item.content.role : "assistant";
}

function messageText(item: ItemProjection): string {
  const value = item.content.text ?? item.content.content ?? "";
  return typeof value === "string" ? value : "";
}

function messageAttachments(item: ItemProjection): InputAttachmentProjection[] {
  const metadata = item.content.metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return [];
  const raw = (metadata as Record<string, unknown>).input_attachments;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const attachment = value as Record<string, unknown>;
    if (
      typeof attachment.attachment_id !== "string"
      || typeof attachment.revision_id !== "string"
      || typeof attachment.display_name !== "string"
      || typeof attachment.mime_type !== "string"
      || typeof attachment.size_bytes !== "number"
      || !["image", "document", "file"].includes(String(attachment.media_kind))
      || typeof attachment.sha256 !== "string"
      || typeof attachment.created_at !== "string"
    ) return [];
    return [attachment as unknown as InputAttachmentProjection];
  });
}

function artifactFrom(item: ItemProjection): ArtifactProjection | null {
  const raw = item.content.artifact ?? item.content;
  return tryValidateArtifactProjection(raw);
}

function phaseLabel(status: TurnProjection["status"] | undefined): string {
  switch (status) {
    case "queued": return "已排队";
    case "preparing": return "正在准备";
    case "model_requested": return "正在思考";
    case "streaming": return "正在组织结果";
    case "tool_pending": return "正在选择工具";
    case "tool_running": return "正在执行";
    case "retry_wait": return "等待重试";
    case "finalizing": return "正在检查产物";
    default: return "正在处理";
  }
}

const MessageRow = memo(function MessageRow({ item }: { item: ItemProjection }) {
  const user = role(item) === "user";
  const text = messageText(item);
  const attachments = user ? messageAttachments(item) : [];
  if (!text && !attachments.length) return null;
  const streaming = item.status === "in_progress";
  return (
    <article
      className={`ex-message is-${user ? "user" : "assistant"}${streaming ? " is-streaming" : ""}`}
    >
      <div className="ex-message-body" aria-busy={streaming || undefined}>
        {user && attachments.length ? (
          <div className="ex-message-attachments" aria-label="本条消息的附件">
            {attachments.map((attachment) => (
              <span key={attachment.attachment_id} title={attachment.display_name}>
                {attachment.media_kind === "image" ? <Image aria-hidden="true" /> : <FileText aria-hidden="true" />}
                {attachment.display_name}
              </span>
            ))}
          </div>
        ) : null}
        {user ? (
          <p className="ex-message-plain" aria-live="off">{text}</p>
        ) : (
          <Suspense fallback={<p className="ex-message-plain" aria-live="off">{text}</p>}>
            <OfficeMarkdown text={text} streaming={streaming} />
          </Suspense>
        )}
      </div>
    </article>
  );
});

export function Timeline({
  items,
  activeTurn,
  isThinking,
  visibleReasoning,
  artifacts,
  artifactPreviewUrls,
  onArtifactAction,
  onArtifactPreviewVisible,
  retouchAvailable,
  retouchUnavailableReason,
  projects,
  newConversationProject,
  projectPickerBusy,
  onSelectConversationProject,
  onPickProject,
}: TimelineProps) {
  const messages = useMemo(
    () => items.filter((item) => item.kind === "message"),
    [items],
  );
  const timelineEntries = useMemo(
    () => items.filter((item) => (
      item.kind === "message" || item.kind === "tool_call" || item.kind === "checkpoint"
    )),
    [items],
  );
  const [historyEndAnchorId, setHistoryEndAnchorId] = useState<string | null>(null);
  const messageWindow = useMemo(
    () => selectTimelineWindow(timelineEntries, historyEndAnchorId),
    [historyEndAnchorId, timelineEntries],
  );
  useEffect(() => {
    if (messageWindow.anchorMissing) setHistoryEndAnchorId(null);
  }, [messageWindow.anchorMissing]);
  const itemArtifacts = useMemo(
    () => items
      .filter((item) => item.kind === "artifact")
      .map(artifactFrom)
      .filter((artifact): artifact is ArtifactProjection => artifact !== null),
    [items],
  );
  const retouchResults = useMemo(
    () => items
      .map(retouchPresentation)
      .filter((result): result is NonNullable<typeof result> => result !== null),
    [items],
  );
  const visibleArtifacts = useMemo(
    () => mergeArtifactProjections(itemArtifacts, artifacts),
    [artifacts, itemArtifacts],
  );
  const timelineRef = useRef<HTMLDivElement>(null);
  const retouchPreviewIdentity = retouchResults
    .map((result) => (
      `${result.artifact.artifact_id}:${result.artifact.revision_id}:`
      + String(result.artifact.actions.includes("preview"))
    ))
    .join("|");
  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline || !retouchPreviewIdentity) return;
    const byId = new Map(
      retouchResults.map((result) => [result.artifact.artifact_id, result.artifact]),
    );
    const candidates = [...timeline.querySelectorAll<HTMLElement>(
      "[data-retouch-preview-artifact-id]",
    )];
    const reveal = (element: HTMLElement) => {
      const artifactId = element.dataset.retouchPreviewArtifactId;
      const artifact = artifactId ? byId.get(artifactId) : undefined;
      if (artifact) onArtifactPreviewVisible(artifact);
    };
    if (!("IntersectionObserver" in window)) {
      candidates.forEach(reveal);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const element = entry.target as HTMLElement;
        observer.unobserve(element);
        reveal(element);
      }
    }, { rootMargin: "240px 0px" });
    candidates.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [onArtifactPreviewVisible, retouchPreviewIdentity]);
  let latestCompletedAssistantId: string | null = null;
  for (const message of messages) {
    if (role(message) === "assistant" && message.status === "completed") {
      latestCompletedAssistantId = message.item_id;
    }
  }

  if (
    !timelineEntries.length
    && !itemArtifacts.length
    && !artifacts.length
    && !retouchResults.length
    && !isThinking
    && !visibleReasoning
  ) {
    return (
      <div className="ex-empty-state ex-new-conversation-start">
        <h1>和 EcoreX 一起开始工作</h1>
        <p>{newConversationProject ? `${newConversationProject.name} 项目会话` : "选择一个开始方式"}</p>
        <div className="ex-new-conversation-options" aria-label="新会话入口">
          <button
            className={!newConversationProject ? "is-selected" : ""}
            type="button"
            aria-pressed={!newConversationProject}
            onClick={() => onSelectConversationProject(null)}
          >
            <Workflow aria-hidden="true" />
            <span><strong>通用会话</strong><small>不绑定项目，适合临时问答、资料整理和轻量任务。</small></span>
          </button>
          <div className="ex-new-project-options">
            {projects.map((project) => (
              <button
                className={newConversationProject?.project_id === project.project_id ? "is-selected" : ""}
                type="button"
                key={project.project_id}
                aria-pressed={newConversationProject?.project_id === project.project_id}
                title={project.project_path}
                onClick={() => onSelectConversationProject(project)}
              >
                <FolderOpen aria-hidden="true" />
                <span><strong>{project.name}</strong><small>使用此项目文件夹开启独立项目会话。</small></span>
              </button>
            ))}
            <button type="button" disabled={projectPickerBusy} onClick={() => void onPickProject().then((project) => {
              if (project) onSelectConversationProject(project);
            })}>
              <FolderOpen className={projectPickerBusy ? "ex-spin" : ""} aria-hidden="true" />
              <span><strong>{projectPickerBusy ? "正在选择" : "项目文件夹"}</strong><small>选择已有目录，作为本次会话的项目上下文。</small></span>
            </button>
          </div>
        </div>
        <p className="ex-new-conversation-note">
          {newConversationProject
            ? `将从 ${newConversationProject.name} 项目开始，不会自动复用旧项目会话。`
            : "将从不绑定项目的通用会话开始，不会串入项目文件夹上下文。"}
        </p>
      </div>
    );
  }

  return (
    <div ref={timelineRef} className="ex-timeline-inner">
      <div className="ex-live-status" aria-live="polite" aria-atomic="true">
        {latestCompletedAssistantId ? (
          <span key={latestCompletedAssistantId}>EcoreX 已完成回复</span>
        ) : null}
      </div>
      {messageWindow.hiddenBefore > 0 ? (
        <div className="ex-timeline-history-nav is-before">
          <button
            className="ex-button"
            type="button"
            onClick={() => setHistoryEndAnchorId(
              earlierTimelineAnchor(timelineEntries, messageWindow),
            )}
          >
            显示更早的 {Math.min(TIMELINE_WINDOW_SIZE, messageWindow.hiddenBefore)} 条记录
          </button>
        </div>
      ) : null}
      {!messageWindow.atLatest ? (
        <p className="ex-timeline-history-status" role="status">
          正在查看历史消息；当前任务的最新进度和产物仍在末尾。
        </p>
      ) : null}
      {messageWindow.items.map((item) => (
        item.kind === "message"
          ? <MessageRow item={item} key={item.item_id} />
          : (
            <Suspense
              fallback={<div className="ex-activity-row" role="status">正在更新工作步骤…</div>}
              key={item.item_id}
            >
              <TimelineActivity item={item} />
            </Suspense>
          )
      ))}
      {!messageWindow.atLatest ? (
        <div className="ex-timeline-history-nav is-after" role="group" aria-label="历史消息翻页">
          <button
            className="ex-button"
            type="button"
            onClick={() => setHistoryEndAnchorId(
              newerTimelineAnchor(timelineEntries, messageWindow),
            )}
          >
            显示较新的消息
          </button>
          <button
            className="ex-button is-primary"
            type="button"
            onClick={() => setHistoryEndAnchorId(null)}
          >
            回到最新消息
          </button>
        </div>
      ) : null}
      {messageWindow.atLatest ? retouchResults.map((result) => (
        <section
          className="ex-retouch-result"
          data-retouch-preview-artifact-id={result.artifact.artifact_id}
          key={`retouch-${result.artifact.revision_id}`}
        >
          <WandSparkles aria-hidden="true" />
          <div>
            <strong>精准修图已完成</strong>
            <p>{result.changeSummary}</p>
            <span>
              {result.inspectionRegionCount > 0
                ? `已检查 ${result.inspectionRegionCount} 个修改区域。请看一眼下方新图片。`
                : "已检查新修订。请看一眼下方图片。"}
            </span>
            {artifactPreviewUrls[result.artifact.artifact_id] ? (
              <button
                className="ex-retouch-result-media"
                type="button"
                onClick={() => onArtifactAction(result.artifact, "preview")}
              >
                <img
                  src={artifactPreviewUrls[result.artifact.artifact_id]}
                  alt={`查看修图结果：${result.artifact.display_name}`}
                />
              </button>
            ) : (
              <div className="ex-retouch-result-loading" role="status">正在载入新修订预览…</div>
            )}
            <div className="ex-retouch-result-actions">
              <button className="ex-button" type="button" onClick={() => onArtifactAction(result.artifact, "preview")}>查看大图</button>
              <button
                className="ex-button is-primary"
                type="button"
                disabled={!retouchAvailable || !result.artifact.actions.includes("precise_retouch")}
                title={!retouchAvailable ? retouchUnavailableReason ?? "精准修图当前不可用" : undefined}
                onClick={() => onArtifactAction(result.artifact, "precise_retouch")}
              >继续修改</button>
            </div>
          </div>
        </section>
      )) : null}
      {messageWindow.atLatest ? (
        <ArtifactShelf
          artifacts={visibleArtifacts}
          previewUrls={artifactPreviewUrls}
          onAction={onArtifactAction}
          onPreviewVisible={onArtifactPreviewVisible}
          retouchAvailable={retouchAvailable}
          retouchUnavailableReason={retouchUnavailableReason}
        />
      ) : null}
      {messageWindow.atLatest && (isThinking || visibleReasoning) ? (
        <div className={`ex-thinking${visibleReasoning ? " has-summary" : ""}`}>
          <CircleDashed aria-hidden="true" />
          <div>
            <span role="status" aria-live="polite" aria-atomic="true">
              {phaseLabel(activeTurn?.status)}
            </span>
            {visibleReasoning && typeof visibleReasoning.content.text === "string" ? (
              <p aria-live="off">{visibleReasoning.content.text}</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
