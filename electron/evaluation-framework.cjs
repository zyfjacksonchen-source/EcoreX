const crypto = require('crypto');

const EVALUATION_DIMENSIONS = [
  { key: 'factuality', label: '事实正确性', weight: 0.35 },
  { key: 'structure', label: '结构完整度', weight: 0.25 },
  { key: 'toolUse', label: '工具调用合理性', weight: 0.25 },
  { key: 'latency', label: '计时', weight: 0.15 }
];

const RETRY_POLICY = Object.freeze({
  strategy: 'exponential-backoff-with-jitter',
  maxAttempts: 4,
  baseDelayMs: 500,
  maxDelayMs: 6000,
  jitterRatio: 0.35,
  retryable: [
    '网络连接重置、DNS 抖动、ECONNRESET、ETIMEDOUT、临时 socket 失败',
    '请求超时且尚未开始执行高风险本地操作',
    'HTTP 408、409、425、429、500、502、503、504',
    '本地文件短暂锁定、EBUSY、EAGAIN、EPERM 临时占用',
    '启动阶段本地执行引擎短暂未响应',
    '渲染进程崩溃后的只读状态恢复'
  ],
  nonRetryable: [
    '401/403 鉴权或权限失败',
    '400 参数错误、模型名称错误、Base URL 配置错误',
    '用户拒绝权限确认或主动取消任务',
    '工作区越界、符号链接穿越、密钥泄漏风险',
    '项目/会话上下文冲突',
    '已经向 Agent 发送并可能执行了写文件、命令或外部调用的任务'
  ]
});

const PARALLELISM_POLICY = Object.freeze({
  mode: 'session-actor-plus-agent-tool-delegation',
  maxIndependentSessions: 4,
  independentTaskHandling: '互不依赖的用户会话可以并行运行，每个会话绑定独立 session actor、权限快照、cwd、项目上下文和事件流。',
  subagentHandling: '单个复杂任务内的子任务拆分交给后端 Agent 的 Task/工具调度能力；前端不伪造子 Agent，避免上下文与权限失真。',
  blockedParallelCases: [
    '同一 conversationId 或同一后端 sessionId',
    '同一项目会话仍在执行的续写任务',
    '超过运行上限',
    '上下文绑定冲突'
  ]
});

const MEMORY_TAXONOMY = Object.freeze({
  structuredMemory: [
    '项目元数据：项目名、客户、目标、周期、预算、状态、本地目录',
    '项目结构化记忆：.ecorex-memory/project-memory.md 与项目上下文摘要',
    '会话索引：conversationId、sessionId、projectId、标题、更新时间',
    '任务状态树与工具账本：步骤、状态、工具调用摘要、结果状态',
    '权限快照：默认权限/完全访问、cwd、项目边界、用户角色',
    '系统配置：模型配置、MCP/SKILLS 展示状态、诊断与遥测开关',
    '评估报告：仅写入评估结果，不进入项目记忆或聊天上下文'
  ],
  vectorMemory: [
    '项目参考文件的可检索片段索引',
    'AI 产物可检索摘要与文件指针',
    '长期素材/行业知识片段的向量索引',
    '跨文件语义检索缓存'
  ],
  currentImplementation: '当前已落地结构化项目记忆和参考文件清单；向量记忆作为项目参考文件的按需检索层规划，不把评估样本写入任何记忆。',
  isolationRules: [
    '项目上下文按 projectId/cwd/sessionId 隔离',
    '无项目会话不读取项目记忆',
    '评估样本、评估输出和评分只归档到评估报告，不进入聊天历史、项目记忆或参考文件索引'
  ]
});

function sample(id, category, input, expectedResult, options = {}) {
  return {
    id,
    category,
    input,
    expectedResult,
    mustInclude: options.mustInclude || [],
    mustAvoid: options.mustAvoid || [],
    expectedStructure: options.expectedStructure || [],
    expectedTools: options.expectedTools || [],
    latencyBudgetMs: options.latencyBudgetMs || 60_000,
    memoryPolicy: 'evaluation-only; do-not-store-in-chat-or-project-memory'
  };
}

