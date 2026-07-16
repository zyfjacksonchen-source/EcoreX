"""Fail-closed, single-release admission for an explicit direct rollout.

This contract is deliberately separate from the normal release gate bundle.
It never turns a protected live-acceptance waiver into ``passed``.  The
release key authenticates the bundle while the embedded operator waiver and
public readback bind the independent publication-key role.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from ecorex.update import (
    ReleaseChannel,
    ReleaseManifest,
    SignatureEnvelope,
    SignatureVerifier,
)
from .direct_operator import (
    DirectReleaseWaiverError,
    validate_direct_candidate_receipt,
    validate_direct_release_waiver,
)
from .live_acceptance import LIVE_ACCEPTANCE_GATES
from .public_index import PublicBootstrapIndexError, validate_publication_receipt
from .signing import ReleaseSigner, sign_envelope


DIRECT_ADMISSION_SCHEMA_VERSION = 1
DIRECT_ADMISSION_TYPE = "ecorex-direct-release-admission"
DIRECT_ADMISSION_DOMAIN = b"ecorex-direct-release-admission-v1\n"
DIRECT_WAIVER_EVIDENCE_PREFIX = "operator-waiver:sha256:"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GATE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_GATE_RECEIPT = re.compile(r"^gate-receipt:sha256:[0-9a-f]{64}$")
_PUBLICATION_RECEIPT = re.compile(
    r"^publication-receipt:sha256:[0-9a-f]{64}$"
)
_BOOTSTRAP_PROOF = re.compile(
    r"^bootstrap-index-proof:bread_[0-9a-f]{32}:sha256:[0-9a-f]{64}$"
)
_PUBLICATION_GATES = frozenset({"github-release", "mirror-sync", "cdn-sync"})
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_CANDIDATE_RECEIPT_BYTES = 4 * 1024 * 1024
_MAX_WAIVER_BYTES = 2 * 1024 * 1024
_MAX_PUBLICATION_RECEIPT_BYTES = 2 * 1024 * 1024


class DirectReleaseAdmissionError(ValueError):
    """The direct admission is malformed, out of scope, or untrusted."""


@dataclass(frozen=True, slots=True)
class DirectReleaseAdmissionPolicy:
    """One immutable production exception, disabled unless explicitly scoped."""

    enabled: bool = False
    release_id: str | None = None
    operator_instruction_sha256: str | None = None
    release_public_keys: Mapping[str, bytes] | None = None
    publication_public_keys: Mapping[str, bytes] | None = None

    def __post_init__(self) -> None:
        if not self.enabled:
            if any(
                value is not None
                for value in (
                    self.release_id,
                    self.operator_instruction_sha256,
                    self.release_public_keys,
                    self.publication_public_keys,
                )
            ):
                raise DirectReleaseAdmissionError(
                    "direct_release_admission_disabled_configuration_invalid"
                )
            return
        release_keys = dict(self.release_public_keys or {})
        publication_keys = dict(self.publication_public_keys or {})
        if (
            not isinstance(self.release_id, str)
            or not self.release_id
            or len(self.release_id) > 128
            or _SHA256.fullmatch(str(self.operator_instruction_sha256)) is None
            or not release_keys
            or not publication_keys
            or set(release_keys) & set(publication_keys)
            or any(
                not isinstance(key, str)
                or not isinstance(material, bytes)
                or len(material) != 32
                for ring in (release_keys, publication_keys)
                for key, material in ring.items()
            )
            or {
                hashlib.sha256(item).digest() for item in release_keys.values()
            }
            & {
                hashlib.sha256(item).digest() for item in publication_keys.values()
            }
        ):
            raise DirectReleaseAdmissionError(
                "direct_release_admission_configuration_invalid"
            )
        object.__setattr__(
            self, "release_public_keys", MappingProxyType(dict(release_keys))
        )
        object.__setattr__(
            self,
            "publication_public_keys",
            MappingProxyType(dict(publication_keys)),
        )


@dataclass(frozen=True, slots=True)
class ValidatedDirectReleaseAdmission:
    phase: str
    attestation_sha256: str
    gates: Mapping[str, Mapping[str, str]]
    candidate_receipt_sha256: str
    operator_waiver_sha256: str
    publication_receipt_sha256: str
    release_key_id: str
    publication_key_id: str


def canonical_direct_admission(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise DirectReleaseAdmissionError("direct_release_admission_invalid") from None


def direct_admission_signing_payload(unsigned: Mapping[str, object]) -> bytes:
    return DIRECT_ADMISSION_DOMAIN + canonical_direct_admission(unsigned) + b"\n"


def build_unsigned_direct_admission(
    *,
    phase: str,
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    commit_sha: str,
    operator_instruction_sha256: str,
    candidate_receipt_bytes: bytes,
    operator_waiver_bytes: bytes,
    publication_receipt_bytes: bytes,
    publication_key_id: str,
    gates: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Build a byte-bound unsigned prepare/finalize admission document."""

    if phase not in {"prepare", "finalize"}:
        raise DirectReleaseAdmissionError("direct_release_admission_phase_invalid")
    value: dict[str, object] = {
        "schema_version": DIRECT_ADMISSION_SCHEMA_VERSION,
        "attestation_type": DIRECT_ADMISSION_TYPE,
        "phase": phase,
        "release": {
            "commit_sha": commit_sha,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "channel": manifest.channel.value,
            "build_digest": manifest.build_digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "operator_instruction_sha256": operator_instruction_sha256,
        "evidence": {
            "manifest_base64": base64.b64encode(manifest_bytes).decode("ascii"),
            "candidate_receipt_sha256": hashlib.sha256(
                candidate_receipt_bytes
            ).hexdigest(),
            "candidate_receipt_base64": base64.b64encode(
                candidate_receipt_bytes
            ).decode("ascii"),
            "operator_waiver_sha256": hashlib.sha256(
                operator_waiver_bytes
            ).hexdigest(),
            "operator_waiver_base64": base64.b64encode(
                operator_waiver_bytes
            ).decode("ascii"),
            "publication_receipt_sha256": hashlib.sha256(
                publication_receipt_bytes
            ).hexdigest(),
            "publication_receipt_base64": base64.b64encode(
                publication_receipt_bytes
            ).decode("ascii"),
        },
        "key_roles": {
            "release_key_id": manifest.signature.key_id,
            "publication_key_id": publication_key_id,
        },
        "gates": {name: dict(result) for name, result in sorted(gates.items())},
    }
    # The complete cryptographic validation occurs after signing.  Keep this
    # builder strict enough that no ambiguous JSON or oversized evidence can
    # enter a signing adapter.
    _validate_envelope_shape(value, signed=False)
    return value


def sign_direct_admission(
    unsigned: Mapping[str, object],
    *,
    signer: ReleaseSigner,
    manifest: ReleaseManifest,
) -> dict[str, object]:
    if signer.key_id != manifest.signature.key_id:
        raise DirectReleaseAdmissionError(
            "direct_release_admission_release_key_role_invalid"
        )
    signature = sign_envelope(signer, direct_admission_signing_payload(unsigned))
    return {**dict(unsigned), "signature": signature.to_dict()}


def validate_signed_direct_admission(
    value: object,
    *,
    manifest: ReleaseManifest,
    expected_manifest_sha256: str,
    expected_gates: frozenset[str],
    expected_phase: str,
    policy: DirectReleaseAdmissionPolicy,
    release_verifier: SignatureVerifier,
) -> ValidatedDirectReleaseAdmission:
    """Validate every direct-release authority and return normalized facts."""

    if not policy.enabled:
        raise DirectReleaseAdmissionError("direct_release_admission_disabled")
    if not isinstance(value, dict):
        raise DirectReleaseAdmissionError("direct_release_admission_invalid")
    _validate_envelope_shape(value, signed=True)
    if expected_phase not in {"prepare", "finalize"}:
        raise DirectReleaseAdmissionError("direct_release_admission_phase_invalid")
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    release = _mapping(unsigned.get("release"), "direct_release_admission_invalid")
    if (
        unsigned.get("schema_version") != DIRECT_ADMISSION_SCHEMA_VERSION
        or unsigned.get("attestation_type") != DIRECT_ADMISSION_TYPE
        or unsigned.get("phase") != expected_phase
        or manifest.channel is not ReleaseChannel.STABLE
        or release.get("release_id") != manifest.release_id
        or release.get("release_id") != policy.release_id
        or release.get("version") != manifest.version
        or release.get("channel") != manifest.channel.value
        or release.get("build_digest") != manifest.build_digest
        or release.get("manifest_sha256") != expected_manifest_sha256
        or _COMMIT.fullmatch(str(release.get("commit_sha"))) is None
        or unsigned.get("operator_instruction_sha256")
        != policy.operator_instruction_sha256
    ):
        raise DirectReleaseAdmissionError("direct_release_admission_identity_invalid")

    release_keys = dict(policy.release_public_keys or {})
    publication_keys = dict(policy.publication_public_keys or {})
    roles = _mapping(unsigned.get("key_roles"), "direct_release_admission_invalid")
    release_key_id = str(roles.get("release_key_id"))
    publication_key_id = str(roles.get("publication_key_id"))
    release_public = release_keys.get(release_key_id)
    publication_public = publication_keys.get(publication_key_id)
    if (
        set(roles) != {"release_key_id", "publication_key_id"}
        or release_key_id != manifest.signature.key_id
        or release_public is None
        or publication_public is None
        or release_key_id == publication_key_id
        or release_public == publication_public
    ):
        raise DirectReleaseAdmissionError(
            "direct_release_admission_key_roles_invalid"
        )

    evidence = _mapping(unsigned.get("evidence"), "direct_release_admission_invalid")
    manifest_bytes = _embedded(
        evidence,
        "manifest_base64",
        None,
        _MAX_MANIFEST_BYTES,
    )
    candidate_bytes = _embedded(
        evidence,
        "candidate_receipt_base64",
        "candidate_receipt_sha256",
        _MAX_CANDIDATE_RECEIPT_BYTES,
    )
    waiver_bytes = _embedded(
        evidence,
        "operator_waiver_base64",
        "operator_waiver_sha256",
        _MAX_WAIVER_BYTES,
    )
    publication_bytes = _embedded(
        evidence,
        "publication_receipt_base64",
        "publication_receipt_sha256",
        _MAX_PUBLICATION_RECEIPT_BYTES,
    )
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256
        or _strict_json(manifest_bytes, "direct_release_manifest_invalid")
        != manifest.to_dict()
    ):
        raise DirectReleaseAdmissionError("direct_release_manifest_mismatch")
    candidate = _strict_json(
        candidate_bytes, "direct_release_candidate_receipt_invalid"
    )
    waiver = _strict_json(waiver_bytes, "direct_release_waiver_invalid")
    publication = _strict_json(
        publication_bytes, "direct_release_publication_receipt_invalid"
    )
    if not all(isinstance(item, dict) for item in (candidate, waiver, publication)):
        raise DirectReleaseAdmissionError("direct_release_admission_evidence_invalid")
    try:
        validate_direct_candidate_receipt(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            receipt=candidate,
            receipt_bytes=candidate_bytes,
            commit_sha=str(release["commit_sha"]),
            release_key_id=release_key_id,
            release_public_key=release_public,
        )
        validate_direct_release_waiver(
            waiver,
            expected_manifest=manifest,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_candidate_receipt_sha256=str(
                evidence["candidate_receipt_sha256"]
            ),
            expected_commit_sha=str(release["commit_sha"]),
            expected_operator_instruction_sha256=str(
                policy.operator_instruction_sha256
            ),
            release_public_key=release_public,
            publication_key_id=publication_key_id,
            publication_public_key=publication_public,
        )
    except DirectReleaseWaiverError as exc:
        raise DirectReleaseAdmissionError(str(exc)) from None
    try:
        validate_publication_receipt(
            manifest=manifest,
            manifest_sha256=expected_manifest_sha256,
            receipt=publication,
            receipt_sha256=str(evidence["publication_receipt_sha256"]),
        )
    except PublicBootstrapIndexError:
        raise DirectReleaseAdmissionError(
            "direct_release_publication_receipt_invalid"
        ) from None

    gates = _validate_gates(
        unsigned.get("gates"),
        expected_gates=expected_gates,
        waiver_sha256=str(evidence["operator_waiver_sha256"]),
        publication_sha256=str(evidence["publication_receipt_sha256"]),
    )
    signature_raw = value.get("signature")
    try:
        signature = SignatureEnvelope.from_dict(signature_raw)
        if signature.key_id != release_key_id:
            raise DirectReleaseAdmissionError(
                "direct_release_admission_release_key_role_invalid"
            )
        verdict = release_verifier.verify(
            direct_admission_signing_payload(unsigned), signature
        )
    except DirectReleaseAdmissionError:
        raise
    except Exception:
        raise DirectReleaseAdmissionError(
            "direct_release_admission_signature_invalid"
        ) from None
    if verdict is not True:
        raise DirectReleaseAdmissionError(
            "direct_release_admission_signature_invalid"
        )
    return ValidatedDirectReleaseAdmission(
        phase=expected_phase,
        attestation_sha256=hashlib.sha256(
            canonical_direct_admission(value)
        ).hexdigest(),
        gates=gates,
        candidate_receipt_sha256=str(evidence["candidate_receipt_sha256"]),
        operator_waiver_sha256=str(evidence["operator_waiver_sha256"]),
        publication_receipt_sha256=str(evidence["publication_receipt_sha256"]),
        release_key_id=release_key_id,
        publication_key_id=publication_key_id,
    )


