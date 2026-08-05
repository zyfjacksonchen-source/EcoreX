from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import httpx
import pytest

from ecorex.security import Ed25519AccessTokenVerifier
from ecorex.gateway import (
    Ed25519GatewayJWTAuthenticator,
    GatewayEvent,
    GatewayEventType,
    GatewayModelPolicy,
    GatewayPrincipal,
    GatewaySchemaManager,
    ManagedModelGatewayClient,
    ModelGatewayRequest,
    SQLiteGatewayStore,
)
from ecorex.gateway.models import (
    GatewayAssistantMessageInput,
    GatewayFunctionCallOutputInput,
    GatewayToolOutput,
    GatewayUserMessageInput,
)
from ecorex.gateway.production import (
    EnvironmentGatewaySecretProvider,
    GatewayProductionConfig,
    GatewayProductionConfigurationError,
    SingleNodeSQLiteResponsesProvider,
    main as gateway_main,
)
from ecorex.gateway.production_storage import GatewayProductionStorageError
from ecorex.gateway.responses_provider import (
    ManagedHTTPSResponsesProvider,
    ResponsesProviderConfigurationError,
    ResponsesProviderProtocolError,
    ResponsesProviderRejected,
    ResponsesProviderUnavailable,
)


PROVIDER_TOKEN = "provider-workload-token-00000001"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _key() -> tuple[Ed25519PrivateKey, str, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    encoded = base64.b64encode(public).decode("ascii")
    return private, json.dumps({"gateway-key": encoded}), public


def _jwt(
    private: Ed25519PrivateKey,
    *,
    models: list[str] | None = None,
    account_id: str = "account-1",
) -> str:
    now = datetime.now(UTC)
    header = {"alg": "EdDSA", "kid": "gateway-key", "typ": "JWT"}
    claims: dict[str, Any] = {
        "iss": "https://identity.ecorex.invalid",
        "aud": "ecorex-gateway",
        "token_use": "access",
        "sub": "user-1",
        "client_id": "ecorex-web",
        "account_id": account_id,
        "roles": ["member"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "quota_period": "2026-07",
        "request_limit": 100,
        "concurrent_request_limit": 4,
    }
    if models is not None:
        claims["allowed_model_ids"] = models
    encoded_header = _b64url(
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    )
    encoded_claims = _b64url(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    )
    signature = private.sign(f"{encoded_header}.{encoded_claims}".encode("ascii"))
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _environment(tmp_path: Path, keyring: str) -> dict[str, str]:
    return {
        "ECOREX_GATEWAY_STORAGE_BACKEND": "sqlite-wal",
        "ECOREX_GATEWAY_REPLICA_COUNT": "1",
        "ECOREX_GATEWAY_DATABASE_PATH": str((tmp_path / "gateway.sqlite3").resolve()),
        "ECOREX_GATEWAY_STORAGE_ENCRYPTION_AT_REST": "true",
        "ECOREX_GATEWAY_MODEL_MAPPING_JSON": json.dumps(
            {"ecorex-chat": "gpt-5.6-luna"}
        ),
        "ECOREX_GATEWAY_PROVIDER_ORIGIN": "https://provider.ecorex.invalid",
        "ECOREX_GATEWAY_PROVIDER_ALLOWED_ORIGINS_JSON": json.dumps(
            ["https://provider.ecorex.invalid"]
        ),
        "ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN": PROVIDER_TOKEN,
        "ECOREX_GATEWAY_AUTH_ISSUER": "https://identity.ecorex.invalid",
        "ECOREX_GATEWAY_AUTH_AUDIENCE": "ecorex-gateway",
        "ECOREX_GATEWAY_AUTH_PUBLIC_KEYS_JSON": keyring,
        "ECOREX_GATEWAY_PROVIDER_CONNECT_TIMEOUT_SECONDS": "0.1",
        "ECOREX_GATEWAY_PROVIDER_READ_TIMEOUT_SECONDS": "0.5",
        "ECOREX_GATEWAY_PROVIDER_TOTAL_TIMEOUT_SECONDS": "1",
        "ECOREX_GATEWAY_LEASE_SECONDS": "31",
        "ECOREX_GATEWAY_READINESS_CACHE_SECONDS": "1",
        "ECOREX_GATEWAY_GRACEFUL_SHUTDOWN_SECONDS": "5",
    }


def _request(request_id: str = "request-1") -> ModelGatewayRequest:
    return ModelGatewayRequest(
        request_id=request_id,
        thread_id="thread-1",
        turn_id="turn-1",
        trace_id="trace-1",
        model_id="ecorex-chat",
        input="请帮我整理摘要",
        config_snapshot_id="config-1",
        capability_snapshot_id="capability-1",
        permission_snapshot_id="permission-1",
    )


def test_gateway_dynamic_origin_presets_and_handoff_ttl_are_deployment_fixed(
    tmp_path: Path,
) -> None:
    _private, keyring, _public = _key()
    environment = _environment(tmp_path, keyring)
    environment.update(
        {
            "ECOREX_GATEWAY_ADMIN_MANAGEMENT_ENABLED": "true",
            "ECOREX_GATEWAY_ADMIN_MANAGEMENT_DATABASE_PATH": str(
                (tmp_path / "control-plane.sqlite3").resolve()
            ),
            "ECOREX_GATEWAY_MODEL_PROVIDER_ORIGINS_JSON": json.dumps(
                {
                    "ecorex_chat": "https://gpt.ecorex.invalid",
                    "deepseek_chat": "https://deepseek.ecorex.invalid",
                    "gemini_chat": "https://gemini.ecorex.invalid",
                    "doubao_chat": "https://doubao.ecorex.invalid",
                }
            ),
            "ECOREX_GATEWAY_CHAT_HANDOFF_TTL_SECONDS": "7200",
        }
    )
    config = GatewayProductionConfig.from_environment(environment)
    assert config.chat_handoff_ttl_seconds == 7200
    assert set(config.model_provider_origins) == {
        "ecorex_chat",
        "deepseek_chat",
        "gemini_chat",
        "doubao_chat",
    }


def test_gateway_request_carries_the_authoritative_chat_model_policy() -> None:
    request = _request()

    assert request.model_id == "ecorex-chat"
    assert request.model_policy.model_dump(mode="json") == {
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


def _tool_descriptor(tool_id: str = "fetch_document") -> dict[str, Any]:
    return {
        "spec": {
            "tool_id": tool_id,
            "version": "1.0.0",
            "display_name": "读取文档",
            "description": "读取一个办公文档。",
            "aliases": [],
            "effects": ["read"],
            "idempotency": "read_only",
            "concurrency_safe": True,
            "required_sandbox": "read-only",
            "approval_requirement": "never",
            "default_exposure": "direct",
            "intent_tags": [],
            "required_packs": [],
            "required_connectors": [],
            "supported_platforms": [],
            "input_schema": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
        "decision": {
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "exposure": "direct",
            "eligible": True,
            "requires_approval": False,
            "effective_sandbox": "read-only",
            "score": 100,
            "reason_codes": [],
        },
    }


class FakeProvider:
    def __init__(self) -> None:
        self.health_calls = 0
        self.stream_calls = 0
        self.closed = False

    async def health(self) -> None:
        self.health_calls += 1

    async def stream(self, request, principal):
        self.stream_calls += 1
        assert principal.account_id == "account-1"
        yield GatewayEvent(
            seq=1,
            event_type=GatewayEventType.RESPONSE_COMPLETED,
            response_id="response-1",
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeProviderFactory:
    def __init__(self) -> None:
        self.created: list[FakeProvider] = []

    def create(self, config, secrets):
        assert config.provider_origin == "https://provider.ecorex.invalid"
        assert secrets.read("provider-bearer-token") == PROVIDER_TOKEN
        provider = FakeProvider()
        self.created.append(provider)
        return provider


def test_shared_verifier_projects_typed_entitlements_and_gateway_requires_them() -> None:
    private, _encoded, public = _key()
    token = _jwt(private, models=["ecorex-chat", "image-2"])
    verifier = Ed25519AccessTokenVerifier(
        {"gateway-key": public},
        issuer="https://identity.ecorex.invalid",
        audience="ecorex-gateway",
    )
    claims = verifier.verify(token)
    assert claims.account_id == "account-1"
    assert claims.entitlements.allowed_model_ids == frozenset(
        {"ecorex-chat", "image-2"}
    )
    assert claims.entitlements.request_limit == 100

    authenticator = Ed25519GatewayJWTAuthenticator(
        {"gateway-key": public},
        issuer="https://identity.ecorex.invalid",
        audience="ecorex-gateway",
        service_model_ids=frozenset({"ecorex-chat"}),
    )
    principal = authenticator.authenticate(token)
    assert principal.allowed_model_ids == frozenset({"ecorex-chat"})
    with pytest.raises(PermissionError):
        authenticator.authenticate(_jwt(private, models=None))
    with pytest.raises(PermissionError):
        authenticator.authenticate(_jwt(private, models=["image-2"]))
    with pytest.raises(PermissionError):
        authenticator.authenticate(
            _jwt(private, models=["ecorex-chat"], account_id="a" * 129)
        )


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        b"data: "
        + json.dumps(
            {**event, "sequence_number": index},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n\n"
        for index, event in enumerate(events, start=1)
    )


def test_fixed_https_responses_adapter_translates_real_sse_contract() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []
        image_tool = _tool_descriptor("imagegen")
        image_tool["spec"]["description"] = "生成或编辑图片。"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={"object": "list", "data": [{"id": "gpt-5.6-luna"}]},
                    headers={"Content-Type": "application/json"},
                )
            assert request.url == "https://provider.ecorex.invalid/v1/responses"
            assert request.headers["authorization"] == "Bearer " + PROVIDER_TOKEN
            assert request.headers["idempotency-key"] == "request-1"
            submitted = json.loads(request.content)
            assert submitted["model"] == "gpt-5.6-luna"
            assert submitted["reasoning"] == {"effort": "max"}
            assert submitted["context_management"] == [
                {"type": "compaction", "compact_threshold": 272_000}
            ]
            assert submitted["stream"] is submitted["store"] is True
            assert submitted["tools"] == [
                {
                    "type": "function",
                    "name": "imagegen",
                    "description": "生成或编辑图片。",
                    "parameters": image_tool["spec"]["input_schema"],
                    "strict": False,
                },
                {
                    "type": "function",
                    "name": "fetch_document",
                    "description": "读取一个办公文档。",
                    "parameters": _tool_descriptor()["spec"]["input_schema"],
                    "strict": False,
                }
            ]
            assert "provider-workload" not in request.content.decode("utf-8")
            body = _sse(
                {
                    "type": "response.created",
                    "response": {"id": "resp_1", "model": "gpt-5.6-luna"},
                },
                {
                    "type": "response.reasoning_summary_text.delta",
                    "response_id": "resp_1",
                    "item_id": "reasoning_1",
                    "delta": "我会先提炼重点。",
                },
                {
                    "type": "response.output_text.delta",
                    "response_id": "resp_1",
                    "delta": "摘要",
                },
                {
                    "type": "response.output_item.added",
                    "response_id": "resp_1",
                    "item": {"type": "compaction", "id": "compaction_1"},
                },
                {
                    "type": "response.output_item.done",
                    "response_id": "resp_1",
                    "item": {"type": "compaction", "id": "compaction_1"},
                },
                {
                    "type": "response.output_item.added",
                    "response_id": "resp_1",
                    "item": {
                        "type": "function_call",
                        "id": "item_1",
                        "call_id": "call_1",
                        "name": "fetch_document",
                        "arguments": "",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "response_id": "resp_1",
                    "item_id": "item_1",
                    "delta": "{\"document_id\":\"doc_1\"}",
                },
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": "resp_1",
                    "item_id": "item_1",
                    "arguments": "{\"document_id\":\"doc_1\"}",
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "model": "gpt-5.6-luna",
                        "status": "completed",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                },
            )
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "text/event-stream"},
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=client,
        )
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        await provider.health()
        request = _request().model_copy(
            update={"direct_tools": [image_tool, _tool_descriptor()]}
        )
        events = [event async for event in provider.stream(request, principal)]
        assert [event.event_type for event in events] == [
            GatewayEventType.REASONING_SUMMARY_DELTA,
            GatewayEventType.OUTPUT_TEXT_DELTA,
            GatewayEventType.TOOL_CALL_REQUESTED,
        ]
        assert [event.seq for event in events] == [1, 2, 3]
        assert events[2].arguments == {"document_id": "doc_1"}
        assert events[2].idempotency_key.startswith("tool_")
        assert events[2].usage == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        await client.aclose()
        assert len(requests) == 2

    asyncio.run(scenario())


def test_responses_adapter_only_exposes_snapshot_disclosed_deferred_tools() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    500, headers={"Content-Type": "application/json"}
                )
            ),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=client,
        )
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        descriptor = json.loads(json.dumps(_tool_descriptor("vision")))
        descriptor["spec"]["default_exposure"] = "deferred"
        descriptor["decision"]["exposure"] = "deferred"

        with pytest.raises(ResponsesProviderRejected, match="descriptor"):
            provider._payload(
                _request().model_copy(update={"direct_tools": [descriptor]}),
                principal,
            )

        payload, mapping = provider._payload(
            _request().model_copy(
                update={
                    "direct_tools": [descriptor],
                    "disclosed_tool_ids": ["vision"],
                }
            ),
            principal,
        )
        assert payload["parallel_tool_calls"] is False
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "vision",
                "description": "读取一个办公文档。",
                "parameters": descriptor["spec"]["input_schema"],
                "strict": False,
            }
        ]
        assert mapping == {"vision": "vision"}

        tampered = _request().model_policy.model_dump(mode="python")
        tampered["context_management"]["compact_threshold_tokens"] = 271_999
        with pytest.raises(ResponsesProviderRejected, match="policy"):
            provider._payload(
                _request().model_copy(
                    update={"model_policy": GatewayModelPolicy.model_validate(tampered)}
                ),
                principal,
            )
        await client.aclose()

    asyncio.run(scenario())


