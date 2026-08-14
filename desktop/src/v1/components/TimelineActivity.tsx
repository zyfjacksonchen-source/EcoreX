import {
  BookmarkCheck,
  ChevronDown,
  FileSearch,
  Globe2,
  Image,
  Images,
  Link2,
  Puzzle,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import type { ReactNode } from "react";

import type { ItemProjection, PublicToolActivity } from "../api/contracts.ts";

function statusLabel(status: string, toolId?: string): string {
  if (toolId === "desktop_update" && status === "completed") return "已受理";
  return ({
    created: "已准备",
    in_progress: "进行中",
    waiting_human: "等待你确认",
    completed: "已完成",
    failed: "未完成",
    cancelled: "已取消",
  } satisfies Record<string, string>)[status] ?? "进行中";
}

export default function TimelineActivity({ item, elapsed }: { item: ItemProjection; elapsed: ReactNode }) {
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
    const icon = (() => {
      switch (activity.tool_id) {
        case "vision": return <Images aria-hidden="true" />;
        case "imagegen": return <Image aria-hidden="true" />;
        case "bash": return <TerminalSquare aria-hidden="true" />;
        case "browser": return <Globe2 aria-hidden="true" />;
        case "web_fetch": return <Link2 aria-hidden="true" />;
        case "read":
        case "artifact_read": return <FileSearch aria-hidden="true" />;
        case "skill_search":
        case "skill_read":
        case "tool_search":
        case "tool_describe": return <Puzzle aria-hidden="true" />;
        default: return <Wrench aria-hidden="true" />;
      }
    })();
    const effects = Array.isArray(activity.effects)
      ? activity.effects.map((effect) => ({
          read: "读取",
          write: "写入",
          network: "联网",
          execute: "执行",
          ui_automation: "浏览器操作",
          generate_media: "生成媒体",
        }[effect] ?? effect)).join(" · ")
      : "";
    if (activity.tool_id === "imagegen" && item.status === "in_progress") {
      return (
        <section className="ex-image-generation" aria-label="正在生成图片">
          <div className="ex-image-generation-canvas" role="img" aria-label="图片生成占位">
            <span className="ex-image-generation-glow" aria-hidden="true" />
            <Image aria-hidden="true" />
          </div>
          <p><strong>正在生成图片 {elapsed}</strong><span>{summary}</span></p>
        </section>
      );
    }
    const webSearch = activity.tool_id === "web_fetch" || activity.tool_id === "web_search";
    return (
      <details className={`ex-activity${webSearch ? " is-web-search" : ""}`} data-status={item.status}>
        <summary className="ex-activity-row">
          {icon}
          <span className={webSearch && item.status === "in_progress" ? "ex-activity-shimmer" : undefined}>{summary}</span>
          <small>{statusLabel(item.status, activity.tool_id)} {elapsed}</small>
          <ChevronDown className="ex-activity-chevron" aria-hidden="true" />
        </summary>
        <div className="ex-activity-detail">
          <span><b>步骤</b>{displayLabel}</span>
          {effects ? <span><b>范围</b>{effects}</span> : null}
          {activity.artifact_refs?.length ? (
            <span><b>产物</b>关联 {activity.artifact_refs.length} 个文件或图片</span>
          ) : null}
        </div>
      </details>
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
