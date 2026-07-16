"""Single-host ImageContentAddressedStore over the attested encrypted CAS."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping

from ecorex.storage.attested_local_cas import (
    AttestedEncryptedLocalCAS,
    AttestedLocalCASError,
)

from .cas import (
    ImageContentMetadata,
    ImageContentReference,
    validate_image_payload,
)
from .models import ImageResult, ImageResultRejected, canonical_json


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_REFERENCES = 1024
_ATTEMPTS = 128


class AttestedLocalImageContentStore:
    """Cross-process CAS for one API/worker host.

    ``deployment_scope='shared'`` preserves the existing Image worker protocol:
    API and worker processes really do share durable bytes.  The additional
    availability properties are the authoritative deployment boundary and
    explicitly prohibit multi-host or replica-count > 1 use.
    """

    deployment_scope = "shared"
    availability_scope = "single-host"
    replica_count = 1
    supports_multi_host_ha = False

    def __init__(self, cas: AttestedEncryptedLocalCAS) -> None:
        if not isinstance(cas, AttestedEncryptedLocalCAS) or cas.namespace != "image":
            raise TypeError("attested Image CAS namespace is invalid")
        self.cas = cas
        self.max_bytes = cas.max_blob_bytes

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
        try:
            stored = self.cas.put(payload, expected_sha256=result.sha256)
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        if stored.size_bytes != result.size_bytes:
            raise ImageResultRejected("attested Image CAS size commitment changed")
        metadata = self._load_or_create(result)
        if metadata.result != result or metadata.state != "active":
            raise ImageResultRejected("attested Image CAS metadata conflicts with content")
        if reference is not None:
            metadata = self.add_reference(result.sha256, reference)
            if metadata.result != result:
                raise ImageResultRejected("attested Image CAS metadata conflicts with content")
        return result

    def read(self, sha256: str) -> bytes:
        self._validate_digest(sha256)
        metadata = self._read_metadata(sha256)
        if metadata.state != "active":
            raise ImageResultRejected("attested Image CAS object is being deleted")
        try:
            payload = self.cas.read(sha256)
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        actual = validate_image_payload(
            payload,
            mime_type=metadata.result.mime_type,
            max_bytes=self.max_bytes,
            expected_sha256=sha256,
        )
        if actual != metadata.result:
            raise ImageResultRejected("attested Image CAS metadata commitment changed")
        return payload

    def describe(self, sha256: str) -> ImageContentMetadata:
        metadata = self._read_metadata(sha256)
        payload = self.read(sha256)
        if len(payload) != metadata.result.size_bytes:
            raise ImageResultRejected("attested Image CAS size commitment changed")
        return metadata

    def add_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        return self._mutate_reference(sha256, reference, add=True)

    def release_reference(
        self, sha256: str, reference: ImageContentReference
    ) -> ImageContentMetadata:
        if not isinstance(reference, ImageContentReference):
            raise TypeError("image CAS reference is invalid")
        return self._mutate_reference(sha256, reference, add=False)

    def delete_if_unreferenced(
        self, sha256: str, *, expected_reference_version: str
    ) -> bool:
        self._validate_digest(sha256)
        if not isinstance(expected_reference_version, str) or not expected_reference_version:
            raise ValueError("expected reference version is invalid")
        current = self._read_metadata(sha256)
        if (
            current.reference_version != expected_reference_version
            or current.state != "active"
            or current.references
        ):
            return False
        tombstone = self._write_metadata(
            current.result,
            (),
            state="deleting",
            expected_version=current.reference_version,
        )
        try:
            self.cas.delete(sha256, expected_size=current.result.size_bytes)
            self.cas.delete_record(
                "image-references",
                sha256,
                expected_version=tombstone.reference_version,
            )
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        return True

    def reconcile_deletion(self, sha256: str) -> bool:
        self._validate_digest(sha256)
        current = self._read_metadata(sha256)
        if current.state != "deleting":
            return False
        if current.references:
            raise ImageResultRejected("attested Image CAS tombstone is invalid")
        try:
            self.cas.delete(sha256, expected_size=current.result.size_bytes)
            self.cas.delete_record(
                "image-references",
                sha256,
                expected_version=current.reference_version,
            )
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        return True

    def recover(self) -> Mapping[str, int]:
        """Repair crash-left blob-before-metadata and deletion tombstones."""

        repaired = 0
        deleted = 0
        for sha256 in self.cas.list_blob_digests():
            try:
                metadata = self._read_metadata(sha256)
            except ImageResultRejected as error:
                if str(error) != "attested_local_cas_record_unavailable":
                    raise
                try:
                    payload = self.cas.read(sha256)
                except AttestedLocalCASError as cas_error:
                    raise ImageResultRejected(cas_error.code) from None
                result = _infer_result(payload, self.max_bytes, sha256)
                self._write_metadata(
                    result,
                    (),
                    state="active",
                    expected_version=None,
                )
                repaired += 1
                continue
            if metadata.state == "deleting" and self.reconcile_deletion(sha256):
                deleted += 1
        return {"repaired_orphans": repaired, "reconciled_deletions": deleted}

    def health_probe(self, *, write_probe: bool, deep: bool = False) -> Mapping[str, object]:
        recovered = self.recover()
        base = dict(self.cas.health_probe(write_probe=write_probe, deep=deep))
        return {**base, "image_recovery": recovered}

    def _load_or_create(self, result: ImageResult) -> ImageContentMetadata:
        try:
            current = self._read_metadata(result.sha256)
        except ImageResultRejected as error:
            if str(error) != "attested_local_cas_record_unavailable":
                raise
            try:
                return self._write_metadata(
                    result,
                    (),
                    state="active",
                    expected_version=None,
                )
            except ImageResultRejected as conflict:
                if str(conflict) != "attested_local_cas_record_conflict":
                    raise
                return self._read_metadata(result.sha256)
        return current

    def _mutate_reference(
        self, sha256: str, reference: ImageContentReference, *, add: bool
    ) -> ImageContentMetadata:
        self._validate_digest(sha256)
        for _attempt in range(_ATTEMPTS):
            current = self._read_metadata(sha256)
            if current.state != "active":
                raise ImageResultRejected("attested Image CAS object is being deleted")
            references = set(current.references)
            before = len(references)
            if add:
                references.add(reference)
            else:
                references.discard(reference)
            if len(references) == before:
                return current
            if len(references) > _MAX_REFERENCES:
                raise ImageResultRejected("image CAS reference limit is exceeded")
            try:
                return self._write_metadata(
                    current.result,
                    tuple(sorted(references)),
                    state="active",
                    expected_version=current.reference_version,
                )
            except ImageResultRejected as error:
                if str(error) != "attested_local_cas_record_conflict":
                    raise
                # Multiple API/worker processes may update the same digest's
                # reference set concurrently.  CAS conflicts are expected;
                # use a short bounded backoff so a hot object cannot exhaust
                # the retry budget in one scheduler quantum.
                time.sleep(min(0.0005 * (2 ** min(_attempt, 6)), 0.02))
        raise ImageResultRejected("attested Image CAS metadata is contended")

    def _read_metadata(self, sha256: str) -> ImageContentMetadata:
        self._validate_digest(sha256)
        try:
            record = self.cas.read_record("image-references", sha256)
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        raw = _strict_json(record.payload)
        if not isinstance(raw, dict) or set(raw) != {
            "mime_type",
            "references",
            "schema_version",
            "sha256",
            "size_bytes",
            "state",
        }:
            raise ImageResultRejected("attested Image CAS metadata is invalid")
        try:
            result = ImageResult(raw["sha256"], raw["size_bytes"], raw["mime_type"])
            references = tuple(
                sorted(ImageContentReference.parse(item) for item in raw["references"])
            )
            if (
                raw["schema_version"] != 1
                or result.sha256 != sha256
                or raw["state"] not in {"active", "deleting"}
                or len(references) > _MAX_REFERENCES
                or len(set(references)) != len(references)
            ):
                raise ValueError
            return ImageContentMetadata(result, references, record.version, raw["state"])
        except (KeyError, TypeError, ValueError):
            raise ImageResultRejected("attested Image CAS metadata is invalid") from None

    def _write_metadata(
        self,
        result: ImageResult,
        references: tuple[ImageContentReference, ...],
        *,
        state: str,
        expected_version: str | None,
    ) -> ImageContentMetadata:
        canonical = tuple(sorted(set(references)))
        if state not in {"active", "deleting"} or len(canonical) > _MAX_REFERENCES:
            raise ImageResultRejected("attested Image CAS metadata is invalid")
        value = {
            "schema_version": 1,
            "sha256": result.sha256,
            "size_bytes": result.size_bytes,
            "mime_type": result.mime_type,
            "references": [item.key for item in canonical],
            "state": state,
        }
        payload = canonical_json(value).encode("utf-8")
        try:
            stored = self.cas.compare_exchange_record(
                "image-references",
                result.sha256,
                payload,
                expected_version=expected_version,
            )
        except AttestedLocalCASError as error:
            raise ImageResultRejected(error.code) from None
        return ImageContentMetadata(result, canonical, stored.version, state)

    @staticmethod
    def _validate_digest(value: str) -> None:
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise ValueError("image CAS digest is invalid")


def _strict_json(payload: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise ImageResultRejected("attested Image CAS metadata is invalid") from None


def _infer_result(payload: bytes, max_bytes: int, sha256: str) -> ImageResult:
    for mime_type in ("image/png", "image/jpeg", "image/webp", "image/avif"):
        try:
            return validate_image_payload(
                payload,
                mime_type=mime_type,
                max_bytes=max_bytes,
                expected_sha256=sha256,
            )
        except ImageResultRejected:
            continue
    raise ImageResultRejected("attested Image CAS orphan is not a valid image")


__all__ = ["AttestedLocalImageContentStore"]
