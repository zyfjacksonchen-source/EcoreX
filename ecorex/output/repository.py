"""SQLite facts for output policy snapshots and exported-file receipts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from ecorex.runtime.database import SQLiteDatabase

from .errors import OutputPolicyNotFound
from .models import (
    MaterializationProjection,
    MaterializationStatus,
    OutputAuditProjection,
    OutputLocationAlias,
    OutputPolicyProjection,
    OutputPreferenceProjection,
)


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    projection: OutputPolicyProjection
    root_path: str
    root_device: int
    root_inode: int
    root_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredMaterialization:
    projection: MaterializationProjection
    account_id: str
    display_name_key: str
    attempt_count: int


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class OutputRepository:
    """Short, process-safe transactions; filesystem I/O never holds SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self.database = SQLiteDatabase(database_path)
        self.database_path = self.database.path

    def connect(self) -> sqlite3.Connection:
        return self.database.connect()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.database.transaction() as connection:
            yield connection

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        with self.database.reader() as connection:
            yield connection

    @staticmethod
    def preference_from_row(row: sqlite3.Row) -> OutputPreferenceProjection:
        return OutputPreferenceProjection(
            account_id=str(row["account_id"]),
            location_alias=OutputLocationAlias(row["location_alias"]),
            revision=int(row["revision"]),
            output_policy_snapshot_id=str(row["output_policy_snapshot_id"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def policy_from_row(row: sqlite3.Row) -> StoredPolicy:
        return StoredPolicy(
            projection=OutputPolicyProjection(
                output_policy_snapshot_id=str(row["output_policy_snapshot_id"]),
                account_id=str(row["account_id"]),
                preference_revision=int(row["preference_revision"]),
                location_alias=OutputLocationAlias(row["location_alias"]),
                created_at=str(row["created_at"]),
            ),
            root_path=str(row["root_path"]),
            root_device=int(row["root_device"]),
            root_inode=int(row["root_inode"]),
            root_fingerprint=str(row["root_fingerprint"]),
        )

    @staticmethod
    def materialization_from_row(row: sqlite3.Row) -> StoredMaterialization:
        return StoredMaterialization(
            projection=MaterializationProjection(
                materialization_id=str(row["materialization_id"]),
                artifact_id=str(row["artifact_id"]),
                revision_id=str(row["revision_id"]),
                output_policy_snapshot_id=str(row["output_policy_snapshot_id"]),
                location_alias=OutputLocationAlias(row["location_alias"]),
                display_name=str(row["display_name"]),
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
                status=MaterializationStatus(row["status"]),
                reused_existing=bool(row["reused_existing"]),
                created_at=str(row["created_at"]),
                completed_at=(str(row["completed_at"]) if row["completed_at"] else None),
            ),
            account_id=str(row["account_id"]),
            display_name_key=str(row["display_name_key"]),
            attempt_count=int(row["attempt_count"]),
        )

    def get_preference(self, account_id: str) -> OutputPreferenceProjection | None:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM output_preferences WHERE account_id = ?", (account_id,)
            ).fetchone()
        return None if row is None else self.preference_from_row(row)

    def get_policy(self, snapshot_id: str, *, account_id: str | None = None) -> StoredPolicy:
        with self.reader() as connection:
            query = "SELECT * FROM output_policy_snapshots WHERE output_policy_snapshot_id = ?"
            parameters: list[Any] = [snapshot_id]
            if account_id is not None:
                query += " AND account_id = ?"
                parameters.append(account_id)
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise OutputPolicyNotFound("the selected output policy is unavailable")
        return self.policy_from_row(row)

    def latest_policy_for_alias(
        self,
        account_id: str,
        alias: OutputLocationAlias,
    ) -> StoredPolicy | None:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM output_policy_snapshots "
                "WHERE account_id = ? AND location_alias = ? "
                "ORDER BY preference_revision DESC LIMIT 1",
                (account_id, alias.value),
            ).fetchone()
        return None if row is None else self.policy_from_row(row)

    def get_materialization(self, materialization_id: str) -> StoredMaterialization | None:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT * FROM output_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        return None if row is None else self.materialization_from_row(row)

    def list_audit(self, *, account_id: str, limit: int = 200) -> tuple[OutputAuditProjection, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("audit limit must be between 1 and 1000")
        with self.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM output_audit WHERE account_id = ? "
                "ORDER BY audit_order DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return tuple(
            OutputAuditProjection(
                audit_id=str(row["audit_id"]),
                account_id=str(row["account_id"]),
                action=str(row["action"]),
                subject_id=str(row["subject_id"]),
                details=json.loads(row["details_json"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )


__all__ = [
    "OutputRepository",
    "StoredMaterialization",
    "StoredPolicy",
    "canonical_json",
]
