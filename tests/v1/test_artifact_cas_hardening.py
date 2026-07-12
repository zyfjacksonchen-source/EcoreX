from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib

import pytest

from ecorex.artifacts import ContentAddressedStore, ContentIntegrityError


def _put_from_process(root: str, payload: bytes) -> tuple[str, int]:
    blob = ContentAddressedStore(root).put_bytes(payload)
    return blob.sha256, blob.size_bytes


def test_cas_is_atomic_across_independent_store_instances(tmp_path):
    root = tmp_path / "cas"
    stores = [ContentAddressedStore(root) for _ in range(8)]
    payload = b"same cross-instance content" * 20_000

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        blobs = list(pool.map(lambda store: store.put_bytes(payload), stores))

    assert {blob.sha256 for blob in blobs} == {hashlib.sha256(payload).hexdigest()}
    files = [path for path in root.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert not any(path.name.startswith(".ecorex-cas-") for path in files)


def test_cas_is_atomic_across_processes(tmp_path):
    root = tmp_path / "cas"
    payload = b"same cross-process content" * 20_000

    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_put_from_process, [str(root)] * 8, [payload] * 8))

    assert {result[0] for result in results} == {hashlib.sha256(payload).hexdigest()}
    assert {result[1] for result in results} == {len(payload)}
    files = [path for path in root.rglob("*") if path.is_file()]
    assert len(files) == 1


def test_verified_open_rejects_tampered_content(tmp_path):
    store = ContentAddressedStore(tmp_path / "cas")
    blob = store.put_bytes(b"trusted")
    blob.path.write_bytes(b"tampered")

    with pytest.raises(ContentIntegrityError):
        store.open(blob.sha256)
