#!/usr/bin/env python3
"""Static browser smoke for the Codex-like user attachment bubble.

This smoke does not need a running EcoreX backend. It renders the real desktop
CSS against a minimal transcript DOM with one user message containing text,
a document attachment, and an image attachment.
"""

from __future__ import annotations

import argparse
import base64
import json
import textwrap
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
TOKENS_CSS = ROOT / "desktop" / "src" / "styles" / "tokens.css"
APP_CSS = ROOT / "desktop" / "src" / "styles" / "app.css"
ARTIFACT_DIR = ROOT / "docs" / "v0.2.3" / "artifacts"

MESSAGE_TEXT = (
    "\u5e2e\u5fd9\u7f8e\u5316\u4e0b\uff0c\u4e3b\u8272\u8fd8\u662f"
    "\u7528\u4ea6\u82af\u7684\u6a59\u8272\uff0c\u6211\u73b0\u5728"
    "\u7684\u8868\u6846\u592a\u5f3a\u4e86\uff0c\u5e2e\u5fd9\u8c03"
    "\u6574\u6210\u50cf Codex \u8fd9\u6837\u7b80\u7ea6\u7684\u6837\u5f0f"
)
DOC_NAME = "\u6d59\u6c5f26Q2\u590d\u76d8\u53caQ3\u89c4\u5212.pptx"


def _read_css() -> str:
    return "\n".join([
        TOKENS_CSS.read_text(encoding="utf-8"),
        APP_CSS.read_text(encoding="utf-8"),
    ])


def _fixture_html(theme: str) -> str:
    image_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="112" viewBox="0 0 160 112">'
        '<rect width="160" height="112" rx="12" fill="#fff7ed"/>'
        '<rect x="12" y="12" width="136" height="18" rx="5" fill="#f97316"/>'
        '<rect x="16" y="42" width="36" height="10" rx="2" fill="#fb923c"/>'
        '<rect x="62" y="42" width="36" height="10" rx="2" fill="#fed7aa"/>'
        '<rect x="108" y="42" width="36" height="10" rx="2" fill="#f97316"/>'
        '<rect x="16" y="62" width="128" height="7" rx="2" fill="#f97316"/>'
        '<rect x="16" y="76" width="128" height="7" rx="2" fill="#22c55e"/>'
        '<rect x="16" y="90" width="88" height="7" rx="2" fill="#38bdf8"/>'
        '</svg>'
    )
    image_data = "data:image/svg+xml;base64," + base64.b64encode(image_svg.encode("utf-8")).decode("ascii")
    css = _read_css()
    return f"""<!doctype html>
<html data-theme="{theme}">
<head>
  <meta charset="utf-8" />
  <style>{css}</style>
  <style>
    html, body, #root {{ width: 100%; height: 100%; overflow: hidden; }}
    body {{ display: grid; place-items: center; padding: 24px; }}
    .smoke-frame {{ width: min(960px, calc(100vw - 48px)); display: grid; gap: 16px; }}
    .message {{ content-visibility: visible; contain-intrinsic-size: auto; }}
    .message-copy-button {{ opacity: 1; }}
    .smoke-caption {{ color: var(--color-muted); font: 12px var(--font-sans); }}
  </style>
</head>
<body>
  <main id="smoke-root" class="smoke-frame">
    <article class="message user has-files" data-smoke-message="1">
      <div class="message-body">
        <div class="message-files">
          <button type="button" title="{DOC_NAME}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <path d="M14 2v6h6"/>
              <path d="M8 13h8M8 17h5"/>
            </svg>
            <span>{DOC_NAME}</span>
          </button>
          <button type="button" title="preview image">
            <img src="{image_data}" alt="preview" />
            <span>37304f6f5e407a4.png</span>
          </button>
        </div>
        <div class="message-text-bubble">
          <button type="button" class="message-copy-button" title="copy" aria-label="copy">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <rect x="9" y="9" width="13" height="13" rx="2"/>
              <rect x="2" y="2" width="13" height="13" rx="2"/>
            </svg>
          </button>
          <div class="message-content">
            <div class="markdown-content"><p>{MESSAGE_TEXT}</p></div>
          </div>
        </div>
      </div>
    </article>
  </main>
</body>
</html>"""


def _collect_metrics(page: Any, theme: str, width: int) -> dict[str, Any]:
    return page.evaluate(
        """({theme, width}) => {
          const rectOf = (selector) => {
            const el = document.querySelector(selector);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
          };
          const styleOf = (selector) => {
            const el = document.querySelector(selector);
            if (!el) return null;
            const s = getComputedStyle(el);
            return {
              background: s.backgroundColor,
              borderColor: s.borderColor,
              borderRadius: s.borderRadius,
              display: s.display
            };
          };
          const body = rectOf('.message.user .message-body');
          const bubble = rectOf('.message.user .message-text-bubble');
          const files = rectOf('.message.user .message-files');
          const fileButtons = Array.from(document.querySelectorAll('.message.user .message-files button')).map((el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return {width: r.width, height: r.height, background: s.backgroundColor, borderColor: s.borderColor};
          });
          const text = document.querySelector('.message.user .markdown-content')?.textContent || '';
          const bodyStyle = styleOf('.message.user .message-body');
          const bubbleStyle = styleOf('.message.user .message-text-bubble');
          const ok = Boolean(
            body && bubble && files &&
            bodyStyle && bodyStyle.background === 'rgba(0, 0, 0, 0)' &&
            bubble.width <= body.width + 1 &&
            files.width <= body.width + 1 &&
            fileButtons.length === 2 &&
            fileButtons[0].height <= 48 &&
            fileButtons[1].width <= 110 &&
            fileButtons[1].height <= 86 &&
            text.includes('Codex')
          );
          return {theme, width, ok, body, bubble, files, fileButtons, bodyStyle, bubbleStyle, textLength: text.length};
        }""",
        {"theme": theme, "width": width},
    )


def _render_case(browser: Any, *, theme: str, width: int, height: int, screenshot: Path) -> dict[str, Any]:
    page = browser.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type == "error" else None)
    page.set_content(_fixture_html(theme), wait_until="load")
    page.locator('[data-smoke-message="1"]').screenshot(path=str(screenshot))
    metrics = _collect_metrics(page, theme, width)
    page.close()
    metrics["screenshot"] = screenshot.resolve().relative_to(ROOT.resolve()).as_posix()
    metrics["console_errors"] = errors
    return metrics


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cases = [
        ("light", 1120, 420, ARTIFACT_DIR / "chat-attachment-bubble-light.png"),
        ("dark", 1120, 420, ARTIFACT_DIR / "chat-attachment-bubble-dark.png"),
        ("light", 390, 420, ARTIFACT_DIR / "chat-attachment-bubble-narrow.png"),
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        metrics = [
            _render_case(browser, theme=theme, width=width, height=height, screenshot=screenshot)
            for theme, width, height, screenshot in cases
        ]
        browser.close()
    result = {
        "status": "PASS" if all(item.get("ok") and not item.get("console_errors") for item in metrics) else "FAIL",
        "duration_ms": round((time.time() - started) * 1000),
        "metrics": metrics,
    }
    output_path = ARTIFACT_DIR / "chat-attachment-bubble-smoke.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["artifact"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run static chat attachment bubble smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of headless.")
    args = parser.parse_args()
    try:
        result = run_smoke(args)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
