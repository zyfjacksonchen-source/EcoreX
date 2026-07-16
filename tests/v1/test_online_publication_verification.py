from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
)
from ecorex.release.online_verification import (
    OnlinePublicationVerificationError,
    OnlinePublicationVerifier,
    OnlineVerificationLimits,
)
from ecorex.release.public_index import _validate_publication_receipt
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
)


CHECKPOINT_KEY = hashlib.sha256(b"online-checkpoint-test-key").digest()


def _release(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.txt").write_bytes(b"signed EcoreX runtime\n")
    signer = Ed25519MemorySigner("release-key-2026", Ed25519PrivateKey.generate())
    built = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-07-16T18:00:00+08:00",
            sources=(
                ReleaseSource(
                    "github-cn",
                    SourceKind.GITHUB_CN_MIRROR,
                    0,
                    (
                        "https://ghproxy.net/https://github.com/acme/ecorex/"
                        "releases/download"
                    ),
                ),
                ReleaseSource(
                    "github",
                    SourceKind.GITHUB_RELEASE,
                    1,
                    "https://github.com/acme/ecorex/releases/download",
                ),
                ReleaseSource(
                    "cdn",
                    SourceKind.ECOREX_CDN,
                    2,
                    "https://cdn.ecorex.test/releases",
                ),
            ),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.CORE,
                    platform="windows",
                    architecture="x64",
                ),
            ),
            release_scoped_sources=True,
        ),
        tmp_path / "release",
    )
    verifier = Ed25519SignatureVerifier({"release-key-2026": signer.public_key_bytes})
    files = {
        path.name: path.read_bytes()
        for path in built.output_dir.iterdir()
        if path.is_file()
    }
    return built, verifier, files


class _Origin:
    def __init__(
        self,
        built,
        files: dict[str, bytes],
        *,
        drift_host: str | None = None,
        redirect_proxy_to: str | None = None,
        fail_host: str | None = None,
        draft: bool = False,
    ) -> None:
        self.built = built
        self.files = files
        self.drift_host = drift_host
        self.redirect_proxy_to = redirect_proxy_to
        self.fail_host = fail_host
        self.draft = draft
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        assert request.method == "GET"
        if request.url.host == "api.github.com":
            github = self.built.manifest.sources[1]
            assets = [
                {
                    "name": name,
                    "size": len(payload),
                    "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    "browser_download_url": (
                        f"{github.base_url}/{quote(name, safe='')}"
                    ),
                }
                for name, payload in sorted(self.files.items())
            ]
            return httpx.Response(
                200,
                json={
                    "id": 4242,
                    "draft": self.draft,
                    "tag_name": github.base_url.rsplit("/", 1)[1],
                    "assets": assets,
                },
            )
        host = request.url.host
        if host == self.fail_host:
            return httpx.Response(503, content=b"retry")
        if host == "ghproxy.net" and self.redirect_proxy_to:
            return httpx.Response(302, headers={"location": self.redirect_proxy_to})
        name = unquoted_name(request.url.path)
        payload = self.files.get(name)
        if payload is None:
            return httpx.Response(404)
        if host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://release-assets.githubusercontent.com/{name}"
                },
            )
        if host == self.drift_host:
            payload = payload[:-1] + bytes([payload[-1] ^ 1])
        return httpx.Response(
            200,
            stream=httpx.ByteStream(payload),
            headers={"content-length": str(len(payload))},
        )


def unquoted_name(path: str) -> str:
    from urllib.parse import unquote

    return unquote(path.rsplit("/", 1)[1])


def _online(verifier, origin: _Origin, *, attempts: int = 1):
    return OnlinePublicationVerifier(
        verifier=verifier,
        client=httpx.Client(
            transport=httpx.MockTransport(origin), follow_redirects=False
        ),
        limits=OnlineVerificationLimits(
            attempts=attempts,
            maximum_total_bytes=64 * 1024 * 1024,
            total_timeout_seconds=60,
        ),
        checkpoint_key=CHECKPOINT_KEY,
        sleep=lambda _seconds: None,
    )


