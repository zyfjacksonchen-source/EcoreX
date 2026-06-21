# encoding:utf-8
"""Explicit model fallback routing helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, Iterable, List, Optional

from models.model_capabilities import infer_provider_id, normalize_model_name
from models.model_retry import build_retry_decision
from models.model_telemetry import extract_error_details, is_model_error_response


@dataclass(frozen=True)
class ModelFallbackRoute:
    model: str
    bot_type: str = ""
    provider: str = ""
    reason: str = "fallback"
    index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "bot_type": self.bot_type,
            "provider": self.provider,
            "reason": self.reason,
            "index": self.index,
        }


def configured_model_fallback_routes(
    config: Optional[Dict[str, Any]],
    *,
    primary_model: str = "",
    primary_bot_type: str = "",
) -> List[ModelFallbackRoute]:
    """Parse explicit fallback model config.

    Supported shapes:
      - ["gpt-5.4-mini", {"model": "deepseek-v4-flash", "bot_type": "deepseek"}]
      - "gpt-5.4-mini, deepseek-v4-flash"
      - {"models": [...]} or {"fallbacks": [...]}

    Simple string entries infer their provider from the model name. Use object
    entries with ``bot_type`` for custom or provider-pinned deployments.
    """
    raw = (config or {}).get("model_fallbacks")
    if raw in (None, "", False):
        raw = (config or {}).get("model_fallback_chain")
    entries = _coerce_entries(raw)
    routes: List[ModelFallbackRoute] = []
    seen = {
        _route_key(
            normalize_model_name(primary_model),
            str(primary_bot_type or "").strip(),
        ),
        _route_key(normalize_model_name(primary_model), ""),
    }

    for entry in entries:
        parsed = _parse_entry(entry)
        if not parsed:
            continue
        model = normalize_model_name(parsed.get("model"))
        if not model:
            continue
        bot_type = str(parsed.get("bot_type") or parsed.get("provider") or "").strip()
        key = _route_key(model, bot_type)
        if key in seen:
            continue
        seen.add(key)
        provider = bot_type or infer_provider_id(model, configured_bot_type="")
        routes.append(ModelFallbackRoute(
            model=model,
            bot_type=bot_type,
            provider=provider,
            reason=str(parsed.get("reason") or "fallback"),
            index=len(routes) + 1,
        ))
    return routes


def should_try_model_fallback(response: Any) -> bool:
    """Return True when an exhausted model error may safely route elsewhere."""
    if not is_model_error_response(response):
        return False
    if isinstance(response, dict) and response.get("retry_suppressed"):
        return False
    if isinstance(response, dict) and response.get("retryable") is True:
        return response.get("retry_exhausted", True) is not False
    details = extract_error_details(response if isinstance(response, dict) else {})
    decision = build_retry_decision(details, attempt=0, max_retries=1)
    return decision.retryable


def annotate_fallback_result(
    response: Dict[str, Any],
    *,
    from_route: ModelFallbackRoute,
    to_route: ModelFallbackRoute,
    exhausted: bool = False,
) -> Dict[str, Any]:
    annotated = dict(response or {})
    annotated["model_fallback"] = {
        "used": not exhausted,
        "attempted": True,
        "exhausted": bool(exhausted),
        "from_model": from_route.model,
        "from_provider": from_route.provider,
        "to_model": to_route.model,
        "to_provider": to_route.provider,
    }
    return annotated


def _coerce_entries(raw: Any) -> Iterable[Any]:
    if raw in (None, "", False):
        return []
    if isinstance(raw, dict):
        return raw.get("models") or raw.get("fallbacks") or raw.get("routes") or []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                return _coerce_entries(json.loads(text))
            except Exception:
                pass
        return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return raw
    return []


def _parse_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if isinstance(entry, str):
        return {"model": entry}
    if not isinstance(entry, dict):
        return None
    if entry.get("enabled") is False:
        return None
    model = entry.get("model") or entry.get("name")
    if not model:
        return None
    return dict(entry, model=model)


def _route_key(model: str, bot_type: str) -> str:
    return "\0".join([str(model or "").strip().lower(), str(bot_type or "").strip().lower()])
