from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import ssl

import httpx
import pytest

from ecorex.update import (
    FetchError,
    HTTPArtifactFetcher,
    HTTPSReleaseFeedClient,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    UpdateAvailableSignal,
    UpdateProtocolError,
    WebSocketUpdateSignalSource,
)


class Credentials:
    def bearer_token(self) -> str:
        return "control-plane-token-1234567890"


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"test-signature").decode(),
    )


def _manifest(payload: bytes) -> ReleaseManifest:
    return ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.1-stable",
        version="1.0.1",
        build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource(
                "mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"
            ),
            ReleaseSource(
                "github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"
            ),
            ReleaseSource(
                "cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"
            ),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def test_http_artifact_fetcher_requires_exact_range_and_resumes(tmp_path) -> None:
    payload = b"signed-core-payload"
    manifest = _manifest(payload)
    artifact = manifest.artifacts[0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.headers.get("range", "bytes=0-")[6:-1])
        body = payload[start:]
        headers = {"Content-Length": str(len(body))}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(payload) - 1}/{len(payload)}"
        return httpx.Response(206 if start else 200, content=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HTTPArtifactFetcher(
        allowed_hosts=frozenset({"mirror.example"}), client=client, chunk_size=4096
    )
    destination = tmp_path / "core.part"
    destination.write_bytes(payload[:6])

    fetcher.fetch(
        manifest.sources[0],
        artifact,
        destination,
        resume_from=6,
        max_bytes=len(payload),
    )

    assert destination.read_bytes() == payload
    assert requests[0].headers["range"] == "bytes=6-"
    assert requests[0].headers["accept-encoding"] == "identity"


def test_http_artifact_fetcher_rejects_redirect_wrong_host_and_bad_range(tmp_path) -> None:
    payload = b"signed-core-payload"
    manifest = _manifest(payload)
    artifact = manifest.artifacts[0]
    redirect_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"Location": "https://evil.test/core"})
        )
    )
    fetcher = HTTPArtifactFetcher(
        allowed_hosts=frozenset({"mirror.example"}), client=redirect_client
    )
    with pytest.raises(FetchError):
        fetcher.fetch(
            manifest.sources[0], artifact, tmp_path / "redirect.part", resume_from=0,
            max_bytes=len(payload),
        )
    with pytest.raises(FetchError):
        fetcher.fetch(
            manifest.sources[1], artifact, tmp_path / "host.part", resume_from=0,
            max_bytes=len(payload),
        )

    bad_range_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                206,
                content=payload[3:],
                headers={"Content-Range": f"bytes 2-{len(payload)-1}/{len(payload)}"},
            )
        )
    )
    bad_range = HTTPArtifactFetcher(
        allowed_hosts=frozenset({"mirror.example"}), client=bad_range_client
    )
    partial = tmp_path / "range.part"
    partial.write_bytes(payload[:3])
    with pytest.raises(FetchError):
        bad_range.fetch(
            manifest.sources[0], artifact, partial, resume_from=3, max_bytes=len(payload)
        )
    assert partial.read_bytes() == payload[:3]


def test_http_artifact_fetcher_rejects_hardlink_destination(tmp_path) -> None:
    payload = b"signed-core-payload"
    manifest = _manifest(payload)
    artifact = manifest.artifacts[0]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=payload,
                headers={"Content-Length": str(len(payload))},
            )
        )
    )
    fetcher = HTTPArtifactFetcher(
        allowed_hosts=frozenset({"mirror.example"}), client=client
    )
    original = tmp_path / "unrelated.txt"
    original.write_bytes(b"")
    hardlink = tmp_path / "download.part"
    os.link(original, hardlink)
    with pytest.raises(FetchError, match="unsafe"):
        fetcher.fetch(
            manifest.sources[0], artifact, hardlink, resume_from=0, max_bytes=len(payload)
        )
    assert original.read_bytes() == b""


