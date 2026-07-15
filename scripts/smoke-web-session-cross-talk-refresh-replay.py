#!/usr/bin/env python3
"""Browser smoke for R23-20 stale history / refresh replay session isolation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


SMOKE_SALT = b"ecorex-v023-session-refresh-replay"


def _h(value: str) -> str:
    return "hmac:" + hmac.new(SMOKE_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:16]


def _stub_script() -> str:
    return r"""
(() => {
  const now = 1782473600000;
  const firstLoad = sessionStorage.getItem('r23-refresh-replay-initialized') !== '1';
  if (firstLoad) {
    sessionStorage.setItem('r23-refresh-replay-initialized', '1');
    localStorage.clear();
    localStorage.setItem('ecorex-theme', 'light');
    localStorage.setItem('ecorex-skill-defaults-v1', '1');
    localStorage.setItem('ecorex-last-active-session-id', 'session-b');
    localStorage.setItem('ecorex-session-ui-state', JSON.stringify({
      'session-b': {
        title: 'Session B Clean',
        messages: [],
        composerText: '',
        attachments: [],
        lastActivityAt: now - 1000
      }
    }));
  }

  const calls = window.__ecorexSmoke?.calls || {
    history: [],
    projection: [],
    sessions: [],
    stream: []
  };
  window.__ecorexSmoke = { calls };
  const ok = (value) => Promise.resolve(value);
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const runtimeSessions = [
    {
      session_id: 'session-a',
      title: 'Session A Slow',
      created_at: now - 5000,
      last_active: now - 5000,
      lastActivityAt: now - 5000,
      msg_count: 2,
      scope: 'general',
      project: null
    },
    {
      session_id: 'session-b',
      title: 'Session B Clean',
      created_at: now - 1000,
      last_active: now - 1000,
      lastActivityAt: now - 1000,
      msg_count: 2,
      scope: 'general',
      project: null
    }
  ];

  window.ecorexDesktop = {
    platform: 'web',
    getEnterpriseSession: () => ok({
      token: 'smoke-token',
      user: { name: 'Smoke User', email: 'smoke-user' },
      quota: { allowed: true, dailyUsed: 0, dailyLimit: 100000, weeklyUsed: 0, weeklyLimit: 100000 }
    }),
    getSidecarStatus: () => ok({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }),
    onSidecarStatus: (listener) => {
      setTimeout(() => listener({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }), 0);
      return () => {};
    },
    checkEnterpriseQuota: () => ok({ ok: true, quota: { allowed: true } }),
    refreshEnterprisePolicy: () => ok({ configured: true, changed: false }),
    reportTelemetry: () => ok({ status: 'success' }),
    setWindowTheme: () => ok(undefined),
    apiJson: async ({ path, method, body }) => {
      const url = new URL(String(path || ''), window.location.origin);
      const pathname = url.pathname;
      if (pathname === '/api/version') return { version: '0.2.3-refresh-replay-smoke' };
      if (pathname === '/api/sessions') {
        calls.sessions.push({ includePinned: url.searchParams.get('include_pinned') === '1' });
        return { status: 'success', sessions: runtimeSessions, total: runtimeSessions.length };
      }
      if (pathname === '/api/active-requests') {
        return {
          status: 'success',
          requests: [{
            request_id: 'req-a',
            session_id: 'session-b',
            state: 'running',
            source: 'chat',
            run_type: 'chat',
            stream_available: true,
            created_at: now - 800
          }],
          recentTerminalRequests: [],
          runStatusCounts: { running: 1 },
          staleLocks: []
        };
      }
      if (pathname === '/api/ui-state') {
        return method === 'GET' ? { status: 'success', state: {} } : { status: 'success' };
      }
      if (pathname === '/api/history') {
        const target = url.searchParams.get('session_id') || '';
        calls.history.push({ target, at: Date.now() });
        if (target === 'session-a') {
          await wait(650);
          return {
            status: 'success',
            messages: [
              { role: 'user', content: 'A slow user prompt', created_at: now - 5000 },
              { role: 'assistant', content: 'A LATE CONTENT MUST NOT APPEAR', created_at: now - 4990 }
            ],
            context_start_seq: 0,
            total: 2,
            has_more: false
          };
        }
        return {
          status: 'success',
          messages: [
            { role: 'user', content: 'B clean user prompt', created_at: now - 1000 },
            { role: 'assistant', content: 'B CLEAN CONTENT STAYS VISIBLE', created_at: now - 990 }
          ],
          context_start_seq: 0,
          total: 2,
          has_more: false
        };
      }
      if (pathname === '/api/runtime-projection') {
        const request = url.searchParams.get('request_id') || '';
        const expected = url.searchParams.get('session_id') || '';
        const mismatch = Boolean(request && expected === 'session-b');
        calls.projection.push({ request: Boolean(request), expected: Boolean(expected), mismatch });
        if (request && expected === 'session-b') {
          return {
            status: 'error',
            code: 'SESSION_MISMATCH',
            message: 'request belongs to another session'
          };
        }
        return {
          status: 'success',
          latest_event_id: 0,
          projection: {
            session_id: expected,
            messages: [],
            requests: [],
            history: { messages: [], has_more: false }
          }
        };
      }
      if (pathname === '/api/tools') return { status: 'success', tools: [] };
      if (pathname === '/api/skills') return { status: 'success', skills: [] };
      if (pathname === '/api/models') return { status: 'success', providers: [], capabilities: {} };
      if (pathname === '/api/extensions') return { status: 'success', extensions: [], count: 0, summary: {} };
      if (pathname === '/api/channels') return { status: 'success', channels: [] };
      if (pathname === '/api/scheduler') return { status: 'success', enabled: true, initialized: true, running: false, tasks: [], taskCount: 0, counts: {} };
      if (pathname === '/api/tool-permissions') return { status: 'success', mode: 'smart-ask', grantsCount: 0 };
      if (pathname === '/api/memory/files') return { status: 'success', files: [] };
      if (pathname === '/api/capabilities') return { status: 'success', packs: [] };
      return { status: 'success' };
    }
  };

  class QuietEventSource {
    constructor(url) {
      this.url = url;
      const parsed = new URL(String(url || ''), window.location.href);
      const requestId = parsed.searchParams.get('request_id') || '';
      const expectedSession = parsed.searchParams.get('session_id') || '';
      calls.stream.push({
        request: Boolean(requestId),
        expected: Boolean(expectedSession),
        mismatchCase: requestId === 'req-a' && expectedSession === 'session-b'
      });
      this.readyState = requestId === 'req-a' && expectedSession === 'session-b' ? 2 : 1;
      setTimeout(() => {
        if (requestId === 'req-a' && expectedSession === 'session-b') {
          if (typeof this.onerror === 'function') this.onerror({ type: 'error', data: '' });
          return;
        }
        if (typeof this.onopen === 'function') this.onopen({ type: 'open', data: '' });
      }, 0);
    }
    addEventListener() {}
    removeEventListener() {}
    close() { this.readyState = 2; }
  }
  window.EventSource = QuietEventSource;
  window.EventSource.CLOSED = 2;
})();
"""


def _race_probe_script() -> str:
    return r"""
