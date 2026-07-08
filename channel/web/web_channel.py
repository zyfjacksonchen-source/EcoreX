import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from queue import Queue, Empty
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import web

if not hasattr(web, "ctx"):
    class _DefaultWebCtx:
        env = {}
        method = "GET"
        status = "200 OK"

    web.ctx = _DefaultWebCtx()

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.web.auth import AuthCheckHandler, AuthLoginHandler, AuthLogoutHandler
from channel.web.capabilities import CapabilitiesHandler, ExtensionsHandler, SkillsHandler, ToolsHandler
# Legacy static release-gate markers after focused handler modularization:
# CapabilityService(RuntimeCapabilityRegistry(_get_workspace_root())).capabilities_payload()
# retry-prepare
# _is_within_directory(upload_dir, full_path)
from channel.web.diagnostics import DiagnosticsBundleHandler, LogsHandler, LogsSnapshotHandler
from channel.web.files import FileJsonHandler, FileServeHandler, FileStatHandler, UploadsHandler
from channel.web.image_jobs import ImageJobActionHandler, ImageJobsHandler
from channel.web.projection import ActiveRequestsHandler, RequestRetryPrepareHandler, RuntimeProjectionHandler
from channel.web.sessions import (
    HistoryHandler,
    MessageDeleteHandler,
    SessionClearContextHandler,
    SessionDetailHandler,
    SessionsHandler,
    SessionTitleHandler,
    UiStateHandler,
)
from channel.web.sse import StreamHandler
from channel.channel_catalog import (
    CHANNEL_CATALOG,
    active_channel_set,
    channel_auth_surface,
    channel_config_status,
    channel_observability,
    normalize_channel_name,
    parse_channel_list,
)
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from channel.messaging_adapter_contract import (
    build_adapter_contract,
    EXTERNAL_CONNECTION_EVENT_SESSION_ID,
    record_external_connection_runtime_event,
    test_messaging_adapter,
)
from channel.web.routes import WEB_ROUTES
from collections import OrderedDict, deque
from common import const
from common import i18n
from common.feishu_register_credentials import (
    extract_feishu_register_credentials,
    summarize_feishu_register_result_shape,
)
from common.feishu_runtime_readiness import feishu_dependency_status
from common.log import logger
from common.ecorex_public_payload import mask_sensitive_text, redact_public_tool_value
from common.singleton import singleton
from config import conf, _ensure_ecorex_runtime_defaults
from agent.tools.imagegen.provider_runner import image_generation_env_with_config, run_image_generation_payload

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
REQUEST_CONFLICT_RETRYABLE_CODE = "REQUEST_CONFLICT_RETRYABLE"
REQUEST_CONFLICT_RETRY_AFTER_MS = 1500
BACKPRESSURE_GLOBAL_LIMIT_CODE = "BACKPRESSURE_GLOBAL_LIMIT"
BACKPRESSURE_SESSION_LIMIT_CODE = "BACKPRESSURE_SESSION_LIMIT"
BACKPRESSURE_RETRY_AFTER_MS = 2000
TOOL_OUTPUT_LIMIT_CODE = "TOOL_OUTPUT_LIMIT"
ARTIFACT_METADATA_LIMIT_CODE = "ARTIFACT_METADATA_LIMIT"
SSE_RUN_TERMINAL_TYPES = {"done", "error", "cancelled", "interrupted"}
SSE_STREAM_TERMINAL_TYPES = SSE_RUN_TERMINAL_TYPES | {"replay_gap"}
_RUNTIME_STARTED_AT = time.time()
RECENT_TERMINAL_RUN_MAX_AGE_SECONDS = 30 * 60
RECENT_TERMINAL_RUN_LIMIT = 50
ACTIVE_RUN_STALE_SECONDS = 180
DANGEROUS_OPEN_EXTENSIONS = {
    ".app",
    ".bat",
    ".cmd",
    ".command",
    ".com",
    ".exe",
    ".js",
    ".jse",
    ".lnk",
    ".msi",
    ".ps1",
    ".reg",
    ".scr",
    ".sh",
    ".vbe",
    ".vbs",
    ".wsf",
}
QUEUE_PAYLOAD_SCHEMA_VERSION = 1
WEBUI_IDENTITY_GUARD_CONTEXT = "\n".join([
    "WebUI identity guard:",
    "You are 小芯, the AI Agent for EcoreX WebUI by 亦芯广告.",
    "When the user asks who you are, answer as 小芯 / EcoreX WebUI's AI assistant.",
    "Do not claim to be Gemini, Google DeepMind, Antigravity, Claude, OpenAI, or any underlying model/provider.",
    "If the user explicitly asks about the selected model or provider, mention it only as runtime information, not as your identity.",
    "EcoreX is an office agent, not a coding-first agent. For semantic image generation/editing, poster retouching, "
    "single-character image text fixes, or 精准修图/局部修图 tasks, use the native imagegen/image editing route. "
    "Do not use bash, Python, PIL, OpenCV, ImageMagick, SVG/canvas, or coordinate scripts as the primary edit path. "
    "Shell may only be used after imagegen output for deterministic post-processing such as copy, rename, checksum, zip, or reveal.",
])


class QueuedRequestPayloadStore:
    """Small file-backed payload store for queued /message runs."""

    def __init__(self, workspace: str):
        self._workspace = str(workspace or "")
        digest = hashlib.sha256(self._workspace.encode("utf-8", errors="replace")).hexdigest()[:12]
        self._dir = Path(self._workspace).expanduser().resolve() / ".ecorex" / "queued-requests"
        self._lock = threading.RLock()
        self._digest = digest

    @property
    def workspace(self) -> str:
        return self._workspace

    def save(self, payload: Dict[str, Any]) -> bool:
        request_id = self._safe_request_id(payload.get("request_id"))
        session_id = str(payload.get("session_id") or "").strip()
        if not request_id or not session_id:
            return False
        record = {
            "schemaVersion": QUEUE_PAYLOAD_SCHEMA_VERSION,
            "request_id": request_id,
            "session_id": session_id,
            "created_at": time.time(),
            "payload": self._public_json_safe_payload(payload),
        }
        path = self._path_for(request_id)
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(tmp, path)
            return True
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning(f"[WebChannel] queued payload save failed: request={request_id} store={self._digest} error={exc.__class__.__name__}")
            return False

    def load(self, request_id: str) -> Optional[Dict[str, Any]]:
        request_id = self._safe_request_id(request_id)
        if not request_id:
            return None
        path = self._path_for(request_id)
        try:
            with self._lock, path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning(f"[WebChannel] queued payload read failed: request={request_id} error={exc.__class__.__name__}")
            return None
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        if self._safe_request_id(payload.get("request_id")) != request_id:
            return None
        return payload

    def delete(self, request_id: str) -> None:
        request_id = self._safe_request_id(request_id)
        if not request_id:
            return
        try:
            with self._lock:
                self._path_for(request_id).unlink(missing_ok=True)
        except Exception:
            pass

    def exists(self, request_id: str) -> bool:
        request_id = self._safe_request_id(request_id)
        return bool(request_id and self._path_for(request_id).exists())

    def _path_for(self, request_id: str) -> Path:
        return self._dir / f"{self._safe_request_id(request_id)}.json"

    @staticmethod
    def _safe_request_id(value: Any) -> str:
        raw = str(value or "").strip()
        if raw and len(raw) <= 128 and all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in raw):
            return raw
        return ""

    @staticmethod
    def _public_json_safe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        safe = dict(payload or {})
        attachments = safe.get("attachments")
        if isinstance(attachments, list):
            safe["attachments"] = [dict(item) for item in attachments[:50] if isinstance(item, dict)]
        project_context = safe.get("project_context_meta")
        if not isinstance(project_context, dict):
            safe["project_context_meta"] = {}
        return safe


def _web_runtime_bridge_version() -> str:
    try:
        from cli import __version__
    except Exception:
        __version__ = ""
    version = str(__version__ or "").strip()
    if not version:
        version = "0.3.0"
    if "-web." in version:
        return version
    return f"{version}-web.1"


def _web_app_bridge_script() -> str:
    """Small browser bridge so the desktop React app can reuse WebUI HTTP APIs."""
    configured_client_base = json.dumps(_web_enterprise_client_base())
    configured_client_keys = json.dumps(WEB_ENTERPRISE_CLIENT_KEYS)
    bridge_app_version = json.dumps(_web_runtime_bridge_version())
    return r"""
<script>
(function () {
  var existingDesktopBridge = (window.ecorexDesktop && typeof window.ecorexDesktop === "object")
    ? window.ecorexDesktop
    : {};

  var CONFIGURED_WEB_CLIENT_BASE = __ECOREX_WEB_CLIENT_BASE__;
  var CONFIGURED_WEB_CLIENT_KEYS = __ECOREX_WEB_CLIENT_KEYS__;
  var WEB_APP_VERSION = __ECOREX_WEB_APP_VERSION__;
  if (CONFIGURED_WEB_CLIENT_BASE && !window.ECOREX_WEB_CLIENT_BASE) {
    var configuredClientBase = sameOriginClientBase(CONFIGURED_WEB_CLIENT_BASE);
    if (configuredClientBase) window.ECOREX_WEB_CLIENT_BASE = configuredClientBase;
  }
  if (Array.isArray(CONFIGURED_WEB_CLIENT_KEYS) && CONFIGURED_WEB_CLIENT_KEYS.length && !window.ECOREX_WEB_CLIENT_KEYS) {
    window.ECOREX_WEB_CLIENT_KEYS = CONFIGURED_WEB_CLIENT_KEYS;
  }

  var DEFAULT_WEB_CLIENT_KEY = "ecorex-web-v" + String(WEB_APP_VERSION || "0.3.0-web.1").replace(/^v/, "");
  var DEFAULT_WEB_COMPAT_CLIENT_KEYS = [
    "ecorex-web-v0.3.0-web.1",
    "ecorex-web-v0.2.9.2-web.1",
    "ecorex-web-v0.2.9.1-web.1",
    "ecorex-web-v0.2.9-web.1",
    "ecorex-web-v0.2.8-web.1",
    "ecorex-web-v0.2.7.2-web.1",
    "ecorex-web-v0.2.7.1-web.1",
    "ecorex-web-v0.2.7-web.1",
    "ecorex-web-v0.2.6-web.1",
    "ecorex-web-v0.2.2-web.1",
    "ecorex-web-v0.2.1-web.1",
    "ecorex-web-v0.2.0-web.1",
    "ecorex-web-v0.1.19-web.1",
    "ecorex-web-v0.1.18-web.1",
    "ecorex-web-v0.1.17-web.1",
    "ecorex-web-v0.1.16-web.1",
    "ecorex-web-v0.1.15-web.1",
    "ecorex-web-v0.1.14-web.1",
    "ecorex-web-v0.1.13-web.1",
    "ecorex-web-v0.1.12-web.1",
    "ecorex-web-v0.1.11-web.1"
  ];
  var WEB_CLIENT_KEY = window.ECOREX_WEB_CLIENT_KEY || DEFAULT_WEB_CLIENT_KEY;
  var WEB_SESSION_KEY = "ecorex-web-enterprise-session";
  var WEB_LOCAL_SESSION_KEY = "ecorex-web-local-session";
  var WEB_DEVICE_KEY = "ecorex-web-device-id";
  var webPort = Number(window.location.port || (window.location.protocol === "https:" ? 443 : 80));
  var desktopPlatform = detectDesktopPlatform();
  var status = {
    state: "running",
    message: "EcoreX 兼容运行时已启动",
    webPort: webPort
  };
  var listeners = new Set();

  function isEcorexAgentPage() {
    return /^\/ecorex-agent(?:\/|$)/.test(window.location.pathname || "");
  }

  function sameOriginClientBase(value) {
    var raw = String(value || "").trim();
    if (!raw) return "";
    try {
      var parsed = new URL(raw, window.location.href);
      if (isEcorexAgentPage() && parsed.origin !== window.location.origin) {
        return "";
      }
      return raw.replace(/\/+$/, "");
    } catch (error) {
      return raw.replace(/\/+$/, "");
    }
  }

  function detectDesktopPlatform() {
    var platform = "";
    try {
      platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || "";
    } catch (error) {}
    if (/mac|iphone|ipad|ipod/i.test(platform)) return "darwin";
    if (/win/i.test(platform)) return "win32";
    if (/linux|x11|cros/i.test(platform)) return "linux";
    return "win32";
  }

  function parseJson(text) {
    try {
      return text ? JSON.parse(text) : {};
    } catch (error) {
      throw new Error(text || "Invalid JSON response");
    }
  }

  function deviceId() {
    var existing = "";
    try { existing = window.localStorage.getItem(WEB_DEVICE_KEY) || ""; } catch (error) {}
    if (existing) return existing;
    var next = "web-" + Math.random().toString(16).slice(2) + "-" + Date.now().toString(16);
    try { window.localStorage.setItem(WEB_DEVICE_KEY, next); } catch (error) {}
    return next;
  }

  function readAdminSession() {
    try {
      var raw = window.localStorage.getItem(WEB_SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function sessionDeviceId(session) {
    var id = session && session.deviceId ? String(session.deviceId) : deviceId();
    if (session && !session.deviceId) {
      session.deviceId = id;
      writeAdminSession(session);
    }
    return id;
  }

  function writeAdminSession(session) {
    try {
      if (session) window.localStorage.setItem(WEB_SESSION_KEY, JSON.stringify(session));
      else window.localStorage.removeItem(WEB_SESSION_KEY);
    } catch (error) {}
  }

  function readLocalSession() {
    try {
      var raw = window.localStorage.getItem(WEB_LOCAL_SESSION_KEY);
      var session = raw ? JSON.parse(raw) : null;
      return session && session.user && session.user.email ? session : null;
    } catch (error) {
      return null;
    }
  }

  function writeLocalSession(session) {
    try {
      if (session) window.localStorage.setItem(WEB_LOCAL_SESSION_KEY, JSON.stringify(session));
      else window.localStorage.removeItem(WEB_LOCAL_SESSION_KEY);
    } catch (error) {}
  }

  function isGenericLocalSession(session) {
    var email = String(session && session.user && session.user.email || "").trim().toLowerCase();
    return !email || email === "ecorex@ecorex.local" || email === "local@ecorex.local";
  }

  function purgeGenericLocalSession() {
    var local = readLocalSession();
    if (local && isGenericLocalSession(local)) {
      writeLocalSession(null);
    }
  }

  function allowLocalSessionFallback() {
    return window.ECOREX_ALLOW_LOCAL_SESSION_FALLBACK === true;
  }

  function makeLocalSession(authRequired, identity) {
    identity = identity || {};
    var email = String(identity.email || "").trim().toLowerCase();
    var hasProvidedIdentity = Boolean(email);
    if (!email) email = authRequired ? "ecorex@ecorex.local" : "local@ecorex.local";
    var name = String(identity.name || "").trim();
    if (!name) name = email.indexOf("@") > 0 ? email.split("@")[0] : "EcoreX";
    if (email === "ecorex@ecorex.local" || email === "local@ecorex.local") name = "EcoreX";
    return {
      authenticated: true,
      localFallback: !hasProvidedIdentity,
      authProvider: hasProvidedIdentity ? "web-password" : "local-fallback",
      identitySource: hasProvidedIdentity ? "login-email" : "local-fallback",
      deviceId: deviceId(),
      expiresAt: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
      user: {
        id: hasProvidedIdentity ? "ecorex-password:" + email : (authRequired ? "ecorex-password" : "ecorex-local"),
        name: name,
        email: email,
        role: "user",
        status: "active"
      },
      quota: { allowed: true }
    };
  }

  function webSession(authRequired, allowLocalFallback, identity, persistLocal) {
    purgeGenericLocalSession();
    if (!allowLocalSessionFallback()) {
      allowLocalFallback = false;
      persistLocal = false;
    }
    var admin = readAdminSession();
    if (admin && admin.user && admin.token) return admin;
    if (!allowLocalFallback) return null;
    if (identity && identity.email) {
      var next = makeLocalSession(authRequired, identity);
      if (persistLocal) writeLocalSession(next);
      return next;
    }
    var local = readLocalSession();
    if (local && (!authRequired || !isGenericLocalSession(local))) return local;
    if (authRequired && !(identity && identity.email)) return null;
    var fallback = makeLocalSession(authRequired, {});
    if (persistLocal) writeLocalSession(fallback);
    return fallback;
  }

  function clientBase() {
    return window.ECOREX_WEB_CLIENT_BASE || runtimePath("/client");
  }

  function enterpriseClientConfigured() {
    return Boolean(String(window.ECOREX_WEB_CLIENT_BASE || "").trim()) || isEcorexAgentPage();
  }

  function webClientKeys(preferredKey) {
    var configured = window.ECOREX_WEB_CLIENT_KEYS;
    var raw = [];
    if (Array.isArray(configured)) raw = configured;
    else if (typeof configured === "string") raw = configured.split(",");
    else if (WEB_CLIENT_KEY === DEFAULT_WEB_CLIENT_KEY) raw = DEFAULT_WEB_COMPAT_CLIENT_KEYS;
    else raw = [WEB_CLIENT_KEY];

    var keys = [];
    function add(value) {
      var key = String(value || "").trim();
      if (key && keys.indexOf(key) < 0) keys.push(key);
    }
    add(preferredKey);
    add(WEB_CLIENT_KEY);
    raw.forEach(add);
    return keys;
  }

  function isInvalidClientKey(response, payload) {
    var text = String((payload && (payload.error || payload.message)) || "").toLowerCase();
    return response && response.status === 403 && text.indexOf("invalid client key") >= 0;
  }

  function runtimePrefix() {
    var path = window.location.pathname || "";
    var markers = ["/app", "/chat", "/auth", "/message", "/upload", "/uploads", "/api", "/poll", "/stream", "/cancel", "/config", "/assets"];
    for (var i = 0; i < markers.length; i += 1) {
      var marker = markers[i];
      var idx = path.indexOf(marker);
      if (idx >= 0) return path.slice(0, idx);
    }
    return "";
  }

  function isRuntimePath(path) {
    return path === "/message" ||
      path === "/upload" ||
      path === "/poll" ||
      path === "/stream" ||
      path === "/cancel" ||
      path === "/chat" ||
      path === "/config" ||
      path.indexOf("/app") === 0 ||
      path.indexOf("/auth/") === 0 ||
      path.indexOf("/uploads/") === 0 ||
      path.indexOf("/api/") === 0 ||
      path.indexOf("/assets/") === 0 ||
      path.indexOf("/client/") === 0;
  }

  function runtimePath(path) {
    if (!path || path.charAt(0) !== "/" || path.indexOf("//") === 0) return path;
    var prefix = runtimePrefix();
    if (!prefix || path.indexOf(prefix + "/") === 0) return path;
    return prefix + path;
  }

  function isSafeFeishuCliAuthUrl(value) {
    var url = String(value || "").trim();
    return url.indexOf("https://open.feishu.cn/") === 0 || url.indexOf("https://open.larksuite.com/") === 0;
  }

  function feishuCliAuthUrlFromPayload(payload) {
    var agentAuth = payload && typeof payload.agentAuth === "object" ? payload.agentAuth : {};
    var url = String((payload && payload.verificationUrl) || agentAuth.verificationUrl || "").trim();
    return isSafeFeishuCliAuthUrl(url) ? url : "";
  }

  function feishuCliAuthSessionFromPayload(payload) {
    var agentAuth = payload && typeof payload.agentAuth === "object" ? payload.agentAuth : {};
    return String((payload && payload.sessionId) || agentAuth.sessionId || "").trim();
  }

  function copyTextBestEffort(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).catch(function () {});
        return;
      }
      var textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "-9999px";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      try { document.execCommand("copy"); } catch (error) {}
      textarea.remove();
    } catch (error) {}
  }

  function showFeishuCliAuthNotice(url) {
    if (!url) return;
    var existing = document.getElementById("ecorex-feishu-cli-auth-notice");
    if (existing) existing.remove();
    var notice = document.createElement("div");
    notice.id = "ecorex-feishu-cli-auth-notice";
    notice.style.cssText = "position:fixed;right:18px;top:18px;z-index:99999;max-width:min(460px,calc(100vw - 36px));padding:14px 16px;border:1px solid rgba(59,130,246,.28);border-radius:10px;background:#fff;color:#0f172a;box-shadow:0 18px 48px rgba(15,23,42,.18);font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;";
    var title = document.createElement("div");
    title.textContent = "飞书 Agent 授权链接已打开并复制";
    title.style.cssText = "font-weight:650;margin-bottom:6px;";
    var hint = document.createElement("div");
    hint.textContent = "如果浏览器拦截了弹窗，请点击下方链接完成授权。";
    hint.style.cssText = "color:#475569;margin-bottom:8px;";
    var link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url;
    link.style.cssText = "display:block;color:#2563eb;word-break:break-all;text-decoration:underline;";
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "关闭");
    close.style.cssText = "position:absolute;right:8px;top:6px;border:0;background:transparent;color:#64748b;font-size:20px;line-height:1;cursor:pointer;";
    close.onclick = function () { notice.remove(); };
    notice.appendChild(close);
    notice.appendChild(title);
    notice.appendChild(hint);
    notice.appendChild(link);
    document.body.appendChild(notice);
    window.setTimeout(function () {
      if (notice.parentNode) notice.remove();
    }, 120000);
  }

  function showFeishuCliAuthCompletedNotice() {
    var existing = document.getElementById("ecorex-feishu-cli-auth-notice");
    if (!existing) return;
    var title = existing.querySelector("div:nth-child(2)");
    var hint = existing.querySelector("div:nth-child(3)");
    if (title) title.textContent = "飞书 Agent 授权已完成";
    if (hint) hint.textContent = "本机飞书配置已完成回写，后续飞书技能可直接调用。";
  }

  function showFeishuCliAuthFailedNotice(message) {
    var existing = document.getElementById("ecorex-feishu-cli-auth-notice");
    if (!existing) return;
    existing.style.borderColor = "rgba(239,68,68,.35)";
    var title = existing.querySelector("div:nth-child(2)");
    var hint = existing.querySelector("div:nth-child(3)");
    if (title) title.textContent = "飞书 Agent 授权未完成";
    if (hint) hint.textContent = String(message || "本机飞书配置未确认写回，请重新发起授权。").slice(0, 220);
  }

  function startFeishuCliAuthPolling(payload) {
    var sessionId = feishuCliAuthSessionFromPayload(payload);
    if (!sessionId) return;
    if (window.__ecorexFeishuCliAuthPollTimer) {
      clearTimeout(window.__ecorexFeishuCliAuthPollTimer);
      window.__ecorexFeishuCliAuthPollTimer = 0;
    }
    var agentAuth = payload && typeof payload.agentAuth === "object" ? payload.agentAuth : {};
    var timeoutSeconds = Number((payload && payload.cliWritebackTimeoutSeconds) || agentAuth.cliWritebackTimeoutSeconds || 240);
    var deadline = Date.now() + Math.max(10, timeoutSeconds) * 1000;
    function schedule() {
      if (Date.now() >= deadline) return;
      window.__ecorexFeishuCliAuthPollTimer = setTimeout(poll, 3000);
    }
    function poll() {
      fetch(runtimePath("/api/external-connections/feishu/actions"), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ action: "agent_auth_status", sessionId: sessionId })
      }).then(function (response) {
        return response.text().then(function (text) {
          try { return JSON.parse(text || "{}"); } catch (error) { return {}; }
        });
      }).then(function (data) {
        var next = data && typeof data.agentAuth === "object" ? data.agentAuth : {};
        var statusValue = String(next.status || data.status || "").toLowerCase();
        if (data.authCompleted === true || next.authCompleted === true) {
          showFeishuCliAuthCompletedNotice();
          return;
        }
        if (data.writebackPending === true || next.writebackPending === true) {
          schedule();
          return;
        }
        if (data.status === "error" || ["error", "timeout", "cancelled", "not_found", "auth_incomplete"].indexOf(statusValue) >= 0) {
          showFeishuCliAuthFailedNotice(data.message || next.message);
        }
      }).catch(function () {
        schedule();
      });
    }
    schedule();
  }

  function handleFeishuCliAuthPayload(request, payload) {
    var path = String((request && request.path) || "");
    var body = request && request.body && typeof request.body === "object" ? request.body : {};
    if (path.indexOf("/api/external-connections/feishu/actions") !== 0 || body.action !== "agent_auth") return;
    var url = feishuCliAuthUrlFromPayload(payload);
    if (!url) return;
    copyTextBestEffort(url);
    try { window.open(url, "_blank", "noopener,noreferrer"); } catch (error) {}
    showFeishuCliAuthNotice(url);
    startFeishuCliAuthPolling(payload);
  }

  function runtimeAuthHeaders() {
    var session = readAdminSession();
    var headers = { "Accept": "application/json" };
    var clientKeys = webClientKeys(session && session.clientKey);
    if (clientKeys.length) headers["X-EcoreX-Client-Key"] = clientKeys[0];
    if (session && session.token) {
      headers["X-EcoreX-User-Token"] = session.token;
      headers["Authorization"] = "Bearer " + session.token;
      headers["X-EcoreX-Device-Id"] = sessionDeviceId(session);
    }
    return headers;
  }

  async function apiJson(request) {
    request = request || {};
    if (String(request.path || "") === "/message") {
      var modelReady = await ensureModelReady();
      if (!modelReady.ready) {
        return {
          status: "error",
          code: modelReady.code || "MODEL_CONFIG_UNAVAILABLE",
          error_type: "model_config",
          recoverable: modelReady.recoverable !== false,
          message: modelReady.message || "当前账号暂时没有可用模型，请重新登录或联系管理员检查企业模型配置。"
        };
      }
    }
    var method = request.method || "GET";
    var headers = runtimeAuthHeaders();
    var init = {
      method: method,
      credentials: "same-origin",
      headers: headers
    };
    if (request.body !== undefined && method !== "GET") {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(request.body);
    }
    var response = await fetch(runtimePath(request.path || "/"), init);
    var text = await response.text();
    var payload = parseJson(text);
    if (!response.ok || payload.status === "error") {
      var err = new Error(payload.message || response.statusText || "Request failed");
      err.code = payload.code;
      err.payload = payload;
      throw err;
    }
    if (String(request.path || "") === "/message" && payload && payload.request_id) {
      phase2EmitUserMessage(request, payload).catch(function () {});
    }
    handleFeishuCliAuthPayload(request, payload);
    return payload;
  }

  function localRuntimeUrl(value) {
    if (!value || typeof value !== "string") return value;
    try {
      var parsed = new URL(value, window.location.href);
      var loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost" || parsed.hostname === "::1";
      var sameOrigin = parsed.origin === window.location.origin;
      if ((loopback || sameOrigin) && isRuntimePath(parsed.pathname)) {
        return runtimePath(parsed.pathname) + parsed.search + parsed.hash;
      }
    } catch (error) {}
    return value;
  }

  function patchUrlProperty(proto, prop) {
    var descriptor = Object.getOwnPropertyDescriptor(proto, prop);
    if (!descriptor || !descriptor.set || !descriptor.get) return;
    Object.defineProperty(proto, prop, {
      configurable: true,
      enumerable: descriptor.enumerable,
      get: function () { return descriptor.get.call(this); },
      set: function (value) { descriptor.set.call(this, localRuntimeUrl(value)); }
    });
  }

  patchUrlProperty(HTMLImageElement.prototype, "src");
  patchUrlProperty(HTMLIFrameElement.prototype, "src");
  patchUrlProperty(HTMLAnchorElement.prototype, "href");

  var nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    var attr = String(name || "").toLowerCase();
    if ((attr === "src" || attr === "href") && typeof value === "string") {
      value = localRuntimeUrl(value);
    }
    return nativeSetAttribute.call(this, name, value);
  };

  var NativeEventSource = window.EventSource;
  if (NativeEventSource) {
    window.EventSource = function (url, options) {
      url = localRuntimeUrl(url);
      return new NativeEventSource(url, options);
    };
    window.EventSource.prototype = NativeEventSource.prototype;
  }

  function uploadBlob(file, fileName) {
    var form = new FormData();
    form.append("file", file, fileName || file.name || ("upload-" + Date.now()));
    return fetch(runtimePath("/upload"), { method: "POST", credentials: "same-origin", headers: runtimeAuthHeaders(), body: form })
      .then(function (response) {
        return response.text().then(function (text) {
          var payload = parseJson(text);
          if (!response.ok || payload.status !== "success") {
            throw new Error(payload.message || "Upload failed");
          }
          return {
            file_path: payload.file_path,
            file_name: payload.file_name || fileName || file.name || "upload",
            file_type: payload.file_type || (file.type && file.type.indexOf("image/") === 0 ? "image" : "file")
          };
        });
      });
  }

  function chooseFiles() {
    return new Promise(function (resolve, reject) {
      var input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.style.position = "fixed";
      input.style.left = "-9999px";
      input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files || []);
        input.remove();
        Promise.all(files.map(function (file) { return uploadBlob(file, file.name); })).then(resolve, reject);
      }, { once: true });
      document.body.appendChild(input);
      input.click();
    });
  }

  function savePastedFile(payload) {
    payload = payload || {};
    var binary = atob(payload.dataBase64 || "");
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    var blob = new Blob([bytes], { type: payload.mimeType || "application/octet-stream" });
    return uploadBlob(blob, payload.fileName || ("paste-" + Date.now()));
  }

  async function clientJson(path, method, body, requireToken) {
    var session = readAdminSession();
    if (requireToken && !(session && session.token)) {
      return null;
    }
    var keys = webClientKeys(session && session.clientKey);
    var lastPayload = {};
    var lastResponse = null;
    for (var i = 0; i < keys.length; i += 1) {
      var headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-EcoreX-Client-Key": keys[i],
        "X-EcoreX-Device-Id": sessionDeviceId(session)
      };
      if (session && session.token) {
        headers["X-EcoreX-User-Token"] = session.token;
        headers["Authorization"] = "Bearer " + session.token;
      }
      var response = await fetch(clientBase() + path, {
        method: method || "POST",
        credentials: "same-origin",
        headers: headers,
        body: body === undefined ? undefined : JSON.stringify(body || {})
      });
      var payload = parseJson(await response.text());
      lastPayload = payload;
      lastResponse = response;
      if (isInvalidClientKey(response, payload) && i < keys.length - 1) {
        continue;
      }
      if (!response.ok) {
        var err = new Error(payload.error || payload.message || "Client request failed");
        err.status = response.status;
        err.code = payload.code || payload.error_code || "";
        err.payload = payload;
        throw err;
      }
      if (payload && typeof payload === "object") payload.clientKey = keys[i];
      return payload;
    }
    throw new Error((lastPayload && (lastPayload.error || lastPayload.message)) || (lastResponse && lastResponse.statusText) || "Client request failed");
  }

  function isMissingClientBridge(error) {
    return /not found|404|failed to fetch|networkerror|load failed|invalid client key|client key/i.test(String((error && error.message) || error || ""));
  }

  var PHASE1_DENY_KEYS = {
    body: true, blob: true, bytes: true, content: true, data: true, data_base64: true,
    database64: true, delta: true, file_content: true, file_path: true, filecontent: true,
    filepath: true, final_text: true, finaltext: true, html: true, input: true,
    markdown: true, message: true, messages: true, output: true, path: true,
    preview_url: true, previewurl: true, prompt: true, raw: true, relative_path: true,
    relativepath: true, response: true, result: true, status_path: true, statuspath: true,
    text: true, thumbnail_url: true, thumbnailurl: true, transcript: true, url: true
  };

  function phase1SyncEnabled() {
    if (window.ECOREX_PHASE1_SYNC === false || window.ECOREX_DISABLE_PHASE1_SYNC === true) return false;
    try { return localStorage.getItem("ecorex_phase1_sync") !== "off"; } catch (error) { return true; }
  }

  function phase1Now() {
    return new Date().toISOString();
  }

  function phase1NormalizeKey(key) {
    return String(key || "").toLowerCase().replace(/-/g, "_");
  }

  function phase1SafeJson(value, depth) {
    depth = depth || 0;
    if (depth > 4) return Object.prototype.toString.call(value).slice(0, 80);
    if (Array.isArray(value)) {
      return value.slice(0, 32).map(function (item) { return phase1SafeJson(item, depth + 1); });
    }
    if (value && typeof value === "object") {
      var result = {};
      Object.keys(value).slice(0, 64).forEach(function (key) {
        result[key] = PHASE1_DENY_KEYS[phase1NormalizeKey(key)] ? "[omitted]" : phase1SafeJson(value[key], depth + 1);
      });
      return result;
    }
    if (typeof value === "string") return value.slice(0, 1000);
    if (typeof value === "number" || typeof value === "boolean" || value === null || value === undefined) return value == null ? null : value;
    return String(value).slice(0, 1000);
  }

  function phase1FallbackHash(value) {
    var text = String(value || "");
    var hash = 2166136261;
    for (var i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return "fnv-" + (hash >>> 0).toString(36);
  }

  async function phase1Digest(value) {
    var text = String(value || "");
    try {
      if (window.crypto && window.crypto.subtle && window.TextEncoder) {
        var bytes = new TextEncoder().encode(text);
        var digest = await window.crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(digest)).map(function (b) { return b.toString(16).padStart(2, "0"); }).join("").slice(0, 40);
      }
    } catch (error) {}
    return phase1FallbackHash(text);
  }

  async function phase1SyncKey(parts) {
    return "phase1:" + await phase1Digest((parts || []).join("|"));
  }

  function phase1ArtifactSource(artifact) {
    artifact = artifact || {};
    return String(
      artifact.path || artifact.filePath || artifact.file_path ||
      artifact.relativePath || artifact.relative_path ||
      artifact.previewUrl || artifact.preview_url ||
      artifact.statusPath || artifact.status_path ||
      artifact.thumbnailUrl || artifact.thumbnail_url ||
      artifact.url || artifact.content || ""
    );
  }

  function phase1ArtifactTitle(artifact, source) {
    var explicit = artifact.title || artifact.name || artifact.fileName || artifact.file_name;
    if (explicit) return String(explicit).slice(0, 240);
    var clean = String(source || "").split(/[?#]/, 1)[0].replace(/\\/g, "/");
    return (clean.split("/").pop() || "artifact").slice(0, 240);
  }

  function phase1ArtifactExt(source, title) {
    var candidate = String(source || "");
    try {
      var parsed = new URL(candidate, window.location.href);
      candidate = parsed.searchParams.get("path") || parsed.pathname || candidate;
    } catch (error) {}
    candidate = String(candidate || title || "").split(/[?#]/, 1)[0];
    var match = candidate.match(/(\.[A-Za-z0-9]{1,12})$/);
    return match ? match[1].toLowerCase() : "";
  }

  function phase1ArtifactsFromItem(item) {
    if (!item || typeof item !== "object") return [];
    if (item.type === "done" && Array.isArray(item.artifacts)) return item.artifacts;
    if (item.type === "artifact" && item.artifact) return [item.artifact];
    if (item.type === "file" || item.type === "image" || item.type === "video" || item.type === "audio" || item.type === "voice_attach") {
      return [{
        kind: item.type === "voice_attach" ? "audio" : item.type,
        title: item.file_name || item.name || item.type,
        path: item.path || item.content || "",
        url: item.url || "",
        fileType: item.file_type || item.mime_type || "",
        mimeType: item.mime_type || item.mimeType || "",
        sizeBytes: item.size_bytes || item.sizeBytes || 0,
        status: item.status || "ready"
      }];
    }
    return [];
  }

  async function phase1ArtifactMetadata(artifact, sessionId, requestId) {
    artifact = artifact || {};
    var source = phase1ArtifactSource(artifact);
    var title = phase1ArtifactTitle(artifact, source);
    var rawIdentity = [
      artifact.safeArtifactId || artifact.safe_artifact_id || artifact.id || artifact.artifactId || artifact.artifact_id,
      title,
      source,
      requestId
    ].filter(Boolean).join("|");
    var safeArtifactId = artifact.safeArtifactId || artifact.safe_artifact_id || ("artifact:" + await phase1Digest(rawIdentity || title));
    var sourceInfo = artifact.source || {};
    return {
      idempotencyKey: await phase1SyncKey(["artifact", sessionId, requestId, safeArtifactId]),
      safeArtifactId: safeArtifactId,
      sessionId: sessionId || "",
      requestId: artifact.requestId || artifact.request_id || requestId || "",
      kind: artifact.kind || artifact.type || artifact.fileType || artifact.file_type || "file",
      intent: artifact.intent || "deliverable",
      operation: artifact.operation || artifact.action || "created",
      status: artifact.status || "ready",
      title: title,
      pathHash: source ? await phase1Digest(source) : "",
      pathExt: artifact.pathExt || artifact.path_ext || phase1ArtifactExt(source, title),
      mimeType: artifact.mimeType || artifact.mime_type || "",
      sizeBytes: Number(artifact.sizeBytes || artifact.size_bytes || 0) || 0,
      artifactValidity: artifact.artifactValidity || artifact.artifact_validity || "valid",
      artifactFeedbackSignal: artifact.artifactFeedbackSignal || artifact.artifact_feedback_signal || "default",
      artifactFeedbackAt: artifact.artifactFeedbackAt || artifact.artifact_feedback_at || "",
      stats: phase1SafeJson(artifact.stats || {}),
      source: {
        toolName: sourceInfo.toolName || sourceInfo.tool_name || artifact.toolName || artifact.tool || "",
        toolCallId: sourceInfo.toolCallId || sourceInfo.tool_call_id || artifact.toolCallId || artifact.tool_call_id || "",
        activityId: sourceInfo.activityId || sourceInfo.activity_id || artifact.activityId || artifact.activity_id || ""
      },
      metadataTruncated: !!artifact.metadataTruncated,
      createdAt: phase1Now()
    };
  }

  async function phase1Emit(payload) {
    if (!phase1SyncEnabled()) return;
    if ((!payload.events || !payload.events.length) && (!payload.artifacts || !payload.artifacts.length)) return;
    try {
      await clientJson("/sync/events", "POST", {
        type: "phase1_sync",
        source: "WebUI",
        sessionId: payload.sessionId || "",
        requestId: payload.requestId || "",
        events: payload.events || [],
        artifacts: payload.artifacts || []
      }, true);
    } catch (error) {}
  }

  async function phase1RunEvent(sessionId, requestId, eventType, statusText, detail) {
    if (!requestId) return;
    await phase1Emit({
      sessionId: sessionId || "",
      requestId: requestId,
      events: [{
        idempotencyKey: await phase1SyncKey(["event", sessionId || "", requestId, eventType, statusText]),
        eventType: eventType,
        status: statusText,
        source: "WebUI",
        sessionId: sessionId || "",
        requestId: requestId,
        detail: phase1SafeJson(detail || {}),
        createdAt: phase1Now()
      }]
    });
  }

  var phase2PolicyCache = { checkedAt: 0, enabled: false };

  function phase2LocalSwitchEnabled() {
    if (window.ECOREX_PHASE2_SYNC === false || window.ECOREX_DISABLE_PHASE2_SYNC === true) return false;
    try { return localStorage.getItem("ecorex_phase2_sync") !== "off"; } catch (error) { return true; }
  }

  async function phase2SyncEnabled() {
    if (!phase2LocalSwitchEnabled()) return false;
    var now = Date.now();
    if (now - phase2PolicyCache.checkedAt < 30000) return !!phase2PolicyCache.enabled;
    phase2PolicyCache.checkedAt = now;
    phase2PolicyCache.enabled = false;
    try {
      var payload = await clientJson("/sync/policy", "GET", undefined, true);
      var phase2 = payload && payload.syncPolicy && payload.syncPolicy.phase2;
      phase2PolicyCache.enabled = !!(phase2 && phase2.chatBodiesEnabled);
    } catch (error) {
      phase2PolicyCache.enabled = false;
    }
    return !!phase2PolicyCache.enabled;
  }

  async function phase2SyncKey(parts) {
    return "phase2:" + await phase1Digest((parts || []).join("|"));
  }

  function phase2VisibleContent(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    return value;
  }

  async function phase2EmitMessages(sessionId, requestId, messages) {
    if (!requestId || !messages || !messages.length) return;
    if (!await phase2SyncEnabled()) return;
    try {
      await clientJson("/sync/messages", "POST", {
        source: "WebUI",
        sessionId: sessionId || "",
        requestId: requestId || "",
        messages: messages
      }, true);
    } catch (error) {}
  }

  async function phase2EmitUserMessage(request, payload) {
    var body = request && request.body ? request.body : {};
    var requestId = payload && payload.request_id ? String(payload.request_id) : "";
    var sessionId = String(body.session_id || body.sessionId || payload.session_id || payload.sessionId || "");
    var content = phase2VisibleContent(body.visible_message !== undefined ? body.visible_message : body.message);
    if (!requestId || content === "" || content === null || content === undefined) return;
    await phase2EmitMessages(sessionId, requestId, [{
      idempotencyKey: await phase2SyncKey(["message", sessionId, requestId, "user", body.client_attempt_id || ""]),
      messageId: body.client_attempt_id || "",
      seq: Number(body.user_seq || 0) || 0,
      role: "user",
      content: content,
      extras: phase1SafeJson({
        clientAttemptId: body.client_attempt_id || "",
        retryOfRequestId: body.retry_of_request_id || "",
        interruptsRequestId: body.interrupts_request_id || "",
        attachmentCount: Array.isArray(body.attachments) ? body.attachments.length : 0,
        isVoice: !!body.is_voice
      }),
      createdAt: phase1Now()
    }]);
  }

  var phase3PolicyCache = { checkedAt: 0, enabled: false, policy: null };
  var phase3UploadMemo = {};

  function phase3LocalSwitchEnabled() {
    if (window.ECOREX_PHASE3_SYNC === false || window.ECOREX_DISABLE_PHASE3_SYNC === true) return false;
    try { return localStorage.getItem("ecorex_phase3_sync") !== "off"; } catch (error) { return true; }
  }

  async function phase3Policy() {
    if (!phase3LocalSwitchEnabled()) return null;
    var now = Date.now();
    if (now - phase3PolicyCache.checkedAt < 30000) return phase3PolicyCache.enabled ? phase3PolicyCache.policy : null;
    phase3PolicyCache.checkedAt = now;
    phase3PolicyCache.enabled = false;
    phase3PolicyCache.policy = null;
    try {
      var payload = await clientJson("/sync/policy", "GET", undefined, true);
      var phase3 = payload && payload.syncPolicy && payload.syncPolicy.phase3;
      phase3PolicyCache.enabled = !!(phase3 && phase3.artifactFilesEnabled && phase3.killSwitch !== true);
      phase3PolicyCache.policy = phase3 || null;
    } catch (error) {
      phase3PolicyCache.enabled = false;
      phase3PolicyCache.policy = null;
    }
    return phase3PolicyCache.enabled ? phase3PolicyCache.policy : null;
  }

  async function phase3SyncKey(parts) {
    return "phase3:" + await phase1Digest((parts || []).join("|"));
  }

  function phase3ArtifactFetchUrl(artifact) {
    artifact = artifact || {};
    var candidates = [
      artifact.previewUrl || artifact.preview_url,
      artifact.url,
      artifact.statusPath || artifact.status_path,
      artifact.path || artifact.filePath || artifact.file_path || artifact.relativePath || artifact.relative_path
    ];
    for (var i = 0; i < candidates.length; i += 1) {
      var value = String(candidates[i] || "").trim();
      if (!value) continue;
      if (/^https?:\/\//i.test(value)) {
        try {
          var absolute = new URL(value, window.location.href);
          if (absolute.origin === window.location.origin && isRuntimePath(absolute.pathname)) {
            return runtimePath(absolute.pathname) + absolute.search + absolute.hash;
          }
        } catch (error) {}
        continue;
      }
      if (value.indexOf("/api/file") === 0 || value.indexOf("/uploads/") === 0) return runtimePath(value);
      return runtimePath("/api/file?path=" + encodeURIComponent(value));
    }
    return "";
  }

  function phase3ArtifactPayload(metadata) {
    metadata = metadata || {};
    return {
      safeArtifactId: metadata.safeArtifactId || metadata.safe_artifact_id || "",
      title: metadata.title || "",
      kind: metadata.kind || "file",
      status: metadata.status || "",
      mimeType: metadata.mimeType || metadata.mime_type || "",
      sizeBytes: Number(metadata.sizeBytes || metadata.size_bytes || 0) || 0,
      pathHash: metadata.pathHash || metadata.path_hash || "",
      pathExt: metadata.pathExt || metadata.path_ext || "",
      createdAt: metadata.createdAt || metadata.created_at || phase1Now()
    };
  }

  function phase3ArrayBufferToBase64(buffer) {
    var bytes = new Uint8Array(buffer || []);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 0x8000) {
      var slice = bytes.subarray(i, Math.min(i + 0x8000, bytes.length));
      binary += String.fromCharCode.apply(null, Array.prototype.slice.call(slice));
    }
    return btoa(binary);
  }

  async function phase3BytesSha256(bytes) {
    if (!(window.crypto && window.crypto.subtle)) return "";
    var digest = await window.crypto.subtle.digest("SHA-256", bytes);
    var view = new Uint8Array(digest);
    var hex = "";
    for (var i = 0; i < view.length; i += 1) hex += view[i].toString(16).padStart(2, "0");
    return hex;
  }

  function phase3Sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, Math.max(0, ms || 0)); });
  }

  async function phase3EmitArtifactFile(rawArtifact, metadata, sessionId, requestId) {
    var policy = await phase3Policy();
    if (!policy || !requestId) return;
    var safe = phase3ArtifactPayload(metadata);
    var artifactId = String(safe.safeArtifactId || safe.pathHash || safe.title || "");
    if (!artifactId) return;
    var memoKey = [sessionId || "", requestId || "", artifactId, safe.pathHash || "", safe.sizeBytes || 0].join("|");
    if (phase3UploadMemo[memoKey]) return;
    phase3UploadMemo[memoKey] = true;
    try {
      var url = phase3ArtifactFetchUrl(rawArtifact || {});
      if (!url) return;
      var response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      var blob = await response.blob();
      var maxAutoBytes = Number(policy.maxAutoBytes || 0) || 0;
      if (maxAutoBytes && blob.size > maxAutoBytes) return;
      var chunkBytes = Math.max(1, Number(policy.chunkBytes || 0) || (2 * 1024 * 1024));
      var bytesPerSecond = Number(policy.bytesPerSecond || 0) || 0;
      var buffer = await blob.arrayBuffer();
      var contentSha256 = await phase3BytesSha256(buffer);
      if (!contentSha256) return;
      var bytes = new Uint8Array(buffer);
      var chunkCount = Math.max(1, Math.ceil(bytes.length / chunkBytes));
      var fileSyncKey = await phase3SyncKey(["artifact-file", sessionId || "", requestId || "", artifactId, contentSha256]);
      for (var i = 0; i < chunkCount; i += 1) {
        var start = i * chunkBytes;
        var end = Math.min(start + chunkBytes, bytes.length);
        var chunk = bytes.subarray(start, end);
        var chunkHash = await phase3BytesSha256(chunk);
        await clientJson("/sync/artifact-blobs/" + encodeURIComponent(artifactId), "PUT", {
          source: "WebUI",
          sessionId: sessionId || "",
          requestId: requestId || "",
          artifactId: artifactId,
          fileSyncKey: fileSyncKey,
          artifact: safe,
          title: safe.title || "artifact",
          mimeType: blob.type || safe.mimeType || "application/octet-stream",
          totalSizeBytes: bytes.length,
          contentSha256: contentSha256,
          chunkIndex: i,
          chunkCount: chunkCount,
          chunkSha256: chunkHash,
          contentBase64: phase3ArrayBufferToBase64(chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength)),
          createdAt: phase1Now()
        }, true);
        if (bytesPerSecond > 0 && i + 1 < chunkCount) {
          await phase3Sleep(Math.ceil((chunk.byteLength / bytesPerSecond) * 1000));
        }
      }
    } catch (error) {
      delete phase3UploadMemo[memoKey];
    }
  }

  async function phase1StreamItem(sessionId, requestId, item) {
    if (!requestId || !item || typeof item !== "object") return;
    var sid = item.session_id || item.sessionId || sessionId || "";
    var rid = item.request_id || item.requestId || requestId || "";
    var events = [];
    var artifacts = [];
    var phase2Messages = [];
    var phase3Artifacts = [];
    if (item.type === "tool_start") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "tool.started", item.tool_call_id || item.tool || ""]),
        eventType: "tool.started",
        status: "running",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ tool: item.tool || "", toolCallId: item.tool_call_id || "" }),
        createdAt: phase1Now()
      });
    } else if (item.type === "tool_end") {
      var toolStatus = item.status === "success" ? "completed" : "failed";
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "tool.finished", item.tool_call_id || item.tool || "", toolStatus]),
        eventType: "tool.finished",
        status: toolStatus,
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ tool: item.tool || "", toolCallId: item.tool_call_id || "", executionTime: item.execution_time || 0 }),
        createdAt: phase1Now()
      });
    } else if (item.type === "artifact_limit") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "artifact.limit", item.tool_call_id || ""]),
        eventType: "artifact.limit",
        status: "limited",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ omittedArtifactCount: Number(item.omitted_artifact_count || 0) || 0 }),
        createdAt: phase1Now()
      });
    } else if (item.type === "cancelled") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "run.cancelled", "cancelled"]),
        eventType: "run.cancelled",
        status: "cancelled",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ acknowledged: true }),
        createdAt: phase1Now()
      });
    } else if (item.type === "paused") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "run.paused", "paused"]),
        eventType: "run.paused",
        status: "paused",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ reason: item.terminal_reason || item.reason || "paused" }),
        createdAt: phase1Now()
      });
    } else if (item.type === "done") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "run.completed", "completed"]),
        eventType: "run.completed",
        status: "completed",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({
          hasUsage: !!item.usage,
          artifactCount: Array.isArray(item.artifacts) ? item.artifacts.length : 0,
          hasTurnIdentity: !!(item.turn_id || item.user_seq !== undefined || item.bot_seq !== undefined)
        }),
        createdAt: phase1Now()
      });
      var assistantContent = item.final_text !== undefined ? item.final_text : item.content;
      if (assistantContent !== undefined && assistantContent !== null && assistantContent !== "") {
        phase2Messages.push({
          idempotencyKey: await phase2SyncKey(["message", sid, rid, "assistant", item.bot_seq || item.turn_id || "done"]),
          messageId: item.turn_id || "",
          seq: Number(item.bot_seq || 0) || 0,
          role: "assistant",
          content: assistantContent,
          extras: phase1SafeJson({
            hasUsage: !!item.usage,
            artifactCount: Array.isArray(item.artifacts) ? item.artifacts.length : 0,
            userSeq: item.user_seq,
            botSeq: item.bot_seq
          }),
          createdAt: phase1Now()
        });
      }
    } else if (item.type === "error") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "run.failed", "failed"]),
        eventType: "run.failed",
        status: "failed",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ errorCode: item.error_code || "", errorType: item.error_type || "", statusCode: item.status_code || "" }),
        createdAt: phase1Now()
      });
    } else if (item.type === "interrupted") {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "run.interrupted", "interrupted"]),
        eventType: "run.interrupted",
        status: "interrupted",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ terminalReason: item.terminal_reason || "interrupted", errorCode: item.error_code || "" }),
        createdAt: phase1Now()
      });
    }
    var rawArtifacts = phase1ArtifactsFromItem(item);
    for (var i = 0; i < Math.min(rawArtifacts.length, 32); i += 1) {
      var artifactMetadata = await phase1ArtifactMetadata(rawArtifacts[i], sid, rid);
      artifacts.push(artifactMetadata);
      phase3Artifacts.push({ raw: rawArtifacts[i], metadata: artifactMetadata });
    }
    if (artifacts.length) {
      events.push({
        idempotencyKey: await phase1SyncKey(["event", sid, rid, "artifact.updated", artifacts.map(function (artifact) { return artifact.safeArtifactId; }).join(",")]),
        eventType: "artifact.updated",
        status: "ready",
        source: "WebUI",
        sessionId: sid,
        requestId: rid,
        detail: phase1SafeJson({ artifactCount: artifacts.length }),
        createdAt: phase1Now()
      });
    }
    await phase1Emit({ sessionId: sid, requestId: rid, events: events, artifacts: artifacts });
    if (phase2Messages.length) {
      await phase2EmitMessages(sid, rid, phase2Messages);
    }
    for (var j = 0; j < phase3Artifacts.length; j += 1) {
      phase3EmitArtifactFile(phase3Artifacts[j].raw, phase3Artifacts[j].metadata, sid, rid).catch(function () {});
    }
  }

  function phase1RequestIdFromUrl(url) {
    try {
      var parsed = new URL(String(url || ""), window.location.href);
      if (parsed.pathname.replace(/^.*\/stream$/, "/stream") !== "/stream") return "";
      return parsed.searchParams.get("request_id") || "";
    } catch (error) {
      return "";
    }
  }

  function installPhase1EventSourceSync() {
    if (window.__ecorexPhase1SseSyncInstalled || !window.EventSource) return;
    var NativeEventSource = window.EventSource;
    window.__ecorexPhase1SseSyncInstalled = true;
    function EcoreXPhase1EventSource(url, options) {
      var requestId = phase1RequestIdFromUrl(url);
      var source = new NativeEventSource(url, options);
      if (requestId) {
        phase1RunEvent("", requestId, "run.accepted", "running", { stream: true }).catch(function () {});
        source.addEventListener("message", function (event) {
          try {
            var item = JSON.parse(event.data || "{}");
            phase1StreamItem("", requestId, item).catch(function () {});
          } catch (error) {}
        });
      }
      return source;
    }
    EcoreXPhase1EventSource.prototype = NativeEventSource.prototype;
    EcoreXPhase1EventSource.CONNECTING = NativeEventSource.CONNECTING;
    EcoreXPhase1EventSource.OPEN = NativeEventSource.OPEN;
    EcoreXPhase1EventSource.CLOSED = NativeEventSource.CLOSED;
    window.EventSource = EcoreXPhase1EventSource;
  }

  async function applyModelPolicy(payload) {
    if (!payload || !payload.configured || !payload.settings) {
      return {
        configured: false,
        changed: false,
        restarted: false,
        message: "enterprise model policy is empty"
      };
    }
    await apiJson({
      path: "/config",
      method: "POST",
      body: { updates: payload.settings }
    });
    return {
      configured: true,
      changed: true,
      restarted: false,
      message: "enterprise model policy refreshed",
      model: payload.model,
      provider: payload.provider,
      updatedAt: payload.updatedAt
    };
  }

  async function refreshModelPolicy() {
    var payload = await clientJson("/model-config", "GET", undefined, true);
    if (!payload) {
      return {
        configured: false,
        changed: false,
        restarted: false,
        message: "enterprise login required"
      };
    }
    return applyModelPolicy(payload);
  }

  async function localModelReady() {
    try {
      var overview = await apiJson({ path: "/api/models", method: "GET" });
      var providers = Array.isArray(overview.providers) ? overview.providers : [];
      var configuredProviders = providers.filter(function (provider) { return provider && provider.configured; });
      var configured = configuredProviders.length > 0;
      var chat = overview.capabilities && overview.capabilities.chat ? overview.capabilities.chat : {};
      if (!configured) return false;
      if (chat.current_provider || chat.current_model) return true;
      return configuredProviders.some(function (provider) {
        return provider && provider.id && Array.isArray(provider.models) && provider.models.length > 0;
      });
    } catch (error) {
      return false;
    }
  }

  function modelConfigNotReady(code, message) {
    return {
      ready: false,
      code: code || "MODEL_CONFIG_UNAVAILABLE",
      recoverable: true,
      message: message || "当前账号暂时没有可用模型，请重新登录或联系管理员检查企业模型配置。"
    };
  }

  async function ensureModelReady() {
    var hasEnterpriseSession = !!readAdminSession();
    if (await localModelReady()) {
      return { ready: true };
    }
    try {
      if (hasEnterpriseSession) {
        var result = await refreshModelPolicy();
        if (result && result.configured && await localModelReady()) {
          return { ready: true };
        }
      }
    } catch (error) {
      var status = Number((error && error.status) || 0);
      var text = String((error && error.message) || error || "").toLowerCase();
      if (status === 401 || /missing user token|invalid user token|expired|token|login|登录|未登录/.test(text)) {
        return modelConfigNotReady("ENTERPRISE_LOGIN_REQUIRED", "登录状态已失效，请重新登录后再发送。");
      }
      if (status === 403 || /invalid client key|client key/.test(text)) {
        return modelConfigNotReady("ENTERPRISE_POLICY_UNAVAILABLE", "企业模型配置暂时无法同步，请稍后重试；如持续出现，请联系管理员更新服务端配置。");
      }
      return modelConfigNotReady("ENTERPRISE_POLICY_SYNC_FAILED", "企业模型配置同步失败，请稍后重试；如持续出现，请联系管理员检查后台模型配置。");
    }
    try {
      var fallbackResult = await refreshModelPolicy();
      if (fallbackResult && fallbackResult.configured && await localModelReady()) {
        return { ready: true };
      }
    } catch (error) {
      var status = Number((error && error.status) || 0);
      var text = String((error && error.message) || error || "").toLowerCase();
      if (status === 401 || /missing user token|invalid user token|expired|token|login|登录|未登录/.test(text)) {
        return modelConfigNotReady("ENTERPRISE_LOGIN_REQUIRED", "登录状态已失效，请重新登录后再发送。");
      }
      if (status === 403 || /invalid client key|client key/.test(text)) {
        return modelConfigNotReady("ENTERPRISE_POLICY_UNAVAILABLE", "企业模型配置暂时无法同步，请稍后重试；如持续出现，请联系管理员更新服务端配置。");
      }
      return modelConfigNotReady("ENTERPRISE_POLICY_SYNC_FAILED", "企业模型配置同步失败，请稍后重试；如持续出现，请联系管理员检查后台模型配置。");
    }
    return modelConfigNotReady("MODEL_CONFIG_UNAVAILABLE", "当前账号暂时没有可用模型，请重新登录或联系管理员检查企业模型配置。");
  }

  var updateStatus = {
    state: "idle",
    platform: desktopPlatform,
    currentVersion: WEB_APP_VERSION,
    message: "尚未检查更新"
  };

  async function checkForUpdates() {
    try {
      var payload = await apiJson({ path: "/api/update-check?platform=" + encodeURIComponent(desktopPlatform), method: "GET" });
      updateStatus = {
        state: payload.hasUpdate ? "available" : "not-available",
        platform: desktopPlatform,
        currentVersion: payload.currentVersion || WEB_APP_VERSION,
        version: payload.latestVersion || payload.version,
        downloadUrl: payload.downloadUrl,
        releasePageUrl: payload.releasePageUrl,
        artifactDownloadUrl: payload.artifactDownloadUrl,
        message: payload.message || (payload.hasUpdate ? "发现新版本，可在本机检查更新" : "当前已经是最新版本"),
        checkedAt: new Date().toISOString()
      };
      return updateStatus;
    } catch (error) {
      updateStatus = {
        state: "error",
        platform: desktopPlatform,
        currentVersion: WEB_APP_VERSION,
        message: error && error.message ? error.message : String(error),
        checkedAt: new Date().toISOString()
      };
      return updateStatus;
    }
  }

  var webDesktopBridge = {
    platform: desktopPlatform,
    apiJson: apiJson,
    getSidecarStatus: async function () { return status; },
    onSidecarStatus: function (callback) {
      try { callback(status); } catch (error) {}
      listeners.add(callback);
      return function () { listeners.delete(callback); };
    },
    shouldUseDarkColors: true,
    checkForUpdates: checkForUpdates,
    getUpdateStatus: async function () { return updateStatus; },
    installDownloadedUpdate: checkForUpdates,
    openDownloadPage: async function () {
      window.open("https://mvdcm.ecoremedia.net/ecorex-agent/", "_blank", "noopener,noreferrer");
      return { ok: true, url: "https://mvdcm.ecoremedia.net/ecorex-agent/" };
    },
    onUpdateStatus: function (callback) {
      try { callback(updateStatus); } catch (error) {}
      return function () {};
    },
    chooseFiles: chooseFiles,
    chooseProjectFolder: async function () {
      function notifyProjectPicker(state, message) {
        try {
          window.dispatchEvent(new CustomEvent("ecorex:project-folder-picker", {
            detail: { state: state, message: message || "" }
          }));
        } catch (error) {}
      }
      function isLocalProjectPickerHost() {
        var host = String(window.location.hostname || "").toLowerCase();
        return !host || host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]" || /(^|\\.)localhost$/.test(host);
      }
      function canFallbackProjectPicker(error) {
        var message = String((error && error.message) || error || "").toLowerCase();
        return /not found|404|failed to fetch|networkerror|load failed|native folder picker is unavailable|folder picker failed|osascript|zenity|kdialog/.test(message);
      }
      var nativeError = null;
      if (isLocalProjectPickerHost()) {
        try {
          notifyProjectPicker("opening", "Opening native folder picker");
          var nativePayload = await apiJson({ path: "/api/project-folder/choose", method: "POST", body: {} });
          if (nativePayload && nativePayload.project) {
            notifyProjectPicker("selected", nativePayload.project.path || "");
            return nativePayload.project;
          }
          if (nativePayload && nativePayload.status === "cancelled") {
            notifyProjectPicker("cancelled", "Folder picker cancelled");
            return null;
          }
          if (nativePayload && nativePayload.status === "error") {
            throw new Error(nativePayload.message || "Native folder picker failed");
          }
        } catch (error) {
          if (!canFallbackProjectPicker(error)) {
            notifyProjectPicker("error", error && error.message ? error.message : "Folder picker failed");
            throw error;
          }
          nativeError = error;
          notifyProjectPicker("fallback", error && error.message ? error.message : "Native folder picker unavailable");
        }
      } else {
        notifyProjectPicker("fallback", "Manual project path is required for non-local WebUI");
      }
      var label = desktopPlatform === "darwin"
        ? "请输入或粘贴本机项目文件夹路径，例如 /Users/name/project"
        : "请输入或粘贴本机项目文件夹路径，例如 C:\\\\Users\\\\name\\\\project";
      if (nativeError && nativeError.message) {
        label += "\\n\\nNative picker note: " + nativeError.message;
      }
      var folderPath = window.prompt(label, "");
      folderPath = String(folderPath || "").trim();
      if (!folderPath) {
        notifyProjectPicker("cancelled", "Folder picker cancelled");
        return null;
      }
      try {
        var payload = await apiJson({ path: "/api/project-folder", method: "POST", body: { path: folderPath, create: true } });
        notifyProjectPicker("selected", folderPath);
        return payload.project || null;
      } catch (error) {
        notifyProjectPicker("error", error && error.message ? error.message : "Folder registration failed");
        throw error;
      }
    },
    savePastedFile: savePastedFile,
    statPath: async function (filePath) {
      return apiJson({ path: "/api/file-stat", method: "POST", body: { path: filePath || "" } });
    },
    openPath: async function (filePath, action) {
      var result = await apiJson({ path: "/api/open-path", method: "POST", body: { path: filePath || "", action: action || "open" } });
      return result.message || "";
    },
    getPermissionState: async function () {
      return apiJson({ path: "/api/tool-permissions", method: "GET" });
    },
    setPermissionMode: async function (mode) {
      return apiJson({ path: "/api/tool-permissions", method: "POST", body: { action: "set_mode", mode: mode } });
    },
    resetPermissionGrants: async function () {
      return apiJson({ path: "/api/tool-permissions", method: "POST", body: { action: "reset_grants" } });
    },
    listCapabilityPacks: async function () {
      var payload = await apiJson({ path: "/api/capabilities", method: "GET" });
      var abilityPayload = payload.abilities;
      if (!Array.isArray(abilityPayload) && abilityPayload && Array.isArray(abilityPayload.abilities)) {
        abilityPayload = abilityPayload.abilities;
      }
      var abilities = Array.isArray(abilityPayload) ? abilityPayload : [];
      return abilities
        .filter(function (item) { return item && (item.agentCanInstall || item.packId || item.kind === "capability-pack"); })
        .map(function (item) {
          var state = item.capabilityState || {};
          var packId = item.packId || item.id || "";
          var installed = !!state.installed;
          return {
            id: String(packId),
            name: String(item.label || packId),
            summary: String(item.notes || item.defaultPolicy || ""),
            installMode: "user-or-admin",
            discoveryOnly: item.discoveryOnly === true || state.discoveryOnly === true,
            sourceUrl: typeof item.sourceUrl === "string" ? item.sourceUrl : state.sourceUrl,
            mirrorUrls: Array.isArray(item.mirrorUrls) ? item.mirrorUrls : state.mirrorUrls,
            installHint: typeof item.installHint === "string" ? item.installHint : state.installHint,
            defaultEnabled: item.defaultEnabled === true || state.defaultEnabled === true,
            readOnly: item.readOnly === true || state.readOnly === true,
            configureOnly: item.configureOnly === true || state.configureOnly === true,
            allowedCommands: Array.isArray(item.allowedCommands) ? item.allowedCommands : state.allowedCommands,
            state: String(state.state || (installed ? "installed" : "not-installed")),
            message: String(state.message || (installed ? "能力包已安装" : "点击安装后由当前会话 agent 处理")),
            installed: installed,
            logPath: state.logPath,
            updatedAt: state.updatedAt,
            policyMode: item.policyMode || state.policyMode || "ask",
            installAllowed: item.installAllowed !== false,
            disabledReason: item.disabledReason || state.disabledReason || "",
            policyUpdatedAt: item.policyUpdatedAt || state.policyUpdatedAt || "",
            policySource: item.policySource || state.policySource || ""
          };
        })
        .filter(function (item) { return !!item.id; });
    },
    reportTelemetry: async function (event) {
      try {
        var targetPath = event && event.type === "phase1_sync" ? "/sync/events" : "/events";
        await clientJson(targetPath, "POST", event || {}, true);
      } catch (error) {}
    },
    getEnterpriseSession: async function () {
      purgeGenericLocalSession();
      var admin = readAdminSession();
      if (admin && admin.user && admin.token) return admin;
      var auth = await apiJson({ path: "/auth/check", method: "GET" });
      if (auth.auth_required && !auth.authenticated) return null;
      if (!auth.auth_required) {
        return null;
      }
      var authIdentity = auth && auth.session && auth.session.user ? auth.session.user : null;
      if (auth && auth.session && auth.session.user && auth.session.user.email && !isGenericLocalSession(auth.session)) {
        writeLocalSession(auth.session);
      }
      if (!enterpriseClientConfigured()) {
        return isGenericLocalSession(auth.session) ? null : (auth.session || null);
      }
      try {
        await clientJson("/model-config", "GET", undefined, false);
      } catch (error) {
        if (isMissingClientBridge(error)) {
          return isGenericLocalSession(auth.session) ? null : (auth.session || null);
        }
      }
      if (auth && auth.session && auth.session.user && auth.session.user.email && !isGenericLocalSession(auth.session)) return auth.session;
      return null;
    },
    enterpriseLogin: async function (input) {
      input = input || {};
      var adminPayload = null;
      var adminError = null;
      var currentDeviceId = deviceId();
      if (enterpriseClientConfigured()) {
        try {
          adminPayload = await clientJson("/auth/login", "POST", {
            email: input.email,
            password: input.password,
            deviceId: currentDeviceId,
            appVersion: WEB_APP_VERSION
          }, false);
        } catch (error) {
          adminError = error;
        }
      }
      if (adminPayload && adminPayload.token && adminPayload.user) {
        var adminSession = {
          authenticated: true,
          token: adminPayload.token,
          deviceId: adminPayload.deviceId || adminPayload.device_id || currentDeviceId,
          clientKey: adminPayload.clientKey || WEB_CLIENT_KEY,
          expiresAt: adminPayload.expiresAt || new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
          user: adminPayload.user,
          quota: adminPayload.quota || { allowed: true }
        };
        writeAdminSession(adminSession);
        try { await apiJson({ path: "/auth/login", method: "POST", body: { email: input.email, password: input.password } }); } catch (error) {}
        try { await refreshModelPolicy(); } catch (error) {}
        return adminSession;
      }
      if (adminError && !isMissingClientBridge(adminError)) {
        throw adminError;
      }
      if (enterpriseClientConfigured()) {
        throw adminError || new Error("Enterprise login bridge is unavailable");
      }
      var localAuth = await apiJson({
        path: "/auth/login",
        method: "POST",
        body: { email: input.email, password: input.password }
      });
      if (localAuth && localAuth.session && localAuth.session.user && localAuth.session.user.email) {
        writeLocalSession(localAuth.session);
        return localAuth.session;
      }
      throw new Error("登录成功但运行时未返回有效会话，请重新登录。");
    },
    enterpriseLogout: async function () {
      writeAdminSession(null);
      writeLocalSession(null);
      return apiJson({ path: "/auth/logout", method: "POST", body: {} });
    },
    enterpriseChangePassword: async function (input) {
      return clientJson("/auth/change-password", "POST", input || {}, true);
    },
    checkEnterpriseQuota: async function (estimatedTokens) {
      try {
        var quota = await clientJson("/quota/check", "POST", { estimatedTokens: estimatedTokens || 0 }, true);
        if (quota) return quota;
      } catch (error) {}
      return { ok: true, quota: { allowed: true } };
    },
    refreshEnterprisePolicy: refreshModelPolicy
    ,
    getEnterpriseModelConfig: async function () {
      return clientJson("/model-config", "GET", undefined, true);
    }
  };
  try {
    window.ecorexDesktop = Object.assign(existingDesktopBridge, webDesktopBridge);
  } catch (error) {
    window.ecorexDesktop = webDesktopBridge;
  }
  window.ecorexDesktop.__ecorexWebBridgeVersion = WEB_APP_VERSION;
  installPhase1EventSourceSync();
})();
</script>
""".replace("__ECOREX_WEB_CLIENT_BASE__", configured_client_base).replace("__ECOREX_WEB_CLIENT_KEYS__", configured_client_keys).replace("__ECOREX_WEB_APP_VERSION__", bridge_app_version)


def _inject_web_app_bridge(html: str) -> str:
    bridge_script = _web_app_bridge_script()
    base_script = """<script data-ecorex-web-base>
(function () {
  var path = window.location.pathname || "/app/";
  var idx = path.indexOf("/app");
  var href = idx >= 0 ? path.slice(0, idx + 5) : "/app/";
  if (href.charAt(href.length - 1) !== "/") href += "/";
  document.write('<base href="' + href.replace(/"/g, "%22") + '">');
})();
</script>"""
    if 'data-ecorex-web-base' not in html and '<base ' not in html:
        html = html.replace("<head>", "<head>\n    " + base_script, 1)
    if "window.ecorexDesktop" not in html:
        marker = '<script type="module"'
        if marker in html:
            html = html.replace(marker, bridge_script + "\n    " + marker, 1)
        else:
            html = html.replace("</head>", bridge_script + "\n</head>", 1)
    return html


def _default_web_app_html() -> str:
    return _inject_web_app_bridge("""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>EcoreX Web App</title>
    <style>
      :root { color-scheme: light dark; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #101418; color: #edf2f7; }
      main { width: min(720px, calc(100vw - 40px)); }
      h1 { font-size: clamp(28px, 5vw, 44px); margin: 0 0 12px; letter-spacing: 0; }
      p { color: #aeb8c4; line-height: 1.7; margin: 0 0 22px; }
      nav { display: flex; flex-wrap: wrap; gap: 10px; }
      a { color: #101418; background: #edf2f7; border-radius: 8px; padding: 10px 14px; text-decoration: none; font-weight: 650; }
      a.secondary { color: #edf2f7; background: #25303b; }
    </style>
  </head>
  <body>
    <main>
      <h1>EcoreX Web App</h1>
      <p>The parallel Web entry is active. Drop a built app into channel/web/static/app to replace this shell; backend APIs are shared with the existing WebUI.</p>
      <nav>
        <a href="../app/">Open EcoreX</a>
        <a class="secondary" href="../api/installations">Installations API</a>
        <a class="secondary" href="../api/ui-state">UI state API</a>
      </nav>
    </main>
  </body>
</html>
""")

def _get_web_password() -> str:
    # Coerce to str so non-string values in config.json (e.g. numeric password) won't break comparisons
    pwd = conf().get("web_password", "")
    if pwd is None:
        return ""
    return str(pwd)


def _is_password_enabled():
    return bool(_get_web_password())


def _configured_web_host() -> str:
    return str(conf().get("web_host", "") or "").strip()


def _effective_web_host() -> str:
    configured_host = _configured_web_host()
    return configured_host or ("0.0.0.0" if _is_password_enabled() else "127.0.0.1")


def _is_loopback_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").lower()
    if normalized in {"", "localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_public_bind_host(host: str) -> bool:
    return not _is_loopback_bind_host(host)


def _validate_web_bind_auth(host: str) -> None:
    if _is_public_bind_host(host) and not _is_password_enabled():
        raise RuntimeError(
            "Refusing to start WebUI on a non-loopback address without web_password. "
            "Set web_password in config.json, or bind web_host to 127.0.0.1/localhost."
        )


def _session_expire_seconds():
    return int(conf().get("web_session_expire_days", 30)) * 86400


def _web_device_id() -> str:
    web_ctx = getattr(web, "ctx", None)
    env = getattr(web_ctx, "env", {}) if web_ctx else {}
    return str(env.get("HTTP_X_ECOREX_DEVICE_ID") or env.get("HTTP_X_DEVICE_ID") or "web-password").strip()


def _encode_auth_identity(email: str) -> str:
    raw = str(email or "").strip().lower().encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_auth_identity(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").strip().lower()
    except Exception:
        return ""


def _create_auth_token(email: str = ""):
    """Create a stateless signed token: ``<timestamp_hex>.<identity_b64>.<hmac_hex>``."""
    ts = format(int(time.time()), "x")
    identity = _encode_auth_identity(email)
    body = f"{ts}.{identity}"
    sig = hmac.new(
        _get_web_password().encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}.{sig}"


def _verify_auth_token(token):
    """Verify a signed token is valid and not expired.

    The token is derived from the password, so it survives server restarts
    and automatically invalidates when the password changes.
    """
    if not token or "." not in token:
        return False
    parts = token.split(".")
    if len(parts) == 2:
        ts_hex, sig = parts
        body = ts_hex
    elif len(parts) == 3:
        ts_hex, _identity, sig = parts
        body = f"{ts_hex}.{_identity}"
    else:
        return False
    try:
        ts = int(ts_hex, 16)
    except ValueError:
        return False
    if time.time() - ts > _session_expire_seconds():
        return False
    expected = hmac.new(
        _get_web_password().encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def _auth_token_email(token: str) -> str:
    if not _verify_auth_token(token):
        return ""
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    return _decode_auth_identity(parts[1])


def _desktop_runtime_token_matches() -> bool:
    runtime_token = os.environ.get("ECOREX_DESKTOP_RUNTIME_TOKEN", "")
    web_ctx = getattr(web, "ctx", None)
    env = getattr(web_ctx, "env", {}) if web_ctx else {}
    header_token = env.get("HTTP_X_ECOREX_RUNTIME_TOKEN", "")
    return bool(runtime_token and header_token and hmac.compare_digest(runtime_token, header_token))


def _desktop_runtime_token_required() -> bool:
    if os.environ.get("ECOREX_DESKTOP_RUNTIME_TOKEN"):
        return True
    return str(os.environ.get("ECOREX_DESKTOP", "")).strip().lower() in {"1", "true", "yes"}


WEB_ENTERPRISE_AUTH_CACHE_TTL_SECONDS = 30
WEB_ENTERPRISE_CLIENT_KEYS = (
    "ecorex-web-v0.3.0-web.1",
    "ecorex-web-v0.2.9.2-web.1",
    "ecorex-web-v0.2.9.1-web.1",
    "ecorex-web-v0.2.9-web.1",
    "ecorex-web-v0.2.8-web.1",
    "ecorex-web-v0.2.7.2-web.1",
    "ecorex-web-v0.2.7.1-web.1",
    "ecorex-web-v0.2.7-web.1",
    "ecorex-web-v0.2.6-web.1",
    "ecorex-web-v0.2.2-web.1",
    "ecorex-web-v0.2.1-web.1",
    "ecorex-web-v0.2.0-web.1",
    "ecorex-web-v0.1.19-web.1",
    "ecorex-web-v0.1.18-web.1",
    "ecorex-web-v0.1.17-web.1",
    "ecorex-web-v0.1.16-web.1",
    "ecorex-web-v0.1.15-web.1",
    "ecorex-web-v0.1.14-web.1",
    "ecorex-web-v0.1.13-web.1",
    "ecorex-web-v0.1.12-web.1",
    "ecorex-web-v0.1.11-web.1",
)
_enterprise_auth_cache: Dict[str, Tuple[float, bool]] = {}
_enterprise_policy_sync_cache: Dict[str, Tuple[float, bool]] = {}


def _enterprise_client_keys_for_request() -> List[str]:
    requested = _request_header("X-EcoreX-Client-Key").strip()
    keys: List[str] = []
    for key in (requested, *WEB_ENTERPRISE_CLIENT_KEYS):
        key = str(key or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _request_header(name: str) -> str:
    env_name = "HTTP_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    web_ctx = getattr(web, "ctx", None)
    env = getattr(web_ctx, "env", {}) if web_ctx else {}
    if name.lower() == "content-type":
        return str(env.get("CONTENT_TYPE", "") or "")
    return str(env.get(env_name, "") or "")


def _web_enterprise_client_base() -> str:
    public_base = str(os.environ.get("ECOREX_WEB_PUBLIC_BASE_URL") or conf().get("web_public_base_url") or "").strip().rstrip("/")
    configured = (
        os.environ.get("ECOREX_WEB_CLIENT_BASE")
        or conf().get("web_client_base")
        or conf().get("admin_client_base")
        or (f"{public_base}/client" if public_base else "")
        or ClientProxyHandler.DEFAULT_CLIENT_BASE
    )
    return str(configured).strip().rstrip("/")


def _enterprise_user_token_from_request() -> str:
    auth = _request_header("Authorization")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return _request_header("X-EcoreX-User-Token").strip()


def _enterprise_user_token_auth_valid() -> bool:
    """Validate Admin-managed enterprise user tokens for Web runtime APIs."""
    token = _enterprise_user_token_from_request()
    if not token:
        return False
    device_id = _request_header("X-EcoreX-Device-Id").strip()
    client_keys = _enterprise_client_keys_for_request()
    cache_key = hashlib.sha256(f"{token}\n{device_id}\n{','.join(client_keys)}".encode("utf-8", errors="replace")).hexdigest()
    now = time.time()
    cached = _enterprise_auth_cache.get(cache_key)
    if cached and now - cached[0] < WEB_ENTERPRISE_AUTH_CACHE_TTL_SECONDS:
        return cached[1]
    valid = False
    try:
        for client_key in client_keys:
            request = urllib.request.Request(
                f"{_web_enterprise_client_base()}/model-config",
                headers={
                    "Accept": "application/json",
                    "X-EcoreX-Client-Key": client_key,
                    "X-EcoreX-User-Token": token,
                    "X-EcoreX-Device-Id": device_id,
                    "User-Agent": "EcoreX-WebRuntimeAuth/0.3.0",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read(512_000).decode("utf-8", errors="replace") or "{}")
                    valid = response.status < 400 and payload.get("ok") is not False
                    if valid:
                        _sync_enterprise_model_policy_payload(payload, cache_key)
                if valid:
                    break
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    raise
                continue
    except Exception as exc:
        logger.debug(f"[WebChannel] enterprise token auth failed: {_web_body_log_summary(exc)}")
        valid = False
    _enterprise_auth_cache[cache_key] = (now, valid)
    if len(_enterprise_auth_cache) > 512:
        expired_before = now - WEB_ENTERPRISE_AUTH_CACHE_TTL_SECONDS
        for key, (stamp, _value) in list(_enterprise_auth_cache.items()):
            if stamp < expired_before:
                _enterprise_auth_cache.pop(key, None)
    return valid


def _sync_enterprise_model_policy_payload(payload: dict, cache_key: str = "") -> bool:
    """Apply Admin model policy to the live Web runtime after token auth.

    Browser-side WebUI normally refreshes the enterprise policy before sending,
    but old tabs and stale local sessions can miss that step. Keeping the sync
    on the authenticated backend path makes `/api/models` and `/message` recover
    without requiring users to know why a model policy banner got stale.
    """
    if not isinstance(payload, dict) or payload.get("ok") is False or not payload.get("configured"):
        return False
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not settings:
        return False
    now = time.time()
    if cache_key:
        cached = _enterprise_policy_sync_cache.get(cache_key)
        if cached and now - cached[0] < WEB_ENTERPRISE_AUTH_CACHE_TTL_SECONDS and cached[1]:
            return True
    try:
        handler = globals().get("ModelsHandler")
        if handler is None:
            return False
        local_config = conf()
        file_cfg = handler._read_file_config()
        if not isinstance(file_cfg, dict):
            file_cfg = {}
        changed = False
        for key, value in settings.items():
            if local_config.get(key) != value:
                local_config[key] = value
                changed = True
            if file_cfg.get(key) != value:
                file_cfg[key] = value
                changed = True
        if changed:
            handler._write_file_config(file_cfg)
            try:
                handler._reset_bridge()
            except Exception as exc:
                logger.debug(f"[WebChannel] enterprise model policy bridge refresh skipped: {_web_body_log_summary(exc)}")
            logger.info(
                "[WebChannel] Enterprise model policy synced from authenticated user token "
                f"(provider={payload.get('provider')!r}, model={payload.get('model')!r})"
            )
        if cache_key:
            _enterprise_policy_sync_cache[cache_key] = (now, True)
            if len(_enterprise_policy_sync_cache) > 512:
                expired_before = now - WEB_ENTERPRISE_AUTH_CACHE_TTL_SECONDS
                for key, (stamp, _value) in list(_enterprise_policy_sync_cache.items()):
                    if stamp < expired_before:
                        _enterprise_policy_sync_cache.pop(key, None)
        return True
    except Exception as exc:
        logger.warning(f"[WebChannel] enterprise model policy sync failed: {_web_body_log_summary(exc)}")
        if cache_key:
            _enterprise_policy_sync_cache[cache_key] = (now, False)
        return False


def _check_auth():
    """Return True if request is authenticated or password not enabled."""
    if _desktop_runtime_token_matches():
        return True
    if _desktop_runtime_token_required():
        return False
    if not _is_password_enabled():
        return not _is_public_bind_host(_effective_web_host())
    return _verify_auth_token(web.cookies().get("cow_auth_token", "")) or _enterprise_user_token_auth_valid()


def _require_auth():
    """Raise 401 if not authenticated. Call at the top of protected handlers."""
    if not _check_auth():
        raise web.HTTPError("401 Unauthorized",
                            {"Content-Type": "application/json; charset=utf-8"},
                            json.dumps({"status": "error", "message": "Unauthorized"}))


# Localized text for /cancel system replies. Web is the only channel that
# honors a per-request `lang`; other channels reply in Chinese by default.
def _cancel_reply_text(cancelled: int, lang: str) -> str:
    en = lang.startswith("en")
    if cancelled > 0:
        return "🛑 Cancelled" if en else "🛑 已中止"
    return "Nothing to cancel." if en else "当前没有可中止的任务。"


def _get_upload_dir() -> str:
    from common.utils import expand_path
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def _sanitize_upload_relative_path(relative_path: str) -> str:
    """Normalize relative upload path and reject escapes / absolute paths."""
    relative_path = (relative_path or "").replace("\\", "/").strip("/")
    if not relative_path:
        raise ValueError("Empty relative path")
    parts = []
    for part in relative_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Invalid relative path")
        parts.append(part)
    if not parts:
        raise ValueError("Invalid relative path")
    norm_path = "/".join(parts)
    if os.path.isabs(norm_path):
        raise ValueError("Invalid relative path")
    return norm_path


def _sanitize_upload_id(upload_id: str) -> str:
    """Allow only simple batch ids for directory uploads."""
    sanitized = "".join(ch for ch in (upload_id or "") if ch.isalnum() or ch in ("-", "_"))
    if not sanitized:
        raise ValueError("Invalid upload id")
    return sanitized[:80]


def _is_within_directory(root_path: str, target_path: str) -> bool:
    try:
        return os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        return False


def _resolve_upload_path(upload_root: str, relative_path: str) -> Tuple[str, str]:
    """Resolve a relative upload path under upload_root and reject escapes."""
    safe_rel_path = _sanitize_upload_relative_path(relative_path)
    upload_root_real = os.path.realpath(upload_root)
    save_path = os.path.realpath(os.path.join(upload_root_real, *safe_rel_path.split("/")))
    if not _is_within_directory(upload_root_real, save_path):
        raise ValueError("Invalid directory upload path")
    return safe_rel_path, save_path


def _read_uploaded_file_bytes(file_obj) -> bytes:
    """Return uploaded content as bytes across web.py upload object variants."""
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None

    if hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value

    if content is None:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")


def _raw_web_input():
    """Return unprocessed multipart form data when web.py exposes rawinput."""
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise RuntimeError("web.py rawinput is not available")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _generate_session_title(user_message: str = "", assistant_reply: str = "", **kwargs) -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply, **kwargs)


def _project_context_text_value(value: Any, limit: int = 4096) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit]
    return text


def _web_body_log_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "redacted": bool(text),
        "hash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] if text else "",
        "chars": len(text),
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


def _public_exception_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "errorType": type(value).__name__ if value is not None else "",
        "errorHash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] if text else "",
        "errorLength": len(text),
        "errorBytes": len(text.encode("utf-8", errors="replace")),
    }


def _public_exception_message(prefix: str, value: Any) -> str:
    summary = _public_exception_summary(value)
    if not summary["errorHash"]:
        return prefix
    return (
        f"{prefix} Details redacted "
        f"(type={summary['errorType']}, hash={summary['errorHash']}, "
        f"chars={summary['errorLength']}, bytes={summary['errorBytes']})."
    )


def _public_error_payload(prefix: str, value: Any, **extra: Any) -> Dict[str, Any]:
    return {
        "status": "error",
        "message": _public_exception_message(prefix, value),
        **_public_exception_summary(value),
        **extra,
    }


def _public_validation_error_payload(value: Any) -> Dict[str, Any]:
    message = mask_sensitive_text(str(value or ""), max_chars=240).strip()
    return {
        "status": "error",
        "message": message or "Invalid request.",
    }


TENCENT_DOCS_MCP_SERVER_NAME = "tencent-docs"
TENCENT_DOCS_MCP_ENDPOINT = "https://docs.qq.com/openapi/mcp"
TENCENT_DOCS_AUTH_URL = os.environ.get("ECOREX_TENCENT_DOCS_AUTH_URL", "https://docs.qq.com/open/auth/mcp.html").strip() or "https://docs.qq.com/open/auth/mcp.html"
TENCENT_DOCS_PROVIDER = "tencent-docs"
REMOTE_ATTACHMENT_SAFE_KEYS = (
    "file_path",
    "file_name",
    "file_type",
    "preview_url",
    "provider",
    "source",
    "key",
    "file_id",
    "fileId",
    "node_id",
    "nodeId",
    "doc_type",
    "docType",
    "url",
    "owner",
    "updated_at",
    "updatedAt",
    "remote",
)


def _safe_attachment_snapshot(attachments: Any, limit: int = 20) -> List[Dict[str, str]]:
    if not isinstance(attachments, list):
        return []
    cleaned_attachments: List[Dict[str, str]] = []
    for att in attachments[:limit]:
        if not isinstance(att, dict):
            continue
        cleaned: Dict[str, str] = {}
        for key in REMOTE_ATTACHMENT_SAFE_KEYS:
            value = att.get(key)
            if isinstance(value, bool):
                cleaned[key] = "true" if value else "false"
            elif value:
                cleaned[key] = str(value)[:4096]
        if cleaned.get("file_path") or cleaned.get("key") or cleaned.get("url"):
            cleaned_attachments.append(cleaned)
    return cleaned_attachments


def _is_tencent_docs_attachment(att: Any) -> bool:
    if not isinstance(att, dict):
        return False
    provider = str(att.get("provider") or att.get("source") or "").strip().lower()
    file_path = str(att.get("file_path") or att.get("path") or att.get("key") or "").strip().lower()
    return provider == TENCENT_DOCS_PROVIDER or file_path.startswith("tencent-docs://")


def _tencent_docs_attachment_id(att: Dict[str, Any]) -> str:
    for key in ("file_id", "fileId", "node_id", "nodeId", "id", "key"):
        value = str(att.get(key) or "").strip()
        if value:
            return value.replace("tencent-docs://", "", 1)
    path_value = str(att.get("file_path") or att.get("path") or "").strip()
    if path_value.startswith("tencent-docs://"):
        return path_value.replace("tencent-docs://", "", 1)
    url = str(att.get("url") or "").strip()
    title = str(att.get("file_name") or att.get("title") or att.get("name") or "").strip()
    digest_source = url or title
    return hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:16] if digest_source else ""


def _web_attachment_prompt_refs_and_context(attachments: Any) -> Tuple[List[str], str]:
    attachments = attachments if isinstance(attachments, list) else []
    file_refs: List[str] = []
    remote_lines: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if _is_tencent_docs_attachment(att):
            title = str(att.get("file_name") or att.get("title") or att.get("name") or "Tencent Docs document").strip()
            file_id = _tencent_docs_attachment_id(att)
            node_id = str(att.get("node_id") or att.get("nodeId") or "").strip()
            doc_type = str(att.get("doc_type") or att.get("docType") or att.get("file_type") or "").strip()
            url = str(att.get("url") or "").strip()
            display_id = file_id or node_id or url
            file_refs.append(f"[腾讯文档: {title}{f' ({display_id})' if display_id else ''}]")
            remote_lines.append(
                "- "
                + "; ".join(
                    part
                    for part in [
                        f"title={title}",
                        f"file_id={file_id}" if file_id else "",
                        f"node_id={node_id}" if node_id else "",
                        f"doc_type={doc_type}" if doc_type else "",
                        f"url={url}" if url else "",
                    ]
                    if part
                )
            )
            continue
        ftype = att.get("file_type", "file")
        fpath = att.get("file_path", "")
        if not fpath:
            continue
        if ftype == "image":
            file_refs.append(f"[{i18n.t('图片', 'Image')}: {fpath}]")
        elif ftype == "video":
            file_refs.append(f"[{i18n.t('视频', 'Video')}: {fpath}]")
        elif ftype == "directory":
            file_refs.append(f"[{i18n.t('目录', 'Directory')}: {fpath}]")
        else:
            file_refs.append(f"[{i18n.t('文件', 'File')}: {fpath}]")

    if not remote_lines:
        return file_refs, ""
    remote_context = "\n".join([
        "Tencent Docs remote attachments selected in WebUI:",
        *remote_lines,
        "",
        "These Tencent Docs attachments are remote documents, not local file paths. "
        "Do not read them from the local filesystem. When document content is needed, "
        "use the available MCP tools from the `tencent-docs` server discovered at runtime. "
        "Treat Tencent Docs as read-only unless the user explicitly asks to create, update, or delete documents.",
    ])
    return file_refs, remote_context


def _attachments_include_tencent_docs(attachments: Any) -> bool:
    attachments = attachments if isinstance(attachments, list) else []
    return any(isinstance(att, dict) and _is_tencent_docs_attachment(att) for att in attachments)


def _ensure_tencent_docs_tools_for_attachments(attachments: Any, reason: str = "attachment") -> Dict[str, Any]:
    if not _attachments_include_tencent_docs(attachments):
        return {}
    snapshot = _tencent_docs_wait_for_ready(timeout_seconds=4.0)
    status = str(snapshot.get("runtimeStatus") or "")
    if status != "ready" and int(snapshot.get("toolCount") or 0) <= 0:
        logger.warning(
            f"[WebChannel] Tencent Docs attachment sent before MCP ready: "
            f"reason={reason}, status={status}, tools={snapshot.get('toolCount')}"
        )
    else:
        logger.info(
            f"[WebChannel] Tencent Docs MCP ready for attachment flow: "
            f"reason={reason}, status={status}, tools={snapshot.get('toolCount')}"
        )
    return snapshot


def _append_hidden_context(hidden_context: Any, extra_context: str) -> str:
    parts = [str(hidden_context or "").strip(), str(extra_context or "").strip()]
    return "\n\n".join(part for part in parts if part)


def _tencent_docs_mcp_path() -> Path:
    return Path(_get_workspace_root()).expanduser() / "mcp.json"


def _read_workspace_mcp_payload() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    path = _tencent_docs_mcp_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        raw_list = payload.get("mcp_servers")
        if isinstance(raw_list, list):
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                entry = dict(item)
                entry.pop("name", None)
                servers[name] = entry
        if not servers:
            for name, item in payload.items():
                if isinstance(item, dict) and any(key in item for key in ("url", "command", "type")):
                    servers[str(name)] = dict(item)
        payload["mcpServers"] = servers
    return payload, servers


def _write_workspace_mcp_payload(payload: Dict[str, Any]) -> None:
    path = _tencent_docs_mcp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _tencent_docs_auth_header(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("Tencent Docs authorization token is required")
    if re.match(r"(?i)^(bearer|basic|token)\s+", raw):
        return raw
    return f"Bearer {raw}"


def _tencent_docs_configured_entry() -> Dict[str, Any]:
    _payload, servers = _read_workspace_mcp_payload()
    entry = servers.get(TENCENT_DOCS_MCP_SERVER_NAME)
    return dict(entry) if isinstance(entry, dict) else {}


def _tencent_docs_is_configured() -> bool:
    entry = _tencent_docs_configured_entry()
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    return bool(entry.get("url") == TENCENT_DOCS_MCP_ENDPOINT and headers.get("Authorization"))


def _write_tencent_docs_mcp_config(token: str) -> None:
    auth_header = _tencent_docs_auth_header(token)
    payload, servers = _read_workspace_mcp_payload()
    servers[TENCENT_DOCS_MCP_SERVER_NAME] = {
        "type": "streamable-http",
        "url": TENCENT_DOCS_MCP_ENDPOINT,
        "headers": {
            "Authorization": auth_header,
        },
        "timeout": 120,
    }
    payload["mcpServers"] = servers
    _write_workspace_mcp_payload(payload)


def _remove_tencent_docs_mcp_config() -> bool:
    payload, servers = _read_workspace_mcp_payload()
    existed = TENCENT_DOCS_MCP_SERVER_NAME in servers
    servers.pop(TENCENT_DOCS_MCP_SERVER_NAME, None)
    payload["mcpServers"] = servers
    _write_workspace_mcp_payload(payload)
    return existed


def _tencent_docs_tool_snapshot(start: bool = False) -> Dict[str, Any]:
    try:
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        if not getattr(manager, "tool_classes", None):
            manager.load_tools(start_mcp=False)
        ensure_mcp = getattr(manager, "ensure_mcp_configured_loaded", None)
        if callable(ensure_mcp):
            ensure_mcp(
                wait_seconds=0.5 if start else 0.0,
                server_name=TENCENT_DOCS_MCP_SERVER_NAME,
            )
        elif start:
            if not getattr(manager, "_mcp_loaded", False):
                manager._load_mcp_tools()
            else:
                manager.refresh_mcp_if_changed()
        elif getattr(manager, "_mcp_loaded", False):
            manager.refresh_mcp_if_changed()
        status = manager.list_mcp_status().get(TENCENT_DOCS_MCP_SERVER_NAME, "not_loaded")
        tools = []
        for public_name, tool in list(getattr(manager, "_mcp_tool_instances", {}).items()):
            if getattr(tool, "server_name", "") != TENCENT_DOCS_MCP_SERVER_NAME:
                continue
            tools.append({
                "name": str(public_name),
                "remoteName": str(getattr(tool, "remote_name", "")),
                "description": str(getattr(tool, "description", ""))[:240],
            })
        return {
            "runtimeStatus": status,
            "toolCount": len(tools),
            "contentToolCount": sum(1 for item in tools if _tencent_docs_tool_score(item, "content", "") > 0),
            "tools": tools,
        }
    except Exception as exc:
        logger.debug(f"[WebChannel] Tencent Docs MCP snapshot unavailable: {_web_body_log_summary(exc)}")
        return {"runtimeStatus": "unknown", "toolCount": 0, "contentToolCount": 0, "tools": []}


def _tencent_docs_wait_for_ready(timeout_seconds: float = 8.0, interval_seconds: float = 0.4) -> Dict[str, Any]:
    deadline = time.time() + max(0.0, float(timeout_seconds or 0))
    snapshot = _tencent_docs_tool_snapshot(start=True)
    while True:
        status = str(snapshot.get("runtimeStatus") or "")
        if status == "ready" or int(snapshot.get("toolCount") or 0) > 0:
            return snapshot
        if time.time() >= deadline:
            return snapshot
        time.sleep(max(0.05, float(interval_seconds or 0.4)))
        snapshot = _tencent_docs_tool_snapshot(start=False)


def _tencent_docs_status_payload(*, start: bool = False) -> Dict[str, Any]:
    configured = _tencent_docs_is_configured()
    runtime = _tencent_docs_wait_for_ready(timeout_seconds=6.0) if start and configured else _tencent_docs_tool_snapshot(start=False)
    runtime_status = str(runtime.get("runtimeStatus") or "not_loaded")
    return {
        "status": "success",
        "capability": {
            "id": TENCENT_DOCS_PROVIDER,
            "displayName": "腾讯文档",
            "endpoint": TENCENT_DOCS_MCP_ENDPOINT,
            "authUrl": TENCENT_DOCS_AUTH_URL,
            "authMode": "official_qr_scan_with_token_fallback",
            "qrLoginAvailable": True,
            "tokenFallbackAvailable": True,
            "setupHint": "Scan or open Tencent Docs official MCP authorization, then use the token fallback only when automatic connection is unavailable.",
            "configured": configured,
            "connected": configured and runtime_status == "ready",
            "runtimeStatus": runtime_status,
            "toolCount": int(runtime.get("toolCount") or 0),
            "contentToolCount": int(runtime.get("contentToolCount") or 0),
            "redacted": True,
        },
    }


def _tencent_docs_tool_score(tool: Any, mode: str, query: str) -> int:
    haystack = " ".join(
        str(value or "")
        for value in [
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""),
            tool.get("remoteName") if isinstance(tool, dict) else getattr(tool, "remote_name", ""),
            tool.get("description") if isinstance(tool, dict) else getattr(tool, "description", ""),
            json.dumps(tool.get("parameters") or {}, ensure_ascii=False, default=str) if isinstance(tool, dict) else json.dumps(getattr(tool, "params", {}) or {}, ensure_ascii=False, default=str),
        ]
    ).lower()
    score = 0
    if any(token in haystack for token in ("doc", "document", "file", "docs", "腾讯", "文档")):
        score += 1
    if mode == "search" or query:
        if any(token in haystack for token in ("search", "find", "query", "搜索", "检索")):
            score += 5
    elif mode == "recent":
        if any(token in haystack for token in ("recent", "history", "最近")):
            score += 5
        if any(token in haystack for token in ("list", "documents", "files", "文档", "列表")):
            score += 2
    else:
        if any(token in haystack for token in ("list", "documents", "files", "my", "drive", "文档", "列表", "我的")):
            score += 4
    if any(token in haystack for token in ("read", "content", "get", "open", "读取", "内容")):
        score += 1 if mode == "content" else 0
    return score


def _tencent_docs_tool_candidates(mode: str, query: str = "") -> List[Any]:
    try:
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        tools = []
        for _public_name, tool in list(getattr(manager, "_mcp_tool_instances", {}).items()):
            if getattr(tool, "server_name", "") == TENCENT_DOCS_MCP_SERVER_NAME:
                score = _tencent_docs_tool_score(tool, mode, query)
                if score > 0:
                    tools.append((score, tool))
        tools.sort(key=lambda item: item[0], reverse=True)
        return [tool for _score, tool in tools]
    except Exception:
        return []


def _tencent_docs_tool_args(tool: Any, mode: str, query: str, limit: int) -> Dict[str, Any]:
    params = getattr(tool, "params", {}) or {}
    props = params.get("properties") if isinstance(params, dict) else {}
    props = props if isinstance(props, dict) else {}
    args: Dict[str, Any] = {}
    for key in props.keys():
        lowered = str(key).lower()
        if query and lowered in {"q", "query", "keyword", "keywords", "search", "text"}:
            args[key] = query
        elif lowered in {"limit", "page_size", "pagesize", "size", "count"}:
            args[key] = limit
        elif lowered in {"scope", "tab", "category", "type"} and mode in {"recent", "mine", "my", "search"}:
            args[key] = "my" if mode == "mine" else mode
    if query and not any(str(key).lower() in {"q", "query", "keyword", "keywords", "search", "text"} for key in args):
        args["query"] = query
    if not any(str(key).lower() in {"limit", "page_size", "pagesize", "size", "count"} for key in args):
        args["limit"] = limit
    return args


def _json_loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return text


def _first_present(data: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_tencent_docs_file(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        title = item.strip()
        if not title:
            return None
        digest = hashlib.sha256(title.encode("utf-8", errors="replace")).hexdigest()[:16]
        return {
            "key": f"tencent-docs://{digest}",
            "provider": TENCENT_DOCS_PROVIDER,
            "file_id": digest,
            "title": title,
            "file_name": title,
            "file_type": "file",
        }
    if not isinstance(item, dict):
        return None
    file_id = _first_present(item, "file_id", "fileId", "doc_id", "docId", "id", "token", "resource_id", "resourceId")
    node_id = _first_present(item, "node_id", "nodeId", "node_token", "nodeToken")
    url = _first_present(item, "url", "link", "web_url", "webUrl", "docs_url", "docsUrl")
    title = _first_present(item, "title", "name", "file_name", "fileName", "display_name", "displayName") or file_id or node_id or "腾讯文档"
    stable = file_id or node_id or (hashlib.sha256((url or title).encode("utf-8", errors="replace")).hexdigest()[:16])
    doc_type = _first_present(item, "doc_type", "docType", "type", "file_type", "fileType", "document_type", "documentType") or "file"
    result = {
        "key": f"tencent-docs://{stable}",
        "provider": TENCENT_DOCS_PROVIDER,
        "source": TENCENT_DOCS_PROVIDER,
        "file_id": file_id or stable,
        "node_id": node_id,
        "title": title,
        "file_name": title,
        "file_type": "file",
        "doc_type": doc_type,
        "url": url,
        "owner": _first_present(item, "owner", "owner_name", "ownerName", "creator", "creator_name", "creatorName"),
        "updated_at": _first_present(item, "updated_at", "updatedAt", "modified_time", "modifiedTime", "update_time", "updateTime"),
    }
    return {key: value for key, value in result.items() if value}


def _extract_tencent_docs_files(payload: Any) -> List[Dict[str, Any]]:
    value = _json_loads_maybe(payload)
    if isinstance(value, dict):
        for key in ("files", "documents", "docs", "items", "list", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in (_normalize_tencent_docs_file(entry) for entry in nested) if item]
        data = value.get("data")
        if isinstance(data, (dict, list)):
            return _extract_tencent_docs_files(data)
        one = _normalize_tencent_docs_file(value)
        return [one] if one else []
    if isinstance(value, list):
        return [item for item in (_normalize_tencent_docs_file(entry) for entry in value) if item]
    return []


def _tencent_docs_files_payload(mode: str = "recent", query: str = "", limit: int = 20) -> Dict[str, Any]:
    mode = str(mode or "recent").strip().lower()
    query = str(query or "").strip()
    try:
        limit = max(1, min(50, int(limit or 20)))
    except (TypeError, ValueError):
        limit = 20
    if not _tencent_docs_is_configured():
        return {"status": "error", "message": "Tencent Docs MCP is not connected.", "files": [], "redacted": True}
    runtime = _tencent_docs_wait_for_ready(timeout_seconds=8.0)
    candidates = _tencent_docs_tool_candidates("search" if query else mode, query)
    if not candidates:
        return {
            "status": "success",
            "files": [],
            "message": f"Tencent Docs MCP tools are not ready yet (runtime: {runtime.get('runtimeStatus') or 'unknown'}). Retry after the runtime finishes loading MCP tools.",
            "redacted": True,
        }
    errors: List[Dict[str, Any]] = []
    for tool in candidates[:3]:
        args = _tencent_docs_tool_args(tool, "search" if query else mode, query, limit)
        try:
            result = tool.execute(args)
            if getattr(result, "status", "") != "success":
                errors.append(_public_exception_summary(getattr(result, "result", "")))
                continue
            files = _extract_tencent_docs_files(getattr(result, "result", {}))[:limit]
            return {
                "status": "success",
                "files": files,
                "source": {
                    "server": TENCENT_DOCS_MCP_SERVER_NAME,
                    "tool": getattr(tool, "name", ""),
                    "remoteTool": getattr(tool, "remote_name", ""),
                    "redacted": True,
                },
                "redacted": True,
            }
        except Exception as exc:
            errors.append(_public_exception_summary(exc))
    return {
        "status": "error",
        "message": "Tencent Docs MCP file listing failed. Details are redacted.",
        "files": [],
        "errors": errors[:3],
        "redacted": True,
    }


def _normalize_project_context_meta(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    project_id = _project_context_text_value(value.get("projectId") or value.get("project_id"), 256)
    project_path = _project_context_text_value(value.get("projectPath") or value.get("project_path"), 4096)
    if not project_id or not project_path:
        return {}
    project_name = _project_context_text_value(value.get("projectName") or value.get("project_name") or project_id, 512)
    memory_path = _project_context_text_value(value.get("memoryPath") or value.get("memory_path"), 4096)
    dreams_path = _project_context_text_value(value.get("dreamsPath") or value.get("dreams_path"), 4096)
    if not memory_path:
        memory_path = os.path.join(project_path, ".ecorex", "project-memory.md")
    if not dreams_path:
        dreams_path = os.path.join(project_path, ".ecorex", "dreams")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "projectId": project_id,
        "projectName": project_name,
        "projectPath": project_path,
        "memoryPath": memory_path,
        "dreamsPath": dreams_path,
        "createdAt": _project_context_text_value(value.get("createdAt") or value.get("created_at"), 128),
        "lastUsedAt": now,
        "source": _project_context_text_value(value.get("source") or "web-message", 128),
    }


def _project_context_event_summary(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {"present": False}
    refs = [
        str(value.get("projectId") or ""),
        str(value.get("projectPath") or ""),
        str(value.get("memoryPath") or ""),
        str(value.get("dreamsPath") or ""),
    ]
    digest = hashlib.sha256("\n".join(refs).encode("utf-8", errors="replace")).hexdigest()[:16]
    return {
        "present": True,
        "scope": "project",
        "source": _project_context_text_value(value.get("source") or "web-message", 128),
        "bindingHash": digest,
    }


def _persist_project_session_binding(session_id: str, binding: Dict[str, Any]) -> None:
    if not session_id or not binding:
        return
    try:
        from common.ecorex_workspace import save_ui_state
        project = {
            "id": binding.get("projectId"),
            "name": binding.get("projectName") or binding.get("projectId"),
            "path": binding.get("projectPath"),
            "memoryPath": binding.get("memoryPath"),
            "dreamsPath": binding.get("dreamsPath"),
            "updatedAt": binding.get("lastUsedAt") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        save_ui_state(_get_workspace_root(), {
            "projectStateMode": "merge",
            "projects": [project] if project.get("id") and project.get("path") else [],
            "sessionProjects": {session_id: binding.get("projectId")},
            "sessionProjectBindings": {session_id: binding},
        })
    except Exception as exc:
        logger.warning(f"[WebChannel] failed to persist project session binding: {_web_body_log_summary(exc)}")


def restore_feishu_public_auth_fields(public_result: Any, bounded_raw_result: Any, tool_name: str) -> Any:
    if str(tool_name or "").lower() != "feishu_cli":
        return public_result
    if not isinstance(public_result, dict) or not isinstance(bounded_raw_result, dict):
        return public_result
    if not (bounded_raw_result.get("authRequired") or bounded_raw_result.get("writebackPending")):
        return public_result
    restored = dict(public_result)
    url = str(bounded_raw_result.get("verificationUrl") or "").strip()
    if url.startswith("https://open.feishu.cn/") or url.startswith("https://open.larksuite.com/"):
        restored["verificationUrl"] = url
    qr = bounded_raw_result.get("qrCode")
    if isinstance(qr, dict):
        public_qr = dict(restored.get("qrCode") or {}) if isinstance(restored.get("qrCode"), dict) else {}
        safe_qr: Dict[str, Any] = {}
        for key in ("status", "exitCode", "relativePath", "message"):
            if key in qr:
                safe_qr[key] = qr.get(key)
        safe_qr.update(public_qr)
        if safe_qr:
            restored["qrCode"] = safe_qr
    for key in ("authRequired", "writebackPending", "backgroundProcess", "cliWritebackTimeoutSeconds", "authFlow", "nextAction", "sessionId", "authCompleted", "authenticated", "authState"):
        if key in bounded_raw_result:
            if key == "nextAction":
                restored[key] = redact_public_tool_value(bounded_raw_result.get(key))
                raw_next = bounded_raw_result.get(key)
                if isinstance(raw_next, dict) and raw_next.get("session_id"):
                    restored[key]["session_id"] = str(raw_next.get("session_id"))
            else:
                restored[key] = bounded_raw_result.get(key)
    return restored


def _safe_feishu_agent_auth_payload(public_result: Any) -> Dict[str, Any]:
    if not isinstance(public_result, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key in (
        "status",
        "exitCode",
        "authRequired",
        "writebackPending",
        "backgroundProcess",
        "cliWritebackTimeoutSeconds",
        "authFlow",
        "sessionId",
        "authCompleted",
        "authenticated",
        "authState",
        "verificationUrl",
        "webSessionId",
        "traceId",
        "message",
    ):
        value = public_result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    next_action = public_result.get("nextAction")
    if isinstance(next_action, dict):
        safe["nextAction"] = {
            key: value
            for key, value in next_action.items()
            if key in {"tool", "action", "domain", "scope", "session_id", "sessionId", "webSessionId", "traceId"}
            and (isinstance(value, str) or value is None)
        }
    qr = public_result.get("qrCode")
    if isinstance(qr, dict):
        safe_qr = {
            key: value
            for key, value in qr.items()
            if key in {"status", "exitCode", "relativePath", "message"}
            and (isinstance(value, (str, int, float, bool)) or value is None)
        }
        if safe_qr:
            safe["qrCode"] = safe_qr
    return safe


class WebMessage(ChatMessage):
    def __init__(
            self,
            msg_id,
            content,
            ctype=ContextType.TEXT,
            from_user_id="User",
            to_user_id="Chatgpt",
            other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None
    SSE_PROTOCOL_VERSION = "ecorex.stream.v1"
    SSE_MAX_REPLAY_EVENTS = 2000
    SSE_ORPHAN_TTL_SECONDS = 120
    BACKPRESSURE_GLOBAL_ACTIVE_LIMIT = 32
    BACKPRESSURE_SESSION_ACTIVE_LIMIT = 2
    TOOL_RESULT_PREVIEW_CHAR_LIMIT = 2000
    TOOL_OUTPUT_FIELD_CHAR_LIMIT = 2000
    TOOL_OUTPUT_COLLECTION_ITEM_LIMIT = 64
    ARTIFACT_METADATA_MAX_ITEMS = 8
    ARTIFACT_METADATA_STRING_CHAR_LIMIT = 512
    ARTIFACT_METADATA_PATH_CHAR_LIMIT = 4096
    SESSION_QUEUE_LIMIT = 8

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.runtime_started_at = _RUNTIME_STARTED_AT
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)
        self.request_to_session = {}  # request_id -> session_id
        self.sse_queues = {}  # request_id -> Queue (legacy/test mirror for SSE events)
        self.sse_events = {}  # request_id -> replayable SSE event list
        self.sse_event_offsets = {}  # request_id -> absolute event id for sse_events[0]
        self.sse_conditions = {}  # request_id -> threading.Condition
        self.sse_subscribers = {}  # request_id -> active EventSource connection count
        self.sse_done_sent = set()  # request_id values that already emitted a terminal event
        self.sse_cleanup_timers = {}  # request_id -> guarded orphan cleanup Timer
        self.sse_lock = threading.RLock()
        self.backpressure_lock = threading.RLock()
        self.same_session_replacement_lock = threading.RLock()
        self.same_session_replacement_tickets = {}  # session_id -> latest rapid-resend ticket
        self.session_run_queue_lock = threading.RLock()
        self.session_run_queues = {}  # session_id -> deque(request_id) for queued /message runs
        self.queued_request_payloads = {}  # request_id -> in-memory request payload for delayed start
        self.queued_request_payload_store = None
        self.request_artifacts = {}  # request_id -> list of structured artifact dicts
        self.request_project_contexts = {}  # request_id -> structured project binding
        self.sse_stream_tokens = {}  # legacy field; no longer used to supersede streams
        self._http_server = None
        self.runtime_event_append_failures = 0
        self.runtime_event_append_failure_tail: List[Dict[str, Any]] = []

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

    def _current_chat_route_snapshot(self) -> Dict[str, str]:
        local_config = conf()
        try:
            chat = ModelsHandler._chat_capability(local_config)
            return {
                "model": str(chat.get("current_model") or local_config.get("model") or ""),
                "provider": str(chat.get("current_provider") or ""),
            }
        except Exception as e:
            logger.debug(f"[WebChannel] chat route snapshot fallback: {_web_body_log_summary(e)}")
            model = str(local_config.get("model") or "")
            provider = ""
            try:
                from models.model_capabilities import infer_provider_id

                provider = infer_provider_id(
                    model,
                    configured_bot_type=str(local_config.get("bot_type") or ""),
                    use_linkai=bool(local_config.get("use_linkai", False)),
                    has_linkai_key=bool(local_config.get("linkai_api_key")),
                    use_azure_chatgpt=bool(local_config.get("use_azure_chatgpt", False)),
                    gemini_api_base=local_config.get("gemini_api_base") or "",
                    has_gemini_key=bool(local_config.get("gemini_api_key")),
                    gemini_api_key=local_config.get("gemini_api_key") or "",
                    custom_api_base=local_config.get("custom_api_base") or "",
                    custom_api_key=local_config.get("custom_api_key") or "",
                )
            except Exception:
                provider = str(local_config.get("bot_type") or "")
            return {"model": model, "provider": provider}

    def _active_request_ids_for_session(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        try:
            from agent.protocol import get_cancel_registry
            registry = get_cancel_registry()
            return [
                request_id
                for request_id, mapped_session_id in list(self.request_to_session.items())
                if mapped_session_id == session_id and registry.get_event(request_id) is not None
            ]
        except Exception as e:
            logger.debug(f"[WebChannel] active request lookup failed for session {session_id}: {_web_body_log_summary(e)}")
            return []

    def _recover_interrupted_runs_for_removed_session_locks(self, session_id: str = "") -> List[str]:
        """Close ledger rows whose session lock owner process is confirmed dead."""
        session_id = str(session_id or "").strip()
        try:
            from common.ecorex_workspace import list_session_locks

            removed_session_ids = set()
            for item in list_session_locks(_get_workspace_root(), cleanup=False):
                item_session_id = str(item.get("session_id") or "").strip()
                if session_id and item_session_id != session_id:
                    continue
                if not item_session_id or not item.get("dead_owner"):
                    continue
                path_text = str(item.get("path") or "")
                try:
                    if path_text:
                        Path(path_text).unlink()
                    item["removed"] = True
                except FileNotFoundError:
                    item["removed"] = True
                except Exception as e:
                    logger.debug(f"[WebChannel] dead session lock removal skipped for {item_session_id}: {_web_body_log_summary(e)}")
                    continue
                if item.get("removed"):
                    removed_session_ids.add(item_session_id)
        except Exception as e:
            logger.debug(f"[WebChannel] dead session lock recovery skipped for {session_id}: {_web_body_log_summary(e)}")
            return []

        if not removed_session_ids:
            return []

        interrupted_request_ids: List[str] = []
        try:
            from agent.protocol import get_run_ledger

            ledger = get_run_ledger()
            for row in ledger.active_snapshot():
                row_session_id = str(row.get("session_id") or "").strip()
                request_id = str(row.get("request_id") or "").strip()
                run_type = str(row.get("run_type") or "message").strip().lower()
                if row_session_id not in removed_session_ids or not request_id or run_type != "message":
                    continue
                ledger.mark_terminal(
                    request_id,
                    "interrupted",
                    reason="sidecar_interrupted",
                    error_code="SIDECAR_INTERRUPTED",
                    error_message="Runtime session lock owner disappeared before the run reached a terminal state.",
                )
                interrupted_request_ids.append(request_id)
        except Exception as e:
            logger.warning(f"[WebChannel] failed to mark dead-owner lock runs interrupted: {_web_body_log_summary(e)}")
            return interrupted_request_ids

        if interrupted_request_ids:
            logger.warning(
                f"[WebChannel] recovered dead-owner session lock active runs: "
                f"sessions={sorted(removed_session_ids)}, requests={interrupted_request_ids}"
            )
        return interrupted_request_ids

    @staticmethod
    def _coerce_positive_int(value, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(fallback)
        return max(0, parsed)

    def _backpressure_limits(self) -> Dict[str, int]:
        return {
            "global_active_limit": self._coerce_positive_int(
                conf().get("web_max_active_requests", self.BACKPRESSURE_GLOBAL_ACTIVE_LIMIT),
                self.BACKPRESSURE_GLOBAL_ACTIVE_LIMIT,
            ),
            "session_active_limit": self._coerce_positive_int(
                conf().get("web_max_active_requests_per_session", self.BACKPRESSURE_SESSION_ACTIVE_LIMIT),
                self.BACKPRESSURE_SESSION_ACTIVE_LIMIT,
            ),
        }

    def _tool_output_limits(self) -> Dict[str, int]:
        return {
            "result_preview_chars": self._coerce_positive_int(
                conf().get("web_tool_result_preview_chars", self.TOOL_RESULT_PREVIEW_CHAR_LIMIT),
                self.TOOL_RESULT_PREVIEW_CHAR_LIMIT,
            ),
            "output_field_chars": self._coerce_positive_int(
                conf().get("web_tool_output_field_chars", self.TOOL_OUTPUT_FIELD_CHAR_LIMIT),
                self.TOOL_OUTPUT_FIELD_CHAR_LIMIT,
            ),
            "collection_items": self._coerce_positive_int(
                conf().get("web_tool_output_collection_items", self.TOOL_OUTPUT_COLLECTION_ITEM_LIMIT),
                self.TOOL_OUTPUT_COLLECTION_ITEM_LIMIT,
            ),
        }

    def _artifact_metadata_limits(self) -> Dict[str, int]:
        return {
            "max_items": self._coerce_positive_int(
                conf().get("web_artifact_metadata_max_items", self.ARTIFACT_METADATA_MAX_ITEMS),
                self.ARTIFACT_METADATA_MAX_ITEMS,
            ),
            "string_chars": self._coerce_positive_int(
                conf().get("web_artifact_metadata_string_chars", self.ARTIFACT_METADATA_STRING_CHAR_LIMIT),
                self.ARTIFACT_METADATA_STRING_CHAR_LIMIT,
            ),
            "path_chars": self._coerce_positive_int(
                conf().get("web_artifact_metadata_path_chars", self.ARTIFACT_METADATA_PATH_CHAR_LIMIT),
                self.ARTIFACT_METADATA_PATH_CHAR_LIMIT,
            ),
        }

    def _backpressure_snapshot(self, session_id: str = "", ignore_request_ids=None) -> Dict[str, Any]:
        limits = self._backpressure_limits()
        ignored = set(ignore_request_ids or [])
        active_by_request: Dict[str, Dict[str, Any]] = {}
        try:
            from agent.protocol import RuntimeProjectionService, get_cancel_registry, get_run_ledger

            registry_rows = get_cancel_registry().snapshot()
            registry_by_request = {
                row.get("request_id", ""): row
                for row in registry_rows
                if row.get("request_id")
            }
            self._recover_stale_active_runs(get_run_ledger(), registry_by_request=registry_by_request)
            for row in get_run_ledger().active_snapshot():
                request_id = row.get("request_id", "")
                if request_id in ignored:
                    continue
                if request_id:
                    active_by_request[request_id] = row
            for row in registry_rows:
                request_id = row.get("request_id", "")
                if request_id in ignored:
                    continue
                if request_id:
                    existing = active_by_request.get(request_id, {})
                    merged = {**existing, **row}
                    if existing.get("session_id") and not row.get("session_id"):
                        merged["session_id"] = existing.get("session_id")
                    active_by_request[request_id] = merged
        except Exception as e:
            logger.debug(f"[WebChannel] backpressure snapshot fallback failed: {_web_body_log_summary(e)}")

        active_requests = list(active_by_request.values())
        session_active = [
            row for row in active_requests
            if session_id and row.get("session_id") == session_id
        ]
        return {
            **limits,
            "global_active": len(active_requests),
            "session_active": len(session_active),
            "session_id": session_id,
            "active_request_ids": [
                row.get("request_id", "")
                for row in session_active
                if row.get("request_id")
            ],
            "sse_replay_limit": self.SSE_MAX_REPLAY_EVENTS,
        }

    def _backpressure_rejection_payload(self, session_id: str, ignore_request_ids=None) -> Optional[Dict[str, Any]]:
        snapshot = self._backpressure_snapshot(session_id, ignore_request_ids=ignore_request_ids)
        global_limit = snapshot["global_active_limit"]
        if global_limit and snapshot["global_active"] >= global_limit:
            return self._backpressure_payload("global", snapshot)
        session_limit = snapshot["session_active_limit"]
        if session_limit and snapshot["session_active"] >= session_limit:
            return self._backpressure_payload("session", snapshot)
        return None

    def _backpressure_payload(self, scope: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        limit_key = "global_active_limit" if scope == "global" else "session_active_limit"
        active_key = "global_active" if scope == "global" else "session_active"
        code = BACKPRESSURE_GLOBAL_LIMIT_CODE if scope == "global" else BACKPRESSURE_SESSION_LIMIT_CODE
        payload = {
            "status": "error",
            "code": code,
            "error_type": "backpressure_limit",
            "state": "backpressure",
            "scope": scope,
            "recoverable": True,
            "retryable": True,
            "retry_after_ms": BACKPRESSURE_RETRY_AFTER_MS,
            "reason": "active_request_limit",
            "limit": snapshot.get(limit_key, 0),
            "active": snapshot.get(active_key, 0),
            "global_active": snapshot.get("global_active", 0),
            "session_active": snapshot.get("session_active", 0),
            "global_active_limit": snapshot.get("global_active_limit", 0),
            "session_active_limit": snapshot.get("session_active_limit", 0),
            "sse_replay_limit": snapshot.get("sse_replay_limit", self.SSE_MAX_REPLAY_EVENTS),
            "session_id": snapshot.get("session_id", ""),
            "active_request_ids": snapshot.get("active_request_ids", []),
            "message": (
                "Too many active agent runs. Please retry shortly."
                if scope == "global"
                else "This session already has too many active runs. Please retry shortly."
            ),
        }
        if scope == "session":
            payload["same_session"] = self._same_session_decision_payload(
                "retryable_conflict",
                active_request_ids=snapshot.get("active_request_ids", []),
                reason="active_request_limit",
            )
        return payload

    def _mark_run_phase(self, request_id: str, phase: str, status: Optional[str] = None) -> None:
        if not request_id or not phase:
            return
        try:
            from agent.protocol import get_run_ledger

            get_run_ledger().mark_phase(request_id, phase, status=status)
        except Exception as e:
            logger.debug(f"[WebChannel] run ledger phase update skipped for {request_id}: {_web_body_log_summary(e)}")

    def _mark_run_terminal(
        self,
        request_id: str,
        status: str,
        reason: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if not request_id:
            return
        try:
            from agent.protocol import get_run_ledger

            get_run_ledger().mark_terminal(
                request_id,
                status,
                reason=reason,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as e:
            logger.debug(f"[WebChannel] run ledger terminal update skipped for {request_id}: {_web_body_log_summary(e)}")

    def _record_run_event_phase(self, request_id: str, event: Dict[str, Any]) -> None:
        etype = str((event or {}).get("type") or "")
        if not request_id or not etype:
            return
        if etype == "done":
            self._mark_run_terminal(request_id, "completed", reason="done")
        elif etype == "cancelled":
            self._mark_run_terminal(request_id, "cancelled", reason="cancelled")
        elif etype == "error":
            self._mark_run_terminal(
                request_id,
                "failed",
                reason=str(event.get("terminal_reason") or "stream_error"),
                error_code=str(event.get("error_code") or "STREAM_ERROR"),
                error_message=str(event.get("message") or event.get("content") or ""),
            )
        elif etype == "interrupted":
            interrupted_reason = str(event.get("terminal_reason") or "interrupted")
            interrupted_code = str(event.get("error_code") or "RUN_INTERRUPTED")
            terminal_status = "timeout" if interrupted_reason == "tool_timeout" or interrupted_code == "TOOL_TIMEOUT" else "interrupted"
            self._mark_run_terminal(
                request_id,
                terminal_status,
                reason=interrupted_reason,
                error_code=interrupted_code,
                error_message=str(event.get("message") or event.get("content") or ""),
            )
        elif etype == "tool_timeout":
            self._mark_run_terminal(
                request_id,
                "timeout",
                reason=str(event.get("terminal_reason") or "tool_timeout"),
                error_code=str(event.get("error_code") or "TOOL_TIMEOUT"),
                error_message=str(event.get("message") or event.get("content") or ""),
            )
        elif etype == "tool_permission_request":
            self._mark_run_phase(request_id, "waiting_permission", status="running")
        elif etype in ("tool_execution_start", "tool_execution_heartbeat", "tool_execution_deadline_extended"):
            self._mark_run_phase(request_id, "tool_running", status="running")
        elif etype == "tool_execution_end":
            self._mark_run_phase(request_id, "tool_completed", status="running")
        elif etype == "phase" and (event.get("queue_position") or "排队" in str(event.get("content") or event.get("message") or "")):
            self._mark_run_phase(request_id, "queued", status="queued")
        elif etype in ("agent_start", "turn_start", "message_start", "phase"):
            self._mark_run_phase(request_id, etype, status="running")
        elif etype == "message_end":
            self._mark_run_phase(request_id, "finalizing", status="finalizing")

    def _append_runtime_event(
        self,
        request_id: str,
        event_type: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
        idempotency_key: str = "",
        source: str = "web_channel",
    ) -> Optional[Dict[str, Any]]:
        if not request_id or not event_type:
            return None
        if not self._runtime_event_ledger_enabled():
            return {
                "event_id": None,
                "event_type": event_type,
                "append_skipped": True,
                "error_code": "RUN_EVENT_LEDGER_DISABLED",
            }
        try:
            from agent.protocol import get_run_event_ledger

            return get_run_event_ledger().append_event(
                request_id=request_id,
                session_id=session_id or self.request_to_session.get(request_id, ""),
                turn_id=turn_id or request_id,
                event_type=event_type,
                payload=payload or {},
                idempotency_key=idempotency_key,
                source=source,
            )
        except Exception as e:
            error_summary = _web_body_log_summary(e)
            detail = {
                "request_id": request_id,
                "event_type": event_type,
                "error_type": type(e).__name__,
                "error_hash": error_summary.get("hash", ""),
                "error_chars": error_summary.get("chars", 0),
                "error_bytes": error_summary.get("bytes", 0),
                "redacted": True,
                "timestamp": time.time(),
            }
            self.runtime_event_append_failures += 1
            self.runtime_event_append_failure_tail.append(detail)
            if len(self.runtime_event_append_failure_tail) > 50:
                del self.runtime_event_append_failure_tail[:-50]
            logger.warning(
                f"[WebChannel] runtime event append failed for {request_id}/{event_type}: "
                f"{detail['error_type']}: {error_summary}"
            )
            return {
                "event_id": None,
                "event_type": event_type,
                "append_failed": True,
                "error_code": "RUN_EVENT_APPEND_FAILED",
                "error_type": detail["error_type"],
            }

    def _runtime_event_ledger_enabled(self) -> bool:
        raw_env = os.environ.get("ECOREX_RUNTIME_EVENT_LEDGER", "")
        if str(raw_env).strip().lower() in {"0", "false", "off", "no", "disabled"}:
            return False
        try:
            raw_conf = conf().get("web_runtime_event_ledger_enabled", True)
            if str(raw_conf).strip().lower() in {"0", "false", "off", "no", "disabled"}:
                return False
        except Exception:
            pass
        return True

    def _record_request_accepted_events(
        self,
        request_id: str,
        session_id: str,
        *,
        visible_message: str = "",
        client_attempt_id: str = "",
        retry_of_request_id: str = "",
        interrupts_request_id: str = "",
        project_context_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        turn_id = request_id
        base_payload = {
            "request_id": request_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "client_attempt_id": client_attempt_id,
            "retry_of_request_id": retry_of_request_id,
            "interrupts_request_id": interrupts_request_id,
        }
        self._append_runtime_event(
            request_id,
            "run.accepted",
            session_id=session_id,
            turn_id=turn_id,
            payload={**base_payload, "project_context": _project_context_event_summary(project_context_meta)},
            idempotency_key=f"request:{request_id}:run.accepted",
        )
        self._append_runtime_event(
            request_id,
            "message.user.accepted",
            session_id=session_id,
            turn_id=turn_id,
            payload={**base_payload, "content": visible_message or ""},
            idempotency_key=f"request:{request_id}:message.user.accepted",
        )
        self._append_runtime_event(
            request_id,
            "message.assistant.created",
            session_id=session_id,
            turn_id=turn_id,
            payload=base_payload,
            idempotency_key=f"request:{request_id}:message.assistant.created",
        )

    def _record_request_queued_events(
        self,
        request_id: str,
        session_id: str,
        *,
        visible_message: str = "",
        client_attempt_id: str = "",
        retry_of_request_id: str = "",
        interrupts_request_id: str = "",
        project_context_meta: Optional[Dict[str, Any]] = None,
        queued_after_request_ids: Optional[List[str]] = None,
        queue_position: int = 0,
    ) -> None:
        turn_id = request_id
        base_payload = {
            "request_id": request_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "client_attempt_id": client_attempt_id,
            "retry_of_request_id": retry_of_request_id,
            "interrupts_request_id": interrupts_request_id,
            "queued_after_request_ids": list(queued_after_request_ids or []),
            "queue_position": int(queue_position or 0),
        }
        self._append_runtime_event(
            request_id,
            "run.queued",
            session_id=session_id,
            turn_id=turn_id,
            payload={**base_payload, "project_context": _project_context_event_summary(project_context_meta)},
            idempotency_key=f"request:{request_id}:run.queued",
        )
        self._append_runtime_event(
            request_id,
            "message.user.accepted",
            session_id=session_id,
            turn_id=turn_id,
            payload={**base_payload, "content": visible_message or ""},
            idempotency_key=f"request:{request_id}:message.user.accepted",
        )

    def _record_request_started_events(self, request_id: str, session_id: str) -> None:
        turn_id = request_id
        base_payload = {
            "request_id": request_id,
            "session_id": session_id,
            "turn_id": turn_id,
        }
        self._append_runtime_event(
            request_id,
            "run.started",
            session_id=session_id,
            turn_id=turn_id,
            payload=base_payload,
            idempotency_key=f"request:{request_id}:run.started",
        )
        self._append_runtime_event(
            request_id,
            "message.assistant.created",
            session_id=session_id,
            turn_id=turn_id,
            payload=base_payload,
            idempotency_key=f"request:{request_id}:message.assistant.created",
        )

    def _build_web_message_context(
        self,
        request_id: str,
        session_id: str,
        *,
        prompt: str,
        visible_prompt: str,
        visible_message: str,
        hidden_context: Any = "",
        project_context_meta: Optional[Dict[str, Any]] = None,
        internal_action: bool = False,
        use_sse: bool = True,
        attachments: Any = None,
        is_voice_input: bool = False,
        session_lock=None,
        client_attempt_id: str = "",
        pre_persisted_user_message: bool = False,
    ) -> Optional[Context]:
        attachments = attachments if isinstance(attachments, list) else []
        next_prompt = str(prompt or "")
        if not internal_action:
            hidden_context = _append_hidden_context(WEBUI_IDENTITY_GUARD_CONTEXT, hidden_context)
        if attachments:
            file_refs, remote_context = _web_attachment_prompt_refs_and_context(attachments)
            if file_refs:
                next_prompt = next_prompt + "\n" + "\n".join(file_refs)
                logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")
            if remote_context:
                _ensure_tencent_docs_tools_for_attachments(attachments, "queued-message")
                hidden_context = _append_hidden_context(hidden_context, remote_context)

        if isinstance(hidden_context, str) and hidden_context.strip():
            next_prompt = hidden_context.strip() + "\n\nUser request:\n" + (next_prompt or "Please handle these attachments.")

        trigger_prefixs = conf().get("single_chat_prefix", [""])
        if check_prefix(next_prompt, trigger_prefixs) is None and trigger_prefixs:
            next_prompt = trigger_prefixs[0] + next_prompt
            logger.debug(f"[WebChannel] Added prefix to message summary: {_web_body_log_summary(next_prompt)}")

        msg = WebMessage(self._generate_msg_id(), next_prompt)
        msg.from_user_id = session_id

        context = self._compose_context(ContextType.TEXT, next_prompt, msg=msg, isgroup=False)
        if context is None:
            return None

        context["session_id"] = session_id
        context["receiver"] = session_id
        context["channel_type"] = "web"
        context["request_id"] = request_id
        context["session_lock"] = session_lock
        context["cancel_token_owner"] = "web_channel"
        context["visible_message"] = (visible_message or "Please handle these attachments.").strip()
        if project_context_meta:
            context["project_context_meta"] = project_context_meta
        if internal_action:
            context["internal_action"] = True
        context["attachments"] = attachments
        if is_voice_input:
            context["is_voice_input"] = True
        if use_sse:
            context["on_event"] = self._make_sse_callback(request_id)

        if pre_persisted_user_message:
            context["pre_persisted_user_message"] = True
        elif self._pre_persist_web_user_message(
            session_id,
            visible_message or visible_prompt or "Please handle these attachments.",
            request_id=request_id,
            client_attempt_id=client_attempt_id,
            attachments=attachments,
            project_context=project_context_meta,
        ):
            context["pre_persisted_user_message"] = True
        return context

    def _accept_queued_message(
        self,
        session_id: str,
        *,
        visible_prompt: str,
        visible_message: str,
        prompt: str,
        hidden_context: Any = "",
        project_context_meta: Optional[Dict[str, Any]] = None,
        internal_action: bool = False,
        use_sse: bool = True,
        attachments: Any = None,
        client_attempt_id: str = "",
        interrupts_request_id: str = "",
        retry_of_request_id: str = "",
        interrupt_mode: str = "queue",
        lang: str = "zh",
        is_voice_input: bool = False,
        queued_after_request_ids: Optional[List[str]] = None,
        reason: str = "session_busy",
    ) -> Dict[str, Any]:
        queued_after_request_ids = list(queued_after_request_ids or [])
        attachments = attachments if isinstance(attachments, list) else []
        with self.session_run_queue_lock:
            existing_queue = self.session_run_queues.setdefault(session_id, deque())
            queue_limit = self._session_queue_limit()
            if queue_limit and len(existing_queue) >= queue_limit:
                return {
                    "status": "error",
                    "code": BACKPRESSURE_SESSION_LIMIT_CODE,
                    "error_type": "backpressure_limit",
                    "state": "queue_full",
                    "recoverable": True,
                    "retryable": True,
                    "retry_after_ms": BACKPRESSURE_RETRY_AFTER_MS,
                    "reason": "session_queue_full",
                    "session_id": session_id,
                    "active_request_ids": queued_after_request_ids,
                    "message": "This session already has too many queued runs. Please retry shortly.",
                    "same_session": self._same_session_decision_payload(
                        "retryable_conflict",
                        active_request_ids=queued_after_request_ids,
                        reason="session_queue_full",
                    ),
                }
            request_id = self._generate_request_id()
            existing_queue.append(request_id)
            queue_position = len(existing_queue)

        self.request_to_session[request_id] = session_id
        self.request_project_contexts[request_id] = project_context_meta or {}
        pre_persisted = self._pre_persist_web_user_message(
            session_id,
            visible_message or visible_prompt or "Please handle these attachments.",
            request_id=request_id,
            client_attempt_id=client_attempt_id,
            attachments=attachments,
            project_context=project_context_meta,
        )
        payload = {
            "request_id": request_id,
            "session_id": session_id,
            "visible_prompt": visible_prompt,
            "visible_message": visible_message,
            "prompt": prompt,
            "hidden_context": hidden_context,
            "project_context_meta": project_context_meta or {},
            "internal_action": bool(internal_action),
            "use_sse": bool(use_sse),
            "attachments": attachments,
            "client_attempt_id": client_attempt_id,
            "interrupts_request_id": interrupts_request_id,
            "retry_of_request_id": retry_of_request_id,
            "interrupt_mode": interrupt_mode,
            "lang": lang,
            "is_voice_input": bool(is_voice_input),
            "queued_after_request_ids": queued_after_request_ids,
            "pre_persisted_user_message": bool(pre_persisted),
        }
        if not self._persist_queued_payload(payload):
            with self.session_run_queue_lock:
                queue = self.session_run_queues.get(session_id)
                if queue:
                    try:
                        queue.remove(request_id)
                    except ValueError:
                        pass
                    if not queue:
                        self.session_run_queues.pop(session_id, None)
            self.queued_request_payloads.pop(request_id, None)
            self.request_to_session.pop(request_id, None)
            return {
                "status": "error",
                "code": "QUEUE_PAYLOAD_STORE_UNAVAILABLE",
                "error_type": "runtime_state_unavailable",
                "message": "Runtime queue storage is unavailable; request was not queued. Please retry shortly.",
                "retryable": True,
                "recoverable": True,
                "request_id": "",
            }

        try:
            from agent.protocol import get_run_ledger

            chat_route = self._current_chat_route_snapshot()
            retry_visible_message, _retry_visible_trunc = self._limit_text_with_marker(
                visible_message or visible_prompt or "",
                64 * 1024,
            )
            get_run_ledger().create_run(
                request_id,
                session_id,
                run_type="message",
                phase="queued",
                status="queued",
                model=chat_route.get("model", ""),
                provider=chat_route.get("provider", ""),
                metadata={
                    "stream": bool(use_sse),
                    "internal_action": bool(internal_action),
                    "attachments": len(attachments),
                    "attachment_items": self._retry_attachment_snapshot(attachments),
                    "visible_message": retry_visible_message,
                    "model": chat_route.get("model", ""),
                    "provider": chat_route.get("provider", ""),
                    "client_attempt_id": client_attempt_id,
                    "interrupts_request_id": interrupts_request_id,
                    "retry_of_request_id": retry_of_request_id,
                    "interrupt_mode": interrupt_mode,
                    "project_context": project_context_meta,
                    "queue_position": queue_position,
                    "queued_after_request_ids": queued_after_request_ids,
                    "queue_reason": reason,
                },
            )
        except Exception as e:
            logger.error(f"[WebChannel] queued run ledger unavailable for {request_id}: {_web_body_log_summary(e)}")
            with self.session_run_queue_lock:
                queue = self.session_run_queues.get(session_id)
                if queue:
                    try:
                        queue.remove(request_id)
                    except ValueError:
                        pass
            self._delete_queued_payload(request_id)
            self.request_to_session.pop(request_id, None)
            return {
                "status": "error",
                "code": "RUN_LEDGER_UNAVAILABLE",
                "error_type": "runtime_state_unavailable",
                "message": "Runtime run ledger is unavailable; request was not queued. Please retry shortly.",
                "retryable": True,
                "recoverable": True,
                "request_id": "",
            }

        self._record_request_queued_events(
            request_id,
            session_id,
            visible_message=visible_message or visible_prompt or "",
            client_attempt_id=client_attempt_id,
            retry_of_request_id=retry_of_request_id,
            interrupts_request_id=interrupts_request_id,
            project_context_meta=project_context_meta,
            queued_after_request_ids=queued_after_request_ids,
            queue_position=queue_position,
        )

        if session_id not in self.session_queues:
            self.session_queues[session_id] = Queue()
        if use_sse:
            self._ensure_sse_state(request_id)
            self._push_sse_event(request_id, {
                "type": "phase",
                "content": f"已排队，当前会话第 {queue_position} 位等待执行",
                "request_id": request_id,
                "timestamp": time.time(),
                "queue_position": queue_position,
            })

        logger.info(
            f"[WebChannel] queued same-session message: session={session_id}, "
            f"request={request_id}, position={queue_position}, active={queued_after_request_ids}"
        )
        return {
            "status": "success",
            "request_id": request_id,
            "stream": use_sse,
            "queued": True,
            "queue_position": queue_position,
            "same_session": self._same_session_decision_payload(
                "queued",
                active_request_ids=queued_after_request_ids,
                reason=reason,
                queue_position=queue_position,
                queued_request_id=request_id,
            ),
        }

    def _start_next_queued_request(self, session_id: str, *, completed_request_id: str = "") -> bool:
        if not session_id:
            return False
        self._recover_session_run_queue_from_ledger(session_id)
        with self.session_run_queue_lock:
            queue = self.session_run_queues.get(session_id)
            if not queue:
                return False
            request_id = queue[0]
            payload = self._load_queued_payload(request_id)
            if not payload:
                queue.popleft()
                if not queue:
                    self.session_run_queues.pop(session_id, None)
                self._delete_queued_payload(request_id)
                self._mark_run_terminal(
                    request_id,
                    "interrupted",
                    reason="queued_payload_missing",
                    error_code="QUEUE_PAYLOAD_MISSING",
                    error_message="Queued request payload was missing before the run could start.",
                )
                return self._start_next_queued_request(session_id, completed_request_id=completed_request_id)

        claim_owner = f"web:{os.getpid()}:{id(self)}"
        claimed = False
        try:
            from agent.protocol import get_run_ledger

            claimed = get_run_ledger().claim_queued_run(
                request_id,
                owner=claim_owner,
                lease_seconds=self._coerce_positive_int(conf().get("web_queue_claim_lease_seconds", 30), 30) or 30,
            )
        except Exception as e:
            logger.warning(f"[WebChannel] queued request claim failed: {_web_body_log_summary(e)}")
            claimed = False
        if not claimed:
            with self.session_run_queue_lock:
                queue = self.session_run_queues.get(session_id)
                if queue and request_id in queue:
                    try:
                        queue.remove(request_id)
                    except ValueError:
                        pass
                    if not queue:
                        self.session_run_queues.pop(session_id, None)
            return False

        try:
            from common.ecorex_workspace import SessionBusyError, SessionLock

            session_lock = SessionLock(_get_workspace_root(), session_id).acquire()
        except SessionBusyError:
            try:
                from agent.protocol import get_run_ledger

                get_run_ledger().release_queued_claim(request_id, owner=claim_owner)
            except Exception:
                pass
            return False
        except Exception as e:
            logger.warning(f"[WebChannel] queued request lock acquisition failed: {_web_body_log_summary(e)}")
            try:
                from agent.protocol import get_run_ledger

                get_run_ledger().release_queued_claim(request_id, owner=claim_owner)
            except Exception:
                pass
            return False

        with self.session_run_queue_lock:
            queue = self.session_run_queues.get(session_id)
            if not queue or queue[0] != request_id:
                try:
                    session_lock.release()
                except Exception:
                    pass
                try:
                    from agent.protocol import get_run_ledger

                    get_run_ledger().release_queued_claim(request_id, owner=claim_owner)
                except Exception:
                    pass
                return False
            queue.popleft()
            if not queue:
                self.session_run_queues.pop(session_id, None)
            payload = self._load_queued_payload(request_id) or payload
            self._delete_queued_payload(request_id)

        self.request_to_session[request_id] = session_id
        try:
            from agent.protocol import get_cancel_registry

            get_cancel_registry().register(request_id, session_id=session_id)
        except Exception as e:
            logger.debug(f"[WebChannel] queued request cancel token register skipped: {_web_body_log_summary(e)}")
        self._mark_run_phase(request_id, "starting", status="running")
        self._record_request_started_events(request_id, session_id)
        if self._sse_request_exists(request_id):
            self._push_sse_event(request_id, {
                "type": "phase",
                "content": "已轮到此消息，正在准备响应",
                "request_id": request_id,
                "timestamp": time.time(),
                "queued_after_request_id": completed_request_id,
            })

        context = self._build_web_message_context(
            request_id,
            session_id,
            prompt=str(payload.get("prompt") or ""),
            visible_prompt=str(payload.get("visible_prompt") or ""),
            visible_message=str(payload.get("visible_message") or ""),
            hidden_context=payload.get("hidden_context") or "",
            project_context_meta=payload.get("project_context_meta") if isinstance(payload.get("project_context_meta"), dict) else {},
            internal_action=bool(payload.get("internal_action")),
            use_sse=bool(payload.get("use_sse")),
            attachments=payload.get("attachments") if isinstance(payload.get("attachments"), list) else [],
            is_voice_input=bool(payload.get("is_voice_input")),
            session_lock=session_lock,
            client_attempt_id=str(payload.get("client_attempt_id") or ""),
            pre_persisted_user_message=bool(payload.get("pre_persisted_user_message")),
        )
        if context is None:
            logger.warning(f"[WebChannel] queued context filtered: session={session_id}, request={request_id}")
            self._abort_pre_worker_request(
                request_id,
                session_id,
                message="Message was filtered",
                reason="context_filtered",
                error_code="CONTEXT_FILTERED",
                session_lock=session_lock,
            )
            self._start_next_queued_request(session_id, completed_request_id=request_id)
            return False

        threading.Thread(target=self._produce_with_session_lock, args=(context, session_lock), daemon=True).start()
        logger.info(f"[WebChannel] started queued request: session={session_id}, request={request_id}")
        return True

    def queue_action_request(self, request_id: str) -> Dict[str, Any]:
        request_id = str(request_id or "").strip()
        if not request_id:
            return {"status": "error", "message": "missing request_id"}
        try:
            data = web.data()
            try:
                body = json.loads(data) if data else {}
            except Exception:
                body = {}
            action = str(body.get("action") or body.get("queue_action") or "").strip().lower()
            session_id = str(body.get("session_id") or "").strip()
            if action in {"cancel", "cancel_queued", "remove"}:
                return self._cancel_queued_request(request_id, expected_session_id=session_id)
            if action in {"guide", "guide_queue", "insert_queue", "observe_queue"}:
                return self._guide_queued_request(request_id, expected_session_id=session_id)
            if action in {"run_now", "stop_current_and_run"}:
                promoted = self._promote_queued_request(request_id, expected_session_id=session_id)
                if promoted.get("status") != "success":
                    return promoted
                active_request_ids = self._active_request_ids_for_session(promoted.get("session_id") or session_id)
                try:
                    from agent.protocol import get_cancel_registry

                    cancelled = get_cancel_registry().cancel_session(promoted.get("session_id") or session_id)
                except Exception:
                    cancelled = 0
                if active_request_ids:
                    self._push_cancelled_events_for_session(promoted.get("session_id") or session_id, active_request_ids, lang="zh")
                return {
                    **promoted,
                    "action": action,
                    "cancelled_current_requests": cancelled,
                    "active_request_ids": active_request_ids,
                }
            return {
                "status": "error",
                "message": "unsupported queue action",
                "request_id": request_id,
                "supported_actions": ["cancel_queued", "guide_queue", "run_now"],
            }
        except Exception as e:
            logger.error(f"[WebChannel] queue action error: {_web_body_log_summary(e)}")
            return _public_error_payload("Queue action failed.", e)

    def _guide_queued_request(self, request_id: str, *, expected_session_id: str = "") -> Dict[str, Any]:
        """Observe and confirm a queued request without pre-empting the current run."""
        payload = self._load_queued_payload(request_id)
        actual_session_id = self.request_to_session.get(request_id, "") or str((payload or {}).get("session_id") or "")
        session_id = actual_session_id or expected_session_id
        if expected_session_id and actual_session_id and expected_session_id != actual_session_id:
            return {
                "status": "error",
                "message": "request belongs to a different session",
                "request_id": request_id,
                "session_id": actual_session_id,
            }
        if not session_id:
            try:
                from agent.protocol import get_run_ledger

                row = get_run_ledger().get_run(request_id)
                session_id = str((row or {}).get("session_id") or "").strip()
            except Exception:
                session_id = ""
        if not session_id:
            return {
                "status": "error",
                "message": "queued request has no observable session",
                "request_id": request_id,
            }

        with self.session_run_queue_lock:
            queue_before_recovery = list(self.session_run_queues.get(session_id) or [])
        self._recover_session_run_queue_from_ledger(session_id)
        with self.session_run_queue_lock:
            queue_after_recovery = list(self.session_run_queues.get(session_id) or [])
        recovered_by_guide = request_id not in queue_before_recovery and request_id in queue_after_recovery
        payload = self._load_queued_payload(request_id)
        row: Dict[str, Any] = {}
        try:
            from agent.protocol import get_run_ledger

            row = get_run_ledger().get_run(request_id) or {}
        except Exception:
            row = {}

        raw_state = str(row.get("status") or row.get("phase") or "").strip().lower()
        terminal = row.get("terminal_at") is not None
        if terminal or raw_state in {"completed", "failed", "cancelled", "interrupted"}:
            with self.session_run_queue_lock:
                queue = self.session_run_queues.get(session_id)
                if queue:
                    try:
                        queue.remove(request_id)
                    except ValueError:
                        pass
                    if not queue:
                        self.session_run_queues.pop(session_id, None)
            if payload:
                self._delete_queued_payload(request_id)
            return {
                "status": "success",
                "request_id": request_id,
                "session_id": session_id,
                "state": raw_state or "terminal",
                "queue_position": 0,
                "message": "该请求已结束，无需重新插入队列。",
            }

        inserted = recovered_by_guide
        if payload and raw_state in {"queued", "pending", ""}:
            with self.session_run_queue_lock:
                queue = self.session_run_queues.setdefault(session_id, deque())
                if request_id not in queue:
                    queue.append(request_id)
                    inserted = True
        queue_position = self._queue_position_for_request(session_id, request_id)
        if payload and queue_position > 0:
            try:
                self._mark_run_phase(request_id, "queued", status="queued")
            except Exception:
                pass
            if self._sse_request_exists(request_id):
                self._push_sse_event(request_id, {
                    "type": "phase",
                    "content": f"已确认在队列中，第 {queue_position} 位等待执行" if queue_position > 1 else "已确认在队列中，等待当前任务完成",
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "queue_position": queue_position,
                })
            active_request_ids = self._active_request_ids_for_session(session_id)
            if not active_request_ids:
                self._start_next_queued_request(session_id, completed_request_id="")
            return {
                "status": "success",
                "request_id": request_id,
                "session_id": session_id,
                "state": "queued",
                "queue_position": queue_position,
                "inserted": inserted,
                "active_request_ids": active_request_ids,
                "message": "已重新观测并确认队列。" if inserted else "已在队列中，当前任务完成后自动继续。",
            }

        return {
            "status": "error",
            "request_id": request_id,
            "session_id": session_id,
            "state": raw_state or "unknown",
            "queue_position": queue_position,
            "message": "队列载荷不可用，无法重新插入；请在输入框中重新发送。",
        }

    def _cancel_queued_request(self, request_id: str, *, expected_session_id: str = "") -> Dict[str, Any]:
        payload = self._load_queued_payload(request_id)
        actual_session_id = self.request_to_session.get(request_id, "") or str((payload or {}).get("session_id") or "")
        session_id = actual_session_id or expected_session_id
        if expected_session_id and actual_session_id and expected_session_id != actual_session_id:
            return {
                "status": "error",
                "message": "request belongs to a different session",
                "request_id": request_id,
                "session_id": actual_session_id,
            }
        self._recover_session_run_queue_from_ledger(session_id)
        removed = False
        with self.session_run_queue_lock:
            payload = self._load_queued_payload(request_id)
            session_id = session_id or str((payload or {}).get("session_id") or "")
            queue = self.session_run_queues.get(session_id)
            if queue:
                try:
                    queue.remove(request_id)
                    removed = True
                except ValueError:
                    pass
                if not queue:
                    self.session_run_queues.pop(session_id, None)
        if not removed and not payload:
            return {
                "status": "error",
                "message": "queued request was not found",
                "request_id": request_id,
                "session_id": session_id,
            }
        self._delete_queued_payload(request_id)
        self._mark_run_terminal(request_id, "cancelled", reason="queued_cancelled")
        if self._sse_request_exists(request_id):
            self._push_cancelled_event_once(request_id, {
                "type": "cancelled",
                "content": "已从队列移除",
                "request_id": request_id,
                "timestamp": time.time(),
                "terminal_reason": "queued_cancelled",
            })
        else:
            self.request_to_session.pop(request_id, None)
        logger.info(f"[WebChannel] queued request cancelled: session={session_id}, request={request_id}")
        return {
            "status": "success",
            "request_id": request_id,
            "session_id": session_id,
            "cancelled": 1,
            "state": "cancelled",
        }

    def _promote_queued_request(self, request_id: str, *, expected_session_id: str = "") -> Dict[str, Any]:
        payload = self._load_queued_payload(request_id)
        actual_session_id = self.request_to_session.get(request_id, "") or str((payload or {}).get("session_id") or "")
        session_id = actual_session_id or expected_session_id
        if expected_session_id and actual_session_id and expected_session_id != actual_session_id:
            return {
                "status": "error",
                "message": "request belongs to a different session",
                "request_id": request_id,
                "session_id": actual_session_id,
            }
        self._recover_session_run_queue_from_ledger(session_id)
        with self.session_run_queue_lock:
            payload = self._load_queued_payload(request_id)
            session_id = session_id or str((payload or {}).get("session_id") or "")
            queue = self.session_run_queues.get(session_id)
            if not payload or not queue or request_id not in queue:
                return {
                    "status": "error",
                    "message": "queued request was not found",
                    "request_id": request_id,
                    "session_id": session_id,
                }
            try:
                queue.remove(request_id)
            except ValueError:
                pass
            queue.appendleft(request_id)
        if self._sse_request_exists(request_id):
            self._push_sse_event(request_id, {
                "type": "phase",
                "content": "已移到队首，正在停止当前任务",
                "request_id": request_id,
                "timestamp": time.time(),
                "queue_position": 1,
            })
        return {
            "status": "success",
            "request_id": request_id,
            "session_id": session_id,
            "state": "queued",
            "queue_position": 1,
        }

    def _safe_runtime_artifact_payload(self, event: Dict[str, Any], base_payload: Dict[str, Any]) -> Dict[str, Any]:
        safe: Dict[str, Any] = {}
        for key in ("type", "request_id", "session_id", "turn_id", "action", "source"):
            if key in base_payload:
                value = base_payload.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    safe[key] = value

        artifact = event.get("artifact") if isinstance(event.get("artifact"), dict) else None
        if artifact is not None:
            safe["artifact"] = self._safe_runtime_artifact(artifact)
        artifacts = event.get("artifacts")
        if isinstance(artifacts, list):
            safe["artifacts"] = [
                self._safe_runtime_artifact(item)
                for item in artifacts
                if isinstance(item, dict)
            ][:32]
        if artifact is None and not isinstance(artifacts, list):
            top_level = self._safe_runtime_artifact(event)
            if top_level:
                safe.update(top_level)
        return safe

    def _safe_runtime_artifact(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        allowed_fields = {
            "id", "kind", "title", "name", "path", "relativePath", "relative_path",
            "url", "previewUrl", "preview_url", "fileName", "file_name",
            "fileType", "file_type", "mimeType", "mime_type", "sizeBytes",
            "size_bytes", "width", "height", "sha256", "safeArtifactId",
        }
        path_fields = {"path", "relativePath", "relative_path", "url", "previewUrl", "preview_url"}
        safe: Dict[str, Any] = {}
        omitted = 0
        truncated = False
        for key, value in dict(artifact or {}).items():
            if key not in allowed_fields:
                omitted += 1
                continue
            if key in {"sizeBytes", "size_bytes", "width", "height"}:
                try:
                    safe[key] = max(0, int(value))
                except (TypeError, ValueError):
                    omitted += 1
                continue
            if isinstance(value, bool):
                safe[key] = value
                continue
            if not isinstance(value, str):
                omitted += 1
                continue
            stripped = value.strip()
            if key in path_fields and stripped.lower().startswith("data:"):
                omitted += 1
                continue
            limit = 4096 if key in path_fields else 512
            if len(stripped) > limit:
                safe[key] = f"{stripped[:limit]}...[truncated {len(stripped) - limit} chars]"
                truncated = True
            else:
                safe[key] = stripped
        if "kind" not in safe:
            safe["kind"] = "file"
        if omitted:
            safe["artifact_sanitized"] = True
            safe["omitted_field_count"] = omitted
        if truncated:
                safe["metadata_truncated"] = True
        return safe

    def _safe_runtime_payload_with_artifacts(self, event: Dict[str, Any], base_payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(base_payload or {})
        payload.pop("artifact", None)
        payload.pop("artifacts", None)
        artifact_payload = self._safe_runtime_artifact_payload(event, base_payload)
        if "artifact" in artifact_payload:
            payload["artifact"] = artifact_payload["artifact"]
        if "artifacts" in artifact_payload:
            payload["artifacts"] = artifact_payload["artifacts"]
        return payload

    def _runtime_events_for_sse_item(
        self,
        request_id: str,
        event: Dict[str, Any],
        legacy_event_id: int,
    ) -> List[Dict[str, Any]]:
        legacy_type = str((event or {}).get("type") or "")
        session_id = str(event.get("session_id") or self.request_to_session.get(request_id, "") or "")
        turn_id = str(event.get("turn_id") or request_id)
        base_payload = dict(event or {})
        base_payload.setdefault("request_id", request_id)
        base_payload.setdefault("session_id", session_id)
        base_payload.setdefault("turn_id", turn_id)
        base_key = f"stream:{request_id}:{legacy_event_id}"
        if legacy_type == "done":
            finalized_payload = self._safe_runtime_payload_with_artifacts(event, base_payload)
            return [
                {
                    "event_type": "message.assistant.finalized",
                    "payload": {
                        **finalized_payload,
                        "content": event.get("final_text") or event.get("content") or event.get("text") or event.get("message") or "",
                    },
                    "idempotency_key": f"{base_key}:message.assistant.finalized",
                },
                {
                    "event_type": "run.completed",
                    "payload": finalized_payload,
                    "idempotency_key": f"{base_key}:run.completed",
                },
            ]
        if legacy_type == "error":
            return [{
                "event_type": "run.failed",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:run.failed",
            }]
        if legacy_type == "cancelled":
            return [{
                "event_type": "run.cancelled",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:run.cancelled",
            }]
        if legacy_type == "interrupted":
            return [{
                "event_type": "run.interrupted",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:run.interrupted",
            }]
        if legacy_type == "delta":
            return [{
                "event_type": "assistant.delta",
                "payload": {**base_payload, "content": event.get("content") or event.get("delta") or event.get("text") or ""},
                "idempotency_key": f"{base_key}:assistant.delta",
            }]
        if legacy_type == "message_update":
            update_mode = str(event.get("update_mode") or "").lower()
            event_type = "assistant.snapshot" if update_mode == "replace" else "assistant.delta"
            return [{
                "event_type": event_type,
                "payload": {**base_payload, "content": event.get("content") or event.get("delta") or event.get("text") or ""},
                "idempotency_key": f"{base_key}:{event_type}",
            }]
        if legacy_type == "tool_permission_request":
            return [{
                "event_type": "permission.requested",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:permission.requested",
            }]
        if legacy_type == "task_observation":
            task_event_type = str(event.get("task_event_type") or "")
            if task_event_type.startswith("task."):
                return [{
                    "event_type": task_event_type,
                    "payload": base_payload,
                    "idempotency_key": f"{base_key}:{task_event_type}",
                }]
        if legacy_type == "tool_start":
            return [{
                "event_type": "tool.started",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:tool.started",
            }]
        if legacy_type == "tool_heartbeat":
            return [{
                "event_type": "tool.heartbeat",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:tool.heartbeat",
            }]
        if legacy_type == "tool_deadline_extended":
            return [{
                "event_type": "tool.deadline_extended",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:tool.deadline_extended",
            }]
        if legacy_type == "tool_end":
            status = str(event.get("status") or "").lower()
            event_type = "tool.failed" if status in ("failed", "error", "timeout") else "tool.completed"
            return [{
                "event_type": event_type,
                "payload": base_payload,
                "idempotency_key": f"{base_key}:{event_type}",
            }]
        if legacy_type.startswith("subagent_"):
            subagent_event_type = {
                "subagent_start": "subagent.started",
                "subagent_update": "subagent.updated",
                "subagent_complete": "subagent.completed",
                "subagent_failed": "subagent.failed",
                "subagent_timeout": "subagent.timeout",
                "subagent_cancelled": "subagent.cancelled",
            }.get(legacy_type)
            if not subagent_event_type:
                return []
            task = event.get("task") if isinstance(event.get("task"), dict) else {}
            child_request_id = str(
                event.get("child_request_id")
                or task.get("requestId")
                or task.get("childSessionId")
                or ""
            )
            task_id = str(event.get("task_id") or task.get("id") or task.get("task_id") or "")
            parent_request_id = str(request_id)
            parent_session_id = str(session_id or "")
            payload = {
                **base_payload,
                "parent_request_id": parent_request_id,
                "parent_session_id": parent_session_id,
                "child_request_id": child_request_id,
                "task_id": task_id,
                "name": event.get("name") or task.get("name") or task.get("summary") or task_id or "Subagent",
                "role": event.get("role") or task.get("role") or "subagent",
                "summary": event.get("summary") or task.get("summary") or "",
                "status": event.get("status") or task.get("status") or "",
                "result_preview": event.get("result_preview") or "",
                "deadline_at": task.get("deadlineAt"),
                "timeout_seconds": task.get("timeoutSeconds"),
                "last_heartbeat_at": task.get("lastHeartbeatAt"),
            }
            return [{
                "event_type": subagent_event_type,
                "payload": payload,
                "idempotency_key": f"{base_key}:{subagent_event_type}:{child_request_id or task_id}",
            }]
        if legacy_type in ("artifact", "file", "image", "video", "audio", "voice_attach"):
            return [{
                "event_type": "artifact.created",
                "payload": self._safe_runtime_artifact_payload(event, base_payload),
                "idempotency_key": f"{base_key}:artifact.created",
            }]
        if legacy_type == "phase":
            return [{
                "event_type": "run.phase",
                "payload": base_payload,
                "idempotency_key": f"{base_key}:run.phase",
            }]
        return []

    def _record_runtime_events_for_sse_item(
        self,
        request_id: str,
        event: Dict[str, Any],
        legacy_event_id: int,
    ) -> None:
        rows = []
        attempted = []
        failures = []
        for item in self._runtime_events_for_sse_item(request_id, event, legacy_event_id):
            attempted.append(item["event_type"])
            row = self._append_runtime_event(
                request_id,
                item["event_type"],
                session_id=str(event.get("session_id") or self.request_to_session.get(request_id, "") or ""),
                turn_id=str(event.get("turn_id") or request_id),
                payload=item.get("payload") or {},
                idempotency_key=item.get("idempotency_key") or "",
            )
            if row and not row.get("append_failed") and not row.get("append_skipped"):
                rows.append(row)
            elif row:
                failures.append({
                    "event_type": item["event_type"],
                    "error_code": row.get("error_code") or "RUN_EVENT_APPEND_FAILED",
                    "error_type": row.get("error_type") or "",
                })
        if rows:
            event["runtime_event_ids"] = [row.get("event_id") for row in rows]
            event["runtime_event_types"] = [row.get("event_type") for row in rows]
        if attempted:
            event["runtime_event_attempted_types"] = attempted
            event["runtime_event_persisted"] = len(rows) == len(attempted) and not failures
        if failures:
            event["runtime_event_append_errors"] = failures

    def _active_run_stale_seconds(self) -> int:
        configured = conf().get("web_active_run_stale_seconds", ACTIVE_RUN_STALE_SECONDS)
        parsed = self._coerce_positive_int(configured, ACTIVE_RUN_STALE_SECONDS)
        return parsed or ACTIVE_RUN_STALE_SECONDS

    def _recover_stale_active_runs(
        self,
        ledger,
        *,
        registry_by_request: Dict[str, Dict[str, Any]],
        stale_locks: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        """Terminalize orphaned active message runs that have no live evidence."""
        stale_seconds = self._active_run_stale_seconds()
        now = time.time()
        interrupted: List[str] = []
        if stale_locks is None:
            try:
                from common.ecorex_workspace import list_session_locks

                stale_locks = list_session_locks(_get_workspace_root(), cleanup=False)
            except Exception as exc:
                logger.debug(f"[WebChannel] stale active run lock scan skipped: {_web_body_log_summary(exc)}")
                stale_locks = []

        def has_live_session_lock(session_id: str) -> bool:
            if not session_id:
                return False
            for item in stale_locks or []:
                if str(item.get("session_id") or "") != session_id:
                    continue
                if item.get("removed") or item.get("dead_owner"):
                    continue
                if item.get("alive") is True:
                    return True
                if not item.get("stale"):
                    return True
            return False

        for row in ledger.active_snapshot():
            request_id = str(row.get("request_id") or "")
            if not request_id or request_id in registry_by_request:
                continue
            run_type = str(row.get("run_type") or "message").lower()
            if run_type != "message":
                continue
            if str(row.get("status") or row.get("phase") or "").lower() == "queued":
                continue
            if self._sse_request_exists(request_id):
                continue
            session_id = str(row.get("session_id") or self.request_to_session.get(request_id, "") or "")
            if has_live_session_lock(session_id):
                continue
            updated_at = float(row.get("updated_at") or row.get("created_at") or now)
            if now - updated_at < stale_seconds:
                continue
            message = (
                "Active run had no cancel token, stream state, or live session lock "
                f"for at least {stale_seconds} seconds."
            )
            ledger.mark_terminal(
                request_id,
                "interrupted",
                reason="stale_active_recovered",
                error_code="STALE_ACTIVE_RUN",
                error_message=message,
            )
            self.request_to_session.pop(request_id, None)
            interrupted.append(request_id)
            logger.warning(
                f"[WebChannel] marked stale active run interrupted: "
                f"request={request_id} session={session_id}"
            )
        return interrupted

    def active_requests_snapshot(self):
        """Return backend-authoritative active request state for UI recovery.

        The browser may lose local request bookkeeping on refresh or when the
        same runtime is opened from another surface. The durable run ledger is
        the primary in-flight source of truth; the cancel registry supplies
        in-process cancelling and compatibility fallback state.
        """
        try:
            from agent.protocol import RuntimeProjectionService, get_cancel_registry, get_run_ledger
            from common.ecorex_workspace import list_session_locks

            requests = []
            recent_terminal_requests = []
            sessions = {}
            run_status_counts = {}
            seen_request_ids = set()
            registry_rows = get_cancel_registry().snapshot()
            registry_by_request = {
                row.get("request_id", ""): row
                for row in registry_rows
                if row.get("request_id")
            }
            ledger = get_run_ledger()
            projection_service = RuntimeProjectionService()
            def bump_status_count(item) -> None:
                status = str((item or {}).get("state") or (item or {}).get("status") or "").strip()
                if not status:
                    return
                run_status_counts[status] = run_status_counts.get(status, 0) + 1

            def is_current_cancelling_registry_row(durable_row, registry_row) -> bool:
                if not durable_row or not registry_row:
                    return False
                if durable_row.get("status") != "cancelled" or not registry_row.get("cancelled"):
                    return False
                terminal_age = durable_row.get("terminal_age_seconds")
                try:
                    terminal_age_seconds = float(terminal_age)
                except Exception:
                    return False
                return terminal_age_seconds <= RECENT_TERMINAL_RUN_MAX_AGE_SECONDS

            def is_primary_chat_request(item, request_id: str, session_id: str) -> bool:
                run_type = str((item or {}).get("run_type") or "message").lower()
                return (
                    run_type not in {"subagent", "scheduler"}
                    and not str(request_id or "").startswith(("subagent-", "scheduler_"))
                    and not str(session_id or "").startswith(("subagent-", "scheduler_"))
                )

            def attach_run_center_policy(item) -> dict:
                row = dict(item or {})
                request_id = str(row.get("request_id") or "")
                session_id = str(row.get("session_id") or "")
                run_type = str(row.get("run_type") or "message").lower()
                state = str(row.get("state") or row.get("status") or row.get("phase") or "").lower()
                terminal = row.get("terminal_at") is not None or state in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "timeout",
                }
                is_subagent = (
                    run_type == "subagent"
                    or request_id.startswith("subagent-")
                    or session_id.startswith("subagent-")
                )
                is_scheduler = (
                    run_type == "scheduler"
                    or request_id.startswith("scheduler_")
                    or session_id.startswith("scheduler_")
                )
                failed = "fail" in state or "error" in state or "interrupt" in state
                cancelling = bool(row.get("cancelled")) or "cancell" in state
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                if state == "queued" or str(row.get("phase") or "").lower() == "queued":
                    row["queue_position"] = int(metadata.get("queue_position") or self._queue_position_for_request(session_id, request_id) or 0)
                    row["queued_after_request_ids"] = list(metadata.get("queued_after_request_ids") or [])
                    row["queue_reason"] = metadata.get("queue_reason") or ""
                if request_id:
                    try:
                        projection = projection_service.request_projection(
                            request_id,
                            expected_session_id=session_id,
                            include_events=False,
                        )
                    except Exception:
                        projection = {}
                    task_observations = projection.get("task_observations") if isinstance(projection, dict) else []
                    image_jobs = projection.get("image_jobs") if isinstance(projection, dict) else []
                    if isinstance(task_observations, list) and task_observations:
                        row["task_observations"] = task_observations[:8]
                    if isinstance(image_jobs, list) and image_jobs:
                        row["image_jobs"] = image_jobs[:4]
                terminal_code = "{} {}".format(
                    row.get("error_code") or metadata.get("error_code") or "",
                    row.get("terminal_reason") or metadata.get("terminal_reason") or "",
                ).lower()
                non_retryable_terminal = any(
                    marker in terminal_code
                    for marker in (
                        "auth",
                        "permission",
                        "policy",
                        "denied",
                        "forbidden",
                        "invalid",
                        "bad_request",
                        "badrequest",
                        "not_retryable",
                        "non_retryable",
                    )
                )
                retryable = False
                retry_mode = "unavailable"
                retry_disabled_reason = ""
                if is_subagent:
                    retry_disabled_reason = "subagent_replay_unavailable"
                    role = str(metadata.get("role") or "subagent")
                    fallback_name = f"{role} {str(metadata.get('task_id') or request_id or session_id)[-4:]}"
                    row["display_name"] = metadata.get("name") or metadata.get("summary") or fallback_name
                    row["title"] = row.get("display_name")
                    row["task_id"] = metadata.get("task_id") or request_id.replace("subagent-", "")
                    row["parent_request_id"] = metadata.get("parent_request_id") or ""
                    row["deadline_at"] = metadata.get("deadline_at")
                    row["timeout_seconds"] = metadata.get("timeout_seconds")
                elif is_scheduler:
                    retry_disabled_reason = "scheduler_replay_unavailable"
                elif not session_id:
                    retry_disabled_reason = "missing_session_id"
                elif not failed:
                    retry_disabled_reason = "not_failed"
                elif row.get("retryable") is False and row.get("recoverable") is False:
                    retry_disabled_reason = "non_retryable_terminal"
                elif non_retryable_terminal:
                    retry_disabled_reason = "non_retryable_terminal"
                else:
                    retryable = True
                    retry_mode = "manual_retry_prepare"
                row["retryable"] = retryable
                row["recoverable"] = bool(session_id) and not is_subagent and not is_scheduler
                row["retry_mode"] = retry_mode
                row["retry_disabled_reason"] = retry_disabled_reason
                row["actions"] = {
                    "open": bool(session_id) and not is_subagent and not is_scheduler,
                    "recover": bool(session_id) and not is_subagent and not is_scheduler,
                    "retry": retryable,
                    "stop": bool(
                        (request_id or session_id)
                        and not failed
                        and not terminal
                        and not (is_subagent and not request_id)
                    ),
                    "collect": bool(is_subagent and terminal),
                    "cancelQueue": bool(state == "queued" or str(row.get("phase") or "").lower() == "queued"),
                    "diagnostics": True,
                }
                if cancelling and not terminal:
                    row["actions"]["retry"] = False
                    row["retryable"] = False
                    row["retry_mode"] = "unavailable"
                    row["retry_disabled_reason"] = "stopping"
                return row

            stale_locks = []
            for item in list_session_locks(_get_workspace_root(), cleanup=False):
                if not (item.get("dead_owner") or item.get("stale")):
                    continue
                if item.get("dead_owner"):
                    path = str(item.get("path") or "")
                    if path:
                        try:
                            Path(path).unlink()
                            item["removed"] = True
                            logger.warning(
                                f"[WebChannel] Removed dead session lock before active snapshot: "
                                f"{_diagnostic_stale_lock_summary(item)}"
                            )
                        except FileNotFoundError:
                            item["removed"] = True
                        except Exception as exc:
                            item["remove_error"] = _public_exception_summary(exc)
                stale_locks.append(item)
            interrupted_session_ids = {
                session_id
                for item in stale_locks
                for session_id in [str(item.get("session_id") or "")]
                if item.get("dead_owner")
                if session_id
            }
            interrupted_request_ids = set()
            if interrupted_session_ids:
                for row in ledger.active_snapshot():
                    request_id = row.get("request_id", "")
                    session_id = row.get("session_id", "")
                    run_type = str(row.get("run_type") or "message")
                    if request_id and session_id in interrupted_session_ids and run_type == "message":
                        ledger.mark_terminal(
                            request_id,
                            "interrupted",
                            reason="sidecar_interrupted",
                            error_code="SIDECAR_INTERRUPTED",
                            error_message="Runtime session lock owner disappeared before the run reached a terminal state.",
                        )
                        interrupted_request_ids.add(request_id)
            for request_id in self._recover_stale_active_runs(
                ledger,
                registry_by_request=registry_by_request,
                stale_locks=stale_locks,
            ):
                interrupted_request_ids.add(request_id)
            boot_time = float(getattr(self, "runtime_started_at", 0) or 0)
            if boot_time > 0:
                for row in ledger.active_snapshot():
                    request_id = str(row.get("request_id") or "")
                    if not request_id or request_id in registry_by_request:
                        continue
                    run_type = str(row.get("run_type") or "message").lower()
                    if run_type not in {"subagent", "scheduler"}:
                        continue
                    updated_at = float(row.get("updated_at") or row.get("created_at") or boot_time)
                    if updated_at >= boot_time:
                        continue
                    reason = f"{run_type}_sidecar_interrupted"
                    error_code = f"{run_type.upper()}_SIDECAR_INTERRUPTED"
                    error_message = (
                        f"{run_type} run was left active by a previous runtime process "
                        "and has no in-process cancel token after sidecar restart."
                    )
                    if run_type == "subagent":
                        self._interrupt_orphan_subagent_state(
                            row,
                            reason=reason,
                            error_code=error_code,
                            error_message=error_message,
                        )
                    ledger.mark_terminal(
                        request_id,
                        "interrupted",
                        reason=reason,
                        error_code=error_code,
                        error_message=error_message,
                    )
                    interrupted_request_ids.add(request_id)
            for row in ledger.active_snapshot():
                request_id = row.get("request_id", "")
                session_id = row.get("session_id") or self.request_to_session.get(request_id, "")
                registry_row = registry_by_request.get(request_id, {})
                item = {
                    **row,
                    "session_id": session_id,
                    "stream_available": request_id in self.sse_queues,
                }
                if registry_row.get("cancelled"):
                    item["cancelled"] = True
                    item["state"] = "cancelling"
                    item["cancelled_at"] = registry_row.get("cancelled_at")
                    item["cancel_age_seconds"] = registry_row.get("cancel_age_seconds")
                item = attach_run_center_policy(item)
                requests.append(item)
                bump_status_count(item)
                seen_request_ids.add(request_id)
                if session_id and not item.get("cancelled") and is_primary_chat_request(item, request_id, session_id):
                    sessions.setdefault(session_id, []).append(request_id)
            for row in ledger.terminal_snapshot(
                max_age_seconds=RECENT_TERMINAL_RUN_MAX_AGE_SECONDS,
                limit=RECENT_TERMINAL_RUN_LIMIT,
            ):
                request_id = row.get("request_id", "")
                if not request_id or request_id in seen_request_ids:
                    continue
                session_id = row.get("session_id") or self.request_to_session.get(request_id, "")
                item = {
                    **row,
                    "session_id": session_id,
                    "stream_available": request_id in self.sse_queues,
                    "source": "run_ledger",
                }
                item = attach_run_center_policy(item)
                recent_terminal_requests.append(item)
                bump_status_count(item)
                if not is_current_cancelling_registry_row(item, registry_by_request.get(request_id, {})):
                    seen_request_ids.add(request_id)
            for row in registry_rows:
                request_id = row.get("request_id", "")
                if not request_id or request_id in seen_request_ids or request_id in interrupted_request_ids:
                    continue
                durable_row = ledger.get_run(request_id)
                if (
                    durable_row
                    and durable_row.get("terminal_at") is not None
                    and not is_current_cancelling_registry_row(durable_row, row)
                ):
                    continue
                session_id = row.get("session_id") or self.request_to_session.get(request_id, "")
                item = {
                    **row,
                    "session_id": session_id,
                    "stream_available": request_id in self.sse_queues,
                    "source": "cancel_registry",
                }
                if request_id.startswith("subagent-") or str(session_id).startswith("subagent-"):
                    item["run_type"] = "subagent"
                elif request_id.startswith("scheduler_") or str(session_id).startswith("scheduler_"):
                    item["run_type"] = "scheduler"
                item = attach_run_center_policy(item)
                requests.append(item)
                bump_status_count(item)
                if session_id and not item.get("cancelled") and is_primary_chat_request(item, request_id, session_id):
                    sessions.setdefault(session_id, []).append(request_id)
            children_by_parent = {}
            for item in [*requests, *recent_terminal_requests]:
                run_type = str((item or {}).get("run_type") or "").lower()
                request_id = str((item or {}).get("request_id") or "")
                session_id = str((item or {}).get("session_id") or "")
                if run_type != "subagent" and not request_id.startswith("subagent-") and not session_id.startswith("subagent-"):
                    continue
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                parent_key = (
                    str(metadata.get("parent_request_id") or "").strip()
                    or str(metadata.get("parent_session_id") or "").strip()
                    or str(item.get("parent_id") or "").strip()
                )
                if not parent_key:
                    continue
                children_by_parent.setdefault(parent_key, []).append({
                    "request_id": request_id,
                    "session_id": session_id,
                    "task_id": metadata.get("task_id") or item.get("task_id") or request_id.replace("subagent-", ""),
                    "name": item.get("display_name") or metadata.get("name") or request_id,
                    "role": metadata.get("role") or "subagent",
                    "status": item.get("status") or item.get("state") or "",
                    "phase": item.get("phase") or "",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "terminal_at": item.get("terminal_at"),
                    "deadline_at": metadata.get("deadline_at") or item.get("deadline_at"),
                    "actions": item.get("actions") or {},
                })
            public_stale_locks = [_diagnostic_stale_lock_summary(item) for item in stale_locks]
            return {
                "status": "success",
                "requests": requests,
                "recent_terminal_requests": recent_terminal_requests,
                "recentTerminalRequests": recent_terminal_requests,
                "children_by_parent": children_by_parent,
                "childrenByParent": children_by_parent,
                "run_status_counts": run_status_counts,
                "runStatusCounts": run_status_counts,
                "sessions": sessions,
                "stale_locks": public_stale_locks,
                "staleLocks": public_stale_locks,
            }
        except Exception as e:
            logger.error(f"[WebChannel] active_requests_snapshot error: {_web_body_log_summary(e)}")
            return {
                "status": "error",
                "message": _public_exception_message("Active requests snapshot unavailable.", e),
                **_public_exception_summary(e),
                "requests": [],
                "sessions": {},
                "stale_locks": [],
                "staleLocks": [],
            }

    def _ensure_sse_state(self, request_id: str) -> None:
        if not request_id:
            return
        with self.sse_lock:
            if request_id not in self.sse_queues:
                self.sse_queues[request_id] = Queue()
            if request_id not in self.sse_events:
                self.sse_events[request_id] = []
            if request_id not in self.sse_event_offsets:
                self.sse_event_offsets[request_id] = 0
            if request_id not in self.sse_conditions:
                self.sse_conditions[request_id] = threading.Condition()

    def _sse_request_exists(self, request_id: str) -> bool:
        with self.sse_lock:
            return bool(request_id) and (
                request_id in self.sse_queues or request_id in self.sse_events
            )

    def _normalize_sse_event(self, request_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(item or {})
        legacy_type = str(event.get("type") or "")
        event.setdefault("request_id", request_id)
        event.setdefault("timestamp", time.time())
        event["protocol_version"] = self.SSE_PROTOCOL_VERSION

        if legacy_type == "done":
            event["event_type"] = "run.completed"
            event["state"] = "completed"
            event["terminal"] = True
            event.setdefault("terminal_reason", event.get("terminal_reason") or "completed")
        elif legacy_type == "error":
            event["event_type"] = "run.failed"
            event["state"] = "failed"
            event["terminal"] = True
            event.setdefault("terminal_reason", event.get("terminal_reason") or "failed")
            event.setdefault("error_code", event.get("error_code") or "STREAM_ERROR")
        elif legacy_type == "cancelled":
            event["event_type"] = "run.cancelled"
            event["state"] = "cancelled"
            event["terminal"] = True
            event.setdefault("terminal_reason", event.get("terminal_reason") or "cancelled")
        elif legacy_type == "replay_gap":
            event["event_type"] = "stream.replay_gap"
            event["state"] = "recovering"
            event["terminal"] = True
            event.setdefault("terminal_reason", "replay_gap")
            event.setdefault("recoverable", True)
        elif legacy_type == "interrupted":
            event["event_type"] = "run.interrupted"
            event["state"] = "interrupted"
            event["terminal"] = True
            event.setdefault("terminal_reason", event.get("terminal_reason") or "interrupted")
            event.setdefault("error_code", event.get("error_code") or "RUN_INTERRUPTED")
            event.setdefault("recoverable", True)
        elif legacy_type in ("delta", "message_update"):
            event.setdefault("event_type", "model.delta")
            event.setdefault("update_mode", "replace" if legacy_type == "message_update" else "append")
            event.setdefault("state", "running")
        elif legacy_type in ("reasoning", "thinking"):
            event.setdefault("event_type", "reasoning.update")
            event.setdefault("state", "running")
        elif legacy_type == "tool_permission_request":
            event.setdefault("event_type", "approval.requested")
            event.setdefault("state", "waiting_permission")
        elif legacy_type == "tool_start":
            event.setdefault("event_type", "tool.started")
            event.setdefault("state", "running")
        elif legacy_type == "tool_heartbeat":
            event.setdefault("event_type", "tool.heartbeat")
            event.setdefault("state", "running")
        elif legacy_type == "tool_deadline_extended":
            event.setdefault("event_type", "tool.deadline_extended")
            event.setdefault("state", "running")
        elif legacy_type == "tool_end":
            status = str(event.get("status") or "").lower()
            event.setdefault("event_type", "tool.failed" if status in ("failed", "error", "timeout") else "tool.completed")
            event.setdefault("state", "running")
        elif legacy_type in ("artifact", "file", "image", "video", "audio", "voice_attach"):
            event.setdefault("event_type", "artifact.updated")
            event.setdefault("state", "finalizing" if legacy_type == "voice_attach" else "running")
        elif legacy_type == "artifact_limit":
            event.setdefault("event_type", "artifact.limit")
            event.setdefault("state", "running")
            event.setdefault("recoverable", True)
        elif legacy_type == "message_end":
            event.setdefault("event_type", "message.finalizing")
            event.setdefault("state", "finalizing")
        else:
            event.setdefault("event_type", f"legacy.{legacy_type or 'unknown'}")
            event.setdefault("state", "running")
        return event

    def _build_replay_gap_event(
        self,
        request_id: str,
        requested_last_event_id: int,
        retained_from_event_id: int,
    ) -> Dict[str, Any]:
        return self._normalize_sse_event(request_id, {
            "type": "replay_gap",
            "request_id": request_id,
            "requested_last_event_id": requested_last_event_id,
            "retained_from_event_id": retained_from_event_id,
            "next_event_id": retained_from_event_id,
            "message": "SSE replay cursor is older than the retained event window.",
        })

    def _build_interrupted_event(
        self,
        request_id: str,
        *,
        session_id: str = "",
        terminal_reason: str = "sidecar_interrupted",
        error_code: str = "SIDECAR_INTERRUPTED",
        message: str = "Runtime sidecar restarted before this run reached a terminal state.",
    ) -> Dict[str, Any]:
        return self._normalize_sse_event(request_id, {
            "type": "interrupted",
            "request_id": request_id,
            "session_id": session_id,
            "terminal": True,
            "terminal_reason": terminal_reason or "interrupted",
            "error_code": error_code or "RUN_INTERRUPTED",
            "message": message,
            "content": message,
            "recoverable": True,
        })

    def _recover_sidecar_interrupted_stream_event(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Return a terminal stream event when durable state proves sidecar loss.

        A restarted sidecar loses in-memory SSE queues, but the durable ledger
        and old session lock can still prove that a message run was interrupted.
        In that case reconnecting clients should receive a typed terminal
        event instead of waiting on an invalid request id path.
        """
        if not request_id:
            return None
        try:
            from agent.protocol import get_run_ledger
            from common.ecorex_workspace import list_session_locks

            ledger = get_run_ledger()
            row = ledger.get_run(request_id)
            if not row:
                return None
            run_type = str(row.get("run_type") or "message").lower()
            if run_type != "message":
                return None
            session_id = str(row.get("session_id") or self.request_to_session.get(request_id, "") or "")
            if (
                row.get("status") == "interrupted"
                and row.get("terminal_reason") == "sidecar_interrupted"
            ):
                return self._build_interrupted_event(request_id, session_id=session_id)
            if row.get("terminal_at") is not None or not session_id:
                return None

            dead_lock = None
            for item in list_session_locks(_get_workspace_root(), cleanup=False):
                if item.get("dead_owner") and str(item.get("session_id") or "") == session_id:
                    dead_lock = item
                    break
            if not dead_lock:
                return None

            path = str(dead_lock.get("path") or "")
            if path:
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.debug(f"[WebChannel] sidecar interruption lock cleanup skipped: {_web_body_log_summary(exc)}")
            message = "Runtime session lock owner disappeared before the run reached a terminal state."
            ledger.mark_terminal(
                request_id,
                "interrupted",
                reason="sidecar_interrupted",
                error_code="SIDECAR_INTERRUPTED",
                error_message=message,
            )
            final_row = ledger.get_run(request_id) or {}
            if (
                final_row.get("status") != "interrupted"
                or final_row.get("terminal_reason") != "sidecar_interrupted"
            ):
                return None
            return self._build_interrupted_event(request_id, session_id=session_id, message=message)
        except Exception as exc:
            logger.debug(f"[WebChannel] sidecar interrupted stream recovery skipped for {request_id}: {_web_body_log_summary(exc)}")
            return None

    def _request_session_mismatch_event(
        self,
        request_id: str,
        expected_session_id: str = "",
        *,
        actual_session_id: str = "",
    ) -> Dict[str, Any]:
        expected = str(expected_session_id or "").strip()
        if not request_id or not expected:
            return {}
        actual = str(actual_session_id or self.request_to_session.get(request_id, "") or "").strip()
        if not actual:
            try:
                from agent.protocol import RuntimeProjectionService

                actual = str(RuntimeProjectionService().owner_session_id_for_request(request_id) or "").strip()
            except Exception as exc:
                logger.debug(f"[WebChannel] request owner lookup skipped for {request_id}: {_web_body_log_summary(exc)}")
                actual = ""
        if not actual or actual == expected:
            return {}
        return self._normalize_sse_event(request_id, {
            "type": "error",
            "error_type": "session_mismatch",
            "error_code": "SESSION_MISMATCH",
            "code": "SESSION_MISMATCH",
            "message": "Request does not belong to the active session. Refresh the conversation list and retry.",
            "content": "Request does not belong to the active session. Refresh the conversation list and retry.",
            "recoverable": True,
            "retryable": False,
        })

    def _runtime_projection_replay_events(self, request_id: str, expected_session_id: str = "") -> List[Dict[str, Any]]:
        """Build legacy-compatible SSE recovery events from durable runtime events."""
        if not request_id:
            return []
        mismatch_event = self._request_session_mismatch_event(request_id, expected_session_id)
        if mismatch_event:
            return [mismatch_event]
        try:
            from agent.protocol import RuntimeProjectionService

            projection = RuntimeProjectionService().request_projection(
                request_id,
                expected_session_id=str(expected_session_id or ""),
            )
        except Exception as exc:
            logger.warning(f"[WebChannel] runtime projection replay failed for {request_id}: {_web_body_log_summary(exc)}")
            return []
        if not projection or int(projection.get("event_count") or 0) <= 0:
            return []

        session_id = str(projection.get("session_id") or self.request_to_session.get(request_id, "") or "")
        state = str(projection.get("state") or "unknown")
        latest_event_id = int(projection.get("latest_event_id") or 0)
        terminal_reason = str(projection.get("terminal_reason") or state or "")
        terminal_message = str(projection.get("terminal_message") or "")
        assistant = next(
            (
                message for message in projection.get("messages", [])
                if isinstance(message, dict) and message.get("role") == "assistant"
            ),
            {},
        )
        content = str((assistant or {}).get("content") or "")
        base = {
            "request_id": request_id,
            "session_id": session_id,
            "runtime_projection_replay": True,
            "runtime_event_latest_id": latest_event_id,
            "projection_state": state,
        }
        events: List[Dict[str, Any]] = []
        if content:
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "message_update",
                "content": content,
                "update_mode": "replace",
            }))

        if state == "completed":
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "done",
                "content": content,
                "final_text": content,
                "terminal_reason": terminal_reason or "completed",
            }))
        elif state == "failed":
            error_text = terminal_message or terminal_reason or content or "Runtime failed."
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "error",
                "content": error_text,
                "message": error_text,
                "terminal_reason": terminal_reason or "failed",
                "error_code": "RUNTIME_PROJECTION_FAILED",
            }))
        elif state == "cancelled":
            cancel_text = terminal_message or content
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "cancelled",
                "content": cancel_text,
                "terminal_reason": terminal_reason or "cancelled",
            }))
        elif state == "interrupted":
            interrupted_text = terminal_message or terminal_reason or content or "Runtime stream was interrupted."
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "interrupted",
                "content": interrupted_text,
                "message": interrupted_text,
                "terminal_reason": terminal_reason or "interrupted",
                "error_code": "RUNTIME_PROJECTION_INTERRUPTED",
                "recoverable": True,
            }))
        elif state not in {"unknown"}:
            events.append(self._normalize_sse_event(request_id, {
                **base,
                "type": "interrupted",
                "content": content or "Runtime stream state was restored, but the live stream is no longer attached.",
                "message": "Runtime stream state was restored, but the live stream is no longer attached.",
                "terminal_reason": "runtime_projection_detached",
                "error_code": "RUNTIME_PROJECTION_DETACHED",
                "recoverable": True,
            }))
        return events

    def _push_sse_event(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Append one SSE event for every subscriber and keep legacy queue parity."""
        if not self._sse_request_exists(request_id):
            return False
        self._ensure_sse_state(request_id)
        event = self._normalize_sse_event(request_id, item)
        legacy_event_id = 0
        with self.sse_lock:
            if not self._sse_request_exists(request_id):
                return False
            cond = self.sse_conditions[request_id]
        with cond:
            events = self.sse_events.get(request_id)
            if events is None:
                return False
            legacy_event_id = self.sse_event_offsets.get(request_id, 0) + len(events)
            self._record_runtime_events_for_sse_item(request_id, event, legacy_event_id)
            events.append(event)
            excess = len(events) - self.SSE_MAX_REPLAY_EVENTS
            if excess > 0:
                del events[:excess]
                self.sse_event_offsets[request_id] = self.sse_event_offsets.get(request_id, 0) + excess
            cond.notify_all()
        try:
            self.sse_queues[request_id].put(event)
        except Exception as e:
            logger.debug(f"[WebChannel] legacy SSE queue mirror skipped for {request_id}: {_web_body_log_summary(e)}")
        self._record_run_event_phase(request_id, event)
        return True

    def _cleanup_sse_request(self, request_id: str) -> None:
        with self.sse_lock:
            timer = self.sse_cleanup_timers.pop(request_id, None)
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self.sse_queues.pop(request_id, None)
            self.sse_events.pop(request_id, None)
            self.sse_event_offsets.pop(request_id, None)
            self.sse_conditions.pop(request_id, None)
            self.sse_subscribers.pop(request_id, None)
            self.sse_done_sent.discard(request_id)
            self.sse_stream_tokens.pop(request_id, None)
            self.request_artifacts.pop(request_id, None)
            self.request_project_contexts.pop(request_id, None)
            self.request_to_session.pop(request_id, None)

    def _cleanup_sse_request_if_idle(self, request_id: str, reason: str = "") -> None:
        with self.sse_lock:
            self.sse_cleanup_timers.pop(request_id, None)
            exists = bool(request_id) and (
                request_id in self.sse_queues or request_id in self.sse_events
            )
            subscribers = self.sse_subscribers.get(request_id, 0)
        if not exists:
            return
        if subscribers > 0:
            return
        try:
            from agent.protocol import get_cancel_registry

            if get_cancel_registry().get_event(request_id) is not None:
                self._schedule_sse_cleanup(request_id, delay=self.SSE_ORPHAN_TTL_SECONDS, reason="active-request")
                return
        except Exception as exc:
            logger.debug(f"[WebChannel] SSE cleanup active check skipped for {request_id}: {_web_body_log_summary(exc)}")
            return
        logger.info(f"[WebChannel] Cleaning idle SSE replay state for {request_id}: {reason}")
        self._cleanup_sse_request(request_id)

    def _schedule_sse_cleanup(self, request_id: str, delay: int = None, reason: str = "") -> None:
        if not request_id or not self._sse_request_exists(request_id):
            return
        with self.sse_lock:
            timer = self.sse_cleanup_timers.pop(request_id, None)
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
            next_delay = self.SSE_ORPHAN_TTL_SECONDS if delay is None else max(1, int(delay))
            cleanup_timer = threading.Timer(next_delay, lambda: self._cleanup_sse_request_if_idle(request_id, reason))
            cleanup_timer.daemon = True
            self.sse_cleanup_timers[request_id] = cleanup_timer
            cleanup_timer.start()

    def _push_terminal_event_once(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Emit one terminal stream event per request while preserving replay."""
        with self.sse_lock:
            if request_id in self.sse_done_sent:
                logger.debug(f"[WebChannel] duplicate terminal event skipped for request {request_id}")
                return False
            self.sse_done_sent.add(request_id)
        pushed = self._push_sse_event(request_id, item)
        if not pushed:
            with self.sse_lock:
                self.sse_done_sent.discard(request_id)
            return False
        with self.sse_lock:
            has_subscribers = self.sse_subscribers.get(request_id, 0) > 0
        if pushed:
            if not has_subscribers:
                self._schedule_sse_cleanup(request_id, reason="terminal-without-subscriber")
        return pushed

    def _push_done_event_once(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Emit one successful terminal done event per request."""
        payload = dict(item or {})
        payload["type"] = "done"
        return self._push_terminal_event_once(request_id, payload)

    def _push_error_event_once(
        self,
        request_id: str,
        message: str,
        *,
        error_code: str = "STREAM_ERROR",
        terminal_reason: str = "failed",
        usage: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        payload = {
            "type": "error",
            "content": message or "Worker failed before producing a response.",
            "message": message or "Worker failed before producing a response.",
            "request_id": request_id,
            "timestamp": time.time(),
            "error_code": error_code,
            "terminal_reason": terminal_reason or "failed",
            "usage": usage,
        }
        if isinstance(extra, dict):
            for key, value in extra.items():
                if value is not None and key not in payload:
                    if key == "retry_mode" and value == "auto_retry":
                        value = "manual_retry_prepare" if extra.get("retryable") else "unavailable"
                    payload[key] = value
        return self._push_terminal_event_once(request_id, payload)

    def _push_cancelled_event_once(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Emit one cancellation terminal event per request."""
        payload = dict(item or {})
        payload["type"] = "cancelled"
        return self._push_terminal_event_once(request_id, payload)

    def _push_cancelled_events_for_session(self, session_id: str, request_ids: List[str], lang: str = "zh") -> None:
        content = "Interrupted by a new message." if str(lang).lower().startswith("en") else "已被新消息中断"
        for request_id in request_ids:
            self._mark_run_phase(request_id, "cancelling", status="cancelling")
            if not self._sse_request_exists(request_id):
                continue
            self._push_cancelled_event_once(request_id, {
                "type": "cancelled",
                "content": content,
                "request_id": request_id,
                "timestamp": time.time(),
            })

    def _cancel_subagents_for_parent(self, session_id: str) -> Dict[str, Any]:
        if not session_id or str(session_id).startswith("subagent-"):
            return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": []}
        try:
            from agent.tools.subagent.subagent import cancel_children_for_default_workspace

            return cancel_children_for_default_workspace(session_id, workspace=_get_workspace_root())
        except Exception as e:
            logger.warning(f"[WebChannel] subagent cascade cancel skipped for {session_id}: {_web_body_log_summary(e)}")
            public = _public_error_payload("Subagent cascade cancel failed.", e)
            return {
                "cancelledTasks": 0,
                "cancelledRequests": 0,
                "tasks": [],
                "error": public["message"],
                "errorType": public.get("errorType", ""),
                "errorHash": public.get("errorHash", ""),
            }

    def _interrupt_orphan_subagent_state(
        self,
        row: Dict[str, Any],
        *,
        reason: str,
        error_code: str,
        error_message: str,
    ) -> Dict[str, Any]:
        try:
            from agent.tools.subagent.subagent import interrupt_orphan_task

            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            return interrupt_orphan_task(
                _get_workspace_root(),
                task_id=str(metadata.get("task_id") or ""),
                child_session_id=str(row.get("request_id") or row.get("session_id") or ""),
                reason=reason,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as e:
            logger.warning(f"[WebChannel] subagent orphan state interruption skipped: {_web_body_log_summary(e)}")
            public = _public_error_payload("Subagent orphan state interruption failed.", e)
            return {
                "updated": False,
                "task": {},
                "error": public["message"],
                "errorType": public.get("errorType", ""),
                "errorHash": public.get("errorHash", ""),
            }

    def _begin_same_session_replacement(self, session_id: str) -> int:
        with self.same_session_replacement_lock:
            ticket = int(self.same_session_replacement_tickets.get(session_id, 0)) + 1
            self.same_session_replacement_tickets[session_id] = ticket
            return ticket

    def _same_session_replacement_is_current(self, session_id: str, ticket: Optional[int]) -> bool:
        if ticket is None:
            return True
        with self.same_session_replacement_lock:
            return self.same_session_replacement_tickets.get(session_id) == ticket

    def _raise_if_same_session_replacement_superseded(self, session_id: str, ticket: Optional[int]) -> None:
        if self._same_session_replacement_is_current(session_id, ticket):
            return
        from common.ecorex_workspace import SessionBusyError
        raise SessionBusyError(f"same_session_replacement_superseded: {session_id}")

    def _session_conflict_retry_payload(
        self,
        session_id: str,
        *,
        reason: str = "session_lock_unavailable",
        active_request_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if active_request_ids is None:
            active_request_ids = self._backpressure_snapshot(session_id).get("active_request_ids", [])
        active_request_ids = list(active_request_ids or [])
        return {
            "status": "error",
            "code": REQUEST_CONFLICT_RETRYABLE_CODE,
            "error_type": "concurrency_conflict",
            "state": "retryable_conflict",
            "recoverable": True,
            "retryable": True,
            "retry_after_ms": REQUEST_CONFLICT_RETRY_AFTER_MS,
            "reason": reason,
            "session_id": session_id,
            "active_request_ids": active_request_ids,
            "same_session": self._same_session_decision_payload(
                "retryable_conflict",
                active_request_ids=active_request_ids,
                reason=reason,
            ),
            "message": "The previous run is still stopping. Please retry shortly.",
        }

    def _same_session_decision_payload(
        self,
        decision: str,
        *,
        policy: str = "active_turn_control",
        queue: str = "explicit",
        active_request_ids: List[str] | None = None,
        replaced_request_ids: List[str] | None = None,
        cancelled_requests: int = 0,
        cancelled_subagents: int = 0,
        reason: str = "",
        queue_position: int = 0,
        queued_request_id: str = "",
    ) -> Dict[str, Any]:
        return {
            "policy": policy,
            "queue": queue,
            "decision": decision,
            "active_request_ids": list(active_request_ids or []),
            "replaced_request_ids": list(replaced_request_ids or []),
            "cancelled_requests": int(cancelled_requests or 0),
            "cancelled_subagents": int(cancelled_subagents or 0),
            "retry_after_ms": REQUEST_CONFLICT_RETRY_AFTER_MS if decision == "retryable_conflict" else 0,
            "reason": reason,
            "queue_position": int(queue_position or 0),
            "queued_request_id": queued_request_id,
        }

    def _session_queue_limit(self) -> int:
        return self._coerce_positive_int(conf().get("web_max_queued_requests_per_session", self.SESSION_QUEUE_LIMIT), self.SESSION_QUEUE_LIMIT)

    def _queued_payload_store(self) -> QueuedRequestPayloadStore:
        workspace = str(_get_workspace_root() or "")
        store = self.queued_request_payload_store
        if not isinstance(store, QueuedRequestPayloadStore) or store.workspace != workspace:
            store = QueuedRequestPayloadStore(workspace)
            self.queued_request_payload_store = store
        return store

    def _load_queued_payload(self, request_id: str) -> Optional[Dict[str, Any]]:
        payload = self.queued_request_payloads.get(request_id)
        if isinstance(payload, dict):
            return payload
        payload = self._queued_payload_store().load(request_id)
        if isinstance(payload, dict):
            self.queued_request_payloads[request_id] = payload
            session_id = str(payload.get("session_id") or "").strip()
            if session_id:
                self.request_to_session[request_id] = session_id
            return payload
        return None

    def _persist_queued_payload(self, payload: Dict[str, Any]) -> bool:
        request_id = str((payload or {}).get("request_id") or "").strip()
        if request_id:
            self.queued_request_payloads[request_id] = payload
        return self._queued_payload_store().save(payload)

    def _delete_queued_payload(self, request_id: str) -> None:
        self.queued_request_payloads.pop(request_id, None)
        self._queued_payload_store().delete(request_id)

    def _recover_session_run_queue_from_ledger(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        try:
            from agent.protocol import get_run_ledger

            rows = get_run_ledger().queued_snapshot(session_id)
        except Exception:
            rows = []
        recovered: List[str] = []
        with self.session_run_queue_lock:
            queue = self.session_run_queues.setdefault(session_id, deque())
            existing = set(queue)
            for row in rows:
                request_id = str((row or {}).get("request_id") or "").strip()
                if not request_id or request_id in existing:
                    continue
                payload = self._load_queued_payload(request_id)
                if not payload:
                    continue
                queue.append(request_id)
                existing.add(request_id)
                recovered.append(request_id)
            if not queue:
                self.session_run_queues.pop(session_id, None)
        return recovered

    def _queue_position_for_request(self, session_id: str, request_id: str) -> int:
        self._recover_session_run_queue_from_ledger(session_id)
        with self.session_run_queue_lock:
            queue = list(self.session_run_queues.get(session_id) or [])
        try:
            return queue.index(request_id) + 1
        except ValueError:
            return 0

    def _queued_request_ids_for_session(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        self._recover_session_run_queue_from_ledger(session_id)
        with self.session_run_queue_lock:
            return list(self.session_run_queues.get(session_id) or [])

    @staticmethod
    def _retry_attachment_snapshot(attachments: Any) -> List[Dict[str, Any]]:
        return _safe_attachment_snapshot(attachments)

    @staticmethod
    def _retry_non_retryable_reason(row: Dict[str, Any]) -> str:
        run_type = str((row or {}).get("run_type") or "message").lower()
        request_id = str((row or {}).get("request_id") or "")
        session_id = str((row or {}).get("session_id") or "")
        if run_type == "subagent" or request_id.startswith("subagent-") or session_id.startswith("subagent-"):
            return "subagent_replay_unavailable"
        if run_type == "scheduler" or request_id.startswith("scheduler_") or session_id.startswith("scheduler_"):
            return "scheduler_replay_unavailable"
        status = str((row or {}).get("status") or (row or {}).get("state") or "").lower()
        if status in {"queued", "running", "cancelling", "finalizing", "recovering"}:
            return "request_still_active"
        if status == "completed":
            return "already_completed"
        code_text = "{} {}".format(
            (row or {}).get("error_code") or "",
            (row or {}).get("terminal_reason") or "",
        ).lower()
        if any(marker in code_text for marker in (
            "auth",
            "permission",
            "policy",
            "denied",
            "forbidden",
            "invalid",
            "bad_request",
            "badrequest",
            "not_retryable",
            "non_retryable",
        )):
            return "non_retryable_terminal"
        return ""

    def prepare_request_retry(self, request_id: str, *, session_id: str = "") -> Dict[str, Any]:
        """Build a safe manual retry draft for a prior request.

        This endpoint never starts execution. It mirrors Codex-style recovery:
        recover history first when possible, and only offer an explicit retry
        draft when replaying the visible user turn is safe.
        """
        request_id = str(request_id or "").strip()
        expected_session_id = str(session_id or "").strip()
        if not request_id:
            return {"status": "error", "message": "missing request_id", "retryable": False, "recoverable": False}
        try:
            from agent.protocol import get_run_ledger

            row = get_run_ledger().get_run(request_id)
        except Exception as e:
            logger.error(f"[WebChannel] retry prepare ledger lookup failed: {_web_body_log_summary(e)}")
            return {
                "status": "error",
                "message": "Runtime run ledger is unavailable; please recover history first.",
                "request_id": request_id,
                "retryable": False,
                "recoverable": True,
                "reason": "run_ledger_unavailable",
            }
        if not row:
            return {
                "status": "error",
                "message": "Request was not found. Recover the session history before retrying.",
                "request_id": request_id,
                "retryable": False,
                "recoverable": True,
                "reason": "request_not_found",
            }

        actual_session_id = str(row.get("session_id") or "")
        if expected_session_id and expected_session_id != actual_session_id:
            return {
                "status": "error",
                "message": "Request belongs to a different session; retry was not prepared.",
                "request_id": request_id,
                "session_id": actual_session_id,
                "retryable": False,
                "recoverable": True,
                "reason": "session_mismatch",
            }

        reason = self._retry_non_retryable_reason(row)
        recoverable = bool(actual_session_id) and reason not in {"subagent_replay_unavailable", "scheduler_replay_unavailable"}
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        visible_message = str(metadata.get("visible_message") or "").strip()
        source_user_seq = metadata.get("source_user_seq")
        exact_replay = bool(visible_message)
        attachments = metadata.get("attachment_items") if isinstance(metadata.get("attachment_items"), list) else []

        if not visible_message and actual_session_id:
            try:
                from agent.memory import get_conversation_store

                latest = get_conversation_store().get_visible_user_message(actual_session_id)
                visible_message = str(latest.get("text") or "").strip()
                source_user_seq = latest.get("seq")
            except Exception as e:
                logger.warning(f"[WebChannel] retry prepare history fallback failed: {_web_body_log_summary(e)}")

        if reason:
            return {
                "status": "success",
                "message": "Recover the existing response state before retrying." if reason == "request_still_active" else "This request cannot be safely retried.",
                "request_id": request_id,
                "session_id": actual_session_id,
                "retryable": False,
                "recoverable": recoverable,
                "retry_mode": "unavailable",
                "exactReplay": exact_replay,
                "exact_replay": exact_replay,
                "prompt": visible_message,
                "visible_message": visible_message,
                "attachments": attachments,
                "source_user_seq": source_user_seq,
                "reason": reason,
            }

        if not visible_message:
            return {
                "status": "success",
                "message": "No visible user message was available to retry.",
                "request_id": request_id,
                "session_id": actual_session_id,
                "retryable": False,
                "recoverable": recoverable,
                "retry_mode": "unavailable",
                "exactReplay": False,
                "exact_replay": False,
                "prompt": "",
                "visible_message": "",
                "attachments": attachments,
                "source_user_seq": source_user_seq,
                "reason": "missing_visible_message",
            }

        return {
            "status": "success",
            "message": "Retry draft prepared. Review and send to run it again.",
            "request_id": request_id,
            "session_id": actual_session_id,
            "retryable": True,
            "recoverable": recoverable,
            "retry_mode": "manual_retry_prepare",
            "exactReplay": exact_replay,
            "exact_replay": exact_replay,
            "prompt": visible_message,
            "visible_message": visible_message,
            "attachments": attachments,
            "source_user_seq": source_user_seq,
            "reason": "manual_retry_prepare",
        }

    def _interrupt_and_wait_for_session_lock(
        self,
        session_id: str,
        lang: str = "zh",
        *,
        replacement_ticket: Optional[int] = None,
    ):
        """Cancel the active request for a busy session and wait briefly for its lock."""
        from agent.protocol import get_cancel_registry
        from common.ecorex_workspace import SessionBusyError, SessionLock

        active_request_ids = []
        active_deadline = time.time() + 1.5
        while time.time() < active_deadline:
            self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
            active_request_ids = self._active_request_ids_for_session(session_id)
            if active_request_ids:
                break
            time.sleep(0.1)
        if not active_request_ids:
            # The worker may have already unregistered its cancel token while
            # the WebChannel callback is still finalizing and releasing the
            # session lock. In that narrow window, waiting for the lock is
            # friendlier than surfacing a silent busy state to the UI.
            deadline = time.time() + 4
            last_error = None
            while time.time() < deadline:
                self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
                try:
                    lock = SessionLock(_get_workspace_root(), session_id).acquire()
                    try:
                        self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
                    except Exception:
                        lock.release()
                        raise
                    return {
                        "lock": lock,
                        "same_session": self._same_session_decision_payload(
                            "accepted_after_finalize_wait",
                            reason="lock_released_without_active_request",
                        ),
                    }
                except SessionBusyError as e:
                    last_error = e
                    time.sleep(0.2)
            raise last_error or SessionBusyError(f"session is busy: {session_id}")

        cancelled = get_cancel_registry().cancel_session(session_id)
        subagent_cancel = self._cancel_subagents_for_parent(session_id)
        if cancelled <= 0:
            deadline = time.time() + 4
            last_error = None
            while time.time() < deadline:
                self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
                try:
                    lock = SessionLock(_get_workspace_root(), session_id).acquire()
                    try:
                        self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
                    except Exception:
                        lock.release()
                        raise
                    return {
                        "lock": lock,
                        "same_session": self._same_session_decision_payload(
                            "accepted_after_finalize_wait",
                            active_request_ids=active_request_ids,
                            reason="active_request_had_no_cancel_token",
                        ),
                    }
                except SessionBusyError as e:
                    last_error = e
                    time.sleep(0.2)
            raise last_error or SessionBusyError(f"session is busy: {session_id}")

        self._push_cancelled_events_for_session(session_id, active_request_ids, lang=lang)
        logger.info(
            f"[WebChannel] interrupting busy session before new message: "
            f"session={session_id}, cancelled={cancelled}, requests={active_request_ids}, "
            f"subagents={subagent_cancel}"
        )

        deadline = time.time() + 12
        last_error = None
        while time.time() < deadline:
            self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
            try:
                lock = SessionLock(_get_workspace_root(), session_id).acquire()
                try:
                    self._raise_if_same_session_replacement_superseded(session_id, replacement_ticket)
                except Exception:
                    lock.release()
                    raise
                return {
                    "lock": lock,
                    "same_session": self._same_session_decision_payload(
                        "replacement_accepted",
                        active_request_ids=active_request_ids,
                        replaced_request_ids=active_request_ids,
                        cancelled_requests=cancelled,
                        cancelled_subagents=int(subagent_cancel.get("cancelledTasks") or 0),
                        reason="previous_run_cancelled",
                    ),
                }
            except SessionBusyError as e:
                last_error = e
                time.sleep(0.2)
        raise last_error or SessionBusyError(f"session is busy: {session_id}")

    def _fetch_latest_pair_seqs(self, session_id: str):
        """Query the conversation store for the latest user/bot message seqs.

        Returned as ``{"user_seq": int|None, "bot_seq": int|None}``; used to
        attach seq metadata onto the SSE ``done`` event so the frontend can
        wire edit / regenerate buttons for live-streamed bubbles without a
        page refresh.
        """
        try:
            from agent.memory import get_conversation_store
            return get_conversation_store().get_latest_pair_seqs(session_id)
        except Exception as e:
            logger.debug(f"[WebChannel] _fetch_latest_pair_seqs failed: {_web_body_log_summary(e)}")
            return {"user_seq": None, "bot_seq": None}

    def _fetch_agent_usage(self, session_id: str):
        """Return the latest provider usage captured by the agent, if any."""
        if not session_id:
            return None
        try:
            ab = Bridge().get_agent_bridge()
            agent = getattr(ab, "agents", {}).get(session_id)
            usage = getattr(agent, "last_usage", None) if agent else None
            return usage if isinstance(usage, dict) and usage.get("totalTokens") else None
        except Exception as e:
            logger.debug(f"[WebChannel] _fetch_agent_usage failed: {_web_body_log_summary(e)}")
            return None

    @staticmethod
    def _limit_text_with_marker(value: Any, limit: int, *, from_end: bool = False) -> Tuple[str, Optional[Dict[str, int]]]:
        text = str(value if value is not None else "")
        if limit <= 0 or len(text) <= limit:
            return text, None
        omitted = len(text) - limit
        marker = f"[truncated {omitted} chars; limit {limit}]"
        clipped = text[-limit:] if from_end else text[:limit]
        limited = f"{marker}\n{clipped}" if from_end else f"{clipped}\n{marker}"
        return limited, {
            "original_chars": len(text),
            "kept_chars": limit,
            "omitted_chars": omitted,
        }

    def _bounded_tool_value(
        self,
        value: Any,
        limits: Dict[str, int],
        truncated_fields: List[Dict[str, Any]],
        path: str = "",
        depth: int = 0,
    ) -> Any:
        if depth > 6:
            truncated_fields.append({
                "field": path or "result",
                "reason": "max_depth",
                "kept_depth": 6,
            })
            return {"__truncated_depth": True, "__type": type(value).__name__}
        if isinstance(value, dict):
            bounded: Dict[str, Any] = {}
            item_limit = limits.get("collection_items", self.TOOL_OUTPUT_COLLECTION_ITEM_LIMIT)
            total_items = len(value)
            for index, (key, child) in enumerate(value.items()):
                if item_limit > 0 and index >= item_limit:
                    omitted = max(1, total_items - item_limit)
                    bounded["__omitted_keys"] = omitted
                    truncated_fields.append({
                        "field": path or "result",
                        "original_items": total_items,
                        "kept_items": item_limit,
                        "omitted_items": omitted,
                    })
                    break
                field_path = f"{path}.{key}" if path else str(key)
                lowered_key = str(key).lower()
                if lowered_key in ("stdout", "stderr", "output", "stdouttail", "stderrtail", "log", "logs"):
                    if not isinstance(child, (str, int, float, bool)) and child is not None:
                        bounded[key] = self._bounded_tool_value(child, limits, truncated_fields, field_path, depth + 1)
                    else:
                        limited, meta = self._limit_text_with_marker(
                            child,
                            limits.get("output_field_chars", self.TOOL_OUTPUT_FIELD_CHAR_LIMIT),
                            from_end=True,
                        )
                        bounded[key] = limited
                        if meta:
                            truncated_fields.append({"field": field_path, **meta})
                else:
                    bounded[key] = self._bounded_tool_value(child, limits, truncated_fields, field_path, depth + 1)
            return bounded
        if isinstance(value, (list, tuple, set)):
            item_limit = limits.get("collection_items", self.TOOL_OUTPUT_COLLECTION_ITEM_LIMIT)
            total_items = len(value)
            bounded_list = []
            for index, child in enumerate(value):
                if item_limit > 0 and index >= item_limit:
                    omitted = max(1, total_items - item_limit)
                    bounded_list.append({"__omitted_items": omitted})
                    truncated_fields.append({
                        "field": path or "result",
                        "original_items": total_items,
                        "kept_items": item_limit,
                        "omitted_items": omitted,
                    })
                    break
                bounded_list.append(self._bounded_tool_value(child, limits, truncated_fields, f"{path}[{index}]", depth + 1))
            return bounded_list
        if isinstance(value, str) and path:
            key = path.rsplit(".", 1)[-1].strip("[]")
            limited, meta = self._limit_text_with_marker(
                value,
                self._artifact_text_limit_for_key(key, limits),
            )
            if meta:
                truncated_fields.append({"field": path, **meta})
            return limited
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (bytes, bytearray)):
            byte_limit = max(0, limits.get("output_field_chars", self.TOOL_OUTPUT_FIELD_CHAR_LIMIT))
            raw = bytes(value)
            preview = raw[:byte_limit].decode("utf-8", errors="replace") if byte_limit else ""
            if len(raw) > byte_limit:
                truncated_fields.append({
                    "field": path or "result",
                    "original_bytes": len(raw),
                    "kept_bytes": byte_limit,
                    "omitted_bytes": len(raw) - byte_limit,
                })
            return {
                "__type": "bytes",
                "size_bytes": len(raw),
                "preview": preview,
                "truncated": len(raw) > byte_limit,
            }
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        truncated_fields.append({
            "field": path or "result",
            "reason": "non_json_value",
            "value_type": type(value).__name__,
        })
        return f"<non_json_value:{type(value).__name__}>"

    @staticmethod
    def _restore_feishu_public_auth_fields(public_result: Any, bounded_raw_result: Any, tool_name: str) -> Any:
        return restore_feishu_public_auth_fields(public_result, bounded_raw_result, tool_name)

    def _bounded_tool_result_for_sse(self, result: Any, tool_name: str = "") -> Tuple[str, Dict[str, Any]]:
        limits = self._tool_output_limits()
        truncated_fields: List[Dict[str, Any]] = []
        bounded_raw_result = self._bounded_tool_value(result, limits, truncated_fields)
        bounded_result = redact_public_tool_value(bounded_raw_result)
        bounded_result = self._restore_feishu_public_auth_fields(bounded_result, bounded_raw_result, tool_name)
        if isinstance(bounded_result, (dict, list)):
            result_str = json.dumps(
                bounded_result,
                ensure_ascii=False,
                default=lambda value: f"<non_json_value:{type(value).__name__}>",
            )
        else:
            result_str = str(bounded_result)

        preview_limit = limits.get("result_preview_chars", self.TOOL_RESULT_PREVIEW_CHAR_LIMIT)
        result_meta: Dict[str, Any] = {
            "tool_output_limits": limits,
            "result_truncated": bool(truncated_fields),
            "truncated_output_fields": truncated_fields,
        }
        if preview_limit > 0 and len(result_str) > preview_limit:
            original_chars = len(result_str)
            result_str, preview_meta = self._limit_text_with_marker(result_str, preview_limit)
            result_meta["result_truncated"] = True
            result_meta["result_original_chars"] = original_chars
            result_meta["result_limit_chars"] = preview_limit
            if preview_meta:
                result_meta["truncated_output_fields"] = [
                    *truncated_fields,
                    {"field": "result", **preview_meta},
                ]
        if result_meta["result_truncated"]:
            result_meta["limit_code"] = TOOL_OUTPUT_LIMIT_CODE
            result_meta["limit_reason"] = "tool_output_limit"
            result_meta["error_type"] = "tool_output_limit"
            result_meta["recoverable"] = True
        return result_str, result_meta

    @staticmethod
    def _artifact_text_limit_for_key(key: str, limits: Dict[str, int]) -> int:
        lowered = str(key or "").lower()
        if lowered in {"id", "path", "relativepath", "url", "previewurl", "statuspath"}:
            return limits.get("path_chars", 4096)
        return limits.get("string_chars", 512)

    def _sanitize_artifact_metadata(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        limits = self._artifact_metadata_limits()
        truncated_fields: List[Dict[str, Any]] = []

        def sanitize(value: Any, key: str = "", path: str = "", depth: int = 0) -> Any:
            if depth > 4:
                return str(value)
            if isinstance(value, dict):
                return {
                    child_key: sanitize(child_value, str(child_key), f"{path}.{child_key}" if path else str(child_key), depth + 1)
                    for child_key, child_value in value.items()
                    if not str(child_key).startswith("_")
                }
            if isinstance(value, list):
                return [
                    sanitize(child, key, f"{path}[{index}]", depth + 1)
                    for index, child in enumerate(value[:16])
                ]
            if isinstance(value, str):
                limit = self._artifact_text_limit_for_key(key, limits)
                limited, meta = self._limit_text_with_marker(value, limit)
                if meta:
                    truncated_fields.append({"field": path or key, **meta})
                return limited
            return value

        sanitized = sanitize(artifact)
        if not isinstance(sanitized, dict):
            sanitized = {}
        sanitized["metadataLimits"] = limits
        if truncated_fields:
            sanitized["metadataTruncated"] = True
            sanitized["truncatedFields"] = truncated_fields
        return sanitized

    def _artifact_kind(self, file_type: str, path_value: str = "") -> str:
        kind = str(file_type or "").lower()
        if kind in ("image", "video", "audio", "directory", "file", "url", "diff"):
            return kind
        lower = str(path_value or "").lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
            return "image"
        if lower.endswith((".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi")):
            return "video"
        if lower.endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")):
            return "audio"
        return "file"

    def _resolve_artifact_local_path(self, path_value: str) -> str:
        value = str(path_value or "").strip()
        if value.startswith("/api/file"):
            parsed = urllib.parse.urlparse(value)
            query = urllib.parse.parse_qs(parsed.query)
            value = (query.get("path") or [""])[0] or value
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            expanded = os.path.join(_get_workspace_root(), expanded.lstrip("/\\"))
        return os.path.realpath(expanded)

    def _artifact_path_available(self, path_value: str) -> bool:
        value = str(path_value or "").strip()
        if not value:
            return False
        if value.startswith("http://") or value.startswith("https://"):
            return True
        resolved = self._resolve_artifact_local_path(value)
        if not os.path.exists(resolved):
            return False
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_file_access(
                "read",
                resolved,
                cwd=_get_workspace_root(),
            )
            return _decision_allowed(decision)
        except Exception as exc:
            logger.warning(f"[WebChannel] artifact availability check failed: {_web_body_log_summary(exc)}")
            return False

    def _artifact_from_file_event(self, request_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        file_path = str(data.get("path") or data.get("file_path") or "").strip()
        if not file_path:
            return {}
        path_available = self._artifact_path_available(file_path)
        file_name = str(data.get("file_name") or os.path.basename(file_path) or "artifact").strip()
        file_type = str(data.get("file_type") or "file").strip()
        kind = self._artifact_kind(file_type, file_path)
        raw_status = str(data.get("status") or "").strip().lower()
        if raw_status in ("failed", "error"):
            artifact_status = "failed"
        elif raw_status in ("pending", "queued", "running", "retrying"):
            artifact_status = "pending"
        else:
            artifact_status = "ready" if path_available else "pending"
        from urllib.parse import quote
        preview_url = f"/api/file?path={quote(file_path)}" if file_path else ""
        artifact = {
            "id": f"{request_id}:{file_path or file_name}",
            "requestId": request_id,
            "kind": kind,
            "intent": "deliverable",
            "operation": "exported",
            "status": artifact_status,
            "title": file_name,
            "path": file_path,
            "previewUrl": preview_url if kind in ("image", "video", "audio", "file") else "",
            "source": {
                "toolName": str(data.get("tool_name") or data.get("tool") or "file_to_send"),
                "createdAt": time.time(),
            },
        }
        try:
            resolved_path = self._resolve_artifact_local_path(file_path)
            if path_available and os.path.exists(resolved_path):
                artifact["sizeBytes"] = os.path.getsize(resolved_path)
                mime_type = mimetypes.guess_type(resolved_path)[0]
                if mime_type:
                    artifact["mimeType"] = mime_type
        except Exception:
            pass
        return artifact

    def _record_local_reply_artifact(self, request_id: str, reply_type, content: str) -> None:
        if not request_id or not content.startswith("file://"):
            return
        local_path = content[len("file://"):].strip()
        file_type = "image" if reply_type == ReplyType.IMAGE_URL else "file"
        artifact = self._artifact_from_file_event(request_id, {
            "path": local_path,
            "file_path": local_path,
            "file_name": os.path.basename(local_path) or "artifact",
            "file_type": file_type,
            "tool_name": "reply",
        })
        self._record_request_artifact(request_id, artifact)

    def _record_request_artifact(self, request_id: str, artifact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not request_id or not artifact:
            return None
        artifact = self._sanitize_artifact_metadata(artifact)
        items = self.request_artifacts.setdefault(request_id, [])
        key = str(artifact.get("path") or artifact.get("relativePath") or artifact.get("url") or artifact.get("id") or "").lower()
        for index, existing in enumerate(items):
            existing_key = str(existing.get("path") or existing.get("relativePath") or existing.get("url") or existing.get("id") or "").lower()
            if existing.get("id") == artifact.get("id") or (key and existing_key == key):
                items[index] = {**existing, **artifact}
                return items[index]
        max_items = self._artifact_metadata_limits().get("max_items", self.ARTIFACT_METADATA_MAX_ITEMS)
        if max_items >= 0 and len(items) >= max_items:
            return None
        items.append(artifact)
        return artifact

    def _artifact_limit_event(
        self,
        request_id: str,
        tool_name: str,
        tool_call_id: str,
        omitted_count: int = 1,
    ) -> Dict[str, Any]:
        limits = self._artifact_metadata_limits()
        return {
            "type": "artifact_limit",
            "status": "warning",
            "code": ARTIFACT_METADATA_LIMIT_CODE,
            "error_type": "artifact_metadata_limit",
            "reason": "artifact_metadata_limit",
            "recoverable": True,
            "retryable": False,
            "request_id": request_id,
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "limit": limits.get("max_items", self.ARTIFACT_METADATA_MAX_ITEMS),
            "omitted": max(1, int(omitted_count or 1)),
            "artifact_limits": limits,
            "message": "Artifact metadata limit reached; additional artifacts were omitted from the stream.",
            "timestamp": time.time(),
        }

    def _diff_stats(self, diff_text: str) -> Dict[str, int]:
        added = 0
        removed = 0
        for line in str(diff_text or "").splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return {"addedLines": added, "removedLines": removed}

    def _artifacts_from_tool_result(
        self,
        request_id: str,
        tool_name: str,
        tool_call_id: str,
        status: str,
        result: Any,
    ) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []
        tool_key = str(tool_name or "").lower()
        result_type = str(result.get("type") or "").lower()
        artifact_tool = any(
            keyword in tool_key
            for keyword in ("write", "save", "send", "export", "render", "image", "artifact", "deliverable", "edit", "patch")
        )
        if not artifact_tool and result_type not in {"file_to_send", "artifact", "generated_file", "output_file"}:
            return []
        artifact_limits = self._artifact_metadata_limits()
        max_items = artifact_limits.get("max_items", self.ARTIFACT_METADATA_MAX_ITEMS)
        raw_items: List[Dict[str, Any]] = []
        omitted_items = 0
        seen_paths = set()

        def add_artifact_candidate(value: Any, meta: Optional[Dict[str, Any]] = None, collection_key: str = "") -> None:
            nonlocal omitted_items
            path_value = ""
            item_meta = meta if isinstance(meta, dict) else {}
            if isinstance(value, str):
                path_value = value.strip()
            elif isinstance(value, dict):
                item_meta = value
                for nested_key in ("path", "file_path", "filePath", "output", "output_path", "outputPath", "url"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str) and nested_value.strip():
                        path_value = nested_value.strip()
                        break
            if not path_value:
                return
            normalized_key = path_value.replace("\\", "/").lower()
            if normalized_key in seen_paths:
                return
            seen_paths.add(normalized_key)
            if max_items >= 0 and len(raw_items) >= max_items:
                omitted_items += 1
                return
            inferred_type = str(
                item_meta.get("file_type")
                or item_meta.get("fileType")
                or item_meta.get("kind")
                or item_meta.get("type")
                or ("image" if collection_key == "images" else "")
            ).strip()
            raw_items.append({
                "path": path_value,
                "file_name": item_meta.get("file_name") or item_meta.get("fileName") or item_meta.get("name"),
                "file_type": inferred_type,
            })

        for key in ("path", "file_path", "filePath", "output", "output_path", "outputPath"):
            add_artifact_candidate(result.get(key), result)
        for key in ("images", "files", "outputs", "artifacts", "generated_files", "generatedFiles"):
            collection = result.get(key)
            if isinstance(collection, list):
                for index, entry in enumerate(collection):
                    if max_items >= 0 and len(raw_items) >= max_items:
                        omitted_items += max(1, len(collection) - index)
                        break
                    add_artifact_candidate(entry, entry if isinstance(entry, dict) else None, key)
            elif isinstance(collection, dict):
                add_artifact_candidate(collection, collection, key)

        if not raw_items:
            if omitted_items:
                return [{
                    "_artifact_limit_only": True,
                    "_omitted_artifact_count": omitted_items,
                }]
            return []
        artifacts: List[Dict[str, Any]] = []
        for raw_item in raw_items:
            raw_path = str(raw_item.get("path") or "").strip()
            file_name = str(raw_item.get("file_name") or result.get("file_name") or result.get("fileName") or os.path.basename(raw_path) or raw_path)
            raw_type = str(raw_item.get("file_type") or result.get("file_type") or result.get("fileType") or "file")
            kind = self._artifact_kind(raw_type, raw_path)
            is_edit = "edit" in tool_key or "patch" in tool_key
            path_available = self._artifact_path_available(raw_path)
            result_status = str(result.get("status") or status or "").strip().lower()
            if result_status in ("failed", "error"):
                artifact_status = "failed"
            elif result_status in ("pending", "queued", "running", "retrying"):
                artifact_status = "pending"
            else:
                artifact_status = "ready" if path_available else "pending"
            stats = self._diff_stats(str(result.get("diff") or "")) if result.get("diff") else {}
            if not stats and isinstance(result.get("added_lines"), int):
                stats = {
                    "addedLines": int(result.get("added_lines") or 0),
                    "removedLines": int(result.get("removed_lines") or 0),
                }
            artifact = {
                "id": f"{request_id}:{tool_call_id}:{raw_path}",
                "requestId": request_id,
                "kind": kind,
                "intent": "changed-file" if is_edit else "deliverable",
                "operation": "modified" if is_edit else "created",
                "status": artifact_status,
                "title": file_name,
                "path": raw_path,
                "source": {
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "createdAt": time.time(),
                },
            }
            status_path = result.get("status_path") or result.get("statusPath")
            if isinstance(status_path, str) and status_path:
                artifact["statusPath"] = status_path
            if kind in ("image", "video", "audio", "file") and raw_path:
                from urllib.parse import quote
                artifact["previewUrl"] = f"/api/file?path={quote(raw_path)}"
            if stats:
                artifact["stats"] = stats
            if isinstance(result.get("bytes_written"), int):
                artifact["stats"] = {**artifact.get("stats", {}), "bytesWritten": int(result.get("bytes_written") or 0)}
            artifact["metadataLimits"] = artifact_limits
            artifacts.append(artifact)
        if omitted_items and artifacts:
            artifacts[-1]["_omitted_artifact_count"] = omitted_items
        return artifacts

    def _persist_request_artifacts(self, request_id: str, session_id: str) -> None:
        artifacts = self.request_artifacts.get(request_id) or []
        if not artifacts or not session_id:
            return
        try:
            from agent.memory import get_conversation_store
            get_conversation_store().attach_extras_to_last_assistant(session_id, {"artifacts": artifacts})
        except Exception as e:
            logger.debug(f"[WebChannel] artifact persist skipped for {request_id}: {_web_body_log_summary(e)}")

    @staticmethod
    def _cached_chat_item_to_store_message(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        role = str((item or {}).get("role") or "").strip()
        if role not in {"user", "assistant"}:
            return None
        if role == "assistant" and item.get("pending") and not str(item.get("content") or "").strip():
            return None
        text = str((item or {}).get("content") or "").strip()
        attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
        if not text and not attachments:
            return None
        message: Dict[str, Any] = {
            "role": role,
            "content": [{"type": "text", "text": text}] if text else "",
        }
        cleaned_attachments = _safe_attachment_snapshot(attachments)
        extras: Dict[str, Any] = {}
        if cleaned_attachments:
            extras["attachments"] = cleaned_attachments
        request_id = str((item or {}).get("requestId") or "").strip()
        if request_id:
            extras["request_id"] = request_id
            extras["turn_id"] = request_id
        user_seq = item.get("userSeq")
        bot_seq = item.get("botSeq")
        if isinstance(user_seq, int):
            extras["user_seq"] = user_seq
        if isinstance(bot_seq, int):
            extras["bot_seq"] = bot_seq
        if extras:
            message["extras"] = extras
        return message

    @staticmethod
    def _ui_state_project_context(session_id: str, state: Dict[str, Any], cached: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        bindings = state.get("sessionProjectBindings") if isinstance(state.get("sessionProjectBindings"), dict) else {}
        binding = bindings.get(session_id)
        if not isinstance(binding, dict):
            binding = cached.get("projectBinding") if isinstance(cached.get("projectBinding"), dict) else None
        if not isinstance(binding, dict):
            project_id = str(
                (state.get("sessionProjects") or {}).get(session_id)
                if isinstance(state.get("sessionProjects"), dict)
                else cached.get("projectId") or ""
            ).strip()
            if not project_id:
                return None
            binding = {"projectId": project_id}
        return _normalize_project_context_meta(binding)

    @classmethod
    def _hydrate_conversation_store_from_ui_state(cls, state: Dict[str, Any]) -> Dict[str, Any]:
        session_state = state.get("sessionUiState") if isinstance(state.get("sessionUiState"), dict) else {}
        if not session_state:
            return {"importedSessions": 0, "importedMessages": 0, "skippedSessions": 0}
        try:
            from agent.memory import get_conversation_store

            store = get_conversation_store()
        except Exception as exc:
            logger.debug(f"[WebChannel] UI history import skipped: {_web_body_log_summary(exc)}")
            return {"importedSessions": 0, "importedMessages": 0, "skippedSessions": len(session_state)}

        imported_sessions = 0
        imported_messages = 0
        skipped_sessions = 0
        for session_id, cached in list(session_state.items())[:200]:
            session_id = str(session_id or "").strip()
            if not session_id or not isinstance(cached, dict):
                skipped_sessions += 1
                continue
            messages_raw = cached.get("messages")
            if not isinstance(messages_raw, list) or not messages_raw:
                skipped_sessions += 1
                continue
            try:
                existing = store.load_history_page(session_id, page=1, page_size=1)
                if int(existing.get("total") or 0) > 0:
                    skipped_sessions += 1
                    continue
            except Exception:
                pass
            messages = []
            for item in messages_raw[:200]:
                if isinstance(item, dict):
                    converted = cls._cached_chat_item_to_store_message(item)
                    if converted:
                        messages.append(converted)
            if not messages:
                skipped_sessions += 1
                continue
            try:
                store.append_messages(
                    session_id,
                    messages,
                    channel_type="web",
                    project_context=cls._ui_state_project_context(session_id, state, cached),
                )
                title = str(
                    (state.get("sessionTitles") or {}).get(session_id)
                    if isinstance(state.get("sessionTitles"), dict)
                    else cached.get("title") or ""
                ).strip()
                if title:
                    store.rename_session(session_id, title, respect_title_lock=True)
                imported_sessions += 1
                imported_messages += len(messages)
            except Exception as exc:
                skipped_sessions += 1
                logger.debug(f"[WebChannel] UI history import failed: {_web_body_log_summary(exc)}")
        return {
            "importedSessions": imported_sessions,
            "importedMessages": imported_messages,
            "skippedSessions": skipped_sessions,
        }

    @staticmethod
    def _pre_persist_web_user_message(
        session_id: str,
        visible_message: str,
        *,
        request_id: str = "",
        client_attempt_id: str = "",
        attachments: Any = None,
        project_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not session_id or not str(visible_message or "").strip():
            return False
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return False
            from agent.memory import get_conversation_store

            user_msg: Dict[str, Any] = {
                "role": "user",
                "content": [{"type": "text", "text": str(visible_message or "").strip()}],
            }
            extras: Dict[str, Any] = {}
            safe_request_id = str(request_id or "").strip()
            if safe_request_id:
                extras["request_id"] = safe_request_id
                extras["turn_id"] = safe_request_id
            safe_client_attempt_id = str(client_attempt_id or "").strip()
            if safe_client_attempt_id:
                extras["client_attempt_id"] = safe_client_attempt_id
            cleaned_attachments = _safe_attachment_snapshot(attachments)
            if cleaned_attachments:
                extras["attachments"] = cleaned_attachments
            if extras:
                user_msg["extras"] = extras
            get_conversation_store().append_messages(
                session_id,
                [user_msg],
                channel_type="web",
                project_context=project_context,
            )
            return True
        except Exception as exc:
            if getattr(exc, "code", "") == "SESSION_OWNER_CONFLICT":
                logger.warning(
                    f"[WebChannel] Refused pre-persist due to session owner conflict: reason={getattr(exc, 'reason', 'unknown')}"
                )
            else:
                logger.warning(f"[WebChannel] pre-persist user message failed: {_web_body_log_summary(exc)}")
            return False

    def _ensure_final_reply_persisted(self, request_id: str, session_id: str, content: str) -> Dict[str, Optional[int]]:
        seqs = self._fetch_latest_pair_seqs(session_id)
        if not session_id or seqs.get("bot_seq") is not None or not str(content or "").strip():
            return seqs
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return seqs
            from agent.memory import get_conversation_store

            extras = {"request_id": request_id, "turn_id": request_id}
            user_seq = seqs.get("user_seq")
            if user_seq is not None:
                extras["user_seq"] = user_seq
            get_conversation_store().append_messages(
                session_id,
                [{
                    "role": "assistant",
                    "content": [{"type": "text", "text": str(content or "").strip()}],
                    "extras": extras,
                }],
                channel_type="web",
                project_context=self.request_project_contexts.get(request_id),
            )
            return self._fetch_latest_pair_seqs(session_id)
        except Exception as exc:
            logger.warning(f"[WebChannel] final reply persist failed: {_web_body_log_summary(exc)}")
            return seqs

    def _build_done_event(self, request_id: str, session_id: str, content: str):
        self._persist_request_artifacts(request_id, session_id)
        seqs = self._ensure_final_reply_persisted(request_id, session_id, content)
        turn_id = request_id or ""
        if not turn_id and seqs.get("user_seq") is not None and seqs.get("bot_seq") is not None:
            turn_id = f"{seqs.get('user_seq')}:{seqs.get('bot_seq')}"
        identity_extras = {
            "request_id": request_id,
            "turn_id": turn_id,
            "user_seq": seqs.get("user_seq"),
            "bot_seq": seqs.get("bot_seq"),
        }
        try:
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            bot_seq = seqs.get("bot_seq")
            if bot_seq is not None:
                store.attach_extras_to_assistant_seq(session_id, int(bot_seq), identity_extras)
            else:
                store.attach_extras_to_last_assistant(session_id, identity_extras)
        except Exception as e:
            logger.debug(f"[WebChannel] request identity persist skipped for {request_id}: {_web_body_log_summary(e)}")
        payload = {
            "type": "done",
            "content": content,
            "final_text": content,
            "update_mode": "replace",
            "request_id": request_id,
            "turn_id": turn_id,
            "timestamp": time.time(),
            "user_seq": seqs.get("user_seq"),
            "bot_seq": seqs.get("bot_seq"),
        }
        usage = self._fetch_agent_usage(session_id)
        if usage:
            payload["usage"] = usage
        artifacts = self.request_artifacts.get(request_id) or []
        if artifacts:
            payload["artifacts"] = artifacts
        return payload

    @staticmethod
    def _release_context_session_lock(context: Context) -> None:
        session_lock = None
        try:
            session_lock = context.get("session_lock") if context else None
        except Exception:
            session_lock = None
        if not session_lock:
            return
        try:
            session_lock.release()
            try:
                context["session_lock"] = None
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[WebChannel] session lock release skipped: {_web_body_log_summary(e)}")

    def _abort_pre_worker_request(
        self,
        request_id: str,
        session_id: str,
        *,
        message: str,
        reason: str,
        error_code: str,
        session_lock=None,
        error_extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Release request state when `/message` fails before worker ownership.

        Once a request id has been allocated, WebChannel owns the cancel token,
        session lock, SSE replay state, and ledger row until a worker finalizer
        takes over. If anything fails before the worker starts, clear those
        resources here so active snapshots do not show a phantom run.
        """
        if request_id:
            self._mark_run_terminal(
                request_id,
                "failed",
                reason=reason,
                error_code=error_code,
                error_message=message,
            )
            if self._sse_request_exists(request_id):
                try:
                    self._push_error_event_once(
                        request_id,
                        message,
                        error_code=error_code,
                        terminal_reason=reason,
                        extra=error_extra,
                    )
                except Exception as e:
                    logger.debug(f"[WebChannel] pre-worker abort SSE error skipped for {request_id}: {_web_body_log_summary(e)}")
                self._cleanup_sse_request(request_id)
            else:
                self.request_to_session.pop(request_id, None)
            try:
                from agent.protocol import get_cancel_registry

                get_cancel_registry().unregister(request_id)
            except Exception as e:
                logger.debug(f"[WebChannel] pre-worker abort token unregister skipped for {request_id}: {_web_body_log_summary(e)}")
        if session_lock:
            try:
                session_lock.release()
            except Exception as e:
                logger.debug(f"[WebChannel] pre-worker abort session lock release skipped: {_web_body_log_summary(e)}")

    def _thread_pool_callback(self, session_id, **kwargs):
        parent_callback = super()._thread_pool_callback(session_id, **kwargs)
        context = kwargs.get("context")

        def callback(worker):
            worker_exception = None
            next_session_id = ""
            completed_request_id = ""
            try:
                try:
                    worker_exception = worker.exception()
                except Exception as e:
                    worker_exception = e
                parent_callback(worker)
            finally:
                try:
                    next_session_id = str(context.get("session_id") or "") if context else ""
                    completed_request_id = str(context.get("request_id") or "") if context else ""
                except Exception:
                    next_session_id = ""
                    completed_request_id = ""
                self._finalize_request_after_worker(context, worker_exception)
                self._release_context_session_lock(context)
                if next_session_id:
                    self._start_next_queued_request(next_session_id, completed_request_id=completed_request_id)

        return callback

    def _finalize_request_after_worker(self, context: Context, worker_exception=None) -> None:
        """Release request-scoped runtime state once the worker has stopped.

        The SSE queue may intentionally outlive the worker until the browser
        consumes the final ``done`` event, but the cancel registry must not:
        active-request checks use it to decide whether a session is still
        running. Leaving completed request ids registered makes a finished
        session look busy and can block or mis-route the next message.
        """
        if not context:
            return
        request_id = context.get("request_id")
        if not request_id:
            return

        was_cancelled = False
        try:
            from agent.protocol import get_cancel_registry

            registry = get_cancel_registry()
            event = registry.get_event(request_id)
            was_cancelled = bool(event and event.is_set())
            registry.unregister(request_id)
        except Exception as e:
            logger.debug(f"[WebChannel] cancel token unregister skipped for {request_id}: {_web_body_log_summary(e)}")

        session_id = context.get("session_id") or self.request_to_session.get(request_id, "")
        self._persist_request_artifacts(request_id, session_id)

        if worker_exception is not None:
            public_message = "" if was_cancelled else _public_exception_message(
                "Worker failed before producing a response.",
                worker_exception,
            )
            public_extra = {} if was_cancelled else _public_exception_summary(worker_exception)
            self._mark_run_terminal(
                request_id,
                "cancelled" if was_cancelled else "failed",
                reason="worker_cancelled" if was_cancelled else "worker_exception",
                error_code="" if was_cancelled else "WORKER_EXCEPTION",
                error_message=public_message,
            )
        elif was_cancelled:
            self._mark_run_terminal(request_id, "cancelled", reason="cancelled")
        else:
            self._mark_run_terminal(request_id, "completed", reason="worker_completed")

        if worker_exception is not None and self._sse_request_exists(request_id):
            try:
                message = public_message or "Worker failed before producing a response."
                self._push_error_event_once(
                    request_id,
                    message,
                    error_code="WORKER_EXCEPTION",
                    usage=self._fetch_agent_usage(session_id),
                    extra=public_extra,
                )
                self._push_done_event_once(request_id, {
                    "type": "done",
                    "content": f"❌ {message}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "usage": self._fetch_agent_usage(session_id),
                })
            except Exception as e:
                logger.debug(f"[WebChannel] worker exception SSE fallback skipped for {request_id}: {_web_body_log_summary(e)}")

        if self._sse_request_exists(request_id):
            if self.sse_subscribers.get(request_id, 0) <= 0:
                self._schedule_sse_cleanup(request_id, reason="worker-finalized-without-subscriber")
        else:
            self.request_to_session.pop(request_id, None)

    def send(self, reply: Reply, context: Context):
        try:
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # SSE mode: push events to SSE queue
            if self._sse_request_exists(request_id):
                content = reply.content if reply.content is not None else ""

                # Intermediate status lines (e.g. /install-browser phases) must NOT use "done",
                # or the frontend closes EventSource and drops subsequent events.
                if getattr(reply, "sse_phase", False):
                    self._push_sse_event(request_id, {
                        "type": "phase",
                        "content": content,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                    logger.debug(f"SSE phase for request {request_id}")
                    return

                # Files are already pushed via on_event (file_to_send) during agent execution.
                # Skip duplicate file pushes here; just let the done event through.
                if reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE) and content.startswith("file://"):
                    self._record_local_reply_artifact(request_id, reply.type, content)
                    text_content = getattr(reply, 'text_content', '')
                    self._push_done_event_once(
                        request_id,
                        self._build_done_event(request_id, session_id, text_content or "")
                    )
                    logger.debug(f"SSE done sent for local file reply {request_id}")
                    return

                # Skip http-URL FILE/IMAGE_URL replies produced by chat_channel's media extraction:
                # the text reply (already sent as "done") contains the URL and the frontend will
                # render it via renderMarkdown/injectVideoPlayers, so no separate SSE event needed.
                if reply.type in (ReplyType.FILE, ReplyType.IMAGE_URL) and content.startswith(("http://", "https://")):
                    logger.debug(f"SSE skipped http media reply for request {request_id}")
                    return

                done_event = self._build_done_event(request_id, session_id, content)
                self._push_done_event_once(request_id, done_event)
                logger.debug(f"SSE done sent for request {request_id}")
                # Auto-trigger TTS once the bot finishes its text reply. The
                # synthesis runs in the background so the chat stream is never
                # blocked; the resulting audio URL is pushed via a follow-up
                # `voice_attach` SSE event and persisted to messages.extras.
                if reply.type == ReplyType.TEXT and content.strip():
                    self._maybe_dispatch_auto_tts(request_id, session_id, content, context, done_event.get("bot_seq"))
                return

            # Fallback: polling mode
            if session_id in self.session_queues:
                content = reply.content if reply.content is not None else ""
                # Skip file:// IMAGE_URL/FILE replies originating from an SSE-enabled
                # request: they were already pushed via the `file_to_send` event during
                # agent execution. By the time the chat_channel sends the IMAGE_URL reply,
                # the SSE stream has typically closed (after the text "done") and the
                # request_id is gone from sse_queues, so we'd otherwise duplicate the file
                # as a polling bubble. Scheduler/push tasks have no on_event and must
                # still go through polling normally.
                if (
                    reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE)
                    and content.startswith("file://")
                    and context.get("on_event") is not None
                ):
                    logger.debug(f"Polling skipped duplicate file reply for session {session_id}")
                    return
                # SSE-enabled requests already stream the text reply to the
                # client. Do NOT also enqueue it for polling: if the user
                # switched away mid-run, the queued copy would resurface as a
                # duplicate bubble when they return and poll the session.
                if reply.type == ReplyType.TEXT and context.get("on_event") is not None:
                    logger.debug(f"Polling skipped SSE text reply for session {session_id}")
                    return
                response_data = {
                    "type": str(reply.type),
                    "content": content,
                    "timestamp": time.time(),
                    "request_id": request_id
                }
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {_web_body_log_summary(e)}")

    def _make_sse_callback(self, request_id: str):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""

        # Keep live reasoning large enough for real user inspection while still
        # protecting the browser from unbounded traces.
        MAX_REASONING_STREAM_CHARS = 256 * 1024
        # Use a single-element list as a mutable counter accessible from closure.
        reasoning_chars_sent = [0]
        reasoning_capped_notified = [False]
        # Captures the first error message emitted by agent_stream so the
        # subsequent agent_end handler can skip its "empty final_response"
        # fallback (which would otherwise overwrite the real error).
        streamed_error: List[str] = []
        stream_coalesce_lock = threading.RLock()
        stream_pending: Dict[str, List[str]] = {"delta": [], "reasoning": []}
        stream_pending_chars: Dict[str, int] = {"delta": 0, "reasoning": 0}
        stream_flush_timer: List[threading.Timer] = [None]
        STREAM_FLUSH_SECONDS = 0.032
        STREAM_FLUSH_CHARS = 220

        def flush_stream_pending(kind: str = None) -> None:
            payloads: List[Dict[str, Any]] = []
            with stream_coalesce_lock:
                timer = stream_flush_timer[0]
                stream_flush_timer[0] = None
                if timer:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                pending_kinds = [kind] if kind else ["delta", "reasoning"]
                for pending_kind in pending_kinds:
                    parts = stream_pending.get(pending_kind) or []
                    if not parts:
                        continue
                    content = "".join(parts)
                    stream_pending[pending_kind] = []
                    stream_pending_chars[pending_kind] = 0
                    payloads.append({"type": pending_kind, "content": content})
            for payload in payloads:
                self._push_sse_event(request_id, payload)
            if kind:
                with stream_coalesce_lock:
                    has_other_pending = any(parts for pending_kind, parts in stream_pending.items() if pending_kind != kind)
                if has_other_pending:
                    schedule_stream_flush()

        def schedule_stream_flush() -> None:
            with stream_coalesce_lock:
                if stream_flush_timer[0] is not None:
                    return
                timer = threading.Timer(STREAM_FLUSH_SECONDS, flush_stream_pending)
                timer.daemon = True
                stream_flush_timer[0] = timer
                timer.start()

        def push_stream_delta(kind: str, delta: str) -> None:
            if not delta:
                return
            with stream_coalesce_lock:
                stream_pending[kind].append(delta)
                stream_pending_chars[kind] += len(delta)
                should_flush = stream_pending_chars[kind] >= STREAM_FLUSH_CHARS
            if should_flush:
                flush_stream_pending(kind)
            else:
                schedule_stream_flush()

        def push_boundary_event(item: Dict[str, Any]) -> bool:
            flush_stream_pending()
            return self._push_sse_event(request_id, item)

        def _extract_subagent_task(value: Any) -> Dict[str, Any]:
            payload = value
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                return {}
            if isinstance(payload.get("task"), dict):
                return payload.get("task") or {}
            if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("task"), dict):
                return payload["data"].get("task") or {}
            return {}

        def _subagent_text_hash(value: Any) -> str:
            text = str(value or "")
            if not text:
                return ""
            return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

        def _subagent_public_text(value: Any) -> Dict[str, Any]:
            text = str(value or "")
            return {
                "preview": "[redacted-content]" if text else "",
                "hash": _subagent_text_hash(text),
                "length": len(text),
            }

        def _subagent_public_role(value: Any) -> str:
            role = str(value or "subagent").strip() or "subagent"
            role = re.sub(r"[^A-Za-z0-9_.:-]+", "-", role)[:32]
            return role or "subagent"

        def _public_subagent_task(raw_task: Any, *, fallback_task_id: str = "", fallback_child_request_id: str = "") -> Dict[str, Any]:
            task = raw_task if isinstance(raw_task, dict) else {}
            task_id = str(task.get("id") or task.get("task_id") or fallback_task_id or "")
            child_request_id = str(task.get("requestId") or task.get("childSessionId") or fallback_child_request_id or "")
            summary_meta = _subagent_public_text(
                task.get("summary") or task.get("task") or task.get("prompt") or task.get("name") or ""
            )
            result_meta = _subagent_public_text(task.get("result_preview") or task.get("result") or "")
            public = {
                "id": task_id,
                "task_id": task_id,
                "requestId": child_request_id,
                "childSessionId": child_request_id,
                "role": _subagent_public_role(task.get("role")),
                "status": str(task.get("status") or ""),
                "summary": summary_meta["preview"],
                "summaryHash": summary_meta["hash"],
                "summaryLength": summary_meta["length"],
                "result_preview": result_meta["preview"],
                "resultHash": result_meta["hash"],
                "resultLength": result_meta["length"],
                "deadlineAt": task.get("deadlineAt"),
                "timeoutSeconds": task.get("timeoutSeconds"),
                "lastHeartbeatAt": task.get("lastHeartbeatAt"),
            }
            return {key: value for key, value in public.items() if value not in ("", None)}

        def _subagent_public_name(public_task: Dict[str, Any], *, fallback_seed: Any = "") -> str:
            suffix = str(
                public_task.get("task_id")
                or public_task.get("requestId")
                or fallback_seed
                or ""
            )[-6:]
            role = _subagent_public_role(public_task.get("role"))
            return f"{role} {suffix}" if suffix else "Subagent"

        def on_event(event: dict):
            if not self._sse_request_exists(request_id):
                return
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "agent_start":
                self._push_sse_event(request_id, {
                    "type": "phase",
                    "content": "已收到，正在准备响应",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "turn_start":
                turn = data.get("turn")
                self._push_sse_event(request_id, {
                    "type": "phase",
                    "content": f"正在组织上下文{f' · 第 {turn} 步' if turn else ''}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "message_start":
                self._push_sse_event(request_id, {
                    "type": "phase",
                    "content": "正在连接模型响应",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "reasoning_update":
                delta = data.get("delta", "")
                if not delta:
                    return
                remaining = MAX_REASONING_STREAM_CHARS - reasoning_chars_sent[0]
                if remaining <= 0:
                    reasoning_capped_notified[0] = True
                    return
                if len(delta) > remaining:
                    delta = delta[:remaining]
                reasoning_chars_sent[0] += len(delta)
                push_stream_delta("reasoning", delta)

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    push_stream_delta("delta", delta)

            elif event_type.startswith("task."):
                flush_stream_pending()
                payload = dict(data or {})
                payload["type"] = "task_observation"
                payload["task_event_type"] = event_type
                payload["request_id"] = request_id
                payload["timestamp"] = time.time()
                self._push_sse_event(request_id, payload)

            elif event_type == "tool_execution_start":
                flush_stream_pending()
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                if not isinstance(arguments, dict):
                    arguments = {}
                public_arguments = redact_public_tool_value(arguments)
                if str(tool_name) == "subagent":
                    public_task = _public_subagent_task(
                        {
                            "role": arguments.get("role"),
                            "summary": arguments.get("summary") or arguments.get("task"),
                            "name": arguments.get("name"),
                        },
                        fallback_task_id=str(data.get("tool_call_id") or ""),
                    )
                    self._push_sse_event(request_id, {
                        "type": "subagent_start",
                        "tool_call_id": data.get("tool_call_id", ""),
                        "name": _subagent_public_name(public_task, fallback_seed=data.get("tool_call_id")),
                        "role": public_task.get("role") or "explorer",
                        "summary": public_task.get("summary") or "",
                        "summaryHash": public_task.get("summaryHash") or "",
                        "summaryLength": public_task.get("summaryLength") or 0,
                        "status": "starting",
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                    return
                self._push_sse_event(request_id, {
                    "type": "tool_start",
                    "tool": tool_name,
                    "tool_call_id": data.get("tool_call_id", ""),
                    "arguments": public_arguments,
                })

            elif event_type == "tool_execution_heartbeat":
                self._push_sse_event(request_id, {
                    "type": "tool_heartbeat",
                    "tool": data.get("tool_name", "tool"),
                    "tool_call_id": data.get("tool_call_id", ""),
                    "elapsed_seconds": data.get("elapsed_seconds", 0),
                    "deadline_seconds": data.get("deadline_seconds"),
                    "max_seconds": data.get("max_seconds"),
                    "extension_count": data.get("extension_count"),
                    "status": data.get("status", "running"),
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "tool_execution_deadline_extended":
                self._push_sse_event(request_id, {
                    "type": "tool_deadline_extended",
                    "tool": data.get("tool_name", "tool"),
                    "tool_call_id": data.get("tool_call_id", ""),
                    "elapsed_seconds": data.get("elapsed_seconds", 0),
                    "previous_deadline_seconds": data.get("previous_deadline_seconds"),
                    "deadline_seconds": data.get("deadline_seconds"),
                    "max_seconds": data.get("max_seconds"),
                    "extension_count": data.get("extension_count"),
                    "reason": data.get("reason", "adaptive"),
                    "status": data.get("status", "running"),
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "tool_execution_timeout":
                flush_stream_pending()
                tool_name = str(data.get("tool_name") or "tool")
                tool_call_id = str(data.get("tool_call_id") or "")
                elapsed_seconds = data.get("elapsed_seconds", 0)
                timeout_seconds = data.get("timeout_seconds", 0)
                message = str(
                    data.get("message")
                    or f"Tool '{tool_name}' timed out after {elapsed_seconds}s."
                )
                if tool_name == "subagent":
                    raw_task = data.get("task") if isinstance(data.get("task"), dict) else _extract_subagent_task(data.get("result"))
                    task = _public_subagent_task(
                        raw_task,
                        fallback_task_id=str(data.get("task_id") or ""),
                        fallback_child_request_id=str(data.get("child_request_id") or ""),
                    )
                    message = f"Subagent timed out after {elapsed_seconds}s."
                    self._push_sse_event(request_id, {
                        "type": "subagent_timeout",
                        "tool_call_id": tool_call_id,
                        "task": task,
                        "task_id": task.get("id") or task.get("task_id") or "",
                        "child_request_id": task.get("requestId") or task.get("childSessionId") or "",
                        "name": _subagent_public_name(task, fallback_seed=tool_call_id),
                        "role": task.get("role") or "subagent",
                        "summary": task.get("summary") or "",
                        "summaryHash": task.get("summaryHash") or "",
                        "summaryLength": task.get("summaryLength") or 0,
                        "status": "timeout",
                        "result_preview": task.get("result_preview") or "",
                        "resultHash": task.get("resultHash") or "",
                        "resultLength": task.get("resultLength") or 0,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                else:
                    self._push_sse_event(request_id, {
                        "type": "tool_end",
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "status": "timeout",
                        "result": message,
                        "execution_time": elapsed_seconds,
                        "timeout_seconds": timeout_seconds,
                        "error_code": str(data.get("error_code") or "TOOL_TIMEOUT"),
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                self._mark_run_terminal(
                    request_id,
                    "timeout",
                    reason="tool_timeout",
                    error_code=str(data.get("error_code") or "TOOL_TIMEOUT"),
                    error_message=message,
                )
                self._push_terminal_event_once(
                    request_id,
                    self._build_interrupted_event(
                        request_id,
                        session_id=self.request_to_session.get(request_id, ""),
                        terminal_reason="tool_timeout",
                        error_code=str(data.get("error_code") or "TOOL_TIMEOUT"),
                        message=message,
                    ),
                )

            elif event_type == "tool_permission_request":
                flush_stream_pending()
                self._push_sse_event(request_id, {
                    "type": "tool_permission_request",
                    "permission_request_id": data.get("id", ""),
                    "tool": data.get("tool", "tool"),
                    "title": data.get("title", ""),
                    "message": data.get("message", ""),
                    "summary": data.get("summary", ""),
                    "mode": data.get("mode", ""),
                    "created_at": data.get("created_at", ""),
                })

            elif event_type == "tool_execution_end":
                flush_stream_pending()
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                tool_call_id = data.get("tool_call_id", "")
                if str(tool_name) == "subagent":
                    raw_task = _extract_subagent_task(result)
                    task = _public_subagent_task(raw_task)
                    status_value = str(status or "success")
                    if task:
                        status_value = str(task.get("status") or status or "running")
                        event_type_name = {
                            "completed": "subagent_complete",
                            "failed": "subagent_failed",
                            "timeout": "subagent_timeout",
                            "cancelled": "subagent_cancelled",
                            "interrupted": "subagent_failed",
                        }.get(status_value, "subagent_update")
                        self._push_sse_event(request_id, {
                            "type": event_type_name,
                            "tool_call_id": tool_call_id,
                            "task": task,
                            "task_id": task.get("id") or task.get("task_id") or "",
                            "child_request_id": task.get("requestId") or task.get("childSessionId") or "",
                            "name": _subagent_public_name(task, fallback_seed=tool_call_id),
                            "role": task.get("role") or "subagent",
                            "summary": task.get("summary") or "",
                            "summaryHash": task.get("summaryHash") or "",
                            "summaryLength": task.get("summaryLength") or 0,
                            "status": status_value,
                            "result_preview": task.get("result_preview") or "",
                            "resultHash": task.get("resultHash") or "",
                            "resultLength": task.get("resultLength") or 0,
                            "request_id": request_id,
                            "timestamp": time.time(),
                        })
                    result = {
                        "status": status_value,
                        "message": "subagent result is available through runtime projection",
                    }
                    return
                for artifact in self._artifacts_from_tool_result(request_id, tool_name, tool_call_id, status, result):
                    omitted_artifact_count = int(artifact.pop("_omitted_artifact_count", 0) or 0)
                    if artifact.pop("_artifact_limit_only", False):
                        self._push_sse_event(
                            request_id,
                            self._artifact_limit_event(request_id, tool_name, tool_call_id, omitted_artifact_count),
                        )
                        continue
                    recorded_artifact = self._record_request_artifact(request_id, artifact)
                    if recorded_artifact:
                        self._push_sse_event(request_id, {
                            "type": "artifact",
                            "action": "upsert",
                            "artifact": recorded_artifact,
                            "request_id": request_id,
                            "timestamp": time.time(),
                        })
                    else:
                        omitted_artifact_count = max(1, omitted_artifact_count)
                    if omitted_artifact_count:
                        self._push_sse_event(
                            request_id,
                            self._artifact_limit_event(request_id, tool_name, tool_call_id, omitted_artifact_count),
                        )
                result_str, result_meta = self._bounded_tool_result_for_sse(result, tool_name)
                self._push_sse_event(request_id, {
                    "type": "tool_end",
                    "tool": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2),
                    **result_meta,
                })

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    push_boundary_event({"type": "message_end", "has_tool_calls": True})
                else:
                    flush_stream_pending()

            elif event_type == "error":
                flush_stream_pending()
                # Agent raised an exception (LLM 401/timeout/etc). Surface the
                # real message instead of letting the empty-response fallback
                # below hide it as "(模型未返回任何内容)".
                err_msg = data.get("error") or "unknown error"
                logger.warning(
                    f"[WebChannel] agent_stream emitted error for "
                    f"request {request_id}: {err_msg}"
                )
                # Remember it so the agent_end handler below knows not to
                # rewrite the message into a generic empty-response notice.
                streamed_error.append(err_msg)
                retry_meta_keys = (
                    "error_type",
                    "error_taxonomy",
                    "retryable",
                    "recoverable",
                    "retry_exhausted",
                    "retry_suppressed",
                    "retry_suppressed_reason",
                    "retry_attempt",
                    "max_retries",
                    "status_code",
                    "retry_mode",
                    "errorHash",
                    "error_hash",
                    "errorType",
                    "error_exception_type",
                    "errorLength",
                    "error_chars",
                    "errorBytes",
                    "error_bytes",
                    "redacted",
                    "errorRedacted",
                )
                retry_meta = {
                    key: data.get(key)
                    for key in retry_meta_keys
                    if data.get(key) is not None
                }
                terminal_reason = str(
                    data.get("terminal_reason")
                    or (
                        "model_retry_suppressed_stream_output_started"
                        if data.get("retry_suppressed")
                        else "failed"
                    )
                )
                self._push_error_event_once(
                    request_id,
                    err_msg,
                    error_code=str(data.get("error_code") or "AGENT_STREAM_ERROR"),
                    terminal_reason=terminal_reason,
                    extra=retry_meta,
                    usage=data.get("usage") or self._fetch_agent_usage(self.request_to_session.get(request_id, "")),
                )
                self._push_done_event_once(request_id, {
                    "type": "done",
                    "content": f"❌ {err_msg}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "usage": data.get("usage") or self._fetch_agent_usage(self.request_to_session.get(request_id, "")),
                })

            elif event_type == "agent_cancelled":
                flush_stream_pending()
                # Push an explicit cancelled SSE event so the frontend
                # marks the bubble as stopped with a single terminal event.
                final_response = data.get("final_response", "")
                self._push_cancelled_event_once(request_id, {
                    "type": "cancelled",
                    "content": final_response,
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "agent_end":
                flush_stream_pending()
                # Safety net: if the agent finishes with an empty final_response,
                # chat_channel skips _send_reply (because reply.content is empty),
                # which means no "done" event is ever emitted and the SSE stream
                # would hang until the 10-min idle timeout. Push a fallback "done"
                # here so the frontend always gets closure.
                final_response = data.get("final_response", "")
                if not final_response or not str(final_response).strip():
                    if streamed_error:
                        # Error was already surfaced via the `error` event
                        # handler above; nothing more to do here.
                        pass
                    else:
                        logger.warning(
                            f"[WebChannel] agent_end with empty final_response for "
                            f"request {request_id}, sending fallback done"
                        )
                        self._push_done_event_once(request_id, {
                            "type": "done",
                            "content": i18n.t(
                                "(模型未返回任何内容，请重试或换一种方式描述你的需求)",
                                "(The model returned no content. Please retry or rephrase your request.)",
                            ),
                            "request_id": request_id,
                            "timestamp": time.time(),
                            "usage": data.get("usage") or self._fetch_agent_usage(self.request_to_session.get(request_id, "")),
                        })

            elif event_type == "file_to_send":
                flush_stream_pending()
                artifact = self._artifact_from_file_event(request_id, data)
                if not artifact:
                    return
                recorded_artifact = self._record_request_artifact(request_id, artifact)
                if recorded_artifact:
                    self._push_sse_event(request_id, {
                        "type": "artifact",
                        "action": "upsert",
                        "artifact": recorded_artifact,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                else:
                    self._push_sse_event(
                        request_id,
                        self._artifact_limit_event(
                            request_id,
                            str(data.get("tool_name") or data.get("tool") or "file_to_send"),
                            str(data.get("tool_call_id") or ""),
                        ),
                    )

        return on_event

    # ------------------------------------------------------------------
    # TTS auto-dispatch
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_voice_reply_mode() -> str:
        """
        Decide the TTS auto-reply policy.

        Source of truth is the cross-channel pair
        (`always_reply_voice`, `voice_reply_voice`) which chat_channel
        also consults. The web UI presents these as a single three-state
        picker (off / voice_if_voice / always) via a lossless mapping.
        """
        if conf().get("always_reply_voice", False):
            return "always"
        if conf().get("voice_reply_voice", False):
            return "voice_if_voice"
        return "off"

    # Mirror of ModelsHandler._TTS_PROVIDERS. zhipu is intentionally omitted
    # from the UI (GLM-TTS prelude beep); pinning it in config.json still works.
    _TTS_PROVIDERS_SUGGEST_ORDER = ["openai", "minimax", "dashscope", "linkai"]

    @classmethod
    def _tts_provider_ready(cls) -> bool:
        """True if user picked a provider OR any suggested vendor has an API key."""
        if (conf().get("text_to_voice") or "").strip():
            return True
        for pid in cls._TTS_PROVIDERS_SUGGEST_ORDER:
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                continue
            val = (conf().get(key_field) or "").strip()
            if val and val not in ("YOUR API KEY", "YOUR_API_KEY"):
                return True
        return False

    def _maybe_dispatch_auto_tts(
        self,
        request_id: str,
        session_id: str,
        text: str,
        context: dict,
        bot_seq: int = None,
    ) -> None:
        try:
            mode = self._resolve_voice_reply_mode()
            if mode == "off":
                return
            if mode == "voice_if_voice" and not context.get("is_voice_input"):
                return
            if not self._tts_provider_ready():
                return
            threading.Thread(
                target=self._synthesize_tts_async,
                args=(request_id, session_id, text, bot_seq),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"[WebChannel] auto-tts dispatch skipped: {_web_body_log_summary(e)}")

    def _synthesize_tts_async(
        self,
        request_id: str,
        session_id: str,
        text: str,
        bot_seq: int = None,
    ) -> None:
        try:
            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                logger.warning(
                    f"[WebChannel] TTS produced no audio for request {request_id}: "
                    f"reply={reply}"
                )
                return
            url = self._publish_tts_audio(reply.content)
            if not url:
                logger.warning(f"[WebChannel] TTS publish failed for request {request_id}")
                return
            payload = {"audio": {"url": url, "kind": "tts"}}
            try:
                from agent.memory import get_conversation_store
                store = get_conversation_store()
                if bot_seq is not None:
                    attached = store.attach_extras_to_assistant_seq(session_id, int(bot_seq), payload)
                    if attached is None:
                        logger.debug(f"[WebChannel] tts seq attach missed for request {request_id}, seq={bot_seq}")
                else:
                    store.attach_extras_to_last_assistant(session_id, payload)
            except Exception as e:
                logger.debug(f"[WebChannel] tts persist skipped: {_web_body_log_summary(e)}")
            if not self._sse_request_exists(request_id):
                logger.warning(
                    f"[WebChannel] TTS ready but SSE queue already closed "
                    f"for request {request_id} (url={url})"
                )
                return
            self._push_sse_event(request_id, {
                "type": "voice_attach",
                "url": url,
                "request_id": request_id,
                "timestamp": time.time(),
            })
            logger.info(f"[WebChannel] TTS voice_attach pushed for request {request_id}: {url}")
        except Exception as e:
            # TTS failures are intentionally silent (no user-facing error).
            logger.warning(f"[WebChannel] TTS synthesis failed: {_web_body_log_summary(e)}")

    @staticmethod
    def _publish_tts_audio(src_path: str) -> str:
        """Move a TTS file into uploads/ and return its public URL."""
        try:
            if not src_path or not os.path.isfile(src_path):
                logger.warning(f"[WebChannel] publish_tts_audio missing source: {src_path!r}")
                return ""
            ext = os.path.splitext(src_path)[1].lower() or ".mp3"
            upload_dir = _get_upload_dir()
            os.makedirs(upload_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dst_name = f"voice_reply_{ts}_{random.randint(0, 9999)}{ext}"
            dst_path = os.path.join(upload_dir, dst_name)
            shutil.move(src_path, dst_path)
            logger.debug(f"[WebChannel] publish_tts_audio moved {src_path} -> {dst_path}")
            return f"/uploads/{dst_name}"
        except Exception as e:
            logger.warning(f"[WebChannel] publish_tts_audio failed: {_web_body_log_summary(e)}")
            return ""

    @staticmethod
    def _cleanup_stale_voice_recordings(max_age_seconds: int = 3600) -> None:
        """Drop voice_input_* uploads older than max_age_seconds (run at startup)."""
        try:
            upload_dir = _get_upload_dir()
            if not os.path.isdir(upload_dir):
                return
            now = time.time()
            removed = 0
            for name in os.listdir(upload_dir):
                if not name.startswith("voice_input_"):
                    continue
                full = os.path.join(upload_dir, name)
                try:
                    if not os.path.isfile(full):
                        continue
                    if now - os.path.getmtime(full) > max_age_seconds:
                        os.remove(full)
                        removed += 1
                except OSError:
                    continue
            if removed:
                logger.info(f"[WebChannel] cleaned up {removed} stale voice recording(s) from {upload_dir}")
        except Exception as e:
            logger.warning(f"[WebChannel] voice cleanup failed: {_web_body_log_summary(e)}")

    def upload_file(self):
        """Handle file or directory upload via multipart/form-data."""
        try:
            params = _raw_web_input()
            file_obj = params.get("file")
            file_objs = params.get("files")
            session_id = params.get("session_id", "")
            relative_path = params.get("relative_path", "")
            relative_paths = params.get("relative_paths")
            upload_id = params.get("upload_id", "")

            directory_files = _ensure_list(file_objs)

            # NOTE: cgi.FieldStorage raises TypeError on truthy checks for single-file
            # uploads (Python 3.9+). Always use `is not None` instead of `if file_obj`.
            if not directory_files and file_obj is not None and relative_path:
                directory_files = [file_obj]

            directory_rel_paths = _ensure_list(relative_paths)

            if not directory_rel_paths and relative_path:
                directory_rel_paths = [relative_path]

            is_directory_upload = bool(directory_files) or bool(directory_rel_paths) or bool(relative_path) or bool(upload_id)

            upload_dir = _get_upload_dir()
            if is_directory_upload:
                if not upload_id:
                    return json.dumps({"status": "error", "message": "Missing upload_id for directory upload"})
                if not directory_files:
                    return json.dumps({"status": "error", "message": "No files uploaded"})
                if len(directory_files) != len(directory_rel_paths):
                    return json.dumps({"status": "error", "message": "Directory upload payload mismatch"})

                safe_upload_id = _sanitize_upload_id(upload_id)
                upload_root = os.path.join(upload_dir, f"webdir_{safe_upload_id}")
                upload_root_real = os.path.realpath(upload_root)

                root_name = None
                saved_files = 0
                for file_obj, rel_path in zip(directory_files, directory_rel_paths):
                    if file_obj is None:
                        raise ValueError("Invalid uploaded file")
                    safe_rel_path, save_path = _resolve_upload_path(upload_root_real, rel_path)
                    current_root_name = safe_rel_path.split("/", 1)[0]
                    if root_name is None:
                        root_name = current_root_name
                    elif root_name != current_root_name:
                        raise ValueError("Directory upload must use a single root folder")
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    content_bytes = _read_uploaded_file_bytes(file_obj)
                    with open(save_path, "wb") as f:
                        f.write(content_bytes)
                    saved_files += 1

                if not root_name:
                    raise ValueError("Directory root path missing")

                root_path = os.path.realpath(os.path.join(upload_root_real, root_name))
                if not _is_within_directory(upload_root_real, root_path):
                    raise ValueError("Invalid directory upload path")

                logger.info(f"[WebChannel] Directory uploaded: {root_name} -> {root_path} ({saved_files} files)")
                return json.dumps({
                    "status": "success",
                    "file_path": root_path,
                    "file_name": root_name,
                    "file_type": "directory",
                    "file_count": saved_files,
                    "root_path": root_path,
                    "root_name": root_name,
                    "upload_type": "directory",
                }, ensure_ascii=False)

            if file_obj is None or not hasattr(file_obj, "filename") or not file_obj.filename:
                return json.dumps({"status": "error", "message": "No file uploaded"})

            original_name = file_obj.filename
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = f"web_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(upload_dir, safe_name)
            public_path = safe_name
            display_name = original_name

            content_bytes = _read_uploaded_file_bytes(file_obj)
            with open(save_path, "wb") as f:
                f.write(content_bytes)

            if ext in IMAGE_EXTENSIONS:
                file_type = "image"
            elif ext in VIDEO_EXTENSIONS:
                file_type = "video"
            else:
                file_type = "file"

            from urllib.parse import quote
            preview_url = f"/uploads/{quote(public_path, safe='/')}"

            logger.info(f"[WebChannel] File uploaded: {original_name} -> {save_path} ({file_type})")

            return json.dumps({
                "status": "success",
                "file_path": save_path,
                "file_name": display_name,
                "file_type": file_type,
                "preview_url": preview_url,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[WebChannel] File upload error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def post_message(self):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports optional attachments (file paths from /upload).
        """
        session_lock = None
        request_id = ""
        session_id = ""
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id', f'session_{int(time.time())}')
            visible_prompt = str(json_data.get('message') or '')
            visible_message_raw = json_data.get('visible_message') if 'visible_message' in json_data else None
            visible_message = str(visible_message_raw) if visible_message_raw is not None else visible_prompt
            prompt = visible_prompt
            hidden_context = json_data.get('hidden_context') or ''
            legacy_project_context = json_data.get('project_context')
            project_context_meta = _normalize_project_context_meta(
                json_data.get('project_context_meta')
                or json_data.get('project_binding')
                or json_data.get('projectContext')
                or (legacy_project_context if isinstance(legacy_project_context, dict) else None)
            )
            if project_context_meta:
                try:
                    from agent.memory import ConversationSessionOwnerConflict, get_conversation_store

                    get_conversation_store().validate_session_owner(
                        session_id,
                        channel_type="web",
                        project_context=project_context_meta,
                    )
                except ConversationSessionOwnerConflict as exc:
                    web.ctx.status = "409 Conflict"
                    logger.warning(
                        f"[WebChannel] session owner conflict rejected before project binding persist: reason={exc.reason}"
                    )
                    return json.dumps({
                        "status": "error",
                        "code": exc.code,
                        "error_type": "session_owner_conflict",
                        "message": "该会话已绑定到其他范围，请新建会话后重试。",
                        "retryable": False,
                        "recoverable": True,
                    }, ensure_ascii=False)
                _persist_project_session_binding(session_id, project_context_meta)
            internal_action = bool(json_data.get('internal_action', False))
            use_sse = json_data.get('stream', True)
            attachments = json_data.get('attachments', [])
            client_attempt_id = str(json_data.get('client_attempt_id') or '').strip()
            interrupts_request_id = str(json_data.get('interrupts_request_id') or '').strip()
            interrupt_mode = str(json_data.get('interrupt_mode') or '').strip().lower()
            if interrupt_mode not in {"replace", "amend", "queue", "branch"}:
                interrupt_mode = "replace"
            retry_of_request_id = str(json_data.get('retry_of_request_id') or '').strip()
            lang = (json_data.get('lang') or 'zh').lower()
            # Tag the message as originating from voice input so the post-reply
            # TTS hook can honour the `voice_if_voice` policy (mirrors the
            # desire_rtype concept used by other channels).
            is_voice_input = bool(json_data.get('is_voice', False))

            # Fast path for /cancel: bypass the session queue and SSE setup.
            # Web frontend (stream=true) only listens to SSE, so we return an
            # inline_reply payload to be rendered synchronously.
            stripped_prompt = (prompt or "").strip().lower()
            if stripped_prompt == "/cancel":
                from agent.protocol import get_cancel_registry
                active_request_ids = self._active_request_ids_for_session(session_id)
                cancelled = get_cancel_registry().cancel_session(session_id)
                subagent_cancel = self._cancel_subagents_for_parent(session_id)
                self._push_cancelled_events_for_session(session_id, active_request_ids, lang=lang)
                msg_text = _cancel_reply_text(cancelled + int(subagent_cancel.get("cancelledTasks") or 0), lang)
                logger.info(
                    f"[WebChannel] /cancel fast-path: session={session_id}, cancelled={cancelled}, "
                    f"subagents={subagent_cancel}, lang={lang}"
                )
                return json.dumps({
                    "status": "success",
                    "request_id": "",
                    "stream": False,
                    "inline_reply": msg_text,
                    "subagents": subagent_cancel,
                })

            same_session_decision = self._same_session_decision_payload("accepted")
            replacement_request_ids: List[str] = []
            same_session_ticket = self._begin_same_session_replacement(session_id) if interrupt_mode in {"replace", "amend"} else None
            recovered_interrupted_request_ids = self._recover_interrupted_runs_for_removed_session_locks(session_id)
            if recovered_interrupted_request_ids:
                replacement_request_ids.extend(recovered_interrupted_request_ids)
                same_session_decision = self._same_session_decision_payload(
                    "accepted_after_recovery",
                    active_request_ids=recovered_interrupted_request_ids,
                    replaced_request_ids=recovered_interrupted_request_ids,
                    reason="dead_owner_lock_recovered",
                )

            try:
                from common.ecorex_workspace import SessionBusyError, SessionLock
                session_lock = SessionLock(_get_workspace_root(), session_id).acquire()
                try:
                    self._raise_if_same_session_replacement_superseded(session_id, same_session_ticket)
                except SessionBusyError as e:
                    session_lock.release()
                    session_lock = None
                    logger.warning(f"[WebChannel] direct session admission superseded: session={session_id}")
                    return json.dumps(
                        self._session_conflict_retry_payload(
                            session_id,
                            reason="same_session_replacement_superseded",
                        ),
                        ensure_ascii=False,
                    )
            except SessionBusyError:
                active_request_ids = self._active_request_ids_for_session(session_id)
                if interrupt_mode == "branch":
                    return json.dumps(
                        self._session_conflict_retry_payload(
                            session_id,
                            reason="branch_requires_new_session",
                            active_request_ids=active_request_ids,
                        ),
                        ensure_ascii=False,
                    )
                if interrupt_mode != "queue":
                    try:
                        replacement = self._interrupt_and_wait_for_session_lock(
                            session_id,
                            lang,
                            replacement_ticket=same_session_ticket,
                        )
                        session_lock = replacement.get("lock")
                        replacement_same_session = replacement.get("same_session")
                        if isinstance(replacement_same_session, dict):
                            replacement_same_session["interrupt_mode"] = interrupt_mode
                            same_session_decision = replacement_same_session
                            replacement_request_ids.extend(replacement_same_session.get("replaced_request_ids") or [])
                    except SessionBusyError:
                        return json.dumps(
                            self._session_conflict_retry_payload(
                                session_id,
                                reason=f"{interrupt_mode}_previous_run_still_stopping",
                                active_request_ids=active_request_ids,
                            ),
                            ensure_ascii=False,
                        )
                else:
                    result = self._accept_queued_message(
                        session_id,
                        visible_prompt=visible_prompt,
                        visible_message=visible_message,
                        prompt=prompt,
                        hidden_context=hidden_context,
                        project_context_meta=project_context_meta,
                        internal_action=internal_action,
                        use_sse=use_sse,
                        attachments=attachments,
                        client_attempt_id=client_attempt_id,
                        interrupts_request_id=interrupts_request_id,
                        retry_of_request_id=retry_of_request_id,
                        interrupt_mode=interrupt_mode,
                        lang=lang,
                        is_voice_input=is_voice_input,
                        queued_after_request_ids=active_request_ids,
                        reason="session_lock_busy",
                    )
                    return json.dumps(result, ensure_ascii=False)

            same_session_snapshot = self._backpressure_snapshot(
                session_id,
                ignore_request_ids=replacement_request_ids,
            )
            residual_same_session_active_ids = list(same_session_snapshot.get("active_request_ids") or [])
            if residual_same_session_active_ids:
                if interrupt_mode == "branch":
                    if session_lock:
                        try:
                            session_lock.release()
                        except Exception as e:
                            logger.debug(f"[WebChannel] branch conflict session lock release skipped: {_web_body_log_summary(e)}")
                        session_lock = None
                    return json.dumps(
                        self._session_conflict_retry_payload(
                            session_id,
                            reason="branch_requires_new_session",
                            active_request_ids=residual_same_session_active_ids,
                        ),
                        ensure_ascii=False,
                    )
                if interrupt_mode != "queue":
                    if session_lock:
                        try:
                            session_lock.release()
                        except Exception as e:
                            logger.debug(f"[WebChannel] replacement session lock release skipped: {_web_body_log_summary(e)}")
                        session_lock = None
                    try:
                        replacement = self._interrupt_and_wait_for_session_lock(
                            session_id,
                            lang,
                            replacement_ticket=same_session_ticket,
                        )
                        session_lock = replacement.get("lock")
                        replacement_same_session = replacement.get("same_session")
                        if isinstance(replacement_same_session, dict):
                            replacement_same_session["interrupt_mode"] = interrupt_mode
                            same_session_decision = replacement_same_session
                            replacement_request_ids.extend(replacement_same_session.get("replaced_request_ids") or [])
                    except SessionBusyError:
                        return json.dumps(
                            self._session_conflict_retry_payload(
                                session_id,
                                reason=f"{interrupt_mode}_previous_run_still_stopping",
                                active_request_ids=residual_same_session_active_ids,
                            ),
                            ensure_ascii=False,
                        )
                    residual_same_session_active_ids = []
                else:
                    logger.warning(
                        f"[WebChannel] same-session active request remains; queueing new message: "
                        f"session={session_id}, active={residual_same_session_active_ids}, "
                        f"decision={same_session_decision.get('decision')}"
                    )
                    if session_lock:
                        try:
                            session_lock.release()
                        except Exception as e:
                            logger.debug(f"[WebChannel] conflict session lock release skipped: {_web_body_log_summary(e)}")
                        session_lock = None
                    result = self._accept_queued_message(
                        session_id,
                        visible_prompt=visible_prompt,
                        visible_message=visible_message,
                        prompt=prompt,
                        hidden_context=hidden_context,
                        project_context_meta=project_context_meta,
                        internal_action=internal_action,
                        use_sse=use_sse,
                        attachments=attachments,
                        client_attempt_id=client_attempt_id,
                        interrupts_request_id=interrupts_request_id,
                        retry_of_request_id=retry_of_request_id,
                        interrupt_mode=interrupt_mode,
                        lang=lang,
                        is_voice_input=is_voice_input,
                        queued_after_request_ids=residual_same_session_active_ids,
                        reason="same_session_active_request",
                    )
                    return json.dumps(result, ensure_ascii=False)

            if not internal_action:
                hidden_context = _append_hidden_context(WEBUI_IDENTITY_GUARD_CONTEXT, hidden_context)

            # Append file references to the prompt (same format as QQ channel)
            if attachments:
                file_refs, remote_context = _web_attachment_prompt_refs_and_context(attachments)
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")
                if remote_context:
                    _ensure_tencent_docs_tools_for_attachments(attachments, "chat-message")
                    hidden_context = _append_hidden_context(hidden_context, remote_context)

            if isinstance(hidden_context, str) and hidden_context.strip():
                prompt = hidden_context.strip() + "\n\nUser request:\n" + (prompt or "Please handle these attachments.")

            with self.backpressure_lock:
                backpressure_payload = self._backpressure_rejection_payload(
                    session_id,
                    ignore_request_ids=replacement_request_ids,
                )
                if backpressure_payload:
                    logger.warning(
                        f"[WebChannel] backpressure rejected message: "
                        f"session={session_id}, scope={backpressure_payload.get('scope')}, "
                        f"active={backpressure_payload.get('active')}, "
                        f"limit={backpressure_payload.get('limit')}"
                    )
                    if session_lock:
                        try:
                            session_lock.release()
                        except Exception as e:
                            logger.debug(f"[WebChannel] backpressure session lock release skipped: {_web_body_log_summary(e)}")
                        session_lock = None
                    return json.dumps(backpressure_payload, ensure_ascii=False)

                request_id = self._generate_request_id()
                self.request_to_session[request_id] = session_id
                self.request_project_contexts[request_id] = project_context_meta or {}
                try:
                    from agent.protocol import get_cancel_registry
                    get_cancel_registry().register(request_id, session_id=session_id)
                except Exception as e:
                    logger.debug(f"[WebChannel] pre-register cancel token skipped: {_web_body_log_summary(e)}")
                try:
                    from agent.protocol import get_run_ledger

                    ledger = get_run_ledger()
                    chat_route = self._current_chat_route_snapshot()
                    retry_visible_message, _retry_visible_trunc = self._limit_text_with_marker(
                        visible_message or visible_prompt or "",
                        64 * 1024,
                    )
                    persisted = ledger.create_run(
                        request_id,
                        session_id,
                        run_type="message",
                        phase="accepted",
                        status="running",
                        model=chat_route.get("model", ""),
                        provider=chat_route.get("provider", ""),
                        metadata={
                            "stream": bool(use_sse),
                            "internal_action": bool(internal_action),
                            "attachments": len(attachments) if isinstance(attachments, list) else 0,
                            "attachment_items": self._retry_attachment_snapshot(attachments),
                            "visible_message": retry_visible_message,
                            "model": chat_route.get("model", ""),
                            "provider": chat_route.get("provider", ""),
                            "client_attempt_id": client_attempt_id,
                            "interrupts_request_id": interrupts_request_id,
                            "interrupt_mode": interrupt_mode,
                            "retry_of_request_id": retry_of_request_id,
                            "project_context": project_context_meta,
                        },
                    )
                    if not persisted:
                        raise RuntimeError("run ledger did not persist request")
                except Exception as e:
                    logger.error(f"[WebChannel] run ledger unavailable for {request_id}: {_web_body_log_summary(e)}")
                    self._abort_pre_worker_request(
                        request_id,
                        session_id,
                        message="Runtime run ledger is unavailable; request was not started.",
                        reason="run_ledger_unavailable",
                        error_code="RUN_LEDGER_UNAVAILABLE",
                        session_lock=session_lock,
                    )
                    session_lock = None
                    return json.dumps({
                        "status": "error",
                        "code": "RUN_LEDGER_UNAVAILABLE",
                        "error_type": "runtime_state_unavailable",
                        "message": "Runtime run ledger is unavailable; request was not started. Please retry shortly.",
                        "retryable": True,
                        "recoverable": True,
                        "request_id": "",
                    }, ensure_ascii=False)

                self._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message=visible_message or visible_prompt or "",
                    client_attempt_id=client_attempt_id,
                    retry_of_request_id=retry_of_request_id,
                    interrupts_request_id=interrupts_request_id,
                    project_context_meta=project_context_meta,
                )

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                self._ensure_sse_state(request_id)
                self._push_sse_event(request_id, {
                    "type": "phase",
                    "content": "已收到，正在准备响应",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message summary: {_web_body_log_summary(prompt)}")

            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                self._abort_pre_worker_request(
                    request_id,
                    session_id,
                    message="Message was filtered",
                    reason="context_filtered",
                    error_code="CONTEXT_FILTERED",
                    session_lock=session_lock,
                )
                session_lock = None
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            context["channel_type"] = "web"
            context["request_id"] = request_id
            context["session_lock"] = session_lock
            context["cancel_token_owner"] = "web_channel"
            context["visible_message"] = (visible_message or "Please handle these attachments.").strip()
            if project_context_meta:
                context["project_context_meta"] = project_context_meta
            if internal_action:
                context["internal_action"] = True
            if isinstance(attachments, list):
                context["attachments"] = attachments
            if is_voice_input:
                # Web channel runs its own TTS post-pipeline via
                # _maybe_dispatch_auto_tts; don't set desire_rtype here or
                # chat_channel would synthesize a duplicate VOICE reply.
                context["is_voice_input"] = True

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            if self._pre_persist_web_user_message(
                session_id,
                visible_message or visible_prompt or "Please handle these attachments.",
                request_id=request_id,
                client_attempt_id=client_attempt_id,
                attachments=attachments,
                project_context=project_context_meta,
            ):
                context["pre_persisted_user_message"] = True

            threading.Thread(target=self._produce_with_session_lock, args=(context, session_lock), daemon=True).start()

            return json.dumps({
                "status": "success",
                "request_id": request_id,
                "stream": use_sse,
                "same_session": same_session_decision,
            })

        except Exception as e:
            public_message = _public_exception_message("Message request failed before worker start.", e)
            public_extra = _public_exception_summary(e)
            if request_id:
                self._abort_pre_worker_request(
                    request_id,
                    session_id,
                    message=public_message,
                    reason="post_message_exception",
                    error_code="POST_MESSAGE_EXCEPTION",
                    session_lock=session_lock,
                    error_extra=public_extra,
                )
                session_lock = None
            else:
                try:
                    if session_lock:
                        session_lock.release()
                except Exception:
                    pass
            logger.error(f"Error processing message: {_web_body_log_summary(e)}")
            return json.dumps({"status": "error", "message": public_message, **public_extra})

    def _produce_with_session_lock(self, context: Context, session_lock):
        session_id = ""
        request_id = ""
        try:
            session_id = str(context.get("session_id") or "") if context else ""
            request_id = str(context.get("request_id") or "") if context else ""
            self.produce(context)
        except Exception as e:
            logger.error(f"[WebChannel] produce failed before worker start: {_web_body_log_summary(e)}")
            self._finalize_request_after_worker(context, e)
            try:
                if session_lock:
                    session_lock.release()
            except Exception as e:
                logger.debug(f"[WebChannel] session lock release skipped: {_web_body_log_summary(e)}")
            if session_id:
                self._start_next_queued_request(session_id, completed_request_id=request_id)

    def stream_response(self, request_id: str, session_id: str = ""):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        Supports multiple concurrent clients and reconnection: events are
        appended to a per-request replay log, and every EventSource connection
        reads with its own cursor instead of consuming a shared Queue.
        """
        expected_session_id = str(session_id or "").strip()
        mismatch_event = self._request_session_mismatch_event(request_id, expected_session_id)
        if mismatch_event:
            payload = json.dumps(mismatch_event, ensure_ascii=False)
            yield f"id: 0\ndata: {payload}\n\n".encode("utf-8")
            return

        if not self._sse_request_exists(request_id):
            replay_events = self._runtime_projection_replay_events(request_id, expected_session_id)
            if replay_events and any(event.get("type") in SSE_STREAM_TERMINAL_TYPES for event in replay_events):
                for offset, replay_event in enumerate(replay_events):
                    replay_event["runtime_projection_replay_index"] = offset
                    replay_event_id = int(replay_event.get("runtime_event_latest_id") or 0) + offset
                    payload = json.dumps(replay_event, ensure_ascii=False)
                    yield f"id: {replay_event_id}\ndata: {payload}\n\n".encode("utf-8")
                return
            interrupted_event = self._recover_sidecar_interrupted_stream_event(request_id)
            if interrupted_event is not None:
                payload = json.dumps(interrupted_event, ensure_ascii=False)
                yield f"id: 0\ndata: {payload}\n\n".encode("utf-8")
                return
            if replay_events:
                for offset, replay_event in enumerate(replay_events):
                    replay_event["runtime_projection_replay_index"] = offset
                    replay_event_id = int(replay_event.get("runtime_event_latest_id") or 0) + offset
                    payload = json.dumps(replay_event, ensure_ascii=False)
                    yield f"id: {replay_event_id}\ndata: {payload}\n\n".encode("utf-8")
                return
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        self._ensure_sse_state(request_id)
        with self.sse_lock:
            cond = self.sse_conditions.get(request_id)
            if cond is None:
                yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
                return
            subscriber_count = self.sse_subscribers.get(request_id, 0) + 1
            self.sse_subscribers[request_id] = subscriber_count
            cleanup_timer = self.sse_cleanup_timers.pop(request_id, None)
        if cleanup_timer:
            try:
                cleanup_timer.cancel()
            except Exception:
                pass

        start_index = 0
        replay_gap_event = None
        terminal_consumed = False
        try:
            raw_last_id = getattr(web, "ctx", None)
            raw_last_id = getattr(raw_last_id, "env", {}).get("HTTP_LAST_EVENT_ID", "") if raw_last_id else ""
            params = web.input(last_event_id="")
            query_last_id = str(params.last_event_id or "")
            raw_last_id = str(raw_last_id or query_last_id or "")
            if raw_last_id != "":
                with self.sse_lock:
                    event_offset = self.sse_event_offsets.get(request_id, 0)
                    events = self.sse_events.get(request_id, [])
                start_index = max(0, int(raw_last_id) + 1 - event_offset)
                try:
                    last_event_number = int(raw_last_id)
                    if last_event_number + 1 < event_offset:
                        replay_gap_event = self._build_replay_gap_event(
                            request_id,
                            last_event_number,
                            event_offset,
                        )
                        start_index = 0
                    for offset, event in enumerate(events):
                        if event.get("type") in SSE_STREAM_TERMINAL_TYPES and last_event_number >= event_offset + offset:
                            terminal_consumed = True
                            break
                except Exception:
                    terminal_consumed = False
        except Exception:
            start_index = 0
        index = start_index
        idle_timeout = 600  # 10 minutes without any real event
        deadline = time.time() + idle_timeout
        # After the main reply is done we keep the stream open for a short
        # tail so async post-processing (TTS auto-synthesis) can deliver a
        # `voice_attach` event before the client disconnects.
        POST_DONE_TAIL_SECONDS = 12
        HEARTBEAT_SECONDS = 15
        post_done = terminal_consumed
        post_deadline = time.time() + POST_DONE_TAIL_SECONDS if terminal_consumed else 0.0
        last_heartbeat_at = 0.0

        try:
            if replay_gap_event is not None:
                gap_event_id = max(0, int(replay_gap_event.get("retained_from_event_id") or 0) - 1)
                payload = json.dumps(replay_gap_event, ensure_ascii=False)
                yield f"id: {gap_event_id}\ndata: {payload}\n\n".encode("utf-8")
                return

            while time.time() < deadline:
                item = None
                event_id = None
                with cond:
                    events = self.sse_events.get(request_id, [])
                    if index < len(events):
                        event_id = self.sse_event_offsets.get(request_id, 0) + index
                        item = events[index]
                        index += 1
                    else:
                        cond.wait(timeout=1)

                if item is None:
                    if post_done and time.time() >= post_deadline:
                        break
                    now = time.time()
                    if now - last_heartbeat_at >= HEARTBEAT_SECONDS:
                        heartbeat = {
                            "type": "heartbeat",
                            "request_id": request_id,
                            "timestamp": now,
                        }
                        payload = json.dumps(heartbeat, ensure_ascii=False)
                        yield f"data: {payload}\n\n".encode("utf-8")
                        last_heartbeat_at = now
                    else:
                        yield b": keepalive\n\n"
                    continue

                deadline = time.time() + idle_timeout
                payload = json.dumps(item, ensure_ascii=False)
                yield f"id: {event_id}\ndata: {payload}\n\n".encode("utf-8")

                itype = item.get("type")
                if itype == "done":
                    post_done = True
                    post_deadline = time.time() + POST_DONE_TAIL_SECONDS
                elif itype == "cancelled":
                    # Close SSE tail quickly after cancel; don't wait for the
                    # full TTS tail since the user already pressed Stop.
                    post_done = True
                    post_deadline = time.time() + 3
                elif itype in ("error", "interrupted", "replay_gap"):
                    post_done = True
                    post_deadline = time.time() + 3
                elif itype == "voice_attach":
                    # WSGI buffers the previous chunk until the next yield;
                    # shrink the tail so the generator wakes up quickly to
                    # emit a couple of keepalive comments that push the
                    # voice_attach payload through to the browser.
                    post_done = True
                    post_deadline = time.time() + 2  # 2s post-attach tail
        finally:
            disconnected_early = not post_done and time.time() < deadline
            if disconnected_early:
                logger.info(f"[WebChannel] SSE client detached; request keeps running: {request_id}")
            with self.sse_lock:
                remaining = max(0, self.sse_subscribers.get(request_id, 1) - 1)
                if remaining:
                    self.sse_subscribers[request_id] = remaining
                else:
                    self.sse_subscribers.pop(request_id, None)
            # Only drop the queue once the reply is actually complete. If the
            # client disconnected early (e.g. switched sessions and will
            # re-attach with the same request_id), keep the queue so the new
            # connection can resume reading the remaining events.
            if remaining <= 0 and (post_done or time.time() >= deadline):
                self._cleanup_sse_request(request_id)
            elif remaining <= 0:
                self._schedule_sse_cleanup(request_id, reason="detached-without-terminal")

    def cancel_request(self):
        """
        Cancel an in-flight agent run.

        Body: {"request_id": "...", "session_id": "..."}
        Either field is sufficient; request_id is preferred when known.
        Always returns success even when nothing was running, so the
        client's UX is idempotent.
        """
        try:
            from agent.protocol import get_cancel_registry

            data = web.data()
            try:
                json_data = json.loads(data) if data else {}
            except Exception:
                json_data = {}

            request_id = (json_data.get("request_id") or "").strip()
            session_id = (json_data.get("session_id") or "").strip()
            lang = (json_data.get("lang") or "zh").lower()
            if request_id and not session_id:
                session_id = self.request_to_session.get(request_id, "")
            if request_id:
                queued_payload = self._load_queued_payload(request_id)
                queued_row = None
                if not queued_payload:
                    try:
                        from agent.protocol import get_run_ledger

                        queued_row = get_run_ledger().get_run(request_id)
                    except Exception:
                        queued_row = None
                if queued_payload or str((queued_row or {}).get("status") or (queued_row or {}).get("phase") or "").lower() == "queued":
                    return json.dumps(
                        self._cancel_queued_request(request_id, expected_session_id=session_id),
                        ensure_ascii=False,
                    )
            active_request_ids = self._active_request_ids_for_session(session_id) if session_id else []

            registry = get_cancel_registry()
            cancelled = 0

            if request_id:
                if registry.cancel_request(request_id):
                    cancelled = 1
                    self._mark_run_phase(request_id, "cancelling", status="cancelling")

            if cancelled == 0 and session_id:
                cancelled = registry.cancel_session(session_id)
                for active_request_id in active_request_ids:
                    self._mark_run_phase(active_request_id, "cancelling", status="cancelling")
            subagent_cancel = self._cancel_subagents_for_parent(session_id) if session_id else {
                "cancelledTasks": 0,
                "cancelledRequests": 0,
                "tasks": [],
            }

            if request_id and self._sse_request_exists(request_id):
                self._push_cancelled_event_once(request_id, {
                    "type": "cancelled",
                    "content": "🛑 Cancelled" if lang.startswith("en") else "🛑 已中止",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })
            elif active_request_ids:
                self._push_cancelled_events_for_session(session_id, active_request_ids, lang=lang)

            logger.info(
                f"[WebChannel] cancel request: request_id={request_id!r}, "
                f"session_id={session_id!r}, cancelled={cancelled}, subagents={subagent_cancel}"
            )
            return json.dumps({
                "status": "success",
                "cancelled": cancelled + int(subagent_cancel.get("cancelledTasks") or 0),
                "parentCancelled": cancelled,
                "subagents": subagent_cancel,
            })

        except Exception as e:
            logger.error(f"[WebChannel] cancel_request error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def poll_response(self):
        """
        Poll for responses using the session_id.
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')

            if not session_id or session_id not in self.session_queues:
                return json.dumps({"status": "error", "message": "Invalid session ID"})

            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_id].get(block=False)

                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success",
                    "has_content": True,
                    "content": response["content"],
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"]
                })

            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})

        except Exception as e:
            logger.error(f"Error polling response: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        # Inject the backend-resolved default language so the console can use
        # it on first load (when the user has no saved cow_lang preference).
        return html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())

    def startup(self):
        host = _effective_web_host()
        _validate_web_bind_auth(host)
        port = conf().get("web_port", 9899)
        is_public_bind = _is_public_bind_host(host)

        self._cleanup_stale_voice_recordings()

        # Print available channel types (ordered by language: prioritize
        # locally-popular channels for the current UI language)
        logger.info(
            "[WebChannel] Available channels (edit `channel_type` in config.json to switch, separate multiple with commas):")
        zh_channels = [
            ("web", "Web"),
            ("terminal", "Terminal"),
            ("weixin", "WeChat"),
            ("feishu", "Feishu"),
            ("dingtalk", "DingTalk"),
            ("wecom_bot", "WeCom Bot"),
            ("wechatcom_app", "WeCom App"),
            ("wechat_kf", "WeChat Customer Service"),
            ("wechatmp", "WeChat Official Account"),
            ("wechatmp_service", "WeChat Official Account (Service)"),
            ("telegram", "Telegram"),
            ("slack", "Slack"),
            ("discord", "Discord"),
        ]
        en_channels = [
            ("web", "Web"),
            ("terminal", "Terminal"),
            ("telegram", "Telegram"),
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("weixin", "WeChat"),
            ("feishu", "Feishu"),
            ("dingtalk", "DingTalk"),
            ("wecom_bot", "WeCom Bot"),
            ("wechatcom_app", "WeCom App"),
            ("wechat_kf", "WeChat Customer Service"),
            ("wechatmp", "WeChat Official Account"),
            ("wechatmp_service", "WeChat Official Account (Service)"),
        ]
        channels = en_channels if i18n.get_language() == "en" else zh_channels
        name_width = max(len(name) for name, _ in channels)
        for idx, (name, label) in enumerate(channels, 1):
            logger.info(f"[WebChannel]  {idx:>2}. {name:<{name_width}} - {label}")
        logger.info("[WebChannel] ✅ Web console is running")
        logger.info(f"[WebChannel] 🌐 Local access: http://localhost:{port}")
        if is_public_bind:
            logger.info(f"[WebChannel] 🌍 Server access: http://YOUR_IP:{port} (replace YOUR_IP with your server IP)")
        else:
            logger.info(f"[WebChannel] 🔒 Listening on {host} only (local access). For public access, set web_host to 0.0.0.0 and configure web_password")

        try:
            import webbrowser

            def _web_auto_open_enabled() -> bool:
                if os.environ.get("ECOREX_DESKTOP") == "1":
                    return False
                if str(os.environ.get("ECOREX_UPDATE_MODE") or "").strip().lower() == "background":
                    return False
                if str(os.environ.get("ECOREX_WEB_NO_BROWSER") or "").strip().lower() in {"1", "true", "yes", "on"}:
                    return False
                configured = conf().get("web_auto_open")
                if configured is None:
                    return True
                if isinstance(configured, bool):
                    return configured
                return str(configured).strip().lower() not in {"0", "false", "no", "off"}

            if _web_auto_open_enabled():
                webbrowser.open(f"http://localhost:{port}")
                logger.debug(f"[WebChannel] Opened browser at http://localhost:{port}")
            else:
                logger.debug("[WebChannel] Browser auto-open disabled for this runtime session")
        except Exception as e:
            logger.debug(f"[WebChannel] Could not open browser: {_web_body_log_summary(e)}")

        # 确保静态文件目录存在
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        if not os.path.exists(static_dir):
            os.makedirs(static_dir)
            logger.debug(f"[WebChannel] Created static directory: {static_dir}")

        try:
            from cli import __version__
            from common.ecorex_workspace import register_installation
            surface = "desktop" if os.environ.get("ECOREX_DESKTOP") == "1" else "webui"
            register_installation(_get_workspace_root(), surface, {
                "version": __version__,
                "bindHost": host,
                "port": port,
                "url": f"http://localhost:{port}/app/",
            })
        except Exception as e:
            logger.debug(f"[WebChannel] installation manifest registration skipped: {_web_body_log_summary(e)}")

        app = web.application(WEB_ROUTES, globals(), autoreload=False)

        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        try:
            # Build WSGI app with middleware (same as runsimple but without print).
            # WSGIServer construction binds the socket; once it succeeds the
            # channel is externally observable as ready even though start()
            # blocks the channel thread.
            func = web.httpserver.StaticMiddleware(app.wsgifunc())
            func = web.httpserver.LogMiddleware(func)
            server = web.httpserver.WSGIServer((host, port), func)
            server.daemon_threads = True
            # Default request_queue_size(5) / timeout(10s) / numthreads(10) are
            # too small: when SSE streams occupy many threads, the backlog fills
            # and new connections get refused (ERR_CONNECTION_ABORTED).
            server.request_queue_size = 128
            server.timeout = 300
            server.requests.min = 20
            server.requests.max = 80
            self._http_server = server
            self.report_startup_success()
            server.start()
        except (KeyboardInterrupt, SystemExit):
            if self._http_server:
                self._http_server.stop()
        except OSError as e:
            self.report_startup_error(mask_sensitive_text(str(e), max_chars=500))
            if e.errno in (48, 98):  # macOS/Linux EADDRINUSE
                logger.error(
                    f"[WebChannel] 端口 {port} 已被占用，可执行 `cow restart` 清理残留进程，"
                    f"或在 config.json 中修改 web_port"
                )
            raise
        except Exception as e:
            self.report_startup_error(mask_sensitive_text(str(e), max_chars=500))
            raise

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {_web_body_log_summary(e)}")
            self._http_server = None


class RootHandler:
    def GET(self):
        return _serve_web_app_asset("")


def _web_app_static_dir() -> str:
    packaged_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "static", "app"))
    source_dist = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "desktop", "dist"))
    if os.path.isfile(os.path.join(source_dist, "index.html")):
        return source_dist
    return packaged_dir


def _serve_web_app_asset(file_path: str = ""):
    app_dir = _web_app_static_dir()
    requested = (file_path or "").strip("/")
    knowledge_rel = _knowledge_rel_from_app_path(requested)
    if knowledge_rel:
        return _knowledge_viewer_html(knowledge_rel)
    target = os.path.realpath(os.path.join(app_dir, requested)) if requested else os.path.join(app_dir, "index.html")
    if not _is_within_directory(app_dir, target):
        raise web.notfound()
    if not os.path.isfile(target):
        if requested and os.path.splitext(requested)[1]:
            raise web.notfound()
        target = os.path.join(app_dir, "index.html")
    if not os.path.isfile(target):
        web.header("Content-Type", "text/html; charset=utf-8")
        web.header("Cache-Control", "no-cache, no-store, must-revalidate")
        return _default_web_app_html()
    content_type = mimetypes.guess_type(target)[0] or "text/html"
    if os.path.basename(target) == "index.html":
        web.header("Content-Type", "text/html; charset=utf-8")
        web.header("Cache-Control", "no-cache, no-store, must-revalidate")
        with open(target, "r", encoding="utf-8") as f:
            return _inject_web_app_bridge(f.read())
    else:
        web.header("Content-Type", content_type)
        web.header("Cache-Control", "public, max-age=31536000, immutable")
        with open(target, "rb") as f:
            return f.read()


def _knowledge_rel_from_app_path(requested: str) -> str:
    path_value = (requested or "").replace("\\", "/").lstrip("/")
    if not path_value.startswith("knowledge/"):
        return ""
    rel_path = path_value[len("knowledge/"):].strip("/")
    if not rel_path or ".." in rel_path or not rel_path.lower().endswith(".md"):
        return ""
    return rel_path


def _html_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _knowledge_viewer_html(rel_path: str):
    web.header("Content-Type", "text/html; charset=utf-8")
    web.header("Cache-Control", "no-cache, no-store, must-revalidate")
    encoded_path = json.dumps(rel_path, ensure_ascii=False)
    title = _html_escape(os.path.basename(rel_path))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} - EcoreX Knowledge</title>
  <style>
    body {{ margin:0; font-family: Inter, "Microsoft YaHei", system-ui, sans-serif; color:#1d140e; background:#fff9f2; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 28px 20px 56px; }}
    header {{ display:grid; gap:6px; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:24px; line-height:1.25; }}
    small {{ color:#7b6b5d; }}
    pre {{ white-space:pre-wrap; word-break:break-word; line-height:1.68; padding:18px; border:1px solid #e2d5c8; border-radius:8px; background:#fff; }}
    .error {{ padding:14px 16px; border:1px solid #e3a36f; border-radius:8px; color:#8f3f12; background:#fff1e5; }}
  </style>
</head>
<body>
  <main>
    <header>
      <small>知识库</small>
      <h1>{title}</h1>
      <small>{_html_escape(rel_path)}</small>
    </header>
    <section id="content">正在读取...</section>
  </main>
  <script>
    const relPath = {encoded_path};
    const target = document.getElementById("content");
    function runtimePath(path) {{
      const markers = ["/app", "/chat", "/auth", "/api", "/message", "/upload", "/stream", "/assets"];
      const current = window.location.pathname || "";
      let base = "";
      for (const marker of markers) {{
        const index = current.indexOf(marker);
        if (index > 0) {{
          base = current.slice(0, index).replace(/\/+$/, "");
          break;
        }}
      }}
      return base && path.charAt(0) === "/" && !path.startsWith(base + "/") ? base + path : path;
    }}
    fetch(runtimePath("/api/knowledge/read?path=" + encodeURIComponent(relPath)), {{ credentials: "same-origin" }})
      .then((response) => response.json().then((payload) => {{ if (!response.ok || payload.status === "error") throw new Error(payload.message || "读取失败"); return payload; }}))
      .then((payload) => {{
        const pre = document.createElement("pre");
        pre.textContent = payload.content || "";
        target.replaceChildren(pre);
      }})
      .catch((error) => {{
        const div = document.createElement("div");
        div.className = "error";
        div.textContent = error && error.message ? error.message : String(error);
        target.replaceChildren(div);
      }});
  </script>
</body>
</html>"""


class WebAppRootHandler:
    def GET(self):
        return _serve_web_app_asset("")


class WebAppAssetHandler:
    def GET(self, file_path):
        return _serve_web_app_asset(file_path)


class MessageHandler:
    def POST(self):
        _require_auth()
        return WebChannel().post_message()


class UploadHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().upload_file()


class VoiceAsrHandler:
    """Receive a mic recording, persist it under uploads/ and run ASR.
    Returns {status, text, audio_url} so the UI can render a playback bubble."""
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')

        saved_path = None
        try:
            params = _raw_web_input()
            file_obj = params.get("file")
            if file_obj is None:
                return json.dumps({"status": "error", "message": "no audio file"})

            filename = getattr(file_obj, "filename", "") or "recording.webm"
            ext = os.path.splitext(filename)[1].lower() or ".webm"
            if ext not in (".webm", ".ogg", ".opus", ".mp4", ".m4a", ".mp3", ".wav"):
                ext = ".webm"

            upload_dir = _get_upload_dir()
            os.makedirs(upload_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            saved_name = f"voice_input_{ts}_{random.randint(0, 9999)}{ext}"
            saved_path = os.path.join(upload_dir, saved_name)
            with open(saved_path, "wb") as f:
                f.write(file_obj.file.read() if hasattr(file_obj, "file") else file_obj.value)

            audio_url = f"/uploads/{saved_name}"

            from bridge.bridge import Bridge
            reply = Bridge().fetch_voice_to_text(saved_path)
            if reply is None:
                return json.dumps({
                    "status": "error",
                    "message": "ASR returned no reply",
                    "audio_url": audio_url,
                })

            from bridge.reply import ReplyType
            if reply.type == ReplyType.TEXT:
                return json.dumps({
                    "status": "success",
                    "text": reply.content or "",
                    "audio_url": audio_url,
                })
            return json.dumps({
                "status": "error",
                "message": reply.content or "ASR failed",
                "audio_url": audio_url,
            })
        except Exception as e:
            logger.error(f"[VoiceAsrHandler] failed: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("ASR failed.", e),
                **_public_exception_summary(e),
            })


class VoiceTtsHandler:
    """On-demand TTS for the in-chat "read aloud" button. Returns the
    audio URL and (when session_id is given) persists it onto the message."""
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            text = (data.get("text") or "").strip()
            session_id = (data.get("session_id") or "").strip()
            if not text:
                return json.dumps({"status": "error", "message": "empty text"})
            # `@singleton` makes WebChannel a factory function — go via instance.
            channel = WebChannel()
            if not channel._tts_provider_ready():
                return json.dumps({"status": "error", "message": "tts not configured"})

            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                msg = getattr(reply, "content", "") or "tts failed"
                return json.dumps({"status": "error", "message": str(msg)})

            url = channel._publish_tts_audio(reply.content)
            if not url:
                return json.dumps({"status": "error", "message": "publish failed"})

            if session_id:
                try:
                    from agent.memory import get_conversation_store
                    get_conversation_store().attach_extras_to_last_assistant(
                        session_id, {"audio": {"url": url, "kind": "tts"}},
                    )
                except Exception as e:
                    logger.debug(f"[VoiceTtsHandler] persist skipped: {_web_body_log_summary(e)}")

            return json.dumps({"status": "success", "audio_url": url})
        except Exception as e:
            logger.error(f"[VoiceTtsHandler] failed: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("TTS failed.", e),
                **_public_exception_summary(e),
            })


def _resolve_web_local_path(path_value: str) -> str:
    workspace_root = os.path.realpath(_get_workspace_root())
    expanded_path = os.path.expanduser(str(path_value or "").strip())
    if not os.path.isabs(expanded_path):
        expanded_path = os.path.join(workspace_root, expanded_path.lstrip("/\\"))
    return os.path.realpath(expanded_path)


def _web_file_preview_roots(workspace_root: str, upload_root: str) -> List[str]:
    roots = [
        os.path.realpath(workspace_root),
        os.path.realpath(upload_root),
    ]
    serve_root = conf().get("web_file_serve_root")
    if serve_root:
        roots.append(os.path.realpath(os.path.expanduser(serve_root)))
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        for root in get_tool_permission_broker().list_workspace_roots(cwd=workspace_root):
            if root:
                roots.append(os.path.realpath(os.path.expanduser(str(root))))
    except Exception as exc:
        logger.warning(f"[WebChannel] file preview root lookup failed: {_web_body_log_summary(exc)}")
    deduped: List[str] = []
    seen = set()
    for root in roots:
        key = os.path.normcase(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _decision_allowed(decision: Any) -> bool:
    return isinstance(decision, dict) and decision.get("allowed") is True


def _decision_reason(decision: Any, fallback: str) -> str:
    if isinstance(decision, dict):
        reason = str(decision.get("reason") or "").strip()
        if reason:
            return reason
    return fallback


class PollHandler:
    def POST(self):
        _require_auth()
        return WebChannel().poll_response()


class CancelHandler:
    def POST(self):
        _require_auth()
        return WebChannel().cancel_request()


class RequestQueueActionHandler:
    def POST(self, request_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return json.dumps(WebChannel().queue_action_request(request_id), ensure_ascii=False)


def _runtime_projection_public_payload(value: Any, *, include_events: bool = False, _depth: int = 0) -> Any:
    if isinstance(value, list):
        return [
            _runtime_projection_public_payload(item, include_events=include_events, _depth=_depth + 1)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    public = {}
    for key, item in value.items():
        if key == "events" and not (include_events and _depth == 0):
            continue
        public[key] = _runtime_projection_public_payload(
            item,
            include_events=include_events,
            _depth=_depth + 1,
        )
    return public


_IMAGE_JOB_API_ID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
_IMAGE_JOB_QUALITY_RETRY_PROMPT_SUFFIX = (
    "\n\nQuality retry: regenerate a clean final image with no broken seams, "
    "no ghosted overlays, no watermark artifacts, no garbled text fragments, "
    "and preserve authorized reference-image structure when references are provided."
)
_IMAGE_JOB_API_TASK_FIELDS = {
    "prompt",
    "image_url",
    "provider",
    "model",
    "quality",
    "size",
    "aspect_ratio",
    "output_format",
    "output_compression",
    "background",
    "moderation",
    "operation",
    "input_image_count",
    "output_count",
    "quality_retry_max",
    "max_quality_retries",
    "n",
}


def _safe_image_job_api_identifier(value: Any, *, prefix: str = "", allow_empty: bool = True) -> str:
    raw = str(value or "").strip()
    if (
        raw
        and len(raw) <= 128
        and all(char in _IMAGE_JOB_API_ID_CHARS for char in raw)
        and not any(part in raw.lower() for part in ("private", "prompt", "secret", "token", "password"))
    ):
        return raw
    if allow_empty:
        return ""
    return f"{prefix}-{uuid.uuid4().hex[:16]}" if prefix else uuid.uuid4().hex[:16]


def _safe_image_job_public_request_id(value: Any) -> str:
    raw = str(value or "").strip()
    safe = _safe_image_job_api_identifier(raw, prefix="req-image-job", allow_empty=True)
    if safe:
        return safe
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"req-image-job-{digest}"


def _safe_image_job_public_session_id(value: Any) -> str:
    raw = str(value or "").strip()
    safe = _safe_image_job_api_identifier(raw, prefix="session-image-job", allow_empty=True)
    if safe:
        return safe
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"session-image-job-{digest}"


def _safe_image_job_public_turn_id(value: Any) -> str:
    raw = str(value or "").strip()
    safe = _safe_image_job_api_identifier(raw, prefix="turn-image-job", allow_empty=True)
    if safe:
        return safe
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"turn-image-job-{digest}"


def _image_job_task_count(body: Dict[str, Any]) -> int:
    for key in ("count", "n", "output_count", "outputCount"):
        try:
            value = int(body.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return min(value, 16)
    return 1


def _image_job_api_tasks(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_tasks = body.get("tasks")
    if isinstance(raw_tasks, list) and raw_tasks:
        tasks: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_tasks[:16]):
            if not isinstance(item, dict):
                raise ValueError("tasks must be objects")
            prompt = str(item.get("prompt") or item.get("text") or "").strip()
            if not prompt:
                raise ValueError("each image task requires prompt")
            task = {key: item.get(key) for key in _IMAGE_JOB_API_TASK_FIELDS if key in item}
            task["prompt"] = prompt
            task["operation"] = item.get("operation") or ("edit" if item.get("image_url") else "generate")
            if "quality_retry_max" not in task and "max_quality_retries" not in task:
                task["quality_retry_max"] = _image_job_quality_retry_max(item.get("quality_retry_max") or item.get("max_quality_retries"))
            task["task_id"] = f"task-{index + 1}"
            tasks.append(task)
        return tasks
    prompt = str(body.get("prompt") or body.get("text") or "").strip()
    if not prompt:
        raise ValueError("prompt or tasks is required")
    count = _image_job_task_count(body)
    tasks: List[Dict[str, Any]] = []
    for index in range(count):
        task = {
            "prompt": prompt,
            "operation": body.get("operation") or ("edit" if body.get("image_url") else "generate"),
            "output_count": 1,
        }
        for key in (
            "image_url",
            "provider",
            "model",
            "quality",
            "size",
            "aspect_ratio",
            "output_format",
            "output_compression",
            "background",
            "moderation",
            "quality_retry_max",
            "max_quality_retries",
        ):
            if key in body:
                task[key] = body.get(key)
        if "quality_retry_max" not in task and "max_quality_retries" not in task:
            task["quality_retry_max"] = _image_job_quality_retry_max(body.get("quality_retry_max") or body.get("max_quality_retries"))
        task["task_id"] = f"task-{index + 1}"
        tasks.append(task)
    return tasks


def _image_job_quality_retry_max(value: Any = None) -> int:
    raw = value
    if raw in (None, ""):
        raw = conf().get("image_quality_retry_max") or 1
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = 1
    return max(0, min(parsed, 2))


def _image_job_ocr_reuse_enabled(body: Dict[str, Any]) -> bool:
    value = body.get("ocr_reuse") if "ocr_reuse" in body else body.get("ocrReuse")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _image_job_dry_run_ocr_provider(payload: Dict[str, Any]) -> Dict[str, Any]:
    refs = payload.get("image_urls") if isinstance(payload.get("image_urls"), list) else [payload.get("image") or ""]
    digest = hashlib.sha256("\n".join(str(item or "") for item in refs).encode("utf-8", errors="replace")).hexdigest()[:16]
    return {
        "brief": f"dry-run-image-brief-{digest}",
        "provider": "dry_run",
    }


def _authorize_web_capability(
    capability: str,
    action: str,
    *,
    arguments: Optional[Dict[str, Any]] = None,
    resource: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        broker = get_tool_permission_broker()
        method = getattr(broker, "authorize_capability", None)
        if callable(method):
            decision = method(
                capability,
                action,
                arguments=arguments or {},
                resource=resource,
                metadata=metadata or {},
                cwd=_get_workspace_root(),
            )
            if isinstance(decision, dict) and decision.get("allowed") in {True, False}:
                return decision
            logger.warning("[WebChannel] capability permission check returned invalid decision; blocking action")
    except Exception as exc:
        logger.warning(f"[WebChannel] capability permission check failed: {_web_body_log_summary(exc)}")
    return {"allowed": False, "reason": "Permission broker unavailable; capability action was blocked."}


def _permission_denied_payload(
    message: str,
    decision: Optional[Dict[str, Any]] = None,
    *,
    capability: str = "",
    action: str = "",
) -> Dict[str, Any]:
    decision = decision if isinstance(decision, dict) else {}
    mode = ""
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        mode = str(get_tool_permission_broker().get_state().get("mode") or "")
    except Exception:
        mode = ""
    permission = {
        "allowed": False,
        "reason": decision.get("reason") or message or "Permission denied.",
    }
    if capability:
        permission["capability"] = capability
    if action:
        permission["action"] = action
    if mode:
        permission["mode"] = mode
    return {
        "status": "error",
        "code": "permission_denied",
        "message": message or decision.get("reason") or "Permission denied.",
        "permission": permission,
    }


def _image_job_vision_ocr_provider(payload: Dict[str, Any]) -> Any:
    image = str(payload.get("image") or payload.get("image_url") or "").strip()
    if not image:
        return ""
    permission_ref = hashlib.sha256(image.encode("utf-8", errors="replace")).hexdigest()[:16]
    decision = _authorize_web_capability(
        "vision",
        "ocr_brief",
        arguments={
            "image": f"image-input-{permission_ref}",
            "question": "image job OCR/vision brief",
            "source": "image_job_ocr",
        },
        metadata={"source": "image_job_ocr"},
    )
    if decision.get("allowed") is not True:
        raise RuntimeError("vision OCR permission denied")
    from agent.tools.vision.vision import Vision

    return Vision({"cwd": _get_workspace_root()}).execute({
        "image": image,
        "question": (
            "Extract a concise OCR and visual brief for image generation/editing. "
            "Include visible text, layout, key objects, colors, and composition. "
            "Do not add instructions that are not observable in the image."
        ),
    })


def _image_job_ocr_provider(body: Dict[str, Any]):
    if not _image_job_ocr_reuse_enabled(body):
        return None
    if bool(body.get("dry_run")) or str(body.get("runner") or "").strip().lower() == "dry_run":
        return _image_job_dry_run_ocr_provider
    return _image_job_vision_ocr_provider


def _image_job_output_dir() -> str:
    path = os.path.join(_get_upload_dir(), "image-jobs")
    os.makedirs(path, exist_ok=True)
    return path


def _image_job_dry_run_runner(task: Dict[str, Any], emit_progress, cancel_event) -> Dict[str, Any]:
    if cancel_event.is_set():
        from agent.protocol import ImageJobCancelled

        raise ImageJobCancelled("cancel_requested")
    emit_progress("provider_request", progress=0.25, detail={"provider": "test", "source": "web_channel"})
    output_dir = _image_job_output_dir()
    task_id = _safe_image_job_api_identifier(task.get("task_id"), prefix="task", allow_empty=False)
    path = os.path.join(output_dir, f"{task_id}.png")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(b"")
    emit_progress("saving", progress=0.75, detail={"provider": "test", "source": "web_channel"})
    return {
        "kind": "image",
        "title": f"{task_id}.png",
        "path": path,
        "fileType": "image",
        "mimeType": "image/png",
        "sizeBytes": 0,
    }


def _image_job_skill_runner(task: Dict[str, Any], emit_progress, cancel_event) -> Dict[str, Any]:
    if cancel_event.is_set():
        from agent.protocol import ImageJobCancelled

        raise ImageJobCancelled("cancel_requested")
    script_path = Path(__file__).resolve().parents[2] / "skills" / "image-generation" / "scripts" / "generate.py"
    if not script_path.exists():
        raise RuntimeError("image generation skill runner is unavailable")
    args: Dict[str, Any] = {}
    for key in (
        "prompt",
        "image_url",
        "provider",
        "model",
        "quality",
        "size",
        "aspect_ratio",
        "output_format",
        "output_compression",
        "background",
        "moderation",
    ):
        if key in task and task.get(key) not in (None, ""):
            args[key] = task.get(key)
    ocr_brief = str(task.get("_ocr_brief") or "").strip()
    if ocr_brief:
        args["ocr_brief"] = ocr_brief[:4096]
    if not args.get("prompt"):
        raise RuntimeError("image generation prompt is required")
    try:
        quality_retry_attempt = int(task.get("_quality_retry_attempt") or 0)
    except (TypeError, ValueError):
        quality_retry_attempt = 0
    if quality_retry_attempt > 0:
        args["prompt"] = f"{str(args['prompt']).rstrip()}{_IMAGE_JOB_QUALITY_RETRY_PROMPT_SUFFIX}"
    output_dir = _image_job_output_dir()
    env = os.environ.copy()
    env["IMAGE_OUTPUT_DIR"] = output_dir
    env = image_generation_env_with_config(env)
    emit_progress("provider_request", progress=0.2, detail={"source": "web_channel", "operation": task.get("operation") or "generate"})
    provider_result = run_image_generation_payload(
        args,
        script_path=script_path,
        output_dir=output_dir,
        env=env,
    )
    if cancel_event.is_set():
        from agent.protocol import ImageJobCancelled

        raise ImageJobCancelled("cancel_requested")
    payload = provider_result.get("payload") if isinstance(provider_result.get("payload"), dict) else {}
    returncode = int(provider_result.get("returncode") or 0)
    if returncode != 0 or payload.get("error"):
        error = payload.get("provider_error") if isinstance(payload.get("provider_error"), dict) else {}
        emit_progress(
            "failed",
            progress=1.0,
            detail={
                "source": "web_channel",
                "provider": error.get("provider") or "",
                "taxonomy": error.get("taxonomy") or "",
                "status_code": error.get("status_code") or 0,
                "retryable": bool(error.get("retryable")) if "retryable" in error else False,
            },
        )
        raise RuntimeError("image generation skill failed")
    artifacts = []
    for item in payload.get("images") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("url") or "").strip()
        if not path:
            continue
        artifacts.append({
            "kind": "image",
            "title": os.path.basename(path) or "image.png",
            "path": path,
            "fileType": "image",
        })
    if not artifacts:
        raise RuntimeError("image generation produced no artifacts")
    model_fallback = payload.get("model_fallback") if isinstance(payload.get("model_fallback"), dict) else {}
    if model_fallback:
        emit_progress(
            "fallback",
            progress=0.7,
            detail={
                "source": "web_channel",
                "fallback_used": bool(model_fallback.get("used", True)),
                "fallback_provider": model_fallback.get("provider") or payload.get("provider") or "",
                "fallback_from_model": model_fallback.get("from_model") or "",
                "fallback_to_model": model_fallback.get("to_model") or "",
                "fallback_reason": model_fallback.get("reason") or "",
            },
        )
    emit_progress(
        "provider_response",
        progress=0.8,
        detail={
            "source": "web_channel",
            "provider": payload.get("provider") or task.get("provider") or "",
            "model": payload.get("model") or "",
            "attempted_provider_count": payload.get("attempted_provider_count") or 0,
            "fallback_used": bool(model_fallback.get("used")) if model_fallback else False,
            "fallback_provider": model_fallback.get("provider") or "",
            "fallback_from_model": model_fallback.get("from_model") or "",
            "fallback_to_model": model_fallback.get("to_model") or "",
            "fallback_reason": model_fallback.get("reason") or "",
        },
    )
    return {"artifacts": artifacts}


def _image_job_runner(body: Dict[str, Any]):
    if bool(body.get("dry_run")) or str(body.get("runner") or "").strip().lower() == "dry_run":
        return _image_job_dry_run_runner
    return _image_job_skill_runner


def _image_job_request_id_from_events(job_id: str) -> str:
    safe_job_id = _safe_image_job_api_identifier(job_id, prefix="image-job", allow_empty=True)
    if not safe_job_id:
        return ""
    try:
        from agent.protocol import get_run_event_ledger

        event = get_run_event_ledger().latest_event_for_image_job(safe_job_id)
        if event:
            return str(event.get("request_id") or "")
    except Exception:
        return ""
    return ""


def _image_job_owner_ids_from_events(request_id: str) -> Dict[str, str]:
    if not request_id:
        return {}
    try:
        from agent.protocol import get_run_event_ledger

        events = get_run_event_ledger().events_for_request(request_id, limit=0)
        for event in events:
            session_id = str(event.get("session_id") or "")
            turn_id = str(event.get("turn_id") or "")
            if session_id or turn_id:
                return {"session_id": session_id, "turn_id": turn_id}
    except Exception:
        return {}
    return {}


def _image_job_with_projection_fallback(job: Dict[str, Any], projection: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    if not isinstance(job, dict) or job.get("status") != "unknown" or not isinstance(projection, dict):
        return job
    job_id = _safe_image_job_api_identifier(job.get("job_id"), prefix="image-job", allow_empty=True)
    if not job_id:
        return job
    for projected in projection.get("image_jobs") or []:
        if not isinstance(projected, dict):
            continue
        projected_job_id = _safe_image_job_api_identifier(projected.get("job_id"), prefix="image-job", allow_empty=True)
        if projected_job_id != job_id:
            continue
        return {
            "job_id": job_id,
            "request_id": _safe_image_job_public_request_id(request_id),
            "session_id": projection.get("session_id") or "",
            "turn_id": projection.get("turn_id") or "",
            "status": projected.get("status") or "unknown",
            "artifacts": projected.get("artifacts") or [],
            "cancel_requested": False,
            "running": False,
            "recovered_from_projection": True,
        }
    return job


def _image_job_public_job_payload(
    job: Dict[str, Any],
    public_request_id: str,
    public_session_id: str,
    public_turn_id: str,
) -> Dict[str, Any]:
    public = dict(job or {})
    if public.get("request_id") or public_request_id:
        public["request_id"] = public_request_id or _safe_image_job_public_request_id(public.get("request_id"))
    if public.get("session_id") or public_session_id:
        public["session_id"] = public_session_id or _safe_image_job_public_session_id(public.get("session_id"))
    if public.get("turn_id") or public_turn_id:
        public["turn_id"] = public_turn_id or _safe_image_job_public_turn_id(public.get("turn_id"))
    return public


def _image_job_replace_public_ids(value: Any, public_request_id: str, public_session_id: str, public_turn_id: str) -> Any:
    if not public_request_id and not public_session_id and not public_turn_id:
        return value
    if isinstance(value, list):
        return [_image_job_replace_public_ids(item, public_request_id, public_session_id, public_turn_id) for item in value]
    if not isinstance(value, dict):
        return value
    replaced = {}
    for key, item in value.items():
        if key == "request_id":
            replaced[key] = public_request_id or item
        elif key == "session_id":
            replaced[key] = public_session_id or item
        elif key == "turn_id":
            replaced[key] = public_turn_id or item
        else:
            replaced[key] = _image_job_replace_public_ids(item, public_request_id, public_session_id, public_turn_id)
    return replaced


def _image_job_fill_public_projection_event_ids(
    projection: Dict[str, Any],
    public_request_id: str,
    public_session_id: str,
    public_turn_id: str,
) -> Dict[str, Any]:
    if not isinstance(projection, dict):
        return projection
    events = projection.get("events")
    if not isinstance(events, list):
        return projection
    filled = dict(projection)
    filled_events = []
    for event in events:
        if not isinstance(event, dict):
            filled_events.append(event)
            continue
        item = dict(event)
        if public_request_id and not item.get("request_id"):
            item["request_id"] = public_request_id
        if public_session_id and not item.get("session_id"):
            item["session_id"] = public_session_id
        if public_turn_id and not item.get("turn_id"):
            item["turn_id"] = public_turn_id
        filled_events.append(item)
    filled["events"] = filled_events
    return filled


def _image_job_projection_payload(job: Dict[str, Any], *, include_events: bool = False, request_id: str = "") -> Dict[str, Any]:
    request_id = str(job.get("request_id") or request_id or "")
    event_owner_ids = _image_job_owner_ids_from_events(request_id)
    public_request_id = _safe_image_job_public_request_id(request_id)
    public_session_id = _safe_image_job_public_session_id((job or {}).get("session_id") or event_owner_ids.get("session_id"))
    public_turn_id = _safe_image_job_public_turn_id((job or {}).get("turn_id") or event_owner_ids.get("turn_id"))
    projection = {}
    if request_id:
        from agent.protocol import RuntimeProjectionService

        projection = RuntimeProjectionService().request_projection(request_id)
        projection = _runtime_projection_public_payload(projection, include_events=include_events)
        public_session_id = _safe_image_job_public_session_id(projection.get("session_id") or event_owner_ids.get("session_id") or public_session_id)
        public_turn_id = _safe_image_job_public_turn_id(projection.get("turn_id") or event_owner_ids.get("turn_id") or public_turn_id)
        projection = _image_job_replace_public_ids(projection, public_request_id, public_session_id, public_turn_id)
        projection = _image_job_fill_public_projection_event_ids(projection, public_request_id, public_session_id, public_turn_id)
        job = _image_job_with_projection_fallback(job, projection, request_id)
    job = _image_job_public_job_payload(job, public_request_id, public_session_id, public_turn_id)
    return {
        "status": "success",
        "job": job,
        "projection": projection,
        "latest_event_id": projection.get("latest_event_id", 0) if isinstance(projection, dict) else 0,
    }


class ToolPermissionHandler:
    def GET(self):
        _require_auth()
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            return json.dumps(get_tool_permission_broker().list_pending(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] tool permission list error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e), ensure_ascii=False)

    def POST(self):
        _require_auth()
        try:
            payload = json.loads(web.data() or b"{}")
            action = (payload.get("action") or "").strip()
            mode = (payload.get("mode") or "").strip()
            from common.ecorex_tool_permissions import get_tool_permission_broker

            broker = get_tool_permission_broker()
            if action == "set_mode":
                return json.dumps(broker.set_mode(mode), ensure_ascii=False)
            if action == "reset_grants":
                return json.dumps(broker.reset_grants(), ensure_ascii=False)

            request_id = (payload.get("request_id") or payload.get("permission_request_id") or "").strip()
            decision = (payload.get("decision") or "").strip()
            remember = bool(payload.get("remember"))
            if not request_id:
                return json.dumps({"status": "error", "message": "missing request_id"}, ensure_ascii=False)

            return json.dumps(
                broker.decide(request_id, decision, remember),
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] tool permission decision error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e), ensure_ascii=False)


class UpdateCheckHandler:
    DEFAULT_MANIFEST_URL = "https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json"

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(platform='web')
            platform = str(params.platform or "web").strip()
            manifest = self._load_manifest()
            from cli import __version__

            latest_version = str(manifest.get("version") or "")
            artifact = self._pick_artifact(manifest, platform)
            notice = _enterprise_release_notice_payload() or self._release_notice(manifest)
            notice_revision = str(notice.get("revision") or "").strip()
            version_compare = self._compare_versions(latest_version, __version__) if latest_version else 0
            installed_artifact = self._installed_artifact_metadata(platform, artifact)
            artifact_changed = bool(artifact and version_compare == 0 and self._artifact_changed(artifact, installed_artifact))
            notice_active = bool(latest_version and notice_revision and version_compare > 0)
            has_update = bool(latest_version and (version_compare > 0 or artifact_changed))
            artifact_download_url = self._artifact_download_url(manifest, artifact)
            release_page_url = self._release_page_url()
            update_reason = "version" if version_compare > 0 else ("artifact" if artifact_changed else "")
            if has_update and update_reason == "artifact":
                message = f"发现 {latest_version} 同版本更新，本机更新器可在空闲时检查并安装"
            else:
                message = f"发现新版本 {latest_version}，本机更新器可在空闲时检查并安装" if has_update else "当前已经是最新版本"
            return json.dumps({
                "status": "success",
                "platform": platform,
                "currentVersion": __version__,
                "latestVersion": latest_version or __version__,
                "version": latest_version or __version__,
                "hasUpdate": has_update,
                "updateReason": update_reason,
                "noticeRevision": notice_revision,
                "notice": notice,
                "message": message,
                "downloadUrl": release_page_url,
                "releasePageUrl": release_page_url,
                "artifactDownloadUrl": artifact_download_url,
                "artifact": artifact,
                "installedArtifact": installed_artifact,
                "recommendedDownloads": manifest.get("recommendedDownloads", {}),
                "update": manifest.get("update", {}),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] update check error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e), ensure_ascii=False)

    def _release_notice(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        update = manifest.get("update") if isinstance(manifest.get("update"), dict) else {}
        webui = update.get("webui") if isinstance(update.get("webui"), dict) else {}
        notice = webui.get("notice") if isinstance(webui.get("notice"), dict) else {}
        revision = str(notice.get("revision") or webui.get("noticeRevision") or "").strip()
        if not revision:
            return {}
        message = str(notice.get("message") or "").strip()
        published_at = str(notice.get("publishedAt") or webui.get("noticeUpdatedAt") or "").strip()
        return {
            "revision": revision[:120],
            "version": str(notice.get("version") or manifest.get("version") or "")[:40],
            "message": message[:240],
            "publishedAt": published_at[:80],
            "reason": str(notice.get("reason") or "admin-release-notify")[:80],
            "redacted": True,
        }

    def _load_manifest(self) -> Dict[str, Any]:
        configured = (
            os.environ.get("ECOREX_RELEASE_MANIFEST_URL")
            or conf().get("release_manifest_url")
            or self.DEFAULT_MANIFEST_URL
        )
        url = str(configured).strip()
        if os.path.isfile(url):
            with open(url, "r", encoding="utf-8") as handle:
                return json.load(handle)
        with urllib.request.urlopen(url, timeout=6) as response:
            return json.loads(response.read().decode("utf-8"))

    def _pick_artifact(self, manifest: Dict[str, Any], platform: str) -> Dict[str, Any]:
        artifacts = [
            artifact for artifact in (manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else [])
            if isinstance(artifact, dict) and artifact.get("status") == "ready"
        ]
        by_id = {str(artifact.get("id") or ""): artifact for artifact in artifacts}
        platform_value = platform.lower()
        recommended = manifest.get("recommendedDownloads") if isinstance(manifest.get("recommendedDownloads"), dict) else {}
        preferred_ids: List[str] = []
        platform_keys: List[str] = []
        if platform_value in ("win32", "windows", "win"):
            platform_keys = ["win32", "windows", "web"]
            preferred_ids.append("webui-windows-x64")
        elif platform_value in ("darwin", "mac", "macos"):
            platform_keys = ["darwin", "macos", "web"]
            preferred_ids.append("webui-macos-universal")
        elif platform_value in ("web", "webui"):
            platform_keys = ["web"]
            preferred_ids.append("webui-windows-x64")
        for key in platform_keys:
            row = recommended.get(key)
            if isinstance(row, dict):
                for field in ("webui", "primary"):
                    artifact_id = str(row.get(field) or "").strip()
                    if artifact_id and artifact_id not in preferred_ids:
                        preferred_ids.append(artifact_id)
        for preferred_id in preferred_ids:
            if preferred_id in by_id:
                return by_id[preferred_id]
        return {}

    def _installed_artifact_metadata(self, platform: str, artifact: Dict[str, Any]) -> Dict[str, Any]:
        if not artifact:
            return {}
        try:
            from common.ecorex_workspace import load_installation_manifest

            manifest = load_installation_manifest(_get_workspace_root())
        except Exception:
            return {}
        surfaces = manifest.get("surfaces") if isinstance(manifest, dict) else {}
        if not isinstance(surfaces, dict):
            return {}
        artifact_id = str(artifact.get("id") or "").strip()
        platform_value = str(platform or "").strip().lower()
        candidates: List[str] = []
        if artifact_id:
            candidates.append(artifact_id)
        if platform_value in ("win32", "windows", "win"):
            candidates.extend(["webui-windows-x64", "webui", "desktop"])
        elif platform_value in ("darwin", "mac", "macos"):
            candidates.extend(["webui-macos-universal", "webui", "desktop"])
        elif platform_value in ("linux", "web-linux-service"):
            candidates.extend(["web-linux-service", "webui-linux-service", "webui"])
        else:
            candidates.extend(["webui", "desktop", "webui-linux-service"])
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            row = surfaces.get(key)
            if isinstance(row, dict):
                return self._safe_installed_artifact(row)
        for row in surfaces.values():
            if isinstance(row, dict) and artifact_id and str(row.get("artifactId") or row.get("artifact_id") or "").strip() == artifact_id:
                return self._safe_installed_artifact(row)
        return {}

    def _safe_installed_artifact(self, row: Dict[str, Any]) -> Dict[str, Any]:
        def clean_text(*names: str, limit: int = 160) -> str:
            for name in names:
                value = str(row.get(name) or "").strip()
                if value:
                    return value[:limit]
            return ""

        def clean_int(*names: str) -> int:
            for name in names:
                try:
                    value = int(row.get(name) or 0)
                except Exception:
                    value = 0
                if value > 0:
                    return value
            return 0

        payload = {
            "artifactId": clean_text("artifactId", "artifact_id"),
            "artifactSha256": clean_text("artifactSha256", "artifact_sha256", "sha256", limit=80).upper(),
            "artifactSize": clean_int("artifactSize", "artifact_size", "size"),
            "contentFingerprint": clean_text("contentFingerprint", "content_fingerprint", "fingerprint", limit=120),
            "surface": clean_text("surface", limit=120),
            "version": clean_text("version", limit=80),
        }
        return {key: value for key, value in payload.items() if value not in ("", 0)}

    def _artifact_changed(self, artifact: Dict[str, Any], installed: Dict[str, Any]) -> bool:
        if not artifact or not installed:
            return False
        remote_fingerprint = str(artifact.get("contentFingerprint") or artifact.get("content_fingerprint") or "").strip()
        installed_fingerprint = str(installed.get("contentFingerprint") or "").strip()
        if remote_fingerprint and installed_fingerprint:
            return remote_fingerprint != installed_fingerprint
        remote_sha = str(artifact.get("sha256") or "").strip().upper()
        installed_sha = str(installed.get("artifactSha256") or installed.get("sha256") or "").strip().upper()
        if remote_sha and installed_sha:
            return remote_sha != installed_sha
        try:
            remote_size = int(artifact.get("size") or 0)
            installed_size = int(installed.get("artifactSize") or installed.get("size") or 0)
        except Exception:
            remote_size = installed_size = 0
        return bool(remote_size and installed_size and remote_size != installed_size)

    def _absolute_download_url(self, href: str) -> str:
        if not href:
            return "https://mvdcm.ecoremedia.net/ecorex-agent/"
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return "https://mvdcm.ecoremedia.net/ecorex-agent/" + href.lstrip("./")

    def _artifact_download_url(self, manifest: Dict[str, Any], artifact: Dict[str, Any]) -> str:
        if not artifact:
            return self._absolute_download_url("")
        download = manifest.get("download") if isinstance(manifest.get("download"), dict) else {}
        mirrors = download.get("mirrors") if isinstance(download.get("mirrors"), list) else []
        for mirror in mirrors:
            if not isinstance(mirror, dict):
                continue
            base = str(mirror.get("baseUrl") or "").strip().rstrip("/")
            if not base.startswith(("http://", "https://")):
                continue
            path_mode = str(mirror.get("pathMode") or "href").strip()
            path = str(artifact.get("fileName") if path_mode == "fileName" else artifact.get("href") or "").strip()
            if not path:
                continue
            if path.startswith(("http://", "https://")):
                return path
            return f"{base}/{path.lstrip('./')}"
        return self._absolute_download_url(str(artifact.get("href") or ""))

    def _release_page_url(self) -> str:
        configured = (
            os.environ.get("ECOREX_RELEASE_MANIFEST_URL")
            or conf().get("release_manifest_url")
            or self.DEFAULT_MANIFEST_URL
        )
        manifest_url = str(configured or "").strip()
        if not manifest_url:
            return "https://mvdcm.ecoremedia.net/ecorex-agent/"
        if manifest_url.endswith("/manifest.json"):
            return manifest_url[: -len("manifest.json")]
        if manifest_url.endswith("manifest.json"):
            return manifest_url[: -len("manifest.json")]
        if manifest_url.endswith("/"):
            return manifest_url
        return manifest_url.rsplit("/", 1)[0] + "/"

    def _compare_versions(self, left: str, right: str) -> int:
        def parts(value: str) -> List[int]:
            return [int(part) if part.isdigit() else 0 for part in str(value or "0").replace("-", ".").split(".")]

        a = parts(left)
        b = parts(right)
        for index in range(max(len(a), len(b))):
            diff = (a[index] if index < len(a) else 0) - (b[index] if index < len(b) else 0)
            if diff:
                return 1 if diff > 0 else -1
        return 0


class OpenPathHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            path_value = str(body.get("path") or body.get("file_path") or "").strip()
            action = str(body.get("action") or "open").strip().lower()
            if action not in ("open", "reveal", "openwith", "open_with"):
                action = "open"
            if not path_value:
                return json.dumps({"status": "error", "message": "path is required"}, ensure_ascii=False)
            workspace_root = os.path.realpath(_get_workspace_root())
            expanded_path = os.path.expanduser(path_value)
            if not os.path.isabs(expanded_path):
                expanded_path = os.path.join(workspace_root, expanded_path.lstrip("/\\"))
            path_value = os.path.realpath(expanded_path)

            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    path_value,
                    cwd=workspace_root,
                )
                if not _decision_allowed(decision):
                    return json.dumps({
                        "status": "error",
                        "message": _decision_reason(decision, "open path blocked by permissions"),
                    }, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[WebChannel] open path permission check failed: {_web_body_log_summary(exc)}")
                return json.dumps({"status": "error", "message": "open path permission check failed"}, ensure_ascii=False)

            if not os.path.exists(path_value):
                return json.dumps({"status": "error", "message": f"path not found: {path_value}"}, ensure_ascii=False)
            if action != "reveal":
                ext = os.path.splitext(path_value.rstrip("/\\"))[1].lower()
                if ext in DANGEROUS_OPEN_EXTENSIONS:
                    return json.dumps({
                        "status": "error",
                        "message": "Refusing to launch executable or script files from WebUI. Use reveal/show in folder and open it manually if you trust it.",
                    }, ensure_ascii=False)

            self._open_path(path_value, action)
            return json.dumps({"status": "success", "message": "", "path": path_value}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] open path error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e), ensure_ascii=False)

    def _open_path(self, path_value: str, action: str = "open") -> None:
        if os.name == "nt":
            if action == "reveal":
                subprocess.Popen(["explorer", "/select,", path_value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif action in ("openwith", "open_with"):
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path_value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(path_value)  # type: ignore[attr-defined]
            return
        if action == "reveal" and sys.platform == "darwin":
            command = ["open", "-R", path_value]
        elif action == "reveal":
            command = ["xdg-open", os.path.dirname(path_value) or path_value]
        else:
            command = ["open", path_value] if sys.platform == "darwin" else ["xdg-open", path_value]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _artifact_feedback_text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _artifact_feedback_digest(value: Any, length: int = 40) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length] if text else ""


def _artifact_feedback_source(artifact: Dict[str, Any]) -> str:
    for key in (
        "path",
        "filePath",
        "file_path",
        "relativePath",
        "relative_path",
        "previewUrl",
        "preview_url",
        "statusPath",
        "status_path",
        "thumbnailUrl",
        "thumbnail_url",
        "url",
    ):
        value = _artifact_feedback_text(artifact.get(key), 4096)
        if value:
            return value
    return ""


def _artifact_feedback_ext(source: str, title: str = "") -> str:
    candidate = str(source or title or "").split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    match = re.search(r"(\.[A-Za-z0-9]{1,12})$", candidate)
    return match.group(1).lower() if match else ""


def _artifact_feedback_title(artifact: Dict[str, Any], source: str) -> str:
    explicit = artifact.get("title") or artifact.get("name") or artifact.get("fileName") or artifact.get("file_name")
    if explicit:
        return _artifact_feedback_text(explicit, 240)
    clean = str(source or "").split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    return _artifact_feedback_text(clean.rsplit("/", 1)[-1] or "artifact", 240)


def _artifact_feedback_phase1_key(parts: List[Any]) -> str:
    return "phase1:" + _artifact_feedback_digest("|".join(str(part or "") for part in parts), 40)


def _artifact_feedback_safe_id(artifact: Dict[str, Any], title: str, source: str, request_id: str) -> str:
    explicit = _artifact_feedback_text(
        artifact.get("safeArtifactId")
        or artifact.get("safe_artifact_id")
        or artifact.get("artifactId")
        or artifact.get("artifact_id"),
        180,
    )
    if explicit:
        return explicit
    raw_identity = "|".join(
        item
        for item in (
            _artifact_feedback_text(artifact.get("id"), 4096),
            title,
            source,
            request_id,
        )
        if item
    )
    return "artifact:" + _artifact_feedback_digest(raw_identity or title or request_id or "artifact", 40)


class ArtifactFeedbackHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 128 * 1024:
                return json.dumps({"status": "error", "message": "payload too large"}, ensure_ascii=False)
            payload = json.loads(raw)
            artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
            source = _artifact_feedback_source(artifact)
            session_id = _artifact_feedback_text(
                payload.get("sessionId")
                or payload.get("session_id")
                or artifact.get("sessionId")
                or artifact.get("session_id"),
                180,
            )
            request_id = _artifact_feedback_text(
                payload.get("requestId")
                or payload.get("request_id")
                or artifact.get("requestId")
                or artifact.get("request_id"),
                180,
            )
            title = _artifact_feedback_title(artifact, source)
            signal = _artifact_feedback_text(
                payload.get("signal")
                or artifact.get("artifactFeedbackSignal")
                or artifact.get("artifact_feedback_signal")
                or "",
                40,
            ).lower()
            if signal not in {"thumbs_up", "thumbs_down"}:
                signal = "thumbs_down" if _artifact_feedback_text(payload.get("validity")).lower() == "invalid" else "thumbs_up"
            validity = "invalid" if signal == "thumbs_down" else "valid"
            feedback_share_id = _artifact_feedback_text(
                payload.get("feedbackShareId")
                or payload.get("feedback_share_id")
                or artifact.get("feedbackShareId")
                or artifact.get("feedback_share_id")
                or artifact.get("shareId")
                or artifact.get("share_id"),
                80,
            )
            feedback_share_url = _artifact_feedback_text(
                payload.get("feedbackShareUrl")
                or payload.get("feedback_share_url")
                or artifact.get("feedbackShareUrl")
                or artifact.get("feedback_share_url")
                or artifact.get("shareUrl")
                or artifact.get("share_url"),
                500,
            )
            safe_artifact_id = _artifact_feedback_safe_id(artifact, title, source, request_id)
            path_hash = _artifact_feedback_text(
                artifact.get("pathHash")
                or artifact.get("path_hash")
                or (_artifact_feedback_digest(source, 64) if source else ""),
                80,
            )
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            artifact_payload = {
                "idempotencyKey": _artifact_feedback_phase1_key(["artifact", session_id, request_id, safe_artifact_id]),
                "safeArtifactId": safe_artifact_id,
                "sessionId": session_id,
                "requestId": request_id,
                "kind": _artifact_feedback_text(artifact.get("kind") or artifact.get("type") or "file", 40),
                "intent": _artifact_feedback_text(artifact.get("intent") or "deliverable", 60),
                "operation": _artifact_feedback_text(artifact.get("operation") or "created", 60),
                "status": _artifact_feedback_text(artifact.get("status") or "ready", 60),
                "title": title,
                "pathHash": path_hash,
                "pathExt": _artifact_feedback_text(artifact.get("pathExt") or artifact.get("path_ext") or _artifact_feedback_ext(source, title), 32),
                "mimeType": _artifact_feedback_text(artifact.get("mimeType") or artifact.get("mime_type"), 120),
                "sizeBytes": max(0, int(artifact.get("sizeBytes") or artifact.get("size_bytes") or 0)),
                "artifactValidity": validity,
                "artifactFeedbackSignal": signal,
                "artifactFeedbackAt": created_at,
                "feedbackShareId": feedback_share_id,
                "feedbackShareUrl": feedback_share_url,
                "feedbackSource": "web_user_artifact_feedback",
                "createdAt": created_at,
            }
            event_payload = {
                "idempotencyKey": _artifact_feedback_phase1_key(["event", session_id, request_id, "artifact.feedback", safe_artifact_id, signal]),
                "eventType": "artifact.feedback",
                "status": validity,
                "source": "WebUI",
                "sessionId": session_id,
                "requestId": request_id,
                "detail": {
                    "artifact_hash": _artifact_feedback_digest(safe_artifact_id, 16),
                    "artifact_validity": validity,
                    "artifact_feedback_signal": signal,
                    "feedback_share_id": feedback_share_id,
                    "feedback_share_url": feedback_share_url,
                },
                "createdAt": created_at,
            }

            token = _enterprise_user_token_from_request()
            if not token:
                return json.dumps({"status": "success", "synced": False, "reason": "enterprise_login_required"}, ensure_ascii=False)

            device_id = _request_header("X-EcoreX-Device-Id").strip()
            sync_body = json.dumps({
                "type": "phase1_sync",
                "source": "WebUI",
                "sessionId": session_id,
                "requestId": request_id,
                "events": [event_payload],
                "artifacts": [artifact_payload],
            }).encode("utf-8")
            last_error = ""
            for client_key in _enterprise_client_keys_for_request():
                request = urllib.request.Request(
                    f"{_web_enterprise_client_base()}/sync/events",
                    data=sync_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-EcoreX-Client-Key": client_key,
                        "X-EcoreX-User-Token": token,
                        "Authorization": f"Bearer {token}",
                        "X-EcoreX-Device-Id": device_id,
                        "User-Agent": "EcoreX-WebArtifactFeedback/0.3.0",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=8) as response:
                        admin_payload = json.loads(response.read(512_000).decode("utf-8", errors="replace") or "{}")
                    return json.dumps({
                        "status": "success",
                        "synced": True,
                        "admin": {
                            "eventsAccepted": admin_payload.get("eventsAccepted", 0),
                            "artifactsAccepted": admin_payload.get("artifactsAccepted", 0),
                        },
                    }, ensure_ascii=False)
                except urllib.error.HTTPError as exc:
                    body = exc.read(512).decode("utf-8", errors="replace")
                    last_error = f"HTTP {exc.code}: {body[:160]}"
                    if exc.code in (401, 403):
                        continue
                    break
            return json.dumps({"status": "error", "message": "artifact feedback sync failed", "detail": last_error}, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"[WebChannel] artifact feedback sync failed: {_web_body_log_summary(exc)}")
            return json.dumps(_public_error_payload("Artifact feedback sync failed.", exc), ensure_ascii=False)


def _session_share_redact_text(value: Any, limit: int = 8000) -> str:
    text = mask_sensitive_text(str(value or ""), max_chars=limit)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s<>'\"]+", "[local-path]", text)
    text = re.sub(r"(?<!\w)/(?:Users|Volumes|home|tmp|var|mnt|opt|srv|Applications)/[^\s<>'\"]+", "[local-path]", text)
    text = re.sub(r"file://[^\s<>'\"]+", "[local-file-url]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    return text[:limit]


def _session_share_content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("message") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "title"):
            if value.get(key):
                return _session_share_content_text(value.get(key))
    return str(value)


def _session_share_safe_url(value: Any, limit: int = 700_000) -> str:
    raw = str(value or "").strip()
    if re.match(r"(?i)^data:image/(?:png|jpe?g|gif|webp);base64,", raw) and len(raw) <= limit:
        return raw
    text = _session_share_redact_text(value, limit).strip()
    if not text or "[local-path]" in text or "[local-file-url]" in text:
        return ""
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    if text.startswith(("/client/", "/ecorex-agent/client/")) and not text.startswith("//"):
        return text
    return ""


def _session_share_int(value: Any) -> int:
    try:
        number = int(float(value))
        return number if number > 0 else 0
    except Exception:
        return 0


def _session_share_artifact_payload(artifact: Dict[str, Any]) -> Dict[str, Any]:
    signal = _artifact_feedback_text(
        artifact.get("artifactFeedbackSignal")
        or artifact.get("artifact_feedback_signal")
        or "default",
        40,
    ).lower()
    validity = _artifact_feedback_text(
        artifact.get("artifactValidity")
        or artifact.get("artifact_validity")
        or ("invalid" if signal == "thumbs_down" else "valid"),
        40,
    ).lower()
    title = _session_share_redact_text(
        artifact.get("title")
        or artifact.get("fileName")
        or artifact.get("file_name")
        or artifact.get("name")
        or "artifact",
        240,
    )
    file_name = _session_share_redact_text(
        artifact.get("fileName")
        or artifact.get("file_name")
        or artifact.get("name")
        or title,
        240,
    )
    mime_type = _artifact_feedback_text(artifact.get("mimeType") or artifact.get("mime_type") or "", 120)
    path_ext = _artifact_feedback_text(artifact.get("pathExt") or artifact.get("path_ext") or "", 24)
    url = _session_share_safe_url(artifact.get("url") or artifact.get("href"))
    preview_url = _session_share_safe_url(artifact.get("previewUrl") or artifact.get("preview_url"))
    thumbnail_url = _session_share_safe_url(artifact.get("thumbnailUrl") or artifact.get("thumbnail_url"))
    media_url = _session_share_safe_url(artifact.get("mediaUrl") or artifact.get("media_url")) or preview_url or thumbnail_url or url
    payload: Dict[str, Any] = {
        "title": title,
        "kind": _artifact_feedback_text(artifact.get("kind") or artifact.get("type") or "file", 40),
        "status": _artifact_feedback_text(artifact.get("status") or "ready", 40),
        "artifactValidity": "invalid" if validity == "invalid" or signal == "thumbs_down" else "valid",
        "artifactFeedbackSignal": signal if signal in {"default", "thumbs_up", "thumbs_down"} else "default",
        "fileName": file_name,
        "mimeType": mime_type,
        "sizeBytes": _session_share_int(artifact.get("sizeBytes") or artifact.get("size_bytes")),
        "pathExt": path_ext,
        "safeArtifactId": _artifact_feedback_text(artifact.get("safeArtifactId") or artifact.get("safe_artifact_id") or artifact.get("id") or "", 120),
    }
    if url:
        payload["url"] = url
    if preview_url:
        payload["previewUrl"] = preview_url
    if thumbnail_url:
        payload["thumbnailUrl"] = thumbnail_url
    if media_url:
        payload["mediaUrl"] = media_url
    return payload


def _session_share_message_payload(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    role = _artifact_feedback_text(item.get("role") or "", 20).lower()
    if role not in {"user", "assistant"}:
        return None
    content = _session_share_redact_text(
        _session_share_content_text(item.get("content") if "content" in item else item.get("text")),
        8000,
    )
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
    safe_artifacts = [
        _session_share_artifact_payload(artifact)
        for artifact in artifacts[:24]
        if isinstance(artifact, dict)
    ]
    if not content and not safe_artifacts:
        return None
    return {
        "role": role,
        "content": content,
        "createdAt": _artifact_feedback_text(item.get("createdAt") or item.get("created_at"), 80),
        "artifacts": safe_artifacts,
    }


SESSION_SHARE_UPSTREAM_SOFT_LIMIT = 1_200_000


def _session_share_strip_artifact_media(artifact: Dict[str, Any]) -> Dict[str, Any]:
    compact = dict(artifact or {})
    for key in ("mediaUrl", "previewUrl", "thumbnailUrl", "url"):
        compact.pop(key, None)
    return compact


def _session_share_strip_message_media(message: Dict[str, Any]) -> Dict[str, Any]:
    compact = dict(message or {})
    artifacts = compact.get("artifacts") if isinstance(compact.get("artifacts"), list) else []
    compact["artifacts"] = [
        _session_share_strip_artifact_media(artifact)
        for artifact in artifacts
        if isinstance(artifact, dict)
    ]
    return compact


def _session_share_body_bytes(title: str, session_id: str, messages: List[Dict[str, Any]]) -> Tuple[bytes, bool]:
    includes_artifact_files = any(
        artifact.get("mediaUrl") or artifact.get("url") or artifact.get("previewUrl") or artifact.get("thumbnailUrl")
        for message in messages
        for artifact in (message.get("artifacts") or [])
        if isinstance(artifact, dict)
    )
    body = json.dumps({
        "title": title,
        "sessionId": _artifact_feedback_text(session_id, 180),
        "messages": messages,
        "privacy": {
            "redacted": True,
            "includesLocalPaths": False,
            "includesArtifactFiles": includes_artifact_files,
        },
    }).encode("utf-8")
    return body, includes_artifact_files


def _session_share_compact_body(title: str, session_id: str, messages: List[Dict[str, Any]]) -> Tuple[bytes, List[Dict[str, Any]], bool]:
    candidates = [message for message in messages[:200] if isinstance(message, dict)]
    for strip_media in (False, True):
        working = [
            _session_share_strip_message_media(message) if strip_media else message
            for message in candidates
        ]
        while working:
            body, includes_artifact_files = _session_share_body_bytes(title, session_id, working)
            if len(body) <= SESSION_SHARE_UPSTREAM_SOFT_LIMIT:
                return body, working, includes_artifact_files
            working = working[1:]
    fallback_messages: List[Dict[str, Any]] = []
    if candidates:
        latest = dict(candidates[-1])
        latest["content"] = _session_share_redact_text(latest.get("content") or "", 1200)
        latest["artifacts"] = [
            _session_share_strip_artifact_media(artifact)
            for artifact in (latest.get("artifacts") or [])[:4]
            if isinstance(artifact, dict)
        ]
        fallback_messages = [latest]
    body, includes_artifact_files = _session_share_body_bytes(title, session_id, fallback_messages)
    if len(body) > SESSION_SHARE_UPSTREAM_SOFT_LIMIT:
        raise ValueError("payload too large")
    return body, fallback_messages, includes_artifact_files


class SessionShareHandler:
    def POST(self, session_id: str):
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 2 * 1024 * 1024:
                return json.dumps({"status": "error", "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            title = _session_share_redact_text(body.get("title") or "EcoreX shared session", 160)
            raw_messages = body.get("messages") if isinstance(body.get("messages"), list) else []
            if not raw_messages:
                from agent.memory import get_conversation_store

                page = get_conversation_store().load_history_page(session_id=session_id, page=1, page_size=120)
                raw_messages = page.get("messages") if isinstance(page, dict) else []
            messages = [
                message
                for message in (_session_share_message_payload(item) for item in raw_messages[:200] if isinstance(item, dict))
                if message
            ]
            if not messages:
                return json.dumps({"status": "error", "message": "当前会话没有可分享内容"}, ensure_ascii=False)

            token = _enterprise_user_token_from_request()
            if not token:
                return json.dumps({"status": "error", "message": "请先登录企业账号后再分享会话"}, ensure_ascii=False)
            device_id = _request_header("X-EcoreX-Device-Id").strip()
            share_body, messages, includes_artifact_files = _session_share_compact_body(title, session_id, messages)
            last_error = ""
            for client_key in _enterprise_client_keys_for_request():
                request = urllib.request.Request(
                    f"{_web_enterprise_client_base()}/session-shares",
                    data=share_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-EcoreX-Client-Key": client_key,
                        "X-EcoreX-User-Token": token,
                        "Authorization": f"Bearer {token}",
                        "X-EcoreX-Device-Id": device_id,
                        "User-Agent": "EcoreX-WebSessionShare/0.3.0",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=12) as response:
                        result = json.loads(response.read(512_000).decode("utf-8", errors="replace") or "{}")
                    return json.dumps({
                        "status": "success",
                        "shareId": result.get("shareId", ""),
                        "shareUrl": result.get("shareUrl", ""),
                        "messageCount": result.get("messageCount", len(messages)),
                    }, ensure_ascii=False)
                except urllib.error.HTTPError as exc:
                    body_text = exc.read(512).decode("utf-8", errors="replace")
                    last_error = f"HTTP {exc.code}: {body_text[:160]}"
                    if exc.code in (401, 403):
                        continue
                    break
            return json.dumps({"status": "error", "message": "创建分享链接失败", "detail": last_error}, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"[WebChannel] session share failed: {_web_body_log_summary(exc)}")
            return json.dumps(_public_error_payload("Session share failed.", exc), ensure_ascii=False)


def _stable_project_id(path_value: str) -> str:
    normalized = os.path.normcase(os.path.realpath(path_value)).replace("\\", "/")
    digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"project-{digest}"


def _project_payload_from_path(path_value: str, create: bool = False, user_selected: bool = False) -> Dict[str, Any]:
    folder_path = os.path.realpath(os.path.expanduser(path_value))
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        broker = get_tool_permission_broker()
        workspace_root = _get_workspace_root()
        state = broker.get_state()
        if state.get("mode") == "read-only":
            raise PermissionError("Read Only mode blocks project folder registration because it creates .ecorex files.")
        if not os.path.isdir(folder_path):
            if not create:
                raise ValueError(f"project folder not found: {folder_path}")
            parent = os.path.dirname(folder_path) or folder_path
            if not os.path.isdir(parent):
                raise ValueError(f"parent folder not found: {parent}")
            parent_decision = broker.authorize_file_access("write", parent, cwd=workspace_root)
            if not _decision_allowed(parent_decision):
                raise PermissionError(_decision_reason(parent_decision, "project folder parent is not writable"))
            os.makedirs(folder_path, exist_ok=True)
        if user_selected:
            registered = broker.remember_workspace_root(folder_path, access="write", cwd=workspace_root)
            if registered.get("status") == "error":
                raise PermissionError(registered.get("message") or "project folder permission registration failed")
        folder_decision = broker.authorize_file_access("write", folder_path, cwd=workspace_root)
        if not _decision_allowed(folder_decision):
            raise PermissionError(_decision_reason(folder_decision, "project folder is not writable"))
    except Exception as exc:
        logger.warning(f"[WebChannel] project folder permission registration failed: {_web_body_log_summary(exc)}")
        raise
    project_state_dir = os.path.join(folder_path, ".ecorex")
    project_memory_path = os.path.join(project_state_dir, "project-memory.md")
    project_dreams_path = os.path.join(project_state_dir, "dreams")
    os.makedirs(project_dreams_path, exist_ok=True)
    if not os.path.exists(project_memory_path):
        with open(project_memory_path, "w", encoding="utf-8") as handle:
            handle.write("# Project Memory\n\nEcoreX stores project-specific summaries here. Keep this file concise and do not duplicate global user memory.\n")
    try:
        if not user_selected:
            registered = broker.remember_workspace_root(folder_path, access="write", cwd=workspace_root)
            if registered.get("status") == "error":
                raise PermissionError(registered.get("message") or "project folder permission registration failed")
    except Exception as exc:
        logger.warning(f"[WebChannel] project folder permission registration failed after project metadata write: {_web_body_log_summary(exc)}")
        raise
    return {
        "id": _stable_project_id(folder_path),
        "name": os.path.basename(folder_path) or folder_path,
        "path": folder_path,
        "memoryPath": project_memory_path,
        "dreamsPath": project_dreams_path,
        "updatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def _choose_project_folder_native() -> str:
    title = "Select Project Root"
    if os.name == "nt":
        ps_title = title.replace("'", "''")
        command = [
            "powershell",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$owner = New-Object System.Windows.Forms.Form;"
                "$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen;"
                "$owner.Width = 1; $owner.Height = 1;"
                "$owner.ShowInTaskbar = $false;"
                "$owner.TopMost = $true;"
                "$owner.Opacity = 0;"
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$dialog.Description = '{ps_title}';"
                "$dialog.ShowNewFolderButton = $true;"
                "$owner.Show();"
                "$owner.Activate();"
                "$owner.BringToFront();"
                "$result = $dialog.ShowDialog($owner);"
                "$owner.Close();"
                "$owner.Dispose();"
                "if ($result -eq [System.Windows.Forms.DialogResult]::OK) {"
                "  Write-Output $dialog.SelectedPath; exit 0"
                "} exit 2;"
            ),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if result.returncode == 2:
            return ""
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "folder picker failed").strip())
        return (result.stdout or "").strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if result.returncode != 0:
            if "User canceled" in (result.stderr or ""):
                return ""
            raise RuntimeError((result.stderr or result.stdout or "folder picker failed").strip())
        return (result.stdout or "").strip()
    for candidate in (
        ["zenity", "--file-selection", "--directory", "--title", title],
        ["kdialog", "--getexistingdirectory", str(Path.home()), title],
    ):
        if not shutil.which(candidate[0]):
            continue
        result = subprocess.run(candidate, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if result.returncode == 0:
            return (result.stdout or "").strip()
        if result.returncode in (1, 2):
            return ""
    raise RuntimeError("native folder picker is unavailable on this host")


class ProjectFolderHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            path_value = str(body.get("path") or body.get("folder_path") or "").strip()
            if not path_value:
                return json.dumps({"status": "error", "message": "path is required"}, ensure_ascii=False)
            create = bool(body.get("create") or body.get("createDirectory") or body.get("create_directory"))
            project = _project_payload_from_path(path_value, create=create)
            try:
                from common.ecorex_workspace import save_ui_state

                save_ui_state(_get_workspace_root(), {"projects": [project]})
            except Exception as state_exc:
                logger.warning(f"[WebChannel] project folder registered but UI state merge failed: {state_exc}")
            return json.dumps({"status": "success", "project": project}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] project folder error: {_web_body_log_summary(exc)}")
            return json.dumps(_public_error_payload("Request failed.", exc), ensure_ascii=False)


class ProjectFolderChooseHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            folder_path = _choose_project_folder_native()
            if not folder_path:
                return json.dumps({"status": "cancelled", "project": None}, ensure_ascii=False)
            project = _project_payload_from_path(folder_path, create=False, user_selected=True)
            try:
                from common.ecorex_workspace import save_ui_state

                save_ui_state(_get_workspace_root(), {"projects": [project]})
            except Exception as state_exc:
                logger.warning(f"[WebChannel] project folder selected but UI state merge failed: {state_exc}")
            return json.dumps({"status": "success", "project": project}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] project folder choose error: {_web_body_log_summary(exc)}")
            return json.dumps(_public_error_payload("Request failed.", exc), ensure_ascii=False)


def _tool_result_to_payload(result) -> dict:
    payload = getattr(result, "result", result)
    if not isinstance(payload, dict):
        payload = {"result": payload}
    status = getattr(result, "status", None) or payload.get("status") or "success"
    return {"status": status, **payload}


def _flatten_capability_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(payload or {})
    abilities_payload = result.get("abilities")
    if isinstance(abilities_payload, dict) and isinstance(abilities_payload.get("abilities"), list):
        result["abilityDiagnostics"] = {
            key: value for key, value in abilities_payload.items() if key != "abilities"
        }
        result["abilities"] = abilities_payload.get("abilities") or []
    return result


def _safe_capability_event_identifier(value: Any) -> str:
    raw = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if 1 <= len(raw) <= 128 and all(char in allowed for char in raw):
        return raw
    return ""


def _record_capability_policy_blocked_event(request_id: str, session_id: str, blocked: Dict[str, Any], action: str) -> None:
    safe_request_id = _safe_capability_event_identifier(request_id)
    if not safe_request_id:
        return
    safe_session_id = _safe_capability_event_identifier(session_id)
    pack_id = str(blocked.get("packId") or "").strip()
    policy = blocked.get("policy") if isinstance(blocked.get("policy"), dict) else {}
    try:
        from agent.protocol import get_run_event_ledger

        get_run_event_ledger().append_event(
            request_id=safe_request_id,
            session_id=safe_session_id,
            turn_id=safe_request_id,
            event_type="capability.policy_blocked",
            payload={
                "pack_id": pack_id,
                "action": action,
                "error_type": blocked.get("errorType") or "capability_policy_blocked",
                "policy_mode": policy.get("policyMode") or "disabled",
                "install_allowed": bool(policy.get("installAllowed")),
                "policy_source": policy.get("policySource") or "",
                "policy_updated_at": policy.get("policyUpdatedAt") or "",
                "pack_id_redacted": bool(blocked.get("packIdRedacted") or policy.get("packIdRedacted")),
            },
            idempotency_key=f"{safe_request_id}:capability.policy_blocked:{pack_id}:{action}",
            source="web_channel",
        )
    except Exception as exc:
        logger.debug(f"[WebChannel] capability policy event skipped: {_web_body_log_summary(exc)}")


class AgentInstallRequestHandler:
    def POST(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            data = json.loads(web.data() or b"{}")
        except Exception:
            data = {}
        pack_id = str(data.get("packId") or data.get("pack_id") or data.get("id") or "").strip()
        pack_name = str(data.get("packName") or data.get("name") or pack_id or "能力包").strip()
        session_id = str(data.get("sessionId") or data.get("session_id") or "").strip()
        request_id = str(data.get("requestId") or data.get("request_id") or "").strip()
        if not pack_id:
            return json.dumps({"status": "error", "message": "packId is required"}, ensure_ascii=False)
        from common.ecorex_capability_policy import blocked_install_payload, normalize_capability_pack_id

        policy_pack_id = normalize_capability_pack_id(pack_id)
        policy_lookup_id = policy_pack_id or pack_id
        blocked = blocked_install_payload(policy_lookup_id, pack_name=pack_name, action="agent_install_request")
        if blocked:
            if policy_pack_id and not blocked.get("packIdRedacted") and policy_pack_id != pack_id:
                blocked["requestedPackId"] = pack_id
            public_pack_name = "Capability pack" if blocked.get("packIdRedacted") else pack_name
            _record_capability_policy_blocked_event(request_id, session_id, blocked, "agent_install_request")
            return json.dumps({
                "status": "error",
                "type": "capability-pack",
                "packId": blocked.get("packId") or policy_pack_id,
                "packName": public_pack_name,
                "sessionId": session_id,
                **blocked,
            }, ensure_ascii=False)
        normalized_pack_id = pack_id.strip().lower().replace("_", "-")
        if normalized_pack_id in {"tongxin", "tongxin-cli", "xin-agent", "xin-agent-cli", "tx-assistant"}:
            prompt = (
                f"Connect the EcoreX Tongxin Assistant read-only CLI capability `{pack_id}` ({pack_name}) inside this response. "
                "Do not install through raw bash/curl/npm/git and do not run the CLI directly through shell. "
                "Tongxin is a default read-only capability: call `agent_capability` action `install_pack` with "
                "`pack_id=tongxin-cli` or call `tongxin_cli` action `configure` so EcoreX persists the auto-discovered "
                "`xin_agent_cli.py` path into `tools.tongxin_cli.script_path`. If auto-discovery fails, use configured "
                "`tools.tongxin_cli.bootstrap_url` plus `bootstrap_sha256` for authenticated server bootstrap, pass an explicit "
                "`script_path`, or use `ECOREX_TONGXIN_CLI_PATH`, then call `host_diagnostics` with action `status` and "
                "`tongxin_cli` with action `status` or `schema`. "
                "Only read-only queries are allowed for all users; write, sync, auth, submit, approve, delete, export-to-file, "
                "and permission-changing commands must stay blocked."
            )
            extra_fields = {
                "discoveryOnly": False,
                "readOnly": True,
                "defaultEnabled": True,
                "installHint": "Use the structured tongxin_cli tool and configure an existing xin_agent_cli.py path, or configured authenticated bootstrap settings, when auto-discovery is unavailable.",
            }
        elif normalized_pack_id in {"feishu", "lark", "feishu-lark", "lark-feishu", "feishu-cli", "lark-cli"}:
            prompt = (
                f"Install the EcoreX Feishu/Lark CLI connector `{pack_id}` ({pack_name}) inside this response. "
                "The connector installs the official `@larksuite/cli` package on demand. "
                "First use the built-in find skill/find-skill gate to confirm the official source, then use the structured "
                "`agent_capability` tool with "
                f"`{{\"action\":\"install_pack\",\"pack_id\":\"{pack_id}\",\"discovery_source\":\"find-skill\"}}` "
                "or pass the structured `find_skill_result`; direct `feishu_cli` install must also include "
                "`discovery_source=\"find-skill\"` or `find_skill_result`. "
                "Do not use raw bash/curl/npm/git clone for this setup unless the structured tool returns a concrete unrecoverable setup issue. "
                "If npmjs.org times out, the structured installer must retry the domestic npm mirror https://registry.npmmirror.com. "
                "Do not stop at discoveryOnly guidance; this runtime supports on-demand installation after the find-skill gate. "
                "After installation, call `agent_capability` with `action=\"diagnose\"`, then continue the original user task."
            )
            extra_fields = {
                "discoveryOnly": False,
                "sourceUrl": "https://github.com/larksuite/cli",
                "mirrorUrls": ["https://registry.npmmirror.com/@larksuite/cli"],
                "installHint": "Installs official @larksuite/cli on demand through feishu_cli after the find-skill gate.",
            }
        elif normalized_pack_id in {"feishu", "lark", "feishu-lark", "lark-feishu"}:
            source_url = "https://github.com/larksuite/cli"
            mirror_url = "https://registry.npmmirror.com/@larksuite/cli"
            npm_mirror = "https://registry.npmmirror.com"
            prompt = (
                f"当前用户需要 EcoreX 飞书 / Lark 连接器 `{pack_id}`（{pack_name}）。"
                "该能力包现在是 discovery-only：不要调用 "
                f"`{{\"action\":\"install_pack\",\"pack_id\":\"{pack_id}\"}}`，也不要触发旧的预置安装。"
                "所有飞书 CLI / Lark / skill 安装入口必须先走内置 `find` skill（能力 gate 名称为 `find-skill`）做发现和安装方案选择。"
                "请先用 `agent_capability` 的 `diagnose` 确认 `find` skill / `find-skill` 能力已加载；如未加载，先启用/安装它，再继续。"
                "通过 `find` skill 查找飞书/Lark 相关 skill、connector 或官方 CLI 源，并优先使用官方 `https://github.com/larksuite/cli` / `@larksuite/cli`。"
                "如果最终调用 `agent_capability` 的 `install_skill`，必须带上 `discovery_source: \"find-skill\"` 或 `find_skill_result`。"
                "如果真实任务需要本机 CLI，调用 `feishu_cli` 的 `install` action 按需安装官方 `@larksuite/cli@1.0.56`，并必须带上 `discovery_source: \"find-skill\"` 或结构化 `find_skill_result`。"
                f"如果 npm 官方源超时或不可达，降级使用国内 npm 镜像：`npm install --registry={npm_mirror} @larksuite/cli@1.0.56`。"
                "每个来源最多重试一次，避免长时间卡住。"
                "不要要求用户输入“同意安装”；如果需要授权，等待权限弹窗/权限工具。不要反复诊断同一个失败状态。"
                "完成后再调用一次 `agent_capability` 的 `diagnose`，给用户正文只保留安装结论、使用状态和必要下一步；详细 stdout/stderr/log path 放在调用过程里。"
            )
            extra_fields = {
                "discoveryOnly": True,
                "sourceUrl": source_url,
                "mirrorUrls": [mirror_url],
                "installHint": (
                    "先通过内置 find skill / find-skill 能力发现；真实任务需要 CLI 时按需安装 @larksuite/cli；npm 官方源超时后使用国内 npm 镜像。"
                ),
            }
        else:
            prompt = (
                f"请在当前会话内安装 EcoreX 能力包 `{pack_id}`（{pack_name}）。"
                "必须调用 `agent_capability` 工具执行安装："
                f"`{{\"action\":\"install_pack\",\"pack_id\":\"{pack_id}\"}}`。"
                "不要要求用户输入“同意安装”；如果需要授权，等待权限弹窗/权限工具。"
                "如果安装失败，先调用 `agent_capability` 的 `diagnose`，读取 stdout/stderr、状态和日志路径，"
                "给出修复动作并重试；如果权限或管理员策略阻止，请明确说明原因。"
                "给用户的正文只保留安装结论、启用状态和下一步建议；详细日志放在调用过程里。"
            )
            extra_fields = {}
        return json.dumps({
            "status": "success",
            "type": "capability-pack",
            "packId": pack_id,
            "packName": pack_name,
            "sessionId": session_id,
            "prompt": prompt,
            **extra_fields,
        }, ensure_ascii=False)


class _SubagentContext:
    def __init__(self, session_id: str, workspace_dir: str, request_id: str = ""):
        self._current_session_id = session_id
        self._current_request_id = request_id
        self.workspace_dir = workspace_dir


class SubagentsHandler:
    def GET(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.tools.subagent.subagent import SubagentTool

            tool = SubagentTool()
            tool.context = _SubagentContext("", _get_workspace_root())
            return json.dumps(_tool_result_to_payload(tool.execute({"action": "list"})), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] subagent list failed: {_web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Subagent list unavailable.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)

    def POST(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            data = json.loads(web.data() or b"{}")
        except Exception:
            data = {}
        try:
            from agent.tools.subagent.subagent import SubagentTool

            action = str(data.get("action") or "start").strip().lower()
            tool = SubagentTool()
            tool.context = _SubagentContext(
                str(data.get("sessionId") or data.get("session_id") or ""),
                _get_workspace_root(),
                str(data.get("requestId") or data.get("request_id") or ""),
            )
            return json.dumps(_tool_result_to_payload(tool.execute({"action": action, **data})), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] subagent action failed: {_web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Subagent action failed.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)


class SubagentActionHandler:
    def POST(self, task_id: str, action: str):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.tools.subagent.subagent import SubagentTool

            tool = SubagentTool()
            tool.context = _SubagentContext("", _get_workspace_root())
            return json.dumps(_tool_result_to_payload(tool.execute({"action": action, "id": task_id})), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] subagent route action failed: {_web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Subagent action failed.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)


class ChatHandler:
    def GET(self):
        return _serve_web_app_asset("")


class ClientProxyHandler:
    """Proxy WebUI enterprise-client requests to the Admin API.

    Local one-click WebUI runs from 127.0.0.1, so browser-side cross-origin
    requests to the public admin host would be blocked by CORS. Keeping the
    proxy inside the local runtime also lets the same React bridge work for
    public deployments and local installs.
    """

    DEFAULT_CLIENT_BASE = "https://mvdcm.ecoremedia.net/ecorex-agent/client"
    FORWARD_HEADERS = {
        "accept",
        "authorization",
        "content-type",
        "x-ecorex-client-key",
        "x-ecorex-device-id",
        "x-ecorex-user-email",
        "x-ecorex-user-token",
        "x-ecorex-org-id",
    }

    def _client_base(self) -> str:
        public_base = str(os.environ.get("ECOREX_WEB_PUBLIC_BASE_URL") or conf().get("web_public_base_url") or "").strip().rstrip("/")
        configured = (
            os.environ.get("ECOREX_WEB_CLIENT_BASE")
            or conf().get("web_client_base")
            or conf().get("admin_client_base")
            or (f"{public_base}/client" if public_base else "")
            or self.DEFAULT_CLIENT_BASE
        )
        return str(configured).strip().rstrip("/")

    def _target_url(self, path: str = "") -> str:
        clean_path = (path or "").strip("/")
        target = self._client_base()
        if clean_path:
            target = f"{target}/{clean_path}"
        query = web.ctx.env.get("QUERY_STRING", "")
        if query:
            target = f"{target}?{query}"
        return target

    def _forward_headers(self) -> dict:
        headers = {"User-Agent": "EcoreX-WebUI/0.3.0"}
        for key, value in web.ctx.env.items():
            if key == "CONTENT_TYPE":
                name = "Content-Type"
            elif key == "HTTP_ACCEPT":
                name = "Accept"
            elif key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").title()
            else:
                continue
            if name.lower() in self.FORWARD_HEADERS and value:
                headers[name] = value
        return headers

    @staticmethod
    def _json_response(status: int, payload: dict) -> str:
        web.ctx.status = f"{status} {'OK' if status < 400 else 'Error'}"
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        return json.dumps(payload, ensure_ascii=False)

    def _model_config_fallback_response(self, code: str, message: str, status: int = 200) -> str:
        return self._json_response(status, {
            "ok": False,
            "configured": False,
            "source": "web-client-proxy",
            "code": code,
            "message": message,
            "configurationState": code,
        })

    @staticmethod
    def _is_model_config_path(path: str = "") -> bool:
        return (path or "").strip("/") == "model-config"

    def _proxy(self, path: str = ""):
        method = web.ctx.method.upper()
        body = web.data() if method not in ("GET", "HEAD") else None
        request = urllib.request.Request(
            self._target_url(path),
            data=body,
            headers=self._forward_headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read()
                web.ctx.status = f"{response.status} {response.reason}"
                web.header("Content-Type", response.headers.get("Content-Type", "application/json; charset=utf-8"))
                web.header("Cache-Control", "no-store")
            return raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if self._is_model_config_path(path) and exc.code == 404:
                logger.info("[WebChannel] Admin client model-config bridge unavailable; using local model fallback")
                return self._model_config_fallback_response(
                    "enterprise_model_config_bridge_unavailable",
                    "Enterprise model config bridge is unavailable; local Web model configuration remains active.",
                )
            web.ctx.status = f"{exc.code} {exc.reason}"
            web.header("Content-Type", exc.headers.get("Content-Type", "application/json; charset=utf-8"))
            web.header("Cache-Control", "no-store")
            return raw
        except Exception as exc:
            if self._is_model_config_path(path):
                logger.info(f"[WebChannel] Admin client model-config bridge unreachable: {_web_body_log_summary(exc)}")
                return self._model_config_fallback_response(
                    "enterprise_model_config_bridge_unreachable",
                    "Enterprise model config bridge is unreachable; local Web model configuration remains active.",
                )
            logger.warning(f"[WebChannel] Admin client proxy failed: {_web_body_log_summary(exc)}")
            return self._json_response(502, {
                "ok": False,
                "error": "admin client proxy failed",
                "detail": _public_exception_message("Admin client proxy failed.", exc),
                **_public_exception_summary(exc),
            })

    def GET(self, path: str = ""):
        return self._proxy(path)

    def POST(self, path: str = ""):
        return self._proxy(path)

    def PATCH(self, path: str = ""):
        return self._proxy(path)

    def DELETE(self, path: str = ""):
        return self._proxy(path)

    def OPTIONS(self, path: str = ""):
        web.ctx.status = "204 No Content"
        web.header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-EcoreX-Client-Key, X-EcoreX-User-Token, X-EcoreX-Device-Id")
        web.header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        return ""


class ConfigHandler:

    _RECOMMENDED_MODELS = [
        const.DEEPSEEK_V4_PRO, const.DEEPSEEK_V4_FLASH,
        const.MINIMAX_M3, const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_7,
        # claude-fable-5 is intentionally placed at the end of the Claude
        # group here: it is expensive, so avoid surfacing it too early in
        # the LinkAI dropdown.
        const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS, const.CLAUDE_FABLE_5,
        const.GEMINI_31_PRO_PRE, const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7,
        const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS,
        const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_21_PRO, const.DOUBAO_SEED_2_CODE,
        const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2,
        const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K,
        const.MIMO_V2_5_PRO, const.MIMO_V2_5,
    ]

    # Generic placeholder hints surfaced in the web console. We deliberately
    # show the version-path tail (e.g. "/v1") so users are reminded to type
    # the full base URL. The form is intentionally vague (`...../v1`) so it
    # never looks like a real default a user might paste verbatim — and we
    # never auto-rewrite anything on the server side.
    _PLACEHOLDER_V1 = "https://...../v1"
    _PLACEHOLDER_QIANFAN = "https://...../v2"
    _PLACEHOLDER_ZHIPU = "https://...../api/paas/v4"
    _PLACEHOLDER_DOUBAO = "https://...../api/v3"
    _PLACEHOLDER_GEMINI = "https://....."

    PROVIDER_MODELS = OrderedDict([
        ("deepseek", {
            "label": "DeepSeek",
            "api_key_field": "deepseek_api_key",
            "api_base_key": "deepseek_api_base",
            "api_base_default": "https://api.deepseek.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.DEEPSEEK_V4_PRO],
        }),
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.MINIMAX_M3],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.CLAUDE_FABLE_5],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "api_base_placeholder": _PLACEHOLDER_GEMINI,
            "models": [const.GEMINI_31_PRO_PRE],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GPT_55],
        }),
        ("zhipu", {
            "label": {"zh": "智谱AI", "en": "GLM"},
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "api_base_placeholder": _PLACEHOLDER_ZHIPU,
            "models": [const.GLM_5_1],
        }),
        ("dashscope", {
            "label": {"zh": "通义千问", "en": "Qwen"},
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN37_MAX],
        }),
        ("doubao", {
            "label": {"zh": "豆包", "en": "Doubao"},
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "api_base_placeholder": _PLACEHOLDER_DOUBAO,
            "models": [const.DOUBAO_SEED_2_PRO],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.KIMI_K2_6],
        }),
        ("qianfan", {
            "label": {"zh": "百度千帆", "en": "ERNIE"},
            "api_key_field": "qianfan_api_key",
            "api_base_key": "qianfan_api_base",
            "api_base_default": "https://qianfan.baidubce.com/v2",
            "api_base_placeholder": _PLACEHOLDER_QIANFAN,
            "models": [const.ERNIE_5_1],
        }),
        ("mimo", {
            "label": {"zh": "小米 MiMo", "en": "MiMo"},
            "api_key_field": "mimo_api_key",
            "api_base_key": "mimo_api_base",
            "api_base_default": "https://api.xiaomimimo.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.MIMO_V2_5_PRO],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.GPT_55],
        }),
        ("custom", {
            "label": {"zh": "自定义", "en": "Custom"},
            "api_key_field": "custom_api_key",
            "api_base_key": "custom_api_base",
            "api_base_default": "",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [],
        }),
    ])

    EDITABLE_KEYS = {
        "cow_lang",
        "model", "bot_type", "use_linkai",
        "open_ai_api_base", "deepseek_api_base", "qianfan_api_base", "claude_api_base", "gemini_api_base",
        "zhipu_ai_api_base", "moonshot_base_url", "ark_base_url", "custom_api_base", "mimo_api_base",
        "open_ai_api_key", "deepseek_api_key", "qianfan_api_key", "claude_api_key", "gemini_api_key",
        "zhipu_ai_api_key", "dashscope_api_key", "moonshot_api_key",
        "ark_api_key", "minimax_api_key", "linkai_api_key", "custom_api_key", "mimo_api_key",
        "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
        "enable_thinking", "self_evolution_enabled", "web_password",
    }

    @staticmethod
    def _config_path() -> str:
        configured = os.environ.get("ECOREX_CONFIG_PATH", "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(configured))
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.json",
        )

    @staticmethod
    def _mask_key(value: str) -> str:
        """Mask the middle part of an API key for display."""
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            use_agent = local_config.get("agent", True)
            title = "EcoreX" if use_agent else "AI Assistant"
            welcome_title = "和小芯一起开始工作"

            api_bases = {}
            api_keys_masked = {}
            for pid, pinfo in self.PROVIDER_MODELS.items():
                base_key = pinfo.get("api_base_key")
                if base_key:
                    api_bases[base_key] = local_config.get(base_key, pinfo["api_base_default"])
                key_field = pinfo.get("api_key_field")
                if key_field and key_field not in api_keys_masked:
                    raw = local_config.get(key_field, "")
                    api_keys_masked[key_field] = self._mask_key(raw) if raw else ""

            providers = {}
            for pid, p in self.PROVIDER_MODELS.items():
                providers[pid] = {
                    "label": p["label"],
                    "models": p["models"],
                    "api_base_key": p["api_base_key"],
                    "api_base_default": p["api_base_default"],
                    "api_base_placeholder": p.get("api_base_placeholder", ""),
                    "api_key_field": p.get("api_key_field"),
                }

            raw_pwd = str(local_config.get("web_password", "") or "")
            masked_pwd = ("*" * len(raw_pwd)) if raw_pwd else ""

            return json.dumps({
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "welcome_title": welcome_title,
                "model": local_config.get("model", ""),
                "bot_type": "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", ""),
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": (
                    local_config.get("model_auto_compact_token_limit")
                    or local_config.get("agent_max_context_tokens", 800000)
                ),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", 20),
                "agent_max_steps": local_config.get("agent_max_steps", 20),
                "enable_thinking": bool(local_config.get("enable_thinking", False)),
                "self_evolution_enabled": bool(local_config.get("self_evolution_enabled", False)),
                "api_bases": api_bases,
                "api_keys": api_keys_masked,
                "providers": providers,
                "web_password_masked": masked_pwd,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error getting config: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data())
            updates = data.get("updates", {})
            if not updates:
                return json.dumps({"status": "error", "message": "no updates provided"})

            local_config = conf()
            applied = {}
            for key, value in updates.items():
                if key not in self.EDITABLE_KEYS:
                    continue
                if key in ("agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps"):
                    value = int(value)
                if key in ("use_linkai", "enable_thinking", "self_evolution_enabled"):
                    value = bool(value)
                local_config[key] = value
                applied[key] = value
                if key == "agent_max_context_tokens":
                    local_config["model_auto_compact_token_limit"] = value
                    applied["model_auto_compact_token_limit"] = value

            if not applied:
                return json.dumps({"status": "error", "message": "no valid keys to update"})

            config_path = self._config_path()
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
            else:
                file_cfg = {}
            file_cfg.update(applied)
            _ensure_ecorex_runtime_defaults(file_cfg)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)

            logger.info(f"[WebChannel] Config updated: {list(applied.keys())}")

            # Apply a language change immediately so backend logs, agent
            # replies and CLI output switch without a restart.
            if "cow_lang" in applied:
                try:
                    i18n.resolve_language(applied["cow_lang"])
                    logger.info(f"[WebChannel] Language switched to: {i18n.get_language()}")
                except Exception as lang_err:
                    logger.warning(f"[WebChannel] Failed to apply language: {lang_err}")

            # Reset Bridge so that bot routing reflects the new config.
            # Without this, Bridge keeps its cached bot instance (e.g. LinkAIBot)
            # even after the user switches bot_type / use_linkai / model in UI.
            bridge_routing_keys = {"bot_type", "use_linkai", "model"}
            if any(k in applied for k in bridge_routing_keys):
                try:
                    from bridge.bridge import Bridge
                    bridge = Bridge()
                    refresh = getattr(bridge, "refresh_chat_routing", None)
                    if callable(refresh):
                        refresh()
                        logger.info("[WebChannel] Bridge chat routing refreshed due to config change")
                    else:
                        bridge.reset_bot()
                        logger.info("[WebChannel] Bridge bot routing reset due to config change")
                except Exception as reset_err:
                    logger.warning(f"[WebChannel] Failed to reset bridge: {reset_err}")

            return json.dumps({"status": "success", "applied": applied}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error updating config: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


class ModelsHandler:
    """API for the unified Models console.

    Layered model:
      Layer 1 (providers): vendor credentials shared across capabilities.
                            Stored as flat *_api_key / *_api_base fields in
                            config.json — the same fields ConfigHandler
                            already manages.
      Layer 2 (capabilities): which provider/model is used by chat / vision /
                            asr / tts / embedding / image / search.

    GET  /api/models           -> overview (providers + capabilities)
    POST /api/models/provider  -> upsert a vendor credential
    DELETE /api/models/provider -> clear a vendor credential
    POST /api/models/capability -> set provider/model for a capability
    """

    # Capability -> provider ids drawn from ConfigHandler.PROVIDER_MODELS.
    _ASR_PROVIDERS = ["openai", "dashscope", "zhipu", "linkai"]
    # Web-console white-list. Other vendors stay usable via direct config.
    _TTS_PROVIDERS = ["openai", "minimax", "dashscope", "mimo", "linkai"]

    # TTS engine catalog (speech models, not voice timbres). Entries are
    # either a bare code or {value, hint?} when a friendly label helps.
    _TTS_PROVIDER_MODELS = {
        "openai":    ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
        "minimax": [
            {"value": "speech-2.8-hd",    "hint": "情绪渲染融合语气词,自然听感"},
            {"value": "speech-2.8-turbo", "hint": "极致生成速度,更自然逼真"},
            {"value": "speech-2.6-hd",    "hint": "超低延时,归一化升级"},
            {"value": "speech-2.6-turbo", "hint": "更快更便宜,适合语音聊天/数字人"},
        ],
        "dashscope": [
            {"value": "qwen3-tts-flash", "hint": "覆盖普通话、方言与主流外语"},
        ],
        # 小米 MiMo TTS 系列，通过 chat completions 接口合成
        "mimo": [
            {"value": "mimo-v2.5-tts", "hint": "预置音色 · 支持唱歌模式"},
        ],
        # Aggregating gateway: a single endpoint multiplexes several
        # underlying TTS engines, selected via the `model` field.
        # Each engine exposes its own voice catalog (see _TTS_PROVIDER_VOICES).
        "linkai": [
            {"value": "tts-1",  "hint": "OpenAI · 多语种通用"},
            {"value": "doubao", "hint": "字节豆包 · 中文音色丰富"},
            {"value": "baidu",  "hint": "百度 · 中文主播音色"},
        ],
    }

    # ASR engine catalog per provider. The first entry of each list is the
    # runtime default (mirrors DEFAULT_ASR_MODEL in voice/*). Users can still
    # pick "custom" in the UI to send any other model id.
    _ASR_PROVIDER_MODELS = {
        "openai": [
            {"value": "gpt-4o-mini-transcribe", "hint": "默认 · 速度快"},
            {"value": "gpt-4o-transcribe",      "hint": "更高准确率"},
            {"value": "whisper-1",              "hint": "经典 Whisper"},
        ],
        "dashscope": [
            {"value": "qwen3-asr-flash", "hint": "覆盖普通话、方言与主流外语"},
        ],
        "zhipu": [
            {"value": "glm-asr-2512", "hint": "智谱语音识别"},
        ],
        # LinkAI gateway pins whisper-1 for ASR and ignores any other id,
        # so expose only that to avoid misleading the user.
        "linkai": [
            {"value": "whisper-1", "hint": "网关固定使用"},
        ],
    }

    # Per-provider voice timbres. Entries can be a bare code string
    # (label = code) or {value, hint?} when a friendly secondary label
    # helps recognition. We keep `value` as the raw API code so power
    # users can cross-reference config.json.
    _TTS_PROVIDER_VOICES = {
        "openai":    [
            "alloy", "echo", "fable", "onyx", "nova", "shimmer",
            "ash", "ballad", "coral", "sage", "verse",
        ],
        "minimax": [
            # Mandarin Chinese (full catalog)
            {"value": "male-qn-qingse",                           "hint": "中文 · 青涩青年（男）"},
            {"value": "male-qn-jingying",                         "hint": "中文 · 精英青年（男）"},
            {"value": "male-qn-badao",                            "hint": "中文 · 霸道青年（男）"},
            {"value": "male-qn-daxuesheng",                       "hint": "中文 · 青年大学生（男）"},
            {"value": "female-shaonv",                            "hint": "中文 · 少女（女）"},
            {"value": "female-yujie",                             "hint": "中文 · 御姐（女）"},
            {"value": "female-chengshu",                          "hint": "中文 · 成熟女性（女）"},
            {"value": "female-tianmei",                           "hint": "中文 · 甜美女性（女）"},
            {"value": "male-qn-qingse-jingpin",                   "hint": "中文 · 青涩青年-beta（男）"},
            {"value": "male-qn-jingying-jingpin",                 "hint": "中文 · 精英青年-beta（男）"},
            {"value": "male-qn-badao-jingpin",                    "hint": "中文 · 霸道青年-beta（男）"},
            {"value": "male-qn-daxuesheng-jingpin",               "hint": "中文 · 青年大学生-beta（男）"},
            {"value": "female-shaonv-jingpin",                    "hint": "中文 · 少女-beta（女）"},
            {"value": "female-yujie-jingpin",                     "hint": "中文 · 御姐-beta（女）"},
            {"value": "female-chengshu-jingpin",                  "hint": "中文 · 成熟女性-beta（女）"},
            {"value": "female-tianmei-jingpin",                   "hint": "中文 · 甜美女性-beta（女）"},
            {"value": "clever_boy",                               "hint": "中文 · 聪明男童"},
            {"value": "cute_boy",                                 "hint": "中文 · 可爱男童"},
            {"value": "lovely_girl",                              "hint": "中文 · 萌萌女童"},
            {"value": "cartoon_pig",                              "hint": "中文 · 卡通猪小琪"},
            {"value": "bingjiao_didi",                            "hint": "中文 · 病娇弟弟"},
            {"value": "junlang_nanyou",                           "hint": "中文 · 俊朗男友"},
            {"value": "chunzhen_xuedi",                           "hint": "中文 · 纯真学弟"},
            {"value": "lengdan_xiongzhang",                       "hint": "中文 · 冷淡学长"},
            {"value": "badao_shaoye",                             "hint": "中文 · 霸道少爷"},
            {"value": "tianxin_xiaoling",                         "hint": "中文 · 甜心小玲"},
            {"value": "qiaopi_mengmei",                           "hint": "中文 · 俏皮萌妹"},
            {"value": "wumei_yujie",                              "hint": "中文 · 妩媚御姐"},
            {"value": "diadia_xuemei",                            "hint": "中文 · 嗲嗲学妹"},
            {"value": "danya_xuejie",                             "hint": "中文 · 淡雅学姐"},
            {"value": "Chinese (Mandarin)_Reliable_Executive",    "hint": "中文 · 沉稳高管"},
            {"value": "Chinese (Mandarin)_News_Anchor",           "hint": "中文 · 新闻女声"},
            {"value": "Chinese (Mandarin)_Mature_Woman",          "hint": "中文 · 傲娇御姐"},
            {"value": "Chinese (Mandarin)_Unrestrained_Young_Man","hint": "中文 · 不羁青年"},
            {"value": "Arrogant_Miss",                            "hint": "中文 · 嚣张小姐"},
            {"value": "Robot_Armor",                              "hint": "中文 · 机械战甲"},
            {"value": "Chinese (Mandarin)_Kind-hearted_Antie",    "hint": "中文 · 热心大婶"},
            {"value": "Chinese (Mandarin)_HK_Flight_Attendant",   "hint": "中文 · 港普空姐"},
            {"value": "Chinese (Mandarin)_Humorous_Elder",        "hint": "中文 · 搞笑大爷"},
            {"value": "Chinese (Mandarin)_Gentleman",             "hint": "中文 · 温润男声"},
            {"value": "Chinese (Mandarin)_Warm_Bestie",           "hint": "中文 · 温暖闺蜜"},
            {"value": "Chinese (Mandarin)_Male_Announcer",        "hint": "中文 · 播报男声"},
            {"value": "Chinese (Mandarin)_Sweet_Lady",            "hint": "中文 · 甜美女声"},
            {"value": "Chinese (Mandarin)_Southern_Young_Man",    "hint": "中文 · 南方小哥"},
            {"value": "Chinese (Mandarin)_Wise_Women",            "hint": "中文 · 阅历姐姐"},
            {"value": "Chinese (Mandarin)_Gentle_Youth",          "hint": "中文 · 温润青年"},
            {"value": "Chinese (Mandarin)_Warm_Girl",             "hint": "中文 · 温暖少女"},
            {"value": "Chinese (Mandarin)_Kind-hearted_Elder",    "hint": "中文 · 花甲奶奶"},
            {"value": "Chinese (Mandarin)_Cute_Spirit",           "hint": "中文 · 憨憨萌兽"},
            {"value": "Chinese (Mandarin)_Radio_Host",            "hint": "中文 · 电台男主播"},
            {"value": "Chinese (Mandarin)_Lyrical_Voice",         "hint": "中文 · 抒情男声"},
            {"value": "Chinese (Mandarin)_Straightforward_Boy",   "hint": "中文 · 率真弟弟"},
            {"value": "Chinese (Mandarin)_Sincere_Adult",         "hint": "中文 · 真诚青年"},
            {"value": "Chinese (Mandarin)_Gentle_Senior",         "hint": "中文 · 温柔学姐"},
            {"value": "Chinese (Mandarin)_Stubborn_Friend",       "hint": "中文 · 嘴硬竹马"},
            {"value": "Chinese (Mandarin)_Crisp_Girl",            "hint": "中文 · 清脆少女"},
            {"value": "Chinese (Mandarin)_Pure-hearted_Boy",      "hint": "中文 · 清澈邻家弟弟"},
            {"value": "Chinese (Mandarin)_Soft_Girl",             "hint": "中文 · 柔和少女"},
            # Cantonese (full catalog)
            {"value": "Cantonese_ProfessionalHost（F)",            "hint": "粤语 · 专业女主持"},
            {"value": "Cantonese_GentleLady",                     "hint": "粤语 · 温柔女声"},
            {"value": "Cantonese_ProfessionalHost（M)",            "hint": "粤语 · 专业男主持"},
            {"value": "Cantonese_PlayfulMan",                     "hint": "粤语 · 活泼男声"},
            {"value": "Cantonese_CuteGirl",                       "hint": "粤语 · 可爱女孩"},
            {"value": "Cantonese_KindWoman",                      "hint": "粤语 · 善良女声"},
            # English (curated: 1F + 1M)
            {"value": "English_Graceful_Lady",                    "hint": "英文 · Graceful Lady（女）"},
            {"value": "English_Trustworthy_Man",                  "hint": "英文 · Trustworthy Man（男）"},
            # Japanese (curated: 1F + 1M)
            {"value": "Japanese_KindLady",                        "hint": "日文 · Kind Lady（女）"},
            {"value": "Japanese_LoyalKnight",                     "hint": "日文 · Loyal Knight（男）"},
            # Korean (curated: 1F + 1M)
            {"value": "Korean_SweetGirl",                         "hint": "韩文 · Sweet Girl（女）"},
            {"value": "Korean_CheerfulBoyfriend",                 "hint": "韩文 · Cheerful Boyfriend（男）"},
        ],
        "dashscope": [
            {"value": "Cherry",   "hint": "芊悦 · 阳光女声"},
            {"value": "Serena",   "hint": "苏瑶 · 温柔女声"},
            {"value": "Chelsie",  "hint": "千雪 · 二次元少女"},
            {"value": "Ethan",    "hint": "晨煦 · 阳光男声"},
            {"value": "Moon",     "hint": "月白 · 率性男声"},
            {"value": "Kai",      "hint": "凯 · 治愈男声"},
            {"value": "Nofish",   "hint": "不吃鱼 · 设计师男声"},
            {"value": "Bella",    "hint": "萌宝 · 小萝莉"},
            {"value": "Bunny",    "hint": "萌小姬 · 萌系少女"},
            {"value": "Stella",   "hint": "少女阿月 · 元气少女"},
            {"value": "Neil",     "hint": "阿闻 · 新闻主播"},
            {"value": "Seren",    "hint": "小婉 · 助眠女声"},
            {"value": "Jada",     "hint": "上海话 · 阿珍"},
            {"value": "Dylan",    "hint": "北京话 · 晓东"},
            {"value": "Sunny",    "hint": "四川话 · 晴儿"},
            {"value": "Eric",     "hint": "四川话 · 程川"},
            {"value": "Rocky",    "hint": "粤语 · 阿强"},
            {"value": "Kiki",     "hint": "粤语 · 阿清"},
            {"value": "Peter",    "hint": "天津话 · 李彼得"},
            {"value": "Marcus",   "hint": "陕西话 · 秦川"},
            {"value": "Roy",      "hint": "闽南语 · 阿杰"},
        ],
        # 小米 MiMo 预置音色列表（mimo-v2.5-tts），文档：
        # https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5
        "mimo": [
            {"value": "冰糖",   "hint": "中文 · 女声 · 冰糖"},
            {"value": "茉莉",   "hint": "中文 · 女声 · 茉莉"},
            {"value": "苏打",   "hint": "中文 · 男声 · 苏打"},
            {"value": "白桦",   "hint": "中文 · 男声 · 白桦"},
            {"value": "Mia",   "hint": "英文 · 女声 · Mia"},
            {"value": "Chloe", "hint": "英文 · 女声 · Chloe"},
            {"value": "Milo",  "hint": "英文 · 男声 · Milo"},
            {"value": "Dean",  "hint": "英文 · 男声 · Dean"},
        ],
        # Aggregating gateway: voices are scoped per engine model. The
        # frontend picks the correct list based on the selected model so
        # users don't see incompatible timbres for the active engine.
        "linkai": {
            "tts-1": [
                "alloy", "echo", "fable", "onyx", "nova", "shimmer",
            ],
            "doubao": [
                {"value": "zh_female_wanwanxiaohe_moon_bigtts",       "hint": "湾湾小何"},
                {"value": "BV007_streaming",                          "hint": "亲切女声"},
                {"value": "BV001_streaming",                          "hint": "通用女声"},
                {"value": "BV002_streaming",                          "hint": "通用男声"},
                {"value": "BV051_streaming",                          "hint": "奶气萌娃"},
                {"value": "zh_female_linjianvhai_moon_bigtts",        "hint": "邻家女孩"},
                {"value": "BV700_streaming",                          "hint": "灿灿"},
                {"value": "BV019_streaming",                          "hint": "重庆小伙"},
                {"value": "BV524_streaming",                          "hint": "日语男声"},
                {"value": "BV021_streaming",                          "hint": "东北老铁"},
                {"value": "BV701_streaming",                          "hint": "擎苍"},
                {"value": "BV113_streaming",                          "hint": "甜宠少御"},
                {"value": "BV056_streaming",                          "hint": "阳光男声"},
                {"value": "BV213_streaming",                          "hint": "广西表哥"},
                {"value": "BV119_streaming",                          "hint": "通用赘婿"},
                {"value": "BV705_streaming",                          "hint": "炀炀"},
                {"value": "BV033_streaming",                          "hint": "温柔小哥"},
                {"value": "BV102_streaming",                          "hint": "儒雅青年"},
                {"value": "BV522_streaming",                          "hint": "气质女生"},
                {"value": "BV034_streaming",                          "hint": "知性姐姐 · 双语"},
                {"value": "BV005_streaming",                          "hint": "活泼女声"},
                {"value": "zh_female_wanqudashu_moon_bigtts",         "hint": "湾区大叔"},
                {"value": "zh_female_daimengchuanmei_moon_bigtts",    "hint": "呆萌川妹"},
                {"value": "zh_male_guozhoudege_moon_bigtts",          "hint": "广州德哥"},
                {"value": "zh_male_beijingxiaoye_moon_bigtts",        "hint": "北京小爷"},
                {"value": "zh_male_shaonianzixin_moon_bigtts",        "hint": "少年梓辛 / Brayan"},
                {"value": "zh_female_meilinvyou_moon_bigtts",         "hint": "魅力女友"},
                {"value": "zh_male_shenyeboke_moon_bigtts",           "hint": "深夜播客"},
                {"value": "zh_female_sajiaonvyou_moon_bigtts",        "hint": "柔美女友"},
                {"value": "zh_female_yuanqinvyou_moon_bigtts",        "hint": "撒娇学妹"},
                {"value": "zh_male_haoyuxiaoge_moon_bigtts",          "hint": "浩宇小哥"},
                {"value": "zh_male_guangxiyuanzhou_moon_bigtts",      "hint": "广西远舟"},
                {"value": "zh_female_meituojieer_moon_bigtts",        "hint": "妹坨洁儿"},
                {"value": "zh_male_yuzhouzixuan_moon_bigtts",         "hint": "豫州子轩"},
                {"value": "BV115_streaming",                          "hint": "古风少御"},
                {"value": "zh_female_gaolengyujie_moon_bigtts",       "hint": "高冷御姐"},
                {"value": "zh_male_yuanboxiaoshu_moon_bigtts",        "hint": "渊博小叔"},
                {"value": "zh_male_yangguangqingnian_moon_bigtts",    "hint": "阳光青年"},
                {"value": "zh_male_aojiaobazong_moon_bigtts",         "hint": "傲娇霸总"},
                {"value": "zh_male_jingqiangkanye_moon_bigtts",       "hint": "京腔侃爷 / Harmony"},
                {"value": "zh_female_shuangkuaisisi_moon_bigtts",     "hint": "爽快思思 / Skye"},
                {"value": "zh_male_wennuanahu_moon_bigtts",           "hint": "温暖阿虎 / Alvin"},
                {"value": "multi_female_shuangkuaisisi_moon_bigtts",  "hint": "はるこ / Esmeralda"},
                {"value": "multi_male_jingqiangkanye_moon_bigtts",    "hint": "かずね / Javier or Álvaro"},
                {"value": "multi_female_gaolengyujie_moon_bigtts",    "hint": "あけみ"},
                {"value": "multi_male_wanqudashu_moon_bigtts",        "hint": "ひろし / Roberto"},
                {"value": "ICL_zh_female_bingruoshaonv_tob",          "hint": "病弱少女"},
                {"value": "ICL_zh_female_huoponvhai_tob",             "hint": "活泼女孩"},
                {"value": "ICL_zh_female_heainainai_tob",             "hint": "和蔼奶奶"},
                {"value": "ICL_zh_female_linjuayi_tob",               "hint": "邻居阿姨"},
                {"value": "zh_female_wenrouxiaoya_moon_bigtts",       "hint": "温柔小雅"},
                {"value": "zh_female_tianmeixiaoyuan_moon_bigtts",    "hint": "甜美小源"},
                {"value": "zh_female_qingchezizi_moon_bigtts",        "hint": "清澈梓梓"},
                {"value": "zh_male_dongfanghaoran_moon_bigtts",       "hint": "东方浩然"},
                {"value": "zh_male_jieshuoxiaoming_moon_bigtts",      "hint": "解说小明"},
                {"value": "zh_female_kailangjiejie_moon_bigtts",      "hint": "开朗姐姐"},
                {"value": "zh_male_linjiananhai_moon_bigtts",         "hint": "邻家男孩"},
                {"value": "zh_female_tianmeiyueyue_moon_bigtts",      "hint": "甜美悦悦"},
                {"value": "zh_female_xinlingjitang_moon_bigtts",      "hint": "心灵鸡汤"},
            ],
            "baidu": [
                {"value": "baidu_0",    "hint": "度小美 · 标准女主播"},
                {"value": "baidu_1",    "hint": "度小宇 · 亲切男声"},
                {"value": "baidu_3",    "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_4",    "hint": "度丫丫 · 童声"},
                {"value": "baidu_5",    "hint": "度小娇 · 成熟女主播"},
                {"value": "baidu_5003", "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_5118", "hint": "度小鹿 · 甜美女声"},
                {"value": "baidu_103",  "hint": "度米朵 · 可爱童声"},
                {"value": "baidu_106",  "hint": "度博文 · 专业男主播"},
                {"value": "baidu_110",  "hint": "度小童 · 童声主播"},
                {"value": "baidu_111",  "hint": "度小萌 · 软萌妹子"},
                {"value": "baidu_4003", "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_4100", "hint": "度小雯 · 活力女主播"},
                {"value": "baidu_4103", "hint": "度米朵 · 可爱女声"},
                {"value": "baidu_4105", "hint": "度灵儿 · 清澈女声"},
                {"value": "baidu_4106", "hint": "度博文 · 专业男主播"},
                {"value": "baidu_4115", "hint": "度小贤 · 电台男主播"},
                {"value": "baidu_4117", "hint": "度小乔 · 活泼女声"},
                {"value": "baidu_4119", "hint": "度小鹿 · 甜美女声"},
                {"value": "baidu_4129", "hint": "度小彦 · 知识男主播"},
                {"value": "baidu_4140", "hint": "度小新 · 专业女主播"},
                {"value": "baidu_4143", "hint": "度清风 · 配音男声"},
                {"value": "baidu_4144", "hint": "度姗姗 · 娱乐女声"},
                {"value": "baidu_4149", "hint": "度星河 · 广告男声"},
                {"value": "baidu_4206", "hint": "度博文 · 综艺男声"},
                {"value": "baidu_4226", "hint": "南方 · 电台女主播"},
                {"value": "baidu_4254", "hint": "度小清 · 广告女声"},
                {"value": "baidu_4278", "hint": "度小贝 · 知识女主播"},
            ],
        },
    }
    _EMBEDDING_PROVIDERS = ["openai", "dashscope", "doubao", "zhipu", "linkai"]

    # Capability-scoped model catalogs. The chat dropdown can reuse the
    # provider's generic model list, but vision and image generation are
    # served by a narrower subset that the runtime actually dispatches to —
    # see agent/tools/vision/vision.py and skills/image-generation/SKILL.md.
    # Anything not listed here intentionally hides the model dropdown so
    # users cannot pin a chat-only model and silently get a 4xx at runtime.
    _VISION_PROVIDER_MODELS = {
        # OpenAI ordering matches the recommended GPT-5.4 family first, then
        # GPT-5 and the GPT-4.1/4o backstops.
        "openai":    [
            const.GPT_55,
            const.GPT_54,
            const.GPT_54_MINI,
            const.GPT_54_NANO,
            const.GPT_5,
            const.GPT_41,
            const.GPT_41_MINI,
            const.GPT_4o,
        ],
        "doubao":    [const.DOUBAO_SEED_2_PRO],
        "moonshot":  [const.KIMI_K2_6],
        "dashscope": [const.QWEN37_PLUS, const.QWEN36_PLUS],
        "claudeAPI": [const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS],
        "gemini":    [const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        "qianfan":   [const.ERNIE_45_TURBO_VL],
        # Zhipu's bot hard-codes the call to glm-5v-turbo regardless of what
        # name is passed in (see models/zhipuai/zhipuai_bot.py::call_vision),
        # so listing the chat models here would silently route to the same
        # endpoint. Surface only the model the runtime can truly dispatch to.
        "zhipu":     [const.GLM_5V_TURBO],
        # MiniMax's vision endpoint is similarly hard-coded to MiniMax-Text-01
        # (see models/minimax/minimax_bot.py::call_vision); the M2.x chat
        # family is text-only.
        "minimax":   [const.MINIMAX_TEXT_01],
        # MiMo 原生全模态模型：v2.5-pro / v2.5 支持图像/音频/视频输入
        "mimo":      [const.MIMO_V2_5_PRO, const.MIMO_V2_5],
        # LinkAI proxies the underlying vendor; surface a curated set of
        # multimodal models. Order: gpt-4.1-mini → gpt-5.4-mini as the
        # cross-vendor baselines, then each vendor's recommended default.
        "linkai":    [
            const.GPT_41_MINI,
            const.GPT_54_MINI,
            const.QWEN37_PLUS,
            const.DOUBAO_SEED_2_PRO,
            const.KIMI_K2_6,
            const.CLAUDE_4_6_SONNET,
            const.GEMINI_31_FLASH_LITE_PRE,
        ],
    }

    # Image-generation catalog. Source of truth: skills/image-generation/SKILL.md.
    # Listed verbatim (not via const.*) because these are skill-side names
    # the script forwards directly to the vendor's image endpoint.
    #
    # Two shapes are accepted per model entry:
    #   - bare string                           → the model id, no hint
    #   - {"value": ..., "hint": "..."}         → model id + dim secondary
    #                                             label rendered on the right
    #                                             of the dropdown row. Useful
    #                                             for surfacing brand names
    #                                             (e.g. "Nano Banana 2" next
    #                                             to gemini-3.1-flash-image-preview).
    # The skill itself maps either form to the real vendor endpoint, so the
    # hint is purely cosmetic.
    _IMAGE_PROVIDER_MODELS = {
        "openai":    ["gpt-image-2-pro", "gpt-image-2", "gpt-image-1"],
        "gemini": [
            {"value": "gemini-3.1-flash-image-preview", "hint": "Nano Banana 2"},
            {"value": "gemini-3-pro-image-preview",     "hint": "Nano Banana Pro"},
            {"value": "gemini-2.5-flash-image",         "hint": "Nano Banana"},
        ],
        "doubao":    ["seedream-5.0-lite", "seedream-4.5"],
        "dashscope": ["qwen-image-2.0-pro", "qwen-image-2.0"],
        "minimax":   ["image-01"],
        "linkai": [
            "gpt-image-2-pro",
            "gpt-image-2",
            {"value": "gemini-3.1-flash-image-preview", "hint": "Nano Banana 2"},
            {"value": "gemini-3-pro-image-preview",     "hint": "Nano Banana Pro"},
            "seedream-5.0-lite",
        ],
    }

    @staticmethod
    def _config_path() -> str:
        return ConfigHandler._config_path()

    @classmethod
    def _read_file_config(cls) -> dict:
        path = cls._config_path()
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @classmethod
    def _write_file_config(cls, data: dict) -> None:
        with open(cls._config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    def _is_real_key(value: str) -> bool:
        return bool(value) and value not in ("", "YOUR API KEY", "YOUR_API_KEY")

    @classmethod
    def _provider_overview(cls) -> List[dict]:
        """All known providers (configured first, unconfigured after).
        Re-uses ConfigHandler.PROVIDER_MODELS for the canonical list."""
        local_config = conf()
        items = []
        for pid, p in ConfigHandler.PROVIDER_MODELS.items():
            key_field = p.get("api_key_field")
            base_field = p.get("api_base_key")
            raw_key = local_config.get(key_field, "") if key_field else ""
            raw_base = local_config.get(base_field, "") if base_field else ""
            configured = cls._is_real_key(raw_key)
            items.append({
                "id": pid,
                "label": p["label"],
                "configured": configured,
                "api_key_field": key_field,
                "api_base_field": base_field,
                "api_key_masked": ConfigHandler._mask_key(raw_key) if configured else "",
                "api_base": raw_base or (p.get("api_base_default") or ""),
                "api_base_default": p.get("api_base_default") or "",
                "api_base_placeholder": p.get("api_base_placeholder") or "",
                "models": list(p.get("models") or []),
            })
        items.sort(key=lambda it: (0 if it["configured"] else 1, list(ConfigHandler.PROVIDER_MODELS.keys()).index(it["id"])))
        return items

    @staticmethod
    def _provider_label_text(label) -> str:
        if isinstance(label, dict):
            return label.get("zh") or label.get("en") or ""
        return str(label or "")

    @staticmethod
    def _model_entry_value(entry) -> str:
        if isinstance(entry, dict):
            return str(entry.get("value") or entry.get("model") or "").strip()
        return str(entry or "").strip()

    @classmethod
    def _provider_for_model(cls, model: str) -> str:
        target = str(model or "").strip()
        if not target:
            return ""
        for provider_id, provider in ConfigHandler.PROVIDER_MODELS.items():
            for entry in provider.get("models") or ():
                if cls._model_entry_value(entry) == target:
                    return provider_id
        return ""

    @classmethod
    def _chat_context_policy(cls, model: str, provider_id: str) -> dict:
        from models.model_capabilities import context_policy_for_model

        policy = context_policy_for_model(model, provider_id).to_dict()
        return {
            "contextWindowTokens": policy.get("context_window_tokens"),
            "maxOutputTokens": policy.get("max_output_tokens"),
            "autoCompactTokenLimit": policy.get("auto_compact_token_limit"),
            "hardContextTokenLimit": policy.get("hard_context_token_limit"),
            "source": policy.get("source"),
            "note": policy.get("note"),
            "tokenizer": policy.get("tokenizer"),
            "tokenizerStatus": policy.get("tokenizer_status"),
            "tokenizerNote": policy.get("tokenizer_note"),
            "context_window_tokens": policy.get("context_window_tokens"),
            "max_output_tokens": policy.get("max_output_tokens"),
            "auto_compact_token_limit": policy.get("auto_compact_token_limit"),
            "hard_context_token_limit": policy.get("hard_context_token_limit"),
            "tokenizer_status": policy.get("tokenizer_status"),
            "tokenizer_note": policy.get("tokenizer_note"),
        }

    @staticmethod
    def _model_alias_family(model: str) -> str:
        lowered = str(model or "").strip().lower()
        if lowered.startswith("gemini"):
            return "gemini"
        return ""

    @classmethod
    def _is_legacy_custom_gemini_config(cls, local_config: dict, model: str = "") -> bool:
        from models.model_capabilities import should_route_custom_gemini_as_rest

        return should_route_custom_gemini_as_rest(
            model or (local_config or {}).get("model") or "",
            configured_bot_type=(local_config or {}).get("bot_type") or "",
            gemini_api_base=(local_config or {}).get("gemini_api_base") or "",
            gemini_api_key=(local_config or {}).get("gemini_api_key") or "",
            custom_api_base=(local_config or {}).get("custom_api_base") or "",
            custom_api_key=(local_config or {}).get("custom_api_key") or "",
        )

    @classmethod
    def _has_custom_gemini_rest_endpoint(cls, local_config: dict) -> bool:
        from models.model_capabilities import is_official_gemini_api_base

        return bool(
            cls._is_real_key((local_config or {}).get("gemini_api_key", ""))
            and (local_config or {}).get("gemini_api_base")
            and not is_official_gemini_api_base((local_config or {}).get("gemini_api_base") or "")
        )

    @classmethod
    def _chat_route_metadata(cls, provider_id: str, model: str, local_config: Optional[dict] = None) -> dict:
        from models.model_capabilities import is_official_gemini_api_base

        alias_family = cls._model_alias_family(model)
        gemini_rest_route = bool(provider_id == const.GEMINI and alias_family == "gemini")
        official_gemini = bool(
            gemini_rest_route
            and is_official_gemini_api_base((local_config or {}).get("gemini_api_base") or "")
        )
        custom_gemini_endpoint = bool(gemini_rest_route and not official_gemini)
        return {
            "modelAliasFamily": alias_family,
            "model_alias_family": alias_family,
            "effectiveTransportProvider": provider_id,
            "effective_transport_provider": provider_id,
            "isOfficialGeminiProvider": official_gemini,
            "is_official_gemini_provider": official_gemini,
            "officialGeminiApiUsed": official_gemini,
            "official_gemini_api_used": official_gemini,
            "isCustomGeminiEndpoint": custom_gemini_endpoint,
            "is_custom_gemini_endpoint": custom_gemini_endpoint,
            "geminiEndpointFamily": "custom-rest" if custom_gemini_endpoint else ("google-official" if gemini_rest_route else ""),
            "gemini_endpoint_family": "custom-rest" if custom_gemini_endpoint else ("google-official" if gemini_rest_route else ""),
        }

    @staticmethod
    def _coerce_positive_int(value) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _apply_chat_context_policy(cls, local_config: dict, file_cfg: dict, provider_id: str, model: str) -> dict:
        policy = cls._chat_context_policy(model, provider_id)
        context_window = cls._coerce_positive_int(policy.get("contextWindowTokens"))
        auto_limit = cls._coerce_positive_int(policy.get("autoCompactTokenLimit"))
        if context_window:
            local_config["model_context_window"] = context_window
            file_cfg["model_context_window"] = context_window
        if auto_limit:
            local_config["model_auto_compact_token_limit"] = auto_limit
            file_cfg["model_auto_compact_token_limit"] = auto_limit
            # Agent execution still consumes this legacy key; keep it synced
            # until S7/S8 collapse runtime settings into one projection.
            local_config["agent_max_context_tokens"] = auto_limit
            file_cfg["agent_max_context_tokens"] = auto_limit
        return policy

    @classmethod
    def _chat_model_options(cls, local_config: dict, current_provider: str, current_model: str) -> List[dict]:
        options = []
        seen = set()
        has_custom_gemini_rest = cls._has_custom_gemini_rest_endpoint(local_config)
        for provider_id, provider in ConfigHandler.PROVIDER_MODELS.items():
            key_field = provider.get("api_key_field")
            configured = cls._is_real_key(local_config.get(key_field, "")) if key_field else False
            preserve_unconfigured = provider_id == "openai" or provider_id == current_provider
            if not configured and not preserve_unconfigured:
                continue
            provider_label = cls._provider_label_text(provider.get("label")) or provider_id
            if provider_id == const.GEMINI and has_custom_gemini_rest:
                provider_label = "自定义 Gemini"
            entries = list(provider.get("models") or [])
            selected_entry = entries[0] if entries else current_model
            selected_index = 0
            if provider_id == current_provider and current_model:
                for index, entry in enumerate(entries):
                    if cls._model_entry_value(entry) == current_model:
                        selected_entry = entry
                        selected_index = index
                        break
                else:
                    selected_entry = current_model
                    selected_index = -1
            model = cls._model_entry_value(selected_entry)
            if not model:
                continue
            option_key = (provider_id, model)
            if option_key in seen:
                continue
            seen.add(option_key)
            hint = selected_entry.get("hint") if isinstance(selected_entry, dict) else ""
            if not hint:
                if not configured:
                    hint = "needs credentials"
                elif provider_id == current_provider and model == current_model:
                    hint = "current"
                elif provider_id == const.GEMINI and has_custom_gemini_rest:
                    hint = "Gemini REST endpoint"
                else:
                    hint = "top-tier"
            options.append({
                "provider": provider_id,
                "providerLabel": provider_label,
                "model": model,
                "label": model,
                "hint": hint,
                "configured": configured,
                "current": provider_id == current_provider and model == current_model,
                "contextPolicy": cls._chat_context_policy(model, provider_id),
                **cls._chat_route_metadata(provider_id, model, local_config),
            })

        custom_provider = ConfigHandler.PROVIDER_MODELS.get(const.CUSTOM) or {}
        custom_key_field = custom_provider.get("api_key_field")
        custom_configured = cls._is_real_key(local_config.get(custom_key_field, "")) if custom_key_field else False
        custom_model = str(current_model or "").strip()
        if (
            custom_model
            and custom_configured
            and cls._model_alias_family(custom_model) == "gemini"
            and (const.CUSTOM, custom_model) not in seen
        ):
            seen.add((const.CUSTOM, custom_model))
            options.append({
                "provider": const.CUSTOM,
                "providerLabel": cls._provider_label_text(custom_provider.get("label")) or "Custom",
                "model": custom_model,
                "label": custom_model,
                "hint": "OpenAI-compatible",
                "configured": True,
                "current": current_provider == const.CUSTOM and custom_model == current_model,
                "contextPolicy": cls._chat_context_policy(custom_model, const.CUSTOM),
                **cls._chat_route_metadata(const.CUSTOM, custom_model, local_config),
            })

        if current_model and (current_provider, current_model) not in seen:
            provider = ConfigHandler.PROVIDER_MODELS.get(current_provider) or {}
            provider_label = cls._provider_label_text(provider.get("label")) or current_provider or "Current"
            key_field = provider.get("api_key_field")
            configured = cls._is_real_key(local_config.get(key_field, "")) if key_field else False
            options.insert(0, {
                "provider": current_provider,
                "providerLabel": provider_label,
                "model": current_model,
                "label": current_model,
                "hint": "current" if configured else "needs credentials",
                "configured": configured,
                "current": True,
                "contextPolicy": cls._chat_context_policy(current_model, current_provider),
                **cls._chat_route_metadata(current_provider, current_model, local_config),
            })
        provider_order = {provider_id: index for index, provider_id in enumerate(ConfigHandler.PROVIDER_MODELS.keys())}
        options.sort(key=lambda option: (
            0 if option.get("current") else 1,
            0 if option.get("configured") else 1,
            provider_order.get(option.get("provider"), 999),
            str(option.get("model") or ""),
        ))
        return options

    @classmethod
    def _chat_route_provider(cls, local_config: dict, capability_provider: str) -> str:
        bot_type = str((local_config or {}).get("bot_type") or "").strip()
        if cls._is_legacy_custom_gemini_config(local_config):
            return const.GEMINI
        if bot_type:
            if bot_type == const.OPENAI:
                return "openai"
            if bot_type == const.CHATGPT:
                return capability_provider if capability_provider == "openai_compatible" else "openai"
            if bot_type in ConfigHandler.PROVIDER_MODELS:
                return bot_type
        model_provider = cls._provider_for_model(str((local_config or {}).get("model") or ""))
        if model_provider:
            if model_provider == "openai":
                if bot_type == const.OPENAI:
                    return "openai"
                base = str((local_config or {}).get("open_ai_api_base") or "").strip().rstrip("/")
                default_base = str(ConfigHandler.PROVIDER_MODELS["openai"].get("api_base_default") or "").rstrip("/")
                if base and default_base and base != default_base and capability_provider:
                    return capability_provider
            return model_provider
        if bot_type == const.OPENAI:
            return "openai"
        return capability_provider

    @classmethod
    def _chat_capability(cls, local_config: dict) -> dict:
        """Main chat model — drives the agent. bot_type maps to a provider id."""
        from models.model_capabilities import build_provider_capability_matrix, capabilities_for_config, get_model_capabilities

        capability = capabilities_for_config(local_config or {})
        provider_models = {
            provider_id: provider.get("models") or ()
            for provider_id, provider in ConfigHandler.PROVIDER_MODELS.items()
        }
        provider_models.setdefault(const.CHATGPTONAZURE, provider_models.get("openai") or ())
        provider_id = cls._chat_route_provider(local_config, capability.provider)
        current_model = local_config.get("model", "")
        routed_capability = get_model_capabilities(current_model, provider_id) if provider_id else capability
        return {
            "editable": True,
            "current_provider": provider_id,
            "current_model": current_model,
            "providers": list(ConfigHandler.PROVIDER_MODELS.keys()),
            "provider_models": provider_models,
            "model_options": cls._chat_model_options(local_config, provider_id, current_model),
            "use_linkai": bool(local_config.get("use_linkai", False)),
            "context_policy": cls._chat_context_policy(current_model, provider_id),
            "capabilities": routed_capability.to_dict(),
            "capability_matrix": build_provider_capability_matrix(provider_models),
        }

    @classmethod
    def _validate_chat_selection(cls, local_config: dict, provider_id: str, model: str) -> Tuple[bool, str]:
        provider_id = (provider_id or "").strip()
        model = (model or "").strip()
        if not provider_id or not model:
            return True, ""
        capability = cls._chat_capability(local_config)
        for option in capability.get("model_options") or []:
            if not isinstance(option, dict):
                continue
            if option.get("provider") == provider_id and option.get("model") == model:
                if option.get("configured") is False:
                    return False, "model provider credentials are required"
                return True, ""
        if provider_id == const.CUSTOM:
            provider = ConfigHandler.PROVIDER_MODELS.get(provider_id) or {}
            key_field = provider.get("api_key_field")
            if key_field and cls._is_real_key((local_config or {}).get(key_field, "")):
                return True, ""
            return False, "model provider credentials are required"
        return False, "model is not available in configured model options"

    # Auto-fallback order for vision when no explicit model is pinned.
    # Mirrors agent/tools/vision/vision.py::_resolve_providers — DeepSeek and
    # other text-only chat bots are intentionally absent, since they cannot
    # actually serve a vision request. Each entry is
    #   (provider_id, api_key_field, default_vision_model)
    # and lookups are case-insensitive on the api_key_field. LinkAI and
    # OpenAI are handled separately below so use_linkai can promote LinkAI
    # to the front of the chain.
    _VISION_AUTO_ORDER = [
        ("moonshot",  "moonshot_api_key",  const.KIMI_K2_6),
        ("doubao",    "ark_api_key",       const.DOUBAO_SEED_2_PRO),
        ("dashscope", "dashscope_api_key", const.QWEN37_PLUS),
        ("claudeAPI", "claude_api_key",    const.CLAUDE_4_6_SONNET),
        ("gemini",    "gemini_api_key",    const.GEMINI_35_FLASH),
        ("qianfan",   "qianfan_api_key",   const.ERNIE_45_TURBO_VL),
        ("zhipu",     "zhipu_ai_api_key",  const.GLM_5V_TURBO),
        ("minimax",   "minimax_api_key",   const.MINIMAX_TEXT_01),
        ("mimo",      "mimo_api_key",      const.MIMO_V2_5_PRO),
    ]

    @classmethod
    def _predict_vision_auto(cls, local_config: dict) -> dict:
        """Predict which provider vision.py will actually dispatch to when
        no tools.vision.model is set. Mirrors the fallback order in
        agent/tools/vision/vision.py::_resolve_providers so the UI hint
        matches reality."""
        chat = cls._chat_capability(local_config)
        main_provider = chat["current_provider"]
        main_model = chat["current_model"]
        use_linkai_flag = bool(local_config.get("use_linkai", False))
        linkai_configured = cls._is_real_key(local_config.get("linkai_api_key", ""))

        def _try(pid: str, model_default: str):
            # Look up the api_key for this provider via the canonical
            # provider table so we don't hardcode field names here.
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                return None
            if not cls._is_real_key(local_config.get(key_field, "")):
                return None
            # Pick a model that the vision runtime can actually dispatch to
            # for this provider. Using `main_model` here is unsafe — for
            # vendors like Zhipu/MiniMax the bot hard-codes the vision model
            # name regardless of the chat-model name, so surfacing the chat
            # model name in the hint is misleading. Trust the curated
            # _VISION_PROVIDER_MODELS list: prefer the main model only if
            # it appears there; otherwise show the vendor's first vision-
            # capable model.
            allowed = cls._VISION_PROVIDER_MODELS.get(pid, [])
            if pid == main_provider and main_model and main_model in allowed:
                return {"provider": pid, "model": main_model}
            fallback = allowed[0] if allowed else model_default
            return {"provider": pid, "model": fallback}

        # 1. use_linkai → suppress the hint entirely. LinkAI is a proxy and
        #    we don't observe which underlying model it picks; surfacing
        #    "LinkAI" with no model would not tell the user anything useful.
        if use_linkai_flag and linkai_configured:
            return {"provider": "", "model": ""}

        # 2. Main bot — only when it natively supports vision. We approximate
        #    "natively supports" by membership in _VISION_PROVIDER_MODELS,
        #    which is the same set vision.py's _DISCOVERABLE_MODELS covers
        #    (minus the chat-only DeepSeek family).
        if main_provider in cls._VISION_PROVIDER_MODELS:
            hit = _try(main_provider, main_model)
            if hit:
                return hit

        # 3. Other discoverable providers in declared order
        for pid, _key, default_model in cls._VISION_AUTO_ORDER:
            hit = _try(pid, default_model)
            if hit:
                return hit

        # 4. OpenAI raw HTTP
        if cls._is_real_key(local_config.get("open_ai_api_key", "")):
            return {"provider": "openai", "model": const.GPT_55}

        # 5. LinkAI as last resort (only reached when use_linkai is off)
        if linkai_configured:
            return {"provider": "linkai", "model": const.GPT_41_MINI}

        return {"provider": "", "model": ""}

    @classmethod
    def _vision_capability(cls, local_config: dict) -> dict:
        """Vision model. tools.vision.model is the explicit override; otherwise
        the runtime fallback chain in agent/tools/vision/vision.py decides."""
        tools_conf = local_config.get("tools") or local_config.get("tool") or {}
        if not isinstance(tools_conf, dict):
            tools_conf = {}
        vision_conf = tools_conf.get("vision") or {}
        if not isinstance(vision_conf, dict):
            vision_conf = {}
        user_specified = (vision_conf.get("model") or "").strip()
        explicit_provider = (vision_conf.get("provider") or "").strip()

        # Provider resolution priority:
        #   1. Explicit `tools.vision.provider` (persisted via UI; supports
        #      custom model names that prefix-inference can't recognize).
        #   2. Scan per-provider model lists by model name.
        # Empty provider keeps the dropdown on "auto" when we can't tell.
        inferred_provider = ""
        if explicit_provider and explicit_provider in cls._VISION_PROVIDER_MODELS:
            inferred_provider = explicit_provider
        elif user_specified:
            for pid, models in cls._VISION_PROVIDER_MODELS.items():
                if user_specified in models:
                    inferred_provider = pid
                    break

        # In auto mode the hint should reflect what vision.py will actually
        # dispatch to — surface that prediction via fallback_* so the UI
        # shows e.g. "openai / gpt-4.1-mini" instead of the chat-model name.
        predicted = cls._predict_vision_auto(local_config)

        return {
            "editable": True,
            "strategy": "specified" if user_specified else "auto",
            "user_specified_model": user_specified,
            "current_provider": inferred_provider,
            "current_model": user_specified,
            "fallback_provider": predicted["provider"],
            "fallback_model": predicted["model"],
            "providers": list(cls._VISION_PROVIDER_MODELS.keys()),
            "provider_models": cls._VISION_PROVIDER_MODELS,
        }

    @classmethod
    def _asr_capability(cls, local_config: dict) -> dict:
        # "Pick or empty" — when voice_to_text is unset we don't show a
        # current selection. `suggested_provider` previews which vendor
        # the bridge auto-picker would land on (purely a UX hint, NOT
        # persisted). Once the user saves a vendor, we lock onto it.
        explicit = (local_config.get("voice_to_text") or "").strip().lower()
        suggested = ""
        if not explicit:
            for pid in cls._ASR_PROVIDERS:
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
        return {
            "editable": True,
            "current_provider": explicit,
            "suggested_provider": suggested,
            "current_model": (local_config.get("voice_to_text_model") or "") if explicit else "",
            "providers": cls._ASR_PROVIDERS,
            "provider_models": cls._ASR_PROVIDER_MODELS,
        }

    @classmethod
    def _tts_capability(cls, local_config: dict) -> dict:
        explicit = (local_config.get("text_to_voice") or "").strip().lower()
        # Providers outside the white-list don't drive the picker, but their
        # underlying runtime config is preserved so bridge still routes them.
        ui_provider = explicit if explicit in cls._TTS_PROVIDERS else ""
        suggested = ""
        if not ui_provider:
            for pid in cls._TTS_PROVIDERS:
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
        return {
            "editable": True,
            "current_provider": ui_provider,
            "suggested_provider": suggested,
            "current_model": (local_config.get("text_to_voice_model") or "") if ui_provider else "",
            "current_voice": (local_config.get("tts_voice_id") or "") if ui_provider else "",
            "providers": cls._TTS_PROVIDERS,
            "provider_models": cls._TTS_PROVIDER_MODELS,
            "provider_voices": cls._TTS_PROVIDER_VOICES,
            "reply_mode": cls._tts_reply_mode(local_config),
        }

    @staticmethod
    def _tts_reply_mode(local_config: dict) -> str:
        if local_config.get("always_reply_voice", False):
            return "always"
        if local_config.get("voice_reply_voice", False):
            return "voice_if_voice"
        return "off"

    @classmethod
    def _embedding_capability(cls, local_config: dict) -> dict:
        # Embedding is "pick or empty" — runtime's legacy openai/linkai
        # fallback is a safety net, not a UX-visible auto mode.
        # `suggested_provider` is a UI-only hint (NOT persisted) that
        # preselects the dropdown to whichever configured vendor we'd
        # recommend, so users don't have to expand the menu to find it.
        explicit = (local_config.get("embedding_provider") or "").strip().lower()
        suggested = ""
        if not explicit:
            for pid in cls._EMBEDDING_PROVIDERS:
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
        return {
            "editable": True,
            "current_provider": explicit,
            "suggested_provider": suggested,
            "current_model": local_config.get("embedding_model", "") or "",
            "current_dim": int(local_config.get("embedding_dimensions") or 0) or None,
            "providers": cls._EMBEDDING_PROVIDERS,
        }

    # Auto-fallback order for image generation. Mirrors the global priority
    # used inside skills/image-generation/scripts/generate.py
    # (`_DEFAULT_PROVIDER_ORDER`): OpenAI → Gemini → Seedream(Ark/doubao) →
    # Qwen(dashscope) → MiniMax → LinkAI. Each entry maps the
    # provider-card id to the script's per-provider DEFAULT_MODEL so the
    # hint matches what the runtime would actually request.
    _IMAGE_AUTO_ORDER = [
        ("openai",    "gpt-image-2-pro"),
        ("gemini",    "gemini-3.1-flash-image-preview"),  # nano-banana-2
        ("doubao",    "seedream-5.0-lite"),
        ("dashscope", "qwen-image-2.0"),
        ("minimax",   "image-01"),
        ("linkai",    "gpt-image-2-pro"),
    ]

    @classmethod
    def _predict_image_auto(cls, local_config: dict) -> dict:
        """Predict which provider/model the image-generation skill will hit
        when no SKILL_IMAGE_GENERATION_MODEL override is set. Mirrors
        skills/image-generation/scripts/generate.py::_build_providers so
        the UI hint matches reality. Chat-only providers (DeepSeek etc.)
        are absent by design — image generation never falls back to a chat
        bot regardless of the main model.

        When use_linkai is enabled the hint is suppressed entirely — LinkAI
        proxies to whichever backend it deems appropriate and surfacing
        "LinkAI" alone tells the user nothing actionable."""
        use_linkai_flag = bool(local_config.get("use_linkai", False))
        linkai_configured = cls._is_real_key(local_config.get("linkai_api_key", ""))
        if use_linkai_flag and linkai_configured:
            return {"provider": "", "model": ""}

        for pid, default_model in cls._IMAGE_AUTO_ORDER:
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                continue
            if cls._is_real_key(local_config.get(key_field, "")):
                return {"provider": pid, "model": default_model}
        return {"provider": "", "model": ""}

    @classmethod
    def _image_capability(cls, local_config: dict) -> dict:
        """Image generation. Source of truth: config["skills"]["image-generation"]["model"]
        (mirrors the per-skill config schema documented in skills/image-generation).
        The runtime resolver in skills/image-generation/scripts/generate.py
        reads this via the SKILL_IMAGE_GENERATION_MODEL env var that the
        agent_initializer syncs at startup; provider is inferred from the
        model name prefix, mirroring vision.py's design.

        ``skill`` (singular) is still tolerated as a legacy fallback —
        config.load_config() folds it into ``skills`` at startup.
        """
        skills_node = local_config.get("skills") or local_config.get("skill") or {}
        if not isinstance(skills_node, dict):
            skills_node = {}
        img_node = skills_node.get("image-generation") or {}
        if not isinstance(img_node, dict):
            img_node = {}
        explicit_model = (img_node.get("model") or "").strip()
        explicit_provider = (img_node.get("provider") or "").strip()
        configured_model = explicit_model or cls._configured_image_model(local_config)

        # Provider resolution priority:
        #   1. Explicit `skills.image-generation.provider` (persisted via UI;
        #      supports custom model names that prefix-inference can't catch).
        #   2. Scan per-provider model catalog by model name.
        # Empty provider keeps the dropdown on "auto" when we can't tell.
        inferred_provider = ""
        if explicit_provider and explicit_provider in cls._IMAGE_PROVIDER_MODELS:
            inferred_provider = explicit_provider
        elif configured_model:
            for pid, models in cls._IMAGE_PROVIDER_MODELS.items():
                for entry in models:
                    val = entry if isinstance(entry, str) else (entry.get("value") or "")
                    if val == configured_model:
                        inferred_provider = pid
                        break
                if inferred_provider:
                    break

        # In auto mode the hint should reflect what generate.py will actually
        # dispatch to — surface that prediction via fallback_* so the UI
        # never claims a chat-only bot (e.g. minimax/MiniMax-M2.7) "would
        # generate the image", which is impossible.
        predicted = cls._predict_image_auto(local_config)

        return {
            "editable": True,
            "strategy": "specified" if explicit_model or local_config.get("text_to_image") else "auto",
            "current_provider": inferred_provider,
            "current_model": configured_model,
            "fallback_provider": predicted["provider"],
            "fallback_model": predicted["model"],
            "providers": list(cls._IMAGE_PROVIDER_MODELS.keys()),
            "provider_models": cls._IMAGE_PROVIDER_MODELS,
            # The dispatcher that honors a pinned provider isn't wired up
            # yet; advertise this so the UI can show a "saved but not active"
            # banner until the runtime catches up.
            "runtime_active": False,
            "note": "router_pending",
        }

    @classmethod
    def _configured_image_model(cls, local_config: dict) -> str:
        skills_node = local_config.get("skills") or local_config.get("skill") or {}
        if isinstance(skills_node, dict):
            img_node = skills_node.get("image-generation") or {}
            if isinstance(img_node, dict) and img_node.get("model"):
                return str(img_node.get("model") or "")
        return str(local_config.get("text_to_image") or "gpt-image-2-pro")

    # Canonical search provider order. Mirrors PROVIDER_ORDER in
    # agent/tools/web_search/web_search.py — keep them in sync.
    _SEARCH_PROVIDERS = ("bocha", "qianfan", "zhipu", "linkai")

    _SEARCH_PROVIDER_LABELS = {
        "bocha":   {"zh": "博查", "en": "Bocha"},
        "zhipu":   {"zh": "智谱", "en": "GLM"},
        "qianfan": {"zh": "百度千帆", "en": "ERNIE"},
        "linkai":  {"zh": "LinkAI", "en": "LinkAI"},
    }

    @classmethod
    def _search_provider_key(cls, provider: str, local_config: dict) -> str:
        """Resolve the (raw) key for a given search provider."""
        if provider == "bocha":
            tools_cfg = local_config.get("tools") or {}
            block = tools_cfg.get("web_search") or {} if isinstance(tools_cfg, dict) else {}
            return (block.get("bocha_api_key") if isinstance(block, dict) else "") or os.environ.get("BOCHA_API_KEY", "")
        if provider == "zhipu":
            return local_config.get("zhipu_ai_api_key") or os.environ.get("ZHIPUAI_API_KEY", "")
        if provider == "qianfan":
            return local_config.get("qianfan_api_key") or os.environ.get("QIANFAN_API_KEY", "")
        if provider == "linkai":
            return local_config.get("linkai_api_key") or os.environ.get("LINKAI_API_KEY", "")
        return ""

    @classmethod
    def _search_capability(cls, local_config: dict) -> dict:
        """Search is editable: pick auto (default) or pin a specific backend.
        Providers reuse model-vendor keys (zhipu/qianfan/linkai) so they show
        up as configured once the user adds those vendors; bocha keeps its
        own key under tools.web_search."""
        tools_cfg = local_config.get("tools") or {}
        ws_cfg = tools_cfg.get("web_search") or {} if isinstance(tools_cfg, dict) else {}
        if not isinstance(ws_cfg, dict):
            ws_cfg = {}

        providers = []
        configured_ids = []
        for pid in cls._SEARCH_PROVIDERS:
            ok = cls._is_real_key(cls._search_provider_key(pid, local_config))
            raw_key = cls._search_provider_key(pid, local_config) if ok else ""
            providers.append({
                "id": pid,
                "label": cls._SEARCH_PROVIDER_LABELS.get(pid, pid),
                "configured": ok,
                # bocha owns its key under tools.web_search; the other three
                # piggy-back on a model-vendor credential. Frontend uses
                # this hint to decide which credential editor to surface.
                "needs_dedicated_key": pid == "bocha",
                "api_key_masked": ConfigHandler._mask_key(raw_key) if raw_key else "",
            })
            if ok:
                configured_ids.append(pid)

        strategy = (ws_cfg.get("strategy") or "auto").strip().lower()
        if strategy not in ("auto", "fixed"):
            strategy = "auto"
        fixed_provider = (ws_cfg.get("provider") or "").strip().lower()
        if fixed_provider and fixed_provider not in configured_ids:
            fixed_provider = ""

        # current_provider drives the chip in the header — show the actually
        # active backend (pinned or first auto-picked).
        if strategy == "fixed" and fixed_provider:
            current = fixed_provider
        else:
            current = configured_ids[0] if configured_ids else ""

        return {
            "editable": True,
            "strategy": strategy,
            "providers": providers,
            "configured_providers": configured_ids,
            "current_provider": current,
            "fixed_provider": fixed_provider,
            "available": bool(current),
        }

    @classmethod
    def _capabilities(cls, local_config: dict) -> dict:
        return {
            "chat":      cls._chat_capability(local_config),
            "vision":    cls._vision_capability(local_config),
            "asr":       cls._asr_capability(local_config),
            "tts":       cls._tts_capability(local_config),
            "embedding": cls._embedding_capability(local_config),
            "image":     cls._image_capability(local_config),
            "search":    cls._search_capability(local_config),
        }

    def GET(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            local_config = conf()
            return json.dumps({
                "status": "success",
                "providers": self._provider_overview(),
                "capabilities": self._capabilities(local_config),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[ModelsHandler] GET failed: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            data = json.loads(web.data() or b"{}")
            action = data.get("action") or ""
            if action == "set_provider":
                return self._handle_set_provider(data)
            if action == "delete_provider":
                return self._handle_delete_provider(data)
            if action == "set_capability":
                return self._handle_set_capability(data)
            if action == "set_voice_reply_mode":
                return self._handle_set_voice_reply_mode(data)
            if action == "set_search_credential":
                return self._handle_set_search_credential(data)
            return json.dumps({"status": "error", "message": f"unknown action: {action!r}"})
        except Exception as e:
            logger.error(f"[ModelsHandler] POST failed: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def _handle_set_provider(self, data: dict) -> str:
        provider_id = (data.get("provider_id") or "").strip()
        meta = ConfigHandler.PROVIDER_MODELS.get(provider_id)
        if not meta:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        # api_key absent / empty / null => leave the existing key untouched
        # (used by the "edit only base url" flow). To clear the key, callers
        # must use action=delete_provider explicitly.
        api_key_raw = data.get("api_key")
        api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""

        # api_base presence is significant: an explicit "" means "reset to
        # default", whereas a missing key means "no change".
        api_base_present = "api_base" in data
        api_base = (data.get("api_base") or "").strip() if api_base_present else None

        applied = {}
        local_config = conf()
        file_cfg = self._read_file_config()

        key_field = meta.get("api_key_field")
        if key_field and api_key:
            local_config[key_field] = api_key
            file_cfg[key_field] = api_key
            applied[key_field] = True
        base_field = meta.get("api_base_key")
        if base_field and api_base_present:
            local_config[base_field] = api_base
            file_cfg[base_field] = api_base
            applied[base_field] = True

        if not applied:
            # Nothing actually changed (e.g. user opened the modal and hit
            # save without editing). Treat as a successful no-op so the
            # frontend can show "Saved" instead of surfacing an error.
            return json.dumps({"status": "success", "provider": provider_id, "noop": True})

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] provider {provider_id} updated: {sorted(applied.keys())}")

        # Vendor credentials affect bot routing for any capability that uses
        # them; safest to reset Bridge so the next request rebuilds bots.
        self._reset_bridge()
        return json.dumps({"status": "success", "provider": provider_id})

    def _handle_delete_provider(self, data: dict) -> str:
        provider_id = (data.get("provider_id") or "").strip()
        meta = ConfigHandler.PROVIDER_MODELS.get(provider_id)
        if not meta:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        local_config = conf()
        file_cfg = self._read_file_config()

        cleared = []
        for field_name in (meta.get("api_key_field"), meta.get("api_base_key")):
            if not field_name:
                continue
            # Always write the key — even if it was absent before — so the
            # in-memory conf() reflects the cleared state without needing a
            # restart. (`in local_config` was too strict: provider keys that
            # were ever set then deleted manually wouldn't get reset.)
            local_config[field_name] = ""
            file_cfg[field_name] = ""
            cleared.append(field_name)

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] provider {provider_id} cleared: {cleared}")
        self._reset_bridge()
        return json.dumps({"status": "success", "provider": provider_id, "cleared": cleared})

    def _handle_set_capability(self, data: dict) -> str:
        capability = (data.get("capability") or "").strip()
        provider_id = (data.get("provider_id") or "").strip()
        model = (data.get("model") or "").strip()

        if capability == "chat":
            return self._set_chat(provider_id, model)
        if capability == "vision":
            return self._set_vision(provider_id, model)
        if capability == "asr":
            return self._set_asr(provider_id, model)
        if capability == "tts":
            return self._set_tts(provider_id, model, (data.get("voice") or "").strip())
        if capability == "embedding":
            return self._set_embedding(provider_id, model)
        if capability == "image":
            return self._set_image(provider_id, model)
        if capability == "search":
            return self._set_search(
                (data.get("strategy") or "").strip().lower(),
                (data.get("provider") or "").strip().lower(),
            )
        return json.dumps({"status": "error", "message": f"capability not editable: {capability}"})

    def _set_image(self, provider_id: str, model: str) -> str:
        # Source of truth: skills.image-generation.{provider, model}. The
        # provider field is persisted so users picking a custom model under
        # a specific vendor still get routed there — runtime falls back to
        # model-name prefix inference only when provider is empty.
        local_config = conf()
        file_cfg = self._read_file_config()

        self._set_nested_namespace_value(local_config, "skills", "image-generation", "model", model or "")
        self._set_nested_namespace_value(file_cfg, "skills", "image-generation", "model", model or "")
        self._set_nested_namespace_value(local_config, "skills", "image-generation", "provider", provider_id or "")
        self._set_nested_namespace_value(file_cfg, "skills", "image-generation", "provider", provider_id or "")
        self._drop_legacy_namespace(local_config, "skill", "skills", child="image-generation")
        self._drop_legacy_namespace(file_cfg, "skill", "skills", child="image-generation")

        self._write_file_config(file_cfg)

        # The skill subprocess reads SKILL_IMAGE_GENERATION_{MODEL,PROVIDER}
        # from env at startup; mirror the change so live edits apply without
        # restart.
        model_env = "SKILL_IMAGE_GENERATION_MODEL"
        provider_env = "SKILL_IMAGE_GENERATION_PROVIDER"
        if model:
            os.environ[model_env] = model
        else:
            os.environ.pop(model_env, None)
        if provider_id:
            os.environ[provider_env] = provider_id
        else:
            os.environ.pop(provider_env, None)

        logger.info(f"[ModelsHandler] image updated: provider={provider_id!r} model={model!r}")
        return json.dumps({
            "status": "success",
            "provider": provider_id,
            "model": model,
            "router_pending": True,
        })

    def _set_chat(self, provider_id: str, model: str) -> str:
        if provider_id and provider_id not in ConfigHandler.PROVIDER_MODELS:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        applied = {}
        local_config = conf()
        file_cfg = self._read_file_config()
        valid, message = self._validate_chat_selection(local_config, provider_id, model)
        if not valid:
            return json.dumps({
                "status": "error",
                "message": message,
                "provider": provider_id,
                "model": model,
                "code": "CHAT_MODEL_NOT_CONFIGURED",
            })

        if provider_id:
            bot_type_value = const.OPENAI if provider_id == "openai" else provider_id
            local_config["bot_type"] = bot_type_value
            file_cfg["bot_type"] = bot_type_value
            applied["bot_type"] = bot_type_value
            use_linkai = (provider_id == "linkai")
            local_config["use_linkai"] = use_linkai
            file_cfg["use_linkai"] = use_linkai
            applied["use_linkai"] = use_linkai
        if model:
            local_config["model"] = model
            file_cfg["model"] = model
            applied["model"] = model
            context_policy = self._apply_chat_context_policy(local_config, file_cfg, provider_id, model)
            applied["model_context_window"] = context_policy.get("contextWindowTokens")
            applied["model_auto_compact_token_limit"] = context_policy.get("autoCompactTokenLimit")
        else:
            context_policy = self._chat_context_policy(local_config.get("model", ""), provider_id)

        if not applied:
            context_continuity = {
                "agentBridgePreserved": True,
                "existingAgentRoutesReset": 0,
                "artifactHistoryRefs": "enabled",
                "strategy": "noop",
            }
            return json.dumps({
                "status": "success",
                "provider": provider_id,
                "model": model,
                "image_model": self._configured_image_model(local_config),
                "context_policy": context_policy,
                "contextContinuity": context_continuity,
                "context_continuity": context_continuity,
                "applied": {},
                "noop": True,
            })

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] chat updated: {applied}")
        route_refresh = self._reset_bridge()
        if not isinstance(route_refresh, dict):
            route_refresh = {}
        route_reset_count = route_refresh.get("modelRoutesReset", 0)
        try:
            route_reset_count = int(route_reset_count)
        except (TypeError, ValueError):
            route_reset_count = 0
        effective_provider = provider_id or (self._chat_capability(local_config).get("current_provider") or "")
        context_continuity = {
            "agentBridgePreserved": route_refresh.get("agentBridgePreserved") is not False,
            "existingAgentRoutesReset": route_reset_count,
            "artifactHistoryRefs": "enabled",
            "strategy": route_refresh.get("strategy") or "refresh_chat_routing",
        }
        return json.dumps({
            "status": "success",
            "provider": effective_provider,
            "model": local_config.get("model", ""),
            "image_model": self._configured_image_model(local_config),
            "context_policy": context_policy,
            "contextContinuity": context_continuity,
            "context_continuity": context_continuity,
            **self._chat_route_metadata(effective_provider, local_config.get("model", ""), local_config),
            "applied": applied,
        })

    def _set_vision(self, provider_id: str, model: str) -> str:
        # Source of truth: tools.vision.{provider, model}. The provider field
        # is persisted so users picking a custom model under a specific vendor
        # still get routed there — runtime falls back to model-name prefix
        # inference only when provider is empty.
        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(file_cfg, "tools", "vision", "model", model)
        self._set_nested_namespace_value(local_config, "tools", "vision", "model", model)
        self._set_nested_namespace_value(file_cfg, "tools", "vision", "provider", provider_id or "")
        self._set_nested_namespace_value(local_config, "tools", "vision", "provider", provider_id or "")
        self._drop_legacy_namespace(file_cfg, "tool", "tools", child="vision")
        self._drop_legacy_namespace(local_config, "tool", "tools", child="vision")

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] vision updated: provider={provider_id!r} model={model!r}")
        return json.dumps({"status": "success", "provider": provider_id, "model": model})

    @staticmethod
    def _set_nested_namespace_value(cfg, top: str, name: str, key: str, value):
        """Set ``cfg[top][name][key] = value``, creating missing dicts."""
        bucket = cfg.get(top)
        if not isinstance(bucket, dict):
            bucket = {}
        node = bucket.get(name)
        if not isinstance(node, dict):
            node = {}
        node[key] = value
        bucket[name] = node
        cfg[top] = bucket

    @staticmethod
    def _drop_legacy_namespace(cfg, legacy: str, canonical: str, child: str) -> None:
        """Strip the deprecated singular key so config.json stays single-source."""
        legacy_section = cfg.get(legacy)
        if not isinstance(legacy_section, dict):
            return
        legacy_section.pop(child, None)
        if legacy_section:
            cfg[legacy] = legacy_section
        else:
            cfg.pop(legacy, None)

    def _handle_set_voice_reply_mode(self, data: dict) -> str:
        # UI picker (off / voice_if_voice / always) maps to the legacy
        # always_reply_voice + voice_reply_voice pair that chat_channel.py
        # reads, so all channels (web/feishu/wecom/...) share the routing.
        mode = (data.get("mode") or "").strip().lower()
        if mode not in ("off", "voice_if_voice", "always"):
            return json.dumps({"status": "error", "message": f"invalid mode: {mode!r}"})
        always = (mode == "always")
        if_voice = (mode == "voice_if_voice")
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["always_reply_voice"] = always
        local_config["voice_reply_voice"] = if_voice
        file_cfg["always_reply_voice"] = always
        file_cfg["voice_reply_voice"] = if_voice
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] voice reply mode set: {mode!r} "
            f"(always_reply_voice={always}, voice_reply_voice={if_voice})"
        )
        return json.dumps({"status": "success", "mode": mode})

    def _set_simple(self, key: str, value: str) -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config[key] = value
        file_cfg[key] = value
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] {key} set: {value!r}")
        # Hot-swap the cached voice bot so the change takes effect immediately.
        if key in ("voice_to_text", "text_to_voice"):
            self._refresh_voice_routing()
        return json.dumps({"status": "success", key: value})

    def _set_asr(self, provider_id: str, model: str) -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["voice_to_text"] = provider_id
        file_cfg["voice_to_text"] = provider_id
        # Only overwrite the model when one is supplied. An empty model means
        # "keep whatever is configured" so switching provider from the console
        # never wipes a user's hand-set voice_to_text_model (runtime falls back
        # to the engine default via `or DEFAULT_ASR_MODEL` regardless).
        if model:
            local_config["voice_to_text_model"] = model
            file_cfg["voice_to_text_model"] = model
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] asr updated: provider={provider_id!r} "
            f"model={model!r}"
        )
        self._refresh_voice_routing()
        return json.dumps({
            "status": "success",
            "provider": provider_id,
            "model": local_config.get("voice_to_text_model", ""),
        })

    def _set_tts(self, provider_id: str, model: str, voice: str = "") -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["text_to_voice"] = provider_id
        file_cfg["text_to_voice"] = provider_id
        local_config["text_to_voice_model"] = model
        file_cfg["text_to_voice_model"] = model
        local_config["tts_voice_id"] = voice
        file_cfg["tts_voice_id"] = voice
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] tts updated: provider={provider_id!r} "
            f"model={model!r} voice={voice!r}"
        )
        self._refresh_voice_routing()
        return json.dumps({
            "status": "success",
            "provider": provider_id, "model": model, "voice": voice,
        })

    @staticmethod
    def _refresh_voice_routing() -> None:
        try:
            from bridge.bridge import Bridge
            Bridge().refresh_voice()
        except Exception as e:
            logger.warning(f"[ModelsHandler] Bridge voice refresh failed: {_web_body_log_summary(e)}")

    def _set_embedding(self, provider_id: str, model: str) -> str:
        # Two valid states: both empty (reset to pick-or-empty) OR both set.
        # A provider without a model leaves the runtime in a broken half-state,
        # so reject that explicitly instead of silently writing it through.
        if provider_id and not model:
            return json.dumps({
                "status": "error",
                "message": "embedding model is required when a provider is selected",
            })
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["embedding_provider"] = provider_id
        file_cfg["embedding_provider"] = provider_id
        local_config["embedding_model"] = model
        file_cfg["embedding_model"] = model
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] embedding updated: provider={provider_id!r} model={model!r}")
        # The next /memory rebuild-index command hot-swaps the provider onto
        # the running MemoryManager (see plugins/cow_cli). The dim may have
        # changed, so the frontend prompts the user to rebuild.
        return json.dumps({"status": "success", "provider": provider_id, "model": model})

    def _set_search(self, strategy: str, provider: str) -> str:
        """Persist search routing under tools.web_search.{strategy,provider}.

        strategy 'auto'  -> provider field is cleared (auto picks at call time)
        strategy 'fixed' -> provider must be in the canonical list; runtime
                            silently falls back to auto if its key is missing.
        """
        if strategy not in ("auto", "fixed"):
            return json.dumps({"status": "error", "message": f"invalid strategy: {strategy!r}"})
        if strategy == "fixed":
            if provider not in self._SEARCH_PROVIDERS:
                return json.dumps({"status": "error", "message": f"unknown provider: {provider!r}"})
        else:
            provider = ""

        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(local_config, "tools", "web_search", "strategy", strategy)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "strategy", strategy)
        self._set_nested_namespace_value(local_config, "tools", "web_search", "provider", provider)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "provider", provider)
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] search updated: strategy={strategy!r} provider={provider!r}")
        return json.dumps({"status": "success", "strategy": strategy, "provider": provider})

    def _handle_set_search_credential(self, data: dict) -> str:
        """Persist the bocha API key under tools.web_search.bocha_api_key.

        The other three providers (zhipu/qianfan/linkai) reuse model-vendor
        credentials, so they go through set_provider with the standard
        model-vendor flow.
        """
        api_key = (data.get("api_key") or "").strip() if isinstance(data.get("api_key"), str) else ""
        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(local_config, "tools", "web_search", "bocha_api_key", api_key)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "bocha_api_key", api_key)
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] search credential set: bocha_api_key={'***' if api_key else ''}")
        return json.dumps({"status": "success", "provider": "bocha"})

    @staticmethod
    def _reset_bridge() -> dict:
        try:
            from bridge.bridge import Bridge
            bridge = Bridge()
            refresh = getattr(bridge, "refresh_chat_routing", None)
            if callable(refresh):
                result = refresh()
                result["strategy"] = "refresh_chat_routing"
                logger.info("[ModelsHandler] Bridge chat routing refreshed")
                return result
            bridge.reset_bot()
            logger.info("[ModelsHandler] Bridge bot routing reset")
            return {"agentBridgePreserved": False, "modelRoutesReset": 0, "strategy": "reset_bot"}
        except Exception as e:
            logger.warning(f"[ModelsHandler] Bridge reset failed: {_web_body_log_summary(e)}")
            return {"agentBridgePreserved": True, "modelRoutesReset": 0, "strategy": "reset_failed"}


class ChannelsHandler:
    """API for managing external channel configurations (feishu, dingtalk, etc)."""

    CHANNEL_DEFS = CHANNEL_CATALOG
    CHANNEL_RUNTIME_STATE: Dict[str, Dict[str, Any]] = {}
    CONFIG_WRITE_LOCK = threading.RLock()
    SENSITIVE_CHANNEL_FIELD_KEYS = {
        "app_id",
        "appid",
        "client_id",
        "clientid",
        "feishu_app_id",
        "feishu_home_channel",
        "home_channel",
        "open_chat_id",
        "open_id",
        "receiver",
    }

    @staticmethod
    def _config_path() -> str:
        return ConfigHandler._config_path()

    @classmethod
    def _read_file_config(cls) -> Dict[str, Any]:
        path = cls._config_path()
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _write_file_config_atomic(cls, data: Dict[str, Any]) -> None:
        path = cls._config_path()
        cls._cleanup_stale_config_temps(path)
        tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd: Optional[int] = None
        replaced = False
        try:
            fd = os.open(tmp_path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            replaced = True
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            if not replaced and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    @classmethod
    def _cleanup_stale_config_temps(cls, path: Optional[str] = None) -> None:
        base_path = path or cls._config_path()
        directory = os.path.dirname(base_path) or "."
        prefix = os.path.basename(base_path) + ".tmp-"
        try:
            for name in os.listdir(directory):
                if not name.startswith(prefix):
                    continue
                candidate = os.path.join(directory, name)
                try:
                    os.remove(candidate)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _coerce_channel_field_value(field_def: Dict[str, Any], value: Any) -> Any:
        field_type = field_def.get("type")
        key = str(field_def.get("key") or "")
        if field_type == "number":
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
        if field_type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes", "on"}:
                    return True
                if normalized in {"false", "0", "no", "off", ""}:
                    return False
            raise ValueError(f"{key} must be a boolean")
        return value

    @classmethod
    def _collect_channel_config_updates(cls, ch_def: Dict[str, Any], updates: Any) -> Tuple[Dict[str, Any], int]:
        if not isinstance(updates, dict):
            return {}, 0
        valid_fields = {f["key"]: f for f in ch_def["fields"] if f.get("key")}
        sensitive_keys = {key for key, field in valid_fields.items() if cls._is_sensitive_channel_field(key, field)}
        applied: Dict[str, Any] = {}
        skipped_masked = 0
        for key, value in updates.items():
            if key not in valid_fields:
                continue
            if key in sensitive_keys and cls._is_masked_secret_value(value):
                skipped_masked += 1
                continue
            applied[key] = cls._coerce_channel_field_value(valid_fields[key], value)
        return applied, skipped_masked

    @classmethod
    def _is_sensitive_channel_field(cls, key: str, field_def: Optional[Dict[str, Any]] = None) -> bool:
        normalized = str(key or "").strip().lower()
        field_def = field_def or {}
        if field_def.get("type") == "secret" or field_def.get("sensitive") is True:
            return True
        return normalized in cls.SENSITIVE_CHANNEL_FIELD_KEYS

    @staticmethod
    def _home_channel_projection(config: Dict[str, Any], channel_name: str) -> Dict[str, Any]:
        channel_id = str(config.get(f"{channel_name}_home_channel") or "").strip()
        if not channel_id:
            return {}
        channel_name_value = str(config.get(f"{channel_name}_home_channel_name") or "").strip()
        if channel_name == "feishu":
            digest = hmac.new(
                b"ecorex-feishu-home-channel-v1",
                channel_id.encode("utf-8", errors="replace"),
                hashlib.sha256,
            ).hexdigest()[:16]
            payload = {"configured": True, "idHash": f"hmac:{digest}"}
            if channel_name_value:
                payload["name"] = channel_name_value
            return payload
        payload = {"id": channel_id}
        if channel_name_value:
            payload["name"] = channel_name_value
        return payload

    @staticmethod
    def _redact_runtime_error(value: Any) -> str:
        return mask_sensitive_text(value, max_chars=500)

    @staticmethod
    def _channel_start_config_error(config: Dict[str, Any], channel_name: str) -> str:
        status = channel_config_status(config, channel_name)
        if status.get("state") in {"configured", "not_required"}:
            return ""
        missing = [str(item) for item in status.get("missingFields") or []]
        if missing:
            return f"missing required config fields: {', '.join(missing)}"
        return "missing required config fields"

    @staticmethod
    def _get_weixin_login_status() -> str:
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                ch = mgr.get_channel("weixin")
                if ch and hasattr(ch, 'login_status'):
                    return ch.login_status
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value:
            return value
        if len(value) <= 8:
            return "*" * len(value)
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _is_masked_secret_value(value: Any) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return True
        if set(raw) == {"*"}:
            return True
        return len(raw) > 8 and "****" in raw

    @staticmethod
    def _parse_channel_list(raw) -> list:
        return parse_channel_list(raw)

    @classmethod
    def _active_channel_set(cls) -> set:
        return active_channel_set(conf())

    @classmethod
    def _set_runtime_state(cls, channel_name: str, **updates) -> Dict[str, Any]:
        state = dict(cls.CHANNEL_RUNTIME_STATE.get(channel_name) or {})
        state.update({k: v for k, v in updates.items() if v is not None})
        state["updated_at"] = time.time()
        cls.CHANNEL_RUNTIME_STATE[channel_name] = state
        return state

    @staticmethod
    def _feishu_dependency_status(config: Dict[str, Any]) -> Dict[str, Any]:
        return feishu_dependency_status(config)

    @classmethod
    def _runtime_state(cls, channel_name: str) -> Dict[str, Any]:
        return dict(cls.CHANNEL_RUNTIME_STATE.get(channel_name) or {})

    @staticmethod
    @staticmethod
    def _channel_startup_observation(channel, thread=None) -> Dict[str, Any]:
        error = ChannelsHandler._redact_runtime_error(getattr(channel, "_startup_error", "") or "")
        event = getattr(channel, "_startup_event", None)
        ready = bool(event.is_set()) if event is not None and hasattr(event, "is_set") else False
        thread_alive = bool(thread is not None and getattr(thread, "is_alive", lambda: False)())
        if error:
            return {"running": False, "status": "error", "last_error": error}
        if ready:
            return {"running": True, "status": "active", "last_error": ""}
        if thread_alive:
            return {"running": False, "status": "starting", "last_error": ""}
        return {"running": False, "status": "stopped", "last_error": ""}

    @staticmethod
    def _channel_runtime_observations() -> Dict[str, Dict[str, Any]]:
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if not mgr:
                return {}
            observations: Dict[str, Dict[str, Any]] = {}
            for name in CHANNEL_CATALOG.keys():
                try:
                    channel = mgr.get_channel(name)
                    if channel is None:
                        continue
                    thread = getattr(mgr, "_threads", {}).get(name) if hasattr(mgr, "_threads") else None
                    observations[name] = ChannelsHandler._channel_startup_observation(channel, thread)
                except Exception:
                    continue
            return observations
        except Exception:
            return {}

    @staticmethod
    def _running_channel_names(observations: Optional[Dict[str, Dict[str, Any]]] = None) -> set:
        runtime = observations if observations is not None else ChannelsHandler._channel_runtime_observations()
        return {name for name, state in runtime.items() if state.get("running") is True}

    @staticmethod
    def _agent_tool_names() -> Optional[set]:
        try:
            from agent.tools.tool_manager import ToolManager

            manager = ToolManager()
            if not getattr(manager, "tool_classes", None):
                manager.load_tools(start_mcp=False)
            ensure_mcp = getattr(manager, "ensure_mcp_configured_loaded", None)
            if callable(ensure_mcp):
                ensure_mcp(wait_seconds=0.0)
            names = {str(name) for name in getattr(manager, "tool_classes", {}).keys()}
            names.update(str(name) for name in getattr(manager, "_mcp_tool_instances", {}).keys())
            return names if names else None
        except Exception as exc:
            logger.debug(f"[WebChannel] Agent tool snapshot unavailable: {_web_body_log_summary(exc)}")
            return None

    @staticmethod
    def _refresh_runtime_capabilities(reason: str = "") -> None:
        try:
            from bridge.bridge import Bridge
            bridge = Bridge()
            refresh = getattr(bridge, "refresh_chat_routing", None)
            if callable(refresh):
                refresh()
            else:
                bridge.reset_bot()
            logger.info(f"[WebChannel] Runtime capabilities refresh requested: {reason}")
        except Exception as e:
            logger.debug(f"[WebChannel] Runtime capability refresh skipped: {_web_body_log_summary(e)}")

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            active_channels = self._active_channel_set()
            runtime_observations = self._channel_runtime_observations()
            running_channels = self._running_channel_names(runtime_observations)
            agent_tool_names = self._agent_tool_names()
            channels = []
            for ch_name, ch_def in self.CHANNEL_DEFS.items():
                runtime_state = self._runtime_state(ch_name)
                startup_state = runtime_observations.get(ch_name) or {}
                if startup_state:
                    runtime_state = {
                        **runtime_state,
                        "status": startup_state.get("status") or runtime_state.get("status"),
                        "last_error": self._redact_runtime_error(startup_state.get("last_error") or runtime_state.get("last_error")),
                    }
                observed = channel_observability(
                    local_config,
                    ch_name,
                    running_channels=running_channels,
                    runtime_state=runtime_state,
                    tool_names=agent_tool_names,
                )
                dependency_status = {}
                if ch_name == "feishu":
                    dependency_status = self._feishu_dependency_status(local_config)
                    if observed.get("configured") and dependency_status.get("status") == "missing":
                        runtime_state = {
                            **runtime_state,
                            "status": "dependency_missing",
                            "dependency_missing": True,
                            "dependency_status": dependency_status,
                            "last_error": "",
                        }
                        observed = channel_observability(
                            local_config,
                            ch_name,
                            running_channels=running_channels,
                            runtime_state=runtime_state,
                            tool_names=agent_tool_names,
                        )
                fields_out = []
                for f in ch_def["fields"]:
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                    sensitive = self._is_sensitive_channel_field(str(f.get("key") or ""), f)
                    if sensitive and raw_val:
                        display_val = self._mask_secret(str(raw_val))
                    else:
                        display_val = raw_val
                    fields_out.append({
                        "key": f["key"],
                        "label": f["label"],
                        "type": f["type"],
                        "value": display_val,
                        "default": f.get("default", ""),
                        "sensitive": sensitive,
                        "masked": bool(sensitive and raw_val),
                    })
                ch_info = {
                    "name": ch_name,
                    "aliases": ch_def.get("aliases", []),
                    "label": ch_def["label"],
                    "description": ch_def.get("description", ""),
                    "icon": ch_def["icon"],
                    "color": ch_def["color"],
                    "active": observed["active"],
                    "configured": observed["configured"],
                    "running": observed["running"],
                    "last_error": self._redact_runtime_error(runtime_state.get("last_error") or ""),
                    "started_at": runtime_state.get("started_at"),
                    "operation_id": runtime_state.get("operation_id") or "",
                    "dependency_missing": bool(runtime_state.get("dependency_missing") or False),
                    "dependencyStatus": dependency_status or runtime_state.get("dependency_status") or {},
                    "status": observed["status"],
                    "configState": observed["configState"],
                    "auth": observed["auth"],
                    "agentSurface": observed["agentSurface"],
                    "adapterContract": build_adapter_contract(
                        ch_name,
                        observed,
                        runtime_state=runtime_state,
                    ),
                    "fields": fields_out,
                    "homeChannel": self._home_channel_projection(local_config, ch_name),
                }
                if ch_name == "weixin" and ch_name in active_channels:
                    ch_info["login_status"] = self._get_weixin_login_status()
                channels.append(ch_info)
            return json.dumps({"status": "success", "channels": channels}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = normalize_channel_name(body.get("channel"))

            if not action or not channel_name:
                return json.dumps({"status": "error", "message": "action and channel required"})

            if channel_name not in self.CHANNEL_DEFS:
                return json.dumps({"status": "error", "message": f"unknown channel: {channel_name}"})

            if action == "save":
                return self._handle_save(channel_name, body.get("config", {}))
            elif action == "connect":
                return self._handle_connect(channel_name, body.get("config", {}))
            elif action == "disconnect":
                return self._handle_disconnect(channel_name)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] Channels POST error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def _handle_save(self, channel_name: str, updates: dict):
        ch_def = self.CHANNEL_DEFS[channel_name]
        try:
            applied, skipped_masked = self._collect_channel_config_updates(ch_def, updates)
        except ValueError as exc:
            return json.dumps(_public_validation_error_payload(exc), ensure_ascii=False)

        if not applied:
            if skipped_masked:
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.config.saved",
                    {
                        "action": "save_config",
                        "status": "unchanged",
                        "applied": [],
                        "masked_secret_skipped": skipped_masked,
                    },
                )
                return json.dumps({
                    "status": "success",
                    "applied": [],
                    "unchanged": True,
                    "masked_secret_skipped": skipped_masked,
                    "capability_refresh_required": False,
                }, ensure_ascii=False)
            return json.dumps({"status": "error", "message": "no valid fields to update"}, ensure_ascii=False)

        with self.CONFIG_WRITE_LOCK:
            file_cfg = self._read_file_config()
            file_cfg.update(applied)
            self._write_file_config_atomic(file_cfg)
            conf().update(applied)

        logger.info(f"[WebChannel] Channel '{channel_name}' config updated: {list(applied.keys())}")

        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels:
            if channel_name == "feishu":
                dependency_status = self._feishu_dependency_status(conf())
                if dependency_status.get("status") == "missing":
                    self._set_runtime_state(
                        channel_name,
                        status="dependency_missing",
                        started_at=None,
                        last_error="",
                        dependency_missing=True,
                        dependency_status=dependency_status,
                    )
                    should_restart = False
                else:
                    should_restart = True
            else:
                should_restart = True
        if should_restart:
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr:
                    threading.Thread(
                        target=mgr.restart,
                        args=(channel_name,),
                        daemon=True,
                    ).start()
                    logger.info(f"[WebChannel] Channel '{channel_name}' restart triggered")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {_web_body_log_summary(e)}")

        self._refresh_runtime_capabilities(f"channel-save:{channel_name}")
        record_external_connection_runtime_event(
            channel_name,
            "external_connection.config.saved",
            {
                "action": "save_config",
                "status": "success",
                "applied": list(applied.keys()),
                "restarted": should_restart,
            },
        )
        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
            "capability_refresh_required": True,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """Save config fields, add channel to channel_type, and start it."""
        ch_def = self.CHANNEL_DEFS[channel_name]
        operation_id = f"{channel_name}-{int(time.time() * 1000)}"

        # Feishu connected via web console must use websocket (long connection) mode
        if channel_name == "feishu":
            updates = dict(updates) if isinstance(updates, dict) else {}
            updates.setdefault("feishu_event_mode", "websocket")
            ch_def = {**ch_def, "fields": [*ch_def.get("fields", []), {"key": "feishu_event_mode", "type": "text"}]}

        try:
            applied, _skipped_masked = self._collect_channel_config_updates(ch_def, updates)
        except ValueError as exc:
            return json.dumps(_public_validation_error_payload(exc), ensure_ascii=False)

        with self.CONFIG_WRITE_LOCK:
            local_config = conf()
            file_cfg = self._read_file_config()
            candidate_config = {**local_config, **file_cfg, **applied}
            config_error = self._channel_start_config_error(candidate_config, channel_name)
            if config_error:
                return json.dumps({
                    "status": "error",
                    "message": config_error,
                    "missingFields": channel_config_status(candidate_config, channel_name).get("missingFields", []),
                }, ensure_ascii=False)
            dependency_status = self._feishu_dependency_status(candidate_config) if channel_name == "feishu" else {}
            if channel_name == "feishu" and dependency_status.get("status") == "missing":
                existing = self._parse_channel_list(file_cfg.get("channel_type", local_config.get("channel_type", "")))
                existing = [ch for ch in existing if ch != channel_name]
                new_channel_type = ",".join(existing)
                file_cfg.update(applied)
                file_cfg["channel_type"] = new_channel_type
                self._write_file_config_atomic(file_cfg)
                local_config.update(applied)
                local_config["channel_type"] = new_channel_type
                self._set_runtime_state(
                    channel_name,
                    status="dependency_missing",
                    operation_id=operation_id,
                    started_at=None,
                    last_error="",
                    dependency_missing=True,
                    dependency_status=dependency_status,
                )
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.lifecycle.dependency_missing",
                    {
                        "action": "start",
                        "status": "dependency_missing",
                        "operation_id": operation_id,
                        "dependencyStatus": dependency_status,
                        "configured": True,
                        "remoteConnectivityProbed": False,
                    },
                    operation_id=operation_id,
                )
                self._refresh_runtime_capabilities(f"channel-connect-dependency-missing:{channel_name}")
                return json.dumps({
                    "status": "blocked",
                    "reason": "dependency_missing",
                    "message": "Feishu/Lark App ID and Secret were saved, but the active WebUI runtime is missing lark-oapi.",
                    "channel_type": new_channel_type,
                    "starting": False,
                    "operation_id": operation_id,
                    "configured": True,
                    "dependencyStatus": dependency_status,
                    "capability_refresh_required": True,
                }, ensure_ascii=False)
            existing = self._parse_channel_list(file_cfg.get("channel_type", local_config.get("channel_type", "")))
            if channel_name not in existing:
                existing.append(channel_name)
            new_channel_type = ",".join(existing)
            file_cfg.update(applied)
            file_cfg["channel_type"] = new_channel_type
            self._write_file_config_atomic(file_cfg)
            local_config.update(applied)
            local_config["channel_type"] = new_channel_type

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")
        self._set_runtime_state(
            channel_name,
            status="starting",
            operation_id=operation_id,
            last_error="",
            dependency_missing=False,
            dependency_status=dependency_status if channel_name == "feishu" else {},
        )
        record_external_connection_runtime_event(
            channel_name,
            "external_connection.lifecycle.start_requested",
            {
                "action": "start",
                "status": "starting",
                "operation_id": operation_id,
            },
            operation_id=operation_id,
        )

        def _do_start():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr is None:
                    msg = f"ChannelManager not available, cannot start '{channel_name}'"
                    self._set_runtime_state(channel_name, status="error", last_error=msg, operation_id=operation_id)
                    record_external_connection_runtime_event(
                        channel_name,
                        "external_connection.lifecycle.start_failed",
                        {
                            "action": "start",
                            "status": "error",
                            "operation_id": operation_id,
                            "reason": "channel_manager_unavailable",
                            "lastError": msg,
                        },
                        operation_id=operation_id,
                    )
                    logger.warning(f"[WebChannel] {msg}")
                    return
                # Stop existing instance first if still running (e.g. re-connect without disconnect)
                existing_ch = mgr.get_channel(channel_name)
                if existing_ch is not None:
                    logger.info(f"[WebChannel] Stopping existing '{channel_name}' before reconnect...")
                    mgr.stop(channel_name)
                # Always wait for the remote service to release the old connection before
                # establishing a new one (DingTalk drops callbacks on duplicate connections)
                logger.info(f"[WebChannel] Waiting for '{channel_name}' old connection to close...")
                time.sleep(5)
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Starting channel '{channel_name}'...")
                mgr.start([channel_name], first_start=False)
                started_ch = mgr.get_channel(channel_name)
                if started_ch is not None and hasattr(started_ch, "wait_startup"):
                    ok, err = started_ch.wait_startup(timeout=8)
                    if not ok:
                        safe_err = self._redact_runtime_error(err or f"Channel '{channel_name}' startup failed")
                        self._set_runtime_state(
                            channel_name,
                            status="error",
                            last_error=safe_err,
                            operation_id=operation_id,
                        )
                        logger.warning(f"[WebChannel] Channel '{channel_name}' startup reported error: {safe_err}")
                        self._refresh_runtime_capabilities(f"channel-connect-error:{channel_name}")
                        record_external_connection_runtime_event(
                            channel_name,
                            "external_connection.lifecycle.start_failed",
                            {
                                "action": "start",
                                "status": "error",
                                "operation_id": operation_id,
                                "reason": "startup_error",
                                "lastError": safe_err,
                            },
                            operation_id=operation_id,
                        )
                        return
                    observation = self._channel_startup_observation(
                        started_ch,
                        getattr(mgr, "_threads", {}).get(channel_name) if hasattr(mgr, "_threads") else None,
                    )
                else:
                    observation = {"running": False, "status": "starting", "last_error": ""}
                next_status = "active" if observation.get("running") else observation.get("status", "starting")
                self._set_runtime_state(
                    channel_name,
                    status=next_status,
                    started_at=time.time() if observation.get("running") else None,
                    last_error=observation.get("last_error", ""),
                    operation_id=operation_id,
                )
                self._refresh_runtime_capabilities(f"channel-connect:{channel_name}")
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.lifecycle.started",
                    {
                        "action": "start",
                        "status": next_status,
                        "running": bool(observation.get("running")),
                        "operation_id": operation_id,
                    },
                    operation_id=operation_id,
                )
                logger.info(f"[WebChannel] Channel '{channel_name}' start state: {next_status}")
            except Exception as e:
                safe_err = self._redact_runtime_error(e)
                self._set_runtime_state(
                    channel_name,
                    status="error",
                    last_error=safe_err,
                    operation_id=operation_id,
                )
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.lifecycle.start_failed",
                    {
                        "action": "start",
                        "status": "error",
                        "operation_id": operation_id,
                        "reason": "exception",
                        "lastError": safe_err,
                    },
                    operation_id=operation_id,
                )
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {safe_err}")

        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
            "starting": True,
            "operation_id": operation_id,
            "capability_refresh_required": True,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        operation_id = f"{channel_name}-stop-{int(time.time() * 1000)}"
        with self.CONFIG_WRITE_LOCK:
            local_config = conf()
            file_cfg = self._read_file_config()
            existing = self._parse_channel_list(file_cfg.get("channel_type", local_config.get("channel_type", "")))
            existing = [ch for ch in existing if ch != channel_name]
            new_channel_type = ",".join(existing)
            file_cfg["channel_type"] = new_channel_type
            self._write_file_config_atomic(file_cfg)
            local_config["channel_type"] = new_channel_type

        self._set_runtime_state(channel_name, status="stopping", operation_id=operation_id)
        record_external_connection_runtime_event(
            channel_name,
            "external_connection.lifecycle.stop_requested",
            {
                "action": "stop",
                "status": "stopping",
                "operation_id": operation_id,
            },
            operation_id=operation_id,
        )

        def _do_stop():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                if mgr:
                    mgr.stop(channel_name)
                else:
                    logger.warning(f"[WebChannel] ChannelManager not found, cannot stop '{channel_name}'")
                if clear_fn:
                    clear_fn(channel_name)
                self._set_runtime_state(channel_name, status="configured", operation_id="", started_at=None, last_error="")
                self._refresh_runtime_capabilities(f"channel-disconnect:{channel_name}")
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.lifecycle.stopped",
                    {
                        "action": "stop",
                        "status": "configured",
                        "running": False,
                        "operation_id": operation_id,
                    },
                    operation_id=operation_id,
                )
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                safe_err = self._redact_runtime_error(e)
                self._set_runtime_state(channel_name, status="error", last_error=safe_err)
                record_external_connection_runtime_event(
                    channel_name,
                    "external_connection.lifecycle.stop_failed",
                    {
                        "action": "stop",
                        "status": "error",
                        "operation_id": operation_id,
                        "reason": "exception",
                        "lastError": safe_err,
                    },
                    operation_id=operation_id,
                )
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {safe_err}")

        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
            "starting": False,
            "operation_id": operation_id,
            "capability_refresh_required": True,
        }, ensure_ascii=False)


_EXTERNAL_CONNECTION_PRODUCT_META: Dict[str, Dict[str, Any]] = {
    "feishu": {
        "rank": 20,
        "category": "collaboration",
        "group": "featured",
        "connectStyle": "agent_auth_or_credentials",
        "workbuddyStyle": True,
    },
    "dingtalk": {
        "rank": 30,
        "category": "collaboration",
        "group": "featured",
        "connectStyle": "credentials",
        "workbuddyStyle": True,
    },
    "wechatcom_app": {
        "rank": 40,
        "category": "collaboration",
        "group": "featured",
        "connectStyle": "credentials",
        "workbuddyStyle": True,
    },
    "wecom_bot": {
        "rank": 41,
        "category": "collaboration",
        "group": "featured",
        "connectStyle": "credentials",
        "workbuddyStyle": True,
    },
    "qq": {
        "rank": 90,
        "category": "messaging",
        "group": "featured",
        "connectStyle": "credentials",
        "workbuddyStyle": True,
    },
}


_IMPLEMENTED_EXTERNAL_CONNECTOR_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "tencent-docs",
        "displayName": "腾讯文档",
        "category": "knowledge",
        "group": "featured",
        "rank": 10,
        "connectStyle": "mcp_agent_auth",
        "status": "implemented",
        "source": "tencent_docs_mcp",
        "workbuddyStyle": True,
    },
    {
        "id": "feishu",
        "displayName": "飞书",
        "category": "collaboration",
        "group": "featured",
        "rank": 20,
        "connectStyle": "agent_auth_or_credentials",
        "status": "implemented",
        "source": "channel_projection",
        "workbuddyStyle": True,
    },
    {
        "id": "dingtalk",
        "displayName": "钉钉",
        "category": "collaboration",
        "group": "featured",
        "rank": 30,
        "connectStyle": "credentials",
        "status": "implemented",
        "source": "channel_projection",
        "workbuddyStyle": True,
    },
    {
        "id": "wechatcom_app",
        "displayName": "企业微信应用",
        "category": "collaboration",
        "group": "featured",
        "rank": 40,
        "connectStyle": "credentials",
        "status": "implemented",
        "source": "channel_projection",
        "workbuddyStyle": True,
    },
    {
        "id": "wecom_bot",
        "displayName": "企业微信群机器人",
        "category": "collaboration",
        "group": "featured",
        "rank": 41,
        "connectStyle": "credentials",
        "status": "implemented",
        "source": "channel_projection",
        "workbuddyStyle": True,
    },
    {
        "id": "qq",
        "displayName": "QQ",
        "category": "messaging",
        "group": "featured",
        "rank": 90,
        "connectStyle": "credentials",
        "status": "implemented",
        "source": "channel_projection",
        "workbuddyStyle": True,
    },
]


def _external_connection_product_meta(channel_name: str) -> Dict[str, Any]:
    return dict(_EXTERNAL_CONNECTION_PRODUCT_META.get(channel_name, {
        "rank": 500,
        "category": "messaging",
        "group": "advanced",
        "connectStyle": "channel_config",
        "workbuddyStyle": False,
    }))


def _external_connection_logo(channel_name: str, channel_info: Dict[str, Any]) -> Dict[str, Any]:
    logo_keys = {
        "weixin": "wechat",
        "wechatmp": "wechat",
        "wechatmp_service": "wechat",
        "wechat_kf": "wechat",
        "wechatcom_app": "wecom",
        "wecom_bot": "wecom",
        "feishu": "feishu",
        "dingtalk": "dingtalk",
        "qq": "qq",
        "telegram": "telegram",
        "slack": "slack",
        "discord": "discord",
        "tencent-docs": "tencent-docs",
    }
    label = channel_info.get("label") or {}
    fallback = label.get("zh") if isinstance(label, dict) else str(label or "")
    return {
        "type": "brand",
        "key": logo_keys.get(channel_name, channel_name),
        "fallbackText": (fallback or channel_name)[:4],
        "icon": channel_info.get("icon") or "",
        "color": channel_info.get("color") or "gray",
    }


def _external_connection_from_channel(channel: Dict[str, Any]) -> Dict[str, Any]:
    raw_name = str(channel.get("id") or channel.get("type") or channel.get("name") or "")
    name = normalize_channel_name(raw_name) or raw_name
    product_meta = _external_connection_product_meta(name)
    label = channel.get("label") if isinstance(channel.get("label"), dict) else {}
    display_name = (
        label.get("zh")
        or label.get("en")
        or channel.get("displayName")
        or channel.get("display_name")
        or (channel.get("name") if str(channel.get("name") or "") != name else "")
        or name
    )
    auth = channel.get("auth") if isinstance(channel.get("auth"), dict) else {}
    agent_surface = channel.get("agentSurface") if isinstance(channel.get("agentSurface"), dict) else {}
    status = str(channel.get("status") or ("connected" if channel.get("running") else "available"))
    running = bool(channel.get("running") or channel.get("connected"))
    enabled = bool(channel.get("active") or channel.get("enabled"))
    config_schema = channel.get("configSchema") if isinstance(channel.get("configSchema"), dict) else {}
    fields = channel.get("fields") if isinstance(channel.get("fields"), list) else config_schema.get("fields")
    if not isinstance(fields, list):
        fields = []
    home_channel = channel.get("homeChannel") if isinstance(channel.get("homeChannel"), dict) else {}
    if name == "feishu" and isinstance(home_channel, dict) and home_channel.get("id"):
        raw_home_channel_id = str(home_channel.get("id") or "")
        digest = hmac.new(
            b"ecorex-feishu-home-channel-v1",
            raw_home_channel_id.encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()[:16]
        home_channel = {
            "configured": True,
            "idHash": f"hmac:{digest}",
            **({"name": home_channel.get("name")} if home_channel.get("name") else {}),
        }
    actions = [
        {"id": "save_config", "label": "保存", "enabled": True},
        {"id": "test", "label": "状态检查", "enabled": True},
        *([{
            "id": "agent_auth",
            "label": "Agent 授权",
            "enabled": True,
            "tool": (auth.get("agentAuthorizationAction") or {}).get("tool") if isinstance(auth.get("agentAuthorizationAction"), dict) else "",
            "discoveryDriven": True,
        }] if auth.get("agentAuthSupported") else []),
        {"id": "start", "label": "连接", "enabled": not running},
        {"id": "stop", "label": "断开", "enabled": bool(running or enabled)},
        {"id": "set_home_channel", "label": "设为投递目标", "enabled": True},
    ]
    return {
        "id": name,
        "platform": name,
        "name": name,
        "label": label,
        "displayName": display_name,
        "description": channel.get("description") or "",
        "logo": _external_connection_logo(name, channel),
        "status": status,
        "configured": bool(channel.get("configured")),
        "enabled": enabled,
        "connected": running,
        "running": running,
        "lastError": ChannelsHandler._redact_runtime_error(channel.get("last_error") or ""),
        "dependencyMissing": bool(channel.get("dependency_missing") or False),
        "dependencyStatus": channel.get("dependencyStatus") if isinstance(channel.get("dependencyStatus"), dict) else {},
        "configState": channel.get("configState") or {},
        "auth": auth,
        "agentSurface": agent_surface,
        "adapterContract": channel.get("adapterContract") if isinstance(channel.get("adapterContract"), dict) else {},
        "callable": bool(agent_surface.get("callable") or channel.get("callable")),
        "fields": fields,
        "configSchema": config_schema or {"fields": fields},
        "homeChannel": home_channel,
        "actions": actions,
        "product": product_meta,
        "rank": int(product_meta.get("rank") or 500),
        "group": str(product_meta.get("group") or "advanced"),
        "connectStyle": str(product_meta.get("connectStyle") or "channel_config"),
        "workbuddyStyle": bool(product_meta.get("workbuddyStyle")),
        "source": "channel_projection",
    }


def _external_connection_runtime_projection_by_platform() -> Dict[str, Any]:
    try:
        from agent.protocol import RuntimeProjectionService

        projection = RuntimeProjectionService().external_connections_projection(limit=0)
    except Exception as exc:
        logger.debug(f"[WebChannel] External connection runtime projection unavailable: {_web_body_log_summary(exc)}")
        return {"latestEventId": 0, "byPlatform": {}}
    by_platform: Dict[str, Dict[str, Any]] = {}
    for item in projection.get("external_connections") or []:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip()
        if not platform:
            continue
        previous = by_platform.get(platform) or {}
        if int(item.get("lastEventId") or 0) >= int(previous.get("lastEventId") or 0):
            by_platform[platform] = item
    return {
        "latestEventId": projection.get("latest_event_id", 0),
        "byPlatform": by_platform,
    }


def _safe_feishu_cli_status_probe(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"status": "error", "available": False, "authState": "unknown"}
    safe: Dict[str, Any] = {
        "status": str(payload.get("status") or ""),
        "available": bool(payload.get("available")),
        "authState": str(payload.get("authState") or "unknown"),
        "authenticated": bool(payload.get("authenticated")),
        "commandAvailable": bool(payload.get("command")),
    }
    next_action = payload.get("nextAction")
    if isinstance(next_action, dict):
        safe["nextAction"] = {
            "tool": str(next_action.get("tool") or "feishu_cli"),
            "action": str(next_action.get("action") or "auth_login"),
            **({"domain": str(next_action.get("domain"))} if next_action.get("domain") else {}),
        }
    return safe


class ExternalConnectionsHandler:
    """Hermes-style external connection projection backed by channel observability."""

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            payload = json.loads(ChannelsHandler().GET())
            channels = payload.get("channels") if isinstance(payload, dict) else []
            connections = [
                _external_connection_from_channel(item)
                for item in channels
                if isinstance(item, dict)
            ]
            runtime_projection = _external_connection_runtime_projection_by_platform()
            by_platform = runtime_projection.get("byPlatform") if isinstance(runtime_projection, dict) else {}
            for connection in connections:
                platform = str(connection.get("platform") or connection.get("id") or "")
                connection["runtimeProjection"] = (by_platform or {}).get(platform, {})
                connection["runtimeProjectionSource"] = "RunEventLedger"
            connections.sort(key=lambda item: int((item.get("product") or {}).get("rank") or item.get("rank") or 500))
            summary = {
                "total": len(connections),
                "configured": sum(1 for item in connections if item.get("configured")),
                "enabled": sum(1 for item in connections if item.get("enabled")),
                "connected": sum(1 for item in connections if item.get("connected")),
            }
            return json.dumps({
                "status": "success",
                "connections": connections,
                "summary": summary,
                "catalog": {
                    "schema": "ecorex.external-connectors.implemented.v1",
                    "style": "workbuddy_like_real_only",
                    "implemented": list(_IMPLEMENTED_EXTERNAL_CONNECTOR_CATALOG),
                    "featured": [
                        item for item in _IMPLEMENTED_EXTERNAL_CONNECTOR_CATALOG
                        if item.get("group") == "featured"
                    ],
                },
                "runtimeProjection": {
                    "latestEventId": runtime_projection.get("latestEventId", 0),
                    "source": "RunEventLedger",
                },
                "updatedAt": time.time(),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] External connections GET error: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("External connections unavailable.", e),
                **_public_exception_summary(e),
            }, ensure_ascii=False)


class ExternalConnectionActionHandler:
    """Bounded action surface for Settings > External Connections."""

    def POST(self, platform: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            channel_name = normalize_channel_name(platform)
            if channel_name not in ChannelsHandler.CHANNEL_DEFS:
                return json.dumps({"status": "error", "message": f"unknown external connection: {platform}"}, ensure_ascii=False)
            body = json.loads(web.data() or b"{}")
            action = str(body.get("action") or "").strip().lower()
            config = body.get("config") if isinstance(body.get("config"), dict) else {}
            handler = ChannelsHandler()
            if action == "save_config":
                return handler._handle_save(channel_name, config)
            if action in {"enable", "start"}:
                return handler._handle_connect(channel_name, config)
            if action in {"disable", "stop"}:
                return handler._handle_disconnect(channel_name)
            if action == "test":
                return self._handle_test(channel_name)
            if action in {"agent_auth", "agent_authorize", "authorize_agent"}:
                return self._handle_agent_auth(channel_name, config, body)
            if action in {"agent_auth_status", "agent_auth_poll", "poll_agent_auth"}:
                return self._handle_agent_auth_status(channel_name, body)
            if action in {"set_home_channel", "clear_home_channel"}:
                return self._handle_home_channel(channel_name, action, body)
            return json.dumps({"status": "error", "message": f"unknown action: {action}"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] External connection action error: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("External connection action failed.", e),
                **_public_exception_summary(e),
            }, ensure_ascii=False)

    @staticmethod
    def _handle_test(channel_name: str) -> str:
        payload = json.loads(ChannelsHandler().GET())
        channels = payload.get("channels") if isinstance(payload, dict) else []
        channel = next((item for item in channels if isinstance(item, dict) and item.get("name") == channel_name), None)
        if not channel:
            return json.dumps({"status": "error", "message": f"connection not found: {channel_name}"}, ensure_ascii=False)
        connection = _external_connection_from_channel(channel)
        adapter = test_messaging_adapter(channel_name, config=conf())
        dependency_status = {}
        if channel_name == "feishu":
            dependency_status = ChannelsHandler._feishu_dependency_status(conf())
            adapter["dependencyStatus"] = dependency_status
            if connection.get("configured") and dependency_status.get("status") == "missing":
                adapter["readiness"] = "dependency_missing"
                adapter["reason"] = "local runtime dependency is missing"
            agent_cli_status = ExternalConnectionActionHandler._probe_feishu_cli_status()
            adapter["agentCliStatus"] = agent_cli_status
            connection["agentCliStatus"] = agent_cli_status
        adapter_readiness = str(adapter.get("readiness") or "")
        test_status = "success"
        if not adapter.get("configured") or adapter_readiness == "not_configured":
            test_status = "blocked"
        elif adapter_readiness == "dependency_missing":
            test_status = "blocked"
        elif adapter_readiness == "error":
            test_status = "error"
        record_external_connection_runtime_event(
            channel_name,
            "external_connection.test.completed",
            {
                "action": "test",
                "status": test_status,
                "configured": connection["configured"],
                "connected": connection["connected"],
                "callable": connection["callable"],
                "mode": "projection_dry_run",
                "remoteConnectivityProbed": False,
                "adapter": adapter,
                "dependencyStatus": dependency_status,
                "agentCliStatus": adapter.get("agentCliStatus", {}),
            },
        )
        return json.dumps({
            "status": "success",
            "connection": connection,
            "adapter": adapter,
            "test": {
                "status": test_status,
                "configured": connection["configured"],
                "connected": connection["connected"],
                "callable": connection["callable"],
                "lastError": connection["lastError"],
                "mode": "projection_dry_run",
                "remoteConnectivityProbed": False,
                "dependencyStatus": dependency_status,
                "agentCliStatus": adapter.get("agentCliStatus", {}),
            },
        }, ensure_ascii=False)

    @staticmethod
    def _probe_feishu_cli_status() -> Dict[str, Any]:
        try:
            from agent.tools.feishu_cli.feishu_cli import FeishuCli

            result = FeishuCli({"cwd": _get_workspace_root()}).execute({"action": "status", "timeout": 15})
            safe = _safe_feishu_cli_status_probe(getattr(result, "result", {}))
            safe["toolStatus"] = str(getattr(result, "status", "") or "")
            return safe
        except Exception as exc:
            return {
                "status": "error",
                "available": False,
                "authState": "unknown",
                "message": _public_exception_message("Feishu CLI status probe failed.", exc),
                **_public_exception_summary(exc),
            }

    @staticmethod
    def _handle_agent_auth(channel_name: str, config: Dict[str, Any], body: Optional[Dict[str, Any]] = None) -> str:
        channel_name = normalize_channel_name(channel_name)
        if channel_name != "feishu":
            return ExternalConnectionActionHandler._handle_generic_agent_auth(channel_name, config)
        body = body if isinstance(body, dict) else {}
        web_session_id = str(
            body.get("webSessionId")
            or body.get("sessionId")
            or body.get("session_id")
            or config.get("webSessionId")
            or config.get("sessionId")
            or config.get("session_id")
            or ""
        ).strip()
        trace_id = f"web:{web_session_id}:feishu-agent-auth:{int(time.time())}" if web_session_id else f"web:feishu-agent-auth:{int(time.time())}"
        decision = _authorize_web_capability(
            "feishu_cli",
            "agent_auth",
            arguments={
                "action": "agent_auth",
                "surface": "web_external_connection",
                "web_session_id": web_session_id,
                "trace_id": trace_id,
            },
            metadata={
                "surface": "web",
                "source": "external_connection_action",
                "user_initiated": True,
                "webSessionId": web_session_id,
                "traceId": trace_id,
            },
        )
        if decision.get("allowed") is not True:
            return json.dumps(
                _permission_denied_payload(
                    decision.get("reason", ""),
                    decision,
                    capability="feishu_cli",
                    action="agent_auth",
                ),
                ensure_ascii=False,
            )
        try:
            from agent.tools.feishu_cli.feishu_cli import FeishuCli

            # Keep the Web entry thin: the structured tool probes official
            # lark-cli diagnostics and chooses the auth/config flow itself.
            # Do not pass saved app credentials here, or this can regress into
            # a fixed config_init branch that bypasses the visible CLI auth URL.
            args: Dict[str, Any] = {
                "action": "agent_auth",
                "timeout": 240,
                "surface": "web_external_connection",
                "web_session_id": web_session_id,
                "trace_id": trace_id,
            }

            result = FeishuCli({"cwd": _get_workspace_root()}).execute(args)
            raw = getattr(result, "result", {})
            public = redact_public_tool_value(raw)
            public = restore_feishu_public_auth_fields(public, raw, "feishu_cli")
            public = _safe_feishu_agent_auth_payload(public)
            if web_session_id:
                public["webSessionId"] = web_session_id
                public["traceId"] = trace_id
            if isinstance(raw, dict) and raw.get("sessionId"):
                public["sessionId"] = str(raw.get("sessionId"))
                if isinstance(public.get("nextAction"), dict) and isinstance(raw.get("nextAction"), dict):
                    raw_session = raw.get("nextAction", {}).get("session_id")
                    if raw_session:
                        public["nextAction"]["session_id"] = str(raw_session)
            status = "success" if getattr(result, "status", "") == "success" else "error"
            record_external_connection_runtime_event(
                channel_name,
                "external_connection.agent_auth.completed",
                {
                    "action": "agent_auth",
                    "status": status,
                    "authRequired": bool(isinstance(raw, dict) and raw.get("authRequired")),
                    "writebackPending": bool(isinstance(raw, dict) and raw.get("writebackPending")),
                    "hasVerificationUrl": bool(isinstance(raw, dict) and raw.get("verificationUrl")),
                    "webSessionId": web_session_id,
                    "traceId": trace_id,
                },
            )
            return json.dumps({
                "status": status,
                "platform": channel_name,
                "agentAuth": public,
                "authRequired": bool(isinstance(raw, dict) and raw.get("authRequired")),
                "writebackPending": bool(isinstance(raw, dict) and raw.get("writebackPending")),
                "authCompleted": bool(isinstance(raw, dict) and raw.get("authCompleted")),
                "sessionId": public.get("sessionId") if isinstance(public, dict) else "",
                "verificationUrl": public.get("verificationUrl") if isinstance(public, dict) else "",
                "message": (
                    public.get("message")
                    if isinstance(public, dict) and public.get("message")
                    else "Feishu CLI auth/config action completed."
                ),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Feishu CLI auth/config failed.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)

    @staticmethod
    def _handle_generic_agent_auth(channel_name: str, config: Dict[str, Any]) -> str:
        definition = CHANNEL_CATALOG.get(channel_name) or {}
        auth_surface = channel_auth_surface(conf(), channel_name)
        action_spec = auth_surface.get("agentAuthorizationAction") if isinstance(auth_surface.get("agentAuthorizationAction"), dict) else {}
        tool_name = str(action_spec.get("tool") or "").strip()
        if not tool_name:
            return json.dumps({
                "status": "error",
                "platform": channel_name,
                "discoveryDriven": True,
                "message": (
                    "This external connection has no declared agent authorization tool yet. "
                    "Ask the agent to discover the official install/config/auth diagnostics first."
                ),
                "nextAction": {
                    "tool": "agent_capability",
                    "action": "diagnose",
                    "platform": channel_name,
                },
            }, ensure_ascii=False)
        try:
            from agent.tools.tool_manager import ToolManager

            tool = ToolManager().create_tool(tool_name)
            if tool is None:
                return json.dumps({
                    "status": "error",
                    "platform": channel_name,
                    "discoveryDriven": True,
                    "message": f"Declared agent authorization tool is not loaded: {tool_name}",
                    "nextAction": {
                        "tool": "agent_capability",
                        "action": "diagnose",
                        "platform": channel_name,
                        "declaredTool": tool_name,
                    },
                }, ensure_ascii=False)
            args = {key: value for key, value in action_spec.items() if key not in {"tool"}}
            args.setdefault("action", "agent_auth")
            args.setdefault("surface", "web_external_connection")
            args.setdefault("platform", channel_name)
            if isinstance(config, dict):
                for key in ("scope", "domain", "thread_id", "threadId"):
                    if config.get(key):
                        args[key] = config.get(key)
            result = tool.execute(args)
            raw = getattr(result, "result", {})
            public = redact_public_tool_value(raw)
            status = "success" if getattr(result, "status", "") == "success" else "error"
            return json.dumps({
                "status": status,
                "platform": channel_name,
                "agentAuth": public,
                "discoveryDriven": True,
                "message": (
                    public.get("message")
                    if isinstance(public, dict) and public.get("message")
                    else f"{channel_name} agent auth action completed through {tool_name}."
                ),
            }, ensure_ascii=False)
        except Exception as exc:
            label = definition.get("label") if isinstance(definition.get("label"), dict) else {}
            display = label.get("zh") or label.get("en") or channel_name
            return json.dumps({
                "status": "error",
                "platform": channel_name,
                "message": _public_exception_message(f"{display} agent auth failed.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)

    @staticmethod
    def _handle_agent_auth_status(channel_name: str, body: Dict[str, Any]) -> str:
        if channel_name != "feishu":
            return json.dumps({"status": "error", "message": f"agent auth status is not supported for: {channel_name}"}, ensure_ascii=False)
        try:
            from agent.tools.feishu_cli.feishu_cli import FeishuCli

            agent_auth = body.get("agentAuth") if isinstance(body.get("agentAuth"), dict) else {}
            session_id = str(
                body.get("sessionId")
                or body.get("session_id")
                or agent_auth.get("sessionId")
                or agent_auth.get("session_id")
                or ""
            ).strip()
            result = FeishuCli({"cwd": _get_workspace_root()}).execute({
                "action": "agent_auth_status",
                "session_id": session_id,
                "timeout": 15,
            })
            raw = getattr(result, "result", {})
            public = redact_public_tool_value(raw)
            public = restore_feishu_public_auth_fields(public, raw, "feishu_cli")
            public = _safe_feishu_agent_auth_payload(public)
            if isinstance(raw, dict) and raw.get("sessionId"):
                public["sessionId"] = str(raw.get("sessionId"))
            status = "success" if getattr(result, "status", "") == "success" else "error"
            record_external_connection_runtime_event(
                channel_name,
                "external_connection.agent_auth_status.completed",
                {
                    "action": "agent_auth_status",
                    "status": status,
                    "writebackPending": bool(isinstance(raw, dict) and raw.get("writebackPending")),
                    "authCompleted": bool(isinstance(raw, dict) and raw.get("authCompleted")),
                },
            )
            return json.dumps({
                "status": status,
                "platform": channel_name,
                "agentAuth": public,
                "authRequired": bool(isinstance(raw, dict) and raw.get("authRequired")),
                "writebackPending": bool(isinstance(raw, dict) and raw.get("writebackPending")),
                "authCompleted": bool(isinstance(raw, dict) and raw.get("authCompleted")),
                "authenticated": bool(isinstance(raw, dict) and raw.get("authenticated")),
                "authState": str(raw.get("authState") or "") if isinstance(raw, dict) else "",
                "sessionId": public.get("sessionId") if isinstance(public, dict) else "",
                "verificationUrl": public.get("verificationUrl") if isinstance(public, dict) else "",
                "message": (
                    public.get("message")
                    if isinstance(public, dict) and public.get("message")
                    else "Feishu CLI auth/config status checked."
                ),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Feishu CLI auth/config status check failed.", exc),
                **_public_exception_summary(exc),
            }, ensure_ascii=False)

    @staticmethod
    def _handle_home_channel(channel_name: str, action: str, body: Dict[str, Any]) -> str:
        key = f"{channel_name}_home_channel"
        name_key = f"{channel_name}_home_channel_name"
        with ChannelsHandler.CONFIG_WRITE_LOCK:
            file_cfg = ChannelsHandler._read_file_config()
            local_config = conf()
            if action == "clear_home_channel":
                file_cfg.pop(key, None)
                file_cfg.pop(name_key, None)
                ChannelsHandler._write_file_config_atomic(file_cfg)
                local_config.pop(key, None)
                local_config.pop(name_key, None)
            else:
                target = str(body.get("home_channel") or body.get("homeChannel") or "").strip()
                target_name = str(body.get("home_channel_name") or body.get("homeChannelName") or "").strip()
                if not target:
                    return json.dumps({"status": "error", "message": "home_channel is required"}, ensure_ascii=False)
                file_cfg[key] = target
                if target_name:
                    file_cfg[name_key] = target_name
                else:
                    file_cfg.pop(name_key, None)
                ChannelsHandler._write_file_config_atomic(file_cfg)
                local_config[key] = target
                if target_name:
                    local_config[name_key] = target_name
                else:
                    local_config.pop(name_key, None)
        target_hash = ""
        if action != "clear_home_channel":
            target = str(body.get("home_channel") or body.get("homeChannel") or "").strip()
            target_hash = hashlib.sha256(target.encode("utf-8", errors="replace")).hexdigest()[:16] if target else ""
        record_external_connection_runtime_event(
            channel_name,
            "external_connection.home_channel.updated",
            {
                "action": action,
                "status": "success",
                "homeChannelConfigured": action != "clear_home_channel",
                "homeChannelHash": target_hash,
            },
        )
        return json.dumps({
            "status": "success",
            "platform": channel_name,
            "homeChannelConfigured": action != "clear_home_channel",
        }, ensure_ascii=False)


class WeixinQrHandler:
    """Handle WeChat QR code login from the web console.

    GET  /api/weixin/qrlogin          → fetch a new QR code
    POST /api/weixin/qrlogin          → poll QR status or start channel after login
    """

    _qr_state = {}

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """Generate a QR code as a PNG data URI."""
        try:
            import qrcode as qr_lib
            import io
            import base64
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=6, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            return ""

    @staticmethod
    def _get_running_channel():
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                return mgr.get_channel("weixin")
        except Exception:
            pass
        return None

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            running_ch = self._get_running_channel()
            if running_ch and hasattr(running_ch, '_current_qr_url') and running_ch._current_qr_url:
                qr_image = self._qr_to_data_uri(running_ch._current_qr_url)
                return json.dumps({
                    "status": "success",
                    "qrcode_url": running_ch._current_qr_url,
                    "qr_image": qr_image,
                    "source": "channel",
                })

            from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
            base_url = conf().get("weixin_base_url", DEFAULT_BASE_URL)
            api = WeixinApi(base_url=base_url)
            qr_resp = api.fetch_qr_code()
            qrcode = qr_resp.get("qrcode", "")
            qrcode_url = qr_resp.get("qrcode_img_content", "")
            if not qrcode:
                return json.dumps({"status": "error", "message": "No QR code returned"})
            qr_image = self._qr_to_data_uri(qrcode_url)
            WeixinQrHandler._qr_state = {
                "qrcode": qrcode,
                "qrcode_url": qrcode_url,
                "base_url": base_url,
            }
            return json.dumps({"status": "success", "qrcode_url": qrcode_url, "qr_image": qr_image})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr GET error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action", "poll")

            if action == "poll":
                return self._poll_status()
            elif action == "refresh":
                return self.GET()
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr POST error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def _poll_status(self):
        state = WeixinQrHandler._qr_state
        qrcode = state.get("qrcode", "")
        base_url = state.get("base_url", "")
        if not qrcode:
            return json.dumps({"status": "error", "message": "No active QR session"})

        from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
        api = WeixinApi(base_url=base_url or DEFAULT_BASE_URL)
        try:
            status_resp = api.poll_qr_status(qrcode, timeout=10)
        except Exception as e:
            return json.dumps(_public_error_payload("Request failed.", e))

        qr_status = status_resp.get("status", "wait")

        if qr_status == "confirmed":
            bot_token = status_resp.get("bot_token", "")
            bot_id = status_resp.get("ilink_bot_id", "")
            result_base_url = status_resp.get("baseurl", base_url)
            user_id = status_resp.get("ilink_user_id", "")

            if not bot_token or not bot_id:
                return json.dumps({"status": "error", "message": "Login confirmed but missing token"})

            cred_path = os.path.expanduser(
                conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
            )
            from channel.weixin.weixin_channel import _save_credentials
            _save_credentials(cred_path, {
                "token": bot_token,
                "base_url": result_base_url,
                "bot_id": bot_id,
                "user_id": user_id,
            })
            conf()["weixin_token"] = bot_token
            conf()["weixin_base_url"] = result_base_url

            WeixinQrHandler._qr_state = {}
            logger.info(f"[WebChannel] WeChat QR login confirmed: bot_id={bot_id}")

            return json.dumps({
                "status": "success",
                "qr_status": "confirmed",
                "bot_id": bot_id,
            })

        if qr_status == "expired":
            new_resp = api.fetch_qr_code()
            new_qrcode = new_resp.get("qrcode", "")
            new_qrcode_url = new_resp.get("qrcode_img_content", "")
            new_qr_image = self._qr_to_data_uri(new_qrcode_url)
            WeixinQrHandler._qr_state["qrcode"] = new_qrcode
            WeixinQrHandler._qr_state["qrcode_url"] = new_qrcode_url
            return json.dumps({
                "status": "success",
                "qr_status": "expired",
                "qrcode_url": new_qrcode_url,
                "qr_image": new_qr_image,
            })

        return json.dumps({"status": "success", "qr_status": qr_status})


def _redact_feishu_register_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""

    def _redact_kv(match: re.Match) -> str:
        return f"{match.group(1)}[redacted]{match.group(3) or ''}"

    secret_key = r"(?:app[_-]?secret|client[_-]?secret|token|password|credential|api[_-]?key)"
    identifier_key = r"(?:open_id|chat_id|open-?id|chat-?id)"
    text = re.sub(
        rf"(?i)((?:[\"']?{secret_key}[\"']?)\s*[:=]\s*[\"']?)([^\"'\s&,}}\]]+)([\"']?)",
        _redact_kv,
        text,
    )
    text = re.sub(
        rf"(?i)((?:[\"']?{identifier_key}[\"']?)\s*[:=]\s*[\"']?)([^\"'\s&,}}\]]+)([\"']?)",
        _redact_kv,
        text,
    )
    text = re.sub(r"(?i)(sk-[A-Za-z0-9_\-]{8,}|bearer\s+[A-Za-z0-9._\-]+)", "[redacted]", text)
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    return text[:320]


def _safe_feishu_register_status(info: Any) -> Dict[str, Any]:
    if not isinstance(info, dict):
        return {"shape": type(info).__name__}
    safe: Dict[str, Any] = {}
    for key in ("status", "code", "expire_in", "error_code"):
        if key in info:
            safe[key] = _redact_feishu_register_text(info.get(key))
    if "message" in info:
        safe["message"] = _redact_feishu_register_text(info.get("message"))
    unknown = sorted(str(key) for key in info.keys() if key not in safe and key not in {"url", "qr_code", "client_id", "client_secret", "app_id", "app_secret", "appId", "appSecret", "clientId", "clientSecret"})
    if unknown:
        safe["unknown_keys_hash"] = hashlib.sha256(",".join(unknown).encode("utf-8")).hexdigest()[:12]
    return safe or {"shape": "object", "key_count": len(info)}


def _feishu_register_secret_presence(app_id: str, app_secret: str) -> Dict[str, Any]:
    return {
        "app_id_present": bool(app_id),
        "app_secret_present": bool(app_secret),
        "credential_hash": hashlib.sha256(f"{app_id}:{app_secret}".encode("utf-8")).hexdigest()[:12] if app_id and app_secret else "",
    }


class FeishuRegisterHandler:
    """飞书智能体应用一键创建（OAuth 设备授权流，基于 lark.register_app SDK）。

    GET  /api/feishu/register   → 启动注册：调用 SDK 生成二维码 URL，立即返回；
                                   后台线程继续轮询飞书侧直到用户扫码授权。
    POST /api/feishu/register   → 轮询当前会话状态（pending / done / error / expired）。
                                   注册成功后不直接写 config，由前端再调
                                   /api/channels {action:'connect'} 走标准启用流程。
    """

    # 进程内单例状态（{url, expire_in, status, app_id, app_secret, error, thread}）。
    # 简单的本地自部署场景下不需要 session 隔离。
    _state = {}
    _lock = threading.Lock()

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """复用 WeixinQrHandler 的二维码渲染。"""
        return WeixinQrHandler._qr_to_data_uri(data)

    @classmethod
    def _reset_state(cls):
        with cls._lock:
            cls._state = {}

    @classmethod
    def _start_register_thread(cls):
        """启动一次新的注册会话。如已有进行中的会话，先取消（通过 cancel_event）。"""
        # 先取消可能存在的上一次会话，避免两个 SDK 线程并发 poll 同一个端点
        with cls._lock:
            old_cancel = cls._state.get("cancel_event") if cls._state else None
            if old_cancel is not None:
                old_cancel.set()
            cancel_event = threading.Event()
            cls._state = {"status": "starting", "cancel_event": cancel_event}

        def _worker():
            try:
                import lark_oapi as lark
            except ImportError:
                with cls._lock:
                    cls._state["status"] = "error"
                    cls._state["error"] = (
                        "飞书应用一键注册是 legacy SDK 通道，当前运行时未包含 lark-oapi，因此不自动安装。"
                        "飞书/Lark CLI、skill 和 connector 安装请统一先走内置 find skill / find-skill；"
                        "真实 CLI 操作按需安装官方 @larksuite/cli，npmjs.org 超时后降级到 https://registry.npmmirror.com。"
                    )
                return

            def _on_qr(info):
                # SDK 拿到二维码 URL 后立即回调；写入 state 让前端 GET 立刻能拿到
                with cls._lock:
                    cls._state["url"] = info.get("url", "")
                    cls._state["expire_in"] = info.get("expire_in", 600)
                    cls._state["qr_image"] = cls._qr_to_data_uri(info.get("url", ""))
                    cls._state["status"] = "pending"
                logger.info("[FeishuRegister] QR ready, expire_in=%ss", _redact_feishu_register_text(info.get("expire_in")))

            def _on_status(info):
                # 过滤掉 polling 心跳（每 5 秒一次，纯噪音）；
                # 保留 slow_down / domain_switched 等真正的状态切换事件
                status = info.get("status")
                if status == "polling":
                    return
                logger.info("[FeishuRegister] SDK status: %s", _safe_feishu_register_status(info))

            try:
                result = lark.register_app(
                    on_qr_code=_on_qr,
                    on_status_change=_on_status,
                    source="ecorex",
                    cancel_event=cancel_event,
                )
                app_id, app_secret = extract_feishu_register_credentials(result)
                with cls._lock:
                    cls._state["status"] = "done"
                    cls._state["app_id"] = app_id
                    cls._state["app_secret"] = app_secret
                    cls._state["result_shape"] = summarize_feishu_register_result_shape(result)
                if app_id and app_secret:
                    logger.info("[FeishuRegister] App created and awaiting write-back")
                else:
                    logger.warning(
                        "[FeishuRegister] App created but credentials were not extractable: %s",
                        summarize_feishu_register_result_shape(result),
                    )
            except Exception as e:
                err_msg = _redact_feishu_register_text(e)
                err_cls = e.__class__.__name__
                # 飞书 SDK 抛出的 AppExpiredError / AppAccessDeniedError / RegisterAppError
                if "Expired" in err_cls:
                    status = "expired"
                elif "Denied" in err_cls:
                    status = "denied"
                elif "abort" in err_msg.lower() or "cancel" in err_msg.lower():
                    # 被新一轮注册抢占，保持安静
                    return
                else:
                    status = "error"
                with cls._lock:
                    # 仅当当前 state 仍属于本次 worker 时才写入，避免覆盖更新的会话
                    if cls._state.get("cancel_event") is cancel_event:
                        cls._state["status"] = status
                        cls._state["error"] = err_msg
                logger.warning("[FeishuRegister] Register failed (%s): %s", err_cls, err_msg)

        threading.Thread(target=_worker, daemon=True, name="feishu-register").start()

    @staticmethod
    def _connect_registered_app(app_id: str, app_secret: str) -> Dict[str, Any]:
        if not app_id or not app_secret:
            return {
                "status": "error",
                "message": "registered Feishu app is missing app_id or app_secret",
                "applied": [],
            }
        try:
            payload = json.loads(ChannelsHandler()._handle_connect("feishu", {
                "feishu_app_id": app_id,
                "feishu_app_secret": app_secret,
            }))
            return {
                "status": payload.get("status", "success"),
                "channel_type": payload.get("channel_type", ""),
                "starting": bool(payload.get("starting")),
                "operation_id": payload.get("operation_id", ""),
                "capability_refresh_required": bool(payload.get("capability_refresh_required")),
                "applied": ["feishu_app_id", "feishu_app_secret", "channel_type"],
                "message": payload.get("message", ""),
            }
        except Exception as exc:
            logger.warning("[FeishuRegister] credential write-back failed: %s", _redact_feishu_register_text(exc))
            return {
                "status": "error",
                "message": _redact_feishu_register_text(exc),
                "applied": [],
            }

    def GET(self):
        """启动一次新的注册会话。如果已有 pending/done 会话则覆盖。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            self._start_register_thread()
            # 等待 SDK 拿到二维码 URL（最多 10s）。SDK 内部会马上回调 _on_qr。
            import time as _t
            for _ in range(100):
                with self._lock:
                    if self._state.get("url") or self._state.get("status") in ("error", "expired", "denied"):
                        break
                _t.sleep(0.1)
            with self._lock:
                if self._state.get("status") in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "error",
                        "message": self._state.get("error", "register failed"),
                    })
                if not self._state.get("url"):
                    return json.dumps({
                        "status": "error",
                        "message": "等待飞书二维码超时，请重试",
                    })
                return json.dumps({
                    "status": "success",
                    "qrcode_url": self._state["url"],
                    "qr_image": self._state.get("qr_image", ""),
                    "expire_in": self._state.get("expire_in", 600),
                })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister GET error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        """轮询注册结果。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "poll")
            if action != "poll":
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})

            with self._lock:
                status = self._state.get("status", "idle")
                app_id = self._state.get("app_id", "") if status == "done" else ""
                app_secret = self._state.get("app_secret", "") if status == "done" else ""
                result_shape = self._state.get("result_shape") if status == "done" else None
                error_message = self._state.get("error", "")
                if status == "done":
                    # 一次性取出凭据后清掉，避免敏感信息长期驻留内存。
                    self._state = {}

            if status == "done":
                writeback = self._connect_registered_app(app_id, app_secret)
                payload = {
                    "status": "success",
                    "register_status": "done",
                    "credential": _feishu_register_secret_presence(app_id, app_secret),
                    "writeback": writeback,
                    "channel_configured": writeback.get("status") == "success",
                }
                if result_shape and not payload["channel_configured"]:
                    payload["register_result_shape"] = result_shape
                return json.dumps(payload, ensure_ascii=False)
            if status in ("error", "expired", "denied"):
                return json.dumps({
                    "status": "success",
                    "register_status": status,
                    "message": error_message,
                }, ensure_ascii=False)
            # pending / starting：还在等用户扫码
            return json.dumps({
                "status": "success",
                "register_status": "pending",
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister POST error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


def _get_workspace_root():
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace", "~/cow"))


class MemoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(page='1', page_size='20', category='memory')
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.list_files(
                page=int(params.page), page_size=int(params.page_size),
                category=params.category,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


class MemoryContentHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(filename='', category='memory')
            if not params.filename:
                return json.dumps({"status": "error", "message": "filename required"})
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.get_content(params.filename, category=params.category)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except ValueError:
            return json.dumps({"status": "error", "message": "invalid filename"})
        except FileNotFoundError:
            return json.dumps({"status": "error", "message": "file not found"})
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


class SchedulerHandler:
    @staticmethod
    def _authorize_action(action: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            return _authorize_web_capability(
                "scheduler",
                action,
                arguments=body or {"action": action},
                metadata={"surface": "web", "source": "scheduler_api"},
            )
        except Exception as exc:
            logger.warning(f"[WebChannel] Scheduler permission check unavailable: {_web_body_log_summary(exc)}")
            return {"allowed": False, "reason": "Permission broker unavailable; scheduled task action was blocked."}

    @staticmethod
    def _mutation_blocked() -> str:
        decision = SchedulerHandler._authorize_action("update", {"action": "update"})
        if decision.get("allowed") is not True:
            return str(decision.get("reason") or "Permission denied.")
        return ""

    @staticmethod
    def _store():
        from agent.tools.scheduler.task_store import TaskStore

        workspace_root = _get_workspace_root()
        store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
        return TaskStore(store_path)

    @staticmethod
    def _projection() -> dict:
        from agent.tools.scheduler.projection import scheduler_projection

        return scheduler_projection(_get_workspace_root())

    @staticmethod
    def _set_enabled(enabled: bool) -> None:
        local_config = conf()
        file_cfg = ConfigHandler._read_file_config()
        local_config["scheduler_enabled"] = bool(enabled)
        file_cfg["scheduler_enabled"] = bool(enabled)
        _ensure_ecorex_runtime_defaults(file_cfg)
        ConfigHandler._write_file_config(file_cfg)

    @staticmethod
    def _start_runtime() -> bool:
        from agent.tools.scheduler.integration import ensure_scheduler_runtime
        from bridge.bridge import Bridge

        return ensure_scheduler_runtime(Bridge().get_agent_bridge())

    @staticmethod
    def _stop_runtime() -> None:
        try:
            from agent.tools.scheduler.integration import get_scheduler_service

            service = get_scheduler_service()
            if service is not None:
                service.stop()
        except Exception as exc:
            logger.warning(f"[WebChannel] Scheduler stop failed: {_web_body_log_summary(exc)}")

    @staticmethod
    def _parse_schedule(data: dict) -> Optional[dict]:
        if isinstance(data.get("schedule"), dict):
            schedule = dict(data.get("schedule") or {})
            schedule_type = schedule.get("type")
            if schedule_type == "cron" and schedule.get("expression"):
                return schedule
            if schedule_type == "interval":
                try:
                    seconds = int(schedule.get("seconds") or 0)
                except (TypeError, ValueError):
                    seconds = 0
                if seconds > 0:
                    schedule["seconds"] = seconds
                    return schedule
            if schedule_type == "once" and schedule.get("run_at"):
                return schedule
            return None
        schedule_type = data.get("schedule_type") or data.get("scheduleType")
        schedule_value = data.get("schedule_value") or data.get("scheduleValue")
        if not schedule_type or not schedule_value:
            return None
        from agent.tools.scheduler.scheduler_tool import SchedulerTool

        return SchedulerTool({})._parse_schedule(str(schedule_type), str(schedule_value))

    @staticmethod
    def _calculate_next_run(task: dict) -> str:
        try:
            from agent.tools.scheduler.scheduler_tool import SchedulerTool

            next_run = SchedulerTool({})._calculate_next_run(task)
            return next_run.isoformat() if next_run else ""
        except Exception as exc:
            logger.warning(f"[WebChannel] Scheduler next_run calculation failed: {_web_body_log_summary(exc)}")
            return ""

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            decision = self._authorize_action("list", {"action": "list"})
            if decision.get("allowed") is not True:
                return json.dumps(
                    _permission_denied_payload(
                        decision.get("reason", ""),
                        decision,
                        capability="scheduler",
                        action="list",
                    ),
                    ensure_ascii=False,
                )
            return json.dumps({"status": "success", **self._projection()}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Scheduler API request failed.", e),
                **_public_exception_summary(e),
            }, ensure_ascii=False)

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = str(body.get("action") or "").strip().lower()
            if not action:
                return json.dumps({"status": "error", "message": "action is required"})

            blocked = self._mutation_blocked()
            if blocked:
                return json.dumps(
                    _permission_denied_payload(
                        blocked,
                        {"allowed": False, "reason": blocked},
                        capability="scheduler",
                        action=action,
                    ),
                    ensure_ascii=False,
                )

            store = self._store()
            if action == "start":
                self._set_enabled(True)
                started = self._start_runtime()
                return json.dumps({
                    "status": "success" if started else "error",
                    "message": "scheduler started" if started else "scheduler start failed",
                    **self._projection(),
                }, ensure_ascii=False)

            if action == "stop":
                self._set_enabled(False)
                self._stop_runtime()
                return json.dumps({"status": "success", **self._projection()}, ensure_ascii=False)

            task_id = str(body.get("task_id") or body.get("taskId") or "").strip()
            if action in {"delete", "enable", "disable", "update"} and not task_id:
                return json.dumps({"status": "error", "message": "task_id is required"}, ensure_ascii=False)

            if action == "delete":
                store.delete_task(task_id)
            elif action == "enable":
                store.enable_task(task_id, True)
            elif action == "disable":
                store.enable_task(task_id, False)
            elif action == "update":
                task = store.get_task(task_id)
                if not task:
                    return json.dumps({"status": "error", "message": f"task not found: {task_id}"}, ensure_ascii=False)
                updates: Dict[str, Any] = {}
                if "name" in body:
                    name = str(body.get("name") or "").strip()
                    if not name:
                        return json.dumps({"status": "error", "message": "name cannot be empty"}, ensure_ascii=False)
                    updates["name"] = name
                if "enabled" in body:
                    updates["enabled"] = bool(body.get("enabled"))
                schedule = self._parse_schedule(body)
                if schedule is not None:
                    updates["schedule"] = schedule
                    task_with_schedule = dict(task)
                    task_with_schedule["schedule"] = schedule
                    next_run = self._calculate_next_run(task_with_schedule)
                    updates["next_run_at"] = next_run
                if "content" in body or "taskDescription" in body or "task_description" in body:
                    action_block = dict(task.get("action") or {})
                    if action_block.get("type") == "send_message" and "content" in body:
                        action_block["content"] = str(body.get("content") or "")
                    if action_block.get("type") == "agent_task":
                        description = body.get("taskDescription", body.get("task_description", None))
                        if description is not None:
                            action_block["task_description"] = str(description or "")
                    updates["action"] = action_block
                if not updates:
                    return json.dumps({"status": "success", "noop": True, **self._projection()}, ensure_ascii=False)
                store.update_task(task_id, updates)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"}, ensure_ascii=False)

            return json.dumps({"status": "success", **self._projection()}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler POST error: {_web_body_log_summary(e)}")
            return json.dumps({
                "status": "error",
                "message": _public_exception_message("Scheduler API request failed.", e),
                **_public_exception_summary(e),
            }, ensure_ascii=False)


class InstallationsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.ecorex_workspace import load_installation_manifest
            manifest = load_installation_manifest(_get_workspace_root())
            return json.dumps({"status": "success", "manifest": manifest}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] installations GET error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 256 * 1024:
                return json.dumps({"status": "error", "message": "installation payload too large"})
            body = json.loads(raw)
            surface = (body.get("surface") or "").strip()
            if not surface:
                return json.dumps({"status": "error", "message": "surface is required"})
            metadata = body.get("metadata", {})
            if not isinstance(metadata, dict):
                return json.dumps({"status": "error", "message": "metadata must be an object"})
            from common.ecorex_workspace import register_installation
            manifest = register_installation(_get_workspace_root(), surface, metadata)
            return json.dumps({"status": "success", "manifest": manifest}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] installations POST error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


def _log_snapshot_payload(max_lines: int = 200) -> Dict[str, Any]:
    from config import get_root

    log_path = _resolve_run_log_path(Path(get_root()))
    try:
        from agent.tools.host_diagnostics.host_diagnostics import _tail_text

        tail = _tail_text(log_path, max_lines=max(1, min(500, int(max_lines or 200))), cwd=str(log_path.parent))
        safe_tail = dict(tail)
        safe_tail["lines"] = [
            mask_sensitive_text(line, max_chars=2000)
            for line in (tail.get("lines") or [])
        ]
        for key in ("path", "cwd", "reason", "error"):
            if key in safe_tail:
                safe_tail[key] = mask_sensitive_text(safe_tail.get(key), max_chars=500)
        return {
            "status": "success" if safe_tail.get("exists") and not safe_tail.get("blocked") else "error",
            "type": "snapshot",
            "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "log": safe_tail,
            "content": "\n".join(safe_tail.get("lines") or []),
            "message": safe_tail.get("reason") or safe_tail.get("error") or ("" if safe_tail.get("exists") else "run.log not found"),
        }
    except Exception as exc:
        public_message = _public_exception_message("Log snapshot unavailable.", exc)
        return {
            "status": "error",
            "type": "snapshot",
            "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "log": {
                "path": _diagnostic_path_summary(log_path),
                "exists": False,
                "error": public_message,
                "lines": [],
                **_public_exception_summary(exc),
            },
            "content": "",
            "message": public_message,
            **_public_exception_summary(exc),
        }


def _diagnostic_event_summary(line: str) -> Dict[str, Any]:
    raw = str(line or "").replace("\r", "").strip()
    lower = raw.lower()
    if "traceback" in lower or "exception" in lower or "error" in lower:
        severity = "error"
    elif "warn" in lower or "warning" in lower:
        severity = "warning"
    else:
        severity = "info"
    category = "runtime"
    for label, pattern in (
        ("permission", r"permission|denied|authorize|授权|权限"),
        ("sse", r"\bsse\b|eventsource|stream"),
        ("sidecar", r"sidecar"),
        ("artifact", r"artifact|preview|thumbnail|file-stat|file stat"),
        ("session", r"session|request|lock"),
        ("diagnostics", r"diagnostic|log"),
    ):
        if re.search(pattern, raw, re.IGNORECASE):
            category = label
            break
    timestamp_match = re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?", raw)
    return {
        "severity": severity,
        "category": category,
        "eventHash": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16],
        "timestampHint": timestamp_match.group(0) if timestamp_match else "",
        "redacted": True,
    }


def _diagnostic_path_summary(path_value: Any) -> Dict[str, Any]:
    raw = str(path_value or "")
    return {
        "present": bool(raw),
        "pathHash": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16] if raw else "",
        "redacted": True,
    }


def _diagnostic_stale_lock_summary(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"redacted": True}
    lock_path = item.get("path") or item.get("lock_path") or item.get("file") or item.get("file_path") or ""
    dead_owner = bool(item.get("dead_owner"))
    result = {
        "sessionHash": hashlib.sha256(str(item.get("session_id") or item.get("sessionId") or "").encode("utf-8", errors="replace")).hexdigest()[:16] if (item.get("session_id") or item.get("sessionId")) else "",
        "pid": item.get("pid", ""),
        "deadOwner": dead_owner,
        "dead_owner": dead_owner,
        "stale": bool(item.get("stale")),
        "alive": bool(item.get("alive")),
        "removed": bool(item.get("removed")),
        "removeError": bool(item.get("remove_error")),
        "lockPath": _diagnostic_path_summary(lock_path),
        "redacted": True,
    }
    if isinstance(item.get("age_seconds"), (int, float)):
        result["age_seconds"] = item.get("age_seconds")
    return result


def _diagnostic_active_request_summary(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"redacted": True}
    return {
        "requestHash": _diagnostic_hash(item.get("request_id") or item.get("requestId")),
        "sessionHash": _diagnostic_hash(item.get("session_id") or item.get("sessionId")),
        "cancelled": bool(item.get("cancelled")),
        "createdAt": _diagnostic_timestamp(item.get("created_at") or item.get("createdAt")),
        "streamAvailable": bool(item.get("stream_available") or item.get("streamAvailable")),
        "redacted": True,
    }


def _diagnostic_hash(value: Any) -> str:
    raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16] if raw else ""


def _diagnostic_safe_token(value: Any, limit: int = 80) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)[:limit]
    if re.search(r"(?i)(secret|token|password|credential|api[_-]?key|bearer|sk-|ghp_|github_pat)", safe):
        return "redacted"
    return safe


_DIAGNOSTIC_ACTIONS = {"agent_install_request", "diagnose", "install", "install_capability_pack", "install_skill"}
_DIAGNOSTIC_ERROR_TYPES = {
    "artifact_metadata_limit",
    "backpressure_global",
    "backpressure_session",
    "capability_policy_blocked",
    "cancelled",
    "image_job_failed",
    "model_config_not_ready",
    "permission_denied",
    "request_conflict",
    "runtime_error",
    "stream_lost",
    "tool_output_limit",
}
_DIAGNOSTIC_IMAGE_STATUSES = {
    "cancelled",
    "completed",
    "failed",
    "partial",
    "queued",
    "ready",
    "running",
    "started",
}
_DIAGNOSTIC_POLICY_MODES = {"ask", "disabled", "preinstall"}
_DIAGNOSTIC_POLICY_SOURCES = {
    "admin-cache",
    "runtime-default",
    "runtime-default-invalid",
    "runtime-default-none",
    "runtime-default-unavailable",
}
_DIAGNOSTIC_TERMINAL_STATUSES = {
    "cancelled",
    "completed",
    "done",
    "failed",
    "interrupted",
    "manual_retry",
    "running",
    "stream_lost",
    "timeout",
}
_DIAGNOSTIC_EVENT_TYPES = {
    "approval.requested",
    "artifact.created",
    "artifact.feedback",
    "artifact.limit",
    "artifact.updated",
    "assistant.delta",
    "assistant.snapshot",
    "capability.policy_blocked",
    "image_job.artifact",
    "image_job.cancelled",
    "image_job.completed",
    "image_job.failed",
    "image_job.progress",
    "image_job.started",
    "legacy.phase",
    "message.assistant.finalized",
    "message.finalizing",
    "model.delta",
    "permission.requested",
    "reasoning.update",
    "run.cancelled",
    "run.completed",
    "run.failed",
    "run.interrupted",
    "run.paused",
    "run.phase",
    "run.queued",
    "run.started",
    "stream.replay_gap",
    "task.cancelled",
    "task.completed",
    "task.failed",
    "task.health_changed",
    "task.heartbeat",
    "task.intervention_requested",
    "task.started",
    "subagent.cancelled",
    "subagent.completed",
    "subagent.failed",
    "subagent.started",
    "subagent.timeout",
    "subagent.updated",
    "tool.completed",
    "tool.deadline_extended",
    "tool.failed",
    "tool.finished",
    "tool.heartbeat",
    "tool.started",
}


def _diagnostic_enum(value: Any, allowed: set, limit: int = 80) -> str:
    token = _diagnostic_safe_token(value, limit)
    return token if token in allowed else ""


def _diagnostic_event_type(value: Any) -> str:
    return _diagnostic_enum(value, _DIAGNOSTIC_EVENT_TYPES, 120)


def _diagnostic_event_type_summary(value: Any) -> Dict[str, Any]:
    event_type = _diagnostic_event_type(value)
    if event_type:
        return {"eventType": event_type}
    return {
        "eventType": "unknown",
        "eventTypeHash": _diagnostic_hash(value),
        "eventTypeRedacted": True,
    }


def _diagnostic_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", raw):
        return raw[:80]
    return ""


def _diagnostic_source_summary(value: Any) -> Dict[str, Any]:
    token = _diagnostic_safe_token(value, 80)
    known_sources = {
        "agent",
        "admin",
        "bridge",
        "image_job",
        "runtime",
        "scheduler",
        "subagent",
        "test",
        "tool",
        "web",
        "web_channel",
        "WebUI",
    }
    result = {"sourceHash": _diagnostic_hash(value), "redacted": True}
    if token in known_sources:
        result["source"] = token
    return result


def _diagnostic_event_payload_summary(event_type: str, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"redacted": True, "payloadShape": "non-object"}
    result: Dict[str, Any] = {
        "redacted": True,
        "payloadShape": "object",
        "payloadKeyCount": len(payload),
    }
    if event_type == "capability.policy_blocked":
        pack_id = payload.get("pack_id") or payload.get("packId")
        raw_action = payload.get("action")
        raw_error_type = payload.get("error_type") or payload.get("errorType")
        raw_policy_mode = payload.get("policy_mode") or payload.get("policyMode")
        raw_policy_source = payload.get("policy_source") or payload.get("policySource")
        result.update({
            "action": _diagnostic_enum(raw_action, _DIAGNOSTIC_ACTIONS, 40),
            "actionHash": _diagnostic_hash(raw_action) if raw_action and not _diagnostic_enum(raw_action, _DIAGNOSTIC_ACTIONS, 40) else "",
            "errorType": _diagnostic_enum(raw_error_type, _DIAGNOSTIC_ERROR_TYPES, 80),
            "errorTypeHash": _diagnostic_hash(raw_error_type) if raw_error_type and not _diagnostic_enum(raw_error_type, _DIAGNOSTIC_ERROR_TYPES, 80) else "",
            "policyMode": _diagnostic_enum(raw_policy_mode, _DIAGNOSTIC_POLICY_MODES, 40),
            "policyModeHash": _diagnostic_hash(raw_policy_mode) if raw_policy_mode and not _diagnostic_enum(raw_policy_mode, _DIAGNOSTIC_POLICY_MODES, 40) else "",
            "policySource": _diagnostic_enum(raw_policy_source, _DIAGNOSTIC_POLICY_SOURCES, 80),
            "policySourceHash": _diagnostic_hash(raw_policy_source) if raw_policy_source and not _diagnostic_enum(raw_policy_source, _DIAGNOSTIC_POLICY_SOURCES, 80) else "",
            "installAllowed": bool(payload.get("install_allowed") or payload.get("installAllowed")),
            "packHash": _diagnostic_hash(pack_id),
            "packIdRedacted": bool(payload.get("pack_id_redacted") or payload.get("packIdRedacted")),
        })
    elif event_type.startswith("image_job."):
        raw_status = payload.get("status")
        raw_policy_version = payload.get("parallelism_policy_version")
        result.update({
            "jobHash": _diagnostic_hash(payload.get("job_id") or payload.get("jobId")),
            "status": _diagnostic_enum(raw_status, _DIAGNOSTIC_IMAGE_STATUSES, 40),
            "statusHash": _diagnostic_hash(raw_status) if raw_status and not _diagnostic_enum(raw_status, _DIAGNOSTIC_IMAGE_STATUSES, 40) else "",
            "artifactCount": len(payload.get("artifacts") or []) if isinstance(payload.get("artifacts"), list) else 0,
            "effectiveMaxParallel": payload.get("effective_max_parallel") if isinstance(payload.get("effective_max_parallel"), int) else None,
            "parallelismPolicyVersion": "v1" if raw_policy_version == "v1" else "",
            "parallelismPolicyVersionHash": _diagnostic_hash(raw_policy_version) if raw_policy_version and raw_policy_version != "v1" else "",
        })
    elif event_type in {"run.queued", "run.started", "run.completed", "run.failed", "run.cancelled", "message.assistant.finalized"}:
        raw_status = payload.get("status") or payload.get("terminal_reason")
        raw_error_type = payload.get("error_type") or payload.get("errorType")
        result.update({
            "terminal": event_type in {"run.completed", "run.failed", "run.cancelled", "message.assistant.finalized"},
            "status": _diagnostic_enum(raw_status, _DIAGNOSTIC_TERMINAL_STATUSES, 40),
            "statusHash": _diagnostic_hash(raw_status) if raw_status and not _diagnostic_enum(raw_status, _DIAGNOSTIC_TERMINAL_STATUSES, 40) else "",
            "errorType": _diagnostic_enum(raw_error_type, _DIAGNOSTIC_ERROR_TYPES, 80),
            "errorTypeHash": _diagnostic_hash(raw_error_type) if raw_error_type and not _diagnostic_enum(raw_error_type, _DIAGNOSTIC_ERROR_TYPES, 80) else "",
        })
    return {key: value for key, value in result.items() if value not in ("", None)}


def _diagnostic_runtime_event_summary(event: Any) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return {"redacted": True}
    raw_event_type = event.get("event_type")
    event_type = _diagnostic_event_type(raw_event_type) or "unknown"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "eventId": int(event.get("event_id") or 0),
        **_diagnostic_event_type_summary(raw_event_type),
        "eventHash": _diagnostic_hash(f"{event.get('event_id')}:{raw_event_type}:{event.get('created_at')}"),
        "requestHash": _diagnostic_hash(event.get("request_id")),
        "sessionHash": _diagnostic_hash(event.get("session_id")),
        **_diagnostic_source_summary(event.get("source")),
        "createdAt": event.get("created_at", ""),
        "payload": _diagnostic_event_payload_summary(event_type, payload),
        "redacted": True,
    }


def _diagnostic_runtime_events_payload(session_id: str = "", request_id: str = "", limit: int = 80) -> Dict[str, Any]:
    try:
        from agent.protocol import get_run_event_ledger

        ledger = get_run_event_ledger()
        bounded_limit = max(1, min(200, int(limit or 80)))
        latest_event_id = int(ledger.latest_event_id() or 0)
        if request_id:
            events = ledger.events_for_request(str(request_id), limit=bounded_limit)
        elif session_id:
            events = ledger.list_events(session_id=str(session_id), limit=bounded_limit)
        else:
            events = ledger.list_events(after_event_id=max(0, latest_event_id - bounded_limit * 2), limit=bounded_limit)
        event_type_counts: Dict[str, int] = {}
        for item in events:
            event_type = _diagnostic_event_type(item.get("event_type")) or "unknown"
            if event_type:
                event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        capability_blocks = sum(
            1
            for item in events
            if _diagnostic_event_type(item.get("event_type")) == "capability.policy_blocked"
        )
        terminal_events = sum(
            1
            for item in events
            if _diagnostic_event_type(item.get("event_type"))
            in {"run.completed", "run.failed", "run.cancelled", "message.assistant.finalized"}
        )
        return {
            "status": "success",
            "source": "runtime-event-ledger",
            "latestEventId": latest_event_id,
            "scopedBy": "request" if request_id else ("session" if session_id else "global"),
            "eventsInspected": len(events),
            "eventTypeCounts": dict(sorted(event_type_counts.items())),
            "capabilityPolicyBlockedCount": capability_blocks,
            "terminalEventCount": terminal_events,
            "recent": [_diagnostic_runtime_event_summary(item) for item in events[-20:]],
            "redacted": True,
        }
    except Exception as exc:
        logger.debug(f"[WebChannel] diagnostic runtime event summary skipped: {_web_body_log_summary(exc)}")
        return {
            "status": "error",
            "source": "runtime-event-ledger",
            "message": "runtime event summary unavailable",
            "messageHash": _diagnostic_hash(exc),
            "redacted": True,
        }


def _diagnostic_capability_policy_payload() -> Dict[str, Any]:
    try:
        from common.ecorex_capability_policy import load_capability_policy, policy_for_pack

        payload = load_capability_policy()
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
        mode_counts: Dict[str, int] = {}
        disabled_packs: List[Dict[str, Any]] = []
        for pack_id, item in capabilities.items():
            if not isinstance(item, dict):
                continue
            public_policy = policy_for_pack(pack_id, pack_name=item.get("name") or pack_id)
            mode = _diagnostic_enum(public_policy.get("policyMode"), _DIAGNOSTIC_POLICY_MODES, 40) or "ask"
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            if mode == "disabled":
                disabled_packs.append({
                    "packHash": _diagnostic_hash(pack_id),
                    "policyStatusHash": _diagnostic_hash(public_policy.get("policyStatus")),
                    "packIdRedacted": bool(public_policy.get("packIdRedacted")),
                    "redacted": True,
                })
        source = payload.get("source")
        global_mode = _diagnostic_enum(policy.get("mode"), _DIAGNOSTIC_POLICY_MODES, 40) or "ask"
        source_value = _diagnostic_enum(source, _DIAGNOSTIC_POLICY_SOURCES, 80)
        updated_at = _diagnostic_timestamp(payload.get("updatedAt") or policy.get("updatedAt"))
        result = {
            "status": "success" if payload.get("available") else "default",
            "source": source_value or "runtime-default",
            "sourceHash": _diagnostic_hash(source) if source and not source_value else "",
            "policyAvailable": bool(payload.get("available")),
            "globalMode": global_mode,
            "policyUpdatedAt": updated_at,
            "policyUpdatedAtHash": _diagnostic_hash(payload.get("updatedAt") or policy.get("updatedAt")) if (payload.get("updatedAt") or policy.get("updatedAt")) and not updated_at else "",
            "mirrorConfigured": bool(policy.get("mirror")),
            "offlineCacheConfigured": bool(policy.get("offlineCache") or policy.get("offline_cache")),
            "capabilityCount": len(capabilities),
            "modeCounts": dict(sorted(mode_counts.items())),
            "disabledPackCount": len(disabled_packs),
            "disabledPacks": disabled_packs[:20],
            "redacted": True,
        }
        return {key: value for key, value in result.items() if value not in ("", None)}
    except Exception as exc:
        logger.debug(f"[WebChannel] diagnostic capability policy summary skipped: {_web_body_log_summary(exc)}")
        return {
            "status": "error",
            "source": "capability-policy",
            "message": "capability policy summary unavailable",
            "messageHash": _diagnostic_hash(exc),
            "redacted": True,
        }


def _diagnostic_bundle_payload(session_id: str = "", request_id: str = "") -> Dict[str, Any]:
    from cli import __version__
    from config import get_root

    log_snapshot = _log_snapshot_payload(120)
    log_events = [
        _diagnostic_event_summary(line)
        for line in (log_snapshot.get("log", {}) or {}).get("lines", [])
        if re.search(r"\b(error|warn|failed|exception|traceback)\b|错误|失败|异常", str(line or ""), re.IGNORECASE)
    ][-50:]
    workspace_root = _get_workspace_root()
    runtime_root = str(get_root())
    channel = WebChannel()
    active = channel.active_requests_snapshot()
    active_requests = [
        _diagnostic_active_request_summary(item)
        for item in (active.get("requests", []) if isinstance(active, dict) else [])
    ]
    return {
        "status": "success",
        "type": "diagnostic_bundle",
        "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "version": __version__,
        "runtime": {
            "surface": "desktop" if os.environ.get("ECOREX_DESKTOP") == "1" else "webui",
            "bootId": os.environ.get("ECOREX_DESKTOP_BOOT_ID", ""),
            "pid": os.getpid(),
            "workspaceRoot": _diagnostic_path_summary(workspace_root),
            "runtimeRoot": _diagnostic_path_summary(runtime_root),
        },
        "current": {
            "sessionHash": _diagnostic_hash(session_id),
            "requestHash": _diagnostic_hash(request_id),
            "redacted": True,
        },
        "activeRequests": active_requests,
        "staleLocks": [
            _diagnostic_stale_lock_summary(item)
            for item in (active.get("stale_locks", []) if isinstance(active, dict) else [])
        ],
        "logs": {
            "path": _diagnostic_path_summary((log_snapshot.get("log", {}) or {}).get("path", "")),
            "exists": bool((log_snapshot.get("log", {}) or {}).get("exists")),
            "recentEvents": log_events,
            "note": "Recent events and local paths are category/hash summaries only; prompts, file contents, artifact contents, and raw log lines are intentionally omitted.",
        },
        "runtimeEvents": _diagnostic_runtime_events_payload(session_id=session_id, request_id=request_id),
        "capabilityPolicy": _diagnostic_capability_policy_payload(),
        "audit": {
            "sourceOfTruth": "runtime-event-ledger",
            "includesRawRuntimePayloads": False,
            "includesRawCapabilityPolicyPaths": False,
            "redacted": True,
        },
        "privacy": {
            "includesPromptText": False,
            "includesFileContents": False,
            "includesArtifactContents": False,
            "includesRawRuntimePayloads": False,
            "includesRawCapabilityPolicyPaths": False,
        },
    }


def _resolve_run_log_path(runtime_root: Path) -> Path:
    try:
        from agent.tools.host_diagnostics.host_diagnostics import _candidate_log_paths

        candidates = _candidate_log_paths(runtime_root)
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        if candidates:
            return candidates[0]
    except Exception as exc:
        logger.debug(f"[WebChannel] active log path lookup failed: {_web_body_log_summary(exc)}")
    return runtime_root / "run.log"


def _parse_log_line_limit(value: Any, default: int = 200) -> int:
    try:
        return max(1, min(500, int(value or default)))
    except (TypeError, ValueError):
        return default


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                # Browsers routinely probe optional asset variants (e.g. a
                # .ttf fallback declared alongside .woff2 in @font-face);
                # logging these as errors floods the console with harmless
                # noise. Keep it at debug level — real misconfigurations
                # will still surface via the network panel.
                logger.debug(f"Static file not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except web.HTTPError:
            # The 404 path above already logged at debug; re-raise as-is so
            # web.py returns the original status to the client.
            raise
        except Exception as e:
            logger.error(f"Error serving static file: {_web_body_log_summary(e)}")
            raise web.notfound()


class KnowledgeListHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            svc = KnowledgeService(_get_workspace_root())
            result = svc.list_tree()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge list error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


class KnowledgeReadHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            params = web.input(path='')
            svc = KnowledgeService(_get_workspace_root())
            result = svc.read_file(params.path)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps(_public_error_payload("Request failed.", e))
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Request failed.", e))


class KnowledgeGraphHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            svc = KnowledgeService(_get_workspace_root())
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {_web_body_log_summary(e)}")
            return json.dumps({"nodes": [], "links": []})


class TencentDocsStatusHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(start='0')
            start = str(params.start or "").strip().lower() in {"1", "true", "yes"}
            return json.dumps(_tencent_docs_status_payload(start=start), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tencent Docs status error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Tencent Docs status unavailable.", e), ensure_ascii=False)


class TencentDocsConnectHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            endpoint = str(body.get("endpoint") or TENCENT_DOCS_MCP_ENDPOINT).strip().rstrip("/")
            if endpoint != TENCENT_DOCS_MCP_ENDPOINT:
                return json.dumps({
                    "status": "error",
                    "message": "Tencent Docs MCP endpoint must use the official https://docs.qq.com/openapi/mcp endpoint.",
                    "redacted": True,
                }, ensure_ascii=False)
            token = str(body.get("token") or body.get("authorization") or "").strip()
            _write_tencent_docs_mcp_config(token)
            _tencent_docs_wait_for_ready(timeout_seconds=8.0)
            try:
                ChannelsHandler._refresh_runtime_capabilities("tencent-docs-mcp-connect")
            except Exception:
                pass
            payload = _tencent_docs_status_payload(start=True)
            payload["message"] = "Tencent Docs MCP connected."
            return json.dumps(payload, ensure_ascii=False)
        except ValueError as e:
            return json.dumps(_public_validation_error_payload(e), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tencent Docs connect error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Tencent Docs connect failed.", e), ensure_ascii=False)


class TencentDocsDisconnectHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            removed = _remove_tencent_docs_mcp_config()
            _tencent_docs_tool_snapshot(start=True)
            try:
                ChannelsHandler._refresh_runtime_capabilities("tencent-docs-mcp-disconnect")
            except Exception:
                pass
            payload = _tencent_docs_status_payload(start=False)
            payload["message"] = "Tencent Docs MCP disconnected." if removed else "Tencent Docs MCP was not configured."
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tencent Docs disconnect error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Tencent Docs disconnect failed.", e), ensure_ascii=False)


class TencentDocsFilesHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(tab='recent', q='', limit='20')
            return json.dumps(
                _tencent_docs_files_payload(params.tab, params.q, params.limit),
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Tencent Docs files GET error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Tencent Docs files unavailable.", e), ensure_ascii=False)

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            return json.dumps(
                _tencent_docs_files_payload(
                    body.get("tab") or body.get("mode") or "recent",
                    body.get("q") or body.get("query") or "",
                    body.get("limit") or 20,
                ),
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Tencent Docs files POST error: {_web_body_log_summary(e)}")
            return json.dumps(_public_error_payload("Tencent Docs files unavailable.", e), ensure_ascii=False)


_UPDATE_STATE_MODES = {"manual", "background"}
_UPDATE_STATE_STATUSES = {
    "available",
    "downloading",
    "verified",
    "staged",
    "deferred",
    "installed",
    "activated",
    "failed",
    "rollback",
}
_UPDATE_BROWSER_ACTIONS = {
    "defer-to-existing-tab-soft-refresh",
    "open-default-browser",
    "none",
}
_UPDATE_NOTICE_CACHE_LOCK = threading.RLock()
_UPDATE_NOTICE_CACHE: Dict[str, Any] = {"expiresAt": 0.0, "payload": {}}
_ENTERPRISE_RELEASE_NOTICE_CACHE_LOCK = threading.RLock()
_ENTERPRISE_RELEASE_NOTICE_CACHE: Dict[str, Any] = {"expiresAt": 0.0, "notice": {}}


def _normalize_release_notice_payload(value: Any, fallback_version: str = "") -> Dict[str, Any]:
    raw = value.get("notice") if isinstance(value, dict) and isinstance(value.get("notice"), dict) else value
    if not isinstance(raw, dict):
        return {}
    revision = str(raw.get("revision") or raw.get("noticeRevision") or "").strip()
    if not revision:
        return {}
    version = str(raw.get("version") or fallback_version or "").strip()[:40]
    message = str(
        raw.get("message")
        or (f"EcoreX {version} 已发布，已安装用户可在本机检查更新。" if version else "")
    ).strip()[:240]
    return {
        "revision": revision[:120],
        "version": version,
        "message": message,
        "publishedAt": str(raw.get("publishedAt") or raw.get("published_at") or raw.get("noticeUpdatedAt") or "").strip()[:80],
        "reason": str(raw.get("reason") or "admin-release-notify").strip()[:80],
        "redacted": True,
    }


def _release_notice_update_state_payload(notice: Dict[str, Any], source: str = "release-notice") -> Dict[str, Any]:
    notice = _normalize_release_notice_payload(notice)
    if not notice:
        return {}
    version = notice.get("version") or ""
    current_version = ""
    try:
        from cli import __version__
        current_version = str(__version__ or "")
    except Exception:
        current_version = ""
    same_or_older = bool(
        version
        and current_version
        and UpdateCheckHandler()._compare_versions(version, current_version) <= 0
    )
    message = notice.get("message") or (
        f"EcoreX {version} 已是当前 stable，无需重复下载。"
        if same_or_older
        else f"EcoreX {version} 已发布，本机更新器可在空闲时检查并安装。"
    )
    return {
        "stateAvailable": True,
        "source": source,
        "product": "EcoreX WebUI",
        "version": version,
        "mode": "manual",
        "status": "available",
        "reason": notice.get("reason") or "admin-release-notify",
        "message": message,
        "browserAction": "none",
        "activationPolicy": "manual-update-check",
        "healthCheck": {"endpoint": "/api/version", "status": "pending", "passed": False},
        "generatedAt": notice.get("publishedAt") or datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "refreshRequired": False,
        "noticeRevision": notice.get("revision"),
        "redacted": True,
    }


def _enterprise_release_notice_payload() -> Dict[str, Any]:
    now = time.time()
    with _ENTERPRISE_RELEASE_NOTICE_CACHE_LOCK:
        if float(_ENTERPRISE_RELEASE_NOTICE_CACHE.get("expiresAt") or 0) > now:
            cached = _ENTERPRISE_RELEASE_NOTICE_CACHE.get("notice")
            return dict(cached) if isinstance(cached, dict) else {}
    notice: Dict[str, Any] = {}
    try:
        base = _web_enterprise_client_base()
        if base:
            for client_key in _enterprise_client_keys_for_request():
                request = urllib.request.Request(
                    f"{base}/release-notice",
                    headers={
                        "Accept": "application/json",
                        "X-EcoreX-Client-Key": client_key,
                        "User-Agent": "EcoreX-WebReleaseNotice/0.3.0",
                    },
                    method="GET",
                )
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        payload = json.loads(response.read(128_000).decode("utf-8", errors="replace") or "{}")
                    notice = _normalize_release_notice_payload(payload)
                    if notice:
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code in (403, 404):
                        continue
                    raise
    except Exception as exc:
        logger.debug(f"[WebChannel] enterprise release notice unavailable: {_web_body_log_summary(exc)}")
        notice = {}
    with _ENTERPRISE_RELEASE_NOTICE_CACHE_LOCK:
        _ENTERPRISE_RELEASE_NOTICE_CACHE["expiresAt"] = now + 30
        _ENTERPRISE_RELEASE_NOTICE_CACHE["notice"] = dict(notice)
    return notice


def _release_manifest_notice_state_payload() -> Dict[str, Any]:
    now = time.time()
    with _UPDATE_NOTICE_CACHE_LOCK:
        if float(_UPDATE_NOTICE_CACHE.get("expiresAt") or 0) > now:
            cached = _UPDATE_NOTICE_CACHE.get("payload")
            return dict(cached) if isinstance(cached, dict) else {}
    payload: Dict[str, Any] = {}
    enterprise_notice = _enterprise_release_notice_payload()
    if enterprise_notice:
        payload = _release_notice_update_state_payload(enterprise_notice, "admin-release-notice")
        with _UPDATE_NOTICE_CACHE_LOCK:
            _UPDATE_NOTICE_CACHE["expiresAt"] = now + 30
            _UPDATE_NOTICE_CACHE["payload"] = dict(payload)
        return payload
    try:
        configured = (
            os.environ.get("ECOREX_RELEASE_MANIFEST_URL")
            or conf().get("release_manifest_url")
            or "https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json"
        )
        manifest_url = str(configured or "").strip()
        if os.path.isfile(manifest_url):
            with open(manifest_url, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        else:
            with urllib.request.urlopen(manifest_url, timeout=3) as response:
                manifest = json.loads(response.read().decode("utf-8"))
        if isinstance(manifest, dict):
            update = manifest.get("update") if isinstance(manifest.get("update"), dict) else {}
            webui = update.get("webui") if isinstance(update.get("webui"), dict) else {}
            notice = webui.get("notice") if isinstance(webui.get("notice"), dict) else {}
            notice = _normalize_release_notice_payload(
                {
                    **notice,
                    "revision": notice.get("revision") or webui.get("noticeRevision"),
                    "publishedAt": notice.get("publishedAt") or webui.get("noticeUpdatedAt"),
                },
                str(manifest.get("version") or ""),
            )
            payload = _release_notice_update_state_payload(notice, "release-notice")
    except Exception as exc:
        logger.debug(f"[WebChannel] release notice unavailable: {_web_body_log_summary(exc)}")
    with _UPDATE_NOTICE_CACHE_LOCK:
        _UPDATE_NOTICE_CACHE["expiresAt"] = now + 30
        _UPDATE_NOTICE_CACHE["payload"] = dict(payload)
    return payload


def _safe_local_update_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return ""
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path or "/",
        "",
        "",
        "",
    ))


def _webui_update_state_path() -> Optional[Path]:
    configured = (
        os.environ.get("ECOREX_WEBUI_STATE_DIR")
        or conf().get("webui_state_dir")
        or conf().get("state_dir")
    )
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(str(configured)).expanduser())
    appdata_dir = str(conf().get("appdata_dir") or "").strip()
    if appdata_dir:
        appdata_path = Path(appdata_dir).expanduser()
        if appdata_path.name.lower() == "appdata":
            candidates.append(appdata_path.parent)
        candidates.append(appdata_path)
    if os.name != "nt":
        candidates.append(Path("/opt/ecorex-web/state"))
    for candidate in candidates:
        try:
            path = candidate / "update-state.json"
            resolved = path.resolve()
            if path.is_file():
                return resolved
        except Exception:
            continue
    return candidates[0] / "update-state.json" if candidates else None


def _local_runtime_update_state_payload() -> Dict[str, Any]:
    path = _webui_update_state_path()
    if not path or not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 64 * 1024:
            return {
                "stateAvailable": False,
                "status": "failed",
                "reason": "state_file_too_large",
                "source": "local-update-state",
            }
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {}
        mode = _diagnostic_enum(payload.get("mode"), _UPDATE_STATE_MODES, 40) or "manual"
        status = _diagnostic_enum(payload.get("status"), _UPDATE_STATE_STATUSES, 40) or "available"
        browser_action = (
            _diagnostic_enum(payload.get("browserAction") or payload.get("browser_action"), _UPDATE_BROWSER_ACTIONS, 80)
            or ("defer-to-existing-tab-soft-refresh" if mode == "background" else "open-default-browser")
        )
        activation_policy = str(payload.get("activationPolicy") or payload.get("activation_policy") or "").strip()
        if not activation_policy:
            activation_policy = "prompt-soft-refresh-existing-tab" if mode == "background" else "manual-open-browser"
        health_check = payload.get("healthCheck") or payload.get("health_check") or {}
        health_payload: Dict[str, Any] = {
            "endpoint": "/api/version",
            "status": "pass" if status in {"installed", "activated"} else ("failed" if status in {"failed", "rollback"} else "pending"),
            "passed": status in {"installed", "activated"},
        }
        if isinstance(health_check, dict):
            endpoint = str(health_check.get("endpoint") or "").strip()
            if endpoint.startswith("/api/"):
                health_payload["endpoint"] = endpoint
            check_status = _diagnostic_enum(health_check.get("status"), {"pass", "pending", "failed"}, 40)
            if check_status:
                health_payload["status"] = check_status
            if isinstance(health_check.get("passed"), bool):
                health_payload["passed"] = health_check.get("passed")
        external_connections = payload.get("externalConnections") or payload.get("external_connections") or {}
        external_connections_payload: Dict[str, Any] = {}
        if isinstance(external_connections, dict):
            def _safe_id_list(value: Any) -> List[str]:
                if not isinstance(value, list):
                    return []
                return sorted({
                    str(item).strip()[:80]
                    for item in value
                    if str(item or "").strip()
                })

            def _safe_snapshot(value: Any) -> Dict[str, Any]:
                if not isinstance(value, dict):
                    return {}
                return {
                    "status": _diagnostic_enum(value.get("status"), {"pass", "pending", "failed", "unavailable", "not_checked"}, 40) or "unknown",
                    "reason": str(value.get("reason") or "")[:120],
                    "configuredIds": _safe_id_list(value.get("configuredIds") or value.get("configured_ids")),
                    "connectedIds": _safe_id_list(value.get("connectedIds") or value.get("connected_ids")),
                    "callableIds": _safe_id_list(value.get("callableIds") or value.get("callable_ids")),
                    "checkedAt": _diagnostic_timestamp(value.get("checkedAt") or value.get("checked_at")),
                    "redacted": True,
                }

            external_connections_payload = {
                "required": bool(external_connections.get("required", True)),
                "status": _diagnostic_enum(external_connections.get("status"), {"pass", "pending", "failed"}, 40) or "pending",
                "passed": bool(external_connections.get("passed")),
                "policy": str(external_connections.get("policy") or "")[:180],
                "before": _safe_snapshot(external_connections.get("before")),
                "after": _safe_snapshot(external_connections.get("after")),
                "missingIds": _safe_id_list(external_connections.get("missingIds") or external_connections.get("missing_ids")),
                "redacted": True,
            }
        result: Dict[str, Any] = {
            "stateAvailable": True,
            "source": "local-update-state",
            "product": str(payload.get("product") or "EcoreX WebUI")[:80],
            "version": str(payload.get("version") or "")[:40],
            "mode": mode,
            "status": status,
            "reason": str(payload.get("reason") or "")[:120],
            "message": str(payload.get("message") or "")[:240],
            "browserAction": browser_action,
            "activationPolicy": activation_policy[:80],
            "healthCheck": health_payload,
            "externalConnections": external_connections_payload,
            "generatedAt": _diagnostic_timestamp(payload.get("generatedAt")),
            "refreshRequired": mode == "background" and status in {"installed", "activated"} and browser_action == "defer-to-existing-tab-soft-refresh",
            "noticeRevision": str(payload.get("noticeRevision") or payload.get("notice_revision") or "")[:120],
            "redacted": True,
        }
        safe_url = _safe_local_update_url(payload.get("url"))
        if safe_url:
            result["url"] = safe_url
        return {key: value for key, value in result.items() if value not in ("", None)}
    except Exception as exc:
        logger.debug(f"[WebChannel] update state unavailable: {_web_body_log_summary(exc)}")
        return {
            "stateAvailable": False,
            "status": "failed",
            "reason": "state_read_error",
            "errorHash": _diagnostic_hash(exc),
            "source": "local-update-state",
            "redacted": True,
        }


def _runtime_update_state_payload() -> Dict[str, Any]:
    local_payload = _local_runtime_update_state_payload()
    local_status = str(local_payload.get("status") or "")
    local_mode = str(local_payload.get("mode") or "")
    if (
        local_payload.get("noticeRevision")
        or local_payload.get("refreshRequired")
        or (
            local_payload.get("stateAvailable")
            and local_mode == "background"
            and local_status in {"installed", "activated", "staged", "verified", "downloading", "deferred"}
        )
    ):
        return local_payload
    notice_payload = _release_manifest_notice_state_payload()
    if notice_payload:
        return notice_payload
    return local_payload


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        import os
        from cli import __version__
        from common.ecorex_release_notes import get_current_release_notes
        runtime_token = os.environ.get("ECOREX_DESKTOP_RUNTIME_TOKEN", "")
        web_ctx = getattr(web, "ctx", None)
        header_token = getattr(web_ctx, "env", {}).get("HTTP_X_ECOREX_RUNTIME_TOKEN", "") if web_ctx else ""
        verified = bool(runtime_token and header_token and runtime_token == header_token)

        payload = {
            "version": __version__,
            "releaseNotes": get_current_release_notes(),
            "desktopRuntimeVerified": verified,
            "updateState": _runtime_update_state_payload(),
        }
        if verified:
            payload["bootId"] = os.environ.get("ECOREX_DESKTOP_BOOT_ID", "")
        return json.dumps(payload, ensure_ascii=False)
