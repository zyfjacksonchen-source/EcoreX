from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ecorex.control_plane.management import (
    AdminManagementConflict,
    AdminManagementRepository,
)
from ecorex.control_plane.management_models import CreateAdminUserRequest
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.image_orchestrator.models import ImageJobStatus, ImageUsage
from ecorex.image_orchestrator.production import _AdminManagementImageUsageProvider
from ecorex.image_orchestrator.provider import ProviderResult, ProviderState


class _CompletedProvider:
    provider_id = "managed-image"

    def __init__(self, model_id: str = "gpt-image-2") -> None:
        self.model_id = model_id

    async def submit(self, job, *, idempotency_key):
        return self._result()

    async def recover(self, job, *, idempotency_key, provider_request_id):
        return self._result()

    async def cancel(self, job, *, idempotency_key, provider_request_id):
        return None

    async def health(self):
        return None

    async def aclose(self):
        return None

    def _result(self) -> ProviderResult:
        return ProviderResult(
            ProviderState.COMPLETED,
            payload=b"image",
            mime_type="image/png",
            sha256="0" * 64,
            usage=ImageUsage(
                "managed-image", self.model_id, output_units=1, billed_units=1
            ),
        )


def test_admin_image_usage_settlement_is_exactly_once_across_recovery(tmp_path) -> None:
    database = tmp_path / "management.db"
    AdminManagementSchemaManager(database).migrate()
    repository = AdminManagementRepository(database, encryption_key=b"m" * 32)
    actor = ControlPrincipal(
        subject="administrator",
        client_id="admin-web",
        account_id="admin",
        roles=frozenset({"platform_admin"}),
    )
    repository.create_user(
        CreateAdminUserRequest(
            account_id="account-1",
            display_name="Image User",
            email="image@example.com",
            organization_id="org-1",
            token_limit=0,
            image_limit=10,
            password="ordinary-user-password-1",
            client_request_id="create-image-user",
        ),
        actor=actor,
    )
    provider = _AdminManagementImageUsageProvider(
        _CompletedProvider("gpt-image-2-pro"), repository  # type: ignore[arg-type]
    )
    job = SimpleNamespace(
        job_id="imgjob_" + "1" * 32,
        account_id="account-1",
        request=SimpleNamespace(count=1, model_id="gpt-image-2"),
        status=ImageJobStatus.RUNNING,
        created_at=datetime.now(UTC),
    )

    async def scenario() -> None:
        await provider.submit(job, idempotency_key="image-provider-request-1")
        await provider.recover(
            job,
            idempotency_key="image-provider-request-1",
            provider_request_id="provider-request-1",
        )

    asyncio.run(scenario())
    assert repository.get_user("account-1").images_used == 1
    connection = repository._connect()
    try:
        row = connection.execute(
            "SELECT COUNT(*),organization_id,requested_model_id,"
            "provider_reported_model_id,actual_model_id,"
            "actual_provider_id,fallback_from_model_id,fallback_used,job_status,result_status "
            "FROM admin_ops_provider_usage_facts "
            "WHERE source_service='image_service' AND source_id=?",
            (job.job_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (
        1,
        "org-1",
        "gpt-image-2",
        "gpt-image-2-pro",
        None,
        "managed-image",
        None,
        None,
        "running",
        "completed",
    )


def test_admin_image_usage_records_reported_model_and_one_fact_per_batch_job(tmp_path) -> None:
    database = tmp_path / "management.db"
    AdminManagementSchemaManager(database).migrate()
    repository = AdminManagementRepository(database, encryption_key=b"m" * 32)
    actor = ControlPrincipal(
        subject="administrator",
        client_id="admin-web",
        account_id="admin",
        roles=frozenset({"platform_admin"}),
    )
    repository.create_user(
        CreateAdminUserRequest(
            account_id="account-1",
            display_name="Image User",
            email="image@example.com",
            organization_id="org-1",
            token_limit=0,
            image_limit=10,
            password="ordinary-user-password-1",
            client_request_id="create-image-user",
        ),
        actor=actor,
    )
    provider = _AdminManagementImageUsageProvider(
        _CompletedProvider("gpt-image-2"), repository  # type: ignore[arg-type]
    )
    jobs = [
        SimpleNamespace(
            job_id="imgjob_" + str(index) * 32,
            account_id="account-1",
            request=SimpleNamespace(count=1, model_id="gpt-image-2"),
            status=ImageJobStatus.RUNNING,
            created_at=datetime.now(UTC),
        )
        for index in (2, 3)
    ]

    async def scenario() -> None:
        await asyncio.gather(
            *(
                provider.submit(job, idempotency_key=f"image-batch-{index}")
                for index, job in enumerate(jobs)
            )
        )

    asyncio.run(scenario())
    assert repository.get_user("account-1").images_used == 2
    with pytest.raises(AdminManagementConflict, match="reused"):
        repository.record_provider_usage(
            source_service="image_service",
            source_id=jobs[0].job_id,
            usage_kind="image",
            account_id="account-1",
            image_count=1,
            provider_created_at=jobs[0].created_at.isoformat(),
            requested_model_id="gpt-image-2",
            provider_reported_model_id="gpt-image-2-pro",
            actual_provider_id="managed-image",
            job_status="running",
            result_status="completed",
        )
    connection = repository._connect()
    try:
        rows = connection.execute(
            "SELECT source_id,requested_model_id,provider_reported_model_id,actual_model_id,"
            "fallback_from_model_id,fallback_used FROM admin_ops_provider_usage_facts "
            "ORDER BY source_id"
        ).fetchall()
    finally:
        connection.close()
    assert [tuple(row) for row in rows] == [
        (jobs[0].job_id, "gpt-image-2", "gpt-image-2", None, None, None),
        (jobs[1].job_id, "gpt-image-2", "gpt-image-2", None, None, None),
    ]
