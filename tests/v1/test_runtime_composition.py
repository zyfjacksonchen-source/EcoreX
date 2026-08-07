from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ecorex.capabilities import (
    CapabilitySnapshotRepository,
    Exposure,
    ManagedModelCatalog,
    ManagedModelSpec,
    ModelModality,
    RuntimeAvailability,
    UnknownModelError,
)
from ecorex.connectors import InMemoryCredentialVault
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import (
    AgentTurnWorker,
    RuntimeComposition,
    RuntimeSettings,
    RuntimeSnapshotConflict,
    RuntimeSnapshotRepository,
    ToolExecutionRepository,
    create_app,
)


TOKEN = "r" * 32
CSRF = "c" * 32
ORIGIN = "http://testserver"


def _client(tmp_path):
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            installed_capability_packs=frozenset({"image", "ocr", "sandbox"}),
            capability_handlers={
                "imagegen": lambda arguments, context: {"ok": True},
                "vision": lambda arguments: {"ok": True},
                "shell": lambda arguments, context: {"exit_code": 0},
            },
        )
    )
    return app, TestClient(app)


def _headers(*, mutation: bool = False):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if mutation:
        headers.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF})
    return headers


def test_bootstrap_and_turns_are_generated_from_backend_catalogs(tmp_path) -> None:
    app, client = _client(tmp_path)
    assert isinstance(
        app.state.connector_composition.service.vault,
        InMemoryCredentialVault,
    )
    bootstrap = client.get("/api/v1/bootstrap", headers=_headers()).json()

    assert bootstrap["models"]["snapshot_id"].startswith("models_")
    assert bootstrap["models"]["chat"][0]["model_id"] == "ecorex-chat"
    assert bootstrap["models"]["chat"][0]["display_name"] == ("GPT-5.6 Luna · 最大推理")
    assert bootstrap["models"]["chat"][0]["model_policy"] == {
        "schema_version": 1,
        "policy_id": "ecorex-chat-gpt-5.6-luna",
        "policy_version": "1.2.0",
        "local_model_id": "ecorex-chat",
        "upstream_model_id": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "context_management": {
            "type": "compaction",
            "compact_threshold_tokens": 272_000,
        },
    }
    assert bootstrap["models"]["image"][0]["model_id"] == "gpt-image-2"
    assert bootstrap["models"]["image"][0]["capabilities"] == [
        "image-edit",
        "image-generation",
    ]
    connectors = {item["connector_id"]: item for item in bootstrap["connectors"]}
    assert connectors["feishu"]["tier"] == "stable"
    assert connectors["tencent-docs"]["contract_version"] == "1.0"
    assert connectors["dingtalk"]["tier"] == "beta"
    assert connectors["feishu"]["adapter_available"] is False

    thread = client.post(
        "/api/v1/threads", json={}, headers=_headers(mutation=True)
    ).json()
    created = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={
            "input": "read the reference and then 改图 with bash if needed",
            "agent_model_id": "ecorex-chat",
            "image_model_id": "image2",
            "client_message_id": "catalog-turn",
        },
        headers=_headers(mutation=True),
    )
    assert created.status_code == 202
    body = created.json()
    assert body["turn"]["agent_model_id"] == "ecorex-chat"
    assert body["turn"]["image_model_id"] == "gpt-image-2"
    assert "payload" not in body["job"]
    assert "checkpoint" not in body["job"]
    assert "lease_token" not in body["job"]

    events = client.get(
        f"/api/v1/threads/{thread['thread_id']}/events",
        headers=_headers(),
    ).json()["events"]
    turn_events = [event for event in events if event["turn_id"]]
    assert turn_events
    context = {
        (
            event["config_snapshot_id"],
            event["capability_snapshot_id"],
            event["permission_snapshot_id"],
        )
        for event in turn_events
    }
    assert len(context) == 1
    config_id, capability_id, permission_id = context.pop()
    assert config_id and capability_id and permission_id
    assert permission_id == bootstrap["permissions"]["snapshot_id"]

    plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(capability_id)
    decisions = {decision.tool_id: decision for decision in plan.decisions}
    assert set(decisions) == {
        "read",
        "fetch",
        "vision",
        "ocr",
        "cdp",
        "shell",
        "imagegen",
        "skill_search",
        "skill_read",
        "skill_run",
        "tool_search",
        "tool_describe",
        "connector_search",
        "connector_describe",
        "connector_read",
        "connector_write",
        "artifact_read",
            "input_attachment_read",
            "task_list",
        }
    assert decisions["imagegen"].eligible is True
    assert not any(
        reason.startswith("missing_model_capabilities:")
        for reason in decisions["imagegen"].reason_codes
    )
    assert plan.selected_model_capabilities == {
        "chat": frozenset({"chat", "reasoning", "tools", "vision"}),
        "image": frozenset({"image-edit", "image-generation"}),
    }
    assert decisions["read"].eligible is True
    # The trusted attachment reader remains discoverable for ordinary Turns;
    # only a Turn with backend-bound uploads promotes it to direct exposure.
    assert decisions["input_attachment_read"].eligible is True
    assert decisions["input_attachment_read"].exposure is Exposure.DEFERRED
    assert (
        "runtime_context_required"
        not in decisions["input_attachment_read"].reason_codes
    )
    assert decisions["shell"].eligible is True
    assert {decision.tool_id for decision in plan.direct} >= {
        "read",
        "shell",
        "imagegen",
        "tool_search",
        "tool_describe",
    }
    assert plan.direct[0].tool_id == "shell"
    assert decisions["imagegen"].exposure is Exposure.DIRECT
    snapshots = RuntimeSnapshotRepository(tmp_path / "runtime.db")
    config = snapshots.get(config_id)
    assert config.kind == "config"
    assert config.payload["agent_model_id"] == "ecorex-chat"
    assert config.payload["image_model_id"] == "gpt-image-2"
    assert config.payload["availability"]["selected_model_capabilities"] == {
        "chat": ["chat", "reasoning", "tools", "vision"],
        "image": ["image-edit", "image-generation"],
    }
    assert snapshots.get(permission_id).kind == "permission"
    assert snapshots.get(bootstrap["models"]["snapshot_id"]).kind == "models"
    frozen_models = snapshots.get(bootstrap["models"]["snapshot_id"])
    assert (
        frozen_models.payload["modalities"]["chat"][0]["model_policy"]
        == (bootstrap["models"]["chat"][0]["model_policy"])
    )
    assert (
        frozen_models.payload["modalities"]["image"][0]["capabilities"]
        == (bootstrap["models"]["image"][0]["capabilities"])
    )
    assert (
        app.state.runtime_composition.model_catalog.snapshot_id
        == bootstrap["models"]["snapshot_id"]
    )


