#!/usr/bin/env python3
"""Production-grade Agent product acceptance suite for EcoreX v0.2.7.

This suite intentionally composes the existing production release checks with a
new fresh-user Agent product matrix.  The new matrix is run on the production
server through the same redacted SSH path used by the existing release scripts.

Default shape:
  - 200 existing production user-behavior checks
  - 32 existing image/OCR/vision/toolchain checks
  - 305 new Agent product checks
  = 537 total checks
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import paramiko


ROOT = Path(__file__).resolve().parents[1]
VERSION = os.environ.get("ECOREX_ACCEPTANCE_VERSION", "0.2.7")
ARTIFACT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-agent-product-acceptance.json"
REMOTE_MARKER = "__ECOREX_V027_AGENT_PRODUCT_ACCEPTANCE_JSON__"

LEGACY_USER_BEHAVIOR_SCRIPT = "smoke-v026-production-200-user-behavior.py"
LEGACY_IMAGE_TOOLCHAIN_SCRIPT = "smoke-v026-production-30-image-ocr-vision-toolchain.py"

TARGET_NEW_CHECKS = 308
TARGET_TOTAL_CHECKS = 540
MIN_ENABLED_CHECKS = 380
PRESSURE_USERS_DEFAULT = 20
PRESSURE_TURNS_DEFAULT = 3

PROVIDER_ROUTE_TARGETS = ("openai", "deepseek", "gemini", "doubao")

NEW_CASE_GROUPS = (
    ("fresh-env", 18, "P0", "low", ("fresh-user", "runtime", "first-run")),
    ("auth-first-use", 16, "P0", "low", ("auth", "first-run")),
    ("runtime-api", 30, "P0", "low", ("runtime-api", "schema")),
    ("ui-ux", 24, "P0", "low", ("ui", "ux", "browser")),
    ("stream-state-machine", 28, "P0", "model", ("streaming", "state-machine", "sse")),
    ("context-session", 20, "P0", "low", ("context", "session")),
    ("tool-skill", 26, "P0", "model", ("tools", "skills", "toolchain")),
    ("multi-model-image-route", 32, "P0", "image", ("models", "imagegen", "image-edit")),
    ("concurrency-pressure", 18, "P0", "stress", ("concurrency", "pressure", "stability")),
    ("v027-integrated-capabilities", 90, "P0", "model", ("v0.2.7", "model-switch", "cdp", "ocr", "tongxin", "imagegen", "update")),
    ("security-observability", 6, "P0", "low", ("security", "observability", "redaction")),
)

NEW_CASE_GROUP_NAMES = tuple(group for group, _count, _priority, _cost, _tags in NEW_CASE_GROUPS)
NEW_CASE_GROUP_COUNTS = {group: count for group, count, _priority, _cost, _tags in NEW_CASE_GROUPS}

FOCUSED_RERUN_DEPENDENCIES = {
    "auth-first-use": ("fresh-env",),
    "runtime-api": ("fresh-env", "auth-first-use"),
    "ui-ux": ("fresh-env", "auth-first-use"),
    "stream-state-machine": ("fresh-env", "auth-first-use"),
    "context-session": ("fresh-env", "auth-first-use", "stream-state-machine"),
    "tool-skill": ("fresh-env", "auth-first-use"),
    "multi-model-image-route": ("fresh-env", "auth-first-use"),
    "v027-integrated-capabilities": ("fresh-env", "auth-first-use", "stream-state-machine", "context-session", "tool-skill"),
    "security-observability": ("fresh-env", "auth-first-use", "stream-state-machine"),
}

REQUIRED_DOMAIN_MINIMUMS = {
    "fresh-env": 12,
    "auth-first-use": 10,
    "runtime-api": 20,
    "ui-ux": 12,
    "stream-state-machine": 12,
    "context-session": 10,
    "tool-skill": 16,
    "multi-model-image-route": 8,
    "concurrency-pressure": 10,
    "v027-integrated-capabilities": 74,
    "security-observability": 4,
}

OPTIONAL_CASE_INDICES = {
    "runtime-api": set(range(21, 31)),
    "ui-ux": set(range(16, 25)),
    "context-session": set(range(16, 21)),
    "tool-skill": set(range(21, 27)),
    "concurrency-pressure": set(range(13, 19)),
    "security-observability": set(range(5, 7)),
}

LEGACY_PRIORITY_BY_GROUP = {
    "deployment": "P0",
    "runtime-api": "P0",
    "browser-toolchain": "P0",
    "toolchain-imagegen": "P0",
    "ocr": "P0",
    "vision": "P0",
    "discovery": "P0",
    "public-http": "P1",
    "manifest": "P1",
    "downloads": "P1",
    "archive-contents": "P1",
    "v026-markers": "P1",
}

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"ark-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)(password|secret|token|api[_-]?key)(\s*[=:]\s*)[^,\s\"'}]+"),
)


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def build_declared_case_registry() -> List[Dict[str, Any]]:
    """Return the declared shape for the new 305-case Agent product matrix."""
    cases: List[Dict[str, Any]] = []
    for group, count, priority, cost, tags in NEW_CASE_GROUPS:
        for index in range(1, count + 1):
            case_id = f"v027-agent-{group}-{index:03d}"
            case_priority = "P2" if index in OPTIONAL_CASE_INDICES.get(group, set()) else priority
            cases.append(
                {
                    "id": case_id,
                    "group": group,
                    "priority": case_priority,
                    "cost": cost,
                    "tags": list(tags),
                    "enabled": True,
                    "removable": case_priority == "P2",
                    "hardGate": case_priority == "P0",
                    "skipReason": "",
                }
            )
    return cases


DECLARED_CASE_REGISTRY = build_declared_case_registry()


def expand_focus_groups(groups: Iterable[str]) -> List[str]:
    requested = {str(group).strip() for group in groups if str(group).strip()}
    expanded = set(requested)
    changed = True
    while changed:
        changed = False
        for group in list(expanded):
            for dependency in FOCUSED_RERUN_DEPENDENCIES.get(group, ()):
                if dependency not in expanded:
                    expanded.add(dependency)
                    changed = True
    return [group for group in NEW_CASE_GROUP_NAMES if group in expanded]


def parse_focus_groups(value: Optional[str]) -> List[str]:
    if not value:
        return []
    groups = [item.strip() for item in str(value).split(",") if item.strip()]
    unknown = sorted(set(groups) - set(NEW_CASE_GROUP_NAMES))
    if unknown:
        raise ValueError(f"Unknown focus group(s): {', '.join(unknown)}")
    return [group for group in NEW_CASE_GROUP_NAMES if group in set(groups)]


def _load_script(filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_").replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_deploy_module():
    return _load_script("deploy-v024-production.py")


@contextmanager
def _temporary_env(name: str, value: str):
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _extract_remote_json(stdout: str) -> Dict[str, Any]:
    index = stdout.rfind(REMOTE_MARKER)
    if index < 0:
        raise RuntimeError("Remote Agent product acceptance JSON marker missing")
    payload = stdout[index + len(REMOTE_MARKER):].strip()
    return json.loads(payload)


def _public_string(value: Any, limit: int = 3000) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s\)\]\"']+", "[URL]", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "[PATH]", text)
    text = re.sub(r"/(?:home|Users|srv|opt|tmp|var)/[^\s\"'<>]+", "[PATH]", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): public_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    if isinstance(value, str):
        return _public_string(value)
    return value


def find_redaction_violations(payload: Any) -> List[str]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    violations: List[str] = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            violations.append(pattern.pattern)
    if re.search(r"https?://[A-Za-z0-9_.:-]+", text):
        violations.append("raw-url")
    if re.search(r"[A-Za-z]:[\\/][^\s\"'<>]+", text) or re.search(r"/(?:home|Users)/[^\s\"'<>]+", text):
        violations.append("raw-user-path")
    return violations


def _redaction_scan_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "redaction":
                redaction = item if isinstance(item, dict) else {}
                out[key_text] = {
                    flag: redaction.get(flag)
                    for flag in (
                        "rawPasswordPersisted",
                        "rawSecretPersisted",
                        "rawUrlPersisted",
                        "rawUserPathPersisted",
                    )
                    if flag in redaction
                }
                continue
            out[key_text] = _redaction_scan_payload(item)
        return out
    if isinstance(value, list):
        return [_redaction_scan_payload(item) for item in value]
    return value


def _normal_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"PASS", "FAIL", "SKIP"}:
        return text
    if text in {"SUCCESS", "OK"}:
        return "PASS"
    return "FAIL"


def _normalize_check(item: Dict[str, Any], source: str, suite_index: int) -> Dict[str, Any]:
    group = str(item.get("group") or source or "unknown")
    priority = str(item.get("priority") or LEGACY_PRIORITY_BY_GROUP.get(group) or "P1")
    status = _normal_status(item.get("status"))
    check = {
        "index": suite_index,
        "source": source,
        "sourceIndex": item.get("index"),
        "caseId": item.get("caseId") or item.get("id") or f"{source}:{item.get('index', suite_index)}",
        "group": group,
        "name": str(item.get("name") or item.get("caseId") or f"check {suite_index}"),
        "status": status,
        "priority": priority,
        "cost": str(item.get("cost") or "low"),
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "enabled": bool(item.get("enabled", status != "SKIP")),
        "removable": bool(item.get("removable", priority == "P2")),
        "hardGate": bool(item.get("hardGate", priority == "P0")),
        "skipReason": str(item.get("skipReason") or ""),
        "detail": public_payload(item.get("detail") or {}),
    }
    if status == "SKIP":
        check["enabled"] = False
    return check


def normalize_checks(payloads: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for payload in payloads:
        source = str(payload.get("scope") or payload.get("source") or "unknown")
        source_checks = payload.get("checks") or []
        if not source_checks and str(payload.get("status") or "").upper() not in {"", "PASS"}:
            checks.append(_normalize_check(
                {
                    "caseId": f"{source}:payload-status",
                    "group": "release-gate",
                    "name": f"{source} produced no checks",
                    "status": "FAIL",
                    "priority": "P0",
                    "cost": "low",
                    "tags": ["release-gate", "matrix"],
                    "enabled": True,
                    "removable": False,
                    "hardGate": True,
                    "skipReason": "",
                    "detail": {
                        "status": payload.get("status"),
                        "errorType": payload.get("errorType"),
                        "remoteExitCode": payload.get("remoteExitCode"),
                        "remoteStdoutHash": payload.get("remoteStdoutHash"),
                        "remoteStderrHash": payload.get("remoteStderrHash"),
                    },
                },
                source,
                len(checks) + 1,
            ))
            continue
        for item in source_checks:
            if isinstance(item, dict):
                checks.append(_normalize_check(item, source, len(checks) + 1))
    return checks


def evaluate_quality_gates(checks: List[Dict[str, Any]], *, target_total: int = TARGET_TOTAL_CHECKS) -> Dict[str, Any]:
    enabled = [item for item in checks if item.get("enabled") is not False and item.get("status") != "SKIP"]
    failures = [item for item in checks if item.get("status") == "FAIL"]
    skipped = [item for item in checks if item.get("status") == "SKIP"]
    hard_gate_failures = [
        item
        for item in checks
        if item.get("hardGate") and item.get("status") != "PASS"
    ]
    p1_failures = [
        item
        for item in checks
        if item.get("priority") == "P1" and item.get("status") != "PASS"
    ]
    p2_failures = [
        item
        for item in checks
        if item.get("priority") == "P2" and item.get("status") == "FAIL"
    ]
    skip_without_reason = [
        item
        for item in skipped
        if not str(item.get("skipReason") or "").strip()
    ]
    domain_counts: Dict[str, int] = {}
    domain_pass_counts: Dict[str, int] = {}
    for item in checks:
        group = str(item.get("group") or "")
        if not group:
            continue
        domain_counts[group] = domain_counts.get(group, 0) + 1
        if item.get("status") == "PASS":
            domain_pass_counts[group] = domain_pass_counts.get(group, 0) + 1
    domain_gaps = [
        {"group": group, "minimum": minimum, "actual": domain_counts.get(group, 0)}
        for group, minimum in REQUIRED_DOMAIN_MINIMUMS.items()
        if domain_counts.get(group, 0) < minimum
    ]
    total_ok = len(checks) >= target_total
    enabled_ok = len(enabled) >= MIN_ENABLED_CHECKS
    status = "PASS" if (
        total_ok
        and enabled_ok
        and not failures
        and not hard_gate_failures
        and not p1_failures
        and not p2_failures
        and not skip_without_reason
        and not domain_gaps
    ) else "FAIL"
    return {
        "status": status,
        "checkCount": len(checks),
        "targetCheckCount": target_total,
        "enabledCheckCount": len(enabled),
        "passCount": sum(1 for item in checks if item.get("status") == "PASS"),
        "failCount": len(failures),
        "skipCount": len(skipped),
        "hardGateFailures": hard_gate_failures[:40],
        "p1Failures": p1_failures[:40],
        "p2Failures": p2_failures[:40],
        "skipWithoutReason": skip_without_reason[:40],
        "domainCounts": domain_counts,
        "domainPassCounts": domain_pass_counts,
        "domainGaps": domain_gaps,
        "qualityGateSummary": {
            "totalCheckCountOk": total_ok,
            "enabledCheckCountOk": enabled_ok,
            "p0PassRate": "100%" if not hard_gate_failures else "below-100%",
            "p1PassRate": "100%" if not p1_failures else "below-100%",
            "p2FailuresAllowed": False,
        },
    }


def _legacy_run_payload(filename: str) -> Dict[str, Any]:
    module = _load_script(filename)
    with _temporary_env("ECOREX_DEPLOY_VERSION", VERSION):
        payload = module.run()
    payload["sourceScript"] = filename
    return payload


REMOTE_SCRIPT = r"""
import concurrent.futures
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "__VERSION__"
BUDGET_MODE = "__BUDGET_MODE__"
PRESSURE_USERS = int("__PRESSURE_USERS__")
PRESSURE_TURNS = int("__PRESSURE_TURNS__")
LOCAL_BASE = "http://127.0.0.1:9909"
TARGET_NEW_CHECKS = 308
CHECKS = []
MODEL_ROUTE_EVIDENCE = []
STATE_MACHINE_EVIDENCE = {}
PRESSURE_EVIDENCE = {}
SECURITY_EVIDENCE = {}
VALIDATION_TMP_ROOT = Path("/srv/ecorex-agent-download/validation-tmp")
VALIDATION_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(VALIDATION_TMP_ROOT))
tempfile.tempdir = str(VALIDATION_TMP_ROOT)
TMP = Path(tempfile.mkdtemp(prefix="ecorex-agent-acceptance-", dir=str(VALIDATION_TMP_ROOT)))
RUN_ID = "agent-acceptance-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:10]
PASSWORD = ""

