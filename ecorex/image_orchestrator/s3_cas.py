"""S3-compatible shared CAS with conditional writes and safe deletion.

The implementation depends on a deliberately small object-transport protocol.
Deployments may inject a configured SDK client through
``BotoS3ObjectTransport`` or a bounded ``httpx`` client through
``S3HTTPObjectTransport``.  Credentials remain owned by that injected client;
this module never accepts or persists access keys.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit, urlunsplit

from .cas import (
    ImageContentAddressedStore,
    ImageContentMetadata,
    ImageContentReference,
    validate_image_payload,
)
from .models import ImageResult, ImageResultRejected, canonical_json


_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ETAG = re.compile(r'^"?[A-Fa-f0-9-]{1,128}"?$')
_MAX_REFERENCE_COUNT = 1024
_MAX_REFERENCE_DOCUMENT_BYTES = 64 * 1024


class S3ObjectError(ImageResultRejected):
    pass


class S3ObjectNotFound(S3ObjectError):
    pass


class S3ObjectPreconditionFailed(S3ObjectError):
    pass


@dataclass(frozen=True, slots=True)
class S3ObjectInfo:
    etag: str
    size_bytes: int
    content_type: str
    metadata: Mapping[str, str]
    checksum_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.etag, str) or not _ETAG.fullmatch(self.etag):
            raise ValueError("S3 object ETag is invalid")
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("S3 object size is invalid")
        if not isinstance(self.content_type, str) or not self.content_type:
            raise ValueError("S3 object content type is invalid")
        if self.checksum_sha256 is not None and not _DIGEST.fullmatch(
            self.checksum_sha256
        ):
            raise ValueError("S3 object checksum is invalid")


@dataclass(frozen=True, slots=True)
class S3ObjectBody:
    info: S3ObjectInfo
    payload: bytes


@runtime_checkable
class S3ObjectTransport(Protocol):
    """Bounded object operations required by the shared image CAS."""

    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str],
        checksum_sha256: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str: ...

    def head_object(self, *, bucket: str, key: str) -> S3ObjectInfo: ...

    def get_object(
        self, *, bucket: str, key: str, max_bytes: int
    ) -> S3ObjectBody: ...

    def delete_object(
        self, *, bucket: str, key: str, if_match: str | None = None
    ) -> None: ...


def _hex_checksum(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if _DIGEST.fullmatch(text):
        return text
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
    return decoded.hex() if len(decoded) == 32 else None


def _etag(value: Any) -> str:
    text = str(value or "").strip()
    if not _ETAG.fullmatch(text):
        raise S3ObjectError("object store omitted a usable ETag")
    return text


def _translate_client_error(error: Exception) -> S3ObjectError:
    response = getattr(error, "response", None)
    code = None
    status = None
    if isinstance(response, Mapping):
        detail = response.get("Error")
        if isinstance(detail, Mapping):
            code = detail.get("Code")
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            status = metadata.get("HTTPStatusCode")
    normalized = str(code or "").casefold()
    if status == 404 or normalized in {"404", "nosuchkey", "notfound"}:
        return S3ObjectNotFound("object is missing")
    if status in {409, 412} or normalized in {
        "409",
        "412",
        "conditionalrequestconflict",
        "preconditionfailed",
    }:
        return S3ObjectPreconditionFailed("object precondition failed")
    return S3ObjectError("object store request failed")


class BotoS3ObjectTransport:
    """Adapter for an injected boto3-compatible S3 client.

    The caller owns authentication, TLS verification, connection-pool limits,
    timeouts and SDK retry limits.  Response bodies are still bounded here.
    """

    def __init__(
        self,
        client: Any,
        *,
        server_side_encryption: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        for name in ("put_object", "head_object", "get_object", "delete_object"):
            if not callable(getattr(client, name, None)):
                raise TypeError("S3 client does not implement the required operations")
        self.client = client
        if server_side_encryption not in {None, "AES256", "aws:kms"}:
            raise ValueError("S3 server-side encryption is invalid")
        if kms_key_id is not None and (
            server_side_encryption != "aws:kms"
            or not isinstance(kms_key_id, str)
            or not 1 <= len(kms_key_id) <= 2048
            or any(ord(character) < 32 for character in kms_key_id)
        ):
            raise ValueError("S3 KMS key identity is invalid")
        self.server_side_encryption = server_side_encryption
        self.kms_key_id = kms_key_id

    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str],
        checksum_sha256: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        arguments: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": content_type,
            "Metadata": dict(metadata),
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(checksum_sha256)).decode(
                "ascii"
            ),
        }
        if if_none_match:
            arguments["IfNoneMatch"] = "*"
        if if_match is not None:
            arguments["IfMatch"] = if_match
        if self.server_side_encryption is not None:
            arguments["ServerSideEncryption"] = self.server_side_encryption
        if self.kms_key_id is not None:
            arguments["SSEKMSKeyId"] = self.kms_key_id
        try:
            response = self.client.put_object(**arguments)
        except Exception as error:
            raise _translate_client_error(error) from None
        if not isinstance(response, Mapping):
            raise S3ObjectError("object store returned an invalid response")
        return _etag(response.get("ETag"))

    def head_object(self, *, bucket: str, key: str) -> S3ObjectInfo:
        try:
            response = self.client.head_object(
                Bucket=bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except Exception as error:
            raise _translate_client_error(error) from None
        return self._info(response)

    def get_object(
        self, *, bucket: str, key: str, max_bytes: int
    ) -> S3ObjectBody:
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("object read bound is invalid")
        try:
            response = self.client.get_object(
                Bucket=bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except Exception as error:
            raise _translate_client_error(error) from None
        info = self._info(response)
        if info.size_bytes > max_bytes:
            self._close_body(response.get("Body"))
            raise S3ObjectError("object exceeds its read bound")
        body = response.get("Body")
        try:
            if isinstance(body, bytes):
                payload = body
            elif callable(getattr(body, "read", None)):
                payload = body.read(max_bytes + 1)
            else:
                raise S3ObjectError("object store returned an invalid body")
        except S3ObjectError:
            raise
        except Exception:
            raise S3ObjectError("object body could not be read") from None
        finally:
            self._close_body(body)
        if not isinstance(payload, bytes) or len(payload) > max_bytes:
            raise S3ObjectError("object exceeds its read bound")
        if len(payload) != info.size_bytes:
            raise S3ObjectError("object body length changed")
        return S3ObjectBody(info, payload)

    def delete_object(
        self, *, bucket: str, key: str, if_match: str | None = None
    ) -> None:
        arguments: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if if_match is not None:
            arguments["IfMatch"] = if_match
        try:
            self.client.delete_object(**arguments)
        except Exception as error:
            raise _translate_client_error(error) from None

    @staticmethod
    def _close_body(body: Any) -> None:
        close = getattr(body, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _info(response: Any) -> S3ObjectInfo:
        if not isinstance(response, Mapping):
            raise S3ObjectError("object store returned invalid metadata")
        raw_metadata = response.get("Metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise S3ObjectError("object store returned invalid custom metadata")
        try:
            return S3ObjectInfo(
                _etag(response.get("ETag")),
                int(response.get("ContentLength")),
                str(response.get("ContentType") or "application/octet-stream"),
                {str(key).casefold(): str(value) for key, value in raw_metadata.items()},
                _hex_checksum(response.get("ChecksumSHA256")),
            )
        except (TypeError, ValueError):
            raise S3ObjectError("object store returned invalid metadata") from None


class S3HTTPObjectTransport:
    """Path-style S3 transport for an injected or bounded ``httpx.Client``.

    Authentication can be supplied by the injected client (for example a
    SigV4 ``httpx.Auth`` implementation or a private gateway).  Access keys are
    intentionally not constructor arguments.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: Any | None = None,
        allow_http: bool = False,
        timeout_seconds: float = 30.0,
        max_connections: int = 32,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("S3 endpoint is invalid")
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("S3 timeout is invalid")
        if not 1 <= max_connections <= 256:
            raise ValueError("S3 connection bound is invalid")
        self.endpoint = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self._owns_client = client is None
        if client is None:
            try:
                import httpx
            except ImportError:
                raise RuntimeError("S3 HTTP transport requires httpx") from None
            client = httpx.Client(
                timeout=httpx.Timeout(timeout_seconds),
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                ),
                follow_redirects=False,
            )
        if not callable(getattr(client, "stream", None)):
            raise TypeError("S3 HTTP client is invalid")
        self.client = client

    def close(self) -> None:
        if self._owns_client:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()

    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str],
        checksum_sha256: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": content_type,
            "x-amz-checksum-sha256": base64.b64encode(
                bytes.fromhex(checksum_sha256)
            ).decode("ascii"),
            **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
        }
        if if_none_match:
            headers["If-None-Match"] = "*"
        if if_match is not None:
            headers["If-Match"] = if_match
        try:
            with self.client.stream(
                "PUT",
                self._url(bucket, key),
                headers=headers,
                content=payload,
            ) as response:
                self._check_response(response)
                return _etag(response.headers.get("etag"))
        except S3ObjectError:
            raise
        except Exception:
            raise S3ObjectError("object store request failed") from None

    def head_object(self, *, bucket: str, key: str) -> S3ObjectInfo:
        try:
            with self.client.stream(
                "HEAD",
                self._url(bucket, key),
                headers={"x-amz-checksum-mode": "ENABLED"},
            ) as response:
                self._check_response(response)
                return self._http_info(response.headers)
        except S3ObjectError:
            raise
        except Exception:
            raise S3ObjectError("object store request failed") from None

    def get_object(
        self, *, bucket: str, key: str, max_bytes: int
    ) -> S3ObjectBody:
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("object read bound is invalid")
        try:
            with self.client.stream(
                "GET",
                self._url(bucket, key),
                headers={"x-amz-checksum-mode": "ENABLED"},
            ) as response:
                self._check_response(response)
                info = self._http_info(response.headers)
                if info.size_bytes > max_bytes:
                    raise S3ObjectError("object exceeds its read bound")
                payload = bytearray()
                for chunk in response.iter_bytes(
                    chunk_size=min(64 * 1024, max_bytes + 1)
                ):
                    if len(payload) + len(chunk) > max_bytes:
                        raise S3ObjectError("object exceeds its read bound")
                    payload.extend(chunk)
        except S3ObjectError:
            raise
        except Exception:
            raise S3ObjectError("object store request failed") from None
        if len(payload) != info.size_bytes:
            raise S3ObjectError("object body length changed")
        return S3ObjectBody(info, bytes(payload))

    def delete_object(
        self, *, bucket: str, key: str, if_match: str | None = None
    ) -> None:
        headers = {"If-Match": if_match} if if_match is not None else {}
        try:
            with self.client.stream(
                "DELETE",
                self._url(bucket, key),
                headers=headers,
            ) as response:
                self._check_response(response)
        except S3ObjectError:
            raise
        except Exception:
            raise S3ObjectError("object store request failed") from None

    def _url(self, bucket: str, key: str) -> str:
        return f"{self.endpoint}/{quote(bucket, safe='')}/{quote(key, safe='/')}"

    @staticmethod
    def _check_response(response: Any) -> None:
        status = int(getattr(response, "status_code", 0))
        if status == 404:
            raise S3ObjectNotFound("object is missing")
        if status in {409, 412}:
            raise S3ObjectPreconditionFailed("object precondition failed")
        if not 200 <= status < 300:
            raise S3ObjectError("object store request failed")

    @staticmethod
    def _http_info(headers: Mapping[str, str]) -> S3ObjectInfo:
        normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
        metadata = {
            key.removeprefix("x-amz-meta-"): value
            for key, value in normalized.items()
            if key.startswith("x-amz-meta-")
        }
        try:
            return S3ObjectInfo(
                _etag(normalized.get("etag")),
                int(normalized.get("content-length", "-1")),
                normalized.get("content-type", "application/octet-stream"),
                metadata,
                _hex_checksum(normalized.get("x-amz-checksum-sha256")),
            )
        except (TypeError, ValueError):
            raise S3ObjectError("object store returned invalid metadata") from None


