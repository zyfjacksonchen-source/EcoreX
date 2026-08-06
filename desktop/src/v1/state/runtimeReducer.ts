import type {
  BootstrapResponse,
  EventEnvelope,
  InteractionContract,
  InteractionProjection,
  InteractionResponse,
  ItemProjection,
  JobProjection,
  RuntimeTiming,
  ThreadProjection,
  ThreadProjectionResponse,
  TurnProjection,
  TurnStatus,
} from "../api/contracts.ts";

const TERMINAL_TURNS = new Set<TurnStatus>([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "superseded",
]);

const THINKING_TURNS = new Set<TurnStatus>([
  "accepted",
  "queued",
  "preparing",
  "model_requested",
  "streaming",
  "tool_pending",
  "tool_running",
  "retry_wait",
  "finalizing",
]);

export type StreamState = "idle" | "connecting" | "open" | "retrying" | "closed";

export interface RuntimeViewState {
  bootstrap: BootstrapResponse | null;
  thread: ThreadProjection | null;
  turns: Record<string, TurnProjection>;
  items: Record<string, ItemProjection>;
  jobs: Record<string, JobProjection>;
  interactions: Record<string, InteractionProjection>;
  watermark: number;
  streamState: StreamState;
  resyncRequired: boolean;
  resyncReason: string | null;
}

export type RuntimeAction =
  | { type: "bootstrap.received"; bootstrap: BootstrapResponse }
  | { type: "projection.received"; projection: ThreadProjectionResponse }
  | { type: "event.received"; event: EventEnvelope }
  | { type: "events.received"; events: readonly EventEnvelope[] }
  | { type: "stream.state"; state: StreamState }
  | { type: "thread.cleared" };

export const initialRuntimeViewState: RuntimeViewState = {
  bootstrap: null,
  thread: null,
  turns: {},
  items: {},
  jobs: {},
  interactions: {},
  watermark: 0,
  streamState: "idle",
  resyncRequired: false,
  resyncReason: null,
};

