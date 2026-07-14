import assert from "node:assert/strict";
import test from "node:test";

import type {
  BootstrapResponse,
  EventEnvelope,
  ThreadProjectionResponse,
  TurnStatus,
} from "../api/contracts.ts";
import {
  initialRuntimeViewState,
  runtimeReducer,
  selectItems,
  selectIsThinking,
  selectPendingInteractions,
  selectVisibleReasoning,
} from "./runtimeReducer.ts";

const now = "2026-07-10T08:00:00.000Z";

function bootstrap(revision: number, fullAccess: boolean, serverTime: string): BootstrapResponse {
  return {
    api_version: "v1",
    event_schema_version: 1,
    storage_schema_version: 1,
    login: {
      authenticated: true,
      account_id: "local",
      display_name: "User",
      organization_id: "org-test",
      roles: ["member"],
      session_revision: 1,
    },
    policy_lease: {
      lease_id: "lease_1",
      issued_at: now,
      expires_at: "2026-07-13T08:00:00.000Z",
      duration_hours: 72,
    },
    models: {
      snapshot_id: "models_1",
      chat: [],
      image: [],
      vision: [],
      audio: [],
      embedding: [],
    },
    model_service: { state: "ready", reason: null },
    login_service: { state: "ready", reason: null },
    share_service: { state: "unavailable", reason: "share_service_not_configured" },
    retouch_service: { state: "unavailable", reason: "managed_image_edit_not_configured" },
    quota: { remaining: null, unit: "managed_requests", resets_at: null, limits: {} },
    permissions: {
      snapshot_id: `perm_${revision}`,
      profile: fullAccess ? "full_access" : "default",
      revision,
      updated_at: serverTime,
      sandbox: fullAccess ? "danger-full-access" : "workspace-write",
      approval: fullAccess ? "never" : "on-request",
      full_access: fullAccess,
      admin_hard_denies: [],
    },
    connectors: [],
    extensions: {
      snapshot_id: "extensions_1",
      contract_version: "1.0",
      items: [],
    },
    update: {
      current_version: "1.0.0",
      state: "idle",
      target_version: null,
      release_id: null,
      build_digest: null,
      transaction_id: null,
      can_activate: false,
      requires_refresh: false,
      error_code: null,
    },
    csrf_token: "c".repeat(43),
    server_time: serverTime,
  };
}

function projection(status: TurnStatus = "queued"): ThreadProjectionResponse {
  return {
    thread: {
      thread_id: "thr_1",
      status: "active",
      title: null,
      metadata: {},
      forked_from_thread_id: null,
      forked_from_turn_id: null,
      forked_from_seq: null,
      created_at: now,
      updated_at: now,
    },
    turns: [
      {
        turn_id: "trn_1",
        thread_id: "thr_1",
        status,
        input: "hello",
        agent_model_id: "ecorex-chat",
        image_model_id: "gpt-image-2",
        client_message_id: "msg_1",
        metadata: {},
        terminal_reason: null,
        inherited: false,
        created_at: now,
        updated_at: now,
      },
    ],
    items: [],
    jobs: [],
    interactions: [],
    watermark: 4,
  };
}

function event(
  seq: number,
  eventType: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<EventEnvelope> = {},
): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `evt_${seq}`,
    seq,
    thread_id: "thr_1",
    turn_id: "trn_1",
    item_id: null,
    job_id: null,
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: null,
    trace_id: null,
    config_snapshot_id: null,
    capability_snapshot_id: null,
    permission_snapshot_id: null,
    extension_snapshot_id: null,
    event_type: eventType,
    created_at: now,
    payload,
    ...overrides,
  };
}

