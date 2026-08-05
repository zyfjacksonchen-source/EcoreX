from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from ecorex.gateway import GatewayEvent
from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthError,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorInvocationUncertain,
    ConnectorInvocationRecord,
    ConnectorPermissionDenied,
    ConnectorService,
    ConnectorUnavailable,
    InMemoryCredentialVault,
    SQLiteConnectorRepository,
    builtin_connector_registry,
)
from ecorex.protocol import (
    ConnectorLoginCheckResponse,
    CreateThreadRequest,
    CreateTurnRequest,
    InteractionAction,
    InteractionActionStyle,
    InteractionActionType,
    InteractionConnectorContext,
    InteractionContract,
    InteractionKind,
    InteractionStatus,
    TurnStatus,
)
from ecorex.runtime import (
    AgentTurnWorker,
    InteractionStore,
    RuntimeSettings,
    SQLiteDatabase,
    WorkerOutcome,
    create_app,
)


RETURN_URI = "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
RUNTIME_TOKEN = "runtime-connector-crash-fence-token-00000001"
CSRF_TOKEN = "csrf-connector-crash-fence-token-0000000001"


class _RecordingVault(InMemoryCredentialVault):
    def references(self) -> set[str]:
        with self._lock:
            return set(self._values)


class _BarrierAdapter:
    def __init__(self) -> None:
        self.scopes = frozenset(
            {
                "docx:document:readonly",
                "docx:document",
                "drive:drive:readonly",
                "im:message",
            }
        )
        self.account_subject = "crash-fence-account"
        self.begin_started = threading.Event()
        self.begin_release = threading.Event()
        self.block_begin = False
        self.complete_error = False
        self.invocation_started = threading.Event()
        self.invocation_release = threading.Event()
        self.invocation_finished = threading.Event()
        self.block_invocation = False
        self.invoke_raises_after_release = False
        self.invocations: list[tuple[str, Mapping[str, Any], str | None]] = []
        self.revoke_count = 0
        self._credential_generation = 0

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
        assert return_uri == RETURN_URI
        assert code_challenge
        assert code_challenge_method == "S256"
        self.begin_started.set()
        if self.block_begin:
            released = await asyncio.to_thread(self.begin_release.wait, 5)
            if not released:
                raise RuntimeError("test begin barrier timed out")
        return AuthChallenge(
            flow_id=flow_id,
            connector_id="feishu",
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example.test/authorize"
                f"?state={state}&code_challenge={code_challenge}"
                "&code_challenge_method=S256"
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
        assert private_state["pkce_verifier"]
        if self.complete_error:
            raise RuntimeError("provider completion failed after consume")
        self._credential_generation += 1
        return AuthGrant(
            account_subject=self.account_subject,
            account_display_name="围栏测试账号",
            granted_scopes=self.scopes,
            credential_material={
                "access_token": f"SECRET-GENERATION-{self._credential_generation}"
            },
        )

    async def check_health(
        self, credentials: Mapping[str, str]
    ) -> ConnectorHealthResult:
        assert credentials["access_token"].startswith("SECRET-GENERATION-")
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        assert credentials["access_token"].startswith("SECRET-GENERATION-")
        self.invocations.append((action_id, dict(inputs), idempotency_key))
        self.invocation_started.set()
        try:
            if self.block_invocation:
                released = await asyncio.to_thread(self.invocation_release.wait, 5)
                if not released:
                    raise RuntimeError("test invocation barrier timed out")
            if self.invoke_raises_after_release:
                raise RuntimeError("provider confirmed no write after timeout")
            return {"ok": True, "title": str(inputs.get("title", "完成"))}
        finally:
            self.invocation_finished.set()

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        assert credentials["access_token"].startswith("SECRET-GENERATION-")
        assert idempotency_key.startswith("ecorex-disconnect:")
        self.revoke_count += 1
        return True


class _ScriptedGateway:
    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        self.scripts = list(scripts)

    async def stream(self, _request):
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
        for payload in script:
            yield GatewayEvent.model_validate(payload)


def _service(
    database: Path,
    *,
    adapter: _BarrierAdapter | None = None,
    vault: _RecordingVault | None = None,
    timeout: float = 1.0,
) -> tuple[ConnectorService, _BarrierAdapter, _RecordingVault]:
    resolved_adapter = adapter or _BarrierAdapter()
    resolved_vault = vault or _RecordingVault()
    service = ConnectorService(
        builtin_connector_registry({"feishu": resolved_adapter}),
        allowed_return_uris=frozenset({RETURN_URI}),
        vault=resolved_vault,
        repository=SQLiteConnectorRepository(database),
        adapter_timeout_seconds=timeout,
        reauthorization_drain_timeout=0.1,
    )
    return service, resolved_adapter, resolved_vault


def test_catalog_is_pure_and_maintenance_owns_expired_flow_cleanup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog-read-only.db"
    service, _adapter, vault = _service(database)
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri=RETURN_URI,
        )
    )
    expired = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    with sqlite3.connect(database) as connection:
        private_ref = str(
            connection.execute(
                "SELECT private_ref FROM connector_auth_flows WHERE flow_id=?",
                (challenge.flow_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE connector_auth_flows SET expires_at=?, "
            "operation_lease_expires_at=? WHERE flow_id=?",
            (expired, expired, challenge.flow_id),
        )
        before = connection.execute(
            "SELECT * FROM connector_auth_flows WHERE flow_id=?",
            (challenge.flow_id,),
        ).fetchone()
        outbox_before = int(
            connection.execute("SELECT COUNT(*) FROM connector_outbox").fetchone()[0]
        )
    assert private_ref in vault.references()

    service.catalog()

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT * FROM connector_auth_flows WHERE flow_id=?",
            (challenge.flow_id,),
        ).fetchone()
        outbox_after = int(
            connection.execute("SELECT COUNT(*) FROM connector_outbox").fetchone()[0]
        )
    assert after == before
    assert outbox_after == outbox_before
    assert private_ref in vault.references()

    asyncio.run(service.maintenance_once())
    assert private_ref not in vault.references()


def _state(challenge: AuthChallenge) -> str:
    assert challenge.authorization_url is not None
    return parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]


def _connect(service: ConnectorService):
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri=RETURN_URI,
        )
    )
    return asyncio.run(
        service.complete_connect(challenge.flow_id, {"state": _state(challenge)})
    )


