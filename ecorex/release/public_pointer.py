"""Fail-closed publication transport for the public Bootstrap pointer.

The discovery document is intentionally untrusted, but replacing it is still a
release mutation.  This transport requires a remote stage/activate protocol and
then reads the public object back byte-for-byte before producing evidence that
can be used by the stable promotion gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

import httpx
from ecorex.update import SignatureVerifier

from .public_index import (
    MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES,
    PUBLIC_BOOTSTRAP_INDEX_FILE_NAME,
    PublicBootstrapIndexError,
    validate_public_bootstrap_index,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PRODUCT_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class PublicBootstrapPublicationError(RuntimeError):
    """A non-sensitive public pointer publication failure."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@runtime_checkable
class PublicBootstrapCredentialProvider(Protocol):
    def bearer_token(self) -> str: ...


class EnvironmentPublicBootstrapCredential:
    """Read the publication token at request time and never retain its value."""

    __slots__ = ("_environment", "_variable")

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        variable: str = "ECOREX_BOOTSTRAP_INDEX_TOKEN",
    ) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", variable) is None:
            raise ValueError("Bootstrap index token environment variable is invalid")
        self._environment = os.environ if environment is None else environment
        self._variable = variable

    def bearer_token(self) -> str:
        token = self._environment.get(self._variable)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_credentials_unavailable"
            )
        return token

    def __repr__(self) -> str:
        return (
            f"<EnvironmentPublicBootstrapCredential variable={self._variable!r} "
            "token=<redacted>>"
        )


@dataclass(frozen=True, slots=True)
class PublicBootstrapStageReceipt:
    release_id: str
    version: str
    index_sha256: str
    index_size_bytes: int
    public_url: str
    staged_revision_id: str
    authority_sequence: int
    authority_revision_id: str
    authority_target: dict[str, object]
    freshness_issued_at: str
    freshness_expires_at: str
    expected_previous_activation_record_id: str | None
    expected_previous_sequence: int | None
    expected_previous_authority_revision_id: str | None
    expected_previous_index_sha256: str | None
    expected_previous_target: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "receipt_type": "ecorex-public-bootstrap-index-stage",
            "release_id": self.release_id,
            "version": self.version,
            "state": "staged",
            "index_sha256": self.index_sha256,
            "index_size_bytes": self.index_size_bytes,
            "public_url": self.public_url,
            "staged_revision_id": self.staged_revision_id,
            "authority_sequence": self.authority_sequence,
            "authority_revision_id": self.authority_revision_id,
            "authority_target": self.authority_target,
            "freshness_issued_at": self.freshness_issued_at,
            "freshness_expires_at": self.freshness_expires_at,
            "expected_previous_activation_record_id": (
                self.expected_previous_activation_record_id
            ),
            "expected_previous_sequence": self.expected_previous_sequence,
            "expected_previous_authority_revision_id": (
                self.expected_previous_authority_revision_id
            ),
            "expected_previous_index_sha256": self.expected_previous_index_sha256,
            "expected_previous_target": self.expected_previous_target,
        }


@dataclass(frozen=True, slots=True)
class PublicBootstrapPublicationReceipt:
    release_id: str
    version: str
    index_sha256: str
    index_size_bytes: int
    public_url: str
    staged_revision_id: str
    active_activation_record_id: str
    active_sequence: int
    active_authority_revision_id: str
    active_target: dict[str, object]
    public_object_revision_id: str
    previous_activation_record_id: str | None
    previous_sequence: int | None
    previous_authority_revision_id: str | None
    previous_index_sha256: str | None
    previous_target: dict[str, object] | None
    readback_record_id: str
    readback_proof_token: str
    read_back_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "receipt_type": "ecorex-public-bootstrap-index-publication",
            "release_id": self.release_id,
            "version": self.version,
            "state": "active-and-read-back",
            "index_sha256": self.index_sha256,
            "index_size_bytes": self.index_size_bytes,
            "public_url": self.public_url,
            "staged_revision_id": self.staged_revision_id,
            "active_activation_record_id": self.active_activation_record_id,
            "active_sequence": self.active_sequence,
            "active_authority_revision_id": self.active_authority_revision_id,
            "active_target": self.active_target,
            "public_object_revision_id": self.public_object_revision_id,
            "previous_activation_record_id": self.previous_activation_record_id,
            "previous_sequence": self.previous_sequence,
            "previous_authority_revision_id": (
                self.previous_authority_revision_id
            ),
            "previous_index_sha256": self.previous_index_sha256,
            "previous_target": self.previous_target,
            "readback_record_id": self.readback_record_id,
            "readback_proof_token": self.readback_proof_token,
            "read_back_at": self.read_back_at,
            "cache_control": "no-store",
        }


