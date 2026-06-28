#!/usr/bin/env python3
"""Browser smoke for v0.2.4 CowAgent-style live Markdown streaming."""

from __future__ import annotations

import argparse
import json
import statistics
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


def _long_markdown() -> str:
    lead = """# Streaming Markdown Smoke

This pending assistant answer is intentionally longer than the old live-render window.
It should be formatted while streaming, not shown as a raw Markdown tail.

"""
    section = """## Live Section {index}

- first bullet with **bold** text
- second bullet with a [safe link](https://example.com/streaming-{index})

1. ordered item one
2. ordered item two

| Metric | Value |
| --- | ---: |
| rows | {index} |
| status | formatted |

```ts
export const section{index} = "formatted while streaming";
```

"""
    body = lead + "\n".join(section.format(index=i) for i in range(1, 61))
    if len(body) <= 12000:
        raise AssertionError("streaming smoke markdown must exceed the legacy 12k live-render window")
    return body


def _stub_script(content: str) -> str:
    escaped = json.dumps(content, ensure_ascii=False)
    return rf"""
(() => {{
  const now = '2026-06-27T10:00:00.000Z';
  const sessionId = 'streaming-markdown-session';
  const requestId = 'req-streaming-markdown';
  const content = {escaped};
  const assistantMessage = {{
    id: 'assistant-streaming',
    role: 'assistant',
    content,
    pending: true,
    requestId,
    createdAt: now,
    runTiming: {{ startedAtMs: Date.now() - 1200 }}
  }};
  const userMessage = {{
    id: 'user-streaming',
    role: 'user',
    content: 'Render a long Markdown answer while streaming.',
    requestId,
    createdAt: now
  }};
  const sessions = [{{
    session_id: sessionId,
    title: 'Streaming Markdown Smoke',
    scope: 'general',
    project: null,
    created_at: Date.parse(now) - 1000,
    last_active: Date.parse(now),
    msg_count: 2
  }}];
  const historyMessages = [
    {{ role: 'user', content: userMessage.content, created_at: now, request_id: requestId }},
    {{ role: 'assistant', content, created_at: now, request_id: requestId, pending: true }}
  ];
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-last-active-session-id', sessionId);
  localStorage.setItem('ecorex-session-ui-state', JSON.stringify({{
    [sessionId]: {{
      title: 'Streaming Markdown Smoke',
      messages: [userMessage, assistantMessage],
      composerText: '',
      attachments: [],
      lastActivityAt: Date.parse(now)
    }}
  }}));
  window.__ecorexSmokeStart = performance.now();
  window.__ecorexSmoke = {{ apiCalls: [] }};
  const ok = (value) => Promise.resolve(value);
  const activeRequest = {{
    request_id: requestId,
    session_id: sessionId,
    run_type: 'chat',
    source: 'web',
    state: 'running',
    status: 'running',
    created_at: now,
    age_seconds: 12
  }};
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
    apiJson: async (request) => {{
      const rawPath = typeof request === 'string' ? request : String(request && request.path || '');
      const method = typeof request === 'object' && request && request.method ? String(request.method) : 'GET';
      const url = new URL(rawPath || '/', window.location.origin);
      const path = url.pathname;
      window.__ecorexSmoke.apiCalls.push({{ path, method }});
      if (path === '/api/version') return {{ status: 'success', version: '0.2.4-streaming-markdown-smoke' }};
      if (path === '/api/sessions') return {{ status: 'success', sessions, total: sessions.length }};
      if (path === '/api/history') return {{ status: 'success', messages: historyMessages, context_start_seq: 0, total: 2, has_more: false }};
      if (path === '/api/runtime-projection') return {{
        status: 'success',
        projection: {{
          request_id: requestId,
          session_id: sessionId,
          latest_event_id: 0,
          event_count: 0,
          messages: [],
          requests: [activeRequest],
          history: {{ messages: historyMessages, has_more: false }}
        }}
      }};
      if (path === '/api/active-requests') return {{ status: 'success', requests: [activeRequest], recentTerminalRequests: [], runStatusCounts: {{ running: 1 }}, staleLocks: [] }};
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
  await wait('streaming markdown', () => Boolean(document.querySelector('.streaming-markdown .markdown-content h2')));
  await wait('formatted table and code', () => (
    document.querySelectorAll('.streaming-markdown table').length > 0
    && document.querySelectorAll('.streaming-markdown pre code').length > 0
  ));
  const root = document.querySelector('.streaming-markdown');
  const text = document.body.innerText || '';
  const metrics = {
    formattedReadyMs: Math.round((performance.now() - (window.__ecorexSmokeStart || 0)) * 10) / 10,
    streamingRootCount: document.querySelectorAll('.streaming-markdown').length,
    markdownBlockCount: document.querySelectorAll('.streaming-markdown .markdown-content').length,
    headingCount: root ? root.querySelectorAll('h1,h2,h3').length : 0,
    unorderedListCount: root ? root.querySelectorAll('ul').length : 0,
    orderedListCount: root ? root.querySelectorAll('ol').length : 0,
    tableCount: root ? root.querySelectorAll('table').length : 0,
    linkCount: root ? root.querySelectorAll('a[href^="https://example.com/"]').length : 0,
    codeBlockCount: root ? root.querySelectorAll('pre code').length : 0,
    rawTailNodeCount: document.querySelectorAll('.streaming-code,.streaming-tail').length,
    hasOmittedStreamingText: /\[\.\.\.\s+\d+\s+chars streaming\s+\.\.\.\]/.test(text),
    contentTextLength: root ? root.innerText.length : 0,
    apiCallCount: (window.__ecorexSmoke?.apiCalls || []).length
  };
  const failures = [];
  if (metrics.streamingRootCount !== 1) failures.push(`expected one streaming root, got ${metrics.streamingRootCount}`);
  if (metrics.markdownBlockCount < 3) failures.push('streaming content was not split into formatted markdown blocks');
  if (metrics.headingCount < 10) failures.push('headings were not formatted during streaming');
  if (metrics.unorderedListCount < 1 || metrics.orderedListCount < 1) failures.push('lists were not formatted during streaming');
  if (metrics.tableCount < 1) failures.push('table was not formatted during streaming');
  if (metrics.linkCount < 1) failures.push('links were not formatted during streaming');
  if (metrics.codeBlockCount < 1) failures.push('code block was not formatted during streaming');
  if (metrics.rawTailNodeCount) failures.push('raw streaming tail/code nodes are still present');
  if (metrics.hasOmittedStreamingText) failures.push('legacy chars-streaming omission marker is visible');
  if (metrics.contentTextLength <= 12000) failures.push('rendered streaming text did not exceed the legacy window size');
  return { metrics, failures };
}
"""


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 1)


