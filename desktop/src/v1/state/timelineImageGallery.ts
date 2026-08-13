import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";
import type { TimelineBlock } from "./timelineTurns.ts";

type ArtifactBlock = Extract<TimelineBlock, { kind: "item" }>;

export interface TimelineImageGallery {
  kind: "image_gallery";
  key: string;
  slots: Array<{ kind: "artifact"; block: ArtifactBlock }>;
}

export type TimelinePresentationBlock = TimelineBlock | TimelineImageGallery;

function readyImage(block: TimelineBlock): block is ArtifactBlock {
  if (
    block.kind !== "item"
    || block.item.kind !== "artifact"
    || typeof block.item.content.retouch_job_id === "string"
  ) return false;
  const artifact = tryValidateArtifactProjection(
    block.item.content.artifact ?? block.item.content,
  );
  return artifact?.family === "image" && artifact.status === "ready";
}

export function groupTimelineImageArtifacts(
  blocks: readonly TimelineBlock[],
): TimelinePresentationBlock[] {
  const images = blocks.filter(readyImage);
  if (images.length < 2) return [...blocks];

  const lastImageKey = images.at(-1)?.key;
  const gallery: TimelineImageGallery = {
    kind: "image_gallery",
    key: `image-gallery:${images.map((block) => block.item.item_id).join(":")}`,
    slots: images.map((block) => ({ kind: "artifact", block })),
  };
  const projected: TimelinePresentationBlock[] = [];
  for (const block of blocks) {
    if (!readyImage(block)) projected.push(block);
    else if (block.key === lastImageKey) projected.push(gallery);
  }
  return projected;
}
