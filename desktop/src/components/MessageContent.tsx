import { useState, type MouseEvent, type ReactNode } from "react";

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
      id?: string;
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
      fileType?: "image" | "video" | "audio" | "file";
      url?: string;
      fileName?: string;
    };

export type LocalFilePayload = {
  file_path: string;
  file_name: string;
  file_type?: "image" | "video" | "audio" | "file";
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

function safeUrl(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  if (isRuntimeHttpPath(value)) {
    return runtimeHttpUrl(value);
  }
  const localPath = localPathFromSource(value);
  if (localPath && localFilePreviewUrl) {
    return localFilePreviewUrl(localPath);
  }
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function safeImageUrl(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  if (isRuntimeHttpPath(value)) {
    return runtimeHttpUrl(value);
  }
  const localPath = localPathFromSource(value);
  if (localPath && localFilePreviewUrl) {
    return localFilePreviewUrl(localPath);
  }
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:"].includes(url.protocol) || url.href.startsWith("data:image/") ? url.href : "";
  } catch {
    return "";
  }
}

function safeMediaUrl(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  if (isRuntimeHttpPath(value)) {
    return runtimeHttpUrl(value);
  }
  const localPath = localPathFromSource(value);
  if (localPath && localFilePreviewUrl) {
    return localFilePreviewUrl(localPath);
  }
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:"].includes(url.protocol) || /^data:(image|video|audio)\//i.test(url.href) ? url.href : "";
  } catch {
    return "";
  }
}

function isRuntimeHttpPath(value?: string) {
  return /^\/(uploads|static|app)\//.test(String(value || "").trim());
}

function runtimeHttpUrl(value: string) {
  try {
    return new URL(value, window.location.origin).href;
  } catch {
    return value;
  }
}

