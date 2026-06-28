#!/usr/bin/env python3
"""Browser smoke for Web runtime projection recovery and reconnect behavior."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, base_api_stub_script, web_asset_server


def _projection_stub_script() -> str:
    extra_fetch_cases = r"""
    if (path === '/poll') {
      const state = window.__ecorexSmoke.runtime ||= {};
      state.pollCalls ||= [];
      state.pollCalls.push(Date.now());
      if (!state.pollProjectionReady) {
        return makeResponse({ status: 'success', has_content: false });
      }
      if (!state.pollProjectionDelivered) {
        state.pollProjectionDelivered = true;
        return makeResponse({
          status: 'success',
          has_content: true,
          content: 'legacy poll fallback should not render',
          request_id: 'req-poll-image-job',
          timestamp: Date.now() / 1000
        });
      }
      return makeResponse({ status: 'success', has_content: false });
    }
    if (path === '/api/runtime-projection') {
      const state = window.__ecorexSmoke.runtime ||= {};
      state.requestFetches ||= [];
      state.sessionFetches ||= [];
      state.historyProjectionFetches ||= [];
      state.historyFallbackCalls ||= [];
      const requestId = url.searchParams.get('request_id') || '';
      const sessionId = url.searchParams.get('session_id') || 'session-projection-smoke';
      if (requestId) {
        state.requestFetches.push(requestId);
        if (requestId === 'req-projection-loss') {
          return makeResponse({
            status: 'success',
            projection: {
              request_id: requestId,
              session_id: sessionId,
              state: 'completed',
              latest_event_id: 88,
              event_count: 6,
              messages: [
                { role: 'user', content: 'recover from stream loss' },
                {
                  role: 'assistant',
                  content: '# Projection Recovery\n\nRecovered after stream loss from backend projection.\n\n- durable event\n- projection source of truth',
                  pending: false,
                  tool_calls: [
                    { id: 'tool-loss-1', name: 'runtime_projection', status: 'success', elapsed_seconds: 1.2 }
                  ],
                  artifacts: [
                    { kind: 'file', title: 'projection.txt', path: 'javascript:alert(1)' },
                    { kind: 'file', title: 'projection-secret.txt', path: 'file:///C:/Users/user/private.txt' }
                  ]
                }
              ]
            }
          });
        }
        if (requestId === 'req-non-sse-image-job') {
          return makeResponse({
            status: 'success',
            projection: {
              request_id: requestId,
              session_id: sessionId,
              state: 'completed',
              latest_event_id: 98,
              event_count: 5,
              messages: [
                { role: 'user', content: 'non SSE image job prompt' }
              ],
              image_jobs: [
                {
                  job_id: 'image-job-non-sse-smoke',
                  status: 'completed',
                  operation: 'generate',
                  artifact_count: 1,
                  artifacts: [
                    { kind: 'image', title: 'non-sse-image.png', path: '/assets/icon.png' }
                  ],
                  tasks: [
                    { task_id: 'task-1', status: 'completed', progress: 1 }
                  ]
                }
              ]
            }
          });
        }
        if (requestId === 'req-poll-image-job') {
          return makeResponse({
            status: 'success',
            projection: {
              request_id: requestId,
              session_id: sessionId,
              state: 'completed',
              latest_event_id: 99,
              event_count: 5,
              messages: [
                { role: 'user', content: 'poll image job prompt' }
              ],
              image_jobs: [
                {
                  job_id: 'image-job-poll-smoke',
                  status: 'completed',
                  operation: 'generate',
                  artifact_count: 1,
                  artifacts: [
                    { kind: 'image', title: 'poll-image.png', path: '/assets/icon.png' }
                  ],
                  tasks: [
                    { task_id: 'task-1', status: 'completed', progress: 1 }
                  ]
                }
              ]
            }
          });
        }
        if (requestId === 'req-stable-stream') {
          return makeResponse({
            status: 'success',
            projection: {
              request_id: requestId,
              session_id: sessionId,
              state: 'completed',
              latest_event_id: 78,
              event_count: 4,
              messages: [
                { role: 'user', content: 'stable stream prompt' },
                {
                  role: 'assistant',
                  content: '# Stable Stream\n\nDone without reconnect.',
                  pending: false
                }
              ]
            }
          });
        }
        return makeResponse({
          status: 'success',
          projection: {
            request_id: requestId,
            session_id: sessionId,
            state: 'running',
            latest_event_id: 61,
            messages: [
              { role: 'user', content: 'active history prompt' },
              { role: 'assistant', content: '## Active Projection\n\npartial answer from projection', pending: true }
            ]
          }
        });
      }
      if (url.searchParams.has('history_page')) {
        state.historyProjectionFetches.push(url.search);
        return makeResponse({
          status: 'success',
          projection: {
            session_id: sessionId,
            latest_event_id: 61,
            event_count: 9,
            history_source: 'runtime_projection_browser_smoke',
            history: {
              status: 'success',
              messages: [],
              page: Number(url.searchParams.get('history_page') || '1'),
              page_size: 20,
              total: 0,
              has_more: false,
              context_start_seq: 0
            },
            requests: [
              {
                request_id: 'req-history-projection',
                session_id: sessionId,
                state: 'completed',
                latest_event_id: 51,
                messages: [
                  { role: 'user', content: 'history prompt exists only in runtime projection' },
                  {
                    role: 'assistant',
                    content: '# Projection-Owned History\n\nHard refresh restored this answer from backend projection.',
                    pending: false
                  }
                ]
              },
              {
                request_id: 'req-history-active',
                session_id: sessionId,
                state: 'running',
                latest_event_id: 61,
                messages: [
                  { role: 'user', content: 'running prompt exists only in runtime projection' },
                  {
                    role: 'assistant',
                    content: '## Active History Projection\n\nstill running after reload',
                    pending: true
                  }
                ]
              }
            ]
          }
        });
      }
      if (sessionId) {
        state.sessionFetches.push(url.search);
        return makeResponse({
          status: 'success',
          projection: {
            session_id: sessionId,
            latest_event_id: 0,
            event_count: 0,
            messages: [],
            requests: []
          }
        });
      }
    }
    if (path === '/api/history') {
      (window.__ecorexSmoke.runtime ||= {}).historyFallbackCalls ||= [];
      window.__ecorexSmoke.runtime.historyFallbackCalls.push(url.search);
      return makeResponse({
        status: 'success',
        messages: [
          {
            role: 'assistant',
            content: 'legacy fallback history should not render',
            created_at: Date.now() / 1000,
            request_id: 'req-legacy-fallback'
          }
        ],
        has_more: false
      });
    }
