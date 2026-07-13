"""Signed, Candidate-bound authority for Control Plane release gates.

CI gate receipts are useful only if the Control Plane can prove that their
digest set was approved by the same release-signing authority as the immutable
manifest.  This module supplies one domain-separated bundle.  Administrator
text and receipt-looking strings are deliberately insufficient.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from ecorex.update import ReleaseManifest, SignatureEnvelope, SignatureVerifier
from ecorex.update.verification import SignatureVerificationError

from .signing import ReleaseSigner, sign_envelope


GATE_BUNDLE_TYPE = "ecorex-release-gate-bundle"
GATE_BUNDLE_DOMAIN = b"ecorex-release-gate-bundle-v1\n"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GATE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_GATE_RECEIPT = re.compile(r"^gate-receipt:sha256:[0-9a-f]{64}$")
_PUBLICATION_RECEIPT = re.compile(r"^publication-receipt:sha256:[0-9a-f]{64}$")
_BOOTSTRAP_PROOF = re.compile(
    r"^bootstrap-index-proof:bread_[0-9a-f]{32}:sha256:[0-9a-f]{64}$"
)
_PUBLICATION_GATES = frozenset({"github-release", "mirror-sync", "cdn-sync"})


class GateAttestationError(ValueError):
    """A gate bundle is malformed, incomplete, drifted, or untrusted."""


def canonical_gate_bundle(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise GateAttestationError("release_gate_bundle_invalid") from None


def gate_bundle_signing_payload(unsigned: Mapping[str, object]) -> bytes:
    return GATE_BUNDLE_DOMAIN + canonical_gate_bundle(unsigned) + b"\n"


def build_unsigned_gate_bundle(
    *,
    phase: str,
    commit_sha: str,
    workflow_run_id: int,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    gates: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "attestation_type": GATE_BUNDLE_TYPE,
        "phase": phase,
        "commit_sha": commit_sha,
        "workflow_run_id": workflow_run_id,
        "release_id": manifest.release_id,
        "version": manifest.version,
        "channel": manifest.channel.value,
        "build_digest": manifest.build_digest,
        "manifest_sha256": manifest_sha256,
        "gates": {name: dict(result) for name, result in sorted(gates.items())},
    }
    _validate_unsigned(
        value,
        manifest=manifest,
        expected_gates=frozenset(gates),
        expected_phase=phase,
        expected_manifest_sha256=manifest_sha256,
    )
    return value


def sign_gate_bundle(
    unsigned: Mapping[str, object],
    *,
    signer: ReleaseSigner,
    manifest: ReleaseManifest,
) -> dict[str, object]:
    payload = gate_bundle_signing_payload(unsigned)
    signature = sign_envelope(signer, payload)
    if signature.key_id != manifest.signature.key_id:
        raise GateAttestationError("release_gate_bundle_signer_mismatch")
    return {**dict(unsigned), "signature": signature.to_dict()}


def validate_signed_gate_bundle(
    value: object,
    *,
    manifest: ReleaseManifest,
    expected_gates: frozenset[str],
    expected_phase: str,
    verifier: SignatureVerifier,
    expected_manifest_sha256: str | None = None,
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "attestation_type",
        "phase",
        "commit_sha",
        "workflow_run_id",
        "release_id",
        "version",
        "channel",
        "build_digest",
        "manifest_sha256",
        "gates",
        "signature",
    }:
        raise GateAttestationError("release_gate_bundle_invalid")
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    gates = _validate_unsigned(
        unsigned,
        manifest=manifest,
        expected_gates=expected_gates,
        expected_phase=expected_phase,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    signature_raw = value.get("signature")
    if not isinstance(signature_raw, dict):
        raise GateAttestationError("release_gate_bundle_signature_invalid")
    try:
        signature = SignatureEnvelope.from_dict(signature_raw)
    except (TypeError, ValueError):
        raise GateAttestationError("release_gate_bundle_signature_invalid") from None
    if signature.key_id != manifest.signature.key_id:
        raise GateAttestationError("release_gate_bundle_signer_mismatch")
    try:
        verdict = verifier.verify(gate_bundle_signing_payload(unsigned), signature)
    except Exception as exc:
        if isinstance(exc, SignatureVerificationError):
            raise GateAttestationError("release_gate_bundle_signature_invalid") from None
        raise GateAttestationError("release_gate_bundle_verifier_failed") from None
    if verdict is not True:
        raise GateAttestationError("release_gate_bundle_signature_invalid")
    return gates


def gate_bundle_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_gate_bundle(value)).hexdigest()


def _validate_unsigned(
    value: Mapping[str, object],
    *,
    manifest: ReleaseManifest,
    expected_gates: frozenset[str],
    expected_phase: str,
    expected_manifest_sha256: str | None,
) -> dict[str, dict[str, str]]:
    if set(value) != {
        "schema_version",
        "attestation_type",
        "phase",
        "commit_sha",
        "workflow_run_id",
        "release_id",
        "version",
        "channel",
        "build_digest",
        "manifest_sha256",
        "gates",
    }:
        raise GateAttestationError("release_gate_bundle_invalid")
    if (
        value.get("schema_version") != 1
        or value.get("attestation_type") != GATE_BUNDLE_TYPE
        or expected_phase not in {"prepare", "finalize"}
        or value.get("phase") != expected_phase
        or not isinstance(value.get("commit_sha"), str)
        or _COMMIT.fullmatch(str(value.get("commit_sha"))) is None
        or isinstance(value.get("workflow_run_id"), bool)
        or not isinstance(value.get("workflow_run_id"), int)
        or int(value["workflow_run_id"]) < 1
        or value.get("release_id") != manifest.release_id
        or value.get("version") != manifest.version
        or value.get("channel") != manifest.channel.value
        or value.get("build_digest") != manifest.build_digest
        or not isinstance(value.get("manifest_sha256"), str)
        or _SHA256.fullmatch(str(value.get("manifest_sha256"))) is None
        or (
            expected_manifest_sha256 is not None
            and value.get("manifest_sha256") != expected_manifest_sha256
        )
    ):
        raise GateAttestationError("release_gate_bundle_identity_invalid")
    gates_raw = value.get("gates")
    if not isinstance(gates_raw, dict) or set(gates_raw) != expected_gates:
        raise GateAttestationError("release_gate_bundle_gate_set_invalid")
    publication_tokens: set[str] = set()
    normalized: dict[str, dict[str, str]] = {}
    for gate, result in gates_raw.items():
        if not isinstance(gate, str) or _GATE.fullmatch(gate) is None:
            raise GateAttestationError("release_gate_bundle_gate_invalid")
        if (
            not isinstance(result, dict)
            or set(result) != {"status", "evidence"}
            or result.get("status") != "passed"
            or not isinstance(result.get("evidence"), str)
        ):
            raise GateAttestationError("release_gate_bundle_gate_invalid")
        evidence = str(result["evidence"])
        if gate in _PUBLICATION_GATES:
            if _PUBLICATION_RECEIPT.fullmatch(evidence) is None:
                raise GateAttestationError("release_gate_bundle_gate_invalid")
            publication_tokens.add(evidence)
        elif gate == "bootstrap-index":
            if _BOOTSTRAP_PROOF.fullmatch(evidence) is None:
                raise GateAttestationError("release_gate_bundle_gate_invalid")
        elif _GATE_RECEIPT.fullmatch(evidence) is None:
            raise GateAttestationError("release_gate_bundle_gate_invalid")
        normalized[gate] = {"status": "passed", "evidence": evidence}
    if publication_tokens and len(publication_tokens) != 1:
        raise GateAttestationError("release_gate_bundle_publication_drift")
    return normalized


__all__ = [
    "GATE_BUNDLE_DOMAIN",
    "GATE_BUNDLE_TYPE",
    "GateAttestationError",
    "build_unsigned_gate_bundle",
    "canonical_gate_bundle",
    "gate_bundle_sha256",
    "gate_bundle_signing_payload",
    "sign_gate_bundle",
    "validate_signed_gate_bundle",
]