function localPathFromSource(value?: string) {
  const source = String(value || "").trim();
  if (!source) return "";
  if (isRuntimeHttpPath(source)) return "";
  if (/^file:\/\//i.test(source)) {
    try {
      const url = new URL(source);
      const decodedPath = decodeURIComponent(url.pathname || "");
      if (/^\/[a-zA-Z]:\//.test(decodedPath)) {
        return decodedPath.slice(1);
      }
      if (url.hostname) {
        return `//${url.hostname}${decodedPath}`;
      }
      return decodedPath;
    } catch {
      const stripped = source.replace(/^file:\/+/i, "");
      if (/^[a-zA-Z]:[\\/]/.test(stripped) || stripped.startsWith("/")) {
        return stripped;
      }
      return `/${stripped}`;
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

function decodeBasicEntities(value: string) {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function linkAttributesForUrl(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  const localPath = localPathFromSource(value);
  const href = safeUrl(value, localFilePreviewUrl);
  const attrs = [`href="${escapeHtml(href)}"`, `target="_blank"`, `rel="noreferrer"`];
  if (localPath) {
    attrs.push(`class="markdown-local-file-link"`);
    attrs.push(`data-ecorex-file-path="${escapeHtml(localPath)}"`);
    attrs.push(`data-ecorex-file-name="${escapeHtml(basenameFromPath(localPath))}"`);
  }
  return attrs.join(" ");
}

function mediaTypeFromUrl(value: string): "image" | "video" | "audio" | "" {
  const path = String(value || "").split(/[?#]/)[0].toLowerCase();
  if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(path)) return "image";
  if (/\.(mp4|webm|mov|m4v|mkv|avi)$/.test(path)) return "video";
  if (/\.(mp3|wav|ogg|m4a|aac|flac|webm)$/.test(path)) return "audio";
  return "";
}

function splitTrailingUrlPunctuation(value: string) {
  const match = value.match(/^(.+?)([.,!?;:)\]\u3002\uff0c\uff01\uff1f\uff1b\uff1a]*)$/);
  return { url: match?.[1] || value, trailing: match?.[2] || "" };
}

function renderBareUrl(raw: string, localFilePreviewUrl?: (filePath: string) => string) {
  const { url, trailing } = splitTrailingUrlPunctuation(raw);
  const mediaType = mediaTypeFromUrl(url);
  if (mediaType === "image") {
    const src = safeImageUrl(url, localFilePreviewUrl);
    if (src) return `<img class="markdown-image" src="${escapeHtml(src)}" alt="${escapeHtml(basenameFromPath(url))}" loading="lazy" />${escapeHtml(trailing)}`;
  }
  if (mediaType === "video" || mediaType === "audio") {
    const src = safeMediaUrl(url, localFilePreviewUrl);
    if (src && mediaType === "video") return `<video class="markdown-video" src="${escapeHtml(src)}" controls></video>${escapeHtml(trailing)}`;
    if (src && mediaType === "audio") return `<audio class="markdown-audio" src="${escapeHtml(src)}" controls></audio>${escapeHtml(trailing)}`;
  }
  return `<a ${linkAttributesForUrl(url, localFilePreviewUrl)}>${escapeHtml(url)}</a>${escapeHtml(trailing)}`;
}

function linkifyPlainTextSegments(html: string, localFilePreviewUrl?: (filePath: string) => string) {
  const localPathPrefix = String.raw`(?:[A-Za-z]:[\\/]|\\\\|\/(?:Users|Volumes|home|tmp|var|mnt|opt|srv|Applications)\/)`;
  const pattern = new RegExp(
    String.raw`(&quot;|&#39;)(${localPathPrefix}[^<>\r\n]*?)\1|((?:https?:\/\/|file:\/\/)[^\s<>"']+|${localPathPrefix}[^\s<>"']+)`,
    "g"
  );
  return html.split(/(<[^>]+>)/g).map((part) => {
    if (!part || part.startsWith("<")) return part;
    return part.replace(pattern, (match, quote: string, quotedPath: string, rawPath: string) => {
      if (quote && quotedPath) {
        return `${quote}${renderBareUrl(decodeBasicEntities(quotedPath), localFilePreviewUrl)}${quote}`;
      }
      return renderBareUrl(rawPath || match, localFilePreviewUrl);
    });
  }).join("");
}

function renderInline(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_match, alt: string, href: string) => {
    const src = safeImageUrl(href, localFilePreviewUrl);
    if (!src) return escapeHtml(alt);
    return `<img class="markdown-image" src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy" />`;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label: string, href: string) => {
    return `<a ${linkAttributesForUrl(href, localFilePreviewUrl)}>${escapeHtml(label)}</a>`;
  });
  return linkifyPlainTextSegments(html, localFilePreviewUrl);
}

function flushParagraph(lines: string[], out: string[], localFilePreviewUrl?: (filePath: string) => string) {
  if (!lines.length) return;
  out.push(`<p>${renderInline(lines.join(" "), localFilePreviewUrl)}</p>`);
  lines.length = 0;
}

function renderList(items: string[], ordered: boolean, out: string[], localFilePreviewUrl?: (filePath: string) => string) {
  if (!items.length) return;
  const tag = ordered ? "ol" : "ul";
  out.push(`<${tag}>${items.map((item) => `<li>${renderInline(item, localFilePreviewUrl)}</li>`).join("")}</${tag}>`);
  items.length = 0;
}

function renderMarkdown(markdown: string, localFilePreviewUrl?: (filePath: string) => string) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  const paragraph: string[] = [];
  const bullets: string[] = [];
  const numbers: string[] = [];
  let code: string[] | null = null;

  const flushAll = () => {
    flushParagraph(paragraph, out, localFilePreviewUrl);
    renderList(bullets, false, out, localFilePreviewUrl);
    renderList(numbers, true, out, localFilePreviewUrl);
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
      out.push(`<h${level}>${renderInline(heading[2], localFilePreviewUrl)}</h${level}>`);
      continue;
    }

    const bullet = raw.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph(paragraph, out, localFilePreviewUrl);
      renderList(numbers, true, out, localFilePreviewUrl);
      bullets.push(bullet[1]);
      continue;
    }

    const numbered = raw.match(/^\s*\d+\.\s+(.+)$/);
    if (numbered) {
      flushParagraph(paragraph, out, localFilePreviewUrl);
      renderList(bullets, false, out, localFilePreviewUrl);
      numbers.push(numbered[1]);
      continue;
    }

    renderList(bullets, false, out, localFilePreviewUrl);
    renderList(numbers, true, out, localFilePreviewUrl);
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