function byId<T>(values: T[], key: keyof T): Record<string, T> {
  return Object.fromEntries(values.map((value) => [String(value[key]), value]));
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function operationTiming(
  startedAt: string,
  finishedAt: string | null,
): RuntimeTiming {
  return {
    started_at: startedAt,
    finished_at: finishedAt,
    duration_ms: finishedAt === null
      ? null
      : Math.max(0, Date.parse(finishedAt) - Date.parse(startedAt)),
  };
}

function transitionToolActivity(
  content: Record<string, unknown>,
  status: ItemProjection["status"],
  updatedAt: string,
): Record<string, unknown> {
  const phase = ({
    created: "requested",
    in_progress: "running",
    waiting_human: "waiting_human",
    completed: "completed",
    failed: "failed",
    cancelled: "cancelled",
  } satisfies Record<ItemProjection["status"], string>)[status];
  const fixedSummary = status === "failed"
    ? "此步骤未完成"
    : status === "cancelled"
    ? "此步骤已取消"
    : status === "waiting_human"
    ? "等待你确认后继续"
    : null;
  const currentTiming = objectValue(content.timing);
  const startedAt = stringValue(currentTiming.started_at) ?? updatedAt;
  const terminal = status === "completed" || status === "failed" || status === "cancelled";
  return {
    ...content,
    phase,
    status,
    timing: operationTiming(startedAt, terminal ? updatedAt : null),
    ...(fixedSummary ? { result_summary: fixedSummary } : {}),
  };
}

interface FrameEventSpan {
  event: EventEnvelope;
  firstSeq: number;
  lastSeq: number;
}

export function coalesceFrameEvents(
  events: readonly EventEnvelope[],
): FrameEventSpan[] {
  const spans: FrameEventSpan[] = [];
  for (const event of events) {
    const previous = spans.at(-1);
    const delta = stringValue(event.payload.delta);
    if (
      delta !== null
      && (event.event_type === "item.delta" || event.event_type === "reasoning.delta")
      && previous?.event.event_type === event.event_type
      && previous.event.item_id === event.item_id
      && event.seq === previous.lastSeq + 1
    ) {
      previous.event = {
        ...previous.event,
        created_at: event.created_at,
        payload: {
          ...previous.event.payload,
          ...event.payload,
          delta: `${stringValue(previous.event.payload.delta) ?? ""}${delta}`,
        },
      };
      previous.lastSeq = event.seq;
      continue;
    }
    spans.push({ event, firstSeq: event.seq, lastSeq: event.seq });
  }
  return spans;
}

function interactionContractValue(value: unknown): InteractionContract | null {
  const contract = objectValue(value);
  if (
    contract.schema_version !== 1
    || typeof contract.title !== "string"
    || !Array.isArray(contract.fields)
    || !Array.isArray(contract.actions)
    || contract.actions.length === 0
  ) return null;
  return contract as unknown as InteractionContract;
}

function interactionResponseValue(value: unknown): InteractionResponse | null {
  const response = objectValue(value);
  if (typeof response.action_id !== "string") return null;
  return {
    action_id: response.action_id,
    values: objectValue(response.values) as Record<string, string | boolean>,
  };
}

function replaceTurnStatus(
  state: RuntimeViewState,
  event: EventEnvelope,
  status: TurnStatus,
): RuntimeViewState {
  if (!event.turn_id) return state;
  const existing = state.turns[event.turn_id];
  if (!existing) return state;
  const terminal = TERMINAL_TURNS.has(status);
  const startedAt = existing.timing?.started_at ?? existing.created_at;
  return {
    ...state,
    turns: {
      ...state.turns,
      [event.turn_id]: {
        ...existing,
        status,
        terminal_reason: TERMINAL_TURNS.has(status)
          ? stringValue(event.payload.reason)
          : null,
        timing: operationTiming(startedAt, terminal ? event.created_at : null),
        updated_at: event.created_at,
      },
    },
  };
}

function reduceKnownEvent(state: RuntimeViewState, event: EventEnvelope): RuntimeViewState {
  switch (event.event_type) {
    case "thread.created":
      return {
        ...state,
        thread: state.thread ?? {
          thread_id: event.thread_id,
          status: "active",
          title: stringValue(event.payload.title),
          pinned: objectValue(event.payload.metadata).pinned === true,
          active_turn_status: null,
          last_turn_status: null,
          metadata: objectValue(event.payload.metadata),
          forked_from_thread_id: null,
          forked_from_turn_id: null,
          forked_from_seq: null,
          created_at: event.created_at,
          updated_at: event.created_at,
        },
      };
    case "thread.archived":
      return state.thread
        ? {
            ...state,
            thread: { ...state.thread, status: "archived", updated_at: event.created_at },
          }
        : state;
    case "thread.restored":
      return state.thread
        ? {
            ...state,
            thread: { ...state.thread, status: "active", updated_at: event.created_at },
          }
        : state;
    case "thread.deleted":
      return state.thread
        ? {
            ...state,
            thread: { ...state.thread, status: "deleted", updated_at: event.created_at },
          }
        : state;
    case "thread.pin_changed":
      return state.thread
        ? {
            ...state,
            thread: {
              ...state.thread,
              pinned: event.payload.pinned === true,
              metadata: {
                ...state.thread.metadata,
                pinned: event.payload.pinned === true,
              },
              updated_at: event.created_at,
            },
          }
        : state;
    case "thread.renamed":
    case "thread.title_generated":
      return state.thread
        ? {
            ...state,
            thread: {
              ...state.thread,
              title: stringValue(event.payload.title) ?? state.thread.title,
              updated_at: event.created_at,
            },
          }
        : state;
    case "turn.accepted": {
      if (!event.turn_id) return state;
      const existing = state.turns[event.turn_id];
      const turn: TurnProjection = existing ?? {
        turn_id: event.turn_id,
        thread_id: event.thread_id,
        status: "accepted",
        input: stringValue(event.payload.input) ?? "",
        agent_model_id: stringValue(event.payload.agent_model_id) ?? "",
        image_model_id: stringValue(event.payload.image_model_id),
        client_message_id: event.client_message_id,
        metadata: objectValue(event.payload.metadata),
        terminal_reason: null,
        timing: operationTiming(event.created_at, null),
        created_at: event.created_at,
        updated_at: event.created_at,
      };
      return { ...state, turns: { ...state.turns, [turn.turn_id]: turn } };
    }
    case "turn.queued":
      return replaceTurnStatus(state, event, "queued");
    case "turn.status_changed": {
      const target = stringValue(event.payload.to) as TurnStatus | null;
      return target ? replaceTurnStatus(state, event, target) : state;
    }
    case "item.created": {
      if (!event.item_id || !event.turn_id) return state;
      const existing = state.items[event.item_id];
      const item: ItemProjection = existing ?? {
        item_id: event.item_id,
        thread_id: event.thread_id,
        turn_id: event.turn_id,
        kind: (stringValue(event.payload.kind) ?? "message") as ItemProjection["kind"],
        status: (stringValue(event.payload.status) ?? "created") as ItemProjection["status"],
        content: objectValue(event.payload.content),
        inherited: false,
        created_seq: event.seq,
        created_at: event.created_at,
        updated_at: event.created_at,
      };
      return { ...state, items: { ...state.items, [item.item_id]: item } };
    }
    case "task_list.updated": {
      if (!event.item_id || state.items[event.item_id]?.kind !== "task_list") return state;
      const item = state.items[event.item_id];
      const content = objectValue(event.payload);
      const entries = Array.isArray(content.items) ? content.items : [];
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            status: entries.length > 0 && entries.every((entry) => objectValue(entry).status === "completed")
              ? "completed"
              : "in_progress",
            content,
            updated_at: event.created_at,
          },
        },
      };
    }
    case "turn.steered": {
      if (!event.item_id || !event.turn_id) return state;
      const item: ItemProjection = {
        item_id: event.item_id,
        thread_id: event.thread_id,
        turn_id: event.turn_id,
        kind: "message",
        status: "completed",
        content: {
          role: "user",
          text: stringValue(event.payload.input) ?? "",
          metadata: objectValue(event.payload.metadata),
          steer: true,
        },
        inherited: false,
        created_seq: event.seq,
        created_at: event.created_at,
        updated_at: event.created_at,
      };
      return { ...state, items: { ...state.items, [item.item_id]: item } };
    }
    case "reasoning.replaced": {
      if (!event.item_id || !event.turn_id) return state;
      const previousItemId = stringValue(event.payload.previous_item_id);
      const previous = previousItemId ? state.items[previousItemId] : null;
      const items = { ...state.items };
      if (previousItemId && previous?.kind === "reasoning") {
        items[previousItemId] = {
          ...previous,
          status: "completed",
          content: {
            ...previous.content,
            revision:
              numberValue(event.payload.previous_revision)
              ?? numberValue(previous.content.revision)
              ?? 1,
            presentation:
              stringValue(event.payload.previous_presentation) ?? "archived",
            archived_reason: "replaced_by_next_atom",
          },
          updated_at: event.created_at,
        };
      }
      items[event.item_id] = {
        item_id: event.item_id,
        thread_id: event.thread_id,
        turn_id: event.turn_id,
        kind: "reasoning",
        status: "in_progress",
        content: {
          channel: "reasoning_summary",
          atom_id: stringValue(event.payload.atom_id) ?? "",
          text: stringValue(event.payload.delta) ?? "",
          revision: numberValue(event.payload.revision) ?? 1,
          presentation: stringValue(event.payload.presentation) ?? "visible",
          archived_reason: null,
        },
        inherited: false,
        created_seq: event.seq,
        created_at: event.created_at,
        updated_at: event.created_at,
      };
      return { ...state, items };
    }
    case "reasoning.delta": {
      if (!event.item_id || state.items[event.item_id]?.kind !== "reasoning") return state;
      const item = state.items[event.item_id];
      const delta = stringValue(event.payload.delta) ?? "";
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            content: {
              ...item.content,
              text: `${stringValue(item.content.text) ?? ""}${delta}`,
              revision:
                numberValue(event.payload.revision)
                ?? (numberValue(item.content.revision) ?? 1) + 1,
            },
            updated_at: event.created_at,
          },
        },
      };
    }
    case "reasoning.archived": {
      if (!event.item_id || state.items[event.item_id]?.kind !== "reasoning") return state;
      const item = state.items[event.item_id];
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            content: {
              ...item.content,
              revision:
                numberValue(event.payload.revision)
                ?? (numberValue(item.content.revision) ?? 1) + 1,
              presentation: stringValue(event.payload.presentation) ?? "collapsed",
              archived_reason: stringValue(event.payload.reason),
            },
            updated_at: event.created_at,
          },
        },
      };
    }
    case "item.status_changed": {
      if (!event.item_id || !state.items[event.item_id]) return state;
      const item = state.items[event.item_id];
      const status = (stringValue(event.payload.to) ?? item.status) as ItemProjection["status"];
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            status,
            content: item.kind === "tool_call"
              ? transitionToolActivity(item.content, status, event.created_at)
              : item.content,
            updated_at: event.created_at,
          },
        },
      };
    }
    case "tool.result": {
      if (!event.item_id || state.items[event.item_id]?.kind !== "tool_call") return state;
      const activity = objectValue(event.payload.activity);
      if (
        typeof activity.tool_call_id !== "string"
        || typeof activity.tool_id !== "string"
        || typeof activity.display_label !== "string"
      ) return state;
      const item = state.items[event.item_id];
      const status = stringValue(activity.status) as ItemProjection["status"] | null;
      if (!status) return state;
      const currentTiming = objectValue(item.content.timing);
      const startedAt = stringValue(currentTiming.started_at) ?? item.created_at;
      const terminal = status === "completed" || status === "failed" || status === "cancelled";
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            status,
            content: {
              ...activity,
              timing: operationTiming(startedAt, terminal ? event.created_at : null),
            },
            updated_at: event.created_at,
          },
        },
      };
    }
    case "item.delta": {
      if (!event.item_id || state.items[event.item_id]?.kind !== "message") return state;
      const item = state.items[event.item_id];
      const delta = stringValue(event.payload.delta);
      if (delta === null || delta.length === 0) return state;
      return {
        ...state,
        items: {
          ...state.items,
          [event.item_id]: {
            ...item,
            content: {
              ...item.content,
              text: `${stringValue(item.content.text) ?? ""}${delta}`,
            },
            updated_at: event.created_at,
          },
        },
      };
    }
    case "interaction.requested": {
      if (!event.item_id) return state;
      const contract = interactionContractValue(event.payload.contract);
      if (!contract) return state;
      const interaction: InteractionProjection = {
        interaction_id: event.item_id,
        kind: (stringValue(event.payload.kind) ?? "information") as InteractionProjection["kind"],
        status: "pending",
        prompt: stringValue(event.payload.prompt) ?? "需要你的确认",
        contract,
        options: Array.isArray(event.payload.options)
          ? event.payload.options.map(objectValue)
          : [],
        response: null,
        response_client_request_id: null,
        thread_id: event.thread_id,
        turn_id: event.turn_id,
        job_id: event.job_id,
        expires_at: stringValue(event.payload.expires_at),
        created_seq: event.seq,
        created_at: event.created_at,
        updated_at: event.created_at,
      };
      return {
        ...state,
        interactions: { ...state.interactions, [interaction.interaction_id]: interaction },
      };
    }
    case "interaction.resolved":
    case "interaction.cancelled":
    case "interaction.expired": {
      if (!event.item_id || !state.interactions[event.item_id]) return state;
      const interaction = state.interactions[event.item_id];
      const status = event.event_type.split(".")[1] as InteractionProjection["status"];
      return {
        ...state,
        interactions: {
          ...state.interactions,
          [event.item_id]: {
            ...interaction,
            status,
            response:
              event.event_type === "interaction.resolved"
                ? interactionResponseValue(event.payload.response)
                : null,
            response_client_request_id:
              event.event_type === "interaction.resolved"
                ? stringValue(event.payload.client_request_id)
                : null,
            updated_at: event.created_at,
          },
        },
      };
    }
    default:
      return state;
  }
}