GROUP_META = {
    "fresh-env": {"priority": "P0", "cost": "low", "tags": ["fresh-user", "runtime", "first-run"]},
    "auth-first-use": {"priority": "P0", "cost": "low", "tags": ["auth", "first-run"]},
    "runtime-api": {"priority": "P0", "cost": "low", "tags": ["runtime-api", "schema"]},
    "ui-ux": {"priority": "P0", "cost": "low", "tags": ["ui", "ux", "browser"]},
    "stream-state-machine": {"priority": "P0", "cost": "model", "tags": ["streaming", "state-machine", "sse"]},
    "context-session": {"priority": "P0", "cost": "low", "tags": ["context", "session"]},
    "tool-skill": {"priority": "P0", "cost": "model", "tags": ["tools", "skills", "toolchain"]},
    "multi-model-image-route": {"priority": "P0", "cost": "image", "tags": ["models", "imagegen", "image-edit"]},
    "concurrency-pressure": {"priority": "P0", "cost": "stress", "tags": ["concurrency", "pressure", "stability"]},
    "v027-integrated-capabilities": {"priority": "P0", "cost": "model", "tags": ["v0.2.7", "model-switch", "cdp", "ocr", "tongxin", "imagegen", "update"]},
    "security-observability": {"priority": "P0", "cost": "low", "tags": ["security", "observability", "redaction"]},
}
GROUP_COUNTS = {
    "fresh-env": 18,
    "auth-first-use": 16,
    "runtime-api": 30,
    "ui-ux": 24,
    "stream-state-machine": 28,
    "context-session": 20,
    "tool-skill": 26,
    "multi-model-image-route": 32,
    "concurrency-pressure": 18,
    "v027-integrated-capabilities": 90,
    "security-observability": 6,
}
GROUP_ORDER = list(GROUP_META.keys())
FOCUS_GROUPS = set(json.loads(r'''__FOCUS_GROUPS__'''))
FOCUS_DEPENDENCIES = {
    "auth-first-use": ["fresh-env"],
    "runtime-api": ["fresh-env", "auth-first-use"],
    "ui-ux": ["fresh-env", "auth-first-use"],
    "stream-state-machine": ["fresh-env", "auth-first-use"],
    "context-session": ["fresh-env", "auth-first-use", "stream-state-machine"],
    "tool-skill": ["fresh-env", "auth-first-use"],
    "multi-model-image-route": ["fresh-env", "auth-first-use"],
    "v027-integrated-capabilities": ["fresh-env", "auth-first-use", "stream-state-machine", "context-session", "tool-skill"],
    "security-observability": ["fresh-env", "auth-first-use", "stream-state-machine"],
}
GROUP_COUNTERS = {}
OPTIONAL_CASE_INDICES = {
    "runtime-api": set(range(21, 31)),
    "ui-ux": set(range(16, 25)),
    "context-session": set(range(16, 21)),
    "tool-skill": set(range(21, 27)),
    "concurrency-pressure": set(range(13, 19)),
    "security-observability": set(range(5, 7)),
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"ark-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)(password|secret|token|api[_-]?key)(\s*[=:]\s*)[^,\s\"'}]+"),
]


def selected_focus_groups():
    if not FOCUS_GROUPS:
        return set(GROUP_ORDER)
    selected = set(FOCUS_GROUPS)
    changed = True
    while changed:
        changed = False
        for group in list(selected):
            for dependency in FOCUS_DEPENDENCIES.get(group, []):
                if dependency not in selected:
                    selected.add(dependency)
                    changed = True
    return selected


SELECTED_GROUPS = selected_focus_groups()
FOCUS_MODE = bool(FOCUS_GROUPS)
EXPECTED_NEW_CHECKS = sum(GROUP_COUNTS.get(group, 0) for group in SELECTED_GROUPS)


def should_run(group):
    return (not FOCUS_MODE) or group in SELECTED_GROUPS


def h(value):
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest().upper()[:16]


