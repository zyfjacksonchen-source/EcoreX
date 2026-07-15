import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertTriangle,
  CheckCircle2,
  FileClock,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import type {
  LiveReplayResponse,
  MockReplayResponse,
  ThreadProjection,
  TurnProjection,
} from "../api/contracts.ts";
import { createClientRequestId } from "../api/runtimeClient.ts";
import {
  canSubmitLiveReplay,
  initialReplayViewState,
  replayViewReducer,
  stableLiveReplayRequest,
} from "../state/replay.ts";
import { userFacingError } from "../state/userLanguage.ts";
import { IconButton } from "./IconButton.tsx";
import { TechnicalDetails } from "./TechnicalDetails.tsx";

interface ReplayDialogProps {
  open: boolean;
  thread: ThreadProjection | null;
  onOpenChange: (open: boolean) => void;
  onMockReplay: (
    threadId: string,
    signal?: AbortSignal,
  ) => Promise<MockReplayResponse>;
  onLiveReplay: (
    threadId: string,
    sourceTurnId: string,
    clientRequestId: string,
  ) => Promise<LiveReplayResponse>;
}

const TURN_STATUS_LABELS: Record<TurnProjection["status"], string> = {
  accepted: "已接收",
  queued: "排队中",
  preparing: "准备中",
  model_requested: "正在请求模型",
  streaming: "正在返回",
  tool_pending: "等待使用工具",
  waiting_human: "等待确认",
  tool_running: "正在使用工具",
  retry_wait: "等待重试",
  finalizing: "正在收尾",
  completed: "已完成",
  failed: "已失败",
  cancelled: "已取消",
  interrupted: "已中断",
  superseded: "已替代",
};

function formatError(error: unknown): string {
  return userFacingError(error);
}

function compactInput(value: string): string {
  const normalized = value.replaceAll(/\s+/g, " ").trim();
  return normalized.length > 44 ? `${normalized.slice(0, 44)}…` : normalized;
}

function replayCandidateLabel(turn: TurnProjection, index: number): string {
  const input = compactInput(turn.input) || "无文本输入";
  return `第 ${index + 1} 轮 · ${TURN_STATUS_LABELS[turn.status]} · ${input}`;
}

