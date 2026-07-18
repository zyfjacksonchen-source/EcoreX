from __future__ import annotations

from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
import hashlib
import json
import os
from pathlib import Path
import stat
from threading import Barrier, Event

import pytest

import ecorex.storage.attested_local_cas as attested_local_cas_module
from ecorex.control_plane.share_attested_local_objects import (
    AttestedLocalShareObjectStore,
)
from ecorex.image_orchestrator.attested_local_cas import (
    AttestedLocalImageContentStore,
)
from ecorex.image_orchestrator.cas import ImageContentReference
from ecorex.image_orchestrator.models import ImageResultRejected
from ecorex.storage.attested_local_cas import (
    AttestedEncryptedLocalCAS,
    AttestedEncryptedLocalVolume,
    AttestedLocalCASError,
    MountObservation,
    PosixGroupCASFileSecurity,
    _ensure_directory,
)


MACHINE = b"0123456789abcdef0123456789abcdef"
MACHINE_SHA = hashlib.sha256(MACHINE).hexdigest()
PNG = b"\x89PNG\r\n\x1a\n" + b"attested-image" * 16


class PortableSecurity:
    """Test-only policy; production composition uses POSIX group enforcement."""

    @staticmethod
    def validate_directory(path: Path) -> None:
        metadata = path.lstat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert not path.is_symlink()
        assert not bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    @staticmethod
    def prepare_directory(path: Path) -> None:
        PortableSecurity.validate_directory(path)

    @staticmethod
    def validate_file(
        path: Path, *, allow_multiple_links: bool = False
    ) -> os.stat_result:
        metadata = path.lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert not path.is_symlink()
        if not allow_multiple_links:
            assert metadata.st_nlink == 1
        return metadata

    @staticmethod
    def prepare_file(_descriptor: int) -> None:
        return None


def _machine() -> bytes:
    return MACHINE


def _mount(path: Path) -> MountObservation:
    return MountObservation(path, path.stat().st_dev, True)


def _not_mount(path: Path) -> MountObservation:
    return MountObservation(path, path.stat().st_dev, False)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-mode contract")
def test_empty_umask_reduced_directory_is_repaired_but_content_is_not(
    tmp_path: Path,
) -> None:
    security = PosixGroupCASFileSecurity(owner_gid=os.getegid())
    empty = tmp_path / "empty"
    empty.mkdir()
    empty.chmod(0o2700)

    _ensure_directory(empty, security)
    assert stat.S_IMODE(empty.stat().st_mode) == 0o2770

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    occupied.chmod(0o2700)
    (occupied / "data").write_bytes(b"x")
    with pytest.raises(
        AttestedLocalCASError,
        match="attested_local_cas_directory_permission_invalid",
    ):
        _ensure_directory(occupied, security)


def _fixture(tmp_path: Path, *, quota: int = 8 * 1024 * 1024):
    mount = tmp_path / "encrypted-volume"
    mount.mkdir()
    root = mount / "cas"
    root.mkdir()
    attestation = tmp_path / "encrypted-volume-attestation.json"
    attestation.write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "provider": "luks2",
                "volume_id": "ecorex-volume-a",
                "mount_root": str(mount.absolute()),
                "encrypted": True,
                "evidence_reference": "kms:evidence:immutable",
                "evidence_sha256": "a" * 64,
            }
        )
    )
    digest = hashlib.sha256(attestation.read_bytes()).hexdigest()
    volume = _open_volume(root, attestation, digest, quota=quota)
    return volume, root, attestation, digest


def _open_volume(
    root: Path,
    attestation: Path,
    digest: str,
    *,
    quota: int = 8 * 1024 * 1024,
    replica_count: int = 1,
    mount_probe=_mount,
) -> AttestedEncryptedLocalVolume:
    return AttestedEncryptedLocalVolume(
        cas_root=root.absolute(),
        attestation_path=attestation.absolute(),
        expected_attestation_sha256=digest,
        expected_volume_id="ecorex-volume-a",
        expected_machine_id_sha256=MACHINE_SHA,
        replica_count=replica_count,
        quota_bytes=quota,
        minimum_free_bytes=1024 * 1024,
        security=PortableSecurity(),
        mount_probe=mount_probe,
        machine_id_reader=_machine,
    )


def _process_put(arguments: tuple[str, str, str, bytes]) -> tuple[str, int]:
    root, attestation, digest, payload = arguments
    volume = _open_volume(Path(root), Path(attestation), digest)
    stored = AttestedEncryptedLocalCAS(
        volume, namespace="share", max_blob_bytes=1024 * 1024
    ).put(payload)
    return stored.sha256, stored.size_bytes


