import {
  lazy,
  memo,
  Suspense,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ArrowDown, FolderOpen, Workflow, WandSparkles } from "lucide-react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";

import type {
  ArtifactProjection,
  InteractionProjection,
  ItemProjection,
  InputAttachmentProjection,
  ModelDescriptor,
  ProjectProjection,
  PublicToolActivity,
  TurnProjection,
} from "../api/contracts.ts";
import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";
import { mergeArtifactProjections } from "../state/artifactActions.ts";
import type { FailedImageBatchSlot } from "../state/imageBatchFacts.ts";
import type { ImageArtifactGalleryViewSlot } from "./ArtifactShelf.tsx";
import { retouchPresentation, type RetouchPresentation } from "../state/retouchPresentation.ts";
import {
  artifactRevisionIdentity,
  selectUnbackedArtifactProjections,
} from "../state/timelineArtifacts.ts";
import {
  groupTimelineImageArtifacts,
  type TimelinePresentationBlock,
} from "../state/timelineImageGallery.ts";
import {
  buildTimelineTurns,
  type TimelineBlock,
  type TimelineTurn,
} from "../state/timelineTurns.ts";
import { InputAttachmentPreview, type InputAttachmentBlobLoader } from "./InputAttachmentPreview.tsx";
import OperationElapsed from "./OperationElapsed.tsx";

const OfficeMarkdown = lazy(() => import("./OfficeMarkdown.tsx"));
const TimelineActivity = lazy(() => import("./TimelineActivity.tsx"));
const NewConversationProjectSelector = lazy(() => import("./NewConversationProjectSelector.tsx"));
const ReasoningBlock = lazy(() => import("./ReasoningBlock.tsx"));
const TurnCompletionRow = lazy(() => import("./TurnCompletionRow.tsx"));
const ArtifactShelf = lazy(async () => ({ default: (await import("./ArtifactShelf.tsx")).ArtifactShelf }));
const ImageArtifactGallery = lazy(async () => ({ default: (await import("./ArtifactShelf.tsx")).ImageArtifactGallery }));

interface TimelineProps {
  items: ItemProjection[];
  turns: TurnProjection[];
  interactions: InteractionProjection[];
  chatModels: ModelDescriptor[];
  serverClockOffsetMs: number;
  activeTurn: TurnProjection | null;
  isThinking: boolean;
  artifacts: ArtifactProjection[];
  imageBatchFailures: FailedImageBatchSlot[];
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
  onLoadAttachment: InputAttachmentBlobLoader;
  onLoadAttachmentThumbnail: InputAttachmentBlobLoader;
}

const TIMELINE_BOTTOM_THRESHOLD_PX = 72;
const TIMELINE_SCROLL_SETTLE_MS = 80;

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
  return tryValidateArtifactProjection(item.content.artifact ?? item.content);
}

function phaseLabel(status: TurnProjection["status"] | undefined): string {
  switch (status) {
    case "queued": return "已排队";
    case "preparing": return "正在准备";
    case "model_requested": return "思考中";
    case "streaming": return "思考中";
    case "tool_pending": return "正在选择工具";
    case "waiting_human": return "等待你确认";
    case "tool_running": return "正在执行";
    case "retry_wait": return "等待重试";
    case "finalizing": return "正在检查结果";
    default: return "正在处理";
  }
}

function ThinkingOrb() {
  return (
    <span className="ex-thinking-orb-b5" aria-hidden="true">
      <span className="ex-thinking-orb-stage">
        <span className="ex-thinking-orb-shape is-a" />
        <span className="ex-thinking-orb-shape is-b" />
        <span className="ex-thinking-orb-shape is-c" />
      </span>
    </span>
  );
}