def test_bound_image_attachment_promotes_reader_vision_and_ocr(tmp_path) -> None:
    app, client = _client(tmp_path)
    uploaded = client.post(
        "/api/v1/input-attachments",
        headers=_headers(mutation=True),
        files={"file": ("screen.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")},
        data={"client_request_id": "image-attachment-runtime-direct"},
    )
    assert uploaded.status_code == 201
    thread = client.post(
        "/api/v1/threads", json={}, headers=_headers(mutation=True)
    ).json()
    turn = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=_headers(mutation=True),
        json={
            "input": "请识别这张图片里的内容",
            "attachment_ids": [uploaded.json()["attachment_id"]],
            "client_message_id": "image-attachment-message",
        },
    )
    assert turn.status_code == 202
    events = client.get(
        f"/api/v1/threads/{thread['thread_id']}/events", headers=_headers()
    ).json()["events"]
    snapshot_id = next(
        event["capability_snapshot_id"]
        for event in events
        if event.get("turn_id") == turn.json()["turn"]["turn_id"]
    )
    plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(snapshot_id)
    for tool_id in ("input_attachment_read", "vision", "ocr"):
        decision = plan.decision(tool_id)
        assert decision is not None and decision.eligible
        assert decision.exposure is Exposure.DIRECT
        assert "runtime_context_required" in decision.reason_codes


