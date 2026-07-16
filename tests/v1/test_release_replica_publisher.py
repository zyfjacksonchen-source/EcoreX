from __future__ import annotations

import hashlib
import gzip
import json
from pathlib import Path

import httpx
import pytest

from ecorex.release import (
    HTTPSReadThroughReleaseMirror,
    HTTPSReleaseReplicaPublisher,
    ReleaseReplicaError,
)


class Credential:
    def bearer_token(self) -> str:
        return "replica-admin-token-secret"


def test_github_read_through_mirror_streams_and_verifies_exact_bytes(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core-windows-x64.zip"
    package.write_bytes(b"signed package bytes")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["accept-encoding"] == "identity"
        assert request.url == httpx.URL(
            f"https://ghproxy.example/releases/{release_id}/{package.name}"
        )
        return httpx.Response(
            200,
            content=package.read_bytes(),
            headers={
                "content-encoding": "identity",
                "content-length": str(package.stat().st_size),
            },
        )

    mirror = HTTPSReadThroughReleaseMirror(
        source_id="github-cn",
        public_hosts=frozenset({"ghproxy.example"}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    receipt = mirror.verify_asset(
        base_url=f"https://ghproxy.example/releases/{release_id}",
        release_id=release_id,
        path=package,
        expected_sha256=digest,
    )

    assert receipt.sha256 == digest
    assert receipt.size_bytes == package.stat().st_size
    assert receipt.url.endswith(f"/{package.name}")


def test_github_read_through_mirror_rejects_redirects_and_wrong_bytes(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"signed")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    for response in (
        httpx.Response(302, headers={"location": "https://evil.example/core.zip"}),
        httpx.Response(200, content=b"forged"),
    ):
        mirror = HTTPSReadThroughReleaseMirror(
            source_id="github-cn",
            public_hosts=frozenset({"ghproxy.example"}),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _request, item=response: item)
            ),
        )
        with pytest.raises(ReleaseReplicaError):
            mirror.verify_asset(
                base_url=f"https://ghproxy.example/releases/{release_id}",
                release_id=release_id,
                path=package,
                expected_sha256=digest,
            )


@pytest.mark.parametrize(
    "response,code,retryable",
    [
        (
            httpx.Response(
                200,
                content=gzip.compress(b"signed"),
                headers={"content-encoding": "gzip"},
            ),
            "mirror_compressed_response",
            False,
        ),
        (
            httpx.Response(
                200,
                content=b"signed",
                headers={"content-length": "999"},
            ),
            "mirror_size_mismatch",
            False,
        ),
        (httpx.Response(503), "mirror_unavailable", True),
    ],
)
def test_github_read_through_mirror_rejects_invalid_transport_contract(
    tmp_path: Path,
    response: httpx.Response,
    code: str,
    retryable: bool,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"signed")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    mirror = HTTPSReadThroughReleaseMirror(
        source_id="github-cn",
        public_hosts=frozenset({"ghproxy.example"}),
        attempts=1,
        backoff_seconds=(),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: response)
        ),
    )

    with pytest.raises(ReleaseReplicaError, match=code) as failure:
        mirror.verify_asset(
            base_url=f"https://ghproxy.example/releases/{release_id}",
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )

    assert failure.value.retryable is retryable


def test_github_read_through_mirror_retries_only_transient_propagation(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"signed")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    responses = [
        httpx.Response(404, headers={"retry-after": "2"}),
        httpx.Response(503),
        httpx.Response(200, content=package.read_bytes()),
    ]
    calls = 0
    now = [0.0]
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    mirror = HTTPSReadThroughReleaseMirror(
        source_id="github-cn",
        public_hosts=frozenset({"ghproxy.example"}),
        attempts=3,
        backoff_seconds=(1, 2),
        deadline_seconds=30,
        sleeper=sleep,
        clock=lambda: now[0],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = mirror.verify_asset(
        base_url=f"https://ghproxy.example/releases/{release_id}",
        release_id=release_id,
        path=package,
        expected_sha256=digest,
    )

    assert receipt.sha256 == digest
    assert calls == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://evil.example/core.zip"}),
        httpx.Response(401),
        httpx.Response(200, content=b"forged"),
    ],
)
def test_github_read_through_mirror_never_retries_deterministic_failure(
    tmp_path: Path,
    response: httpx.Response,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"signed")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    mirror = HTTPSReadThroughReleaseMirror(
        source_id="github-cn",
        public_hosts=frozenset({"ghproxy.example"}),
        sleeper=sleeps.append,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ReleaseReplicaError):
        mirror.verify_asset(
            base_url=f"https://ghproxy.example/releases/{release_id}",
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )

    assert calls == 1
    assert sleeps == []