class HTTPSPublicBootstrapIndexPublisher:
    """Stage, atomically activate and publicly verify one exact index."""

    def __init__(
        self,
        *,
        endpoint: str,
        allowed_hosts: frozenset[str],
        public_url: str,
        public_hosts: frozenset[str],
        credentials: PublicBootstrapCredentialProvider,
        verifier: SignatureVerifier,
        freshness_verifier: SignatureVerifier,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(credentials, PublicBootstrapCredentialProvider):
            raise TypeError("Bootstrap index credential provider is invalid")
        if not isinstance(verifier, SignatureVerifier):
            raise TypeError("Bootstrap index signature verifier is invalid")
        if not isinstance(freshness_verifier, SignatureVerifier):
            raise TypeError("Bootstrap index freshness verifier is invalid")
        control_hosts = _hosts(allowed_hosts, "Bootstrap index control hosts")
        download_hosts = _hosts(public_hosts, "Bootstrap index public hosts")
        parsed_endpoint = urlsplit(endpoint.rstrip("/"))
        if (
            parsed_endpoint.scheme != "https"
            or _hostname(parsed_endpoint) not in control_hosts
            or parsed_endpoint.port not in {None, 443}
            or parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.path != "/api/v1/bootstrap-index"
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ValueError(
                "Bootstrap index endpoint must be the allowlisted HTTPS v1 root"
            )
        parsed_public = urlsplit(public_url)
        if (
            parsed_public.scheme != "https"
            or _hostname(parsed_public) not in download_hosts
            or parsed_public.port not in {None, 443}
            or parsed_public.username
            or parsed_public.password
            or not parsed_public.path.endswith("/" + PUBLIC_BOOTSTRAP_INDEX_FILE_NAME)
            or parsed_public.query
            or parsed_public.fragment
        ):
            raise ValueError("Bootstrap index public URL is invalid")
        self.root = f"https://{_hostname(parsed_endpoint)}/api/v1/bootstrap-index"
        self.public_url = public_url
        self.credentials = credentials
        self.verifier = verifier
        self.freshness_verifier = freshness_verifier
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=60, write=60, pool=15),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def stage(self, index_bytes: bytes) -> PublicBootstrapStageReceipt:
        """Stage exact discovery bytes without changing the public pointer."""

        index = _validated_canonical_index(
            index_bytes,
            verifier=self.verifier,
            freshness_verifier=self.freshness_verifier,
        )
        release = index["release"]
        authority = index["authority"]
        freshness = index["freshness"]
        assert isinstance(release, Mapping)
        assert isinstance(authority, Mapping)
        assert isinstance(freshness, Mapping)
        release_id = str(release["release_id"])
        version = str(release["version"])
        if _SAFE_ID.fullmatch(release_id) is None:
            raise PublicBootstrapPublicationError("bootstrap_index_release_id_invalid")
        digest = hashlib.sha256(index_bytes).hexdigest()
        size = len(index_bytes)
        token = self.credentials.bearer_token()
        candidate_url = f"{self.root}/candidates/{quote(release_id, safe='')}"
        try:
            staged = self._json_request(
                "PUT",
                candidate_url,
                token=token,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(size),
                    "X-EcoreX-SHA256": digest,
                    "X-EcoreX-Size": str(size),
                    "Idempotency-Key": f"bootstrap-index:stage:{release_id}:{digest}",
                },
                content=index_bytes,
                accepted={200, 201},
            )
            receipt = self._validate_stage_receipt(
                staged,
                state="staged",
                release_id=release_id,
                digest=digest,
                size=size,
                authority=authority,
                freshness=freshness,
            )
        except PublicBootstrapPublicationError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_publication_unavailable", retryable=True
            ) from None
        finally:
            token = ""
        return PublicBootstrapStageReceipt(
            release_id=release_id,
            version=version,
            index_sha256=digest,
            index_size_bytes=size,
            public_url=self.public_url,
            staged_revision_id=str(receipt["revision_id"]),
            authority_sequence=int(receipt["authority_sequence"]),
            authority_revision_id=str(receipt["authority_revision_id"]),
            authority_target=dict(receipt["authority_target"]),
            freshness_issued_at=str(receipt["freshness_issued_at"]),
            freshness_expires_at=str(receipt["freshness_expires_at"]),
            expected_previous_activation_record_id=receipt[
                "active_activation_record_id"
            ],
            expected_previous_sequence=receipt["active_sequence"],
            expected_previous_authority_revision_id=receipt[
                "active_authority_revision_id"
            ],
            expected_previous_index_sha256=receipt["active_index_sha256"],
            expected_previous_target=(
                dict(receipt["active_target"])
                if receipt["active_target"] is not None
                else None
            ),
        )

    def activate(
        self,
        index_bytes: bytes,
        staged: PublicBootstrapStageReceipt,
    ) -> PublicBootstrapPublicationReceipt:
        """CAS-activate a previously staged revision, then read it back exactly."""

        if not isinstance(staged, PublicBootstrapStageReceipt):
            raise TypeError("staged Bootstrap index receipt is invalid")
        index = _validated_canonical_index(
            index_bytes,
            verifier=self.verifier,
            freshness_verifier=self.freshness_verifier,
        )
        release = index["release"]
        authority = index["authority"]
        freshness = index["freshness"]
        assert isinstance(release, Mapping)
        assert isinstance(authority, Mapping)
        assert isinstance(freshness, Mapping)
        release_id = str(release["release_id"])
        version = str(release["version"])
        digest = hashlib.sha256(index_bytes).hexdigest()
        size = len(index_bytes)
        if (
            staged.release_id != release_id
            or staged.version != version
            or staged.index_sha256 != digest
            or staged.index_size_bytes != size
            or staged.public_url != self.public_url
            or not _record_id(staged.staged_revision_id, "bstage")
            or staged.authority_sequence != authority.get("sequence")
            or staged.authority_revision_id != authority.get("revision")
            or staged.authority_target != authority.get("target")
            or staged.freshness_issued_at != freshness.get("issued_at")
            or staged.freshness_expires_at != freshness.get("expires_at")
            or not _valid_previous_identity(staged)
        ):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_stage_identity_changed"
            )
        token = self.credentials.bearer_token()
        candidate_url = f"{self.root}/candidates/{quote(release_id, safe='')}"
        try:
            activation_body = json.dumps(
                {
                    "expected_previous_activation_record_id": (
                        staged.expected_previous_activation_record_id
                    ),
                    "expected_previous_sequence": staged.expected_previous_sequence,
                    "expected_previous_authority_revision_id": (
                        staged.expected_previous_authority_revision_id
                    ),
                    "expected_previous_index_sha256": (
                        staged.expected_previous_index_sha256
                    ),
                    "expected_previous_target": staged.expected_previous_target,
                    "index_sha256": digest,
                    "revision_id": staged.staged_revision_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            active = self._json_request(
                "POST",
                f"{candidate_url}/activate",
                token=token,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(activation_body)),
                    "Idempotency-Key": (
                        f"bootstrap-index:activate:{release_id}:{digest}:"
                        f"{staged.staged_revision_id}"
                    ),
                },
                content=activation_body,
                accepted={200},
            )
            receipt = self._validate_active_receipt(
                active,
                release_id=release_id,
                version=version,
                build_digest=str(release["build_digest"]),
                digest=digest,
                size=size,
                staged=staged,
            )
        except PublicBootstrapPublicationError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_publication_unavailable", retryable=True
            ) from None
        finally:
            token = ""
        readback = receipt["readback"]
        assert isinstance(readback, Mapping)
        return PublicBootstrapPublicationReceipt(
            release_id=release_id,
            version=version,
            index_sha256=digest,
            index_size_bytes=size,
            public_url=self.public_url,
            staged_revision_id=staged.staged_revision_id,
            active_activation_record_id=str(
                receipt["active_activation_record_id"]
            ),
            active_sequence=int(receipt["active_sequence"]),
            active_authority_revision_id=str(
                receipt["active_authority_revision_id"]
            ),
            active_target=dict(receipt["active_target"]),
            public_object_revision_id=str(receipt["public_object_revision_id"]),
            previous_activation_record_id=receipt[
                "previous_activation_record_id"
            ],
            previous_sequence=receipt["previous_sequence"],
            previous_authority_revision_id=receipt[
                "previous_authority_revision_id"
            ],
            previous_index_sha256=receipt["previous_index_sha256"],
            previous_target=(
                dict(receipt["previous_target"])
                if receipt["previous_target"] is not None
                else None
            ),
            readback_record_id=str(readback["record_id"]),
            readback_proof_token=str(readback["proof_token"]),
            read_back_at=str(readback["read_back_at"]),
        )

    def publish(self, index_bytes: bytes) -> PublicBootstrapPublicationReceipt:
        """Compatibility wrapper; production workflows use explicit phases."""

        staged = self.stage(index_bytes)
        return self.activate(index_bytes, staged)

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        headers: Mapping[str, str],
        content: bytes,
        accepted: set[int],
    ) -> Any:
        request = self.client.build_request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                **headers,
            },
            content=content,
        )
        response = self.client.send(request, stream=True, follow_redirects=False)
        try:
            return _consume_json(response, accepted=accepted)
        finally:
            response.close()

    def _validate_stage_receipt(
        self,
        value: Any,
        *,
        state: str,
        release_id: str,
        digest: str,
        size: int,
        authority: Mapping[str, Any],
        freshness: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected = {
            "schema_version",
            "release_id",
            "version",
            "state",
            "index_sha256",
            "index_size_bytes",
            "public_url",
            "revision_id",
            "authority_sequence",
            "authority_revision_id",
            "authority_target",
            "freshness_issued_at",
            "freshness_expires_at",
            "active_activation_record_id",
            "active_sequence",
            "active_authority_revision_id",
            "active_index_sha256",
            "active_target",
        }
        target = authority.get("target")
        assert isinstance(target, Mapping)
        previous = (
            value.get("active_activation_record_id"),
            value.get("active_sequence"),
            value.get("active_authority_revision_id"),
            value.get("active_index_sha256"),
            value.get("active_target"),
        ) if isinstance(value, Mapping) else (None,) * 5
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version") != 1
            or value.get("release_id") != release_id
            or value.get("version") != target.get("version")
            or value.get("state") != state
            or value.get("index_sha256") != digest
            or value.get("index_size_bytes") != size
            or value.get("public_url") != self.public_url
            or not _record_id(value.get("revision_id"), "bstage")
            or value.get("authority_sequence") != authority.get("sequence")
            or value.get("authority_revision_id") != authority.get("revision")
            or value.get("authority_target") != target
            or value.get("freshness_issued_at") != freshness.get("issued_at")
            or value.get("freshness_expires_at") != freshness.get("expires_at")
            or not _previous_tuple(previous)
        ):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_stage_receipt_invalid"
            )
        return value

    def _validate_active_receipt(
        self,
        value: Any,
        *,
        release_id: str,
        version: str,
        build_digest: str,
        digest: str,
        size: int,
        staged: PublicBootstrapStageReceipt,
    ) -> Mapping[str, Any]:
        expected = {
            "schema_version",
            "release_id",
            "version",
            "state",
            "index_sha256",
            "index_size_bytes",
            "public_url",
            "staged_revision_id",
            "active_activation_record_id",
            "active_sequence",
            "active_authority_revision_id",
            "active_target",
            "public_object_revision_id",
            "previous_activation_record_id",
            "previous_sequence",
            "previous_authority_revision_id",
            "previous_index_sha256",
            "previous_target",
            "readback",
        }
        previous = (
            value.get("previous_activation_record_id"),
            value.get("previous_sequence"),
            value.get("previous_authority_revision_id"),
            value.get("previous_index_sha256"),
            value.get("previous_target"),
        ) if isinstance(value, Mapping) else (None,) * 5
        expected_previous = (
            staged.expected_previous_activation_record_id,
            staged.expected_previous_sequence,
            staged.expected_previous_authority_revision_id,
            staged.expected_previous_index_sha256,
            staged.expected_previous_target,
        )
        readback = value.get("readback") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version") != 1
            or value.get("release_id") != release_id
            or value.get("version") != version
            or value.get("state") != "active-and-read-back"
            or value.get("index_sha256") != digest
            or value.get("index_size_bytes") != size
            or value.get("public_url") != self.public_url
            or value.get("staged_revision_id") != staged.staged_revision_id
            or not _record_id(value.get("active_activation_record_id"), "bactive")
            or value.get("active_sequence") != staged.authority_sequence
            or value.get("active_authority_revision_id")
            != staged.authority_revision_id
            or value.get("active_target") != staged.authority_target
            or not _record_id(value.get("public_object_revision_id"), "pobj")
            or not _previous_tuple(previous)
            or previous != expected_previous
            or not _valid_readback(
                readback,
                release_id=release_id,
                version=version,
                build_digest=build_digest,
                digest=digest,
                size=size,
                public_url=self.public_url,
                stage_id=staged.staged_revision_id,
                activation_id=str(value.get("active_activation_record_id")),
                sequence=staged.authority_sequence,
                revision=staged.authority_revision_id,
                issued_at=staged.freshness_issued_at,
                expires_at=staged.freshness_expires_at,
                target=staged.authority_target,
            )
        ):
            raise PublicBootstrapPublicationError(
                "bootstrap_index_activation_receipt_invalid"
            )
        return value


