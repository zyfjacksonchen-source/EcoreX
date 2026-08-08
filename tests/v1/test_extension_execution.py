from __future__ import annotations

import asyncio
from dataclasses import replace
import io
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityDeniedError,
    CapabilityEffect,
    CapabilitySnapshotRepository,
    Exposure,
    IdempotencyClass,
    SandboxLevel,
    SchemaContractError,
    ToolExecutionScope,
    ToolInvocationContext,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
)
from ecorex.capabilities.schema import validate_schema_contract
from ecorex.gateway import GatewayEvent
from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    SteerTurnRequest,
)
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app
from ecorex.extensions import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionCompatibility,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionIntegrityError,
    ExtensionKind,
    ExtensionManifest,
    ExtensionNotFound,
    ExtensionProviderRevoked,
    ExtensionService,
    ExtensionSignature,
    ExtensionSource,
    ExtensionTransport,
    ExtensionTrust,
    LocalSkillBundleStore,
    MCPClientSupervisor,
    MCPProtocolError,
    MCPRuntimeBinding,
    MCPStdioTransport,
    MCPToolContract,
    MCPTransportError,
    ManagedHTTPMCPTransport,
    RuntimeBoundary,
    SQLiteExtensionRepository,
    SkillRuntime,
    ControlledSkillRunResult,
    SkillNotExecutable,
    SkillReadFact,
    SkillStateChanged,
    SkillSearchFact,
    verify_core_extension,
)


def _test_mcp_provider(extension_id: str) -> ToolProviderProvenance:
    return ToolProviderProvenance(
        kind=ToolProviderKind.MCP,
        provider_id=extension_id,
        revision_id="extrev_" + hashlib.sha256(extension_id.encode()).hexdigest(),
        trust=ToolProviderTrust.VERIFIED_PUBLISHER,
        key_id="test-publisher",
        evidence_sha256=hashlib.sha256(f"evidence:{extension_id}".encode()).hexdigest(),
    )


def _zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _skill_payload(
    name: str, body: str = "Follow the frozen office workflow.\n"
) -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        "description: A deterministic office review workflow.\n"
        "version: 1.0.0\n"
        'tags: ["office","review"]\n'
        "---\n"
        f"{body}"
    ).encode()


def _service(tmp_path: Path) -> ExtensionService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
        local_bundle_store=LocalSkillBundleStore(tmp_path / "extension-cas"),
    )


def _install_skill(
    service: ExtensionService,
    *,
    extension_id: str,
    name: str,
    suffix: str,
) -> Any:
    staged = service.install_local_skill_zip(
        _zip(
            {
                "SKILL.md": _skill_payload(name),
                "references/checklist.md": b"- verify totals\n- verify dates\n",
                "assets/preview.json": b'{"not":"readable as a reference"}',
            }
        ),
        extension_id=extension_id,
        expected_revision=(
            service.projection(extension_id).revision
            if _exists(service, extension_id)
            else 0
        ),
        client_request_id=f"install:{suffix}",
    )
    return asyncio.run(
        service.enable(
            extension_id,
            expected_revision=staged.revision,
            client_request_id=f"enable:{suffix}",
        )
    )


def _exists(service: ExtensionService, extension_id: str) -> bool:
    try:
        service.projection(extension_id)
    except Exception:
        return False
    return True