def test_http_artifact_fetcher_rejects_linked_parent_destination(tmp_path) -> None:
    payload = b"signed-core-payload"
    manifest = _manifest(payload)
    artifact = manifest.artifacts[0]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=payload,
                headers={"Content-Length": str(len(payload))},
            )
        )
    )
    fetcher = HTTPArtifactFetcher(
        allowed_hosts=frozenset({"mirror.example"}), client=client
    )

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation unavailable: {error}")
    with pytest.raises(FetchError, match="parent is unsafe"):
        fetcher.fetch(
            manifest.sources[0],
            artifact,
            linked_parent / "core.part",
            resume_from=0,
            max_bytes=len(payload),
        )
    assert not (outside / "core.part").exists()


def test_release_feed_authenticates_bounds_and_verifies_manifest() -> None:
    manifest = _manifest(b"signed-core-payload")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text=manifest.to_json(),
            headers={"Content-Type": "application/vnd.ecorex.release+json"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = HTTPSReleaseFeedClient(
        "https://control.example/api/v1/releases/latest",
        credentials=Credentials(),
        verifier=AcceptingVerifier(),
        allowed_hosts=frozenset({"control.example"}),
        client=client,
    )

    result = feed.latest(
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        current_version="1.0.0",
        update_state="awaiting_user",
    )

    assert result == manifest
    assert seen[0].headers["authorization"].startswith("Bearer control-plane-token")
    assert seen[0].url.params["current_version"] == "1.0.0"
    assert seen[0].url.params["channel"] == "stable"
    assert seen[0].url.params["update_state"] == "awaiting_user"


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        {
            "Content-Type": "application/json",
            "Content-Length": str(1024 * 1024 + 1),
        },
    ],
)
def test_release_feed_rejects_encoded_or_declared_oversized_payloads(headers) -> None:
    manifest = _manifest(b"signed-core-payload")
    body = manifest.to_json().encode()
    if headers.get("Content-Encoding") == "gzip":
        body = gzip.compress(body)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body, headers=headers)
        )
    )
    feed = HTTPSReleaseFeedClient(
        "https://control.example/api/v1/releases/latest",
        credentials=Credentials(),
        verifier=AcceptingVerifier(),
        allowed_hosts=frozenset({"control.example"}),
        client=client,
    )
    with pytest.raises(UpdateProtocolError):
        feed.latest(
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            update_state="idle",
        )


def test_release_feed_rejects_unknown_client_update_state_before_network() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("invalid state must fail before I/O")
        )
    )
    feed = HTTPSReleaseFeedClient(
        "https://control.example/api/v1/releases/latest",
        credentials=Credentials(),
        verifier=AcceptingVerifier(),
        allowed_hosts=frozenset({"control.example"}),
        client=client,
    )

    with pytest.raises(ValueError, match="state"):
        feed.latest(
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            update_state="installing",
        )


def test_update_signal_has_an_exact_bounded_contract() -> None:
    digest = hashlib.sha256(b"build").hexdigest()
    raw = {
        "schema_version": 1,
        "event_id": "event-1",
        "event_type": "update.available",
        "release_id": "release-1.0.1-stable",
        "version": "1.0.1",
        "build_digest": digest,
        "channel": "stable",
    }
    signal = UpdateAvailableSignal.from_json(json.dumps(raw))
    assert signal.build_digest == digest
    with pytest.raises(UpdateProtocolError):
        UpdateAvailableSignal.from_json(json.dumps({**raw, "event_type": "update.activate"}))
    with pytest.raises(UpdateProtocolError):
        UpdateAvailableSignal.from_json(json.dumps({**raw, "event_id": "x" * 129}))
    with pytest.raises(UpdateProtocolError):
        UpdateAvailableSignal.from_json(json.dumps({**raw, "version": "01.0.0"}))


def test_wss_source_builds_bounded_target_query_and_rejects_insecure_tls() -> None:
    source = WebSocketUpdateSignalSource(
        "wss://control.example/api/v1/client/updates/ws",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.example"}),
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        current_version="1.0.0",
    )
    assert source.url == (
        "wss://control.example/api/v1/client/updates/ws?"
        "channel=stable&platform=windows&architecture=x64&current_version=1.0.0"
    )
    insecure = ssl._create_unverified_context()
    with pytest.raises(ValueError, match="verify"):
        WebSocketUpdateSignalSource(
            "wss://control.example/api/v1/client/updates/ws",
            credentials=Credentials(),
            allowed_hosts=frozenset({"control.example"}),
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            ssl_context=insecure,
        )
