#!/usr/bin/env python3
"""Browser smoke for Web runtime projection over real localhost HTTP/SSE."""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, QuietThreadingHTTPServer, WebAssetHandler


SESSION_ID = "session-real-network-smoke"
STABLE_REQUEST_ID = "req-real-stable"
LOSS_REQUEST_ID = "req-real-loss"
STABLE_TEXT = "# Real Stable Network\n\nCompleted over a real local EventSource connection."
LOSS_TEXT = "# Real Network Recovery\n\nRecovered after real local network loss."


class RealNetworkSmokeState:
    """Thread-safe request ledger for the local HTTP/SSE smoke server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stream_attempts: dict[str, int] = {}
        self.stream_requests: list[dict[str, Any]] = []
        self.runtime_projection_requests: list[dict[str, Any]] = []
        self.history_requests: list[dict[str, Any]] = []
        self.poll_requests: list[dict[str, Any]] = []
        self.api_requests: list[dict[str, Any]] = []

    def record_stream(self, request_id: str, query: dict[str, list[str]], headers: http.client.HTTPMessage) -> int:
        with self._lock:
            attempt = self.stream_attempts.get(request_id, 0) + 1
            self.stream_attempts[request_id] = attempt
            self.stream_requests.append(
                {
                    "request_id": request_id,
                    "attempt": attempt,
                    "last_event_id": (query.get("last_event_id") or [""])[0],
                    "last_event_id_header": headers.get("Last-Event-ID", ""),
                }
            )
            return attempt

    def record(self, bucket: str, value: dict[str, Any]) -> None:
        with self._lock:
            getattr(self, bucket).append(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stream_attempts": dict(self.stream_attempts),
                "stream_requests": list(self.stream_requests),
                "runtime_projection_requests": list(self.runtime_projection_requests),
                "history_requests": list(self.history_requests),
                "poll_requests": list(self.poll_requests),
                "api_requests": list(self.api_requests),
            }


class RealNetworkSmokeHandler(WebAssetHandler):
    """Serve real Web assets plus real localhost JSON and SSE endpoints."""

    state: RealNetworkSmokeState

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _record_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        self.state.record(
            "api_requests",
            {
                "method": method,
                "path": path,
                "query": {key: list(value) for key, value in query.items()},
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._read_json_body()
        self._record_api("POST", path, query)

        if path == "/poll":
            self.state.record("poll_requests", {"body_session_id": str(body.get("session_id") or "")})
            self._send_json({"status": "success", "has_content": False})
            return
        if path == "/config":
            self._send_json({"status": "success", "applied": {}})
            return
        if path.startswith("/api/"):
            self._send_json({"status": "success"})
            return
        self._send_json({"status": "success"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/smoke/state":
            self._send_json({"status": "success", "state": self.state.snapshot()})
            return
        if path == "/stream":
            self._handle_stream(query)
            return

        self._record_api("GET", path, query)

        if path == "/auth/check":
            self._send_json({"auth_required": False, "authenticated": True})
            return
        if path == "/config":
            self._send_json(
                {
                    "status": "success",
                    "title": "EcoreX Real Network Smoke",
                    "phase1_sync_enabled": False,
                }
            )
            return
        if path == "/api/runtime-projection":
            self._handle_runtime_projection(query)
            return
        if path == "/api/history":
            self.state.record(
                "history_requests",
                {
                    "session_id": (query.get("session_id") or [""])[0],
                    "page": (query.get("page") or [""])[0],
                },
            )
            self._send_json({"status": "success", "messages": [], "has_more": False})
            return
        if path == "/api/knowledge/list":
            self._send_json({"status": "success", "tree": [], "root_files": []})
            return
        if path == "/api/version":
            self._send_json({"version": "0.2.2-real-network-smoke"})
            return
        if path == "/api/models":
            self._send_json({"status": "success", "capabilities": {}})
            return
        if path == "/api/tools":
            self._send_json({"status": "success", "tools": []})
            return
        if path == "/api/skills":
            self._send_json({"status": "success", "skills": []})
            return
        if path == "/api/channels":
            self._send_json({"status": "success", "channels": []})
            return
        if path == "/api/scheduler":
            self._send_json(
                {
                    "status": "success",
                    "enabled": True,
                    "initialized": True,
                    "running": False,
                    "serviceStatus": "real_network_smoke",
                    "taskCount": 0,
                    "counts": {"total": 0, "enabled": 0, "disabled": 0, "error": 0},
                    "taskStore": {},
                    "tasks": [],
                }
            )
            return
        if path.startswith("/api/"):
            self._send_json({"status": "success"})
            return

        super().do_GET()

    def _projection_payload(self, request_id: str) -> dict[str, Any]:
        if request_id == STABLE_REQUEST_ID:
            return {
                "request_id": request_id,
                "session_id": SESSION_ID,
                "state": "completed",
                "latest_event_id": 310,
                "event_count": 4,
                "messages": [
                    {"role": "user", "content": "real stable network"},
                    {"role": "assistant", "content": STABLE_TEXT, "pending": False},
                ],
            }
        if request_id == LOSS_REQUEST_ID:
            return {
                "request_id": request_id,
                "session_id": SESSION_ID,
                "state": "completed",
                "latest_event_id": 420,
                "event_count": 6,
                "messages": [
                    {"role": "user", "content": "real local stream loss"},
                    {
                        "role": "assistant",
                        "content": LOSS_TEXT,
                        "pending": False,
                        "tool_calls": [
                            {
                                "id": "tool-real-network-recovery",
                                "name": "runtime_projection",
                                "status": "success",
                                "elapsed_seconds": 0.4,
                            }
                        ],
                    },
                ],
            }
        return {
            "request_id": request_id,
            "session_id": SESSION_ID,
            "state": "running",
            "latest_event_id": 1,
            "event_count": 1,
            "messages": [],
        }

    def _handle_runtime_projection(self, query: dict[str, list[str]]) -> None:
        request_id = (query.get("request_id") or [""])[0]
        session_id = (query.get("session_id") or [SESSION_ID])[0]
        record = {
            "request_id": request_id,
            "session_id": session_id,
            "history_page": (query.get("history_page") or [""])[0],
            "after_event_id": (query.get("after_event_id") or [""])[0],
        }
        self.state.record("runtime_projection_requests", record)

        if request_id:
            self._send_json({"status": "success", "projection": self._projection_payload(request_id)})
            return

        history_page = (query.get("history_page") or [""])[0]
        history = {
            "status": "success",
            "messages": [],
            "page": int(history_page or "1"),
            "page_size": 20,
            "total": 0,
            "has_more": False,
            "context_start_seq": 0,
        }
        self._send_json(
            {
                "status": "success",
                "projection": {
                    "session_id": session_id,
                    "latest_event_id": 0,
                    "event_count": 0,
                    "history_source": "real_network_smoke",
                    "history": history,
                    "requests": [],
                    "messages": [],
                },
            }
        )

    def _write_sse_event(self, event_id: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True)
        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _handle_stream(self, query: dict[str, list[str]]) -> None:
        request_id = (query.get("request_id") or [""])[0]
        attempt = self.state.record_stream(request_id, query, self.headers)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            if request_id == STABLE_REQUEST_ID:
                self._write_sse_event("rn-stable-1", {"type": "delta", "content": "# Real Stable Network\n\n"})
                time.sleep(0.02)
                self._write_sse_event(
                    "rn-stable-2",
                    {"type": "delta", "content": "Completed over a real local EventSource connection."},
                )
                time.sleep(0.02)
                self._write_sse_event("rn-stable-3", {"type": "done", "final_text": STABLE_TEXT})
                return

            if request_id == LOSS_REQUEST_ID and attempt == 1:
                self._write_sse_event("rn-loss-1", {"type": "delta", "content": "# Partial Before Loss\n\n"})
                time.sleep(0.02)
                return

            time.sleep(0.02)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return


@contextlib.contextmanager
def real_network_smoke_server() -> Iterator[tuple[str, RealNetworkSmokeState]]:
    state = RealNetworkSmokeState()
    handler_cls = type("BoundRealNetworkSmokeHandler", (RealNetworkSmokeHandler,), {"state": state})
    handler = functools.partial(handler_cls)
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/chat.html", state
    finally:
        server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()


def _probe_script() -> str:
    return r"""