def test_skill_search_read_snapshot_and_no_path_disclosure(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _install_skill(
        service,
        extension_id="local.office-review",
        name="Office review",
        suffix="skill:1",
    )
    _install_skill(
        service,
        extension_id="local.other-review",
        name="Other review",
        suffix="skill:2",
    )
    extension_snapshot = service.snapshot()
    runtime = SkillRuntime(service)
    contribution = runtime.contribution_snapshot(extension_snapshot.snapshot_id)

    assert contribution.extension_snapshot_id == extension_snapshot.snapshot_id
    assert contribution.snapshot_id.startswith("extcontrib_")
    assert (
        service.repository.snapshot_payload(contribution.snapshot_id)[
            "extension_snapshot_id"
        ]
        == extension_snapshot.snapshot_id
    )
    assert all(item.revision_id.startswith("extrev_") for item in contribution.skills)
    assert all(len(item.export_digest) == 64 for item in contribution.skills)
    results = runtime.search(
        extension_snapshot.snapshot_id,
        "review",
        explicit_names=("local.other-review",),
    )
    assert [item.name for item in results] == ["Other review", "Office review"]
    assert set(results[0].to_dict()) == {
        "discovery_id",
        "name",
        "description",
        "tags",
    }
    assert results[0].discovery_id == (
        f"skill:local.other-review@"
        f"{next(item.revision_id for item in contribution.skills if item.extension_id == 'local.other-review')}"
    )
    assert "artifact_sha256" not in json.dumps(results[0].to_dict())
    assert [
        item.name
        for item in runtime.search(
            extension_snapshot.snapshot_id,
            "PowerPoint Office",
        )
    ] == ["Office review", "Other review"]
    assert [
        item.name
        for item in runtime.search(
            extension_snapshot.snapshot_id,
            "unmatched terms",
            explicit_names=("local.other-review",),
        )
    ] == ["Other review"]

    skill = next(item for item in contribution.skills if item.name == "Office review")
    assert len(skill.references) == 1
    result = runtime.read(
        extension_snapshot.snapshot_id,
        f"skill:{skill.extension_id}@{skill.revision_id}",
        reference_ids=(skill.references[0].reference_id,),
    )
    encoded = json.dumps(result)
    assert result["instructions"] == "Follow the frozen office workflow.\n"
    assert result["available_references"] == [
        {
            "reference_id": skill.references[0].reference_id,
            "size_bytes": skill.references[0].size_bytes,
        }
    ]
    assert result["references"][0]["content"].startswith("- verify totals")
    assert "SKILL.md" not in encoded
    assert "references/" not in encoded
    assert str(tmp_path) not in encoded


def test_skill_frozen_revision_is_revoked_not_silently_replaced(tmp_path: Path) -> None:
    service = _service(tmp_path)
    enabled = _install_skill(
        service,
        extension_id="local.frozen",
        name="Frozen skill",
        suffix="frozen:1",
    )
    frozen = service.snapshot().snapshot_id
    runtime = SkillRuntime(service)
    contribution = runtime.contribution_snapshot(frozen)
    skill = contribution.skills[0]

    staged = service.install_local_skill_zip(
        _zip({"SKILL.md": _skill_payload("Frozen skill", "New instructions.\n")}),
        extension_id="local.frozen",
        expected_revision=enabled.revision,
        client_request_id="install:frozen:2",
    )
    asyncio.run(
        service.enable(
            "local.frozen",
            expected_revision=staged.revision,
            client_request_id="enable:frozen:2",
        )
    )
    with pytest.raises(SkillStateChanged):
        runtime.read(frozen, f"skill:{skill.extension_id}@{skill.revision_id}")


def test_skill_disable_and_cas_tamper_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    enabled = _install_skill(
        service,
        extension_id="local.revoked",
        name="Revoked skill",
        suffix="revoked:1",
    )
    snapshot_id = service.snapshot().snapshot_id
    runtime = SkillRuntime(service)
    contribution = runtime.contribution_snapshot(snapshot_id)
    revoked = next(
        item for item in contribution.skills if item.extension_id == "local.revoked"
    )
    service.disable(
        "local.revoked",
        expected_revision=enabled.revision,
        client_request_id="disable:revoked:1",
    )
    with pytest.raises(SkillStateChanged):
        runtime.read(
            snapshot_id,
            f"skill:{revoked.extension_id}@{revoked.revision_id}",
        )

    # A separate active revision demonstrates read-time CAS re-verification.
    enabled = _install_skill(
        service,
        extension_id="local.tamper",
        name="Tamper skill",
        suffix="tamper:1",
    )
    snapshot_id = service.snapshot().snapshot_id
    runtime = SkillRuntime(service)
    contribution = runtime.contribution_snapshot(snapshot_id)
    item = next(
        value for value in contribution.skills if value.extension_id == "local.tamper"
    )
    target = (
        tmp_path
        / "extension-cas"
        / "sha256"
        / item.artifact_sha256[:2]
        / item.artifact_sha256
        / "files"
        / "SKILL.md"
    )
    target.write_bytes(b"tampered")
    with pytest.raises(Exception):
        runtime.read(snapshot_id, f"skill:{item.extension_id}@{item.revision_id}")


def _prepared_skill_runtime(tmp_path: Path):
    service = _service(tmp_path)
    _install_skill(
        service,
        extension_id="local.alpha-workflow",
        name="Alpha workflow",
        suffix="alpha:1",
    )
    _install_skill(
        service,
        extension_id="local.beta-workflow",
        name="Beta workflow",
        suffix="beta:1",
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            extension_service=service,
            installed_capability_packs=frozenset({"image"}),
            capability_handlers={
                "vision": lambda arguments: {"summary": arguments["instruction"]}
            },
            full_access=True,
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="Skill disclosure"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Use local.alpha-workflow for this office task",
            client_message_id="skill-disclosure-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=prepared.snapshot_context,
    )
    scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
    )
    return app, service, kernel, composition, thread, created, prepared, batch, scope


def test_enabled_skills_are_mentionable_and_structured_selection_is_audited(
    tmp_path: Path,
) -> None:
    app, _service_instance, _kernel, composition, *_rest = _prepared_skill_runtime(
        tmp_path
    )
    response = TestClient(app).get(
        "/api/v1/capability-mentions",
        headers={"Authorization": f"Bearer {app.state.runtime_bearer_token}"},
    )
    assert response.status_code == 200, response.text
    catalog = response.json()
    alpha = next(
        item
        for item in catalog["items"]
        if item["reference"] == "skill:local.alpha-workflow"
    )

    assert catalog["snapshot_id"].startswith("mention_")
    assert alpha["kind"] == "skill"
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="@local.alpha-workflow 核对这份材料",
            explicit_tool_ids=["skill:local.alpha-workflow"],
            client_message_id="structured-skill-mention",
        )
    )
    plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
        prepared.snapshot_context.capability_snapshot_id
    )

    assert prepared.request.explicit_tool_ids == ["skill:local.alpha-workflow"]
    assert "explicit_reference" in plan.decision("skill_search").reason_codes


def _skill_context(prepared, scope, tool_id: str) -> ToolInvocationContext:
    return ToolInvocationContext(
        invocation_id=f"invoke-{tool_id}",
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id=tool_id,
        idempotency_key=None,
        approved=False,
        effective_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
        execution_scope=scope,
    )


def _complete_skill_search(composition, created, prepared, batch, scope, *, query: str):
    context = _skill_context(prepared, scope, "skill_search")
    arguments = {"query": query, "limit": 10}
    result = asyncio.run(
        composition.capability_service.tool_call(
            context.capability_snapshot_id,
            "skill_search",
            arguments,
            policy_snapshot_id=context.policy_snapshot_id,
            execution_scope=scope,
        )
    ).value
    call_id = f"skill-search-{query.replace(' ', '-')}"
    composition.tool_execution_repository.begin(
        tool_call_id=call_id,
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="skill_search",
        arguments=arguments,
        idempotency_key=None,
    )
    composition.tool_execution_repository.complete(call_id, result)
    return result


def test_new_skill_is_discoverable_on_next_tool_round_without_new_turn(
    tmp_path: Path,
) -> None:
    (
        _app,
        service,
        _kernel,
        composition,
        _thread,
        created,
        prepared,
        batch,
        scope,
    ) = _prepared_skill_runtime(tmp_path)
    original_snapshot_id = batch.extension_snapshot_id

    _install_skill(
        service,
        extension_id="local.gamma-workflow",
        name="Gamma workflow",
        suffix="gamma:hot-install",
    )

    result = _complete_skill_search(
        composition,
        created,
        prepared,
        batch,
        scope,
        query="gamma",
    )
    assert result["extension_snapshot_id"] != original_snapshot_id
    assert [item["discovery_id"].split("@", 1)[0] for item in result["skills"]] == [
        "skill:local.gamma-workflow"
    ]


