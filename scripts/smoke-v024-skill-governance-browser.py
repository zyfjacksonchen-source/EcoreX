#!/usr/bin/env python3
"""Browser smoke for v0.2.4 skill governance and unified display."""

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


def _skill_extension(
    name: str,
    *,
    display: str,
    source: str,
    source_group: str,
    source_label: str,
    purpose_group: str,
    purpose_label: str,
    enabled: bool,
    toggleable: bool,
    locked: bool = False,
    lock_reason: str = "",
) -> dict[str, Any]:
    return {
        "id": f"skill:{name}",
        "type": "builtin_skill" if source_group == "builtin" else "user_skill",
        "displayName": display,
        "description": f"{display} smoke skill",
        "origin": "builtin" if source_group == "builtin" else "workspace" if source_group == "custom" else "global",
        "source": source,
        "sourceGroup": source_group,
        "source_group": source_group,
        "sourceLabel": source_label,
        "source_label": source_label,
        "purposeGroup": purpose_group,
        "purpose_group": purpose_group,
        "purposeLabel": purpose_label,
        "purpose_label": purpose_label,
        "sourcePath": "",
        "enabled": enabled,
        "defaultEnabled": source_group == "builtin",
        "default_enabled": source_group == "builtin",
        "installed": True,
        "policy": "built-in-locked" if source_group == "builtin" else "user-overlay" if source_group == "custom" else "global-skill",
        "toggleable": toggleable,
        "locked": locked,
        "lockReason": lock_reason,
        "lock_reason": lock_reason,
        "status": "ready" if enabled else "disabled",
        "mentionable": True,
        "mention_category": "document" if purpose_group == "office" else "creative" if purpose_group == "image_media" else "general",
    }


