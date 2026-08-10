import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Download,
  Eye,
  ExternalLink,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  MoreHorizontal,
  ThumbsDown,
  ThumbsUp,
  WandSparkles,
  X,
} from "lucide-react";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import type { ArtifactProjection } from "../api/contracts.ts";
import {
  artifactUiActions,
  type ArtifactUiAction,
} from "../state/artifactActions.ts";
import type { FailedImageBatchSlot } from "../state/imageBatchFacts.ts";
import {
  artifactFamilyLabel,
  formatFileSize,
  serviceReasonMessage,
} from "../state/userLanguage.ts";
import { IconButton } from "./IconButton.tsx";

interface ArtifactShelfProps {
  artifacts: ArtifactProjection[];
  previewUrls: Record<string, string>;
  onAction: (artifact: ArtifactProjection, action: string) => void;
  onPreviewVisible: (artifact: ArtifactProjection) => void;
  retouchAvailable: boolean;
  retouchUnavailableReason: string | null;
}

const ACTION_LABELS: Record<ArtifactUiAction, string> = {
  thumbs_up: "有帮助",
  thumbs_down: "需要改进",
  precise_retouch: "精准修图",
  preview: "预览",
  open: "打开",
  reveal: "在文件夹中显示",
  download: "保存到默认位置",
};

const ACTION_ICONS = {
  thumbs_up: ThumbsUp,
  thumbs_down: ThumbsDown,
  precise_retouch: WandSparkles,
  preview: Eye,
  open: ExternalLink,
  reveal: FolderOpen,
  download: Download,
} satisfies Record<ArtifactUiAction, typeof Download>;

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

interface OverflowActionsProps {
  artifact: ArtifactProjection;
  onAction: ArtifactShelfProps["onAction"];
  includeContextual: boolean;
  asSheet: boolean;
  retouchAvailable: boolean;
  retouchUnavailableReason: string | null;
}

function actionDisabled(action: ArtifactUiAction, retouchAvailable: boolean): boolean {
  return action === "precise_retouch" && !retouchAvailable;
}

function retouchUnavailableMessage(reason: string | null): string {
  return serviceReasonMessage(
    reason,
    "精准修图暂时不可用，请稍后重试。",
  );
}