def test_skill_resource_grant_rejects_guess_cross_skill_and_cross_tool(
    tmp_path: Path,
) -> None:
    (
        _app,
        _service_value,
        _kernel,
        composition,
        _thread,
        created,
        prepared,
        batch,
        scope,
    ) = _prepared_skill_runtime(tmp_path)
    search = _complete_skill_search(
        composition,
        created,
        prepared,
        batch,
        scope,
        query="alpha",
    )
    assert search["schema_version"] == 1
    assert search["extension_snapshot_id"] == batch.extension_snapshot_id
    assert search["extension_contribution_snapshot_id"].startswith("extcontrib_")
    assert len(search["skills"]) == 1
    alpha_id = search["skills"][0]["discovery_id"]
    assert alpha_id.startswith("skill:local.alpha-workflow@extrev_")
    assert not any(
        forbidden in json.dumps(search)
        for forbidden in ("artifact_sha256", "SKILL.md", "references/", str(tmp_path))
    )

    all_skills = composition.skill_runtime._snapshot(batch.extension_snapshot_id).skills
    beta = next(
        item for item in all_skills if item.extension_id == "local.beta-workflow"
    )
    beta_id = f"skill:{beta.extension_id}@{beta.revision_id}"
    beta_reference = beta.references[0].reference_id

    result = asyncio.run(
        composition.capability_service.tool_call(
            prepared.snapshot_context.capability_snapshot_id,
            "skill_read",
            {"discovery_id": alpha_id},
            policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
            execution_scope=scope,
        )
    ).value
    assert result["discovery_id"] == alpha_id
    assert result["search_tool_call_id"] == "skill-search-alpha"
    assert len(result["search_result_sha256"]) == 64

    search_record = composition.tool_execution_repository.get("skill-search-alpha")
    real_resolver = composition.skill_runtime.search_fact_resolver
    composition.skill_runtime.search_fact_resolver = lambda *_arguments: (
        SkillSearchFact(
            tool_call_id=search_record.tool_call_id,
            arguments=search_record.arguments,
            result=search_record.result,
            result_sha256="0" * 64,
        )
    )
    with pytest.raises(ExtensionIntegrityError, match="digest"):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_read",
                {"discovery_id": alpha_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )
    composition.skill_runtime.search_fact_resolver = real_resolver

    for guessed in ("Alpha workflow", "local.alpha-workflow"):
        with pytest.raises(ExtensionNotFound):
            asyncio.run(
                composition.capability_service.tool_call(
                    prepared.snapshot_context.capability_snapshot_id,
                    "skill_read",
                    {"discovery_id": guessed},
                    policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                    execution_scope=scope,
                )
            )
    with pytest.raises(ExtensionNotFound):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_read",
                {"discovery_id": beta_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )
    with pytest.raises(ExtensionNotFound):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_read",
                {"discovery_id": alpha_id, "reference_ids": [beta_reference]},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )

    # A Skill content grant has no authority over Tool/MCP/Connector actions.
    assert not composition.tool_execution_repository.has_completed_disclosure(
        execution_scope=scope,
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id="vision",
        tool_version="1.0.0",
    )


def test_skill_run_requires_durable_read_and_rechecks_state(tmp_path: Path) -> None:
    (_app, service, _kernel, composition, _thread, created, prepared, batch, scope) = (
        _prepared_skill_runtime(tmp_path)
    )
    search = _complete_skill_search(
        composition, created, prepared, batch, scope, query="alpha"
    )
    discovery_id = search["skills"][0]["discovery_id"]

    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_run",
                {"discovery_id": discovery_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )

    read_arguments = {"discovery_id": discovery_id}
    read_result = asyncio.run(
        composition.capability_service.tool_call(
            prepared.snapshot_context.capability_snapshot_id,
            "skill_read",
            read_arguments,
            policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
            execution_scope=scope,
        )
    ).value
    composition.tool_execution_repository.begin(
        tool_call_id="skill-read-alpha",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id="skill_read",
        arguments=read_arguments,
        idempotency_key=None,
    )
    composition.tool_execution_repository.complete("skill-read-alpha", read_result)

    with pytest.raises(SkillNotExecutable):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_run",
                {"discovery_id": discovery_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )

    class NativeRunner:
        calls = []

        def supports(self, skill):
            return skill.extension_id == "local.alpha-workflow"

        async def run(self, skill, parameters, context, *, state_fence):
            state_fence()
            self.calls.append((skill.extension_id, parameters, context.tool_id))
            return {"native": True}

    native_runner = NativeRunner()
    composition.skill_runtime.bind_native_runner(native_runner)
    native_result = asyncio.run(
        composition.capability_service.tool_call(
            prepared.snapshot_context.capability_snapshot_id,
            "skill_run",
            {"discovery_id": discovery_id, "parameters": {"title": "report"}},
            policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
            execution_scope=scope,
        )
    ).value
    assert native_result == {
        "schema_version": 1,
        "discovery_id": discovery_id,
        "result": {"native": True},
    }
    assert native_runner.calls == [
        ("local.alpha-workflow", {"title": "report"}, "skill_run")
    ]

    projection = service.projection("local.alpha-workflow")
    service.disable(
        projection.extension_id,
        expected_revision=projection.revision,
        client_request_id="disable:alpha:state-fence",
    )
    with pytest.raises(SkillStateChanged):
        composition.skill_runtime.search(batch.extension_snapshot_id, "alpha")
    with pytest.raises(SkillStateChanged):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_run",
                {"discovery_id": discovery_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )
    reenabled = asyncio.run(
        service.enable(
            projection.extension_id,
            expected_revision=service.projection(projection.extension_id).revision,
            client_request_id="enable:alpha:state-fence",
        )
    )
    assert reenabled.status == "enabled"
    with pytest.raises(SkillStateChanged):
        composition.skill_runtime.search(batch.extension_snapshot_id, "alpha")
    refreshed = service.snapshot()
    assert SkillRuntime(service).search(refreshed.snapshot_id, "alpha")


