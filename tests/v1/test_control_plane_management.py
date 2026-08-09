from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from ecorex.control_plane import management_schema
from ecorex.control_plane.admin_management_router import (
    create_admin_management_router,
)
from ecorex.control_plane.management import (
    AdminManagementConflict,
    AdminManagementRepository,
    HTTPSModelConnectionTester,
    ModelConnectionTestResult,
)
from ecorex.control_plane.management_models import (
    AdjustUsageRequest,
    CreateAdminUserRequest,
    CreateModelConfigurationRequest,
    StageModelConfigurationRequest,
    UpdateAdminUserRequest,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.gateway.models import GatewayEventType
from ecorex.gateway.production import AdminManagementGatewayUsageAccountant
from ecorex.gateway.server import GatewayCompletedUsageFact


KEY = b"m" * 32
ACTOR = ControlPrincipal(
    subject="administrator",
    client_id="admin-web",
    account_id="admin",
    roles=frozenset({"platform_admin", "release_admin"}),
)


def _repository(tmp_path: Path) -> AdminManagementRepository:
    path = tmp_path / "control-plane.db"
    AdminManagementSchemaManager(path).migrate()
    return AdminManagementRepository(path, encryption_key=KEY)


def _user_request(request_id: str = "request-user-create") -> CreateAdminUserRequest:
    return CreateAdminUserRequest(
        account_id="account-1",
        display_name="测试用户",
        email="user@example.com",
        organization_id="org-1",
        token_limit=200_000,
        image_limit=100,
        password="ordinary-user-password-1",
        client_request_id=request_id,
    )


def _model_request(request_id: str = "request-model-create") -> CreateModelConfigurationRequest:
    return CreateModelConfigurationRequest(
        local_model_id="ecorex-chat",
        modality="chat",
        display_name="GPT-5.6 Luna · 高推理",
        upstream_model_id="gpt-5.6-luna",
        provider_preset="responses",
        is_default=True,
        enabled=True,
        api_key="sk-production-secret-123456",
        client_request_id=request_id,
    )


def test_user_management_is_filterable_revisioned_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = repository.create_user(_user_request(), actor=ACTOR)
    replayed = repository.create_user(_user_request(), actor=ACTOR)
    assert replayed == created

    listing = repository.list_users(query="测试", status="active", organization_id="org-1")
    assert listing.total == 1
    assert listing.items[0].tokens_used == 0
    assert listing.items[0].password_configured is True
    assert listing.items[0].credential_state == "configured"
    assert listing.items[0].password_changed_at is not None

    adjusted = repository.adjust_usage(
        created.account_id,
        AdjustUsageRequest(
            token_delta=12_000,
            image_delta=2,
            reason="账单回填",
            expected_revision=created.revision,
            client_request_id="request-usage-adjust",
        ),
        actor=ACTOR,
    )
    assert adjusted.tokens_used == 12_000
    assert adjusted.images_used == 2
    assert adjusted.revision == 2

    with pytest.raises(AdminManagementConflict, match="revision"):
        repository.update_user(
            created.account_id,
            UpdateAdminUserRequest(
                display_name="已过期编辑",
                email="user@example.com",
                organization_id="org-1",
                status="active",
                token_limit=300_000,
                image_limit=200,
                expected_revision=1,
                client_request_id="request-user-stale",
            ),
            actor=ACTOR,
        )

    summary = repository.usage_summary()
    assert summary.users_total == 1
    assert summary.users_active == 1
    assert summary.tokens_used == 12_000
    repository.verify_integrity()


def test_account_ids_and_emails_share_one_unambiguous_login_namespace(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = repository.create_user(_user_request(), actor=ACTOR)
    with pytest.raises(AdminManagementConflict, match="identity"):
        repository.create_user(
            CreateAdminUserRequest(
                account_id="user@example.com",
                display_name="冲突账号",
                email="second@example.com",
                password="second-user-password-1",
                client_request_id="request-user-account-email-conflict",
            ),
            actor=ACTOR,
        )
    second = repository.create_user(
        CreateAdminUserRequest(
            account_id="account-2@example.com",
            display_name="第二用户",
            email="second@example.com",
            password="second-user-password-1",
            client_request_id="request-user-second",
        ),
        actor=ACTOR,
    )
    with pytest.raises(AdminManagementConflict, match="identity"):
        repository.update_user(
            first.account_id,
            UpdateAdminUserRequest(
                display_name=first.display_name,
                email=second.account_id,
                organization_id=first.organization_id,
                status="active",
                token_limit=first.token_limit,
                image_limit=first.image_limit,
                expected_revision=first.revision,
                client_request_id="request-user-email-account-conflict",
            ),
            actor=ACTOR,
        )


def test_provider_usage_settlement_is_exactly_once_and_conflict_safe(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    created = repository.create_user(_user_request(), actor=ACTOR)
    values = {
        "source_service": "managed_gateway",
        "source_id": "gateway-request-1",
        "usage_kind": "chat",
        "account_id": created.account_id,
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "provider_created_at": "2026-07-19T02:30:00+00:00",
    }
    settled = repository.record_provider_usage(**values)
    replayed = repository.record_provider_usage(**values)
    assert replayed == settled
    assert settled.tokens_used == 150
    assert settled.revision == created.revision + 1

    with pytest.raises(AdminManagementConflict, match="reused"):
        repository.record_provider_usage(**{**values, "total_tokens": 151})

    with sqlite3.connect(tmp_path / "control-plane.db") as connection:
        facts = connection.execute(
            "SELECT source_service,source_id,total_tokens FROM "
            "admin_ops_provider_usage_facts"
        ).fetchall()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM admin_ops_audit "
            "WHERE action='usage.provider.settled'"
        ).fetchone()[0]
    assert facts == [("managed_gateway", "gateway-request-1", 150)]
    assert audit_count == 1
    assert repository.usage_summary().tokens_used == 150
    repository.verify_integrity()


def test_gateway_accountant_settles_replay_once_and_enforces_token_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository(tmp_path)
    repository.create_user(_user_request(), actor=ACTOR)
    accountant = AdminManagementGatewayUsageAccountant(repository)
    fact = GatewayCompletedUsageFact(
        request_id="gateway-accountant-request",
        account_id="account-1",
        terminal_event_type=GatewayEventType.TOOL_CALL_REQUESTED,
        input_tokens=190_000,
        output_tokens=10_000,
        total_tokens=200_000,
        provider_created_at=datetime.fromisoformat(
            "2026-07-19T03:00:00+00:00"
        ),
    )

    accountant.reconcile((fact, fact))
    assert repository.get_user("account-1").tokens_used == 200_000
    assert accountant.tokens_available("account-1") is False

    monkeypatch.setattr(
        "ecorex.gateway.production.build_account_usage_projection",
        lambda account_id, *, timezone_name: {
            "schema_version": 1,
            "scope": "account",
            "timezone": timezone_name,
            "today": {
                "input_tokens": 190_000,
                "output_tokens": 10_000,
                "total_tokens": 200_000,
            },
            "week": {
                "input_tokens": 190_000,
                "output_tokens": 10_000,
                "total_tokens": 200_000,
            },
            "week_started_at": "2026-07-12T16:00:00+00:00",
            "coverage_started_at": "2026-06-21T16:00:00+00:00",
            "calculated_at": "2026-07-19T03:00:01+00:00",
        },
    )
    projection = accountant.project(
        "account-1",
        timezone_name="Asia/Shanghai",
    )
    assert projection.week.total_tokens == 200_000


def test_model_key_is_encrypted_and_only_tested_revision_activates(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = repository.create_model_configuration(_model_request(), actor=ACTOR)
    assert created.active is None
    assert created.draft is not None
    assert created.draft.key_configured
    assert created.draft.key_fingerprint == "76e3d17e4398ecfd"

    raw = (tmp_path / "control-plane.db").read_bytes()
    assert b"sk-production-secret-123456" not in raw

    lease = repository.begin_model_test(
        created.config_id,
        1,
        actor=ACTOR,
        client_request_id="request-test-activate",
    )
    activated = repository.finish_model_test(
        lease, ModelConnectionTestResult(passed=True), actor=ACTOR
    )
    assert activated.status == "passed"
    active = repository.active_model(modality="chat")
    assert active.revision == 1
    assert active.api_key == "sk-production-secret-123456"
    assert repository.active_public_catalog()[0]["reasoning_effort"] == "max"

    staged = repository.stage_model_configuration(
        created.config_id,
        StageModelConfigurationRequest(
            display_name="GPT-5.6 Luna 新名称",
            upstream_model_id="gpt-5.6-luna-new",
            provider_preset="responses",
            is_default=True,
            enabled=True,
            api_key="sk-replacement-secret-123456",
            expected_active_revision=1,
            client_request_id="request-model-stage-2",
        ),
        actor=ACTOR,
    )
    assert staged.draft and staged.draft.revision == 2
    lease2 = repository.begin_model_test(
        created.config_id,
        2,
        actor=ACTOR,
        client_request_id="request-test-reject",
    )
    failed = repository.finish_model_test(
        lease2,
        ModelConnectionTestResult(passed=False, error_code="provider_key_rejected"),
        actor=ACTOR,
    )
    assert failed.status == "failed"
    assert repository.active_model(modality="chat").revision == 1


def test_edit_during_model_probe_supersedes_old_result(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = repository.create_model_configuration(_model_request(), actor=ACTOR)
    lease = repository.begin_model_test(
        created.config_id,
        1,
        actor=ACTOR,
        client_request_id="request-running-test",
    )
    repository.stage_model_configuration(
        created.config_id,
        StageModelConfigurationRequest(
            display_name="替代草稿",
            upstream_model_id="gpt-5.6-luna-next",
            provider_preset="responses",
            is_default=True,
            enabled=True,
            api_key=None,
            expected_active_revision=None,
            client_request_id="request-replace-running",
        ),
        actor=ACTOR,
    )
    result = repository.finish_model_test(
        lease, ModelConnectionTestResult(passed=True), actor=ACTOR
    )
    assert result.status == "superseded"
    assert repository.list_model_configurations()[0].active is None


def test_connection_test_uses_server_origin_and_checks_exact_model() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/responses":
            body = json.loads(request.content)
            if "previous_response_id" in body:
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": "resp-final", "model": "gpt-5.6-luna"},
                    },
                    {
                        "type": "response.output_text.delta",
                        "delta": "ECOREX_ACTIVATION_OK",
                    },
                    {
                        "type": "response.completed",
                        "response": {"id": "resp-final", "model": "gpt-5.6-luna"},
                    },
                ]
            else:
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": "resp-tool", "model": "gpt-5.6-luna"},
                    },
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "call_id": "call-tool",
                            "name": "ecorex_activation_check",
                            "arguments": '{"value":"ECOREX_ACTIVATION_OK"}',
                        },
                    },
                    {
                        "type": "response.completed",
                        "response": {"id": "resp-tool", "model": "gpt-5.6-luna"},
                    },
                ]
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content="".join(
                    f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    for event in events
                ),
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"data": [{"id": "gpt-5.6-luna"}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tester = HTTPSModelConnectionTester(
        {"responses": "https://models.ecorex.example"}, client=client
    )
    configuration = _repository_for_active_configuration()
    result = asyncio.run(tester.test(configuration))
    asyncio.run(client.aclose())

    assert result.passed
    assert requests[0].url == "https://models.ecorex.example/v1/models"
    assert requests[0].headers["authorization"] == "Bearer sk-test-value"
    assert requests[1].url == "https://models.ecorex.example/v1/responses"
    assert requests[1].headers["authorization"] == "Bearer sk-test-value"
    assert requests[1].headers["idempotency-key"].startswith(
        "ecorex-model-activation-"
    )
    assert json.loads(requests[1].content)["model"] == "gpt-5.6-luna"
    assert len(requests) == 3
    assert json.loads(requests[2].content)["previous_response_id"] == "resp-tool"


def _repository_for_active_configuration():
    from ecorex.control_plane.management_models import ActiveModelConfiguration

    return ActiveModelConfiguration(
        config_id="model-test",
        revision=1,
        local_model_id="ecorex-chat",
        modality="chat",
        display_name="GPT",
        upstream_model_id="gpt-5.6-luna",
        provider_preset="responses",
        is_default=True,
        api_key="sk-test-value",
    )


def test_admin_management_router_applies_roles_and_activates_after_test(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    class PassingTester:
        async def test(self, configuration):
            assert configuration.api_key.startswith("sk-")
            return ModelConnectionTestResult(passed=True)

    def current() -> ControlPrincipal:
        return ACTOR

    app = FastAPI()
    app.include_router(
        create_admin_management_router(
            repository,
            model_tester=PassingTester(),
            user_admin_dependency=current,
            model_admin_dependency=current,
        )
    )
    client = TestClient(app)
    created_user = client.post(
        "/api/v1/admin/users", json=_user_request().model_dump(mode="json")
    )
    assert created_user.status_code == 201
    assert created_user.json()["password_configured"] is True
    missing_password = _user_request("request-user-without-password").model_dump(
        mode="json", exclude={"password"}
    )
    missing_password["account_id"] = "account-without-password"
    missing_password["email"] = "without-password@example.com"
    rejected_user = client.post("/api/v1/admin/users", json=missing_password)
    assert rejected_user.status_code == 422
    assert rejected_user.json()["detail"]["code"] == "initial_password_required"
    model_payload = _model_request().model_dump(mode="json", exclude={"api_key"})
    model_payload["api_key"] = "sk-production-secret-123456"
    created_model = client.post("/api/v1/admin/models", json=model_payload)
    assert created_model.status_code == 201
    body = created_model.json()
    assert "api_key" not in created_model.text
    activated = client.post(
        f"/api/v1/admin/models/{body['config_id']}/test-and-activate",
        json={"revision": 1, "client_request_id": "request-router-test"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "passed"
    assert repository.active_model(modality="chat").revision == 1
    default_policy = client.get("/api/v1/admin/tenants/org-1/model-policy")
    assert default_policy.status_code == 200
    assert default_policy.json()["configured"] is False
    assert "ecorex-gpt-5.6-sol" in default_policy.json()["allowed_model_ids"]
    updated_policy = client.put(
        "/api/v1/admin/tenants/org-1/model-policy",
        json={
            "allowed_model_ids": ["ecorex-chat", "gpt-image-2"],
            "expected_revision": 0,
            "client_request_id": "request-router-tenant-policy",
        },
    )
    assert updated_policy.status_code == 200
    assert updated_policy.json()["default_chat_reasoning_effort"] == "max"
    assert updated_policy.json()["image_primary_model_id"] == "gpt-image-2-pro"
    assert updated_policy.json()["image_fallback_upstream_model_id"] == "gpt-image-2"
    assert "api_key" not in updated_policy.text


def test_management_schema_rejects_drift(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.db"
    manager = AdminManagementSchemaManager(path)
    manager.migrate()
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE admin_ops_users ADD COLUMN unsafe TEXT")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Exception, match="schema drifted"):
        manager.validate()


def test_management_schema_migrates_v1_model_origin_presets(tmp_path: Path) -> None:
    from ecorex.control_plane.management_schema import ADMIN_MANAGEMENT_SCHEMA_SQL

    path = tmp_path / "control-plane-v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(ADMIN_MANAGEMENT_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO admin_ops_schema_migrations VALUES(1,?,?,?)",
            (
                "initial-admin-management",
                "ceeb871fe920bc47afe58032a461b464220f707a56633deed1ce8b4e45afc72d",
                "2026-07-16T00:00:00+00:00",
            ),
        )
        connection.executemany(
            "INSERT INTO admin_ops_users VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    "existing-org-user",
                    "Existing organization user",
                    "org@example.test",
                    "existing-org",
                    "active",
                    100,
                    0,
                    10,
                    0,
                    1,
                    "2026-07-16T00:00:00+00:00",
                    "2026-07-16T00:00:00+00:00",
                ),
                (
                    "existing-personal-user",
                    "Existing personal user",
                    "personal@example.test",
                    None,
                    "active",
                    100,
                    0,
                    10,
                    0,
                    1,
                    "2026-07-16T00:00:00+00:00",
                    "2026-07-16T00:00:00+00:00",
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    receipt = AdminManagementSchemaManager(path).migrate()
    assert receipt.migration_version == 7
    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_ops_model_revisions)")
        }
        usage_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='admin_ops_provider_usage_facts'"
        ).fetchone()
        password_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='admin_ops_password_credentials'"
        ).fetchone()
        tenant_policy_table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='admin_ops_tenant_model_policies'"
        ).fetchone()
        tenant_policies = connection.execute(
            "SELECT organization_id,default_chat_model_id,"
            "default_chat_reasoning_effort,image_primary_model_id,"
            "image_fallback_upstream_model_id,revision "
            "FROM admin_ops_tenant_model_policies ORDER BY organization_id"
        ).fetchall()
        usage_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_ops_provider_usage_facts)"
            )
        }
        auth_revisions = connection.execute(
            "SELECT account_id,auth_revision FROM admin_ops_users ORDER BY account_id"
        ).fetchall()
    finally:
        connection.close()
    assert "provider_origin_preset" in columns
    assert usage_table == (1,)
    assert password_table == (1,)
    assert tenant_policy_table == (1,)
    assert {
        "organization_id",
        "requested_model_id",
        "provider_reported_model_id",
        "actual_model_id",
        "actual_provider_id",
        "fallback_from_model_id",
        "fallback_used",
        "job_status",
        "result_status",
    } <= usage_columns
    assert tenant_policies == [
        ("existing-org", "ecorex-chat", "max", "gpt-image-2-pro", "gpt-image-2", 1),
        (
            "personal:existing-personal-user",
            "ecorex-chat",
            "max",
            "gpt-image-2-pro",
            "gpt-image-2",
            1,
        ),
    ]
    assert auth_revisions == [
        ("existing-org-user", 1),
        ("existing-personal-user", 1),
    ]


