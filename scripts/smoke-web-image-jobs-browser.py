#!/usr/bin/env python3
"""Browser/API smoke for backend-led Web image jobs."""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import sync_playwright

from web_smoke_support import ROOT, WebAssetHandler, QuietThreadingHTTPServer, base_api_stub_script


sys.path.insert(0, str(ROOT))


def _ensure_web_stub() -> None:
    if "web" in sys.modules:
        return
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


_ensure_web_stub()


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfeA\xe2\x1d\x9d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ImageJobSmokeHandler(WebAssetHandler):
    """Serve Web assets and dispatch image-job API calls to WebChannel handlers."""

    workspace_dir: Path = ROOT
    output_dir: Path = ROOT
    event_db_path: Path = ROOT / "runtime-events.db"
    api_calls: list[dict[str, Any]] = []
    ledger: Any = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/image-jobs":
            self._handle_image_jobs_get(parsed.query)
            return
        if parsed.path == "/__smoke/events":
            self._handle_events_get(parsed.query)
            return
        if parsed.path == "/api/file":
            self._send_bytes(PNG_1X1, content_type="image/png")
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/image-jobs":
            self._handle_image_jobs_post()
            return
        if parsed.path.startswith("/api/image-jobs/"):
            job_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self._handle_image_job_action_post(job_id)
            return
        if parsed.path == "/__smoke/reset-image-service":
            from agent.protocol import reset_image_job_service_for_tests

            reset_image_job_service_for_tests(self.ledger)
            self._send_json({"status": "success", "reset": True})
            return
        self._send_json({"status": "error", "message": "not found"}, status=404)

    def _handle_image_jobs_get(self, query: str) -> None:
        params = _query_namespace(query, {
            "job_id": "",
            "request_id": "",
            "requestId": "",
            "wait": "",
            "timeout": "",
            "include_events": "",
        })
        self._dispatch_json("GET", "/api/image-jobs", params=params)

    def _handle_image_jobs_post(self) -> None:
        self._dispatch_json("POST", "/api/image-jobs", body=self._read_body())

    def _handle_image_job_action_post(self, job_id: str) -> None:
        self._dispatch_json("POST", f"/api/image-jobs/{job_id}", body=self._read_body(), job_id=job_id)

    def _dispatch_json(
        self,
        method: str,
        path: str,
        *,
        params: types.SimpleNamespace | None = None,
        body: bytes = b"{}",
        job_id: str = "",
    ) -> None:
        from channel.web import web_channel

        call: dict[str, Any] = {"method": method, "path": path}
        try:
            body_payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except Exception:
            body_payload = {}
        call["action"] = body_payload.get("action") if isinstance(body_payload, dict) else ""
        call["body_keys"] = sorted(body_payload.keys()) if isinstance(body_payload, dict) else []
        try:
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "header", return_value=None), \
                patch.object(web_channel.web, "data", return_value=body), \
                patch.object(web_channel.web, "input", side_effect=_input_factory(params)), \
                patch.object(web_channel, "_image_job_output_dir", return_value=str(self.output_dir)):
                if path == "/api/image-jobs" and method == "GET":
                    raw = web_channel.ImageJobsHandler().GET()
                elif path == "/api/image-jobs" and method == "POST":
                    raw = web_channel.ImageJobsHandler().POST()
                else:
                    raw = web_channel.ImageJobActionHandler().POST(job_id)
            payload = json.loads(raw or "{}")
            call["status"] = payload.get("status")
            call["job_status"] = (payload.get("job") or {}).get("status")
            self.api_calls.append(call)
            self._send_json(payload)
        except Exception as exc:
            call["status"] = "exception"
            call["error"] = str(exc)
            self.api_calls.append(call)
            self._send_json({"status": "error", "message": str(exc)}, status=500)

    def _handle_events_get(self, query: str) -> None:
        params = parse_qs(query)
        request_id = (params.get("request_id") or [""])[0]
        events = []
        if request_id:
            events = self.ledger.events_for_request(request_id, limit=0)
        self._send_json({"status": "success", "events": events, "api_calls": self.api_calls})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b"{}"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, payload: bytes, *, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _query_namespace(query: str, defaults: dict[str, str]) -> types.SimpleNamespace:
    parsed = parse_qs(query)
    values = dict(defaults)
    for key in defaults:
        if key in parsed:
            values[key] = (parsed.get(key) or [""])[0]
    return types.SimpleNamespace(**values)


def _input_factory(params: types.SimpleNamespace | None):
    def _fake_input(**defaults: Any) -> types.SimpleNamespace:
        values = dict(defaults)
        if params:
            values.update(vars(params))
        return types.SimpleNamespace(**values)

    return _fake_input


