import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v022_harness_matrix_checker_reports_required_surface_coverage():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check-v022-harness-matrix.py"),
            "--json",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=True,
    )

    summary = json.loads(result.stdout)
    assert summary["status"] in {"REVIEWED-PASS", "LOCAL-PASS-REVIEW-PENDING"}
    assert summary["command_shell"] == "PowerShell"
    assert summary["rows"] >= 12

    required = {
        "replay",
        "refresh",
        "disconnect",
        "restart",
        "permissions",
        "artifacts",
        "channels",
        "feishu",
        "image-jobs",
        "image-fallback",
        "scheduler",
        "project-sessions",
        "markdown",
        "ui-polish",
        "status-motion",
        "run-center",
    }
    assert set(summary["required_surfaces"]) == required
    for surface in required:
        assert summary["coverage"][surface], f"{surface} has no pass/contract coverage"


def test_v022_harness_matrix_keeps_real_feishu_smoke_as_explicit_blocker():
    matrix = json.loads((ROOT / "docs" / "v0.2.2" / "harness-matrix.json").read_text(encoding="utf-8"))

    blockers = matrix.get("externalBlockers") or []
    assert any(
        blocker.get("surface") == "feishu"
        and blocker.get("status") == "BLOCKER-PENDING-CREDENTIALS"
        for blocker in blockers
    )
    assert any(
        "feishu" in row.get("surfaces", [])
        and row.get("status") == "contract-pass"
        for row in matrix.get("rows") or []
    )
    for row in matrix.get("rows") or []:
        for command in row.get("commands") or []:
            assert not command.lstrip().startswith("PYTEST_DISABLE_PLUGIN_AUTOLOAD=")
