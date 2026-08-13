/* EcoreX v1 operator usage analytics. */
function emptyUsageData() {
  const today = dateString(new Date());
  return {
    meta: {
      title: 'e-Mate 使用情况分析面板',
      range: `${today} 至 ${today}`,
      startDate: today,
      endDate: today,
      generatedAt: '',
      rawSheetUrl: '#',
      source: '等待实时数据',
      version: '1.0.5',
      productGeneration: 'all',
      live: false
    },
    kpis: {},
    users: [],
    dates: [today],
    scenarios: [],
    summaryRows: [],
    tasks: [],
    rawEvents: [],
    charts: { daily: [], users: [], scenarios: [] },
    insights: []
  };
}

let DATA = window.ECOREX_USAGE_DATA || emptyUsageData();
let liveRefreshSeq = 0;
let auditRefreshSeq = 0;

const icons = {
  external: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
  download: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
  table: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/></svg>',
  code: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/></svg>',
  refresh: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 5v4h4"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 19v-4h-4"/></svg>',
  rotate: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
  users: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  check: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>',
  clock: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
  alert: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  database: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg>',
  target: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>',
  calendar: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 2v4"/><path d="M16 2v4"/><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M3 10h18"/></svg>'
};

const colors = {
  blue: '#2563eb',
  green: '#16a34a',
  red: '#dc2626',
  orange: '#d97706',
  teal: '#0f766e',
  purple: '#7c3aed',
  slate: '#475569',
  gray: '#64748b'
};

const manualKey = 'ecorex-usage-panel-effective-artifacts-v1';
let manualArtifacts = readManualArtifacts();
const manualNoteKey = 'ecorex-usage-panel-manual-notes-v1';
let manualNotes = readManualNotes();
const auditState = {
  status: 'loading',
  data: null,
  error: ''
};

const statusOptions = ['成功', '部分完成', '失败', '中止', '进行中'];
const mainstreamCacheReferenceRate = 90;
const mainstreamCacheReferenceText = 'Artificial Analysis 公开编码 Agent 榜单显示 Claude Code 96%、Cursor CLI 89%，面板取 90% 做对标线';
const scenarioDefinitions = {
  '创作内容': '创作文案/标题/报告/脚本等',
  '制作素材': '制作图片、海报图片编辑等',
  '搜索查询': '网页搜索抓取等',
  '处理数据': '数据处理（Excel/word/ppt/pdf）等',
  '编辑文档': '操作在线文档（飞书/腾讯文档）等',
  '交付通知': '打包/展示/发消息/提醒等',
  '系统维护': '环境检查/排错/配置等'
};
const statusOptionMeta = {
  '成功': {
    label: '成功',
    desc: '这一天这个用户有成功完成的任务；只要成功任务数大于 0，就会出现在这里。'
  },
  '部分完成': {
    label: '部分完成',
    desc: '任务已返回结果，但至少一个工具步骤失败或取消。'
  },
  '失败': {
    label: '失败',
    desc: '已接收但未成功，且不是用户主动停止的任务。'
  },
  '中止': {
    label: '中止',
    desc: '用户主动停止的任务；不再混入失败。'
  },
  '进行中': {
    label: '进行中',
    desc: '任务已接收，但还没有终态结果。'
  }
};
const initialDateRange = defaultDateRange();

function dateString(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function dateFromString(value) {
  const [year, month, day] = String(value || '').split('-').map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
}

function addDays(value, days) {
  const date = dateFromString(value);
  if (!date) return '';
  date.setDate(date.getDate() + days);
  return dateString(date);
}

function defaultDateRange() {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - 6);
  return { start: dateString(start), end: dateString(end) };
}

const state = {
  metricView: 'tasks',
  productGeneration: DATA.meta.productGeneration || 'all',
  dateRange: initialDateRange,
  auditFilters: {
    userEmail: '',
    start: initialDateRange.start,
    end: initialDateRange.end
  },
  users: new Set(DATA.users),
  dates: new Set(DATA.dates),
  scenarios: new Set(DATA.scenarios),
  states: new Set(statusOptions),
  eventTypes: new Set(rawEventTypeOptions()),
  resultClasses: new Set(rawResultClassOptions()),
  expandedUsers: new Set(),
  expandedFailures: new Set()
};

function $(selector) {
  return document.querySelector(selector);
}

function number(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

function pct(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function minutes(value) {
  return value == null ? '-' : `${Number(value).toFixed(2)} 分钟`;
}

function tokenNumber(value) {
  const amount = Number(value || 0);
  const abs = Math.abs(amount);
  const format = (scaled) => {
    const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    return Number(scaled.toFixed(digits)).toLocaleString('zh-CN', {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits
    });
  };
  if (abs >= 1000000) return `${format(amount / 1000000)}M`;
  if (abs >= 1000) return `${format(amount / 1000)}K`;
  return amount.toLocaleString('zh-CN');
}

function tokenRate(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function cacheHitRate(stats) {
  const basis = Number((stats && (stats.cacheInputTokens || stats.inputTokens)) || 0);
  return basis ? Number((Number(stats.cacheReadTokens || 0) / basis * 100).toFixed(1)) : 0;
}

function cacheReportedLabel(stats) {
  const records = Number((stats && stats.cacheReportedRecords) || 0);
  return records ? `${number(records)} 条有缓存上报` : '当前未上报缓存字段';
}

function serverArtifactCount(row) {
  return Number(row && (row.effectiveArtifacts || row.autoArtifactEvents) || 0);
}

function artifactValueForRow(row) {
  if (Object.prototype.hasOwnProperty.call(manualArtifacts, row.id)) {
    return manualArtifacts[row.id];
  }
  const auto = serverArtifactCount(row);
  return auto ? String(auto) : '';
}

function artifactCountForRow(row) {
  const value = artifactValueForRow(row);
  return Number(value || 0);
}

function readManualArtifacts() {
  try {
    return JSON.parse(localStorage.getItem(manualKey) || '{}');
  } catch {
    return {};
  }
}

function saveManualArtifacts() {
  localStorage.setItem(manualKey, JSON.stringify(manualArtifacts));
}

function readManualNotes() {
  try {
    return JSON.parse(localStorage.getItem(manualNoteKey) || '{}');
  } catch {
    return {};
  }
}

function saveManualNotes() {
  localStorage.setItem(manualNoteKey, JSON.stringify(manualNotes));
}

function rowState(row) {
  if (row.successTasks > 0) return '成功';
  if (partialTasksForRow(row) > 0) return '部分完成';
  if (stoppedTasksForRow(row) > 0) return '中止';
  if (failedTasksForRow(row) > 0) return '失败';
  if (runningTasksForRow(row) > 0) return '进行中';
  return '无任务';
}

function partialTasksForRow(row) {
  return Number(row && row.partialTasks || 0) || 0;
}

function runningTasksForRow(row) {
  if (row && row.runningTasks != null) return Number(row.runningTasks || 0);
  return 0;
}

function stoppedTasksForRow(row) {
  return Number(row && (row.stoppedTasks || row.cancelledTasks || 0)) || 0;
}

function failedTasksForRow(row) {
  if (row && row.failedTasks != null) return Number(row.failedTasks || 0);
  return Math.max(0, Number(row && row.totalTasks || 0) - Number(row && row.successTasks || 0) - partialTasksForRow(row) - stoppedTasksForRow(row) - Number(row && row.runningTasks || 0));
}

function invalidArtifactsForRow(row) {
  return Number(row && (row.invalidArtifacts || row.thumbsDownArtifacts || 0)) || 0;
}

function taskStatus(task) {
  return task.statusCategory || (task.success ? '成功' : (task.cancelled || task.rawStatus === 'cancelled' ? '中止' : '失败'));
}

function scenarioDefinition(name) {
  return scenarioDefinitions[name] || '按当前任务工具和内容自动归类。';
}

function scenarioDetailText(name, value, total) {
  return `${name}：${scenarioDefinition(name)}，占当前筛选内容的 ${pct(value / total * 100)}`;
}

function rowMatches(row) {
  const failedTasks = failedTasksForRow(row);
  const stoppedTasks = stoppedTasksForRow(row);
  const stateTags = [];
  if (row.successTasks > 0) stateTags.push('成功');
  if (partialTasksForRow(row) > 0) stateTags.push('部分完成');
  if (failedTasks > 0) stateTags.push('失败');
  if (stoppedTasks > 0) stateTags.push('中止');
  if (runningTasksForRow(row) > 0) stateTags.push('进行中');
  const allScenariosSelected = state.scenarios.size === DATA.scenarios.length;
  const scenarioMatches = row.mainScenario === '无'
    ? allScenariosSelected
    : [...state.scenarios].some((scenario) => row.mainScenario.includes(scenario));
  const allStatesSelected = state.states.size === statusOptions.length;
  const statusMatches = stateTags.length === 0
    ? allStatesSelected
    : stateTags.some((tag) => state.states.has(tag));
  return state.users.has(row.user)
    && state.dates.has(row.date)
    && statusMatches
    && scenarioMatches;
}

function applyStatusFilterToRow(row) {
  const wantsSuccess = state.states.has('成功');
  const wantsPartial = state.states.has('部分完成');
  const wantsFailed = state.states.has('失败');
  const wantsStopped = state.states.has('中止');
  const wantsRunning = state.states.has('进行中');
  const partialTasks = partialTasksForRow(row);
  const failedTasks = failedTasksForRow(row);
  const stoppedTasks = stoppedTasksForRow(row);
  const runningTasks = runningTasksForRow(row);
  if (wantsSuccess && wantsPartial && wantsFailed && wantsStopped && wantsRunning) return row;
  const selectedTotal = (wantsSuccess ? row.successTasks : 0) + (wantsPartial ? partialTasks : 0) + (wantsFailed ? failedTasks : 0) + (wantsStopped ? stoppedTasks : 0) + (wantsRunning ? runningTasks : 0);
  const selectedTerminal = selectedTotal - (wantsRunning ? runningTasks : 0);
  if (selectedTotal > 0) {
    return {
      ...row,
      totalTasks: selectedTotal,
      successTasks: wantsSuccess ? row.successTasks : 0,
      partialTasks: wantsPartial ? partialTasks : 0,
      failedTasks: wantsFailed ? failedTasks : 0,
      stoppedTasks: wantsStopped ? stoppedTasks : 0,
      runningTasks: wantsRunning ? runningTasks : 0,
      terminalTasks: selectedTerminal,
      successRate: selectedTerminal ? (wantsSuccess ? row.successTasks : 0) / selectedTerminal * 100 : 0,
      avgCompletionMinutes: wantsSuccess ? row.avgCompletionMinutes : null,
      interventionCount: Math.min(row.interventionCount || 0, selectedTotal),
      interventionRate: selectedTotal ? Math.min(row.interventionCount || 0, selectedTotal) / selectedTotal * 100 : 0,
      remarks: `仅显示${[wantsSuccess && '成功', wantsPartial && '部分完成', wantsFailed && '失败', wantsStopped && '中止', wantsRunning && '进行中'].filter(Boolean).join('、')}任务`
    };
  }
  return { ...row, totalTasks: 0, successTasks: 0, partialTasks: 0, failedTasks: 0, stoppedTasks: 0, runningTasks: 0, terminalTasks: 0, successRate: 0, avgCompletionMinutes: null };
}

function filteredRows() {
  return DATA.summaryRows
    .filter(rowMatches)
    .map(applyStatusFilterToRow);
}

function filteredTasks() {
  return DATA.tasks.filter((task) => (
    state.users.has(task.user)
    && state.dates.has(task.date)
    && state.scenarios.has(task.scenario)
    && state.states.has(taskStatus(task))
  ));
}

function rawEventsForSelection() {
  return DATA.rawEvents.filter((event) => (
    state.users.has(event.user)
    && state.dates.has(event.date)
  ));
}

function filteredRawEvents() {
  return rawEventsForSelection().filter((event) => (
    state.eventTypes.has(event.eventType)
    && state.resultClasses.has(event.resultClass)
  ));
}

function tokenValue(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const cleaned = value.replace(/,/g, '').trim();
    if (/^\d+(\.\d+)?$/.test(cleaned)) return Number(cleaned);
  }
  return 0;
}

function firstTokenValue(source, keys) {
  if (!source) return 0;
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(source, key)) {
      const value = tokenValue(source[key]);
      if (value) return value;
    }
  }
  return 0;
}