def _validate_envelope_shape(value: Mapping[str, object], *, signed: bool) -> None:
    expected = {
        "schema_version",
        "attestation_type",
        "phase",
        "release",
        "operator_instruction_sha256",
        "evidence",
        "key_roles",
        "gates",
    }
    if signed:
        expected.add("signature")
    if set(value) != expected:
        raise DirectReleaseAdmissionError("direct_release_admission_invalid")
    release = _mapping(value.get("release"), "direct_release_admission_invalid")
    evidence = _mapping(value.get("evidence"), "direct_release_admission_invalid")
    if set(release) != {
        "commit_sha",
        "release_id",
        "version",
        "channel",
        "build_digest",
        "manifest_sha256",
    } or set(evidence) != {
        "manifest_base64",
        "candidate_receipt_sha256",
        "candidate_receipt_base64",
        "operator_waiver_sha256",
        "operator_waiver_base64",
        "publication_receipt_sha256",
        "publication_receipt_base64",
    }:
        raise DirectReleaseAdmissionError("direct_release_admission_invalid")
    for name in (
        "candidate_receipt_sha256",
        "operator_waiver_sha256",
        "publication_receipt_sha256",
    ):
        if _SHA256.fullmatch(str(evidence.get(name))) is None:
            raise DirectReleaseAdmissionError("direct_release_admission_invalid")
    # Decode here as an early signing-boundary size check too.
    _embedded(evidence, "manifest_base64", None, _MAX_MANIFEST_BYTES)
    _embedded(
        evidence,
        "candidate_receipt_base64",
        "candidate_receipt_sha256",
        _MAX_CANDIDATE_RECEIPT_BYTES,
    )
    _embedded(
        evidence,
        "operator_waiver_base64",
        "operator_waiver_sha256",
        _MAX_WAIVER_BYTES,
    )
    _embedded(
        evidence,
        "publication_receipt_base64",
        "publication_receipt_sha256",
        _MAX_PUBLICATION_RECEIPT_BYTES,
    )


