"""Public Share object adapter for the attested single-host encrypted CAS."""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Mapping

from ecorex.storage.attested_local_cas import (
    AttestedEncryptedLocalCAS,
    AttestedLocalCASError,
)

from .share_objects import (
    ShareObjectCapacityError,
    ShareObjectError,
    ShareObjectRead,
    ShareStoredObject,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_KEY = re.compile(r"^local-cas/share/sha256/[0-9a-f]{64}$")


class AttestedLocalShareObjectStore:
    """Share CAS for one host; never returns a filesystem path or public URL."""

    availability_scope = "single-host"
    replica_count = 1
    supports_multi_host_ha = False

    def __init__(self, cas: AttestedEncryptedLocalCAS, *, max_open_streams: int = 32) -> None:
        if not isinstance(cas, AttestedEncryptedLocalCAS) or cas.namespace != "share":
            raise TypeError("attested Share CAS namespace is invalid")
        if (
            isinstance(max_open_streams, bool)
            or not isinstance(max_open_streams, int)
            or not 1 <= max_open_streams <= 1024
        ):
            raise ValueError("attested Share CAS stream limit is invalid")
        self.cas = cas
        self._streams = threading.BoundedSemaphore(max_open_streams)

    def put(self, content: bytes, *, sha256: str, mime_type: str) -> ShareStoredObject:
        if (
            not isinstance(content, bytes)
            or _DIGEST.fullmatch(str(sha256)) is None
            or hashlib.sha256(content).hexdigest() != sha256
            or not isinstance(mime_type, str)
            or _MIME.fullmatch(mime_type) is None
        ):
            raise ShareObjectError("attested Share CAS input is invalid")
        try:
            stored = self.cas.put(content, expected_sha256=sha256)
        except AttestedLocalCASError as error:
            raise ShareObjectError(error.code) from None
        return ShareStoredObject(
            object_key=self._key(stored.sha256),
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            mime_type=mime_type,
            etag=stored.sha256,
        )

    def open(
        self,
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareObjectRead:
        descriptor = self._descriptor(
            object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
        )
        if not self._streams.acquire(blocking=False):
            raise ShareObjectCapacityError("attested Share CAS stream capacity is busy")
        try:
            verified = self.cas.open_verified(sha256, expected_size=size_bytes)
        except AttestedLocalCASError as error:
            self._streams.release()
            raise ShareObjectError(error.code) from None
        return ShareObjectRead(
            descriptor=descriptor,
            _handle=verified.handle,
            _release=self._streams.release,
        )

    def delete(self, object_key: str, *, sha256: str) -> bool:
        if (
            not isinstance(object_key, str)
            or _KEY.fullmatch(object_key) is None
            or object_key != self._key(sha256)
        ):
            raise ShareObjectError("attested Share CAS identity is invalid")
        try:
            return self.cas.delete(sha256)
        except AttestedLocalCASError as error:
            raise ShareObjectError(error.code) from None

    def health_probe(self, *, write_probe: bool, deep: bool = False) -> Mapping[str, object]:
        return self.cas.health_probe(write_probe=write_probe, deep=deep)

    @staticmethod
    def _key(sha256: str) -> str:
        if not isinstance(sha256, str) or _DIGEST.fullmatch(sha256) is None:
            raise ShareObjectError("attested Share CAS digest is invalid")
        return f"local-cas/share/sha256/{sha256}"

    @classmethod
    def _descriptor(
        cls,
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareStoredObject:
        if object_key != cls._key(sha256):
            raise ShareObjectError("attested Share CAS identity is invalid")
        try:
            return ShareStoredObject(
                object_key=object_key,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                etag=sha256,
            )
        except (TypeError, ValueError):
            raise ShareObjectError("attested Share CAS metadata is invalid") from None


__all__ = ["AttestedLocalShareObjectStore"]
