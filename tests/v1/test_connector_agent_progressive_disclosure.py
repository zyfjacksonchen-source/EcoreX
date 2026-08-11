from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from ecorex.capabilities import (
    SandboxLevel,
    ToolExecutionScope,
    ToolInvocationContext,
)
from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorInvocationUncertain,
    ConnectorPermissionDenied,
    InMemoryCredentialVault,
)
from ecorex.gateway import GatewayEvent
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, InteractionKind
from ecorex.runtime import (
    AgentTurnWorker,
    RuntimeSettings,
    WorkerOutcome,
    create_app,
)


class _ConnectorAdapter:
    def __init__(self, connector_id: str, scopes: frozenset[str]) -> None:
        self.connector_id = connector_id
        self.scopes = scopes
        self.account_index = 0
        self.invocations: list[tuple[str, dict[str, Any], str | None]] = []
        self.fail_write = False

    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        del return_uri
        return AuthChallenge(
            flow_id=flow_id,
            connector_id=self.connector_id,
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example.test/start"
                f"?state={state}&code_challenge={code_challenge}"
                f"&code_challenge_method={code_challenge_method}"
            ),
        )

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        del flow_id
        assert response["state"] == private_state["state"]
        self.account_index += 1
        return AuthGrant(
            account_subject=f"{self.connector_id}-subject-{self.account_index}",
            account_display_name=f"{self.connector_id}-账号-{self.account_index}",
            granted_scopes=self.scopes,
            credential_material={"access_token": f"secret-{self.account_index}"},
        )

    async def check_health(
        self, credentials: Mapping[str, str]
    ) -> ConnectorHealthResult:
        assert credentials["access_token"].startswith("secret-")
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        assert credentials["access_token"].startswith("secret-")
        self.invocations.append((action_id, dict(inputs), idempotency_key))
        if self.fail_write and action_id.endswith(".write"):
            raise RuntimeError("provider-secret-must-not-escape")
        return {
            "ok": True,
            "action_id": action_id,
            "title": str(inputs.get("title") or inputs.get("document_id") or "结果"),
        }

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        del credentials, idempotency_key
        return True


class _ScriptedGateway:
    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        self.scripts = list(scripts)

    async def stream(self, _request):
        for value in self.scripts.pop(0):
            yield GatewayEvent.model_validate(value)


class _DisclosureWriteGateway:
    """Drive the exact Search -> Describe -> Write model protocol."""

    def __init__(self) -> None:
        self.step = 0
        self.discovery_id: str | None = None

    async def stream(self, request):
        if self.step == 0:
            event = {
                "seq": 1,
                "event_type": "tool_call.requested",
                "response_id": "response-search-write-approval",
                "tool_call_id": "approval-search",
                "tool_name": "connector_search",
                "arguments": {"query": "编辑飞书文档", "limit": 10},
            }
        elif self.step == 1:
            result = request.tool_outputs[0].output
            candidate = next(
                item
                for item in result["actions"]
                if item["action_id"] == "documents.write"
            )
            self.discovery_id = str(candidate["discovery_id"])
            event = {
                "seq": 1,
                "event_type": "tool_call.requested",
                "response_id": "response-describe-write-approval",
                "tool_call_id": "approval-describe",
                "tool_name": "connector_describe",
                "arguments": {"discovery_id": self.discovery_id},
            }
        else:
            assert self.discovery_id is not None
            event = {
                "seq": 1,
                "event_type": "tool_call.requested",
                "response_id": "response-call-write-approval",
                "tool_call_id": "approval-write",
                "tool_name": "connector_write",
                "arguments": {
                    "discovery_id": self.discovery_id,
                    "input": {"document_id": "doc-approval", "title": "正式方案"},
                },
            }
        self.step += 1
        yield GatewayEvent.model_validate(event)


def _runtime(
    tmp_path: Path,
    *,
    feishu: _ConnectorAdapter | None = None,
    tencent: _ConnectorAdapter | None = None,
    vault: InMemoryCredentialVault | None = None,
    full_access: bool = False,
):
    adapters = {}
    if feishu is not None:
        adapters["feishu"] = feishu
    if tencent is not None:
        adapters["tencent-docs"] = tencent
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            connector_adapters=adapters,
            connector_vault=vault or InMemoryCredentialVault(),
            full_access=full_access,
        )
    )
    return app, app.state.connector_composition.service


