import { useState, type ReactNode } from "react";

export type ToolCallDisclosure = {
  name?: string;
  arguments?: unknown;
  result?: unknown;
  is_error?: boolean;
  status?: string;
  execution_time?: number;
  running?: boolean;
};

export type AgentStepDisclosure =
  | {
      type: "thinking";
      content?: string;
      running?: boolean;
      startedAt?: number;
      duration?: number;
    }
  | {
      type: "content";
      content?: string;
      intermediate?: boolean;
    }
  | {
      type: "tool";
      name?: string;
      arguments?: unknown;
      result?: unknown;
      status?: string;
      execution_time?: number;
      is_error?: boolean;
      running?: boolean;
    }
  | {
      type: "phase";
      content?: string;
    }
  | {
      type: "media";
      fileType?: "image" | "video" | "file";
      url?: string;
      fileName?: string;
    };

type LocalFilePayload = {
  file_path: string;
  file_name: string;
  file_type?: "image" | "video" | "file";
};

const REASONING_RENDER_CAP = 4 * 1024;
const LONG_REPLY_COLLAPSE_CHARS = 1400;
const LONG_REPLY_PREVIEW_CHARS = 520;

function escapeHtml(value: unknown) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(value: string) {
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function safeImageUrl(value: string) {
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:"].includes(url.protocol) || url.href.startsWith("data:image/") ? url.href : "";
  } catch {
    return "";
  }
}

function localPathFromSource(value?: string) {
  const source = String(value || "").trim();
  if (!source) return "";
  if (/^file:\/\//i.test(source)) {
    try {
      return decodeURIComponent(source.replace(/^file:\/+/i, ""));
    } catch {
      return source.replace(/^file:\/+/i, "");
    }
  }
  if (/^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("\\\\") || source.startsWith("/")) {
    return source;
  }
  return "";
}

function basenameFromPath(value: string) {
  const normalized = value.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() || value;
}

function renderInline(value: string) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_match, alt: string, href: string) => {
    const src = safeImageUrl(href);
    if (!src) return escapeHtml(alt);
    return `<img class="markdown-image" src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy" />`;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label: string, href: string) => {
    return `<a href="${escapeHtml(safeUrl(href))}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
  });
  return html;
}

function flushParagraph(lines: string[], out: string[]) {
  if (!lines.length) return;
  out.push(`<p>${renderInline(lines.join(" "))}</p>`);
  lines.length = 0;
}

function renderList(items: string[], ordered: boolean, out: string[]) {
  if (!items.length) return;
  const tag = ordered ? "ol" : "ul";
  out.push(`<${tag}>${items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${tag}>`);
  items.length = 0;
}

function renderMarkdown(markdown: string) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  const paragraph: string[] = [];
  const bullets: string[] = [];
  const numbers: string[] = [];
  let code: string[] | null = null;

  const flushAll = () => {
    flushParagraph(paragraph, out);
    renderList(bullets, false, out);
    renderList(numbers, true, out);
  };

  for (const raw of lines) {
    const fence = raw.match(/^```([\w-]*)\s*$/);
    if (fence) {
      if (code) {
        out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = null;
      } else {
        flushAll();
        code = [];
      }
      continue;
    }

    if (code) {
      code.push(raw);
      continue;
    }

    if (!raw.trim()) {
      flushAll();
      continue;
    }

    const heading = raw.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushAll();
      const level = Math.min(heading[1].length + 2, 5);
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = raw.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph(paragraph, out);
      renderList(numbers, true, out);
      bullets.push(bullet[1]);
      continue;
    }

    const numbered = raw.match(/^\s*\d+\.\s+(.+)$/);
    if (numbered) {
      flushParagraph(paragraph, out);
      renderList(bullets, false, out);
      numbers.push(numbered[1]);
      continue;
    }

    renderList(bullets, false, out);
    renderList(numbers, true, out);
    paragraph.push(raw.trim());
  }

  if (code) {
    out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  }
  flushAll();
  return out.join("");
}

function truncateReasoning(text: string) {
  if (text.length <= REASONING_RENDER_CAP) return { text, truncated: false };
  const half = Math.floor(REASONING_RENDER_CAP / 2);
  const omitted = text.length - REASONING_RENDER_CAP;
  return {
    text: `${text.slice(0, half)}\n\n... [${omitted} chars omitted] ...\n\n${text.slice(-half)}`,
    truncated: true
  };
}

