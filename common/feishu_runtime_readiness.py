"""Feishu/Lark local runtime readiness helpers.

These probes are intentionally local-only: they do not validate credentials,
mint tenant tokens, call Feishu APIs, or expose raw local executable paths.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
from typing import Any, Dict, Mapping


LARK_OAPI_MODULE = "lark_oapi"
LARK_OAPI_PACKAGE = "lark-oapi"
LARK_OAPI_MIN_VERSION = "1.5.5"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4])


def _version_at_least(current: str, minimum: str) -> bool:
    current_parts = _version_tuple(current)
    minimum_parts = _version_tuple(minimum)
    if not current_parts:
        return False
    length = max(len(current_parts), len(minimum_parts))
    return current_parts + (0,) * (length - len(current_parts)) >= minimum_parts + (0,) * (length - len(minimum_parts))


def _python_executable_kind() -> str:
    executable = str(getattr(sys, "executable", "") or "").replace("\\", "/").lower()
    if "ecorex webui/runtime/python" in executable or "ecorex-runtime/python" in executable:
        return "ecorex_webui_runtime"
    if "/venv/" in executable:
        return "venv"
    return "system_or_unknown"


def probe_lark_oapi() -> Dict[str, Any]:
    """Return a secret-free local SDK probe for the active Python runtime.

    This intentionally avoids importing ``lark_oapi``. The full SDK import is
    expensive and should stay on the actual Feishu startup/register paths.
    """
    spec = importlib.util.find_spec(LARK_OAPI_MODULE)
    present = spec is not None
    version = ""
    if present:
        try:
            version = importlib.metadata.version(LARK_OAPI_PACKAGE)
        except Exception:
            version = "unknown"
    version_ok = bool(version and version != "unknown" and _version_at_least(version, LARK_OAPI_MIN_VERSION))
    return {
        "dependency": LARK_OAPI_MODULE,
        "package": LARK_OAPI_PACKAGE,
        "requiredVersion": f">={LARK_OAPI_MIN_VERSION}",
        "sdkPresent": present,
        "sdkVersion": version,
        "versionOk": version_ok,
        "registerAppAvailable": bool(present and version_ok),
        "registerAppAvailabilitySource": "metadata",
        "pythonExecutableKind": _python_executable_kind(),
    }


def feishu_dependency_status(config: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """Classify Feishu websocket dependency readiness without touching the network."""
    cfg = dict(config or {})
    mode = str(cfg.get("feishu_event_mode") or "websocket").strip().lower() or "websocket"
    credential_present = bool(str(cfg.get("feishu_app_id") or "").strip() and str(cfg.get("feishu_app_secret") or "").strip())
    sdk = probe_lark_oapi()
    required_for_transport = mode == "websocket"
    ready = (not required_for_transport) or bool(sdk.get("sdkPresent"))
    status = "ready" if ready else "missing"
    return {
        **sdk,
        "status": status,
        "mode": mode,
        "requiredForTransport": required_for_transport,
        "credentialPresent": credential_present,
        "credentialValid": "unknown",
        "remoteConnectivityProbed": False,
        "remediation": (
            "Install lark-oapi into the active EcoreX WebUI Python runtime and restart WebUI."
            if status == "missing"
            else ""
        ),
    }


def lark_oapi_available() -> bool:
    return bool(probe_lark_oapi().get("sdkPresent"))