def _validate_gates(
    value: object,
    *,
    expected_gates: frozenset[str],
    waiver_sha256: str,
    publication_sha256: str,
) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != expected_gates:
        raise DirectReleaseAdmissionError("direct_release_admission_gate_set_invalid")
    normalized: dict[str, dict[str, str]] = {}
    publication_token = f"publication-receipt:sha256:{publication_sha256}"
    waiver_token = f"{DIRECT_WAIVER_EVIDENCE_PREFIX}{waiver_sha256}"
    for gate, raw in value.items():
        if not isinstance(gate, str) or _GATE.fullmatch(gate) is None:
            raise DirectReleaseAdmissionError("direct_release_admission_gate_invalid")
        if not isinstance(raw, Mapping) or set(raw) != {"status", "evidence"}:
            raise DirectReleaseAdmissionError("direct_release_admission_gate_invalid")
        status = raw.get("status")
        evidence = raw.get("evidence")
        if not isinstance(evidence, str):
            raise DirectReleaseAdmissionError("direct_release_admission_gate_invalid")
        if gate in LIVE_ACCEPTANCE_GATES:
            if status != "waived" or evidence != waiver_token:
                raise DirectReleaseAdmissionError(
                    "direct_release_live_acceptance_false_pass"
                )
        elif status != "passed":
            raise DirectReleaseAdmissionError(
                "direct_release_required_gate_not_passed"
            )
        elif gate in _PUBLICATION_GATES:
            if evidence != publication_token or _PUBLICATION_RECEIPT.fullmatch(
                evidence
            ) is None:
                raise DirectReleaseAdmissionError(
                    "direct_release_publication_gate_invalid"
                )
        elif gate == "bootstrap-index":
            if _BOOTSTRAP_PROOF.fullmatch(evidence) is None:
                raise DirectReleaseAdmissionError(
                    "direct_release_bootstrap_proof_invalid"
                )
        elif _GATE_RECEIPT.fullmatch(evidence) is None:
            raise DirectReleaseAdmissionError("direct_release_admission_gate_invalid")
        normalized[str(gate)] = {
            "status": str(status),
            "evidence": evidence,
        }
    return normalized


