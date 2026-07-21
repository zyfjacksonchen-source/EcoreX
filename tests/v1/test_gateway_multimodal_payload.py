from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ecorex.gateway import (
    GatewayImageInput,
    GatewayUserMessageInput,
    ModelGatewayRequest,
    ecorex_chat_gateway_policy,
)
from ecorex.gateway.chat_completions_provider import (
    ManagedHTTPSChatCompletionsProvider,
)
from ecorex.gateway.responses_provider import ManagedHTTPSResponsesProvider


def _image(content: bytes = b"bounded-image") -> GatewayImageInput:
    return GatewayImageInput(
        attachment_id="artifact_image_1",
        revision_id="revision_image_1",
        mime_type="image/png",
        data_base64=base64.b64encode(content).decode("ascii"),
        sha256=hashlib.sha256(content).hexdigest(),
        source_sha256=hashlib.sha256(content).hexdigest(),
    )


def _request() -> ModelGatewayRequest:
    policy = ecorex_chat_gateway_policy()
    return ModelGatewayRequest(
        request_id="request_multimodal_1",
        thread_id="thread_multimodal_1",
        turn_id="turn_multimodal_1",
        trace_id="trace_multimodal_1",
        model_id=policy.local_model_id,
        model_policy=policy,
        input_items=[
            GatewayUserMessageInput(
                message_id="message_multimodal_1",
                content="请识别这张图片",
                images=[_image()],
            )
        ],
        config_snapshot_id="config_multimodal_1",
        capability_snapshot_id="capability_multimodal_1",
        permission_snapshot_id="permission_multimodal_1",
    )


def test_responses_payload_contains_runtime_attested_input_image() -> None:
    request = _request()
    fake = SimpleNamespace(
        validate_request=lambda _request, _principal: None,
        model_policies={request.model_id: request.model_policy},
        model_mapping={request.model_id: request.model_policy.upstream_model_id},
    )
    principal = SimpleNamespace(account_id="account_1")

    payload, _names = ManagedHTTPSResponsesProvider._payload(
        fake, request, principal
    )

    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "请识别这张图片"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert "artifact_image_1" not in content[1]["image_url"]


def test_chat_completions_payload_contains_compatible_image_url_part() -> None:
    request = _request()
    fake = SimpleNamespace(
        model_mapping={request.model_id: request.model_policy.upstream_model_id}
    )

    payload, _names = ManagedHTTPSChatCompletionsProvider._payload(
        fake, request, prior=None
    )

    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "请识别这张图片"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_gateway_image_rejects_invalid_digest_and_four_mib_unsafe_size() -> None:
    with pytest.raises(ValidationError, match="digest"):
        GatewayImageInput(
            attachment_id="artifact_image_1",
            revision_id="revision_image_1",
            mime_type="image/png",
            data_base64=base64.b64encode(b"content").decode("ascii"),
            sha256="0" * 64,
            source_sha256=hashlib.sha256(b"content").hexdigest(),
        )

    oversized = b"x" * (1536 * 1024 + 1)
    with pytest.raises(ValidationError, match="oversized"):
        _image(oversized)


def test_maximum_image_input_keeps_signed_gateway_envelope_below_four_mib() -> None:
    content = b"x" * (1536 * 1024)
    policy = ecorex_chat_gateway_policy()
    request = ModelGatewayRequest(
        request_id="request_boundary_1",
        thread_id="thread_boundary_1",
        turn_id="turn_boundary_1",
        trace_id="trace_boundary_1",
        model_id=policy.local_model_id,
        model_policy=policy,
        input_items=[
            GatewayUserMessageInput(
                message_id="message_boundary_1",
                content="inspect",
                images=[_image(content)],
            )
        ],
        config_snapshot_id="config_boundary_1",
        capability_snapshot_id="capability_boundary_1",
        permission_snapshot_id="permission_boundary_1",
    )

    assert len(request.model_dump_json().encode("utf-8")) < 4 * 1024 * 1024