def test_controlled_skill_runner_receives_only_frozen_declared_contract(
    tmp_path: Path,
) -> None:
    class Runner:
        request = None

        def supports(self, runtime: str) -> bool:
            return runtime == "python"

        async def run(self, request, *, state_fence):
            state_fence()
            self.request = request
            return ControlledSkillRunResult({"ok": True})

    runner = Runner()
    service = _service(tmp_path)
    service.bind_skill_runner(runner)
    runtime_manifest = json.dumps(
        {
            "schema_version": 1,
            "runtime": "python",
            "entrypoint": "scripts/main.py",
            "environment": [],
            "network_domains": [],
            "external_commands": [],
            "effects": ["read"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    staged = service.install_local_skill_zip(
        _zip(
            {
                "SKILL.md": _skill_payload("Controlled skill"),
                "skill-runtime.json": runtime_manifest,
                "scripts/main.py": b"print('runner-owned')\n",
            }
        ),
        extension_id="local.controlled-skill",
        expected_revision=0,
        client_request_id="install:controlled-skill:1",
    )
    asyncio.run(
        service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable:controlled-skill:1",
        )
    )
    snapshot = service.snapshot()
    discovery_id = SkillRuntime(service).search(
        snapshot.snapshot_id, "controlled"
    )[0].discovery_id
    read_result = SkillRuntime(service).read(snapshot.snapshot_id, discovery_id)
    read_result = {
        "extension_snapshot_id": snapshot.snapshot_id,
        "extension_contribution_snapshot_id": SkillRuntime(service)
        ._snapshot(snapshot.snapshot_id)
        .snapshot_id,
        **read_result,
    }
    canonical = json.dumps(
        read_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    scope = ToolExecutionScope("job", "thread", "turn", "batch")
    runtime = SkillRuntime(
        service,
        snapshot_resolver=lambda _scope: snapshot.snapshot_id,
        read_fact_resolver=lambda *_args: SkillReadFact(
            "read-call", {"discovery_id": discovery_id}, read_result,
            hashlib.sha256(canonical).hexdigest(),
        ),
        controlled_runner=runner,
    )
    context = ToolInvocationContext(
        invocation_id="run-call",
        capability_snapshot_id="capability-snapshot",
        policy_snapshot_id="policy-snapshot",
        tool_id="skill_run",
        idempotency_key=None,
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        execution_scope=scope,
    )
    result = asyncio.run(
        runtime.handlers()["skill_run"](
            {"discovery_id": discovery_id},
            context,
        )
    )
    assert result["result"] == {"ok": True}
    assert runner.request is not None
    assert runner.request.entrypoint == "scripts/main.py"
    assert runner.request.parameters == {}
    assert not hasattr(runner.request, "command")
    with pytest.raises(ValueError, match="not declared"):
        replace(runner.request, parameters={"document_id": "doc-1"})


def test_skill_search_fact_is_recomputed_and_batch_bound(tmp_path: Path) -> None:
    (
        _app,
        _service_value,
        kernel,
        composition,
        thread,
        created,
        prepared,
        batch,
        scope,
    ) = _prepared_skill_runtime(tmp_path)
    valid_projection = composition.skill_runtime.search_projection(
        batch.extension_snapshot_id,
        "alpha",
        explicit_names=("local.alpha-workflow",),
        limit=10,
    )
    forged_arguments = {"query": "no-such-skill", "limit": 10}
    composition.tool_execution_repository.begin(
        tool_call_id="forged-skill-search",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id="skill_search",
        arguments=forged_arguments,
        idempotency_key=None,
    )
    composition.tool_execution_repository.complete(
        "forged-skill-search",
        {**valid_projection, "query": forged_arguments["query"]},
    )
    alpha_id = valid_projection["skills"][0]["discovery_id"]
    with pytest.raises(ExtensionIntegrityError, match="recomputation"):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_read",
                {"discovery_id": alpha_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=scope,
            )
        )

    kernel.steer_turn(
        created.turn.turn_id,
        SteerTurnRequest(
            input="Use local.beta-workflow instead",
            client_message_id="skill-disclosure-second-batch",
        ),
    )
    second_batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=1,
        last_revision_ordinal=1,
        snapshot_context=prepared.snapshot_context,
    )
    second_scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=second_batch.batch_id,
    )
    with pytest.raises(CapabilityDeniedError, match="not been disclosed"):
        asyncio.run(
            composition.capability_service.tool_call(
                prepared.snapshot_context.capability_snapshot_id,
                "skill_read",
                {"discovery_id": alpha_id},
                policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
                execution_scope=second_scope,
            )
        )


def test_skill_search_and_read_are_worker_durable_and_restart_safe(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _install_skill(
        service,
        extension_id="local.restart-workflow",
        name="Restart workflow",
        suffix="restart:1",
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            extension_service=service,
            full_access=True,
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="Skill restart"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Use local.restart-workflow",
            client_message_id="skill-restart-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )

    class SkillGateway:
        def __init__(self) -> None:
            self.round = 0
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            self.round += 1
            if self.round == 1:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="skill-search-response",
                    tool_call_id="skill-search-call",
                    tool_name="skill_search",
                    arguments={"query": "restart", "limit": 5},
                )
            elif self.round == 2:
                discovery_id = request.tool_outputs[0].output["skills"][0][
                    "discovery_id"
                ]
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="skill-read-response",
                    tool_call_id="skill-read-call",
                    tool_name="skill_read",
                    arguments={"discovery_id": discovery_id},
                )
            else:
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id="skill-final-response",
                    delta="Workflow loaded.",
                )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id="skill-final-response",
                )

    gateway = SkillGateway()
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        extension_fence=composition.extension_invocation_fence,
    )
    outcome = asyncio.run(worker.run_once("skill-worker"))
    assert outcome.outcome is WorkerOutcome.COMPLETED
    assert "skill_read" in gateway.requests[0].deferred_tool_ids
    assert "skill_read" in gateway.requests[1].disclosed_tool_ids
    read_output = gateway.requests[2].tool_outputs[0].output
    assert read_output["search_tool_call_id"].startswith("tool_exec_")
    assert len(read_output["search_result_sha256"]) == 64

    with kernel.database.reader() as connection:
        row = connection.execute(
            "SELECT execution_batch_id FROM tool_executions "
            "WHERE job_id = ? AND tool_id = 'skill_search' AND status = 'completed'",
            (created.job.job_id,),
        ).fetchone()
    assert row is not None
    execution_batch_id = str(row["execution_batch_id"])
    records = composition.tool_execution_repository.completed_for_job(
        created.job.job_id,
        execution_batch_id=execution_batch_id,
        tool_ids=("skill_search", "skill_read"),
    )
    assert [record.tool_id for record in records] == ["skill_search", "skill_read"]

    # A fresh Runtime/Skill service reconstructs both the generic disclosure
    # and the exact resource link from durable ToolExecution facts.
    restarted_service = _service(tmp_path)
    restarted_app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            extension_service=restarted_service,
            full_access=True,
        )
    )
    restarted = restarted_app.state.runtime_composition
    scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=execution_batch_id,
    )
    replayed = asyncio.run(
        restarted.capability_service.tool_call(
            prepared.snapshot_context.capability_snapshot_id,
            "skill_read",
            {"discovery_id": read_output["discovery_id"]},
            policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
            execution_scope=scope,
        )
    ).value
    assert replayed["search_tool_call_id"] == read_output["search_tool_call_id"]
    assert replayed["search_result_sha256"] == read_output["search_result_sha256"]