def _stub_script() -> str:
    skills = [
        {
            "name": "office-documents",
            "display_name": "Office Documents",
            "description": "Builtin office document skill",
            "source": "builtin",
            "source_group": "builtin",
            "source_label": "内置",
            "purpose_group": "office",
            "purpose_label": "办公能力",
            "enabled": True,
            "default_enabled": True,
            "toggleable": False,
            "locked": True,
            "lock_reason": "builtin-default-enabled",
            "category": "document",
            "mentionable": True,
            "mention_category": "document",
        },
        {
            "name": "my-custom-skill",
            "display_name": "My Custom Skill",
            "description": "Custom system helper",
            "source": "custom",
            "source_group": "custom",
            "source_label": "自建",
            "purpose_group": "system",
            "purpose_label": "系统能力",
            "enabled": True,
            "toggleable": True,
            "locked": False,
            "category": "general",
            "mentionable": True,
            "mention_category": "general",
        },
        {
            "name": "vendor-image-skill",
            "display_name": "Vendor Image Skill",
            "description": "External image media helper",
            "source": "extra",
            "source_group": "external",
            "source_label": "外部",
            "purpose_group": "image_media",
            "purpose_label": "图像 / 媒体",
            "enabled": False,
            "toggleable": True,
            "locked": False,
            "category": "creative",
            "mentionable": True,
            "mention_category": "creative",
        },
    ]
    extensions = [
        _skill_extension(
            "office-documents",
            display="Office Documents",
            source="builtin",
            source_group="builtin",
            source_label="内置",
            purpose_group="office",
            purpose_label="办公能力",
            enabled=True,
            toggleable=False,
            locked=True,
            lock_reason="builtin-default-enabled",
        ),
        _skill_extension(
            "my-custom-skill",
            display="My Custom Skill",
            source="custom",
            source_group="custom",
            source_label="自建",
            purpose_group="system",
            purpose_label="系统能力",
            enabled=True,
            toggleable=True,
        ),
        _skill_extension(
            "vendor-image-skill",
            display="Vendor Image Skill",
            source="extra",
            source_group="external",
            source_label="外部",
            purpose_group="image_media",
            purpose_label="图像 / 媒体",
            enabled=False,
            toggleable=True,
        ),
    ]
    return rf"""
(() => {{
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  const now = Math.floor(Date.now() / 1000);
  const sessions = [{{
    session_id: 'skill-governance-session',
    title: 'Skill Governance Smoke',
    created_at: now - 60,
    last_active: now,
    msg_count: 0,
    scope: 'general',
    project: null
  }}];
  const skills = {json.dumps(skills, ensure_ascii=False)};
  const extensions = {json.dumps(extensions, ensure_ascii=False)};
  window.__ecorexSmoke = {{ skillPosts: [], skillsCalls: 0, extensionsCalls: 0 }};
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
      const body = typeof request === 'object' && request ? request.body : undefined;
      const url = new URL(rawPath || '/', window.location.origin);
      const path = url.pathname;
      if (path === '/api/version') return {{ status: 'success', version: '0.2.4-skill-governance-smoke' }};
      if (path === '/api/sessions') return {{ status: 'success', sessions, total: sessions.length }};
      if (path === '/api/history') return {{ status: 'success', messages: [], context_start_seq: 0, total: 0, has_more: false }};
      if (path === '/api/runtime-projection') return {{
        status: 'success',
        projection: {{
          request_id: url.searchParams.get('request_id') || '',
          session_id: url.searchParams.get('session_id') || 'skill-governance-session',
          latest_event_id: 0,
          event_count: 0,
          messages: [],
          requests: [],
          history: {{ messages: [], has_more: false }}
        }}
      }};
      if (path === '/api/active-requests') return {{ status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {{}}, staleLocks: [] }};
      if (path === '/api/ui-state') return method === 'GET' ? {{ status: 'success', state: {{}} }} : {{ status: 'success' }};
      if (path === '/api/tools') return {{ status: 'success', tools: [] }};
      if (path === '/api/skills') {{
        window.__ecorexSmoke.skillsCalls += 1;
        if (method === 'POST') {{
          window.__ecorexSmoke.skillPosts.push(body || {{}});
          return {{ status: 'success' }};
        }}
        return {{ status: 'success', skills }};
      }}
      if (path === '/api/models') return {{ status: 'success', providers: [], capabilities: {{}} }};
      if (path === '/api/extensions') {{
        window.__ecorexSmoke.extensionsCalls += 1;
        return {{ status: 'success', extensions, count: extensions.length, summary: {{ builtin_skill: 1, user_skill: 2 }} }};
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
  const text = (node) => (node && node.innerText ? node.innerText : '').trim();
  await wait('settings button', () => Array.from(document.querySelectorAll('button')).some((el) => text(el) === '设置'));
  Array.from(document.querySelectorAll('button')).find((el) => text(el) === '设置').click();
  await wait('settings sheet', () => Boolean(document.querySelector('.settings-sheet')));
  Array.from(document.querySelectorAll('.settings-nav button')).find((el) => text(el).includes('能力')).click();
  await wait('skill source sections', () => document.querySelectorAll('.skill-source-section').length === 3);

  const sourceSections = Array.from(document.querySelectorAll('.skill-source-section')).map((section) => ({
    heading: text(section.querySelector('.skill-source-heading strong')),
    count: text(section.querySelector('.skill-source-heading span')),
    purposeHeadings: Array.from(section.querySelectorAll('.skill-category-heading strong')).map(text),
    rows: Array.from(section.querySelectorAll('.toggle-row')).map((row) => ({
      title: text(row.querySelector('strong')),
      meta: text(row.querySelector('small')),
      status: text(row.querySelector('em')),
      inputDisabled: Boolean(row.querySelector('input')?.disabled),
      inputChecked: Boolean(row.querySelector('input')?.checked),
      className: row.className
    }))
  }));
  const rows = Object.fromEntries(sourceSections.flatMap((section) => section.rows).map((row) => [row.title, row]));
  const failures = [];
  const headings = sourceSections.map((section) => section.heading);
  for (const expected of ['内置', '自建', '外部']) {
    if (!headings.includes(expected)) failures.push(`missing source heading ${expected}`);
  }
  if (!sourceSections.find((section) => section.heading === '内置')?.purposeHeadings.includes('办公能力')) failures.push('missing builtin office purpose');
  if (!sourceSections.find((section) => section.heading === '自建')?.purposeHeadings.includes('系统能力')) failures.push('missing custom system purpose');
  if (!sourceSections.find((section) => section.heading === '外部')?.purposeHeadings.includes('图像 / 媒体')) failures.push('missing external image purpose');
  if (!rows['Office Documents']?.inputDisabled || !rows['Office Documents']?.inputChecked) failures.push('builtin row is not locked enabled');
  if (!rows['Office Documents']?.status.includes('内置能力默认启用')) failures.push('builtin row lacks lock status');
  if (rows['My Custom Skill']?.inputDisabled) failures.push('custom row should be toggleable');
  if (rows['Vendor Image Skill']?.inputDisabled) failures.push('external row should be toggleable');
  if (!rows['Vendor Image Skill'] || rows['Vendor Image Skill'].inputChecked) failures.push('external disabled row state lost');
  if (document.querySelector('.skill-background-details')) failures.push('legacy background details still rendered');
  const classSet = new Set(Object.values(rows).map((row) => row.className.replace(/\s*is-readonly/g, '').trim()));
  if (classSet.size !== 1) failures.push('skill rows do not share one base display class');
  const builtinInput = Array.from(document.querySelectorAll('.toggle-row')).find((row) => text(row.querySelector('strong')) === 'Office Documents')?.querySelector('input');
  builtinInput?.click();
  await new Promise((resolve) => setTimeout(resolve, 100));
  if (window.__ecorexSmoke.skillPosts.some((post) => post && post.name === 'office-documents' && post.action === 'close')) {
    failures.push('disabled builtin input posted a close action');
  }
  return {
    failures,
    sourceSections,
    skillPostCount: window.__ecorexSmoke.skillPosts.length,
    skillsCalls: window.__ecorexSmoke.skillsCalls,
    extensionsCalls: window.__ecorexSmoke.extensionsCalls
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
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
            browser.close()
    failures = list(metrics.get("failures") or [])
    result = {
        "status": "PASS" if not failures and not console_errors else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": "v024-skill-source-purpose-governance",
        "redacted": True,
        "metrics": {
            "sourceSectionCount": len(metrics.get("sourceSections") or []),
            "skillPostCount": int(metrics.get("skillPostCount") or 0),
            "skillsCalls": int(metrics.get("skillsCalls") or 0),
            "extensionsCalls": int(metrics.get("extensionsCalls") or 0),
        },
        "failures": failures,
        "consoleErrorCount": len(console_errors),
        "sourceSections": metrics.get("sourceSections") or [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "v0.2.4" / "artifacts" / "skill-governance-browser-smoke.json")
    parser.add_argument("--screenshot", type=Path, default=ROOT / "docs" / "v0.2.4" / "artifacts" / "skill-governance-browser-smoke.png")
    args = parser.parse_args()
    result = run(args.output, args.screenshot)
    print(json.dumps({"status": result["status"], "artifact": _relative(args.output), "metrics": result["metrics"], "failures": result["failures"]}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
