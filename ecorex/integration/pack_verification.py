"""Product adapter for the update domain's inner Pack verification protocol."""

from __future__ import annotations

from pathlib import Path

from ecorex.capabilities.packs import (
    CapabilityPackManifest,
    verify_capability_pack,
)
from ecorex.update.manifest import ReleaseArtifact
from ecorex.update.verification import SignatureVerifier


RUNTIME_API_VERSION = "1.0.0"


def verify_product_capability_pack(
    sidecar_payload: bytes,
    artifact_path: Path,
    *,
    pack_id: str,
    release_version: str,
    platform: str,
    architecture: str,
    artifact: ReleaseArtifact,
    verifier: SignatureVerifier,
) -> None:
    inner = CapabilityPackManifest.from_bytes(sidecar_payload)
    if (
        inner.pack_id != pack_id
        or inner.version != release_version
        or inner.platform != platform
        or inner.architecture != architecture
        or inner.artifact_file_name != artifact.file_name
        or inner.artifact_size_bytes != artifact.size_bytes
        or inner.artifact_sha256 != artifact.sha256
        or inner.runtime_api_version != RUNTIME_API_VERSION
    ):
        raise ValueError("Capability Pack sidecar identity is inconsistent")
    verify_capability_pack(
        inner,
        artifact_path,
        verifier=verifier,
        platform=platform,
        architecture=architecture,
        runtime_api_version=RUNTIME_API_VERSION,
    )


__all__ = ["verify_product_capability_pack"]