def _mcp_manifest(
    *, transport: ExtensionTransport = ExtensionTransport.STREAMABLE_HTTP
):
    digest = "a" * 64
    boundary = (
        RuntimeBoundary.PROCESS
        if transport is ExtensionTransport.STDIO
        else RuntimeBoundary.MANAGED_ADAPTER
    )
    manifest = ExtensionManifest(
        schema_version=1,
        contract_version=EXTENSION_CONTRACT_VERSION,
        extension_id="ecorex.mcp.office",
        version="1.0.0",
        kind=ExtensionKind.MCP_SERVER,
        display_name="Office MCP",
        description="Verified office MCP provider.",
        artifact_sha256=digest,
        source=ExtensionSource.CORE_BUNDLE,
        trust=ExtensionTrust.BUILTIN,
        runtime_boundary=boundary,
        transport=transport,
        compatibility=ExtensionCompatibility(
            runtime_api="=1.0.0", platforms=(), architectures=()
        ),
        dependencies=(),
        conflicts=(),
        exports=(
            ExtensionExport(
                export_id="ecorex.mcp.office",
                kind=ExtensionExportKind.MCP_SERVER,
                exposure=ExtensionExposure.DEFERRED,
                permission_effects=("network", "read"),
            ),
        ),
        supported_protocol_versions=("2025-11-25",),
        upstream_metadata=None,
        signature=ExtensionSignature(
            algorithm="core-slot-sha256", key_id="core-slot-v1", value=digest
        ),
    )
    return manifest, verify_core_extension(
        manifest,
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
    )


def _tool(
    *,
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY,
) -> MCPToolContract:
    return MCPToolContract(
        name="lookup",
        description="Look up one office record.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
        idempotency=idempotency,
    )


def test_mcp_contract_reserves_system_tags_and_requires_egress_approval() -> None:
    contract = _tool()
    assert contract.approval_requirement is ApprovalRequirement.ON_REQUEST

    with pytest.raises(ValueError, match="must enter Runtime as deferred"):
        MCPToolContract(
            name="unsafe_direct",
            description="A provider cannot self-promote into every model request.",
            input_schema={"type": "object"},
            exposure=Exposure.DIRECT,
        )

    with pytest.raises(ValueError, match="data egress"):
        MCPToolContract(
            name="unsafe_egress",
            description="Read remote data without an approval boundary.",
            input_schema={"type": "object"},
            effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
            approval_requirement=ApprovalRequirement.NEVER,
        )

    tags = frozenset(f"office-tag-{index}" for index in range(30))
    bounded = MCPToolContract(
        name="bounded_tags",
        description="Use all extension-owned search tag slots.",
        input_schema={"type": "object"},
        intent_tags=tags,
    )
    assert (
        len(
            bounded.to_tool_spec(
                "ecorex.mcp.office",
                "1.0.0",
                provider=_test_mcp_provider("ecorex.mcp.office"),
            ).intent_tags
        )
        == 32
    )
    with pytest.raises(ValueError, match="reserved Runtime slots"):
        MCPToolContract(
            name="too_many_tags",
            description="Attempt to consume Runtime-owned tag slots.",
            input_schema={"type": "object"},
            intent_tags=tags | {"one-tag-too-many"},
        )


def test_mcp_contract_rejects_untrusted_patterns_and_unsafe_text() -> None:
    # Reviewed Core schemas retain the bounded pattern subset.
    validate_schema_contract(
        {"type": "string", "pattern": r"^[a-z]+$"},
        label="Core reviewed pattern",
    )
    with pytest.raises(SchemaContractError, match="forbidden keyword 'pattern'"):
        MCPToolContract(
            name="regex_from_provider",
            description="Untrusted regular expression.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "pattern": r"(a+)+$"}},
            },
        )
    with pytest.raises(ValueError, match="unsafe control"):
        MCPToolContract(
            name="bidi_description",
            description="Safe prefix\u202eunsafe suffix",
            input_schema={"type": "object"},
        )
    with pytest.raises(ValueError, match="description is invalid"):
        MCPToolContract(
            name="oversized_description",
            description="图" * 1_366,
            input_schema={"type": "object"},
        )
    with pytest.raises(SchemaContractError, match="schema size limit"):
        MCPToolContract(
            name="oversized_schema",
            description="Schema bytes are bounded independently of messages.",
            input_schema={
                "type": "object",
                "properties": {
                    f"field_{index}": {
                        "type": "string",
                        "description": "x" * 2_000,
                    }
                    for index in range(40)
                },
            },
        )
    with pytest.raises(ValueError, match="intent tag.*unsafe control"):
        MCPToolContract(
            name="bidi_tag",
            description="Safe description.",
            input_schema={"type": "object"},
            intent_tags=frozenset({"office\u2066spoof"}),
        )

    normalized = MCPToolContract(
        name="normalized_text",
        description="  First line\r\nSecond\tline  ",
        input_schema={"type": "object"},
    )
    assert normalized.description == "First line\nSecond line"


