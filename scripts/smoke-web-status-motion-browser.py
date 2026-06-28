#!/usr/bin/env python3
"""Browser smoke for sweep-free Web status motion.

The smoke loads the real Web channel assets in Chromium, drives the production
SSE renderer with a deterministic EventSource, and verifies status text does
not use broad or glyph sweep animation while the small activity dot remains.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _browser_probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitFor(predicate, message) {
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      const value = predicate();
      if (value) return value;
      await sleep(25);
    }
    throw new Error(message);
  }

  const OriginalEventSource = window.EventSource;
  const instances = [];
  class MotionSmokeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = {};
      this.closed = false;
      instances.push(this);
      setTimeout(() => this._emit('open', { type: 'open' }), 0);
    }
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    }
    removeEventListener(type, handler) {
      this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== handler);
    }
    _emit(type, event) {
      (this.listeners[type] || []).forEach((handler) => handler(event));
      const direct = this[`on${type}`];
      if (typeof direct === 'function') direct(event);
    }
    emit(item, lastEventId) {
      this._emit('message', {
        type: 'message',
        data: JSON.stringify(item),
        lastEventId: String(lastEventId || '')
      });
    }
    close() {
      this.closed = true;
      this.readyState = 2;
    }
  }

  function styleMetrics(el) {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return {
      animationName: style.animationName,
      backgroundImage: style.backgroundImage,
      backgroundSize: style.backgroundSize,
      backgroundClip: style.backgroundClip,
      webkitBackgroundClip: style.webkitBackgroundClip,
      color: style.color,
      webkitTextFillColor: style.webkitTextFillColor,
      width: rect.width,
      height: rect.height,
      className: el.className
    };
  }

  function readMotionMetrics() {
    const status = document.querySelector('.agent-current-phase.ecorex-activity-status');
    const statusText = status?.querySelector('.agent-current-phase-text');
    const liveTool = document.querySelector('.agent-tool-step[data-tool-call-id="tool-motion-live"]');
    const liveMeta = liveTool?.querySelector('.tool-live-meta');
    const terminalTool = document.querySelector('.agent-tool-step[data-tool-call-id="tool-motion-terminal"]');
    const terminalMeta = terminalTool?.querySelector('.tool-live-meta');
    const dot = status?.querySelector('.ecorex-activity-dot');
    assert(status && statusText, 'production phase status DOM missing');
    assert(liveTool && liveMeta, 'production live tool DOM missing');
    assert(terminalTool && terminalMeta, 'production terminal tool DOM missing');

    const containerStyle = window.getComputedStyle(status);
    const pseudoAfter = window.getComputedStyle(status, '::after');
    const statusRect = status.getBoundingClientRect();
    const dotStyle = window.getComputedStyle(dot);
    return {
      statusText: styleMetrics(statusText),
      liveMeta: styleMetrics(liveMeta),
      terminalMeta: {
        ...styleMetrics(terminalMeta),
        isLive: terminalMeta.classList.contains('is-live'),
        text: terminalMeta.textContent || ''
      },
      container: {
        animationName: containerStyle.animationName,
        backgroundImage: containerStyle.backgroundImage,
        width: statusRect.width,
        height: statusRect.height,
        pseudoAfterContent: pseudoAfter.content,
        pseudoAfterAnimationName: pseudoAfter.animationName,
        pseudoAfterBackgroundImage: pseudoAfter.backgroundImage
      },
      dot: {
        animationName: dotStyle.animationName,
        width: dotStyle.width,
        height: dotStyle.height
      }
    };
  }

  function assertNormal(metrics) {
    const statusClip = `${metrics.statusText.backgroundClip} ${metrics.statusText.webkitBackgroundClip}`;
    const liveClip = `${metrics.liveMeta.backgroundClip} ${metrics.liveMeta.webkitBackgroundClip}`;
    assert(metrics.statusText.animationName === 'none', 'status text should not sweep animate');
    assert(metrics.liveMeta.animationName === 'none', 'live tool meta should not sweep animate');
    assert(!statusClip.includes('text'), 'status text should not be background-clipped to glyphs');
    assert(!liveClip.includes('text'), 'live tool meta should not be background-clipped to glyphs');
    assert(metrics.statusText.backgroundImage === 'none', 'status text should not keep sweep gradient');
    assert(metrics.liveMeta.backgroundImage === 'none', 'live tool meta should not keep sweep gradient');
    assert(metrics.statusText.webkitTextFillColor !== 'rgba(0, 0, 0, 0)', 'status text fill should stay visible');
    assert(metrics.liveMeta.webkitTextFillColor !== 'rgba(0, 0, 0, 0)', 'live tool meta fill should stay visible');
    assert(metrics.container.animationName === 'none', 'status container should not animate');
    assert(metrics.container.pseudoAfterContent === 'none', 'broad status ::after pseudo-element is present');
    assert(metrics.container.pseudoAfterAnimationName === 'none', 'status ::after should not animate');
    assert(metrics.container.pseudoAfterBackgroundImage === 'none', 'status ::after should not carry a light band');
    assert(metrics.dot.animationName === 'ecorexActivityPulse', 'activity dot pulse should remain small and local');
    assert(metrics.terminalMeta.isLive === false, 'terminal tool meta kept live animation class');
    assert(metrics.terminalMeta.animationName === 'none', 'terminal tool meta should not animate');
    assert(metrics.terminalMeta.backgroundImage === 'none', 'terminal tool meta should not keep sweep gradient');
  }

  window.EventSource = MotionSmokeEventSource;
  try {
    assert(typeof startSSE === 'function', 'production startSSE renderer is not available');
    startSSE('req-status-motion-smoke', null, new Date(), null);
    const stream = await waitFor(() => instances[0], 'fake EventSource was not created by startSSE');

    stream.emit({ type: 'phase', content: 'Connecting model response' }, 1);
    stream.emit({
      type: 'tool_start',
      tool: 'scheduler',
      tool_call_id: 'tool-motion-live',
      arguments: { task: 'status motion live probe' }
    }, 2);
    stream.emit({
      type: 'tool_heartbeat',
      tool: 'scheduler',
      tool_call_id: 'tool-motion-live',
      elapsed_seconds: 19,
      deadline_seconds: 60,
      max_seconds: 180,
      extension_count: 1
    }, 3);
    stream.emit({
      type: 'tool_start',
      tool: 'filesystem',
      tool_call_id: 'tool-motion-terminal',
      arguments: { task: 'status motion terminal probe' }
    }, 4);
    stream.emit({
      type: 'tool_end',
      tool: 'filesystem',
      tool_call_id: 'tool-motion-terminal',
      status: 'success',
      execution_time: 0.1,
      result: 'ok'
    }, 5);
    await sleep(80);

    const normal = readMotionMetrics();
    assertNormal(normal);
    const host = document.querySelector('.message-bot') || document.body;
    host.setAttribute('data-status-motion-smoke', '1');
    window.__statusMotionSmokeRead = readMotionMetrics;
    window.__statusMotionSmokeNormal = normal;
    window.__statusMotionSmokeStream = stream;
    return normal;
  } finally {
    window.EventSource = OriginalEventSource;
  }
})()
"""


