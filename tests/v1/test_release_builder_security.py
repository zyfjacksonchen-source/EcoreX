from __future__ import annotations

import json
import stat
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    ReleaseBuildError,
    ReleaseBuilder,
    ReleaseBuildSpec,
    SigningError,
)
from ecorex.release.builder import _metadata_is_link_or_reparse
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


class _ShortSignatureSigner:
    key_id = "test-broken-signer"

    def sign(self, payload: bytes) -> bytes:
        return b"not-an-ed25519-signature"


class _ExplodingSigner:
    key_id = "test-exploding-signer"

    def sign(self, payload: bytes) -> bytes:
        raise SigningError("SECRET-KEY-MATERIAL-MUST-NOT-ESCAPE")


class _ExplodingIdentitySigner:
    @property
    def key_id(self) -> str:
        raise RuntimeError("SECRET-IDENTITY-BACKEND-DETAIL")

    def sign(self, payload: bytes) -> bytes:
        return b"0" * 64


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"),
        ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
    )


def _artifact(source: Path, **changes: object) -> ArtifactBuildInput:
    base = ArtifactBuildInput(
        source_dir=source,
        kind=ArtifactKind.CORE,
        platform="windows",
        architecture="x64",
    )
    return replace(base, **changes)


def _spec(artifact: ArtifactBuildInput, *, sources: tuple[ReleaseSource, ...] | None = None):
    return ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=sources or _sources(),
        artifacts=(artifact,),
    )


def test_builder_rejects_reserved_output_name_before_publishing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"

    with pytest.raises(ReleaseBuildError, match="reserved|colliding"):
        ReleaseBuilder(_ShortSignatureSigner()).build(
            _spec(_artifact(source, file_name="SBOM.CDX.JSON")), destination
        )

    assert not destination.exists()


def test_builder_rejects_wrong_source_priority_before_signing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"

    with pytest.raises(ReleaseBuildError, match="sources must be ordered"):
        ReleaseBuilder(_ShortSignatureSigner()).build(
            _spec(_artifact(source), sources=tuple(reversed(_sources()))), destination
        )

    assert not destination.exists()


def test_builder_rejects_invalid_release_timestamp_before_signing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"
    spec = replace(_spec(_artifact(source)), created_at="2026-07-10 12:00:00")

    with pytest.raises(ReleaseBuildError, match="created_at"):
        ReleaseBuilder(_ShortSignatureSigner()).build(spec, destination)

    assert not destination.exists()


def test_source_mode_never_changes_declared_archive_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "runtime.bin"
    payload.write_bytes(b"runtime")
    payload.chmod(0o755)

    # This deliberately uses a broken signer. Packaging happens before
    # signing, so inspect the private deterministic ZIP primitive through a
    # complete build with a real in-memory signer supplied by the main suite.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from ecorex.release import Ed25519MemorySigner

    result = ReleaseBuilder(
        Ed25519MemorySigner("test-key", Ed25519PrivateKey.generate())
    ).build(_spec(_artifact(source)), tmp_path / "release")
    package = next(iter(result.artifact_paths.values()))
    with zipfile.ZipFile(package) as archive:
        mode = stat.S_IMODE(archive.getinfo("runtime.bin").external_attr >> 16)
    assert mode == 0o644


def test_builder_rejects_crlf_shell_instead_of_silently_rewriting_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bootstrap.sh").write_bytes(b"#!/usr/bin/env sh\r\necho ready\r\n")
    destination = tmp_path / "release"

    with pytest.raises(ReleaseBuildError, match="must use LF line endings"):
        ReleaseBuilder(_ShortSignatureSigner()).build(
            _spec(
                _artifact(
                    source,
                    platform="macos",
                    architecture="arm64",
                    executable_paths=("bootstrap.sh",),
                )
            ),
            destination,
        )

    assert not destination.exists()


def test_builder_preserves_lf_shell_bytes_and_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = b"#!/usr/bin/env sh\necho ready\n"
    (source / "bootstrap.sh").write_bytes(payload)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from ecorex.release import Ed25519MemorySigner

    result = ReleaseBuilder(
        Ed25519MemorySigner("test-key", Ed25519PrivateKey.generate())
    ).build(
        _spec(
            _artifact(
                source,
                platform="macos",
                architecture="arm64",
                executable_paths=("bootstrap.sh",),
            )
        ),
        tmp_path / "release",
    )
    package = next(iter(result.artifact_paths.values()))
    with zipfile.ZipFile(package) as archive:
        assert archive.read("bootstrap.sh") == payload


def test_windows_reparse_attribute_is_rejected_without_symlink_privilege() -> None:
    metadata = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o644,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    assert _metadata_is_link_or_reparse(metadata)


def test_invalid_signer_output_never_publishes_or_leaks_exception_details(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"

    with pytest.raises(SigningError, match="exactly 64"):
        ReleaseBuilder(_ShortSignatureSigner()).build(_spec(_artifact(source)), destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".release.staging-*"))


def test_signer_exception_is_redacted_and_has_no_sensitive_exception_chain(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"

    with pytest.raises(SigningError) as raised:
        ReleaseBuilder(_ExplodingSigner()).build(_spec(_artifact(source)), destination)

    assert "SECRET-KEY-MATERIAL" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert not destination.exists()


def test_signer_identity_property_is_not_evaluated_outside_redacted_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    destination = tmp_path / "release"

    with pytest.raises(SigningError) as raised:
        ReleaseBuilder(_ExplodingIdentitySigner()).build(
            _spec(_artifact(source)), destination
        )

    assert "SECRET-IDENTITY" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert not destination.exists()


@pytest.mark.parametrize("bad_paths", ["bin/ecorex", ("bin/ecorex", 3)])
def test_artifact_input_rejects_ambiguous_executable_path_sequences(
    tmp_path: Path, bad_paths: object
) -> None:
    with pytest.raises(ValueError, match="executable"):
        ArtifactBuildInput(
            source_dir=tmp_path,
            kind=ArtifactKind.CORE,
            platform="windows",
            architecture="x64",
            executable_paths=bad_paths,  # type: ignore[arg-type]
        )


def test_release_metadata_is_parseable_after_atomic_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.bin").write_bytes(b"runtime")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from ecorex.release import Ed25519MemorySigner

    result = ReleaseBuilder(
        Ed25519MemorySigner("test-key", Ed25519PrivateKey.generate())
    ).build(_spec(_artifact(source)), tmp_path / "release")

    metadata = json.loads(result.metadata_path.read_bytes())
    assert metadata["release_id"] == result.manifest.release_id
    assert not list(result.output_dir.glob(".*.tmp"))
