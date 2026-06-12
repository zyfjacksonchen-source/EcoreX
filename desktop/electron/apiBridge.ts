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
    "GET /api/knowledge/list"
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

  const response = await fetch(`${sidecar.getBaseUrl()}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json"
    },
    body: method === "GET" ? undefined : JSON.stringify(request.body ?? {})
  });

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