const MessageRow = memo(function MessageRow({
  item,
  onLoadAttachment,
  onLoadAttachmentThumbnail,
}: {
  item: ItemProjection;
  onLoadAttachment: InputAttachmentBlobLoader;
  onLoadAttachmentThumbnail: InputAttachmentBlobLoader;
}) {
  const user = role(item) === "user";
  const text = messageText(item);
  const attachments = user ? messageAttachments(item) : [];
  if (!text && !attachments.length) return null;
  const streaming = item.status === "in_progress";
  return (
    <article className={`ex-message is-${user ? "user" : "assistant"}${streaming ? " is-streaming" : ""}`}>
      <div className="ex-message-body" aria-busy={streaming || undefined}>
        {user && attachments.length ? (
          <div className="ex-message-attachments" aria-label="本条消息的附件">
            {attachments.map((attachment) => (
              <InputAttachmentPreview
                key={attachment.attachment_id}
                attachment={attachment}
                loadBlob={onLoadAttachment}
                loadThumbnailBlob={onLoadAttachmentThumbnail}
              />
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

function InteractionReceipt({ interaction }: { interaction: InteractionProjection }) {
  if (interaction.status === "pending") return null;
  const label = interaction.status === "resolved"
    ? "已处理"
    : interaction.status === "cancelled"
    ? "已取消"
    : "已过期";
  return (
    <div className="ex-interaction-receipt" data-status={interaction.status}>
      <span>{interaction.contract.title}</span>
      <small>{label}</small>
    </div>
  );
}

interface BlockProps {
  block: TimelineBlock;
  turn: TurnProjection;
  artifactByRevision: Map<string, ArtifactProjection>;
  imageBatchFailures: FailedImageBatchSlot[];
  artifactPreviewUrls: Record<string, string>;
  onArtifactAction: (artifact: ArtifactProjection, action: string) => void;
  onArtifactPreviewVisible: (artifact: ArtifactProjection) => void;
  retouchAvailable: boolean;
  retouchUnavailableReason: string | null;
  onLoadAttachment: InputAttachmentBlobLoader;
  onLoadAttachmentThumbnail: InputAttachmentBlobLoader;
  serverClockOffsetMs: number;
}

function TimelineBlockView({
  block,
  turn,
  artifactByRevision,
  artifactPreviewUrls,
  onArtifactAction,
  onArtifactPreviewVisible,
  retouchAvailable,
  retouchUnavailableReason,
  onLoadAttachment,
  onLoadAttachmentThumbnail,
  serverClockOffsetMs,
}: BlockProps) {
  if (block.kind === "interaction") {
    return <InteractionReceipt interaction={block.interaction} />;
  }
  const item = block.item;
  if (item.kind === "message") {
    return (
      <MessageRow
        item={item}
        onLoadAttachment={onLoadAttachment}
        onLoadAttachmentThumbnail={onLoadAttachmentThumbnail}
      />
    );
  }
  if (item.kind === "reasoning") {
    return (
      <Suspense fallback={<div className="ex-thinking-state" role="status"><ThinkingOrb />{phaseLabel(turn.status)}</div>}>
        <ReasoningBlock item={item} label={phaseLabel(turn.status)} />
      </Suspense>
    );
  }
  if (item.kind === "artifact") {
    const projected = artifactFrom(item);
    if (!projected) return null;
    const artifact = artifactByRevision.get(artifactRevisionIdentity(projected)) ?? projected;
    const retouch = retouchPresentation(item);
    if (retouch) {
      return <RetouchResultBlock
        artifact={artifact}
        retouch={retouch}
        previewUrl={artifactPreviewUrls[artifact.artifact_id] ?? null}
        onAction={onArtifactAction}
        onPreviewVisible={onArtifactPreviewVisible}
        retouchAvailable={retouchAvailable}
        retouchUnavailableReason={retouchUnavailableReason}
      />;
    }
    return (
      <Suspense fallback={<div className="ex-activity-row" role="status">正在载入产物…</div>}>
        <ArtifactShelf
          artifacts={[artifact]}
          previewUrls={artifactPreviewUrls}
          onAction={onArtifactAction}
          onPreviewVisible={onArtifactPreviewVisible}
          retouchAvailable={retouchAvailable}
          retouchUnavailableReason={retouchUnavailableReason}
        />
      </Suspense>
    );
  }
  const activity = item.kind === "tool_call" ? item.content as Partial<PublicToolActivity> : null;
  return (
    <Suspense fallback={<div className="ex-activity-row" role="status">正在更新工作步骤…</div>}>
      <TimelineActivity
        item={item}
        elapsed={activity ? <OperationElapsed
          timing={activity.timing}
          fallbackStartedAt={item.created_at}
          terminal={["completed", "failed", "cancelled"].includes(item.status)}
          serverClockOffsetMs={serverClockOffsetMs}
        /> : null}
      />
    </Suspense>
  );
}

function RetouchResultBlock({
  artifact,
  retouch,
  previewUrl,
  onAction,
  onPreviewVisible,
  retouchAvailable,
  retouchUnavailableReason,
}: {
  artifact: ArtifactProjection;
  retouch: RetouchPresentation;
  previewUrl: string | null;
  onAction: (artifact: ArtifactProjection, action: string) => void;
  onPreviewVisible: (artifact: ArtifactProjection) => void;
  retouchAvailable: boolean;
  retouchUnavailableReason: string | null;
}) {
  useEffect(() => onPreviewVisible(artifact), [artifact.artifact_id, artifact.revision_id, onPreviewVisible]);
  return (
    <section className="ex-retouch-result" data-retouch-preview-artifact-id={artifact.artifact_id}>
      <WandSparkles aria-hidden="true" />
      <div>
        <strong>精准修图已完成</strong>
        <p>{retouch.changeSummary}</p>
        <span>{retouch.inspectionRegionCount > 0 ? `已检查 ${retouch.inspectionRegionCount} 个修改区域。` : "已检查新修订。"}</span>
        {previewUrl ? (
          <button className="ex-retouch-result-media" type="button" data-artifact-preview-trigger={artifact.artifact_id} aria-label={`查看修图结果：${artifact.display_name}`} onClick={() => onAction(artifact, "preview")}>
            <img src={previewUrl} alt="" />
          </button>
        ) : <button className="ex-button" type="button" onClick={() => onPreviewVisible(artifact)}>载入预览</button>}
        <div className="ex-retouch-result-actions">
          <button className="ex-button" type="button" onClick={() => onAction(artifact, "preview")}>查看大图</button>
          <button
            className="ex-button is-primary"
            type="button"
            disabled={!retouchAvailable || !artifact.actions.includes("precise_retouch")}
            title={!retouchAvailable ? retouchUnavailableReason ?? "精准修图当前不可用" : undefined}
            onClick={() => onAction(artifact, "precise_retouch")}
          >继续修改</button>
        </div>
      </div>
    </section>
  );
}

interface TurnRowProps extends Omit<BlockProps, "block" | "turn"> {
  entry: TimelineTurn;
  modelSwitch: string | null;
}

const TurnRow = memo(function TurnRow({
  entry,
  modelSwitch,
  ...blockProps
}: TurnRowProps) {
  const presentationBlocks = groupTimelineImageArtifacts(
    entry.blocks,
    blockProps.imageBatchFailures.filter((failure) => failure.turnId === entry.turn.turn_id),
  );
  const firstAssistant = entry.assistantBlocks.find((block) => (
    block.kind !== "interaction" || block.interaction.status !== "pending"
  ));
  const copyText = [...entry.blocks].reverse().find((block) => (
    block.kind === "item"
    && block.item.kind === "message"
    && role(block.item) === "assistant"
    && messageText(block.item).trim()
  ));
  const renderBlock = (block: TimelinePresentationBlock) => {
    if (block.kind !== "image_gallery") {
      return <TimelineBlockView key={block.key} block={block} turn={entry.turn} {...blockProps} />;
    }
    const slots = block.slots.flatMap<ImageArtifactGalleryViewSlot>((slot) => {
      if (slot.kind === "failed") return [slot];
      const projected = artifactFrom(slot.block.item);
      if (!projected) return [];
      return [{
        kind: "artifact" as const,
        artifact: blockProps.artifactByRevision.get(artifactRevisionIdentity(projected)) ?? projected,
      }];
    });
    return (
      <Suspense key={block.key} fallback={<div className="ex-activity-row" role="status">正在载入图片…</div>}>
        <ImageArtifactGallery
          slots={slots}
          previewUrls={blockProps.artifactPreviewUrls}
          onAction={blockProps.onArtifactAction}
          onPreviewVisible={blockProps.onArtifactPreviewVisible}
        />
      </Suspense>
    );
  };
  const containsBlock = (candidate: TimelinePresentationBlock, key: string) => (
    candidate.kind === "image_gallery"
      ? candidate.slots.some((slot) => slot.kind === "artifact" && slot.block.key === key)
      : candidate.key === key
  );
  let headingRendered = false;
  return (
    <article
      className="ex-timeline-turn"
      data-turn-id={entry.turn.turn_id}
      data-turn-status={entry.turn.status}
    >
      {modelSwitch ? (
        <div className="ex-model-switch-divider" role="separator"><span>已切换至 {modelSwitch}</span></div>
      ) : null}
      {presentationBlocks.map((block) => {
        const showHeading = !headingRendered
          && firstAssistant !== undefined
          && containsBlock(block, firstAssistant.key);
        if (showHeading) headingRendered = true;
        return (
          <div key={block.key}>
            {showHeading ? (
              <div className="ex-assistant-heading"><span aria-hidden="true" /><strong>小芯</strong></div>
            ) : null}
            {renderBlock(block)}
          </div>
        );
      })}
      {!entry.terminal ? (
        <div className="ex-thinking-state ex-turn-running">
          <ThinkingOrb />
          <span className="ex-live-status" role="status">{phaseLabel(entry.turn.status)}</span>
          <span aria-hidden="true">{phaseLabel(entry.turn.status)} <OperationElapsed
              timing={entry.turn.timing}
              fallbackStartedAt={entry.turn.created_at}
              terminal={false}
              serverClockOffsetMs={blockProps.serverClockOffsetMs}
            /></span>
        </div>
      ) : null}
      {entry.terminal ? (
        <Suspense fallback={null}>
          <TurnCompletionRow
            turn={entry.turn}
            copyText={copyText?.kind === "item" ? messageText(copyText.item) : ""}
          />
        </Suspense>
      ) : null}
    </article>
  );
});

export function Timeline({
  items,
  turns,
  interactions,
  chatModels,
  serverClockOffsetMs,
  activeTurn,
  isThinking,
  artifacts,
  imageBatchFailures,
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
  onLoadAttachment,
  onLoadAttachmentThumbnail,
}: TimelineProps) {
  const timelineItems = useMemo(
    () => items.filter((item) => item.kind !== "task_list"),
    [items],
  );
  const timelineTurns = useMemo(
    () => buildTimelineTurns(turns, timelineItems, interactions),
    [interactions, timelineItems, turns],
  );
  const itemArtifacts = useMemo(
    () => items.filter((item) => item.kind === "artifact").map(artifactFrom).filter((artifact): artifact is ArtifactProjection => artifact !== null),
    [items],
  );
  const visibleArtifacts = useMemo(
    () => mergeArtifactProjections(itemArtifacts, artifacts),
    [artifacts, itemArtifacts],
  );
  const artifactByRevision = useMemo(
    () => new Map(artifacts.map((artifact) => [artifactRevisionIdentity(artifact), artifact])),
    [artifacts],
  );
  const fallbackArtifacts = useMemo(
    () => selectUnbackedArtifactProjections(itemArtifacts, visibleArtifacts),
    [itemArtifacts, visibleArtifacts],
  );
  const modelSwitches = useMemo(() => {
    const labels = new Map(chatModels.map((model) => [model.model_id, model.display_name]));
    const switches = new Map<string, string>();
    let previous: string | null = null;
    for (const turn of turns) {
      if (previous && turn.agent_model_id && turn.agent_model_id !== previous) {
        switches.set(turn.turn_id, labels.get(turn.agent_model_id) ?? turn.agent_model_id);
      }
      if (turn.agent_model_id) previous = turn.agent_model_id;
    }
    return switches;
  }, [chatModels, turns]);
  const [scrollParent, setScrollParent] = useState<HTMLElement | null>(null);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const followLatestRef = useRef(true);
  const followPausedByUserRef = useRef(false);
  const resumeAtBottomRef = useRef(false);
  const followedThreadIdRef = useRef<string | null>(null);
  const pausedAnchorRef = useRef<{ turnId: string; viewportOffset: number } | null>(null);
  const mountRef = useRef<HTMLDivElement>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const bottomSettleTimer = useRef<number | null>(null);
  const pausedAnchorTimer = useRef<number | null>(null);
  const contentRevision = timelineItems.map((item) => `${item.item_id}:${item.updated_at}:${messageText(item).length}`).join("|");
  const timelineThreadId = timelineTurns[0]?.turn.thread_id ?? null;
  const clearPausedAnchor = () => {
    if (scrollParent) {
      delete scrollParent.dataset.scrollAnchorTurnId;
      delete scrollParent.dataset.scrollAnchorOffset;
    }
    pausedAnchorRef.current = null;
  };
  const capturePausedAnchor = () => {
    if (!scrollParent) return;
    const viewport = scrollParent.getBoundingClientRect();
    const anchor = [...scrollParent.querySelectorAll<HTMLElement>(".ex-timeline-turn[data-turn-id]")]
      .map((row) => ({ row, bounds: row.getBoundingClientRect() }))
      .filter(({ bounds }) => bounds.bottom > viewport.top && bounds.top < viewport.bottom)
      .sort((left, right) => (
        Math.abs((left.bounds.top + left.bounds.bottom) / 2 - (viewport.top + viewport.bottom) / 2)
        - Math.abs((right.bounds.top + right.bounds.bottom) / 2 - (viewport.top + viewport.bottom) / 2)
      ))[0];
    if (anchor?.row.dataset.turnId) {
      clearPausedAnchor();
      pausedAnchorRef.current = {
        turnId: anchor.row.dataset.turnId,
        viewportOffset: anchor.bounds.top - viewport.top,
      };
      scrollParent.dataset.scrollAnchorTurnId = anchor.row.dataset.turnId;
      scrollParent.dataset.scrollAnchorOffset = String(
        anchor.bounds.top - viewport.top,
      );
    }
  };
  const restorePausedAnchor = () => {
    const anchor = pausedAnchorRef.current;
    if (!scrollParent || !followPausedByUserRef.current || !anchor) return;
    const row = [...scrollParent.querySelectorAll<HTMLElement>(".ex-timeline-turn[data-turn-id]")]
      .find((candidate) => candidate.dataset.turnId === anchor.turnId);
    if (!row) return;
    const viewport = scrollParent.getBoundingClientRect();
    const drift = row.getBoundingClientRect().top - viewport.top - anchor.viewportOffset;
    if (Math.abs(drift) > 0.5) scrollParent.scrollTop += drift;
  };

  useLayoutEffect(() => {
    if (
      timelineThreadId === null
      || timelineThreadId === followedThreadIdRef.current
    ) return;
    followedThreadIdRef.current = timelineThreadId;
    followPausedByUserRef.current = false;
    followLatestRef.current = true;
    resumeAtBottomRef.current = false;
    clearPausedAnchor();
    setShowJumpToLatest(false);
  }, [timelineThreadId]);

  useEffect(() => {
    setScrollParent(mountRef.current?.parentElement ?? null);
    return () => {
      if (bottomSettleTimer.current !== null) window.clearTimeout(bottomSettleTimer.current);
      if (pausedAnchorTimer.current !== null) window.clearTimeout(pausedAnchorTimer.current);
    };
  }, []);

  useEffect(() => {
    if (!scrollParent) return undefined;
    const readAtBottom = () => {
      const remaining = scrollParent.scrollHeight - scrollParent.clientHeight - scrollParent.scrollTop;
      return remaining <= TIMELINE_BOTTOM_THRESHOLD_PX;
    };
    const schedulePausedAnchorCapture = () => {
      if (pausedAnchorTimer.current !== null || pausedAnchorRef.current !== null) return;
      pausedAnchorTimer.current = window.setTimeout(() => {
        pausedAnchorTimer.current = null;
        if (followPausedByUserRef.current) capturePausedAnchor();
      }, TIMELINE_SCROLL_SETTLE_MS);
    };
    const syncFollowState = () => {
      const atBottom = readAtBottom();
      if (followPausedByUserRef.current && atBottom && resumeAtBottomRef.current) {
        followPausedByUserRef.current = false;
        followLatestRef.current = true;
        resumeAtBottomRef.current = false;
        clearPausedAnchor();
        setShowJumpToLatest(false);
      } else if (followPausedByUserRef.current) {
        followLatestRef.current = false;
        setShowJumpToLatest(true);
        schedulePausedAnchorCapture();
      } else if (atBottom) {
        followLatestRef.current = true;
        setShowJumpToLatest(false);
      } else if (followLatestRef.current) {
        scrollParent.scrollTop = scrollParent.scrollHeight;
        setShowJumpToLatest(false);
      } else {
        setShowJumpToLatest(true);
      }
    };
    const pauseFollowOnWheel = (event: WheelEvent) => {
      if (event.deltaY < 0 || followPausedByUserRef.current) {
        resumeAtBottomRef.current = event.deltaY > 0;
        followPausedByUserRef.current = true;
        followLatestRef.current = false;
        clearPausedAnchor();
        setShowJumpToLatest(true);
        schedulePausedAnchorCapture();
      }
    };
    const pauseFollowOnTouch = () => {
      followPausedByUserRef.current = true;
      followLatestRef.current = false;
      resumeAtBottomRef.current = false;
      setShowJumpToLatest(true);
      schedulePausedAnchorCapture();
    };
    const releaseTouchFollow = () => {
      if (readAtBottom()) {
        followPausedByUserRef.current = false;
        followLatestRef.current = true;
        resumeAtBottomRef.current = false;
        clearPausedAnchor();
        setShowJumpToLatest(false);
      } else {
        schedulePausedAnchorCapture();
      }
    };
    scrollParent.addEventListener("scroll", syncFollowState, { passive: true });
    scrollParent.addEventListener("wheel", pauseFollowOnWheel, { passive: true });
    scrollParent.addEventListener("touchmove", pauseFollowOnTouch, { passive: true });
    scrollParent.addEventListener("touchend", releaseTouchFollow, { passive: true });
    syncFollowState();
    return () => {
      if (pausedAnchorTimer.current !== null) {
        window.clearTimeout(pausedAnchorTimer.current);
        pausedAnchorTimer.current = null;
      }
      scrollParent.removeEventListener("scroll", syncFollowState);
      scrollParent.removeEventListener("wheel", pauseFollowOnWheel);
      scrollParent.removeEventListener("touchmove", pauseFollowOnTouch);
      scrollParent.removeEventListener("touchend", releaseTouchFollow);
    };
  }, [scrollParent]);

  useLayoutEffect(() => {
    if (!scrollParent || !followPausedByUserRef.current) return undefined;
    restorePausedAnchor();
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      restorePausedAnchor();
      secondFrame = window.requestAnimationFrame(restorePausedAnchor);
    });
    const settle = window.setTimeout(restorePausedAnchor, TIMELINE_SCROLL_SETTLE_MS);
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
      window.clearTimeout(settle);
    };
  }, [contentRevision, interactions, scrollParent, timelineTurns.length]);

  useEffect(() => {
    if (!followLatestRef.current || timelineTurns.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      if (!followLatestRef.current) return;
      virtuosoRef.current?.scrollToIndex({
        index: timelineTurns.length - 1,
        align: "end",
        behavior: "auto",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [contentRevision, interactions, scrollParent, timelineTurns.length]);

  const jumpToLatest = () => {
    followPausedByUserRef.current = false;
    followLatestRef.current = true;
    resumeAtBottomRef.current = false;
    clearPausedAnchor();
    setShowJumpToLatest(false);
    if (timelineTurns.length) {
      virtuosoRef.current?.scrollToIndex({ index: timelineTurns.length - 1, align: "end", behavior: "auto" });
      window.requestAnimationFrame(() => {
        if (followLatestRef.current && scrollParent) scrollParent.scrollTop = scrollParent.scrollHeight;
      });
      if (bottomSettleTimer.current !== null) window.clearTimeout(bottomSettleTimer.current);
      bottomSettleTimer.current = window.setTimeout(() => {
        bottomSettleTimer.current = null;
        followLatestRef.current = true;
        if (scrollParent) scrollParent.scrollTop = scrollParent.scrollHeight;
      }, TIMELINE_SCROLL_SETTLE_MS);
    }
  };

  if (!timelineTurns.length && !visibleArtifacts.length && !isThinking) {
    return (
      <div className="ex-empty-state ex-new-conversation-start">
        <h1>和小芯一起开始工作</h1>
        <p>{newConversationProject ? `${newConversationProject.name} 项目会话` : "选择一个开始方式"}</p>
        <div className="ex-new-conversation-options" role="group" aria-label="新会话入口">
          <button className={!newConversationProject ? "is-selected" : ""} type="button" aria-pressed={!newConversationProject} onClick={() => onSelectConversationProject(null)}>
            <Workflow aria-hidden="true" />
            <span><strong>通用会话</strong><small>不绑定项目，适合临时问答、资料整理和轻量任务。</small></span>
          </button>
          <Suspense fallback={<button className="ex-new-project-trigger" type="button" disabled><FolderOpen aria-hidden="true" /><span><strong>项目会话</strong><small>正在准备项目列表…</small></span></button>}>
            <NewConversationProjectSelector
              projects={projects}
              selectedProject={newConversationProject}
              pickerBusy={projectPickerBusy}
              onSelect={onSelectConversationProject}
              onPick={onPickProject}
            />
          </Suspense>
        </div>
        <p className="ex-new-conversation-note">{newConversationProject ? `将从 ${newConversationProject.name} 项目开始。` : "将从不绑定项目的通用会话开始。"}</p>
        {newConversationComposer ? <div className="ex-new-conversation-composer">{newConversationComposer}</div> : null}
      </div>
    );
  }

  const footer = fallbackArtifacts.length ? () => (
    <div className="ex-timeline-turn">
      <Suspense fallback={<div className="ex-activity-row" role="status">正在载入产物…</div>}>
        <ArtifactShelf
          artifacts={fallbackArtifacts}
          previewUrls={artifactPreviewUrls}
          onAction={onArtifactAction}
          onPreviewVisible={onArtifactPreviewVisible}
          retouchAvailable={retouchAvailable}
          retouchUnavailableReason={retouchUnavailableReason}
        />
      </Suspense>
    </div>
  ) : undefined;
  const latestTerminal = [...turns].reverse().find((turn) => (
    turn.status === "completed" || turn.status === "partial"
  ));
  return (
    <>
      <div ref={mountRef} className="ex-timeline-inner ex-timeline-virtualized">
        <div className="ex-live-status" aria-live="polite" aria-atomic="true">
          {latestTerminal ? <span key={latestTerminal.turn_id}>
            {latestTerminal.status === "partial" ? "小芯已返回部分结果" : "小芯已完成回复"}
          </span> : null}
        </div>
        {scrollParent ? (
          <Virtuoso
            ref={virtuosoRef}
            data={timelineTurns}
            customScrollParent={scrollParent}
            computeItemKey={(_index, entry) => entry.turn.turn_id}
            increaseViewportBy={{ top: 800, bottom: 800 }}
            atBottomThreshold={TIMELINE_BOTTOM_THRESHOLD_PX}
            followOutput={() => followLatestRef.current ? "auto" : false}
            totalListHeightChanged={() => {
              if (followPausedByUserRef.current) restorePausedAnchor();
              else if (followLatestRef.current) scrollParent.scrollTop = scrollParent.scrollHeight;
            }}
            components={{ Footer: footer }}
            itemContent={(_index, entry) => (
              <TurnRow
                entry={entry}
                modelSwitch={modelSwitches.get(entry.turn.turn_id) ?? null}
                artifactByRevision={artifactByRevision}
                imageBatchFailures={imageBatchFailures}
                artifactPreviewUrls={artifactPreviewUrls}
                onArtifactAction={onArtifactAction}
                onArtifactPreviewVisible={onArtifactPreviewVisible}
                retouchAvailable={retouchAvailable}
                retouchUnavailableReason={retouchUnavailableReason}
                onLoadAttachment={onLoadAttachment}
                onLoadAttachmentThumbnail={onLoadAttachmentThumbnail}
                serverClockOffsetMs={serverClockOffsetMs}
              />
            )}
          />
        ) : null}
      </div>
      {showJumpToLatest ? (
        <div className="ex-timeline-jump">
          <button className="ex-timeline-jump-button" type="button" aria-label="回到底部" title="回到底部" onClick={jumpToLatest}>
            <ArrowDown aria-hidden="true" />
          </button>
        </div>
      ) : null}
      {activeTurn && !timelineTurns.some((entry) => entry.turn.turn_id === activeTurn.turn_id) ? (
        <div className="ex-live-status" role="status">{phaseLabel(activeTurn.status)}</div>
      ) : null}
    </>
  );
}
