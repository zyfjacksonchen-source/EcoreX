#!/usr/bin/env python3
"""Build a non-promoting final gate audit for the EcoreX v0.2.4 long goal.

The audit reads the v0.2.4 acceptance checklist, review log, and release
evidence artifacts. It deliberately does not promote pending slices. A
complete result requires the real EcoreX-vs-Codex image-generation timing
benchmark and final multi-agent release consensus to be PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "v0.2.4"
ARTIFACTS = DOCS / "artifacts"
ACCEPTANCE = DOCS / "acceptance-checklist.md"
REVIEW_LOG = DOCS / "review-log.md"
DEFAULT_OUTPUT = ARTIFACTS / "final-release-gate-preflight.json"

EXPECTED_SLICES: tuple[str, ...] = (
    "R24-00",
    "R24-01",
    "R24-01B",
    "R24-02",
    "R24-02A",
    "R24-03",
    "R24-04",
    "R24-05",
    "R24-06",
    "R24-07",
    "R24-08",
    "R24-09",
    "R24-10",
    "R24-11",
    "R24-12",
    "R24-13",
    "R24-14",
    "R24-14A",
    "R24-14B",
    "R24-15",
)
COMPLETED_PASS_SLICES = tuple(item for item in EXPECTED_SLICES if item not in {"R24-14B", "R24-15"})
REQUIRED_IMAGEGEN_CASE_IDS: tuple[str, ...] = (
    "icon-no-reference",
    "poster-reference-edit",
)
REQUIRED_IMAGEGEN_PROMPT_HASHES: dict[str, str] = {
    "icon-no-reference": "7cc74acddacf4fd6",
    "poster-reference-edit": "f821a39587639d06",
}
REQUIRED_IMAGEGEN_PROMPT_LENGTHS: dict[str, int] = {
    "icon-no-reference": 86,
    "poster-reference-edit": 87,
}
REQUIRED_IMAGEGEN_CASE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "icon-no-reference": {
        "promptLength": 86,
        "referenceImageCount": 0,
        "size": "1024x1024",
        "outputFormat": "png",
        "qualityRetryMax": 1,
    },
    "poster-reference-edit": {
        "promptLength": 87,
        "referenceImageCount": 1,
        "size": "1024x1024",
        "outputFormat": "png",
        "qualityRetryMax": 1,
    },
}
REQUIRED_CODEX_COMPARISON_SCHEMA_VERSION = "r24-14b-codex-timing-v1"
PROVIDER_IDS = {"openai", "linkai", "gemini", "seedream", "qwen", "minimax"}
CODEX_RESULT_TOP_LEVEL_KEYS = {
    "status",
    "redacted",
    "mode",
    "schemaVersion",
    "resultModeRequired",
    "resultStatusRequired",
    "timingSemantics",
    "cases",
}
CODEX_RESULT_CASE_KEYS = {
    "caseId",
    "promptHash",
    "promptLength",
    "referenceImageCount",
    "size",
    "outputFormat",
    "qualityRetryMax",
    "status",
    "finalUsableMs",
    "wallMs",
}
REAL_BENCHMARK_TOP_LEVEL_KEYS = {
    "status",
    "redacted",
    "mode",
    "provider",
    "realProviderReady",
    "timingSemantics",
    "codexComparison",
    "cases",
    "failedCases",
}
REAL_BENCHMARK_READY_KEYS = PROVIDER_IDS
REAL_BENCHMARK_TIMING_SEMANTIC_KEYS = {
    "providerLatencyMs",
    "providerRunnerOverheadMs",
    "ecorexControllableOverheadMs",
}
REAL_BENCHMARK_CODEX_SUMMARY_KEYS = {
    "available",
    "status",
    "schemaVersion",
    "caseCount",
    "sourceSha256",
    "validatedBy",
}
REAL_BENCHMARK_CASE_KEYS = {
    "caseId",
    "promptHash",
    "promptLength",
    "referenceImageCount",
    "size",
    "outputFormat",
    "qualityRetryMax",
    "provider",
    "ecorexDirect",
    "ecorexJob",
    "comparison",
}
REAL_BENCHMARK_DIRECT_KEYS = {
    "status",
    "wallMs",
    "finalUsableMs",
    "providerTotalLatencyMs",
    "qualityTotalLatencyMs",
    "finalizationTotalLatencyMs",
    "postprocessTotalLatencyMs",
    "attemptCount",
    "retryCount",
    "finalizationStatus",
    "imageCount",
}
REAL_BENCHMARK_JOB_KEYS = {
    "status",
    "finalUsableMs",
    "providerTotalMs",
    "qualityTotalMs",
    "finalizationTotalMs",
    "postprocessTotalMs",
    "taskStatus",
    "artifactCount",
    "retryCount",
}
REAL_BENCHMARK_COMPARISON_KEYS = {
    "available",
    "codexFinalUsableMs",
    "ecorexFinalUsableMs",
    "deltaPct",
}

ARTIFACT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "release-contract",
        "slice": "R24-14B",
        "path": ARTIFACTS / "r24-14b-release-contract-check.json",
        "expectedStatus": "PASS",
        "requireChecksPass": True,
    },
    {
        "id": "imagegen-fake-overhead",
        "slice": "R24-14B",
        "path": ARTIFACTS / "imagegen-efficiency-benchmark-fake.json",
        "expectedStatus": "PASS",
        "validator": "imagegen-fake",
    },
    {
        "id": "imagegen-real-preflight",
        "slice": "R24-14B",
        "path": ARTIFACTS / "imagegen-efficiency-real-preflight.json",
        "expectedStatus": "BLOCKED",
        "allowedStatuses": {"BLOCKED", "PASS"},
    },
    {
        "id": "imagegen-real-benchmark",
        "slice": "R24-14B",
        "path": ARTIFACTS / "imagegen-efficiency-real-benchmark.json",
        "fallbackPaths": (ARTIFACTS / "imagegen-efficiency-real-blocked.json",),
        "expectedStatus": "PASS",
        "validator": "imagegen-real",
    },
    {
        "id": "imagegen-codex-result",
        "slice": "R24-14B",
        "path": ARTIFACTS / "imagegen-efficiency-codex-result.json",
        "expectedStatus": "PASS",
        "validator": "codex-result",
    },
    {
        "id": "imagegen-benchmark-privacy",
        "slice": "R24-14B",
        "path": ARTIFACTS / "imagegen-efficiency-benchmark-privacy.json",
        "expectedStatus": "success",
        "validator": "imagegen-benchmark-privacy",
    },
    {
        "id": "visual-analysis-speed",
        "slice": "R24-14",
        "path": ARTIFACTS / "image-visual-analysis-speed-optimized.json",
        "expectedStatus": "PASS",
        "validator": "visual-speed",
    },
    {
        "id": "streaming-markdown",
        "slice": "R24-14A",
        "path": ARTIFACTS / "streaming-markdown-browser-smoke.json",
        "expectedStatus": "PASS",
        "validator": "streaming-markdown",
    },
    {
        "id": "streaming-markdown-privacy",
        "slice": "R24-14A",
        "path": ARTIFACTS / "streaming-markdown-privacy.json",
        "expectedStatus": "success",
        "validator": "privacy",
    },
)


class FinalGateAuditError(RuntimeError):
    """Raised when a required source cannot be parsed."""


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FinalGateAuditError(f"required file missing: {_display_path(path)}")
    return path.read_text(encoding="utf-8-sig")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FinalGateAuditError(f"required artifact missing: {_display_path(path)}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise FinalGateAuditError(f"artifact is not an object: {_display_path(path)}")
    return data


def _parse_status_table(path: Path, *, status_index: int, evidence_index: int | None = None) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in _read_text(path).splitlines():
        raw = line.strip()
        if not raw.startswith("| R24-"):
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        if len(cells) <= status_index:
            continue
        item_id = cells[0]
        row = {"status": cells[status_index]}
        if evidence_index is not None and len(cells) > evidence_index:
            row["evidence"] = cells[evidence_index]
        rows[item_id] = row
    return rows


def _load_acceptance_rows(path: Path = ACCEPTANCE) -> dict[str, dict[str, str]]:
    return _parse_status_table(path, status_index=2, evidence_index=3)


def _load_review_rows(path: Path = REVIEW_LOG) -> dict[str, dict[str, str]]:
    return _parse_status_table(path, status_index=1)


def _is_final_pass(status: Any) -> bool:
    return str(status or "").strip().upper() == "PASS"


def _artifact_payload(spec: dict[str, Any], overrides: dict[str, dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str]:
    artifact_id = str(spec["id"])
    if overrides and artifact_id in overrides:
        return overrides[artifact_id], "override"
    paths = [spec["path"], *(spec.get("fallbackPaths") or [])]
    first_path = paths[0]
    for path in paths:
        if path.is_file():
            try:
                return _load_json(path), _display_path(path)
            except FinalGateAuditError:
                return None, _display_path(path)
    return None, _display_path(first_path)


def _checks_all_pass(payload: dict[str, Any]) -> bool:
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return True
    return all(isinstance(item, dict) and str(item.get("status") or "").upper() == "PASS" for item in checks)


def _privacy_clean(payload: dict[str, Any]) -> bool:
    if int(payload.get("findingCount") or 0) != 0:
        return False
    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and int(metrics.get("findingBucketCount") or 0) != 0:
        return False
    return bool(payload.get("redacted", True))


def _imagegen_benchmark_privacy_clean(payload: dict[str, Any], *, min_files_scanned: int) -> bool:
    if _privacy_clean(payload) is not True:
        return False
    try:
        files_scanned = int(payload.get("filesScanned") or 0)
    except (TypeError, ValueError):
        return False
    return files_scanned >= min_files_scanned


def _imagegen_fake_clean(payload: dict[str, Any]) -> bool:
    cases = payload.get("cases")
    comparison = payload.get("codexComparison")
    return (
        payload.get("mode") == "fake-provider-overhead"
        and isinstance(cases, list)
        and _imagegen_case_id_state(cases)["ok"] is True
        and _imagegen_prompt_hashes_clean(cases) is True
        and _imagegen_case_requirements_clean(cases) is True
        and not payload.get("failedCases")
        and isinstance(comparison, dict)
        and comparison.get("available") is False
        and bool(payload.get("redacted"))
    )


def _imagegen_case_id_state(cases: Any) -> dict[str, Any]:
    expected = set(REQUIRED_IMAGEGEN_CASE_IDS)
    if not isinstance(cases, list) or not cases:
        return {
            "ok": False,
            "missingCaseIds": sorted(expected),
            "unknownCaseIds": [],
            "duplicateCaseIds": [],
            "caseCount": 0,
            "expectedCaseCount": len(expected),
        }
    raw_case_ids = [
        str(item.get("caseId") or "")
        for item in cases
        if isinstance(item, dict) and item.get("caseId")
    ]
    duplicate_case_ids = sorted({case_id for case_id in raw_case_ids if raw_case_ids.count(case_id) > 1})
    actual = set(raw_case_ids)
    missing_case_ids = sorted(expected - actual)
    unknown_case_ids = sorted(actual - expected)
    return {
        "ok": not missing_case_ids and not unknown_case_ids and not duplicate_case_ids and len(raw_case_ids) == len(expected),
        "missingCaseIds": missing_case_ids,
        "unknownCaseIds": unknown_case_ids,
        "duplicateCaseIds": duplicate_case_ids,
        "caseCount": len(raw_case_ids),
        "expectedCaseCount": len(expected),
    }


def _imagegen_prompt_hashes_clean(cases: Any) -> bool:
    if not isinstance(cases, list):
        return False
    for item in cases:
        if not isinstance(item, dict):
            return False
        case_id = str(item.get("caseId") or "")
        expected_hash = REQUIRED_IMAGEGEN_PROMPT_HASHES.get(case_id)
        if not expected_hash or str(item.get("promptHash") or "") != expected_hash:
            return False
    return True


def _imagegen_case_requirements_clean(cases: Any) -> bool:
    if not isinstance(cases, list):
        return False
    for item in cases:
        if not isinstance(item, dict):
            return False
        case_id = str(item.get("caseId") or "")
        expected = REQUIRED_IMAGEGEN_CASE_REQUIREMENTS.get(case_id)
        if not expected:
            return False
        try:
            prompt_length = int(item.get("promptLength"))
            reference_count = int(item.get("referenceImageCount"))
            retry_max = int(item.get("qualityRetryMax"))
        except (TypeError, ValueError):
            return False
        if prompt_length != expected["promptLength"]:
            return False
        if reference_count != expected["referenceImageCount"]:
            return False
        if retry_max != expected["qualityRetryMax"]:
            return False
        if str(item.get("size") or "") != expected["size"]:
            return False
        if str(item.get("outputFormat") or "") != expected["outputFormat"]:
            return False
    return True


def _codex_result_clean(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if set(payload) - CODEX_RESULT_TOP_LEVEL_KEYS:
        return False
    if str(payload.get("status") or "").upper() != "PASS":
        return False
    if payload.get("mode") != "codex-imagegen-timing-result":
        return False
    if payload.get("schemaVersion") != REQUIRED_CODEX_COMPARISON_SCHEMA_VERSION:
        return False
    if payload.get("redacted") is not True:
        return False
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return False
    if any(not isinstance(item, dict) or set(item) - CODEX_RESULT_CASE_KEYS for item in cases):
        return False
    if _imagegen_case_id_state(cases)["ok"] is not True:
        return False
    if _imagegen_prompt_hashes_clean(cases) is not True:
        return False
    if _imagegen_case_requirements_clean(cases) is not True:
        return False
    return all(
        isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() == "pass"
        and _positive_int(item.get("finalUsableMs"))
        for item in cases
    )


def _canonical_json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _codex_cases_by_id(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not _codex_result_clean(payload):
        return {}
    return {
        str(item.get("caseId") or ""): item
        for item in payload.get("cases", [])
        if isinstance(item, dict) and item.get("caseId")
    }


def _imagegen_real_ready(payload: dict[str, Any], codex_payload: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    cases = payload.get("cases")
    comparison = payload.get("codexComparison")
    if payload.get("status") != "PASS" or payload.get("mode") != "real-provider-benchmark":
        blockers.append("r24-14b-real-ecorex-provider-timing-missing")
    elif _real_benchmark_shape_clean(payload) is not True:
        blockers.append("r24-14b-real-benchmark-shape-not-clean")
    if not isinstance(comparison, dict) or comparison.get("available") is not True:
        blockers.append("r24-14b-codex-same-prompt-comparison-missing")
    elif _codex_comparison_evidence_clean(comparison, codex_payload) is not True:
        blockers.append("r24-14b-codex-comparison-evidence-not-clean")
    if not isinstance(cases, list) or not cases:
        blockers.append("r24-14b-real-case-results-missing")
    else:
        case_state = _imagegen_case_id_state(cases)
        if case_state["ok"] is not True:
            blockers.append("r24-14b-real-case-set-mismatch")
        if _imagegen_prompt_hashes_clean(cases) is not True:
            blockers.append("r24-14b-real-case-prompt-hash-mismatch")
        if _imagegen_case_requirements_clean(cases) is not True:
            blockers.append("r24-14b-real-case-requirement-mismatch")
        codex_cases = _codex_cases_by_id(codex_payload)
        if not all(_real_case_clean(item, codex_cases=codex_cases) for item in cases):
            blockers.append("r24-14b-real-case-results-not-clean")
    if payload.get("failedCases"):
        blockers.append("r24-14b-real-benchmark-failed-cases")
    return not blockers, blockers


def _real_benchmark_shape_clean(payload: dict[str, Any]) -> bool:
    if set(payload) - REAL_BENCHMARK_TOP_LEVEL_KEYS:
        return False
    if payload.get("redacted") is not True:
        return False
    if str(payload.get("provider") or "") not in PROVIDER_IDS:
        return False
    ready = payload.get("realProviderReady")
    if not isinstance(ready, dict) or set(ready) - REAL_BENCHMARK_READY_KEYS:
        return False
    if not all(isinstance(value, bool) for value in ready.values()):
        return False
    timing_semantics = payload.get("timingSemantics")
    if not isinstance(timing_semantics, dict) or set(timing_semantics) - REAL_BENCHMARK_TIMING_SEMANTIC_KEYS:
        return False
    comparison = payload.get("codexComparison")
    if not isinstance(comparison, dict) or set(comparison) - REAL_BENCHMARK_CODEX_SUMMARY_KEYS:
        return False
    failed_cases = payload.get("failedCases")
    if not isinstance(failed_cases, list):
        return False
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return False
    for item in cases:
        if not isinstance(item, dict) or set(item) - REAL_BENCHMARK_CASE_KEYS:
            return False
        if str(item.get("provider") or "") not in PROVIDER_IDS:
            return False
        direct = item.get("ecorexDirect")
        job = item.get("ecorexJob")
        case_comparison = item.get("comparison")
        if not isinstance(direct, dict) or set(direct) - REAL_BENCHMARK_DIRECT_KEYS:
            return False
        if not isinstance(job, dict) or set(job) - REAL_BENCHMARK_JOB_KEYS:
            return False
        if not isinstance(case_comparison, dict) or set(case_comparison) - REAL_BENCHMARK_COMPARISON_KEYS:
            return False
    return True


def _codex_comparison_evidence_clean(comparison: dict[str, Any], codex_payload: dict[str, Any] | None) -> bool:
    if set(comparison) - REAL_BENCHMARK_CODEX_SUMMARY_KEYS:
        return False
    if comparison.get("status") != "ready":
        return False
    if _codex_result_clean(codex_payload) is not True:
        return False
    if comparison.get("schemaVersion") != REQUIRED_CODEX_COMPARISON_SCHEMA_VERSION:
        return False
    try:
        case_count = int(comparison.get("caseCount"))
    except (TypeError, ValueError):
        return False
    if case_count != len(REQUIRED_IMAGEGEN_CASE_IDS):
        return False
    source_sha = str(comparison.get("sourceSha256") or "")
    if len(source_sha) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in source_sha):
        return False
    if comparison.get("validatedBy") != "ecorex-v024-imagegen-efficiency-loader":
        return False
    if source_sha.lower() != _canonical_json_sha256(codex_payload).lower():
        return False
    return True


def _real_case_clean(item: Any, *, codex_cases: dict[str, dict[str, Any]] | None = None) -> bool:
    if not isinstance(item, dict):
        return False
    case_id = str(item.get("caseId") or "")
    if str(item.get("promptHash") or "") != REQUIRED_IMAGEGEN_PROMPT_HASHES.get(case_id):
        return False
    if _imagegen_case_requirements_clean([item]) is not True:
        return False
    comparison = item.get("comparison")
    if not isinstance(comparison, dict) or comparison.get("available") is not True:
        return False
    if codex_cases is not None:
        codex_case = codex_cases.get(case_id)
        if not isinstance(codex_case, dict):
            return False
        try:
            expected_codex_ms = int(codex_case.get("finalUsableMs") or 0)
        except (TypeError, ValueError):
            return False
        if int(comparison.get("codexFinalUsableMs") or 0) != expected_codex_ms:
            return False
    direct = item.get("ecorexDirect")
    if not isinstance(direct, dict):
        return False
    try:
        comparison_ecorex_ms = int(comparison.get("ecorexFinalUsableMs") or 0)
        direct_ms = int(direct.get("finalUsableMs") or 0)
    except (TypeError, ValueError):
        return False
    if comparison_ecorex_ms != direct_ms:
        return False
    for key in ("codexFinalUsableMs", "ecorexFinalUsableMs"):
        if not _positive_int(comparison.get(key)):
            return False
    for key in ("ecorexDirect", "ecorexJob"):
        section = item.get(key)
        if not isinstance(section, dict) or section.get("status") != "pass":
            return False
        if not _positive_int(section.get("finalUsableMs")):
            return False
    return True


def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _visual_speed_clean(payload: dict[str, Any]) -> bool:
    comparison = payload.get("baselineComparison")
    quality = payload.get("qualityExpectations")
    return (
        isinstance(comparison, dict)
        and comparison.get("available") is True
        and comparison.get("compatible") is True
        and comparison.get("performanceAcceptable") is True
        and isinstance(quality, dict)
        and quality.get("ok") is True
        and bool(payload.get("redacted"))
    )


def _streaming_markdown_clean(payload: dict[str, Any]) -> bool:
    performance = payload.get("performance")
    samples = payload.get("samples")
    return (
        int(payload.get("consoleErrorCount") or 0) == 0
        and isinstance(performance, dict)
        and int(performance.get("sampleCount") or 0) >= 1
        and isinstance(samples, list)
        and bool(samples)
        and not any(isinstance(item, dict) and item.get("hasOmittedStreamingText") for item in samples)
        and bool(payload.get("redacted"))
    )


def _validator_clean(validator: str | None, payload: dict[str, Any]) -> bool:
    if validator == "privacy":
        return _privacy_clean(payload)
    if validator == "imagegen-fake":
        return _imagegen_fake_clean(payload)
    if validator == "codex-result":
        return _codex_result_clean(payload)
    if validator == "visual-speed":
        return _visual_speed_clean(payload)
    if validator == "streaming-markdown":
        return _streaming_markdown_clean(payload)
    return True


def _evaluate_artifacts(overrides: dict[str, dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    artifact_states: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    real_blockers: list[str] = []
    codex_payload, _codex_source = _artifact_payload({"id": "imagegen-codex-result", "path": ARTIFACTS / "imagegen-efficiency-codex-result.json"}, overrides)
    real_payload, _real_source = _artifact_payload(
        {
            "id": "imagegen-real-benchmark",
            "path": ARTIFACTS / "imagegen-efficiency-real-benchmark.json",
            "fallbackPaths": (ARTIFACTS / "imagegen-efficiency-real-blocked.json",),
        },
        overrides,
    )
    for spec in ARTIFACT_SPECS:
        payload, source = _artifact_payload(spec, overrides)
        actual_status = "missing" if payload is None else str(payload.get("status") or "")
        allowed_statuses = {str(value) for value in (spec.get("allowedStatuses") or {spec.get("expectedStatus")})}
        status_ok = payload is not None and actual_status in allowed_statuses
        checks_ok = payload is not None and (not spec.get("requireChecksPass") or _checks_all_pass(payload))
        validator_ok = payload is not None and _validator_clean(spec.get("validator"), payload)
        if spec.get("validator") == "imagegen-benchmark-privacy" and payload is not None:
            min_files_scanned = 4
            if _codex_result_clean(codex_payload) and isinstance(real_payload, dict) and real_payload.get("status") == "PASS":
                min_files_scanned = 6
            validator_ok = _imagegen_benchmark_privacy_clean(payload, min_files_scanned=min_files_scanned)
        if spec.get("validator") == "imagegen-real" and payload is not None:
            real_ok, real_blockers = _imagegen_real_ready(payload, codex_payload=codex_payload)
            status_ok = real_ok
            validator_ok = real_ok
        clean = bool(status_ok and checks_ok and validator_ok)
        artifact_states.append(
            {
                "id": spec["id"],
                "slice": spec["slice"],
                "source": source,
                "status": actual_status,
                "expectedStatus": spec.get("expectedStatus"),
                "allowedStatuses": sorted(allowed_statuses),
                "clean": clean,
            }
        )
        if not clean:
            blockers.append(
                {
                    "id": f"artifact-not-clean-{spec['id']}",
                    "slice": spec["slice"],
                    "status": actual_status,
                }
            )
    return artifact_states, blockers, real_blockers


def build_audit(
    *,
    acceptance_rows: dict[str, dict[str, str]] | None = None,
    review_rows: dict[str, dict[str, str]] | None = None,
    artifact_payloads: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    acceptance_rows = acceptance_rows if acceptance_rows is not None else _load_acceptance_rows()
    review_rows = review_rows if review_rows is not None else _load_review_rows()
    blockers: list[dict[str, Any]] = []
    slice_states: list[dict[str, Any]] = []

    for item_id in EXPECTED_SLICES:
        acceptance = acceptance_rows.get(item_id)
        review = review_rows.get(item_id)
        acceptance_status = "missing" if acceptance is None else acceptance.get("status", "")
        review_status = "missing" if review is None else review.get("status", "")
        required_now = item_id in COMPLETED_PASS_SLICES
        final_required = item_id in {"R24-14B", "R24-15"}
        acceptance_ok = _is_final_pass(acceptance_status)
        review_ok = _is_final_pass(review_status)
        slice_states.append(
            {
                "id": item_id,
                "acceptanceStatus": acceptance_status,
                "reviewStatus": review_status,
                "requiredNow": required_now,
                "finalRequired": final_required,
                "ok": acceptance_ok and review_ok,
            }
        )
        if required_now:
            if not acceptance_ok:
                blockers.append({"id": f"{item_id.lower()}-acceptance-not-pass", "slice": item_id, "status": acceptance_status})
            if not review_ok:
                blockers.append({"id": f"{item_id.lower()}-review-not-pass", "slice": item_id, "status": review_status})
        elif item_id == "R24-14B":
            if not acceptance_ok:
                blockers.append({"id": "r24-14b-final-pass-not-earned", "slice": item_id, "status": acceptance_status})
            if not review_ok:
                blockers.append({"id": "r24-14b-final-review-not-pass", "slice": item_id, "status": review_status})
        elif item_id == "R24-15":
            if not acceptance_ok or not review_ok:
                blockers.append({"id": "r24-15-final-consensus-not-pass", "slice": item_id, "status": f"{acceptance_status}/{review_status}"})

    artifact_states, artifact_blockers, real_blocker_ids = _evaluate_artifacts(artifact_payloads)
    blockers.extend(artifact_blockers)
    for blocker_id in real_blocker_ids:
        blockers.append({"id": blocker_id, "slice": "R24-14B", "status": "pending"})

    blocker_keys: set[tuple[str, str]] = set()
    deduped_blockers: list[dict[str, Any]] = []
    for blocker in blockers:
        key = (str(blocker.get("id") or ""), str(blocker.get("slice") or ""))
        if key in blocker_keys:
            continue
        blocker_keys.add(key)
        deduped_blockers.append(blocker)

    remaining_slices = sorted({str(item.get("slice")) for item in deduped_blockers if item.get("slice")})
    status = "PASS" if not deduped_blockers else "PENDING"
    return {
        "version": "0.2.4",
        "scenario": "v024-final-release-gate-preflight",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "complete": status == "PASS",
        "redacted": True,
        "scope": "webui-dual-end",
        "remainingSlices": remaining_slices,
        "metrics": {
            "sliceCount": len(slice_states),
            "artifactCount": len(artifact_states),
            "blockerCount": len(deduped_blockers),
            "completedPassSliceCount": sum(1 for item in slice_states if item["id"] in COMPLETED_PASS_SLICES and item["ok"]),
        },
        "sliceStates": slice_states,
        "artifactStates": artifact_states,
        "blockers": deduped_blockers,
        "nextRequiredEvidence": [
            "Run the real EcoreX image provider benchmark with configured provider credentials.",
            "Produce docs/v0.2.4/artifacts/imagegen-efficiency-codex-result.json from the same-prompt Codex imagegen timing run and merge it by caseId.",
            "Apply any data-driven EcoreX controllable-overhead optimization that the real comparison exposes.",
            "Run final R24-15 multi-agent release consensus after R24-14B is PASS.",
        ] if deduped_blockers else [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit v0.2.4 final release gate readiness")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit = build_audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(audit, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if args.require_complete and not audit.get("complete"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