@dataclass(frozen=True, slots=True)
class _ReferenceDocument:
    result: ImageResult
    references: tuple[ImageContentReference, ...]
    state: str
    content_etag: str
    etag: str

    def projection(self) -> ImageContentMetadata:
        return ImageContentMetadata(
            self.result,
            self.references,
            self.etag,
            self.state,
        )


class S3ImageContentStore:
    """Multi-worker S3-compatible image CAS.

    Blob writes use ``If-None-Match: *`` and SHA-256 commitments.  Reference
    documents use bounded ETag compare-and-swap.  Deletion first turns an empty
    active reference document into a tombstone; reference writers then fail
    closed, so a concurrent owner can never be added to an object being
    deleted.
    """

    deployment_scope = "shared"

    def __init__(
        self,
        transport: S3ObjectTransport,
        *,
        bucket: str,
        prefix: str = "ecorex/images/v1",
        max_bytes: int = 256 * 1024 * 1024,
        metadata_attempts: int = 8,
    ) -> None:
        if not isinstance(transport, S3ObjectTransport):
            raise TypeError("S3 object transport is invalid")
        if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket):
            raise ValueError("S3 bucket is invalid")
        normalized_prefix = prefix.strip("/")
        prefix_segments = normalized_prefix.split("/")
        if (
            not normalized_prefix
            or not _PREFIX.fullmatch(normalized_prefix)
            or any(segment in {"", ".", ".."} for segment in prefix_segments)
        ):
            raise ValueError("S3 key prefix is invalid")
        if not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise ValueError("image CAS size bound is invalid")
        if not 1 <= metadata_attempts <= 32:
            raise ValueError("S3 metadata retry bound is invalid")
        self.transport = transport
        self.bucket = bucket
        self.prefix = normalized_prefix
        self.max_bytes = max_bytes
        self.metadata_attempts = metadata_attempts

    def put(
        self,
        payload: bytes,
        *,
        mime_type: str,
        expected_sha256: str | None = None,
        reference: ImageContentReference | None = None,
    ) -> ImageResult:
        result = validate_image_payload(
            payload,
            mime_type=mime_type,
            max_bytes=self.max_bytes,
            expected_sha256=expected_sha256,
        )
        checksum = result.sha256
        try:
            self.transport.put_object(
                bucket=self.bucket,
                key=self._blob_key(checksum),
                payload=payload,
                content_type=result.mime_type,
                metadata=self._blob_metadata(result),
                checksum_sha256=checksum,
                if_none_match=True,
            )
        except S3ObjectPreconditionFailed:
            pass
        body = self._verified_body(result)
        self._ensure_reference_document(result, body.info.etag)
        if reference is not None:
            self.add_reference(result.sha256, reference)
        return result

    def read(self, sha256: str) -> bytes:
        self._validate_digest(sha256)
        body = self.transport.get_object(
            bucket=self.bucket,
            key=self._blob_key(sha256),
            max_bytes=self.max_bytes,
        )
        try:
            result = self._result_from_info(sha256, body.info)
        except ImageResultRejected:
            raise
        self._validate_body_payload(body, result)
        return body.payload

    def describe(self, sha256: str) -> ImageContentMetadata:
        self._validate_digest(sha256)
        body = self.transport.get_object(
            bucket=self.bucket,
            key=self._blob_key(sha256),
            max_bytes=self.max_bytes,
        )
        result = self._result_from_info(sha256, body.info)
        self._validate_body_payload(body, result)
        document = self._load_reference_document(sha256)
        self._match_reference_document(document, result, body.info.etag)
        return document.projection()

    def add_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        return self._mutate_reference(sha256, reference, add=True).projection()

    def release_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        return self._mutate_reference(sha256, reference, add=False).projection()

    def delete_if_unreferenced(
        self, sha256: str, *, expected_reference_version: str
    ) -> bool:
        self._validate_digest(sha256)
        if not isinstance(expected_reference_version, str) or not _ETAG.fullmatch(
            expected_reference_version
        ):
            raise ValueError("expected reference version is invalid")
        current = self._load_reference_document(sha256)
        if (
            current.etag != expected_reference_version
            or current.state != "active"
            or current.references
        ):
            return False
        try:
            tombstone = self._write_reference_document(
                current.result,
                (),
                state="deleting",
                content_etag=current.content_etag,
                if_match=current.etag,
            )
        except S3ObjectPreconditionFailed:
            # A reference writer won after our read.  Never delete against a
            # stale ownership projection.
            return False
        try:
            self.transport.delete_object(
                bucket=self.bucket,
                key=self._blob_key(sha256),
                if_match=current.content_etag,
            )
        except S3ObjectPreconditionFailed:
            return False
        except S3ObjectNotFound:
            pass
        # A failed reference-document cleanup leaves a tombstone.  That is a
        # recoverable metadata leak, never an unsafe resurrection or deletion.
        try:
            self.transport.delete_object(
                bucket=self.bucket,
                key=self._reference_key(sha256),
                if_match=tombstone.etag,
            )
        except (S3ObjectError, S3ObjectNotFound):
            pass
        return True

    def reconcile_deletion(self, sha256: str) -> bool:
        """Finish a tombstoned delete after a crash or transient cleanup error.

        No expected active-version token is needed: the already-committed
        ``deleting`` state is itself the fence.  Content is still removed only
        under the ETag captured before the tombstone was written.
        """

        self._validate_digest(sha256)
        tombstone = self._load_reference_document(sha256)
        if tombstone.state != "deleting":
            return False
        if tombstone.references:
            raise ImageResultRejected("CAS deletion tombstone is invalid")
        try:
            self.transport.delete_object(
                bucket=self.bucket,
                key=self._blob_key(sha256),
                if_match=tombstone.content_etag,
            )
        except S3ObjectNotFound:
            pass
        except S3ObjectPreconditionFailed:
            raise ImageResultRejected(
                "CAS tombstone content commitment changed"
            ) from None
        try:
            self.transport.delete_object(
                bucket=self.bucket,
                key=self._reference_key(sha256),
                if_match=tombstone.etag,
            )
        except S3ObjectNotFound:
            pass
        except S3ObjectPreconditionFailed:
            return False
        return True

    def _verified_body(self, expected: ImageResult) -> S3ObjectBody:
        body = self.transport.get_object(
            bucket=self.bucket,
            key=self._blob_key(expected.sha256),
            max_bytes=self.max_bytes,
        )
        actual = self._result_from_info(expected.sha256, body.info)
        if actual != expected:
            raise ImageResultRejected("CAS identity collides with different metadata")
        self._validate_body_payload(body, expected)
        return body

    def _validate_body_payload(
        self,
        body: S3ObjectBody,
        expected: ImageResult,
    ) -> None:
        """Verify transport, digest *and* file signature on every read.

        Object metadata is not content authority.  This also catches an
        operator or compromised compatible store rewriting both MIME metadata
        fields while retaining a blob under the old digest key.
        """

        actual = validate_image_payload(
            body.payload,
            mime_type=expected.mime_type,
            max_bytes=self.max_bytes,
            expected_sha256=expected.sha256,
        )
        if actual != expected or body.info.size_bytes != expected.size_bytes:
            raise ImageResultRejected("CAS object size commitment changed")
        if body.info.checksum_sha256 is not None and not secrets.compare_digest(
            body.info.checksum_sha256,
            expected.sha256,
        ):
            raise ImageResultRejected("CAS transport checksum changed")

    def _ensure_reference_document(
        self, result: ImageResult, content_etag: str
    ) -> _ReferenceDocument:
        for _attempt in range(self.metadata_attempts):
            try:
                existing = self._load_reference_document(result.sha256)
            except S3ObjectNotFound:
                try:
                    return self._write_reference_document(
                        result,
                        (),
                        state="active",
                        content_etag=content_etag,
                        if_none_match=True,
                    )
                except S3ObjectPreconditionFailed:
                    continue
            self._match_reference_document(existing, result, content_etag)
            return existing
        raise ImageResultRejected("CAS reference metadata is contended")

    def _mutate_reference(
        self,
        sha256: str,
        reference: ImageContentReference,
        *,
        add: bool,
    ) -> _ReferenceDocument:
        self._validate_digest(sha256)
        for _attempt in range(self.metadata_attempts):
            current = self._load_reference_document(sha256)
            if current.state != "active":
                raise ImageResultRejected("CAS object is being deleted")
            references = set(current.references)
            before = len(references)
            if add:
                references.add(reference)
            else:
                references.discard(reference)
            if len(references) == before:
                return current
            if len(references) > _MAX_REFERENCE_COUNT:
                raise ImageResultRejected("CAS reference limit is exceeded")
            try:
                return self._write_reference_document(
                    current.result,
                    tuple(sorted(references)),
                    state="active",
                    content_etag=current.content_etag,
                    if_match=current.etag,
                )
            except S3ObjectPreconditionFailed:
                continue
        raise ImageResultRejected("CAS reference metadata is contended")

    def _load_reference_document(self, sha256: str) -> _ReferenceDocument:
        body = self.transport.get_object(
            bucket=self.bucket,
            key=self._reference_key(sha256),
            max_bytes=_MAX_REFERENCE_DOCUMENT_BYTES,
        )
        reference_metadata = {
            str(key).casefold(): str(value) for key, value in body.info.metadata.items()
        }
        if (
            body.info.content_type.split(";", 1)[0].strip().casefold()
            != "application/json"
            or reference_metadata.get("ecorex-kind") != "references"
            or reference_metadata.get("ecorex-schema") != "1"
            or hashlib.sha256(body.payload).hexdigest()
            != reference_metadata.get("ecorex-sha256")
        ):
            raise ImageResultRejected("CAS reference metadata checksum changed")
        if body.info.checksum_sha256 is not None and body.info.checksum_sha256 != hashlib.sha256(
            body.payload
        ).hexdigest():
            raise ImageResultRejected("CAS reference transport checksum changed")
        try:
            raw = json.loads(body.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ImageResultRejected("CAS reference metadata is unreadable") from None
        if not isinstance(raw, dict) or set(raw) != {
            "content_etag",
            "mime_type",
            "references",
            "schema_version",
            "sha256",
            "size_bytes",
            "state",
        }:
            raise ImageResultRejected("CAS reference metadata is invalid")
        if raw["schema_version"] != 1 or raw["sha256"] != sha256:
            raise ImageResultRejected("CAS reference metadata identity changed")
        try:
            result = ImageResult(raw["sha256"], raw["size_bytes"], raw["mime_type"])
            references = tuple(
                sorted(ImageContentReference.parse(item) for item in raw["references"])
            )
            if (
                raw["state"] not in {"active", "deleting"}
                or len(references) > _MAX_REFERENCE_COUNT
                or len(set(references)) != len(references)
            ):
                raise ValueError("non-canonical reference metadata")
            return _ReferenceDocument(
                result,
                references,
                raw["state"],
                _etag(raw["content_etag"]),
                body.info.etag,
            )
        except (TypeError, ValueError, S3ObjectError):
            raise ImageResultRejected("CAS reference metadata is invalid") from None

    def _write_reference_document(
        self,
        result: ImageResult,
        references: tuple[ImageContentReference, ...],
        *,
        state: str,
        content_etag: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> _ReferenceDocument:
        if if_none_match and if_match is not None:
            raise ValueError("reference write preconditions are mutually exclusive")
        canonical_references = tuple(sorted(set(references)))
        if state not in {"active", "deleting"} or len(
            canonical_references
        ) > _MAX_REFERENCE_COUNT:
            raise ImageResultRejected("CAS reference metadata is invalid")
        document = {
            "content_etag": _etag(content_etag),
            "mime_type": result.mime_type,
            "references": [item.key for item in canonical_references],
            "schema_version": 1,
            "sha256": result.sha256,
            "size_bytes": result.size_bytes,
            "state": state,
        }
        payload = canonical_json(document).encode("utf-8")
        if len(payload) > _MAX_REFERENCE_DOCUMENT_BYTES:
            raise ImageResultRejected("CAS reference metadata is oversized")
        checksum = hashlib.sha256(payload).hexdigest()
        etag = self.transport.put_object(
            bucket=self.bucket,
            key=self._reference_key(result.sha256),
            payload=payload,
            content_type="application/json",
            metadata={
                "ecorex-kind": "references",
                "ecorex-sha256": checksum,
                "ecorex-schema": "1",
            },
            checksum_sha256=checksum,
            if_none_match=if_none_match,
            if_match=if_match,
        )
        return _ReferenceDocument(
            result,
            canonical_references,
            state,
            content_etag,
            etag,
        )

    @staticmethod
    def _match_reference_document(
        document: _ReferenceDocument, result: ImageResult, content_etag: str
    ) -> None:
        if document.result != result or document.content_etag != content_etag:
            raise ImageResultRejected("CAS reference metadata does not match content")
        if document.state != "active":
            raise ImageResultRejected("CAS object is being deleted")

    @staticmethod
    def _blob_metadata(result: ImageResult) -> dict[str, str]:
        return {
            "ecorex-kind": "blob",
            "ecorex-mime": result.mime_type,
            "ecorex-schema": "1",
            "ecorex-sha256": result.sha256,
            "ecorex-size": str(result.size_bytes),
        }

    @staticmethod
    def _result_from_info(sha256: str, info: S3ObjectInfo) -> ImageResult:
        metadata = {str(key).casefold(): str(value) for key, value in info.metadata.items()}
        if (
            metadata.get("ecorex-kind") != "blob"
            or metadata.get("ecorex-schema") != "1"
            or metadata.get("ecorex-sha256") != sha256
        ):
            raise ImageResultRejected("CAS object metadata is invalid")
        try:
            result = ImageResult(
                sha256,
                int(metadata["ecorex-size"]),
                metadata["ecorex-mime"],
            )
        except (KeyError, TypeError, ValueError):
            raise ImageResultRejected("CAS object metadata is invalid") from None
        if info.size_bytes != result.size_bytes:
            raise ImageResultRejected("CAS object size metadata changed")
        if info.content_type.split(";", 1)[0].strip().casefold() != result.mime_type:
            raise ImageResultRejected("CAS object content type changed")
        return result

    def _blob_key(self, sha256: str) -> str:
        return f"{self.prefix}/blobs/{sha256[:2]}/{sha256[2:4]}/{sha256}"

    def _reference_key(self, sha256: str) -> str:
        return f"{self.prefix}/references/{sha256[:2]}/{sha256[2:4]}/{sha256}.json"

    @staticmethod
    def _validate_digest(sha256: str) -> None:
        if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
            raise ValueError("image CAS digest is invalid")


assert isinstance(S3ImageContentStore, type)


__all__ = [
    "BotoS3ObjectTransport",
    "S3HTTPObjectTransport",
    "S3ImageContentStore",
    "S3ObjectBody",
    "S3ObjectError",
    "S3ObjectInfo",
    "S3ObjectNotFound",
    "S3ObjectPreconditionFailed",
    "S3ObjectTransport",
]