function matchTokenValue(text, patterns) {
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return tokenValue(match[1]);
  }
  return 0;
}

function tokensFromDetail(detail) {
  const text = String(detail || '');
  const input = matchTokenValue(text, [
    /(?:input[_\s-]?tokens?|prompt[_\s-]?tokens?)["'：:\s=]+([0-9][0-9,]*)/i,
    /(?:输入|提示词|请求)(?:\s*Token|\s*tokens?)?[^0-9]{0,18}([0-9][0-9,]*)/i
  ]);
  const output = matchTokenValue(text, [
    /(?:output[_\s-]?tokens?|completion[_\s-]?tokens?)["'：:\s=]+([0-9][0-9,]*)/i,
    /(?:输出|回答|生成)(?:\s*Token|\s*tokens?)?[^0-9]{0,18}([0-9][0-9,]*)/i
  ]);
  const total = matchTokenValue(text, [
    /(?:total[_\s-]?tokens?)["'：:\s=]+([0-9][0-9,]*)/i,
    /(?:总计|总量|总)(?:\s*Token|\s*tokens?)?[^0-9]{0,18}([0-9][0-9,]*)/i
  ]) || input + output;
  const hasUsage = /包含用量信息：是|hasUsage["'：:\s=]+true/i.test(text) || total > 0 || input > 0 || output > 0;
  const explicitNoUsage = /包含用量信息：否|hasUsage["'：:\s=]+false/i.test(text);
  return { input, output, total, hasUsage, explicitNoUsage };
}

function tokenUsageFromSource(source) {
  const input = firstTokenValue(source, ['inputTokens', 'input_tokens', 'promptTokens', 'prompt_tokens', 'promptTokenCount']);
  const output = firstTokenValue(source, ['outputTokens', 'output_tokens', 'completionTokens', 'completion_tokens', 'completionTokenCount']);
  const total = firstTokenValue(source, ['totalTokens', 'total_tokens', 'tokens', 'tokenCount']) || input + output;
  const merged = {
    input,
    output,
    total,
    hasUsage: Boolean(source && source.hasUsage),
    explicitNoUsage: Boolean(source && source.noUsageFlag)
  };
  if (!merged.total) merged.total = merged.input + merged.output;
  return merged;
}

function tokenUsageForTask(task) {
  const direct = tokenUsageFromSource(task);
  return {
    ...direct,
    hasTokens: direct.total > 0 || direct.input > 0 || direct.output > 0
  };
}

function summarizeTokenTasks(tasks) {
  return tasks.reduce((acc, task) => {
    const usage = tokenUsageForTask(task);
    acc.taskCount += 1;
    acc.inputTokens += usage.input;
    acc.outputTokens += usage.output;
    acc.totalTokens += usage.total || usage.input + usage.output;
    acc.cacheInputTokens += usage.input;
    if (usage.hasTokens) acc.reportedTasks += 1;
    else acc.missingTasks += 1;
    if (usage.explicitNoUsage) acc.noUsageFlagTasks += 1;
    return acc;
  }, {
    taskCount: 0,
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    cacheInputTokens: 0,
    cacheReportedRecords: 0,
    reportedTasks: 0,
    missingTasks: 0,
    noUsageFlagTasks: 0
  });
}

function tokenTasksForRow(row) {
  return filteredTasks().filter(task => task.user === row.user && task.date === row.date);
}

function tokenStatsFromRow(row) {
  const directTotal = Number(row.totalTokens || 0);
  const directInput = Number(row.inputTokens || 0);
  const directOutput = Number(row.outputTokens || 0);
  const recordCount = Number(row.tokenUsageRecords || row.tokenUsageTasks || 0);
  const cacheInput = Number(row.cacheInputTokens || 0) || directInput;
  const cacheRead = Number(row.cacheReadTokens || 0);
  return {
    recordCount,
    taskCount: recordCount,
    inputTokens: directInput,
    outputTokens: directOutput,
    totalTokens: directTotal || directInput + directOutput,
    cacheReadTokens: cacheRead,
    cacheWriteTokens: Number(row.cacheWriteTokens || 0),
    cacheInputTokens: cacheInput,
    cacheHitRate: cacheInput ? cacheRead / cacheInput * 100 : 0,
    cacheReportedRecords: Number(row.cacheReportedRecords || 0),
    estimatedRecords: Number(row.tokenEstimatedRecords || 0),
    reportedTasks: recordCount,
    missingTasks: 0,
    noUsageFlagTasks: 0,
    modelText: row.tokenModels || '-',
    sourceText: row.tokenSources || '-'
  };
}

function tokenRowMatches(row) {
  return state.users.has(row.user)
    && state.dates.has(row.date);
}

function filteredTokenRows() {
  return DATA.summaryRows
    .filter(tokenRowMatches)
    .map(row => ({ ...row, tokenStats: tokenStatsFromRow(row) }));
}

function aggregateTokenRows(rows) {
  return rows.reduce((acc, row) => {
    const stats = row.tokenStats || tokenStatsFromRow(row);
    acc.taskCount += stats.taskCount;
    acc.recordCount += stats.recordCount;
    acc.inputTokens += stats.inputTokens;
    acc.outputTokens += stats.outputTokens;
    acc.totalTokens += stats.totalTokens;
    acc.cacheReadTokens += stats.cacheReadTokens || 0;
    acc.cacheWriteTokens += stats.cacheWriteTokens || 0;
    acc.cacheInputTokens += stats.cacheInputTokens || stats.inputTokens || 0;
    acc.cacheReportedRecords += stats.cacheReportedRecords || 0;
    acc.estimatedRecords += stats.estimatedRecords;
    acc.reportedTasks += stats.reportedTasks;
    acc.missingTasks += stats.missingTasks;
    acc.noUsageFlagTasks += stats.noUsageFlagTasks;
    return acc;
  }, {
    taskCount: 0,
    recordCount: 0,
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    cacheInputTokens: 0,
    cacheReportedRecords: 0,
    estimatedRecords: 0,
    reportedTasks: 0,
    missingTasks: 0,
    noUsageFlagTasks: 0
  });
}

function failureTasksFor(user, date = null) {
  return DATA.tasks.filter((task) => (
    task.user === user
    && (!date || task.date === date)
    && state.dates.has(task.date)
    && state.scenarios.has(task.scenario)
    && taskStatus(task) !== '成功'
  ));
}

function failureReasonForTask(task) {
  const status = taskStatus(task);
  const events = DATA.rawEvents.filter((event) => (
    event.user === task.user
    && event.date === task.date
    && event.requestId === task.requestId
  ));
  const problemEvents = events.filter((event) => (
    event.resultClass === '失败事件'
    || event.resultClass === '受限事件'
    || ['已取消', '失败', '受限'].includes(event.status)
  ));
  if (problemEvents.length) {
    return problemEvents.map((event) => {
      const detail = event.detail ? `；${event.detail}` : '';
      return `${event.time} ${event.eventType}，状态 ${event.status}${detail}`;
    }).join('；');
  }
  if (status === '中止') return '用户主动停止任务';
  return '没有看到完成记录，按未成功完成计为失败';
}

function rawEventTypeOptions() {
  return [...new Set(DATA.rawEvents.map(event => event.eventType).filter(Boolean))].sort();
}

function rawResultClassOptions() {
  return [...new Set(DATA.rawEvents.map(event => event.resultClass).filter(Boolean))].sort();
}

function aggregateRows(rows) {
  const totals = rows.reduce((acc, row) => {
    acc.totalTasks += row.totalTasks;
    acc.successTasks += row.successTasks;
    acc.partialTasks += partialTasksForRow(row);
    acc.failedTasks += failedTasksForRow(row);
    acc.stoppedTasks += stoppedTasksForRow(row);
    acc.runningTasks += runningTasksForRow(row);
    acc.interventionCount += row.interventionCount;
    acc.effectiveArtifacts += artifactCountForRow(row);
    acc.invalidArtifacts += invalidArtifactsForRow(row);
    return acc;
  }, { totalTasks: 0, successTasks: 0, partialTasks: 0, failedTasks: 0, stoppedTasks: 0, runningTasks: 0, interventionCount: 0, effectiveArtifacts: 0, invalidArtifacts: 0 });
  const tasks = filteredTasks();
  const rawEvents = rawEventsForSelection();
  const durations = tasks.filter(t => t.durationMinutes != null).map(t => t.durationMinutes);
  totals.rawEvents = rawEvents.length;
  if (!totals.failedTasks) totals.failedTasks = Math.max(0, totals.totalTasks - totals.successTasks - totals.partialTasks - totals.stoppedTasks - totals.runningTasks);
  totals.terminalTasks = totals.successTasks + totals.partialTasks + totals.failedTasks + totals.stoppedTasks;
  totals.avgCompletionMinutes = durations.length
    ? durations.reduce((sum, item) => sum + item, 0) / durations.length
    : 0;
  totals.successRate = totals.terminalTasks ? totals.successTasks / totals.terminalTasks * 100 : 0;
  totals.interventionRate = totals.totalTasks ? totals.interventionCount / totals.totalTasks * 100 : 0;
  return totals;
}

function mountIcons() {
  document.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icons[node.dataset.icon] || '';
  });
}

function filterOptionMeta(value, setName) {
  if (setName === 'states') return statusOptionMeta[value] || { label: value, desc: '' };
  if (setName === 'eventTypes') return { label: value, desc: '后台记录的一类动作，比如任务接收、任务完成、工具执行。' };
  if (setName === 'resultClasses') {
    const descriptions = {
      '成功事件': '这条记录表示某个动作完成了。',
      '失败事件': '这条记录表示某个动作失败或被取消。',
      '受限事件': '这条记录表示触发了数量或权限一类限制。',
      '过程事件': '这条记录只是过程动作，不直接代表成功或失败。'
    };
    return { label: value, desc: descriptions[value] || '按这条记录的结果类型筛选明细。' };
  }
  return { label: value, desc: '' };
}

function updateMetaLine() {
  const parts = [DATA.meta.range, DATA.meta.source, DATA.meta.generatedAt ? `生成 ${DATA.meta.generatedAt}` : ''].filter(Boolean);
  $('#metaLine').textContent = parts.join(' · ');
}

function setLiveStatus(text, tone = '') {
  const node = $('#liveStatus');
  if (!node) return;
  node.textContent = text;
  node.className = `live-status ${tone}`.trim();
}

function syncRangeInputs() {
  const startInput = $('#rangeStart');
  const endInput = $('#rangeEnd');
  if (startInput) startInput.value = state.dateRange.start;
  if (endInput) endInput.value = state.dateRange.end;
  const generation = $('#productGeneration');
  if (generation) generation.value = state.productGeneration;
}

function rangeFromInputs() {
  const start = $('#rangeStart')?.value || state.dateRange.start;
  const end = $('#rangeEnd')?.value || state.dateRange.end;
  if (!dateFromString(start) || !dateFromString(end)) {
    setLiveStatus('请选择完整的开始日期和结束日期', 'error');
    return null;
  }
  if (start > end) {
    setLiveStatus('开始日期不能晚于结束日期', 'error');
    return null;
  }
  return { start, end };
}

function setSelectedValues(setName, values, keepExisting = false) {
  const current = state[setName] ? [...state[setName]] : [];
  const allowed = new Set(values);
  let next = keepExisting ? current.filter(value => allowed.has(value)) : values;
  if (keepExisting && current.length > 0 && next.length === 0) next = values;
  state[setName] = new Set(next);
}

function syncSelectionsWithData(keepExisting = false) {
  setSelectedValues('users', DATA.users, keepExisting);
  setSelectedValues('dates', DATA.dates, keepExisting);
  setSelectedValues('scenarios', DATA.scenarios, keepExisting);
  setSelectedValues('states', statusOptions, keepExisting);
  setSelectedValues('eventTypes', rawEventTypeOptions(), keepExisting);
  setSelectedValues('resultClasses', rawResultClassOptions(), keepExisting);
}

function allSelected(values, selected) {
  return values.length === selected.size && values.every(value => selected.has(value));
}

function renderKpis(rows) {
  if (state.metricView === 'tokens') {
    renderTokenKpis(filteredTokenRows());
    return;
  }
  const a = aggregateRows(rows);
  const cards = [
    { label: '总任务数', value: number(a.totalTasks), foot: `由 ${number(a.rawEvents)} 条事件整理`, icon: 'target', color: colors.blue, tip: `任务：用户发起一次需求并被系统接收，就算 1 个任务；同一个任务过程里会有多条事件。` },
    { label: '成功任务数', value: number(a.successTasks), foot: `已结束成功率 ${pct(a.successRate)}`, icon: 'check', color: colors.green, tip: `成功任务没有失败的工具步骤；成功率按已结束任务计算，进行中任务不进入分母。` },
    { label: '部分完成', value: number(a.partialTasks), foot: '有工具步骤未完成', icon: 'alert', color: colors.orange, tip: '任务返回了结果，但至少一个工具步骤失败或取消；模型文字兜底不会把它改成成功。' },
    { label: '失败任务数', value: number(a.failedTasks), foot: `失败率 ${pct(a.totalTasks ? a.failedTasks / a.totalTasks * 100 : 0)}`, icon: 'alert', color: colors.red, tip: `失败任务：已接收但未成功，且不是用户主动停止的任务。` },
    { label: '中止任务数', value: number(a.stoppedTasks), foot: `中止率 ${pct(a.totalTasks ? a.stoppedTasks / a.totalTasks * 100 : 0)}`, icon: 'rotate', color: colors.orange, tip: `中止任务：用户主动停止的任务，不再混入失败。` },
    { label: '进行中', value: number(a.runningTasks), foot: '不计入成功率', icon: 'clock', color: colors.slate, tip: '已接收但尚未产生终态的任务。' },
    { label: '有效产物', value: number(a.effectiveArtifacts), foot: `无效 ${number(a.invalidArtifacts)} 个`, icon: 'table', color: colors.teal, tip: `有效产物：未被用户勾选下拇指且状态可用的产物；用户勾选 👎 定义为无效产物。` },
    { label: '涉及用户', value: number(new Set(rows.map(r => r.user)).size), foot: `RAW ${number(a.rawEvents)} 条`, icon: 'users', color: colors.purple, tip: `当前筛选里出现过记录的用户数；RAW 是后台事件明细。` }
  ];
  $('#kpiGrid').innerHTML = cards.map(card => `
    <article class="kpi" data-tip="${escapeAttr(card.tip)}" title="${escapeAttr(card.tip)}">
      <div class="kpi-top">
        <span>${card.label}</span>
        <span class="kpi-mark" style="background:${card.color}">${icons[card.icon]}</span>
      </div>
      <div class="kpi-value">${card.value}</div>
      <div class="kpi-foot">${card.foot}</div>
    </article>
  `).join('');
}

function renderTokenKpis(rows) {
  const a = aggregateTokenRows(rows);
  const activeUsers = new Set(rows.map(row => row.user)).size;
  const currentCacheRate = cacheHitRate(a);
  const cards = [
    { label: '总 Token', value: tokenNumber(a.totalTokens), foot: '服务端合并账本', icon: 'database', color: colors.blue, tip: '总 Token 来自旧版用量事实与 v1 Gateway 终态事实的服务端去重投影，不解析任务说明文字。' },
    { label: '输入 Token', value: tokenNumber(a.inputTokens), foot: '提供商实报汇总', icon: 'target', color: colors.teal, tip: '输入 Token 来自提供商完成事实，按请求 ID 幂等去重。' },
    { label: '输出 Token', value: tokenNumber(a.outputTokens), foot: '提供商实报汇总', icon: 'check', color: colors.green, tip: '输出 Token 来自提供商完成事实，按请求 ID 幂等去重。' },
    { label: '缓存命中率', value: tokenRate(currentCacheRate), foot: `参考线 ${tokenRate(mainstreamCacheReferenceRate)}`, icon: 'clock', color: currentCacheRate >= mainstreamCacheReferenceRate ? colors.green : colors.orange, tip: `缓存命中率 = 缓存命中输入 Token / 输入 Token。当前命中 ${tokenNumber(a.cacheReadTokens)}，输入 ${tokenNumber(a.cacheInputTokens || a.inputTokens)}；${cacheReportedLabel(a)}。${mainstreamCacheReferenceText}。` },
    { label: '用量记录数', value: number(a.recordCount), foot: `${number(a.estimatedRecords)} 条历史估算`, icon: 'table', color: colors.purple, tip: '用量记录数为服务端合并后的唯一用量事实数；历史旧版记录若只能估算会明确标记。' },
    { label: '涉及用户', value: number(activeUsers), foot: '按当前筛选', icon: 'users', color: colors.red, tip: '当前筛选下有 Token 用量记录的用户数。' }
  ];
  $('#kpiGrid').innerHTML = cards.map(card => `
    <article class="kpi" data-tip="${escapeAttr(card.tip)}" title="${escapeAttr(card.tip)}">
      <div class="kpi-top">
        <span>${card.label}</span>
        <span class="kpi-mark" style="background:${card.color}">${icons[card.icon]}</span>
      </div>
      <div class="kpi-value">${card.value}</div>
      <div class="kpi-foot">${card.foot}</div>
    </article>
  `).join('');
}

function makeMultiSelect(root, title, values, setName) {
  const selected = state[setName];
  root.innerHTML = `
    <div class="filter">
      <button class="filter-button" type="button">
        <span>${title} · <b data-filter-count>${selected.size}</b></span>
        <span>▾</span>
      </button>
      <div class="filter-menu">
        <div class="filter-actions">
          <button class="mini" type="button" data-action="all">全选</button>
          <button class="mini" type="button" data-action="none">清空</button>
        </div>
        ${values.map(value => `
          <label class="check-row ${setName === 'states' ? 'state-row' : ''}" title="${escapeAttr(filterOptionMeta(value, setName).desc || value)}">
            <input type="checkbox" value="${escapeAttr(value)}" ${selected.has(value) ? 'checked' : ''}>
            <span class="check-copy">
              <span class="check-main">${escapeHtml(filterOptionMeta(value, setName).label)}</span>
              ${filterOptionMeta(value, setName).desc ? `<span class="check-desc">${escapeHtml(filterOptionMeta(value, setName).desc)}</span>` : ''}
            </span>
          </label>
        `).join('')}
      </div>
    </div>
  `;
  const filter = root.querySelector('.filter');
  const updateFilterUi = () => {
    filter.querySelector('[data-filter-count]').textContent = selected.size;
    filter.querySelectorAll('input').forEach(input => {
      input.checked = selected.has(input.value);
    });
  };
  filter.querySelector('.filter-button').addEventListener('click', () => {
    document.querySelectorAll('.filter.open').forEach((item) => {
      if (item !== filter) item.classList.remove('open');
    });
    filter.classList.toggle('open');
  });
  filter.querySelectorAll('input').forEach(input => {
    input.addEventListener('change', () => {
      if (input.checked) selected.add(input.value);
      else selected.delete(input.value);
      updateFilterUi();
      refresh();
    });
  });
  filter.querySelector('[data-action="all"]').addEventListener('click', (event) => {
    event.preventDefault();
    values.forEach(value => selected.add(value));
    updateFilterUi();
    refresh();
  });
  filter.querySelector('[data-action="none"]').addEventListener('click', (event) => {
    event.preventDefault();
    selected.clear();
    updateFilterUi();
    refresh();
  });
}

function renderFilters() {
  makeMultiSelect($('#filterUsers'), '用户', DATA.users, 'users');
  makeMultiSelect($('#filterDates'), '日期', DATA.dates, 'dates');
  makeMultiSelect($('#filterScenarios'), '主要场景', DATA.scenarios, 'scenarios');
  makeMultiSelect($('#filterStates'), '任务状态', statusOptions, 'states');
}

function auditUserOptions() {
  const users = new Map();
  const add = (name, email) => {
    const cleanEmail = String(email || '').trim();
    if (!cleanEmail || cleanEmail === 'unknown') return;
    const key = cleanEmail.toLowerCase();
    const cleanName = String(name || '').trim() || cleanEmail.split('@')[0];
    if (!users.has(key)) {
      users.set(key, {
        email: cleanEmail,
        name: cleanName,
        label: `${cleanName} · ${cleanEmail}`
      });
    }
  };
  (DATA.summaryRows || []).forEach(row => add(row.user, row.email));
  (DATA.rawEvents || []).forEach(row => add(row.user, row.email));
  return [...users.values()].sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'));
}

function syncAuditFilterInputs() {
  const user = $('#auditUserFilter');
  const start = $('#auditRangeStart');
  const end = $('#auditRangeEnd');
  if (user) user.value = state.auditFilters.userEmail || '';
  if (start) start.value = state.auditFilters.start || state.dateRange.start;
  if (end) end.value = state.auditFilters.end || state.dateRange.end;
}

function renderAuditFilters() {
  const select = $('#auditUserFilter');
  if (!select) return;
  const options = auditUserOptions();
  const selectedStillExists = !state.auditFilters.userEmail || options.some(item => item.email.toLowerCase() === state.auditFilters.userEmail.toLowerCase());
  if (!selectedStillExists) state.auditFilters.userEmail = '';
  select.innerHTML = [
    '<option value="">全部用户</option>',
    ...options.map(item => `<option value="${escapeAttr(item.email)}">${escapeHtml(item.label)}</option>`)
  ].join('');
  syncAuditFilterInputs();
  renderAuditFilterSummary();
}

function auditRangeFromInputs() {
  const start = $('#auditRangeStart')?.value || state.dateRange.start;
  const end = $('#auditRangeEnd')?.value || state.dateRange.end;
  if (!start || !end || end < start) {
    setAuditStatus('审计筛选时间段无效', 'error');
    return null;
  }
  return { start, end };
}

function auditSelectedUserLabel() {
  const email = state.auditFilters.userEmail || '';
  if (!email) return '全部用户';
  const match = auditUserOptions().find(item => item.email.toLowerCase() === email.toLowerCase());
  return match ? match.name : email;
}

function renderAuditFilterSummary() {
  const node = $('#auditFilterSummary');
  if (!node) return;
  const start = state.auditFilters.start || state.dateRange.start;
  const end = state.auditFilters.end || state.dateRange.end;
  node.textContent = `${auditSelectedUserLabel()} · ${start} 至 ${end}`;
}

function renderDailyChart(rows) {
  if (state.metricView === 'tokens') {
    renderTokenDailyChart(filteredTokenRows());
    return;
  }
  const byDate = new Map(DATA.dates.map(date => [date, { date, total: 0, success: 0, partial: 0, failed: 0, stopped: 0, running: 0 }]));
  rows.forEach(row => {
    const item = byDate.get(row.date);
    item.total += row.totalTasks;
    item.success += row.successTasks;
    item.partial += partialTasksForRow(row);
    item.failed += failedTasksForRow(row);
    item.stopped += stoppedTasksForRow(row);
    item.running += runningTasksForRow(row);
  });
  const items = [...byDate.values()];
  const max = Math.max(1, ...items.map(item => item.total));
  const width = 760;
  const height = 260;
  const gap = 26;
  const barWidth = 56;
  const base = 210;
  const svg = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="分日趋势图">
      <line x1="42" y1="${base}" x2="${width - 20}" y2="${base}" stroke="#cbd5e1"/>
      ${items.map((item, index) => {
        const x = 58 + index * (barWidth + gap);
        const successHeight = Math.round(item.success / max * 150);
        const partialHeight = Math.round(item.partial / max * 150);
        const failedHeight = Math.round(item.failed / max * 150);
        const stoppedHeight = Math.round(item.stopped / max * 150);
        const runningHeight = Math.round(item.running / max * 150);
        const totalHeight = successHeight + partialHeight + failedHeight + stoppedHeight + runningHeight;
        const successY = base - successHeight;
        const partialY = successY - partialHeight;
        const failedY = partialY - failedHeight;
        const stoppedY = failedY - stoppedHeight;
        const runningY = stoppedY - runningHeight;
        return `
          <g>
            <title>${item.date}: 成功 ${item.success}，部分完成 ${item.partial}，失败 ${item.failed}，中止 ${item.stopped}，进行中 ${item.running}</title>
            <rect x="${x}" y="${successY}" width="${barWidth}" height="${successHeight}" rx="5" fill="${colors.green}"/>
            <rect x="${x}" y="${partialY}" width="${barWidth}" height="${partialHeight}" rx="5" fill="${colors.orange}"/>
            <rect x="${x}" y="${failedY}" width="${barWidth}" height="${failedHeight}" rx="5" fill="${colors.red}"/>
            <rect x="${x}" y="${stoppedY}" width="${barWidth}" height="${stoppedHeight}" rx="5" fill="${colors.orange}"/>
            <rect x="${x}" y="${runningY}" width="${barWidth}" height="${runningHeight}" rx="5" fill="${colors.slate}"/>
            <text x="${x + barWidth / 2}" y="${base - totalHeight - 8}" text-anchor="middle" font-size="12" fill="#172033">${item.total}</text>
            <text x="${x + barWidth / 2}" y="${base + 26}" text-anchor="middle" font-size="13" fill="#64748b">${item.date.slice(5).replace('-', '/')}</text>
          </g>
        `;
      }).join('')}
    </svg>`;
  $('#dailyChart').innerHTML = svg;
}

function tokenEmptyMessage(stats) {
  if (!stats.recordCount) return '<div class="empty">当前筛选下没有 Token 用量记录。</div>';
  return `<div class="empty token-empty">
    当前筛选有 ${number(stats.recordCount)} 条 Token 用量记录。<br>
    Token 来自服务端去重用量账本，任务事件来自运行审计账本。
  </div>`;
}

function renderTokenDailyChart(rows) {
  const aggregate = aggregateTokenRows(rows);
  if (!aggregate.totalTokens) {
    $('#dailyChart').innerHTML = tokenEmptyMessage(aggregate);
    return;
  }
  const byDate = new Map(DATA.dates.map(date => [date, { date, totalTokens: 0, inputTokens: 0, outputTokens: 0 }]));
  rows.forEach(row => {
    const item = byDate.get(row.date);
    if (!item) return;
    item.totalTokens += row.tokenStats.totalTokens;
    item.inputTokens += row.tokenStats.inputTokens;
    item.outputTokens += row.tokenStats.outputTokens;
  });
  const items = [...byDate.values()];
  const max = Math.max(1, ...items.map(item => item.totalTokens));
  const width = 760;
  const height = 260;
  const gap = 26;
  const barWidth = 56;
  const base = 210;
  const svg = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="分日 Token 用量图">
      <line x1="42" y1="${base}" x2="${width - 20}" y2="${base}" stroke="#cbd5e1"/>
      ${items.map((item, index) => {
        const x = 58 + index * (barWidth + gap);
        const inputHeight = Math.round(item.inputTokens / max * 150);
        const outputHeight = Math.round(item.outputTokens / max * 150);
        const totalHeight = inputHeight + outputHeight;
        const inputY = base - inputHeight;
        const outputY = inputY - outputHeight;
        return `
          <g>
            <title>${item.date}: 总 Token ${tokenNumber(item.totalTokens)}，输入 ${tokenNumber(item.inputTokens)}，输出 ${tokenNumber(item.outputTokens)}</title>
            <rect x="${x}" y="${inputY}" width="${barWidth}" height="${inputHeight}" rx="5" fill="${colors.blue}"/>
            <rect x="${x}" y="${outputY}" width="${barWidth}" height="${outputHeight}" rx="5" fill="${colors.green}"/>
            <text x="${x + barWidth / 2}" y="${base - totalHeight - 8}" text-anchor="middle" font-size="12" fill="#172033">${tokenNumber(item.totalTokens)}</text>
            <text x="${x + barWidth / 2}" y="${base + 26}" text-anchor="middle" font-size="13" fill="#64748b">${item.date.slice(5).replace('-', '/')}</text>
          </g>
        `;
      }).join('')}
    </svg>`;
  $('#dailyChart').innerHTML = svg;
}

function renderUserChart(rows) {
  if (state.metricView === 'tokens') {
    renderTokenUserChart(filteredTokenRows());
    return;
  }
  const byUser = new Map();
  rows.forEach(row => {
    const item = byUser.get(row.user) || { user: row.user, total: 0, success: 0, intervention: 0 };
    item.total += row.totalTasks;
    item.success += row.successTasks;
    item.intervention += row.interventionCount;
    byUser.set(row.user, item);
  });
  const items = [...byUser.values()].sort((a, b) => b.total - a.total).slice(0, 8);
  const max = Math.max(1, ...items.map(item => item.total));
  $('#userChart').innerHTML = items.length ? `
    <div class="bar-list">
      ${items.map(item => `
        <div class="bar-row" data-tip="${escapeAttr(`${item.user}: 总任务 ${item.total}，成功 ${item.success}，需复查 ${item.intervention}`)}" title="${escapeAttr(`${item.user}: 总任务 ${item.total}，成功 ${item.success}，需复查 ${item.intervention}`)}">
          <div class="bar-label">${escapeHtml(item.user)}</div>
          <div class="bar-track"><span style="width:${Math.max(2, item.total / max * 100)}%"></span></div>
          <div class="bar-value">${item.total}</div>
        </div>
      `).join('')}
    </div>
  ` : '<div class="empty">无匹配用户数据</div>';
  ensureBarListStyles();
}

function renderTokenUserChart(rows) {
  const aggregate = aggregateTokenRows(rows);
  if (!aggregate.totalTokens) {
    $('#userChart').innerHTML = tokenEmptyMessage(aggregate);
    return;
  }
  const byUser = new Map();
  rows.forEach(row => {
    const item = byUser.get(row.user) || { user: row.user, totalTokens: 0, inputTokens: 0, outputTokens: 0 };
    item.totalTokens += row.tokenStats.totalTokens;
    item.inputTokens += row.tokenStats.inputTokens;
    item.outputTokens += row.tokenStats.outputTokens;
    byUser.set(row.user, item);
  });
  const items = [...byUser.values()].sort((a, b) => b.totalTokens - a.totalTokens).slice(0, 8);
  const max = Math.max(1, ...items.map(item => item.totalTokens));
  $('#userChart').innerHTML = items.length ? `
    <div class="bar-list">
      ${items.map(item => `
        <div class="bar-row" data-tip="${escapeAttr(`${item.user}: 总 Token ${tokenNumber(item.totalTokens)}，输入 ${tokenNumber(item.inputTokens)}，输出 ${tokenNumber(item.outputTokens)}`)}" title="${escapeAttr(`${item.user}: 总 Token ${tokenNumber(item.totalTokens)}，输入 ${tokenNumber(item.inputTokens)}，输出 ${tokenNumber(item.outputTokens)}`)}">
          <div class="bar-label">${escapeHtml(item.user)}</div>
          <div class="bar-track"><span style="width:${Math.max(2, item.totalTokens / max * 100)}%"></span></div>
          <div class="bar-value">${tokenNumber(item.totalTokens)}</div>
        </div>
      `).join('')}
    </div>
  ` : '<div class="empty">无匹配用户数据</div>';
  ensureBarListStyles();
}

function renderScenarioChart() {
  if (state.metricView === 'tokens') {
    renderTokenScenarioChart(filteredTokenRows());
    return;
  }
  const counts = new Map();
  filteredTasks().forEach(task => counts.set(task.scenario, (counts.get(task.scenario) || 0) + 1));
  const items = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const total = items.reduce((sum, item) => sum + item[1], 0) || 1;
  const palette = [colors.blue, colors.green, colors.orange, colors.teal, colors.purple, colors.red];
  $('#scenarioChart').innerHTML = items.length ? `
    <div class="scenario-list">
      ${items.map(([name, value], index) => `
        <details class="scenario-item">
          <summary class="scenario-line">
            <span><i style="background:${palette[index % palette.length]}"></i>${escapeHtml(name)}</span>
            <b>${value}</b>
          </summary>
          <div class="scenario-track"><span style="width:${value / total * 100}%;background:${palette[index % palette.length]}"></span></div>
          <div class="scenario-detail">${escapeHtml(scenarioDetailText(name, value, total))}</div>
        </details>
      `).join('')}
    </div>
  ` : '<div class="empty">无匹配场景数据</div>';
}

function renderTokenScenarioChart(rows) {
  const aggregate = aggregateTokenRows(rows);
  if (!aggregate.totalTokens) {
    $('#scenarioChart').innerHTML = tokenEmptyMessage(aggregate);
    return;
  }
  const counts = new Map();
  rows.forEach(row => {
    const sourceText = row.tokenStats.sourceText || '-';
    sourceText.split('、').forEach((part) => {
      const trimmed = part.trim();
      if (!trimmed || trimmed === '-') return;
      const match = trimmed.match(/^(.*)\s+(\d+)$/);
      const name = match ? match[1].trim() : trimmed;
      const value = match ? Number(match[2]) : 1;
      counts.set(name, (counts.get(name) || 0) + value);
    });
  });
  const totalRecords = [...counts.values()].reduce((sum, value) => sum + value, 0) || 1;
  const items = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const palette = [colors.blue, colors.green, colors.orange, colors.teal, colors.purple, colors.red];
  $('#scenarioChart').innerHTML = items.length ? `
    <div class="scenario-list">
      ${items.map(([name, value], index) => `
        <div class="scenario-item" title="${escapeAttr(`${name}: ${value} 条用量记录，占 ${pct(value / totalRecords * 100)}`)}">
          <div class="scenario-line">
            <span><i style="background:${palette[index % palette.length]}"></i>${escapeHtml(name)}</span>
            <b>${value}</b>
          </div>
          <div class="scenario-track"><span style="width:${value / totalRecords * 100}%;background:${palette[index % palette.length]}"></span></div>
        </div>
      `).join('')}
    </div>
  ` : '<div class="empty">无匹配场景数据</div>';
}

function ensureBarListStyles() {
  if ($('#dynamicBarStyles')) return;
  const style = document.createElement('style');
  style.id = 'dynamicBarStyles';
  style.textContent = `
    .bar-list,.scenario-list{display:flex;flex-direction:column;gap:12px}
    .bar-row{display:grid;grid-template-columns:minmax(78px,116px) 1fr 42px;gap:10px;align-items:center}
    .bar-label{font-size:13px;color:#334155;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .bar-track,.scenario-track{height:12px;border-radius:999px;background:#e2e8f0;overflow:hidden}
    .bar-track span,.scenario-track span{display:block;height:100%;border-radius:999px;background:#2563eb}
    .bar-value{font-size:13px;text-align:right;color:#172033;font-weight:700}
    .scenario-item{display:flex;flex-direction:column;gap:7px}
    .scenario-item summary{list-style:none;cursor:pointer}
    .scenario-item summary::-webkit-details-marker{display:none}
    .scenario-line{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}
    .scenario-line span{display:flex;align-items:center;gap:8px;color:#334155}
    .scenario-line i{width:10px;height:10px;border-radius:50%;display:inline-block}
    .scenario-detail{padding:8px 10px;border:1px solid #dbeafe;border-radius:7px;background:#f8fbff;color:#334155;font-size:12px;line-height:1.55;white-space:normal;overflow-wrap:anywhere}
  `;
  document.head.appendChild(style);
}

function renderInsights(rows = filteredRows()) {
  if (state.metricView === 'tokens') {
    renderTokenInsights(filteredTokenRows());
    return;
  }
  const a = aggregateRows(rows);
  const tasks = filteredTasks();
  const rawEvents = rawEventsForSelection();
  const activeUsers = new Set(rows.map(row => row.user)).size;
  const activeDates = new Set(rows.map(row => row.date)).size;
  const byDate = new Map();
  const byUser = new Map();
  const byScenario = new Map();
  rows.forEach(row => {
    const dateItem = byDate.get(row.date) || { name: row.date, total: 0 };
    dateItem.total += row.totalTasks;
    byDate.set(row.date, dateItem);
    const userItem = byUser.get(row.user) || { name: row.user, total: 0 };
    userItem.total += row.totalTasks;
    byUser.set(row.user, userItem);
  });
  tasks.forEach(task => byScenario.set(task.scenario, (byScenario.get(task.scenario) || 0) + 1));
  const topDate = [...byDate.values()].sort((left, right) => right.total - left.total)[0];
  const topUser = [...byUser.values()].sort((left, right) => right.total - left.total)[0];
  const topScenario = [...byScenario.entries()].sort((left, right) => right[1] - left[1])[0];
  const insights = [];
  insights.push(`当前筛选包含 ${activeUsers} 个用户、${activeDates} 天，共 ${a.totalTasks} 个任务、${rawEvents.length} 条事件。`);
  if (a.totalTasks) {
    insights.push(`成功 ${a.successTasks} 个，部分完成 ${a.partialTasks} 个，失败 ${a.failedTasks} 个，中止 ${a.stoppedTasks} 个，进行中 ${a.runningTasks} 个，已结束成功率 ${pct(a.successRate)}；平均完成时间 ${minutes(a.avgCompletionMinutes)}。`);
    insights.push(`有效产物 ${a.effectiveArtifacts} 个，无效产物 ${a.invalidArtifacts} 个；无效产物按用户勾选 👎 统计。`);
    if (topDate) insights.push(`任务最多的日期是 ${topDate.name}，当天 ${topDate.total} 个任务。`);
    if (topUser) insights.push(`任务最多的用户是 ${topUser.name}，共 ${topUser.total} 个任务。`);
    if (topScenario) insights.push(`主要使用场景是“${topScenario[0]}”，当前筛选下 ${topScenario[1]} 个任务。`);
    insights.push(a.interventionCount
      ? `有 ${a.interventionCount} 个任务需要人工看一下，通常是失败、受限或中止导致。`
      : '当前筛选下没有发现失败、受限或中止任务。');
  } else {
    insights.push('当前筛选下没有匹配的成功、失败或中止任务。');
    insights.push('可以放宽用户、日期、场景或任务状态筛选后再看。');
  }
  $('#insights').innerHTML = insights.map(item => `<div class="insight">${escapeHtml(item)}</div>`).join('');
}

function renderTokenInsights(rows) {
  const a = aggregateTokenRows(rows);
  const insights = [];
  insights.push(`当前筛选包含 ${a.recordCount} 条 Token 用量记录，总 Token ${tokenNumber(a.totalTokens)}。`);
  if (a.totalTokens) {
    const currentCacheRate = cacheHitRate(a);
    insights.push(`总 Token ${tokenNumber(a.totalTokens)}，其中输入 ${tokenNumber(a.inputTokens)}，输出 ${tokenNumber(a.outputTokens)}。`);
    insights.push(`缓存命中率 ${tokenRate(currentCacheRate)}：命中 ${tokenNumber(a.cacheReadTokens)}，输入 ${tokenNumber(a.cacheInputTokens || a.inputTokens)}；${cacheReportedLabel(a)}。`);
    insights.push(`Artificial Analysis 公开编码 Agent 榜单显示 Claude Code 96%、Cursor CLI 89%，这里取 ${tokenRate(mainstreamCacheReferenceRate)} 做对标线；当前相差 ${tokenRate(Math.max(0, mainstreamCacheReferenceRate - currentCacheRate))}。`);
    const topUser = [...rows.reduce((map, row) => {
      const item = map.get(row.user) || { user: row.user, totalTokens: 0 };
      item.totalTokens += row.tokenStats.totalTokens;
      map.set(row.user, item);
      return map;
    }, new Map()).values()].sort((left, right) => right.totalTokens - left.totalTokens)[0];
    if (topUser) insights.push(`Token 用量最高的用户是 ${topUser.user}，当前筛选下 ${tokenNumber(topUser.totalTokens)} Token。`);
    if (a.estimatedRecords) insights.push(`其中 ${a.estimatedRecords} 条记录为估算口径，来自 usage_events.detail 里的 usageSource=estimated。`);
  } else {
    insights.push('当前筛选下没有 Token 用量记录。');
    insights.push('可以放宽用户或日期筛选后再看。');
  }
  $('#insights').innerHTML = insights.map(item => `<div class="insight">${escapeHtml(item)}</div>`).join('');
}

function runtimeAuditData() {
  return auditState.data || DATA.runtimeAudit || {};
}

function auditSummaryValue(summary, key, fallback = 0) {
  const value = Number(summary && summary[key]);
  return Number.isFinite(value) ? value : fallback;
}

function setAuditStatus(text, tone = '') {
  const node = $('#auditStatus');
  if (!node) return;
  node.textContent = text;
  node.className = `live-status ${tone}`.trim();
}

function renderAuditEmpty(target, text) {
  const node = $(target);
  if (node) node.innerHTML = `<div class="audit-empty">${escapeHtml(text)}</div>`;
}

function renderAuditPanel() {
  if (!$('#auditSummaryGrid')) return;
  renderAuditFilterSummary();
  const audit = runtimeAuditData();
  const summary = audit.summary || {};
  const actionCounts = audit.actionTypeCounts || {};
  const actionLabels = audit.actionTypeLabels || {};
  const userActions = Array.isArray(audit.userActions) ? audit.userActions : [];
  const effectiveArtifacts = Array.isArray(audit.effectiveArtifacts) ? audit.effectiveArtifacts : [];
  const feedbackTraces = Array.isArray(audit.feedbackTraces) ? audit.feedbackTraces : [];
  const hasAudit = Boolean(audit && (audit.summary || userActions.length || effectiveArtifacts.length || feedbackTraces.length));

  if (auditState.status === 'error') setAuditStatus('审计数据读取失败', 'error');
  else if (auditState.status === 'loading') setAuditStatus('正在读取审计数据', 'loading');
  else setAuditStatus(hasAudit ? '审计数据已接入' : '暂无审计数据', hasAudit ? 'ok' : '');

  const summaryCards = [
    { label: '用户动作', value: auditSummaryValue(summary, 'userActions'), foot: '按运行时事件自动分类' },
    { label: '图片处理', value: auditSummaryValue(summary, 'imageProcessingActions'), foot: '包含 imagegen / image_job' },
    { label: '有效产物', value: auditSummaryValue(summary, 'effectiveArtifacts', effectiveArtifacts.length), foot: '来自 sync_artifacts 自动判断' },
    { label: '下拇指回溯', value: auditSummaryValue(summary, 'feedbackTraceCount', feedbackTraces.length), foot: '关联用户、产物与会话线索' }
  ];
  $('#auditSummaryGrid').innerHTML = summaryCards.map(card => `
    <article class="audit-summary-card">
      <span>${escapeHtml(card.label)}</span>
      <strong>${number(card.value)}</strong>
      <small>${escapeHtml(card.foot)}</small>
    </article>
  `).join('');

  const actionItems = Object.entries(actionCounts)
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0))
    .slice(0, 8);
  if (actionItems.length) {
    $('#auditActionTypes').innerHTML = actionItems.map(([key, value]) => `
      <div class="audit-line">
        <span>${escapeHtml(actionLabels[key] || key)}</span>
        <strong>${number(value)}</strong>
      </div>
    `).join('');
  } else {
    renderAuditEmpty('#auditActionTypes', '暂无可展示的动作分类。');
  }

  if (userActions.length) {
    $('#auditUserActions').innerHTML = userActions.slice(0, 8).map(action => `
      <div class="audit-item">
        <strong>${escapeHtml(action.actionLabel || action.actionType || '用户动作')}</strong>
        <span>${escapeHtml(action.eventType || '')}${action.status ? ` · ${escapeHtml(action.status)}` : ''}</span>
        <small>${escapeHtml(action.occurredAt || action.ingestedAt || '')}</small>
      </div>
    `).join('');
  } else {
    renderAuditEmpty('#auditUserActions', '暂无最近用户动作。');
  }

  if (effectiveArtifacts.length) {
    $('#effectiveArtifacts').innerHTML = effectiveArtifacts.slice(0, 8).map(item => `
      <div class="audit-item">
        <strong>${escapeHtml(item.artifactTitle || item.intent || '有效产物')}</strong>
        <span>${escapeHtml([item.kind, item.pathExt, item.status].filter(Boolean).join(' · '))}</span>
        <small>${escapeHtml(item.createdAt || item.ingestedAt || '')}</small>
      </div>
    `).join('');
  } else {
    renderAuditEmpty('#effectiveArtifacts', '暂无自动识别的有效产物。');
  }

  if (feedbackTraces.length) {
    $('#feedbackTraces').innerHTML = feedbackTraces.slice(0, 8).map(item => {
      const share = item.feedbackShareUrl
        ? `<a href="${escapeAttr(item.feedbackShareUrl)}" target="_blank" rel="noreferrer">查看会话</a>`
        : '<span>无会话链接</span>';
      return `
        <div class="audit-item">
          <strong>${escapeHtml(item.userName || item.userEmail || '匿名用户')}</strong>
          <span>${escapeHtml(item.artifactTitle || item.artifactHash || '产物线索')} · ${escapeHtml(item.artifactFeedbackSignal || 'thumbs_down')}</span>
          <small>${escapeHtml(item.artifactFeedbackAt || item.createdAt || '')} ${share}</small>
        </div>
      `;
    }).join('');
  } else {
    renderAuditEmpty('#feedbackTraces', '暂无下拇指反馈。');
  }
}

function bindManualNoteInputs(root) {
  root.querySelectorAll('.manual-note').forEach((input) => {
    input.addEventListener('input', () => {
      if (input.value.trim() === '') delete manualNotes[input.dataset.rowId];
      else manualNotes[input.dataset.rowId] = input.value;
      saveManualNotes();
    });
  });
}

function headerCell(label, tip) {
  return `<th data-tip="${escapeAttr(tip)}" title="${escapeAttr(tip)}">${escapeHtml(label)}</th>`;
}

function renderViewCopy() {
  document.body.dataset.view = state.metricView;
  document.querySelectorAll('.dimension-tab').forEach((button) => {
    button.classList.toggle('active', button.dataset.metricView === state.metricView);
  });
  if (state.metricView === 'tokens') {
    $('#dailyChartTitle').textContent = '分日 Token 用量';
    $('#dailyChartHelp').textContent = '来自 usage_events 表，按输入 / 输出 Token 分日汇总。';
    $('#dailyLegend').innerHTML = '<i class="dot blue"></i>输入 <i class="dot green"></i>输出';
    $('#userChartTitle').textContent = '用户 Token 用量';
    $('#scenarioChartTitle').textContent = '用量来源';
    $('#summaryTableTitle').textContent = '用户 × 分日 Token 用量';
    $('#summaryTableHelp').textContent = 'Token 数字来自服务器 usage_events 表；缓存命中率按已上报缓存命中 Token 计算，未上报时显示 0.0%。';
    $('#personTableTitle').textContent = '个人 Token 明细';
    $('#personTableHelp').textContent = '按个人先汇总；点击个人行可展开查看每天的 Token 和缓存命中情况。';
  } else {
    $('#dailyChartTitle').textContent = '分日趋势';
    $('#dailyChartHelp').textContent = '柱上数字为：成功 / 失败 / 中止；中止为用户主动停止任务。';
    $('#dailyLegend').innerHTML = '<i class="dot green"></i>成功 <i class="dot red"></i>失败 <i class="dot orange"></i>中止';
    $('#userChartTitle').textContent = '用户任务量';
    $('#scenarioChartTitle').textContent = '主要使用场景';
    $('#summaryTableTitle').textContent = '用户 × 分日使用情况';
    $('#summaryTableHelp').textContent = '有效产物排除用户勾选 👎 的产物；无效产物按下拇指口径统计。';
    $('#personTableTitle').textContent = '个人明细数据';
    $('#personTableHelp').textContent = '按个人先汇总；点击个人行可展开查看每天的成功、失败、中止、产物和主要使用场景。';
  }
}

function renderTableHeaders() {
  if (state.metricView === 'tokens') {
    $('#summaryHead').innerHTML = `<tr>
      ${headerCell('用户', '这个用户是谁；没有姓名时会显示邮箱前半段')}
      ${headerCell('分日', '按天拆开看 Token 上报情况')}
      ${headerCell('用量记录数', 'usage_events 表里的记录条数')}
      ${headerCell('输入 Token', 'usage_events.input_tokens 汇总')}
      ${headerCell('缓存命中 Token', '已上报的缓存命中输入 Token；本期没有上报时为 0')}
      ${headerCell('缓存命中率', '缓存命中率 = 缓存命中输入 Token / 输入 Token；主流编码 Agent 参考约 89%-96%')}
      ${headerCell('输出 Token', 'usage_events.output_tokens 汇总')}
      ${headerCell('总 Token', 'usage_events.total_tokens 汇总')}
      ${headerCell('估算记录数', 'usageSource=estimated 的记录数')}
      ${headerCell('主要模型', '当前范围内最常见的模型')}
      ${headerCell('用量来源', 'usage_events.detail 里的 usageSource')}
      ${headerCell('有效产物数', '优先使用服务器自动统计；你也可以手动修正，导出汇总时会带上')}
      ${headerCell('备注', '需要你手动填写；适合写当天补充说明、异常说明或业务侧判断')}
    </tr>`;
    $('#personDetailHead').innerHTML = `<tr>
      ${headerCell('用户', '点击个人行可展开或收起分日数据')}
      ${headerCell('日期', '个人汇总行显示当前筛选范围；展开后显示具体日期')}
      ${headerCell('用量记录数', 'usage_events 表里的记录条数')}
      ${headerCell('输入 Token', 'usage_events.input_tokens 汇总')}
      ${headerCell('缓存命中 Token', '已上报的缓存命中输入 Token；本期没有上报时为 0')}
      ${headerCell('缓存命中率', '缓存命中率 = 缓存命中输入 Token / 输入 Token；主流编码 Agent 参考约 89%-96%')}
      ${headerCell('输出 Token', 'usage_events.output_tokens 汇总')}
      ${headerCell('总 Token', 'usage_events.total_tokens 汇总')}
      ${headerCell('估算记录数', 'usageSource=estimated 的记录数')}
      ${headerCell('主要模型', '当前范围内最常见的模型')}
      ${headerCell('用量来源', 'usage_events.detail 里的 usageSource')}
      ${headerCell('有效产物数', '个人汇总行会累加自动统计或已手动修正的有效产物数')}
      ${headerCell('备注', '展开到分日后可手动填写；适合写个人每天的补充说明')}
    </tr>`;
    return;
  }
  $('#summaryHead').innerHTML = `<tr>
    ${headerCell('用户', '这个用户是谁；没有姓名时会显示邮箱前半段')}
    ${headerCell('分日', '按天拆开看，方便看到哪一天用得多、哪一天没记录')}
    ${headerCell('总任务数', '用户发起一次需求并被系统接收，就算 1 个任务；同一个任务会产生多条事件')}
    ${headerCell('成功任务数', '这一天完成了多少个任务；有完成结果才算成功')}
    ${headerCell('部分完成', '任务已返回结果，但至少一个工具步骤失败或取消')}
    ${headerCell('失败任务数', '已接收但未成功，且不是用户主动停止的任务')}
    ${headerCell('中止任务数', '用户主动停止的任务')}
    ${headerCell('进行中', '任务已接收，但还没有终态结果')}
    ${headerCell('成功任务率', '成功任务数除以已结束任务数；进行中任务不进入分母')}
    ${headerCell('平均完成时间', '只看已完成任务，从开始到完成平均用了多久')}
    ${headerCell('人工干预次数', '取消、失败、受限这类需要人工看一下的任务数量')}
    ${headerCell('干预率', '人工干预次数除以总任务数；用于看这一天需要复查的比例')}
    ${headerCell('主要使用场景', '按创作内容、制作素材、搜索查询、处理数据、编辑文档、交付通知、系统维护归类')}
    ${headerCell('有效产物数', '未被用户勾选下拇指且状态可用的产物；仍可手动修正')}
    ${headerCell('无效产物数', '用户勾选 👎 的产物定义为无效产物')}
    ${headerCell('备注', '需要你手动填写；适合写当天补充说明、异常说明或业务侧判断')}
  </tr>`;
  $('#personDetailHead').innerHTML = `<tr>
    ${headerCell('用户', '点击个人行可展开或收起分日数据')}
    ${headerCell('日期', '个人汇总行显示当前筛选范围；展开后显示具体日期')}
    ${headerCell('任务数', '任务数就是使用次数：用户发起一次需求并被系统接收，算 1 次')}
    ${headerCell('成功', '成功完成的任务数')}
    ${headerCell('部分完成', '任务返回结果，但有工具步骤失败或取消')}
    ${headerCell('失败', '已接收但未成功，且不是用户主动停止的任务')}
    ${headerCell('中止', '用户主动停止的任务')}
    ${headerCell('进行中', '已接收但尚未结束的任务')}
    ${headerCell('成功率', '成功数除以已结束任务数')}
    ${headerCell('平均完成时间', '只统计已完成任务，从开始到完成平均用了多久')}
    ${headerCell('人工干预', '取消、失败、受限这类需要人工看一下的任务数量')}
    ${headerCell('主要使用场景', '当前范围内最常出现的使用方向')}
    ${headerCell('有效产物数', '个人汇总行会累加自动统计或已手动修正的有效产物数；展开后显示每天数值')}
    ${headerCell('无效产物数', '用户勾选 👎 的产物数量')}
    ${headerCell('备注', '展开到分日后可手动填写；适合写个人每天的补充说明')}
  </tr>`;
}

function renderTable(rows) {
  if (state.metricView === 'tokens') {
    renderTokenTable(filteredTokenRows());
    return;
  }
  $('#rowCount').textContent = `${rows.length} 行`;
  const body = $('#summaryBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="16"><div class="empty">没有匹配的汇总数据</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(row => {
    const manualValue = artifactValueForRow(row);
    const manualNote = manualNotes[row.id] ?? '';
    const failedTasks = failedTasksForRow(row);
    const stoppedTasks = stoppedTasksForRow(row);
    const partialTasks = partialTasksForRow(row);
    const runningTasks = runningTasksForRow(row);
    const unsuccessfulTasks = partialTasks + failedTasks + stoppedTasks;
    const successClass = row.totalTasks === 0 ? '' : row.successRate >= 95 ? 'metric-good' : row.successRate >= 80 ? 'metric-warn' : 'metric-bad';
    const interventionClass = row.interventionCount === 0 ? 'metric-good' : row.interventionRate >= 20 ? 'metric-bad' : 'metric-warn';
    const mainRow = `
      <tr title="${escapeAttr(`${row.user} ${row.date}: 总任务 ${row.totalTasks}，成功 ${row.successTasks}，部分完成 ${partialTasks}，失败 ${failedTasks}，中止 ${stoppedTasks}，进行中 ${runningTasks}，主要场景 ${row.mainScenario}`)}">
        <td><strong>${escapeHtml(row.user)}</strong><br><span class="muted small">${escapeHtml(row.email || '')}</span></td>
        <td>${row.date}</td>
        <td>${row.totalTasks}</td>
        <td>${row.successTasks}</td>
        <td class="${partialTasks ? 'metric-warn' : 'metric-good'}">${partialTasks}</td>
        <td class="${failedTasks ? 'metric-bad' : 'metric-good'}">${failedTasks}</td>
        <td class="${stoppedTasks ? 'metric-warn' : 'metric-good'}">${stoppedTasks}</td>
        <td>${runningTasks}</td>
        <td class="${successClass}">${pct(row.successRate)}${failureToggleHtml(row.id, row.user, row.date, unsuccessfulTasks)}</td>
        <td>${minutes(row.avgCompletionMinutes)}</td>
        <td class="${interventionClass}">${row.interventionCount}</td>
        <td class="${interventionClass}">${pct(row.interventionRate)}</td>
        <td>${escapeHtml(row.mainScenario)}</td>
        <td><input class="manual-input" data-row-id="${escapeAttr(row.id)}" type="number" min="0" step="1" value="${escapeAttr(manualValue)}"></td>
        <td class="${invalidArtifactsForRow(row) ? 'metric-bad' : 'metric-good'}">${number(invalidArtifactsForRow(row))}</td>
        <td><textarea class="manual-note" data-row-id="${escapeAttr(row.id)}" placeholder="手动填写备注">${escapeHtml(manualNote)}</textarea></td>
      </tr>
    `;
    return mainRow + failureDetailRowHtml(row.id, row.user, row.date, 16);
  }).join('');
  body.querySelectorAll('.manual-input').forEach((input) => {
    input.addEventListener('input', () => {
      if (input.value === '') delete manualArtifacts[input.dataset.rowId];
      else manualArtifacts[input.dataset.rowId] = input.value;
      saveManualArtifacts();
    });
  });
  bindManualNoteInputs(body);
  bindFailureToggles(body, () => renderTable(filteredRows()));
}

