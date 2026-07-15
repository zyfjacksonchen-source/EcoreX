#!/usr/bin/env python3
"""Browser smoke for R23-07/R23-12 Settings > External Connections."""

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


SMOKE_SALT = b"ecorex-v023-external-connections-browser"


def _h(value: str) -> str:
    return "hmac:" + hmac.new(SMOKE_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:16]


def _stub_script() -> str:
    return r"""
(() => {
  localStorage.clear();
  localStorage.setItem('ecorex-theme', 'light');
  localStorage.setItem('ecorex-skill-defaults-v1', '1');
  localStorage.setItem('ecorex-last-active-session-id', 'external-smoke-session');
  const now = 1782478200000;
  const calls = {
    externalList: 0,
    actions: [],
    sessions: 0
  };
  window.__ecorexSmoke = { calls };
  const ok = (value) => Promise.resolve(value);
  const connections = [
    {
      id: 'feishu',
      platform: 'feishu',
      displayName: 'Feishu / Lark',
      description: 'Team message ingress and delivery',
      logo: { type: 'brand', key: 'feishu', fallbackText: '飞' },
      status: 'dependency_missing',
      configured: true,
      enabled: false,
      connected: false,
      running: false,
      callable: true,
      dependencyMissing: true,
      dependencyStatus: {
        status: 'missing',
        dependency: 'lark_oapi',
        sdkPresent: false,
        credentialPresent: true,
        credentialValid: 'unknown',
        remoteConnectivityProbed: false
      },
      auth: { channelConfigState: 'configured' },
      agentSurface: { callableReason: '' },
      fields: [
        { key: 'feishu_app_id', label: 'App ID', type: 'text', value: 'cli_****xxxx', sensitive: true, masked: true },
        { key: 'feishu_app_secret', label: 'App Secret', type: 'secret', value: '****', sensitive: true, masked: true },
        { key: 'allow_all_users', label: 'Allow all users', type: 'bool', value: false }
      ],
      homeChannel: { configured: true, idHash: 'hmac:feedfacecafebeef', name: 'Smoke Home' },
      actions: [
        { id: 'save_config', label: '保存' },
        { id: 'test', label: '测试' },
        { id: 'start', label: '连接' },
        { id: 'set_home_channel', label: '设置主页频道' }
      ],
      source: 'channel'
    },
    {
      id: 'slack',
      platform: 'slack',
      displayName: 'Slack',
      description: 'Design-ready connector metadata',
      logo: { type: 'brand', key: 'slack', fallbackText: 'SL' },
      status: 'available',
      configured: false,
      enabled: false,
      connected: false,
      running: false,
      callable: false,
      auth: { channelConfigState: 'not_configured' },
      agentSurface: { callableReason: '未配置' },
      fields: [],
      actions: [
        { id: 'save_config', label: '保存' },
        { id: 'test', label: '测试' }
      ],
      source: 'channel'
    }
  ];

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
    refreshEnterprisePolicy: () => ok({ configured: true, changed: false }),
    reportTelemetry: () => ok({ status: 'success' }),
    setWindowTheme: () => ok(undefined),
    apiJson: async ({ path, method, body }) => {
      const url = new URL(String(path || ''), window.location.origin);
      const pathname = url.pathname;
      if (pathname === '/api/version') return { version: '0.2.3-external-connections-smoke' };
      if (pathname === '/api/sessions') {
        calls.sessions += 1;
        return {
          status: 'success',
          sessions: [{
            session_id: 'external-smoke-session',
            title: 'External Connections Smoke',
            created_at: now - 1000,
            last_active: now - 1000,
            msg_count: 0,
            scope: 'general',
            project: null
          }],
          total: 1
        };
      }
      if (pathname === '/api/external-connections') {
        calls.externalList += 1;
        return { status: 'success', connections, summary: { total: connections.length, configured: 1, connected: 0 }, updatedAt: now };
      }
      if (pathname.startsWith('/api/external-connections/') && pathname.endsWith('/actions')) {
        calls.actions.push({
          target: pathname.split('/')[3] || '',
          action: body && body.action ? String(body.action) : '',
          configKeys: body && body.config && typeof body.config === 'object' ? Object.keys(body.config).sort() : [],
          homeChannel: body && body.homeChannel ? String(body.homeChannel) : '',
          secretEchoed: Boolean(body && body.config && typeof body.config === 'object' && (
            Object.prototype.hasOwnProperty.call(body.config, 'app_secret') ||
            Object.prototype.hasOwnProperty.call(body.config, 'feishu_app_secret') ||
            Object.prototype.hasOwnProperty.call(body.config, 'feishu_app_id')
          ))
        });
        return { status: 'success', connection: connections[0] };
      }
      if (pathname === '/api/active-requests') return { status: 'success', requests: [], recentTerminalRequests: [], runStatusCounts: {}, staleLocks: [] };
      if (pathname === '/api/ui-state') return method === 'GET' ? { status: 'success', state: {} } : { status: 'success' };
      if (pathname === '/api/history') return { status: 'success', messages: [], context_start_seq: 0, total: 0, has_more: false };
      if (pathname === '/api/runtime-projection') return { status: 'success', latest_event_id: 0, projection: { messages: [], requests: [] } };
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
    constructor() {
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
  await wait('settings button', () => Array.from(document.querySelectorAll('button')).some((button) => text(button).includes('设置')));
  Array.from(document.querySelectorAll('button')).find((button) => text(button).includes('设置')).click();
  await wait('settings nav external connections', () => Array.from(document.querySelectorAll('.settings-nav button')).some((button) => text(button).includes('外部连接')));
  Array.from(document.querySelectorAll('.settings-nav button')).find((button) => text(button).includes('外部连接')).click();
  await wait('external connection cards', () => document.querySelectorAll('.external-connection-card').length >= 2);

  const panelText = document.querySelector('.settings-section')?.innerText || '';
  const cards = Array.from(document.querySelectorAll('.external-connection-card'));
  const feishu = cards.find((card) => text(card).includes('Feishu / Lark'));
  const slack = cards.find((card) => text(card).includes('Slack'));
  assert(feishu, 'Feishu/Lark card missing');
  assert(slack, 'Slack card missing');
  assert(document.querySelector('.connection-logo.is-feishu'), 'Feishu logo marker missing');
  assert(document.querySelector('.connection-logo.is-slack'), 'Slack logo marker missing');
  assert(panelText.includes('已启用') || panelText.includes('已配置'), 'configured/enabled state missing');
  assert(panelText.includes('智能体可调用'), 'callable state missing');
  assert(panelText.includes('运行依赖缺失'), 'localized dependency-missing state missing');
  assert(panelText.includes('不是凭据校验失败'), 'dependency-missing explanation missing');
  assert(panelText.includes('允许所有用户'), 'localized field label missing');
  assert(!panelText.includes('no agent tool is declared for this channel'), 'raw agent callable reason leaked');
  assert(!panelText.includes('auth unknown'), 'raw auth state leaked');
  assert(feishu.querySelector('input[type="password"]'), 'secret field is not masked as password input');
  assert(text(slack).includes('该平台使用扫码、授权或运行时状态完成连接'), 'empty setup hint missing');
  assert(Array.from(feishu.querySelectorAll('button')).some((button) => text(button).includes('设为投递目标')), 'projected home-channel action missing');
  assert(!document.body.innerText.includes('Run Center'), 'Run Center leaked into production settings smoke');

  const saveButton = Array.from(feishu.querySelectorAll('button')).find((button) => text(button).includes('保存'));
  assert(saveButton, 'save action missing');
  saveButton.click();
  await wait('save action call', () => window.__ecorexSmoke.calls.actions.some((call) => call.target === 'feishu' && call.action === 'save_config'));
  await wait('home-channel action remains disabled without raw id', () => {
    const button = Array.from(feishu.querySelectorAll('button')).find((item) => text(item).includes('设为投递目标'));
    return Boolean(button && button.disabled);
  });
  const homeButton = Array.from(feishu.querySelectorAll('button')).find((button) => text(button).includes('设为投递目标'));
  assert(homeButton, 'home-channel action missing');
  assert(homeButton.disabled, 'home-channel action should be disabled when API only returns a hashed Feishu homeChannel');
  const saveCall = window.__ecorexSmoke.calls.actions.find((call) => call.target === 'feishu' && call.action === 'save_config');
  assert(saveCall.secretEchoed === false, 'masked secret was echoed back in save_config');
  assert(!saveCall.configKeys.includes('feishu_app_id'), 'masked Feishu App ID was echoed back in save_config');
  assert(!saveCall.configKeys.includes('feishu_app_secret'), 'masked Feishu App Secret was echoed back in save_config');
  assert(saveCall.configKeys.includes('allow_all_users'), 'boolean config field missing from save_config');

  return {
    connectionCards: cards.length,
    externalListCalls: window.__ecorexSmoke.calls.externalList,
    actionCalls: window.__ecorexSmoke.calls.actions.length,
    hasFeishuLogo: Boolean(document.querySelector('.connection-logo.is-feishu')),
    hasSlackLogo: Boolean(document.querySelector('.connection-logo.is-slack')),
    homeChannelActionVisible: Array.from(feishu.querySelectorAll('button')).some((button) => text(button).includes('设为投递目标')),
    homeChannelActionUsable: false,
    secretRedactedOnSave: saveCall.secretEchoed === false,
    localizedExternalConnectionText: panelText.includes('智能体可调用') && panelText.includes('允许所有用户') && panelText.includes('运行依赖缺失'),
    runCenterHidden: !document.body.innerText.includes('Run Center')
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
        "fixtureHash": _h("external-connections-browser"),
        "screenshot": screenshot_name,
        "metrics": metrics,
        "consoleErrorCount": len(errors),
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL", "consoleErrorCount": len(errors)}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-07/R23-12 External Connections browser smoke.")
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
