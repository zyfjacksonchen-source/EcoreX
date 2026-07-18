#!/usr/bin/env python3
"""Assemble an unsigned, byte-bound direct release prepare/finalize bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.control_plane.cli import (  # noqa: E402
    _bootstrap_index_evidence_token,
    _publication_evidence_token,
)
from ecorex.control_plane.repository import (  # noqa: E402
    required_publication_gates,
    required_release_gates,
)
from ecorex.release import (  # noqa: E402
    LIVE_ACCEPTANCE_GATES,
    build_unsigned_direct_admission,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.update import ReleaseChannel, ReleaseManifest  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts-dir", required=True, type=Path)
    parser.add_argument("--publication-receipt", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--operator-waiver", required=True, type=Path)
    parser.add_argument("--bootstrap-index-receipt", type=Path)
    parser.add_argument("--phase", choices=("prepare", "finalize"), required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-workflow-run-id", required=True, type=int)
    parser.add_argument("--operator-instruction-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read(path: Path, maximum: int, code: str) -> tuple[bytes, dict]:
    payload = read_stable_regular_file(path, maximum_bytes=maximum, code=code)
    value = strict_json_loads(payload, code=code)
    if not isinstance(value, dict):
        raise ValueError(code)
    return payload, value


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            _COMMIT.fullmatch(args.expected_commit) is None
            or args.expected_workflow_run_id < 1
            or _SHA256.fullmatch(args.operator_instruction_sha256) is None
        ):
            raise ValueError("direct_release_admission_identity_invalid")
        manifest_bytes, manifest_raw = _read(
            args.manifest, 16 * 1024 * 1024, "release_manifest_invalid"
        )
        manifest = ReleaseManifest.from_dict(manifest_raw)
        if manifest.channel is not ReleaseChannel.STABLE:
            raise ValueError("direct_release_stable_channel_required")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        candidate_bytes, candidate = _read(
            args.candidate_receipt,
            4 * 1024 * 1024,
            "direct_release_candidate_receipt_invalid",
        )
        waiver_bytes, waiver = _read(
            args.operator_waiver,
            2 * 1024 * 1024,
            "direct_release_waiver_invalid",
        )
        publication_bytes, _publication = _read(
            args.publication_receipt,
            2 * 1024 * 1024,
            "direct_release_publication_receipt_invalid",
        )
        signing = waiver.get("signing")
        if (
            candidate.get("commit_sha") != args.expected_commit
            or candidate.get("manifest_sha256") != manifest_sha256
            or not isinstance(candidate.get("staging_provenance"), dict)
            or candidate["staging_provenance"].get("workflow_run_id")
            != args.expected_workflow_run_id
            or waiver.get("operator_instruction_sha256")
            != args.operator_instruction_sha256
            or not isinstance(signing, dict)
            or not isinstance(signing.get("publication_key_id"), str)
        ):
            raise ValueError("direct_release_admission_identity_invalid")
        required = required_release_gates(manifest.channel)
        publication_gates = required_publication_gates(manifest.channel)
        receipt_gates = (
            required
            - publication_gates
            - LIVE_ACCEPTANCE_GATES
            - {"bootstrap-index"}
        )
        directory = Path(os.path.abspath(args.receipts_dir))
        if not directory.is_dir() or {
            path.stem for path in directory.glob("*.json")
        } != receipt_gates:
            raise ValueError("direct_release_gate_receipt_set_incomplete")
        gates: dict[str, dict[str, str]] = {}
        for gate in sorted(receipt_gates):
            payload, value = _read(
                directory / f"{gate}.json",
                64 * 1024,
                "direct_release_gate_receipt_invalid",
            )
            if (
                value.get("schema_version") != 2
                or value.get("receipt_type") != "ecorex-release-gate"
                or value.get("gate") != gate
                or value.get("status") != "passed"
                or value.get("commit_sha") != args.expected_commit
                or value.get("workflow_run_id") != args.expected_workflow_run_id
                or value.get("release_id") != manifest.release_id
                or value.get("version") != manifest.version
                or value.get("channel") != manifest.channel.value
                or value.get("build_digest") != manifest.build_digest
                or value.get("manifest_sha256") != manifest_sha256
                or _SHA256.fullmatch(str(value.get("evidence_sha256"))) is None
            ):
                raise ValueError("direct_release_gate_receipt_invalid")
            gates[gate] = {
                "status": "passed",
                "evidence": "gate-receipt:sha256:"
                + hashlib.sha256(payload).hexdigest(),
            }
        publication_token = _publication_evidence_token(
            args.publication_receipt,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        if publication_token.rsplit(":", 1)[1] != hashlib.sha256(
            publication_bytes
        ).hexdigest():
            raise ValueError("direct_release_publication_receipt_invalid")
        for gate in publication_gates:
            gates[gate] = {"status": "passed", "evidence": publication_token}
        waiver_token = "operator-waiver:sha256:" + hashlib.sha256(
            waiver_bytes
        ).hexdigest()
        for gate in LIVE_ACCEPTANCE_GATES:
            gates[gate] = {"status": "waived", "evidence": waiver_token}
        if args.phase == "finalize":
            if args.bootstrap_index_receipt is None:
                raise ValueError("bootstrap_index_receipt_required")
            gates["bootstrap-index"] = {
                "status": "passed",
                "evidence": _bootstrap_index_evidence_token(
                    args.bootstrap_index_receipt,
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                    release_publication_receipt_sha256=hashlib.sha256(
                        publication_bytes
                    ).hexdigest(),
                ),
            }
        elif args.bootstrap_index_receipt is not None:
            raise ValueError("bootstrap_index_receipt_forbidden")
        expected = required - ({"bootstrap-index"} if args.phase == "prepare" else set())
        if set(gates) != expected:
            raise ValueError("direct_release_admission_gate_set_incomplete")
        unsigned = build_unsigned_direct_admission(
            phase=args.phase,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            commit_sha=args.expected_commit,
            operator_instruction_sha256=args.operator_instruction_sha256,
            candidate_receipt_bytes=candidate_bytes,
            operator_waiver_bytes=waiver_bytes,
            publication_receipt_bytes=publication_bytes,
            publication_key_id=str(signing["publication_key_id"]),
            gates=gates,
        )
        write_new_json_file(
            unsigned,
            args.output.resolve(),
            code="direct_release_admission_exists",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "phase": args.phase,
                    "passed": len(gates) - len(LIVE_ACCEPTANCE_GATES),
                    "waived": len(LIVE_ACCEPTANCE_GATES),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        code = str(exc)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) is None:
            code = "direct_release_admission_assembly_failed"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
