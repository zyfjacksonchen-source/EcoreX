from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Lock
from types import ModuleType
from typing import Any, Mapping
import uuid

import httpx
import pytest

from ecorex.image_orchestrator.api import create_image_orchestration_router
from ecorex.image_orchestrator.cas import (
    ImageContentAddressedStore,
    ImageContentReference,
    ImageContentStore,
    validate_image_payload,
)
from ecorex.image_orchestrator.models import ImageResultRejected
from ecorex.image_orchestrator.postgres_schema import (
    CURRENT_IMAGE_SCHEMA_VERSION,
    EMPTY_IMAGE_SCHEMA_CATALOG,
    EMPTY_IMAGE_SCHEMA_SHA256,
    EXPECTED_IMAGE_SCHEMA_CATALOG,
    EXPECTED_IMAGE_SCHEMA_COLUMNS,
    EXPECTED_IMAGE_SCHEMA_INDEX_COLUMNS,
    EXPECTED_IMAGE_SCHEMA_TRIGGERS,
    IMAGE_SCHEMA_MIGRATIONS,
    ImageSchemaError,
    POSTGRES_IMAGE_SCHEMA_SHA256,
    PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG,
    PRE_AUTHORITY_IMAGE_SCHEMA_SHA256,
    PRE_AUTHORITY_MIGRATION_CHECKSUM,
    PRE_AUTHORITY_MIGRATION_NAME,
    PostgresImageSchemaCatalog,
    PostgresImageSchemaManager,
    PostgresImageSchemaReceipt,
    main as postgres_schema_main,
    migrate_postgres_image_database,
    validate_postgres_image_database,
)
from ecorex.image_orchestrator.postgres_store import (
    PostgresImageConnectionPool,
    PostgresImageJobStore,
)
from ecorex.image_orchestrator.s3_cas import (
    S3HTTPObjectTransport,
    S3ImageContentStore,
    S3ObjectBody,
    S3ObjectError,
    S3ObjectInfo,
    S3ObjectNotFound,
    S3ObjectPreconditionFailed,
    S3ObjectTransport,
)
from ecorex.image_orchestrator.service import ImageOrchestrationService


PNG = b"\x89PNG\r\n\x1a\n" + b"shared-cas-production-contract"


@dataclass
class _MemoryObject:
    payload: bytes
    content_type: str
    metadata: dict[str, str]
    checksum: str
    etag: str


class MemoryS3Transport:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], _MemoryObject] = {}
        self.lock = Lock()
        self.version = 0
        self.fail_reference_updates = 0
        self.mutate_blob_before_delete = False
        self.fail_reference_deletes = 0

    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str],
        checksum_sha256: str,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        if hashlib.sha256(payload).hexdigest() != checksum_sha256:
            raise S3ObjectError("checksum mismatch")
        identity = (bucket, key)
        with self.lock:
            current = self.objects.get(identity)
            if if_none_match and current is not None:
                raise S3ObjectPreconditionFailed("exists")
            if if_match is not None and (
                current is None or current.etag != if_match
            ):
                raise S3ObjectPreconditionFailed("changed")
            if (
                "/references/" in key
                and if_match is not None
                and self.fail_reference_updates > 0
            ):
                self.fail_reference_updates -= 1
                raise S3ObjectPreconditionFailed("injected contention")
            self.version += 1
            etag = f'"{self.version:032x}"'
            self.objects[identity] = _MemoryObject(
                bytes(payload),
                content_type,
                {str(name).casefold(): str(value) for name, value in metadata.items()},
                checksum_sha256,
                etag,
            )
            return etag

    def head_object(self, *, bucket: str, key: str) -> S3ObjectInfo:
        with self.lock:
            item = self.objects.get((bucket, key))
            if item is None:
                raise S3ObjectNotFound("missing")
            return self._info(item)

    def get_object(
        self, *, bucket: str, key: str, max_bytes: int
    ) -> S3ObjectBody:
        with self.lock:
            item = self.objects.get((bucket, key))
            if item is None:
                raise S3ObjectNotFound("missing")
            if len(item.payload) > max_bytes:
                raise S3ObjectError("bounded")
            return S3ObjectBody(self._info(item), bytes(item.payload))

    def delete_object(
        self, *, bucket: str, key: str, if_match: str | None = None
    ) -> None:
        with self.lock:
            item = self.objects.get((bucket, key))
            if item is None:
                raise S3ObjectNotFound("missing")
            if "/references/" in key and self.fail_reference_deletes > 0:
                self.fail_reference_deletes -= 1
                raise S3ObjectError("injected cleanup failure")
            if self.mutate_blob_before_delete and "/blobs/" in key:
                self.mutate_blob_before_delete = False
                self.version += 1
                item.etag = f'"{self.version:032x}"'
            if if_match is not None and item.etag != if_match:
                raise S3ObjectPreconditionFailed("changed")
            del self.objects[(bucket, key)]

    @staticmethod
    def _info(item: _MemoryObject) -> S3ObjectInfo:
        return S3ObjectInfo(
            item.etag,
            len(item.payload),
            item.content_type,
            dict(item.metadata),
            item.checksum,
        )