@contextlib.contextmanager
def image_job_smoke_server(workspace_dir: Path):
    from agent.protocol import (
        reset_image_job_service_for_tests,
        reset_run_event_ledger_for_tests,
        reset_run_ledger_for_tests,
    )

    workspace_dir.mkdir(parents=True, exist_ok=True)
    output_dir = workspace_dir / "image-jobs"
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = workspace_dir / "runtime-events.db"
    ledger = reset_run_event_ledger_for_tests(db_path)
    reset_run_ledger_for_tests(db_path)
    reset_image_job_service_for_tests(ledger)

    handler_cls = type(
        "BoundImageJobSmokeHandler",
        (ImageJobSmokeHandler,),
        {
            "workspace_dir": workspace_dir,
            "output_dir": output_dir,
            "event_db_path": db_path,
            "api_calls": [],
            "ledger": ledger,
        },
    )
    handler = functools.partial(handler_cls)
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/chat.html", handler_cls
    finally:
        server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()


def _image_job_stub_script() -> str:
    extra_fetch_cases = r"""
    if (path === '/api/image-jobs' || path.startsWith('/api/image-jobs/') || path.startsWith('/__smoke/')) {
      return window.__ecorexNativeFetch(input, init);
    }
"""
    return (
        "(() => { window.__ecorexNativeFetch = window.fetch.bind(window); "
        "localStorage.setItem('cow_session_id', 'session-image-job-browser-smoke'); })();\n"
        + base_api_stub_script(extra_fetch_cases)
    )