def _connect(service, connector_id: str):
    return_uri = next(iter(service.allowed_return_uris))
    challenge = asyncio.run(
        service.begin_connect(
            connector_id,
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri=return_uri,
        )
    )
    assert challenge.authorization_url is not None
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    return asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))


def _turn(app, text: str, suffix: str = "one"):
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="connector-agent"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input=text,
            client_message_id=f"connector-message-{suffix}",
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
    return kernel, composition, thread, created, prepared, batch


def _context(thread, created, prepared, batch, *, tool_id: str, call_id: str):
    return ToolInvocationContext(
        invocation_id=f"invoke-{call_id}",
        capability_snapshot_id=prepared.snapshot_context.capability_snapshot_id,
        policy_snapshot_id=prepared.snapshot_context.permission_snapshot_id,
        tool_id=tool_id,
        idempotency_key=f"{created.turn.turn_id}:{call_id}",
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        execution_scope=ToolExecutionScope(
            job_id=created.job.job_id,
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            execution_batch_id=batch.batch_id,
        ),
        tool_call_id=call_id,
    )


def _search(composition, thread, created, prepared, batch, query: str):
    repository = composition.tool_execution_repository
    arguments = {"query": query, "limit": 50}
    context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_search",
        call_id="connector-search",
    )
    repository.begin(
        tool_call_id="connector-search",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="connector_search",
        arguments=arguments,
        idempotency_key=None,
    )
    result = composition.connector_agent_runtime.search(arguments, context)
    repository.complete("connector-search", result)
    return result


def _describe(
    composition,
    thread,
    created,
    prepared,
    batch,
    discovery_id: str,
):
    repository = composition.tool_execution_repository
    arguments = {"discovery_id": discovery_id}
    context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_describe",
        call_id="connector-describe",
    )
    repository.begin(
        tool_call_id="connector-describe",
        job_id=created.job.job_id,
        turn_id=created.turn.turn_id,
        execution_batch_id=batch.batch_id,
        capability_snapshot_id=context.capability_snapshot_id,
        policy_snapshot_id=context.policy_snapshot_id,
        tool_id="connector_describe",
        arguments=arguments,
        idempotency_key=None,
    )
    result = composition.connector_agent_runtime.describe(arguments, context)
    repository.complete("connector-describe", result)
    return result


def _adapter_pair():
    feishu = _ConnectorAdapter(
        "feishu",
        frozenset(
            {
                "docx:document:readonly",
                "docx:document",
                "drive:drive:readonly",
                "im:message",
            }
        ),
    )
    tencent = _ConnectorAdapter(
        "tencent-docs",
        frozenset({"docs.read", "docs.write"}),
    )
    return feishu, tencent


def _headers(app, *, mutation: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {app.state.runtime_bearer_token}"}
    if mutation:
        headers.update(
            {
                "Origin": "http://127.0.0.1:8765",
                "X-EcoreX-CSRF": app.state.csrf_token,
            }
        )
    return headers


def _create_login_interaction(app, text: str = "使用飞书编辑文档"):
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="connector-login-e2e"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input=text,
            client_message_id="connector-login-e2e-message",
        )
    )
    kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _ScriptedGateway(
        [[{
            "seq": 1,
            "event_type": "tool_call.requested",
            "response_id": "response-login-e2e",
            "tool_call_id": "provider-connector-search-e2e",
            "tool_name": "connector_search",
            "arguments": {"query": "飞书文档", "limit": 10},
        }]]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        turn_preparer=composition.prepare_turn,
        permission_mutation_lock=composition.permission_mutation_lock,
        permission_account_id=composition.permission_account_id,
    )
    outcome = asyncio.run(worker.run_once("connector-login-e2e-worker"))
    assert outcome.outcome is WorkerOutcome.WAITING_HUMAN
    interaction = kernel.list_interactions(thread.thread_id).interactions[0]
    return thread, interaction


