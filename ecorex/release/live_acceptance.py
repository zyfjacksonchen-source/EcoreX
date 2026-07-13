"""Strict, redaction-safe evidence contract for protected live acceptance.

The protected Windows acceptance runner is the only environment allowed to
exercise production-managed model and image transports before publication.
It emits metadata and content digests only.  This module deliberately rejects
URLs, paths, prompts, model output and credentials by accepting an exact JSON
shape with bounded identifiers and SHA-256 values.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from ecorex.managed_model_policy import ECOREX_CHAT_MODEL_POLICY


LIVE_ACCEPTANCE_GATES = frozenset(
    {"live-model", "live-image", "cdp-acceptance"}
)
REQUIRED_LIVE_TOOL_IDS = frozenset(
    {"read", "fetch", "vision", "cdp", "shell", "imagegen"}
)
REQUIRED_CDP_SCENARIOS = frozenset(
    {
        "new-composer-centered",
        "normal-composer-bottom",
        "model-before-first-message",
        "model-switch-chat-image",
        "image-intent-routing",
        "tool-progressive-disclosure",
        "steer-queue-replace",
        "reasoning-sticky-replacement",
        "permission-default-full",
        "artifact-hover-actions",
        "image-fit-preview",
        "precise-retouch",
        "share-chat-image",
        "share-role-separation",
        "project-session",
        "office-document-flow",
        "connector-catalog",
        "memory-reset-output-path",
    }
)
REQUIRED_CDP_VIEWPORTS = frozenset(
    {"1440x900", "1024x768", "768x900", "390x844"}
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _exact(value: object, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(code)
    return value


def _sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _positive_number(value: object, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(code)
    return float(value)


def _integer(value: object, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(code)
    return value


def _safe_string_list(
    value: object,
    code: str,
    *,
    minimum: int,
    maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(code)
    if any(
        not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None
        for item in value
    ):
        raise ValueError(code)
    if len(set(value)) != len(value):
        raise ValueError(code)
    return value


def _candidate_identity(
    value: object,
    *,
    expected: Mapping[str, object],
) -> None:
    candidate = _exact(
        value,
        {
            "release_id",
            "version",
            "channel",
            "build_digest",
            "manifest_sha256",
            "web_tree_sha256",
            "candidate_receipt_sha256",
        },
        "live_acceptance_candidate_invalid",
    )
    for key in ("release_id", "version", "channel"):
        if candidate.get(key) != expected.get(key):
            raise ValueError("live_acceptance_candidate_mismatch")
    for key in (
        "build_digest",
        "manifest_sha256",
        "web_tree_sha256",
        "candidate_receipt_sha256",
    ):
        _sha256(candidate.get(key), "live_acceptance_candidate_invalid")
        if candidate.get(key) != expected.get(key):
            raise ValueError("live_acceptance_candidate_mismatch")


def _model(value: object) -> dict[str, Any]:
    execution = _exact(
        value,
        {
            "status",
            "local_model_id",
            "upstream_model_id",
            "reasoning_effort",
            "compact_threshold_tokens",
            "terminal_event",
            "request_count",
            "response_sha256",
            "trace_sha256",
            "provider_receipt_sha256",
            "duration_milliseconds",
        },
        "live_model_execution_invalid",
    )
    policy = ECOREX_CHAT_MODEL_POLICY
    if (
        execution.get("status") != "passed"
        or execution.get("local_model_id") != policy.local_model_id
        or execution.get("upstream_model_id") != policy.upstream_model_id
        or execution.get("reasoning_effort") != policy.reasoning_effort
        or execution.get("compact_threshold_tokens")
        != policy.compact_threshold_tokens
        or execution.get("terminal_event") != "response.completed"
        or isinstance(execution.get("request_count"), bool)
        or not isinstance(execution.get("request_count"), int)
        or execution["request_count"] < 1
    ):
        raise ValueError("live_model_execution_invalid")
    for key in ("response_sha256", "trace_sha256", "provider_receipt_sha256"):
        _sha256(execution.get(key), "live_model_execution_invalid")
    _positive_number(
        execution.get("duration_milliseconds"), "live_model_execution_invalid"
    )
    return dict(execution)


def _retouch(value: object) -> dict[str, Any]:
    retouch = _exact(
        value,
        {
            "status",
            "base_revision_sha256",
            "result_revision_sha256",
            "annotation_kind",
            "target_change_score",
            "unchanged_region_similarity",
            "output_sha256",
        },
        "live_image_retouch_invalid",
    )
    if (
        retouch.get("status") != "passed"
        or retouch.get("annotation_kind") != "rectangle"
    ):
        raise ValueError("live_image_retouch_invalid")
    for key in (
        "base_revision_sha256",
        "result_revision_sha256",
        "output_sha256",
    ):
        _sha256(retouch.get(key), "live_image_retouch_invalid")
    if retouch["base_revision_sha256"] == retouch["result_revision_sha256"]:
        raise ValueError("live_image_retouch_invalid")
    changed = _positive_number(
        retouch.get("target_change_score"), "live_image_retouch_invalid"
    )
    similarity = _positive_number(
        retouch.get("unchanged_region_similarity"), "live_image_retouch_invalid"
    )
    if changed > 1 or not 0.95 <= similarity <= 1:
        raise ValueError("live_image_retouch_invalid")
    return dict(retouch)


def _image(value: object) -> dict[str, Any]:
    execution = _exact(
        value,
        {
            "status",
            "canonical_model_id",
            "selected_tool_id",
            "route_strategy",
            "discovered_tools",
            "concurrent_requests",
            "completed_requests",
            "unique_artifacts",
            "server_errors",
            "artifact_sha256",
            "provider_receipt_sha256",
            "retouch",
            "duration_milliseconds",
        },
        "live_image_execution_invalid",
    )
    tools = _safe_string_list(
        execution.get("discovered_tools"),
        "live_image_execution_invalid",
        minimum=1,
        maximum=64,
    )
    artifacts = execution.get("artifact_sha256")
    concurrent = _integer(
        execution.get("concurrent_requests"),
        "live_image_execution_invalid",
        minimum=4,
    )
    completed = _integer(
        execution.get("completed_requests"),
        "live_image_execution_invalid",
    )
    unique = _integer(
        execution.get("unique_artifacts"),
        "live_image_execution_invalid",
    )
    server_errors = _integer(
        execution.get("server_errors"),
        "live_image_execution_invalid",
    )
    if not isinstance(artifacts, list) or len(artifacts) != concurrent:
        raise ValueError("live_image_execution_invalid")
    for digest in artifacts:
        _sha256(digest, "live_image_execution_invalid")
    if len(set(artifacts)) != concurrent:
        raise ValueError("live_image_execution_invalid")
    if (
        execution.get("status") != "passed"
        or execution.get("canonical_model_id") != "gpt-image-2"
        or execution.get("selected_tool_id") != "imagegen"
        or execution.get("route_strategy") != "ranked-non-exclusive"
        or not REQUIRED_LIVE_TOOL_IDS.issubset(tools)
        or completed != concurrent
        or unique != concurrent
        or server_errors != 0
    ):
        raise ValueError("live_image_execution_invalid")
    _sha256(
        execution.get("provider_receipt_sha256"),
        "live_image_execution_invalid",
    )
    _positive_number(
        execution.get("duration_milliseconds"), "live_image_execution_invalid"
    )
    result = dict(execution)
    result["retouch"] = _retouch(execution.get("retouch"))
    return result


def _cdp(value: object) -> dict[str, Any]:
    execution = _exact(
        value,
        {
            "status",
            "browser_engine",
            "browser_version",
            "protocol",
            "scenarios",
            "viewports",
            "assertions",
            "console_errors",
            "page_errors",
            "failed_requests",
            "screenshot_sha256",
            "duration_milliseconds",
        },
        "cdp_acceptance_execution_invalid",
    )
    scenarios = _safe_string_list(
        execution.get("scenarios"),
        "cdp_acceptance_execution_invalid",
        minimum=len(REQUIRED_CDP_SCENARIOS),
        maximum=len(REQUIRED_CDP_SCENARIOS),
    )
    viewports = _safe_string_list(
        execution.get("viewports"),
        "cdp_acceptance_execution_invalid",
        minimum=len(REQUIRED_CDP_VIEWPORTS),
        maximum=len(REQUIRED_CDP_VIEWPORTS),
    )
    screenshots = execution.get("screenshot_sha256")
    assertions = _integer(
        execution.get("assertions"),
        "cdp_acceptance_execution_invalid",
        minimum=len(REQUIRED_CDP_SCENARIOS),
    )
    diagnostics = tuple(
        _integer(
            execution.get(key),
            "cdp_acceptance_execution_invalid",
        )
        for key in ("console_errors", "page_errors", "failed_requests")
    )
    if (
        execution.get("status") != "passed"
        or execution.get("browser_engine") != "chrome"
        or execution.get("protocol") != "cdp"
        or _SAFE_ID.fullmatch(str(execution.get("browser_version"))) is None
        or set(scenarios) != REQUIRED_CDP_SCENARIOS
        or set(viewports) != REQUIRED_CDP_VIEWPORTS
        or assertions < len(REQUIRED_CDP_SCENARIOS)
        or any(diagnostics)
        or not isinstance(screenshots, dict)
        or not 8 <= len(screenshots) <= len(REQUIRED_CDP_SCENARIOS)
        or not set(screenshots).issubset(REQUIRED_CDP_SCENARIOS)
    ):
        raise ValueError("cdp_acceptance_execution_invalid")
    for digest in screenshots.values():
        _sha256(digest, "cdp_acceptance_execution_invalid")
    _positive_number(
        execution.get("duration_milliseconds"),
        "cdp_acceptance_execution_invalid",
    )
    return dict(execution)


def validate_live_acceptance_evidence(
    value: object,
    *,
    expected_commit: str,
    expected_workflow_run_id: int,
    expected_candidate: Mapping[str, object],
) -> dict[str, dict[str, Any]]:
    """Validate and return the three sanitized execution projections."""

    if (
        _COMMIT.fullmatch(expected_commit) is None
        or isinstance(expected_workflow_run_id, bool)
        or expected_workflow_run_id < 1
    ):
        raise ValueError("live_acceptance_identity_invalid")
    evidence = _exact(
        value,
        {
            "schema_version",
            "evidence_type",
            "status",
            "commit_sha",
            "workflow_run_id",
            "runner",
            "candidate",
            "executions",
        },
        "live_acceptance_evidence_invalid",
    )
    runner = _exact(
        evidence.get("runner"),
        {
            "platform",
            "architecture",
            "self_hosted",
            "protected_environment",
            "network_mode",
            "browser_protocol",
            "acceptance_driver_sha256",
        },
        "live_acceptance_runner_invalid",
    )
    if (
        evidence.get("schema_version") != 1
        or evidence.get("evidence_type")
        != "ecorex-protected-live-acceptance"
        or evidence.get("status") != "passed"
        or evidence.get("commit_sha") != expected_commit
        or evidence.get("workflow_run_id") != expected_workflow_run_id
        or runner.get("platform") != "windows"
        or runner.get("architecture") != "x64"
        or runner.get("self_hosted") is not True
        or runner.get("protected_environment") != "ecorex-live-acceptance"
        or runner.get("network_mode") != "production-managed"
        or runner.get("browser_protocol") != "cdp"
    ):
        raise ValueError("live_acceptance_evidence_invalid")
    _sha256(
        runner.get("acceptance_driver_sha256"),
        "live_acceptance_runner_invalid",
    )
    _candidate_identity(evidence.get("candidate"), expected=expected_candidate)
    executions = _exact(
        evidence.get("executions"),
        set(LIVE_ACCEPTANCE_GATES),
        "live_acceptance_execution_set_incomplete",
    )
    return {
        "live-model": _model(executions["live-model"]),
        "live-image": _image(executions["live-image"]),
        "cdp-acceptance": _cdp(executions["cdp-acceptance"]),
    }


__all__ = [
    "LIVE_ACCEPTANCE_GATES",
    "REQUIRED_CDP_SCENARIOS",
    "REQUIRED_CDP_VIEWPORTS",
    "REQUIRED_LIVE_TOOL_IDS",
    "validate_live_acceptance_evidence",
]
