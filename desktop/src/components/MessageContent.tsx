import { memo, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  ExternalLink,
  Eye,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  MoreHorizontal,
  MonitorUp
} from "lucide-react";
import type { AgentArtifact, LocalJsonResult, LocalPathStat } from "../services/ecorexApi";
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
  previewDataUrl?: string;
  preview_url?: string;
};

const REASONING_RENDER_CAP = 4 * 1024;
const TOOL_DETAIL_RENDER_CAP = 6 * 1024;
const LONG_REPLY_COLLAPSE_CHARS = 1400;
const LONG_REPLY_PREVIEW_CHARS = 520;
const STREAM_RENDER_THROTTLE_CHARS = 12000;
const STREAM_MARKDOWN_CHUNK_CHARS = 8000;
const STREAM_LIVE_FULL_RENDER_CHARS = 32000;
const STREAM_LIVE_HEAD_CHARS = 7000;
const STREAM_LIVE_TAIL_CHARS = 4200;
const STREAM_LIVE_ARIA_CHARS = 12000;
const ARTIFACT_PENDING_MAX_RETRIES = 6;
type LocalFileContextHandler = (event: MouseEvent, file: LocalFilePayload) => void;
const ARTIFACT_PREVIEW_LIMIT = 6;
const ARTIFACT_RELATIVE_ROOTS = "deliverables|output|outputs|artifacts|images|assets";
const ARTIFACT_ABSOLUTE_POSIX_ROOTS = "Users|Volumes|home|tmp|var|mnt|opt|srv|workspace";
const ARTIFACT_PATH_PREFIX = `(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/(?:${ARTIFACT_ABSOLUTE_POSIX_ROOTS})[\\\\/]|(?:\\.{1,2}[\\\\/])?(?:${ARTIFACT_RELATIVE_ROOTS})[\\\\/])`;
const INTERRUPTED_TOOL_STATUSES = new Set(["aborted", "cancelled", "canceled", "paused", "stopped"]);

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
    if (localFilePreviewUrl) return localFilePreviewUrl(value);
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
    if (localFilePreviewUrl) return localFilePreviewUrl(value);
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
    if (localFilePreviewUrl) return localFilePreviewUrl(value);
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

type OrderedListItem = {
  value: string;
  number: number;
};

type DisplayArtifact = AgentArtifact & {
  legacyPath?: string;
};

type ArtifactAvailability = "pending" | "ready" | "missing" | "denied" | "error";
type ArtifactStatusJsonState = "pending" | "ready" | "failed" | "unknown";

const ARTIFACT_STATUS_READY = new Set(["complete", "completed", "done", "ready", "success", "saved", "cached"]);
const ARTIFACT_STATUS_FAILED = new Set(["failed", "error", "cancelled", "canceled", "aborted", "timeout", "timed_out"]);

function useThrottledStreamingContent(content: string, pending?: boolean) {
  const [visible, setVisible] = useState(content);
  const latestRef = useRef(content);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    latestRef.current = content;
    const shouldThrottle = pending && content.length >= STREAM_RENDER_THROTTLE_CHARS;
    if (!shouldThrottle) {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setVisible(content);
      return;
    }
    if (timerRef.current !== null) return;
    const delay = content.length >= 100000 ? 220 : 110;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setVisible(latestRef.current);
    }, delay);
  }, [content, pending]);

  useEffect(() => () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  return visible;
}

function artifactSourceKey(artifact: AgentArtifact) {
  return (artifactPath(artifact) || artifact.id || "").replace(/\\/g, "/").toLowerCase();
}

function isRemoteArtifactSource(value: string) {
  return /^https?:\/\//i.test(String(value || "").trim());
}

function shouldVerifyArtifact(artifact: AgentArtifact) {
  const source = artifactPath(artifact);
  return Boolean(source) && artifact.kind !== "url" && artifact.kind !== "diff" && !isRemoteArtifactSource(source);
}

function artifactExistsForKind(stat: LocalPathStat, kind: AgentArtifact["kind"]) {
  if (!stat.exists) return false;
  if (kind === "directory") return stat.isDirectory !== false;
  return stat.isFile !== false;
}

function artifactAvailabilityFromStat(stat: LocalPathStat, kind: AgentArtifact["kind"]): ArtifactAvailability {
  const status = String(stat.status || "").toLowerCase();
  if (status === "denied") return "denied";
  if (status === "error") return "error";
  return artifactExistsForKind(stat, kind) ? "ready" : "missing";
}

function artifactAvailabilityLabel(status: ArtifactAvailability) {
  if (status === "pending") return "checking local file";
  if (status === "denied") return "blocked by file permissions";
  if (status === "error") return "could not verify local file";
  if (status === "missing") return "local file not found";
  return "";
}

function artifactActionAllowed(status: ArtifactAvailability) {
  return status === "ready" || status === "error";
}

function artifactMenuStyle(anchor: { x: number; y: number; width: number; height: number }): CSSProperties {
  const menuWidth = 224;
  const menuHeight = 172;
  const margin = 8;
  const viewportWidth = typeof window === "undefined" ? 1024 : window.innerWidth;
  const viewportHeight = typeof window === "undefined" ? 768 : window.innerHeight;
  const preferredLeft = anchor.x + anchor.width - menuWidth;
  const preferredTop = anchor.y + anchor.height + 4;
  const left = Math.max(margin, Math.min(preferredLeft, viewportWidth - menuWidth - margin));
  const top = Math.max(margin, Math.min(preferredTop, viewportHeight - menuHeight - margin));
  return { left, top, width: menuWidth };
}

