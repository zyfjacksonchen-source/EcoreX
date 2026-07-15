#!/usr/bin/env python3
"""Browser smoke for Web channel transport/auth/agent observability."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _channels_stub_script() -> str:
    extra_fetch_cases = r"""
    if (path === '/api/channels') {
      return makeResponse({
        status: 'success',
        channels: [
          {
            name: 'feishu',
            aliases: ['lark'],
            label: { zh: '飞书', en: 'Feishu / Lark' },
            description: 'Feishu/Lark bot channel using app credentials and websocket events.',
            icon: 'fa-paper-plane',
            color: 'blue',
            active: true,
            configured: true,
            running: false,
            status: 'configured',
            configState: 'configured',
            last_error: '',
            operation_id: 'feishu-browser-smoke',
            auth: {
              mode: 'bot_app_credentials',
              channelAuthorization: 'app_credentials',
              channelAuthSupported: true,
              authEndpoint: '/api/feishu/register',
              authEndpointMethods: ['GET', 'POST'],
              statusProbe: 'credential_configured_only',
              channelConfigState: 'configured',
              requiredFields: ['feishu_app_id', 'feishu_app_secret'],
              presentFields: ['feishu_app_id', 'feishu_app_secret'],
              missingFields: [],
              agentAuthSupported: true,
              agentAuthorizationAction: { tool: 'feishu_cli', action: 'auth_login', domain: 'base' }
            },
            agentSurface: {
              tool: 'feishu_cli',
              declaredDiscoverable: true,
              schemaVisible: true,
              discoverable: true,
              toolSchemaCallable: true,
              callable: false,
              readiness: 'unverified',
              callableReason: 'tool schema is visible, but CLI/auth readiness requires an explicit status probe',
              requiresStatusProbe: true,
              permissionGated: true,
              policy: 'find-skill-first-on-demand-cli',
              statusAction: { tool: 'feishu_cli', action: 'status' },
              authorizationAction: { tool: 'feishu_cli', action: 'auth_login', domain: 'base' },
              status: 'schema_visible_unverified'
            },
            fields: [
              { key: 'feishu_app_id', label: 'App ID', type: 'text', value: 'cli_aabbcc' },
              { key: 'feishu_app_secret', label: 'App Secret', type: 'secret', value: 'super-secret-value' }
            ]
          },
          {
            name: 'slack',
            aliases: [],
            label: { zh: 'Slack', en: 'Slack' },
            description: 'Slack bot channel.',
            icon: 'fa-hashtag',
            color: 'purple',
            active: false,
            configured: true,
            running: false,
            status: 'configured',
            configState: 'configured',
            last_error: '',
            auth: {
              mode: 'bot_tokens',
              channelAuthorization: 'app_credentials',
              channelAuthSupported: true,
              authEndpoint: '',
              authEndpointMethods: [],
              statusProbe: 'credential_configured_only',
              channelConfigState: 'configured',
              requiredFields: ['slack_bot_token', 'slack_app_token'],
              presentFields: ['slack_bot_token', 'slack_app_token'],
              missingFields: [],
              agentAuthSupported: false,
              agentAuthorizationAction: null
            },
            agentSurface: {
              tool: '',
              declaredDiscoverable: false,
              schemaVisible: null,
              discoverable: false,
              toolSchemaCallable: false,
              callable: false,
              readiness: 'not_applicable',
              callableReason: 'no agent tool is declared for this channel',
              requiresStatusProbe: false,
              permissionGated: false,
              status: 'not_applicable'
            },
            fields: [
              { key: 'slack_bot_token', label: 'Bot Token', type: 'secret', value: 'xoxb-secret-value' },
              { key: 'slack_app_token', label: 'App Token', type: 'secret', value: 'xapp-star*raw-secret-value' }
            ]
          },
          {
            name: 'telegram',
            aliases: [],
            label: { zh: 'Telegram', en: 'Telegram' },
            description: 'Telegram bot channel.',
            icon: 'fa-paper-plane',
            color: 'sky',
            active: true,
            configured: false,
            running: false,
            status: 'error',
            configState: 'missing',
            last_error: 'missing Telegram bot token',
            auth: {
              mode: 'bot_token',
              channelAuthorization: 'app_credentials',
              channelAuthSupported: true,
              authEndpoint: '',
              authEndpointMethods: [],
              statusProbe: 'credential_configured_only',
              channelConfigState: 'missing',
              requiredFields: ['telegram_token'],
              presentFields: [],
              missingFields: ['telegram_token'],
              agentAuthSupported: false,
              agentAuthorizationAction: null
            },
            agentSurface: {
              tool: '',
              declaredDiscoverable: false,
              schemaVisible: null,
              discoverable: false,
              toolSchemaCallable: false,
              callable: false,
              readiness: 'not_applicable',
              callableReason: 'no agent tool is declared for this channel',
              requiresStatusProbe: false,
              permissionGated: false,
              status: 'not_applicable'
            },
            fields: [
              { key: 'telegram_token', label: 'Bot Token', type: 'secret', value: '' }
            ]
          },
          {
            name: "evil');window.__channelNameXss=1;//",
            aliases: [],
            label: { zh: '<img src=x onerror=window.__channelLabelXss=1>', en: '<img src=x onerror=window.__channelLabelXss=1>' },
            description: 'Hostile metadata channel for browser smoke.',
            icon: 'fa-paper-plane" onclick="window.__channelIconXss=1',
            color: 'red" onmouseover="window.__channelColorXss=1',
            active: false,
            configured: false,
            running: false,
            status: 'available',
            configState: 'missing',
            last_error: '',
            auth: {
              mode: 'bot_token',
              channelAuthorization: 'app_credentials',
              channelAuthSupported: true,
              authEndpoint: '',
              authEndpointMethods: [],
              statusProbe: '',
              channelConfigState: 'missing',
              requiredFields: ['evil_secret'],
              presentFields: [],
              missingFields: ['evil_secret'],
              agentAuthSupported: false,
              agentAuthorizationAction: null
            },
            agentSurface: {
              tool: '',
              declaredDiscoverable: false,
              schemaVisible: null,
              discoverable: false,
              toolSchemaCallable: false,
              callable: false,
              readiness: 'not_applicable',
              callableReason: 'no agent tool is declared for this channel',
              requiresStatusProbe: false,
              permissionGated: false,
              status: 'not_applicable'
            },
            fields: [
              { key: 'evil_secret', label: 'Evil Secret', type: 'secret', value: 'evil-raw-secret-value' }
            ]
          }
        ]
      });
    }