def _reserve_login(
    service: ConnectorService,
    interaction_id: str,
    *,
    mode: str = "connect",
    target_instance_id: str | None = None,
):
    database = SQLiteDatabase(service.repository.database)
    store = InteractionStore(database)
    with database.reader() as connection:
        exists = connection.execute(
            "SELECT 1 FROM interactions WHERE interaction_id=?",
            (interaction_id,),
        ).fetchone()
    if exists is None:
        with database.transaction() as connection:
            store.create_in_transaction(
                connection,
                kind=InteractionKind.CONNECTOR_LOGIN,
                prompt="需要连接飞书后继续。",
                thread_id=f"thread-{interaction_id}",
                idempotency_key=f"request-{interaction_id}",
                interaction_id=interaction_id,
                contract=_login_contract(
                    state=(
                        "reauthorization_required"
                        if mode == "reauthorize"
                        else "authorization_required"
                    )
                ),
            )
    reservation = service.repository.reserve_interaction_login(
        interaction_id=interaction_id,
        connector_id="feishu",
        mode=mode,
        target_instance_id=target_instance_id,
    )
    assert reservation.outcome == "reserved"
    assert reservation.binding.operation_token is not None
    return reservation


def _begin_bound_login(
    service: ConnectorService,
    interaction_id: str,
    *,
    mode: str = "connect",
    target_instance_id: str | None = None,
):
    reservation = _reserve_login(
        service,
        interaction_id,
        mode=mode,
        target_instance_id=target_instance_id,
    )
    binding = reservation.binding
    interaction_binding = (
        interaction_id,
        binding.generation,
        str(binding.operation_token),
    )
    if mode == "reauthorize":
        assert target_instance_id is not None
        challenge = asyncio.run(
            service.begin_reauthorize(
                target_instance_id,
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id=binding.lifecycle_request_id,
                interaction_binding=interaction_binding,
            )
        )
    else:
        challenge = asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id=binding.lifecycle_request_id,
                interaction_binding=interaction_binding,
            )
        )
    return reservation, challenge


def _login_contract(
    *,
    state: str = "authorization_required",
    required_action_ids: list[str] | None = None,
) -> InteractionContract:
    return InteractionContract(
        title="连接飞书",
        connector=InteractionConnectorContext(
            connector_id="feishu",
            display_name="飞书",
            state=state,
            required_action_ids=required_action_ids or ["documents.read"],
        ),
        actions=[
            InteractionAction(
                action_id="begin_login",
                label="开始连接",
                action_type=InteractionActionType.CONNECTOR_BEGIN_LOGIN,
                style=InteractionActionStyle.PRIMARY,
            ),
            InteractionAction(
                action_id="check_status",
                label="检查连接状态",
                action_type=InteractionActionType.CONNECTOR_CHECK_STATUS,
            ),
            InteractionAction(
                action_id="cancel",
                label="取消",
                action_type=InteractionActionType.CANCEL,
            ),
        ],
    ).validate_for_kind(InteractionKind.CONNECTOR_LOGIN)


def _api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {RUNTIME_TOKEN}",
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF_TOKEN,
    }


def _assert_no_consumable_or_private_flow(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM connector_auth_flows WHERE status='active'"
        ).fetchone()[0]
        private = connection.execute(
            "SELECT COUNT(*) FROM connector_auth_flows WHERE private_ref IS NOT NULL"
        ).fetchone()[0]
    assert active == 0
    assert private == 0


def test_model_begin_cancel_race_resolves_interaction_without_orphan_flow(
    tmp_path: Path,
) -> None:
    """A user cancel linearizes before a concurrently starting model login."""

    database = tmp_path / "runtime.db"
    adapter = _BarrierAdapter()
    adapter.block_begin = True
    vault = _RecordingVault()
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            connector_adapters={"feishu": adapter},
            connector_vault=vault,
        )
    )
    kernel = app.state.runtime
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="使用飞书读取文档",
            client_message_id="connector-crash-race-message",
        ),
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("connector-crash-worker")
    assert leased is not None
    kernel.jobs.start(leased.job_id, "connector-crash-worker", leased.lease_token)
    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="connector-crash-worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.CONNECTOR_LOGIN,
        prompt="需要连接飞书后继续。",
        contract=_login_contract(),
        idempotency_key="connector-crash-login-interaction",
    )

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            begin_task = asyncio.create_task(
                client.post(
                    f"/api/v1/interactions/{interaction.interaction_id}/"
                    "connector-login/begin",
                    headers=_api_headers(),
                )
            )
            assert await asyncio.to_thread(adapter.begin_started.wait, 5)
            try:
                cancelled = await client.post(
                    f"/api/v1/interactions/{interaction.interaction_id}/"
                    "connector-login/cancel",
                    headers=_api_headers(),
                )
            finally:
                adapter.begin_release.set()
            begun = await begin_task
            return cancelled, begun

    cancelled, begun = asyncio.run(scenario())
    assert cancelled.status_code == 200
    assert begun.status_code >= 400
    persisted = kernel.interactions.get(interaction.interaction_id)
    assert persisted.status is InteractionStatus.RESOLVED
    assert persisted.response is not None
    assert persisted.response.action_id == "cancel"
    binding = app.state.connector_composition.repository.interaction_login_binding(
        interaction.interaction_id
    )
    assert binding is not None and binding.status == "cancelled"
    _assert_no_consumable_or_private_flow(database)
    assert not any(ref.startswith("ecorex/connector-flow/") for ref in vault.references())

    restarted = SQLiteConnectorRepository(database)
    recovered = restarted.interaction_login_binding(interaction.interaction_id)
    assert recovered is not None and recovered.status == "cancelled"


