#!/usr/bin/env python3
"""Aggregate browser refresh/replay smokes into a redacted R23-16P artifact."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / name
        module_name = "ecorex_perf_" + name.replace("-", "_").replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load smoke script: {name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        with contextlib_suppress_value_error():
            sys.path.remove(str(SCRIPTS))


class contextlib_suppress_value_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return exc_type is ValueError


def _count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    try:
        return int(value)
    except Exception:
        return 0


def _bool(value: Any) -> bool:
    return bool(value)


def _duration_ms(result: dict[str, Any]) -> int:
    return int(result.get("durationMs") or result.get("duration_ms") or 0)


def _status(result: dict[str, Any]) -> str:
    return str(result.get("status") or "").upper()


def _safe_cross_talk(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    race = metrics.get("race") if isinstance(metrics.get("race"), dict) else {}
    refresh = metrics.get("refresh") if isinstance(metrics.get("refresh"), dict) else {}
    return {
        "status": _status(result),
        "durationMs": _duration_ms(result),
        "staleHistoryIgnored": _bool(race.get("staleHistoryIgnored")),
        "activeSessionContentStable": _bool(race.get("activeSessionContentStable")),
        "mismatchDiagnosticObserved": _bool(race.get("mismatchDiagnosticObserved")),
        "streamExpectedSessionObserved": _bool(race.get("streamExpectedSessionObserved")),
        "raceHistoryCallCount": _count(race.get("historyCallCount")),
        "projectionCallCount": _count(race.get("projectionCallCount")),
        "streamCallCount": _count(race.get("streamCallCount")),
        "refreshKeptCleanSession": _bool(refresh.get("refreshKeptCleanSession")),
        "backendHistoryFetched": _bool(refresh.get("backendHistoryFetched")),
        "refreshRejectedLateSession": _bool(refresh.get("refreshRejectedLateSession")),
        "refreshHistoryCallCount": _count(refresh.get("historyCallCount")),
        "consoleErrorCount": _count(result.get("consoleErrorCount")),
    }


def _safe_reconnect(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    fetches = metrics.get("fetches") if isinstance(metrics.get("fetches"), dict) else {}
    groups = {}
    duplicate_message_count = 0
    for key in ("history", "stable", "reconnect", "pollProjection", "nonSse"):
        item = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
        bot_count = _count(item.get("botCount"))
        duplicate_message_count += max(0, bot_count - 1)
        groups[key] = {
            "botCount": bot_count,
            "mediaItemCount": _count(item.get("mediaItems")),
            "eventIdPresent": bool(item.get("eventId")),
            "requestFetchCount": _count(item.get("requestFetches")),
        }
    return {
        "status": _status(result),
        "durationMs": _duration_ms(result),
        "groups": groups,
        "historyProjectionFetchCount": _count(fetches.get("historyProjectionFetches")),
        "sessionFetchCount": _count(fetches.get("sessionFetches")),
        "historyFallbackCallCount": _count(fetches.get("historyFallbackCalls")),
        "pollCallCount": _count(fetches.get("pollCalls")),
        "streamUrlCount": _count(fetches.get("streamUrls")),
        "lostStreamUrlCount": _count(fetches.get("lostStreamUrls")),
        "duplicateMessageCount": duplicate_message_count,
        "consoleErrorCount": _count(result.get("console_errors")),
    }


def _safe_history(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    fetches = metrics.get("fetches") if isinstance(metrics.get("fetches"), dict) else {}
    duplicate_message_count = 0
    pages = {}
    for key in ("pageOne", "pageTwo", "cursorDelta"):
        item = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
        bot_count = _count(item.get("botCount"))
        prompt_count = _count(item.get("promptCount"))
        duplicate_message_count += max(0, bot_count - 1)
        if prompt_count:
            duplicate_message_count += max(0, prompt_count - 1)
        pages[key] = {
            "botCount": bot_count,
            "promptCount": prompt_count,
            "eventIdPresent": bool(item.get("eventId")),
        }
    return {
        "status": _status(result),
        "durationMs": _duration_ms(result),
        "pages": pages,
        "historyProjectionFetchCount": _count(fetches.get("historyProjectionFetches")),
        "sessionFetchCount": _count(fetches.get("sessionFetches")),
        "historyFallbackCallCount": _count(fetches.get("historyFallbackCalls")),
        "duplicateMessageCount": duplicate_message_count,
        "consoleErrorCount": _count(result.get("console_errors")),
    }


def _percentile_95(values: list[int]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return round(float(statistics.quantiles(values, n=20, method="inclusive")[18]), 3)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    cross_talk_module = _load_script("smoke-web-session-cross-talk-refresh-replay.py")
    reconnect_module = _load_script("smoke-web-runtime-projection-reconnect-browser.py")
    history_module = _load_script("smoke-web-runtime-projection-history-pagination-browser.py")

    cross_talk = cross_talk_module.run_smoke(SimpleNamespace(
        app_root=args.app_root,
        headed=args.headed,
        width=args.width,
        height=args.height,
        timeout_ms=args.timeout_ms,
        screenshot="",
        artifact="",
    ))
    reconnect = reconnect_module.run_smoke(SimpleNamespace(
        headed=args.headed,
        width=args.width,
        height=args.height,
        timeout_ms=args.timeout_ms,
        screenshot="",
    ))
    history = history_module.run_smoke(SimpleNamespace(
        headed=args.headed,
        width=args.width,
        height=args.height,
        timeout_ms=args.timeout_ms,
        screenshot="",
    ))

    safe_cross_talk = _safe_cross_talk(cross_talk)
    safe_reconnect = _safe_reconnect(reconnect)
    safe_history = _safe_history(history)
    durations = [
        safe_cross_talk["durationMs"],
        safe_reconnect["durationMs"],
        safe_history["durationMs"],
    ]
    duplicate_message_count = (
        safe_reconnect["duplicateMessageCount"]
        + safe_history["duplicateMessageCount"]
    )
    total_console_errors = (
        safe_cross_talk["consoleErrorCount"]
        + safe_reconnect["consoleErrorCount"]
        + safe_history["consoleErrorCount"]
    )
    status = "pass"
    failure_codes: list[str] = []
    for name, item in (
        ("crossTalkRefresh", safe_cross_talk),
        ("runtimeProjectionReconnect", safe_reconnect),
        ("historyPagination", safe_history),
    ):
        if item.get("status") != "PASS":
            status = "fail"
            failure_codes.append(f"{name}.status")
    if duplicate_message_count:
        status = "fail"
        failure_codes.append("duplicate_message_count")
    if total_console_errors:
        status = "fail"
        failure_codes.append("console_errors")
    if not safe_cross_talk.get("backendHistoryFetched"):
        status = "fail"
        failure_codes.append("backend_history_missing")
    if not safe_cross_talk.get("refreshRejectedLateSession"):
        status = "fail"
        failure_codes.append("late_session_not_rejected")

    return {
        "version": "0.2.3",
        "slice": "R23-16P",
        "scenario": "refresh-replay",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "metrics": {
            "durationMs": round((time.time() - started) * 1000),
            "replayP95Ms": _percentile_95(durations),
            "duplicateMessageCount": duplicate_message_count,
            "duplicateArtifactCount": None,
            "duplicateArtifactCountMeasured": False,
            "latestEventIdDelta": None,
            "latestEventIdDeltaMeasured": False,
            "reconnectCount": safe_reconnect["lostStreamUrlCount"],
            "projectionCallCount": safe_cross_talk["projectionCallCount"],
            "streamCallCount": safe_cross_talk["streamCallCount"],
            "historyProjectionFetchCount": (
                safe_reconnect["historyProjectionFetchCount"]
                + safe_history["historyProjectionFetchCount"]
            ),
            "historyFallbackCallCount": (
                safe_reconnect["historyFallbackCallCount"]
                + safe_history["historyFallbackCallCount"]
            ),
            "consoleErrorCount": total_console_errors,
        },
        "scenarios": {
            "crossTalkRefresh": safe_cross_talk,
            "runtimeProjectionReconnect": safe_reconnect,
            "historyPagination": safe_history,
        },
        "failureCodes": failure_codes,
        "redaction": {
            "containsRawPrompts": False,
            "containsRawMessageBodies": False,
            "containsFullPaths": False,
            "containsSecretShapedValues": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run redacted R23-16P refresh/replay performance smoke.")
    parser.add_argument("--app-root", default="desktop/dist")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-refresh-replay.json")
    args = parser.parse_args()
    try:
        result = run_smoke(args)
        _write_json(args.output, result)
    except Exception as exc:  # pragma: no cover - script-level failure report
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
