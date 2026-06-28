#!/usr/bin/env python3
"""Smoke the image-generation skill through the real CLI/stdin entrypoint.

The smoke runs ``skills/image-generation/scripts/generate.py --stdin`` against a
local fake GPT Image-compatible API. It proves the tool invocation path starts
with ``gpt-image-2-pro``, falls back only to ``gpt-image-2`` on the same OpenAI
route when the pro model is unavailable, and surfaces ``model_fallback`` for
both text-to-image and edit/image-to-image calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GENERATE = ROOT / "skills" / "image-generation" / "scripts" / "generate.py"
GENERATION_ROUTE_SUFFIX = "/images/generations"
EDIT_ROUTE_SUFFIX = "/images/edits"
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

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
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
        model = ""
        prompt = ""
        has_image_file = False
        if route == "generations":
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            model = str(payload.get("model") or "")
            prompt = str(payload.get("prompt") or "")
        else:
            model = self._multipart_field(body, "model")
            prompt = self._multipart_field(body, "prompt")
            has_image_file = b'name="image"' in body or b'name="image[]"' in body

        self.calls.append({
            "route": route,
            "path": self.path,
            "model": model,
            "promptHash": hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12],
            "promptLength": len(prompt),
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
        self._send_json(400, {"error": {"message": f"unexpected model {model}", "code": "unexpected_model"}})


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


def _run_generate(payload: dict[str, Any], *, api_base: str, output_dir: Path, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    for key in (
        "GEMINI_API_KEY",
        "ARK_API_KEY",
        "DASHSCOPE_API_KEY",
        "MINIMAX_API_KEY",
        "LINKAI_API_KEY",
        "SKILL_IMAGE_GENERATION_PROVIDER",
        "SKILL_IMAGE_GENERATION_MODEL",
    ):
        env.pop(key, None)
    env.update({
        "OPENAI_API_KEY": "sk-smoke-tool-invocation",
        "OPENAI_API_BASE": api_base,
        "IMAGE_OUTPUT_DIR": str(output_dir),
        "PYTHONIOENCODING": "utf-8",
    })
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(GENERATE), "--stdin"],
        cwd=str(ROOT),
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    duration_ms = round((time.time() - started) * 1000)
    try:
        stdout_json = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        stdout_json = {"raw": proc.stdout}
    return {
        "returncode": proc.returncode,
        "stdout": stdout_json,
        "stderr": proc.stderr,
        "duration_ms": duration_ms,
    }


def _assert_success(case: str, result: dict[str, Any], route_calls: list[dict[str, Any]]) -> dict[str, Any]:
    if result["returncode"] != 0:
        raise AssertionError(f"{case} invocation failed: {result}")
    payload = result["stdout"]
    fallback = payload.get("model_fallback") or {}
    if payload.get("model") != "gpt-image-2":
        raise AssertionError(f"{case} did not finish on gpt-image-2: {payload}")
    if fallback.get("used") is not True:
        raise AssertionError(f"{case} missing model_fallback.used: {payload}")
    if fallback.get("from_model") != "gpt-image-2-pro" or fallback.get("to_model") != "gpt-image-2":
        raise AssertionError(f"{case} fallback target mismatch: {fallback}")
    if fallback.get("provider") != "OpenAI":
        raise AssertionError(f"{case} fallback provider mismatch: {fallback}")
    images = payload.get("images") or []
    if len(images) != 1 or not Path(images[0].get("url", "")).exists():
        raise AssertionError(f"{case} image output missing: {payload}")
    if [call["model"] for call in route_calls] != ["gpt-image-2-pro", "gpt-image-2"]:
        raise AssertionError(f"{case} model attempt order mismatch: {route_calls}")
    if "sk-smoke-tool-invocation" in json.dumps(payload) or "sk-smoke-tool-invocation" in result.get("stderr", ""):
        raise AssertionError(f"{case} leaked fake API key")
    return {
        "model": payload.get("model"),
        "model_fallback": fallback,
        "image_count": len(images),
        "attempted_models": [call["model"] for call in route_calls],
        "duration_ms": result["duration_ms"],
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "outputs"
        input_image = tmp_path / "input.png"
        input_image.write_bytes(PNG_BYTES)

        with FakeImageApiServer() as api_base:
            generation = _run_generate(
                {
                    "prompt": "tool invocation text-to-image fallback smoke",
                    "quality": "low",
                    "output_format": "png",
                },
                api_base=api_base,
                output_dir=output_dir,
                timeout=args.timeout,
            )
            generation_calls = list(FakeImageApiHandler.calls)
            FakeImageApiHandler.calls = []
            edit = _run_generate(
                {
                    "prompt": "tool invocation edit fallback smoke",
                    "image_url": str(input_image),
                    "quality": "low",
                    "output_format": "png",
                },
                api_base=api_base,
                output_dir=output_dir,
                timeout=args.timeout,
            )
            edit_calls = list(FakeImageApiHandler.calls)

        generation_route_calls = [call for call in generation_calls if call["route"] == "generations"]
        edit_route_calls = [call for call in edit_calls if call["route"] == "edits"]
        if len(generation_route_calls) != 2:
            raise AssertionError(f"generation route calls mismatch: {generation_calls}")
        if len(edit_route_calls) != 2:
            raise AssertionError(f"edit route calls mismatch: {edit_calls}")
        if not all(call["authorization_seen"] for call in generation_route_calls + edit_route_calls):
            raise AssertionError("fake API did not receive Authorization headers")
        if not all(call["has_image_file"] for call in edit_route_calls):
            raise AssertionError(f"edit route did not receive multipart image file: {edit_route_calls}")

        return {
            "status": "PASS",
            "generation": _assert_success("generation", generation, generation_route_calls),
            "edit": _assert_success("edit", edit, edit_route_calls),
            "calls": generation_route_calls + edit_route_calls,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run image generation tool-invocation fallback smoke.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--artifact", default="", help="Optional JSON artifact path.")
    args = parser.parse_args()
    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover - script-level report
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    if args.artifact:
        artifact = Path(args.artifact)
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
        result["artifact"] = str(artifact)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
