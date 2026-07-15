#!/usr/bin/env python3
"""Generate the EcoreX v0.2.5 release gate report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACT_DIR = ROOT / "docs" / "v0.2.5" / "artifacts"
REPORT_JSON = ARTIFACT_DIR / "v0.2.5-release-gate-report.json"
REPORT_MD = ROOT / "docs" / "v0.2.5" / "v0.2.5-release-gate-report.md"


ARTIFACTS = {
    "baseline": ARTIFACT_DIR / "v0.2.5-runtime-baseline.json",
    "runtimeManifest": ARTIFACT_DIR / "v0.2.5-runtime-manifest-probe.json",
    "skillToolBinding": ARTIFACT_DIR / "v0.2.5-skill-tool-binding-probe.json",
    "toolMatrix": ARTIFACT_DIR / "v0.2.5-tool-matrix-smoke.json",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"status": "missing", "path": str(path.name), "redacted": True}
    except Exception as exc:
        return {"status": "error", "path": str(path.name), "errorType": exc.__class__.__name__, "redacted": True}


def _run_check(name: str, command: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    return {
        "name": name,
        "status": "pass" if completed.returncode == 0 else "fail",
        "exitCode": completed.returncode,
        "stdoutPresent": bool((completed.stdout or "").strip()),
        "stderrPresent": bool((completed.stderr or "").strip()),
        "durationMs": int((time.monotonic() - started) * 1000),
        "redacted": True,
    }


def refresh_artifacts() -> list[dict[str, Any]]:
    checks = [
        _run_check("runtime-baseline", [sys.executable, "scripts/check-v025-runtime-baseline.py"]),
        _run_check("skill-tool-bindings", [sys.executable, "scripts/check-v025-skill-tool-bindings.py"]),
        _run_check("tool-matrix-smoke", [sys.executable, "scripts/run-v025-tool-matrix-smoke.py"]),
    ]
    manifest = ARTIFACTS["runtimeManifest"]
    if manifest.exists():
        checks.append(_run_check(
            "runtime-manifest",
            [
                sys.executable,
                "scripts/check-v025-runtime-manifest.py",
                str(manifest),
                "--platform",
                "linux-service",
                "--version",
                "0.2.5",
                "--runtime-root",
                ".",
                "--package-root",
                ".",
            ],
        ))
    else:
        checks.append({"name": "runtime-manifest", "status": "missing", "redacted": True})
    return checks


def _artifact_status(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "present")
    if name == "baseline" and status == "present":
        status = "present"
    if name == "runtimeManifest" and status == "present":
        status = "present"
    return {
        "name": name,
        "status": status,
        "schemaVersion": payload.get("schemaVersion"),
        "redacted": True,
    }


def _production_service_ready(payload: Mapping[str, Any]) -> bool:
    for item in payload.get("environments") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("environment") != "production-service-user":
            continue
        if item.get("status") != "pass":
            return False
        verification = item.get("productionVerification")
        if not isinstance(verification, Mapping):
            return False
        return bool(
            verification.get("effectiveUserOk")
            and verification.get("pythonUnderInstallRoot")
        )
    return False


def _tool_matrix_pending(payload: Mapping[str, Any]) -> bool:
    return not _production_service_ready(payload)


def build_report(*, refresh: bool = False) -> dict[str, Any]:
    refresh_checks = refresh_artifacts() if refresh else []
    artifacts = {name: _read_json(path) for name, path in ARTIFACTS.items()}
    artifact_statuses = [_artifact_status(name, payload) for name, payload in artifacts.items()]
    local_failures = [
        item for item in artifact_statuses
        if item.get("status") in {"fail", "error", "missing"}
    ] + [
        item for item in refresh_checks
        if item.get("status") in {"fail", "error", "missing"}
    ]
    production_pending = _tool_matrix_pending(artifacts.get("toolMatrix", {}))
    overall = "fail" if local_failures else "local_pass_production_pending" if production_pending else "pass"
    return {
        "schemaVersion": "v0.2.5-release-gate-report-v1",
        "version": "0.2.5",
        "generatedAt": _now(),
        "status": overall,
        "releaseReady": overall == "pass",
        "localGatePassed": not local_failures,
        "productionServiceUserPending": production_pending,
        "artifacts": artifact_statuses,
        "refreshChecks": refresh_checks,
        "requiredFollowUps": [
            "Run scripts/run-v025-tool-matrix-smoke.py on the Web Linux service as the production ecorex user."
        ] if production_pending else [],
        "ledgerP2": [
            "S6: verify production service-user effective user and venv.",
            "S6: correlate Office/PDF partial smoke summaries with packaged manifest evidence.",
            "S6: decide ImageGen generated-output path retrieval/redaction contract.",
            "S6/test-hardening: add focused run_matrix variant tests.",
        ],
        "redacted": True,
    }


def _write_markdown(report: Mapping[str, Any], path: Path) -> None:
    lines = [
        "# EcoreX v0.2.5 Release Gate Report",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Release ready: `{str(report.get('releaseReady')).lower()}`",
        f"- Local gate passed: `{str(report.get('localGatePassed')).lower()}`",
        f"- Production service-user pending: `{str(report.get('productionServiceUserPending')).lower()}`",
        "",
        "## Artifacts",
        "",
    ]
    for item in report.get("artifacts") or []:
        lines.append(f"- `{item.get('name')}`: `{item.get('status')}`")
    if report.get("refreshChecks"):
        lines.extend(["", "## Refresh Checks", ""])
        for item in report.get("refreshChecks") or []:
            lines.append(f"- `{item.get('name')}`: `{item.get('status')}`")
    if report.get("requiredFollowUps"):
        lines.extend(["", "## Required Follow-Ups", ""])
        for item in report.get("requiredFollowUps") or []:
            lines.append(f"- {item}")
    if report.get("ledgerP2"):
        lines.extend(["", "## Ledger P2", ""])
        for item in report.get("ledgerP2") or []:
            lines.append(f"- {item}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Re-run local check scripts before building the report.")
    parser.add_argument("--strict-production", action="store_true", help="Return non-zero when production service-user probe is pending.")
    parser.add_argument("--json-output", default=str(REPORT_JSON), help="JSON report path.")
    parser.add_argument("--markdown-output", default=str(REPORT_MD), help="Markdown report path.")
    parser.add_argument("--json", action="store_true", help="Print the JSON report.")
    args = parser.parse_args()

    report = build_report(refresh=args.refresh)
    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_markdown(report, Path(args.markdown_output))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['status']}: v0.2.5 release gate report ({json_path})")
    if report["status"] == "fail":
        return 1
    if args.strict_production and report.get("productionServiceUserPending"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
