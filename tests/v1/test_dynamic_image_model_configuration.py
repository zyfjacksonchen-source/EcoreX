from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ecorex.control_plane.management import (
    AdminManagementRepository,
    ModelConnectionTestResult,
)
from ecorex.control_plane.management_models import (
    ActiveModelConfiguration,
    CreateModelConfigurationRequest,
    StageModelConfigurationRequest,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.image_orchestrator.dynamic_provider import (
    AdminImageModelConfigurationResolver,
    DynamicManagedImageProvider,
)
from ecorex.image_orchestrator.cas import ImageContentStore
from ecorex.image_orchestrator.models import ImageOperation, ImageSubmitRequest, ImageUsage
from ecorex.image_orchestrator.openai_provider import (
    OpenAICompatibleImageProvider,
)
from ecorex.image_orchestrator.provider import (
    ProviderResult,
    ProviderState,
    ProviderUnavailable,
)
from ecorex.image_orchestrator.service import ImageOrchestrationService
from ecorex.image_orchestrator.sqlite_schema import SQLiteImageSchemaManager
from ecorex.image_orchestrator.sqlite_store import SQLiteImageJobStore


KEY = b"i" * 32
ACTOR = ControlPrincipal(
    subject="image-administrator",
    client_id="admin-web",
    account_id="admin",
    roles=frozenset({"platform_admin"}),
)


def _repository(tmp_path: Path) -> AdminManagementRepository:
    database = tmp_path / "management.db"
    AdminManagementSchemaManager(database).migrate()
    return AdminManagementRepository(database, encryption_key=KEY)


def _activate_new(
    repository: AdminManagementRepository,
    *,
    local_model_id: str,
    modality: str,
    upstream_model_id: str,
    api_key: str,
    request_suffix: str,
) -> str:
    created = repository.create_model_configuration(
        CreateModelConfigurationRequest(
            local_model_id=local_model_id,
            modality=modality,
            display_name=upstream_model_id,
            upstream_model_id=upstream_model_id,
            provider_preset="openai_compatible_image",
            is_default=True,
            enabled=True,
            api_key=api_key,
            client_request_id=f"create-{request_suffix}",
        ),
        actor=ACTOR,
    )
    lease = repository.begin_model_test(
        created.config_id,
        1,
        actor=ACTOR,
        client_request_id=f"test-{request_suffix}",
    )
    repository.finish_model_test(
        lease,
        ModelConnectionTestResult(passed=True),
        actor=ACTOR,
    )
    return created.config_id


def _activate_next(
    repository: AdminManagementRepository,
    config_id: str,
    *,
    active_revision: int,
    upstream_model_id: str,
    api_key: str,
) -> int:
    staged = repository.stage_model_configuration(
        config_id,
        StageModelConfigurationRequest(
            display_name=upstream_model_id,
            upstream_model_id=upstream_model_id,
            provider_preset="openai_compatible_image",
            is_default=True,
            enabled=True,
            api_key=api_key,
            expected_active_revision=active_revision,
            client_request_id=f"stage-{upstream_model_id}",
        ),
        actor=ACTOR,
    )
    assert staged.draft is not None
    revision = staged.draft.revision
    lease = repository.begin_model_test(
        config_id,
        revision,
        actor=ACTOR,
        client_request_id=f"test-{upstream_model_id}",
    )
    repository.finish_model_test(
        lease,
        ModelConnectionTestResult(passed=True),
        actor=ACTOR,
    )
    return revision


class _RecordingProvider:
    provider_id = "managed-image"

    def __init__(self, configuration: ActiveModelConfiguration) -> None:
        self.configuration = configuration
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.calls.append(
            (
                job.request.model_id,
                self.configuration.api_key,
                self.configuration.revision,
            )
        )
        return ProviderResult(
            state=ProviderState.PENDING,
            provider_request_id=f"request-{self.configuration.revision}",
        )

    async def recover(self, job, **kwargs) -> ProviderResult:
        return await self.submit(job, idempotency_key=kwargs["idempotency_key"])

    async def cancel(self, job, **kwargs) -> None:
        self.calls.append(
            (
                job.request.model_id,
                self.configuration.api_key,
                self.configuration.revision,
            )
        )

    async def health(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class _GatedProvider(_RecordingProvider):
    def __init__(
        self,
        configuration: ActiveModelConfiguration,
        *,
        release: asyncio.Event,
        expected_entries: int,
    ) -> None:
        super().__init__(configuration)
        self.release = release
        self.expected_entries = expected_entries
        self.entered = asyncio.Event()
        self.entered_count = 0
        self.active_count = 0
        self.close_calls = 0
        self.closed_while_active = False

    async def _block(self) -> None:
        self.entered_count += 1
        self.active_count += 1
        if self.entered_count == self.expected_entries:
            self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.active_count -= 1

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        await self._block()
        return await super().submit(job, idempotency_key=idempotency_key)

    async def recover(self, job, **kwargs) -> ProviderResult:
        await self._block()
        return await _RecordingProvider.submit(
            self, job, idempotency_key=kwargs["idempotency_key"]
        )

    async def cancel(self, job, **kwargs) -> None:
        await self._block()
        await super().cancel(job, **kwargs)

    async def health(self) -> None:
        await self._block()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed_while_active = self.active_count != 0
        await super().aclose()


class _FallbackProvenanceProvider(_RecordingProvider):
    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.calls.append(
            (
                job.request.model_id,
                self.configuration.api_key,
                self.configuration.revision,
            )
        )
        return ProviderResult(
            ProviderState.COMPLETED,
            payload=b"image",
            mime_type="image/png",
            usage=ImageUsage(
                "upstream-image", "gpt-image-2", output_units=1, billed_units=1
            ),
            actual_model_id="gpt-image-2",
            fallback_from_model_id="gpt-image-2-pro",
            fallback_used=True,
        )


def test_image_jobs_freeze_tested_revision_and_cache_is_bounded(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config_id = _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="provider-image-v1",
        api_key="sk-image-version-one",
        request_suffix="image-v1",
    )
    _activate_new(
        repository,
        local_model_id="gpt-image-2-edit",
        modality="image_edit",
        upstream_model_id="provider-edit-v1",
        api_key="sk-edit-version-one",
        request_suffix="edit-v1",
    )
    image_database = tmp_path / "image.db"
    SQLiteImageSchemaManager(image_database).migrate()
    store = SQLiteImageJobStore(image_database)
    resolver = AdminImageModelConfigurationResolver(repository)
    service = ImageOrchestrationService(
        store,
        allowed_models=frozenset({"gpt-image-2"}),
        model_configuration_resolver=resolver,
    )

    original_request = ImageSubmitRequest(
        operation=ImageOperation.GENERATE,
        model_id="gpt-image-2",
        client_request_id="image-job-versioned-one",
        prompt="Create a clean office illustration",
    )
    job_v1, created = service.submit("account-1", original_request)
    assert created
    assert job_v1.request.model_config_id == config_id
    assert job_v1.request.model_config_revision == 1
    assert job_v1.request.provider_model_id == "provider-image-v1"

    revision_2 = _activate_next(
        repository,
        config_id,
        active_revision=1,
        upstream_model_id="provider-image-v2",
        api_key="sk-image-version-two",
    )
    replayed, replay_created = service.submit("account-1", original_request)
    assert not replay_created
    assert replayed.job_id == job_v1.job_id
    assert replayed.request.model_config_revision == 1

    job_v2, created_v2 = service.submit(
        "account-1",
        ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="gpt-image-2",
            client_request_id="image-job-versioned-two",
            prompt="Create a second clean office illustration",
        ),
    )
    assert created_v2
    assert job_v2.request.model_config_revision == revision_2
    assert job_v2.request.provider_model_id == "provider-image-v2"

    providers: list[_RecordingProvider] = []

    def factory(configuration: ActiveModelConfiguration, _origin: str):
        provider = _RecordingProvider(configuration)
        providers.append(provider)
        return provider

    dynamic = DynamicManagedImageProvider(
        repository,
        provider_id="managed-image",
        origins={"ecorex_image": "https://images.ecorex.example"},
        timeout_seconds=120,
        connect_timeout_seconds=5,
        max_image_bytes=64 * 1024 * 1024,
        max_connections=8,
        max_concurrency=4,
        max_cached_revisions=2,
        provider_factory=factory,  # type: ignore[arg-type]
    )
    asyncio.run(dynamic.submit(job_v1, idempotency_key="provider-job-v1"))
    asyncio.run(dynamic.submit(job_v2, idempotency_key="provider-job-v2"))
    assert providers[0].calls == [("provider-image-v1", "sk-image-version-one", 1)]
    assert providers[1].calls == [("provider-image-v2", "sk-image-version-two", 2)]

    revision_3 = _activate_next(
        repository,
        config_id,
        active_revision=2,
        upstream_model_id="provider-image-v3",
        api_key="sk-image-version-three",
    )
    job_v3, _ = service.submit(
        "account-1",
        ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="gpt-image-2",
            client_request_id="image-job-versioned-three",
            prompt="Create a third clean office illustration",
        ),
    )
    assert job_v3.request.model_config_revision == revision_3
    asyncio.run(dynamic.submit(job_v3, idempotency_key="provider-job-v3"))
    assert providers[0].closed

    retouch = resolver.resolve(model_id="gpt-image-2", operation="retouch")
    assert retouch.provider_model_id == "provider-edit-v1"
    assert retouch.config_id != config_id
    asyncio.run(dynamic.aclose())