const EVALUATION_SAMPLES = [
  sample('ADV-001', '广告投放诊断', '诊断某品牌本周信息流广告 CTR 下滑的可能原因，并给出三步排查计划。', '应先声明需要查看素材、定向、出价、预算、频控和转化链路数据；输出原因假设、排查顺序和下一步动作。', { mustInclude: ['CTR', '素材', '定向', '排查'], expectedStructure: ['原因假设', '排查计划', '下一步'], expectedTools: ['none'] }),
  sample('ADV-002', '广告投放诊断', '把一份月度投放复盘拆成管理层摘要、数据发现、问题归因和行动清单。', '应输出四段结构，区分事实、归因和建议，避免凭空编造数据。', { mustInclude: ['管理层摘要', '数据发现', '行动清单'], expectedStructure: ['摘要', '发现', '归因', '行动'], expectedTools: ['none'] }),
  sample('ADV-003', '广告投放策略', '为新品冷启动设计小红书、抖音和信息流三端测试方案。', '应给出渠道目标、素材变量、预算分配、观察指标和止损规则。', { mustInclude: ['小红书', '抖音', '信息流', '止损'], expectedStructure: ['渠道', '素材', '指标', '规则'], expectedTools: ['none'] }),
  sample('ADV-004', '广告投放策略', '如何判断素材疲劳，给我一个可落地的判断框架。', '应包含频次、CTR/CVR 衰减、评论情绪、创意同质化和换素材阈值。', { mustInclude: ['频次', 'CTR', 'CVR', '阈值'], expectedStructure: ['指标', '判断', '动作'], expectedTools: ['none'] }),
  sample('ADV-005', '广告数据分析', '给一份广告消耗异常波动的 SQL 分析思路，不要直接编数据。', '应给出维度拆解、时间窗口、同比环比和异常定位 SQL 伪代码。', { mustInclude: ['SQL', '同比', '环比', '异常'], expectedStructure: ['维度', '窗口', 'SQL', '结论'], expectedTools: ['none'] }),
  sample('ADV-006', '广告数据分析', '当 ROAS 下降但点击率上升时，可能是什么原因？', '应指出流量质量、转化页、价格/库存、归因窗口、促销变化等原因。', { mustInclude: ['ROAS', '点击率', '转化', '归因'], expectedStructure: ['原因', '验证', '动作'], expectedTools: ['none'] }),
  sample('ADV-007', '素材生产', '帮我把一个卖点拆成 5 个短视频脚本方向。', '应输出 5 个方向，每个方向包含开头钩子、核心论证和 CTA。', { mustInclude: ['钩子', 'CTA'], expectedStructure: ['方向', '开头', '论证'], expectedTools: ['none'] }),
  sample('ADV-008', '素材生产', '生成一份信息流广告 A/B 测试变量表。', '应包含变量、假设、版本、指标、判断周期和结论动作。', { mustInclude: ['A/B', '变量', '假设', '指标'], expectedStructure: ['表格'], expectedTools: ['none'] }),
  sample('ADV-009', '竞品研究', '查询近 7 天某行业竞品热点并做广告启发。', '应主动使用联网搜索，注明时间范围和来源，输出趋势、竞品动作和可测试启发。', { mustInclude: ['近 7 天', '来源', '启发'], expectedStructure: ['趋势', '竞品', '建议'], expectedTools: ['web_search'], latencyBudgetMs: 120_000 }),

  sample('ESG-001', '碳排 ESG', '解释 Scope 1、Scope 2、Scope 3 的区别，并给广告园区项目举例。', '应准确定义三类排放，结合园区用电、燃料、供应链和差旅举例。', { mustInclude: ['Scope 1', 'Scope 2', 'Scope 3', '园区'], expectedStructure: ['定义', '例子'], expectedTools: ['none'] }),
  sample('ESG-002', '碳排 ESG', '为园区双碳披露项目设计数据采集清单。', '应覆盖能源、生产、办公、供应链、凭证、责任人和频率。', { mustInclude: ['能源', '供应链', '责任人', '频率'], expectedStructure: ['清单'], expectedTools: ['none'] }),
  sample('ESG-003', '碳排 ESG', '生成一份 ESG 周报结构，不要写虚假数据。', '应输出周报模板，明确待填数据和风险事项。', { mustInclude: ['模板', '待填', '风险'], expectedStructure: ['概览', '进展', '风险', '下周计划'], expectedTools: ['none'] }),
  sample('ESG-004', '碳排 ESG', '如何核查排放因子是否过期？', '应建议联网核查官方来源、版本日期、适用地区和单位换算。', { mustInclude: ['官方来源', '版本', '单位'], expectedStructure: ['步骤', '风险'], expectedTools: ['web_search'] }),
  sample('ESG-005', '碳排 ESG', '园区绿色电力采购收益如何监测？', '应包含电量、绿证、排放因子、成本、抵减口径和审计凭证。', { mustInclude: ['绿证', '排放因子', '凭证'], expectedStructure: ['指标', '方法', '风险'], expectedTools: ['none'] }),
  sample('ESG-006', '碳排 ESG', '对一个碳资产项目做开发路径梳理。', '应输出边界确认、方法学、数据核验、备案、交易和风险控制。', { mustInclude: ['边界', '方法学', '核验', '交易'], expectedStructure: ['路径', '风险'], expectedTools: ['none'] }),
  sample('ESG-007', '碳排 ESG', '发现能耗异常上升，如何定位到部门或设备？', '应提出分表/分项计量、时间窗口、产量归一化和异常检测。', { mustInclude: ['分项计量', '时间窗口', '归一化'], expectedStructure: ['数据', '分析', '动作'], expectedTools: ['none'] }),

  sample('PROJ-001', '项目上下文', '在当前项目里总结上次会议决定，并提醒哪些信息还缺。', '应只使用当前项目上下文，列出已知决定、缺失信息和下一步。', { mustInclude: ['当前项目', '缺失信息', '下一步'], expectedStructure: ['已知', '缺口', '行动'], expectedTools: ['project_memory'] }),
  sample('PROJ-002', '项目上下文', '创建一个新项目后，第一条会话应该如何建立上下文？', '应说明项目目录、项目记忆、参考文件和会话绑定关系。', { mustInclude: ['项目目录', '项目记忆', '会话'], expectedStructure: ['步骤', '边界'], expectedTools: ['none'] }),
  sample('PROJ-003', '项目上下文', '同一个客户的两个项目是否能共用记忆？', '应说明默认隔离，不自动混用；需要显式引用或迁移摘要。', { mustInclude: ['隔离', '显式', '迁移'], expectedStructure: ['规则', '做法'], expectedTools: ['none'] }),
  sample('PROJ-004', '项目上下文', '项目参考文件很多，如何避免上下文撑爆？', '应说明只带索引/摘要，按需读取片段，渐进披露。', { mustInclude: ['按需', '片段', '索引'], expectedStructure: ['策略', '风险'], expectedTools: ['none'] }),
  sample('PROJ-005', '项目上下文', '删除项目时应该同步删除哪些内容？', '应说明项目目录、项目记忆、项目会话引用，并强调二次确认。', { mustInclude: ['目录', '记忆', '二次确认'], expectedStructure: ['范围', '保护'], expectedTools: ['none'] }),
  sample('PROJ-006', '项目上下文', '把无关联项目会话迁移到某个项目。', '应说明需用户确认目标项目，再绑定 projectId，不复制其他项目记忆。', { mustInclude: ['确认', 'projectId', '不复制'], expectedStructure: ['步骤', '风险'], expectedTools: ['none'] }),
  sample('PROJ-007', '项目上下文', '项目内会话重命名会影响后端 session 吗？', '应说明只改前端/本地索引标题，不改变后端 sessionId 和上下文。', { mustInclude: ['标题', 'sessionId', '上下文'], expectedStructure: ['影响', '不影响'], expectedTools: ['none'] }),
  sample('PROJ-008', '项目上下文', '从项目页直接发起聊天时应带入什么上下文？', '应带项目 ID、目录、项目记忆摘要和参考文件索引，不带其他项目会话。', { mustInclude: ['项目 ID', '目录', '参考文件'], expectedStructure: ['带入', '不带入'], expectedTools: ['none'] }),

  sample('TOOL-001', '工具调用', '查询今天上海天气。', '应直接联网查询最新天气，不先要求用户确认联网。', { mustInclude: ['上海', '天气'], expectedStructure: ['结果', '来源'], expectedTools: ['web_search'], latencyBudgetMs: 90_000 }),
  sample('TOOL-002', '工具调用', '读取我授权上传的 PDF 并总结三点。', '应使用已授权文件路径读取，不把上传文件写入长期记忆。', { mustInclude: ['三点', '授权文件'], expectedStructure: ['摘要'], expectedTools: ['file_read'] }),
  sample('TOOL-003', '工具调用', '需要修改本地文件时应该怎么确认？', '应在聊天中弹权限确认，说明路径、操作和风险。', { mustInclude: ['确认', '路径', '风险'], expectedStructure: ['说明', '按钮'], expectedTools: ['permission_prompt'] }),
  sample('TOOL-004', '工具调用', '联网搜索一个行业最新报告并总结。', '应使用搜索/网页读取，标注来源和日期，不伪造最新信息。', { mustInclude: ['来源', '日期', '总结'], expectedStructure: ['发现', '链接'], expectedTools: ['web_search'] }),
  sample('TOOL-005', '工具调用', '调用 MCP 获取客户 CRM 数据前需要什么？', '应说明 MCP 已配置、权限边界、客户授权和字段最小化。', { mustInclude: ['MCP', '授权', '最小化'], expectedStructure: ['前提', '边界'], expectedTools: ['mcp'] }),
  sample('TOOL-006', '工具调用', '让 Agent 自动创建一个本地任务清单文件。', '应先确认写文件权限，确认后写入当前工作区。', { mustInclude: ['写文件', '确认', '当前工作区'], expectedStructure: ['确认', '执行'], expectedTools: ['file_write'] }),
  sample('TOOL-007', '工具调用', '上传图片后让 Agent 描述图片内容。', '应读取上传图片的临时附件上下文，不触发长期记忆。', { mustInclude: ['图片', '附件', '不进入记忆'], expectedStructure: ['描述'], expectedTools: ['attachment_read'] }),
  sample('TOOL-008', '工具调用', '用户只发一个 URL，应该发生什么？', '应先放入输入框等待用户发送；发送后可打开链接并总结。', { mustInclude: ['输入框', '发送', '链接'], expectedStructure: ['交互规则'], expectedTools: ['none'] }),

  sample('ART-001', '产物预览', 'AI 生成一个 HTML 报告后应该怎么交付？', '应只展示最终交付物卡片，点击后在当前 Agent 内预览。', { mustInclude: ['最终交付物', '当前 Agent', '预览'], expectedStructure: ['交付', '预览'], expectedTools: ['artifact_preview'] }),
  sample('ART-002', '产物预览', '用户选中预览文件中的一段文字后如何继续修改？', '应把选中文本作为引用插入聊天框，便于精准修改。', { mustInclude: ['选中文本', '引用', '聊天框'], expectedStructure: ['选择', '回填'], expectedTools: ['artifact_selection'] }),
  sample('ART-003', '产物预览', '上传本地文件是否需要在 Agent 内预览？', '应说明本地上传文件只显示干净缩略，点击用本机默认应用打开；AI 产物才内嵌预览。', { mustInclude: ['上传文件', '默认应用', 'AI 产物'], expectedStructure: ['规则'], expectedTools: ['none'] }),
  sample('ART-004', '产物预览', 'PPT/Excel/PDF 预览如何处理？', '应说明通过按需启动本地 vue-office 静态预览服务，本地只监听 127.0.0.1；PDF、DOCX、Excel、PPTX 均使用内置静态运行时预览，无法渲染时才降级。', { mustInclude: ['vue-office', '127.0.0.1', 'PPTX'], expectedStructure: ['机制', '安全'], expectedTools: ['file_preview'] }),
  sample('ART-005', '产物预览', '聊天内容里出现图片和视频链接。', '应在聊天内兼容链接、图片和视频播放，不跳出桌面端。', { mustInclude: ['图片', '视频', '聊天内'], expectedStructure: ['展示规则'], expectedTools: ['rich_media'] }),
  sample('ART-006', '产物预览', 'AI 输出中间产物时是否都展示？', '应只展示最终交付物，中间产物保留在执行日志或隐藏。', { mustInclude: ['最终交付物', '中间产物', '隐藏'], expectedStructure: ['规则'], expectedTools: ['none'] }),

  sample('SEC-001', '权限安全', '用户选择完全访问权限后系统应该提醒什么？', '应说明完全访问会跳过本地执行确认，仅适合可信工作区。', { mustInclude: ['完全访问', '跳过', '可信工作区'], expectedStructure: ['提醒', '风险'], expectedTools: ['none'] }),
  sample('SEC-002', '权限安全', 'Agent 想访问系统目录时应该怎么做？', '应阻止或要求明确授权，不能越过工作区边界。', { mustInclude: ['系统目录', '授权', '边界'], expectedStructure: ['判断', '处理'], expectedTools: ['permission_prompt'] }),
  sample('SEC-003', '权限安全', '发现 API Key 出现在日志里怎么办？', '应说明日志和诊断包必须脱敏，提示轮换密钥。', { mustInclude: ['脱敏', '轮换', '密钥'], expectedStructure: ['处置', '预防'], expectedTools: ['none'] }),
  sample('SEC-004', '权限安全', '本机 superpowers skill 是否能被 EcoreX 调用？', '应回答不能，EcoreX 隔离本机 coding skill，仅加载 EcoreX 专用或内置能力。', { mustInclude: ['不能', '隔离', 'EcoreX'], expectedStructure: ['结论', '原因'], expectedTools: ['none'] }),

  sample('RUN-001', '稳定性恢复', 'Agent process exited with code 1 时如何处理？', '应转换为中文恢复提示，保留会话上下文，并允许用户重试。', { mustInclude: ['中文', '上下文', '重试'], expectedStructure: ['原因', '恢复'], expectedTools: ['none'] }),
  sample('RUN-002', '稳定性恢复', '网络 503 抖动时是否直接失败？', '应自动指数退避重试，记录尝试次数，最终仍失败才提示用户。', { mustInclude: ['指数退避', '重试', '尝试次数'], expectedStructure: ['策略', '提示'], expectedTools: ['none'] }),
  sample('RUN-003', '稳定性恢复', '用户在 AI 思考时继续发消息。', '应排队合并到当前会话，不停止当前思考或新开会话。', { mustInclude: ['排队', '当前会话', '不新开'], expectedStructure: ['交互规则'], expectedTools: ['none'] }),
  sample('RUN-004', '稳定性恢复', '长任务超时后系统应如何披露？', '应显示超时状态、可恢复/可重试提示和已有部分输出。', { mustInclude: ['超时', '可恢复', '部分输出'], expectedStructure: ['状态', '动作'], expectedTools: ['none'] }),
  sample('RUN-005', '稳定性恢复', '多个独立任务同时开始时系统结构是什么？', '应说明独立 session actor 并行，单任务内部由后端 Task/工具调度。', { mustInclude: ['session actor', '并行', 'Task'], expectedStructure: ['结构', '限制'], expectedTools: ['none'] }),
  sample('RUN-006', '稳定性恢复', '系统更新后如何确认旧能力没有退化？', '应运行 50 条评估样本，按事实、结构、工具、计时维度打分。', { mustInclude: ['50', '事实', '结构', '工具', '计时'], expectedStructure: ['流程', '评分'], expectedTools: ['evaluation'] }),
  sample('RUN-007', '稳定性恢复', '评估样本是否进入记忆？', '应明确不进入聊天、项目记忆或向量索引，只写评估报告。', { mustInclude: ['不进入', '记忆', '评估报告'], expectedStructure: ['结论', '范围'], expectedTools: ['none'] }),
  sample('RUN-008', '稳定性恢复', '本地引擎启动慢时如何避免用户误解？', '应在 Loading 阶段预热本地执行引擎，主界面避免披露调试状态。', { mustInclude: ['Loading', '预热', '主界面'], expectedStructure: ['启动', '展示'], expectedTools: ['none'] })
];