def test_management_schema_v7_preserves_v6_provider_facts(tmp_path: Path) -> None:
    path = tmp_path / "control-plane-v6.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_SQL)
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_V2_SQL)
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_V3_SQL)
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_V4_SQL)
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_V5_SQL)
        connection.executescript(management_schema.ADMIN_MANAGEMENT_SCHEMA_V6_SQL)
        connection.executemany(
            "INSERT INTO admin_ops_schema_migrations VALUES(?,?,?,?)",
            (
                (1, management_schema._INITIAL_MIGRATION_NAME, management_schema._INITIAL_MIGRATION_CHECKSUM, "2026-07-16T00:00:00+00:00"),
                (2, management_schema._MIGRATION_V2_NAME, management_schema._MIGRATION_V2_CHECKSUM, "2026-07-16T00:00:00+00:00"),
                (3, "provider-usage-facts", management_schema._MIGRATION_V3_CHECKSUM, "2026-07-16T00:00:00+00:00"),
                (4, management_schema._MIGRATION_V4_NAME, management_schema._MIGRATION_V4_CHECKSUM, "2026-07-16T00:00:00+00:00"),
                (5, management_schema._MIGRATION_V5_NAME, management_schema._MIGRATION_V5_CHECKSUM, "2026-07-16T00:00:00+00:00"),
                (6, management_schema._MIGRATION_V6_NAME, management_schema._MIGRATION_V6_CHECKSUM, "2026-07-16T00:00:00+00:00"),
            ),
        )
        connection.execute(
            "INSERT INTO admin_ops_users VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "account-v5", "Existing User", "v5@example.test", "org-v5",
                "active", 0, 0, 10, 1, 7,
                "2026-07-16T00:00:00+00:00", "2026-07-16T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO admin_ops_provider_usage_facts("
            "fact_id,source_service,source_id,usage_kind,account_id,input_tokens,"
            "output_tokens,total_tokens,image_count,payload_sha256,provider_created_at,"
            "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "fact-v5", "image_service", "job-v5", "image", "account-v5",
                0, 0, 0, 1, "0" * 64,
                "2026-07-16T00:00:00+00:00", "2026-07-16T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    receipt = AdminManagementSchemaManager(path).migrate()

    assert receipt.migration_version == 7
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT source_id,image_count,organization_id,actual_model_id,"
            "fallback_used,result_status FROM admin_ops_provider_usage_facts"
        ).fetchone()
        auth_revision = connection.execute(
            "SELECT auth_revision FROM admin_ops_users WHERE account_id='account-v5'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("job-v5", 1, None, None, None, None)
    assert auth_revision == (7,)
