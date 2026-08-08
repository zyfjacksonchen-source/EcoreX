import type { EventEnvelope } from "../api/contracts.ts";

export interface FailedImageBatchSlot {
  kind: "failed";
  threadId: string;
  turnId: string;
  createdSeq: number;
  batchId: string;
  parentExecutionId: string;
  index: number;
  count: number;
  taskId: string;
  errorCode: string;
  retryable: boolean;
}

interface ImageBatchSettlement {
  threadId: string;
  turnId: string;
  batchId: string;
  parentExecutionId: string;
  requestedCount: number;
  completedCount: number;
  failedCount: number;
  status: "completed" | "partial_failed" | "failed";
}

export interface ImageBatchFactState {
  failures: Record<string, FailedImageBatchSlot>;
  settlements: Record<string, ImageBatchSettlement>;
}

export interface ImageBatchEventPage {
  events: EventEnvelope[];
  after_seq: number;
  watermark: number;
  has_more: boolean;
}

export const emptyImageBatchFacts = (): ImageBatchFactState => ({
  failures: {},
  settlements: {},
});

function identity(value: unknown): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 256
    && value.trim() === value;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function batchKey(parentExecutionId: string, batchId: string): string {
  return JSON.stringify([parentExecutionId, batchId]);
}

function failureFact(event: EventEnvelope): FailedImageBatchSlot | null {
  if (
    event.event_type !== "artifact.image.batch_task_failed"
    || !event.turn_id
    || event.payload.schema_version !== 1
  ) return null;
  const batch = record(event.payload.image_batch);
  const error = record(event.payload.error);
  if (
    !batch
    || !error
    || batch.schema_version !== 1
    || !identity(batch.batch_id)
    || !identity(batch.parent_execution_id)
    || !identity(batch.task_id)
    || !Number.isInteger(batch.index)
    || !Number.isInteger(batch.count)
    || !identity(error.code)
    || typeof error.retryable !== "boolean"
  ) return null;
  const index = batch.index as number;
  const count = batch.count as number;
  if (count < 2 || count > 8 || index < 0 || index >= count) return null;
  return {
    kind: "failed",
    threadId: event.thread_id,
    turnId: event.turn_id,
    createdSeq: event.seq,
    batchId: batch.batch_id,
    parentExecutionId: batch.parent_execution_id,
    index,
    count,
    taskId: batch.task_id,
    errorCode: error.code,
    retryable: error.retryable,
  };
}

function settlementFact(event: EventEnvelope): ImageBatchSettlement | null {
  if (
    event.event_type !== "artifact.image.batch_settled"
    || !event.turn_id
    || event.payload.schema_version !== 1
    || !identity(event.payload.batch_id)
    || !identity(event.payload.parent_execution_id)
    || !Number.isInteger(event.payload.requested_count)
    || !Number.isInteger(event.payload.completed_count)
    || !Number.isInteger(event.payload.failed_count)
    || !["completed", "partial_failed", "failed"].includes(String(event.payload.status))
  ) return null;
  const requestedCount = event.payload.requested_count as number;
  const completedCount = event.payload.completed_count as number;
  const failedCount = event.payload.failed_count as number;
  if (
    requestedCount < 2
    || requestedCount > 8
    || completedCount < 0
    || failedCount < 0
    || completedCount + failedCount !== requestedCount
  ) return null;
  return {
    threadId: event.thread_id,
    turnId: event.turn_id,
    batchId: event.payload.batch_id,
    parentExecutionId: event.payload.parent_execution_id,
    requestedCount,
    completedCount,
    failedCount,
    status: event.payload.status as ImageBatchSettlement["status"],
  };
}

export function reduceImageBatchFacts(
  state: ImageBatchFactState,
  events: readonly EventEnvelope[],
): ImageBatchFactState {
  let next = state;
  for (const event of events) {
    const failure = failureFact(event);
    if (failure) {
      if (next === state) next = {
        failures: { ...state.failures },
        settlements: { ...state.settlements },
      };
      next.failures[JSON.stringify([
        failure.parentExecutionId,
        failure.batchId,
        failure.taskId,
      ])] = failure;
      continue;
    }
    const settlement = settlementFact(event);
    if (!settlement) continue;
    if (next === state) next = {
      failures: { ...state.failures },
      settlements: { ...state.settlements },
    };
    next.settlements[batchKey(
      settlement.parentExecutionId,
      settlement.batchId,
    )] = settlement;
  }
  return next;
}

export function mergeImageBatchFacts(
  history: ImageBatchFactState,
  live: ImageBatchFactState,
): ImageBatchFactState {
  return {
    failures: { ...history.failures, ...live.failures },
    settlements: { ...history.settlements, ...live.settlements },
  };
}

export function selectFailedImageBatchSlots(
  state: ImageBatchFactState,
): FailedImageBatchSlot[] {
  const failures = Object.values(state.failures);
  return Object.values(state.settlements).flatMap((settlement) => {
    if (settlement.failedCount === 0) return [];
    const batchFailures = failures.filter((failure) => (
      failure.threadId === settlement.threadId
      && failure.turnId === settlement.turnId
      && failure.batchId === settlement.batchId
      && failure.parentExecutionId === settlement.parentExecutionId
      && failure.count === settlement.requestedCount
    ));
    if (batchFailures.length !== settlement.failedCount) return [];
    return batchFailures;
  }).sort((left, right) => left.createdSeq - right.createdSeq);
}

export async function loadImageBatchFactHistory(
  threadId: string,
  throughSeq: number,
  eventPage: (afterSeq: number, signal?: AbortSignal) => Promise<ImageBatchEventPage>,
  signal?: AbortSignal,
): Promise<ImageBatchFactState> {
  let state = emptyImageBatchFacts();
  let afterSeq = 0;
  while (!signal?.aborted && afterSeq < throughSeq) {
    const page = await eventPage(afterSeq, signal);
    state = reduceImageBatchFacts(
      state,
      page.events.filter((event) => event.thread_id === threadId && event.seq <= throughSeq),
    );
    if (!page.has_more || page.after_seq >= throughSeq) break;
    if (page.after_seq <= afterSeq) throw new Error("image_batch_event_cursor_stalled");
    afterSeq = page.after_seq;
  }
  return state;
}
