from __future__ import annotations

import asyncio
import copy

import pytest

from ecorex.gateway import (
    MAX_DISCLOSED_WORKING_SET,
    MAX_MODEL_VISIBLE_TOOLS,
    MAX_TOOL_DESCRIPTOR_BYTES,
    MAX_TOOL_SCHEMA_BATCH_BYTES,
    GatewayPrincipal,
    ModelGatewayRequest,
    canonical_tool_descriptor_bytes,
    canonical_tool_schema_batch_bytes,
)
from ecorex.gateway.responses_provider import (
    ManagedHTTPSResponsesProvider,
    ResponsesProviderRejected,
)


_TOKEN = "provider-workload-token-00000001"


def _descriptor(
    tool_id: str,
    *,
    exposure: str = "direct",
    schema_padding: int = 0,
) -> dict:
    return {
        "spec": {
            "tool_id": tool_id,
            "version": "1.0.0",
            "display_name": tool_id,
            "description": f"Use {tool_id}.",
            "aliases": [],
            "effects": ["read"],
            "idempotency": "read_only",
            "concurrency_safe": True,
            "required_sandbox": "read-only",
            "approval_requirement": "never",
            "default_exposure": exposure,
            "priority_bias": 0,
            "intent_tags": [],
            "routing_facets": [],
            "required_packs": [],
            "required_connectors": [],
            "required_model_modalities": [],
            "required_model_capabilities": {},
            "supported_platforms": [],
            "input_schema": {
                "type": "object",
                "description": "x" * schema_padding,
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
        "decision": {
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "exposure": exposure,
            "eligible": True,
            "requires_approval": False,
            "effective_sandbox": "read-only",
            "score": 100,
            "reason_codes": [],
            "matched_evidence": [],
            "suppression_reasons": [],
        },
    }


def _request(**updates) -> ModelGatewayRequest:
    values = {
        "request_id": "request-budget",
        "thread_id": "thread-budget",
        "turn_id": "turn-budget",
        "trace_id": "trace-budget",
        "model_id": "ecorex-chat",
        "input": "整理这份资料",
        "config_snapshot_id": "config-budget",
        "capability_snapshot_id": "capability-budget",
        "permission_snapshot_id": "permission-budget",
    }
    values.update(updates)
    return ModelGatewayRequest(**values)


def _provider() -> ManagedHTTPSResponsesProvider:
    return ManagedHTTPSResponsesProvider(
        origin="https://provider.ecorex.invalid",
        allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
        model_mapping={"ecorex-chat": "gpt-5.6-luna"},
        bearer_token=lambda: _TOKEN,
    )


def _principal() -> GatewayPrincipal:
    return GatewayPrincipal(
        subject="user-budget",
        account_id="account-budget",
        allowed_model_ids=frozenset({"ecorex-chat"}),
        quota_period="2026-07",
        request_limit=100,
    )


def test_gateway_request_enforces_visible_and_disclosed_boundaries() -> None:
    direct = [_descriptor(f"direct_{index}") for index in range(MAX_MODEL_VISIBLE_TOOLS)]
    assert len(_request(direct_tools=direct).direct_tools) == MAX_MODEL_VISIBLE_TOOLS

    expanded = [*direct, _descriptor("direct_overflow")]
    assert _request(direct_tools=expanded).direct_tools == expanded

    disclosed = [
        _descriptor(f"grant_{index}", exposure="deferred")
        for index in range(MAX_DISCLOSED_WORKING_SET)
    ]
    disclosed_ids = [item["spec"]["tool_id"] for item in disclosed]
    assert len(
        _request(
            direct_tools=disclosed,
            disclosed_tool_ids=disclosed_ids,
        ).disclosed_tool_ids
    ) == 12

    with pytest.raises(ValueError, match="at most 12|working set exceeds"):
        overflow = [*disclosed, _descriptor("grant_overflow", exposure="deferred")]
        _request(
            direct_tools=overflow,
            disclosed_tool_ids=[item["spec"]["tool_id"] for item in overflow],
        )


def test_gateway_request_preserves_large_cow_tool_schemas() -> None:
    exact = _descriptor("exact_descriptor")
    base_size = len(canonical_tool_descriptor_bytes(exact))
    exact["spec"]["input_schema"]["description"] = "x" * (
        MAX_TOOL_DESCRIPTOR_BYTES - base_size
    )
    assert len(canonical_tool_descriptor_bytes(exact)) == MAX_TOOL_DESCRIPTOR_BYTES
    assert _request(direct_tools=[exact]).direct_tools[0] == exact

    oversized = copy.deepcopy(exact)
    oversized["spec"]["input_schema"]["description"] += "x"
    assert _request(direct_tools=[oversized]).direct_tools == [oversized]

    large_descriptors = [
        _descriptor(f"large_{index}", schema_padding=90 * 1024)
        for index in range(3)
    ]
    assert all(
        len(canonical_tool_descriptor_bytes(item)) < MAX_TOOL_DESCRIPTOR_BYTES
        for item in large_descriptors
    )
    assert (
        len(canonical_tool_schema_batch_bytes(large_descriptors))
        > MAX_TOOL_SCHEMA_BATCH_BYTES
    )
    assert _request(direct_tools=large_descriptors).direct_tools == large_descriptors


def test_provider_preserves_large_cow_catalog_after_unvalidated_model_copy() -> None:
    provider = _provider()
    valid = _request()
    bypassed = valid.model_copy(
        update={
            "direct_tools": [
                _descriptor(f"provider_flood_{index}")
                for index in range(MAX_MODEL_VISIBLE_TOOLS + 1)
            ]
        }
    )
    try:
        payload, _names = provider._payload(bypassed, _principal())
        assert len(payload["tools"]) == MAX_MODEL_VISIBLE_TOOLS + 1

        oversized = _descriptor("provider_oversized")
        current = len(canonical_tool_descriptor_bytes(oversized))
        oversized["spec"]["input_schema"]["description"] = "x" * (
            MAX_TOOL_DESCRIPTOR_BYTES - current + 1
        )
        payload, _names = provider._payload(
            valid.model_copy(update={"direct_tools": [oversized]}),
            _principal(),
        )
        assert payload["tools"][0]["parameters"] == oversized["spec"]["input_schema"]

        batch_overflow = [
            _descriptor(f"provider_large_{index}", schema_padding=90 * 1024)
            for index in range(3)
        ]
        assert all(
            len(canonical_tool_descriptor_bytes(item)) < MAX_TOOL_DESCRIPTOR_BYTES
            for item in batch_overflow
        )
        payload, _names = provider._payload(
            valid.model_copy(update={"direct_tools": batch_overflow}),
            _principal(),
        )
        assert [tool["parameters"] for tool in payload["tools"]] == [
            item["spec"]["input_schema"] for item in batch_overflow
        ]

        disclosed_overflow = [
            _descriptor(f"provider_grant_{index}", exposure="deferred")
            for index in range(MAX_DISCLOSED_WORKING_SET + 1)
        ]
        with pytest.raises(ResponsesProviderRejected, match="count budget"):
            provider._payload(
                valid.model_copy(
                    update={
                        "direct_tools": disclosed_overflow,
                        "disclosed_tool_ids": [
                            item["spec"]["tool_id"] for item in disclosed_overflow
                        ],
                    }
                ),
                _principal(),
            )
    finally:
        asyncio.run(provider.aclose())
