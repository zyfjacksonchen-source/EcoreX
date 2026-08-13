from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
import httpx
import pytest

from ecorex.artifacts import ArtifactService, RenditionKind, RetouchAnnotation
from ecorex.artifacts.retouch_surface import compile_annotation_mask
from ecorex.capabilities import SandboxLevel, ToolExecutionScope, ToolInvocationContext
from ecorex.image_orchestrator import (
    ImageContentStore,
    ImageJobWorker,
    ImageOperation,
    ImageOrchestrationService,
    ImageSubmitRequest,
    ImageUsage,
    SQLiteImageSchemaManager,
    SQLiteImageJobStore,
    create_image_orchestration_router,
)
from ecorex.image_orchestrator.provider import ProviderResult, ProviderState
from ecorex.integration.image_orchestrator import ManagedImageRetouchAdapter
from ecorex.integration.image_tools import (
    ImageToolError,
    ImageToolPublicationBusy,
    RuntimeImageToolBackend,
)
from ecorex.integration.managed_image import (
    ManagedImageClientError,
    ManagedImageDownloadedResult,
    ManagedImageInputAsset,
    ManagedImageJob,
    ManagedImageOrchestrationClient,
    ManagedImageResultDescriptor,
)
from ecorex.integration.retouch_adapter import (
    RetouchImageAsset,
    RetouchMaskAsset,
    StructuredRetouchAdapterRequest,
)
from ecorex.input_attachments import InputAttachmentService
from ecorex.protocol import CreateTurnRequest, ItemKind
from ecorex.runtime.kernel import RuntimeKernel
from ecorex.session import ManagedSessionService, ManagedSessionSnapshot


def _test_png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


PNG = _test_png((30, 90, 210))
MASK = b"\x89PNG\r\n\x1a\nmanaged-image-mask"


def _snapshot(
    account_id: str,
    *,
    generation: int = 7,
    lease_digest: str | None = None,
    revision: int = 1,
) -> ManagedSessionSnapshot:
    now = datetime.now(UTC)
    return ManagedSessionSnapshot(
        generation=generation,
        lease_digest=lease_digest or ("a" if account_id == "tenant-001" else "b") * 64,
        lease_id="lease-0001",
        account_id=account_id,
        organization_id="organization-001",
        display_name="Test User",
        roles=("member",),
        model_allowlist=("gpt-image-2",),
        quota={"images": 100},
        admin_denies=(),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        revision=revision,
    )


def _session(state: dict[str, ManagedSessionSnapshot]) -> ManagedSessionService:
    service = object.__new__(ManagedSessionService)
    service.snapshot = lambda: state["snapshot"]  # type: ignore[method-assign]
    service.bearer_token = lambda: "managed-session-token-000000000000"  # type: ignore[method-assign]
    return service


class _Principal:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id


class _Provider:
    provider_id = "managed-provider"

    def __init__(self) -> None:
        self.submits = 0

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.submits += 1
        return ProviderResult(
            ProviderState.COMPLETED,
            provider_request_id=f"provider-request-{self.submits:04d}",
            payload=PNG,
            mime_type="image/png",
            sha256=hashlib.sha256(PNG).hexdigest(),
            usage=ImageUsage(
                self.provider_id,
                job.request.model_id,
                billed_units=1,
            ),
        )

    async def recover(self, job, *, idempotency_key: str, provider_request_id: str | None) -> ProviderResult:
        return ProviderResult(ProviderState.NOT_FOUND)

    async def cancel(self, job, *, idempotency_key: str, provider_request_id: str | None) -> None:
        return None


def _cloud(tmp_path: Path):
    database = tmp_path / "cloud.db"
    SQLiteImageSchemaManager(database).migrate()
    store = SQLiteImageJobStore(database)
    cas = ImageContentStore(tmp_path / "cloud-cas")
    service = ImageOrchestrationService(store)
    app = FastAPI()

    def principal(authorization: str = Header(...)) -> _Principal:
        if authorization != "Bearer managed-session-token-000000000000":
            raise HTTPException(status_code=401)
        return _Principal("tenant-001")

    app.include_router(
        create_image_orchestration_router(
            service,
            principal_dependency=principal,
            content_store=cas,
        )
    )
    provider = _Provider()
    worker = ImageJobWorker(
        store,
        provider,
        cas,
        lease_seconds=5,
        heartbeat_seconds=0.1,
        base_retry_seconds=0.01,
        max_retry_seconds=1,
    )
    return app, store, cas, provider, worker


