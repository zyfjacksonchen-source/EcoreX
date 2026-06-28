#!/usr/bin/env python3
"""R23-16P aggregate performance evidence release gate.

The report is intentionally small: it records counts, enum statuses, relative
artifact names, and HMAC references. It never echoes matched text from scanned
artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "v0.2.3" / "performance-harness-matrix.json"
DEFAULT_OUTPUT = ROOT / "docs" / "v0.2.3" / "artifacts" / "perf-evidence-audit.json"
SCANNER_PATH = ROOT / "scripts" / "scan-session-artifacts-privacy.py"
SELF_SCENARIO_ID = "performance-evidence-audit"
REQUIRED_SCENARIO_IDS = {
    "long-session-projection",
    "frontend-render-state-isolation",
    "complex-task-soak",
    "refresh-replay",
    "browser-ocr",
    "image-artifact-ocr",
    "scheduler-subagent",
    SELF_SCENARIO_ID,
}


def _load_scanner():
    spec = importlib.util.spec_from_file_location("ecorex_artifact_privacy_scanner", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("scanner_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _salt(value: str = "") -> bytes:
    raw = value or os.environ.get("ECOREX_ARTIFACT_SCAN_HMAC_SALT") or secrets.token_hex(32)
    return str(raw).encode("utf-8", errors="replace")


def _hmac_ref(value: str, salt: bytes) -> str:
    digest = hmac.new(salt, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _scenario_artifacts(matrix: Dict[str, Any], salt: bytes) -> Tuple[List[Tuple[str, Path, Path]], List[Dict[str, Any]], List[Dict[str, Any]], int, int]:
    rows: List[Tuple[str, Path, Path]] = []
    config_issues: List[Dict[str, Any]] = []
    seen_ids = set()
    self_audit_scenario_count = 0
    scenarios = matrix.get("scenarios") or []
    for index, scenario in enumerate(scenarios):
        artifact = scenario.get("artifact")
        privacy_artifact = scenario.get("privacyArtifact")
        scenario_id = str(scenario.get("id") or "")
        if scenario_id:
            seen_ids.add(scenario_id)
        scenario_hash = _hmac_ref(scenario_id or f"index:{index}", salt)
        if not artifact:
            config_issues.append({"scenarioHash": scenario_hash, "issue": "missing_main_artifact_field"})
        if not privacy_artifact:
            config_issues.append({"scenarioHash": scenario_hash, "issue": "missing_scan_artifact_field"})
        if not artifact or not privacy_artifact:
            continue
        if scenario_id == SELF_SCENARIO_ID:
            self_audit_scenario_count += 1
            continue
        rows.append((scenario_id, ROOT / str(artifact), ROOT / str(privacy_artifact)))
    missing_required = [
        {"scenarioHash": _hmac_ref(scenario_id, salt)}
        for scenario_id in sorted(REQUIRED_SCENARIO_IDS.difference(seen_ids))
    ]
    return rows, config_issues, missing_required, len(scenarios), self_audit_scenario_count


def _scan_paths(paths: Iterable[Path], scanner: Any, salt: bytes) -> List[Dict[str, Any]]:
    buckets: List[Dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for pattern, count in sorted(scanner._scan_text(text).items()):
            buckets.append(
                {
                    "artifactHash": _hmac_ref(str(path.resolve()), salt),
                    "findingTypeHash": _hmac_ref(str(pattern), salt),
                    "count": int(count),
                }
            )
    return buckets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit R23-16P performance artifacts")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--salt", default="")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    salt = _salt(args.salt)
    scanner = _load_scanner()
    matrix = _load_json(args.matrix)
    rows, config_issues, missing_required, matrix_scenario_count, self_audit_scenario_count = _scenario_artifacts(matrix, salt)

    missing_main: List[Dict[str, str]] = []
    missing_scan: List[Dict[str, str]] = []
    scan_not_clean: List[Dict[str, Any]] = []
    scanned_paths: List[Path] = []

    for scenario_id, artifact, privacy_artifact in rows:
        if artifact.exists():
            scanned_paths.append(artifact)
        else:
            missing_main.append(
                {
                    "scenarioHash": _hmac_ref(scenario_id, salt),
                    "artifactHash": _hmac_ref(str(artifact.resolve()), salt),
                }
            )
        if privacy_artifact.exists():
            scanned_paths.append(privacy_artifact)
            try:
                payload = _load_json(privacy_artifact)
            except Exception:
                payload = {}
            if payload.get("status") != "success" or int(payload.get("findingCount") or 0) != 0:
                scan_not_clean.append(
                    {
                        "scenarioHash": _hmac_ref(scenario_id, salt),
                        "artifactHash": _hmac_ref(str(privacy_artifact.resolve()), salt),
                        "status": str(payload.get("status") or "invalid"),
                        "findingBucketCount": int(payload.get("findingCount") or 0),
                    }
                )
        else:
            missing_scan.append(
                {
                    "scenarioHash": _hmac_ref(scenario_id, salt),
                    "artifactHash": _hmac_ref(str(privacy_artifact.resolve()), salt),
                }
            )

    finding_buckets = _scan_paths(scanned_paths, scanner, salt)
    failed = bool(config_issues or missing_required or missing_main or missing_scan or scan_not_clean or finding_buckets)
    payload = {
        "version": "0.2.3",
        "slice": "R23-16P-09",
        "scenario": "performance-evidence-audit",
        "status": "fail" if failed else "pass",
        "redacted": True,
        "metrics": {
            "matrixScenarioCount": matrix_scenario_count,
            "scenarioPairCount": len(rows),
            "selfAuditScenarioCount": self_audit_scenario_count,
            "requiredScenarioMissingCount": len(missing_required),
            "matrixConfigIssueCount": len(config_issues),
            "mainArtifactCount": len([path for _, path, _ in rows if path.exists()]),
            "scanArtifactCount": len([path for _, _, path in rows if path.exists()]),
            "scannedArtifactCount": len(scanned_paths),
            "missingMainArtifactCount": len(missing_main),
            "missingScanArtifactCount": len(missing_scan),
            "scanNotCleanCount": len(scan_not_clean),
            "findingBucketCount": len(finding_buckets),
            "findingTotalCount": sum(int(item.get("count") or 0) for item in finding_buckets),
        },
        "matrixConfigIssues": config_issues,
        "requiredScenarioMissing": missing_required,
        "artifacts": [
            {
                "scenarioHash": _hmac_ref(scenario_id, salt),
                "mainHash": _hmac_ref(str(artifact.resolve()), salt),
                "scanHash": _hmac_ref(str(privacy_artifact.resolve()), salt),
            }
            for scenario_id, artifact, privacy_artifact in rows
        ],
        "missingMainArtifacts": missing_main,
        "missingScanArtifacts": missing_scan,
        "scanNotClean": scan_not_clean,
        "findingBuckets": finding_buckets,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