function renderTokenTable(rows) {
  $('#rowCount').textContent = `${rows.length} 行`;
  const body = $('#summaryBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="13"><div class="empty">没有匹配的 Token 汇总数据</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(row => {
    const stats = row.tokenStats;
    const manualValue = artifactValueForRow(row);
    const manualNote = manualNotes[row.id] ?? '';
    const tokenClass = stats.totalTokens ? 'metric-good' : 'metric-warn';
    const rate = cacheHitRate(stats);
    const cacheClass = stats.cacheReportedRecords ? (rate >= mainstreamCacheReferenceRate ? 'metric-good' : 'metric-warn') : 'metric-warn';
    return `
      <tr title="${escapeAttr(`${row.user} ${row.date}: 总 Token ${tokenNumber(stats.totalTokens)}，缓存命中率 ${tokenRate(rate)}，${cacheReportedLabel(stats)}`)}">
        <td><strong>${escapeHtml(row.user)}</strong><br><span class="muted small">${escapeHtml(row.email || '')}</span></td>
        <td>${row.date}</td>
        <td>${number(stats.recordCount)}</td>
        <td>${tokenNumber(stats.inputTokens)}</td>
        <td>${tokenNumber(stats.cacheReadTokens)}</td>
        <td class="${cacheClass}" title="${escapeAttr(cacheReportedLabel(stats))}">${tokenRate(rate)}</td>
        <td>${tokenNumber(stats.outputTokens)}</td>
        <td class="${tokenClass}">${tokenNumber(stats.totalTokens)}</td>
        <td>${number(stats.estimatedRecords)}</td>
        <td>${escapeHtml(stats.modelText)}</td>
        <td>${escapeHtml(stats.sourceText)}</td>
        <td><input class="manual-input" data-row-id="${escapeAttr(row.id)}" type="number" min="0" step="1" value="${escapeAttr(manualValue)}"></td>
        <td><textarea class="manual-note" data-row-id="${escapeAttr(row.id)}" placeholder="手动填写备注">${escapeHtml(manualNote)}</textarea></td>
      </tr>
    `;
  }).join('');
  body.querySelectorAll('.manual-input').forEach((input) => {
    input.addEventListener('input', () => {
      if (input.value === '') delete manualArtifacts[input.dataset.rowId];
      else manualArtifacts[input.dataset.rowId] = input.value;
      saveManualArtifacts();
    });
  });
  bindManualNoteInputs(body);
}

