#!/usr/bin/env python3
# encoding:utf-8
"""Real full-access Web/runtime toolchain smoke for EcoreX.

This script is meant to run from the deployed runtime. It logs into the Web
surface, switches permission mode to full-access, verifies discovery endpoints,
probes key runtime tools, and optionally performs real gpt-image-2-pro
generation + edit through /api/image-jobs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ark-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})", re.I)
REQUIRED_DISCOVERY_MARKERS = (
    "imagegen",
    "ocr",
    "vision",
    "browser",
    "feishu",
    "office-pdf",
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_RE.sub("[redacted]", value)
    return value


def _read_json(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_runtime_config_into_process(runtime_config_path: str) -> Dict[str, Any]:
    config_path = Path(runtime_config_path or (RUNTIME_ROOT / "config.json"))
    if not config_path.is_absolute():
        config_path = (RUNTIME_ROOT / config_path).resolve()
    root = config_path.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        os.chdir(root)
        from config import load_config

        load_config()
    except Exception as exc:
        return {"_load_error": {"type": exc.__class__.__name__, "message": str(exc)[:220]}, **_read_json(str(config_path))}
    return _read_json(str(config_path))


def _join(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _fetch(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: int,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        response = session.request(method, url, json=json_body, timeout=timeout)
        latency = round((time.perf_counter() - started) * 1000)
        try:
            payload = response.json()
        except Exception:
            payload = None
        return {
            "ok": bool(response.ok),
            "status": "ok" if response.ok else ("auth_required" if response.status_code in (401, 403) else "http_error"),
            "httpStatus": response.status_code,
            "latencyMs": latency,
            "json": _redact(payload) if isinstance(payload, dict) else None,
            "textPreview": _redact((response.text or "")[:240].replace("\n", " ")),
        }
    except requests.Timeout:
        return {"ok": False, "status": "timeout", "httpStatus": 0, "latencyMs": None}
    except Exception as exc:
        return {
            "ok": False,
            "status": "transport_error",
            "httpStatus": 0,
            "latencyMs": None,
            "errorType": exc.__class__.__name__,
            "errorPreview": _redact(str(exc)[:240]),
        }


def _login(session: requests.Session, base_url: str, config: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    password = str(config.get("web_password") or "")
    if not password:
        return {"status": "skipped", "reason": "web_password_empty"}
    result = _fetch(
        session,
        "POST",
        _join(base_url, "/auth/login"),
        timeout=timeout,
        json_body={"email": "full-access-smoke@ecorex.local", "password": password},
    )
    payload = result.get("json") if isinstance(result.get("json"), dict) else {}
    return {
        "status": "ok" if result.get("ok") and payload.get("status") == "success" else "failed",
        "httpStatus": result.get("httpStatus"),
        "latencyMs": result.get("latencyMs"),
        "sessionUser": ((payload.get("session") or {}).get("user") or {}).get("email") if isinstance(payload, dict) else "",
    }


def _set_full_access(session: requests.Session, base_url: str, timeout: int) -> Dict[str, Any]:
    result = _fetch(
        session,
        "POST",
        _join(base_url, "/api/tool-permissions"),
        timeout=timeout,
        json_body={"action": "set_mode", "mode": "full-access"},
    )
    payload = result.get("json") if isinstance(result.get("json"), dict) else {}
    return {
        "status": "ok" if result.get("ok") and payload.get("mode") == "full-access" else "failed",
        "httpStatus": result.get("httpStatus"),
        "latencyMs": result.get("latencyMs"),
        "mode": payload.get("mode") if isinstance(payload, dict) else "",
        "auditPathPresent": bool(payload.get("auditPath")) if isinstance(payload, dict) else False,
    }


def _discovery(session: requests.Session, base_url: str, timeout: int) -> Dict[str, Any]:
    endpoints = ["/api/capabilities", "/api/tools", "/api/skills", "/api/extensions", "/api/models"]
    probes: List[Dict[str, Any]] = []
    combined = ""
    for path in endpoints:
        result = _fetch(session, "GET", _join(base_url, path), timeout=timeout)
        payload = result.get("json") if isinstance(result.get("json"), dict) else {}
        combined += "\n" + json.dumps(payload, ensure_ascii=False)
        probes.append({
            "path": path,
            "status": result.get("status"),
            "httpStatus": result.get("httpStatus"),
            "latencyMs": result.get("latencyMs"),
            "topLevelKeys": sorted(list(payload.keys()))[:20] if isinstance(payload, dict) else [],
        })
    low = combined.lower()
    markers = {marker: (marker.lower() in low) for marker in REQUIRED_DISCOVERY_MARKERS}
    return {
        "status": "ok" if all(row["status"] == "ok" for row in probes) and all(markers.values()) else "failed",
        "endpoints": probes,
        "markers": markers,
    }


def _runtime_tool_probes(base_url: str, runtime_config: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    probes: Dict[str, Any] = {}
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker
        from common.tool_execution_environment import ToolExecutionEnvironment

        broker = get_tool_permission_broker()
        broker.set_mode("full-access")
        executor = ToolExecutionEnvironment(tool_name="smoke", include_system_path=True)
        python_dep = executor.resolve_python()
        probes["permissionBroker"] = broker.get_state()
        probes["python"] = {
            "available": python_dep.available,
            "source": python_dep.source,
            "pathPresent": bool(python_dep.path),
        }
        for name in ("node", "npm", "npx"):
            dep = executor.resolve_executable(name, native=True)
            probes[name] = {
                "available": dep.available,
                "source": dep.source,
                "pathPresent": bool(dep.path),
            }
            if dep.available:
                completed = executor.run_completed(
                    [dep.path, "--version"],
                    timeout=min(timeout, 20),
                    allow_external_executable=True,
                )
                probes[name]["invoked"] = completed.returncode == 0
                probes[name]["versionPreview"] = (completed.stdout or completed.stderr or "")[:80].strip()
    except Exception as exc:
        probes["runtimeError"] = {"type": exc.__class__.__name__, "message": str(exc)[:220]}

    sample_path = Path("/tmp") / f"ecorex-full-access-smoke-{uuid.uuid4().hex[:8]}.png"
    try:
        from common.tool_execution_environment import ToolExecutionEnvironment

        executor = ToolExecutionEnvironment(tool_name="smoke", include_system_path=True)
        Image = executor.import_python_module("PIL.Image")
        ImageDraw = executor.import_python_module("PIL.ImageDraw")
        image = Image.new("RGB", (420, 160), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 400, 140), outline=(255, 110, 0), width=6)
        draw.text((60, 62), "ECOREX OCR", fill=(0, 0, 0))
        image.save(sample_path)
        probes["sampleImage"] = {"created": sample_path.exists(), "pathHash": _hash_text(str(sample_path))}
    except Exception as exc:
        probes["sampleImage"] = {"created": False, "errorType": exc.__class__.__name__}

    try:
        from agent.tools.ocr.ocr import OcrTool

        result = OcrTool({"cwd": str(sample_path.parent)}).execute({
            "action": "extract_text",
            "image": str(sample_path),
            "timeout": 8,
        })
        probes["ocr"] = _tool_result_summary(result)
    except Exception as exc:
        probes["ocr"] = {"status": "error", "errorType": exc.__class__.__name__, "message": str(exc)[:220]}

    try:
        from agent.tools.vision.vision import Vision

        result = Vision({"cwd": str(sample_path.parent)}).execute({
            "image": str(sample_path),
            "question": "Briefly describe the image and any visible text.",
        })
        probes["vision"] = _tool_result_summary(result)
    except Exception as exc:
        probes["vision"] = {"status": "error", "errorType": exc.__class__.__name__, "message": str(exc)[:220]}

    try:
        from agent.tools.browser.browser_automation_service import browser_automation_diagnostics

        diagnostics = browser_automation_diagnostics((runtime_config.get("tools") or {}).get("browser") or {})
        probes["browserDiagnostics"] = _redact({
            "mode": diagnostics.get("mode"),
            "playwrightPackageAvailable": diagnostics.get("fallbackAvailable"),
            "chromeExecutableSource": diagnostics.get("chromeExecutableSource"),
            "cdpReady": ((diagnostics.get("cdp") or {}).get("ready")),
            "fallbackAvailable": diagnostics.get("fallbackAvailable"),
        })
    except Exception as exc:
        probes["browserDiagnostics"] = {"status": "error", "errorType": exc.__class__.__name__, "message": str(exc)[:220]}

    try:
        from agent.tools.browser.browser_tool import BrowserTool

        result = BrowserTool({"cdp_fallback": True, "cdp_auto_launch": True}).execute({
            "action": "navigate",
            "url": _join(base_url, "/api/version"),
            "timeout": 20000,
        })
        probes["browserInvoke"] = _tool_result_summary(result)
    except Exception as exc:
        probes["browserInvoke"] = {"status": "error", "errorType": exc.__class__.__name__, "message": str(exc)[:220]}

    try:
        sample_path.unlink(missing_ok=True)
    except Exception:
        pass
    return probes


def _tool_result_summary(result: Any) -> Dict[str, Any]:
    status = str(getattr(result, "status", "") or "")
    content = getattr(result, "content", None)
    payload = getattr(result, "result", None)
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False)
    elif payload is not None:
        text = str(payload)
    summary = {
        "status": status,
        "ok": status == "success",
        "contentPreview": text[:360],
        "payloadKeys": sorted(payload.keys())[:20] if isinstance(payload, dict) else [],
    }
    if isinstance(payload, dict) and isinstance(payload.get("ocr"), dict):
        summary["ocrStatus"] = str(payload["ocr"].get("status") or "")
        summary["ocrProvider"] = str(payload["ocr"].get("provider") or "")
        if isinstance(payload.get("text"), str):
            summary["textSample"] = payload.get("text", "")[:120]
    return _redact(summary)


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:16]


def _compact_alnum(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _image_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    dicts = list(_iter_dicts(payload))
    provider_models = []
    fallback_used = False
    for item in dicts:
        provider = str(item.get("provider") or item.get("fallback_provider") or "").strip()
        model = str(item.get("model") or item.get("resolved_model") or item.get("fallback_to_model") or "").strip()
        if provider or model:
            provider_models.append({"provider": provider, "model": model})
        if item.get("fallback_used") is True or (isinstance(item.get("model_fallback"), dict) and item["model_fallback"].get("used")):
            fallback_used = True
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    titles = [
        str(item.get("title") or item.get("fileName") or item.get("name") or "")
        for item in artifacts
        if isinstance(item, dict)
    ]
    has_openai_pro = any(
        str(item.get("provider") or "").lower() == "openai" and item.get("model") == "gpt-image-2-pro"
        for item in provider_models
    )
    return {
        "status": job.get("status") or "",
        "artifactCount": len(artifacts),
        "artifactTitles": [title for title in titles if title][:4],
        "providerModels": provider_models[-8:],
        "usedOpenAIImage2Pro": has_openai_pro,
        "fallbackUsed": fallback_used,
    }


def _image_job(
    session: requests.Session,
    base_url: str,
    timeout: int,
    *,
    prompt: str,
    operation: str,
    image_url: str = "",
) -> Dict[str, Any]:
    request_id = f"req-full-access-image-{uuid.uuid4().hex[:12]}"
    body = {
        "action": "start",
        "request_id": request_id,
        "session_id": f"session-full-access-image-{uuid.uuid4().hex[:10]}",
        "operation": operation,
        "provider": "openai",
        "model": "gpt-image-2-pro",
        "prompt": prompt,
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "quality_retry_max": 0,
        "synchronous": True,
        "include_events": True,
    }
    if image_url:
        body["image_url"] = image_url
    result = _fetch(
        session,
        "POST",
        _join(base_url, "/api/image-jobs"),
        timeout=max(timeout, 360),
        json_body=body,
    )
    payload = result.get("json") if isinstance(result.get("json"), dict) else {}
    signal = _image_signal(payload)
    return {
        "status": "ok" if result.get("ok") and signal["status"] == "completed" and signal["artifactCount"] > 0 else "failed",
        "httpStatus": result.get("httpStatus"),
        "latencyMs": result.get("latencyMs"),
        "requestIdPresent": bool(request_id),
        "signal": signal,
        "jobId": ((payload.get("job") or {}).get("job_id") if isinstance(payload, dict) else "") or "",
    }


def _local_image_job_artifact_path(title: str, runtime_config: Dict[str, Any]) -> str:
    safe_title = os.path.basename(str(title or "").strip())
    if not safe_title:
        return ""
    try:
        from common.utils import expand_path

        workspace = expand_path(str(runtime_config.get("agent_workspace") or "~/cow"))
    except Exception:
        workspace = os.path.expanduser(str(runtime_config.get("agent_workspace") or "~/cow"))
    candidate = Path(workspace) / "tmp" / "image-jobs" / safe_title
    return str(candidate) if candidate.exists() else ""


def _run_image_chain(
    session: requests.Session,
    base_url: str,
    timeout: int,
    runtime_config: Dict[str, Any],
) -> Dict[str, Any]:
    generated = _image_job(
        session,
        base_url,
        timeout,
        prompt=(
            "Create a simple clean square icon: a bright orange letter X centered "
            "on a white background, flat vector-like style, no extra text."
        ),
        operation="generate",
    )
    edit_url = ""
    edit_input = ""
    edit_input_source = ""
    titles = ((generated.get("signal") or {}).get("artifactTitles") or [])
    if titles:
        local_path = _local_image_job_artifact_path(str(titles[0]), runtime_config)
        if local_path:
            edit_input = local_path
            edit_input_source = "server_local_image_job_artifact_path"
        else:
            edit_url = _join(base_url, f"/uploads/image-jobs/{titles[0]}")
            try:
                response = session.get(edit_url, timeout=timeout)
                if response.ok and response.content:
                    content_type = response.headers.get("Content-Type") or "image/png"
                    if "image/" not in content_type:
                        content_type = "image/png"
                    encoded = base64.b64encode(response.content).decode("ascii")
                    edit_input = f"data:{content_type.split(';', 1)[0]};base64,{encoded}"
                    edit_input_source = "authenticated_upload_fetch_data_url"
            except Exception:
                edit_input = ""
    edited = {"status": "skipped", "reason": "no generated artifact title"}
    if edit_input:
        edited = _image_job(
            session,
            base_url,
            timeout,
            prompt=(
                "Edit the provided image by adding a thin blue circular border "
                "around the orange X. Keep the white background."
            ),
            operation="edit",
            image_url=edit_input,
        )
    elif edit_url:
        edited = {"status": "failed", "reason": "generated artifact could not be fetched for edit input"}
    checks = {
        "generateCompleted": generated.get("status") == "ok",
        "generateOpenAIImage2Pro": (generated.get("signal") or {}).get("usedOpenAIImage2Pro") is True,
        "generateNoModelFallback": (generated.get("signal") or {}).get("fallbackUsed") is False,
        "editCompleted": edited.get("status") == "ok",
        "editOpenAIImage2Pro": (edited.get("signal") or {}).get("usedOpenAIImage2Pro") is True,
        "editNoModelFallback": (edited.get("signal") or {}).get("fallbackUsed") is False,
    }
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "checks": checks,
        "generate": generated,
        "edit": edited,
        "editInputUrlUsed": bool(edit_url),
        "editInputSource": edit_input_source,
    }


def run(args: argparse.Namespace) -> Dict[str, Any]:
    config = _load_runtime_config_into_process(args.runtime_config)
    session = requests.Session()
    auth = _login(session, args.base_url, config, args.timeout)
    permission = _set_full_access(session, args.base_url, args.timeout)
    discovery = _discovery(session, args.base_url, args.timeout)
    runtime_probes = _runtime_tool_probes(args.base_url, config, args.timeout)
    image_chain = {"status": "skipped", "reason": "pass --run-real-imagegen"}
    if args.run_real_imagegen:
        image_chain = _run_image_chain(session, args.base_url, args.timeout, config)
    ocr_probe = runtime_probes.get("ocr") if isinstance(runtime_probes.get("ocr"), dict) else {}
    ocr_text_ok = "ECOREXOCR" in _compact_alnum(str(ocr_probe.get("textSample") or ocr_probe.get("contentPreview") or ""))
    runtime_checks = {
        "python": bool((runtime_probes.get("python") or {}).get("available")),
        "node": bool((runtime_probes.get("node") or {}).get("available") and (runtime_probes.get("node") or {}).get("invoked")),
        "npm": bool((runtime_probes.get("npm") or {}).get("available") and (runtime_probes.get("npm") or {}).get("invoked")),
        "npx": bool((runtime_probes.get("npx") or {}).get("available") and (runtime_probes.get("npx") or {}).get("invoked")),
        "ocr": bool(
            ocr_probe.get("ok")
            and str(ocr_probe.get("ocrStatus") or "").lower() == "success"
            and ocr_text_ok
        ),
        "vision": bool((runtime_probes.get("vision") or {}).get("ok")),
        "browserDiagnostic": "browserDiagnostics" in runtime_probes,
        "browserInvoke": bool((runtime_probes.get("browserInvoke") or {}).get("ok")),
    }
    checks = {
        "auth": auth.get("status") == "ok",
        "fullAccess": permission.get("status") == "ok",
        "discovery": discovery.get("status") == "ok",
        "runtime": all(runtime_checks.values()),
        "imageChain": image_chain.get("status") == "ok" if args.require_real_imagegen else image_chain.get("status") in {"ok", "skipped"},
    }
    return {
        "schemaVersion": "ecorex.full-access-toolchain-smoke.v1",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseUrl": args.base_url,
        "secretsRedacted": True,
        "summary": {
            "ok": all(checks.values()),
            "checks": checks,
            "runtimeChecks": runtime_checks,
        },
        "auth": auth,
        "permission": permission,
        "discovery": discovery,
        "runtimeProbes": _redact(runtime_probes),
        "imageChain": _redact(image_chain),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--run-real-imagegen", action="store_true")
    parser.add_argument("--require-real-imagegen", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    payload = run(args)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