def test_image_followup_requires_durable_success_in_the_same_thread(tmp_path) -> None:
    app, _client_instance = _client(tmp_path)
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="image context"))

    without_context = composition.prepare_turn(
        CreateTurnRequest(input="再来一张", client_message_id="no-image-context"),
        thread_id=thread.thread_id,
    )
    without_plan = composition.capability_service.get_plan(
        without_context.snapshot_context.capability_snapshot_id
    )
    assert without_plan.decision("imagegen").exposure is Exposure.DEFERRED

    prepared = composition.prepare_turn(
        CreateTurnRequest(input="生成一张海报", client_message_id="image-context"),
        thread_id=thread.thread_id,
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=object(),
        capabilities=composition.capability_service,
    )
    context = worker._job_context(created.job.job_id)
    with kernel.jobs.control_transaction(
        scope="test_image_context", subject=created.job.job_id
    ) as connection:
        batch = kernel.turn_execution_batches.create_in_transaction(
            connection,
            turn_id=created.turn.turn_id,
            first_revision_ordinal=0,
            last_revision_ordinal=0,
            snapshot_context=worker._snapshot_context(context),
        )
        connection.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=?",
            (created.turn.turn_id,),
        )
    executions = ToolExecutionRepository(kernel.database)
    executions.begin(
        tool_call_id="image-context-call",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context["capability_snapshot_id"],
        policy_snapshot_id=context["permission_snapshot_id"],
        tool_id="imagegen",
        arguments={"instruction": "海报"},
        idempotency_key="image-context-call",
    )
    executions.complete("image-context-call", {"artifact_id": "artifact-image-1"})

    inherited = composition.prepare_turn(
        CreateTurnRequest(input="再来一张", client_message_id="has-image-context"),
        thread_id=thread.thread_id,
    )
    inherited_plan = composition.capability_service.get_plan(
        inherited.snapshot_context.capability_snapshot_id
    )
    imagegen = inherited_plan.decision("imagegen")
    assert imagegen is not None and imagegen.eligible
    assert imagegen.exposure is Exposure.DIRECT
    assert "runtime_context_required" in imagegen.reason_codes

    negated = composition.prepare_turn(
        CreateTurnRequest(
            input="不要再来一张，改为总结刚才的过程",
            client_message_id="negated-image-context",
        ),
        thread_id=thread.thread_id,
    )
    negated_plan = composition.capability_service.get_plan(
        negated.snapshot_context.capability_snapshot_id
    )
    assert negated_plan.decision("imagegen").exposure is Exposure.DEFERRED


def test_projection_only_composition_does_not_publish_execution_authority(
    tmp_path,
) -> None:
    source_app, _client_instance = _client(tmp_path / "source")
    source = source_app.state.runtime_composition
    database = tmp_path / "projection-only.db"
    RuntimeSnapshotRepository(database)

    composition = RuntimeComposition(
        database_path=str(database),
        product_version="1.0.0",
        permission_snapshot_id=source.permission_snapshot.snapshot_id,
        permission_payload=source.permission_snapshot.payload,
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset(),
        connected_connectors=frozenset(),
        online=True,
        model_catalog=source.model_catalog,
        persist_startup_snapshots=False,
    )

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM runtime_snapshots").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM extension_catalog_snapshots"
            ).fetchone()[0]
            == 0
        )
    assert composition.model_snapshot.snapshot_id == source.model_catalog.snapshot_id
    with pytest.raises(RuntimeError, match="projection-only"):
        composition.prepare_turn(
            CreateTurnRequest(
                input="不得在只读模式创建新会话轮次",
                client_message_id="projection-only-turn",
            )
        )