def test_responses_adapter_preserves_ordered_tool_outputs_and_user_revisions() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    500, headers={"Content-Type": "application/json"}
                )
            ),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=client,
        )
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        base = _request().model_dump(mode="json")
        base.pop("input")
        base["previous_response_id"] = "response-before-tools"
        base["input_items"] = [
            {
                "type": "function_call_output",
                "tool_call_id": "call-1",
                "output": {"result": "done"},
            },
            {
                "type": "user_message",
                "message_id": "message-steer-1",
                "content": "把标题再简化一些。",
            },
            {
                "type": "user_message",
                "message_id": "message-steer-2",
                "content": "并保留数据来源。",
            },
        ]
        request = ModelGatewayRequest.model_validate(base)

        payload, _mapping = provider._payload(request, principal)

        assert payload["input"] == [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"result":"done"}',
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "把标题再简化一些。"}
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "并保留数据来源。"}
                ],
            },
        ]

        legacy_initial, _mapping = provider._payload(_request(), principal)
        assert legacy_initial["input"] == [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "请帮我整理摘要"}
                ],
            }
        ]
        legacy_continuation, _mapping = provider._payload(
            _request().model_copy(
                update={
                    "previous_response_id": "response-before-tools",
                    "tool_outputs": [
                        GatewayToolOutput(
                            tool_call_id="call-legacy",
                            output={"ok": True},
                        )
                    ],
                }
            ),
            principal,
        )
        assert legacy_continuation["input"] == [
            {
                "type": "function_call_output",
                "call_id": "call-legacy",
                "output": '{"ok":true}',
            }
        ]
        await client.aclose()

    asyncio.run(scenario())