function localFilePayloadFromArtifact(artifact: DisplayArtifact, source: string, previewUrl?: string): LocalFilePayload {
  return {
    file_path: source,
    file_name: displayArtifactTitle(artifact),
    file_type: artifactFileTypeFromKind(artifact.kind),
    preview_url: previewUrl
  };
}

function artifactStatusJsonState(result: LocalJsonResult): ArtifactStatusJsonState {
  const transportStatus = String(result.status || "").toLowerCase();
  if (transportStatus === "denied") return "failed";
  if (transportStatus === "missing") return "pending";
  if (transportStatus === "error" && !result.data) return "failed";
  const data = result.data;
  if (!data || typeof data !== "object") return "unknown";
  const record = data as Record<string, unknown>;
  const status = String(record.status || record.state || record.phase || "").trim().toLowerCase();
  if (record.ok === false) return "failed";
  if (ARTIFACT_STATUS_FAILED.has(status)) return "failed";
  if (ARTIFACT_STATUS_READY.has(status)) return "ready";
  if (status) return "pending";
  if (record.ok === true) return "ready";
  return "pending";
}

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

function displayArtifactPath(artifact: AgentArtifact) {
  return artifact.path || artifact.relativePath || artifact.url || "";
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
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  artifacts: ImageArtifact[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
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
          onContextMenu={(event) => onLocalFileContextMenu?.(event, { file_path: artifact.path, file_name: artifact.name, file_type: "image" })}
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
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  artifacts: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
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
            onContextMenu={(event) => onLocalFileContextMenu?.(event, { file_path: artifact.path, file_name: artifact.name, file_type: artifact.fileType })}
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
  localFileJson,
  localFileStat,
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  artifacts?: AgentArtifact[];
  legacyArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  localFileJson?: (filePath: string) => Promise<LocalJsonResult>;
  localFileStat?: (filePath: string) => Promise<LocalPathStat>;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
}) {
  const [expanded, setExpanded] = useState(false);
  const [openMenu, setOpenMenu] = useState<{ id: string; x: number; y: number; width: number; height: number } | null>(null);
  const [availability, setAvailability] = useState<Record<string, ArtifactAvailability>>({});
  const statRetryCounts = useRef<Record<string, number>>({});
  const statRetryTimers = useRef<Record<string, number>>({});
  const statusRetryCounts = useRef<Record<string, number>>({});
  const statusRetryTimers = useRef<Record<string, number>>({});
  const rawItems = useMemo(() => mergeAgentArtifacts(artifacts, legacyArtifacts), [artifacts, legacyArtifacts]);
  useEffect(() => {
    if (!localFileStat) return;
    rawItems.forEach((artifact) => {
      if (!shouldVerifyArtifact(artifact)) return;
      const source = artifactPath(artifact);
      const key = artifactSourceKey(artifact);
      if (!source || availability[key]) return;
      setAvailability((current) => current[key] ? current : { ...current, [key]: "pending" });
      localFileStat(source)
        .then((stat) => {
          const nextStatus = artifactAvailabilityFromStat(stat, artifact.kind);
          if (nextStatus === "ready") {
            statRetryCounts.current[key] = 0;
          }
          setAvailability((current) => ({
            ...current,
            [key]: nextStatus
          }));
          if (nextStatus === "missing" && artifact.status === "pending") {
            const attempts = statRetryCounts.current[key] || 0;
            if (attempts < ARTIFACT_PENDING_MAX_RETRIES && !statRetryTimers.current[key]) {
              statRetryCounts.current[key] = attempts + 1;
              statRetryTimers.current[key] = window.setTimeout(() => {
                delete statRetryTimers.current[key];
                setAvailability((current) => {
                  const next = { ...current };
                  delete next[key];
                  return next;
                });
              }, Math.min(1000 + attempts * 500, 6000));
            }
          }
        })
        .catch(() => {
          setAvailability((current) => ({ ...current, [key]: "error" }));
        });
    });
  }, [availability, localFileStat, rawItems]);
  useEffect(() => {
    if (!localFileJson) return;
    const scheduleStatusRetry = (availabilityKey: string, timerKey: string) => {
      const attempts = statusRetryCounts.current[timerKey] || 0;
      if (attempts >= ARTIFACT_PENDING_MAX_RETRIES || statusRetryTimers.current[timerKey]) return;
      statusRetryCounts.current[timerKey] = attempts + 1;
      statusRetryTimers.current[timerKey] = window.setTimeout(() => {
        delete statusRetryTimers.current[timerKey];
        setAvailability((current) => {
          const value = current[availabilityKey];
          if (value === "ready" || value === "error" || value === "denied") return current;
          const next = { ...current };
          delete next[availabilityKey];
          return next;
        });
      }, Math.min(1000 + attempts * 500, 6000));
    };

    rawItems.forEach((artifact) => {
      if (!shouldVerifyArtifact(artifact) || artifact.status !== "pending" || !artifact.statusPath) return;
      const source = artifactPath(artifact);
      const key = artifactSourceKey(artifact);
      const timerKey = `${key}:status:${artifact.statusPath}`;
      const currentStatus = availability[key];
      if (currentStatus === "ready" || currentStatus === "error" || currentStatus === "denied") return;
      if (statusRetryTimers.current[timerKey]) return;
      setAvailability((current) => current[key] ? current : { ...current, [key]: "pending" });
      localFileJson(artifact.statusPath)
        .then(async (result) => {
          const statusState = artifactStatusJsonState(result);
          if (statusState === "failed") {
            statusRetryCounts.current[timerKey] = 0;
            const transportStatus = String(result.status || "").toLowerCase();
            setAvailability((current) => ({ ...current, [key]: transportStatus === "denied" ? "denied" : "error" }));
            return;
          }
          if (statusState === "ready") {
            if (localFileStat && source) {
              const stat = await localFileStat(source);
              const nextStatus = artifactAvailabilityFromStat(stat, artifact.kind);
              if (nextStatus === "ready" || nextStatus === "denied" || nextStatus === "error") {
                statusRetryCounts.current[timerKey] = 0;
                setAvailability((current) => ({ ...current, [key]: nextStatus }));
                return;
              }
            } else {
              statusRetryCounts.current[timerKey] = 0;
              setAvailability((current) => ({ ...current, [key]: "ready" }));
              return;
            }
          }
          scheduleStatusRetry(key, timerKey);
        })
        .catch(() => {
          scheduleStatusRetry(key, timerKey);
        });
    });
  }, [availability, localFileJson, localFileStat, rawItems]);
  useEffect(() => () => {
    Object.values(statRetryTimers.current).forEach((timer) => window.clearTimeout(timer));
    statRetryTimers.current = {};
    Object.values(statusRetryTimers.current).forEach((timer) => window.clearTimeout(timer));
    statusRetryTimers.current = {};
  }, []);
  const items = rawItems.filter((artifact) => {
    const source = artifactPath(artifact);
    if (String(artifact.status || "").toLowerCase() === "failed") return false;
    if (!localFileStat || !shouldVerifyArtifact(artifact)) return true;
    const key = artifactSourceKey(artifact);
    const statStatus = availability[key] || "pending";
    const attempts = statRetryCounts.current[key] || 0;
    if (statStatus === "ready") return true;
    if (statStatus === "pending") return true;
    if (statStatus === "missing" && String(artifact.status || "").toLowerCase() === "pending" && attempts < ARTIFACT_PENDING_MAX_RETRIES) return true;
    return !source;
  });
  if (!items.length) return null;

  const visibleArtifacts = expanded ? items : items.slice(0, 3);
  const hiddenCount = Math.max(items.length - visibleArtifacts.length, 0);
  const changedCount = items.filter((item) => item.intent === "changed-file").length;
  const title = changedCount === items.length ? "更改文件" : changedCount ? "产物与更改" : "生成产物";

  const runAction = async (artifact: DisplayArtifact, action: NonNullable<LocalFilePayload["open_action"]>) => {
    const source = artifactPath(artifact);
    if (!source) return;
    const sourceStatus = localFileStat && shouldVerifyArtifact(artifact)
      ? availability[artifactSourceKey(artifact)] || "pending"
      : "ready";
    if (action === "copy") {
      await navigator.clipboard?.writeText(source).catch(() => undefined);
      setOpenMenu(null);
      return;
    }
    if (!artifactActionAllowed(sourceStatus)) {
      setOpenMenu(null);
      return;
    }
    if (artifact.kind === "url" && artifact.url && action !== "reveal" && action !== "openWith") {
      window.open(artifact.url, "_blank", "noopener,noreferrer");
      setOpenMenu(null);
      return;
    }
    onOpenLocalFile?.({
      file_path: source,
      file_name: displayArtifactTitle(artifact),
      file_type: artifactFileTypeFromKind(artifact.kind),
      open_action: action,
      previewDataUrl: action === "preview" && /^data:image\//i.test(artifact.previewUrl || artifact.thumbnailUrl || "")
        ? artifact.previewUrl || artifact.thumbnailUrl
        : undefined,
      preview_url: action === "preview" ? artifact.previewUrl || artifact.thumbnailUrl : undefined
    });
    setOpenMenu(null);
  };

  useEffect(() => {
    if (!openMenu) return undefined;
    const close = () => setOpenMenu(null);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [openMenu]);

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
          const displayPath = displayArtifactPath(artifact);
          const previewSource = artifact.thumbnailUrl || artifact.previewUrl || source;
          const previewUrl = artifact.kind === "image" && previewSource
            ? safeImageUrl(previewSource, localFilePreviewUrl)
            : "";
          const artifactStatus = String(artifact.status || "").toLowerCase();
          const artifactKey = artifactSourceKey(artifact);
          const statStatus = localFileStat && shouldVerifyArtifact(artifact)
            ? availability[artifactKey] || "pending"
            : "ready";
          const pendingRetryExhausted = artifactStatus === "pending"
            && statStatus === "missing"
            && (statRetryCounts.current[artifactKey] || 0) >= ARTIFACT_PENDING_MAX_RETRIES;
          const availabilityStatus = artifactStatus === "failed"
            ? "error"
            : artifactStatus === "pending" && (statStatus === "pending" || statStatus === "missing") && !pendingRetryExhausted
              ? "pending"
              : statStatus;
          const availabilityText = artifactAvailabilityLabel(availabilityStatus);
          const blocked = !artifactActionAllowed(availabilityStatus);
          const displayPreviewUrl = artifactActionAllowed(availabilityStatus) ? previewUrl : "";
          const isPreviewableImage = artifact.kind === "image" && Boolean(displayPreviewUrl);
          const menuOpen = openMenu?.id === artifact.id;
          const menuStyle = openMenu && menuOpen ? artifactMenuStyle(openMenu) : undefined;
          const payload = source ? localFilePayloadFromArtifact(artifact, source, displayPreviewUrl || previewUrl) : null;
          return (
            <div
              className={`artifact-row is-${artifact.kind}${blocked ? ` is-${availabilityStatus}` : ""}`}
              key={artifact.id}
              title={displayPath || subtitle || name}
              onContextMenu={(event) => {
                if (!payload || !onLocalFileContextMenu) return;
                onLocalFileContextMenu(event, payload);
              }}
            >
              <span className="artifact-row-icon" aria-hidden="true">
                {displayPreviewUrl ? <img src={displayPreviewUrl} alt="" loading="lazy" /> : artifactIcon(fileType)}
              </span>
              <span className="artifact-row-main">
                <strong>{name}</strong>
                {availabilityText ? <small className={`artifact-status is-${availabilityStatus}`}>{availabilityText}</small> : null}
                {displayPath ? <small className="artifact-card-path">路径：{displayPath}</small> : subtitle && <small>{subtitle}</small>}
              </span>
              {artifact.stats && (
                <span className="artifact-row-stats" aria-label="变更统计">
                  {typeof artifact.stats.addedLines === "number" && <em className="is-added">+{artifact.stats.addedLines}</em>}
                  {typeof artifact.stats.removedLines === "number" && <em className="is-removed">-{artifact.stats.removedLines}</em>}
                </span>
              )}
              <span className="artifact-row-actions">
                {isPreviewableImage && (
                  <button type="button" className="artifact-icon-button" title="预览图片" aria-label={`预览图片 ${name}`} onClick={() => void runAction(artifact, "preview")}>
                    <Eye aria-hidden="true" />
                  </button>
                )}
                <button type="button" className="artifact-icon-button" title="本地打开" aria-label={`本地打开 ${name}`} disabled={blocked} onClick={() => void runAction(artifact, "open")}>
                  <MonitorUp aria-hidden="true" />
                </button>
                <span className="artifact-menu-wrap">
                  <button
                    type="button"
                    className="artifact-icon-button"
                    title="打开方式"
                    aria-label={`${name} 的打开方式`}
                    aria-expanded={menuOpen}
                    onClick={(event) => {
                      const rect = event.currentTarget.getBoundingClientRect();
                      setOpenMenu((current) => current?.id === artifact.id ? null : {
                        id: artifact.id,
                        x: rect.left,
                        y: rect.top,
                        width: rect.width,
                        height: rect.height
                      });
                    }}
                  >
                    <MoreHorizontal aria-hidden="true" />
                  </button>
                  {menuOpen && menuStyle && createPortal(
                    <span className="artifact-action-menu artifact-action-menu-portal" role="menu" style={menuStyle} onMouseDown={(event) => event.stopPropagation()}>
                      <button type="button" role="menuitem" disabled={blocked} onClick={() => void runAction(artifact, "open")}>本地打开</button>
                      <button type="button" role="menuitem" disabled={blocked} onClick={() => void runAction(artifact, "reveal")}>在文件夹中显示</button>
                      <button type="button" role="menuitem" disabled={blocked} onClick={() => void runAction(artifact, "openWith")}>选择应用打开</button>
                      <button type="button" role="menuitem" onClick={() => void runAction(artifact, "copy")}>复制路径</button>
                    </span>,
                    document.body
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
  const localPath = localPathFromSource(url) || relativeArtifactPathFromSource(url, true);
  if (localPath) {
    const localLink = `<a ${linkAttributesForUrl(url, localFilePreviewUrl)}>${escapeHtml(url)}</a>`;
    const previewSrc = mediaTypeFromUrl(localPath) === "image" ? safeImageUrl(localPath, localFilePreviewUrl) : "";
    if (previewSrc) {
      return `<span class="markdown-local-image-artifact">${localLink}<img class="markdown-image markdown-local-image-preview" src="${escapeHtml(previewSrc)}" alt="${escapeHtml(basenameFromPath(localPath))}" loading="lazy" /></span>${escapeHtml(trailing)}`;
    }
    return `${localLink}${escapeHtml(trailing)}`;
  }
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

function renderList(items: string[] | OrderedListItem[], ordered: boolean, out: string[], localFilePreviewUrl?: (filePath: string) => string) {
  if (!items.length) return;
  if (ordered) {
    const orderedItems = items as OrderedListItem[];
    const start = orderedItems[0]?.number && orderedItems[0].number !== 1 ? ` start="${orderedItems[0].number}"` : "";
    out.push(`<ol${start}>${orderedItems.map((item) => `<li>${renderInline(item.value, localFilePreviewUrl)}</li>`).join("")}</ol>`);
  } else {
    const bulletItems = items as string[];
    out.push(`<ul>${bulletItems.map((item) => `<li>${renderInline(item, localFilePreviewUrl)}</li>`).join("")}</ul>`);
  }
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

function normalizeMarkdownForRender(markdown: string) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  let inFence = false;
  return lines.map((raw) => {
    if (/^```[\w-]*\s*$/.test(raw.trim())) {
      inFence = !inFence;
      return raw;
    }
    if (inFence) return raw;
    const wrappedHeading = raw.match(/^\s*-{3,}\s*(#{1,6}.+?)\s*-{3,}\s*$/);
    if (wrappedHeading) return wrappedHeading[1].replace(/^(#{1,6})(\S)/, "$1 $2");
    return raw.replace(/^(\s{0,3}#{1,6})(\S)/, "$1 $2");
  }).join("\n");
}

function renderMarkdown(markdown: string, localFilePreviewUrl?: (filePath: string) => string) {
  const lines = normalizeMarkdownForRender(markdown).split("\n");
  const out: string[] = [];
  const paragraph: string[] = [];
  const bullets: string[] = [];
  const numbers: OrderedListItem[] = [];
  const table: string[] = [];
  let code: string[] | null = null;
  let lastOrderedNumber = 0;

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

    if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(raw)) {
      flushAll();
      out.push("<hr />");
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

    const heading = raw.match(/^(#{1,6})(?:\s+|(?=[\u4e00-\u9fff\d一二三四五六七八九十、]))(.+)$/u);
    if (heading) {
      flushAll();
      lastOrderedNumber = 0;
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

    const numbered = raw.match(/^\s*(\d+)\.\s+(.+)$/);
    if (numbered) {
      flushParagraph(paragraph, out, localFilePreviewUrl);
      renderList(bullets, false, out, localFilePreviewUrl);
      const parsedNumber = Number.parseInt(numbered[1] || "1", 10);
      const effectiveNumber = parsedNumber > lastOrderedNumber ? parsedNumber : lastOrderedNumber + 1;
      lastOrderedNumber = effectiveNumber;
      numbers.push({ number: effectiveNumber, value: numbered[2] || "" });
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
  const source = normalizeMarkdownForRender(markdown);
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
    let stable = source.slice(0, lastLineBreak).trimEnd();
    let tail = source.slice(lastLineBreak + 1);
    const stableLines = stable.split("\n");
    let lastContentLine = stableLines.length - 1;
    while (lastContentLine >= 0 && !stableLines[lastContentLine].trim()) {
      lastContentLine -= 1;
    }
    if (lastContentLine >= 0 && isTableRow(stableLines[lastContentLine])) {
      let tableStart = lastContentLine;
      while (tableStart > 0 && isTableRow(stableLines[tableStart - 1])) {
        tableStart -= 1;
      }
      const tableLines = stableLines.slice(tableStart, lastContentLine + 1);
      if (tableLines.length < 2 || !isTableSeparator(tableLines[1])) {
        const moved = stableLines.slice(tableStart).join("\n");
        stable = stableLines.slice(0, tableStart).join("\n").trimEnd();
        tail = [moved, tail].filter(Boolean).join("\n");
      }
    }
    return {
      stable,
      tail,
      tailKind: "text" as const
    };
  }

  return { stable: "", tail: source, tailKind: "text" as const };
}

function cleanStreamingTail(value: string) {
  let text = redactInternalPromptText(value || "").replace(/\r\n/g, "\n").trimEnd();
  text = text.replace(/^(```+[\w-]*\s*)/gm, "");
  const tailLines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  if (tailLines.length && tailLines.every((line) => (
    isTableRow(line)
    || isTableSeparator(line)
    || /^\|?(?:\s*:?-{0,3}:?\s*\|?)+\s*$/.test(line)
  ))) {
    return "";
  }
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
      /^(?:artifacts?|deliverables?|outputs?|output|outputPath|output_path|files?|generatedFiles|generated_files|media|attachments?)$/i.test(key)
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
    const rawPath = String(record.file_path || record.filePath || record.path || record.output || record.output_path || record.outputPath || record.url || "").trim();
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
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  content: string;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
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
  const handleContextMenu = (event: MouseEvent<HTMLDivElement>) => {
    if (!onLocalFileContextMenu) return;
    const target = event.target instanceof Element ? event.target : null;
    const anchor = target?.closest<HTMLAnchorElement>("a[data-ecorex-file-path]");
    const filePath = anchor?.dataset.ecorexFilePath || "";
    if (!anchor || !filePath) return;
    onLocalFileContextMenu(event, {
      file_path: filePath,
      file_name: anchor.dataset.ecorexFileName || basenameFromPath(filePath),
      file_type: mediaTypeFromUrl(filePath) || "file"
    });
  };
  return (
    <div
      className="markdown-content"
      onClick={handleClick}
      onContextMenu={handleContextMenu}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

const MemoMarkdownBlock = memo(MarkdownBlock);

function splitStableMarkdownChunks(content: string) {
  const source = String(content || "");
  if (source.length <= STREAM_MARKDOWN_CHUNK_CHARS) return [source];
  const chunks: string[] = [];
  let start = 0;
  const hasBalancedFences = (value: string) => {
    return ([...value.matchAll(/^```[\w-]*\s*$/gm)].length % 2) === 0;
  };
  while (start < source.length) {
    const remaining = source.length - start;
    if (remaining <= STREAM_MARKDOWN_CHUNK_CHARS) {
      chunks.push(source.slice(start));
      break;
    }
    const target = start + STREAM_MARKDOWN_CHUNK_CHARS;
    let boundary = source.lastIndexOf("\n\n", target);
    const minBoundary = start + Math.floor(STREAM_MARKDOWN_CHUNK_CHARS / 3);
    while (boundary > minBoundary && !hasBalancedFences(source.slice(start, boundary))) {
      boundary = source.lastIndexOf("\n\n", Math.max(start, boundary - 2));
    }
    if (boundary <= minBoundary) {
      boundary = source.lastIndexOf("\n", target);
    }
    if (boundary <= minBoundary) {
      boundary = Math.max(
        source.lastIndexOf("。", target),
        source.lastIndexOf("；", target),
        source.lastIndexOf("，", target),
        source.lastIndexOf(".", target)
      );
    }
    if (boundary <= minBoundary) {
      boundary = target;
    }
    chunks.push(source.slice(start, boundary).trimEnd());
    start = boundary;
    while (source[start] === "\n") start += 1;
  }
  return chunks.filter(Boolean);
}

function markdownPreviewContent(content: string) {
  const source = normalizeMarkdownForRender(content);
  if (source.length <= LONG_REPLY_PREVIEW_CHARS) return source;
  const minBoundary = Math.floor(LONG_REPLY_PREVIEW_CHARS / 2);
  const softLimit = Math.min(source.length, LONG_REPLY_PREVIEW_CHARS + 220);
  let boundary = source.lastIndexOf("\n\n", softLimit);
  if (boundary < minBoundary) boundary = source.lastIndexOf("\n", softLimit);
  if (boundary < minBoundary) {
    boundary = Math.max(
      source.lastIndexOf("。", softLimit),
      source.lastIndexOf("；", softLimit),
      source.lastIndexOf(".", softLimit)
    );
  }
  if (boundary < minBoundary) boundary = LONG_REPLY_PREVIEW_CHARS;
  return `${source.slice(0, boundary).trimEnd()}\n\n...`;
}

function splitStableMarkdownChunksV2(content: string) {
  const source = normalizeMarkdownForRender(content);
  if (source.length <= STREAM_MARKDOWN_CHUNK_CHARS) return [source];
  const chunks: string[] = [];
  let start = 0;
  const hasBalancedFences = (value: string) => (
    [...value.matchAll(/^```[\w-]*\s*$/gm)].length % 2 === 0
  );
  const acceptBoundary = (candidate: number, minBoundary: number) => (
    candidate > minBoundary && hasBalancedFences(source.slice(start, candidate))
  );
  while (start < source.length) {
    const remaining = source.length - start;
    if (remaining <= STREAM_MARKDOWN_CHUNK_CHARS) {
      chunks.push(source.slice(start));
      break;
    }
    const target = start + STREAM_MARKDOWN_CHUNK_CHARS;
    const minBoundary = start + Math.floor(STREAM_MARKDOWN_CHUNK_CHARS / 3);
    let boundary = source.lastIndexOf("\n\n", target);
    while (boundary > minBoundary && !hasBalancedFences(source.slice(start, boundary))) {
      boundary = source.lastIndexOf("\n\n", Math.max(start, boundary - 2));
    }
    if (!acceptBoundary(boundary, minBoundary)) {
      const lineBoundary = source.lastIndexOf("\n", target);
      boundary = acceptBoundary(lineBoundary, minBoundary) ? lineBoundary : 0;
    }
    if (!acceptBoundary(boundary, minBoundary)) {
      const punctuationBoundary = Math.max(
        source.lastIndexOf(".", target),
        source.lastIndexOf("!", target),
        source.lastIndexOf("?", target)
      );
      boundary = acceptBoundary(punctuationBoundary, minBoundary) ? punctuationBoundary : 0;
    }
    if (!acceptBoundary(boundary, minBoundary)) {
      const nextFence = source.slice(target).search(/^```[\w-]*\s*$/m);
      if (nextFence >= 0) {
        const fenceIndex = target + nextFence;
        const fenceEnd = source.indexOf("\n", fenceIndex);
        const nextBoundary = fenceEnd >= 0 ? fenceEnd : source.length;
        boundary = hasBalancedFences(source.slice(start, nextBoundary)) ? nextBoundary : 0;
      }
    }
    if (!acceptBoundary(boundary, minBoundary)) {
      boundary = hasBalancedFences(source.slice(start, target)) ? target : source.length;
    }
    chunks.push(source.slice(start, boundary).trimEnd());
    start = boundary;
    while (source[start] === "\n") start += 1;
  }
  return chunks.filter(Boolean);
}

function markdownPreviewContentSafe(content: string) {
  const raw = String(content || "");
  const source = normalizeMarkdownForRender(raw.slice(0, Math.min(raw.length, LONG_REPLY_PREVIEW_CHARS + 240)));
  if (source.length <= LONG_REPLY_PREVIEW_CHARS) return source;
  const minBoundary = Math.floor(LONG_REPLY_PREVIEW_CHARS / 2);
  const softLimit = Math.min(source.length, LONG_REPLY_PREVIEW_CHARS + 220);
  let boundary = source.lastIndexOf("\n\n", softLimit);
  if (boundary < minBoundary) boundary = source.lastIndexOf("\n", softLimit);
  if (boundary < minBoundary) {
    boundary = Math.max(source.lastIndexOf(".", softLimit), source.lastIndexOf("!", softLimit), source.lastIndexOf("?", softLimit));
  }
  if (boundary < minBoundary) boundary = LONG_REPLY_PREVIEW_CHARS;
  let preview = source.slice(0, boundary).trimEnd();
  const fenceMatches = [...preview.matchAll(/^```[\w-]*\s*$/gm)];
  if (fenceMatches.length % 2 === 1) {
    const lastFence = fenceMatches[fenceMatches.length - 1];
    preview = preview.slice(0, lastFence.index).trimEnd();
  }
  return `${preview}\n\n...`;
}

function streamingWindowHeadEnd(source: string) {
  const paragraphEnd = source.lastIndexOf("\n\n", STREAM_LIVE_HEAD_CHARS);
  if (paragraphEnd > STREAM_LIVE_HEAD_CHARS * 0.6) return paragraphEnd;
  const lineEnd = source.lastIndexOf("\n", STREAM_LIVE_HEAD_CHARS);
  if (lineEnd > STREAM_LIVE_HEAD_CHARS * 0.6) return lineEnd;
  return STREAM_LIVE_HEAD_CHARS;
}

function trimUnbalancedFenceTail(segment: string) {
  const fenceMatches = [...segment.matchAll(/^```[\w-]*\s*$/gm)];
  if (fenceMatches.length % 2 === 0) return segment;
  const lastFence = fenceMatches[fenceMatches.length - 1];
  return segment.slice(0, lastFence.index).trimEnd();
}

function streamingWindowMarkdown(content: string) {
  const source = redactInternalPromptText(content || "").replace(/\r\n/g, "\n");
  if (source.length <= STREAM_LIVE_FULL_RENDER_CHARS) return source;
  const rawHead = trimUnbalancedFenceTail(source.slice(0, streamingWindowHeadEnd(source)).trimEnd());
  const rawTailStart = Math.max(STREAM_LIVE_HEAD_CHARS, source.length - STREAM_LIVE_TAIL_CHARS);
  const paragraphTailStart = source.indexOf("\n\n", rawTailStart);
  const lineTailStart = source.indexOf("\n", rawTailStart);
  const tailStart = paragraphTailStart >= 0 && paragraphTailStart - rawTailStart < 1200
    ? paragraphTailStart + 2
    : lineTailStart >= 0 && lineTailStart - rawTailStart < 1200
      ? lineTailStart + 1
      : rawTailStart;
  const rawTail = source.slice(tailStart).trimStart();
  const head = normalizeMarkdownForRender(rawHead);
  const tail = normalizeMarkdownForRender(rawTail);
  const omitted = Math.max(source.length - rawHead.length - rawTail.length, 0);
  return `${head}\n\n[... ${omitted} chars streaming ...]\n\n${tail}`;
}

function StreamingStableMarkdown({
  content,
  localFilePreviewUrl,
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  content: string;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
}) {
  const chunks = useMemo(() => splitStableMarkdownChunksV2(content), [content]);
  return (
    <>
      {chunks.map((chunk, index) => (
        <MemoMarkdownBlock
          key={`${index}:${chunk.length}:${chunk.slice(0, 24)}`}
          content={chunk}
          localFilePreviewUrl={localFilePreviewUrl}
          onOpenLocalFile={onOpenLocalFile}
          onLocalFileContextMenu={onLocalFileContextMenu}
        />
      ))}
    </>
  );
}

function StreamingMarkdownBlock({
  content,
  localFilePreviewUrl,
  onOpenLocalFile,
  onLocalFileContextMenu
}: {
  content: string;
  localFilePreviewUrl?: (filePath: string) => string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
}) {
  const liveContent = useMemo(() => streamingWindowMarkdown(content), [content]);
  const { stable, tail, tailKind, cleanedTail } = useMemo(() => {
    const split = splitStreamingMarkdown(redactInternalPromptText(liveContent));
    return { ...split, cleanedTail: cleanStreamingTail(split.tail) };
  }, [liveContent]);
  return (
    <div className="streaming-markdown">
      {stable && <StreamingStableMarkdown content={stable} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />}
      {tailKind === "code" ? (
        <pre className="streaming-code"><code>{tail}</code></pre>
      ) : cleanedTail ? (
        <div className="streaming-tail">
          <MarkdownBlock content={cleanedTail} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
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
  const isInterrupted = toolStepIsInterrupted(step);
  const isError = !isInterrupted && (step.is_error === true || step.status === "error" || step.status === "failed");
  const running = step.running === true || step.status === "running";
  const statusClass = running ? " is-running" : isInterrupted ? " is-cancelled" : isError ? " is-error" : " is-done";
  return (
    <details className={`agent-step agent-tool-step${isError ? " tool-failed" : ""}${isInterrupted ? " tool-cancelled" : ""}`}>
      <summary title={running ? "工具正在执行，完成后可查看输入和结果" : isInterrupted ? "工具调用已中止，可展开查看已返回的信息" : "展开查看工具输入和结果"}>
        <span className={`tool-status-dot${statusClass}`} aria-hidden="true" />
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

function MediaStep({ step, onOpenLocalFile, onLocalFileContextMenu, localFilePreviewUrl }: {
  step: Extract<AgentStepDisclosure, { type: "media" }>;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
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
    const payload: LocalFilePayload = { file_path: localPath, file_name: fileName, file_type: "image" };
    return (
      <button
        type="button"
        className="agent-media-button"
        onClick={() => onOpenLocalFile(payload)}
        onContextMenu={(event) => onLocalFileContextMenu?.(event, payload)}
        title="点击在本地打开"
      >
        {image}
      </button>
    );
  }
  if (localPath && onOpenLocalFile) {
    const payload: LocalFilePayload = { file_path: localPath, file_name: fileName, file_type: step.fileType || "file" };
    return (
      <button
        type="button"
        className="agent-file-link"
        onClick={() => onOpenLocalFile(payload)}
        onContextMenu={(event) => onLocalFileContextMenu?.(event, payload)}
        title="点击在本地打开"
      >
        {fileName}
      </button>
    );
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
  localFilePreviewUrl?: (filePath: string) => string,
  onLocalFileContextMenu?: LocalFileContextHandler
): ReactNode {
  if (step.type === "thinking") return <ThinkingStep key={index} content={step.content} running={step.running} />;
  if (step.type === "tool") return <ToolStep key={index} step={step} />;
  if (step.type === "content") {
    return (
      <div className="agent-step agent-content-step" key={index}>
        <MarkdownBlock content={step.content || ""} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
      </div>
    );
  }
  if (step.type === "phase") {
    return <div className="agent-step agent-phase-step" key={index}>{redactInternalPromptText(step.content || "")}</div>;
  }
  return <MediaStep key={index} step={step} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} localFilePreviewUrl={localFilePreviewUrl} />;
}

function stepIsRunning(step: AgentStepDisclosure) {
  return step.type === "thinking" && step.running
    || step.type === "tool" && (step.running || step.status === "running");
}

function stepIsError(step: AgentStepDisclosure) {
  return step.type === "tool" && !stepIsInterrupted(step) && (step.is_error === true || step.status === "error" || step.status === "failed");
}

function toolStepIsInterrupted(step: Pick<ToolCallDisclosure, "status">) {
  return INTERRUPTED_TOOL_STATUSES.has(String(step.status || "").trim().toLowerCase());
}

function stepIsInterrupted(step: AgentStepDisclosure) {
  return step.type === "tool" && toolStepIsInterrupted(step);
}

function stepSummary(step?: AgentStepDisclosure) {
  if (!step) return "";
  if (step.type === "thinking") return step.running ? "正在思考" : "思考完成";
  if (step.type === "tool") return stepIsInterrupted(step) ? (step.name ? `已中止：${step.name}` : "已中止") : step.name ? `工具：${step.name}` : "工具调用";
  if (step.type === "media") return step.fileName ? `产物：${step.fileName}` : "产物已生成";
  if (step.type === "phase") return redactInternalPromptText(step.content || "").slice(0, 48);
  return redactInternalPromptText(step.content || "").slice(0, 48);
}

function ProcessDisclosure({
  pending,
  steps,
  legacySteps,
  onOpenLocalFile,
  onLocalFileContextMenu,
  localFilePreviewUrl
}: {
  pending?: boolean;
  steps: AgentStepDisclosure[];
  legacySteps: ReactNode[];
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  if (!steps.length && !legacySteps.length) return null;
  const runningStep = [...steps].reverse().find(stepIsRunning);
  const errorStep = [...steps].reverse().find(stepIsError);
  const interruptedStep = [...steps].reverse().find(stepIsInterrupted);
  const currentStep = runningStep || errorStep || interruptedStep || steps[steps.length - 1];
  const count = steps.length + legacySteps.length;
  const statusClass = pending ? " is-running" : errorStep ? " is-error" : interruptedStep ? " is-cancelled" : " is-done";
  return (
    <details className={`agent-process-disclosure${pending ? " is-live" : ""}`}>
      <summary>
        <span className={`tool-status-dot${statusClass}`} aria-hidden="true" />
        <span>{pending ? `正在处理 · ${count} 步` : `调用过程 · ${count} 步`}</span>
        {currentStep && <span className="agent-process-current">{stepSummary(currentStep)}</span>}
      </summary>
      <div className="agent-steps">
        {steps.map((step, index) => renderStep(step, index, onOpenLocalFile, localFilePreviewUrl, onLocalFileContextMenu))}
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

function MainAnswer({ content, pending, collapsible, artifacts = [], extraArtifacts = [], localFilePreviewUrl, localFileJson, localFileStat, onOpenLocalFile, onLocalFileContextMenu }: {
  content: string;
  pending?: boolean;
  collapsible?: boolean;
  artifacts?: AgentArtifact[];
  extraArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  localFileJson?: (filePath: string) => Promise<LocalJsonResult>;
  localFileStat?: (filePath: string) => Promise<LocalPathStat>;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
}) {
  const [expanded, setExpanded] = useState(false);
  const legacyArtifacts = useMemo(
    () => pending ? mergeArtifacts(extraArtifacts) : mergeArtifacts(extractArtifacts(content, { allowBareFiles: false }), extraArtifacts),
    [content, pending, extraArtifacts]
  );
  const artifactShelf = <ArtifactShelf artifacts={artifacts} legacyArtifacts={legacyArtifacts} localFilePreviewUrl={localFilePreviewUrl} localFileJson={localFileJson} localFileStat={localFileStat} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />;
  if (!content) return artifactShelf;
  if (!collapsible || pending || content.length <= LONG_REPLY_COLLAPSE_CHARS) {
    return (
      <>
        {pending ? (
          <StreamingMarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
        ) : (
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
        )}
        {artifactShelf}
      </>
    );
  }
  if (expanded) {
    return (
      <div className="long-answer-disclosure is-expanded">
        <div className="long-answer-full">
          <MarkdownBlock content={content} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
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
        <MarkdownBlock content={markdownPreviewContentSafe(content)} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
        {artifactShelf}
      </div>
      <button className="long-answer-toggle long-answer-expand-bottom" type="button" onClick={() => setExpanded(true)} title="长回复已默认收起，点击展开完整内容">
        展开完整回复
      </button>
    </div>
  );
}

export const MessageContent = memo(function MessageContent(props: {
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
  onLocalFileContextMenu?: LocalFileContextHandler;
  localFilePreviewUrl?: (filePath: string) => string;
  localFileJson?: (filePath: string) => Promise<LocalJsonResult>;
  localFileStat?: (filePath: string) => Promise<LocalPathStat>;
}) {
  const steps = props.steps || [];
  const visibleContent = useThrottledStreamingContent(props.content, props.pending);
  const { mainContent, visibleSteps } = useMemo(() => splitSteps(steps, visibleContent), [steps, visibleContent]);
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
    <div className="message-content" aria-live={props.pending && visibleContent.length <= STREAM_LIVE_ARIA_CHARS ? "polite" : undefined}>
      <ProcessDisclosure
        pending={props.pending}
        steps={compactedSteps}
        legacySteps={legacySteps}
        onOpenLocalFile={props.onOpenLocalFile}
        onLocalFileContextMenu={props.onLocalFileContextMenu}
        localFilePreviewUrl={props.localFilePreviewUrl}
      />
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} artifacts={props.artifacts} extraArtifacts={stepArtifacts} localFilePreviewUrl={props.localFilePreviewUrl} localFileJson={props.localFileJson} localFileStat={props.localFileStat} onOpenLocalFile={props.onOpenLocalFile} onLocalFileContextMenu={props.onLocalFileContextMenu} />
      {props.cancelled ? (
        <div className="agent-cancelled-tag">已中止</div>
      ) : props.pending ? (
        <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>
      ) : null}
    </div>
  );
});
