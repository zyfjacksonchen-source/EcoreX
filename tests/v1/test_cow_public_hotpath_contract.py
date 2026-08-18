from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace

from agent.skills.manager import SkillManager
from agent.tools.base_tool import ToolResult
from agent.tools.imagegen.imagegen import ImageGenTool
from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.scheduler_tool import SchedulerTool
from agent.tools.scheduler.task_store import TaskStore
from agent.tools.tool_manager import ToolManager
from bridge.agent_initializer import AgentInitializer
from PIL import Image

from ecorex.gateway import (
    GatewayEvent,
    GatewayFunctionCallOutputInput,
    GatewayUserMessageInput,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, SteerTurnRequest
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app


class _AttachmentGateway:
    def __init__(self) -> None:
        self.requests = []
        self.first_request_started = threading.Event()
        self.steer_received = threading.Event()

    async def stream(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.first_request_started.set()
            assert await asyncio.to_thread(self.steer_received.wait, 2)
        response_id = f"cow-hotpath-response-{len(self.requests)}"
        yield GatewayEvent(
            seq=1,
            event_type="output_text.delta",
            response_id=response_id,
            delta="done",
        )
        yield GatewayEvent(
            seq=2,
            event_type="response.completed",
            response_id=response_id,
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(output, format="PNG")
    return output.getvalue()


def test_public_cow_worker_sends_initial_and_steer_images_to_gateway(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.protocol.steer import SteerInbox

    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    initial_image = app.state.input_attachment_service.upload(
        _png((20, 80, 220)),
        filename="initial.png",
        mime_type="image/png",
        client_request_id="cow-hotpath-initial-image",
    )
    steer_image = app.state.input_attachment_service.upload(
        _png((220, 80, 20)),
        filename="steer.png",
        mime_type="image/png",
        client_request_id="cow-hotpath-steer-image",
    )
    thread = kernel.create_thread(CreateThreadRequest(title="attachment"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Inspect the first image",
            metadata={
                "input_attachments": [initial_image.model_dump(mode="json")]
            },
            client_message_id="cow-hotpath-attachment-turn",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _AttachmentGateway()
    original_submit = SteerInbox.submit

    def observe_submit(inbox, content):
        result = original_submit(inbox, content)
        if "Use this replacement image instead" in content:
            gateway.steer_received.set()
        return result

    monkeypatch.setattr(SteerInbox, "submit", observe_submit)
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        input_attachments=app.state.input_attachment_service,
    )

    async def run_with_live_steer():
        task = asyncio.create_task(worker.run_once("cow-hotpath-attachment-worker"))
        assert await asyncio.to_thread(gateway.first_request_started.wait, 2)
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(
                input="Use this replacement image instead",
                metadata={
                    "input_attachments": [steer_image.model_dump(mode="json")]
                },
                client_message_id="cow-hotpath-steer-image-turn",
            ),
        )
        assert await asyncio.to_thread(gateway.steer_received.wait, 2)
        return await task

    result = asyncio.run(run_with_live_steer())

    assert result.outcome is WorkerOutcome.COMPLETED
    assert len(gateway.requests) == 2
    initial_messages = [
        item
        for item in gateway.requests[0].ordered_input_items()
        if isinstance(item, GatewayUserMessageInput)
    ]
    steer_messages = [
        item
        for item in gateway.requests[1].ordered_input_items()
        if isinstance(item, GatewayUserMessageInput)
    ]
    assert len(initial_messages) == 1
    assert len(steer_messages) == 2
    assert [image.attachment_id for image in initial_messages[0].images] == [
        initial_image.attachment_id
    ]
    assert [image.source_sha256 for image in initial_messages[0].images] == [
        initial_image.sha256
    ]
    assert [image.attachment_id for image in steer_messages[0].images] == [
        initial_image.attachment_id
    ]
    assert [image.attachment_id for image in steer_messages[1].images] == [
        steer_image.attachment_id
    ]
    assert [image.source_sha256 for image in steer_messages[1].images] == [
        steer_image.sha256
    ]


def test_public_cow_worker_materializes_steer_file_and_redirects_pending_read(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.protocol.steer import SteerInbox

    workspace = tmp_path / "workspace"
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    attachments = app.state.input_attachment_service
    initial = attachments.upload(
        b"initial attachment proof",
        filename="initial proof.txt",
        mime_type="text/plain",
        client_request_id="cow-hotpath-initial-file",
    )
    steer = attachments.upload(
        b"steer attachment proof",
        filename="steer proof.txt",
        mime_type="text/plain",
        client_request_id="cow-hotpath-steer-file",
    )
    thread = kernel.create_thread(CreateThreadRequest(title="file attachment"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Read the initial file",
            metadata={"input_attachments": [initial.model_dump(mode="json")]},
            client_message_id="cow-hotpath-file-turn",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    first_request_started = threading.Event()
    steer_received = threading.Event()
    original_submit = SteerInbox.submit

    def observe_submit(inbox, content):
        result = original_submit(inbox, content)
        if "Then read the steer file" in content:
            steer_received.set()
        return result

    monkeypatch.setattr(SteerInbox, "submit", observe_submit)

    class Gateway:
        def __init__(self) -> None:
            self.round = 0
            self.paths: list[str] = []

        async def stream(self, request):
            self.round += 1
            response_id = f"file-response-{self.round}"
            if self.round == 1:
                text = "\n".join(
                    item.content
                    for item in request.ordered_input_items()
                    if isinstance(item, GatewayUserMessageInput)
                )
                self.paths = re.findall(r"\[File: ([^\]]+)\]", text)
                assert len(self.paths) == 1
                materialized = Path(self.paths[0])
                assert materialized.is_relative_to(workspace)
                assert materialized.stat().st_mode & 0o222 == 0
                first_request_started.set()
                assert await asyncio.to_thread(steer_received.wait, 2)
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id="read-initial",
                    tool_name="read",
                    arguments={"path": self.paths[0]},
                )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id=response_id,
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
                return
            outputs = [
                item
                for item in request.ordered_input_items()
                if isinstance(item, GatewayFunctionCallOutputInput)
            ]
            if self.round == 2:
                assert "Skipped because the user redirected" in json.dumps(
                    outputs[-1].output, ensure_ascii=False
                )
                text = "\n".join(
                    item.content
                    for item in request.ordered_input_items()
                    if isinstance(item, GatewayUserMessageInput)
                )
                steer_paths = re.findall(r"\[File: ([^\]]+)\]", text)
                assert len(steer_paths) == 2
                materialized = Path(steer_paths[-1])
                assert materialized.is_relative_to(workspace)
                assert materialized.stat().st_mode & 0o222 == 0
                self.paths.append(steer_paths[-1])
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id="read-steer",
                    tool_name="read",
                    arguments={"path": self.paths[1]},
                )
                yield GatewayEvent(
                    seq=2,
                    event_type="response.completed",
                    response_id=response_id,
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
                return
            assert "steer attachment proof" in json.dumps(
                outputs[-1].output, ensure_ascii=False
            )
            yield GatewayEvent(
                seq=1,
                event_type="output_text.delta",
                response_id=response_id,
                delta="both files read",
            )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id=response_id,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    worker = AgentTurnWorker(
        kernel,
        gateway=Gateway(),
        workspace_root=workspace,
        input_attachments=attachments,
    )

    async def run_with_live_steer():
        task = asyncio.create_task(worker.run_once("cow-hotpath-file-worker"))
        assert await asyncio.to_thread(first_request_started.wait, 2)
        kernel.steer_turn(
            created.turn.turn_id,
            SteerTurnRequest(
                input="Then read the steer file",
                metadata={"input_attachments": [steer.model_dump(mode="json")]},
                client_message_id="cow-hotpath-steer-file-turn",
            ),
        )
        return await task

    result = asyncio.run(run_with_live_steer())

    assert result.outcome is WorkerOutcome.COMPLETED


def test_public_cow_worker_materializes_owned_artifact_for_ocr_and_projects_failures(
    tmp_path: Path, monkeypatch,
) -> None:
    from ecorex.artifacts import ArtifactFamily, ArtifactScope
    from agent.tools.ocr import ocr

    workspace = tmp_path / "workspace"
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    artifacts = app.state.artifact_service
    thread = kernel.create_thread(CreateThreadRequest(title="artifact OCR"))
    foreign_thread = kernel.create_thread(CreateThreadRequest(title="foreign artifact"))
    declaration = artifacts.issue_trusted_deliverable_declaration(
        "imagegen", family=ArtifactFamily.IMAGE
    )

    def image_artifact(target_thread, name, color):
        return artifacts.create_artifact(
            _png(color),
            requested_name=name,
            mime_type="image/png",
            declaration=declaration,
            scope=ArtifactScope(
                account_id=app.state.runtime_settings.account_id,
                thread_id=target_thread.thread_id,
                created_by_tool_id="imagegen",
            ),
        )

    owned = image_artifact(thread, "EMATE204.png", (255, 140, 0))
    foreign = image_artifact(foreign_thread, "foreign.png", (0, 0, 0))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Read EMATE204 from the prior image artifact",
            client_message_id="cow-hotpath-artifact-ocr-turn",
        )
    )
    kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )

    observed_paths = []
    original_image_bytes = ocr._image_bytes_from_source

    def observe_image_bytes(source, cwd=None):
        observed_paths.append(Path(source))
        return original_image_bytes(source, cwd=cwd)

    monkeypatch.setattr(ocr, "_image_bytes_from_source", observe_image_bytes)
    monkeypatch.setattr(
        ocr,
        "_local_ocr",
        lambda content, _timeout: {
            "status": "success",
            "provider": "fixture",
            "text": "EMATE204" if content == _png((255, 140, 0)) else "WRONG",
            "latencyMs": 1,
            "cacheHit": False,
        },
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            round_index = len(self.requests)
            response_id = f"artifact-ocr-{round_index}"
            outputs = [
                item
                for item in request.ordered_input_items()
                if isinstance(item, GatewayFunctionCallOutputInput)
            ]
            if round_index < 3:
                if round_index == 2:
                    assert "EMATE204" in json.dumps(
                        outputs[-1].output, ensure_ascii=False
                    )
                target = owned if round_index == 1 else foreign
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id=f"ocr-{'owned' if round_index == 1 else 'foreign'}",
                    tool_name="ocr",
                    arguments={
                        "action": "extract_text",
                        "image": f"/api/v1/artifacts/{target.artifact_id}/preview",
                        "timeout": 8,
                    },
                )
            else:
                assert str(outputs[-1].output).startswith("Error:")
                assert "'status': 'error'" in str(outputs[-1].output)
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta="OCR boundary verified",
                )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id=response_id,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    result = asyncio.run(
        AgentTurnWorker(
            kernel,
            gateway=Gateway(),
            workspace_root=workspace,
            input_attachments=app.state.input_attachment_service,
        ).run_once("cow-hotpath-artifact-ocr-worker")
    )

    assert result.outcome is WorkerOutcome.COMPLETED
    assert len(observed_paths) == 1
    assert observed_paths[0].read_bytes() == _png((255, 140, 0))
    assert observed_paths[0].stat().st_mode & 0o222 == 0
    tool_items = [
        item
        for item in kernel.projection(thread.thread_id).items
        if item.kind.value == "tool_call"
    ]
    assert [item.status.value for item in tool_items] == ["completed", "failed"]


def test_public_tool_catalog_is_cow_owned_and_does_not_hide_web_search(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.web_search import web_search
    from agent.tools.web_search.web_search import WebSearch
    from common import ecorex_tool_permissions as permissions

    monkeypatch.setattr(ToolManager, "_instance", None)
    monkeypatch.setattr(WebSearch, "is_available", staticmethod(lambda: False))
    monkeypatch.setattr(web_search, "configured_providers", lambda: [])
    manager = ToolManager()
    manager.load_tools(start_mcp=False)
    initializer = AgentInitializer(SimpleNamespace(), SimpleNamespace())
    tools = initializer._load_tools(str(tmp_path), None, [], "cow-hotpath-catalog")
    names = {tool.name for tool in tools}

    assert {"search_files", "web_search"} <= names
    assert names.isdisjoint(
        {"ecorex_cli", "host_diagnostics", "optional_abilities", "agent_capability"}
    )
    assert names == set(manager.list_tools())
    token = permissions.bind_cow_direct_tools()
    try:
        failure = next(tool for tool in tools if tool.name == "web_search").execute(
            {"query": "current news"}
        )
    finally:
        permissions.reset_cow_direct_tools(token)
    assert failure.status == "error"
    assert "No search provider configured" in str(failure.result)


def test_local_skill_enablement_does_not_read_enterprise_projection(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skills" / "local-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: local-report\ndescription: Local report workflow.\n---\n",
        encoding="utf-8",
    )
    manager = SkillManager(builtin_dir=str(tmp_path / "builtin"), custom_dir=str(tmp_path / "skills"))
    manager.skills_config["local-report"]["enabled"] = True
    monkeypatch.setattr(
        "ecorex.extensions.live_authority.live_skill_enabled",
        lambda _name: False,
    )

    assert manager.is_skill_enabled("local-report") is True
    assert "local-report" in {entry.skill.name for entry in manager.filter_skills()}


def test_upstream_mcp_oauth_refresh_notifies_dynamic_reload(monkeypatch) -> None:
    from agent.tools.mcp import mcp_oauth
    from agent.tools.mcp.mcp_client import (
        notify_server_authorized,
        set_reload_callback,
    )

    records = {
        "fixture": {
            "metadata": {"token_endpoint": "https://auth.example/token"},
            "client_id": "client",
            "access_token": "expired",
            "refresh_token": "refresh",
            "expires_at": 1,
        }
    }
    monkeypatch.setattr(
        mcp_oauth,
        "load_server_record",
        lambda name: dict(records.get(name, {})),
    )
    monkeypatch.setattr(
        mcp_oauth,
        "save_server_record",
        lambda name, record: records.__setitem__(name, dict(record)),
    )
    monkeypatch.setattr(
        mcp_oauth,
        "_http_post_form",
        lambda _url, fields: {
            "access_token": "fresh",
            "refresh_token": fields["refresh_token"],
            "expires_in": 3600,
        },
    )
    handler = mcp_oauth.OAuthHandler(
        "fixture",
        "https://mcp.example/session",
        "http://127.0.0.1:9899/mcp/oauth/callback",
    )

    assert handler.get_valid_access_token() == "fresh"
    reloaded = []
    set_reload_callback(reloaded.append)
    try:
        notify_server_authorized("fixture")
        assert reloaded == ["fixture"]
    finally:
        set_reload_callback(None)
    assert callable(getattr(ToolManager(), "reload_mcp_server"))


def test_public_parent_turn_runs_subagent_through_same_managed_gateway(
    tmp_path: Path, monkeypatch,
) -> None:
    child_done = threading.Event()

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            response_id = f"subagent-response-{len(self.requests)}"
            if ":subagent-" in request.request_id:
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta="child result",
                )
                child_done.set()
            elif sum(
                ":subagent-" not in item.request_id for item in self.requests
            ) == 1:
                yield GatewayEvent(
                    seq=1,
                    event_type="tool_call.requested",
                    response_id=response_id,
                    tool_call_id="start-child",
                    tool_name="subagent",
                    arguments={
                        "action": "start",
                        "task": "return the child proof",
                        "name": "proof",
                    },
                )
            else:
                assert await asyncio.to_thread(child_done.wait, 3)
                yield GatewayEvent(
                    seq=1,
                    event_type="output_text.delta",
                    response_id=response_id,
                    delta="parent done",
                )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id=response_id,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    monkeypatch.setattr(
        "bridge.bridge.Bridge",
        lambda: SimpleNamespace(
            get_agent_bridge=lambda: (_ for _ in ()).throw(
                AssertionError("managed subagent must not construct the legacy bridge")
            )
        ),
    )
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="subagent"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Delegate a child proof",
            client_message_id="cow-hotpath-subagent-turn",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = Gateway()
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        workspace_root=tmp_path / "workspace",
    )

    result = asyncio.run(worker.run_once("cow-hotpath-subagent-worker"))

    assert result.outcome is WorkerOutcome.COMPLETED
    child_requests = [
        request for request in gateway.requests if ":subagent-" in request.request_id
    ]
    assert len(child_requests) == 1
    assert child_requests[0].thread_id == thread.thread_id
    assert child_requests[0].turn_id == created.turn.turn_id
    usage_events = [
        event
        for event in kernel.events.page(thread.thread_id, limit=100).events
        if event.event_type == "model.response_completed"
    ]
    assert len(usage_events) == 3


def test_cow_direct_context_reaches_image_batch_child_threads(monkeypatch) -> None:
    from common import ecorex_tool_permissions as permissions

    tool = ImageGenTool()
    original_execute = tool.execute
    observed = []

    def execute(params):
        if params.get("tasks"):
            return original_execute(params)
        observed.append(permissions.get_tool_permission_broker())
        return ToolResult.success({"images": []})

    monkeypatch.setattr(tool, "execute", execute)
    token = permissions.bind_cow_direct_tools()
    try:
        result = tool.execute(
            {
                "tasks": [{"prompt": "one"}, {"prompt": "two"}],
                "max_parallel": 2,
            }
        )
    finally:
        permissions.reset_cow_direct_tools(token)

    assert result.status == "success"
    assert observed == [permissions._COW_DIRECT_BROKER] * 2


def test_official_scheduler_is_default_and_isolated_per_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.scheduler import integration

    bridge = SimpleNamespace(scheduler_initialized=False)
    workspaces = [tmp_path / "project-a", tmp_path / "project-b"]
    tools = [[SchedulerTool()], [SchedulerTool()]]
    monkeypatch.setattr(integration, "conf", lambda: {"scheduler_enabled": False})

    initializer = AgentInitializer(SimpleNamespace(), bridge)
    for workspace, workspace_tools in zip(workspaces, tools):
        initializer._initialize_scheduler(
            workspace_tools, str(workspace), "cow-hotpath-scheduler"
        )

    try:
        assert tools[0][0].task_store is not None
        assert tools[1][0].task_store is not None
        assert tools[0][0].task_store is not tools[1][0].task_store
        assert tools[0][0].scheduler_service.running is True
        assert tools[1][0].scheduler_service.running is True
        assert Path(tools[0][0].task_store.store_path) == workspaces[0] / "scheduler" / "tasks.json"
        assert Path(tools[1][0].task_store.store_path) == workspaces[1] / "scheduler" / "tasks.json"
        assert tools[0][0].execute({"action": "list"}).status == "success"
        assert tools[1][0].execute({"action": "list"}).status == "success"
    finally:
        tools[0][0].scheduler_service.stop()
        tools[1][0].scheduler_service.stop()


def test_product_scheduler_binding_is_reused_by_cow_initializer(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.scheduler import integration

    workspace = tmp_path / "workspace"
    store = TaskStore(str(workspace / "scheduler" / "tasks.json"))
    service = SchedulerService(store, lambda _task: True)
    integration.bind_scheduler_runtime(store, service, workspace)

    def fail_if_initialized(*_args, **_kwargs):
        raise AssertionError("started a second scheduler")

    monkeypatch.setattr(
        integration,
        "init_scheduler",
        fail_if_initialized,
    )
    tool = SchedulerTool({"channel_type": "web"})

    try:
        AgentInitializer(SimpleNamespace(), SimpleNamespace(scheduler_initialized=False))._initialize_scheduler(
            [tool], str(workspace), "cow-scheduler-singleton"
        )
        assert tool.task_store is store
        assert tool.scheduler_service is service
        assert integration.get_task_store(workspace) is store
        assert integration.get_scheduler_service(workspace) is service
    finally:
        integration.bind_scheduler_runtime(None, None, workspace)


def test_scheduler_executes_without_legacy_permission_broker(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.scheduler import integration
    from bridge.context import Context
    from common import ecorex_tool_permissions as permissions

    monkeypatch.setattr(
        permissions,
        "get_tool_permission_broker",
        lambda: (_ for _ in ()).throw(AssertionError("legacy broker consulted")),
    )
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    tool = SchedulerTool({"channel_type": "web"})
    tool.task_store = store
    tool.current_context = Context(
        kwargs={
            "thread_id": "thread-1",
            "session_id": "session-1",
            "receiver": "receiver-1",
        }
    )
    created = tool.execute(
        {
            "action": "create",
            "name": "direct Cow scheduler",
            "message": "run once",
            "schedule_type": "once",
            "schedule_value": "+5m",
        }
    )
    assert created.status == "success"

    executed = []
    monkeypatch.setattr(integration, "_is_channel_ready", lambda *_args: True)
    monkeypatch.setattr(
        integration,
        "_execute_send_message",
        lambda task, _bridge: executed.append(task["id"]) or True,
    )
    task = store.list_tasks()[0]
    assert integration._execute_scheduled_task(task, SimpleNamespace()) is True
    assert executed == [task["id"]]


def test_public_worker_passes_channel_delivery_context_to_scheduler(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.scheduler import integration

    observed: list[tuple[str, str]] = []
    original = integration.attach_scheduler_to_tool

    def capture(tool, context):
        observed.append((context.get("channel_type"), context.get("receiver")))
        return original(tool, context)

    monkeypatch.setattr(integration, "attach_scheduler_to_tool", capture)
    app = create_app(settings=RuntimeSettings(database_path=tmp_path / "runtime.db"))
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="channel scheduler"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="Schedule this for the current channel",
            metadata={
                "channel": {
                    "channel_type": "feishu",
                    "receiver": "chat-42",
                }
            },
            client_message_id="cow-hotpath-channel-context",
        )
    )
    kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )

    class Gateway:
        async def stream(self, _request):
            yield GatewayEvent(
                seq=1,
                event_type="output_text.delta",
                response_id="channel-context-response",
                delta="scheduled",
            )
            yield GatewayEvent(
                seq=2,
                event_type="response.completed",
                response_id="channel-context-response",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    result = asyncio.run(
        AgentTurnWorker(kernel, gateway=Gateway()).run_once(
            "cow-hotpath-channel-worker"
        )
    )

    assert result.outcome is WorkerOutcome.COMPLETED
    assert ("feishu", "chat-42") in observed
