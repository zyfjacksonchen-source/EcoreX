from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Iterable, Tuple


_APP_ID_KEYS = ("client_id", "app_id", "clientId", "appId")
_APP_SECRET_KEYS = ("client_secret", "app_secret", "clientSecret", "appSecret")
_KNOWN_CONTAINER_KEYS = ("data", "app", "application", "credential", "credentials", "result")


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _lookup_path(value: Any, path: Iterable[str]) -> Any:
    current = value
    for part in path:
        if current is None:
            return None
        current = _mapping_get(current, part)
    return current


def _clean_credential(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null"}:
        return ""
    return text


def _extract_pair_from_container(value: Any) -> Tuple[str, str]:
    if value is None:
        return "", ""
    for id_key in _APP_ID_KEYS:
        app_id = _clean_credential(_mapping_get(value, id_key))
        if not app_id:
            continue
        for secret_key in _APP_SECRET_KEYS:
            app_secret = _clean_credential(_mapping_get(value, secret_key))
            if app_secret:
                return app_id, app_secret
    return "", ""


def extract_feishu_register_credentials(result: Any) -> Tuple[str, str]:
    """Extract app credentials from lark_oapi.register_app return shapes.

    The SDK has shipped different key spellings across versions and wrappers.
    Accept both client_* and app_* names, including common nested containers.
    """

    direct = _extract_pair_from_container(result)
    if direct[0] and direct[1]:
        return direct

    candidate_paths = []
    for container in _KNOWN_CONTAINER_KEYS:
        candidate_paths.append((container,))
        for nested in _KNOWN_CONTAINER_KEYS:
            candidate_paths.append((container, nested))
    for path in candidate_paths:
        value = _lookup_path(result, path)
        pair = _extract_pair_from_container(value)
        if pair[0] and pair[1]:
            return pair

    stack = [result]
    seen = set()
    while stack:
        current = stack.pop()
        if current is None:
            continue
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        pair = _extract_pair_from_container(current)
        if pair[0] and pair[1]:
            return pair
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        else:
            for key in _KNOWN_CONTAINER_KEYS:
                nested = getattr(current, key, None)
                if nested is not None:
                    stack.append(nested)
    return "", ""


def summarize_feishu_register_result_shape(result: Any) -> Dict[str, Any]:
    """Return a secret-free summary for diagnosing register_app shape drift."""

    def keys_for(value: Any) -> list[str]:
        if isinstance(value, Mapping):
            return sorted(str(key) for key in value.keys())[:20]
        return []

    top_keys = keys_for(result)
    app_id_present, app_secret_present = False, False
    containers = []
    stack = [("", result)]
    seen = set()
    while stack:
        prefix, current = stack.pop()
        if current is None:
            continue
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        current_keys = set(keys_for(current))
        if current_keys.intersection(_APP_ID_KEYS):
            app_id_present = True
            if prefix:
                containers.append(prefix)
        if current_keys.intersection(_APP_SECRET_KEYS):
            app_secret_present = True
            if prefix:
                containers.append(prefix)
        if isinstance(current, Mapping):
            for key, value in current.items():
                if isinstance(value, (Mapping, list, tuple)):
                    child_prefix = f"{prefix}.{key}" if prefix else str(key)
                    stack.append((child_prefix, value))
        elif isinstance(current, (list, tuple)):
            for index, value in enumerate(current[:10]):
                if isinstance(value, (Mapping, list, tuple)):
                    child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
                    stack.append((child_prefix, value))
    return {
        "shape": type(result).__name__,
        "topLevelKeys": top_keys,
        "appIdFieldPresent": app_id_present,
        "appSecretFieldPresent": app_secret_present,
        "credentialContainers": sorted(set(containers))[:10],
    }