function addScenarioCounts(counter, text) {
  if (!text || text === '无') return;
  text.split('、').forEach((part) => {
    const trimmed = part.trim();
    if (!trimmed) return;
    const match = trimmed.match(/^(.*)\s+(\d+)$/);
    const name = match ? match[1].trim() : trimmed;
    const count = match ? Number(match[2]) : 1;
    counter.set(name, (counter.get(name) || 0) + count);
  });
}

function formatScenarioCounter(counter) {
  const items = [...counter.entries()].sort((left, right) => right[1] - left[1]).slice(0, 3);
  return items.length ? items.map(([name, count]) => `${name} ${count}`).join('、') : '无';
}

function summarizeRows(rows) {
  const scenarioCounts = new Map();
  const totals = rows.reduce((acc, row) => {
    acc.totalTasks += row.totalTasks;
    acc.successTasks += row.successTasks;
    acc.partialTasks += partialTasksForRow(row);
    acc.failedTasks += failedTasksForRow(row);
    acc.stoppedTasks += stoppedTasksForRow(row);
    acc.runningTasks += runningTasksForRow(row);
    acc.interventionCount += row.interventionCount;
    acc.manualArtifacts += artifactCountForRow(row);
    acc.invalidArtifacts += invalidArtifactsForRow(row);
    if (row.avgCompletionMinutes != null && row.successTasks > 0) {
      acc.durationSum += row.avgCompletionMinutes * row.successTasks;
      acc.durationCount += row.successTasks;
    }
    addScenarioCounts(scenarioCounts, row.mainScenario);
    if (row.remarks) acc.remarks.add(row.remarks);
    return acc;
  }, {
    totalTasks: 0,
    successTasks: 0,
    partialTasks: 0,
    failedTasks: 0,
    stoppedTasks: 0,
    runningTasks: 0,
    interventionCount: 0,
    manualArtifacts: 0,
    invalidArtifacts: 0,
    durationSum: 0,
    durationCount: 0,
    remarks: new Set()
  });
  if (!totals.failedTasks) totals.failedTasks = Math.max(0, totals.totalTasks - totals.successTasks - totals.partialTasks - totals.stoppedTasks - totals.runningTasks);
  totals.terminalTasks = totals.successTasks + totals.partialTasks + totals.failedTasks + totals.stoppedTasks;
  totals.successRate = totals.terminalTasks ? totals.successTasks / totals.terminalTasks * 100 : 0;
  totals.avgCompletionMinutes = totals.durationCount ? totals.durationSum / totals.durationCount : null;
  totals.mainScenario = formatScenarioCounter(scenarioCounts);
  totals.remarksText = [...totals.remarks].slice(0, 3).join('；') || (totals.totalTasks ? '当前筛选范围内有任务记录' : '当前筛选范围内无任务记录');
  return totals;
}