(async () => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = (label, predicate, timeout = 9000) => new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - started > timeout) return reject(new Error(`timeout waiting for ${label}`));
      setTimeout(tick, 35);
    };
    tick();
  });
  const text = (node) => (node && node.innerText ? node.innerText : '').trim();
  const rows = () => Array.from(document.querySelectorAll('.session-row'));
  const rowByTitle = (title) => rows().find((row) => text(row).includes(title));
  await wait('session rows', () => rowByTitle('Session A Slow') && rowByTitle('Session B Clean'));

  const rowA = rowByTitle('Session A Slow');
  const rowB = rowByTitle('Session B Clean');
  rowA.querySelector('.session-main').click();
  await new Promise((resolve) => setTimeout(resolve, 45));
  rowB.querySelector('.session-main').click();
  await wait('B clean history visible', () => (document.querySelector('.chat-pane')?.innerText || '').includes('B CLEAN CONTENT STAYS VISIBLE'));
  await wait('stream mismatch projection attempted', () => (
    (window.__ecorexSmoke.calls.stream || []).some((call) => call.mismatchCase && call.expected)
    && (window.__ecorexSmoke.calls.projection || []).some((call) => call.request && call.expected && call.mismatch)
  ));
  await new Promise((resolve) => setTimeout(resolve, 850));

  const paneText = document.querySelector('.chat-pane')?.innerText || '';
  assert(paneText.includes('B CLEAN CONTENT STAYS VISIBLE'), 'B clean session content was lost');
  assert(!paneText.includes('A LATE CONTENT MUST NOT APPEAR'), 'late A history polluted active B session');
  const mismatchObserved = (window.__ecorexSmoke.calls.projection || []).some((call) => call.request && call.expected && call.mismatch);
  const streamExpectedSessionObserved = (window.__ecorexSmoke.calls.stream || []).some((call) => call.mismatchCase && call.expected);
  const projectionCallCount = (window.__ecorexSmoke.calls.projection || []).length;
  const streamCallCount = (window.__ecorexSmoke.calls.stream || []).length;
  assert(mismatchObserved, 'expected session mismatch diagnostic did not return through renderer recovery');
  assert(streamExpectedSessionObserved, 'stream did not include expected session id');
  assert(projectionCallCount <= 6, 'session mismatch projection retried without backoff');
  assert(streamCallCount <= 6, 'session mismatch stream retried without backoff');
  const afterMismatchText = document.querySelector('.chat-pane')?.innerText || '';
  assert(!afterMismatchText.includes('A LATE CONTENT MUST NOT APPEAR'), 'renderer mismatch diagnostic polluted UI');

  return {
    staleHistoryIgnored: !paneText.includes('A LATE CONTENT MUST NOT APPEAR'),
    activeSessionContentStable: paneText.includes('B CLEAN CONTENT STAYS VISIBLE'),
    mismatchDiagnosticObserved: mismatchObserved,
    streamExpectedSessionObserved,
    historyCallCount: (window.__ecorexSmoke.calls.history || []).length,
    projectionCallCount,
    streamCallCount
  };
})();
"""


def _refresh_probe_script() -> str:
    return r"""
