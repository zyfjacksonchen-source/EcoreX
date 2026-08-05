from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from ecorex.capabilities import (
    Exposure,
    ToolSpec,
    builtin_capability_registry,
)
from ecorex.gateway import GatewayEvent
from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    InteractionAction,
    InteractionActionStyle,
    InteractionActionType,
    InteractionConnectorContext,
    InteractionContract,
    InteractionFieldControl,
    InteractionFormField,
    InteractionKind,
)
from ecorex.replay import ReplayService
from ecorex.runtime import (
    AgentTurnWorker,
    InteractionStore,
    PermissionAuthority,
    RuntimeComposition,
    RuntimeKernel,
    SQLiteDatabase,
    WorkerOutcome,
)
from ecorex.runtime.errors import (
    IdempotencyConflictError,
    InteractionResponseValidationError,
)


class ScriptedGateway:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        script = self.scripts.pop(0)
        if len(script) == 1 and script[0]["event_type"] == "response.completed":
            yield GatewayEvent.model_validate(
                {
                    "seq": 1,
                    "event_type": "output_text.delta",
                    "response_id": script[0]["response_id"],
                    "delta": "done",
                }
            )
            script = [{**script[0], "seq": 2}]
        for event in script:
            yield GatewayEvent.model_validate(event)


def _permission_contract() -> InteractionContract:
    return InteractionContract(
        title="权限确认",
        actions=[
            InteractionAction(
                action_id="allow",
                label="允许",
                action_type=InteractionActionType.ALLOW,
            ),
            InteractionAction(
                action_id="deny",
                label="拒绝",
                action_type=InteractionActionType.DENY,
            ),
        ],
    )


def test_typed_form_rejects_extra_invalid_and_sensitive_values_before_persistence(
    tmp_path,
) -> None:
    store = InteractionStore(SQLiteDatabase(tmp_path / "runtime.db"))
    contract = InteractionContract(
        title="补充交付信息",
        fields=[
            InteractionFormField(
                field_id="audience",
                label="目标读者",
                control=InteractionFieldControl.SELECT,
                required=True,
                options=[
                    {"option_id": "internal", "label": "内部"},
                    {"option_id": "customer", "label": "客户"},
                ],
            ),
            InteractionFormField(
                field_id="notes",
                label="修改说明",
                control=InteractionFieldControl.TEXTAREA,
                required=True,
                min_length=2,
                max_length=80,
            ),
        ],
        actions=[
            InteractionAction(
                action_id="submit",
                label="提交",
                action_type=InteractionActionType.SUBMIT,
                style=InteractionActionStyle.PRIMARY,
                submits_form=True,
            ),
            InteractionAction(
                action_id="cancel",
                label="取消",
                action_type=InteractionActionType.CANCEL,
            ),
        ],
    )
    request = store.create(
        kind=InteractionKind.INFORMATION,
        prompt="请确认交付对象和修改说明。",
        contract=contract,
        thread_id="thread-form",
        idempotency_key="form-1",
    )

    invalid_responses = (
        {"action_id": "submit", "values": {"audience": "unknown", "notes": "修改"}},
        {"action_id": "submit", "values": {"audience": "internal", "extra": "x"}},
        {
            "action_id": "submit",
            "values": {"audience": "internal", "notes": "Bearer abcdefghijklmnop"},
        },
    )
    for index, response in enumerate(invalid_responses):
        with pytest.raises(InteractionResponseValidationError):
            store.respond(
                request.interaction_id,
                response,
                client_request_id=f"invalid-{index}",
            )
    assert store.get(request.interaction_id).response is None
    assert not any(
        event.event_type == "interaction.resolved"
        for event in store.events.page("thread-form").events
    )

    resolved = store.respond(
        request.interaction_id,
        {
            "action_id": "submit",
            "values": {"audience": "customer", "notes": "调整首页"},
        },
        client_request_id="valid-form-response",
    )
    assert resolved.response and resolved.response.values["audience"] == "customer"


def test_connector_login_contract_has_safe_actions_and_never_accepts_credentials(
) -> None:
    with pytest.raises(ValidationError, match="sensitive interaction fields"):
        InteractionFormField(
            field_id="access_token",
            label="访问令牌",
            control=InteractionFieldControl.TEXT,
        )

    contract = InteractionContract(
        title="连接飞书",
        connector=InteractionConnectorContext(
            connector_id="feishu",
            display_name="飞书",
            state="authorization_required",
        ),
        actions=[
            InteractionAction(
                action_id="begin_login",
                label="打开安全登录",
                action_type=InteractionActionType.CONNECTOR_BEGIN_LOGIN,
                style=InteractionActionStyle.PRIMARY,
            ),
            InteractionAction(
                action_id="cancel",
                label="取消",
                action_type=InteractionActionType.CANCEL,
            ),
        ],
    ).validate_for_kind(InteractionKind.CONNECTOR_LOGIN)
    assert contract.fields == []
    with pytest.raises(ValueError, match="safe actions/status only"):
        contract.model_copy(
            update={
                "fields": [
                    InteractionFormField(
                        field_id="account",
                        label="账号",
                        control=InteractionFieldControl.TEXT,
                    )
                ]
            }
        ).validate_for_kind(InteractionKind.CONNECTOR_LOGIN)


