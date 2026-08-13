"""Copy-on-write recovery of the released ECoreX Runtime conversation graph."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.invariants import RuntimeInvariantAuditor
from ecorex.runtime.storage_migrations import (
    StorageMigrationIdentity,
    apply_live_storage_migration,
    current_storage_schema_sha256,
    dry_run_storage_migration,
    product_storage_migration_manifest,
)

from .errors import MigrationError
from .legacy import snapshot_sqlite
from .path_security import secure_directory, secure_regular_file, stable_sha256_file


RECEIPT_RELATIVE_PATH = Path("migration/ecorex-history-v1.json")
_DATABASE_RELATIVE_PATH = Path("state/runtime.sqlite3")
_SNAPSHOT_TABLES = frozenset(
    {"runtime_snapshots", "capability_snapshots", "extension_catalog_snapshots"}
)


class ECoreXHistoryMigrationError(MigrationError):
    """The old Runtime graph could not be restored without risking current data."""


@dataclass(frozen=True, slots=True)
class ECoreXHistoryMigrationResult:
    source_found: bool
    migrated: bool
    imported: Mapping[str, int]
    reused: Mapping[str, int]


def default_ecorex_data_roots(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> tuple[Path, ...]:
    """Return packaged ECoreX roots, in native user-data precedence order."""

    user_home = Path.home() if home is None else Path(home)
    values = os.environ if environment is None else environment
    system = sys.platform if platform is None else platform
    if system == "darwin":
        return (user_home / "Library" / "Application Support" / "ECoreX",)
    if system.startswith("win"):
        candidates = []
        for key in ("APPDATA", "LOCALAPPDATA"):
            raw = values.get(key)
            if raw:
                candidates.append(Path(raw) / "ECoreX")
        candidates.append(user_home / "AppData" / "Roaming" / "ECoreX")
        return tuple(dict.fromkeys(candidates))
    return ()


def restore_ecorex_history(
    target_root: str | os.PathLike[str],
    *,
    source_roots: Sequence[Path] | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> ECoreXHistoryMigrationResult:
    """Late-merge old same-generation history without opening the old DB."""

    hook = fault_hook or (lambda _phase: None)
    target = Path(target_root).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    try:
        target = secure_directory(target, label="e-Mate data root")
    except MigrationError as error:
        raise ECoreXHistoryMigrationError("e-Mate data root is unsafe") from error
    roots = tuple(
        default_ecorex_data_roots(
            home=home, environment=environment, platform=platform
        )
        if source_roots is None
        else source_roots
    )
    source_database = _discover_source(target, roots)
    if source_database is None:
        return ECoreXHistoryMigrationResult(False, False, {}, {})

    try:
        source_fingerprint = _source_fingerprint(source_database)
        receipt_path = target / RECEIPT_RELATIVE_PATH
        if _receipt_matches(receipt_path, source_fingerprint):
            return ECoreXHistoryMigrationResult(True, False, {}, {})
        migration_root = receipt_path.parent / "ecorex-history"
        migration_root.mkdir(parents=True, exist_ok=True)
        secure_directory(migration_root, label="ECoreX history migration directory")
        target_database = target / _DATABASE_RELATIVE_PATH
        target_database.parent.mkdir(parents=True, exist_ok=True)
        if not target_database.exists():
            SQLiteDatabase(target_database)
        _checkpoint_target(target_database)

        with tempfile.TemporaryDirectory(
            prefix="emate-ecorex-history-", dir=migration_root
        ) as raw:
            staging_root = Path(raw)
            source_snapshot = staging_root / "source-v1.sqlite3"
            snapshot_sqlite(
                source_database,
                source_snapshot,
                subject="legacy ECoreX Runtime database",
            )
            if _source_fingerprint(source_database) != source_fingerprint:
                raise ECoreXHistoryMigrationError(
                    "legacy ECoreX Runtime database changed during history restore"
                )
            source_current = _upgrade_private_source(source_snapshot, staging_root)
            _reject_unmigrated_artifacts(source_current)
            target_snapshot = staging_root / "target.sqlite3"
            snapshot_sqlite(
                target_database,
                target_snapshot,
                subject="current e-Mate Runtime database",
            )
            imported, reused = _merge_history(source_current, target_snapshot)
            merged = staging_root / "merged.sqlite3"
            snapshot_sqlite(
                target_snapshot,
                merged,
                subject="merged e-Mate Runtime database",
            )
            RuntimeInvariantAuditor(merged).audit().raise_if_invalid()

            backup = migration_root / f"target-before-{source_fingerprint[:16]}.sqlite3"
            if not backup.exists():
                snapshot_sqlite(
                    target_database,
                    backup,
                    subject="pre-restore e-Mate Runtime database",
                )
                os.chmod(backup, 0o600)
            hook("before_publish")
            _remove_empty_sidecars(target_database)
            os.replace(merged, target_database)
            _fsync_directory(target_database.parent)
            hook("after_publish")

        _write_receipt(
            receipt_path,
            {
                "schema_version": 1,
                "source_fingerprint": source_fingerprint,
                "imported": dict(sorted(imported.items())),
                "reused": dict(sorted(reused.items())),
            },
        )
        return ECoreXHistoryMigrationResult(True, bool(sum(imported.values())), imported, reused)
    except ECoreXHistoryMigrationError:
        raise
    except Exception as error:
        raise ECoreXHistoryMigrationError(
            "legacy ECoreX history could not be restored safely"
        ) from error


def _discover_source(target: Path, roots: Sequence[Path]) -> Path | None:
    for root in roots:
        candidate_root = Path(root).expanduser()
        if not candidate_root.is_absolute():
            continue
        candidate = candidate_root / _DATABASE_RELATIVE_PATH
        if candidate.resolve(strict=False) == (target / _DATABASE_RELATIVE_PATH):
            continue
        if not os.path.lexists(candidate):
            continue
        try:
            secure_directory(candidate_root, label="legacy ECoreX data root")
            return secure_regular_file(
                candidate,
                label="legacy ECoreX Runtime database",
                root=candidate_root,
            )
        except MigrationError as error:
            raise ECoreXHistoryMigrationError(
                "legacy ECoreX Runtime database is unsafe"
            ) from error
    return None


def _source_fingerprint(database: Path) -> str:
    records = []
    for label, path in (("database", database), ("wal", Path(str(database) + "-wal"))):
        if os.path.lexists(path):
            digest, identity = stable_sha256_file(path, label=f"legacy ECoreX {label}")
            records.append((label, identity.size, digest))
    return hashlib.sha256(
        json.dumps(records, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _upgrade_private_source(source: Path, root: Path) -> Path:
    manifest = product_storage_migration_manifest()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = StorageMigrationIdentity(
        release_id="ecorex-history-v2",
        build_digest=current_storage_schema_sha256(),
        artifact_id="legacy-runtime-copy",
        artifact_sha256=digest,
    )
    receipts = root / "storage-migration"
    preflight = dry_run_storage_migration(
        source,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    apply_live_storage_migration(
        source,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    current = root / "source-v2.sqlite3"
    snapshot_sqlite(source, current, subject="upgraded private ECoreX Runtime copy")
    return current


def _checkpoint_target(database: Path) -> None:
    """Fold an old WAL into the owned e-Mate DB before staging its replacement."""

    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0:
            raise ECoreXHistoryMigrationError(
                "current e-Mate Runtime database is busy"
            )
    finally:
        connection.close()


def _remove_empty_sidecars(database: Path) -> None:
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ECoreXHistoryMigrationError(
            "current e-Mate Runtime database changed during history restore"
        )
    wal.unlink(missing_ok=True)
    Path(str(database) + "-shm").unlink(missing_ok=True)


def _reject_unmigrated_artifacts(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        for table in ("artifact_entities", "artifact_revisions", "input_attachment_uploads"):
            if connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise ECoreXHistoryMigrationError(
                    "legacy ECoreX attachment history requires a supported artifact migration"
                )
    finally:
        connection.close()


def _merge_history(source_path: Path, target_path: Path) -> tuple[dict[str, int], dict[str, int]]:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    source.row_factory = target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    imported: dict[str, int] = {}
    reused: dict[str, int] = {}
    try:
        thread_ids = {str(row[0]) for row in source.execute("SELECT thread_id FROM threads")}
        if not thread_ids:
            return imported, reused
        rows: dict[str, list[sqlite3.Row]] = {}
        rows["threads"] = _selected(source, "threads", "thread_id", thread_ids)
        for table in (
            "thread_heads", "turns", "turn_input_revisions", "turn_execution_batches",
            "items", "jobs", "interactions", "project_thread_bindings", "events",
        ):
            rows[table] = _selected(source, table, "thread_id", thread_ids)
        job_ids = {str(row["job_id"]) for row in rows["jobs"]}
        rows["job_runtime_contexts"] = _selected(source, "job_runtime_contexts", "job_id", job_ids)
        rows["tool_executions"] = _selected(source, "tool_executions", "job_id", job_ids)
        tool_ids = {str(row["tool_call_id"]) for row in rows["tool_executions"]}
        rows["invocation_admissions"] = _selected(source, "invocation_admissions", "tool_call_id", tool_ids)
        project_ids = {str(row["project_id"]) for row in rows["project_thread_bindings"]}
        rows["projects"] = _selected(source, "projects", "project_id", project_ids)

        runtime_ids: set[str] = set()
        capability_ids: set[str] = set()
        extension_ids: set[str] = set()
        for table in ("turn_execution_batches", "job_runtime_contexts", "events"):
            for row in rows[table]:
                keys = set(row.keys())
                for column in ("config_snapshot_id", "permission_snapshot_id", "model_catalog_snapshot_id"):
                    if column in keys and row[column]:
                        runtime_ids.add(str(row[column]))
                if "capability_snapshot_id" in keys and row["capability_snapshot_id"]:
                    capability_ids.add(str(row["capability_snapshot_id"]))
                if "extension_snapshot_id" in keys and row["extension_snapshot_id"]:
                    extension_ids.add(str(row["extension_snapshot_id"]))
        rows["runtime_snapshots"] = _selected(source, "runtime_snapshots", "snapshot_id", runtime_ids)
        rows["capability_snapshots"] = _selected(source, "capability_snapshots", "snapshot_id", capability_ids)
        rows["extension_catalog_snapshots"] = _selected(source, "extension_catalog_snapshots", "snapshot_id", extension_ids)

        order = (
            "runtime_snapshots", "capability_snapshots", "extension_catalog_snapshots",
            "projects", "threads", "turns", "turn_input_revisions",
            "turn_execution_batches", "items", "jobs", "job_runtime_contexts",
            "tool_executions", "invocation_admissions", "interactions",
            "project_thread_bindings", "events", "thread_heads",
        )
        target.execute("BEGIN IMMEDIATE")
        for table in order:
            imported[table], reused[table] = _copy_rows(source, target, table, rows[table])
        target.commit()
        return imported, reused
    except sqlite3.IntegrityError as error:
        target.rollback()
        raise ECoreXHistoryMigrationError(
            "legacy ECoreX history collides with current e-Mate history"
        ) from error
    except BaseException:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()


def _selected(
    connection: sqlite3.Connection, table: str, column: str, values: set[str]
) -> list[sqlite3.Row]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return list(connection.execute(
        f'SELECT * FROM "{table}" WHERE "{column}" IN ({placeholders})',
        tuple(sorted(values)),
    ))


def _copy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    rows: Sequence[sqlite3.Row],
) -> tuple[int, int]:
    del source
    columns = [str(row[1]) for row in target.execute(f'PRAGMA table_info("{table}")')]
    primary = [
        str(row[1])
        for row in sorted(
            target.execute(f'PRAGMA table_info("{table}")'), key=lambda value: int(value[5])
        )
        if int(row[5]) > 0
    ]
    if not primary:
        raise ECoreXHistoryMigrationError("history table has no durable identity")
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    imported = reused = 0
    for row in rows:
        where = " AND ".join(f'"{column}" = ?' for column in primary)
        key = tuple(row[column] for column in primary)
        existing = target.execute(
            f'SELECT {quoted} FROM "{table}" WHERE {where}', key
        ).fetchone()
        if existing is not None:
            ignored = {"created_at"} if table in _SNAPSHOT_TABLES else set()
            if any(existing[column] != row[column] for column in columns if column not in ignored):
                raise ECoreXHistoryMigrationError(
                    "legacy ECoreX history identity collides with current data"
                )
            reused += 1
            continue
        target.execute(
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
            tuple(row[column] for column in columns),
        )
        imported += 1
    return imported, reused


def _receipt_matches(path: Path, source_fingerprint: str) -> bool:
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "source_fingerprint", "imported", "reused", "receipt_digest"
        }:
            raise ValueError
        receipt_digest = value.pop("receipt_digest")
        expected = hashlib.sha256(
            b"emate-ecorex-history-receipt-v1\0"
            + json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if value.get("schema_version") != 1 or receipt_digest != expected:
            raise ValueError
        return value.get("source_fingerprint") == source_fingerprint
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise ECoreXHistoryMigrationError("ECoreX history migration receipt is invalid") from None
    except ValueError:
        raise ECoreXHistoryMigrationError("ECoreX history migration receipt is invalid") from None


def _write_receipt(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    document = dict(value)
    document["receipt_digest"] = hashlib.sha256(
        b"emate-ecorex-history-receipt-v1\0"
        + json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ECoreXHistoryMigrationError",
    "ECoreXHistoryMigrationResult",
    "RECEIPT_RELATIVE_PATH",
    "default_ecorex_data_roots",
    "restore_ecorex_history",
]
