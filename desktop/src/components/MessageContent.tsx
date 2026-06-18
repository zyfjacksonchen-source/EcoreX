import { useMemo, useState, type MouseEvent, type ReactNode } from "react";
import {
  ExternalLink,
  Eye,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  MoreHorizontal,
  MonitorUp
} from "lucide-react";
import type { AgentArtifact } from "../services/ecorexApi";
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
  open_action?: "preview" | "open" | "reveal" | "copy" | "openWith";
};

const REASONING_RENDER_CAP = 4 * 1024;
const TOOL_DETAIL_RENDER_CAP = 6 * 1024;
const LONG_REPLY_COLLAPSE_CHARS = 1400;
const LONG_REPLY_PREVIEW_CHARS = 520;
const ARTIFACT_PREVIEW_LIMIT = 6;
const ARTIFACT_RELATIVE_ROOTS = "deliverables|output|outputs|artifacts|images|assets";
const ARTIFACT_ABSOLUTE_POSIX_ROOTS = "Users|Volumes|home|tmp|var|mnt|opt|srv|workspace";
const ARTIFACT_PATH_PREFIX = `(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/(?:${ARTIFACT_ABSOLUTE_POSIX_ROOTS})[\\\\/]|(?:\\.{1,2}[\\\\/])?(?:${ARTIFACT_RELATIVE_ROOTS})[\\\\/])`;

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