def test_core_runtime_availability_normalizes_only_handler_absence() -> None:
    composition = object.__new__(RuntimeComposition)
    composition.connector_service = None
    composition.artifact_service = None
    composition.input_attachment_read_runtime = None
    unbound = RuntimeAvailability(
        platform="windows",
        disabled_tools={
            tool_id: "verified_handler_not_installed"
            for tool_id in (
                "connector_search",
                "connector_describe",
                "connector_read",
                "connector_write",
                "artifact_read",
                "input_attachment_read",
            )
        },
    )

    unbound = composition._apply_connector_execution_availability(unbound)
    unbound = composition._apply_artifact_read_availability(unbound)
    unbound = composition._apply_input_attachment_read_availability(unbound)

    assert unbound.disabled_tools == {
        "connector_search": "connector_runtime_not_bound",
        "connector_describe": "connector_runtime_not_bound",
        "connector_read": "connector_runtime_not_bound",
        "connector_write": "connector_runtime_not_bound",
        "artifact_read": "artifact_runtime_not_bound",
        "input_attachment_read": "input_attachment_runtime_not_bound",
    }

    # Binding proves only that the Core handlers exist.  It must not erase a
    # denial from another availability authority.
    composition.connector_service = object()
    composition.artifact_service = object()
    composition.input_attachment_read_runtime = object()
    bound = RuntimeAvailability(
        platform="windows",
        disabled_tools={
            "connector_search": "verified_handler_not_installed",
            "connector_describe": "administrator_hard_deny",
            "connector_read": "offline",
            "connector_write": "sandbox_profile_unavailable",
            "artifact_read": "capability_pack_disabled",
            "input_attachment_read": "verified_handler_not_installed",
        },
    )

    bound = composition._apply_connector_execution_availability(bound)
    bound = composition._apply_artifact_read_availability(bound)
    bound = composition._apply_input_attachment_read_availability(bound)

    assert bound.disabled_tools == {
        "connector_describe": "administrator_hard_deny",
        "connector_read": "offline",
        "connector_write": "sandbox_profile_unavailable",
        "artifact_read": "capability_pack_disabled",
    }


def test_invocation_reuses_bound_core_availability_not_raw_pack_facts(tmp_path) -> None:
    """A disclosed Core tool cannot regress at just-in-time governance."""

    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            disabled_capability_tools={
                # This is the low-level pack builder's pre-composition fact.
                # Product composition binds the trusted handler afterwards.
                "connector_search": "verified_handler_not_installed",
            },
        )
    )
    composition = app.state.runtime_composition
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="搜索可用连接器",
            explicit_tool_ids=["connector_search"],
            client_message_id="bound-invocation-availability",
        )
    )

    governance = composition.capability_service.invocation_governance(
        prepared.snapshot_context.capability_snapshot_id,
        "connector_search",
    )

    assert governance.allowed is True
    assert not any(
        reason.startswith("current_availability:disabled:connector_search")
        for reason in governance.reason_codes
    )


def test_turn_permission_admission_rejects_an_async_acceptance_callback(
    tmp_path,
) -> None:
    app, _client_instance = _client(tmp_path)

    async def external_provider(_prepared):
        return "must-not-run"

    with pytest.raises(TypeError, match="must not return an awaitable"):
        app.state.runtime_composition.admit_turn(
            CreateTurnRequest(
                input="do not hold permission lock over await",
                client_message_id="async-admission-rejected",
            ),
            external_provider,
        )


def test_runtime_explicit_media_name_respects_diagnostic_suppression(tmp_path) -> None:
    _app, client = _client(tmp_path)
    thread = client.post(
        "/api/v1/threads", json={}, headers=_headers(mutation=True)
    ).json()
    created = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={
            "input": "imagegen 生图失败，只分析故障，不要生成图片",
            "client_message_id": "diagnostic-media-alias",
        },
        headers=_headers(mutation=True),
    )
    assert created.status_code == 202
    accepted = next(
        event
        for event in client.get(
            f"/api/v1/threads/{thread['thread_id']}/events",
            headers=_headers(),
        ).json()["events"]
        if event["event_type"] == "turn.accepted"
    )
    plan = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
        accepted["capability_snapshot_id"]
    )
    candidate = plan.decision("imagegen")

    assert candidate is not None
    assert candidate.exposure.value == "deferred"
    assert "explicit_reference" not in candidate.reason_codes
    assert any(
        reason.startswith("intent_route:media.image.")
        for reason in candidate.suppression_reasons
    )


