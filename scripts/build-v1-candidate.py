#!/usr/bin/env python3
"""Build one signed v1 Candidate from exact platform staging receipts."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release import (  # noqa: E402
    CandidateBuildError,
    DigestPinnedExternalSigner,
    build_candidate,
    write_failure_receipt,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify real Windows/macOS Runtime and Capability Pack stages, then "
            "build one immutable externally-signed EcoreX Candidate."
        )
    )
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--web-dist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-staging-run-id", required=True, type=int)
    parser.add_argument("--staging-provenance", required=True, type=Path)
    parser.add_argument("--dependency-lock-manifest", required=True, type=Path)
    parser.add_argument("--delta-base-release-dir", type=Path)
    return parser


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CandidateBuildError("candidate_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    encoded_public = _required_environment("ECOREX_RELEASE_SIGNER_PUBLIC_KEY")
    try:
        public_key = base64.b64decode(encoded_public, validate=True)
    except (TypeError, ValueError):
        raise CandidateBuildError("candidate_signer_public_key_invalid") from None
    executable_sha256 = _required_environment(
        "ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256"
    )
    if _SHA256.fullmatch(executable_sha256) is None:
        raise CandidateBuildError("candidate_signer_configuration_invalid")
    adapter = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None
    adapter_sha256 = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256") or None
    try:
        return DigestPinnedExternalSigner(
            key_id=_required_environment("ECOREX_RELEASE_SIGNER_KEY_ID"),
            public_key=public_key,
            executable_path=_required_environment("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=adapter,
            adapter_sha256=adapter_sha256,
            environment=os.environ,
        )
    except (TypeError, ValueError) as exc:
        raise CandidateBuildError(
            f"candidate_signer_{type(exc).__name__.casefold()}"
        ) from None


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        built = build_candidate(
            recipe_path=args.recipe,
            input_root=args.input_root,
            web_dist=args.web_dist,
            destination=args.output,
            receipt_path=args.receipt,
            expected_commit=args.expected_commit,
            expected_workflow_run_id=args.expected_staging_run_id,
            staging_provenance_path=args.staging_provenance,
            dependency_lock_manifest_path=args.dependency_lock_manifest,
            signer=_signer(),
            delta_base_release_dir=args.delta_base_release_dir,
        )
        result = {
            "ok": True,
            "release_id": built.manifest.release_id,
            "version": built.manifest.version,
            "channel": built.manifest.channel.value,
            "build_digest": built.manifest.build_digest,
            "release_dir": str(built.output_dir),
            "receipt": str(args.receipt.resolve()),
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, CandidateBuildError) else (
            f"candidate_build_{type(exc).__name__.casefold()}"
        )
        try:
            if not os.path.lexists(args.receipt):
                write_failure_receipt(
                    args.receipt,
                    code=code,
                    expected_commit=args.expected_commit,
                )
        except Exception:
            pass
        print(
            json.dumps(
                {"ok": False, "error": "CandidateBuildError", "code": code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
