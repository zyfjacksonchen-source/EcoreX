from __future__ import annotations

import asyncio
import json

import httpx

import pytest

from ecorex.gateway.chat_completions_provider import (
    ManagedHTTPSChatCompletionsProvider,
    _ChatCompletionParser,
)
from ecorex.gateway.handoff import ChatModelRevision
from ecorex.gateway.models import (
    GatewayEventType,
    ModelGatewayRequest,
    ecorex_chat_gateway_policy,
)
from ecorex.gateway.responses_provider import ResponsesProviderProtocolError
from ecorex.gateway.server import GatewayPrincipal


def test_stream_chat_completion_maps_text_reasoning_usage() -> None:
    parser = _ChatCompletionParser(
        "request-chat-stream",
        expected_model_id="deepseek-v4-flash",
        tool_names={},
    )
    first = parser.feed_stream(
        {
            "id": "chatcmpl_1",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "reasoning_summary": "正在检查资料。",
                        # Raw provider reasoning is intentionally ignored.
                        "reasoning_content": "private chain of thought",
                        "content": "完成",
                    },
                    "finish_reason": None,
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        }
    )
    terminal = parser.feed_stream(
        {
            "id": "chatcmpl_1",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
            },
        }
    )
    assert [event.event_type for event in first + terminal] == [
        GatewayEventType.REASONING_SUMMARY_DELTA,
        GatewayEventType.OUTPUT_TEXT_DELTA,
        GatewayEventType.RESPONSE_COMPLETED,
    ]
    assert terminal[0].usage == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
    }
    assert all("private" not in (event.delta or "") for event in first)


def test_nonstream_chat_completion_maps_one_tool_handoff() -> None:
    parser = _ChatCompletionParser(
        "request-chat-tool",
        expected_model_id="gemini-3.1-pro-preview",
        tool_names={"fetch_document": "fetch.document"},
    )
    events = parser.feed_response(
        {
            "id": "chatcmpl_tool_1",
            "model": "gemini-3.1-pro-preview",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "fetch_document",
                                    "arguments": '{"document_id":"doc-1"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        }
    )
    assert len(events) == 1
    assert events[0].event_type is GatewayEventType.TOOL_CALL_REQUESTED
    assert events[0].tool_name == "fetch.document"
    assert events[0].arguments == {"document_id": "doc-1"}
    assert events[0].usage == {
        "input_tokens": 8,
        "output_tokens": 2,
        "total_tokens": 10,
    }
    assert parser.handoff_message is not None


def test_parallel_chat_tool_calls_fail_closed() -> None:
    parser = _ChatCompletionParser(
        "request-chat-tools",
        expected_model_id="doubao",
        tool_names={"one": "one", "two": "two"},
    )
    with pytest.raises(ResponsesProviderProtocolError, match="parallel"):
        parser.feed_response(
            {
                "id": "chatcmpl_parallel",
                "model": "doubao",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "call_1", "function": {"name": "one", "arguments": "{}"}},
                                {"id": "call_2", "function": {"name": "two", "arguments": "{}"}},
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )


def test_provider_stages_handoff_before_exposing_tool_terminal() -> None:
    calls: list[str] = []

    class Authority:
        def bind_chat_model_attempt(self, *_args, **_kwargs):
            calls.append("bind")

        def consume_chat_handoff(self, *_args, **_kwargs):
            calls.append("consume")
            return None

        def stage_chat_handoff(self, _request, _revision, event, **values):
            assert event.tool_call_id == "call_1"
            assert values == {
                "provider_tool_name": "fetch_document",
                "arguments_json": '{"document_id":"doc-1"}',
            }
            calls.append("stage")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append("post")
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["parallel_tool_calls"] is False
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "id": "chatcmpl_provider_tool",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fetch_document",
                                        "arguments": '{ "document_id": "doc-1" }',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 3,
                    "total_tokens": 12,
                },
            },
        )

    descriptor = {
        "spec": {
            "tool_id": "fetch_document",
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
            "tool_id": "fetch_document",
            "tool_version": "1.0.0",
            "exposure": "direct",
            "eligible": True,
            "requires_approval": False,
            "effective_sandbox": "read-only",
            "score": 100,
            "reason_codes": [],
        },
    }
    request = ModelGatewayRequest(
        request_id="provider-tool-request",
        thread_id="thread-1",
        turn_id="turn-1",
        trace_id="trace-1",
        model_id="ecorex-deepseek-v4-pro",
        model_policy=ecorex_chat_gateway_policy("ecorex-deepseek-v4-pro"),
        input="读取文档",
        config_snapshot_id="config-1",
        capability_snapshot_id="capability-1",
        permission_snapshot_id="permission-1",
        direct_tools=[descriptor],
    )
    authority = Authority()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ManagedHTTPSChatCompletionsProvider(
        origin="https://deepseek.ecorex.invalid",
        allowed_origins=frozenset({"https://deepseek.ecorex.invalid"}),
        model_mapping={"ecorex-deepseek-v4-pro": "deepseek-v4-flash"},
        model_policies={
            "ecorex-deepseek-v4-pro": ecorex_chat_gateway_policy(
                "ecorex-deepseek-v4-pro"
            )
        },
        bearer_token=lambda: "provider-secret-value",
        handoff_authority=authority,
        model_revision=ChatModelRevision(
            config_id="model-deepseek",
            revision=7,
            local_model_id="ecorex-deepseek-v4-pro",
            upstream_model_id="deepseek-v4-flash",
            provider_protocol="openai_compatible_chat",
            provider_origin_preset="deepseek_chat",
        ),
        client=client,
    )
    principal = GatewayPrincipal(
        subject="member-1",
        account_id="account-1",
        allowed_model_ids=frozenset({"ecorex-deepseek-v4-pro"}),
        quota_period="2026-07",
        request_limit=10,
    )

    async def collect():
        return [event async for event in provider.stream(request, principal)]

    events = asyncio.run(collect())
    asyncio.run(client.aclose())
    assert [event.event_type for event in events] == [
        GatewayEventType.TOOL_CALL_REQUESTED
    ]
    assert events[0].usage == {
        "input_tokens": 9,
        "output_tokens": 3,
        "total_tokens": 12,
    }
    assert calls == ["bind", "consume", "post", "stage"]
