import assert from "node:assert/strict";
import test from "node:test";

import type { ItemProjection, TurnProjection } from "../api/contracts.ts";
import { buildTimelineTurns } from "./timelineTurns.ts";

const timestamp = "2026-08-06T00:00:00.000Z";
const turn: TurnProjection = {
  turn_id: "turn-1",
  thread_id: "thread-1",
  status: "completed",
  input: "执行",
  agent_model_id: "agent",
  image_model_id: null,
  client_message_id: "message-1",
  metadata: {},
  inherited: false,
  terminal_reason: null,
  timing: { started_at: timestamp, finished_at: timestamp, duration_ms: 0 },
  created_at: timestamp,
  updated_at: timestamp,
};

function item(
  itemId: string,
  createdSeq: number,
  kind: ItemProjection["kind"],
  content: Record<string, unknown>,
): ItemProjection {
  return {
    item_id: itemId,
    thread_id: "thread-1",
    turn_id: "turn-1",
    kind,
    status: "completed",
    content,
    inherited: false,
    created_seq: createdSeq,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

test("timeline turns use durable sequence across reasoning, tools and messages", () => {
  const entry = buildTimelineTurns([turn], [
    item("answer", 9, "message", { role: "assistant", text: "最终答案" }),
    item("reasoning", 6, "reasoning", { text: "分析", presentation: "visible" }),
    item("user", 5, "message", { role: "user", text: "问题" }),
    item("tool", 7, "tool_call", {}),
  ], [])[0];

  assert.deepEqual(entry?.blocks.map((block) => block.key), [
    "user",
    "reasoning",
    "tool",
    "answer",
  ]);
});

test("replacement removes archived reasoning atoms from the live timeline", () => {
  const entry = buildTimelineTurns([turn], [
    item("reasoning-old", 1, "reasoning", { text: "旧过程", presentation: "archived" }),
    item("reasoning-current", 2, "reasoning", { text: "当前过程", presentation: "visible" }),
  ], [])[0];

  assert.deepEqual(entry?.blocks.map((block) => block.key), ["reasoning-current"]);
});
