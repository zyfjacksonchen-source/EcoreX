"""Cross-file authority for one published v0.3 import generation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from .cas_authority import CAS_AUTHORITY_NAME, validate_cas_authority
from .errors import MigrationError, MigrationVerificationError
from .migrator import BACKUP_MANIFEST_NAME, INVENTORY_NAME
from .path_security import secure_directory, stable_read_bytes, stable_sha256_file
from .quarantine import MigrationQuarantineService


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_AUTHORITY_BYTES = 64 * 1024
_REPORT_DOMAIN = b"EcoreX v0.3 migration report authority v1\0"
_INVENTORY_FILE_DOMAIN = b"EcoreX v0.3 source inventory file v1\0"
_BACKUP_MANIFEST_DOMAIN = b"EcoreX v0.3 backup manifest v1\0"
_CAS_AUTHORITY_DOMAIN = b"EcoreX v0.3 CAS authority file v1\0"


@dataclass(frozen=True, slots=True)
class TargetFileAuthority:
    report_sha256: str
    source_inventory_file_sha256: str
    backup_manifest_sha256: str
    cas_authority_sha256: str
    quarantine_sha256: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "report_sha256": self.report_sha256,
            "source_inventory_file_sha256": self.source_inventory_file_sha256,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "cas_authority_sha256": self.cas_authority_sha256,
            "quarantine_sha256": self.quarantine_sha256,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _authority_digest(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + _canonical(value)).hexdigest()


def report_sha256(report: Mapping[str, Any]) -> str:
    return _authority_digest(_REPORT_DOMAIN, dict(report))


def _read_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    try:
        raw = stable_read_bytes(path, label=label, maximum=maximum)
        if not raw:
            raise MigrationVerificationError(f"{label} is empty")
        value = json.loads(raw)
    except MigrationVerificationError:
        raise
    except (MigrationError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationVerificationError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise MigrationVerificationError(f"{label} is invalid")
    return value


def _safe_relative(value: object, *, prefix: str | None = None) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise MigrationVerificationError("migration authority contains an unsafe path")
    normalized = path.as_posix()
    if prefix is not None and not normalized.startswith(prefix):
        raise MigrationVerificationError("migration authority path is outside its domain")
    return normalized


def _verify_inventory(target: Path, report: Mapping[str, Any]) -> str:
    value = _read_json(
        target / INVENTORY_NAME,
        maximum=_MAX_INVENTORY_BYTES,
        label="migration source inventory",
    )
    if set(value) != {
        "source_version",
        "digest",
        "file_count",
        "entry_count",
        "total_bytes",
        "entries",
    }:
        raise MigrationVerificationError("migration source inventory contract is invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > 500_000:
        raise MigrationVerificationError("migration source inventory entries are invalid")
    normalized: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "relative_path",
            "kind",
            "size_bytes",
            "mtime_ns",
            "sha256",
        }:
            raise MigrationVerificationError("migration source inventory entry is invalid")
        relative = str(entry.get("relative_path") or "")
        if relative.startswith("@pinned/"):
            _safe_relative(relative.removeprefix("@pinned/"))
        else:
            _safe_relative(relative)
        size = entry.get("size_bytes")
        mtime = entry.get("mtime_ns")
        digest = entry.get("sha256")
        if (
            relative in seen
            or entry.get("kind") != "file"
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(mtime, bool)
            or not isinstance(mtime, int)
            or mtime < 0
            or not isinstance(digest, str)
            or _HEX_64.fullmatch(digest) is None
        ):
            raise MigrationVerificationError("migration source inventory entry is invalid")
        seen.add(relative)
        total += size
        normalized.append(dict(entry))
    inventory_digest = hashlib.sha256(_canonical(normalized)).hexdigest()
    if (
        value.get("source_version") != "0.3.0"
        or value.get("digest") != report.get("source_inventory_digest")
        or value.get("digest") != inventory_digest
        or value.get("file_count") != len(normalized)
        or value.get("entry_count") != len(normalized)
        or value.get("total_bytes") != total
    ):
        raise MigrationVerificationError("migration source inventory is inconsistent")
    return _authority_digest(_INVENTORY_FILE_DOMAIN, value)


def _verify_backups(
    target: Path, report: Mapping[str, Any], *, verify_content: bool
) -> str:
    value = _read_json(
        target / BACKUP_MANIFEST_NAME,
        maximum=_MAX_MANIFEST_BYTES,
        label="migration backup manifest",
    )
    backups = report.get("backups")
    if (
        set(value) != {
            "schema_version",
            "source_inventory_digest",
            "source_unchanged",
            "backups",
        }
        or value.get("schema_version") != 1
        or value.get("source_inventory_digest") != report.get("source_inventory_digest")
        or value.get("source_unchanged") is not True
        or not isinstance(backups, list)
        or value.get("backups") != backups
    ):
        raise MigrationVerificationError("migration backup manifest is inconsistent")
    seen: set[str] = set()
    for backup in backups:
        if not isinstance(backup, dict) or set(backup) != {
            "source_relative_path",
            "backup_relative_path",
            "source_sha256",
            "backup_sha256",
            "kind",
        }:
            raise MigrationVerificationError("migration backup record is invalid")
        relative = _safe_relative(
            backup.get("backup_relative_path"), prefix="backups/"
        )
        _safe_relative(backup.get("source_relative_path"))
        if (
            relative in seen
            or backup.get("kind") != "sqlite_snapshot"
            or _HEX_64.fullmatch(str(backup.get("source_sha256") or "")) is None
            or _HEX_64.fullmatch(str(backup.get("backup_sha256") or "")) is None
        ):
            raise MigrationVerificationError("migration backup record is invalid")
        seen.add(relative)
        if verify_content:
            digest, _identity = stable_sha256_file(
                target / PurePosixPath(relative),
                label="migration database backup",
                root=target,
            )
            if digest != backup["backup_sha256"]:
                raise MigrationVerificationError("migration database backup digest changed")
    return _authority_digest(_BACKUP_MANIFEST_DOMAIN, value)


def _verify_cas(
    target: Path,
    report: Mapping[str, Any],
    referenced_digests: Iterable[str],
    *,
    verify_blob_content: bool,
) -> str:
    normalized = tuple(sorted(set(str(value) for value in referenced_digests)))
    value = _read_json(
        target / CAS_AUTHORITY_NAME,
        maximum=_MAX_AUTHORITY_BYTES,
        label="migration CAS authority",
    )
    try:
        validate_cas_authority(
            value,
            source_inventory_digest=str(report["source_inventory_digest"]),
            digests=normalized,
        )
    except MigrationError as error:
        raise MigrationVerificationError("migration CAS authority is inconsistent") from error
    blob_root = target / "artifacts" / "blobs"
    if normalized:
        secure_directory(blob_root, label="migration CAS root", root=target)
    if verify_blob_content:
        for digest in normalized:
            path = blob_root / digest[:2] / digest[2:4] / digest
            observed, _identity = stable_sha256_file(
                path,
                label="migration CAS blob",
                root=blob_root,
            )
            if observed != digest:
                raise MigrationVerificationError("migration CAS blob digest changed")
    return _authority_digest(_CAS_AUTHORITY_DOMAIN, value)


def verify_target_file_authority(
    target: Path,
    report: Mapping[str, Any],
    *,
    referenced_digests: Iterable[str],
    verify_blob_content: bool,
    verify_static_content: bool = True,
    expected_authority: Mapping[str, Any] | None = None,
) -> TargetFileAuthority:
    root = secure_directory(target, label="migrated Runtime state")
    report_digest = report_sha256(report)
    if verify_static_content:
        inventory_digest = _verify_inventory(root, report)
        backup_digest = _verify_backups(root, report, verify_content=True)
    else:
        if expected_authority is None:
            raise MigrationVerificationError(
                "expected migration file authority is unavailable"
            )
        inventory_digest = str(
            expected_authority.get("source_inventory_file_sha256") or ""
        )
        backup_digest = str(expected_authority.get("backup_manifest_sha256") or "")
        if (
            _HEX_64.fullmatch(inventory_digest) is None
            or _HEX_64.fullmatch(backup_digest) is None
        ):
            raise MigrationVerificationError(
                "expected migration file authority is invalid"
            )
    cas_digest = _verify_cas(
        root,
        report,
        referenced_digests,
        verify_blob_content=verify_blob_content,
    )
    quarantine_count = int(
        report.get("quarantine", {}).get("entry_count", 0)
        if isinstance(report.get("quarantine"), Mapping)
        else 0
    )
    quarantine_service = MigrationQuarantineService(root)
    projection, quarantine_digest = quarantine_service.verified_authority()
    if projection.entry_count != quarantine_count:
        raise MigrationVerificationError("migration quarantine count is inconsistent")
    if quarantine_count and quarantine_digest is None:
        raise MigrationVerificationError("migration quarantine authority is missing")
    return TargetFileAuthority(
        report_sha256=report_digest,
        source_inventory_file_sha256=inventory_digest,
        backup_manifest_sha256=backup_digest,
        cas_authority_sha256=cas_digest,
        quarantine_sha256=quarantine_digest,
    )


__all__ = [
    "TargetFileAuthority",
    "report_sha256",
    "verify_target_file_authority",
]