def test_image_model_stage_rejects_chat_provider_preset(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config_id = _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="provider-image-v1",
        api_key="sk-image-version-one",
        request_suffix="image-modality",
    )
    try:
        repository.stage_model_configuration(
            config_id,
            StageModelConfigurationRequest(
                display_name="wrong provider",
                upstream_model_id="provider-image-v2",
                provider_preset="responses",
                is_default=True,
                enabled=True,
                api_key=None,
                expected_active_revision=1,
                client_request_id="stage-wrong-provider",
            ),
            actor=ACTOR,
        )
    except Exception as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("image model accepted a chat provider preset")


def test_default_dynamic_provider_uses_cloud_direct_adapter_and_shared_inputs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="provider-image-v1",
        api_key="sk-image-version-one",
        request_suffix="image-direct-adapter",
    )
    content = ImageContentStore(tmp_path / "image-cas")
    dynamic = DynamicManagedImageProvider(
        repository,
        provider_id="managed-image",
        origins={"ecorex_image": "https://images.ecorex.example"},
        timeout_seconds=120,
        connect_timeout_seconds=5,
        max_image_bytes=64 * 1024 * 1024,
        max_connections=8,
        max_concurrency=4,
        input_store=content,
    )
    configuration = repository.active_model(modality="image_generation")
    provider = dynamic._create_provider(
        configuration, "https://images.ecorex.example"
    )
    assert isinstance(provider, OpenAICompatibleImageProvider)
    assert provider.input_store is content
    assert provider.allowed_models == frozenset({"provider-image-v1"})
    asyncio.run(provider.aclose())


