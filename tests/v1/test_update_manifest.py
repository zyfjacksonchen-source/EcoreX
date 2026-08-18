from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from ecorex.update import (
    ManifestError,
    MAX_ARTIFACT_BYTES,
    MAX_CAPABILITY_PACK_ARTIFACT_BYTES,
    MAX_CORE_ARTIFACT_BYTES,
    MAX_MANIFEST_BYTES,
    Ed25519SignatureVerifier,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    RejectingSignatureVerifier,
    SignatureEnvelope,
    SourceKind,
    SignatureVerificationError,
    VerifierUnavailable,
    verify_artifact_file,
    verify_manifest_signature,
)


class AcceptingTestVerifier:
    """Explicit non-production fake; the core itself never includes a bypass."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        assert signature.algorithm == "ed25519"
        assert signature.key_id == "release-key-2026"
        self.payloads.append(payload)
        return True


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key-2026",
        value=base64.b64encode(b"test-only-detached-signature").decode("ascii"),
    )


def _manifest(payload: bytes = b"signed package") -> ReleaseManifest:
    digest = hashlib.sha256(payload).hexdigest()
    return ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.0-stable-001",
        version="1.0.0",
        build_digest=hashlib.sha256(b"build-1.0.0").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T11:00:00+08:00",
        sources=(
            ReleaseSource(
                "github-cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://gh-proxy.example/releases/v1.0.0",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.com/ecorex/releases/download/v1.0.0",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                "https://downloads.example/ecorex/v1.0.0",
            ),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-core.zip",
                size_bytes=len(payload),
                sha256=digest.upper(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def test_release_manifest_round_trips_and_has_stable_signed_payload() -> None:
    manifest = _manifest()

    decoded = ReleaseManifest.from_json(manifest.to_json(pretty=True))

    assert decoded == manifest
    assert decoded.artifacts[0].sha256 == decoded.artifacts[0].sha256.lower()
    assert decoded.canonical_payload() == manifest.canonical_payload()
    assert decoded.canonical_payload().startswith(b"ecorex-release-manifest-v1\n")
    assert b'"signature"' in decoded.to_json().encode()
    assert b'"signature"' not in decoded.canonical_payload().split(b'"artifacts"', 1)[0]
    assert [source.kind for source in decoded.sources] == [
        SourceKind.GITHUB_CN_MIRROR,
        SourceKind.GITHUB_RELEASE,
        SourceKind.ECOREX_CDN,
    ]


def test_manifest_rejects_source_reordering_and_path_traversal() -> None:
    manifest = _manifest()
    with pytest.raises(ManifestError, match="ordered exactly"):
        ReleaseManifest(
            schema_version=manifest.schema_version,
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            channel=manifest.channel,
            created_at=manifest.created_at,
            sources=tuple(reversed(manifest.sources)),
            artifacts=manifest.artifacts,
            signature=manifest.signature,
        )

    same_failure_domain = (
        manifest.sources[0],
        manifest.sources[1],
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://gh-proxy.example/another/path",
        ),
    )
    with pytest.raises(ManifestError, match="distinct download hosts"):
        ReleaseManifest(
            schema_version=manifest.schema_version,
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            channel=manifest.channel,
            created_at=manifest.created_at,
            sources=same_failure_domain,
            artifacts=manifest.artifacts,
            signature=manifest.signature,
        )

    with pytest.raises(ManifestError, match="leading zeroes"):
        ReleaseManifest(
            schema_version=manifest.schema_version,
            release_id=manifest.release_id,
            version="1.0.0-01",
            build_digest=manifest.build_digest,
            channel=manifest.channel,
            created_at=manifest.created_at,
            sources=manifest.sources,
            artifacts=manifest.artifacts,
            signature=manifest.signature,
        )

    artifact = manifest.artifacts[0]
    with pytest.raises(ManifestError, match="one safe path segment"):
        ReleaseArtifact(
            artifact_id=artifact.artifact_id,
            platform=artifact.platform,
            architecture=artifact.architecture,
            file_name="../escape.zip",
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            signature=artifact.signature,
        )


def test_verification_is_explicit_and_fails_closed_without_provider(tmp_path: Path) -> None:
    payload = b"signed package"
    manifest = _manifest(payload)
    artifact_path = tmp_path / "ecorex-core.zip"
    artifact_path.write_bytes(payload)

    with pytest.raises(VerifierUnavailable):
        verify_manifest_signature(manifest, RejectingSignatureVerifier())

    verifier = AcceptingTestVerifier()
    verify_manifest_signature(manifest, verifier)
    verify_artifact_file(artifact_path, manifest, manifest.artifacts[0], verifier)

    assert len(verifier.payloads) == 2
    assert verifier.payloads[1].startswith(b"ecorex-artifact-v1\n")


def test_artifact_hash_mismatch_is_rejected_before_signature(tmp_path: Path) -> None:
    manifest = _manifest(b"expected")
    artifact_path = tmp_path / "ecorex-core.zip"
    artifact_path.write_bytes(b"tampered")
    verifier = AcceptingTestVerifier()

    with pytest.raises(Exception, match="SHA-256 mismatch"):
        verify_artifact_file(artifact_path, manifest, manifest.artifacts[0], verifier)

    assert verifier.payloads == []


def test_signature_verifier_requires_explicit_true() -> None:
    class AmbiguousVerifier:
        def verify(self, payload: bytes, signature: SignatureEnvelope) -> None:
            del payload, signature
            return None

    with pytest.raises(SignatureVerificationError, match="explicit True"):
        verify_manifest_signature(_manifest(), AmbiguousVerifier())


@pytest.mark.parametrize(
    "file_name",
    [
        "CON.zip",
        "nul.txt",
        "release. ",
        "release.",
        "bad?.zip",
        "line\nbreak.zip",
        "ecorex．zip",
    ],
)
def test_manifest_rejects_cross_platform_unsafe_artifact_names(file_name: str) -> None:
    artifact = _manifest().artifacts[0]
    with pytest.raises(ManifestError, match="portable|reserved"):
        ReleaseArtifact(
            artifact_id=artifact.artifact_id,
            platform=artifact.platform,
            architecture=artifact.architecture,
            file_name=file_name,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            signature=artifact.signature,
        )


def test_manifest_enforces_the_release_package_hard_limit() -> None:
    artifact = _manifest().artifacts[0]
    with pytest.raises(ManifestError, match="between"):
        ReleaseArtifact(
            artifact_id=artifact.artifact_id,
            platform=artifact.platform,
            architecture=artifact.architecture,
            file_name=artifact.file_name,
            size_bytes=MAX_ARTIFACT_BYTES + 1,
            sha256=artifact.sha256,
            signature=artifact.signature,
        )

    with pytest.raises(ManifestError, match="1 MiB"):
        ReleaseManifest.from_json(b" " * (MAX_MANIFEST_BYTES + 1))


def test_manifest_keeps_core_bounded_while_allowing_real_browser_pack() -> None:
    artifact = _manifest().artifacts[0]
    with pytest.raises(ManifestError, match="between"):
        ReleaseArtifact(
            artifact_id="core-windows-x64",
            platform="windows",
            architecture="x64",
            file_name="core.zip",
            size_bytes=MAX_CORE_ARTIFACT_BYTES + 1,
            sha256=artifact.sha256,
            signature=artifact.signature,
        )
    pack = ReleaseArtifact(
        artifact_id="capability-pack-browser-windows-x64",
        platform="windows",
        architecture="x64",
        file_name="browser.zip",
        size_bytes=300 * 1024 * 1024,
        sha256=artifact.sha256,
        signature=artifact.signature,
    )
    assert pack.size_bytes > MAX_CORE_ARTIFACT_BYTES
    assert pack.size_bytes <= MAX_CAPABILITY_PACK_ARTIFACT_BYTES
    with pytest.raises(ManifestError, match="between"):
        ReleaseArtifact(
            artifact_id=pack.artifact_id,
            platform=pack.platform,
            architecture=pack.architecture,
            file_name=pack.file_name,
            size_bytes=MAX_CAPABILITY_PACK_ARTIFACT_BYTES + 1,
            sha256=pack.sha256,
            signature=pack.signature,
        )


def test_real_ed25519_verifier_accepts_only_the_trusted_key() -> None:
    asymmetric = pytest.importorskip(
        "cryptography.hazmat.primitives.asymmetric.ed25519"
    )
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private_key = asymmetric.Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = b"ecorex release trust test"
    envelope = SignatureEnvelope(
        "ed25519",
        "trusted-key",
        base64.b64encode(private_key.sign(payload)).decode("ascii"),
    )

    verifier = Ed25519SignatureVerifier({"trusted-key": public_key})
    assert verifier.verify(payload, envelope) is True
    with pytest.raises(SignatureVerificationError, match="invalid"):
        verifier.verify(payload + b"tampered", envelope)
