#!/usr/bin/env python3
"""Browser smoke for the Web Markdown-it renderer.

This script starts a tiny local static server that mirrors the Web channel's
asset mapping, loads the real chat page in Chromium through Playwright, and
verifies the Markdown renderer in the browser DOM. It intentionally stubs API
calls so the smoke stays focused on Web assets and projection rendering rather
than model/provider availability.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _stubbed_api_script() -> str:
    return base_api_stub_script()


def _browser_probe_script(final_fixture: str, long_fixture: str) -> str:
    return f"""
(async () => {{
  const finalFixture = {json.dumps(final_fixture)};
  const longFixture = {json.dumps(long_fixture)};

  function assert(condition, message) {{
    if (!condition) throw new Error(message);
  }}

  function renderIntoHost(html) {{
    const host = document.createElement('div');
    host.className = 'msg-content text-slate-700';
    host.innerHTML = html;
    document.body.appendChild(host);
    applyHighlighting(host);
    return host;
  }}

  function settleHighlighting() {{
    return new Promise((resolve) => setTimeout(resolve, 25));
  }}

  const finalHost = renderIntoHost(`<div class="answer-content">${{renderAnswerHtml(finalFixture)}}</div>`);
  const answer = finalHost.querySelector('.answer-content');
  answer.dataset.rawMd = finalFixture;

  const streamingCases = {{
    loneHeading: '#',
    stableHeading: '# Browser Smoke',
    danglingList: '- ',
    stableList: '- item',
    partialFence: '``',
    openFence: '```javascript\\nconst x = 1 < 2',
    tableDelimiter: '| --- |',
    partialTableRow: '| A | B',
    partialLink: '[label](',
    partialImage: '![alt',
    partialStrong: '**bold',
    xss: '<img src=x onerror=alert(1)>'
  }};
  const streaming = {{}};
  for (const [name, source] of Object.entries(streamingCases)) {{
    const host = renderIntoHost(renderStreamingMarkdown(source));
    streaming[name] = {{
      text: host.innerText,
      html: host.innerHTML,
      h1: host.querySelectorAll('h1').length,
      listItems: host.querySelectorAll('li').length,
      codeBlocks: host.querySelectorAll('pre').length,
      copyButtons: host.querySelectorAll('.code-copy-btn').length
    }};
  }}

  const bot = createBotMessageEl(finalFixture, new Date(), 'req-browser-markdown-smoke', {{ role: 'assistant' }});
  document.getElementById('chat-messages').appendChild(bot);

  const runningProjection = {{
    request_id: 'req-browser-projection-smoke',
    session_id: 'session-browser-projection-smoke',
    state: 'running',
    latest_event_id: 7,
    messages: [{{ role: 'assistant', content: '# Streaming Projection' }}]
  }};
  renderRuntimeProjectionRequest(runningProjection, 'browser_smoke');
  const projected = document.querySelector('[data-request-id="req-browser-projection-smoke"]');

  const longHost = renderIntoHost(renderAnswerHtml(longFixture));

  await settleHighlighting();

  const styleProbe = finalHost.querySelector('p') || finalHost;
  const preProbe = finalHost.querySelector('pre');
  const ulProbe = finalHost.querySelector('ul');
  const style = window.getComputedStyle(styleProbe);
  const preStyle = preProbe ? window.getComputedStyle(preProbe) : null;
  const ulStyle = ulProbe ? window.getComputedStyle(ulProbe) : null;

  const metrics = {{
    title: document.title,
    rendererReady: typeof renderMarkdown === 'function' && typeof renderStreamingMarkdown === 'function',
    final: {{
      h1Text: finalHost.querySelector('h1')?.textContent || '',
      listItems: finalHost.querySelectorAll('li').length,
      tableCells: finalHost.querySelectorAll('td').length,
      codeWrappers: finalHost.querySelectorAll('.code-block-wrapper').length,
      copyButtons: finalHost.querySelectorAll('.code-copy-btn').length,
      safeLinks: finalHost.querySelectorAll('a[target="_blank"][rel~="noopener"][rel~="noreferrer"]').length,
      imageArtifacts: finalHost.querySelectorAll('[data-artifact-kind="image"]').length,
      rawScriptableImages: finalHost.querySelectorAll('img[src="x"]').length,
      rawMdPreserved: answer.dataset.rawMd === finalFixture,
      visibleText: finalHost.innerText
    }},
    streaming,
    bot: {{
      rawMdPreserved: bot.querySelector('.answer-content')?.dataset.rawMd === finalFixture,
      h1Text: bot.querySelector('h1')?.textContent || '',
      codeWrappers: bot.querySelectorAll('.code-block-wrapper').length
    }},
    projection: {{
      inserted: !!projected,
      source: projected?.dataset.runtimeProjectionSource || '',
      state: projected?.dataset.runtimeProjectionState || '',
      hasStreamingPreview: !!projected?.querySelector('.streaming-markdown-preview'),
      h1Text: projected?.querySelector('h1')?.textContent || ''
    }},
    longAnswer: {{
      hasPreview: longHost.querySelectorAll('.long-answer-preview').length,
      previewHeadings: longHost.querySelectorAll('.long-answer-preview h1').length,
      hasToggle: longHost.querySelectorAll('[data-long-answer-toggle]').length
    }},
    css: {{
      paragraphLineHeight: style.lineHeight,
      preOverflowX: preStyle ? preStyle.overflowX : '',
      listStyleType: ulStyle ? ulStyle.listStyleType : ''
    }}
  }};

  assert(metrics.rendererReady, 'markdown renderer functions were not ready');
  assert(metrics.final.h1Text.includes('Browser Smoke'), 'final heading did not render');
  assert(metrics.final.listItems >= 2, 'final list did not render');
  assert(metrics.final.tableCells >= 2, 'final table did not render');
  assert(metrics.final.codeWrappers >= 1, 'code block wrapper missing');
  assert(metrics.final.copyButtons >= 1, 'code copy button missing');
  assert(metrics.final.safeLinks >= 2, 'safe target/rel links missing');
  assert(metrics.final.imageArtifacts >= 1, 'image preview artifact missing');
  assert(metrics.final.rawScriptableImages === 0, 'raw scriptable image leaked');
  assert(metrics.final.rawMdPreserved, 'answer raw Markdown copy payload was not preserved');
  assert(metrics.streaming.stableHeading.h1 >= 1, 'stable streaming heading did not render');
  assert(!/^#\\s*$/.test(metrics.streaming.loneHeading.text.trim()), 'lone streaming # marker is visible');
  assert(metrics.streaming.stableList.listItems >= 1, 'stable streaming list did not render');
  assert(!metrics.streaming.danglingList.text.includes('-'), 'dangling streaming list marker is visible');
  assert(!metrics.streaming.partialFence.text.includes('``'), 'partial code fence marker is visible');
  assert(!metrics.streaming.openFence.text.includes('```'), 'open code fence marker is visible');
  assert(metrics.streaming.openFence.codeBlocks >= 1, 'open code fence preview missing');
  assert(!metrics.streaming.tableDelimiter.text.includes('| --- |'), 'partial table delimiter is visible');
  assert(!metrics.streaming.partialTableRow.text.includes('| A | B'), 'partial table row is visible');
  assert(!metrics.streaming.partialLink.text.includes('[label]('), 'partial link marker is visible');
  assert(!metrics.streaming.partialImage.text.includes('![alt'), 'partial image marker is visible');
  assert(!metrics.streaming.partialStrong.text.includes('**bold'), 'partial strong marker is visible');
  assert(metrics.streaming.xss.text.includes('<img'), 'escaped XSS text missing');
  assert(!metrics.streaming.xss.html.includes('<img src=x onerror'), 'streaming raw XSS HTML leaked');
  assert(metrics.bot.rawMdPreserved, 'bot message raw Markdown was not preserved');
  assert(metrics.projection.inserted, 'runtime projection did not insert a bot message');
  assert(metrics.projection.hasStreamingPreview, 'running projection did not use streaming Markdown preview');
  assert(metrics.longAnswer.hasPreview === 1 && metrics.longAnswer.previewHeadings >= 1, 'long answer preview did not render Markdown before clipping');
  assert(metrics.css.preOverflowX === 'auto', 'code block overflow style missing');
  assert(metrics.css.listStyleType === 'disc', 'list style did not load from CSS');

  finalHost.setAttribute('data-smoke-final-host', '1');
  return metrics;
}})();
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    final_fixture = "\n".join(
        [
            "# Browser Smoke \u2713",
            "",
            "first line",
            "second line \U0001f600",
            "",
            "- item one",
            "- item two",
            "",
            "> quoted content",
            "",
            "| A | B |",
            "| --- | --- |",
            "| 1 | 2 |",
            "",
            "```javascript",
            "const x = 1 < 2;",
            "```",
            "",
            "See https://example.com/page",
            "",
            "https://example.com/a.png",
            "",
            "<img src=x onerror=alert(1)>",
        ]
    )
    long_fixture = (final_fixture + "\n\n") * 80

    errors: list[str] = []
    result: dict[str, Any]
    started = time.time()
    with web_asset_server() as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(_stubbed_api_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof renderMarkdown === 'function' && typeof renderStreamingMarkdown === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_browser_probe_script(final_fixture, long_fixture))
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator('[data-smoke-final-host="1"]').screenshot(path=str(screenshot_target))
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
    parser = argparse.ArgumentParser(description="Run Web Markdown-it browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the final rendered fixture.")
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
