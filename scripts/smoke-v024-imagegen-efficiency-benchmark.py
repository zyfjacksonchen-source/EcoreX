#!/usr/bin/env python3
"""Benchmark EcoreX image-generation overhead with redacted timing evidence.

The script can run without real provider credentials by using a local fake
GPT Image-compatible endpoint. A later real Codex/imagegen timing artifact can
be supplied with --codex-result to compare identical case ids. Use
--mode codex-template to create a redacted Codex timing template for the exact
case ids and prompt hashes used by the EcoreX benchmark.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.tools.imagegen.provider_runner import (  # noqa: E402
    image_generation_env_with_config,
    run_image_generation_payload,
)

GENERATE = ROOT / "skills" / "image-generation" / "scripts" / "generate.py"
GENERATION_ROUTE_SUFFIX = "/images/generations"
EDIT_ROUTE_SUFFIX = "/images/edits"
PROVIDER_IDS = {"openai", "linkai", "gemini", "seedream", "qwen", "minimax"}
CODEX_TEMPLATE_MODE = "codex-imagegen-timing-template"
CODEX_RESULT_MODE = "codex-imagegen-timing-result"
CODEX_RESULT_SCHEMA_VERSION = "r24-14b-codex-timing-v1"
CODEX_RESULT_TOP_LEVEL_KEYS = {
    "status",
    "redacted",
    "mode",
    "schemaVersion",
    "resultModeRequired",
    "resultStatusRequired",
    "timingSemantics",
    "cases",
}
CODEX_RESULT_CASE_KEYS = {
    "caseId",
    "promptHash",
    "promptLength",
    "referenceImageCount",
    "size",
    "outputFormat",
    "qualityRetryMax",
    "status",
    "finalUsableMs",
    "wallMs",
}
CONFIG_TO_ENV = {
    "open_ai_api_key": "OPENAI_API_KEY",
    "open_ai_api_base": "OPENAI_API_BASE",
    "linkai_api_key": "LINKAI_API_KEY",
    "linkai_api_base": "LINKAI_API_BASE",
    "gemini_api_key": "GEMINI_API_KEY",
    "gemini_api_base": "GEMINI_API_BASE",
    "ark_api_key": "ARK_API_KEY",
    "ark_api_base": "ARK_API_BASE",
    "dashscope_api_key": "DASHSCOPE_API_KEY",
    "dashscope_api_base": "DASHSCOPE_API_BASE",
    "minimax_api_key": "MINIMAX_API_KEY",
    "minimax_api_base": "MINIMAX_API_BASE",
}


def _gradient_png_bytes() -> bytes:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - smoke host guard
        raise RuntimeError(f"Pillow is required for imagegen benchmark: {exc.__class__.__name__}") from exc
    import io

    image = Image.new("RGB", (128, 128))
    draw = ImageDraw.Draw(image)
    for x in range(128):
        color = (40 + (x % 100), 90 + (x % 80), 210 - (x % 120))
        draw.line([(x, 0), (x, 127)], fill=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


PNG_B64 = base64.b64encode(_gradient_png_bytes()).decode("ascii")


class FakeImageApiHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    provider_delay_ms = 250

    def log_message(self, _format: str, *_args: Any) -> None:  # pragma: no cover - quiet smoke server
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        route = "generations" if self.path.endswith(GENERATION_ROUTE_SUFFIX) else "edits" if self.path.endswith(EDIT_ROUTE_SUFFIX) else ""
        if not route:
            self._send_json(404, {"error": {"message": "unexpected route"}})
            return
        prompt = _prompt_from_request(route, body)
        self.calls.append({
            "route": route,
            "promptHash": _hash_text(prompt),
            "promptLength": len(prompt),
            "authorization_seen": bool(self.headers.get("Authorization")),
        })
        time.sleep(max(0, int(self.provider_delay_ms)) / 1000)
        self._send_json(200, {"data": [{"b64_json": PNG_B64}]})


def _prompt_from_request(route: str, body: bytes) -> str:
    if route == "generations":
        try:
            return str((json.loads(body.decode("utf-8")) or {}).get("prompt") or "")
        except Exception:
            return ""
    marker = b'name="prompt"\r\n\r\n'
    start = body.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = body.find(b"\r\n", start)
    if end < 0:
        end = len(body)
    return body[start:end].decode("utf-8", errors="replace")


class FakeImageApiServer:
    def __init__(self, *, provider_delay_ms: int):
        self.provider_delay_ms = provider_delay_ms

    def __enter__(self) -> str:
        FakeImageApiHandler.calls = []
        FakeImageApiHandler.provider_delay_ms = self.provider_delay_ms
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
def _provider_env(api_base: str, output_dir: Path):
    saved = {key: os.environ.get(key) for key in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "LINKAI_API_KEY",
        "GEMINI_API_KEY",
        "ARK_API_KEY",
        "DASHSCOPE_API_KEY",
        "MINIMAX_API_KEY",
        "SKILL_IMAGE_GENERATION_PROVIDER",
        "SKILL_IMAGE_GENERATION_MODEL",
        "IMAGE_OUTPUT_DIR",
    )}
    try:
        for key in list(saved):
            os.environ.pop(key, None)
        os.environ.update({
            "OPENAI_API_KEY": "sk-v024-imagegen-benchmark",
            "OPENAI_API_BASE": api_base,
            "IMAGE_OUTPUT_DIR": str(output_dir),
            "PYTHONIOENCODING": "utf-8",
        })
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


CASES = [
    {
        "caseId": "icon-no-reference",
        "prompt": "Create a clean square app icon: abstract orange spark on white, no text, no watermark.",
        "reference": False,
    },
    {
        "caseId": "poster-reference-edit",
        "prompt": "Use the reference colors and layout rhythm to create a polished social poster, no text.",
        "reference": True,
    },
]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _canonical_json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _case_public(case: dict[str, Any]) -> dict[str, Any]:
    prompt = str(case.get("prompt") or "")
    return {
        "caseId": case["caseId"],
        "promptHash": _hash_text(prompt),
        "promptLength": len(prompt),
        "referenceImageCount": 1 if case.get("reference") else 0,
        "size": "1024x1024",
        "outputFormat": "png",
        "qualityRetryMax": 1,
    }


def _write_reference(root: Path) -> Path:
    ref = root / "reference.png"
    ref.write_bytes(base64.b64decode(PNG_B64))
    return ref


def _run_ecorex_direct(case: dict[str, Any], output_dir: Path, reference: Path | None, *, provider: str) -> dict[str, Any]:
    from agent.tools.imagegen.imagegen import ImageGenTool

    params: dict[str, Any] = {
        "prompt": case["prompt"],
        "output_dir": str(output_dir),
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "provider": provider,
        "quality_retry_max": 1,
        "timeout": 60,
    }
    if reference is not None:
        params["image_url"] = str(reference)
    started = time.perf_counter()
    result = ImageGenTool().execute(params)
    wall_ms = int((time.perf_counter() - started) * 1000)
    if result.status != "success":
        raise RuntimeError(f"direct imagegen failed for {case['caseId']}: {result.result}")
    payload = result.result if isinstance(result.result, dict) else {}
    timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
    return {
        "status": "pass",
        "wallMs": wall_ms,
        "finalUsableMs": int(payload.get("durationMs") or wall_ms),
        "providerTotalLatencyMs": int(timing.get("providerTotalLatencyMs") or 0),
        "qualityTotalLatencyMs": int(timing.get("qualityTotalLatencyMs") or 0),
        "finalizationTotalLatencyMs": int(timing.get("finalizationTotalLatencyMs") or 0),
        "postprocessTotalLatencyMs": int(timing.get("postprocessTotalLatencyMs") or 0),
        "attemptCount": int(timing.get("attemptCount") or 0),
        "retryCount": int(timing.get("retryCount") or 0),
        "finalizationStatus": str((payload.get("finalization") or {}).get("status") or "unknown"),
        "imageCount": len(payload.get("images") or []),
    }


def _image_runner_env(output_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["IMAGE_OUTPUT_DIR"] = str(output_dir)
    return image_generation_env_with_config(env)


def _runner_from_generate(output_dir: Path):
    def _runner(task: dict[str, Any], _emit_progress, _cancel_event) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": task.get("prompt") or "",
            "size": task.get("size") or "1024x1024",
            "quality": task.get("quality") or "low",
            "output_format": task.get("output_format") or "png",
            "provider": task.get("provider") or "openai",
        }
        if task.get("image_url"):
            payload["image_url"] = task["image_url"]
        env = _image_runner_env(output_dir)
        completed = run_image_generation_payload(
            payload,
            script_path=GENERATE,
            output_dir=output_dir,
            env=env,
        )
        if int(completed.get("returncode") or 0) != 0:
            raise RuntimeError("provider failed")
        body = completed.get("payload") if isinstance(completed.get("payload"), dict) else {}
        images = body.get("images") if isinstance(body.get("images"), list) else []
        if not images:
            raise RuntimeError("provider produced no image")
        path = str(images[0].get("url") or images[0].get("path") or "")
        return {"kind": "image", "path": path, "title": Path(path).name or "image.png", "fileType": "image"}

    return _runner


def _run_ecorex_job(case: dict[str, Any], output_dir: Path, reference: Path | None, *, provider: str) -> dict[str, Any]:
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

    output_dir.mkdir(parents=True, exist_ok=True)
    request_id = f"req-v024-imagegen-benchmark-{case['caseId']}"
    ledger = reset_run_event_ledger_for_tests(output_dir / "events.db")
    task = {
        "task_id": "task-1",
        "prompt": case["prompt"],
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "provider": provider,
        "quality_retry_max": 1,
    }
    if reference is not None:
        task["image_url"] = str(reference)
    ImageJobService(ledger).start(
        request_id=request_id,
        session_id="session-v024-imagegen-benchmark",
        job_id=f"image-job-v024-benchmark-{case['caseId']}",
        tasks=[task],
        runner=_runner_from_generate(output_dir),
        synchronous=True,
    )
    projection = RuntimeProjectionService(ledger).request_projection(request_id)
    job = (projection.get("image_jobs") or [{}])[0]
    task_projection = (job.get("tasks") or [{}])[0]
    return {
        "status": "pass" if job.get("status") == "completed" else "fail",
        "finalUsableMs": int(job.get("total_latency_ms") or 0),
        "providerTotalMs": int(job.get("provider_total_ms") or 0),
        "qualityTotalMs": int(job.get("quality_total_ms") or 0),
        "finalizationTotalMs": int(job.get("finalization_total_ms") or 0),
        "postprocessTotalMs": int(job.get("postprocess_total_ms") or 0),
        "taskStatus": task_projection.get("status") or "",
        "artifactCount": int(job.get("artifact_count") or 0),
        "retryCount": int(task_projection.get("retry_count") or 0),
    }


def _load_codex_result(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"available": False, "cases": {}, "status": "pending-codex-result"}
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if str(data.get("status") or "").upper() != "PASS":
        return {"available": False, "cases": {}, "status": "incomplete-codex-result"}
    if data.get("mode") != CODEX_RESULT_MODE or data.get("redacted") is not True:
        return {
            "available": False,
            "cases": {},
            "status": "invalid-codex-result",
            "expectedMode": CODEX_RESULT_MODE,
        }
    if data.get("schemaVersion") != CODEX_RESULT_SCHEMA_VERSION:
        return {
            "available": False,
            "cases": {},
            "status": "invalid-codex-result",
            "expectedSchemaVersion": CODEX_RESULT_SCHEMA_VERSION,
        }
    unknown_top_level_keys = sorted(set(data) - CODEX_RESULT_TOP_LEVEL_KEYS)
    if unknown_top_level_keys:
        return {
            "available": False,
            "cases": {},
            "status": "invalid-codex-result",
            "unknownTopLevelKeys": unknown_top_level_keys,
        }
    expected_case_ids = {str(case.get("caseId") or "") for case in CASES}
    raw_cases = data.get("cases") or []
    if not isinstance(raw_cases, list):
        return {"available": False, "cases": {}, "status": "invalid-codex-result"}
    unknown_case_keys = {
        str(item.get("caseId") or f"case-{index}"): sorted(set(item) - CODEX_RESULT_CASE_KEYS)
        for index, item in enumerate(raw_cases)
        if isinstance(item, dict) and set(item) - CODEX_RESULT_CASE_KEYS
    }
    if unknown_case_keys:
        return {
            "available": False,
            "cases": {},
            "status": "invalid-codex-result",
            "unknownCaseKeys": unknown_case_keys,
        }
    raw_case_ids = [
        str(item.get("caseId") or "")
        for item in raw_cases
        if isinstance(item, dict) and item.get("caseId")
    ]
    unknown_case_ids = sorted(set(raw_case_ids) - expected_case_ids)
    duplicate_case_ids = sorted({case_id for case_id in raw_case_ids if raw_case_ids.count(case_id) > 1})
    if unknown_case_ids or duplicate_case_ids:
        return {
            "available": False,
            "cases": {},
            "status": "invalid-codex-result",
            "unknownCaseIds": unknown_case_ids,
            "duplicateCaseIds": duplicate_case_ids,
        }
    cases = {
        str(item.get("caseId") or ""): item
        for item in raw_cases
        if _codex_case_valid(item)
    }
    if not cases:
        return {"available": False, "cases": {}, "status": "invalid-codex-result"}
    missing_case_ids = sorted(expected_case_ids - set(cases))
    if missing_case_ids:
        return {
            "available": False,
            "cases": {},
            "status": "incomplete-codex-result",
            "missingCaseIds": missing_case_ids,
            "expectedCaseCount": len(expected_case_ids),
            "validCaseCount": len(cases),
        }
    return {
        "available": True,
        "cases": cases,
        "caseCount": len(cases),
        "schemaVersion": CODEX_RESULT_SCHEMA_VERSION,
        "sourceSha256": _canonical_json_sha256(data),
        "status": "ready",
    }


def _codex_case_valid(item: Any) -> bool:
    if not isinstance(item, dict) or not item.get("caseId"):
        return False
    if set(item) - CODEX_RESULT_CASE_KEYS:
        return False
    if str(item.get("status") or "").strip().lower() != "pass":
        return False
    case_id = str(item.get("caseId") or "")
    expected_hash = _case_prompt_hash(case_id)
    if not expected_hash:
        return False
    try:
        final_ms = int(item.get("finalUsableMs") or 0)
    except (TypeError, ValueError):
        return False
    if final_ms <= 0:
        return False
    if str(item.get("promptHash") or "") != expected_hash:
        return False
    expected_requirements = _case_requirements(case_id)
    if not expected_requirements:
        return False
    try:
        prompt_length = int(item.get("promptLength"))
        reference_count = int(item.get("referenceImageCount"))
        retry_max = int(item.get("qualityRetryMax"))
    except (TypeError, ValueError):
        return False
    if prompt_length != expected_requirements["promptLength"]:
        return False
    if reference_count != expected_requirements["referenceImageCount"]:
        return False
    if retry_max != expected_requirements["qualityRetryMax"]:
        return False
    if str(item.get("size") or "") != expected_requirements["size"]:
        return False
    if str(item.get("outputFormat") or "") != expected_requirements["outputFormat"]:
        return False
    return True


def _case_prompt_hash(case_id: str) -> str:
    for case in CASES:
        if case.get("caseId") == case_id:
            return _hash_text(str(case.get("prompt") or ""))
    return ""


def _case_requirements(case_id: str) -> dict[str, Any]:
    for case in CASES:
        if case.get("caseId") == case_id:
            public = _case_public(case)
            return {
                "promptLength": int(public["promptLength"]),
                "referenceImageCount": int(public["referenceImageCount"]),
                "size": str(public["size"]),
                "outputFormat": str(public["outputFormat"]),
                "qualityRetryMax": int(public["qualityRetryMax"]),
            }
    return {}


def _real_provider_readiness() -> dict[str, bool]:
    config_values: dict[str, Any] = {}
    try:
        from config import conf

        config_values = conf()
    except Exception:
        config_values = {}
    pairs = {
        "openai": ("OPENAI_API_KEY", "open_ai_api_key"),
        "linkai": ("LINKAI_API_KEY", "linkai_api_key"),
        "gemini": ("GEMINI_API_KEY", "gemini_api_key"),
        "seedream": ("ARK_API_KEY", "ark_api_key"),
        "qwen": ("DASHSCOPE_API_KEY", "dashscope_api_key"),
        "minimax": ("MINIMAX_API_KEY", "minimax_api_key"),
    }
    readiness: dict[str, bool] = {}
    for provider, (env_key, config_key) in pairs.items():
        readiness[provider] = bool(os.environ.get(env_key) or config_values.get(config_key))
    return readiness


def _normalize_provider(value: Any) -> str:
    provider = str(value or "openai").strip().lower()
    return provider if provider in PROVIDER_IDS else "openai"


def _provider_ready(provider: str, readiness: dict[str, bool]) -> bool:
    return bool(readiness.get(_normalize_provider(provider)))


def _comparison(case_id: str, ecorex: dict[str, Any], codex: dict[str, Any]) -> dict[str, Any]:
    if not codex.get("available"):
        return {"available": False, "status": str(codex.get("status") or "pending")}
    item = (codex.get("cases") or {}).get(case_id)
    if not item:
        return {"available": False, "status": "missing-case"}
    codex_ms = int(item.get("finalUsableMs") or 0)
    ecorex_ms = int(ecorex.get("finalUsableMs") or 0)
    delta = round((ecorex_ms - codex_ms) * 100 / codex_ms, 2) if codex_ms else None
    return {
        "available": True,
        "codexFinalUsableMs": codex_ms,
        "ecorexFinalUsableMs": ecorex_ms,
        "deltaPct": delta,
    }


def _codex_comparison_summary(codex: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "available": codex.get("available") is True,
        "status": "ready" if codex.get("available") else str(codex.get("status") or "pending-codex-result"),
    }
    if codex.get("available"):
        summary.update({
            "schemaVersion": str(codex.get("schemaVersion") or ""),
            "caseCount": int(codex.get("caseCount") or 0),
            "sourceSha256": str(codex.get("sourceSha256") or ""),
            "validatedBy": "ecorex-v024-imagegen-efficiency-loader",
        })
    return summary


def _codex_result_artifact_payload(codex: dict[str, Any]) -> dict[str, Any]:
    if codex.get("available") is not True:
        payload: dict[str, Any] = {
            "status": "FAIL",
            "redacted": True,
            "mode": CODEX_RESULT_MODE,
            "schemaVersion": CODEX_RESULT_SCHEMA_VERSION,
            "valid": False,
            "reason": str(codex.get("status") or "invalid-codex-result"),
        }
        for key in ("expectedMode", "expectedSchemaVersion", "missingCaseIds", "expectedCaseCount", "validCaseCount"):
            if key in codex:
                payload[key] = codex[key]
        if "unknownCaseIds" in codex:
            payload["unknownCaseIdCount"] = len(codex.get("unknownCaseIds") or [])
        if "duplicateCaseIds" in codex:
            payload["duplicateCaseIdCount"] = len(codex.get("duplicateCaseIds") or [])
        if "unknownTopLevelKeys" in codex:
            payload["unknownTopLevelKeyCount"] = len(codex.get("unknownTopLevelKeys") or [])
        if "unknownCaseKeys" in codex:
            unknown_case_keys = codex.get("unknownCaseKeys") or {}
            if isinstance(unknown_case_keys, dict):
                payload["unknownCaseKeyCaseCount"] = len(unknown_case_keys)
                payload["unknownCaseKeyCount"] = sum(len(value or []) for value in unknown_case_keys.values())
        return payload
    cases: list[dict[str, Any]] = []
    for case in CASES:
        public_case = _case_public(case)
        source_case = (codex.get("cases") or {}).get(public_case["caseId"], {})
        final_ms = int(source_case.get("finalUsableMs") or 0)
        wall_ms = int(source_case.get("wallMs") or final_ms)
        cases.append({
            **public_case,
            "status": "pass",
            "finalUsableMs": final_ms,
            "wallMs": wall_ms,
        })
    return {
        "status": "PASS",
        "redacted": True,
        "mode": CODEX_RESULT_MODE,
        "schemaVersion": CODEX_RESULT_SCHEMA_VERSION,
        "cases": cases,
    }


def _attach_overhead_metrics(item: dict[str, Any], *, provider_delay_ms: int) -> dict[str, Any]:
    attempts = max(1, int(item.get("attemptCount") or (int(item.get("retryCount") or 0) + 1)))
    synthetic_provider_ms = max(0, int(provider_delay_ms)) * attempts
    provider_total = int(item.get("providerTotalLatencyMs") or item.get("providerTotalMs") or 0)
    quality_total = int(item.get("qualityTotalLatencyMs") or item.get("qualityTotalMs") or 0)
    finalization_total = int(item.get("finalizationTotalLatencyMs") or item.get("finalizationTotalMs") or 0)
    final_usable = int(item.get("finalUsableMs") or 0)
    item["providerRunnerOverheadMs"] = max(0, provider_total - synthetic_provider_ms)
    item["qaAndFinalizationMs"] = max(0, quality_total + finalization_total)
    item["ecorexControllableOverheadMs"] = max(0, final_usable - synthetic_provider_ms)
    return item


def _timing_semantics() -> dict[str, str]:
    return {
        "providerLatencyMs": "whole in-process provider runner latency, including provider API call, response decode, and local image save",
        "providerRunnerOverheadMs": "fake-provider mode only: provider latency minus configured synthetic provider delay per attempt",
        "ecorexControllableOverheadMs": "fake-provider mode only: final usable-image time minus configured synthetic provider delay per attempt",
    }


def _codex_template_payload() -> dict[str, Any]:
    return {
        "status": "TEMPLATE",
        "redacted": True,
        "mode": CODEX_TEMPLATE_MODE,
        "schemaVersion": CODEX_RESULT_SCHEMA_VERSION,
        "resultModeRequired": CODEX_RESULT_MODE,
        "resultStatusRequired": "PASS",
        "timingSemantics": {
            "finalUsableMs": "positive integer wall-clock milliseconds from Codex imagegen request start to final usable image availability using the same prompt, size, output format, reference image presence, and quality policy",
            "wallMs": "optional positive integer total wall-clock milliseconds for the same Codex run; it cannot replace finalUsableMs",
        },
        "cases": [
            {
                **_case_public(case),
                "status": "pending-measurement",
                "finalUsableMs": 0,
                "wallMs": 0,
            }
            for case in CASES
        ],
    }


def _preflight_payload(args: argparse.Namespace, readiness: dict[str, bool]) -> dict[str, Any]:
    provider = _normalize_provider(args.provider)
    ready = _provider_ready(provider, readiness)
    return {
        "status": "PASS" if ready else "BLOCKED",
        "redacted": True,
        "mode": "real-provider-preflight",
        "provider": provider,
        "ready": ready,
        "realProviderReady": readiness,
        "timingSemantics": _timing_semantics(),
        "blockedReason": ""
        if ready
        else "No configured EcoreX image provider credentials for the selected provider; real same-prompt EcoreX-vs-Codex benchmark not run.",
    }


def _run_cases(args: argparse.Namespace, *, codex: dict[str, Any], output_dir: Path, provider_delay_ms: int | None) -> list[dict[str, Any]]:
    provider = _normalize_provider(args.provider)
    case_results: list[dict[str, Any]] = []
    for case in CASES:
        reference = _write_reference(output_dir.parent) if case.get("reference") else None
        direct = _run_ecorex_direct(case, output_dir / case["caseId"] / "direct", reference, provider=provider)
        job = _run_ecorex_job(case, output_dir / case["caseId"] / "job", reference, provider=provider)
        if provider_delay_ms is not None:
            _attach_overhead_metrics(direct, provider_delay_ms=provider_delay_ms)
            _attach_overhead_metrics(job, provider_delay_ms=provider_delay_ms)
        public_case = _case_public(case)
        public_case.update({
            "provider": provider,
            "providerDelayMs": provider_delay_ms if provider_delay_ms is not None else None,
            "ecorexDirect": direct,
            "ecorexJob": job,
            "comparison": _comparison(case["caseId"], direct, codex),
        })
        if public_case["providerDelayMs"] is None:
            public_case.pop("providerDelayMs", None)
        case_results.append(public_case)
    return case_results


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "codex-template":
        return _codex_template_payload()
    if args.mode == "codex-result":
        return _codex_result_artifact_payload(_load_codex_result(args.codex_result))
    codex = _load_codex_result(args.codex_result)
    provider = _normalize_provider(args.provider)
    readiness = _real_provider_readiness()
    if args.mode == "preflight":
        return _preflight_payload(args, readiness)
    if args.mode == "real" and not _provider_ready(provider, readiness):
        payload = _preflight_payload(args, readiness)
        payload["mode"] = "real-provider-benchmark"
        payload["codexComparison"] = _codex_comparison_summary(codex)
        payload["cases"] = []
        payload["failedCases"] = []
        return payload
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output_dir = root / "outputs"
        output_dir.mkdir()
        if args.mode == "fake":
            with FakeImageApiServer(provider_delay_ms=args.provider_delay_ms) as api_base:
                with _provider_env(api_base, output_dir):
                    case_results = _run_cases(
                        args,
                        codex=codex,
                        output_dir=output_dir,
                        provider_delay_ms=args.provider_delay_ms,
                    )
        else:
            case_results = _run_cases(
                args,
                codex=codex,
                output_dir=output_dir,
                provider_delay_ms=None,
            )
    failures = [
        item["caseId"]
        for item in case_results
        if item["ecorexDirect"].get("status") != "pass" or item["ecorexJob"].get("status") != "pass"
    ]
    payload = {
        "status": "FAIL" if failures else "PASS",
        "redacted": True,
        "mode": "fake-provider-overhead" if args.mode == "fake" else "real-provider-benchmark",
        "provider": provider,
        "providerDelayMs": args.provider_delay_ms if args.mode == "fake" else None,
        "realProviderReady": readiness,
        "timingSemantics": _timing_semantics(),
        "codexComparison": _codex_comparison_summary(codex),
        "cases": case_results,
        "failedCases": failures,
    }
    if payload["providerDelayMs"] is None:
        payload.pop("providerDelayMs", None)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "preflight", "real", "codex-template", "codex-result"), default="fake")
    parser.add_argument("--provider", choices=sorted(PROVIDER_IDS), default="openai")
    parser.add_argument("--provider-delay-ms", type=int, default=250)
    parser.add_argument("--codex-result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run(args)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("status") == "PASS":
        return 0
    if args.mode == "codex-template" and payload.get("status") == "TEMPLATE":
        return 0
    if args.mode == "preflight" and payload.get("status") == "BLOCKED":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