def test_file_and_s3_implement_shared_cas_contract(tmp_path: Path) -> None:
    file_store = ImageContentStore(tmp_path / "file-cas")
    s3_store = S3ImageContentStore(
        MemoryS3Transport(),
        bucket="ecorex-images",
    )
    assert isinstance(file_store, ImageContentAddressedStore)
    assert isinstance(s3_store, ImageContentAddressedStore)


def test_cloud_job_store_cannot_be_composed_with_process_local_cas(
    tmp_path: Path,
) -> None:
    # Construction of a real Postgres store validates a live schema.  This
    # uninitialized instance is used only to exercise the structural
    # composition guard without claiming a real database integration.
    cloud_jobs = object.__new__(PostgresImageJobStore)
    service = ImageOrchestrationService(cloud_jobs)

    def principal() -> object:
        return object()

    with pytest.raises(ValueError, match="shared content-addressed"):
        create_image_orchestration_router(
            service,
            principal_dependency=principal,
            content_store=ImageContentStore(tmp_path / "local-cas"),
        )
    router = create_image_orchestration_router(
        service,
        principal_dependency=principal,
        content_store=S3ImageContentStore(
            MemoryS3Transport(),
            bucket="ecorex-images",
        ),
    )
    assert router.routes


def test_avif_magic_requires_an_avif_brand_and_s3_prefixes_cannot_traverse() -> None:
    generic_heif = b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00heic"
    with pytest.raises(ImageResultRejected, match="signature"):
        validate_image_payload(
            generic_heif,
            mime_type="image/avif",
            max_bytes=1024,
        )
    actual_avif = b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00avif"
    assert validate_image_payload(
        actual_avif,
        mime_type="image/avif",
        max_bytes=1024,
    ).mime_type == "image/avif"
    with pytest.raises(ValueError, match="prefix"):
        S3ImageContentStore(
            MemoryS3Transport(),
            bucket="ecorex-images",
            prefix="tenant/../shared",
        )


def test_s3_cas_concurrent_conditional_puts_keep_every_reference() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(
        transport,
        bucket="ecorex-images",
        metadata_attempts=32,
    )
    digest = hashlib.sha256(PNG).hexdigest()

    def put(index: int) -> str:
        result = store.put(
            PNG,
            mime_type="image/png",
            expected_sha256=digest,
            reference=ImageContentReference("job-result", f"job-{index:04d}"),
        )
        return result.sha256

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(put, range(32)))

    assert set(results) == {digest}
    metadata = store.describe(digest)
    assert len(metadata.references) == 32
    assert {item.reference_id for item in metadata.references} == {
        f"job-{index:04d}" for index in range(32)
    }
    assert store.read(digest) == PNG
    blob_keys = [key for (_bucket, key) in transport.objects if "/blobs/" in key]
    assert len(blob_keys) == 1


def test_s3_cas_stale_delete_is_fenced_by_new_reference() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(transport, bucket="ecorex-images")
    result = store.put(PNG, mime_type="image/png")
    empty = store.describe(result.sha256)

    owner = ImageContentReference("job-result", "job-new-owner")
    store.add_reference(result.sha256, owner)

    assert (
        store.delete_if_unreferenced(
            result.sha256,
            expected_reference_version=empty.reference_version,
        )
        is False
    )
    assert store.read(result.sha256) == PNG
    assert store.describe(result.sha256).references == (owner,)


