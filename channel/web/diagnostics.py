"""Diagnostics and log Web handlers."""

import json
import time
from pathlib import Path
from typing import Any

import web

from channel.web.handler_support import public_error_payload, require_auth, web_body_log_summary
from common.log import logger
from common.ecorex_public_payload import mask_sensitive_text


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


def parse_log_line_limit(value: Any, default: int = 200) -> int:
    try:
        return max(1, min(500, int(value or default)))
    except (TypeError, ValueError):
        return default


class LogsSnapshotHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        params = web.input(lines="200")
        return json.dumps(
            _legacy_web_channel()._log_snapshot_payload(parse_log_line_limit(params.lines)),
            ensure_ascii=False,
        )


class DiagnosticsBundleHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        params = web.input(session_id="", request_id="")
        return json.dumps(
            _legacy_web_channel()._diagnostic_bundle_payload(
                session_id=str(params.session_id or "").strip(),
                request_id=str(params.request_id or "").strip(),
            ),
            ensure_ascii=False,
        )


class LogsHandler:
    def GET(self):
        require_auth()
        accept = (web.ctx.env.get("HTTP_ACCEPT") or "").lower()
        legacy = _legacy_web_channel()
        if "text/event-stream" not in accept:
            web.header("Content-Type", "application/json; charset=utf-8")
            params = web.input(lines="200")
            return json.dumps(legacy._log_snapshot_payload(parse_log_line_limit(params.lines)), ensure_ascii=False)

        web.header("Content-Type", "text/event-stream; charset=utf-8")
        web.header("Cache-Control", "no-cache")
        web.header("X-Accel-Buffering", "no")

        from config import get_root

        log_path = legacy._resolve_run_log_path(Path(get_root()))

        def generate():
            if not log_path.is_file():
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            try:
                from agent.tools.host_diagnostics.host_diagnostics import _mask, _tail_text

                tail = _tail_text(log_path, max_lines=200, cwd=str(log_path.parent))
                if tail.get("blocked"):
                    payload = json.dumps({
                        "type": "error",
                        "message": tail.get("reason") or "run.log read blocked by permissions",
                    }, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode("utf-8")
                    return
                if not tail.get("exists"):
                    yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                    return
                chunk = "\n".join(mask_sensitive_text(line, max_chars=2000) for line in (tail.get("lines") or []))
                if chunk:
                    chunk += "\n"
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")
            except Exception as exc:
                public = public_error_payload("Log stream unavailable.", exc, type="error")
                payload = json.dumps(public, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")
                return

            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(0, 2)
                    deadline = time.time() + 600
                    while time.time() < deadline:
                        line = handle.readline()
                        if line:
                            payload = json.dumps({
                                "type": "line",
                                "content": mask_sensitive_text(_mask(line), max_chars=2000),
                            }, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode("utf-8")
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception as exc:
                logger.error(f"[WebChannel] log stream failed: {web_body_log_summary(exc)}")
                public = public_error_payload("Log stream unavailable.", exc, type="error")
                payload = json.dumps(public, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")

        return generate()