test("opaque Item ids and tied clocks never reorder the durable conversation", () => {
  const tied = projection();
  tied.items = [
    {
      item_id: "itm_z_created_first",
      thread_id: "thr_1",
      turn_id: "trn_1",
      kind: "message",
      status: "completed",
      content: { role: "user", text: "先创建" },
      inherited: false,
      created_at: now,
      updated_at: now,
    },
    {
      item_id: "itm_a_created_second",
      thread_id: "thr_1",
      turn_id: "trn_1",
      kind: "message",
      status: "completed",
      content: { role: "assistant", text: "后创建" },
      inherited: false,
      created_at: now,
      updated_at: now,
    },
  ];
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: tied,
  });
  assert.deepEqual(
    selectItems(state).map((item) => item.item_id),
    ["itm_z_created_first", "itm_a_created_second"],
  );

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      5,
      "item.created",
      { kind: "message", status: "completed", content: { role: "assistant", text: "最新" } },
      { item_id: "itm_0_latest", turn_id: "trn_1" },
    ),
  });
  assert.deepEqual(
    selectItems(state).map((item) => item.item_id),
    ["itm_z_created_first", "itm_a_created_second", "itm_0_latest"],
  );
});

test("a delayed bootstrap cannot roll back a newer permission revision", () => {
  const current = runtimeReducer(initialRuntimeViewState, {
    type: "bootstrap.received",
    bootstrap: bootstrap(2, true, "2026-07-10T08:01:00.000Z"),
  });
  const stale = runtimeReducer(current, {
    type: "bootstrap.received",
    bootstrap: bootstrap(1, false, "2026-07-10T08:02:00.000Z"),
  });
  assert.equal(stale.bootstrap?.permissions.revision, 2);
  assert.equal(stale.bootstrap?.permissions.full_access, true);
});

test("first turn terminal event clears thinking even with zero assistant output", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("model_requested"),
  });
  assert.equal(selectIsThinking(state), true);

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(5, "turn.status_changed", {
      from: "model_requested",
      to: "failed",
      reason: "gateway_unavailable",
    }),
  });

  assert.equal(selectIsThinking(state), false);
  assert.equal(state.turns.trn_1.status, "failed");
  assert.equal(state.turns.trn_1.terminal_reason, "gateway_unavailable");
  assert.deepEqual(state.items, {});
});

test("duplicate terminal delivery is idempotent", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("streaming"),
  });
  const terminal = event(5, "turn.status_changed", {
    from: "streaming",
    to: "completed",
  });
  state = runtimeReducer(state, { type: "event.received", event: terminal });
  const duplicate = runtimeReducer(state, { type: "event.received", event: terminal });
  assert.deepEqual(duplicate, state);
  assert.equal(selectIsThinking(duplicate), false);
});

test("projection refresh hydrates and replaces authoritative jobs and interactions", () => {
  const hydrated: ThreadProjectionResponse = {
    ...projection("waiting_human"),
    jobs: [
      {
        job_id: "job_hydrated",
        kind: "agent_turn",
        status: "waiting_human",
        priority: 0,
        attempt: 1,
        max_attempts: 3,
        thread_id: "thr_1",
        turn_id: "trn_1",
        available_at: now,
        deadline: null,
        reason_code: null,
        created_at: now,
        updated_at: now,
      },
    ],
    interactions: [
      {
        interaction_id: "hitl_hydrated",
        kind: "permission_approval",
        status: "pending",
        prompt: "允许继续？",
        contract: {
          schema_version: 1,
          title: "权限确认",
          fields: [],
          actions: [
            {
              action_id: "allow",
              label: "允许",
              action_type: "allow",
              style: "primary",
              submits_form: false,
            },
          ],
          connector: null,
        },
        options: [],
        response: null,
        response_client_request_id: null,
        thread_id: "thr_1",
        turn_id: "trn_1",
        job_id: "job_hydrated",
        expires_at: null,
        created_at: now,
        updated_at: now,
      },
    ],
  };
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: hydrated,
  });
  assert.equal(state.jobs.job_hydrated.status, "waiting_human");
  assert.equal(selectPendingInteractions(state)[0]?.interaction_id, "hitl_hydrated");

  state = runtimeReducer(state, {
    type: "projection.received",
    projection: { ...hydrated, jobs: [], interactions: [], watermark: 5 },
  });
  assert.deepEqual(state.jobs, {});
  assert.deepEqual(state.interactions, {});
});

