#!/usr/bin/env python3
"""Generate the multi-agent split strategy for EcoreX real release validation.

This script is local-only.  It does not SSH to production and does not run
model, image, or pressure tests.  Its job is to produce an auditable work
allocation so multiple developer agents can collect evidence in parallel while
the final full real-release gate remains the source of truth.
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
DEFAULT_OUTPUT = ROOT / "docs" / f"v{VERSION}" / "artifacts" / "real-release-multi-agent-strategy.json"
HEAVY_SCRIPT = ROOT / "scripts" / "smoke-v026-production-agent-product-acceptance.py"

LANE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "id": "coordinator-light-preflight",
        "wave": 0,
        "role": "Coordinator Agent",
        "focus": "local release contract, matrix, route, documentation, wrapper commands",
        "groups": [],
        "legacyScripts": [],
        "command": "python scripts/真实发布轻量校验.py",
        "artifact": "docs/v0.2.8/artifacts/real-release-light-validation.json",
        "exclusiveLocks": [],
        "dependsOn": [],
        "estimatedCost": "local-low",
        "timeoutMinutes": 5,
        "parallelSafe": True,
        "evidenceRequired": [
            "light validation status PASS",
            "matrix count 345/577/400",
            "Chinese wrapper commands work",
        ],
    },
    {
        "id": "agent-a-fresh-runtime-auth",
        "wave": 1,
        "role": "Fresh Runtime Agent",
        "focus": "fresh user, auth, first-use runtime APIs, terminal state contracts",
        "groups": ["fresh-env", "auth-first-use", "runtime-api"],
        "legacyScripts": ["smoke-v026-production-200-user-behavior.py"],
        "command": "focus-run: fresh-env auth-first-use runtime-api, then attach redacted evidence",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-a-fresh-runtime-auth.json",
        "exclusiveLocks": [],
        "dependsOn": ["coordinator-light-preflight"],
        "estimatedCost": "online-low",
        "timeoutMinutes": 25,
        "parallelSafe": True,
        "evidenceRequired": [
            "isolated new-user run id",
            "auth/check/message/active-requests evidence",
            "no request left permanently running",
        ],
    },
    {
        "id": "agent-b-ui-context-session",
        "wave": 1,
        "role": "UX Session Agent",
        "focus": "desktop/mobile UI, session isolation, project context, compression behavior",
        "groups": ["ui-ux", "context-session"],
        "legacyScripts": [],
        "command": "focus-run: ui-ux context-session with browser screenshots and ledger evidence",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-b-ui-context-session.json",
        "exclusiveLocks": [],
        "dependsOn": ["coordinator-light-preflight"],
        "estimatedCost": "online-low",
        "timeoutMinutes": 30,
        "parallelSafe": True,
        "evidenceRequired": [
            "no blank buttons or blocking white screens",
            "session/project context isolation proof",
            "manual and automatic compression behavior proof",
        ],
    },
    {
        "id": "agent-c-security-observability",
        "wave": 1,
        "role": "Security Observability Agent",
        "focus": "redaction, diagnostics, logs, release artifact integrity, admin gates",
        "groups": ["security-observability"],
        "legacyScripts": ["smoke-v026-production-200-user-behavior.py"],
        "command": "focus-run: security-observability plus redacted diagnostics bundle review",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-c-security-observability.json",
        "exclusiveLocks": [],
        "dependsOn": ["coordinator-light-preflight"],
        "estimatedCost": "online-low",
        "timeoutMinutes": 20,
        "parallelSafe": True,
        "evidenceRequired": [
            "no sensitive-value/raw URL/raw path leakage",
            "request/session ids are hashable and actionable",
            "admin auth gate and artifact manifest evidence",
        ],
    },
    {
        "id": "agent-d-stream-state-machine",
        "wave": 2,
        "role": "State Machine Agent",
        "focus": "SSE streaming, replay, cancellation, refresh recovery, run ledger consistency",
        "groups": ["stream-state-machine"],
        "legacyScripts": [],
        "command": "focus-run: stream-state-machine with unique run id and replay probes",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-d-stream-state-machine.json",
        "exclusiveLocks": ["stream-ledger-heavy"],
        "dependsOn": ["agent-a-fresh-runtime-auth", "agent-b-ui-context-session"],
        "estimatedCost": "model-medium",
        "timeoutMinutes": 35,
        "parallelSafe": True,
        "evidenceRequired": [
            "SSE first content chunk/final done timing",
            "cancelled/failed/completed terminal states",
            "UI pending state matches backend active requests",
        ],
    },
    {
        "id": "agent-e-tool-skill-mcp-cli",
        "wave": 2,
        "role": "Toolchain Agent",
        "focus": "Skill discovery, MCP status, CLI, OCR, Vision, browser and file tools",
        "groups": ["tool-skill"],
        "legacyScripts": ["smoke-v026-production-30-image-ocr-vision-toolchain.py"],
        "command": "focus-run: tool-skill plus OCR/Vision/toolchain direct probes",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-e-tool-skill-mcp-cli.json",
        "exclusiveLocks": ["tool-permission-audit"],
        "dependsOn": ["agent-a-fresh-runtime-auth"],
        "estimatedCost": "model-medium",
        "timeoutMinutes": 40,
        "parallelSafe": True,
        "evidenceRequired": [
            "SkillService direct discovery",
            "MCP status/list/call evidence",
            "EcoreX CLI safe read-only evidence",
            "OCR/Vision direct-probe evidence not memory-derived",
        ],
    },
    {
        "id": "agent-f-multi-model-image-route",
        "wave": 2,
        "role": "Model Route Agent",
        "focus": "model switching, image generation/edit routing, no fallback drift",
        "groups": ["multi-model-image-route"],
        "legacyScripts": ["smoke-v026-production-30-image-ocr-vision-toolchain.py"],
        "command": "focus-run: multi-model-image-route with provider switch matrix",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-f-multi-model-image-route.json",
        "exclusiveLocks": ["image-route", "provider-switch"],
        "dependsOn": ["agent-a-fresh-runtime-auth"],
        "estimatedCost": "image-high",
        "timeoutMinutes": 50,
        "parallelSafe": True,
        "evidenceRequired": [
            "OpenAI/DeepSeek/Gemini/Doubao switch evidence",
            "generate route uses gpt-image-2-pro native route",
            "edit route uses gpt-image-2-pro native route",
            "no shell/Python/PIL/SVG/canvas fallback evidence",
        ],
    },
    {
        "id": "agent-g-concurrency-pressure",
        "wave": 3,
        "role": "Pressure Agent",
        "focus": "20 virtual users, 60 requests, active-request drain, resource recovery",
        "groups": ["concurrency-pressure"],
        "legacyScripts": [],
        "command": "python scripts/真实发布校验.py --skip-legacy --pressure-users 20 --pressure-turns 3 --output docs/v0.2.8/artifacts/real-release-pressure-focus.json",
        "artifact": "docs/v0.2.8/artifacts/real-release-pressure-focus.json",
        "exclusiveLocks": ["production-pressure", "active-requests-drain"],
        "dependsOn": [
            "agent-d-stream-state-machine",
            "agent-e-tool-skill-mcp-cli",
            "agent-f-multi-model-image-route",
        ],
        "estimatedCost": "stress-high",
        "timeoutMinutes": 90,
        "parallelSafe": False,
        "evidenceRequired": [
            "active requests return to zero",
            "no 5xx storm",
            "CPU/memory peak and recovery values",
            "no stuck request after pressure",
        ],
    },
    {
        "id": "agent-h-v027-integrated-capabilities",
        "wave": 3,
        "role": "v0.2.7 Integration Agent",
        "focus": "custom Gemini routing and generation, context continuity, divider UI, CDP/OCR open-box, Tongxin MPI, imagegen incremental delivery, mac/runtime parity markers, online update success",
        "groups": ["v027-integrated-capabilities"],
        "legacyScripts": [],
        "command": "focus-run: v027-integrated-capabilities with custom Gemini switch, MPI strict sample, and redacted toolchain evidence",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-h-v027-integrated-capabilities.json",
        "exclusiveLocks": ["provider-switch", "tongxin-mpi", "imagegen-route"],
        "dependsOn": [
            "agent-b-ui-context-session",
            "agent-e-tool-skill-mcp-cli",
            "agent-f-multi-model-image-route",
        ],
        "estimatedCost": "model-high",
        "timeoutMinutes": 60,
        "parallelSafe": False,
        "evidenceRequired": [
            "custom Gemini provider=custom and modelAliasFamily=gemini",
            "custom Gemini actual stream returns content and avoids empty fallback apology",
            "contextContinuity keeps AgentBridge and artifact history refs",
            "model-switch-divider is contextExcluded, non-sticky, and flows as a normal message row",
            "CDP/OCR/Vision defaults are discoverable",
            "Tongxin MPI sample uses MPI fact source and data-volume project/account source",
            "imagegen multi-image route emits each ready image and avoids shell fallback",
            "public manifest/admin-gated online update chain succeeds for Win/Mac WebUI artifacts",
        ],
    },
    {
        "id": "agent-i-v028-runtime-observability-queue",
        "wave": 3,
        "role": "v0.2.8 Runtime Observability Agent",
        "focus": "Codex-style same-session queue, durable queued payload/claim lease, task_observations projection, image-job intervention controls, and Run Center observation surface",
        "groups": ["v028-runtime-observability-queue"],
        "legacyScripts": [],
        "command": "focus-run: v028-runtime-observability-queue with synthetic task observation and cancellable queued-message probe",
        "artifact": "docs/v0.2.8/artifacts/multi-agent-agent-i-v028-runtime-observability-queue.json",
        "exclusiveLocks": ["session-queue", "runtime-observation-ledger"],
        "dependsOn": [
            "agent-a-fresh-runtime-auth",
            "agent-d-stream-state-machine",
        ],
        "estimatedCost": "online-low",
        "timeoutMinutes": 25,
        "parallelSafe": False,
        "evidenceRequired": [
            "TaskObserver and task_observations projection import from production runtime",
            "queued message admission returns same_session.policy=queue without cancelling the active run",
            "queued request payload can be cancelled and drains from active requests",
            "image-job continue/extend/background intervention controls are packaged",
            "Run Center static assets expose compact observation state",
        ],
    },
    {
        "id": "coordinator-final-real-release-gate",
        "wave": 4,
        "role": "Release Coordinator",
        "focus": "single authoritative full real release validation after all split evidence",
        "groups": [
            "fresh-env",
            "auth-first-use",
            "runtime-api",
            "ui-ux",
            "stream-state-machine",
            "context-session",
            "tool-skill",
            "multi-model-image-route",
            "concurrency-pressure",
            "v027-integrated-capabilities",
            "v028-runtime-observability-queue",
            "security-observability",
        ],
        "legacyScripts": [
            "smoke-v026-production-200-user-behavior.py",
            "smoke-v026-production-30-image-ocr-vision-toolchain.py",
        ],
        "command": "python scripts/真实发布校验.py",
        "artifact": "docs/v0.2.8/artifacts/production-agent-product-acceptance.json",
        "exclusiveLocks": ["final-release-gate", "production-pressure", "image-route"],
        "dependsOn": [
            "agent-a-fresh-runtime-auth",
            "agent-b-ui-context-session",
            "agent-c-security-observability",
            "agent-d-stream-state-machine",
            "agent-e-tool-skill-mcp-cli",
            "agent-f-multi-model-image-route",
            "agent-g-concurrency-pressure",
            "agent-h-v027-integrated-capabilities",
            "agent-i-v028-runtime-observability-queue",
        ],
        "estimatedCost": "full-high",
        "timeoutMinutes": 180,
        "parallelSafe": False,
        "evidenceRequired": [
            "P0/P1 100 percent pass",
            "enabled checks >= 400",
            "hardGateFailures empty",
            "redaction violations empty",
        ],
    },
]


def _load_heavy_module():
    spec = importlib.util.spec_from_file_location("smoke_v026_agent_product_acceptance", HEAVY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {HEAVY_SCRIPT}")
    spec.loader.exec_module(module)
    return module


def _case_counts(registry: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    counts: Dict[str, Dict[str, int]] = {}
    for case in registry:
        group = str(case.get("group") or "unknown")
        row = counts.setdefault(group, {"total": 0, "p0": 0, "p1": 0, "p2": 0, "hardGate": 0})
        row["total"] += 1
        priority = str(case.get("priority") or "").lower()
        if priority in ("p0", "p1", "p2"):
            row[priority] += 1
        if case.get("hardGate"):
            row["hardGate"] += 1
    return counts


def _lane_with_counts(lane: Dict[str, Any], counts: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    groups = list(lane.get("groups") or [])
    total = sum(counts.get(group, {}).get("total", 0) for group in groups)
    hard = sum(counts.get(group, {}).get("hardGate", 0) for group in groups)
    p2 = sum(counts.get(group, {}).get("p2", 0) for group in groups)
    return {
        **lane,
        "caseCount": total,
        "hardGateCount": hard,
        "removableCaseCount": p2,
    }


def _wave_summary(lanes: List[Dict[str, Any]], max_parallel_agents: int) -> List[Dict[str, Any]]:
    waves: List[Dict[str, Any]] = []
    for wave_id in sorted({int(lane["wave"]) for lane in lanes}):
        wave_lanes = [lane for lane in lanes if int(lane["wave"]) == wave_id]
        lock_counts: Dict[str, int] = {}
        for lane in wave_lanes:
            for lock in lane.get("exclusiveLocks") or []:
                lock_counts[lock] = lock_counts.get(lock, 0) + 1
        lock_conflicts = sorted(lock for lock, count in lock_counts.items() if count > 1)
        waves.append(
            {
                "wave": wave_id,
                "laneIds": [lane["id"] for lane in wave_lanes],
                "recommendedParallelism": min(max_parallel_agents, len([lane for lane in wave_lanes if lane.get("parallelSafe")]), len(wave_lanes)),
                "hasSerialLane": any(not lane.get("parallelSafe") for lane in wave_lanes),
                "exclusiveLockConflicts": lock_conflicts,
            }
        )
    return waves


def _validate_strategy(lanes: List[Dict[str, Any]], all_groups: Set[str], waves: List[Dict[str, Any]]) -> Dict[str, Any]:
    non_final_lanes = [lane for lane in lanes if lane["id"] != "coordinator-final-real-release-gate"]
    covered_by_split = {group for lane in non_final_lanes for group in lane.get("groups", [])}
    covered_by_final = {
        group
        for lane in lanes
        if lane["id"] == "coordinator-final-real-release-gate"
        for group in lane.get("groups", [])
    }
    final_lane = next((lane for lane in lanes if lane["id"] == "coordinator-final-real-release-gate"), None)
    pressure_lane = next((lane for lane in lanes if lane["id"] == "agent-g-concurrency-pressure"), None)
    image_lane = next((lane for lane in lanes if lane["id"] == "agent-f-multi-model-image-route"), None)
    failures = []
    if all_groups - covered_by_split:
        failures.append({"rule": "splitCoverage", "missingGroups": sorted(all_groups - covered_by_split)})
    if all_groups - covered_by_final:
        failures.append({"rule": "finalCoverage", "missingGroups": sorted(all_groups - covered_by_final)})
    if not final_lane or final_lane.get("command") != "python scripts/真实发布校验.py":
        failures.append({"rule": "finalGateCommand", "message": "Final lane must run the full real release gate."})
    if pressure_lane and image_lane and int(pressure_lane["wave"]) <= int(image_lane["wave"]):
        failures.append({"rule": "pressureAfterModelRoute", "message": "Pressure should run after model/image route evidence."})
    for wave in waves:
        if wave.get("exclusiveLockConflicts"):
            failures.append({"rule": "exclusiveLocks", "wave": wave["wave"], "locks": wave["exclusiveLockConflicts"]})
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "coveredGroupsBySplit": sorted(covered_by_split),
        "coveredGroupsByFinalGate": sorted(covered_by_final),
    }


def build_strategy(*, max_parallel_agents: int = 4) -> Dict[str, Any]:
    heavy = _load_heavy_module()
    registry = list(heavy.DECLARED_CASE_REGISTRY)
    counts = _case_counts(registry)
    all_groups = {str(row[0]) for row in heavy.NEW_CASE_GROUPS}
    lanes = [_lane_with_counts(lane, counts) for lane in LANE_DEFINITIONS]
    waves = _wave_summary(lanes, max(1, int(max_parallel_agents)))
    validation = _validate_strategy(lanes, all_groups, waves)
    split_case_count = sum(lane["caseCount"] for lane in lanes if lane["id"] != "coordinator-final-real-release-gate")
    final_new_case_count = next((lane["caseCount"] for lane in lanes if lane["id"] == "coordinator-final-real-release-gate"), 0)
    status = "PASS" if validation["status"] == "PASS" else "FAIL"
    return {
        "status": status,
        "schemaVersion": "real-release-multi-agent-strategy-v1",
        "version": VERSION,
        "scope": "real-release-multi-agent-strategy",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "feasibility": "FEASIBLE_WITH_FINAL_SERIAL_GATE" if status == "PASS" else "NEEDS_FIX",
        "maxParallelAgents": max(1, int(max_parallel_agents)),
        "recommendedParallelAgents": min(max(1, int(max_parallel_agents)), 4),
        "splitEvidenceCaseCount": split_case_count,
        "finalGateCaseCount": heavy.TARGET_TOTAL_CHECKS,
        "finalGateNewCaseCount": final_new_case_count,
        "targetTotalChecks": heavy.TARGET_TOTAL_CHECKS,
        "minimumEnabledChecks": heavy.MIN_ENABLED_CHECKS,
        "rules": [
            "Do not run multiple full real release gates against production at the same time.",
            "Every lane must use a unique run id and isolated session/project/user markers.",
            "Disable memory-derived proof: direct APIs, SkillService, MCP status, CLI, SSE ledger, and route evidence are required.",
            "Image generation and image editing evidence must prove gpt-image-2-pro native routing after every chat provider switch.",
            "Pressure testing is serial and must run after model/tool/stream evidence lanes.",
            "The final full real release gate is the only release-blocking source of truth.",
        ],
        "waves": waves,
        "lanes": lanes,
        "mergeContract": {
            "requiredPerLaneFields": [
                "laneId",
                "agentId",
                "runId",
                "status",
                "startedAt",
                "completedAt",
                "artifact",
                "redaction",
                "checks",
                "failurePreview",
            ],
            "releaseDecision": "Only docs/v0.2.8/artifacts/production-agent-product-acceptance.json can mark the release accepted.",
            "failFastRules": [
                "Any P0 lane failure blocks the final gate until fixed.",
                "Any redaction violation blocks artifact sharing.",
                "Any active request left running after pressure requires cleanup and rerun.",
            ],
        },
        "validation": validation,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-parallel-agents", type=int, default=4)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--print", action="store_true", help="Also print the full strategy JSON.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    strategy = build_strategy(max_parallel_agents=max(1, int(args.max_parallel_agents)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": strategy["status"],
        "feasibility": strategy["feasibility"],
        "artifact": str(args.output),
        "laneCount": len(strategy["lanes"]),
        "waveCount": len(strategy["waves"]),
        "recommendedParallelAgents": strategy["recommendedParallelAgents"],
        "finalGateCaseCount": strategy["finalGateCaseCount"],
    }
    print(json.dumps(strategy if args.print else summary, ensure_ascii=False, indent=2))
    return 0 if strategy["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