def test_responses_adapter_preserves_completed_assistant_history_roles() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    500, headers={"Content-Type": "application/json"}
                )
            ),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=client,
        )
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        values = _request().model_dump(mode="python")
        values["input"] = None
        values["input_items"] = [
            GatewayUserMessageInput(message_id="message-1", content="列出 5 条标题"),
            GatewayAssistantMessageInput(
                message_id="message-2", content="1. 城市漫游 2. 海边周末"
            ),
            GatewayUserMessageInput(message_id="message-3", content="5"),
        ]
        request = ModelGatewayRequest.model_validate(values)

        payload, _mapping = provider._payload(request, principal)

        assert payload["input"] == [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "列出 5 条标题"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "1. 城市漫游 2. 海边周末"}
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "5"}],
            },
        ]
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("input_items", "previous_response_id", "legacy_input", "message"),
    [
        (
            [
                GatewayUserMessageInput(message_id="message-1", content="继续"),
                GatewayFunctionCallOutputInput(tool_call_id="call-1", output={}),
            ],
            "response-1",
            None,
            "must precede",
        ),
        (
            [
                GatewayUserMessageInput(message_id="message-1", content="一"),
                GatewayUserMessageInput(message_id="message-1", content="二"),
            ],
            None,
            None,
            "message IDs must be unique",
        ),
        (
            [
                GatewayFunctionCallOutputInput(tool_call_id="call-1", output={}),
                GatewayFunctionCallOutputInput(tool_call_id="call-1", output={}),
            ],
            "response-1",
            None,
            "output IDs must be unique",
        ),
        (
            [
                GatewayFunctionCallOutputInput(tool_call_id="shared-1", output={}),
                GatewayUserMessageInput(message_id="shared-1", content="继续"),
            ],
            "response-1",
            None,
            "item IDs must be unique",
        ),
        (
            [GatewayFunctionCallOutputInput(tool_call_id="call-1", output={})],
            None,
            None,
            "require a previous response",
        ),
        (
            [GatewayUserMessageInput(message_id="message-1", content="继续")],
            None,
            "legacy",
            "cannot be combined",
        ),
    ],
)
def test_typed_gateway_input_rejects_ambiguous_order_and_identity(
    input_items,
    previous_response_id,
    legacy_input,
    message,
) -> None:
    values = _request().model_dump(mode="python")
    values["input"] = legacy_input
    values["input_items"] = input_items
    values["previous_response_id"] = previous_response_id
    with pytest.raises(ValueError, match=message):
        ModelGatewayRequest.model_validate(values)


