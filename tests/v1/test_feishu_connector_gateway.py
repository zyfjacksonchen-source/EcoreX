from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
import httpx
import pytest

from ecorex.capabilities.schema import validate_schema_instance
from ecorex.connectors import (
    ConnectorAuthError,
    ConnectorAuthKind,
    ManagedConnectorGatewayAdapter,
    ManagedConnectorTransportError,
)
from ecorex.connectors.builtin import builtin_connector_definitions
from ecorex.control_plane.audit import CloudAuditRepository
from ecorex.control_plane.audit_schema import CloudAuditSchemaManager
from ecorex.control_plane.connector_gateway import (
    FEISHU_OAUTH_RETURN_URI,
    FEISHU_SCOPES,
    FeishuConnectorGateway,
    FeishuProviderClient,
)
from ecorex.control_plane.connector_gateway_schema import (
    ConnectorGatewaySchemaManager,
)
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.production import (
    EnvironmentSecretProvider,
    ProductionConfigurationError,
)


class _Session:
    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(account_id="account-a", lease_digest="lease-a", generation=1)

    def bearer_token(self) -> str:
        return "managed-session-token"


def _provider_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, **data})


def test_feishu_gateway_closes_oauth_refresh_actions_and_revoke_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"
    CloudAuditSchemaManager(database).migrate()
    ConnectorGatewaySchemaManager(database).migrate()
    audit = CloudAuditRepository(
        database, encryption_key=b"a" * 32, integrity_key=b"i" * 32
    )
    provider_calls: list[tuple[str, str, dict[str, str], dict]] = []
    document_reads = 0
    title_reads = 0
    document_has_content = False
    document_content_posts: list[str] = []

    def provider_handler(request: httpx.Request) -> httpx.Response:
        nonlocal document_reads, title_reads, document_has_content
        body = json.loads(request.content) if request.content else {}
        provider_calls.append(
            (request.method, request.url.path, dict(request.url.params), body)
        )
        path = request.url.path
        if path == "/open-apis/authen/v2/oauth/token":
            if body["grant_type"] == "authorization_code":
                assert body["client_id"] == "cli_test"
                assert body["client_secret"] == "server-secret"
                assert body["redirect_uri"] == FEISHU_OAUTH_RETURN_URI
                assert body["code_verifier"] == "v" * 64
                return _provider_response(
                    {
                        "access_token": "provider-access-old",
                        "refresh_token": "provider-refresh-old",
                        "expires_in": 3600,
                        "refresh_token_expires_in": 86400,
                        "scope": " ".join(FEISHU_SCOPES),
                    }
                )
            assert body == {
                "grant_type": "refresh_token",
                "client_id": "cli_test",
                "client_secret": "server-secret",
                "refresh_token": "provider-refresh-old",
            }
            return _provider_response(
                {
                    "access_token": "provider-access-new",
                    "refresh_token": "provider-refresh-new",
                    "expires_in": 3600,
                    "refresh_token_expires_in": 86400,
                    "scope": " ".join(FEISHU_SCOPES),
                }
            )
        if path == "/open-apis/authen/v1/user_info":
            assert request.headers["authorization"] in {
                "Bearer provider-access-old",
                "Bearer provider-access-new",
            }
            return _provider_response(
                {"data": {"open_id": "ou_test", "name": "飞书测试用户"}}
            )
        if path == "/open-apis/docx/v1/documents/doc-read/raw_content":
            if request.headers["authorization"] == "Bearer provider-access-old":
                return httpx.Response(200, json={"code": 20005})
            return _provider_response({"data": {"content": "真实正文"}})
        if path == "/open-apis/docx/v1/documents/doc-read":
            if request.headers["authorization"] == "Bearer provider-access-old":
                return httpx.Response(200, json={"code": 20005})
            return _provider_response(
                {"data": {"document": {"document_id": "doc-read", "revision_id": 3, "title": "文档"}}}
            )
        if path == "/open-apis/docx/v1/documents/doc-nonempty":
            return _provider_response(
                {"data": {"document": {"document_id": "doc-nonempty", "revision_id": 1, "title": "已有正文"}}}
            )
        if path == "/open-apis/docx/v1/documents/doc-nonempty/blocks/doc-nonempty/children":
            return _provider_response(
                {"data": {"items": [{"block_id": "block-a"}], "has_more": False}}
            )
        if path == "/open-apis/docx/v1/documents/doc-nonempty/raw_content":
            return _provider_response({"data": {"content": "原有正文"}})
        if path == "/open-apis/docx/v1/documents/doc-write":
            document_reads += 1
            return _provider_response(
                {
                    "data": {
                        "document": {
                            "document_id": "doc-write",
                            "revision_id": 7 if document_reads == 1 else 8,
                            "title": "旧标题",
                        }
                    }
                }
            )
        if path == "/open-apis/docx/v1/documents/doc-write/blocks/doc-write":
            assert request.method == "PATCH"
            assert body["update_text_elements"]["elements"][0]["text_run"]["content"] == "新标题"
            return _provider_response({"data": {}})
        if path == "/open-apis/docx/v1/documents/doc-write/blocks/doc-write/children":
            if request.method == "GET":
                assert request.url.params["document_revision_id"] in {"7", "-1"}
                items = []
                if document_has_content:
                    items = [
                        {
                            "block_type": 2,
                            "text": {
                                "elements": [
                                    {"text_run": {"content": "第一行\n第二行"}}
                                ]
                            },
                        }
                    ]
                return _provider_response({"data": {"items": items, "has_more": False}})
            assert request.method == "POST"
            assert request.url.params["document_revision_id"] == "7"
            assert body["children"][0]["text"]["elements"][0]["text_run"]["content"] == "第一行\n第二行"
            document_content_posts.append(request.url.params["client_token"])
            document_has_content = True
            return _provider_response({"data": {}})
        if path == "/open-apis/docx/v1/documents/doc-write/raw_content":
            return _provider_response({"data": {"content": "旧标题\n第一行\n第二行"}})
        if path == "/open-apis/docx/v1/documents/doc-title":
            title_reads += 1
            return _provider_response(
                {
                    "data": {
                        "document": {
                            "document_id": "doc-title",
                            "revision_id": 10 if title_reads == 1 else 11,
                            "title": "旧标题" if title_reads == 1 else "新标题",
                        }
                    }
                }
            )
        if path == "/open-apis/docx/v1/documents/doc-title/blocks/doc-title":
            assert request.method == "PATCH"
            assert request.url.params["document_revision_id"] == "10"
            assert body["update_text_elements"]["elements"][0]["text_run"]["content"] == "新标题"
            return _provider_response({"data": {}})
        if path == "/open-apis/suite/docs-api/search/object":
            if body == {"search_key": "漂移", "count": 5, "offset": 0}:
                return _provider_response(
                    {"data": {"items": [], "has_more": False, "total": 0}}
                )
            if body == {"search_key": "坏条目", "count": 5, "offset": 0}:
                return _provider_response(
                    {
                        "data": {
                            "docs_entities": [
                                {
                                    "file_id": "alias-not-accepted",
                                    "title": "缺少官方 token",
                                    "docs_type": "docx",
                                }
                            ],
                            "has_more": False,
                            "total": 1,
                        }
                    }
                )
            assert body in (
                {"search_key": "方案", "count": 5, "offset": 0},
                {"search_key": "下一页", "count": 5, "offset": 5},
            )
            return _provider_response(
                {
                    "data": {
                        "docs_entities": [
                            {
                                "docs_token": "doc-search",
                                "title": "方案文档",
                                "docs_type": "docx",
                                "url": "https://example.feishu.cn/docx/doc-search",
                            }
                        ],
                        "has_more": False,
                        "total": 1,
                    }
                }
            )
        if path == "/open-apis/im/v1/messages":
            assert dict(request.url.params) == {"receive_id_type": "chat_id"}
            assert body == {
                "receive_id": "oc_test",
                "msg_type": "text",
                "content": json.dumps(
                    {"text": "第一行\n第二行"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "uuid": body["uuid"],
            }
            assert len(body["uuid"]) == 36
            return _provider_response(
                {
                    "data": {
                        "message_id": "om_test",
                        "chat_id": "oc_test",
                        "create_time": "1786204800000",
                    }
                }
            )
        raise AssertionError(f"unexpected Feishu request: {request.method} {path}")

    principal = ControlPrincipal(
        subject="subject-a",
        client_id="desktop-a",
        account_id="account-a",
        organization_id="organization-a",
    )
    current_principal = {"value": principal}
    provider_http = httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(provider_handler),
    )
    gateway = FeishuConnectorGateway(
        database,
        app_id="cli_test",
        app_secret="server-secret",
        encryption_key=b"g" * 32,
        audit_repository=audit,
        provider=FeishuProviderClient(provider_http),
        clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
    )
    app = FastAPI()
    app.include_router(
        gateway.create_router(principal_dependency=lambda: current_principal["value"])
    )

    async def scenario() -> tuple[dict[str, dict], str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://dl.ecoremedia.net",
        ) as gateway_http:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://dl.ecoremedia.net/api/v1/connectors",
                allowed_hosts=frozenset({"dl.ecoremedia.net"}),
                session=_Session(),
                client=gateway_http,
            )
            verifier = "v" * 64
            challenge_value = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            challenge = await adapter.begin_auth(
                flow_id="connflow_test",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=FEISHU_OAUTH_RETURN_URI,
                state="state_0123456789abcdef",
                code_challenge=challenge_value,
                code_challenge_method="S256",
            )
            query = parse_qs(urlsplit(challenge.authorization_url or "").query)
            private_challenge = json.dumps(
                challenge.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assert query == {
                "client_id": ["cli_test"],
                "redirect_uri": [FEISHU_OAUTH_RETURN_URI],
                "response_type": ["code"],
                "scope": [" ".join(FEISHU_SCOPES)],
                "state": ["state_0123456789abcdef"],
                "code_challenge": [challenge_value],
                "code_challenge_method": ["S256"],
            }
            conflicting_flow = await gateway_http.post(
                "/api/v1/connectors/feishu/auth/begin",
                headers={"Idempotency-Key": "different-key"},
                json={
                    "flow_id": "connflow_test",
                    "auth_kind": "oauth2",
                    "return_uri": FEISHU_OAUTH_RETURN_URI,
                    "state": "different_0123456789abcdef",
                    "code_challenge": challenge_value,
                    "code_challenge_method": "S256",
                },
            )
            assert conflicting_flow.status_code == 409
            invalid_verifier = await gateway_http.post(
                "/api/v1/connectors/feishu/auth/complete",
                headers={"Idempotency-Key": "invalid-unicode-verifier"},
                json={
                    "flow_id": "connflow_test",
                    "response": {"code": "one-time-code", "state": "state_0123456789abcdef"},
                    "private_state": {
                        "state": "state_0123456789abcdef",
                        "pkce_verifier": "芯" * 43,
                        "challenge_json": private_challenge,
                    },
                },
            )
            assert invalid_verifier.status_code == 422
            complete_idempotency = gateway._complete_idempotency
            failed_keys: set[str] = set()

            def fail_complete_once(account_id, organization_id, key, response):
                if key in {"complete:connflow_test", "write-once"} and key not in failed_keys:
                    failed_keys.add(key)
                    raise sqlite3.OperationalError("simulated local completion failure")
                return complete_idempotency(account_id, organization_id, key, response)

            gateway._complete_idempotency = fail_complete_once
            with pytest.raises(ConnectorAuthError):
                await adapter.complete_auth(
                    flow_id="connflow_test",
                    response={"code": "one-time-code", "state": "state_0123456789abcdef"},
                    private_state={
                        "state": "state_0123456789abcdef",
                        "pkce_verifier": verifier,
                        "challenge_json": private_challenge,
                    },
                )
            grant = await adapter.complete_auth(
                flow_id="connflow_test",
                response={"code": "one-time-code", "state": "state_0123456789abcdef"},
                private_state={
                    "state": "state_0123456789abcdef",
                    "pkce_verifier": verifier,
                    "challenge_json": private_challenge,
                },
            )
            assert set(grant.credential_material) == {"managed_grant"}
            assert "provider-access" not in repr(grant)
            current_principal["value"] = ControlPrincipal(
                subject="subject-a",
                client_id="desktop-a",
                account_id="account-a",
                organization_id="organization-b",
            )
            with pytest.raises(ConnectorAuthError):
                await adapter.complete_auth(
                    flow_id="connflow_test",
                    response={"code": "one-time-code", "state": "state_0123456789abcdef"},
                    private_state={
                        "state": "state_0123456789abcdef",
                        "pkce_verifier": verifier,
                        "challenge_json": private_challenge,
                    },
                )
            wrong_tenant = await gateway_http.post(
                "/api/v1/connectors/feishu/health",
                json=grant.credential_material,
            )
            assert wrong_tenant.status_code == 401
            current_principal["value"] = principal
            assert (await adapter.check_health(grant.credential_material)).health.value == "connected"
            results = {
                "documents.read": await adapter.invoke(
                    action_id="documents.read",
                    inputs={"document_id": "doc-read"},
                    credentials=grant.credential_material,
                    idempotency_key=None,
                ),
            }
            with pytest.raises(
                ManagedConnectorTransportError, match="remote_retryable"
            ):
                await adapter.invoke(
                    action_id="documents.write",
                    inputs={
                        "document_id": "doc-write",
                        "revision_id": "7",
                        "content": "第一行\n第二行",
                    },
                    credentials=grant.credential_material,
                    idempotency_key="write-once",
                )
            results["documents.write"] = await adapter.invoke(
                action_id="documents.write",
                inputs={
                    "document_id": "doc-write",
                    "revision_id": "7",
                    "content": "第一行\n第二行",
                },
                credentials=grant.credential_material,
                idempotency_key="write-once",
            )
            results.update(
                {
                    "drive.search": await adapter.invoke(
                        action_id="drive.search",
                        inputs={"query": "方案", "limit": 5},
                        credentials=grant.credential_material,
                        idempotency_key=None,
                    ),
                    "messages.send": await adapter.invoke(
                        action_id="messages.send",
                        inputs={
                            "conversation_id": "oc_test",
                            "text": "第一行\n第二行",
                        },
                        credentials=grant.credential_material,
                        idempotency_key="message-once",
                    ),
                }
            )
            replayed_message = await adapter.invoke(
                action_id="messages.send",
                inputs={"conversation_id": "oc_test", "text": "第一行\n第二行"},
                credentials=grant.credential_material,
                idempotency_key="message-once",
            )
            assert replayed_message == results["messages.send"]
            title_result = await adapter.invoke(
                action_id="documents.write",
                inputs={
                    "document_id": "doc-title",
                    "revision_id": "10",
                    "title": "新标题",
                },
                credentials=grant.credential_material,
                idempotency_key="title-once",
            )
            assert title_result["title"] == "新标题"
            for invalid_write in (
                {"title": "不能隐式创建"},
                {
                    "document_id": "doc-title",
                    "title": "标题",
                    "content": "正文",
                },
                {"document_id": "doc-title", "title": "字" * 801},
            ):
                with pytest.raises(
                    ManagedConnectorTransportError, match="remote_rejected"
                ):
                    await adapter.invoke(
                        action_id="documents.write",
                        inputs=invalid_write,
                        credentials=grant.credential_material,
                        idempotency_key="invalid-write-" + hashlib.sha256(
                            repr(invalid_write).encode()
                        ).hexdigest()[:12],
                    )
            await adapter.invoke(
                action_id="drive.search",
                inputs={"query": "下一页", "limit": 5, "cursor": "5"},
                credentials=grant.credential_material,
                idempotency_key=None,
            )
            for query in ("漂移", "坏条目"):
                with pytest.raises(
                    ManagedConnectorTransportError, match="remote_retryable"
                ):
                    await adapter.invoke(
                        action_id="drive.search",
                        inputs={"query": query, "limit": 5},
                        credentials=grant.credential_material,
                        idempotency_key=None,
                    )
            with pytest.raises(
                ManagedConnectorTransportError, match="remote_rejected"
            ):
                await adapter.invoke(
                    action_id="drive.search",
                    inputs={"query": "坏游标", "cursor": "05"},
                    credentials=grant.credential_material,
                    idempotency_key=None,
                )
            with pytest.raises(
                ManagedConnectorTransportError, match="remote_rejected"
            ):
                await adapter.invoke(
                    action_id="drive.search",
                    inputs={"query": "越界", "cursor": "150", "limit": 50},
                    credentials=grant.credential_material,
                    idempotency_key=None,
                )
            with pytest.raises(
                ManagedConnectorTransportError, match="remote_rejected"
            ):
                await adapter.invoke(
                    action_id="documents.write",
                    inputs={"document_id": "doc-nonempty", "content": "不能覆盖"},
                    credentials=grant.credential_material,
                    idempotency_key="replace-refused",
                )
            current_principal["value"] = ControlPrincipal(
                subject="subject-a",
                client_id="desktop-a",
                account_id="account-a",
                organization_id="organization-b",
            )
            with pytest.raises(
                ManagedConnectorTransportError, match="remote_retryable"
            ):
                await adapter.invoke(
                    action_id="messages.send",
                    inputs={"conversation_id": "oc_test", "text": "第一行\n第二行"},
                    credentials=grant.credential_material,
                    idempotency_key="message-once",
                )
            current_principal["value"] = principal
            assert await adapter.revoke(
                credentials=grant.credential_material,
                idempotency_key="revoke-once",
            )
            return results, grant.credential_material["managed_grant"]

    try:
        results, managed_grant = asyncio.run(scenario())
    finally:
        asyncio.run(provider_http.aclose())

    feishu = {item.connector_id: item for item in builtin_connector_definitions()}[
        "feishu"
    ]
    for action_id, result in results.items():
        validate_schema_instance(
            result, feishu.action(action_id).output_schema, label=action_id
        )
    assert results["documents.read"]["content"] == "真实正文"
    assert results["messages.send"]["message_id"] == "om_test"
    assert sum(
        method == "POST" and path == "/open-apis/authen/v2/oauth/token"
        for method, path, _params, _body in provider_calls
    ) == 2

    with sqlite3.connect(database) as connection:
        grant_row = connection.execute(
            "SELECT grant_sha256,token_envelope_json,granted_scopes_json,revoked "
            "FROM connector_gateway_grants"
        ).fetchone()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM cloud_audit_records WHERE category='connector'"
        ).fetchone()[0]
        successful_message_audits = connection.execute(
            "SELECT COUNT(*) FROM cloud_audit_idempotency WHERE "
            "event_type='connector.action.messages.send' AND "
            "source_event_id NOT LIKE '%.failed.%'"
        ).fetchone()[0]
    assert grant_row[0] == hashlib.sha256(managed_grant.encode("ascii")).hexdigest()
    assert managed_grant not in database.read_bytes().decode("latin1")
    assert "provider-access" not in grant_row[1]
    assert "provider-refresh" not in grant_row[1]
    assert grant_row[2] == "[]"
    assert grant_row[3] == 1
    assert audit_count >= 8
    assert successful_message_audits == 1
    assert len(document_content_posts) == 1
    assert not any(
        path == "/open-apis/docx/v1/documents/doc-write/raw_content"
        for _method, path, _params, _body in provider_calls
    )
    assert sum(
        method == "POST" and path == "/open-apis/im/v1/messages"
        for method, path, _params, _body in provider_calls
    ) == 1