function reduceEventEnvelope(
  state: RuntimeViewState,
  event: EventEnvelope,
  lastSeq = event.seq,
): RuntimeViewState {
  if (state.resyncRequired) return state;
  if (state.thread && event.thread_id !== state.thread.thread_id) return state;
  if (event.seq <= state.watermark) return state;
  if (event.seq !== state.watermark + 1) {
    return {
      ...state,
      resyncRequired: true,
      resyncReason: `event_gap:${state.watermark + 1}:${event.seq}`,
    };
  }
  const reduced = reduceKnownEvent(state, event);
  return { ...reduced, watermark: lastSeq };
}

export function runtimeReducer(
  state: RuntimeViewState,
  action: RuntimeAction,
): RuntimeViewState {
  switch (action.type) {
    case "bootstrap.received": {
      const existing = state.bootstrap;
      if (!existing) return { ...state, bootstrap: action.bootstrap };
      const existingPermission = existing.permissions;
      const incomingPermission = action.bootstrap.permissions;
      const incomingIsOlder = incomingPermission.revision < existingPermission.revision
        || (
          incomingPermission.revision === existingPermission.revision
          && Date.parse(action.bootstrap.server_time) < Date.parse(existing.server_time)
        );
      return {
        ...state,
        bootstrap: incomingIsOlder
          ? { ...action.bootstrap, permissions: existingPermission }
          : action.bootstrap,
      };
    }
    case "projection.received":
      if (
        state.thread?.thread_id === action.projection.thread.thread_id &&
        action.projection.watermark < state.watermark
      ) {
        return state;
      }
      return {
        ...state,
        thread: action.projection.thread,
        turns: byId(action.projection.turns, "turn_id"),
        items: byId(action.projection.items, "item_id"),
        jobs: byId(action.projection.jobs, "job_id"),
        interactions: byId(action.projection.interactions, "interaction_id"),
        watermark: action.projection.watermark,
        resyncRequired: false,
        resyncReason: null,
      };
    case "event.received": {
      return reduceEventEnvelope(state, action.event);
    }
    case "events.received": {
      if (action.events.length === 0) return state;
      let reduced = state;
      const unseen = action.events.filter((event) => event.seq > state.watermark);
      for (const span of coalesceFrameEvents(unseen)) {
        reduced = reduceEventEnvelope(reduced, span.event, span.lastSeq);
        if (reduced.resyncRequired) break;
      }
      return reduced;
    }
    case "stream.state":
      return { ...state, streamState: action.state };
    case "thread.cleared":
      return { ...initialRuntimeViewState, bootstrap: state.bootstrap };
  }
}