def test_attestation_machine_mount_replica_and_marker_are_fail_closed(
    tmp_path: Path,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    marker = root / volume.MARKER
    assert json.loads(marker.read_text(encoding="utf-8")) == volume.marker_identity()
    assert volume.replica_count == 1

    with pytest.raises(
        AttestedLocalCASError, match="attested_local_cas_attestation_digest_mismatch"
    ):
        _open_volume(root, attestation, "0" * 64)
    with pytest.raises(
        AttestedLocalCASError, match="attested_local_cas_replica_count_invalid"
    ):
        _open_volume(root, attestation, digest, replica_count=2)
    with pytest.raises(
        AttestedLocalCASError, match="attested_local_cas_mount_identity_mismatch"
    ):
        _open_volume(root, attestation, digest, mount_probe=_not_mount)

    value = json.loads(marker.read_text(encoding="utf-8"))
    value["multi_host_ha"] = True
    marker.write_bytes(_canonical(value))
    with pytest.raises(AttestedLocalCASError, match="attested_local_cas_marker_conflict"):
        _open_volume(root, attestation, digest)


def test_cross_process_create_if_absent_produces_one_verified_blob(
    tmp_path: Path,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    payload = b"same immutable payload" * 4096
    arguments = (str(root), str(attestation), digest, payload)
    with ProcessPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(_process_put, [arguments] * 12))

    expected = hashlib.sha256(payload).hexdigest()
    assert values == [(expected, len(payload))] * 12
    cas = AttestedEncryptedLocalCAS(
        volume, namespace="share", max_blob_bytes=1024 * 1024
    )
    assert cas.list_blob_digests() == (expected,)
    assert cas.read(expected) == payload
    assert not list(root.rglob("*.new"))
    assert not list(root.rglob("*.tmp"))


def test_volume_quota_and_health_probe_cover_all_namespaces(tmp_path: Path) -> None:
    volume, _root, _attestation, _digest_value = _fixture(
        tmp_path, quota=1024 * 1024
    )
    share = AttestedEncryptedLocalCAS(
        volume, namespace="share", max_blob_bytes=800 * 1024
    )
    image = AttestedEncryptedLocalCAS(
        volume, namespace="image", max_blob_bytes=800 * 1024
    )
    share.put(b"a" * (600 * 1024))
    with pytest.raises(AttestedLocalCASError, match="attested_local_cas_quota_exceeded"):
        image.put(b"b" * (600 * 1024))

    receipt = share.health_probe(write_probe=True, deep=True)
    assert receipt["status"] == "passed"
    assert receipt["backend"] == "attested-encrypted-local-cas"
    assert receipt["availability_scope"] == "single-host"
    assert receipt["multi_host_ha"] is False
    assert receipt["replica_count"] == 1
    assert receipt["attestation_sha256"] == volume.attestation_sha256
    assert receipt["blob_count"] == 1


def test_image_health_probe_is_invisible_to_concurrent_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    api = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            volume, namespace="image", max_blob_bytes=1024 * 1024
        )
    )
    worker = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            _open_volume(root, attestation, digest),
            namespace="image",
            max_blob_bytes=1024 * 1024,
        )
    )
    probe_written = Event()
    release_probe = Event()
    listing_started = Event()
    original_write = attested_local_cas_module._write_temporary
    original_list = worker.cas.list_blob_digests

    def paused_probe_write(
        path: Path,
        payload: bytes,
        security: PortableSecurity,
    ) -> None:
        original_write(path, payload, security)
        if path.name.startswith(".health-"):
            probe_written.set()
            if not release_probe.wait(5):
                raise RuntimeError("health probe test barrier timed out")

    def observed_list() -> tuple[str, ...]:
        listing_started.set()
        return original_list()

    monkeypatch.setattr(
        attested_local_cas_module,
        "_write_temporary",
        paused_probe_write,
    )
    monkeypatch.setattr(worker.cas, "list_blob_digests", observed_list)

    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(api.health_probe, write_probe=True)
        assert probe_written.wait(5)
        recovery = pool.submit(worker.health_probe, write_probe=False)
        assert listing_started.wait(5)
        try:
            with pytest.raises(FutureTimeoutError):
                recovery.result(timeout=0.1)
        finally:
            release_probe.set()
        assert probe.result(timeout=5)["status"] == "passed"
        assert recovery.result(timeout=5)["status"] == "passed"

    assert api.cas.list_blob_digests() == ()
    assert not list(root.rglob(".health-*.tmp"))


def test_crash_left_health_attempt_is_removed_before_next_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, root, _attestation, _digest_value = _fixture(tmp_path)
    cas = AttestedEncryptedLocalCAS(
        volume, namespace="image", max_blob_bytes=1024 * 1024
    )
    original_write = attested_local_cas_module._write_temporary

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_after_write(
        path: Path,
        payload: bytes,
        security: PortableSecurity,
    ) -> None:
        original_write(path, payload, security)
        if path.name.startswith(".health-"):
            raise SimulatedProcessCrash

    monkeypatch.setattr(
        attested_local_cas_module,
        "_write_temporary",
        crash_after_write,
    )
    with pytest.raises(SimulatedProcessCrash):
        cas.health_probe(write_probe=True)
    assert len(list(root.rglob(".health-*.tmp"))) == 1

    monkeypatch.setattr(
        attested_local_cas_module,
        "_write_temporary",
        original_write,
    )
    assert cas.health_probe(write_probe=False)["status"] == "passed"
    assert not list(root.rglob(".health-*.tmp"))


