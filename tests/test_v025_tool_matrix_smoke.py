from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_module():
    script = ROOT / "scripts" / "run-v025-tool-matrix-smoke.py"
    spec = importlib.util.spec_from_file_location("run_v025_tool_matrix_smoke", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_v025_tool_matrix_worker_contract():
    module = _load_smoke_module()

    payload = module.run_worker("unit")
    assert payload["schemaVersion"] == "v0.2.5-tool-matrix-smoke-v1"
    assert payload["status"] == "pass"
    ids = {item["id"] for item in payload["smokes"]}
    assert {
        "office-documents",
        "office-pdf",
        "office-presentations",
        "office-spreadsheets",
        "imagegen",
        "feishu-canary",
        "tongxin-canary",
        "browser-schema",
        "mcp-discovery",
    } <= ids
    assert all(item.get("redacted") is True for item in payload["smokes"])


def test_v025_tool_matrix_payload_summary_redacts_paths_and_secrets():
    module = _load_smoke_module()

    summary = module._payload_summary({
        "status": "success",
        "installRoot": "C:/Users/example/secret/path",
        "authState": "ready",
        "app_secret": "should-not-appear",
    })
    assert summary["status"] == "success"
    assert summary["authState"] == "ready"
    assert "installRoot" not in summary
    assert "app_secret" in summary["payloadKeys"]
    assert "should-not-appear" not in str(summary)


def test_v025_tool_matrix_production_identity_shape():
    module = _load_smoke_module()

    verification = module._production_identity_verification()
    assert verification["redacted"] is True
    assert "effectiveUserOk" in verification
    assert "pythonUnderInstallRoot" in verification
    assert "venvUnderInstallRoot" in verification
    assert verification["effectiveUser"] in {"ecorex", "not-ecorex"}
    assert verification["effectiveUserSource"] in {
        "posix-euid",
        "posix-euid-unavailable",
        "platform-user",
        "platform-user-unavailable",
    }