class _FakeSession:
    transport_kind = ExtensionTransport.STREAMABLE_HTTP

    def __init__(self, tool: MCPToolContract, *, behavior: str = "ok") -> None:
        self.tool = tool
        self.behavior = behavior
        self.methods: list[str] = []
        self.messages: list[Mapping[str, Any]] = []
        self.closed = False
        self._first_id: Any = None
        self.block = asyncio.Event()

    async def exchange(self, message, *, timeout_seconds, max_response_bytes):
        method = message["method"]
        self.methods.append(method)
        self.messages.append(message)
        if self._first_id is None:
            self._first_id = message["id"]
        if self.behavior == "initialize_crash" and method == "initialize":
            raise MCPTransportError("mcp_process_crashed", retryable=True)
        if self.behavior == "list_timeout" and method == "tools/list":
            raise TimeoutError
        if self.behavior == "timeout" and method == "tools/call":
            raise TimeoutError
        if self.behavior == "crash" and method == "tools/call":
            raise MCPTransportError("mcp_process_crashed", retryable=True)
        if self.behavior == "block" and method == "tools/call":
            await self.block.wait()
        response_id = message["id"]
        if self.behavior == "duplicate_id" and method == "tools/list":
            response_id = self._first_id
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0.0"},
            }
            if self.behavior == "initialize_metadata":
                result["serverInfo"]["title"] = "Unsigned server title"
        elif method == "tools/list":
            if self.behavior == "paged" and not message.get("params", {}).get("cursor"):
                result = {"tools": [], "nextCursor": "page-2"}
            else:
                descriptor = self.tool.expected_list_item()
                if self.behavior == "catalog_metadata":
                    descriptor["_meta"] = {"trusted": True}
                elif self.behavior == "catalog_mismatch":
                    descriptor["description"] = (
                        "A different but syntactically safe tool."
                    )
                elif self.behavior == "catalog_control":
                    descriptor["description"] += "\u202e"
                elif self.behavior == "catalog_pattern":
                    descriptor["inputSchema"] = {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "pattern": r"(a+)+$"}
                        },
                    }
                result = {"tools": [descriptor]}
                if self.behavior == "list_metadata":
                    result["_meta"] = {"page": "unsigned"}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            raise AssertionError(method)
        response = {"jsonrpc": "2.0", "id": response_id, "result": result}
        if self.behavior == "malformed" and method == "tools/list":
            response.pop("jsonrpc")
        return response

    async def notify(self, message, *, timeout_seconds):
        self.methods.append(message["method"])

    async def close(self):
        self.closed = True


def _mcp_setup(
    tmp_path: Path,
    factory,
    *,
    threshold: int = 3,
    tool: MCPToolContract | None = None,
):
    service = _service(tmp_path)
    manifest, verified = _mcp_manifest()
    service.register_runtime_bound(verified)
    tool = tool or _tool()
    binding = MCPRuntimeBinding(
        extension_id=manifest.extension_id,
        revision_id=manifest.revision_id,
        artifact_sha256=manifest.artifact_sha256,
        transport=manifest.transport,
        tools=(tool,),
        verified_manifest=verified,
        session_factory=factory,
        request_timeout_seconds=0.1,
    )
    supervisor = MCPClientSupervisor(
        service,
        (binding,),
        failure_threshold=threshold,
        circuit_seconds=30,
    )
    return service, binding, supervisor, service.snapshot().snapshot_id


def test_mcp_handshake_list_call_namespace_and_tenant_isolation(tmp_path: Path) -> None:
    sessions: list[tuple[str, _FakeSession]] = []

    def factory(tenant: str):
        session = _FakeSession(_tool())
        sessions.append((tenant, session))
        return session

    service, binding, supervisor, snapshot_id = _mcp_setup(tmp_path, factory)

    async def scenario():
        first = await supervisor.call(
            snapshot_id, binding, _tool(), {"query": "a"}, tenant_id="tenant-a"
        )
        second = await supervisor.call(
            snapshot_id, binding, _tool(), {"query": "b"}, tenant_id="tenant-b"
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first["content"][0]["text"] == second["content"][0]["text"] == "ok"
    assert [tenant for tenant, _ in sessions] == ["tenant-a", "tenant-b"]
    assert sessions[0][1].methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    spec = supervisor.tool_specs()[0]
    assert spec.tool_id == "mcp.ecorex.mcp.office:lookup"
    record = supervisor.contribution_records(snapshot_id)[0]
    assert record.revision_id == binding.revision_id
    assert record.tool_ids == (spec.tool_id,)
    assert len(record.export_digest) == len(record.tool_catalog_digest) == 64

    projection = service.projection(binding.extension_id)
    service.disable(
        binding.extension_id,
        expected_revision=projection.revision,
        client_request_id="disable:mcp:1",
    )
    with pytest.raises(ExtensionProviderRevoked):
        asyncio.run(
            supervisor.call(
                snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant-c"
            )
        )


@pytest.mark.parametrize(
    ("behavior", "error"),
    [
        ("duplicate_id", MCPProtocolError),
        ("malformed", MCPProtocolError),
        ("timeout", MCPTransportError),
    ],
)
def test_mcp_rejects_duplicate_malformed_and_timeout(
    tmp_path: Path,
    behavior: str,
    error: type[Exception],
) -> None:
    service, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path,
        lambda _tenant: _FakeSession(_tool(), behavior=behavior),
    )
    with pytest.raises(error):
        asyncio.run(
            supervisor.call(
                snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
            )
        )


