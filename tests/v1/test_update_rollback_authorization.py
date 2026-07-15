from __future__ import annotations

import base64
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.release.signing import Ed25519MemorySigner, sign_envelope
from ecorex.update import (
    Ed25519SignatureVerifier,
    InstallCoordinator,
    LocalSourceFetcher,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    RollbackAuthorizationError,
    RollbackAuthorizationVerifier,
    RuntimeUpdateService,
    SignatureEnvelope,
    SingleUseRollbackAuthorizer,
    SourceKind,
    issue_rollback_authorization,
)


def _placeholder() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="placeholder",
        value=base64.b64encode(b"\0" * 64).decode("ascii"),
    )


def _manifest(
    version: str,
    *,
    signer: Ed25519MemorySigner,
    payload: bytes | None = None,
) -> ReleaseManifest:
    payload = payload or f"core-{version}".encode()
    release_id = f"release-{version}-stable"
    build_digest = hashlib.sha256(f"build-{version}".encode()).hexdigest()
    artifact = ReleaseArtifact(
        artifact_id="core-windows-x64",
        platform="windows",
        architecture="x64",
        file_name="ecorex-core.zip",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=_placeholder(),
    )
    artifact = replace(
        artifact,
        signature=sign_envelope(
            signer,
            artifact.signed_payload(
                release_id=release_id,
                version=version,
                build_digest=build_digest,
            ),
        ),
    )
    unsigned = ReleaseManifest(
        schema_version=1,
        release_id=release_id,
        version=version,
        build_digest=build_digest,
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-12T08:00:00+00:00",
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
        artifacts=(artifact,),
        signature=_placeholder(),
    )
    return replace(unsigned, signature=sign_envelope(signer, unsigned.canonical_payload()))


