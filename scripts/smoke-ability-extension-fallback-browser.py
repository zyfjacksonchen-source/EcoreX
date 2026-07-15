#!/usr/bin/env python3
"""Browser smoke for Settings > Abilities builtin-tool fallback.

The regression this guards: `/api/extensions` exposes builtin `tool:*` entries,
but `/api/tools` is empty or stale, causing the Settings ability cards to show
every built-in capability as `未加载`.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


ARTIFACT_DIR = ROOT / "docs" / "v0.2.3" / "artifacts"


def _extension(tool: str) -> dict[str, Any]:
    return {
        "id": f"tool:{tool}",
        "type": "builtin_tool",
        "displayName": tool,
        "description": f"{tool} builtin tool",
        "origin": "first-party",
        "enabled": True,
        "installed": True,
        "policy": "built-in",
        "status": "ready",
        "provides": ["tool", tool],
    }


def _stub_script() -> str:
    extensions = json.dumps(
        [
            _extension("bash"),
            _extension("read"),
            _extension("write"),
            _extension("edit"),
            _extension("ls"),
            _extension("find"),
            _extension("ocr"),
            _extension("vision"),
            _extension("scheduler"),
            _extension("feishu_cli"),
            _extension("browser"),
            _extension("optional_abilities"),
            _extension("agent_capability"),
            _extension("host_diagnostics"),
        ],
        ensure_ascii=False,
    )
    return rf"""
