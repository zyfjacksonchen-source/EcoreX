#!/usr/bin/env python3
"""Externally sign one exact Control Plane release-gate bundle."""

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
    build_unsigned_gate_bundle,
    sign_gate_bundle,
    validate_signed_gate_bundle,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)
from ecorex.update import (  # noqa: E402
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseManifest,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unsigned", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("release_gate_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    try:
        public_key = base64.b64decode(
            _required("ECOREX_RELEASE_SIGNER_PUBLIC_KEY"), validate=True
        )
    except (TypeError, ValueError):
        raise ValueError("release_gate_signer_public_key_invalid") from None
    executable_sha256 = _required("ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256")
    if _SHA256.fullmatch(executable_sha256) is None:
        raise ValueError("release_gate_signer_configuration_invalid")
    adapter = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None
    adapter_sha256 = os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256") or None
    try:
        return DigestPinnedExternalSigner(
            key_id=_required("ECOREX_RELEASE_SIGNER_KEY_ID"),
            public_key=public_key,
            executable_path=_required("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=adapter,
            adapter_sha256=adapter_sha256,
            environment=os.environ,
        )
    except (TypeError, ValueError):
        raise ValueError("release_gate_signer_configuration_invalid") from None


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest_payload = read_stable_regular_file(
            args.manifest,
            maximum_bytes=16 * 1024 * 1024,
            code="release_gate_manifest_invalid",
        )
        manifest_raw = strict_json_loads(
            manifest_payload, code="release_gate_manifest_invalid"
        )
        if not isinstance(manifest_raw, dict):
            raise ValueError("release_gate_manifest_invalid")
        manifest = ReleaseManifest.from_dict(manifest_raw)
        unsigned_payload = read_stable_regular_file(
            args.unsigned,
            maximum_bytes=2 * 1024 * 1024,
            code="release_gate_bundle_invalid",
        )
        unsigned = strict_json_loads(
            unsigned_payload, code="release_gate_bundle_invalid"
        )
        if not isinstance(unsigned, dict):
            raise ValueError("release_gate_bundle_invalid")
        phase = unsigned.get("phase")
        expected_gates = required_release_gates(manifest.channel)
        if manifest.channel is ReleaseChannel.STABLE and phase == "prepare":
            expected_gates -= {"bootstrap-index"}
        elif phase != "finalize":
            raise ValueError("release_gate_bundle_phase_invalid")
        gates = unsigned.get("gates")
        if not isinstance(gates, dict) or set(gates) != expected_gates:
            raise ValueError("release_gate_bundle_gate_set_invalid")
        reconstructed = build_unsigned_gate_bundle(
            phase=str(phase),
            commit_sha=str(unsigned.get("commit_sha")),
            workflow_run_id=unsigned.get("workflow_run_id"),
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
            gates=gates,
        )
        if reconstructed != unsigned:
            raise ValueError("release_gate_bundle_invalid")
        signer = _signer()
        signed = sign_gate_bundle(unsigned, signer=signer, manifest=manifest)
        validate_signed_gate_bundle(
            signed,
            manifest=manifest,
            expected_gates=frozenset(expected_gates),
            expected_phase=str(phase),
            verifier=Ed25519SignatureVerifier(
                {signer.key_id: signer.public_key_bytes}
            ),
            expected_manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        )
        write_new_json_file(
            signed, args.output.resolve(), code="release_gate_bundle_exists"
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "phase": phase,
                    "gate_count": len(expected_gates),
                    "key_id": signer.key_id,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = type(exc).__name__
        print(json.dumps({"ok": False, "error": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
