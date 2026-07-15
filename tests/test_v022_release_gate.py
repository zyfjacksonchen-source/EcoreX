import argparse
import contextlib
import hashlib
import io
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_gate(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-v022-release-gate.py"), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=check,
    )


def _load_gate_module():
    script = ROOT / "scripts" / "check-v022-release-gate.py"
    spec = importlib.util.spec_from_file_location("check_v022_release_gate_for_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_target_smoke_module():
    script = ROOT / "scripts" / "smoke-v022-release-target-deploy-rollback.py"
    spec = importlib.util.spec_from_file_location("smoke_v022_release_target_deploy_rollback_for_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_feishu_smoke_payload() -> dict:
    return {
        "status": "PASS",
        "scope": "real-feishu-im-readonly",
        "requiresNetwork": True,
        "writesMessages": False,
        "writesFiles": False,
        "rawIdentifiersPersisted": False,
        "auth": {
            "user": {"available": True, "tokenStatus": "valid"},
            "bot": {"available": True},
            "scopeChecks": {"im:chat:read": True, "im:message": True},
        },
        "im": {
            "command": "lark-cli im +chat-list --as user --page-size 1 --format json",
            "identity": "user",
            "readOnly": True,
            "itemCount": 1,
            "firstChatIdHash": "abc123",
        },
        "redaction": {"rawChatPayload": "not_persisted"},
    }


def _write_promoted_acceptance(path: Path) -> None:
    lines = [
        "# v0.2.2 Acceptance Checklist",
        "",
        "| ID | Area | Acceptance | Status | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index in range(1, 20):
        item_id = f"R22-{index:02d}"
        status = "PASS" if item_id == "R22-12" else "PASS-SIMULATED"
        lines.append(f"| {item_id} | Simulated | Simulated acceptance | {status} | promoted release gate test |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hash16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()[:16]


def _target_command_rows(module, *, rebuilt_rollback_baseline: bool = False) -> list[dict]:
    sequence = (
        module.TARGET_DEPLOY_COMMAND_SEQUENCE_WITH_REBUILT_ROLLBACK_BASELINE
        if rebuilt_rollback_baseline
        else module.TARGET_DEPLOY_COMMAND_SEQUENCE
    )
    return [
        {
            "name": name,
            "argvHash": _hash16(f"{name}:argv"),
            "exitCode": 0,
            "stdoutHash": _hash16(f"{name}:stdout"),
            "stderrHash": _hash16(f"{name}:stderr"),
        }
        for name in sequence
    ]


def _add_target_pass_boundary(payload: dict, module) -> dict:
    payload.update(
        {
            "scope": "target-environment-web-linux-service",
            "productionEnvironment": True,
            "requiresRoot": True,
            "requiresSystemd": True,
            "requiresNetwork": True,
            "pointerMethod": "target-current-symlink",
            "preState": {
                "currentVersion": "0.2.1",
                "serviceActive": True,
                "serviceEnabled": True,
            },
            "target": {
                "sshHostHash": _hash16("host"),
                "sshUserHash": _hash16("user"),
                "sshPortHash": _hash16("port"),
                "serviceNameHash": _hash16("service"),
                "installRootHash": _hash16("install"),
                "workspaceRootHash": _hash16("workspace"),
                "rawTargetPersisted": False,
            },
            "commands": _target_command_rows(module),
            "redaction": {
                "rawTargetPersisted": False,
                "rawCommandsPersisted": False,
                "rawStdoutPersisted": False,
                "rawStderrPersisted": False,
                "rawSecretsPersisted": False,
            },
        }
    )
    payload.setdefault("deploy", {})["targetCheckCommandPassed"] = True
    payload["deploy"]["serviceActiveAfterDeploy"] = True
    payload["deploy"]["serviceEnabledAfterDeploy"] = True
    payload.setdefault("rollback", {})["serviceActiveAfterRollback"] = True
    payload["rollback"]["serviceEnabledAfterRollback"] = True
    return payload


def _online_pass_payload(module) -> dict:
    payload = json.loads(module.ONLINE_WEB_BROWSER_SMOKE_ARTIFACT.read_text(encoding="utf-8"))
    payload["status"] = "PASS"
    payload["generatedAt"] = "2099-01-01T00:00:00+00:00"
    payload["assertionErrors"] = []
    metrics = payload.setdefault("metrics", {})
    for key in (
        "emailVisible",
        "versionVisible",
        "newSessionHeadline",
        "oldHeadlineHidden",
        "projectEntry",
        "generalEntry",
        "runCenterHidden",
        "localFallbackHidden",
        "bodyHasSystemStack",
        "codeHasMonoStack",
        "runTimingVisible",
    ):
        metrics[key] = True
    metrics["runTimingInProcessSummary"] = True
    run_timing = metrics.setdefault("runTiming", {})
    run_timing.update(
        {
            "attempted": True,
            "visible": True,
            "inProcessSummary": True,
            "fallbackVisible": False,
            "finalLabelVisible": True,
        }
    )
    metrics.setdefault("projectStartMenu", {}).update(
        {
            "visible": True,
            "hasImport": True,
            "hasNoProject": True,
            "hasSearch": True,
            "closedOnBlank": True,
        }
    )
    metrics.setdefault("narrowViewport", {}).update({"noHorizontalOverflow": True})
    payload["consoleErrors"] = []
    payload.setdefault("redaction", {}).update(
        {
            "rawTargetPersisted": False,
            "rawPasswordPersisted": False,
            "rawSecretsPersisted": False,
        }
    )
    return payload


def test_v022_release_gate_reports_structured_pass(tmp_path):
    artifact = tmp_path / "release-gate.json"
    result = _run_gate("--json", "--artifact", str(artifact), check=False)

    payload = json.loads(result.stdout)
    artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
    blocker_ids = {item["id"] for item in payload["blockers"]}

    assert payload == artifact_payload
    assert payload["status"] == "PASS"
    assert payload["releasable"] is True
    assert result.returncode == 0
    assert payload["matrix"]["status"] == "REVIEWED-PASS"
    assert payload["matrix"]["rows"] >= 19
    assert payload["matrix"]["commands"] >= 34
    assert blocker_ids == set()
    assert "r22-12-release-gate-not-pass" not in blocker_ids
    assert "final-release-review-not-pass" not in blocker_ids
    assert "broader-real-network-validation" not in blocker_ids
    assert "v022-release-manifest-missing" not in blocker_ids
    assert "deploy-rollback-smoke-missing" not in blocker_ids
    assert "feishu-im-real-credential-smoke" not in blocker_ids

    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["harness-matrix-reviewed"]["status"] == "pass"
    assert checks["acceptance-ids-complete"]["status"] == "pass"
    assert checks["non-release-acceptance-complete"]["status"] == "pass"
    assert checks["release-scripts-present"]["status"] == "pass"
    assert checks["release-gate-doc"]["status"] == "pass"
    assert checks["release-manifest-valid"]["status"] == "pass"
    assert checks["public-manifest-promoted"]["status"] == "pass"
    assert checks["release-default-versions-promoted"]["status"] == "pass"
    assert checks["public-release-package-valid"]["status"] == "pass"
    assert checks["target-deploy-rollback-smoke-valid"]["status"] == "pass"
    assert checks["target-environment-deploy-rollback"]["status"] == "pass"
    assert checks["deploy-rollback-smoke-valid"]["status"] == "pass"
    assert checks["production-deploy-online-valid"]["status"] == "pass"
    assert checks["online-web-browser-smoke-valid"]["status"] == "waived"
    assert checks["online-web-browser-smoke-waiver-valid"]["status"] == "pass"
    assert checks["feishu-im-real-credential-smoke-valid"]["status"] == "pass"


def test_v022_release_gate_require_releasable_passes_after_target_smoke():
    result = _run_gate("--json", "--require-releasable", check=False)

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["status"] == "PASS"
    assert payload["releasable"] is True
    assert payload["blockers"] == []
    assert payload["errors"] == []


def test_v022_release_gate_validates_production_and_online_artifacts(tmp_path, monkeypatch):
    module = _load_gate_module()

    production = tmp_path / "production-deploy-online.json"
    online = tmp_path / "online-web-browser-smoke.json"
    production_payload = json.loads(module.PRODUCTION_DEPLOY_ONLINE_ARTIFACT.read_text(encoding="utf-8"))
    online_payload = _online_pass_payload(module)
    production.write_text(json.dumps(production_payload), encoding="utf-8")
    online.write_text(json.dumps(online_payload), encoding="utf-8")
    monkeypatch.setattr(module, "PRODUCTION_DEPLOY_ONLINE_ARTIFACT", production)
    monkeypatch.setattr(module, "ONLINE_WEB_BROWSER_SMOKE_ARTIFACT", online)

    payload = module.evaluate_release_gate()
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["production-deploy-online-valid"]["status"] == "pass"
    assert checks["online-web-browser-smoke-valid"]["status"] == "pass"
    assert checks["online-web-browser-smoke-freshness"]["status"] == "pass"

    production_payload["postState"]["serviceActive"] = False
    production.write_text(json.dumps(production_payload), encoding="utf-8")
    failed_production = module.evaluate_release_gate()
    failed_checks = {item["id"]: item for item in failed_production["checks"]}
    assert failed_production["status"] == "BLOCKED"
    assert failed_checks["production-deploy-online-valid"]["status"] == "fail"
    assert any("postState.serviceActive" in error for error in failed_production["errors"])

    production_payload["postState"]["serviceActive"] = True
    production.write_text(json.dumps(production_payload), encoding="utf-8")
    online_payload["metrics"]["newSessionHeadline"] = False
    online.write_text(json.dumps(online_payload), encoding="utf-8")
    failed_online = module.evaluate_release_gate()
    failed_checks = {item["id"]: item for item in failed_online["checks"]}
    assert failed_online["status"] == "BLOCKED"
    assert failed_checks["online-web-browser-smoke-valid"]["status"] == "fail"
    assert any("metrics.newSessionHeadline" in error for error in failed_online["errors"])


def test_v022_public_manifest_requires_exact_artifact_size(tmp_path, monkeypatch):
    module = _load_gate_module()
    manifest = json.loads(module.PUBLIC_SITE_MANIFEST.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact.get("id") == "webui-windows-x64":
            artifact.pop("size", None)
            break
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(module, "PUBLIC_SITE_MANIFEST", manifest_path)

    ok, detail = module._validate_public_manifest_promotion()
    assert ok is False
    assert "webui-windows-x64.size=None" in detail


def test_v022_public_manifest_validates_download_size_and_hash(tmp_path, monkeypatch):
    module = _load_gate_module()
    site_root = tmp_path / "site"
    artifacts = []
    for artifact_id, expected in module.EXPECTED_PUBLIC_ARTIFACTS.items():
        artifacts.append({"id": artifact_id, **expected, "status": "ready"})
        download_path = site_root.joinpath(*expected["href"].split("/"))
        download_path.parent.mkdir(parents=True, exist_ok=True)
        download_path.write_bytes(b"wrong-bytes")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "product": "EcoreX",
                "version": module.EXPECTED_RELEASE_VERSION,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "PUBLIC_SITE_MANIFEST", manifest_path)
    monkeypatch.setattr(module, "PUBLIC_SITE_ROOT", site_root)

    ok, detail = module._validate_public_manifest_promotion()
    assert ok is False
    assert ".download.size=" in detail
    assert ".download.sha256=" in detail


def test_v022_target_deploy_smoke_requires_redacted_command_evidence():
    module = _load_gate_module()
    payload = json.loads((ROOT / "docs" / "v0.2.2" / "artifacts" / "release-deploy-rollback-smoke.json").read_text(encoding="utf-8"))
    _add_target_pass_boundary(payload, module)

    ok, detail = module._validate_target_deploy_smoke_artifact(payload)
    assert ok is True
    assert "target-environment-web-linux-service" in detail

    rebuilt_baseline_payload = json.loads(json.dumps(payload))
    rebuilt_baseline_payload["commands"] = _target_command_rows(module, rebuilt_rollback_baseline=True)
    ok, detail = module._validate_target_deploy_smoke_artifact(rebuilt_baseline_payload)
    assert ok is True
    assert "target-environment-web-linux-service" in detail

    def assert_rejected(mutator, expected: str) -> None:
        tampered = json.loads(json.dumps(payload))
        mutator(tampered)
        try:
            module._validate_target_deploy_smoke_artifact(tampered)
        except module.ReleaseGateError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"target deploy smoke accepted malformed evidence: {expected}")

    assert_rejected(lambda item: item.update({"requiresRoot": False}), "requiresRoot must be true")
    assert_rejected(lambda item: item.pop("preState", None), "preState must be an object")
    assert_rejected(
        lambda item: item["preState"].update({"currentVersion": "0.2.2"}),
        "preState.currentVersion must be 0.2.1",
    )
    assert_rejected(
        lambda item: item["preState"].update({"serviceActive": False}),
        "preState.serviceActive must be true",
    )
    assert_rejected(
        lambda item: item["preState"].update({"serviceEnabled": False}),
        "preState.serviceEnabled must be true",
    )
    assert_rejected(
        lambda item: item["deploy"].update({"serviceActiveAfterDeploy": False}),
        "deploy.serviceActiveAfterDeploy must be true",
    )
    assert_rejected(
        lambda item: item["deploy"].update({"serviceEnabledAfterDeploy": False}),
        "deploy.serviceEnabledAfterDeploy must be true",
    )
    assert_rejected(
        lambda item: item["rollback"].update({"serviceActiveAfterRollback": False}),
        "rollback.serviceActiveAfterRollback must be true",
    )
    assert_rejected(
        lambda item: item["rollback"].update({"serviceEnabledAfterRollback": False}),
        "rollback.serviceEnabledAfterRollback must be true",
    )

    payload["target"]["sshUserHash"] = "raw-user"
    try:
        module._validate_target_deploy_smoke_artifact(payload)
    except module.ReleaseGateError as exc:
        assert "target.sshUserHash must be an uppercase hex hash" in str(exc)
    else:
        raise AssertionError("target deploy smoke accepted non-hash target evidence")
    payload["target"]["sshUserHash"] = _hash16("user")

    payload["commands"][7]["name"] = "skip_check_deploy"
    try:
        module._validate_target_deploy_smoke_artifact(payload)
    except module.ReleaseGateError as exc:
        assert "expected ordered command chain" in str(exc)
    else:
        raise AssertionError("target deploy smoke accepted incomplete command chain")
    payload["commands"] = _target_command_rows(module)

    payload["commands"][0]["argvHash"] = "argv-0"
    try:
        module._validate_target_deploy_smoke_artifact(payload)
    except module.ReleaseGateError as exc:
        assert "argvHash must be an uppercase hex hash" in str(exc)
    else:
        raise AssertionError("target deploy smoke accepted non-hash command evidence")
    payload["commands"] = _target_command_rows(module)

    payload["commands"][0]["stdout"] = "raw target output"
    try:
        module._validate_target_deploy_smoke_artifact(payload)
    except module.ReleaseGateError as exc:
        assert "raw stdout" in str(exc)
    else:
        raise AssertionError("target deploy smoke accepted raw stdout")


def test_v022_target_smoke_timeout_failure_is_redacted(monkeypatch):
    module = _load_target_smoke_module()

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["ssh", "-i", "C:/secret-key.pem", "raw-user@raw-target.example", "echo raw-command"],
            timeout=1,
            output="raw stdout from target",
            stderr="raw stderr from target",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    commands = []
    try:
        module._run_command(
            "prepare_remote_dir",
            ["ssh", "-i", "C:/secret-key.pem", "raw-user@raw-target.example", "echo raw-command"],
            commands,
            timeout_seconds=1,
        )
    except module.TargetSmokeError as exc:
        assert str(exc) == "prepare_remote_dir timed out"
    else:
        raise AssertionError("timeout did not raise TargetSmokeError")

    serialized = json.dumps(commands, sort_keys=True)
    for raw in ("raw-target", "raw-user", "secret-key", "raw-command", "raw stdout", "raw stderr"):
        assert raw not in serialized

    reason, reason_hash = module._safe_failure_reason(module.TargetSmokeError("prepare_remote_dir timed out"))
    assert reason == "command-timeout"
    blocked = module._blocked_artifact(
        argparse.Namespace(
            package=str(ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"),
            expected_version="0.2.2",
            ssh_host="raw-target.example",
            ssh_user="raw-user",
            public_base_url="http://raw-target.example/app/",
        ),
        "ssh -i C:/secret-key.pem raw-user@raw-target.example",
        reason_hash,
    )
    serialized_blocked = json.dumps(blocked, sort_keys=True)
    assert blocked["reason"] == "target-smoke-failed"
    for raw in ("raw-target", "raw-user", "secret-key"):
        assert raw not in serialized_blocked


def test_v022_target_smoke_command_template_is_secret_free():
    module = _load_target_smoke_module()
    payload = module._target_command_template(
        argparse.Namespace(
            package=str(ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"),
            installer=str(ROOT / "scripts" / "install-ecorex-web.sh"),
            checker=str(ROOT / "scripts" / "check-ecorex-web-release.sh"),
            expected_version="0.2.2",
                expected_sha256="9631E563D5457B7032228384F139370E49535AA317E9C24BA1A0442F39792D4D",
            ssh_host="raw-target.example",
            ssh_user="raw-user",
            ssh_identity="C:/secret-key.pem",
            public_base_url="https://raw-target.example/app/",
            template_artifact="docs/v0.2.2/artifacts/release-target-command-template.json",
        )
    )

    assert payload["status"] == "READY_FOR_TARGET_INPUT"
    assert payload["targetExecution"] is False
    assert payload["networkUsed"] is False
    assert payload["artifact"]["sha256MatchesExpected"] is True
    serialized = json.dumps(payload, sort_keys=True)
    for raw in ("raw-target", "raw-user", "secret-key", "https://raw-target"):
        assert raw not in serialized
    assert "<target-host>" in serialized
    assert "<path-to-private-key>" in serialized
    assert payload["templateArtifact"]["writesPassEvidence"] is False
    assert payload["templateArtifact"]["clearsTargetBlocker"] is False


def test_v022_target_command_template_artifact_is_gate_valid_and_non_promoting(tmp_path):
    smoke_module = _load_target_smoke_module()
    gate_module = _load_gate_module()
    template_artifact = tmp_path / "release-target-command-template.json"

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        exit_code = smoke_module.main(
            [
                "--print-command-template",
                "--template-artifact",
                str(template_artifact),
                "--ssh-host",
                "raw-target.example",
                "--ssh-user",
                "raw-user",
                "--ssh-identity",
                "C:/secret-key.pem",
                "--public-base-url",
                "https://raw-target.example/app/",
            ]
        )

    assert exit_code == 0
    payload = json.loads(template_artifact.read_text(encoding="utf-8"))
    stdout_payload = json.loads(stdout.getvalue())
    assert stdout_payload == payload
    serialized = json.dumps(payload, sort_keys=True)
    for raw in ("raw-target", "raw-user", "secret-key", "https://raw-target"):
        assert raw not in serialized

    detail = gate_module._validate_target_command_template_artifact(payload)
    assert "does not execute target or clear blocker" in detail

    assert payload["targetExecution"] is False
    assert payload["networkUsed"] is False
    assert payload["templateArtifact"]["writesPassEvidence"] is False
    assert payload["templateArtifact"]["clearsTargetBlocker"] is False


def test_v022_target_command_template_rejects_secret_shaped_text():
    gate_module = _load_gate_module()
    smoke_module = _load_target_smoke_module()
    payload = smoke_module._target_command_template(
        argparse.Namespace(
            package=str(ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"),
            installer=str(ROOT / "scripts" / "install-ecorex-web.sh"),
            checker=str(ROOT / "scripts" / "check-ecorex-web-release.sh"),
            expected_version="0.2.2",
            expected_sha256="9631E563D5457B7032228384F139370E49535AA317E9C24BA1A0442F39792D4D",
            template_artifact="docs/v0.2.2/artifacts/release-target-command-template.json",
        )
    )
    payload["notes"] = ["".join(("sk", "-", "testtargetcommandtemplateleak000000"))]

    try:
        gate_module._validate_target_command_template_artifact(payload)
    except gate_module.ReleaseGateError as exc:
        assert "API-key shaped token" in str(exc)
    else:
        raise AssertionError("target command template accepted secret-shaped text")


def test_v022_target_command_template_requires_reproducible_local_inputs():
    gate_module = _load_gate_module()
    smoke_module = _load_target_smoke_module()
    payload = smoke_module._target_command_template(
        argparse.Namespace(
            package=str(ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"),
            installer=str(ROOT / "scripts" / "install-ecorex-web.sh"),
            checker=str(ROOT / "scripts" / "check-ecorex-web-release.sh"),
            expected_version="0.2.2",
            expected_sha256="9631E563D5457B7032228384F139370E49535AA317E9C24BA1A0442F39792D4D",
            template_artifact="docs/v0.2.2/artifacts/release-target-command-template.json",
        )
    )
    payload["localInputs"] = [{"name": "package"}, {"name": "installer"}, {"name": "checker"}]

    try:
        gate_module._validate_target_command_template_artifact(payload)
    except gate_module.ReleaseGateError as exc:
        assert "localInputs.package.exists must be true" in str(exc)
    else:
        raise AssertionError("target command template accepted non-reproducible local input rows")


def test_v022_target_blocked_reason_hash_must_be_hex_hash():
    module = _load_gate_module()
    payload = {
        "status": "BLOCKED",
        "scope": "target-environment-web-linux-service",
        "productionEnvironment": True,
        "requiresRoot": True,
        "requiresSystemd": True,
        "requiresNetwork": True,
        "reason": "missing-target-confirmation",
        "reasonHash": "raw-target.example",
        "target": {"rawTargetPersisted": False},
        "redaction": {
            "rawTargetPersisted": False,
            "rawCommandsPersisted": False,
            "rawStdoutPersisted": False,
            "rawStderrPersisted": False,
            "rawSecretsPersisted": False,
        },
    }

    try:
        module._validate_target_deploy_smoke_artifact(payload)
    except module.ReleaseGateError as exc:
        assert "reasonHash must be uppercase hex hash" in str(exc)
    else:
        raise AssertionError("target deploy smoke accepted raw reasonHash")

    payload["reasonHash"] = "228889AB6B5944AE"
    ok, detail = module._validate_target_deploy_smoke_artifact(payload)
    assert ok is False
    assert "missing-target-confirmation" in detail


def test_v022_release_manifest_requires_target_pass_hash_boundary_markers():
    module = _load_gate_module()
    text = (ROOT / "docs" / "v0.2.2" / "release-manifest.md").read_text(encoding="utf-8")

    assert "exact ordered target smoke command chain" in text
    assert "target.*Hash" in text

    weakened = text.replace("exact ordered target smoke command chain", "target command chain")
    try:
        module._validate_release_manifest(
            weakened,
            "blocked",
            {"online-web-browser-smoke-not-pass"},
        )
    except module.ReleaseGateError as exc:
        assert "exact ordered target smoke command chain" in str(exc)
    else:
        raise AssertionError("release manifest accepted missing target command-chain marker")

    weakened = text.replace("target.*Hash", "target hashes")
    try:
        module._validate_release_manifest(
            weakened,
            "blocked",
            {"online-web-browser-smoke-not-pass"},
        )
    except module.ReleaseGateError as exc:
        assert "target.*Hash" in str(exc)
    else:
        raise AssertionError("release manifest accepted missing target hash-boundary marker")


def test_v022_release_gate_feishu_overlay_restores_blocker_when_missing_or_invalid(tmp_path):
    module = _load_gate_module()

    module.FEISHU_IM_SMOKE_ARTIFACT = tmp_path / "missing-feishu-smoke.json"
    missing_payload = module.evaluate_release_gate()
    missing_blockers = {item["id"] for item in missing_payload["blockers"]}
    assert "feishu-im-real-credential-smoke" in missing_blockers
    missing_checks = {item["id"]: item for item in missing_payload["checks"]}
    assert missing_checks["feishu-im-real-credential-smoke-valid"]["status"] == "blocked"

    invalid = tmp_path / "invalid-feishu-smoke.json"
    payload = _valid_feishu_smoke_payload()
    payload["im"]["firstChatIdHash"] = "oc_raw_chat_id_should_not_persist"
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    module.FEISHU_IM_SMOKE_ARTIFACT = invalid

    invalid_payload = module.evaluate_release_gate()
    invalid_blockers = {item["id"] for item in invalid_payload["blockers"]}
    invalid_checks = {item["id"]: item for item in invalid_payload["checks"]}
    assert "feishu-im-real-credential-smoke" in invalid_blockers
    assert invalid_checks["feishu-im-real-credential-smoke-valid"]["status"] == "fail"
    assert any("raw Feishu identifier" in error for error in invalid_payload["errors"])

    invalid_command = tmp_path / "invalid-feishu-command-smoke.json"
    payload = _valid_feishu_smoke_payload()
    payload["im"]["command"] = "lark-cli im +messages-send --as user --chat-id redacted"
    invalid_command.write_text(json.dumps(payload), encoding="utf-8")
    module.FEISHU_IM_SMOKE_ARTIFACT = invalid_command

    invalid_command_payload = module.evaluate_release_gate()
    invalid_command_blockers = {item["id"] for item in invalid_command_payload["blockers"]}
    assert "feishu-im-real-credential-smoke" in invalid_command_blockers
    assert any("expected read-only chat-list command" in error for error in invalid_command_payload["errors"])


def test_v022_release_gate_has_state_aware_releasable_pass_path(tmp_path, monkeypatch, capsys):
    module = _load_gate_module()

    acceptance = tmp_path / "acceptance-checklist.md"
    review = tmp_path / "review-log.md"
    evidence = tmp_path / "evidence-ledger.md"
    gate_doc = tmp_path / "release-gate.md"
    manifest_doc = tmp_path / "release-manifest.md"
    public_manifest = tmp_path / "manifest.json"
    deploy_smoke = tmp_path / "release-deploy-rollback-smoke.json"
    target_smoke = tmp_path / "release-target-deploy-rollback-smoke.json"
    production_online = tmp_path / "production-deploy-online.json"
    online_browser = tmp_path / "online-web-browser-smoke.json"
    default_sh = tmp_path / "install-ecorex-web.sh"
    default_check = tmp_path / "check-ecorex-web-release.sh"
    default_server = tmp_path / "check-ecorex-server-release.sh"
    default_public = tmp_path / "install-ecorex-public-release.sh"
    default_ps1 = tmp_path / "prepare-ecorex-web-release.ps1"
    default_webui_ps1 = tmp_path / "prepare-ecorex-webui-local-release.ps1"

    _write_promoted_acceptance(acceptance)
    review.write_text("# Review\n\n## Pending Reviews\n\n", encoding="utf-8")
    evidence.write_text("# Evidence\n\n## Pending Evidence\n\n", encoding="utf-8")
    gate_doc.write_text(
        "\n".join(
            [
                "# v0.2.2 Release Gate",
                "",
                "## Status",
                "",
                "Current status: `PASS`.",
                "",
                "## Release Evidence",
                "",
                "No active release blockers.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_doc.write_text(
        "\n".join(
            [
                "# EcoreX v0.2.2 Release Manifest",
                "",
                "## Status",
                "",
                "Status: `RELEASE-PASS`.",
                "",
                "## Promoted Release Evidence",
                "",
                "Public manifest promoted.",
                "Release defaults promoted.",
                "Target-environment deploy/rollback smoke passed.",
                "Future PASS evidence includes the exact ordered target smoke command chain.",
                "Target evidence includes target.*Hash and command hash fields.",
                "",
                "Artifact: `web-linux-service`",
                f"Path: `{module.EXPECTED_WEB_SERVICE_TARBALL}`",
                f"SHA256: `{module.EXPECTED_WEB_SERVICE_SHA256}`",
                "docs/v0.2.2/artifacts/release-deploy-rollback-smoke.json",
                "docs/v0.2.2/artifacts/feishu-im-real-credential-smoke.json",
                "Real Feishu/IM read-only credential smoke now passes",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    public_manifest.write_text(
        json.dumps(
            {
                "product": "EcoreX",
                "version": module.EXPECTED_RELEASE_VERSION,
                "artifacts": [
                    {
                        "id": "web-linux-service",
                        "version": module.EXPECTED_RELEASE_VERSION,
                        "fileName": module.EXPECTED_WEB_SERVICE_FILENAME,
                        "href": f"downloads/{module.EXPECTED_WEB_SERVICE_FILENAME}",
                        "size": module.EXPECTED_PUBLIC_ARTIFACTS["web-linux-service"]["size"],
                        "sha256": module.EXPECTED_WEB_SERVICE_SHA256,
                        "status": "ready",
                    },
                    {
                        "id": "webui-windows-x64",
                        "version": module.EXPECTED_RELEASE_VERSION,
                        "fileName": "EcoreX_0.2.2-webui-windows-x64.zip",
                        "href": "downloads/EcoreX_0.2.2-webui-windows-x64.zip",
                        "size": module.EXPECTED_PUBLIC_ARTIFACTS["webui-windows-x64"]["size"],
                        "sha256": module.EXPECTED_PUBLIC_ARTIFACTS["webui-windows-x64"]["sha256"],
                        "status": "ready",
                    },
                    {
                        "id": "webui-macos-universal",
                        "version": module.EXPECTED_RELEASE_VERSION,
                        "fileName": "EcoreX_0.2.2-webui-macos-universal.zip",
                        "href": "downloads/EcoreX_0.2.2-webui-macos-universal.zip",
                        "size": module.EXPECTED_PUBLIC_ARTIFACTS["webui-macos-universal"]["size"],
                        "sha256": module.EXPECTED_PUBLIC_ARTIFACTS["webui-macos-universal"]["sha256"],
                        "status": "ready",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    payload = json.loads((ROOT / "docs" / "v0.2.2" / "artifacts" / "release-deploy-rollback-smoke.json").read_text(encoding="utf-8"))
    deploy_smoke.write_text(json.dumps(payload), encoding="utf-8")
    target_payload = json.loads(json.dumps(payload))
    _add_target_pass_boundary(target_payload, module)
    target_smoke.write_text(json.dumps(target_payload), encoding="utf-8")
    production_online.write_text(module.PRODUCTION_DEPLOY_ONLINE_ARTIFACT.read_text(encoding="utf-8"), encoding="utf-8")
    online_browser.write_text(json.dumps(_online_pass_payload(module)), encoding="utf-8")

    for path in (default_sh, default_check, default_server, default_public):
        path.write_text('VERSION="${VERSION:-0.2.2}"\n', encoding="utf-8")
    default_ps1.write_text('[string]$Version = "0.2.2"\n', encoding="utf-8")
    default_webui_ps1.write_text('[string]$Version = "0.2.2"\n', encoding="utf-8")

    monkeypatch.setattr(module, "ACCEPTANCE_PATH", acceptance)
    monkeypatch.setattr(module, "REVIEW_PATH", review)
    monkeypatch.setattr(module, "EVIDENCE_PATH", evidence)
    monkeypatch.setattr(module, "RELEASE_GATE_DOC", gate_doc)
    monkeypatch.setattr(module, "RELEASE_MANIFEST", manifest_doc)
    monkeypatch.setattr(module, "PUBLIC_SITE_MANIFEST", public_manifest)
    monkeypatch.setattr(module, "DEPLOY_SMOKE_ARTIFACT", deploy_smoke)
    monkeypatch.setattr(module, "TARGET_DEPLOY_SMOKE_ARTIFACT", target_smoke)
    monkeypatch.setattr(module, "PRODUCTION_DEPLOY_ONLINE_ARTIFACT", production_online)
    monkeypatch.setattr(module, "ONLINE_WEB_BROWSER_SMOKE_ARTIFACT", online_browser)
    monkeypatch.setattr(
        module,
        "RELEASE_DEFAULT_VERSION_FILES",
        [
            (str(default_sh), r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
            (str(default_check), r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
            (str(default_server), r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
            (str(default_public), r'VERSION="\$\{VERSION:-([^}"]+)\}"'),
            (str(default_ps1), r'\[string\]\$Version\s*=\s*"([^"]+)"'),
            (str(default_webui_ps1), r'\[string\]\$Version\s*=\s*"([^"]+)"'),
        ],
    )

    payload = module.evaluate_release_gate()
    assert payload["status"] == "PASS"
    assert payload["releasable"] is True
    assert payload["errors"] == []
    assert payload["blockers"] == []
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["public-manifest-promoted"]["status"] == "pass"
    assert checks["release-default-versions-promoted"]["status"] == "pass"
    assert checks["public-release-package-valid"]["status"] == "pass"
    assert checks["target-environment-deploy-rollback"]["status"] not in {"blocked", "fail"}
    assert checks["production-deploy-online-valid"]["status"] == "pass"
    assert checks["online-web-browser-smoke-valid"]["status"] == "pass"
    assert checks["online-web-browser-smoke-freshness"]["status"] == "pass"
    assert checks["release-gate-doc"]["status"] == "pass"
    assert checks["release-manifest-valid"]["status"] == "pass"

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        exit_code = module.main(["--json", "--require-releasable"])
    cli_payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert cli_payload["status"] == "PASS"
    assert cli_payload["releasable"] is True
    capsys.readouterr()


def test_v022_release_gate_source_contract_keeps_explicit_boundaries():
    source = (ROOT / "scripts" / "check-v022-release-gate.py").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "v0.2.2" / "release-gate.md").read_text(encoding="utf-8")

    for marker in (
        "check-v022-harness-matrix.py",
        "REQUIRED_ACCEPTANCE_IDS",
        "REQUIRED_RELEASE_SCRIPTS",
        "BLOCKER-PENDING-CREDENTIALS",
        "broader-real-network-validation",
        "v022-release-manifest-missing",
        "deploy-rollback-smoke-missing",
        "release-manifest-valid",
        "deploy-rollback-smoke-valid",
        "public-manifest-promoted",
        "release-default-versions-promoted",
        "public-release-package-valid",
        "public-release-package-not-built",
        "EXPECTED_PUBLIC_RELEASE_ZIP_SHA256",
        "EXPECTED_WEB_SERVICE_SIZE",
        "PUBLIC_SITE_ROOT",
        "download.sha256",
        "TARGET_DEPLOY_SMOKE_ARTIFACT",
        "TARGET_COMMAND_TEMPLATE_ARTIFACT",
        "TARGET_DEPLOY_COMMAND_SEQUENCE",
        "TARGET_DEPLOY_COMMAND_SEQUENCE_WITH_REBUILT_ROLLBACK_BASELINE",
        "HASH_SHAPE_PATTERN",
        "expected ordered command chain",
        "target-deploy-rollback-smoke-valid",
        "target-command-template-valid",
        "production-deploy-online-valid",
            "online-web-browser-smoke-valid",
            "online-web-browser-smoke-freshness",
        "PRODUCTION_DEPLOY_ONLINE_ARTIFACT",
            "ONLINE_WEB_BROWSER_SMOKE_ARTIFACT",
            "ONLINE_WEB_BROWSER_SMOKE_WAIVER_ARTIFACT",
            "EXPECTED_ONLINE_SMOKE_WAIVER_REASON",
        "release-target-deploy-rollback-smoke.json",
        "production-deploy-online.json",
        "online-web-browser-smoke.json",
        "release-target-command-template.json",
        "target-environment-deploy-rollback",
        "target-environment-web-linux-service",
        "public-manifest-not-promoted",
        "release-defaults-not-promoted",
        "target-environment-deploy-rollback-not-exercised",
        "online-web-browser-smoke-not-pass",
        "final-release-review-not-pass",
        "RELEASE-PASS",
        "Promoted Release Evidence",
        "local-filesystem-web-linux-service",
        "feishu-im-real-credential-smoke-valid",
        "real-feishu-im-readonly",
        "EXPECTED_FEISHU_IM_COMMAND",
        "EXPECTED_WEB_SERVICE_SHA256",
        "EXPECTED_PUBLIC_ARTIFACTS",
        "--require-releasable",
    ):
        assert marker in source

    for marker in (
        "Current status: `PASS`",
        "No active release blockers",
        "online-web-browser-smoke-waiver.json",
        "WAIVED",
        "feishu-im-real-credential-smoke",
        "reviewed local HTTP/SSE browser smoke",
        "feishu-im-real-credential-smoke.json",
        "release-manifest.md",
        "release-deploy-rollback-smoke.json",
        "Promoted Release Checks",
    ):
        assert marker in doc

    smoke_source = (ROOT / "scripts" / "smoke-v022-release-deploy-rollback.py").read_text(encoding="utf-8")
    for marker in (
        "local-filesystem-web-linux-service",
        "requiresSystemd",
        "manifest-pointer-fallback",
        "candidateRetainedForAudit",
        "SHA256SUMS.txt",
        "checksums.json",
    ):
        assert marker in smoke_source

    target_smoke_source = (ROOT / "scripts" / "smoke-v022-release-target-deploy-rollback.py").read_text(encoding="utf-8")
    for marker in (
        "--confirm-target-environment",
        "--write-blocked-artifact",
        "target-environment-web-linux-service",
        "rawCommandsPersisted",
        "rawStdoutPersisted",
        "rawStderrPersisted",
        "target-current-symlink",
        "--print-command-template",
        "--template-artifact",
        "READY_FOR_TARGET_INPUT",
        "clearsTargetBlocker",
        "sudo -n",
        "sshHostHash",
        "install_v022",
        "rollback_to_previous",
    ):
        assert marker in target_smoke_source

    feishu_smoke_source = (ROOT / "scripts" / "smoke-feishu-im-real-credential.py").read_text(encoding="utf-8")
    for marker in (
        "auth status --verify",
        "+chat-list",
        "rawIdentifiersPersisted",
        "rawChatPayload",
        "not_persisted",
        "writesMessages",
        "stdoutHash",
        "stderrHash",
    ):
        assert marker in feishu_smoke_source
    assert "detail[:500]" not in feishu_smoke_source
