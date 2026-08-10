import type { ConversationUsageProjection } from "./contracts.ts";
import { RuntimeContractError } from "./runtimeContract.ts";

const CONTRACT = "ConversationUsageProjection";

function reject(path: string, expectation: string): never {
  throw new RuntimeContractError(CONTRACT, path, expectation);
}

function record(value: unknown, path: string): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject(path, "an object");
  }
}

function fields(value: Record<string, unknown>, expected: readonly string[], path: string): void {
  for (const key of expected) {
    if (!Object.hasOwn(value, key)) reject(path === "root" ? key : `${path}.${key}`, "a declared field");
  }
  for (const key of Object.keys(value)) {
    if (!expected.includes(key)) reject(path === "root" ? key : `${path}.${key}`, "no undeclared fields");
  }
}

function text(value: unknown, path: string, nullable = false): void {
  if ((nullable && value === null) || (typeof value === "string" && value.trim())) return;
  reject(path, nullable ? "a non-empty string or null" : "a non-empty string");
}

function integer(value: unknown, path: string, minimum = 0): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    reject(path, `an integer >= ${minimum}`);
  }
}

function usage(value: unknown, path: string): void {
  record(value, path);
  fields(value, ["input_tokens", "output_tokens", "total_tokens"], path);
  integer(value.input_tokens, `${path}.input_tokens`);
  integer(value.output_tokens, `${path}.output_tokens`);
  integer(value.total_tokens, `${path}.total_tokens`);
  if (value.total_tokens < value.input_tokens + value.output_tokens) {
    reject(`${path}.total_tokens`, "at least input plus output tokens");
  }
}

export function validateConversationUsageProjection(value: unknown): ConversationUsageProjection {
  record(value, "root");
  fields(value, [
    "thread_id", "timezone", "scope", "source", "complete_across_devices",
    "today", "week", "context", "task_activity", "data_quality", "calculated_at",
  ], "root");
  text(value.thread_id, "thread_id");
  text(value.timezone, "timezone");
  if (!(["account", "local_device"] as unknown[]).includes(value.scope)) reject("scope", "account or local_device");
  if (!(["managed_gateway", "local_event_store"] as unknown[]).includes(value.source)) reject("source", "managed_gateway or local_event_store");
  if (typeof value.complete_across_devices !== "boolean") reject("complete_across_devices", "a boolean");
  usage(value.today, "today");
  usage(value.week, "week");

  record(value.task_activity, "task_activity");
  const activity = value.task_activity;
  fields(activity, ["completed_today", "partial_today", "waiting", "terminal_today", "days"], "task_activity");
  for (const name of ["completed_today", "partial_today", "waiting", "terminal_today"] as const) {
    integer(activity[name], `task_activity.${name}`);
  }
  if (!Array.isArray(activity.days) || activity.days.length !== 7) {
    reject("task_activity.days", "seven daily task activity records");
  }
  activity.days.forEach((day, index) => {
    const path = `task_activity.days[${index}]`;
    record(day, path);
    fields(day, ["date", "completed", "partial", "terminal"], path);
    text(day.date, `${path}.date`);
    if (!/^\d{4}-\d{2}-\d{2}$/u.test(day.date as string)) reject(`${path}.date`, "YYYY-MM-DD");
    integer(day.completed, `${path}.completed`);
    integer(day.partial, `${path}.partial`);
    integer(day.terminal, `${path}.terminal`);
    if (day.completed > day.terminal) reject(`${path}.completed`, "at most terminal");
    if (day.partial > day.terminal) reject(`${path}.partial`, "at most terminal");
  });

  record(value.context, "context");
  const context = value.context;
  fields(context, [
    "used_tokens", "window_tokens", "model_id", "model_display_name",
    "model_catalog_snapshot_id", "measured_at",
  ], "context");
  if (context.used_tokens !== null) integer(context.used_tokens, "context.used_tokens");
  if (context.window_tokens !== null) integer(context.window_tokens, "context.window_tokens", 1_000);
  for (const name of ["model_id", "model_display_name", "model_catalog_snapshot_id", "measured_at"] as const) {
    text(context[name], `context.${name}`, true);
  }
  text(value.calculated_at, "calculated_at");
  record(value.data_quality, "data_quality");
  fields(value.data_quality, [
    "audit_continuity", "recovery_count", "removed_audit_rows",
    "removed_trace_rows", "last_recovery_at",
  ], "data_quality");
  if (!( ["complete", "recovered_with_gap", "uncertain"] as unknown[]).includes(value.data_quality.audit_continuity)) {
    reject("data_quality.audit_continuity", "complete, recovered_with_gap, or uncertain");
  }
  for (const name of ["recovery_count", "removed_audit_rows", "removed_trace_rows"] as const) {
    integer(value.data_quality[name], `data_quality.${name}`);
  }
  text(value.data_quality.last_recovery_at, "data_quality.last_recovery_at", true);
  return value as unknown as ConversationUsageProjection;
}
