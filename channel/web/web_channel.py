import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import random
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
from typing import Any, Dict, List, Tuple

import web

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from collections import OrderedDict
from common import const
from common import i18n
from common.log import logger
from common.singleton import singleton
from config import conf, _ensure_ecorex_runtime_defaults

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
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


def _web_app_bridge_script() -> str:
    """Small browser bridge so the desktop React app can reuse WebUI HTTP APIs."""
    return r"""
<script>
(function () {
  if (window.ecorexDesktop) return;

  var DEFAULT_WEB_CLIENT_KEY = "ecorex-web-v0.1.15-web.1";
  var DEFAULT_WEB_COMPAT_CLIENT_KEYS = [
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

  function makeLocalSession(authRequired, identity) {
    identity = identity || {};
    var email = String(identity.email || "").trim().toLowerCase();
    if (!email) email = authRequired ? "ecorex@ecorex.local" : "local@ecorex.local";
    var name = String(identity.name || "").trim();
    if (!name) name = email.indexOf("@") > 0 ? email.split("@")[0] : "EcoreX";
    if (email === "ecorex@ecorex.local" || email === "local@ecorex.local") name = "EcoreX";
    return {
      authenticated: true,
      localFallback: true,
      deviceId: deviceId(),
      expiresAt: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
      user: {
        id: authRequired ? "ecorex-password" : "ecorex-local",
        name: name,
        email: email,
        role: "user",
        status: "active"
      },
      quota: { allowed: true }
    };
  }

  function webSession(authRequired, allowLocalFallback, identity, persistLocal) {
    var admin = readAdminSession();
    if (admin && admin.user && admin.token) return admin;
    if (!allowLocalFallback) return null;
    if (identity && identity.email) {
      var next = makeLocalSession(authRequired, identity);
      if (persistLocal) writeLocalSession(next);
      return next;
    }
    var local = readLocalSession();
    if (local) return local;
    var fallback = makeLocalSession(authRequired, {});
    if (persistLocal) writeLocalSession(fallback);
    return fallback;
  }

  function clientBase() {
    return window.ECOREX_WEB_CLIENT_BASE || runtimePath("/client");
  }

  function webClientKeys() {
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

  async function apiJson(request) {
    request = request || {};
    if (String(request.path || "") === "/message") {
      var modelReady = await ensureModelReady();
      if (!modelReady.ready) {
        return {
          status: "error",
          message: modelReady.message || "请先登录企业账号，或在设置 > 模型中配置可用的 API Key 后再发送。"
        };
      }
    }
    var method = request.method || "GET";
    var headers = { "Accept": "application/json" };
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
    return fetch(runtimePath("/upload"), { method: "POST", credentials: "same-origin", body: form })
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
    var keys = webClientKeys();
    var lastPayload = {};
    var lastResponse = null;
    for (var i = 0; i < keys.length; i += 1) {
      var headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-EcoreX-Client-Key": keys[i],
        "X-EcoreX-Device-Id": sessionDeviceId(session)
      };
      if (session && session.token) headers["X-EcoreX-User-Token"] = session.token;
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
      if (!response.ok) throw new Error(payload.error || payload.message || "Client request failed");
      return payload;
    }
    throw new Error((lastPayload && (lastPayload.error || lastPayload.message)) || (lastResponse && lastResponse.statusText) || "Client request failed");
  }

  function isMissingClientBridge(error) {
    return /not found|404|failed to fetch|networkerror|load failed/i.test(String((error && error.message) || error || ""));
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
      var configured = providers.some(function (provider) { return provider && provider.configured; });
      var chat = overview.capabilities && overview.capabilities.chat ? overview.capabilities.chat : {};
      return Boolean(configured && (chat.current_provider || chat.current_model));
    } catch (error) {
      return false;
    }
  }

  async function ensureModelReady() {
    if (await localModelReady()) {
      return { ready: true };
    }
    try {
      var result = await refreshModelPolicy();
      if (result && result.configured && await localModelReady()) {
        return { ready: true };
      }
    } catch (error) {
      return {
        ready: false,
        message: "企业模型配置同步失败，请先登录企业账号或联系管理员检查后台模型配置。"
      };
    }
    return {
      ready: false,
      message: "当前网页版没有可用模型配置。请先登录企业账号，或在设置 > 模型中配置可用的 API Key。"
    };
  }

  var updateStatus = {
    state: "idle",
    platform: desktopPlatform,
    currentVersion: "0.1.15-web.1",
    message: "尚未检查更新"
  };

  async function checkForUpdates() {
    try {
      var payload = await apiJson({ path: "/api/update-check?platform=" + encodeURIComponent(desktopPlatform), method: "GET" });
      updateStatus = {
        state: payload.hasUpdate ? "available" : "not-available",
        platform: desktopPlatform,
        currentVersion: payload.currentVersion || "0.1.15-web.1",
        version: payload.latestVersion || payload.version,
        downloadUrl: payload.downloadUrl,
        message: payload.message || (payload.hasUpdate ? "发现新版本，请前往下载页安装" : "当前已经是最新版本"),
        checkedAt: new Date().toISOString()
      };
      return updateStatus;
    } catch (error) {
      updateStatus = {
        state: "error",
        platform: desktopPlatform,
        currentVersion: "0.1.15-web.1",
        message: error && error.message ? error.message : String(error),
        checkedAt: new Date().toISOString()
      };
      return updateStatus;
    }
  }

  window.ecorexDesktop = {
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
      window.open("https://www.ecoreai.cn/ecorex-agent/", "_blank", "noopener,noreferrer");
      return { ok: true, url: "https://www.ecoreai.cn/ecorex-agent/" };
    },
    onUpdateStatus: function (callback) {
      try { callback(updateStatus); } catch (error) {}
      return function () {};
    },
    chooseFiles: chooseFiles,
    chooseProjectFolder: async function () {
      var label = desktopPlatform === "darwin"
        ? "请输入或粘贴本机项目文件夹路径，例如 /Users/name/project"
        : "请输入或粘贴本机项目文件夹路径，例如 C:\\\\Users\\\\name\\\\project";
      var folderPath = window.prompt(label, "");
      folderPath = String(folderPath || "").trim();
      if (!folderPath) return null;
      var payload = await apiJson({ path: "/api/project-folder", method: "POST", body: { path: folderPath } });
      return payload.project || null;
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
      var abilities = Array.isArray(payload.abilities) ? payload.abilities : [];
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
            state: String(state.state || (installed ? "installed" : "not-installed")),
            message: String(state.message || (installed ? "能力包已安装" : "点击安装后由当前会话 agent 处理")),
            installed: installed,
            logPath: state.logPath,
            updatedAt: state.updatedAt,
            policyMode: "ask"
          };
        })
        .filter(function (item) { return !!item.id; });
    },
    reportTelemetry: async function (event) {
      try { await clientJson("/events", "POST", event || {}, true); } catch (error) {}
    },
    getEnterpriseSession: async function () {
      var admin = readAdminSession();
      if (admin && admin.user && admin.token) return admin;
      var auth = await apiJson({ path: "/auth/check", method: "GET" });
      if (auth.auth_required && !auth.authenticated) return null;
      try {
        await clientJson("/model-config", "GET", undefined, false);
      } catch (error) {
        if (isMissingClientBridge(error)) return webSession(Boolean(auth.auth_required), true);
      }
      return null;
    },
    enterpriseLogin: async function (input) {
      input = input || {};
      var adminPayload = null;
      var adminError = null;
      try {
        var currentDeviceId = deviceId();
        adminPayload = await clientJson("/auth/login", "POST", {
          email: input.email,
          password: input.password,
          deviceId: currentDeviceId,
          appVersion: "0.1.15-web.1"
        }, false);
      } catch (error) {
        adminError = error;
      }
      if (adminPayload && adminPayload.token && adminPayload.user) {
        var adminSession = {
          authenticated: true,
          token: adminPayload.token,
          deviceId: adminPayload.deviceId || adminPayload.device_id || currentDeviceId,
          expiresAt: adminPayload.expiresAt || new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
          user: adminPayload.user,
          quota: adminPayload.quota || { allowed: true }
        };
        writeAdminSession(adminSession);
        try { await apiJson({ path: "/auth/login", method: "POST", body: { password: input.password } }); } catch (error) {}
        try { await refreshModelPolicy(); } catch (error) {}
        return adminSession;
      }
      if (adminError && !isMissingClientBridge(adminError)) {
        throw adminError;
      }
      await apiJson({ path: "/auth/login", method: "POST", body: { password: input.password } });
      return webSession(true, true, { email: input.email }, true);
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
  };
})();
</script>
"""


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