def _validated_canonical_index(
    payload: bytes,
    *,
    verifier: SignatureVerifier,
    freshness_verifier: SignatureVerifier,
) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES:
        raise PublicBootstrapPublicationError("bootstrap_index_bytes_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PublicBootstrapPublicationError("bootstrap_index_bytes_invalid") from None
    try:
        validate_public_bootstrap_index(
            value,
            verifier=verifier,
            freshness_verifier=freshness_verifier,
        )
    except (PublicBootstrapIndexError, TypeError):
        raise PublicBootstrapPublicationError("bootstrap_index_bytes_invalid") from None
    canonical = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if canonical != payload:
        raise PublicBootstrapPublicationError("bootstrap_index_not_canonical")
    assert isinstance(value, Mapping)
    return value


def _consume_json(response: httpx.Response, *, accepted: set[int]) -> Any:
    if response.is_redirect or response.history:
        raise PublicBootstrapPublicationError("bootstrap_index_redirect_refused")
    if response.status_code not in accepted:
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        raise PublicBootstrapPublicationError(
            "bootstrap_index_publication_rejected", retryable=retryable
        )
    if response.headers.get("content-encoding", "identity").casefold() != "identity":
        raise PublicBootstrapPublicationError("bootstrap_index_response_compressed")
    if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise PublicBootstrapPublicationError("bootstrap_index_response_invalid")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise PublicBootstrapPublicationError("bootstrap_index_response_too_large")
    try:
        return json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PublicBootstrapPublicationError("bootstrap_index_response_invalid") from None


def _hosts(values: frozenset[str], label: str) -> frozenset[str]:
    normalized = frozenset(value.casefold().rstrip(".") for value in values if value)
    if not normalized or any(not _valid_host(value) for value in normalized):
        raise ValueError(f"{label} are invalid")
    return normalized


def _hostname(value: Any) -> str:
    return (value.hostname or "").casefold().rstrip(".")


def _valid_host(value: str) -> bool:
    return (
        1 <= len(value) <= 253
        and all(_HOST_LABEL.fullmatch(label) is not None for label in value.split("."))
    )


def _record_id(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(re.escape(prefix) + r"_[0-9a-f]{32}", value) is not None
    )


def _target(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value)
        == {"manifest_sha256", "release_id", "version", "build_digest"}
        and _sha(value.get("manifest_sha256"))
        and isinstance(value.get("release_id"), str)
        and re.fullmatch(
            r"release-stable-[0-9a-f]{24}", str(value.get("release_id"))
        )
        is not None
        and isinstance(value.get("version"), str)
        and _PRODUCT_VERSION.fullmatch(str(value.get("version"))) is not None
        and _sha(value.get("build_digest"))
    )