def test_typed_gateway_input_bounds_aggregate_user_text() -> None:
    values = _request().model_dump(mode="python")
    values["input"] = None
    values["input_items"] = [
        GatewayUserMessageInput(message_id="message-1", content="甲" * 600_000),
        GatewayUserMessageInput(message_id="message-2", content="乙" * 600_000),
    ]
    with pytest.raises(ValueError, match="oversized"):
        ModelGatewayRequest.model_validate(values)


def test_typed_gateway_input_bounds_item_count_and_validates_ids() -> None:
    values = _request().model_dump(mode="python")
    values["input"] = None
    values["input_items"] = [
        {
            "type": "user_message",
            "message_id": f"message-{index}",
            "content": "继续",
        }
        for index in range(257)
    ]
    with pytest.raises(ValueError, match="at most 256"):
        ModelGatewayRequest.model_validate(values)

    values["input_items"] = [
        {
            "type": "user_message",
            "message_id": "unsafe message id",
            "content": "继续",
        }
    ]
    with pytest.raises(ValueError, match="unsafe identifier"):
        ModelGatewayRequest.model_validate(values)


def test_tool_handoff_is_a_replayable_terminal_and_releases_concurrency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tool-handoff.sqlite3"
    GatewaySchemaManager(database).migrate()
    store = SQLiteGatewayStore(database)
    principal = GatewayPrincipal(
        subject="user-1",
        account_id="account-1",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=10,
        concurrent_request_limit=1,
    )
    body = _request("tool-round")
    reservation = store.reserve(body, principal, lease_seconds=30)
    handoff = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.TOOL_CALL_REQUESTED,
        response_id="resp_tool",
        tool_call_id="call_1",
        tool_name="read",
        arguments={"path": "report.docx"},
        idempotency_key="tool-call-1",
        usage={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
    )
    store.append_terminal(body.request_id, reservation.lease_token, handoff)
    replay = store.reserve(body, principal, lease_seconds=30)
    assert replay.mode == "replay"
    assert replay.events == (handoff,)
    facts = store.completed_usage_facts(request_id=body.request_id, maximum=1)
    assert len(facts) == 1
    assert facts[0].total_tokens == 9
    assert store.account_usage(
        "account-1",
        timezone_name="Asia/Shanghai",
    ).week.total_tokens >= 9
    assert store.reserve(
        _request("next-model-round"), principal, lease_seconds=30
    ).mode == "execute"


