#!/usr/bin/env python3
"""Bind typed executions to one authenticated manifest and Candidate receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release import candidate_receipt_signing_payload  # noqa: E402
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS  # noqa: E402
from ecorex.release.candidate import STAGE_WORKFLOW_PATH, TARGETS  # noqa: E402
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.update import (  # noqa: E402
    Ed25519SignatureVerifier,
    ReleaseManifest,
    SignatureEnvelope,
    verify_manifest_signature,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^release-(?:canary|stable)-[0-9a-f]{24}$")
_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_IMAGE_PYTEST_NODE_IDS = [
    "tests/v1/test_image_orchestrator_real_shared_storage.py::"
    "test_real_postgres_s3_concurrency_idempotency_recovery_and_gc",
    "tests/v1/test_image_orchestrator_production_storage.py::"
    "test_real_postgres_image_schema_migrate_validate_and_drift_gate",
]
_STAGE_IDS = frozenset(
    f"{kind}-{platform}-{architecture}"
    for platform, architecture in TARGETS
    for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
)
_CANDIDATE_KEYS = {
    "schema_version",
    "receipt_type",
    "status",
    "code",
    "commit_sha",
    "staging_provenance",
    "release_id",
    "version",
    "channel",
    "build_digest",
    "python_dependency_lock_sha256",
    "manifest_sha256",
    "web_tree_sha256",
    "stage_receipts",
    "artifacts",
    "signing",
    "signature",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gate",
        required=True,
        choices=("e2e", "migration-dry-run", "image-shared-storage", "image-soak"),
    )
    parser.add_argument("--source-evidence", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--trusted-public-key", required=True)
    parser.add_argument("--staging-provenance", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--expected-staging-run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read(path: Path, *, maximum: int = _MAX_EVIDENCE_BYTES) -> tuple[dict[str, Any], bytes, str]:
    payload = read_stable_regular_file(
        path, maximum_bytes=maximum, code="release_bound_evidence_invalid"
    )
    value = strict_json_loads(payload, code="release_bound_evidence_invalid")
    if not isinstance(value, dict):
        raise ValueError("release_bound_evidence_invalid")
    return value, payload, hashlib.sha256(payload).hexdigest()


def _trusted_key(encoded: str) -> bytes:
    try:
        value = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise ValueError("trusted_release_public_key_invalid") from None
    if len(value) != 32:
        raise ValueError("trusted_release_public_key_invalid")
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("candidate_build_receipt_invalid") from None


def _manifest(path: Path, public: bytes) -> tuple[ReleaseManifest, str, Ed25519SignatureVerifier]:
    raw, payload, digest = _read(path, maximum=16 * 1024 * 1024)
    try:
        manifest = ReleaseManifest.from_dict(raw)
        verifier = Ed25519SignatureVerifier({manifest.signature.key_id: public})
        verify_manifest_signature(manifest, verifier)
    except Exception:
        raise ValueError("release_manifest_untrusted") from None
    # Parsing the exact raw object, rather than a normalized re-serialization,
    # leaves ``digest`` bound to the immutable bytes published to every origin.
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("release_manifest_untrusted")
    return manifest, digest, verifier


def _candidate(
    value: dict[str, Any],
    *,
    commit: str,
    staging_run_id: int,
    staging_sha256: str,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    verifier: Ed25519SignatureVerifier,
) -> tuple[dict[str, str], str]:
    provenance = value.get("staging_provenance")
    signing = value.get("signing")
    signature = value.get("signature")
    artifacts = value.get("artifacts")
    stage_receipts = value.get("stage_receipts")
    expected_artifacts = {
        artifact.artifact_id: {
            "file_name": artifact.file_name,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for artifact in sorted(manifest.artifacts, key=lambda item: item.artifact_id)
    }
    if (
        set(value) != _CANDIDATE_KEYS
        or value.get("schema_version") != 2
        or value.get("receipt_type") != "ecorex-candidate-build"
        or value.get("status") != "passed"
        or value.get("code") is not None
        or value.get("commit_sha") != commit
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("channel") != manifest.channel.value
        or value.get("build_digest") != manifest.build_digest
        or value.get("manifest_sha256") != manifest_sha256
        or _SHA256.fullmatch(str(value.get("python_dependency_lock_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("web_tree_sha256"))) is None
        or not isinstance(provenance, dict)
        or set(provenance)
        != {"workflow_path", "workflow_run_id", "run_attempt", "receipt_sha256"}
        or provenance.get("workflow_path") != STAGE_WORKFLOW_PATH
        or provenance.get("workflow_run_id") != staging_run_id
        or isinstance(provenance.get("run_attempt"), bool)
        or not isinstance(provenance.get("run_attempt"), int)
        or provenance["run_attempt"] < 1
        or provenance.get("receipt_sha256") != staging_sha256
        or not isinstance(stage_receipts, dict)
        or set(stage_receipts) != _STAGE_IDS
        or any(_SHA256.fullmatch(str(digest)) is None for digest in stage_receipts.values())
        or artifacts != expected_artifacts
        or not isinstance(signing, dict)
        or set(signing)
        != {"algorithm", "key_id", "operation_count", "executable_sha256", "adapter_sha256"}
        or signing.get("algorithm") != "ed25519"
        or signing.get("key_id") != manifest.signature.key_id
        or isinstance(signing.get("operation_count"), bool)
        or not isinstance(signing.get("operation_count"), int)
        or signing["operation_count"] < 1
        or _SHA256.fullmatch(str(signing.get("executable_sha256"))) is None
        or (
            signing.get("adapter_sha256") is not None
            and _SHA256.fullmatch(str(signing.get("adapter_sha256"))) is None
        )
        or not isinstance(signature, dict)
    ):
        raise ValueError("candidate_build_receipt_invalid")
    try:
        envelope = SignatureEnvelope.from_dict(signature)
        if envelope.key_id != manifest.signature.key_id:
            raise ValueError
        verifier.verify(candidate_receipt_signing_payload(value), envelope)
    except Exception:
        raise ValueError("candidate_build_receipt_signature_invalid") from None
    return {str(key): str(value) for key, value in stage_receipts.items()}, str(value["web_tree_sha256"])


def _staging(path: Path, *, commit: str, run_id: int) -> tuple[dict[str, Any], str]:
    value, _payload, digest = _read(path, maximum=2 * 1024 * 1024)
    if (
        set(value)
        != {
            "schema_version",
            "status",
            "workflow_path",
            "workflow_run_id",
            "run_attempt",
            "commit_sha",
            "repository",
            "metadata_sha256",
        }
        or value.get("schema_version") != 1
        or value.get("status") != "passed"
        or value.get("workflow_path") != STAGE_WORKFLOW_PATH
        or value.get("workflow_run_id") != run_id
        or isinstance(value.get("run_attempt"), bool)
        or not isinstance(value.get("run_attempt"), int)
        or value["run_attempt"] < 1
        or value.get("commit_sha") != commit
        or not isinstance(value.get("repository"), str)
        or not value["repository"]
        or _SHA256.fullmatch(str(value.get("metadata_sha256"))) is None
    ):
        raise ValueError("staging_provenance_invalid")
    return value, digest


def _validate_source(
    gate: str, value: dict[str, Any], *, commit: str, workflow_run_id: int
) -> dict[str, Any]:
    if value.get("status") != "passed" or value.get("commit_sha") != commit:
        raise ValueError("gate_source_identity_invalid")
    if value.get("workflow_run_id") != workflow_run_id:
        raise ValueError("gate_source_workflow_invalid")
    if gate in {"e2e", "migration-dry-run"}:
        executions = value.get("executions")
        selected_key = "browser-e2e" if gate == "e2e" else "migration-pytest"
        selected = executions.get(selected_key) if isinstance(executions, dict) else None
        if (
            set(value)
            != {
                "schema_version",
                "evidence_type",
                "status",
                "commit_sha",
                "workflow_run_id",
                "gates",
                "dependencies",
                "executions",
            }
            or value.get("schema_version") != 3
            or value.get("evidence_type") != "ecorex-source-quality-execution"
            or not isinstance(selected, dict)
            or _SHA256.fullmatch(str(selected.get("report_sha256"))) is None
        ):
            raise ValueError("release_bound_source_execution_missing")
        if gate == "e2e":
            if (
                set(selected)
                != {"report_sha256", "tests", "passed", "failed", "skipped", "duration_milliseconds"}
                or selected.get("tests") != 11
                or selected.get("passed") != 11
                or selected.get("failed") != 0
                or selected.get("skipped") != 0
                or isinstance(selected.get("duration_milliseconds"), bool)
                or not isinstance(selected.get("duration_milliseconds"), (int, float))
                or not math.isfinite(float(selected["duration_milliseconds"]))
                or selected["duration_milliseconds"] <= 0
            ):
                raise ValueError("browser_e2e_execution_missing")
        elif (
            set(selected)
            != {"report_sha256", "tests", "failures", "errors", "skipped", "required_corpus"}
            or isinstance(selected.get("tests"), bool)
            or not isinstance(selected.get("tests"), int)
            or selected["tests"] < 1
            or selected.get("failures") != 0
            or selected.get("errors") != 0
            or selected.get("skipped") != 0
            or not isinstance(selected.get("required_corpus"), list)
            or len(selected["required_corpus"]) != 6
        ):
            raise ValueError("migration_execution_missing")
        return dict(selected)
    expected_type = "shared-storage" if gate == "image-shared-storage" else "soak"
    node_ids = value.get("node_ids")
    minimum_duration = 0 if expected_type == "shared-storage" else 4 * 60 * 60
    duration = value.get("duration_seconds")
    junit = value.get("pytest_junit_sha256")
    expected_keys = {
        "schema_version",
        "evidence_type",
        "gate_type",
        "status",
        "commit_sha",
        "workflow_run_id",
        "node_ids",
        "jobs_per_round",
        "workers_per_round",
        "pytest_node_ids",
        "rounds_completed",
        "duration_seconds",
        "pytest_junit_sha256",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("evidence_type") != "ecorex-image-shared-storage-execution"
        or value.get("gate_type") != expected_type
        or not isinstance(node_ids, list)
        or len(node_ids) != 2
        or len(set(node_ids)) != 2
        or isinstance(value.get("jobs_per_round"), bool)
        or not isinstance(value.get("jobs_per_round"), int)
        or value["jobs_per_round"] < 256
        or isinstance(value.get("workers_per_round"), bool)
        or not isinstance(value.get("workers_per_round"), int)
        or value["workers_per_round"] < 48
        or value.get("pytest_node_ids") != _IMAGE_PYTEST_NODE_IDS
        or isinstance(value.get("rounds_completed"), bool)
        or not isinstance(value.get("rounds_completed"), int)
        or value["rounds_completed"] < 1
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < minimum_duration
        or not isinstance(junit, list)
        or len(junit) != value["rounds_completed"]
        or any(_SHA256.fullmatch(str(item)) is None for item in junit)
    ):
        raise ValueError("image_gate_execution_invalid")
    return {
        "node_ids": node_ids,
        "jobs_per_round": value["jobs_per_round"],
        "workers_per_round": value["workers_per_round"],
        "pytest_node_ids": value["pytest_node_ids"],
        "pytest_junit_sha256": junit,
        "rounds_completed": value["rounds_completed"],
        "duration_seconds": duration,
    }


def authenticate_candidate(
    *,
    candidate_receipt: Path,
    release_manifest: Path,
    trusted_public_key: str,
    staging_provenance: Path,
    commit_sha: str,
    expected_staging_run_id: int,
) -> dict[str, Any]:
    """Authenticate the exact signed Candidate and its staging authority.

    This is intentionally reusable by the typed gate writer.  A writer must
    never trust release identity fields copied into an unsigned intermediate
    JSON document when the actual signed Candidate is available beside it.
    """

    if (
        _COMMIT.fullmatch(commit_sha) is None
        or expected_staging_run_id < 1
    ):
        raise ValueError("release_bound_identity_invalid")
    candidate, candidate_payload, candidate_sha256 = _read(candidate_receipt)
    if candidate_payload != _canonical_json(candidate):
        raise ValueError("candidate_build_receipt_invalid")
    staging, staging_sha256 = _staging(
        staging_provenance,
        commit=commit_sha,
        run_id=expected_staging_run_id,
    )
    public = _trusted_key(trusted_public_key)
    manifest, manifest_sha256, verifier = _manifest(release_manifest, public)
    stage_receipts, web_tree_sha256 = _candidate(
        candidate,
        commit=commit_sha,
        staging_run_id=expected_staging_run_id,
        staging_sha256=staging_sha256,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        verifier=verifier,
    )
    if candidate["staging_provenance"]["run_attempt"] != staging["run_attempt"]:
        raise ValueError("staging_provenance_invalid")
    return {
        "candidate": candidate,
        "candidate_payload": candidate_payload,
        "candidate_sha256": candidate_sha256,
        "staging": staging,
        "staging_sha256": staging_sha256,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "stage_receipts": stage_receipts,
        "web_tree_sha256": web_tree_sha256,
    }


def build_release_bound_evidence(
    *,
    gate: str,
    source_evidence: Path,
    candidate_receipt: Path,
    release_manifest: Path,
    trusted_public_key: str,
    staging_provenance: Path,
    commit_sha: str,
    workflow_run_id: int,
    expected_staging_run_id: int,
) -> dict[str, Any]:
    """Recompute one generic release-bound execution without writing it."""

    if gate not in {
        "e2e",
        "migration-dry-run",
        "image-shared-storage",
        "image-soak",
    }:
        raise ValueError("release_bound_gate_invalid")
    if _COMMIT.fullmatch(commit_sha) is None or workflow_run_id < 1:
        raise ValueError("release_bound_identity_invalid")
    source, _source_payload, source_sha256 = _read(source_evidence)
    authenticated = authenticate_candidate(
        candidate_receipt=candidate_receipt,
        release_manifest=release_manifest,
        trusted_public_key=trusted_public_key,
        staging_provenance=staging_provenance,
        commit_sha=commit_sha,
        expected_staging_run_id=expected_staging_run_id,
    )
    manifest = authenticated["manifest"]
    execution = _validate_source(
        gate,
        source,
        commit=commit_sha,
        workflow_run_id=workflow_run_id,
    )
    return {
        "schema_version": 2,
        "evidence_type": "ecorex-release-bound-gate",
        "gate": gate,
        "status": "passed",
        "commit_sha": commit_sha,
        "workflow_run_id": workflow_run_id,
        "staging_workflow_run_id": expected_staging_run_id,
        "staging_provenance_sha256": authenticated["staging_sha256"],
        "release_id": manifest.release_id,
        "version": manifest.version,
        "channel": manifest.channel.value,
        "build_digest": manifest.build_digest,
        "manifest_sha256": authenticated["manifest_sha256"],
        "web_tree_sha256": authenticated["web_tree_sha256"],
        "candidate_receipt_sha256": authenticated["candidate_sha256"],
        "source_evidence_sha256": source_sha256,
        "stage_receipts": dict(sorted(authenticated["stage_receipts"].items())),
        "execution": execution,
    }


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = build_release_bound_evidence(
            gate=args.gate,
            source_evidence=args.source_evidence,
            candidate_receipt=args.candidate_receipt,
            release_manifest=args.release_manifest,
            trusted_public_key=args.trusted_public_key,
            staging_provenance=args.staging_provenance,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            expected_staging_run_id=args.expected_staging_run_id,
        )
        output = args.output.resolve()
        if os.path.lexists(output):
            raise ValueError("release_bound_evidence_exists")
        write_new_json_file(value, output, code="release_bound_evidence_exists")
        print(
            json.dumps(
                {"ok": True, "gate": args.gate, "release_id": value["release_id"]}
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
