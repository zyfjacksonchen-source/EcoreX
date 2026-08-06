"""Credential-gated PostgreSQL/S3 image orchestration integration.

This module is intentionally skipped in the ordinary source suite.  A release
environment opts in with an isolated PostgreSQL database and an S3-compatible
bucket endpoint.  Unlike the in-memory contract tests, this exercises real
row locks, transaction visibility, conditional object writes and ETag fencing
under concurrent workers.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import base64
import os
import threading
import uuid

import pytest

from ecorex.image_orchestrator import postgres_schema as postgres_schema_module
from ecorex.image_orchestrator.cas import ImageContentReference
from ecorex.image_orchestrator.models import (
    ImageJobStatus,
    ImageLeaseLost,
    ImageOperation,
    ImageSubmitRequest,
    ImageUsage,
)
from ecorex.image_orchestrator.postgres_schema import PostgresImageSchemaManager
from ecorex.image_orchestrator.postgres_store import PostgresImageJobStore
from ecorex.image_orchestrator.s3_cas import (
    BotoS3ObjectTransport,
    S3ImageContentStore,
    S3ObjectNotFound,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_REQUIRED_ENVIRONMENT = (
    "ECOREX_TEST_POSTGRES_DSN",
    "ECOREX_TEST_S3_ENDPOINT",
    "ECOREX_TEST_S3_ACCESS_KEY",
    "ECOREX_TEST_S3_SECRET_KEY",
)


def test_postgres_index_catalog_query_uses_server_valid_aliases_and_types() -> None:
    query = postgres_schema_module._CATALOG_INDEXES_SQL
    assert "keys(attnum,opclass,collation_oid,option,ordinality)" in query
    assert "keys.collation_oid" in query
    assert "keys.ordinality::integer" in query
    assert "keys(attnum,opclass,collation,option,ordinality)" not in query


def _integration_ready() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENVIRONMENT)


@pytest.mark.skipif(
    not _integration_ready(),
    reason="requires isolated real PostgreSQL 15+ and S3-compatible storage",
)
def test_real_postgres_s3_concurrency_idempotency_recovery_and_gc() -> None:
    boto3 = pytest.importorskip("boto3")
    psycopg = pytest.importorskip("psycopg")
    botocore_config = pytest.importorskip("botocore.config")
    sql = pytest.importorskip("psycopg.sql")
    rows = pytest.importorskip("psycopg.rows")

    dsn = os.environ["ECOREX_TEST_POSTGRES_DSN"]
    endpoint = os.environ["ECOREX_TEST_S3_ENDPOINT"]
    run_id = uuid.uuid4().hex
    schema = f"ecorex_image_it_{run_id}"
    bucket = f"ecorex-image-it-{run_id[:20]}"
    prefix = f"runs/{run_id}"
    total_jobs = max(32, min(256, int(os.environ.get("ECOREX_TEST_IMAGE_JOBS", "96"))))
    worker_count = max(8, min(48, int(os.environ.get("ECOREX_TEST_IMAGE_WORKERS", "24"))))
    account_count = max(4, min(32, total_jobs // 4))
    node_ids = tuple(
        value.strip()
        for value in os.environ.get("ECOREX_TEST_IMAGE_NODE_IDS", "node-a,node-b").split(",")
        if value.strip()
    )
    assert len(node_ids) == 2 and len(set(node_ids)) == 2

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["ECOREX_TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["ECOREX_TEST_S3_SECRET_KEY"],
        region_name="us-east-1",
        use_ssl=endpoint.lower().startswith("https://"),
        config=botocore_config.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=10,
            max_pool_connections=max(32, worker_count * 2),
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    s3.create_bucket(Bucket=bucket)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    clock_lock = threading.Lock()
    clock_value = datetime.now(UTC)

    def clock() -> datetime:
        with clock_lock:
            return clock_value

    def connection_factory():
        return psycopg.connect(
            dsn,
            row_factory=rows.dict_row,
            options=f"-csearch_path={schema}",
        )

    store: PostgresImageJobStore | None = None
    try:
        manager = PostgresImageSchemaManager(
            "postgresql://isolated-image-integration",
            connection_factory=connection_factory,
        )
        receipt = manager.migrate()
        assert manager.validate() == receipt
        store = PostgresImageJobStore(
            "postgresql://isolated-image-integration",
            connection_factory=connection_factory,
            clock=clock,
        )
        cas = S3ImageContentStore(
            BotoS3ObjectTransport(s3),
            bucket=bucket,
            prefix=prefix,
        )

        requests = tuple(
            ImageSubmitRequest(
                operation=ImageOperation.GENERATE,
                model_id="image-2",
                client_request_id=f"it-request-{index:04d}",
                prompt=f"integration image {index}",
            )
            for index in range(total_jobs)
        )

        # Advisory locking must collapse a concurrent retry storm into exactly
        # one durable job without an intermediate placeholder row.
        def duplicate_submit(_index: int):
            assert store is not None
            return store.submit("acct-duplicate", requests[0])

        with ThreadPoolExecutor(max_workers=16) as pool:
            duplicates = list(pool.map(duplicate_submit, range(16)))
        assert len({job.job_id for job, _created in duplicates}) == 1
        assert sum(1 for _job, created in duplicates if created) == 1

        def submit(index: int):
            assert store is not None
            if index == 0:
                return duplicates[0][0]
            account = f"acct-{index % account_count:02d}"
            job, created = store.submit(account, requests[index])
            assert created
            return job

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            submitted = list(pool.map(submit, range(total_jobs)))
        assert len({job.job_id for job in submitted}) == total_jobs

        completed_ids: set[str] = set()
        active_nodes: set[str] = set()
        completed_lock = threading.Lock()
        worker_barrier = threading.Barrier(worker_count)

        def worker(index: int) -> None:
            assert store is not None
            node_id = node_ids[index % len(node_ids)]
            worker_barrier.wait(timeout=30)
            while True:
                leased = store.lease_next(
                    f"{node_id}-worker-{index:02d}", lease_seconds=30
                )
                if leased is None:
                    return
                with completed_lock:
                    active_nodes.add(node_id)
                token = leased.lease_token
                assert token is not None
                store.transition(
                    leased.job_id,
                    token,
                    expected=(ImageJobStatus.LEASED.value,),
                    target=ImageJobStatus.RUNNING.value,
                    checkpoint={"provider_started": True},
                    provider_request_id=f"provider/{leased.job_id}",
                )
                result = cas.put(
                    _PNG,
                    mime_type="image/png",
                    reference=ImageContentReference("job-result", leased.job_id),
                )
                store.transition(
                    leased.job_id,
                    token,
                    expected=(ImageJobStatus.RUNNING.value,),
                    target=ImageJobStatus.VERIFYING.value,
                    checkpoint={"result_sha256": result.sha256},
                )
                store.transition(
                    leased.job_id,
                    token,
                    expected=(ImageJobStatus.VERIFYING.value,),
                    target=ImageJobStatus.COMMITTING.value,
                    checkpoint={"result_sha256": result.sha256},
                )
                finished = store.complete(
                    leased.job_id,
                    token,
                    result=result,
                    usage=ImageUsage("integration-provider", "image-2", billed_units=1),
                )
                assert finished.status is ImageJobStatus.COMPLETED
                with completed_lock:
                    assert finished.job_id not in completed_ids
                    completed_ids.add(finished.job_id)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(worker, range(worker_count)))
        assert completed_ids == {job.job_id for job in submitted}
        assert active_nodes == set(node_ids)
        metadata = cas.describe(cas.put(_PNG, mime_type="image/png").sha256)
        assert len(metadata.references) == total_jobs
        assert cas.read(metadata.result.sha256) == _PNG
        metrics = store.metrics()
        assert metrics.completed == total_jobs
        assert metrics.active == 0
        assert metrics.queued == 0

        # Simulate a worker crash without sleeping.  The database clock moves
        # beyond the lease; recovery must fence the stale token and make the
        # same job safely leasable by a new worker.
        crash_request = ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="image-2",
            client_request_id="it-crash-recovery-request",
            prompt="crash recovery image",
        )
        crashed, created = store.submit("acct-crash", crash_request)
        assert created
        first_lease = store.lease_next("worker-crash", lease_seconds=5)
        assert first_lease is not None and first_lease.job_id == crashed.job_id
        stale_token = first_lease.lease_token
        assert stale_token is not None
        store.transition(
            crashed.job_id,
            stale_token,
            expected=(ImageJobStatus.LEASED.value,),
            target=ImageJobStatus.RUNNING.value,
            checkpoint={"provider_started": True},
        )
        with clock_lock:
            clock_value += timedelta(seconds=6)
        assert store.reclaim_expired(account_id="acct-crash") == 1
        with pytest.raises(ImageLeaseLost):
            store.heartbeat(crashed.job_id, stale_token)
        second_lease = store.lease_next("worker-recovery", lease_seconds=30)
        assert second_lease is not None and second_lease.job_id == crashed.job_id
        assert second_lease.attempt == 2
        assert second_lease.checkpoint.get("provider_uncertain") is True
        recovery_token = second_lease.lease_token
        assert recovery_token is not None
        store.transition(
            crashed.job_id,
            recovery_token,
            expected=(ImageJobStatus.LEASED.value,),
            target=ImageJobStatus.RUNNING.value,
            checkpoint={"provider_reconciled": True},
        )
        recovery_result = cas.put(
            _PNG,
            mime_type="image/png",
            reference=ImageContentReference("job-result", crashed.job_id),
        )
        store.transition(
            crashed.job_id,
            recovery_token,
            expected=(ImageJobStatus.RUNNING.value,),
            target=ImageJobStatus.VERIFYING.value,
            checkpoint={"result_sha256": recovery_result.sha256},
        )
        store.transition(
            crashed.job_id,
            recovery_token,
            expected=(ImageJobStatus.VERIFYING.value,),
            target=ImageJobStatus.COMMITTING.value,
            checkpoint={"result_sha256": recovery_result.sha256},
        )
        recovered = store.complete(
            crashed.job_id,
            recovery_token,
            result=recovery_result,
            usage=ImageUsage("integration-provider", "image-2", billed_units=1),
        )
        assert recovered.status is ImageJobStatus.COMPLETED

        # A separate unowned object exercises real conditional tombstone + GC
        # without deleting the shared result still owned by completed jobs.
        gc_payload = _PNG + b"ecorex-gc-probe"
        gc_result = cas.put(gc_payload, mime_type="image/png")
        gc_projection = cas.describe(gc_result.sha256)
        assert cas.delete_if_unreferenced(
            gc_result.sha256,
            expected_reference_version=gc_projection.reference_version,
        )
        with pytest.raises(S3ObjectNotFound):
            cas.read(gc_result.sha256)
    finally:
        if store is not None:
            store.close()
        try:
            response = s3.list_objects_v2(Bucket=bucket)
            for item in response.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=item["Key"])
            s3.delete_bucket(Bucket=bucket)
        finally:
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
