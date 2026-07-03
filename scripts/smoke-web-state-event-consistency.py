#!/usr/bin/env python3
# encoding:utf-8
"""Smoke-test Web frontend/backend/event/toolchain consistency on a release.

The script is intentionally safe by default: it reads public/runtime state and
only mutates when --allow-mutation is set. It never prints credentials.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests


FRONTEND_MARKERS = {
    "model_policy_refresh_before_message": "refreshModelPolicy",
    "model_ready_gate_before_message": "ensureModelReady",
    "model_switch_divider": "model-switch-divider",
    "provider_logo_renderer": "provider-model-icon",
    "provider_logo_url": "assets/logos",
    "request_identity_key": "requestId",
    "phase1_sse_event_sync": "phase1StreamItem",
    "phase2_user_message_sync": "phase2EmitUserMessage",
    "tool_started_event": "tool.started",
    "run_completed_event": "run.completed",
}

SECRET_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|ark-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_-]{32,})"
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_KEY_RE.sub("[redacted]", value)
    return value


def _read_json(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _join(base_url: str, path: str) -> str:
    base = base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


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
        elapsed = round((time.perf_counter() - started) * 1000)
        text = response.text or ""
        parsed = None
        try:
            parsed = response.json()
        except Exception:
            parsed = None
        status = "ok" if response.ok else "http_error"
        if response.status_code in (401, 403):
            status = "auth_required"
        return {
            "status": status,
            "httpStatus": response.status_code,
            "latencyMs": elapsed,
            "json": _redact(parsed) if isinstance(parsed, dict) else None,
            "textPreview": _redact(text[:220].replace("\n", " ")),
        }
    except requests.Timeout:
        return {"status": "timeout", "httpStatus": 0, "latencyMs": None}
    except Exception as exc:
        return {
            "status": "transport_error",
            "httpStatus": 0,
            "latencyMs": None,
            "errorType": exc.__class__.__name__,
            "errorPreview": _redact(str(exc)[:220]),
        }


def _asset_marker_report(asset_text: str) -> Dict[str, Any]:
    markers = {
        marker_id: (needle in asset_text)
        for marker_id, needle in FRONTEND_MARKERS.items()
    }
    missing = [marker_id for marker_id, present in markers.items() if not present]
    return {
        "status": "ok" if not missing else "missing_markers",
        "markers": markers,
        "missing": missing,
    }


def _extract_asset_paths(html: str) -> List[str]:
    paths: List[str] = []
    for match in re.finditer(r"""(?:src|href)=["']([^"']+)["']""", html or "", re.I):
        value = match.group(1).strip()
        if value and (value.endswith(".js") or value.endswith(".css")):
            paths.append(value)
    return paths


def probe_frontend_assets(session: requests.Session, base_url: str, timeout: int) -> Dict[str, Any]:
    app_probe = _fetch(session, "GET", _join(base_url, "/app/"), timeout=timeout)
    html = str(app_probe.get("textPreview") or "")
    if app_probe.get("status") != "ok":
        return {
            "status": app_probe.get("status"),
            "app": app_probe,
            "assets": [],
            "markerReport": _asset_marker_report(""),
        }
    try:
        response = session.get(_join(base_url, "/app/"), timeout=timeout)
        html = response.text or ""
    except Exception:
        html = str(app_probe.get("textPreview") or "")
    asset_paths = _extract_asset_paths(html)
    assets = []
    combined_release_text = html
    for path in asset_paths[:8]:
        url = _join(base_url, path if path.startswith("/") else f"/app/{path}")
        probe = _fetch(session, "GET", url, timeout=timeout)
        item = {
            "path": path,
            "status": probe.get("status"),
            "httpStatus": probe.get("httpStatus"),
            "latencyMs": probe.get("latencyMs"),
        }
        assets.append(item)
        if (path.endswith(".js") or path.endswith(".css")) and probe.get("status") == "ok":
            try:
                combined_release_text += "\n" + (session.get(url, timeout=timeout).text or "")
            except Exception:
                pass
    marker_report = _asset_marker_report(combined_release_text)
    return {
        "status": "ok" if app_probe.get("status") == "ok" and marker_report["status"] == "ok" else "failed",
        "app": {key: app_probe.get(key) for key in ("status", "httpStatus", "latencyMs")},
        "assetCount": len(asset_paths),
        "assets": assets,
        "markerReport": marker_report,
    }


def _config_provider(config: Dict[str, Any]) -> str:
    bot_type = str(config.get("bot_type") or "").strip()
    use_linkai = bool(config.get("use_linkai"))
    if use_linkai:
        return "linkai"
    aliases = {
        "chatGPT": "openai",
        "openai": "openai",
        "deepseek": "deepseek",
        "gemini": "gemini",
        "doubao": "doubao",
        "zhipuai": "zhipu",
        "moonshot": "moonshot",
        "qianfan": "qianfan",
        "claudeapi": "claudeAPI",
    }
    return aliases.get(bot_type, bot_type or "")


def compare_model_state(models_payload: Dict[str, Any], runtime_config: Dict[str, Any]) -> Dict[str, Any]:
    capabilities = models_payload.get("capabilities") if isinstance(models_payload, dict) else {}
    chat = capabilities.get("chat") if isinstance(capabilities, dict) else {}
    image = capabilities.get("image") if isinstance(capabilities, dict) else {}
    current_provider = str(chat.get("current_provider") or "")
    current_model = str(chat.get("current_model") or "")
    expected_provider = _config_provider(runtime_config)
    expected_model = str(runtime_config.get("model") or "")
    checks = {
        "chatModelMatchesConfig": not expected_model or current_model == expected_model,
        "chatProviderMatchesConfig": not expected_provider or current_provider == expected_provider,
        "imageGenerationPinned": str(image.get("fallback_model") or image.get("current_model") or "") in {
            "",
            "gpt-image-2-pro",
        },
    }
    return {
        "status": "ok" if all(checks.values()) else "mismatch",
        "checks": checks,
        "current": {
            "provider": current_provider,
            "model": current_model,
            "imageModel": str(image.get("fallback_model") or image.get("current_model") or ""),
        },
        "expectedFromRuntimeConfig": {
            "provider": expected_provider,
            "model": expected_model,
        },
    }


def _models_current(payload: Dict[str, Any]) -> Tuple[str, str]:
    chat = ((payload.get("capabilities") or {}).get("chat") or {}) if isinstance(payload, dict) else {}
    return str(chat.get("current_provider") or ""), str(chat.get("current_model") or "")


def _probe_json_endpoint(
    session: requests.Session,
    base_url: str,
    path: str,
    timeout: int,
) -> Dict[str, Any]:
    probe = _fetch(session, "GET", _join(base_url, path), timeout=timeout)
    payload = probe.get("json") if isinstance(probe.get("json"), dict) else {}
    return {
        "path": path,
        "status": probe.get("status"),
        "httpStatus": probe.get("httpStatus"),
        "latencyMs": probe.get("latencyMs"),
        "jsonStatus": payload.get("status") if isinstance(payload, dict) else "",
        "summaryKeys": sorted(list(payload.keys()))[:24] if isinstance(payload, dict) else [],
        "payload": payload if path in {"/api/models", "/api/capabilities"} and probe.get("status") == "ok" else None,
    }


def _post_model_switch(
    session: requests.Session,
    base_url: str,
    provider: str,
    model: str,
    timeout: int,
) -> Dict[str, Any]:
    return _fetch(
        session,
        "POST",
        _join(base_url, "/api/models"),
        timeout=timeout,
        json_body={
            "action": "set_capability",
            "capability": "chat",
            "provider_id": provider,
            "model": model,
        },
    )


def _login_from_runtime_config(
    session: requests.Session,
    base_url: str,
    runtime_config: Dict[str, Any],
    timeout: int,
    email: str,
) -> Dict[str, Any]:
    password = str(runtime_config.get("web_password") or "")
    if not password:
        return {"status": "skipped", "reason": "web_password is empty"}
    result = _fetch(
        session,
        "POST",
        _join(base_url, "/auth/login"),
        timeout=timeout,
        json_body={"email": email, "password": password},
    )
    payload = result.get("json") if isinstance(result.get("json"), dict) else {}
    return {
        "status": "ok" if result.get("status") == "ok" and payload.get("status") == "success" else "failed",
        "httpStatus": result.get("httpStatus"),
        "latencyMs": result.get("latencyMs"),
        "sessionUser": ((payload.get("session") or {}).get("user") or {}).get("email") if isinstance(payload, dict) else "",
    }


def _run_message_smoke(
    session: requests.Session,
    base_url: str,
    timeout: int,
    prompt: str,
) -> Dict[str, Any]:
    session_id = f"smoke-consistency-{uuid.uuid4().hex[:12]}"
    attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
    accepted = _fetch(
        session,
        "POST",
        _join(base_url, "/message"),
        timeout=timeout,
        json_body={
            "message": prompt,
            "visible_message": prompt,
            "session_id": session_id,
            "client_attempt_id": attempt_id,
        },
    )
    payload = accepted.get("json") if isinstance(accepted.get("json"), dict) else {}
    request_id = str(payload.get("request_id") or "")
    checks = {
        "acceptedHasRequestId": bool(request_id),
        "acceptedSessionMatches": not request_id or str(payload.get("session_id") or session_id) == session_id,
    }
    projection = None
    active = None
    if request_id:
        active = _probe_json_endpoint(session, base_url, "/api/active-requests", timeout)
        projection = _probe_json_endpoint(
            session,
            base_url,
            f"/api/runtime-projection?request_id={request_id}&session_id={session_id}&include_events=1",
            timeout,
        )
        checks["projectionReadable"] = projection.get("status") == "ok"
        checks["activeRequestsReadable"] = active.get("status") == "ok"
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "sessionId": session_id,
        "requestId": request_id,
        "checks": checks,
        "accepted": {
            "status": accepted.get("status"),
            "httpStatus": accepted.get("httpStatus"),
            "latencyMs": accepted.get("latencyMs"),
            "jsonStatus": payload.get("status") if isinstance(payload, dict) else "",
            "stream": bool(payload.get("stream")) if isinstance(payload, dict) else False,
        },
        "activeRequests": active,
        "runtimeProjection": projection,
    }


def run_smoke(args: argparse.Namespace) -> Dict[str, Any]:
    session = requests.Session()
    for header in args.header or []:
        if ":" in header:
            key, value = header.split(":", 1)
            session.headers[key.strip()] = value.strip()
    if args.cookie:
        session.headers["Cookie"] = args.cookie

    runtime_config = _read_json(args.runtime_config)
    auth = None
    if args.login_from_runtime_config:
        auth = _login_from_runtime_config(
            session,
            args.base_url,
            runtime_config,
            args.timeout,
            args.login_email,
        )

    endpoints = [
        "/api/version",
        "/api/models",
        "/api/capabilities",
        "/api/tools",
        "/api/skills",
        "/api/extensions",
        "/api/active-requests",
    ]
    endpoint_probes = [
        _probe_json_endpoint(session, args.base_url, path, args.timeout)
        for path in endpoints
    ]
    by_path = {item["path"]: item for item in endpoint_probes}
    frontend = probe_frontend_assets(session, args.base_url, args.timeout)
    model_compare = None
    models_payload = by_path.get("/api/models", {}).get("payload")
    if isinstance(models_payload, dict) and runtime_config:
        model_compare = compare_model_state(models_payload, runtime_config)

    mutation = None
    if args.switch:
        if not args.allow_mutation:
            mutation = {"status": "skipped", "reason": "pass --allow-mutation to switch chat models"}
        elif not isinstance(models_payload, dict):
            mutation = {"status": "failed", "reason": "/api/models was not readable before mutation"}
        else:
            old_provider, old_model = _models_current(models_payload)
            if ":" in args.switch:
                provider, model = args.switch.split(":", 1)
            else:
                provider, model = "", args.switch
            switch_result = _post_model_switch(session, args.base_url, provider, model, args.timeout)
            after = _probe_json_endpoint(session, args.base_url, "/api/models", args.timeout)
            after_payload = after.get("payload") if isinstance(after.get("payload"), dict) else {}
            after_provider, after_model = _models_current(after_payload)
            restored = None
            if old_provider and old_model and (old_provider != after_provider or old_model != after_model):
                restored = _post_model_switch(session, args.base_url, old_provider, old_model, args.timeout)
            mutation = {
                "status": "ok" if switch_result.get("status") == "ok" and after_provider == provider and after_model == model else "failed",
                "target": {"provider": provider, "model": model},
                "before": {"provider": old_provider, "model": old_model},
                "after": {"provider": after_provider, "model": after_model},
                "switchHttp": {
                    "status": switch_result.get("status"),
                    "httpStatus": switch_result.get("httpStatus"),
                    "latencyMs": switch_result.get("latencyMs"),
                },
                "restoreHttp": (
                    {
                        "status": restored.get("status"),
                        "httpStatus": restored.get("httpStatus"),
                        "latencyMs": restored.get("latencyMs"),
                    }
                    if isinstance(restored, dict) else None
                ),
            }

    message_smoke = None
    if args.message_smoke:
        message_smoke = _run_message_smoke(
            session,
            args.base_url,
            args.timeout,
            args.message_prompt,
        )

    endpoint_failures = [
        f"{item['path']}:{item['status']}"
        for item in endpoint_probes
        if item["status"] != "ok" and (args.require_authenticated_apis or item["status"] != "auth_required")
    ]
    checks = {
        "frontendAssets": frontend.get("status") == "ok",
        "versionEndpoint": by_path.get("/api/version", {}).get("status") == "ok",
        "authenticatedApis": not endpoint_failures,
        "modelState": model_compare is None or model_compare.get("status") == "ok",
        "mutation": mutation is None or mutation.get("status") in {"ok", "skipped"},
        "messageSmoke": message_smoke is None or message_smoke.get("status") == "ok",
    }
    return {
        "schemaVersion": "ecorex.web-state-event-consistency-smoke.v1",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseUrl": args.base_url,
        "secretsRedacted": True,
        "summary": {
            "ok": all(checks.values()),
            "checks": checks,
            "endpointFailures": endpoint_failures,
        },
        "frontend": frontend,
        "auth": auth,
        "endpoints": endpoint_probes,
        "modelState": model_compare,
        "mutation": mutation,
        "messageSmoke": message_smoke,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="EcoreX Web base URL, for example https://host/ecorex-agent")
    parser.add_argument("--runtime-config", default="", help="Optional runtime config.json path for backend state comparison.")
    parser.add_argument("--output", default="", help="Optional path to write JSON results.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--header", action="append", default=[], help="Extra HTTP header, e.g. 'Authorization: Bearer ...'.")
    parser.add_argument("--cookie", default="", help="Cookie header value for authenticated smoke.")
    parser.add_argument("--login-from-runtime-config", action="store_true", help="Read web_password from --runtime-config and POST /auth/login.")
    parser.add_argument("--login-email", default="smoke@ecorex.local")
    parser.add_argument("--require-authenticated-apis", action="store_true")
    parser.add_argument("--switch", default="", help="Optional provider:model target to test a real model switch.")
    parser.add_argument("--allow-mutation", action="store_true", help="Allow --switch to mutate runtime state, then restore.")
    parser.add_argument("--message-smoke", action="store_true", help="Send one real minimal message and inspect state/projection.")
    parser.add_argument("--message-prompt", default="Reply with exactly OK.")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    payload = run_smoke(args)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
