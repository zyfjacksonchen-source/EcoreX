"""Private S3-compatible CAS adapter for public Share media.

The caller owns the injected client's credentials, TLS policy, retries and
connection pool.  EcoreX receives no access key and persists only opaque object
keys plus content identities.  Downloads are verified into a byte-budgeted
spooled file so public HTTP range reads remain seekable without keeping every
16 MiB object resident in memory.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
import tempfile
import threading
from typing import Any, BinaryIO, Mapping, Protocol, runtime_checkable

from .share_objects import (
    ShareObjectCapacityError,
    ShareObjectError,
    ShareObjectRead,
    ShareStoredObject,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_OBJECT_BYTES = 16 * 1024 * 1024
_METADATA_CONTRACT = "ecorex-share-cas-v1"


class S3ShareObjectNotFound(ShareObjectError):
    """The private object does not exist."""


class S3ShareObjectPreconditionFailed(ShareObjectError):
    """An immutable-object or ETag condition lost a race."""


@runtime_checkable
class S3ShareStreamingBody(Protocol):
    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class S3ShareClient(Protocol):
    """Narrow boto-compatible surface; authentication remains client-owned."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _S3Head:
    size_bytes: int
    mime_type: str
    sha256: str
    provider_etag: str


class _ByteCapacity:
    def __init__(self, total: int) -> None:
        self.total = total
        self._available = total
        self._lock = threading.Lock()

    def acquire(self, amount: int) -> bool:
        with self._lock:
            if amount > self._available:
                return False
            self._available -= amount
            return True

    def release(self, amount: int) -> None:
        with self._lock:
            self._available += amount
            if self._available > self.total:  # pragma: no cover - invariant guard
                self._available = self.total
                raise RuntimeError("S3 Share spool capacity was released twice")


