#!/usr/bin/env python3
"""Browser smoke for projection-owned Web history pagination and cursors."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _projection_history_stub_script() -> str:
    extra_fetch_cases = r"""
    if (path === '/api/runtime-projection') {
      const state = window.__ecorexSmoke.historyPagination ||= {};
      state.historyProjectionFetches ||= [];
      state.historyFallbackCalls ||= [];
      state.sessionFetches ||= [];
      const requestId = url.searchParams.get('request_id') || '';
      const sessionId = url.searchParams.get('session_id') || 'session-history-pagination-smoke';
      if (requestId) {
        return makeResponse({
          status: 'success',
          projection: {
            request_id: requestId,
            session_id: sessionId,
            state: 'completed',
            latest_event_id: 210,
            messages: [
              { role: 'user', content: `${requestId} prompt` },
              { role: 'assistant', content: `# ${requestId} Projection\n\nRequest projection recovered.`, pending: false }
            ]
          }
        });
      }
      if (url.searchParams.has('history_page')) {
        const page = Number(url.searchParams.get('history_page') || '1');
        state.historyProjectionFetches.push(url.search);
        if (page === 3) {
          return makeResponse({ status: 'error', message: 'transient runtime projection history unavailable' }, 503);
        }
        const latestEventId = page === 1 ? 200 : 205;
        const requestIdForPage = page === 1 ? 'req-history-page1' : 'req-history-page2';
        const prompt = page === 1
          ? 'page one prompt from history message'
          : 'page two older prompt from history message';
        const staleAnswer = page === 1
          ? '# Page One History Stale\n\nThis stale history answer must be updated from runtime projection.'
          : '# Page Two History Stale\n\nThis stale older answer must be updated from runtime projection.';
        const projectedAnswer = page === 1
          ? '# Page One Projection\n\nRuntime projection owns page one after hard refresh.'
          : '# Page Two Projection\n\nRuntime projection owns older history pagination.';
        return makeResponse({
          status: 'success',
          projection: {
            session_id: sessionId,
            latest_event_id: latestEventId,
            event_count: page === 1 ? 8 : 4,
            history_source: 'runtime_projection_history_pagination_browser_smoke',
            history: {
              status: 'success',
              messages: [
                {
                  role: 'user',
                  content: prompt,
                  created_at: page === 1 ? 2000 : 1000,
                  request_id: requestIdForPage,
                  turn_id: requestIdForPage
                },
                {
                  role: 'assistant',
                  content: staleAnswer,
                  created_at: page === 1 ? 2001 : 1001,
                  request_id: requestIdForPage,
                  turn_id: requestIdForPage
                }
              ],
              page,
              page_size: 20,
              total: 40,
              has_more: page === 1,
              context_start_seq: 0
            },
            requests: [
              {
                request_id: requestIdForPage,
                session_id: sessionId,
                state: 'completed',
                latest_event_id: latestEventId,
                created_at: page === 1 ? 2001 : 1001,
                messages: [
                  { role: 'user', content: prompt, created_at: page === 1 ? 2000 : 1000 },
                  { role: 'assistant', content: projectedAnswer, pending: false, created_at: page === 1 ? 2001 : 1001 }
                ]
              }
            ]
          }
        });
      }
      if (sessionId) {
        const afterEventId = Number(url.searchParams.get('after_event_id') || '0');
        state.sessionFetches.push(url.search);
        if (afterEventId >= 210) {
          return makeResponse({
            status: 'success',
            projection: {
              session_id: sessionId,
              after_event_id: afterEventId,
              latest_event_id: afterEventId,
              event_count: 0,
              messages: [],
              requests: []
            }
          });
        }
        if (afterEventId >= 200) {
          return makeResponse({
            status: 'success',
            projection: {
              session_id: sessionId,
              after_event_id: afterEventId,
              latest_event_id: 210,
              event_count: 2,
              messages: [],
              requests: [
                {
                  request_id: 'req-new-after-cursor',
                  session_id: sessionId,
                  state: 'completed',
                  latest_event_id: 210,
                  created_at: 2100,
                  messages: [
                    { role: 'user', content: 'new prompt after cursor', created_at: 2099 },
                    { role: 'assistant', content: '# Cursor Delta\n\nOnly the post-cursor request should render.', pending: false, created_at: 2100 }
                  ]
                }
              ]
            }
          });
        }
        return makeResponse({
          status: 'success',
          projection: {
            session_id: sessionId,
            after_event_id: afterEventId,
            latest_event_id: 200,
            event_count: 2,
            messages: [],
            requests: [
              {
                request_id: 'req-should-not-full-replay',
                session_id: sessionId,
                state: 'completed',
                latest_event_id: 199,
                messages: [
                  { role: 'user', content: 'old full replay prompt' },
                  { role: 'assistant', content: '# Old Full Replay\n\nThis should not render after history cursor advances.', pending: false }
                ]
              }
            ]
          }
        });
      }
    }
    if (path === '/api/history') {
      const state = window.__ecorexSmoke.historyPagination ||= {};
      state.historyFallbackCalls ||= [];
      state.historyFallbackCalls.push(url.search);
      return makeResponse({
        status: 'success',
        messages: [
          {
            role: 'assistant',
            content: 'weak-network fallback history response',
            created_at: Date.now() / 1000,
            request_id: 'req-weak-network-fallback'
          }
        ],
        page: Number(url.searchParams.get('page') || '1'),
        page_size: 20,
        has_more: false
      });
    }
