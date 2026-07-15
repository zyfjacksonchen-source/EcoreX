#!/usr/bin/env python3
"""Aggregate browser smoke for Web UI polish interactions.

This smoke complements the narrower Markdown/status/project/artifact smokes by
clicking the user-facing controls that R22-10 groups together: copy controls,
artifact action menus, long-answer toggles, thinking disclosure, and ordinary
menu toggles. It loads the real Web channel page and stubs only backend APIs.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _probe_script(final_fixture: str, long_fixture: str, thinking_fixture: str) -> str:
    return f"""
(async () => {{
  const finalFixture = {json.dumps(final_fixture)};
  const longFixture = {json.dumps(long_fixture)};
  const thinkingFixture = {json.dumps(thinking_fixture)};
  const waitFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));
  const waitMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};

  window.__uiPolishClipboard = [];
  window.copyToClipboard = async (text) => {{
    window.__uiPolishClipboard.push(String(text || ''));
    return true;
  }};
  window.copyImageToClipboard = async (url) => {{
    window.__uiPolishClipboard.push(`image:${{String(url || '')}}`);
    return true;
  }};

  const messages = document.getElementById('chat-messages');
  assert(messages, 'chat messages root missing');

  const manageGroup = document.querySelector('.menu-group[data-group="manage"]');
  assert(manageGroup?.classList.contains('open'), 'manage menu group did not start open');
  manageGroup.querySelector('button').click();
  await waitFrame();
  const manageClosed = !manageGroup.classList.contains('open');
  manageGroup.querySelector('button').click();
  await waitFrame();
  const manageReopened = manageGroup.classList.contains('open');

  const attachBtn = document.getElementById('attach-btn');
  const attachMenu = document.getElementById('attach-menu');
  assert(attachBtn && attachMenu, 'attach button/menu missing');
  attachBtn.click();
  await waitFrame();
  const attachOpened = !attachMenu.classList.contains('hidden');
  document.body.click();
  await waitFrame();
  const attachClosed = attachMenu.classList.contains('hidden');

  const bot = createBotMessageEl(finalFixture, new Date(), 'req-ui-polish-copy', {{ role: 'assistant' }});
  messages.appendChild(bot);
  await waitMs(30);
  const answer = bot.querySelector('.answer-content');
  const msgCopy = bot.querySelector('.copy-msg-btn');
  assert(answer?.dataset.rawMd === finalFixture, 'message raw Markdown payload missing');
  assert(msgCopy, 'message copy button missing');
  msgCopy.style.display = '';
  msgCopy.click();
  await waitMs(30);

  const codeCopy = bot.querySelector('.code-copy-btn');
  assert(codeCopy, 'code copy button missing');
  codeCopy.click();
  await waitMs(30);

  const longHost = document.createElement('div');
  longHost.className = 'answer-content';
  longHost.dataset.rawMd = longFixture;
  longHost.innerHTML = renderAnswerHtml(longFixture);
  messages.appendChild(longHost);
  const expandBtn = longHost.querySelector('[data-long-answer-toggle="expand"]');
  assert(expandBtn, 'long-answer expand control missing');
  expandBtn.click();
  await waitMs(30);
  const longExpanded = !longHost.querySelector('.long-answer-preview') && !!longHost.querySelector('[data-long-answer-toggle="collapse"]');
  const collapseBtn = longHost.querySelector('[data-long-answer-toggle="collapse"]');
  assert(collapseBtn, 'long-answer collapse control missing after expand');
  collapseBtn.click();
  await waitMs(30);
  const longCollapsed = !!longHost.querySelector('.long-answer-preview') && !!longHost.querySelector('[data-long-answer-toggle="expand"]');

  const thinkingHost = document.createElement('div');
  thinkingHost.innerHTML = renderThinkingHtml(thinkingFixture);
  const thinkingStep = thinkingHost.firstElementChild;
  assert(thinkingStep, 'thinking step did not render');
  messages.appendChild(thinkingStep);
  const thinkingHeader = thinkingStep.querySelector('.thinking-header');
  assert(thinkingHeader, 'thinking header missing');
  const thinkingRenderedMarkdown = !!thinkingStep.querySelector('.thinking-full h2') && thinkingStep.querySelectorAll('.thinking-full li').length >= 1;
  thinkingHeader.click();
  await waitFrame();
  const thinkingExpanded = thinkingStep.classList.contains('expanded');
  thinkingHeader.click();
  await waitFrame();
  const thinkingCollapsed = !thinkingStep.classList.contains('expanded');

  const artifactHost = document.createElement('div');
  artifactHost.className = 'media-content';
  artifactHost.innerHTML = _buildArtifactHtml({{
    title: 'ui-polish-image.png',
    kind: 'image',
    path: '/assets/icon.png',
    status: 'ready'
  }});
  messages.appendChild(artifactHost);
  const artifactCard = artifactHost.querySelector('.artifact-card[data-artifact-kind="image"]');
  const artifactActions = artifactHost.querySelector('.artifact-actions');
  const artifactCopyImageButton = artifactHost.querySelector('.artifact-copy-image');
  const artifactMenuButton = artifactHost.querySelector('.artifact-menu-btn');
  assert(artifactCard && artifactActions && artifactCopyImageButton && artifactMenuButton, 'artifact action surface missing');
  artifactCopyImageButton.click();
  await waitMs(30);
  artifactMenuButton.click();
  await waitFrame();
  const artifactMenuOpened = artifactActions.classList.contains('menu-open') && artifactMenuButton.getAttribute('aria-expanded') === 'true';
  const copyLink = artifactHost.querySelector('.artifact-copy-link');
  assert(copyLink, 'artifact copy link action missing');
  copyLink.click();
  await waitMs(30);
  const artifactMenuClosedAfterCopy = !artifactActions.classList.contains('menu-open') && artifactMenuButton.getAttribute('aria-expanded') === 'false';
  artifactMenuButton.click();
  await waitFrame();
  document.body.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, composed: true }}));
  await waitFrame();
  const artifactMenuClosedOnOutside = !artifactActions.classList.contains('menu-open') && artifactMenuButton.getAttribute('aria-expanded') === 'false';

  const writes = window.__uiPolishClipboard.slice();
  const metrics = {{
    title: document.title,
    menus: {{ manageClosed, manageReopened, attachOpened, attachClosed }},
    copy: {{
      writes,
      messageCopied: writes.includes(finalFixture),
      codeCopied: writes.some((item) => item.includes('const answer = 42;')),
      artifactImageCopied: writes.some((item) => item === 'image:/assets/icon.png'),
      artifactLinkCopied: writes.some((item) => item === '/assets/icon.png'),
      artifactCopied: writes.some((item) => item.includes('/assets/icon.png'))
    }},
    longAnswer: {{ longExpanded, longCollapsed }},
    thinking: {{ thinkingRenderedMarkdown, thinkingExpanded, thinkingCollapsed }},
    artifact: {{
      cardKind: artifactCard?.dataset.artifactKind || '',
      hasActions: !!artifactActions,
      menuOpened: artifactMenuOpened,
      menuClosedAfterCopy: artifactMenuClosedAfterCopy,
      menuClosedOnOutside: artifactMenuClosedOnOutside
    }},
    text: {{
      hasRunCenter: /Run Center|RUNCENTER|run-center/.test(document.body.innerText)
    }}
  }};

  assert(metrics.menus.manageClosed && metrics.menus.manageReopened, 'sidebar group menu toggle failed');
  assert(metrics.menus.attachOpened && metrics.menus.attachClosed, 'attach menu open/close failed');
  assert(metrics.copy.messageCopied, 'message copy did not write raw Markdown');
  assert(metrics.copy.codeCopied, 'code copy did not write code text');
  assert(metrics.copy.artifactImageCopied, 'artifact image copy did not write image payload');
  assert(metrics.copy.artifactLinkCopied, 'artifact link copy did not write artifact URL');
  assert(metrics.longAnswer.longExpanded && metrics.longAnswer.longCollapsed, 'long answer toggle failed');
  assert(metrics.thinking.thinkingRenderedMarkdown, 'thinking body did not render Markdown');
  assert(metrics.thinking.thinkingExpanded && metrics.thinking.thinkingCollapsed, 'thinking disclosure did not toggle');
  assert(metrics.artifact.cardKind === 'image', 'artifact image card did not render');
  assert(metrics.artifact.hasActions && metrics.artifact.menuOpened, 'artifact action menu did not open');
  assert(metrics.artifact.menuClosedAfterCopy && metrics.artifact.menuClosedOnOutside, 'artifact menu did not close correctly');
  assert(!metrics.text.hasRunCenter, 'ordinary Web UI leaked Run Center text during UI polish smoke');

  bot.setAttribute('data-ui-polish-smoke', '1');
  return metrics;
}})();
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    final_fixture = "\n".join([
        "# UI Polish Copy",
        "",
        "Body with **Markdown** and emoji \U0001f600.",
        "",
        "```javascript",
        "const answer = 42;",
        "```",
    ])
    long_fixture = (final_fixture + "\n\n- keep rendered preview\n\n") * 90
    thinking_fixture = "\n".join(["## Thinking Summary", "", "- inspect state", "- keep output concise"])
    errors: list[str] = []
    started = time.time()

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
            page.wait_for_function(
                "() => typeof renderMarkdown === 'function' && typeof createBotMessageEl === 'function' && typeof _buildArtifactHtml === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_probe_script(final_fixture, long_fixture, thinking_fixture))
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator('[data-ui-polish-smoke="1"]').screenshot(path=str(screenshot_target))
                screenshot_path = str(screenshot_target)
            browser.close()

    result = {
        "status": "PASS",
        "duration_ms": round((time.time() - started) * 1000),
        "screenshot": screenshot_path,
        "metrics": metrics,
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web UI polish aggregate browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the rendered message fixture.")
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
