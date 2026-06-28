"""Shared CDP/browser automation diagnostics and launch helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

from common.utils import expand_path


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_CDP_USER_DATA_DIR = "~/.cow/chrome_cdp_profile"


def normalize_cdp_endpoint(value: Any = "") -> str:
    endpoint = str(value or "").strip() or DEFAULT_CDP_ENDPOINT
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    return endpoint.rstrip("/")


def cdp_version_url(endpoint: str) -> str:
    return normalize_cdp_endpoint(endpoint).rstrip("/") + "/json/version"


def cdp_status(endpoint: str, *, timeout: float = 1.5) -> Dict[str, Any]:
    endpoint = normalize_cdp_endpoint(endpoint)
    try:
        with urllib.request.urlopen(cdp_version_url(endpoint), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        return {
            "configured": True,
            "ready": bool(payload.get("webSocketDebuggerUrl") or payload.get("Browser")),
            "endpoint": endpoint,
            "browser": str(payload.get("Browser") or ""),
            "protocolVersion": str(payload.get("Protocol-Version") or payload.get("ProtocolVersion") or ""),
            "webSocketDebuggerUrl": bool(payload.get("webSocketDebuggerUrl")),
        }
    except Exception as exc:
        return {
            "configured": True,
            "ready": False,
            "endpoint": endpoint,
            "error": str(exc),
            "errorType": exc.__class__.__name__,
        }


def cdp_is_reachable(endpoint: str) -> bool:
    return bool(cdp_status(endpoint).get("ready"))


def find_chrome_executable(config: Optional[Dict[str, Any]] = None) -> str:
    config = config or {}
    configured = str(config.get("chrome_executable") or "").strip()
    candidates: List[str] = [configured] if configured else []
    if sys.platform.startswith("win"):
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates.extend([
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])
    else:
        candidates.extend([
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
        ])

    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return ""


def cdp_port(endpoint: str) -> str:
    raw = normalize_cdp_endpoint(endpoint).rsplit(":", 1)[-1].split("/", 1)[0]
    return raw if raw.isdigit() else "9222"


def cdp_user_data_dir(config: Optional[Dict[str, Any]] = None) -> str:
    config = config or {}
    return expand_path(str(config.get("cdp_user_data_dir") or DEFAULT_CDP_USER_DATA_DIR))


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def launch_cdp_browser(config: Optional[Dict[str, Any]], endpoint: str) -> subprocess.Popen:
    config = config or {}
    chrome = find_chrome_executable(config)
    if not chrome:
        raise RuntimeError("No Chrome/Edge executable found for CDP auto-launch")
    user_data_dir = cdp_user_data_dir(config)
    os.makedirs(user_data_dir, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={cdp_port(endpoint)}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_cdp_browser(config: Optional[Dict[str, Any]], endpoint: str, *, timeout_seconds: float = 8.0) -> Optional[subprocess.Popen]:
    endpoint = normalize_cdp_endpoint(endpoint)
    if cdp_is_reachable(endpoint):
        return None
    process = launch_cdp_browser(config, endpoint)
    try:
        deadline = time.time() + max(1.0, float(timeout_seconds or 8.0))
        while time.time() < deadline:
            if cdp_is_reachable(endpoint):
                return process
            time.sleep(0.25)
        raise RuntimeError(f"Chrome CDP did not become ready at {endpoint}")
    except Exception:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except Exception:
                    process.kill()
        except Exception:
            pass
        raise


def browser_automation_diagnostics(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or {}
    endpoint = normalize_cdp_endpoint(config.get("cdp_endpoint") or DEFAULT_CDP_ENDPOINT)
    status = cdp_status(endpoint)
    chrome = find_chrome_executable(config)
    fallback = config.get("cdp_fallback", True) is not False
    return {
        "mode": "cdp-first",
        "endpoint": endpoint,
        "cdp": status,
        "autoLaunch": config.get("cdp_auto_launch", True) is True,
        "fallbackEnabled": fallback,
        "fallbackAvailable": fallback and playwright_available(),
        "persistent": config.get("persistent", True) is not False,
        "chromeExecutable": chrome,
        "chromeExecutableFound": bool(chrome),
        "cdpUserDataDir": cdp_user_data_dir(config),
        "openAtFirstUse": True,
    }