function cleanArtifactCandidate(value: string) {
  return String(value || "")
    .trim()
    .replace(/^["'`]+|["'`]+$/g, "")
    .replace(/[)\]'",.;:!?，。；：！？]+$/g, "")
    .trim();
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

type DisplayArtifact = AgentArtifact & {
  legacyPath?: string;
};

function artifactKindFromFileType(fileType: ArtifactFileType): AgentArtifact["kind"] {
  return fileType === "image" || fileType === "video" || fileType === "audio" || fileType === "directory"
    ? fileType
    : "file";
}

function artifactFileTypeFromKind(kind: AgentArtifact["kind"]): ArtifactFileType {
  if (kind === "image" || kind === "video" || kind === "audio" || kind === "directory") return kind;
  return "file";
}

function artifactPath(artifact: AgentArtifact) {
  return artifact.path || artifact.relativePath || artifact.url || "";
}

function displayArtifactTitle(artifact: AgentArtifact) {
  const source = artifact.title || artifactPath(artifact);
  return source ? basenameFromPath(source) : "未命名产物";
}

function displayArtifactSubtitle(artifact: AgentArtifact) {
  if (artifact.relativePath) return artifact.relativePath;
  if (artifact.path) return artifact.path;
  return artifact.url || "";
}

function legacyArtifactToAgentArtifact(item: ArtifactItem): DisplayArtifact {
  return {
    id: `legacy:${artifactDedupeKey(item)}`,
    kind: artifactKindFromFileType(item.fileType),
    intent: "deliverable",
    operation: "exported",
    status: "ready",
    title: item.name,
    path: item.path,
    legacyPath: item.path,
    source: { toolName: "legacy-detected", createdAt: Date.now() }
  };
}

function mergeAgentArtifacts(primary: AgentArtifact[] = [], legacy: ArtifactItem[] = []) {
  const items: DisplayArtifact[] = [];
  const seen = new Set<string>();
  const add = (artifact: DisplayArtifact) => {
    const source = artifactPath(artifact).replace(/\\/g, "/").toLowerCase();
    const key = source || artifact.id;
    if (!key || seen.has(key)) return;
    seen.add(key);
    items.push(artifact);
  };
  primary.forEach((artifact) => add(artifact));
  legacy.map(legacyArtifactToAgentArtifact).forEach(add);
  return items;
}

function mergeArtifacts(...groups: ArtifactItem[][]) {
  const items: ArtifactItem[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group) {
      const key = artifactDedupeKey(item);
      if (!item.path || seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  }
  return items;
}

function artifactDedupeKey(item: ArtifactItem) {
  const normalized = item.path
    .replace(/^file:\/+/i, "")
    .replace(/\\/g, "/")
    .replace(/^\/([A-Za-z]:\/)/, "$1")
    .replace(/[?#].*$/, "")
    .replace(/\/+/g, "/")
    .toLowerCase();
  if (item.fileType === "image") return `image:${basenameFromPath(normalized)}`;
  const parts = normalized.split("/").filter(Boolean);
  return `${item.fileType}:${parts.slice(-4).join("/") || normalized}`;
}

function artifactFileTypeFromPath(value: string): ArtifactFileType {
  const media = mediaTypeFromUrl(value);
  if (media) return media;
  if (/[\\/]$/.test(value)) return "directory";
  return "file";
}

function isArtifactFilePath(value: string) {
  const source = cleanArtifactCandidate(value);
  if (!source || /^https?:\/\//i.test(source) || /[{}]/.test(source)) return false;
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
  const source = cleanArtifactCandidate(value);
  if (!source || isArtifactFilePath(source)) return false;
  if (/^https?:\/\//i.test(source) || /[{}]/.test(source)) return false;
  return new RegExp(`^${ARTIFACT_PATH_PREFIX}`, "i").test(source);
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
  const pattern = new RegExp(`(${ARTIFACT_PATH_PREFIX}[^\\\`\\r\\n<>]*?\\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[)\\]'"\\\`,.;:!?]|$)`, "gi");
  const items: ImageArtifact[] = [];
  const seen = new Set<string>();
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) && items.length < 12) {
    const raw = cleanArtifactCandidate(match[1] || "");
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
    const path = (localPathFromSource(rawPath) || relativeArtifactPathFromSource(rawPath, allowBareFiles) || "").trim();
    if (!path || seen.has(path)) return;
    seen.add(path);
    items.push({
      path,
      name: basenameFromPath(path),
      fileType: fileType || artifactFileTypeFromPath(path)
    });
  };

  const dirPattern = new RegExp(`(${ARTIFACT_PATH_PREFIX}[^\\\`\\r\\n<>]*?[\\\\/])(?:[\\s)\\]'"\\\`,.;:!?]|$)`, "gi");
  let dirMatch: RegExpExecArray | null;
  while ((dirMatch = dirPattern.exec(source)) && items.length < 20) {
    const raw = cleanArtifactCandidate(dirMatch[1] || "");
    if (!raw || !isArtifactBasePath(raw)) continue;
    bases.push(raw);
    add(raw, "directory");
  }

  const filePattern = new RegExp(
    `(${ARTIFACT_PATH_PREFIX}[^\\\`\\r\\n<>]*?\\.(${ARTIFACT_FILE_EXTENSIONS}))(?:[)\\]'"\\\`,.;:!?]|$)`,
    "gi"
  );
  let fileMatch: RegExpExecArray | null;
  while ((fileMatch = filePattern.exec(source)) && items.length < 24) {
    const raw = cleanArtifactCandidate(fileMatch[1] || "");
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
  const [expanded, setExpanded] = useState(false);
  if (!artifacts.length) return null;
  const visibleArtifacts = expanded ? artifacts : artifacts.slice(0, ARTIFACT_PREVIEW_LIMIT);
  const hiddenCount = Math.max(artifacts.length - visibleArtifacts.length, 0);
  return (
    <div className="artifact-section">
      <div className="artifact-section-head">
        <strong>生成产物</strong>
        <span>{artifacts.length} 个</span>
      </div>
      <div className="artifact-grid image-artifact-grid">
        {visibleArtifacts.map((artifact) => (
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
      {artifacts.length > ARTIFACT_PREVIEW_LIMIT && (
        <button className="artifact-grid-toggle" type="button" onClick={() => setExpanded((current) => !current)}>
          {expanded ? "收起产物" : `显示另外 ${hiddenCount} 个`}
        </button>
      )}
    </div>
  );
}

function ArtifactShelf({
  artifacts,
  legacyArtifacts = [],
  localFilePreviewUrl,
  onOpenLocalFile
}: {
  artifacts?: AgentArtifact[];
  legacyArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [openMenuId, setOpenMenuId] = useState("");
  const items = mergeAgentArtifacts(artifacts, legacyArtifacts);
  if (!items.length) return null;

  const visibleArtifacts = expanded ? items : items.slice(0, 3);
  const hiddenCount = Math.max(items.length - visibleArtifacts.length, 0);
  const changedCount = items.filter((item) => item.intent === "changed-file").length;
  const title = changedCount === items.length ? "更改文件" : changedCount ? "产物与更改" : "生成产物";

  const runAction = async (artifact: DisplayArtifact, action: NonNullable<LocalFilePayload["open_action"]>) => {
    const source = artifactPath(artifact);
    if (!source) return;
    if (action === "copy") {
      await navigator.clipboard?.writeText(source).catch(() => undefined);
      setOpenMenuId("");
      return;
    }
    if (artifact.kind === "url" && artifact.url && action !== "reveal" && action !== "openWith") {
      window.open(artifact.url, "_blank", "noopener,noreferrer");
      setOpenMenuId("");
      return;
    }
    onOpenLocalFile?.({
      file_path: source,
      file_name: displayArtifactTitle(artifact),
      file_type: artifactFileTypeFromKind(artifact.kind),
      open_action: action
    });
    setOpenMenuId("");
  };

  return (
    <section className="artifact-shelf" aria-label={title}>
      <div className="artifact-section-head">
        <strong>{title}</strong>
        <span>{items.length} 个</span>
      </div>
      <div className="artifact-list">
        {visibleArtifacts.map((artifact) => {
          const source = artifactPath(artifact);
          const fileType = artifactFileTypeFromKind(artifact.kind);
          const name = displayArtifactTitle(artifact);
          const subtitle = displayArtifactSubtitle(artifact);
          const previewSource = artifact.thumbnailUrl || artifact.previewUrl || source;
          const previewUrl = artifact.kind === "image" && previewSource && localFilePreviewUrl
            ? localFilePreviewUrl(previewSource)
            : artifact.kind === "image" && previewSource
              ? safeImageUrl(previewSource, localFilePreviewUrl)
              : "";
          const menuOpen = openMenuId === artifact.id;
          return (
            <div className={`artifact-row is-${artifact.kind}`} key={artifact.id} title={subtitle || name}>
              <span className="artifact-row-icon" aria-hidden="true">
                {previewUrl ? <img src={previewUrl} alt="" loading="lazy" /> : artifactIcon(fileType)}
              </span>
              <span className="artifact-row-main">
                <strong>{name}</strong>
                {subtitle && <small>{subtitle}</small>}
              </span>
              {artifact.stats && (
                <span className="artifact-row-stats" aria-label="变更统计">
                  {typeof artifact.stats.addedLines === "number" && <em className="is-added">+{artifact.stats.addedLines}</em>}
                  {typeof artifact.stats.removedLines === "number" && <em className="is-removed">-{artifact.stats.removedLines}</em>}
                </span>
              )}
              <span className="artifact-row-actions">
                <button type="button" className="artifact-icon-button" title="预览" aria-label={`预览 ${name}`} onClick={() => void runAction(artifact, "preview")}>
                  <Eye aria-hidden="true" />
                </button>
                <button type="button" className="artifact-icon-button" title="本地打开" aria-label={`本地打开 ${name}`} onClick={() => void runAction(artifact, "open")}>
                  <MonitorUp aria-hidden="true" />
                </button>
                <span className="artifact-menu-wrap">
                  <button
                    type="button"
                    className="artifact-icon-button"
                    title="打开方式"
                    aria-label={`${name} 的打开方式`}
                    aria-expanded={menuOpen}
                    onClick={() => setOpenMenuId((current) => current === artifact.id ? "" : artifact.id)}
                  >
                    <MoreHorizontal aria-hidden="true" />
                  </button>
                  {menuOpen && (
                    <span className="artifact-action-menu" role="menu">
                      <button type="button" role="menuitem" onClick={() => void runAction(artifact, "open")}>本地打开</button>
                      <button type="button" role="menuitem" onClick={() => void runAction(artifact, "reveal")}>在文件夹中显示</button>
                      <button type="button" role="menuitem" onClick={() => void runAction(artifact, "openWith")}>选择应用打开</button>
                      <button type="button" role="menuitem" onClick={() => void runAction(artifact, "copy")}>复制路径</button>
                    </span>
                  )}
                </span>
              </span>
            </div>
          );
        })}
      </div>
      {items.length > 3 && (
        <button className="artifact-grid-toggle" type="button" onClick={() => setExpanded((current) => !current)}>
          {expanded ? "收起产物" : `显示另外 ${hiddenCount} 个`}
        </button>
      )}
    </section>
  );
}

function relativeArtifactPathFromSource(value?: string, allowBareFile = false) {
  const source = cleanArtifactCandidate(value || "");
  if (!source || /^[a-z][a-z0-9+.-]*:/i.test(source) || localPathFromSource(source)) return "";
  if (!isArtifactFilePath(source)) return "";
  if (allowBareFile && isBareArtifactFilePath(source)) return source;
  if (!new RegExp(`^(?:\\.{1,2}[\\\\/])?(?:${ARTIFACT_RELATIVE_ROOTS})[\\\\/]`, "i").test(source)) return "";
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

function isTableRow(line: string) {
  const trimmed = line.trim();
  return trimmed.includes("|") && /^\|?.+\|.+\|?$/.test(trimmed);
}

function isTableSeparator(line: string) {
  const cells = parseTableCells(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function parseTableCells(line: string) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function renderTable(lines: string[], out: string[], localFilePreviewUrl?: (filePath: string) => string) {
  if (!lines.length) return;
  if (lines.length < 2 || !isTableSeparator(lines[1])) {
    out.push(`<p>${renderInline(lines.map((line) => line.trim()).join(" "), localFilePreviewUrl)}</p>`);
    lines.length = 0;
    return;
  }

  const header = parseTableCells(lines[0]);
  const rows = lines.slice(2).filter(isTableRow).map(parseTableCells);
  const headHtml = `<thead><tr>${header.map((cell) => `<th>${renderInline(cell, localFilePreviewUrl)}</th>`).join("")}</tr></thead>`;
  const bodyHtml = rows.length
    ? `<tbody>${rows.map((row) => `<tr>${header.map((_cell, index) => `<td>${renderInline(row[index] || "", localFilePreviewUrl)}</td>`).join("")}</tr>`).join("")}</tbody>`
    : "";
  out.push(`<table>${headHtml}${bodyHtml}</table>`);
  lines.length = 0;
}

function renderMarkdown(markdown: string, localFilePreviewUrl?: (filePath: string) => string) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  const paragraph: string[] = [];
  const bullets: string[] = [];
  const numbers: string[] = [];
  const table: string[] = [];
  let code: string[] | null = null;

  const flushAll = () => {
    flushParagraph(paragraph, out, localFilePreviewUrl);
    renderList(bullets, false, out, localFilePreviewUrl);
    renderList(numbers, true, out, localFilePreviewUrl);
    renderTable(table, out, localFilePreviewUrl);
  };

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const raw = lines[lineIndex];
    const nextRaw = lines[lineIndex + 1] || "";
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

    if (isTableRow(raw) && (table.length > 0 || isTableSeparator(nextRaw))) {
      flushParagraph(paragraph, out, localFilePreviewUrl);
      renderList(bullets, false, out, localFilePreviewUrl);
      renderList(numbers, true, out, localFilePreviewUrl);
      table.push(raw);
      continue;
    }

    renderTable(table, out, localFilePreviewUrl);

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

function countMatches(value: string, pattern: RegExp) {
  return value.match(pattern)?.length || 0;
}

function splitStreamingMarkdown(markdown: string) {
  const source = String(markdown || "").replace(/\r\n/g, "\n");
  const fenceMatches = [...source.matchAll(/^```[\w-]*\s*$/gm)];
  if (fenceMatches.length % 2 === 1) {
    const lastFence = fenceMatches[fenceMatches.length - 1];
    const fenceLineEnd = source.indexOf("\n", lastFence.index || 0);
    return {
      stable: source.slice(0, lastFence.index).trimEnd(),
      tail: source.slice(fenceLineEnd >= 0 ? fenceLineEnd + 1 : source.length),
      tailKind: "code" as const
    };
  }

  const lastLineBreak = source.lastIndexOf("\n");
  if (lastLineBreak >= 0) {
    return {
      stable: source.slice(0, lastLineBreak).trimEnd(),
      tail: source.slice(lastLineBreak + 1),
      tailKind: "text" as const
    };
  }

  return { stable: "", tail: source, tailKind: "text" as const };
}

function cleanStreamingTail(value: string) {
  let text = redactInternalPromptText(value || "").replace(/\r\n/g, "\n").trimEnd();
  text = text.replace(/^(```+[\w-]*\s*)/gm, "");
  if (/^\s{0,3}#{1,6}\s*$/.test(text)) return "";
  if (/^\s{0,3}(?:[-*]|\d+\.)\s*$/.test(text)) return "";
  if (/^\s*\|?(?:\s*:?-{0,3}:?\s*\|)+\s*$/.test(text)) return "";
  if (/^\s*\|.+\|?\s*$/.test(text)) return "";
  text = text.replace(/!\[([^\]]*)\]\([^)]*$/, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*$/, "$1");
  text = text.replace(/\[([^\]]*)$/, "$1");
  if (countMatches(text, /\*\*/g) % 2 === 1) text = text.replace(/\*\*/g, "");
  if (countMatches(text, /`/g) % 2 === 1) text = text.replace(/`/g, "");
  return text;
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
  let text = "";
  if (typeof value === "string") {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value);
    }
  }
  text = redactInternalPromptText(text);
  if (text.length <= TOOL_DETAIL_RENDER_CAP) return text;
  const head = text.slice(0, Math.floor(TOOL_DETAIL_RENDER_CAP * 0.65));
  const tail = text.slice(-Math.floor(TOOL_DETAIL_RENDER_CAP * 0.2));
  return `${head}\n\n... [tool output truncated for display, ${text.length - head.length - tail.length} chars omitted] ...\n\n${tail}`;
}

function resultHasExplicitArtifactFields(value: unknown) {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) {
    return value.slice(0, 20).some(resultHasExplicitArtifactFields);
  }
  try {
    return Object.keys(value as Record<string, unknown>).some((key) => (
      /^(?:artifacts?|deliverables?|outputs?|files?|generatedFiles|generated_files|media|attachments?|file_path|filePath|path|url)$/i.test(key)
    ));
  } catch {
    return false;
  }
}

function extractArtifactsFromStructured(value: unknown): ArtifactItem[] {
  if (!value || typeof value !== "object") return [];
  const groups: ArtifactItem[][] = [];
  const visit = (entry: unknown, depth = 0, parentKey = "") => {
    if (depth > 4 || entry === undefined || entry === null) return;
    if (typeof entry === "string") {
      const keyAllowsText = /^(?:artifacts?|deliverables?|outputs?|files?|generatedFiles|generated_files|media|attachments?)$/i.test(parentKey);
      if (keyAllowsText) groups.push(extractArtifacts(entry, { allowBareFiles: true }));
      return;
    }
    if (Array.isArray(entry)) {
      entry.forEach((item) => visit(item, depth + 1, parentKey));
      return;
    }
    if (typeof entry !== "object") return;
    const record = entry as Record<string, unknown>;
    const rawPath = String(record.file_path || record.filePath || record.path || record.url || "").trim();
    if (rawPath) {
      const path = localPathFromSource(rawPath) || relativeArtifactPathFromSource(rawPath, false);
      if (path) {
        const rawType = String(record.file_type || record.fileType || record.type || "").toLowerCase();
        const fileType: ArtifactFileType = rawType === "directory" || rawType === "image" || rawType === "video" || rawType === "audio"
          ? rawType
          : artifactFileTypeFromPath(path);
        groups.push([{
          path,
          name: String(record.file_name || record.fileName || record.name || basenameFromPath(path)),
          fileType
        }]);
      }
    }
    for (const [key, child] of Object.entries(record)) {
      if (/^(?:artifacts?|deliverables?|outputs?|files?|generatedFiles|generated_files|media|attachments?)$/i.test(key)) {
        visit(child, depth + 1, key);
      }
    }
  };
  visit(value);
  return mergeArtifacts(...groups);
}

function extractArtifactsFromUnknown(value: unknown, options?: { allowBareFiles?: boolean }) {
  if (value === undefined || value === null || value === "") return [];
  if (typeof value === "object") {
    const structured = extractArtifactsFromStructured(value);
    if (structured.length) return structured;
  }
  if (typeof value !== "string") return [];
  const text = value;
  return extractArtifacts(text, { allowBareFiles: options?.allowBareFiles ?? true });
}

function isArtifactProducingTool(name?: string) {
  return /(?:write|save|send|export|render|image|artifact|deliverable)/i.test(String(name || ""));
}

function extractArtifactsFromSteps(steps: AgentStepDisclosure[], toolCalls: ToolCallDisclosure[] = []) {
  const groups: ArtifactItem[][] = [];
  for (const step of steps) {
    if (step.type === "media") {
      const source = step.filePath || step.url || step.previewUrl || "";
      const path = localPathFromSource(source) || relativeArtifactPathFromSource(source, false);
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
      const artifactTool = isArtifactProducingTool(step.name);
      if (artifactTool || resultHasExplicitArtifactFields(step.result)) {
        groups.push(extractArtifactsFromUnknown(step.result, { allowBareFiles: artifactTool }));
      }
    }
  }
  for (const tool of toolCalls) {
    const artifactTool = isArtifactProducingTool(tool.name);
    if (artifactTool || resultHasExplicitArtifactFields(tool.result)) {
      groups.push(extractArtifactsFromUnknown(tool.result, { allowBareFiles: artifactTool }));
    }
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
  const html = useMemo(
    () => renderMarkdown(redactInternalPromptText(content), localFilePreviewUrl),
    [content, localFilePreviewUrl]
  );
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
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function StreamingMarkdownBlock({
  content,
  localFilePreviewUrl,
  onOpenLocalFile
}: {
  content: string;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const { stable, tail, tailKind, cleanedTail } = useMemo(() => {
    const split = splitStreamingMarkdown(redactInternalPromptText(content));
    return { ...split, cleanedTail: cleanStreamingTail(split.tail) };
  }, [content]);
  return (
    <div className="streaming-markdown">
      {stable && <MarkdownBlock content={stable} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />}
      {tailKind === "code" ? (
        <pre className="streaming-code"><code>{tail}</code></pre>
      ) : cleanedTail ? (
        <div className="streaming-tail">
          <MarkdownBlock content={cleanedTail} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
        </div>
      ) : null}
    </div>
  );
}

function ThinkingStep({ content = "", running }: { content?: string; running?: boolean }) {
  const trimmed = redactInternalPromptText(content).trim();
  const shown = truncateReasoning(trimmed);
  return (
    <details className={`agent-step agent-thinking-step${running ? " is-running" : ""}`}>
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
    <details className={`agent-step agent-tool-step${isError ? " tool-failed" : ""}`}>
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

function stepIsRunning(step: AgentStepDisclosure) {
  return step.type === "thinking" && step.running
    || step.type === "tool" && (step.running || step.status === "running");
}

function stepIsError(step: AgentStepDisclosure) {
  return step.type === "tool" && (step.is_error === true || step.status === "error" || step.status === "failed");
}

function stepSummary(step?: AgentStepDisclosure) {
  if (!step) return "";
  if (step.type === "thinking") return step.running ? "正在思考" : "思考完成";
  if (step.type === "tool") return step.name ? `工具：${step.name}` : "工具调用";
  if (step.type === "media") return step.fileName ? `产物：${step.fileName}` : "产物已生成";
  if (step.type === "phase") return redactInternalPromptText(step.content || "").slice(0, 48);
  return redactInternalPromptText(step.content || "").slice(0, 48);
}

function ProcessDisclosure({
  pending,
  steps,
  legacySteps,
  onOpenLocalFile,
  localFilePreviewUrl
}: {
  pending?: boolean;
  steps: AgentStepDisclosure[];
  legacySteps: ReactNode[];
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  if (!steps.length && !legacySteps.length) return null;
  const runningStep = [...steps].reverse().find(stepIsRunning);
  const errorStep = [...steps].reverse().find(stepIsError);
  const currentStep = runningStep || errorStep || steps[steps.length - 1];
  const count = steps.length + legacySteps.length;
  const statusClass = pending ? " is-running" : errorStep ? " is-error" : " is-done";
  return (
    <details className={`agent-process-disclosure${pending ? " is-live" : ""}`}>
      <summary>
        <span className={`tool-status-dot${statusClass}`} aria-hidden="true" />
        <span>{pending ? `正在处理 · ${count} 步` : `调用过程 · ${count} 步`}</span>
        {currentStep && <span className="agent-process-current">{stepSummary(currentStep)}</span>}
      </summary>
      <div className="agent-steps">
        {steps.map((step, index) => renderStep(step, index, onOpenLocalFile, localFilePreviewUrl))}
        {legacySteps}
      </div>
    </details>
  );
}

function splitSteps(steps: AgentStepDisclosure[], content: string) {
  const lastContentIndex = steps.reduce((latest, step, index) => (
    step.type === "content" && !step.intermediate ? index : latest
  ), -1);
  const mainContent = redactInternalPromptText(content || (lastContentIndex >= 0 && steps[lastContentIndex].type === "content" ? steps[lastContentIndex].content || "" : ""));
  const visibleSteps = steps.filter((_step, index) => index !== lastContentIndex);
  return { mainContent, visibleSteps };
}

function MainAnswer({ content, pending, collapsible, artifacts = [], extraArtifacts = [], localFilePreviewUrl, onOpenLocalFile }: {
  content: string;
  pending?: boolean;
  collapsible?: boolean;
  artifacts?: AgentArtifact[];
  extraArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const legacyArtifacts = useMemo(
    () => pending ? mergeArtifacts(extraArtifacts) : mergeArtifacts(extractArtifacts(content, { allowBareFiles: false }), extraArtifacts),
    [content, pending, extraArtifacts]
  );
  const artifactShelf = <ArtifactShelf artifacts={artifacts} legacyArtifacts={legacyArtifacts} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />;
  if (!content) return artifactShelf;
  if (!collapsible || pending || content.length <= LONG_REPLY_COLLAPSE_CHARS) {
    return (
      <>
        {pending ? (
          <StreamingMarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
        ) : (
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
        )}
        {artifactShelf}
      </>
    );
  }
  if (expanded) {
    return (
      <div className="long-answer-disclosure is-expanded">
        <div className="long-answer-full">
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} />
          {artifactShelf}
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
        {artifactShelf}
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
  artifacts?: AgentArtifact[];
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  const steps = props.steps || [];
  const { mainContent, visibleSteps } = useMemo(() => splitSteps(steps, props.content), [steps, props.content]);
  const compactedSteps = useMemo(() => compactToolSteps(visibleSteps), [visibleSteps]);
  const stepArtifacts = useMemo(
    () => extractArtifactsFromSteps(compactedSteps, props.toolCalls || []),
    [compactedSteps, props.toolCalls]
  );
  const legacySteps: ReactNode[] = [];

  if (!steps.length && props.role !== "user") {
    if (props.reasoning?.trim()) legacySteps.push(<ThinkingStep key="reasoning" content={props.reasoning} />);
    (props.toolCalls || []).forEach((tool, index) => legacySteps.push(<ToolStep key={`tool-${index}`} step={tool} />));
  }

  return (
    <div className="message-content" aria-live={props.pending ? "polite" : undefined}>
      <ProcessDisclosure
        pending={props.pending}
        steps={compactedSteps}
        legacySteps={legacySteps}
        onOpenLocalFile={props.onOpenLocalFile}
        localFilePreviewUrl={props.localFilePreviewUrl}
      />
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} artifacts={props.artifacts} extraArtifacts={stepArtifacts} localFilePreviewUrl={props.localFilePreviewUrl} onOpenLocalFile={props.onOpenLocalFile} />
      {props.cancelled ? (
        <div className="agent-cancelled-tag">已中止</div>
      ) : props.pending ? (
        <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>
      ) : null}
    </div>
  );
}
