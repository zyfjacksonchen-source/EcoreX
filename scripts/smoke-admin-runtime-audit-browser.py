#!/usr/bin/env python3
"""Browser smoke for the admin runtime-audit projection panel."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, static_site_server


ADMIN_ROOT = ROOT / "deploy" / "ecorex-site" / "admin"
ARTIFACT = ROOT / "docs" / "v0.2.2" / "artifacts" / "admin-runtime-audit-browser-smoke.png"


def _admin_state_stub() -> str:
    payload = {
        "ok": True,
        "version": "0.2.2-admin-smoke",
        "users": [],
        "usageByUser": [],
        "logs": [],
        "logUsers": [],
        "capabilityPolicy": {"mirror": "configured", "mode": "disabled", "offlineCache": "configured"},
        "capabilities": [],
        "globalModel": None,
        "modelCredentials": [],
        "summary": {
            "users": 0,
            "tokens": 0,
            "errors": 0,
            "capabilities": 0,
            "modelCredentials": 0,
            "runtimeAuditEvents": 3,
            "runtimeAuditRequests": 1,
        },
        "runtimeAudit": {
            "status": "success",
            "sourceOfTruth": "admin-sync-runtime-events",
            "summary": {
                "events": 3,
                "requests": 1,
                "sessions": 1,
                "artifacts": 1,
                "messages": 0,
                "terminalEvents": 1,
                "capabilityPolicyBlocked": 1,
                "unknownEventTypes": 1,
                "lastIngestedAt": "2026-06-25T09:00:00Z",
            },
            "eventTypeCounts": {
                "run.failed": 1,
                "capability.policy_blocked": 1,
                "unknown": 1,
            },
            "sourceCounts": {"runtime": 2, "unknown": 1},
            "statusCounts": {"failed": 1, "blocked": 1, "completed": 1},
            "requests": [
                {
                    "requestHash": "reqhash1234567890",
                    "sessionHash": "sessionhash1234",
                    "userHash": "userhash1234567",
                    "deviceHash": "devicehash12345",
                    "eventCount": 3,
                    "terminalEventCount": 1,
                    "artifactCount": 1,
                    "messageCount": 0,
                    "lastIngestedAt": "2026-06-25T09:00:00Z",
                    "redacted": True,
                }
            ],
            "recentEvents": [
                {
                    "eventHash": "eventhashfailed1",
                    "eventType": "run.failed",
                    "requestHash": "reqhash1234567890",
                    "sessionHash": "sessionhash1234",
                    "userHash": "userhash1234567",
                    "deviceHash": "devicehash12345",
                    "source": "unknown",
                    "sourceHash": "sourcehash123456",
                    "status": "unknown",
                    "statusHash": "statushash12345",
                    "ingestedAt": "2026-06-25T09:00:00Z",
                    "detail": {"redacted": True, "shape": "object", "keyCount": 2, "keys": ["policy_mode"], "unknownKeyCount": 1},
                    "redacted": True,
                },
                {
                    "eventHash": "eventhashpolicy1",
                    "eventType": "capability.policy_blocked",
                    "requestHash": "reqhash1234567890",
                    "sessionHash": "sessionhash1234",
                    "source": "runtime",
                    "status": "blocked",
                    "ingestedAt": "2026-06-25T09:00:01Z",
                    "detail": {"redacted": True, "shape": "object", "keyCount": 1, "keys": ["policy_mode"]},
                    "redacted": True,
                },
                {
                    "eventHash": "eventhashunknown1",
                    "eventType": "unknown",
                    "eventTypeHash": "etypehash123456",
                    "eventTypeRedacted": True,
                    "requestHash": "reqhash1234567890",
                    "sessionHash": "sessionhash1234",
                    "source": "client",
                    "status": "completed",
                    "ingestedAt": "2026-06-25T09:00:02Z",
                    "detail": {"redacted": True, "shape": "object", "keyCount": 1, "unknownKeyCount": 1},
                    "redacted": True,
                },
            ],
            "privacy": {
                "redacted": True,
                "includesRawRuntimePayloads": False,
                "includesRawRequestSessionIds": False,
                "includesRawDeviceIds": False,
                "includesPromptText": False,
                "includesArtifactPaths": False,
            },
            "redacted": True,
        },
    }
    return f"""
(() => {{
  const statePayload = {json.dumps(payload, ensure_ascii=False)};
  const makeResponse = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {{
    status,
    headers: {{ 'Content-Type': 'application/json' }}
  }}));
  window.fetch = (input, init) => {{
    const raw = String(input && input.url ? input.url : input || '');
    const url = new URL(raw, window.location.href);
    if (url.pathname.endsWith('/api/state')) return makeResponse(statePayload);
    if (url.pathname.endsWith('/manifest.json')) return makeResponse({{ version: '0.2.2-admin-smoke', artifacts: [] }});
    return makeResponse({{ ok: true }});
  }};
}})();
"""


def _probe_script() -> str:
    forbidden = [
        "request-runtime-audit-private",
        "session-runtime-audit-private",
        "C:\\Users\\Alice",
        "private prompt",
        "private_prompt",
        "office-pdf-ghp_abcd",
        "sync@example.com",
        "device-1",
    ]
    return f"""
(() => {{
  function assert(condition, message) {{
    if (!condition) throw new Error(message);
  }}
  const auditButton = document.querySelector('[data-panel="runtime-audit"]');
  assert(!!auditButton, 'runtime audit nav missing');
  auditButton.click();
  const panel = document.querySelector('[data-panel-view="runtime-audit"].active');
  assert(!!panel, 'runtime audit panel did not activate');
  const text = panel.innerText || '';
  const html = panel.outerHTML || '';
  assert(text.includes('Runtime event projection'), 'audit heading missing');
  assert(text.includes('run.failed'), 'event type count missing');
  assert(text.includes('capability.policy_blocked'), 'policy block count missing');
  assert(text.includes('reqhash1234567890'), 'request hash missing');
  assert(text.includes('eventhashunknown1'), 'unknown event hash missing');
  const forbidden = {json.dumps(forbidden)};
  const visibleLeaks = forbidden.filter((item) => text.includes(item) || html.includes(item));
  assert(visibleLeaks.length === 0, `raw audit value leaked: ${{visibleLeaks.join(', ')}}`);
  return {{
    textLength: text.length,
    eventRows: panel.querySelectorAll('[data-runtime-audit-events] .audit-event-row').length,
    requestRows: panel.querySelectorAll('[data-runtime-audit-requests] .audit-request-row').length,
    countRows: panel.querySelectorAll('[data-runtime-audit-types] .audit-count-row').length,
    visibleLeaks
  }};
}})()
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    with static_site_server(ADMIN_ROOT, "index.html") as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_admin_state_stub())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_selector('[data-panel="runtime-audit"]', timeout=8000)
            metrics = page.evaluate(_probe_script())
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ARTIFACT), full_page=True)
            browser.close()
    if errors:
        raise AssertionError("; ".join(errors))
    return {
        "status": "PASS",
        "artifact": str(ARTIFACT),
        "durationSeconds": round(time.time() - started, 2),
        **metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--width", type=int, default=1366)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    print(json.dumps(run_smoke(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