test("a frame batch appends streaming text in order with one reducer action", () => {
  const streamingProjection: ThreadProjectionResponse = {
    ...projection("streaming"),
    items: [
      {
        item_id: "assistant_1",
        thread_id: "thr_1",
        turn_id: "trn_1",
        kind: "message",
        status: "in_progress",
        content: { role: "assistant", text: "" },
        inherited: false,
        created_at: now,
        updated_at: now,
      },
    ],
  };
  const state = runtimeReducer(
    runtimeReducer(initialRuntimeViewState, {
      type: "projection.received",
      projection: streamingProjection,
    }),
    {
      type: "events.received",
      events: [
        event(5, "item.delta", { delta: "正在" }, { item_id: "assistant_1" }),
        event(6, "item.delta", { delta: "整理结果" }, { item_id: "assistant_1" }),
        event(
          7,
          "item.status_changed",
          { from: "in_progress", to: "completed" },
          { item_id: "assistant_1" },
        ),
      ],
    },
  );

  assert.equal(state.items.assistant_1.content.text, "正在整理结果");
  assert.equal(state.items.assistant_1.status, "completed");
  assert.equal(state.watermark, 7);
});

test("tool facts render only the backend public activity and keep Artifact references", () => {
  const requested = {
    schema_version: 1,
    tool_call_id: "call_read_1",
    tool_id: "read",
    tool_name: "read",
    display_label: "读取工作区",
    phase: "requested",
    status: "in_progress",
    effects: ["read"],
    risk: "low",
    argument_summary: "正在读取工作资料",
    result_summary: null,
    argument_sha256: "a".repeat(64),
    result_sha256: null,
    artifact_refs: [],
  };
  const completed = {
    ...requested,
    phase: "completed",
    status: "completed",
    result_summary: "已读取工作资料",
    result_sha256: "b".repeat(64),
    artifact_refs: [{ artifact_id: "art_report", revision_id: "rev_report" }],
  };
  const state = runtimeReducer(
    runtimeReducer(initialRuntimeViewState, {
      type: "projection.received",
      projection: projection("streaming"),
    }),
    {
      type: "events.received",
      events: [
        event(
          5,
          "item.created",
          { kind: "tool_call", status: "in_progress", content: requested },
          { item_id: "tool_item_1", tool_call_id: "call_read_1" },
        ),
        event(
          6,
          "tool.result",
          {
            activity: completed,
            result: { token: "must-never-enter-the-view-state" },
          },
          { item_id: "tool_item_1", tool_call_id: "call_read_1" },
        ),
        event(
          7,
          "item.status_changed",
          { from: "in_progress", to: "completed" },
          { item_id: "tool_item_1", tool_call_id: "call_read_1" },
        ),
      ],
    },
  );

  assert.deepEqual(state.items.tool_item_1.content, completed);
  assert.equal(state.items.tool_item_1.status, "completed");
  assert.equal(JSON.stringify(state).includes("must-never-enter-the-view-state"), false);
});

test("a frame batch stops at the first sequence gap without applying later facts", () => {
  const current = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("queued"),
  });
  const state = runtimeReducer(current, {
    type: "events.received",
    events: [
      event(5, "turn.status_changed", { to: "preparing" }),
      event(7, "turn.status_changed", { to: "streaming" }),
      event(8, "turn.status_changed", { to: "completed" }),
    ],
  });

  assert.equal(state.watermark, 5);
  assert.equal(state.turns.trn_1.status, "preparing");
  assert.equal(state.resyncRequired, true);
  assert.equal(state.resyncReason, "event_gap:6:7");
});

