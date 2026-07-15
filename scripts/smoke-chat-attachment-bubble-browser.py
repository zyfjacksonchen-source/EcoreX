#!/usr/bin/env python3
"""Integrated browser smoke for Codex-like user attachment bubbles.

Unlike the static CSS smoke, this runs the built desktop React app and feeds a
runtime history message through the same API contracts used by the real UI.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


ARTIFACT_DIR = ROOT / "docs" / "v0.2.3" / "artifacts"

MESSAGE_TEXT = (
    "\u5e2e\u5fd9\u7f8e\u5316\u4e0b\uff0c\u4e3b\u8272\u8fd8\u662f"
    "\u7528\u4ea6\u82af\u7684\u6a59\u8272\uff0c\u6211\u73b0\u5728"
    "\u7684\u8868\u6846\u592a\u5f3a\u4e86\uff0c\u5e2e\u5fd9\u8c03"
    "\u6574\u6210\u50cf Codex \u8fd9\u6837\u7b80\u7ea6\u7684\u6837\u5f0f"
)
DOC_NAME = "\u6d59\u6c5f26Q2\u590d\u76d8\u53caQ3\u89c4\u5212.pptx"


def _preview_data_url() -> str:
    image_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="112" viewBox="0 0 160 112">'
        '<rect width="160" height="112" rx="12" fill="#fff7ed"/>'
        '<rect x="12" y="12" width="136" height="18" rx="5" fill="#f97316"/>'
        '<rect x="16" y="42" width="36" height="10" rx="2" fill="#fb923c"/>'
        '<rect x="62" y="42" width="36" height="10" rx="2" fill="#fed7aa"/>'
        '<rect x="108" y="42" width="36" height="10" rx="2" fill="#f97316"/>'
        '<rect x="16" y="62" width="128" height="7" rx="2" fill="#f97316"/>'
        '<rect x="16" y="76" width="128" height="7" rx="2" fill="#22c55e"/>'
        '<rect x="16" y="90" width="88" height="7" rx="2" fill="#38bdf8"/>'
        '</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(image_svg.encode("utf-8")).decode("ascii")


def _stub_script() -> str:
    preview = _preview_data_url()
    message_text = json.dumps(MESSAGE_TEXT, ensure_ascii=False)
    doc_name = json.dumps(DOC_NAME, ensure_ascii=False)
    preview_json = json.dumps(preview)
    return rf"""