def test_s3_cas_delete_requires_zero_references_and_exact_version() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(transport, bucket="ecorex-images")
    result = store.put(
        PNG,
        mime_type="image/png",
        reference=ImageContentReference("job-result", "job-delete-owner"),
    )
    owned = store.describe(result.sha256)
    assert not store.delete_if_unreferenced(
        result.sha256,
        expected_reference_version=owned.reference_version,
    )

    empty = store.release_reference(result.sha256, owned.references[0])
    assert store.delete_if_unreferenced(
        result.sha256,
        expected_reference_version=empty.reference_version,
    )
    with pytest.raises(S3ObjectNotFound):
        store.read(result.sha256)


def test_s3_cas_corruption_and_bounded_contention_fail_closed() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(
        transport,
        bucket="ecorex-images",
        metadata_attempts=2,
    )
    result = store.put(PNG, mime_type="image/png")
    reference = ImageContentReference("job-result", "job-contention")
    transport.fail_reference_updates = 2
    with pytest.raises(ImageResultRejected, match="contended"):
        store.add_reference(result.sha256, reference)
    assert store.describe(result.sha256).references == ()
    assert store.read(result.sha256) == PNG

    blob_identity = next(
        identity for identity in transport.objects if "/blobs/" in identity[1]
    )
    transport.objects[blob_identity].payload += b"corrupt"
    with pytest.raises(ImageResultRejected, match="size|integrity"):
        store.read(result.sha256)


def test_s3_cas_revalidates_magic_instead_of_trusting_object_metadata() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(transport, bucket="ecorex-images")
    result = store.put(PNG, mime_type="image/png")
    blob_identity = next(
        identity for identity in transport.objects if "/blobs/" in identity[1]
    )
    # Simulate an operator rewriting every MIME declaration but not the
    # content-addressed bytes. Digest verification alone would accept this.
    blob = transport.objects[blob_identity]
    blob.content_type = "image/jpeg"
    blob.metadata["ecorex-mime"] = "image/jpeg"
    with pytest.raises(ImageResultRejected, match="signature"):
        store.read(result.sha256)


def test_s3_cas_content_etag_race_leaves_a_fail_closed_tombstone() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(transport, bucket="ecorex-images")
    result = store.put(PNG, mime_type="image/png")
    empty = store.describe(result.sha256)
    transport.mutate_blob_before_delete = True

    assert not store.delete_if_unreferenced(
        result.sha256,
        expected_reference_version=empty.reference_version,
    )
    # Bytes were not deleted against a stale ETag, while the tombstone blocks
    # any resurrection until an operator reconciles the unexpected rewrite.
    assert store.read(result.sha256) == PNG
    with pytest.raises(ImageResultRejected, match="being deleted"):
        store.add_reference(
            result.sha256,
            ImageContentReference("job-result", "job-after-tombstone"),
        )


def test_s3_cas_reconciles_a_crash_left_deletion_tombstone() -> None:
    transport = MemoryS3Transport()
    store = S3ImageContentStore(transport, bucket="ecorex-images")
    result = store.put(PNG, mime_type="image/png")
    empty = store.describe(result.sha256)
    transport.fail_reference_deletes = 1

    # Content deletion succeeds; injected metadata cleanup failure leaves the
    # deletion fence behind for a later GC/reconciliation pass.
    assert store.delete_if_unreferenced(
        result.sha256,
        expected_reference_version=empty.reference_version,
    )
    assert any("/references/" in key for _bucket, key in transport.objects)
    assert store.reconcile_deletion(result.sha256)
    assert not any(result.sha256 in key for _bucket, key in transport.objects)


def test_http_transport_streams_with_a_hard_response_bound() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": "1024",
                "content-type": "image/png",
                "etag": '"0123456789abcdef"',
            },
            content=b"x" * 1024,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = S3HTTPObjectTransport(
        "https://objects.invalid",
        client=client,
    )
    with pytest.raises(S3ObjectError, match="read bound"):
        transport.get_object(
            bucket="ecorex-images",
            key="bounded/object",
            max_bytes=64,
        )
    client.close()


class _Cursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = list(rows or [])

    def fetchone(self) -> Any:
        if not self.rows:
            return None
        return self.rows[0]

    def fetchall(self) -> list[Any]:
        return list(self.rows)


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: _SchemaConnection) -> None:
        self.connection = connection
        self.before: dict[str, Any] | None = None

    def __enter__(self) -> None:
        self.before = deepcopy(vars(self.connection.state))
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is not None and self.before is not None:
            vars(self.connection.state).clear()
            vars(self.connection.state).update(self.before)
            self.connection.state.rolled_back = True
        return False


