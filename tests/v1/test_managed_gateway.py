from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ecorex.gateway import (
    GatewayAuthenticationError,
    GatewayEventType,
    GatewayProtocolError,
    GatewayUnavailable,
    ManagedModelGatewayClient,
    ModelGatewayRequest,
)


class Credentials:
    def __init__(self, value: str = "session_" + "s" * 32) -> None:
        self.value = value

    def bearer_token(self) -> str:
        return self.value


def _request() -> ModelGatewayRequest:
    return ModelGatewayRequest(
        request_id="req_1",
        thread_id="thr_1",
        turn_id="trn_1",
        trace_id="trace_1",
        model_id="ecorex-chat",
        input="hello",
        config_snapshot_id="cfg_1",
        capability_snapshot_id="cap_1",
        permission_snapshot_id="perm_1",
    )


def _client(handler, *, credentials: Credentials | None = None):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return ManagedModelGatewayClient(
        "https://gateway.ecorex.test/v1/responses",
        credentials=credentials or Credentials(),
        allowed_hosts=frozenset({"gateway.ecorex.test"}),
        client=http,
    ), http


def test_gateway_stream_requires_contiguous_terminal_ndjson_and_authenticates() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        events = [
            {
                "schema_version": 1,
                "seq": 1,
                "event_type": "output_text.delta",
                "response_id": "response_1",
                "delta": "hello",
            },
            {
                "schema_version": 1,
                "seq": 2,
                "event_type": "response.completed",
                "response_id": "response_1",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content="\n".join(json.dumps(item) for item in events) + "\n",
        )

    client, http = _client(handler)

    async def run():
        result = [event async for event in client.stream(_request())]
        await http.aclose()
        return result

    events = asyncio.run(run())
    assert [event.event_type for event in events] == [
        GatewayEventType.OUTPUT_TEXT_DELTA,
        GatewayEventType.RESPONSE_COMPLETED,
    ]
    assert captured[0].headers["authorization"].startswith("Bearer session_")
    body = json.loads(captured[0].content)
    assert body["model_id"] == "ecorex-chat"
    assert "api_key" not in body


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://gateway.ecorex.test/v1/responses",
        "https://user:secret@gateway.ecorex.test/v1/responses",
        "https://other.test/v1/responses",
        "https://gateway.ecorex.test:8443/v1/responses",
        "https://gateway.ecorex.test/arbitrary",
    ],
)
def test_gateway_endpoint_is_https_credential_free_and_allowlisted(endpoint: str) -> None:
    with pytest.raises(ValueError):
        ManagedModelGatewayClient(
            endpoint,
            credentials=Credentials(),
            allowed_hosts=frozenset({"gateway.ecorex.test"}),
        )


def test_gateway_rejects_gap_and_truncated_stream() -> None:
    def gap(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=json.dumps(
                {
                    "schema_version": 1,
                    "seq": 2,
                    "event_type": "response.completed",
                    "response_id": "response_1",
                }
            ),
        )

    client, http = _client(gap)

    async def run_gap():
        try:
            return [event async for event in client.stream(_request())]
        finally:
            await http.aclose()

    with pytest.raises(GatewayProtocolError, match="sequence"):
        asyncio.run(run_gap())

    def truncated(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=json.dumps(
                {
                    "schema_version": 1,
                    "seq": 1,
                    "event_type": "output_text.delta",
                    "response_id": "response_1",
                    "delta": "partial",
                }
            ),
        )

    client, http = _client(truncated)

    async def run_truncated():
        try:
            return [event async for event in client.stream(_request())]
        finally:
            await http.aclose()

    with pytest.raises(GatewayUnavailable, match="terminal"):
        asyncio.run(run_truncated())