def test_managed_client_restart_recovers_before_submit_and_downloads_verified_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, _store, _cas, provider, image_worker = _cloud(tmp_path)
        state = {"snapshot": _snapshot("tenant-001")}
        session = _session(state)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=session,
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "local.db",
                client=http,
                poll_interval_seconds=0.05,
                max_poll_seconds=10,
            )
            request = ImageSubmitRequest(
                ImageOperation.GENERATE,
                "gpt-image-2",
                "managed-client-request-0001",
                "create an office dashboard",
                deadline_seconds=30,
            )
            task = asyncio.create_task(client.execute(request))
            while not task.done():
                await image_worker.run_once("cloud-worker-001")
                await asyncio.sleep(0.01)
            first = await task
            assert first.content == PNG
            assert provider.submits == 1

            restarted = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=session,
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "local.db",
                client=http,
                poll_interval_seconds=0.05,
                max_poll_seconds=10,
            )
            replay = await restarted.execute(request)
            assert replay.job.job_id == first.job.job_id
            assert replay.content == PNG
            assert provider.submits == 1

    asyncio.run(scenario())


def test_uncertain_dead_letter_does_not_invite_a_new_provider_submit() -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        client = object.__new__(ManagedImageOrchestrationClient)
        client.max_poll_seconds = 10
        for error_code, retryable in (
            ("provider_uncertain", False),
            ("provider_unavailable", True),
        ):
            job = ManagedImageJob(
                job_id="imgjob_" + "1" * 32,
                operation="retouch",
                model_id="gpt-image-2",
                status="dead_letter",
                attempt=4,
                max_attempts=4,
                created_at=now,
                updated_at=now,
                deadline=now + timedelta(minutes=10),
                result=None,
                last_error_code=error_code,
            )
            with pytest.raises(ManagedImageClientError) as raised:
                await client.poll(job)
            assert raised.value.code == error_code
            assert raised.value.retryable is retryable

    asyncio.run(scenario())


def test_client_accepts_same_account_refresh_between_execute_operations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, _store, _cas, provider, image_worker = _cloud(tmp_path)
        state = {"snapshot": _snapshot("tenant-001")}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=_session(state),
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "local.db",
                client=http,
                poll_interval_seconds=0.05,
                max_poll_seconds=10,
            )

            async def execute(client_request_id: str) -> ManagedImageDownloadedResult:
                task = asyncio.create_task(
                    client.execute(
                        ImageSubmitRequest(
                            ImageOperation.GENERATE,
                            "gpt-image-2",
                            client_request_id,
                            "create an office dashboard",
                            deadline_seconds=30,
                        )
                    )
                )
                while not task.done():
                    await image_worker.run_once("cloud-worker-refresh")
                    await asyncio.sleep(0.01)
                return await task

            first = await execute("managed-client-before-refresh-0001")
            state["snapshot"] = _snapshot(
                "tenant-001",
                generation=8,
                lease_digest="c" * 64,
                revision=2,
            )
            second = await execute("managed-client-after-refresh-0002")

            assert first.content == second.content == PNG
            assert provider.submits == 2

            state["snapshot"] = _snapshot("tenant-002")
            with pytest.raises(ManagedImageClientError) as fenced:
                await execute("managed-client-cross-account-0003")
            assert fenced.value.code == "managed_image_session_changed"
            assert provider.submits == 2

    asyncio.run(scenario())


def test_client_continues_policy_equivalent_refresh_during_one_execute_operation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, _store, _cas, provider, image_worker = _cloud(tmp_path)
        initial = _snapshot("tenant-001")
        state = {"snapshot": initial}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=_session(state),
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "local.db",
                client=http,
                poll_interval_seconds=0.05,
                max_poll_seconds=10,
            )
            original_poll = client.poll

            async def poll_after_refresh(
                job: ManagedImageJob, *, timeout_seconds: float | None = None
            ) -> ManagedImageJob:
                await image_worker.run_once("cloud-worker-mid-operation-refresh")
                state["snapshot"] = replace(
                    initial,
                    generation=8,
                    lease_digest="c" * 64,
                    lease_id="lease-refreshed",
                    issued_at=datetime.now(UTC),
                    revision=2,
                )
                return await original_poll(job, timeout_seconds=timeout_seconds)

            client.poll = poll_after_refresh  # type: ignore[method-assign]
            downloaded = await client.execute(
                ImageSubmitRequest(
                    ImageOperation.GENERATE,
                    "gpt-image-2",
                    "managed-client-mid-operation-refresh-0001",
                    "create an office dashboard",
                    deadline_seconds=30,
                )
            )
            assert downloaded.content == PNG
            assert provider.submits == 1

    asyncio.run(scenario())


