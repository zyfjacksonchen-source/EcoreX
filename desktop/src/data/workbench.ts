export type SessionStatus = "active" | "waiting" | "idle" | "failed";

export type Session = {
  id: string;
  name: string;
  status: SessionStatus;
  detail: string;
  updatedAt: string;
};

export const sessions: Session[] = [
  {
    id: "daily-report",
    name: "投放日报自动化",
    status: "waiting",
    detail: "等待确认：创建飞书日报草稿",
    updatedAt: "刚刚"
  },
  {
    id: "asset-sync",
    name: "飞书素材同步",
    status: "active",
    detail: "正在整理 3 个素材文件",
    updatedAt: "2 分钟前"
  },
  {
    id: "local-files",
    name: "本地文件整理",
    status: "idle",
    detail: "上次完成：归档周报附件",
    updatedAt: "18 分钟前"
  },
  {
    id: "campaign-audit",
    name: "投放报告分析",
    status: "idle",
    detail: "可继续：预算异常解释",
    updatedAt: "昨天"
  }
];

export const recentFiles = [
  { name: "ads-weekly.xlsx", meta: "已读取 · 2.4 MB" },
  { name: "creative-review.pdf", meta: "可预览 · 8 页" },
  { name: "daily-draft.md", meta: "EcoreX 生成" }
];

export const activeAgents = [
  { name: "日报整理", state: "等待确认" },
  { name: "网页搜索", state: "排队中" }
];