def test_connector_search_uses_exact_instance_action_contract_ids(tmp_path: Path) -> None:
    feishu, tencent = _adapter_pair()
    app, service = _runtime(tmp_path, feishu=feishu, tencent=tencent)
    first = _connect(service, "feishu")
    second = _connect(service, "feishu")
    tencent_instance = _connect(service, "tencent-docs")
    _kernel, composition, thread, created, prepared, batch = _turn(
        app,
        "使用飞书和腾讯文档读取文档",
    )

    result = _search(composition, thread, created, prepared, batch, "文档")

    discoveries = {item["discovery_id"] for item in result["actions"]}
    assert any(value.startswith(f"connector:{first.instance_id}@feishu/") for value in discoveries)
    assert any(value.startswith(f"connector:{second.instance_id}@feishu/") for value in discoveries)
    assert any(
        value.startswith(f"connector:{tencent_instance.instance_id}@tencent-docs/")
        for value in discoveries
    )
    assert all(len(value.rpartition("@")[2]) == 64 for value in discoveries)
    assert "credential_ref" not in json.dumps(result, ensure_ascii=False)
    assert "access_token" not in json.dumps(result, ensure_ascii=False)


def test_exact_describe_grant_survives_restart_but_not_cross_batch(tmp_path: Path) -> None:
    vault = InMemoryCredentialVault()
    feishu, _tencent = _adapter_pair()
    app, service = _runtime(tmp_path, feishu=feishu, vault=vault)
    _connect(service, "feishu")
    _kernel, composition, thread, created, prepared, batch = _turn(
        app,
        "使用飞书读取文档",
    )
    search = _search(composition, thread, created, prepared, batch, "读取飞书文档")
    candidate = next(
        item for item in search["actions"] if item["action_id"] == "documents.read"
    )
    described = _describe(
        composition,
        thread,
        created,
        prepared,
        batch,
        candidate["discovery_id"],
    )
    assert described["action"]["input_schema"]["required"] == ["document_id"]

    call_context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_read",
        call_id="connector-read",
    )
    result = asyncio.run(
        composition.connector_agent_runtime.read(
            {
                "discovery_id": candidate["discovery_id"],
                "input": {"document_id": "doc-1"},
            },
            call_context,
        )
    )
    assert result["delivery"] == "inline"
    assert result["data"]["ok"] is True

    _kernel2, _composition2, thread2, created2, prepared2, batch2 = _turn(
        app,
        "另一个批次读取飞书文档",
        "two",
    )
    forged_context = _context(
        thread2,
        created2,
        prepared2,
        batch2,
        tool_id="connector_read",
        call_id="connector-read-cross-batch",
    )
    with pytest.raises(ConnectorPermissionDenied, match="not disclosed"):
        asyncio.run(
            composition.connector_agent_runtime.read(
                {
                    "discovery_id": candidate["discovery_id"],
                    "input": {"document_id": "doc-2"},
                },
                forged_context,
            )
        )

    restarted, _service = _runtime(tmp_path, feishu=feishu, vault=vault)
    restarted_runtime = restarted.state.runtime_composition.connector_agent_runtime
    replay = asyncio.run(
        restarted_runtime.read(
            {
                "discovery_id": candidate["discovery_id"],
                "input": {"document_id": "doc-1"},
            },
            call_context,
        )
    )
    assert replay == result


