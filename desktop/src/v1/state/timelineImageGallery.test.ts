import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection, ItemProjection } from "../api/contracts.ts";
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

function itemBlock(item: ItemProjection): TimelineBlock {
  return { kind: "item", key: item.item_id, item };
}

function artifactBlock(
  id: string,
  family: ArtifactProjection["family"] = "image",
  extra: Record<string, unknown> = {},
): TimelineBlock {
  return itemBlock({
    item_id: `item-${id}`,
    thread_id: "thread-1",
    turn_id: "turn-1",
    kind: "artifact",
    status: "completed",
    content: { artifact: artifact(id, family), ...extra },
    inherited: false,
    created_seq: 1,
    created_at: timestamp,
    updated_at: timestamp,
  });
}

function imageToolCall(id: string, status: ItemProjection["status"]): TimelineBlock {
  return itemBlock({
    item_id: `tool-${id}`,
    thread_id: "thread-1",
    turn_id: "turn-1",
    kind: "tool_call",
    status,
    content: { tool_id: "imagegen", tool_call_id: id },
    inherited: false,
    created_seq: 1,
    created_at: timestamp,
    updated_at: timestamp,
  });
}

function presentationKinds(blocks: TimelinePresentationBlock[]): string[] {
  return blocks.map((candidate) => candidate.kind);
}

test("groups independent imagegen artifacts from one turn despite intervening tool calls", () => {
  const grouped = groupTimelineImageArtifacts([
    imageToolCall("one", "completed"),
    artifactBlock("one"),
    imageToolCall("two", "completed"),
    artifactBlock("two"),
    imageToolCall("three", "completed"),
    artifactBlock("three"),
  ]);

  assert.deepEqual(presentationKinds(grouped), ["item", "item", "item", "image_gallery"]);
  const gallery = grouped.find((candidate) => candidate.kind === "image_gallery");
  assert.deepEqual(
    gallery?.kind === "image_gallery"
      ? gallery.slots.map((slot) => slot.block.item.item_id)
      : [],
    ["item-one", "item-two", "item-three"],
  );
});

test("partial tool failure displays only the two successful artifact facts", () => {
  const grouped = groupTimelineImageArtifacts([
    imageToolCall("one", "completed"),
    artifactBlock("one"),
    imageToolCall("failed", "failed"),
    imageToolCall("two", "completed"),
    artifactBlock("two"),
  ]);

  assert.deepEqual(presentationKinds(grouped), ["item", "item", "item", "image_gallery"]);
  const gallery = grouped.find((candidate) => candidate.kind === "image_gallery");
  assert.deepEqual(
    gallery?.kind === "image_gallery"
      ? gallery.slots.map((slot) => slot.block.item.item_id)
      : [],
    ["item-one", "item-two"],
  );
});

test("a failed imagegen call without an artifact does not create a gallery", () => {
  const failed = imageToolCall("failed", "failed");
  assert.deepEqual(groupTimelineImageArtifacts([failed]), [failed]);
});

test("single images and retouch artifacts keep their native cards", () => {
  const single = artifactBlock("single");
  const retouch = artifactBlock("retouch", "image", { retouch_job_id: "job-1" });
  const document = artifactBlock("document", "pdf");

  assert.deepEqual(groupTimelineImageArtifacts([single, retouch, document]), [
    single,
    retouch,
    document,
  ]);
});
