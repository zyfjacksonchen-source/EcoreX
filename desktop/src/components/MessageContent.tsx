import { memo, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import MarkdownIt from "markdown-it";
import {
  Brain,
  CircleCheck,
  ExternalLink,
  Eye,
  FileText,
  FolderOpen,
  Image as ImageIcon,
  MoreHorizontal,
  MonitorUp,
  Search,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  TriangleAlert,
  Wrench
} from "lucide-react";
import type { AgentArtifact, AgentArtifactValidity, LocalJsonResult, LocalPathStat, QualityEvidence } from "../services/ecorexApi";
import { redactInternalPromptText, redactToolDisclosureValue } from "../utils/redaction";

export type ToolCallDisclosure = {
  name?: string;
  arguments?: unknown;
  result?: unknown;
  qualityEvidence?: QualityEvidence;
  is_error?: boolean;
  status?: string;
  execution_time?: number;
  deadline_seconds?: number;
  max_seconds?: number;
  extension_count?: number;
  lastHeartbeatAt?: number;
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
      qualityEvidence?: QualityEvidence;
      status?: string;
      execution_time?: number;
      deadline_seconds?: number;
      max_seconds?: number;
      extension_count?: number;
      lastHeartbeatAt?: number;
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

const REASONING_MARKDOWN_RENDER_CAP = 64 * 1024;
const LONG_REPLY_COLLAPSE_CHARS = 1400;
const LONG_REPLY_PREVIEW_CHARS = 1400;
const STREAM_RENDER_THROTTLE_CHARS = 1200;
const STREAM_MARKDOWN_CHUNK_CHARS = 5000;
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

const OFFICE_HIDDEN_IMPLEMENTATION_EXTENSIONS = new Set([
  "py", "pyw", "js", "jsx", "ts", "tsx", "mjs", "cjs",
  "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
  "go", "rs", "java", "kt", "swift", "c", "cc", "cpp", "cxx", "h", "hpp",
  "css", "scss", "sass", "less", "vue", "svelte",
  "sql", "sqlite", "db", "toml", "yaml", "yml", "lock", "log"
]);

type ArtifactFileType = NonNullable<LocalFilePayload["file_type"]>;

type ArtifactItem = {
  path: string;
  name: string;
  fileType: ArtifactFileType;
};

type DisplayArtifact = AgentArtifact & {
  legacyPath?: string;
};

type ArtifactAvailability = "pending" | "ready" | "preview" | "missing" | "denied" | "error";
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
    const delay = content.length >= 100000 ? 80 : content.length >= 30000 ? 48 : 32;
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
  return canonicalArtifactKey({
    kind: artifact.kind,
    path: artifact.path,
    relativePath: artifact.relativePath,
    url: artifact.url,
    previewUrl: artifact.previewUrl,
    thumbnailUrl: artifact.thumbnailUrl,
    title: artifact.title,
    id: artifact.id
  });
}

function isRemoteArtifactSource(value: string) {
  return /^https?:\/\//i.test(String(value || "").trim());
}

function shouldVerifyArtifact(artifact: AgentArtifact) {
  const source = artifactPath(artifact);
  if (artifact.kind === "image" && (artifact.previewUrl || artifact.thumbnailUrl)) return false;
  return Boolean(source) && artifact.kind !== "url" && artifact.kind !== "diff" && !isRemoteArtifactSource(source);
}

function artifactExistsForKind(stat: LocalPathStat, kind: AgentArtifact["kind"]) {
  if (!stat.exists) return false;
  if (kind === "directory") return stat.isDirectory !== false;
  return stat.isFile !== false;
}

function artifactAvailabilityFromStat(stat: LocalPathStat, kind: AgentArtifact["kind"]): ArtifactAvailability {
  const status = String(stat.status || "").toLowerCase();
  if (status === "remote") return kind === "image" ? "preview" : "error";
  if (status === "denied") return "denied";
  if (status === "error") return "error";
  return artifactExistsForKind(stat, kind) ? "ready" : "missing";
}

function artifactAvailabilityLabel(status: ArtifactAvailability) {
  if (status === "pending") return "checking local file";
  if (status === "preview") return "preview only";
  if (status === "denied") return "blocked by file permissions";
  if (status === "error") return "could not verify local file";
  if (status === "missing") return "local file not found";
  return "";
}

function artifactActionAllowed(status: ArtifactAvailability) {
  return status === "ready";
}

function artifactPreviewAllowed(status: ArtifactAvailability) {
  return status === "ready" || status === "preview";
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

function artifactImageBasename(artifact: AgentArtifact) {
  if (artifact.kind !== "image") return "";
  const source = artifactPath(artifact) || artifact.previewUrl || artifact.thumbnailUrl || artifact.title || "";
  const name = basenameFromPath(source).toLowerCase();
  return /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(name) ? name : "";
}

function isBareArtifactSource(value?: string) {
  const source = String(value || "").trim();
  if (!source) return false;
  if (/^(?:https?:|file:|data:|blob:|\/api\/file|\/uploads\/)/i.test(source)) return false;
  if (/^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("\\\\") || source.startsWith("/")) return false;
  return !/[\\/]/.test(source);
}

function hasConcreteArtifactSource(artifact: AgentArtifact) {
  const source = artifactPath(artifact);
  if (!source) return Boolean(artifact.previewUrl || artifact.thumbnailUrl);
  return !isBareArtifactSource(source);
}

function dropBarePreviewDuplicateArtifacts(artifacts: DisplayArtifact[]) {
  const concreteImageNames = new Set<string>();
  artifacts.forEach((artifact) => {
    const name = artifactImageBasename(artifact);
    if (name && hasConcreteArtifactSource(artifact)) {
      concreteImageNames.add(name);
    }
  });
  if (!concreteImageNames.size) return artifacts;
  return artifacts.filter((artifact) => {
    const name = artifactImageBasename(artifact);
    if (!name || !concreteImageNames.has(name)) return true;
    return !isBareArtifactSource(artifactPath(artifact));
  });
}

function normalizeArtifactSource(value?: string) {
  return String(value || "")
    .trim()
    .replace(/^file:\/+/i, "")
    .replace(/\\/g, "/")
    .replace(/^\/([A-Za-z]:\/)/, "$1")
    .replace(/[?#].*$/, "")
    .replace(/\/+/g, "/")
    .toLowerCase();
}

function canonicalArtifactKey(input: {
  kind?: AgentArtifact["kind"] | ArtifactFileType;
  path?: string;
  relativePath?: string;
  url?: string;
  previewUrl?: string;
  thumbnailUrl?: string;
  title?: string;
  id?: string;
}) {
  const candidates = [
    input.path,
    input.relativePath,
    input.url,
    input.previewUrl,
    input.thumbnailUrl,
    input.title,
    input.id
  ].map(normalizeArtifactSource).filter(Boolean);
  const pathLike = candidates.find((value) => /[/.]/.test(value) && !value.startsWith("blob:") && !value.startsWith("data:"));
  if (pathLike) {
    return `${input.kind || "artifact"}:${pathLike}`;
  }
  return candidates[0] || "";
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
    const key = artifactSourceKey(artifact);
    if (!key || seen.has(key)) return;
    seen.add(key);
    items.push(artifact);
  };
  primary.forEach((artifact) => add(artifact));
  legacy.map(legacyArtifactToAgentArtifact).forEach(add);
  return dropBarePreviewDuplicateArtifacts(items);
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
  const normalized = normalizeArtifactSource(item.path);
  const parts = normalized.split("/").filter(Boolean);
  return `${item.fileType}:${parts.join("/") || normalized}`;
}

function artifactFileTypeFromPath(value: string): ArtifactFileType {
  const media = mediaTypeFromUrl(value);
  if (media) return media;
  if (/[\\/]$/.test(value)) return "directory";
  return "file";
}

function artifactExtension(value?: string) {
  const source = String(value || "").split(/[?#]/, 1)[0].trim();
  const fileName = basenameFromPath(source).toLowerCase();
  const match = /\.([a-z0-9]+)$/.exec(fileName);
  return match?.[1] || "";
}

function isOfficeVisibleArtifact(artifact: AgentArtifact) {
  if (artifact.kind === "url" || artifact.kind === "image" || artifact.kind === "video" || artifact.kind === "audio" || artifact.kind === "directory") {
    return true;
  }
  if (artifact.kind === "diff") return false;
  const ext = artifactExtension(artifactPath(artifact) || artifact.title || "");
  if (!ext) return true;
  return !OFFICE_HIDDEN_IMPLEMENTATION_EXTENSIONS.has(ext);
}

function isOfficeImplementationArtifact(artifact: AgentArtifact) {
  return !isOfficeVisibleArtifact(artifact);
}

function isMarkdownArtifact(artifact: AgentArtifact) {
  return artifactExtension(artifactPath(artifact) || artifact.title || "") === "md";
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
  const scopedPattern = new RegExp(`(${ARTIFACT_PATH_PREFIX}[^\\\`\\r\\n<>]*?\\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[\\s)\\]'"\\\`,.;:!?]|$)`, "gi");
  const looseLocalPattern = /((?:[A-Za-z]:[\\/]|\\\\|\/)[^\s`<>]*?\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[\s)\]'"`,.;:!?]|$)/gi;
  const items: ImageArtifact[] = [];
  const seen = new Set<string>();
  const add = (rawValue: string) => {
    const raw = cleanArtifactCandidate(rawValue || "");
    const path = localPathFromSource(raw) || relativeArtifactPathFromSource(raw);
    if (!path || seen.has(path)) return;
    seen.add(path);
    items.push({ path, name: basenameFromPath(path) });
  };
  for (const pattern of [scopedPattern, looseLocalPattern]) {
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(source)) && items.length < 12) {
      add(match[1] || "");
    }
  }
  return items;
}

function imageArtifactsToLegacyArtifacts(artifacts: ImageArtifact[]): ArtifactItem[] {
  return artifacts.map((artifact) => ({
    path: artifact.path,
    name: artifact.name,
    fileType: "image"
  }));
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
    `(${ARTIFACT_PATH_PREFIX}[^\\\`\\r\\n<>]*?\\.(${ARTIFACT_FILE_EXTENSIONS}))(?:[\\s)\\]'"\\\`,.;:!?]|$)`,
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
  onLocalFileContextMenu,
  onArtifactFeedback,
  onImageRetouchRequest
}: {
  artifacts?: AgentArtifact[];
  legacyArtifacts?: ArtifactItem[];
  localFilePreviewUrl?: (filePath: string) => string;
  localFileJson?: (filePath: string) => Promise<LocalJsonResult>;
  localFileStat?: (filePath: string) => Promise<LocalPathStat>;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
  onArtifactFeedback?: (artifact: AgentArtifact, validity: AgentArtifactValidity) => void | Promise<void>;
  onImageRetouchRequest?: (artifact: AgentArtifact, meta: { source: string; previewUrl: string; title: string }) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [openMenu, setOpenMenu] = useState<{ id: string; x: number; y: number; width: number; height: number } | null>(null);
  const openMenuElementRef = useRef<HTMLSpanElement | null>(null);
  const [availability, setAvailability] = useState<Record<string, ArtifactAvailability>>({});
  const [pendingFeedback, setPendingFeedback] = useState<Record<string, AgentArtifactValidity | "">>({});
  const statRetryCounts = useRef<Record<string, number>>({});
  const statRetryTimers = useRef<Record<string, number>>({});
  const statusRetryCounts = useRef<Record<string, number>>({});
  const statusRetryTimers = useRef<Record<string, number>>({});
  const [showImplementationFiles, setShowImplementationFiles] = useState(false);
  const mergedItems = useMemo(
    () => mergeAgentArtifacts(artifacts, legacyArtifacts),
    [artifacts, legacyArtifacts]
  );
  const hiddenImplementationItems = useMemo(
    () => mergedItems.filter(isOfficeImplementationArtifact),
    [mergedItems]
  );
  const rawItems = useMemo(
    () => showImplementationFiles ? mergedItems : mergedItems.filter(isOfficeVisibleArtifact),
    [mergedItems, showImplementationFiles]
  );
  const verificationItems = useMemo(
    () => expanded ? rawItems : rawItems.slice(0, ARTIFACT_PREVIEW_LIMIT),
    [expanded, rawItems]
  );
  useEffect(() => {
    if (!localFileStat) return;
    verificationItems.forEach((artifact) => {
      if (!shouldVerifyArtifact(artifact)) return;
      const source = artifactPath(artifact);
      const key = artifactSourceKey(artifact);
      if (!source || availability[key]) return;
      setAvailability((current) => current[key] ? current : { ...current, [key]: "pending" });
      localFileStat(source)
        .then((stat) => {
          const nextStatus = artifactAvailabilityFromStat(stat, artifact.kind);
          if (nextStatus === "ready" || nextStatus === "preview") {
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
  }, [availability, localFileStat, verificationItems]);
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
          if (value === "ready" || value === "preview" || value === "error" || value === "denied") return current;
          const next = { ...current };
          delete next[availabilityKey];
          return next;
        });
      }, Math.min(1000 + attempts * 500, 6000));
    };

    verificationItems.forEach((artifact) => {
      if (!shouldVerifyArtifact(artifact) || artifact.status !== "pending" || !artifact.statusPath) return;
      const source = artifactPath(artifact);
      const key = artifactSourceKey(artifact);
      const timerKey = `${key}:status:${artifact.statusPath}`;
      const currentStatus = availability[key];
      if (currentStatus === "ready" || currentStatus === "preview" || currentStatus === "error" || currentStatus === "denied") return;
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
              if (nextStatus === "ready" || nextStatus === "preview" || nextStatus === "denied" || nextStatus === "error") {
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
  }, [availability, localFileJson, localFileStat, verificationItems]);
  useEffect(() => () => {
    Object.values(statRetryTimers.current).forEach((timer) => window.clearTimeout(timer));
    statRetryTimers.current = {};
    Object.values(statusRetryTimers.current).forEach((timer) => window.clearTimeout(timer));
    statusRetryTimers.current = {};
  }, []);
  useEffect(() => {
    if (!openMenu) return undefined;
    const close = () => setOpenMenu(null);
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const path = typeof event.composedPath === "function" ? event.composedPath() : [];
      if (openMenuElementRef.current && path.includes(openMenuElementRef.current)) return;
      const trigger = target?.closest("[data-artifact-menu-trigger]") as HTMLElement | null;
      if (trigger?.dataset.artifactMenuTrigger === openMenu.id) return;
      close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [openMenu]);
  const items = rawItems.filter((artifact) => {
    const source = artifactPath(artifact);
    if (String(artifact.status || "").toLowerCase() === "failed") return false;
    if (!localFileStat || !shouldVerifyArtifact(artifact)) return true;
    const key = artifactSourceKey(artifact);
    const statStatus = availability[key] || "pending";
    const attempts = statRetryCounts.current[key] || 0;
    if (statStatus === "ready" || statStatus === "preview") return true;
    if (statStatus === "pending") return true;
    if (statStatus === "missing" && String(artifact.status || "").toLowerCase() === "pending" && attempts < ARTIFACT_PENDING_MAX_RETRIES) return true;
    if (artifact.kind === "image" && (artifact.previewUrl || artifact.thumbnailUrl)) return true;
    return !source;
  });
  if (!items.length && !hiddenImplementationItems.length) return null;

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
    if (!artifactActionAllowed(sourceStatus) && !(action === "preview" && sourceStatus === "preview")) {
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

  const submitArtifactFeedback = async (artifact: DisplayArtifact, validity: AgentArtifactValidity) => {
    if (!onArtifactFeedback) return;
    const key = artifactSourceKey(artifact);
    setPendingFeedback((current) => ({ ...current, [key]: validity }));
    try {
      await onArtifactFeedback(artifact, validity);
    } finally {
      setPendingFeedback((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
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
          const rawAvailabilityStatus = artifactStatus === "failed"
            ? "error"
            : artifactStatus === "pending" && (statStatus === "pending" || statStatus === "missing") && !pendingRetryExhausted
              ? "pending"
              : statStatus;
          const imagePreviewFallback = artifact.kind === "image"
            && Boolean(previewUrl)
            && (rawAvailabilityStatus === "missing" || rawAvailabilityStatus === "error" || rawAvailabilityStatus === "denied");
          const availabilityStatus = imagePreviewFallback ? "preview" : rawAvailabilityStatus;
          const availabilityText = artifactAvailabilityLabel(availabilityStatus);
          const openBlocked = !artifactActionAllowed(availabilityStatus);
          const blocked = openBlocked && !(artifact.kind === "image" && availabilityStatus === "preview");
          const displayPreviewUrl = artifactPreviewAllowed(availabilityStatus) ? previewUrl : "";
          const isPreviewableImage = artifact.kind === "image" && Boolean(displayPreviewUrl);
          const canRetouchImage = Boolean(onImageRetouchRequest && isPreviewableImage && source && availabilityStatus === "ready" && !blocked);
          const markdownArtifact = isMarkdownArtifact(artifact);
          const menuOpen = openMenu?.id === artifact.id;
          const menuStyle = openMenu && menuOpen ? artifactMenuStyle(openMenu) : undefined;
          const payload = source ? localFilePayloadFromArtifact(artifact, source, displayPreviewUrl || previewUrl) : null;
          const feedbackSignal = artifact.artifactFeedbackSignal || "default";
          const feedbackValidity = artifact.artifactValidity || "valid";
          const feedbackPending = pendingFeedback[artifactKey] || "";
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
                <QualityEvidenceBadge evidence={artifact.qualityEvidence} compact />
                {displayPath ? <small className="artifact-card-path">路径：{displayPath}</small> : subtitle && <small>{subtitle}</small>}
              </span>
              {artifact.stats && (
                <span className="artifact-row-stats" aria-label="变更统计">
                  {typeof artifact.stats.addedLines === "number" && <em className="is-added">+{artifact.stats.addedLines}</em>}
                  {typeof artifact.stats.removedLines === "number" && <em className="is-removed">-{artifact.stats.removedLines}</em>}
                </span>
              )}
              <span className={`artifact-row-actions${markdownArtifact ? " is-pinned" : ""}`}>
                {onArtifactFeedback && (
                  <>
                    <button
                      type="button"
                      className={`artifact-icon-button artifact-feedback-button${feedbackSignal === "thumbs_up" ? " is-selected is-valid" : ""}`}
                      title="标记为有效产物"
                      aria-label={`标记为有效产物 ${name}`}
                      aria-pressed={feedbackValidity === "valid" && feedbackSignal === "thumbs_up"}
                      disabled={Boolean(feedbackPending)}
                      onClick={() => void submitArtifactFeedback(artifact, "valid")}
                    >
                      <ThumbsUp aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className={`artifact-icon-button artifact-feedback-button${feedbackSignal === "thumbs_down" ? " is-selected is-invalid" : ""}`}
                      title="标记为无效产物"
                      aria-label={`标记为无效产物 ${name}`}
                      aria-pressed={feedbackValidity === "invalid"}
                      disabled={Boolean(feedbackPending)}
                      onClick={() => void submitArtifactFeedback(artifact, "invalid")}
                    >
                      <ThumbsDown aria-hidden="true" />
                    </button>
                  </>
                )}
                {isPreviewableImage && (
                  <button type="button" className="artifact-icon-button" title="预览图片" aria-label={`预览图片 ${name}`} onClick={() => void runAction(artifact, "preview")}>
                    <Eye aria-hidden="true" />
                  </button>
                )}
                {canRetouchImage && (
                  <button
                    type="button"
                    className="artifact-icon-button"
                    title="精准修图"
                    aria-label={`精准修图 ${name}`}
                    onClick={() => onImageRetouchRequest?.(artifact, { source, previewUrl: displayPreviewUrl || previewUrl, title: name })}
                  >
                    <Sparkles aria-hidden="true" />
                  </button>
                )}
                <button type="button" className="artifact-icon-button" title={markdownArtifact ? "本地打开 Markdown" : "本地打开"} aria-label={`本地打开 ${name}`} disabled={openBlocked} onClick={() => void runAction(artifact, "open")}>
                  <MonitorUp aria-hidden="true" />
                </button>
                <span className="artifact-menu-wrap">
                  <button
                    type="button"
                    className="artifact-icon-button"
                    title="打开方式"
                    aria-label={`${name} 的打开方式`}
                    aria-expanded={menuOpen}
                    data-artifact-menu-trigger={artifact.id}
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
                    <span ref={openMenuElementRef} className="artifact-action-menu artifact-action-menu-portal" role="menu" style={menuStyle} onMouseDown={(event) => event.stopPropagation()}>
                      <button type="button" role="menuitem" disabled={openBlocked} onClick={() => void runAction(artifact, "open")}>本地打开</button>
                      <button type="button" role="menuitem" disabled={openBlocked} onClick={() => void runAction(artifact, "reveal")}>在文件夹中显示</button>
                      <button type="button" role="menuitem" disabled={openBlocked} onClick={() => void runAction(artifact, "openWith")}>选择应用打开</button>
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
      <div className="artifact-shelf-footer">
        {items.length > 3 && (
          <button className="artifact-grid-toggle" type="button" onClick={() => setExpanded((current) => !current)}>
            {expanded ? "收起产物" : `显示另外 ${hiddenCount} 个`}
          </button>
        )}
        {hiddenImplementationItems.length > 0 && (
          <button className="artifact-grid-toggle is-muted" type="button" onClick={() => setShowImplementationFiles((current) => !current)}>
            {showImplementationFiles ? "隐藏实现文件" : `显示实现文件 ${hiddenImplementationItems.length} 个`}
          </button>
        )}
      </div>
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

function renderPlainTextWithLinks(value: string, localFilePreviewUrl?: (filePath: string) => string) {
  const localPathPrefix = String.raw`(?:[A-Za-z]:[\\/]|\\\\|\/(?:Users|Volumes|home|tmp|var|mnt|opt|srv|Applications)\/)`;
  const pattern = new RegExp(
    String.raw`(["'])(${localPathPrefix}[^<>\r\n]*?)\1|((?:https?:\/\/|file:\/\/)[^\s<>"']+|${localPathPrefix}[^\s<>"']+)`,
    "g"
  );
  let html = "";
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value)) !== null) {
    html += escapeHtml(value.slice(cursor, match.index));
    const quote = match[1] || "";
    const quotedPath = match[2] || "";
    const rawPath = match[3] || "";
    if (quote && quotedPath) {
      html += `${escapeHtml(quote)}${renderBareUrl(quotedPath, localFilePreviewUrl)}${escapeHtml(quote)}`;
    } else {
      html += renderBareUrl(rawPath || match[0], localFilePreviewUrl);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor === 0) return "";
  html += escapeHtml(value.slice(cursor));
  return html;
}

function linkifyPlainTextSegments(html: string, localFilePreviewUrl?: (filePath: string) => string) {
  if (typeof document === "undefined") return html;
  const template = document.createElement("template");
  template.innerHTML = html;
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest("pre, code, a, script, style")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const textNodes: Text[] = [];
  let current = walker.nextNode();
  while (current) {
    if (current instanceof Text) textNodes.push(current);
    current = walker.nextNode();
  }
  for (const node of textNodes) {
    const linked = renderPlainTextWithLinks(node.nodeValue || "", localFilePreviewUrl);
    if (!linked) continue;
    const fragmentTemplate = document.createElement("template");
    fragmentTemplate.innerHTML = linked;
    node.replaceWith(fragmentTemplate.content.cloneNode(true));
  }
  return template.innerHTML;
}

function normalizeMarkdownForRender(markdown: string) {
  return String(markdown || "")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.replace(/^[\uFEFF\u200B-\u200D]+/, ""))
    .join("\n");
}

type MarkdownRenderEnv = {
  localFilePreviewUrl?: (filePath: string) => string;
};

const cowMarkdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: true,
  highlight: (source) => escapeHtml(source)
});

const defaultMarkdownLinkOpen = cowMarkdown.renderer.rules.link_open || ((tokens, index, options, env, self) => (
  self.renderToken(tokens, index, options)
));

cowMarkdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index];
  const rawHref = token.attrGet("href") || "";
  const renderEnv = env as MarkdownRenderEnv;
  const localPath = localPathFromSource(rawHref) || relativeArtifactPathFromSource(rawHref, true);
  token.attrSet("href", safeUrl(rawHref, renderEnv.localFilePreviewUrl));
  token.attrSet("target", "_blank");
  token.attrSet("rel", "noopener noreferrer");
  if (localPath) {
    const existingClass = token.attrGet("class");
    token.attrSet("class", [existingClass, "markdown-local-file-link"].filter(Boolean).join(" "));
    token.attrSet("data-ecorex-file-path", localPath);
    token.attrSet("data-ecorex-file-name", basenameFromPath(localPath));
  }
  return defaultMarkdownLinkOpen(tokens, index, options, env, self);
};

cowMarkdown.renderer.rules.image = (tokens, index, _options, env) => {
  const token = tokens[index];
  const renderEnv = env as MarkdownRenderEnv;
  const rawSrc = token.attrGet("src") || "";
  const alt = token.content || token.attrGet("alt") || basenameFromPath(rawSrc);
  const src = /^data:/i.test(rawSrc) ? "" : safeImageUrl(rawSrc, renderEnv.localFilePreviewUrl);
  if (!src) return escapeHtml(alt);
  return `<img class="markdown-image" src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy" />`;
};

function plainTextFromHtml(value: string) {
  return decodeBasicEntities(String(value || "").replace(/<[^>]*>/g, ""));
}

function injectLinkedMediaPreviews(html: string, localFilePreviewUrl?: (filePath: string) => string) {
  return html.replace(/<a\b([^>]*?)href="([^"]+)"([^>]*)>([\s\S]*?)<\/a>/gi, (match, before: string, href: string, after: string, labelHtml: string) => {
    if (/\bdata-ecorex-file-path=/i.test(`${before} ${after}`)) return match;
    const type = mediaTypeFromUrl(href);
    if (!type) return match;
    if (type === "image") {
      const src = safeImageUrl(href, localFilePreviewUrl);
      return src ? `<img class="markdown-image" src="${escapeHtml(src)}" alt="${escapeHtml(plainTextFromHtml(labelHtml) || basenameFromPath(href))}" loading="lazy" />` : match;
    }
    const src = safeMediaUrl(href, localFilePreviewUrl);
    if (!src) return match;
    if (type === "video") return `<video class="markdown-video" src="${escapeHtml(src)}" controls></video>`;
    if (type === "audio") return `<audio class="markdown-audio" src="${escapeHtml(src)}" controls></audio>`;
    return `<a${before}href="${escapeHtml(safeUrl(href, localFilePreviewUrl))}"${after}>${labelHtml}</a>`;
  });
}

function renderMarkdown(markdown: string, localFilePreviewUrl?: (filePath: string) => string) {
  try {
    const source = normalizeMarkdownForRender(markdown);
    let html = cowMarkdown.render(source, { localFilePreviewUrl } satisfies MarkdownRenderEnv);
    html = injectLinkedMediaPreviews(html, localFilePreviewUrl);
    html = linkifyPlainTextSegments(html, localFilePreviewUrl);
    return html;
  } catch {
    return `<p>${escapeHtml(markdown).replace(/\n/g, "<br />")}</p>`;
  }
}

function truncateReasoning(text: string) {
  return { text, truncated: text.length > REASONING_MARKDOWN_RENDER_CAP };
}

function formatToolValue(value: unknown) {
  if (value === undefined || value === null || value === "") return "(none)";
  const redactedValue = redactToolDisclosureValue(value);
  let text = "";
  if (typeof redactedValue === "string") {
    text = redactedValue;
  } else {
    try {
      text = JSON.stringify(redactedValue, null, 2);
    } catch {
      text = String(redactedValue);
    }
  }
  text = redactInternalPromptText(text);
  return text;
}

const QUALITY_EVIDENCE_ALLOWED_GATES = new Set([
  "artifact-tool-authoring",
  "chart-integrity",
  "chart-render",
  "dashboard-structure",
  "design-preset",
  "export-verify",
  "font-size-check",
  "formula-audit",
  "generation-verify",
  "artifact-integrity",
  "anomaly-check",
  "decode-valid",
  "layout-bounds",
  "layout-inspection",
  "non-blank",
  "overlap-check",
  "overlay-ghosting-check",
  "page-render",
  "redline-preserve",
  "reference-fidelity",
  "render-docx",
  "render-preview",
  "seam-check",
  "subject-structure-check",
  "story-flow",
  "structure-check",
  "table-geometry",
  "table-structure",
  "text-orientation",
  "text-glyph-check",
  "typed-values",
  "visual-diff",
  "visual-inspection",
  "watermark-check"
]);

const QUALITY_EVIDENCE_DETAIL_KEYS = new Set([
  "anomaly_risk",
  "blank_pages",
  "blank_risk",
  "chart_issues",
  "charts",
  "comment_id_mismatches",
  "comment_refs",
  "comments",
  "date_text",
  "diff",
  "diff_mismatches",
  "decode_error",
  "decode_valid",
  "empty_sheets",
  "empty_slides",
  "empty_text_pages",
  "error_cells",
  "expected_min",
  "export",
  "extraction_errors",
  "formula_errors",
  "formulas",
  "finalized",
  "generated",
  "glyph_fragments",
  "glyph_issues",
  "headings",
  "image_only_pages",
  "issues",
  "manual_visual_review",
  "missing_titles",
  "non_empty_sheets",
  "numeric_text",
  "overlay_risk",
  "out_of_bounds",
  "overlaps",
  "page_count",
  "page_size_variants",
  "pages_compared",
  "paragraphs",
  "rendered",
  "reference_count",
  "reference_mismatch",
  "reference_similarity",
  "reference_status",
  "references_compared",
  "remote_references",
  "max_retries",
  "retry_count",
  "retry_gate",
  "retry_recommended",
  "rotation_issues",
  "route",
  "sections",
  "saliency_pct",
  "seam_axis",
  "seam_risk",
  "sheets",
  "size_bytes",
  "slides",
  "subject_review",
  "subject_risk",
  "table_candidates",
  "table_issues",
  "table_text_candidates",
  "tables",
  "text_density",
  "text_like_regions",
  "text_pages",
  "titles",
  "tracked_changes",
  "translucent_pct",
  "unspecified",
  "unique_color_buckets",
  "violations",
  "watermark_risk"
]);

const QUALITY_EVIDENCE_DETAIL_ENUMS = new Set([
  "artifact-tool",
  "decode-error",
  "decompressionbomberror",
  "decompressionbombwarning",
  "empty",
  "filenotfounderror",
  "horizontal",
  "missing",
  "not_applicable",
  "none",
  "oserror",
  "pass",
  "pending",
  "pillow-missing",
  "skipped",
  "template-following",
  "unspecified",
  "unidentifiedimageerror",
  "unknown",
  "valueerror",
  "vertical",
  "verified",
  "verified-existing-deck",
  "artifact-integrity",
  "anomaly-check",
  "decode-valid",
  "final",
  "needs_review",
  "non-blank",
  "overlay-ghosting-check",
  "reference-fidelity",
  "retry",
  "seam-check",
  "subject-structure-check",
  "text-glyph-check",
  "watermark-check"
]);

const QUALITY_EVIDENCE_STATUSES = new Set(["pass", "fail", "warn", "pending", "skipped", "unknown"]);
const REFERENCE_FIDELITY_SKIPPED_REVIEW_MARKER = "reference-fidelity-skipped-review";
const QUALITY_EVIDENCE_KINDS = new Set(["presentation", "spreadsheet", "document", "pdf", "image"]);

function qualityEvidenceText(value: unknown, limit: number) {
  const text = String(value ?? "").trim().replace(/\s+/g, " ");
  return text ? text.slice(0, limit) : "";
}

function qualityEvidenceGate(value: unknown) {
  const gate = qualityEvidenceText(value, 72).toLowerCase();
  return QUALITY_EVIDENCE_ALLOWED_GATES.has(gate) ? gate : "";
}

function qualityEvidenceSafeStatus(value: unknown) {
  const status = qualityEvidenceText(value, 24).toLowerCase();
  return QUALITY_EVIDENCE_STATUSES.has(status) ? status : "unknown";
}

function qualityEvidenceSafeKind(value: unknown) {
  const kind = qualityEvidenceText(value, 32).toLowerCase();
  return QUALITY_EVIDENCE_KINDS.has(kind) ? kind : "";
}

function qualityEvidenceDetail(value: unknown) {
  const parts: string[] = [];
  for (const rawPart of String(value ?? "").split(";")) {
    if (parts.length >= 12) break;
    const separator = rawPart.indexOf("=");
    if (separator < 0) continue;
    const key = rawPart.slice(0, separator).trim().toLowerCase();
    const val = rawPart.slice(separator + 1).trim().toLowerCase();
    if (!QUALITY_EVIDENCE_DETAIL_KEYS.has(key)) continue;
    if (/^\d+$/.test(val)) {
      parts.push(`${key}=${Number.parseInt(val, 10)}`);
    } else if (QUALITY_EVIDENCE_DETAIL_ENUMS.has(val)) {
      parts.push(`${key}=${val}`);
    }
  }
  return parts.join("; ").slice(0, 240);
}

function qualityEvidenceCheck(value: unknown) {
  if (!value || Array.isArray(value) || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  const detail = qualityEvidenceDetail(record.detail || record.summary || "");
  return {
    id: qualityEvidenceGate(record.id || record.gate) || "unknown-check",
    status: qualityEvidenceSafeStatus(record.status),
    ...(detail ? { detail } : {})
  };
}

function qualityEvidenceFromUnknown(value: unknown): QualityEvidence | undefined {
  if (!value) return undefined;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed.startsWith("{") || trimmed.length > 64 * 1024) return undefined;
    try {
      return qualityEvidenceFromUnknown(JSON.parse(trimmed) as unknown);
    } catch {
      return undefined;
    }
  }
  if (Array.isArray(value) || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  const direct = record.qualityEvidence || record.quality_evidence;
  if (direct && typeof direct === "object" && !Array.isArray(direct)) {
    return qualityEvidenceFromUnknown(direct);
  }
  if (Array.isArray(record.qualityGates) || Array.isArray(record.checks)) {
    const checks = Array.isArray(record.checks)
      ? record.checks.map(qualityEvidenceCheck).filter((item): item is NonNullable<typeof item> => !!item).slice(0, 48)
      : [];
    const qualityGates = Array.isArray(record.qualityGates)
      ? record.qualityGates.map(qualityEvidenceGate).filter(Boolean).slice(0, 40)
      : [];
    const missingQualityGates = Array.isArray(record.missingQualityGates)
      ? record.missingQualityGates.map(qualityEvidenceGate).filter(Boolean).slice(0, 40)
      : [];
    return {
      ...(qualityEvidenceSafeKind(record.kind) ? { kind: qualityEvidenceSafeKind(record.kind) } : {}),
      ...(qualityGates.length ? { qualityGates } : {}),
      ...(checks.length ? { checks } : {}),
      ...(missingQualityGates.length ? { missingQualityGates } : {}),
      status: qualityEvidenceSafeStatus(record.status),
      redacted: true,
      qualityEvidenceSanitized: true
    };
  }
  return undefined;
}

function qualityEvidenceStatus(evidence?: QualityEvidence) {
  const raw = String(evidence?.status || "").toLowerCase();
  if (raw === "pass" || raw === "fail" || raw === "warn" || raw === "pending" || raw === "skipped") return raw;
  const checks = Array.isArray(evidence?.checks) ? evidence?.checks || [] : [];
  if (checks.some((check) => String(check.status || "").toLowerCase() === "fail")) return "fail";
  if ((evidence?.missingQualityGates || []).length) return "fail";
  if (checks.some((check) => String(check.status || "").toLowerCase() === "warn")) return "warn";
  if (checks.some((check) => String(check.status || "").toLowerCase() === "pending")) return "pending";
  return "unknown";
}

function qualityEvidenceKindLabel(kind?: string) {
  const normalized = String(kind || "").toLowerCase();
  if (normalized === "presentation") return "PPT";
  if (normalized === "spreadsheet") return "Excel";
  if (normalized === "document") return "Word";
  if (normalized === "pdf") return "PDF";
  if (normalized === "image") return "Image";
  return "QA";
}

function qualityEvidenceStatusLabel(status: string) {
  if (status === "pass") return "通过";
  if (status === "fail") return "未通过";
  if (status === "warn") return "有警告";
  if (status === "pending") return "待复核";
  if (status === "skipped") return "已跳过";
  return "已记录";
}

function qualityEvidenceDetailNumber(detail: unknown, key: string) {
  const parts = String(detail || "").split(";");
  for (const part of parts) {
    const [rawKey, rawValue] = part.split("=");
    if (rawKey?.trim() !== key) continue;
    const value = Number.parseInt(String(rawValue || "").trim(), 10);
    return Number.isFinite(value) ? Math.max(0, value) : 0;
  }
  return 0;
}

function shouldSurfaceSkippedQualityCheck(check: NonNullable<QualityEvidence["checks"]>[number]) {
  const status = String(check.status || "").toLowerCase();
  if (status !== "skipped" || String(check.id || "") !== "reference-fidelity") return false;
  const referenceCount = qualityEvidenceDetailNumber(check.detail, "reference_count");
  const remoteReferences = qualityEvidenceDetailNumber(check.detail, "remote_references");
  return referenceCount > 0 || remoteReferences > 0;
}

function qualityEvidenceProblemChecks(evidence?: QualityEvidence) {
  const checks = Array.isArray(evidence?.checks) ? evidence?.checks || [] : [];
  return checks.filter((check) => {
    const status = String(check.status || "").toLowerCase();
    return status === "fail" || status === "warn" || status === "pending" || shouldSurfaceSkippedQualityCheck(check);
  }).slice(0, 5);
}

function QualityEvidenceBadge({ evidence, compact = false }: { evidence?: QualityEvidence; compact?: boolean }) {
  if (!evidence) return null;
  const problemChecks = qualityEvidenceProblemChecks(evidence);
  const baseStatus = qualityEvidenceStatus(evidence);
  const hasSkippedReference = problemChecks.some((check) => shouldSurfaceSkippedQualityCheck(check));
  const status = baseStatus === "pass" && hasSkippedReference ? "pending" : baseStatus;
  const missingCount = evidence.missingQualityGates?.length || 0;
  const gateCount = evidence.qualityGates?.length || 0;
  const label = `${qualityEvidenceKindLabel(String(evidence.kind || ""))} QA ${qualityEvidenceStatusLabel(status)}`;
  const detail = [
    gateCount ? `${gateCount} gates` : "",
    problemChecks.length ? `${problemChecks.length} issue checks` : "",
    missingCount ? `${missingCount} missing` : ""
  ].filter(Boolean).join(" · ");
  return (
    <span
      className={`quality-evidence-badge is-${status}${compact ? " is-compact" : ""}`}
      title={detail || label}
      data-qa-reference-skipped={hasSkippedReference ? REFERENCE_FIDELITY_SKIPPED_REVIEW_MARKER : undefined}
    >
      {status === "pass" ? <CircleCheck aria-hidden="true" /> : <TriangleAlert aria-hidden="true" />}
      <span>{label}</span>
      {!compact && detail ? <em>{detail}</em> : null}
    </span>
  );
}

function QualityEvidencePanel({ evidence }: { evidence?: QualityEvidence }) {
  if (!evidence) return null;
  const problemChecks = qualityEvidenceProblemChecks(evidence);
  return (
    <div className="quality-evidence-panel">
      <QualityEvidenceBadge evidence={evidence} />
      {problemChecks.length ? (
        <ul>
          {problemChecks.map((check, index) => (
            <li key={`${check.id || "check"}-${index}`}>
              <strong>{String(check.id || "unknown-check")}</strong>
              {check.detail ? <span>{redactInternalPromptText(check.detail)}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
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
  const liveContent = useMemo(
    () => normalizeMarkdownForRender(redactInternalPromptText(content || "")),
    [content]
  );
  if (!liveContent) return null;
  return (
    <div className="streaming-markdown">
      <StreamingStableMarkdown content={liveContent} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
    </div>
  );
}

type StepVisualKind = "thinking" | "search" | "tool" | "artifact" | "phase";

function stepVisualKindFromText(text?: string): StepVisualKind {
  const value = String(text || "").toLowerCase();
  if (/(search|web_search|web fetch|browser|browse|联网|搜索|检索|查找|浏览器|网页)/i.test(value)) return "search";
  if (/(image|artifact|media|file|生成产物|产物|图片|图像|保存|文件|导出)/i.test(value)) return "artifact";
  if (/(tool|bash|shell|mcp|skill|call|execute|工具|调用|执行|命令)/i.test(value)) return "tool";
  if (/(think|reason|推理|思考|分析)/i.test(value)) return "thinking";
  return "phase";
}

function toolStepVisualKind(step: ToolCallDisclosure): StepVisualKind {
  const name = String(step.name || "").toLowerCase();
  if (/(search|fetch|browser|chrome|web)/.test(name)) return "search";
  if (/(image|artifact|media|file|office|pdf|slides|sheet)/.test(name)) return "artifact";
  return "tool";
}

function StepIcon({ kind, running = false }: { kind: StepVisualKind; running?: boolean }) {
  const icon = kind === "thinking"
    ? <Brain aria-hidden="true" />
    : kind === "search"
      ? <Search aria-hidden="true" />
      : kind === "artifact"
        ? <Sparkles aria-hidden="true" />
        : kind === "tool"
          ? <Wrench aria-hidden="true" />
          : <CircleCheck aria-hidden="true" />;
  return (
    <span className={`agent-step-icon is-${kind}${running ? " is-running" : ""}`} aria-hidden="true">
      {icon}
    </span>
  );
}

function ThinkingStep({ content = "", running }: { content?: string; running?: boolean }) {
  const trimmed = redactInternalPromptText(content).trim();
  const shown = truncateReasoning(trimmed);
  return (
    <details className={`agent-step agent-thinking-step${running ? " is-running" : ""}`}>
      <summary title={running ? "EcoreX 正在推理，完成后可展开查看思考摘要" : "展开查看本轮思考摘要"}>
        <StepIcon kind="thinking" running={running} />
        <span>{running ? "思考中" : "思考完成"}</span>
      </summary>
      <div className="thinking-full">
        {shown.truncated ? <pre className="thinking-stream-pre">{shown.text}</pre> : <MarkdownBlock content={shown.text} />}
      </div>
    </details>
  );
}

function ToolStep({ step }: { step: ToolCallDisclosure }) {
  const [open, setOpen] = useState(false);
  const isInterrupted = toolStepIsInterrupted(step);
  const isError = !isInterrupted && (step.is_error === true || step.status === "error" || step.status === "failed" || step.status === "timeout");
  const running = step.running === true || step.status === "running";
  const extensionCount = typeof step.extension_count === "number" ? step.extension_count : 0;
  const qualityEvidence = step.qualityEvidence || qualityEvidenceFromUnknown(step.result);
  const visualKind = toolStepVisualKind(step);
  return (
    <details
      className={`agent-step agent-tool-step${isError ? " tool-failed" : ""}${isInterrupted ? " tool-cancelled" : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary title={running ? "工具正在执行，完成后可查看输入和结果" : isInterrupted ? "工具调用已中止，可展开查看已返回的信息" : "展开查看工具输入和结果"}>
        <StepIcon kind={visualKind} running={running} />
        <span className="tool-name">{step.name || "工具调用"}</span>
        {typeof step.execution_time === "number" && <span className="tool-time">{step.execution_time}s</span>}
        {running && extensionCount > 0 && <span className="tool-time">lease x{extensionCount}</span>}
      </summary>
      {open && (
        <div className="tool-detail">
          <div className="tool-detail-section">
            <div className="tool-detail-label">Input</div>
            <pre className="tool-detail-content">{formatToolValue(step.arguments)}</pre>
          </div>
          <QualityEvidencePanel evidence={qualityEvidence} />
          {step.result !== undefined && step.result !== "" && (
            <div className="tool-detail-section tool-output-section">
              <div className="tool-detail-label">{isError ? "Error" : "Output"}</div>
              <pre className={`tool-detail-content${isError ? " tool-error-text" : ""}`}>{formatToolValue(step.result)}</pre>
            </div>
          )}
        </div>
      )}
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

function PhaseStep({ content = "" }: { content?: string }) {
  const safeContent = redactInternalPromptText(content || "");
  return (
    <div className="agent-step agent-phase-step ecorex-activity-status">
      <StepIcon kind={stepVisualKindFromText(safeContent)} running />
      <span>{safeContent}</span>
    </div>
  );
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
    return <PhaseStep key={index} content={step.content} />;
  }
  return (
    <div className="agent-step agent-media-step" key={index}>
      <StepIcon kind="artifact" />
      <div className="agent-media-step-body">
        <MediaStep step={step} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} localFilePreviewUrl={localFilePreviewUrl} />
      </div>
    </div>
  );
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
  runTimingLabel,
  onOpenLocalFile,
  onLocalFileContextMenu,
  localFilePreviewUrl
}: {
  pending?: boolean;
  steps: AgentStepDisclosure[];
  legacySteps: ReactNode[];
  runTimingLabel?: string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
  localFilePreviewUrl?: (filePath: string) => string;
}) {
  const [open, setOpen] = useState(false);
  if (!steps.length && !legacySteps.length) return null;
  const runningStep = [...steps].reverse().find(stepIsRunning);
  const errorStep = [...steps].reverse().find(stepIsError);
  const interruptedStep = [...steps].reverse().find(stepIsInterrupted);
  const currentStep = runningStep || errorStep || interruptedStep || steps[steps.length - 1];
  const count = steps.length + legacySteps.length;
  const statusClass = pending ? " is-running" : errorStep ? " is-error" : interruptedStep ? " is-cancelled" : " is-done";
  return (
    <details
      className={`agent-process-disclosure${pending ? " is-live" : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className={`tool-status-dot${statusClass}`} aria-hidden="true" />
        <span>{pending ? `正在处理 · ${count} 步` : `调用过程 · ${count} 步`}</span>
        {runTimingLabel && <span className="agent-process-timing">{runTimingLabel}</span>}
        {currentStep && <span className="agent-process-current">{stepSummary(currentStep)}</span>}
      </summary>
      {open && (
        <div className="agent-steps">
          {steps.map((step, index) => renderStep(step, index, onOpenLocalFile, localFilePreviewUrl, onLocalFileContextMenu))}
          {legacySteps}
        </div>
      )}
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

function MainAnswer({ content, pending, collapsible, artifacts = [], extraArtifacts = [], localFilePreviewUrl, localFileJson, localFileStat, onOpenLocalFile, onLocalFileContextMenu, onArtifactFeedback, onImageRetouchRequest }: {
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
  onArtifactFeedback?: (artifact: AgentArtifact, validity: AgentArtifactValidity) => void | Promise<void>;
  onImageRetouchRequest?: (artifact: AgentArtifact, meta: { source: string; previewUrl: string; title: string }) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const legacyArtifacts = useMemo(
    () => {
      if (pending) return mergeArtifacts(extraArtifacts);
      return mergeArtifacts(
        extractArtifacts(content, { allowBareFiles: false }),
        imageArtifactsToLegacyArtifacts(extractImageArtifacts(content)),
        extraArtifacts
      );
    },
    [content, pending, extraArtifacts]
  );
  const artifactShelf = <ArtifactShelf artifacts={artifacts} legacyArtifacts={legacyArtifacts} localFilePreviewUrl={localFilePreviewUrl} localFileJson={localFileJson} localFileStat={localFileStat} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} onArtifactFeedback={onArtifactFeedback} onImageRetouchRequest={onImageRetouchRequest} />;
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
  const previewContent = content.length > LONG_REPLY_PREVIEW_CHARS
    ? `${content.slice(0, LONG_REPLY_PREVIEW_CHARS).trimEnd()}\n\n...`
    : content;
  return (
    <div className="long-answer-disclosure">
      <div className="long-answer-preview">
        <MarkdownBlock content={previewContent} localFilePreviewUrl={localFilePreviewUrl} onOpenLocalFile={onOpenLocalFile} onLocalFileContextMenu={onLocalFileContextMenu} />
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
  runTimingLabel?: string;
  onOpenLocalFile?: (file: LocalFilePayload) => void;
  onLocalFileContextMenu?: LocalFileContextHandler;
  localFilePreviewUrl?: (filePath: string) => string;
  localFileJson?: (filePath: string) => Promise<LocalJsonResult>;
  localFileStat?: (filePath: string) => Promise<LocalPathStat>;
  onArtifactFeedback?: (artifact: AgentArtifact, validity: AgentArtifactValidity) => void | Promise<void>;
  onImageRetouchRequest?: (artifact: AgentArtifact, meta: { source: string; previewUrl: string; title: string }) => void;
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
        runTimingLabel={props.runTimingLabel}
        onOpenLocalFile={props.onOpenLocalFile}
        onLocalFileContextMenu={props.onLocalFileContextMenu}
        localFilePreviewUrl={props.localFilePreviewUrl}
      />
      <MainAnswer content={mainContent} pending={props.pending} collapsible={props.role !== "user"} artifacts={props.artifacts} extraArtifacts={stepArtifacts} localFilePreviewUrl={props.localFilePreviewUrl} localFileJson={props.localFileJson} localFileStat={props.localFileStat} onOpenLocalFile={props.onOpenLocalFile} onLocalFileContextMenu={props.onLocalFileContextMenu} onArtifactFeedback={props.onArtifactFeedback} onImageRetouchRequest={props.onImageRetouchRequest} />
      {props.cancelled ? (
        <div className="agent-cancelled-tag">已中止</div>
      ) : props.pending ? (
        <span className="thinking-indicator"><span className="thinking-ring" aria-hidden="true" /><span>{mainContent ? "继续生成中" : "思考中"}</span></span>
      ) : null}
    </div>
  );
});
