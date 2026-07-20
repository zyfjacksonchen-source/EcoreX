import { Fragment, lazy, memo, Suspense, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, Check, CircleDashed, Copy, FileText, FolderOpen, Image, Workflow, WandSparkles } from "lucide-react";

import type {
  ArtifactProjection,
  ItemProjection,
  InputAttachmentProjection,
  ModelDescriptor,
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
const NewConversationProjectSelector = lazy(() => import("./NewConversationProjectSelector.tsx"));

interface TimelineProps {
  items: ItemProjection[];
  turns: TurnProjection[];
  chatModels: ModelDescriptor[];
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
  newConversationComposer: ReactNode;
}

const TIMELINE_BOTTOM_THRESHOLD_PX = 24;

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

function isNearTimelineBottom(element: HTMLElement): boolean {
  const remaining = element.scrollHeight - element.clientHeight - element.scrollTop;
  return remaining <= TIMELINE_BOTTOM_THRESHOLD_PX;
}

const TERMINAL_TURN_STATUSES = new Set<TurnProjection["status"]>([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "superseded",
]);

export function formatTurnDuration(turn: Pick<TurnProjection, "created_at" | "updated_at">): string {
  const startedAt = Date.parse(turn.created_at);
  const endedAt = Date.parse(turn.updated_at);
  const elapsedSeconds = Math.max(0, Math.round((endedAt - startedAt) / 1_000));
  if (!Number.isFinite(elapsedSeconds)) return "耗时未知";
  if (elapsedSeconds < 60) return `耗时 ${elapsedSeconds} 秒`;
  const elapsedMinutes = Math.floor(elapsedSeconds / 60);
  const remainingSeconds = elapsedSeconds % 60;
  if (elapsedMinutes < 60) {
    return remainingSeconds
      ? `耗时 ${elapsedMinutes} 分 ${remainingSeconds} 秒`
      : `耗时 ${elapsedMinutes} 分`;
  }
  const elapsedHours = Math.floor(elapsedMinutes / 60);
  const remainingMinutes = elapsedMinutes % 60;
  return remainingMinutes
    ? `耗时 ${elapsedHours} 小时 ${remainingMinutes} 分`
    : `耗时 ${elapsedHours} 小时`;
}

const TurnCompletionRow = memo(function TurnCompletionRow({
  turn,
  copyText,
}: {
  turn: TurnProjection;
  copyText: string;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const resetTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  const copyReply = async () => {
    if (!copyText.trim() || copyState === "copied") return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(copyText);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setCopyState("idle"), 2_000);
  };

  return (
    <div className="ex-turn-completion" data-turn-status={turn.status}>
      <span>{formatTurnDuration(turn)}</span>
      {copyText.trim() ? (
        <button
          className="ex-icon-button ex-turn-copy"
          type="button"
          aria-label={copyState === "copied" ? "回复已复制" : "复制本次回复"}
          title={copyState === "copied" ? "已复制" : "复制回复"}
          onClick={() => void copyReply()}
        >
          {copyState === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </button>
      ) : null}
      <span className="ex-turn-copy-notice" aria-live="polite">
        {copyState === "copied" ? "已复制" : copyState === "error" ? "复制失败" : ""}
      </span>
    </div>
  );
});

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
  turns,
  chatModels,
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
  newConversationComposer,
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
  const modelSwitches = useMemo(() => {
    const labels = new Map(chatModels.map((model) => [model.model_id, model.display_name]));
    const firstItemByTurn = new Map<string, string>();
    for (const item of timelineEntries) {
      if (!firstItemByTurn.has(item.turn_id)) firstItemByTurn.set(item.turn_id, item.item_id);
    }
    const switches = new Map<string, string>();
    let previousModel: string | null = null;
    for (const turn of [...turns].sort((left, right) => left.created_at.localeCompare(right.created_at))) {
      const currentModel = turn.agent_model_id;
      const firstItemId = firstItemByTurn.get(turn.turn_id);
      if (previousModel && currentModel && currentModel !== previousModel && firstItemId) {
        switches.set(firstItemId, labels.get(currentModel) || currentModel);
      }
      if (currentModel) previousModel = currentModel;
    }
    return switches;
  }, [chatModels, timelineEntries, turns]);
  const turnCompletions = useMemo(() => {
    const terminalTurns = new Map(
      turns
        .filter((turn) => TERMINAL_TURN_STATUSES.has(turn.status))
        .map((turn) => [turn.turn_id, turn]),
    );
    const lastItemByTurn = new Map<string, string>();
    const lastReplyByTurn = new Map<string, string>();
    for (const item of timelineEntries) {
      if (!terminalTurns.has(item.turn_id)) continue;
      lastItemByTurn.set(item.turn_id, item.item_id);
      if (item.kind === "message" && role(item) === "assistant" && messageText(item)) {
        lastReplyByTurn.set(item.turn_id, messageText(item));
      }
    }
    const result = new Map<string, { turn: TurnProjection; copyText: string }>();
    for (const [turnId, itemId] of lastItemByTurn) {
      const turn = terminalTurns.get(turnId);
      if (turn) result.set(itemId, { turn, copyText: lastReplyByTurn.get(turnId) ?? "" });
    }
    return result;
  }, [timelineEntries, turns]);
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
  const followLatestRef = useRef(true);
  const pendingJumpToLatestRef = useRef(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const retouchPreviewIdentity = retouchResults
    .map((result) => (
      `${result.artifact.artifact_id}:${result.artifact.revision_id}:`
      + String(result.artifact.actions.includes("preview"))
    ))
    .join("|");

  const jumpToLatest = () => {
    const scroller = timelineRef.current?.parentElement;
    pendingJumpToLatestRef.current = false;
    followLatestRef.current = true;
    setShowJumpToLatest(false);
    if (scroller) {
      window.requestAnimationFrame(() => {
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: "auto" });
      });
    }
  };

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
  }, [onArtifactPreviewVisible, retouchPreviewIdentity, retouchResults]);

  useEffect(() => {
    const scroller = timelineRef.current?.parentElement;
    if (!scroller) return;
    const syncScrollState = () => {
      const atBottom = isNearTimelineBottom(scroller);
      followLatestRef.current = atBottom;
      setShowJumpToLatest(!atBottom);
    };
    syncScrollState();
    scroller.addEventListener("scroll", syncScrollState, { passive: true });
    return () => scroller.removeEventListener("scroll", syncScrollState);
  }, [
    isThinking,
    timelineEntries.length,
    retouchResults.length,
    visibleArtifacts.length,
    visibleReasoning,
  ]);

  useEffect(() => {
    const scroller = timelineRef.current?.parentElement;
    if (!scroller || !messageWindow.atLatest) return;
    if (!followLatestRef.current && !pendingJumpToLatestRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      scroller.scrollTo({ top: scroller.scrollHeight, behavior: "auto" });
      followLatestRef.current = true;
      pendingJumpToLatestRef.current = false;
      setShowJumpToLatest(false);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    isThinking,
    messageWindow.atLatest,
    retouchResults.length,
    timelineEntries.length,
    visibleArtifacts.length,
    visibleReasoning,
  ]);
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
        <div className="ex-new-conversation-options" role="group" aria-label="新会话入口">
          <button
            className={!newConversationProject ? "is-selected" : ""}
            type="button"
            aria-pressed={!newConversationProject}
            onClick={() => onSelectConversationProject(null)}
          >
            <Workflow aria-hidden="true" />
            <span><strong>通用会话</strong><small>不绑定项目，适合临时问答、资料整理和轻量任务。</small></span>
          </button>
          <Suspense fallback={(
              <button
                className={`ex-new-project-trigger${newConversationProject ? " is-selected" : ""}`}
                type="button"
                aria-label="选择项目会话"
                aria-pressed={Boolean(newConversationProject)}
                disabled
              >
                <FolderOpen aria-hidden="true" />
                <span>
                  <strong>{newConversationProject?.name || "项目会话"}</strong>
                  <small>正在准备项目列表…</small>
                </span>
              </button>
          )}>
            <NewConversationProjectSelector
              projects={projects}
              selectedProject={newConversationProject}
              pickerBusy={projectPickerBusy}
              onSelect={onSelectConversationProject}
              onPick={onPickProject}
            />
          </Suspense>
        </div>
        <p className="ex-new-conversation-note">
          {newConversationProject
            ? `将从 ${newConversationProject.name} 项目开始，不会自动复用旧项目会话。`
            : "将从不绑定项目的通用会话开始，不会串入项目文件夹上下文。"}
        </p>
        {newConversationComposer ? (
          <div className="ex-new-conversation-composer">
            {newConversationComposer}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <>
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
        {messageWindow.items.map((item) => {
          const completion = turnCompletions.get(item.item_id);
          return (
          <Fragment key={item.item_id}>
            {modelSwitches.has(item.item_id) ? (
              <div className="ex-model-switch-divider" role="separator">
                <span>已切换至 {modelSwitches.get(item.item_id)}</span>
              </div>
            ) : null}
            {item.kind === "message"
              ? <MessageRow item={item} />
              : (
                <Suspense fallback={<div className="ex-activity-row" role="status">正在更新工作步骤…</div>}>
                  <TimelineActivity item={item} />
                </Suspense>
              )}
            {completion ? (
              <TurnCompletionRow turn={completion.turn} copyText={completion.copyText} />
            ) : null}
          </Fragment>
          );
        })}
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
              onClick={() => {
                pendingJumpToLatestRef.current = true;
                setHistoryEndAnchorId(null);
                jumpToLatest();
              }}
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
      {showJumpToLatest ? (
        <div className="ex-timeline-jump">
          <button
            className="ex-button is-primary ex-timeline-jump-button"
            type="button"
            onClick={() => {
              pendingJumpToLatestRef.current = true;
              setHistoryEndAnchorId(null);
              jumpToLatest();
            }}
          >
            <ArrowDown aria-hidden="true" />
            回到底部
          </button>
        </div>
      ) : null}
    </>
  );
}