def test_runtime_client_accepts_tool_handoff_as_terminal_only_after_eof() -> None:
    handoff = GatewayEvent(
        seq=1,
        event_type=GatewayEventType.TOOL_CALL_REQUESTED,
        response_id="resp_tool_client",
        tool_call_id="call_client_1",
        tool_name="read",
        arguments={"path": "report.docx"},
        idempotency_key="tool-client-1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=handoff.model_dump_json().encode("utf-8") + b"\n",
        )

    class Credentials:
        def bearer_token(self) -> str:
            return "session_" + "x" * 32

    async def scenario() -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        )
        client = ManagedModelGatewayClient(
            "https://gateway.ecorex.invalid/v1/responses",
            credentials=Credentials(),
            allowed_hosts=frozenset({"gateway.ecorex.invalid"}),
            client=http,
        )
        events = [event async for event in client.stream(_request("client-tool"))]
        assert events == [handoff]
        await http.aclose()

    asyncio.run(scenario())


def test_responses_adapter_has_total_deadline_and_bounded_sse() -> None:
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield _sse(
                {
                    "type": "response.created",
                    "response": {"id": "resp_slow", "model": "gpt-5.6-luna"},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_slow",
                        "model": "gpt-5.6-luna",
                        "status": "completed",
                    },
                },
            )

    class Flood(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (1024 * 1024 + 1)

    async def scenario() -> None:
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        streams = iter((Slow(), Flood()))

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=next(streams),
                headers={"Content-Type": "text/event-stream"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            connect_timeout_seconds=0.1,
            read_timeout_seconds=0.5,
            total_timeout_seconds=0.5,
            client=client,
        )
        with pytest.raises(ResponsesProviderUnavailable):
            _ = [item async for item in provider.stream(_request("slow"), principal)]
        with pytest.raises(ResponsesProviderProtocolError, match="line"):
            _ = [item async for item in provider.stream(_request("flood"), principal)]
        await client.aclose()

    asyncio.run(scenario())


def test_responses_adapter_rejects_ssrf_redirect_and_secret_errors() -> None:
    with pytest.raises(ResponsesProviderConfigurationError):
        ManagedHTTPSResponsesProvider(
            origin="https://169.254.169.254",
            allowed_origins=frozenset({"https://169.254.169.254"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
        )
    with pytest.raises(ResponsesProviderConfigurationError, match="allowlisted"):
        ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://other.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
        )

    async def scenario() -> None:
        redirect_calls: list[str] = []

        def redirect(request: httpx.Request) -> httpx.Response:
            redirect_calls.append(str(request.url))
            return httpx.Response(
                302, headers={"Location": "https://evil.invalid/steal"}
            )

        redirect_client = httpx.AsyncClient(
            transport=httpx.MockTransport(redirect),
            follow_redirects=True,
        )
        redirect_provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=redirect_client,
        )
        principal = GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-07",
            request_limit=100,
        )
        with pytest.raises(ResponsesProviderRejected, match="rejected"):
            _ = [item async for item in redirect_provider.stream(_request(), principal)]
        assert redirect_calls == ["https://provider.ecorex.invalid/v1/responses"]
        await redirect_client.aclose()

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    302, headers={"Location": "https://evil.invalid/steal"}
                )
            ),
            follow_redirects=True,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: (_ for _ in ()).throw(
                RuntimeError("provider secret " + PROVIDER_TOKEN)
            ),
            client=client,
        )
        with pytest.raises(ResponsesProviderUnavailable) as captured:
            _ = [item async for item in provider.stream(_request(), principal)]
        assert PROVIDER_TOKEN not in str(captured.value)
        await client.aclose()

    asyncio.run(scenario())