@pytest.mark.parametrize(
    "behavior",
    [
        "initialize_metadata",
        "catalog_metadata",
        "catalog_mismatch",
        "catalog_control",
        "catalog_pattern",
        "list_metadata",
    ],
)
def test_mcp_rejects_unsigned_or_unsafe_dynamic_catalog_fields(
    tmp_path: Path,
    behavior: str,
) -> None:
    service, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path,
        lambda _tenant: _FakeSession(_tool(), behavior=behavior),
    )
    with pytest.raises(MCPProtocolError):
        asyncio.run(
            supervisor.call(
                snapshot_id,
                binding,
                _tool(),
                {"query": "x"},
                tenant_id="tenant",
            )
        )


def test_mcp_crash_restarts_then_circuit_opens(tmp_path: Path) -> None:
    created = 0
    restarted_sessions: list[_FakeSession] = []

    def restart_factory(_tenant: str):
        nonlocal created
        created += 1
        session = _FakeSession(_tool(), behavior="crash" if created == 1 else "ok")
        restarted_sessions.append(session)
        return session

    _, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path / "restart", restart_factory
    )
    result = asyncio.run(
        supervisor.call(
            snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
        )
    )
    assert result["content"][0]["text"] == "ok"
    assert created == 2
    assert (
        sum(session.methods.count("tools/call") for session in restarted_sessions) == 2
    )

    _, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path / "circuit",
        lambda _tenant: _FakeSession(_tool(), behavior="crash"),
        threshold=1,
    )
    with pytest.raises(MCPTransportError, match="mcp_process_crashed"):
        asyncio.run(
            supervisor.call(
                snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
            )
        )
    with pytest.raises(MCPTransportError, match="mcp_circuit_open"):
        asyncio.run(
            supervisor.call(
                snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
            )
        )


@pytest.mark.parametrize("first_behavior", ["initialize_crash", "list_timeout"])
def test_mcp_handshake_and_catalog_retry_never_replays_tool_call(
    tmp_path: Path,
    first_behavior: str,
) -> None:
    """Pre-invocation protocol recovery is safe for every idempotency class."""

    tool = _tool(idempotency=IdempotencyClass.NON_IDEMPOTENT)
    sessions: list[_FakeSession] = []

    def factory(_tenant: str):
        session = _FakeSession(
            tool,
            behavior=first_behavior if not sessions else "ok",
        )
        sessions.append(session)
        return session

    _, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path,
        factory,
        tool=tool,
    )
    result = asyncio.run(
        supervisor.call(
            snapshot_id,
            binding,
            tool,
            {"query": "x"},
            tenant_id="tenant",
            idempotency_key="stable-non-idempotent-key",
        )
    )

    assert result["content"][0]["text"] == "ok"
    assert len(sessions) == 2
    assert sum(session.methods.count("tools/call") for session in sessions) == 1


@pytest.mark.parametrize("with_key", [True, False])
def test_mcp_idempotent_tool_retries_only_with_nonempty_key(
    tmp_path: Path,
    with_key: bool,
) -> None:
    tool = _tool(idempotency=IdempotencyClass.IDEMPOTENT)
    sessions: list[_FakeSession] = []

    def factory(_tenant: str):
        session = _FakeSession(
            tool,
            behavior="crash" if not sessions else "ok",
        )
        sessions.append(session)
        return session

    _, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path,
        factory,
        tool=tool,
    )
    call = supervisor.call(
        snapshot_id,
        binding,
        tool,
        {"query": "x"},
        tenant_id="tenant",
        idempotency_key="stable-idempotency-key" if with_key else "  ",
    )

    if with_key:
        result = asyncio.run(call)
        assert result["content"][0]["text"] == "ok"
        assert len(sessions) == 2
        call_messages = [
            message
            for session in sessions
            for message in session.messages
            if message["method"] == "tools/call"
        ]
        assert len(call_messages) == 2
        assert {
            message["params"]["_meta"]["com.ecorex/idempotency-key"]
            for message in call_messages
        } == {"stable-idempotency-key"}
    else:
        with pytest.raises(MCPTransportError) as failure:
            asyncio.run(call)
        assert failure.value.retryable is False
        assert len(sessions) == 1
        assert sessions[0].methods.count("tools/call") == 1
        tool_call = next(
            message
            for message in sessions[0].messages
            if message["method"] == "tools/call"
        )
        assert "_meta" not in tool_call["params"]


@pytest.mark.parametrize("behavior", ["crash", "timeout"])
def test_mcp_non_idempotent_transport_uncertainty_calls_provider_once(
    tmp_path: Path,
    behavior: str,
) -> None:
    tool = _tool(idempotency=IdempotencyClass.NON_IDEMPOTENT)
    sessions: list[_FakeSession] = []

    def factory(_tenant: str):
        session = _FakeSession(tool, behavior=behavior)
        sessions.append(session)
        return session

    _, binding, supervisor, snapshot_id = _mcp_setup(
        tmp_path,
        factory,
        tool=tool,
    )
    with pytest.raises(MCPTransportError) as failure:
        asyncio.run(
            supervisor.call(
                snapshot_id,
                binding,
                tool,
                {"query": "x"},
                tenant_id="tenant",
                idempotency_key="non-idempotent-attempt-key",
            )
        )

    # The Durable Worker sees a non-retryable transport uncertainty and its
    # NON_IDEMPOTENT fence persists the execution for explicit HITL review.
    assert failure.value.retryable is False
    assert len(sessions) == 1
    assert sessions[0].methods.count("tools/call") == 1


def test_mcp_tools_list_pagination_is_bounded_and_exact(tmp_path: Path) -> None:
    session = _FakeSession(_tool(), behavior="paged")
    _, binding, supervisor, snapshot_id = _mcp_setup(tmp_path, lambda _tenant: session)
    result = asyncio.run(
        supervisor.call(
            snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
        )
    )
    assert result["content"][0]["text"] == "ok"
    assert session.methods.count("tools/list") == 2


def test_mcp_cancellation_closes_session(tmp_path: Path) -> None:
    session = _FakeSession(_tool(), behavior="block")
    _, binding, supervisor, snapshot_id = _mcp_setup(tmp_path, lambda _tenant: session)

    async def scenario():
        task = asyncio.create_task(
            supervisor.call(
                snapshot_id, binding, _tool(), {"query": "x"}, tenant_id="tenant"
            )
        )
        while "tools/call" not in session.methods:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert session.closed is True
    assert "notifications/cancelled" in session.methods