(() => {{
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-last-active-session-id', 'chat-bubble-integrated-session');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  const now = Math.floor(Date.now() / 1000);
  const sessionId = 'chat-bubble-integrated-session';
  const userMessage = {{
    role: 'user',
    content: {message_text},
    created_at: now,
    seq: 1,
    request_id: 'req-chat-bubble-integrated',
    extras: {{
      attachments: [
        {{
          file_path: 'artifact://docx-pptx-ref',
          file_name: {doc_name},
          file_type: 'file'
        }},
        {{
          file_path: 'artifact://image-preview-ref',
          file_name: '37304f6f5e407a4.png',
          file_type: 'image',
          preview_url: {preview_json}
        }}
      ]
    }}
  }};
  const assistantMessage = {{
    role: 'assistant',
    content: '收到，已按附件和文字要求进入样式核对。',
    created_at: now + 1,
    seq: 2,
    request_id: 'req-chat-bubble-integrated'
  }};
  const sessions = [{{
    session_id: sessionId,
    title: 'Chat Attachment Bubble Integrated Smoke',
    created_at: now - 120,
    last_active: now,
    msg_count: 2,
    scope: 'general',
    project: null
  }}];
  const history = [userMessage, assistantMessage];
  window.__ecorexSmoke = {{ calls: {{ history: 0, runtimeProjection: 0, sessions: 0 }} }};
  const ok = (value) => Promise.resolve(value);
  window.ecorexDesktop = {{
    platform: 'web',
    getEnterpriseSession: () => ok({{
      token: 'smoke-token',
      user: {{ name: 'Smoke User', email: 'smoke@example.test' }},
      quota: {{ allowed: true, dailyUsed: 0, dailyLimit: 100000, weeklyUsed: 0, weeklyLimit: 100000 }}
    }}),
    getSidecarStatus: () => ok({{ state: 'running', message: 'Smoke runtime running', webPort: 9899 }}),
    onSidecarStatus: (listener) => {{
      setTimeout(() => listener({{ state: 'running', message: 'Smoke runtime running', webPort: 9899 }}), 0);
      return () => {{}};
    }},
    checkEnterpriseQuota: () => ok({{ ok: true, quota: {{ allowed: true }} }}),
    refreshEnterprisePolicy: () => ok({{ configured: true, changed: false }}),
    reportTelemetry: () => ok({{ status: 'success' }}),
    setWindowTheme: () => ok(undefined),
    openPath: () => ok({{ status: 'success' }}),
    apiJson: async (request) => {{
      const rawPath = typeof request === 'string' ? request : String(request && request.path || '');
      const method = typeof request === 'object' && request && request.method ? String(request.method) : 'GET';
      const url = new URL(rawPath || '/', window.location.origin);
      const path = url.pathname;
      if (path === '/api/version') return {{ version: '0.2.3-chat-bubble-integrated-smoke' }};
      if (path === '/api/sessions') {{
        window.__ecorexSmoke.calls.sessions += 1;
        return {{ status: 'success', sessions, total: sessions.length }};
      }}
      if (path === '/api/history') {{
        window.__ecorexSmoke.calls.history += 1;
        return {{ status: 'success', messages: history, context_start_seq: 0, total: history.length, has_more: false }};
      }}
      if (path === '/api/runtime-projection') {{
        window.__ecorexSmoke.calls.runtimeProjection += 1;
        return {{
          status: 'success',
          projection: {{
            request_id: url.searchParams.get('request_id') || '',
            session_id: url.searchParams.get('session_id') || sessionId,
            latest_event_id: 2,
            event_count: 2,
            messages: history,
            requests: [],
            history: {{ messages: history, has_more: false }}
          }}
        }};
      }}
      if (path === '/api/active-requests') return {{ status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {{}}, staleLocks: [] }};
      if (path === '/api/ui-state') return method === 'GET' ? {{ status: 'success', state: {{}} }} : {{ status: 'success' }};
      if (path === '/api/tools') return {{ status: 'success', tools: [] }};
      if (path === '/api/skills') return {{ status: 'success', skills: [] }};
      if (path === '/api/models') return {{ status: 'success', providers: [], capabilities: {{}} }};
      if (path === '/api/extensions') return {{ status: 'success', extensions: [], count: 0, summary: {{}} }};
      if (path === '/api/channels') return {{ status: 'success', channels: [] }};
      if (path === '/api/external-connections') return {{ status: 'success', connections: [], summary: {{ total: 0 }} }};
      if (path === '/api/scheduler') return {{ status: 'success', enabled: true, initialized: true, running: false, tasks: [], taskCount: 0, counts: {{}} }};
      if (path === '/api/tool-permissions') return {{ status: 'success', mode: 'smart-ask', grantsCount: 0 }};
      if (path === '/api/memory/files') return {{ status: 'success', files: [] }};
      if (path === '/api/capabilities') return {{ status: 'success', packs: [] }};
      if (path === '/message') return {{ status: 'success', request_id: 'req-chat-bubble-integrated-send' }};
      return {{ status: 'success' }};
    }}
  }};
  class QuietEventSource {{
    constructor() {{
      this.readyState = 1;
      setTimeout(() => {{ if (typeof this.onopen === 'function') this.onopen({{ type: 'open', data: '' }}); }}, 0);
    }}
    addEventListener() {{}}
    removeEventListener() {{}}
    close() {{ this.readyState = 2; }}
  }}
  window.EventSource = QuietEventSource;
}})();
"""


def _probe_script() -> str:
    return r"""
async () => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = (label, predicate, timeout = 12000) => new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - started > timeout) return reject(new Error(`timeout waiting for ${label}`));
      setTimeout(tick, 50);
    };
    tick();
  });
  const rectOf = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  };
  const styleOf = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const s = getComputedStyle(el);
    return {
      background: s.backgroundColor,
      borderColor: s.borderColor,
      borderRadius: s.borderRadius,
      display: s.display
    };
  };
  await wait('sidebar smoke session', () => Array.from(document.querySelectorAll('button, [role="button"], .session-item')).some((el) => (el.innerText || '').includes('Chat Attachment Bubble Integrated Smoke')));
  const smokeSession = Array.from(document.querySelectorAll('button, [role="button"], .session-item')).find((el) => (el.innerText || '').includes('Chat Attachment Bubble Integrated Smoke'));
  smokeSession.click();
  await wait('integrated user message with attachments', () => {
    const user = document.querySelector('.message.user.has-files');
    return Boolean(
      user &&
      user.querySelector('.message-files button') &&
      user.querySelector('.message-files img') &&
      user.querySelector('.message-text-bubble') &&
      user.innerText.includes('Codex')
    );
  });
  const user = document.querySelector('.message.user.has-files');
  const fileButtons = Array.from(user.querySelectorAll('.message-files button')).map((el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return { width: r.width, height: r.height, background: s.backgroundColor, borderColor: s.borderColor };
  });
  const body = rectOf('.message.user.has-files .message-body');
  const files = rectOf('.message.user.has-files .message-files');
  const bubble = rectOf('.message.user.has-files .message-text-bubble');
  const bodyStyle = styleOf('.message.user.has-files .message-body');
  const bubbleStyle = styleOf('.message.user.has-files .message-text-bubble');
  const text = user.innerText || '';
  assert(body && files && bubble, 'message geometry missing');
  assert(fileButtons.length === 2, 'expected two compact attachment buttons');
  assert(fileButtons[0].height <= 52, 'document attachment too tall');
  assert(fileButtons[1].width <= 120 && fileButtons[1].height <= 92, 'image thumbnail too large');
  assert(files.width <= body.width + 1, 'attachment row exceeds message body');
  assert(bubble.width <= body.width + 1, 'text bubble exceeds message body');
  assert(bodyStyle.background === 'rgba(0, 0, 0, 0)', 'user message wrapper background is not transparent');
  assert(!document.body.innerText.includes('Run Center'), 'Run Center leaked into integrated chat smoke');
  return {
    historyCalls: window.__ecorexSmoke.calls.history,
    runtimeProjectionCalls: window.__ecorexSmoke.calls.runtimeProjection,
    userMessageCount: document.querySelectorAll('.message.user.has-files').length,
    attachmentButtonCount: fileButtons.length,
    imageAttachmentCount: document.querySelectorAll('.message.user.has-files .message-files img').length,
    textIncludesCodex: text.includes('Codex'),
    body,
    files,
    bubble,
    bodyStyle,
    bubbleStyle,
    fileButtons,
    runCenterHidden: !document.body.innerText.includes('Run Center')
  };
}
"""


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    app_root = Path(args.app_root)
    if not app_root.is_absolute():
        app_root = ROOT / app_root
    screenshot_target = Path(args.screenshot)
    if not screenshot_target.is_absolute():
        screenshot_target = ROOT / screenshot_target
    screenshot_target.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with static_site_server(app_root) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            metrics = page.evaluate(_probe_script())
            page.locator('.message.user.has-files').screenshot(path=str(screenshot_target))
            browser.close()

    status = "PASS" if not errors and metrics.get("userMessageCount", 0) >= 1 else "FAIL"
    return {
        "status": status,
        "durationMs": round((time.time() - started) * 1000),
        "screenshot": _relative(screenshot_target),
        "metrics": metrics,
        "consoleErrorCount": len(errors),
        "redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run integrated chat attachment bubble browser smoke.")
    parser.add_argument("--app-root", default="desktop/dist")
    parser.add_argument("--artifact", default="docs/v0.2.3/artifacts/chat-attachment-bubble-browser-smoke.json")
    parser.add_argument("--screenshot", default="docs/v0.2.3/artifacts/chat-attachment-bubble-browser.png")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    args = parser.parse_args()
    try:
        result = run_smoke(args)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "errorType": exc.__class__.__name__, "errorLength": len(str(exc)), "redacted": True}, ensure_ascii=True, indent=2))
        return 1
    artifact = Path(args.artifact)
    if not artifact.is_absolute():
        artifact = ROOT / artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