class S3ShareObjectStore:
    """Content-addressed private-bucket Share object storage.

    ``client`` may be a boto3 S3 client or a compatible adapter.  This class
    deliberately has no credential, endpoint, TLS or pool constructor fields.
    The named bucket must be provisioned privately with S3 Block Public Access
    (or the compatible provider's equivalent); the adapter never assigns a
    public ACL and never creates or returns an object URL.
    """

    def __init__(
        self,
        client: S3ShareClient,
        *,
        bucket: str,
        prefix: str = "ecorex/share",
        max_open_streams: int = 16,
        max_object_bytes: int = _DEFAULT_MAX_OBJECT_BYTES,
        max_total_spool_bytes: int = 256 * 1024 * 1024,
        memory_spool_bytes: int = 256 * 1024,
        spool_directory: str | None = None,
    ) -> None:
        if not isinstance(client, S3ShareClient):
            raise TypeError("S3 Share client is invalid")
        normalized_prefix = str(prefix).strip("/")
        if (
            not isinstance(bucket, str)
            or not _BUCKET.fullmatch(bucket)
            or not normalized_prefix
            or not _PREFIX.fullmatch(normalized_prefix)
            or any(part in {"", ".", ".."} for part in normalized_prefix.split("/"))
        ):
            raise ValueError("S3 Share private bucket or prefix is invalid")
        for value, upper, message in (
            (max_open_streams, 1024, "S3 Share stream limit is invalid"),
            (max_object_bytes, _DEFAULT_MAX_OBJECT_BYTES, "S3 Share object limit is invalid"),
            (
                max_total_spool_bytes,
                64 * 1024 * 1024 * 1024,
                "S3 Share total spool limit is invalid",
            ),
            (memory_spool_bytes, max_object_bytes, "S3 Share memory spool limit is invalid"),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= upper
            ):
                raise ValueError(message)
        if max_total_spool_bytes < max_object_bytes:
            raise ValueError("S3 Share total spool limit is smaller than one object")
        self.client = client
        self.bucket = bucket
        self.prefix = normalized_prefix
        self.max_object_bytes = max_object_bytes
        self.memory_spool_bytes = memory_spool_bytes
        self.spool_directory = spool_directory
        self._stream_slots = threading.BoundedSemaphore(max_open_streams)
        self._spool_capacity = _ByteCapacity(max_total_spool_bytes)

    def put(
        self,
        content: bytes,
        *,
        sha256: str,
        mime_type: str,
    ) -> ShareStoredObject:
        self._validate_identity(sha256, mime_type, len(content) if isinstance(content, bytes) else -1)
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != sha256:
            raise ShareObjectError("S3 Share object input is invalid")
        key = self._key(sha256)
        arguments = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": content,
            "ContentLength": len(content),
            "ContentType": mime_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": _checksum_b64(sha256),
            "Metadata": _metadata(sha256, mime_type),
            "IfNoneMatch": "*",
        }
        try:
            self.client.put_object(**arguments)
        except Exception as error:
            translated = _translate_error(error)
            if not isinstance(translated, S3ShareObjectPreconditionFailed):
                raise translated from None
            # Another writer won the immutable CAS key.  The following HEAD is
            # the authority for whether that winner stored the same object.
        head = self._head(key, expected_sha256=sha256, expected_size=len(content), expected_mime=mime_type)
        return _descriptor(key, head)

    def open(
        self,
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareObjectRead:
        self._validate_identity(sha256, mime_type, size_bytes)
        if object_key != self._key(sha256):
            raise ShareObjectError("S3 Share object identity is invalid")
        if not self._stream_slots.acquire(blocking=False):
            raise ShareObjectCapacityError("S3 Share stream capacity is busy")
        if not self._spool_capacity.acquire(size_bytes):
            self._stream_slots.release()
            raise ShareObjectCapacityError("S3 Share spool capacity is busy")

        spool: BinaryIO | None = None
        body: S3ShareStreamingBody | None = None
        try:
            head = self._head(
                object_key,
                expected_sha256=sha256,
                expected_size=size_bytes,
                expected_mime=mime_type,
            )
            try:
                response = self.client.get_object(
                    Bucket=self.bucket,
                    Key=object_key,
                    Range=f"bytes=0-{size_bytes - 1}",
                    IfMatch=head.provider_etag,
                    ChecksumMode="ENABLED",
                )
            except Exception as error:
                raise _translate_error(error) from None
            response_head = self._parse_head(response, require_checksum=False)
            if response_head != head:
                raise ShareObjectError("S3 Share object changed between HEAD and GET")
            if str(response.get("ContentRange", "")) != f"bytes 0-{size_bytes - 1}/{size_bytes}":
                raise ShareObjectError("S3 Share object range response is invalid")
            candidate = response.get("Body")
            if not isinstance(candidate, S3ShareStreamingBody):
                raise ShareObjectError("S3 Share object body is not streaming")
            body = candidate
            spool = tempfile.SpooledTemporaryFile(
                max_size=self.memory_spool_bytes,
                mode="w+b",
                dir=self.spool_directory,
                prefix="ecorex-share-s3-",
            )
            digest = hashlib.sha256()
            observed = 0
            while observed < size_bytes:
                chunk = body.read(min(_CHUNK_BYTES, size_bytes - observed))
                if not isinstance(chunk, bytes) or not chunk:
                    raise ShareObjectError("S3 Share object body ended early")
                observed += len(chunk)
                if observed > size_bytes:
                    raise ShareObjectError("S3 Share object body exceeded its bound")
                digest.update(chunk)
                spool.write(chunk)
            trailing = body.read(1)
            if trailing not in {b"", None}:
                raise ShareObjectError("S3 Share object body exceeded its bound")
            if observed != size_bytes or digest.hexdigest() != sha256:
                raise ShareObjectError("S3 Share object body failed integrity verification")
            spool.seek(0)
        except ShareObjectCapacityError:
            _close_quietly(body)
            _close_quietly(spool)
            self._release_capacity(size_bytes)
            raise
        except (OSError, ShareObjectError, TypeError, ValueError):
            _close_quietly(body)
            _close_quietly(spool)
            self._release_capacity(size_bytes)
            raise ShareObjectError("S3 Share object is unavailable or corrupt") from None
        except Exception:
            _close_quietly(body)
            _close_quietly(spool)
            self._release_capacity(size_bytes)
            raise ShareObjectError("S3 Share object request failed") from None
        finally:
            _close_quietly(body)

        return ShareObjectRead(
            descriptor=_descriptor(object_key, head),
            _handle=spool,
            _release=lambda: self._release_capacity(size_bytes),
        )

    def delete(self, object_key: str, *, sha256: str) -> bool:
        if not _DIGEST.fullmatch(str(sha256)) or object_key != self._key(sha256):
            raise ShareObjectError("S3 Share object identity is invalid")
        try:
            head = self._head(object_key, expected_sha256=sha256)
        except S3ShareObjectNotFound:
            return False
        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=object_key,
                IfMatch=head.provider_etag,
            )
        except Exception as error:
            translated = _translate_error(error)
            if isinstance(translated, S3ShareObjectNotFound):
                return False
            raise translated from None
        return True

    def _head(
        self,
        key: str,
        *,
        expected_sha256: str,
        expected_size: int | None = None,
        expected_mime: str | None = None,
    ) -> _S3Head:
        try:
            response = self.client.head_object(
                Bucket=self.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except Exception as error:
            raise _translate_error(error) from None
        head = self._parse_head(response, require_checksum=True)
        if (
            head.sha256 != expected_sha256
            or (expected_size is not None and head.size_bytes != expected_size)
            or (expected_mime is not None and head.mime_type != expected_mime)
        ):
            raise ShareObjectError("S3 Share object metadata conflicts with its identity")
        return head

    @staticmethod
    def _parse_head(
        response: Mapping[str, Any],
        *,
        require_checksum: bool,
    ) -> _S3Head:
        if not isinstance(response, Mapping):
            raise ShareObjectError("S3 Share object metadata response is invalid")
        metadata = response.get("Metadata")
        if not isinstance(metadata, Mapping):
            raise ShareObjectError("S3 Share custom metadata is missing")
        normalized = {str(key).casefold(): str(value) for key, value in metadata.items()}
        if set(normalized) != {"ecorex-contract", "ecorex-sha256", "ecorex-mime-type"}:
            raise ShareObjectError("S3 Share custom metadata is invalid")
        sha256 = normalized["ecorex-sha256"]
        mime_type = normalized["ecorex-mime-type"]
        response_checksum = response.get("ChecksumSHA256")
        checksum_valid = (
            response_checksum is None and not require_checksum
        ) or _decode_checksum(response_checksum) == sha256
        if (
            normalized["ecorex-contract"] != _METADATA_CONTRACT
            or not _DIGEST.fullmatch(sha256)
            or not _MIME.fullmatch(mime_type)
            or str(response.get("ContentType", "")) != mime_type
            or not checksum_valid
        ):
            raise ShareObjectError("S3 Share object checksum or metadata is invalid")
        size = response.get("ContentLength")
        etag = response.get("ETag")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= _DEFAULT_MAX_OBJECT_BYTES
            or not isinstance(etag, str)
            or not 1 <= len(etag) <= 256
            or any(ord(character) < 32 for character in etag)
        ):
            raise ShareObjectError("S3 Share object HEAD is invalid")
        return _S3Head(size_bytes=size, mime_type=mime_type, sha256=sha256, provider_etag=etag)

    def _key(self, sha256: str) -> str:
        return f"{self.prefix}/sha256/{sha256}"

    def _validate_identity(self, sha256: str, mime_type: str, size_bytes: int) -> None:
        if (
            not isinstance(sha256, str)
            or not _DIGEST.fullmatch(sha256)
            or not isinstance(mime_type, str)
            or not _MIME.fullmatch(mime_type)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 1 <= size_bytes <= self.max_object_bytes
        ):
            raise ShareObjectError("S3 Share object identity is invalid")

    def _release_capacity(self, size_bytes: int) -> None:
        self._spool_capacity.release(size_bytes)
        self._stream_slots.release()


