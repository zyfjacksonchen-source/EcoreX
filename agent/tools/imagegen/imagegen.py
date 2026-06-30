"""Runtime tool wrapper for the built-in image-generation skill."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.imagegen.provider_runner import image_generation_env_with_config, run_image_generation_payload
from common.image_quality_runtime import (
    aggregate_image_finalization_decisions,
    attach_image_finalization_evidence,
    build_image_finalization_decision,
    build_image_quality_evidence,
)
from common.log import logger
from common.utils import expand_path
from config import conf


_QUALITY_RETRY_PROMPT_SUFFIX = (
    "\n\nQuality retry: regenerate a clean final image with no broken seams, "
    "no ghosted overlays, no watermark artifacts, no garbled text fragments, "
    "and preserve authorized reference-image structure when references are provided."
)


def _runtime_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_output_dir() -> Path:
    workspace = Path(expand_path(conf().get("agent_workspace", "~/EcoreX")))
    return workspace / "images"


def _safe_timeout(value: Any) -> int:
    try:
        timeout = int(float(value))
    except (TypeError, ValueError):
        timeout = 300
    return max(30, min(timeout, 1800))


def _redacted_tail(text: str, limit: int = 2000) -> str:
    value = str(text or "")
    if len(value) > limit:
        value = value[-limit:]
    for key in ("OPENAI_API_KEY", "LINKAI_API_KEY", "GEMINI_API_KEY", "ARK_API_KEY", "DASHSCOPE_API_KEY", "MINIMAX_API_KEY"):
        secret = os.environ.get(key)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)bearer\s+[a-z0-9._-]{8,}", "Bearer [REDACTED]", value)
    value = re.sub(r"sk-[a-zA-Z0-9_-]{6,}", "[REDACTED_SECRET]", value)
    value = re.sub(r"(?i)https?://[^\s\"'<>]+", "[REDACTED_URL]", value)
    value = re.sub(r"(?i)file://[^\s\"'<>]+", "[REDACTED_PATH]", value)
    value = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "[REDACTED_PATH]", value)
    return value


def _safe_text_presence(text: Any) -> Dict[str, Any]:
    value = str(text or "")
    return {
        "present": bool(value.strip()),
        "charCount": min(len(value), 100000),
        "redacted": True,
    }


def _safe_token(value: Any, limit: int = 96) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if re.search(r"\s", text):
        return None
    if re.search(r"(?i)(bearer\s+|https?://|file://|data:image/|sk-[a-z0-9_-]{6,}|[a-z]:[\\/])", text):
        return None
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)[:limit].strip(".-_")
    return token or None


def _safe_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100000 else None


def _quality_retry_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(0, min(parsed, 2))


def _safe_imagegen_failure_payload(payload: Any) -> Dict[str, Any]:
    safe: Dict[str, Any] = {"redacted": True}
    if not isinstance(payload, dict):
        safe["payloadType"] = type(payload).__name__
        safe["payloadPresent"] = bool(payload)
        return safe

    for key in ("provider", "model", "model_fallback", "code", "errorType", "error_type", "status"):
        token = _safe_token(payload.get(key))
        if token:
            safe[key] = token
    for key in ("attempted_provider_count", "attempt_count", "retry_count"):
        number = _safe_int(payload.get(key))
        if number is not None:
            safe[key] = number

    images = payload.get("images")
    if isinstance(images, list):
        safe["imageCount"] = len(images)
    elif images is not None:
        safe["imageCount"] = 1

    if any(payload.get(key) for key in ("error", "message", "stderr", "raw", "provider_raw_response")):
        safe["hasErrorDetail"] = True
    safe["payloadKeyCount"] = len(payload)
    return safe


def _is_inline_or_remote_image(value: str) -> bool:
    source = str(value or "").strip().lower()
    return source.startswith(("http://", "https://", "data:image/"))


def _resolve_local_source(value: str, cwd: Path) -> Path:
    source = str(value or "").strip()
    if source.startswith("file://"):
        source = source[7:]
    expanded = expand_path(source)
    path = Path(expanded)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _safe_image_result_row(item: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in ("url", "path"):
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        safe[key] = "[inline-image-redacted]" if value.lower().startswith("data:image/") else value
    for key in ("kind", "format", "extension", "mimeType", "mime_type", "provider", "model"):
        token = _safe_token(item.get(key))
        if token:
            safe[key] = token
    for key in ("width", "height", "sizeBytes", "index"):
        number = _safe_int(item.get(key))
        if number is not None:
            safe[key] = number
    return safe


def _with_image_quality_evidence(
    images: Any,
    *,
    reference_images: Any = None,
) -> tuple[list[dict[str, Any]], Optional[Dict[str, Any]]]:
    inspected: list[dict[str, Any]] = []
    evidence_items: list[Dict[str, Any]] = []
    for item in images if isinstance(images, list) else []:
        if not isinstance(item, dict):
            continue
        projected = _safe_image_result_row(item)
        target = str(projected.get("url") or projected.get("path") or "").strip()
        if target and not _is_inline_or_remote_image(target):
            try:
                evidence = build_image_quality_evidence(
                    {"path": target, "kind": "image"},
                    reference_images=reference_images,
                )
            except Exception:
                evidence = None
            if evidence:
                projected["qualityEvidence"] = evidence
                evidence_items.append(evidence)
        inspected.append(projected)
    return inspected, _aggregate_image_quality_evidence(evidence_items)


def _with_image_finalization(
    images: list[dict[str, Any]],
    *,
    retry_count: int,
    max_retries: int,
) -> tuple[list[dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    evidence_items: list[Dict[str, Any]] = []
    decisions: list[Dict[str, Any]] = []
    finalized_images: list[dict[str, Any]] = []
    for item in images:
        projected = dict(item)
        evidence = projected.get("qualityEvidence") if isinstance(projected.get("qualityEvidence"), dict) else None
        if evidence:
            decision = build_image_finalization_decision(
                evidence,
                retry_count=retry_count,
                max_retries=max_retries,
            )
            annotated = attach_image_finalization_evidence(evidence, decision)
            if annotated:
                projected["qualityEvidence"] = annotated
                evidence_items.append(annotated)
            decisions.append(decision)
        finalized_images.append(projected)
    aggregate = _aggregate_image_quality_evidence(evidence_items)
    finalization = aggregate_image_finalization_decisions(decisions)
    return finalized_images, aggregate, finalization


def _aggregate_image_quality_evidence(items: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    statuses = {str(item.get("status") or "").lower() for item in items}
    status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pending" if "pending" in statuses else "pass"
    checks: list[Dict[str, Any]] = []
    for evidence in items:
        for check in evidence.get("checks") or []:
            if isinstance(check, dict):
                checks.append(dict(check))
    gates: list[str] = []
    for evidence in items:
        for gate in evidence.get("qualityGates") or []:
            text = str(gate or "").strip()
            if text and text not in gates:
                gates.append(text)
    return {
        "schemaVersion": "v0.2.4",
        "kind": "image",
        "sourceRef": items[0].get("sourceRef") or "",
        "qualityGates": gates[:40],
        "checks": checks[:48],
        "missingQualityGates": [],
        "status": status,
        "redacted": True,
    }


def _authorize_file_access(operation: str, path: Path, cwd: Path) -> tuple[bool, str]:
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        decision = get_tool_permission_broker().authorize_file_access(operation, str(path), cwd=str(cwd))
    except Exception as exc:
        return False, f"Permission broker unavailable; {operation} blocked. {exc.__class__.__name__}"
    return bool(decision.get("allowed")), str(decision.get("reason") or "")


class ImageGenTool(BaseTool):
    name: str = "imagegen"
    description: str = (
        "Generate or edit images using the built-in image-generation skill. "
        "Use for text-to-image, image-to-image, and visual asset generation requests."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image generation or edit instruction.",
            },
            "image_url": {
                "type": "string",
                "description": "Optional local path, file URL, data URL, or remote URL for image-to-image editing.",
            },
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional reference images for multi-image editing.",
            },
            "model": {
                "type": "string",
                "description": "Optional image model override.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider override, such as OpenAI, Gemini, Seedream, Qwen, MiniMax, or LinkAI.",
            },
            "size": {
                "type": "string",
                "description": "Optional size, for example 1024x1024.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Optional aspect ratio, for example 1:1, 16:9, or 9:16.",
            },
            "quality": {
                "type": "string",
                "description": "Optional quality hint.",
            },
            "output_format": {
                "type": "string",
                "description": "Optional output format, such as png, jpeg, or webp.",
            },
            "output_dir": {
                "type": "string",
                "description": "Optional output directory.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Defaults to 300.",
            },
            "quality_retry_max": {
                "type": "integer",
                "description": "Maximum post-QA image regeneration attempts. Defaults to 1 and is capped at 2.",
            },
            "action": {
                "type": "string",
                "enum": ["generate", "probe", "status"],
                "description": "Use probe/status for lightweight readiness checks; generate is the default image action.",
            },
        },
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        args = params or {}
        action = str(args.get("action") or "generate").strip().lower().replace("-", "_")
        if action in {"probe", "status"}:
            return ToolResult.success(self._probe())
        if action and action != "generate":
            return ToolResult.fail({
                "error": "unsupported imagegen action",
                "action": action,
                "allowedActions": ["generate", "probe", "status"],
                "redacted": True,
            })

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult.fail("prompt is required")

        root = _runtime_root()
        script = root / "skills" / "image-generation" / "scripts" / "generate.py"
        if not script.exists():
            return ToolResult.fail({
                "error": "image-generation script not found",
                "script": str(script),
            })

        payload: Dict[str, Any] = {"prompt": prompt}
        passthrough = (
            "model",
            "provider",
            "quality",
            "size",
            "aspect_ratio",
            "output_format",
            "output_compression",
            "background",
            "moderation",
            "ocr_brief",
        )
        for key in passthrough:
            value = args.get(key)
            if value not in (None, ""):
                payload[key] = value

        image_urls = args.get("image_urls")
        normalized_sources: list[str] = []
        if isinstance(image_urls, list) and image_urls:
            normalized_sources = [str(item) for item in image_urls if str(item or "").strip()]
        elif args.get("image_url"):
            normalized_sources = [str(args.get("image_url"))]

        authorized_sources: list[str] = []
        for source in normalized_sources:
            if _is_inline_or_remote_image(source):
                authorized_sources.append(source)
                continue
            source_path = _resolve_local_source(source, root)
            allowed, reason = _authorize_file_access("read", source_path, root)
            if not allowed:
                return ToolResult.fail({
                    "error": "image input read blocked by permissions",
                    "reason": reason,
                    "redacted": True,
                })
            authorized_sources.append(str(source_path))
        if len(authorized_sources) > 1:
            payload["image_url"] = authorized_sources
        elif authorized_sources:
            payload["image_url"] = authorized_sources[0]

        output_dir = Path(expand_path(str(args.get("output_dir") or _default_output_dir())))
        if not output_dir.is_absolute():
            output_dir = root / output_dir
        output_dir = output_dir.resolve()
        allowed, reason = _authorize_file_access("write", output_dir, root)
        if not allowed:
            return ToolResult.fail({
                "error": "image output directory blocked by permissions",
                "reason": reason,
                "redacted": True,
            })
        output_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["IMAGE_OUTPUT_DIR"] = str(output_dir)
        env = image_generation_env_with_config(env)

        max_quality_retries = _quality_retry_limit(
            args.get("quality_retry_max")
            if "quality_retry_max" in args
            else args.get("max_quality_retries")
        )
        started = time.monotonic()
        provider_total_latency_ms = 0
        quality_total_latency_ms = 0
        finalization_total_latency_ms = 0
        retry_count = 0
        current_payload = dict(payload)
        while True:
            try:
                provider_started = time.monotonic()
                provider_result = run_image_generation_payload(
                    current_payload,
                    script_path=script,
                    output_dir=output_dir,
                    env=env,
                )
                provider_total_latency_ms += int((time.monotonic() - provider_started) * 1000)
            except Exception as exc:
                logger.warning("[ImageGen] invocation failed: %s", exc)
                return ToolResult.fail({
                    "error": "image generation invocation failed",
                    "errorType": exc.__class__.__name__,
                    "redacted": True,
                })

            elapsed_ms = round((time.monotonic() - started) * 1000)
            stdout_json = provider_result.get("payload") if isinstance(provider_result.get("payload"), dict) else {}
            returncode = int(provider_result.get("returncode") or 0)
            stderr_text = str(provider_result.get("stderr") or "")

            if returncode != 0 or stdout_json.get("error"):
                return ToolResult.fail({
                    "error": "image generation failed",
                    "payload": _safe_imagegen_failure_payload(stdout_json),
                    "returncode": returncode,
                    "durationMs": elapsed_ms,
                    "stderr": _safe_text_presence(stderr_text),
                    "redacted": True,
                })

            quality_started = time.monotonic()
            images, _quality_evidence = _with_image_quality_evidence(
                stdout_json.get("images") or [],
                reference_images=authorized_sources,
            )
            quality_total_latency_ms += int((time.monotonic() - quality_started) * 1000)
            finalization_started = time.monotonic()
            images, quality_evidence, finalization = _with_image_finalization(
                images,
                retry_count=retry_count,
                max_retries=max_quality_retries,
            )
            finalization_total_latency_ms += int((time.monotonic() - finalization_started) * 1000)
            if finalization.get("status") == "retry" and retry_count < max_quality_retries:
                retry_count += 1
                current_payload = dict(payload)
                current_payload["prompt"] = f"{prompt.rstrip()}{_QUALITY_RETRY_PROMPT_SUFFIX}"
                continue

            duration_ms = round((time.monotonic() - started) * 1000)
            result = {
                "provider": _safe_token(stdout_json.get("provider")),
                "model": _safe_token(stdout_json.get("model")),
                "images": images,
                "model_fallback": _safe_token(stdout_json.get("model_fallback")),
                "attempted_provider_count": _safe_int(stdout_json.get("attempted_provider_count")),
                "durationMs": duration_ms,
                "timing": {
                    "providerTotalLatencyMs": provider_total_latency_ms,
                    "qualityTotalLatencyMs": quality_total_latency_ms,
                    "finalizationTotalLatencyMs": finalization_total_latency_ms,
                    "postprocessTotalLatencyMs": quality_total_latency_ms + finalization_total_latency_ms,
                    "totalLatencyMs": duration_ms,
                    "attemptCount": retry_count + 1,
                    "retryCount": retry_count,
                },
                "outputDir": str(output_dir),
                "stderr": _safe_text_presence(stderr_text),
                "finalization": finalization,
            }
            if quality_evidence:
                result["qualityEvidence"] = quality_evidence
            return ToolResult.success(result)

    def _probe(self) -> Dict[str, Any]:
        root = _runtime_root()
        script = root / "skills" / "image-generation" / "scripts" / "generate.py"
        provider_env = image_generation_env_with_config(os.environ.copy())
        configured_env = [
            key
            for key in (
                "OPENAI_API_KEY",
                "GEMINI_API_KEY",
                "ARK_API_KEY",
                "DASHSCOPE_API_KEY",
                "MINIMAX_API_KEY",
                "LINKAI_API_KEY",
            )
            if provider_env.get(key)
        ]
        checks = {
            "script": script.exists(),
            "providerRunner": callable(run_image_generation_payload),
            "qualityRuntime": callable(build_image_quality_evidence)
            and callable(build_image_finalization_decision)
            and callable(attach_image_finalization_evidence)
            and callable(aggregate_image_finalization_decisions),
        }
        missing = [name for name, ok in checks.items() if not ok]
        return {
            "schemaVersion": "v0.2.5",
            "status": "ready" if not missing else "missing",
            "tool": self.name,
            "scriptPresent": checks["script"],
            "qualityRuntimePresent": checks["qualityRuntime"],
            "providerConfigured": bool(configured_env),
            "configuredProviderEnvCount": len(configured_env),
            "missing": missing,
            "qualityGates": [
                "structural-image-qa",
                "vision-anomaly-qa",
                "reference-fidelity-qa",
                "retry-ledger",
            ],
            "redacted": True,
        }
