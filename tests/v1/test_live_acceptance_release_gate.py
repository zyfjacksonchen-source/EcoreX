from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

from ecorex.control_plane.repository import REQUIRED_RELEASE_GATES
from ecorex.managed_model_policy import ECOREX_CHAT_MODEL_POLICY
from ecorex.release.live_acceptance import (
    LIVE_ACCEPTANCE_GATES,
    REQUIRED_CDP_SCENARIOS,
    REQUIRED_CDP_VIEWPORTS,
    REQUIRED_LIVE_TOOL_IDS,
    validate_live_acceptance_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
RUN_ID = 7319


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _candidate() -> dict[str, object]:
    return {
        "release_id": "release-canary-" + "b" * 24,
        "version": "1.0.0",
        "channel": "canary",
        "build_digest": _digest("build"),
        "manifest_sha256": _digest("manifest"),
        "web_tree_sha256": _digest("web"),
        "candidate_receipt_sha256": _digest("candidate"),
    }


def _evidence() -> dict[str, object]:
    artifacts = [_digest(f"artifact-{index}") for index in range(4)]
    screenshots = {
        scenario: _digest(f"screenshot-{scenario}")
        for scenario in sorted(REQUIRED_CDP_SCENARIOS)[:8]
    }
    return {
        "schema_version": 1,
        "evidence_type": "ecorex-protected-live-acceptance",
        "status": "passed",
        "commit_sha": COMMIT,
        "workflow_run_id": RUN_ID,
        "runner": {
            "platform": "windows",
            "architecture": "x64",
            "self_hosted": True,
            "protected_environment": "ecorex-live-acceptance",
            "network_mode": "production-managed",
            "browser_protocol": "cdp",
            "acceptance_driver_sha256": _digest("acceptance-driver"),
        },
        "candidate": _candidate(),
        "executions": {
            "live-model": {
                "status": "passed",
                "local_model_id": ECOREX_CHAT_MODEL_POLICY.local_model_id,
                "upstream_model_id": ECOREX_CHAT_MODEL_POLICY.upstream_model_id,
                "reasoning_effort": ECOREX_CHAT_MODEL_POLICY.reasoning_effort,
                "compact_threshold_tokens": (
                    ECOREX_CHAT_MODEL_POLICY.compact_threshold_tokens
                ),
                "terminal_event": "response.completed",
                "request_count": 1,
                "response_sha256": _digest("model-response"),
                "trace_sha256": _digest("model-trace"),
                "provider_receipt_sha256": _digest("model-provider"),
                "duration_milliseconds": 812.5,
            },
            "live-image": {
                "status": "passed",
                "canonical_model_id": "gpt-image-2",
                "selected_tool_id": "imagegen",
                "route_strategy": "ranked-non-exclusive",
                "discovered_tools": sorted(REQUIRED_LIVE_TOOL_IDS),
                "concurrent_requests": 4,
                "completed_requests": 4,
                "unique_artifacts": 4,
                "server_errors": 0,
                "artifact_sha256": artifacts,
                "provider_receipt_sha256": _digest("image-provider"),
                "retouch": {
                    "status": "passed",
                    "base_revision_sha256": _digest("base-revision"),
                    "result_revision_sha256": _digest("result-revision"),
                    "annotation_kind": "rectangle",
                    "target_change_score": 0.31,
                    "unchanged_region_similarity": 0.982,
                    "output_sha256": _digest("retouch-output"),
                },
                "duration_milliseconds": 2_431.0,
            },
            "cdp-acceptance": {
                "status": "passed",
                "browser_engine": "chrome",
                "browser_version": "126.0.0.0",
                "protocol": "cdp",
                "scenarios": sorted(REQUIRED_CDP_SCENARIOS),
                "viewports": sorted(REQUIRED_CDP_VIEWPORTS),
                "assertions": 74,
                "console_errors": 0,
                "page_errors": 0,
                "failed_requests": 0,
                "screenshot_sha256": screenshots,
                "duration_milliseconds": 31_842.0,
            },
        },
    }


def _validate(value: object) -> dict[str, dict[str, object]]:
    return validate_live_acceptance_evidence(
        value,
        expected_commit=COMMIT,
        expected_workflow_run_id=RUN_ID,
        expected_candidate=_candidate(),
    )


def _module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_live_acceptance_is_a_fixed_control_plane_publication_contract() -> None:
    assert LIVE_ACCEPTANCE_GATES <= REQUIRED_RELEASE_GATES
    writer = _module(
        "ecorex_live_acceptance_gate_writer",
        "scripts/write-v1-gate-receipts.py",
    )
    assert LIVE_ACCEPTANCE_GATES <= writer._BOUND_GATES


def test_valid_live_acceptance_proves_model_image_retouch_and_cdp() -> None:
    evidence = _evidence()
    executions = _validate(evidence)
    assert set(executions) == LIVE_ACCEPTANCE_GATES
    assert executions["live-model"]["upstream_model_id"] == "gpt-5.6-sol"
    assert executions["live-image"]["canonical_model_id"] == "gpt-image-2"
    assert executions["live-image"]["completed_requests"] == 4
    assert executions["cdp-acceptance"]["protocol"] == "cdp"
    binder = _module(
        "ecorex_live_acceptance_gate_binder",
        "scripts/bind-v1-release-gate-evidence.py",
    )
    for gate in LIVE_ACCEPTANCE_GATES:
        assert binder._validate_source(
            gate,
            evidence,
            commit=COMMIT,
            workflow_run_id=RUN_ID,
            expected_candidate=_candidate(),
        ) == executions[gate]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda value: value["executions"]["live-model"].__setitem__(
                "upstream_model_id", "gpt-5.5"
            ),
            "live_model_execution_invalid",
        ),
        (
            lambda value: value["executions"]["live-image"].__setitem__(
                "discovered_tools", ["imagegen"]
            ),
            "live_image_execution_invalid",
        ),
        (
            lambda value: value["executions"]["live-image"][
                "discovered_tools"
            ].append(7),
            "live_image_execution_invalid",
        ),
        (
            lambda value: value["executions"]["live-image"].__setitem__(
                "server_errors", False
            ),
            "live_image_execution_invalid",
        ),
        (
            lambda value: value["executions"]["live-image"][
                "artifact_sha256"
            ].__setitem__(0, {"not": "a digest"}),
            "live_image_execution_invalid",
        ),
        (
            lambda value: value["executions"]["live-image"]["retouch"].__setitem__(
                "result_revision_sha256",
                value["executions"]["live-image"]["retouch"][
                    "base_revision_sha256"
                ],
            ),
            "live_image_retouch_invalid",
        ),
        (
            lambda value: value["executions"]["cdp-acceptance"]["scenarios"].pop(),
            "cdp_acceptance_execution_invalid",
        ),
        (
            lambda value: value["candidate"].__setitem__(
                "build_digest", _digest("other-build")
            ),
            "live_acceptance_candidate_mismatch",
        ),
    ],
)
def test_live_acceptance_fails_closed_on_partial_or_drifted_evidence(
    mutation,
    code: str,
) -> None:
    evidence = deepcopy(_evidence())
    mutation(evidence)
    with pytest.raises(ValueError, match=code):
        _validate(evidence)