def test_completed_oauth_check_releases_permission_lock_and_is_replayable(
    tmp_path: Path,
) -> None:
    """The successful check path must not release an RLock from another thread."""

    database = tmp_path / "oauth-check-lock.db"
    adapter = _BarrierAdapter()
    vault = _RecordingVault()
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            connector_adapters={"feishu": adapter},
            connector_vault=vault,
        )
    )
    kernel = app.state.runtime
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="使用飞书读取文档",
            client_message_id="connector-oauth-check-message",
        ),
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("connector-oauth-check-worker")
    assert leased is not None
    kernel.jobs.start(
        leased.job_id,
        "connector-oauth-check-worker",
        leased.lease_token,
    )
    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="connector-oauth-check-worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.CONNECTOR_LOGIN,
        prompt="需要连接飞书后继续。",
        contract=_login_contract(required_action_ids=["documents.read"]),
        idempotency_key="connector-oauth-check-interaction",
    )

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            begun = await client.post(
                f"/api/v1/interactions/{interaction.interaction_id}/"
                "connector-login/begin",
                headers=_api_headers(),
            )
            assert begun.status_code == 200
            state = parse_qs(urlsplit(begun.json()["authorization_url"]).query)[
                "state"
            ][0]
            callback = await client.get(
                "/api/v1/connectors/oauth/callback",
                params={"state": state, "code": "provider-code"},
                headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"},
            )
            assert callback.status_code == 200
            checked = await asyncio.wait_for(
                client.post(
                    f"/api/v1/interactions/{interaction.interaction_id}/"
                    "connector-login/check",
                    headers=_api_headers(),
                ),
                timeout=3,
            )
            bootstrap = await client.get(
                "/api/v1/bootstrap",
                headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"},
            )
            revision = int(bootstrap.json()["permissions"]["revision"])
            permission = await asyncio.wait_for(
                client.put(
                    "/api/v1/settings/permissions",
                    json={
                        "profile": "full_access",
                        "expected_revision": revision,
                        "client_request_id": "permission-after-connector-check",
                    },
                    headers=_api_headers(),
                ),
                timeout=3,
            )
            replay = await asyncio.wait_for(
                client.post(
                    f"/api/v1/interactions/{interaction.interaction_id}/"
                    "connector-login/check",
                    headers=_api_headers(),
                ),
                timeout=3,
            )
            return checked, permission, replay

    checked, permission, replay = asyncio.run(scenario())
    assert checked.status_code == 200
    assert checked.json()["connected"] is True
    assert checked.json()["authority_refresh_revision_id"]
    assert permission.status_code == 200
    assert permission.json()["permissions"]["full_access"] is True
    assert replay.status_code == 200
    assert replay.json()["authority_refresh_revision_id"] == checked.json()[
        "authority_refresh_revision_id"
    ]
    replay_mutation = replay.json()["mutation"]
    assert replay_mutation["interaction"]["interaction_id"] == (
        interaction.interaction_id
    )
    assert replay_mutation["turn"]["turn_id"] == created.turn.turn_id
    assert set(replay_mutation["job"]) == {
        "job_id",
        "kind",
        "status",
        "priority",
        "attempt",
        "max_attempts",
        "thread_id",
        "turn_id",
        "available_at",
        "deadline",
        "reason_code",
        "created_at",
        "updated_at",
    }
    assert replay_mutation["job"]["job_id"] == leased.job_id
    assert {
        "payload",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
        "checkpoint",
        "idempotency_key",
        "last_error",
    }.isdisjoint(replay_mutation["job"])
    cross_thread = replay.json()
    cross_thread["mutation"]["job"]["thread_id"] = "thread-wrong"
    with pytest.raises(ValidationError, match="another Thread"):
        ConnectorLoginCheckResponse.model_validate(cross_thread)


@pytest.mark.parametrize("boundary", ["flow_commit", "vault_put"])
def test_cancel_during_begin_commit_boundaries_scrubs_flow_and_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Flow DB commit and vault put are both before the consumable-flow fence."""

    database = tmp_path / f"begin-{boundary}.db"
    service, adapter, vault = _service(database)
    interaction_id = f"hitl-begin-{boundary}"
    reservation = _reserve_login(service, interaction_id)
    binding = reservation.binding
    reached = threading.Event()
    release = threading.Event()

    if boundary == "flow_commit":
        original = service.repository.create_preparing_flow

        def gated_create(*args, **kwargs):
            original(*args, **kwargs)
            reached.set()
            if not release.wait(5):
                raise RuntimeError("test flow-commit barrier timed out")

        monkeypatch.setattr(service.repository, "create_preparing_flow", gated_create)
    else:
        original = vault.put

        def gated_put(reference: str, material: Mapping[str, str]) -> None:
            original(reference, material)
            if reference.startswith("ecorex/connector-flow/"):
                reached.set()
                if not release.wait(5):
                    raise RuntimeError("test vault-put barrier timed out")

        monkeypatch.setattr(vault, "put", gated_put)

    def begin() -> AuthChallenge:
        return asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id=binding.lifecycle_request_id,
                interaction_binding=(
                    interaction_id,
                    binding.generation,
                    str(binding.operation_token),
                ),
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(begin)
        assert reached.wait(5)
        asyncio.run(service.cancel_interaction_login(interaction_id))
        release.set()
        with pytest.raises((ConnectorUnavailable, RuntimeError)):
            pending.result(timeout=5)

    current = service.repository.interaction_login_binding(interaction_id)
    assert current is not None and current.status == "cancelled"
    lifecycle = service.repository.lifecycle_request_state(
        binding.lifecycle_request_id
    )
    assert lifecycle is not None and lifecycle["status"] == "failed"
    _assert_no_consumable_or_private_flow(database)
    assert vault.references() == set()
    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    asyncio.run(restarted.maintenance_once())
    _assert_no_consumable_or_private_flow(database)


@pytest.mark.parametrize("mode", ["connect", "reauthorize"])
def test_cancel_at_instance_or_reauth_commit_never_rolls_back_committed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Cancellation loses once account activation and its completion fact commit."""

    database = tmp_path / f"completion-{mode}.db"
    service, adapter, vault = _service(database)
    target = _connect(service) if mode == "reauthorize" else None
    interaction_id = f"hitl-completion-{mode}"
    reservation, challenge = _begin_bound_login(
        service,
        interaction_id,
        mode=mode,
        target_instance_id=(target.instance_id if target is not None else None),
    )
    reached = threading.Event()
    release = threading.Event()
    method_name = "commit_reauthorization" if mode == "reauthorize" else "activate_instance"
    original = getattr(service.repository, method_name)

    def gated_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        reached.set()
        if not release.wait(5):
            raise RuntimeError("test activation-commit barrier timed out")
        return result

    monkeypatch.setattr(service.repository, method_name, gated_commit)

    async def scenario():
        completion = asyncio.create_task(
            service.complete_connect(
                challenge.flow_id,
                {"state": _state(challenge)},
            )
        )
        assert await asyncio.to_thread(reached.wait, 5)
        try:
            with pytest.raises(RuntimeError, match="completion already started"):
                await service.cancel_interaction_login(interaction_id)
        finally:
            release.set()
        return await completion

    completed = asyncio.run(scenario())
    if target is not None:
        assert completed.instance_id == target.instance_id
    current = service.repository.interaction_login_binding(interaction_id)
    assert current is not None
    assert current.generation == reservation.binding.generation
    assert current.status == "completed"
    exact = service.repository.interaction_login_completion(interaction_id)
    assert exact is not None and exact[1] == completed.instance_id
    persisted = service.repository.get_instance(completed.instance_id)
    assert persisted is not None
    assert vault.get(persisted.credential_ref)["access_token"].startswith(
        "SECRET-GENERATION-"
    )

    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    recovered = restarted.repository.interaction_login_completion(interaction_id)
    assert recovered is not None and recovered[1] == completed.instance_id
    assert restarted.repository.get_instance(completed.instance_id) is not None


