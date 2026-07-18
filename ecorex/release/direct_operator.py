"""Signed, release-scoped evidence for an explicit direct-release waiver.

The waiver never converts a skipped protected gate into a pass.  It binds the
operator instruction hash to one exact signed Candidate and records the
non-waivable controls that the existing Candidate builder already verified.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ecorex.update import ReleaseChannel, ReleaseManifest, SignatureEnvelope

from .candidate import candidate_receipt_signing_payload
from .publication_policy import (
    publication_receipt_policy,
    required_publication_source_ids,
)
from .signing import ReleaseSigner, sign_envelope


DIRECT_RELEASE_WAIVER_SCHEMA_VERSION = 1
DIRECT_RELEASE_WAIVER_TYPE = "ecorex.direct-release.operator-waiver"
DIRECT_RELEASE_WAIVER_DOMAIN = b"ecorex.direct-release.operator-waiver.v1\0"
EXTERNAL_PUBLIC_KEY_DESCRIPTION_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KEY_ROLE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WAIVED_GATES: Mapping[str, str] = {
    "github-protected-environments": "not-run",
    "isolated-release-runners": "not-run",
    "kms-hsm-release-signing": (
        "substituted-by-dpapi-and-attested-encrypted-volume-software-keys"
    ),
    "managed-live-cdp-acceptance": "not-run",
    "protected-publication-workflow": "not-run",
}
_VERIFIED_REQUIREMENTS: Mapping[str, str] = {
    "exact-main-source": "verified",
    "windows-x64-platform-stage": "verified-by-candidate",
    "macos-arm64-platform-stage": "verified-by-candidate",
    "macos-x64-platform-stage": "verified-by-candidate",
    "stage-gate-evidence": "verified-by-candidate",
    "signed-manifest-and-artifacts": "verified-by-candidate",
    "independent-release-and-publication-keys": "required",
    "required-publication-before-live-pointer": "required",
}


class DirectReleaseWaiverError(ValueError):
    """One direct-release waiver is malformed, stale or unauthenticated."""


def parse_external_public_key_description(
    value: Mapping[str, Any], *, expected_role: str
) -> tuple[str, bytes]:
    """Parse a public-only handoff from the production signing boundary."""

    if not isinstance(expected_role, str) or _KEY_ROLE.fullmatch(expected_role) is None:
        raise DirectReleaseWaiverError("direct_release_public_key_role_invalid")
    root = _mapping(value, "direct_release_public_key_description_invalid")
    if (
        set(root)
        != {
            "schema_version",
            "role",
            "algorithm",
            "key_id",
            "public_key_base64",
            "public_key_sha256",
        }
        or root.get("schema_version")
        != EXTERNAL_PUBLIC_KEY_DESCRIPTION_SCHEMA_VERSION
        or root.get("role") != expected_role
        or root.get("algorithm") != "ed25519"
        or not isinstance(root.get("key_id"), str)
        or _KEY_ID.fullmatch(root["key_id"]) is None
        or not isinstance(root.get("public_key_sha256"), str)
        or _SHA256.fullmatch(root["public_key_sha256"]) is None
    ):
        raise DirectReleaseWaiverError(
            "direct_release_public_key_description_invalid"
        )
    try:
        public = base64.b64decode(root.get("public_key_base64"), validate=True)
    except (TypeError, ValueError):
        raise DirectReleaseWaiverError(
            "direct_release_public_key_description_invalid"
        ) from None
    public = _public_key(public, expected_role)
    if hashlib.sha256(public).hexdigest() != root["public_key_sha256"]:
        raise DirectReleaseWaiverError(
            "direct_release_public_key_description_invalid"
        )
    return str(root["key_id"]), public


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise DirectReleaseWaiverError("direct_release_waiver_json_invalid") from None


def direct_release_waiver_signing_payload(value: Mapping[str, Any]) -> bytes:
    unsigned = dict(value)
    unsigned.pop("signature", None)
    return DIRECT_RELEASE_WAIVER_DOMAIN + _canonical_json(unsigned) + b"\n"


def build_direct_release_waiver(
    *,
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    candidate_receipt: Mapping[str, Any],
    candidate_receipt_bytes: bytes,
    commit_sha: str,
    operator_instruction_sha256: str,
    signer: ReleaseSigner,
    signer_public_key: bytes,
    publication_key_id: str,
    publication_public_key: bytes,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create and self-verify one immutable, explicitly non-PASS waiver."""

    if not isinstance(manifest, ReleaseManifest):
        raise TypeError("manifest must be a ReleaseManifest")
    if manifest.channel is not ReleaseChannel.STABLE:
        raise DirectReleaseWaiverError("direct_release_stable_channel_required")
    if _COMMIT.fullmatch(commit_sha) is None:
        raise DirectReleaseWaiverError("direct_release_commit_invalid")
    if _SHA256.fullmatch(operator_instruction_sha256) is None:
        raise DirectReleaseWaiverError("direct_release_instruction_hash_invalid")
    release_public = _public_key(signer_public_key, "release")
    publication_public = _public_key(publication_public_key, "publication")
    if signer.key_id == publication_key_id or release_public == publication_public:
        raise DirectReleaseWaiverError("direct_release_keys_not_independent")
    _verify_manifest_bytes(manifest, manifest_bytes)
    validate_direct_candidate_receipt(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        receipt=candidate_receipt,
        receipt_bytes=candidate_receipt_bytes,
        commit_sha=commit_sha,
        release_key_id=signer.key_id,
        release_public_key=release_public,
    )
    issued_at = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(issued_at, str) or not issued_at:
        raise DirectReleaseWaiverError("direct_release_created_at_invalid")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    receipt_sha256 = hashlib.sha256(candidate_receipt_bytes).hexdigest()
    value: dict[str, Any] = {
        "schema_version": DIRECT_RELEASE_WAIVER_SCHEMA_VERSION,
        "evidence_type": DIRECT_RELEASE_WAIVER_TYPE,
        "status": "operator-waived",
        "scope": "single-release",
        "created_at": issued_at,
        "operator_instruction_sha256": operator_instruction_sha256,
        "release": {
            "commit_sha": commit_sha,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "channel": manifest.channel.value,
            "build_digest": manifest.build_digest,
            "manifest_sha256": manifest_sha256,
            "candidate_receipt_sha256": receipt_sha256,
        },
        "protected_pipeline": {
            "status": "not-run-by-explicit-operator-waiver",
            "represented_as_passed": False,
            "gates": {
                gate: {"status": status, "represented_as_passed": False}
                for gate, status in sorted(_WAIVED_GATES.items())
            },
        },
        "verified_requirements": dict(sorted(_VERIFIED_REQUIREMENTS.items())),
        "publication": {
            "status": "not-yet-published",
            "live_pointer_authorized": False,
            "required_publication_policy": publication_receipt_policy(manifest),
            "required_source_ids": list(
                sorted(required_publication_source_ids(manifest))
            ),
            "requires_published_signed_index": True,
        },
        "signing": {
            "algorithm": "ed25519",
            "release_key_id": signer.key_id,
            "release_public_key_sha256": hashlib.sha256(release_public).hexdigest(),
            "publication_key_id": publication_key_id,
            "publication_public_key_sha256": hashlib.sha256(
                publication_public
            ).hexdigest(),
            "release_private_key_storage": "windows-dpapi-current-user",
            "publication_private_key_storage": (
                "attested-encrypted-volume-software-key"
            ),
            "normal_kms_hsm_gate_passed": False,
        },
    }
    value["signature"] = sign_envelope(
        signer, direct_release_waiver_signing_payload(value)
    ).to_dict()
    validate_direct_release_waiver(
        value,
        expected_manifest=manifest,
        expected_manifest_sha256=manifest_sha256,
        expected_candidate_receipt_sha256=receipt_sha256,
        expected_commit_sha=commit_sha,
        expected_operator_instruction_sha256=operator_instruction_sha256,
        release_public_key=release_public,
        publication_key_id=publication_key_id,
        publication_public_key=publication_public,
    )
    return value


