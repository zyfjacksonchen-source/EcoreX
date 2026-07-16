#!/usr/bin/env python3
"""Externally sign and fully validate one direct release admission bundle."""

from __future__ import annotations

import argparse
import base64
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
from ecorex.release import (  # noqa: E402
    DigestPinnedExternalSigner,
    DirectReleaseAdmissionPolicy,
    DirectReleaseWaiverError,
    parse_external_public_key_description,
    sign_direct_admission,
    validate_signed_direct_admission,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.update import Ed25519SignatureVerifier, ReleaseManifest  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unsigned", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--publication-key-description", required=True, type=Path)
    parser.add_argument("--operator-instruction-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("direct_admission_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    try:
        public = base64.b64decode(
            _required("ECOREX_RELEASE_SIGNER_PUBLIC_KEY"), validate=True
        )
    except (TypeError, ValueError):
        raise ValueError("direct_admission_signer_public_key_invalid") from None
    executable_sha256 = _required("ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256")
    if _SHA256.fullmatch(executable_sha256) is None:
        raise ValueError("direct_admission_signer_configuration_invalid")
    try:
        return DigestPinnedExternalSigner(
            key_id=_required("ECOREX_RELEASE_SIGNER_KEY_ID"),
            public_key=public,
            executable_path=_required("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None,
            adapter_sha256=(
                os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256") or None
            ),
            environment=os.environ,
        )
    except (TypeError, ValueError):
        raise ValueError("direct_admission_signer_configuration_invalid") from None


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if _SHA256.fullmatch(args.operator_instruction_sha256) is None:
            raise ValueError("direct_release_instruction_hash_invalid")
        manifest_bytes = read_stable_regular_file(
            args.manifest,
            maximum_bytes=16 * 1024 * 1024,
            code="direct_release_manifest_invalid",
        )
        manifest_raw = strict_json_loads(
            manifest_bytes, code="direct_release_manifest_invalid"
        )
        if not isinstance(manifest_raw, dict):
            raise ValueError("direct_release_manifest_invalid")
        manifest = ReleaseManifest.from_dict(manifest_raw)
        unsigned_bytes = read_stable_regular_file(
            args.unsigned,
            maximum_bytes=32 * 1024 * 1024,
            code="direct_release_admission_invalid",
        )
        unsigned = strict_json_loads(
            unsigned_bytes, code="direct_release_admission_invalid"
        )
        if not isinstance(unsigned, dict):
            raise ValueError("direct_release_admission_invalid")
        description_bytes = read_stable_regular_file(
            args.publication_key_description,
            maximum_bytes=64 * 1024,
            code="direct_release_public_key_description_invalid",
        )
        description = strict_json_loads(
            description_bytes,
            code="direct_release_public_key_description_invalid",
        )
        if not isinstance(description, dict):
            raise ValueError("direct_release_public_key_description_invalid")
        try:
            publication_id, publication_public = (
                parse_external_public_key_description(
                    description, expected_role="publication"
                )
            )
        except DirectReleaseWaiverError:
            raise ValueError(
                "direct_release_public_key_description_invalid"
            ) from None
        signer = _signer()
        phase = unsigned.get("phase")
        expected = required_release_gates(manifest.channel)
        if phase == "prepare":
            expected -= {"bootstrap-index"}
        elif phase != "finalize":
            raise ValueError("direct_release_admission_phase_invalid")
        signed = sign_direct_admission(
            unsigned, signer=signer, manifest=manifest
        )
        policy = DirectReleaseAdmissionPolicy(
            enabled=True,
            release_id=manifest.release_id,
            operator_instruction_sha256=args.operator_instruction_sha256,
            release_public_keys={signer.key_id: signer.public_key_bytes},
            publication_public_keys={publication_id: publication_public},
        )
        validated = validate_signed_direct_admission(
            signed,
            manifest=manifest,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_gates=frozenset(expected),
            expected_phase=str(phase),
            policy=policy,
            release_verifier=Ed25519SignatureVerifier(
                {signer.key_id: signer.public_key_bytes}
            ),
        )
        write_new_json_file(
            signed,
            args.output.resolve(),
            code="direct_release_admission_exists",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "phase": phase,
                    "attestation_sha256": validated.attestation_sha256,
                    "release_key_id": validated.release_key_id,
                    "publication_key_id": validated.publication_key_id,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        code = str(exc)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) is None:
            code = "direct_release_admission_signing_failed"
        print(json.dumps({"ok": False, "error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