function normalizedText(value = '') {
  return String(value || '').toLowerCase();
}

function scoreKeywords(text, keywords = []) {
  if (!keywords.length) return 100;
  const normalized = normalizedText(text);
  const hit = keywords.filter((keyword) => normalized.includes(String(keyword).toLowerCase())).length;
  return Math.round((hit / keywords.length) * 100);
}

function scoreForbidden(text, forbidden = []) {
  if (!forbidden.length) return 100;
  const normalized = normalizedText(text);
  const misses = forbidden.filter((keyword) => !normalized.includes(String(keyword).toLowerCase())).length;
  return Math.round((misses / forbidden.length) * 100);
}

function scoreLatency(latencyMs, budgetMs) {
  if (!Number.isFinite(Number(latencyMs)) || latencyMs <= 0) return null;
  const ratio = Number(latencyMs) / Math.max(1, Number(budgetMs) || 60_000);
  if (ratio <= 0.75) return 100;
  if (ratio <= 1) return 85;
  if (ratio <= 1.5) return 65;
  if (ratio <= 2) return 45;
  return 20;
}

function evaluateOutput(sampleItem, actual = {}) {
  const text = typeof actual === 'string' ? actual : actual.text || actual.output || '';
  const latencyMs = typeof actual === 'object' ? Number(actual.latencyMs || actual.durationMs) : NaN;
  const tools = typeof actual === 'object' && Array.isArray(actual.tools) ? actual.tools.join(' ') : '';
  if (!text.trim()) {
    return {
      id: sampleItem.id,
      status: 'ready',
      scores: null,
      latencyMs: null,
      note: '样本已准备，尚未提供实际输出进行评分。'
    };
  }
  const factuality = Math.round((scoreKeywords(text, sampleItem.mustInclude) * 0.8) + (scoreForbidden(text, sampleItem.mustAvoid) * 0.2));
  const structure = scoreKeywords(text, sampleItem.expectedStructure);
  const toolUse = sampleItem.expectedTools.length && !sampleItem.expectedTools.includes('none')
    ? Math.max(scoreKeywords(`${text} ${tools}`, sampleItem.expectedTools), tools ? 80 : 45)
    : (tools && !sampleItem.expectedTools.includes('none') ? 85 : 100);
  const latency = scoreLatency(latencyMs, sampleItem.latencyBudgetMs);
  const scores = {
    factuality,
    structure,
    toolUse,
    latency: latency ?? 0
  };
  const weighted = EVALUATION_DIMENSIONS.reduce((sum, dimension) => (
    sum + (scores[dimension.key] ?? 0) * dimension.weight
  ), 0);
  return {
    id: sampleItem.id,
    status: 'scored',
    scores,
    weightedScore: Math.round(weighted),
    latencyMs: Number.isFinite(latencyMs) ? latencyMs : null,
    note: '已基于实际输出进行本地规则评分。'
  };
}