test("reasoning stays visible across phase and tool events until the next atom replaces it", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("streaming"),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      5,
      "reasoning.replaced",
      {
        atom_id: "reasoning-a",
        delta: "先检查输入文件。",
        revision: 1,
        presentation: "visible",
        previous_item_id: null,
        previous_revision: null,
        previous_presentation: null,
      },
      { item_id: "reasoning_item_a" },
    ),
  });
  assert.equal(selectVisibleReasoning(state)?.content.text, "先检查输入文件。");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(6, "tool.call_requested", { tool_name: "read" }, { item_id: "tool_1" }),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(7, "turn.status_changed", { from: "streaming", to: "tool_pending" }),
  });
  assert.equal(selectVisibleReasoning(state)?.item_id, "reasoning_item_a");
  assert.equal(selectVisibleReasoning(state)?.content.text, "先检查输入文件。");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      8,
      "reasoning.replaced",
      {
        atom_id: "reasoning-b",
        delta: "文件已读取，继续核对结构。",
        revision: 1,
        presentation: "visible",
        previous_item_id: "reasoning_item_a",
        previous_revision: 2,
        previous_presentation: "archived",
      },
      { item_id: "reasoning_item_b" },
    ),
  });
  assert.equal(selectVisibleReasoning(state)?.item_id, "reasoning_item_b");
  assert.equal(state.items.reasoning_item_a.content.presentation, "archived");
  assert.equal(state.items.reasoning_item_a.status, "completed");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      9,
      "reasoning.delta",
      { atom_id: "reasoning-b", delta: "准备输出。", revision: 2 },
      { item_id: "reasoning_item_b" },
    ),
  });
  assert.equal(
    selectVisibleReasoning(state)?.content.text,
    "文件已读取，继续核对结构。准备输出。",
  );
  assert.equal(selectVisibleReasoning(state)?.content.revision, 2);
});

test("terminal phase alone cannot hide reasoning; explicit archive does", () => {
  const withReasoning: ThreadProjectionResponse = {
    ...projection("finalizing"),
    items: [
      {
        item_id: "reasoning_item",
        thread_id: "thr_1",
        turn_id: "trn_1",
        kind: "reasoning",
        status: "in_progress",
        content: {
          channel: "reasoning_summary",
          atom_id: "reasoning-final",
          text: "正在核对最终结果。",
          revision: 3,
          presentation: "visible",
          archived_reason: null,
        },
        inherited: false,
        created_at: now,
        updated_at: now,
      },
    ],
  };
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: withReasoning,
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(5, "turn.status_changed", { from: "finalizing", to: "completed" }),
  });
  assert.equal(selectIsThinking(state), false);
  assert.equal(selectVisibleReasoning(state)?.content.text, "正在核对最终结果。");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      6,
      "reasoning.archived",
      {
        revision: 4,
        presentation: "collapsed",
        reason: "completed",
        terminal_status: "completed",
      },
      { item_id: "reasoning_item" },
    ),
  });
  assert.equal(selectVisibleReasoning(state), null);
  assert.equal(state.items.reasoning_item.content.presentation, "collapsed");
});

test("projection replay reconstructs the exact visible reasoning revision", () => {
  const replayProjection: ThreadProjectionResponse = {
    ...projection("streaming"),
    watermark: 17,
    items: [
      {
        item_id: "reasoning_old",
        thread_id: "thr_1",
        turn_id: "trn_1",
        kind: "reasoning",
        status: "completed",
        content: {
          channel: "reasoning_summary",
          atom_id: "old",
          text: "旧步骤",
          revision: 2,
          presentation: "archived",
          archived_reason: "replaced_by_next_atom",
        },
        inherited: false,
        created_at: now,
        updated_at: now,
      },
      {
        item_id: "reasoning_current",
        thread_id: "thr_1",
        turn_id: "trn_1",
        kind: "reasoning",
        status: "in_progress",
        content: {
          channel: "reasoning_summary",
          atom_id: "current",
          text: "当前步骤仍然可见",
          revision: 5,
          presentation: "visible",
          archived_reason: null,
        },
        inherited: false,
        created_at: "2026-07-10T08:00:01.000Z",
        updated_at: "2026-07-10T08:00:02.000Z",
      },
    ],
  };
  const restored = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: replayProjection,
  });
  assert.equal(selectVisibleReasoning(restored)?.item_id, "reasoning_current");
  assert.equal(selectVisibleReasoning(restored)?.content.revision, 5);
  assert.equal(restored.watermark, 17);
});

test("a sequence gap does not mutate projections and requests resync", () => {
  const state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("queued"),
  });
  const gapped = runtimeReducer(state, {
    type: "event.received",
    event: event(7, "turn.status_changed", { from: "queued", to: "streaming" }),
  });
  assert.equal(gapped.watermark, 4);
  assert.equal(gapped.turns.trn_1.status, "queued");
  assert.equal(gapped.resyncRequired, true);
  assert.equal(gapped.resyncReason, "event_gap:5:7");
});