(async () => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = (label, predicate, timeout = 9000) => new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - started > timeout) return reject(new Error(`timeout waiting for refresh ${label}`));
      setTimeout(tick, 35);
    };
    tick();
  });
  await wait('B backend history after reload', () => (
    (document.querySelector('.chat-pane')?.innerText || '').includes('B CLEAN CONTENT STAYS VISIBLE')
    && (window.__ecorexSmoke.calls.history || []).some((call) => call.target === 'session-b')
  ));
  const paneText = document.querySelector('.chat-pane')?.innerText || '';
  const backendHistoryFetched = (window.__ecorexSmoke.calls.history || []).some((call) => call.target === 'session-b');
  assert(!paneText.includes('A LATE CONTENT MUST NOT APPEAR'), 'late A content survived refresh into B');
  assert(backendHistoryFetched, 'refresh did not fetch B backend history');
  assert(localStorage.getItem('ecorex-last-active-session-id') === 'session-b', 'last active session drifted after refresh');
  return {
    refreshKeptCleanSession: paneText.includes('B CLEAN CONTENT STAYS VISIBLE'),
    backendHistoryFetched,
    refreshRejectedLateSession: !paneText.includes('A LATE CONTENT MUST NOT APPEAR'),
    historyCallCount: (window.__ecorexSmoke.calls.history || []).length
  };
})();
"""


def _write_json(path: str, payload: dict[str, Any]) -> str:
    if not path:
        return ""
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return target.name


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    app_root = Path(args.app_root)
    if not app_root.is_absolute():
        app_root = ROOT / app_root

    with static_site_server(app_root) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            race_metrics = page.evaluate(_race_probe_script())
            page.reload(wait_until="domcontentloaded", timeout=args.timeout_ms)
            refresh_metrics = page.evaluate(_refresh_probe_script())
            screenshot_name = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_target), full_page=False)
                screenshot_name = screenshot_target.name
            browser.close()

    result = {
        "status": "PASS",
        "durationMs": round((time.time() - started) * 1000),
        "fixtureHash": _h("session-cross-talk-refresh-replay"),
        "screenshot": screenshot_name,
        "metrics": {
            "race": race_metrics,
            "refresh": refresh_metrics,
        },
        "consoleErrorCount": len(errors),
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL", "consoleErrorCount": len(errors)}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-20 stale history/refresh replay browser smoke.")
    parser.add_argument("--app-root", default="desktop/dist", help="Built React app root. Run `npm --prefix desktop run build:renderer` first.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=920)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--artifact", default="")
    args = parser.parse_args()

    try:
        result = run_smoke(args)
        _write_json(args.artifact, result)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