def test_response_client_request_id_is_global_and_payload_fenced(tmp_path) -> None:
    store = InteractionStore(SQLiteDatabase(tmp_path / "runtime.db"))
    first = store.create(
        kind=InteractionKind.PERMISSION_APPROVAL,
        prompt="允许？",
        contract=_permission_contract(),
        thread_id="thread-a",
        idempotency_key="first",
    )
    second = store.create(
        kind=InteractionKind.PERMISSION_APPROVAL,
        prompt="再次允许？",
        contract=_permission_contract(),
        thread_id="thread-b",
        idempotency_key="second",
    )
    response = {"action_id": "allow", "values": {}}
    resolved = store.respond(
        first.interaction_id,
        response,
        client_request_id="stable-response-id",
    )
    assert store.respond(
        first.interaction_id,
        response,
        client_request_id="stable-response-id",
    ) == resolved
    with pytest.raises(IdempotencyConflictError):
        store.respond(
            second.interaction_id,
            response,
            client_request_id="stable-response-id",
        )


def _artifact_runtime(database, handler):
    authority = PermissionAuthority(
        database,
        account_id="local-user",
        initial_full_access=False,
    )
    permission = authority.current()
    registry = builtin_capability_registry()
    registry.register(
        ToolSpec(
            tool_id="artifact_review_probe",
            version="1.0.0",
            display_name="交付物检查",
            description="创建办公交付物并请求用户检查。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "_ecorex_interaction": {"type": "object"},
                },
                "required": ["artifact_id", "_ecorex_interaction"],
                "additionalProperties": False,
            },
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"artifact", "review", "交付物", "检查"}),
        )
    )
    return RuntimeComposition(
        database_path=str(database),
        product_version="1.0.0",
        permission_snapshot_id=permission.snapshot_id,
        permission_payload=permission.model_dump(mode="json"),
        full_access=False,
        admin_hard_denies=frozenset(),
        platform="windows",
        installed_packs=frozenset(),
        connected_connectors=frozenset(),
        online=True,
        capability_registry=registry,
        capability_handlers={"artifact_review_probe": handler},
        permission_provider=authority.current,
    )


def test_worker_artifact_review_contract_survives_restart_and_replays(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    calls = []

    def create_artifact(_arguments):
        calls.append("called")
        return {
            "artifact_id": "art_report",
            "_ecorex_interaction": {
                "schema_version": 1,
                "kind": "artifact_review",
                "prompt": "请检查报告首页，确认是否需要调整。",
                "contract": {
                    "schema_version": 1,
                    "title": "检查办公交付物",
                    "fields": [
                        {
                            "field_id": "notes",
                            "label": "修改说明",
                            "control": "textarea",
                            "required": True,
                            "min_length": 2,
                            "max_length": 120,
                        }
                    ],
                    "actions": [
                        {
                            "action_id": "request_changes",
                            "label": "继续修改",
                            "action_type": "request_changes",
                            "style": "primary",
                            "submits_form": True,
                        },
                        {
                            "action_id": "accept",
                            "label": "确认完成",
                            "action_type": "accept",
                        },
                    ],
                },
            },
        }

    composition = _artifact_runtime(database, create_artifact)
    kernel = RuntimeKernel(database)
    thread = kernel.create_thread(CreateThreadRequest(title="artifact review"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="创建交付物后让我检查",
            client_message_id="artifact-review-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "artifact-response",
                    "tool_call_id": "artifact-call",
                    "tool_name": "artifact_review_probe",
                    "arguments": {},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "after-review",
                }
            ],
        ]
    )
    first_worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
    )
    first = asyncio.run(first_worker.run_once("artifact-worker"))
    assert first.outcome is WorkerOutcome.WAITING_HUMAN
    assert calls == ["called"]
    interaction = kernel.list_interactions(thread.thread_id).interactions[0]
    assert interaction.kind is InteractionKind.ARTIFACT_REVIEW
    assert interaction.contract.fields[0].field_id == "notes"

    restarted = RuntimeKernel(database)
    restarted.respond_interaction(
        interaction.interaction_id,
        {
            "action_id": "request_changes",
            "values": {"notes": "标题再醒目一些"},
        },
        client_request_id="artifact-review-response",
    )
    restarted_composition = _artifact_runtime(database, create_artifact)
    restarted_worker = AgentTurnWorker(
        restarted,
        gateway=gateway,
        capabilities=restarted_composition.capability_service,
    )
    completed = asyncio.run(restarted_worker.run_once("artifact-worker-restarted"))
    assert completed.outcome is WorkerOutcome.COMPLETED
    assert calls == ["called"]
    assert gateway.requests[1].tool_outputs[0].output == {
        "tool_result": {"artifact_id": "art_report"},
        "human_response": {
            "action_id": "request_changes",
            "values": {"notes": "标题再醒目一些"},
        },
    }
    replay = ReplayService(restarted).mock_replay(thread.thread_id)
    replay_interaction = replay.interactions[0]
    assert replay_interaction.response_client_request_id == "artifact-review-response"
    assert replay_interaction.response and replay_interaction.response.action_id == "request_changes"
    assert restarted.jobs.get(created.job.job_id).status.value == "completed"
