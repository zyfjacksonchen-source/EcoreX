"""Shared CDP/browser automation diagnostics and launch helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.tool_execution_environment import ToolExecutionEnvironment
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
            dependency = ToolExecutionEnvironment(tool_name="browser", include_system_path=True).resolve_executable(candidate)
            if dependency.available:
                return dependency.path
    playwright_chromium = find_playwright_chromium_executable()
    if playwright_chromium:
        return playwright_chromium
    return ""


def _playwright_browser_roots() -> List[Path]:
    roots: List[Path] = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get("ECOREX_PLAYWRIGHT_BROWSERS_DIR")
    if configured:
        roots.append(Path(expand_path(configured)))
    state_dir = os.environ.get("ECOREX_STATE_DIR")
    if not configured and state_dir:
        roots.append(Path(expand_path(state_dir)) / "playwright-browsers")
    if state_dir or os.environ.get("ECOREX_INSTALL_ROOT"):
        return roots
    roots.append(Path(expand_path("~/.cache/ms-playwright")))
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
    elif sys.platform == "darwin":
        roots.append(Path(expand_path("~/Library/Caches/ms-playwright")))
    seen = set()
    unique: List[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def find_playwright_chromium_executable() -> str:
    patterns = {
        "win32": [
            "chromium-*/chrome-win/chrome.exe",
            "chromium-*/chrome-win*/chrome.exe",
            "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
        ],
        "darwin": [
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        ],
        "linux": [
            "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-linux*/chrome",
            "chromium_headless_shell-*/chrome-linux/headless_shell",
            "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
        ],
    }
    platform_key = "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    for root in _playwright_browser_roots():
        if not root.exists():
            continue
        for pattern in patterns[platform_key]:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate)
    return ""


def cdp_port(endpoint: str) -> str:
    raw = normalize_cdp_endpoint(endpoint).rsplit(":", 1)[-1].split("/", 1)[0]
    return raw if raw.isdigit() else "9222"


def cdp_user_data_dir(config: Optional[Dict[str, Any]] = None) -> str:
    config = config or {}
    return expand_path(str(config.get("cdp_user_data_dir") or DEFAULT_CDP_USER_DATA_DIR))


def playwright_available() -> bool:
    return ToolExecutionEnvironment(tool_name="browser").provider.resolve_python_package("playwright").available


def launch_cdp_browser(config: Optional[Dict[str, Any]], endpoint: str) -> subprocess.Popen:
    config = config or {}
    chrome = find_chrome_executable(config)
    if not chrome:
        raise RuntimeError("No Chrome/Edge/Playwright Chromium executable found for CDP auto-launch")
    user_data_dir = cdp_user_data_dir(config)
    os.makedirs(user_data_dir, exist_ok=True)
    headless_cfg = config.get("headless")
    headless = bool(headless_cfg) if headless_cfg is not None else (not sys.platform.startswith("win") and sys.platform != "darwin" and not os.environ.get("DISPLAY"))
    args = [
        chrome,
        f"--remote-debugging-port={cdp_port(endpoint)}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "about:blank",
    ]
    if headless:
        args.insert(-1, "--headless=new")
        args.insert(-1, "--no-sandbox")
    executor = ToolExecutionEnvironment(tool_name="browser")
    return executor.popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=executor.build_env(),
        allow_external_executable=True,
    )


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
    source = "missing"
    if chrome:
        normalized = chrome.replace("\\", "/").lower()
        source = "playwright-chromium" if "ms-playwright" in normalized or "playwright-browsers" in normalized else "external-browser"
    return {
        "mode": "cdp-first",
        "endpoint": endpoint,
        "cdp": status,
        "autoLaunch": config.get("cdp_auto_launch", True) is True,
        "fallbackEnabled": fallback,
        "fallbackAvailable": fallback and playwright_available(),
        "persistent": config.get("persistent", True) is not False,
        "cdpPersistSession": config.get("cdp_persist_session", True) is not False,
        "cdpKeepaliveIntervalSeconds": int(config.get("cdp_keepalive_interval") or 60),
        "idleTimeoutSeconds": int(config.get("idle_timeout") or 0),
        "chromeExecutable": chrome,
        "chromeExecutableFound": bool(chrome),
        "chromeExecutableSource": source,
        "cdpUserDataDir": cdp_user_data_dir(config),
        "openAtFirstUse": True,
    }
