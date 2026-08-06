import { Check, Copy } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";

import type { TurnProjection } from "../api/contracts.ts";

function duration(milliseconds: number | null | undefined): string | null {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0) return null;
  const elapsed = Math.round(milliseconds / 1_000);
  if (elapsed < 60) return `${elapsed}s`;
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function statusLabel(turn: TurnProjection): string {
  if (turn.status === "superseded") return "已替换";
  const label = turn.status === "completed"
    ? "已完成"
    : turn.status === "failed"
    ? "未完成"
    : turn.status === "cancelled"
    ? "已取消"
    : "已中断";
  const elapsed = duration(turn.timing?.duration_ms);
  return elapsed ? `${label} ${elapsed}` : label;
}

export default memo(function TurnCompletionRow({ turn, copyText }: { turn: TurnProjection; copyText: string }) {
  const [state, setState] = useState<"idle" | "copied" | "error">("idle");
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
  const copy = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(copyText);
      setState("copied");
    } catch { setState("error"); }
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 2_000);
  };
  return (
    <div className="ex-turn-completion" data-turn-status={turn.status}>
      <span>{statusLabel(turn)}</span>
      {copyText.trim() ? <button className="ex-icon-button ex-turn-copy" type="button" aria-label={state === "copied" ? "回复已复制" : "复制本次回复"} title={state === "copied" ? "已复制" : "复制回复"} onClick={() => void copy()}>{state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}</button> : null}
      <span className="ex-turn-copy-notice" aria-live="polite">{state === "copied" ? "已复制" : state === "error" ? "复制失败" : ""}</span>
    </div>
  );
});
