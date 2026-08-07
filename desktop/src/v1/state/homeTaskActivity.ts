import type { TaskActivityProjection } from "../api/contracts.ts";

export function homeTaskActivity(activity: TaskActivityProjection) {
  return {
    completed: activity.completed_today,
    partial: activity.partial_today,
    waiting: activity.waiting,
    terminal: activity.terminal_today,
    successRate: activity.terminal_today
      ? `${Math.round((activity.completed_today / activity.terminal_today) * 100)}%`
      : "暂无",
    trend: activity.days.map((day) => ({
      ...day,
      label: `${Number(day.date.slice(5, 7))}/${Number(day.date.slice(8, 10))}`,
    })),
  };
}