def test_write_is_idempotent_scope_fenced_and_uncertain_is_observable(tmp_path: Path) -> None:
    feishu, _tencent = _adapter_pair()
    app, service = _runtime(tmp_path, feishu=feishu)
    instance = _connect(service, "feishu")
    _kernel, composition, thread, created, prepared, batch = _turn(
        app,
        "使用飞书编辑文档",
    )
    search = _search(composition, thread, created, prepared, batch, "编辑飞书文档")
    candidate = next(
        item for item in search["actions"] if item["action_id"] == "documents.write"
    )
    _describe(composition, thread, created, prepared, batch, candidate["discovery_id"])
    context = _context(
        thread,
        created,
        prepared,
        batch,
        tool_id="connector_write",
        call_id="connector-write-stable",
    )
    arguments = {
        "discovery_id": candidate["discovery_id"],
        "input": {"document_id": "doc-1", "title": "正式方案"},
    }
    first = asyncio.run(composition.connector_agent_runtime.write(arguments, context))
    replay = asyncio.run(composition.connector_agent_runtime.write(arguments, context))
    assert first == replay
    writes = [value for value in feishu.invocations if value[0] == "documents.write"]
    assert len(writes) == 1
    assert writes[0][2] is not None
    assert len(str(writes[0][2])) < 128

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute(
            "UPDATE connector_runtime_instances SET granted_scopes_json='[]' "
            "WHERE instance_id=?",
            (instance.instance_id,),
        )
    with pytest.raises(ConnectorPermissionDenied, match="scope"):
        asyncio.run(
            composition.connector_agent_runtime.write(
                {
                    **arguments,
                    "input": {"document_id": "doc-2", "title": "不得写入"},
                },
                _context(
                    thread,
                    created,
                    prepared,
                    batch,
                    tool_id="connector_write",
                    call_id="connector-write-scope-revoked",
                ),
            )
        )

    # Restore the scope, then force a provider acknowledgement loss.  The
    # exception is explicitly classified for the Worker's durable HITL path.
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute(
            "UPDATE connector_runtime_instances SET granted_scopes_json=? "
            "WHERE instance_id=?",
            (json.dumps(sorted(feishu.scopes)), instance.instance_id),
        )
    feishu.fail_write = True
    with pytest.raises(ConnectorInvocationUncertain) as uncertain:
        asyncio.run(
            composition.connector_agent_runtime.write(
                {
                    **arguments,
                    "input": {"document_id": "doc-3", "title": "未知结果"},
                },
                _context(
                    thread,
                    created,
                    prepared,
                    batch,
                    tool_id="connector_write",
                    call_id="connector-write-uncertain",
                ),
            )
        )
    assert uncertain.value.side_effect_uncertain is True
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        payloads = [
            json.loads(str(row[0]))
            for row in connection.execute(
                "SELECT payload_json FROM connector_outbox "
                "WHERE event_type='connector.invocation.outcome_unknown'"
            ).fetchall()
        ]
    assert payloads[-1]["runtime"]["tool_call_id"] == "connector-write-uncertain"
    assert payloads[-1]["runtime"]["execution_batch_id"] == batch.batch_id
    assert "provider-secret" not in json.dumps(payloads)


def test_default_write_approval_is_informed_and_descriptor_swap_fails_closed(
    tmp_path: Path,
) -> None:
    feishu, _tencent = _adapter_pair()
    app, service = _runtime(tmp_path, feishu=feishu)
    instance = _connect(service, "feishu")
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="connector approval"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用飞书编辑文档",
            client_message_id="connector-informed-approval",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _DisclosureWriteGateway()
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        turn_preparer=composition.prepare_turn,
        permission_mutation_lock=composition.permission_mutation_lock,
        permission_account_id=composition.permission_account_id,
    )

    waiting = asyncio.run(worker.run_once("connector-informed-approval-worker"))

    assert waiting.outcome is WorkerOutcome.WAITING_HUMAN
    interaction = kernel.list_interactions(thread.thread_id).interactions[0]
    assert "飞书" in interaction.prompt
    assert instance.account_display_name in interaction.prompt
    assert "编辑飞书文档" in interaction.prompt
    assert "写入外部服务" in interaction.prompt
    checkpoint = kernel.jobs.get(created.job.job_id).checkpoint
    assert checkpoint is not None
    descriptor = checkpoint["connector_approval"]["descriptor"]
    assert descriptor["instance_id"] == instance.instance_id
    assert descriptor["action_id"] == "documents.write"
    assert "input" not in checkpoint["connector_approval"]
    assert feishu.invocations == []

    kernel.respond_interaction(
        interaction.interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="allow-informed-connector-write",
    )
    original = worker.tool_executions.connector_approval_description

    def swapped_description(**kwargs):
        current = original(**kwargs)
        assert current is not None
        return {**current, "action_name": "发送飞书消息"}

    worker.tool_executions.connector_approval_description = swapped_description
    rejected = asyncio.run(worker.run_once("connector-swapped-approval-worker"))

    assert rejected.outcome is WorkerOutcome.FAILED
    assert feishu.invocations == []


def test_explicit_missing_connector_persists_login_hitl_even_in_full_access(
    tmp_path: Path,
) -> None:
    app, _service = _runtime(tmp_path, full_access=True)
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="connector-login"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用飞书读取文档",
            client_message_id="connector-login-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    gateway = _ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "response-connector-login",
                    "tool_call_id": "provider-connector-search",
                    "tool_name": "connector_search",
                    "arguments": {"query": "飞书文档", "limit": 10},
                }
            ]
        ]
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        turn_preparer=composition.prepare_turn,
        permission_mutation_lock=composition.permission_mutation_lock,
        permission_account_id=composition.permission_account_id,
    )

    outcome = asyncio.run(worker.run_once("connector-login-worker"))

    assert outcome.outcome is WorkerOutcome.WAITING_HUMAN
    interactions = kernel.list_interactions(thread.thread_id).interactions
    assert len(interactions) == 1
    interaction = interactions[0]
    assert interaction.kind is InteractionKind.CONNECTOR_LOGIN
    assert interaction.contract.connector is not None
    assert interaction.contract.connector.connector_id == "feishu"
    assert all(
        action.action_type.value != "allow"
        for action in interaction.contract.actions
    )