def _metadata(sha256: str, mime_type: str) -> dict[str, str]:
    return {
        "ecorex-contract": _METADATA_CONTRACT,
        "ecorex-sha256": sha256,
        "ecorex-mime-type": mime_type,
    }


def _descriptor(key: str, head: _S3Head) -> ShareStoredObject:
    try:
        return ShareStoredObject(
            object_key=key,
            sha256=head.sha256,
            size_bytes=head.size_bytes,
            mime_type=head.mime_type,
            # Public and database ETags remain stable content identities.  The
            # provider ETag is used only for HEAD/GET/DELETE fencing.
            etag=head.sha256,
        )
    except ValueError as error:
        raise ShareObjectError("S3 Share object descriptor is invalid") from error


def _checksum_b64(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")


def _decode_checksum(value: Any) -> str:
    if not isinstance(value, str):
        raise ShareObjectError("S3 Share SHA-256 checksum is missing")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ShareObjectError("S3 Share SHA-256 checksum is invalid") from None
    if len(decoded) != 32:
        raise ShareObjectError("S3 Share SHA-256 checksum is invalid")
    return decoded.hex()


def _translate_error(error: Exception) -> ShareObjectError:
    if isinstance(error, ShareObjectError):
        return error
    response = getattr(error, "response", None)
    code = ""
    status = 0
    if isinstance(response, Mapping):
        details = response.get("Error")
        metadata = response.get("ResponseMetadata")
        if isinstance(details, Mapping):
            code = str(details.get("Code", "")).casefold()
        if isinstance(metadata, Mapping):
            try:
                status = int(metadata.get("HTTPStatusCode", 0))
            except (TypeError, ValueError):
                status = 0
    if status == 404 or code in {"404", "nosuchkey", "notfound", "no_such_key"}:
        return S3ShareObjectNotFound("S3 Share object is missing")
    if status == 412 or code in {
        "412",
        "conditionalrequestconflict",
        "preconditionfailed",
        "precondition_failed",
    }:
        return S3ShareObjectPreconditionFailed("S3 Share object precondition failed")
    return ShareObjectError("S3 Share object request failed")


def _close_quietly(value: Any) -> None:
    if value is None:
        return
    try:
        value.close()
    except Exception:
        pass


__all__ = [
    "S3ShareClient",
    "S3ShareObjectNotFound",
    "S3ShareObjectPreconditionFailed",
    "S3ShareObjectStore",
    "S3ShareStreamingBody",
]