def test_consumed_flow_adapter_error_becomes_typed_retry_not_stuck_completing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "adapter-error.db"
    service, adapter, vault = _service(database)
    interaction_id = "hitl-consume-adapter-error"
    reservation, challenge = _begin_bound_login(service, interaction_id)
    adapter.complete_error = True

    with pytest.raises(ConnectorAuthError):
        asyncio.run(
            service.complete_connect(
                challenge.flow_id,
                {"state": _state(challenge)},
            )
        )

    failed = service.repository.interaction_login_binding(interaction_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error_code == "connector_auth_error"
    retried = service.repository.reserve_interaction_login(
        interaction_id=interaction_id,
        connector_id="feishu",
        mode="connect",
        target_instance_id=None,
    )
    assert retried.outcome == "reserved"
    assert retried.binding.generation == reservation.binding.generation + 1
    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    assert restarted.repository.interaction_login_binding(interaction_id).status == "starting"
    _assert_no_consumable_or_private_flow(database)


def test_crash_after_flow_consume_expires_to_typed_retry_and_scrubs_private_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "consume-crash.db"
    service, adapter, vault = _service(database)
    interaction_id = "hitl-consume-crash"
    reservation, challenge = _begin_bound_login(service, interaction_id)
    with service.control_admission(
        operation="test_consume_flow_crash",
        subject=challenge.flow_id,
    ):
        consumption = service.repository.consume_flow(
            challenge.flow_id,
            operation_token="connflowconsume_test_crash",
            lease_seconds=30,
        )
    assert consumption.reason == "consumed"
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
            "WHERE flow_id=?",
            (past, challenge.flow_id),
        )
        connection.execute(
            "UPDATE connector_interaction_logins SET operation_lease_expires_at=? "
            "WHERE interaction_id=?",
            (past, interaction_id),
        )

    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    failed = restarted.repository.interaction_login_binding(interaction_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error_code == "auth_completion_interrupted"
    retried = restarted.repository.reserve_interaction_login(
        interaction_id=interaction_id,
        connector_id="feishu",
        mode="connect",
        target_instance_id=None,
    )
    assert retried.outcome == "reserved"
    assert retried.binding.generation == reservation.binding.generation + 1
    _assert_no_consumable_or_private_flow(database)
    assert not any(ref.startswith("ecorex/connector-flow/") for ref in vault.references())


def test_scope_shortfall_creates_new_reauth_generation_and_hides_old_completion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scope-generation.db"
    service, adapter, _vault = _service(database)
    adapter.scopes = frozenset({"drive:drive:readonly"})
    interaction_id = "hitl-scope-generation"
    initial, challenge = _begin_bound_login(service, interaction_id)
    instance = asyncio.run(
        service.complete_connect(
            challenge.flow_id,
            {"state": _state(challenge)},
        )
    )
    old_completion = service.repository.interaction_login_completion(interaction_id)
    assert old_completion is not None and old_completion[1] == instance.instance_id

    required = service.repository.mark_interaction_reauthorization_required(
        interaction_id,
        target_instance_id=instance.instance_id,
        error_code="required_connector_scope_missing",
    )
    assert required.generation == initial.binding.generation + 1
    assert required.status == "reauthorization_required"
    assert service.repository.interaction_login_completion(interaction_id) is None

    adapter.scopes = frozenset(
        {
            "docx:document:readonly",
            "docx:document",
            "drive:drive:readonly",
            "im:message",
        }
    )
    reservation, reauth = _begin_bound_login(
        service,
        interaction_id,
        mode="reauthorize",
        target_instance_id=instance.instance_id,
    )
    assert reservation.binding.generation == required.generation
    assert service.repository.interaction_login_completion(interaction_id) is None
    updated = asyncio.run(
        service.complete_connect(reauth.flow_id, {"state": _state(reauth)})
    )
    exact = service.repository.interaction_login_completion(interaction_id)
    assert exact is not None
    assert exact[0].generation == required.generation
    assert exact[0].flow_id == reauth.flow_id
    assert exact[1] == updated.instance_id == instance.instance_id


@pytest.mark.parametrize(
    ("lifecycle", "resolution", "provider_finishes_with_error"),
    [
        ("disconnect", "manually_reconciled", False),
        ("reauthorize", "confirmed_not_executed", True),
    ],
)
def test_timed_out_adapter_retains_exact_operation_fence_until_reconciliation(
    tmp_path: Path,
    lifecycle: str,
    resolution: str,
    provider_finishes_with_error: bool,
) -> None:
    database = tmp_path / f"timeout-{lifecycle}.db"
    service, adapter, _vault = _service(database, timeout=0.05)
    instance = _connect(service)
    adapter.block_invocation = True
    adapter.invoke_raises_after_release = provider_finishes_with_error
    reauth_challenge = None
    if lifecycle == "reauthorize":
        reauth_challenge = asyncio.run(
            service.begin_reauthorize(
                instance.instance_id,
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id="timeout-reauth-first",
            )
        )

    async def scenario():
        uncertain: ConnectorInvocationUncertain | None = None
        try:
            await service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "超时写入"},
                idempotency_key=f"timeout-write-{lifecycle}",
            )
        except ConnectorInvocationUncertain as error:
            uncertain = error
        assert uncertain is not None and uncertain.invocation_id is not None
        assert adapter.invocation_started.is_set()
        assert not adapter.invocation_finished.is_set()
        with pytest.raises(RuntimeError, match="still executing"):
            service.repository.resolve_uncertain_invocation(
                uncertain.invocation_id,
                resolution,
                wait_seconds=0,
            )

        lifecycle_error: Exception | None = None
        try:
            if lifecycle == "disconnect":
                await service.disconnect(
                    instance.instance_id,
                    drain_timeout=0.01,
                    client_request_id="timeout-disconnect-first",
                )
            else:
                assert reauth_challenge is not None
                await service.complete_connect(
                    reauth_challenge.flow_id,
                    {"state": _state(reauth_challenge)},
                )
        except Exception as error:  # asserted after the provider thread is released
            lifecycle_error = error
        finally:
            adapter.invocation_release.set()
        assert await asyncio.to_thread(adapter.invocation_finished.wait, 5)
        watchers = tuple(service._uncertain_watchers)
        if watchers:
            await asyncio.gather(*watchers)
        return uncertain, lifecycle_error

    uncertain, lifecycle_error = asyncio.run(scenario())
    assert isinstance(lifecycle_error, ConnectorUnavailable)
    assert adapter.revoke_count == 0
    assert uncertain.invocation_id is not None
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT operation_id FROM connector_invocations WHERE invocation_id=?",
            (uncertain.invocation_id,),
        ).fetchone()
    assert row is not None
    operation_id = str(row[0])
    uncertain_operations = service.repository.uncertain_operation_ids(
        instance.instance_id
    )
    if not provider_finishes_with_error:
        # A late successful provider result is authoritative: Runtime commits
        # it and removes the exact drain fence, so no human may claim it did
        # not execute and no retry may send a second write.
        assert uncertain_operations == ()
        service.repository.resolve_uncertain_invocation(
            uncertain.invocation_id,
            resolution,
        )
    else:
        assert uncertain_operations == (operation_id,)
    with pytest.raises(KeyError):
        service.repository.resolve_uncertain_invocation(
            "conninvoke_wrong_exact_identity",
            resolution,
        )
    with pytest.raises(RuntimeError, match="invocation reconciliation"):
        service.repository.resolve_uncertain_operation(
            instance.instance_id,
            operation_id,
            resolution=resolution,
        )
    if provider_finishes_with_error:
        service.repository.resolve_uncertain_invocation(
            uncertain.invocation_id,
            resolution,
        )
    assert service.repository.uncertain_operation_ids(instance.instance_id) == ()

    if lifecycle == "disconnect":
        asyncio.run(
            service.disconnect(
                instance.instance_id,
                client_request_id="timeout-disconnect-after-reconcile",
            )
        )
        assert service.repository.get_instance(instance.instance_id) is None
    else:
        retry = asyncio.run(
            service.begin_reauthorize(
                instance.instance_id,
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id="timeout-reauth-after-reconcile",
            )
        )
        adapter.invoke_raises_after_release = False
        updated = asyncio.run(
            service.complete_connect(retry.flow_id, {"state": _state(retry)})
        )
        assert updated.instance_id == instance.instance_id