export function ReplayDialog({
  open,
  thread,
  onOpenChange,
  onMockReplay,
  onLiveReplay,
}: ReplayDialogProps) {
  const [state, dispatch] = useReducer(replayViewReducer, initialReplayViewState);
  const refreshButtonRef = useRef<HTMLButtonElement>(null);
  const threadId = thread?.thread_id ?? null;

  const loadSnapshot = useCallback(async (signal?: AbortSignal) => {
    if (!threadId) return;
    dispatch({ type: "mock.requested" });
    try {
      const snapshot = await onMockReplay(threadId, signal);
      if (signal?.aborted) return;
      dispatch({ type: "mock.received", snapshot });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      dispatch({ type: "mock.failed", message: formatError(error) });
    }
  }, [onMockReplay, threadId]);

  useEffect(() => {
    if (!open) {
      dispatch({ type: "dialog.reset" });
      return;
    }
    const controller = new AbortController();
    void loadSnapshot(controller.signal);
    return () => controller.abort();
  }, [loadSnapshot, open]);

  const candidates = useMemo(() => {
    if (!state.snapshot) return [];
    const byId = new Map(
      state.snapshot.projection.turns.map((turn) => [turn.turn_id, turn]),
    );
    return state.snapshot.live_replay_turn_ids
      .map((turnId) => byId.get(turnId))
      .filter((turn): turn is TurnProjection => turn !== undefined);
  }, [state.snapshot]);

  const submitLiveReplay = async () => {
    if (
      !threadId
      || !canSubmitLiveReplay(state)
    ) return;
    const request = stableLiveReplayRequest(
      state,
      state.selectedTurnId,
      () => createClientRequestId("live_replay"),
    );
    dispatch({ type: "live.requested", request });
    try {
      const result = await onLiveReplay(
        threadId,
        request.sourceTurnId,
        request.clientRequestId,
      );
      dispatch({ type: "live.received", result });
    } catch (error) {
      dispatch({ type: "live.failed", message: formatError(error) });
    }
  };

  const snapshot = state.snapshot;
  const liveSubmitting = state.liveState === "submitting";

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-replay-dialog"
          aria-describedby="ex-replay-description"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            refreshButtonRef.current?.focus();
          }}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>任务检查与重新运行</Dialog.Title>
              <Dialog.Description id="ex-replay-description">
                检查已保存的任务记录。这里只读取记录，不会运行模型、工具或外部操作。
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <IconButton label="关闭任务检查">
                <X aria-hidden="true" />
              </IconButton>
            </Dialog.Close>
          </div>

          <section className="ex-replay-section" aria-labelledby="ex-mock-replay-title">
            <div className="ex-replay-section-heading">
              <div>
                <h2 id="ex-mock-replay-title">任务记录检查</h2>
                <p>
                  从已保存的记录恢复当前视图，不会运行模型、工具、连接或外部写入。
                </p>
              </div>
              <button
                ref={refreshButtonRef}
                className="ex-button"
                type="button"
                disabled={!threadId}
                aria-disabled={state.mockState === "loading"}
                aria-busy={state.mockState === "loading"}
                onClick={() => {
                  if (state.mockState !== "loading") void loadSnapshot();
                }}
              >
                {state.mockState === "loading"
                  ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                  : <RefreshCw aria-hidden="true" />}
                {state.mockState === "loading" ? "正在检查" : "重新检查"}
              </button>
            </div>

            {state.mockState === "loading" && !snapshot ? (
              <div className="ex-replay-loading" role="status">
                <LoaderCircle className="ex-spin" aria-hidden="true" />
                <span>正在检查已保存的任务记录…</span>
              </div>
            ) : state.mockState === "error" && !snapshot ? (
              <div className="ex-replay-error" role="alert">
                <AlertTriangle aria-hidden="true" />
                <div>
                  <strong>未能完成记录检查</strong>
                  <span>{state.mockError}</span>
                </div>
              </div>
            ) : snapshot ? (
              <>
                <div className="ex-replay-verified" role="status">
                  <ShieldCheck aria-hidden="true" />
                  <div>
                    <strong>任务记录完整且可以恢复</strong>
                    <span>已检查 {snapshot.event_count} 条记录</span>
                  </div>
                </div>
                {state.mockError ? (
                  <p className="ex-replay-stale-warning" role="alert">
                    最近一次重新检查失败，下方仍保留上一份可用记录：{state.mockError}
                  </p>
                ) : null}
                <dl className="ex-replay-facts">
                  <div>
                    <dt>已检查记录</dt>
                    <dd>{snapshot.event_count}</dd>
                  </div>
                  <div>
                    <dt>工作步骤</dt>
                    <dd>{snapshot.projection.turns.length}</dd>
                  </div>
                  <div>
                    <dt>任务内容</dt>
                    <dd>{snapshot.projection.items.length}</dd>
                  </div>
                  <div>
                    <dt>待确认事项</dt>
                    <dd>{snapshot.interactions.length}</dd>
                  </div>
                </dl>
                <div className="ex-replay-projection-summary">
                  <span>任务摘要</span>
                  <strong>{snapshot.projection.thread.title || "未命名任务"}</strong>
                  <small>
                    任务状态：{snapshot.projection.thread.status === "active" ? "活跃" : "已归档"}
                    · 记录完整
                  </small>
                </div>
                <TechnicalDetails
                  summary="记录详情"
                  entries={[
                    { label: "记录摘要", value: snapshot.event_digest },
                    { label: "读取位置", value: String(snapshot.through_seq) },
                    { label: "来源位置", value: String(snapshot.source_watermark) },
                  ]}
                />
              </>
            ) : null}
          </section>

          <section className="ex-replay-section is-live" aria-labelledby="ex-live-replay-title">
            <div className="ex-replay-danger-heading">
              <AlertTriangle aria-hidden="true" />
              <div>
                <h2 id="ex-live-replay-title">重新运行</h2>
                <p>
                  从选中的历史步骤创建一个新的工作步骤，并按当前权限重新检查外部操作。
                </p>
              </div>
            </div>

            {snapshot && state.mockState !== "ready" ? (
              <p className="ex-replay-live-pending" role="status">
                上次检查的记录仍可查看，但只有重新检查成功后才能开始新的运行。
              </p>
            ) : snapshot && candidates.length === 0 ? (
              <div className="ex-replay-empty" role="status">
                <FileClock aria-hidden="true" />
                <div>
                  <strong>当前没有可重新运行的工作步骤</strong>
                  <span>只有已经结束并保存在当前任务中的步骤可以重新运行；从其他任务继承的历史仅供查看。</span>
                </div>
              </div>
            ) : snapshot ? (
              <div className="ex-replay-live-form">
                <label className="ex-replay-source" htmlFor="ex-replay-source-turn">
                  <span>从哪一步开始</span>
                  <select
                    id="ex-replay-source-turn"
                    value={state.selectedTurnId}
                    disabled={liveSubmitting}
                    onChange={(event) => dispatch({
                      type: "source.selected",
                      turnId: event.target.value,
                    })}
                  >
                    {candidates.map((turn, index) => (
                      <option value={turn.turn_id} key={turn.turn_id}>
                        {replayCandidateLabel(turn, index)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="ex-replay-confirm">
                  <input
                    type="checkbox"
                    checked={state.confirmed}
                    disabled={liveSubmitting}
                    onChange={(event) => dispatch({
                      type: "confirmation.changed",
                      confirmed: event.target.checked,
                    })}
                  />
                  <span>
                    <strong>我确认重新运行这一步</strong>
                    <small>
                      重新运行可能发起外部读写；EcoreX 会按当前权限重新询问，不会沿用历史批准。
                    </small>
                  </span>
                </label>

                {state.liveError ? (
                  <div className="ex-replay-error" role="alert">
                    <AlertTriangle aria-hidden="true" />
                    <div>
                      <strong>新的工作步骤未能建立</strong>
                      <span>{state.liveError} 再次尝试会延续同一次请求，不会重复创建工作步骤。</span>
                    </div>
                  </div>
                ) : null}

                {state.liveResult ? (
                  <div className="ex-replay-live-result" aria-live="polite">
                    <CheckCircle2 aria-hidden="true" />
                    <div>
                      <strong>新的工作步骤已加入当前任务</strong>
                      <span>{TURN_STATUS_LABELS[state.liveResult.replay.turn.status]}</span>
                      <TechnicalDetails
                        summary="运行详情"
                        entries={[
                          { label: "工作步骤 ID", value: state.liveResult.replay.turn.turn_id },
                          { label: "权限记录 ID", value: state.liveResult.permission_snapshot_id },
                        ]}
                      />
                    </div>
                  </div>
                ) : null}

                <div className="ex-replay-actions">
                  {state.liveResult ? (
                    <button
                      className="ex-button is-primary"
                      type="button"
                      onClick={() => onOpenChange(false)}
                    >
                      关闭并查看新结果
                    </button>
                  ) : (
                    <button
                      className="ex-button ex-replay-run"
                      type="button"
                      disabled={!canSubmitLiveReplay(state)}
                      aria-busy={liveSubmitting}
                      onClick={() => void submitLiveReplay()}
                    >
                      {liveSubmitting
                        ? <LoaderCircle className="ex-spin" aria-hidden="true" />
                        : <FileClock aria-hidden="true" />}
                      {liveSubmitting
                        ? "正在创建新步骤"
                        : state.liveState === "error"
                          ? "重试运行"
                          : "开始重新运行"}
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <p className="ex-replay-live-pending">请先完成上方记录检查，再选择可以重新运行的工作步骤。</p>
            )}
          </section>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