def test_dynamic_pro_image_revision_allows_only_its_exact_safe_fallback(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="gpt-image-2-pro",
        api_key="sk-image-version-one",
        request_suffix="image-pro-fallback",
    )
    dynamic = DynamicManagedImageProvider(
        repository,
        provider_id="managed-image",
        origins={"ecorex_image": "https://images.ecorex.example"},
        timeout_seconds=120,
        connect_timeout_seconds=5,
        max_image_bytes=64 * 1024 * 1024,
        max_connections=8,
        max_concurrency=4,
    )
    configuration = repository.active_model(modality="image_generation")
    provider = dynamic._create_provider(
        configuration, "https://images.ecorex.example"
    )
    assert isinstance(provider, OpenAICompatibleImageProvider)
    assert provider.allowed_models == frozenset(
        {"gpt-image-2-pro", "gpt-image-2"}
    )
    asyncio.run(provider.aclose())


def test_dynamic_provider_preserves_exact_upstream_fallback_provenance(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="gpt-image-2-pro",
        api_key="sk-image-version-one",
        request_suffix="image-provenance",
    )
    image_database = tmp_path / "image-provenance.db"
    SQLiteImageSchemaManager(image_database).migrate()
    service = ImageOrchestrationService(
        SQLiteImageJobStore(image_database),
        allowed_models=frozenset({"gpt-image-2"}),
        model_configuration_resolver=AdminImageModelConfigurationResolver(repository),
    )
    job, created = service.submit(
        "account-1",
        ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="gpt-image-2",
            client_request_id="image-provider-provenance",
            prompt="Create a provenance-safe office illustration",
        ),
    )
    assert created
    dynamic = DynamicManagedImageProvider(
        repository,
        provider_id="managed-image",
        origins={"ecorex_image": "https://images.ecorex.example"},
        timeout_seconds=120,
        connect_timeout_seconds=5,
        max_image_bytes=64 * 1024 * 1024,
        max_connections=8,
        max_concurrency=4,
        provider_factory=lambda configuration, _origin: _FallbackProvenanceProvider(
            configuration
        ),
    )

    result = asyncio.run(
        dynamic.submit(job, idempotency_key="provider-provenance-submit")
    )

    assert result.usage is not None
    assert result.usage.model_id == "gpt-image-2"
    assert result.actual_model_id == "gpt-image-2"
    assert result.fallback_from_model_id == "gpt-image-2-pro"
    assert result.fallback_used is True
    asyncio.run(dynamic.aclose())


