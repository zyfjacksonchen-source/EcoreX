import assert from "node:assert/strict";
import test from "node:test";

import type { EventEnvelope } from "../api/contracts.ts";
import {
  emptyImageBatchFacts,
  loadImageBatchFactHistory,
  reduceImageBatchFacts,
  selectFailedImageBatchSlots,
} from "./imageBatchFacts.ts";

function event(seq: number, type: string, payload: Record<string, unknown>): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `event-${seq}`,
    seq,
    thread_id: "thread-1",
    turn_id: "turn-1",
    item_id: null,
    job_id: "job-1",
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: null,
    permission_snapshot_id: null,
    extension_snapshot_id: null,
    event_type: type,
    created_at: "2026-08-08T00:00:00.000Z",
    payload,
  };
}

const failed = event(5, "artifact.image.batch_task_failed", {
  schema_version: 1,
  image_batch: {
    schema_version: 1,
    batch_id: "batch-1",
    parent_execution_id: "execution-1",
    index: 1,
    count: 3,
    task_id: "task-1",
  },
  error: { code: "managed_image_unavailable", retryable: true },
});
const settled = event(7, "artifact.image.batch_settled", {
  schema_version: 1,
  batch_id: "batch-1",
  parent_execution_id: "execution-1",
  requested_count: 3,
  completed_count: 2,
  failed_count: 1,
  status: "partial_failed",
});

test("live facts expose a failed slot only after the authoritative settlement", () => {
  const running = reduceImageBatchFacts(emptyImageBatchFacts(), [failed]);
  assert.deepEqual(selectFailedImageBatchSlots(running), []);
  const terminal = reduceImageBatchFacts(running, [settled]);
  assert.deepEqual(selectFailedImageBatchSlots(terminal), [{
    kind: "failed",
    threadId: "thread-1",
    turnId: "turn-1",
    createdSeq: 5,
    batchId: "batch-1",
    parentExecutionId: "execution-1",
    index: 1,
    count: 3,
    taskId: "task-1",
    errorCode: "managed_image_unavailable",
    retryable: true,
  }]);
});

test("refresh rebuilds partial failures from durable paginated events", async () => {
  const pages = [
    { events: [event(1, "turn.accepted", {}), failed], after_seq: 5, watermark: 7, has_more: true },
    { events: [settled], after_seq: 7, watermark: 7, has_more: false },
  ];
  const requested: number[] = [];
  const history = await loadImageBatchFactHistory(
    "thread-1",
    7,
    async (afterSeq) => {
      requested.push(afterSeq);
      return pages[requested.length - 1];
    },
  );

  assert.deepEqual(requested, [0, 5]);
  assert.equal(selectFailedImageBatchSlots(history).length, 1);
});
