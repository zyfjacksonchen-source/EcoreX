import assert from "node:assert/strict";
import test from "node:test";

import type {
  LiveReplayResponse,
  MockReplayResponse,
  ThreadProjectionResponse,
} from "../api/contracts.ts";
import {
  canSubmitLiveReplay,
  hasCurrentLiveReplayAuthority,
  initialReplayViewState,
  replayViewReducer,
  stableLiveReplayRequest,
} from "./replay.ts";

const timestamp = "2026-07-10T12:00:00.000Z";

function projection(): ThreadProjectionResponse {
  return {
    thread: {
      thread_id: "thread-replay",
      status: "active",
      title: "月度复盘",
      pinned: false,
      active_turn_status: null,
      last_turn_status: null,
      metadata: {},
      forked_from_thread_id: null,
      forked_from_turn_id: null,
      forked_from_seq: null,
      created_at: timestamp,
      updated_at: timestamp,
    },
    turns: [
      {
        turn_id: "turn-not-authorized",
        thread_id: "thread-replay",
        status: "completed",
        input: "没有被后端授权的继承 Turn",
        agent_model_id: "ecorex-chat",
        image_model_id: "gpt-image-2",
        client_message_id: "message-one",
        metadata: {},
        terminal_reason: "completed",
        inherited: false,
        created_at: timestamp,
        updated_at: timestamp,
      },
      {
        turn_id: "turn-authorized",
        thread_id: "thread-replay",
        status: "completed",
        input: "形成月度复盘",
        agent_model_id: "ecorex-chat",
        image_model_id: "gpt-image-2",
        client_message_id: "message-two",
        metadata: {},
        terminal_reason: "completed",
        inherited: false,
        created_at: timestamp,
        updated_at: timestamp,
      },
    ],
    items: [],
    jobs: [],
    interactions: [],
    watermark: 18,
  };
}

function snapshot(): MockReplayResponse {
  return {
    projection: projection(),
    interactions: [],
    live_replay_turn_ids: ["turn-authorized"],
    source_watermark: 18,
    through_seq: 18,
    event_count: 18,
    event_digest: "a".repeat(64),
  };
}

function liveResult(): LiveReplayResponse {
  return {
    source_thread_id: "thread-replay",
    source_turn_id: "turn-authorized",
    causation_event_id: "event-source",
    permission_snapshot_id: "permission-current",
    extension_snapshot_id: "extensions-current",
    replay: {
      turn: {
        ...projection().turns[1]!,
        turn_id: "turn-live-replay",
        status: "queued",
        client_message_id: "live-replay-message",
      },
      job: null,
      watermark: 19,
    },
  };
}

test("Mock Replay maps only backend-authorized Live Replay candidates", () => {
  const loaded = replayViewReducer(initialReplayViewState, {
    type: "mock.received",
    snapshot: snapshot(),
  });
  assert.equal(loaded.mockState, "ready");
  assert.equal(loaded.selectedTurnId, "turn-authorized");
  assert.notEqual(loaded.selectedTurnId, "turn-not-authorized");
  assert.equal(loaded.confirmed, false);
});

test("Live Replay retry keeps one client request id until the backend accepts it", () => {
  let allocations = 0;
  const loaded = replayViewReducer(initialReplayViewState, {
    type: "mock.received",
    snapshot: snapshot(),
  });
  const firstRequest = stableLiveReplayRequest(
    loaded,
    loaded.selectedTurnId,
    () => `replay-request-${++allocations}`,
  );
  const submitting = replayViewReducer(
    replayViewReducer(loaded, { type: "confirmation.changed", confirmed: true }),
    { type: "live.requested", request: firstRequest },
  );
  const failed = replayViewReducer(submitting, {
    type: "live.failed",
    message: "Runtime connection was interrupted",
  });
  const retryRequest = stableLiveReplayRequest(
    failed,
    failed.selectedTurnId,
    () => `replay-request-${++allocations}`,
  );

  assert.equal(failed.liveState, "error");
  assert.equal(retryRequest.clientRequestId, firstRequest.clientRequestId);
  assert.equal(allocations, 1);
});

test("changing the source requires a new confirmation and request identity", () => {
  const pending = replayViewReducer(
    replayViewReducer(initialReplayViewState, {
      type: "mock.received",
      snapshot: snapshot(),
    }),
    {
      type: "live.requested",
      request: { sourceTurnId: "turn-authorized", clientRequestId: "stable-one" },
    },
  );
  const changed = replayViewReducer(pending, {
    type: "source.selected",
    turnId: "different-authorized-turn",
  });

  assert.equal(changed.confirmed, false);
  assert.equal(changed.pendingLive, null);
  assert.equal(changed.liveResult, null);
});

test("accepted Live Replay surfaces the exact new Turn and permission snapshot", () => {
  const state = replayViewReducer(initialReplayViewState, {
    type: "live.received",
    result: liveResult(),
  });
  assert.equal(state.liveState, "completed");
  assert.equal(state.liveResult?.replay.turn.turn_id, "turn-live-replay");
  assert.equal(state.liveResult?.permission_snapshot_id, "permission-current");
  assert.equal(state.confirmed, false);
  assert.equal(state.pendingLive, null);
});

test("a retained snapshot is readable but cannot authorize Live Replay after revalidation fails", () => {
  const loaded = replayViewReducer(initialReplayViewState, {
    type: "mock.received",
    snapshot: snapshot(),
  });
  const confirmed = replayViewReducer(loaded, {
    type: "confirmation.changed",
    confirmed: true,
  });
  assert.equal(hasCurrentLiveReplayAuthority(confirmed), true);
  assert.equal(canSubmitLiveReplay(confirmed), true);

  const revalidating = replayViewReducer(confirmed, { type: "mock.requested" });
  assert.equal(revalidating.snapshot?.event_digest, snapshot().event_digest);
  assert.equal(hasCurrentLiveReplayAuthority(revalidating), false);
  assert.equal(canSubmitLiveReplay(revalidating), false);

  const stale = replayViewReducer(revalidating, {
    type: "mock.failed",
    message: "integrity check failed",
  });
  assert.equal(stale.snapshot?.event_digest, snapshot().event_digest);
  assert.equal(hasCurrentLiveReplayAuthority(stale), false);
  assert.equal(canSubmitLiveReplay(stale), false);
});
