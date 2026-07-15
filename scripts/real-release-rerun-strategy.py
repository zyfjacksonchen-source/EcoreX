#!/usr/bin/env python3
"""Generate a focused rerun plan after a real release validation failure.

This script is local-only.  It reads a failed report, classifies the failing
groups, expands their dependency chain, and emits the cheapest safe rerun plan.
Focused reruns are proof-of-fix evidence only; the final full real release gate
is still required before promotion.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.8"
DEFAULT_REPORT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-agent-product-acceptance.json"
DEFAULT_OUTPUT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "real-release-rerun-strategy.json"
HEAVY_SCRIPT = ROOT / "scripts" / "smoke-v026-production-agent-product-acceptance.py"

HIGH_COST_GROUPS = {"stream-state-machine", "tool-skill", "multi-model-image-route", "concurrency-pressure", "v027-integrated-capabilities", "v028-runtime-observability-queue"}
SERIAL_GROUPS = {"concurrency-pressure", "v027-integrated-capabilities", "v028-runtime-observability-queue"}
IMAGE_ROUTE_GROUPS = {"multi-model-image-route", "v027-integrated-capabilities"}
STATEFUL_GROUPS = {"runtime-api", "stream-state-machine", "context-session", "concurrency-pressure", "v027-integrated-capabilities", "v028-runtime-observability-queue"}


def _load_heavy_module():
    spec = importlib.util.spec_from_file_location("smoke_v026_agent_product_acceptance", HEAVY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {HEAVY_SCRIPT}")
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "UNREADABLE", "errorType": exc.__class__.__name__, "error": str(exc)[:240]}


def _public_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except Exception:
        return f"[external-path:{path.name}]"


def _split_groups(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _failure_checks(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    failures: List[Dict[str, Any]] = []
    for bucket in ("checks", "hardGateFailures", "p1Failures", "p2Failures", "failurePreview"):
        for item in report.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").upper()
            hard_gate = bool(item.get("hardGate"))
            if status != "FAIL" and not (hard_gate and status != "PASS"):
                continue
            key = str(item.get("caseId") or item.get("id") or item.get("name") or len(failures))
            if key in seen:
                continue
            seen.add(key)
            failures.append(item)
    return failures


def _groups_from_failures(failures: Iterable[Dict[str, Any]], valid_groups: Set[str]) -> List[str]:
    groups = []
    for item in failures:
        group = str(item.get("group") or "").strip()
        if group in valid_groups and group not in groups:
            groups.append(group)
    return groups


def _priority_counts(failures: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0, "unknown": 0}
    for item in failures:
        priority = str(item.get("priority") or "").upper()
        if priority in counts:
            counts[priority] += 1
        elif item.get("hardGate"):
            counts["P0"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _focused_command(groups: List[str], output_name: str) -> str:
    return (
        "python scripts/真实发布校验.py "
        f"--focus-groups {','.join(groups)} "
        "--skip-legacy "
        f"--output docs/v0.2.8/artifacts/{output_name}"
    )


def build_strategy(
    report: Optional[Dict[str, Any]] = None,
    *,
    report_path: Optional[Path] = None,
    manual_groups: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    heavy = _load_heavy_module()
    valid_groups = set(heavy.NEW_CASE_GROUP_NAMES)
    manual = [group for group in manual_groups or [] if group]
    unknown = sorted(set(manual) - valid_groups)
    if unknown:
        return {
            "status": "FAIL",
            "schemaVersion": "real-release-rerun-strategy-v1",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "error": f"Unknown group(s): {', '.join(unknown)}",
            "validGroups": list(heavy.NEW_CASE_GROUP_NAMES),
        }

    loaded_report = report if report is not None else _read_json(report_path or DEFAULT_REPORT)
    failures = _failure_checks(loaded_report)
    failure_groups = manual or _groups_from_failures(failures, valid_groups)
    selected_groups = heavy.expand_focus_groups(failure_groups)
    priority_counts = _priority_counts(failures)
    has_failure_report = bool(loaded_report)
    has_failures = bool(failures or manual)
    needs_pressure = bool(set(selected_groups) & SERIAL_GROUPS)
    needs_image_route = bool(set(selected_groups) & IMAGE_ROUTE_GROUPS)
    needs_stateful = bool(set(selected_groups) & STATEFUL_GROUPS)

    commands: List[Dict[str, Any]] = [
        {
            "step": 1,
            "name": "local contract preflight",
            "command": "python scripts/真实发布轻量校验.py",
            "reason": "Catch syntax, matrix, wrapper, docs, and strategy regressions before touching production.",
            "releaseBlocking": False,
        }
    ]
    if selected_groups:
        commands.append(
            {
                "step": 2,
                "name": "focused proof-of-fix rerun",
                "command": _focused_command(selected_groups, "real-release-focused-rerun.json"),
                "reason": "Rerun only failed groups plus required setup/dependency groups.",
                "releaseBlocking": False,
            }
        )
    if needs_pressure:
        commands.append(
            {
                "step": len(commands) + 1,
                "name": "serial pressure confirmation",
                "command": _focused_command(["concurrency-pressure"], "real-release-focused-pressure-rerun.json"),
                "reason": "Pressure tests must be serial and must prove active requests drain to zero.",
                "releaseBlocking": False,
            }
        )
    commands.append(
        {
            "step": len(commands) + 1,
            "name": "final full real release gate",
            "command": "python scripts/真实发布校验.py",
            "reason": "Run once after fixes are batched and focused proof-of-fix evidence is green.",
            "releaseBlocking": True,
        }
    )

    action = "NO_FAILURES_FOUND" if has_failure_report and not has_failures else "FOCUSED_RERUN_THEN_FINAL_GATE"
    if not has_failure_report:
        action = "NO_REPORT_FOUND_USE_MANUAL_GROUPS_OR_RUN_FULL_GATE"
    return {
        "status": "PASS",
        "schemaVersion": "real-release-rerun-strategy-v1",
        "version": VERSION,
        "scope": "real-release-rerun-strategy",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "reportPath": _public_path(report_path or DEFAULT_REPORT),
        "failureCount": len(failures),
        "failureGroups": failure_groups,
        "selectedGroups": selected_groups,
        "priorityCounts": priority_counts,
        "needsStatefulRerun": needs_stateful,
        "needsImageRouteRerun": needs_image_route,
        "needsSerialPressureRerun": needs_pressure,
        "mustRunFullGateBeforePromotion": True,
        "runFullGateImmediatelyAfterEachFix": False,
        "recommendedBatching": {
            "maxFocusedAttemptsBeforeInvestigation": 2,
            "batchFixesBeforeFinalGate": True,
            "finalGatePolicy": "Run full real release validation once after all P0/P1 focused reruns pass.",
        },
        "commands": commands,
        "rules": [
            "Do not rerun the full gate after every single fix.",
            "Freeze and keep the failed artifact; do not overwrite it until a rerun plan is generated.",
            "Run lightweight validation first after code changes.",
            "Run focused groups plus dependencies as proof-of-fix evidence.",
            "If the focused rerun fails twice, stop and debug evidence instead of retrying blindly.",
            "Run the full real release gate once at the end, before promotion.",
        ],
        "failurePreview": failures[:20],
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--groups", default="", help="Comma-separated groups to plan manually when no report exists.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--print", action="store_true", help="Also print the full rerun strategy JSON.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    strategy = build_strategy(report_path=args.report, manual_groups=_split_groups(args.groups))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": strategy.get("status"),
        "action": strategy.get("action"),
        "artifact": str(args.output),
        "failureCount": strategy.get("failureCount"),
        "failureGroups": strategy.get("failureGroups"),
        "selectedGroups": strategy.get("selectedGroups"),
        "mustRunFullGateBeforePromotion": strategy.get("mustRunFullGateBeforePromotion"),
    }
    print(json.dumps(strategy if args.print else summary, ensure_ascii=False, indent=2))
    return 0 if strategy.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