function failureToggleHtml(key, user, date, failedTasks) {
  if (!failedTasks) return '';
  const expanded = state.expandedFailures.has(key);
  const scope = date ? `${user} ${date}` : user;
  return `<button class="failure-toggle" type="button" data-failure-key="${escapeAttr(key)}" data-tip="展开查看 ${escapeAttr(scope)} 的未成功原因">${expanded ? '收起原因' : `未成功 ${failedTasks} · 原因`}</button>`;
}

function failureDetailRowHtml(key, user, date, colspan) {
  if (!state.expandedFailures.has(key)) return '';
  const tasks = failureTasksFor(user, date);
  const content = tasks.length
    ? `<ol class="failure-list">${tasks.map(task => `
        <li>
          <strong>${escapeHtml(task.date)} ${escapeHtml(task.time || '')}</strong>
          <span>${escapeHtml(taskStatus(task))} · ${escapeHtml(task.scenario || '未识别场景')}</span>
          <span>${escapeHtml(failureReasonForTask(task))}</span>
        </li>
      `).join('')}</ol>`
    : '<div class="failure-empty">当前筛选下没有未成功任务。</div>';
  return `<tr class="failure-detail-row"><td colspan="${colspan}">${content}</td></tr>`;
}

function bindFailureToggles(root, rerender) {
  root.querySelectorAll('.failure-toggle').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const key = button.dataset.failureKey;
      if (state.expandedFailures.has(key)) state.expandedFailures.delete(key);
      else state.expandedFailures.add(key);
      rerender();
      mountIcons();
    });
  });
}

