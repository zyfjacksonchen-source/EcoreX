import contextlib
import hashlib
import io
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _canonical_json_sha256(value: dict) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _load_audit_module():
    script = ROOT / "scripts" / "audit-v024-final-release-gate.py"
    spec = importlib.util.spec_from_file_location("audit_v024_final_release_gate_for_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _codex_result_payload() -> dict:
    return {
        "status": "PASS",
        "redacted": True,
        "mode": "codex-imagegen-timing-result",
        "schemaVersion": "r24-14b-codex-timing-v1",
        "cases": [
            {
                "caseId": "icon-no-reference",
                "promptHash": "7cc74acddacf4fd6",
                "promptLength": 86,
                "referenceImageCount": 0,
                "size": "1024x1024",
                "outputFormat": "png",
                "qualityRetryMax": 1,
                "status": "pass",
                "finalUsableMs": 4000,
                "wallMs": 4000,
            },
            {
                "caseId": "poster-reference-edit",
                "promptHash": "f821a39587639d06",
                "promptLength": 87,
                "referenceImageCount": 1,
                "size": "1024x1024",
                "outputFormat": "png",
                "qualityRetryMax": 1,
                "status": "pass",
                "finalUsableMs": 5100,
                "wallMs": 5100,
            },
        ],
    }


def _real_pass_payload(codex_result: dict | None = None) -> dict:
    codex_result = codex_result or _codex_result_payload()
    return {
        "status": "PASS",
        "redacted": True,
        "mode": "real-provider-benchmark",
        "provider": "openai",
        "realProviderReady": {"openai": True},
        "timingSemantics": {
            "providerLatencyMs": "whole provider runner latency",
            "providerRunnerOverheadMs": "provider runner overhead",
            "ecorexControllableOverheadMs": "controllable overhead",
        },
        "codexComparison": {
            "available": True,
            "status": "ready",
            "schemaVersion": "r24-14b-codex-timing-v1",
            "caseCount": 2,
            "sourceSha256": _canonical_json_sha256(codex_result),
            "validatedBy": "ecorex-v024-imagegen-efficiency-loader",
        },
        "cases": [
            {
                "caseId": "icon-no-reference",
                "promptHash": "7cc74acddacf4fd6",
                "promptLength": 86,
                "referenceImageCount": 0,
                "size": "1024x1024",
                "outputFormat": "png",
                "qualityRetryMax": 1,
                "provider": "openai",
                "ecorexDirect": {"status": "pass", "finalUsableMs": 4100},
                "ecorexJob": {"status": "pass", "finalUsableMs": 4175},
                "comparison": {
                    "available": True,
                    "codexFinalUsableMs": 4000,
                    "ecorexFinalUsableMs": 4100,
                    "deltaPct": 2.5,
                },
            },
            {
                "caseId": "poster-reference-edit",
                "promptHash": "f821a39587639d06",
                "promptLength": 87,
                "referenceImageCount": 1,
                "size": "1024x1024",
                "outputFormat": "png",
                "qualityRetryMax": 1,
                "provider": "openai",
                "ecorexDirect": {"status": "pass", "finalUsableMs": 5200},
                "ecorexJob": {"status": "pass", "finalUsableMs": 5300},
                "comparison": {
                    "available": True,
                    "codexFinalUsableMs": 5100,
                    "ecorexFinalUsableMs": 5200,
                    "deltaPct": 1.96,
                },
            },
        ],
        "failedCases": [],
    }


def _future_pass_rows(module):
    acceptance_rows = module._load_acceptance_rows()
    review_rows = module._load_review_rows()
    for slice_id in ("R24-14B", "R24-15"):
        acceptance_rows[slice_id] = {**acceptance_rows[slice_id], "status": "PASS"}
        review_rows[slice_id] = {**review_rows[slice_id], "status": "PASS"}
    return acceptance_rows, review_rows


def _real_preflight_pass_payload() -> dict:
    return {
        "status": "PASS",
        "redacted": True,
        "mode": "real-provider-preflight",
        "ready": True,
        "provider": "openai",
        "realProviderReady": {"openai": True},
    }


def _privacy_payload(files_scanned: int = 6) -> dict:
    return {
        "status": "success",
        "filesScanned": files_scanned,
        "findingCount": 0,
        "imageOcrScannedCount": 0,
        "imageOcrUnavailableCount": 0,
        "imageOcrErrorCount": 0,
        "findings": [],
        "redacted": True,
    }


def test_v024_final_release_gate_current_state_is_pending_not_complete():
    module = _load_audit_module()

    audit = module.build_audit()

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "R24-14B" in audit["remainingSlices"]
    assert "R24-15" in audit["remainingSlices"]
    assert "r24-14b-real-ecorex-provider-timing-missing" in blocker_ids
    assert "r24-14b-codex-same-prompt-comparison-missing" in blocker_ids
    assert "r24-15-final-consensus-not-pass" in blocker_ids
    assert audit["metrics"]["completedPassSliceCount"] == len(module.COMPLETED_PASS_SLICES)


def test_v024_final_release_gate_require_complete_exits_nonzero_while_pending(tmp_path):
    module = _load_audit_module()
    artifact = tmp_path / "final-gate.json"

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        exit_code = module.main(["--output", str(artifact), "--require-complete"])

    payload = json.loads(stdout.getvalue())
    artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload == artifact_payload
    assert payload["status"] == "PENDING"
    assert payload["complete"] is False


def test_v024_final_release_gate_has_future_pass_path_with_real_codex_comparison():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": _real_pass_payload(),
            "imagegen-benchmark-privacy": _privacy_payload(files_scanned=6),
        },
    )

    assert audit["status"] == "PASS"
    assert audit["complete"] is True
    assert audit["blockers"] == []
    real_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-real-benchmark"]
    assert real_artifact["clean"] is True