def test_production_cli_migrate_check_serve_zero_ddl_and_single_process_lock(
    tmp_path: Path, capsys
) -> None:
    private, keyring, _public = _key()
    environment = _environment(tmp_path, keyring)
    factory = FakeProviderFactory()
    selected = SingleNodeSQLiteResponsesProvider(responses_factory=factory)
    secrets = EnvironmentGatewaySecretProvider(environment)

    assert gateway_main(
        ["schema", "migrate"],
        environment=environment,
        secret_provider=secrets,
        provider=selected,
    ) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["storage_backend"] == "sqlite-wal-single-node"
    assert gateway_main(
        ["schema", "check"],
        environment=environment,
        secret_provider=secrets,
        provider=selected,
    ) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "check"
    assert factory.created[-1].closed is True

    database = Path(environment["ECOREX_GATEWAY_DATABASE_PATH"])
    with sqlite3.connect(database) as connection:
        before = tuple(connection.execute(
            "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
        ))
    config = GatewayProductionConfig.from_environment(environment)
    bundle = selected.compose(config, secrets)
    try:
        with pytest.raises(GatewayProductionStorageError, match="another gateway"):
            selected.compose(config, secrets)
        with sqlite3.connect(database) as connection:
            after = tuple(connection.execute(
                "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
            ))
        assert after == before
        assert PROVIDER_TOKEN.encode() not in database.read_bytes()
    finally:
        asyncio.run(bundle.lifecycle.force_close())

    observed: dict[str, Any] = {}

    def runner(current) -> None:
        observed["schema"] = current.store.schema_receipt.target_schema_sha256

    assert gateway_main(
        ["serve"],
        environment=environment,
        secret_provider=secrets,
        provider=selected,
        server_runner=runner,
    ) == 0
    assert observed["schema"] == migrated["gateway_storage_schema_sha256"]
    assert factory.created[-1].closed is True
    assert _jwt(private, models=["ecorex-chat"])