def run(output: Path, screenshot: Path | None = None, iterations: int = 5) -> dict[str, Any]:
    dist = ROOT / "desktop" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("desktop/dist/index.html is missing; run npm --prefix desktop run build:renderer first")
    content = _long_markdown()
    console_errors: list[str] = []
    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    with static_site_server(dist) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for index in range(max(1, iterations)):
                page = browser.new_page(viewport={"width": 1440, "height": 980}, device_scale_factor=1)
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.add_init_script(_stub_script(content))
                page.goto(url, wait_until="domcontentloaded")
                probe = page.evaluate(_probe_script())
                samples.append(probe.get("metrics") or {})
                failures.extend(str(item) for item in (probe.get("failures") or []))
                if index == 0 and screenshot:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot), full_page=True)
                page.close()
            browser.close()
    ready_ms = [float(sample.get("formattedReadyMs") or 0) for sample in samples]
    perf = {
        "sampleCount": len(ready_ms),
        "p50FormattedReadyMs": round(statistics.median(ready_ms), 1) if ready_ms else 0.0,
        "p95FormattedReadyMs": _percentile(ready_ms, 0.95),
        "maxFormattedReadyMs": round(max(ready_ms), 1) if ready_ms else 0.0,
    }
    if perf["p95FormattedReadyMs"] > 5000:
        failures.append(f"formatted ready p95 too slow: {perf['p95FormattedReadyMs']}ms")
    result = {
        "status": "PASS" if not failures and not console_errors else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": "v024-cowagent-style-live-markdown-streaming",
        "redacted": True,
        "contentLength": len(content),
        "performance": perf,
        "samples": samples,
        "failures": sorted(set(failures)),
        "consoleErrorCount": len(console_errors),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "v0.2.4" / "artifacts" / "streaming-markdown-browser-smoke.json")
    parser.add_argument("--screenshot", type=Path, default=ROOT / "docs" / "v0.2.4" / "artifacts" / "streaming-markdown-browser-smoke.png")
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    result = run(args.output, args.screenshot, args.iterations)
    print(json.dumps({
        "status": result["status"],
        "artifact": _relative(args.output),
        "performance": result["performance"],
        "failures": result["failures"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