def test_v024_final_release_gate_rejects_malformed_real_benchmark_case_status():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][0]["ecorexDirect"] = {"status": "fail", "finalUsableMs": 4100}

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_incomplete_real_benchmark_case_set():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"] = malformed["cases"][:1]

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-set-mismatch" in blocker_ids


def test_v024_final_release_gate_rejects_missing_real_benchmark_prompt_hash():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][0].pop("promptHash", None)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-prompt-hash-mismatch" in blocker_ids
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_wrong_real_benchmark_prompt_hash():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][1]["promptHash"] = "badpromptbadhash"

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-prompt-hash-mismatch" in blocker_ids


def test_v024_final_release_gate_rejects_nonpositive_codex_comparison_timing():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][0]["comparison"]["codexFinalUsableMs"] = 0

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_unvalidated_codex_comparison_evidence():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["codexComparison"].pop("sourceSha256", None)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_codex_comparison_not_backed_by_artifact():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["codexComparison"]["sourceSha256"] = "a" * 64

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_codex_result_extra_raw_fields():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    codex_result = _codex_result_payload()
    codex_result["cases"][0]["referencePath"] = "C:\\secret\\reference.png"
    malformed = _real_pass_payload(codex_result)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": codex_result,
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    codex_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-codex-result"]
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert codex_artifact["clean"] is False
    assert "artifact-not-clean-imagegen-codex-result" in blocker_ids
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_real_benchmark_extra_raw_fields():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["rawPrompt"] = "Create a clean square app icon"
    malformed["cases"][0]["referencePath"] = "C:\\secret\\reference.png"
    malformed["cases"][0]["ecorexDirect"]["providerPayload"] = {"path": "C:\\secret\\output.png"}
    malformed["cases"][0]["comparison"]["rawCodexResponse"] = {"imagePath": "C:\\secret\\codex.png"}

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-benchmark-shape-not-clean" in blocker_ids
    assert "artifact-not-clean-imagegen-real-benchmark" in blocker_ids


def test_v024_final_release_gate_rejects_stale_imagegen_privacy_scan_for_future_real_pass():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": _real_pass_payload(),
            "imagegen-benchmark-privacy": _privacy_payload(files_scanned=4),
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    privacy_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-benchmark-privacy"]
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert privacy_artifact["clean"] is False
    assert "artifact-not-clean-imagegen-benchmark-privacy" in blocker_ids


def test_v024_final_release_gate_rejects_codex_case_timing_mismatch():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    codex_result = _codex_result_payload()
    codex_result["cases"][0]["finalUsableMs"] = 4700
    codex_result["cases"][0]["wallMs"] = 4700
    malformed = _real_pass_payload(codex_result)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": codex_result,
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_codex_wall_time_without_final_usable_time():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    codex_result = _codex_result_payload()
    codex_result["cases"][0]["finalUsableMs"] = 0
    codex_result["cases"][0]["wallMs"] = 4000
    malformed = _real_pass_payload(codex_result)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": codex_result,
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    codex_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-codex-result"]
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert codex_artifact["clean"] is False
    assert "artifact-not-clean-imagegen-codex-result" in blocker_ids
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_failed_codex_case_status():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    codex_result = _codex_result_payload()
    codex_result["cases"][0]["status"] = "fail"
    malformed = _real_pass_payload(codex_result)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": codex_result,
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    codex_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-codex-result"]
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert codex_artifact["clean"] is False
    assert "artifact-not-clean-imagegen-codex-result" in blocker_ids
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_codex_prompt_length_mismatch():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    codex_result = _codex_result_payload()
    codex_result["cases"][0]["promptLength"] = 999
    malformed = _real_pass_payload(codex_result)

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": codex_result,
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    codex_artifact = {item["id"]: item for item in audit["artifactStates"]}["imagegen-codex-result"]
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert codex_artifact["clean"] is False
    assert "artifact-not-clean-imagegen-codex-result" in blocker_ids
    assert "r24-14b-codex-comparison-evidence-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_ecorex_comparison_timing_mismatch():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][0]["comparison"]["ecorexFinalUsableMs"] = 9999

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_real_benchmark_requirement_mismatch():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][0]["size"] = "1536x1024"

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-requirement-mismatch" in blocker_ids
    assert "r24-14b-real-case-results-not-clean" in blocker_ids


def test_v024_final_release_gate_rejects_unknown_real_benchmark_case_set():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][1]["caseId"] = "unexpected-case"

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-set-mismatch" in blocker_ids


def test_v024_final_release_gate_rejects_duplicate_real_benchmark_case_set():
    module = _load_audit_module()
    acceptance_rows, review_rows = _future_pass_rows(module)
    malformed = _real_pass_payload()
    malformed["cases"][1]["caseId"] = malformed["cases"][0]["caseId"]

    audit = module.build_audit(
        acceptance_rows=acceptance_rows,
        review_rows=review_rows,
        artifact_payloads={
            "imagegen-real-preflight": _real_preflight_pass_payload(),
            "imagegen-codex-result": _codex_result_payload(),
            "imagegen-real-benchmark": malformed,
        },
    )

    blocker_ids = {item["id"] for item in audit["blockers"]}
    assert audit["status"] == "PENDING"
    assert audit["complete"] is False
    assert "r24-14b-real-case-set-mismatch" in blocker_ids
