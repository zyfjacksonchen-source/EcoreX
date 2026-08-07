from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import threading
import time

import pytest

import ecorex.update.download_cache as download_cache_module

from ecorex.update import (
    ContentVerificationError,
    InstallCoordinator,
    InstallState,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    VerifiedDownloadCache,
)


class _AcceptingVerifier:
    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        assert payload
        assert signature.key_id == "download-cache-test"
        return True


def test_verified_cache_copy_prefers_copy_on_write_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"immutable dependency bytes")
    clone_calls = 0

    def clone(first: Path, second: Path) -> bool:
        nonlocal clone_calls
        clone_calls += 1
        second.write_bytes(first.read_bytes())
        return True

    def reject_stream_copy(*_args, **_kwargs) -> None:
        raise AssertionError("copy-on-write hit must not stream the file")

    monkeypatch.setattr(download_cache_module, "_try_clone_regular", clone)
    monkeypatch.setattr(download_cache_module.shutil, "copyfileobj", reject_stream_copy)

    download_cache_module._copy_regular_stable(source, destination)

    assert clone_calls == 1
    assert destination.read_bytes() == source.read_bytes()


class _CountingFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(
        self,
        source,
        artifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        del source, artifact
        self.calls += 1
        assert max_bytes == len(self.payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("ab" if resume_from else "wb") as stream:
            stream.write(self.payload[resume_from:])
            stream.flush()
            os.fsync(stream.fileno())


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        "ed25519",
        "download-cache-test",
        base64.b64encode(b"test-signature").decode("ascii"),
    )


def _manifest(
    payload: bytes,
    *,
    artifact_id: str = "core-windows-x64",
    file_name: str = "ecorex-core.zip",
    version: str = "1.0.0",
) -> tuple[ReleaseManifest, ReleaseArtifact]:
    artifact = ReleaseArtifact(
        artifact_id=artifact_id,
        platform="windows",
        architecture="x64",
        file_name=file_name,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=_signature(),
    )
    manifest = ReleaseManifest(
        schema_version=1,
        release_id=f"release-cache-{hashlib.sha256(payload).hexdigest()[:20]}",
        version=version,
        build_digest=hashlib.sha256(b"build\0" + payload).hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-12T00:00:00Z",
        sources=(
            ReleaseSource("cn", SourceKind.GITHUB_CN_MIRROR, 0, "https://cn.example/v1"),
            ReleaseSource("gh", SourceKind.GITHUB_RELEASE, 1, "https://gh.example/v1"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
        ),
        artifacts=(artifact,),
        signature=_signature(),
    )
    return manifest, artifact


def test_unverified_bytes_never_enter_the_content_addressed_object_root(
    tmp_path: Path,
) -> None:
    manifest, artifact = _manifest(b"verified release bytes")
    cache = VerifiedDownloadCache(tmp_path / "cache", verifier=_AcceptingVerifier())
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"unverified release bytes")

    with cache.acquire(manifest, artifact) as lease:
        with pytest.raises(ContentVerificationError):
            lease.admit(bad)

    assert not tuple(cache.objects.rglob(artifact.sha256))
    assert not tuple(cache.incoming.iterdir())


def test_corrupt_cache_object_is_quarantined_and_never_materialized(
    tmp_path: Path,
) -> None:
    payload = b"signed cache object"
    manifest, artifact = _manifest(payload)
    cache = VerifiedDownloadCache(tmp_path / "cache", verifier=_AcceptingVerifier())
    source = tmp_path / artifact.file_name
    source.write_bytes(payload)
    with cache.acquire(manifest, artifact) as lease:
        object_path = lease.admit(source)
    assert object_path is not None
    object_path.write_bytes(b"tampered cache object")

    destination = tmp_path / "transaction" / artifact.file_name
    with cache.acquire(manifest, artifact) as lease:
        assert lease.materialize(destination) is False

    assert not destination.exists()
    assert not object_path.exists()
    quarantined = tuple(cache.quarantine.iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"tampered cache object"


def test_digest_lock_single_flights_concurrent_producers_and_copies_isolate_writes(
    tmp_path: Path,
) -> None:
    payload = b"one producer across concurrent transactions"
    manifest, artifact = _manifest(payload)
    cache = VerifiedDownloadCache(tmp_path / "cache", verifier=_AcceptingVerifier())
    barrier = threading.Barrier(2)
    producer_count = 0
    counter_lock = threading.Lock()
    failures: list[BaseException] = []
    destinations = [tmp_path / f"transaction-{index}.zip" for index in range(2)]

    def materialize(index: int) -> None:
        nonlocal producer_count
        try:
            barrier.wait(timeout=5)
            with cache.acquire(manifest, artifact) as lease:
                if not lease.materialize(destinations[index]):
                    with counter_lock:
                        producer_count += 1
                    source = tmp_path / f"producer-{index}.zip"
                    source.write_bytes(payload)
                    time.sleep(0.1)
                    lease.admit(source)
                    assert lease.materialize(destinations[index]) is True
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=materialize, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert producer_count == 1
    assert [path.read_bytes() for path in destinations] == [payload, payload]
    destinations[0].write_bytes(b"writable transaction changed")
    third = tmp_path / "transaction-third.zip"
    with cache.acquire(manifest, artifact) as lease:
        assert lease.materialize(third) is True
    assert third.read_bytes() == payload


def test_gc_enforces_age_capacity_and_cleans_crash_left_incoming(
    tmp_path: Path,
) -> None:
    first_payload = b"a" * 80
    second_payload = b"b" * 80
    first_manifest, first_artifact = _manifest(first_payload, file_name="first.zip")
    second_manifest, second_artifact = _manifest(second_payload, file_name="second.zip")
    cache = VerifiedDownloadCache(
        tmp_path / "cache",
        verifier=_AcceptingVerifier(),
        max_bytes=100,
        max_age_seconds=60,
        quarantine_age_seconds=60,
    )
    paths: list[Path] = []
    for manifest, artifact, payload in (
        (first_manifest, first_artifact, first_payload),
        (second_manifest, second_artifact, second_payload),
    ):
        source = tmp_path / artifact.file_name
        source.write_bytes(payload)
        with cache.acquire(manifest, artifact) as lease:
            admitted = lease.admit(source)
        assert admitted is not None
        paths.append(admitted)
    old = time.time() - 120
    os.utime(paths[0], (old, old))
    incoming = cache.incoming / f"{first_artifact.sha256}.crash.partial"
    incoming.write_bytes(b"never verified")
    os.utime(incoming, (old, old))

    result = cache.collect(keep_digests=(second_artifact.sha256,))

    assert result.removed_objects == 1
    assert result.removed_bytes == len(first_payload)
    assert not paths[0].exists()
    assert paths[1].exists()
    assert not incoming.exists()
    assert result.retained_bytes == len(second_payload)


def test_install_coordinator_reuses_verified_full_download_after_cancel(
    tmp_path: Path,
) -> None:
    payload = b"not-a-zip-but-valid-single-file-core"
    manifest, artifact = _manifest(payload)
    fetcher = _CountingFetcher(payload)
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=fetcher,
        verifier=_AcceptingVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
    )

    first = coordinator.prepare(manifest, artifact.artifact_id)
    assert first.state is InstallState.AWAITING_USER
    assert fetcher.calls == 1
    coordinator.cancel_pending_activation(first.transaction_id)

    second = coordinator.prepare(manifest, artifact.artifact_id)

    assert second.state is InstallState.AWAITING_USER
    assert fetcher.calls == 1
    assert "artifact_restored_from_download_cache" in {
        entry.event for entry in coordinator.journal.entries()
    }