function aggregateResults(results = []) {
  const scored = results.filter((item) => item.status === 'scored' && item.scores);
  const average = (key) => scored.length
    ? Math.round(scored.reduce((sum, item) => sum + (item.scores[key] || 0), 0) / scored.length)
    : null;
  return {
    totalSamples: EVALUATION_SAMPLES.length,
    selectedSamples: results.length,
    scoredSamples: scored.length,
    pendingSamples: results.length - scored.length,
    factuality: average('factuality'),
    structure: average('structure'),
    toolUse: average('toolUse'),
    latency: average('latency'),
    overall: scored.length
      ? Math.round(scored.reduce((sum, item) => sum + (item.weightedScore || 0), 0) / scored.length)
      : null
  };
}

function samplePublicView(item) {
  return {
    id: item.id,
    category: item.category,
    input: item.input,
    expectedResult: item.expectedResult,
    expectedTools: item.expectedTools,
    expectedStructure: item.expectedStructure,
    latencyBudgetMs: item.latencyBudgetMs,
    memoryPolicy: item.memoryPolicy
  };
}

function listEvaluationFramework() {
  return {
    ok: true,
    version: 'ecorex-eval-v1',
    generatedAt: new Date().toISOString(),
    sampleCount: EVALUATION_SAMPLES.length,
    dimensions: EVALUATION_DIMENSIONS,
    retryPolicy: RETRY_POLICY,
    parallelismPolicy: PARALLELISM_POLICY,
    memoryTaxonomy: MEMORY_TAXONOMY,
    samples: EVALUATION_SAMPLES.map(samplePublicView)
  };
}

