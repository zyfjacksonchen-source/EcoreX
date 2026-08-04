import { FolderOpen, Sparkles, X } from "lucide-react";
import type { ReactNode } from "react";

import type {
  ConversationUsageProjection,
  ProjectProjection,
  ThreadProjection,
} from "../api/contracts.ts";
import { homeTaskActivity } from "../state/homeTaskActivity.ts";

const TEMPLATES = [
  ["图片创作", "把想法变成可直接使用的视觉素材", "帮我创作一张图片："],
  ["视频策划", "整理脚本、分镜和制作清单", "帮我策划一个视频，主题是："],
  ["文案写作", "起草、改写或润色办公内容", "帮我写一份文案，用途是："],
  ["品牌方案", "梳理定位、口径和品牌表达", "帮我制定一份品牌方案，背景是："],
  ["营销推广", "输出渠道计划和可执行素材", "帮我制定一份营销推广计划，目标是："],
  ["灵感探索", "从模糊想法快速形成可行方向", "围绕这个想法帮我发散并形成行动方案："],
] as const;

interface HomeDashboardProps {
  mode?: "home" | "creative";
  composer: ReactNode;
  threads: readonly ThreadProjection[];
  projects: readonly ProjectProjection[];
  selectedProject: ProjectProjection | null;
  projectPickerBusy: boolean;
  usage: ConversationUsageProjection | null;
  onSelectProject: (project: ProjectProjection | null) => void;
  onPickProject: () => void;
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
  onTemplate,
}: HomeDashboardProps) {
  const taskActivity = homeTaskActivity(usage?.task_activity ?? {
    completed_today: 0,
    waiting: 0,
    terminal_today: 0,
    days: [],
  });
  const { completed, waiting, terminal, trend } = taskActivity;
  const trendMaximum = Math.max(1, ...trend.map((item) => item.terminal));
  const recent = [...threads]
    .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
    .slice(0, 2);

  if (mode === "creative") {
    return (
      <div className="ex-home-dashboard ex-home-dashboard-creative">
        <section className="ex-home-creative" aria-labelledby="emate-creative-title">
          <header><div><small>Creative Center</small><h1 id="emate-creative-title">创意中心</h1></div><p>选择模板开始，已有草稿不会被覆盖。</p></header>
          <div>
            {TEMPLATES.map(([title, description, prompt]) => (
              <button className="ex-button ex-home-template" key={title} type="button" onClick={() => onTemplate(prompt)}>
                <Sparkles aria-hidden="true" />
                <span><strong>{title}</strong><small>{description}</small></span>
              </button>
            ))}
          </div>
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
          <FolderOpen aria-hidden="true" />
          <label className="ex-visually-hidden" htmlFor="emate-home-project">在项目中工作</label>
          <select
            id="emate-home-project"
            value={selectedProject?.project_id ?? ""}
            onChange={(event) => {
              const project = projects.find((item) => item.project_id === event.target.value);
              onSelectProject(project ?? null);
            }}
          >
            <option value="">在项目中工作</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>{project.name}</option>
            ))}
          </select>
          {selectedProject ? (
            <button className="ex-button ex-home-project-clear" type="button" aria-label="退出当前项目" onClick={() => onSelectProject(null)}>
              <X aria-hidden="true" />
            </button>
          ) : null}
          <button className="ex-button ex-home-project-button" type="button" disabled={projectPickerBusy} onClick={onPickProject}>
            {projectPickerBusy ? "正在选择" : "打开项目"}
          </button>
        </div>
      </section>

      <section className="ex-home-overview" aria-labelledby="emate-overview-title">
        <header>
          <h2 id="emate-overview-title">今日使用概览</h2>
          <span><i aria-hidden="true" />数据来自本机任务与 Token 记录</span>
        </header>
        <div className="ex-home-metrics">
          <article><small>完成任务数</small><strong>{completed}</strong><span>今日完成</span></article>
          <article><small>等待任务</small><strong>{waiting}</strong><span>{waiting ? "等待小芯处理" : "当前无等待"}</span></article>
          <article><small>Token 消耗量</small><strong>{(usage?.today.total_tokens ?? 0).toLocaleString("zh-CN")}</strong><span>{usage?.complete_across_devices ? "账号统一可核对用量" : "本机可核对用量"}</span></article>
          <article><small>任务成功率</small><strong>{taskActivity.successRate}</strong><span>{terminal ? "按已结束任务计算" : "暂无已结束任务"}</span></article>
        </div>
        <div className="ex-home-report">
          <section>
            <h3>任务趋势（近 7 天）</h3>
            <div className="ex-home-trend" aria-label="近七日任务趋势">
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
                    <span>{thread.title || "未命名任务"}</span>
                    <small>{thread.active_turn_status ? "进行中" : "已结束"}</small>
                  </li>
                ))}
              </ol>
            ) : <p>完成首个任务后会显示在这里。</p>}
          </section>
          <section>
            <h3>工作摘要</h3>
            <p>{terminal ? `今天已结束 ${terminal} 项任务，成功完成 ${completed} 项。` : "今天还没有已结束的任务。告诉小芯目标即可开始。"}</p>
          </section>
        </div>
      </section>
    </div>
  );
}