def _image_job_probe_script() -> str:
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
        return reject(new Error(`timeout waiting for image job smoke: ${label}`));
      }
      setTimeout(tick, 25);
    };
    tick();
  });

  await wait('runtime projection renderer', () => (
    typeof renderRuntimeProjectionRequest === 'function' &&
    typeof runtimeProjectionImageJobSummary === 'function'
  ));
  const uploadRuntimeUrl = _toWebUrl('/uploads/voice.mp3');
  const localPosixRuntimeUrl = _toWebUrl('/tmp/image.png');
  assert(uploadRuntimeUrl === '/uploads/voice.mp3', 'runtime /uploads path was incorrectly routed through file API');
  assert(!uploadRuntimeUrl.includes('/api/file?path='), 'runtime /uploads path should not use backend file API');
  assert(localPosixRuntimeUrl.includes('/api/file?path='), 'local POSIX artifact path did not use backend file API');

  const startPayload = {
    action: 'start',
    dry_run: true,
    synchronous: true,
    include_events: true,
    request_id: 'req-privateprompt',
    session_id: 'session-image-job-browser',
    turn_id: 'turn-privateprompt',
    job_id: 'image-job-privateprompt',
    prompt: 'draw a browser smoke image from backend projection',
    count: 2,
    max_parallel: 2,
    provider: 'privateprompt',
    model: 'privateprompt'
  };
  const start = await fetch('/api/image-jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(startPayload)
  }).then((response) => response.json());
  const jobId = start.job && start.job.job_id;
  const requestId = start.job && start.job.request_id;

  assert(start.status === 'success', 'image job start failed');
  assert(start.job.status === 'completed', 'synchronous dry-run image job did not complete');
  assert(/^image-job-/.test(jobId), 'job id was not sanitized');
  assert(/^req-image-job-/.test(requestId), 'request id was not sanitized');
  assert((start.projection.image_jobs || []).length === 1, 'image job projection missing');
  assert(start.projection.image_jobs[0].artifacts.length === 2, 'incremental image artifacts missing from projection');
  const projectedImageJob = start.projection.image_jobs[0];
  assert(projectedImageJob.parallelism_policy_version === 'v1', 'image job parallelism policy version missing from projection');
  assert(projectedImageJob.requested_max_parallel === 2, 'requested image parallelism missing from projection');
  assert(projectedImageJob.hard_max_parallel === 8, 'hard image parallelism cap missing from projection');
  assert(projectedImageJob.effective_max_parallel === 2, 'effective image parallelism missing from projection');
  assert(projectedImageJob.parallelism_clamped === false, 'unclamped image job was marked clamped');
  assert(projectedImageJob.parallelism_clamp_reason === 'none', 'unclamped image job clamp reason should be none');
  assert(projectedImageJob.max_parallel === 2, 'started image max_parallel did not match effective policy');
  const startedProjectionEvent = (start.projection.events || []).find((event) => event.event_type === 'image_job.started');
  assert(startedProjectionEvent && startedProjectionEvent.payload.effective_max_parallel === 2, 'started event missing effective image parallelism');

  const status = await fetch(`/api/image-jobs?job_id=${encodeURIComponent(jobId)}&include_events=1`).then((response) => response.json());
  const collect = await fetch(`/api/image-jobs/${encodeURIComponent(jobId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'collect', wait: true, timeout: 1, include_events: true })
  }).then((response) => response.json());
  const cancelCompleted = await fetch(`/api/image-jobs/${encodeURIComponent(jobId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'cancel', include_events: true })
  }).then((response) => response.json());

  assert(status.job.status === 'completed', 'status endpoint did not return completed job');
  assert(collect.job.status === 'completed', 'collect endpoint did not return completed job');
  assert(cancelCompleted.job.status === 'completed', 'cancel on completed job should preserve completed state');

  await fetch('/__smoke/reset-image-service', { method: 'POST' }).then((response) => response.json());
  const recovered = await fetch(`/api/image-jobs?job_id=${encodeURIComponent(jobId)}&include_events=1`).then((response) => response.json());
  assert(recovered.job.status === 'completed', 'recovered image job did not use projection status');
  assert(recovered.job.recovered_from_projection === true, 'service reset did not force projection recovery');
  assert((recovered.projection.events || []).some((event) => event.event_type === 'image_job.completed'), 'completed event missing from public projection');

  const eventsPayload = await fetch(`/__smoke/events?request_id=${encodeURIComponent(requestId)}`).then((response) => response.json());
  const eventTypes = (eventsPayload.events || []).map((event) => event.event_type);
  assert(eventTypes[0] === 'image_job.started', 'first durable image event should be image_job.started');
  assert(eventTypes.includes('image_job.progress'), 'progress event missing');
  assert(eventTypes.filter((type) => type === 'image_job.artifact').length === 2, 'artifact events missing');
  assert(eventTypes.includes('image_job.completed'), 'completed event missing');

  const serialized = JSON.stringify({ start, status, collect, cancelCompleted, recovered, eventsPayload });
  assert(!serialized.includes('privateprompt'), 'private/sensitive identifiers leaked through image job API');

  const pureImageJobProjection = {
    ...recovered.projection,
    request_id: `${recovered.projection.request_id}-pure`,
    messages: [],
    terminal_message: ''
  };
  assert((pureImageJobProjection.messages || []).length === 0, 'pure image job projection should not depend on assistant messages');
  const rendered = renderRuntimeProjectionRequest(pureImageJobProjection, 'image_job_api_smoke');
  assert(rendered === true, 'pure image job projection was not renderable');
  await wait('rendered image job bubble', () => {
    const bot = document.querySelector(`[data-request-id="${CSS.escape(pureImageJobProjection.request_id)}"]`);
    return bot && bot.dataset.runtimeProjectionSource === 'image_job_api_smoke';
  });
  await wait('rendered image artifacts', () => document.querySelectorAll('.media-content .artifact-card[data-artifact-kind="image"]').length >= 2);
  await wait('image previews loaded', () => Array.from(document.querySelectorAll('.media-content img')).every((img) => img.complete && img.naturalWidth > 0));

  const bot = document.querySelector(`[data-request-id="${CSS.escape(pureImageJobProjection.request_id)}"]`);
  const answer = bot.querySelector('.answer-content');
  const imageCards = Array.from(bot.querySelectorAll('.media-content .artifact-card[data-artifact-kind="image"]'));
  const imageSrcs = Array.from(bot.querySelectorAll('.media-content img')).map((img) => img.getAttribute('src') || '');
  assert(answer.innerText.includes('Image generation completed'), 'image job summary text missing');
  assert(answer.dataset.rawMd.includes('Image generation completed'), 'raw Markdown metadata missing image job summary');
  assert(imageCards.length === 2, 'rendered image artifact card count mismatch');
  assert(imageCards.every((card) => (card.dataset.artifactUrl || '').includes('/api/file?path=')), 'image artifacts did not route through backend file API');
  assert(imageCards.every((card) => card.querySelectorAll('.artifact-actions').length === 1), 'image artifact actions missing');
  assert(!bot.querySelector('.artifact-card-disabled'), 'safe dry-run artifacts rendered as disabled');
  assert(imageSrcs.every((src) => src.includes('/api/file?path=')), 'image preview did not use backend file API');
  assert(!bot.innerHTML.includes('privateprompt'), 'private identifiers leaked into rendered DOM');

  return {
    jobId,
    requestId,
    recoveredFromProjection: recovered.job.recovered_from_projection,
    startStatus: start.job.status,
    statusStatus: status.job.status,
    collectStatus: collect.job.status,
    cancelCompletedStatus: cancelCompleted.job.status,
    eventTypes,
    projectionState: recovered.projection.state,
    projectionImageJobs: (recovered.projection.image_jobs || []).length,
    renderedSource: bot.dataset.runtimeProjectionSource,
    renderedEventId: bot.dataset.runtimeProjectionEventId,
    artifactCards: imageCards.length,
    imageSrcs,
    uploadRuntimeUrl,
    localPosixRuntimeUrl,
    apiCalls: eventsPayload.api_calls,
    visibleText: document.getElementById('chat-messages')?.innerText || ''
  };
})();
"""


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    started = time.time()
    with tempfile.TemporaryDirectory() as workspace:
        with image_job_smoke_server(Path(workspace)) as (url, handler_cls):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.headed)
                page = browser.new_page(viewport={"width": args.width, "height": args.height})
                page.add_init_script(_image_job_stub_script())
                page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
                page.on(
                    "console",
                    lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                    if msg.type == "error"
                    else None,
                )
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                page.wait_for_function(
                    "() => typeof renderRuntimeProjectionRequest === 'function'",
                    timeout=args.timeout_ms,
                )
                metrics = page.evaluate(_image_job_probe_script())
                metrics["serverApiCalls"] = handler_cls.api_calls
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
    parser = argparse.ArgumentParser(description="Run Web image jobs browser/API smoke.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium instead of running headless.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--screenshot", default="", help="Optional screenshot path for the rendered image job projection.")
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
