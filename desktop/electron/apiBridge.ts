import type { SidecarManager } from "./sidecar.js";

type ApiRequest = {
  path: string;
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
};

const MAX_SIDECAR_RESPONSE_BYTES = 2 * 1024 * 1024;

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function sanitizeBridgeSnippet(value: string, runtimeToken = "") {
  let text = String(value || "")
    .replace(/(X-EcoreX-Runtime-Token["':=\s]+)([A-Za-z0-9._~+/=-]{8,})/gi, "$1[redacted]")
    .replace(/(Authorization["':=\s]+Bearer\s+)([A-Za-z0-9._~+/=-]{8,})/gi, "$1[redacted]")
    .replace(/\bsk-[A-Za-z0-9_-]{16,}\b/g, "[api-key]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[email]")
    .replace(/C:\\Users\\[^\\\s]+/gi, "C:\\Users\\[user]");
  if (runtimeToken) {
    text = text.replace(new RegExp(escapeRegExp(runtimeToken), "g"), "[runtime-token]");
  }
  return text;
}

async function readResponseTextWithLimit(response: Response, maxBytes = MAX_SIDECAR_RESPONSE_BYTES) {
  if (!response.body) {
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > maxBytes) {
      throw new Error(`sidecar response exceeded ${maxBytes} bytes`);
    }
    return text;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let bytes = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxBytes) {
        await reader.cancel().catch(() => undefined);
        throw new Error(`sidecar response exceeded ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

function isAllowedPath(pathname: string, method: string) {
  const cleanPath = pathname.split("?")[0] || "/";
  const upperMethod = method.toUpperCase();
  const exact = new Set([
    "GET /auth/check",
    "POST /auth/login",
    "POST /auth/logout",
    "POST /message",
    "POST /cancel",
    "GET /api/tool-permissions",
    "POST /api/tool-permissions",
    "GET /api/active-requests",
    "GET /api/image-jobs",
    "POST /upload",
    "POST /api/image-jobs",
    "GET /api/sessions",
    "GET /api/history",
    "POST /api/messages/delete",
    "GET /api/tools",
    "GET /api/skills",
    "POST /api/skills",
    "GET /api/memory",
    "GET /api/memory/content",
    "GET /api/models",
    "GET /api/channels",
    "POST /api/channels",
    "GET /api/weixin/qrlogin",
    "POST /api/weixin/qrlogin",
    "GET /api/feishu/register",
    "POST /api/feishu/register",
    "GET /api/scheduler",
    "GET /api/version",
    "GET /api/logs",
    "GET /api/logs/snapshot",
    "GET /api/diagnostics/bundle",
    "POST /api/file-stat",
    "POST /api/file-json",
    "GET /api/knowledge/list",
    "GET /api/knowledge/read",
    "GET /api/knowledge/graph",
    "GET /api/ui-state",
    "POST /api/ui-state",
    "POST /api/open-path",
    "GET /api/capabilities",
    "GET /api/extensions",
    "POST /api/agent-install-request",
    "POST /api/project-folder/choose",
    "POST /api/project-folder",
    "GET /api/subagents",
    "POST /api/subagents"
  ]);
  if (exact.has(`${upperMethod} ${cleanPath}`)) {
    return true;
  }
  if (upperMethod === "POST" && /^\/api\/sessions\/[^/]+\/generate_title$/.test(cleanPath)) {
    return true;
  }
  if ((upperMethod === "PUT" || upperMethod === "DELETE") && /^\/api\/sessions\/[^/]+$/.test(cleanPath)) {
    return true;
  }
  if (upperMethod === "POST" && /^\/api\/sessions\/[^/]+\/clear_context$/.test(cleanPath)) {
    return true;
  }
  if (upperMethod === "POST" && /^\/api\/subagents\/[^/]+\/(?:cancel|collect)$/.test(cleanPath)) {
    return true;
  }
  if (upperMethod === "POST" && /^\/api\/image-jobs\/[^/]+$/.test(cleanPath)) {
    return true;
  }
  if (upperMethod === "GET" && cleanPath.startsWith("/uploads/")) {
    return true;
  }
  return false;
}

function isSidecarConnectivityError(error: unknown) {
  if (!(error instanceof Error)) {
    return false;
  }
  if (error.name === "AbortError" || error.name === "TimeoutError") {
    return true;
  }
  const message = error.message.toLowerCase();
  return (
    message.includes("fetch failed") ||
    message.includes("econnrefused") ||
    message.includes("econnreset") ||
    message.includes("socket hang up") ||
    message.includes("terminated") ||
    message.includes("aborted")
  );
}

export async function fetchSidecarJson(sidecar: SidecarManager, request: ApiRequest) {
  const method = request.method || "GET";
  const path = request.path || "";

  if (!path.startsWith("/") || path.startsWith("//") || !isAllowedPath(path, method)) {
    return {
      status: "error",
      message: "This EcoreX desktop API path is not allowed"
    };
  }

  const ready = await sidecar.waitUntilReady(30000);
  if (!ready) {
    const status = sidecar.getStatus();
    return {
      status: "error",
      message: status.state === "starting"
        ? "EcoreX local runtime is still starting. Please try again in a moment."
        : status.message || "EcoreX local runtime is unavailable.",
      sidecarState: status.state,
      sidecarPhase: status.phase,
      sidecarDiagnostics: status.diagnostics,
      webPort: status.webPort
    };
  }

  let response: Response;
  let text = "";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    response = await fetch(`${sidecar.getBaseUrl()}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-EcoreX-Runtime-Token": sidecar.getRuntimeToken()
      },
      body: method === "GET" ? undefined : JSON.stringify(request.body ?? {}),
      signal: controller.signal
    });
    text = await readResponseTextWithLimit(response);
  } catch (error) {
    const status = isSidecarConnectivityError(error)
      ? sidecar.reportApiFailure(error instanceof Error && error.name === "AbortError" ? "api-bridge-timeout" : "api-bridge-connectivity")
      : sidecar.getStatus();
    return {
      status: "error",
      message: status.state === "starting"
        ? "EcoreX local runtime is still starting. Please try again in a moment."
        : error instanceof Error && error.name === "AbortError"
          ? "EcoreX local runtime request timed out. Please try again."
          : `EcoreX local runtime request failed: ${error instanceof Error ? error.message : String(error)}`,
      sidecarState: status.state,
      sidecarPhase: status.phase,
      sidecarDiagnostics: status.diagnostics,
      webPort: status.webPort
    };
  } finally {
    clearTimeout(timeout);
  }

  if (!text) {
    return {
      status: response.ok ? "success" : "error",
      httpStatus: response.status
    };
  }

  try {
    return JSON.parse(text);
  } catch {
    return {
      status: "error",
      httpStatus: response.status,
      message: sanitizeBridgeSnippet(text, sidecar.getRuntimeToken()).slice(0, 400)
    };
  }
}