def test_candidate_workflow_cannot_publish_the_pre_acceptance_artifact() -> None:
    source = (ROOT / ".github/workflows/ecorex-v1-candidate.yml").read_text(
        encoding="utf-8"
    )
    live_job = source.index("  live-acceptance:")
    accepted = source[live_job:]
    assert "needs: build-and-sign" in accepted
    assert "name: ecorex-live-acceptance" in accepted
    assert "run-v1-protected-live-acceptance.py" in accepted
    assert accepted.count("bind-v1-release-gate-evidence.py") == 3
    assert "name: ecorex-v1-accepted-${{ inputs.channel }}" in accepted
    assert "publish-assets" not in source

    publication = (
        ROOT / ".github/workflows/ecorex-v1-promote-candidate.yml"
    ).read_text(encoding="utf-8")
    assert "candidate_run_id:" in publication
    assert "candidate_artifact_id:" in publication
    assert "scripts/select-v1-accepted-candidate.py" in publication
    assert "actions/artifacts/${CANDIDATE_ARTIFACT_ID}/zip" in publication
    assert "scripts/extract-v1-workflow-artifact.py" in publication
    assert publication.index("verify-v1-accepted-candidate.py") < publication.index(
        "publish-assets"
    )


def test_acceptance_driver_cannot_run_before_candidate_authentication() -> None:
    source = (
        ROOT / "scripts/run-v1-protected-live-acceptance.py"
    ).read_text(encoding="utf-8")
    run_body = source[source.index("def run(") :]
    assert run_body.index("expected_candidate = _candidate_identity(") < run_body.index(
        "result = run_bounded_process("
    )
    assert "authenticate_candidate(" in source
    assert "--trusted-public-key" in source
    assert "--staging-provenance" in source
    assert "--expected-staging-run-id" in source


def test_acceptance_wrapper_redacts_uncontrolled_failure_text() -> None:
    wrapper = _module(
        "ecorex_v1_live_acceptance_wrapper",
        "scripts/run-v1-protected-live-acceptance.py",
    )
    assert wrapper._safe_error_code(
        OSError(r"cannot open C:\\Users\\runner\\credential.json")
    ) == "OSError"
    assert wrapper._safe_error_code(
        ValueError("live_acceptance_candidate_receipt_invalid")
    ) == "live_acceptance_candidate_receipt_invalid"
    assert wrapper._safe_error_code(ValueError("secret/provider/path")) == "ValueError"