def test_production_app_liveness_readiness_auth_model_isolation_and_drain(
    tmp_path: Path,
) -> None:
    private, keyring, _public = _key()
    environment = _environment(tmp_path, keyring)
    config = GatewayProductionConfig.from_environment(environment)
    secrets = EnvironmentGatewaySecretProvider(environment)
    selected = SingleNodeSQLiteResponsesProvider(
        responses_factory=FakeProviderFactory()
    )
    selected.migrate(config, secrets)
    bundle = selected.compose(config, secrets)
    token = _jwt(private, models=["ecorex-chat"])
    image_only = _jwt(private, models=["image-2"])
    oversized_account = _jwt(
        private, models=["ecorex-chat"], account_id="a" * 129
    )
    headers = {
        "Authorization": "Bearer " + token,
        "X-EcoreX-Protocol": "1",
    }
    with TestClient(bundle.create_app()) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/api/v1/models", headers=headers).json() == {
            "schema_version": 1,
            "models": ["ecorex-chat"],
        }
        denied = client.get(
            "/api/v1/models", headers={"Authorization": "Bearer " + image_only}
        )
        assert denied.status_code == 401
        incompatible = client.get(
            "/api/v1/models",
            headers={"Authorization": "Bearer " + oversized_account},
        )
        assert incompatible.status_code == 401
        completed = client.post(
            "/v1/responses",
            headers=headers,
            json=_request().model_dump(mode="json"),
        )
        assert completed.status_code == 200
        assert "response.completed" in completed.text
        bundle.lifecycle.begin_drain()
        assert client.get("/health/ready").status_code == 503
        draining = client.get("/api/v1/models", headers=headers)
        assert draining.status_code == 503
        assert draining.json() == {"status": "draining"}
    assert bundle.provider.closed is True


