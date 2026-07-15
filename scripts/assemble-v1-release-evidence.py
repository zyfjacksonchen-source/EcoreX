#!/usr/bin/env python3
"""Assemble exact Control Plane gate evidence from immutable CI receipts."""

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

from ecorex.control_plane.repository import required_release_gates  # noqa: E402
from ecorex.control_plane.cli import (  # noqa: E402
    _bootstrap_index_evidence_token,
    _publication_evidence_token,
)
from ecorex.update import ReleaseChannel, ReleaseManifest  # noqa: E402
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.release.gate_attestation import build_unsigned_gate_bundle  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION_GATES = frozenset({"github-release", "mirror-sync", "cdn-sync"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts-dir", required=True, type=Path)
    parser.add_argument("--publication-receipt", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bootstrap-index-receipt", type=Path)
    parser.add_argument(
        "--phase", choices=("prepare", "finalize"), default="finalize"
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-workflow-run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            _COMMIT.fullmatch(args.expected_commit) is None
            or isinstance(args.expected_workflow_run_id, bool)
            or args.expected_workflow_run_id < 1
        ):
            raise ValueError("release_evidence_commit_invalid")
        manifest_payload = read_stable_regular_file(
            args.manifest,
            maximum_bytes=16 * 1024 * 1024,
            code="release_manifest_invalid",
        )
        manifest_raw = strict_json_loads(manifest_payload, code="release_manifest_invalid")
        if not isinstance(manifest_raw, dict):
            raise ValueError("release_manifest_invalid")
        manifest = ReleaseManifest.from_dict(manifest_raw)
        # Keep the release identity byte-for-byte aligned with the immutable
        # file uploaded to all three origins. ReleaseBuilder intentionally
        # writes pretty JSON plus one LF; parsing and compact re-serialization
        # is a different byte stream and must never become a second identity.
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        required = required_release_gates(manifest.channel)
        directory = Path(os.path.abspath(args.receipts_dir))
        if not directory.is_dir():
            raise ValueError("release_gate_receipt_set_incomplete")
        expected = required - _PUBLICATION_GATES - {"bootstrap-index"}
        observed = {path.stem for path in directory.glob("*.json")}
        if observed != expected:
            raise ValueError("release_gate_receipt_set_incomplete")
        result: dict[str, dict[str, str]] = {}
        receipt_run_id: int | None = None
        for gate in sorted(expected):
            path = directory / f"{gate}.json"
            payload = read_stable_regular_file(
                path, maximum_bytes=64 * 1024, code="release_gate_receipt_invalid"
            )
            value = strict_json_loads(payload, code="release_gate_receipt_invalid")
            if (
                not isinstance(value, dict)
                or set(value)
                != {
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
                or value.get("schema_version") != 2
                or value.get("receipt_type") != "ecorex-release-gate"
                or value.get("gate") != gate
                or value.get("status") != "passed"
                or value.get("commit_sha") != args.expected_commit
                or isinstance(value.get("workflow_run_id"), bool)
                or not isinstance(value.get("workflow_run_id"), int)
                or value["workflow_run_id"] < 1
                or value.get("release_id") != manifest.release_id
                or value.get("version") != manifest.version
                or value.get("channel") != manifest.channel.value
                or value.get("build_digest") != manifest.build_digest
                or value.get("manifest_sha256") != manifest_sha256
                or not isinstance(value.get("evidence_type"), str)
                or not value["evidence_type"]
                or _SHA256.fullmatch(str(value.get("evidence_sha256"))) is None
            ):
                raise ValueError("release_gate_receipt_invalid")
            if receipt_run_id is None:
                receipt_run_id = value["workflow_run_id"]
            elif receipt_run_id != value["workflow_run_id"]:
                raise ValueError("release_gate_receipt_mixed_workflow_runs")
            result[gate] = {
                "status": "passed",
                "evidence": "gate-receipt:sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        publication = Path(os.path.abspath(args.publication_receipt))
        publication_payload = read_stable_regular_file(
            publication, maximum_bytes=2 * 1024 * 1024, code="publication_receipt_invalid"
        )
        publication_value = strict_json_loads(
            publication_payload, code="publication_receipt_invalid"
        )
        if not isinstance(publication_value, dict):
            raise ValueError("publication_receipt_invalid")
        publication_token = _publication_evidence_token(
            publication,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        for gate in sorted(_PUBLICATION_GATES):
            result[gate] = {"status": "passed", "evidence": publication_token}
        if manifest.channel is ReleaseChannel.STABLE and args.phase == "finalize":
            if args.bootstrap_index_receipt is None:
                raise ValueError("bootstrap_index_receipt_required")
            proof_token = _bootstrap_index_evidence_token(
                args.bootstrap_index_receipt,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                release_publication_receipt_sha256=hashlib.sha256(
                    publication_payload
                ).hexdigest(),
            )
            result["bootstrap-index"] = {
                "status": "passed",
                "evidence": proof_token,
            }
        elif args.bootstrap_index_receipt is not None:
            raise ValueError("bootstrap_index_receipt_forbidden")
        expected_result = (
            required - {"bootstrap-index"}
            if manifest.channel is ReleaseChannel.STABLE and args.phase == "prepare"
            else required
        )
        if set(result) != expected_result:
            raise ValueError("release_evidence_incomplete")
        if receipt_run_id is None:
            raise ValueError("release_gate_receipt_set_incomplete")
        if receipt_run_id != args.expected_workflow_run_id:
            raise ValueError("release_gate_receipt_workflow_run_mismatch")
        unsigned = build_unsigned_gate_bundle(
            phase=(
                "prepare"
                if manifest.channel is ReleaseChannel.STABLE
                and args.phase == "prepare"
                else "finalize"
            ),
            commit_sha=args.expected_commit,
            workflow_run_id=receipt_run_id,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            gates=result,
        )
        output = args.output.resolve()
        if os.path.lexists(output):
            raise ValueError("release_evidence_exists")
        write_new_json_file(unsigned, output, code="release_evidence_exists")
        print(json.dumps({"ok": True, "gate_count": len(result)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc) or type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