@dataclass
class _SchemaState:
    catalog: PostgresImageSchemaCatalog
    calls: list[str]
    history: list[tuple[Any, ...]]
    legacy_history: list[tuple[Any, ...]]
    fail_migration: bool = False
    rolled_back: bool = False
    server_version: int = 150000
    read_only_seen: bool = False
    core_applied: bool = False
    function_source: str = (
        "BEGIN RAISE EXCEPTION 'image ledger rows are immutable'; END;"
    )


class _SchemaConnection:
    def __init__(self, state: _SchemaState) -> None:
        self.state = state

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def close(self) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        normalized = " ".join(sql.split())
        self.state.calls.append(normalized)
        if normalized == "SET TRANSACTION READ ONLY":
            self.state.read_only_seen = True
            return _Cursor()
        if normalized == "SHOW server_version_num":
            return _Cursor([(self.state.server_version,)])
        if normalized == "SELECT current_schema() AS schema_name":
            return _Cursor([("public",)])
        marker = "/* ecorex:image-schema:"
        if marker in normalized:
            dimension = normalized.split(marker, 1)[1].split(" */", 1)[0]
            if dimension in self.state.catalog.to_dict():
                records = list(getattr(self.state.catalog, dimension))
                if dimension == "functions":
                    records = [
                        (*record[:-1], self.state.function_source) for record in records
                    ]
                return _Cursor(records)
            if dimension == "legacy-history":
                return _Cursor(self.state.legacy_history)
            if dimension == "history":
                return _Cursor(self.state.history)
        if normalized.startswith("INSERT INTO ecorex_image_schema_migrations"):
            self.state.history.append(tuple(params))
            return _Cursor()
        if (
            self.state.fail_migration
            and "CREATE TABLE image_scheduler_control" in normalized
        ):
            raise RuntimeError("injected migration failure")
        if normalized.startswith("CREATE TABLE image_scheduler_control"):
            self.state.core_applied = True
            return _Cursor()
        if normalized.startswith("CREATE TABLE ecorex_image_schema_migrations"):
            self.state.catalog = EXPECTED_IMAGE_SCHEMA_CATALOG
            return _Cursor()
        if normalized.startswith("DROP TABLE ecorex_image_schema_migrations"):
            self.state.catalog = EXPECTED_IMAGE_SCHEMA_CATALOG
            self.state.legacy_history.clear()
            return _Cursor()
        return _Cursor()


def _receipt_row(source_schema_sha256: str) -> tuple[Any, ...]:
    migration = IMAGE_SCHEMA_MIGRATIONS[0]
    receipt = PostgresImageSchemaReceipt(
        schema_version=1,
        migration_version=CURRENT_IMAGE_SCHEMA_VERSION,
        migration_name=migration.name,
        migration_checksum=migration.checksum,
        source_schema_sha256=source_schema_sha256,
        target_schema_sha256=POSTGRES_IMAGE_SCHEMA_SHA256,
        installed_at="2026-07-11T00:00:00+00:00",
    )
    receipt_json = json.dumps(
        receipt.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        receipt.migration_version,
        receipt.migration_name,
        receipt.migration_checksum,
        receipt.source_schema_sha256,
        receipt.target_schema_sha256,
        receipt_json,
        hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
        receipt.installed_at,
    )


def _schema_state(
    *, installed: bool = False, pre_authority: bool = False
) -> _SchemaState:
    catalog = EMPTY_IMAGE_SCHEMA_CATALOG
    history: list[tuple[Any, ...]] = []
    legacy_history: list[tuple[Any, ...]] = []
    if installed:
        catalog = EXPECTED_IMAGE_SCHEMA_CATALOG
        history = [_receipt_row(EMPTY_IMAGE_SCHEMA_SHA256)]
    elif pre_authority:
        catalog = PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG
        legacy_history = [
            (
                CURRENT_IMAGE_SCHEMA_VERSION,
                PRE_AUTHORITY_MIGRATION_NAME,
                PRE_AUTHORITY_MIGRATION_CHECKSUM,
            )
        ]
    return _SchemaState(catalog, [], history, legacy_history)


def test_postgres_runtime_validates_schema_without_executing_ddl() -> None:
    state = _schema_state(installed=True)
    store = PostgresImageJobStore(
        "postgresql://unused",
        connection_factory=lambda: _SchemaConnection(state),
    )
    assert store.schema_version == CURRENT_IMAGE_SCHEMA_VERSION
    assert store.schema_receipt.target_schema_sha256 == POSTGRES_IMAGE_SCHEMA_SHA256
    assert state.read_only_seen is True
    assert not any(
        call.startswith(("CREATE ", "ALTER ", "DROP ", "INSERT "))
        for call in state.calls
    )