def test_dynamic_provider_close_drains_in_flight_calls_exactly_once(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _activate_new(
        repository,
        local_model_id="gpt-image-2",
        modality="image_generation",
        upstream_model_id="provider-image-v1",
        api_key="sk-image-version-one",
        request_suffix="image-close-race",
    )
    _activate_new(
        repository,
        local_model_id="gpt-image-2-edit",
        modality="image_edit",
        upstream_model_id="provider-edit-v1",
        api_key="sk-edit-version-one",
        request_suffix="edit-close-race",
    )
    image_database = tmp_path / "image-close-race.db"
    SQLiteImageSchemaManager(image_database).migrate()
    service = ImageOrchestrationService(
        SQLiteImageJobStore(image_database),
        allowed_models=frozenset({"gpt-image-2"}),
        model_configuration_resolver=AdminImageModelConfigurationResolver(repository),
    )
    job, created = service.submit(
        "account-1",
        ImageSubmitRequest(
            operation=ImageOperation.GENERATE,
            model_id="gpt-image-2",
            client_request_id="image-close-race",
            prompt="Create a concurrency-safe office illustration",
        ),
    )
    assert created

    async def scenario() -> None:
        release = asyncio.Event()
        providers: list[_GatedProvider] = []

        def factory(configuration: ActiveModelConfiguration, _origin: str):
            provider = _GatedProvider(
                configuration,
                release=release,
                expected_entries=4,
            )
            providers.append(provider)
            return provider

        dynamic = DynamicManagedImageProvider(
            repository,
            provider_id="managed-image",
            origins={"ecorex_image": "https://images.ecorex.example"},
            timeout_seconds=120,
            connect_timeout_seconds=5,
            max_image_bytes=64 * 1024 * 1024,
            max_connections=8,
            max_concurrency=4,
            max_cached_revisions=2,
            provider_factory=factory,  # type: ignore[arg-type]
        )
        calls = [
            asyncio.create_task(
                dynamic.submit(job, idempotency_key="provider-submit")
            ),
            asyncio.create_task(
                dynamic.recover(
                    job,
                    idempotency_key="provider-recover",
                    provider_request_id="request-previous",
                )
            ),
            asyncio.create_task(
                dynamic.cancel(
                    job,
                    idempotency_key="provider-cancel",
                    provider_request_id="request-current",
                )
            ),
            asyncio.create_task(dynamic.health()),
        ]
        while not providers:
            await asyncio.sleep(0)
        provider = providers[0]
        await asyncio.wait_for(provider.entered.wait(), timeout=2)

        closes = [asyncio.create_task(dynamic.aclose()) for _ in range(3)]
        await asyncio.sleep(0)
        assert all(not close.done() for close in closes)
        assert provider.close_calls == 0
        closes[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await closes[0]
        assert all(not close.done() for close in closes[1:])
        with pytest.raises(ProviderUnavailable, match="closed"):
            await dynamic.submit(job, idempotency_key="provider-after-close")

        release.set()
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        await asyncio.gather(*closes[1:])
        await dynamic.aclose()

        assert not any(
            isinstance(outcome, RuntimeError)
            and "reference count is unbalanced" in str(outcome)
            for outcome in outcomes
        )
        assert isinstance(outcomes[3], ProviderUnavailable)
        assert provider.close_calls == 1
        assert provider.closed
        assert not provider.closed_while_active

    asyncio.run(scenario())
