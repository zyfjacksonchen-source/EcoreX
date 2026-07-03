from __future__ import annotations

import importlib.util
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    script = ROOT / "scripts" / "smoke-v026-production-agent-product-acceptance.py"
    spec = importlib.util.spec_from_file_location("smoke_v026_agent_product_acceptance", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_light_module():
    script = ROOT / "scripts" / "light-real-release-validation.py"
    spec = importlib.util.spec_from_file_location("light_real_release_validation", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_strategy_module():
    script = ROOT / "scripts" / "real-release-multi-agent-strategy.py"
    spec = importlib.util.spec_from_file_location("real_release_multi_agent_strategy", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_rerun_module():
    script = ROOT / "scripts" / "real-release-rerun-strategy.py"
    spec = importlib.util.spec_from_file_location("real_release_rerun_strategy", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _synthetic_checks(module, count: int | None = None):
    count = int(count or module.TARGET_TOTAL_CHECKS)
    groups = [row[0] for row in module.NEW_CASE_GROUPS]
    declared = list(module.DECLARED_CASE_REGISTRY)
    checks = []
    for index in range(count):
        if index < len(declared):
            case = declared[index]
            group = case["group"]
            priority = case["priority"]
        else:
            group = groups[(index - len(declared)) % len(groups)]
            priority = "P0" if index < 380 else ("P1" if index < 460 else "P2")
        checks.append(
            {
                "index": index + 1,
                "source": "unit",
                "sourceIndex": index + 1,
                "caseId": f"unit-{index + 1:03d}",
                "group": group,
                "name": f"unit check {index + 1}",
                "status": "PASS",
                "priority": priority,
                "cost": "low",
                "tags": [],
                "enabled": True,
                "removable": priority == "P2",
                "hardGate": priority == "P0",
                "skipReason": "",
                "detail": {},
            }
        )
    return checks


def test_agent_product_registry_declares_declared_new_cases():
    module = _load_module()

    assert len(module.DECLARED_CASE_REGISTRY) == module.TARGET_NEW_CHECKS
    assert sum(row[1] for row in module.NEW_CASE_GROUPS) == module.TARGET_NEW_CHECKS
    assert module.TARGET_NEW_CHECKS == 308
    assert module.TARGET_TOTAL_CHECKS == 540
    assert module.MIN_ENABLED_CHECKS == 380
    p0_cases = [case for case in module.DECLARED_CASE_REGISTRY if case["priority"] == "P0"]
    p2_cases = [case for case in module.DECLARED_CASE_REGISTRY if case["priority"] == "P2"]
    assert len(p0_cases) >= 200
    assert p2_cases
    assert all(case["hardGate"] is True and case["removable"] is False for case in p0_cases)
    assert all(case["hardGate"] is False and case["removable"] is True for case in p2_cases)


def test_agent_product_quality_gates_require_p0_and_count():
    module = _load_module()
    checks = _synthetic_checks(module)

    gates = module.evaluate_quality_gates(checks)
    assert gates["status"] == "PASS"
    assert gates["checkCount"] == module.TARGET_TOTAL_CHECKS
    assert gates["enabledCheckCount"] == module.TARGET_TOTAL_CHECKS

    checks[0] = {**checks[0], "status": "FAIL"}
    gates = module.evaluate_quality_gates(checks)
    assert gates["status"] == "FAIL"
    assert gates["hardGateFailures"]


def test_agent_product_redaction_removes_secrets_urls_and_paths():
    module = _load_module()
    payload = {
        "secret": "sk-testSECRET1234567890",
        "url": "https://example.com/private/path",
        "path": r"C:\Users\person\secret.txt",
        "nested": {"auth": "Bearer abcdefghijklmnopqrstuvwxyz"},
    }

    assert module.find_redaction_violations(payload)
    redacted = module.public_payload(payload)
    assert not module.find_redaction_violations(redacted)
    text = str(redacted)
    assert "sk-test" not in text
    assert "example.com" not in text
    assert "Users" not in text


def test_agent_product_suite_payload_has_required_report_fields():
    module = _load_module()
    checks = _synthetic_checks(module)
    payload = module.build_suite_payload(
        [{"scope": "unit", "checks": checks}],
        started_at=time.perf_counter(),
        budget_mode="tiered",
        pressure_users=20,
        pressure_turns=3,
    )

    assert payload["schemaVersion"] == "v0.2.7-agent-product-acceptance-v1"
    assert payload["status"] == "PASS"
    assert payload["checkCount"] == module.TARGET_TOTAL_CHECKS
    assert payload["enabledCheckCount"] == module.TARGET_TOTAL_CHECKS
    assert payload["hardGateFailures"] == []
    assert "pressureProfile" in payload
    assert "modelRouteEvidence" in payload
    assert "stateMachineEvidence" in payload
    assert "matrixChangeLog" in payload
    assert payload["redaction"]["violations"] == []


def test_agent_product_suite_payload_surfaces_child_matrix_without_checks_as_failure():
    module = _load_module()
    checks = _synthetic_checks(module)
    payload = module.build_suite_payload(
        [
            {"scope": "legacy", "checks": checks},
            {
                "scope": "production-agent-product-fresh-user-305",
                "status": "FAIL",
                "errorType": "RuntimeError",
                "remoteExitCode": 1,
                "remoteStdoutHash": "ABC",
                "remoteStderrHash": "DEF",
            },
        ],
        started_at=time.perf_counter(),
        budget_mode="tiered",
        pressure_users=20,
        pressure_turns=3,
    )

    assert payload["status"] == "FAIL"
    assert payload["failCount"] == 1
    assert payload["hardGateFailures"]
    assert payload["hardGateFailures"][0]["group"] == "release-gate"
    assert payload["hardGateFailures"][0]["detail"]["errorType"] == "RuntimeError"


def test_agent_product_redaction_metadata_does_not_poison_gate():
    module = _load_module()
    checks = _synthetic_checks(module)
    payload = module.build_suite_payload(
        [
            {
                "scope": "unit-child",
                "checks": checks,
                "redaction": {
                    "rawPasswordPersisted": False,
                    "violations": [r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"],
                },
            }
        ],
        started_at=time.perf_counter(),
        budget_mode="tiered",
        pressure_users=20,
        pressure_turns=3,
    )

    assert payload["status"] == "PASS"
    assert payload["redaction"]["violations"] == []


def test_agent_product_redaction_failure_cannot_be_overwritten_by_gates():
    module = _load_module()
    checks = _synthetic_checks(module)
    checks[0] = {
        **checks[0],
        "detail": {"leakedUrl": "https://example.invalid/secret"},
    }
    payload = module.build_suite_payload(
        [{"scope": "unit-child", "checks": checks}],
        started_at=time.perf_counter(),
        budget_mode="tiered",
        pressure_users=20,
        pressure_turns=3,
    )

    assert payload["checkCount"] == module.TARGET_TOTAL_CHECKS
    assert payload["failCount"] == 0
    assert payload["status"] == "FAIL"
    assert "raw-url" in payload["redaction"]["violations"]


def test_agent_product_redaction_avoids_bearer_authorization_false_positive():
    module = _load_module()

    assert module.find_redaction_violations({"name": "bridge sends bearer authorization"}) == []
    assert module.find_redaction_violations({"header": "Bearer abcdefghijklmnopqrstuvwxyz"})


def test_agent_product_focused_rerun_expands_dependencies_and_compiles():
    module = _load_module()

    assert module.parse_focus_groups("stream-state-machine,context-session") == [
        "stream-state-machine",
        "context-session",
    ]
    assert module.expand_focus_groups(["context-session"]) == [
        "fresh-env",
        "auth-first-use",
        "stream-state-machine",
        "context-session",
    ]

    remote = module._render_remote_script(
        budget_mode="tiered",
        pressure_users=2,
        pressure_turns=1,
        focus_groups=["context-session"],
    )
    compile(remote, "<focused-rerun>", "exec")
    assert "production-agent-product-focused-rerun" in remote
    assert '"context-session"' in remote
    assert "latency_under(resp, 5000)" in remote
    assert "ecorex-release-notes-seen-version" in remote
    assert "chat-model-popover" in remote
    assert "stream baseline OpenAI option configured" in remote
    assert "stream baseline restored original chat model" in remote
    assert "pressure-observer" in remote
    assert "browser_automation_diagnostics" in remote
    assert "browser_diagnostics(" not in remote
    assert "from datetime import datetime, timedelta, timezone" in remote
    assert "public manifest carries rebuilt WebUI artifacts" in remote
    assert "custom Gemini switched model produces content" in remote
    assert "custom Gemini response avoids empty fallback apology" in remote
    assert "_stream_chunks_from_chat_response" in remote
    assert "model-switch-message" in remote
    assert "_emit_batch_image_ready" in remote
    assert "update-check endpoint exposes WebUI update policy and artifacts" in remote
    assert "admin release API exposes protected state and promote endpoints" in remote
    assert "admin release promotion validates staged artifacts before current switch" in remote
    assert "admin release page exposes one-click publish controls" in remote
    assert "/srv/ecorex-agent-download/current/checksums.json" not in remote
    assert "DATABASE_CONFIG_KEYS" in remote
    assert "XIN_AGENT_DATABASE" in remote


def test_legacy_200_behavior_manifest_date_is_not_hardcoded():
    source = (ROOT / "scripts" / "smoke-v026-production-200-user-behavior.py").read_text(encoding="utf-8")

    assert "valid_release_date" in source
    assert "artifact.get(\"updatedAt\") == \"2026-07-01\"" not in source
    assert "updated at manifest release date" in source


def test_light_real_release_validation_contract_passes():
    module = _load_light_module()
    report = module.build_report()

    assert report["schemaVersion"] == "real-release-light-validation-v1"
    assert report["status"] == "PASS"
    assert report["failCount"] == 0
    assert report["checkCount"] >= 40
    assert report["commands"]["light"] == "python scripts/真实发布轻量校验.py"
    assert report["commands"]["strategy"] == "python scripts/真实发布多Agent分工策略.py"
    assert report["commands"]["rerun"] == "python scripts/真实发布失败复验策略.py"
    assert report["commands"]["heavy"] == "python scripts/真实发布校验.py"


def test_multi_agent_strategy_contract_passes():
    module = _load_strategy_module()
    heavy = _load_module()
    strategy = module.build_strategy(max_parallel_agents=4)

    assert strategy["schemaVersion"] == "real-release-multi-agent-strategy-v1"
    assert strategy["status"] == "PASS"
    assert strategy["feasibility"] == "FEASIBLE_WITH_FINAL_SERIAL_GATE"
    assert strategy["recommendedParallelAgents"] == 4
    assert strategy["validation"]["failures"] == []
    assert strategy["finalGateCaseCount"] == heavy.TARGET_TOTAL_CHECKS
    assert strategy["finalGateNewCaseCount"] == heavy.TARGET_NEW_CHECKS

    lanes = {lane["id"]: lane for lane in strategy["lanes"]}
    assert lanes["coordinator-final-real-release-gate"]["command"] == "python scripts/真实发布校验.py"
    assert lanes["agent-g-concurrency-pressure"]["parallelSafe"] is False
    assert lanes["agent-h-v027-integrated-capabilities"]["parallelSafe"] is False
    assert lanes["agent-f-multi-model-image-route"]["wave"] < lanes["agent-g-concurrency-pressure"]["wave"]
    assert "v027-integrated-capabilities" in lanes["coordinator-final-real-release-gate"]["groups"]
    assert "gpt-image-2-pro native route" in " ".join(lanes["agent-f-multi-model-image-route"]["evidenceRequired"])


def test_failed_gate_rerun_strategy_targets_failed_groups_before_final_gate():
    module = _load_rerun_module()
    report = {
        "status": "FAIL",
        "checks": [
            {
                "caseId": "unit-stream-001",
                "group": "stream-state-machine",
                "name": "stream done missing",
                "status": "FAIL",
                "priority": "P0",
                "hardGate": True,
            },
            {
                "caseId": "unit-route-001",
                "group": "multi-model-image-route",
                "name": "image route drifted",
                "status": "FAIL",
                "priority": "P0",
                "hardGate": True,
            },
        ],
    }

    strategy = module.build_strategy(report=report)
    commands = [item["command"] for item in strategy["commands"]]

    assert strategy["schemaVersion"] == "real-release-rerun-strategy-v1"
    assert strategy["status"] == "PASS"
    assert strategy["action"] == "FOCUSED_RERUN_THEN_FINAL_GATE"
    assert strategy["runFullGateImmediatelyAfterEachFix"] is False
    assert strategy["mustRunFullGateBeforePromotion"] is True
    assert strategy["reportPath"] == "docs/v0.2.7/artifacts/production-agent-product-acceptance.json"
    assert str(ROOT) not in strategy["reportPath"]
    assert strategy["needsImageRouteRerun"] is True
    assert {"fresh-env", "auth-first-use", "stream-state-machine", "multi-model-image-route"}.issubset(set(strategy["selectedGroups"]))
    assert any("--focus-groups" in command for command in commands)
    assert commands[-1] == "python scripts/真实发布校验.py"
