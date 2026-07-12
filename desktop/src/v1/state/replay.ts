import type {
  LiveReplayResponse,
  MockReplayResponse,
} from "../api/contracts.ts";

export type ReplayLoadState = "idle" | "loading" | "ready" | "error";
export type LiveReplayState = "idle" | "submitting" | "completed" | "error";

export interface PendingLiveReplay {
  sourceTurnId: string;
  clientRequestId: string;
}

export interface ReplayViewState {
  mockState: ReplayLoadState;
  snapshot: MockReplayResponse | null;
  mockError: string | null;
  selectedTurnId: string;
  confirmed: boolean;
  liveState: LiveReplayState;
  liveError: string | null;
  pendingLive: PendingLiveReplay | null;
  liveResult: LiveReplayResponse | null;
}

export type ReplayViewAction =
  | { type: "dialog.reset" }
  | { type: "mock.requested" }
  | { type: "mock.received"; snapshot: MockReplayResponse }
  | { type: "mock.failed"; message: string }
  | { type: "source.selected"; turnId: string }
  | { type: "confirmation.changed"; confirmed: boolean }
  | { type: "live.requested"; request: PendingLiveReplay }
  | { type: "live.received"; result: LiveReplayResponse }
  | { type: "live.failed"; message: string };

export const initialReplayViewState: ReplayViewState = {
  mockState: "idle",
  snapshot: null,
  mockError: null,
  selectedTurnId: "",
  confirmed: false,
  liveState: "idle",
  liveError: null,
  pendingLive: null,
  liveResult: null,
};

export function replayViewReducer(
  state: ReplayViewState,
  action: ReplayViewAction,
): ReplayViewState {
  switch (action.type) {
    case "dialog.reset":
      return initialReplayViewState;
    case "mock.requested":
      return {
        ...state,
        mockState: "loading",
        mockError: null,
      };
    case "mock.received": {
      const candidates = action.snapshot.live_replay_turn_ids;
      const selectedTurnId = candidates.includes(state.selectedTurnId)
        ? state.selectedTurnId
        : candidates.at(-1) ?? "";
      return {
        ...state,
        mockState: "ready",
        snapshot: action.snapshot,
        mockError: null,
        selectedTurnId,
        confirmed: selectedTurnId === state.selectedTurnId ? state.confirmed : false,
        pendingLive: state.pendingLive?.sourceTurnId === selectedTurnId
          ? state.pendingLive
          : null,
      };
    }
    case "mock.failed":
      return {
        ...state,
        mockState: "error",
        mockError: action.message,
      };
    case "source.selected":
      return {
        ...state,
        selectedTurnId: action.turnId,
        confirmed: false,
        liveState: "idle",
        liveError: null,
        pendingLive: state.pendingLive?.sourceTurnId === action.turnId
          ? state.pendingLive
          : null,
        liveResult: null,
      };
    case "confirmation.changed":
      return {
        ...state,
        confirmed: action.confirmed,
        liveError: null,
      };
    case "live.requested":
      return {
        ...state,
        liveState: "submitting",
        liveError: null,
        pendingLive: action.request,
        liveResult: null,
      };
    case "live.received":
      return {
        ...state,
        confirmed: false,
        liveState: "completed",
        liveError: null,
        pendingLive: null,
        liveResult: action.result,
      };
    case "live.failed":
      return {
        ...state,
        liveState: "error",
        liveError: action.message,
      };
    default:
      return state;
  }
}

export function stableLiveReplayRequest(
  state: ReplayViewState,
  sourceTurnId: string,
  allocate: () => string,
): PendingLiveReplay {
  if (state.pendingLive?.sourceTurnId === sourceTurnId) return state.pendingLive;
  return { sourceTurnId, clientRequestId: allocate() };
}

export function hasCurrentLiveReplayAuthority(state: ReplayViewState): boolean {
  return state.mockState === "ready"
    && state.snapshot !== null
    && state.snapshot.live_replay_turn_ids.includes(state.selectedTurnId);
}

export function canSubmitLiveReplay(state: ReplayViewState): boolean {
  return hasCurrentLiveReplayAuthority(state)
    && state.confirmed
    && state.liveState !== "submitting";
}
