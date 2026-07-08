#!/usr/bin/env python3
"""Lightweight preflight for the EcoreX real release validation gate.

This script is intentionally cheap and local-only:
  - no SSH
  - no model calls
  - no image generation
  - no concurrency pressure

Use it during everyday development.  Use scripts/真实发布校验.py only after a
release candidate is deployed to the real production server.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import py_compile
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.8"
DEFAULT_OUTPUT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "real-release-light-validation.json"
HEAVY_SCRIPT = ROOT / "scripts" / "smoke-v026-production-agent-product-acceptance.py"
HEAVY_WRAPPER = ROOT / "scripts" / "真实发布校验.py"
LEGACY_UPGRADE_SCRIPT = ROOT / "scripts" / "smoke-v028-legacy-webui-online-upgrade.ps1"
LIGHT_WRAPPER = ROOT / "scripts" / "真实发布轻量校验.py"
MULTI_AGENT_STRATEGY = ROOT / "scripts" / "real-release-multi-agent-strategy.py"
MULTI_AGENT_WRAPPER = ROOT / "scripts" / "真实发布多Agent分工策略.py"
MULTI_AGENT_DOC = ROOT / "docs" / f"v{VERSION}" / "real-release-multi-agent-strategy.md"
RERUN_STRATEGY = ROOT / "scripts" / "real-release-rerun-strategy.py"
RERUN_WRAPPER = ROOT / "scripts" / "真实发布失败复验策略.py"
RERUN_DOC = ROOT / "docs" / f"v{VERSION}" / "real-release-rerun-strategy.md"

REQUIRED_FILES = [
    HEAVY_SCRIPT,
    HEAVY_WRAPPER,
    LEGACY_UPGRADE_SCRIPT,
    MULTI_AGENT_STRATEGY,
    MULTI_AGENT_WRAPPER,
    MULTI_AGENT_DOC,
    RERUN_STRATEGY,
    RERUN_WRAPPER,
    RERUN_DOC,
    ROOT / "scripts" / "smoke-v026-production-200-user-behavior.py",
    ROOT / "scripts" / "smoke-v026-production-30-image-ocr-vision-toolchain.py",
    ROOT / "tests" / "test_v026_agent_product_acceptance.py",
    ROOT / "tests" / "test_v028_runtime_queue_observation.py",
    ROOT / "channel" / "web" / "routes.py",
    ROOT / "channel" / "web" / "web_channel.py",
    ROOT / "channel" / "web" / "sessions.py",
    ROOT / "channel" / "web" / "sse.py",
    ROOT / "channel" / "web" / "image_jobs.py",
    ROOT / "agent" / "protocol" / "task_observer.py",
    ROOT / "agent" / "protocol" / "run_ledger.py",
    ROOT / "agent" / "protocol" / "runtime_projection.py",
    ROOT / "agent" / "protocol" / "image_job_service.py",
    ROOT / "agent" / "protocol" / "agent_stream.py",
    ROOT / "agent" / "memory" / "conversation_store.py",
    ROOT / "agent" / "tools" / "imagegen" / "imagegen.py",
    ROOT / "agent" / "tools" / "ocr" / "ocr.py",
    ROOT / "agent" / "tools" / "vision" / "vision.py",
    ROOT / "agent" / "tools" / "tongxin_cli" / "tongxin_cli.py",
    ROOT / "agent" / "tools" / "ecorex_cli" / "ecorex_cli.py",
    ROOT / "agent" / "tools" / "mcp" / "mcp_tool.py",
    ROOT / "agent" / "skills" / "service.py",
    ROOT / "agent" / "skills" / "manager.py",
    ROOT / "desktop" / "scripts" / "stage-runtime-win.ps1",
    ROOT / "desktop" / "scripts" / "stage-runtime-mac.sh",
    ROOT / "scripts" / "validate-ecorex-release-artifacts.py",
    ROOT / "scripts" / "scan-session-artifacts-privacy.py",
    ROOT / "config.py",
    ROOT / "config-template.json",
    ROOT / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py",
    ROOT / "deploy" / "ecorex-site" / "nginx" / "ecorex-agent.conf.example",
    ROOT / "deploy" / "ecorex-site" / "nginx" / "ecorex-web.conf.example",
    ROOT / "deploy" / "ecorex-site" / "caddy" / "ecorex-agent.routes.caddy",
    ROOT / "deploy" / "ecorex-site" / "caddy" / "ecorex-web.routes.caddy",
    ROOT / "docs" / "v0.2.8" / "development-log.md",
    ROOT / "docs" / "v0.2.8" / "runtime-observability-and-queue-architecture.md",
]

REQUIRED_ROUTES = [
    "/auth/login",
    "/auth/check",
    "/message",
    "/stream",
    "/cancel",
    "/api/active-requests",
    "/api/runtime-projection",
    "/api/image-jobs",
    "/api/tool-permissions",
    "/api/models",
    "/api/tools",
    "/api/skills",
    "/api/sessions",
    "/api/history",
    "/api/diagnostics/bundle",
]

REQUIRED_HEAVY_MARKERS = [
    "Do not use stored memories or prior conversation context.",
    "direct tool probes bypass conversational memory",
    "gpt-image-2-pro",
    "images.generations",
    "images.edits",
    "SkillService",
    "EcoreXCli",
    "list_mcp_status",
    "PRESSURE_USERS_DEFAULT = 20",
    "TARGET_NEW_CHECKS = 345",
    "TARGET_TOTAL_CHECKS = 577",
    "v027-integrated-capabilities",
    "v028-runtime-observability-queue",
    "TaskObserver",
    "QueuedRequestPayloadStore",
    "claim_queued_run",
    "task_observations",
    "queue-action",
    "guide_queue",
    "Image job observer uses 120s per-image baseline with status leases",
    "Image jobs default multi-image work to two bounded lanes",
    "Native imagegen batch runs through bounded parallel executor",
    "continue extend background",
    "modelAliasFamily",
    "isOfficialGeminiProvider",
    "isCustomGeminiEndpoint",
    "contextContinuity",
    "artifactHistoryRefs",
    "custom Gemini switched model produces content",
    "_stream_chunks_from_chat_response",
    "model-switch-divider",
    "model-switch-message",
    "IMAGEGEN_SHELL_SEMANTIC_SIGNAL_REGEXES",
    "_emit_batch_image_ready",
    "session-summary-send-time",
    "admin release page exposes one-click publish controls",
    "mpi_accuracy",
    "cacheFallbackAllowedForMpi",
    "PLAYWRIGHT_BROWSERS_PATH",
    "tools/tongxin/xin_agent_cli.py",
    "public manifest is promoted to current stable version",
    "update-check endpoint exposes WebUI update policy and artifacts",
    "CDP action hit stale connection; reconnecting once",
    "--focus-groups",
    "production-agent-product-focused-rerun",
]

REQUIRED_WRAPPER_MARKERS = [
    "smoke-v028-legacy-webui-online-upgrade.ps1",
    "--skip-legacy-upgrade",
    "LEGACY_UPGRADE_TARGET",
]

REQUIRED_LEGACY_UPGRADE_MARKERS = [
    "legacy-webui-online-upgrade",
    "current-runtime.txt",
    "0.2.7.1",
    "0.2.7.2",
    "legacy runtime receives v0.2.8 update notification",
    "legacy runtime upgrades online to v0.2.8",
]

REQUIRED_DOC_MARKERS = [
    "真实发布校验",
    "真实发布轻量校验",
    "真实发布多Agent分工策略",
    "并发多 Agent 分工策略",
    "真实发布失败复验策略",
    "focused rerun",
    "部署后、推广前",
    "主动询问用户是否运行真实发布校验",
    "最终发布结论仍必须由单次完整",
]


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {path}")
    spec.loader.exec_module(module)
    return module


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _sha_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()[:16]


def _imports_module(path: Path, module_name: str) -> bool:
    try:
        tree = ast.parse(_read_text(path))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_name or alias.name.startswith(f"{module_name}.") for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == module_name or node.module.startswith(f"{module_name}."):
                return True
    return False


def _add(checks: List[Dict[str, Any]], group: str, name: str, ok: bool, detail: Optional[Dict[str, Any]] = None) -> None:
    checks.append(
        {
            "index": len(checks) + 1,
            "group": group,
            "name": name,
            "status": "PASS" if ok else "FAIL",
            "detail": detail or {},
        }
    )


def _compile_python(path: Path) -> Dict[str, Any]:
    try:
        py_compile.compile(str(path), doraise=True)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "errorType": exc.__class__.__name__, "message": str(exc)[:240]}


def _check_files(checks: List[Dict[str, Any]]) -> None:
    for path in REQUIRED_FILES:
        _add(
            checks,
            "files",
            f"{path.relative_to(ROOT)} exists",
            path.is_file(),
            {"path": str(path.relative_to(ROOT)).replace("\\", "/")},
        )


def _check_compilation(checks: List[Dict[str, Any]]) -> None:
    for path in [
        HEAVY_SCRIPT,
        HEAVY_WRAPPER,
        LIGHT_WRAPPER,
        MULTI_AGENT_STRATEGY,
        MULTI_AGENT_WRAPPER,
        RERUN_STRATEGY,
        RERUN_WRAPPER,
        ROOT / "tests" / "test_v026_agent_product_acceptance.py",
    ]:
        result = _compile_python(path)
        _add(checks, "compile", f"{path.relative_to(ROOT)} compiles", result["ok"], result)

    try:
        module = _load_script(HEAVY_SCRIPT)
        remote = (
            module.REMOTE_SCRIPT
            .replace("__VERSION__", VERSION)
            .replace("__BUDGET_MODE__", "tiered")
            .replace("__PRESSURE_USERS__", "20")
            .replace("__PRESSURE_TURNS__", "3")
            .replace("__FOCUS_GROUPS__", "[]")
            .replace("__REMOTE_MARKER__", module.REMOTE_MARKER)
        )
        compile(remote, "<remote-real-release-validation>", "exec")
        remote_ok = True
        remote_detail = {"remoteScriptHash": _sha_text(remote), "chars": len(remote)}
    except Exception as exc:
        remote_ok = False
        remote_detail = {"errorType": exc.__class__.__name__, "message": str(exc)[:240]}
    _add(checks, "compile", "remote injected real release validation script compiles", remote_ok, remote_detail)

    try:
        module = _load_script(HEAVY_SCRIPT)
        focused_remote = module._render_remote_script(
            budget_mode="tiered",
            pressure_users=2,
            pressure_turns=1,
            focus_groups=["stream-state-machine", "context-session"],
        )
        compile(focused_remote, "<remote-focused-real-release-rerun>", "exec")
        focused_ok = True
        focused_detail = {"remoteScriptHash": _sha_text(focused_remote), "chars": len(focused_remote)}
    except Exception as exc:
        focused_ok = False
        focused_detail = {"errorType": exc.__class__.__name__, "message": str(exc)[:240]}
    _add(checks, "compile", "remote focused rerun script compiles", focused_ok, focused_detail)


def _check_matrix(checks: List[Dict[str, Any]]) -> None:
    module = None
    try:
        module = _load_script(HEAVY_SCRIPT)
        registry = module.DECLARED_CASE_REGISTRY
        p0 = [case for case in registry if case.get("priority") == "P0"]
        p2 = [case for case in registry if case.get("priority") == "P2"]
        matrix_ok = True
        detail = {
            "newCaseCount": len(registry),
            "targetTotalChecks": module.TARGET_TOTAL_CHECKS,
            "minimumEnabledChecks": module.MIN_ENABLED_CHECKS,
            "p0Count": len(p0),
            "p2Count": len(p2),
        }
    except Exception as exc:
        registry = []
        p0 = []
        p2 = []
        matrix_ok = False
        detail = {"errorType": exc.__class__.__name__, "message": str(exc)[:240]}

    _add(checks, "matrix", f"declared new case count is {detail.get('newCaseCount')}", matrix_ok and len(registry) == detail.get("newCaseCount") == getattr(module, "TARGET_NEW_CHECKS", 0), detail)
    _add(checks, "matrix", f"target total check count is {detail.get('targetTotalChecks')}", matrix_ok and detail.get("targetTotalChecks") == getattr(module, "TARGET_TOTAL_CHECKS", 0), detail)
    _add(checks, "matrix", f"minimum enabled check count is {detail.get('minimumEnabledChecks')}", matrix_ok and detail.get("minimumEnabledChecks") == getattr(module, "MIN_ENABLED_CHECKS", 0), detail)
    _add(checks, "matrix", "P0 hard gates are present", matrix_ok and len(p0) >= 200, detail)
    _add(checks, "matrix", "P2 removable cases are present", matrix_ok and len(p2) > 0, detail)
    _add(checks, "matrix", "P0 cases are not removable", matrix_ok and all(not case.get("removable") and case.get("hardGate") for case in p0), detail)
    _add(checks, "matrix", "P2 cases are removable", matrix_ok and all(case.get("removable") and not case.get("hardGate") for case in p2), detail)


def _check_routes(checks: List[Dict[str, Any]]) -> None:
    routes = _read_text(ROOT / "channel" / "web" / "routes.py")
    for route in REQUIRED_ROUTES:
        _add(checks, "routes", f"route {route} is declared", route in routes)


def _check_markers(checks: List[Dict[str, Any]]) -> None:
    heavy = _read_text(HEAVY_SCRIPT)
    for marker in REQUIRED_HEAVY_MARKERS:
        _add(checks, "real-release-contract", f"heavy gate contains marker: {marker}", marker in heavy)

    heavy_wrapper = _read_text(HEAVY_WRAPPER)
    for marker in REQUIRED_WRAPPER_MARKERS:
        _add(checks, "real-release-contract", f"heavy wrapper contains marker: {marker}", marker in heavy_wrapper)

    legacy_upgrade = _read_text(LEGACY_UPGRADE_SCRIPT)
    for marker in REQUIRED_LEGACY_UPGRADE_MARKERS:
        _add(checks, "real-release-contract", f"legacy upgrade smoke contains marker: {marker}", marker in legacy_upgrade)

    docs_text = "\n".join(
        [
            _read_text(ROOT / "docs" / "ecorex-dev-log.md"),
            _read_text(ROOT / "docs" / "ecorex-acceptance-checklist.md"),
            _read_text(MULTI_AGENT_DOC),
            _read_text(RERUN_DOC),
            _read_text(ROOT / "CONTRIBUTING.md"),
        ]
    )
    for marker in REQUIRED_DOC_MARKERS:
        _add(checks, "development-standard", f"development standard records: {marker}", marker in docs_text)


def _check_v028_local_markers(checks: List[Dict[str, Any]]) -> None:
    app_source = _read_text(ROOT / "desktop" / "src" / "App.tsx")
    api_source = _read_text(ROOT / "desktop" / "src" / "services" / "ecorexApi.ts")
    web_channel = _read_text(ROOT / "channel" / "web" / "web_channel.py")
    image_job_service = _read_text(ROOT / "agent" / "protocol" / "image_job_service.py")
    imagegen_tool = _read_text(ROOT / "agent" / "tools" / "imagegen" / "imagegen.py")
    runtime_projection = _read_text(ROOT / "agent" / "protocol" / "runtime_projection.py")
    config_source = _read_text(ROOT / "config.py")
    config_template = _read_text(ROOT / "config-template.json")
    admin_api = _read_text(ROOT / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py")
    nginx_agent = _read_text(ROOT / "deploy" / "ecorex-site" / "nginx" / "ecorex-agent.conf.example")
    nginx_web = _read_text(ROOT / "deploy" / "ecorex-site" / "nginx" / "ecorex-web.conf.example")
    caddy_agent = _read_text(ROOT / "deploy" / "ecorex-site" / "caddy" / "ecorex-agent.routes.caddy")
    caddy_web = _read_text(ROOT / "deploy" / "ecorex-site" / "caddy" / "ecorex-web.routes.caddy")
    admin_tests = _read_text(ROOT / "tests" / "test_ecorex_admin_device_id.py")
    image_tests = "\n".join(
        [
            _read_text(ROOT / "tests" / "test_v028_runtime_queue_observation.py"),
            _read_text(ROOT / "tests" / "test_v024_image_quality_runtime.py"),
        ]
    )

    _add(
        checks,
        "v028-local-contract",
        "queued message surface exposes user-triggered guide action",
        all(marker in app_source for marker in ("handleGuideQueuedMessage", "queuedGuidancePhase", "重新观测并确认是否插入队列"))
        and '"guide_queue"' in api_source
        and all(marker in web_channel for marker in ("def _guide_queued_request", '"guide_queue"', '"inserted"')),
    )
    _add(
        checks,
        "v028-local-contract",
        "image observation uses 120s baseline and status-driven deadline extensions",
        all(marker in image_job_service for marker in ("IMAGE_JOB_BASELINE_SECONDS = 120.0", "image_job_observation_per_image_baseline_seconds", "provider_polling", "deadline_extended"))
        and all(marker in image_tests for marker in ("test_image_job_observation_uses_two_minute_single_image_baseline", "test_image_job_status_events_extend_observation_deadline")),
    )
    _add(
        checks,
        "v028-local-contract",
        "image generation defaults batch speed to two bounded lanes",
        all(marker in image_job_service for marker in ("image_job_default_max_parallel", "parallelism_defaulted", "default_max_parallel"))
        and all(marker in imagegen_tool for marker in ("ThreadPoolExecutor", "resolve_image_job_parallelism_policy", '"maxParallel"', '"parallelismPolicy"'))
        and '"image_job_default_max_parallel": 2' in config_source
        and '"image_job_default_max_parallel": 2' in config_template
        and all(marker in image_tests for marker in ("test_image_job_parallelism_defaults_batch_to_two_lanes", "test_imagegen_tool_batches_tasks_with_default_parallel_lanes")),
    )
    _add(
        checks,
        "v028-local-contract",
        "runtime projection preserves image parallelism default fields",
        all(marker in runtime_projection for marker in ("default_max_parallel", "parallelism_defaulted", "parallelism_clamped")),
    )
    _add(
        checks,
        "v028-local-contract",
        "session share public URL and reverse-proxy routes avoid bare 404",
        all(marker in admin_api for marker in ("ECOREX_PUBLIC_CLIENT_BASE_URL", "ECOREX_PUBLIC_BASE_URL", "X-Forwarded-Prefix", "/client/session-shares/"))
        and all(marker in nginx_agent for marker in ("location ^~ /client/session-shares/", "proxy_pass $ecorex_admin_api/client/session-shares/", "X-Forwarded-Prefix /ecorex-agent"))
        and all(marker in nginx_web for marker in ("location ^~ /client/session-shares/", "proxy_pass http://127.0.0.1:18084/client/session-shares/", "X-Forwarded-Prefix /ecorex-agent"))
        and "handle /client/session-shares/*" in caddy_agent
        and "handle /client/session-shares/*" in caddy_web
        and all(marker in admin_tests for marker in ("test_session_share_url_uses_public_ecorex_agent_client_prefix", "test_session_share_url_infers_public_prefix_for_forwarded_production_host")),
    )


def _check_multi_agent_strategy(checks: List[Dict[str, Any]]) -> None:
    try:
        module = _load_script(MULTI_AGENT_STRATEGY)
        strategy = module.build_strategy(max_parallel_agents=4)
        detail = {
            "status": strategy.get("status"),
            "feasibility": strategy.get("feasibility"),
            "laneCount": len(strategy.get("lanes") or []),
            "waveCount": len(strategy.get("waves") or []),
            "finalGateCaseCount": strategy.get("finalGateCaseCount"),
            "validation": strategy.get("validation"),
        }
        ok = strategy.get("status") == "PASS"
    except Exception as exc:
        strategy = {}
        detail = {"errorType": exc.__class__.__name__, "message": str(exc)[:240]}
        ok = False
    lane_ids = {lane.get("id") for lane in strategy.get("lanes") or []}
    _add(checks, "multi-agent-strategy", "multi-agent strategy builds", ok, detail)
    _add(checks, "multi-agent-strategy", "strategy declares final full gate lane", "coordinator-final-real-release-gate" in lane_ids, detail)
    _add(checks, "multi-agent-strategy", "strategy declares serial pressure lane", "agent-g-concurrency-pressure" in lane_ids, detail)
    _add(checks, "multi-agent-strategy", "strategy uses final serial gate feasibility", strategy.get("feasibility") == "FEASIBLE_WITH_FINAL_SERIAL_GATE", detail)


def _check_rerun_strategy(checks: List[Dict[str, Any]]) -> None:
    synthetic_report = {
        "status": "FAIL",
        "checks": [
            {
                "caseId": "synthetic-stream",
                "group": "stream-state-machine",
                "name": "synthetic stream failure",
                "status": "FAIL",
                "priority": "P0",
                "hardGate": True,
            },
            {
                "caseId": "synthetic-context",
                "group": "context-session",
                "name": "synthetic context failure",
                "status": "FAIL",
                "priority": "P0",
                "hardGate": True,
            },
        ],
    }
    try:
        module = _load_script(RERUN_STRATEGY)
        strategy = module.build_strategy(report=synthetic_report)
        detail = {
            "status": strategy.get("status"),
            "action": strategy.get("action"),
            "failureGroups": strategy.get("failureGroups"),
            "selectedGroups": strategy.get("selectedGroups"),
            "mustRunFullGateBeforePromotion": strategy.get("mustRunFullGateBeforePromotion"),
        }
        ok = strategy.get("status") == "PASS"
    except Exception as exc:
        strategy = {}
        detail = {"errorType": exc.__class__.__name__, "message": str(exc)[:240]}
        ok = False
    commands = " ".join(command.get("command", "") for command in strategy.get("commands") or [])
    selected = set(strategy.get("selectedGroups") or [])
    _add(checks, "rerun-strategy", "failed-report rerun strategy builds", ok, detail)
    _add(checks, "rerun-strategy", "rerun strategy expands required dependencies", {"fresh-env", "auth-first-use", "stream-state-machine", "context-session"}.issubset(selected), detail)
    _add(checks, "rerun-strategy", "rerun strategy emits focus-groups command", "--focus-groups" in commands, detail)
    _add(checks, "rerun-strategy", "rerun strategy requires final full gate before promotion", strategy.get("mustRunFullGateBeforePromotion") is True, detail)


def _check_wrapper_commands(checks: List[Dict[str, Any]]) -> None:
    heavy_wrapper_text = _read_text(HEAVY_WRAPPER)
    light_wrapper_text = _read_text(LIGHT_WRAPPER)
    multi_agent_wrapper_text = _read_text(MULTI_AGENT_WRAPPER)
    rerun_wrapper_text = _read_text(RERUN_WRAPPER)
    _add(checks, "commands", "Chinese heavy command delegates to heavy implementation", "smoke-v026-production-agent-product-acceptance.py" in heavy_wrapper_text)
    _add(checks, "commands", "Chinese light command delegates to light implementation", "light-real-release-validation.py" in light_wrapper_text)
    _add(checks, "commands", "Chinese multi-agent strategy command delegates to strategy implementation", "real-release-multi-agent-strategy.py" in multi_agent_wrapper_text)
    _add(checks, "commands", "Chinese failed-rerun strategy command delegates to rerun implementation", "real-release-rerun-strategy.py" in rerun_wrapper_text)
    _add(checks, "commands", "light script has no Paramiko dependency", not _imports_module(ROOT / "scripts" / "light-real-release-validation.py", "paramiko"))


def build_report() -> Dict[str, Any]:
    started = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    _check_files(checks)
    _check_compilation(checks)
    _check_matrix(checks)
    _check_routes(checks)
    _check_markers(checks)
    _check_v028_local_markers(checks)
    _check_multi_agent_strategy(checks)
    _check_rerun_strategy(checks)
    _check_wrapper_commands(checks)
    failures = [check for check in checks if check["status"] != "PASS"]
    return {
        "status": "PASS" if not failures else "FAIL",
        "schemaVersion": "real-release-light-validation-v1",
        "version": VERSION,
        "scope": "real-release-light-validation",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "durationSeconds": round(time.perf_counter() - started, 2),
        "checkCount": len(checks),
        "passCount": sum(1 for check in checks if check["status"] == "PASS"),
        "failCount": len(failures),
        "checks": checks,
        "failurePreview": failures[:12],
        "redaction": {
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
            "rawUrlPersisted": False,
            "rawUserPathPersisted": False,
        },
        "commands": {
            "light": "python scripts/真实发布轻量校验.py",
            "strategy": "python scripts/真实发布多Agent分工策略.py",
            "rerun": "python scripts/真实发布失败复验策略.py",
            "heavy": "python scripts/真实发布校验.py",
        },
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact": str(args.output),
                "checkCount": report["checkCount"],
                "passCount": report["passCount"],
                "failCount": report["failCount"],
                "durationSeconds": report["durationSeconds"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
