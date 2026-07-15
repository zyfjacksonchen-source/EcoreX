#!/usr/bin/env python3
"""Browser smoke proving ordinary Web users do not see Run Center UI."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _browser_probe_script() -> str:
    forbidden = [
        "Run Center",
        "RUNCENTER",
        "runCenter",
        "RUN_CENTER",
        "run-center",
        "运行中心",
    ]
    return f"""
(() => {{
  function assert(condition, message) {{
    if (!condition) throw new Error(message);
  }}
  const forbidden = {json.dumps(forbidden)};
  const text = document.body.innerText || '';
  const html = document.documentElement.outerHTML || '';
  const selectorMatches = Array.from(document.querySelectorAll(
    '[class*="run-center" i], [id*="run-center" i], [aria-label*="Run Center" i], [title*="Run Center" i], [data-run-center-surface]'
  )).map((node) => ({{
    tag: node.tagName,
    className: node.className || '',
    id: node.id || '',
    ariaLabel: node.getAttribute('aria-label') || '',
    title: node.getAttribute('title') || '',
    text: (node.textContent || '').trim().slice(0, 80)
  }}));
  const visibleMatches = forbidden.filter((marker) => text.includes(marker));
  const htmlMatches = forbidden.filter((marker) => html.includes(marker));
  assert(visibleMatches.length === 0, `Run Center visible text leaked: ${{visibleMatches.join(', ')}}`);
  assert(htmlMatches.length === 0, `Run Center DOM/source marker leaked: ${{htmlMatches.join(', ')}}`);
  assert(selectorMatches.length === 0, `Run Center selector surfaced: ${{JSON.stringify(selectorMatches)}}`);
  const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).map((node) => ({{
    text: (node.textContent || '').trim(),
    ariaLabel: node.getAttribute('aria-label') || '',
    title: node.getAttribute('title') || '',
    className: node.className || ''
  }}));
  const buttonLeaks = buttons.filter((button) => forbidden.some((marker) => (
    button.text.includes(marker)
    || button.ariaLabel.includes(marker)
    || button.title.includes(marker)
    || String(button.className).includes(marker)
  )));
  assert(buttonLeaks.length === 0, `Run Center button leaked: ${{JSON.stringify(buttonLeaks)}}`);
  const metrics = {{
    title: document.title,
    forbidden,
    visibleTextLength: text.length,
    htmlLength: html.length,
    selectorMatches,
    buttonCount: buttons.length,
    buttonLeaks,
    hasChatRoot: !!document.querySelector('#chat-messages'),
    hasSettingsButton: !!document.querySelector('#settings-btn')
  }};
  assert(metrics.hasChatRoot, 'chat root did not load');
  return metrics;
}})()
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    result: dict[str, Any]
    with web_asset_server() as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(base_api_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_selector("#chat-messages", timeout=args.timeout_ms)
            metrics = page.evaluate(_browser_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_target), full_page=True)
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
    parser = argparse.ArgumentParser(description="Run Web Run Center hiding browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument(
        "--screenshot",
        default=str(ROOT / "docs" / "v0.2.2" / "artifacts" / "web-run-center-hidden-browser-smoke.png"),
        help="Optional screenshot path for the ordinary Web UI.",
    )
    args = parser.parse_args()
    print(json.dumps(run_smoke(args), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
