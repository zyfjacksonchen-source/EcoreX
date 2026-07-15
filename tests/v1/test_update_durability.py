from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import stat
import time
import zipfile
from pathlib import Path

import pytest
import ecorex.update.storage as storage_module

from ecorex.update import (
    InstallJournal,
    InstallState,
    JournalCorruption,
    LockUnavailable,
    ProductFileLock,
    FetchError,
    LocalSourceFetcher,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SlotStore,
    SourceKind,
    UnsafePackage,
)


def _child_lock_attempt(path: str, queue: multiprocessing.Queue) -> None:
    try:
        with ProductFileLock(path, timeout=0.15, poll_interval=0.01):
            queue.put("acquired")
    except LockUnavailable:
        queue.put("blocked")


def _child_lock_and_exit(path: str, ready: multiprocessing.Event) -> None:
    lock = ProductFileLock(path, timeout=1.0)
    lock.acquire()
    ready.set()
    os._exit(23)


_SPAWN_DEADLINE_SECONDS = 20.0
_PROCESS_CLEANUP_SECONDS = 2.0


def _process_diagnostic(process: multiprocessing.Process) -> str:
    return (
        f"pid={process.pid}, exitcode={process.exitcode}, "
        f"alive={process.is_alive()}"
    )


def _wait_for_process_exit(
    process: multiprocessing.Process,
    *,
    timeout: float = _SPAWN_DEADLINE_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                "spawned lock-test child did not exit before the bounded "
                f"{timeout:.1f}s deadline ({_process_diagnostic(process)})"
            )
        process.join(timeout=min(0.1, remaining))
    process.join(timeout=0)


def _wait_for_process_ready(
    process: multiprocessing.Process,
    ready: multiprocessing.Event,
    *,
    timeout: float = _SPAWN_DEADLINE_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                "spawned lock-test child did not signal readiness before the "
                f"bounded {timeout:.1f}s deadline "
                f"({_process_diagnostic(process)})"
            )
        if ready.wait(timeout=min(0.1, remaining)):
            return
        if not process.is_alive():
            process.join(timeout=0)
            pytest.fail(
                "spawned lock-test child exited before signalling readiness "
                f"({_process_diagnostic(process)})"
            )


def _reap_process(process: multiprocessing.Process) -> None:
    """Best-effort cleanup that never replaces the test's original failure."""

    try:
        process.join(timeout=_PROCESS_CLEANUP_SECONDS)
    except Exception:
        pass
    try:
        alive = process.is_alive()
    except Exception:
        return
    if alive:
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.join(timeout=_PROCESS_CLEANUP_SECONDS)
        except Exception:
            pass
    try:
        alive = process.is_alive()
    except Exception:
        return
    if alive and hasattr(process, "kill"):
        try:
            process.kill()
            process.join(timeout=_PROCESS_CLEANUP_SECONDS)
        except Exception:
            pass


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        "ed25519",
        "test-key",
        base64.b64encode(b"fixture").decode("ascii"),
    )


def _storage_manifest(package: Path) -> tuple[ReleaseManifest, ReleaseArtifact]:
    payload = package.read_bytes()
    artifact = ReleaseArtifact(
        "core-macos-arm64",
        "macos",
        "arm64",
        package.name,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        _signature(),
    )
    manifest = ReleaseManifest(
        1,
        "release-storage-test",
        "1.0.0",
        hashlib.sha256(b"storage-build").hexdigest(),
        ReleaseChannel.STABLE,
        "2026-07-10T00:00:00Z",
        (
            ReleaseSource("cn", SourceKind.GITHUB_CN_MIRROR, 0, "https://cn.example/v1"),
            ReleaseSource("gh", SourceKind.GITHUB_RELEASE, 1, "https://gh.example/v1"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
        ),
        (artifact,),
        _signature(),
    )
    return manifest, artifact


def test_product_lock_is_reentrant_but_excludes_another_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "ecorex-product.lock"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()

    with ProductFileLock(lock_path, timeout=0.0) as lock:
        with lock:
            process = context.Process(target=_child_lock_attempt, args=(str(lock_path), queue))
            process.start()
            try:
                _wait_for_process_exit(process)
                assert process.exitcode == 0, _process_diagnostic(process)
                assert queue.get(timeout=2) == "blocked"
            finally:
                _reap_process(process)

    with ProductFileLock(lock_path, timeout=0.0):
        pass


def test_product_lock_releases_after_exception_and_process_death(tmp_path: Path) -> None:
    lock_path = tmp_path / "ecorex-product.lock"
    with pytest.raises(RuntimeError, match="boom"):
        with ProductFileLock(lock_path, timeout=0.0):
            raise RuntimeError("boom")
    with ProductFileLock(lock_path, timeout=0.0):
        pass

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_child_lock_and_exit, args=(str(lock_path), ready))
    process.start()
    try:
        _wait_for_process_ready(process, ready)
        _wait_for_process_exit(process)
        assert process.exitcode == 23, _process_diagnostic(process)
    finally:
        _reap_process(process)
    with ProductFileLock(lock_path, timeout=1.0):
        pass


