#!/usr/bin/env python3
# encoding:utf-8
"""Smoke-test configured chat model connectivity without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests


PROMPT = "Connectivity smoke test. Reply with exactly OK."


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Optional[Path]) -> str:
    if not path or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _pick(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else ""


def _classify_http(status: int, body: str) -> str:
    low = (body or "").lower()
    if status in (401, 403):
        return "auth_or_entitlement_failed"
    if status == 404 and ("model" in low or "endpoint" in low or "not found" in low):
        return "model_not_found_or_no_entitlement"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "provider_server_error"
    if status >= 400:
        return "request_failed"
    return "ok"


def _result(provider: str, model: str, status: str, ok: bool = False, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "ok": bool(ok),
        "status": status,
        "latencyMs": extra.pop("latencyMs", None),
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return payload


def _post_openai_compatible(
    provider: str,
    model: str,
    api_key: str,
    api_base: str,
    timeout: int,
    *,
    max_tokens_field: str = "max_tokens",
) -> Dict[str, Any]:
    if not api_key:
        return _result(provider, model, "credential_missing", False)
    url = f"{api_base.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        max_tokens_field: 8,
        "stream": False,
    }
    start = time.perf_counter()
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        latency_ms = round((time.perf_counter() - start) * 1000)
        body = response.text or ""
        if response.ok:
            preview = ""
            try:
                data = response.json()
                preview = str(data["choices"][0]["message"].get("content") or "")[:80]
            except Exception:
                pass
            return _result(provider, model, "ok", True, latencyMs=latency_ms, responsePreview=preview)
        return _result(
            provider,
            model,
            _classify_http(response.status_code, body),
            False,
            latencyMs=latency_ms,
            httpStatus=response.status_code,
            errorPreview=body[:260].replace("\n", " "),
        )
    except requests.Timeout:
        return _result(provider, model, "timeout", False)
    except Exception as exc:
        return _result(
            provider,
            model,
            "transport_error",
            False,
            errorType=exc.__class__.__name__,
            errorPreview=str(exc)[:220],
        )


def _post_gemini(
    model: str,
    api_key: str,
    api_base: str,
    timeout: int,
) -> Dict[str, Any]:
    if not api_key:
        return _result("gemini", model, "credential_missing", False)
    url = f"{api_base.rstrip('/')}/v1beta/models/{model}:generateContent?key={api_key}"
    start = time.perf_counter()
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
            },
            timeout=timeout,
        )
        latency_ms = round((time.perf_counter() - start) * 1000)
        body = response.text or ""
        if response.ok:
            preview = ""
            try:
                data = response.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                preview = "".join(str(part.get("text", "")) for part in parts)[:80]
            except Exception:
                pass
            return _result("gemini", model, "ok", True, latencyMs=latency_ms, responsePreview=preview)
        return _result(
            "gemini",
            model,
            _classify_http(response.status_code, body),
            False,
            latencyMs=latency_ms,
            httpStatus=response.status_code,
            errorPreview=body[:260].replace("\n", " "),
        )
    except requests.Timeout:
        return _result("gemini", model, "timeout", False)
    except Exception as exc:
        return _result(
            "gemini",
            model,
            "transport_error",
            False,
            errorType=exc.__class__.__name__,
            errorPreview=str(exc)[:220],
        )


def _config_keys(config: Dict[str, Any], key_text: str) -> Dict[str, str]:
    enterprise_settings = _enterprise_model_settings()
    config_openai_key = str(config.get("open_ai_api_key") or "")
    enterprise_openai_key = enterprise_settings.get("open_ai_api_key", "")
    if config_openai_key:
        openai_key = config_openai_key
        openai_credential_source = "runtime_config"
    elif enterprise_openai_key:
        openai_key = enterprise_openai_key
        openai_credential_source = "admin_policy_cache"
    else:
        # gpt-5.5 is the canonical Admin-managed EcoreX chat model. Do not
        # let the local multi-model note file mask a missing Admin policy.
        openai_key = ""
        openai_credential_source = "missing_admin_policy_or_runtime_config"
    if config_openai_key:
        openai_base = str(config.get("open_ai_api_base") or enterprise_settings.get("open_ai_api_base") or "https://api.openai.com/v1")
    elif enterprise_openai_key:
        openai_base = str(enterprise_settings.get("open_ai_api_base") or config.get("open_ai_api_base") or "https://api.openai.com/v1")
    else:
        openai_base = str(config.get("open_ai_api_base") or enterprise_settings.get("open_ai_api_base") or "https://api.openai.com/v1")
    return {
        "openai_key": openai_key,
        "openai_base": openai_base,
        "openai_credential_source": openai_credential_source,
        "deepseek_key": config.get("deepseek_api_key") or _pick(r"(sk-[A-Za-z0-9_-]{20,})\s*deepseek", key_text),
        "deepseek_base": config.get("deepseek_api_base") or "https://api.deepseek.com/v1",
        "gemini_key": config.get("gemini_api_key") or _pick(r"(sk-[A-Za-z0-9_-]{20,})\s*\r?\n\s*https?://", key_text),
        "gemini_base": config.get("gemini_api_base") or _pick(r"\b(https?://[^\s]+)", key_text) or "https://generativelanguage.googleapis.com",
        "ark_key": config.get("ark_api_key") or _pick(r"(ark-[A-Za-z0-9_-]{20,})", key_text),
        "ark_base": config.get("ark_base_url") or "https://ark.cn-beijing.volces.com/api/v3",
    }


def _enterprise_model_policy_paths() -> List[Path]:
    paths: List[Path] = []
    explicit = os.environ.get("ECOREX_ENTERPRISE_MODEL_POLICY_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        paths.append(Path(appdata) / "ecorex-desktop" / "enterprise-model-policy.json")
    localappdata = os.environ.get("LOCALAPPDATA", "").strip()
    if localappdata:
        paths.append(Path(localappdata) / "ecorex-desktop" / "enterprise-model-policy.json")
    home = Path.home()
    paths.append(home / "Library" / "Application Support" / "ecorex-desktop" / "enterprise-model-policy.json")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    paths.append(xdg / "ecorex-desktop" / "enterprise-model-policy.json")
    return paths


def _enterprise_model_settings() -> Dict[str, str]:
    for path in _enterprise_model_policy_paths():
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not payload.get("configured"):
                continue
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                continue
            return {
                key: str(value)
                for key, value in settings.items()
                if key in {"open_ai_api_key", "open_ai_api_base", "model", "bot_type"} and value
            }
        except Exception:
            continue
    return {}


def run_smoke(config: Dict[str, Any], key_text: str, timeout: int, include_diagnostic_fallbacks: bool) -> Dict[str, Any]:
    keys = _config_keys(config, key_text)
    openai_credential_source = keys.get("openai_credential_source", "")
    checks: List[Callable[[], Dict[str, Any]]] = [
        lambda: {
            **_post_openai_compatible("openai", "gpt-5.5", keys["openai_key"], keys["openai_base"], timeout, max_tokens_field="max_completion_tokens"),
            "credentialSource": openai_credential_source,
        },
        lambda: _post_openai_compatible("deepseek", "deepseek-v4-pro", keys["deepseek_key"], keys["deepseek_base"], timeout),
        lambda: _post_gemini("gemini-3.1-pro-preview", keys["gemini_key"], keys["gemini_base"], timeout),
        lambda: _post_openai_compatible("doubao", "doubao-seed-2-0-pro-260215", keys["ark_key"], keys["ark_base"], timeout),
    ]
    diagnostic: List[Callable[[], Dict[str, Any]]] = []
    if include_diagnostic_fallbacks:
        diagnostic.append(lambda: _post_openai_compatible("doubao", "doubao-seed-2.1-pro", keys["ark_key"], keys["ark_base"], timeout))

    results = [check() for check in checks]
    diagnostic_results = [check() for check in diagnostic]
    return {
        "schemaVersion": "ecorex.chat-model-connectivity-smoke.v1",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "smokeScenario": "minimal OK chat completion smoke",
        "results": results,
        "menuModelsPass": [f"{item['provider']}:{item['model']}" for item in results if item.get("ok")],
        "menuModelsFail": [f"{item['provider']}:{item['model']}:{item['status']}" for item in results if not item.get("ok")],
        "diagnosticOnly": diagnostic_results,
        "credentialSources": {
            "openai:gpt-5.5": openai_credential_source,
        },
        "secretsRedacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", help="Path to EcoreX config.json.")
    parser.add_argument("--key-file", default="", help="Optional local key note file used only for missing config keys.")
    parser.add_argument("--output", default="", help="Optional path to write JSON results.")
    parser.add_argument("--timeout", type=int, default=45, help="HTTP timeout seconds per provider.")
    parser.add_argument("--include-diagnostic-fallbacks", action="store_true")
    args = parser.parse_args()

    config = _read_json(Path(args.config))
    key_text = _read_text(Path(args.key_file)) if args.key_file else ""
    payload = run_smoke(config, key_text, max(3, args.timeout), args.include_diagnostic_fallbacks)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if not payload["menuModelsFail"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