def test_positive_image_model_alias_is_canonicalized_and_audited(tmp_path) -> None:
    app, _client_instance = _client(tmp_path)
    composition = app.state.runtime_composition

    selected = composition.prepare_turn(
        CreateTurnRequest(
            input="用 image2 做海报",
            client_message_id="intent-image-model-alias",
        )
    )
    selected_config = RuntimeSnapshotRepository(tmp_path / "runtime.db").get(
        selected.snapshot_context.config_snapshot_id
    )
    mentioned = composition.prepare_turn(
        CreateTurnRequest(
            input="image2 有什么特点和价格？",
            client_message_id="mention-image-model-alias",
        )
    )
    mentioned_config = RuntimeSnapshotRepository(tmp_path / "runtime.db").get(
        mentioned.snapshot_context.config_snapshot_id
    )

    assert selected.request.image_model_id == "gpt-image-2"
    assert selected_config.payload["image_model_selection_source"] == "intent_alias"
    assert mentioned.request.image_model_id == "gpt-image-2"
    assert mentioned_config.payload["image_model_selection_source"] == "default"


def test_chat_only_model_snapshot_hides_image_tools_before_invocation(tmp_path) -> None:
    app, _client_instance = _client(tmp_path)
    source = app.state.runtime_composition
    chat_only = ManagedModelCatalog(
        (
            ManagedModelSpec(
                model_id="ecorex-chat",
                display_name="EcoreX Chat",
                modalities=frozenset({ModelModality.CHAT}),
                default_for=frozenset({ModelModality.CHAT}),
            ),
        )
    )
    composition = RuntimeComposition(
        database_path=str(tmp_path / "chat-only.db"),
        product_version="1.0.0",
        permission_snapshot_id=source.permission_snapshot.snapshot_id,
        permission_payload=source.permission_snapshot.payload,
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset({"image"}),
        connected_connectors=frozenset(),
        online=True,
        model_catalog=chat_only,
        capability_handlers={"imagegen": lambda arguments: {"ok": True}},
    )

    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="生成一张产品海报",
            client_message_id="chat-only-image-intent",
        )
    )
    plan = composition.capability_service.get_plan(
        prepared.snapshot_context.capability_snapshot_id
    )
    imagegen = plan.decision("imagegen")

    assert prepared.request.image_model_id is None
    assert imagegen is not None
    assert imagegen.eligible is False
    assert imagegen.exposure.value == "hidden"
    assert "missing_model_modalities:image" in imagegen.reason_codes
    assert {decision.tool_id for decision in plan.direct} >= {
        "tool_search",
        "tool_describe",
    }


