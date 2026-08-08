import { Check, ChevronDown, Circle, LoaderCircle, OctagonX } from "lucide-react";
import { useState } from "react";

import type { ItemProjection } from "../api/contracts.ts";

type TaskStatus = "pending" | "in_progress" | "completed";
type TaskEntry = { id: string; title: string; status: TaskStatus };

export default function TaskListBlock({
  item,
  interrupted,
}: {
  item: ItemProjection;
  interrupted: boolean;
}) {
  const [open, setOpen] = useState(false);
  const entries = Array.isArray(item.content.items)
    ? item.content.items.filter((entry): entry is TaskEntry => {
        if (!entry || typeof entry !== "object") return false;
        const value = entry as Partial<TaskEntry>;
        return typeof value.id === "string"
          && typeof value.title === "string"
          && ["pending", "in_progress", "completed"].includes(value.status ?? "");
      })
    : [];
  const completed = entries.filter((entry) => entry.status === "completed").length;

  return (
    <details
      className="ex-runtime-task-list is-composer"
      open={open}
      onPointerEnter={() => setOpen(true)}
      onPointerLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>任务清单</span>
        <small>{interrupted ? "任务已中断" : `${completed}/${entries.length} 已完成`}</small>
        <ChevronDown aria-hidden="true" />
      </summary>
      <div className="ex-runtime-task-list-content">
        <ol>
          {entries.map((entry) => (
            <li key={entry.id} data-status={entry.status}>
              {entry.status === "completed" ? <Check aria-hidden="true" /> : null}
              {entry.status === "in_progress" ? <LoaderCircle aria-hidden="true" /> : null}
              {entry.status === "pending" ? <Circle aria-hidden="true" /> : null}
              <span>{entry.title}</span>
            </li>
          ))}
        </ol>
        {interrupted ? <p><OctagonX aria-hidden="true" />未完成项目保持原状态</p> : null}
      </div>
    </details>
  );
}