def validate_direct_release_waiver(
    value: Mapping[str, Any],
    *,
    expected_manifest: ReleaseManifest,
    expected_manifest_sha256: str,
    expected_candidate_receipt_sha256: str,
    expected_commit_sha: str,
    expected_operator_instruction_sha256: str,
    release_public_key: bytes,
    publication_key_id: str,
    publication_public_key: bytes,
) -> None:
    """Reject a waiver that widens scope or describes a skipped gate as PASS."""

    root = _mapping(value, "direct_release_waiver_invalid")
    expected_root = {
        "schema_version",
        "evidence_type",
        "status",
        "scope",
        "created_at",
        "operator_instruction_sha256",
        "release",
        "protected_pipeline",
        "verified_requirements",
        "publication",
        "signing",
        "signature",
    }
    if (
        set(root) != expected_root
        or root.get("schema_version") != DIRECT_RELEASE_WAIVER_SCHEMA_VERSION
        or root.get("evidence_type") != DIRECT_RELEASE_WAIVER_TYPE
        or root.get("status") != "operator-waived"
        or root.get("scope") != "single-release"
        or not isinstance(root.get("created_at"), str)
        or root.get("operator_instruction_sha256")
        != expected_operator_instruction_sha256
    ):
        raise DirectReleaseWaiverError("direct_release_waiver_invalid")
    release = _mapping(root.get("release"), "direct_release_waiver_invalid")
    expected_release = {
        "commit_sha": expected_commit_sha,
        "release_id": expected_manifest.release_id,
        "version": expected_manifest.version,
        "channel": expected_manifest.channel.value,
        "build_digest": expected_manifest.build_digest,
        "manifest_sha256": expected_manifest_sha256,
        "candidate_receipt_sha256": expected_candidate_receipt_sha256,
    }
    if release != expected_release:
        raise DirectReleaseWaiverError("direct_release_waiver_release_mismatch")
    pipeline = _mapping(
        root.get("protected_pipeline"), "direct_release_waiver_invalid"
    )
    if (
        set(pipeline) != {"status", "represented_as_passed", "gates"}
        or pipeline.get("status") != "not-run-by-explicit-operator-waiver"
        or pipeline.get("represented_as_passed") is not False
    ):
        raise DirectReleaseWaiverError("direct_release_waiver_false_pass")
    gates = _mapping(pipeline.get("gates"), "direct_release_waiver_invalid")
    if set(gates) != set(_WAIVED_GATES):
        raise DirectReleaseWaiverError("direct_release_waiver_scope_invalid")
    for gate, status in _WAIVED_GATES.items():
        gate_value = _mapping(gates.get(gate), "direct_release_waiver_invalid")
        if gate_value != {"status": status, "represented_as_passed": False}:
            raise DirectReleaseWaiverError("direct_release_waiver_false_pass")
    if root.get("verified_requirements") != dict(sorted(_VERIFIED_REQUIREMENTS.items())):
        raise DirectReleaseWaiverError("direct_release_waiver_requirements_invalid")
    if root.get("publication") != {
        "status": "not-yet-published",
        "live_pointer_authorized": False,
        "required_publication_policy": publication_receipt_policy(expected_manifest),
        "required_source_ids": list(
            sorted(required_publication_source_ids(expected_manifest))
        ),
        "requires_published_signed_index": True,
    }:
        raise DirectReleaseWaiverError("direct_release_waiver_publication_invalid")
    release_public = _public_key(release_public_key, "release")
    publication_public = _public_key(publication_public_key, "publication")
    signing = _mapping(root.get("signing"), "direct_release_waiver_invalid")
    expected_signing = {
        "algorithm": "ed25519",
        "release_key_id": expected_manifest.signature.key_id,
        "release_public_key_sha256": hashlib.sha256(release_public).hexdigest(),
        "publication_key_id": publication_key_id,
        "publication_public_key_sha256": hashlib.sha256(
            publication_public
        ).hexdigest(),
        "release_private_key_storage": "windows-dpapi-current-user",
        "publication_private_key_storage": (
            "attested-encrypted-volume-software-key"
        ),
        "normal_kms_hsm_gate_passed": False,
    }
    if signing != expected_signing:
        raise DirectReleaseWaiverError("direct_release_waiver_signing_invalid")
    if (
        expected_manifest.signature.key_id == publication_key_id
        or release_public == publication_public
    ):
        raise DirectReleaseWaiverError("direct_release_keys_not_independent")
    try:
        envelope = SignatureEnvelope.from_dict(root.get("signature"))
        if envelope.key_id != expected_manifest.signature.key_id:
            raise DirectReleaseWaiverError("direct_release_waiver_signature_invalid")
        signature = base64.b64decode(envelope.value, validate=True)
        Ed25519PublicKey.from_public_bytes(release_public).verify(
            signature, direct_release_waiver_signing_payload(root)
        )
    except DirectReleaseWaiverError:
        raise
    except (TypeError, ValueError, InvalidSignature):
        raise DirectReleaseWaiverError(
            "direct_release_waiver_signature_invalid"
        ) from None


