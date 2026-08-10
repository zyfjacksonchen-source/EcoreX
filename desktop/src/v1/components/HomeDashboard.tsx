import { CalendarClock, ListChecks, PauseCircle, PlayCircle, Trash2 } from "lucide-react";
import type { ReactNode } from "react";

import type {
  ConversationUsageProjection,
  ProjectProjection,
  ThreadProjection,
} from "../api/contracts.ts";
import { homeTaskActivity } from "../state/homeTaskActivity.ts";
import NewConversationProjectSelector from "./NewConversationProjectSelector.tsx";

const SCHEDULE_ACTIONS = [
  [CalendarClock, "创建定时任务", "告诉小芯执行时间、内容和发送通道", "请帮我创建一个定时任务。先向我确认执行时间、任务内容和发送通道，再调用定时任务能力保存："],
  [ListChecks, "查看定时任务", "从 Runtime 读取当前任务，不在页面伪造状态", "请调用定时任务能力，列出我当前的全部定时任务和下次执行时间。"],
  [PauseCircle, "暂停定时任务", "选择任务后由 Runtime 立即停用", "请调用定时任务能力，先列出当前启用的定时任务，再让我选择要暂停的任务。"],
  [PlayCircle, "恢复定时任务", "选择任务后由 Runtime 重新启用", "请调用定时任务能力，先列出当前暂停的定时任务，再让我选择要恢复的任务。"],
  [Trash2, "删除定时任务", "删除前由小芯再次向你确认", "请调用定时任务能力，先列出当前定时任务，再让我选择要删除的任务；删除前必须再次确认。"],
] as const;

interface HomeDashboardProps {
  mode?: "home" | "schedules";
  composer: ReactNode;
  threads: readonly ThreadProjection[];
  projects: readonly ProjectProjection[];
  selectedProject: ProjectProjection | null;
  projectPickerBusy: boolean;
  usage: ConversationUsageProjection | null;
  onSelectProject: (project: ProjectProjection | null) => void;
  onPickProject: () => Promise<ProjectProjection | null>;
  onOpenThread: (threadId: string) => void;
  onTemplate: (prompt: string) => void;
}

export function HomeDashboard({
  mode = "home",
  composer,
  threads,
  projects,
  selectedProject,
  projectPickerBusy,
  usage,
  onSelectProject,
  onPickProject,
  onOpenThread,
  onTemplate,
}: HomeDashboardProps) {
  const taskActivity = homeTaskActivity(usage?.task_activity ?? {
    completed_today: 0,
    partial_today: 0,
    waiting: 0,
    terminal_today: 0,
    days: [],
  });
  const { completed, partial, waiting, terminal, trend } = taskActivity;
  const trendMaximum = Math.max(1, ...trend.map((item) => item.terminal));
  const recent = [...threads]
    .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
    .slice(0, 2);

  if (mode === "schedules") {
    return (
      <div className="ex-home-dashboard ex-home-dashboard-schedules" data-testid="schedules-workspace">
        <section className="ex-home-schedules" aria-labelledby="emate-schedules-title">
          <header>
            <div><small>Runtime Scheduler</small><h1 id="emate-schedules-title">定时任务</h1></div>
            <p>操作将在当前会话中执行，由 Runtime 返回真实结果。</p>
          </header>
          <div>
            {SCHEDULE_ACTIONS.map(([Icon, title, description, prompt]) => (
              <button className="ex-button ex-home-template" key={title} type="button" onClick={() => onTemplate(prompt)}>
                <Icon aria-hidden="true" />
                <span><strong>{title}</strong><small>{description}</small></span>
              </button>
            ))}
          </div>
          <p className="ex-home-schedule-note">此页面不缓存任务清单；查看、修改和执行状态始终以 Runtime 返回的事实为准。</p>
        </section>
      </div>
    );
  }

  return (
    <div className="ex-home-dashboard">
      <section className="ex-home-hero" aria-labelledby="emate-home-title">
        <div className="ex-home-hero-stage">
          <div className="ex-home-hero-image" role="img" aria-label="e-Mate 五位办公助手" />
        </div>
        <h1 id="emate-home-title">和<span>小芯</span>一起开始工作吧</h1>
        <p>告诉我的目标，我会帮你拆解步骤、调用工具、完成任务。</p>
      </section>

      <section className="ex-home-composer" aria-label="开始新任务">
        {composer}
        <div className="ex-home-project">
          <NewConversationProjectSelector
            compact
            projects={[...projects]}
            selectedProject={selectedProject}
            pickerBusy={projectPickerBusy}
            onSelect={onSelectProject}
            onPick={onPickProject}
          />
        </div>
      </section>

      <section className="ex-home-overview" aria-labelledby="emate-overview-title">
        <header>
          <h2 id="emate-overview-title">今日使用概览</h2>
          <span><i aria-hidden="true" />数据来自本机任务与 Token 记录</span>
        </header>
        <div className="ex-home-metrics">
          <article><small>完成任务数</small><strong>{completed}</strong><span>{partial ? `另有 ${partial} 项部分完成` : "今日完成"}</span></article>
          <article><small>等待任务</small><strong>{waiting}</strong><span>{waiting ? "等待小芯处理" : "当前无等待"}</span></article>
          <article><small>Token 消耗量</small><strong>{(usage?.today.total_tokens ?? 0).toLocaleString("zh-CN")}</strong><span>{usage?.data_quality.audit_continuity !== "complete" ? "审计连续性待核对" : usage?.complete_across_devices ? "账号统一可核对用量" : "本机可核对用量"}</span></article>
          <article><small>任务成功率</small><strong>{taskActivity.successRate}</strong><span>{terminal ? "按已结束任务计算" : "暂无已结束任务"}</span></article>
        </div>
        <div className="ex-home-report">
          <section>
            <h3>任务趋势（近 7 天）</h3>
            <div className="ex-home-trend" role="img" aria-label="近七日任务趋势">
              {trend.map((item) => (
                <span key={item.date}>
                  <i style={{ height: `${Math.max(4, (item.terminal / trendMaximum) * 100)}%` }} />
                  <small>{item.label}</small>
                </span>
              ))}
            </div>
          </section>
          <section>
            <h3>最近任务</h3>
            {recent.length ? (
              <ol className="ex-home-recent">
                {recent.map((thread) => (
                  <li key={thread.thread_id}>
                    <button className="ex-button" type="button" onClick={() => onOpenThread(thread.thread_id)}>
                      <span>{thread.title || "未命名任务"}</span>
                      <small>{thread.active_turn_status ? "进行中" : "已结束"}</small>
                    </button>
                  </li>
                ))}
              </ol>
            ) : <p>完成首个任务后会显示在这里。</p>}
          </section>
          <section>
            <h3>工作摘要</h3>
            <p>{terminal ? `今天已结束 ${terminal} 项任务，成功完成 ${completed} 项，部分完成 ${partial} 项。` : "今天还没有已结束的任务。告诉小芯目标即可开始。"}</p>
          </section>
        </div>
      </section>
    </div>
  );
}
