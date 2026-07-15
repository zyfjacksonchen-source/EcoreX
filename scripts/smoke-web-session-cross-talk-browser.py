#!/usr/bin/env python3
"""Browser smoke for R23-20 session list cross-talk and pin/rename semantics."""

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


SMOKE_SALT = b"ecorex-v023-session-cross-talk-browser"


def _h(value: str) -> str:
    return "hmac:" + hmac.new(SMOKE_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:16]


def _stub_script() -> str:
    return r"""
(() => {
  const now = 1782473200000;
  const iso = (offset) => new Date(now + offset).toISOString();
  const project = {
    id: 'proj-cross-talk',
    name: 'Cross Talk Project',
    path: 'workspace-redacted',
    memoryPath: 'memory-redacted',
    dreamsPath: 'dreams-redacted',
    updatedAt: iso(0)
  };
  const projectBinding = {
    projectId: project.id,
    projectName: project.name,
    projectPath: project.path,
    memoryPath: project.memoryPath,
    dreamsPath: project.dreamsPath,
    source: 'runtime'
  };

  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-projects', JSON.stringify([project]));
  localStorage.setItem('ecorex-pinned-sessions', JSON.stringify({
    'session-pinned-newer': true,
    'session-pinned-old': true,
    'session-project-pinned': true
  }));
  localStorage.setItem('ecorex-last-active-session-id', 'session-pinned-old');
  localStorage.setItem('ecorex-session-projects', JSON.stringify({
    'session-general-backend': project.id,
    'session-project-backend': 'stale-other-project'
  }));
  localStorage.setItem('ecorex-session-project-bindings', JSON.stringify({
    'session-general-backend': projectBinding,
    'session-project-backend': { ...projectBinding, projectId: 'stale-other-project' }
  }));
  localStorage.setItem('ecorex-session-ui-state', JSON.stringify({
    'session-general-backend': {
      title: 'General Backend Wins',
      projectId: project.id,
      projectBinding,
      messages: [],
      composerText: '',
      attachments: [],
      lastActivityAt: now - 4000
    },
    'session-project-backend': {
      title: 'Project Backend Wins',
      projectId: 'stale-other-project',
      projectBinding: { ...projectBinding, projectId: 'stale-other-project' },
      messages: [],
      composerText: '',
      attachments: [],
      lastActivityAt: now - 3000
    }
  }));

  const baseSessions = [
    {
      session_id: 'session-general-fresh',
      title: 'Newest Unpinned General',
      created_at: now - 1000,
      last_active: now - 1000,
      lastActivityAt: now - 1000,
      msg_count: 1,
      scope: 'general',
      project: null
    },
    {
      session_id: 'session-general-backend',
      title: 'General Backend Wins',
      created_at: now - 4000,
      last_active: now - 4000,
      lastActivityAt: now - 4000,
      msg_count: 1,
      scope: 'general',
      project: null
    },
    {
      session_id: 'session-project-backend',
      title: 'Project Backend Wins',
      created_at: now - 3000,
      last_active: now - 3000,
      lastActivityAt: now - 3000,
      msg_count: 1,
      scope: 'project',
      project,
      projectId: project.id,
      projectName: project.name,
      projectPath: project.path,
      memoryPath: project.memoryPath,
      dreamsPath: project.dreamsPath
    },
    {
      session_id: 'session-project-pinned',
      title: 'Pinned Project Session',
      created_at: now - 3500,
      last_active: now - 3500,
      lastActivityAt: now - 3500,
      msg_count: 1,
      scope: 'project',
      project,
      projectId: project.id,
      projectName: project.name,
      projectPath: project.path,
      memoryPath: project.memoryPath,
      dreamsPath: project.dreamsPath
    }
  ];
  const pinnedSessions = [
    {
      session_id: 'session-pinned-newer',
      title: 'Pinned Newer',
      created_at: now - 2500,
      last_active: now - 2500,
      lastActivityAt: now - 2500,
      msg_count: 1,
      scope: 'general',
      project: null
    },
    {
      session_id: 'session-pinned-old',
      title: 'Pinned Old Outside Page',
      created_at: now - 100000,
      last_active: now - 100000,
      lastActivityAt: now - 100000,
      msg_count: 1,
      scope: 'general',
      project: null
    }
  ];

  const calls = {
    sessions: [],
    rename: [],
    uiState: []
  };
  window.__ecorexSmoke = { calls };
  const ok = (value) => Promise.resolve(value);

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
    apiJson: ({ path, method, body }) => {
      const url = new URL(String(path || ''), window.location.origin);
      const pathname = url.pathname;
      if (pathname === '/api/version') return ok({ version: '0.2.3-session-smoke' });
      if (pathname === '/api/sessions') {
        const includePinned = url.searchParams.get('include_pinned') === '1';
        const pinnedIds = (url.searchParams.get('pinned_ids') || '').split(',').filter(Boolean);
        const includeIds = (url.searchParams.get('include_ids') || '').split(',').filter(Boolean);
        calls.sessions.push({
          includePinned,
          pinnedCount: pinnedIds.length,
          includeCount: includeIds.length
        });
        return ok({
          status: 'success',
          sessions: includePinned && pinnedIds.length >= 2
            ? [...pinnedSessions, ...baseSessions]
            : baseSessions,
          total: 60,
          included_session_ids: includePinned ? pinnedIds : []
        });
      }
      if (pathname.startsWith('/api/sessions/') && method === 'PUT') {
        calls.rename.push({ title: body && body.title ? String(body.title) : '' });
        return ok({ status: 'success' });
      }
      if (pathname === '/api/active-requests') {
        return ok({ status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {}, staleLocks: [] });
      }
      if (pathname === '/api/ui-state') {
        if (method === 'POST') calls.uiState.push(body || {});
        return ok({ status: 'success', state: {} });
      }
      if (pathname === '/api/history') {
        return ok({ status: 'success', messages: [], context_start_seq: 0, project_context: null, total: 0, has_more: false });
      }
      if (pathname === '/api/runtime-projection') {
        return ok({ status: 'success', latest_event_id: 0, projection: { session_id: url.searchParams.get('session_id') || '', messages: [], requests: [] } });
      }
      if (pathname === '/api/tools') return ok({ status: 'success', tools: [] });
      if (pathname === '/api/skills') return ok({ status: 'success', skills: [] });
      if (pathname === '/api/models') return ok({ status: 'success', providers: [], capabilities: {} });
      if (pathname === '/api/extensions') return ok({ status: 'success', extensions: [], count: 0, summary: {} });
      if (pathname === '/api/channels') return ok({ status: 'success', channels: [] });
      if (pathname === '/api/scheduler') return ok({ status: 'success', enabled: true, initialized: true, running: false, tasks: [], taskCount: 0, counts: {} });
      if (pathname === '/api/tool-permissions') return ok({ status: 'success', mode: 'smart-ask', grantsCount: 0 });
      if (pathname === '/api/memory/files') return ok({ status: 'success', files: [] });
      if (pathname === '/api/capabilities') return ok({ status: 'success', packs: [] });
      return ok({ status: 'success' });
    }
  };

  class QuietEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      setTimeout(() => {
        if (typeof this.onopen === 'function') this.onopen({ type: 'open', data: '' });
      }, 0);
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
  const generalRows = () => Array.from(document.querySelectorAll('.session-list > .session-row'));
  const projectRows = () => Array.from(document.querySelectorAll('.project-session-list .session-row'));
  const hasGeneral = (label) => generalRows().some((row) => text(row).includes(label));
  const hasProject = (label) => projectRows().some((row) => text(row).includes(label));

  await wait('session rows', () => rows().length >= 5 && generalRows().length >= 4 && projectRows().length >= 1);
  await wait('included pinned sessions query', () => (window.__ecorexSmoke.calls.sessions || []).some((call) => call.includePinned && call.pinnedCount >= 2 && call.includeCount >= 2));

  assert(hasGeneral('Pinned Newer'), 'included newer pinned session missing from general list');
  assert(hasGeneral('Pinned Old Outside Page'), 'included old pinned session missing from general list');
  assert(hasGeneral('Newest Unpinned General'), 'fresh unpinned general session missing');
  assert(hasGeneral('General Backend Wins'), 'backend general session missing');
  assert(hasProject('Project Backend Wins'), 'backend project session missing');
  assert(hasProject('Pinned Project Session'), 'pinned project session missing');
  assert(!hasProject('General Backend Wins'), 'backend general session leaked into project bucket through stale local binding');
  assert(!hasGeneral('Project Backend Wins'), 'backend project session leaked into general bucket');

  const generalOrder = generalRows().map((row) => text(row));
  const pinnedNewerIndex = generalOrder.findIndex((value) => value.includes('Pinned Newer'));
  const pinnedOldIndex = generalOrder.findIndex((value) => value.includes('Pinned Old Outside Page'));
  const unpinnedFreshIndex = generalOrder.findIndex((value) => value.includes('Newest Unpinned General'));
  assert(pinnedNewerIndex >= 0 && pinnedOldIndex >= 0 && unpinnedFreshIndex >= 0, 'general order fixture missing');
  assert(pinnedNewerIndex < pinnedOldIndex, 'pinned sessions were not sorted newest-first inside pinned group');
  assert(pinnedOldIndex < unpinnedFreshIndex, 'pinned group did not stay above newer unpinned sessions');

  const projectOrder = projectRows().map((row) => text(row));
  const projectPinnedIndex = projectOrder.findIndex((value) => value.includes('Pinned Project Session'));
  const projectUnpinnedIndex = projectOrder.findIndex((value) => value.includes('Project Backend Wins'));
  assert(projectPinnedIndex >= 0 && projectUnpinnedIndex >= 0, 'project order fixture missing');
  assert(projectPinnedIndex < projectUnpinnedIndex, 'project bucket pinned group did not stay above unpinned sessions');

  const renameRow = generalRows().find((row) => text(row).includes('General Backend Wins'));
  assert(renameRow, 'rename target row missing');
  window.prompt = () => 'Renamed General Backend';
  renameRow.querySelector('[aria-label="重命名会话"]').click();
  await wait('rename api call', () => window.__ecorexSmoke.calls.rename.length === 1);
  await wait('renamed title visible', () => hasGeneral('Renamed General Backend'));
  const pinnedAfterRename = JSON.parse(localStorage.getItem('ecorex-pinned-sessions') || '{}');
  assert(pinnedAfterRename['session-general-backend'] !== true, 'rename auto-pinned a previously unpinned session');
  assert(pinnedAfterRename['session-pinned-newer'] === true && pinnedAfterRename['session-pinned-old'] === true, 'existing pinned sessions were lost');

  return {
    sessionQueryIncludePinned: true,
    includedPinnedCount: Math.max(...window.__ecorexSmoke.calls.sessions.map((call) => call.pinnedCount || 0)),
    generalRows: generalRows().length,
    projectRows: projectRows().length,
    pinnedGroupBeforeUnpinned: pinnedNewerIndex < pinnedOldIndex && pinnedOldIndex < unpinnedFreshIndex,
    projectPinnedGroupBeforeUnpinned: projectPinnedIndex < projectUnpinnedIndex,
    backendOwnerWonOverLocalStaleBinding: hasGeneral('Renamed General Backend') && !hasProject('Renamed General Backend'),
    projectOwnerStayedInProjectBucket: hasProject('Project Backend Wins') && !hasGeneral('Project Backend Wins'),
    renameCalls: window.__ecorexSmoke.calls.rename.length,
    renameDidNotPin: pinnedAfterRename['session-general-backend'] !== true
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
            metrics = page.evaluate(_probe_script())
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
        "fixtureHash": _h("session-cross-talk-browser"),
        "screenshot": screenshot_name,
        "metrics": metrics,
        "consoleErrorCount": len(errors),
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL", "consoleErrorCount": len(errors)}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-20 session cross-talk browser smoke.")
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
