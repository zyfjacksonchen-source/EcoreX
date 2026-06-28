#!/usr/bin/env python3
"""Build an evidence-based completion audit for the v0.2.2 long goal.

The audit is deliberately non-promoting: it summarizes existing authoritative
evidence from the acceptance checklist, harness matrix, and release gate. It
does not clear release blockers or replace the target deploy/rollback smoke.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "v0.2.2"
DEFAULT_ARTIFACT = DOCS / "artifacts" / "goal-completion-audit.json"
ACCEPTANCE_PATH = DOCS / "acceptance-checklist.md"

INCOMPLETE_STATUS_MARKERS = (
    "PENDING",
    "NEEDS",
    "LOCAL-PASS",
    "PARTIAL",
    "BLOCKED",
    "TODO",
)
SAFE_BLOCKER_REASONS = {
    "target-environment-deploy-rollback-not-exercised": (
        "No target-environment deploy/rollback smoke has been exercised for the promoted Web release."
    ),
}
ALLOWED_RELEASE_BLOCKER_IDS = {
    "target-environment-deploy-rollback-not-exercised",
    "public-manifest-not-promoted",
    "release-defaults-not-promoted",
    "public-release-package-not-built",
    "deploy-rollback-smoke-missing",
    "feishu-im-real-credential-smoke",
    "final-release-review-not-pass",
}
ALLOWED_RELEASE_BLOCKER_SURFACES = {"release"}

REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "backend-runtime-source-of-truth",
        "title": "Backend runtime events and projections are canonical",
        "acceptanceIds": ["R22-01", "R22-02", "R22-03", "R22-06", "R22-11"],
        "matrixSurfaces": ["replay", "permissions"],
    },
    {
        "id": "frontend-projection-recovery",
        "title": "Frontend consumes projections for refresh, reconnect, and recovery",
        "acceptanceIds": ["R22-04", "R22-05", "R22-07"],
        "matrixSurfaces": ["replay", "refresh", "disconnect", "restart"],
    },
    {
        "id": "feishu-im-auth-observability",
        "title": "Feishu/IM transport, auth, and callable state are separated",
        "acceptanceIds": ["R22-08", "R22-09"],
        "matrixSurfaces": ["channels", "feishu"],
        "releaseChecks": ["feishu-im-real-credential-smoke-valid"],
    },
    {
        "id": "admin-audit-capability-policy",
        "title": "Admin audit and capability policy are runtime-evidence based",
        "acceptanceIds": ["R22-11"],
        "matrixSurfaces": ["permissions"],
    },
    {
        "id": "image-generation-runtime-jobs",
        "title": "Image generation uses backend ImageJobService, events, and projections",
        "acceptanceIds": ["R22-13", "R22-14"],
        "matrixSurfaces": ["image-jobs", "image-fallback", "artifacts"],
    },
    {
        "id": "scheduler-projection-management",
        "title": "Scheduler state is visible and managed through backend projection",
        "acceptanceIds": ["R22-15"],
        "matrixSurfaces": ["scheduler"],
    },
    {
        "id": "web-ux-session-markdown-status-runcenter",
        "title": "Web project sessions, Markdown, status motion, and Run Center hiding pass",
        "acceptanceIds": ["R22-10", "R22-16", "R22-17", "R22-18", "R22-19"],
        "matrixSurfaces": ["project-sessions", "markdown", "status-motion", "run-center", "ui-polish"],
    },
    {
        "id": "release-target-deploy-rollback",
        "title": "Release evidence, deploy, rollback, and final release gate are complete",
        "acceptanceIds": ["R22-12"],
        "matrixSurfaces": [],
        "releaseRequired": True,
    },
]


class CompletionAuditError(RuntimeError):
    """Raised when the audit sources are malformed."""


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CompletionAuditError(f"cannot load module: {_display_path(path)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def _safe_identifier(value: Any, *, fallback: str, allowed: set[str] | None = None) -> str:
    text = str(value or "").strip()
    if allowed is not None and text in allowed:
        return text
    if not text:
        return fallback
    return f"{fallback}-{_hash_text(text)[:16]}"


def _safe_error_summary(value: Any) -> dict[str, str]:
    text = str(value or "")
    return {
        "errorType": "release-gate-error",
        "errorHash": _hash_text(text)[:16],
    }


def _safe_release_blocker(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    blocker_id = _safe_identifier(raw.get("id"), fallback="release-blocker", allowed=ALLOWED_RELEASE_BLOCKER_IDS)
    surface = _safe_identifier(raw.get("surface"), fallback="release", allowed=ALLOWED_RELEASE_BLOCKER_SURFACES)
    expected_reason = SAFE_BLOCKER_REASONS.get(blocker_id)
    observed_reason = str(raw.get("reason") or "")
    if expected_reason and observed_reason == expected_reason:
        reason = expected_reason
    else:
        reason = "redacted-release-blocker-reason"
    result = {"id": blocker_id, "surface": surface, "reason": reason}
    if observed_reason and reason.startswith("redacted"):
        result["reasonHash"] = _hash_text(observed_reason)[:16]
    return result


def _parse_acceptance_table(text: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("| R22-"):
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        if len(cells) < 5:
            continue
        item_id, area, acceptance, status, evidence = cells[:5]
        rows[item_id] = {
            "area": area,
            "acceptance": acceptance,
            "status": status,
            "evidence": evidence,
        }
    return rows


def _load_acceptance_rows(path: Path = ACCEPTANCE_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise CompletionAuditError(f"acceptance checklist missing: {_display_path(path)}")
    return _parse_acceptance_table(path.read_text(encoding="utf-8"))


def _load_release_gate_result() -> dict[str, Any]:
    gate = _load_module(ROOT / "scripts" / "check-v022-release-gate.py", "check_v022_release_gate_for_completion_audit")
    result = gate.evaluate_release_gate()
    if not isinstance(result, dict):
        raise CompletionAuditError("release gate did not return an object")
    return result


def _load_matrix_summary() -> dict[str, Any]:
    checker = _load_module(ROOT / "scripts" / "check-v022-harness-matrix.py", "check_v022_harness_matrix_for_completion_audit")
    summary = checker.validate_matrix()
    if not isinstance(summary, dict):
        raise CompletionAuditError("harness matrix checker did not return an object")
    return summary


def _acceptance_status_proven(status: str) -> bool:
    value = str(status or "").upper()
    if any(marker in value for marker in INCOMPLETE_STATUS_MARKERS):
        return False
    return "PASS" in value


def _checks_by_id(release_gate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in release_gate.get("checks") or []:
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    return result


def _surface_coverage(matrix_summary: dict[str, Any], surface: str) -> list[str]:
    coverage = matrix_summary.get("coverage")
    if not isinstance(coverage, dict):
        return []
    values = coverage.get(surface)
    return [str(value) for value in values or [] if value]


def _evaluate_requirement(
    spec: dict[str, Any],
    *,
    acceptance_rows: dict[str, dict[str, str]],
    matrix_summary: dict[str, Any],
    release_gate: dict[str, Any],
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    gaps: list[str] = []

    for item_id in spec.get("acceptanceIds") or []:
        row = acceptance_rows.get(item_id)
        if not row:
            gaps.append(f"acceptance {item_id} missing")
            continue
        status = row.get("status", "")
        evidence.append({"type": "acceptance", "id": item_id, "status": status})
        if spec.get("releaseRequired") and item_id == "R22-12":
            if release_gate.get("releasable") is not True:
                gaps.append("release gate is not releasable")
            continue
        if not _acceptance_status_proven(status):
            gaps.append(f"acceptance {item_id} status is {status}")

    matrix_surfaces = spec.get("matrixSurfaces") or []
    if matrix_surfaces and matrix_summary.get("status") != "REVIEWED-PASS":
        gaps.append(f"harness matrix status is {matrix_summary.get('status')}")

    for surface in matrix_surfaces:
        rows = _surface_coverage(matrix_summary, surface)
        evidence.append({"type": "matrixSurface", "surface": surface, "rows": rows})
        if not rows:
            gaps.append(f"matrix surface {surface} has no coverage rows")

    checks = _checks_by_id(release_gate)
    for check_id in spec.get("releaseChecks") or []:
        check = checks.get(check_id)
        if not check:
            gaps.append(f"release check {check_id} missing")
            continue
        evidence.append({"type": "releaseCheck", "id": check_id, "status": check.get("status")})
        if check.get("status") != "pass":
            gaps.append(f"release check {check_id} status is {check.get('status')}")

    blockers = release_gate.get("blockers") if isinstance(release_gate.get("blockers"), list) else []
    if spec.get("releaseRequired") and blockers:
        for blocker in blockers:
            evidence.append({"type": "releaseBlocker", **_safe_release_blocker(blocker)})

    status = "PROVEN" if not gaps else "BLOCKED" if spec.get("releaseRequired") else "INCOMPLETE"
    return {
        "id": spec["id"],
        "title": spec["title"],
        "status": status,
        "gaps": gaps,
        "evidence": evidence,
    }


def build_completion_audit(
    *,
    acceptance_rows: dict[str, dict[str, str]] | None = None,
    matrix_summary: dict[str, Any] | None = None,
    release_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    acceptance_rows = acceptance_rows if acceptance_rows is not None else _load_acceptance_rows()
    matrix_summary = matrix_summary if matrix_summary is not None else _load_matrix_summary()
    release_gate = release_gate if release_gate is not None else _load_release_gate_result()

    requirements = [
        _evaluate_requirement(
            spec,
            acceptance_rows=acceptance_rows,
            matrix_summary=matrix_summary,
            release_gate=release_gate,
        )
        for spec in REQUIREMENTS
    ]
    release_blockers = [_safe_release_blocker(item) for item in release_gate.get("blockers") or [] if item]
    errors = [_safe_error_summary(item) for item in release_gate.get("errors") or [] if item]
    incomplete_requirements = [
        {"id": item["id"], "status": item["status"], "gaps": item["gaps"]}
        for item in requirements
        if item["status"] != "PROVEN"
    ]
    complete = not errors and not release_blockers and not incomplete_requirements
    status = "PASS" if complete else "ERROR" if errors else "BLOCKED"
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "complete": complete,
        "objective": "EcoreX v0.2.2 backend-led, observable, replayable, auditable Web runtime release goal",
        "authoritativeSources": {
            "acceptanceChecklist": _display_path(ACCEPTANCE_PATH),
            "harnessMatrix": _display_path(ROOT / "docs" / "v0.2.2" / "harness-matrix.json"),
            "releaseGate": "scripts/check-v022-release-gate.py",
        },
        "releaseGate": {
            "status": release_gate.get("status"),
            "releasable": release_gate.get("releasable"),
            "errors": errors,
            "blockers": release_blockers,
        },
        "matrix": {
            "status": matrix_summary.get("status"),
            "rows": matrix_summary.get("rows"),
            "commands": matrix_summary.get("commands"),
            "externalBlockers": matrix_summary.get("external_blockers"),
        },
        "requirements": requirements,
        "incompleteRequirements": incomplete_requirements,
        "completionBlockers": release_blockers,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full audit JSON.")
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT.relative_to(ROOT)), help="Path to write the audit JSON.")
    parser.add_argument("--require-complete", action="store_true", help="Return non-zero unless every requirement is proven.")
    args = parser.parse_args(argv)

    try:
        audit = build_completion_audit()
    except CompletionAuditError as exc:
        print(f"v0.2.2 goal completion audit failed: {exc}")
        return 2

    artifact = Path(args.artifact)
    if not artifact.is_absolute():
        artifact = ROOT / artifact
    if args.artifact:
        _write_json(artifact, audit)

    if args.json:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"v0.2.2 goal completion audit {audit['status']}: "
            f"{len(audit['incompleteRequirements'])} incomplete requirements, "
            f"{len(audit['completionBlockers'])} completion blockers"
        )

    if audit["status"] == "ERROR":
        return 2
    if args.require_complete and not audit["complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
