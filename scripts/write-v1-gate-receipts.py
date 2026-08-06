#!/usr/bin/env python3
"""Write immutable release-bound receipts from gate-specific typed evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.control_plane.repository import REQUIRED_RELEASE_GATES  # noqa: E402
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS  # noqa: E402
from ecorex.release.ci_reproducibility import (  # noqa: E402
    bind_to_candidate as bind_reproducibility_to_candidate,
    canonical_json_bytes,
)
from ecorex.release.live_acceptance import LIVE_ACCEPTANCE_GATES  # noqa: E402
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.update import ReleaseManifest  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION_GATES = frozenset(
    {"github-release", "mirror-sync", "cdn-sync", "bootstrap-index"}
)
_QUALITY_GATES = frozenset(
    {"lint", "typecheck", "unit", "contract", "integration", "migration-dry-run"}
)
_BOUND_GATES = frozenset(
    {
        "e2e",
        "migration-dry-run",
        "image-shared-storage",
        "image-soak",
        *LIVE_ACCEPTANCE_GATES,
    }
)
_REPRODUCIBILITY_GATES = frozenset({"reproducibility"})
_PLATFORM_GATES = frozenset({"windows-build", "macos-build"})
_PREFLIGHT_GATES = frozenset({"license", "secret-scan"})
_RELEASE_SUPPLY_GATES = frozenset({"sbom", "size-scan"})
_STAGE_IDS = frozenset(
    f"{kind}-{platform}-{architecture}"
    for platform, architecture in (("windows", "x64"), ("macos", "arm64"), ("macos", "x64"))
    for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
)
_REPRODUCIBILITY_TARGETS = frozenset(
    {"ubuntu-x64", "windows-x64", "macos-arm64", "macos-x64"}
)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("release_bound_reproducibility_evidence_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("release_bound_reproducibility_evidence_invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("release_bound_reproducibility_evidence_invalid")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", action="append", required=True)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--trusted-public-key", required=True)
    parser.add_argument("--source-evidence", type=Path)
    parser.add_argument("--staging-provenance", required=True, type=Path)
    parser.add_argument("--expected-staging-run-id", required=True, type=int)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def _binding_module() -> Any:
    """Load the checked-in generic binder as the single validation authority."""

    path = ROOT / "scripts" / "bind-v1-release-gate-evidence.py"
    name = "ecorex_v1_release_gate_binding_authority"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("release_gate_binding_authority_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise ValueError("release_gate_binding_authority_unavailable") from None
    return module


def _read(path: Path, *, maximum: int) -> tuple[dict[str, Any], bytes]:
    payload = read_stable_regular_file(
        path, maximum_bytes=maximum, code="gate_evidence_invalid"
    )
    value = strict_json_loads(payload, code="gate_evidence_invalid")
    if not isinstance(value, dict):
        raise ValueError("gate_evidence_invalid")
    return value, payload


def _snapshot_file(
    source: Path,
    destination: Path,
    *,
    maximum: int,
) -> Path:
    """Copy one stable input into a private per-invocation authority snapshot."""

    payload = read_stable_regular_file(
        source,
        maximum_bytes=maximum,
        code="gate_authority_snapshot_invalid",
    )
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ValueError("gate_authority_snapshot_invalid") from None
    return destination


def _identity(value: dict[str, Any], *, commit: str, run_id: int) -> None:
    if value.get("commit_sha") != commit or value.get("workflow_run_id") != run_id:
        raise ValueError("gate_evidence_identity_mismatch")


def _quality(gate: str, value: dict[str, Any], *, commit: str, run_id: int) -> str:
    _identity(value, commit=commit, run_id=run_id)
    gates = value.get("gates")
    executions = value.get("executions")
    dependencies = value.get("dependencies")
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
        or value.get("status") != "passed"
        or not isinstance(gates, dict)
        or set(gates) != _QUALITY_GATES
        or any(status != "passed" for status in gates.values())
        or gates.get(gate) != "passed"
        or not isinstance(dependencies, dict)
        or set(dependencies) != {"byte-contract", "supply-chain", "v030-baseline"}
        or any(_SHA256.fullmatch(str(item)) is None for item in dependencies.values())
        or not isinstance(executions, dict)
        or set(executions) != {"full-pytest", "migration-pytest", "browser-e2e"}
    ):
        raise ValueError("quality_gate_evidence_invalid")
    execution_key = "migration-pytest" if gate == "migration-dry-run" else "full-pytest"
    execution = executions.get(execution_key)
    if (
        not isinstance(execution, dict)
        or _SHA256.fullmatch(str(execution.get("report_sha256"))) is None
        or isinstance(execution.get("tests"), bool)
        or not isinstance(execution.get("tests"), int)
        or execution["tests"] < (1 if gate == "migration-dry-run" else 1000)
        or execution.get("failures") != 0
        or execution.get("errors") != 0
        or isinstance(execution.get("skipped"), bool)
        or not isinstance(execution.get("skipped"), int)
        or execution["skipped"] < 0
        or not isinstance(execution.get("required_corpus"), list)
        or not execution["required_corpus"]
    ):
        raise ValueError("quality_gate_execution_invalid")
    if (
        gate == "migration-dry-run" and execution["skipped"] != 0
    ) or (
        gate != "migration-dry-run"
        and execution["tests"] - execution["skipped"] < 1000
    ):
        raise ValueError("quality_gate_execution_invalid")
    return "source-quality"


def _bound(
    gate: str,
    value: dict[str, Any],
    *,
    commit: str,
    run_id: int,
    manifest: ReleaseManifest,
    manifest_sha256: str,
) -> str:
    _identity(value, commit=commit, run_id=run_id)
    expected_keys = {
        "schema_version",
        "evidence_type",
        "gate",
        "status",
        "commit_sha",
        "workflow_run_id",
        "staging_workflow_run_id",
        "staging_provenance_sha256",
        "release_id",
        "version",
        "channel",
        "build_digest",
        "manifest_sha256",
        "web_tree_sha256",
        "candidate_receipt_sha256",
        "source_evidence_sha256",
        "stage_receipts",
        "execution",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version") != 2
        or value.get("evidence_type") != "ecorex-release-bound-gate"
        or value.get("gate") != gate
        or value.get("status") != "passed"
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("channel") != manifest.channel.value
        or value.get("build_digest") != manifest.build_digest
        or value.get("manifest_sha256") != manifest_sha256
        or isinstance(value.get("staging_workflow_run_id"), bool)
        or not isinstance(value.get("staging_workflow_run_id"), int)
        or value["staging_workflow_run_id"] < 1
        or _SHA256.fullmatch(str(value.get("staging_provenance_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("candidate_receipt_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("source_evidence_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("web_tree_sha256"))) is None
        or not isinstance(value.get("stage_receipts"), dict)
        or set(value["stage_receipts"]) != _STAGE_IDS
        or any(_SHA256.fullmatch(str(item)) is None for item in value["stage_receipts"].values())
        or not isinstance(value.get("execution"), dict)
    ):
        raise ValueError("release_bound_gate_evidence_invalid")
    return "release-bound-execution"


def _reproducibility(
    value: dict[str, Any],
    *,
    commit: str,
    manifest: ReleaseManifest,
    manifest_sha256: str,
) -> str:
    """Validate the specialized CI-to-signed-Candidate binding.

    The source four-runner comparison is intentionally insufficient here.  A
    gate receipt can only be minted from the specialized binder output that
    has already verified the signed Candidate receipt and release manifest.
    """

    expected_keys = {
        "schema_version",
        "evidence_type",
        "status",
        "commit_sha",
        "ci_run",
        "artifact_metadata_sha256",
        "artifacts",
        "reproducibility_evidence_sha256",
        "byte_contract_sha256",
        "canonical_web_bundle_sha256",
        "candidate_receipt_sha256",
        "release_id",
        "version",
        "channel",
        "build_digest",
        "manifest_sha256",
        "web_tree_sha256",
        "candidate_signature_key_id",
    }
    ci_run = value.get("ci_run")
    artifacts = value.get("artifacts")
    contract_digests = value.get("byte_contract_sha256")
    if (
        set(value) != expected_keys
        or value.get("schema_version") != 2
        or value.get("evidence_type") != "ecorex-release-bound-reproducibility"
        or value.get("status") != "passed"
        or value.get("commit_sha") != commit
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("channel") != manifest.channel.value
        or value.get("build_digest") != manifest.build_digest
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("canonical_web_bundle_sha256")
        != value.get("web_tree_sha256")
        or value.get("candidate_signature_key_id") != manifest.signature.key_id
        or _SHA256.fullmatch(
            str(value.get("artifact_metadata_sha256"))
        )
        is None
        or _SHA256.fullmatch(
            str(value.get("reproducibility_evidence_sha256"))
        )
        is None
        or _SHA256.fullmatch(str(value.get("candidate_receipt_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("canonical_web_bundle_sha256")))
        is None
        or _SHA256.fullmatch(str(value.get("web_tree_sha256"))) is None
        or not isinstance(ci_run, dict)
        or set(ci_run)
        != {
            "repository",
            "repository_id",
            "head_repository_id",
            "workflow_path",
            "workflow_run_id",
            "run_attempt",
            "event",
            "protected_ref",
            "run_started_at",
            "completed_at",
        }
        or ci_run.get("workflow_path") != ".github/workflows/ecorex-v1-ci.yml"
        or ci_run.get("event") not in {"push", "workflow_dispatch"}
        or ci_run.get("protected_ref") != "refs/heads/main"
        or not isinstance(ci_run.get("repository"), str)
        or not ci_run["repository"]
        or isinstance(ci_run.get("repository_id"), bool)
        or not isinstance(ci_run.get("repository_id"), int)
        or ci_run["repository_id"] < 1
        or isinstance(ci_run.get("head_repository_id"), bool)
        or not isinstance(ci_run.get("head_repository_id"), int)
        or ci_run["head_repository_id"] < 1
        or isinstance(ci_run.get("workflow_run_id"), bool)
        or not isinstance(ci_run.get("workflow_run_id"), int)
        or ci_run["workflow_run_id"] < 1
        or isinstance(ci_run.get("run_attempt"), bool)
        or not isinstance(ci_run.get("run_attempt"), int)
        or ci_run["run_attempt"] < 1
        or not isinstance(ci_run.get("run_started_at"), str)
        or not ci_run["run_started_at"]
        or not isinstance(ci_run.get("completed_at"), str)
        or not ci_run["completed_at"]
        or not isinstance(contract_digests, dict)
        or set(contract_digests) != _REPRODUCIBILITY_TARGETS
        or any(
            _SHA256.fullmatch(str(digest)) is None
            for digest in contract_digests.values()
        )
        or len(set(contract_digests.values())) != 1
        or not isinstance(artifacts, dict)
        or set(artifacts) != _REPRODUCIBILITY_TARGETS
    ):
        raise ValueError("release_bound_reproducibility_evidence_invalid")
    started = _timestamp(ci_run["run_started_at"])
    completed = _timestamp(ci_run["completed_at"])
    if completed < started:
        raise ValueError("release_bound_reproducibility_evidence_invalid")
    artifact_ids: set[int] = set()
    for artifact in artifacts.values():
        if (
            not isinstance(artifact, dict)
            or set(artifact)
            != {
                "artifact_id",
                "archive_sha256",
                "size_bytes",
                "created_at",
                "updated_at",
            }
            or isinstance(artifact.get("artifact_id"), bool)
            or not isinstance(artifact.get("artifact_id"), int)
            or artifact["artifact_id"] < 1
            or _SHA256.fullmatch(str(artifact.get("archive_sha256"))) is None
            or isinstance(artifact.get("size_bytes"), bool)
            or not isinstance(artifact.get("size_bytes"), int)
            or artifact["size_bytes"] < 1
            or not isinstance(artifact.get("created_at"), str)
            or not artifact["created_at"]
            or not isinstance(artifact.get("updated_at"), str)
            or not artifact["updated_at"]
        ):
            raise ValueError("release_bound_reproducibility_evidence_invalid")
        artifact_id = artifact["artifact_id"]
        created = _timestamp(artifact["created_at"])
        updated = _timestamp(artifact["updated_at"])
        if (
            artifact_id in artifact_ids
            or created < started
            or updated < created
            or updated > completed + timedelta(minutes=5)
        ):
            raise ValueError("release_bound_reproducibility_evidence_invalid")
        artifact_ids.add(artifact_id)
    return "release-bound-reproducibility"


def _preflight(gate: str, value: dict[str, Any]) -> str:
    gates = value.get("gates")
    selected = gates.get(gate) if isinstance(gates, dict) else None
    if (
        set(value) != {"schema_version", "status", "gates"}
        or value.get("schema_version") != 1
        or value.get("status") != "passed"
        or not isinstance(selected, dict)
        or selected.get("status") != "passed"
    ):
        raise ValueError("preflight_gate_evidence_invalid")
    if gate == "license" and (
        not isinstance(selected.get("python_packages"), list)
        or not selected["python_packages"]
        or not isinstance(selected.get("node_packages"), list)
        or not selected["node_packages"]
    ):
        raise ValueError("preflight_gate_evidence_invalid")
    if gate == "secret-scan" and (
        isinstance(selected.get("file_count"), bool)
        or not isinstance(selected.get("file_count"), int)
        or selected["file_count"] < 1
        or _SHA256.fullmatch(str(selected.get("inventory_sha256"))) is None
    ):
        raise ValueError("preflight_gate_evidence_invalid")
    return "source-supply-chain"


def _release_supply(gate: str, value: dict[str, Any], manifest: ReleaseManifest) -> str:
    gates = value.get("gates")
    selected = gates.get(gate) if isinstance(gates, dict) else None
    if (
        set(value) != {"schema_version", "status", "release_id", "gates"}
        or value.get("schema_version") != 1
        or value.get("status") != "passed"
        or value.get("release_id") != manifest.release_id
        or not isinstance(selected, dict)
        or selected.get("status") != "passed"
    ):
        raise ValueError("release_supply_gate_evidence_invalid")
    if gate == "sbom" and (
        _SHA256.fullmatch(str(selected.get("sha256"))) is None
        or isinstance(selected.get("component_count"), bool)
        or not isinstance(selected.get("component_count"), int)
        or selected["component_count"] < 1
    ):
        raise ValueError("release_supply_gate_evidence_invalid")
    if gate == "size-scan" and (
        not isinstance(selected.get("artifacts"), list) or not selected["artifacts"]
    ):
        raise ValueError("release_supply_gate_evidence_invalid")
    return "release-supply-chain"


def _signature(value: dict[str, Any], manifest: ReleaseManifest, manifest_sha256: str) -> str:
    artifacts = value.get("artifacts")
    if (
        set(value)
        != {
            "schema_version",
            "status",
            "release_id",
            "manifest_sha256",
            "key_id",
            "public_key_sha256",
            "artifacts",
        }
        or value.get("schema_version") != 1
        or value.get("status") != "passed"
        or value.get("release_id") != manifest.release_id
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("key_id") != manifest.signature.key_id
        or _SHA256.fullmatch(str(value.get("public_key_sha256"))) is None
        or not isinstance(artifacts, list)
        or len(artifacts) != len(manifest.artifacts)
        or any(
            not isinstance(item, dict)
            or set(item) != {"artifact_id", "sha256"}
            or _SHA256.fullmatch(str(item.get("sha256"))) is None
            for item in artifacts
        )
        or {
            (item.get("artifact_id"), item.get("sha256"))
            for item in artifacts
            if isinstance(item, dict)
        }
        != {(item.artifact_id, item.sha256) for item in manifest.artifacts}
    ):
        raise ValueError("signature_gate_evidence_invalid")
    return "release-signature-verification"


def _evidence_type(
    gate: str,
    value: dict[str, Any],
    *,
    commit: str,
    run_id: int,
    manifest: ReleaseManifest,
    manifest_sha256: str,
) -> str:
    if gate in _REPRODUCIBILITY_GATES:
        return _reproducibility(
            value,
            commit=commit,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
    if gate in _BOUND_GATES:
        return _bound(
            gate,
            value,
            commit=commit,
            run_id=run_id,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
    if gate in _QUALITY_GATES:
        return _quality(gate, value, commit=commit, run_id=run_id)
    if gate in _PLATFORM_GATES:
        source_gate = value.get("gate")
        if source_gate not in _BOUND_GATES:
            raise ValueError("platform_gate_evidence_invalid")
        _bound(
            str(source_gate),
            value,
            commit=commit,
            run_id=run_id,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        stage_receipts = value.get("stage_receipts")
        platform = "windows" if gate == "windows-build" else "macos"
        required = {
            f"{kind}-{platform}-{architecture}"
            for architecture in (("x64",) if platform == "windows" else ("arm64", "x64"))
            for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
        }
        if not isinstance(stage_receipts, dict) or not required.issubset(stage_receipts):
            raise ValueError("platform_gate_evidence_invalid")
        return "release-bound-platform-stage"
    if gate in _PREFLIGHT_GATES:
        return _preflight(gate, value)
    if gate in _RELEASE_SUPPLY_GATES:
        return _release_supply(gate, value, manifest)
    if gate == "signature":
        return _signature(value, manifest, manifest_sha256)
    raise ValueError("gate_evidence_type_unsupported")


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshots: tempfile.TemporaryDirectory[str] | None = None
    try:
        gates = tuple(args.gate)
        if (
            not gates
            or len(gates) != len(set(gates))
            or not set(gates).issubset(REQUIRED_RELEASE_GATES - _PUBLICATION_GATES)
            or _COMMIT.fullmatch(args.commit_sha) is None
            or args.workflow_run_id < 1
            or args.expected_staging_run_id < 1
        ):
            raise ValueError("gate_receipt_input_invalid")
        snapshots = tempfile.TemporaryDirectory(prefix="ecorex-gate-authority-")
        snapshot_root = Path(snapshots.name).resolve(strict=True)
        args.evidence_file = _snapshot_file(
            args.evidence_file,
            snapshot_root / "evidence.json",
            maximum=64 * 1024 * 1024,
        )
        args.candidate_receipt = _snapshot_file(
            args.candidate_receipt,
            snapshot_root / "candidate.json",
            maximum=64 * 1024 * 1024,
        )
        args.manifest = _snapshot_file(
            args.manifest,
            snapshot_root / "manifest.json",
            maximum=16 * 1024 * 1024,
        )
        args.staging_provenance = _snapshot_file(
            args.staging_provenance,
            snapshot_root / "staging.json",
            maximum=2 * 1024 * 1024,
        )
        if args.source_evidence is not None:
            args.source_evidence = _snapshot_file(
                args.source_evidence,
                snapshot_root / "source.json",
                maximum=64 * 1024 * 1024,
            )
        evidence_value, evidence_payload = _read(args.evidence_file, maximum=64 * 1024 * 1024)
        evidence_sha256 = hashlib.sha256(evidence_payload).hexdigest()
        binding = _binding_module()
        authenticated = binding.authenticate_candidate(
            candidate_receipt=args.candidate_receipt,
            release_manifest=args.manifest,
            trusted_public_key=args.trusted_public_key,
            staging_provenance=args.staging_provenance,
            commit_sha=args.commit_sha,
            expected_staging_run_id=args.expected_staging_run_id,
        )
        manifest = authenticated["manifest"]
        manifest_sha256 = authenticated["manifest_sha256"]

        generic_gates = {
            gate for gate in gates if gate in _BOUND_GATES or gate in _PLATFORM_GATES
        }
        if generic_gates:
            if args.source_evidence is None:
                raise ValueError("release_bound_source_evidence_required")
            allowed_sets = {
                frozenset({gate}) for gate in _BOUND_GATES
            } | {frozenset(_PLATFORM_GATES)}
            if (
                set(gates) != generic_gates
                or frozenset(generic_gates) not in allowed_sets
            ):
                raise ValueError("release_bound_gate_set_ambiguous")
            recompute_gate = (
                "e2e"
                if generic_gates == set(_PLATFORM_GATES)
                else next(iter(generic_gates))
            )
            recomputed = binding.build_release_bound_evidence(
                gate=recompute_gate,
                source_evidence=args.source_evidence,
                candidate_receipt=args.candidate_receipt,
                release_manifest=args.manifest,
                trusted_public_key=args.trusted_public_key,
                staging_provenance=args.staging_provenance,
                commit_sha=args.commit_sha,
                workflow_run_id=args.workflow_run_id,
                expected_staging_run_id=args.expected_staging_run_id,
            )
            if canonical_json_bytes(recomputed) != evidence_payload:
                raise ValueError("release_bound_evidence_recomputation_mismatch")
        if set(gates) & _REPRODUCIBILITY_GATES:
            if set(gates) != _REPRODUCIBILITY_GATES or args.source_evidence is None:
                raise ValueError("reproducibility_gate_set_invalid")
            recomputed = bind_reproducibility_to_candidate(
                evidence_path=args.source_evidence,
                candidate_receipt_path=args.candidate_receipt,
                release_manifest_path=args.manifest,
                trusted_public_key=args.trusted_public_key,
            )
            if canonical_json_bytes(recomputed) != evidence_payload:
                raise ValueError("reproducibility_evidence_recomputation_mismatch")
        evidence_types = {
            gate: _evidence_type(
                gate,
                evidence_value,
                commit=args.commit_sha,
                run_id=args.workflow_run_id,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )
            for gate in gates
        }
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        if any(os.path.lexists(output / f"{gate}.json") for gate in gates):
            raise ValueError("gate_receipt_exists")
        for gate in gates:
            path = output / f"{gate}.json"
            value = {
                "schema_version": 2,
                "receipt_type": "ecorex-release-gate",
                "gate": gate,
                "status": "passed",
                "commit_sha": args.commit_sha,
                "workflow_run_id": args.workflow_run_id,
                "release_id": manifest.release_id,
                "version": manifest.version,
                "channel": manifest.channel.value,
                "build_digest": manifest.build_digest,
                "manifest_sha256": manifest_sha256,
                "evidence_type": evidence_types[gate],
                "evidence_sha256": evidence_sha256,
            }
            write_new_json_file(value, path, code="gate_receipt_exists")
        print(json.dumps({"ok": True, "gates": sorted(gates)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc) or type(exc).__name__}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    finally:
        if snapshots is not None:
            snapshots.cleanup()


if __name__ == "__main__":
    raise SystemExit(run())
