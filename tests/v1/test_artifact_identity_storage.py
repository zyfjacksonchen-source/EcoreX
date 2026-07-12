from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from ecorex.artifacts import (
    ContentAddressedStore,
    ContentIntegrityError,
    ArtifactService,
    minute_display_name,
    sanitize_display_filename,
)
from ecorex.ids import is_id


FIXED_NOW = datetime(2026, 7, 10, 15, 34, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (r"C:\private\CON.txt", "_CON.txt"),
        ("aux.pdf", "_aux.pdf"),
        ("LPT9", "_LPT9"),
        ("sales:Q3?.pdf. ", "sales_Q3_.pdf"),
        ("../", "未命名"),
    ],
)
def test_cross_platform_filename_sanitization_and_windows_reserved_names(requested, expected):
    assert sanitize_display_filename(requested) == expected


def test_minute_display_name_is_human_readable_but_not_an_identity():
    assert minute_display_name("市场报告.pdf", FIXED_NOW, 1) == "市场报告_20260710-1534_01.pdf"
    assert minute_display_name("市场报告.pdf", FIXED_NOW, 100) == "市场报告_20260710-1534_100.pdf"


def test_one_hundred_artifacts_in_same_minute_have_unique_opaque_ids_and_names(tmp_path):
    service = ArtifactService(tmp_path, clock=lambda: FIXED_NOW)

    def create(index: int):
        payload = f"pdf-{index}".encode()
        return service.create_artifact(
            payload, requested_name="市场报告.pdf", mime_type="application/pdf"
        ), payload

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(create, range(100)))

    projections = [result[0] for result in results]
    assert len({item.artifact_id for item in projections}) == 100
    assert len({item.revision_id for item in projections}) == 100
    assert len({item.display_name.casefold() for item in projections}) == 100
    assert all(is_id(item.artifact_id, "art") for item in projections)
    assert all(is_id(item.revision_id, "rev") for item in projections)
    assert len(service.list_user_artifacts()) == 100
    for projection, payload in results:
        assert projection.sha256 == hashlib.sha256(payload).hexdigest()
        assert service.read_user_content(projection.artifact_id) == payload


def test_cas_concurrent_deduplication_is_atomic_and_detects_tampering(tmp_path):
    store = ContentAddressedStore(tmp_path / "cas")
    payload = b"same immutable content" * 1000

    with ThreadPoolExecutor(max_workers=16) as pool:
        blobs = list(pool.map(lambda _: store.put_bytes(payload), range(64)))

    assert {blob.sha256 for blob in blobs} == {hashlib.sha256(payload).hexdigest()}
    files = [path for path in (tmp_path / "cas").rglob("*") if path.is_file()]
    assert len(files) == 1
    assert not any(path.name.startswith(".ecorex-cas-") for path in files)
    files[0].write_bytes(b"tampered")
    with pytest.raises(ContentIntegrityError):
        store.read_bytes(blobs[0].sha256)
