import { useRef, useSyncExternalStore } from "react";

import type { RuntimeTiming } from "../api/contracts.ts";

let tick = Date.now();
let timer: number | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    timer = window.setInterval(() => {
      tick = Date.now();
      listeners.forEach((notify) => notify());
    }, 250);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}

const idleSubscribe = () => () => undefined;
const snapshot = () => tick;

export function formatElapsed(milliseconds: number): string {
  const elapsed = Math.round(Math.max(0, milliseconds) / 1_000);
  if (elapsed < 60) return `${elapsed}s`;
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export default function OperationElapsed({
  timing,
  fallbackStartedAt,
  terminal,
  serverClockOffsetMs,
}: {
  timing: RuntimeTiming | null | undefined;
  fallbackStartedAt: string;
  terminal: boolean;
  serverClockOffsetMs: number;
}) {
  const now = useSyncExternalStore(terminal ? idleSubscribe : subscribe, snapshot, snapshot);
  const startedAt = Date.parse(timing?.started_at ?? fallbackStartedAt);
  const measured = timing?.duration_ms ?? Math.max(0, now + serverClockOffsetMs - startedAt);
  const maximum = useRef({ startedAt, elapsed: 0 });
  if (maximum.current.startedAt !== startedAt) maximum.current = { startedAt, elapsed: 0 };
  maximum.current.elapsed = terminal ? measured : Math.max(maximum.current.elapsed, measured);
  return <>{formatElapsed(maximum.current.elapsed)}</>;
}