def test_generate_and_retouch_share_one_cloud_scheduler(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, store, _cas, provider, image_worker = _cloud(tmp_path)
        state = {"snapshot": _snapshot("tenant-001")}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=_session(state),
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "local.db",
                client=http,
                poll_interval_seconds=0.05,
                max_poll_seconds=10,
            )
            digest = hashlib.sha256(MASK).hexdigest()
            await client.upload_input(
                ManagedImageInputAsset(digest, "image/png", MASK)
            )
            generated, _ = await client.submit(
                ImageSubmitRequest(
                    ImageOperation.GENERATE,
                    "gpt-image-2",
                    "shared-scheduler-generate-0001",
                    "generate a chart",
                )
            )
            retouched, _ = await client.submit(
                ImageSubmitRequest(
                    ImageOperation.RETOUCH,
                    "gpt-image-2",
                    "shared-scheduler-retouch-0001",
                    "structured retouch",
                    input_sha256=(digest,),
                    instruction="make the chart title blue",
                )
            )
            assert store.metrics(account_id="tenant-001").queued == 2
            assert {store.get(generated.job_id).request.operation, store.get(retouched.job_id).request.operation} == {
                ImageOperation.GENERATE,
                ImageOperation.RETOUCH,
            }
            await image_worker.run_once("cloud-worker-001")
            await image_worker.run_once("cloud-worker-002")
            assert store.get(generated.job_id).status.value == "completed"
            assert store.get(retouched.job_id).status.value == "completed"
            assert provider.submits == 2

    asyncio.run(scenario())


def test_client_rejects_digest_mismatch_and_session_account_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = {"snapshot": _snapshot("tenant-001")}
        session = _session(state)
        digest = hashlib.sha256(PNG).hexdigest()
        now = datetime.now(UTC)
        job = ManagedImageJob(
            job_id="imgjob_" + "1" * 32,
            operation="generate",
            model_id="gpt-image-2",
            status="completed",
            attempt=1,
            max_attempts=4,
            created_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=5),
            result=ManagedImageResultDescriptor(digest, len(PNG), "image/png"),
            last_error_code=None,
        )

        async def mismatch(request: httpx.Request) -> httpx.Response:
            bad = PNG[:-1] + b"X"
            return httpx.Response(
                200,
                content=bad,
                headers={
                    "Content-Type": "image/png",
                    "ETag": f'"{digest}"',
                    "X-Content-SHA256": digest,
                    "Content-Length": str(len(bad)),
                },
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(mismatch)) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=session,
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "mismatch.db",
                client=http,
            )
            with pytest.raises(ManagedImageClientError) as rejected:
                await client.download_result(job)
            assert rejected.value.code == "managed_image_result_digest_mismatch"

        job_payload = {
            "job_id": job.job_id,
            "operation": job.operation,
            "model_id": job.model_id,
            "status": job.status,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "deadline": job.deadline.isoformat(),
            "result": {
                "sha256": digest,
                "size_bytes": len(PNG),
                "mime_type": "image/png",
            },
            "last_error_code": None,
        }

        async def switch_account(request: httpx.Request) -> httpx.Response:
            state["snapshot"] = _snapshot("tenant-002")
            return httpx.Response(200, json=job_payload, request=request)

        state["snapshot"] = _snapshot("tenant-001")
        async with httpx.AsyncClient(transport=httpx.MockTransport(switch_account)) as http:
            client = ManagedImageOrchestrationClient(
                "https://images.example/api/v1/images",
                session=session,
                allowed_hosts=frozenset({"images.example"}),
                database_path=tmp_path / "fence.db",
                client=http,
            )
            with pytest.raises(ManagedImageClientError) as fenced:
                await client.get(job.job_id)
            assert fenced.value.code == "managed_image_session_changed"

    asyncio.run(scenario())


