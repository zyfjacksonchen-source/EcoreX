from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

import ecorex.update.delta as delta_module

from ecorex.update import (
    CoreDeltaEndpoint,
    DeltaError,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    apply_core_delta_archive,
    core_delta_artifact_id,
    core_delta_file_name,
    create_core_delta_archive,
    select_core_delta_artifact,
)


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"s" * 64).decode("ascii"),
    )


def _manifest(
    *, release_id: str, version: str, build: bytes, artifacts: tuple[ReleaseArtifact, ...]
) -> ReleaseManifest:
    return ReleaseManifest(
        schema_version=1,
        release_id=release_id,
        version=version,
        build_digest=hashlib.sha256(build).hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-11T12:00:00+08:00",
        sources=(
            ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://m.example/r"),
            ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://g.example/r"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://c.example/r"),
        ),
        artifacts=artifacts,
        signature=_signature(),
    )


def _artifact(artifact_id: str, path: Path) -> ReleaseArtifact:
    payload = path.read_bytes()
    return ReleaseArtifact(
        artifact_id=artifact_id,
        platform="windows",
        architecture="x64",
        file_name=path.name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=_signature(),
    )


def _release_set(tmp_path: Path):
    base_path = tmp_path / "base-core.bin"
    target_path = tmp_path / "target-core.bin"
    base = bytearray((index * 17 + 31) % 256 for index in range(1024 * 1024))
    target = bytearray(base)
    changed = (b"changed-target-data\n" * 4000)[: 64 * 1024]
    target[5 * 64 * 1024 : 6 * 64 * 1024] = changed
    assert len(target) == len(base)
    base_path.write_bytes(base)
    target_path.write_bytes(target)
    base_artifact = _artifact("core-windows-x64", base_path)
    target_artifact = _artifact("core-windows-x64", target_path)
    base_manifest = _manifest(
        release_id="release-stable-base",
        version="1.0.0",
        build=b"base-build",
        artifacts=(base_artifact,),
    )
    target_without_delta = _manifest(
        release_id="release-stable-target",
        version="1.0.1",
        build=b"target-build",
        artifacts=(target_artifact,),
    )
    delta_path = tmp_path / core_delta_file_name(
        platform="windows",
        architecture="x64",
        base_artifact_sha256=base_artifact.sha256,
        target_artifact_sha256=target_artifact.sha256,
    )
    create_core_delta_archive(
        base_package=base_path,
        target_package=target_path,
        base=CoreDeltaEndpoint.from_release(base_manifest, base_artifact),
        target=CoreDeltaEndpoint.from_release(
            target_without_delta, target_artifact
        ),
        destination=delta_path,
    )
    delta_artifact = _artifact(
        core_delta_artifact_id(
            platform="windows",
            architecture="x64",
            base_artifact_sha256=base_artifact.sha256,
        ),
        delta_path,
    )
    target_manifest = _manifest(
        release_id="release-stable-target",
        version="1.0.1",
        build=b"target-build",
        artifacts=(target_artifact, delta_artifact),
    )
    return (
        base_path,
        target_path,
        delta_path,
        base_manifest,
        base_artifact,
        target_manifest,
        target_artifact,
        delta_artifact,
    )


def test_signed_delta_reconstructs_exact_target_atomically(tmp_path: Path) -> None:
    (
        base_path,
        target_source,
        delta_path,
        base_manifest,
        base_artifact,
        target_manifest,
        target_artifact,
        delta_artifact,
    ) = _release_set(tmp_path)
    output = tmp_path / "transaction" / target_artifact.file_name

    selected = select_core_delta_artifact(
        target_manifest,
        target_artifact=target_artifact,
        base_artifact=base_artifact,
    )
    assert selected == delta_artifact
    assert delta_path.stat().st_size < target_source.stat().st_size

    apply_core_delta_archive(
        delta_path=delta_path,
        delta_artifact=delta_artifact,
        base_package=base_path,
        base_manifest=base_manifest,
        base_artifact=base_artifact,
        target_path=output,
        target_manifest=target_manifest,
        target_artifact=target_artifact,
        verifier=AcceptingVerifier(),
    )

    assert output.read_bytes() == target_source.read_bytes()
    assert hashlib.sha256(output.read_bytes()).hexdigest() == target_artifact.sha256
    assert not list(output.parent.glob(".*.delta-*"))