def _embedded(
    value: Mapping[str, Any],
    encoded_name: str,
    digest_name: str | None,
    maximum_bytes: int,
) -> bytes:
    try:
        payload = base64.b64decode(value.get(encoded_name), validate=True)
    except (TypeError, ValueError):
        raise DirectReleaseAdmissionError("direct_release_admission_evidence_invalid") from None
    if not 1 <= len(payload) <= maximum_bytes:
        raise DirectReleaseAdmissionError("direct_release_admission_evidence_invalid")
    if digest_name is not None and hashlib.sha256(payload).hexdigest() != value.get(
        digest_name
    ):
        raise DirectReleaseAdmissionError("direct_release_admission_evidence_invalid")
    return payload


def _strict_json(payload: bytes, code: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(code)
            result[key] = item
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError(code)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DirectReleaseAdmissionError(code) from None


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectReleaseAdmissionError(code)
    return value


__all__ = [
    "DIRECT_ADMISSION_DOMAIN",
    "DIRECT_ADMISSION_SCHEMA_VERSION",
    "DIRECT_ADMISSION_TYPE",
    "DIRECT_WAIVER_EVIDENCE_PREFIX",
    "DirectReleaseAdmissionError",
    "DirectReleaseAdmissionPolicy",
    "ValidatedDirectReleaseAdmission",
    "build_unsigned_direct_admission",
    "canonical_direct_admission",
    "direct_admission_signing_payload",
    "sign_direct_admission",
    "validate_signed_direct_admission",
]
