#!/usr/bin/env python3
"""Redacted R23-16P Browser/CDP + OCR performance smoke."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("NODE_NO_WARNINGS", "1")
SMOKE_SALT = b"ecorex-v023-browser-ocr"
TARGET_URL = "http://xhslink.com/o/perfSmokeRedacted"
HTML = "<!doctype html><title>EcoreX Browser OCR Smoke</title><main>browser-ready</main>"
OCR_IMAGE_P95_THRESHOLD_MS = 2000


def _hash(value: str) -> str:
    return "hmac:" + hmac.new(SMOKE_SALT, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:16]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * 0.95))))
    return round(float(values[index]), 3)


def _measure(label: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        payload = fn()
        return {
            "status": "pass",
            "latencyMs": round((time.perf_counter() - started) * 1000, 3),
            **payload,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "latencyMs": round((time.perf_counter() - started) * 1000, 3),
            "failureCode": label,
            "errorType": exc.__class__.__name__,
        }


def _data_url() -> str:
    encoded = base64.b64encode(HTML.encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{encoded}"


def _make_url_image(path: Path) -> None:
    image = Image.new("RGB", (1200, 240), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    draw.text((28, 84), f"Read this link with CDP: {TARGET_URL}", fill="black", font=font)
    image.save(path)


def _make_warmup_image(path: Path) -> None:
    image = Image.new("RGB", (320, 96), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
    draw.text((16, 32), "EcoreX OCR warmup", fill="black", font=font)
    image.save(path)


def _ocr_text_url(iterations: int) -> dict[str, Any]:
    from agent.tools.ocr.ocr import OcrTool

    tool = OcrTool({"cwd": str(ROOT)})
    latencies: list[float] = []
    url_counts: list[int] = []
    next_action_ok = True
    for _ in range(max(1, iterations)):
        started = time.perf_counter()
        result = tool.execute({"action": "extract_urls", "text": f"请用 CDP 读取 {TARGET_URL}"})
        latencies.append((time.perf_counter() - started) * 1000)
        payload = result.result if result.status == "success" else {}
        url_counts.append(int(payload.get("urlCount") or 0))
        next_action = payload.get("nextAction") if isinstance(payload.get("nextAction"), dict) else {}
        next_action_ok = next_action_ok and next_action.get("tool") == "browser" and next_action.get("action") == "navigate"
    return {
        "latencyP95Ms": _p95(latencies),
        "urlCountMin": min(url_counts) if url_counts else 0,
        "nextActionBrowserNavigate": next_action_ok,
        "targetUrlHash": _hash(TARGET_URL),
    }


def _ocr_image_url(iterations: int, workspace: Path) -> dict[str, Any]:
    from agent.tools.ocr.ocr import OcrTool

    image_path = workspace / "ocr-url-input.png"
    _make_url_image(image_path)
    tool = OcrTool({"cwd": str(workspace)})
    providers = tool.execute({"action": "diagnose"}).result.get("providers", {})
    provider_available = bool(providers.get("rapidocr") or providers.get("pytesseract") or providers.get("tesseractCli"))
    if not provider_available:
        return {
            "measured": False,
            "provider": "unavailable",
            "latencyP95Ms": None,
            "urlCountMin": 0,
            "nextActionBrowserNavigate": False,
            "targetUrlHash": _hash(TARGET_URL),
        }
    warmup_path = workspace / "ocr-warmup.png"
    try:
        _make_warmup_image(warmup_path)
        tool.execute({"action": "extract_text", "image": str(warmup_path), "timeout": 2.0})
    except Exception:
        pass

    latencies: list[float] = []
    url_counts: list[int] = []
    next_action_ok = True
    provider = "local"
    for _ in range(max(1, iterations)):
        started = time.perf_counter()
        result = tool.execute({"action": "extract_urls", "image": str(image_path), "timeout": 2.0})
        latencies.append((time.perf_counter() - started) * 1000)
        payload = result.result if result.status == "success" else {}
        url_counts.append(int(payload.get("urlCount") or 0))
        ocr = payload.get("ocr") if isinstance(payload.get("ocr"), dict) else {}
        provider = str(ocr.get("provider") or provider)
        next_action = payload.get("nextAction") if isinstance(payload.get("nextAction"), dict) else {}
        next_action_ok = next_action_ok and next_action.get("tool") == "browser" and next_action.get("action") == "navigate"
    return {
        "measured": True,
        "provider": provider,
        "latencyP95Ms": _p95(latencies),
        "urlCountMin": min(url_counts) if url_counts else 0,
        "nextActionBrowserNavigate": next_action_ok,
        "targetUrlHash": _hash(TARGET_URL),
    }


def _browser_action(config: dict[str, Any], *, timeout_ms: int) -> dict[str, Any]:
    from agent.tools.browser.browser_service import BrowserService

    service = BrowserService(config)
    try:
        result = service.navigate(_data_url(), timeout=timeout_ms)
        mode = getattr(service, "_launch_mode", "")
        cdp_process = getattr(service, "_cdp_process", None)
        process_observed = cdp_process is not None
        pid_hash = _hash(str(getattr(cdp_process, "pid", ""))) if process_observed else ""
        ok = not result.get("error")
        service.close()
        process_alive_after_close = None
        if cdp_process is not None:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    if cdp_process.poll() is not None:
                        break
                except Exception:
                    break
                time.sleep(0.1)
            try:
                process_alive_after_close = cdp_process.poll() is None
            except Exception:
                process_alive_after_close = None
        return {
            "ok": bool(ok),
            "mode": str(mode or "unknown"),
            "statusCodeObserved": result.get("status") is not None,
            "autoLaunchedCdpProcessObserved": process_observed,
            "autoLaunchedCdpProcessAliveAfterClose": process_alive_after_close,
            "autoLaunchedCdpProcessHash": pid_hash,
        }
    finally:
        try:
            service.close()
        except Exception:
            pass


def _browser_cdp_first(workspace: Path, endpoint: str, timeout_ms: int) -> dict[str, Any]:
    config = {
        "cdp_endpoint": endpoint,
        "cdp_auto_launch": True,
        "cdp_fallback": True,
        "persistent": True,
        "headless": True,
        "idle_timeout": 1,
        "user_data_dir": str(workspace / "browser-persistent-profile"),
        "cdp_user_data_dir": str(workspace / "browser-cdp-profile"),
    }
    return _browser_action(config, timeout_ms=timeout_ms)


def _browser_forced_fallback(workspace: Path, timeout_ms: int) -> dict[str, Any]:
    config = {
        "cdp_endpoint": "http://127.0.0.1:9",
        "cdp_auto_launch": False,
        "cdp_fallback": True,
        "persistent": True,
        "headless": True,
        "idle_timeout": 1,
        "user_data_dir": str(workspace / "browser-fallback-profile"),
        "cdp_user_data_dir": str(workspace / "browser-unused-cdp-profile"),
    }
    return _browser_action(config, timeout_ms=timeout_ms)


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    from agent.tools.browser.browser_automation_service import browser_automation_diagnostics
    from common.log import logger

    previous_log_level = logger.level
    logger.setLevel(logging.CRITICAL)

    started = time.perf_counter()
    failure_codes: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ecorex-browser-ocr-") as raw_workspace:
            workspace = Path(raw_workspace)
            endpoint = f"http://127.0.0.1:{int(args.cdp_port)}"
            diagnostics = browser_automation_diagnostics({
                "cdp_endpoint": endpoint,
                "cdp_auto_launch": True,
                "cdp_fallback": True,
                "cdp_user_data_dir": str(workspace / "diagnostic-cdp-profile"),
            })
            text_ocr = _measure("ocr_text_url", lambda: _ocr_text_url(args.iterations))
            image_ocr = _measure("ocr_image_url", lambda: _ocr_image_url(args.iterations, workspace))
            cdp_first = _measure("browser_cdp_first", lambda: _browser_cdp_first(workspace, endpoint, args.timeout_ms))
            fallback = _measure("browser_fallback", lambda: _browser_forced_fallback(workspace, args.timeout_ms))
    finally:
        logger.setLevel(previous_log_level)

    for key, item in (
        ("ocr_text_url", text_ocr),
        ("ocr_image_url", image_ocr),
        ("browser_cdp_first", cdp_first),
        ("browser_fallback", fallback),
    ):
        if item.get("status") != "pass":
            failure_codes.append(f"{key}.failed")
    if text_ocr.get("urlCountMin", 0) < 1 or not text_ocr.get("nextActionBrowserNavigate"):
        failure_codes.append("ocr_text_url_handoff")
    image_provider = str(image_ocr.get("provider") or "")
    if image_ocr.get("measured") is not True:
        failure_codes.append("ocr_image_url_unmeasured")
    if image_provider not in {"rapidocr_onnxruntime", "rapidocr"}:
        failure_codes.append("ocr_image_url_provider_not_rapidocr")
    if image_ocr.get("measured") and (image_ocr.get("urlCountMin", 0) < 1 or not image_ocr.get("nextActionBrowserNavigate")):
        failure_codes.append("ocr_image_url_handoff")
    image_p95 = image_ocr.get("latencyP95Ms")
    if image_p95 is None or float(image_p95) > OCR_IMAGE_P95_THRESHOLD_MS:
        failure_codes.append("ocr_image_url_p95_over_threshold")
    if not cdp_first.get("ok"):
        failure_codes.append("browser_cdp_first_action")
    if cdp_first.get("mode") != "cdp":
        failure_codes.append("browser_cdp_first_mode_not_cdp")
    if cdp_first.get("autoLaunchedCdpProcessObserved") is not True:
        failure_codes.append("browser_cdp_process_not_observed")
    if cdp_first.get("autoLaunchedCdpProcessAliveAfterClose") is not False:
        failure_codes.append("browser_cdp_process_cleanup_unmeasured_or_alive")
    if not fallback.get("ok") or fallback.get("mode") != "persistent":
        failure_codes.append("browser_fallback_action")

    browser_process_measured = cdp_first.get("autoLaunchedCdpProcessAliveAfterClose") is not None
    status = "pass" if not failure_codes else "fail"
    return {
        "version": "0.2.3",
        "slice": "R23-16P",
        "scenario": "browser-ocr",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "metrics": {
            "durationMs": round((time.perf_counter() - started) * 1000),
            "browserFirstActionMs": cdp_first.get("latencyMs"),
            "browserFirstActionMode": cdp_first.get("mode"),
            "browserFallbackMs": fallback.get("latencyMs"),
            "browserFallbackMode": fallback.get("mode"),
            "ocrTextUrlP95Ms": text_ocr.get("latencyP95Ms"),
            "ocrImageUrlP95Ms": image_ocr.get("latencyP95Ms"),
            "ocrImageUrlMeasured": bool(image_ocr.get("measured")),
            "liveBrowserProcessDeltaAfterIdle": 0 if browser_process_measured and not cdp_first.get("autoLaunchedCdpProcessAliveAfterClose") else None,
            "liveBrowserProcessDeltaMeasured": browser_process_measured,
        },
        "browser": {
            "diagnostics": {
                "mode": diagnostics.get("mode"),
                "autoLaunch": bool(diagnostics.get("autoLaunch")),
                "fallbackEnabled": bool(diagnostics.get("fallbackEnabled")),
                "fallbackAvailable": bool(diagnostics.get("fallbackAvailable")),
                "chromeExecutableFound": bool(diagnostics.get("chromeExecutableFound")),
                "cdpReadyBeforeSmoke": bool((diagnostics.get("cdp") or {}).get("ready")),
                "endpointHash": _hash(endpoint),
            },
            "cdpFirst": cdp_first,
            "fallback": fallback,
        },
        "ocr": {
            "textUrl": text_ocr,
            "imageUrl": image_ocr,
        },
        "failureCodes": failure_codes,
        "redaction": {
            "containsRawUrls": False,
            "containsRawOcrText": False,
            "containsFullPaths": False,
            "containsSecretShapedValues": False,
        },
        "environment": {
            "playwrightAvailable": bool(diagnostics.get("fallbackAvailable")),
            "rapidocrAvailable": bool((((image_ocr.get("provider") or "") in {"rapidocr_onnxruntime", "rapidocr"}) or False)),
            "tesseractCliAvailable": bool(shutil.which("tesseract")),
        },
    }


def _write_json(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run R23-16P browser/CDP + OCR performance smoke.")
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-browser-ocr.json")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--cdp-port", type=int, default=9333)
    args = parser.parse_args()
    try:
        result = run_smoke(args)
        _write_json(args.output, result)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "fail", "errorType": exc.__class__.__name__}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