def test_connector_login_api_atomically_completes_and_refreshes_authority(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    feishu, _tencent = _adapter_pair()
    app, _service = _runtime(tmp_path, feishu=feishu, vault=vault)
    _thread, interaction = _create_login_interaction(app)
    client = TestClient(app)

    before = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert before.status_code == 409

    begun = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
        headers=_headers(app, mutation=True),
    )
    assert begun.status_code == 200
    body = begun.json()
    state = parse_qs(urlsplit(body["authorization_url"]).query)["state"][0]
    pending = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert pending.status_code == 202
    assert pending.json()["state"] == "awaiting_callback"

    callback = client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": state, "code": "provider-code"},
    )
    assert callback.status_code == 200
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.row_factory = sqlite3.Row
        binding = connection.execute(
            "SELECT * FROM connector_interaction_logins "
            "WHERE interaction_id=? ORDER BY generation DESC LIMIT 1",
            (interaction.interaction_id,),
        ).fetchone()
        completion = connection.execute(
            "SELECT * FROM connector_auth_completions WHERE flow_id=?",
            (binding["flow_id"],),
        ).fetchone()
        instance = connection.execute(
            "SELECT lifecycle FROM connector_runtime_instances WHERE instance_id=?",
            (completion["completed_instance_id"],),
        ).fetchone()
    assert binding["status"] == "completed"
    assert instance["lifecycle"] == "active"

    checked = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert checked.status_code == 200
    assert checked.json()["authority_refresh_revision_id"]
    # The RLock must not remain owned by the executor thread used by check.
    lock = app.state.runtime_composition.permission_mutation_lock
    def acquire_and_release() -> bool:
        acquired = lock.acquire(timeout=1)
        if acquired:
            lock.release()
        return acquired
    with ThreadPoolExecutor(max_workers=1) as executor:
        acquired = executor.submit(acquire_and_release).result(timeout=2)
    assert acquired is True


def test_connector_login_wrong_state_and_consumed_crash_are_retryable(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    feishu, _tencent = _adapter_pair()
    app, _service = _runtime(tmp_path, feishu=feishu, vault=vault)
    _thread, interaction = _create_login_interaction(app)
    client = TestClient(app)
    begun = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
        headers=_headers(app, mutation=True),
    ).json()
    binding1 = app.state.connector_composition.repository.interaction_login_binding(
        interaction.interaction_id
    )
    assert binding1 is not None and binding1.flow_id is not None
    with pytest.raises(Exception):
        asyncio.run(
            app.state.connector_composition.service.complete_connect(
                binding1.flow_id, {"state": "wrong"}
            )
        )
    wrong_state_retry = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert wrong_state_retry.status_code == 202
    assert wrong_state_retry.json()["state"] == "authorization_required"

    # Start a fresh interaction and simulate process death after consume_flow.
    app2_path = tmp_path / "crash"
    app2_path.mkdir()
    app2, _service2 = _runtime(app2_path, feishu=feishu, vault=vault)
    _thread2, interaction2 = _create_login_interaction(app2)
    client2 = TestClient(app2)
    begun2 = client2.post(
        f"/api/v1/interactions/{interaction2.interaction_id}/connector-login/begin",
        headers=_headers(app2, mutation=True),
    ).json()
    binding = app2.state.connector_composition.repository.interaction_login_binding(
        interaction2.interaction_id
    )
    assert binding is not None and binding.flow_id is not None
    service2 = app2.state.connector_composition.service
    with service2.control_admission(
        operation="test_consume_flow_crash",
        subject=binding.flow_id,
    ):
        consumption = service2.repository.consume_flow(
            binding.flow_id,
            operation_token="connflowconsume_crash",
            lease_seconds=300,
        )
    assert consumption.reason == "consumed"
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(app2_path / "runtime.db") as connection:
        connection.execute(
            "UPDATE connector_interaction_logins SET operation_lease_expires_at=? "
            "WHERE interaction_id=? AND generation=?",
            (expired, interaction2.interaction_id, binding.generation),
        )
        connection.execute(
            "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
            "WHERE flow_id=?",
            (expired, binding.flow_id),
        )
    restarted, _service3 = _runtime(app2_path, feishu=feishu, vault=vault)
    retry = TestClient(restarted).post(
        f"/api/v1/interactions/{interaction2.interaction_id}/connector-login/check",
        headers=_headers(restarted, mutation=True),
    )
    assert retry.status_code == 202
    assert retry.json()["state"] == "authorization_required"
    assert retry.json()["reason"] == "auth_completion_interrupted"


