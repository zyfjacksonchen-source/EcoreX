#!/usr/bin/env python3
"""Browser smoke for React Web project/general session isolation."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


def _project_session_stub_script() -> str:
    return r"""
(() => {
  const now = '2026-06-25T09:30:00+08:00';
  const project = {
    id: 'proj-a',
    name: 'Smoke Project',
    path: 'C:\\CowAgent\\smoke-project',
    memoryPath: 'C:\\CowAgent\\smoke-project\\.ecorex\\project-memory.md',
    dreamsPath: 'C:\\CowAgent\\smoke-project\\.ecorex\\dreams',
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

  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-projects', JSON.stringify([project]));
  localStorage.setItem('ecorex-last-active-session-id', 'session-general-existing');
  localStorage.setItem('ecorex-session-projects', JSON.stringify({
    'session-project-existing': project.id
  }));
  localStorage.setItem('ecorex-session-project-bindings', JSON.stringify({
    'session-project-existing': projectBinding
  }));
  localStorage.setItem('ecorex-session-ui-state', JSON.stringify({
    'session-general-existing': {
      title: 'General Saved',
      messages: [{ id: 'g1', role: 'user', content: 'general memory', createdAt: now }],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now)
    },
    'session-project-existing': {
      title: 'Project Saved',
      projectId: project.id,
      projectBinding,
      messages: [{ id: 'p1', role: 'user', content: 'project memory', createdAt: now }],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now) - 1000
    }
  }));

  const runtimeSessions = [
    {
      session_id: 'session-general-existing',
      title: 'General Saved',
      created_at: Date.parse(now) - 2000,
      last_active: Date.parse(now) - 2000,
      msg_count: 1
    },
    {
      session_id: 'session-project-existing',
      title: 'Project Saved',
      created_at: Date.parse(now) - 3000,
      last_active: Date.parse(now) - 3000,
      msg_count: 1,
      projectId: project.id,
      projectName: project.name,
      projectPath: project.path,
      memoryPath: project.memoryPath,
      dreamsPath: project.dreamsPath
    }
  ];
  const sentBodies = [];
  const uiStates = [];
  const deletedSessions = [];

  const makeResult = (body) => Promise.resolve(body);
  window.__ecorexSmoke = {
    project,
    projectBinding,
    sentBodies,
    uiStates,
    deletedSessions
  };
  window.ecorexDesktop = {
    platform: 'web',
    getEnterpriseSession: () => makeResult({
      token: 'smoke-token',
      user: { name: 'Smoke User', email: 'smoke@example.test' },
      quota: { allowed: true, dailyUsed: 0, dailyLimit: 100000, weeklyUsed: 0, weeklyLimit: 100000 }
    }),
    getSidecarStatus: () => makeResult({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }),
    onSidecarStatus: (listener) => {
      setTimeout(() => listener({ state: 'running', message: 'Smoke runtime running', webPort: 9899 }), 0);
      return () => {};
    },
    checkEnterpriseQuota: () => makeResult({ ok: true, quota: { allowed: true, dailyUsed: 0, dailyLimit: 100000, weeklyUsed: 0, weeklyLimit: 100000 } }),
    chooseProjectFolder: () => makeResult(project),
    refreshEnterprisePolicy: () => makeResult({ configured: true, changed: false, restarted: false, message: 'ok' }),
    reportTelemetry: () => makeResult({ status: 'success' }),
    setWindowTheme: () => makeResult(undefined),
    apiJson: ({ path, method, body }) => {
      const url = new URL(path, window.location.origin);
      const pathname = url.pathname;
      if (pathname === '/api/version') return makeResult({ version: '0.2.2-project-smoke' });
      if (pathname === '/api/sessions' || pathname === '/api/sessions/') {
        return makeResult({ status: 'success', sessions: runtimeSessions, total: runtimeSessions.length });
      }
      if (pathname.startsWith('/api/sessions/') && method === 'DELETE') {
        deletedSessions.push(pathname);
        return makeResult({ status: 'success' });
      }
      if (pathname.startsWith('/api/sessions/') && method === 'PUT') {
        return makeResult({ status: 'success' });
      }
      if (pathname === '/api/active-requests') {
        return makeResult({
          status: 'success',
          requests: [],
          recentTerminalRequests: [],
          runStatusCounts: {},
          staleLocks: []
        });
      }
      if (pathname === '/api/tools') return makeResult({ status: 'success', tools: [] });
      if (pathname === '/api/skills') return makeResult({ status: 'success', skills: [] });
      if (pathname === '/api/models') return makeResult({ status: 'success', providers: [], capabilities: {} });
      if (pathname === '/api/extensions') return makeResult({ status: 'success', extensions: [], count: 0, summary: {} });
      if (pathname === '/api/channels') return makeResult({ status: 'success', channels: [] });
      if (pathname === '/api/scheduler') {
        return makeResult({
          status: 'success',
          enabled: true,
          initialized: true,
          running: false,
          serviceStatus: 'browser_smoke',
          tasks: [],
          taskCount: 0,
          counts: { total: 0, enabled: 0, disabled: 0, error: 0 }
        });
      }
      if (pathname === '/api/tool-permissions') {
        return makeResult({ status: 'success', mode: 'smart-ask', grantsCount: 0, auditPath: '' });
      }
      if (pathname === '/api/memory/files') return makeResult({ status: 'success', files: [] });
      if (pathname === '/api/capabilities') return makeResult({ status: 'success', packs: [] });
      if (pathname === '/api/ui-state') {
        if (method === 'POST') uiStates.push(body || {});
        return makeResult({ status: 'success', state: {} });
      }
      if (pathname === '/api/history') {
        const sessionId = url.searchParams.get('session_id') || '';
        return makeResult({
          status: 'success',
          messages: sessionId === 'session-project-existing'
            ? [{ role: 'user', content: 'project memory', created_at: now }]
            : [{ role: 'user', content: 'general memory', created_at: now }],
          context_start_seq: 0,
          project_context: sessionId.includes('project') ? projectBinding : null,
          total: 1,
          page: 1,
          page_size: 50,
          has_more: false
        });
      }
      if (pathname === '/api/runtime-projection') {
        return makeResult({
          status: 'success',
          latest_event_id: 0,
          projection: { request_id: '', session_id: url.searchParams.get('session_id') || '', messages: [], requests: [] }
        });
      }
      if (pathname === '/message') {
        sentBodies.push(JSON.parse(JSON.stringify(body || {})));
        return makeResult({
          status: 'success',
          inline_reply: 'project smoke accepted',
          session_id: body && body.session_id,
          usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2, model: 'smoke', provider: 'smoke' }
        });
      }
      return makeResult({ status: 'success' });
    }
  };

  class SmokeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = {};
      setTimeout(() => this._emit('open', {}), 0);
    }
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    }
    removeEventListener(type, handler) {
      this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== handler);
    }
    _emit(type, payload) {
      const event = { type, data: payload && payload.data ? payload.data : '' };
      (this.listeners[type] || []).forEach((handler) => handler(event));
      const direct = this[`on${type}`];
      if (typeof direct === 'function') direct(event);
    }
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = SmokeEventSource;
})();
"""


def _project_session_probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }
  const wait = (label, predicate, timeout = 7000) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - start > timeout) return reject(new Error(`timeout waiting for project session UI: ${label}`));
      setTimeout(tick, 35);
    };
    tick();
  });
  const text = (node) => (node && node.innerText ? node.innerText : '').trim();
  const rows = () => Array.from(document.querySelectorAll('.session-row'));
  const projectRows = () => Array.from(document.querySelectorAll('.project-session-list .session-row'));
  const generalRows = () => Array.from(document.querySelectorAll('.session-list > .session-row'));

  await wait('app shell', () => document.querySelector('.app-shell'));
  await wait('runtime sessions', () => rows().length >= 2 && projectRows().length >= 1 && generalRows().length >= 1);

  assert(projectRows().some((row) => text(row).includes('Project Saved')), 'project session did not render in project group');
  assert(generalRows().some((row) => text(row).includes('General Saved')), 'general session did not render in general list');
  assert(!generalRows().some((row) => text(row).includes('Project Saved')), 'project session leaked into general list');
  assert(!projectRows().some((row) => text(row).includes('General Saved')), 'general session leaked into project list');

  rows().forEach((row) => {
    assert(row.getAttribute('draggable') === 'false', 'session row is draggable');
    const event = new DragEvent('dragstart', { bubbles: true, cancelable: true });
    const allowed = row.dispatchEvent(event);
    assert(event.defaultPrevented || allowed === false, 'session row dragstart was not prevented');
  });
  assert(projectRows().every((row) => row.dataset.sessionOwnership === 'project'), 'project row ownership marker missing');
  assert(generalRows().every((row) => row.dataset.sessionOwnership === 'general'), 'general row ownership marker missing');

  const projectGroup = Array.from(document.querySelectorAll('.project-group')).find((node) => text(node).includes('Smoke Project'));
  assert(projectGroup, 'project group missing');
  const newProjectButton = projectGroup.querySelector('.project-new-session-button') || projectGroup.querySelector('.project-session-empty');
  assert(newProjectButton, 'project new-session command missing');
  newProjectButton.click();
  await wait('pending project session active', () => {
    const header = document.querySelector('.chat-header h1');
    const path = document.querySelector('.chat-header .project-path');
    return header && text(header).includes('Smoke Project') && path && text(path).includes('smoke-project');
  });

  const pendingProjectRows = projectRows().filter((row) => text(row).includes('Smoke Project'));
  assert(pendingProjectRows.length >= 1, 'pending project session row missing from project group');
  assert(!generalRows().some((row) => text(row).includes('Smoke Project')), 'pending project session leaked into general list');

  const textarea = document.querySelector('.composer textarea');
  assert(textarea, 'composer textarea missing');
  const initialHeight = textarea.getBoundingClientRect().height;
  const textareaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  textareaSetter.call(textarea, 'line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8');
  textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'line 8' }));
  await wait('composer autosize', () => textarea.getBoundingClientRect().height > initialHeight);
  const expandedHeight = textarea.getBoundingClientRect().height;
  const maxHeight = Number.parseFloat(getComputedStyle(textarea).maxHeight) || 168;
  assert(expandedHeight <= maxHeight + 2, 'composer exceeded max height');

  await wait('send button enabled', () => {
    const button = document.querySelector('.composer .send-button[type="submit"]');
    return button && !button.disabled;
  });
  const sendButton = document.querySelector('.composer .send-button[type="submit"]');
  sendButton.click();
  await wait('project message accepted', () => (window.__ecorexSmoke.sentBodies || []).length === 1);
  await wait('pending project id replaced', () => {
    const state = JSON.parse(localStorage.getItem('ecorex-session-projects') || '{}');
    return Object.keys(state).some((key) => key.startsWith('ecorex-project-proj-a-'))
      && !Object.keys(state).some((key) => key.startsWith('ecorex-pending-project-'));
  });
  await wait('durable project ui state', () => {
    const state = JSON.parse(localStorage.getItem('ecorex-session-ui-state') || '{}');
    return Object.entries(state).some(([key, value]) => (
      key.startsWith('ecorex-project-proj-a-') && value && value.projectId === 'proj-a'
    ));
  });

  const sent = window.__ecorexSmoke.sentBodies[0];
  assert(/^ecorex-project-proj-a-/.test(sent.session_id || ''), 'project send did not replace pending session id');
  assert(sent.project_context_meta && sent.project_context_meta.projectId === 'proj-a', 'project context meta missing from send');
  assert(sent.project_context_meta.projectPath.includes('smoke-project'), 'project path missing from context meta');
  assert(Array.isArray(sent.attachments) && sent.attachments.some((item) => item.file_type === 'directory' && item.file_path.includes('smoke-project')), 'project directory attachment missing');
  assert(!sent.hidden_context, 'project send should use structured project_context_meta instead of hidden prompt');

  const sessionProjects = JSON.parse(localStorage.getItem('ecorex-session-projects') || '{}');
  const sessionBindings = JSON.parse(localStorage.getItem('ecorex-session-project-bindings') || '{}');
  const sessionUiState = JSON.parse(localStorage.getItem('ecorex-session-ui-state') || '{}');
  const projectSessionIds = Object.keys(sessionProjects).filter((key) => sessionProjects[key] === 'proj-a');
  assert(projectSessionIds.some((key) => key.startsWith('ecorex-project-proj-a-')), 'durable project session ownership missing');
  assert(sessionProjects['session-general-existing'] === undefined, 'general session was assigned to a project');
  assert(sessionBindings['session-general-existing'] === undefined, 'general session binding leaked');
  assert(Object.entries(sessionUiState).some(([key, value]) => key.startsWith('ecorex-project-proj-a-') && value.projectId === 'proj-a'), 'project ui state missing durable binding');

  const metrics = {
    projectRows: projectRows().map((row) => ({
      text: text(row),
      ownership: row.dataset.sessionOwnership,
      draggable: row.getAttribute('draggable')
    })),
    generalRows: generalRows().map((row) => ({
      text: text(row),
      ownership: row.dataset.sessionOwnership,
      draggable: row.getAttribute('draggable')
    })),
    initialHeight,
    expandedHeight,
    maxHeight,
    sent,
    sessionProjectKeys: Object.keys(sessionProjects).sort(),
    visibleHeader: text(document.querySelector('.chat-header'))
  };
  return metrics;
})();
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
            page.add_init_script(_project_session_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            metrics = page.evaluate(_project_session_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_target), full_page=False)
                screenshot_path = str(screenshot_target)
            browser.close()

    result = {
        "status": "PASS",
        "url": url,
        "app_root": str(app_root),
        "duration_ms": round((time.time() - started) * 1000),
        "screenshot": screenshot_path,
        "metrics": metrics,
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run React Web project/general session isolation browser smoke.")
    parser.add_argument("--app-root", default="desktop/dist", help="Built React Web app root. Run `npm --prefix desktop run build:renderer` first.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=920)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the React Web app.")
    args = parser.parse_args()

    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
