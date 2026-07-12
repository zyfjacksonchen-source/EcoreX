import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
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
import { useEffect, useRef, useState } from "react";

import type { ArtifactProjection } from "../api/contracts.ts";
import {
  artifactUiActions,
  type ArtifactUiAction,
} from "../state/artifactActions.ts";
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
