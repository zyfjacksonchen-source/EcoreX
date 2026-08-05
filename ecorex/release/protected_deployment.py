"""Signed, generic production-deployment admission.

The admission is the only object allowed to cross from the protected Candidate
workflow into a production deployment workflow.  It binds immutable Candidate,
cloud, public-site and acceptance evidence without carrying runner paths or
provider credentials.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from ecorex.product_version import is_stable_release_version


DOMAIN = b"ecorex.protected-deployment-admission.v1\0"
DOCUMENT_TYPE = "ecorex.protected-deployment-admission"
SCHEMA_VERSION = 1
PUBLIC_SITE_DOMAIN = b"ecorex.public-site-deployment.v2\0"
PUBLIC_SITE_DOCUMENT_TYPE = "ecorex.public-site-deployment-authorization"
PUBLIC_SITE_SCHEMA_VERSION = 2

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CHANNELS = frozenset({"canary", "stable"})
_MODES = frozenset({"create", "create-and-activate"})
_TARGETS = ("cloud", "control_plane", "public_site")
_GATES = (
    "candidate",
    "cdp_acceptance",
    "image_soak",
    "live_image",
    "live_model",
    "signature",
)


class ProtectedDeploymentAdmissionError(ValueError):
    """Stable fail-closed admission error."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_invalid"
        ) from None


def _timestamp(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProtectedDeploymentAdmissionError(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProtectedDeploymentAdmissionError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProtectedDeploymentAdmissionError(code)
    return parsed


def _digest(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProtectedDeploymentAdmissionError(code)
    return value


def validate_admission_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "admission_id",
        "repository",
        "commit_sha",
        "channel",
        "candidate",
        "gates",
        "targets",
        "decision",
        "issued_at",
        "expires_at",
    }:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_invalid"
        )
    if (
        not isinstance(value["admission_id"], str)
        or _IDENTITY.fullmatch(value["admission_id"]) is None
        or not isinstance(value["repository"], str)
        or re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["repository"]
        )
        is None
        or not isinstance(value["commit_sha"], str)
        or _COMMIT.fullmatch(value["commit_sha"]) is None
        or value["channel"] not in _CHANNELS
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_identity_invalid"
        )
    candidate = value["candidate"]
    if not isinstance(candidate, Mapping) or set(candidate) != {
        "workflow_run_id",
        "run_attempt",
        "artifact_id",
        "artifact_sha256",
        "release_id",
        "version",
        "build_digest",
    }:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_candidate_invalid"
        )
    if (
        not isinstance(candidate["workflow_run_id"], int)
        or candidate["workflow_run_id"] <= 0
        or not isinstance(candidate["run_attempt"], int)
        or candidate["run_attempt"] <= 0
        or not isinstance(candidate["artifact_id"], int)
        or candidate["artifact_id"] <= 0
        or not isinstance(candidate["release_id"], str)
        or _IDENTITY.fullmatch(candidate["release_id"]) is None
        or not is_stable_release_version(candidate["version"])
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_candidate_invalid"
        )
    _digest(candidate["artifact_sha256"], code="protected_deployment_candidate_invalid")
    _digest(candidate["build_digest"], code="protected_deployment_candidate_invalid")

    gates = value["gates"]
    if not isinstance(gates, Mapping) or tuple(sorted(gates)) != _GATES:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_gate_set_invalid"
        )
    for digest in gates.values():
        _digest(digest, code="protected_deployment_gate_set_invalid")

    targets = value["targets"]
    if not isinstance(targets, Mapping) or tuple(sorted(targets)) != _TARGETS:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_target_set_invalid"
        )
    expected_target_keys = {
        "cloud": {"artifact_sha256", "manifest_sha256"},
        "control_plane": {"release_manifest_sha256"},
        "public_site": {"tree_sha256", "public_index_sha256"},
    }
    for name, keys in expected_target_keys.items():
        target = targets[name]
        if not isinstance(target, Mapping) or set(target) != keys:
            raise ProtectedDeploymentAdmissionError(
                "protected_deployment_target_set_invalid"
            )
        for digest in target.values():
            _digest(digest, code="protected_deployment_target_set_invalid")

    decision = value["decision"]
    if (
        not isinstance(decision, Mapping)
        or set(decision) != {"mode", "rollout_percentage"}
        or decision["mode"] not in _MODES
        or not isinstance(decision["rollout_percentage"], int)
        or not 1 <= decision["rollout_percentage"] <= 100
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_decision_invalid"
        )
    issued = _timestamp(
        value["issued_at"], code="protected_deployment_admission_time_invalid"
    )
    expires = _timestamp(
        value["expires_at"], code="protected_deployment_admission_time_invalid"
    )
    if expires <= issued or (expires - issued).total_seconds() > 86_400:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_time_invalid"
        )
    return json.loads(canonical_json(value))


