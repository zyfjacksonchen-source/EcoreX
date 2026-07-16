"""Read-only v0.2.9.2 Admin identity/session export.

The released Admin database stores only SHA-256 token commitments. This module
exports those commitments and bounded user identity fields; it never reads
conversation tables or emits plaintext credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_USER_COLUMNS = frozenset(
    {
        "id",
        "name",
        "email",
        "role",
        "status",
        "daily_token_limit",
        "weekly_token_limit",
        "deleted_at",
    }
)
_SESSION_COLUMNS = frozenset(
    {"id", "user_id", "token_hash", "expires_at", "revoked_at"}
)


class LegacyIdentityExportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyIdentityExportReport:
    schema_version: int
    source_version: str
    active_users: int
    eligible_sessions: int
    excluded_deleted_users: int
    excluded_disabled_users: int
    excluded_revoked_sessions: int
    excluded_expired_sessions: int
    records_sha256: str
    as_of: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_version": self.source_version,
            "active_users": self.active_users,
            "eligible_sessions": self.eligible_sessions,
            "excluded_deleted_users": self.excluded_deleted_users,
            "excluded_disabled_users": self.excluded_disabled_users,
            "excluded_revoked_sessions": self.excluded_revoked_sessions,
            "excluded_expired_sessions": self.excluded_expired_sessions,
            "records_sha256": self.records_sha256,
            "as_of": self.as_of,
        }


def export_v0292_legacy_identities(
    database_path: str | os.PathLike[str],
    *,
    as_of: datetime | None = None,
) -> tuple[tuple[dict[str, object], ...], LegacyIdentityExportReport]:
    path = Path(database_path).resolve()
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size > 8 * 1024 * 1024 * 1024
    ):
        raise LegacyIdentityExportError("legacy Admin database is unavailable")
    cutoff = (as_of or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    connection = _connect_read_only(path)
    try:
        connection.execute("BEGIN")
        _validate_schema(connection)
        users = connection.execute(
            "SELECT id,name,email,role,status,daily_token_limit,weekly_token_limit,"
            "deleted_at FROM users ORDER BY id"
        ).fetchall()
        sessions = connection.execute(
            "SELECT id,user_id,token_hash,expires_at,revoked_at "
            "FROM client_sessions ORDER BY id"
        ).fetchall()
        connection.commit()
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise LegacyIdentityExportError(
            "legacy identity snapshot could not be read"
        ) from error
    finally:
        connection.close()

    valid_users: dict[str, sqlite3.Row] = {}
    deleted = 0
    disabled = 0
    for user in users:
        if user["deleted_at"] is not None:
            deleted += 1
            continue
        if str(user["status"]) != "active":
            disabled += 1
            continue
        user_id = str(user["id"])
        if _SAFE_ID.fullmatch(user_id) is None:
            raise LegacyIdentityExportError("legacy user identity is invalid")
        valid_users[user_id] = user

    records: list[dict[str, object]] = []
    revoked = 0
    expired = 0
    for session in sessions:
        user = valid_users.get(str(session["user_id"]))
        if user is None:
            continue
        if session["revoked_at"] is not None:
            revoked += 1
            continue
        expires_at = _time(str(session["expires_at"]))
        if expires_at <= cutoff:
            expired += 1
            continue
        token_hash = str(session["token_hash"] or "").casefold()
        if _SHA256.fullmatch(token_hash) is None:
            raise LegacyIdentityExportError("legacy session commitment is invalid")
        record: dict[str, object] = {
            "account_id": str(user["id"]),
            "credential_sha256": token_hash,
            "display_name": _text(user["name"], 256),
            "email": _email(user["email"]),
            "role": _text(user["role"], 64),
            "daily_token_limit": _nonnegative(user["daily_token_limit"]),
            "weekly_token_limit": _nonnegative(user["weekly_token_limit"]),
            "session_expires_at": _iso(expires_at),
        }
        record["source_record_sha256"] = hashlib.sha256(
            b"ecorex-v0.2.9.2-admin-identity-v1\n" + _canonical(record)
        ).hexdigest()
        records.append(record)
    records.sort(
        key=lambda item: (
            str(item["account_id"]),
            str(item["source_record_sha256"]),
        )
    )
    encoded = b"".join(_canonical(item) + b"\n" for item in records)
    return tuple(records), LegacyIdentityExportReport(
        schema_version=1,
        source_version="0.2.9.2",
        active_users=len(valid_users),
        eligible_sessions=len(records),
        excluded_deleted_users=deleted,
        excluded_disabled_users=disabled,
        excluded_revoked_sessions=revoked,
        excluded_expired_sessions=expired,
        records_sha256=hashlib.sha256(encoded).hexdigest(),
        as_of=_iso(cutoff),
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&nofollow=1",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }
    if not {"users", "client_sessions"} <= tables:
        raise LegacyIdentityExportError("legacy Admin schema is unsupported")
    users = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    sessions = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(client_sessions)").fetchall()
    }
    if not _USER_COLUMNS <= users or not _SESSION_COLUMNS <= sessions:
        raise LegacyIdentityExportError("legacy Admin schema is unsupported")


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LegacyIdentityExportError("legacy session timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise LegacyIdentityExportError("legacy session timestamp is invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _text(value: Any, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise LegacyIdentityExportError("legacy identity text is invalid")
    return normalized


def _email(value: Any) -> str:
    email = _text(value, 254).casefold()
    if email.count("@") != 1 or any(character.isspace() for character in email):
        raise LegacyIdentityExportError("legacy identity email is invalid")
    return email


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LegacyIdentityExportError("legacy identity quota is invalid")
    return value


__all__ = [
    "LegacyIdentityExportError",
    "LegacyIdentityExportReport",
    "export_v0292_legacy_identities",
]
