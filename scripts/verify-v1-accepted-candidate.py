#!/usr/bin/env python3
"""Re-authenticate one exact accepted Candidate before external publication."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.control_plane.repository import required_release_gates  # noqa: E402
from ecorex.release.candidate_handoff import (  # noqa: E402
    CandidateHandoffError,
    load_candidate_handoff,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION_GATES = frozenset(
    {"github-release", "mirror-sync", "cdn-sync", "bootstrap-index"}
)
_GATE_KEYS = {
    "schema_version",
    "receipt_type",
    "gate",
    "status",
    "commit_sha",
    "workflow_run_id",
    "release_id",
    "version",
    "channel",
    "build_digest",
    "manifest_sha256",
    "evidence_type",
    "evidence_sha256",
}


def _binding_module() -> Any:
    path = ROOT / "scripts" / "bind-v1-release-gate-evidence.py"
    name = "ecorex_v1_candidate_publication_authority"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("candidate_handoff_authority_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _json(path: Path, *, maximum: int, code: str) -> tuple[Any, bytes]:
    payload = read_stable_regular_file(path, maximum_bytes=maximum, code=code)
    return strict_json_loads(payload, code=code), payload


def _staging_run_id(candidate_receipt: Path) -> int:
    value, _payload = _json(
        candidate_receipt,
        maximum=2 * 1024 * 1024,
        code="candidate_build_receipt_invalid",
    )
    staging = value.get("staging_provenance") if isinstance(value, dict) else None
    run_id = staging.get("workflow_run_id") if isinstance(staging, dict) else None
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("candidate_build_receipt_invalid")
    return run_id


def _gate_receipts(
    root: Path,
    *,
    manifest: Any,
    manifest_sha256: str,
    commit_sha: str,
    workflow_run_id: int,
) -> dict[str, str]:
    expected = required_release_gates(manifest.channel) - _PUBLICATION_GATES
    if not root.is_dir() or root.is_symlink():
        raise ValueError("candidate_handoff_gate_set_invalid")
    paths = sorted(root.glob("*.json"))
    if {path.stem for path in paths} != expected or len(paths) != len(expected):
        raise ValueError("candidate_handoff_gate_set_invalid")
    result: dict[str, str] = {}
    for path in paths:
        payload = read_stable_regular_file(
            path,
            maximum_bytes=64 * 1024,
            code="candidate_handoff_gate_receipt_invalid",
        )
        value = strict_json_loads(payload, code="candidate_handoff_gate_receipt_invalid")
        gate = path.stem
        if (
            not isinstance(value, dict)
            or set(value) != _GATE_KEYS
            or value.get("schema_version") != 2
            or value.get("receipt_type") != "ecorex-release-gate"
            or value.get("gate") != gate
            or value.get("status") != "passed"
            or value.get("commit_sha") != commit_sha
            or isinstance(value.get("workflow_run_id"), bool)
            or not isinstance(value.get("workflow_run_id"), int)
            or value.get("workflow_run_id") != workflow_run_id
            or value.get("release_id") != manifest.release_id
            or value.get("version") != manifest.version
            or value.get("channel") != manifest.channel.value
            or value.get("build_digest") != manifest.build_digest
            or value.get("manifest_sha256") != manifest_sha256
            or not isinstance(value.get("evidence_type"), str)
            or not value["evidence_type"]
            or _SHA256.fullmatch(str(value.get("evidence_sha256"))) is None
        ):
            raise ValueError("candidate_handoff_gate_receipt_invalid")
        result[gate] = hashlib.sha256(payload).hexdigest()
    return result


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-v1-accepted-candidate")
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--artifact-id", required=True, type=int)
    parser.add_argument("--channel", required=True, choices=("canary", "stable"))
    parser.add_argument("--trusted-public-key", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        handoff = load_candidate_handoff(
            args.handoff,
            repository=args.repository,
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
            artifact_id=args.artifact_id,
            channel=args.channel,
        )
        root = Path(os.path.abspath(args.candidate_root))
        if not root.is_dir() or root.is_symlink():
            raise ValueError("candidate_handoff_root_invalid")
        output_root = root / "output"
        candidate_receipt = output_root / "candidate-build-receipt.json"
        manifest_path = output_root / "release" / "release-manifest.json"
        staging = output_root / "evidence" / "staging-provenance.json"
        staging_run_id = _staging_run_id(candidate_receipt)
        authenticated = _binding_module().authenticate_candidate(
            candidate_receipt=candidate_receipt,
            release_manifest=manifest_path,
            trusted_public_key=args.trusted_public_key,
            staging_provenance=staging,
            commit_sha=args.commit_sha,
            expected_staging_run_id=staging_run_id,
        )
        manifest = authenticated["manifest"]
        if manifest.channel.value != args.channel:
            raise ValueError("candidate_handoff_channel_mismatch")
        gates = _gate_receipts(
            root / "gates",
            manifest=manifest,
            manifest_sha256=authenticated["manifest_sha256"],
            commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
        )
        receipt = {
            "schema_version": 1,
            "receipt_type": "ecorex-accepted-candidate-verification",
            "status": "passed",
            "repository": args.repository,
            "commit_sha": args.commit_sha,
            "workflow_run_id": args.workflow_run_id,
            "run_attempt": args.run_attempt,
            "artifact_id": args.artifact_id,
            "artifact_archive_sha256": handoff["artifact_archive_sha256"],
            "release_id": manifest.release_id,
            "version": manifest.version,
            "channel": manifest.channel.value,
            "build_digest": manifest.build_digest,
            "manifest_sha256": authenticated["manifest_sha256"],
            "candidate_receipt_sha256": authenticated["candidate_sha256"],
            "gate_receipt_sha256": gates,
        }
        write_new_json_file(
            receipt,
            args.output,
            code="candidate_handoff_verification_output_exists",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "release_id": manifest.release_id,
                    "gate_count": len(gates),
                },
                sort_keys=True,
            )
        )
        return 0
    except (CandidateHandoffError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, CandidateHandoffError) else str(exc)
        if not code or re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "candidate_handoff_verification_failed"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