function MoreMenu({
  artifact,
  onAction,
  includeContextual,
  retouchAvailable,
  retouchUnavailableReason,
}: Omit<OverflowActionsProps, "asSheet">) {
  const actions = artifactUiActions(artifact.actions, includeContextual);
  if (!actions.length) return null;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="ex-icon-button ex-artifact-more" type="button" aria-label={`更多：${artifact.display_name}`}>
          <MoreHorizontal aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="ex-menu" sideOffset={8} align="end">
          {actions.map((action) => {
            const Icon = ACTION_ICONS[action];
            const disabled = actionDisabled(action, retouchAvailable);
            return (
              <DropdownMenu.Item
                className="ex-menu-item"
                key={action}
                disabled={disabled}
                onSelect={() => onAction(artifact, action)}
              >
                <Icon aria-hidden="true" />
                {ACTION_LABELS[action]}
              </DropdownMenu.Item>
            );
          })}
          {includeContextual
            && artifact.actions.includes("precise_retouch")
            && !retouchAvailable ? (
              <DropdownMenu.Label className="ex-menu-note">
                {retouchUnavailableMessage(retouchUnavailableReason)}
              </DropdownMenu.Label>
            ) : null}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function ActionSheet({
  artifact,
  onAction,
  includeContextual,
  retouchAvailable,
  retouchUnavailableReason,
}: Omit<OverflowActionsProps, "asSheet">) {
  const [open, setOpen] = useState(false);
  const actions = artifactUiActions(artifact.actions, includeContextual);
  if (!actions.length) return null;
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button className="ex-icon-button ex-artifact-more" type="button" aria-label={`更多：${artifact.display_name}`}>
          <MoreHorizontal aria-hidden="true" />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay ex-artifact-sheet-overlay" />
        <Dialog.Content
          className="ex-artifact-sheet"
          aria-describedby="ex-artifact-sheet-description"
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>产物操作</Dialog.Title>
              <Dialog.Description id="ex-artifact-sheet-description">
                {artifact.display_name}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <IconButton label="关闭产物操作"><X aria-hidden="true" /></IconButton>
            </Dialog.Close>
          </div>
          <div className="ex-artifact-sheet-actions">
            {actions.map((action) => {
              const Icon = ACTION_ICONS[action];
              const disabled = actionDisabled(action, retouchAvailable);
              return (
                <button
                  className="ex-artifact-sheet-action"
                  type="button"
                  key={action}
                  disabled={disabled}
                  onClick={() => {
                    onAction(artifact, action);
                    setOpen(false);
                  }}
                >
                  <Icon aria-hidden="true" />
                  <span>{ACTION_LABELS[action]}</span>
                  {disabled ? (
                    <small>{retouchUnavailableMessage(retouchUnavailableReason)}</small>
                  ) : null}
                </button>
              );
            })}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function OverflowActions(props: OverflowActionsProps) {
  return props.asSheet ? <ActionSheet {...props} /> : <MoreMenu {...props} />;
}

export type ImageArtifactGalleryViewSlot =
  | { kind: "artifact"; artifact: ArtifactProjection }
  | FailedImageBatchSlot;

export function ImageArtifactGallery({
  slots,
  previewUrls,
  onAction,
  onPreviewVisible,
}: {
  slots: ImageArtifactGalleryViewSlot[];
  previewUrls: Record<string, string>;
  onAction: ArtifactShelfProps["onAction"];
  onPreviewVisible: ArtifactShelfProps["onPreviewVisible"];
}) {
  const [activeIndex, setActiveIndex] = useState(0);
  const trackRef = useRef<HTMLDivElement>(null);
  const count = slots.length;
  useEffect(() => setActiveIndex((index) => Math.min(index, count - 1)), [count]);
  const activeArtifact = slots[activeIndex]?.kind === "artifact"
    ? slots[activeIndex].artifact
    : null;
  useEffect(() => {
    if (activeArtifact?.actions.includes("preview")) onPreviewVisible(activeArtifact);
  }, [activeArtifact?.artifact_id, activeArtifact?.revision_id, onPreviewVisible]);
  const goTo = (index: number) => {
    const target = Math.max(0, Math.min(count - 1, index));
    const track = trackRef.current;
    const slide = track?.children.item(target) as HTMLElement | null;
    if (!track || !slide) return;
    track.scrollTo({
      left: slide.offsetLeft - track.offsetLeft,
      behavior: "auto",
    });
    setActiveIndex(target);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    goTo(activeIndex + (event.key === "ArrowRight" ? 1 : -1));
  };
  const syncActiveSlide = () => {
    const track = trackRef.current;
    if (!track) return;
    const slides = [...track.children] as HTMLElement[];
    const nearest = slides.reduce((best, slide, index) => (
      Math.abs(slide.offsetLeft - track.offsetLeft - track.scrollLeft)
        < Math.abs(slides[best].offsetLeft - track.offsetLeft - track.scrollLeft)
        ? index
        : best
    ), 0);
    setActiveIndex(nearest);
  };
  return (
    <section className="ex-artifact-shelf" aria-label="任务产物">
      <p className="ex-section-label">产物</p>
      <div className="ex-image-gallery">
      <div
        ref={trackRef}
        className="ex-image-gallery-track"
        role="region"
        tabIndex={0}
        aria-label={`图片画廊，第 ${activeIndex + 1} 张，共 ${count} 张`}
        onKeyDown={onKeyDown}
        onScroll={syncActiveSlide}
      >
        {slots.map((slot, index) => {
          const artifact = slot.kind === "artifact" ? slot.artifact : null;
          const ready = artifact?.status === "ready";
          const failed = slot.kind === "failed"
            || artifact?.status === "failed"
            || artifact?.status === "deleted";
          const displayName = artifact?.display_name ?? `批次图片 ${index + 1}`;
          const previewUrl = artifact ? previewUrls[artifact.artifact_id] : null;
          const canPreview = Boolean(ready && artifact?.actions.includes("preview"));
          const media = artifact && ready && previewUrl ? (
            <img src={previewUrl} alt={artifact.display_name} />
          ) : failed ? (
            <span className="ex-image-gallery-failure" role="img" aria-label={artifact?.status === "deleted" ? "图片已不可用" : "图片未完成"}>
              <AlertCircle aria-hidden="true" />
              <strong>{artifact?.status === "deleted" ? "图片已不可用" : "图片未完成"}</strong>
              {slot.kind === "failed" ? (
                <small>{slot.retryable ? "可以稍后重试此张图片。" : "本次未生成有效图片。"}</small>
              ) : artifact?.quality_evidence.summary ? <small>{artifact.quality_evidence.summary}</small> : null}
            </span>
          ) : (
            <span className="ex-image-generation-canvas" role="img" aria-label={ready ? "正在载入图片预览" : "正在生成图片"}>
              <span className="ex-image-generation-glow" aria-hidden="true" />
              <ImageIcon aria-hidden="true" />
            </span>
          );
          return (
            <article
              className="ex-image-gallery-slide"
              data-preview-artifact-id={canPreview ? artifact?.artifact_id : undefined}
              data-artifact-status={slot.kind === "failed" ? "failed" : artifact?.status}
              data-image-batch-task-id={slot.kind === "failed" ? slot.taskId : undefined}
              role="group"
              aria-label={`${index + 1}/${count}：${displayName}`}
              key={slot.kind === "failed" ? slot.taskId : artifact?.artifact_id}
            >
              {canPreview && artifact ? (
                <button
                  className="ex-image-gallery-media"
                  type="button"
                  data-artifact-preview-trigger={artifact.artifact_id}
                  aria-label={`预览图片：${artifact.display_name}`}
                  onClick={() => onAction(artifact, "preview")}
                >{media}</button>
              ) : <div className="ex-image-gallery-media">{media}</div>}
              <div className="ex-image-gallery-caption">
                <span><strong>{displayName}</strong><small>{slot.kind === "failed" ? "生成失败" : artifact?.status === "pending" ? "正在生成" : ready ? "已完成" : artifact?.status === "failed" ? "生成失败" : "不可用"}</small></span>
                {artifact && ready ? (
                  <div role="group" aria-label={`图片操作：${artifact.display_name}`}>
                    {artifact.actions.includes("open") ? (
                      <IconButton label={`打开：${artifact.display_name}`} onClick={() => onAction(artifact, "open")}>
                        <ExternalLink aria-hidden="true" />
                      </IconButton>
                    ) : null}
                    {artifact.actions.includes("download") ? (
                      <IconButton label={`下载：${artifact.display_name}`} onClick={() => onAction(artifact, "download")}>
                        <Download aria-hidden="true" />
                      </IconButton>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
      <div className="ex-image-gallery-navigation">
        <IconButton label="上一张图片" disabled={activeIndex === 0} onClick={() => goTo(activeIndex - 1)}>
          <ChevronLeft aria-hidden="true" />
        </IconButton>
        <span aria-live="polite" aria-atomic="true">{activeIndex + 1} / {count}</span>
        <IconButton label="下一张图片" disabled={activeIndex === count - 1} onClick={() => goTo(activeIndex + 1)}>
          <ChevronRight aria-hidden="true" />
        </IconButton>
      </div>
      </div>
    </section>
  );
}

export function ArtifactShelf({
  artifacts,
  previewUrls,
  onAction,
  onPreviewVisible,
  retouchAvailable,
  retouchUnavailableReason,
}: ArtifactShelfProps) {
  const coarsePointer = useMediaMatch("(pointer: coarse)");
  const compactViewport = useMediaMatch("(max-width: 680px)");
  const shelfRef = useRef<HTMLElement>(null);
  const previewIdentity = artifacts
    .filter((artifact) => (
      artifact.actions.includes("preview")
      && (artifact.family === "image" || artifact.family === "video")
    ))
    .map((artifact) => `${artifact.artifact_id}:${artifact.revision_id}`)
    .join("|");
  useEffect(() => {
    const shelf = shelfRef.current;
    if (!shelf || !previewIdentity) return;
    const byId = new Map(artifacts.map((artifact) => [artifact.artifact_id, artifact]));
    const candidates = [...shelf.querySelectorAll<HTMLElement>(
      "[data-preview-artifact-id]",
    )];
    const reveal = (element: HTMLElement) => {
      const artifactId = element.dataset.previewArtifactId;
      const artifact = artifactId ? byId.get(artifactId) : undefined;
      if (artifact) onPreviewVisible(artifact);
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
  }, [onPreviewVisible, previewIdentity]);
  if (!artifacts.length) return null;
  return (
    <section ref={shelfRef} className="ex-artifact-shelf" aria-label="任务产物">
      <p className="ex-section-label">产物</p>
      <div className="ex-artifact-list">
        {artifacts.map((artifact) => {
          const media = artifact.family === "image" || artifact.family === "video";
          const previewUrl = previewUrls[artifact.artifact_id];
          const canPreview = artifact.actions.includes("preview");
          const primaryContent = (
            <>
              {media && previewUrl ? (
                <img src={previewUrl} alt="" className="ex-artifact-thumbnail" />
              ) : (
                <span className="ex-artifact-glyph" aria-hidden="true">
                  {media ? <ImageIcon /> : <FileText />}
                </span>
              )}
              <span className="ex-artifact-copy">
                <strong>{artifact.display_name}</strong>
                <span>{artifactFamilyLabel(artifact.family)} · {formatFileSize(artifact.size_bytes)}</span>
              </span>
            </>
          );
          return (
            <article
              className={`ex-artifact ${media ? "is-media" : "is-row"}`}
              data-preview-artifact-id={media && canPreview ? artifact.artifact_id : undefined}
              key={artifact.artifact_id}
            >
              {canPreview ? (
                <button
                  className="ex-artifact-primary"
                  type="button"
                  data-artifact-preview-trigger={artifact.artifact_id}
                  onClick={() => onAction?.(artifact, "preview")}
                >
                  {primaryContent}
                </button>
              ) : (
                <div className="ex-artifact-primary">{primaryContent}</div>
              )}
              <div
                className="ex-artifact-actions"
                role="group"
                aria-label={`产物操作：${artifact.display_name}`}
              >
                {artifact.actions.includes("feedback") ? (
                  <>
                    <IconButton
                      className={artifact.feedback?.signal === "thumbs_up" ? "is-selected" : undefined}
                      label="有帮助"
                      aria-pressed={artifact.feedback?.signal === "thumbs_up"}
                      onClick={() => onAction(artifact, "thumbs_up")}
                    >
                      <ThumbsUp aria-hidden="true" />
                    </IconButton>
                    <IconButton
                      className={artifact.feedback?.signal === "thumbs_down" ? "is-selected" : undefined}
                      label="需要改进"
                      aria-pressed={artifact.feedback?.signal === "thumbs_down"}
                      onClick={() => onAction(artifact, "thumbs_down")}
                    >
                      <ThumbsDown aria-hidden="true" />
                    </IconButton>
                  </>
                ) : null}
                {artifact.actions.includes("precise_retouch") ? (
                  <IconButton
                    label={retouchAvailable
                      ? "精准修图"
                      : retouchUnavailableMessage(retouchUnavailableReason)}
                    disabled={!retouchAvailable}
                    onClick={() => onAction(artifact, "precise_retouch")}
                  >
                    <WandSparkles aria-hidden="true" />
                  </IconButton>
                ) : null}
                <OverflowActions
                  artifact={artifact}
                  onAction={onAction}
                  includeContextual={coarsePointer || compactViewport}
                  asSheet={coarsePointer}
                  retouchAvailable={retouchAvailable}
                  retouchUnavailableReason={retouchUnavailableReason}
                />
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
