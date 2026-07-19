"""Idempotent read-only import of v0.2.9.2 Django password credentials."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.password_credentials import (
    PasswordCredentialError,
    validate_encoded_password,
)


class LegacyPasswordCredentialImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyPasswordCredentialImportReport:
    schema_version: int
    source_version: str
    dry_run: bool
    source_file_sha256: str
    source_snapshot_sha256: str
    eligible_credentials: int
    imported: int
    replayed: int
    skipped_deleted: int
    skipped_unsupported: int
    skipped_invalid: int
    skipped_missing_target: int
    skipped_admin_reset: int
    import_receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def import_v0292_password_credentials(
    source_database: str | os.PathLike[str],
    target_database: str | os.PathLike[str],
    *,
    dry_run: bool = False,
) -> LegacyPasswordCredentialImportReport:
    source = _regular_file(source_database)
    target = Path(target_database).expanduser().resolve()
    if source == target or target.is_symlink() or not target.is_file():
        raise LegacyPasswordCredentialImportError("password import target is unavailable")
    try:
        AdminManagementSchemaManager(target).validate()
    except Exception:
        raise LegacyPasswordCredentialImportError(
            "password import target schema is unavailable"
        ) from None
    source_file_sha = _sha256_file(source)
    source_connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro&nofollow=1",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    source_connection.row_factory = sqlite3.Row
    try:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.execute("PRAGMA trusted_schema=OFF")
        columns = {
            str(row[1])
            for row in source_connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if not {
            "id",
            "email",
            "status",
            "password_hash",
            "deleted_at",
        } <= columns:
            raise LegacyPasswordCredentialImportError(
                "legacy password schema is unsupported"
            )
        version_before = int(
            source_connection.execute("PRAGMA data_version").fetchone()[0]
        )
        source_connection.execute("BEGIN")
        rows = source_connection.execute(
            "SELECT id,email,status,password_hash,deleted_at FROM users ORDER BY id"
        ).fetchall()
        source_connection.commit()
        version_after = int(
            source_connection.execute("PRAGMA data_version").fetchone()[0]
        )
    except sqlite3.Error:
        if source_connection.in_transaction:
            source_connection.rollback()
        raise LegacyPasswordCredentialImportError(
            "legacy password snapshot could not be read"
        ) from None
    finally:
        source_connection.close()
    if version_before != version_after or source_file_sha != _sha256_file(source):
        raise LegacyPasswordCredentialImportError(
            "legacy password source changed during inventory"
        )

    snapshot = hashlib.sha256(b"ecorex-v0292-password-snapshot-v1\n")
    eligible: list[tuple[str, str, str, str]] = []
    deleted = unsupported = invalid = 0
    for row in rows:
        material = dict(row)
        snapshot.update(_canonical(material))
        snapshot.update(b"\n")
        if row["deleted_at"] is not None:
            deleted += 1
            continue
        if str(row["status"] or "").casefold() not in {
            "active",
            "suspended",
            "disabled",
        }:
            unsupported += 1
            continue
        account_id = str(row["id"] or "")
        email = str(row["email"] or "").strip().casefold()
        encoded = str(row["password_hash"] or "")
        if not account_id or not email or not encoded:
            unsupported += 1
            continue
        try:
            validate_encoded_password(encoded)
        except PasswordCredentialError:
            invalid += 1
            continue
        record_sha = hashlib.sha256(
            b"ecorex-v0292-password-record-v1\0" + _canonical(material)
        ).hexdigest()
        eligible.append((account_id, email, encoded, record_sha))

    snapshot_sha = snapshot.hexdigest()
    receipt_sha = hashlib.sha256(
        b"ecorex-v0292-password-import-v1\n"
        + _canonical(
            {
                "source_snapshot_sha256": snapshot_sha,
                "records": [record[3] for record in eligible],
            }
        )
    ).hexdigest()
    if dry_run:
        return LegacyPasswordCredentialImportReport(
            schema_version=1,
            source_version="0.2.9.2",
            dry_run=True,
            source_file_sha256=source_file_sha,
            source_snapshot_sha256=snapshot_sha,
            eligible_credentials=len(eligible),
            imported=0,
            replayed=0,
            skipped_deleted=deleted,
            skipped_unsupported=unsupported,
            skipped_invalid=invalid,
            skipped_missing_target=0,
            skipped_admin_reset=0,
            import_receipt_sha256=receipt_sha,
        )
    return _commit(
        target,
        eligible,
        source_file_sha=source_file_sha,
        snapshot_sha=snapshot_sha,
        receipt_sha=receipt_sha,
        deleted=deleted,
        unsupported=unsupported,
        invalid=invalid,
    )


def _commit(
    target: Path,
    records: list[tuple[str, str, str, str]],
    *,
    source_file_sha: str,
    snapshot_sha: str,
    receipt_sha: str,
    deleted: int,
    unsupported: int,
    invalid: int,
) -> LegacyPasswordCredentialImportReport:
    imported = replayed = missing = admin_reset = 0
    connection = sqlite3.connect(target, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).isoformat()
        for source_account, email, encoded, record_sha in records:
            target_user = connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?",
                (source_account,),
            ).fetchone()
            if target_user is None:
                matches = connection.execute(
                    "SELECT * FROM admin_ops_users WHERE email=? ORDER BY account_id",
                    (email,),
                ).fetchall()
                target_user = matches[0] if len(matches) == 1 else None
            if target_user is None:
                missing += 1
                continue
            account_id = str(target_user["account_id"])
            existing = connection.execute(
                "SELECT * FROM admin_ops_password_credentials WHERE account_id=? "
                "OR source_record_sha256=?",
                (account_id, record_sha),
            ).fetchall()
            if existing:
                if len(existing) == 1 and (
                    str(existing[0]["account_id"]) == account_id
                    and str(existing[0]["encoded_hash"]) == encoded
                    and str(existing[0]["source_record_sha256"] or "") == record_sha
                ):
                    replayed += 1
                    continue
                if len(existing) == 1 and existing[0]["source_version"] == "admin":
                    admin_reset += 1
                    continue
                raise LegacyPasswordCredentialImportError(
                    "legacy password import identity changed"
                )
            connection.execute(
                "INSERT INTO admin_ops_password_credentials("
                "account_id,algorithm,encoded_hash,credential_version,source_version,"
                "source_record_sha256,password_changed_at,updated_at"
                ") VALUES(?,'pbkdf2_sha256',?,1,'0.2.9.2',?,?,?)",
                (account_id, encoded, record_sha, now, now),
            )
            imported += 1
        if imported:
            _append_audit(
                connection,
                target_id=receipt_sha,
                payload={
                    "source_snapshot_sha256": snapshot_sha,
                    "imported": imported,
                    "replayed": replayed,
                    "skipped_missing_target": missing,
                    "skipped_admin_reset": admin_reset,
                },
                created_at=now,
            )
        connection.commit()
    except LegacyPasswordCredentialImportError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise LegacyPasswordCredentialImportError(
            "legacy password import could not be committed"
        ) from None
    finally:
        connection.close()
    return LegacyPasswordCredentialImportReport(
        schema_version=1,
        source_version="0.2.9.2",
        dry_run=False,
        source_file_sha256=source_file_sha,
        source_snapshot_sha256=snapshot_sha,
        eligible_credentials=len(records),
        imported=imported,
        replayed=replayed,
        skipped_deleted=deleted,
        skipped_unsupported=unsupported,
        skipped_invalid=invalid,
        skipped_missing_target=missing,
        skipped_admin_reset=admin_reset,
        import_receipt_sha256=receipt_sha,
    )


def _append_audit(
    connection: sqlite3.Connection,
    *,
    target_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    row = connection.execute(
        "SELECT sequence,entry_digest FROM admin_ops_audit ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = (int(row["sequence"]) if row is not None else 0) + 1
    previous = str(row["entry_digest"]) if row is not None else "0" * 64
    payload_sha = hashlib.sha256(_canonical(payload)).hexdigest()
    material = {
        "sequence": sequence,
        "actor_subject": "migration:v0.2.9.2-passwords",
        "action": "legacy.password_credentials.imported",
        "target_id": target_id,
        "payload_sha256": payload_sha,
        "previous_digest": previous,
        "created_at": created_at,
    }
    entry = hashlib.sha256(_canonical(material)).hexdigest()
    connection.execute(
        "INSERT INTO admin_ops_audit("
        "sequence,actor_subject,action,target_id,payload_sha256,previous_digest,"
        "entry_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            sequence,
            material["actor_subject"],
            material["action"],
            target_id,
            payload_sha,
            previous,
            entry,
            created_at,
        ),
    )


def _regular_file(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise LegacyPasswordCredentialImportError(
            "legacy password source is unavailable"
        )
    path = candidate.resolve()
    try:
        metadata = path.lstat()
    except OSError:
        raise LegacyPasswordCredentialImportError(
            "legacy password source is unavailable"
        ) from None
    if path.is_symlink() or not path.is_file() or metadata.st_size > 8 * 1024**3:
        raise LegacyPasswordCredentialImportError(
            "legacy password source is unavailable"
        )
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "LegacyPasswordCredentialImportError",
    "LegacyPasswordCredentialImportReport",
    "import_v0292_password_credentials",
]