"""
    return base_api_stub_script(extra_fetch_cases)


def _channels_probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  currentLang = 'en';
  applyI18n();
  navigateTo('channels');
  loadChannelsView();

  const wait = (label, predicate, timeout = 5000) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - start > timeout) return reject(new Error(`timeout waiting for channels UI: ${label}`));
      setTimeout(tick, 25);
    };
    tick();
  });

  await wait('channel cards', () => document.querySelectorAll('[data-channel-card="1"]').length >= 4);
  await wait('observability rows', () => document.querySelectorAll('.channel-observability-panel').length >= 4);

  const feishu = document.getElementById('channel-card-feishu');
  const slack = document.getElementById('channel-card-slack');
  const telegram = document.getElementById('channel-card-telegram');
  const hostile = Array.from(document.querySelectorAll('[data-channel-card="1"]'))
    .find((card) => (card.dataset.channelName || '').includes('__channelNameXss'));
  assert(feishu, 'feishu card missing');
  assert(slack, 'slack card missing');
  assert(telegram, 'telegram card missing');
  assert(hostile, 'hostile metadata channel card missing');

  const feishuText = feishu.innerText;
  const feishuAgent = feishu.querySelector('[data-channel-state-row="agent"]');
  assert(feishuText.includes('Enabled, not running'), 'feishu transport overclaimed connection');
  assert(!feishuText.includes('Connected'), 'feishu card still says Connected');
  assert(feishuText.includes('/api/feishu/register'), 'feishu auth endpoint is not visible');
  assert(feishuText.includes('Agent auth action'), 'feishu agent authorization action is hidden');
  assert(feishuText.includes('feishu_cli'), 'feishu agent tool name is hidden');
  assert(feishuText.includes('Auth/probe pending'), 'feishu schema-visible unverified state is hidden');
  assert(feishuText.includes('Schema visible'), 'feishu schema visibility is hidden');
  assert(feishuText.includes('Not callable'), 'feishu not-callable state is hidden');
  assert(feishuText.includes('Status probe required'), 'feishu status probe requirement is hidden');
  assert(feishuText.includes('Permission gated'), 'feishu permission gate is hidden');
  assert(feishuAgent.dataset.agentCallable === 'false', 'feishu agent callable dataset overclaims');
  assert(feishuAgent.dataset.agentStatus === 'schema_visible_unverified', 'feishu agent status dataset missing');

  const slackText = slack.innerText;
  assert(slackText.includes('Configured'), 'slack configured state is hidden');
  assert(slackText.includes('Not applicable'), 'slack should show no declared agent tool');

  const telegramText = telegram.innerText;
  assert(telegramText.includes('Error'), 'telegram error state is hidden');
  assert(telegramText.includes('missing Telegram bot token'), 'telegram last_error is hidden');
  assert(telegramText.includes('Missing: telegram token'), 'telegram missing field is hidden');

  const attrText = Array.from(document.querySelectorAll('#view-channels *'))
    .flatMap((el) => Array.from(el.attributes || []).map((attr) => `${attr.name}=${attr.value}`))
    .join('\n');
  const inputValues = Array.from(document.querySelectorAll('#view-channels input'))
    .map((input) => input.value || '')
    .join('\n');
  const leakSurface = [
    document.getElementById('view-channels').innerText,
    document.getElementById('view-channels').innerHTML,
    attrText,
    inputValues
  ].join('\n');
  ['super-secret-value', 'xoxb-secret-value', 'xapp-star*raw-secret-value', 'evil-raw-secret-value'].forEach((needle) => {
    assert(!leakSurface.includes(needle), `raw secret leaked into channel UI: ${needle}`);
  });
  assert(!window.__channelNameXss && !window.__channelLabelXss && !window.__channelIconXss && !window.__channelColorXss, 'hostile channel metadata executed');
  assert(document.querySelectorAll('#view-channels [onclick*="connectChannelConfig"]').length === 0, 'connectChannelConfig inline handler remains');
  assert(document.querySelectorAll('#view-channels [onclick*="saveChannelConfig"]').length === 0, 'saveChannelConfig inline handler remains');
  assert(document.querySelectorAll('#view-channels [onclick*="disconnectChannel"]').length === 0, 'disconnectChannel inline handler remains');
  assert(document.querySelectorAll('#view-channels [onmouseover], #view-channels [onerror]').length === 0, 'hostile metadata created inline event attributes');

  return {
    cards: document.querySelectorAll('[data-channel-card="1"]').length,
    panels: document.querySelectorAll('.channel-observability-panel').length,
    feishuAgentCallable: feishuAgent.dataset.agentCallable,
    feishuAgentStatus: feishuAgent.dataset.agentStatus,
    feishuText,
    slackText,
    telegramText,
    hostileHtml: hostile.innerHTML,
    inputValues
  };
})();
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    with web_asset_server() as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_channels_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof loadChannelsView === 'function' && typeof buildChannelObservabilityHtml === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_channels_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator("#view-channels").screenshot(path=str(screenshot_target))
                screenshot_path = str(screenshot_target)
            browser.close()
    result = {
        "status": "PASS",
        "url": url,
        "duration_ms": round((time.time() - started) * 1000),
        "screenshot": screenshot_path,
        "metrics": metrics,
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web channels observability browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the channels view.")
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
