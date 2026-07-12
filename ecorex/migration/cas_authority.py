"""Persistent authority for the immutable blobs created by legacy import."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .errors import MigrationVerificationError


CAS_AUTHORITY_NAME = "artifacts/migration-cas-authority.json"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SET_DOMAIN = b"EcoreX v0.3 imported CAS digest set v1\0"


def digest_set_sha256(values: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    digests = tuple(sorted(set(str(value) for value in values)))
    if any(_HEX_64.fullmatch(value) is None for value in digests):
        raise MigrationVerificationError("migration CAS authority contains an invalid digest")
    encoded = json.dumps(
        list(digests),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_SET_DOMAIN + encoded).hexdigest(), digests


def build_cas_authority(
    *, source_inventory_digest: str, digests: Iterable[str]
) -> dict[str, Any]:
    if _HEX_64.fullmatch(str(source_inventory_digest)) is None:
        raise MigrationVerificationError("migration CAS source digest is invalid")
    root, normalized = digest_set_sha256(digests)
    return {
        "schema_version": 1,
        "source_inventory_digest": source_inventory_digest,
        "blob_count": len(normalized),
        "digest_set_sha256": root,
    }


def validate_cas_authority(
    value: Mapping[str, Any],
    *,
    source_inventory_digest: str,
    digests: Iterable[str],
) -> dict[str, Any]:
    expected = build_cas_authority(
        source_inventory_digest=source_inventory_digest,
        digests=digests,
    )
    if dict(value) != expected:
        raise MigrationVerificationError("migration CAS authority is inconsistent")
    return expected


__all__ = [
    "CAS_AUTHORITY_NAME",
    "build_cas_authority",
    "digest_set_sha256",
    "validate_cas_authority",
]
