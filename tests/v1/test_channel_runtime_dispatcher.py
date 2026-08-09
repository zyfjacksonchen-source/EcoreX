from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ecorex.connectors import (
    ChannelCredentialOwner,
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
)
from ecorex.gateway import GatewayEvent
from ecorex.runtime import RuntimeSettings, create_app


class _Gateway:
    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent.model_validate(
            {
                "seq": 1,
                "event_type": "output_text.delta",
                "response_id": f"response-{len(self.requests)}",
                "delta": f"answer-{len(self.requests)}",
            }
        )
        yield GatewayEvent.model_validate(
            {
                "seq": 2,
                "event_type": "response.completed",
                "response_id": f"response-{len(self.requests)}",
            }
        )

    async def aclose(self) -> None:
        return None


class _Transport:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self._delivered: set[str] = set()

    def send_text(self, **message: str) -> None:
        key = message["idempotency_key"]
        if key in self._delivered:
            return
        self._delivered.add(key)
        self.sent.append(message)


def _wait_for_reply(dispatcher, receipt):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        reply = dispatcher.project_outbound(receipt)
        if reply is not None:
            return reply
        time.sleep(0.01)
    raise TimeoutError("channel reply was not projected")


def test_channel_dispatcher_reuses_runtime_continuity_and_facts(tmp_path) -> None:
    gateway = _Gateway()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token="r" * 32,
            csrf_token="c" * 32,
            webui_origins=("http://testserver",),
            model_gateway=gateway,
            allow_unmanaged_model_gateway_for_testing=True,
            model_worker_concurrency=1,
            model_worker_poll_seconds=0.01,
            model_worker_shutdown_seconds=1,
        )
    )
    conversation_id = "external-chat-42"
    message_id = "external-message-1"

    with TestClient(app):
        dispatcher = ChannelRuntimeDispatcher(
            owner=ChannelCredentialOwner("account-a", "organization-a"),
            composition=app.state.runtime_composition,
            kernel=app.state.runtime,
            worker=app.state.model_worker_supervisor,
        )
        inbound = ChannelInboundMessage(
            channel_id="telegram",
            conversation_id=conversation_id,
            message_id=message_id,
            text="first",
        )
        first = dispatcher.dispatch(inbound)
        duplicate = dispatcher.dispatch(inbound)
        first_reply = _wait_for_reply(dispatcher, first)

        assert duplicate == first
        assert first_reply.text == "answer-1"
        assert len(gateway.requests) == 1

        second = dispatcher.dispatch(
            ChannelInboundMessage(
                channel_id="telegram",
                conversation_id=conversation_id,
                message_id="external-message-2",
                text="continue",
            )
        )
        second_reply = _wait_for_reply(dispatcher, second)

        assert second.thread_id == first.thread_id
        assert second.turn_id != first.turn_id
        assert second_reply.text == "answer-2"
        assert len(gateway.requests) == 2

        projection = app.state.runtime.projection(first.thread_id)
        assert conversation_id not in repr(projection)
        assert message_id not in repr(projection)
        assert projection.turns[0].metadata["channel"]["channel_id"] == "telegram"

        transport = _Transport()
        assert dispatcher.deliver(
            first,
            conversation_id=conversation_id,
            transport=transport,
        )
        assert dispatcher.deliver(
            first,
            conversation_id=conversation_id,
            transport=transport,
        )
        assert [item["text"] for item in transport.sent] == ["answer-1"]

        with pytest.raises(ValueError, match="does not match"):
            dispatcher.deliver(
                first,
                conversation_id="wrong-chat",
                transport=transport,
            )