def test_managed_client_requires_exact_session_and_strict_job_types(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="exact ManagedSessionService"):
        ManagedImageOrchestrationClient(
            "https://images.example/api/v1/images",
            session=object(),  # type: ignore[arg-type]
            allowed_hosts=frozenset({"images.example"}),
            database_path=tmp_path / "invalid-session.db",
        )

    now = datetime.now(UTC)
    payload = {
        "job_id": "imgjob_" + "4" * 32,
        "operation": "generate",
        "model_id": "gpt-image-2",
        "status": "queued",
        "attempt": True,
        "max_attempts": 4,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "deadline": (now + timedelta(minutes=5)).isoformat(),
        "result": None,
        "last_error_code": None,
    }
    with pytest.raises(ManagedImageClientError) as protocol_error:
        ManagedImageOrchestrationClient._job(payload)
    assert protocol_error.value.code == "managed_image_protocol"


def test_legacy_sync_retouch_transport_is_absent_from_product_surface() -> None:
    import ecorex.integration as product_integration

    assert not hasattr(product_integration, "ManagedGatewayImageRetouchAdapter")
    assert not hasattr(product_integration, "RetouchGatewayCredentialProvider")
    repository_root = Path(__file__).resolve().parents[2]
    product_sources = tuple((repository_root / "ecorex").rglob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in product_sources)
    assert "ManagedGatewayImageRetouchAdapter" not in source
    assert "RetouchGatewayCredentialProvider" not in source
    assert "/v1/images/retouch" not in source
    assert "image_base64" not in source


def test_structured_retouch_maps_surface_mask_and_digest_inputs_to_one_job(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        captured = []
        now = datetime.now(UTC)
        digest = hashlib.sha256(PNG).hexdigest()
        result_job = ManagedImageJob(
            job_id="imgjob_" + "2" * 32,
            operation="retouch",
            model_id="gpt-image-2",
            status="completed",
            attempt=1,
            max_attempts=4,
            created_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=5),
            result=ManagedImageResultDescriptor(digest, len(PNG), "image/png"),
            last_error_code=None,
        )
        client = object.__new__(ManagedImageOrchestrationClient)

        async def execute(command, *, inputs=()):
            captured.append((command, inputs))
            return ManagedImageDownloadedResult(result_job, PNG)

        client.execute = execute  # type: ignore[method-assign]
        client.recover_result = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        adapter = ManagedImageRetouchAdapter(client)
        base = RetouchImageAsset(
            "artifact-001", "revision-001", "image/png", digest, PNG
        )
        annotation = RetouchAnnotation(
            kind="rectangle",
            normalized_geometry={"x": 0.1, "y": 0.1, "width": 0.25, "height": 0.25},
            instruction="make the title blue",
        )
        compiled = compile_annotation_mask(
            1024, 1024, [annotation.to_dict()]
        )
        mask = RetouchMaskAsset(
            sha256=compiled.sha256,
            width_px=compiled.width_px,
            height_px=compiled.height_px,
            covered_fraction=compiled.covered_fraction,
            pixel_regions=compiled.pixel_regions,
            content=compiled.png_bytes,
        )
        request = StructuredRetouchAdapterRequest(
            job_id="retouch-job-0001",
            idempotency_key="retouch-idempotency-0001",
            model_id="gpt-image-2",
            base=base,
            selected=(base,),
            references=(),
            annotations=(annotation,),
            global_instruction="make the title blue",
            edit_surface={
                "base_revision_id": base.revision_id,
                "raster_digest": base.sha256,
                "width_px": 1024,
                "height_px": 1024,
                "orientation": 1,
                "color_space": "sRGB",
                "mime_type": "image/png",
                "coordinate_space_version": "oriented-normalized-v1",
            },
            mask=mask,
        )
        result = await adapter.edit(request)
        assert result.result_id == result_job.job_id
        assert len(captured) == 1
        command, inputs = captured[0]
        assert command.operation is ImageOperation.RETOUCH
        assert (command.width, command.height) == (1024, 1024)
        assert command.input_sha256 == (base.sha256, mask.sha256)
        structured = json.loads(command.instruction)
        assert structured["edit_surface"]["raster_digest"] == base.sha256
        assert structured["mask"]["sha256"] == mask.sha256
        assert {item.sha256 for item in inputs} == {base.sha256, mask.sha256}

    asyncio.run(scenario())


