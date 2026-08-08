import { tryValidateArtifactProjection } from "../api/runtimeContract.ts";
import type { FailedImageBatchSlot } from "./imageBatchFacts.ts";
import type { TimelineBlock } from "./timelineTurns.ts";

export type TimelineImageGallerySlot =
  | { kind: "artifact"; block: Extract<TimelineBlock, { kind: "item" }> }
  | FailedImageBatchSlot;

export interface TimelineImageGallery {
  kind: "image_gallery";
  key: string;
  slots: TimelineImageGallerySlot[];
}

export type TimelinePresentationBlock = TimelineBlock | TimelineImageGallery;

interface ImageBatchFact {
  slot: TimelineImageGallerySlot;
  batchId: string;
  parentExecutionId: string;
  index: number;
  count: number;
}

function identity(value: unknown): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 256
    && value.trim() === value;
}

function imageBatchFact(
  value: TimelineBlock | FailedImageBatchSlot,
): ImageBatchFact | null {
  if (value.kind === "failed") return {
    slot: value,
    batchId: value.batchId,
    parentExecutionId: value.parentExecutionId,
    index: value.index,
    count: value.count,
  };
  const block = value;
  if (
    block.kind !== "item"
    || block.item.kind !== "artifact"
    || typeof block.item.content.retouch_job_id === "string"
  ) return null;
  const artifact = tryValidateArtifactProjection(
    block.item.content.artifact ?? block.item.content,
  );
  if (artifact?.family !== "image") return null;
  const batch = block.item.content.image_batch;
  if (!batch || typeof batch !== "object" || Array.isArray(batch)) return null;
  const fact = batch as Record<string, unknown>;
  if (
    fact.schema_version !== 1
    || !identity(fact.batch_id)
    || !identity(fact.parent_execution_id)
    || !identity(fact.task_id)
    || !Number.isInteger(fact.index)
    || !Number.isInteger(fact.count)
  ) return null;
  const index = fact.index as number;
  const count = fact.count as number;
  if (count < 2 || count > 8 || index < 0 || index >= count) return null;
  return {
    slot: { kind: "artifact", block },
    batchId: fact.batch_id,
    parentExecutionId: fact.parent_execution_id,
    index,
    count,
  };
}

export function groupTimelineImageArtifacts(
  blocks: readonly TimelineBlock[],
  failures: readonly FailedImageBatchSlot[] = [],
): TimelinePresentationBlock[] {
  const result: TimelinePresentationBlock[] = [];
  let images: ImageBatchFact[] = [];
  const flush = () => {
    const complete = images.length > 1
      && images.length === images[0].count
      && images.every((fact) => (
        fact.batchId === images[0].batchId
        && fact.parentExecutionId === images[0].parentExecutionId
        && fact.count === images[0].count
      ))
      && new Set(images.map((fact) => fact.index)).size === images[0].count;
    if (complete) {
      const ordered = [...images].sort((left, right) => left.index - right.index);
      result.push({
        kind: "image_gallery",
        key: `image-gallery:${images[0].batchId}:${images[0].parentExecutionId}`,
        slots: ordered.map((fact) => fact.slot),
      });
    } else {
      result.push(...images.flatMap((fact) => (
        fact.slot.kind === "artifact" ? [fact.slot.block] : []
      )));
    }
    images = [];
  };
  const orderedFacts = [
    ...blocks.map((block, fallback) => ({
      value: block,
      seq: block.kind === "item" ? block.item.created_seq : block.interaction.created_seq,
      fallback,
    })),
    ...failures.map((failure, index) => ({
      value: failure,
      seq: failure.createdSeq,
      fallback: blocks.length + index,
    })),
  ].sort((left, right) => {
    if (left.seq != null && right.seq != null) return left.seq - right.seq;
    if (left.seq != null) return -1;
    if (right.seq != null) return 1;
    return left.fallback - right.fallback;
  });
  for (const { value } of orderedFacts) {
    const fact = imageBatchFact(value);
    if (!fact) {
      flush();
      if (value.kind !== "failed") result.push(value);
      continue;
    }
    const previous = images.at(-1);
    if (
      previous
      && (
        previous.batchId !== fact.batchId
        || previous.parentExecutionId !== fact.parentExecutionId
        || previous.count !== fact.count
      )
    ) flush();
    images.push(fact);
  }
  flush();
  return result;
}