class _BlockingBeginAdapter(_ConnectorAdapter):
    def __init__(self) -> None:
        super().__init__("feishu", frozenset({"docx:document"}))
        self.entered = threading.Event()
        self.release = threading.Event()
        self.oauth_state: str | None = None

    async def begin_auth(self, **kwargs) -> AuthChallenge:
        self.oauth_state = str(kwargs["state"])
        self.entered.set()
        await asyncio.to_thread(self.release.wait, 5)
        return await super().begin_auth(**kwargs)


def test_connector_login_begin_cancel_fences_late_callback(tmp_path: Path) -> None:
    adapter = _BlockingBeginAdapter()
    vault = InMemoryCredentialVault()
    app, _service = _runtime(tmp_path, feishu=adapter, vault=vault)
    _thread, interaction = _create_login_interaction(app)
    client = TestClient(app, raise_server_exceptions=False)

    with ThreadPoolExecutor(max_workers=1) as executor:
        beginning = executor.submit(
            lambda: client.post(
                f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
                headers=_headers(app, mutation=True),
            )
        )
        assert adapter.entered.wait(3)
        cancelled = client.post(
            f"/api/v1/interactions/{interaction.interaction_id}/connector-login/cancel",
            headers=_headers(app, mutation=True),
        )
        assert cancelled.status_code == 200
        adapter.release.set()
        assert beginning.result(timeout=8).status_code != 200
    late = client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": adapter.oauth_state, "code": "late"},
    )
    assert late.status_code != 200
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM connector_auth_flows "
            "WHERE status='active' AND private_ref IS NOT NULL"
        ).fetchone()[0]
    assert active == 0
    assert all(
        not reference.startswith("ecorex/connector-flow/")
        for reference in vault._values
    )


def test_scope_missing_appends_reauth_generation_and_latest_check_wins(
    tmp_path: Path,
) -> None:
    vault = InMemoryCredentialVault()
    feishu = _ConnectorAdapter(
        "feishu", frozenset({"docx:document:readonly"})
    )
    app, _service = _runtime(tmp_path, feishu=feishu, vault=vault)
    _thread, interaction = _create_login_interaction(app)
    client = TestClient(app)
    begun = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
        headers=_headers(app, mutation=True),
    ).json()
    state = parse_qs(urlsplit(begun["authorization_url"]).query)["state"][0]
    assert client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": state, "code": "provider-code"},
    ).status_code == 200
    insufficient = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert insufficient.status_code == 202
    assert insufficient.json()["state"] == "reauthorization_required"
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        rows = connection.execute(
            "SELECT generation, mode, status FROM connector_interaction_logins "
            "WHERE interaction_id=? ORDER BY generation",
            (interaction.interaction_id,),
        ).fetchall()
    assert rows == [(0, "connect", "completed"), (1, "reauthorize", "reauthorization_required")]

    reauth = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
        headers=_headers(app, mutation=True),
    )
    assert reauth.status_code == 200
    still_pending = client.post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/check",
        headers=_headers(app, mutation=True),
    )
    assert still_pending.status_code == 202
    assert still_pending.json()["state"] == "awaiting_callback"
    restarted, _service2 = _runtime(tmp_path, feishu=feishu, vault=vault)
    replay = TestClient(restarted).post(
        f"/api/v1/interactions/{interaction.interaction_id}/connector-login/begin",
        headers=_headers(restarted, mutation=True),
    )
    assert replay.status_code == 200
    assert replay.json()["authorization_url"] == reauth.json()["authorization_url"]