function formatToolValue(value: unknown) {
  if (value === undefined || value === null || value === "") return "(none)";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function MarkdownBlock({ content }: { content: string }) {
  return <div className="markdown-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />;
}

function ThinkingStep({ content = "", running }: { content?: string; running?: boolean }) {
  const trimmed = content.trim();
  const shown = truncateReasoning(trimmed);
  return (
    <details className={`agent-step agent-thinking-step${running ? " is-running" : ""}`} open={running || undefined}>
      <summary title={running ? "EcoreX 正在推理，完成后可展开查看思考摘要" : "展开查看本轮思考摘要"}>
        <span className="thinking-ring" aria-hidden="true" />
        <span>{running ? "思考中" : "思考完成"}</span>
      </summary>
      <div className="thinking-full">
        {shown.truncated ? <pre className="thinking-stream-pre">{shown.text}</pre> : <MarkdownBlock content={shown.text} />}
      </div>
    </details>
  );
}

function ToolStep({ step }: { step: ToolCallDisclosure }) {
  const isError = step.is_error === true || step.status === "error" || step.status === "failed";
  const running = step.running === true || step.status === "running";
  return (
    <details className={`agent-step agent-tool-step${isError ? " tool-failed" : ""}`} open={running || undefined}>
      <summary title={running ? "工具正在执行，完成后可查看输入和结果" : "展开查看工具输入和结果"}>
        <span className={`tool-status-dot${running ? " is-running" : isError ? " is-error" : " is-done"}`} aria-hidden="true" />
        <span className="tool-name">{step.name || "工具调用"}</span>
        {typeof step.execution_time === "number" && <span className="tool-time">{step.execution_time}s</span>}
      </summary>
      <div className="tool-detail">
        <div className="tool-detail-section">
          <div className="tool-detail-label">Input</div>
          <pre className="tool-detail-content">{formatToolValue(step.arguments)}</pre>
        </div>
        {step.result !== undefined && step.result !== "" && (
          <div className="tool-detail-section tool-output-section">
            <div className="tool-detail-label">{isError ? "Error" : "Output"}</div>
            <pre className={`tool-detail-content${isError ? " tool-error-text" : ""}`}>{formatToolValue(step.result)}</pre>
          </div>
        )}
      </div>
    </details>
  );
}

function MediaStep({ step, onOpenLocalFile, localFilePreviewUrl }: {
  step: Extract<AgentStepDisclosure, { type: "media" }>;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  if (!step.url) return null;
  const localPath = localPathFromSource(step.url);
  if (localPath && step.fileType === "image" && localFilePreviewUrl) {
    const fileName = step.fileName || basenameFromPath(localPath);
    const image = <img className="agent-media-image" src={localFilePreviewUrl(localPath)} alt={fileName} />;
    if (!onOpenLocalFile) return image;
    return (
      <button
        type="button"
        className="agent-media-button"
        onClick={() => onOpenLocalFile({ file_path: localPath, file_name: fileName, file_type: "image" })}
        title="点击在本地打开"
      >
        {image}
      </button>
    );
  }
  if (localPath && onOpenLocalFile) {
    const fileName = step.fileName || basenameFromPath(localPath);
    return (
      <button
        type="button"
        className="agent-file-link"
        onClick={() => onOpenLocalFile({ file_path: localPath, file_name: fileName, file_type: step.fileType || "file" })}
        title="点击在本地打开"
      >
        {fileName}
      </button>
    );
  }
  if (step.fileType === "image") {
    return <img className="agent-media-image" src={step.url} alt={step.fileName || "image"} />;
  }
  if (step.fileType === "video") {
    return <video className="agent-media-video" src={step.url} controls />;
  }
  return (
    <a className="agent-file-link" href={step.url} target="_blank" rel="noreferrer">
      {step.fileName || step.url}
    </a>
  );
}

function compactToolSteps(steps: AgentStepDisclosure[]) {
  const compacted: AgentStepDisclosure[] = [];
  const toolIndexes = new Map<string, number>();
  const toolCounts = new Map<string, number>();
  for (const step of steps) {
    if (step.type !== "tool") {
      compacted.push(step);
      continue;
    }
    const rawName = step.name || "tool";
    const count = (toolCounts.get(rawName) || 0) + 1;
    toolCounts.set(rawName, count);
    const displayStep = count > 1 ? { ...step, name: `${rawName} (${count} 次)` } : step;
    const existingIndex = toolIndexes.get(rawName);
    if (existingIndex === undefined) {
      toolIndexes.set(rawName, compacted.length);
      compacted.push(displayStep);
    } else {
      compacted[existingIndex] = displayStep;
    }
  }
  return compacted;
}

function renderStep(
  step: AgentStepDisclosure,
  index: number,
  onOpenLocalFile?: (file: LocalFilePayload) => void,
  localFilePreviewUrl?: (filePath: string) => string
): ReactNode {
  if (step.type === "thinking") return <ThinkingStep key={index} content={step.content} running={step.running} />;
  if (step.type === "tool") return <ToolStep key={index} step={step} />;
  if (step.type === "content") {
    return (
      <div className="agent-step agent-content-step" key={index}>
        <MarkdownBlock content={step.content || ""} />
      </div>
    );
  }
  if (step.type === "phase") {
    return <div className="agent-step agent-phase-step" key={index}>{step.content}</div>;
  }
  return <MediaStep key={index} step={step} onOpenLocalFile={onOpenLocalFile} localFilePreviewUrl={localFilePreviewUrl} />;
}

function splitSteps(steps: AgentStepDisclosure[], content: string) {
  const lastContentIndex = steps.reduce((latest, step, index) => (
    step.type === "content" && !step.intermediate ? index : latest
  ), -1);
  const mainContent = content || (lastContentIndex >= 0 && steps[lastContentIndex].type === "content" ? steps[lastContentIndex].content || "" : "");
  const visibleSteps = steps.filter((_step, index) => index !== lastContentIndex);
  return { mainContent, visibleSteps };
}

function MainAnswer({ content, pending, collapsible }: { content: string; pending?: boolean; collapsible?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!content) return null;
  if (!collapsible || pending || content.length <= LONG_REPLY_COLLAPSE_CHARS) {
    return <MarkdownBlock content={content} />;
  }
  if (expanded) {
    return (
      <div className="long-answer-disclosure is-expanded">
        <div className="long-answer-full">
          <MarkdownBlock content={content} />
        </div>
        <button className="long-answer-toggle long-answer-collapse-bottom" type="button" onClick={() => setExpanded(false)}>
          收起完整回复
        </button>
      </div>
    );
  }
  return (
    <div className="long-answer-disclosure">
      <button className="long-answer-toggle" type="button" onClick={() => setExpanded(true)} title="长回复已默认收起，点击展开完整内容">
        展开完整回复
      </button>
      <div className="long-answer-preview">
        <MarkdownBlock content={`${content.slice(0, LONG_REPLY_PREVIEW_CHARS).trimEnd()}...`} />
      </div>
    </div>
  );
}

export function MessageContent(props: {
  role: "user" | "assistant" | "system";
  content: string;
  pending?: boolean;
  cancelled?: boolean;
  reasoning?: string;
  steps?: AgentStepDisclosure[];
  toolCalls?: ToolCallDisclosure[];
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  const steps = props.steps || [];
  const { mainContent, visibleSteps } = splitSteps(steps, props.content);
  const compactedSteps = compactToolSteps(visibleSteps);
  const legacySteps: ReactNode[] = [];

  if (!steps.length && props.role !== "user") {
    if (props.reasoning?.trim()) legacySteps.push(<ThinkingStep key="reasoning" content={props.reasoning} />);
    (props.toolCalls || []).forEach((tool, index) => legacySteps.push(<ToolStep key={`tool-${index}`} step={tool} />));
  }

  return (
    <div className="message-content" aria-live={props.pending ? "polite" : undefined}>
      {(visibleSteps.length > 0 || legacySteps.length > 0) && (
        <div className="agent-steps">
          {compactedSteps.map((step, index) => renderStep(step, index, props.onOpenLocalFile, props.localFilePreviewUrl))}
          {legacySteps}
        </div>
      )}
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} />
      {props.cancelled && <div className="agent-cancelled-tag">已中止</div>}
      {props.pending && <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>}
    </div>
  );
}