def _reduced_motion_assert_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  const normal = window.__statusMotionSmokeNormal;
  const reduced = window.__statusMotionSmokeRead();
  assert(reduced.statusText.animationName === 'none', 'reduced-motion status text still animates');
  assert(reduced.liveMeta.animationName === 'none', 'reduced-motion live tool meta still animates');
  assert(reduced.statusText.backgroundImage === 'none', 'reduced-motion status text still has sweep gradient');
  assert(reduced.liveMeta.backgroundImage === 'none', 'reduced-motion live tool meta still has sweep gradient');
  assert(reduced.statusText.webkitTextFillColor !== 'rgba(0, 0, 0, 0)', 'reduced-motion status text fill stayed transparent');
  assert(reduced.liveMeta.webkitTextFillColor !== 'rgba(0, 0, 0, 0)', 'reduced-motion live tool meta fill stayed transparent');
  assert(Math.abs(reduced.statusText.width - normal.statusText.width) < 1.5, 'reduced-motion changed status text width');
  assert(Math.abs(reduced.liveMeta.width - normal.liveMeta.width) < 1.5, 'reduced-motion changed live tool meta width');
  assert(reduced.terminalMeta.animationName === 'none', 'reduced-motion terminal tool meta should remain static');
  assert(reduced.container.pseudoAfterContent === 'none', 'reduced-motion status ::after appeared');
  window.__statusMotionSmokeStream.emit({
    type: 'done',
    final_text: 'Final status motion answer'
  }, 6);
  await sleep(80);
  reduced.donePhaseRemaining = !!document.querySelector('.agent-current-phase.ecorex-activity-status');
  assert(!reduced.donePhaseRemaining, 'terminal done left phase status animating');
  return reduced;
})()
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
            page.wait_for_function("() => typeof startSSE === 'function'", timeout=args.timeout_ms)
            normal_metrics = page.evaluate(_browser_probe_script())

            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator('[data-status-motion-smoke="1"]').screenshot(path=str(screenshot_target))
                screenshot_path = str(screenshot_target)

            page.emulate_media(reduced_motion="reduce")
            reduced_metrics = page.evaluate(_reduced_motion_assert_script())
            browser.close()

            result = {
                "status": "PASS",
                "url": url,
                "duration_ms": round((time.time() - started) * 1000),
                "screenshot": screenshot_path,
                "metrics": {
                    "normal": normal_metrics,
                    "reducedMotion": reduced_metrics,
                },
                "console_errors": errors,
            }

    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web status motion browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument(
        "--screenshot",
        default=str(ROOT / "docs" / "v0.2.2" / "artifacts" / "web-status-motion-browser-smoke.png"),
        help="Optional screenshot path for the production-rendered status-motion probe.",
    )
    args = parser.parse_args()
    print(json.dumps(run_smoke(args), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
