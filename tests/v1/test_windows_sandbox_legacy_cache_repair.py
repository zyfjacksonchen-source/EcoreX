from __future__ import annotations

from pathlib import Path

import pytest

from ecorex.integration.windows_sandbox_security import (
    WindowsSandboxSecurityError,
    _repair_legacy_windows_cache_pollution,
)


def _cache(payload: Path) -> Path:
    return (
        payload
        / "%SystemDrive%"
        / "ProgramData"
        / "Microsoft"
        / "Windows"
        / "Caches"
    )


def test_repairs_only_known_relative_windows_cache_projection(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    cache = _cache(payload)
    cache.mkdir(parents=True)
    (payload / "runtime.bin").write_bytes(b"signed")
    (cache / "cversions.2.db").write_bytes(b"cache")
    (cache / "{6AF0698E-D558-4F6E-9B3C-3716689AF493}.2.ver0x0001.db").write_bytes(
        b"cache"
    )

    assert _repair_legacy_windows_cache_pollution(payload) is True
    assert not (payload / "%SystemDrive%").exists()
    assert (payload / "runtime.bin").read_bytes() == b"signed"
    assert _repair_legacy_windows_cache_pollution(payload) is False


@pytest.mark.parametrize(
    "unexpected",
    ["notes.txt", "cversions.db", "{not-a-guid}.2.ver0x1.db"],
)
def test_rejects_unknown_residue_without_deleting_it(
    tmp_path: Path, unexpected: str
) -> None:
    payload = tmp_path / "payload"
    cache = _cache(payload)
    cache.mkdir(parents=True)
    target = cache / unexpected
    target.write_bytes(b"keep-for-diagnosis")

    with pytest.raises(
        WindowsSandboxSecurityError,
        match="unexpected shape",
    ):
        _repair_legacy_windows_cache_pollution(payload)

    assert target.read_bytes() == b"keep-for-diagnosis"