function renderPersonDetailTable(rows) {
  if (state.metricView === 'tokens') {
    renderTokenPersonDetailTable(filteredTokenRows());
    return;
  }
  const body = $('#personDetailBody');
  const groups = new Map();
  rows.forEach((row) => {
    const item = groups.get(row.user) || { user: row.user, email: row.email || '', rows: [] };
    if (!item.email && row.email) item.email = row.email;
    item.rows.push(row);
    groups.set(row.user, item);
  });
  const users = [...groups.values()].sort((left, right) => {
    const leftTotal = summarizeRows(left.rows).totalTasks;
    const rightTotal = summarizeRows(right.rows).totalTasks;
    return rightTotal - leftTotal || left.user.localeCompare(right.user, 'zh-CN');
  });
  const dayCount = users.reduce((sum, item) => sum + item.rows.length, 0);
  $('#detailRowCount').textContent = `${number(users.length)} 人 · ${number(dayCount)} 天`;
  if (!users.length) {
    body.innerHTML = '<tr><td colspan="15"><div class="empty">没有匹配的个人明细数据</div></td></tr>';
    return;
  }
  body.innerHTML = users.map((item) => {
    const summary = summarizeRows(item.rows);
    const expanded = state.expandedUsers.has(item.user);
    const notedDays = item.rows.filter(row => (manualNotes[row.id] || '').trim()).length;
    const successClass = summary.totalTasks === 0 ? '' : summary.successRate >= 95 ? 'metric-good' : summary.successRate >= 80 ? 'metric-warn' : 'metric-bad';
    const unsuccessfulTasks = summary.partialTasks + summary.failedTasks + summary.stoppedTasks;
    const parent = `
      <tr class="person-row" data-user="${escapeAttr(item.user)}" title="${escapeAttr(`点击查看 ${item.user} 的分日数据`)}">
        <td><button class="expand-toggle" type="button">${expanded ? '收起' : '展开'}</button><strong>${escapeHtml(item.user)}</strong><br><span class="muted small">${escapeHtml(item.email || '')}</span></td>
        <td>当前筛选范围</td>
        <td>${summary.totalTasks}</td>
        <td class="metric-good">${summary.successTasks}</td>
        <td class="${summary.partialTasks ? 'metric-warn' : 'metric-good'}">${summary.partialTasks}</td>
        <td class="${summary.failedTasks ? 'metric-bad' : 'metric-good'}">${summary.failedTasks}</td>
        <td class="${summary.stoppedTasks ? 'metric-warn' : 'metric-good'}">${summary.stoppedTasks}</td>
        <td>${summary.runningTasks}</td>
        <td class="${successClass}">${pct(summary.successRate)}${failureToggleHtml(`person|${item.user}`, item.user, null, unsuccessfulTasks)}</td>
        <td>${minutes(summary.avgCompletionMinutes)}</td>
        <td class="${summary.interventionCount ? 'metric-warn' : 'metric-good'}">${summary.interventionCount}</td>
        <td>${escapeHtml(summary.mainScenario)}</td>
        <td>${summary.manualArtifacts || ''}</td>
        <td class="${summary.invalidArtifacts ? 'metric-bad' : 'metric-good'}">${summary.invalidArtifacts || ''}</td>
        <td>${escapeHtml(notedDays ? `已填写 ${notedDays} 天备注` : (expanded ? '可在分日行填写备注' : `点击展开 ${item.rows.length} 天数据`))}</td>
      </tr>
    `;
    const parentFailureRow = failureDetailRowHtml(`person|${item.user}`, item.user, null, 15);
    if (!expanded) return parent + parentFailureRow;
    const children = item.rows
      .sort((left, right) => left.date.localeCompare(right.date))
      .map((row) => {
        const failedTasks = failedTasksForRow(row);
        const stoppedTasks = stoppedTasksForRow(row);
        const partialTasks = partialTasksForRow(row);
        const runningTasks = runningTasksForRow(row);
        const unsuccessfulTasks = partialTasks + failedTasks + stoppedTasks;
        const manualNote = manualNotes[row.id] ?? '';
        const rowSuccessClass = row.totalTasks === 0 ? '' : row.successRate >= 95 ? 'metric-good' : row.successRate >= 80 ? 'metric-warn' : 'metric-bad';
        return `
          <tr class="detail-child-row">
            <td><span class="muted small">分日</span></td>
            <td>${row.date}</td>
            <td>${row.totalTasks}</td>
            <td class="metric-good">${row.successTasks}</td>
            <td class="${partialTasks ? 'metric-warn' : 'metric-good'}">${partialTasks}</td>
            <td class="${failedTasks ? 'metric-bad' : 'metric-good'}">${failedTasks}</td>
            <td class="${stoppedTasks ? 'metric-warn' : 'metric-good'}">${stoppedTasks}</td>
            <td>${runningTasks}</td>
            <td class="${rowSuccessClass}">${pct(row.successRate)}${failureToggleHtml(row.id, row.user, row.date, unsuccessfulTasks)}</td>
            <td>${minutes(row.avgCompletionMinutes)}</td>
            <td class="${row.interventionCount ? 'metric-warn' : 'metric-good'}">${row.interventionCount}</td>
            <td>${escapeHtml(row.mainScenario)}</td>
            <td>${escapeHtml(artifactValueForRow(row))}</td>
            <td class="${invalidArtifactsForRow(row) ? 'metric-bad' : 'metric-good'}">${invalidArtifactsForRow(row) || ''}</td>
            <td><textarea class="manual-note compact" data-row-id="${escapeAttr(row.id)}" placeholder="手动填写备注">${escapeHtml(manualNote)}</textarea></td>
          </tr>
        ` + failureDetailRowHtml(row.id, row.user, row.date, 15);
      }).join('');
    return parent + parentFailureRow + children;
  }).join('');
  body.querySelectorAll('.person-row').forEach((row) => {
    row.addEventListener('click', () => {
      const user = row.dataset.user;
      if (state.expandedUsers.has(user)) state.expandedUsers.delete(user);
      else state.expandedUsers.add(user);
      renderPersonDetailTable(filteredRows());
      mountIcons();
    });
  });
  bindManualNoteInputs(body);
  bindFailureToggles(body, () => renderPersonDetailTable(filteredRows()));
}

