from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ecorex.release import (
    HTTPSReleaseReplicaPublisher,
    ReleaseReplicaError,
)


class Credential:
    def bearer_token(self) -> str:
        return "replica-admin-token-secret"


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