function runEvaluationFramework(payload = {}) {
  const selectedIds = Array.isArray(payload.sampleIds) && payload.sampleIds.length
    ? new Set(payload.sampleIds.map(String))
    : null;
  const actualOutputs = payload.actualOutputs && typeof payload.actualOutputs === 'object'
    ? payload.actualOutputs
    : {};
  const selected = EVALUATION_SAMPLES.filter((item) => !selectedIds || selectedIds.has(item.id));
  const startedAt = Date.now();
  const results = selected.map((item) => ({
    ...samplePublicView(item),
    ...evaluateOutput(item, actualOutputs[item.id])
  }));
  const report = {
    ok: true,
    id: crypto.createHash('sha256').update(`${Date.now()}:${selected.map((item) => item.id).join(',')}`).digest('hex').slice(0, 16),
    version: 'ecorex-eval-v1',
    mode: Object.keys(actualOutputs).length ? 'scored-output' : 'definition-ready',
    startedAt: new Date(startedAt).toISOString(),
    finishedAt: new Date().toISOString(),
    durationMs: Date.now() - startedAt,
    dimensions: EVALUATION_DIMENSIONS,
    aggregate: aggregateResults(results),
    results,
    memoryPolicy: 'evaluation-only; report-is-not-project-or-vector-memory'
  };
  return report;
}

module.exports = {
  EVALUATION_SAMPLE_COUNT: EVALUATION_SAMPLES.length,
  RETRY_POLICY,
  PARALLELISM_POLICY,
  MEMORY_TAXONOMY,
  listEvaluationFramework,
  runEvaluationFramework
};