(async () => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  const wait = (label, predicate, timeout = 10000) => new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        if (predicate()) return resolve();
      } catch (_) {}
      if (Date.now() - start > timeout) {
        return reject(new Error(`timeout waiting for real-network smoke: ${label}`));
      }
      setTimeout(tick, 25);
    };
    tick();
  });

  await wait('runtime functions', () => (
    typeof startSSE === 'function' &&
    typeof loadRequestRuntimeProjection === 'function' &&
    typeof fetchHistoryPage === 'function'
  ));

  startSSE('req-real-stable', null, new Date(), null);
  await wait('stable terminal projection', () => {
    const bot = document.querySelector('[data-request-id="req-real-stable"]');
    return bot && bot.dataset.runtimeProjectionSource === 'sse_terminal';
  });

  const stableBot = document.querySelector('[data-request-id="req-real-stable"]');
  const stableAnswer = stableBot.querySelector('.answer-content');
  assert(document.querySelectorAll('[data-request-id="req-real-stable"]').length === 1, 'stable stream duplicated bot bubble');
  assert(stableBot.dataset.runtimeProjectionEventId === '310', 'stable projection event id missing');
  assert(stableAnswer.dataset.rawMd.includes('real local EventSource'), 'stable answer did not come from projection');
  assert(!!stableBot.querySelector('h1'), 'stable Markdown heading did not render');
  assert(!stableAnswer.innerText.trim().startsWith('#'), 'stable raw # marker leaked');
  assert(document.querySelectorAll('.message-recovery-actions').length === 0, 'stable stream showed recovery actions');

  const nativeSetTimeout = window.setTimeout.bind(window);
  window.setTimeout = (handler, delay, ...args) => nativeSetTimeout(handler, Math.min(Number(delay) || 0, 2), ...args);

  startSSE('req-real-loss', null, new Date(), null);
  await wait('loss projection recovery', () => {
    const bot = document.querySelector('[data-request-id="req-real-loss"]');
    return bot && bot.dataset.runtimeProjectionSource === 'stream_lost';
  });

  const lossBot = document.querySelector('[data-request-id="req-real-loss"]');
  const lossAnswer = lossBot.querySelector('.answer-content');
  assert(document.querySelectorAll('[data-request-id="req-real-loss"]').length === 1, 'loss recovery duplicated bot bubble');
  assert(lossBot.dataset.runtimeProjectionEventId === '420', 'loss projection event id missing');
  assert(lossAnswer.dataset.rawMd.includes('Recovered after real local network loss'), 'loss answer did not come from projection');
  assert(!!lossBot.querySelector('h1'), 'loss Markdown heading did not render');
  assert(!lossAnswer.innerText.trim().startsWith('#'), 'loss raw # marker leaked');
  assert(lossBot.querySelectorAll('.agent-tool-step').length >= 1, 'loss projection tool call did not render');
  assert(!lossBot.innerText.includes('Failed to send'), 'legacy stream loss fallback appeared');
  assert(document.querySelectorAll('.message-recovery-actions').length === 0, 'loss recovery showed recovery actions');

  const serverPayload = await fetch('/api/smoke/state', { cache: 'no-store' }).then((r) => r.json());
  const state = serverPayload.state || {};
  const stableAttempts = Number((state.stream_attempts || {})['req-real-stable'] || 0);
  const lossAttempts = Number((state.stream_attempts || {})['req-real-loss'] || 0);
  const lossRuntimeFetches = (state.runtime_projection_requests || []).filter((item) => item.request_id === 'req-real-loss');
  const stableRuntimeFetches = (state.runtime_projection_requests || []).filter((item) => item.request_id === 'req-real-stable');
  const lossStreamRequests = (state.stream_requests || []).filter((item) => item.request_id === 'req-real-loss');
  const lossLastEventIds = lossStreamRequests.map((item) => item.last_event_id).filter(Boolean);
  const lossAttemptNumbers = lossStreamRequests.map((item) => Number(item.attempt || 0));

  assert(stableAttempts === 1, `stable stream should use one real EventSource connection, got ${stableAttempts}`);
  assert(lossAttempts === 11, `loss stream should exhaust exactly 11 EventSource requests, got ${lossAttempts}`);
  assert(lossStreamRequests.length === 11, `loss stream request ledger should contain exactly 11 entries, got ${lossStreamRequests.length}`);
  assert(lossAttemptNumbers.join(',') === '1,2,3,4,5,6,7,8,9,10,11', `loss stream attempts are not contiguous: ${lossAttemptNumbers.join(',')}`);
  assert(lossStreamRequests[0].last_event_id === '', 'first loss stream request unexpectedly had a cursor');
  assert(lossStreamRequests.slice(1).every((item) => item.last_event_id === 'rn-loss-1'), 'reconnect requests did not all preserve rn-loss-1 cursor');
  assert(stableRuntimeFetches.length >= 1, 'stable terminal projection fetch missing');
  assert(lossRuntimeFetches.length >= 1, 'stream_lost projection fetch missing');
  assert(lossLastEventIds.includes('rn-loss-1'), 'manual reconnect did not preserve last_event_id query cursor');
  assert((state.history_requests || []).length === 0, 'legacy /api/history fallback was used');

  const fetchSource = String(window.fetch || '');
  const eventSourceSource = String(window.EventSource || '');
  assert(!fetchSource.includes('__ecorexSmoke'), 'browser fetch is using smoke stub source');
  assert(!eventSourceSource.includes('SmokeEventSource'), 'browser EventSource is using smoke stub source');

  return {
    stable: {
      source: stableBot.dataset.runtimeProjectionSource,
      eventId: stableBot.dataset.runtimeProjectionEventId,
      heading: stableBot.querySelector('h1')?.textContent || '',
      botCount: document.querySelectorAll('[data-request-id="req-real-stable"]').length
    },
    reconnect: {
      source: lossBot.dataset.runtimeProjectionSource,
      eventId: lossBot.dataset.runtimeProjectionEventId,
      heading: lossBot.querySelector('h1')?.textContent || '',
      botCount: document.querySelectorAll('[data-request-id="req-real-loss"]').length,
      toolSteps: lossBot.querySelectorAll('.agent-tool-step').length,
      lastEventIds: lossLastEventIds
    },
    network: {
      streamAttempts: state.stream_attempts || {},
      streamRequests: state.stream_requests || [],
      runtimeProjectionRequests: state.runtime_projection_requests || [],
      historyRequests: state.history_requests || [],
      pollRequests: state.poll_requests || [],
      apiRequestCount: (state.api_requests || []).length
    },
    browserFunctions: {
      fetchRuntimeWrapper: fetchSource.includes('runtimeFetch'),
      eventSourceRuntimeWrapper: eventSourceSource.includes('RuntimeEventSource')
    },
    visibleText: document.getElementById('chat-messages')?.innerText || ''
  };
})();
"""


def _write_artifact(path_value: str, result: dict[str, Any]) -> str:
    if not path_value:
        return ""
    artifact_path = Path(path_value)
    if not artifact_path.is_absolute():
        artifact_path = ROOT / artifact_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    return str(artifact_path)


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    with real_network_smoke_server() as (url, state):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.add_init_script(f"localStorage.setItem('cow_session_id', {json.dumps(SESSION_ID)});")
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.on(
                "console",
                lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                if msg.type == "error"
                else None,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_function(
                "() => typeof startSSE === 'function' && typeof loadRequestRuntimeProjection === 'function'",
                timeout=args.timeout_ms,
            )
            metrics = page.evaluate(_probe_script())
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
        "server_state": state.snapshot(),
        "console_errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps({**result, "status": "FAIL"}, ensure_ascii=True, indent=2))
    if args.artifact:
        result["artifact"] = _write_artifact(args.artifact, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web runtime projection real-network browser smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the chat projection view.")
    parser.add_argument("--artifact", default="", help="Optional JSON artifact path for network metrics.")
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