def public_string(value, limit=2000):
    text = str(value or "")
    if PASSWORD:
        text = text.replace(PASSWORD, "[REDACTED]")
    text = re.sub(r"https?://[^\s\)\]\"']+", "[URL]", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "[PATH]", text)
    text = re.sub(r"/(?:home|Users|srv|opt|tmp|var)/[^\s\"'<>]+", "[PATH]", text)
    text = re.sub(r"(?i)file://[^\s\"'<>]+", "[PATH]", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def public_detail(value):
    if isinstance(value, dict):
        return {str(key): public_detail(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_detail(item) for item in value[:80]]
    if isinstance(value, str):
        return public_string(value)
    return value


def add(group, name, ok=None, detail=None, *, status=None, skip_reason="", priority=None, cost=None, tags=None, removable=None, hard_gate=None):
    meta = GROUP_META.get(group, {})
    GROUP_COUNTERS[group] = GROUP_COUNTERS.get(group, 0) + 1
    case_index = GROUP_COUNTERS[group]
    resolved_status = str(status or ("PASS" if bool(ok) else "FAIL")).upper()
    if resolved_status not in {"PASS", "FAIL", "SKIP"}:
        resolved_status = "FAIL"
    resolved_priority = priority or ("P2" if case_index in OPTIONAL_CASE_INDICES.get(group, set()) else (meta.get("priority") or "P1"))
    CHECKS.append({
        "index": len(CHECKS) + 1,
        "caseId": f"v027-agent-{group}-{case_index:03d}",
        "group": group,
        "name": name,
        "status": resolved_status,
        "priority": resolved_priority,
        "cost": cost or meta.get("cost") or "low",
        "tags": list(tags or meta.get("tags") or []),
        "enabled": resolved_status != "SKIP",
        "removable": bool(resolved_priority == "P2" if removable is None else removable),
        "hardGate": bool(resolved_priority == "P0" if hard_gate is None else hard_gate),
        "skipReason": public_string(skip_reason) if resolved_status == "SKIP" else "",
        "detail": public_detail(detail or {}),
    })


def add_skip(group, name, reason, detail=None, **kwargs):
    add(group, name, status="SKIP", skip_reason=reason, detail=detail, **kwargs)


def run(args, timeout=30, cwd=None, env=None):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, cwd=cwd, env=env)
    except Exception as exc:
        class Result:
            returncode = 999
            stdout = ""
            stderr = str(exc)
        return Result()


def request(path_or_url, method="GET", data=None, opener=None, timeout=35, headers=None, read_limit=2_000_000):
    url = path_or_url if str(path_or_url).startswith(("http://", "https://")) else LOCAL_BASE + path_or_url
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if data is not None and "Content-Type" not in req.headers:
        req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        with open_fn(req, timeout=timeout) as resp:
            raw = resp.read(read_limit)
            text = raw.decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(text)
            except Exception:
                pass
            return {
                "ok": 200 <= resp.status < 400,
                "status": resp.status,
                "json": parsed,
                "text": text,
                "bytes": len(raw),
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "headers": {k.lower(): v for k, v in resp.headers.items()},
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(4000).decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            pass
        return {
            "ok": False,
            "status": exc.code,
            "json": parsed,
            "text": text,
            "bytes": len(text),
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "headers": {k.lower(): v for k, v in exc.headers.items()},
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "json": None,
            "text": str(exc)[:500],
            "bytes": 0,
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "errorType": exc.__class__.__name__,
        }


def latency_under(resp, limit_ms):
    try:
        latency = int(resp.get("latencyMs"))
    except Exception:
        return False
    return 0 <= latency < limit_ms


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_text(path, limit=2_000_000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def load_service_env():
    global PASSWORD
    env_path = Path("/etc/ecorex-web/ecorex-web.env")
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)
            if key.strip() == "WEB_PASSWORD":
                PASSWORD = value
    os.environ.setdefault("ECOREX_CONFIG_PATH", "/opt/ecorex-web/state/config.json")
    os.environ.setdefault("ECOREX_STATE_DIR", "/opt/ecorex-web/state")
    os.environ.setdefault("ECOREX_CAPABILITY_STATE_DIR", "/opt/ecorex-web/state/capability-state")
    os.environ.setdefault("ECOREX_CAPABILITY_TARGET_DIR", "/opt/ecorex-web/state/capability-packages")
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/ecorex-web/state/playwright-browsers")
    os.environ.setdefault("ECOREX_PLAYWRIGHT_BROWSERS_DIR", "/opt/ecorex-web/state/playwright-browsers")
    PASSWORD = PASSWORD or os.environ.get("WEB_PASSWORD", "")


def login_opener(email):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = request("/auth/login", method="POST", data={"email": email, "password": PASSWORD}, opener=opener, timeout=35)
    return opener, jar, login


def safe_tool_payload(result):
    payload = getattr(result, "result", result)
    status = getattr(result, "status", "")
    if isinstance(payload, dict):
        return status, payload
    return status, {"text": str(payload)[:1000]}


def phase_fresh_env():
    current = Path("/opt/ecorex-web/current")
    runtime = current / "runtime"
    release = read_json(current / "release.json")
    fresh_home = TMP / "home"
    fresh_state = TMP / "state"
    fresh_workspace = TMP / "workspace"
    fresh_profile = TMP / "browser-profile"
    for path in (fresh_home, fresh_state, fresh_workspace, fresh_profile):
        path.mkdir(parents=True, exist_ok=True)
    service_active = run(["systemctl", "is-active", "ecorex-web"], timeout=15).stdout.strip()
    add("fresh-env", "current release directory exists", current.exists())
    add("fresh-env", "runtime directory exists", runtime.exists())
    add("fresh-env", "release version is v0.2.7", release.get("version") == VERSION, {"version": release.get("version")})
    add("fresh-env", "service env file exists", Path("/etc/ecorex-web/ecorex-web.env").is_file())
    add("fresh-env", "state config exists", Path("/opt/ecorex-web/state/config.json").is_file())
    add("fresh-env", "state directory exists", Path("/opt/ecorex-web/state").is_dir())
    add("fresh-env", "workspace directory exists", Path("/srv/ecorex-agent-workspace").is_dir())
    add("fresh-env", "fresh envelope directory created", TMP.is_dir(), {"runHash": h(RUN_ID)})
    add("fresh-env", "fresh home starts without app state", not any(fresh_home.iterdir()))
    add("fresh-env", "fresh state starts empty", not any(fresh_state.iterdir()))
    add("fresh-env", "fresh workspace starts empty", not any(fresh_workspace.iterdir()))
    add("fresh-env", "fresh browser profile starts empty", not any(fresh_profile.iterdir()))
    add("fresh-env", "runtime manifest exists", (runtime / "runtime-manifest.json").is_file())
    add("fresh-env", "capability state exists", Path("/opt/ecorex-web/state/capability-state.json").is_file())
    add("fresh-env", "permission matrix exists", Path("/opt/ecorex-web/state/permission-matrix.json").is_file())
    add("fresh-env", "bundled node exists", Path("/opt/ecorex-web/node/bin/node").is_file())
    add("fresh-env", "runtime venv python exists", Path("/opt/ecorex-web/venv/bin/python").is_file())
    add("fresh-env", "systemd service active", service_active == "active", {"state": service_active})


def phase_auth_first_use():
    pre = request("/auth/check", timeout=20)
    opener, jar, login = login_opener(f"fresh-{RUN_ID}@ecorex.local")
    login_json = login.get("json") if isinstance(login.get("json"), dict) else {}
    session = login_json.get("session") if isinstance(login_json.get("session"), dict) else {}
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    post = request("/auth/check", opener=opener, timeout=20)
    perm_before = request("/api/tool-permissions", opener=opener, timeout=20)
    set_perm = request("/api/tool-permissions", method="POST", data={"action": "set_mode", "mode": "full-access"}, opener=opener, timeout=20)
    perm_after = request("/api/tool-permissions", opener=opener, timeout=20)
    sessions = request("/api/sessions?page=1&page_size=10", opener=opener, timeout=20)
    add("auth-first-use", "pre-login auth check returns 200", pre["status"] == 200)
    add("auth-first-use", "pre-login auth is required", (pre.get("json") or {}).get("auth_required") is True)
    add("auth-first-use", "pre-login is unauthenticated", (pre.get("json") or {}).get("authenticated") is False)
    add("auth-first-use", "login returns 200", login["status"] == 200)
    add("auth-first-use", "login status success", login_json.get("status") == "success")
    add("auth-first-use", "login session authenticated", session.get("authenticated") is True)
    add("auth-first-use", "login is not local fallback", session.get("localFallback") is False)
    add("auth-first-use", "login auth provider web-password", session.get("authProvider") == "web-password")
    add("auth-first-use", "login user is fresh smoke email", user.get("email") == f"fresh-{RUN_ID}@ecorex.local")
    add("auth-first-use", "login cookie persisted", len(list(jar)) > 0)
    add("auth-first-use", "post-login auth is authenticated", (post.get("json") or {}).get("authenticated") is True)
    add("auth-first-use", "tool permission endpoint returns 200 before change", perm_before["status"] == 200)
    add("auth-first-use", "set full-access returns 200", set_perm["status"] == 200)
    add("auth-first-use", "full-access mode accepted", (set_perm.get("json") or {}).get("mode") == "full-access")
    add("auth-first-use", "permission audit path present", bool((perm_after.get("json") or {}).get("auditPath")))
    add("auth-first-use", "sessions endpoint works for fresh user", sessions["status"] == 200)
    return opener


def phase_runtime_api(opener):
    endpoints = [
        "/api/version",
        "/api/models",
        "/api/tools",
        "/api/skills",
        "/api/capabilities",
        "/api/extensions",
        "/api/channels",
        "/api/external-connections",
        "/api/scheduler",
        "/api/active-requests",
        "/api/installations",
        "/api/runtime-projection",
        "/api/sessions?page=1&page_size=5",
        "/api/tool-permissions",
        "/api/update-check",
    ]
    for path in endpoints:
        resp = request(path, opener=opener, timeout=30)
        label = path.split("?", 1)[0]
        add("runtime-api", f"{label} returns 2xx", 200 <= int(resp.get("status") or 0) < 300, {"status": resp.get("status")})
        add("runtime-api", f"{label} responds under 5s", latency_under(resp, 5000), {"latencyMs": resp.get("latencyMs")})


def phase_ui_ux(opener):
    metrics = {
        "playwrightImport": False,
        "browserLaunched": False,
        "loginRequestOk": False,
        "desktopAppLoaded": False,
        "bodyTextLength": 0,
        "buttonCount": 0,
        "emptyButtonCount": 999,
        "unnamedButtonCount": 999,
        "composerPresent": False,
        "desktopHorizontalOverflow": True,
        "largeOverlapCount": 999,
        "mobileAppLoaded": False,
        "mobileHorizontalOverflow": True,
        "mobileUnnamedButtonCount": 999,
        "fatalConsoleErrors": 999,
        "appIndexHasScript": False,
        "jsAssetBytes": 0,
        "cssAssetBytes": 0,
        "namedEcoreX": False,
        "screenshotBytes": 0,
        "runtimeStatusVisible": False,
        "sessionUiVisible": False,
        "modelUiVisible": False,
        "modelButtonCount": 0,
        "modelMenuCount": 0,
        "providerLabels": [],
        "failedResponseCount": 999,
    }
    try:
        from playwright.sync_api import sync_playwright
        metrics["playwrightImport"] = True
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            metrics["browserLaunched"] = True
            context = browser.new_context(viewport={"width": 1440, "height": 980}, ignore_https_errors=True)
            context.add_init_script(script=f"window.localStorage.setItem('ecorex-release-notes-seen-version', '{VERSION}');")
            api_resp = context.request.post(LOCAL_BASE + "/auth/login", data=json.dumps({"email": f"ui-{RUN_ID}@ecorex.local", "password": PASSWORD}), headers={"Content-Type": "application/json"})
            metrics["loginRequestOk"] = api_resp.status == 200
            fatal_errors = []
            failed_responses = []
            page = context.new_page()
            def allowed_console_error(text):
                lowered = str(text or "").lower()
                return (
                    "favicon" in lowered
                    or ("failed to load resource" in lowered and ("/client/model-config" in lowered or "401" in lowered or "403" in lowered))
                )
            def on_console(msg):
                if msg.type == "error" and not allowed_console_error(msg.text):
                    fatal_errors.append(msg.text)
            def on_response(resp):
                status = int(resp.status or 0)
                if status < 400:
                    return
                url = resp.url
                lowered = url.lower()
                if "favicon" in lowered or ("/client/model-config" in lowered and status in {401, 403}):
                    return
                parsed_url = urllib.parse.urlparse(url)
                public_path = parsed_url.path
                if parsed_url.query:
                    public_path += "?" + parsed_url.query[:80]
                failed_responses.append({"status": status, "urlHash": h(url), "path": public_path[:160]})
            page.on("console", on_console)
            page.on("response", on_response)
            page.goto(LOCAL_BASE + f"/app/?release=agent-product-{RUN_ID}", wait_until="domcontentloaded", timeout=25000)
            try:
                page.wait_for_selector(".app-shell, main, body", timeout=15000)
            except Exception:
                pass
            time.sleep(1.5)
            metrics["desktopAppLoaded"] = bool(page.locator("body").count())
            metrics["bodyTextLength"] = len(page.locator("body").inner_text(timeout=5000))
            metrics["buttonCount"] = page.locator("button").count()
            button_rows = page.locator("button").evaluate_all('''
                (buttons) => buttons.map((b) => {
                  const containers = Array.from(document.querySelectorAll('.session-row, article, .composer, .chat-input, .model-selector, .sidebar, .topbar, [role="dialog"]'));
                  const r = b.getBoundingClientRect();
                  const style = window.getComputedStyle(b);
                  const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  const name = (b.innerText || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim();
                  const container = b.closest('.session-row, article, .composer, .chat-input, .model-selector, .sidebar, .topbar, [role="dialog"]');
                  const containerIndex = container ? containers.indexOf(container) : -1;
                  return { visible, name, w: r.width, h: r.height, x: r.x, y: r.y, containerIndex };
                })
            ''')
            visible_buttons = [row for row in button_rows if row.get("visible")]
            metrics["emptyButtonCount"] = sum(1 for row in visible_buttons if not str(row.get("name") or "").strip())
            metrics["unnamedButtonCount"] = metrics["emptyButtonCount"]
            metrics["composerPresent"] = page.locator("textarea, input[type='text'], [contenteditable='true']").count() > 0
            metrics["desktopHorizontalOverflow"] = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
            overlap_count = 0
            for i, a in enumerate(visible_buttons[:80]):
                for b in visible_buttons[i + 1:80]:
                    if a.get("containerIndex", -1) >= 0 and a.get("containerIndex") == b.get("containerIndex"):
                        continue
                    ax1, ay1 = a["x"], a["y"]
                    ax2, ay2 = ax1 + a["w"], ay1 + a["h"]
                    bx1, by1 = b["x"], b["y"]
                    bx2, by2 = bx1 + b["w"], by1 + b["h"]
                    x_overlap = max(0, min(ax2, bx2) - max(ax1, bx1))
                    y_overlap = max(0, min(ay2, by2) - max(ay1, by1))
                    if x_overlap * y_overlap > 48:
                        overlap_count += 1
            metrics["largeOverlapCount"] = overlap_count
            shot = page.screenshot(full_page=False)
            metrics["screenshotBytes"] = len(shot)
            text = page.locator("body").inner_text(timeout=5000)
            metrics["namedEcoreX"] = "EcoreX" in text or "Ecore" in text
            metrics["runtimeStatusVisible"] = any(token in text for token in ("运行", "runtime", "Runtime", "已连接", "模型"))
            metrics["sessionUiVisible"] = any(token in text for token in ("会话", "Session", "新建"))
            metrics["modelUiVisible"] = any(token in text for token in ("模型", "Model", "OpenAI", "DeepSeek", "Gemini"))
            model_button = page.locator("button[title^='当前模型'], button[title^='Current model'], button[aria-label*='模型'], button[aria-label*='Model']").first
            metrics["modelButtonCount"] = page.locator("button[title^='当前模型'], button[title^='Current model'], button[aria-label*='模型'], button[aria-label*='Model']").count()
            if metrics["modelButtonCount"] > 0:
                try:
                    model_button.click(timeout=12000)
                    page.wait_for_selector(".chat-model-popover, [role='menu']", timeout=12000)
                    popover = page.locator(".chat-model-popover, [role='menu']").first
                    menu_text = popover.inner_text(timeout=5000)
                    option_count = popover.locator("button").count()
                    provider_labels = [label for label in ("OpenAI", "DeepSeek", "Gemini", "豆包", "Doubao") if label in menu_text]
                    metrics["modelMenuCount"] = option_count
                    metrics["providerLabels"] = provider_labels
                    metrics["modelUiVisible"] = option_count > 1 and len(provider_labels) >= 2
                    page.keyboard.press("Escape")
                except Exception as exc:
                    metrics["modelMenuErrorType"] = exc.__class__.__name__
            page.set_viewport_size({"width": 390, "height": 844})
            time.sleep(0.8)
            metrics["mobileAppLoaded"] = metrics["bodyTextLength"] > 0 and bool(page.locator("body").count())
            metrics["mobileHorizontalOverflow"] = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
            mobile_rows = page.locator("button").evaluate_all('''
                (buttons) => buttons.map((b) => {
                  const r = b.getBoundingClientRect();
                  const style = window.getComputedStyle(b);
                  const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  const name = (b.innerText || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim();
                  return { visible, name };
                })
            ''')
            metrics["mobileUnnamedButtonCount"] = sum(1 for row in mobile_rows if row.get("visible") and not str(row.get("name") or "").strip())
            metrics["fatalConsoleErrors"] = len(fatal_errors)
            metrics["failedResponseCount"] = len(failed_responses)
            metrics["failedResponseHashes"] = failed_responses[:8]
            browser.close()
    except Exception as exc:
        metrics["uiErrorType"] = exc.__class__.__name__
        metrics["uiError"] = str(exc)[:300]
    app_index = request("/app", opener=opener, timeout=25)
    metrics["appIndexHasScript"] = "<script" in (app_index.get("text") or "")
    js_match = re.search(r'src="([^"]+\.js)"', app_index.get("text") or "")
    css_match = re.search(r'href="([^"]+\.css)"', app_index.get("text") or "")
    if js_match:
        metrics["jsAssetBytes"] = request(urllib.parse.urljoin(LOCAL_BASE + "/app/", js_match.group(1)), opener=opener, timeout=25).get("bytes") or 0
    if css_match:
        metrics["cssAssetBytes"] = request(urllib.parse.urljoin(LOCAL_BASE + "/app/", css_match.group(1)), opener=opener, timeout=25).get("bytes") or 0
    add("ui-ux", "Playwright imports in runtime", metrics["playwrightImport"], metrics)
    add("ui-ux", "Chromium launches headless", metrics["browserLaunched"], metrics)
    add("ui-ux", "browser API login succeeds", metrics["loginRequestOk"], metrics)
    add("ui-ux", "desktop app loads", metrics["desktopAppLoaded"], metrics)
    add("ui-ux", "desktop body has visible text", metrics["bodyTextLength"] > 40, metrics)
    add("ui-ux", "desktop renders interactive buttons", metrics["buttonCount"] > 0, metrics)
    add("ui-ux", "desktop has no empty visible buttons", metrics["emptyButtonCount"] == 0, metrics)
    add("ui-ux", "desktop has no unnamed visible buttons", metrics["unnamedButtonCount"] == 0, metrics)
    add("ui-ux", "composer input is visible", metrics["composerPresent"], metrics)
    add("ui-ux", "desktop has no horizontal overflow", metrics["desktopHorizontalOverflow"] is False, metrics)
    add("ui-ux", "desktop buttons do not materially overlap", metrics["largeOverlapCount"] == 0, metrics)
    add("ui-ux", "mobile app loads", metrics["mobileAppLoaded"], metrics)
    add("ui-ux", "mobile has no horizontal overflow", metrics["mobileHorizontalOverflow"] is False, metrics)
    add("ui-ux", "mobile visible buttons have names", metrics["mobileUnnamedButtonCount"] == 0, metrics)
    add("ui-ux", "browser console has no fatal UI errors", metrics["fatalConsoleErrors"] == 0 and metrics.get("failedResponseCount", 0) == 0, metrics)
    add("ui-ux", "static app index references script", metrics["appIndexHasScript"], metrics)
    add("ui-ux", "static JS asset is non-empty", metrics["jsAssetBytes"] > 1000, metrics)
    add("ui-ux", "static CSS asset is non-empty", metrics["cssAssetBytes"] > 1000, metrics)
    add("ui-ux", "UI names EcoreX", metrics["namedEcoreX"], metrics)
    add("ui-ux", "UI screenshot is nonblank", metrics["screenshotBytes"] > 10000, metrics)
    add("ui-ux", "runtime/model status is discoverable in UI", metrics["runtimeStatusVisible"], metrics)
    add("ui-ux", "session controls are discoverable in UI", metrics["sessionUiVisible"], metrics)
    add("ui-ux", "model controls are discoverable in UI", metrics["modelUiVisible"], metrics)
    add("ui-ux", "UI metrics are redacted", "password" not in json.dumps(public_detail(metrics)).lower(), metrics)


def parse_sse_events(resp, max_seconds):
    started = time.perf_counter()
    events = []
    current_id = None
    current_data = []
    first_event_ms = None
    first_content_ms = None
    terminal_ms = None
    while time.perf_counter() - started < max_seconds:
        line = resp.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        except AttributeError:
            text = str(line).rstrip("\r\n")
        if text.startswith("id:"):
            current_id = text[3:].strip()
        elif text.startswith("data:"):
            current_data.append(text[5:].strip())
        elif text == "":
            if not current_data:
                current_id = None
                continue
            raw = "\n".join(current_data)
            current_data = []
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"type": "raw", "content": raw}
            payload["_event_id"] = current_id
            current_id = None
            if first_event_ms is None:
                first_event_ms = int((time.perf_counter() - started) * 1000)
            text_content = str(payload.get("content") or payload.get("delta") or payload.get("final_text") or "")
            if text_content and first_content_ms is None:
                first_content_ms = int((time.perf_counter() - started) * 1000)
            events.append(payload)
            if payload.get("type") in {"done", "error", "cancelled", "interrupted", "replay_gap"} or payload.get("terminal"):
                terminal_ms = int((time.perf_counter() - started) * 1000)
                break
    return events, first_event_ms, first_content_ms, terminal_ms


def send_streamed_message(opener, session_id, message, timeout=160):
    post = request("/message", method="POST", data={"session_id": session_id, "message": message, "stream": True, "lang": "en"}, opener=opener, timeout=35)
    request_id = str((post.get("json") or {}).get("request_id") or "")
    events = []
    first_event_ms = None
    first_content_ms = None
    terminal_ms = None
    if request_id:
        req = urllib.request.Request(f"{LOCAL_BASE}/stream?request_id={urllib.parse.quote(request_id)}&session_id={urllib.parse.quote(session_id)}", method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                events, first_event_ms, first_content_ms, terminal_ms = parse_sse_events(resp, timeout)
        except Exception as exc:
            events = [{"type": "stream_exception", "errorType": exc.__class__.__name__, "content": str(exc)[:240]}]
    terminal_events = [item for item in events if item.get("type") in {"done", "error", "cancelled", "interrupted", "replay_gap"} or item.get("terminal")]
    content = "\n".join(str(item.get("content") or item.get("delta") or item.get("final_text") or "") for item in events)
    return {
        "post": post,
        "requestId": request_id,
        "events": events,
        "types": [str(item.get("type") or "") for item in events],
        "terminalEvents": terminal_events,
        "contentHash": h(content),
        "contentLength": len(content),
        "contentPreview": public_string(content, 240),
        "firstEventMs": first_event_ms,
        "firstContentMs": first_content_ms,
        "terminalMs": terminal_ms,
    }


def phase_stream_state_machine(opener):
    session_id = "accept-stream-" + RUN_ID
    baseline_evidence = {}
    if BUDGET_MODE == "no-external-models":
        for name in [
            "stream baseline OpenAI option configured", "stream baseline chat switch accepted",
            "stream baseline chat switch verified", "stream baseline restored original chat model",
            "message POST returns success", "request id created", "SSE first event under 5s",
            "SSE emits phase event", "SSE emits content", "SSE reaches terminal state",
            "SSE has exactly one terminal", "SSE has no error terminal", "SSE finalizes under 120s",
            "active request cleared after stream", "runtime projection available", "history persisted user",
        ]:
            add_skip("stream-state-machine", name, "budget mode disables external model calls", priority="P2", hard_gate=False)
        stream = {"requestId": "", "types": [], "terminalEvents": [], "firstEventMs": None, "terminalMs": None, "events": []}
    else:
        models_before = request("/api/models", opener=opener, timeout=30)
        models_payload = models_before.get("json") if isinstance(models_before.get("json"), dict) else {}
        options_by_provider, original_provider, original_model = model_options_by_provider(models_payload)
        openai_option = options_by_provider.get("openai") if isinstance(options_by_provider.get("openai"), dict) else {}
        target_model = str((openai_option or {}).get("model") or "").strip()
        switch = {"status": 0, "json": {}}
        verify = {"status": 0, "json": {}}
        baseline_confirmed = False
        if target_model:
            switch = request(
                "/api/models",
                method="POST",
                data={"action": "set_capability", "capability": "chat", "provider_id": "openai", "model": target_model},
                opener=opener,
                timeout=60,
            )
            verify = request("/api/models", opener=opener, timeout=30)
            verify_payload = verify.get("json") if isinstance(verify.get("json"), dict) else {}
            _verified_options, current_provider, current_model = model_options_by_provider(verify_payload)
            baseline_confirmed = current_provider == "openai" and current_model == target_model
        baseline_evidence = {
            "targetProvider": "openai",
            "targetModelHash": h(target_model),
            "originalProvider": original_provider,
            "originalModelHash": h(original_model),
            "switchStatus": switch.get("status"),
            "verifyStatus": verify.get("status"),
            "baselineConfirmed": baseline_confirmed,
        }
        add("stream-state-machine", "stream baseline OpenAI option configured", bool(target_model), baseline_evidence)
        add("stream-state-machine", "stream baseline chat switch accepted", switch.get("status") == 200 and (switch.get("json") or {}).get("status") == "success", baseline_evidence)
        add("stream-state-machine", "stream baseline chat switch verified", baseline_confirmed, baseline_evidence)
        if baseline_confirmed:
            stream = send_streamed_message(
                opener,
                session_id,
                (
                    f"Fresh isolated acceptance run {RUN_ID}. "
                    f"Reply with exactly ECX_STREAM_OK_{RUN_ID}. "
                    "Do not use stored memories or prior conversation context."
                ),
                timeout=170,
            )
        else:
            stream = {
                "post": {"status": 0, "json": {}},
                "requestId": "",
                "events": [],
                "types": [],
                "terminalEvents": [],
                "contentHash": h(""),
                "contentLength": 0,
                "firstEventMs": None,
                "firstContentMs": None,
                "terminalMs": None,
            }
        post_json = stream["post"].get("json") if isinstance(stream["post"].get("json"), dict) else {}
        terminal_types = [str(item.get("type") or "") for item in stream["terminalEvents"]]
        active = request("/api/active-requests", opener=opener, timeout=30)
        projection = request(f"/api/runtime-projection?request_id={urllib.parse.quote(stream['requestId'])}&session_id={urllib.parse.quote(session_id)}&include_events=1", opener=opener, timeout=30)
        history = request(f"/api/history?session_id={urllib.parse.quote(session_id)}&page=1&page_size=20", opener=opener, timeout=30)
        active_requests = (active.get("json") or {}).get("requests") or []
        add("stream-state-machine", "message POST returns success", stream["post"]["status"] == 200 and post_json.get("status") == "success", {"requestHash": h(stream["requestId"]), "http": stream["post"]["status"]})
        add("stream-state-machine", "request id created", bool(stream["requestId"]), {"requestHash": h(stream["requestId"])})
        add("stream-state-machine", "SSE first event under 5s", stream["firstEventMs"] is not None and stream["firstEventMs"] < 5000, {"firstEventMs": stream["firstEventMs"]})
        add("stream-state-machine", "SSE emits phase event", "phase" in stream["types"], {"types": stream["types"][:20]})
        add("stream-state-machine", "SSE emits content", stream["contentLength"] > 0 or any(t in stream["types"] for t in ("delta", "message_update", "done")), {"contentLength": stream["contentLength"], "contentHash": stream["contentHash"]})
        add("stream-state-machine", "SSE reaches terminal state", bool(stream["terminalEvents"]), {"terminalTypes": terminal_types})
        add("stream-state-machine", "SSE has exactly one terminal", len(stream["terminalEvents"]) == 1, {"terminalTypes": terminal_types})
        add("stream-state-machine", "SSE has no error terminal", not any(t in {"error", "interrupted", "replay_gap"} for t in terminal_types), {"terminalTypes": terminal_types})
        add("stream-state-machine", "SSE finalizes under 120s", stream["terminalMs"] is not None and stream["terminalMs"] < 120000, {"terminalMs": stream["terminalMs"]})
        add("stream-state-machine", "active request cleared after stream", all(str(row.get("request_id") or "") != stream["requestId"] for row in active_requests), {"activeCount": len(active_requests)})
        add("stream-state-machine", "runtime projection available", projection["status"] == 200 and (projection.get("json") or {}).get("status") == "success", {"requestHash": h(stream["requestId"])})
        add("stream-state-machine", "history persisted user", history["status"] == 200 and any((row.get("role") == "user") for row in ((history.get("json") or {}).get("messages") or [])), {"sessionHash": h(session_id)})
        restore_confirmed = True
        restore = {"status": 0, "json": {}}
        if original_provider and original_model:
            restore = request(
                "/api/models",
                method="POST",
                data={"action": "set_capability", "capability": "chat", "provider_id": original_provider, "model": original_model},
                opener=opener,
                timeout=60,
            )
            restore_verify = request("/api/models", opener=opener, timeout=30)
            restore_payload = restore_verify.get("json") if isinstance(restore_verify.get("json"), dict) else {}
            _restored_options, restored_provider, restored_model = model_options_by_provider(restore_payload)
            restore_confirmed = restored_provider == original_provider and restored_model == original_model
        baseline_evidence.update({"restoreStatus": restore.get("status"), "restoreConfirmed": restore_confirmed})
        add("stream-state-machine", "stream baseline restored original chat model", restore_confirmed, baseline_evidence)
    reqid = stream.get("requestId") or "missing"
    replay = request(f"/stream?request_id={urllib.parse.quote(reqid)}&session_id={urllib.parse.quote(session_id)}&last_event_id=0", opener=opener, timeout=20, read_limit=10000)
    cancel_fake = request("/cancel", method="POST", data={"request_id": "missing-" + RUN_ID, "session_id": session_id, "lang": "en"}, opener=opener, timeout=20)
    active2 = request("/api/active-requests", opener=opener, timeout=30)
    add("stream-state-machine", "post-terminal stream replay returns bytes", replay["status"] in {200, 400} and replay["bytes"] >= 0, {"status": replay["status"], "bytes": replay["bytes"]})
    add("stream-state-machine", "post-terminal replay has no session mismatch", "SESSION_MISMATCH" not in (replay.get("text") or ""), {"requestHash": h(reqid)})
    add("stream-state-machine", "idempotent cancel endpoint returns success", cancel_fake["status"] == 200 and (cancel_fake.get("json") or {}).get("status") == "success", {"status": cancel_fake["status"]})
    add("stream-state-machine", "cancel endpoint reports numeric count", isinstance((cancel_fake.get("json") or {}).get("cancelled"), int), cancel_fake.get("json") or {})
    add("stream-state-machine", "run ledger module imports", run(["/opt/ecorex-web/venv/bin/python", "-c", "from agent.protocol import get_run_ledger; print(bool(get_run_ledger()))"], cwd="/opt/ecorex-web/current/runtime").returncode == 0)
    add("stream-state-machine", "runtime event ledger module imports", run(["/opt/ecorex-web/venv/bin/python", "-c", "from agent.protocol import get_run_event_ledger; print(bool(get_run_event_ledger()))"], cwd="/opt/ecorex-web/current/runtime").returncode == 0)
    add("stream-state-machine", "cancel registry module imports", run(["/opt/ecorex-web/venv/bin/python", "-c", "from agent.protocol import get_cancel_registry; print(bool(get_cancel_registry()))"], cwd="/opt/ecorex-web/current/runtime").returncode == 0)
    add("stream-state-machine", "active requests payload has requests", isinstance((active2.get("json") or {}).get("requests"), list), active2.get("json") or {})
    add("stream-state-machine", "active requests payload has runStatusCounts", isinstance((active2.get("json") or {}).get("runStatusCounts"), dict), active2.get("json") or {})
    add("stream-state-machine", "active requests payload has staleLocks", isinstance((active2.get("json") or {}).get("staleLocks"), list), active2.get("json") or {})
    add("stream-state-machine", "no duplicate done events", stream.get("types", []).count("done") <= 1, {"types": stream.get("types", [])[:40]})
    add("stream-state-machine", "state machine evidence captured", bool(stream.get("requestId") or BUDGET_MODE == "no-external-models"), {"requestHash": h(stream.get("requestId"))})
    STATE_MACHINE_EVIDENCE.update({
        "sessionHash": h(session_id),
        "requestHash": h(stream.get("requestId")),
        "firstEventMs": stream.get("firstEventMs"),
        "firstContentMs": stream.get("firstContentMs"),
        "terminalMs": stream.get("terminalMs"),
        "eventTypes": stream.get("types", [])[:60],
        "terminalTypes": [str(item.get("type") or "") for item in stream.get("terminalEvents", [])],
        "baseline": baseline_evidence,
    })
    return session_id


def phase_context_session(opener, stream_session_id):
    sessions1 = request("/api/sessions?page=1&page_size=50", opener=opener, timeout=30)
    history1 = request(f"/api/history?session_id={urllib.parse.quote(stream_session_id)}&page=1&page_size=50", opener=opener, timeout=30)
    hist_json = history1.get("json") if isinstance(history1.get("json"), dict) else {}
    messages = hist_json.get("messages") if isinstance(hist_json.get("messages"), list) else []
    rename = request(f"/api/sessions/{urllib.parse.quote(stream_session_id)}", method="PUT", data={"title": "Acceptance " + RUN_ID}, opener=opener, timeout=30)
    sessions2 = request("/api/sessions?page=1&page_size=50", opener=opener, timeout=30)
    clear = request(f"/api/sessions/{urllib.parse.quote(stream_session_id)}/clear_context", method="POST", data={}, opener=opener, timeout=30)
    history2 = request(f"/api/history?session_id={urllib.parse.quote(stream_session_id)}&page=1&page_size=50", opener=opener, timeout=30)
    second_id = "accept-isolated-" + RUN_ID
    history3 = request(f"/api/history?session_id={urllib.parse.quote(second_id)}&page=1&page_size=20", opener=opener, timeout=30)
    delete_fake = request(f"/api/sessions/{urllib.parse.quote('nonexistent-' + RUN_ID)}", method="DELETE", opener=opener, timeout=20)
    sessions3 = request("/api/sessions?page=1&page_size=50", opener=opener, timeout=30)
    session_rows = (sessions2.get("json") or {}).get("sessions") or []
    ids = [str(row.get("session_id") or "") for row in session_rows if isinstance(row, dict)]
    add("context-session", "session list status success", sessions1["status"] == 200 and (sessions1.get("json") or {}).get("status") == "success")
    add("context-session", "history status success", history1["status"] == 200 and hist_json.get("status") == "success")
    add("context-session", "history message array exists", isinstance(messages, list), {"count": len(messages)})
    add("context-session", "history has user message", any(row.get("role") == "user" for row in messages if isinstance(row, dict)), {"count": len(messages)})
    add("context-session", "history has assistant or skipped model evidence", any(row.get("role") == "assistant" for row in messages if isinstance(row, dict)) or BUDGET_MODE == "no-external-models", {"count": len(messages)})
    add("context-session", "session list includes stream session", stream_session_id in ids, {"sessionHash": h(stream_session_id)})
    add("context-session", "session ids are unique", len(ids) == len(set(ids)), {"count": len(ids)})
    add("context-session", "rename session returns success", rename["status"] == 200 and (rename.get("json") or {}).get("status") == "success")
    add("context-session", "renamed session visible", any(str(row.get("title") or "") == "Acceptance " + RUN_ID for row in session_rows if isinstance(row, dict)), {"sessionHash": h(stream_session_id)})
    add("context-session", "clear context returns success", clear["status"] == 200 and (clear.get("json") or {}).get("status") == "success", clear.get("json") or {})
    add("context-session", "clear context returns context_start_seq", isinstance((clear.get("json") or {}).get("context_start_seq"), int), clear.get("json") or {})
    add("context-session", "history after clear returns success", history2["status"] == 200 and (history2.get("json") or {}).get("status") == "success")
    add("context-session", "history after clear exposes context_start_seq", isinstance((history2.get("json") or {}).get("context_start_seq"), int), history2.get("json") or {})
    add("context-session", "isolated second session has no copied history", history3["status"] == 200 and not ((history3.get("json") or {}).get("messages") or []), {"sessionHash": h(second_id)})
    add("context-session", "delete nonexistent session is idempotent success", delete_fake["status"] == 200 and (delete_fake.get("json") or {}).get("status") == "success")
    add("context-session", "session pagination deterministic", sessions3["status"] == 200 and isinstance((sessions3.get("json") or {}).get("sessions"), list))
    add("context-session", "session rows expose msg_count", all("msg_count" in row for row in session_rows[:10] if isinstance(row, dict)), {"rows": len(session_rows)})
    add("context-session", "session rows expose last_active", all("last_active" in row for row in session_rows[:10] if isinstance(row, dict)), {"rows": len(session_rows)})
    add("context-session", "history does not expose raw password", PASSWORD not in json.dumps(public_detail(history2.get("json") or {}), ensure_ascii=False), {"sessionHash": h(stream_session_id)})
    add("context-session", "context/session evidence captured", bool(stream_session_id), {"sessionHash": h(stream_session_id)})


def create_fixture(path):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (900, 520), "white")
    draw = ImageDraw.Draw(image)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 52)
        font_mid = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
    except Exception:
        font_big = font_mid = None
    draw.rectangle((40, 40, 410, 250), fill=(238, 40, 48), outline=(120, 0, 0), width=4)
    draw.rectangle((490, 40, 860, 250), fill=(40, 180, 70), outline=(0, 90, 20), width=4)
    draw.text((78, 112), "RED BOX", fill="white", font=font_mid)
    draw.text((535, 112), "GREEN BOX", fill="white", font=font_mid)
    draw.text((80, 310), "ECX OCR 4827", fill="black", font=font_big)
    image.save(path)


