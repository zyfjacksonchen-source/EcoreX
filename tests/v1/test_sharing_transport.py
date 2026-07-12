from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import gzip
import json

import httpx
import pytest

from ecorex.sharing import (
    HTTPSSharePublisher,
    SharePayload,
    ShareTransportError,
)


class Credentials:
    def bearer_token(self) -> str:
        return "control-plane-session-" + "x" * 32


class InvalidCredentials:
    def bearer_token(self) -> str:
        return "control-plane-session-" + "x" * 24 + "\x7f"


def payload() -> SharePayload:
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    return SharePayload(
        schema_version=2,
        share_id="shr_" + "a" * 32,
        thread_id="thread-1",
        source_watermark=1,
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


def test_https_share_publisher_authenticates_and_never_follows_redirects() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "remote_snapshot_id": "remote-1",
                "public_url": "https://share.ecorex.test/s/unique-token",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=http,
    )
    result = asyncio.run(publisher.publish(payload(), idempotency_key="share-1"))
    asyncio.run(http.aclose())
    assert result.remote_snapshot_id == "remote-1"
    assert seen[0].headers["authorization"].startswith("Bearer control-plane-session-")
    assert seen[0].headers["idempotency-key"] == "share-1"
    assert json.loads(seen[0].content)["share_id"].startswith("shr_")

    redirect_calls = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirect_calls.append(request.url.host)
        if request.url.host == "evil.test":
            raise AssertionError("authorization redirect must never be followed")
        return httpx.Response(307, headers={"Location": "https://evil.test"})

    redirect_http = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(
            redirect_handler
        )
    )
    redirected = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=redirect_http,
    )
    with pytest.raises(ShareTransportError, match="redirect"):
        asyncio.run(redirected.publish(payload(), idempotency_key="share-2"))
    assert redirect_calls == ["control.ecorex.test"]
    asyncio.run(redirect_http.aclose())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://control.ecorex.test/api/v1/shares",
        "https://user:secret@control.ecorex.test/api/v1/shares",
        "https://other.test/api/v1/shares",
        "https://control.ecorex.test:8443/api/v1/shares",
    ],
)
def test_share_endpoint_is_https_credential_free_and_allowlisted(endpoint: str) -> None:
    with pytest.raises(ValueError):
        HTTPSSharePublisher(
            endpoint,
            credentials=Credentials(),
            allowed_hosts=frozenset({"control.ecorex.test"}),
        )


def test_share_publisher_rejects_encoded_oversized_or_wrong_type_response() -> None:
    cases = [
        httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=gzip.compress(b"{}"),
        ),
        httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Length": "999999"},
            content=b"{}",
        ),
        httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"{}"),
    ]
    for response in cases:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request, value=response: value)
        )
        publisher = HTTPSSharePublisher(
            "https://control.ecorex.test/api/v1/shares",
            credentials=Credentials(),
            allowed_hosts=frozenset({"control.ecorex.test"}),
            client=http,
        )
        with pytest.raises(ShareTransportError):
            asyncio.run(publisher.publish(payload(), idempotency_key="share"))
        asyncio.run(http.aclose())


def test_share_publisher_rejects_ambiguous_length_and_header_unsafe_credentials() -> None:
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json", "Content-Length": "-1"},
        content=b"{}",
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    )
    publisher = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=http,
    )
    with pytest.raises(ShareTransportError, match="Content-Length"):
        asyncio.run(publisher.publish(payload(), idempotency_key="share"))
    asyncio.run(http.aclose())

    invalid = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=InvalidCredentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response)),
    )
    with pytest.raises(ShareTransportError, match="session"):
        asyncio.run(invalid.publish(payload(), idempotency_key="share"))
    asyncio.run(invalid.client.aclose())


def test_share_transport_drops_secret_bearing_httpx_exception_context() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream-secret", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    publisher = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=http,
    )
    with pytest.raises(ShareTransportError, match="transport failed") as failure:
        asyncio.run(publisher.publish(payload(), idempotency_key="share"))
    assert failure.value.__cause__ is None
    assert "upstream-secret" not in str(failure.value)
    asyncio.run(http.aclose())
