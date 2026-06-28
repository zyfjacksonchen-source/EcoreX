#!/usr/bin/env python3
"""Smoke Web ImageJobs API provider fallback through the real skill runner.

This runs the Web `/api/image-jobs` handler against a local fake GPT Image API.
The path under test is:

    ImageJobsHandler -> ImageJobService -> _image_job_skill_runner
    -> skills/image-generation/scripts/generate.py --stdin

It proves provider/model fallback telemetry survives durable runtime events and
RuntimeProjection without requiring external provider credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import types
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GENERATION_ROUTE_SUFFIX = "/images/generations"
EDIT_ROUTE_SUFFIX = "/images/edits"
FAKE_API_KEY = "sk-smoke-image-job-fallback"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    + (1).to_bytes(4, "big")
    + (1).to_bytes(4, "big")
    + b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


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


class FakeImageApiHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, _format: str, *_args: Any) -> None:  # pragma: no cover - quiet smoke server
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _multipart_field(body: bytes, name: str) -> str:
        pattern = rb'name="' + re.escape(name.encode("utf-8")) + rb'"\r\n\r\n([^\r\n]+)'
        match = re.search(pattern, body)
        return match.group(1).decode("utf-8", "replace") if match else ""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type") or ""
        if self.path.endswith(GENERATION_ROUTE_SUFFIX):
            route = "generations"
        elif self.path.endswith(EDIT_ROUTE_SUFFIX):
            route = "edits"
        else:
            self._send_json(404, {"error": {"message": f"unexpected route {self.path}"}})
            return

        if route == "generations":
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            model = str(payload.get("model") or "")
            prompt = str(payload.get("prompt") or "")
            has_image_file = False
        else:
            model = self._multipart_field(body, "model")
            prompt = self._multipart_field(body, "prompt")
            has_image_file = b'name="image"' in body or b'name="image[]"' in body

        self.calls.append({
            "route": route,
            "path": self.path,
            "model": model,
            "prompt": prompt,
            "content_type": content_type.split(";", 1)[0],
            "has_image_file": has_image_file,
            "authorization_seen": bool(self.headers.get("Authorization")),
        })

        if model == "gpt-image-2-pro":
            self._send_json(404, {
                "error": {
                    "message": "model gpt-image-2-pro does not exist or is unavailable",
                    "code": "model_not_found",
                    "type": "invalid_request_error",
                }
            })
            return
        if model == "gpt-image-2":
            self._send_json(200, {"data": [{"b64_json": PNG_B64}]})
            return
        self._send_json(400, {"error": {"message": f"unexpected model {model}"}})


class FakeImageApiServer:
    def __enter__(self) -> str:
        FakeImageApiHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImageApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def _provider_env(api_base: str):
    keys = (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "GEMINI_API_KEY",
        "ARK_API_KEY",
        "DASHSCOPE_API_KEY",
        "MINIMAX_API_KEY",
        "LINKAI_API_KEY",
        "SKILL_IMAGE_GENERATION_PROVIDER",
        "SKILL_IMAGE_GENERATION_MODEL",
        "PYTHONIOENCODING",
    )
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update({
            "OPENAI_API_KEY": FAKE_API_KEY,
            "OPENAI_API_BASE": api_base,
            "PYTHONIOENCODING": "utf-8",
        })
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _invoke_image_job(web_channel: Any, body: dict[str, Any], *, workspace: Path, timeout: int) -> dict[str, Any]:
    with patch.object(web_channel, "_require_auth", return_value=None), \
        patch.object(web_channel, "conf", return_value={
            "agent_workspace": str(workspace),
            "image_request_timeout_seconds": timeout,
        }), \
        patch.object(web_channel.web, "data", return_value=json.dumps(body).encode("utf-8")):
        return json.loads(web_channel.ImageJobsHandler().POST())


def _assert_case(case: str, payload: dict[str, Any], events: list[dict[str, Any]], calls: list[dict[str, Any]], route: str) -> dict[str, Any]:
    if payload.get("status") != "success":
        raise AssertionError(f"{case} image job failed: {payload}")
    job = payload.get("job") or {}
    projection = payload.get("projection") or {}
    projected_jobs = projection.get("image_jobs") or []
    if job.get("status") != "completed" or not projected_jobs:
        raise AssertionError(f"{case} image job did not complete/project: {payload}")
    projected = projected_jobs[0]
    if projected.get("fallback_used") is not True:
        raise AssertionError(f"{case} projection missing fallback_used: {projected}")
    for key, expected in {
        "fallback_provider": "OpenAI",
        "fallback_from_model": "gpt-image-2-pro",
        "fallback_to_model": "gpt-image-2",
        "fallback_reason": "client_error",
        "last_provider": "OpenAI",
        "last_model": "gpt-image-2",
    }.items():
        if projected.get(key) != expected:
            raise AssertionError(f"{case} projection {key} mismatch: {projected}")
    if projected.get("attempted_provider_count") != 1:
        raise AssertionError(f"{case} attempted provider count mismatch: {projected}")

    route_calls = [call for call in calls if call.get("route") == route]
    if [call.get("model") for call in route_calls] != ["gpt-image-2-pro", "gpt-image-2"]:
        raise AssertionError(f"{case} model order mismatch: {route_calls}")
    if not all(call.get("authorization_seen") for call in route_calls):
        raise AssertionError(f"{case} fake API did not see auth headers: {route_calls}")
    if route == "edits" and not all(call.get("has_image_file") for call in route_calls):
        raise AssertionError(f"{case} edit route did not receive image file: {route_calls}")

    progress_payloads = [
        event.get("payload") or {}
        for event in events
        if event.get("event_type") == "image_job.progress"
    ]
    if not any(item.get("status") == "fallback" and item.get("fallback_to_model") == "gpt-image-2" for item in progress_payloads):
        raise AssertionError(f"{case} durable fallback progress missing: {progress_payloads}")
    serialized = json.dumps({"payload": payload, "events": events}, ensure_ascii=False)
    if FAKE_API_KEY in serialized:
        raise AssertionError(f"{case} leaked fake API key")
    if "does not exist or is unavailable" in serialized:
        raise AssertionError(f"{case} leaked provider raw error message")
    return {
        "status": "PASS",
        "job_id": job.get("job_id"),
        "request_id": job.get("request_id"),
        "route": route,
        "attempted_models": [call.get("model") for call in route_calls],
        "projection": {
            "fallback_used": projected.get("fallback_used"),
            "fallback_from_model": projected.get("fallback_from_model"),
            "fallback_to_model": projected.get("fallback_to_model"),
            "last_provider": projected.get("last_provider"),
            "last_model": projected.get("last_model"),
            "artifact_count": len(projected.get("artifacts") or []),
        },
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    _ensure_web_stub()
    from agent.protocol import (
        get_run_event_ledger,
        reset_image_job_service_for_tests,
        reset_run_event_ledger_for_tests,
        reset_run_ledger_for_tests,
    )
    from channel.web import web_channel

    with tempfile.TemporaryDirectory() as workspace_raw:
        workspace = Path(workspace_raw)
        db_path = workspace / "run-ledger.db"
        edit_input = workspace / "edit-input.png"
        edit_input.write_bytes(PNG_BYTES)
        reset_run_ledger_for_tests(db_path)
        reset_run_event_ledger_for_tests(db_path)
        ledger = get_run_event_ledger()
        reset_image_job_service_for_tests(ledger)
        try:
            with FakeImageApiServer() as api_base:
                with _provider_env(api_base):
                    generation_body = {
                        "action": "start",
                        "synchronous": True,
                        "include_events": True,
                        "request_id": "req-image-provider-fallback-generation",
                        "session_id": "session-image-provider-fallback",
                        "job_id": "image-job-provider-fallback-generation",
                        "prompt": "image job provider fallback generation smoke",
                        "provider": "openai",
                        "model": "gpt-image-2-pro",
                        "output_format": "png",
                    }
                    generation_payload = _invoke_image_job(web_channel, generation_body, workspace=workspace, timeout=args.timeout)
                    generation_events = ledger.events_for_request(generation_body["request_id"], limit=0)
                    generation_calls = list(FakeImageApiHandler.calls)

                    FakeImageApiHandler.calls = []
                    edit_body = {
                        "action": "start",
                        "synchronous": True,
                        "include_events": True,
                        "request_id": "req-image-provider-fallback-edit",
                        "session_id": "session-image-provider-fallback",
                        "job_id": "image-job-provider-fallback-edit",
                        "prompt": "image job provider fallback edit smoke",
                        "image_url": str(edit_input),
                        "provider": "openai",
                        "model": "gpt-image-2-pro",
                        "output_format": "png",
                    }
                    edit_payload = _invoke_image_job(web_channel, edit_body, workspace=workspace, timeout=args.timeout)
                    edit_events = ledger.events_for_request(edit_body["request_id"], limit=0)
                    edit_calls = list(FakeImageApiHandler.calls)

            result = {
                "status": "PASS",
                "generation": _assert_case("generation", generation_payload, generation_events, generation_calls, "generations"),
                "edit": _assert_case("edit", edit_payload, edit_events, edit_calls, "edits"),
                "calls": generation_calls + edit_calls,
            }
        finally:
            reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")
            reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-event-ledger-test-reset.db")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Web ImageJobs provider fallback integration smoke.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--artifact", default="", help="Optional JSON artifact path.")
    args = parser.parse_args()
    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    if args.artifact:
        artifact = Path(args.artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