def test_signed_delta_tamper_never_replaces_transaction_target(tmp_path: Path) -> None:
    (
        base_path,
        _target_source,
        delta_path,
        base_manifest,
        base_artifact,
        target_manifest,
        target_artifact,
        delta_artifact,
    ) = _release_set(tmp_path)
    payload = bytearray(delta_path.read_bytes())
    payload[len(payload) // 2] ^= 0x40
    delta_path.write_bytes(payload)
    output = tmp_path / "transaction" / target_artifact.file_name

    with pytest.raises(Exception):
        apply_core_delta_archive(
            delta_path=delta_path,
            delta_artifact=delta_artifact,
            base_package=base_path,
            base_manifest=base_manifest,
            base_artifact=base_artifact,
            target_path=output,
            target_manifest=target_manifest,
            target_artifact=target_artifact,
            verifier=AcceptingVerifier(),
        )

    assert not output.exists()
    assert not output.parent.exists() or not list(output.parent.glob(".*.delta-*"))


def test_delta_selector_rejects_nonbeneficial_signed_artifact(tmp_path: Path) -> None:
    (
        _base_path,
        target_source,
        _delta_path,
        _base_manifest,
        base_artifact,
        target_manifest,
        target_artifact,
        delta_artifact,
    ) = _release_set(tmp_path)
    oversized = ReleaseArtifact(
        artifact_id=delta_artifact.artifact_id,
        platform=delta_artifact.platform,
        architecture=delta_artifact.architecture,
        file_name=delta_artifact.file_name,
        size_bytes=target_source.stat().st_size,
        sha256=delta_artifact.sha256,
        signature=delta_artifact.signature,
    )
    manifest = _manifest(
        release_id=target_manifest.release_id,
        version=target_manifest.version,
        build=b"target-build",
        artifacts=(target_artifact, oversized),
    )
    with pytest.raises(DeltaError, match="not beneficial"):
        select_core_delta_artifact(
            manifest,
            target_artifact=target_artifact,
            base_artifact=base_artifact,
        )


def test_delta_apply_rejects_artifact_not_bound_to_the_signed_manifest(
    tmp_path: Path,
) -> None:
    (
        base_path,
        _target_source,
        delta_path,
        base_manifest,
        base_artifact,
        target_manifest,
        target_artifact,
        delta_artifact,
    ) = _release_set(tmp_path)
    foreign_target = replace(target_artifact, file_name="foreign-target.bin")

    with pytest.raises(DeltaError, match="differs from its signed release"):
        apply_core_delta_archive(
            delta_path=delta_path,
            delta_artifact=delta_artifact,
            base_package=base_path,
            base_manifest=base_manifest,
            base_artifact=base_artifact,
            target_path=tmp_path / "output.bin",
            target_manifest=target_manifest,
            target_artifact=foreign_target,
            verifier=AcceptingVerifier(),
        )


def test_delta_literal_overflow_never_calls_unbounded_zlib_flush(monkeypatch) -> None:
    class OverflowingDecompressor:
        eof = False
        unused_data = b""
        unconsumed_tail = b"compressed-tail"

        @staticmethod
        def decompress(_payload: bytes, maximum: int) -> bytes:
            return b"x" * maximum

        @staticmethod
        def flush() -> bytes:  # pragma: no cover - must never be reached
            raise AssertionError("unbounded zlib flush was called")

    monkeypatch.setattr(
        delta_module.zlib,
        "decompressobj",
        lambda: OverflowingDecompressor(),
    )
    with pytest.raises(DeltaError, match="invalid bounds"):
        delta_module._decompress_exact(b"payload", 128 * 1024)