function topTextFromRows(rows, field) {
  const counts = new Map();
  rows.forEach((row) => {
    const text = row.tokenStats && row.tokenStats[field];
    if (!text || text === '-') return;
    text.split('、').forEach((part) => {
      const trimmed = part.trim();
      if (!trimmed) return;
      const match = trimmed.match(/^(.*)\s+(\d+)$/);
      const name = match ? match[1].trim() : trimmed;
      const count = match ? Number(match[2]) : 1;
      counts.set(name, (counts.get(name) || 0) + count);
    });
  });
  const items = [...counts.entries()].sort((left, right) => right[1] - left[1]).slice(0, 3);
  return items.length ? items.map(([name, count]) => `${name} ${count}`).join('、') : '-';
}

function renderTokenPersonDetailTable(rows) {
  const body = $('#personDetailBody');
  const groups = new Map();
  rows.forEach((row) => {
    const item = groups.get(row.user) || { user: row.user, email: row.email || '', rows: [] };
    if (!item.email && row.email) item.email = row.email;
    item.rows.push(row);
    groups.set(row.user, item);
  });
  const users = [...groups.values()].sort((left, right) => {
    const leftTotal = aggregateTokenRows(left.rows).totalTokens;
    const rightTotal = aggregateTokenRows(right.rows).totalTokens;
    const leftRecords = aggregateTokenRows(left.rows).recordCount;
    const rightRecords = aggregateTokenRows(right.rows).recordCount;
    return rightTotal - leftTotal || rightRecords - leftRecords || left.user.localeCompare(right.user, 'zh-CN');
  });
  const dayCount = users.reduce((sum, item) => sum + item.rows.length, 0);
  $('#detailRowCount').textContent = `${number(users.length)} 人 · ${number(dayCount)} 天`;
  if (!users.length) {
    body.innerHTML = '<tr><td colspan="13"><div class="empty">没有匹配的个人 Token 明细</div></td></tr>';
    return;
  }
  body.innerHTML = users.map((item) => {
    const summary = aggregateTokenRows(item.rows);
    const expanded = state.expandedUsers.has(item.user);
    const notedDays = item.rows.filter(row => (manualNotes[row.id] || '').trim()).length;
    const manualTotal = item.rows.reduce((sum, row) => sum + artifactCountForRow(row), 0);
    const modelText = topTextFromRows(item.rows, 'modelText');
    const sourceText = topTextFromRows(item.rows, 'sourceText');
    const rate = cacheHitRate(summary);
    const cacheClass = summary.cacheReportedRecords ? (rate >= mainstreamCacheReferenceRate ? 'metric-good' : 'metric-warn') : 'metric-warn';
    const parent = `
      <tr class="person-row" data-user="${escapeAttr(item.user)}" title="${escapeAttr(`点击查看 ${item.user} 的分日 Token 数据`)}">
        <td><button class="expand-toggle" type="button">${expanded ? '收起' : '展开'}</button><strong>${escapeHtml(item.user)}</strong><br><span class="muted small">${escapeHtml(item.email || '')}</span></td>
        <td>当前筛选范围</td>
        <td>${number(summary.recordCount)}</td>
        <td>${tokenNumber(summary.inputTokens)}</td>
        <td>${tokenNumber(summary.cacheReadTokens)}</td>
        <td class="${cacheClass}" title="${escapeAttr(cacheReportedLabel(summary))}">${tokenRate(rate)}</td>
        <td>${tokenNumber(summary.outputTokens)}</td>
        <td class="${summary.totalTokens ? 'metric-good' : 'metric-warn'}">${tokenNumber(summary.totalTokens)}</td>
        <td>${number(summary.estimatedRecords)}</td>
        <td>${escapeHtml(modelText)}</td>
        <td>${escapeHtml(sourceText)}</td>
        <td>${manualTotal || ''}</td>
        <td>${escapeHtml(notedDays ? `已填写 ${notedDays} 天备注` : (expanded ? '可在分日行填写备注' : `点击展开 ${item.rows.length} 天数据`))}</td>
      </tr>
    `;
    if (!expanded) return parent;
    const children = item.rows
      .sort((left, right) => left.date.localeCompare(right.date))
      .map((row) => {
        const stats = row.tokenStats;
        const manualNote = manualNotes[row.id] ?? '';
        const rowRate = cacheHitRate(stats);
        const rowCacheClass = stats.cacheReportedRecords ? (rowRate >= mainstreamCacheReferenceRate ? 'metric-good' : 'metric-warn') : 'metric-warn';
        return `
          <tr class="detail-child-row">
            <td><span class="muted small">分日</span></td>
            <td>${row.date}</td>
            <td>${number(stats.recordCount)}</td>
            <td>${tokenNumber(stats.inputTokens)}</td>
            <td>${tokenNumber(stats.cacheReadTokens)}</td>
            <td class="${rowCacheClass}" title="${escapeAttr(cacheReportedLabel(stats))}">${tokenRate(rowRate)}</td>
            <td>${tokenNumber(stats.outputTokens)}</td>
            <td class="${stats.totalTokens ? 'metric-good' : 'metric-warn'}">${tokenNumber(stats.totalTokens)}</td>
            <td>${number(stats.estimatedRecords)}</td>
            <td>${escapeHtml(stats.modelText)}</td>
            <td>${escapeHtml(stats.sourceText)}</td>
            <td>${escapeHtml(artifactValueForRow(row))}</td>
            <td><textarea class="manual-note compact" data-row-id="${escapeAttr(row.id)}" placeholder="手动填写备注">${escapeHtml(manualNote)}</textarea></td>
          </tr>
        `;
      }).join('');
    return parent + children;
  }).join('');
  body.querySelectorAll('.person-row').forEach((row) => {
    row.addEventListener('click', () => {
      const user = row.dataset.user;
      if (state.expandedUsers.has(user)) state.expandedUsers.delete(user);
      else state.expandedUsers.add(user);
      renderTokenPersonDetailTable(filteredTokenRows());
      mountIcons();
    });
  });
  bindManualNoteInputs(body);
}