def test_feishu_gateway_deployment_is_same_origin_and_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    routes = (root / "deploy/ecorex-cloud-sidecar/nginx/ecorex-cloud.routes.conf").read_text()
    public = (root / "deploy/ecorex-cloud-sidecar/config/control-plane.env.example").read_text()
    secrets = (root / "deploy/ecorex-cloud-sidecar/config/control-plane.secret.env.example").read_text()
    workflow = (root / ".github/workflows/emate-2.0-desktop-release.yml").read_text()
    route = routes.split("location ^~ /api/v1/connectors/ {", 1)[1].split("\n}", 1)[0]
    assert "proxy_pass $ecorex_control_plane;" in route
    assert "proxy_set_header Authorization $http_authorization;" in route
    assert "limit_except POST { deny all; }" in route
    assert "access_log off;" in route
    assert "ECOREX_CP_FEISHU_CONNECTOR_ENABLED=false" in public
    assert "# ECOREX_CP_FEISHU_APP_SECRET=REPLACE_ME" in secrets
    assert "# ECOREX_CP_FEISHU_TOKEN_ENCRYPTION_KEY_B64=REPLACE_ME" in secrets
    assert 'ECOREX_V1_FEISHU_CONNECTOR_ENABLED: "true"' in workflow
    assert "assert runtime_config['connectors'] == {" in workflow
    with pytest.raises(
        ProductionConfigurationError,
        match="required Control Plane secret is unavailable",
    ):
        EnvironmentSecretProvider({}).read("feishu-app-secret")
