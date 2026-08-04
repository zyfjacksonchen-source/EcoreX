import { Check, Copy } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";

import type { TurnProjection } from "../api/contracts.ts";

function duration(turn: Pick<TurnProjection, "created_at" | "updated_at">): string {
  const elapsed = Math.max(0, Math.round((Date.parse(turn.updated_at) - Date.parse(turn.created_at)) / 1_000));
  if (!Number.isFinite(elapsed)) return "耗时未知";
  if (elapsed < 60) return `耗时 ${elapsed} 秒`;
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  if (minutes < 60) return seconds ? `耗时 ${minutes} 分 ${seconds} 秒` : `耗时 ${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `耗时 ${hours} 小时 ${minutes % 60} 分` : `耗时 ${hours} 小时`;
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
      <span>{duration(turn)}</span>
      {copyText.trim() ? <button className="ex-icon-button ex-turn-copy" type="button" aria-label={state === "copied" ? "回复已复制" : "复制本次回复"} title={state === "copied" ? "已复制" : "复制回复"} onClick={() => void copy()}>{state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}</button> : null}
      <span className="ex-turn-copy-notice" aria-live="polite">{state === "copied" ? "已复制" : state === "error" ? "复制失败" : ""}</span>
    </div>
  );
});
