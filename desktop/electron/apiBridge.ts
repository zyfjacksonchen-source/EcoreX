import type { SidecarManager } from "./sidecar.js";

type ApiRequest = {
  path: string;
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
};

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
    "POST /upload",
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
    "GET /api/scheduler",
    "GET /api/version",
    "GET /api/logs",
    "GET /api/logs/snapshot",
    "GET /api/diagnostics/bundle",
    "POST /api/file-stat",
    "GET /api/knowledge/list",
    "GET /api/knowledge/read",
    "GET /api/update-check",
    "POST /api/ui-state",
    "POST /api/open-path",
    "GET /api/capabilities",
    "GET /api/extensions",
    "POST /api/agent-install-request",
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
  if (upperMethod === "GET" && cleanPath.startsWith("/uploads/")) {
    return true;
  }
  return false;
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
      webPort: status.webPort
    };
  }

  let response: Response;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    response = await fetch(`${sidecar.getBaseUrl()}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json"
      },
      body: method === "GET" ? undefined : JSON.stringify(request.body ?? {}),
      signal: controller.signal
    });
  } catch (error) {
    const status = sidecar.getStatus();
    return {
      status: "error",
      message: status.state === "starting"
        ? "EcoreX local runtime is still starting. Please try again in a moment."
        : error instanceof Error && error.name === "AbortError"
          ? "EcoreX local runtime request timed out. Please try again."
          : `EcoreX local runtime request failed: ${error instanceof Error ? error.message : String(error)}`,
      sidecarState: status.state,
      webPort: status.webPort
    };
  } finally {
    clearTimeout(timeout);
  }

  const text = await response.text();
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
      message: text.slice(0, 400)
    };
  }
}