def test_share_api_adapter_streams_verified_bytes_without_paths(tmp_path: Path) -> None:
    volume, _root, _attestation, _digest_value = _fixture(tmp_path)
    adapter = AttestedLocalShareObjectStore(
        AttestedEncryptedLocalCAS(
            volume, namespace="share", max_blob_bytes=1024 * 1024
        )
    )
    payload = b"share image payload"
    digest = hashlib.sha256(payload).hexdigest()
    stored = adapter.put(payload, sha256=digest, mime_type="image/png")

    assert stored.object_key == f"local-cas/share/sha256/{digest}"
    opened = adapter.open(
        stored.object_key,
        sha256=digest,
        size_bytes=len(payload),
        mime_type="image/png",
    )
    assert b"".join(opened.iter_range(0, len(payload) - 1)) == payload
    assert adapter.delete(stored.object_key, sha256=digest)
    assert not adapter.delete(stored.object_key, sha256=digest)


def test_image_api_and_worker_instances_share_cross_process_safe_references(
    tmp_path: Path,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    api = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            volume, namespace="image", max_blob_bytes=1024 * 1024
        )
    )
    worker_volume = _open_volume(root, attestation, digest)
    worker = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            worker_volume, namespace="image", max_blob_bytes=1024 * 1024
        )
    )
    result = api.put(
        PNG,
        mime_type="image/png",
        reference=ImageContentReference("image-input", "tenant-a"),
    )
    assert worker.read(result.sha256) == PNG

    references = [
        ImageContentReference("job-result", f"job-{index}") for index in range(32)
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda value: worker.add_reference(result.sha256, value), references))
    metadata = api.describe(result.sha256)
    assert set(metadata.references) == {
        ImageContentReference("image-input", "tenant-a"),
        *references,
    }
    for reference in tuple(metadata.references):
        metadata = api.release_reference(result.sha256, reference)
    assert api.delete_if_unreferenced(
        result.sha256, expected_reference_version=metadata.reference_version
    )
    with pytest.raises(ImageResultRejected):
        worker.read(result.sha256)


def test_image_crash_recovery_repairs_orphan_and_finishes_tombstone(
    tmp_path: Path,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    raw = AttestedEncryptedLocalCAS(
        volume, namespace="image", max_blob_bytes=1024 * 1024
    )
    orphan = raw.put(PNG)
    restarted = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            _open_volume(root, attestation, digest),
            namespace="image",
            max_blob_bytes=1024 * 1024,
        )
    )
    assert restarted.recover() == {
        "repaired_orphans": 1,
        "reconciled_deletions": 0,
    }
    metadata = restarted.describe(orphan.sha256)
    tombstone = restarted._write_metadata(  # noqa: SLF001 - crash fixture
        metadata.result,
        (),
        state="deleting",
        expected_version=metadata.reference_version,
    )
    assert tombstone.state == "deleting"

    second_restart = AttestedLocalImageContentStore(
        AttestedEncryptedLocalCAS(
            _open_volume(root, attestation, digest),
            namespace="image",
            max_blob_bytes=1024 * 1024,
        )
    )
    assert second_restart.recover() == {
        "repaired_orphans": 0,
        "reconciled_deletions": 1,
    }
    assert raw.list_blob_digests() == ()


def test_concurrent_image_recovery_converges_on_one_orphan_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume, root, attestation, digest = _fixture(tmp_path)
    raw = AttestedEncryptedLocalCAS(
        volume, namespace="image", max_blob_bytes=1024 * 1024
    )
    orphan = raw.put(PNG)
    stores = tuple(
        AttestedLocalImageContentStore(
            AttestedEncryptedLocalCAS(
                _open_volume(root, attestation, digest),
                namespace="image",
                max_blob_bytes=1024 * 1024,
            )
        )
        for _index in range(2)
    )
    both_ready_to_publish = Barrier(2)

    for store in stores:
        original_write = store._write_metadata  # noqa: SLF001 - concurrency fixture

        def synchronized_write(
            *args: object,
            _write=original_write,
            **kwargs: object,
        ):
            if kwargs.get("expected_version") is None:
                both_ready_to_publish.wait(timeout=5)
            return _write(*args, **kwargs)

        monkeypatch.setattr(store, "_write_metadata", synchronized_write)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda store: store.recover(), stores))

    assert sorted(receipt["repaired_orphans"] for receipt in receipts) == [0, 1]
    assert {receipt["reconciled_deletions"] for receipt in receipts} == {0}
    assert stores[0].describe(orphan.sha256).result.sha256 == orphan.sha256
    assert raw.list_blob_digests() == (orphan.sha256,)