def _create_auth_token():
    """Create a stateless signed token: ``<timestamp_hex>.<hmac_hex>``."""
    ts = format(int(time.time()), "x")
    sig = hmac.new(
        _get_web_password().encode(),
        ts.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def _verify_auth_token(token):
    """Verify a signed token is valid and not expired.

    The token is derived from the password, so it survives server restarts
    and automatically invalidates when the password changes.
    """
    if not token or "." not in token:
        return False
    ts_hex, sig = token.split(".", 1)
    try:
        ts = int(ts_hex, 16)
    except ValueError:
        return False
    if time.time() - ts > _session_expire_seconds():
        return False
    expected = hmac.new(
        _get_web_password().encode(),
        ts_hex.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def _check_auth():
    """Return True if request is authenticated or password not enabled."""
    if not _is_password_enabled():
        return not _is_public_bind_host(_effective_web_host())
    return _verify_auth_token(web.cookies().get("cow_auth_token", ""))


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


def _generate_session_title(user_message: str, assistant_reply: str = "") -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply)


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
    SSE_MAX_REPLAY_EVENTS = 2000
    SSE_ORPHAN_TTL_SECONDS = 120

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)
        self.request_to_session = {}  # request_id -> session_id
        self.sse_queues = {}  # request_id -> Queue (legacy/test mirror for SSE events)
        self.sse_events = {}  # request_id -> replayable SSE event list
        self.sse_event_offsets = {}  # request_id -> absolute event id for sse_events[0]
        self.sse_conditions = {}  # request_id -> threading.Condition
        self.sse_subscribers = {}  # request_id -> active EventSource connection count
        self.sse_done_sent = set()  # request_id values that already emitted a done event
        self.sse_cleanup_timers = {}  # request_id -> guarded orphan cleanup Timer
        self.request_artifacts = {}  # request_id -> list of structured artifact dicts
        self.sse_stream_tokens = {}  # legacy field; no longer used to supersede streams
        self._http_server = None

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

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
            logger.debug(f"[WebChannel] active request lookup failed for session {session_id}: {e}")
            return []

    def active_requests_snapshot(self):
        """Return backend-authoritative active request state for UI recovery.

        The browser may lose local request bookkeeping on refresh or when the
        same runtime is opened from another surface. The cancel registry is the
        source of truth for in-flight work; this API exposes it without exposing
        tool output or message content.
        """
        try:
            from agent.protocol import get_cancel_registry
            from common.ecorex_workspace import cleanup_stale_session_locks

            requests = []
            sessions = {}
            for row in get_cancel_registry().snapshot():
                request_id = row.get("request_id", "")
                session_id = row.get("session_id") or self.request_to_session.get(request_id, "")
                item = {
                    **row,
                    "session_id": session_id,
                    "stream_available": request_id in self.sse_queues,
                }
                requests.append(item)
                if session_id and not item.get("cancelled"):
                    sessions.setdefault(session_id, []).append(request_id)
            stale_locks = [
                item for item in cleanup_stale_session_locks(_get_workspace_root())
                if item.get("removed") or item.get("dead_owner") or item.get("stale")
            ]
            return {
                "status": "success",
                "requests": requests,
                "sessions": sessions,
                "stale_locks": stale_locks,
                "staleLocks": stale_locks,
            }
        except Exception as e:
            logger.error(f"[WebChannel] active_requests_snapshot error: {e}")
            return {"status": "error", "message": str(e), "requests": [], "sessions": {}, "stale_locks": [], "staleLocks": []}

    def _ensure_sse_state(self, request_id: str) -> None:
        if not request_id:
            return
        if request_id not in self.sse_queues:
            self.sse_queues[request_id] = Queue()
        if request_id not in self.sse_events:
            self.sse_events[request_id] = []
        if request_id not in self.sse_event_offsets:
            self.sse_event_offsets[request_id] = 0
        if request_id not in self.sse_conditions:
            self.sse_conditions[request_id] = threading.Condition()

    def _sse_request_exists(self, request_id: str) -> bool:
        return bool(request_id) and (
            request_id in self.sse_queues or request_id in self.sse_events
        )

    def _push_sse_event(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Append one SSE event for every subscriber and keep legacy queue parity."""
        if not self._sse_request_exists(request_id):
            return False
        self._ensure_sse_state(request_id)
        event = dict(item or {})
        try:
            self.sse_queues[request_id].put(event)
        except Exception as e:
            logger.debug(f"[WebChannel] legacy SSE queue mirror skipped for {request_id}: {e}")
        cond = self.sse_conditions[request_id]
        with cond:
            events = self.sse_events[request_id]
            events.append(event)
            excess = len(events) - self.SSE_MAX_REPLAY_EVENTS
            if excess > 0:
                del events[:excess]
                self.sse_event_offsets[request_id] = self.sse_event_offsets.get(request_id, 0) + excess
            cond.notify_all()
        return True

    def _cleanup_sse_request(self, request_id: str) -> None:
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
        self.request_to_session.pop(request_id, None)

    def _cleanup_sse_request_if_idle(self, request_id: str, reason: str = "") -> None:
        self.sse_cleanup_timers.pop(request_id, None)
        if not self._sse_request_exists(request_id):
            return
        if self.sse_subscribers.get(request_id, 0) > 0:
            return
        try:
            from agent.protocol import get_cancel_registry

            if get_cancel_registry().get_event(request_id) is not None:
                self._schedule_sse_cleanup(request_id, delay=self.SSE_ORPHAN_TTL_SECONDS, reason="active-request")
                return
        except Exception as exc:
            logger.debug(f"[WebChannel] SSE cleanup active check skipped for {request_id}: {exc}")
            return
        logger.info(f"[WebChannel] Cleaning idle SSE replay state for {request_id}: {reason}")
        self._cleanup_sse_request(request_id)

    def _schedule_sse_cleanup(self, request_id: str, delay: int = None, reason: str = "") -> None:
        if not request_id or not self._sse_request_exists(request_id):
            return
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

    def _push_done_event_once(self, request_id: str, item: Dict[str, Any]) -> bool:
        """Emit one terminal done event per request while preserving replay."""
        if request_id in self.sse_done_sent:
            logger.debug(f"[WebChannel] duplicate done skipped for request {request_id}")
            return False
        pushed = self._push_sse_event(request_id, item)
        if pushed:
            self.sse_done_sent.add(request_id)
            if self.sse_subscribers.get(request_id, 0) <= 0:
                self._schedule_sse_cleanup(request_id, reason="terminal-without-subscriber")
        return pushed

    def _push_cancelled_events_for_session(self, session_id: str, request_ids: List[str], lang: str = "zh") -> None:
        content = "Interrupted by a new message." if str(lang).lower().startswith("en") else "已被新消息中断"
        for request_id in request_ids:
            if not self._sse_request_exists(request_id):
                continue
            self._push_sse_event(request_id, {
                "type": "cancelled",
                "content": content,
                "request_id": request_id,
                "timestamp": time.time(),
            })

    def _cancel_subagents_for_parent(self, session_id: str) -> Dict[str, Any]:
        if not session_id or str(session_id).startswith("subagent-"):
            return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": []}
        try:
            from agent.tools.subagent.subagent import cancel_children_for_parent

            return cancel_children_for_parent(_get_workspace_root(), session_id)
        except Exception as e:
            logger.warning(f"[WebChannel] subagent cascade cancel skipped for {session_id}: {e}")
            return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": [], "error": str(e)}

    def _interrupt_and_wait_for_session_lock(self, session_id: str, lang: str = "zh"):
        """Cancel the active request for a busy session and wait briefly for its lock."""
        from agent.protocol import get_cancel_registry
        from common.ecorex_workspace import SessionBusyError, SessionLock

        active_request_ids = []
        active_deadline = time.time() + 1.5
        while time.time() < active_deadline:
            active_request_ids = self._active_request_ids_for_session(session_id)
            if active_request_ids:
                break
            time.sleep(0.1)
        if not active_request_ids:
            raise SessionBusyError(f"session is busy: {session_id}")

        cancelled = get_cancel_registry().cancel_session(session_id)
        subagent_cancel = self._cancel_subagents_for_parent(session_id)
        if cancelled <= 0:
            raise SessionBusyError(f"session is busy: {session_id}")

        self._push_cancelled_events_for_session(session_id, active_request_ids, lang=lang)
        logger.info(
            f"[WebChannel] interrupting busy session before new message: "
            f"session={session_id}, cancelled={cancelled}, requests={active_request_ids}, "
            f"subagents={subagent_cancel}"
        )

        deadline = time.time() + 12
        last_error = None
        while time.time() < deadline:
            try:
                return SessionLock(_get_workspace_root(), session_id).acquire()
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
            logger.debug(f"[WebChannel] _fetch_latest_pair_seqs failed: {e}")
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
            logger.debug(f"[WebChannel] _fetch_agent_usage failed: {e}")
            return None

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
            return bool(decision.get("allowed", True))
        except Exception as exc:
            logger.warning(f"[WebChannel] artifact availability check failed: {exc}")
            return False

    def _artifact_from_file_event(self, request_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        file_path = str(data.get("path") or data.get("file_path") or "").strip()
        if file_path and not self._artifact_path_available(file_path):
            return {}
        file_name = str(data.get("file_name") or os.path.basename(file_path) or "artifact").strip()
        file_type = str(data.get("file_type") or "file").strip()
        kind = self._artifact_kind(file_type, file_path)
        from urllib.parse import quote
        preview_url = f"/api/file?path={quote(file_path)}" if file_path else ""
        artifact = {
            "id": f"{request_id}:{file_path or file_name}",
            "requestId": request_id,
            "kind": kind,
            "intent": "deliverable",
            "operation": "exported",
            "status": "ready",
            "title": file_name,
            "path": file_path,
            "previewUrl": preview_url if kind in ("image", "video", "audio", "file") else "",
            "source": {
                "toolName": str(data.get("tool_name") or data.get("tool") or "file_to_send"),
                "createdAt": time.time(),
            },
        }
        try:
            if file_path and os.path.exists(file_path):
                artifact["sizeBytes"] = os.path.getsize(file_path)
                mime_type = mimetypes.guess_type(file_path)[0]
                if mime_type:
                    artifact["mimeType"] = mime_type
        except Exception:
            pass
        return artifact

    def _record_request_artifact(self, request_id: str, artifact: Dict[str, Any]) -> None:
        if not request_id or not artifact:
            return
        items = self.request_artifacts.setdefault(request_id, [])
        key = str(artifact.get("path") or artifact.get("relativePath") or artifact.get("url") or artifact.get("id") or "").lower()
        for index, existing in enumerate(items):
            existing_key = str(existing.get("path") or existing.get("relativePath") or existing.get("url") or existing.get("id") or "").lower()
            if existing.get("id") == artifact.get("id") or (key and existing_key == key):
                items[index] = {**existing, **artifact}
                return
        items.append(artifact)

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
        raw_paths = []
        for key in ("path", "file_path", "filePath", "output_path", "outputPath"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                raw_paths.append(value.strip())
        if not raw_paths:
            return []
        artifacts: List[Dict[str, Any]] = []
        for raw_path in raw_paths[:4]:
            if not self._artifact_path_available(raw_path):
                continue
            file_name = str(result.get("file_name") or result.get("fileName") or os.path.basename(raw_path) or raw_path)
            raw_type = str(result.get("file_type") or result.get("fileType") or "file")
            kind = self._artifact_kind(raw_type, raw_path)
            is_edit = "edit" in tool_key or "patch" in tool_key
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
                "status": "ready" if status in ("success", "done", "ok", "") else "failed",
                "title": file_name,
                "path": raw_path,
                "source": {
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "createdAt": time.time(),
                },
            }
            if stats:
                artifact["stats"] = stats
            if isinstance(result.get("bytes_written"), int):
                artifact["stats"] = {**artifact.get("stats", {}), "bytesWritten": int(result.get("bytes_written") or 0)}
            artifacts.append(artifact)
        return artifacts

    def _persist_request_artifacts(self, request_id: str, session_id: str) -> None:
        artifacts = self.request_artifacts.get(request_id) or []
        if not artifacts or not session_id:
            return
        try:
            from agent.memory import get_conversation_store
            get_conversation_store().attach_extras_to_last_assistant(session_id, {"artifacts": artifacts})
        except Exception as e:
            logger.debug(f"[WebChannel] artifact persist skipped for {request_id}: {e}")

    def _build_done_event(self, request_id: str, session_id: str, content: str):
        self._persist_request_artifacts(request_id, session_id)
        seqs = self._fetch_latest_pair_seqs(session_id)
        payload = {
            "type": "done",
            "content": content,
            "request_id": request_id,
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
            logger.debug(f"[WebChannel] session lock release skipped: {e}")

    def _thread_pool_callback(self, session_id, **kwargs):
        parent_callback = super()._thread_pool_callback(session_id, **kwargs)
        context = kwargs.get("context")

        def callback(worker):
            worker_exception = None
            try:
                try:
                    worker_exception = worker.exception()
                except Exception as e:
                    worker_exception = e
                parent_callback(worker)
            finally:
                self._finalize_request_after_worker(context, worker_exception)
                self._release_context_session_lock(context)

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

        try:
            from agent.protocol import get_cancel_registry

            get_cancel_registry().unregister(request_id)
        except Exception as e:
            logger.debug(f"[WebChannel] cancel token unregister skipped for {request_id}: {e}")

        session_id = context.get("session_id") or self.request_to_session.get(request_id, "")
        self._persist_request_artifacts(request_id, session_id)

        if worker_exception is not None and self._sse_request_exists(request_id):
            try:
                message = str(worker_exception) or "Worker failed before producing a response."
                self._push_done_event_once(request_id, {
                    "type": "done",
                    "content": f"❌ {message}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "usage": self._fetch_agent_usage(session_id),
                })
            except Exception as e:
                logger.debug(f"[WebChannel] worker exception SSE fallback skipped for {request_id}: {e}")

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
                    text_content = getattr(reply, 'text_content', '')
                    if text_content:
                        self._push_done_event_once(
                            request_id,
                            self._build_done_event(request_id, session_id, text_content)
                        )
                    logger.debug(f"SSE skipped duplicate file for request {request_id}")
                    return

                # Skip http-URL FILE/IMAGE_URL replies produced by chat_channel's media extraction:
                # the text reply (already sent as "done") contains the URL and the frontend will
                # render it via renderMarkdown/injectVideoPlayers, so no separate SSE event needed.
                if reply.type in (ReplyType.FILE, ReplyType.IMAGE_URL) and content.startswith(("http://", "https://")):
                    logger.debug(f"SSE skipped http media reply for request {request_id}")
                    return

                self._push_done_event_once(
                    request_id,
                    self._build_done_event(request_id, session_id, content)
                )
                logger.debug(f"SSE done sent for request {request_id}")
                # Auto-trigger TTS once the bot finishes its text reply. The
                # synthesis runs in the background so the chat stream is never
                # blocked; the resulting audio URL is pushed via a follow-up
                # `voice_attach` SSE event and persisted to messages.extras.
                if reply.type == ReplyType.TEXT and content.strip():
                    self._maybe_dispatch_auto_tts(request_id, session_id, content, context)
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
            logger.error(f"Error in send method: {e}")

    def _make_sse_callback(self, request_id: str):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""

        # Cap reasoning bytes pushed to the frontend per request to avoid
        # browser stalls / crashes on very long chains-of-thought. Anything
        # beyond the cap is dropped from the stream (DB still persists a
        # truncated copy via _truncate_reasoning_for_storage).
        # Keep aligned with frontend REASONING_RENDER_CAP and backend
        # MAX_STORED_REASONING_CHARS.
        MAX_REASONING_STREAM_CHARS = 4 * 1024  # 4 KB
        # Use a single-element list as a mutable counter accessible from closure.
        reasoning_chars_sent = [0]
        reasoning_capped_notified = [False]
        # Captures the first error message emitted by agent_stream so the
        # subsequent agent_end handler can skip its "empty final_response"
        # fallback (which would otherwise overwrite the real error).
        streamed_error: List[str] = []

        def on_event(event: dict):
            if not self._sse_request_exists(request_id):
                return
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "reasoning_update":
                delta = data.get("delta", "")
                if not delta:
                    return
                remaining = MAX_REASONING_STREAM_CHARS - reasoning_chars_sent[0]
                if remaining <= 0:
                    if not reasoning_capped_notified[0]:
                        reasoning_capped_notified[0] = True
                        self._push_sse_event(request_id, {
                            "type": "reasoning",
                            "content": "\n\n... [reasoning truncated for display] ...",
                        })
                    return
                if len(delta) > remaining:
                    delta = delta[:remaining]
                reasoning_chars_sent[0] += len(delta)
                self._push_sse_event(request_id, {"type": "reasoning", "content": delta})

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    self._push_sse_event(request_id, {"type": "delta", "content": delta})

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                self._push_sse_event(request_id, {
                    "type": "tool_start",
                    "tool": tool_name,
                    "tool_call_id": data.get("tool_call_id", ""),
                    "arguments": arguments,
                })

            elif event_type == "tool_permission_request":
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
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                tool_call_id = data.get("tool_call_id", "")
                for artifact in self._artifacts_from_tool_result(request_id, tool_name, tool_call_id, status, result):
                    self._record_request_artifact(request_id, artifact)
                    self._push_sse_event(request_id, {
                        "type": "artifact",
                        "action": "upsert",
                        "artifact": artifact,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                # Truncate long results to avoid huge SSE payloads
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "…"
                self._push_sse_event(request_id, {
                    "type": "tool_end",
                    "tool": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2)
                })

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    self._push_sse_event(request_id, {"type": "message_end", "has_tool_calls": True})

            elif event_type == "error":
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
                self._push_done_event_once(request_id, {
                    "type": "done",
                    "content": f"❌ {err_msg}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "usage": data.get("usage") or self._fetch_agent_usage(self.request_to_session.get(request_id, "")),
                })

            elif event_type == "agent_cancelled":
                # Push an explicit cancelled SSE event so the frontend
                # marks the bubble as stopped. A trailing "done" still
                # arrives with the partial answer.
                final_response = data.get("final_response", "")
                self._push_sse_event(request_id, {
                    "type": "cancelled",
                    "content": final_response,
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "agent_end":
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
                artifact = self._artifact_from_file_event(request_id, data)
                self._record_request_artifact(request_id, artifact)
                self._push_sse_event(request_id, {
                    "type": "artifact",
                    "action": "upsert",
                    "artifact": artifact,
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

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
                args=(request_id, session_id, text),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"[WebChannel] auto-tts dispatch skipped: {e}")

    def _synthesize_tts_async(
        self,
        request_id: str,
        session_id: str,
        text: str,
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
                get_conversation_store().attach_extras_to_last_assistant(session_id, payload)
            except Exception as e:
                logger.debug(f"[WebChannel] tts persist skipped: {e}")
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
            logger.warning(f"[WebChannel] TTS synthesis failed: {e}")

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
            logger.warning(f"[WebChannel] publish_tts_audio failed: {e}")
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
            logger.warning(f"[WebChannel] voice cleanup failed: {e}")

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
            logger.error(f"[WebChannel] File upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})

    def post_message(self):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports optional attachments (file paths from /upload).
        """
        session_lock = None
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id', f'session_{int(time.time())}')
            visible_prompt = str(json_data.get('message') or '')
            visible_message = str(json_data.get('visible_message') or visible_prompt or '')
            prompt = visible_prompt
            hidden_context = json_data.get('hidden_context') or json_data.get('project_context') or ''
            use_sse = json_data.get('stream', True)
            attachments = json_data.get('attachments', [])
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

            try:
                from common.ecorex_workspace import SessionBusyError, SessionLock
                session_lock = SessionLock(_get_workspace_root(), session_id).acquire()
            except SessionBusyError:
                try:
                    session_lock = self._interrupt_and_wait_for_session_lock(session_id, lang=lang)
                except SessionBusyError:
                    return json.dumps({
                        "status": "error",
                        "code": "session_busy",
                        "message": i18n.t("该会话正在执行中，请稍后再试", "This session is already running. Please try again shortly."),
                    }, ensure_ascii=False)

            # Append file references to the prompt (same format as QQ channel)
            if attachments:
                file_refs = []
                for att in attachments:
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
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")

            if isinstance(hidden_context, str) and hidden_context.strip():
                prompt = hidden_context.strip() + "\n\nUser request:\n" + (prompt or "Please handle these attachments.")

            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id
            try:
                from agent.protocol import get_cancel_registry
                get_cancel_registry().register(request_id, session_id=session_id)
            except Exception as e:
                logger.debug(f"[WebChannel] pre-register cancel token skipped: {e}")

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                self._ensure_sse_state(request_id)

            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                if self._sse_request_exists(request_id):
                    self._cleanup_sse_request(request_id)
                try:
                    from agent.protocol import get_cancel_registry
                    get_cancel_registry().unregister(request_id)
                except Exception:
                    pass
                if session_lock:
                    session_lock.release()
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            context["request_id"] = request_id
            context["session_lock"] = session_lock
            context["visible_message"] = (visible_message or "Please handle these attachments.").strip()
            if isinstance(attachments, list):
                context["attachments"] = attachments
            if is_voice_input:
                # Web channel runs its own TTS post-pipeline via
                # _maybe_dispatch_auto_tts; don't set desire_rtype here or
                # chat_channel would synthesize a duplicate VOICE reply.
                context["is_voice_input"] = True

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            threading.Thread(target=self._produce_with_session_lock, args=(context, session_lock), daemon=True).start()

            return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

        except Exception as e:
            try:
                if session_lock:
                    session_lock.release()
            except Exception:
                pass
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _produce_with_session_lock(self, context: Context, session_lock):
        try:
            self.produce(context)
        except Exception as e:
            logger.error(f"[WebChannel] produce failed before worker start: {e}", exc_info=True)
            self._finalize_request_after_worker(context, e)
            try:
                if session_lock:
                    session_lock.release()
            except Exception as e:
                logger.debug(f"[WebChannel] session lock release skipped: {e}")

    def stream_response(self, request_id: str):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        Supports multiple concurrent clients and reconnection: events are
        appended to a per-request replay log, and every EventSource connection
        reads with its own cursor instead of consuming a shared Queue.
        """
        if not self._sse_request_exists(request_id):
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        self._ensure_sse_state(request_id)
        cond = self.sse_conditions[request_id]
        subscriber_count = self.sse_subscribers.get(request_id, 0) + 1
        self.sse_subscribers[request_id] = subscriber_count
        cleanup_timer = self.sse_cleanup_timers.pop(request_id, None)
        if cleanup_timer:
            try:
                cleanup_timer.cancel()
            except Exception:
                pass

        start_index = 0
        try:
            raw_last_id = getattr(web, "ctx", None)
            raw_last_id = getattr(raw_last_id, "env", {}).get("HTTP_LAST_EVENT_ID", "") if raw_last_id else ""
            params = web.input(last_event_id="")
            query_last_id = str(params.last_event_id or "")
            raw_last_id = str(raw_last_id or query_last_id or "")
            if raw_last_id != "":
                event_offset = self.sse_event_offsets.get(request_id, 0)
                start_index = max(0, int(raw_last_id) + 1 - event_offset)
        except Exception:
            start_index = 0
        index = start_index
        idle_timeout = 600  # 10 minutes without any real event
        deadline = time.time() + idle_timeout
        # After the main reply is done we keep the stream open for a short
        # tail so async post-processing (TTS auto-synthesis) can deliver a
        # `voice_attach` event before the client disconnects.
        POST_DONE_TAIL_SECONDS = 60
        post_done = False
        post_deadline = 0.0

        try:
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
            active_request_ids = self._active_request_ids_for_session(session_id) if session_id else []

            registry = get_cancel_registry()
            cancelled = 0

            if request_id:
                if registry.cancel_request(request_id):
                    cancelled = 1

            if cancelled == 0 and session_id:
                cancelled = registry.cancel_session(session_id)
            subagent_cancel = self._cancel_subagents_for_parent(session_id) if session_id else {
                "cancelledTasks": 0,
                "cancelledRequests": 0,
                "tasks": [],
            }

            if request_id and self._sse_request_exists(request_id):
                self._push_sse_event(request_id, {
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
            logger.error(f"[WebChannel] cancel_request error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            if os.environ.get("ECOREX_DESKTOP") != "1":
                webbrowser.open(f"http://localhost:{port}")
            logger.debug(f"[WebChannel] Opened browser at http://localhost:{port}")
        except Exception as e:
            logger.debug(f"[WebChannel] Could not open browser: {e}")

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
            logger.debug(f"[WebChannel] installation manifest registration skipped: {e}")

        urls = (
            '/', 'RootHandler',
            '/app', 'WebAppRootHandler',
            '/app/(.*)', 'WebAppAssetHandler',
            '/auth/login', 'AuthLoginHandler',
            '/auth/check', 'AuthCheckHandler',
            '/auth/logout', 'AuthLogoutHandler',
            '/message', 'MessageHandler',
            '/upload', 'UploadHandler',
            '/uploads/(.*)', 'UploadsHandler',
            '/api/file', 'FileServeHandler',
            '/api/voice/asr', 'VoiceAsrHandler',
            '/api/voice/tts', 'VoiceTtsHandler',
            '/poll', 'PollHandler',
            '/stream', 'StreamHandler',
            '/cancel', 'CancelHandler',
            '/api/active-requests', 'ActiveRequestsHandler',
            '/api/tool-permissions', 'ToolPermissionHandler',
            '/api/update-check', 'UpdateCheckHandler',
            '/api/file-stat', 'FileStatHandler',
            '/api/open-path', 'OpenPathHandler',
            '/api/project-folder', 'ProjectFolderHandler',
            '/api/capabilities', 'CapabilitiesHandler',
            '/api/agent-install-request', 'AgentInstallRequestHandler',
            '/api/subagents', 'SubagentsHandler',
            '/api/subagents/([^/]+)/(cancel|collect)', 'SubagentActionHandler',
            '/client', 'ClientProxyHandler',
            '/client/(.*)', 'ClientProxyHandler',
            '/chat', 'ChatHandler',
            '/config', 'ConfigHandler',
            '/api/models', 'ModelsHandler',
            '/api/channels', 'ChannelsHandler',
            '/api/weixin/qrlogin', 'WeixinQrHandler',
            '/api/feishu/register', 'FeishuRegisterHandler',
            '/api/tools', 'ToolsHandler',
            '/api/skills', 'SkillsHandler',
            '/api/memory', 'MemoryHandler',
            '/api/memory/content', 'MemoryContentHandler',
            '/api/knowledge/list', 'KnowledgeListHandler',
            '/api/knowledge/read', 'KnowledgeReadHandler',
            '/api/knowledge/graph', 'KnowledgeGraphHandler',
            '/api/scheduler', 'SchedulerHandler',
            '/api/sessions', 'SessionsHandler',
            '/api/sessions/(.*)/generate_title', 'SessionTitleHandler',
            '/api/sessions/(.*)/clear_context', 'SessionClearContextHandler',
            '/api/sessions/(.*)', 'SessionDetailHandler',
            '/api/history', 'HistoryHandler',
            '/api/messages/delete', 'MessageDeleteHandler',
            '/api/ui-state', 'UiStateHandler',
            '/api/installations', 'InstallationsHandler',
            '/api/logs/snapshot', 'LogsSnapshotHandler',
            '/api/logs', 'LogsHandler',
            '/api/version', 'VersionHandler',
            '/assets/(.*)', 'AssetsHandler',
        )
        app = web.application(urls, globals(), autoreload=False)

        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        # Build WSGI app with middleware (same as runsimple but without print)
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
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()
        except OSError as e:
            if e.errno in (48, 98):  # macOS/Linux EADDRINUSE
                logger.error(
                    f"[WebChannel] 端口 {port} 已被占用，可执行 `cow restart` 清理残留进程，"
                    f"或在 config.json 中修改 web_port"
                )
            raise

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
            self._http_server = None


class RootHandler:
    def GET(self):
        return _serve_web_app_asset("")


def _serve_web_app_asset(file_path: str = ""):
    app_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "static", "app"))
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
    fetch("/api/knowledge/read?path=" + encodeURIComponent(relPath), {{ credentials: "same-origin" }})
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


class AuthCheckHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success", "auth_required": False})
        if _check_auth():
            return json.dumps({"status": "success", "auth_required": True, "authenticated": True})
        return json.dumps({"status": "success", "auth_required": True, "authenticated": False})


class AuthLoginHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success"})
        try:
            data = json.loads(web.data())
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"})
        password = str(data.get("password", "") or "")
        expected = _get_web_password()
        if not hmac.compare_digest(password, expected):
            logger.warning("[WebChannel] Invalid login attempt")
            return json.dumps({"status": "error", "message": "Wrong password"})
        token = _create_auth_token()
        web.setcookie("cow_auth_token", token, expires=_session_expire_seconds(),
                       path="/", httponly=True, samesite="Lax")
        return json.dumps({"status": "success"})


class AuthLogoutHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.setcookie("cow_auth_token", "", expires=-1, path="/")
        return json.dumps({"status": "success"})


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
            logger.exception(f"[VoiceAsrHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


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
                    logger.debug(f"[VoiceTtsHandler] persist skipped: {e}")

            return json.dumps({"status": "success", "audio_url": url})
        except Exception as e:
            logger.exception(f"[VoiceTtsHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class UploadsHandler:
    def GET(self, file_name):
        _require_auth()
        try:
            upload_dir = _get_upload_dir()
            full_path = os.path.normpath(os.path.join(upload_dir, file_name))
            if not os.path.abspath(full_path).startswith(os.path.abspath(upload_dir)):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'public, max-age=86400')
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving upload: {e}")
            raise web.notfound()


class FileServeHandler:
    def GET(self):
        _require_auth()
        try:
            params = web.input(path="")
            raw_path = params.path
            if not raw_path:
                raise web.notfound()
            # Resolve symlinks and confine access to explicit preview roots.
            # Default preview is workspace-scoped; serving Home requires an
            # explicit web_file_serve_root override plus the permission broker.
            workspace_root = os.path.realpath(os.path.expanduser(conf().get("agent_workspace", "~/cow")))
            expanded_raw_path = os.path.expanduser(raw_path)
            raw_was_absolute = os.path.isabs(expanded_raw_path)
            file_path = expanded_raw_path if raw_was_absolute else os.path.join(workspace_root, expanded_raw_path.lstrip("/\\"))
            file_path = os.path.realpath(file_path)
            upload_root = os.path.realpath(_get_upload_dir())
            serve_root = conf().get("web_file_serve_root")
            allowed_roots = [
                workspace_root,
                upload_root,
            ]
            if serve_root:
                allowed_roots.append(os.path.realpath(os.path.expanduser(serve_root)))
            if not raw_was_absolute and not any(_is_within_directory(root, file_path) for root in allowed_roots):
                raise web.notfound()
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    file_path,
                    cwd=workspace_root,
                )
            except Exception as exc:
                logger.warning(f"[WebChannel] file permission check failed: {exc}")
                raise web.notfound()
            if not decision.get("allowed", True):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            from urllib.parse import quote
            web.header('Content-Type', content_type)
            web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header('Cache-Control', 'public, max-age=3600')
            with open(file_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving file: {e}")
            raise web.notfound()


def _resolve_web_local_path(path_value: str) -> str:
    workspace_root = os.path.realpath(_get_workspace_root())
    expanded_path = os.path.expanduser(str(path_value or "").strip())
    if not os.path.isabs(expanded_path):
        expanded_path = os.path.join(workspace_root, expanded_path.lstrip("/\\"))
    return os.path.realpath(expanded_path)


class FileStatHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "path": "", "exists": False, "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            path_value = str(body.get("path") or body.get("file_path") or "").strip()
            if not path_value:
                return json.dumps({"status": "error", "path": "", "exists": False, "message": "path is required"}, ensure_ascii=False)
            if path_value.startswith("/api/file"):
                parsed = urllib.parse.urlparse(path_value)
                query = urllib.parse.parse_qs(parsed.query)
                path_value = (query.get("path") or [""])[0] or path_value
            if path_value.startswith("http://") or path_value.startswith("https://"):
                return json.dumps({"status": "remote", "path": path_value, "exists": True}, ensure_ascii=False)

            resolved = _resolve_web_local_path(path_value)
            if not os.path.exists(resolved):
                return json.dumps({"status": "missing", "path": resolved, "exists": False, "message": "path not found"}, ensure_ascii=False)

            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    resolved,
                    cwd=_get_workspace_root(),
                )
                if not decision.get("allowed", True):
                    return json.dumps({
                        "status": "denied",
                        "path": resolved,
                        "exists": False,
                        "message": decision.get("reason") or "file stat blocked by permissions",
                    }, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[WebChannel] file stat permission check failed: {exc}")
                return json.dumps({"status": "error", "path": resolved, "exists": False, "message": "file stat permission check failed"}, ensure_ascii=False)

            is_file = os.path.isfile(resolved)
            payload = {
                "status": "success",
                "path": resolved,
                "exists": True,
                "isFile": is_file,
                "isDirectory": os.path.isdir(resolved),
                "mimeType": mimetypes.guess_type(resolved)[0] or "",
            }
            if is_file:
                payload["sizeBytes"] = os.path.getsize(resolved)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] file stat error: {e}")
            return json.dumps({"status": "error", "path": "", "exists": False, "message": str(e)}, ensure_ascii=False)


class PollHandler:
    def POST(self):
        _require_auth()
        return WebChannel().poll_response()


class CancelHandler:
    def POST(self):
        _require_auth()
        return WebChannel().cancel_request()


class ActiveRequestsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return json.dumps(WebChannel().active_requests_snapshot(), ensure_ascii=False)


class ToolPermissionHandler:
    def GET(self):
        _require_auth()
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            return json.dumps(get_tool_permission_broker().list_pending(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] tool permission list error: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

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
            logger.error(f"[WebChannel] tool permission decision error: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


class UpdateCheckHandler:
    DEFAULT_MANIFEST_URL = "https://www.ecoreai.cn/ecorex-agent/manifest.json"

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
            has_update = bool(artifact and latest_version and self._compare_versions(latest_version, __version__) > 0)
            download_url = self._absolute_download_url(artifact.get("href") if artifact else "")
            message = f"发现新版本 {latest_version}，请前往下载页安装" if has_update else "当前已经是最新版本"
            return json.dumps({
                "status": "success",
                "platform": platform,
                "currentVersion": __version__,
                "latestVersion": latest_version or __version__,
                "version": latest_version or __version__,
                "hasUpdate": has_update,
                "message": message,
                "downloadUrl": download_url,
                "artifact": artifact,
                "recommendedDownloads": manifest.get("recommendedDownloads", {}),
                "update": manifest.get("update", {}),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] update check error: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

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
            if isinstance(artifact, dict) and artifact.get("status") in ("ready", "ready-unsigned")
        ]
        platform_value = platform.lower()
        preferred_id = ""
        if platform_value in ("win32", "windows", "win"):
            preferred_id = "windows-x64"
        elif platform_value in ("darwin", "mac", "macos"):
            preferred_id = "macos-arm64-dmg"
        elif platform_value == "web":
            preferred_id = "webui-windows-x64"
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("id") == preferred_id:
                return artifact
        return {}

    def _absolute_download_url(self, href: str) -> str:
        if not href:
            return "https://www.ecoreai.cn/ecorex-agent/"
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return "https://www.ecoreai.cn/ecorex-agent/" + href.lstrip("./")

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
            if not os.path.exists(path_value):
                return json.dumps({"status": "error", "message": f"path not found: {path_value}"}, ensure_ascii=False)
            if action != "reveal":
                ext = os.path.splitext(path_value.rstrip("/\\"))[1].lower()
                if ext in DANGEROUS_OPEN_EXTENSIONS:
                    return json.dumps({
                        "status": "error",
                        "message": "Refusing to launch executable or script files from WebUI. Use reveal/show in folder and open it manually if you trust it.",
                    }, ensure_ascii=False)

            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    path_value,
                    cwd=workspace_root,
                )
                if not decision.get("allowed", True):
                    return json.dumps({
                        "status": "error",
                        "message": decision.get("reason") or "open path blocked by permissions",
                    }, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[WebChannel] open path permission check failed: {exc}")
                return json.dumps({"status": "error", "message": "open path permission check failed"}, ensure_ascii=False)

            self._open_path(path_value, action)
            return json.dumps({"status": "success", "message": "", "path": path_value}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] open path error: {e}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

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


def _stable_project_id(path_value: str) -> str:
    normalized = os.path.normcase(os.path.realpath(path_value)).replace("\\", "/")
    digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"project-{digest}"


def _project_payload_from_path(path_value: str) -> Dict[str, Any]:
    folder_path = os.path.realpath(os.path.expanduser(path_value))
    if not os.path.isdir(folder_path):
        raise ValueError(f"project folder not found: {folder_path}")
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        broker = get_tool_permission_broker()
        state = broker.get_state()
        if state.get("mode") == "read-only":
            raise PermissionError("Read Only mode blocks project folder registration because it creates .ecorex files.")
        registered = broker.remember_workspace_root(folder_path, access="write", cwd=_get_workspace_root())
        if registered.get("status") == "error":
            raise PermissionError(registered.get("message") or "project folder permission registration failed")
    except Exception as exc:
        logger.warning(f"[WebChannel] project folder permission registration failed: {exc}")
        raise
    project_state_dir = os.path.join(folder_path, ".ecorex")
    project_memory_path = os.path.join(project_state_dir, "project-memory.md")
    project_dreams_path = os.path.join(project_state_dir, "dreams")
    os.makedirs(project_dreams_path, exist_ok=True)
    if not os.path.exists(project_memory_path):
        with open(project_memory_path, "w", encoding="utf-8") as handle:
            handle.write("# Project Memory\n\nEcoreX stores project-specific summaries here. Keep this file concise and do not duplicate global user memory.\n")
    return {
        "id": _stable_project_id(folder_path),
        "name": os.path.basename(folder_path) or folder_path,
        "path": folder_path,
        "memoryPath": project_memory_path,
        "dreamsPath": project_dreams_path,
        "updatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


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
            project = _project_payload_from_path(path_value)
            return json.dumps({"status": "success", "project": project}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] project folder error: {exc}")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


def _tool_result_to_payload(result) -> dict:
    payload = getattr(result, "result", result)
    if not isinstance(payload, dict):
        payload = {"result": payload}
    status = getattr(result, "status", None) or payload.get("status") or "success"
    return {"status": status, **payload}


class CapabilitiesHandler:
    def GET(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.tools.agent_capability.agent_capability import AgentCapabilityTool

            tool = AgentCapabilityTool()
            result = tool.execute({"action": "diagnose"})
            return json.dumps(_tool_result_to_payload(result), ensure_ascii=False)
        except Exception as exc:
            logger.exception("[WebChannel] capability status failed")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


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
        if not pack_id:
            return json.dumps({"status": "error", "message": "packId is required"}, ensure_ascii=False)
        normalized_pack_id = pack_id.strip().lower().replace("_", "-")
        if normalized_pack_id in {"feishu", "lark", "feishu-lark", "lark-feishu"}:
            source_url = "https://github.com/larksuite/oapi-sdk-python"
            mirror_url = "https://gitcode.com/gh_mirrors/oa/oapi-sdk-python.git"
            pypi_mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
            prompt = (
                f"当前用户需要 EcoreX 飞书 / Lark 连接器 `{pack_id}`（{pack_name}）。"
                "该能力包现在是 discovery-only：不要调用 "
                f"`{{\"action\":\"install_pack\",\"pack_id\":\"{pack_id}\"}}`，也不要触发旧的预置安装。"
                "所有飞书 CLI / Lark / skill 安装入口必须先走内置 `find` skill（能力 gate 名称为 `find-skill`）做发现和安装方案选择。"
                "请先用 `agent_capability` 的 `diagnose` 确认 `find` skill / `find-skill` 能力已加载；如未加载，先启用/安装它，再继续。"
                "通过 `find` skill 查找飞书/Lark 相关 skill、connector 或官方 SDK 源，并优先按它返回的 GitHub 地址安装。"
                "如果最终调用 `agent_capability` 的 `install_skill`，必须带上 `discovery_source: \"find-skill\"` 或 `find_skill_result`。"
                f"如果 `find` skill 发现链路中的 GitHub 网络超时或不可达，降级使用国内 Git 镜像：`python -m pip install --upgrade \"git+{mirror_url}\"`。"
                f"如果 Git 镜像仍不可用，再降级使用 PyPI 国内镜像：`python -m pip install -i {pypi_mirror} --upgrade lark-oapi`。"
                f"如果 `find` skill 没有返回可安装 skill，再使用官方 GitHub 源兜底：`python -m pip install --upgrade \"git+{source_url}.git\"`。"
                "每个来源最多重试一次，避免长时间卡住。"
                "完成后再调用一次 `agent_capability` 的 `diagnose`，给用户正文只保留安装结论、使用状态和必要下一步；详细 stdout/stderr/log path 放在调用过程里。"
            )
            extra_fields = {
                "discoveryOnly": True,
                "sourceUrl": source_url,
                "mirrorUrls": [mirror_url],
                "installHint": (
                    "先通过内置 find skill / find-skill 能力发现和安装；GitHub 超时后使用 GitCode 国内镜像；仍失败时使用清华 PyPI 镜像安装 lark-oapi。"
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
    def __init__(self, session_id: str, workspace_dir: str):
        self._current_session_id = session_id
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
            logger.exception("[WebChannel] subagent list failed")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

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
            tool.context = _SubagentContext(str(data.get("sessionId") or data.get("session_id") or ""), _get_workspace_root())
            return json.dumps(_tool_result_to_payload(tool.execute({"action": action, **data})), ensure_ascii=False)
        except Exception as exc:
            logger.exception("[WebChannel] subagent action failed")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


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
            logger.exception("[WebChannel] subagent route action failed")
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)


class StreamHandler:
    def GET(self):
        _require_auth()
        params = web.input(request_id='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(request_id)


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

    DEFAULT_CLIENT_BASE = "https://www.ecoreai.cn/ecorex-agent/client"
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
        configured = (
            os.environ.get("ECOREX_WEB_CLIENT_BASE")
            or conf().get("web_client_base")
            or conf().get("admin_client_base")
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
        headers = {"User-Agent": "EcoreX-WebUI/0.1.15"}
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
            web.ctx.status = f"{exc.code} {exc.reason}"
            web.header("Content-Type", exc.headers.get("Content-Type", "application/json; charset=utf-8"))
            web.header("Cache-Control", "no-store")
            return raw
        except Exception as exc:
            logger.warning(f"[WebChannel] Admin client proxy failed: {exc}")
            return self._json_response(502, {
                "ok": False,
                "error": "admin client proxy failed",
                "detail": str(exc),
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
        const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO,
        const.MINIMAX_M3, const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_7,
        # claude-fable-5 is intentionally placed at the end of the Claude
        # group here: it is expensive, so avoid surfacing it too early in
        # the LinkAI dropdown.
        const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS, const.CLAUDE_FABLE_5,
        const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7,
        const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS,
        const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE,
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
            "models": [const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO, const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER],
        }),
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.MINIMAX_M3, const.MINIMAX_M2_7, const.MINIMAX_M2_7_HIGHSPEED],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "api_base_placeholder": _PLACEHOLDER_GEMINI,
            "models": [const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o],
        }),
        ("zhipu", {
            "label": {"zh": "智谱AI", "en": "GLM"},
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "api_base_placeholder": _PLACEHOLDER_ZHIPU,
            "models": [const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7],
        }),
        ("dashscope", {
            "label": {"zh": "通义千问", "en": "Qwen"},
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS],
        }),
        ("doubao", {
            "label": {"zh": "豆包", "en": "Doubao"},
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "api_base_placeholder": _PLACEHOLDER_DOUBAO,
            "models": [const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2],
        }),
        ("qianfan", {
            "label": {"zh": "百度千帆", "en": "ERNIE"},
            "api_key_field": "qianfan_api_key",
            "api_base_key": "qianfan_api_base",
            "api_base_default": "https://qianfan.baidubce.com/v2",
            "api_base_placeholder": _PLACEHOLDER_QIANFAN,
            "models": [const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K],
        }),
        ("mimo", {
            "label": {"zh": "小米 MiMo", "en": "MiMo"},
            "api_key_field": "mimo_api_key",
            "api_base_key": "mimo_api_base",
            "api_base_default": "https://api.xiaomimimo.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.MIMO_V2_5_PRO, const.MIMO_V2_5],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": _RECOMMENDED_MODELS,
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
                "model": local_config.get("model", ""),
                "bot_type": "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", ""),
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", 258000),
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
            logger.error(f"Error getting config: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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

            if not applied:
                return json.dumps({"status": "error", "message": "no valid keys to update"})

            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "config.json")
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
                    Bridge().reset_bot()
                    logger.info("[WebChannel] Bridge bot routing reset due to config change")
                except Exception as reset_err:
                    logger.warning(f"[WebChannel] Failed to reset bridge: {reset_err}")

            return json.dumps({"status": "success", "applied": applied}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return json.dumps({"status": "error", "message": str(e)})


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
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.json",
        )

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

    @classmethod
    def _chat_capability(cls, local_config: dict) -> dict:
        """Main chat model — drives the agent. bot_type maps to a provider id."""
        bot_type = local_config.get("bot_type") or ""
        provider_id = "openai" if bot_type == "chatGPT" else bot_type
        if provider_id not in ConfigHandler.PROVIDER_MODELS and local_config.get("use_linkai"):
            provider_id = "linkai"
        return {
            "editable": True,
            "current_provider": provider_id,
            "current_model": local_config.get("model", ""),
            "providers": list(ConfigHandler.PROVIDER_MODELS.keys()),
            "use_linkai": bool(local_config.get("use_linkai", False)),
        }

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

        # Provider resolution priority:
        #   1. Explicit `skills.image-generation.provider` (persisted via UI;
        #      supports custom model names that prefix-inference can't catch).
        #   2. Scan per-provider model catalog by model name.
        # Empty provider keeps the dropdown on "auto" when we can't tell.
        inferred_provider = ""
        if explicit_provider and explicit_provider in cls._IMAGE_PROVIDER_MODELS:
            inferred_provider = explicit_provider
        elif explicit_model:
            for pid, models in cls._IMAGE_PROVIDER_MODELS.items():
                for entry in models:
                    val = entry if isinstance(entry, str) else (entry.get("value") or "")
                    if val == explicit_model:
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
            "strategy": "specified" if explicit_model else "auto",
            "current_provider": inferred_provider,
            "current_model": explicit_model,
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
            logger.error(f"[ModelsHandler] GET failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            logger.error(f"[ModelsHandler] POST failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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

        if provider_id:
            bot_type_value = "chatGPT" if provider_id == "openai" else provider_id
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

        if not applied:
            return json.dumps({"status": "success", "applied": {}, "noop": True})

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] chat updated: {applied}")
        self._reset_bridge()
        return json.dumps({"status": "success", "applied": applied})

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
            logger.warning(f"[ModelsHandler] Bridge voice refresh failed: {e}")

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
    def _reset_bridge() -> None:
        try:
            from bridge.bridge import Bridge
            Bridge().reset_bot()
            logger.info("[ModelsHandler] Bridge bot routing reset")
        except Exception as e:
            logger.warning(f"[ModelsHandler] Bridge reset failed: {e}")


class ChannelsHandler:
    """API for managing external channel configurations (feishu, dingtalk, etc)."""

    CHANNEL_DEFS = OrderedDict([
        ("weixin", {
            "label": {"zh": "微信", "en": "WeChat"},
            "icon": "fa-comment",
            "color": "emerald",
            "fields": [],
        }),
        ("feishu", {
            "label": {"zh": "飞书", "en": "Feishu"},
            "icon": "fa-paper-plane",
            "color": "blue",
            "fields": [
                {"key": "feishu_app_id", "label": "App ID", "type": "text"},
                {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("dingtalk", {
            "label": {"zh": "钉钉", "en": "DingTalk"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "dingtalk_client_id", "label": "Client ID", "type": "text"},
                {"key": "dingtalk_client_secret", "label": "Client Secret", "type": "secret"},
            ],
        }),
        ("wecom_bot", {
            "label": {"zh": "企微智能机器人", "en": "WeCom Bot"},
            "icon": "fa-robot",
            "color": "emerald",
            "fields": [
                {"key": "wecom_bot_id", "label": "Bot ID", "type": "text"},
                {"key": "wecom_bot_secret", "label": "Secret", "type": "secret"},
            ],
        }),
        ("qq", {
            "label": {"zh": "QQ 机器人", "en": "QQ Bot"},
            "icon": "fa-comment",
            "color": "blue",
            "fields": [
                {"key": "qq_app_id", "label": "App ID", "type": "text"},
                {"key": "qq_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("wechatcom_app", {
            "label": {"zh": "企微自建应用", "en": "WeCom App"},
            "icon": "fa-building",
            "color": "emerald",
            "fields": [
                {"key": "wechatcom_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechatcomapp_agent_id", "label": "Agent ID", "type": "text"},
                {"key": "wechatcomapp_secret", "label": "Secret", "type": "secret"},
                {"key": "wechatcomapp_token", "label": "Token", "type": "secret"},
                {"key": "wechatcomapp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatcomapp_port", "label": "Port", "type": "number", "default": 9898},
            ],
        }),
        ("wechat_kf", {
            "label": {"zh": "微信客服", "en": "WeChat Customer Service"},
            "icon": "fa-headset",
            "color": "emerald",
            "fields": [
                {"key": "wechat_kf_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechat_kf_secret", "label": "Secret", "type": "secret"},
                {"key": "wechat_kf_token", "label": "Token", "type": "secret"},
                {"key": "wechat_kf_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechat_kf_port", "label": "Port", "type": "number", "default": 9888},
            ],
        }),
        ("wechatmp", {
            "label": {"zh": "公众号", "en": "WeChat MP"},
            "icon": "fa-comment-dots",
            "color": "emerald",
            "fields": [
                {"key": "wechatmp_app_id", "label": "App ID", "type": "text"},
                {"key": "wechatmp_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "wechatmp_token", "label": "Token", "type": "secret"},
                {"key": "wechatmp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatmp_port", "label": "Port", "type": "number", "default": 8080},
            ],
        }),
        ("telegram", {
            "label": {"zh": "Telegram", "en": "Telegram"},
            "icon": "fa-paper-plane",
            "color": "sky",
            "fields": [
                {"key": "telegram_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
        ("slack", {
            "label": {"zh": "Slack", "en": "Slack"},
            "icon": "fa-hashtag",
            "color": "purple",
            "fields": [
                {"key": "slack_bot_token", "label": "Bot Token (xoxb-)", "type": "secret"},
                {"key": "slack_app_token", "label": "App Token (xapp-)", "type": "secret"},
            ],
        }),
        ("discord", {
            "label": {"zh": "Discord", "en": "Discord"},
            "icon": "fa-discord",
            "color": "indigo",
            "fields": [
                {"key": "discord_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
    ])

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
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _parse_channel_list(raw) -> list:
        if isinstance(raw, list):
            return [ch.strip() for ch in raw if ch.strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @classmethod
    def _active_channel_set(cls) -> set:
        return set(cls._parse_channel_list(conf().get("channel_type", "")))

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            active_channels = self._active_channel_set()
            channels = []
            for ch_name, ch_def in self.CHANNEL_DEFS.items():
                fields_out = []
                for f in ch_def["fields"]:
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                    if f["type"] == "secret" and raw_val:
                        display_val = self._mask_secret(str(raw_val))
                    else:
                        display_val = raw_val
                    fields_out.append({
                        "key": f["key"],
                        "label": f["label"],
                        "type": f["type"],
                        "value": display_val,
                        "default": f.get("default", ""),
                    })
                ch_info = {
                    "name": ch_name,
                    "label": ch_def["label"],
                    "icon": ch_def["icon"],
                    "color": ch_def["color"],
                    "active": ch_name in active_channels,
                    "fields": fields_out,
                }
                if ch_name == "weixin" and ch_name in active_channels:
                    ch_info["login_status"] = self._get_weixin_login_status()
                channels.append(ch_info)
            return json.dumps({"status": "success", "channels": channels}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = body.get("channel")

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
            logger.error(f"[WebChannel] Channels POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_save(self, channel_name: str, updates: dict):
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        if not applied:
            return json.dumps({"status": "error", "message": "no valid fields to update"})

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' config updated: {list(applied.keys())}")

        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels:
            should_restart = True
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
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {e}")

        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """Save config fields, add channel to channel_type, and start it."""
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        # Feishu connected via web console must use websocket (long connection) mode
        if channel_name == "feishu":
            updates.setdefault("feishu_event_mode", "websocket")
            valid_keys.add("feishu_event_mode")

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        existing = self._parse_channel_list(conf().get("channel_type", ""))
        if channel_name not in existing:
            existing.append(channel_name)
        new_channel_type = ",".join(existing)
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")

        def _do_start():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr is None:
                    logger.warning(f"[WebChannel] ChannelManager not available, cannot start '{channel_name}'")
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
                logger.info(f"[WebChannel] Channel '{channel_name}' start completed")
            except Exception as e:
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {e}",
                             exc_info=True)

        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        existing = [ch for ch in existing if ch != channel_name]
        new_channel_type = ",".join(existing)

        local_config = conf()
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

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
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {e}",
                               exc_info=True)

        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
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
            logger.error(f"[WebChannel] WeixinQr GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            logger.error(f"[WebChannel] WeixinQr POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            return json.dumps({"status": "error", "message": str(e)})

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
                        "lark-oapi SDK 未安装。请先让当前 agent 通过内置 find skill / find-skill 能力发现飞书/Lark 连接器；"
                        "GitHub 超时后再降级使用 GitCode 国内镜像或清华 PyPI 镜像安装 lark-oapi。"
                    )
                return

            def _on_qr(info):
                # SDK 拿到二维码 URL 后立即回调；写入 state 让前端 GET 立刻能拿到
                with cls._lock:
                    cls._state["url"] = info.get("url", "")
                    cls._state["expire_in"] = info.get("expire_in", 600)
                    cls._state["qr_image"] = cls._qr_to_data_uri(info.get("url", ""))
                    cls._state["status"] = "pending"
                logger.info(f"[FeishuRegister] QR ready, expire_in={info.get('expire_in')}s")

            def _on_status(info):
                # 过滤掉 polling 心跳（每 5 秒一次，纯噪音）；
                # 保留 slow_down / domain_switched 等真正的状态切换事件
                status = info.get("status")
                if status == "polling":
                    return
                logger.info(f"[FeishuRegister] SDK status: {info}")

            try:
                result = lark.register_app(
                    on_qr_code=_on_qr,
                    on_status_change=_on_status,
                    source="ecorex",
                    cancel_event=cancel_event,
                )
                with cls._lock:
                    cls._state["status"] = "done"
                    cls._state["app_id"] = result.get("client_id", "")
                    cls._state["app_secret"] = result.get("client_secret", "")
                logger.info(f"[FeishuRegister] App created: app_id={result.get('client_id')}")
            except Exception as e:
                err_msg = str(e)
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
                logger.warning(f"[FeishuRegister] Register failed ({err_cls}): {err_msg}")

        threading.Thread(target=_worker, daemon=True, name="feishu-register").start()

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
            logger.error(f"[WebChannel] FeishuRegister GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
                if status == "done":
                    payload = {
                        "status": "success",
                        "register_status": "done",
                        "app_id": self._state.get("app_id", ""),
                        "app_secret": self._state.get("app_secret", ""),
                    }
                    # 一次性返回凭据后清掉，避免敏感信息长期驻留内存
                    self._state = {}
                    return json.dumps(payload)
                if status in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "success",
                        "register_status": status,
                        "message": self._state.get("error", ""),
                    })
                # pending / starting：还在等用户扫码
                return json.dumps({
                    "status": "success",
                    "register_status": "pending",
                })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _get_workspace_root():
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace", "~/cow"))


class ToolsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            tm = ToolManager()
            if not tm.tool_classes:
                tm.load_tools()
            tools = []
            for name, cls in tm.tool_classes.items():
                try:
                    instance = cls()
                    tools.append({
                        "name": name,
                        "description": instance.description,
                    })
                except Exception:
                    tools.append({"name": name, "description": ""})
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SkillsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            skills = service.query()
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


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
            logger.error(f"[WebChannel] Memory API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


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
            logger.error(f"[WebChannel] Memory content API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            tasks = store.list_tasks()
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(page='1', page_size='50')
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.list_sessions(
                channel_type="web",
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Sessions API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            store.clear_session(session_id)

            # Also remove the Agent instance from AgentBridge if exists
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Removed agent instance for session {session_id}")
            except Exception:
                pass

            channel = WebChannel()
            channel.session_queues.pop(session_id, None)

            logger.info(f"[WebChannel] Session deleted: {session_id}")
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            title = body.get("title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "title required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            found = store.rename_session(session_id, title)
            if not found:
                return json.dumps({"status": "error", "message": "session not found"})
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session rename error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionTitleHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            body = json.loads(web.data())
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            if not user_message:
                return json.dumps({"status": "error", "message": "user_message required"})

            title = _generate_session_title(user_message, assistant_reply)

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            updated = store.rename_session(session_id, title)
            logger.info(f"[WebChannel] Session title set: sid={session_id}, title='{title}', db_updated={updated}")

            return json.dumps({"status": "success", "title": title}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Title generation error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionClearContextHandler:
    def POST(self, session_id: str):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            new_seq = store.clear_context(session_id)

            # Delete the agent instance so a fresh one is created on the next message
            try:
                from bridge.bridge import Bridge
                bridge = Bridge()
                ab = bridge.get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Cleared agent instance for session {session_id}")
            except Exception:
                pass

            return json.dumps({"status": "success", "context_start_seq": new_seq})
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MessageDeleteHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            session_id = data.get('session_id', '').strip()
            user_seq = data.get('user_seq')
            delete_user = data.get('delete_user', True)
            cascade = data.get('cascade', False)
            
            if not session_id or user_seq is None:
                return json.dumps({"status": "error", "message": "session_id and user_seq required"})
            
            # 1. Delete from database
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            deleted = store.delete_message_pair(session_id, int(user_seq), delete_user=delete_user, cascade=cascade)

            # 2. Sync agent's in-memory context so its next turn sees the
            # same history as the DB. Handled by the agent_bridge helper.
            try:
                from bridge import Bridge
                Bridge().get_agent_bridge().sync_session_messages_from_store(session_id)
            except Exception as sync_err:
                logger.warning(f"[WebChannel] Failed to sync agent memory: {sync_err}")

            return json.dumps({"status": "success", "deleted": deleted}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Message delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class UiStateHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.ecorex_workspace import load_ui_state
            state = load_ui_state(_get_workspace_root())
            return json.dumps({"status": "success", "state": state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] UI state GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 1024 * 1024:
                return json.dumps({"status": "error", "message": "ui state payload too large"})
            body = json.loads(raw)
            incoming = body.get("state", body)
            if not isinstance(incoming, dict):
                return json.dumps({"status": "error", "message": "state must be an object"})
            from common.ecorex_workspace import save_ui_state
            state = save_ui_state(_get_workspace_root(), incoming)
            return json.dumps({"status": "success", "state": state}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] UI state PUT error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        return self.PUT()


class InstallationsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common.ecorex_workspace import load_installation_manifest
            manifest = load_installation_manifest(_get_workspace_root())
            return json.dumps({"status": "success", "manifest": manifest}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] installations GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

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
            logger.error(f"[WebChannel] installations POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _log_snapshot_payload(max_lines: int = 200) -> Dict[str, Any]:
    from config import get_root

    log_path = _resolve_run_log_path(Path(get_root()))
    try:
        from agent.tools.host_diagnostics.host_diagnostics import _tail_text

        tail = _tail_text(log_path, max_lines=max(1, min(500, int(max_lines or 200))), cwd=str(log_path.parent))
        return {
            "status": "success" if tail.get("exists") and not tail.get("blocked") else "error",
            "type": "snapshot",
            "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "log": tail,
            "content": "\n".join(tail.get("lines") or []),
            "message": tail.get("reason") or tail.get("error") or ("" if tail.get("exists") else "run.log not found"),
        }
    except Exception as exc:
        return {
            "status": "error",
            "type": "snapshot",
            "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "log": {"path": str(log_path), "exists": False, "error": str(exc), "lines": []},
            "content": "",
            "message": str(exc),
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
        logger.debug(f"[WebChannel] active log path lookup failed: {exc}")
    return runtime_root / "run.log"


def _parse_log_line_limit(value: Any, default: int = 200) -> int:
    try:
        return max(1, min(500, int(value or default)))
    except (TypeError, ValueError):
        return default


class LogsSnapshotHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        params = web.input(lines="200")
        return json.dumps(_log_snapshot_payload(_parse_log_line_limit(params.lines)), ensure_ascii=False)


class LogsHandler:
    def GET(self):
        _require_auth()
        accept = (web.ctx.env.get("HTTP_ACCEPT") or "").lower()
        if "text/event-stream" not in accept:
            web.header('Content-Type', 'application/json; charset=utf-8')
            params = web.input(lines="200")
            return json.dumps(_log_snapshot_payload(_parse_log_line_limit(params.lines)), ensure_ascii=False)

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        from config import get_root
        log_path = _resolve_run_log_path(Path(get_root()))

        def generate():
            if not log_path.is_file():
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                from agent.tools.host_diagnostics.host_diagnostics import _mask, _tail_text

                tail = _tail_text(log_path, max_lines=200, cwd=str(log_path.parent))
                if tail.get("blocked"):
                    payload = json.dumps({
                        "type": "error",
                        "message": tail.get("reason") or "run.log read blocked by permissions",
                    }, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode('utf-8')
                    return
                if not tail.get("exists"):
                    yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                    return
                chunk = "\n".join(tail.get("lines") or [])
                if chunk:
                    chunk += "\n"
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line", "content": _mask(line)}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


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
            logger.error(f"Error serving static file: {e}", exc_info=True)
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
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


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
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeGraphHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            svc = KnowledgeService(_get_workspace_root())
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli import __version__
        from common.ecorex_release_notes import get_current_release_notes

        return json.dumps({
            "version": __version__,
            "releaseNotes": get_current_release_notes(),
        }, ensure_ascii=False)
