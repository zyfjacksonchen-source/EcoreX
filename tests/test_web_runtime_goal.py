import importlib.util
import json
import os
import pickle
import subprocess
import sys
import types
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


def _touch_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def _load_web_baseline_module():
    path = ROOT / "scripts" / "check-web-core-runtime-baseline.py"
    spec = importlib.util.spec_from_file_location("check_web_core_runtime_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_install_capability_module():
    path = ROOT / "scripts" / "install-capability.py"
    spec = importlib.util.spec_from_file_location("install_capability", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_web_release_gate_module():
    path = ROOT / "scripts" / "generate-web-runtime-release-gate.py"
    spec = importlib.util.spec_from_file_location("generate_web_runtime_release_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_web_state_event_smoke_module():
    path = ROOT / "scripts" / "smoke-web-state-event-consistency.py"
    spec = importlib.util.spec_from_file_location("smoke_web_state_event_consistency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_web_core_baseline_reports_core_missing_as_blocking(tmp_path):
    module = _load_web_baseline_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()

    report = module.capture_report(Namespace(
        runtime_root=runtime,
        state_root=state,
        include_system_path=False,
    ))

    assert report["schemaVersion"] == "web-core-runtime-baseline-v1"
    assert report["summary"]["releaseReady"] is False
    assert report["summary"]["blocking"] > 0
    assert report["summary"]["categoryCounts"]["coreRequired"] > 0
    assert report["runtimeRoot"] == "%RUNTIME_ROOT%"
    credential_rows = [row for row in report["dependencies"] if row["category"] == "credentialRequired"]
    assert credential_rows
    assert all(row["blocking"] is False for row in credential_rows)
    optional_rows = [row for row in report["dependencies"] if row["category"] == "optionalRepairable"]
    assert optional_rows
    assert all(row["blocking"] is False and row["repairAction"] for row in optional_rows)


def test_web_core_baseline_tool_entrypoints_are_core_required(tmp_path):
    module = _load_web_baseline_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    (runtime / "agent" / "tools" / "vision").mkdir(parents=True)
    (runtime / "agent" / "tools" / "vision" / "vision.py").write_text("# test\n", encoding="utf-8")
    state.mkdir()

    report = module.capture_report(Namespace(
        runtime_root=runtime,
        state_root=state,
        include_system_path=False,
    ))
    by_name = {row["name"]: row for row in report["dependencies"]}

    assert by_name["vision"]["status"] == "ready"
    assert by_name["vision"]["category"] == "coreRequired"
    assert by_name["vision"]["path"].startswith("%RUNTIME_ROOT%/")
    assert by_name["ocr"]["status"] == "missing_tool_entrypoint"
    assert by_name["ocr"]["blocking"] is True


def test_web_core_baseline_strict_rejects_system_path(capsys):
    module = _load_web_baseline_module()

    exit_code = module.main([
        "check-web-core-runtime-baseline.py",
        "--strict",
        "--include-system-path",
        "--no-write",
    ])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "strict" in captured.err
    assert "owned-runtime" in captured.err


def test_web_core_baseline_passes_with_owned_install_root_runtime(tmp_path):
    module = _load_web_baseline_module()
    install_root = tmp_path / "ecorex-web"
    runtime = install_root / "releases" / "rel-1" / "runtime"
    state = install_root / "state"
    site_packages = install_root / "venv" / "Lib" / "site-packages"
    posix_site_packages = install_root / "venv" / "lib" / f"python{os.sys.version_info.major}.{os.sys.version_info.minor}" / "site-packages"
    for root in (site_packages, posix_site_packages):
        for module_name in module.CORE_PYTHON_PACKAGES:
            package = root.joinpath(*module_name.split("."))
            (package / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").write_text("# test\n", encoding="utf-8")

    python_path = install_root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _touch_executable(python_path)
    for name in (["node.exe", "npm.cmd", "npx.cmd"] if os.name == "nt" else ["node", "npm", "npx"]):
        _touch_executable(install_root / "node" / "bin" / name)
    for name, path in module.TOOL_ENTRYPOINTS.items():
        (runtime / path).parent.mkdir(parents=True, exist_ok=True)
        (runtime / path).write_text(f"# {name}\n", encoding="utf-8")
    state.mkdir(parents=True)
    if os.name == "nt":
        _touch_executable(state / "playwright-browsers" / "chromium-1223" / "chrome-win" / "chrome.exe")
    else:
        _touch_executable(state / "playwright-browsers" / "chromium-1223" / "chrome-linux" / "chrome")

    output = tmp_path / "baseline.json"
    exit_code = module.main([
        "check-web-core-runtime-baseline.py",
        "--runtime-root",
        str(runtime),
        "--state-root",
        str(state),
        "--output",
        str(output),
        "--strict",
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["summary"]["releaseReady"] is True
    assert report["summary"]["blocking"] == 0
    assert report["systemPathIncluded"] is False


def test_web_core_baseline_accepts_bundled_runtime_playwright_chromium(tmp_path):
    module = _load_web_baseline_module()
    install_root = tmp_path / "ecorex-web"
    runtime = install_root / "releases" / "rel-1" / "runtime"
    state = install_root / "state"
    site_packages = install_root / "venv" / "Lib" / "site-packages"
    posix_site_packages = install_root / "venv" / "lib" / f"python{os.sys.version_info.major}.{os.sys.version_info.minor}" / "site-packages"
    for root in (site_packages, posix_site_packages):
        for module_name in module.CORE_PYTHON_PACKAGES:
            package = root.joinpath(*module_name.split("."))
            (package / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").write_text("# test\n", encoding="utf-8")

    python_path = install_root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _touch_executable(python_path)
    for name in (["node.exe", "npm.cmd", "npx.cmd"] if os.name == "nt" else ["node", "npm", "npx"]):
        _touch_executable(install_root / "node" / "bin" / name)
    for name, path in module.TOOL_ENTRYPOINTS.items():
        (runtime / path).parent.mkdir(parents=True, exist_ok=True)
        (runtime / path).write_text(f"# {name}\n", encoding="utf-8")
    if os.name == "nt":
        _touch_executable(runtime / "playwright-browsers" / "chromium-1228" / "chrome-win" / "chrome.exe")
    else:
        _touch_executable(runtime / "playwright-browsers" / "chromium-1228" / "chrome-linux" / "chrome")

    output = tmp_path / "baseline.json"
    exit_code = module.main([
        "check-web-core-runtime-baseline.py",
        "--runtime-root",
        str(runtime),
        "--state-root",
        str(state),
        "--output",
        str(output),
        "--strict",
    ])
    report = json.loads(output.read_text(encoding="utf-8"))
    chromium = next(item for item in report["dependencies"] if item["name"] == "playwright_chromium")

    assert exit_code == 0
    assert report["summary"]["releaseReady"] is True
    assert chromium["source"] == "ecorex-bundled-playwright"


def test_s9_web_release_gate_generates_required_snapshots(monkeypatch, tmp_path):
    module = _load_web_release_gate_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    output = tmp_path / "gate"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    state.mkdir()
    workspace.mkdir()
    (runtime / "capabilities.json").write_text(json.dumps({
        "schemaVersion": 1,
        "packs": [
            {
                "id": "tongxin-cli",
                "name": "Tongxin",
                "configureOnly": True,
                "configureAction": "configure-capability --pack-id tongxin-cli",
            },
            {
                "id": "feishu-lark",
                "name": "Feishu",
                "discoveryOnly": True,
                "discoverAction": "find-skill --capability feishu-lark",
                "repairAction": "install-capability --action repair --pack-id feishu-lark",
            },
            {
                "id": "office-pdf",
                "name": "Office",
                "requirements": ["pypdf"],
                "moduleChecks": ["pypdf"],
                "repairAction": "install-capability --action repair --pack-id office-pdf",
            },
        ],
    }), encoding="utf-8")
    baseline = tmp_path / "runtime-baseline.json"
    baseline.write_text(json.dumps({
        "schemaVersion": "web-core-runtime-baseline-v1",
        "summary": {"releaseReady": True, "blocking": 0, "blockingNames": []},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_capture_capability_state", lambda *_args, **_kwargs: {
        "schemaVersion": "web-capability-state-snapshot-v1",
        "generatedAt": "2026-07-01T00:00:00+00:00",
        "status": "success",
        "capabilities": {"workspace": str(workspace)},
        "summary": {"total": 3},
        "visualWorkflow": {},
    })

    report = module.capture_release_gate(Namespace(
        runtime_root=runtime,
        state_root=state,
        workspace_root=workspace,
        output_dir=output,
        baseline_input=baseline,
        skip_baseline_capture=False,
        strict=True,
    ))

    assert report["schemaVersion"] == "web-release-gate-v1"
    assert report["summary"]["releaseReady"] is True
    assert (output / "runtime-baseline.json").is_file()
    assert (output / "capability-state.json").is_file()
    assert (output / "permission-matrix.json").is_file()
    assert (output / "review-consensus.md").is_file()
    assert (output / "web-release-gate.json").is_file()
    assert not (workspace / "release-gate-note.md").exists()
    assert report["checks"]["capabilityManifest"]["artifact"] == "web-release-gate.json#manifestAudit"
    matrix = json.loads((output / "permission-matrix.json").read_text(encoding="utf-8"))
    capability_state = json.loads((output / "capability-state.json").read_text(encoding="utf-8"))
    assert str(workspace) not in json.dumps(capability_state)
    assert capability_state["capabilities"]["workspace"] == "%WORKSPACE_ROOT%"
    assert matrix["providedWorkspaceMutated"] is False
    assert matrix["probeWorkspace"] == "temporary-cleaned"
    rows = {(row["mode"], row["caseId"]): row for row in matrix["rows"]}
    assert rows[("read-only", "bash.system_shell")]["allowed"] is False
    assert rows[("full-access", "bash.system_shell")]["allowed"] is True
    assert rows[("read-only", "optional_abilities.status")]["allowed"] is True
    assert rows[("custom", "bash.workspace_write")]["allowed"] is True


def test_v027_release_gate_failure_artifacts_redact_paths_and_secrets(monkeypatch, tmp_path):
    module = _load_web_release_gate_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    output = tmp_path / "gate"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    state.mkdir()
    workspace.mkdir()
    secret_base = "https://custom-gemini-secret.example/v1"
    secret_key = "sk-releasegatefailure123456789"
    secret_token = "xoxb-releasegatefailure-token"

    def boom(*_args, **_kwargs):
        raise RuntimeError(
            f"failed in {workspace} with api_base={secret_base} "
            f"open_ai_api_key={secret_key} token={secret_token}"
        )

    monkeypatch.setattr(module, "_capture_release_gate_inner", boom)

    report = module.capture_release_gate(Namespace(
        runtime_root=runtime,
        state_root=state,
        workspace_root=workspace,
        output_dir=output,
        baseline_input=None,
        skip_baseline_capture=False,
        strict=True,
    ))

    artifact_names = [
        "runtime-baseline.json",
        "capability-state.json",
        "permission-matrix.json",
        "web-release-gate.json",
    ]
    serialized = "\n".join((output / name).read_text(encoding="utf-8") for name in artifact_names)

    assert report["summary"]["releaseReady"] is False
    assert "%WORKSPACE_ROOT%" in serialized
    for raw in (str(workspace), str(workspace).replace("\\", "/"), secret_base, secret_key, secret_token):
        assert raw not in serialized
    assert "api_base=***" in serialized
    assert "open_ai_api_key=***" in serialized
    assert "token=***" in serialized


def test_web_state_event_consistency_smoke_catches_frontend_backend_drift():
    module = _load_web_state_event_smoke_module()

    marker_text = "\n".join(module.FRONTEND_MARKERS.values())
    marker_report = module._asset_marker_report(marker_text)
    assert marker_report["status"] == "ok"

    missing_report = module._asset_marker_report(marker_text.replace("ensureModelReady", ""))
    assert missing_report["status"] == "missing_markers"
    assert "model_ready_gate_before_message" in missing_report["missing"]

    models_payload = {
        "capabilities": {
            "chat": {
                "current_provider": "deepseek",
                "current_model": "deepseek-v4-pro",
            },
            "image": {
                "fallback_model": "gpt-image-2-pro",
            },
        },
    }
    config = {
        "bot_type": "deepseek",
        "model": "deepseek-v4-pro",
        "text_to_image": "gpt-image-2-pro",
    }
    assert module.compare_model_state(models_payload, config)["status"] == "ok"

    drifted = dict(config)
    drifted["model"] = "gpt-5.5"
    comparison = module.compare_model_state(models_payload, drifted)
    assert comparison["status"] == "mismatch"
    assert comparison["checks"]["chatModelMatchesConfig"] is False


def test_s9_capability_manifest_audit_blocks_missing_repair_and_private_paths(tmp_path):
    module = _load_web_release_gate_module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "capabilities.json").write_text(json.dumps({
        "schemaVersion": 1,
        "packs": [
            {
                "id": "bad-pack",
                "name": "Bad",
                "requirements": ["baddep"],
                "moduleChecks": ["baddep"],
                "stateDir": "/tmp/private-state",
            },
            {
                "id": "discover-pack",
                "name": "Discover",
                "discoveryOnly": True,
                "discoverAction": "ask agent to discover",
            },
            {
                "id": "config-pack",
                "name": "Config",
                "configureOnly": True,
                "configureAction": "open settings maybe",
                "install_root": "/tmp/private-install",
            },
        ],
    }), encoding="utf-8")

    audit = module._audit_capability_manifest(runtime)
    messages = "\n".join(item["message"] for item in audit["blockers"])

    assert audit["status"] == "fail"
    assert "repairAction=install-capability --action repair --pack-id bad-pack" in messages
    assert "forbidden private runtime/path field" in messages
    assert "discoverAction=find-skill --capability discover-pack" in messages
    assert "configureAction=configure-capability --pack-id config-pack" in messages


def test_s9_release_gate_writes_failure_artifacts_for_bad_manifest(tmp_path):
    module = _load_web_release_gate_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    output = tmp_path / "gate"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    state.mkdir()
    workspace.mkdir()
    (runtime / "capabilities.json").write_text("{not-json", encoding="utf-8")
    baseline = tmp_path / "runtime-baseline.json"
    baseline.write_text(json.dumps({
        "schemaVersion": "web-core-runtime-baseline-v1",
        "summary": {"releaseReady": True, "blocking": 0, "blockingNames": []},
    }), encoding="utf-8")

    report = module.capture_release_gate(Namespace(
        runtime_root=runtime,
        state_root=state,
        workspace_root=workspace,
        output_dir=output,
        baseline_input=baseline,
        skip_baseline_capture=False,
        strict=True,
    ))

    assert report["summary"]["releaseReady"] is False
    assert "capabilityManifest" in report["summary"]["blockingChecks"]
    assert (output / "runtime-baseline.json").is_file()
    assert (output / "capability-state.json").is_file()
    assert (output / "permission-matrix.json").is_file()
    assert (output / "review-consensus.md").is_file()
    assert (output / "web-release-gate.json").is_file()
    gate = json.loads((output / "web-release-gate.json").read_text(encoding="utf-8"))
    messages = json.dumps(gate["manifestAudit"]["blockers"], ensure_ascii=False)
    assert "not readable JSON" in messages


def test_s9_release_gate_writes_failure_artifacts_when_matrix_crashes(monkeypatch, tmp_path):
    module = _load_web_release_gate_module()
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    output = tmp_path / "gate"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    state.mkdir()
    workspace.mkdir()
    (runtime / "capabilities.json").write_text(json.dumps({"schemaVersion": 1, "packs": []}), encoding="utf-8")
    baseline = tmp_path / "runtime-baseline.json"
    baseline.write_text(json.dumps({
        "schemaVersion": "web-core-runtime-baseline-v1",
        "summary": {"releaseReady": True, "blocking": 0, "blockingNames": []},
    }), encoding="utf-8")
    output.mkdir()
    (output / "capability-state.json").write_text(json.dumps({
        "schemaVersion": "web-capability-state-snapshot-v1",
        "status": "success",
        "summary": {"stale": True},
    }), encoding="utf-8")
    (output / "permission-matrix.json").write_text(json.dumps({
        "schemaVersion": "web-permission-matrix-v1",
        "status": "pass",
        "summary": {"blockers": 0, "stale": True},
        "blockers": [],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_generate_permission_matrix", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("matrix boom")))

    report = module.capture_release_gate(Namespace(
        runtime_root=runtime,
        state_root=state,
        workspace_root=workspace,
        output_dir=output,
        baseline_input=baseline,
        skip_baseline_capture=False,
        strict=True,
    ))

    assert report["summary"]["releaseReady"] is False
    assert report["summary"]["blockingChecks"] == ["releaseGate"]
    assert (output / "capability-state.json").is_file()
    assert (output / "permission-matrix.json").is_file()
    assert (output / "web-release-gate.json").is_file()
    capability = json.loads((output / "capability-state.json").read_text(encoding="utf-8"))
    matrix = json.loads((output / "permission-matrix.json").read_text(encoding="utf-8"))
    consensus = (output / "review-consensus.md").read_text(encoding="utf-8")
    assert capability["status"] == "error"
    assert matrix["status"] == "fail"
    assert "matrix boom" in json.dumps(matrix["blockers"], ensure_ascii=False)
    assert "matrix boom" in consensus


def test_s9_release_scripts_require_gate_outputs():
    checker = (ROOT / "scripts" / "check-ecorex-web-release.sh").read_text(encoding="utf-8")
    preparer = (ROOT / "scripts" / "prepare-ecorex-web-release.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-ecorex-web.sh").read_text(encoding="utf-8")

    assert "/runtime/scripts/generate-web-runtime-release-gate.py" in checker
    assert "capability-state.json" in checker
    assert "permission-matrix.json" in checker
    assert "review-consensus.md" in checker
    assert "web-release-gate.json" in checker
    assert "generate-web-runtime-release-gate.py" in preparer
    assert "run_web_release_gate" in installer
    assert "--baseline-input \"$STATE_DIR/runtime-baseline.json\"" in installer
    assert "EXPECTED_SHA256 is required for online Web release installs" in installer
    assert installer.index("run_web_release_gate \"$runtime_dir\"") < installer.index("ln -sfn \"$release_dir\" \"$INSTALL_ROOT/current\"")


def test_s9_permission_broker_masks_bearer_and_json_tokens():
    from common.ecorex_tool_permissions import _mask_sensitive

    text = (
        'curl -H "Authorization: Bearer eyJ-secret-token-12345" '
        '{"token": "Bearer hidden-secret-value", "authorization": "Bearer another-secret-value", "password": "pw-secret"} '
        "api_key=plain-secret-value"
    )
    masked = _mask_sensitive(text)

    assert "eyJ-secret-token" not in masked
    assert "hidden-secret-value" not in masked
    assert "another-secret-value" not in masked
    assert "pw-secret" not in masked
    assert "plain-secret-value" not in masked
    assert "Bearer ***" in masked


def test_config_env_override_parser_does_not_eval_code(monkeypatch):
    import config

    called = {"value": False}

    def fake_system(_command):
        called["value"] = True
        return 0

    monkeypatch.setattr(config.os, "system", fake_system)
    parsed = config._parse_env_config_value(
        "group_name_white_list",
        "__import__('os').system('echo unsafe')",
    )

    assert called["value"] is False
    assert parsed == config.available_setting["group_name_white_list"]
    assert config._parse_env_config_value("debug", "true") is True
    assert config._parse_env_config_value("debug", "maybe") is False
    assert config._parse_env_config_value("model", "false") == "false"
    assert config._parse_env_config_value("conversation_max_tokens", "2048") == 2048
    assert config._parse_env_config_value("model_fallbacks", '["a", {"model": "b"}]') == ["a", {"model": "b"}]


def test_config_user_datas_json_round_trip(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "get_appdata_dir", lambda: str(tmp_path))
    cfg = config.Config()
    cfg.user_datas = {"alice": {"count": 1, "items": ["x", True]}}

    cfg.save_user_datas()
    assert (tmp_path / "user_datas.json").is_file()
    assert not (tmp_path / "user_datas.pkl").exists()

    loaded = config.Config()
    loaded.load_user_datas()
    assert loaded.user_datas == cfg.user_datas
    assert json.loads((tmp_path / "user_datas.json").read_text(encoding="utf-8")) == cfg.user_datas


def test_config_safe_legacy_pickle_migrates_to_json(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "get_appdata_dir", lambda: str(tmp_path))
    (tmp_path / "user_datas.pkl").write_bytes(pickle.dumps({"bob": {"ok": [1, "yes"]}}))

    cfg = config.Config()
    cfg.load_user_datas()

    assert cfg.user_datas == {"bob": {"ok": [1, "yes"]}}
    assert (tmp_path / "user_datas.json").is_file()


def test_config_malicious_legacy_pickle_is_rejected(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "get_appdata_dir", lambda: str(tmp_path))
    (tmp_path / "user_datas.pkl").write_bytes(b"cos\nsystem\n(S'echo unsafe'\ntR.")

    cfg = config.Config()
    cfg.load_user_datas()

    assert cfg.user_datas == {}
    assert not (tmp_path / "user_datas.json").exists()


def test_web_install_root_node_is_owned_runtime(tmp_path):
    from common.runtime_dependencies import SOURCE_ECOREX_STATE, RuntimeDependencyProvider

    install_root = tmp_path / "ecorex-web"
    runtime = install_root / "releases" / "rel-1" / "runtime"
    state = install_root / "state"
    executable_names = ["node.exe", "npm.cmd", "npx.cmd"] if os.name == "nt" else ["node", "npm", "npx"]
    for name in executable_names:
        _touch_executable(install_root / "node" / "bin" / name)

    provider = RuntimeDependencyProvider(
        runtime,
        state,
        env={
            "PATH": "",
            "ECOREX_INSTALL_ROOT": str(install_root),
            "ECOREX_NODE_ROOT": str(install_root / "node"),
        },
    )

    for executable in ("node", "npm", "npx"):
        dependency = provider.resolve_executable(executable)
        assert dependency.available is True
        assert dependency.source == SOURCE_ECOREX_STATE


def test_models_package_does_not_fabricate_database_aliases():
    import models

    assert getattr(models, "__all__", []) == []
    assert not hasattr(models, "database")
    assert not hasattr(models, "DATABASE")


def test_s3_public_runtime_packs_are_shared_source_of_truth():
    public_root = ROOT / "runtime-packs"
    desktop_root = ROOT / "desktop" / "runtime-packs"

    assert (public_root / "core-requirements.txt").read_text(encoding="utf-8") == (
        desktop_root / "core-requirements.txt"
    ).read_text(encoding="utf-8")
    assert (public_root / "capabilities.json").read_text(encoding="utf-8") == (
        desktop_root / "capabilities.json"
    ).read_text(encoding="utf-8")

    web_release = (ROOT / "scripts" / "prepare-ecorex-web-release.ps1").read_text(encoding="utf-8")
    optional_abilities = (ROOT / "agent" / "tools" / "optional_abilities" / "optional_abilities.py").read_text(encoding="utf-8")
    desktop_wrapper = (ROOT / "desktop" / "scripts" / "install-capability.py").read_text(encoding="utf-8")

    assert 'Resolve-RequiredPath (Join-Path $repoRoot "runtime-packs")' in web_release
    assert 'RUNTIME_ROOT / "runtime-packs" / "capabilities.json"' in optional_abilities
    assert "runpy.run_path" in desktop_wrapper
    assert "pip" not in desktop_wrapper


def test_s3_install_capability_status_uses_unified_state_and_target_dirs(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    packages = state_root / "capability-packages"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({
            "packs": [
                {
                    "id": "needs-module",
                    "name": "Needs Module",
                    "moduleChecks": ["definitely_missing_s3_module"],
                    "requirements": [],
                }
            ]
        }),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["ECOREX_STATE_DIR"] = str(state_root)
    env["ECOREX_CAPABILITY_STATE_DIR"] = str(state)
    env["ECOREX_CAPABILITY_TARGET_DIR"] = str(packages)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--action",
            "status",
            "--pack-id",
            "needs-module",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    status_file = state / "needs-module.json"
    status = json.loads(status_file.read_text(encoding="utf-8"))
    expected_target = packages / "needs-module"

    assert payload["action"] == "status"
    assert payload["capabilityState"]["state"] == "missing_dependency"
    assert status["state"] == "missing_dependency"
    assert Path(status["targetDir"]) == expected_target
    assert status["nextAction"] == "repair"


def test_s3_install_capability_doctor_writes_summary(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({
            "packs": [
                {"id": "ready-pack", "name": "Ready", "moduleChecks": []},
                {"id": "missing-pack", "name": "Missing", "moduleChecks": ["definitely_missing_s3_doctor"]},
            ]
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--action",
            "doctor",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(state),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "ECOREX_STATE_DIR": str(state_root)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    doctor = json.loads((state / "capability-doctor.json").read_text(encoding="utf-8"))

    assert payload["summary"] == doctor["summary"]
    assert payload["summary"]["packs"] == 2
    assert payload["summary"]["blocking"] == 1


def test_s3_install_capability_discovery_state_redacts_source_urls(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({
            "packs": [
                {
                    "id": "secret-discovery",
                    "name": "Secret Discovery",
                    "discoveryOnly": True,
                    "sourceUrl": "https://token@example.invalid/source",
                    "mirrorUrls": ["https://secret@example.invalid/simple"],
                    "installHint": "use discovery",
                }
            ]
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--pack-id",
            "secret-discovery",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(state),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "ECOREX_STATE_DIR": str(state_root)},
        timeout=30,
    )

    assert result.returncode == 4
    serialized = result.stdout + (state / "secret-discovery.json").read_text(encoding="utf-8")
    assert "token@example.invalid" not in serialized
    assert "secret@example.invalid" not in serialized
    assert '"sourceConfigured": true' in serialized
    assert '"mirrorConfigured": true' in serialized


def test_s3_install_capability_rejects_unowned_state_dir(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(json.dumps({"packs": [{"id": "safe", "moduleChecks": []}]}), encoding="utf-8")
    outside = tmp_path / "outside" / "capability-state"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--action",
            "status",
            "--pack-id",
            "safe",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(outside),
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "owned state/runtime" in result.stdout
    assert not outside.exists()


def test_s3_install_capability_sanitizes_pack_id_state_paths(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({
            "packs": [{"id": "../escape", "name": "Escape", "moduleChecks": ["definitely_missing_escape"]}]
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--action",
            "status",
            "--pack-id",
            "../escape",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(state),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "ECOREX_STATE_DIR": str(state_root)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (state / "escape.json").exists()
    assert not (state_root / "escape.json").exists()


def test_s3_install_capability_does_not_probe_host_python_modules(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({"packs": [{"id": "host-json", "name": "Host JSON", "moduleChecks": ["json"]}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install-capability.py"),
            "--action",
            "status",
            "--pack-id",
            "host-json",
            "--runtime-dir",
            str(runtime),
            "--manifest",
            str(manifest),
            "--index-dir",
            str(state),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "ECOREX_STATE_DIR": str(state_root)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["capabilityState"]["state"] == "missing_dependency"
    assert payload["capabilityState"]["installed"] is False


def test_s3_install_capability_configure_only_needs_configuration(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_root = tmp_path / "state"
    state = state_root / "capability-state"
    manifest = tmp_path / "capabilities.json"
    manifest.write_text(
        json.dumps({
            "packs": [
                {
                    "id": "config-only",
                    "name": "Config Only",
                    "configureOnly": True,
                    "moduleChecks": [],
                    "requirements": [],
                    "installHint": "configure me",
                }
            ]
        }),
        encoding="utf-8",
    )
    common_command = [
        sys.executable,
        str(ROOT / "scripts" / "install-capability.py"),
        "--pack-id",
        "config-only",
        "--runtime-dir",
        str(runtime),
        "--manifest",
        str(manifest),
        "--index-dir",
        str(state),
    ]
    env = {**os.environ, "ECOREX_STATE_DIR": str(state_root)}

    status = subprocess.run(
        [*common_command, "--action", "status"],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    install = subprocess.run(common_command, text=True, capture_output=True, env=env, timeout=30)

    assert status.returncode == 0, status.stderr
    assert install.returncode == 4
    payload = json.loads(status.stdout)
    state_payload = json.loads((state / "config-only.json").read_text(encoding="utf-8"))
    assert payload["capabilityState"]["state"] == "needs_configuration"
    assert payload["capabilityState"]["installed"] is False
    assert state_payload["state"] == "needs_configuration"
    assert state_payload["installed"] is False


def test_s3_install_capability_redacts_secret_urls_in_logs(tmp_path):
    module = _load_install_capability_module()
    log_path = tmp_path / "install.log"

    with log_path.open("w", encoding="utf-8") as log:
        try:
            module.run_logged(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('https://token@example.invalid/simple?access_token=abc'); sys.exit(3)",
                    "--index-url",
                    "https://token@example.invalid/simple?access_token=abc",
                ],
                log,
                os.environ.copy(),
                30,
            )
        except RuntimeError as exc:
            error = str(exc)
        else:
            raise AssertionError("run_logged should fail")

    serialized = log_path.read_text(encoding="utf-8") + error
    assert "token@example.invalid" not in serialized
    assert "access_token=abc" not in serialized
    assert "https://***@example.invalid/simple?access_token=%2A%2A%2A" in serialized


def test_s4_capability_service_returns_typed_action_plans_for_key_packs():
    from agent.runtime_capabilities import CapabilityService

    class FakeRegistry:
        workspace_root = "workspace"

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "generatedAt": "2026-06-30T00:00:00Z",
                "abilities": [
                    {
                        "id": "feishu-cli",
                        "label": "Feishu/Lark CLI connector",
                        "kind": "optional-runtime",
                        "packId": "feishu-lark",
                        "installHint": "Use find-skill first.",
                        "capabilityState": {"installed": False, "state": "not-installed"},
                    },
                    {
                        "id": "tongxin-cli",
                        "label": "Tongxin",
                        "kind": "capability-pack",
                        "packId": "tongxin-cli",
                        "configureOnly": True,
                        "capabilityState": {
                            "installed": False,
                            "state": "needs_configuration",
                            "configureOnly": True,
                            "message": "Configure the read-only CLI.",
                        },
                    },
                    {
                        "id": "office-pdf",
                        "label": "Office/PDF",
                        "kind": "capability-pack",
                        "packId": "office-pdf",
                        "capabilityState": {
                            "installed": False,
                            "state": "missing_dependency",
                            "missingModules": ["pypdf", "fitz"],
                            "message": "Office parser dependencies are missing.",
                            "logPath": str(ROOT / "state" / "capability-state" / "office-pdf.log"),
                            "targetDir": str(ROOT / "state" / "capability-packages" / "office-pdf"),
                        },
                    },
                    {
                        "id": "fast-ocr",
                        "label": "Fast OCR",
                        "kind": "capability-pack",
                        "packId": "fast-ocr",
                        "capabilityState": {
                            "installed": False,
                            "state": "missing_dependency",
                            "missingModules": ["rapidocr_onnxruntime", "PIL"],
                        },
                    },
                ],
            }

    payload = CapabilityService(FakeRegistry()).capabilities_payload(include_related=False)
    by_pack = {item["packId"]: item for item in payload["packs"]}

    assert payload["source"] == "runtime-capability-service"
    assert by_pack["feishu-lark"]["state"] == "discovery_only"
    assert by_pack["feishu-lark"]["nextAction"] == "discover"
    assert by_pack["tongxin-cli"]["state"] == "needs_configuration"
    assert by_pack["tongxin-cli"]["nextAction"] == "configure"
    assert by_pack["office-pdf"]["nextAction"] == "repair"
    assert by_pack["office-pdf"]["missingItems"] == ["pypdf", "fitz"]
    assert by_pack["office-pdf"]["logRef"] == {
        "present": True,
        "name": "office-pdf.log",
        "parentName": "capability-state",
        "redacted": True,
    }
    assert by_pack["office-pdf"]["targetRef"] == {
        "present": True,
        "name": "office-pdf",
        "parentName": "capability-packages",
        "redacted": True,
    }
    serialized = json.dumps(payload)
    assert str(ROOT) not in serialized
    assert "logPath" not in serialized
    assert "targetDir" not in serialized
    assert by_pack["fast-ocr"]["nextAction"] == "repair"
    assert by_pack["fast-ocr"]["actionPlan"]["missingItems"] == ["rapidocr_onnxruntime", "PIL"]
    assert payload["summary"]["needsConfiguration"] == 1
    assert payload["summary"]["discoveryOnly"] == 1
    assert payload["summary"]["repairable"] == 2


def test_s4_registry_merges_installer_status_probe_for_unresolved_capability_pack(monkeypatch):
    from agent import runtime_capabilities

    class FakeOptionalAbilities:
        def execute(self, _params):
            return types.SimpleNamespace(result={
                "status": "success",
                "abilities": [
                    {
                        "id": "office-pdf",
                        "label": "Office/PDF",
                        "kind": "capability-pack",
                        "packId": "office-pdf",
                        "capabilityState": {"installed": False, "state": "not-installed"},
                    }
                ],
            })

    calls = []

    def fake_status_probe(pack_id):
        calls.append(pack_id)
        return {
            "installed": False,
            "state": "missing_dependency",
            "missingModules": ["pypdf"],
            "nextAction": "repair",
            "logPath": str(ROOT / "capability-state" / "office-pdf.log"),
            "targetDir": str(ROOT / "capability-packages" / "office-pdf"),
        }

    monkeypatch.setattr("agent.tools.optional_abilities.optional_abilities.OptionalAbilities", FakeOptionalAbilities)
    monkeypatch.setattr(runtime_capabilities, "_installer_status_state", fake_status_probe)

    payload = runtime_capabilities.CapabilityService(
        runtime_capabilities.RuntimeCapabilityRegistry("workspace")
    ).capabilities_payload(include_related=False)
    pack = payload["packs"][0]

    assert calls == ["office-pdf"]
    assert pack["state"] == "missing_dependency"
    assert pack["nextAction"] == "repair"
    assert pack["missingItems"] == ["pypdf"]
    serialized = json.dumps(payload)
    assert str(ROOT) not in serialized
    assert "logPath" not in serialized
    assert "targetDir" not in serialized


def test_s4_capability_service_keeps_imagegen_skill_tool_extension_consistent():
    from agent.runtime_capabilities import CapabilityService

    class FakeRegistry:
        workspace_root = "workspace"

        def optional_abilities_payload(self):
            return {"status": "success", "abilities": []}

        def tools_payload(self):
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "tools": [{"name": "imagegen", "description": "Generate images", "parameters": {}}],
            }

        def skills_payload(self):
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "skills": [{"name": "image-generation", "toolName": "imagegen"}],
            }

        def extensions_payload(self, action_plans=None):
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "extensions": [
                    {
                        "id": "skill:image-generation",
                        "type": "builtin_skill",
                        "toolName": "imagegen",
                        "toolBinding": {"toolName": "imagegen", "probe": {"action": "probe"}},
                    }
                ],
            }

    payload = CapabilityService(FakeRegistry()).capabilities_payload(include_related=True)
    assert any(item["name"] == "imagegen" for item in payload["tools"]["tools"])
    assert any(item["name"] == "image-generation" and item["toolName"] == "imagegen" for item in payload["skills"]["skills"])
    image_ext = next(item for item in payload["extensions"]["extensions"] if item["id"] == "skill:image-generation")
    assert image_ext["toolName"] == "imagegen"
    assert image_ext["toolBinding"]["toolName"] == "imagegen"


def test_s4_web_handlers_read_runtime_capability_service(monkeypatch):
    from channel.web import web_channel

    class FakeRegistry:
        def __init__(self, workspace_root=None, **_kwargs):
            self.workspace_root = workspace_root or "workspace"

        def tools_payload(self):
            return {"status": "success", "source": "runtime-capability-service", "tools": [{"name": "fake_tool"}], "toolCount": 1}

        def skills_payload(self):
            return {"status": "success", "source": "runtime-capability-service", "skills": [{"name": "fake-skill"}], "skillCount": 1}

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "abilities": [
                    {
                        "id": "office-pdf",
                        "label": "Office/PDF",
                        "kind": "capability-pack",
                        "packId": "office-pdf",
                        "capabilityState": {"installed": False, "state": "missing_dependency", "missingModules": ["pypdf"]},
                    }
                ],
            }

        def extensions_payload(self, action_plans=None):
            ext = {"id": "ability:office-pdf", "type": "capability_pack", "packId": "office-pdf"}
            if action_plans:
                ext["actionPlan"] = action_plans[0]["actionPlan"]
            return {"status": "success", "source": "runtime-capability-service", "extensions": [ext], "count": 1, "summary": {"capability_pack": 1}}

    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: "workspace")
    with patch.object(web_channel, "_require_auth", return_value=None), \
            patch("agent.runtime_capabilities.RuntimeCapabilityRegistry", FakeRegistry):
        tools = json.loads(web_channel.ToolsHandler().GET())
        skills = json.loads(web_channel.SkillsHandler().GET())
        capabilities = json.loads(web_channel.CapabilitiesHandler().GET())
        extensions = json.loads(web_channel.ExtensionsHandler().GET())

    assert tools["source"] == "runtime-capability-service"
    assert skills["source"] == "runtime-capability-service"
    assert capabilities["source"] == "runtime-capability-service"
    assert extensions["source"] == "runtime-capability-service"
    assert capabilities["packs"][0]["packId"] == "office-pdf"
    assert capabilities["packs"][0]["nextAction"] == "repair"
    assert extensions["extensions"][0]["actionPlan"]["nextAction"] == "repair"


def test_s4_agent_capability_diagnose_uses_runtime_capability_service(monkeypatch):
    from agent.tools.agent_capability.agent_capability import AgentCapabilityTool

    class FakeService:
        def __init__(self, workspace_root=None, registry=None):
            self.workspace_root = workspace_root

        def diagnose_payload(self):
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "workspace": self.workspace_root,
                "abilities": {"abilities": [{"packId": "office-pdf", "nextAction": "repair"}]},
                "skills": [],
                "tools": [],
                "extensions": [],
                "mcpStatus": {},
            }

    monkeypatch.setattr("agent.runtime_capabilities.CapabilityService", FakeService)

    result = AgentCapabilityTool().execute({"action": "diagnose"})
    assert result.status == "success"
    assert result.result["source"] == "runtime-capability-service"
    assert result.result["abilities"]["abilities"][0]["nextAction"] == "repair"


def test_s4_agent_capability_list_packs_uses_runtime_capability_service(monkeypatch):
    from agent.tools.agent_capability.agent_capability import AgentCapabilityTool

    class FakeService:
        def __init__(self, workspace_root=None, registry=None):
            self.workspace_root = workspace_root

        def capabilities_payload(self, include_related=True):
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "workspace": self.workspace_root,
                "packs": [{"packId": "office-pdf", "nextAction": "repair"}],
            }

    monkeypatch.setattr("agent.runtime_capabilities.CapabilityService", FakeService)

    result = AgentCapabilityTool().execute({"action": "list_packs"})
    assert result.status == "success"
    assert result.result["source"] == "runtime-capability-service"
    assert result.result["packs"][0]["nextAction"] == "repair"


def test_s6_visual_workflow_classifies_ocr_repair_and_model_credentials(monkeypatch):
    from agent import runtime_capabilities

    class FakeRegistry:
        workspace_root = "workspace"

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "abilities": [
                    {
                        "id": "fast-ocr",
                        "label": "Fast OCR",
                        "kind": "capability-pack",
                        "packId": "fast-ocr",
                        "capabilityState": {
                            "installed": False,
                            "state": "missing_dependency",
                            "missingModules": ["rapidocr_onnxruntime", "PIL"],
                        },
                    }
                ],
            }

        def tools_payload(self):
            return {
                "status": "success",
                "tools": [{"name": "ocr"}, {"name": "vision"}, {"name": "imagegen"}],
            }

        def skills_payload(self):
            return {"status": "success", "skills": [{"name": "image-generation", "toolName": "imagegen"}]}

        def extensions_payload(self, action_plans=None):
            return {"status": "success", "extensions": []}

    monkeypatch.setattr(runtime_capabilities, "_load_runtime_config", lambda: {})

    payload = runtime_capabilities.CapabilityService(FakeRegistry()).capabilities_payload()
    visual = payload["visualWorkflow"]

    assert visual["schemaVersion"] == "visual-workflow-v1"
    assert visual["imageInput"]["supported"] is True
    assert visual["imageInput"]["autoDetect"] is True
    assert visual["imageInput"]["acceptedMimePrefixes"] == ["image/"]
    assert visual["imageInput"]["attachmentTypes"] == ["image"]
    assert visual["ocr"]["nextAction"] == "repair_fast_ocr"
    assert visual["ocr"]["missingItems"] == ["rapidocr_onnxruntime", "PIL"]
    assert visual["vision"]["state"] == "needs_provider_credentials"
    assert visual["vision"]["nextAction"] == "configure_model_provider"
    assert visual["imagegen"]["state"] == "needs_provider_credentials"
    assert visual["imagegen"]["nextAction"] == "configure_model_provider"
    assert visual["imagegen"]["model"] == "gpt-image-2-pro"
    assert visual["imagegen"]["routeVisible"] is True
    assert "tool_route" not in visual["imagegen"]["missingItems"]


def test_s6_visual_workflow_ready_with_provider_credentials(monkeypatch):
    from agent import runtime_capabilities

    class FakeRegistry:
        workspace_root = "workspace"

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "abilities": [
                    {
                        "id": "fast-ocr",
                        "label": "Fast OCR",
                        "kind": "capability-pack",
                        "packId": "fast-ocr",
                        "capabilityState": {"installed": True, "state": "installed"},
                    }
                ],
            }

        def tools_payload(self):
            return {"status": "success", "tools": [{"name": "ocr"}, {"name": "vision"}, {"name": "imagegen"}]}

        def skills_payload(self):
            return {"status": "success", "skills": [{"name": "image-generation", "toolName": "imagegen"}]}

        def extensions_payload(self, action_plans=None):
            return {"status": "success", "extensions": []}

    monkeypatch.setattr(runtime_capabilities, "_load_runtime_config", lambda: {"open_ai_api_key": "sk-realistic-test-key"})

    visual = runtime_capabilities.CapabilityService(FakeRegistry()).capabilities_payload()["visualWorkflow"]

    assert visual["ocr"]["state"] == "ready"
    assert visual["vision"]["state"] == "ready"
    assert visual["vision"]["configuredProvider"] == "openai"
    assert visual["imagegen"]["state"] == "ready"
    assert visual["imagegen"]["configuredProvider"] == "openai"
    assert visual["imagegen"]["model"] == "gpt-image-2-pro"
    assert visual["overall"]["state"] == "ready"
    assert "sk-realistic-test-key" not in json.dumps(visual)


def test_s6_visual_workflow_uses_vision_fallback_when_ocr_missing(monkeypatch):
    from agent import runtime_capabilities

    class FakeRegistry:
        workspace_root = "workspace"

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "abilities": [
                    {
                        "id": "fast-ocr",
                        "label": "Fast OCR",
                        "kind": "capability-pack",
                        "packId": "fast-ocr",
                        "capabilityState": {"installed": False, "state": "missing_dependency", "missingModules": ["rapidocr_onnxruntime"]},
                    }
                ],
            }

        def tools_payload(self):
            return {"status": "success", "tools": [{"name": "ocr"}, {"name": "vision"}, {"name": "imagegen"}]}

        def skills_payload(self):
            return {"status": "success", "skills": [{"name": "image-generation", "toolName": "imagegen"}]}

        def extensions_payload(self, action_plans=None):
            return {"status": "success", "extensions": []}

    monkeypatch.setattr(runtime_capabilities, "_load_runtime_config", lambda: {"open_ai_api_key": "sk-realistic-test-key"})

    visual = runtime_capabilities.CapabilityService(FakeRegistry()).capabilities_payload()["visualWorkflow"]

    assert visual["ocr"]["nextAction"] == "repair_fast_ocr"
    assert visual["vision"]["state"] == "ready"
    assert visual["overall"]["state"] == "degraded"
    assert visual["overall"]["visionFallbackAvailable"] is True


def test_s6_web_capabilities_exposes_visual_workflow(monkeypatch):
    from channel.web import web_channel
    from agent import runtime_capabilities

    class FakeRegistry:
        def __init__(self, workspace_root=None, **_kwargs):
            self.workspace_root = workspace_root or "workspace"

        def optional_abilities_payload(self):
            return {
                "status": "success",
                "abilities": [
                    {
                        "id": "fast-ocr",
                        "label": "Fast OCR",
                        "kind": "capability-pack",
                        "packId": "fast-ocr",
                        "capabilityState": {"installed": False, "state": "missing_dependency", "missingModules": ["rapidocr_onnxruntime"]},
                    }
                ],
            }

        def tools_payload(self):
            return {"status": "success", "source": "runtime-capability-service", "tools": [{"name": "ocr"}, {"name": "vision"}, {"name": "imagegen"}]}

        def skills_payload(self):
            return {"status": "success", "source": "runtime-capability-service", "skills": [{"name": "image-generation", "toolName": "imagegen"}]}

        def extensions_payload(self, action_plans=None):
            return {"status": "success", "extensions": []}

    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: "workspace")
    monkeypatch.setattr(runtime_capabilities, "_load_runtime_config", lambda: {})
    with patch.object(web_channel, "_require_auth", return_value=None), \
            patch("agent.runtime_capabilities.RuntimeCapabilityRegistry", FakeRegistry):
        payload = json.loads(web_channel.CapabilitiesHandler().GET())

    visual = payload["visualWorkflow"]
    assert payload["source"] == "runtime-capability-service"
    assert visual["ocr"]["nextAction"] == "repair_fast_ocr"
    assert visual["vision"]["nextAction"] == "configure_model_provider"
    assert visual["imagegen"]["model"] == "gpt-image-2-pro"
    assert visual["imagegen"]["state"] == "needs_provider_credentials"


def test_s6_web_capability_status_runs_installer_status_probe(monkeypatch):
    from channel.web import web_channel
    from agent import runtime_capabilities
    from agent.tools.optional_abilities import optional_abilities as optional_module

    class FakeOptionalAbilities:
        def execute(self, _params):
            return types.SimpleNamespace(result={
                "status": "success",
                "abilities": [
                    {
                        "id": "office-pdf",
                        "label": "Office/PDF",
                        "kind": "capability-pack",
                        "packId": "office-pdf",
                        "capabilityState": {"installed": False, "state": "not-installed"},
                    }
                ],
            })

    seen_probes = []

    def fake_status_probe(pack_id):
        seen_probes.append(pack_id)
        return {"installed": False, "state": "not-installed", "source": "installer-status"}

    monkeypatch.setattr(optional_module, "OptionalAbilities", FakeOptionalAbilities)
    monkeypatch.setattr(runtime_capabilities, "_installer_status_state", fake_status_probe)
    monkeypatch.setattr(runtime_capabilities, "_load_runtime_config", lambda: {})
    monkeypatch.setattr(
        runtime_capabilities.RuntimeCapabilityRegistry,
        "tools_payload",
        lambda self: {"status": "success", "source": "runtime-capability-service", "tools": []},
    )
    monkeypatch.setattr(
        runtime_capabilities.RuntimeCapabilityRegistry,
        "skills_payload",
        lambda self: {"status": "success", "source": "runtime-capability-service", "skills": []},
    )
    monkeypatch.setattr(
        runtime_capabilities.RuntimeCapabilityRegistry,
        "extensions_payload",
        lambda self, action_plans=None: {"status": "success", "source": "runtime-capability-service", "extensions": []},
    )

    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: "workspace")
    with patch.object(web_channel, "_require_auth", return_value=None):
        capabilities = json.loads(web_channel.CapabilitiesHandler().GET())
        extensions = json.loads(web_channel.ExtensionsHandler().GET())

    assert capabilities["source"] == "runtime-capability-service"
    assert capabilities["packs"][0]["packId"] == "office-pdf"
    assert capabilities["packs"][0]["state"] == "not-installed"
    assert seen_probes.count("office-pdf") >= 1
    assert "visualWorkflow" in capabilities
    assert extensions["source"] == "runtime-capability-service"


def test_s7_runtime_projection_exposes_sanitized_inline_action_plans():
    from agent.protocol.runtime_projection import RuntimeProjectionService

    permission_events = [
        {
            "event_id": 1,
            "event_seq": 1,
            "request_id": "req-s7",
            "session_id": "session-s7",
            "turn_id": "req-s7",
            "event_type": "run.accepted",
            "payload": {},
            "created_at": 1,
        },
        {
            "event_id": 2,
            "event_seq": 2,
            "request_id": "req-s7",
            "session_id": "session-s7",
            "turn_id": "req-s7",
            "event_type": "permission.requested",
            "payload": {
                "permission_request_id": "perm-s7",
                "tool": "bash",
                "title": "Run workspace command with api key: should-hide",
                "message": "Needs approval but must not leak Authorization: Bearer sk-secret-value",
                "arguments": {"secret": "sk-secret-value", "token": "Bearer hidden"},
            },
            "created_at": 2,
        },
    ]
    blocked_events = permission_events + [
        {
            "event_id": 3,
            "event_seq": 3,
            "request_id": "req-s7",
            "session_id": "session-s7",
            "turn_id": "req-s7",
            "event_type": "capability.policy_blocked",
            "payload": {
                "pack_id": "office-pdf",
                "action": "install",
                "error_type": "capability_policy_blocked",
                "policy_mode": "disabled",
                "install_allowed": False,
            },
            "created_at": 3,
        },
    ]

    permission_projection = RuntimeProjectionService.project_request_events(permission_events, include_events=False)
    permission_serialized = json.dumps(permission_projection, ensure_ascii=False)
    permission_plans = {item["id"]: item for item in permission_projection["action_plans"]}
    assert permission_projection["state"] == "waiting_permission"
    assert permission_projection["events"] == []
    assert permission_plans["permission:perm-s7"]["nextAction"] == "confirm_permission"
    assert permission_plans["permission:perm-s7"]["permissionRequestId"] == "perm-s7"
    assert "sk-secret-value" not in permission_serialized
    assert "Authorization" not in permission_serialized
    assert "Bearer" not in permission_serialized
    assert "api key" not in permission_serialized.lower()
    assert "arguments" not in permission_serialized

    blocked_projection = RuntimeProjectionService.project_request_events(blocked_events, include_events=False)
    blocked_serialized = json.dumps(blocked_projection, ensure_ascii=False)
    blocked_plans = {item["id"]: item for item in blocked_projection["action_plans"]}

    assert blocked_projection["state"] == "blocked"
    assert blocked_projection["events"] == []
    assert "permission:perm-s7" not in blocked_plans
    assert blocked_plans["capability_policy:office-pdf:install"]["nextAction"] == "view_capability_policy"
    assert "sk-secret-value" not in blocked_serialized
    assert "Authorization" not in blocked_serialized
    assert "Bearer" not in blocked_serialized
    assert "arguments" not in blocked_serialized


def test_s7_runtime_projection_drops_permission_actions_after_terminal_state():
    from agent.protocol.runtime_projection import RuntimeProjectionService

    events = [
        {
            "event_id": 1,
            "event_seq": 1,
            "request_id": "req-s7-terminal",
            "session_id": "session-s7",
            "turn_id": "req-s7-terminal",
            "event_type": "run.accepted",
            "payload": {},
            "created_at": 1,
        },
        {
            "event_id": 2,
            "event_seq": 2,
            "request_id": "req-s7-terminal",
            "session_id": "session-s7",
            "turn_id": "req-s7-terminal",
            "event_type": "permission.requested",
            "payload": {
                "permission_request_id": "perm-s7-terminal",
                "tool": "bash",
                "title": "Run command",
                "message": "Awaiting approval",
            },
            "created_at": 2,
        },
        {
            "event_id": 3,
            "event_seq": 3,
            "request_id": "req-s7-terminal",
            "session_id": "session-s7",
            "turn_id": "req-s7-terminal",
            "event_type": "run.completed",
            "payload": {"terminal_reason": "done", "message": "Done"},
            "created_at": 3,
        },
    ]

    projection = RuntimeProjectionService.project_request_events(events, include_events=False)
    serialized = json.dumps(projection, ensure_ascii=False)

    assert projection["state"] == "completed"
    assert projection["terminal_message"] == "Done"
    assert projection["action_plans"] == []
    assert "confirm_permission" not in serialized
    assert "perm-s7-terminal" not in serialized


def test_s7_inline_action_node_smoke_executes_recovery_contract():
    script = ROOT / "scripts" / "smoke-s7-inline-actions.js"
    result = subprocess.run(["node", str(script)], cwd=ROOT, text=True, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["projectionOnlyRenderable"] is True
    assert payload["permissionRowRemovedAfterTerminalSync"] is True


def test_s7_console_single_submit_recovery_and_inline_action_contract():
    console_source = (ROOT / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
    inline_actions_source = (ROOT / "channel" / "web" / "static" / "js" / "inline-actions.js").read_text(encoding="utf-8")
    css_source = (ROOT / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")

    assert "function submitMessage(opts)" in console_source
    assert console_source.count("fetch('/message'") == 1
    assert console_source.count("return submitMessage({") >= 3
    assert "retryLabel: 'sendMessage'" in console_source
    assert "retryLabel: 'sendVoiceMessage'" in console_source
    assert "retryLabel: 'regenerateResponse'" in console_source
    assert "function renderSubmitFailureOnce(loadingEl, payload, fallbackMessage)" in console_source
    assert "dataset.submitFailureRendered" in console_source

    for marker in (
        "function loadActiveRequestsSnapshot()",
        "fetch('/api/active-requests', { cache: 'no-store' })",
        "async function refreshActiveRuntimeRequests(reason, opts)",
        "refreshActiveRuntimeRequests('history_active_requests_recheck'",
        "refreshActiveRuntimeRequests('stream_lost_active_requests'",
        "window_focus_active_requests",
        "visibility_active_requests",
    ):
        assert marker in console_source

    for marker in (
        "function normalizeInlineActionPlan(input, opts)",
        "function renderInlineActionRowHtml(rawPlan)",
        "function inlineActionPlansFromProjection(projection)",
        "function inlineActionPlansFromSubmitError(payload)",
        "function syncInlineActionRows(plans, container)",
        "window.EcoreXInlineActions",
    ):
        assert marker in console_source

    for marker in (
        "root.EcoreXInlineActions = {",
        "function normalizeInlineActionPlan(input, opts)",
        "function renderInlineActionRowHtml(rawPlan, opts)",
        "function inlineActionPlansFromProjection(projection, opts)",
        "function inlineActionPlansFromSubmitError(payload, opts)",
        "data-inline-action-row=\"1\"",
        "data-inline-action-command=\"open-models\"",
        "data-inline-action-command=\"open-channels\"",
        "data-inline-action-command=\"view-capability-policy\"",
        "nextAction: 'view_capability_policy'",
    ):
        assert marker in inline_actions_source

    for marker in (
        "item.type === 'tool_permission_request'",
        "action_plans: inlineActionPlansFromProjection(projection)",
        "syncInlineActionRows(actionPlans, stepsEl);",
        "syncInlineActionRows(inlineActionPlansFromProjection(projection), stepsEl);",
        "loadingEl.querySelector('.agent-current-phase')?.remove();",
        "fetch('/api/tool-permissions'",
    ):
        assert marker in console_source

    assert "nextAction: 'open_permissions'" not in console_source
    assert "nextAction: 'open_permissions'" not in inline_actions_source
    assert ".inline-action-row" in css_source
    assert ".inline-action-btn.is-primary" in css_source


def test_s8_web_routes_are_declarative_and_external_to_startup():
    from channel.web import web_channel
    from channel.web.routes import WEB_ROUTES, route_pairs

    web_source = (ROOT / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
    routes_source = (ROOT / "channel" / "web" / "routes.py").read_text(encoding="utf-8")
    chat_source = (ROOT / "channel" / "web" / "chat.html").read_text(encoding="utf-8")
    console_source = (ROOT / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
    inline_actions_source = (ROOT / "channel" / "web" / "static" / "js" / "inline-actions.js").read_text(encoding="utf-8")

    assert len(WEB_ROUTES) % 2 == 0
    pairs = dict(route_pairs())
    for path, handler in {
        "/message": "MessageHandler",
        "/stream": "StreamHandler",
        "/api/runtime-projection": "RuntimeProjectionHandler",
        "/api/active-requests": "ActiveRequestsHandler",
        "/api/image-jobs": "ImageJobsHandler",
        "/api/capabilities": "CapabilitiesHandler",
        "/api/diagnostics/bundle": "DiagnosticsBundleHandler",
        "/api/logs": "LogsHandler",
    }.items():
        assert pairs[path] == handler

    startup_block = web_source.split("def startup(self):", 1)[1].split("    def stop(self):", 1)[0]
    assert "web.application(WEB_ROUTES, globals(), autoreload=False)" in startup_block
    assert "urls = (" not in startup_block
    assert "WEB_ROUTES = (" in routes_source
    assert "class CapabilitiesHandler" not in web_source
    assert "class ToolsHandler" not in web_source
    assert "class LogsHandler" not in web_source
    assert "class RuntimeProjectionHandler" not in web_source
    assert "class ActiveRequestsHandler" not in web_source
    assert "class FileServeHandler" not in web_source
    assert "class FileStatHandler" not in web_source
    assert "class FileJsonHandler" not in web_source
    assert "class ImageJobsHandler" not in web_source
    assert "class StreamHandler" not in web_source
    assert "class SessionsHandler" not in web_source
    assert "class SessionTitleHandler" not in web_source
    assert "class HistoryHandler" not in web_source
    assert "class AuthLoginHandler" not in web_source
    assert "class AuthCheckHandler" not in web_source
    assert web_channel.CapabilitiesHandler.__module__ == "channel.web.capabilities"
    assert web_channel.ExtensionsHandler.__module__ == "channel.web.capabilities"
    assert web_channel.ToolsHandler.__module__ == "channel.web.capabilities"
    assert web_channel.SkillsHandler.__module__ == "channel.web.capabilities"
    assert web_channel.DiagnosticsBundleHandler.__module__ == "channel.web.diagnostics"
    assert web_channel.LogsHandler.__module__ == "channel.web.diagnostics"
    assert web_channel.RuntimeProjectionHandler.__module__ == "channel.web.projection"
    assert web_channel.ActiveRequestsHandler.__module__ == "channel.web.projection"
    assert web_channel.RequestRetryPrepareHandler.__module__ == "channel.web.projection"
    assert web_channel.FileServeHandler.__module__ == "channel.web.files"
    assert web_channel.FileStatHandler.__module__ == "channel.web.files"
    assert web_channel.FileJsonHandler.__module__ == "channel.web.files"
    assert web_channel.ImageJobsHandler.__module__ == "channel.web.image_jobs"
    assert web_channel.ImageJobActionHandler.__module__ == "channel.web.image_jobs"
    assert web_channel.StreamHandler.__module__ == "channel.web.sse"
    assert web_channel.SessionsHandler.__module__ == "channel.web.sessions"
    assert web_channel.SessionDetailHandler.__module__ == "channel.web.sessions"
    assert web_channel.SessionTitleHandler.__module__ == "channel.web.sessions"
    assert web_channel.SessionClearContextHandler.__module__ == "channel.web.sessions"
    assert web_channel.HistoryHandler.__module__ == "channel.web.sessions"
    assert web_channel.MessageDeleteHandler.__module__ == "channel.web.sessions"
    assert web_channel.UiStateHandler.__module__ == "channel.web.sessions"
    assert web_channel.AuthCheckHandler.__module__ == "channel.web.auth"
    assert web_channel.AuthLoginHandler.__module__ == "channel.web.auth"
    assert web_channel.AuthLogoutHandler.__module__ == "channel.web.auth"
    assert 'src="assets/js/inline-actions.js"' in chat_source
    assert chat_source.index('src="assets/js/inline-actions.js"') < chat_source.index('src="assets/js/console.js"')
    assert "root.EcoreXInlineActions = {" in inline_actions_source
    assert console_source.count("function normalizeInlineActionPlan(input, opts)") == 1
    assert "function inlineActionTone(plan) {\n    const kind =" not in console_source


def test_s4b_web_app_chat_model_switcher_contract():
    app_source = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
    css_source = (ROOT / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")

    assert "loadChatModelOptions" in api_source
    assert "setChatModel" in api_source
    assert "ChatContextPolicy" in api_source
    assert "ChatContextContinuity" in api_source
    assert "normalizeChatContextContinuity" in api_source
    assert "contextPolicy" in api_source
    assert "contextContinuity" in api_source
    assert 'capability: "chat"' in api_source
    assert 'path: "/message"' in api_source
    assert "model-switch-divider" in app_source
    assert "function chatModelProviderDisplayLabel" in app_source
    assert 'option.provider === "custom" && option.modelAliasFamily === "gemini"' in app_source
    assert "已切换 ${label} 模型" in app_source
    assert "chooseChatModel" in app_source
    assert "chat-model-popover" in app_source
    assert "ProviderModelIcon" in app_source
    assert "provider-model-icon" in app_source
    assert "EFFECTIVE_MODEL_ALIAS_PREFIXES" not in app_source
    assert "is-unavailable" in app_source
    assert "option.configured === false" in app_source
    assert "contextPolicyLimit" in app_source
    assert "DEFAULT_CONTEXT_THRESHOLD_TOKENS" in app_source
    assert ".chat-model-popover" in css_source
    assert ".provider-model-icon" in css_source
    assert ".chat-model-popover button.is-unavailable" in css_source
    assert "grid-template-columns: 22px minmax(0, 1fr) auto" in css_source
    assert ".model-switch-divider" in css_source
    assert ".message.model-switch-message" in css_source
    assert ".message.system.is-model-switch" not in css_source
    render_start = app_source.index("visibleMessages.map((message) =>")
    divider_end = app_source.index("const messageSessionId = activeSessionId;", render_start)
    assert 'className="message system model-switch-message"' in app_source[render_start:divider_end]
    assert 'className="model-switch-divider"' in app_source[render_start:divider_end]
    assert "message-copy-button" not in app_source[render_start:divider_end]


def test_v027_context_estimate_bounds_large_local_file_history():
    app_source = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "TOKEN_ESTIMATE_MAX_FILES_PER_MESSAGE = 8" in app_source
    assert "TOKEN_ESTIMATE_MAX_FILE_KEY_CHARS = 240" in app_source
    assert "TOKEN_ESTIMATE_MAX_STEPS_PER_MESSAGE = 16" in app_source
    assert "TOKEN_ESTIMATE_MAX_TOOL_CALLS_PER_MESSAGE = 12" in app_source
    assert "files.slice(0, TOKEN_ESTIMATE_MAX_FILES_PER_MESSAGE)" in app_source
    assert "(message.steps || []).slice(0, TOKEN_ESTIMATE_MAX_STEPS_PER_MESSAGE)" in app_source
    assert "(message.toolCalls || []).slice(0, TOKEN_ESTIMATE_MAX_TOOL_CALLS_PER_MESSAGE)" in app_source
    assert "omittedFiles * TOKEN_ESTIMATE_OMITTED_FILE_COST" in app_source
    assert "omittedSteps * TOKEN_ESTIMATE_OMITTED_STEP_COST" in app_source
    assert "omittedToolCalls * TOKEN_ESTIMATE_OMITTED_TOOL_COST" in app_source
    assert "setHistoryContextUsed(estimateContextTokens(messagesRef.current, \"\", []));" in app_source
    assert "const historyContextUsed = useMemo(() => estimateContextTokens(messages, \"\", []), [messages]);" not in app_source


def test_v027_public_log_redaction_masks_paths_and_api_base():
    from common.ecorex_public_payload import mask_sensitive_text

    text = r"api_base=https://custom-gemini.example/v1 path=C:\EcoreX-Agent生产版\private\out.png"
    masked = mask_sensitive_text(text, max_chars=500)

    assert "https://custom-gemini.example/v1" not in masked
    assert r"C:\EcoreX-Agent生产版\private\out.png" not in masked
    assert "api_base=***" in masked
    assert r"C:\[redacted-path]" in masked

    diagnostics_source = (ROOT / "channel" / "web" / "diagnostics.py").read_text(encoding="utf-8")
    web_source = (ROOT / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
    agent_bridge_source = (ROOT / "bridge" / "agent_bridge.py").read_text(encoding="utf-8")

    assert "mask_sensitive_text(line" in diagnostics_source
    assert "mask_sensitive_text(_mask(line)" in diagnostics_source
    assert "safe_tail" in web_source
    assert "mask_sensitive_text(line, max_chars=2000)" in web_source
    assert "Sending file: {file_info.get('path')}" not in agent_bridge_source


def test_s4b_web_app_keeps_image_generation_out_of_chat_switcher():
    app_source = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

    set_chat_model_block = api_source.split("export async function setChatModel", 1)[1].split("export async function sendChatMessage", 1)[0]
    assert "image" not in set_chat_model_block
    assert "gpt-image-2-pro" not in app_source


def test_s4b_gpt55_connectivity_smoke_requires_admin_managed_openai_key(monkeypatch):
    script_path = ROOT / "scripts" / "smoke-chat-model-connectivity.py"
    spec = importlib.util.spec_from_file_location("smoke_chat_model_connectivity", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    local_key_text = "sk-proj-localfileopenai1234567890 openai"

    monkeypatch.setattr(module, "_enterprise_model_settings", lambda: {
        "open_ai_api_key": "admin-openai-key",
        "open_ai_api_base": "https://admin-openai.test/v1",
        "model": "gpt-5.5",
        "bot_type": "openai",
    })
    keys = module._config_keys({}, local_key_text)
    assert keys["openai_key"] == "admin-openai-key"
    assert keys["openai_base"] == "https://admin-openai.test/v1"
    assert keys["openai_credential_source"] == "admin_policy_cache"

    monkeypatch.setattr(module, "_enterprise_model_settings", lambda: {})
    keys = module._config_keys({}, local_key_text)
    assert keys["openai_key"] == ""
    assert keys["openai_credential_source"] == "missing_admin_policy_or_runtime_config"

    keys = module._config_keys({
        "open_ai_api_key": "runtime-openai-key",
        "open_ai_api_base": "https://runtime-openai.test/v1",
    }, local_key_text)
    assert keys["openai_key"] == "runtime-openai-key"
    assert keys["openai_base"] == "https://runtime-openai.test/v1"
    assert keys["openai_credential_source"] == "runtime_config"


def test_s5_capability_authorization_low_risk_matrix(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    user_data = tmp_path / "user-data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "note.md"
    outside = tmp_path / "outside.md"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    monkeypatch.setenv("ECOREX_USER_DATA", str(user_data))
    monkeypatch.delenv("ECOREX_DESKTOP_USER_DATA", raising=False)

    broker = ToolPermissionBroker()

    low_risk_actions = [
        ("optional_abilities", "status", {}),
        ("agent_capability", "diagnose", {}),
        ("scheduler", "list", {}),
        ("image_jobs", "status", {}),
        ("browser", "snapshot", {}),
        ("workspace", "read", {"resource": str(inside), "cwd": str(workspace)}),
        ("artifact", "read", {"resource": str(inside), "cwd": str(workspace)}),
    ]
    for mode in ("smart-ask", "read-only", "full-access"):
        broker.set_mode(mode)
        for capability, action, kwargs in low_risk_actions:
            decision = broker.authorize_capability(capability, action, **kwargs)
            assert decision["allowed"] is True, (mode, capability, action, decision)

    broker.set_mode("smart-ask")
    assert broker.authorize_capability("workspace", "read", resource=str(outside), cwd=str(workspace))["allowed"] is False
    assert broker.authorize_capability("bash", "workspace_write", resource=str(inside), cwd=str(workspace))["allowed"] is True
    assert broker.authorize_capability("bash", "system_shell", arguments={"command": "whoami"})["allowed"] is False
    assert broker.authorize_capability(
        "bash",
        "workspace_read",
        resource=str(inside),
        arguments={"command": "whoami"},
        cwd=str(workspace),
    )["allowed"] is False
    assert broker.authorize_capability("image_jobs", "start", arguments={"action": "start"})["allowed"] is False
    assert broker.authorize_capability(
        "image_jobs",
        "start",
        arguments={"action": "start"},
        metadata={"user_initiated": True},
    )["allowed"] is True

    broker.set_mode("read-only")
    settings_path = user_data / "permissions.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.setdefault("alwaysAllow", {})["tool-execution:bash"] = True
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    assert broker.authorize_capability("bash", "workspace_write", resource=str(inside), cwd=str(workspace))["allowed"] is False
    assert broker.authorize_capability("bash", "system_shell", arguments={"command": "whoami"})["allowed"] is False
    assert broker.authorize_capability("scheduler", "create", arguments={"action": "create"})["allowed"] is False

    broker.set_mode("full-access")
    assert broker.authorize_capability("scheduler", "list")["allowed"] is True
    assert broker.authorize_capability("image_jobs", "status")["allowed"] is True
    assert broker.authorize_capability("bash", "system_shell", arguments={"command": "whoami"})["allowed"] is True

    audit = (user_data / "permission-audit.jsonl").read_text(encoding="utf-8")
    assert "capability-authorization" in audit
    assert "default-low-risk-scheduler-read" in audit


def test_s5_low_risk_web_fetch_allowed_in_read_only(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    broker.set_mode("read-only")

    direct = broker.authorize_noninteractive("web_fetch", {"url": "https://example.com/image.png"})
    interactive = broker.authorize(
        "web_fetch",
        "tool-web-fetch",
        {"url": "https://example.com/image.png"},
        timeout_seconds=1,
    )

    assert direct["allowed"] is True
    assert direct["reason"] == "default-low-risk-web-fetch"
    assert interactive["allowed"] is True


def test_s5_web_runtime_forces_browser_cdp_defaults(monkeypatch):
    import config

    monkeypatch.setenv("CHANNEL_TYPE", "web")
    monkeypatch.delenv("ECOREX_BROWSER_CDP_AUTO_LAUNCH", raising=False)
    payload = {
        "channel_type": "web",
        "tools": {
            "browser": {
                "cdp_auto_launch": False,
                "cdp_fallback": False,
                "persistent": False,
            }
        },
    }

    config._ensure_ecorex_runtime_defaults(payload)

    browser = payload["tools"]["browser"]
    assert browser["cdp_auto_launch"] is True
    assert browser["cdp_fallback"] is True
    assert browser["persistent"] is True


def test_s5_web_runtime_ignores_desktop_enterprise_policy_cache(monkeypatch, tmp_path):
    import config

    desktop_cache = tmp_path / "ecorex-desktop" / "enterprise-model-policy.json"
    desktop_cache.parent.mkdir(parents=True)
    desktop_cache.write_text(json.dumps({
        "configured": True,
        "settings": {
            "model": "deepseek-v4-pro",
            "bot_type": "deepseek",
        },
    }), encoding="utf-8")
    cfg = {"channel_type": "web", "model": "gpt-5.5", "bot_type": "openai"}

    monkeypatch.delenv("ECOREX_ENTERPRISE_MODEL_POLICY_FILE", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert config._apply_cached_enterprise_model_policy(cfg) is False
    assert cfg["model"] == "gpt-5.5"
    assert cfg["bot_type"] == "openai"


def test_s5_browser_cdp_discovers_playwright_chromium(monkeypatch, tmp_path):
    from agent.tools.browser import browser_automation_service as service

    browser_root = tmp_path / "playwright-browsers"
    chrome = _touch_executable(browser_root / "chromium-1223" / "chrome-linux" / "chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    monkeypatch.setattr(service.sys, "platform", "linux")

    assert service.find_playwright_chromium_executable() == str(chrome)
    assert service.find_chrome_executable({}) == str(chrome)
    diagnostics = service.browser_automation_diagnostics({})
    assert diagnostics["chromeExecutable"] == str(chrome)
    assert diagnostics["chromeExecutableSource"] == "playwright-chromium"


def test_v027_browser_cdp_discovers_playwright_chromium_new_cft_layout(monkeypatch, tmp_path):
    from agent.tools.browser import browser_automation_service as service

    browser_root = tmp_path / "playwright-browsers"
    chrome = _touch_executable(browser_root / "chromium-1228" / "chrome-win64" / "chrome.exe")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.os.path, "exists", lambda _path: False)

    assert service.find_playwright_chromium_executable() == str(chrome)
    assert service.find_chrome_executable({}) == str(chrome)


def test_s5_web_browser_cdp_uses_only_managed_playwright_runtime(monkeypatch, tmp_path):
    from agent.tools.browser import browser_automation_service as service

    user_cache = tmp_path / "user-cache"
    _touch_executable(user_cache / "chromium-1223" / "chrome-linux" / "chrome")
    state = tmp_path / "state"
    managed = _touch_executable(state / "playwright-browsers" / "chromium-1224" / "chrome-linux" / "chrome")
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("ECOREX_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(user_cache.parent))

    assert service.find_playwright_chromium_executable() == str(managed)


def test_s5_browser_read_only_snapshot_does_not_launch_process(monkeypatch, tmp_path):
    from agent.tools.browser import browser_tool
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    broker.set_mode("read-only")
    browser_tool.BrowserTool._shared_service = None
    monkeypatch.setattr(browser_tool, "cdp_is_reachable", lambda endpoint: False)

    result = browser_tool.BrowserTool({"cdp_endpoint": "http://127.0.0.1:9"}).execute({"action": "snapshot"})

    assert result.status == "error"
    assert "read-only mode" in result.result
    assert browser_tool.BrowserTool._shared_service is None


def test_s5_browser_read_only_existing_cdp_disables_autolaunch_and_fallback(monkeypatch, tmp_path):
    from agent.tools.browser import browser_tool
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    broker.set_mode("read-only")
    browser_tool.BrowserTool._shared_service = None
    monkeypatch.setattr(browser_tool, "cdp_is_reachable", lambda endpoint: True)
    captured = {}

    class FakeService:
        def __init__(self, config):
            captured.update(config)
            self._alive = False
            self._thread = None

        def snapshot(self, selector=None):
            return "existing page"

    monkeypatch.setattr(browser_tool, "BrowserService", FakeService)

    result = browser_tool.BrowserTool({"cdp_endpoint": "http://127.0.0.1:9222"}).execute({"action": "snapshot"})

    assert result.status == "success"
    assert result.result == "existing page"
    assert captured["cdp_auto_launch"] is False
    assert captured["cdp_fallback"] is False
    assert browser_tool.BrowserTool._shared_service is None


def test_s5_web_fetch_image_url_saves_asset(monkeypatch, tmp_path):
    from agent.tools.web_fetch.web_fetch import WebFetch

    class Broker:
        def authorize_noninteractive(self, tool_name, arguments=None):
            return {"allowed": True, "reason": "test"}

        def authorize_file_access(self, operation, path, cwd=None):
            return {"allowed": True, "reason": "test"}

    class Response:
        headers = {"Content-Type": "image/png"}
        content = b"\x89PNG\r\n\x1a\n"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("common.ecorex_tool_permissions.get_tool_permission_broker", lambda: Broker())
    monkeypatch.setattr("agent.tools.web_fetch.web_fetch.requests.get", lambda *args, **kwargs: Response())

    result = WebFetch({"cwd": str(tmp_path)}).execute({"url": "https://example.com/image.png"})

    assert result.status == "success"
    assert "Saved to:" in result.result
    saved_line = next(line for line in result.result.splitlines() if "Saved to:" in line)
    saved_path = Path(saved_line.split("Saved to:", 1)[1].split("]", 1)[0].strip())
    assert saved_path.read_bytes() == Response.content


def test_s5_web_fetch_redacts_signed_url_in_errors(monkeypatch, tmp_path):
    from agent.tools.web_fetch.web_fetch import WebFetch

    class Broker:
        def authorize_noninteractive(self, tool_name, arguments=None):
            return {"allowed": True, "reason": "test"}

    class Response:
        headers = {"Content-Type": "text/html"}

        def raise_for_status(self):
            from requests import HTTPError

            error = HTTPError("forbidden")
            error.response = type("HttpResponse", (), {"status_code": 403})()
            raise error

    monkeypatch.setattr("common.ecorex_tool_permissions.get_tool_permission_broker", lambda: Broker())
    monkeypatch.setattr("agent.tools.web_fetch.web_fetch.requests.get", lambda *args, **kwargs: Response())

    url = "https://example.com/private/image.png?token=secret-token&signature=sig#frag"
    result = WebFetch({"cwd": str(tmp_path)}).execute({"url": url})

    assert result.status == "error"
    assert "https://example.com/private/image.png" in result.result
    assert "secret-token" not in result.result
    assert "signature" not in result.result


def test_s5_feishu_cli_run_read_write_admin_boundaries(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    broker.set_mode("smart-ask")

    read_decision = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["base", "+record-list", "--as", "user"]},
    )
    write_decision = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["im", "+message-send", "--chat-id", "oc_x"]},
    )
    admin_decision = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["drive", "+permission-member-create"]},
    )

    assert read_decision["allowed"] is True
    assert read_decision["classification"] == "read"
    assert write_decision["allowed"] is False
    assert admin_decision["allowed"] is False

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    export_without_output = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["drive", "+file-export"]},
        cwd=str(workspace),
    )
    export_inside_workspace = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["drive", "+file-export", "--output", "doc.md"]},
        cwd=str(workspace),
    )
    export_outside_workspace = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["drive", "+file-export", "--output", str(tmp_path / "outside.md")]},
        cwd=str(workspace),
    )

    assert export_without_output["allowed"] is False
    assert export_inside_workspace["allowed"] is True
    assert export_outside_workspace["allowed"] is False

    broker.set_mode("read-only")
    readonly_write = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["base", "+record-delete"]},
    )
    assert readonly_write["allowed"] is False
    assert readonly_write["classification"] == "write"

    broker.set_mode("full-access")
    full_write = broker.authorize_capability(
        "feishu_cli",
        "run",
        arguments={"action": "run", "args": ["base", "+record-delete"]},
    )
    assert full_write["allowed"] is True
    assert full_write["classification"] == "write"


def test_s5_feishu_cli_structured_config_actions_respect_permission_modes(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    structured_actions = ["install", "config_init", "auth_login", "agent_auth", "authorize_agent"]

    broker.set_mode("read-only")
    for action in structured_actions:
        decision = broker.authorize_capability("feishu_cli", action, arguments={"action": action})
        legacy_decision = broker.authorize_noninteractive("feishu_cli", {"action": action})
        assert decision["allowed"] is False, (action, decision)
        assert decision["classification"] == "configure"
        assert legacy_decision["allowed"] is False, (action, legacy_decision)

    broker.set_mode("smart-ask")
    for action in structured_actions:
        decision = broker.authorize_capability("feishu_cli", action, arguments={"action": action})
        legacy_decision = broker.authorize_noninteractive("feishu_cli", {"action": action})
        assert decision["allowed"] is False, (action, decision)
        assert legacy_decision["allowed"] is False, (action, legacy_decision)

    broker.set_mode("full-access")
    for action in structured_actions:
        decision = broker.authorize_capability("feishu_cli", action, arguments={"action": action})
        assert decision["allowed"] is True, (action, decision)
        assert decision["classification"] == "configure"


def test_s5_web_api_status_paths_use_capability_broker(monkeypatch, tmp_path):
    from channel.web import web_channel
    from common.ecorex_tool_permissions import get_tool_permission_broker

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    get_tool_permission_broker().set_mode("read-only")

    class FakeImageJobService:
        def status(self, job_id):
            return {"job_id": job_id, "status": "running", "artifacts": []}

        def collect(self, job_id, wait=False, timeout=None):
            return {"job_id": job_id, "status": "completed", "artifacts": []}

        def cancel(self, job_id, reason="cancel_requested"):
            return {"job_id": job_id, "status": "cancelled", "reason": reason, "artifacts": []}

    with patch.object(web_channel, "_require_auth", return_value=None), \
            patch.object(web_channel, "_get_workspace_root", return_value=str(workspace)), \
            patch.object(web_channel.SchedulerHandler, "_projection", return_value={"tasks": [], "canModify": False}), \
            patch("agent.protocol.get_image_job_service", return_value=FakeImageJobService()):
        scheduler_payload = json.loads(web_channel.SchedulerHandler().GET())
        with patch.object(
            web_channel.web,
            "input",
            return_value=types.SimpleNamespace(
                job_id="image-job-s5",
                request_id="",
                requestId="",
                wait="",
                timeout="",
                include_events="",
            ),
        ):
            image_payload = json.loads(web_channel.ImageJobsHandler().GET())

        with patch.object(web_channel.web, "data", return_value=json.dumps({"action": "start"}).encode("utf-8")):
            scheduler_start = json.loads(web_channel.SchedulerHandler().POST())

        with patch.object(
            web_channel.web,
            "data",
            return_value=json.dumps({
                "action": "start",
                "request_id": "req-s5",
                "prompt": "draw a small test image",
            }).encode("utf-8"),
        ):
            image_start = json.loads(web_channel.ImageJobsHandler().POST())
        with patch.object(web_channel.web, "data", return_value=json.dumps({"action": "cancel"}).encode("utf-8")):
            image_cancel = json.loads(web_channel.ImageJobActionHandler().POST("image-job-s5"))

    assert scheduler_payload["status"] == "success"
    assert image_payload["status"] == "success"
    assert image_payload["job"]["status"] == "running"
    assert scheduler_start["status"] == "error"
    assert scheduler_start["code"] == "permission_denied"
    assert scheduler_start["permission"]["capability"] == "scheduler"
    assert scheduler_start["permission"]["action"] == "start"
    assert scheduler_start["permission"]["mode"] == "read-only"
    assert image_start["status"] == "error"
    assert image_start["code"] == "permission_denied"
    assert image_start["permission"]["capability"] == "image_jobs"
    assert image_start["permission"]["action"] == "start"
    assert image_cancel["status"] == "success"


def test_s5_web_status_paths_deny_when_capability_broker_denies(monkeypatch, tmp_path):
    from channel.web import web_channel

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class DenyCapabilityBroker:
        def get_state(self):
            return {"mode": "smart-ask"}

        def authorize_capability(self, capability, action="", **_kwargs):
            return {"allowed": False, "reason": f"deny-{capability}-{action}"}

    class FakeImageJobService:
        def status(self, job_id):
            raise AssertionError("image status must not run after broker denial")

        def collect(self, job_id, wait=False, timeout=None):
            raise AssertionError("image collect must not run after broker denial")

    with patch.object(web_channel, "_require_auth", return_value=None), \
            patch.object(web_channel, "_get_workspace_root", return_value=str(workspace)), \
            patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=DenyCapabilityBroker()), \
            patch.object(web_channel.SchedulerHandler, "_projection", side_effect=AssertionError("scheduler projection must not run after broker denial")), \
            patch("agent.protocol.get_image_job_service", return_value=FakeImageJobService()):
        scheduler_payload = json.loads(web_channel.SchedulerHandler().GET())
        with patch.object(
            web_channel.web,
            "input",
            return_value=types.SimpleNamespace(
                job_id="image-job-s5-denied",
                request_id="",
                requestId="",
                wait="",
                timeout="",
                include_events="",
            ),
        ):
            image_status_payload = json.loads(web_channel.ImageJobsHandler().GET())
        with patch.object(
            web_channel.web,
            "input",
            return_value=types.SimpleNamespace(
                job_id="image-job-s5-denied",
                request_id="",
                requestId="",
                wait="true",
                timeout="",
                include_events="",
            ),
        ):
            image_collect_payload = json.loads(web_channel.ImageJobsHandler().GET())

    assert scheduler_payload["status"] == "error"
    assert scheduler_payload["code"] == "permission_denied"
    assert scheduler_payload["permission"]["capability"] == "scheduler"
    assert scheduler_payload["permission"]["action"] == "list"
    assert image_status_payload["status"] == "error"
    assert image_status_payload["code"] == "permission_denied"
    assert image_status_payload["permission"]["capability"] == "image_jobs"
    assert image_status_payload["permission"]["action"] == "status"
    assert image_collect_payload["status"] == "error"
    assert image_collect_payload["code"] == "permission_denied"
    assert image_collect_payload["permission"]["capability"] == "image_jobs"
    assert image_collect_payload["permission"]["action"] == "collect"


def test_s5_web_external_connection_feishu_auth_uses_capability_broker(monkeypatch, tmp_path):
    from channel.web import web_channel
    from common.ecorex_tool_permissions import get_tool_permission_broker
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    get_tool_permission_broker().set_mode("read-only")

    with patch.object(web_channel, "_require_auth", return_value=None), \
            patch.object(web_channel, "_get_workspace_root", return_value=str(workspace)), \
            patch.object(web_channel.web, "data", return_value=json.dumps({"action": "agent_auth"}).encode("utf-8")), \
            patch.object(FeishuCli, "execute", side_effect=AssertionError("FeishuCli.execute must not bypass broker")):
        payload = json.loads(web_channel.ExternalConnectionActionHandler().POST("feishu"))

    assert payload["status"] == "error"
    assert payload["code"] == "permission_denied"
    assert payload["permission"]["capability"] == "feishu_cli"
    assert payload["permission"]["action"] == "agent_auth"
    assert payload["permission"]["mode"] == "read-only"


def test_s5_permission_chain_uses_capability_broker():
    broker_source = (ROOT / "common" / "ecorex_tool_permissions.py").read_text(encoding="utf-8")
    agent_stream_source = (ROOT / "agent" / "protocol" / "agent_stream.py").read_text(encoding="utf-8")
    scheduler_source = (ROOT / "agent" / "tools" / "scheduler" / "integration.py").read_text(encoding="utf-8")
    web_source = (ROOT / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    assert "def authorize_capability" in broker_source
    assert "capability-authorization" in broker_source
    assert "_classify_feishu_cli_run" in broker_source
    assert '"image_jobs"' in broker_source
    assert "authorize_capability" in agent_stream_source
    assert "authorize_capability" in scheduler_source
    assert "_authorize_web_capability" in web_source
    assert "authorize_noninteractive(capability" not in web_source
    assert "default-low-risk-image-job-status" in broker_source


def test_s5_legacy_broker_fallbacks_do_not_treat_mocks_as_allow(monkeypatch):
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.scheduler import integration as scheduler_integration
    from unittest.mock import MagicMock
    from channel.web import web_channel

    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[],
    )
    executable_executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[
            types.SimpleNamespace(
                name="bash",
                description="run shell",
                params={"type": "object", "properties": {}},
            )
        ],
    )

    with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=MagicMock()):
        agent_decision = executor._authorize_tool_execution("bash", "tool-s5", {"command": "whoami"})
        scheduler_allowed = scheduler_integration._authorize_scheduled_execution({
            "id": "task-s5",
            "name": "mocked",
            "action": {"type": "agent_task"},
        })
        with patch.object(web_channel, "_get_workspace_root", return_value="workspace"):
            web_decision = web_channel._authorize_web_capability(
                "scheduler",
                "start",
                arguments={"action": "start"},
            )

    class EmptyDecisionBroker:
        def authorize_capability(self, *args, **kwargs):
            return {}

        def authorize(self, *args, **kwargs):
            return {}

        def authorize_noninteractive(self, *args, **kwargs):
            return {}

    with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=EmptyDecisionBroker()):
        empty_agent_decision = executor._authorize_tool_execution("bash", "tool-s5-empty", {"command": "whoami"})
        empty_scheduler_allowed = scheduler_integration._authorize_scheduled_execution({
            "id": "task-s5-empty",
            "name": "empty",
            "action": {"type": "agent_task"},
        })
        with patch.object(web_channel, "_get_workspace_root", return_value="workspace"):
            empty_web_decision = web_channel._authorize_web_capability(
                "scheduler",
                "start",
                arguments={"action": "start"},
            )

    class HybridMalformedCapabilityBroker:
        def authorize_capability(self, *args, **kwargs):
            return "not-a-decision"

        def authorize(self, *args, **kwargs):
            return {"allowed": True, "reason": "legacy-allow-should-not-run"}

        def authorize_noninteractive(self, *args, **kwargs):
            return {"allowed": True, "reason": "legacy-allow-should-not-run"}

    with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=HybridMalformedCapabilityBroker()):
        hybrid_agent_decision = executor._authorize_tool_execution("bash", "tool-s5-hybrid", {"command": "whoami"})
        hybrid_scheduler_allowed = scheduler_integration._authorize_scheduled_execution({
            "id": "task-s5-hybrid",
            "name": "hybrid",
            "action": {"type": "agent_task"},
        })
        hybrid_scheduler_tool_allowed = scheduler_integration._authorize_scheduled_tool_call(
            types.SimpleNamespace(server_name=""),
            "bash",
            {"command": "whoami"},
            {"id": "task-s5-hybrid-tool", "name": "hybrid-tool"},
        )

    with patch.object(executable_executor, "_authorize_tool_execution", return_value={}):
        malformed_execute_result = executable_executor._execute_tool({
            "id": "tool-s5-malformed",
            "name": "bash",
            "arguments": {"command": "whoami"},
        })

    class LegacySchedulerBroker:
        def authorize_noninteractive(self, tool_name, arguments=None):
            return {"allowed": True, "reason": "legacy-test"}

    with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=LegacySchedulerBroker()):
        legacy_scheduler_allowed = scheduler_integration._authorize_scheduled_execution({
            "id": "task-s5-legacy",
            "name": "legacy",
            "action": {"type": "agent_task"},
        })

    assert agent_decision["allowed"] is False
    assert scheduler_allowed is False
    assert web_decision["allowed"] is False
    assert empty_agent_decision["allowed"] is False
    assert empty_scheduler_allowed is False
    assert empty_web_decision["allowed"] is False
    assert hybrid_agent_decision["allowed"] is False
    assert hybrid_scheduler_allowed is False
    assert hybrid_scheduler_tool_allowed is False
    assert malformed_execute_result["status"] == "error"
    assert "Permission blocked" in malformed_execute_result["result"]
    assert legacy_scheduler_allowed is True


def test_s5_web_file_access_malformed_broker_decisions_fail_closed(monkeypatch, tmp_path):
    from channel.web import web_channel
    from unittest.mock import MagicMock

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample = workspace / "safe.json"
    sample.write_text('{"ok": true}', encoding="utf-8")
    new_project = workspace / "new-project"

    class MalformedFileBroker:
        def __init__(self, decision):
            self.decision = decision

        def authorize_file_access(self, *_args, **_kwargs):
            return self.decision

        def get_state(self):
            return {"mode": "smart-ask"}

        def list_workspace_roots(self, cwd=None):
            return [str(workspace)]

        def remember_workspace_root(self, *_args, **_kwargs):
            return {"status": "success"}

    for malformed_decision in ({}, MagicMock()):
        broker = MalformedFileBroker(malformed_decision)
        with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=broker), \
                patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "_get_workspace_root", return_value=str(workspace)), \
                patch.object(web_channel, "_get_upload_dir", return_value=str(workspace / "uploads")):
            web_channel_instance = web_channel.WebChannel()
            assert web_channel_instance._artifact_path_available(str(sample)) is False

            with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(path=str(sample))):
                try:
                    web_channel.FileServeHandler().GET()
                    served = True
                except Exception:
                    served = False
            assert served is False

            with patch.object(web_channel.web, "data", return_value=json.dumps({"path": str(sample)}).encode("utf-8")):
                stat_payload = json.loads(web_channel.FileStatHandler().POST())
                json_payload = json.loads(web_channel.FileJsonHandler().POST())
            assert stat_payload["status"] == "denied"
            assert json_payload["status"] == "denied"

            with patch.object(web_channel.web, "data", return_value=json.dumps({"path": str(sample), "action": "open"}).encode("utf-8")), \
                    patch.object(web_channel.OpenPathHandler, "_open_path") as open_path:
                open_payload = json.loads(web_channel.OpenPathHandler().POST())
            assert open_payload["status"] == "error"
            open_path.assert_not_called()

            with pytest.raises(Exception):
                web_channel._project_payload_from_path(str(new_project), create=True, user_selected=False)
            assert not new_project.exists()
