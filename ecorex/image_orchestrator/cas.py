"""Verified image content-addressed storage contracts.

The cloud worker consumes :class:`ImageContentAddressedStore`; it does not
assume that all workers share a local filesystem.  ``ImageContentStore`` is a
small, defensive filesystem implementation retained for local development and
tests.  Production object-storage support lives in :mod:`.s3_cas`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock
from typing import Protocol, runtime_checkable

from .models import ImageResult, ImageResultRejected, canonical_json


_REFERENCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_REFERENCE_COUNT = 1024
_MAX_REFERENCE_DOCUMENT_BYTES = 64 * 1024


def _has_avif_brand(payload: bytes) -> bool:
    if len(payload) < 16 or payload[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(payload[:4], "big")
    if box_size < 16 or box_size > len(payload):
        return False
    brands = {payload[8:12]}
    brands.update(
        payload[offset : offset + 4]
        for offset in range(16, box_size - 3, 4)
    )
    return bool(brands & {b"avif", b"avis"})


@dataclass(frozen=True, slots=True, order=True)
class ImageContentReference:
    """Opaque durable owner of one CAS blob.

    Reference identifiers may name a job or tenant-scoped input registration,
    but may not contain paths or credentials.  They are stored in the private
    CAS reference index and are never returned by the public image API.
    """

    kind: str
    reference_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _REFERENCE_KIND.fullmatch(self.kind):
            raise ValueError("image CAS reference kind is invalid")
        if not isinstance(self.reference_id, str) or not _REFERENCE_ID.fullmatch(
            self.reference_id
        ):
            raise ValueError("image CAS reference identity is invalid")

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.reference_id}"

    @classmethod
    def parse(cls, value: str) -> ImageContentReference:
        kind, separator, reference_id = value.partition(":")
        if not separator:
            raise ValueError("image CAS reference identity is invalid")
        return cls(kind, reference_id)


@dataclass(frozen=True, slots=True)
class ImageContentMetadata:
    result: ImageResult
    references: tuple[ImageContentReference, ...]
    reference_version: str
    state: str = "active"

    def __post_init__(self) -> None:
        if self.state not in {"active", "deleting"}:
            raise ValueError("image CAS metadata state is invalid")
        if len(self.references) > _MAX_REFERENCE_COUNT:
            raise ValueError("image CAS reference limit is exceeded")
        if tuple(sorted(set(self.references))) != self.references:
            raise ValueError("image CAS references are not canonical")
        if not isinstance(self.reference_version, str) or not self.reference_version:
            raise ValueError("image CAS reference version is invalid")


@runtime_checkable
class ImageContentAddressedStore(Protocol):
    """Shared CAS contract used by API and worker processes."""

    max_bytes: int
    deployment_scope: str

    def put(
        self,
        payload: bytes,
        *,
        mime_type: str,
        expected_sha256: str | None = None,
        reference: ImageContentReference | None = None,
    ) -> ImageResult: ...

    def read(self, sha256: str) -> bytes: ...

    def describe(self, sha256: str) -> ImageContentMetadata: ...

    def add_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata: ...

    def release_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata: ...

    def delete_if_unreferenced(
        self, sha256: str, *, expected_reference_version: str
    ) -> bool: ...

    def reconcile_deletion(self, sha256: str) -> bool: ...


def validate_image_payload(
    payload: bytes,
    *,
    mime_type: str,
    max_bytes: int,
    expected_sha256: str | None = None,
) -> ImageResult:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= max_bytes:
        raise ImageResultRejected("provider image result is empty or oversized")
    signatures = {
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/webp": len(payload) >= 12
        and payload.startswith(b"RIFF")
        and payload[8:12] == b"WEBP",
        "image/avif": _has_avif_brand(payload),
    }
    if mime_type not in signatures or not signatures[mime_type]:
        raise ImageResultRejected("provider image signature does not match MIME type")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and not secrets.compare_digest(digest, expected_sha256):
        raise ImageResultRejected("provider image digest commitment does not match")
    return ImageResult(digest, len(payload), mime_type)


class ImageContentStore:
    """Defensive filesystem CAS for tests and single-process development.

    It is deliberately not the production multi-node adapter.  Reference
    mutations and deletion are serialized inside this process; production
    deployments use ``S3ImageContentStore`` and object-store preconditions.
    """

    deployment_scope = "local"

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        if not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise ValueError("image CAS size bound is invalid")
        self.max_bytes = max_bytes
        self._metadata_lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._real_directory(self.root)

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
        parent = self.root / result.sha256[:2] / result.sha256[2:4]
        parent.mkdir(parents=True, exist_ok=True)
        self._real_directory(parent)
        target = parent / result.sha256
        if os.path.lexists(target):
            self._verify_existing(target, result)
        else:
            temporary = parent / f".{result.sha256}.{secrets.token_hex(12)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = None
            try:
                descriptor = os.open(temporary, flags, 0o600)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short CAS write")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                try:
                    os.replace(temporary, target)
                except FileExistsError:
                    pass
                self._verify_existing(target, result)
            except ImageResultRejected:
                raise
            except OSError:
                raise ImageResultRejected(
                    "verified image bytes could not be committed"
                ) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        with self._metadata_lock:
            metadata = self._load_or_create_metadata(result)
            if metadata.result != result:
                raise ImageResultRejected("CAS identity collides with different metadata")
            if reference is not None and reference not in metadata.references:
                metadata = self._write_metadata(
                    result,
                    (*metadata.references, reference),
                    state="active",
                )
        return result

    def read(self, sha256: str) -> bytes:
        self._validate_digest(sha256)
        path = self._blob_path(sha256)
        before = self._regular(path)
        if before.st_size > self.max_bytes:
            raise ImageResultRejected("CAS blob exceeds its size bound")
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                payload = stream.read(self.max_bytes + 1)
                after = os.fstat(stream.fileno())
        except OSError:
            raise ImageResultRejected("CAS blob is unreadable") from None
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            len(payload) > self.max_bytes
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != identity
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != identity
            or hashlib.sha256(payload).hexdigest() != sha256
        ):
            raise ImageResultRejected("CAS blob failed integrity verification")
        # The digest protects identity, while the private reference document
        # commits the media type.  Re-validate the signature so a local disk
        # rewrite of metadata cannot turn arbitrary bytes into an image merely
        # by changing a MIME field.
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
        actual = validate_image_payload(
            payload,
            mime_type=metadata.result.mime_type,
            max_bytes=self.max_bytes,
            expected_sha256=sha256,
        )
        if actual != metadata.result:
            raise ImageResultRejected("CAS metadata commitment changed")
        return payload

    def describe(self, sha256: str) -> ImageContentMetadata:
        self._validate_digest(sha256)
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
        # A descriptor is not authority unless the committed bytes still match.
        payload = self.read(sha256)
        if len(payload) != metadata.result.size_bytes:
            raise ImageResultRejected("CAS metadata size commitment changed")
        return metadata

    def add_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
            if metadata.state != "active":
                raise ImageResultRejected("CAS blob is being deleted")
            if reference in metadata.references:
                return metadata
            return self._write_metadata(
                metadata.result,
                (*metadata.references, reference),
                state="active",
            )

    def release_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
            if metadata.state != "active":
                raise ImageResultRejected("CAS blob is being deleted")
            if reference not in metadata.references:
                return metadata
            return self._write_metadata(
                metadata.result,
                tuple(item for item in metadata.references if item != reference),
                state="active",
            )

    def delete_if_unreferenced(
        self, sha256: str, *, expected_reference_version: str
    ) -> bool:
        self._validate_digest(sha256)
        if not isinstance(expected_reference_version, str) or not expected_reference_version:
            raise ValueError("expected reference version is required")
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
            if metadata.reference_version != expected_reference_version:
                return False
            if metadata.state != "active" or metadata.references:
                return False
            tombstone = self._write_metadata(
                metadata.result,
                (),
                state="deleting",
            )
            try:
                self._blob_path(sha256).unlink()
                self._metadata_path(sha256).unlink()
            except FileNotFoundError:
                return False
            except OSError:
                # Restore an active zero-reference record when bytes still
                # exist.  This local implementation has a process lock, so no
                # reference writer can race the restoration.
                if self._blob_path(sha256).exists():
                    self._write_metadata(tombstone.result, (), state="active")
                raise ImageResultRejected("CAS blob could not be deleted") from None
            return True

    def reconcile_deletion(self, sha256: str) -> bool:
        """Resume a crash-left tombstone without reopening reference writes."""

        self._validate_digest(sha256)
        with self._metadata_lock:
            metadata = self._read_metadata(sha256)
            if metadata.state != "deleting":
                return False
            if metadata.references:
                raise ImageResultRejected("CAS deletion tombstone is invalid")
            blob = self._blob_path(sha256)
            if blob.exists():
                self._verify_existing(blob, metadata.result)
                try:
                    blob.unlink()
                except OSError:
                    raise ImageResultRejected(
                        "CAS tombstone could not delete its blob"
                    ) from None
            try:
                self._metadata_path(sha256).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise ImageResultRejected(
                    "CAS tombstone metadata could not be deleted"
                ) from None
            return True

    def _load_or_create_metadata(self, result: ImageResult) -> ImageContentMetadata:
        path = self._metadata_path(result.sha256)
        if path.exists():
            return self._read_metadata(result.sha256)
        return self._write_metadata(result, (), state="active")

    def _read_metadata(self, sha256: str) -> ImageContentMetadata:
        self._validate_digest(sha256)
        path = self._metadata_path(sha256)
        metadata = self._regular(path)
        if metadata.st_size > _MAX_REFERENCE_DOCUMENT_BYTES:
            raise ImageResultRejected("CAS reference metadata is oversized")
        try:
            payload = path.read_bytes()
            raw = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ImageResultRejected("CAS reference metadata is unreadable") from None
        if not isinstance(raw, dict) or set(raw) != {
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
            version = hashlib.sha256(payload).hexdigest()
            return ImageContentMetadata(result, references, version, raw["state"])
        except (TypeError, ValueError):
            raise ImageResultRejected("CAS reference metadata is invalid") from None

    def _write_metadata(
        self,
        result: ImageResult,
        references: tuple[ImageContentReference, ...],
        *,
        state: str,
    ) -> ImageContentMetadata:
        canonical_references = tuple(sorted(set(references)))
        if len(canonical_references) > _MAX_REFERENCE_COUNT:
            raise ImageResultRejected("CAS reference limit is exceeded")
        document = {
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
        target = self._metadata_path(result.sha256)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(12)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError:
            raise ImageResultRejected("CAS reference metadata could not be committed") from None
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return ImageContentMetadata(
            result,
            canonical_references,
            hashlib.sha256(payload).hexdigest(),
            state,
        )

    def _verify_existing(self, path: Path, expected: ImageResult) -> None:
        metadata = self._regular(path)
        if metadata.st_size != expected.size_bytes:
            raise ImageResultRejected("CAS identity collides with a different size")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
        except OSError:
            raise ImageResultRejected("CAS blob is unreadable") from None
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            or digest.hexdigest() != expected.sha256
        ):
            raise ImageResultRejected("CAS blob failed integrity verification")

    def _blob_path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def _metadata_path(self, sha256: str) -> Path:
        return self._blob_path(sha256).with_name(f"{sha256}.refs.json")

    @staticmethod
    def _validate_digest(sha256: str) -> None:
        if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
            raise ValueError("image CAS digest is invalid")

    @staticmethod
    def _regular(path: Path) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError:
            raise ImageResultRejected("CAS blob is missing") from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse)
            or getattr(metadata, "st_nlink", 1) != 1
        ):
            raise ImageResultRejected("CAS blob is not a safe regular file")
        return metadata

    @staticmethod
    def _real_directory(path: Path) -> None:
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse)
        ):
            raise ValueError("image CAS directory is unsafe")


__all__ = [
    "ImageContentAddressedStore",
    "ImageContentMetadata",
    "ImageContentReference",
    "ImageContentStore",
    "validate_image_payload",
]