def test_postgres_default_pool_is_bounded_and_returns_each_lease_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        marker = "connection"

    class FakePool:
        instance: FakePool | None = None

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.connection = FakeConnection()
            self.returned: list[FakeConnection] = []
            self.closed_with: float | None = None
            FakePool.instance = self

        def getconn(self, *, timeout: float) -> FakeConnection:
            assert timeout == 7.0
            return self.connection

        def putconn(self, connection: FakeConnection) -> None:
            self.returned.append(connection)

        def close(self, *, timeout: float) -> None:
            self.closed_with = timeout

    psycopg = ModuleType("psycopg")
    rows = ModuleType("psycopg.rows")
    rows.dict_row = object()  # type: ignore[attr-defined]
    pool_module = ModuleType("psycopg_pool")
    pool_module.ConnectionPool = FakePool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_module)

    pool = PostgresImageConnectionPool(
        "postgresql://redacted",
        min_size=2,
        max_size=9,
        timeout_seconds=7.0,
    )
    backend = FakePool.instance
    assert backend is not None
    assert backend.kwargs["min_size"] == 2
    assert backend.kwargs["max_size"] == 9
    lease = pool()
    assert lease.marker == "connection"
    lease.close()
    lease.close()
    assert backend.returned == [backend.connection]
    pool.close(timeout_seconds=3.0)
    assert backend.closed_with == 3.0
    with pytest.raises(RuntimeError, match="closed"):
        pool()


def test_postgres_schema_migration_is_explicit_versioned_and_atomic() -> None:
    state = _schema_state()
    manager = PostgresImageSchemaManager(
        "postgresql://unused",
        connection_factory=lambda: _SchemaConnection(state),
    )
    receipt = manager.migrate()
    assert receipt.migration_version == CURRENT_IMAGE_SCHEMA_VERSION
    assert receipt.source_schema_sha256 == EMPTY_IMAGE_SCHEMA_SHA256
    assert receipt.target_schema_sha256 == POSTGRES_IMAGE_SCHEMA_SHA256
    assert state.catalog == EXPECTED_IMAGE_SCHEMA_CATALOG
    assert len(state.history) == 1
    assert any("pg_advisory_xact_lock" in call for call in state.calls)
    assert manager.validate() == receipt

    failed = _schema_state()
    failed.fail_migration = True
    failing_manager = PostgresImageSchemaManager(
        "postgresql://unused",
        connection_factory=lambda: _SchemaConnection(failed),
    )
    with pytest.raises(ImageSchemaError, match="migration failed"):
        failing_manager.migrate()
    assert failed.catalog == EMPTY_IMAGE_SCHEMA_CATALOG
    assert failed.history == []
    assert failed.rolled_back is True


def test_postgres_deployment_api_and_idempotent_migrate_return_same_receipt() -> None:
    state = _schema_state()

    def factory() -> _SchemaConnection:
        return _SchemaConnection(state)

    receipt = migrate_postgres_image_database(
        "postgresql://unused",
        connection_factory=factory,
    )
    state.calls.clear()

    assert validate_postgres_image_database(
        "postgresql://unused",
        connection_factory=factory,
    ) == receipt
    assert PostgresImageSchemaManager(
        "postgresql://unused",
        connection_factory=factory,
    ).migrate() == receipt
    assert any("pg_advisory_xact_lock" in call for call in state.calls)
    assert not any(
        call.startswith(("CREATE ", "ALTER ", "DROP ", "INSERT "))
        for call in state.calls
    )


