from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from ecorex.control_plane.management_models import ActiveModelConfiguration
from ecorex.control_plane.model_activation import (
    HTTPSModelConnectionTester,
    _activation_png,
)


def configuration(
    *,
    modality: str = "chat",
    preset: str = "responses",
    local_model_id: str = "ecorex-chat",
    upstream_model_id: str = "gpt-5.6-luna",
) -> ActiveModelConfiguration:
    return ActiveModelConfiguration(
        config_id="config-activation",
        revision=3,
        local_model_id=local_model_id,
        modality=modality,  # type: ignore[arg-type]
        display_name="Activation model",
        upstream_model_id=upstream_model_id,
        provider_preset=preset,  # type: ignore[arg-type]
        is_default=True,
        api_key="test-activation-secret-value",
    )


def run_test(
    handler,
    model: ActiveModelConfiguration,
    *,
    origins: dict[str, str] | None = None,
):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tester = HTTPSModelConnectionTester(
        origins or {model.provider_preset: "https://models.ecorex.example"},
        client=client,
        timeout_seconds=30,
    )
    try:
        return asyncio.run(tester.test(model))
    finally:
        asyncio.run(client.aclose())


def catalog(model_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={"data": [{"id": model_id}]},
    )


def test_catalog_visibility_is_not_enough_to_activate() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/models":
            return catalog("gpt-5.6-luna")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"model": "gpt-5.6-luna", "output": []},
        )

    result = run_test(handler, configuration())
    assert result.passed is False
    assert result.error_code == "provider_test_protocol"
    assert requests == ["/v1/models", "/v1/responses"]


def test_responses_probe_freezes_endpoint_model_and_no_storage() -> None:
    submitted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return catalog("gpt-5.6-luna")
        submitted.append(request)
        body = json.loads(request.content)
        assert body == {
            "model": "gpt-5.6-luna",
            "instructions": "Return exactly ECOREX_ACTIVATION_OK and no other text.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "e-Mate administrator activation probe.",
                        }
                    ],
                }
            ],
            "max_output_tokens": 512,
            "store": False,
            "reasoning": {"effort": "high"},
            "context_management": [
                {"type": "compaction", "compact_threshold": 272_000}
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "model": "gpt-5.6-luna-2026-07-01",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "ECOREX_ACTIVATION_OK",
                            }
                        ],
                    }
                ],
            },
        )

    result = run_test(handler, configuration())
    assert result.passed
    assert len(submitted) == 1
    assert submitted[0].url.path == "/v1/responses"
    assert submitted[0].headers["accept-encoding"] == "identity"
    assert submitted[0].headers["idempotency-key"] == (
        "ecorex-model-activation-c5240da1035c4bda7783d3d15c741357"
    )


def test_openai_compatible_chat_uses_chat_completion_probe() -> None:
    paths: list[str] = []
    model = configuration(
        preset="openai_compatible_chat", upstream_model_id="chat-compatible-1"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return catalog("chat-compatible-1")
        body = json.loads(request.content)
        assert body["model"] == "chat-compatible-1"
        assert body["stream"] is False
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "model": "chat-compatible-1",
                "choices": [
                    {"message": {"content": "ECOREX_ACTIVATION_OK"}}
                ],
            },
        )

    result = run_test(handler, model)
    assert result.passed
    assert paths == ["/v1/models", "/v1/chat/completions"]


@pytest.mark.parametrize(
    ("modality", "local_model_id", "path"),
    [
        ("image_generation", "gpt-image-2", "/v1/images/generations"),
        ("image_edit", "gpt-image-2-edit", "/v1/images/edits"),
    ],
)
def test_image_probe_exercises_the_actual_operation(
    modality: str, local_model_id: str, path: str
) -> None:
    paths: list[str] = []
    model = configuration(
        modality=modality,
        preset="openai_compatible_image",
        local_model_id=local_model_id,
        upstream_model_id="gpt-image-2",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return catalog("gpt-image-2")
        assert request.url.path == path
        assert request.headers["idempotency-key"].startswith(
            "ecorex-model-activation-"
        )
        if modality == "image_generation":
            body = json.loads(request.content)
            assert body["model"] == "gpt-image-2"
            assert body["size"] == "1024x1024"
            assert body["output_format"] == "png"
            assert body["response_format"] == "b64_json"
        else:
            assert b'name="model"' in request.content
            assert b"gpt-image-2" in request.content
            assert b'name="response_format"' in request.content
            assert b"b64_json" in request.content
            assert b'filename="ecorex-activation.png"' in request.content
            assert b"\x89PNG\r\n\x1a\n" in request.content
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "data": [
                    {"b64_json": base64.b64encode(_activation_png()).decode("ascii")}
                ]
            },
        )

    result = run_test(handler, model)
    assert result.passed
    assert paths == ["/v1/models", path]


def test_image_probe_accepts_bounded_native_square_for_runtime_normalization() -> None:
    output = BytesIO()
    image = Image.new("RGB", (1254, 1254), (230, 120, 30))
    image.save(output, format="PNG")
    image.close()
    native = output.getvalue()
    model = configuration(
        modality="image_generation",
        preset="openai_compatible_image",
        local_model_id="gpt-image-2",
        upstream_model_id="gpt-image-2",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return catalog("gpt-image-2")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"data": [{"b64_json": base64.b64encode(native).decode("ascii")}]},
        )

    assert run_test(handler, model).passed


def test_uncertain_submission_is_never_retried() -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/v1/models":
            return catalog("gpt-5.6-luna")
        submissions += 1
        raise httpx.ReadTimeout("result unknown", request=request)

    result = run_test(handler, configuration())
    assert result.passed is False
    assert result.error_code == "provider_test_uncertain"
    assert submissions == 1


def test_server_error_after_submission_is_uncertain_and_never_retried() -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/v1/models":
            return catalog("gpt-5.6-luna")
        submissions += 1
        return httpx.Response(
            503,
            headers={"Content-Type": "application/json"},
            json={"error": {"code": "upstream_result_unknown"}},
        )

    result = run_test(handler, configuration())
    assert result.passed is False
    assert result.error_code == "provider_test_uncertain"
    assert submissions == 1


def test_catalog_timeout_is_not_reported_as_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("not connected", request=request)

    result = run_test(handler, configuration())
    assert result.passed is False
    assert result.error_code == "provider_test_timeout"


def test_image_url_only_result_never_activates() -> None:
    model = configuration(
        modality="image_generation",
        preset="openai_compatible_image",
        local_model_id="gpt-image-2",
        upstream_model_id="gpt-image-2",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return catalog("gpt-image-2")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"data": [{"url": "https://untrusted.invalid/result.png"}]},
        )

    result = run_test(handler, model)
    assert result.passed is False
    assert result.error_code == "provider_test_protocol"


@pytest.mark.parametrize(
    "origin",
    [
        "http://models.ecorex.example",
        "https://localhost",
        "https://127.0.0.1",
        "https://models.ecorex.example:8443",
        "https://user@models.ecorex.example",
        "https://models.ecorex.example/v1",
    ],
)
def test_provider_origin_remains_public_https_only(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        HTTPSModelConnectionTester({"responses": origin})