export function selectTurns(state: RuntimeViewState): TurnProjection[] {
  // Projection arrays and subsequently persisted events already arrive in
  // authoritative sequence order. Timestamps may tie and opaque IDs must
  // never be repurposed as a chat-ordering key.
  return Object.values(state.turns);
}

export function selectItems(state: RuntimeViewState): ItemProjection[] {
  return Object.values(state.items);
}

export function selectActiveTurn(state: RuntimeViewState): TurnProjection | null {
  const candidates = selectTurns(state).filter((turn) => !TERMINAL_TURNS.has(turn.status));
  return candidates.at(-1) ?? null;
}

export function selectIsThinking(state: RuntimeViewState): boolean {
  const active = selectActiveTurn(state);
  return active ? THINKING_TURNS.has(active.status) : false;
}

export function selectVisibleReasoning(state: RuntimeViewState): ItemProjection | null {
  const candidates = selectItems(state).filter(
    (item) => item.kind === "reasoning"
      && (item.content.presentation === "visible" || item.content.presentation === "collapsed"),
  );
  return candidates.at(-1) ?? null;
}

export function selectPendingInteractions(
  state: RuntimeViewState,
): InteractionProjection[] {
  return Object.values(state.interactions)
    .filter((interaction) => interaction.status === "pending")
    .sort((left, right) => left.created_at.localeCompare(right.created_at));
}

export function selectInteractions(state: RuntimeViewState): InteractionProjection[] {
  return Object.values(state.interactions);
}