def test_early_connector_retry_waits_for_provider_fence_then_same_decision_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving HITL early is durable intent, not a terminal Turn failure."""

    database = tmp_path / "early-connector-retry.db"
    adapter = _BarrierAdapter()
    vault = _RecordingVault()
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            connector_adapters={"feishu": adapter},
            connector_vault=vault,
            full_access=True,
        )
    )
    service = app.state.connector_composition.service
    service.adapter_timeout_seconds = 0.05
    instance = _connect(service)
    adapter.block_invocation = True
    adapter.invoke_raises_after_release = False

    # Reproduce a loaded Runtime where the final local policy/SQLite fence is
    # slower than the provider response budget.  That local admission latency
    # must not make a subsequently dispatched write look like a known-safe
    # ConnectorUnavailable failure.
    refresh_invocation_admission = service.repository.refresh_invocation_admission

    def delayed_refresh_invocation_admission(*args: Any, **kwargs: Any) -> Any:
        threading.Event().wait(0.075)
        return refresh_invocation_admission(*args, **kwargs)

    monkeypatch.setattr(
        service.repository,
        "refresh_invocation_admission",
        delayed_refresh_invocation_admission,
    )

    action = service.registry.definition("feishu").action("documents.write")
    contract_sha256 = hashlib.sha256(
        json.dumps(
            action.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    discovery_id = (
        f"connector:{instance.instance_id}@feishu/documents.write@"
        f"{contract_sha256}"
    )
    gateway = _ScriptedGateway(
        [
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "connector-search-response",
                    "tool_call_id": "connector-search-call",
                    "tool_name": "connector_search",
                    "arguments": {"query": "编辑飞书文档", "limit": 10},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "connector-describe-response",
                    "tool_call_id": "connector-describe-call",
                    "tool_name": "connector_describe",
                    "arguments": {"discovery_id": discovery_id},
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "tool_call.requested",
                    "response_id": "connector-write-response",
                    "tool_call_id": "connector-write-call",
                    "tool_name": "connector_write",
                    "arguments": {
                        "discovery_id": discovery_id,
                        "input": {"title": "围栏后重试"},
                    },
                }
            ],
            [
                {
                    "seq": 1,
                    "event_type": "response.completed",
                    "response_id": "connector-retry-completed",
                }
            ],
        ]
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="connector early retry"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="使用飞书编辑文档",
            client_message_id="connector-early-retry-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    worker = AgentTurnWorker(
        kernel,
        gateway=gateway,
        capabilities=composition.capability_service,
        turn_preparer=composition.prepare_turn,
        permission_mutation_lock=composition.permission_mutation_lock,
        permission_account_id=composition.permission_account_id,
        connector_uncertain_resolver=(
            service.repository.resolve_uncertain_invocation
        ),
        retry_delay_seconds=0,
    )

    async def scenario():
        first = await worker.run_once("connector-early-worker")
        assert first.outcome is WorkerOutcome.WAITING_HUMAN
        interaction = next(
            value
            for value in kernel.list_interactions(thread.thread_id).interactions
            if value.status is InteractionStatus.PENDING
        )
        assert adapter.invocation_started.is_set()
        assert not adapter.invocation_finished.is_set()
        kernel.respond_interaction(
            interaction.interaction_id,
            {"action_id": "retry", "values": {}},
            client_request_id="connector-early-retry-decision",
        )
        early = await worker.run_once("connector-early-worker")
        early_job = kernel.jobs.get(created.job.job_id)
        early_turn = kernel.get_turn(created.turn.turn_id)
        assert early.outcome is WorkerOutcome.RETRY_SCHEDULED
        assert early_job.status.value == "retry_scheduled"
        assert early_turn.status is TurnStatus.RETRY_WAIT

        adapter.invocation_release.set()
        assert await asyncio.to_thread(adapter.invocation_finished.wait, 5)
        # Do not synchronize on the service's private watcher set.  The retry
        # is deliberately allowed to race late-result staging/finalization;
        # repository reconciliation must wait for the durable fact itself.
        adapter.block_invocation = False
        completed = await worker.run_once("connector-early-worker")
        return completed

    completed = asyncio.run(scenario())
    assert completed.outcome is WorkerOutcome.COMPLETED
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.COMPLETED
    assert len(adapter.invocations) == 1


def test_action_admin_deny_race_rechecks_after_reservation_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "admin-deny-race.db"
    service, adapter, _vault = _service(database)
    instance = _connect(service)
    current_denies: set[str] = set()
    reserved = threading.Event()
    release = threading.Event()
    original = service.repository.reserve_invocation

    def gated_reservation(*args, **kwargs):
        result = original(*args, **kwargs)
        reserved.set()
        if not release.wait(5):
            raise RuntimeError("test dispatch reservation barrier timed out")
        return result

    monkeypatch.setattr(service.repository, "reserve_invocation", gated_reservation)

    def invoke():
        return asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "管理员刚刚禁止"},
                idempotency_key="admin-deny-race-write",
                admin_hard_denies_provider=lambda: frozenset(current_denies),
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(invoke)
        assert reserved.wait(5)
        current_denies.add("documents.write")
        release.set()
        with pytest.raises(ConnectorPermissionDenied, match="administrator"):
            pending.result(timeout=5)

    assert adapter.invocations == []
    with sqlite3.connect(database) as connection:
        running_invocations = connection.execute(
            "SELECT COUNT(*) FROM connector_invocations WHERE status='running'"
        ).fetchone()[0]
        running_keys = connection.execute(
            "SELECT COUNT(*) FROM connector_idempotency WHERE status='running'"
        ).fetchone()[0]
    assert running_invocations == running_keys == 0
    current_denies.clear()
    retried = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "管理员已解除禁止"},
            idempotency_key="admin-deny-race-write",
            admin_hard_denies_provider=lambda: frozenset(current_denies),
        )
    )
    assert retried["title"] == "管理员已解除禁止"
    assert len(adapter.invocations) == 1


@pytest.mark.parametrize("failure_boundary", ["vault_get", "adapter_lookup"])
def test_pre_dispatch_failure_does_not_poison_idempotency_and_safe_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    class FailGetVault(_RecordingVault):
        fail_get = False

        def get(self, reference: str) -> Mapping[str, str]:
            if self.fail_get and reference.startswith("ecorex/connectors/"):
                raise RuntimeError("simulated vault get failure")
            return super().get(reference)

    database = tmp_path / f"pre-dispatch-{failure_boundary}.db"
    vault = FailGetVault()
    service, adapter, _vault = _service(database, vault=vault)
    instance = _connect(service)
    adapter_lookup_fails = False
    original_adapter_lookup = service.registry.adapter

    def adapter_lookup(connector_id: str):
        if adapter_lookup_fails:
            raise ConnectorUnavailable("simulated adapter lookup failure")
        return original_adapter_lookup(connector_id)

    monkeypatch.setattr(service.registry, "adapter", adapter_lookup)
    if failure_boundary == "vault_get":
        vault.fail_get = True
    else:
        adapter_lookup_fails = True

    with pytest.raises(ConnectorUnavailable):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "尚未派发"},
                idempotency_key=f"pre-dispatch-{failure_boundary}-write",
            )
        )
    assert adapter.invocations == []
    with sqlite3.connect(database) as connection:
        poisoned_invocations = connection.execute(
            "SELECT COUNT(*) FROM connector_invocations "
            "WHERE status IN ('running', 'outcome_unknown')"
        ).fetchone()[0]
        poisoned_keys = connection.execute(
            "SELECT COUNT(*) FROM connector_idempotency "
            "WHERE status IN ('running', 'outcome_unknown')"
        ).fetchone()[0]
    assert poisoned_invocations == poisoned_keys == 0

    vault.fail_get = False
    adapter_lookup_fails = False
    retried = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "尚未派发"},
            idempotency_key=f"pre-dispatch-{failure_boundary}-write",
        )
    )
    assert retried["title"] == "尚未派发"
    assert len(adapter.invocations) == 1


@pytest.mark.parametrize("operation", ["invoke", "health"])
def test_cancelled_operation_acquisition_cannot_orphan_a_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    database = tmp_path / f"acquire-cancel-{operation}.db"
    service, adapter, _vault = _service(database)
    instance = _connect(service)
    acquired = threading.Event()
    release = threading.Event()
    original = service.repository.acquire_instance_operation

    def gated_acquire(*args, **kwargs):
        result = original(*args, **kwargs)
        acquired.set()
        if not release.wait(5):
            raise RuntimeError("test operation-acquire barrier timed out")
        return result

    monkeypatch.setattr(
        service.repository,
        "acquire_instance_operation",
        gated_acquire,
    )

    async def scenario() -> None:
        if operation == "invoke":
            pending = asyncio.create_task(
                service.invoke(
                    instance.instance_id,
                    "documents.write",
                    {"title": "不得越过取消"},
                    idempotency_key="cancelled-acquire-write",
                )
            )
        else:
            pending = asyncio.create_task(
                service.refresh_health(
                    instance.instance_id,
                    client_request_id="cancelled-acquire-health",
                )
            )
        assert await asyncio.to_thread(acquired.wait, 5)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    with sqlite3.connect(database) as connection:
        leases = connection.execute(
            "SELECT COUNT(*) FROM connector_operation_leases"
        ).fetchone()[0]
    assert leases == 0
    assert adapter.invocations == []
    if operation == "health":
        lifecycle = service.repository.lifecycle_request_state(
            "cancelled-acquire-health"
        )
        assert lifecycle is not None
        assert lifecycle["status"] != "running"


def test_cancel_after_invocation_commit_replays_without_second_provider_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "invocation-commit-cancel.db"
    service, adapter, _vault = _service(database)
    instance = _connect(service)
    committed = threading.Event()
    release = threading.Event()
    original = service.repository.complete_invocation

    def gated_completion(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        if not release.wait(5):
            raise RuntimeError("test invocation-commit barrier timed out")
        return result

    monkeypatch.setattr(service.repository, "complete_invocation", gated_completion)

    async def scenario() -> None:
        pending = asyncio.create_task(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "只写一次"},
                idempotency_key="cancel-after-invocation-commit",
            )
        )
        assert await asyncio.to_thread(committed.wait, 5)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM connector_invocations "
            "WHERE idempotency_key_sha256 IS NOT NULL"
        ).fetchone()[0]
        leases = connection.execute(
            "SELECT COUNT(*) FROM connector_operation_leases"
        ).fetchone()[0]
    assert status == "completed"
    assert leases == 0
    replay = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "只写一次"},
            idempotency_key="cancel-after-invocation-commit",
        )
    )
    assert replay["title"] == "只写一次"
    assert len(adapter.invocations) == 1


def test_cancel_vault_delete_failure_keeps_durable_cleanup_pointer(
    tmp_path: Path,
) -> None:
    class DeleteFailVault(_RecordingVault):
        fail_reference: str | None = None

        def delete(self, reference: str) -> None:
            if reference == self.fail_reference:
                raise RuntimeError("simulated credential-store delete failure")
            super().delete(reference)

    database = tmp_path / "cancel-vault-delete.db"
    vault = DeleteFailVault()
    service, adapter, _vault = _service(database, vault=vault)
    interaction_id = "hitl-cancel-vault-delete"
    _reservation, challenge = _begin_bound_login(service, interaction_id)
    with sqlite3.connect(database) as connection:
        private_ref = str(
            connection.execute(
                "SELECT private_ref FROM connector_auth_flows WHERE flow_id=?",
                (challenge.flow_id,),
            ).fetchone()[0]
        )
    vault.fail_reference = private_ref
    with pytest.raises(RuntimeError, match="delete failure"):
        asyncio.run(service.cancel_interaction_login(interaction_id))

    binding = service.repository.interaction_login_binding(interaction_id)
    assert binding is not None and binding.status == "cancelled"
    with sqlite3.connect(database) as connection:
        flow = connection.execute(
            "SELECT status, private_ref, operation_lease_expires_at "
            "FROM connector_auth_flows WHERE flow_id=?",
            (challenge.flow_id,),
        ).fetchone()
    assert flow is not None
    assert tuple(flow[:2]) == ("consumed", private_ref)
    assert flow[2] is not None
    assert private_ref in vault.references()

    vault.fail_reference = None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
            "WHERE flow_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                challenge.flow_id,
            ),
        )
    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    asyncio.run(restarted.maintenance_once())
    _assert_no_consumable_or_private_flow(database)
    assert private_ref not in vault.references()


@pytest.mark.parametrize(
    ("phase", "error_code"),
    [
        ("starting", "auth_start_interrupted"),
        ("awaiting_callback", "auth_flow_expired"),
        ("completing", "auth_completion_interrupted"),
    ],
)
def test_expired_login_phase_has_typed_retry_and_new_generation(
    tmp_path: Path,
    phase: str,
    error_code: str,
) -> None:
    database = tmp_path / f"expired-{phase}.db"
    service, adapter, vault = _service(database)
    interaction_id = f"hitl-expired-{phase}"
    reservation = _reserve_login(service, interaction_id)
    challenge = None
    if phase != "starting":
        binding = reservation.binding
        challenge = asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=RETURN_URI,
                client_request_id=binding.lifecycle_request_id,
                interaction_binding=(
                    interaction_id,
                    binding.generation,
                    str(binding.operation_token),
                ),
            )
        )
    if phase == "completing":
        assert challenge is not None
        with service.control_admission(
            operation="test_consume_flow_expired_phase",
            subject=challenge.flow_id,
        ):
            consumption = service.repository.consume_flow(
                challenge.flow_id,
                operation_token="connflowconsume_expired_phase",
                lease_seconds=30,
            )
        assert consumption.reason == "consumed"

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database) as connection:
        if phase == "starting":
            connection.execute(
                "UPDATE connector_interaction_logins "
                "SET operation_lease_expires_at=? WHERE interaction_id=?",
                (past, interaction_id),
            )
        elif phase == "awaiting_callback":
            connection.execute(
                "UPDATE connector_interaction_logins SET expires_at=? "
                "WHERE interaction_id=?",
                (past, interaction_id),
            )
            connection.execute(
                "UPDATE connector_auth_flows SET expires_at=?, "
                "operation_lease_expires_at=? WHERE flow_id=?",
                (past, past, challenge.flow_id),
            )
        else:
            connection.execute(
                "UPDATE connector_interaction_logins "
                "SET operation_lease_expires_at=? WHERE interaction_id=?",
                (past, interaction_id),
            )
            connection.execute(
                "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
                "WHERE flow_id=?",
                (past, challenge.flow_id),
            )

    recovered = service.repository.recover_expired_interaction_logins()
    assert recovered == (interaction_id,)
    failed = service.repository.interaction_login_binding(interaction_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error_code == error_code
    retried = service.repository.reserve_interaction_login(
        interaction_id=interaction_id,
        connector_id="feishu",
        mode="connect",
        target_instance_id=None,
    )
    assert retried.outcome == "reserved"
    assert retried.binding.generation == reservation.binding.generation + 1

    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    asyncio.run(restarted.maintenance_once())
    assert restarted.repository.interaction_login_binding(interaction_id).status == "starting"
    if challenge is not None:
        _assert_no_consumable_or_private_flow(database)


@pytest.mark.parametrize("phase", ["draining", "revoking", "disconnecting"])
def test_disconnect_transition_recovers_after_restart(
    tmp_path: Path,
    phase: str,
) -> None:
    database = tmp_path / f"disconnect-{phase}.db"
    service, adapter, vault = _service(database)
    instance = _connect(service)
    credential_ref = instance.credential_ref
    assert service.repository.begin_draining(instance.instance_id) is not None
    if phase in {"revoking", "disconnecting"}:
        service.repository.mark_revoking(instance.instance_id)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE connector_runtime_instances "
                "SET transition_lease_expires_at=? WHERE instance_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    instance.instance_id,
                ),
            )
    if phase == "disconnecting":
        claimed = service.repository.claim_revocation(instance.instance_id)
        assert claimed is not None
        _claimed_instance, revocation_token = claimed
        service.repository.mark_remote_revoked(
            instance.instance_id,
            transition_token=revocation_token,
            lease_seconds=30,
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE connector_runtime_instances "
                "SET transition_lease_expires_at=? WHERE instance_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    instance.instance_id,
                ),
            )

    restarted, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    asyncio.run(restarted.maintenance_once())
    asyncio.run(restarted.maintenance_once())
    assert adapter.revoke_count == (0 if phase == "disconnecting" else 1)
    assert restarted.repository.get_instance(instance.instance_id) is None
    with pytest.raises(KeyError):
        vault.get(credential_ref)
    stable, _adapter, _vault = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    assert stable.repository.get_instance(instance.instance_id) is None


def test_restart_promotes_reserved_write_to_exact_reconcilable_unknown(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reserved-write-crash.db"
    service, adapter, vault = _service(database)
    instance = _connect(service)
    acquired = service.repository.acquire_instance_operation(
        instance.instance_id,
        operation_kind="invoke",
        lease_seconds=30,
        uncertainty_policy="auto_release",
    )
    assert acquired is not None
    _instance, lease = acquired
    record = ConnectorInvocationRecord(
        invocation_id="conninvoke_reserved_write_crash",
        instance_id=instance.instance_id,
        connector_id="feishu",
        action_id="documents.write",
        input_sha256="1" * 64,
        idempotency_key_sha256="2" * 64,
        status="running",
        created_at=datetime.now(UTC),
        admission_policy_sha256="3" * 64,
    )
    reservation = service.repository.reserve_invocation(
        record,
        operation_lease=lease,
        retain_on_uncertainty=True,
    )
    assert reservation.outcome == "reserved"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_operation_leases SET expires_at=? WHERE operation_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease.operation_id),
        )

    restarted, _adapter2, _vault2 = _service(database, vault=vault, adapter=adapter)
    assert restarted.repository.has_live_instance_operations(instance.instance_id)
    with sqlite3.connect(database) as connection:
        facts = connection.execute(
            "SELECT invocation.status, idem.status, lease.status "
            "FROM connector_invocations AS invocation "
            "JOIN connector_idempotency AS idem USING(invocation_id) "
            "JOIN connector_operation_leases AS lease USING(operation_id) "
            "WHERE invocation.invocation_id=?",
            (record.invocation_id,),
        ).fetchone()
    assert facts == ("outcome_unknown", "outcome_unknown", "outcome_unknown")
    restarted.repository.resolve_uncertain_invocation(
        record.invocation_id, "confirmed_not_executed"
    )
    assert not restarted.repository.has_live_instance_operations(instance.instance_id)


@pytest.mark.parametrize("kind", ["read", "health"])
def test_restart_auto_releases_expired_safe_operation(
    tmp_path: Path,
    kind: str,
) -> None:
    database = tmp_path / f"safe-{kind}-crash.db"
    service, adapter, vault = _service(database)
    instance = _connect(service)
    acquired = service.repository.acquire_instance_operation(
        instance.instance_id,
        operation_kind=kind,
        lease_seconds=30,
        uncertainty_policy="auto_release",
    )
    assert acquired is not None
    _instance, lease = acquired
    invocation_id = f"conninvoke_safe_{kind}_crash"
    if kind == "read":
        record = ConnectorInvocationRecord(
            invocation_id=invocation_id,
            instance_id=instance.instance_id,
            connector_id="feishu",
            action_id="documents.read",
            input_sha256="4" * 64,
            idempotency_key_sha256=None,
            status="running",
            created_at=datetime.now(UTC),
            admission_policy_sha256="5" * 64,
        )
        service.repository.reserve_invocation(
            record,
            operation_lease=lease,
            retain_on_uncertainty=False,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE connector_operation_leases SET expires_at=? WHERE operation_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease.operation_id),
        )
    restarted, _adapter2, _vault2 = _service(database, vault=vault, adapter=adapter)
    assert not restarted.repository.has_live_instance_operations(instance.instance_id)
    assert restarted.repository.uncertain_operation_ids(instance.instance_id) == ()
    if kind == "read":
        with sqlite3.connect(database) as connection:
            status = connection.execute(
                "SELECT status FROM connector_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()[0]
        assert status == "completed"


def test_completed_write_atomically_removes_manual_operation_fence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completed-write-fence.db"
    service, adapter, vault = _service(database)
    instance = _connect(service)
    acquired = service.repository.acquire_instance_operation(
        instance.instance_id,
        operation_kind="invoke",
        uncertainty_policy="auto_release",
    )
    assert acquired is not None
    _instance, lease = acquired
    record = ConnectorInvocationRecord(
        invocation_id="conninvoke_completed_write_fence",
        instance_id=instance.instance_id,
        connector_id="feishu",
        action_id="documents.write",
        input_sha256="6" * 64,
        idempotency_key_sha256="7" * 64,
        status="running",
        created_at=datetime.now(UTC),
        admission_policy_sha256="8" * 64,
    )
    assert service.repository.reserve_invocation(
        record,
        operation_lease=lease,
        retain_on_uncertainty=True,
    ).outcome == "reserved"

    service.repository.complete_invocation(
        record,
        result={"ok": True},
        operation_lease=lease,
    )

    restarted, _adapter2, _vault2 = _service(
        database,
        adapter=adapter,
        vault=vault,
    )
    assert not restarted.repository.has_live_instance_operations(instance.instance_id)
    assert restarted.repository.uncertain_operation_ids(instance.instance_id) == ()
    with sqlite3.connect(database) as connection:
        facts = connection.execute(
            "SELECT invocation.status, idem.status "
            "FROM connector_invocations AS invocation "
            "JOIN connector_idempotency AS idem USING(invocation_id) "
            "WHERE invocation.invocation_id=?",
            (record.invocation_id,),
        ).fetchone()
        lease_count = connection.execute(
            "SELECT COUNT(*) FROM connector_operation_leases WHERE operation_id=?",
            (lease.operation_id,),
        ).fetchone()[0]
    assert facts == ("completed", "completed")
    assert lease_count == 0


def test_repeated_startup_and_maintenance_dedupe_redacted_recovery_deferred(
    tmp_path: Path,
) -> None:
    class DeleteFailVault(_RecordingVault):
        fail_reference: str | None = None

        def delete(self, reference: str) -> None:
            if reference == self.fail_reference:
                raise RuntimeError(
                    "simulated secret cleanup exception text must not persist"
                )
            super().delete(reference)

    database = tmp_path / "recovery-deferred-dedupe.db"
    vault = DeleteFailVault()
    service, adapter, _vault = _service(database, vault=vault)
    interaction_id = "hitl-recovery-deferred-dedupe"
    _reservation, challenge = _begin_bound_login(service, interaction_id)
    with sqlite3.connect(database) as connection:
        private_ref = str(
            connection.execute(
                "SELECT private_ref FROM connector_auth_flows WHERE flow_id=?",
                (challenge.flow_id,),
            ).fetchone()[0]
        )
    vault.fail_reference = private_ref
    with pytest.raises(RuntimeError, match="must not persist"):
        asyncio.run(service.cancel_interaction_login(interaction_id))

    for cycle in range(3):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
                "WHERE flow_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=cycle + 1)).isoformat(),
                    challenge.flow_id,
                ),
            )
        if cycle == 0:
            service, _adapter, _vault = _service(
                database,
                adapter=adapter,
                vault=vault,
            )
        else:
            asyncio.run(service.maintenance_once())

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT aggregate_seq, payload_json FROM connector_outbox "
            "WHERE event_type='connector.recovery.deferred'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    payload = json.loads(str(rows[0][1]))
    assert payload == {
        "recovery_kind": "flow",
        "record_id": challenge.flow_id,
        "error_code": "credential_cleanup_deferred",
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert private_ref not in encoded
    assert "simulated" not in encoded
    assert "exception" not in encoded


def test_concurrent_recovery_deferred_records_one_fact_per_safe_payload(
    tmp_path: Path,
) -> None:
    repository = SQLiteConnectorRepository(tmp_path / "recovery-dedupe-race.db")

    def record(error_code: str = "credential_cleanup_deferred") -> None:
        repository.record_recovery_deferred(
            recovery_kind="pending_instance",
            record_id="conn_pending_recovery_dedupe",
            error_code=error_code,
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(record) for _ in range(24)]
        for future in futures:
            future.result(timeout=10)
    record("credential_cleanup_blocked")
    record("credential_cleanup_blocked")

    with sqlite3.connect(repository.database) as connection:
        rows = connection.execute(
            "SELECT aggregate_seq, payload_json FROM connector_outbox "
            "WHERE event_type='connector.recovery.deferred' "
            "ORDER BY aggregate_seq"
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    payloads = [json.loads(str(row[1])) for row in rows]
    assert [payload["error_code"] for payload in payloads] == [
        "credential_cleanup_deferred",
        "credential_cleanup_blocked",
    ]
    assert all(set(payload) == {"recovery_kind", "record_id", "error_code"} for payload in payloads)
