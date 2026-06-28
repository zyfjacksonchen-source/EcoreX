#!/usr/bin/env python3
"""Browser smoke for Office/PDF QA evidence badges in the React WebUI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


def _quality_evidence() -> dict[str, Any]:
    return {
        "schemaVersion": "v0.2.4",
        "kind": "pdf",
        "sourceRef": "hmac:source-ref",
        "qualityGates": ["text-orientation", "page-render", "layout-inspection"],
        "checks": [
            {"id": "text-orientation", "status": "pass", "detail": "rotated=0"},
            {"id": "page-render", "status": "fail", "detail": "quality-text-c3fc68a82386a0a8"},
        ],
        "missingQualityGates": ["layout-inspection"],
        "status": "fail",
        "renderedArtifacts": [{"page": 1, "width": 1200, "height": 900, "extension": ".png", "artifactRef": "hmac:render-ref"}],
        "pdfAnalysis": {
            "summary": {"pageCount": 1, "totalExtractedTextChars": 120},
            "pageEvidence": [{"page": 1, "textLengthBucket": "100"}],
        },
        "redacted": True,
    }


def _stub_script(evidence: dict[str, Any]) -> str:
    escaped_evidence = json.dumps(evidence, ensure_ascii=False)
    return rf"""
(() => {{
  const now = '2026-06-27T10:00:00.000Z';
  const sessionId = 'office-pdf-evidence-session';
  const requestId = 'req-office-pdf-evidence';
  const evidence = {escaped_evidence};
  const artifact = {{
    id: 'artifact-report-pdf',
    requestId,
    kind: 'file',
    intent: 'deliverable',
    operation: 'created',
    status: 'ready',
    title: 'report.pdf',
    path: 'outputs/report.pdf',
    qualityEvidence: evidence
  }};
  const assistantMessage = {{
    id: 'assistant-office-pdf-evidence',
    role: 'assistant',
    content: '已生成报告，并完成 Office/PDF 质量检查。',
    pending: false,
    requestId,
    createdAt: now,
    toolCalls: [{{
      name: 'office-pdf',
      status: 'done',
      result: {{ status: 'fail' }},
      qualityEvidence: evidence
    }}],
    artifacts: [artifact]
  }};
  const userMessage = {{
    id: 'user-office-pdf-evidence',
    role: 'user',
    content: '生成并检查 PDF。',
    requestId,
    createdAt: now
  }};
  const sessions = [{{
    session_id: sessionId,
    title: 'Office PDF Evidence Smoke',
    scope: 'general',
    project: null,
    created_at: Date.parse(now) - 1000,
    last_active: Date.parse(now),
    msg_count: 2
  }}];
  const historyMessages = [
    {{ role: 'user', content: userMessage.content, created_at: now, request_id: requestId }},
    {{ role: 'assistant', content: assistantMessage.content, created_at: now, request_id: requestId, tool_calls: assistantMessage.toolCalls, artifacts: [artifact] }}
  ];
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-last-active-session-id', sessionId);
  localStorage.setItem('ecorex-session-ui-state', JSON.stringify({{
    [sessionId]: {{
      title: 'Office PDF Evidence Smoke',
      messages: [userMessage, assistantMessage],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now)
    }}
  }}));
  window.__ecorexSmoke = {{ apiCalls: [] }};
  const ok = (value) => Promise.resolve(value);
  window.ecorexDesktop = {{
    platform: 'web',
    getEnterpriseSession: () => ok({{
      token: 'smoke-token',
      user: {{ name: 'Smoke User', email: 'smoke@example.test' }},
      quota: {{ allowed: true, dailyUsed: 0, dailyLimit: 100000 }}
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
    statPath: () => ok({{ status: 'success', exists: true, isFile: true, path: 'outputs/report.pdf' }}),
    apiJson: async (request) => {{
      const rawPath = typeof request === 'string' ? request : String(request && request.path || '');
      const method = typeof request === 'object' && request && request.method ? String(request.method) : 'GET';
      const url = new URL(rawPath || '/', window.location.origin);
      const path = url.pathname;
      window.__ecorexSmoke.apiCalls.push({{ path, method }});
      if (path === '/api/version') return {{ status: 'success', version: '0.2.4-office-pdf-evidence-smoke' }};
      if (path === '/api/sessions') return {{ status: 'success', sessions, total: sessions.length }};
      if (path === '/api/history') return {{ status: 'success', messages: historyMessages, context_start_seq: 0, total: 2, has_more: false }};
      if (path === '/api/runtime-projection') return {{
        status: 'success',
        projection: {{
          request_id: requestId,
          session_id: sessionId,
          latest_event_id: 0,
          event_count: 0,
          messages: historyMessages,
          requests: [],
          history: {{ messages: historyMessages, has_more: false }}
        }}
      }};
      if (path === '/api/active-requests') return {{ status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {{}}, staleLocks: [] }};
      if (path === '/api/ui-state') return method === 'GET' ? {{ status: 'success', state: {{}} }} : {{ status: 'success' }};
      if (path === '/api/tools') return {{ status: 'success', tools: [] }};
      if (path === '/api/skills') return {{ status: 'success', skills: [] }};
      if (path === '/api/extensions') return {{ status: 'success', extensions: [], count: 0, summary: {{}} }};
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
      setTimeout(tick, 40);
    };
    tick();
  });
  await wait('artifact QA badge', () => document.querySelectorAll('.artifact-row .quality-evidence-badge').length >= 1);
  const processSummary = document.querySelector('.agent-process-disclosure > summary');
  if (processSummary) processSummary.click();
  const toolSummary = document.querySelector('.agent-tool-step > summary');
  if (toolSummary) toolSummary.click();
  await wait('tool QA panel', () => document.querySelectorAll('.quality-evidence-panel').length >= 1);
  const text = document.documentElement.textContent || '';
  const html = document.documentElement.innerHTML || '';
  const metrics = {
    artifactBadgeCount: document.querySelectorAll('.artifact-row .quality-evidence-badge').length,
    toolPanelCount: document.querySelectorAll('.quality-evidence-panel').length,
    failBadgeCount: document.querySelectorAll('.quality-evidence-badge.is-fail').length,
    artifactRows: document.querySelectorAll('.artifact-row').length,
    apiCallCount: (window.__ecorexSmoke?.apiCalls || []).length,
    hasPdfQaFailedText: /PDF QA\s+未通过/.test(text)
  };
  const forbidden = ['renderProof', 'rawText', 'private prompt', 'sk-private', 'C:\\Users'];
  const leaks = forbidden.filter((item) => text.includes(item) || html.includes(item));
  const failures = [];
  if (metrics.artifactBadgeCount < 1) failures.push('artifact QA badge missing');
  if (metrics.toolPanelCount < 1) failures.push('tool QA panel missing');
  if (metrics.failBadgeCount < 2) failures.push('expected failing QA badge on artifact and tool panel');
  if (!metrics.hasPdfQaFailedText) failures.push('PDF QA failure label missing');
  if (leaks.length) failures.push(`privacy leak markers: ${leaks.join(', ')}`);
  return { metrics, leaks, failures };
}
"""


def run(output: Path, screenshot: Path | None = None) -> dict[str, Any]:
    dist = ROOT / "desktop" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("desktop/dist/index.html is missing; run npm --prefix desktop run build:renderer first")
    evidence = _quality_evidence()
    console_errors: list[str] = []
    with static_site_server(dist) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.add_init_script(_stub_script(evidence))
            page.goto(url, wait_until="domcontentloaded")
            result = page.evaluate(_probe_script())
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
            browser.close()

    failures = list(result.get("failures") or [])
    if console_errors:
        failures.append(f"console errors: {console_errors[:3]}")
    payload = {
        "status": "PASS" if not failures else "FAIL",
        "metrics": result.get("metrics") or {},
        "leaks": result.get("leaks") or [],
        "failures": failures,
        "redacted": True,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/v0.2.4/artifacts/office-pdf-evidence-browser-smoke.json")
    parser.add_argument("--screenshot", default="")
    args = parser.parse_args()
    payload = run(Path(args.output), Path(args.screenshot) if args.screenshot else None)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
