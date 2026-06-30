import {
  Activity,
  AlertTriangle,
  ArrowDownToLine,
  AtSign,
  Bell,
  Bot,
  BookOpen,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Database,
  FileText,
  FolderInput,
  FolderPlus,
  FolderOpen,
  FolderX,
  Globe2,
  HardDrive,
  Image as ImageIcon,
  KeyRound,
  LogOut,
  Moon,
  MoreHorizontal,
  Paperclip,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Search,
  SendHorizontal,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  SquareTerminal,
  SunMedium,
  Trash2,
  Upload,
  UserRound,
  WandSparkles,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { FormEvent, KeyboardEvent, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, CSSProperties, DragEvent, MouseEvent, ReactNode } from "react";
import { MessageContent, type AgentStepDisclosure, type LocalFilePayload, type ToolCallDisclosure } from "./components/MessageContent";
import {
  cancelChatRequest,
  cancelSubagentTask,
  clearRuntimeContext,
  enterpriseChangePassword,
  checkEnterpriseQuota,
  chooseProjectFolder,
  chooseLocalFiles,
  decideToolPermission,
  enableDefaultSkills,
  enterpriseLogin,
  enterpriseLogout,
  exportDiagnosticsBundle,
  filePreviewUrl,
  generateSessionTitle,
  getEnterpriseSession,
  hasMessageStreamCursor,
  listCapabilityPacks,
  loadExternalConnections,
  loadSchedulerProjection,
  loadPermissionState,
  loadRuntimeProjection,
  loadMemoryFiles,
  loadRuntimeUiState,
  loadRuntimeSnapshot,
  loadSessionHistoryWithMeta,
  openLocalPath,
  openMessageStream,
  prepareRequestRetry,
  readLocalJson,
  reportDesktopEvent,
  requestAgentInstallRequest,
  resetPermissionGrants,
  deleteRuntimeSession,
  renameRuntimeSession,
  registerProjectFolderPath,
  savePastedFile,
  saveRuntimeUiState,
  sendChatMessage,
  setSkillEnabled,
  statLocalPath,
  updateScheduler,
  updateExternalConnection,
  updatePermissionMode,
  type CapabilityPack,
  type ChatSendResult,
  type AgentArtifact,
  type EnterpriseQuotaCheckResult,
  type EnterpriseSession,
  type ExternalConnection,
  type ExternalConnectionAction,
  type ExternalConnectionActionResponse,
  type ExternalConnectionField,
  type FileAttachment,
  type LocalJsonResult,
  type LocalPathStat,
  type MemoryFile,
  type PermissionMode,
  type PermissionState,
  type ProjectFolder,
  type ProjectSessionBinding,
  type RuntimeActiveRequest,
  type RuntimeSession,
  type RuntimeSessionLock,
  type RuntimeExtension,
  type RuntimeMessage,
  type RuntimeRequestProjection,
  type RuntimeSkill,
  type RuntimeStep,
  type RuntimeToolCall,
  type RuntimeTool,
  type RuntimeSnapshot,
  type RuntimeSchedulerProjection,
  type RuntimeSchedulerTask,
  type StreamItem,
  type TokenUsage,
  type UsageQuota,
  type QualityEvidence,
  type OpenPathAction
} from "./services/ecorexApi";
import { CHAT_SCROLL_THRESHOLD_PX, getChatScrollState, scrollElementToBottom } from "./utils/chatUx";
import { redactInternalPromptText, redactToolDisclosureValue } from "./utils/redaction";
import { projectionRecoveryDecision, type ProjectionTerminalPhase } from "./utils/runtimeProjectionRecovery";

type ThemeMode = "light" | "dark";
type SidecarStatus = {
  state: "starting" | "running" | "stopped" | "failed" | "skipped";
  phase?: "idle" | "spawning" | "probing" | "ready" | "degraded" | "restarting" | "failed" | "stopped" | "skipped";
  message: string;
  pid?: number;
  webPort: number;
  diagnostics?: {
    bootId: string;
    restartAttempts: number;
    consecutiveHealthFailures: number;
    startupInFlight: boolean;
    lastProbeOkAt?: string;
    lastProbeErrorAt?: string;
    recentEvents?: Array<{ ts: string; state: string; phase: string; message: string; reason?: string }>;
  };
};
type StreamRequestPhase =
  | "connecting"
  | "streaming"
  | "stalled"
  | "flushing"
  | "text_done_tail_open"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";
type TerminalStreamRequestPhase = ProjectionTerminalPhase;
type StreamRequestState = {
  sessionId: string;
  requestId: string;
  phase: StreamRequestPhase;
  updatedAt: number;
  terminalAt?: number;
  lastEventAt?: number;
};
type SessionRow = {
  id: string;
  title: string;
  detail: string;
  activityAt?: string | number;
  createdAt?: string | number;
  sortKeyMs?: number;
  updatedAt: string | number;
  status: "active" | "waiting" | "cancelling" | "ready" | "failed";
  requestId?: string;
  streamAvailable?: boolean;
  cancelling?: boolean;
  pinned?: boolean;
  pinnedAt?: number;
  projectId?: string;
  projectName?: string;
};
type ChatItem = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  attachments?: FileAttachment[];
  pending?: boolean;
  paused?: boolean;
  visibleOutputSettled?: boolean;
  reasoning?: string;
  steps?: AgentStepDisclosure[];
  toolCalls?: ToolCallDisclosure[];
  artifacts?: AgentArtifact[];
  cancelled?: boolean;
  requestId?: string;
  runTiming?: ChatRunTiming;
  phaseStartedAt?: number;
  userSeq?: number;
  botSeq?: number;
  contextExcluded?: boolean;
  sendAttempt?: {
    id: string;
    state: "stopping-previous" | "sending" | "accepted" | "restore-available";
    interruptsRequestId?: string;
  };
  recovery?: {
    kind: "stalled" | "failed" | "interrupted" | "replay_gap" | "retryable_conflict" | "reconnecting";
    requestId?: string;
    message: string;
    retryable?: boolean;
    recoverable?: boolean;
    reason?: string;
    retryAfterMs?: number;
    retryMode?: string;
    stopAllowed?: boolean;
  };
};
type ChatRunTiming = {
  requestId?: string;
  state?: "sending" | "running" | "completed" | "failed" | "cancelled" | "interrupted" | string;
  startedAtMs?: number;
  updatedAtMs?: number;
  terminalAtMs?: number;
};
type ApprovalState =
  | {
      type: "capability";
      title: string;
      message: string;
      pack: CapabilityPack;
      resume: () => void;
    }
  | {
      type: "open-file";
      title: string;
      message: string;
      file: FileAttachment;
    }
  | {
      type: "quota" | "permission" | "info" | "error";
      title: string;
      message: string;
      actions?: Array<{ label: string; primary?: boolean; onClick: () => void }>;
    };
type SettingsSection = "account" | "projects" | "abilities" | "external-connections" | "scheduler" | "permissions" | "memory" | "diagnostics";
type SessionProjectMap = Record<string, string>;
type SessionProjectBindingMap = Record<string, ProjectSessionBinding>;
type StringBoolMap = Record<string, boolean>;
type StringNumberMap = Record<string, number>;
type StringMap = Record<string, string>;
type SessionUiState = {
  title: string;
  projectId: string | null;
  projectBinding?: ProjectSessionBinding | null;
  messages: ChatItem[];
  composerText: string;
  attachments: FileAttachment[];
  contextStartSeq?: number;
  lastActivityAt?: string | number;
};
type ProjectContextMenu = {
  projectId: string;
  x: number;
  y: number;
} | null;
type ChatFileContextMenu = {
  file: FileAttachment;
  x: number;
  y: number;
  canAdd: boolean;
  disabledReason?: string;
} | null;
type SidebarCollapseState = {
  projectsSection: boolean;
  generalSessions: boolean;
  projectGroups: StringBoolMap;
};
type InstallNotice = {
  packId: string;
  packName: string;
  message: string;
  dismissed?: boolean;
} | null;

const brandIconUrl = new URL("../build/icon.png", import.meta.url).href;
document.documentElement.dataset.platform = window.ecorexDesktop?.platform || "web";

function isRuntimePreviewPath(value?: string) {
  const source = String(value || "").trim();
  return /^https?:\/\//i.test(source) || /^(?:\/(?:uploads|static|app)(?:\/|$)|\/api\/file(?:[/?#]|$))/.test(source);
}

function nativePathFromFileUrl(value?: string) {
  const source = String(value || "").trim();
  if (!/^file:\/\//i.test(source)) return "";
  try {
    const parsed = new URL(source);
    const platform = window.ecorexDesktop?.platform || "";
    const hostname = parsed.hostname;
    if (hostname && hostname.toLowerCase() !== "localhost") {
      const decodedPath = decodeURIComponent(parsed.pathname || "");
      return platform === "win32" ? `\\\\${hostname}${decodedPath.replace(/\//g, "\\")}` : `//${hostname}${decodedPath}`;
    }
    const decoded = decodeURIComponent(parsed.pathname || "");
    if (platform === "win32") {
      return decoded.replace(/^\/([a-zA-Z]:[\\/])/, "$1").replace(/\//g, "\\");
    }
    return decoded;
  } catch {
    return source.replace(/^file:\/\/+/i, "").replace(/^\/([a-zA-Z]:[\\/])/, "$1");
  }
}

function normalizeLocalSource(value?: string) {
  const source = String(value || "").trim();
  return nativePathFromFileUrl(source) || source;
}

function isLocalAbsolutePath(value?: string) {
  const source = normalizeLocalSource(value);
  const platform = window.ecorexDesktop?.platform || "";
  return /^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("\\\\") || (platform !== "win32" && /^\//.test(source) && !isRuntimePreviewPath(source));
}

function isOpenPathNotFoundMessage(value?: string) {
  return /path not found|not found|找不到|不存在/i.test(String(value || ""));
}

function isOpenPathDeniedMessage(value?: string) {
  return /denied|blocked|forbidden|permission|refusing to launch|not allowed|拒绝|阻止|权限|危险/i.test(String(value || ""));
}

function isOpenPathBridgeFailure(value?: string) {
  return /desktop bridge is not available|local runtime is unavailable|failed to fetch|networkerror|econnrefused|sidecar|runtime/i.test(String(value || ""));
}

function joinLocalPath(base: string, child: string) {
  const root = base.trim();
  const rel = child.trim().replace(/^[\\/]+/g, "");
  if (!root || !rel) return child;
  const slash = root.includes("\\") && !root.includes("/") ? "\\" : "/";
  return root.replace(/[\\/]+$/g, "") + slash + rel;
}

function fixedMenuStyle(x: number, y: number, width = 220, height = 140): CSSProperties {
  const margin = 8;
  const viewportWidth = typeof window === "undefined" ? 1024 : window.innerWidth;
  const viewportHeight = typeof window === "undefined" ? 768 : window.innerHeight;
  return {
    left: Math.max(margin, Math.min(x, viewportWidth - width - margin)),
    top: Math.max(margin, Math.min(y, viewportHeight - height - margin)),
    maxWidth: `calc(100vw - ${margin * 2}px)`,
    maxHeight: `calc(100vh - ${margin * 2}px)`,
    overflowY: "auto"
  };
}

function isImageAttachment(file: FileAttachment) {
  return file.file_type === "image" || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.file_name || file.file_path || "");
}

function normalizeAttachmentDedupeKey(file: FileAttachment) {
  const raw = normalizeLocalSource(file.file_path || file.preview_url || file.file_name || "");
  const compact = raw.replace(/[\\/]+$/g, "").replace(/\\/g, "/");
  if (/^[a-zA-Z]:\//.test(compact) || compact.startsWith("//")) return compact.toLowerCase();
  return compact;
}

function isDurableLocalAttachment(file: FileAttachment) {
  const path = normalizeLocalSource(file.file_path || "");
  if (!path || /^data:/i.test(path) || /^https?:\/\//i.test(path) || isRuntimePreviewPath(path)) return false;
  return true;
}

const initialRuntime: RuntimeSnapshot = {
  status: "offline",
  message: "正在连接本地运行时",
  sessions: [],
  totalSessions: 0,
  toolsCount: 0,
  skillsCount: 0,
  extensionsCount: 0,
  extensionSummary: {},
  modelsCount: 0
};
const initialSidecar: SidecarStatus = {
  state: "starting",
  message: "正在启动本地运行时",
  webPort: 9899
};
const PROJECTS_STORAGE_KEY = "ecorex-projects";
const SESSION_PROJECTS_STORAGE_KEY = "ecorex-session-projects";
const SESSION_PROJECT_BINDINGS_STORAGE_KEY = "ecorex-session-project-bindings";
const SESSION_TITLES_STORAGE_KEY = "ecorex-session-titles";
const LOCKED_SESSION_TITLES_STORAGE_KEY = "ecorex-locked-session-titles";
const PINNED_SESSIONS_STORAGE_KEY = "ecorex-pinned-sessions";
const PINNED_SESSION_TIMES_STORAGE_KEY = "ecorex-pinned-session-times";
const PINNED_PROJECTS_STORAGE_KEY = "ecorex-pinned-projects";
const UNREAD_SESSIONS_STORAGE_KEY = "ecorex-unread-sessions";
const SESSION_UI_STORAGE_KEY = "ecorex-session-ui-state";
const LAST_ACTIVE_SESSION_STORAGE_KEY = "ecorex-last-active-session-id";
const CAPABILITY_ENABLED_STORAGE_KEY = "ecorex-capability-enabled";
const SKILL_DEFAULTS_STORAGE_KEY = "ecorex-skill-defaults-v1";
const RELEASE_NOTES_SEEN_STORAGE_KEY = "ecorex-release-notes-seen-version";
const SIDEBAR_COLLAPSE_STORAGE_KEY = "ecorex-sidebar-collapse-state-v1";
const RUN_CENTER_DEV_GATE_STORAGE_KEY = "ecorex-dev-run-center";
const NEW_SESSION_START_TITLE = "和EcoreX一起开始工作";
const SESSION_UI_RETAINED_SESSIONS = 10;
const SESSION_UI_MESSAGE_LIMIT = 10;
const SESSION_UI_FALLBACK_MESSAGE_LIMIT = 4;
const SESSION_UI_SESSION_SOFT_BYTES = 36_000;
const SESSION_UI_TOTAL_SOFT_BYTES = 520_000;
const SESSION_UI_CONTENT_CHARS = 2400;
const SESSION_UI_STEP_CHARS = 600;
const SESSION_UI_TOOL_CHARS = 800;
const SESSION_UI_REASONING_CHARS = 900;
const SESSION_UI_PREVIEW_DATA_URL_CHARS = 500;
const CONTEXT_THRESHOLD_TOKENS = 258_000;
const EFFECTIVE_MODEL_FALLBACK = "gpt-5.5";
const EFFECTIVE_MODEL_ALIAS_PREFIXES = ["deepseek-"];
const COMPOSER_PERMISSION_MENU_MODES: PermissionMode[] = ["smart-ask", "full-access"];
const SETTINGS_PERMISSION_MODES: PermissionMode[] = ["smart-ask", "full-access"];

const coreAbilityNames = new Set([
  "bash",
  "read",
  "write",
  "edit",
  "ls",
  "find",
  "vision",
  "web_search",
  "web_fetch",
  "browser",
  "ecorex_cli",
  "memory_search",
  "memory_get"
]);

const skillAbilityNames = new Set(["find", "image-generation", "knowledge-wiki", "skill-creator"]);

function readStorage<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeStorage<T>(key: string, value: T) {
  window.localStorage.setItem(key, JSON.stringify(value));
}

function normalizeStringNumberMap(value: unknown): StringNumberMap {
  if (!value || typeof value !== "object") return {};
  const result: StringNumberMap = {};
  Object.entries(value as Record<string, unknown>).forEach(([key, raw]) => {
    const normalizedKey = String(key || "").trim();
    const normalizedValue = Number(raw);
    if (!normalizedKey || !Number.isFinite(normalizedValue) || normalizedValue <= 0) return;
    result[normalizedKey] = normalizedValue;
  });
  return result;
}

function truncatePersistedText(value: unknown, limit: number) {
  const text = String(value ?? "");
  if (!text || text.length <= limit) return text;
  return `${text.slice(0, limit)}\n...[truncated ${text.length - limit} chars]`;
}

function compactPersistedUnknown(value: unknown, limit: number) {
  if (value === undefined || value === null || value === "") return undefined;
  const redacted = redactToolDisclosureValue(value);
  if (typeof redacted === "string") return truncatePersistedText(redacted, limit);
  try {
    return truncatePersistedText(JSON.stringify(redacted), limit);
  } catch {
    return truncatePersistedText(String(redacted), limit);
  }
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
const QUALITY_EVIDENCE_KINDS = new Set(["presentation", "spreadsheet", "document", "pdf", "image"]);
const QUALITY_EVIDENCE_AUTHORING_ROUTES = new Set(["artifact-tool", "template-following", "verified-existing-deck", "unspecified"]);

function normalizeQualityText(value: unknown, limit: number) {
  const text = String(value ?? "").trim().replace(/\s+/g, " ");
  if (!text) return "";
  return text.slice(0, limit);
}

function qualityEvidenceHash(value: string) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function normalizeQualityRef(value: unknown) {
  const text = String(value ?? "").trim().replace(/\s+/g, " ");
  if (!text) return "";
  const digest = text.toLowerCase().startsWith("hmac:") ? text.slice(5) : "";
  if (digest.length >= 8 && digest.length <= 128 && /^[0-9a-fA-F]+$/.test(digest)) return text;
  return `quality-ref-${qualityEvidenceHash(text)}`;
}

function normalizeQualityGate(value: unknown) {
  const gate = normalizeQualityText(value, 72).toLowerCase();
  return QUALITY_EVIDENCE_ALLOWED_GATES.has(gate) ? gate : "";
}

function normalizeQualityStatus(value: unknown) {
  const status = normalizeQualityText(value, 24).toLowerCase();
  return QUALITY_EVIDENCE_STATUSES.has(status) ? status : "unknown";
}

function normalizeQualityKind(value: unknown) {
  const kind = normalizeQualityText(value, 32).toLowerCase();
  return QUALITY_EVIDENCE_KINDS.has(kind) ? kind : "";
}

function sanitizeQualityDetail(value: unknown) {
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

function sanitizeQualityCheck(value: unknown) {
  if (!value || Array.isArray(value) || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  const id = normalizeQualityGate(record.id || record.gate) || "unknown-check";
  const detail = sanitizeQualityDetail(record.detail || record.summary || "");
  return {
    id,
    status: normalizeQualityStatus(record.status),
    ...(detail ? { detail } : {})
  };
}

function normalizeQualityEvidence(value: unknown): QualityEvidence | undefined {
  if (!value) return undefined;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed.startsWith("{") || trimmed.length > 64 * 1024) return undefined;
    try {
      return normalizeQualityEvidence(JSON.parse(trimmed) as unknown);
    } catch {
      return undefined;
    }
  }
  if (Array.isArray(value) || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  const direct = record.qualityEvidence || record.quality_evidence;
  if (direct && typeof direct === "object" && !Array.isArray(direct)) {
    return normalizeQualityEvidence(direct);
  }
  if (!Array.isArray(record.qualityGates) && !Array.isArray(record.checks)) return undefined;
  const checks = Array.isArray(record.checks)
    ? record.checks.map(sanitizeQualityCheck).filter((item): item is NonNullable<typeof item> => !!item).slice(0, 48)
    : [];
  const qualityGates = Array.isArray(record.qualityGates)
    ? record.qualityGates.map(normalizeQualityGate).filter(Boolean).slice(0, 40)
    : [];
  const missingQualityGates = Array.isArray(record.missingQualityGates)
    ? record.missingQualityGates.map(normalizeQualityGate).filter(Boolean).slice(0, 40)
    : [];
  const kind = normalizeQualityKind(record.kind);
  const status = normalizeQualityStatus(record.status);
  const sourceRef = normalizeQualityRef(record.sourceRef);
  const authoringRoute = normalizeQualityText(record.authoringRoute, 64).toLowerCase();
  const evidence: QualityEvidence = {
    ...(record.schemaVersion ? { schemaVersion: normalizeQualityText(record.schemaVersion, 24) } : {}),
    ...(kind ? { kind } : {}),
    ...(sourceRef ? { sourceRef } : {}),
    ...(qualityGates.length ? { qualityGates } : {}),
    ...(checks.length ? { checks } : {}),
    ...(missingQualityGates.length ? { missingQualityGates } : {}),
    status,
    redacted: true,
    qualityEvidenceSanitized: true
  };
  if (QUALITY_EVIDENCE_AUTHORING_ROUTES.has(authoringRoute)) {
    evidence.authoringRoute = authoringRoute;
  }
  return evidence;
}

function compactPersistedQualityEvidence(value: unknown): QualityEvidence | undefined {
  return normalizeQualityEvidence(value);
}

function slimPersistedAttachment(file: FileAttachment): FileAttachment {
  const previewDataUrl = file.previewDataUrl && file.previewDataUrl.length <= SESSION_UI_PREVIEW_DATA_URL_CHARS
    ? file.previewDataUrl
    : undefined;
  return {
    file_path: file.file_path,
    file_name: file.file_name,
    file_type: file.file_type,
    preview_url: file.preview_url,
    ...(previewDataUrl ? { previewDataUrl } : {})
  };
}

function slimPersistedArtifact(artifact: AgentArtifact): AgentArtifact {
  const previewUrl = artifact.previewUrl && artifact.previewUrl.length <= 2048 ? artifact.previewUrl : undefined;
  const thumbnailUrl = artifact.thumbnailUrl && artifact.thumbnailUrl.length <= 2048 ? artifact.thumbnailUrl : undefined;
  return {
    id: artifact.id,
    requestId: artifact.requestId,
    kind: artifact.kind,
    intent: artifact.intent,
    operation: artifact.operation,
    status: artifact.status,
    title: truncatePersistedText(artifact.title, 280),
    path: artifact.path,
    relativePath: artifact.relativePath,
    url: artifact.url,
    mimeType: artifact.mimeType,
    sizeBytes: artifact.sizeBytes,
    statusPath: artifact.statusPath,
    previewUrl,
    thumbnailUrl,
    qualityEvidence: compactPersistedQualityEvidence(artifact.qualityEvidence),
    stats: artifact.stats,
    source: artifact.source ? {
      toolCallId: artifact.source.toolCallId,
      toolName: artifact.source.toolName,
      activityId: artifact.source.activityId,
      createdAt: artifact.source.createdAt
    } : undefined
  };
}

function slimPersistedStep(step: AgentStepDisclosure): AgentStepDisclosure {
  if (step.type === "thinking") {
    return {
      type: "thinking",
      content: truncatePersistedText(step.content, SESSION_UI_STEP_CHARS),
      running: step.running,
      startedAt: step.startedAt,
      duration: step.duration
    };
  }
  if (step.type === "content") {
    return {
      type: "content",
      content: truncatePersistedText(step.content, SESSION_UI_STEP_CHARS),
      intermediate: step.intermediate
    };
  }
  if (step.type === "phase") {
    return { type: "phase", content: truncatePersistedText(step.content, SESSION_UI_STEP_CHARS) };
  }
  if (step.type === "tool") {
    return {
      type: "tool",
      id: step.id,
      name: step.name,
      status: step.status,
      execution_time: step.execution_time,
      deadline_seconds: step.deadline_seconds,
      max_seconds: step.max_seconds,
      extension_count: step.extension_count,
      lastHeartbeatAt: step.lastHeartbeatAt,
      is_error: step.is_error,
      running: step.running,
      arguments: compactPersistedUnknown(step.arguments, SESSION_UI_TOOL_CHARS),
      result: compactPersistedUnknown(step.result, SESSION_UI_TOOL_CHARS),
      qualityEvidence: compactPersistedQualityEvidence(step.qualityEvidence || normalizeQualityEvidence(step.result))
    };
  }
  return {
    type: "media",
    fileType: step.fileType,
    url: step.url,
    filePath: step.filePath,
    previewUrl: step.previewUrl && step.previewUrl.length <= 2048 ? step.previewUrl : undefined,
    fileName: step.fileName
  };
}

function slimPersistedToolCall(tool: ToolCallDisclosure): ToolCallDisclosure {
  return {
    name: tool.name,
    status: tool.status,
    is_error: tool.is_error,
    execution_time: tool.execution_time,
    deadline_seconds: tool.deadline_seconds,
    max_seconds: tool.max_seconds,
    extension_count: tool.extension_count,
    lastHeartbeatAt: tool.lastHeartbeatAt,
    running: tool.running,
    arguments: compactPersistedUnknown(tool.arguments, SESSION_UI_TOOL_CHARS),
    result: compactPersistedUnknown(tool.result, SESSION_UI_TOOL_CHARS),
    qualityEvidence: compactPersistedQualityEvidence(tool.qualityEvidence || normalizeQualityEvidence(tool.result))
  };
}

function slimPersistedMessage(message: ChatItem): ChatItem {
  return {
    id: message.id,
    role: message.role,
    content: truncatePersistedText(message.content, SESSION_UI_CONTENT_CHARS),
    createdAt: message.createdAt,
    attachments: message.attachments?.slice(0, 8).map(slimPersistedAttachment),
    pending: message.pending,
    paused: message.paused,
    visibleOutputSettled: message.visibleOutputSettled,
    reasoning: message.reasoning ? truncatePersistedText(message.reasoning, SESSION_UI_REASONING_CHARS) : undefined,
    steps: message.steps?.slice(-10).map(slimPersistedStep),
    toolCalls: message.toolCalls?.slice(-6).map(slimPersistedToolCall),
    artifacts: message.artifacts?.slice(-12).map(slimPersistedArtifact),
    cancelled: message.cancelled,
    requestId: message.requestId,
    runTiming: message.runTiming,
    phaseStartedAt: message.phaseStartedAt,
    userSeq: message.userSeq,
    botSeq: message.botSeq,
    contextExcluded: message.contextExcluded,
    sendAttempt: message.sendAttempt,
    recovery: message.recovery
  };
}

function compactPersistedMessages(messages: ChatItem[]) {
  const retained = new Map<string, ChatItem>();
  for (const message of messages.slice(-SESSION_UI_MESSAGE_LIMIT)) retained.set(message.id, message);
  for (const message of messages.filter((item) => hasLivePersistedMessages([item]))) retained.set(message.id, message);
  return [...retained.values()].map(slimPersistedMessage);
}

function slimPersistedSessionState(value: SessionUiState): SessionUiState {
  const base: SessionUiState = {
    ...value,
    title: truncatePersistedText(value.title, 160),
    messages: compactPersistedMessages(value.messages || []),
    composerText: truncatePersistedText(value.composerText, SESSION_UI_CONTENT_CHARS),
    attachments: (value.attachments || []).slice(0, 12).map(slimPersistedAttachment)
  };
  try {
    if (JSON.stringify(base).length > SESSION_UI_SESSION_SOFT_BYTES) {
      return {
        ...base,
        messages: (base.messages || []).slice(-SESSION_UI_FALLBACK_MESSAGE_LIMIT).map((message) => ({
          ...message,
          content: truncatePersistedText(message.content, Math.floor(SESSION_UI_CONTENT_CHARS / 2)),
          reasoning: message.reasoning ? truncatePersistedText(message.reasoning, 600) : undefined,
          steps: message.steps?.slice(-4),
          toolCalls: undefined
        }))
      };
    }
  } catch {
    return { ...base, messages: [] };
  }
  return base;
}

function minimalPersistedSessionState(value: SessionUiState): SessionUiState {
  return {
    title: truncatePersistedText(value.title, 120),
    projectId: value.projectId,
    projectBinding: value.projectBinding || null,
    composerText: truncatePersistedText(value.composerText, 1000),
    attachments: (value.attachments || []).slice(0, 4).map(slimPersistedAttachment),
    contextStartSeq: value.contextStartSeq,
    lastActivityAt: value.lastActivityAt,
    messages: (value.messages || []).slice(-2).map((message) => ({
      id: message.id,
      role: message.role,
      content: truncatePersistedText(message.content, 900),
      createdAt: message.createdAt,
      pending: message.pending,
      paused: message.paused,
      visibleOutputSettled: message.visibleOutputSettled,
      cancelled: message.cancelled,
      requestId: message.requestId,
      userSeq: message.userSeq,
      botSeq: message.botSeq,
      contextExcluded: message.contextExcluded,
      sendAttempt: message.sendAttempt,
      recovery: message.recovery
    }))
  };
}

function serializedStateSize(value: unknown) {
  try {
    return JSON.stringify(value).length;
  } catch {
    return Number.MAX_SAFE_INTEGER;
  }
}

function initialSidebarCollapseState(): SidebarCollapseState {
  const saved = readStorage<Partial<SidebarCollapseState>>(SIDEBAR_COLLAPSE_STORAGE_KEY, {});
  return {
    projectsSection: Boolean(saved.projectsSection),
    generalSessions: Boolean(saved.generalSessions),
    projectGroups: saved.projectGroups && typeof saved.projectGroups === "object" ? saved.projectGroups : {}
  };
}

function pruneSessionUiState(state: Record<string, SessionUiState>) {
  const entries = Object.entries(state);
  const liveEntries = entries.filter(([, value]) => hasLivePersistedMessages(value.messages || []));
  const retained = new Map<string, SessionUiState>();
  for (const [sessionId, value] of entries.slice(-SESSION_UI_RETAINED_SESSIONS)) retained.set(sessionId, value);
  for (const [sessionId, value] of liveEntries) retained.set(sessionId, value);
  const next = Object.fromEntries(
    [...retained.entries()].map(([sessionId, value]) => [
      sessionId,
      slimPersistedSessionState(value)
    ])
  );
  while (serializedStateSize(next) > SESSION_UI_TOTAL_SOFT_BYTES && Object.keys(next).length > 1) {
    const candidates = Object.entries(next)
      .filter(([, value]) => !hasLivePersistedMessages(value.messages || []))
      .sort(([, left], [, right]) => (timeMs(left.lastActivityAt) || latestMessageMs(left.messages || [])) - (timeMs(right.lastActivityAt) || latestMessageMs(right.messages || [])));
    const removeId = candidates[0]?.[0] || Object.keys(next)[0];
    delete next[removeId];
  }
  if (serializedStateSize(next) > SESSION_UI_TOTAL_SOFT_BYTES) {
    for (const [sessionId, value] of Object.entries(next)) {
      if (!hasLivePersistedMessages(value.messages || [])) {
        next[sessionId] = minimalPersistedSessionState(value);
      }
    }
  }
  if (serializedStateSize(next) > SESSION_UI_TOTAL_SOFT_BYTES) {
    for (const [sessionId, value] of Object.entries(next)) {
      next[sessionId] = minimalPersistedSessionState(value);
    }
  }
  return next;
}

function hasLivePersistedMessages(messages: ChatItem[]) {
  return messages.some((message) => (
    message.role === "assistant"
    && message.pending
    && Boolean(message.requestId)
    && !message.paused
    && !message.cancelled
  ));
}

function hasLiveSessionUiState(state: Record<string, SessionUiState>) {
  return Object.values(state).some((value) => hasLivePersistedMessages(value.messages || []));
}

function sameAttachments(left: FileAttachment[] = [], right: FileAttachment[] = []) {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  return left.every((item, index) => {
    const other = right[index];
    return Boolean(other)
      && item.file_path === other.file_path
      && item.file_name === other.file_name
      && item.file_type === other.file_type
      && item.preview_url === other.preview_url
      && item.previewDataUrl === other.previewDataUrl;
  });
}

function pickBootSession(state: Record<string, SessionUiState>) {
  const entries = Object.entries(state);
  const savedId = window.localStorage.getItem(LAST_ACTIVE_SESSION_STORAGE_KEY) || "";
  if (savedId && state[savedId]) {
    return { id: savedId, state: state[savedId] };
  }
  const liveEntry = [...entries].reverse().find(([, value]) => hasLivePersistedMessages(value.messages || []));
  if (liveEntry) return { id: liveEntry[0], state: liveEntry[1] };
  const latestEntry = entries[entries.length - 1];
  return latestEntry ? { id: latestEntry[0], state: latestEntry[1] } : null;
}

function initialTheme(): ThemeMode {
  const saved = window.localStorage.getItem("ecorex-theme");
  if (saved === "dark" || saved === "light") return saved;
  return "dark";
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.dataset.theme = theme;
  void window.ecorexDesktop?.setWindowTheme?.(theme).catch(() => undefined);
}

function displayModelName(value?: string) {
  const model = (value || "").trim();
  if (!model || /^ecorex$/i.test(model) || /^openai$/i.test(model)) return EFFECTIVE_MODEL_FALLBACK;
  const normalized = model.toLowerCase();
  if (EFFECTIVE_MODEL_ALIAS_PREFIXES.some((prefix) => normalized.startsWith(prefix))) return EFFECTIVE_MODEL_FALLBACK;
  return model;
}

function isRuntimeRequestUiActive(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  if (!request.cancelled) return true;
  const ageSeconds = Number(request.cancel_age_seconds ?? request.age_seconds ?? 0);
  return !Number.isFinite(ageSeconds) || ageSeconds < 30;
}

function isSubagentRuntimeRequest(request?: RuntimeActiveRequest | null) {
  const requestId = String(request?.request_id || "");
  const sessionId = String(request?.session_id || "");
  return (
    request?.run_type === "subagent"
    || requestId.startsWith("subagent-")
    || sessionId.startsWith("subagent-")
  );
}

function isSchedulerRuntimeRequest(request?: RuntimeActiveRequest | null) {
  const requestId = String(request?.request_id || "");
  const sessionId = String(request?.session_id || "");
  return (
    request?.run_type === "scheduler"
    || requestId.startsWith("scheduler_")
    || sessionId.startsWith("scheduler_")
  );
}

function isPrimaryChatActiveRequest(request?: RuntimeActiveRequest | null) {
  return (
    !isSubagentRuntimeRequest(request)
    && !isSchedulerRuntimeRequest(request)
    && isRuntimeRequestUiActive(request)
  );
}

function isAbnormalTerminalRequest(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  const raw = String(request.state || request.status || request.phase || "").toLowerCase();
  const terminalReason = String(request.terminal_reason || "").trim().toLowerCase();
  const completed = /(complete|success|done|finish)/.test(raw) || /(complete|success|done|finish)/.test(terminalReason);
  if (completed && !request.cancelled) return false;
  if (request.cancelled || request.error_message || request.error_code) return true;
  if (terminalReason && !/(complete|success|done|finish)/.test(terminalReason)) return true;
  return (
    raw.includes("cancel")
    || raw.includes("fail")
    || raw.includes("error")
    || raw.includes("interrupt")
    || raw.includes("stale")
    || raw.includes("dead")
    || raw.includes("lock")
  );
}

function isPrimaryChatTerminalRequest(request?: RuntimeActiveRequest | null) {
  return Boolean(
    request?.request_id
    && !isSubagentRuntimeRequest(request)
    && !isSchedulerRuntimeRequest(request)
    && isAbnormalTerminalRequest(request)
  );
}

function runCenterState(request?: RuntimeActiveRequest | null) {
  const raw = String(request?.state || request?.status || request?.phase || "").toLowerCase();
  if (raw === "cancelled" && request?.terminal_at != null) return "cancelled";
  if (request?.cancelled || raw.includes("cancell")) return "cancelling";
  if (raw.includes("complete") || raw === "done") return "completed";
  if (raw.includes("fail") || raw.includes("error") || raw.includes("interrupt")) return "failed";
  if (raw.includes("queue") || raw.includes("pending")) return "queued";
  if (raw.includes("final")) return "finalizing";
  return raw || "running";
}

function runCenterStateLabel(request?: RuntimeActiveRequest | null) {
  const state = runCenterState(request);
  if (state === "cancelling") return "Stopping";
  if (state === "failed") return "Failed";
  if (state === "cancelled") return "Stopped";
  if (state === "queued") return "Queued";
  if (state === "finalizing") return "Finalizing";
  return "Running";
}

function runCenterStateClass(request?: RuntimeActiveRequest | null) {
  const state = runCenterState(request);
  if (state === "cancelling") return "is-cancelling";
  if (state === "cancelled") return "is-cancelling";
  if (state === "failed") return "is-failed";
  if (state === "queued") return "is-queued";
  if (state === "finalizing") return "is-finalizing";
  return "is-running";
}

function isRunCenterFailedRequest(request?: RuntimeActiveRequest | null) {
  return runCenterState(request) === "failed";
}

function isRunCenterVisibleRequest(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  return runCenterState(request) !== "completed";
}

function isRunCenterSubagentRequest(request?: RuntimeActiveRequest | null) {
  return isSubagentRuntimeRequest(request);
}

function isRunCenterSchedulerRequest(request?: RuntimeActiveRequest | null) {
  return isSchedulerRuntimeRequest(request);
}

function getRunCenterSubagentTaskId(request?: RuntimeActiveRequest | null) {
  const metadataTaskId = request?.metadata?.task_id;
  if (typeof metadataTaskId === "string" && metadataTaskId.trim()) {
    return metadataTaskId.trim();
  }
  for (const value of [request?.request_id, request?.session_id]) {
    const text = String(value || "");
    if (text.startsWith("subagent-") && text.length > "subagent-".length) {
      return text.slice("subagent-".length);
    }
  }
  return "";
}

function shortRequestId(value?: string) {
  const text = String(value || "").trim();
  if (!text) return "unknown";
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text;
}

function formatRunAge(seconds?: number | null) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m ${rest}s`;
}

function epochMs(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return 0;
  if (typeof value === "number") {
    return value < 10_000_000_000 ? value * 1000 : value;
  }
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function runtimeRequestElapsedSeconds(request?: RuntimeActiveRequest | null, nowMs = Date.now()) {
  if (!request) return null;
  const startedAtMs = epochMs(request.created_at);
  const terminalAtMs = epochMs(request.terminal_at);
  if (startedAtMs && terminalAtMs) return Math.max(0, (terminalAtMs - startedAtMs) / 1000);
  if (startedAtMs) return Math.max(0, (nowMs - startedAtMs) / 1000);
  if (typeof request.age_seconds === "number" && Number.isFinite(request.age_seconds)) return request.age_seconds;
  return null;
}

function runtimeRequestElapsedLabel(request?: RuntimeActiveRequest | null, nowMs = Date.now()) {
  return formatRunAge(runtimeRequestElapsedSeconds(request, nowMs));
}

function chatRunTimingElapsedSeconds(timing?: ChatRunTiming, nowMs = Date.now()) {
  if (!timing?.startedAtMs) return null;
  const endMs = timing.terminalAtMs || nowMs;
  return Math.max(0, (endMs - timing.startedAtMs) / 1000);
}

function createDraftSessionId(project?: ProjectFolder | null) {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  return project ? `ecorex-pending-project-${project.id}-${suffix}` : `ecorex-draft-${suffix}`;
}

function isRetryableConcurrencyResult(result?: ChatSendResult | null) {
  return Boolean(
    result
    && result.status === "error"
    && (
      result.code === "REQUEST_CONFLICT_RETRYABLE"
      || result.error_type === "concurrency_conflict"
      || result.state === "retryable_conflict"
    )
    && (result.retryable || result.recoverable)
  );
}

function chatSendErrorMessage(result: ChatSendResult) {
  if (isRetryableConcurrencyResult(result)) {
    return result.message || "The previous run is still stopping. Please retry shortly.";
  }
  return result.message || "发送失败";
}

function isModelConfigSendError(result?: Pick<ChatSendResult, "code" | "error_type" | "message"> | null) {
  if (!result) return false;
  const code = String(result.code || "").toUpperCase();
  if (
    code === "MODEL_CONFIG_UNAVAILABLE"
    || code === "ENTERPRISE_LOGIN_REQUIRED"
    || code === "ENTERPRISE_POLICY_SYNC_FAILED"
    || code === "ENTERPRISE_POLICY_UNAVAILABLE"
  ) {
    return true;
  }
  if (String(result.error_type || "") === "model_config") return true;
  return /模型配置|可用模型|登录状态已失效|企业模型/.test(String(result.message || ""));
}

function BrandMark() {
  const [failed, setFailed] = useState(false);
  return (
    <div className="brand-mark" aria-hidden="true">
      {failed ? <Sparkles aria-hidden="true" /> : <img src={brandIconUrl} alt="" onError={() => setFailed(true)} />}
    </div>
  );
}

function versionLabel(version?: string) {
  const value = String(version || "").trim();
  if (!value) return "";
  return value.startsWith("v") ? value : `v${value}`;
}

function WindowBrand(props: { version?: string } = {}) {
  const label = versionLabel(props.version);
  return (
    <div className="window-brand" aria-hidden="true">
      <img src={brandIconUrl} alt="" />
      <span>EcoreX</span>
      {label && <small>{label}</small>}
    </div>
  );
}

function formatTime(value?: string | number) {
  if (!value) return "刚刚";
  const normalized = typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function timeMs(value?: string | number) {
  if (!value) return 0;
  if (typeof value === "string" && /^(运行中|正在停止|刚刚|本地)$/.test(value)) return 0;
  const normalized = typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value;
  const parsed = new Date(normalized).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function latestMessageMs(messages: ChatItem[] | undefined) {
  return (messages || []).reduce((latest, message) => Math.max(latest, timeMs(message.createdAt)), 0);
}

function latestTimeValue(...values: Array<string | number | undefined>) {
  let bestValue: string | number | undefined;
  let bestMs = 0;
  for (const value of values) {
    const ms = timeMs(value);
    if (ms > bestMs) {
      bestMs = ms;
      bestValue = value;
    }
  }
  return bestValue || values.find((value) => Boolean(value)) || "";
}

function projectListKey(project: ProjectFolder) {
  const pathValue = String(project.path || "").trim();
  if (pathValue) {
    const normalized = pathValue.replace(/\\/g, "/").replace(/\/+$/, "");
    return `path:${(window.ecorexDesktop?.platform || "").toLowerCase() === "win32" ? normalized.toLowerCase() : normalized}`;
  }
  return `id:${String(project.id || "").trim()}`;
}

function projectUpdatedMs(project: ProjectFolder) {
  return timeMs(project.updatedAt) || 0;
}

function mergeProjectFolders(current: ProjectFolder[], incoming?: ProjectFolder[]) {
  if (!Array.isArray(incoming) || incoming.length === 0) return current;
  const merged = new Map<string, ProjectFolder>();
  const order: string[] = [];
  for (const project of [...current, ...incoming]) {
    if (!project || !project.id || !project.path) continue;
    const key = projectListKey(project);
    if (!key || key === "id:") continue;
    const existing = merged.get(key);
    if (!existing) {
      merged.set(key, project);
      order.push(key);
      continue;
    }
    const nextProject = projectUpdatedMs(project) >= projectUpdatedMs(existing) ? { ...existing, ...project } : { ...project, ...existing };
    if (key.startsWith("path:") && existing.id && project.id && existing.id !== project.id) {
      nextProject.id = existing.id;
    }
    merged.set(key, nextProject);
  }
  return order.map((key) => merged.get(key)).filter(Boolean) as ProjectFolder[];
}

function normalizeSessionProjectsForProjects(sessionProjects: unknown, projects: ProjectFolder[]) {
  if (!sessionProjects || typeof sessionProjects !== "object") return {};
  const validProjectIds = new Set(projects.map((project) => project.id).filter(Boolean));
  const projectIdsKnown = validProjectIds.size > 0;
  const normalized: SessionProjectMap = {};
  Object.entries(sessionProjects as Record<string, unknown>).forEach(([sessionId, projectId]) => {
    const sessionKey = String(sessionId || "").trim();
    const projectKey = String(projectId || "").trim();
    if (!sessionKey || !projectKey) return;
    if (projectIdsKnown && !validProjectIds.has(projectKey)) return;
    normalized[sessionKey] = projectKey;
  });
  return normalized;
}

function projectBindingFromProject(project: ProjectFolder, source: ProjectSessionBinding["source"] = "project-new-session"): ProjectSessionBinding {
  const now = new Date().toISOString();
  return {
    projectId: project.id,
    projectName: project.name,
    projectPath: project.path,
    memoryPath: project.memoryPath || `${project.path}/.ecorex/project-memory.md`,
    dreamsPath: project.dreamsPath || `${project.path}/.ecorex/dreams`,
    createdAt: now,
    lastUsedAt: now,
    source
  };
}

function projectFolderFromBinding(binding: ProjectSessionBinding): ProjectFolder {
  return {
    id: binding.projectId,
    name: binding.projectName || binding.projectId,
    path: binding.projectPath,
    memoryPath: binding.memoryPath || (binding.projectPath ? `${binding.projectPath}/.ecorex/project-memory.md` : ""),
    dreamsPath: binding.dreamsPath || (binding.projectPath ? `${binding.projectPath}/.ecorex/dreams` : ""),
    updatedAt: binding.lastUsedAt || binding.createdAt || new Date().toISOString()
  };
}

function normalizeProjectSessionBindingsForProjects(
  bindings: unknown,
  projects: ProjectFolder[],
  sessionProjects?: SessionProjectMap
) {
  if (!bindings || typeof bindings !== "object") return {};
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const projectIdsKnown = projectById.size > 0;
  const normalized: SessionProjectBindingMap = {};
  Object.entries(bindings as Record<string, unknown>).forEach(([sessionId, value]) => {
    if (!value || typeof value !== "object") return;
    const raw = value as Record<string, unknown>;
    const sessionKey = String(sessionId || "").trim();
    const rawProjectId = String(raw.projectId || raw.project_id || sessionProjects?.[sessionKey] || "").trim();
    if (!sessionKey || !rawProjectId) return;
    const project = projectById.get(rawProjectId);
    if (projectIdsKnown && !project && !raw.projectPath && !raw.project_path) return;
    normalized[sessionKey] = {
      projectId: rawProjectId,
      projectName: String(raw.projectName || raw.project_name || project?.name || rawProjectId),
      projectPath: String(raw.projectPath || raw.project_path || project?.path || ""),
      memoryPath: String(raw.memoryPath || raw.memory_path || project?.memoryPath || ""),
      dreamsPath: String(raw.dreamsPath || raw.dreams_path || project?.dreamsPath || ""),
      createdAt: String(raw.createdAt || raw.created_at || ""),
      lastUsedAt: String(raw.lastUsedAt || raw.last_used_at || ""),
      source: String(raw.source || "runtime")
    };
  });
  return normalized;
}

type ProjectBindingLookupOptions = {
  allowFallbackProject?: boolean;
};

function projectBindingForSession(
  sessionId: string,
  sessionProjectBindings: SessionProjectBindingMap,
  sessionProjects: SessionProjectMap,
  sessionUiState: Record<string, SessionUiState>,
  projects: ProjectFolder[],
  fallbackProject?: ProjectFolder | null,
  options: ProjectBindingLookupOptions = {}
) {
  const existing = sessionProjectBindings[sessionId] || sessionUiState[sessionId]?.projectBinding || null;
  if (existing?.projectId && existing.projectPath) return existing;
  const projectId = sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState);
  const project = projectId ? projects.find((item) => item.id === projectId) : null;
  if (!project && options.allowFallbackProject && fallbackProject?.id) {
    return projectBindingFromProject(fallbackProject, "runtime");
  }
  return project ? projectBindingFromProject(project, "runtime") : null;
}

function projectForSessionDisplay(
  sessionId: string,
  projectId: string | null,
  projectById: Map<string, ProjectFolder>,
  sessionProjectBindings: SessionProjectBindingMap,
  sessionUiState: Record<string, SessionUiState>
) {
  if (projectId && projectById.has(projectId)) return projectById.get(projectId);
  const binding = sessionProjectBindings[sessionId] || sessionUiState[sessionId]?.projectBinding || null;
  if (binding?.projectId) return projectFolderFromBinding(binding);
  return undefined;
}

function projectBindingFromRuntimeSession(session: RuntimeSession): ProjectSessionBinding | null {
  const projectId = String(session.projectId || "").trim();
  const projectPath = String(session.projectPath || "").trim();
  if (!projectId && !projectPath) return null;
  return {
    projectId: projectId || projectPath,
    projectName: String(session.projectName || projectId || projectPath),
    projectPath,
    memoryPath: session.memoryPath,
    dreamsPath: session.dreamsPath,
    source: "runtime"
  };
}

function runtimeSessionDeclaresGeneralOwner(session: RuntimeSession) {
  const scope = String(session.scope || "").trim().toLowerCase();
  return scope === "general" || (
    Object.prototype.hasOwnProperty.call(session, "project")
    && session.project === null
    && !session.projectId
    && !session.projectPath
  );
}

function normalizePinnedProjectsForProjects(pinnedProjects: unknown, projects: ProjectFolder[]) {
  if (!pinnedProjects || typeof pinnedProjects !== "object") return {};
  const validProjectIds = new Set(projects.map((project) => project.id).filter(Boolean));
  const projectIdsKnown = validProjectIds.size > 0;
  const normalized: StringBoolMap = {};
  Object.entries(pinnedProjects as Record<string, unknown>).forEach(([projectId, pinned]) => {
    const projectKey = String(projectId || "").trim();
    if (!projectKey) return;
    if (projectIdsKnown && !validProjectIds.has(projectKey)) return;
    normalized[projectKey] = Boolean(pinned);
  });
  return normalized;
}

function sessionActivityMs(row: SessionRow) {
  if (typeof row.sortKeyMs === "number" && Number.isFinite(row.sortKeyMs)) return row.sortKeyMs;
  const activity = timeMs(row.activityAt);
  if (activity) return activity;
  return timeMs(row.createdAt);
}

function pinnedSessionMs(sessionId: string, pinnedSessions: StringBoolMap, pinnedSessionTimes: StringNumberMap, fallbackMs = 0) {
  if (!pinnedSessions[sessionId]) return undefined;
  const pinnedAt = Number(pinnedSessionTimes[sessionId] || 0);
  if (Number.isFinite(pinnedAt) && pinnedAt > 0) return pinnedAt;
  return fallbackMs > 0 ? fallbackMs : undefined;
}

function sessionProjectIdFromState(
  sessionId: string,
  sessionProjects: SessionProjectMap,
  sessionUiState?: Record<string, SessionUiState>,
  fallbackProjectId?: string | null
) {
  return sessionProjects[sessionId]
    || sessionUiState?.[sessionId]?.projectBinding?.projectId
    || sessionUiState?.[sessionId]?.projectId
    || fallbackProjectId
    || null;
}

function shortTitle(text: string) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 22) : NEW_SESSION_START_TITLE;
}

function isPendingProjectSessionId(sessionId: string) {
  return String(sessionId || "").startsWith("ecorex-pending-project-");
}

function mapSessions(
  snapshot: RuntimeSnapshot,
  activeSessionId: string,
  localTitle: string,
  sessionProjects: SessionProjectMap,
  sessionProjectBindings: SessionProjectBindingMap,
  sessionTitles: StringMap,
  pinnedSessions: StringBoolMap,
  pinnedSessionTimes: StringNumberMap,
  projects: ProjectFolder[],
  sessionUiState: Record<string, SessionUiState>,
  locallyCompletedRequestIds: StringBoolMap = {},
  nowMs = Date.now()
): SessionRow[] {
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const activeRequestBySession = new Map<string, RuntimeActiveRequest>(
    (snapshot.activeRequests || [])
      .filter((request) => request.session_id && request.request_id)
      .filter(isPrimaryChatActiveRequest)
      .filter((request) => !locallyCompletedRequestIds[String(request.request_id || "")])
      .map((request) => [String(request.session_id), request])
  );
  const rows: SessionRow[] = snapshot.sessions.map((session, index) => {
    const id = session.session_id || session.id || `runtime-${index}`;
    const runtimeBinding = projectBindingFromRuntimeSession(session);
    const runtimeGeneralOwner = runtimeSessionDeclaresGeneralOwner(session);
    const runtimeBindings = runtimeBinding ? { ...sessionProjectBindings, [id]: runtimeBinding } : sessionProjectBindings;
    const projectId = runtimeBinding?.projectId || (runtimeGeneralOwner ? null : sessionProjectIdFromState(id, sessionProjects, sessionUiState));
    const project = runtimeGeneralOwner && !runtimeBinding ? undefined : projectForSessionDisplay(id, projectId, projectById, runtimeBindings, sessionUiState);
    const cached = sessionUiState[id];
    const cachedActivity = latestTimeValue(cached?.lastActivityAt, latestMessageMs(cached?.messages));
    const activeRequest = activeRequestBySession.get(id);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activeElapsed = runtimeRequestElapsedLabel(activeRequest, nowMs);
    const activityAt = latestTimeValue(cachedActivity, session.last_active, session.updatedAt, session.created_at);
    const sortKeyMs = timeMs(activityAt) || timeMs(session.created_at) || 0;
    return {
      id,
      title: sessionTitles[id] || session.title || session.session_id || "未命名会话",
      detail: project ? project.name : "",
      activityAt,
      createdAt: session.created_at || cachedActivity || activityAt,
      sortKeyMs,
      updatedAt: activeRequestId
        ? (isCancelling ? `正在停止${activeElapsed ? ` · 已处理 ${activeElapsed}` : ""}` : activeElapsed ? `已处理 ${activeElapsed}` : "运行中")
        : activityAt || "",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : id === activeSessionId ? "active" : "ready",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[id]),
      pinnedAt: pinnedSessionMs(id, pinnedSessions, pinnedSessionTimes, sortKeyMs),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    } satisfies SessionRow;
  });
  const rowIds = new Set(rows.map((row) => row.id));
  for (const [sessionId, cached] of Object.entries(sessionUiState)) {
    if (rowIds.has(sessionId)) continue;
    const hasContent = Boolean(cached.composerText || cached.attachments.length || cached.messages.length);
    if (!hasContent) continue;
    const projectId = sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState);
    const project = projectForSessionDisplay(sessionId, projectId, projectById, sessionProjectBindings, sessionUiState);
    const cachedMessages = cached.messages || [];
    const hasRecoveryOrLive = cachedMessages.some((message) => Boolean(message.recovery) || isLiveAssistantMessage(message));
    const live = hasRecoveryOrLive || cachedMessages.some((message) => (
      isLiveAssistantMessage(message)
      && !(message.requestId && locallyCompletedRequestIds[message.requestId])
    ));
    const activeRequest = activeRequestBySession.get(sessionId);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activeElapsed = runtimeRequestElapsedLabel(activeRequest, nowMs);
    const activityAt = cached.lastActivityAt || latestMessageMs(cached.messages);
    const sortKeyMs = timeMs(activityAt) || 0;
    rows.push({
      id: sessionId,
      activityAt,
      createdAt: activityAt,
      sortKeyMs,
      title: sessionTitles[sessionId] || cached.title || "未命名会话",
      detail: project ? project.name : "",
      updatedAt: activeRequestId
        ? (isCancelling ? `正在停止${activeElapsed ? ` · 已处理 ${activeElapsed}` : ""}` : activeElapsed ? `已处理 ${activeElapsed}` : "运行中")
        : live ? "运行中" : "本地",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : live ? "waiting" : sessionId === activeSessionId ? "active" : "ready",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[sessionId]),
      pinnedAt: pinnedSessionMs(sessionId, pinnedSessions, pinnedSessionTimes, sortKeyMs),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
    rowIds.add(sessionId);
  }
  for (const [sessionId, activeRequest] of activeRequestBySession.entries()) {
    if (rowIds.has(sessionId)) continue;
    const projectId = sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState);
    const project = projectForSessionDisplay(sessionId, projectId, projectById, sessionProjectBindings, sessionUiState);
    const requestId = activeRequest.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest.cancelled);
    const activeElapsed = runtimeRequestElapsedLabel(activeRequest, nowMs);
    const activityAt = activeRequest.created_at || Date.now();
    const sortKeyMs = timeMs(activityAt) || Date.now();
    rows.push({
      id: sessionId,
      activityAt,
      createdAt: activityAt,
      sortKeyMs,
      title: sessionTitles[sessionId] || sessionUiState[sessionId]?.title || sessionId,
      detail: project ? project.name : "",
      updatedAt: isCancelling ? `正在停止${activeElapsed ? ` · 已处理 ${activeElapsed}` : ""}` : activeElapsed ? `已处理 ${activeElapsed}` : "运行中",
      status: isCancelling ? "cancelling" : "waiting",
      requestId,
      streamAvailable: activeRequest.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[sessionId]),
      pinnedAt: pinnedSessionMs(sessionId, pinnedSessions, pinnedSessionTimes, sortKeyMs),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
    rowIds.add(sessionId);
  }
  if (!rows.some((row) => row.id === activeSessionId)) {
    const projectId = sessionProjectIdFromState(activeSessionId, sessionProjects, sessionUiState);
    const project = projectForSessionDisplay(activeSessionId, projectId, projectById, sessionProjectBindings, sessionUiState);
    const activeRequest = activeRequestBySession.get(activeSessionId);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activeElapsed = runtimeRequestElapsedLabel(activeRequest, nowMs);
    const activityAt = sessionUiState[activeSessionId]?.lastActivityAt || latestMessageMs(sessionUiState[activeSessionId]?.messages) || Date.now();
    const sortKeyMs = timeMs(activityAt) || Date.now();
    rows.unshift({
      id: activeSessionId,
      activityAt,
      createdAt: activityAt,
      sortKeyMs,
      title: sessionTitles[activeSessionId] || localTitle || NEW_SESSION_START_TITLE,
      detail: project ? project.name : "",
      updatedAt: activeRequestId
        ? (isCancelling ? `正在停止${activeElapsed ? ` · 已处理 ${activeElapsed}` : ""}` : activeElapsed ? `已处理 ${activeElapsed}` : "运行中")
        : "刚刚",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : "active",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[activeSessionId]),
      pinnedAt: pinnedSessionMs(activeSessionId, pinnedSessions, pinnedSessionTimes, sortKeyMs),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
  }
  return rows.sort((a, b) => {
    const pinnedDiff = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
    if (pinnedDiff) return pinnedDiff;
    if (a.pinned || b.pinned) {
      const pinnedTimeDiff = (b.pinnedAt || 0) - (a.pinnedAt || 0);
      if (pinnedTimeDiff) return pinnedTimeDiff;
    }
    const activityDiff = sessionActivityMs(b) - sessionActivityMs(a);
    if (activityDiff) return activityDiff;
    const createdDiff = timeMs(b.createdAt) - timeMs(a.createdAt);
    if (createdDiff) return createdDiff;
    return a.id.localeCompare(b.id);
  });
}

function estimateTextTokens(text: string) {
  let latin = 0;
  let wide = 0;
  let symbols = 0;
  for (const char of text || "") {
    if (/\s/.test(char)) continue;
    if (/[\u3400-\u9fff\uf900-\ufaff]/.test(char)) {
      wide += 1;
    } else if (/[\x00-\x7f]/.test(char)) {
      latin += 1;
    } else {
      symbols += 1;
    }
  }
  return Math.ceil(latin / 4) + Math.ceil(wide * 1.25) + Math.ceil(symbols / 2);
}

function estimateStructuredTokens(value: unknown) {
  if (value === undefined || value === null || value === "") return 0;
  if (typeof value === "string") return estimateTextTokens(value);
  try {
    return estimateTextTokens(JSON.stringify(value));
  } catch {
    return estimateTextTokens(String(value));
  }
}

function estimateFileTokens(files: FileAttachment[]) {
  return files.reduce((total, file) => {
    const nameTokens = estimateTextTokens(file.file_name || file.file_path || "");
    const mediaCost = file.file_type === "image" ? 420 : file.file_type === "video" ? 900 : file.file_type === "directory" ? 220 : 160;
    return total + nameTokens + mediaCost;
  }, 0);
}

function estimateTokenCount(text: string, files: FileAttachment[]) {
  return Math.max(0, estimateTextTokens(text) + estimateFileTokens(files));
}

function estimateTokens(text: string, files: FileAttachment[]) {
  return Math.max(1, estimateTokenCount(text, files));
}

function usageTotal(usage?: TokenUsage | null) {
  if (!usage) return 0;
  const total = Number(usage.totalTokens || 0);
  if (Number.isFinite(total) && total > 0) return total;
  const input = Number(usage.inputTokens || 0);
  const output = Number(usage.outputTokens || 0);
  return Math.max(0, (Number.isFinite(input) ? input : 0) + (Number.isFinite(output) ? output : 0));
}

function compactTokenCount(value: number) {
  const safe = Math.max(0, Math.round(Number.isFinite(value) ? value : 0));
  if (safe >= 1_000_000) {
    const amount = safe / 1_000_000;
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}m`;
  }
  if (safe >= 1_000) {
    const amount = safe / 1_000;
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}k`;
  }
  return String(safe);
}

function quotaNumber(quota: UsageQuota | null | undefined, key: keyof UsageQuota) {
  const value = Number(quota?.[key]);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function isQuotaLimitFailure(quota: UsageQuota | null | undefined) {
  if (!quota || quota.allowed !== false) return false;
  if (quota.overDaily === true || quota.overWeekly === true) return true;
  const reason = String(quota.reason || "").toLowerCase();
  if (!reason) return false;
  if (/device does not match|invalid user token|user token expired|missing user token|尚未登录|未登录|登录|session|device/.test(reason)) return false;
  return /quota|daily|weekly|limit|额度|上限|已用完|超过|本次请求/.test(reason);
}

function isEnterpriseAuthFailure(quota: UsageQuota | null | undefined) {
  if (!quota || quota.allowed !== false) return false;
  const reason = String(quota.reason || "").toLowerCase();
  return /device does not match|invalid user token|user token expired|missing user token|尚未登录|未登录|登录|session|token|device/.test(reason);
}

function percentOf(used: number, limit: number) {
  if (!limit) return 0;
  return Math.min(100, Math.max(0, (used / limit) * 100));
}

function meterTitle(label: string, used: number, limit?: number) {
  const usedDetail = `${compactTokenCount(used)} tokens`;
  if (!limit) return `${label}：${usedDetail}，暂无上限数据`;
  const limitDetail = `${compactTokenCount(limit)} tokens`;
  return `${label}：${usedDetail} / ${limitDetail}，${Math.round(percentOf(used, limit))}%`;
}

function estimateContextTokens(messages: ChatItem[], draft: string, files: FileAttachment[]) {
  const history = messages.reduce((total, message) => {
    if (message.contextExcluded) return total;
    const messageTokens = estimateTokenCount(message.content || "", message.attachments || []);
    const stepTokens = (message.steps || []).reduce((stepTotal, step) => {
      if (step.type === "thinking" || step.type === "content" || step.type === "phase") {
        return stepTotal + estimateStructuredTokens(step.content);
      }
      if (step.type === "tool") {
        return stepTotal
          + estimateStructuredTokens(step.name)
          + estimateStructuredTokens(step.arguments)
          + estimateStructuredTokens(step.result)
          + 80;
      }
      return stepTotal + estimateStructuredTokens(step.fileName || step.url) + 120;
    }, 0);
    const legacyToolTokens = (message.toolCalls || []).reduce((toolTotal, tool) => (
      toolTotal
      + estimateStructuredTokens(tool.name)
      + estimateStructuredTokens(tool.arguments)
      + estimateStructuredTokens(tool.result)
      + 80
    ), 0);
    return total + messageTokens + stepTokens + legacyToolTokens;
  }, 0);
  return history + estimateTokenCount(draft, files);
}

function detectNeededPack(text: string, files: FileAttachment[], packs: CapabilityPack[]) {
  const lower = text.toLowerCase();
  const hasOffice = files.some((file) => /\.(pdf|docx?|xlsx?|pptx?)$/i.test(file.file_name));
  const hasBrowser = /网页|浏览器|playwright|打开网站|搜索网页|爬取|browser/.test(lower);
  const hasVoice = /语音|录音|转写|tts|stt|voice/.test(lower);
  const hasIm = /slack|discord|telegram|wechat|微信|钉钉|dingtalk/.test(lower);
  const hasLark = /飞书|lark|feishu/.test(lower);
  const targetId = hasOffice
    ? "office-pdf"
    : hasBrowser
      ? "browser-automation"
      : hasVoice
        ? "voice"
        : hasIm
          ? "im-channels"
          : hasLark
            ? "feishu-lark"
            : "";
  const pack = packs.find((item) => item.id === targetId);
  return pack && !pack.installed ? pack : null;
}

function fileIcon(file: FileAttachment) {
  if (file.file_type === "image") return <Upload aria-hidden="true" />;
  return <FileText aria-hidden="true" />;
}

function ThinkingIndicator({ label = "思考中", compact = false }: { label?: string; compact?: boolean }) {
  return (
    <span className={`thinking-indicator${compact ? " is-compact" : ""}`} title={label}>
      <span className="thinking-ring" aria-hidden="true" />
      {!compact && <span>{label}</span>}
    </span>
  );
}

function permissionModeLabel(mode?: PermissionMode) {
  return mode === "full-access"
    ? "完全访问权限"
    : "默认权限";
}

function composerPermissionTitle(mode?: PermissionMode) {
  return mode === "full-access" ? "完全访问权限" : "默认权限";
}

function composerPermissionDetail(mode?: PermissionMode) {
  return mode === "full-access"
    ? "按用户选择放开系统 PATH、Node/npx、Python 与本地文件能力"
    : "自动批准日常安全操作，高风险操作按结构化工具边界执行";
}

function composerPermissionIcon(mode?: PermissionMode) {
  if (mode === "full-access") return <KeyRound aria-hidden="true" />;
  return <CheckCircle2 aria-hidden="true" />;
}

function pausedMessageContent(content: string) {
  return content || "";
}

function interruptedMessageContent(content: string) {
  return content || "任务已中断，输入新消息后可重试";
}

type AgentFinishReason = "done" | "paused" | "cancelled" | "error";

function finishAgentSteps(steps: AgentStepDisclosure[] | undefined, reason: AgentFinishReason = "done") {
  return (steps || []).map((step) => {
    if (step.type === "thinking" && step.running) {
      return { ...step, running: false };
    }
    if (step.type === "tool" && step.running) {
      const status = step.status === "running"
        ? reason === "done"
          ? "done"
          : reason
        : step.status;
      return { ...step, running: false, status };
    }
    return step;
  });
}

function pausePendingMessage(item: ChatItem, interrupted = false): ChatItem {
  return {
    ...item,
    content: interrupted ? interruptedMessageContent(item.content) : pausedMessageContent(item.content),
    pending: false,
    paused: true,
    cancelled: false,
    steps: finishAgentSteps(item.steps, "paused"),
    toolCalls: item.toolCalls?.map((tool) => ({ ...tool, running: false }))
  };
}

function finishInactivePendingMessage(item: ChatItem): ChatItem {
  return {
    ...item,
    pending: false,
    paused: false,
    cancelled: false,
    steps: finishAgentSteps(item.steps, "done"),
    toolCalls: item.toolCalls?.map((tool) => ({ ...tool, running: false }))
  };
}

function normalizePausedMessages(
  items: ChatItem[],
  options?: {
    sessionId?: string;
    activeRequestIds?: Set<string>;
    staleSessionIds?: Set<string>;
    nowMs?: number;
    inactiveRequestGraceMs?: number;
  }
) {
  let changed = false;
  const staleSession = Boolean(options?.sessionId && options.staleSessionIds?.has(options.sessionId));
  const activeRequestIds = options?.activeRequestIds;
  const nowMs = options?.nowMs || Date.now();
  const inactiveGraceMs = options?.inactiveRequestGraceMs ?? 0;
  const next = items.map((item) => {
    if (!item.pending) return item;
    const createdAtMs = item.createdAt ? new Date(item.createdAt).getTime() : 0;
    const inGrace = Boolean(createdAtMs && Number.isFinite(createdAtMs) && nowMs - createdAtMs < inactiveGraceMs);
    if (!item.requestId || staleSession) {
      if (!staleSession && !item.requestId && inGrace) return item;
      changed = true;
      return pausePendingMessage(item, Boolean(staleSession));
    }
    if (activeRequestIds && !activeRequestIds.has(item.requestId)) {
      if (!inGrace) {
        changed = true;
        return finishInactivePendingMessage(item);
      }
    }
    return item;
  });
  return changed ? next : items;
}

function messageSequenceKey(message: ChatItem) {
  if (message.role === "user" && typeof message.userSeq === "number") return `user:${message.userSeq}`;
  if (message.role === "assistant" && typeof message.botSeq === "number") return `assistant:${message.botSeq}`;
  return "";
}

function messageRequestKey(message: ChatItem) {
  return message.role === "assistant" && message.requestId ? `request:${message.requestId}` : "";
}

function messageContentKey(message: ChatItem) {
  const content = redactInternalPromptText(message.content || "").trim();
  if (!content) return "";
  return `${message.role}:${content}`;
}

function normalizeArtifactKeySource(value?: string) {
  let source = String(value || "").trim();
  if (!source) return "";
  try {
    const base = typeof window !== "undefined" ? window.location.href : "http://localhost/";
    const parsed = new URL(source, base);
    const embeddedPath = parsed.searchParams.get("path")
      || parsed.searchParams.get("file")
      || parsed.searchParams.get("url");
    if (embeddedPath) {
      source = embeddedPath;
    } else if (parsed.protocol === "file:") {
      source = parsed.pathname;
    } else if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      source = `${parsed.origin}${parsed.pathname}`;
    }
  } catch {
    // Keep the raw value and normalize below.
  }
  try {
    source = decodeURIComponent(source);
  } catch {
    // Keep undecodable paths stable instead of dropping the key.
  }
  return source
    .replace(/^file:\/+/i, "")
    .replace(/\\/g, "/")
    .replace(/^\/([A-Za-z]:\/)/, "$1")
    .replace(/[?#].*$/, "")
    .replace(/\/+/g, "/")
    .toLowerCase();
}

function artifactMergeKey(artifact: AgentArtifact) {
  const kind = artifact.kind || "artifact";
  const sourceKey = [
    artifact.path,
    artifact.relativePath,
    artifact.url,
    artifact.previewUrl,
    artifact.thumbnailUrl,
    artifact.statusPath
  ]
    .map(normalizeArtifactKeySource)
    .find((value) => value && !value.startsWith("blob:") && !value.startsWith("data:"));
  if (sourceKey) return `${kind}:${sourceKey}`;

  const idKey = normalizeArtifactKeySource(artifact.id);
  if (idKey) return `${kind}:id:${idKey}`;

  const titleKey = normalizeArtifactKeySource(artifact.title);
  return titleKey ? `${kind}:title:${titleKey}` : "";
}

function artifactStatusPriority(status?: AgentArtifact["status"]) {
  if (status === "ready" || status === "failed") return 3;
  if (status === "superseded") return 2;
  if (status === "pending") return 1;
  return 0;
}

function mergeAgentArtifactRecord(existing: AgentArtifact, incoming: AgentArtifact): AgentArtifact {
  const existingPriority = artifactStatusPriority(existing.status);
  const incomingPriority = artifactStatusPriority(incoming.status);
  const merged = { ...existing, ...incoming };
  merged.status = incomingPriority >= existingPriority ? incoming.status : existing.status;
  merged.statusPath = incoming.statusPath || existing.statusPath;
  merged.previewUrl = incoming.previewUrl || existing.previewUrl;
  merged.thumbnailUrl = incoming.thumbnailUrl || existing.thumbnailUrl;
  merged.mimeType = incoming.mimeType || existing.mimeType;
  merged.path = incoming.path || existing.path;
  merged.relativePath = incoming.relativePath || existing.relativePath;
  merged.url = incoming.url || existing.url;
  merged.qualityEvidence = incoming.qualityEvidence || existing.qualityEvidence;
  merged.sizeBytes = typeof incoming.sizeBytes === "number" ? incoming.sizeBytes : existing.sizeBytes;
  return merged;
}

function mergeLocalTailArtifacts(historyMessage: ChatItem, localMessage?: ChatItem) {
  return mergeHistoryAndLocalRequestMessage(historyMessage, localMessage);
}

function mergeLocalAssistantRunTiming(historyMessage: ChatItem, localMessage?: ChatItem) {
  if (!localMessage?.runTiming) return historyMessage;
  if (historyMessage.role !== "assistant" || localMessage.role !== "assistant") return historyMessage;
  if (!isSameAssistantTurn(historyMessage, localMessage)) return historyMessage;
  const historyTiming = historyMessage.runTiming || {};
  const localTiming = localMessage.runTiming;
  const updatedAtMs = Math.max(
    typeof historyTiming.updatedAtMs === "number" ? historyTiming.updatedAtMs : 0,
    typeof localTiming.updatedAtMs === "number" ? localTiming.updatedAtMs : 0
  ) || undefined;
  return {
    ...historyMessage,
    runTiming: {
      ...localTiming,
      ...historyTiming,
      requestId: historyTiming.requestId || localTiming.requestId || historyMessage.requestId || localMessage.requestId,
      state: historyTiming.state || localTiming.state,
      startedAtMs: historyTiming.startedAtMs || localTiming.startedAtMs,
      updatedAtMs,
      terminalAtMs: historyTiming.terminalAtMs || localTiming.terminalAtMs
    }
  };
}

function mergeArtifactsIntoMessage(message: ChatItem, artifacts: AgentArtifact[]) {
  if (!artifacts.length) return message;
  const nextArtifacts = [...(message.artifacts || [])];
  const seen = new Set(nextArtifacts.map(artifactMergeKey).filter(Boolean));
  let changed = false;
  for (const artifact of artifacts) {
    const key = artifactMergeKey(artifact);
    const index = key ? nextArtifacts.findIndex((entry) => artifactMergeKey(entry) === key) : -1;
    if (index >= 0) {
      const merged = mergeAgentArtifactRecord(nextArtifacts[index], artifact);
      if (JSON.stringify(merged) !== JSON.stringify(nextArtifacts[index])) {
        nextArtifacts[index] = merged;
        changed = true;
      }
      continue;
    }
    nextArtifacts.push(artifact);
    if (key) seen.add(key);
    changed = true;
  }
  return changed ? { ...message, artifacts: nextArtifacts } : message;
}

function messageHasTerminalPayload(message: ChatItem) {
  return Boolean(redactInternalPromptText(message.content || "").trim())
    || Boolean(message.steps?.length)
    || Boolean(message.toolCalls?.length)
    || Boolean(message.artifacts?.length);
}

function isTerminalAssistantMessage(message?: ChatItem) {
  return Boolean(message
    && message.role === "assistant"
    && !message.pending
    && !message.paused
    && !message.visibleOutputSettled
    && !message.cancelled
    && messageHasTerminalPayload(message));
}

function isSameAssistantTurn(left: ChatItem, right: ChatItem) {
  if (left.role !== "assistant" || right.role !== "assistant") return false;
  if (left.requestId && right.requestId && left.requestId === right.requestId) return true;
  if (typeof left.botSeq === "number" && typeof right.botSeq === "number" && left.botSeq === right.botSeq) return true;
  if (typeof left.userSeq === "number" && typeof right.userSeq === "number" && left.userSeq === right.userSeq) return true;
  return false;
}

function isRecoveryAssistantMessage(message: ChatItem) {
  return message.role === "assistant" && Boolean(message.recovery);
}

function historyHasTerminalAssistantForPending(history: ChatItem[], pendingAssistant: ChatItem) {
  if (pendingAssistant.role !== "assistant") return false;
  const requestKey = messageRequestKey(pendingAssistant);
  const sequenceKey = messageSequenceKey(pendingAssistant);
  return history.some((message) => (
    message.role === "assistant"
    && isTerminalAssistantMessage(message)
    && (
      (requestKey && messageRequestKey(message) === requestKey)
      || (sequenceKey && messageSequenceKey(message) === sequenceKey)
    )
  ));
}

function historyHasFinalAssistantAfterUserTurn(history: ChatItem[], userIndex: number) {
  if (userIndex < 0 || history[userIndex]?.role !== "user") return false;
  for (let index = userIndex + 1; index < history.length; index += 1) {
    const message = history[index];
    if (message.role === "user") return false;
    if (message.role === "assistant" && isTerminalAssistantMessage(message)) return true;
  }
  return false;
}

function mergeHistoryAndLocalRequestMessage(historyMessage: ChatItem, localMessage?: ChatItem) {
  if (!localMessage) return historyMessage;
  const historyWithLocalArtifacts = mergeLocalAssistantRunTiming(
    mergeArtifactsIntoMessage(historyMessage, localMessage.artifacts || []),
    localMessage
  );
  if (historyMessage.role !== "assistant" || localMessage.role !== "assistant") {
    return historyWithLocalArtifacts;
  }
  if (!isSameAssistantTurn(historyMessage, localMessage)) {
    return historyWithLocalArtifacts;
  }
  const historyText = redactInternalPromptText(historyMessage.content || "").trim();
  const localText = redactInternalPromptText(localMessage.content || "").trim();
  if (!isTerminalAssistantMessage(localMessage) || !localText) {
    return historyWithLocalArtifacts;
  }
  const historyHasSameText = Boolean(historyText) && historyText === localText;
  const historyIsClearlyStronger = historyText.length > localText.length + 64;
  if (historyHasSameText || historyIsClearlyStronger) {
    return historyWithLocalArtifacts;
  }
  const mergedLocal = mergeArtifactsIntoMessage(localMessage, historyMessage.artifacts || []);
  return {
    ...mergedLocal,
    id: historyMessage.id || mergedLocal.id,
    createdAt: historyMessage.createdAt || mergedLocal.createdAt,
    requestId: mergedLocal.requestId || historyMessage.requestId,
    userSeq: typeof historyMessage.userSeq === "number" ? historyMessage.userSeq : mergedLocal.userSeq,
    botSeq: typeof historyMessage.botSeq === "number" ? historyMessage.botSeq : mergedLocal.botSeq,
    steps: mergedLocal.steps?.length ? mergedLocal.steps : historyMessage.steps,
    toolCalls: mergedLocal.toolCalls?.length ? mergedLocal.toolCalls : historyMessage.toolCalls
  };
}

function mergeHistoryWithLocalMessages(history: ChatItem[], local: ChatItem[]) {
  if (!history.length || !local.length) return history.length ? history : local;
  const historyUserIndexBySequenceKey = new Map(history
    .map((message, index) => [messageSequenceKey(message), index] as [string, number])
    .filter(([key, index]) => Boolean(key) && history[index]?.role === "user"));
  const historyUserIndicesByContentKey = new Map<string, number[]>();
  history.forEach((message, index) => {
    if (message.role !== "user") return;
    const key = messageContentKey(message);
    if (!key) return;
    historyUserIndicesByContentKey.set(key, [...(historyUserIndicesByContentKey.get(key) || []), index]);
  });
  const localUserTotalsByContentKey = new Map<string, number>();
  local.forEach((message) => {
    if (message.role !== "user") return;
    const key = messageContentKey(message);
    if (!key) return;
    localUserTotalsByContentKey.set(key, (localUserTotalsByContentKey.get(key) || 0) + 1);
  });
  const localUserSeenByContentKey = new Map<string, number>();
  const sequenceKeys = new Set(history.map(messageSequenceKey).filter(Boolean));
  const requestKeys = new Set(history.map(messageRequestKey).filter(Boolean));
  const contentKeys = new Set(history.map(messageContentKey).filter(Boolean));
  const localByRequestKey = new Map(local.map((message) => [messageRequestKey(message), message]).filter(([key]) => Boolean(key)) as [string, ChatItem][]);
  const localBySequenceKey = new Map(local
    .filter((message) => isTerminalAssistantMessage(message))
    .map((message) => [messageSequenceKey(message), message])
    .filter(([key]) => Boolean(key)) as [string, ChatItem][]);
  const preserved: ChatItem[] = [];
  let skipPendingAssistantAfterMatchedUser = false;
  const mergedHistory = history.map((message) => mergeLocalTailArtifacts(
    message,
    localByRequestKey.get(messageRequestKey(message)) || localBySequenceKey.get(messageSequenceKey(message))
  ));

  for (let localIndex = 0; localIndex < local.length; localIndex += 1) {
    const message = local[localIndex];
    const nextLocalMessage = local[localIndex + 1];
    let matchedHistoryUserIndex = -1;
    if (message.role === "user") {
      const userSequenceKey = messageSequenceKey(message);
      const userContentKey = messageContentKey(message);
      if (userContentKey) {
        localUserSeenByContentKey.set(userContentKey, (localUserSeenByContentKey.get(userContentKey) || 0) + 1);
      }
      if (userSequenceKey && historyUserIndexBySequenceKey.has(userSequenceKey)) {
        matchedHistoryUserIndex = historyUserIndexBySequenceKey.get(userSequenceKey) ?? -1;
      } else if (userContentKey) {
        const historyIndices = historyUserIndicesByContentKey.get(userContentKey) || [];
        const localTotal = localUserTotalsByContentKey.get(userContentKey) || 1;
        const localSeen = localUserSeenByContentKey.get(userContentKey) || 1;
        const firstComparableHistoryIndex = Math.max(0, historyIndices.length - localTotal);
        matchedHistoryUserIndex = historyIndices[Math.min(historyIndices.length - 1, firstComparableHistoryIndex + localSeen - 1)] ?? -1;
      }
    }
    if (message.role === "user" && nextLocalMessage && isRecoveryAssistantMessage(nextLocalMessage)) {
      const localAssistant = nextLocalMessage;
      const historyHasSameAssistantTurn = history.some((message) => (
        message.role === "assistant"
        && Boolean(localAssistant && isSameAssistantTurn(message, localAssistant))
      ));
      if (historyHasSameAssistantTurn) {
        skipPendingAssistantAfterMatchedUser = true;
      }
    }
    if (isRecoveryAssistantMessage(message)) {
      const localAssistant = message;
      const historyHasSameAssistantTurn = history.some((message) => (
        message.role === "assistant"
        && Boolean(localAssistant && isSameAssistantTurn(message, localAssistant))
      ));
      const hasAnchor = Boolean(messageRequestKey(message) || messageSequenceKey(message));
      if (!hasAnchor || historyHasSameAssistantTurn || historyHasTerminalAssistantForPending(history, message)) {
        continue;
      }
    }
    if (message.role === "assistant" && message.pending && message.id.startsWith("a-resume-") && historyHasTerminalAssistantForPending(history, message)) {
      continue;
    }
    const sequenceKey = messageSequenceKey(message);
    if (sequenceKey && sequenceKeys.has(sequenceKey)) {
      skipPendingAssistantAfterMatchedUser = historyHasFinalAssistantAfterUserTurn(history, matchedHistoryUserIndex);
      continue;
    }
    const requestKey = messageRequestKey(message);
    if (requestKey && requestKeys.has(requestKey)) {
      skipPendingAssistantAfterMatchedUser = false;
      continue;
    }
    const contentKey = messageContentKey(message);
    if (contentKey && contentKeys.has(contentKey)) {
      skipPendingAssistantAfterMatchedUser = historyHasFinalAssistantAfterUserTurn(history, matchedHistoryUserIndex);
      continue;
    }

    if (message.role === "assistant" && message.pending && skipPendingAssistantAfterMatchedUser) {
      skipPendingAssistantAfterMatchedUser = false;
      continue;
    }

    const localOnly = message.pending
      || message.paused
      || message.id.startsWith("u-")
      || message.id.startsWith("a-")
      || (!sequenceKey && !requestKey);
    if (!localOnly) continue;

    preserved.push(message);
    skipPendingAssistantAfterMatchedUser = false;
    if (sequenceKey) sequenceKeys.add(sequenceKey);
    if (requestKey) requestKeys.add(requestKey);
    if (contentKey) contentKeys.add(contentKey);
  }

  return preserved.length ? [...mergedHistory, ...preserved] : mergedHistory;
}

function plainTextForMessage(message: ChatItem) {
  const parts: string[] = [];
  const seen = new Set<string>();
  const addPart = (value?: string) => {
    const text = redactInternalPromptText(value || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    parts.push(text);
  };
  addPart(message.content);
  for (const step of message.steps || []) {
    if (step.type === "content" && step.content && !step.intermediate) {
      addPart(step.content);
    } else if (step.type === "media" && (step.url || step.filePath)) {
      const source = step.filePath || step.url || "";
      addPart(step.fileName ? `${step.fileName}: ${source}` : source);
    }
  }
  for (const file of message.attachments || []) {
    addPart(`${file.file_name}: ${file.file_path}`);
  }
  return parts.join("\n\n").trim();
}

function isLiveAssistantMessage(message: ChatItem) {
  return message.role === "assistant" && message.pending === true && !message.paused && !message.cancelled;
}

function isSilentPausedAssistantMessage(message: ChatItem) {
  return message.role === "assistant"
    && Boolean(message.paused)
    && !message.pending
    && !message.cancelled
    && !message.content.trim()
    && !(message.reasoning || "").trim()
    && !(message.steps || []).length
    && !(message.toolCalls || []).length;
}

function normalizeToolCall(tool: RuntimeToolCall | ToolCallDisclosure): ToolCallDisclosure {
  const fn = "function" in tool ? tool.function : undefined;
  const result = typeof tool.result === "string" ? redactInternalPromptText(tool.result) : tool.result;
  return {
    name: tool.name || ("tool" in tool ? tool.tool : undefined) || fn?.name,
    arguments: tool.arguments ?? ("input" in tool ? tool.input : undefined) ?? fn?.arguments,
    result,
    qualityEvidence: normalizeQualityEvidence(tool.qualityEvidence || normalizeQualityEvidence(result)),
    status: tool.status,
    is_error: tool.is_error,
    execution_time: tool.execution_time,
    deadline_seconds: tool.deadline_seconds,
    max_seconds: tool.max_seconds,
    extension_count: tool.extension_count,
    lastHeartbeatAt: tool.lastHeartbeatAt
  };
}

function normalizeStep(step: RuntimeStep): AgentStepDisclosure {
  const type = String(step.type || "").toLowerCase();
  const content = redactInternalPromptText(step.content || step.text || step.thinking || "");
  if (type === "thinking" || type === "reasoning") {
    return { type: "thinking", content };
  }
  if (type === "tool" || type === "tool_start" || type === "tool_end") {
    return {
      type: "tool",
      name: step.name || step.tool,
      arguments: step.arguments ?? step.input,
      result: typeof step.result === "string" ? redactInternalPromptText(step.result) : step.result,
      qualityEvidence: normalizeQualityEvidence(step.qualityEvidence || normalizeQualityEvidence(step.result)),
      status: step.status,
      is_error: step.is_error,
      execution_time: step.execution_time,
      deadline_seconds: step.deadline_seconds,
      max_seconds: step.max_seconds,
      extension_count: step.extension_count,
      lastHeartbeatAt: step.lastHeartbeatAt,
      running: type === "tool_start" || step.status === "running"
    };
  }
  if (type === "phase") {
    return { type: "phase", content };
  }
  if (type === "image" || type === "video" || type === "audio" || type === "file" || step.file_type) {
    const fileType = step.file_type === "image" || step.file_type === "video" || step.file_type === "audio"
      ? step.file_type
      : type === "image" || type === "video" || type === "audio"
        ? type
        : "file";
    return {
      type: "media",
      fileType,
      url: step.url || content || step.path,
      filePath: step.path,
      previewUrl: step.url,
      fileName: step.file_name
    };
  }
  return { type: "content", content };
}

function runtimeExtrasMediaSteps(item: RuntimeMessage): AgentStepDisclosure[] {
  const audio = item.extras?.audio;
  const audioUrl = typeof audio?.url === "string" ? audio.url : "";
  if (!audioUrl) return [];
  return [{
    type: "media",
    fileType: "audio",
    url: audioUrl,
    fileName: typeof audio?.kind === "string" ? audio.kind : undefined
  }];
}

function runtimeExtrasAttachments(item: RuntimeMessage): FileAttachment[] | undefined {
  const raw = item.extras?.attachments;
  if (!Array.isArray(raw)) return undefined;
  const attachments = raw
    .filter((entry): entry is Record<string, unknown> => Boolean(entry && typeof entry === "object"))
    .map((entry): FileAttachment | null => {
      const filePath = String(entry.file_path || entry.path || "").trim();
      if (!filePath) return null;
      const fileName = String(entry.file_name || entry.name || filePath.split(/[\\/]/).filter(Boolean).pop() || filePath);
      const rawType = String(entry.file_type || entry.type || "file");
      const fileType: FileAttachment["file_type"] = rawType === "image" || rawType === "video" || rawType === "audio" || rawType === "directory"
        ? rawType
        : "file";
      const attachment: FileAttachment = {
        file_path: filePath,
        file_name: fileName,
        file_type: fileType
      };
      if (typeof entry.preview_url === "string" && entry.preview_url) {
        attachment.preview_url = entry.preview_url;
      }
      return attachment;
    })
    .filter((entry): entry is FileAttachment => Boolean(entry));
  return attachments.length ? attachments : undefined;
}

function inferArtifactKind(rawKind: string, mimeType: string, source: string, hasLocalPath: boolean): AgentArtifact["kind"] {
  if (rawKind === "image" || rawKind === "video" || rawKind === "audio" || rawKind === "directory" || rawKind === "url" || rawKind === "diff") {
    return rawKind;
  }
  const lowerMime = mimeType.toLowerCase();
  const lowerSource = source.toLowerCase().split(/[?#]/)[0];
  if (lowerMime.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|svg)$/.test(lowerSource)) return "image";
  if (lowerMime.startsWith("video/") || /\.(mp4|webm|mov|m4v|mkv|avi)$/.test(lowerSource)) return "video";
  if (lowerMime.startsWith("audio/") || /\.(mp3|wav|ogg|m4a|aac|flac)$/.test(lowerSource)) return "audio";
  if (rawKind === "file" || hasLocalPath) return "file";
  return "url";
}

function normalizeArtifactEntry(entry: unknown, index: number, requestId?: string): AgentArtifact | null {
  if (!entry || typeof entry !== "object") return null;
  const raw = entry as Record<string, unknown>;
  const path = String(raw.path || raw.file_path || raw.filePath || "").trim();
  const url = String(raw.url || "").trim();
  const relativePath = String(raw.relativePath || raw.relative_path || "").trim();
  const mimeType = String(raw.mimeType || raw.mime_type || "").trim();
  const titleSource = String(raw.title || raw.file_name || raw.name || path || relativePath || url || "").trim();
  if (!path && !url && !relativePath && !titleSource) return null;
  const rawKind = String(raw.kind || raw.file_type || raw.type || "").toLowerCase();
  const kind = inferArtifactKind(rawKind, mimeType, path || relativePath || url || titleSource, Boolean(path || relativePath));
  const rawIntent = String(raw.intent || "").toLowerCase();
  const intent: AgentArtifact["intent"] = rawIntent === "changed-file" || rawIntent === "preview" ? rawIntent : "deliverable";
  const rawOperation = String(raw.operation || "").toLowerCase();
  const operation: AgentArtifact["operation"] = rawOperation === "modified" || rawOperation === "created" || rawOperation === "downloaded" || rawOperation === "deployed"
    ? rawOperation
    : "exported";
  const rawStatus = String(raw.status || "").toLowerCase();
  const status: AgentArtifact["status"] = rawStatus === "pending" || rawStatus === "failed" || rawStatus === "superseded" ? rawStatus : "ready";
  const id = String(raw.id || `${requestId || "artifact"}-${index}-${path || relativePath || url || titleSource}`).trim();
  const source = raw.source && typeof raw.source === "object" ? raw.source as Record<string, unknown> : {};
  const stats = raw.stats && typeof raw.stats === "object" ? raw.stats as Record<string, unknown> : {};
  const qualityEvidence = normalizeQualityEvidence(raw.qualityEvidence || raw.quality_evidence);
  return {
    id,
    requestId: String(raw.requestId || raw.request_id || requestId || "").trim() || undefined,
    kind,
    intent,
    operation,
    status,
    title: titleSource || "未命名产物",
    path: path || undefined,
    relativePath: relativePath || undefined,
    url: url || undefined,
    mimeType: mimeType || undefined,
    sizeBytes: typeof raw.sizeBytes === "number" ? raw.sizeBytes : typeof raw.size_bytes === "number" ? raw.size_bytes : undefined,
    previewUrl: String(raw.previewUrl || raw.preview_url || "").trim() || undefined,
    thumbnailUrl: String(raw.thumbnailUrl || raw.thumbnail_url || "").trim() || undefined,
    statusPath: String(raw.statusPath || raw.status_path || "").trim() || undefined,
    qualityEvidence,
    stats: Object.keys(stats).length ? {
      addedLines: typeof stats.addedLines === "number" ? stats.addedLines : typeof stats.added_lines === "number" ? stats.added_lines : undefined,
      removedLines: typeof stats.removedLines === "number" ? stats.removedLines : typeof stats.removed_lines === "number" ? stats.removed_lines : undefined,
      bytesWritten: typeof stats.bytesWritten === "number" ? stats.bytesWritten : typeof stats.bytes_written === "number" ? stats.bytes_written : undefined
    } : undefined,
    source: {
      toolCallId: String(source.toolCallId || source.tool_call_id || "").trim() || undefined,
      toolName: String(source.toolName || source.tool_name || "").trim() || undefined,
      activityId: String(source.activityId || source.activity_id || "").trim() || undefined,
      createdAt: typeof source.createdAt === "number" ? source.createdAt : typeof source.created_at === "number" ? source.created_at : undefined
    }
  };
}

function runtimeExtrasArtifacts(item: RuntimeMessage): AgentArtifact[] | undefined {
  const raw = Array.isArray(item.artifacts) ? item.artifacts : item.extras?.artifacts;
  if (!Array.isArray(raw)) return undefined;
  const artifacts = raw
    .map((entry, index) => normalizeArtifactEntry(entry, index, item.request_id))
    .filter((entry): entry is AgentArtifact => Boolean(entry));
  return artifacts.length ? artifacts : undefined;
}

function mapRuntimeMessage(item: RuntimeMessage, sessionId: string, index: number, contextStartSeq = 0, turnSeq?: number): ChatItem {
  const steps = [
    ...(item.steps?.map(normalizeStep) || []),
    ...runtimeExtrasMediaSteps(item)
  ];
  const displaySeq = typeof item._seq === "number" ? item._seq : typeof item.seq === "number" ? item.seq : undefined;
  const contextSeq = item.role === "user" ? displaySeq : (typeof displaySeq === "number" ? displaySeq : turnSeq);
  return {
    id: `${sessionId}-${index}`,
    role: item.role === "user" ? "user" : "assistant",
    content: redactInternalPromptText(item.content || ""),
    createdAt: item.created_at ? new Date(item.created_at * 1000).toISOString() : new Date().toISOString(),
    reasoning: item.reasoning ? redactInternalPromptText(item.reasoning) : undefined,
    steps: steps.length ? steps : undefined,
    attachments: item.role === "user" ? runtimeExtrasAttachments(item) : undefined,
    artifacts: item.role === "assistant" ? runtimeExtrasArtifacts(item) : undefined,
    toolCalls: item.tool_calls?.map(normalizeToolCall),
    requestId: item.request_id,
    userSeq: item.user_seq ?? item._seq ?? item.seq,
    botSeq: item.role === "assistant" ? item.seq : undefined,
    contextExcluded: contextStartSeq > 0 && typeof contextSeq === "number" && contextSeq < contextStartSeq
  };
}

function mapRuntimeHistory(messages: RuntimeMessage[], sessionId: string, contextStartSeq = 0): ChatItem[] {
  let currentTurnSeq: number | undefined;
  return messages.map((item, index) => {
    const seq = typeof item._seq === "number" ? item._seq : typeof item.seq === "number" ? item.seq : undefined;
    if (item.role === "user" && typeof seq === "number") currentTurnSeq = seq;
    return mapRuntimeMessage(item, sessionId, index, contextStartSeq, currentTurnSeq);
  });
}

function toolEnabled(tools: RuntimeTool[] | undefined, name: string) {
  return Boolean((tools || []).some((tool) => tool.name === name));
}

function extensionToolEnabled(snapshot: RuntimeSnapshot, name: string) {
  const expectedId = `tool:${name}`;
  return Boolean((snapshot.extensions || []).some((extension) => {
    const id = String(extension.id || "").trim();
    const status = String(extension.status || "").trim().toLowerCase();
    if (id !== expectedId && !(extension.type === "builtin_tool" && String(extension.displayName || "").trim() === name)) {
      return false;
    }
    if (extension.enabled === false || extension.installed === false) return false;
    return !["disabled", "error", "missing", "not_loaded"].includes(status);
  }));
}

function runtimeToolReady(snapshot: RuntimeSnapshot, name: string) {
  return toolEnabled(snapshot.tools, name) || extensionToolEnabled(snapshot, name);
}

function skillEnabled(skills: RuntimeSkill[] | undefined, name: string) {
  const skill = (skills || []).find((item) => item.name === name);
  return Boolean(skill && skill.enabled !== false);
}

type SkillMentionCategory = "creative" | "document" | "automation" | "developer" | "general" | "background";
type SkillSourceGroup = "builtin" | "custom" | "external";
type SkillPurposeGroup = "system" | "office" | "image_media" | "collaboration" | "data" | "development" | "automation" | "general";

type SkillDisplayRow = {
  key: string;
  name: string;
  displayName: string;
  display_name?: string;
  description?: string;
  source?: string;
  path?: string;
  enabled: boolean;
  sourceGroup: SkillSourceGroup;
  sourceLabel: string;
  purposeGroup: SkillPurposeGroup;
  purposeLabel: string;
  installed: boolean;
  status?: string;
  origin?: string;
  policy?: string;
  toggleable: boolean;
  locked: boolean;
  lockReason?: string;
  mentionable: boolean;
  category: SkillMentionCategory;
  categoryLabel: string;
  mentionHiddenReason?: string;
};

const SKILL_CATEGORY_LABELS: Record<SkillMentionCategory, string> = {
  creative: "创作",
  document: "文档",
  automation: "自动化",
  developer: "开发",
  general: "通用",
  background: "后台 / CLI"
};

type RawSkillDisplayRow = Omit<
  SkillDisplayRow,
  "mentionable" | "category" | "categoryLabel" | "mentionHiddenReason" | "sourceGroup" | "sourceLabel" | "purposeGroup" | "purposeLabel" | "locked" | "lockReason"
> & {
  rawCategory?: string;
  rawMentionCategory?: string;
  primaryEnv?: string;
  userInvocable?: boolean;
  disableModelInvocation?: boolean;
  explicitMentionable?: boolean;
  explicitMentionHiddenReason?: string;
  rawSourceGroup?: string;
  rawSourceLabel?: string;
  rawPurposeGroup?: string;
  rawPurposeLabel?: string;
  rawToggleable?: boolean;
  rawLocked?: boolean;
  rawLockReason?: string;
};

const SKILL_CATEGORY_ORDER: SkillMentionCategory[] = ["creative", "document", "automation", "developer", "general", "background"];
const SKILL_SOURCE_ORDER: SkillSourceGroup[] = ["builtin", "custom", "external"];
const SKILL_PURPOSE_ORDER: SkillPurposeGroup[] = ["system", "office", "image_media", "collaboration", "data", "development", "automation", "general"];

const SKILL_SOURCE_LABELS: Record<SkillSourceGroup, string> = {
  builtin: "内置",
  custom: "自建",
  external: "外部"
};

const SKILL_PURPOSE_LABELS: Record<SkillPurposeGroup, string> = {
  system: "系统能力",
  office: "办公能力",
  image_media: "图像 / 媒体",
  collaboration: "协作连接",
  data: "数据能力",
  development: "开发能力",
  automation: "自动化",
  general: "通用能力"
};

function normalizeSkillText(value: unknown) {
  return String(value || "").trim().toLowerCase();
}

function mapExplicitSkillCategory(value?: string): SkillMentionCategory | "" {
  const category = normalizeSkillText(value).replace(/[_\s]+/g, "-");
  if (!category) return "";
  if (["creative", "creation", "content", "media", "design"].includes(category)) return "creative";
  if (["document", "documents", "doc", "pdf", "office", "spreadsheet", "slides"].includes(category)) return "document";
  if (["automation", "browser", "computer-use", "workflow"].includes(category)) return "automation";
  if (["developer", "dev", "coding", "github", "figma", "macos"].includes(category)) return "developer";
  if (["background", "cli", "system", "internal", "tooling", "connector"].includes(category)) return "background";
  if (category === "skill" || category === "general") return "general";
  return "";
}

function normalizeSkillSourceGroup(row: Pick<RawSkillDisplayRow, "source" | "origin" | "rawSourceGroup">): SkillSourceGroup {
  const explicit = normalizeSkillText(row.rawSourceGroup);
  if (explicit === "builtin" || explicit === "custom" || explicit === "external") return explicit;
  const source = normalizeSkillText(row.source);
  const origin = normalizeSkillText(row.origin);
  if (source === "builtin" || origin === "builtin" || origin === "first-party" || origin === "factory") return "builtin";
  if (source === "custom" || source === "workspace" || origin === "workspace" || origin === "user") return "custom";
  return "external";
}

function normalizeSkillPurposeGroup(row: RawSkillDisplayRow): SkillPurposeGroup {
  const explicit = normalizeSkillText(row.rawPurposeGroup).replace(/[-\s]+/g, "_");
  const aliases: Record<string, SkillPurposeGroup> = {
    system: "system",
    internal: "system",
    tooling: "system",
    background: "system",
    office: "office",
    document: "office",
    documents: "office",
    doc: "office",
    pdf: "office",
    spreadsheet: "office",
    slides: "office",
    presentation: "office",
    creative: "image_media",
    creation: "image_media",
    media: "image_media",
    image: "image_media",
    image_media: "image_media",
    design: "image_media",
    collaboration: "collaboration",
    connector: "collaboration",
    lark: "collaboration",
    feishu: "collaboration",
    data: "data",
    database: "data",
    analytics: "data",
    developer: "development",
    development: "development",
    dev: "development",
    coding: "development",
    github: "development",
    automation: "automation",
    browser: "automation",
    workflow: "automation",
    computer_use: "automation",
    general: "general"
  };
  if (aliases[explicit]) return aliases[explicit];
  const text = [
    row.name,
    row.displayName,
    row.description,
    row.source,
    row.origin,
    row.path,
    row.primaryEnv
  ].map(normalizeSkillText).join(" ");
  if (/(office|document|documents|pdf|spreadsheet|slides|presentation|docx|pptx|xlsx|xlsm|文档|表格|幻灯片|办公)/.test(text)) return "office";
  if (/(image|vision|media|video|audio|figma|hallmark|remotion|design|creative|生成|图像|图片|视觉|媒体|设计)/.test(text)) return "image_media";
  if (/(lark|feishu|飞书|calendar|mail|approval|attendance|contact|wiki|base|minutes|okr|task|协作|日历|邮箱|审批)/.test(text)) return "collaboration";
  if (/(data|database|sql|csv|analytics|chart|dashboard|base|数据|分析|仪表盘)/.test(text)) return "data";
  if (/(github|openai|plugin|skill|codex|cli|developer|swift|xcode|debug|test|开发|调试|测试)/.test(text)) return "development";
  if (/(browser|chrome|automation|workflow|computer-use|自动化|浏览器)/.test(text)) return "automation";
  if (/(find|knowledge|memory|troubleshooting|a11y|system|系统|记忆|知识|检索|排障)/.test(text)) return "system";
  return "general";
}

function isBackgroundCliSkill(row: RawSkillDisplayRow) {
  const name = normalizeSkillText(row.name || row.displayName);
  const pathText = normalizeSkillText(row.path);
  const primaryEnv = normalizeSkillText(row.primaryEnv);
  const description = normalizeSkillText(row.description);
  const sourceText = normalizeSkillText(`${row.source || ""} ${row.origin || ""}`);
  if (/^(?:lark|feishu)(?:[-_:]|$)/.test(name)) return true;
  if (/(?:^|[\\/])(?:lark|feishu)-[^\\/]+[\\/]skill\.md$/.test(pathText)) return true;
  if (/^(?:lark|feishu)_/.test(primaryEnv)) return true;
  if (description.includes("lark-cli") || sourceText.includes("lark-cli")) return true;
  if ((description.includes("飞书") || sourceText.includes("飞书")) && (description.includes("cli") || sourceText.includes("cli"))) return true;
  return false;
}

function isTestFixtureSkill(row: RawSkillDisplayRow) {
  const name = normalizeSkillText(row.name || row.displayName);
  const pathText = normalizeSkillText(row.path);
  return /^good-skill(?:-|$)/.test(name) || pathText.includes("skill-format-check");
}

function skillMentionHiddenReason(value?: string) {
  const reason = normalizeSkillText(value);
  if (!reason) return "";
  if (reason.includes("lark") || reason.includes("feishu")) return "由飞书/Lark CLI 自动触发";
  if (reason.includes("test")) return "测试样例";
  if (reason.includes("background") || reason.includes("disable") || reason.includes("model")) return "后台触发";
  return value || "";
}

function skillLockReasonLabel(value?: string) {
  const reason = normalizeSkillText(value);
  if (!reason) return "";
  if (reason.includes("builtin") || reason.includes("built-in") || reason.includes("default")) return "内置能力默认启用";
  return value || "";
}

function inferSkillCategory(row: RawSkillDisplayRow): SkillMentionCategory {
  const mentionCategory = mapExplicitSkillCategory(row.rawMentionCategory);
  if (mentionCategory) return mentionCategory;
  const explicit = mapExplicitSkillCategory(row.rawCategory);
  if (explicit) return explicit;
  const text = [
    row.name,
    row.displayName,
    row.description,
    row.source,
    row.origin,
    row.path
  ].map(normalizeSkillText).join(" ");
  if (/(xiaohongshu|image|design|figma|hallmark|remotion|presentation|video|creative|生成|设计)/.test(text)) return "creative";
  if (/(document|documents|pdf|spreadsheet|slides|docx|pptx|xlsx|office|文档|表格|幻灯片)/.test(text)) return "document";
  if (/(browser|chrome|computer-use|automation|workflow|calendar|attendance|自动化|浏览器)/.test(text)) return "automation";
  if (/(github|build-macos|openai|plugin|skill|codex|cli|developer|swift|xcode|开发)/.test(text)) return "developer";
  return "general";
}

function finalizeSkillDisplayRow(row: RawSkillDisplayRow): SkillDisplayRow {
  const category = inferSkillCategory(row);
  const sourceGroup = normalizeSkillSourceGroup(row);
  const purposeGroup = normalizeSkillPurposeGroup(row);
  const locked = row.rawLocked === true || sourceGroup === "builtin";
  let mentionable = category !== "background";
  let mentionHiddenReason = "";
  const explicitHiddenReason = skillMentionHiddenReason(row.explicitMentionHiddenReason);

  if (row.explicitMentionable === false || row.userInvocable === false || row.disableModelInvocation) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "后台触发";
  }
  if (isBackgroundCliSkill(row)) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "由飞书/Lark CLI 自动触发";
  }
  if (isTestFixtureSkill(row)) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "测试样例";
  }

  const finalCategory: SkillMentionCategory = mentionable ? category : "background";
  const toggleable = locked ? false : row.rawToggleable !== undefined ? Boolean(row.rawToggleable) : Boolean(row.name);
  return {
    ...row,
    sourceGroup,
    sourceLabel: row.rawSourceLabel || SKILL_SOURCE_LABELS[sourceGroup],
    purposeGroup,
    purposeLabel: row.rawPurposeLabel || SKILL_PURPOSE_LABELS[purposeGroup],
    toggleable,
    locked,
    lockReason: skillLockReasonLabel(row.rawLockReason) || (locked ? "内置能力默认启用" : undefined),
    mentionable,
    category: finalCategory,
    categoryLabel: SKILL_CATEGORY_LABELS[finalCategory],
    mentionHiddenReason: mentionable ? undefined : mentionHiddenReason || "后台触发"
  };
}

function skillNameFromExtension(extension: RuntimeExtension) {
  const id = String(extension.id || "");
  return id.startsWith("skill:") ? id.slice("skill:".length) : "";
}

function extensionSkillEnabled(snapshot: RuntimeSnapshot, name: string) {
  const extension = (snapshot.extensions || []).find((item) => skillNameFromExtension(item) === name);
  if (extension) return extension.enabled !== false && extension.installed !== false;
  return skillEnabled(snapshot.skills, name);
}

function buildSkillDisplayRows(snapshot: RuntimeSnapshot): SkillDisplayRow[] {
  const legacyByName = new Map((snapshot.skills || []).map((skill) => [skill.name || skill.display_name || "", skill]));
  const rows: SkillDisplayRow[] = [];
  const seen = new Set<string>();
  for (const extension of snapshot.extensions || []) {
    if (extension.type !== "builtin_skill" && extension.type !== "user_skill") continue;
    const name = skillNameFromExtension(extension) || extension.displayName || extension.id;
    if (!name) continue;
    const legacy = legacyByName.get(name);
    seen.add(name);
    rows.push(finalizeSkillDisplayRow({
      key: extension.id || `skill:${name}`,
      name,
      displayName: extension.displayName || legacy?.display_name || name,
      display_name: extension.displayName || legacy?.display_name || name,
      description: extension.description || legacy?.description,
      source: extension.origin || legacy?.source,
      path: extension.sourcePath || legacy?.path,
      enabled: extension.enabled !== false && (legacy?.enabled ?? true) !== false,
      installed: extension.installed !== false,
      status: extension.status,
      origin: extension.origin,
      policy: extension.policy,
      toggleable: true,
      rawToggleable: extension.toggleable ?? legacy?.toggleable,
      rawLocked: extension.locked ?? legacy?.locked,
      rawLockReason: extension.lockReason || extension.lock_reason || legacy?.lockReason || legacy?.lock_reason,
      rawSourceGroup: extension.sourceGroup || extension.source_group || legacy?.sourceGroup || legacy?.source_group,
      rawSourceLabel: extension.sourceLabel || extension.source_label || legacy?.sourceLabel || legacy?.source_label,
      rawPurposeGroup: extension.purposeGroup || extension.purpose_group || legacy?.purposeGroup || legacy?.purpose_group,
      rawPurposeLabel: extension.purposeLabel || extension.purpose_label || legacy?.purposeLabel || legacy?.purpose_label,
      rawCategory: legacy?.category || extension.category,
      rawMentionCategory: legacy?.mention_category || extension.mention_category,
      primaryEnv: legacy?.primary_env || extension.primary_env,
      userInvocable: legacy?.user_invocable ?? extension.user_invocable,
      disableModelInvocation: legacy?.disable_model_invocation ?? extension.disable_model_invocation,
      explicitMentionable: legacy?.mentionable ?? extension.mentionable,
      explicitMentionHiddenReason: legacy?.mention_hidden_reason || extension.mention_hidden_reason
    }));
  }
  for (const skill of snapshot.skills || []) {
    const name = skill.name || skill.display_name || "";
    if (!name || seen.has(name)) continue;
    rows.push(finalizeSkillDisplayRow({
      key: `legacy:${name}`,
      name,
      displayName: skill.display_name || name,
      display_name: skill.display_name || name,
      description: skill.description,
      source: skill.source,
      path: skill.path,
      enabled: skill.enabled !== false,
      installed: true,
      status: skill.enabled === false ? "disabled" : "ready",
      toggleable: true,
      rawToggleable: skill.toggleable,
      rawLocked: skill.locked,
      rawLockReason: skill.lockReason || skill.lock_reason,
      rawSourceGroup: skill.sourceGroup || skill.source_group,
      rawSourceLabel: skill.sourceLabel || skill.source_label,
      rawPurposeGroup: skill.purposeGroup || skill.purpose_group,
      rawPurposeLabel: skill.purposeLabel || skill.purpose_label,
      rawCategory: skill.category,
      rawMentionCategory: skill.mention_category,
      primaryEnv: skill.primary_env,
      userInvocable: skill.user_invocable,
      disableModelInvocation: skill.disable_model_invocation,
      explicitMentionable: skill.mentionable,
      explicitMentionHiddenReason: skill.mention_hidden_reason
    }));
  }
  return rows.sort((a, b) => {
    const sourceDiff = SKILL_SOURCE_ORDER.indexOf(a.sourceGroup) - SKILL_SOURCE_ORDER.indexOf(b.sourceGroup);
    if (sourceDiff) return sourceDiff;
    const purposeDiff = SKILL_PURPOSE_ORDER.indexOf(a.purposeGroup) - SKILL_PURPOSE_ORDER.indexOf(b.purposeGroup);
    if (purposeDiff) return purposeDiff;
    return a.displayName.localeCompare(b.displayName);
  });
}

function memoryFileName(file: MemoryFile) {
  return file.filename || file.name || "未命名记忆";
}

function memoryFileTime(file: MemoryFile) {
  return file.updated_at || file.updatedAt || "";
}

function AuthGate(props: { onLogin: (session: EnterpriseSession) => void; version?: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      props.onLogin(await enterpriseLogin(email, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <WindowBrand version={props.version} />
      <section className="auth-panel">
        <BrandMark />
        <h1>EcoreX</h1>
        <p>亦芯广告 AI Agent</p>
        <form onSubmit={submit}>
          <label>
            登录邮箱
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="email" required />
          </label>
          <label>
            密码
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required />
          </label>
          {error && <div className="inline-error">{error}</div>}
          <button type="submit" disabled={busy}>{busy ? "正在登录" : "登录并进入"}</button>
        </form>
      </section>
    </main>
  );
}

export function App() {
  const bootSession = useMemo(() => pickBootSession(readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {})), []);
  const bootProjects = useMemo(() => readStorage<ProjectFolder[]>(PROJECTS_STORAGE_KEY, []), []);
  const bootSessionProjects = useMemo(() => readStorage<SessionProjectMap>(SESSION_PROJECTS_STORAGE_KEY, {}), []);
  const bootSessionProjectBindings = useMemo(
    () => normalizeProjectSessionBindingsForProjects(
      readStorage<SessionProjectBindingMap>(SESSION_PROJECT_BINDINGS_STORAGE_KEY, {}),
      bootProjects,
      bootSessionProjects
    ),
    [bootProjects, bootSessionProjects]
  );
  const bootActiveProjectId = bootSession?.id
    ? bootSessionProjectBindings[bootSession.id]?.projectId || bootSessionProjects[bootSession.id] || null
    : null;
  const [theme, setTheme] = useState<ThemeMode>(initialTheme);
  const [session, setSession] = useState<EnterpriseSession | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [runtimeSnapshot, setRuntimeSnapshot] = useState(initialRuntime);
  const [sidecarStatus, setSidecarStatus] = useState(initialSidecar);
  const [runClockTick, setRunClockTick] = useState(() => Date.now());
  const [quotaSnapshot, setQuotaSnapshot] = useState<UsageQuota | null>(null);
  const [activeSessionId, setActiveSessionId] = useState(bootSession?.id || `ecorex-${Date.now()}`);
  const [activeSessionTitle, setActiveSessionTitle] = useState(bootSession?.state.title || NEW_SESSION_START_TITLE);
  const [searchQuery, setSearchQuery] = useState("");
  const [messages, setMessages] = useState<ChatItem[]>(() => normalizePausedMessages(bootSession?.state.messages || []));
  const [historyContextUsed, setHistoryContextUsed] = useState(() => estimateContextTokens(bootSession?.state.messages || [], "", []));
  const [composerText, setComposerTextState] = useState(bootSession?.state.composerText || "");
  const [composerHasText, setComposerHasText] = useState(Boolean((bootSession?.state.composerText || "").trim()));
  const [attachments, setAttachments] = useState<FileAttachment[]>(bootSession?.state.attachments || []);
  const [composerDragActive, setComposerDragActive] = useState(false);
  const [, setActiveRequestId] = useState("");
  const [approval, setApproval] = useState<ApprovalState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [runCenterOpen, setRunCenterOpen] = useState(false);
  const [releaseNotesOpen, setReleaseNotesOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [previewFile, setPreviewFile] = useState<FileAttachment | null>(null);
  const [previewZoom, setPreviewZoom] = useState(1);
  const [packs, setPacks] = useState<CapabilityPack[]>([]);
  const [externalConnections, setExternalConnections] = useState<ExternalConnection[]>([]);
  const [externalConnectionDrafts, setExternalConnectionDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [externalConnectionsBusy, setExternalConnectionsBusy] = useState(false);
  const [permissionState, setPermissionState] = useState<PermissionState | null>(null);
  const [projects, setProjects] = useState<ProjectFolder[]>(() => bootProjects);
  const [sessionProjects, setSessionProjects] = useState<SessionProjectMap>(() => bootSessionProjects);
  const [sessionProjectBindings, setSessionProjectBindings] = useState<SessionProjectBindingMap>(() => bootSessionProjectBindings);
  const [projectPickerBusy, setProjectPickerBusy] = useState(false);
  const [projectStartMenuOpen, setProjectStartMenuOpen] = useState(false);
  const [projectStartSearch, setProjectStartSearch] = useState("");
  const [sessionTitles, setSessionTitles] = useState<StringMap>(() => readStorage<StringMap>(SESSION_TITLES_STORAGE_KEY, {}));
  const [lockedSessionTitles, setLockedSessionTitles] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(LOCKED_SESSION_TITLES_STORAGE_KEY, {}));
  const [pinnedSessions, setPinnedSessions] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_SESSIONS_STORAGE_KEY, {}));
  const [pinnedSessionTimes, setPinnedSessionTimes] = useState<StringNumberMap>(() => normalizeStringNumberMap(readStorage<StringNumberMap>(PINNED_SESSION_TIMES_STORAGE_KEY, {})));
  const [pinnedProjects, setPinnedProjects] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_PROJECTS_STORAGE_KEY, {}));
  const [unreadSessionIds, setUnreadSessionIds] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(UNREAD_SESSIONS_STORAGE_KEY, {}));
  const [sessionUiState, setSessionUiState] = useState<Record<string, SessionUiState>>(() => pruneSessionUiState(readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {})));
  const [enabledCapabilityPacks, setEnabledCapabilityPacks] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(CAPABILITY_ENABLED_STORAGE_KEY, {}));
  const [sessionRequestIds, setSessionRequestIds] = useState<StringMap>({});
  const [locallyCompletedRequestIds, setLocallyCompletedRequestIds] = useState<StringBoolMap>({});
  const [activeProjectId, setActiveProjectId] = useState<string | null>(bootActiveProjectId);
  const [pendingProjectStart, setPendingProjectStart] = useState<ProjectFolder | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("account");
  const [projectMenu, setProjectMenu] = useState<ProjectContextMenu>(null);
  const [chatFileMenu, setChatFileMenu] = useState<ChatFileContextMenu>(null);
  const [sidebarCollapse, setSidebarCollapse] = useState<SidebarCollapseState>(() => initialSidebarCollapseState());
  const [installingPackIds, setInstallingPackIds] = useState<StringBoolMap>({});
  const [installNotice, setInstallNotice] = useState<InstallNotice>(null);
  const [memoryFiles, setMemoryFiles] = useState<MemoryFile[]>([]);
  const [dreamFiles, setDreamFiles] = useState<MemoryFile[]>([]);
  const [schedulerBusy, setSchedulerBusy] = useState(false);
  const [toast, setToast] = useState("");
  const [copiedMessageId, setCopiedMessageId] = useState("");
  const [passwordDraft, setPasswordDraft] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const chatFileMenuRef = useRef<HTMLDivElement | null>(null);
  const projectStartMenuRef = useRef<HTMLDivElement | null>(null);
  const composerDragDepth = useRef(0);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const streamCleanup = useRef<null | (() => void)>(null);
  const streamCleanups = useRef<Record<string, () => void>>({});
  const streamCleanupRequestIds = useRef<StringMap>({});
  const streamDeltaBuffers = useRef<Record<string, { sessionId: string; assistantId: string; requestId: string; text: string; timer: number | null }>>({});
  const composerTextRef = useRef(composerText);
  const attachmentsRef = useRef(attachments);
  const draftUiStateTimer = useRef<number | null>(null);
  const composerDraftCommitTimer = useRef<number | null>(null);
  const sessionRequestIdsRef = useRef<StringMap>({});
  const latestSendAttemptRef = useRef<Record<string, string>>({});
  const installWatchers = useRef<Record<string, number>>({});
  const queuedInstallRef = useRef<Array<{ pack: CapabilityPack; onInstalled?: () => void; sessionId: string }>>([]);
  const activeSessionIdRef = useRef(activeSessionId);
  const messagesRef = useRef(messages);
  const pendingProjectStartRef = useRef<ProjectFolder | null>(pendingProjectStart);
  const sessionProjectsRef = useRef<SessionProjectMap>(sessionProjects);
  const sessionProjectBindingsRef = useRef<SessionProjectBindingMap>(sessionProjectBindings);
  const activeProjectIdRef = useRef(activeProjectId);
  const sessionSwitchSeq = useRef(0);
  const autoScrollRef = useRef(true);
  const appBootMs = useRef(Date.now());
  const completedRequestIds = useRef<StringBoolMap>({});
  const completedRequestCleanupTimers = useRef<Record<string, number>>({});
  const locallyCompletedRequestIdsRef = useRef<StringBoolMap>({});
  const lockedSessionTitlesRef = useRef<StringBoolMap>(lockedSessionTitles);
  const handledSnapshotTerminalRequestsRef = useRef<StringBoolMap>({});
  const postDoneStreamCloseTimers = useRef<Record<string, number>>({});
  const postDoneTailArtifactsRef = useRef<Record<string, AgentArtifact[]>>({});
  const streamRetryCounts = useRef<Record<string, number>>({});
  const streamRequestStates = useRef<Record<string, StreamRequestState>>({});
  const streamStallTimers = useRef<Record<string, number>>({});
  const streamReconnectTimers = useRef<Record<string, number>>({});
  const streamReconnectChecks = useRef<StringBoolMap>({});
  const sendGenerationRef = useRef<Record<string, number>>({});
  const preflightAbortRef = useRef<Record<string, AbortController>>({});
  const phaseTimersRef = useRef<Record<string, number[]>>({});
  const historyRecoveryTimersRef = useRef<Record<string, number[]>>({});
  const blankDraftSessionEpochsRef = useRef<Record<string, number>>({});
  const preloadDone = useRef(false);
  const bootHistoryRefreshDone = useRef(false);
  const runtimeUiStateHydrationStarted = useRef(false);
  const runtimeUiStateHydrated = useRef(false);
  const releaseNotesDismissedVersion = useRef("");
  const uiStateLocalSyncTimer = useRef<number | null>(null);
  const pendingUiStateStorage = useRef<Record<string, SessionUiState> | null>(null);
  const uiStateSyncTimer = useRef<number | null>(null);
  const runtimeSnapshotRefreshTimer = useRef<number | null>(null);
  const sessionUiMessageSyncTimers = useRef<Record<string, number>>({});
  const pendingSessionMessageSnapshots = useRef<Record<string, ChatItem[]>>({});
  const committedSessionMessageSnapshots = useRef<Record<string, ChatItem[]>>(
    Object.fromEntries(
      Object.entries(sessionUiState).map(([sessionId, state]) => [sessionId, state.messages || []])
    ) as Record<string, ChatItem[]>
  );

  function commitComposerDraft(value = composerTextRef.current) {
    if (composerDraftCommitTimer.current) {
      window.clearTimeout(composerDraftCommitTimer.current);
      composerDraftCommitTimer.current = null;
    }
    setComposerTextState(value);
  }

  function scheduleComposerDraftCommit(delay = 220) {
    if (composerDraftCommitTimer.current) {
      window.clearTimeout(composerDraftCommitTimer.current);
    }
    composerDraftCommitTimer.current = window.setTimeout(() => {
      composerDraftCommitTimer.current = null;
      setComposerTextState(composerTextRef.current);
    }, delay);
  }

  function setComposerDraft(next: string | ((current: string) => string), options: { immediate?: boolean; syncDom?: boolean } = {}) {
    const value = typeof next === "function" ? next(composerTextRef.current) : next;
    composerTextRef.current = value;
    setComposerHasText(Boolean(value.trim()));
    const textarea = composerRef.current;
    if (options.syncDom !== false && textarea && textarea.value !== value) {
      textarea.value = value;
    }
    if (options.immediate) {
      commitComposerDraft(value);
    } else {
      scheduleComposerDraftCommit();
    }
    window.requestAnimationFrame(syncComposerHeight);
  }

  function handleComposerDraftInput(value: string) {
    composerTextRef.current = value;
    setComposerHasText(Boolean(value.trim()));
    scheduleComposerDraftCommit();
    window.requestAnimationFrame(syncComposerHeight);
  }

  function beginSessionPreflight(sessionId: string) {
    const generation = (sendGenerationRef.current[sessionId] || 0) + 1;
    sendGenerationRef.current = { ...sendGenerationRef.current, [sessionId]: generation };
    preflightAbortRef.current[sessionId]?.abort();
    const controller = new AbortController();
    preflightAbortRef.current = { ...preflightAbortRef.current, [sessionId]: controller };
    return { generation, controller };
  }

  function isSessionPreflightCurrent(sessionId: string, generation: number, controller: AbortController) {
    return sendGenerationRef.current[sessionId] === generation && !controller.signal.aborted;
  }

  function clearSessionPreflight(sessionId: string, controller: AbortController) {
    if (preflightAbortRef.current[sessionId] !== controller) return;
    const next = { ...preflightAbortRef.current };
    delete next[sessionId];
    preflightAbortRef.current = next;
  }

  function abortSessionPreflight(sessionId: string) {
    sendGenerationRef.current = { ...sendGenerationRef.current, [sessionId]: (sendGenerationRef.current[sessionId] || 0) + 1 };
    preflightAbortRef.current[sessionId]?.abort();
    const next = { ...preflightAbortRef.current };
    delete next[sessionId];
    preflightAbortRef.current = next;
  }

  function scheduleRuntimeSnapshotRefresh(delay = 300) {
    if (runtimeSnapshotRefreshTimer.current) {
      window.clearTimeout(runtimeSnapshotRefreshTimer.current);
    }
    runtimeSnapshotRefreshTimer.current = window.setTimeout(() => {
      runtimeSnapshotRefreshTimer.current = null;
      void loadRuntimeSnapshot().then(setRuntimeSnapshot).catch(() => undefined);
    }, delay);
  }

  function bindSessionToProject(sessionId: string, projectOrBinding: ProjectFolder | ProjectSessionBinding | null, source: ProjectSessionBinding["source"] = "project-session-send") {
    if (!sessionId || !projectOrBinding) return null;
    const binding = "projectPath" in projectOrBinding
      ? { ...projectOrBinding, source, lastUsedAt: new Date().toISOString() }
      : projectBindingFromProject(projectOrBinding, source);
    const projectId = binding.projectId;
    sessionProjectsRef.current = { ...sessionProjectsRef.current, [sessionId]: projectId };
    sessionProjectBindingsRef.current = { ...sessionProjectBindingsRef.current, [sessionId]: binding };
    setSessionProjects((current) => current[sessionId] === projectId ? current : { ...current, [sessionId]: projectId });
    setSessionProjectBindings((current) => ({ ...current, [sessionId]: binding }));
    setSessionUiState((current) => {
      const existing = current[sessionId];
      return {
        ...current,
        [sessionId]: {
          ...(existing || {
            title: sessionTitles[sessionId] || activeSessionTitle,
            messages: sessionId === activeSessionIdRef.current ? messagesRef.current : [],
            composerText: sessionId === activeSessionIdRef.current ? composerTextRef.current : "",
            attachments: sessionId === activeSessionIdRef.current ? attachmentsRef.current : []
          }),
          projectId,
          projectBinding: binding
        }
      };
    });
    return binding;
  }

  function rememberHistoryProjectBinding(sessionId: string, binding?: ProjectSessionBinding | null) {
    if (!sessionId || !binding?.projectId) return;
    bindSessionToProject(sessionId, binding, binding.source || "runtime");
  }

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem("ecorex-theme", theme);
  }, [theme]);

  useEffect(() => {
    writeStorage(PROJECTS_STORAGE_KEY, projects);
  }, [projects]);

  useEffect(() => {
    writeStorage(SESSION_PROJECTS_STORAGE_KEY, sessionProjects);
    sessionProjectsRef.current = sessionProjects;
  }, [sessionProjects]);

  useEffect(() => {
    writeStorage(SESSION_PROJECT_BINDINGS_STORAGE_KEY, sessionProjectBindings);
    sessionProjectBindingsRef.current = sessionProjectBindings;
  }, [sessionProjectBindings]);

  useEffect(() => {
    writeStorage(SESSION_TITLES_STORAGE_KEY, sessionTitles);
  }, [sessionTitles]);

  useEffect(() => {
    writeStorage(LOCKED_SESSION_TITLES_STORAGE_KEY, lockedSessionTitles);
    lockedSessionTitlesRef.current = lockedSessionTitles;
  }, [lockedSessionTitles]);

  useEffect(() => {
    writeStorage(PINNED_SESSIONS_STORAGE_KEY, pinnedSessions);
  }, [pinnedSessions]);

  useEffect(() => {
    setPinnedSessionTimes((current) => {
      let changed = false;
      const next: StringNumberMap = {};
      Object.entries(current).forEach(([sessionId, pinnedAt]) => {
        if (!pinnedSessions[sessionId]) {
          changed = true;
          return;
        }
        const normalized = Number(pinnedAt);
        if (!Number.isFinite(normalized) || normalized <= 0) {
          changed = true;
          return;
        }
        next[sessionId] = normalized;
      });
      return changed ? next : current;
    });
  }, [pinnedSessions]);

  useEffect(() => {
    writeStorage(PINNED_SESSION_TIMES_STORAGE_KEY, pinnedSessionTimes);
  }, [pinnedSessionTimes]);

  useEffect(() => {
    writeStorage(PINNED_PROJECTS_STORAGE_KEY, pinnedProjects);
  }, [pinnedProjects]);

  useEffect(() => {
    writeStorage(UNREAD_SESSIONS_STORAGE_KEY, unreadSessionIds);
  }, [unreadSessionIds]);

  useEffect(() => {
    writeStorage(CAPABILITY_ENABLED_STORAGE_KEY, enabledCapabilityPacks);
  }, [enabledCapabilityPacks]);

  useEffect(() => {
    if (runtimeUiStateHydrationStarted.current) return;
    if (sidecarStatus.state !== "running") return;
    runtimeUiStateHydrationStarted.current = true;
    void loadRuntimeUiState()
      .then((state) => {
        if (!state) return;
        const runtimeProjects = Array.isArray(state.projects) ? mergeProjectFolders([], state.projects) : null;
        const mergedProjects = runtimeProjects ? mergeProjectFolders(projects, runtimeProjects) : projects;
        if (runtimeProjects) {
          setProjects((current) => mergeProjectFolders(current, runtimeProjects));
        }
        if (state.sessionProjects && typeof state.sessionProjects === "object") {
          setSessionProjects((current) => normalizeSessionProjectsForProjects(
            { ...current, ...(state.sessionProjects as Record<string, unknown>) },
            mergedProjects
          ));
        }
        if (state.sessionProjectBindings && typeof state.sessionProjectBindings === "object") {
          setSessionProjectBindings((current) => normalizeProjectSessionBindingsForProjects(
            { ...current, ...(state.sessionProjectBindings as Record<string, unknown>) },
            mergedProjects,
            { ...sessionProjectsRef.current, ...(state.sessionProjects as Record<string, string> || {}) }
          ));
        }
        if (state.sessionTitles && typeof state.sessionTitles === "object") {
          setSessionTitles((current) => ({ ...current, ...(state.sessionTitles as StringMap) }));
        }
        if (state.pinnedSessions && typeof state.pinnedSessions === "object") {
          setPinnedSessions((current) => ({ ...current, ...(state.pinnedSessions as StringBoolMap) }));
        }
        if (state.pinnedSessionTimes && typeof state.pinnedSessionTimes === "object") {
          setPinnedSessionTimes((current) => normalizeStringNumberMap({ ...current, ...(state.pinnedSessionTimes as StringNumberMap) }));
        }
        if (state.pinnedProjects && typeof state.pinnedProjects === "object") {
          setPinnedProjects((current) => normalizePinnedProjectsForProjects(
            { ...current, ...(state.pinnedProjects as Record<string, unknown>) },
            mergedProjects
          ));
        }
        if (state.sessionUiState && typeof state.sessionUiState === "object") {
          setSessionUiState((current) => pruneSessionUiState({
            ...current,
            ...(state.sessionUiState as Record<string, SessionUiState>)
          }));
        }
        if ("activeProjectId" in state) {
          const validProjectIds = new Set(mergedProjects.map((project) => project.id));
          const runtimeActiveProjectId = String(state.activeProjectId || "").trim();
          setActiveProjectId(runtimeActiveProjectId && validProjectIds.has(runtimeActiveProjectId) ? runtimeActiveProjectId : null);
        }
      })
      .finally(() => {
        runtimeUiStateHydrated.current = true;
      })
      .catch(() => undefined);
  }, [sidecarStatus.state]);

  useEffect(() => {
    writeStorage(SIDEBAR_COLLAPSE_STORAGE_KEY, sidebarCollapse);
  }, [sidebarCollapse]);

  useEffect(() => {
    const projectSyncedState = Object.fromEntries(
      Object.entries(sessionUiState).map(([sessionId, state]) => [
        sessionId,
        {
          ...state,
          projectId: sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState),
          projectBinding: sessionProjectBindings[sessionId] || state.projectBinding || null
        }
      ])
    );
    const pruned = pruneSessionUiState(projectSyncedState);
    pendingUiStateStorage.current = pruned;
    if (uiStateLocalSyncTimer.current) {
      window.clearTimeout(uiStateLocalSyncTimer.current);
    }
    const hasLiveState = hasLiveSessionUiState(pruned);
    uiStateLocalSyncTimer.current = window.setTimeout(() => {
      const pending = pendingUiStateStorage.current;
      if (pending) {
        writeStorage(SESSION_UI_STORAGE_KEY, pending);
        pendingUiStateStorage.current = null;
      }
      uiStateLocalSyncTimer.current = null;
    }, hasLiveState ? 1800 : 120);
    if (sidecarStatus.state !== "running" || !runtimeUiStateHydrated.current) return;
    if (uiStateSyncTimer.current) {
      window.clearTimeout(uiStateSyncTimer.current);
    }
    const runtimeActiveProjectId = sessionProjectIdFromState(activeSessionIdRef.current, sessionProjects, sessionUiState);
    uiStateSyncTimer.current = window.setTimeout(() => {
      void saveRuntimeUiState({
        version: 1,
        replaceProjectState: false,
        projectStateMode: "merge",
        lastActiveSessionId: activeSessionIdRef.current,
        activeProjectId: runtimeActiveProjectId,
        projects,
        sessionProjects,
        sessionProjectBindings,
        sessionTitles,
        pinnedSessions,
        pinnedSessionTimes,
        pinnedProjects,
        sessionUiState: pruned,
        savedAt: new Date().toISOString()
      }).catch(() => undefined);
      uiStateSyncTimer.current = null;
    }, hasLiveState ? 2500 : 350);
  }, [sessionUiState, sessionProjects, sessionProjectBindings, sessionTitles, pinnedSessions, pinnedSessionTimes, pinnedProjects, projects, sidecarStatus.state]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
    window.localStorage.setItem(LAST_ACTIVE_SESSION_STORAGE_KEY, activeSessionId);
  }, [activeSessionId]);

  useEffect(() => {
    locallyCompletedRequestIdsRef.current = locallyCompletedRequestIds;
  }, [locallyCompletedRequestIds]);

  useEffect(() => {
    const hasRunningRequests = (runtimeSnapshot.activeRequests || []).some(isPrimaryChatActiveRequest)
      || messagesRef.current.some((message) => Boolean(message.pending && message.runTiming?.startedAtMs));
    if (!hasRunningRequests) return undefined;
    const timer = window.setInterval(() => setRunClockTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [runtimeSnapshot.activeRequests, sessionRequestIds]);

  useEffect(() => {
    activeProjectIdRef.current = activeProjectId;
  }, [activeProjectId]);

  useEffect(() => {
    if (isPendingProjectSessionId(activeSessionId) && pendingProjectStartRef.current) {
      const projectId = pendingProjectStartRef.current.id;
      setActiveProjectId((current) => current === projectId ? current : projectId);
      return;
    }
    const projectId = sessionProjectIdFromState(activeSessionId, sessionProjects, sessionUiState);
    setActiveProjectId((current) => current === projectId ? current : projectId);
  }, [activeSessionId, sessionProjects, sessionUiState, pendingProjectStart]);

  useEffect(() => {
    pendingProjectStartRef.current = pendingProjectStart;
  }, [pendingProjectStart]);

  useEffect(() => {
    sessionRequestIdsRef.current = sessionRequestIds;
  }, [sessionRequestIds]);

  useEffect(() => {
    const nextCommitted: Record<string, ChatItem[]> = { ...committedSessionMessageSnapshots.current };
    Object.entries(sessionUiState).forEach(([sessionId, state]) => {
      if (sessionId === activeSessionIdRef.current || pendingSessionMessageSnapshots.current[sessionId]) return;
      nextCommitted[sessionId] = state.messages || [];
    });
    committedSessionMessageSnapshots.current = nextCommitted;
  }, [sessionUiState]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    const hasLiveMessage = messages.some(isLiveAssistantMessage);
    const timer = window.setTimeout(() => {
      setHistoryContextUsed(estimateContextTokens(messagesRef.current, "", []));
    }, hasLiveMessage ? 900 : 120);
    return () => window.clearTimeout(timer);
  }, [messages]);

  useEffect(() => {
    const notes = runtimeSnapshot.releaseNotes;
    if (runtimeSnapshot.status !== "ready" || !notes?.version) return;
    if (releaseNotesDismissedVersion.current === notes.version) return;
    try {
      if (window.localStorage.getItem(RELEASE_NOTES_SEEN_STORAGE_KEY) === notes.version) return;
    } catch {
      // Showing the notes is still useful when storage is unavailable.
    }
    setReleaseNotesOpen(true);
  }, [runtimeSnapshot.status, runtimeSnapshot.releaseNotes?.version]);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => {
    const projectId = sessionProjects[activeSessionId] || null;
    setSessionUiState((current) => {
      const existing = current[activeSessionId] || {};
      if (
        existing.title === activeSessionTitle
        && existing.projectId === projectId
      ) {
        return current;
      }
      return {
        ...current,
        [activeSessionId]: {
          ...existing,
          title: activeSessionTitle,
          projectId,
          messages: existing.messages || messagesRef.current,
          composerText: existing.composerText ?? composerTextRef.current,
          attachments: existing.attachments || attachmentsRef.current,
          contextStartSeq: existing.contextStartSeq,
          lastActivityAt: existing.lastActivityAt || latestMessageMs(existing.messages) || Date.now()
        }
      };
    });
  }, [activeSessionId, activeSessionTitle, sessionProjects]);

  useEffect(() => {
    if (draftUiStateTimer.current) {
      window.clearTimeout(draftUiStateTimer.current);
    }
    const sessionId = activeSessionId;
    const projectId = sessionProjects[sessionId] || null;
    draftUiStateTimer.current = window.setTimeout(() => {
      draftUiStateTimer.current = null;
      setSessionUiState((current) => {
        const existing = current[sessionId] || {};
        if (
          existing.composerText === composerText
          && sameAttachments(existing.attachments || [], attachments)
          && existing.title === activeSessionTitle
          && existing.projectId === projectId
        ) {
          return current;
        }
        return {
          ...current,
          [sessionId]: {
            ...existing,
            title: activeSessionTitle,
            projectId,
            messages: existing.messages || (sessionId === activeSessionIdRef.current ? messagesRef.current : []),
            composerText,
            attachments,
            lastActivityAt: existing.lastActivityAt || latestMessageMs(existing.messages) || Date.now()
          }
        };
      });
    }, 320);
    return () => {
      if (draftUiStateTimer.current) {
        window.clearTimeout(draftUiStateTimer.current);
        draftUiStateTimer.current = null;
      }
    };
  }, [activeSessionId, activeSessionTitle, sessionProjects, composerText, attachments]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(syncComposerHeight);
    return () => window.cancelAnimationFrame(frame);
  }, [composerText, activeSessionId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (autoScrollRef.current) {
        scrollToLatest(false);
      } else {
        updateJumpLatestState();
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, activeSessionId]);

  useEffect(() => {
    if (sidecarStatus.state !== "running") return;
    const liveMessage = messages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (!liveMessage?.requestId) return;
    attachMessageStream(activeSessionId, liveMessage.id, liveMessage.requestId);
  }, [sidecarStatus.state, sidecarStatus.webPort, activeSessionId, messages]);

  useEffect(() => {
    if (bootHistoryRefreshDone.current) return;
    if (sidecarStatus.state !== "running") return;
    if (!bootSession?.id) return;
    const liveMessage = messages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (liveMessage?.requestId) return;
    bootHistoryRefreshDone.current = true;
    void refreshSessionFromHistory(bootSession.id);
  }, [sidecarStatus.state, bootSession?.id, messages]);

  useEffect(() => {
    getEnterpriseSession()
      .then((existing) => setSession(existing))
      .catch(() => setSession(null))
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    const unsubscribe = window.ecorexDesktop?.onSidecarStatus?.((status) => setSidecarStatus(status));
    window.ecorexDesktop?.getSidecarStatus?.().then((status) => setSidecarStatus(status)).catch(() => undefined);
    return () => {
      streamCleanup.current?.();
      Object.values(streamCleanups.current).forEach((cleanup) => cleanup());
      streamCleanup.current = null;
      streamCleanups.current = {};
      streamCleanupRequestIds.current = {};
      Object.values(streamDeltaBuffers.current).forEach((buffer) => {
        if (buffer.timer !== null) window.clearTimeout(buffer.timer);
      });
      streamDeltaBuffers.current = {};
      Object.values(postDoneStreamCloseTimers.current).forEach((timer) => window.clearTimeout(timer));
      postDoneStreamCloseTimers.current = {};
      Object.values(streamReconnectTimers.current).forEach((timer) => window.clearTimeout(timer));
      streamReconnectTimers.current = {};
      streamReconnectChecks.current = {};
      Object.values(completedRequestCleanupTimers.current).forEach((timer) => window.clearTimeout(timer));
      completedRequestCleanupTimers.current = {};
      Object.values(historyRecoveryTimersRef.current).forEach((timers) => {
        timers.forEach((timer) => window.clearTimeout(timer));
      });
      historyRecoveryTimersRef.current = {};
      Object.values(installWatchers.current).forEach((timer) => window.clearInterval(timer));
      installWatchers.current = {};
      if (uiStateLocalSyncTimer.current) {
        window.clearTimeout(uiStateLocalSyncTimer.current);
        uiStateLocalSyncTimer.current = null;
      }
      if (pendingUiStateStorage.current) {
        writeStorage(SESSION_UI_STORAGE_KEY, pendingUiStateStorage.current);
        pendingUiStateStorage.current = null;
      }
      if (uiStateSyncTimer.current) {
        window.clearTimeout(uiStateSyncTimer.current);
        uiStateSyncTimer.current = null;
      }
      if (composerDraftCommitTimer.current) {
        window.clearTimeout(composerDraftCommitTimer.current);
        composerDraftCommitTimer.current = null;
      }
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (preloadDone.current) return;
    if (sidecarStatus.state !== "running") return;
    preloadDone.current = true;
    void (async () => {
      const nextPacks = await listCapabilityPacks().catch(() => []);
      setPacks(nextPacks);
      const nextConnections = await loadExternalConnections().catch(() => null);
      if (nextConnections?.connections) {
        setExternalConnections(nextConnections.connections);
      }
      const snapshot = await loadRuntimeSnapshot().catch(() => null);
      if (snapshot) {
        let nextSnapshot = snapshot;
        if (!window.localStorage.getItem(SKILL_DEFAULTS_STORAGE_KEY)) {
          const changed = await enableDefaultSkills(snapshot.skills || []).catch(() => 0);
          window.localStorage.setItem(SKILL_DEFAULTS_STORAGE_KEY, "1");
          if (changed) {
            nextSnapshot = await loadRuntimeSnapshot().catch(() => snapshot);
          }
        }
        setRuntimeSnapshot(nextSnapshot);
      }
    })();
  }, [sidecarStatus.state]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    async function refresh() {
      const [snapshot, nextPacks, nextConnections, nextPermissions, nextMemoryFiles, nextDreamFiles, quota] = await Promise.all([
        loadRuntimeSnapshot(),
        listCapabilityPacks(),
        loadExternalConnections().catch(() => ({ connections: [] })),
        loadPermissionState(),
        loadMemoryFiles("memory"),
        loadMemoryFiles("dream"),
        checkEnterpriseQuota(0).catch(() => null)
      ]);
      if (!cancelled) {
        setRuntimeSnapshot(snapshot);
        setPacks(nextPacks);
        setExternalConnections(nextConnections.connections || []);
        setPermissionState(nextPermissions);
        setMemoryFiles(nextMemoryFiles);
        setDreamFiles(nextDreamFiles);
        if (quota?.quota) setQuotaSnapshot(quota.quota);
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [session]);

  useEffect(() => {
    if (!packs.length) return;
    const terminal = new Set(
      packs
        .filter((pack) => pack.installed || pack.state === "failed")
        .map((pack) => pack.id)
    );
    if (!terminal.size) return;
    setInstallingPackIds((current) => {
      let changed = false;
      const next = { ...current };
      for (const id of terminal) {
        if (next[id]) {
          delete next[id];
          changed = true;
        }
      }
      return changed ? next : current;
    });
    setInstallNotice((current) => {
      if (!current?.packId || current.dismissed || !terminal.has(current.packId)) return current;
      return null;
    });
  }, [packs]);

  useEffect(() => {
    if (!chatFileMenu && !projectMenu) return undefined;
    const close = () => {
      setChatFileMenu(null);
      setProjectMenu(null);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
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
  }, [chatFileMenu, projectMenu]);

  useEffect(() => {
    if (!projectMenu && !chatFileMenu) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      if (target && projectMenuRef.current?.contains(target)) return;
      if (target && chatFileMenuRef.current?.contains(target)) return;
      setProjectMenu(null);
      setChatFileMenu(null);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [projectMenu, chatFileMenu]);

  useEffect(() => {
    if (!projectStartMenuOpen) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      if (target && projectStartMenuRef.current?.contains(target)) return;
      setProjectStartMenuOpen(false);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setProjectStartMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [projectStartMenuOpen]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    if (runtimeSnapshot.activeRequestsStatus === "unavailable") return;
    const activeRequestIds = new Set(
      (runtimeSnapshot.activeRequests || [])
        .filter(isPrimaryChatActiveRequest)
        .map((request) => request.request_id ? String(request.request_id) : "")
        .filter((requestId) => !locallyCompletedRequestIds[requestId])
        .filter(Boolean)
    );
    const legacyStaleSessionIds = new Set(
      (runtimeSnapshot.staleLocks || [])
        .filter((lock) => lock.removed || lock.dead_owner || lock.deadOwner || lock.stale)
        .map((lock) => lock.session_id ? String(lock.session_id) : "")
        .filter(Boolean)
    );
    const nowMs = Date.now();
    const bootMs = appBootMs.current;
    const nextState: Record<string, SessionUiState> = {};
    const settledSessionIds = new Set<string>();
    let changed = false;
    for (const [sessionId, state] of Object.entries(sessionUiState)) {
      const normalized = normalizePausedMessages(state.messages || [], {
        sessionId,
        activeRequestIds,
        staleSessionIds: legacyStaleSessionIds,
        nowMs,
        inactiveRequestGraceMs: (state.messages || []).some((message) => (
          message.pending
          && message.requestId
          && message.createdAt
          && new Date(message.createdAt).getTime() < bootMs
        )) ? 2_000 : 45_000
      });
      if (normalized !== state.messages) {
        changed = true;
        settledSessionIds.add(sessionId);
        nextState[sessionId] = { ...state, messages: normalized };
      } else {
        nextState[sessionId] = state;
      }
    }
    if (!changed) return;
    setSessionUiState(nextState);
    const activeId = activeSessionIdRef.current;
    if (activeId && settledSessionIds.has(activeId)) {
      const activeState = nextState[activeId];
      if (activeState) setMessages(activeState.messages);
    }
    settledSessionIds.forEach((sessionId) => {
      finishSessionRequest(sessionId);
      void refreshSessionFromHistory(sessionId);
    });
  }, [runtimeSnapshot, sessionUiState, locallyCompletedRequestIds]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    (runtimeSnapshot.recentTerminalRequests || [])
      .filter(isPrimaryChatTerminalRequest)
      .forEach((request) => settleTerminalSnapshotRequest(request));
  }, [runtimeSnapshot.status, runtimeSnapshot.recentTerminalRequests, sessionUiState]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    const lockedFromRuntime: StringBoolMap = {};
    (runtimeSnapshot.sessions || []).forEach((session, index) => {
      const id = session.session_id || session.id || `runtime-${index}`;
      if (!id) return;
      if (session.title_locked || session.titleLocked) lockedFromRuntime[id] = true;
    });
    if (Object.keys(lockedFromRuntime).length === 0) return;
    setLockedSessionTitles((current) => {
      const changed = Object.keys(lockedFromRuntime).some((sessionId) => !current[sessionId]);
      return changed ? { ...current, ...lockedFromRuntime } : current;
    });
  }, [runtimeSnapshot.status, runtimeSnapshot.sessions]);

  const projectCatalog = useMemo(() => {
    const merged = new Map(projects.map((project) => [project.id, project]));
    Object.values(sessionProjectBindings)
      .map((binding) => binding?.projectId && binding.projectPath ? projectFolderFromBinding(binding) : null)
      .filter(Boolean)
      .forEach((project) => {
        if (project && !merged.has(project.id)) merged.set(project.id, project);
      });
    return Array.from(merged.values());
  }, [projects, sessionProjectBindings]);

  const allSessions = useMemo(() => (
    mapSessions(runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionProjectBindings, sessionTitles, pinnedSessions, pinnedSessionTimes, projectCatalog, sessionUiState, locallyCompletedRequestIds, runClockTick)
  ), [runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionProjectBindings, sessionTitles, pinnedSessions, pinnedSessionTimes, projectCatalog, sessionUiState, locallyCompletedRequestIds, runClockTick]);
  const visibleSessions = useMemo(() => {
    const needle = searchQuery.trim().toLowerCase();
    return needle ? allSessions.filter((row) => `${row.title} ${row.detail}`.toLowerCase().includes(needle)) : allSessions;
  }, [allSessions, searchQuery]);
  const runCenterRequests = useMemo(() => {
    const seen = new Set<string>();
    return [
      ...(runtimeSnapshot.activeRequests || []),
      ...(runtimeSnapshot.recentTerminalRequests || [])
    ].filter((request) => {
      if (!isRunCenterVisibleRequest(request)) return false;
      const key = String(request.request_id || `${request.session_id || ""}-${request.run_type || request.source || ""}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [runtimeSnapshot.activeRequests, runtimeSnapshot.recentTerminalRequests]);
  const runCenterStaleLocks = useMemo(() => (
    (runtimeSnapshot.staleLocks || [])
      .filter((lock) => lock.removed || lock.dead_owner || lock.deadOwner || lock.stale)
  ), [runtimeSnapshot.staleLocks]);
  const runCenterStats = useMemo(() => {
    const cancelling = runCenterRequests.filter((request) => ["cancelling", "cancelled"].includes(runCenterState(request))).length;
    const failed = runCenterRequests.filter(isRunCenterFailedRequest).length;
    return {
      running: runCenterRequests.length - cancelling - failed,
      cancelling,
      failed,
      stale: runCenterStaleLocks.length
    };
  }, [runCenterRequests, runCenterStaleLocks]);
  const runCenterNavCount = runCenterRequests.length + runCenterStaleLocks.length;
  const runCenterDevVisible = useMemo(() => {
    try {
      const env = import.meta.env as { DEV?: boolean; VITE_ECOREX_RUN_CENTER?: string };
      if (!env.DEV && env.VITE_ECOREX_RUN_CENTER !== "1") return false;
      const params = new URLSearchParams(window.location.search || "");
      return params.get("ecorexRunCenter") === "1" && window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  }, []);

  const activeProject = useMemo(
    () => pendingProjectStart || projectCatalog.find((project) => project.id === activeProjectId) || null,
    [pendingProjectStart, projectCatalog, activeProjectId]
  );

  const projectPathForSession = (sessionId: string) => {
    const binding = projectBindingForSession(sessionId, sessionProjectBindings, sessionProjects, sessionUiState, projectCatalog);
    if (binding?.projectPath) return binding.projectPath;
    const projectId = sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState);
    if (!projectId) return "";
    return projectCatalog.find((project) => project.id === projectId)?.path || "";
  };

  const resolveArtifactPathForSession = (sessionId: string, filePath: string) => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || isRuntimePreviewPath(raw) || isLocalAbsolutePath(raw) || /^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw;
    const projectPath = projectPathForSession(sessionId);
    return projectPath ? joinLocalPath(projectPath, raw) : raw;
  };

  const resolveArtifactPath = (filePath: string) => {
    return resolveArtifactPathForSession(activeSessionIdRef.current, filePath);
  };
  const statArtifactPath = async (filePath: string, sessionId = activeSessionIdRef.current): Promise<LocalPathStat> => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || isRuntimePreviewPath(raw) || /^https?:\/\//i.test(raw)) {
      return { path: raw, exists: false, isFile: false, isDirectory: false, status: raw ? "remote" : "error" };
    }
    const resolvedPath = resolveArtifactPathForSession(sessionId, raw);
    return statLocalPath(resolvedPath);
  };
  const readArtifactStatusJson = async (filePath: string, sessionId = activeSessionIdRef.current): Promise<LocalJsonResult> => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || /^https?:\/\//i.test(raw)) {
      return { path: raw, status: "error", message: raw ? "remote status JSON is not supported" : "path is required" };
    }
    const resolvedPath = resolveArtifactPathForSession(sessionId, raw);
    return readLocalJson(resolvedPath);
  };
  const attachmentPreviewUrl = (file: FileAttachment) => {
    if (file.previewDataUrl) return file.previewDataUrl;
    if (file.preview_url) return filePreviewUrl(file.preview_url, sidecarStatus.webPort);
    if (!isImageAttachment(file)) return "";
    return filePreviewUrl(file.file_path, sidecarStatus.webPort);
  };
  const sortedProjects = useMemo(() => {
    const priority = (project: ProjectFolder) => (
      Number(Boolean(pinnedProjects[project.id] || project.pinned)) * 100
      + Number(project.id === activeProjectIdRef.current) * 10
      + Number(project.id === activeProjectId)
    );
    return [...projectCatalog].sort((a, b) => priority(b) - priority(a) || a.name.localeCompare(b.name));
  }, [projectCatalog, pinnedProjects, activeProjectId]);
  const projectStartMatches = useMemo(() => {
    const needle = projectStartSearch.trim().toLowerCase();
    const matches = needle
      ? sortedProjects.filter((project) => `${project.name} ${project.path}`.toLowerCase().includes(needle))
      : sortedProjects;
    return matches.slice(0, 8);
  }, [projectStartSearch, sortedProjects]);
  const { projectSessions, generalSessions, pinnedGeneralSessions, regularGeneralSessions, projectSessionGroups } = useMemo(() => {
    const grouped = new Map<string, SessionRow[]>();
    const general: SessionRow[] = [];
    const projectRows: SessionRow[] = [];
    for (const row of visibleSessions) {
      if (!row.projectId) {
        general.push(row);
        continue;
      }
      projectRows.push(row);
      const rows = grouped.get(row.projectId) || [];
      rows.push(row);
      grouped.set(row.projectId, rows);
    }
    return {
      projectSessions: projectRows,
      generalSessions: general,
      pinnedGeneralSessions: general.filter((row) => row.pinned),
      regularGeneralSessions: general.filter((row) => !row.pinned),
      projectSessionGroups: sortedProjects.map((project) => ({
        project,
        sessions: grouped.get(project.id) || []
      }))
    };
  }, [visibleSessions, sortedProjects]);
  const selectOrCreateProjectSession = (project: ProjectFolder) => {
    startNewSession(project);
  };
  const currentModelName = displayModelName(runtimeSnapshot.currentModel);
  const activeRuntimeRequest = useMemo(() => (
    (runtimeSnapshot.activeRequests || []).find((request) => (
      isPrimaryChatActiveRequest(request)
      && String(request.session_id || "") === activeSessionId
      && !locallyCompletedRequestIds[String(request.request_id || "")]
    )) || null
  ), [runtimeSnapshot.activeRequests, activeSessionId, locallyCompletedRequestIds]);
  const activeRuntimeElapsed = runtimeRequestElapsedLabel(activeRuntimeRequest, runClockTick);
  const appVersion = runtimeSnapshot.version || runtimeSnapshot.releaseNotes?.version || "0.2.2";
  const deferredComposerText = useDeferredValue(composerText);
  const skillDisplayRows = useMemo(() => buildSkillDisplayRows(runtimeSnapshot), [runtimeSnapshot]);
  const mentionableSkillRows = useMemo(() => skillDisplayRows.filter((skill) => skill.mentionable), [skillDisplayRows]);
  const backgroundSkillRows = useMemo(() => skillDisplayRows.filter((skill) => !skill.mentionable), [skillDisplayRows]);
  const skillSourceSections = useMemo(() => (
    SKILL_SOURCE_ORDER.map((sourceGroup) => {
      const sourceRows = skillDisplayRows.filter((skill) => skill.sourceGroup === sourceGroup);
      return {
        sourceGroup,
        label: SKILL_SOURCE_LABELS[sourceGroup],
        count: sourceRows.length,
        purposeGroups: SKILL_PURPOSE_ORDER.map((purposeGroup) => ({
          purposeGroup,
          label: SKILL_PURPOSE_LABELS[purposeGroup],
          items: sourceRows.filter((skill) => skill.purposeGroup === purposeGroup)
        })).filter((group) => group.items.length > 0)
      };
    }).filter((section) => section.count > 0)
  ), [skillDisplayRows]);
  const skillMentionCandidates = useMemo(
    () => mentionableSkillRows.filter((skill) => skill.enabled && skill.installed),
    [mentionableSkillRows]
  );
  const mentionMatch = /@([\w\u4e00-\u9fa5-]*)$/.exec(deferredComposerText);
  const skillMentionNeedle = mentionMatch ? mentionMatch[1].toLowerCase() : "";
  const skillMatchesNeedle = (skill: SkillDisplayRow) => {
    const haystack = [
      skill.displayName,
      skill.name,
      skill.description,
      skill.source,
      skill.path,
      skill.categoryLabel
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(skillMentionNeedle);
  };
  const skillMentions = mentionMatch
    ? skillMentionCandidates.filter(skillMatchesNeedle)
    : [];
  const hiddenSkillMentions = mentionMatch && skillMentionNeedle
    ? backgroundSkillRows.filter(skillMatchesNeedle)
    : [];
  const skillMentionGroups = SKILL_CATEGORY_ORDER
    .map((category) => ({
      category,
      label: SKILL_CATEGORY_LABELS[category],
      items: skillMentions.filter((skill) => skill.category === category)
    }))
    .filter((group) => group.items.length > 0);
  const skillMentionNoResults = Boolean(mentionMatch && mentionMatch[1] && !skillMentions.length);
  const activeSessionRequestId = sessionRequestIds[activeSessionId] || "";
  const dailyUsed = quotaNumber(quotaSnapshot, "dailyUsed");
  const weeklyUsed = quotaNumber(quotaSnapshot, "weeklyUsed");
  const dailyLimit = quotaNumber(quotaSnapshot, "dailyLimit");
  const weeklyLimit = quotaNumber(quotaSnapshot, "weeklyLimit");
  const draftContextUsed = useMemo(() => estimateTokenCount(deferredComposerText, attachments), [deferredComposerText, attachments]);
  const contextUsed = historyContextUsed + draftContextUsed;
  const contextPercent = percentOf(contextUsed, CONTEXT_THRESHOLD_TOKENS);
  const tokenMeters = [
    {
      key: "daily",
      label: "今日",
      percent: percentOf(dailyUsed, dailyLimit),
      title: meterTitle("今日 token 用量", dailyUsed, dailyLimit)
    },
    {
      key: "weekly",
      label: "本周",
      percent: percentOf(weeklyUsed, weeklyLimit),
      title: meterTitle("本周 token 用量", weeklyUsed, weeklyLimit)
    }
  ];
  const contextMeter = {
    key: "context",
    label: "上下文",
    percent: contextPercent,
    title: meterTitle("当前会话上下文估算", contextUsed, CONTEXT_THRESHOLD_TOKENS)
  };

  useEffect(() => {
    const queue = queuedInstallRef.current;
    if (!queue.length) return;
    const nextIndex = queue.findIndex((item) => {
      if (sessionRequestIds[item.sessionId]) return false;
      const sessionMessages = item.sessionId === activeSessionId
        ? messages
        : sessionUiState[item.sessionId]?.messages || [];
      return !sessionMessages.some(isUiLiveAssistantMessage);
    });
    if (nextIndex < 0) return;
    const [queued] = queue.splice(nextIndex, 1);
    window.setTimeout(() => void handleInstallPack(queued.pack, queued.onInstalled, queued.sessionId), 0);
  }, [activeSessionId, activeSessionRequestId, messages, sessionRequestIds, sessionUiState]);

  useEffect(() => {
    setPreviewZoom(1);
  }, [previewFile?.file_path]);

  useEffect(() => {
    if (!composerDragActive) return;
    const reset = () => clearComposerDragState();
    window.addEventListener("dragend", reset);
    window.addEventListener("blur", reset);
    return () => {
      window.removeEventListener("dragend", reset);
      window.removeEventListener("blur", reset);
    };
  }, [composerDragActive]);

  useEffect(() => {
    const preventFileDropNavigation = (event: globalThis.DragEvent) => {
      const types = Array.from(event.dataTransfer?.types || []);
      if (!types.includes("Files")) return;
      event.preventDefault();
      if (event.type === "drop") {
        clearComposerDragState();
      }
    };
    window.addEventListener("dragover", preventFileDropNavigation);
    window.addEventListener("drop", preventFileDropNavigation);
    return () => {
      window.removeEventListener("dragover", preventFileDropNavigation);
      window.removeEventListener("drop", preventFileDropNavigation);
    };
  }, []);

  function capabilityPackEnabled(packId: string) {
    const pack = packs.find((item) => item.id === packId);
    if (pack && isDefaultReadOnlyCapability(pack)) return true;
    return enabledCapabilityPacks[packId] !== false;
  }

  function toggleCapabilityPack(pack: CapabilityPack, enabled: boolean) {
    if (isDefaultReadOnlyCapability(pack) && !enabled) {
      setToast(`${pack.name} 是默认只读能力，不能关闭`);
      return;
    }
    setEnabledCapabilityPacks((current) => ({ ...current, [pack.id]: enabled }));
    setToast(enabled ? `${pack.name} 已启用` : `${pack.name} 已关闭`);
  }

  async function toggleRuntimeSkill(skill: Pick<SkillDisplayRow, "name" | "displayName">, enabled: boolean) {
    const name = skill.name || "";
    if (!name) return;
    try {
      await setSkillEnabled(name, enabled);
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast(enabled ? `${skill.displayName || name} 已启用` : `${skill.displayName || name} 已关闭`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Skill 开关失败");
    }
  }

  function insertSkillMention(skill: Pick<SkillDisplayRow, "name" | "displayName">) {
    const label = skill.displayName || skill.name || "";
    if (!label) return;
    setComposerDraft((current) => current.replace(/@([\w\u4e00-\u9fa5-]*)$/, `@${label} `), { immediate: true });
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  function syncComposerHeight() {
    const textarea = composerRef.current;
    if (!textarea) return;
    const maxHeight = Number.parseFloat(window.getComputedStyle(textarea).maxHeight) || 168;
    textarea.style.height = "auto";
    const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
  }

  function updateJumpLatestState() {
    const list = messageListRef.current;
    if (!list) return;
    const state = getChatScrollState(list, CHAT_SCROLL_THRESHOLD_PX);
    autoScrollRef.current = state.autoScrollEnabled;
    setShowJumpLatest(state.showJumpLatest);
  }

  function scrollToLatest(forceAuto = true) {
    const list = messageListRef.current;
    if (!list) return;
    if (forceAuto) autoScrollRef.current = true;
    if (forceAuto) {
      const targetTop = Math.max(0, list.scrollHeight - list.clientHeight);
      list.scrollTo({ top: targetTop, behavior: "smooth" });
    } else {
      scrollElementToBottom(list, "auto");
    }
    setShowJumpLatest(false);
  }

  function focusComposerSoon() {
    const focus = () => {
      const textarea = composerRef.current;
      if (!textarea) return;
      textarea.focus({ preventScroll: true });
      const cursor = textarea.value.length;
      try {
        textarea.setSelectionRange(cursor, cursor);
      } catch {
        // IME/composition can briefly reject selection updates; focus is still useful.
      }
      syncComposerHeight();
    };
    focus();
    window.requestAnimationFrame(focus);
    window.requestAnimationFrame(() => window.requestAnimationFrame(focus));
    [40, 120, 300, 700].forEach((delay) => window.setTimeout(focus, delay));
  }

  function insertComposerNewline(textarea: HTMLTextAreaElement) {
    const start = textarea.selectionStart ?? composerTextRef.current.length;
    const end = textarea.selectionEnd ?? start;
    const value = textarea.value;
    const next = `${value.slice(0, start)}\n${value.slice(end)}`;
    const nextCursor = start + 1;
    setComposerDraft(next, { immediate: true });
    window.requestAnimationFrame(() => {
      const current = composerRef.current;
      if (!current) return;
      current.focus({ preventScroll: true });
      current.setSelectionRange(nextCursor, nextCursor);
      syncComposerHeight();
    });
  }

  function resumeRuntimeRequest(sessionId: string, requestId?: string, streamAvailable = true) {
    if (!requestId) return;
    const cachedMessages = sessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const existing = cachedMessages.find((message) => message.role === "assistant" && message.requestId === requestId);
    if (completedRequestIds.current[requestId] || locallyCompletedRequestIdsRef.current[requestId] || isTerminalAssistantMessage(existing)) {
      markRequestLocallyCompleted(requestId);
      clearSessionRequestState(sessionId, requestId);
      return;
    }
    const assistantId = existing?.id || `a-resume-${requestId}`;
    updateSessionMessages(sessionId, (current) => {
      const hasExisting = current.some((message) => message.id === assistantId || message.requestId === requestId);
      if (hasExisting) {
        return current.map((message) => (
          message.id === assistantId || message.requestId === requestId
            ? {
                ...message,
                id: message.id || assistantId,
                requestId,
                pending: true,
                paused: false,
                cancelled: false
              }
            : message
        ));
      }
      return [
        ...current,
        {
          id: assistantId,
          role: "assistant",
          content: "",
          pending: true,
          requestId,
          createdAt: new Date().toISOString(),
          steps: [{ type: "phase", content: "正在连接后台任务" }]
        }
      ];
    });
    sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [sessionId]: requestId };
    setSessionRequestIds((current) => ({ ...current, [sessionId]: requestId }));
    streamRetryCounts.current[sessionId] = 0;
    if (streamAvailable) {
      window.setTimeout(() => attachMessageStream(sessionId, assistantId, requestId), 0);
      scheduleHistoryRecovery(sessionId, requestId);
    } else {
      void refreshSessionFromHistory(sessionId).then((restored) => {
        if (!restored) {
          window.setTimeout(() => void refreshSessionFromHistory(sessionId), 3000);
        }
      });
    }
  }

  function restoreCachedSession(sessionId: string, activeRequestId?: string, streamAvailable = true) {
    const cached = sessionUiState[sessionId];
    if (!cached) return false;
    const binding = projectBindingForSession(sessionId, sessionProjectBindings, sessionProjects, sessionUiState, projectCatalog);
    const projectId = binding?.projectId || null;
    const nextMessages = normalizePausedMessages(cached.messages, {
      sessionId,
      activeRequestIds: activeRequestId ? new Set([activeRequestId]) : new Set(),
      inactiveRequestGraceMs: 45_000
    });
    messagesRef.current = nextMessages;
    setMessages(nextMessages);
    setComposerDraft(cached.composerText || "", { immediate: true });
    setAttachments(cached.attachments);
    setActiveSessionTitle(sessionTitles[sessionId] || cached.title || NEW_SESSION_START_TITLE);
    setActiveProjectId(projectId);
    setSessionUiState((current) => ({
      ...current,
      [sessionId]: {
        ...cached,
        projectId,
        projectBinding: binding,
        messages: nextMessages
      }
    }));
    const liveMessage = nextMessages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (liveMessage?.requestId && streamAvailable && (!activeRequestId || liveMessage.requestId === activeRequestId)) {
      setSessionRequestIds((current) => ({ ...current, [sessionId]: liveMessage.requestId || "" }));
      window.setTimeout(() => attachMessageStream(sessionId, liveMessage.id, liveMessage.requestId || ""), 0);
    } else if (activeRequestId) {
      resumeRuntimeRequest(sessionId, activeRequestId, streamAvailable);
    } else {
      void refreshSessionFromHistory(sessionId);
    }
    return true;
  }

  function flushActiveSessionDraft() {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    flushPendingSessionMessagesSnapshot(sessionId);
    const binding = projectBindingForSession(sessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, sessionUiState, projectCatalog);
    const projectId = binding?.projectId || null;
    const draftText = composerTextRef.current;
    const draftAttachments = attachmentsRef.current;
    const currentMessages = messagesRef.current;
    setSessionUiState((current) => {
      const existing = current[sessionId] || {};
      const lastActivityAt = latestMessageMs(currentMessages) || existing.lastActivityAt || Date.now();
      if (
        existing.title === activeSessionTitle
        && existing.projectId === projectId
        && existing.projectBinding === binding
        && existing.messages === currentMessages
        && existing.composerText === draftText
        && sameAttachments(existing.attachments || [], draftAttachments)
        && existing.lastActivityAt === lastActivityAt
      ) {
        return current;
      }
      return {
        ...current,
        [sessionId]: {
          ...existing,
          title: activeSessionTitle,
          projectId,
          projectBinding: binding,
          messages: currentMessages,
          composerText: draftText,
          attachments: draftAttachments,
          lastActivityAt
        }
      };
    });
  }

  async function selectSession(row: SessionRow) {
    if (row.id === activeSessionIdRef.current) {
      clearSessionUnread(row.id);
      focusComposerSoon();
      return;
    }
    flushActiveSessionDraft();
    const switchSeq = ++sessionSwitchSeq.current;
    pendingProjectStartRef.current = null;
    setPendingProjectStart(null);
    const nextBinding = projectBindingForSession(row.id, sessionProjectBindings, sessionProjects, sessionUiState, projectCatalog);
    const nextProjectId = nextBinding?.projectId || null;
    autoScrollRef.current = true;
    setShowJumpLatest(false);
    activeSessionIdRef.current = row.id;
    setActiveSessionId(row.id);
    setActiveSessionTitle(row.title);
    setActiveProjectId(nextProjectId);
    setPreviewFile(null);
    setApproval(null);
    clearSessionUnread(row.id);
    if (restoreCachedSession(row.id, row.requestId, row.streamAvailable !== false)) {
      focusComposerSoon();
      return;
    }
    updateSessionMessages(row.id, () => []);
    setSessionUiState((current) => ({
      ...current,
      [row.id]: {
        title: row.title,
        projectId: nextProjectId,
        projectBinding: nextBinding,
        messages: [],
        composerText: "",
        attachments: []
      }
    }));
    setMessages([]);
    setComposerDraft("", { immediate: true });
    setAttachments([]);
    focusComposerSoon();
    try {
      const history = await loadSessionHistoryWithMeta(row.id);
      rememberHistoryProjectBinding(row.id, history.projectContext);
      if (sessionSwitchSeq.current !== switchSeq || activeSessionIdRef.current !== row.id) {
        return;
      }
      const historyBinding = history.projectContext || nextBinding;
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, row.id, history.contextStartSeq));
      setSessionUiState((current) => ({
        ...current,
        [row.id]: {
          ...(current[row.id] || {
            title: row.title,
            composerText: "",
            attachments: []
          }),
          projectId: historyBinding?.projectId || nextProjectId,
          projectBinding: historyBinding,
          messages: mapped,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(mapped) || current[row.id]?.lastActivityAt || row.activityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === row.id) {
        setMessages(mapped);
        focusComposerSoon();
      }
      if (row.requestId) {
        resumeRuntimeRequest(row.id, row.requestId, row.streamAvailable !== false);
      }
    } catch (error) {
      setToast(error instanceof Error ? error.message : "加载会话失败");
    }
  }

  function startNewSession(project?: ProjectFolder | null, options?: { skipFlush?: boolean }) {
    if (!options?.skipFlush) {
      flushActiveSessionDraft();
    }
    setProjectStartMenuOpen(false);
    sessionSwitchSeq.current += 1;
    const id = createDraftSessionId(project);
    const title = project ? `${project.name} · ${NEW_SESSION_START_TITLE}` : NEW_SESSION_START_TITLE;
    const projectBinding = project ? projectBindingFromProject(project, "project-new-session") : null;
    const emptyMessages: ChatItem[] = [];
    protectBlankDraftSession(id);
    activeSessionIdRef.current = id;
    messagesRef.current = emptyMessages;
    committedSessionMessageSnapshots.current[id] = emptyMessages;
    pendingSessionMessageSnapshots.current[id] = emptyMessages;
    setActiveSessionId(id);
    setActiveProjectId(project?.id || null);
    pendingProjectStartRef.current = project || null;
    setPendingProjectStart(project || null);
    setActiveSessionTitle(title);
    if (!project) setSessionTitles((current) => ({ ...current, [id]: title }));
    setMessages(emptyMessages);
    setAttachments([]);
    attachmentsRef.current = [];
    setComposerDraft("", { immediate: true });
    setApproval(null);
    setPreviewFile(null);
    setShowJumpLatest(false);
    setActiveRequestId("");
    const nextSessionRequestIds = { ...sessionRequestIdsRef.current };
    delete nextSessionRequestIds[id];
    sessionRequestIdsRef.current = nextSessionRequestIds;
    setSessionRequestIds((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    setSessionTitles((current) => ({ ...current, [id]: title }));
    if (project && projectBinding) {
      sessionProjectsRef.current = { ...sessionProjectsRef.current, [id]: projectBinding.projectId };
      sessionProjectBindingsRef.current = { ...sessionProjectBindingsRef.current, [id]: projectBinding };
      setSessionProjects((current) => ({ ...current, [id]: projectBinding.projectId }));
      setSessionProjectBindings((current) => ({ ...current, [id]: projectBinding }));
    }
    setSessionUiState((current) => ({
      ...current,
      [id]: {
        title,
        projectId: projectBinding?.projectId || null,
        projectBinding,
        messages: [],
        composerText: "",
        attachments: []
      }
    }));
    focusComposerSoon();
  }

  function startProjectFromWelcome(project: ProjectFolder) {
    setProjectStartMenuOpen(false);
    startNewSession(project);
  }

  async function renameSession(row: SessionRow) {
    const nextTitle = window.prompt("重命名会话", row.title)?.trim();
    if (!nextTitle) return;
    if (nextTitle === row.title) {
      lockedSessionTitlesRef.current = { ...lockedSessionTitlesRef.current, [row.id]: true };
      setLockedSessionTitles((current) => ({ ...current, [row.id]: true }));
      setToast("会话标题已锁定");
      return;
    }
    try {
      setSessionTitles((current) => ({ ...current, [row.id]: nextTitle }));
      lockedSessionTitlesRef.current = { ...lockedSessionTitlesRef.current, [row.id]: true };
      setLockedSessionTitles((current) => ({ ...current, [row.id]: true }));
      const result = await renameRuntimeSession({ sessionId: row.id, title: nextTitle });
      if (result.status === "error") {
        throw new Error(result.message || "重命名失败");
      }
      if (row.id === activeSessionId) {
        setActiveSessionTitle(nextTitle);
      }
      scheduleRuntimeSnapshotRefresh(300);
      setToast("会话已重命名");
    } catch (error) {
      if (row.id === activeSessionId) {
        setActiveSessionTitle(nextTitle);
      }
      setToast(error instanceof Error ? `仅更新本地标题：${error.message}` : "仅更新本地标题");
    }
  }

  async function removeSession(row: SessionRow) {
    if (!window.confirm(`删除会话「${row.title}」？该操作会清除这条会话记录。`)) return;
    flushPendingSessionMessagesSnapshot(row.id);
    closeSessionStream(row.id);
    abortSessionPreflight(row.id);
    const previousSnapshot = runtimeSnapshot;
    const previousSessionState = sessionUiState[row.id];
    const previousProjectId = sessionProjects[row.id];
    const previousProjectBinding = sessionProjectBindings[row.id];
    const previousLocked = lockedSessionTitles[row.id];
    const previousPinned = pinnedSessions[row.id];
    const previousPinnedAt = pinnedSessionTimes[row.id];
    setRuntimeSnapshot((current) => ({
      ...current,
      sessions: current.sessions.filter((session, index) => (session.session_id || session.id || `runtime-${index}`) !== row.id),
      totalSessions: Math.max(0, current.totalSessions - 1)
    }));
    setSessionUiState((current) => {
      if (!current[row.id]) return current;
      const next = { ...current };
      delete next[row.id];
      return next;
    });
    setSessionProjects((current) => {
      const next = { ...current };
      delete next[row.id];
      sessionProjectsRef.current = next;
      return next;
    });
    setSessionProjectBindings((current) => {
      const next = { ...current };
      delete next[row.id];
      sessionProjectBindingsRef.current = next;
      return next;
    });
    setLockedSessionTitles((current) => {
      const next = { ...current };
      delete next[row.id];
      lockedSessionTitlesRef.current = next;
      return next;
    });
    setPinnedSessions((current) => {
      const next = { ...current };
      delete next[row.id];
      return next;
    });
    setPinnedSessionTimes((current) => {
      const next = { ...current };
      delete next[row.id];
      return next;
    });
    setUnreadSessionIds((current) => {
      if (!current[row.id]) return current;
      const next = { ...current };
      delete next[row.id];
      return next;
    });
    if (row.id === activeSessionId) {
      startNewSession(null, { skipFlush: true });
    }
    setToast("会话已删除");
    try {
      const result = await deleteRuntimeSession(row.id);
      if (result.status === "error") {
        throw new Error(result.message || "删除失败");
      }
      scheduleRuntimeSnapshotRefresh(500);
    } catch (error) {
      setRuntimeSnapshot(previousSnapshot);
      if (previousSessionState) {
        setSessionUiState((current) => ({ ...current, [row.id]: previousSessionState }));
      }
      if (previousProjectId) {
        const next = { ...sessionProjectsRef.current, [row.id]: previousProjectId };
        sessionProjectsRef.current = next;
        setSessionProjects(next);
      }
      if (previousProjectBinding) {
        const next = { ...sessionProjectBindingsRef.current, [row.id]: previousProjectBinding };
        sessionProjectBindingsRef.current = next;
        setSessionProjectBindings(next);
      }
      if (previousLocked) {
        setLockedSessionTitles((current) => {
          const next = { ...current, [row.id]: previousLocked };
          lockedSessionTitlesRef.current = next;
          return next;
        });
      }
      if (previousPinned) {
        setPinnedSessions((current) => ({ ...current, [row.id]: previousPinned }));
      }
      if (previousPinnedAt) {
        setPinnedSessionTimes((current) => ({ ...current, [row.id]: previousPinnedAt }));
      }
      scheduleRuntimeSnapshotRefresh(0);
      setApproval({ type: "error", title: "会话删除失败", message: error instanceof Error ? error.message : "请稍后重试。" });
    }
  }

  async function addProject() {
    if (projectPickerBusy) return;
    setProjectPickerBusy(true);
    setToast("正在打开本地文件夹选择窗口，请在弹出的系统窗口中选择项目文件夹");
    try {
      const project = await chooseProjectFolder();
      if (!project) {
        setToast("已取消选择项目文件夹");
        return;
      }
      const registeredProject = project.memoryPath && project.dreamsPath
        ? project
        : await registerProjectFolderPath(project.path);
      if (!registeredProject) {
        throw new Error("Project folder registration failed.");
      }
      const projectForState = registeredProject
        ? {
            ...project,
            ...registeredProject,
            updatedAt: registeredProject.updatedAt || project.updatedAt
          }
        : project;
      let nextProject = projectForState;
      setProjects((current) => {
        const existing = current.find((item) => item.path === projectForState.path);
        if (existing) {
          nextProject = { ...projectForState, id: existing.id, pinned: existing.pinned, updatedAt: projectForState.updatedAt };
          return current.map((item) => item.path === projectForState.path ? nextProject : item);
        }
        return [projectForState, ...current];
      });
      window.setTimeout(() => startNewSession(nextProject), 0);
      setToast("项目已添加");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "添加项目失败");
    } finally {
      setProjectPickerBusy(false);
    }
  }

  function togglePinSession(row: SessionRow) {
    const nextPinned = !Boolean(row.pinned);
    setPinnedSessions((current) => ({ ...current, [row.id]: nextPinned }));
    setPinnedSessionTimes((current) => {
      const next = { ...current };
      if (nextPinned) {
        next[row.id] = Date.now();
      } else {
        delete next[row.id];
      }
      return next;
    });
  }

  function togglePinProject(project: ProjectFolder) {
    const nextPinned = !Boolean(pinnedProjects[project.id] || project.pinned);
    setPinnedProjects((current) => ({ ...current, [project.id]: nextPinned }));
    setProjects((current) => current.map((item) => item.id === project.id ? { ...item, pinned: nextPinned } : item));
  }

  function renameProject(project: ProjectFolder) {
    const nextName = window.prompt("重命名项目", project.name)?.trim();
    if (!nextName || nextName === project.name) return;
    setProjects((current) => current.map((item) => item.id === project.id ? { ...item, name: nextName, updatedAt: new Date().toISOString() } : item));
    setProjectMenu(null);
  }

  function deleteProject(project: ProjectFolder) {
    if (!window.confirm(`删除项目「${project.name}」？项目文件夹不会被删除，已有项目会话会变成通用会话。`)) return;
    const nextProjects = projects.filter((item) => item.id !== project.id);
    const nextPinnedProjects = { ...pinnedProjects };
    delete nextPinnedProjects[project.id];
    const nextSessionProjects = { ...sessionProjects };
    Object.entries(nextSessionProjects).forEach(([sessionId, projectId]) => {
      if (projectId === project.id) delete nextSessionProjects[sessionId];
    });
    const nextSessionProjectBindings = { ...sessionProjectBindings };
    Object.entries(nextSessionProjectBindings).forEach(([sessionId, binding]) => {
      if (binding?.projectId === project.id) delete nextSessionProjectBindings[sessionId];
    });
    const nextSessionUiState = Object.fromEntries(
      Object.entries(sessionUiState).map(([sessionId, state]) => {
        if (state.projectId !== project.id && state.projectBinding?.projectId !== project.id) return [sessionId, state];
        return [sessionId, { ...state, projectId: null, projectBinding: null }];
      })
    ) as Record<string, SessionUiState>;
    setProjects(nextProjects);
    setPinnedProjects(nextPinnedProjects);
    setSessionProjects(nextSessionProjects);
    setSessionProjectBindings(nextSessionProjectBindings);
    setSessionUiState(nextSessionUiState);
    sessionProjectsRef.current = nextSessionProjects;
    sessionProjectBindingsRef.current = nextSessionProjectBindings;
    if (activeProjectId === project.id) {
      setActiveProjectId(null);
    }
    if (sidecarStatus.state === "running") {
      void saveRuntimeUiState({
        version: 1,
        replaceProjectState: true,
        projectStateMode: "replace",
        allowEmptyProjectState: true,
        lastActiveSessionId: activeSessionIdRef.current,
        activeProjectId: activeProjectId === project.id ? null : activeProjectId,
        projects: nextProjects,
        sessionProjects: nextSessionProjects,
        sessionProjectBindings: nextSessionProjectBindings,
        sessionTitles,
        pinnedSessions,
        pinnedSessionTimes,
        pinnedProjects: nextPinnedProjects,
        sessionUiState: nextSessionUiState,
        savedAt: new Date().toISOString()
      }).catch(() => undefined);
    }
    setProjectMenu(null);
  }

  function openProjectInExplorer(project: ProjectFolder) {
    void registerProjectFolderPath(project.path).catch(() => null).then(() => openLocalPath(project.path));
    setProjectMenu(null);
  }

  function showProjectMenu(event: MouseEvent, project: ProjectFolder) {
    event.preventDefault();
    setProjectMenu({ projectId: project.id, x: event.clientX, y: event.clientY });
  }

  async function verifyAddableChatFile(file: FileAttachment, sessionId = activeSessionIdRef.current) {
    const normalizedPath = normalizeLocalSource(file.file_path);
    if (!normalizedPath || !isDurableLocalAttachment({ ...file, file_path: normalizedPath })) {
      return { ok: false, reason: "Only verified local files can be added to the current chat" };
    }
    const resolvedPath = resolveArtifactPathForSession(sessionId, normalizedPath);
    try {
      const stat = await statLocalPath(resolvedPath);
      const status = String(stat.status || "").toLowerCase();
      if (status === "denied") return { ok: false, reason: "File access is blocked by permissions" };
      if (status === "error") return { ok: false, reason: "Could not verify local file" };
      const expectedKindOk = file.file_type === "directory" ? stat.isDirectory !== false : stat.isFile !== false;
      if (!stat.exists || !expectedKindOk) return { ok: false, reason: "Local file was not found" };
      return { ok: true, reason: "", resolvedPath };
    } catch {
      return { ok: false, reason: "Could not verify local file" };
    }
  }

  function showChatFileMenu(event: MouseEvent, file: FileAttachment | LocalFilePayload) {
    event.preventDefault();
    event.stopPropagation();
    const normalizedFile: FileAttachment = {
      file_path: normalizeLocalSource(file.file_path),
      file_name: file.file_name || normalizeLocalSource(file.file_path).split(/[\\/]/).filter(Boolean).pop() || "file",
      file_type: file.file_type || (isImageAttachment(file as FileAttachment) ? "image" : "file"),
      previewDataUrl: file.previewDataUrl,
      preview_url: file.preview_url
    };
    const durable = isDurableLocalAttachment(normalizedFile);
    setProjectMenu(null);
    const menuKey = normalizeAttachmentDedupeKey(normalizedFile);
    setChatFileMenu({
      file: normalizedFile,
      x: event.clientX,
      y: event.clientY,
      canAdd: false,
      disabledReason: durable ? "Verifying local file..." : "Only verified local files can be added to the current chat"
    });
    if (durable) {
      void verifyAddableChatFile(normalizedFile).then((result) => {
        setChatFileMenu((current) => {
          if (!current || normalizeAttachmentDedupeKey(current.file) !== menuKey) return current;
          return { ...current, canAdd: result.ok, disabledReason: result.ok ? "" : result.reason };
        });
      });
    }
  }

  async function addFileToCurrentChat(file: FileAttachment) {
    const verification = await verifyAddableChatFile(file);
    if (!verification.ok) {
      setToast(verification.reason || "Only verified local files can be added to the current chat");
      setChatFileMenu(null);
      return;
    }
    const normalizedPath = normalizeLocalSource(file.file_path);
    const normalizedFile: FileAttachment = {
      ...file,
      file_path: verification.resolvedPath || normalizedPath,
      file_name: file.file_name || normalizedPath.split(/[\\/]/).filter(Boolean).pop() || "file",
      file_type: file.file_type || (isImageAttachment(file) ? "image" : "file")
    };
    const key = normalizeAttachmentDedupeKey(normalizedFile);
    setAttachments((current) => {
      if (current.some((item) => normalizeAttachmentDedupeKey(item) === key)) return current;
      return [...current, normalizedFile];
    });
    setChatFileMenu(null);
    focusComposerSoon();
    setToast("已添加到当前聊天");
  }

  async function chooseFiles() {
    try {
      const files = await chooseLocalFiles(sidecarStatus.webPort);
      setAttachments((current) => {
        const seen = new Set(current.map(normalizeAttachmentDedupeKey));
        const next = [...current];
        files.forEach((file) => {
          const key = normalizeAttachmentDedupeKey(file);
          if (!seen.has(key)) {
            seen.add(key);
            next.push(file);
          }
        });
        return next;
      });
      composerRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setToast(error instanceof Error ? error.message : "选择文件失败");
    }
  }

  async function attachBrowserFiles(files: File[], source: "paste" | "drop") {
    if (!files.length) return;
    try {
      const results = await Promise.allSettled(files.map((file) => savePastedFile(file)));
      const nextFiles = results
        .filter((result): result is PromiseFulfilledResult<FileAttachment | null> => result.status === "fulfilled")
        .map((result) => result.value)
        .filter(Boolean) as FileAttachment[];
      if (!nextFiles.length) {
        setToast(source === "drop" ? "未能添加拖拽的文件" : "未能添加粘贴的文件");
        return;
      }
      setAttachments((current) => {
        const seen = new Set(current.map(normalizeAttachmentDedupeKey));
        const next = [...current];
        nextFiles.forEach((file) => {
          const key = normalizeAttachmentDedupeKey(file);
          if (!seen.has(key)) {
            seen.add(key);
            next.push(file);
          }
        });
        return next;
      });
      const failedCount = results.filter((result) => result.status === "rejected").length;
      setToast(failedCount ? `已添加 ${nextFiles.length} 个文件，${failedCount} 个失败` : source === "drop" ? `已添加 ${nextFiles.length} 个文件` : "已添加粘贴的文件");
      composerRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setToast(error instanceof Error ? error.message : source === "drop" ? "拖拽附件失败" : "粘贴附件失败");
    }
  }

  async function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files || []);
    if (!files.length) return;
    event.preventDefault();
    await attachBrowserFiles(files, "paste");
  }

  function dragEventHasFiles(event: DragEvent<HTMLElement>) {
    return Array.from(event.dataTransfer.types || []).includes("Files");
  }

  function handleComposerDragEnter(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    composerDragDepth.current += 1;
    event.dataTransfer.dropEffect = "copy";
    setComposerDragActive(true);
  }

  function handleComposerDragOver(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setComposerDragActive(true);
  }

  function handleComposerDragLeave(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    composerDragDepth.current = Math.max(0, composerDragDepth.current - 1);
    if (composerDragDepth.current === 0) {
      setComposerDragActive(false);
    }
  }

  function handleComposerDrop(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    composerDragDepth.current = 0;
    setComposerDragActive(false);
    const files = Array.from(event.dataTransfer.files || []);
    void attachBrowserFiles(files, "drop");
  }

  function clearComposerDragState() {
    composerDragDepth.current = 0;
    setComposerDragActive(false);
  }

  function protectBlankDraftSession(sessionId: string) {
    blankDraftSessionEpochsRef.current = {
      ...blankDraftSessionEpochsRef.current,
      [sessionId]: sessionSwitchSeq.current
    };
  }

  function clearBlankDraftProtection(sessionId: string) {
    if (!blankDraftSessionEpochsRef.current[sessionId]) return;
    const next = { ...blankDraftSessionEpochsRef.current };
    delete next[sessionId];
    blankDraftSessionEpochsRef.current = next;
  }

  function isProtectedActiveBlankDraft(sessionId: string) {
    return Boolean(
      blankDraftSessionEpochsRef.current[sessionId]
      && activeSessionIdRef.current === sessionId
      && messagesRef.current.length === 0
    );
  }

  function updateSessionMessages(sessionId: string, updater: (messages: ChatItem[]) => ChatItem[]) {
    if (activeSessionIdRef.current === sessionId) {
      const nextMessages = updater(messagesRef.current);
      if (nextMessages.length) clearBlankDraftProtection(sessionId);
      messagesRef.current = nextMessages;
      committedSessionMessageSnapshots.current[sessionId] = nextMessages;
      setMessages(nextMessages);
      scheduleSessionMessagesSnapshot(sessionId, nextMessages);
      return;
    }

    const existingMessages = pendingSessionMessageSnapshots.current[sessionId]
      || committedSessionMessageSnapshots.current[sessionId]
      || [];
    const nextMessages = updater(existingMessages);
    scheduleSessionMessagesSnapshot(sessionId, nextMessages);
  }

  function commitSessionMessagesSnapshot(sessionId: string, nextMessages: ChatItem[]) {
    committedSessionMessageSnapshots.current[sessionId] = nextMessages;
    setSessionUiState((current) => {
      const binding = projectBindingForSession(sessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, current, projectCatalog);
      const existing = current[sessionId] || {
        title: sessionTitles[sessionId] || activeSessionTitle,
        projectId: binding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
        projectBinding: binding,
        messages: sessionId === activeSessionIdRef.current ? messagesRef.current : [],
        composerText: sessionId === activeSessionIdRef.current ? composerTextRef.current : "",
        attachments: sessionId === activeSessionIdRef.current ? attachmentsRef.current : []
      };
      const activityAt = latestMessageMs(nextMessages) || existing.lastActivityAt || Date.now();
      return {
        ...current,
        [sessionId]: {
          ...existing,
          projectId: binding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
          projectBinding: binding,
          messages: nextMessages,
          composerText: sessionId === activeSessionIdRef.current ? composerTextRef.current : existing.composerText,
          attachments: sessionId === activeSessionIdRef.current ? attachmentsRef.current : existing.attachments,
          lastActivityAt: activityAt
        }
      };
    });
  }

  function flushPendingSessionMessagesSnapshot(sessionId?: string) {
    const ids = sessionId ? [sessionId] : Object.keys(pendingSessionMessageSnapshots.current);
    ids.forEach((id) => {
      const timer = sessionUiMessageSyncTimers.current[id];
      if (timer) {
        window.clearTimeout(timer);
        delete sessionUiMessageSyncTimers.current[id];
      }
      const pending = pendingSessionMessageSnapshots.current[id];
      if (!pending) return;
      delete pendingSessionMessageSnapshots.current[id];
      commitSessionMessagesSnapshot(id, pending);
    });
  }

  function scheduleSessionMessagesSnapshot(sessionId: string, nextMessages: ChatItem[]) {
    pendingSessionMessageSnapshots.current[sessionId] = nextMessages;
    const hasLiveMessage = nextMessages.some((message) => message.role === "assistant" && Boolean(message.pending || message.paused));
    const delay = activeSessionIdRef.current === sessionId
      ? (hasLiveMessage ? 650 : 80)
      : (hasLiveMessage ? 1200 : 160);
    if (sessionUiMessageSyncTimers.current[sessionId]) {
      window.clearTimeout(sessionUiMessageSyncTimers.current[sessionId]);
    }
    sessionUiMessageSyncTimers.current[sessionId] = window.setTimeout(() => {
      delete sessionUiMessageSyncTimers.current[sessionId];
      const pending = pendingSessionMessageSnapshots.current[sessionId];
      if (!pending) return;
      delete pendingSessionMessageSnapshots.current[sessionId];
      commitSessionMessagesSnapshot(sessionId, pending);
    }, delay);
  }

  function clearSessionUnread(sessionId: string) {
    setUnreadSessionIds((current) => {
      if (!current[sessionId]) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }

  function markSessionOutputReady(sessionId: string) {
    if (activeSessionIdRef.current === sessionId) return;
    setUnreadSessionIds((current) => current[sessionId] ? current : { ...current, [sessionId]: true });
  }

  function updateAssistantMessage(sessionId: string, assistantId: string, updater: (message: ChatItem) => ChatItem) {
    updateSessionMessages(sessionId, (current) => current.map((message) => message.id === assistantId ? updater(message) : message));
  }

  function updateAssistantMessageForRequest(sessionId: string, assistantId: string, requestId: string, updater: (message: ChatItem) => ChatItem) {
    updateSessionMessages(sessionId, (current) => {
      let updated = false;
      const next = current.map((message) => {
        if (message.id === assistantId || (requestId && message.role === "assistant" && message.requestId === requestId)) {
          updated = true;
          return updater({ ...message, requestId: requestId || message.requestId });
        }
        return message;
      });
      if (updated) return next;
      const fallback: ChatItem = {
        id: assistantId || `a-resume-${requestId || Date.now()}`,
        role: "assistant",
        content: "",
        pending: true,
        requestId,
        createdAt: new Date().toISOString(),
        steps: [{ type: "phase", content: "正在连接响应" }]
      };
      return [...current, updater(fallback)];
    });
  }

  function rememberStreamTurnSequence(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    if (typeof item.user_seq !== "number" && typeof item.bot_seq !== "number") return;
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
      ...message,
      userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
      botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
    }));
  }

  async function refreshSessionFromHistory(sessionId: string) {
    try {
      const history = await loadSessionHistoryWithMeta(sessionId);
      if (isProtectedActiveBlankDraft(sessionId)) {
        void reportDesktopEvent({
          type: "info",
          source: "Desktop",
          category: "session",
          label: "stale_history_suppressed",
          sessionId,
          detail: { reason: "active_blank_draft" }
        }).catch(() => undefined);
        return false;
      }
      rememberHistoryProjectBinding(sessionId, history.projectContext);
      const historyBinding = history.projectContext
        || projectBindingForSession(sessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, sessionUiState, projectCatalog);
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, sessionId, history.contextStartSeq))
        .map(mergeBufferedPostDoneArtifacts);
      const hasFinalAssistant = mapped.some((message) => (
        message.role === "assistant"
        && !message.pending
        && messageHasTerminalPayload(message)
      ));
      if (!hasFinalAssistant) return false;
      const localMessages = sessionId === activeSessionIdRef.current
        ? messagesRef.current
        : sessionUiState[sessionId]?.messages || [];
      const merged = mergeHistoryWithLocalMessages(mapped, localMessages);
      updateSessionMessages(sessionId, () => merged);
      setSessionUiState((current) => ({
        ...current,
        [sessionId]: {
          ...(current[sessionId] || {
            title: sessionTitles[sessionId] || activeSessionTitle,
            projectId: historyBinding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
            projectBinding: historyBinding,
            composerText: "",
            attachments: []
          }),
          projectId: historyBinding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
          projectBinding: historyBinding,
          messages: merged,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(merged) || current[sessionId]?.lastActivityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === sessionId) {
        setMessages(merged);
      } else {
        markSessionOutputReady(sessionId);
      }
      return true;
    } catch (error) {
      return false;
    }
  }

  async function refreshSessionFromHistoryForRequest(sessionId: string, requestId: string) {
    if (!requestId) return false;
    try {
      const history = await loadSessionHistoryWithMeta(sessionId);
      if (isProtectedActiveBlankDraft(sessionId)) {
        void reportDesktopEvent({
          type: "info",
          source: "Desktop",
          category: "session",
          label: "stale_request_history_suppressed",
          sessionId,
          detail: { reason: "active_blank_draft", requestId }
        }).catch(() => undefined);
        return false;
      }
      rememberHistoryProjectBinding(sessionId, history.projectContext);
      const historyBinding = history.projectContext
        || projectBindingForSession(sessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, sessionUiState, projectCatalog);
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, sessionId, history.contextStartSeq))
        .map(mergeBufferedPostDoneArtifacts);
      const scopedFinal = mapped.some((message) => (
        message.role === "assistant"
        && message.requestId === requestId
        && !message.pending
        && messageHasTerminalPayload(message)
      ));
      const localMessages = sessionId === activeSessionIdRef.current
        ? messagesRef.current
        : sessionUiState[sessionId]?.messages || [];
      const merged = mergeHistoryWithLocalMessages(mapped, localMessages);
      updateSessionMessages(sessionId, () => merged);
      setSessionUiState((current) => ({
        ...current,
        [sessionId]: {
          ...(current[sessionId] || {
            title: sessionTitles[sessionId] || activeSessionTitle,
            projectId: historyBinding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
            projectBinding: historyBinding,
            composerText: "",
            attachments: []
          }),
          projectId: historyBinding?.projectId || sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current),
          projectBinding: historyBinding,
          messages: merged,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(merged) || current[sessionId]?.lastActivityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === sessionId) {
        setMessages(merged);
      } else {
        markSessionOutputReady(sessionId);
      }
      return scopedFinal;
    } catch {
      return false;
    }
  }

  function historyRecoveryKey(sessionId: string, requestId: string) {
    return `${sessionId}::${requestId}`;
  }

  function clearHistoryRecovery(sessionId: string, requestId?: string) {
    const prefix = requestId ? historyRecoveryKey(sessionId, requestId) : `${sessionId}::`;
    Object.keys(historyRecoveryTimersRef.current).forEach((key) => {
      if (requestId ? key === prefix : key.startsWith(prefix)) {
        historyRecoveryTimersRef.current[key].forEach((timer) => window.clearTimeout(timer));
        delete historyRecoveryTimersRef.current[key];
      }
    });
  }

  function scheduleHistoryRecovery(sessionId: string, requestId: string, delays = [1200, 3500, 8000]) {
    if (!requestId) return;
    const key = historyRecoveryKey(sessionId, requestId);
    clearHistoryRecovery(sessionId, requestId);
    historyRecoveryTimersRef.current[key] = delays.map((delay) => window.setTimeout(() => {
      const currentRequestId = sessionRequestIdsRef.current[sessionId];
      if (currentRequestId && currentRequestId !== requestId && !completedRequestIds.current[requestId]) {
        clearHistoryRecovery(sessionId, requestId);
        return;
      }
      void refreshSessionFromHistory(sessionId);
    }, delay));
  }

  function recoverStaleRequestFromHistory(sessionId: string, assistantId: string, requestId: string) {
    void refreshSessionFromHistory(sessionId).then((restored) => {
      if (restored) return;
      updateAssistantMessage(sessionId, assistantId, (message) => ({
        ...finishRunningSteps(message),
        requestId: undefined,
        content: redactInternalPromptText(message.content || "任务状态已同步。如未完成，请重新发送。"),
        pending: false,
        paused: false,
        cancelled: false
      }));
      markSessionOutputReady(sessionId);
    });
  }

  function handleReplayGapStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const requested = typeof item.requested_last_event_id === "number" ? item.requested_last_event_id : undefined;
    const retained = typeof item.retained_from_event_id === "number" ? item.retained_from_event_id : undefined;
    const detail = [requested !== undefined ? `requested=${requested}` : "", retained !== undefined ? `retainedFrom=${retained}` : ""]
      .filter(Boolean)
      .join(", ");
    const message = redactInternalPromptText(
      item.message
      || item.content
      || `响应记录暂时没有接上${detail ? `（${detail}）` : ""}，已刷新保存的会话；如果最终答案缺失，可以准备重试。`
    );
    markStreamTerminal(sessionId, requestId, "failed");
    finishSessionRequest(sessionId, requestId);
    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: false,
        recovery: {
          kind: "replay_gap",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "stream_replay_gap",
      message,
      sessionId,
      detail: {
        requestId,
        requestedLastEventId: requested ?? null,
        retainedFromEventId: retained ?? null,
        nextEventId: item.next_event_id ?? null
      }
    });
  }

  function handleInterruptedStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const message = redactInternalPromptText(
      item.message
      || item.content
      || "Runtime sidecar restarted before this run reached a terminal state. Refreshed saved conversation; retry if the final answer is missing."
    );
    markStreamTerminal(sessionId, requestId, "interrupted");
    finishSessionRequest(sessionId, requestId);
    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: false,
        recovery: {
          kind: "interrupted",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "stream_interrupted",
      message,
      sessionId,
      detail: {
        requestId,
        terminalReason: item.terminal_reason || null,
        errorCode: item.error_code || null
      }
    });
  }

  function settleTerminalSnapshotRequest(request: RuntimeActiveRequest) {
    const sessionId = String(request.session_id || "").trim();
    const requestId = String(request.request_id || "").trim();
    if (!sessionId || !requestId) return;
    if (!isAbnormalTerminalRequest(request)) return;
    const handledKey = `${sessionId}::${requestId}`;
    if (handledSnapshotTerminalRequestsRef.current[handledKey]) return;

    const sourceMessages = activeSessionIdRef.current === sessionId
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const assistant = sourceMessages.find((message) => (
      message.role === "assistant"
      && message.requestId === requestId
      && message.pending
    ));
    if (!assistant && sessionRequestIdsRef.current[sessionId] !== requestId) return;

    handledSnapshotTerminalRequestsRef.current = {
      ...handledSnapshotTerminalRequestsRef.current,
      [handledKey]: true
    };
    const state = runCenterState(request);
    const phase = state === "cancelled" || state === "cancelling" ? "cancelled" : "interrupted";
    const message = redactInternalPromptText(
      request.error_message
      || request.terminal_reason
      || "Runtime session lock owner disappeared before the run reached a terminal state."
    );
    markStreamTerminal(sessionId, requestId, phase);
    finishSessionRequest(sessionId, requestId);
    if (!assistant) return;

    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistant.id, requestId, (entry) => ({
        ...finishRunningSteps(entry, phase === "cancelled" ? "cancelled" : "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: phase === "cancelled",
        recovery: phase === "cancelled" ? undefined : {
          kind: "interrupted",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "snapshot_terminal_request",
      message,
      sessionId,
      detail: {
        requestId,
        state: request.state || request.status || request.phase || null,
        terminalReason: request.terminal_reason || null,
        errorCode: request.error_code || null
      }
    });
  }

  function reportStreamErrorTelemetry(sessionId: string, requestId: string, message: string, staleRequest: boolean) {
    if (staleRequest) {
      void reportDesktopEvent({
        type: "warn",
        source: "Desktop",
        category: "runtime",
        label: "stale_stream_request",
        message,
        sessionId,
        detail: {
          requestId,
          recovery: "history",
          suppressedErrorLog: true
        }
      });
      return;
    }
    void reportDesktopEvent({
      type: "error",
      source: "Desktop",
      message,
      sessionId,
      detail: { requestId }
    });
  }

  function finishRunningSteps(message: ChatItem, reason: AgentFinishReason = "done"): ChatItem {
    return { ...message, steps: finishAgentSteps(message.steps, reason), toolCalls: message.toolCalls?.map((tool) => ({ ...tool, running: false })) };
  }

  function clearTransientSendSteps(message: ChatItem): ChatItem {
    return {
      ...message,
      steps: (message.steps || []).filter((step) => (
        step.type !== "phase"
        || !isTransientPhaseContent(step.content)
      ))
    };
  }

  function isTransientPhaseContent(content?: string) {
    const value = String(content || "").trim();
    if (!value) return true;
    return [
      "正在发送",
      "正在连接响应",
      "正在连接后台任务",
      "已收到，正在准备响应",
      "正在检查额度",
      "正在建立响应通道",
      "正在组织上下文",
      "正在连接模型响应",
      "正在恢复响应通道"
    ].some((prefix) => value === prefix || value.startsWith(`${prefix} `) || value.startsWith(`${prefix} ·`))
      || value.startsWith("等待本机工具授权");
  }

  function replaceCurrentPhase(message: ChatItem, rawContent: string): ChatItem {
    const content = redactInternalPromptText(rawContent || "").trim();
    if (!content) return message;
    const steps = (message.steps || []).filter((step) => (
      step.type !== "phase" || !isTransientPhaseContent(step.content)
    ));
    const last = steps[steps.length - 1];
    if (last?.type === "phase") {
      if ((last.content || "") === content) {
        return { ...message, pending: true, paused: false };
      }
      steps[steps.length - 1] = { ...last, content };
    } else {
      steps.push({ type: "phase", content });
    }
    return {
      ...message,
      pending: true,
      paused: false,
      phaseStartedAt: message.phaseStartedAt || Date.now(),
      steps
    };
  }

  function clearAssistantPhaseTimers(assistantId?: string) {
    if (!assistantId) return;
    const timers = phaseTimersRef.current[assistantId] || [];
    timers.forEach((timer) => window.clearTimeout(timer));
    delete phaseTimersRef.current[assistantId];
  }

  function clearAllPhaseTimers() {
    Object.keys(phaseTimersRef.current).forEach((assistantId) => clearAssistantPhaseTimers(assistantId));
  }

  function queuePreflightPhase(sessionId: string, assistantId: string, generation: number, delayMs: number, content: string) {
    const timer = window.setTimeout(() => {
      if (sendGenerationRef.current[sessionId] !== generation) return;
      updateAssistantMessage(sessionId, assistantId, (message) => {
        if (!message.pending || message.requestId || message.cancelled) return message;
        return replaceCurrentPhase(message, content);
      });
    }, delayMs);
    phaseTimersRef.current[assistantId] = [...(phaseTimersRef.current[assistantId] || []), timer];
  }

  function markSessionRequestsPaused(sessionId: string) {
    updateSessionMessages(sessionId, (current) => current.map((message) => message.pending ? {
      ...finishRunningSteps(message, "paused"),
      content: pausedMessageContent(message.content),
      pending: false,
      paused: true,
      cancelled: false
    } : message));
  }

  function appendReasoningStep(message: ChatItem, chunk: string): ChatItem {
    if (message.visibleOutputSettled) return message;
    chunk = redactInternalPromptText(chunk);
    if (!chunk) return message;
    const steps = [...(message.steps || [])];
    const last = steps[steps.length - 1];
    if (last?.type === "thinking" && last.running) {
      steps[steps.length - 1] = { ...last, content: `${last.content || ""}${chunk}` };
    } else {
      steps.push({ type: "thinking", content: chunk, running: true, startedAt: Date.now() });
    }
    return { ...message, pending: true, steps };
  }

  function flushIntermediateContent(message: ChatItem): ChatItem {
    const content = redactInternalPromptText(message.content).trim();
    if (!content) return message;
    return {
      ...message,
      content: "",
      steps: [...(message.steps || []), { type: "content", content, intermediate: true }]
    };
  }

  function appendToolStart(message: ChatItem, item: StreamItem): ChatItem {
    const next = flushIntermediateContent(finishRunningSteps(message));
    const toolName = item.tool || item.name || "tool";
    const toolId = item.tool_call_id || `${toolName}-${Date.now()}`;
    const steps = [...(next.steps || [])];
    const toolIndex = steps.findIndex((step) => step.type === "tool" && ((toolId && step.id === toolId) || (!toolId && step.name === toolName)));
    const runningTool: Extract<AgentStepDisclosure, { type: "tool" }> = {
      type: "tool",
      id: toolId,
      name: toolName,
      arguments: redactToolDisclosureValue(item.arguments ?? item.input),
      qualityEvidence: normalizeQualityEvidence(item.qualityEvidence || normalizeQualityEvidence(item.result)),
      status: "running",
      deadline_seconds: item.deadline_seconds,
      max_seconds: item.max_seconds,
      extension_count: item.extension_count,
      lastHeartbeatAt: Date.now(),
      running: true
    };
    if (toolIndex >= 0) {
      steps[toolIndex] = runningTool;
    } else {
      steps.push(runningTool);
    }
    return {
      ...next,
      pending: true,
      steps
    };
  }

  function appendToolEnd(message: ChatItem, item: StreamItem): ChatItem {
    const steps = [...(message.steps || [])];
    const toolName = item.tool || item.name;
    const toolId = item.tool_call_id || "";
    let targetIndex = -1;
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const step = steps[index];
      if (step.type === "tool" && step.running && ((toolId && step.id === toolId) || (!toolId && (!toolName || step.name === toolName)))) {
        targetIndex = index;
        break;
      }
    }
    if (targetIndex < 0 && toolName) {
      for (let index = steps.length - 1; index >= 0; index -= 1) {
        const step = steps[index];
        if (step.type === "tool" && step.name === toolName) {
          targetIndex = index;
          break;
        }
      }
    }
    const completedTool: Extract<AgentStepDisclosure, { type: "tool" }> = {
      type: "tool",
      id: toolId || undefined,
      name: toolName,
      arguments: item.arguments ?? item.input,
      result: typeof (item.result ?? item.content ?? item.message) === "string"
        ? redactInternalPromptText(item.result ?? item.content ?? item.message)
        : item.result ?? item.content ?? item.message,
      qualityEvidence: normalizeQualityEvidence(item.qualityEvidence || normalizeQualityEvidence(item.result ?? item.content ?? item.message)),
      status: item.status || "done",
      execution_time: item.execution_time,
      deadline_seconds: item.deadline_seconds,
      max_seconds: item.max_seconds,
      extension_count: item.extension_count,
      lastHeartbeatAt: Date.now(),
      is_error: item.status === "error" || item.status === "failed",
      running: false
    };
    if (targetIndex >= 0) {
      const previous = steps[targetIndex];
      if (previous.type === "tool") {
        steps[targetIndex] = {
          ...previous,
          ...completedTool,
          arguments: completedTool.arguments ?? previous.arguments,
          qualityEvidence: completedTool.qualityEvidence || previous.qualityEvidence
        };
      }
    } else {
      steps.push(completedTool);
    }
    return { ...message, pending: true, steps };
  }

  function appendToolHeartbeat(message: ChatItem, item: StreamItem): ChatItem {
    const steps = [...(message.steps || [])];
    const toolName = item.tool || item.name || "tool";
    const toolId = item.tool_call_id || "";
    let targetIndex = -1;
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const step = steps[index];
      if (step.type === "tool" && ((toolId && step.id === toolId) || (!toolId && step.name === toolName))) {
        targetIndex = index;
        break;
      }
    }
    const heartbeatPatch: Partial<Extract<AgentStepDisclosure, { type: "tool" }>> = {
      type: "tool",
      id: toolId || undefined,
      name: toolName,
      status: item.status || "running",
      deadline_seconds: item.deadline_seconds,
      max_seconds: item.max_seconds,
      extension_count: item.extension_count,
      execution_time: item.elapsed_seconds ?? item.execution_time,
      lastHeartbeatAt: Date.now(),
      running: true
    };
    if (targetIndex >= 0) {
      const previous = steps[targetIndex];
      if (previous.type === "tool") {
        steps[targetIndex] = { ...previous, ...heartbeatPatch, arguments: previous.arguments ?? item.arguments ?? item.input };
      }
    } else {
      steps.push({ ...heartbeatPatch, arguments: item.arguments ?? item.input } as Extract<AgentStepDisclosure, { type: "tool" }>);
    }
    return { ...message, pending: true, steps };
  }

  function appendToolDeadlineExtended(message: ChatItem, item: StreamItem): ChatItem {
    return appendToolHeartbeat(message, {
      ...item,
      type: "tool_heartbeat",
      status: item.status || "running"
    });
  }

  function appendMediaStep(message: ChatItem, item: StreamItem, pending = true): ChatItem {
    const next = finishRunningSteps(message);
    const type = item.type === "image" || item.file_type === "image"
      ? "image"
      : item.type === "video" || item.file_type === "video"
        ? "video"
        : item.type === "audio" || item.type === "voice_attach" || item.file_type === "audio"
          ? "audio"
          : "file";
    return {
      ...next,
      pending,
      paused: false,
      steps: [
        ...(next.steps || []),
        {
          type: "media",
          fileType: type,
          url: item.path || item.url || redactInternalPromptText(item.content || ""),
          fileName: item.file_name || item.name
        }
      ]
    };
  }

  function artifactDedupeKey(artifact: AgentArtifact) {
    return artifactMergeKey(artifact);
  }

  function streamItemArtifacts(item: StreamItem, requestId?: string) {
    const sourceRequestId = item.request_id || requestId;
    const incoming = item.artifact
      ? normalizeArtifactEntry(item.artifact, 0, sourceRequestId)
      : Array.isArray(item.artifacts)
        ? item.artifacts.map((entry, index) => normalizeArtifactEntry(entry, index, sourceRequestId)).filter((entry): entry is AgentArtifact => Boolean(entry))
        : [];
    return Array.isArray(incoming) ? incoming : incoming ? [incoming] : [];
  }

  function appendArtifact(message: ChatItem, item: StreamItem, pending = true): ChatItem {
    const artifacts = streamItemArtifacts(item, item.request_id || message.requestId);
    if (!artifacts.length) return message;
    const nextArtifacts = [...(message.artifacts || [])];
    for (const artifact of artifacts) {
      const key = artifactDedupeKey(artifact);
      const index = nextArtifacts.findIndex((entry) => entry.id === artifact.id || artifactDedupeKey(entry) === key);
      if (index >= 0) {
        nextArtifacts[index] = mergeAgentArtifactRecord(nextArtifacts[index], artifact);
      } else {
        nextArtifacts.push(artifact);
      }
    }
    return { ...message, pending, paused: false, artifacts: nextArtifacts };
  }

  function settleVisibleStreamOutput(message: ChatItem, options: { awaitingStreamDone?: boolean } = {}): ChatItem {
    return {
      ...clearTransientSendSteps(finishRunningSteps(message)),
      pending: false,
      paused: false,
      visibleOutputSettled: options.awaitingStreamDone ?? true,
      recovery: undefined
    };
  }

  function rememberPostDoneTailArtifacts(requestId: string, item: StreamItem) {
    const artifacts = streamItemArtifacts(item, requestId);
    if (!requestId || !artifacts.length) return;
    const currentMessage: ChatItem = {
      id: `postdone-buffer-${requestId}`,
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      artifacts: postDoneTailArtifactsRef.current[requestId] || []
    };
    postDoneTailArtifactsRef.current = {
      ...postDoneTailArtifactsRef.current,
      [requestId]: mergeArtifactsIntoMessage(currentMessage, artifacts).artifacts || artifacts
    };
  }

  function mergeBufferedPostDoneArtifacts(message: ChatItem) {
    const requestId = message.requestId || "";
    return requestId ? mergeArtifactsIntoMessage(message, postDoneTailArtifactsRef.current[requestId] || []) : message;
  }

  function sessionHasAssistantRequest(sessionId: string, requestId: string) {
    if (!requestId) return false;
    const source = activeSessionIdRef.current === sessionId
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    return source.some((message) => message.role === "assistant" && message.requestId === requestId);
  }

  function isCurrentSessionRequest(sessionId: string, requestId?: string) {
    if (!requestId) return true;
    const currentRequestId = sessionRequestIdsRef.current[sessionId];
    if (currentRequestId) return currentRequestId === requestId;
    return sessionHasAssistantRequest(sessionId, requestId);
  }

  function isUiLiveAssistantMessage(message: ChatItem) {
    return isLiveAssistantMessage(message)
      && !(message.requestId && locallyCompletedRequestIdsRef.current[message.requestId]);
  }

  function isPostDoneTailItem(item: StreamItem) {
    return item.type === "voice_attach"
      || item.type === "artifact"
      || item.type === "file"
      || item.type === "image"
      || item.type === "video"
      || item.type === "audio"
      || Boolean(item.artifact || item.artifacts);
  }

  function streamItemText(item: StreamItem) {
    return String(item.content ?? item.text ?? item.delta ?? "");
  }

  function streamRequestKey(sessionId: string, requestId: string) {
    return `${sessionId}::${requestId}`;
  }

  function hasScheduledStreamReconnect(sessionId: string, requestId: string) {
    const key = streamRequestKey(sessionId, requestId);
    return Boolean(streamReconnectTimers.current[key] || streamReconnectChecks.current[key]);
  }

  function isTerminalStreamPhase(phase?: StreamRequestPhase) {
    return phase === "completed" || phase === "failed" || phase === "cancelled" || phase === "interrupted";
  }

  function getStreamRequestState(sessionId: string, requestId: string) {
    return streamRequestStates.current[streamRequestKey(sessionId, requestId)];
  }

  function setStreamRequestPhase(sessionId: string, requestId: string, phase: StreamRequestPhase) {
    if (!sessionId || !requestId) return;
    const key = streamRequestKey(sessionId, requestId);
    const current = streamRequestStates.current[key];
    if (isTerminalStreamPhase(current?.phase) && !isTerminalStreamPhase(phase)) return;
    streamRequestStates.current[key] = {
      sessionId,
      requestId,
      phase,
      updatedAt: Date.now(),
      terminalAt: isTerminalStreamPhase(phase) ? current?.terminalAt || Date.now() : current?.terminalAt,
      lastEventAt: current?.lastEventAt
    };
  }

  function clearStreamStallTimer(sessionId: string, requestId: string) {
    const key = streamRequestKey(sessionId, requestId);
    const timer = streamStallTimers.current[key];
    if (timer) {
      window.clearTimeout(timer);
      delete streamStallTimers.current[key];
    }
  }

  function streamErrorRetryable(item: StreamItem, fallback = true) {
    if (typeof item.retryable === "boolean") return item.retryable;
    const evidence = [
      item.error_taxonomy,
      item.error_type,
      item.error_code,
      item.terminal_reason,
      item.message,
      item.content
    ].filter(Boolean).join(" ").toLowerCase();
    if (!evidence) return fallback;
    if (/(auth|permission|policy|denied|forbidden|invalid|bad_request|context_overflow)/i.test(evidence)) return false;
    return /(network|timeout|rate_limit|server_error|unavailable|502|503|504|retry|MODEL_RETRY)/i.test(evidence) || fallback;
  }

  function streamFailureRecovery(
    requestId: string,
    item: StreamItem,
    fallbackMessage: string,
    kind: NonNullable<ChatItem["recovery"]>["kind"] = "failed"
  ): NonNullable<ChatItem["recovery"]> {
    const reason = item.retry_suppressed_reason || item.terminal_reason || item.error_taxonomy || item.error_type || item.error_code || "";
    const suppressedAfterOutput = Boolean(item.retry_suppressed && item.retry_suppressed_reason === "stream_output_started");
    const message = suppressedAfterOutput
      ? "网络连接中断。为避免重复执行，已暂停自动重放；可以恢复记录或准备重试。"
      : fallbackMessage || "响应中断了，可以恢复记录或准备重试。";
    return {
      kind,
      requestId,
      message,
      recoverable: item.recoverable !== false,
      retryable: streamErrorRetryable(item, true),
      reason,
      retryAfterMs: typeof item.retry_after_ms === "number" ? item.retry_after_ms : undefined,
      retryMode: item.retry_mode === "auto_retry"
        ? "manual_retry_prepare"
        : item.retry_mode || (streamErrorRetryable(item, true) ? "manual_retry_prepare" : "unavailable")
    };
  }

  function streamReconnectingRecovery(requestId: string, reason: string): NonNullable<ChatItem["recovery"]> {
    return {
      kind: "reconnecting",
      requestId,
      message: "连接中断，正在尝试接回同一次任务；如果没有恢复，可以先恢复记录。",
      recoverable: true,
      retryable: false,
      reason,
      retryMode: "stream_reconnect"
    };
  }

  function hasTransientStreamRecovery(sessionId: string, assistantId: string, requestId: string) {
    const source = activeSessionIdRef.current === sessionId
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    return source.some((message) => (
      (message.id === assistantId || (requestId && message.requestId === requestId))
      && message.recovery?.kind === "reconnecting"
    ));
  }

  function clearTransientStreamRecovery(sessionId: string, assistantId: string, requestId: string) {
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
      message.recovery?.kind === "reconnecting"
        ? { ...message, recovery: undefined }
        : message
    ));
  }

  function clearStreamReconnectState(sessionId: string, requestId?: string) {
    const prefix = requestId ? streamRequestKey(sessionId, requestId) : `${sessionId}::`;
    Object.keys(streamReconnectTimers.current).forEach((key) => {
      if (!key.startsWith(prefix)) return;
      window.clearTimeout(streamReconnectTimers.current[key]);
      delete streamReconnectTimers.current[key];
    });
    Object.keys(streamReconnectChecks.current).forEach((key) => {
      if (key.startsWith(prefix)) delete streamReconnectChecks.current[key];
    });
  }

  async function recoverRequestFromProjection(sessionId: string, assistantId: string, requestId: string) {
    if (!requestId) return false;
    let projection: RuntimeRequestProjection | null = null;
    try {
      const result = await loadRuntimeProjection({ mode: "request", requestId, sessionId });
      projection = result.mode === "request" ? result.projection : null;
    } catch {
      return false;
    }
    const decision = projectionRecoveryDecision(projection);
    if (!decision.handled) return false;
    const projectedMessages = normalizePausedMessages(mapRuntimeHistory(decision.messages, sessionId));
    const projectedAssistant = projectedMessages.find((message) => (
      message.role === "assistant" && (!message.requestId || message.requestId === requestId)
    ));
    if (!projectedAssistant) return false;
    clearStreamDeltaBuffers(sessionId, requestId);
    const projectedContent = redactInternalPromptText(decision.content || projectedAssistant.content || "");
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
      ...message,
      ...projectedAssistant,
      id: message.id || assistantId,
      requestId,
      content: projectedContent || message.content,
      pending: false,
      paused: false,
      cancelled: decision.cancelled,
      recovery: undefined
    }));
    if (decision.markCompleted) {
      markRequestLocallyCompleted(requestId);
    }
    markStreamTerminal(sessionId, requestId, decision.terminalPhase);
    markSessionOutputReady(sessionId);
    finishSessionRequest(sessionId, requestId);
    return true;
  }

  async function handleStreamError(sessionId: string, assistantId: string, requestId: string) {
    if (completedRequestIds.current[requestId]) {
      markStreamTerminal(sessionId, requestId, "completed");
      closeSessionStream(sessionId, requestId);
      return;
    }
    if (await recoverRequestFromProjection(sessionId, assistantId, requestId)) return;
    if (!isCurrentSessionRequest(sessionId, requestId)) return;
    setStreamRequestPhase(sessionId, requestId, "stalled");
    markStreamConnectionInterrupted(sessionId, assistantId, requestId);
    closeSessionStream(sessionId, requestId);
    scheduleHistoryRecovery(sessionId, requestId, [800, 2400, 5000]);
    scheduleStreamReconnect(sessionId, assistantId, requestId);
  }

  function markStreamConnectionInterrupted(sessionId: string, assistantId: string, requestId: string) {
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
      if (!message.pending && !message.visibleOutputSettled) return message;
      return {
        ...(message.pending ? replaceCurrentPhase(message, "正在重新连接") : message),
        recovery: streamReconnectingRecovery(requestId, "eventsource_error")
      };
      return {
        ...(message.pending ? replaceCurrentPhase(message, "正在重新连接") : message),
        recovery: {
          kind: "stalled",
          requestId,
          message: "连接中断，正在尝试接回同一次任务；如果没有恢复，可以先恢复记录。",
          recoverable: true,
          retryable: false,
          reason: "eventsource_error",
          retryMode: "stream_reconnect"
        }
      };
    });
  }

  function markStreamReconnectExhausted(sessionId: string, assistantId: string, requestId: string, options: { activeStillRunning?: boolean } = {}) {
    const activeStillRunning = Boolean(options.activeStillRunning);
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
      ...finishRunningSteps(message),
      requestId,
      pending: false,
      paused: false,
      cancelled: false,
      recovery: {
        kind: "failed",
        requestId,
        message: activeStillRunning
          ? "响应通道暂时没有接回，但原任务仍在运行。可以恢复记录，或先停止后再重试。"
          : "响应通道暂时没有接回。可以恢复记录或准备重试。",
        recoverable: true,
        retryable: !activeStillRunning,
        reason: activeStillRunning ? "active_stream_unavailable" : "stream_reconnect_exhausted",
        retryMode: activeStillRunning ? "stop_before_retry" : "manual_retry_prepare",
        stopAllowed: activeStillRunning
      }
    }));
  }

  function scheduleStreamStallTimer(sessionId: string, assistantId: string, requestId: string, delayMs: number) {
    clearStreamStallTimer(sessionId, requestId);
    const key = streamRequestKey(sessionId, requestId);
    streamStallTimers.current[key] = window.setTimeout(() => {
      delete streamStallTimers.current[key];
      const state = getStreamRequestState(sessionId, requestId);
      if (isTerminalStreamPhase(state?.phase) || completedRequestIds.current[requestId]) return;
      setStreamRequestPhase(sessionId, requestId, "stalled");
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
        if (!message.pending && !message.visibleOutputSettled) return message;
        return {
          ...(message.pending ? replaceCurrentPhase(message, "正在重新连接") : message),
          recovery: streamReconnectingRecovery(requestId, "stream_idle_timeout")
        };
      });
      scheduleStreamReconnect(sessionId, assistantId, requestId);
      return;
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
        if (!message.pending && !message.visibleOutputSettled) return message;
        return {
          ...(message.pending ? replaceCurrentPhase(message, "正在重新连接") : message),
          recovery: {
            kind: "stalled",
            requestId,
            message: "连接中断，正在尝试接回同一次任务；如果没有恢复，可以先恢复记录。",
            recoverable: true,
            retryable: false
          }
        };
      });
      scheduleStreamReconnect(sessionId, assistantId, requestId);
    }, delayMs);
  }

  function beginStreamRequest(sessionId: string, assistantId: string, requestId: string) {
    setStreamRequestPhase(sessionId, requestId, "connecting");
    scheduleStreamStallTimer(sessionId, assistantId, requestId, 20_000);
  }

  function observeStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const key = streamRequestKey(sessionId, requestId);
    const current = streamRequestStates.current[key];
    if (isTerminalStreamPhase(current?.phase)) return;
    streamRequestStates.current[key] = {
      sessionId,
      requestId,
      phase: item.type === "message_update" || item.type === "delta" ? "streaming" : current?.phase || "streaming",
      updatedAt: Date.now(),
      lastEventAt: Date.now()
    };
    scheduleStreamStallTimer(sessionId, assistantId, requestId, 90_000);
  }

  function markStreamTerminal(sessionId: string, requestId: string, phase: "completed" | "failed" | "cancelled" | "interrupted") {
    setStreamRequestPhase(sessionId, requestId, phase);
    clearStreamStallTimer(sessionId, requestId);
  }

  function flushStreamBoundary(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    observeStreamItem(sessionId, assistantId, requestId, item);
    const isDeltaItem = item.type === "message_update" || item.type === "delta";
    if (!isDeltaItem) {
      setStreamRequestPhase(sessionId, requestId, "flushing");
      flushStreamDeltaBuffers(sessionId, requestId);
    }
  }

  function streamItemExplicitText(item: StreamItem, keys: Array<keyof StreamItem | "final_text">) {
    const record = item as StreamItem & { final_text?: unknown };
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(record, key)) {
        return String(record[key] ?? "");
      }
    }
    return null;
  }

  function doneItemContent(item: StreamItem, currentContent: string) {
    return redactInternalPromptText(streamItemExplicitText(item, ["final_text", "content", "text", "message"]) ?? currentContent);
  }

  function isReplayGapStreamItem(item: StreamItem) {
    return item.type === "replay_gap" || item.event_type === "stream.replay_gap";
  }

  function isInterruptedStreamItem(item: StreamItem) {
    return item.type === "interrupted" || item.event_type === "run.interrupted" || item.state === "interrupted";
  }

  function shouldAcceptStreamItem(sessionId: string, requestId: string, item: StreamItem) {
    const itemRecord = item as StreamItem & { requestId?: string };
    const itemRequestId = item.request_id || itemRecord.requestId;
    if (itemRequestId && itemRequestId !== requestId) return false;
    if (completedRequestIds.current[requestId]) {
      const state = getStreamRequestState(sessionId, requestId);
      const activeTailStream = streamCleanupRequestIds.current[sessionId] === requestId;
      return isPostDoneTailItem(item)
        && (Boolean(itemRequestId) || activeTailStream || state?.phase === "text_done_tail_open");
    }
    return isCurrentSessionRequest(sessionId, requestId);
  }

  function isTerminalVoiceAttach(item: StreamItem) {
    if (item.type !== "voice_attach") return false;
    const record = item as StreamItem & { terminal?: unknown; final?: unknown; done?: unknown };
    return record.terminal === true
      || record.final === true
      || record.done === true
      || item.status === "done"
      || item.status === "completed";
  }

  function streamDeltaKey(sessionId: string, assistantId: string, requestId: string) {
    return `${sessionId}::${assistantId}::${requestId}`;
  }

  function flushBufferedDelta(key: string) {
    const buffer = streamDeltaBuffers.current[key];
    if (!buffer) return;
    if (buffer.timer !== null) {
      window.clearTimeout(buffer.timer);
      buffer.timer = null;
    }
    const text = buffer.text;
    buffer.text = "";
    if (!text) return;
    updateAssistantMessageForRequest(buffer.sessionId, buffer.assistantId, buffer.requestId, (message) => ({
      ...finishRunningSteps(message),
      content: `${message.content}${text}`,
      pending: true,
      paused: false
    }));
  }

  function enqueueAssistantDelta(sessionId: string, assistantId: string, requestId: string, rawContent: string) {
    const deltaContent = redactInternalPromptText(rawContent);
    if (!deltaContent) return;
    const key = streamDeltaKey(sessionId, assistantId, requestId);
    const buffer = streamDeltaBuffers.current[key] || {
      sessionId,
      assistantId,
      requestId,
      text: "",
      timer: null
    };
    buffer.text += deltaContent;
    streamDeltaBuffers.current[key] = buffer;
    if (buffer.timer !== null) return;
    const currentLength = activeSessionIdRef.current === sessionId
      ? messagesRef.current.find((message) => message.id === assistantId)?.content.length || 0
      : 0;
    const flushDelay = currentLength >= 100000 ? 90 : currentLength >= 30000 ? 45 : 16;
    buffer.timer = window.setTimeout(() => {
      const current = streamDeltaBuffers.current[key];
      if (current) current.timer = null;
      flushBufferedDelta(key);
    }, flushDelay);
  }

  function flushStreamDeltaBuffers(sessionId: string, requestId?: string) {
    Object.keys(streamDeltaBuffers.current).forEach((key) => {
      const buffer = streamDeltaBuffers.current[key];
      if (!buffer || buffer.sessionId !== sessionId) return;
      if (requestId && buffer.requestId !== requestId) return;
      flushBufferedDelta(key);
    });
  }

  function clearStreamDeltaBuffers(sessionId: string, requestId?: string) {
    Object.keys(streamDeltaBuffers.current).forEach((key) => {
      const buffer = streamDeltaBuffers.current[key];
      if (!buffer || buffer.sessionId !== sessionId) return;
      if (requestId && buffer.requestId !== requestId) return;
      if (buffer.timer !== null) window.clearTimeout(buffer.timer);
      delete streamDeltaBuffers.current[key];
    });
  }

  function closeSessionStream(sessionId: string, requestId?: string) {
    const cleanup = streamCleanups.current[sessionId];
    if (!cleanup) return;
    const cleanupRequestId = streamCleanupRequestIds.current[sessionId];
    if (requestId && cleanupRequestId && cleanupRequestId !== requestId) return;
    if (requestId) {
      const postDoneKey = `${sessionId}::${requestId}`;
      if (postDoneStreamCloseTimers.current[postDoneKey]) {
        window.clearTimeout(postDoneStreamCloseTimers.current[postDoneKey]);
        delete postDoneStreamCloseTimers.current[postDoneKey];
      }
      clearStreamStallTimer(sessionId, requestId);
    }
    flushStreamDeltaBuffers(sessionId, requestId);
    cleanup();
    delete streamCleanups.current[sessionId];
    delete streamCleanupRequestIds.current[sessionId];
    if (streamCleanup.current === cleanup) {
      streamCleanup.current = null;
    }
    clearStreamDeltaBuffers(sessionId, requestId);
  }

  function markRequestLocallyCompleted(requestId: string, ttlMs = 30 * 60_000) {
    if (!requestId) return;
    completedRequestIds.current[requestId] = true;
    locallyCompletedRequestIdsRef.current = {
      ...locallyCompletedRequestIdsRef.current,
      [requestId]: true
    };
    setLocallyCompletedRequestIds((current) => current[requestId] ? current : { ...current, [requestId]: true });
    if (completedRequestCleanupTimers.current[requestId]) {
      window.clearTimeout(completedRequestCleanupTimers.current[requestId]);
    }
    completedRequestCleanupTimers.current[requestId] = window.setTimeout(() => {
      delete completedRequestIds.current[requestId];
      delete postDoneTailArtifactsRef.current[requestId];
      Object.keys(streamRequestStates.current).forEach((key) => {
        if (key.endsWith(`::${requestId}`)) delete streamRequestStates.current[key];
      });
      Object.keys(streamStallTimers.current).forEach((key) => {
        if (!key.endsWith(`::${requestId}`)) return;
        window.clearTimeout(streamStallTimers.current[key]);
        delete streamStallTimers.current[key];
      });
      const nextCompleted = { ...locallyCompletedRequestIdsRef.current };
      delete nextCompleted[requestId];
      locallyCompletedRequestIdsRef.current = nextCompleted;
      setLocallyCompletedRequestIds(nextCompleted);
      delete completedRequestCleanupTimers.current[requestId];
    }, ttlMs);
  }

  function schedulePostDoneStreamClose(sessionId: string, requestId: string, delayMs = 60_000) {
    if (!sessionId || !requestId) return;
    const key = `${sessionId}::${requestId}`;
    if (postDoneStreamCloseTimers.current[key]) {
      window.clearTimeout(postDoneStreamCloseTimers.current[key]);
    }
    postDoneStreamCloseTimers.current[key] = window.setTimeout(() => {
      setStreamRequestPhase(sessionId, requestId, "completed");
      closeSessionStream(sessionId, requestId);
      delete postDoneStreamCloseTimers.current[key];
    }, delayMs);
  }

  function clearSessionRequestState(sessionId: string, requestId?: string) {
    const shouldClear = !requestId || sessionRequestIdsRef.current[sessionId] === requestId;
    setActiveRequestId((current) => (!requestId || current === requestId ? "" : current));
    if (shouldClear) {
      const nextRef = { ...sessionRequestIdsRef.current };
      delete nextRef[sessionId];
      sessionRequestIdsRef.current = nextRef;
    }
    setSessionRequestIds((current) => {
      if (requestId && current[sessionId] !== requestId) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
    if (shouldClear) {
      delete streamRetryCounts.current[sessionId];
    }
  }

  function finishSessionRequest(sessionId: string, requestId?: string) {
    flushStreamDeltaBuffers(sessionId, requestId);
    clearHistoryRecovery(sessionId, requestId);
    if (requestId) clearStreamStallTimer(sessionId, requestId);
    if (requestId) clearStreamReconnectState(sessionId, requestId);
    clearSessionRequestState(sessionId, requestId);
    closeSessionStream(sessionId, requestId);
    clearStreamDeltaBuffers(sessionId, requestId);
  }

  function scheduleStreamReconnect(sessionId: string, assistantId: string, requestId: string) {
    if (completedRequestIds.current[requestId]) return;
    if (!isCurrentSessionRequest(sessionId, requestId)) return;
    const reconnectKey = streamRequestKey(sessionId, requestId);
    if (streamReconnectTimers.current[reconnectKey] || streamReconnectChecks.current[reconnectKey]) return;
    const attempts = streamRetryCounts.current[sessionId] || 0;
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
      message.pending ? replaceCurrentPhase(message, attempts ? `正在恢复响应通道 · 第 ${attempts + 1} 次` : "正在恢复响应通道") : message
    ));
    if (attempts >= 5) {
      streamReconnectChecks.current[reconnectKey] = true;
      void (async () => {
        try {
          const snapshot = await loadRuntimeSnapshot().catch(() => null);
          const active = (snapshot?.activeRequests || []).find((request) => (
            String(request.session_id || "") === sessionId
            && String(request.request_id || "") === requestId
            && isPrimaryChatActiveRequest(request)
          ));
          if (snapshot) setRuntimeSnapshot(snapshot);
          if (active?.cancelled) {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
              ...message,
              requestId,
              pending: true,
              paused: false,
              cancelled: false
            }));
            streamReconnectTimers.current[reconnectKey] = window.setTimeout(() => {
              delete streamReconnectTimers.current[reconnectKey];
              scheduleStreamReconnect(sessionId, assistantId, requestId);
            }, 5000);
            return;
          }
          if (active && !active.cancelled) {
            const restored = active.stream_available === false
              ? await refreshSessionFromHistory(sessionId)
              : false;
            if (restored) return;
            if (active.stream_available === false) {
              markStreamReconnectExhausted(sessionId, assistantId, requestId, { activeStillRunning: true });
              markSessionOutputReady(sessionId);
              finishSessionRequest(sessionId, requestId);
              return;
            }
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
              ...message,
              requestId,
              pending: true,
              paused: false,
              cancelled: false
            }));
            streamRetryCounts.current[sessionId] = 0;
            streamReconnectTimers.current[reconnectKey] = window.setTimeout(() => {
              delete streamReconnectTimers.current[reconnectKey];
              attachMessageStream(sessionId, assistantId, requestId);
            }, 3000);
            return;
          }
          const restored = await refreshSessionFromHistory(sessionId);
          if (restored) {
            clearSessionRequestState(sessionId, requestId);
            return;
          }
          markStreamReconnectExhausted(sessionId, assistantId, requestId);
          markSessionOutputReady(sessionId);
          finishSessionRequest(sessionId, requestId);
        } finally {
          delete streamReconnectChecks.current[reconnectKey];
        }
      })();
      return;
    }
    streamRetryCounts.current[sessionId] = attempts + 1;
    streamReconnectTimers.current[reconnectKey] = window.setTimeout(() => {
      delete streamReconnectTimers.current[reconnectKey];
      if (!isCurrentSessionRequest(sessionId, requestId)) return;
      attachMessageStream(sessionId, assistantId, requestId);
    }, Math.min(1500 * (attempts + 1), 8000));
  }

  function attachMessageStream(sessionId: string, assistantId: string, requestId: string) {
    if (!requestId) return;
    if (hasScheduledStreamReconnect(sessionId, requestId)) return;
    const cachedMessages = sessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const existingMessage = cachedMessages.find((message) => (
      message.id === assistantId || (message.role === "assistant" && message.requestId === requestId)
    ));
    if (completedRequestIds.current[requestId] || locallyCompletedRequestIdsRef.current[requestId] || isTerminalAssistantMessage(existingMessage)) {
      markRequestLocallyCompleted(requestId);
      clearSessionRequestState(sessionId, requestId);
      markStreamTerminal(sessionId, requestId, "completed");
      return;
    }
    const existingRequestId = streamCleanupRequestIds.current[sessionId];
    if (existingRequestId === requestId && streamCleanups.current[sessionId]) return;
    if (existingRequestId && existingRequestId !== requestId) {
      closeSessionStream(sessionId, existingRequestId);
    }
    const hasCursor = hasMessageStreamCursor(requestId);
    if (!hasCursor) {
      updateAssistantMessage(sessionId, assistantId, (message) => (
        message.pending && message.requestId === requestId && message.content
          ? { ...message, steps: message.steps?.length ? message.steps : [{ type: "phase", content: "正在恢复响应通道" }] }
          : message
      ));
    }
    setActiveRequestId(requestId);
    sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [sessionId]: requestId };
    setSessionRequestIds((current) => ({ ...current, [sessionId]: requestId }));
    scheduleHistoryRecovery(sessionId, requestId);
    beginStreamRequest(sessionId, assistantId, requestId);
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
      ...message,
      runTiming: {
        ...(message.runTiming || { startedAtMs: Date.now() }),
        requestId,
        state: "running",
        updatedAtMs: Date.now()
      }
    }));
    const cleanup = openMessageStream({
      requestId,
      sessionId,
      webPort: sidecarStatus.webPort,
      onItem: (item) => {
        if (item.request_id && item.request_id !== requestId) return;
        if (!shouldAcceptStreamItem(sessionId, requestId, item)) return;
        flushStreamBoundary(sessionId, assistantId, requestId, item);
        rememberStreamTurnSequence(sessionId, assistantId, requestId, item);
        if (hasTransientStreamRecovery(sessionId, assistantId, requestId)) {
          clearTransientStreamRecovery(sessionId, assistantId, requestId);
        }
        if (isReplayGapStreamItem(item)) {
          handleReplayGapStreamItem(sessionId, assistantId, requestId, item);
          return;
        }
        if (isInterruptedStreamItem(item)) {
          handleInterruptedStreamItem(sessionId, assistantId, requestId, item);
          return;
        }
        if (item.type === "cancelled") {
          const terminalAtMs = Date.now();
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...finishRunningSteps(message, "cancelled"),
            content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
            pending: false,
            cancelled: true,
            runTiming: {
              ...(message.runTiming || { startedAtMs: terminalAtMs }),
              requestId,
              state: "cancelled",
              updatedAtMs: terminalAtMs,
              terminalAtMs
            }
          }));
          markStreamTerminal(sessionId, requestId, "cancelled");
          markSessionOutputReady(sessionId);
          finishSessionRequest(sessionId, requestId);
          return;
        }
        if (item.type === "done") {
          const terminalAtMs = Date.now();
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
            const nextMessage = item.artifact || item.artifacts
              ? appendArtifact(finishRunningSteps(message), item)
              : finishRunningSteps(message);
              return {
                ...clearTransientSendSteps(nextMessage),
                content: doneItemContent(item, message.content),
                pending: false,
                visibleOutputSettled: undefined,
                requestId,
                runTiming: {
                  ...(nextMessage.runTiming || { startedAtMs: terminalAtMs }),
                  requestId,
                  state: "completed",
                  updatedAtMs: terminalAtMs,
                  terminalAtMs
                },
                userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
              };
          });
          markRequestLocallyCompleted(requestId);
          setStreamRequestPhase(sessionId, requestId, "text_done_tail_open");
          markSessionOutputReady(sessionId);
          clearHistoryRecovery(sessionId, requestId);
          clearSessionRequestState(sessionId, requestId);
          schedulePostDoneStreamClose(sessionId, requestId);
          window.setTimeout(() => {
            void refreshSessionFromHistory(sessionId);
          }, 300);
          return;
        }
        if (item.type === "error") {
          const terminalAtMs = Date.now();
          const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
          const staleRequest = /invalid request_id/i.test(message);
          markStreamTerminal(sessionId, requestId, "failed");
          finishSessionRequest(sessionId, requestId);
          if (staleRequest) {
            recoverStaleRequestFromHistory(sessionId, assistantId, requestId);
          } else {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
              ...finishRunningSteps(entry, "error"),
              content: message,
              pending: false,
              paused: false,
              recovery: streamFailureRecovery(requestId, item, message),
              runTiming: {
                ...(entry.runTiming || { startedAtMs: terminalAtMs }),
                requestId,
                state: "failed",
                updatedAtMs: terminalAtMs,
                terminalAtMs
              }
            }));
            markSessionOutputReady(sessionId);
          }
          reportStreamErrorTelemetry(sessionId, requestId, message, staleRequest);
          return;
        }
        if (item.type === "reasoning" || item.type === "thinking") {
          const chunk = item.content || item.text || "";
          if (chunk) {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendReasoningStep(message, chunk));
          }
          return;
        }
        if (item.type === "message_end") {
          if (item.has_tool_calls) {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => flushIntermediateContent(finishRunningSteps(message)));
          }
          return;
        }
        if (item.type === "tool_permission_request") {
          const permissionRequestId = item.permission_request_id || "";
          if (!permissionRequestId) return;
          setApproval({
            type: "permission",
            title: item.title || "本机工具执行前确认",
            message: item.message || `EcoreX 将执行 ${item.tool || "tool"}，请确认是否允许。`,
            actions: [
              {
                label: "允许本次",
                primary: true,
                onClick: () => void answerToolPermission(permissionRequestId, "allow_once")
              },
              {
                label: "始终允许",
                onClick: () => void answerToolPermission(permissionRequestId, "always_allow")
              },
              {
                label: "拒绝",
                onClick: () => void answerToolPermission(permissionRequestId, "deny")
              }
            ]
          });
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...replaceCurrentPhase(message, `等待本机工具授权：${item.tool || "tool"}`),
            pending: true,
            paused: false
          }));
          return;
        }
        if (item.type === "tool_start") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolStart(message, item));
          return;
        }
        if (item.type === "tool_heartbeat") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolHeartbeat(message, item));
          return;
        }
        if (item.type === "tool_deadline_extended") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolDeadlineExtended(message, item));
          return;
        }
        if (item.type === "tool_end") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolEnd(message, item));
          return;
        }
        if (item.type === "artifact") {
          const postDoneTail = Boolean(completedRequestIds.current[requestId]);
          if (postDoneTail) rememberPostDoneTailArtifacts(requestId, item);
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
            const next = appendArtifact(message, item, false);
            return next === message ? message : settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail });
          });
          markSessionOutputReady(sessionId);
          if (postDoneTail) {
            window.setTimeout(() => {
              void refreshSessionFromHistory(sessionId);
            }, 0);
          }
          return;
        }
        if (item.type === "image" || item.type === "video" || item.type === "audio" || item.type === "file" || item.type === "voice_attach") {
          const postDoneTail = Boolean(completedRequestIds.current[requestId]);
          const terminalVoiceAttach = isTerminalVoiceAttach(item);
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
            const visibleTerminalOutput = item.type !== "voice_attach" || terminalVoiceAttach;
            const next = appendMediaStep(message, item, !visibleTerminalOutput);
            return visibleTerminalOutput
              ? settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail && !terminalVoiceAttach })
              : next;
          });
          if (item.type !== "voice_attach" || terminalVoiceAttach) {
            markSessionOutputReady(sessionId);
            if (terminalVoiceAttach) {
              markRequestLocallyCompleted(requestId);
              setStreamRequestPhase(sessionId, requestId, "text_done_tail_open");
              clearHistoryRecovery(sessionId, requestId);
              clearSessionRequestState(sessionId, requestId);
              schedulePostDoneStreamClose(sessionId, requestId);
            }
          }
          return;
        }
        if (item.type === "phase" && (item.content || item.message)) {
          const phaseContent = redactInternalPromptText(item.content || item.message || "");
          if (!phaseContent) return;
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
            message.visibleOutputSettled && isTransientPhaseContent(phaseContent)
              ? message
              : replaceCurrentPhase(message, phaseContent)
          ));
          return;
        }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(sessionId, assistantId, requestId, streamItemText(item));
            }
      },
      onError: () => {
        void handleStreamError(sessionId, assistantId, requestId);
      }
      });
    streamCleanup.current = cleanup;
    streamCleanups.current[sessionId] = cleanup;
    streamCleanupRequestIds.current[sessionId] = requestId;
  }

  function isCompactCommand(text: string) {
    return /^\/(?:compact|context\s+clear)$/i.test(text.trim());
  }

  async function runCompactCommand(text: string) {
    const requestSessionId = activeSessionId;
    const createdAt = new Date().toISOString();
    const userMessage: ChatItem = {
      id: `u-compact-${Date.now()}`,
      role: "user",
      content: text || "/compact",
      createdAt
    };
    const assistantId = `a-compact-${Date.now()}`;
    setComposerDraft("", { immediate: true });
    setAttachments([]);
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      userMessage,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt,
        steps: [{ type: "phase", content: "正在压缩上下文" }]
      }
    ]);
    try {
      const contextStartSeq = await clearRuntimeContext(requestSessionId);
      updateSessionMessages(requestSessionId, (current) => current.map((message) => {
        if (message.id === userMessage.id) return message;
        if (message.id === assistantId) {
          return {
            ...finishRunningSteps(message),
            content: "已压缩上下文。",
            pending: false,
            paused: false
          };
        }
        return { ...message, contextExcluded: true };
      }));
      setSessionUiState((current) => ({
        ...current,
        [requestSessionId]: {
          ...(current[requestSessionId] || {
            title: activeSessionTitle,
            projectId: sessionProjects[requestSessionId] || null,
            messages: [],
            composerText: "",
            attachments: []
          }),
          contextStartSeq,
          lastActivityAt: Date.now()
        }
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : "压缩上下文失败";
      updateAssistantMessage(requestSessionId, assistantId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false
      }));
    }
  }

  async function sendNow(skipCapabilityCheck = false) {
    commitComposerDraft(composerTextRef.current);
    const text = composerTextRef.current.trim();
    if (!text && !attachments.length) return;
    const previousRequestId = activeSessionRequestId;
    const previousSessionId = activeSessionId;

    if (isCompactCommand(text) && !attachments.length) {
      await runCompactCommand(text);
      return;
    }

    const enabledPacks = packs.filter((pack) => capabilityPackEnabled(pack.id));
    const neededPack = skipCapabilityCheck ? null : detectNeededPack(text, attachments, enabledPacks);

    let requestSessionId = activeSessionId;
    const pendingProject = pendingProjectStartRef.current || (isPendingProjectSessionId(activeSessionId) ? activeProject : null);
    const pendingProjectSessionId = isPendingProjectSessionId(requestSessionId) ? requestSessionId : "";
    let projectBindingForRequest = projectBindingForSession(requestSessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, sessionUiState, projectCatalog);
    let projectForRequest: ProjectFolder | null = pendingProject || (projectBindingForRequest ? projectFolderFromBinding(projectBindingForRequest) : null);
    if (pendingProject && isPendingProjectSessionId(requestSessionId)) {
      requestSessionId = `ecorex-project-${pendingProject.id}-${Date.now()}`;
      projectForRequest = pendingProject;
      projectBindingForRequest = projectBindingFromProject(pendingProject, "project-new-session");
      activeSessionIdRef.current = requestSessionId;
      pendingProjectStartRef.current = null;
      setPendingProjectStart(null);
      setActiveSessionId(requestSessionId);
      setActiveProjectId(pendingProject.id);
      const projectTitle = `${pendingProject.name} · 项目会话`;
      setActiveSessionTitle(projectTitle);
      setSessionTitles((current) => ({ ...current, [requestSessionId]: projectTitle }));
      bindSessionToProject(requestSessionId, projectBindingForRequest, "project-new-session");
      if (pendingProjectSessionId && pendingProjectSessionId !== requestSessionId) {
        clearBlankDraftProtection(pendingProjectSessionId);
        const nextSessionProjects = { ...sessionProjectsRef.current };
        delete nextSessionProjects[pendingProjectSessionId];
        sessionProjectsRef.current = nextSessionProjects;
        setSessionProjects(nextSessionProjects);
        const nextSessionProjectBindings = { ...sessionProjectBindingsRef.current };
        delete nextSessionProjectBindings[pendingProjectSessionId];
        sessionProjectBindingsRef.current = nextSessionProjectBindings;
        setSessionProjectBindings(nextSessionProjectBindings);
        setSessionTitles((current) => {
          const next = { ...current };
          delete next[pendingProjectSessionId];
          return next;
        });
        setSessionUiState((current) => {
          if (!current[pendingProjectSessionId]) return current;
          const next = { ...current };
          delete next[pendingProjectSessionId];
          return next;
        });
      }
    } else if (projectBindingForRequest) {
      projectForRequest = projectFolderFromBinding(projectBindingForRequest);
      projectBindingForRequest = bindSessionToProject(requestSessionId, projectBindingForRequest, "project-session-send")
        || projectBindingForRequest;
    }

    const projectAttachment: FileAttachment | null = projectForRequest
      ? {
          file_path: projectForRequest.path,
          file_name: projectForRequest.name,
          file_type: "directory"
        }
      : null;
    const outboundAttachments = projectAttachment
      ? [projectAttachment, ...attachments.filter((file) => file.file_path !== projectAttachment.file_path)]
      : attachments;
    const displayText = text || "请处理这些附件";
    let hiddenContext = "";

    let estimatedTokens = estimateTokens(`${hiddenContext}\n\n${displayText}`.trim(), outboundAttachments);
    let streamTextChars = 0;
    let streamToolChars = 0;
    let streamSawDelta = false;
    const observeStreamUsage = (item: StreamItem) => {
      const textPart = String(item.content || item.text || item.message || "");
      if (item.type === "delta" || item.type === "message_update" || item.type === "reasoning" || item.type === "thinking" || item.type === "phase") {
        streamTextChars += textPart.length;
        if (item.type === "delta" || item.type === "message_update") {
          streamSawDelta = true;
        }
      }
      if (item.type === "done" && !streamSawDelta) {
        streamTextChars += textPart.length;
      }
      if (item.type === "tool_start" || item.type === "tool_heartbeat" || item.type === "tool_end") {
        streamToolChars += 80;
        try {
          streamToolChars += JSON.stringify(item.arguments ?? item.input ?? item.result ?? item.content ?? "").length;
        } catch {
          streamToolChars += String(item.result ?? item.content ?? "").length;
        }
      }
      if (item.type === "file" || item.type === "image" || item.type === "video" || item.type === "voice_attach") {
        streamToolChars += 120;
      }
    };
    const liveEstimatedTokens = () => estimatedTokens + Math.ceil(streamTextChars / 2) + Math.ceil(streamToolChars / 3);
    const userMessage: ChatItem = {
      id: `u-${Date.now()}`,
      role: "user",
      content: text || "请处理这些附件",
      attachments,
      createdAt: new Date().toISOString()
    };
    const assistantId = `a-${Date.now()}`;
    const runStartedAtMs = Date.now();
    const clientAttemptId = `attempt-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    latestSendAttemptRef.current[requestSessionId] = clientAttemptId;
    const { generation: sendGeneration, controller: preflightController } = beginSessionPreflight(requestSessionId);
    const restoreUnacceptedDraft = (message: string, result?: ChatSendResult) => {
      if (latestSendAttemptRef.current[requestSessionId] !== clientAttemptId) return;
      clearAssistantPhaseTimers(assistantId);
      clearSessionPreflight(requestSessionId, preflightController);
      updateSessionMessages(requestSessionId, (current) => current.filter((item) => item.id !== userMessage.id && item.id !== assistantId));
      setComposerDraft(text, { immediate: true });
      setAttachments(attachments);
      const actions: Array<{ label: string; primary?: boolean; onClick: () => void }> = [
          {
            label: "重试发送",
            primary: true,
            onClick: () => {
              setApproval(null);
              void sendNow(skipCapabilityCheck);
            }
          }
        ];
      if (isModelConfigSendError(result)) {
        actions.push({
          label: "重新登录",
          onClick: () => {
            setApproval(null);
            void logout();
          }
        });
      }
      actions.push({
        label: "保留草稿",
        onClick: () => setApproval(null)
      });
      setApproval({
        type: "info",
        title: "消息未发送",
        message,
        actions
      });
      markSessionOutputReady(requestSessionId);
    };
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      {
        ...userMessage,
        sendAttempt: {
          id: clientAttemptId,
          state: previousRequestId ? "stopping-previous" : "sending",
          interruptsRequestId: previousRequestId || undefined
        }
      },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt: new Date().toISOString(),
        runTiming: {
          state: "sending",
          startedAtMs: runStartedAtMs,
          updatedAtMs: runStartedAtMs
        },
        sendAttempt: {
          id: clientAttemptId,
          state: previousRequestId ? "stopping-previous" : "sending",
          interruptsRequestId: previousRequestId || undefined
        },
        steps: [{ type: "phase", content: "正在发送" }]
      }
    ]);
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 800, "已收到，正在准备响应");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 1800, "正在检查额度");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 3600, "正在建立响应通道");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 6500, "正在组织上下文");
    if (!lockedSessionTitlesRef.current[requestSessionId]) {
      setActiveSessionTitle((current) => {
        const nextTitle = current === NEW_SESSION_START_TITLE || current === "新对话" || current.endsWith(`· ${NEW_SESSION_START_TITLE}`) || current.endsWith("· 新会话") || current.endsWith("· 项目会话")
          ? shortTitle(text || projectForRequest?.name || "项目会话")
          : current;
        setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
        return nextTitle;
      });
    }
    setComposerDraft("", { immediate: true });
    setAttachments([]);
    setApproval(null);

    if (neededPack?.policyMode === "disabled") {
      restoreUnacceptedDraft(`${neededPack.name} is disabled by policy. Please ask an administrator to enable or preinstall it.`);
      return;
    }

    if (neededPack) {
      updateAssistantMessage(requestSessionId, assistantId, (message) => replaceCurrentPhase(
        message,
        isConfigureOnlyCapability(neededPack)
          ? `Preparing configuration check for ${neededPack.name}`
          : neededPack.discoveryOnly
          ? `Preparing find-skill discovery for ${neededPack.name}`
          : `Preparing capability setup for ${neededPack.name}`
      ));
      try {
        const request = await requestAgentInstallRequest({
          packId: neededPack.id,
          packName: neededPack.name,
          sessionId: requestSessionId
        });
        if (request.status === "error" || !request.prompt) {
          throw new Error(request.message || "Failed to prepare capability setup task");
        }
        const capabilityContext = [
          "Internal capability preflight:",
          "The visible user turn has already been recorded and must remain the only user request.",
          "Do not restate this preflight as user-authored content.",
          "Run required discovery or install steps as assistant/tool progress, then continue the original user request.",
          request.prompt
        ].join("\n");
        hiddenContext = [hiddenContext, capabilityContext].filter(Boolean).join("\n\n");
        estimatedTokens = estimateTokens(`${hiddenContext}\n\n${displayText}`.trim(), outboundAttachments);
        setInstallNotice({
          packId: neededPack.id,
          packName: neededPack.name,
          message: isConfigureOnlyCapability(neededPack)
            ? `${neededPack.name} will be checked/configured through the structured agent path in this response.`
            : neededPack.discoveryOnly
            ? `${neededPack.name} will be handled through find skill in this response.`
            : `${neededPack.name} setup will run inside this response.`
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to prepare capability setup task";
        restoreUnacceptedDraft(message);
        return;
      }
    }

    const quota = await checkEnterpriseQuota(estimatedTokens).catch((error) => {
      setToast(error instanceof Error ? `额度检查暂不可用，已继续发送：${error.message}` : "额度检查暂不可用，已继续发送");
      return { ok: true, quota: { allowed: true } } as EnterpriseQuotaCheckResult;
    });
    if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
      clearAssistantPhaseTimers(assistantId);
      return;
    }
    if (quota.quota) {
      setQuotaSnapshot(quota.quota);
    }
    if (quota.quota && quota.quota.allowed === false) {
      const quotaMessage = quota.quota.reason || "当前账号暂时不能继续发送。";
      const authFailure = isEnterpriseAuthFailure(quota.quota) && !isQuotaLimitFailure(quota.quota);
      restoreUnacceptedDraft(authFailure ? `${quotaMessage} Please sign in again before sending.` : quotaMessage);
      return;
      setApproval({
        type: authFailure ? "error" : "quota",
        title: authFailure ? "登录状态异常" : "额度已达到上限",
        message: authFailure ? `${quotaMessage}。请重新登录后继续。` : quotaMessage,
        actions: authFailure ? [
          {
            label: "重新登录",
            primary: true,
            onClick: () => void logout()
          },
          {
            label: "知道了",
            onClick: () => setApproval(null)
          }
        ] : undefined
      });
      updateAssistantMessage(requestSessionId, assistantId, (message) => ({
        ...finishRunningSteps(message, "error"),
        content: authFailure ? "登录状态异常，请重新登录后继续。" : quotaMessage,
        pending: false
      }));
      clearAssistantPhaseTimers(assistantId);
      clearSessionPreflight(requestSessionId, preflightController);
      markSessionOutputReady(requestSessionId);
      return;
    }

    let usageReported = false;
    const reportChatUsage = (usage: TokenUsage | undefined, source: "provider" | "estimated") => {
      if (usageReported) return;
      usageReported = true;
      const providerTotal = usageTotal(usage);
      const localEstimate = liveEstimatedTokens();
      const totalTokens = Math.max(providerTotal, localEstimate, estimatedTokens);
      const usageSource = providerTotal >= localEstimate && providerTotal > 0 ? source : "estimated";
      void reportDesktopEvent({
        type: "usage",
        source: "Desktop",
        category: "chat",
        label: "message",
        amount: totalTokens,
        sessionId: requestSessionId,
        detail: {
          inputTokens: usage?.inputTokens || estimatedTokens,
          outputTokens: usage?.outputTokens || Math.max(0, totalTokens - estimatedTokens),
          totalTokens,
          model: usage?.model || currentModelName,
          provider: usage?.provider || "",
          estimatedTokens,
          streamEstimatedTokens: localEstimate,
          providerTotalTokens: providerTotal,
          usageSource
        }
      });
      setQuotaSnapshot((current) => current ? {
        ...current,
        dailyUsed: quotaNumber(current, "dailyUsed") + totalTokens,
        weeklyUsed: quotaNumber(current, "weeklyUsed") + totalTokens
      } : current);
      void checkEnterpriseQuota(0)
        .then((next) => {
          if (next.quota) setQuotaSnapshot(next.quota);
        })
        .catch(() => undefined);
    };

    try {
      const result = await sendChatMessage({
        sessionId: requestSessionId,
        message: displayText,
        hiddenContext,
        projectContext: projectBindingForRequest || null,
        attachments: outboundAttachments,
        clientAttemptId,
        interruptsRequestId: previousRequestId || undefined
      });
      if (latestSendAttemptRef.current[requestSessionId] !== clientAttemptId) {
        clearAssistantPhaseTimers(assistantId);
        if (result.request_id) {
          void cancelChatRequest({ requestId: result.request_id, sessionId: requestSessionId }).catch(() => undefined);
        }
        updateSessionMessages(requestSessionId, (current) => current.filter((item) => item.id !== userMessage.id && item.id !== assistantId));
        return;
      }
      if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
        clearAssistantPhaseTimers(assistantId);
        if (result.request_id) {
          void cancelChatRequest({ requestId: result.request_id, sessionId: requestSessionId }).catch(() => undefined);
        }
        return;
      }
      if (result.status === "error") {
        const message = chatSendErrorMessage(result);
        if (isRetryableConcurrencyResult(result)) {
          void reportDesktopEvent({
            type: "warn",
            source: "Desktop",
            category: "runtime",
            label: "request_conflict_retryable",
            message,
            sessionId: requestSessionId,
            detail: {
              code: result.code || "",
              errorType: result.error_type || "",
              state: result.state || "",
              retryAfterMs: result.retry_after_ms || 0,
              activeRequestIds: result.active_request_ids || []
            }
          });
        }
        restoreUnacceptedDraft(message, result);
        return;
      }
      updateSessionMessages(requestSessionId, (current) => current.map((item) => (
        item.id === userMessage.id || item.id === assistantId
          ? { ...item, sendAttempt: item.sendAttempt ? { ...item.sendAttempt, state: "accepted" } : undefined }
          : item
      )));
      const replacedRequestIds = result.same_session?.decision === "replacement_accepted" || result.same_session?.decision === "accepted_after_recovery"
        ? Array.from(new Set([previousRequestId, ...(result.same_session?.replaced_request_ids || [])].filter(Boolean)))
        : [];
      if (replacedRequestIds.length) {
        replacedRequestIds.forEach((replacedRequestId) => {
          closeSessionStream(previousSessionId, replacedRequestId);
          clearSessionRequestState(previousSessionId, replacedRequestId);
        });
        updateSessionMessages(previousSessionId, (current) => current.map((item) => (
          item.role === "assistant" && item.requestId && replacedRequestIds.includes(item.requestId) && item.pending
            ? {
                ...finishRunningSteps(item, "cancelled"),
                content: item.content || "已切换到新的消息处理。",
                pending: false,
                cancelled: true
              }
            : item
        )));
      }
      if (result.inline_reply) {
        const inlineReply = redactInternalPromptText(result.inline_reply || "");
        streamTextChars += inlineReply.length;
        const terminalAtMs = Date.now();
        updateSessionMessages(requestSessionId, (current) => current.map((item) => item.id === assistantId ? {
          ...clearTransientSendSteps(finishRunningSteps(item)),
          content: inlineReply,
          pending: false,
          runTiming: {
            ...(item.runTiming || { startedAtMs: terminalAtMs }),
            state: "completed",
            updatedAtMs: terminalAtMs,
            terminalAtMs
          }
        } : item));
        markSessionOutputReady(requestSessionId);
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
      }
      if (result.request_id && result.stream) {
        const requestId = result.request_id;
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
        setActiveRequestId(requestId);
        sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [requestSessionId]: requestId };
        setSessionRequestIds((current) => ({ ...current, [requestSessionId]: requestId }));
        streamRetryCounts.current[requestSessionId] = 0;
        scheduleHistoryRecovery(requestSessionId, requestId);
        beginStreamRequest(requestSessionId, assistantId, requestId);
        updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
          ...replaceCurrentPhase(message, "正在连接响应"),
          requestId,
          runTiming: {
            ...(message.runTiming || { startedAtMs: Date.now() }),
            requestId,
            state: "running",
            updatedAtMs: Date.now()
          }
        }));
        const cleanup = openMessageStream({
          requestId,
          sessionId: requestSessionId,
          webPort: sidecarStatus.webPort,
          onItem: (item) => {
            if (item.request_id && item.request_id !== requestId) return;
            if (!shouldAcceptStreamItem(requestSessionId, requestId, item)) return;
            observeStreamUsage(item);
            flushStreamBoundary(requestSessionId, assistantId, requestId, item);
            const sessionId = requestSessionId;
            rememberStreamTurnSequence(sessionId, assistantId, requestId, item);
            if (hasTransientStreamRecovery(sessionId, assistantId, requestId)) {
              clearTransientStreamRecovery(sessionId, assistantId, requestId);
            }
            if (isReplayGapStreamItem(item)) {
              handleReplayGapStreamItem(requestSessionId, assistantId, requestId, item);
              return;
            }
            if (isInterruptedStreamItem(item)) {
              handleInterruptedStreamItem(requestSessionId, assistantId, requestId, item);
              return;
            }
            if (item.type === "cancelled") {
              const terminalAtMs = Date.now();
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
                ...finishRunningSteps(message, "cancelled"),
                content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
                pending: false,
                cancelled: true,
                runTiming: {
                  ...(message.runTiming || { startedAtMs: terminalAtMs }),
                  requestId,
                  state: "cancelled",
                  updatedAtMs: terminalAtMs,
                  terminalAtMs
                }
              }));
              markStreamTerminal(requestSessionId, requestId, "cancelled");
              markSessionOutputReady(requestSessionId);
              finishSessionRequest(requestSessionId, requestId);
              return;
            }
            if (item.type === "done") {
              const terminalAtMs = Date.now();
              if (typeof item.user_seq === "number") {
                updateSessionMessages(requestSessionId, (current) => current.map((message) => (
                  message.id === userMessage.id ? { ...message, userSeq: item.user_seq } : message
                )));
              }
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => {
                const nextMessage = item.artifact || item.artifacts
                  ? appendArtifact(finishRunningSteps(message), item)
                  : finishRunningSteps(message);
                return {
                  ...clearTransientSendSteps(nextMessage),
                  content: doneItemContent(item, message.content),
                  pending: false,
                  visibleOutputSettled: undefined,
                  requestId,
                  recovery: undefined,
                  runTiming: {
                    ...(nextMessage.runTiming || { startedAtMs: terminalAtMs }),
                    requestId,
                    state: "completed",
                    updatedAtMs: terminalAtMs,
                    terminalAtMs
                  },
                  userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                  botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
                };
              });
              markRequestLocallyCompleted(requestId);
              setStreamRequestPhase(requestSessionId, requestId, "text_done_tail_open");
              markSessionOutputReady(requestSessionId);
              clearHistoryRecovery(requestSessionId, requestId);
              clearSessionRequestState(requestSessionId, requestId);
              schedulePostDoneStreamClose(requestSessionId, requestId);
              window.setTimeout(() => {
                void refreshSessionFromHistory(requestSessionId);
              }, 300);
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              return;
            }
            if (item.type === "error") {
              const terminalAtMs = Date.now();
              const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
              const staleRequest = /invalid request_id/i.test(message);
              markStreamTerminal(requestSessionId, requestId, "failed");
              finishSessionRequest(requestSessionId, requestId);
              if (staleRequest) {
                recoverStaleRequestFromHistory(requestSessionId, assistantId, requestId);
              } else {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (entry) => ({
                  ...finishRunningSteps(entry, "error"),
                  content: message,
                  pending: false,
                  paused: false,
                  recovery: streamFailureRecovery(requestId, item, message),
                  runTiming: {
                    ...(entry.runTiming || { startedAtMs: terminalAtMs }),
                    requestId,
                    state: "failed",
                    updatedAtMs: terminalAtMs,
                    terminalAtMs
                  }
                }));
                markSessionOutputReady(requestSessionId);
              }
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              reportStreamErrorTelemetry(requestSessionId, requestId, message, staleRequest);
              return;
            }
            if (item.type === "reasoning" || item.type === "thinking") {
              const chunk = item.content || item.text || "";
              if (chunk) {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendReasoningStep(message, chunk));
              }
              return;
            }
            if (item.type === "message_end") {
              if (item.has_tool_calls) {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => flushIntermediateContent(finishRunningSteps(message)));
              }
              return;
            }
            if (item.type === "tool_permission_request") {
              const permissionRequestId = item.permission_request_id || "";
              if (!permissionRequestId) return;
              setApproval({
                type: "permission",
                title: item.title || "本机工具执行前确认",
                message: item.message || `EcoreX 将执行 ${item.tool || "tool"}，请确认是否允许。`,
                actions: [
                  {
                    label: "允许本次",
                    primary: true,
                    onClick: () => void answerToolPermission(permissionRequestId, "allow_once")
                  },
                  {
                    label: "始终允许",
                    onClick: () => void answerToolPermission(permissionRequestId, "always_allow")
                  },
                  {
                    label: "拒绝",
                    onClick: () => void answerToolPermission(permissionRequestId, "deny")
                  }
                ]
              });
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
                ...replaceCurrentPhase(message, `等待本机工具授权：${item.tool || "tool"}`),
                pending: true
              }));
              return;
            }
            if (item.type === "tool_start") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolStart(message, item));
              return;
            }
            if (item.type === "tool_heartbeat") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolHeartbeat(message, item));
              return;
            }
            if (item.type === "tool_deadline_extended") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolDeadlineExtended(message, item));
              return;
            }
            if (item.type === "tool_end") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolEnd(message, item));
              return;
            }
            if (item.type === "artifact") {
              const postDoneTail = Boolean(completedRequestIds.current[requestId]);
              if (postDoneTail) rememberPostDoneTailArtifacts(requestId, item);
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => {
                const next = appendArtifact(message, item, false);
                return next === message ? message : settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail });
              });
              markSessionOutputReady(requestSessionId);
              if (postDoneTail) {
                window.setTimeout(() => {
                  void refreshSessionFromHistory(requestSessionId);
                }, 0);
              }
              return;
            }
            if (item.type === "image" || item.type === "video" || item.type === "audio" || item.type === "file" || item.type === "voice_attach") {
              const postDoneTail = Boolean(completedRequestIds.current[requestId]);
              const terminalVoiceAttach = isTerminalVoiceAttach(item);
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => {
                const visibleTerminalOutput = item.type !== "voice_attach" || terminalVoiceAttach;
                const next = appendMediaStep(message, item, !visibleTerminalOutput);
                return visibleTerminalOutput
                  ? settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail && !terminalVoiceAttach })
                  : next;
              });
              if (item.type !== "voice_attach" || terminalVoiceAttach) {
                markSessionOutputReady(requestSessionId);
                if (terminalVoiceAttach) {
                  markRequestLocallyCompleted(requestId);
                  setStreamRequestPhase(requestSessionId, requestId, "text_done_tail_open");
                  clearHistoryRecovery(requestSessionId, requestId);
                  clearSessionRequestState(requestSessionId, requestId);
                  schedulePostDoneStreamClose(requestSessionId, requestId);
                }
              }
              return;
            }
            if (item.type === "phase" && (item.content || item.message)) {
              const phaseContent = redactInternalPromptText(item.content || item.message || "");
              if (!phaseContent) return;
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => (
                message.visibleOutputSettled && isTransientPhaseContent(phaseContent)
                  ? message
                  : replaceCurrentPhase(message, phaseContent)
              ));
              return;
            }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(requestSessionId, assistantId, requestId, streamItemText(item));
            }
          },
          onError: () => {
            void handleStreamError(requestSessionId, assistantId, requestId);
          }
        });
        streamCleanup.current = cleanup;
        streamCleanups.current[requestSessionId] = cleanup;
        streamCleanupRequestIds.current[requestSessionId] = requestId;
      } else if (!result.inline_reply) {
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
      }
      if (!lockedSessionTitlesRef.current[requestSessionId]) {
        generateSessionTitle({ sessionId: requestSessionId, userMessage: text || projectForRequest?.name || "项目会话" }).then((title) => {
          if (!title || lockedSessionTitlesRef.current[requestSessionId]) return;
          setSessionTitles((current) => ({ ...current, [requestSessionId]: title }));
          setSessionUiState((current) => ({
            ...current,
            [requestSessionId]: {
              ...(current[requestSessionId] || {
                messages: [],
                composerText: "",
                attachments: []
              }),
              projectId: projectBindingForRequest?.projectId || sessionProjectIdFromState(requestSessionId, sessionProjectsRef.current, current),
              projectBinding: projectBindingForRequest,
              title
            }
          }));
          if (activeSessionIdRef.current === requestSessionId) {
            setActiveSessionTitle(title);
          }
        }).catch(() => undefined);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "发送失败";
      if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
        clearAssistantPhaseTimers(assistantId);
        return;
      }
      restoreUnacceptedDraft(message);
      finishSessionRequest(requestSessionId);
      void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: requestSessionId });
    }
  }

  async function stopActiveRequest() {
    abortSessionPreflight(activeSessionId);
    clearAllPhaseTimers();
    const requestId = activeSessionRequestId;
    try {
      if (requestId) {
        await cancelChatRequest({ requestId, sessionId: activeSessionId });
      }
    } catch (error) {
      console.warn("[EcoreX] Failed to cancel active request", error);
    } finally {
      setApproval(null);
      closeSessionStream(activeSessionId, requestId);
      clearSessionRequestState(activeSessionId, requestId);
      updateSessionMessages(activeSessionId, (current) => current.map((message) => message.pending ? {
        ...finishRunningSteps(message, "cancelled"),
        content: message.content || "已停止",
        pending: false,
        cancelled: true
      } : message));
    }
  }

  function handleComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
    const isMeta = event.metaKey || event.ctrlKey;
    if (event.key === "Enter" && isMeta) {
      event.preventDefault();
      insertComposerNewline(event.currentTarget);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendNow();
      return;
    }
  }

  async function syncRuntimeUiStateNow() {
    const binding = isPendingProjectSessionId(activeSessionId)
      ? null
      : projectBindingForSession(activeSessionId, sessionProjectBindings, sessionProjects, sessionUiState, projectCatalog);
    const projectId = binding?.projectId || sessionProjectIdFromState(activeSessionId, sessionProjects, sessionUiState);
    const mergedState = pruneSessionUiState({
      ...sessionUiState,
      [activeSessionId]: {
        ...(sessionUiState[activeSessionId] || {}),
        title: activeSessionTitle,
        projectId,
        projectBinding: binding,
        messages,
        composerText: composerTextRef.current,
        attachments,
        contextStartSeq: sessionUiState[activeSessionId]?.contextStartSeq,
        lastActivityAt: latestMessageMs(messages) || sessionUiState[activeSessionId]?.lastActivityAt || Date.now()
      }
    });
    writeStorage(SESSION_UI_STORAGE_KEY, mergedState);
    if (sidecarStatus.state !== "running") return;
    await saveRuntimeUiState({
      version: 1,
      replaceProjectState: false,
      projectStateMode: "merge",
      lastActiveSessionId: activeSessionId,
      activeProjectId: projectId,
      projects,
      sessionProjects,
      sessionProjectBindings,
      sessionTitles,
      pinnedSessions,
      pinnedSessionTimes,
      pinnedProjects,
      sessionUiState: mergedState,
      savedAt: new Date().toISOString()
    }).catch(() => undefined);
  }

  function packActionLabel(pack: CapabilityPack, installing = false) {
    if (isConfigureOnlyCapability(pack)) {
      return installing ? "正在检查" : "配置检查";
    }
    if (pack.discoveryOnly) {
      return installing ? "正在用 find skill" : "用 find skill";
    }
    return installing ? "正在安装" : "安装";
  }

  function shouldOpenCapabilitySettings(pack: CapabilityPack) {
    return !pack.discoveryOnly;
  }

  function packTaskNoun(pack: CapabilityPack) {
    if (isConfigureOnlyCapability(pack)) return "配置检查任务";
    return pack.discoveryOnly ? "find skill 任务" : "安装任务";
  }

  function isDefaultReadOnlyCapability(pack: CapabilityPack) {
    return pack.defaultEnabled === true && pack.readOnly === true;
  }

  function isConfigureOnlyCapability(pack: CapabilityPack) {
    return pack.configureOnly === true || isDefaultReadOnlyCapability(pack);
  }

  function packReadyLabel(pack: CapabilityPack) {
    return isDefaultReadOnlyCapability(pack) ? "默认只读" : "已安装";
  }

  function watchAgentPackInstall(pack: CapabilityPack, onInstalled?: () => void) {
    if (installWatchers.current[pack.id]) return;
    const started = Date.now();
    const timer = window.setInterval(async () => {
      const nextPacks = await listCapabilityPacks().catch(() => null);
      if (nextPacks) {
        setPacks(nextPacks);
        const nextPack = nextPacks.find((item) => item.id === pack.id);
        if (nextPack?.installed) {
          window.clearInterval(timer);
          delete installWatchers.current[pack.id];
          setInstallingPackIds((current) => {
            const next = { ...current };
            delete next[pack.id];
            return next;
          });
          setInstallNotice((current) => current?.packId === pack.id ? null : current);
          setToast(`${pack.name} 已安装`);
          void loadRuntimeSnapshot().then(setRuntimeSnapshot).catch(() => undefined);
          onInstalled?.();
          return;
        }
        if (nextPack?.state === "failed") {
          window.clearInterval(timer);
          delete installWatchers.current[pack.id];
          setInstallingPackIds((current) => {
            const next = { ...current };
            delete next[pack.id];
            return next;
          });
          setInstallNotice((current) => current?.packId === pack.id ? null : current);
          setToast(nextPack.message || `${pack.name} 安装失败，请查看当前会话诊断`);
          return;
        }
      }
      if (Date.now() - started > 10 * 60 * 1000) {
        window.clearInterval(timer);
        delete installWatchers.current[pack.id];
        setInstallingPackIds((current) => {
          const next = { ...current };
          delete next[pack.id];
          return next;
        });
        setToast(`${pack.name} 安装未确认，请查看当前会话结果`);
      }
    }, 2000);
    installWatchers.current[pack.id] = timer;
  }

  async function startAgentInstallChatTask(pack: CapabilityPack, prompt: string, sessionId: string) {
    const requestSessionId = sessionId;
    const taskLabel = isConfigureOnlyCapability(pack)
      ? `配置检查能力：${pack.name}`
      : pack.discoveryOnly ? `用 find skill 发现能力：${pack.name}` : `安装能力包：${pack.name}`;
    const assistantId = `a-install-${Date.now()}`;
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt: new Date().toISOString(),
        steps: [{ type: "phase", content: taskLabel }]
      }
    ]);
    const currentTitle = sessionTitles[requestSessionId]
      || sessionUiState[requestSessionId]?.title
      || (activeSessionIdRef.current === requestSessionId ? activeSessionTitle : NEW_SESSION_START_TITLE);
    const nextTitle = currentTitle === NEW_SESSION_START_TITLE || currentTitle === "新对话"
      ? (isConfigureOnlyCapability(pack) ? `配置 ${pack.name}` : pack.discoveryOnly ? `find skill ${pack.name}` : `安装 ${pack.name}`)
      : currentTitle;
    if (!lockedSessionTitlesRef.current[requestSessionId]) {
      setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
      if (activeSessionIdRef.current === requestSessionId) {
        setActiveSessionTitle(nextTitle);
      }
    }
    const result = await sendChatMessage({
      sessionId: requestSessionId,
      message: taskLabel,
      visibleMessage: "",
      hiddenContext: prompt,
      attachments: [],
      internalAction: true
    });
    if (result.status === "error") {
      throw new Error(chatSendErrorMessage(result));
    }
    if (result.inline_reply) {
      updateAssistantMessage(requestSessionId, assistantId, (message) => ({
        ...message,
        content: redactInternalPromptText(result.inline_reply || ""),
        pending: false
      }));
    }
    if (result.request_id && result.stream) {
      const requestId = result.request_id;
      setActiveRequestId(requestId);
      sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [requestSessionId]: requestId };
      setSessionRequestIds((current) => ({ ...current, [requestSessionId]: requestId }));
      streamRetryCounts.current[requestSessionId] = 0;
      updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({ ...message, requestId }));
      attachMessageStream(requestSessionId, assistantId, requestId);
    }
  }

  async function handleInstallPack(pack: CapabilityPack, onInstalled?: () => void, targetSessionId?: string) {
    const requestSessionId = targetSessionId || activeSessionIdRef.current;
    if (pack.policyMode === "disabled") {
      if (shouldOpenCapabilitySettings(pack)) {
        setSettingsSection("abilities");
        setSettingsOpen(true);
      }
      setToast("管理员已禁用安装，请联系管理员预置能力包");
      return;
    }
    const targetRequestId = sessionRequestIdsRef.current[requestSessionId] || sessionRequestIds[requestSessionId] || "";
    const targetMessages = requestSessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[requestSessionId]?.messages || [];
    if (targetRequestId || targetMessages.some(isUiLiveAssistantMessage)) {
      const alreadyQueued = queuedInstallRef.current.some((item) => item.sessionId === requestSessionId && item.pack.id === pack.id);
      if (!alreadyQueued) {
        queuedInstallRef.current.push({ pack, onInstalled, sessionId: requestSessionId });
      }
      if (shouldOpenCapabilitySettings(pack)) {
        setSettingsSection("abilities");
        setSettingsOpen(true);
      }
      setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
      setInstallNotice({
        packId: pack.id,
        packName: pack.name,
        message: `${pack.name} 已排队，当前任务结束后自动${isConfigureOnlyCapability(pack) ? "配置检查" : pack.discoveryOnly ? "走 find skill" : "安装"}`
      });
      setToast(`${pack.name} 已排队${isConfigureOnlyCapability(pack) ? "配置检查" : pack.discoveryOnly ? "走 find skill" : "安装"}`);
      return;
    }
    if (shouldOpenCapabilitySettings(pack)) {
      setSettingsSection("abilities");
      setSettingsOpen(true);
    }
    setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
    setInstallNotice({
      packId: pack.id,
      packName: pack.name,
      message: isConfigureOnlyCapability(pack)
        ? `${pack.name} 正在检查只读配置，请稍后`
        : pack.discoveryOnly ? `${pack.name} 正在通过 find skill 发现安装源，请稍后` : `${pack.name} 正在安装，请稍后`
    });
    try {
      const request = await requestAgentInstallRequest({
        packId: pack.id,
        packName: pack.name,
        sessionId: requestSessionId
      });
      if (request.status === "error" || !request.prompt) {
        throw new Error(request.message || "生成安装任务失败");
      }
      await startAgentInstallChatTask(pack, request.prompt, requestSessionId);
      if (pack.discoveryOnly || isConfigureOnlyCapability(pack)) {
        setInstallingPackIds((current) => {
          const next = { ...current };
          delete next[pack.id];
          return next;
        });
        setInstallNotice({
          packId: pack.id,
          packName: pack.name,
          message: isConfigureOnlyCapability(pack) ? `${pack.name} 配置检查任务已创建` : `${pack.name} find skill 任务已创建`
        });
      } else {
        watchAgentPackInstall(pack, onInstalled);
      }
      setToast(`${pack.name} ${packTaskNoun(pack)}已创建`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : `${pack.name} ${packTaskNoun(pack)}创建失败`);
      setInstallingPackIds((current) => {
        const next = { ...current };
        delete next[pack.id];
        return next;
      });
      setInstallNotice((current) => current?.packId === pack.id ? null : current);
    }
  }

  function requestOpenFile(file: FileAttachment) {
    setApproval({
      type: "open-file",
      title: "打开文件前确认",
      message: `EcoreX 将在系统中打开 ${file.file_name}。`,
      file
    });
  }

  function previewOrOpenFile(file: FileAttachment) {
    if (!isImageAttachment(file)) {
      requestOpenFile(file);
      return;
    }
    setPreviewFile({
      ...file,
      file_type: "image",
      previewDataUrl: attachmentPreviewUrl(file)
    });
  }

  async function confirmOpenFile(file: FileAttachment) {
    if (activeProject?.path) {
      await registerProjectFolderPath(activeProject.path).catch(() => null);
    }
    const result = await openLocalPath(file.file_path);
    setApproval(null);
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  async function answerToolPermission(requestId: string, decision: "allow_once" | "always_allow" | "deny") {
    try {
      const result = await decideToolPermission({
        requestId,
        decision,
        remember: decision === "always_allow"
      });
      if (result.status === "error") {
        setToast(result.message || "权限确认已失效");
      }
    } catch (error) {
      setToast(error instanceof Error ? error.message : "权限确认失败");
    } finally {
      setApproval(null);
    }
  }

  async function copyMessageText(message: ChatItem) {
    const text = plainTextForMessage(message);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "true");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    setCopiedMessageId(message.id);
    window.setTimeout(() => setCopiedMessageId((current) => current === message.id ? "" : current), 1400);
  }

  function downloadJsonFile(fileName: string, payload: unknown) {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function handleExportDiagnostics() {
    try {
      const bundle = await exportDiagnosticsBundle({
        sessionId: activeSessionId,
        requestId: activeSessionRequestId || undefined
      });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJsonFile(`ecorex-diagnostics-${stamp}.json`, bundle);
      await navigator.clipboard?.writeText(JSON.stringify(bundle, null, 2)).catch(() => undefined);
      setToast("诊断包已生成并复制到剪贴板");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "诊断包生成失败");
    }
  }

  async function refreshRunCenter(showToast = true) {
    const snapshot = await loadRuntimeSnapshot();
    setRuntimeSnapshot(snapshot);
    if (showToast) setToast("Run Center refreshed");
  }

  function openRunCenterSurface() {
    if (!runCenterDevVisible) return;
    setRunCenterOpen(true);
    void refreshRunCenter(false).catch(() => undefined);
  }

  async function openRunCenterSession(request: RuntimeActiveRequest, options: { closeSurface?: boolean } = {}) {
    if (isRunCenterSubagentRequest(request)) {
      setToast("Subagent runs are visible in Run Center; export diagnostics for details");
      return;
    }
    if (isRunCenterSchedulerRequest(request)) {
      setToast("Scheduler runs are visible in Run Center; export diagnostics for details");
      return;
    }
    const sessionId = String(request.session_id || "");
    if (!sessionId) {
      setToast("Run Center item has no session id");
      return;
    }
    const existing = allSessions.find((row) => row.id === sessionId);
    const requestId = request.request_id ? String(request.request_id) : undefined;
    const state = runCenterState(request);
    const scopedRow: SessionRow = {
      ...(existing || {
        id: sessionId,
        title: sessionId,
        detail: String(request.run_type || request.source || ""),
        activityAt: request.updated_at || request.created_at,
        createdAt: request.created_at,
        updatedAt: request.updated_at || request.created_at || "running",
        status: state === "failed" ? "failed" : state === "cancelling" ? "cancelling" : "waiting"
      }),
      id: sessionId,
      requestId,
      streamAvailable: state !== "failed" && request.stream_available !== false,
      cancelling: state === "cancelling",
      status: state === "failed" ? "failed" : state === "cancelling" ? "cancelling" : existing?.status || "waiting"
    };
    await selectSession(scopedRow);
    if (options.closeSurface) {
      setRunCenterOpen(false);
    }
  }

  function staleLockIsDeadOwner(lock: RuntimeSessionLock) {
    return Boolean(lock.dead_owner || lock.deadOwner);
  }

  function staleLockKey(lock: RuntimeSessionLock, index: number) {
    const lockPathHash = typeof lock.lockPath?.pathHash === "string" ? lock.lockPath.pathHash : "";
    return `${lock.sessionHash || lockPathHash || "lock"}-${index}`;
  }

  function staleLockDisplayName(lock: RuntimeSessionLock) {
    if (lock.sessionHash) {
      return `session ${String(lock.sessionHash).slice(0, 8)}`;
    }
    const lockPathHash = typeof lock.lockPath?.pathHash === "string" ? lock.lockPath.pathHash : "";
    if (lockPathHash) {
      return `lock ${String(lockPathHash).slice(0, 8)}`;
    }
    return "session lock";
  }

  function staleLockStatusLabel(lock: RuntimeSessionLock) {
    const state = lock.removed ? "removed" : staleLockIsDeadOwner(lock) ? "dead owner" : "stale";
    const age = formatRunAge(lock.age_seconds);
    const removeError = lock.removeError ? " · remove error" : "";
    return `${state}${age ? ` · ${age}` : ""}${removeError}`;
  }

  function canOpenStaleLockSession(lock: RuntimeSessionLock) {
    return Boolean(lock.session_id);
  }

  async function openRunCenterStaleLockSession(lock: RuntimeSessionLock, options: { closeSurface?: boolean } = {}) {
    if (!canOpenStaleLockSession(lock)) {
      setToast("Stale lock details are redacted; export diagnostics for the session hash.");
      return;
    }
    await selectSession({
      id: String(lock.session_id),
      title: String(lock.session_id),
      detail: "stale lock",
      activityAt: Date.now(),
      updatedAt: Date.now(),
      status: "failed"
    });
    if (options.closeSurface) {
      setRunCenterOpen(false);
    }
  }

  function runCenterRetryPolicy(request: RuntimeActiveRequest) {
    const sessionId = String(request.session_id || "");
    const state = runCenterState(request);
    const retryAfterMs = Number(request.retry_after_ms || 0);
    if (request.actions && request.actions.retry === false) {
      return {
        enabled: false,
        title: request.retry_disabled_reason || "Retry is unavailable for this run"
      };
    }
    if (request.actions?.retry === true) {
      return {
        enabled: true,
        title: retryAfterMs > 0
          ? `Prepare a retry after ${Math.ceil(retryAfterMs / 1000)}s`
          : "Open the session and prepare a retry prompt"
      };
    }
    if (isRunCenterSubagentRequest(request)) {
      return {
        enabled: false,
        title: "Subagent runs are stop/diagnostics-only until subagent replay is available"
      };
    }
    if (isRunCenterSchedulerRequest(request)) {
      return {
        enabled: false,
        title: "Scheduler runs are stop/diagnostics-only until scheduler replay is available"
      };
    }
    if (!sessionId) {
      return {
        enabled: false,
        title: "Retry requires a chat session id"
      };
    }
    if (state !== "failed") {
      return {
        enabled: false,
        title: state === "cancelling" ? "Retry is available after stopping finishes" : "Retry is available for failed chat runs"
      };
    }
    if (request.retryable === false && request.recoverable === false) {
      return {
        enabled: false,
        title: "This failed run is marked non-retryable"
      };
    }
    return {
      enabled: true,
      title: retryAfterMs > 0
        ? `Prepare a retry after ${Math.ceil(retryAfterMs / 1000)}s`
        : "Open the session and prepare a retry prompt"
    };
  }

  async function retryRunCenterRequest(request: RuntimeActiveRequest) {
    const policy = runCenterRetryPolicy(request);
    if (!policy.enabled) {
      setToast(policy.title);
      return;
    }
    await openRunCenterSession(request);
    const requestId = String(request.request_id || "");
    const prepared = await prepareRetryDraft(requestId, String(request.session_id || ""));
    if (prepared) setToast("Run Center retry prepared; review and send.");
    setRunCenterOpen(false);
  }

  async function prepareRetryDraft(requestId: string, sessionId = activeSessionIdRef.current) {
    if (!requestId) {
      setToast("Retry requires a request id");
      return false;
    }
    try {
      const result = await prepareRequestRetry({ requestId, sessionId });
      if (result.recoverable) {
        void refreshSessionFromHistory(sessionId);
      }
      if (result.status === "error" || !result.retryable || !result.prompt) {
        setToast(result.message || "当前还不能安全重试，请稍后再试。");
        return false;
      }
      setComposerDraft(result.prompt, { immediate: true });
      setAttachments((result.attachments || []).filter(isDurableLocalAttachment));
      focusComposerSoon();
      setToast(result.exactReplay || result.exact_replay ? "已准备好重试草稿，请确认后发送。" : "已基于最新记录准备好重试草稿，请确认后发送。");
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "准备重试失败");
      return false;
    }
  }

  async function stopRunCenterRequest(request: RuntimeActiveRequest) {
    const requestId = String(request.request_id || "");
    const sessionId = String(request.session_id || "");
    if (!requestId && !sessionId) {
      setToast("Run Center item cannot be stopped");
      return;
    }
    try {
      if (isRunCenterSubagentRequest(request)) {
        const taskId = getRunCenterSubagentTaskId(request);
        if (!taskId) {
          setToast("Subagent run cannot be stopped without task id");
          return;
        }
        try {
          await cancelSubagentTask(taskId);
        } catch (subagentError) {
          const fallback = await cancelChatRequest({ requestId, sessionId });
          if (Number(fallback.cancelled || 0) <= 0) {
            throw subagentError;
          }
        }
        setRuntimeSnapshot(await loadRuntimeSnapshot());
        setToast("Subagent stop requested");
        return;
      }
      const result = await cancelChatRequest({ requestId, sessionId });
      if (Number(result.cancelled || 0) <= 0) {
        throw new Error("Run Center stop found no cancellable runtime row");
      }
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast(isRunCenterSchedulerRequest(request) ? "Scheduler stop requested" : "Stop requested");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Stop request failed");
    }
  }

  async function exportRunCenterDiagnostics(request: RuntimeActiveRequest) {
    try {
      const requestId = request.request_id ? String(request.request_id) : undefined;
      const sessionId = request.session_id ? String(request.session_id) : undefined;
      const bundle = await exportDiagnosticsBundle({ sessionId, requestId });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJsonFile(`ecorex-run-diagnostics-${stamp}.json`, bundle);
      setToast("Run diagnostics generated");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Run diagnostics failed");
    }
  }

  async function openArtifactFile(
    file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"]; open_action?: "preview" | "open" | "reveal" | "copy" | "openWith"; previewDataUrl?: string; preview_url?: string },
    sessionId = activeSessionIdRef.current
  ) {
    const rawPath = normalizeLocalSource(file.file_path);
    if (!rawPath) return;
    let action = file.open_action || "open";
    const fileType = file.file_type || "file";
    const artifactSessionId = sessionId;
    const resolvedPath = resolveArtifactPathForSession(artifactSessionId, rawPath);
    if (action === "preview") {
      if (fileType !== "image") {
        action = "open";
      } else {
        const previewPath = isRuntimePreviewPath(rawPath) ? rawPath : resolvedPath;
        setPreviewFile({
          file_path: previewPath,
          file_name: file.file_name,
          file_type: "image",
          previewDataUrl: file.previewDataUrl || (file.preview_url ? filePreviewUrl(file.preview_url, sidecarStatus.webPort) : filePreviewUrl(previewPath, sidecarStatus.webPort)),
          preview_url: file.preview_url
        });
        return;
      }
    }
    if (action === "copy") {
      await navigator.clipboard?.writeText(resolvedPath || rawPath).catch(() => undefined);
      setToast("路径已复制");
      return;
    }
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    const projectPath = projectPathForSession(artifactSessionId);
    if (projectPath) {
      await registerProjectFolderPath(projectPath).catch(() => null);
    }
    let result = "";
    const openAction: OpenPathAction = action === "reveal" ? "reveal" : action === "openWith" ? "openWith" : "open";
    for (const candidate of candidates) {
      try {
        result = await openLocalPath(candidate, openAction);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error || "");
        result = message;
        if (isOpenPathDeniedMessage(message)) {
          break;
        }
        if (isLocalAbsolutePath(candidate) && isOpenPathBridgeFailure(message)) {
          result = await openLocalPath(candidate, openAction);
        }
      }
      if (isOpenPathDeniedMessage(result) || !isOpenPathNotFoundMessage(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  async function legacyOpenArtifactFile(file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"] }, sessionId = activeSessionIdRef.current) {
    const rawPath = normalizeLocalSource(file.file_path);
    if (!rawPath) return;
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const artifactSessionId = sessionId;
    const resolvedPath = resolveArtifactPathForSession(artifactSessionId, rawPath);
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    const projectPath = projectPathForSession(artifactSessionId);
    if (projectPath) {
      await registerProjectFolderPath(projectPath).catch(() => null);
    }
    let result = "";
    for (const candidate of candidates) {
      try {
        result = await openLocalPath(candidate);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error || "");
        result = message;
        if (isOpenPathDeniedMessage(message)) {
          break;
        }
        if (isLocalAbsolutePath(candidate) && isOpenPathBridgeFailure(message)) {
          result = await openLocalPath(candidate);
        }
      }
      if (isOpenPathDeniedMessage(result) || !isOpenPathNotFoundMessage(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  const handleOpenMessageLocalFile = useCallback((file: LocalFilePayload) => {
    void openArtifactFile(file, activeSessionId);
  }, [activeSessionId, activeProject?.path, sidecarStatus.webPort, sessionProjects, projects]);

  const messageLocalFilePreviewUrl = useCallback((filePath: string) => (
    filePreviewUrl(resolveArtifactPathForSession(activeSessionId, filePath), sidecarStatus.webPort)
  ), [activeSessionId, sidecarStatus.webPort, sessionProjects, projects]);

  const messageLocalFileStat = useCallback((filePath: string) => (
    statArtifactPath(filePath, activeSessionId)
  ), [activeSessionId, sessionProjects, projects]);

  const messageLocalJson = useCallback((filePath: string) => (
    readArtifactStatusJson(filePath, activeSessionId)
  ), [activeSessionId, sessionProjects, projects]);

  async function logout() {
    await enterpriseLogout();
    setSession(null);
    setQuotaSnapshot(null);
    setMessages([]);
    setApproval(null);
  }

  async function submitPasswordChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (passwordDraft.newPassword.length < 8) {
      setApproval({ type: "error", title: "密码太短", message: "新密码至少需要 8 个字符。" });
      return;
    }
    if (passwordDraft.newPassword !== passwordDraft.confirmPassword) {
      setApproval({ type: "error", title: "两次密码不一致", message: "请重新输入并确认新密码。" });
      return;
    }
    setPasswordBusy(true);
    try {
      const nextSession = await enterpriseChangePassword({
        oldPassword: passwordDraft.oldPassword,
        newPassword: passwordDraft.newPassword
      });
      setSession(nextSession);
      setPasswordDraft({ oldPassword: "", newPassword: "", confirmPassword: "" });
      setToast("密码已更新");
    } catch (error) {
      setApproval({ type: "error", title: "密码修改失败", message: error instanceof Error ? error.message : "请稍后重试。" });
    } finally {
      setPasswordBusy(false);
    }
  }

  function closeReleaseNotes() {
    const seenVersion = runtimeSnapshot.releaseNotes?.version || runtimeSnapshot.version;
    if (seenVersion) {
      releaseNotesDismissedVersion.current = seenVersion;
      try {
        window.localStorage.setItem(RELEASE_NOTES_SEEN_STORAGE_KEY, seenVersion);
      } catch {
        // Ignore storage failures; closing should still work.
      }
    }
    setReleaseNotesOpen(false);
  }

  function externalConnectionConfigState(connection: ExternalConnection) {
    const raw = connection.configState;
    if (typeof raw === "string") return raw.toLowerCase();
    if (raw && typeof raw === "object" && "state" in raw) {
      return String((raw as { state?: unknown }).state || "").toLowerCase();
    }
    return "";
  }

  function externalConnectionReadinessState(connection: ExternalConnection) {
    const readiness = connection.adapterContract?.readiness;
    if (!readiness || typeof readiness !== "object") return "";
    return String(readiness.readiness || readiness.status || "").toLowerCase();
  }

  function externalConnectionDependencyMissing(connection: ExternalConnection) {
    const status = String(connection.status || "").toLowerCase();
    const dependencyStatus = connection.dependencyStatus && typeof connection.dependencyStatus === "object" ? connection.dependencyStatus : {};
    const readiness = connection.adapterContract?.readiness;
    const adapterDependencyStatus = readiness && typeof readiness === "object" && typeof readiness.dependencyStatus === "object"
      ? (readiness.dependencyStatus as Record<string, unknown>)
      : {};
    return Boolean(connection.dependencyMissing)
      || status === "dependency_missing"
      || String(dependencyStatus.status || "").toLowerCase() === "missing"
      || String(adapterDependencyStatus.status || "").toLowerCase() === "missing";
  }

  function externalConnectionNeedsConfiguration(connection: ExternalConnection) {
    const status = String(connection.status || "").toLowerCase();
    const configState = externalConnectionConfigState(connection);
    const readiness = externalConnectionReadinessState(connection);
    if (externalConnectionDependencyMissing(connection)) return false;
    if (status === "available") return false;
    return status === "blocked"
      || status === "not_configured"
      || readiness === "not_configured"
      || (Boolean(connection.enabled || connection.running || connection.connected)
        && (configState === "missing" || configState === "partial" || configState === "not_configured"));
  }

  function externalConnectionNeedsAuthorization(connection: ExternalConnection) {
    const status = String(connection.status || "").toLowerCase();
    const configState = externalConnectionConfigState(connection);
    const readiness = externalConnectionReadinessState(connection);
    if (externalConnectionDependencyMissing(connection)) return false;
    if (status === "available") return false;
    return status === "auth_required"
      || readiness === "auth_required"
      || (Boolean(connection.enabled || connection.running || connection.connected) && configState === "auth_required");
  }

  function externalConnectionCardState(connection: ExternalConnection) {
    if (externalConnectionNeedsConfiguration(connection) || externalConnectionNeedsAuthorization(connection)) return "blocked";
    if (externalConnectionDependencyMissing(connection)) return "blocked";
    if (String(connection.status || "").toLowerCase() === "error" || connection.lastError) return "error";
    if (connection.connected || connection.running) return "connected";
    if (connection.configured) return "configured";
    return "available";
  }

  function externalConnectionStatusLabel(connection: ExternalConnection) {
    if (externalConnectionDependencyMissing(connection)) return "运行依赖缺失";
    if (externalConnectionNeedsAuthorization(connection)) return "需授权";
    if (externalConnectionNeedsConfiguration(connection)) return "需配置";
    if (String(connection.status || "").toLowerCase() === "error" || connection.lastError) return "异常";
    if (connection.connected || connection.running) return "已连接";
    if (connection.enabled) return "已启用";
    if (connection.configured) return "已配置";
    return "待配置";
  }

  function externalConnectionHumanLabel(value: unknown, fallback = "状态未知") {
    const raw = String(value || "").trim();
    if (!raw) return fallback;
    const normalized = raw.toLowerCase().replace(/\s+/g, " ");
    const labels: Record<string, string> = {
      active: "已启用",
      auth_required: "需授权",
      available: "可配置",
      blocked: "已阻塞",
      callable: "可调用",
      configured: "已配置",
      connected: "已连接",
      disabled: "未启用",
      dependency_missing: "运行依赖缺失",
      disconnected: "未连接",
      enabled: "已启用",
      error: "异常",
      missing: "缺少配置",
      not_configured: "未配置",
      partial: "配置不完整",
      present: "已填写",
      ready: "就绪",
      running: "运行中",
      sdk_missing: "运行依赖缺失",
      schema_visible_unverified: "工具可见，待状态检查",
      stopped: "已停止",
      tool_not_loaded: "工具未加载",
      unknown: "状态未知",
      "auth unknown": "授权状态未知",
      "no agent tool is declared for this channel": "该平台暂无可调用的智能体工具",
      "declared tool schema is not loaded in the current agent snapshot": "已声明工具，但当前运行时尚未加载",
      "tool schema is visible, but cli/auth readiness requires an explicit status probe": "工具已可见，需先做状态检查"
    };
    return labels[normalized] || labels[raw] || raw;
  }

  function externalConnectionDescription(connection: ExternalConnection, label: string) {
    const descriptions: Record<string, string> = {
      weixin: "微信个人助手通道",
      feishu: "飞书 / Lark 机器人通道，使用应用凭据和事件订阅",
      dingtalk: "钉钉机器人通道",
      wecom_bot: "企微智能机器人通道",
      wechatcom_app: "企业微信自建应用通道",
      wechat_kf: "微信客服通道",
      wechatmp: "微信公众号被动回复通道",
      wechatmp_service: "微信公众号客服通道",
      qq: "QQ 机器人通道",
      telegram: "Telegram 机器人通道",
      slack: "Slack 机器人通道",
      discord: "Discord 机器人通道"
    };
    const key = String(connection.id || connection.platform || "").trim();
    if (descriptions[key]) return descriptions[key];
    return externalConnectionHumanLabel(connection.description, label);
  }

  function externalConnectionNotice(connection: ExternalConnection) {
    if (!externalConnectionDependencyMissing(connection)) return connection.lastError || "";
    const dependencyStatus = connection.dependencyStatus && typeof connection.dependencyStatus === "object" ? connection.dependencyStatus : {};
    const dependency = String(dependencyStatus.dependency || "lark_oapi");
    if (String(connection.id || connection.platform || "") === "feishu") {
      return `已保存 App ID / Secret，但当前 WebUI 运行时缺少 ${dependency}，无法启动飞书消息接收；这不是凭据校验失败。`;
    }
    return `当前 WebUI 运行时缺少 ${dependency}。`;
  }

  function externalConnectionConfigLabel(connection: ExternalConnection) {
    const state = connection.auth?.channelConfigState || externalConnectionConfigState(connection);
    return externalConnectionHumanLabel(state, connection.configured ? "已配置" : "未配置");
  }

  function externalConnectionCallableLabel(connection: ExternalConnection) {
    if (connection.callable) return "智能体可调用";
    const surface = connection.agentSurface || {};
    return externalConnectionHumanLabel(
      surface.callableReason || surface.readiness || surface.status,
      "智能体待就绪"
    );
  }

  function externalConnectionFieldLabel(field: ExternalConnectionField) {
    const raw = String(field.label || field.key || "").trim();
    const normalized = raw.toLowerCase().replace(/\s+/g, " ");
    const labels: Record<string, string> = {
      app_id: "应用 ID",
      app_secret: "应用密钥",
      "app id": "应用 ID",
      "app secret": "应用密钥",
      allow_all_users: "允许所有用户",
      "allow all users": "允许所有用户",
      allowed_users: "允许用户",
      "allowed users": "允许用户",
      bot_token: "机器人 Token",
      bot_secret: "机器人密钥",
      home_channel: "主页频道",
      home_channel_name: "主页频道名称",
      signing_secret: "签名密钥",
      slack_bot_token: "Slack 机器人 Token",
      telegram_token: "Telegram Bot Token",
      wecom_bot_id: "企微机器人 BotID",
      wecom_bot_secret: "企微机器人密钥",
      webhook_url: "Webhook 地址"
    };
    return labels[normalized] || labels[raw] || raw;
  }

  function externalConnectionActionLabel(action: ExternalConnectionAction) {
    if (action.id === "save_config") return "保存配置";
    if (action.id === "test") return "状态检查";
    if (action.id === "start") return "连接";
    if (action.id === "enable") return "启用";
    if (action.id === "stop") return "断开";
    if (action.id === "disable") return "停用";
    if (action.id === "set_home_channel") return "设为投递目标";
    return externalConnectionHumanLabel(action.label, action.id);
  }

  function externalConnectionActionIcon(actionId: string) {
    if (actionId === "save_config") return <KeyRound aria-hidden="true" />;
    if (actionId === "test") return <Activity aria-hidden="true" />;
    if (actionId === "stop" || actionId === "disable") return <Square aria-hidden="true" />;
    if (actionId === "set_home_channel") return <AtSign aria-hidden="true" />;
    return <CheckCircle2 aria-hidden="true" />;
  }

  function externalConnectionActionTone(actionId: string) {
    if (actionId === "start" || actionId === "enable" || actionId === "save_config") return "primary";
    if (actionId === "stop" || actionId === "disable") return "danger";
    if (actionId === "test") return "check";
    return "neutral";
  }

  function externalConnectionActions(connection: ExternalConnection, connected: boolean): ExternalConnectionAction[] {
    if (connection.actions?.length) return connection.actions;
    return [
      { id: "save_config", label: "保存" },
      { id: "test", label: "状态检查" },
      { id: connected ? "stop" : "start", label: connected ? "断开" : "连接" }
    ];
  }

  function externalConnectionFieldValue(connection: ExternalConnection, key: string) {
    const draft = externalConnectionDrafts[connection.id]?.[key];
    if (draft !== undefined) return draft;
    const field = (connection.fields || []).find((item) => item.key === key);
    return field?.value ?? field?.default ?? "";
  }

  function updateExternalConnectionDraft(connection: ExternalConnection, key: string, value: unknown) {
    setExternalConnectionDrafts((current) => ({
      ...current,
      [connection.id]: {
        ...(current[connection.id] || {}),
        [key]: value
      }
    }));
  }

  function externalConnectionConfig(connection: ExternalConnection) {
    const config: Record<string, unknown> = {};
    for (const field of connection.fields || []) {
      if (!field.key) continue;
      const value = externalConnectionFieldValue(connection, field.key);
      if ((field.type === "secret" || field.sensitive || field.masked) && typeof value === "string" && value.includes("****")) continue;
      config[field.key] = value;
    }
    return config;
  }

  function externalConnectionHomeChannel(connection: ExternalConnection) {
    const config = externalConnectionConfig(connection);
    const projected = connection.homeChannel && typeof connection.homeChannel === "object" ? connection.homeChannel : {};
    const homeId = String(
      projected.id
      || projected.channelId
      || projected.channel_id
      || projected.value
      || config.home_channel
      || config.homeChannel
      || config[`${connection.id}_home_channel`]
      || ""
    ).trim();
    const homeName = String(
      projected.name
      || projected.label
      || config.home_channel_name
      || config.homeChannelName
      || config[`${connection.id}_home_channel_name`]
      || ""
    ).trim();
    return { id: homeId, name: homeName };
  }

  function externalConnectionActionToast(label: string, action: string, result: ExternalConnectionActionResponse) {
    if (action === "test") {
      const dryRun = result.test?.remoteConnectivityProbed === false || result.adapter?.remoteConnectivityProbed === false || result.test?.mode === "projection_dry_run" || result.adapter?.testMode === "projection_dry_run";
      return dryRun ? `${label} 状态已检查，未探测远端连通` : `${label} 状态检查完成`;
    }
    if (action === "set_home_channel") return `${label} 主页频道已更新`;
    if (action === "stop" || action === "disable") return `${label} 已断开`;
    return `${label} 已更新`;
  }

  async function refreshExternalConnections(showToast = true) {
    setExternalConnectionsBusy(true);
    try {
      const payload = await loadExternalConnections();
      setExternalConnections(payload.connections || []);
      if (showToast) setToast("外部连接已刷新");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "外部连接刷新失败");
    } finally {
      setExternalConnectionsBusy(false);
    }
  }

  async function applyExternalConnectionAction(connection: ExternalConnection, action: string) {
    setExternalConnectionsBusy(true);
    try {
      const payload: Record<string, unknown> = {
        action,
        config: externalConnectionConfig(connection)
      };
      if (action === "set_home_channel") {
        const homeChannel = externalConnectionHomeChannel(connection);
        if (!homeChannel.id) {
          throw new Error("请先填写主页频道");
        }
        payload.homeChannel = homeChannel.id;
        if (homeChannel.name) payload.homeChannelName = homeChannel.name;
      }
      const result = await updateExternalConnection(connection.id, payload);
      await refreshExternalConnections(false);
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      const label = connection.displayName || connection.label?.zh || connection.id;
      setToast(externalConnectionActionToast(label, action, result));
    } catch (error) {
      setToast(error instanceof Error ? error.message : "外部连接操作失败");
    } finally {
      setExternalConnectionsBusy(false);
    }
  }

  const schedulerProjection: RuntimeSchedulerProjection = runtimeSnapshot.scheduler || {
    enabled: false,
    initialized: false,
    running: false,
    serviceStatus: "unavailable",
    tasks: [],
    taskCount: 0,
    counts: { total: 0, enabled: 0, disabled: 0, error: 0 }
  };
  const schedulerTasks = Array.isArray(schedulerProjection.tasks) ? schedulerProjection.tasks : [];
  const schedulerCanModify = schedulerProjection.canModify !== false;

  function schedulerStatusLabel(projection = schedulerProjection) {
    if (projection.running) return "运行中";
    if (projection.serviceStatus === "enabled_not_initialized") return "已启用，待初始化";
    if (projection.enabled) return "已启用，未运行";
    if (projection.serviceStatus === "unavailable") return "不可用";
    return "未启用";
  }

  function schedulerTaskActionLabel(task: RuntimeSchedulerTask) {
    const action = task.action || {};
    if (action.type === "agent_task") return "AI 任务";
    if (action.type === "send_message") return "固定消息";
    if (action.type === "tool_call") return action.toolName ? `工具 ${action.toolName}` : "工具调用";
    if (action.type === "skill_call") return action.skillName ? `Skill ${action.skillName}` : "Skill 调用";
    return action.type || "任务";
  }

  function schedulerTaskDetail(task: RuntimeSchedulerTask) {
    const action = task.action || {};
    const preview =
      action.taskDescriptionPreview ||
      action.contentPreview ||
      action.resultPrefixPreview ||
      "";
    if (preview === "[redacted-content]") {
      const hash = action.taskDescriptionHash || action.contentHash || action.resultPrefixHash || "";
      const length = action.taskDescriptionLength || action.contentLength || action.resultPrefixLength || 0;
      return `内容已隐藏${length ? ` · ${length} 字符` : ""}${hash ? ` · ${hash}` : ""}`;
    }
    return preview || schedulerTaskActionLabel(task);
  }

  async function refreshSchedulerPanel() {
    setSchedulerBusy(true);
    try {
      const scheduler = await loadSchedulerProjection();
      setRuntimeSnapshot((current) => ({ ...current, scheduler }));
      setToast("定时任务已刷新");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "定时任务刷新失败");
    } finally {
      setSchedulerBusy(false);
    }
  }

  async function applySchedulerAction(input: Record<string, unknown>, successText: string) {
    setSchedulerBusy(true);
    try {
      const scheduler = await updateScheduler(input);
      setRuntimeSnapshot((current) => ({ ...current, scheduler }));
      if (scheduler.status === "error") {
        throw new Error(scheduler.message || "定时任务操作失败");
      }
      setToast(successText);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "定时任务操作失败");
    } finally {
      setSchedulerBusy(false);
    }
  }

  async function renameSchedulerTask(task: RuntimeSchedulerTask) {
    const nextName = window.prompt("重命名定时任务", task.name || "")?.trim();
    if (!nextName || nextName === task.name) return;
    await applySchedulerAction({ action: "update", task_id: task.id, name: nextName }, "定时任务已重命名");
  }

  async function editSchedulerTaskCron(task: RuntimeSchedulerTask) {
    const current = typeof task.schedule?.expression === "string" ? task.schedule.expression : "";
    const nextCron = window.prompt("Cron 表达式", current || "30 9 * * *")?.trim();
    if (!nextCron || nextCron === current) return;
    await applySchedulerAction({ action: "update", task_id: task.id, schedule_type: "cron", schedule_value: nextCron }, "定时计划已更新");
  }

  async function editSchedulerTaskContent(task: RuntimeSchedulerTask) {
    const action = task.action || {};
    const nextText = window.prompt("任务内容（重新输入完整内容）", "")?.trim();
    if (!nextText) return;
    const key = action.type === "send_message" ? "content" : "taskDescription";
    await applySchedulerAction({ action: "update", task_id: task.id, [key]: nextText }, "定时任务内容已更新");
  }

  const browserPack = packs.find((pack) => pack.id === "browser-automation");
  const feishuPack = packs.find((pack) => pack.id === "feishu-lark");
  const shellReady = runtimeToolReady(runtimeSnapshot, "bash");
  const webSearchReady = runtimeToolReady(runtimeSnapshot, "web_search");
  const fileToolsReady = ["read", "write", "edit", "ls"].every((name) => runtimeToolReady(runtimeSnapshot, name));
  const ocrReady = runtimeToolReady(runtimeSnapshot, "ocr");
  const visionReady = runtimeToolReady(runtimeSnapshot, "vision");
  const schedulerToolReady = runtimeToolReady(runtimeSnapshot, "scheduler");
  const feishuToolReady = runtimeToolReady(runtimeSnapshot, "feishu_cli");
  const browserToolReady = runtimeToolReady(runtimeSnapshot, "browser");
  const abilityRows = [
    {
      id: "web_search",
      name: "联网搜索",
      detail: webSearchReady ? "搜索工具已加载" : "已启用入口，等待搜索服务凭据",
      enabled: webSearchReady,
      statusLabel: webSearchReady ? "已加载" : "需凭据",
      icon: <Globe2 aria-hidden="true" />
    },
    {
      id: "bash",
      name: "Bash / Shell",
      detail: shellReady ? "可在项目上下文中执行命令" : "运行时尚未返回 shell 工具",
      enabled: shellReady,
      statusLabel: shellReady ? "已加载" : "未加载",
      icon: <SquareTerminal aria-hidden="true" />
    },
    {
      id: "files",
      name: "本地文件读写",
      detail: fileToolsReady
        ? "读取、写入、编辑、目录浏览已就绪"
        : "基础文件工具未全部加载",
      enabled: fileToolsReady,
      statusLabel: fileToolsReady ? "已加载" : "未加载",
      icon: <HardDrive aria-hidden="true" />
    },
    {
      id: "vision",
      name: "OCR / 图像理解",
      detail: ocrReady && visionReady
        ? "快速 OCR 与图像理解工具已加载"
        : ocrReady
          ? "快速 OCR 已加载；复杂图像理解等待模型能力"
          : visionReady
            ? "图像理解工具已加载；快速 OCR 等待运行时刷新"
            : "等待 OCR 或视觉工具加载",
      enabled: ocrReady || visionReady,
      statusLabel: ocrReady || visionReady ? "已加载" : "未加载",
      icon: <ImageIcon aria-hidden="true" />
    },
    {
      id: "image-generation",
      name: "Image Gen",
      detail: extensionSkillEnabled(runtimeSnapshot, "image-generation") ? "图像生成 Skill 已开启" : "等待开启图像生成 Skill",
      enabled: extensionSkillEnabled(runtimeSnapshot, "image-generation"),
      statusLabel: extensionSkillEnabled(runtimeSnapshot, "image-generation") ? "已启用" : "未启用",
      icon: <WandSparkles aria-hidden="true" />
    },
    {
      id: "scheduler",
      name: "定时任务",
      detail: schedulerToolReady
        ? `${schedulerStatusLabel()}，${schedulerProjection.taskCount || 0} 个任务`
        : "定时任务工具未加载",
      enabled: schedulerToolReady,
      statusLabel: schedulerProjection.running ? "运行中" : schedulerToolReady ? "工具已加载" : "未加载",
      icon: <Bell aria-hidden="true" />
    },
    {
      id: "feishu_cli",
      name: "飞书 / Lark CLI",
      detail: feishuToolReady
        ? "结构化 feishu_cli 已加载；授权缺失时会引导登录，不走 raw lark-cli"
        : feishuPack?.installed
          ? "能力包已安装，等待结构化工具刷新"
          : "飞书任务会先走结构化 feishu_cli，再按需安装官方 CLI",
      enabled: feishuToolReady,
      statusLabel: feishuToolReady ? "已加载" : feishuPack?.installed ? "等待刷新" : "按需安装",
      icon: <Database aria-hidden="true" />,
      pack: feishuPack
    },
    {
      id: "browser",
      name: "Playwright 浏览器",
      detail: browserToolReady
        ? "浏览器工具已加载，CDP 优先并按需 fallback"
        : browserPack?.installed
          ? "能力包已安装，等待运行时刷新"
          : "CDP 优先；Playwright fallback 按需安装",
      enabled: browserToolReady || Boolean(browserPack?.installed),
      statusLabel: browserToolReady ? "已加载" : browserPack?.installed ? "等待刷新" : "CDP 优先",
      icon: <Globe2 aria-hidden="true" />,
      pack: browserPack
    },
    {
      id: "memory",
      name: "项目记忆",
      detail: activeProject ? `写入 ${activeProject.name} 的 .ecorex/project-memory.md` : "通用会话保留原项目内置记忆入口",
      enabled: true,
      statusLabel: "已开启",
      icon: <Brain aria-hidden="true" />
    }
  ];
  const activeProjectMemoryPath = activeProject?.memoryPath || (activeProject ? `${activeProject.path}\\.ecorex\\project-memory.md` : "");
  const hasPendingAssistantMessage = messages.some(isUiLiveAssistantMessage);
  const visibleMessages = messages.filter((message) => !isSilentPausedAssistantMessage(message));
  const isNewSessionView = visibleMessages.length === 0 && !hasPendingAssistantMessage;
  const composerHasPayload = Boolean(composerHasText || attachments.length);
  const sessionRowNeedsReveal = (row: SessionRow, options?: { includeActive?: boolean }) => {
    const cachedMessages = sessionUiState[row.id]?.messages || [];
    const isRunning = row.status === "waiting" || row.status === "cancelling" || Boolean(row.requestId) || Boolean(sessionRequestIds[row.id]) || cachedMessages.some((message) => Boolean(message.recovery) || isUiLiveAssistantMessage(message));
    return (options?.includeActive !== false && row.id === activeSessionId)
      || isRunning
      || Boolean(unreadSessionIds[row.id])
      || Boolean(searchQuery.trim());
  };
  const projectsForceRevealed = projectSessionGroups.some(({ project, sessions }) => (
    project.id === activeProjectId || sessions.some((row) => sessionRowNeedsReveal(row))
  ));
  const projectsSectionCollapsed = sidebarCollapse.projectsSection && !projectsForceRevealed && !searchQuery.trim();
  const generalForceRevealed = generalSessions.some((row) => sessionRowNeedsReveal(row, { includeActive: false }));
  const generalSessionsCollapsed = sidebarCollapse.generalSessions && !generalForceRevealed && !searchQuery.trim();
  const currentComposerPermissionMode: PermissionMode = permissionState?.mode || "smart-ask";
  const releaseNotes = runtimeSnapshot.releaseNotes;
  const settingsNav: Array<{ id: SettingsSection; label: string; icon: ReactNode }> = [
    { id: "account", label: "账号", icon: <UserRound aria-hidden="true" /> },
    { id: "projects", label: "项目", icon: <FolderOpen aria-hidden="true" /> },
    { id: "abilities", label: "能力", icon: <Sparkles aria-hidden="true" /> },
    { id: "external-connections", label: "外部连接", icon: <Globe2 aria-hidden="true" /> },
    { id: "scheduler", label: "定时", icon: <Bell aria-hidden="true" /> },
    { id: "permissions", label: "权限", icon: <ShieldCheck aria-hidden="true" /> },
    { id: "memory", label: "记忆", icon: <Brain aria-hidden="true" /> },
    { id: "diagnostics", label: "诊断", icon: <Database aria-hidden="true" /> }
  ];

  function renderRunCenterPanel(surface: "settings" | "primary" = "settings") {
    return (
      <div className={`run-center-panel is-${surface}`} aria-label="Run Center" data-run-center-surface={surface}>
        <div className="run-center-head">
          <div>
            <strong>Run Center</strong>
            <span>{runCenterRequests.length} active/recent / {runCenterStaleLocks.length} stale</span>
          </div>
          <button type="button" onClick={() => void refreshRunCenter()} title="Refresh Run Center">
            <RefreshCw aria-hidden="true" />
            Refresh
          </button>
        </div>
        <div className="run-center-stats" aria-label="Run state summary">
          <span><Activity aria-hidden="true" />{runCenterStats.running} running</span>
          <span className="is-cancelling"><Square aria-hidden="true" />{runCenterStats.cancelling} stopping</span>
          <span className="is-failed"><AlertTriangle aria-hidden="true" />{runCenterStats.failed} failed</span>
          <span className="is-stale"><HardDrive aria-hidden="true" />{runCenterStats.stale} stale</span>
        </div>
        <div className="run-center-list">
          {runCenterRequests.map((request) => {
            const requestId = String(request.request_id || "");
            const sessionId = String(request.session_id || "");
            const isSubagent = isRunCenterSubagentRequest(request);
            const isScheduler = isRunCenterSchedulerRequest(request);
            const diagnosticsOnly = isSubagent || isScheduler;
            const subagentTaskId = isSubagent ? getRunCenterSubagentTaskId(request) : "";
            const age = formatRunAge(request.cancelled ? request.cancel_age_seconds ?? request.age_seconds : request.age_seconds);
            const diagnosticsOnlyTitle = isScheduler ? "Scheduler runs are diagnostics-only here" : "Subagent runs are diagnostics-only here";
            const retryPolicy = runCenterRetryPolicy(request);
            const openAllowed = request.actions?.open ?? !diagnosticsOnly;
            const stopAllowed = request.actions?.stop ?? !(runCenterState(request) === "failed" || (isSubagent && !subagentTaskId));
            return (
              <article className={`run-center-row ${runCenterStateClass(request)}`} key={requestId || `${sessionId}-${request.source || "request"}`}>
                <div className="run-center-row-main">
                  <span className="run-center-state">{runCenterStateLabel(request)}</span>
                  <strong>{sessionId || request.run_type || request.source || "runtime run"}</strong>
                  <small>{shortRequestId(requestId)}{request.phase ? ` · ${request.phase}` : ""}{age ? ` · ${age}` : ""}</small>
                </div>
                <div className="run-center-actions">
                  <button type="button" onClick={() => void openRunCenterSession(request, { closeSurface: surface === "primary" })} disabled={!openAllowed} title={!openAllowed ? diagnosticsOnlyTitle : "Open or recover session"}>
                    <FolderOpen aria-hidden="true" />
                    Open
                  </button>
                  <button type="button" onClick={() => void retryRunCenterRequest(request)} disabled={!retryPolicy.enabled} title={retryPolicy.title}>
                    <RefreshCw aria-hidden="true" />
                    Retry
                  </button>
                  <button type="button" onClick={() => void exportRunCenterDiagnostics(request)} title="Export diagnostics for this run">
                    <ArrowDownToLine aria-hidden="true" />
                    Diagnostics
                  </button>
                  <button
                    type="button"
                    onClick={() => void stopRunCenterRequest(request)}
                    disabled={!stopAllowed}
                    title={isScheduler ? "Stop scheduler run" : isSubagent ? (subagentTaskId ? "Stop subagent run" : "Subagent task id unavailable") : "Stop run"}
                  >
                    <Square aria-hidden="true" />
                    Stop
                  </button>
                </div>
              </article>
            );
          })}
          {runCenterStaleLocks.map((lock, index) => (
            <article className="run-center-row is-stale" key={staleLockKey(lock, index)}>
              <div className="run-center-row-main">
                <span className="run-center-state">Stale</span>
                <strong>{staleLockDisplayName(lock)}</strong>
                <small>{staleLockStatusLabel(lock)}</small>
              </div>
              <div className="run-center-actions">
                <button
                  type="button"
                  onClick={() => void openRunCenterStaleLockSession(lock, { closeSurface: surface === "primary" })}
                  disabled={!canOpenStaleLockSession(lock)}
                  title={canOpenStaleLockSession(lock) ? "Open session" : "Stale lock session id is redacted"}
                >
                  <FolderOpen aria-hidden="true" />
                  Open
                </button>
              </div>
            </article>
          ))}
          {!runCenterRequests.length && !runCenterStaleLocks.length && (
            <div className="session-empty">No active or stale runs</div>
          )}
        </div>
      </div>
    );
  }

  const renderSessionRow = (row: SessionRow) => {
    const cachedMessages = sessionUiState[row.id]?.messages || [];
    const rowProjectId = row.projectId || null;
    const isRunning = row.status === "waiting" || row.status === "cancelling" || Boolean(row.requestId) || Boolean(sessionRequestIds[row.id]) || cachedMessages.some((message) => Boolean(message.recovery) || isUiLiveAssistantMessage(message)) || (row.id === activeSessionId && hasPendingAssistantMessage);
    const isActive = row.id === activeSessionId;
    const hasUnread = Boolean(unreadSessionIds[row.id]) && !isActive && !isRunning;
    const hasLeadingSessionStatus = isRunning || hasUnread;
    const waitingReply = isActive && Boolean(approval);
    const rowTitle = [row.title, row.pinned ? "置顶会话" : "", row.detail, formatTime(row.updatedAt)].filter(Boolean).join("\n");
    return (
      <article
        className={`session-row is-${isRunning ? "waiting" : row.status}${isActive ? " is-active" : ""}${row.pinned ? " is-pinned" : ""}${waitingReply ? " is-awaiting-reply" : ""}${hasUnread ? " is-unread" : ""}`}
        key={row.id}
        draggable={false}
        data-session-ownership={rowProjectId ? "project" : "general"}
        data-session-pinned={row.pinned ? "true" : undefined}
        onDragStart={(event) => event.preventDefault()}
      >
        <button
          className={`session-main${hasLeadingSessionStatus ? " has-leading-status" : ""}`}
          type="button"
          onClick={() => void selectSession(row)}
          title={rowTitle}
          data-tooltip={rowTitle}
          aria-current={isActive ? "page" : undefined}
        >
          {isRunning ? <ThinkingIndicator compact /> : hasUnread ? <span className="session-unread-dot" aria-hidden="true" /> : null}
          <span className="session-line"><strong>{row.title}</strong>{row.detail ? <small>{row.detail}</small> : null}</span>
          <em>{waitingReply ? <span className="session-waiting-reply">等待回复</span> : formatTime(row.updatedAt)}</em>
        </button>
        <div className="session-actions">
          <button type="button" onClick={() => togglePinSession(row)} title={row.pinned ? "取消置顶" : "置顶会话"} aria-label={row.pinned ? "取消置顶" : "置顶会话"}>
            {row.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
          </button>
            <button type="button" onClick={() => void renameSession(row)} title="重命名会话" aria-label="重命名会话"><Pencil aria-hidden="true" /></button>
            <button type="button" onClick={() => void removeSession(row)} title="删除会话" aria-label="删除会话"><Trash2 aria-hidden="true" /></button>
        </div>
      </article>
    );
  };

  const renderGeneralSessionGroup = (label: string, rows: SessionRow[], kind: "pinned" | "regular") => (
    <section className={`session-group is-${kind}`} aria-label={label} key={kind}>
      <div className="session-group-title">
        <span>{kind === "pinned" ? <Pin aria-hidden="true" /> : null}{label}</span>
        <small>{rows.length}</small>
      </div>
      <div className="session-group-rows">
        {rows.map(renderSessionRow)}
      </div>
    </section>
  );

  function renderMessageRunTiming(message: ChatItem) {
    const label = messageRunTimingLabel(message);
    if (!label) return null;
    const hasProcessDisclosure = Boolean((message.steps || []).length || (message.toolCalls || []).length || message.reasoning?.trim());
    if (hasProcessDisclosure) return null;
    return <div className="message-run-timing"><CheckCircle2 aria-hidden="true" />{label}</div>;
  }

  function messageRunTimingLabel(message: ChatItem) {
    if (message.role !== "assistant" || !message.runTiming?.startedAtMs) return null;
    const elapsed = formatRunAge(chatRunTimingElapsedSeconds(message.runTiming, runClockTick));
    if (!elapsed) return null;
    const state = String(message.runTiming.state || "").toLowerCase();
    if (message.pending) return `已处理 ${elapsed}`;
    if (message.cancelled || state === "cancelled") return `已处理 ${elapsed} 后已中止`;
    if (state === "failed" || state === "error") return `已处理 ${elapsed} 后失败`;
    if (state === "interrupted") return `已处理 ${elapsed} 后中断`;
    return `已在 ${elapsed} 内达成目标`;
  }

  function renderRecoveryActions(message: ChatItem, sessionId: string) {
    const recovery = message.recovery;
    const requestId = recovery?.requestId || message.requestId || "";
    if (!recovery && !message.sendAttempt) return null;
    if (message.sendAttempt && message.sendAttempt.state !== "accepted") {
      const label = message.sendAttempt.state === "stopping-previous"
        ? (message.role === "user" ? "正在发送新消息" : "正在切换到这条新消息")
        : message.sendAttempt.state === "restore-available"
          ? "消息未发出，可在输入框中重试"
          : (message.role === "user" ? "正在发送" : "正在准备响应");
      return <div className="message-recovery-actions"><span>{label}</span></div>;
    }
    if (!recovery) return null;
    if (recovery.kind === "reconnecting") {
      return <div className="message-recovery-actions is-reconnecting ecorex-activity-status"><span>{recovery.message}</span></div>;
    }
    const canReconnect = Boolean(requestId && (message.pending || message.visibleOutputSettled));
    const showStop = Boolean(requestId && (message.pending || message.visibleOutputSettled || recovery.stopAllowed));
    return (
      <div className="message-recovery-actions">
        <span>{recovery.message}</span>
        {canReconnect && (
          <button type="button" onClick={() => attachMessageStream(sessionId, message.id, requestId)}>
            <RefreshCw aria-hidden="true" />重新连接
          </button>
        )}
        {recovery.recoverable && (
          <button type="button" onClick={() => void refreshSessionFromHistory(sessionId)}>
            <BookOpen aria-hidden="true" />恢复记录
          </button>
        )}
        {showStop && (
          <button type="button" onClick={() => void cancelChatRequest({ requestId, sessionId }).then(() => stopActiveRequest()).catch(() => undefined)}>
            <Square aria-hidden="true" />停止
          </button>
        )}
        {requestId && recovery.retryable && (
          <button type="button" onClick={() => void prepareRetryDraft(requestId, sessionId)}>
            <RefreshCw aria-hidden="true" />准备重试
          </button>
        )}
        <button type="button" onClick={() => void exportDiagnosticsBundle({ sessionId, requestId: requestId || undefined }).then((bundle) => {
          const stamp = new Date().toISOString().replace(/[:.]/g, "-");
          downloadJsonFile(`ecorex-recovery-diagnostics-${stamp}.json`, bundle);
        }).catch((error) => setToast(error instanceof Error ? error.message : "诊断信息导出失败"))}>
          <FileText aria-hidden="true" />诊断信息
        </button>
      </div>
    );
  }

  if (!authChecked) {
    return <main className="auth-shell"><WindowBrand version={appVersion} /><section className="auth-panel"><p>正在检查登录状态</p></section></main>;
  }

  if (!session) {
    return <AuthGate onLogin={(next) => {
      if (!next) return;
      setSession(next);
      setQuotaSnapshot((next.quota || null) as UsageQuota | null);
    }} version={appVersion} />;
  }

  return (
    <main className="app-shell">
      <WindowBrand version={appVersion} />
      <aside className="session-sidebar">
        <div className="sidebar-actions">
          <button onClick={() => startNewSession(null)} title="创建不绑定项目的通用会话" data-tooltip="创建不绑定项目的通用会话" data-tooltip-position="bottom-left"><Plus aria-hidden="true" />新对话</button>
          <label className="search-box" title="搜索会话标题和摘要">
            <Search aria-hidden="true" />
            <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="搜索会话" />
          </label>
        </div>

        <section className="project-panel" aria-label="项目">
          <div className="sidebar-section-title">
            <button className="sidebar-collapse-button" type="button" onClick={() => setSidebarCollapse((current) => ({ ...current, projectsSection: !current.projectsSection }))} aria-expanded={!projectsSectionCollapsed} title={projectsSectionCollapsed ? "展开项目" : "折叠项目"}>
              {projectsSectionCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
              <span>项目</span>
            </button>
            <button className="icon-button" type="button" onClick={() => void addProject()} title={projectPickerBusy ? "正在选择项目文件夹" : "添加项目文件夹"} disabled={projectPickerBusy} aria-busy={projectPickerBusy}>
              <FolderPlus aria-hidden="true" />
            </button>
          </div>
          {projectsSectionCollapsed ? null : projectSessionGroups.length === 0 ? (
            <button className="project-empty" type="button" onClick={() => void addProject()} title={projectPickerBusy ? "正在等待本地文件夹选择窗口" : "选择一个本地文件夹作为项目"} disabled={projectPickerBusy} aria-busy={projectPickerBusy}>
              <FolderOpen aria-hidden="true" />
              <span>{projectPickerBusy ? "正在选择项目文件夹" : "添加项目文件夹"}</span>
            </button>
          ) : (
            <div className="project-list">
              {projectSessionGroups.slice(0, 8).map(({ project, sessions }) => {
                const forceRevealGroup = project.id === activeProjectId || sessions.some((row) => sessionRowNeedsReveal(row));
                const groupCollapsed = Boolean(sidebarCollapse.projectGroups[project.id]) && !forceRevealGroup && !searchQuery.trim();
                return (
                <article className={`project-group ${project.id === activeProjectId ? "is-active" : ""}${groupCollapsed ? " is-collapsed" : ""}`} key={project.id}>
                  <div
                    className="project-row"
                    onContextMenu={(event) => showProjectMenu(event, project)}
                  >
                    <button
                      type="button"
                      onClick={() => selectOrCreateProjectSession(project)}
                      title={`${project.name}\n${project.path}\n项目记忆：${project.memoryPath || ".ecorex/project-memory.md"}`}
                    >
                      <FolderOpen aria-hidden="true" />
                      <span>{project.name}</span>
                    </button>
                    <button className="project-collapse-button" type="button" title={groupCollapsed ? "展开项目会话" : "折叠项目会话"} aria-expanded={!groupCollapsed} onClick={() => setSidebarCollapse((current) => ({ ...current, projectGroups: { ...current.projectGroups, [project.id]: !current.projectGroups[project.id] } }))}>
                      {groupCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
                    </button>
                    <button className="project-new-session-button" type="button" title={`为 ${project.name} 创建新会话`} aria-label={`为 ${project.name} 创建新会话`} onClick={() => startNewSession(project)}>
                      <Plus aria-hidden="true" />
                    </button>
                    <button className="project-menu-button" type="button" title="项目操作" aria-label="项目操作" onClick={(event) => showProjectMenu(event, project)}>
                      <MoreHorizontal aria-hidden="true" />
                    </button>
                  </div>
                  {!groupCollapsed && <div className="project-session-list" aria-label={`${project.name} 的会话`}>
                    {sessions.length ? (
                      sessions.map(renderSessionRow)
                    ) : (
                      <button className="project-session-empty" type="button" onClick={() => startNewSession(project)} title={`为 ${project.name} 创建项目会话`}>
                        新建项目会话
                      </button>
                    )}
                  </div>}
                </article>
                );
              })}
            </div>
          )}
        </section>

        <div className={`session-list${generalSessionsCollapsed ? " is-collapsed" : ""}`} aria-label="会话列表">
          <div className="sidebar-section-title">
            <button className="sidebar-collapse-button" type="button" onClick={() => setSidebarCollapse((current) => ({ ...current, generalSessions: !current.generalSessions }))} aria-expanded={!generalSessionsCollapsed} title={generalSessionsCollapsed ? "展开通用会话" : "折叠通用会话"}>
              {generalSessionsCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
              <span>通用会话</span>
            </button>
            <small>{generalSessions.length}</small>
          </div>
          {generalSessionsCollapsed ? null : generalSessions.length ? (
            <div className="session-groups">
              {pinnedGeneralSessions.length ? renderGeneralSessionGroup("置顶任务", pinnedGeneralSessions, "pinned") : null}
              {regularGeneralSessions.length ? renderGeneralSessionGroup("任务", regularGeneralSessions, "regular") : null}
            </div>
          ) : <div className="session-empty">暂无通用会话</div>}
        </div>

        <div className="sidebar-footer">
          {runCenterDevVisible && (
            <button className={`run-center-nav-button${runCenterOpen ? " is-active" : ""}`} onClick={() => openRunCenterSurface()} title="Open Run Center" aria-label="Open Run Center">
              <Activity aria-hidden="true" />
              <span>Run Center</span>
              <em>{runCenterNavCount > 99 ? "99+" : runCenterNavCount}</em>
            </button>
          )}
          <button onClick={() => { setSettingsSection("account"); setSettingsOpen(true); }} title="设置、能力包、权限和诊断"><Settings aria-hidden="true" />设置</button>
          <button onClick={() => { setSettingsSection("account"); setSettingsOpen(true); }} title={`${session.user.name} / ${session.user.email}`}><UserRound aria-hidden="true" />{session.user.name}</button>
        </div>
      </aside>

      {projectMenu && (() => {
        const project = projects.find((item) => item.id === projectMenu.projectId);
        if (!project) return null;
        const isPinned = Boolean(pinnedProjects[project.id] || project.pinned);
        return (
          <div className="context-menu" ref={projectMenuRef} style={fixedMenuStyle(projectMenu.x, projectMenu.y, 230, 152)}>
            <button type="button" onClick={() => openProjectInExplorer(project)}><FolderInput aria-hidden="true" />在资源管理器打开</button>
            <button type="button" onClick={() => { togglePinProject(project); setProjectMenu(null); }}>
              {isPinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}{isPinned ? "取消置顶项目" : "置顶项目"}
            </button>
            <button type="button" onClick={() => renameProject(project)}><Pencil aria-hidden="true" />重命名项目</button>
            <button type="button" onClick={() => deleteProject(project)}><FolderX aria-hidden="true" />删除项目</button>
          </div>
        );
      })()}

      {chatFileMenu && (
        <div className="context-menu chat-file-context-menu" ref={chatFileMenuRef} style={fixedMenuStyle(chatFileMenu.x, chatFileMenu.y, 230, 84)}>
          <button type="button" disabled={!chatFileMenu.canAdd} title={chatFileMenu.disabledReason || "添加到当前聊天"} onClick={() => void addFileToCurrentChat(chatFileMenu.file)}>
            <Paperclip aria-hidden="true" />添加到当前聊天
          </button>
          <button type="button" onClick={() => { void openArtifactFile(chatFileMenu.file, activeSessionId); setChatFileMenu(null); }}>
            <FolderOpen aria-hidden="true" />本地打开
          </button>
        </div>
      )}

      <section className={`chat-pane${isNewSessionView ? " is-new-session" : ""}`}>
        <header className="chat-header">
          <div>
            <h1>{activeSessionTitle}</h1>
            {activeProject && <small className="project-path" title={activeProject.path}>{activeProject.path}</small>}
          </div>
          <div className="chat-status">
            <span title={runtimeSnapshot.message}><Bot aria-hidden="true" />{runtimeSnapshot.status === "ready" ? "运行时已连接" : "等待运行时"}</span>
            {activeRuntimeElapsed && (
              <span className="chat-run-timing" title={`当前任务已处理 ${activeRuntimeElapsed}`}><Activity aria-hidden="true" />已处理 {activeRuntimeElapsed}</span>
            )}
            <span title="当前企业账号"><CheckCircle2 aria-hidden="true" />{session.user.email}</span>
            <button
              className="icon-button"
              title={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              data-tooltip={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              data-tooltip-position="bottom-right"
              aria-label={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}
            </button>
            <button className="icon-button" title="通知与运行状态" data-tooltip="通知与运行状态" data-tooltip-position="bottom-right" aria-label="通知与运行状态" onClick={() => setNotificationsOpen((open) => !open)}>
              <Bell aria-hidden="true" />
            </button>
          </div>
          {notificationsOpen && (
            <section className="chat-popover-panel">
              <strong>运行状态</strong>
              <span>{sidecarStatus.message}</span>
              <span>{runtimeSnapshot.message}</span>
            </section>
          )}
        </header>

        <div className="message-list" ref={messageListRef} onScroll={updateJumpLatestState}>
          {visibleMessages.length === 0 ? (
            <div className="empty-chat new-session-start">
              <h2>{NEW_SESSION_START_TITLE}</h2>
              <span>{activeProject ? `${activeProject.name} 项目会话` : "选择一个开始方式"}</span>
              <div className="new-session-actions" aria-label="新会话入口">
                <button className={`new-session-option${!activeProject ? " is-selected" : ""}`} type="button" onClick={() => startNewSession(null)} title="从通用会话开始" aria-pressed={!activeProject}>
                  <Bot aria-hidden="true" />
                  <strong>通用会话</strong>
                  <small>不绑定项目，适合临时问答、资料整理和轻量任务。</small>
                </button>
                <div className="new-session-project-picker" ref={projectStartMenuRef}>
                  <button
                    className={`new-session-option${activeProject ? " is-selected" : ""}`}
                    type="button"
                    onClick={() => setProjectStartMenuOpen((open) => !open)}
                    disabled={projectPickerBusy}
                    aria-busy={projectPickerBusy}
                    aria-expanded={projectStartMenuOpen}
                    aria-haspopup="menu"
                    title={projectPickerBusy ? "正在选择项目文件夹" : "选择已有项目或导入新文件夹"}
                    aria-pressed={Boolean(activeProject)}
                  >
                    <FolderOpen aria-hidden="true" />
                    <strong>{projectPickerBusy ? "选择中" : activeProject ? activeProject.name : "项目文件夹"}</strong>
                    <small>选择已有项目，或导入新文件夹作为本次项目会话上下文。</small>
                  </button>
                  {projectStartMenuOpen && (
                    <div className="new-session-project-menu" role="menu" aria-label="选择项目开始">
                      <label className="project-start-search" title="搜索项目">
                        <Search aria-hidden="true" />
                        <input value={projectStartSearch} onChange={(event) => setProjectStartSearch(event.target.value)} placeholder="搜索项目" autoFocus />
                      </label>
                      <div className="project-start-list">
                        {projectStartMatches.length ? projectStartMatches.map((project) => (
                          <button key={project.id} type="button" role="menuitem" onClick={() => startProjectFromWelcome(project)} title={`${project.name}\n${project.path}`}>
                            <FolderOpen aria-hidden="true" />
                            <span><strong>{project.name}</strong><small>{project.path}</small></span>
                            {project.id === activeProjectId && <CheckCircle2 aria-hidden="true" />}
                          </button>
                        )) : (
                          <div className="project-start-empty">没有匹配的项目</div>
                        )}
                      </div>
                      <div className="project-start-actions">
                        <button type="button" role="menuitem" onClick={() => void addProject()} disabled={projectPickerBusy} aria-busy={projectPickerBusy}>
                          <FolderPlus aria-hidden="true" />
                          <span>导入新文件夹</span>
                        </button>
                        <button type="button" role="menuitem" onClick={() => startNewSession(null)}>
                          <FolderX aria-hidden="true" />
                          <span>不使用项目</span>
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
              <p className="new-session-helper">
                {activeProject
                  ? `将从 ${activeProject.name} 的项目文件夹开始，旧项目会话不会被自动复用。`
                  : "将从不绑定项目的通用会话开始，不会串入项目文件夹上下文。"}
              </p>
            </div>
          ) : (
            visibleMessages.map((message) => {
              const messageSessionId = activeSessionId;
              const messageFiles = message.attachments || [];
              const hasMessageFiles = messageFiles.length > 0;
              const messageFileList = hasMessageFiles ? (
                <div className="message-files">
                  {messageFiles.map((file) => {
                    const previewUrl = attachmentPreviewUrl(file);
                    const openLabel = `点击在本地打开 ${file.file_name}`;
                    return (
                      <button key={file.file_path} onClick={() => void openArtifactFile(file, messageSessionId)} onContextMenu={(event) => showChatFileMenu(event, file)} title={openLabel} aria-label={openLabel}>
                        {previewUrl ? <img src={previewUrl} alt={file.file_name} loading="lazy" /> : fileIcon(file)}<span>{file.file_name}</span>
                      </button>
                    );
                  })}
                </div>
              ) : null;
              return (
              <article className={`message ${message.role}${hasMessageFiles ? " has-files" : ""}`} key={message.id}>
                <div className="message-body">
                  {message.role === "user" ? messageFileList : null}
                  <div className={message.role === "user" ? "message-text-bubble" : "message-content-shell"}>
                    <button
                      type="button"
                      className="message-copy-button"
                      onClick={() => void copyMessageText(message)}
                      title={copiedMessageId === message.id ? "已复制" : "复制文本"}
                      aria-label={copiedMessageId === message.id ? "已复制" : "复制文本"}
                    >
                      <Copy aria-hidden="true" />
                    </button>
                    <MessageContent
                      role={message.role}
                      content={message.content}
                      pending={message.pending}
                      paused={message.paused}
                      cancelled={message.cancelled}
                      reasoning={message.reasoning}
                      steps={message.steps}
                      toolCalls={message.toolCalls}
                      artifacts={message.artifacts}
                      runTimingLabel={messageRunTimingLabel(message) || undefined}
                      onOpenLocalFile={handleOpenMessageLocalFile}
                      localFilePreviewUrl={messageLocalFilePreviewUrl}
                      localFileJson={messageLocalJson}
                      localFileStat={messageLocalFileStat}
                      onLocalFileContextMenu={showChatFileMenu}
                    />
                    {message.role !== "user" ? messageFileList : null}
                    {renderRecoveryActions(message, messageSessionId)}
                    {renderMessageRunTiming(message)}
                  </div>
                </div>
              </article>
              );
            })
          )}
        </div>

        {showJumpLatest && (
          <button
            type="button"
            className="jump-latest-button"
            onClick={() => scrollToLatest(true)}
            title="回到最新消息"
            aria-label="回到最新消息"
          >
            <ArrowDownToLine aria-hidden="true" />
          </button>
        )}

        <div className="composer-zone">
          {approval && (
            <section className={`approval-bar is-${approval.type}`}>
              <strong>{approval.title}</strong>
              <span>{approval.message}</span>
              <div>
                {approval.type === "capability" ? (
                  <>
                    {approval.pack.policyMode === "disabled" ? (
                      <button onClick={() => setApproval(null)}>知道了</button>
                    ) : (
                      <>
                        <button className="primary-action" onClick={() => void handleInstallPack(approval.pack)}>
                          {isConfigureOnlyCapability(approval.pack) ? "配置检查" : approval.pack.discoveryOnly ? "用 find skill" : "安装并继续"}
                        </button>
                        <button onClick={() => { setApproval(null); approval.resume(); }}>跳过继续</button>
                      </>
                    )}
                    <button onClick={() => setApproval(null)}>取消</button>
                  </>
                ) : approval.type === "open-file" ? (
                  <>
                    <button className="primary-action" onClick={() => void confirmOpenFile(approval.file)}>允许本次</button>
                    <button onClick={() => setApproval(null)}>取消</button>
                  </>
                ) : approval.actions?.length ? (
                  approval.actions.map((action) => <button key={action.label} className={action.primary ? "primary-action" : ""} onClick={action.onClick}>{action.label}</button>)
                ) : (
                  <button onClick={() => setApproval(null)}>知道了</button>
                )}
              </div>
            </section>
          )}

          <form
            className={`composer${composerDragActive ? " is-drag-active" : ""}`}
            onSubmit={(event) => { event.preventDefault(); void sendNow(); }}
            onDragEnter={handleComposerDragEnter}
            onDragOver={handleComposerDragOver}
            onDragLeave={handleComposerDragLeave}
            onDrop={handleComposerDrop}
          >
            {attachments.length > 0 && (
              <div className="attachment-tray">
                {attachments.map((file) => (
                  <article key={file.file_path}>
                    <button className="attachment-preview" type="button" onClick={() => previewOrOpenFile(file)} title={isImageAttachment(file) ? "点击预览图片" : "点击在本地打开"}>
                      {file.previewDataUrl ? <img src={file.previewDataUrl} alt="" /> : fileIcon(file)}
                      <span>{file.file_name}</span>
                    </button>
                    <button
                      className="attachment-remove"
                      type="button"
                      aria-label={`移除 ${file.file_name}`}
                      title="移除附件"
                      onClick={() => setAttachments((current) => current.filter((item) => item.file_path !== file.file_path))}
                    >
                      <X aria-hidden="true" />
                    </button>
                  </article>
                ))}
              </div>
            )}
            {(skillMentions.length > 0 || skillMentionNoResults) && (
              <div className="skill-mention-popover" role="listbox" aria-label="选择 Skill">
                {skillMentionGroups.map((group) => (
                  <div className="skill-mention-group" key={group.category}>
                    <div className="skill-mention-group-label">{group.label}</div>
                    {group.items.map((skill) => (
                      <button key={skill.key} type="button" onClick={() => insertSkillMention(skill)} title={skill.path || skill.source || "Skill"}>
                        <AtSign aria-hidden="true" />
                        <span>{skill.displayName || skill.name}</span>
                        <em>{skill.categoryLabel}</em>
                      </button>
                    ))}
                  </div>
                ))}
                {skillMentionNoResults && (
                  <div className="skill-mention-empty" role="option" aria-disabled="true">
                    <AtSign aria-hidden="true" />
                    <span>{hiddenSkillMentions.length ? "后台 / CLI 辅助" : "没有匹配的 Skill"}</span>
                    <em>{hiddenSkillMentions.length ? (hiddenSkillMentions[0].mentionHiddenReason || "后台触发") : "换个关键词试试"}</em>
                  </div>
                )}
              </div>
            )}
            <button type="button" className="round-button" onClick={chooseFiles} title="添加本地文件"><Paperclip aria-hidden="true" /></button>
            <textarea
              ref={composerRef}
              defaultValue={composerText}
              placeholder="给 EcoreX 发送消息，支持粘贴图片或文件"
              onChange={(event) => handleComposerDraftInput(event.target.value)}
              onKeyDown={handleComposerKey}
              onPaste={(event) => void handlePaste(event)}
              rows={1}
            />
            <button type="button" className="mode-button" onClick={() => void loadRuntimeSnapshot().then(setRuntimeSnapshot).catch(() => undefined)} title={`当前模型：${currentModelName}`}>
              <Bot aria-hidden="true" />{currentModelName}<ChevronDown aria-hidden="true" />
            </button>
            {(activeSessionRequestId || hasPendingAssistantMessage) && !composerHasPayload ? (
              <button type="button" className="send-button stop" onClick={stopActiveRequest} title="停止当前回复"><Square aria-hidden="true" /></button>
            ) : (
              <button type="submit" className="send-button" disabled={!composerHasPayload} title={activeSessionRequestId || hasPendingAssistantMessage ? "发送并暂停上一条回复" : "发送，Enter 也可以发送"}>
                <SendHorizontal aria-hidden="true" />
              </button>
            )}
            <div className="composer-drop-overlay" aria-hidden="true">
              <Upload aria-hidden="true" />
              <span>松开添加</span>
            </div>
            <div className="composer-footer">
              <div className="composer-permission-row" aria-label="本机访问权限">
                <div className="composer-permission-menu">
                  <button
                    type="button"
                    className="composer-permission-trigger"
                    aria-haspopup="menu"
                    aria-expanded={permissionMenuOpen}
                    title={`本机访问权限：${composerPermissionTitle(currentComposerPermissionMode)}`}
                    onClick={() => setPermissionMenuOpen((open) => !open)}
                  >
                    {composerPermissionIcon(currentComposerPermissionMode)}
                    <span>{composerPermissionTitle(currentComposerPermissionMode)}</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  {permissionMenuOpen && (
                    <div className="composer-permission-popover" role="menu">
                      <div className="composer-permission-help">
                        <span>应如何批准 EcoreX 操作?</span>
                        <button type="button" onClick={() => { setSettingsSection("permissions"); setSettingsOpen(true); setPermissionMenuOpen(false); }}>
                          了解更多
                        </button>
                      </div>
                      {COMPOSER_PERMISSION_MENU_MODES.map((mode) => (
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={currentComposerPermissionMode === mode}
                          key={mode}
                          className={currentComposerPermissionMode === mode ? "is-active" : ""}
                          onClick={() => updatePermissionMode(mode).then((next) => {
                            setPermissionState(next);
                            setPermissionMenuOpen(false);
                          }).catch(() => setToast("权限模式切换失败"))}
                        >
                          {composerPermissionIcon(mode)}
                          <span>
                            <strong>{composerPermissionTitle(mode)}</strong>
                            <small>{composerPermissionDetail(mode)}</small>
                          </span>
                          {currentComposerPermissionMode === mode && <CheckCircle2 aria-hidden="true" />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              <div className="composer-metrics" aria-label="Token 和上下文用量">
                <div className="composer-token-meters" aria-label="Token 用量">
                  {tokenMeters.map((meter) => (
                    <div className={`composer-meter composer-meter-${meter.key}`} key={meter.key} title={meter.title} data-tooltip={meter.title} data-tooltip-position="top-right">
                      <span>{meter.label}</span>
                      <div className="composer-meter-track" aria-hidden="true">
                        <i style={{ width: `${meter.percent}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
                <div
                  className="composer-context-meter"
                  title={contextMeter.title}
                  data-tooltip={contextMeter.title}
                  data-tooltip-position="top-right"
                  style={{ "--context-meter-percent": `${contextMeter.percent}%` } as CSSProperties}
                >
                  <span>{contextMeter.label}</span>
                  <i aria-hidden="true" />
                </div>
              </div>
            </div>
          </form>
        </div>
      </section>

      {settingsOpen && (
        <div className="modal-backdrop" onClick={() => setSettingsOpen(false)}>
          <section className="settings-sheet" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <h2>设置</h2>
                <span>账号、项目、能力、外部连接、权限、记忆和诊断</span>
              </div>
              <button className="icon-button" title="关闭设置" aria-label="关闭设置" onClick={() => setSettingsOpen(false)}><X aria-hidden="true" /></button>
            </header>
            <div className="settings-layout">
              <nav className="settings-nav" aria-label="设置分区">
                {settingsNav.map((item) => (
                  <button
                    key={item.id}
                    className={settingsSection === item.id ? "is-active" : ""}
                    onClick={() => setSettingsSection(item.id)}
                    title={item.label}
                  >
                    {item.icon}
                    <span>{item.label}</span>
                  </button>
                ))}
              </nav>
              <div className="settings-content">
                {settingsSection === "account" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>账号与外观</strong>
                      <span>{session.user.name} / {session.user.email}</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>主题</strong><span>当前为 {theme === "dark" ? "深色" : "明亮"} 模式</span></div>
                        <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}切换</button>
                      </article>
                      <article>
                        <div><strong>登录账号</strong><span>{session.user.email}</span></div>
                        <button onClick={logout}><LogOut aria-hidden="true" />退出</button>
                      </article>
                    </div>
                    <form className="password-form" onSubmit={submitPasswordChange}>
                      <strong>修改登录密码</strong>
                      <input value={passwordDraft.oldPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, oldPassword: event.target.value }))} type="password" placeholder="当前密码" autoComplete="current-password" required />
                      <input value={passwordDraft.newPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, newPassword: event.target.value }))} type="password" placeholder="新密码，至少 8 位" autoComplete="new-password" minLength={8} required />
                      <input value={passwordDraft.confirmPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, confirmPassword: event.target.value }))} type="password" placeholder="确认新密码" autoComplete="new-password" minLength={8} required />
                      <button type="submit" disabled={passwordBusy}>{passwordBusy ? "保存中" : "更新密码"}</button>
                    </form>
                  </section>
                )}

                {settingsSection === "projects" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>项目</strong>
                      <span>项目会话会自动引用项目文件夹，并把总结沉淀到项目记忆</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>添加项目</strong><span>选择一个本地文件夹作为项目工作区</span></div>
                        <button onClick={() => void addProject()} disabled={projectPickerBusy} aria-busy={projectPickerBusy} title={projectPickerBusy ? "正在选择项目文件夹" : "添加项目"}><FolderPlus aria-hidden="true" />{projectPickerBusy ? "选择中" : "添加"}</button>
                      </article>
                      {projects.map((project) => (
                        <article key={project.id}>
                          <div><strong>{project.name}</strong><span title={project.path}>{project.path}</span></div>
                          <button onClick={() => startNewSession(project)}><Plus aria-hidden="true" />开始会话</button>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                {settingsSection === "abilities" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>能力</strong>
                      <span>点击安装后由当前会话 agent 诊断、安装和修复；勾选只控制是否参与自动触发</span>
                    </div>
                    {installNotice && !installNotice.dismissed && (
                      <div className="install-notice" role="status">
                        <strong>{installNotice.message}</strong>
                        <span>安装会在当前会话继续执行，关闭提示不会中断任务。</span>
                        <button type="button" onClick={() => setInstallNotice((current) => current ? { ...current, dismissed: true } : current)}>
                          关闭
                        </button>
                      </div>
                    )}
                    <div className="ability-grid">
                      {abilityRows.map((ability) => (
                        <article key={ability.id} className={ability.enabled ? "is-ready" : "is-waiting"}>
                          {ability.icon}
                          <div><strong>{ability.name}</strong><span>{ability.detail}</span></div>
                          {"pack" in ability && ability.pack && !ability.enabled && !ability.pack.installed ? (
                            <button disabled={Boolean(installingPackIds[ability.pack.id])} onClick={() => void handleInstallPack(ability.pack!)}>
                              {packActionLabel(ability.pack, Boolean(installingPackIds[ability.pack.id]))}
                            </button>
                          ) : <em>{ability.statusLabel}</em>}
                        </article>
                      ))}
                    </div>
                    <div className="skill-toggle-list">
                      <div className="toggle-list-head"><strong>Skill</strong><span>{skillDisplayRows.length}</span></div>
                      {skillSourceSections.map((section) => (
                        <section key={section.sourceGroup} className="skill-source-section">
                          <div className="skill-source-heading"><strong>{section.label}</strong><span>{section.count}</span></div>
                          {section.purposeGroups.map((group) => (
                            <div key={`${section.sourceGroup}-${group.purposeGroup}`} className="skill-purpose-group">
                              <div className="skill-category-heading"><strong>{group.label}</strong><span>{group.items.length}</span></div>
                              {group.items.map((skill) => {
                                const name = skill.name || "";
                                const enabled = skill.enabled;
                                const statusText = skill.locked
                                  ? skill.lockReason || "内置能力默认启用"
                                  : enabled
                                    ? "已启用"
                                    : "已关闭";
                                const meta = [
                                  skill.sourceLabel,
                                  skill.mentionable ? "@可提及" : skill.mentionHiddenReason || "后台触发",
                                  skill.status || (enabled ? "ready" : "disabled"),
                                  skill.policy
                                ].filter(Boolean).join(" · ");
                                return (
                                  <label key={skill.key} className={`toggle-row${skill.toggleable ? "" : " is-readonly"}`} title={skill.path || skill.source || name}>
                                    <input type="checkbox" checked={enabled} disabled={!skill.toggleable} onChange={(event) => void toggleRuntimeSkill(skill, event.currentTarget.checked)} />
                                    <span><strong>{skill.displayName || name || "未命名 Skill"}</strong><small>{meta}</small></span>
                                    <em>{statusText}</em>
                                  </label>
                                );
                              })}
                            </div>
                          ))}
                        </section>
                      ))}
                      {!skillDisplayRows.length && <div className="session-empty">运行时暂未返回 Skill 列表</div>}
                    </div>
                    <div className="pack-list">
                      {packs.map((pack) => (
                        <article key={pack.id}>
                          <label className="pack-toggle" title="控制该能力包是否参与自动触发；安装状态不会被删除">
                            <input
                              type="checkbox"
                              checked={capabilityPackEnabled(pack.id)}
                              disabled={isDefaultReadOnlyCapability(pack)}
                              onChange={(event) => toggleCapabilityPack(pack, event.currentTarget.checked)}
                            />
                            <span>生效</span>
                          </label>
                          <div>
                            <strong>{pack.name}</strong>
                            <span>{installingPackIds[pack.id] ? `${packActionLabel(pack, true)}，请稍候` : isDefaultReadOnlyCapability(pack) ? `默认只读 · ${pack.message}` : pack.message}</span>
                          </div>
                          <button disabled={pack.installed || pack.policyMode === "disabled" || Boolean(installingPackIds[pack.id])} onClick={() => void handleInstallPack(pack)}>
                            {installingPackIds[pack.id]
                              ? packActionLabel(pack, true)
                              : pack.installed
                                ? packReadyLabel(pack)
                                : pack.policyMode === "disabled"
                                  ? "管理员禁用"
                                  : packActionLabel(pack)}
                          </button>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                {settingsSection === "external-connections" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>外部连接</strong>
                      <span>{externalConnections.length} 个消息平台；状态来自后端连接投影</span>
                    </div>
                    <div className="external-connections-toolbar">
                      <button
                        type="button"
                        className="external-connections-refresh"
                        onClick={() => void refreshExternalConnections()}
                        disabled={externalConnectionsBusy}
                        title="刷新外部连接"
                      >
                        <RefreshCw aria-hidden="true" />
                        刷新
                      </button>
                    </div>
                    <div className="external-connections-grid">
                      {externalConnections.map((connection) => {
                        const label = connection.displayName || connection.label?.zh || connection.label?.en || connection.id;
                        const fields = connection.fields || [];
                        const cardState = externalConnectionCardState(connection);
                        const connected = cardState === "connected";
                        const connectionNotice = externalConnectionNotice(connection);
                        return (
                          <article className={`external-connection-card is-${cardState}`} key={connection.id}>
                            <div className="external-connection-head">
                              <span className={`connection-logo is-${connection.logo?.key || connection.id}`} aria-hidden="true">
                                {connection.logo?.fallbackText || label.slice(0, 2)}
                              </span>
                              <div>
                                <strong>{label}</strong>
                                <small>{externalConnectionDescription(connection, label)}</small>
                              </div>
                              <em>{externalConnectionStatusLabel(connection)}</em>
                            </div>
                            {connectionNotice ? <div className="external-connection-error">{connectionNotice}</div> : null}
                            <div className="external-connection-meta">
                              <span>{connection.configured ? "已配置" : "未配置"}</span>
                              <span>{externalConnectionConfigLabel(connection)}</span>
                              <span>{externalConnectionCallableLabel(connection)}</span>
                            </div>
                            <div className="external-connection-fields">
                              {fields.length ? fields.map((field) => (
                                <label key={field.key}>
                                  <span>{externalConnectionFieldLabel(field)}</span>
                                  {field.type === "bool" ? (
                                    <input
                                      type="checkbox"
                                      checked={Boolean(externalConnectionFieldValue(connection, field.key))}
                                      onChange={(event) => updateExternalConnectionDraft(connection, field.key, event.currentTarget.checked)}
                                    />
                                  ) : (
                                    <input
                                      type={field.type === "secret" ? "password" : field.type === "number" ? "number" : "text"}
                                      value={String(externalConnectionFieldValue(connection, field.key) || "")}
                                      onChange={(event) => updateExternalConnectionDraft(connection, field.key, field.type === "number" ? Number(event.currentTarget.value || 0) : event.currentTarget.value)}
                                      autoComplete="off"
                                    />
                                  )}
                                </label>
                              )) : (
                                <div className="external-connection-empty">该平台使用扫码、授权或运行时状态完成连接</div>
                              )}
                            </div>
                            <div className="external-connection-actions">
                              {externalConnectionActions(connection, connected).map((action) => {
                                const disabled = externalConnectionsBusy
                                  || action.enabled === false
                                  || (action.id === "save_config" && !fields.length)
                                  || (action.id === "set_home_channel" && !externalConnectionHomeChannel(connection).id);
                                return (
                                  <button
                                    type="button"
                                    key={action.id}
                                    className={`external-connection-action-button is-${externalConnectionActionTone(action.id)}`}
                                    onClick={() => void applyExternalConnectionAction(connection, action.id)}
                                    disabled={disabled}
                                    title={externalConnectionActionLabel(action)}
                                  >
                                    {externalConnectionActionIcon(action.id)}
                                    {externalConnectionActionLabel(action)}
                                  </button>
                                );
                              })}
                            </div>
                          </article>
                        );
                      })}
                      {!externalConnections.length && (
                        <div className="session-empty">运行时暂未返回外部连接列表</div>
                      )}
                    </div>
                  </section>
                )}

                {settingsSection === "scheduler" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>定时</strong>
                      <span>{schedulerStatusLabel()}，{schedulerProjection.taskCount || 0} 个任务；状态来自后端调度投影</span>
                    </div>
                    <div className="scheduler-panel" aria-label="定时任务管理">
                      <div className="scheduler-toolbar">
                        <div className="scheduler-status">
                          <Bell aria-hidden="true" />
                          <span>{schedulerProjection.serviceStatus || "unknown"}</span>
                          {schedulerProjection.modifyBlockingReason ? <small>{schedulerProjection.modifyBlockingReason}</small> : schedulerProjection.blockingReason ? <small>{schedulerProjection.blockingReason}</small> : null}
                        </div>
                        <div className="scheduler-toolbar-actions">
                          <button type="button" onClick={() => void refreshSchedulerPanel()} disabled={schedulerBusy} title="刷新定时任务投影">
                            <RefreshCw aria-hidden="true" />
                            刷新
                          </button>
                          {schedulerProjection.running ? (
                            <button type="button" onClick={() => void applySchedulerAction({ action: "stop" }, "定时服务已停止")} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "停止调度服务" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                              <Square aria-hidden="true" />
                              停止
                            </button>
                          ) : (
                            <button type="button" onClick={() => void applySchedulerAction({ action: "start" }, "定时服务已启动")} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "启动调度服务" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                              <CheckCircle2 aria-hidden="true" />
                              启动
                            </button>
                          )}
                        </div>
                      </div>
                      <div className="scheduler-stats" aria-label="定时任务统计">
                        <span>全部 {schedulerProjection.counts?.total || schedulerProjection.taskCount || 0}</span>
                        <span>启用 {schedulerProjection.counts?.enabled || 0}</span>
                        <span>暂停 {schedulerProjection.counts?.disabled || 0}</span>
                        <span>异常 {schedulerProjection.counts?.error || 0}</span>
                      </div>
                      <div className="scheduler-task-list">
                        {schedulerTasks.map((task) => (
                          <article className={`scheduler-task-row is-${task.state || (task.enabled ? "enabled" : "disabled")}`} key={task.id}>
                            <div className="scheduler-task-main">
                              <span className="scheduler-task-state">{task.state || (task.enabled ? "enabled" : "disabled")}</span>
                              <strong>{task.name || task.id}</strong>
                              <small>{task.scheduleDescription || "schedule unknown"} · {schedulerTaskActionLabel(task)}</small>
                              <p>{schedulerTaskDetail(task)}</p>
                            </div>
                            <div className="scheduler-task-meta">
                              <span>下次 {task.nextRunAt ? formatTime(task.nextRunAt) : "未计算"}</span>
                              <span>上次 {task.lastRunAt ? formatTime(task.lastRunAt) : "暂无"}</span>
                              {task.lastError ? <span className="is-error" title={task.lastError}>错误 {task.lastError}</span> : null}
                            </div>
                            <div className="scheduler-task-actions">
                              <button type="button" onClick={() => void renameSchedulerTask(task)} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "重命名任务" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                                <Pencil aria-hidden="true" />
                                命名
                              </button>
                              <button type="button" onClick={() => void editSchedulerTaskCron(task)} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "修改 Cron 表达式" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                                <RefreshCw aria-hidden="true" />
                                Cron
                              </button>
                              <button type="button" onClick={() => void editSchedulerTaskContent(task)} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "修改任务内容" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                                <FileText aria-hidden="true" />
                                内容
                              </button>
                              <button
                                type="button"
                                onClick={() => void applySchedulerAction({ action: task.enabled ? "disable" : "enable", task_id: task.id }, task.enabled ? "定时任务已暂停" : "定时任务已启用")}
                                disabled={schedulerBusy || !schedulerCanModify}
                                title={schedulerCanModify ? (task.enabled ? "暂停任务" : "启用任务") : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}
                              >
                                {task.enabled ? <Square aria-hidden="true" /> : <CheckCircle2 aria-hidden="true" />}
                                {task.enabled ? "暂停" : "启用"}
                              </button>
                              <button type="button" onClick={() => window.confirm(`删除定时任务「${task.name || task.id}」？`) && void applySchedulerAction({ action: "delete", task_id: task.id }, "定时任务已删除")} disabled={schedulerBusy || !schedulerCanModify} title={schedulerCanModify ? "删除任务" : schedulerProjection.modifyBlockingReason || "当前不可修改定时任务"}>
                                <Trash2 aria-hidden="true" />
                                删除
                              </button>
                            </div>
                          </article>
                        ))}
                        {!schedulerTasks.length && (
                          <div className="scheduler-empty">
                            <Bell aria-hidden="true" />
                            <span>暂无定时任务；通过聊天让 agent 创建任务后，这里会显示可管理列表。</span>
                          </div>
                        )}
                      </div>
                    </div>
                  </section>
                )}

                {settingsSection === "permissions" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>权限</strong>
                      <span>{permissionState ? `当前 ${permissionModeLabel(permissionState.mode)}，保存 ${permissionState.grantsCount} 条授权` : "权限状态暂不可用"}</span>
                    </div>
                    <div className="permission-modes">
                      {SETTINGS_PERMISSION_MODES.map((mode) => (
                        <button key={mode} className={permissionState?.mode === mode ? "is-active" : ""} onClick={() => updatePermissionMode(mode).then(setPermissionState)}>
                          {permissionModeLabel(mode)}
                        </button>
                      ))}
                    </div>
                    <div className="settings-list">
                      <article><div><strong>授权记录</strong><span>{permissionState?.auditPath || "暂无记录"}</span></div><button onClick={() => resetPermissionGrants().then(setPermissionState)}>清空授权</button></article>
                      <article><div><strong>文件预览</strong><span>点击附件才显示预览，系统打开前会进行权限确认</span></div><button onClick={() => setPreviewFile(null)}>关闭预览</button></article>
                    </div>
                  </section>
                )}

                {settingsSection === "memory" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>记忆</strong>
                      <span>{activeProject ? `项目记忆：${activeProject.name}` : "原项目记忆入口"}</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>项目记忆</strong><span title={activeProjectMemoryPath}>{activeProjectMemoryPath || "选择项目后自动创建 .ecorex/project-memory.md"}</span></div>
                        <button disabled={!activeProject} onClick={() => activeProject?.memoryPath && requestOpenFile({ file_path: activeProject.memoryPath, file_name: "project-memory.md", file_type: "file" })}><BookOpen aria-hidden="true" />打开</button>
                      </article>
                      <article>
                        <div><strong>梦境蒸馏</strong><span>{activeProject ? "项目会话只写入项目梦境目录" : "通用会话保留原项目记忆入口"}</span></div>
                        <em>已开启</em>
                      </article>
                    </div>
                    <div className="memory-list">
                      {[...memoryFiles, ...dreamFiles].slice(0, 8).map((file) => (
                        <article key={`${file.category || ""}-${memoryFileName(file)}`}>
                          <Brain aria-hidden="true" />
                          <div><strong>{memoryFileName(file)}</strong><span>{memoryFileTime(file)}</span></div>
                        </article>
                      ))}
                      {!memoryFiles.length && !dreamFiles.length && <div className="session-empty">暂无可展示的原项目记忆文件</div>}
                    </div>
                  </section>
                )}

                {settingsSection === "diagnostics" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>诊断</strong>
                      <span>{sidecarStatus.message}</span>
                    </div>
                    {runCenterDevVisible ? renderRunCenterPanel("settings") : null}
                    <div className="settings-list">
                      <article><div><strong>运行时</strong><span>{runtimeSnapshot.message}</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                      <article><div><strong>模型策略</strong><span>{runtimeSnapshot.modelsCount} 个企业模型映射</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>刷新</button></article>
                      <article><div><strong>Skill / MCP</strong><span>{runtimeSnapshot.skillsCount} 个 Skill，{runtimeSnapshot.toolsCount} 个工具通道，{runtimeSnapshot.extensionsCount || 0} 个扩展登记</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                      <article><div><strong>诊断包</strong><span>导出运行状态、活动请求和脱敏日志摘要</span></div><button onClick={() => void handleExportDiagnostics()}>生成</button></article>
                    </div>
                  </section>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      {runCenterDevVisible && runCenterOpen && (
        <div className="modal-backdrop run-center-backdrop" onClick={() => setRunCenterOpen(false)}>
          <section className="run-center-sheet" role="dialog" aria-modal="true" aria-labelledby="run-center-title" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>Runtime control</span>
                <h2 id="run-center-title">Run Center</h2>
              </div>
              <button className="icon-button" title="Close Run Center" aria-label="Close Run Center" onClick={() => setRunCenterOpen(false)}><X aria-hidden="true" /></button>
            </header>
            {renderRunCenterPanel("primary")}
          </section>
        </div>
      )}

      {releaseNotesOpen && releaseNotes && (
        <div className="modal-backdrop release-notes-backdrop" onClick={closeReleaseNotes}>
          <section className="release-notes-sheet" role="dialog" aria-modal="true" aria-labelledby="release-notes-title" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>EcoreX {releaseNotes.version}</span>
                <h2 id="release-notes-title">{releaseNotes.title || "更新说明"}</h2>
                {releaseNotes.summary && <p>{releaseNotes.summary}</p>}
              </div>
              <button className="icon-button" title="关闭更新说明" aria-label="关闭更新说明" onClick={closeReleaseNotes}><X aria-hidden="true" /></button>
            </header>
            <div className="release-notes-content">
              {!!releaseNotes.highlights?.length && (
                <section>
                  <strong>新增和改进</strong>
                  <ul>
                    {releaseNotes.highlights.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {!!releaseNotes.fixes?.length && (
                <section>
                  <strong>修复的问题</strong>
                  <ul>
                    {releaseNotes.fixes.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {!!releaseNotes.howTo?.length && (
                <section>
                  <strong>怎么用</strong>
                  <ul>
                    {releaseNotes.howTo.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {releaseNotes.updatePolicy && (
                <section className="release-notes-update-policy">
                  <strong>更新方式</strong>
                  {releaseNotes.updatePolicy.windows && <p>{releaseNotes.updatePolicy.windows}</p>}
                  {releaseNotes.updatePolicy.macos && <p>{releaseNotes.updatePolicy.macos}</p>}
                  {releaseNotes.updatePolicy.webui && <p>{releaseNotes.updatePolicy.webui}</p>}
                </section>
              )}
            </div>
            <footer>
              <button type="button" onClick={closeReleaseNotes}>知道了</button>
            </footer>
          </section>
        </div>
      )}

      {previewFile && isImageAttachment(previewFile) && (
        <div className="preview-popover image-preview-popover" style={{ "--preview-zoom": previewZoom, "--preview-width": `${previewZoom * 100}%` } as CSSProperties}>
          <header>
            <strong>{previewFile.file_name}</strong>
            <span className="preview-actions">
              <button className="icon-button" title="缩小" aria-label="缩小" onClick={() => setPreviewZoom((value) => Math.max(0.5, Math.round((value - 0.25) * 100) / 100))}><ZoomOut aria-hidden="true" /></button>
              <button className="icon-button" title="放大" aria-label="放大" onClick={() => setPreviewZoom((value) => Math.min(3, Math.round((value + 0.25) * 100) / 100))}><ZoomIn aria-hidden="true" /></button>
              <button className="icon-button" title="关闭预览" aria-label="关闭预览" onClick={() => setPreviewFile(null)}><X aria-hidden="true" /></button>
            </span>
          </header>
          <div className="preview-image-frame">
            <img src={previewFile.previewDataUrl || filePreviewUrl(previewFile.file_path, sidecarStatus.webPort)} alt={previewFile.file_name} />
          </div>
          <button onClick={() => requestOpenFile(previewFile)}>在系统中打开</button>
        </div>
      )}

      {toast && <div className="toast" onAnimationEnd={() => setToast("")}>{toast}</div>}
    </main>
  );
}