def test_model_catalog_provider_refreshes_new_turn_snapshot_and_revokes_old_model(
    tmp_path,
) -> None:
    app, _client_instance = _client(tmp_path / "source")
    source = app.state.runtime_composition
    old_catalog = ManagedModelCatalog(
        (
            ManagedModelSpec(
                model_id="managed-chat-old",
                display_name="Managed Chat Old",
                modalities=frozenset({ModelModality.CHAT}),
                capabilities=frozenset({"chat", "tools"}),
                default_for=frozenset({ModelModality.CHAT}),
            ),
        )
    )
    new_catalog = ManagedModelCatalog(
        (
            ManagedModelSpec(
                model_id="managed-chat-new",
                display_name="Managed Chat New",
                modalities=frozenset({ModelModality.CHAT}),
                capabilities=frozenset({"chat", "tools", "reasoning"}),
                default_for=frozenset({ModelModality.CHAT}),
            ),
        )
    )
    active = {"catalog": old_catalog}
    database = tmp_path / "hot-models.db"
    composition = RuntimeComposition(
        database_path=str(database),
        product_version="1.0.0",
        permission_snapshot_id=source.permission_snapshot.snapshot_id,
        permission_payload=source.permission_snapshot.payload,
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset(),
        connected_connectors=frozenset(),
        online=True,
        model_catalog=old_catalog,
        model_catalog_provider=lambda: active["catalog"],
    )

    before = composition.prepare_turn(
        CreateTurnRequest(
            input="first turn",
            agent_model_id="managed-chat-old",
            client_message_id="model-before-refresh",
        )
    )
    active["catalog"] = new_catalog
    after = composition.prepare_turn(
        CreateTurnRequest(
            input="new allowlisted model",
            agent_model_id="managed-chat-new",
            client_message_id="model-after-refresh",
        )
    )

    assert before.request.agent_model_id == "managed-chat-old"
    assert before.snapshot_context.model_catalog_snapshot_id == old_catalog.snapshot_id
    assert after.request.agent_model_id == "managed-chat-new"
    assert after.snapshot_context.model_catalog_snapshot_id == new_catalog.snapshot_id
    assert before.snapshot_context.model_catalog_snapshot_id != (
        after.snapshot_context.model_catalog_snapshot_id
    )
    snapshots = RuntimeSnapshotRepository(database)
    assert snapshots.get(old_catalog.snapshot_id).payload["snapshot_id"] == (
        old_catalog.snapshot_id
    )
    assert snapshots.get(new_catalog.snapshot_id).payload["snapshot_id"] == (
        new_catalog.snapshot_id
    )

    with pytest.raises(UnknownModelError, match="unknown managed model"):
        composition.prepare_turn(
            CreateTurnRequest(
                input="must not retain revoked model authority",
                agent_model_id="managed-chat-old",
                client_message_id="revoked-model-rejected",
            )
        )


def test_model_catalog_provider_empty_result_fails_closed_without_stale_fallback(
    tmp_path,
) -> None:
    app, _client_instance = _client(tmp_path / "source")
    source = app.state.runtime_composition
    catalog = ManagedModelCatalog(
        (
            ManagedModelSpec(
                model_id="managed-chat",
                display_name="Managed Chat",
                modalities=frozenset({ModelModality.CHAT}),
                capabilities=frozenset({"chat", "tools"}),
                default_for=frozenset({ModelModality.CHAT}),
            ),
        )
    )
    active: dict[str, object] = {"catalog": catalog}
    composition = RuntimeComposition(
        database_path=str(tmp_path / "empty-hot-models.db"),
        product_version="1.0.0",
        permission_snapshot_id=source.permission_snapshot.snapshot_id,
        permission_payload=source.permission_snapshot.payload,
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset(),
        connected_connectors=frozenset(),
        online=True,
        model_catalog=catalog,
        model_catalog_provider=lambda: active["catalog"],  # type: ignore[return-value]
    )
    accepted = composition.prepare_turn(
        CreateTurnRequest(
            input="catalog still available",
            agent_model_id="managed-chat",
            client_message_id="catalog-before-empty",
        )
    )
    assert accepted.request.agent_model_id == "managed-chat"

    active["catalog"] = None
    with pytest.raises(
        UnknownModelError,
        match="managed model catalog is unavailable",
    ):
        composition.prepare_turn(
            CreateTurnRequest(
                input="do not fall back to stale allowlist",
                agent_model_id="managed-chat",
                client_message_id="catalog-empty-fail-closed",
            )
        )