def test_structured_retouch_accepts_bounded_large_surface_mask_and_rejects_drift(
) -> None:
    digest = hashlib.sha256(PNG).hexdigest()
    base = RetouchImageAsset(
        "artifact-large", "revision-large", "image/png", digest, PNG
    )
    annotation = RetouchAnnotation(
        kind="rectangle",
        normalized_geometry={"x": 0.2, "y": 0.25, "width": 0.3, "height": 0.2},
        instruction="replace only this region",
    )
    compiled = compile_annotation_mask(3840, 2160, [annotation.to_dict()])
    assert (compiled.width_px, compiled.height_px) == (2048, 1152)
    mask = RetouchMaskAsset(
        sha256=compiled.sha256,
        width_px=compiled.width_px,
        height_px=compiled.height_px,
        covered_fraction=compiled.covered_fraction,
        pixel_regions=compiled.pixel_regions,
        content=compiled.png_bytes,
    )
    surface = {
        "base_revision_id": base.revision_id,
        "raster_digest": base.sha256,
        "width_px": 3840,
        "height_px": 2160,
        "orientation": 1,
        "color_space": "sRGB",
        "mime_type": "image/png",
        "coordinate_space_version": "oriented-normalized-v1",
    }
    request = StructuredRetouchAdapterRequest(
        job_id="retouch-job-large",
        idempotency_key="retouch-idempotency-large",
        model_id="gpt-image-2",
        base=base,
        selected=(base,),
        references=(),
        annotations=(annotation,),
        global_instruction="preserve everything outside the rectangle",
        edit_surface=surface,
        mask=mask,
    )
    assert request.mask is mask
    client = object.__new__(ManagedImageOrchestrationClient)
    adapter = ManagedImageRetouchAdapter(client)
    command, _inputs = adapter._command(request)
    assert (command.width, command.height) == (3840, 2160)

    drifted = RetouchMaskAsset(
        sha256=compiled.sha256,
        width_px=compiled.width_px,
        height_px=compiled.height_px,
        covered_fraction=min(1.0, compiled.covered_fraction + 0.01),
        pixel_regions=compiled.pixel_regions,
        content=compiled.png_bytes,
    )
    with pytest.raises(ValueError, match="structured annotations"):
        StructuredRetouchAdapterRequest(
            job_id="retouch-job-large-drift",
            idempotency_key="retouch-idempotency-large-drift",
            model_id="gpt-image-2",
            base=base,
            selected=(base,),
            references=(),
            annotations=(annotation,),
            global_instruction="preserve everything outside the rectangle",
            edit_surface=surface,
            mask=drifted,
        )