def _package(version: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("runtime/version.txt", version)
        archive.writestr(
            "web/assets/app.0123456789abcdef.js", "console.log('ready')"
        )
    return stream.getvalue()


def _current(manifest: ReleaseManifest) -> dict[str, str]:
    artifact = manifest.artifact("core-windows-x64")
    return {
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.sha256,
        "channel": manifest.channel.value,
        "platform": artifact.platform,
        "architecture": artifact.architecture,
    }


def test_signed_rollback_authorization_is_exact_time_limited_and_nonce_bound() -> None:
    release_signer = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    rollback_signer = Ed25519MemorySigner(
        "rollback-key", Ed25519PrivateKey.generate()
    )
    source = _manifest("1.0.2", signer=release_signer)
    target = _manifest("1.0.1", signer=release_signer)
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    verifier = RollbackAuthorizationVerifier(
        Ed25519SignatureVerifier(
            {"rollback-key": rollback_signer.public_key_bytes}
        ),
        clock=lambda: now,
    )
    nonce = "n" * 43
    token = issue_rollback_authorization(
        signer=rollback_signer,
        rollback_id="rollback_123",
        client_id="client-1",
        source_manifest=source,
        target_manifest=target,
        platform="windows",
        architecture="x64",
        request_nonce=nonce,
        ttl_seconds=300,
        now=now,
    )

    claims = verifier.verify(
        token,
        current=_current(source),
        target=target,
        platform="windows",
        architecture="x64",
        expected_nonce=nonce,
        expected_client_id="client-1",
    )
    assert claims.source_release_id == source.release_id
    assert claims.target_release_id == target.release_id
    with pytest.raises(RollbackAuthorizationError, match="nonce"):
        verifier.verify(
            token,
            current=_current(source),
            target=target,
            platform="windows",
            architecture="x64",
            expected_nonce="x" * 43,
        )
    expired = RollbackAuthorizationVerifier(
        Ed25519SignatureVerifier(
            {"rollback-key": rollback_signer.public_key_bytes}
        ),
        clock=lambda: now + timedelta(minutes=10),
        clock_skew_seconds=0,
    )
    with pytest.raises(RollbackAuthorizationError, match="expired"):
        expired.verify(
            token,
            current=_current(source),
            target=target,
            platform="windows",
            architecture="x64",
        )


def test_feed_accepted_rollback_grant_is_single_use_at_install_boundary() -> None:
    release_signer = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    rollback_signer = Ed25519MemorySigner(
        "rollback-key", Ed25519PrivateKey.generate()
    )
    source = _manifest("1.0.2", signer=release_signer)
    target = _manifest("1.0.1", signer=release_signer)
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    authorizer = SingleUseRollbackAuthorizer(
        RollbackAuthorizationVerifier(
            Ed25519SignatureVerifier(
                {"rollback-key": rollback_signer.public_key_bytes}
            ),
            clock=lambda: now,
        ),
        platform="windows",
        architecture="x64",
    )
    nonce = "r" * 43
    token = issue_rollback_authorization(
        signer=rollback_signer,
        rollback_id="rollback_456",
        client_id="client-2",
        source_manifest=source,
        target_manifest=target,
        platform="windows",
        architecture="x64",
        request_nonce=nonce,
        now=now,
    )

    authorizer.accept(
        token,
        current=_current(source),
        target=target,
        expected_nonce=nonce,
    )
    assert authorizer.authorize(_current(source), target, token) is True
    assert authorizer.authorize(_current(source), target, token) is False


def test_runtime_prepares_and_activates_rollback_through_existing_safe_chain(
    tmp_path,
) -> None:
    release_signer = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    rollback_signer = Ed25519MemorySigner(
        "rollback-key", Ed25519PrivateKey.generate()
    )
    source_payload = _package("1.0.2")
    target_payload = _package("1.0.1")
    source = _manifest("1.0.2", signer=release_signer, payload=source_payload)
    target = _manifest("1.0.1", signer=release_signer, payload=target_payload)
    release_verifier = Ed25519SignatureVerifier(
        {"release-key": release_signer.public_key_bytes}
    )
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    authorizer = SingleUseRollbackAuthorizer(
        RollbackAuthorizationVerifier(
            Ed25519SignatureVerifier(
                {"rollback-key": rollback_signer.public_key_bytes}
            ),
            clock=lambda: now,
        ),
        platform="windows",
        architecture="x64",
    )
    directories = {}
    for source_id in ("mirror", "github", "cdn"):
        directory = tmp_path / source_id
        directory.mkdir()
        (directory / "ecorex-core.zip").write_bytes(source_payload)
        directories[source_id] = directory
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=LocalSourceFetcher(directories),
        verifier=release_verifier,
        rollback_authorizer=authorizer.authorize,
        health_checker=lambda slot: (
            slot / "payload/runtime/version.txt"
        ).is_file(),
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
    )
    first = coordinator.prepare_update(source, "core-windows-x64")
    coordinator.activate(first.transaction_id)
    assert coordinator.current_release_identity() == _current(source)
    for directory in directories.values():
        (directory / "ecorex-core.zip").write_bytes(target_payload)

    nonce = "z" * 43
    token = issue_rollback_authorization(
        signer=rollback_signer,
        rollback_id="rollback_runtime",
        client_id="client-runtime",
        source_manifest=source,
        target_manifest=target,
        platform="windows",
        architecture="x64",
        request_nonce=nonce,
        now=now,
    )
    authorizer.accept(
        token,
        current=_current(source),
        target=target,
        expected_nonce=nonce,
    )

    class Feed:
        def __init__(self) -> None:
            self.authorization = token

        def latest(self, **_kwargs):
            return target

        def rollback_authorization(self, manifest):
            assert manifest == target
            value, self.authorization = self.authorization, None
            return value

    restarts: list[str] = []
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=coordinator,
        feed=Feed(),
        artifact_id="core-windows-x64",
        current_version=source.version,
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=restarts.append,
    )
    prepared = asyncio.run(service.check_now())
    assert prepared.state == "awaiting_user"
    assert prepared.target_version == target.version
    response = asyncio.run(
        service.activate(
            transaction_id=str(prepared.transaction_id),
            client_request_id="activate-admin-rollback",
        )
    )
    assert response.update.requires_refresh is True
    assert restarts == [prepared.transaction_id]
    assert coordinator.current_release_identity() == _current(target)
