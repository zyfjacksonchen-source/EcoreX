import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection, ItemProjection } from "../api/contracts.ts";
import type { FailedImageBatchSlot } from "./imageBatchFacts.ts";
import {
  groupTimelineImageArtifacts,
  type TimelinePresentationBlock,
} from "./timelineImageGallery.ts";
import type { TimelineBlock } from "./timelineTurns.ts";

const timestamp = "2026-08-08T00:00:00.000Z";

function artifact(id: string, family: ArtifactProjection["family"] = "image"): ArtifactProjection {
  return {
    artifact_id: id,
    revision_id: `revision-${id}`,
    family,
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: `${id}.png`,
    mime_type: family === "image" ? "image/png" : "application/pdf",
    size_bytes: 1,
    sha256: "a".repeat(64),
    created_at: timestamp,
    renditions: [],
    actions: ["preview", "download"],
    feedback: null,
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    quality_evidence: { status: "not_checked", checks: [], score: null, summary: null },
  };
}

function block(
  id: string,
  projection: ArtifactProjection,
  extra: Record<string, unknown> = {},
): TimelineBlock {
  const item: ItemProjection = {
    item_id: `item-${id}`,
    thread_id: "thread-1",
    turn_id: "turn-1",
    kind: "artifact",
    status: "completed",
    content: { artifact: projection, ...extra },
    inherited: false,
    created_seq: 1,
    created_at: timestamp,
    updated_at: timestamp,
  };
  return { kind: "item", key: item.item_id, item };
}

function imageBatch(batchId: string, index: number, count: number) {
  return {
    image_batch: {
      schema_version: 1,
      batch_id: batchId,
      parent_execution_id: `execution-${batchId}`,
      index,
      count,
      task_id: `task-${batchId}-${index}`,
    },
  };
}

function presentationKinds(blocks: TimelinePresentationBlock[]): string[] {
  return blocks.map((candidate) => candidate.kind);
}

function failedSlot(batchId: string, index: number, count: number): FailedImageBatchSlot {
  return {
    kind: "failed",
    threadId: "thread-1",
    turnId: "turn-1",
    createdSeq: 5,
    batchId,
    parentExecutionId: `execution-${batchId}`,
    index,
    count,
    taskId: `task-${batchId}-${index}`,
    errorCode: "managed_image_unavailable",
    retryable: true,
  };
}

test("groups one complete backend batch and orders it by the backend index", () => {
  const grouped = groupTimelineImageArtifacts([
    block("three", artifact("three"), imageBatch("batch-one", 2, 3)),
    block("one", artifact("one"), imageBatch("batch-one", 0, 3)),
    block("two", artifact("two"), imageBatch("batch-one", 1, 3)),
    block("document", artifact("document", "pdf")),
  ]);

  assert.deepEqual(presentationKinds(grouped), ["image_gallery", "item"]);
  assert.deepEqual(
    grouped[0]?.kind === "image_gallery"
      ? grouped[0].slots.map((candidate) => (
          candidate.kind === "artifact" ? candidate.block.item.item_id : candidate.taskId
        ))
      : [],
    ["item-one", "item-two", "item-three"],
  );
});

test("merges a durable failed event with successful Artifact facts by index", () => {
  const grouped = groupTimelineImageArtifacts([
    block("three", artifact("three"), imageBatch("partial", 2, 3)),
    block("one", artifact("one"), imageBatch("partial", 0, 3)),
  ], [failedSlot("partial", 1, 3)]);

  assert.deepEqual(presentationKinds(grouped), ["image_gallery"]);
  assert.deepEqual(
    grouped[0]?.kind === "image_gallery"
      ? grouped[0].slots.map((slot) => slot.kind)
      : [],
    ["artifact", "failed", "artifact"],
  );
});

test("does not merge two independent consecutive image calls or legacy facts", () => {
  const grouped = groupTimelineImageArtifacts([
    block("one", artifact("one")),
    block("batch-one", artifact("batch-one"), imageBatch("batch-one", 0, 2)),
    block("batch-two", artifact("batch-two"), imageBatch("batch-two", 0, 2)),
    block("two", artifact("two")),
  ]);

  assert.deepEqual(presentationKinds(grouped), ["item", "item", "item", "item"]);
});

test("keeps incomplete, duplicate-index and precise retouch facts on native paths", () => {
  const grouped = groupTimelineImageArtifacts([
    block("incomplete", artifact("incomplete"), imageBatch("incomplete", 0, 2)),
    block("duplicate-a", artifact("duplicate-a"), imageBatch("duplicate", 0, 2)),
    block("duplicate-b", artifact("duplicate-b"), imageBatch("duplicate", 0, 2)),
    block("retouch", artifact("retouch"), {
      retouch_job_id: "job-1",
      ...imageBatch("retouch", 0, 2),
    }),
  ]);

  assert.deepEqual(presentationKinds(grouped), ["item", "item", "item", "item"]);
});