def test_imagegen_publication_crash_recovers_artifact_without_cloud_repeat(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.db"
        kernel = RuntimeKernel(database)
        thread = kernel.create_thread()
        turn = kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(
                input="generate image",
                image_model_id="gpt-image-2",
                client_message_id="image-message-0001",
            ),
        ).turn
        artifacts = ArtifactService(tmp_path / "artifacts", database_path=database)
        now = datetime.now(UTC)
        digest = hashlib.sha256(PNG).hexdigest()
        job = ManagedImageJob(
            job_id="imgjob_" + "3" * 32,
            operation="generate",
            model_id="gpt-image-2",
            status="completed",
            attempt=1,
            max_attempts=4,
            created_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=5),
            result=ManagedImageResultDescriptor(digest, len(PNG), "image/png"),
            last_error_code=None,
        )

        class Client:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, request, *, inputs=()):
                self.calls += 1
                return ManagedImageDownloadedResult(job, PNG)

        client = Client()
        armed = {"value": True}

        def fault(phase: str, _key: str) -> None:
            if phase == "after_artifact" and armed["value"]:
                raise RuntimeError("simulated crash after Artifact commit")

        context = ToolInvocationContext(
            invocation_id="invoke-image-0001",
            capability_snapshot_id="capability-0001",
            policy_snapshot_id="permission-0001",
            tool_id="imagegen",
            idempotency_key=f"{turn.turn_id}:tool-image-0001",
            approved=True,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
            execution_scope=ToolExecutionScope(
                job_id=turn.job.job_id if hasattr(turn, "job") else "job-image-0001",
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
            ),
        )
        backend = RuntimeImageToolBackend(
            database_path=database,
            artifacts=artifacts,
            kernel=kernel,
            account_id="local-user",
            client=client,  # type: ignore[arg-type]
            fault_hook=fault,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await backend.generate_image({"instruction": "draw a dashboard"}, context)
        assert client.calls == 1
        published = artifacts.list_user_artifacts(account_id="local-user")
        assert len(published) == 1
        assert {item.kind for item in published[0].renditions} == {
            RenditionKind.THUMBNAIL,
            RenditionKind.PREVIEW,
        }

        armed["value"] = False
        restarted = RuntimeImageToolBackend(
            database_path=database,
            artifacts=artifacts,
            kernel=kernel,
            account_id="local-user",
            client=client,  # type: ignore[arg-type]
        )
        output = await restarted.generate_image(
            {"instruction": "draw a dashboard"}, context
        )
        assert client.calls == 1
        assert len(artifacts.list_user_artifacts(account_id="local-user")) == 1
        assert "base64" not in json.dumps(output)
        assert str(tmp_path) not in json.dumps(output)
        publication = restarted.publications.row(
            f"imagegen:local-user:{context.idempotency_key}"
        )
        assert publication is not None
        assert publication["status"] == "completed"
        assert publication["cloud_job_id"] == job.job_id
        assert publication["result_sha256"] == digest
        with pytest.raises(ImageToolError, match="identity"):
            await restarted.generate_image(
                {"instruction": "reuse the key for different pixels"}, context
            )
        assert client.calls == 1
        assert len(artifacts.list_user_artifacts(account_id="local-user")) == 1
        projection = kernel.projection(thread.thread_id)
        assert any(item.kind is ItemKind.ARTIFACT for item in projection.items)

    asyncio.run(scenario())


def test_uploaded_turn_image_is_a_managed_image_edit_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        import io

        from PIL import Image

        database = tmp_path / "runtime.db"
        kernel = RuntimeKernel(database)
        artifacts = ArtifactService(tmp_path / "artifacts", database_path=database)
        attachments = InputAttachmentService(artifacts, account_id="local-user")
        source = io.BytesIO()
        Image.new("RGB", (640, 480), (20, 80, 220)).save(source, format="PNG")
        source_bytes = source.getvalue()
        uploaded = attachments.upload(
            source_bytes,
            filename="user-reference.png",
            mime_type="image/png",
            client_request_id="imagegen-attachment-upload",
        )
        thread = kernel.create_thread()
        turn = kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(
                input="把这张图改成暖色",
                image_model_id="gpt-image-2",
                client_message_id="imagegen-attachment-message",
                metadata={"input_attachments": [uploaded.model_dump(mode="json")]},
            ),
        ).turn
        now = datetime.now(UTC)
        result_digest = hashlib.sha256(PNG).hexdigest()
        job = ManagedImageJob(
            job_id="imgjob_" + "8" * 32,
            operation="retouch",
            model_id="gpt-image-2",
            status="completed",
            attempt=1,
            max_attempts=4,
            created_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=5),
            result=ManagedImageResultDescriptor(result_digest, len(PNG), "image/png"),
            last_error_code=None,
        )

        class Client:
            def __init__(self) -> None:
                self.requests = []
                self.inputs = []

            async def execute(self, request, *, inputs=()):
                self.requests.append(request)
                self.inputs.append(inputs)
                return ManagedImageDownloadedResult(job, PNG)

        client = Client()
        context = ToolInvocationContext(
            invocation_id="invoke-image-attachment",
            capability_snapshot_id="capability-image-attachment",
            policy_snapshot_id="permission-image-attachment",
            tool_id="imagegen",
            idempotency_key=f"{turn.turn_id}:tool-image-attachment",
            approved=True,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
            execution_scope=ToolExecutionScope(
                job_id="job-image-attachment",
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
            ),
        )
        backend = RuntimeImageToolBackend(
            database_path=database,
            artifacts=artifacts,
            kernel=kernel,
            account_id="local-user",
            client=client,  # type: ignore[arg-type]
            input_attachments=attachments,
        )

        output = await backend.generate_image(
            {
                "prompt": "改成暖色，保留构图",
                "image_url": uploaded.attachment_id,
            },
            context,
        )

        assert output["status"] == "completed"
        assert output["model"] == "gpt-image-2"
        assert output["images"] == [
            {
                "url": output["preview_url"],
                "artifact_id": output["artifact_id"],
                "revision_id": output["revision_id"],
            }
        ]
        assert client.requests[0].operation is ImageOperation.RETOUCH
        assert len(client.inputs[0]) == 1
        assert client.inputs[0][0].content.startswith(b"\xff\xd8")
        assert len(client.inputs[0][0].content) <= 8 * 1024 * 1024
        assert client.inputs[0][0].sha256 == hashlib.sha256(
            client.inputs[0][0].content
        ).hexdigest()
        assert attachments.read(uploaded.attachment_id)[1] == source_bytes

    asyncio.run(scenario())