def test_read_only_github_proxy_and_all_three_get_bytes_make_canonical_receipt(
    tmp_path: Path,
) -> None:
    built, verifier, files = _release(tmp_path)
    origin = _Origin(built, files)
    output = tmp_path / "publication-receipt.json"
    checkpoint = tmp_path / "online-checkpoint.json"
    receipt = _online(verifier, origin).verify(
        release_dir=built.output_dir,
        output=output,
        checkpoint=checkpoint,
        temporary_directory=tmp_path,
    )

    assert receipt["github_release_id"] == 4242
    assert receipt["github_draft"] is False
    assert list(receipt["source_receipts"]) == ["github-cn", "github", "cdn"]
    assert all(
        entry["url"].startswith(
            "https://ghproxy.net/https://github.com/acme/ecorex/releases/download/"
        )
        for entry in receipt["source_receipts"]["github-cn"]
    )
    assert (
        output.read_bytes()
        == json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    assert not checkpoint.exists()
    assert not list(tmp_path.glob(".ecorex-online-*.part"))
    assert all(method == "GET" for method, _url in origin.requests)
    manifest_bytes = built.manifest_path.read_bytes()
    _validate_publication_receipt(
        manifest=built.manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        receipt=receipt,
        receipt_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
    )


def test_remote_digest_drift_fails_and_removes_partial_file(tmp_path: Path) -> None:
    built, verifier, files = _release(tmp_path)
    origin = _Origin(built, files, drift_host="ghproxy.net")
    with pytest.raises(
        OnlinePublicationVerificationError, match="online_download_digest_drift"
    ):
        _online(verifier, origin).verify(
            release_dir=built.output_dir,
            output=tmp_path / "receipt.json",
            checkpoint=tmp_path / "checkpoint.json",
            temporary_directory=tmp_path,
        )
    assert not (tmp_path / "receipt.json").exists()
    assert not list(tmp_path.glob(".ecorex-online-*.part"))


def test_unexpected_cross_host_redirect_is_rejected_before_follow(
    tmp_path: Path,
) -> None:
    built, verifier, files = _release(tmp_path)
    origin = _Origin(
        built,
        files,
        redirect_proxy_to="https://evil.example/stolen.zip?credential=value",
    )
    with pytest.raises(
        OnlinePublicationVerificationError, match="online_redirect_host_forbidden"
    ):
        _online(verifier, origin).verify(
            release_dir=built.output_dir,
            output=tmp_path / "receipt.json",
            checkpoint=tmp_path / "checkpoint.json",
        )
    assert not any("evil.example" in url for _method, url in origin.requests)


def test_authenticated_checkpoint_resumes_after_partial_origin_failure(
    tmp_path: Path,
) -> None:
    built, verifier, files = _release(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    failed = _Origin(built, files, fail_host="github.com")
    with pytest.raises(
        OnlinePublicationVerificationError, match="online_download_retry_exhausted"
    ):
        _online(verifier, failed).verify(
            release_dir=built.output_dir,
            output=tmp_path / "receipt.json",
            checkpoint=checkpoint,
        )
    assert checkpoint.exists()
    value = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert value["checkpoint_mac"]
    assert {item["source_id"] for item in value["completed"]} == {"github-cn"}

    resumed = _Origin(built, files)
    _online(verifier, resumed).verify(
        release_dir=built.output_dir,
        output=tmp_path / "receipt.json",
        checkpoint=checkpoint,
    )
    assert not any("ghproxy.net" in url for _method, url in resumed.requests)
    assert not checkpoint.exists()


def test_existing_receipt_and_checkpoint_tamper_fail_before_false_success(
    tmp_path: Path,
) -> None:
    built, verifier, files = _release(tmp_path)
    output = tmp_path / "receipt.json"
    output.write_bytes(b"owned")
    origin = _Origin(built, files)
    with pytest.raises(
        OnlinePublicationVerificationError, match="online_receipt_exists"
    ):
        _online(verifier, origin).verify(
            release_dir=built.output_dir,
            output=output,
            checkpoint=tmp_path / "checkpoint.json",
        )
    assert origin.requests == []
    assert output.read_bytes() == b"owned"

    output.unlink()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "document_type": "ecorex.online-publication-verification-checkpoint",
                "release_id": built.manifest.release_id,
                "manifest_sha256": hashlib.sha256(
                    built.manifest_path.read_bytes()
                ).hexdigest(),
                "github_release_id": 4242,
                "github_draft": False,
                "completed": [],
                "checkpoint_mac": "0" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        OnlinePublicationVerificationError,
        match="online_checkpoint_authentication_failed",
    ):
        _online(verifier, _Origin(built, files)).verify(
            release_dir=built.output_dir,
            output=output,
            checkpoint=checkpoint,
        )


def test_github_release_must_be_public_not_draft(tmp_path: Path) -> None:
    built, verifier, files = _release(tmp_path)
    with pytest.raises(
        OnlinePublicationVerificationError, match="github_release_identity_invalid"
    ):
        _online(verifier, _Origin(built, files, draft=True)).verify(
            release_dir=built.output_dir,
            output=tmp_path / "receipt.json",
            checkpoint=tmp_path / "checkpoint.json",
        )