(() => {{
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  const now = Math.floor(Date.now() / 1000);
  const sessions = [{{
    session_id: 'ability-extension-fallback-session',
    title: 'Ability Extension Fallback Smoke',
    created_at: now - 60,
    last_active: now,
    msg_count: 0,
    scope: 'general',
    project: null
  }}];
  const extensions = {extensions};
  window.__ecorexSmoke = {{ calls: {{ tools: 0, extensions: 0 }} }};
  const ok = (value) => Promise.resolve(value);
  window.ecorexDesktop = {{
    platform: 'web',
    getEnterpriseSession: () => ok({{
      token: 'smoke-token',
      user: {{ name: 'Smoke User', email: 'smoke@example.test' }},
      quota: {{ allowed: true }}
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
      if (path === '/api/version') return {{ status: 'success', version: '0.2.3-ability-extension-fallback-smoke' }};
      if (path === '/api/sessions') return {{ status: 'success', sessions, total: sessions.length }};
      if (path === '/api/history') return {{ status: 'success', messages: [], context_start_seq: 0, total: 0, has_more: false }};
      if (path === '/api/runtime-projection') return {{
        status: 'success',
        projection: {{
          request_id: url.searchParams.get('request_id') || '',
          session_id: url.searchParams.get('session_id') || 'ability-extension-fallback-session',
          latest_event_id: 0,
          event_count: 0,
          messages: [],
          requests: [],
          history: {{ messages: [], has_more: false }}
        }}
      }};
      if (path === '/api/active-requests') return {{ status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {{}}, staleLocks: [] }};
      if (path === '/api/ui-state') return method === 'GET' ? {{ status: 'success', state: {{}} }} : {{ status: 'success' }};
      if (path === '/api/tools') {{
        window.__ecorexSmoke.calls.tools += 1;
        return {{ status: 'success', tools: [] }};
      }}
      if (path === '/api/skills') return {{ status: 'success', skills: [] }};
      if (path === '/api/models') return {{ status: 'success', providers: [], capabilities: {{}} }};
      if (path === '/api/extensions') {{
        window.__ecorexSmoke.calls.extensions += 1;
        return {{ status: 'success', extensions, count: extensions.length, summary: {{ builtin_tool: extensions.length }} }};
      }}
      if (path === '/api/channels') return {{ status: 'success', channels: [] }};
      if (path === '/api/external-connections') return {{ status: 'success', connections: [], summary: {{ total: 0 }} }};
      if (path === '/api/scheduler') return {{ status: 'success', enabled: true, initialized: true, running: false, tasks: [], taskCount: 0, counts: {{ total: 0 }} }};
      if (path === '/api/tool-permissions') return {{ status: 'success', mode: 'smart-ask', grantsCount: 0 }};
      if (path === '/api/memory/files') return {{ status: 'success', files: [] }};
      if (path === '/api/capabilities') return {{ status: 'success', packs: [] }};
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
  await wait('settings button', () => Array.from(document.querySelectorAll('button')).some((el) => (el.innerText || '').trim() === '设置'));
  Array.from(document.querySelectorAll('button')).find((el) => (el.innerText || '').trim() === '设置').click();
  await wait('settings sheet', () => Boolean(document.querySelector('.settings-sheet')));
  Array.from(document.querySelectorAll('.settings-nav button')).find((el) => (el.innerText || '').includes('能力')).click();
  await wait('ability rows', () => document.querySelectorAll('.ability-grid article').length >= 8);
  const rows = Array.from(document.querySelectorAll('.ability-grid article')).map((row) => ({
    name: (row.querySelector('strong')?.innerText || '').trim(),
    status: (row.querySelector('em, button')?.innerText || '').trim(),
    className: row.className,
    text: (row.innerText || '').trim()
  }));
  const byName = Object.fromEntries(rows.map((row) => [row.name, row]));
  const requiredLoaded = ['Bash / Shell', '本地文件读写', 'OCR / 图像理解', '飞书 / Lark CLI', 'Playwright 浏览器'];
  const requiredReady = ['定时任务'];
  const failed = [];
  for (const name of requiredLoaded) {
    if (!byName[name] || byName[name].status !== '已加载' || !String(byName[name].className).includes('is-ready')) {
      failed.push({ name, observed: byName[name] || null });
    }
  }
  for (const name of requiredReady) {
    if (!byName[name] || !['工具已加载', '运行中', '已加载'].includes(byName[name].status) || !String(byName[name].className).includes('is-ready')) {
      failed.push({ name, observed: byName[name] || null });
    }
  }
  return {
    failed,
    rows,
    toolsApiCallCount: window.__ecorexSmoke.calls.tools,
    extensionsApiCallCount: window.__ecorexSmoke.calls.extensions,
    unloadedCount: rows.filter((row) => row.status === '未加载').length
  };
}
"""


def run(output: Path, screenshot: Path | None = None) -> dict[str, Any]:
    dist = ROOT / "desktop" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("desktop/dist/index.html is missing; run npm --prefix desktop run build:renderer first")
    console_errors: list[str] = []
    with static_site_server(dist) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 980}, device_scale_factor=1)
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.add_init_script(_stub_script())
            page.goto(url, wait_until="domcontentloaded")
            metrics = page.evaluate(_probe_script())
            if screenshot:
                page.screenshot(path=str(screenshot), full_page=True)
            browser.close()
    failed = list(metrics.get("failed") or [])
    result = {
        "status": "PASS" if not failed and not console_errors else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": "settings-abilities-builtin-extension-fallback",
        "redacted": True,
        "metrics": {
            "toolsApiReturnsEmpty": True,
            "toolsApiCallCount": int(metrics.get("toolsApiCallCount") or 0),
            "extensionsApiCallCount": int(metrics.get("extensionsApiCallCount") or 0),
            "unloadedCount": int(metrics.get("unloadedCount") or 0),
            "rowCount": len(metrics.get("rows") or []),
        },
        "failed": failed,
        "consoleErrorCount": len(console_errors),
        "rows": metrics.get("rows") or [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ARTIFACT_DIR / "ability-extension-fallback-browser-smoke.json")
    parser.add_argument("--screenshot", type=Path, default=ARTIFACT_DIR / "ability-extension-fallback-browser-smoke.png")
    args = parser.parse_args()
    result = run(args.output, args.screenshot)
    print(json.dumps({"status": result["status"], "artifact": str(args.output), "metrics": result["metrics"], "failed": result["failed"]}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
