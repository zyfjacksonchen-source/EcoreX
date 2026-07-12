import { BookmarkCheck, Wrench } from "lucide-react";

import type { ItemProjection, PublicToolActivity } from "../api/contracts.ts";

function statusLabel(status: string): string {
  return ({
    created: "已准备",
    in_progress: "进行中",
    waiting_human: "等待你确认",
    completed: "已完成",
    failed: "未完成",
    cancelled: "已取消",
  } satisfies Record<string, string>)[status] ?? "进行中";
}

export default function TimelineActivity({ item }: { item: ItemProjection }) {
  if (item.kind === "tool_call") {
    const activity = item.content as Partial<PublicToolActivity>;
    const displayLabel = typeof activity.display_label === "string"
      && activity.display_label.trim().length > 0
      ? activity.display_label
      : "工作步骤";
    const summary = typeof activity.result_summary === "string"
      ? activity.result_summary
      : typeof activity.argument_summary === "string"
      ? activity.argument_summary
      : displayLabel;
    return (
      <div className="ex-activity-row" role="status" title={summary}>
        <Wrench aria-hidden="true" />
        <span>{displayLabel}</span>
        <small>{statusLabel(item.status)}</small>
      </div>
    );
  }
  return (
    <div className="ex-activity-row is-checkpoint" role="status">
      <BookmarkCheck aria-hidden="true" />
      <span>已保存可继续位置</span>
      <small>{statusLabel(item.status)}</small>
    </div>
  );
}