def test_journal_is_hash_chained_and_ignores_only_a_partial_tail(tmp_path: Path) -> None:
    path = tmp_path / "journal.ndjson"
    journal = InstallJournal(path)
    transaction_id = "txn-1"
    journal.append(
        transaction_id=transaction_id,
        state=InstallState.RESOLVING,
        event="started",
    )
    journal.append(
        transaction_id=transaction_id,
        state=InstallState.DOWNLOADING,
        event="resolved",
    )
    original_entries = journal.entries()
    with path.open("ab") as stream:
        stream.write(b'{"sequence":3')

    assert journal.entries() == original_entries
    journal.append(
        transaction_id=transaction_id,
        state=InstallState.VERIFYING,
        event="downloaded_after_restart",
    )
    assert [entry.state for entry in journal.entries()] == [
        InstallState.RESOLVING,
        InstallState.DOWNLOADING,
        InstallState.VERIFYING,
    ]

    lines = path.read_bytes().splitlines()
    tampered = json.loads(lines[0])
    tampered["event"] = "tampered"
    path.write_text(json.dumps(tampered) + "\n" + lines[1].decode() + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruption, match="checksum"):
        journal.entries()


def test_slot_store_rejects_zip_traversal(tmp_path: Path) -> None:
    package = tmp_path / "malicious.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside.txt", "escape")
    manifest, artifact = _storage_manifest(package)
    store = SlotStore(tmp_path / "install")

    with pytest.raises(UnsafePackage, match="unsafe"):
        store.stage(
            package,
            slot_id="v1.0.0-malicious",
            manifest=manifest,
            artifact=artifact,
        )

    assert not (tmp_path / "outside.txt").exists()


def test_slot_store_rejects_casefold_and_unicode_zip_collisions(tmp_path: Path) -> None:
    package = tmp_path / "collision.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Runtime/App.py", "first")
        archive.writestr("runtime/app.py", "second")
    manifest, artifact = _storage_manifest(package)

    with pytest.raises(UnsafePackage, match="collision"):
        SlotStore(tmp_path / "install").stage(
            package,
            slot_id="v1-collision",
            manifest=manifest,
            artifact=artifact,
        )


def test_slot_store_rejects_windows_device_names_inside_zip(tmp_path: Path) -> None:
    package = tmp_path / "device.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("runtime/CON.txt", "unsafe")
    manifest, artifact = _storage_manifest(package)

    with pytest.raises(UnsafePackage, match="unsafe"):
        SlotStore(tmp_path / "install").stage(
            package,
            slot_id="v1-device",
            manifest=manifest,
            artifact=artifact,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode assertion")
def test_slot_store_preserves_safe_macos_executable_modes(tmp_path: Path) -> None:
    package = tmp_path / "runtime.zip"
    executable = zipfile.ZipInfo("runtime/bin/ecorex")
    executable.create_system = 3
    executable.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(executable, b"runtime")
    manifest, artifact = _storage_manifest(package)
    slot = SlotStore(tmp_path / "install").stage(
        package,
        slot_id="v1-executable",
        manifest=manifest,
        artifact=artifact,
    )

    assert stat.S_IMODE((slot / "payload/runtime/bin/ecorex").stat().st_mode) == 0o755


def test_slot_store_rejects_preexisting_slot_links_and_prune_never_follows_aliases(
    tmp_path: Path,
) -> None:
    package = tmp_path / "runtime.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("runtime/app.txt", "safe")
    manifest, artifact = _storage_manifest(package)
    store = SlotStore(tmp_path / "install")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".slot.json").write_text("{}")
    target = store.slot_path("v1-linked")
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(Exception, match="real directory"):
        store.stage(
            package,
            slot_id="v1-linked",
            manifest=manifest,
            artifact=artifact,
        )
    target.unlink()

    current = store.stage(
        package,
        slot_id="v1-current",
        manifest=manifest,
        artifact=artifact,
    )
    store.switch_to("v1-current")
    store.mark_known_good("v1-current")
    aliases = [store.slot_path("alias-one"), store.slot_path("alias-two")]
    for alias in aliases:
        alias.symlink_to(current, target_is_directory=True)
    store.prune(max_slots=2)

    assert current.is_dir()
    assert (current / "payload/runtime/app.txt").read_text() == "safe"


def test_slot_store_orphan_cleanup_runs_security_convergence_before_removal(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "install")
    orphan = store.slots_dir / ".v1-orphan.staging-crash"
    (orphan / "payload").mkdir(parents=True)
    calls: list[Path] = []

    removed = store.cleanup_staging_orphans(
        before_remove=lambda path: calls.append(path)
    )

    assert removed == (orphan.name,)
    assert calls == [orphan]
    assert not orphan.exists()


def test_slot_store_keeps_orphan_when_security_convergence_fails(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "install")
    orphan = store.slots_dir / ".v1-orphan.staging-crash"
    (orphan / "payload").mkdir(parents=True)

    def fail(_path: Path) -> None:
        raise RuntimeError("native cleanup unavailable")

    with pytest.raises(RuntimeError, match="native cleanup unavailable"):
        store.cleanup_staging_orphans(before_remove=fail)

    assert orphan.is_dir()


def test_local_fetcher_refuses_oversized_source_before_writing(tmp_path: Path) -> None:
    package = tmp_path / "expected.zip"
    package.write_bytes(b"expected")
    manifest, artifact = _storage_manifest(package)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / artifact.file_name).write_bytes(b"unexpected oversized payload")
    destination = tmp_path / "download.part"

    with pytest.raises(FetchError, match="size"):
        LocalSourceFetcher({"cn": source_dir}).fetch(
            manifest.sources[0],
            artifact,
            destination,
            resume_from=0,
            max_bytes=artifact.size_bytes,
        )
    assert not destination.exists()


def test_windows_reparse_attribute_is_treated_as_a_link() -> None:
    class FakeReparsePath:
        def lstat(self):
            return type(
                "Metadata",
                (),
                {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400},
            )()

    assert storage_module._is_link_or_reparse(FakeReparsePath()) is True