def phase_tool_skill(opener):
    tools = request("/api/tools", opener=opener, timeout=30)
    skills = request("/api/skills", opener=opener, timeout=30)
    capabilities = request("/api/capabilities", opener=opener, timeout=30)
    scheduler = request("/api/scheduler", opener=opener, timeout=30)
    combined = json.dumps({"tools": tools.get("json"), "skills": skills.get("json"), "capabilities": capabilities.get("json")}, ensure_ascii=False).lower()
    add("tool-skill", "tools endpoint success", tools["status"] == 200 and (tools.get("json") or {}).get("status") == "success")
    add("tool-skill", "skills endpoint success", skills["status"] == 200 and (skills.get("json") or {}).get("status") == "success")
    add("tool-skill", "capabilities endpoint success", capabilities["status"] == 200 and (capabilities.get("json") or {}).get("status") == "success")
    for marker in ("imagegen", "ocr", "vision", "browser", "office", "feishu"):
        add("tool-skill", f"discovery exposes {marker}", marker.replace("_", "-") in combined or marker.replace("-", "_") in combined or marker in combined)
    try:
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService
        skill_rows = SkillService(SkillManager()).query()
        skill_names = {str(row.get("name") or "").lower() for row in skill_rows if isinstance(row, dict)}
    except Exception as exc:
        skill_rows = []
        skill_names = set()
        skill_service_error = {"errorType": exc.__class__.__name__}
    else:
        skill_service_error = {}
    add(
        "tool-skill",
        "SkillService discovers built-in skills directly",
        len(skill_rows) >= 4 and any(name in skill_names for name in ("image-generation", "office-pdf", "skill-creator")),
        {"skillCount": len(skill_rows), "sampleHash": h(sorted(skill_names)[:12]), **skill_service_error},
    )
    try:
        from agent.tools.ecorex_cli.ecorex_cli import EcoreXCli
        cli = EcoreXCli({"cwd": "/opt/ecorex-web/current/runtime"})
        cli_version = cli.execute({"action": "version", "timeout": 20})
        cli_skill_list = cli.execute({"action": "skill_list", "timeout": 30})
        cli_version_status = getattr(cli_version, "status", "")
        cli_skill_status = getattr(cli_skill_list, "status", "")
        cli_payload = {
            "versionStatus": cli_version_status,
            "skillListStatus": cli_skill_status,
            "versionHash": h(getattr(cli_version, "result", "")),
            "skillListHash": h(getattr(cli_skill_list, "result", "")),
        }
    except Exception as exc:
        cli_version_status = cli_skill_status = "error"
        cli_payload = {"errorType": exc.__class__.__name__}
    add("tool-skill", "ecorex_cli readonly actions succeed", cli_version_status == "success" and cli_skill_status == "success", cli_payload)
    try:
        from agent.tools.tool_manager import ToolManager
        manager = ToolManager()
        manager.load_tools(start_mcp=False)
        mcp_status = manager.list_mcp_status()
        health = manager.registry_health()
    except Exception as exc:
        mcp_status = None
        health = {"errorType": exc.__class__.__name__}
    add(
        "tool-skill",
        "MCP status is discoverable without auto-start side effects",
        isinstance(mcp_status, dict) and isinstance(health, dict) and "mcpToolCount" in health,
        {"mcpStatusKeys": sorted(list((mcp_status or {}).keys()))[:10], "health": health},
    )
    add(
        "tool-skill",
        "direct tool probes bypass conversational memory",
        RUN_ID not in combined and bool(TMP.name),
        {"runHash": h(RUN_ID), "probeMode": "direct-tool-cli-skill-mcp"},
    )
    fixture_dir = TMP / "tool-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture = fixture_dir / "ocr-vision-fixture.png"
    create_fixture(fixture)
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker
        broker = get_tool_permission_broker()
        broker.set_mode("full-access")
        perm_state = broker.get_state()
    except Exception as exc:
        perm_state = {"errorType": exc.__class__.__name__}
    add("tool-skill", "permission broker full-access state", perm_state.get("mode") == "full-access", perm_state)
    try:
        from agent.tools.write.write import Write
        from agent.tools.read.read import Read
        write_result = Write({"cwd": str(fixture_dir)}).execute({"path": "hello.txt", "content": "ECX_READ_WRITE_OK"})
        read_result = Read({"cwd": str(fixture_dir)}).execute({"path": "hello.txt"})
        read_payload = getattr(read_result, "result", read_result)
        write_status = getattr(write_result, "status", "")
        read_status = getattr(read_result, "status", "")
    except Exception as exc:
        write_status = read_status = "error"
        read_payload = {"errorType": exc.__class__.__name__}
    add("tool-skill", "write tool succeeds", write_status == "success")
    add("tool-skill", "read tool succeeds", read_status == "success")
    add("tool-skill", "read tool returns written content", "ECX_READ_WRITE_OK" in json.dumps(read_payload, ensure_ascii=False), {"payloadHash": h(read_payload)})
    add("tool-skill", "OCR fixture file exists", fixture.is_file() and fixture.stat().st_size > 1000)
    try:
        from agent.tools.ocr.ocr import OcrTool
        ocr = OcrTool({"cwd": str(fixture_dir)})
        ocr_status, ocr_diag = safe_tool_payload(ocr.execute({"action": "diagnose"}))
        text_status, text_payload = safe_tool_payload(ocr.execute({"action": "extract_text", "image": str(fixture), "timeout": 8}))
        ocr_text = str(text_payload.get("text") or "")
    except Exception as exc:
        ocr_status = text_status = "error"
        ocr_diag = {"errorType": exc.__class__.__name__}
        ocr_text = ""
    add("tool-skill", "OCR diagnose succeeds", ocr_status == "success", ocr_diag)
    add("tool-skill", "OCR extracts ECX token", "ECX" in ocr_text.upper(), {"textHash": h(ocr_text), "chars": len(ocr_text)})
    try:
        from agent.tools.browser.browser_tool import BrowserTool
        browser = BrowserTool({"persistent": False, "snapshot_max_chars": 1600, "cdp_fallback": True})
        browser_status, browser_payload = safe_tool_payload(browser.execute({"action": "navigate", "url": "data:text/html,<h1>ECX_BROWSER_OK</h1>", "timeout": 20000}))
        browser.close()
    except Exception as exc:
        browser_status = "error"
        browser_payload = {"errorType": exc.__class__.__name__}
    add("tool-skill", "Browser tool navigate succeeds", browser_status == "success", browser_payload)
    add("tool-skill", "Browser tool sees sentinel", "ECX_BROWSER_OK" in json.dumps(browser_payload, ensure_ascii=False), {"payloadHash": h(browser_payload)})
    try:
        from agent.tools.vision.vision import Vision
        vision_status, vision_payload = safe_tool_payload(Vision({"cwd": str(fixture_dir)}).execute({"image": str(fixture), "question": "Describe this image briefly. Include any visible number."}))
        vision_text = json.dumps(vision_payload, ensure_ascii=False).lower()
    except Exception as exc:
        vision_status = "error"
        vision_text = str(exc)
    add("tool-skill", "Vision tool succeeds", vision_status == "success", {"status": vision_status})
    add("tool-skill", "Vision reads fixture semantics", any(token in vision_text for token in ("red", "green", "4827", "ecx")), {"answerHash": h(vision_text)})
    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return
            def do_GET(self):
                body = b"<html><title>ECX Fetch</title><main>ECX_WEB_FETCH_OK</main></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from agent.tools.web_fetch.web_fetch import WebFetch
            fetch_result = WebFetch({"cwd": str(fixture_dir)}).execute({"url": f"http://127.0.0.1:{server.server_port}/"})
            fetch_status = getattr(fetch_result, "status", "")
            fetch_payload = getattr(fetch_result, "result", fetch_result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    except Exception as exc:
        fetch_status = "error"
        fetch_payload = {"errorType": exc.__class__.__name__}
    add("tool-skill", "web_fetch tool succeeds on local page", fetch_status == "success", {"payloadHash": h(fetch_payload)})
    add("tool-skill", "scheduler endpoint exposes counts", scheduler["status"] == 200 and isinstance((scheduler.get("json") or {}).get("counts"), dict), scheduler.get("json") or {})


def model_options_by_provider(models_payload):
    chat = ((models_payload.get("capabilities") or {}).get("chat") or {}) if isinstance(models_payload.get("capabilities"), dict) else {}
    options = chat.get("model_options") if isinstance(chat.get("model_options"), list) else []
    out = {}
    for item in options:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider or not model:
            continue
        if item.get("configured") is False:
            continue
        out.setdefault(provider, item)
    return out, str(chat.get("current_provider") or ""), str(chat.get("current_model") or "")


def phase_multi_model_image_route(opener):
    models = request("/api/models", opener=opener, timeout=30)
    models_payload = models.get("json") if isinstance(models.get("json"), dict) else {}
    options_by_provider, original_provider, original_model = model_options_by_provider(models_payload)
    fixture_dir = TMP / "model-route"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture = fixture_dir / "edit-source.png"
    create_fixture(fixture)
    if BUDGET_MODE == "no-external-models":
        for provider in ("openai", "deepseek", "gemini", "doubao"):
            for name in (
                "configured chat option exists", "chat switch accepted", "generate route native",
                "generate uses gpt-image-2-pro", "generate avoids fallback", "edit route native",
                "edit uses gpt-image-2-pro", "edit avoids fallback",
            ):
                add_skip("multi-model-image-route", f"{provider} {name}", "budget mode disables image model calls", priority="P2", hard_gate=False)
        return
    try:
        from agent.tools.imagegen.imagegen import ImageGenTool
        imagegen = ImageGenTool()
    except Exception as exc:
        imagegen = None
        import_error = {"errorType": exc.__class__.__name__}
    for provider in ("openai", "deepseek", "gemini", "doubao"):
        option = options_by_provider.get(provider)
        if not option:
            for name in (
                "configured chat option exists", "chat switch accepted", "generate route native",
                "generate uses gpt-image-2-pro", "generate avoids fallback", "edit route native",
                "edit uses gpt-image-2-pro", "edit avoids fallback",
            ):
                add_skip("multi-model-image-route", f"{provider} {name}", "provider has no configured chat model option", priority="P2", hard_gate=False)
            MODEL_ROUTE_EVIDENCE.append({"provider": provider, "status": "skipped", "reason": "not-configured"})
            continue
        model = str(option.get("model") or "")
        switch = request("/api/models", method="POST", data={"action": "set_capability", "capability": "chat", "provider_id": provider, "model": model}, opener=opener, timeout=60)
        gen_status = edit_status = "error"
        gen = edit = {}
        if imagegen is not None and (switch.get("json") or {}).get("status") == "success":
            gen_status, gen = safe_tool_payload(imagegen.execute({
                "prompt": f"Create a clean tiny product icon for provider route {provider}: orange X on white background, no text besides X.",
                "size": "1024x1024",
                "output_format": "png",
                "output_dir": str(fixture_dir),
                "quality_retry_max": 0,
                "timeout": 650,
            }))
            edit_status, edit = safe_tool_payload(imagegen.execute({
                "prompt": f"Edit this reference after switching chat provider {provider}: preserve the colored boxes and make the background cleaner.",
                "image_url": str(fixture),
                "size": "1024x1024",
                "output_format": "png",
                "output_dir": str(fixture_dir),
                "quality_retry_max": 0,
                "timeout": 650,
            }))
        elif imagegen is None:
            gen = edit = import_error
        gen_route = gen.get("route") if isinstance(gen.get("route"), dict) else {}
        edit_route = edit.get("route") if isinstance(edit.get("route"), dict) else {}
        evidence = {
            "provider": provider,
            "chatModelHash": h(model),
            "switchStatus": (switch.get("json") or {}).get("status"),
            "generateStatus": gen_status,
            "generateModel": str(gen.get("model") or ""),
            "generateRoute": gen_route,
            "editStatus": edit_status,
            "editModel": str(edit.get("model") or ""),
            "editRoute": edit_route,
        }
        MODEL_ROUTE_EVIDENCE.append(public_detail(evidence))
        add("multi-model-image-route", f"{provider} configured chat option exists", bool(model), {"provider": provider, "modelHash": h(model)})
        add("multi-model-image-route", f"{provider} chat switch accepted", switch["status"] == 200 and (switch.get("json") or {}).get("status") == "success", {"provider": provider})
        add("multi-model-image-route", f"{provider} generate route native", gen_status == "success" and gen_route.get("providerApiRoute") == "images.generations", evidence)
        add("multi-model-image-route", f"{provider} generate uses gpt-image-2-pro", str(gen.get("model") or "").lower() == "gpt-image-2-pro", evidence)
        add("multi-model-image-route", f"{provider} generate avoids shell/python fallback", gen.get("pythonFallbackUsed") is False and gen.get("fallbackUsed") is False and gen_route.get("shellInvocation") is False and gen_route.get("pythonSubprocess") is False, evidence)
        add("multi-model-image-route", f"{provider} edit route native", edit_status == "success" and edit_route.get("providerApiRoute") == "images.edits", evidence)
        add("multi-model-image-route", f"{provider} edit uses gpt-image-2-pro", str(edit.get("model") or "").lower() == "gpt-image-2-pro", evidence)
        add("multi-model-image-route", f"{provider} edit avoids shell/python fallback", edit.get("pythonFallbackUsed") is False and edit.get("fallbackUsed") is False and edit_route.get("shellInvocation") is False and edit_route.get("pythonSubprocess") is False, evidence)
    if original_provider and original_model:
        request("/api/models", method="POST", data={"action": "set_capability", "capability": "chat", "provider_id": original_provider, "model": original_model}, opener=opener, timeout=60)


def phase_concurrency_pressure():
    total_requests = max(1, PRESSURE_USERS) * max(1, PRESSURE_TURNS)
    observer, _observer_jar, observer_login = login_opener(f"pressure-observer-{RUN_ID}@ecorex.local")
    before = request("/api/active-requests", opener=observer, timeout=30)
    latencies = []
    statuses = []
    errors = []
    user_ids = [f"pressure-{RUN_ID}-{i:02d}" for i in range(PRESSURE_USERS)]
    session_hashes = set()
    login_success = 0

    def worker(user_index):
        nonlocal login_success
        email = f"{user_ids[user_index]}@ecorex.local"
        opener, jar, login = login_opener(email)
        if login.get("status") == 200 and (login.get("json") or {}).get("status") == "success":
            login_success += 1
        local_rows = []
        session_id = f"pressure-session-{RUN_ID}-{user_index:02d}"
        for turn in range(PRESSURE_TURNS):
            if turn % 3 == 0:
                path = "/api/sessions?page=1&page_size=5"
            elif turn % 3 == 1:
                path = "/api/tools"
            else:
                path = f"/api/history?session_id={urllib.parse.quote(session_id)}&page=1&page_size=5"
            resp = request(path, opener=opener, timeout=35)
            local_rows.append({"status": resp.get("status"), "latencyMs": resp.get("latencyMs"), "json": isinstance(resp.get("json"), dict), "session": session_id})
        return local_rows

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, PRESSURE_USERS))) as executor:
        futures = [executor.submit(worker, index) for index in range(PRESSURE_USERS)]
        for future in concurrent.futures.as_completed(futures, timeout=180):
            try:
                for row in future.result():
                    statuses.append(int(row.get("status") or 0))
                    latencies.append(int(row.get("latencyMs") or 0))
                    session_hashes.add(h(row.get("session")))
            except Exception as exc:
                errors.append({"errorType": exc.__class__.__name__, "message": str(exc)[:120]})
    after = request("/api/active-requests", opener=observer, timeout=30)
    active_after = (after.get("json") or {}).get("requests") or []
    p95 = 0
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    duration_ms = int((time.perf_counter() - started) * 1000)
    PRESSURE_EVIDENCE.update({
        "users": PRESSURE_USERS,
        "turnsPerUser": PRESSURE_TURNS,
        "requestedOperations": total_requests,
        "completedOperations": len(statuses),
        "loginSuccess": login_success,
        "durationMs": duration_ms,
        "p95LatencyMs": p95,
        "maxLatencyMs": max(latencies) if latencies else 0,
        "statusCounts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
        "errorCount": len(errors),
        "sessionHashCount": len(session_hashes),
        "activeAfterCount": len(active_after),
        "activeBeforeStatus": before.get("status"),
        "activeAfterStatus": after.get("status"),
        "observerLoginStatus": observer_login.get("status"),
    })
    add("concurrency-pressure", "pressure profile uses 20 virtual users by default", PRESSURE_USERS >= 20, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure profile creates at least 60 operations", total_requests >= 60, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "all pressure workers completed operations", len(statuses) == total_requests, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "no pressure worker transport errors", not errors, {"errors": errors[:5], **PRESSURE_EVIDENCE})
    add("concurrency-pressure", "no pressure API 5xx responses", not any(code >= 500 for code in statuses), PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure API p95 under 5s", p95 < 5000, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure API max latency under 15s", (max(latencies) if latencies else 999999) < 15000, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "virtual user session hashes are isolated", len(session_hashes) == PRESSURE_USERS, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure responses are all HTTP 2xx", all(200 <= code < 300 for code in statuses), PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure responses are JSON", len(statuses) == total_requests, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "active requests endpoint works after pressure", after["status"] == 200, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "no stuck primary active requests after pressure", len(active_after) == 0, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "no stale locks after pressure", not ((after.get("json") or {}).get("staleLocks") or []), after.get("json") or {})
    add("concurrency-pressure", "runStatusCounts remains bounded", sum((after.get("json") or {}).get("runStatusCounts", {}).values()) <= 5 if isinstance((after.get("json") or {}).get("runStatusCounts"), dict) else False, after.get("json") or {})
    add("concurrency-pressure", "all virtual users logged in", login_success == PRESSURE_USERS, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "no 401 after login pressure", 401 not in statuses and 403 not in statuses, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure duration bounded", duration_ms < 180000, PRESSURE_EVIDENCE)
    add("concurrency-pressure", "pressure evidence captured", bool(PRESSURE_EVIDENCE), PRESSURE_EVIDENCE)


def _joined_asset_text(runtime, pattern, limit=3_000_000):
    chunks = []
    total = 0
    try:
        paths = sorted(runtime.glob(pattern))
    except Exception:
        paths = []
    for path in paths:
        text = read_text(path, limit=max(1, limit - total))
        if text:
            chunks.append(text)
            total += len(text)
        if total >= limit:
            break
    return "\n".join(chunks)


def _python_method_block(source, name):
    start = source.find(f"def {name}")
    if start < 0:
        return ""
    end = source.find("\n    def ", start + 10)
    return source[start:end if end > start else None]


def _first_custom_gemini_option(options):
    for item in options:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        model = str(item.get("model") or "").strip()
        alias = str(item.get("modelAliasFamily") or item.get("model_alias_family") or "").strip().lower()
        official = item.get("isOfficialGeminiProvider") is True or item.get("is_official_gemini_provider") is True
        if provider == "custom" and (alias == "gemini" or "gemini" in model.lower()) and not official:
            return item
    return {}


def _current_chat_payload(models_payload):
    chat = ((models_payload.get("capabilities") or {}).get("chat") or {}) if isinstance(models_payload.get("capabilities"), dict) else {}
    return chat if isinstance(chat, dict) else {}


def phase_v027_integrated_capabilities(opener, stream_session_id):
    runtime = Path("/opt/ecorex-web/current/runtime")
    web_channel = read_text(runtime / "channel" / "web" / "web_channel.py")
    conversation_store = read_text(runtime / "agent" / "memory" / "conversation_store.py")
    agent_stream = read_text(runtime / "agent" / "protocol" / "agent_stream.py")
    prompt_builder = read_text(runtime / "agent" / "prompt" / "builder.py")
    skill_contract = read_text(runtime / "agent" / "skills" / "tool_binding_contract.py")
    openai_client = read_text(runtime / "models" / "openai" / "openai_http_client.py")
    model_telemetry = read_text(runtime / "models" / "model_telemetry.py")
    imagegen_tool = read_text(runtime / "agent" / "tools" / "imagegen" / "imagegen.py")
    browser_tool = read_text(runtime / "agent" / "tools" / "browser" / "browser_tool.py")
    browser_auto = read_text(runtime / "agent" / "tools" / "browser" / "browser_automation_service.py")
    permission_broker = read_text(runtime / "common" / "ecorex_tool_permissions.py")
    ocr_source = read_text(runtime / "agent" / "tools" / "ocr" / "ocr.py")
    vision_source = read_text(runtime / "agent" / "tools" / "vision" / "vision.py")
    tongxin_source = read_text(runtime / "agent" / "tools" / "tongxin_cli" / "tongxin_cli.py")
    config_source = read_text(runtime / "config.py")
    app_js = _joined_asset_text(runtime, "channel/web/static/app/assets/*.js")
    app_css = _joined_asset_text(runtime, "channel/web/static/app/assets/*.css")
    admin_api_source = read_text(Path("/srv/ecorex-agent-admin/app/ecorex_admin_api.py"))
    admin_page_html = read_text(Path("/srv/ecorex-agent-download/current/admin/index.html"))
    admin_page_js = read_text(Path("/srv/ecorex-agent-download/current/admin/admin.js"))
    public_manifest = read_json(Path("/srv/ecorex-agent-download/current/manifest.json"))
    background_update = (((public_manifest.get("update") or {}).get("webui") or {}).get("backgroundUpdate") or {})
    set_block = "\n".join([
        _python_method_block(web_channel, "_handle_set_capability"),
        _python_method_block(web_channel, "_set_chat"),
        _python_method_block(web_channel, "_reset_bridge"),
    ])

    version_probe = request("/api/version", opener=opener, timeout=30)
    version_payload = version_probe.get("json") if isinstance(version_probe.get("json"), dict) else {}
    update_state = version_payload.get("updateState") if isinstance(version_payload.get("updateState"), dict) else {}
    update_check_win = request("/api/update-check?platform=win32", opener=opener, timeout=30)
    update_check_mac = request("/api/update-check?platform=darwin", opener=opener, timeout=30)
    update_win_payload = update_check_win.get("json") if isinstance(update_check_win.get("json"), dict) else {}
    update_mac_payload = update_check_mac.get("json") if isinstance(update_check_mac.get("json"), dict) else {}
    models = request("/api/models", opener=opener, timeout=30)
    models_payload = models.get("json") if isinstance(models.get("json"), dict) else {}
    chat_payload = _current_chat_payload(models_payload)
    options = chat_payload.get("model_options") if isinstance(chat_payload.get("model_options"), list) else []
    custom_gemini = _first_custom_gemini_option(options)
    original_provider = str(chat_payload.get("current_provider") or "")
    original_model = str(chat_payload.get("current_model") or "")
    custom_model = str(custom_gemini.get("model") or "")
    custom_alias = str(custom_gemini.get("modelAliasFamily") or custom_gemini.get("model_alias_family") or "")
    switch_payload = {}
    switch_result = {"status": 0, "json": {}}
    if custom_gemini:
        switch_result = request(
            "/api/models",
            method="POST",
            data={"action": "set_capability", "capability": "chat", "provider_id": "custom", "model": custom_model},
            opener=opener,
            timeout=90,
        )
        switch_payload = switch_result.get("json") if isinstance(switch_result.get("json"), dict) else {}
    verify_payload = request("/api/models", opener=opener, timeout=30).get("json") if custom_gemini else {}
    verify_chat = _current_chat_payload(verify_payload if isinstance(verify_payload, dict) else {})
    custom_stream = {
        "post": {"status": 0},
        "requestId": "",
        "types": [],
        "terminalEvents": [],
        "contentHash": h(""),
        "contentLength": 0,
        "contentPreview": "",
        "terminalMs": None,
    }
    if custom_gemini and switch_result.get("status") == 200 and switch_payload.get("status") == "success" and BUDGET_MODE != "no-external-models":
        custom_stream = send_streamed_message(
            opener,
            "accept-custom-gemini-" + RUN_ID,
            (
                f"Fresh isolated custom Gemini acceptance run {RUN_ID}. "
                f"Reply with exactly ECX_GEMINI_OK_{RUN_ID}. "
                "Do not use stored memories or prior conversation context."
            ),
            timeout=190,
        )
    restore = {"status": 0, "json": {}}
    restore_confirmed = not bool(original_provider and original_model)
    if original_provider and original_model:
        restore = request(
            "/api/models",
            method="POST",
            data={"action": "set_capability", "capability": "chat", "provider_id": original_provider, "model": original_model},
            opener=opener,
            timeout=90,
        )
        restored_payload = request("/api/models", opener=opener, timeout=30).get("json")
        restored_chat = _current_chat_payload(restored_payload if isinstance(restored_payload, dict) else {})
        restore_confirmed = restored_chat.get("current_provider") == original_provider and restored_chat.get("current_model") == original_model

    context_policy = switch_payload.get("context_policy") if isinstance(switch_payload.get("context_policy"), dict) else {}
    continuity = switch_payload.get("contextContinuity") if isinstance(switch_payload.get("contextContinuity"), dict) else {}
    switch_evidence = {
        "modelHash": h(custom_model),
        "originalProvider": original_provider,
        "customProvider": custom_gemini.get("provider"),
        "aliasFamily": custom_alias,
        "officialGemini": custom_gemini.get("isOfficialGeminiProvider"),
        "switchStatus": switch_result.get("status"),
        "switchResultStatus": switch_payload.get("status"),
        "restoreStatus": restore.get("status"),
        "restoreConfirmed": restore_confirmed,
        "customStreamRequestHash": h(custom_stream.get("requestId")),
        "customStreamContentLength": custom_stream.get("contentLength"),
        "customStreamContentHash": custom_stream.get("contentHash"),
        "customStreamTerminalTypes": [str(item.get("type") or "") for item in custom_stream.get("terminalEvents", [])],
    }
    add("v027-integrated-capabilities", "models endpoint succeeds", models["status"] == 200 and models_payload.get("status") == "success")
    add("v027-integrated-capabilities", "chat model options are returned", isinstance(options, list) and bool(options), {"count": len(options)})
    add("v027-integrated-capabilities", "custom Gemini option exists", bool(custom_gemini), switch_evidence)
    add("v027-integrated-capabilities", "custom Gemini provider is custom", str(custom_gemini.get("provider") or "").lower() == "custom", switch_evidence)
    add("v027-integrated-capabilities", "custom Gemini alias family is gemini", custom_alias.lower() == "gemini", switch_evidence)
    add("v027-integrated-capabilities", "custom Gemini is not official Google provider", custom_gemini.get("isOfficialGeminiProvider") is False or custom_gemini.get("is_official_gemini_provider") is False, switch_evidence)
    add("v027-integrated-capabilities", "custom Gemini option is configured", custom_gemini.get("configured") is not False, switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini chat capability succeeds", switch_result["status"] == 200 and switch_payload.get("status") == "success", switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini result provider is custom", str(switch_payload.get("provider") or "").lower() == "custom", switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini result model matches option", str(switch_payload.get("model") or "") == custom_model, switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini result alias remains gemini", str(switch_payload.get("modelAliasFamily") or "").lower() == "gemini", switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini result is not official Gemini", switch_payload.get("isOfficialGeminiProvider") is False, switch_evidence)
    add("v027-integrated-capabilities", "set custom Gemini exposes context policy", isinstance(context_policy, dict) and bool(context_policy), {"keys": sorted(context_policy.keys())[:10]})
    add("v027-integrated-capabilities", "context policy has positive window", int(context_policy.get("contextWindowTokens") or 0) > 0, {"window": context_policy.get("contextWindowTokens")})
    add("v027-integrated-capabilities", "set custom Gemini exposes context continuity", isinstance(continuity, dict) and bool(continuity), continuity)
    add("v027-integrated-capabilities", "context continuity preserves agent bridge", continuity.get("agentBridgePreserved") is True, continuity)
    add("v027-integrated-capabilities", "context continuity enables artifact history refs", continuity.get("artifactHistoryRefs") == "enabled", continuity)
    add("v027-integrated-capabilities", "context continuity uses refresh chat routing", continuity.get("strategy") == "refresh_chat_routing", continuity)
    add("v027-integrated-capabilities", "current API reports custom Gemini after switch", verify_chat.get("current_provider") == "custom" and verify_chat.get("current_model") == custom_model, {"modelHash": h(verify_chat.get("current_model"))})
    custom_terminal_types = [str(item.get("type") or "") for item in custom_stream.get("terminalEvents", [])]
    custom_stream_preview = str(custom_stream.get("contentPreview") or "")
    if BUDGET_MODE == "no-external-models":
        add_skip("v027-integrated-capabilities", "custom Gemini switched model produces content", "budget mode disables external custom Gemini calls", priority="P2", hard_gate=False)
        add_skip("v027-integrated-capabilities", "custom Gemini response avoids empty fallback apology", "budget mode disables external custom Gemini calls", priority="P2", hard_gate=False)
    else:
        add(
            "v027-integrated-capabilities",
            "custom Gemini switched model produces content",
            custom_stream.get("contentLength", 0) > 0 and bool(custom_stream.get("terminalEvents")) and not any(t in {"error", "interrupted", "replay_gap"} for t in custom_terminal_types),
            {**switch_evidence, "terminalMs": custom_stream.get("terminalMs")},
        )
        add(
            "v027-integrated-capabilities",
            "custom Gemini response avoids empty fallback apology",
            custom_stream.get("contentLength", 0) > 0 and not any(marker in custom_stream_preview for marker in ("抱歉，我暂时无法生成回复", "暂时无法生成回复", "unable to generate")),
            {**switch_evidence, "contentPreviewHash": h(custom_stream_preview)},
        )
    add("v027-integrated-capabilities", "restore original chat model accepted", restore_confirmed or restore.get("status") == 200, {"restoreStatus": restore.get("status")})
    add("v027-integrated-capabilities", "restore original chat model confirmed", restore_confirmed, {"originalProvider": original_provider, "originalModelHash": h(original_model)})

    add("v027-integrated-capabilities", "set_capability refreshes chat routing", "refresh_chat_routing" in set_block and "getattr(bridge, \"refresh_chat_routing\", None)" in set_block)
    add("v027-integrated-capabilities", "set_capability does not call Bridge().reset_bot", "Bridge().reset_bot()" not in set_block)
    add("v027-integrated-capabilities", "web channel returns modelAliasFamily", "modelAliasFamily" in web_channel)
    add("v027-integrated-capabilities", "web channel returns isOfficialGeminiProvider", "isOfficialGeminiProvider" in web_channel)
    add("v027-integrated-capabilities", "web channel returns context continuity and policy", "contextContinuity" in web_channel and "context_policy" in web_channel)
    add("v027-integrated-capabilities", "conversation store restores user attachment history", "_attachment_history_context" in conversation_store and "file_path" in conversation_store and "历史文件" in conversation_store)
    add("v027-integrated-capabilities", "conversation store restores assistant artifact history", "_artifact_history_context" in conversation_store and "relativePath" in conversation_store and "历史文件产物" in conversation_store)
    add("v027-integrated-capabilities", "conversation store appends compact history summary", "_history_context_summary" in conversation_store and "_content_with_history_context" in conversation_store)
    add("v027-integrated-capabilities", "agent stream estimates artifact metadata tokens", "artifact_metadata_tokens" in agent_stream and "_estimate_artifact_metadata_tokens" in agent_stream)
    add("v027-integrated-capabilities", "agent stream emits context budget telemetry", 'self._emit_event("context_budget", context_budget)' in agent_stream)
    add("v027-integrated-capabilities", "agent stream tracks runtime artifact count", "runtime_artifact_count" in agent_stream)
    add("v027-integrated-capabilities", "OpenAI-compatible stream normalizes non-SSE chat JSON", "_stream_chunks_from_chat_response" in openai_client and 'text/event-stream' in openai_client and "_normalize_chat_completion_chunk" in openai_client)
    add("v027-integrated-capabilities", "OpenAI-compatible stream maps message content to delta", "_message_to_delta" in openai_client and "_normalize_delta_content" in openai_client and '"delta"' in openai_client)
    add("v027-integrated-capabilities", "model telemetry treats message content as output", "message = choice.get(\"message\")" in model_telemetry and "choice.get(\"text\")" in model_telemetry)
    add("v027-integrated-capabilities", "agent stream consumes message-level content and refusal", "_model_content_to_text" in agent_stream and "choice.get(\"message\")" in agent_stream and "message_payload.get(\"refusal\")" in agent_stream)

    add("v027-integrated-capabilities", "version API exposes background update state contract", version_probe["status"] == 200 and isinstance(update_state, dict) and "updateState" in version_payload and "_runtime_update_state_payload" in web_channel and "refreshRequired" in web_channel, {"versionStatus": version_probe.get("status"), "stateKeys": sorted(update_state.keys())[:12]})
    add("v027-integrated-capabilities", "public manifest declares prompt refresh activation policy", background_update.get("activationPolicy") == "prompt-soft-refresh-existing-tab" and background_update.get("healthCheck") == "/api/version" and background_update.get("stateFile") == "update-state.json" and background_update.get("autoLaunchBrowser") == "never-in-background", background_update)
    add("v027-integrated-capabilities", "static app renders non-forced background update refresh banner", all(marker in app_js for marker in ("updateState", "ecorex-background-update-applied", "新版本已就绪", "后台更新已安装")))
    public_manifest_artifacts = {
        row.get("id"): row
        for row in ((public_manifest.get("artifacts") or []))
        if isinstance(row, dict)
    }
    webui_artifact_ids = ("webui-windows-x64", "webui-macos-universal")
    webui_update = (public_manifest.get("update") or {}).get("webui") if isinstance(public_manifest.get("update"), dict) else {}
    webui_update = webui_update if isinstance(webui_update, dict) else {}
    update_artifact_detail = {
        artifact_id: {
            "version": public_manifest_artifacts.get(artifact_id, {}).get("version"),
            "status": public_manifest_artifacts.get(artifact_id, {}).get("status"),
            "hrefHash": h(public_manifest_artifacts.get(artifact_id, {}).get("href")),
            "size": public_manifest_artifacts.get(artifact_id, {}).get("size"),
            "sha256Present": bool(public_manifest_artifacts.get(artifact_id, {}).get("sha256")),
        }
        for artifact_id in webui_artifact_ids
    }
    add("v027-integrated-capabilities", "public manifest is promoted to current stable version", public_manifest.get("version") == VERSION and Path("/srv/ecorex-agent-download/current/manifest.json").is_file(), {"version": public_manifest.get("version"), "updatedAt": public_manifest.get("updatedAt")})
    add("v027-integrated-capabilities", "public manifest WebUI update is admin-gated stable", webui_update.get("promotion") == "admin-gated" and webui_update.get("channel") == "stable" and set(webui_artifact_ids).issubset(set(webui_update.get("artifactIds") or [])), webui_update)
    add("v027-integrated-capabilities", "public manifest WebUI update artifacts are ready local downloads", all(
        artifact_id in public_manifest_artifacts
        and public_manifest_artifacts[artifact_id].get("version") == VERSION
        and public_manifest_artifacts[artifact_id].get("status") == "ready"
        and str(public_manifest_artifacts[artifact_id].get("href") or "").startswith("downloads/")
        and int(public_manifest_artifacts[artifact_id].get("size") or 0) > 0
        and bool(public_manifest_artifacts[artifact_id].get("sha256"))
        for artifact_id in webui_artifact_ids
    ), update_artifact_detail)
    add("v027-integrated-capabilities", "admin release API exposes protected state and promote endpoints", all(marker in admin_api_source for marker in ('path == "/release/state"', 'path == "/release/promote"', "self.store.release_state()", "self.store.promote_release(payload)", "if not self._require_admin()")))
    add("v027-integrated-capabilities", "admin release promotion validates staged artifacts before current switch", all(marker in admin_api_source for marker in ("_validate_release_dir(release_dir, manifest, verify_sha=True)", "os.replace(str(tmp_pointer), str(current_pointer))", '"release.promote"', '"sha256Verified": True')))
    add("v027-integrated-capabilities", "admin release supports same-version hotfix publish by artifact fingerprint", all(marker in admin_api_source for marker in ('def _release_manifest_fingerprint', '"artifactFingerprint"', '"sameVersionHotfix"', 'entry.get("artifactFingerprint") != current_fingerprint')))
    add("v027-integrated-capabilities", "admin release blocks older staged downgrade candidates", all(marker in admin_api_source for marker in ('def _compare_release_versions', 'version_compare < 0', '候选版本低于当前 stable', 'staged release is older than current stable')))
    add("v027-integrated-capabilities", "admin release can notify users for current stable", all(marker in admin_api_source for marker in ('def notify_release', 'path == "/release/notify"', '"release.notify"', '"noticeRevision"')))
    add("v027-integrated-capabilities", "admin release page exposes one-click publish controls", all(marker in admin_page_html + admin_page_js for marker in ("data-release-summary", "data-release-refresh", "data-release-promote", 'request("/release/state")', 'mutate("/release/promote"')))
    add("v027-integrated-capabilities", "admin release page surfaces publish disabled reasons", all(marker in admin_page_js for marker in ("data-release-can-promote", "data-release-disabled-reason", "showNotice(button.dataset.releaseDisabledReason", "发布中...")))
    add("v027-integrated-capabilities", "admin release page exposes notify users action for current stable", all(marker in admin_page_js for marker in ("data-release-can-notify", "通知用户", 'mutate("/release/notify"', "通知中...")))
    update_pick_block = _python_method_block(web_channel, "_pick_artifact")
    add("v027-integrated-capabilities", "update-check source maps Windows and macOS to WebUI artifacts", all(marker in update_pick_block for marker in ('preferred_ids.append("webui-windows-x64")', 'preferred_ids.append("webui-macos-universal")', 'recommended.get(key)')) and 'preferred_ids.append("windows-x64")' not in update_pick_block and 'preferred_ids.append("macos-arm64-dmg")' not in update_pick_block)
    add("v027-integrated-capabilities", "update-check can surface same-version artifact hotfixes", all(marker in web_channel for marker in ('"updateReason"', 'def _artifact_changed', 'def _installed_artifact_metadata', '发现 {latest_version} 同版本更新')))
    add("v027-integrated-capabilities", "update-check can surface admin notice revisions", all(marker in web_channel for marker in ("_release_manifest_notice_state_payload", "noticeRevision", '"notice" if notice_active else ""')))
    update_win_artifact = update_win_payload.get("artifact") if isinstance(update_win_payload.get("artifact"), dict) else {}
    update_mac_artifact = update_mac_payload.get("artifact") if isinstance(update_mac_payload.get("artifact"), dict) else {}
    add("v027-integrated-capabilities", "update-check endpoint exposes WebUI update policy and artifacts", update_check_win.get("status") == 200 and update_check_mac.get("status") == 200 and update_win_artifact.get("id") == "webui-windows-x64" and update_mac_artifact.get("id") == "webui-macos-universal" and ((update_win_payload.get("update") or {}).get("webui") or {}).get("promotion") == "admin-gated", {"winStatus": update_check_win.get("status"), "macStatus": update_check_mac.get("status"), "winArtifact": update_win_artifact.get("id"), "macArtifact": update_mac_artifact.get("id")})
    add("v027-integrated-capabilities", "static app polls update-check and renders user update reminder", all(marker in app_js for marker in ("/api/update-check", "ecorex-update-notice-dismissed", "发现 EcoreX 新版本", "打开下载页")))
    add("v027-integrated-capabilities", "public manifest carries rebuilt WebUI artifacts", all(
        artifact_id in public_manifest_artifacts
        and int(public_manifest_artifacts[artifact_id].get("size") or 0) > 0
        and bool(public_manifest_artifacts[artifact_id].get("sha256"))
        for artifact_id in ("webui-windows-x64", "webui-macos-universal", "web-linux-service")
    ), {"artifactIds": sorted(public_manifest_artifacts.keys())})

    divider_index = app_js.rfind("model-switch-divider")
    divider_window = app_js[max(0, divider_index - 1200):divider_index + 1600] if divider_index >= 0 else ""
    divider_css_index = app_css.rfind("model-switch-divider")
    divider_css_window = app_css[max(0, divider_css_index - 800):divider_css_index + 1200].lower() if divider_css_index >= 0 else ""
    add("v027-integrated-capabilities", "static app renders model switch divider", "model-switch-divider" in app_js)
    add("v027-integrated-capabilities", "static css styles model switch divider", ".model-switch-divider" in app_css)
    add("v027-integrated-capabilities", "model switch divider renders as separator", ("separator" in divider_window and "role" in divider_window) or 'role="separator"' in divider_window)
    add("v027-integrated-capabilities", "model switch divider message is context excluded", "contextExcluded" in app_js and "model-switch-divider" in app_js)
    add("v027-integrated-capabilities", "model switch divider has no recovery controls", "message-recovery-actions" not in divider_window, {"windowHash": h(divider_window)})
    add("v027-integrated-capabilities", "model switch divider participates in normal message flow", ".message.model-switch-message" in app_css and "model-switch-divider" in app_js and "contextExcluded" in app_js, {"windowHash": h(divider_window), "cssWindowHash": h(divider_css_window)})
    add("v027-integrated-capabilities", "model switch divider is not pinned or sticky", all(marker not in divider_css_window for marker in ("position:sticky", "position:fixed", "bottom:0", "bottom: 0")), {"cssWindowHash": h(divider_css_window)})
    add("v027-integrated-capabilities", "model switch divider has no copy controls", "copyMessage" not in divider_window and "message-actions" not in divider_window, {"windowHash": h(divider_window)})
    add("v027-integrated-capabilities", "custom Gemini UI keeps provider custom alias condition", "modelAliasFamily" in app_js and "isOfficialGeminiProvider" in app_js and "custom" in app_js and "gemini" in app_js.lower())

    add("v027-integrated-capabilities", "imagegen semantic regex guard exists", "IMAGEGEN_SHELL_SEMANTIC_SIGNAL_REGEXES" in agent_stream)
    add("v027-integrated-capabilities", "imagegen intent selects primary route", "imagegen_intent_primary_route" in agent_stream and "imagegen_primary_route" in agent_stream)
    add("v027-integrated-capabilities", "missing imagegen uses diagnostics not bash", "imagegen_visibility_diagnostics" in agent_stream and "imagegen_intent_no_safe_schema_tool" in agent_stream)
    add("v027-integrated-capabilities", "imagegen context overflow keeps imagegen schema", "imagegen_context_overflow_recovery" in agent_stream and "schema_only_imagegen_tool_schema_minimized" in agent_stream)
    add("v027-integrated-capabilities", "raw shell semantic image generation is blocked", "Do not generate or edit images through raw bash" in agent_stream or "native `imagegen` route is visible" in agent_stream)
    add("v027-integrated-capabilities", "prompt instructs native one-or-more imagegen calls", "native `imagegen` tool one or more times" in prompt_builder)
    add("v027-integrated-capabilities", "skill contract instructs one-or-more imagegen calls", "call imagegen one or more times" in skill_contract)
    add("v027-integrated-capabilities", "imagegen prompt no longer hardcodes tasks batch route", "Use `imagegen.tasks` for batches" not in prompt_builder and "Use `imagegen.tasks` for batches" not in skill_contract)
    add("v027-integrated-capabilities", "AgentStream binds tool emit_event callback", "tool.emit_event = self._emit_event" in agent_stream and "tool.tool_call_id = tool_id" in agent_stream)
    add("v027-integrated-capabilities", "imagegen batch emits each ready image via file_to_send", "_emit_batch_image_ready" in imagegen_tool and 'emit_event("file_to_send"' in imagegen_tool and "_image_result_path" in imagegen_tool)
    add("v027-integrated-capabilities", "imagegen batch ready event is observable and redacted", "task_index" in imagegen_tool and '"redacted": True' in imagegen_tool and "native_imagegen_tool_loop" in imagegen_tool)

    try:
        from agent.tools.browser.browser_automation_service import browser_automation_diagnostics
        browser_diag = browser_automation_diagnostics({"cdp_auto_launch": True, "cdp_fallback": True})
    except Exception as exc:
        browser_diag = {"errorType": exc.__class__.__name__}
    add("v027-integrated-capabilities", "BrowserTool default cdp_auto_launch is true", 'setdefault("cdp_auto_launch", True)' in browser_tool)
    add("v027-integrated-capabilities", "BrowserTool default cdp_fallback is true", 'setdefault("cdp_fallback", True)' in browser_tool)
    add("v027-integrated-capabilities", "browser diagnostics mode is cdp-first", browser_diag.get("mode") == "cdp-first", browser_diag)
    add("v027-integrated-capabilities", "browser diagnostics auto launch enabled", browser_diag.get("autoLaunch") is True, browser_diag)
    add("v027-integrated-capabilities", "browser diagnostics fallback enabled", browser_diag.get("fallbackEnabled") is True, browser_diag)
    add("v027-integrated-capabilities", "trusted Chrome DevTools launch is permission-scoped", "trusted_default_chrome_devtools" in permission_broker and "127.0.0.1:9222" in permission_broker and "chrome_devtools_mcp_args" in config_source and "--redactNetworkHeaders" in config_source)
    add("v027-integrated-capabilities", "PLAYWRIGHT_BROWSERS_PATH is set for runtime", bool(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")) or "PLAYWRIGHT_BROWSERS_PATH" in browser_auto, {"envSet": bool(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))})
    try:
        from agent.tools.ocr.ocr import OcrTool
        ocr_status, ocr_diag = safe_tool_payload(OcrTool().execute({"action": "diagnose"}))
    except Exception as exc:
        ocr_status = "error"
        ocr_diag = {"errorType": exc.__class__.__name__}
    add("v027-integrated-capabilities", "OCR diagnose succeeds", ocr_status == "success", ocr_diag)
    providers = ocr_diag.get("providers") if isinstance(ocr_diag, dict) else {}
    add("v027-integrated-capabilities", "OCR diagnose exposes local fallback providers", isinstance(providers, dict) and {"rapidocr", "pytesseract", "tesseractCli"}.issubset(providers.keys()), ocr_diag)
    add("v027-integrated-capabilities", "OCR source supports RapidOCR/Pillow/Tesseract fallback", all(marker in ocr_source for marker in ("rapidocr", "pytesseract", "tesseract-cli")))
    try:
        from agent.tools.vision.vision import Vision
        vision_ready = bool(Vision)
    except Exception:
        vision_ready = False
    add("v027-integrated-capabilities", "Vision tool imports and schema is available", vision_ready and "Analyze a local image" in vision_source)
    tools_payload = request("/api/tools", opener=opener, timeout=30).get("json")
    tools_text = json.dumps(tools_payload if isinstance(tools_payload, dict) else {}, ensure_ascii=False).lower()
    add("v027-integrated-capabilities", "tools endpoint exposes browser imagegen OCR and vision", all(marker in tools_text for marker in ("browser", "imagegen", "ocr", "vision")), {"toolsHash": h(tools_text)})

    tongxin_runtime_file = runtime / "tools" / "tongxin" / "xin_agent_cli.py"
    add("v027-integrated-capabilities", "bundled tools/tongxin/xin_agent_cli.py exists", tongxin_runtime_file.is_file() and tongxin_runtime_file.stat().st_size > 10000)
    add("v027-integrated-capabilities", "Tongxin wrapper exposes mpi_accuracy schema", "mpi_accuracy" in tongxin_source and "accuracy_check" in tongxin_source)
    add("v027-integrated-capabilities", "Tongxin source policy is MPI plus data-volume fail-closed", all(marker in tongxin_source for marker in ("factSource", "projectAccountSource", "cacheFallbackAllowedForMpi", "tongxin-data-volume", "DATABASE_CONFIG_KEYS", "XIN_AGENT_DATABASE", "ECOREX_TONGXIN_DATABASE")))
    add("v027-integrated-capabilities", "Tongxin chengfeng empty branch is explicit", "tongxin_mpi_accuracy_zero_project_samples" in tongxin_source and "chengfeng" in tongxin_source)
    try:
        from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
        tongxin = TongxinCli({"cwd": str(runtime)})
        tongxin_schema_status, tongxin_schema = safe_tool_payload(tongxin.execute({"action": "schema"}))
        tongxin_status_status, tongxin_status = safe_tool_payload(tongxin.execute({"action": "status"}))
        today = datetime.now(timezone.utc).date()
        start_date = (today - timedelta(days=7)).isoformat()
        end_date = today.isoformat()
        mpi_status, mpi_payload = safe_tool_payload(tongxin.execute({
            "action": "mpi_accuracy",
            "xhs_channel": "spotlight",
            "start_date": start_date,
            "end_date": end_date,
            "sample_limit": 2,
            "timeout": 240,
        }))
    except Exception as exc:
        tongxin_schema_status = tongxin_status_status = mpi_status = "error"
        tongxin_schema = tongxin_status = {}
        mpi_payload = {"errorType": exc.__class__.__name__, "errorHash": h(str(exc)), "message": str(exc)[:240]}
    mpi_policy = (mpi_payload.get("sourcePolicy") or {}) if isinstance(mpi_payload, dict) else {}
    mpi_counts = {
        "sampleCount": mpi_payload.get("sampleCount") if isinstance(mpi_payload, dict) else None,
        "comparableMetricCount": mpi_payload.get("comparableMetricCount") if isinstance(mpi_payload, dict) else None,
        "passedMetricCount": mpi_payload.get("passedMetricCount") if isinstance(mpi_payload, dict) else None,
        "mpiUnavailableCount": mpi_payload.get("mpiUnavailableCount") if isinstance(mpi_payload, dict) else None,
        "cacheFallbackDetected": mpi_payload.get("cacheFallbackDetected") if isinstance(mpi_payload, dict) else None,
        "dataVolume": mpi_payload.get("dataVolume") if isinstance(mpi_payload.get("dataVolume"), dict) else {},
        "errorType": mpi_payload.get("errorType") if isinstance(mpi_payload, dict) else "",
        "errorHash": mpi_payload.get("errorHash") if isinstance(mpi_payload, dict) else "",
        "message": str(mpi_payload.get("message") or "")[:240] if isinstance(mpi_payload, dict) else "",
    }
    add("v027-integrated-capabilities", "Tongxin structured schema/status succeed", tongxin_schema_status == "success" and tongxin_status_status == "success" and "mpi_accuracy" in json.dumps(tongxin_schema, ensure_ascii=False), {"schemaStatus": tongxin_schema_status, "statusStatus": tongxin_status_status})
    add("v027-integrated-capabilities", "Tongxin MPI accuracy action succeeds", mpi_status == "success" and mpi_payload.get("status") == "success", mpi_counts)
    add("v027-integrated-capabilities", "Tongxin MPI accuracy source policy is strict", mpi_policy.get("factSource") == "mpi" and mpi_policy.get("projectAccountSource") == "tongxin-data-volume" and mpi_policy.get("cacheFallbackAllowedForMpi") is False, mpi_policy)
    add("v027-integrated-capabilities", "Tongxin MPI accuracy has samples without fallback or drift", int(mpi_payload.get("sampleCount") or 0) > 0 and mpi_payload.get("cacheFallbackDetected") is False and int(mpi_payload.get("mpiUnavailableCount") or 0) == 0 and int(mpi_payload.get("comparableMetricCount") or 0) == int(mpi_payload.get("passedMetricCount") or -1), mpi_counts)


def phase_security_observability():
    diag = request("/api/diagnostics/bundle", timeout=40)
    logs = request("/api/logs/snapshot", timeout=40)
    public_checks = json.dumps(public_detail(CHECKS), ensure_ascii=False)
    violations = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(public_checks):
            violations.append(pattern.pattern)
    if PASSWORD and PASSWORD in public_checks:
        violations.append("raw-password")
    add("security-observability", "diagnostics bundle endpoint is gated or successful", diag["status"] in {200, 401, 403}, {"status": diag["status"]})
    add("security-observability", "logs snapshot endpoint is gated or successful", logs["status"] in {200, 401, 403}, {"status": logs["status"]})
    add("security-observability", "check evidence contains no raw secrets", not violations, {"violations": violations})
    add("security-observability", "model route evidence is redacted", "http://" not in json.dumps(public_detail(MODEL_ROUTE_EVIDENCE), ensure_ascii=False).lower(), {"count": len(MODEL_ROUTE_EVIDENCE)})
    add("security-observability", "observability evidence includes request/session hashes", bool(STATE_MACHINE_EVIDENCE.get("requestHash") is not None and STATE_MACHINE_EVIDENCE.get("sessionHash") is not None), STATE_MACHINE_EVIDENCE)
    add("security-observability", "new matrix reaches expected checks", len(CHECKS) + 1 == EXPECTED_NEW_CHECKS, {"checksBeforeFinal": len(CHECKS), "target": EXPECTED_NEW_CHECKS, "focusMode": FOCUS_MODE})
    SECURITY_EVIDENCE.update({"diagnosticsStatus": diag.get("status"), "logsStatus": logs.get("status"), "violations": violations})


def main():
    started = time.perf_counter()
    load_service_env()
    os.chdir("/opt/ecorex-web/current/runtime")
    sys.path.insert(0, "/opt/ecorex-web/current/runtime")
    try:
        from config import load_config
        load_config()
    except Exception:
        pass
    opener = None
    stream_session_id = ""
    try:
        if should_run("fresh-env"):
            phase_fresh_env()
        if should_run("auth-first-use"):
            opener = phase_auth_first_use()
        if should_run("runtime-api"):
            phase_runtime_api(opener)
        if should_run("ui-ux"):
            phase_ui_ux(opener)
        if should_run("stream-state-machine"):
            stream_session_id = phase_stream_state_machine(opener)
        if should_run("context-session"):
            phase_context_session(opener, stream_session_id or ("accept-stream-" + RUN_ID))
        if should_run("tool-skill"):
            phase_tool_skill(opener)
        if should_run("multi-model-image-route"):
            phase_multi_model_image_route(opener)
        if should_run("concurrency-pressure"):
            phase_concurrency_pressure()
        if should_run("v027-integrated-capabilities"):
            phase_v027_integrated_capabilities(opener, stream_session_id or ("accept-stream-" + RUN_ID))
        if should_run("security-observability"):
            phase_security_observability()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failures = [item for item in CHECKS if item.get("status") == "FAIL"]
    hard_failures = [item for item in CHECKS if item.get("hardGate") and item.get("status") != "PASS"]
    skips_without_reason = [item for item in CHECKS if item.get("status") == "SKIP" and not item.get("skipReason")]
    payload = {
        "status": "PASS" if len(CHECKS) == EXPECTED_NEW_CHECKS and not failures and not hard_failures and not skips_without_reason else "FAIL",
        "version": VERSION,
        "scope": "production-agent-product-focused-rerun" if FOCUS_MODE else "production-agent-product-fresh-user-305",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.perf_counter() - started, 2),
        "budgetMode": BUDGET_MODE,
        "releaseBlocking": not FOCUS_MODE,
        "focusGroups": sorted(FOCUS_GROUPS),
        "selectedGroups": [group for group in GROUP_ORDER if group in SELECTED_GROUPS],
        "expectedCheckCount": EXPECTED_NEW_CHECKS,
        "checkCount": len(CHECKS),
        "passCount": sum(1 for item in CHECKS if item.get("status") == "PASS"),
        "failCount": len(failures),
        "skipCount": sum(1 for item in CHECKS if item.get("status") == "SKIP"),
        "hardGateFailures": hard_failures[:12],
        "checks": CHECKS,
        "failurePreview": failures[:12],
        "pressureProfile": PRESSURE_EVIDENCE,
        "modelRouteEvidence": MODEL_ROUTE_EVIDENCE,
        "stateMachineEvidence": STATE_MACHINE_EVIDENCE,
        "securityEvidence": SECURITY_EVIDENCE,
        "target": {"rawTargetPersisted": False},
        "redaction": {
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
            "rawUrlPersisted": False,
            "rawUserPathPersisted": False,
        },
    }
    print("__REMOTE_MARKER__")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
"""


def _render_remote_script(
    *,
    budget_mode: str = "tiered",
    pressure_users: int = PRESSURE_USERS_DEFAULT,
    pressure_turns: int = PRESSURE_TURNS_DEFAULT,
    focus_groups: Optional[Iterable[str]] = None,
) -> str:
    expanded_focus_groups = expand_focus_groups(focus_groups or [])
    return (
        REMOTE_SCRIPT
        .replace("__VERSION__", VERSION)
        .replace("__BUDGET_MODE__", budget_mode)
        .replace("__PRESSURE_USERS__", str(int(pressure_users)))
        .replace("__PRESSURE_TURNS__", str(int(pressure_turns)))
        .replace("__FOCUS_GROUPS__", json.dumps(expanded_focus_groups, ensure_ascii=False))
        .replace("__REMOTE_MARKER__", REMOTE_MARKER)
    )


def run_agent_product_matrix(
    *,
    budget_mode: str = "tiered",
    pressure_users: int = PRESSURE_USERS_DEFAULT,
    pressure_turns: int = PRESSURE_TURNS_DEFAULT,
    focus_groups: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    with _temporary_env("ECOREX_DEPLOY_VERSION", VERSION):
        deploy_module = _load_deploy_module()
        deployer = deploy_module.ProductionDeploy()
    remote_script = _render_remote_script(
        budget_mode=budget_mode,
        pressure_users=pressure_users,
        pressure_turns=pressure_turns,
        focus_groups=focus_groups,
    )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=deployer.host,
        username=deployer.user,
        password=deployer.password,
        timeout=25,
        banner_timeout=25,
        auth_timeout=25,
        look_for_keys=False,
        allow_agent=False,
    )
    remote_tmp_root = "/srv/ecorex-agent-download/validation-tmp"
    remote_script_path = f"{remote_tmp_root}/agent-product-{sha_text(remote_script)[:16]}.py"
    try:
        mkdir_cmd = f"mkdir -p {shlex.quote(remote_tmp_root)}"
        _, mkdir_stdout, mkdir_stderr = client.exec_command(mkdir_cmd, timeout=60)
        del mkdir_stdout
        mkdir_err = mkdir_stderr.read().decode("utf-8", errors="replace")
        mkdir_code = mkdir_stderr.channel.recv_exit_status()
        if mkdir_code != 0:
            raise RuntimeError(f"remote validation tmp mkdir failed: {deployer.redact(mkdir_err)}")
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_script_path, "w") as handle:
                handle.write(remote_script)
        finally:
            sftp.close()
        command = (
            f"TMPDIR={shlex.quote(remote_tmp_root)} "
            f"/opt/ecorex-web/venv/bin/python {shlex.quote(remote_script_path)}; "
            f"code=$?; rm -f {shlex.quote(remote_script_path)}; exit $code"
        )
        _, stdout, stderr = client.exec_command(command, timeout=7200)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    try:
        payload = _extract_remote_json(out)
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "version": VERSION,
            "scope": "production-agent-product-fresh-user-305",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "errorType": exc.__class__.__name__,
            "error": _public_string(str(exc), limit=1000),
            "remoteExitCode": int(code),
            "remoteStdoutHash": sha_text(out),
            "remoteStderrHash": sha_text(err),
            "remoteStdoutExcerptRedacted": deployer.redact(out[-3000:]),
            "remoteStderrExcerptRedacted": deployer.redact(err[-3000:]),
            "target": {
                "domainHash": deployer.secret_hash(deployer.domain),
                "sshHostHash": deployer.secret_hash(deployer.host),
                "sshUserHash": deployer.secret_hash(deployer.user),
                "rawTargetPersisted": False,
            },
            "redaction": {
                "rawPasswordPersisted": False,
                "rawSecretPersisted": False,
                "rawUrlPersisted": False,
                "rawUserPathPersisted": False,
            },
        }
        return public_payload(payload)
    payload["remoteExitCode"] = int(code)
    payload["remoteStdoutHash"] = sha_text(out)
    payload["remoteStderrHash"] = sha_text(err)
    payload["remoteStderrExcerptRedacted"] = deployer.redact(err)
    payload["generatedLocallyAt"] = datetime.now(timezone.utc).isoformat()
    payload["target"] = {
        "domainHash": deployer.secret_hash(deployer.domain),
        "sshHostHash": deployer.secret_hash(deployer.host),
        "sshUserHash": deployer.secret_hash(deployer.user),
        "rawTargetPersisted": False,
    }
    payload["status"] = "PASS" if payload.get("status") == "PASS" and code == 0 else "FAIL"
    return public_payload(payload)


def build_suite_payload(
    payloads: List[Dict[str, Any]],
    *,
    started_at: float,
    budget_mode: str,
    pressure_users: int,
    pressure_turns: int,
) -> Dict[str, Any]:
    checks = normalize_checks(payloads)
    gates = evaluate_quality_gates(checks)
    redaction_violations = find_redaction_violations(_redaction_scan_payload({"checks": checks, "payloads": payloads}))
    agent_payload = next((payload for payload in payloads if payload.get("scope") == "production-agent-product-fresh-user-305"), {})
    status = gates["status"]
    if redaction_violations:
        status = "FAIL"
    payload = {
        **gates,
        "status": status,
        "version": VERSION,
        "scope": "production-agent-product-acceptance",
        "schemaVersion": "v0.2.7-agent-product-acceptance-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.perf_counter() - started_at, 2),
        "budgetMode": budget_mode,
        "pressureProfile": agent_payload.get("pressureProfile") or {
            "users": pressure_users,
            "turnsPerUser": pressure_turns,
        },
        "modelRouteEvidence": agent_payload.get("modelRouteEvidence") or [],
        "stateMachineEvidence": agent_payload.get("stateMachineEvidence") or {},
        "matrixChangeLog": [
            {
                "action": "baseline",
                "declaredNewCases": len(DECLARED_CASE_REGISTRY),
                "targetTotalChecks": TARGET_TOTAL_CHECKS,
                "minimumEnabledChecks": MIN_ENABLED_CHECKS,
                "developerAgentMayRemove": "Only removable=true P2 cases; P0/P1 and domain minimums must remain.",
            }
        ],
        "declaredCaseSummary": {
            "newCaseCount": len(DECLARED_CASE_REGISTRY),
            "groups": [
                {"group": group, "count": count, "priority": priority, "cost": cost}
                for group, count, priority, cost, _tags in NEW_CASE_GROUPS
            ],
        },
        "checks": checks,
        "failurePreview": [item for item in checks if item.get("status") == "FAIL"][:20],
        "redaction": {
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
            "rawUrlPersisted": False,
            "rawUserPathPersisted": False,
            "violations": redaction_violations,
        },
    }
    return public_payload(payload)


def run_suite(
    *,
    budget_mode: str = "tiered",
    pressure_users: int = PRESSURE_USERS_DEFAULT,
    pressure_turns: int = PRESSURE_TURNS_DEFAULT,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    started = time.perf_counter()
    payloads: List[Dict[str, Any]] = []
    if include_legacy:
        payloads.append(_legacy_run_payload(LEGACY_USER_BEHAVIOR_SCRIPT))
        payloads.append(_legacy_run_payload(LEGACY_IMAGE_TOOLCHAIN_SCRIPT))
    payloads.append(
        run_agent_product_matrix(
            budget_mode=budget_mode,
            pressure_users=pressure_users,
            pressure_turns=pressure_turns,
        )
    )
    return build_suite_payload(
        payloads,
        started_at=started,
        budget_mode=budget_mode,
        pressure_users=pressure_users,
        pressure_turns=pressure_turns,
    )


def _case_registry_summary() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "targetNewChecks": TARGET_NEW_CHECKS,
        "targetTotalChecks": TARGET_TOTAL_CHECKS,
        "minimumEnabledChecks": MIN_ENABLED_CHECKS,
        "newCaseCount": len(DECLARED_CASE_REGISTRY),
        "groups": [
            {"group": group, "count": count, "priority": priority, "cost": cost, "tags": list(tags)}
            for group, count, priority, cost, tags in NEW_CASE_GROUPS
        ],
        "cases": DECLARED_CASE_REGISTRY,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget-mode",
        choices=("tiered", "full-real", "no-external-models"),
        default=os.environ.get("ECOREX_ACCEPTANCE_BUDGET_MODE", "tiered"),
        help="Model/image budget mode. Default: tiered.",
    )
    parser.add_argument("--pressure-users", type=int, default=int(os.environ.get("ECOREX_ACCEPTANCE_PRESSURE_USERS", PRESSURE_USERS_DEFAULT)))
    parser.add_argument("--pressure-turns", type=int, default=int(os.environ.get("ECOREX_ACCEPTANCE_PRESSURE_TURNS", PRESSURE_TURNS_DEFAULT)))
    parser.add_argument("--output", type=Path, default=ARTIFACT)
    parser.add_argument("--list-cases", action="store_true", help="Print declared new-case registry and exit without SSH.")
    parser.add_argument("--skip-legacy", action="store_true", help="Run only the new 305-case matrix. Intended for debugging; full gate will fail total-count requirements.")
    parser.add_argument(
        "--focus-groups",
        default="",
        help="Comma-separated new-matrix groups for a non-release-blocking focused rerun after a fix.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_cases:
        print(json.dumps(_case_registry_summary(), ensure_ascii=False, indent=2))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        focus_groups = parse_focus_groups(args.focus_groups)
        if focus_groups:
            payload = run_agent_product_matrix(
                budget_mode=args.budget_mode,
                pressure_users=max(1, int(args.pressure_users)),
                pressure_turns=max(1, int(args.pressure_turns)),
                focus_groups=focus_groups,
            )
            payload["scope"] = "production-agent-product-focused-rerun"
            payload["releaseDecision"] = "not-authoritative-run-full-real-release-gate-before-promotion"
            payload["matrixChangeLog"] = [
                {
                    "action": "focused-rerun",
                    "requestedGroups": focus_groups,
                    "selectedGroups": expand_focus_groups(focus_groups),
                    "releaseBlocking": False,
                    "finalGateRequiredBeforePromotion": True,
                }
            ]
        else:
            payload = run_suite(
                budget_mode=args.budget_mode,
                pressure_users=max(1, int(args.pressure_users)),
                pressure_turns=max(1, int(args.pressure_turns)),
                include_legacy=not args.skip_legacy,
            )
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "version": VERSION,
            "scope": "production-agent-product-acceptance",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "errorType": exc.__class__.__name__,
            "error": _public_string(str(exc), limit=1000),
            "redaction": {
                "rawPasswordPersisted": False,
                "rawSecretPersisted": False,
                "rawUrlPersisted": False,
                "rawUserPathPersisted": False,
            },
        }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": payload.get("status"),
        "artifact": str(args.output),
        "checkCount": payload.get("checkCount"),
        "enabledCheckCount": payload.get("enabledCheckCount"),
        "passCount": payload.get("passCount"),
        "failCount": payload.get("failCount"),
        "skipCount": payload.get("skipCount"),
        "durationSeconds": payload.get("durationSeconds"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
