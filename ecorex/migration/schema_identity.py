"""Stable identities for one imported Runtime data generation.

The one-time v0.3 importer owns an immutable *import* identity even though the
live Runtime schema may later advance through signed storage migrations.  The
identity below is therefore captured at import time and persisted both in the
database and in the product migration completion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Any

from ecorex.runtime.database import SCHEMA_VERSION
from ecorex.runtime.storage_migrations import current_storage_schema_sha256


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_ID = re.compile(r"^mig_[0-9a-f]{26}$")
_GENERATION_ID = re.compile(r"^gen_[0-9a-f]{26}$")
_MAX_SCHEMA_OBJECTS = 8_192
_GENERATION_DOMAIN = b"EcoreX imported data generation v1\0"


class ImportSchemaIdentityError(ValueError):
    """An imported database does not match its immutable schema identity."""


@dataclass(frozen=True, slots=True)
class ImportSchemaIdentity:
    import_layout_version: int
    target_storage_schema_version: int
    target_schema_sha256: str
    data_generation_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.import_layout_version, bool)
            or not isinstance(self.import_layout_version, int)
            or self.import_layout_version < 1
            or isinstance(self.target_storage_schema_version, bool)
            or not isinstance(self.target_storage_schema_version, int)
            or self.target_storage_schema_version < 1
            or _HEX_64.fullmatch(self.target_schema_sha256) is None
            or _GENERATION_ID.fullmatch(self.data_generation_id) is None
        ):
            raise ImportSchemaIdentityError("import schema identity is invalid")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "import_layout_version": self.import_layout_version,
            "target_storage_schema_version": self.target_storage_schema_version,
            "target_schema_sha256": self.target_schema_sha256,
            "data_generation_id": self.data_generation_id,
        }


def data_generation_id(
    *,
    migration_id: str,
    source_inventory_digest: str,
    import_layout_version: int,
    target_storage_schema_version: int,
    target_schema_sha256: str,
) -> str:
    if (
        _MIGRATION_ID.fullmatch(str(migration_id)) is None
        or _HEX_64.fullmatch(str(source_inventory_digest)) is None
        or isinstance(import_layout_version, bool)
        or not isinstance(import_layout_version, int)
        or import_layout_version < 1
        or isinstance(target_storage_schema_version, bool)
        or not isinstance(target_storage_schema_version, int)
        or target_storage_schema_version < 1
        or _HEX_64.fullmatch(str(target_schema_sha256)) is None
    ):
        raise ImportSchemaIdentityError("data generation inputs are invalid")
    payload = {
        "import_layout_version": import_layout_version,
        "migration_id": migration_id,
        "source_inventory_digest": source_inventory_digest,
        "target_schema_sha256": target_schema_sha256,
        "target_storage_schema_version": target_storage_schema_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    generation_digest = hashlib.sha256(_GENERATION_DOMAIN + encoded).hexdigest()
    return f"gen_{generation_digest[:26]}"


def physical_schema_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE type IN ('table','index','trigger') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    if len(rows) > _MAX_SCHEMA_OBJECTS:
        raise ImportSchemaIdentityError("import target schema is too large")
    records: list[dict[str, str]] = []
    for object_type, name, table, sql in rows:
        if (
            object_type not in {"table", "index", "trigger"}
            or not isinstance(name, str)
            or not name
            or not isinstance(table, str)
            or not table
            or not isinstance(sql, str)
            or not sql.strip()
        ):
            raise ImportSchemaIdentityError(
                "import target schema object definition is invalid"
            )
        records.append(
            {
                "type": object_type,
                "name": name,
                "table": table,
                "sql": " ".join(sql.split()),
            }
        )
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_import_schema_identity(
    connection: sqlite3.Connection,
    *,
    migration_id: str,
    source_inventory_digest: str,
    import_layout_version: int,
) -> ImportSchemaIdentity:
    row = connection.execute(
        "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
    ).fetchone()
    try:
        observed_version = int(row[0]) if row is not None else 0
    except (TypeError, ValueError, OverflowError):
        observed_version = 0
    observed_digest = physical_schema_sha256(connection)
    expected_digest = current_storage_schema_sha256()
    if observed_version != SCHEMA_VERSION or observed_digest != expected_digest:
        raise ImportSchemaIdentityError(
            "import target does not match the compiled Runtime schema"
        )
    generation = data_generation_id(
        migration_id=migration_id,
        source_inventory_digest=source_inventory_digest,
        import_layout_version=import_layout_version,
        target_storage_schema_version=observed_version,
        target_schema_sha256=observed_digest,
    )
    return ImportSchemaIdentity(
        import_layout_version=import_layout_version,
        target_storage_schema_version=observed_version,
        target_schema_sha256=observed_digest,
        data_generation_id=generation,
    )


def import_schema_identity_from_mapping(value: Any) -> ImportSchemaIdentity:
    if not isinstance(value, dict):
        raise ImportSchemaIdentityError("import schema identity is invalid")
    try:
        return ImportSchemaIdentity(
            import_layout_version=value["import_layout_version"],
            target_storage_schema_version=value["target_storage_schema_version"],
            target_schema_sha256=value["target_schema_sha256"],
            data_generation_id=value["data_generation_id"],
        )
    except (KeyError, TypeError):
        raise ImportSchemaIdentityError("import schema identity is invalid") from None


__all__ = [
    "ImportSchemaIdentity",
    "ImportSchemaIdentityError",
    "current_import_schema_identity",
    "data_generation_id",
    "import_schema_identity_from_mapping",
    "physical_schema_sha256",
]
