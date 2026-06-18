import { useState, type MouseEvent, type ReactNode } from "react";
import { ExternalLink, FileText, FolderOpen, Image as ImageIcon } from "lucide-react";
import { redactInternalPromptText } from "../utils/redaction";

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
      filePath?: string;
      previewUrl?: string;
      fileName?: string;
    };

export type LocalFilePayload = {
  file_path: string;
  file_name: string;
  file_type?: "image" | "video" | "audio" | "file" | "directory";
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
  const localPath = localPathFromSource(value) || relativeArtifactPathFromSource(value);
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
  const localPath = localPathFromSource(value) || relativeArtifactPathFromSource(value);
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
  const localPath = localPathFromSource(value) || relativeArtifactPathFromSource(value);
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
  return /^(?:\/(?:uploads|static|app)(?:\/|$)|\/api\/file(?:[/?#]|$))/.test(String(value || "").trim());
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
  const localPath = localPathFromSource(value) || relativeArtifactPathFromSource(value);
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
  let candidate = String(value || "").trim();
  try {
    const url = new URL(candidate, window.location.href);
    candidate = url.searchParams.get("path") || url.pathname || candidate;
  } catch {
    // Plain local paths are handled as-is.
  }
  const path = candidate.split(/[?#]/)[0].toLowerCase();
  if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(path)) return "image";
  if (/\.(mp4|webm|mov|m4v|mkv|avi)$/.test(path)) return "video";
  if (/\.(mp3|wav|ogg|m4a|aac|flac|webm)$/.test(path)) return "audio";
  return "";
}

const ARTIFACT_FILE_EXTENSIONS = [
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
  "mp4", "webm", "mov", "m4v", "mp3", "wav", "m4a",
  "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
  "md", "txt", "csv", "json", "html", "zip"
].join("|");

type ArtifactFileType = NonNullable<LocalFilePayload["file_type"]>;

type ArtifactItem = {
  path: string;
  name: string;
  fileType: ArtifactFileType;
};

function mergeArtifacts(...groups: ArtifactItem[][]) {
  const items: ArtifactItem[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group) {
      if (!item.path || seen.has(item.path)) continue;
      seen.add(item.path);
      items.push(item);
    }
  }
  return items;
}

function artifactFileTypeFromPath(value: string): ArtifactFileType {
  const media = mediaTypeFromUrl(value);
  if (media) return media;
  if (/[\\/]$/.test(value)) return "directory";
  return "file";
}

function isArtifactFilePath(value: string) {
  const source = String(value || "").trim();
  return new RegExp(`\\.(${ARTIFACT_FILE_EXTENSIONS})(?:[?#].*)?$`, "i").test(source);
}

function isBareArtifactFilePath(value: string) {
  const source = String(value || "").trim().replace(/^["'`]+|["'`]+$/g, "");
  return Boolean(source)
    && !/[\\/]/.test(source)
    && !source.includes("..")
    && !/^[a-z][a-z0-9+.-]*:/i.test(source)
    && isArtifactFilePath(source);
}

function isArtifactBasePath(value: string) {
  const source = String(value || "").trim();
  if (!source || isArtifactFilePath(source)) return false;
  return /^(?:[A-Za-z]:[\\/]|\\\\|\/|(?:\.{1,2}[\\/])?(?:deliverables|output|outputs|artifacts|images|assets|prompts|workspace)[\\/])/i.test(source);
}

function joinArtifactPath(base: string, fileName: string) {
  const cleanBase = base.trim().replace(/^["'`]+|["'`]+$/g, "");
  const cleanFile = fileName.trim().replace(/^["'`]+|["'`]+$/g, "");
  if (!cleanBase || !cleanFile) return "";
  const slash = cleanBase.includes("\\") && !cleanBase.includes("/") ? "\\" : "/";
  return cleanBase.replace(/[\\/]+$/g, "") + slash + cleanFile.replace(/^[\\/]+/g, "");
}

type ImageArtifact = {
  path: string;
  name: string;
};

function extractImageArtifacts(content: string) {
  const source = redactInternalPromptText(content || "");
  const pattern = /((?:[A-Za-z]:[\\/]|\\\\|\/|(?:\.{1,2}[\\/])?(?:deliverables|output|outputs|artifacts|images|assets|prompts|workspace)[\\/])[^`\r\n<>]*?\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[)\]'"`，。；;,.!?！？:]|$)/gi;
  const items: ImageArtifact[] = [];
  const seen = new Set<string>();
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) && items.length < 12) {
    const raw = (match[1] || "").trim().replace(/^["'`]+|["'`]+$/g, "");
    const path = localPathFromSource(raw) || relativeArtifactPathFromSource(raw);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    items.push({ path, name: basenameFromPath(path) });
  }
  return items;
}

function ImageArtifactGrid({
  artifacts,
  localFilePreviewUrl,
  onOpenLocalFile
}: {
  artifacts: ImageArtifact[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  if (!artifacts.length || !localFilePreviewUrl) return null;
  return (
    <div className="image-artifact-grid">
      {artifacts.map((artifact) => (
        <button
          key={artifact.path}
          type="button"
          className="image-artifact-card"
          onClick={() => onOpenLocalFile?.({ file_path: artifact.path, file_name: artifact.name, file_type: "image" })}
          title={artifact.path}
        >
          <img src={localFilePreviewUrl(artifact.path)} alt={artifact.name} loading="lazy" />
          <span>{artifact.name}</span>
        </button>
      ))}
    </div>
  );
}

function extractArtifacts(content: string, options?: { allowBareFiles?: boolean }) {
  const source = redactInternalPromptText(content || "");
  const items: ArtifactItem[] = [];
  const seen = new Set<string>();
  const bases: string[] = [];
  const allowBareFiles = Boolean(options?.allowBareFiles);
  const add = (rawPath: string, fileType?: ArtifactFileType) => {
    const path = (localPathFromSource(rawPath) || relativeArtifactPathFromSource(rawPath, allowBareFiles) || rawPath).trim();
    if (!path || seen.has(path)) return;
    seen.add(path);
    items.push({
      path,
      name: basenameFromPath(path),
      fileType: fileType || artifactFileTypeFromPath(path)
    });
  };

  const dirPattern = /((?:[A-Za-z]:[\\/]|\\\\|\/|(?:\.{1,2}[\\/])?(?:deliverables|output|outputs|artifacts|images|assets|prompts|workspace)[\\/])[^`\r\n<>]*?[\\/])(?:[\s)\]'"`,.;:!?]|$)/gi;
  let dirMatch: RegExpExecArray | null;
  while ((dirMatch = dirPattern.exec(source)) && items.length < 20) {
    const raw = (dirMatch[1] || "").trim().replace(/^["'`]+|["'`]+$/g, "");
    if (!raw || !isArtifactBasePath(raw)) continue;
    bases.push(raw);
    add(raw, "directory");
  }

  const filePattern = new RegExp(
    `((?:[A-Za-z]:[\\\\/]|\\\\\\\\|/|(?:\\.{1,2}[\\\\/])?(?:deliverables|output|outputs|artifacts|images|assets|prompts|workspace)[\\\\/])[^\\\`\\r\\n<>]*?\\.(${ARTIFACT_FILE_EXTENSIONS}))(?:[)\\]'"\\\`,.;:!?]|$)`,
    "gi"
  );
  let fileMatch: RegExpExecArray | null;
  while ((fileMatch = filePattern.exec(source)) && items.length < 24) {
    const raw = (fileMatch[1] || "").trim().replace(/^["'`]+|["'`]+$/g, "");
    const path = localPathFromSource(raw) || relativeArtifactPathFromSource(raw);
    if (!path) continue;
    add(path);
  }

  const base = bases[0] || "";
  if (base) {
    const fileLinePattern = new RegExp(`^\\s*(?:[-*]\\s*)?([^\\\\/:*?"<>|\\r\\n]+?\\.(${ARTIFACT_FILE_EXTENSIONS}))\\s*$`, "i");
    for (const rawLine of source.replace(/\r\n/g, "\n").split("\n")) {
      if (items.length >= 24) break;
      const cleaned = rawLine.trim().replace(/^["'`]+|["'`]+$/g, "");
      const match = cleaned.match(fileLinePattern);
      if (!match) continue;
      add(joinArtifactPath(base, match[1]));
    }
  }

  if (allowBareFiles) {
    const quotedFilePattern = new RegExp(`["'\`]([^"'\\\`<>\\r\\n]{1,220}\\.(${ARTIFACT_FILE_EXTENSIONS}))["'\`]`, "gi");
    let quotedMatch: RegExpExecArray | null;
    while ((quotedMatch = quotedFilePattern.exec(source)) && items.length < 24) {
      const raw = (quotedMatch[1] || "").trim();
      if (!isBareArtifactFilePath(raw)) continue;
      add(raw);
    }

    const bareFilePattern = new RegExp(`(?:^|[\\s:：,，;；])([^\\s"'\\\`<>\\\\/]{1,180}\\.(${ARTIFACT_FILE_EXTENSIONS}))(?=$|[\\s,，;；。.!?！？])`, "gi");
    let bareMatch: RegExpExecArray | null;
    while ((bareMatch = bareFilePattern.exec(source)) && items.length < 24) {
      const raw = (bareMatch[1] || "").trim();
      if (!isBareArtifactFilePath(raw)) continue;
      add(raw);
    }
  }

  return items;
}

function artifactIcon(type: ArtifactFileType) {
  if (type === "directory") return <FolderOpen aria-hidden="true" />;
  if (type === "image") return <ImageIcon aria-hidden="true" />;
  return <FileText aria-hidden="true" />;
}

function ArtifactGrid({
  artifacts,
  localFilePreviewUrl,
  onOpenLocalFile
}: {
  artifacts: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  if (!artifacts.length) return null;
  return (
    <div className="artifact-grid image-artifact-grid">
      {artifacts.map((artifact) => (
        <button
          key={artifact.path}
          type="button"
          className={`artifact-card image-artifact-card${artifact.fileType === "image" ? " is-image" : ""}`}
          onClick={() => onOpenLocalFile?.({ file_path: artifact.path, file_name: artifact.name, file_type: artifact.fileType })}
          title={artifact.path}
        >
          {artifact.fileType === "image" && localFilePreviewUrl ? (
            <img src={localFilePreviewUrl(artifact.path)} alt={artifact.name} loading="lazy" />
          ) : (
            <span className="artifact-file-icon">{artifactIcon(artifact.fileType)}</span>
          )}
          <span className="artifact-card-label">
            <span>{artifact.name}</span>
            <ExternalLink aria-hidden="true" />
          </span>
        </button>
      ))}
    </div>
  );
}

function relativeArtifactPathFromSource(value?: string, allowBareFile = false) {
  const source = String(value || "").trim().replace(/^["'`]+|["'`]+$/g, "");
  if (!source || /^[a-z][a-z0-9+.-]*:/i.test(source) || localPathFromSource(source)) return "";
  if (!isArtifactFilePath(source)) return "";
  if (allowBareFile && isBareArtifactFilePath(source)) return source;
  if (!/^(?:\.{1,2}[\\/])?(?:deliverables|output|outputs|artifacts|images|assets|prompts|workspace)[\\/]/i.test(source)) return "";
  if (source.includes("..")) return "";
  return source;
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
  html = html.replace(/`([^`]+)`/g, (_match, rawCode: string) => {
    const decoded = decodeBasicEntities(rawCode);
    const localPath = localPathFromSource(decoded) || relativeArtifactPathFromSource(decoded, true);
    if (!localPath) return `<code>${rawCode}</code>`;
    return `<a href="#" class="markdown-local-file-link inline-local-file-code" data-ecorex-file-path="${escapeHtml(localPath)}" data-ecorex-file-name="${escapeHtml(basenameFromPath(localPath))}"><code>${rawCode}</code></a>`;
  });
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
  if (typeof value === "string") return redactInternalPromptText(value);
  try {
    return redactInternalPromptText(JSON.stringify(value, null, 2));
  } catch {
    return redactInternalPromptText(String(value));
  }
}

function extractArtifactsFromUnknown(value: unknown) {
  if (value === undefined || value === null || value === "") return [];
  const text = typeof value === "string" ? value : formatToolValue(value);
  return extractArtifacts(text, { allowBareFiles: true });
}

function extractArtifactsFromSteps(steps: AgentStepDisclosure[], toolCalls: ToolCallDisclosure[] = []) {
  const groups: ArtifactItem[][] = [];
  for (const step of steps) {
    if (step.type === "media") {
      const source = step.filePath || step.url || step.previewUrl || "";
      const path = localPathFromSource(source) || relativeArtifactPathFromSource(source, true);
      if (path) {
        groups.push([{
          path,
          name: step.fileName || basenameFromPath(path),
          fileType: step.fileType || artifactFileTypeFromPath(path)
        }]);
      }
      continue;
    }
    if (step.type === "tool") {
      groups.push(extractArtifactsFromUnknown(step.arguments));
      groups.push(extractArtifactsFromUnknown(step.result));
    }
  }
  for (const tool of toolCalls) {
    groups.push(extractArtifactsFromUnknown(tool.arguments));
    groups.push(extractArtifactsFromUnknown(tool.result));
  }
  return mergeArtifacts(...groups).slice(0, 24);
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
      dangerouslySetInnerHTML={{ __html: renderMarkdown(redactInternalPromptText(content), localFilePreviewUrl) }}
    />
  );
}

function ThinkingStep({ content = "", running }: { content?: string; running?: boolean }) {
  const trimmed = redactInternalPromptText(content).trim();
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
  const previewSource = step.previewUrl || step.url || step.filePath || "";
  const openSource = step.filePath || step.url || "";
  if (!previewSource && !openSource) return null;
  const localPath = localPathFromSource(openSource) || relativeArtifactPathFromSource(openSource);
  const fileName = step.fileName || basenameFromPath(localPath || previewSource || openSource);
  const previewUrl = previewSource
    ? safeMediaUrl(previewSource, localFilePreviewUrl)
    : localPath && localFilePreviewUrl
      ? localFilePreviewUrl(localPath)
      : "";
  if (step.fileType === "image" && previewUrl) {
    const image = <img className="agent-media-image" src={previewUrl} alt={fileName} loading="lazy" />;
    if (!onOpenLocalFile || !localPath) return image;
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
  if (step.fileType === "audio" && previewUrl) {
    return (
      <div className="agent-audio-card">
        <audio className="agent-media-audio" src={previewUrl} controls />
        {localPath && onOpenLocalFile && (
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
  if (step.fileType === "video" && previewUrl) {
    return <video className="agent-media-video" src={previewUrl} controls />;
  }
  if (step.fileType === "audio" && previewUrl) {
    return <audio className="agent-media-audio" src={previewUrl} controls />;
  }
  return (
    <a className="agent-file-link" href={safeUrl(previewSource || openSource, localFilePreviewUrl)} target="_blank" rel="noreferrer">
      {fileName || previewSource || openSource}
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
    return <div className="agent-step agent-phase-step" key={index}>{redactInternalPromptText(step.content || "")}</div>;
  }
  return <MediaStep key={index} step={step} onOpenLocalFile={onOpenLocalFile} localFilePreviewUrl={localFilePreviewUrl} />;
}

function splitSteps(steps: AgentStepDisclosure[], content: string) {
  const lastContentIndex = steps.reduce((latest, step, index) => (
    step.type === "content" && !step.intermediate ? index : latest
  ), -1);
  const mainContent = redactInternalPromptText(content || (lastContentIndex >= 0 && steps[lastContentIndex].type === "content" ? steps[lastContentIndex].content || "" : ""));
  const visibleSteps = steps.filter((_step, index) => index !== lastContentIndex);
  return { mainContent, visibleSteps };
}

function MainAnswer({ content, pending, collapsible, extraArtifacts = [], localFilePreviewUrl, onOpenLocalFile }: {
  content: string;
  pending?: boolean;
  collapsible?: boolean;
  extraArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const artifacts = mergeArtifacts(extractArtifacts(content, { allowBareFiles: true }), extraArtifacts);
  const artifactGrid = <ArtifactGrid artifacts={artifacts} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />;
  if (!content) return artifactGrid;
  if (!collapsible || pending || content.length <= LONG_REPLY_COLLAPSE_CHARS) {
    return (
      <>
        <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
        {artifactGrid}
      </>
    );
  }
  if (expanded) {
    return (
      <div className="long-answer-disclosure is-expanded">
        <div className="long-answer-full">
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
          {artifactGrid}
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
        {artifactGrid}
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
  const stepArtifacts = extractArtifactsFromSteps(compactedSteps, props.toolCalls || []);
  const legacySteps: ReactNode[] = [];

  if (!steps.length && props.role !== "user") {
    if (props.reasoning?.trim()) legacySteps.push(<ThinkingStep key="reasoning" content={props.reasoning} />);
    (props.toolCalls || []).forEach((tool, index) => legacySteps.push(<ToolStep key={`tool-${index}`} step={tool} />));
  }

  return (
    <div className="message-content" aria-live={props.pending ? "polite" : undefined}>
      {(visibleSteps.length > 0 || legacySteps.length > 0) && props.pending && (
        <div className="agent-steps">
          {compactedSteps.map((step, index) => renderStep(step, index, props.onOpenLocalFile, props.localFilePreviewUrl))}
          {legacySteps}
        </div>
      )}
      {(visibleSteps.length > 0 || legacySteps.length > 0) && !props.pending && (
        <details className="agent-process-disclosure">
          <summary>调用过程 · {visibleSteps.length + legacySteps.length} 步</summary>
          <div className="agent-steps">
            {compactedSteps.map((step, index) => renderStep(step, index, props.onOpenLocalFile, props.localFilePreviewUrl))}
            {legacySteps}
          </div>
        </details>
      )}
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} extraArtifacts={stepArtifacts} localFilePreviewUrl={props.localFilePreviewUrl} onOpenLocalFile={props.onOpenLocalFile} />
      {props.cancelled ? (
        <div className="agent-cancelled-tag">已中止</div>
      ) : props.pending ? (
        <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>
      ) : null}
    </div>
  );
}