"""
    return (
        "(() => { localStorage.setItem('cow_session_id', 'session-projection-smoke'); })();\n"
        + base_api_stub_script(extra_fetch_cases)
        + r"""
(() => {
  const state = window.__ecorexSmoke.runtime ||= {};
  state.streamUrls ||= [];
  class PassiveEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = {};
      state.streamUrls.push(String(url || ''));
      setTimeout(() => {
        const event = { type: 'open', data: '' };
        (this.listeners.open || []).forEach((handler) => handler(event));
        if (typeof this.onopen === 'function') this.onopen(event);
      }, 0);
    }
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    }
    removeEventListener(type, handler) {
      this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== handler);
    }
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = PassiveEventSource;
})();
"""
    )


def _projection_probe_script() -> str:
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
        return reject(new Error(`timeout waiting for runtime projection smoke: ${label}`));
      }
      setTimeout(tick, 25);
    };
    tick();
  });

  await wait('runtime projection functions', () => (
    typeof startSSE === 'function' &&
    typeof renderRuntimeProjectionRequest === 'function' &&
    typeof fetchHistoryPage === 'function'
  ));

  await wait('history projection bot', () => {
    const bot = document.querySelector('[data-request-id="req-history-projection"]');
    return bot && bot.dataset.runtimeProjectionSource === 'history_projection';
  });
  await wait('active projection reattached stream', () => (
    (window.__ecorexSmoke.runtime.streamUrls || []).some((url) => url.includes('req-history-active'))
  ));

  const historyBot = document.querySelector('[data-request-id="req-history-projection"]');
  const activeBot = document.querySelector('[data-request-id="req-history-active"]');
  const historyAnswer = historyBot.querySelector('.answer-content');
  const activeAnswer = activeBot.querySelector('.answer-content');
  const initialBodyText = document.body.innerText;

  assert(!document.getElementById('welcome-screen'), 'welcome screen stayed visible after projection-only history load');
  assert((window.__ecorexSmoke.runtime.historyProjectionFetches || []).length >= 1, 'history projection endpoint was not called');
  assert((window.__ecorexSmoke.runtime.historyFallbackCalls || []).length === 0, 'legacy /api/history fallback was used');
  assert(historyBot.dataset.runtimeProjectionEventId === '51', 'history projection event cursor missing');
  assert(historyBot.dataset.runtimeProjectionState === 'completed', 'history completed state missing');
  assert(initialBodyText.includes('history prompt exists only in runtime projection'), 'history projection user prompt missing');
  assert(initialBodyText.includes('running prompt exists only in runtime projection'), 'active projection user prompt missing');
  assert(historyAnswer.dataset.rawMd.includes('Projection-Owned History'), 'history raw Markdown did not come from projection');
  assert(!!historyBot.querySelector('h1'), 'history projection heading did not render as Markdown');
  assert(!historyAnswer.innerText.trim().startsWith('#'), 'history projection leaked raw # marker');
  assert(activeBot.dataset.runtimeProjectionState === 'running', 'active projection running state missing');
  assert(activeAnswer.classList.contains('sse-streaming'), 'active projection did not remain in streaming preview state');
  assert(activeAnswer.dataset.rawMd.includes('Active History Projection'), 'active raw Markdown did not come from projection');
  assert(!initialBodyText.includes('legacy fallback history should not render'), 'legacy fallback history appeared in UI');

  const stableState = window.__ecorexSmoke.runtime;
  stableState.stableStreamUrls = [];
  class StableEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      stableState.stableStreamUrls.push(String(url || ''));
      setTimeout(() => {
        if (typeof this.onmessage === 'function') {
          this.onmessage({
            data: JSON.stringify({
              type: 'tool_start',
              tool: '<img src=x onerror=alert(1)>',
              tool_call_id: 'stable-hostile-tool',
              arguments: { safe: true }
            }),
            lastEventId: 'stable-tool-1'
          });
          this.onmessage({
            data: JSON.stringify({
              type: 'tool_end',
              tool: '<img src=x onerror=alert(1)>',
              tool_call_id: 'stable-hostile-tool',
              status: 'success',
              execution_time: '1</span><img src=x onerror=alert(2)>'
            }),
            lastEventId: 'stable-tool-2'
          });
          this.onmessage({
            data: JSON.stringify({ type: 'delta', content: '# Stable Stream\n\nDone without reconnect.' }),
            lastEventId: 'stable-1'
          });
          this.onmessage({
            data: JSON.stringify({ type: 'done', final_text: '# Stable Stream\n\nDone without reconnect.' }),
            lastEventId: 'stable-2'
          });
        }
        if (typeof this.onerror === 'function') this.onerror({ type: 'error', data: '' });
      }, 0);
    }
    addEventListener() {}
    removeEventListener() {}
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = StableEventSource;
  startSSE('req-stable-stream', null, new Date(), null);
  await wait('stable stream terminal projection refresh', () => {
    const bot = document.querySelector('[data-request-id="req-stable-stream"]');
    return bot && bot.dataset.runtimeProjectionSource === 'sse_terminal';
  });
  const stableBots = Array.from(document.querySelectorAll('[data-request-id="req-stable-stream"]'));
  const stableBot = stableBots[0];
  const stableAnswer = stableBot.querySelector('.answer-content');
  assert(stableBots.length === 1, 'stable stream created duplicate bot bubbles');
  assert(stableState.stableStreamUrls.length === 1, 'stable stream unexpectedly reconnected');
  assert(stableBot.dataset.runtimeProjectionSource === 'sse_terminal', 'stable stream did not settle through terminal projection refresh');
  assert(stableBot.dataset.runtimeProjectionEventId === '78', 'stable stream projection event cursor missing');
  assert(stableAnswer.dataset.rawMd.includes('Done without reconnect'), 'stable stream final answer missing');
  assert(!!stableBot.querySelector('h1'), 'stable stream final Markdown heading missing');
  assert(!stableAnswer.innerText.trim().startsWith('#'), 'stable stream leaked raw # marker');
  assert(!stableBot.innerText.includes('Failed to send'), 'stable stream showed failure fallback');
  assert(document.querySelectorAll('.message-recovery-actions').length === 0, 'stable stream showed recovery actions');
  assert(stableBot.querySelectorAll('img[src="x"]').length === 0, 'stable hostile tool HTML created an image');
  assert(stableBot.querySelectorAll('[onerror]').length === 0, 'stable hostile tool HTML created an event handler');
  assert(stableBot.querySelector('.tool-name')?.innerText.includes('<img src=x'), 'stable hostile tool name was not preserved as text');

  const nativeSetTimeout = window.setTimeout.bind(window);
  window.setTimeout = (handler, delay, ...args) => nativeSetTimeout(handler, Math.min(Number(delay) || 0, 2), ...args);
  const lostState = window.__ecorexSmoke.runtime;
  lostState.lostStreamUrls = [];
  class LostEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      lostState.lostStreamUrls.push(String(url || ''));
      nativeSetTimeout(() => {
        const event = { type: 'error', data: '' };
        if (typeof this.onerror === 'function') this.onerror(event);
      }, 0);
    }
    addEventListener() {}
    removeEventListener() {}
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = LostEventSource;

  startSSE('req-projection-loss', null, new Date(), null);
  await wait('stream-loss request projection fetch', () => (
    (lostState.requestFetches || []).filter((rid) => rid === 'req-projection-loss').length >= 1
  ));
  await wait('stream-loss projection rendered', () => {
    const bot = document.querySelector('[data-request-id="req-projection-loss"]');
    return bot && bot.dataset.runtimeProjectionSource === 'stream_lost';
  });

  const lossBots = Array.from(document.querySelectorAll('[data-request-id="req-projection-loss"]'));
  const lossBot = lossBots[0];
  const lossAnswer = lossBot.querySelector('.answer-content');
  const lossText = lossAnswer.innerText;
  assert(lossBots.length === 1, 'stream-loss projection created duplicate bot bubbles');
  assert((lostState.lostStreamUrls || []).length >= 11, 'SSE retry exhaustion was not exercised');
  assert(lossBot.dataset.runtimeProjectionEventId === '88', 'stream-loss projection event cursor missing');
  assert(lossBot.dataset.runtimeProjectionSource === 'stream_lost', 'stream-loss source marker missing');
  assert(lossAnswer.dataset.rawMd.includes('Recovered after stream loss'), 'stream-loss answer did not come from projection');
  assert(!!lossBot.querySelector('h1'), 'stream-loss projection heading did not render as Markdown');
  assert(!lossText.trim().startsWith('#'), 'stream-loss projection leaked raw # marker');
  assert(lossBot.querySelectorAll('.agent-tool-step').length >= 1, 'projection tool call did not render');
  assert(lossBot.querySelectorAll('.media-content > *').length >= 1, 'projection artifact did not render');
  const disabledUnsafeArtifacts = Array.from(lossBot.querySelectorAll('.artifact-card-disabled'));
  const disabledUnsafeText = disabledUnsafeArtifacts.map((el) => el.textContent || '').join('\\n');
  assert(disabledUnsafeArtifacts.length === 2, 'unsafe projection artifacts were not retained as disabled cards');
  assert(disabledUnsafeText.includes('projection.txt') && disabledUnsafeText.includes('projection-secret.txt'), 'disabled unsafe artifact titles missing');
  assert(disabledUnsafeArtifacts.every((el) => {
    const rawUrl = (el.dataset && el.dataset.artifactUrl) || '';
    return !rawUrl && !el.querySelector('a[href], button, .artifact-actions');
  }), 'disabled unsafe artifacts exposed links or actions');
  assert(Array.from(lossBot.querySelectorAll('a[href]')).every((link) => {
    const href = link.getAttribute('href') || '';
    return !/^javascript:/i.test(href) && !/^file:/i.test(href) && !href.includes('/api/file?path=');
  }), 'unsafe projection artifact href survived');
  assert(!lossText.includes('legacy fallback'), 'legacy stream-loss fallback text appeared in projection answer');

  addBotMessage('stale poll placeholder should update', new Date(), 'req-poll-image-job');
  const stalePollBots = Array.from(document.querySelectorAll('[data-request-id="req-poll-image-job"]'));
  assert(stalePollBots.length === 1, 'poll stale setup did not create exactly one bot bubble');
  assert((stalePollBots[0].innerText || '').includes('stale poll placeholder should update'), 'poll stale setup text missing');
  lostState.pollProjectionReady = true;
  startPolling();
  await wait('poll projection image job rendered', () => {
    const bot = document.querySelector('[data-request-id="req-poll-image-job"]');
    return bot && bot.dataset.runtimeProjectionSource === 'poll_projection';
  });
  const pollBots = Array.from(document.querySelectorAll('[data-request-id="req-poll-image-job"]'));
  const pollBot = pollBots[0];
  const pollText = pollBot.innerText || '';
  const pollArtifact = pollBot.querySelector('.artifact-card[data-artifact-kind="image"]');
  assert(pollBots.length === 1, 'poll projection created duplicate bot bubbles');
  assert(pollBot.dataset.runtimeProjectionEventId === '99', 'poll projection event cursor missing');
  assert(pollText.includes('Image generation completed'), 'poll image job summary missing');
  assert(!pollText.includes('stale poll placeholder should update'), 'poll stale bubble was not updated from projection');
  assert(pollArtifact && (pollArtifact.dataset.artifactUrl || '').includes('/assets/icon.png'), 'poll image job artifact DOM missing');
  assert(!!pollArtifact.querySelector('img[src*="/assets/icon.png"]'), 'poll image job preview missing');
  assert(!document.body.innerText.includes('legacy poll fallback should not render'), 'poll legacy text rendered instead of projection');

  window.EventSource = undefined;
  startSSE('req-non-sse-image-job', null, new Date(), null);
  await wait('non-SSE image job projection rendered', () => {
    const bot = document.querySelector('[data-request-id="req-non-sse-image-job"]');
    return bot && bot.dataset.runtimeProjectionSource === 'non_sse_projection';
  });
  const nonSseBots = Array.from(document.querySelectorAll('[data-request-id="req-non-sse-image-job"]'));
  const nonSseBot = nonSseBots[0];
  const nonSseText = nonSseBot.innerText || '';
  const nonSseArtifact = nonSseBot.querySelector('.artifact-card[data-artifact-kind="image"]');
  assert(nonSseBots.length === 1, 'non-SSE projection created duplicate bot bubbles');
  assert(nonSseBot.dataset.runtimeProjectionEventId === '98', 'non-SSE projection event cursor missing');
  assert(nonSseText.includes('Image generation completed'), 'non-SSE image job summary missing');
  assert(nonSseArtifact && (nonSseArtifact.dataset.artifactUrl || '').includes('/assets/icon.png'), 'non-SSE image job artifact DOM missing');
  assert(!!nonSseArtifact.querySelector('img[src*="/assets/icon.png"]'), 'non-SSE image job preview missing');
  assert((lostState.requestFetches || []).filter((rid) => rid === 'req-non-sse-image-job').length >= 1, 'non-SSE projection endpoint was not polled');
  assert(document.querySelectorAll('.message-recovery-actions').length === 0, 'non-SSE fallback showed recovery actions');

  return {
    history: {
      source: historyBot.dataset.runtimeProjectionSource,
      state: historyBot.dataset.runtimeProjectionState,
      eventId: historyBot.dataset.runtimeProjectionEventId,
      heading: historyBot.querySelector('h1')?.textContent || '',
      activeState: activeBot.dataset.runtimeProjectionState,
      activeStreaming: activeAnswer.classList.contains('sse-streaming')
    },
    stable: {
      source: stableBot.dataset.runtimeProjectionSource,
      eventId: stableBot.dataset.runtimeProjectionEventId,
      streamUrls: stableState.stableStreamUrls,
      botCount: stableBots.length,
      heading: stableBot.querySelector('h1')?.textContent || '',
      recoveryActions: document.querySelectorAll('.message-recovery-actions').length
    },
    reconnect: {
      source: lossBot.dataset.runtimeProjectionSource,
      eventId: lossBot.dataset.runtimeProjectionEventId,
      retryCount: lostState.lostStreamUrls.length,
      requestFetches: lostState.requestFetches.filter((rid) => rid === 'req-projection-loss').length,
      botCount: lossBots.length,
      heading: lossBot.querySelector('h1')?.textContent || '',
      toolSteps: lossBot.querySelectorAll('.agent-tool-step').length,
      mediaItems: lossBot.querySelectorAll('.media-content > *').length
    },
    pollProjection: {
      source: pollBot.dataset.runtimeProjectionSource,
      eventId: pollBot.dataset.runtimeProjectionEventId,
      botCount: pollBots.length,
      mediaItems: pollBot.querySelectorAll('.media-content > *').length
    },
    nonSse: {
      source: nonSseBot.dataset.runtimeProjectionSource,
      eventId: nonSseBot.dataset.runtimeProjectionEventId,
      botCount: nonSseBots.length,
      mediaItems: nonSseBot.querySelectorAll('.media-content > *').length,
      requestFetches: (lostState.requestFetches || []).filter((rid) => rid === 'req-non-sse-image-job').length
    },
    fetches: {
      historyProjectionFetches: lostState.historyProjectionFetches || [],
      sessionFetches: lostState.sessionFetches || [],
      historyFallbackCalls: lostState.historyFallbackCalls || [],
      pollCalls: lostState.pollCalls || [],
      streamUrls: lostState.streamUrls || [],
      lostStreamUrls: lostState.lostStreamUrls || []
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
            page.add_init_script(_projection_stub_script())
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof startSSE === 'function' && typeof fetchHistoryPage === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_projection_probe_script())
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
    parser = argparse.ArgumentParser(description="Run Web runtime projection reconnect browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the chat projection view.")
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