def test_uncertain_provider_disconnect_is_persisted_without_second_call_or_charge(
    tmp_path: Path,
) -> None:
    private, keyring, _public = _key()
    environment = _environment(tmp_path, keyring)
    config = GatewayProductionConfig.from_environment(environment)
    secrets = EnvironmentGatewaySecretProvider(environment)

    class UncertainProvider(FakeProvider):
        async def stream(self, request, principal):
            del request, principal
            self.stream_calls += 1
            if False:  # pragma: no cover - keeps this an async generator
                yield None
            raise ResponsesProviderUnavailable("secret upstream disconnect detail")

    class Factory:
        def __init__(self) -> None:
            self.current: UncertainProvider | None = None

        def create(self, _config, _secrets):
            self.current = UncertainProvider()
            return self.current

    factory = Factory()
    selected = SingleNodeSQLiteResponsesProvider(responses_factory=factory)
    selected.migrate(config, secrets)
    bundle = selected.compose(config, secrets)
    token = _jwt(private, models=["ecorex-chat"])
    headers = {
        "Authorization": "Bearer " + token,
        "X-EcoreX-Protocol": "1",
    }
    with TestClient(bundle.create_app()) as client:
        first = client.post(
            "/v1/responses",
            headers=headers,
            json=_request("uncertain-provider").model_dump(mode="json"),
        )
        replay = client.post(
            "/v1/responses",
            headers=headers,
            json=_request("uncertain-provider").model_dump(mode="json"),
        )
        first_event = json.loads(first.text)
        assert first_event["event_type"] == "response.failed"
        assert first_event["retryable"] is False
        assert "secret upstream" not in first.text
        assert replay.text == first.text
        assert replay.headers["x-ecorex-replay"] == "true"
        assert factory.current is not None and factory.current.stream_calls == 1
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_requests WHERE request_id=?",
            ("uncertain-provider",),
        ).fetchone()[0] == 1


def test_production_configuration_fails_closed_without_schema_or_secrets(
    tmp_path: Path, capsys
) -> None:
    _private, keyring, _public = _key()
    environment = _environment(tmp_path, keyring)
    missing_backend = dict(environment)
    del missing_backend["ECOREX_GATEWAY_STORAGE_BACKEND"]
    with pytest.raises(GatewayProductionConfigurationError):
        GatewayProductionConfig.from_environment(missing_backend)
    wrong_upstream = dict(environment)
    wrong_upstream["ECOREX_GATEWAY_MODEL_MAPPING_JSON"] = json.dumps(
        {"ecorex-chat": "gpt-5.6-sol"}
    )
    with pytest.raises(
        GatewayProductionConfigurationError,
        match="managed model policy",
    ):
        GatewayProductionConfig.from_environment(wrong_upstream)
    assert EnvironmentGatewaySecretProvider(environment).read(
        "provider-bearer-token"
    ) == environment["ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN"]
    assert gateway_main(["serve"], environment=environment) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["status"] == "failed"
    assert PROVIDER_TOKEN not in json.dumps(failure)
    assert not Path(environment["ECOREX_GATEWAY_DATABASE_PATH"]).exists()
