from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from ecorex.capabilities import (
    CapabilityRegistry,
    Exposure,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
    ToolSpec,
)
from ecorex.capabilities.builtin import builtin_tool_specs
from ecorex.gateway import GatewayEvent
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app
from ecorex.runtime.errors import ConflictError
from ecorex.runtime.public_tools import (
    PublicToolActivityProjector,
    PublicToolProjectionError,
)


TOKEN = "r" * 43
CSRF = "c" * 43
ORIGIN = "http://testserver"
SENSITIVE = {
    "token": "tok-live-must-stay-internal",
    "path": "C:\\Users\\alice\\private.txt",
    "worker": "worker-private-identity",
    "lease": "lease-private-fence",
    "checkpoint": "checkpoint-private-state",
    "idempotency": "idempotency-private-key",
    "error": "provider-private-stack-trace",
}


class _ScriptedGateway:
    def __init__(self, scripts) -> None:
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        for raw in self.scripts.pop(0):
            yield GatewayEvent.model_validate(raw)


def _runtime(tmp_path, *, handler):
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            capability_handlers={"read": handler},
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="Public Tool"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="读取工作资料并给出结论",
            client_message_id="public-tool-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    return app, kernel, composition, thread, created


def _wire(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _assert_secret_free(value) -> None:
    wire = _wire(value)
    for secret in SENSITIVE.values():
        assert secret not in wire


def test_real_worker_keeps_raw_tool_data_internal_across_all_public_exits(
    tmp_path,
) -> None:
    raw_result = {
        "document": {
            "credential": SENSITIVE["token"],
            "source_path": SENSITIVE["path"],
            "execution": {
                "worker_id": SENSITIVE["worker"],
                "lease_token": SENSITIVE["lease"],
                "checkpoint": SENSITIVE["checkpoint"],
                "idempotency_key": SENSITIVE["idempotency"],
                "raw_error": SENSITIVE["error"],
            },
        }
    }
    calls = []

    def read_handler(arguments):
        calls.append(dict(arguments))
        return raw_result

    app, kernel, composition, thread, created = _runtime(
        tmp_path,
        handler=read_handler,
    )
    raw_arguments = {"path": SENSITIVE["path"]}
    gateway = _ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "response-public-tool",
                    "tool_call_id": "call-public-read",
                    "tool_name": "read",
                    "arguments": raw_arguments,
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "output_text.delta",
                    "response_id": "response-public-final",
                    "delta": "资料已读取。",
                },
                {
                    "seq": 2,
                    "event_type": "response.completed",
                    "response_id": "response-public-final",
                },
            ],
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
    )
    result = asyncio.run(worker.run_once("worker-public-tool"))
    assert result.outcome is WorkerOutcome.COMPLETED
    assert calls == [raw_arguments]

    # The model continuation and internal ToolExecutionRecord retain the exact
    # result. Public projection is not used as a lossy model input.
    assert gateway.requests[1].tool_outputs[0].output == raw_result
    with kernel.database.reader() as connection:
        execution = connection.execute(
            "SELECT arguments_json, result_json FROM tool_executions "
            "WHERE tool_id='read'"
        ).fetchone()
    assert json.loads(str(execution["arguments_json"])) == raw_arguments
    assert json.loads(str(execution["result_json"])) == raw_result

    projection = kernel.projection(thread.thread_id)
    tool_item = next(
        item for item in projection.items if item.kind is ItemKind.TOOL_CALL
    )
    assert tool_item.content["tool_id"] == "read"
    assert tool_item.content["display_label"] == "读取工作区"
    assert tool_item.content["argument_summary"] == "正在读取工作资料"
    assert tool_item.content["result_summary"] == "已读取工作资料"
    assert tool_item.content["argument_sha256"] == hashlib.sha256(
        json.dumps(
            raw_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _assert_secret_free(projection)

    auth = {"Authorization": f"Bearer {TOKEN}"}
    client = TestClient(app)
    event_page = client.get(
        f"/api/v1/threads/{thread.thread_id}/events",
        headers=auth,
    )
    assert event_page.status_code == 200
    _assert_secret_free(event_page.json())
    tool_events = [
        event
        for event in event_page.json()["events"]
        if event["event_type"] in {"tool.call_requested", "tool.result"}
    ]
    assert len(tool_events) == 2
    assert all(set(event["payload"]) == {"activity"} for event in tool_events)

    sse = client.get(
        f"/api/v1/threads/{thread.thread_id}/events/stream",
        params={"after_seq": 0, "follow": "false"},
        headers={**auth, "Accept": "text/event-stream"},
    )
    assert sse.status_code == 200
    _assert_secret_free(sse.text)

    replay = client.get(
        f"/api/v1/threads/{thread.thread_id}/replay",
        headers=auth,
    )
    assert replay.status_code == 200
    _assert_secret_free(replay.json())
    replay_tool = next(
        item
        for item in replay.json()["projection"]["items"]
        if item["kind"] == "tool_call"
    )
    assert replay_tool["content"] == tool_item.content
    assert kernel.jobs.get(created.job.job_id).status.value == "completed"


def test_public_projection_policies_are_fixed_and_third_party_is_opaque() -> None:
    projector = PublicToolActivityProjector()
    registry = CapabilityRegistry(builtin_tool_specs())
    cases = {
        "read": ("正在读取工作资料", "已读取工作资料", {}),
        "shell": ("正在执行已批准的命令", "命令执行已完成", {}),
        "imagegen": (
            "正在生成或修改图片",
            "图片已生成并保存",
            {"artifact_id": "art_public", "revision_id": "rev_public"},
        ),
        "connector_read": (
            "正在从已连接的应用读取信息",
            "已完成连接器读取",
            {
                "artifact": {
                    "artifact_id": "art_connector",
                    "revision_id": "rev_connector",
                    "provider_body": SENSITIVE["token"],
                }
            },
        ),
    }
    for tool_id, (argument_summary, result_summary, result) in cases.items():
        activity = projector.completed(
            registry.get(tool_id),
            tool_call_id=f"call-{tool_id}",
            arguments=(
                {"command": SENSITIVE["error"]}
                if tool_id == "shell"
                else {"path": SENSITIVE["path"]}
                if tool_id == "read"
                else {"instruction": SENSITIVE["token"]}
                if tool_id == "imagegen"
                else {"discovery_id": "connector:opaque"}
            ),
            result={**result, "private": SENSITIVE},
        )
        assert activity.argument_summary == argument_summary
        assert activity.result_summary == result_summary
        _assert_secret_free(activity)
    image = projector.completed(
        registry.get("imagegen"),
        tool_call_id="call-image-artifact",
        arguments={"instruction": "生成图片"},
        result={"artifact_id": "art_public", "revision_id": "rev_public"},
    )
    assert image.artifact_refs[0].artifact_id == "art_public"
    connector = projector.completed(
        registry.get("connector_read"),
        tool_call_id="call-connector-artifact",
        arguments={"discovery_id": "connector:opaque"},
        result={
            "artifact": {
                "artifact_id": "art_connector",
                "revision_id": "rev_connector",
            }
        },
    )
    assert connector.artifact_refs[0].revision_id == "rev_connector"

    provider_id = "office"
    third_party = ToolSpec(
        tool_id="mcp.office:lookup",
        version="1.0.0",
        display_name=f"泄漏-{SENSITIVE['token']}",
        description="third-party lookup",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        default_exposure=Exposure.DEFERRED,
        provider=ToolProviderProvenance(
            kind=ToolProviderKind.MCP,
            provider_id=provider_id,
            revision_id="extrev_" + hashlib.sha256(provider_id.encode()).hexdigest(),
            trust=ToolProviderTrust.VERIFIED_PUBLISHER,
            key_id="key-office",
            evidence_sha256=hashlib.sha256(b"office-evidence").hexdigest(),
        ),
    )
    opaque = projector.completed(
        third_party,
        tool_call_id="call-third-party",
        arguments={"token": SENSITIVE["token"]},
        result={"path": SENSITIVE["path"], "artifact_id": "art_must_not_project"},
    )
    assert opaque.display_label == "使用已连接的应用"
    assert opaque.argument_summary == "正在使用已连接的应用"
    assert opaque.result_summary == "已完成应用操作"
    assert opaque.artifact_refs == []
    _assert_secret_free(opaque)


def test_every_builtin_tool_has_a_reviewed_public_activity_policy() -> None:
    projector = PublicToolActivityProjector()
    for spec in builtin_tool_specs():
        activity = projector.requested(
            spec,
            tool_call_id=f"call-{spec.tool_id}",
            arguments={},
        )
        assert activity.display_label == spec.display_name
        assert activity.argument_summary != "正在使用已连接的应用"


def test_kernel_and_event_store_reject_raw_tool_public_ingress(tmp_path) -> None:
    app, kernel, composition, _thread, created = _runtime(
        tmp_path,
        handler=lambda _arguments: {},
    )
    raw = {
        "tool_call_id": "call-raw",
        "tool_name": "read",
        "arguments": {"path": SENSITIVE["path"], "token": SENSITIVE["token"]},
    }
    with pytest.raises(ConflictError, match="PublicToolActivity"):
        kernel.create_item(
            turn_id=created.turn.turn_id,
            kind=ItemKind.TOOL_CALL,
            content=raw,
        )
    with pytest.raises(PublicToolProjectionError, match="PublicToolActivity"):
        kernel.events.append(
            thread_id=created.turn.thread_id,
            turn_id=created.turn.turn_id,
            tool_call_id="call-raw",
            event_type="tool.call_requested",
            payload={"tool_name": "read", "arguments": raw["arguments"]},
        )
    public_activity = PublicToolActivityProjector().requested(
        composition.capability_service.registry.get("read"),
        tool_call_id="call-public",
        arguments=raw["arguments"],
    )
    with pytest.raises(PublicToolProjectionError, match="identity differs"):
        kernel.events.append(
            thread_id=created.turn.thread_id,
            turn_id=created.turn.turn_id,
            tool_call_id="call-different",
            event_type="tool.call_requested",
            payload={"activity": public_activity.model_dump(mode="json")},
        )
    assert SENSITIVE["path"].encode("utf-8") not in (
        tmp_path / "runtime.db"
    ).read_bytes()
