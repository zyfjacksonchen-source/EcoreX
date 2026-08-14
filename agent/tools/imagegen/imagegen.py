"""Runtime tool wrapper for the built-in image-generation skill."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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


ManagedImageExecutor = Callable[[Dict[str, Any], Optional[str]], ToolResult]
_MANAGED_IMAGE_EXECUTOR: ContextVar[Optional[ManagedImageExecutor]] = ContextVar(
    "managed_image_executor",
    default=None,
)


def bind_managed_image_executor(executor: ManagedImageExecutor):
    return _MANAGED_IMAGE_EXECUTOR.set(executor)


def reset_managed_image_executor(token: Any) -> None:
    _MANAGED_IMAGE_EXECUTOR.reset(token)


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


def _safe_artifact_label(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.search(r"(?i)(bearer\s+|https?://|file://|data:image/|sk-[a-z0-9_-]{6,}|[a-z]:[\\/])", text):
        return ""
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-_")
    if not text:
        return ""
    return text[:limit].strip(" .-_")


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
    for key in ("title", "fileName", "file_name", "artifactNameSource", "sentAt"):
        label = _safe_artifact_label(item.get(key), limit=160)
        if label:
            safe[key] = label
    for key in ("width", "height", "sizeBytes", "index"):
        number = _safe_int(item.get(key))
        if number is not None:
            safe[key] = number
    return safe


def _image_result_path(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("path", "file_path", "filePath", "url", "output", "output_path", "outputPath"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _image_artifact_timestamp() -> tuple[str, str]:
    now = datetime.now().astimezone()
    return now.strftime("%Y%m%d-%H%M%S"), now.isoformat(timespec="seconds")


def _image_artifact_ext(path: str, fallback: Any = None) -> str:
    ext = Path(str(path or "")).suffix.lower()
    if not ext and fallback:
        ext = "." + str(fallback).strip().lower().lstrip(".")
    if not re.match(r"^\.[a-z0-9]{1,8}$", ext or ""):
        ext = ".png"
    return ext


def _image_artifact_name(
    context: Any,
    *,
    ext: str,
    artifact_ordinal: Optional[int] = None,
    total_artifacts: int = 1,
) -> tuple[str, str]:
    if not isinstance(context, dict):
        return "", ""
    summary = _safe_artifact_label(
        context.get("summary") or context.get("sessionSummary") or context.get("title") or "图片产物",
        limit=48,
    ) or "图片产物"
    timestamp, sent_at = _image_artifact_timestamp()
    suffix_parts: list[str] = []
    if artifact_ordinal is not None and total_artifacts > 1:
        suffix_parts.append(f"i{artifact_ordinal + 1:02d}")
    suffix = f"-{'-'.join(suffix_parts)}" if suffix_parts else ""
    return f"{summary}-{timestamp}{suffix}{ext}", sent_at


def _unique_image_artifact_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}-{int(time.time())}{suffix}")


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


def _imagegen_route(input_route: str = "text_to_image", runner_mode: str = "in_process") -> Dict[str, Any]:
    provider_api_route = "images.edits" if input_route == "image_edit_reference" else "images.generations"
    return {
        "schemaVersion": "imagegen-route-v1",
        "routeKind": "ecorex-native-facade",
        "executionMode": "in_process_provider_runner",
        "runnerMode": runner_mode or "in_process",
        "shellInvocation": False,
        "pythonSubprocess": False,
        "compatibilityCliFallbackUsed": False,
        "providerRuntimeModule": "skills/image-generation/scripts/generate.py",
        "providerRuntimeModuleRole": "in_process_provider_module",
        "inputRoute": input_route,
        "providerApiRoute": provider_api_route,
    }


_COW_IMAGE_TASK_PARAMS = {
    "prompt": {
        "type": "string",
        "description": "Image generation or edit instruction.",
        "minLength": 1,
        "maxLength": 20000,
    },
    "image_url": {
        "type": ["string", "array"],
        "description": (
            "One image reference or an ordered list for editing/fusion. Accepts a local "
            "path, HTTP(S) URL, attachment_id, artifact_id, or prior imagegen result URL."
        ),
        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
        "minLength": 1,
        "maxLength": 4096,
        "maxItems": 16,
    },
}


class ImageGenTool(BaseTool):
    name: str = "imagegen"
    description: str = (
        "Generate or edit images through e-Mate's fixed image-2-pro route. "
        "Use this tool for text-to-image, image edits, reference-image generation, "
        "multi-image fusion, and visual asset requests. Each call produces one independent "
        "asset or variant; for multiple outputs, make one separate imagegen call per output. "
        "If a referenced edit is rejected, keep its references and report that failure; do not "
        "retry it with a different size or as a text-only redraw. "
        "Do not pass a provider, model, output directory, timeout, or concurrency policy; "
        "the Runtime owns them. Do not replace image edits or reference-image "
        "generation with Python/PIL/HTML/SVG scripts."
    )
    params: dict = {
        "type": "object",
        "description": (
            "Codex-style image generation/edit contract for one independent output."
        ),
        "properties": _COW_IMAGE_TASK_PARAMS,
        "required": ["prompt"],
        "additionalProperties": False,
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
                "route": _imagegen_route(),
                "redacted": True,
            })

        managed_executor = _MANAGED_IMAGE_EXECUTOR.get()
        if managed_executor is not None:
            return managed_executor(
                dict(args),
                str(getattr(self, "tool_call_id", "") or "") or None,
            )

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult.fail({
                "error": "prompt is required",
                "route": _imagegen_route(),
                "redacted": True,
            })

        root = _runtime_root()
        script = root / "skills" / "image-generation" / "scripts" / "generate.py"
        if not script.exists():
            return ToolResult.fail({
                "error": "image provider runtime module not found",
                "routeStatus": "provider_runtime_module_missing",
                "providerRuntimeModule": "skills/image-generation/scripts/generate.py",
                "route": _imagegen_route(),
                "redacted": True,
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
        input_route = "image_edit_reference" if normalized_sources else "text_to_image"
        route = _imagegen_route(input_route)

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
                    "route": route,
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
                "route": route,
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
                    "route": route,
                    "redacted": True,
                })

            elapsed_ms = round((time.monotonic() - started) * 1000)
            stdout_json = provider_result.get("payload") if isinstance(provider_result.get("payload"), dict) else {}
            returncode = int(provider_result.get("returncode") or 0)
            stderr_text = str(provider_result.get("stderr") or "")
            route = _imagegen_route(input_route, str(provider_result.get("runnerMode") or "in_process"))

            if returncode != 0 or stdout_json.get("error"):
                return ToolResult.fail({
                    "error": "image generation failed",
                    "code": stdout_json.get("code") or stdout_json.get("error_code") or "",
                    "errorType": stdout_json.get("errorType") or stdout_json.get("error_type") or "",
                    "nextAction": stdout_json.get("nextAction") or stdout_json.get("next_action") or "",
                    "payload": _safe_imagegen_failure_payload(stdout_json),
                    "returncode": returncode,
                    "durationMs": elapsed_ms,
                    "stderr": _safe_text_presence(stderr_text),
                    "route": route,
                    "fallbackUsed": False,
                    "pythonFallbackUsed": False,
                    "redacted": True,
                })

            provider_images = stdout_json.get("images")
            raw_images = self._with_artifact_names(
                provider_images[:1] if isinstance(provider_images, list) else [],
                output_dir=output_dir,
                output_format=args.get("output_format"),
            )
            quality_started = time.monotonic()
            images, _quality_evidence = _with_image_quality_evidence(
                raw_images,
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
                "route": route,
                "fallbackUsed": bool(
                    stdout_json.get("model_fallback")
                    and str(stdout_json.get("model_fallback")).lower() not in {"", "none", "null"}
                ),
                "pythonFallbackUsed": False,
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

    def _with_artifact_names(
        self,
        images: Any,
        *,
        output_dir: Path,
        output_format: Any = None,
    ) -> list[Dict[str, Any]]:
        if not isinstance(images, list):
            return []
        context = getattr(self, "artifact_naming_context", None)
        if not isinstance(context, dict):
            return [dict(item) for item in images if isinstance(item, dict)]

        named: list[Dict[str, Any]] = []
        try:
            safe_output_dir = output_dir.resolve()
        except Exception:
            safe_output_dir = output_dir
        image_count = max(1, len([item for item in images if isinstance(item, dict)]) or 1)
        for local_index, item in enumerate(images):
            if not isinstance(item, dict):
                continue
            projected = dict(item)
            path = _image_result_path(projected)
            ext = _image_artifact_ext(path, output_format or projected.get("format") or projected.get("extension"))
            artifact_ordinal = local_index if image_count > 1 else None
            file_name, sent_at = _image_artifact_name(
                context,
                ext=ext,
                artifact_ordinal=artifact_ordinal,
                total_artifacts=image_count,
            )
            if not file_name:
                named.append(projected)
                continue

            new_path = ""
            if path and not path.lower().startswith(("data:image/", "http://", "https://")):
                try:
                    source_path = Path(expand_path(path)).resolve()
                    if source_path.is_file() and source_path.parent == safe_output_dir:
                        target = _unique_image_artifact_target(source_path.with_name(file_name))
                        if target != source_path:
                            source_path.replace(target)
                        new_path = str(target)
                except Exception as exc:
                    logger.debug("[ImageGen] image artifact file rename skipped: %s", exc.__class__.__name__)
            if new_path:
                for key in ("url", "path", "file_path", "filePath", "output", "output_path", "outputPath"):
                    if projected.get(key):
                        projected[key] = new_path
                projected.setdefault("path", new_path)
            projected["title"] = file_name
            projected["fileName"] = file_name
            projected["file_name"] = file_name
            projected["sentAt"] = sent_at
            projected["artifactNameSource"] = "session-summary-send-time"
            projected["artifactIndex"] = local_index
            projected["artifact_index"] = local_index
            named.append(projected)
        return named

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
            "route": _imagegen_route(),
            "routeStatus": "ready" if not missing else "provider_runtime_module_missing",
            "nativeFacade": True,
            "providerRuntimeModulePresent": checks["script"],
            "providerRuntimeModuleRole": "in_process_provider_module",
            "localPythonCliRequired": False,
            "pythonSubprocess": False,
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