function personalEventAnalysis(row) {
  const base = `${row.user} 在 ${row.time} 发生“${row.eventType}”，结果是${row.resultClass}`;
  return row.detail ? `${base}；${row.detail}` : base;
}

function refresh(options = {}) {
  if (options.renderFilters) {
    renderFilters();
    renderAuditFilters();
  }
  renderViewCopy();
  renderTableHeaders();
  const rows = filteredRows();
  renderKpis(rows);
  renderAuditPanel();
  renderDailyChart(rows);
  renderUserChart(rows);
  renderScenarioChart();
  renderInsights(rows);
  renderTable(rows);
  renderPersonDetailTable(rows);
  mountIcons();
}

async function loadRuntimeAudit() {
  const requestSeq = ++auditRefreshSeq;
  auditState.status = 'loading';
  auditState.error = '';
  renderAuditPanel();
  try {
    const endpoint = new URL('./api/runtime-audit?limit=80', window.location.href);
    endpoint.username = '';
    endpoint.password = '';
    if (state.auditFilters.userEmail) endpoint.searchParams.set('userEmail', state.auditFilters.userEmail);
    if (state.auditFilters.start) endpoint.searchParams.set('start', state.auditFilters.start);
    if (state.auditFilters.end) endpoint.searchParams.set('end', addDays(state.auditFilters.end, 1));
    endpoint.searchParams.set('_', String(Date.now()));
    const response = await fetch(endpoint.toString(), { cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const payload = await response.json();
    if (requestSeq !== auditRefreshSeq) return;
    auditState.data = payload.runtimeAudit || payload;
    auditState.status = 'ok';
    renderAuditPanel();
  } catch (error) {
    if (requestSeq !== auditRefreshSeq) return;
    console.error(error);
    auditState.status = 'error';
    auditState.error = String(error && error.message || error);
    renderAuditPanel();
  }
}

async function refreshLiveData() {
  const button = $('#refreshData');
  const nextRange = rangeFromInputs();
  if (!nextRange) return;
  const requestSeq = ++liveRefreshSeq;
  state.dateRange = nextRange;
  if (button) {
    button.disabled = true;
    button.classList.add('is-loading');
  }
  setLiveStatus(`正在刷新 ${nextRange.start} 至 ${nextRange.end} 的实时数据...`, 'loading');
  try {
    const endpoint = new URL('./api/data', window.location.href);
    endpoint.username = '';
    endpoint.password = '';
    endpoint.searchParams.set('start', nextRange.start);
    endpoint.searchParams.set('end', addDays(nextRange.end, 1));
    endpoint.searchParams.set('productGeneration', state.productGeneration);
    endpoint.searchParams.set('_', String(Date.now()));
    const response = await fetch(endpoint.toString(), { cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const payload = await response.json();
    if (requestSeq !== liveRefreshSeq) return;
    const nextData = payload && payload.data ? payload.data : payload;
    if (!nextData || !Array.isArray(nextData.summaryRows) || !Array.isArray(nextData.rawEvents)) {
      throw new Error('返回内容格式不正确');
    }
    const hadAllUsers = allSelected(DATA.users, state.users);
    const hadAllDates = allSelected(DATA.dates, state.dates);
    const hadAllScenarios = allSelected(DATA.scenarios, state.scenarios);
    DATA = nextData;
    state.productGeneration = DATA.meta.productGeneration || state.productGeneration;
    syncSelectionsWithData(true);
    if (hadAllUsers) state.users = new Set(DATA.users);
    if (hadAllDates) state.dates = new Set(DATA.dates);
    if (hadAllScenarios) state.scenarios = new Set(DATA.scenarios);
    if (DATA.meta.startDate && DATA.meta.endDate) {
      state.dateRange = { start: DATA.meta.startDate, end: DATA.meta.endDate };
      state.auditFilters.start = DATA.meta.startDate;
      state.auditFilters.end = DATA.meta.endDate;
      syncRangeInputs();
      syncAuditFilterInputs();
    }
    updateMetaLine();
    if (DATA.meta.rawSheetUrl) $('#rawSheetLink').href = DATA.meta.rawSheetUrl;
    refresh({ renderFilters: true });
    loadRuntimeAudit();
    const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    setLiveStatus(`已刷新 ${now}，当前为实时数据`, 'ok');
  } catch (error) {
    if (requestSeq !== liveRefreshSeq) return;
    console.error(error);
    setLiveStatus('刷新失败，仍显示当前数据', 'error');
  } finally {
    if (requestSeq !== liveRefreshSeq) return;
    if (button) {
      button.disabled = false;
      button.classList.remove('is-loading');
      mountIcons();
    }
  }
}

function csvEscape(value) {
  const text = value == null ? '' : String(value);
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function exportSummaryCsv() {
  if (state.metricView === 'tokens') {
    exportTokenSummaryCsv();
    return;
  }
  const headers = ['用户', '分日', '总任务数', '成功任务数', '部分完成', '失败任务数', '中止任务数', '进行中', '成功任务率', '平均完成时间', '人工干预次数', '干预率', '主要使用场景', '有效产物数', '无效产物数', '备注'];
  const lines = [headers];
  filteredRows().forEach(row => {
    lines.push([
      row.user,
      row.date,
      row.totalTasks,
      row.successTasks,
      partialTasksForRow(row),
      failedTasksForRow(row),
      stoppedTasksForRow(row),
      runningTasksForRow(row),
      pct(row.successRate),
      row.avgCompletionMinutes == null ? '' : row.avgCompletionMinutes,
      row.interventionCount,
      pct(row.interventionRate),
      row.mainScenario,
      artifactValueForRow(row),
      invalidArtifactsForRow(row),
      manualNotes[row.id] || ''
    ]);
  });
  downloadFile(`e-Mate_使用情况_${state.dateRange.start}_至_${state.dateRange.end}_用户分日汇总.csv`, '\uFEFF' + lines.map(line => line.map(csvEscape).join(',')).join('\n'), 'text/csv;charset=utf-8');
}

function exportTokenSummaryCsv() {
  const headers = ['用户', '分日', '用量记录数', '输入Token', '缓存命中Token', '缓存命中率', '输出Token', '总Token', '缓存上报记录数', '估算记录数', '主要模型', '用量来源', '有效产物数', '备注'];
  const lines = [headers];
  filteredTokenRows().forEach(row => {
    const stats = row.tokenStats;
    lines.push([
      row.user,
      row.date,
      stats.recordCount,
      stats.inputTokens,
      stats.cacheReadTokens,
      tokenRate(cacheHitRate(stats)),
      stats.outputTokens,
      stats.totalTokens,
      stats.cacheReportedRecords,
      stats.estimatedRecords,
      stats.modelText,
      stats.sourceText,
      artifactValueForRow(row),
      manualNotes[row.id] || ''
    ]);
  });
  downloadFile(`e-Mate_Token用量_${state.dateRange.start}_至_${state.dateRange.end}_用户分日汇总.csv`, '\uFEFF' + lines.map(line => line.map(csvEscape).join(',')).join('\n'), 'text/csv;charset=utf-8');
}

function exportRawCsv() {
  const headers = ['序号', '用户', '邮箱', '日期', '时间', '事件类型', '结果分类', '状态', '来源', '请求ID', '会话ID', '设备标识', '个人事件分析', '原始事件类型', '原始状态'];
  const lines = [headers];
  filteredRawEvents().forEach(row => {
    lines.push([row.seq, row.user, row.email, row.date, row.time, row.eventType, row.resultClass, row.status, row.source, row.requestId, row.sessionId, row.device, personalEventAnalysis(row), row.rawEventType, row.rawStatus]);
  });
  downloadFile(`e-Mate_使用情况_${state.dateRange.start}_至_${state.dateRange.end}_RAW中文明细.csv`, '\uFEFF' + lines.map(line => line.map(csvEscape).join(',')).join('\n'), 'text/csv;charset=utf-8');
}

function exportRawJson() {
  const rows = filteredRawEvents().map(row => ({ ...row, personalEventAnalysis: personalEventAnalysis(row) }));
  downloadFile(`e-Mate_使用情况_${state.dateRange.start}_至_${state.dateRange.end}_RAW中文明细.json`, JSON.stringify(rows, null, 2), 'application/json;charset=utf-8');
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[ch]);
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function init() {
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.filter')) {
      document.querySelectorAll('.filter.open').forEach(item => item.classList.remove('open'));
    }
  });
  updateMetaLine();
  syncRangeInputs();
  setLiveStatus(DATA.meta.live ? '当前为实时数据' : '当前为快照数据，可点击刷新');
  $('#rawSheetLink').href = DATA.meta.rawSheetUrl;
  $('#refreshData').addEventListener('click', refreshLiveData);
  $('#applyRange').addEventListener('click', refreshLiveData);
  $('#productGeneration').addEventListener('change', (event) => {
    state.productGeneration = event.target.value || 'all';
    refreshLiveData();
  });
  $('#applyAuditFilters').addEventListener('click', () => {
    const nextRange = auditRangeFromInputs();
    if (!nextRange) return;
    state.auditFilters = {
      userEmail: $('#auditUserFilter').value || '',
      start: nextRange.start,
      end: nextRange.end
    };
    renderAuditFilterSummary();
    loadRuntimeAudit();
  });
  $('#resetAuditFilters').addEventListener('click', () => {
    state.auditFilters = {
      userEmail: '',
      start: state.dateRange.start,
      end: state.dateRange.end
    };
    syncAuditFilterInputs();
    renderAuditFilterSummary();
    loadRuntimeAudit();
  });
  ['#rangeStart', '#rangeEnd'].forEach((selector) => {
    $(selector).addEventListener('keydown', (event) => {
      if (event.key === 'Enter') refreshLiveData();
    });
  });
  ['#auditRangeStart', '#auditRangeEnd'].forEach((selector) => {
    $(selector).addEventListener('keydown', (event) => {
      if (event.key === 'Enter') $('#applyAuditFilters').click();
    });
  });
  $('#auditUserFilter').addEventListener('change', () => {
    const nextRange = auditRangeFromInputs();
    if (!nextRange) return;
    state.auditFilters = {
      userEmail: $('#auditUserFilter').value || '',
      start: nextRange.start,
      end: nextRange.end
    };
    renderAuditFilterSummary();
    loadRuntimeAudit();
  });
  $('#exportSummary').addEventListener('click', exportSummaryCsv);
  $('#exportRawCsv').addEventListener('click', exportRawCsv);
  $('#exportRawJson').addEventListener('click', exportRawJson);
  document.querySelectorAll('[data-metric-view]').forEach((button) => {
    button.addEventListener('click', () => {
      state.metricView = button.dataset.metricView || 'tasks';
      refresh();
    });
  });
  $('#resetFilters').addEventListener('click', () => {
    syncSelectionsWithData(false);
    refresh({ renderFilters: true });
  });
  refresh({ renderFilters: true });
  loadRuntimeAudit();
  window.setTimeout(refreshLiveData, 0);
}

init();