test("projection atomically clears a prior gap and replaces derived state", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("queued"),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(8, "turn.status_changed", { to: "streaming" }),
  });
  assert.equal(state.resyncRequired, true);

  state = runtimeReducer(state, {
    type: "projection.received",
    projection: { ...projection("streaming"), watermark: 8 },
  });
  assert.equal(state.resyncRequired, false);
  assert.equal(state.watermark, 8);
  assert.equal(state.turns.trn_1.status, "streaming");
});

test("a late projection cannot roll the reducer watermark backward", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("streaming"),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(5, "turn.status_changed", { to: "finalizing" }),
  });
  const unchanged = runtimeReducer(state, {
    type: "projection.received",
    projection: projection("queued"),
  });
  assert.deepEqual(unchanged, state);
});

test("HITL is visible until a persisted terminal interaction event", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("waiting_human"),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      5,
      "interaction.requested",
      {
        kind: "permission_approval",
        prompt: "允许写入工作区？",
        options: [{ id: "allow", label: "允许" }],
        contract: {
          schema_version: 1,
          title: "权限确认",
          fields: [],
          actions: [
            {
              action_id: "allow",
              label: "允许",
              action_type: "allow",
              style: "primary",
              submits_form: false,
            },
          ],
          connector: null,
        },
      },
      { item_id: "hitl_1", job_id: "job_1" },
    ),
  });
  assert.equal(selectIsThinking(state), false);
  assert.equal(selectPendingInteractions(state).length, 1);

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(
      6,
      "interaction.resolved",
      {
        response: { action_id: "allow", values: {} },
        client_request_id: "interaction-response-1",
      },
      { item_id: "hitl_1", job_id: "job_1" },
    ),
  });
  assert.equal(selectPendingInteractions(state).length, 0);
  assert.deepEqual(state.interactions.hitl_1.response, {
    action_id: "allow",
    values: {},
  });
});

test("events from another thread cannot cross-talk into the active task", () => {
  const state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("queued"),
  });
  const unchanged = runtimeReducer(state, {
    type: "event.received",
    event: event(
      5,
      "turn.status_changed",
      { to: "completed" },
      { thread_id: "thr_other", turn_id: "trn_other" },
    ),
  });
  assert.deepEqual(unchanged, state);
});

test("thread title and status events remain projection-driven", () => {
  let state = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: projection("completed"),
  });
  state = runtimeReducer(state, {
    type: "event.received",
    event: event(5, "thread.title_generated", { title: "客户访谈摘要" }, { turn_id: null }),
  });
  assert.equal(state.thread?.title, "客户访谈摘要");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(6, "thread.archived", {}, { turn_id: null }),
  });
  assert.equal(state.thread?.status, "archived");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(7, "thread.restored", {}, { turn_id: null }),
  });
  assert.equal(state.thread?.status, "active");

  state = runtimeReducer(state, {
    type: "event.received",
    event: event(8, "thread.renamed", { title: "已确认的客户访谈" }, { turn_id: null }),
  });
  assert.equal(state.thread?.title, "已确认的客户访谈");
  assert.equal(state.watermark, 8);
});

test("switching tasks accepts the target projection even with a lower independent watermark", () => {
  const current = runtimeReducer(initialRuntimeViewState, {
    type: "projection.received",
    projection: { ...projection("completed"), watermark: 42 },
  });
  const target = projection("queued");
  target.thread = { ...target.thread, thread_id: "thr_2", title: "另一项任务" };
  target.turns = target.turns.map((turn) => ({
    ...turn,
    thread_id: "thr_2",
    turn_id: "trn_2",
  }));
  target.watermark = 3;

  const switched = runtimeReducer(current, {
    type: "projection.received",
    projection: target,
  });
  assert.equal(switched.thread?.thread_id, "thr_2");
  assert.equal(switched.watermark, 3);
  assert.deepEqual(Object.keys(switched.turns), ["trn_2"]);

  const cleared = runtimeReducer(switched, { type: "thread.cleared" });
  assert.equal(cleared.thread, null);
  assert.deepEqual(cleared.items, {});
  assert.deepEqual(cleared.turns, {});
});
