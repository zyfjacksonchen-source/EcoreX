import { ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";

import type { ItemProjection } from "../api/contracts.ts";

function duration(item: ItemProjection): number {
  const started = Date.parse(item.created_at);
  const ended = Date.parse(item.updated_at);
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return 1;
  return Math.max(1, Math.round((ended - started) / 1_000));
}

export default function ReasoningBlock({ item, label }: { item: ItemProjection; label: string }) {
  const done = item.content.presentation === "collapsed";
  const [open, setOpen] = useState(!done);
  useEffect(() => setOpen(!done), [done, item.item_id]);
  const text = typeof item.content.text === "string" ? item.content.text : "";
  return (
    <section className="ex-reasoning" data-state={done ? "done" : "thinking"}>
      {done ? (
        <button className="ex-button" type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          <span>思考了 {duration(item)} 秒</span><ChevronDown aria-hidden="true" />
        </button>
      ) : <div className="ex-reasoning-label" role="status" aria-live="polite">{label}</div>}
      <div className="ex-reasoning-collapsible" hidden={done && !open}>
        <p aria-live="off">{text}</p>
      </div>
    </section>
  );
}