"""
    return (
        "(() => { localStorage.setItem('cow_session_id', 'session-history-pagination-smoke'); })();\n"
        + base_api_stub_script(extra_fetch_cases)
    )


def _projection_history_probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  const wait = (label, predicate, timeout = 8000) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - start > timeout) {
        return reject(new Error(`timeout waiting for history pagination smoke: ${label}`));
      }
      setTimeout(tick, 25);
    };
    tick();
  });

  const state = window.__ecorexSmoke.historyPagination ||= {};
  const paramsFor = (search) => new URLSearchParams(String(search || '').replace(/^\?/, ''));
  const countText = (needle) => {
    const text = document.getElementById('chat-messages')?.innerText || '';
    return (text.match(new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length;
  };

  await wait('runtime projection history functions', () => (
    typeof fetchHistoryPage === 'function' &&
    typeof loadHistory === 'function' &&
    typeof refreshSessionRuntimeProjection === 'function'
  ));

  await wait('page one rendered from projection', () => {
    const bot = document.querySelector('[data-request-id="req-history-page1"]');
    return bot && bot.dataset.runtimeProjectionSource === 'history_projection';
  });
  await wait('page one cursor recheck used after_event_id=200', () => (
    (state.sessionFetches || []).some((search) => paramsFor(search).get('after_event_id') === '200')
  ));
  await wait('post-cursor request rendered', () => (
    !!document.querySelector('[data-request-id="req-new-after-cursor"]')
  ));

  const pageOneBot = document.querySelector('[data-request-id="req-history-page1"]');
  const pageOneAnswer = pageOneBot.querySelector('.answer-content');
  assert((state.historyProjectionFetches || []).some((search) => paramsFor(search).get('history_page') === '1'), 'history page 1 did not use runtime projection');
  assert((state.historyFallbackCalls || []).length === 0, 'legacy history fallback was used during primary projection page 1');
  assert(document.querySelectorAll('[data-request-id="req-history-page1"]').length === 1, 'page 1 request duplicated between history messages and runtime requests');
  assert(countText('page one prompt from history message') === 1, 'page 1 user prompt duplicated');
  assert(pageOneAnswer.dataset.rawMd.includes('Page One Projection'), 'page 1 assistant was not updated from runtime projection');
  assert(!pageOneAnswer.dataset.rawMd.includes('Page One History Stale'), 'page 1 stale history answer survived projection update');
  assert(!!pageOneBot.querySelector('h1'), 'page 1 projected Markdown heading did not render');
  assert(!pageOneAnswer.innerText.trim().startsWith('#'), 'page 1 projected Markdown leaked raw heading marker');
  assert(!document.body.innerText.includes('Old Full Replay'), 'session recheck replayed events before the history cursor');

  await wait('load more sentinel', () => !!document.querySelector('#history-load-more button'));
  document.querySelector('#history-load-more button').click();
  await wait('page two rendered from projection', () => {
    const bot = document.querySelector('[data-request-id="req-history-page2"]');
    return bot && bot.dataset.runtimeProjectionSource === 'history_projection';
  });

  const pageTwoBot = document.querySelector('[data-request-id="req-history-page2"]');
  const pageTwoAnswer = pageTwoBot.querySelector('.answer-content');
  assert((state.historyProjectionFetches || []).some((search) => paramsFor(search).get('history_page') === '2'), 'history page 2 did not use runtime projection');
  assert((state.historyFallbackCalls || []).length === 0, 'legacy history fallback was used during primary projection page 2');
  assert(document.querySelectorAll('[data-request-id="req-history-page2"]').length === 1, 'page 2 request duplicated between history messages and runtime requests');
  assert(countText('page two older prompt from history message') === 1, 'page 2 user prompt duplicated');
  assert(pageTwoAnswer.dataset.rawMd.includes('Page Two Projection'), 'page 2 assistant was not updated from runtime projection');
  assert(!pageTwoAnswer.dataset.rawMd.includes('Page Two History Stale'), 'page 2 stale history answer survived projection update');
  assert(!document.getElementById('history-load-more'), 'load more sentinel remained after projection page 2 reported has_more=false');

  await refreshSessionRuntimeProjection('manual_cursor_recheck', {
    sessionId: 'session-history-pagination-smoke'
  });
  assert((state.sessionFetches || []).some((search) => paramsFor(search).get('after_event_id') === '210'), 'manual recheck did not reuse advanced event cursor 210');

  const fallbackPage = await fetchHistoryPage('session-history-pagination-smoke', 3);
  assert(fallbackPage.status === 'success', 'weak-network fallback page did not return success');
  assert((fallbackPage.messages || []).some((item) => item.content === 'weak-network fallback history response'), 'weak-network fallback payload missing');
  assert((state.historyFallbackCalls || []).length === 1, 'weak-network fallback was not isolated to the forced projection failure');

  const pageOneBots = document.querySelectorAll('[data-request-id="req-history-page1"]').length;
  const pageTwoBots = document.querySelectorAll('[data-request-id="req-history-page2"]').length;
  const deltaBots = document.querySelectorAll('[data-request-id="req-new-after-cursor"]').length;
  assert(pageOneBots === 1 && pageTwoBots === 1 && deltaBots === 1, 'projection pagination produced duplicate bot bubbles');

  return {
    pageOne: {
      source: pageOneBot.dataset.runtimeProjectionSource,
      eventId: pageOneBot.dataset.runtimeProjectionEventId,
      botCount: pageOneBots,
      promptCount: countText('page one prompt from history message'),
      heading: pageOneBot.querySelector('h1')?.textContent || ''
    },
    pageTwo: {
      source: pageTwoBot.dataset.runtimeProjectionSource,
      eventId: pageTwoBot.dataset.runtimeProjectionEventId,
      botCount: pageTwoBots,
      promptCount: countText('page two older prompt from history message'),
      heading: pageTwoBot.querySelector('h1')?.textContent || ''
    },
    cursorDelta: {
      botCount: deltaBots,
      text: document.querySelector('[data-request-id="req-new-after-cursor"]')?.innerText || ''
    },
    fetches: {
      historyProjectionFetches: state.historyProjectionFetches || [],
      sessionFetches: state.sessionFetches || [],
      historyFallbackCalls: state.historyFallbackCalls || []
    },
    visibleText: document.getElementById('chat-messages')?.innerText || ''
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
            page.add_init_script(_projection_history_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof fetchHistoryPage === 'function' && typeof loadHistory === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_projection_history_probe_script())
            screenshot_path = ""
            if args.screenshot:
                screenshot_target = Path(args.screenshot)
                if not screenshot_target.is_absolute():
                    screenshot_target = ROOT / screenshot_target
                screenshot_target.parent.mkdir(parents=True, exist_ok=True)
                page.locator("#chat-messages").screenshot(path=str(screenshot_target))
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
    parser = argparse.ArgumentParser(description="Run Web runtime projection history pagination browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the chat projection history view.")
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