@pytest.mark.parametrize(
    ("capabilities", "expected_reason"),
    (
        (
            frozenset(),
            "missing_model_capabilities:image:image-edit,image-generation",
        ),
        (
            frozenset({"image_generation"}),
            "missing_model_capabilities:image:image-edit",
        ),
        (
            frozenset({"image_edit"}),
            "missing_model_capabilities:image:image-generation",
        ),
    ),
)
def test_image_model_modality_cannot_substitute_for_required_features(
    tmp_path,
    capabilities: frozenset[str],
    expected_reason: str,
) -> None:
    app, _client_instance = _client(tmp_path)
    source = app.state.runtime_composition
    chat_model = source.model_catalog.for_modality(ModelModality.CHAT)[0]
    incomplete_catalog = ManagedModelCatalog(
        (
            chat_model,
            ManagedModelSpec(
                model_id="incomplete-image-model",
                display_name="Incomplete Image Model",
                modalities=frozenset({ModelModality.IMAGE}),
                capabilities=capabilities,
                default_for=frozenset({ModelModality.IMAGE}),
            ),
        )
    )

    # Even a provider that claims the missing features cannot widen the
    # canonical managed catalog selected for this Turn.
    claimed = RuntimeAvailability(
        platform="windows",
        installed_packs=frozenset({"image"}),
        selected_model_modalities=frozenset({"chat", "image"}),
        selected_model_capabilities={
            "chat": chat_model.capabilities,
            "image": frozenset({"image_generation", "image_edit"}),
        },
    )
    composition = RuntimeComposition(
        database_path=str(tmp_path / ("incomplete-" + str(len(capabilities)) + ".db")),
        product_version="1.0.0",
        permission_snapshot_id=source.permission_snapshot.snapshot_id,
        permission_payload=source.permission_snapshot.payload,
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset({"image"}),
        connected_connectors=frozenset(),
        online=True,
        model_catalog=incomplete_catalog,
        capability_handlers={"imagegen": lambda arguments: {"ok": True}},
        availability_provider=lambda: claimed,
    )

    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="生成并精修一张产品海报",
            client_message_id="incomplete-image-model",
        )
    )
    plan = composition.capability_service.get_plan(
        prepared.snapshot_context.capability_snapshot_id
    )
    imagegen = plan.decision("imagegen")
    config = RuntimeSnapshotRepository(
        tmp_path / ("incomplete-" + str(len(capabilities)) + ".db")
    ).get(prepared.snapshot_context.config_snapshot_id)
    canonical_capabilities = sorted(
        incomplete_catalog.for_modality(ModelModality.IMAGE)[0].capabilities
    )

    assert imagegen is not None
    assert imagegen.eligible is False
    assert imagegen.exposure.value == "hidden"
    assert expected_reason in imagegen.reason_codes
    assert plan.selected_model_capabilities is not None
    assert plan.selected_model_capabilities["image"] == frozenset(
        canonical_capabilities
    )
    assert config.payload["availability"]["selected_model_capabilities"]["image"] == (
        canonical_capabilities
    )


def test_runtime_rejects_non_scalar_unicode_intent_as_a_client_error(tmp_path) -> None:
    _app, client = _client(tmp_path)
    thread = client.post(
        "/api/v1/threads", json={}, headers=_headers(mutation=True)
    ).json()
    response = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        content=json.dumps(
            {"input": "\ud800 generate an image", "client_message_id": "bad-unicode"},
            ensure_ascii=True,
        ),
        headers={**_headers(mutation=True), "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "string_unicode"
    assert detail[0]["loc"] == ["body", "input"]
    assert "input" not in detail[0]


def test_runtime_snapshots_and_job_contexts_are_immutable(tmp_path) -> None:
    app, client = _client(tmp_path)
    bootstrap = client.get("/api/v1/bootstrap", headers=_headers()).json()
    repository = RuntimeSnapshotRepository(tmp_path / "runtime.db")
    permission_id = bootstrap["permissions"]["snapshot_id"]

    with pytest.raises(RuntimeSnapshotConflict):
        repository.save(
            "permission",
            {"full_access": True},
            snapshot_id=permission_id,
        )

    thread = client.post(
        "/api/v1/threads", json={}, headers=_headers(mutation=True)
    ).json()
    created = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={"input": "hello", "client_message_id": "immutable"},
        headers=_headers(mutation=True),
    ).json()
    accepted = next(
        event
        for event in client.get(
            f"/api/v1/threads/{thread['thread_id']}/events", headers=_headers()
        ).json()["events"]
        if event["event_type"] == "turn.accepted"
    )
    frozen = repository.get(accepted["config_snapshot_id"])
    assert frozen.payload["agent_model_id"] == "ecorex-chat"
    assert frozen.payload["image_model_id"] == "gpt-image-2"
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE job_runtime_contexts SET config_snapshot_id = 'tampered' "
                "WHERE job_id = ?",
                (created["job"]["job_id"],),
            )