def test_imagegen_publication_heartbeat_fences_second_owner_after_initial_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.db"
        kernel = RuntimeKernel(database)
        thread = kernel.create_thread()
        turn = kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(
                input="generate a slow image",
                image_model_id="gpt-image-2",
                client_message_id="image-message-slow-0001",
            ),
        ).turn
        artifacts = ArtifactService(tmp_path / "artifacts", database_path=database)
        now = datetime.now(UTC)
        digest = hashlib.sha256(PNG).hexdigest()
        job = ManagedImageJob(
            job_id="imgjob_" + "5" * 32,
            operation="generate",
            model_id="gpt-image-2",
            status="completed",
            attempt=1,
            max_attempts=4,
            created_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=5),
            result=ManagedImageResultDescriptor(digest, len(PNG), "image/png"),
            last_error_code=None,
        )

        class SlowClient:
            def __init__(self) -> None:
                self.calls = 0
                self.started = asyncio.Event()

            async def execute(self, request, *, inputs=()):
                self.calls += 1
                self.started.set()
                await asyncio.sleep(0.8)
                return ManagedImageDownloadedResult(job, PNG)

        client = SlowClient()
        context = ToolInvocationContext(
            invocation_id="invoke-image-slow-0001",
            capability_snapshot_id="capability-image-slow-0001",
            policy_snapshot_id="permission-image-slow-0001",
            tool_id="imagegen",
            idempotency_key=f"{turn.turn_id}:tool-image-slow-0001",
            approved=True,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
            execution_scope=ToolExecutionScope(
                job_id="job-image-slow-0001",
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
            ),
        )
        backend = RuntimeImageToolBackend(
            database_path=database,
            artifacts=artifacts,
            kernel=kernel,
            account_id="local-user",
            client=client,  # type: ignore[arg-type]
            publication_lease_seconds=0.3,
        )
        first = asyncio.create_task(
            backend.generate_image({"instruction": "draw a slow dashboard"}, context)
        )
        await client.started.wait()
        await asyncio.sleep(0.45)
        with pytest.raises(ImageToolPublicationBusy):
            await backend.generate_image(
                {"instruction": "draw a slow dashboard"}, context
            )
        result = await first
        assert result["artifact_id"]
        assert client.calls == 1
        assert len(artifacts.list_user_artifacts(account_id="local-user")) == 1

    asyncio.run(scenario())


def test_imagegen_missing_result_descriptor_is_stable_error_and_releases_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.db"
        kernel = RuntimeKernel(database)
        thread = kernel.create_thread()
        turn = kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(
                input="generate image",
                image_model_id="gpt-image-2",
                client_message_id="image-message-broken-0001",
            ),
        ).turn
        artifacts = ArtifactService(tmp_path / "artifacts", database_path=database)

        class BrokenDownload:
            class Job:
                result = None

            job = Job()

        class BrokenClient:
            async def execute(self, request, *, inputs=()):
                return BrokenDownload()

        context = ToolInvocationContext(
            invocation_id="invoke-image-broken-0001",
            capability_snapshot_id="capability-image-broken-0001",
            policy_snapshot_id="permission-image-broken-0001",
            tool_id="imagegen",
            idempotency_key=f"{turn.turn_id}:tool-image-broken-0001",
            approved=True,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
            execution_scope=ToolExecutionScope(
                job_id="job-image-broken-0001",
                thread_id=thread.thread_id,
                turn_id=turn.turn_id,
            ),
        )
        backend = RuntimeImageToolBackend(
            database_path=database,
            artifacts=artifacts,
            kernel=kernel,
            account_id="local-user",
            client=BrokenClient(),  # type: ignore[arg-type]
        )
        with pytest.raises(ImageToolError) as missing:
            await backend.generate_image({"instruction": "draw a dashboard"}, context)
        assert missing.value.code == "managed_image_result_descriptor_missing"
        publication = backend.publications.row(
            f"imagegen:local-user:{context.idempotency_key}"
        )
        assert publication is not None
        assert publication["lease_token"] is None
        assert artifacts.list_user_artifacts(account_id="local-user") == ()

    asyncio.run(scenario())