function MarkdownBlock({
  content,
  localFilePreviewUrl,
  onOpenLocalFile
}: {
  content: string;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const handleClick = (event: MouseEvent<HTMLDivElement>) => {
    if (!onOpenLocalFile) return;
    const target = event.target instanceof Element ? event.target : null;
    const anchor = target?.closest<HTMLAnchorElement>("a[data-ecorex-file-path]");
    const filePath = anchor?.dataset.ecorexFilePath || "";
    if (!anchor || !filePath) return;
    event.preventDefault();
    onOpenLocalFile({
      file_path: filePath,
      file_name: anchor.dataset.ecorexFileName || basenameFromPath(filePath),
      file_type: mediaTypeFromUrl(filePath) || "file"
    });
  };
  return (
    <div
      className="markdown-content"
      onClick={handleClick}
      dangerouslySetInnerHTML={{ __html: renderMarkdown(content, localFilePreviewUrl) }}
    />
  );
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
  if (localPath && step.fileType === "audio" && localFilePreviewUrl) {
    const fileName = step.fileName || basenameFromPath(localPath);
    return (
      <div className="agent-audio-card">
        <audio className="agent-media-audio" src={localFilePreviewUrl(localPath)} controls />
        {onOpenLocalFile && (
          <button
            type="button"
            className="agent-file-link"
            onClick={() => onOpenLocalFile({ file_path: localPath, file_name: fileName, file_type: "audio" })}
        title="点击在本地打开"
          >
            {fileName}
          </button>
        )}
      </div>
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
    return <img className="agent-media-image" src={safeImageUrl(step.url, localFilePreviewUrl)} alt={step.fileName || "image"} />;
  }
  if (step.fileType === "video") {
    return <video className="agent-media-video" src={safeMediaUrl(step.url, localFilePreviewUrl)} controls />;
  }
  if (step.fileType === "audio") {
    return <audio className="agent-media-audio" src={safeMediaUrl(step.url, localFilePreviewUrl)} controls />;
  }
  return (
    <a className="agent-file-link" href={safeUrl(step.url, localFilePreviewUrl)} target="_blank" rel="noreferrer">
      {step.fileName || step.url}
    </a>
  );
}

function compactToolSteps(steps: AgentStepDisclosure[]) {
  const seen = new Map<string, number>();
  return steps.map((step) => {
    if (step.type !== "tool") return step;
    const baseName = step.name || "tool";
    const nextCount = (seen.get(baseName) || 0) + 1;
    seen.set(baseName, nextCount);
    return nextCount > 1 ? { ...step, name: `${baseName} #${nextCount}` } : step;
  });
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
        <MarkdownBlock content={step.content || ""} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
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

function MainAnswer({ content, pending, collapsible, localFilePreviewUrl, onOpenLocalFile }: {
  content: string;
  pending?: boolean;
  collapsible?: boolean;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!content) return null;
  if (!collapsible || pending || content.length <= LONG_REPLY_COLLAPSE_CHARS) {
    return <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />;
  }
  if (expanded) {
    return (
      <div className="long-answer-disclosure is-expanded">
        <div className="long-answer-full">
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
        </div>
        <button className="long-answer-toggle long-answer-collapse-bottom" type="button" onClick={() => setExpanded(false)}>
          收起完整回复
        </button>
      </div>
    );
  }
  return (
    <div className="long-answer-disclosure">
      <div className="long-answer-preview">
        <MarkdownBlock content={`${content.slice(0, LONG_REPLY_PREVIEW_CHARS).trimEnd()}...`} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
      </div>
      <button className="long-answer-toggle long-answer-expand-bottom" type="button" onClick={() => setExpanded(true)} title="长回复已默认收起，点击展开完整内容">
        展开完整回复
      </button>
    </div>
  );
}

export function MessageContent(props: {
  role: "user" | "assistant" | "system";
  content: string;
  pending?: boolean;
  paused?: boolean;
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
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} localFilePreviewUrl={props.localFilePreviewUrl} onOpenLocalFile={props.onOpenLocalFile} />
      {props.cancelled ? (
        <div className="agent-cancelled-tag">已中止</div>
      ) : props.paused ? (
        <div className="agent-cancelled-tag">已暂停，输入新消息后继续</div>
      ) : props.pending ? (
        <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>
      ) : null}
    </div>
  );
}