def _verify_manifest_bytes(manifest: ReleaseManifest, payload: bytes) -> None:
    if not isinstance(payload, bytes) or not payload:
        raise DirectReleaseWaiverError("direct_release_manifest_invalid")
    try:
        parsed = ReleaseManifest.from_json(payload)
    except Exception:
        raise DirectReleaseWaiverError("direct_release_manifest_invalid") from None
    if parsed.to_dict() != manifest.to_dict():
        raise DirectReleaseWaiverError("direct_release_manifest_mismatch")


def validate_direct_candidate_receipt(
    *,
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    receipt: Mapping[str, Any],
    receipt_bytes: bytes,
    commit_sha: str,
    release_key_id: str,
    release_public_key: bytes,
) -> None:
    if not isinstance(receipt_bytes, bytes) or not receipt_bytes:
        raise DirectReleaseWaiverError("direct_release_candidate_receipt_invalid")
    try:
        parsed = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise DirectReleaseWaiverError(
            "direct_release_candidate_receipt_invalid"
        ) from None
    if parsed != receipt:
        raise DirectReleaseWaiverError("direct_release_candidate_receipt_mismatch")
    expected_keys = {
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
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version") != 2
        or receipt.get("receipt_type") != "ecorex-candidate-build"
        or receipt.get("status") != "passed"
        or receipt.get("code") is not None
        or receipt.get("commit_sha") != commit_sha
        or receipt.get("release_id") != manifest.release_id
        or receipt.get("version") != manifest.version
        or receipt.get("channel") != manifest.channel.value
        or receipt.get("build_digest") != manifest.build_digest
        or receipt.get("manifest_sha256") != manifest_sha256
    ):
        raise DirectReleaseWaiverError("direct_release_candidate_receipt_mismatch")
    signing = receipt.get("signing")
    if (
        not isinstance(signing, Mapping)
        or signing.get("algorithm") != "ed25519"
        or signing.get("key_id") != release_key_id
        or isinstance(signing.get("operation_count"), bool)
        or not isinstance(signing.get("operation_count"), int)
        or signing["operation_count"] < 2
    ):
        raise DirectReleaseWaiverError("direct_release_candidate_receipt_mismatch")
    try:
        signature = SignatureEnvelope.from_dict(receipt.get("signature"))
        if signature.key_id != release_key_id:
            raise DirectReleaseWaiverError(
                "direct_release_candidate_receipt_signature_invalid"
            )
        raw = base64.b64decode(signature.value, validate=True)
        Ed25519PublicKey.from_public_bytes(release_public_key).verify(
            raw, candidate_receipt_signing_payload(receipt)
        )
    except DirectReleaseWaiverError:
        raise
    except (TypeError, ValueError, InvalidSignature):
        raise DirectReleaseWaiverError(
            "direct_release_candidate_receipt_signature_invalid"
        ) from None


def _public_key(value: bytes, role: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise DirectReleaseWaiverError(f"direct_release_{role}_public_key_invalid")
    try:
        Ed25519PublicKey.from_public_bytes(value)
    except ValueError:
        raise DirectReleaseWaiverError(
            f"direct_release_{role}_public_key_invalid"
        ) from None
    return value


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectReleaseWaiverError(code)
    return value


__all__ = [
    "DIRECT_RELEASE_WAIVER_DOMAIN",
    "DIRECT_RELEASE_WAIVER_SCHEMA_VERSION",
    "DIRECT_RELEASE_WAIVER_TYPE",
    "EXTERNAL_PUBLIC_KEY_DESCRIPTION_SCHEMA_VERSION",
    "DirectReleaseWaiverError",
    "build_direct_release_waiver",
    "direct_release_waiver_signing_payload",
    "parse_external_public_key_description",
    "validate_direct_candidate_receipt",
    "validate_direct_release_waiver",
]
