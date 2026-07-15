from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_report_module():
    script = ROOT / "scripts" / "generate-v025-release-gate-report.py"
    spec = importlib.util.spec_from_file_location("generate_v025_release_gate_report", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_v025_release_gate_report_local_pass_production_pending():
    module = _load_report_module()

    report = module.build_report(refresh=False)
    assert report["schemaVersion"] == "v0.2.5-release-gate-report-v1"
    assert report["localGatePassed"] is True
    assert report["status"] in {"pass", "local_pass_production_pending"}
    if report["productionServiceUserPending"]:
        assert report["releaseReady"] is False
        assert report["requiredFollowUps"]


def test_v025_release_gate_markdown_writer(tmp_path):
    module = _load_report_module()
    path = tmp_path / "report.md"
    report = {
        "status": "local_pass_production_pending",
        "releaseReady": False,
        "localGatePassed": True,
        "productionServiceUserPending": True,
        "artifacts": [{"name": "toolMatrix", "status": "pass"}],
        "refreshChecks": [{"name": "tool-matrix-smoke", "status": "pass"}],
        "requiredFollowUps": ["Run production probe."],
        "ledgerP2": ["Owner S6: item."],
    }

    module._write_markdown(report, path)
    text = path.read_text(encoding="utf-8")
    assert "Release Gate Report" in text
    assert "`local_pass_production_pending`" in text
    assert "Run production probe." in text


def test_v025_release_gate_requires_verified_production_row():
    module = _load_report_module()

    assert module._tool_matrix_pending({"environments": []}) is True
    assert module._tool_matrix_pending({
        "environments": [
            {"environment": "production-service-user", "status": "pass"},
        ],
    }) is True
    assert module._tool_matrix_pending({
        "environments": [
            {
                "environment": "production-service-user",
                "status": "pass",
                "productionVerification": {
                    "effectiveUserOk": True,
                    "pythonUnderInstallRoot": False,
                    "venvUnderInstallRoot": True,
                },
            },
        ],
    }) is True
    assert module._tool_matrix_pending({
        "environments": [
            {
                "environment": "production-service-user",
                "status": "pass",
                "productionVerification": {
                    "effectiveUserOk": True,
                    "pythonUnderInstallRoot": True,
                },
            },
        ],
    }) is False