def test_github_read_through_mirror_rejects_non_allowlisted_signed_host(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"signed")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=package.read_bytes())

    mirror = HTTPSReadThroughReleaseMirror(
        source_id="github-cn",
        public_hosts=frozenset({"ghproxy.example"}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="signed source URL"):
        mirror.verify_asset(
            base_url=f"https://evil.example/releases/{release_id}",
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )
    assert calls == 0


def test_domestic_mirror_upload_and_finalize_are_digest_idempotent(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core-windows-x64.zip"
    package.write_bytes(b"signed package bytes")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest_digest = hashlib.sha256(b"manifest").hexdigest()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer replica-admin-token-secret"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "PUT":
            assert request.content == package.read_bytes()
            assert request.headers["x-ecorex-sha256"] == digest
            value = {
                "release_id": release_id,
                "source_id": "github-cn",
                "name": package.name,
                "size_bytes": package.stat().st_size,
                "sha256": digest,
                "url": (
                    f"https://download.example/releases/{release_id}/{package.name}"
                ),
                "state": "ready",
            }
            status = 201
        else:
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "manifest_sha256": manifest_digest
            }
            value = {
                "release_id": release_id,
                "source_id": "github-cn",
                "state": "ready",
                "manifest_sha256": manifest_digest,
            }
            status = 200
        return httpx.Response(
            status, json=value, headers={"content-type": "application/json"}
        )

    publisher = HTTPSReleaseReplicaPublisher(
        source_id="github-cn",
        endpoint="https://mirror-control.example/api/v1/releases",
        allowed_hosts=frozenset({"mirror-control.example"}),
        public_hosts=frozenset({"download.example"}),
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    receipt = publisher.ensure_asset(
        release_id=release_id,
        path=package,
        expected_sha256=digest,
    )
    assert receipt.sha256 == digest
    assert receipt.source_id == "github-cn"
    assert publisher.finalize(
        release_id=release_id,
        manifest_sha256=manifest_digest,
    )
    assert [method for method, _url in calls] == ["PUT", "POST"]
    assert "replica-admin-token-secret" not in repr(calls)


@pytest.mark.parametrize(
    "endpoint,allowed",
    [
        ("http://mirror.example/api/v1/releases", {"mirror.example"}),
        ("https://evil.example/api/v1/releases", {"mirror.example"}),
        ("https://mirror.example:8443/api/v1/releases", {"mirror.example"}),
        ("https://mirror.example/api/free-form", {"mirror.example"}),
        ("https://mirror.example/api/v1/releases", {"-mirror.example"}),
    ],
)
def test_replica_control_root_is_fixed_https(
    endpoint: str,
    allowed: set[str],
) -> None:
    with pytest.raises(ValueError):
        HTTPSReleaseReplicaPublisher(
            source_id="github-cn",
            endpoint=endpoint,
            allowed_hosts=frozenset(allowed),
            public_hosts=frozenset({"download.example"}),
            credentials=Credential(),
        )


def test_replica_rejects_wrong_public_url_and_retryable_remote_error(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "core.zip"
    package.write_bytes(b"package")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    def wrong_url(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "release_id": release_id,
                "source_id": "github-cn",
                "name": package.name,
                "size_bytes": package.stat().st_size,
                "sha256": digest,
                "url": f"https://evil.example/{release_id}/{package.name}",
                "state": "ready",
            },
            headers={"content-type": "application/json"},
        )

    publisher = HTTPSReleaseReplicaPublisher(
        source_id="github-cn",
        endpoint="https://mirror-control.example/api/v1/releases",
        allowed_hosts=frozenset({"mirror-control.example"}),
        public_hosts=frozenset({"download.example"}),
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(wrong_url)),
    )
    with pytest.raises(ReleaseReplicaError, match="public_url_invalid"):
        publisher.ensure_asset(
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )

    unavailable = HTTPSReleaseReplicaPublisher(
        source_id="github-cn",
        endpoint="https://mirror-control.example/api/v1/releases",
        allowed_hosts=frozenset({"mirror-control.example"}),
        public_hosts=frozenset({"download.example"}),
        credentials=Credential(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    503,
                    json={"secret": "remote-secret"},
                    headers={"content-type": "application/json"},
                )
            )
        ),
    )
    with pytest.raises(ReleaseReplicaError) as failure:
        unavailable.ensure_asset(
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )
    assert failure.value.retryable is True
    assert "remote-secret" not in str(failure.value)


def test_replica_receipt_rejects_boolean_size_even_for_one_byte(
    tmp_path: Path,
) -> None:
    release_id = "release-stable-0123456789abcdef01234567"
    package = tmp_path / "one.zip"
    package.write_bytes(b"x")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "release_id": release_id,
                "source_id": "github-cn",
                "name": package.name,
                "size_bytes": True,
                "sha256": digest,
                "url": f"https://download.example/{release_id}/{package.name}",
                "state": "ready",
            },
            headers={"content-type": "application/json"},
        )

    publisher = HTTPSReleaseReplicaPublisher(
        source_id="github-cn",
        endpoint="https://mirror-control.example/api/v1/releases",
        allowed_hosts=frozenset({"mirror-control.example"}),
        public_hosts=frozenset({"download.example"}),
        credentials=Credential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ReleaseReplicaError, match="receipt_invalid"):
        publisher.ensure_asset(
            release_id=release_id,
            path=package,
            expected_sha256=digest,
        )