def test_gateway_rejects_response_identity_change_and_post_terminal_data() -> None:
    def changed(_request: httpx.Request) -> httpx.Response:
        lines = [
            {
                "seq": 1,
                "event_type": "output_text.delta",
                "response_id": "response_1",
                "delta": "partial",
            },
            {
                "seq": 2,
                "event_type": "response.completed",
                "response_id": "response_2",
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content="\n".join(json.dumps(item) for item in lines) + "\n",
        )

    client, http = _client(changed)
    with pytest.raises(GatewayProtocolError, match="identity"):
        asyncio.run(_collect_and_close(client, http))

    def post_terminal(_request: httpx.Request) -> httpx.Response:
        lines = [
            {
                "seq": 1,
                "event_type": "response.completed",
                "response_id": "response_1",
            },
            {
                "seq": 2,
                "event_type": "output_text.delta",
                "response_id": "response_1",
                "delta": "contradiction",
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content="\n".join(json.dumps(item) for item in lines) + "\n",
        )

    client, http = _client(post_terminal)

    async def request_only_first_event():
        try:
            stream = client.stream(_request())
            return await anext(stream)
        finally:
            await http.aclose()

    with pytest.raises(GatewayProtocolError, match="after a terminal"):
        asyncio.run(request_only_first_event())


def test_gateway_auth_and_transient_errors_do_not_echo_token() -> None:
    token = "secret-session-" + "x" * 32

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=token)

    client, http = _client(unauthorized, credentials=Credentials(token))

    async def run():
        try:
            return [event async for event in client.stream(_request())]
        finally:
            await http.aclose()

    with pytest.raises(GatewayAuthenticationError) as captured:
        asyncio.run(run())
    assert token not in str(captured.value)

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client, http = _client(unavailable)
    with pytest.raises(GatewayUnavailable):
        asyncio.run(_collect_and_close(client, http))


def test_gateway_rejects_oversized_request_before_transport() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = ManagedModelGatewayClient(
        "https://gateway.ecorex.test/v1/responses",
        credentials=Credentials(),
        allowed_hosts=frozenset({"gateway.ecorex.test"}),
        client=http,
        max_request_bytes=1024,
    )
    oversized = _request().model_copy(update={"input": "文" * 1024})
    with pytest.raises(GatewayProtocolError, match="request exceeded"):
        asyncio.run(_collect_and_close(client, http, oversized))
    assert calls == 0


def test_gateway_bounds_unterminated_event_before_line_buffer_growth() -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                yield b"x" * 300

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=Chunks(),
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = ManagedModelGatewayClient(
        "https://gateway.ecorex.test/v1/responses",
        credentials=Credentials(),
        allowed_hosts=frozenset({"gateway.ecorex.test"}),
        client=http,
        max_event_bytes=1024,
    )
    with pytest.raises(GatewayProtocolError, match="size limit"):
        asyncio.run(_collect_and_close(client, http))


@pytest.mark.parametrize(
    ("headers", "content", "message"),
    [
        (
            {
                "content-type": "application/x-ndjson",
                "content-encoding": "gzip",
            },
            b"not-really-gzip",
            "encoding",
        ),
        (
            {
                "content-type": "application/x-ndjson",
                "content-length": str(17 * 1024 * 1024),
            },
            b"",
            "size limit",
        ),
        (
            {"content-type": "application/x-ndjson"},
            b"\xff\n",
            "invalid event",
        ),
    ],
)
def test_gateway_rejects_encoded_declared_large_and_invalid_utf8_streams(
    headers: dict[str, str], content: bytes, message: str
) -> None:
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if content:
                yield content

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=Body())

    client, http = _client(handler)
    with pytest.raises(GatewayProtocolError, match=message):
        asyncio.run(_collect_and_close(client, http))


def test_gateway_rejects_unsafe_session_token_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client, http = _client(
        handler,
        credentials=Credentials("x" * 24 + "\n" + "y" * 8),
    )
    with pytest.raises(GatewayAuthenticationError, match="token is invalid"):
        asyncio.run(_collect_and_close(client, http))
    assert calls == 0


def test_gateway_redacts_credential_provider_exception() -> None:
    secret = "LOCAL-CREDENTIAL-SECRET"

    class BrokenCredentials:
        def bearer_token(self):
            raise RuntimeError(secret)

    client, http = _client(lambda _request: httpx.Response(500))
    client.credentials = BrokenCredentials()
    with pytest.raises(GatewayAuthenticationError) as captured:
        asyncio.run(_collect_and_close(client, http))
    assert secret not in str(captured.value)


async def _collect_and_close(client, http, request=None):
    try:
        return [event async for event in client.stream(request or _request())]
    finally:
        await http.aclose()
