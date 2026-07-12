"""Digest-fenced upload transport for the domestic mirror and CDN replicas."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

import httpx


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ReleaseReplicaError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@runtime_checkable
class ReplicaCredentialProvider(Protocol):
    def bearer_token(self) -> str: ...


class EnvironmentReplicaCredential:
    """Read a replica token at request time without retaining secret material."""

    __slots__ = ("_environment", "_variable")

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        variable: str = "ECOREX_RELEASE_REPLICA_TOKEN",
    ) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", variable) is None:
            raise ValueError("replica token environment variable is invalid")
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
            raise ReleaseReplicaError("replica_credentials_unavailable")
        return token

    def __repr__(self) -> str:
        return (
            f"<EnvironmentReplicaCredential variable={self._variable!r} "
            "token=<redacted>>"
        )


@dataclass(frozen=True, slots=True)
class ReleaseReplicaReceipt:
    source_id: str
    release_id: str
    name: str
    size_bytes: int
    sha256: str
    url: str


class HTTPSReleaseReplicaPublisher:
    """Upload exact bytes to one EcoreX-managed release replica service."""

    def __init__(
        self,
        *,
        source_id: str,
        endpoint: str,
        allowed_hosts: frozenset[str],
        public_hosts: frozenset[str],
        credentials: ReplicaCredentialProvider,
        client: httpx.Client | None = None,
    ) -> None:
        if _SAFE_ID.fullmatch(source_id) is None:
            raise ValueError("replica source ID is invalid")
        if not isinstance(credentials, ReplicaCredentialProvider):
            raise TypeError("replica credential provider is invalid")
        hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts if host)
        public = frozenset(host.casefold().rstrip(".") for host in public_hosts if host)
        if (
            not hosts
            or not public
            or any(not _valid_host(host) for host in (*hosts, *public))
        ):
            raise ValueError("replica host allowlist is invalid")
        parsed = urlsplit(endpoint.rstrip("/"))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") not in hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.path != "/api/v1/releases"
            or parsed.query
            or parsed.fragment
            or not public
        ):
            raise ValueError("replica endpoint must be the allowlisted HTTPS v1 root")
        self.source_id = source_id
        self.root = (
            f"https://{(parsed.hostname or '').casefold().rstrip('.')}/api/v1/releases"
        )
        self.public_hosts = public
        self.credentials = credentials
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=180, write=180, pool=15),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def ensure_asset(
        self,
        *,
        release_id: str,
        path: str | os.PathLike[str],
        expected_sha256: str,
    ) -> ReleaseReplicaReceipt:
        if _SAFE_ID.fullmatch(release_id) is None:
            raise ValueError("release ID is invalid")
        if _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("replica expected digest is invalid")
        file_path = Path(path)
        if _SAFE_FILE.fullmatch(file_path.name) is None:
            raise ValueError("replica asset filename is invalid")
        before = _regular_file(file_path)
        if before.st_size < 1 or _hash_file(file_path, before) != expected_sha256:
            raise ReleaseReplicaError("local_asset_digest_mismatch")
        token = self.credentials.bearer_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream",
            "Content-Length": str(before.st_size),
            "X-EcoreX-SHA256": expected_sha256,
            "X-EcoreX-Size": str(before.st_size),
            "Idempotency-Key": (
                f"{self.source_id}:{release_id}:{file_path.name}:{expected_sha256}"
            ),
        }
        url = (
            f"{self.root}/{quote(release_id, safe='')}/replicas/"
            f"{quote(self.source_id, safe='')}/assets/{quote(file_path.name, safe='')}"
        )
        try:
            with file_path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _identity(opened) != _identity(before):
                    raise ReleaseReplicaError("local_asset_changed")
                request = self.client.build_request(
                    "PUT", url, headers=headers, content=stream
                )
                response = self.client.send(request, stream=True, follow_redirects=False)
                try:
                    value = _consume_json(response, accepted={200, 201})
                finally:
                    response.close()
                stream.seek(0)
                uploaded_source_digest = _hash_open_stream(stream)
                after = os.fstat(stream.fileno())
            if _identity(after) != _identity(before):
                raise ReleaseReplicaError("local_asset_changed")
            if uploaded_source_digest != expected_sha256:
                raise ReleaseReplicaError("local_asset_changed")
        except ReleaseReplicaError:
            raise
        except (OSError, httpx.TimeoutException, httpx.TransportError):
            raise ReleaseReplicaError("replica_unavailable", retryable=True) from None
        finally:
            token = ""
        return self._receipt(
            value,
            release_id=release_id,
            name=file_path.name,
            size_bytes=before.st_size,
            sha256=expected_sha256,
        )

    def finalize(
        self,
        *,
        release_id: str,
        manifest_sha256: str,
    ) -> bool:
        if _SAFE_ID.fullmatch(release_id) is None or _SHA256.fullmatch(
            manifest_sha256
        ) is None:
            raise ValueError("replica finalization identity is invalid")
        body = json.dumps(
            {"manifest_sha256": manifest_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = self.credentials.bearer_token()
        try:
            request = self.client.build_request(
                "POST",
                (
                    f"{self.root}/{quote(release_id, safe='')}/replicas/"
                    f"{quote(self.source_id, safe='')}/finalize"
                ),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    "Idempotency-Key": (
                        f"finalize:{self.source_id}:{release_id}:{manifest_sha256}"
                    ),
                },
                content=body,
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
            try:
                value = _consume_json(response, accepted={200})
            finally:
                response.close()
        except ReleaseReplicaError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise ReleaseReplicaError("replica_unavailable", retryable=True) from None
        finally:
            token = ""
        if (
            not isinstance(value, Mapping)
            or set(value) != {"release_id", "source_id", "state", "manifest_sha256"}
            or value.get("release_id") != release_id
            or value.get("source_id") != self.source_id
            or value.get("state") != "ready"
            or value.get("manifest_sha256") != manifest_sha256
        ):
            raise ReleaseReplicaError("replica_finalization_invalid")
        return True

    def _receipt(
        self,
        value: Any,
        *,
        release_id: str,
        name: str,
        size_bytes: int,
        sha256: str,
    ) -> ReleaseReplicaReceipt:
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {"release_id", "source_id", "name", "size_bytes", "sha256", "url", "state"}
            or value.get("release_id") != release_id
            or value.get("source_id") != self.source_id
            or value.get("name") != name
            or isinstance(value.get("size_bytes"), bool)
            or value.get("size_bytes") != size_bytes
            or value.get("sha256") != sha256
            or value.get("state") != "ready"
            or not isinstance(value.get("url"), str)
        ):
            raise ReleaseReplicaError("replica_receipt_invalid")
        url = str(value["url"])
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") not in self.public_hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(f"/{quote(name, safe='')}")
        ):
            raise ReleaseReplicaError("replica_public_url_invalid")
        return ReleaseReplicaReceipt(
            self.source_id, release_id, name, size_bytes, sha256, url
        )


def _consume_json(response: httpx.Response, *, accepted: set[int]) -> Any:
    if response.is_redirect or response.history:
        raise ReleaseReplicaError("replica_redirect_refused")
    if response.status_code not in accepted:
        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        raise ReleaseReplicaError("replica_rejected", retryable=retryable)
    if response.headers.get("content-encoding", "identity").casefold() != "identity":
        raise ReleaseReplicaError("replica_compressed_response")
    if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ReleaseReplicaError("replica_invalid_response")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise ReleaseReplicaError("replica_response_too_large")
    try:
        return json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ReleaseReplicaError("replica_invalid_response") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise ReleaseReplicaError("local_asset_unavailable") from None
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
    ):
        raise ReleaseReplicaError("local_asset_is_not_regular")
    return metadata


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _hash_file(path: Path, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise ReleaseReplicaError("local_asset_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except ReleaseReplicaError:
        raise
    except OSError:
        raise ReleaseReplicaError("local_asset_unavailable") from None
    if _identity(after) != _identity(before):
        raise ReleaseReplicaError("local_asset_changed")
    return digest.hexdigest()


def _hash_open_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _valid_host(value: str) -> bool:
    return (
        1 <= len(value) <= 253
        and all(_HOST_LABEL.fullmatch(label) is not None for label in value.split("."))
    )


__all__ = [
    "EnvironmentReplicaCredential",
    "HTTPSReleaseReplicaPublisher",
    "ReleaseReplicaError",
    "ReleaseReplicaReceipt",
    "ReplicaCredentialProvider",
]
