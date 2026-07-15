"""One compiled authority for non-core tables in the local product database."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import re
import sqlite3
from typing import Iterable, Sequence

from .errors import SchemaVersionError


_SAFE_FRAGMENT_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_OBJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                statements.append(statement)
    if pending.strip():
        raise ValueError("schema fragment contains an incomplete statement")
    return tuple(statements)


@dataclass(frozen=True, slots=True)
class SchemaFragment:
    """A compiled, non-optional group of local Runtime schema objects."""

    fragment_id: str
    sql: str
    object_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if _SAFE_FRAGMENT_ID.fullmatch(self.fragment_id) is None:
            raise ValueError("schema fragment ID is invalid")
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("schema fragment SQL is empty")
        if not self.object_names or len(set(self.object_names)) != len(
            self.object_names
        ):
            raise ValueError("schema fragment object inventory is invalid")
        if any(_SAFE_OBJECT_NAME.fullmatch(name) is None for name in self.object_names):
            raise ValueError("schema fragment object name is invalid")
        statements = _statements(self.sql)
        if not statements or any(
            statement.lstrip().split(None, 1)[0].upper() != "CREATE"
            for statement in statements
        ):
            raise ValueError("schema fragment may contain only CREATE statements")


def _fragments() -> tuple[SchemaFragment, ...]:
    # Delayed import avoids a catalog/registry cycle while keeping registration
    # static and reviewable.
    from .schema_fragments import PRODUCT_SCHEMA_FRAGMENTS

    fragments = tuple(PRODUCT_SCHEMA_FRAGMENTS)
    ids = [fragment.fragment_id for fragment in fragments]
    names = [name for fragment in fragments for name in fragment.object_names]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise SchemaVersionError("compiled product schema catalog has duplicate identity")
    return fragments


def _records(
    connection: sqlite3.Connection, object_names: Sequence[str]
) -> tuple[dict[str, str], ...]:
    placeholders = ",".join("?" for _ in object_names)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        f"WHERE name IN ({placeholders}) ORDER BY type, name",
        tuple(sorted(object_names)),
    ).fetchall()
    observed = {str(row[1]) for row in rows}
    missing = set(object_names) - observed
    if missing:
        raise SchemaVersionError(
            "product schema objects are missing: " + ", ".join(sorted(missing))
        )
    records: list[dict[str, str]] = []
    for row in rows:
        sql = row[3]
        if not isinstance(sql, str) or not sql.strip():
            raise SchemaVersionError("product schema object definition is invalid")
        records.append(
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table": str(row[2]),
                "sql": " ".join(sql.split()),
            }
        )
    return tuple(records)


def _digest(records: Iterable[dict[str, str]]) -> str:
    payload = json.dumps(
        list(records),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _execute_fragment(connection: sqlite3.Connection, sql: str) -> None:
    """Execute complete SQLite statements without ``executescript`` autocommit."""

    try:
        statements = _statements(sql)
    except ValueError as error:
        raise SchemaVersionError(str(error)) from error
    for statement in statements:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _compiled_fragment_digests() -> tuple[tuple[str, str], ...]:
    fragments = _fragments()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for fragment in fragments:
            connection.executescript(fragment.sql)
        return tuple(
            (
                fragment.fragment_id,
                _digest(_records(connection, fragment.object_names)),
            )
            for fragment in fragments
        )
    except sqlite3.Error as error:
        raise SchemaVersionError("compiled product schema cannot be materialized") from error
    finally:
        connection.close()


@lru_cache(maxsize=1)
def compiled_product_schema_digest() -> str:
    fragments = _fragments()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for fragment in fragments:
            connection.executescript(fragment.sql)
        records = [
            record
            for fragment in fragments
            for record in _records(connection, fragment.object_names)
        ]
        return _digest(records)
    except sqlite3.Error as error:
        raise SchemaVersionError("compiled product schema cannot be fingerprinted") from error
    finally:
        connection.close()


def bootstrap_product_schema(connection: sqlite3.Connection) -> None:
    """Create every compiled domain object for a brand-new product database."""

    fragments = _fragments()
    if not fragments:
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        for fragment in fragments:
            _execute_fragment(connection, fragment.sql)
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    validate_product_schema(connection)


def validate_product_schema(connection: sqlite3.Connection) -> str:
    """Validate exact compiled domain definitions without executing DDL."""

    fragments = _fragments()
    expected = dict(_compiled_fragment_digests())
    combined: list[dict[str, str]] = []
    for fragment in fragments:
        records = _records(connection, fragment.object_names)
        if _digest(records) != expected[fragment.fragment_id]:
            raise SchemaVersionError(
                f"product schema fragment {fragment.fragment_id} is incompatible"
            )
        combined.extend(records)
    digest = _digest(combined)
    if digest != compiled_product_schema_digest():
        raise SchemaVersionError("product schema catalog fingerprint is incompatible")
    return digest


def product_schema_inventory() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (fragment.fragment_id, fragment.object_names) for fragment in _fragments()
    )


def product_schema_sql() -> str:
    """Return compiled fresh-database DDL in its only accepted order."""

    return "\n".join(fragment.sql.strip() for fragment in _fragments()) + "\n"


__all__ = [
    "SchemaFragment",
    "bootstrap_product_schema",
    "compiled_product_schema_digest",
    "product_schema_inventory",
    "product_schema_sql",
    "validate_product_schema",
]
