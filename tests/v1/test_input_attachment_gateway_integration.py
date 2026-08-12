from __future__ import annotations

import base64
import asyncio
import hashlib
import io
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from ecorex.gateway import GatewayEvent
from ecorex.gateway.responses_provider import ManagedHTTPSResponsesProvider
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.worker import LegacyAgentTurnWorker as AgentTurnWorker


TOKEN = "m" * 32
CSRF = "n" * 32
ORIGIN = "http://testserver"


class ProviderSerializingGateway:
    """Capture the real provider payload produced from a Worker request."""

    def __init__(self) -> None:
        self.requests = []
        self.provider_payloads = []
        self.errors = []

    async def stream(self, request):
        self.requests.append(request)
        provider = SimpleNamespace(
            validate_request=lambda _request, _principal: None,
            model_policies={request.model_id: request.model_policy},
            model_mapping={request.model_id: request.model_policy.upstream_model_id},
        )
        try:
            payload, _names = ManagedHTTPSResponsesProvider._payload(
                provider,
                request,
                SimpleNamespace(account_id="local-user"),
            )
        except Exception as error:
            self.errors.append(error)
            raise
        self.provider_payloads.append(payload)
        yield GatewayEvent(
            seq=1,
            event_type="output_text.delta",
            response_id="response_multimodal_integration",
            delta="done",
        )
        yield GatewayEvent(
            seq=2,
            event_type="response.completed",
            response_id="response_multimodal_integration",
        )


def _headers(*, mutation: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if mutation:
        headers.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF})
    return headers


def retired_legacy_uploaded_image_reaches_worker_and_real_responses_provider_payload(tmp_path) -> None:
    gateway = ProviderSerializingGateway()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            model_gateway=gateway,
            allow_unmanaged_model_gateway_for_testing=True,
            model_worker_concurrency=1,
        )
    )
    source = io.BytesIO()
    Image.new("RGB", (32, 24), (245, 120, 32)).save(source, format="PNG")
    image = source.getvalue()
    # Do not start the background supervisor here: drive the production Worker
    # once explicitly so this test proves every boundary deterministically.
    client = TestClient(app)
    uploaded = client.post(
        "/api/v1/input-attachments",
        headers=_headers(mutation=True),
        files={"file": ("receipt.png", image, "image/png")},
        data={"client_request_id": "upload-multimodal-integration"},
    )
    assert uploaded.status_code == 201
    attachment = uploaded.json()
    thread = client.post(
        "/api/v1/threads",
        headers=_headers(mutation=True),
        json={"client_request_id": "thread-multimodal-integration"},
    ).json()
    created = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=_headers(mutation=True),
        json={
            "input": "请识别这张图片",
            "attachment_ids": [attachment["attachment_id"]],
            "client_message_id": "message-multimodal-integration",
        },
    )
    assert created.status_code == 202
    worker = AgentTurnWorker(
        app.state.runtime,
        gateway=gateway,
        capabilities=app.state.runtime_composition.capability_service,
        turn_preparer=app.state.runtime_composition.prepare_turn,
        permission_mutation_lock=(
            app.state.runtime_composition.permission_mutation_lock
        ),
        permission_account_id=app.state.runtime_composition.permission_account_id,
        connector_uncertain_resolver=(
            app.state.connector_composition.repository.resolve_uncertain_invocation
        ),
        input_attachments=app.state.input_attachment_service,
    )
    result = asyncio.run(worker.run_once("worker-multimodal-integration"))
    assert result.outcome.value == "completed", (result, gateway.errors)
    asyncio.run(worker.close())

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    user = request.input_items[-1]
    assert user.images[0].attachment_id == attachment["attachment_id"]
    assert user.images[0].source_sha256 == hashlib.sha256(image).hexdigest()
    rendition = base64.b64decode(user.images[0].data_base64)
    assert hashlib.sha256(rendition).hexdigest() == user.images[0].sha256

    payload = gateway.provider_payloads[0]
    content = payload["input"][-1]["content"]
    assert content[0]["type"] == "input_text"
    assert "请识别这张图片" in content[0]["text"]
    assert content[1] == {
        "type": "input_image",
        "image_url": (
            f"data:{user.images[0].mime_type};base64,{user.images[0].data_base64}"
        ),
    }