def admission_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    return DOMAIN + canonical_json(validate_admission_payload(payload))


def sign_admission(payload: Mapping[str, Any], *, signer: Any) -> dict[str, Any]:
    body = validate_admission_payload(payload)
    key_id = getattr(signer, "key_id", None)
    if not isinstance(key_id, str) or _IDENTITY.fullmatch(key_id) is None:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_signer_invalid"
        )
    try:
        signature = signer.sign(DOMAIN + canonical_json(body))
    except Exception:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_signing_failed"
        ) from None
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_signature_invalid"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "admission": body,
        "signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }


def verify_admission(
    document: Mapping[str, Any],
    *,
    public_keys: Mapping[str, bytes],
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(document, Mapping)
        or set(document)
        != {"schema_version", "document_type", "admission", "signature"}
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("document_type") != DOCUMENT_TYPE
        or not isinstance(document.get("admission"), Mapping)
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_invalid"
        )
    body = validate_admission_payload(document["admission"])
    signature = document.get("signature")
    if (
        not isinstance(signature, Mapping)
        or set(signature) != {"algorithm", "key_id", "value"}
        or signature.get("algorithm") != "ed25519"
        or not isinstance(signature.get("key_id"), str)
        or not isinstance(signature.get("value"), str)
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_signature_invalid"
        )
    public = public_keys.get(str(signature["key_id"]))
    try:
        raw = base64.b64decode(str(signature["value"]), validate=True)
        if public is None or len(public) != 32 or len(raw) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(
            raw, DOMAIN + canonical_json(body)
        )
    except (InvalidSignature, TypeError, ValueError):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_signature_rejected"
        ) from None
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_time_invalid"
        )
    if instant >= _timestamp(
        body["expires_at"], code="protected_deployment_admission_time_invalid"
    ):
        raise ProtectedDeploymentAdmissionError(
            "protected_deployment_admission_expired"
        )
    return body


def admission_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document) + b"\n").hexdigest()


def public_site_v2_payload(
    *,
    admission: Mapping[str, Any],
    admission_digest: str,
    release_id: str,
    site_tree_sha256: str,
    public_index_sha256: str,
    admin_identity_sha256: str,
) -> dict[str, Any]:
    body = validate_admission_payload(admission)
    for value in (
        admission_digest,
        site_tree_sha256,
        public_index_sha256,
        admin_identity_sha256,
    ):
        _digest(value, code="public_site_v2_authorization_invalid")
    if (
        release_id != body["candidate"]["release_id"]
        or site_tree_sha256 != body["targets"]["public_site"]["tree_sha256"]
        or public_index_sha256
        != body["targets"]["public_site"]["public_index_sha256"]
    ):
        raise ProtectedDeploymentAdmissionError(
            "public_site_v2_authorization_identity_mismatch"
        )
    return {
        "release_id": release_id,
        "version": body["candidate"]["version"],
        "admission_id": body["admission_id"],
        "admission_sha256": admission_digest,
        "site_tree_sha256": site_tree_sha256,
        "public_index_sha256": public_index_sha256,
        "admin_identity_sha256": admin_identity_sha256,
    }


__all__ = [
    "DOMAIN",
    "DOCUMENT_TYPE",
    "PUBLIC_SITE_DOCUMENT_TYPE",
    "PUBLIC_SITE_DOMAIN",
    "PUBLIC_SITE_SCHEMA_VERSION",
    "ProtectedDeploymentAdmissionError",
    "admission_sha256",
    "admission_signing_bytes",
    "canonical_json",
    "public_site_v2_payload",
    "sign_admission",
    "validate_admission_payload",
    "verify_admission",
]