def test_stdio_transport_uses_true_jsonrpc_subprocess(tmp_path: Path) -> None:
    script = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"subprocess","version":"1"}}
    elif method == "tools/list":
        result = {"tools":[{"name":"lookup","description":"Look up one office record.","inputSchema":{"type":"object","properties":{"query":{"type":"string","minLength":1}},"required":["query"],"additionalProperties":False},"outputSchema":{"type":"object"}}]}
    else:
        result = {"content":[{"type":"text","text":"subprocess-ok"}]}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}, separators=(",",":")), flush=True)
"""

    async def scenario():
        service = _service(tmp_path)
        manifest, verified = _mcp_manifest(transport=ExtensionTransport.STDIO)
        service.register_runtime_bound(verified)

        async def factory(_tenant: str):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-u",
                "-c",
                script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return MCPStdioTransport(process)

        binding = MCPRuntimeBinding(
            extension_id=manifest.extension_id,
            revision_id=manifest.revision_id,
            artifact_sha256=manifest.artifact_sha256,
            transport=manifest.transport,
            tools=(_tool(),),
            verified_manifest=verified,
            session_factory=factory,
        )
        supervisor = MCPClientSupervisor(service, (binding,))
        response = await supervisor.call(
            service.snapshot().snapshot_id,
            binding,
            _tool(),
            {"query": "x"},
            tenant_id="tenant",
        )
        await supervisor.close()
        return response

    response = asyncio.run(scenario())
    assert response["content"][0]["text"] == "subprocess-ok"


def test_managed_http_transport_runs_true_protocol_and_pins_session(
    tmp_path: Path,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202, headers={"MCP-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fake", "version": "1.0.0"},
            }
        elif payload["method"] == "tools/list":
            result = {"tools": [_tool().expected_list_item()]}
        elif payload["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "http-ok"}]}
        else:
            raise AssertionError(payload)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "MCP-Session-Id": "session-1"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = _service(tmp_path)
        manifest, verified = _mcp_manifest()
        service.register_runtime_bound(verified)
        binding = MCPRuntimeBinding(
            extension_id=manifest.extension_id,
            revision_id=manifest.revision_id,
            artifact_sha256=manifest.artifact_sha256,
            transport=manifest.transport,
            tools=(_tool(),),
            verified_manifest=verified,
            session_factory=lambda _tenant: ManagedHTTPMCPTransport(
                "https://mcp.example.test/v1/session",
                client=client,
                expected_host="mcp.example.test",
            ),
        )
        supervisor = MCPClientSupervisor(service, (binding,))
        result = await supervisor.call(
            service.snapshot().snapshot_id,
            binding,
            _tool(),
            {"query": "record"},
            tenant_id="tenant",
        )
        await supervisor.close()
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result["content"][0]["text"] == "http-ok"
    assert {str(request.url) for request in observed} == {
        "https://mcp.example.test/v1/session"
    }
    assert observed[0].headers["MCP-Protocol-Version"] == "2025-11-25"
    assert "MCP-Session-Id" not in observed[0].headers
    assert observed[1].headers["MCP-Session-Id"] == "session-1"
    assert [json.loads(request.content).get("method") for request in observed[:-1]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert observed[-1].method == "DELETE"


def test_agent_turn_worker_discovers_and_invokes_namespaced_mcp_tool(
    tmp_path: Path,
) -> None:
    session = _FakeSession(_tool())
    service, binding, _unused, _snapshot = _mcp_setup(
        tmp_path,
        lambda _tenant: session,
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            extension_service=service,
            mcp_runtime_bindings=(binding,),
            full_access=True,
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="MCP worker"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Use the office MCP lookup tool for this record",
            client_message_id="mcp-worker-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )

    class Gateway:
        def __init__(self) -> None:
            self.round = 0
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            self.round += 1
            if self.round == 1:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="mcp-search-response",
                    tool_call_id="mcp-search-call",
                    tool_name="tool_search",
                    arguments={"query": "office lookup", "limit": 5},
                )
            elif self.round == 2:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="mcp-describe-response",
                    tool_call_id="mcp-describe-call",
                    tool_name="tool_describe",
                    arguments={
                        "discovery_id": "tool:mcp.ecorex.mcp.office:lookup@1.0.0"
                    },
                )
            elif self.round == 3:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id="mcp-tool-response",
                    tool_call_id="mcp-call-1",
                    tool_name="mcp.ecorex.mcp.office:lookup",
                    arguments={"query": "quarterly"},
                )
            else:
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id="mcp-response-2",
                    delta="已读取办公记录。",
                )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id="mcp-response-2",
                )

    gateway = Gateway()
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        extension_fence=composition.extension_invocation_fence,
    )
    result = asyncio.run(worker.run_once("mcp-worker"))
    assert result.outcome is WorkerOutcome.COMPLETED
    assert kernel.jobs.get(created.job.job_id).status.value == "completed"
    assert "mcp.ecorex.mcp.office:lookup" in gateway.requests[0].deferred_tool_ids
    search_output = gateway.requests[1].tool_outputs[0].output
    assert search_output["tools"][0]["discovery_id"] == (
        "tool:mcp.ecorex.mcp.office:lookup@1.0.0"
    )
    assert gateway.requests[2].disclosed_tool_ids == ["mcp.ecorex.mcp.office:lookup"]
    assert gateway.requests[3].tool_outputs[0].output["content"][0]["text"] == "ok"
    tool_item = next(
        item
        for item in kernel.projection(thread.thread_id).items
        if item.kind is ItemKind.TOOL_CALL
        and item.content.get("tool_name") == "mcp.ecorex.mcp.office:lookup"
    )
    assert tool_item.content["display_label"] == "使用已连接的应用"
    assert tool_item.content["result_summary"] == "已完成应用操作"
    assert tool_item.content["result_sha256"]
    assert "result" not in tool_item.content