def _previous_tuple(value: tuple[Any, ...]) -> bool:
    if len(value) != 5:
        return False
    if all(item is None for item in value):
        return True
    activation, sequence, revision, digest, target = value
    return (
        all(item is not None for item in value)
        and _record_id(activation, "bactive")
        and not isinstance(sequence, bool)
        and isinstance(sequence, int)
        and sequence > 0
        and isinstance(revision, str)
        and re.fullmatch(r"release-stable-[0-9a-f]{24}", revision) is not None
        and _sha(digest)
        and _target(target)
        and target.get("release_id") == revision
    )


def _valid_previous_identity(value: PublicBootstrapStageReceipt) -> bool:
    return _previous_tuple(
        (
            value.expected_previous_activation_record_id,
            value.expected_previous_sequence,
            value.expected_previous_authority_revision_id,
            value.expected_previous_index_sha256,
            value.expected_previous_target,
        )
    )


def _valid_readback(
    value: Any,
    *,
    release_id: str,
    version: str,
    build_digest: str,
    digest: str,
    size: int,
    public_url: str,
    stage_id: str,
    activation_id: str,
    sequence: int,
    revision: str,
    issued_at: str,
    expires_at: str,
    target: Mapping[str, object],
) -> bool:
    expected = {
        "schema_version",
        "record_id",
        "activation_record_id",
        "stage_record_id",
        "release_id",
        "version",
        "build_digest",
        "sequence",
        "revision",
        "issued_at",
        "expires_at",
        "target",
        "index_sha256",
        "index_size_bytes",
        "public_url",
        "read_back_at",
        "proof_token",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("schema_version") != 1
        or not _record_id(value.get("record_id"), "bread")
        or value.get("activation_record_id") != activation_id
        or value.get("stage_record_id") != stage_id
        or value.get("release_id") != release_id
        or value.get("version") != version
        or value.get("build_digest") != build_digest
        or value.get("sequence") != sequence
        or value.get("revision") != revision
        or value.get("issued_at") != issued_at
        or value.get("expires_at") != expires_at
        or value.get("target") != target
        or value.get("index_sha256") != digest
        or value.get("index_size_bytes") != size
        or value.get("public_url") != public_url
        or not isinstance(value.get("read_back_at"), str)
        or _RFC3339.fullmatch(str(value.get("read_back_at"))) is None
    ):
        return False
    unsigned = dict(value)
    proof_token = unsigned.pop("proof_token")
    proof_digest = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return proof_token == (
        f"bootstrap-index-proof:{value['record_id']}:sha256:{proof_digest}"
    )


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


__all__ = [
    "EnvironmentPublicBootstrapCredential",
    "HTTPSPublicBootstrapIndexPublisher",
    "PublicBootstrapCredentialProvider",
    "PublicBootstrapPublicationError",
    "PublicBootstrapPublicationReceipt",
    "PublicBootstrapStageReceipt",
]
