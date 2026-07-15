#!/usr/bin/env python3
"""Browser smoke for v0.2.4 session-list visual cleanup."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name


def _stub_script() -> str:
    return r"""
(() => {
  const now = '2026-06-27T10:00:00.000Z';
  const project = {
    id: 'proj-visual',
    name: 'Visual Project',
    path: 'smoke-project',
    memoryPath: 'smoke-project/.ecorex/project-memory.md',
    dreamsPath: 'smoke-project/.ecorex/dreams',
    updatedAt: now
  };
  const projectBinding = {
    projectId: project.id,
    projectName: project.name,
    projectPath: project.path,
    memoryPath: project.memoryPath,
    dreamsPath: project.dreamsPath,
    source: 'runtime',
    createdAt: now,
    lastUsedAt: now
  };
  const sessions = [
    {
      session_id: 'session-general-normal',
      title: 'General Normal',
      scope: 'general',
      project: null,
      created_at: Date.parse(now) - 1000,
      last_active: Date.parse(now) - 1000,
      msg_count: 1
    },
    {
      session_id: 'session-project-normal',
      title: 'Project Normal',
      projectId: project.id,
      projectName: project.name,
      projectPath: project.path,
      memoryPath: project.memoryPath,
      dreamsPath: project.dreamsPath,
      created_at: Date.parse(now) - 2000,
      last_active: Date.parse(now) - 2000,
      msg_count: 1
    },
    {
      session_id: 'session-unread',
      title: 'Unread Ready',
      scope: 'general',
      project: null,
      created_at: Date.parse(now) - 3000,
      last_active: Date.parse(now) - 3000,
      msg_count: 2
    },
    {
      session_id: 'session-running',
      title: 'Running Task',
      scope: 'general',
      project: null,
      created_at: Date.parse(now) - 4000,
      last_active: Date.parse(now) - 4000,
      msg_count: 2
    }
  ];
  const histories = {
    'session-general-normal': [
      { role: 'user', content: 'general normal', created_at: now, request_id: 'req-general' }
    ],
    'session-project-normal': [
      { role: 'user', content: 'project normal', created_at: now, request_id: 'req-project' }
    ],
    'session-unread': [
      { role: 'user', content: 'unread prompt', created_at: now, request_id: 'req-unread' },
      { role: 'assistant', content: 'unread final', created_at: now, request_id: 'req-unread' }
    ],
    'session-running': [
      { role: 'user', content: 'running prompt', created_at: now, request_id: 'req-running' },
      { role: 'assistant', content: '', created_at: now, request_id: 'req-running' }
    ]
  };
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-last-active-session-id', 'session-general-normal');
  localStorage.setItem('ecorex-unread-sessions', JSON.stringify({
    'session-unread': true
  }));
  localStorage.setItem('ecorex-projects', JSON.stringify([project]));
  localStorage.setItem('ecorex-session-projects', JSON.stringify({
    'session-project-normal': project.id
  }));
  localStorage.setItem('ecorex-session-project-bindings', JSON.stringify({
    'session-project-normal': projectBinding
  }));
  localStorage.setItem('ecorex-session-ui-state', JSON.stringify({
    'session-general-normal': {
      title: 'General Normal',
      messages: histories['session-general-normal'],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now) - 1000
    },
    'session-project-normal': {
      title: 'Project Normal',
      projectId: project.id,
      projectBinding,
      messages: histories['session-project-normal'],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now) - 2000
    },
    'session-unread': {
      title: 'Unread Ready',
      messages: [
        { id: 'u1', role: 'user', content: 'unread prompt', requestId: 'req-unread', createdAt: now },
        { id: 'u2', role: 'assistant', content: 'unread final', requestId: 'req-unread', createdAt: now }
      ],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now) - 3000
    },
    'session-running': {
      title: 'Running Task',
      messages: [
        { id: 'r1', role: 'user', content: 'running prompt', requestId: 'req-running', createdAt: now },
        { id: 'r2', role: 'assistant', content: '', pending: true, requestId: 'req-running', createdAt: now }
      ],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now) - 4000
    }
  }));

  const activeRequest = {
    request_id: 'req-running',
    session_id: 'session-running',
    run_type: 'chat',
    source: 'web',
    state: 'running',
    status: 'running',
    created_at: now,
    age_seconds: 12
  };
  const recentTerminal = {
    request_id: 'req-unread',
    session_id: 'session-unread',
    run_type: 'chat',
    source: 'web',
    state: 'failed',
    status: 'failed',
    error_message: 'browser smoke terminal marker',
    terminal_reason: 'failed',
    created_at: now,
    terminal_at: now,
    age_seconds: 4
  };

  const ok = (value) => Promise.resolve(value);
  window.__ecorexSmoke = { historyCalls: [], uiStatePosts: [] };
  window.ecorexDesktop = {
    platform: 'web',
    getEnterpriseSession: () => ok({
      token: 'smoke-token',
      user: { name: 'Smoke User', email: 'smoke@example.test' },
      quota: { allowed: true, dailyUsed: 0, dailyLimit: 100000, weeklyUsed: 0, weeklyLimit: 100000 }
    }),
    getSidecarStatus: () => ok({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }),
    onSidecarStatus: (listener) => {
      setTimeout(() => listener({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }), 0);
      return () => {};
    },
    checkEnterpriseQuota: () => ok({ ok: true, quota: { allowed: true } }),
    chooseProjectFolder: () => ok(project),
    refreshEnterprisePolicy: () => ok({ configured: true, changed: false }),
    reportTelemetry: () => ok({ status: 'success' }),
    setWindowTheme: () => ok(undefined),
    openPath: () => ok({ status: 'success' }),
    apiJson: ({ path, method, body }) => {
      const url = new URL(path || '/', window.location.origin);
      const pathname = url.pathname;
      if (pathname === '/api/version') return ok({ version: '0.2.4-session-list-visual-smoke' });
      if (pathname === '/api/sessions' || pathname === '/api/sessions/') {
        return ok({ status: 'success', sessions, total: sessions.length });
      }
      if (pathname === '/api/active-requests') {
        return ok({
          status: 'success',
          requests: [activeRequest],
          recentTerminalRequests: [recentTerminal],
          runStatusCounts: { running: 1, failed: 1 },
          staleLocks: []
        });
      }
      if (pathname === '/api/history') {
        const sessionId = url.searchParams.get('session_id') || 'session-general-normal';
        window.__ecorexSmoke.historyCalls.push(sessionId);
        return ok({
          status: 'success',
          messages: histories[sessionId] || [],
          context_start_seq: 0,
          project_context: sessionId === 'session-project-normal' ? projectBinding : null,
          total: (histories[sessionId] || []).length,
          page: 1,
          page_size: 50,
          has_more: false
        });
      }
      if (pathname === '/api/runtime-projection') {
        return ok({
          status: 'success',
          projection: {
            request_id: url.searchParams.get('request_id') || '',
            session_id: url.searchParams.get('session_id') || '',
            latest_event_id: 0,
            event_count: 0,
            messages: [],
            requests: [],
            history: { messages: [], has_more: false }
          }
        });
      }
      if (pathname === '/api/ui-state') {
        if (method === 'POST') window.__ecorexSmoke.uiStatePosts.push(body || {});
        return ok({ status: 'success', state: {} });
      }
      if (pathname === '/api/tools') return ok({ status: 'success', tools: [] });
      if (pathname === '/api/skills') return ok({ status: 'success', skills: [] });
      if (pathname === '/api/models') return ok({ status: 'success', providers: [], capabilities: {} });
      if (pathname === '/api/extensions') return ok({ status: 'success', extensions: [], count: 0, summary: {} });
      if (pathname === '/api/channels') return ok({ status: 'success', channels: [] });
      if (pathname === '/api/external-connections') return ok({ status: 'success', connections: [], summary: { total: 0 } });
      if (pathname === '/api/scheduler') return ok({ status: 'success', enabled: true, initialized: true, running: false, tasks: [], taskCount: 0, counts: {} });
      if (pathname === '/api/tool-permissions') return ok({ status: 'success', mode: 'smart-ask', grantsCount: 0 });
      if (pathname === '/api/memory/files') return ok({ status: 'success', files: [] });
      if (pathname === '/api/capabilities') return ok({ status: 'success', packs: [] });
      return ok({ status: 'success' });
    }
  };

  class QuietEventSource {
    constructor() {
      this.readyState = 1;
      setTimeout(() => { if (typeof this.onopen === 'function') this.onopen({ type: 'open', data: '' }); }, 0);
    }
    addEventListener() {}
    removeEventListener() {}
    close() { this.readyState = 2; }
  }
  window.EventSource = QuietEventSource;
})();
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
      setTimeout(tick, 40);
    };
    tick();
  });
  const text = (node) => (node && node.innerText ? node.innerText : '').trim();
  const rowByTitle = (title) => Array.from(document.querySelectorAll('.session-row'))
    .find((row) => text(row).includes(title));
  const rowMain = (title) => {
    const row = rowByTitle(title);
    assert(row, `${title} row missing`);
    const main = row.querySelector('.session-main');
    assert(main, `${title} session-main missing`);
    return { row, main };
  };
  const directChild = (main, selector) => Array.from(main.children).find((child) => child.matches(selector));
  const columnCount = (main) => getComputedStyle(main).gridTemplateColumns.split(' ').filter(Boolean).length;

  await wait('app shell', () => document.querySelector('.app-shell'));
  await wait('all session rows', () => (
    rowByTitle('General Normal') &&
    rowByTitle('Project Normal') &&
    rowByTitle('Unread Ready') &&
    rowByTitle('Running Task')
  ));
  await wait('unread terminal marker', () => rowByTitle('Unread Ready')?.classList.contains('is-unread'));

  const general = rowMain('General Normal');
  const project = rowMain('Project Normal');
  const unread = rowMain('Unread Ready');
  const running = rowMain('Running Task');

  for (const item of [general, project]) {
    assert(!item.main.classList.contains('has-leading-status'), 'normal row should not have leading status class');
    assert(!directChild(item.main, 'svg'), 'normal row rendered a direct SVG icon');
    assert(!directChild(item.main, '.session-unread-dot'), 'normal row rendered unread dot');
    assert(!directChild(item.main, '.thinking-indicator'), 'normal row rendered thinking indicator');
    assert(columnCount(item.main) === 2, 'normal row should use two grid columns');
  }

  assert(unread.row.classList.contains('is-unread'), 'unread row missing is-unread class');
  assert(unread.main.classList.contains('has-leading-status'), 'unread row missing leading status class');
  assert(Boolean(directChild(unread.main, '.session-unread-dot')), 'unread row missing orange dot');
  assert(columnCount(unread.main) === 3, 'unread row should use three grid columns');

  assert(running.row.classList.contains('is-waiting'), 'running row missing waiting class');
  assert(running.main.classList.contains('has-leading-status'), 'running row missing leading status class');
  assert(Boolean(directChild(running.main, '.thinking-indicator')), 'running row missing thinking indicator');
  assert(columnCount(running.main) === 3, 'running row should use three grid columns');

  unread.main.click();
  await wait('unread clears after read', () => !rowByTitle('Unread Ready')?.classList.contains('is-unread'));
  const unreadAfterRead = rowMain('Unread Ready');
  assert(!directChild(unreadAfterRead.main, '.session-unread-dot'), 'unread dot did not clear after reading session');

  return {
    normalGeneralColumns: columnCount(general.main),
    normalProjectColumns: columnCount(project.main),
    unreadColumns: columnCount(unread.main),
    runningColumns: columnCount(running.main),
    unreadClearedAfterRead: true,
    runningIndicatorCount: running.main.querySelectorAll('.thinking-indicator').length,
    unreadDotCountBeforeRead: unread.main.querySelectorAll('.session-unread-dot').length,
    normalDirectSvgCount: general.main.querySelectorAll(':scope > svg').length + project.main.querySelectorAll(':scope > svg').length,
    historyCallCount: window.__ecorexSmoke.historyCalls.length
  };
}
"""


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
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            metrics = page.evaluate(_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_target), full_page=False)
                screenshot_path = _relative(screenshot_target)
            browser.close()

    result = {
        "status": "PASS",
        "app_root": _relative(app_root),
        "duration_ms": round((time.time() - started) * 1000),
        "screenshot": screenshot_path,
        "metrics": metrics,
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.2.4 session-list visual cleanup browser smoke.")
    parser.add_argument("--app-root", default="desktop/dist", help="Built React app root. Run `npm --prefix desktop run build:renderer` first.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1320)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