def test_postgres_deployment_cli_reads_dsn_from_environment_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = _schema_state()
    secret_dsn = "postgresql://operator:secret@database.invalid/ecorex"
    monkeypatch.setenv("TEST_IMAGE_DSN", secret_dsn)

    class Manager:
        def __init__(self, dsn: str) -> None:
            assert dsn == secret_dsn

        def migrate(self) -> PostgresImageSchemaReceipt:
            return PostgresImageSchemaManager(
                "postgresql://unused",
                connection_factory=lambda: _SchemaConnection(state),
            ).migrate()

        def validate(self) -> PostgresImageSchemaReceipt:
            raise AssertionError("wrong command")

    monkeypatch.setattr(
        "ecorex.image_orchestrator.postgres_schema.PostgresImageSchemaManager",
        Manager,
    )
    assert postgres_schema_main(["migrate", "--dsn-env", "TEST_IMAGE_DSN"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["target_schema_sha256"] == POSTGRES_IMAGE_SCHEMA_SHA256
    assert secret_dsn not in output


def test_postgres_migration_adopts_only_the_frozen_pre_authority_shape() -> None:
    state = _schema_state(pre_authority=True)
    manager = PostgresImageSchemaManager(
        "postgresql://unused",
        connection_factory=lambda: _SchemaConnection(state),
    )

    receipt = manager.migrate()
    assert receipt.source_schema_sha256 == PRE_AUTHORITY_IMAGE_SCHEMA_SHA256
    assert receipt.target_schema_sha256 == POSTGRES_IMAGE_SCHEMA_SHA256
    assert state.catalog == EXPECTED_IMAGE_SCHEMA_CATALOG
    assert state.legacy_history == []
    assert manager.validate() == receipt


def test_postgres_unknown_source_fails_before_any_ddl() -> None:
    extra_table = (
        "image_unowned_partial",
        "r",
        "p",
        "d",
        "false",
        "false",
        "false",
    )
    state = _schema_state()
    state.catalog = replace(
        EMPTY_IMAGE_SCHEMA_CATALOG,
        tables=(extra_table,),
    )
    manager = PostgresImageSchemaManager(
        "postgresql://unused",
        connection_factory=lambda: _SchemaConnection(state),
    )

    with pytest.raises(ImageSchemaError, match="source shape is unknown"):
        manager.migrate()
    assert not any(
        call.startswith(("CREATE ", "ALTER ", "DROP ", "INSERT "))
        for call in state.calls
    )


def test_postgres_pre_authority_requires_the_exact_legacy_history() -> None:
    state = _schema_state(pre_authority=True)
    state.legacy_history[0] = (
        CURRENT_IMAGE_SCHEMA_VERSION,
        PRE_AUTHORITY_MIGRATION_NAME,
        "0" * 64,
    )
    with pytest.raises(ImageSchemaError, match="pre-authority history is invalid"):
        PostgresImageSchemaManager(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(state),
        ).migrate()
    assert state.catalog == PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG


def test_postgres_schema_checksum_drift_fails_startup_closed() -> None:
    state = _schema_state(installed=True)
    row = list(state.history[0])
    row[2] = "0" * 64
    state.history[0] = tuple(row)
    with pytest.raises(ImageSchemaError, match="receipt is inconsistent"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(state),
        )
    assert not any("image_scheduler_control" in call for call in state.calls)


def test_postgres_old_new_and_unsupported_server_fail_startup_closed() -> None:
    old = _schema_state()
    with pytest.raises(ImageSchemaError, match="tables fingerprint"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(old),
        )

    newer = _schema_state(installed=True)
    newer.history.append(
        (
            CURRENT_IMAGE_SCHEMA_VERSION + 1,
            "future",
            "f" * 64,
            POSTGRES_IMAGE_SCHEMA_SHA256,
            POSTGRES_IMAGE_SCHEMA_SHA256,
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
            "2026-07-11T00:00:00+00:00",
        )
    )
    with pytest.raises(ImageSchemaError, match="newer"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(newer),
        )

    unsupported = _schema_state(installed=True)
    unsupported.server_version = 140999
    with pytest.raises(ImageSchemaError, match="15 or newer"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(unsupported),
        )


def _replace_catalog_field(
    catalog: PostgresImageSchemaCatalog,
    dimension: str,
    key: str | tuple[str, str],
    field: int,
    value: str,
) -> PostgresImageSchemaCatalog:
    records = list(getattr(catalog, dimension))
    for index, record in enumerate(records):
        matches = (
            record[0] == key
            if isinstance(key, str)
            else record[0] == key[0] and record[2] == key[1]
        )
        if matches:
            changed = list(record)
            changed[field] = value
            records[index] = tuple(changed)
            return replace(catalog, **{dimension: tuple(records)})
    raise AssertionError(f"missing catalog record {dimension}:{key}")


@pytest.mark.parametrize(
    ("dimension", "key", "field", "value"),
    [
        ("tables", "image_jobs", 4, "true"),
        ("columns", ("image_jobs", "weight"), 3, "bigint"),
        ("columns", ("image_jobs", "weight"), 4, "false"),
        ("columns", ("image_jobs", "attempt"), 5, "1"),
        ("columns", ("image_jobs", "weight"), 6, "d"),
        ("columns", ("image_jobs", "weight"), 7, "s"),
        ("columns", ("image_jobs", "job_id"), 8, "pg_catalog.C"),
        ("constraints", "image_jobs_pkey", 4, "account_id"),
        ("constraints", "image_jobs_provider_idempotency_key_key", 4, "job_id"),
        ("constraints", "image_jobs_operation_check", 9, "(operation <> '')"),
        ("constraints", "image_results_job_id_fkey", 3, "image_events"),
        ("indexes", "image_jobs_schedulable", 2, "hash"),
        ("indexes", "image_jobs_schedulable", 3, "true"),
        ("indexes", "image_jobs_schedulable", 4, "true"),
        ("indexes", "image_jobs_schedulable", 8, "pg_catalog.text_pattern_ops"),
        ("indexes", "image_jobs_schedulable", 10, "true"),
        ("indexes", "image_jobs_schedulable", 11, "true"),
        ("triggers", "image_results_immutable", 2, "D"),
        ("triggers", "image_results_immutable", 3, "19"),
        ("triggers", "image_results_immutable", 4, "(job_id IS NOT NULL)"),
        ("triggers", "image_results_immutable", 5, "other_function()"),
        ("triggers", "image_results_immutable", 8, "1"),
        ("triggers", "image_results_immutable", 10, "00"),
        ("functions", "ecorex_image_immutable", 1, "text"),
        ("functions", "ecorex_image_immutable", 2, "void"),
        ("sequences", "image_events_seq_seq", 3, "2"),
    ],
)
def test_postgres_every_physical_catalog_dimension_fails_closed(
    dimension: str,
    key: str | tuple[str, str],
    field: int,
    value: str,
) -> None:
    state = _schema_state(installed=True)
    state.catalog = _replace_catalog_field(
        state.catalog,
        dimension,
        key,
        field,
        value,
    )
    with pytest.raises(ImageSchemaError, match=f"{dimension} fingerprint"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(state),
        )


@pytest.mark.parametrize(
    "dimension",
    ["tables", "columns", "constraints", "indexes", "triggers", "functions", "sequences"],
)
def test_postgres_extra_or_missing_managed_catalog_object_fails_closed(
    dimension: str,
) -> None:
    state = _schema_state(installed=True)
    records = list(getattr(state.catalog, dimension))
    records.append(records[0])
    state.catalog = replace(state.catalog, **{dimension: tuple(records)})
    with pytest.raises(ImageSchemaError, match=f"{dimension} fingerprint"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(state),
        )


def test_postgres_function_body_and_receipt_target_drift_fail_closed() -> None:
    function_drift = _schema_state(installed=True)
    function_drift.function_source = "BEGIN RETURN NEW; END;"
    with pytest.raises(ImageSchemaError, match="functions fingerprint"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(function_drift),
        )

    receipt_drift = _schema_state(installed=True)
    row = list(receipt_drift.history[0])
    payload = json.loads(row[5])
    payload["target_schema_sha256"] = "0" * 64
    row[5] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    row[6] = hashlib.sha256(row[5].encode("utf-8")).hexdigest()
    row[4] = "0" * 64
    receipt_drift.history[0] = tuple(row)
    with pytest.raises(ImageSchemaError, match="receipt is invalid"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=lambda: _SchemaConnection(receipt_drift),
        )


@pytest.mark.skipif(
    not os.environ.get("ECOREX_TEST_POSTGRES_DSN"),
    reason="requires isolated real PostgreSQL 15+ integration environment",
)
def test_real_postgres_image_schema_migrate_validate_and_drift_gate() -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.rows import dict_row

    dsn = os.environ["ECOREX_TEST_POSTGRES_DSN"]
    schema = f"ecorex_image_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connection_factory():
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            options=f"-csearch_path={schema}",
        )

    try:
        manager = PostgresImageSchemaManager(
            "postgresql://isolated-test",
            connection_factory=connection_factory,
        )
        receipt = manager.migrate()
        assert manager.validate() == receipt
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE INDEX image_jobs_unexpected ON {}.image_jobs(status)").format(
                    sql.Identifier(schema)
                )
            )
        with pytest.raises(ImageSchemaError, match="indexes fingerprint"):
            manager.validate()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
