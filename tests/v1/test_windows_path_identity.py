from __future__ import annotations

import os
from pathlib import Path

import pytest

from ecorex.integration.windows_path_identity import windows_invariant_path_key
from ecorex.migration.path_security import stable_sha256_file


@pytest.mark.skipif(os.name != "nt", reason="Windows invariant path contract")
@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (r"C:\MiXeD\PATH", r"c:\mixed\path"),
        (r"C:\Straße", r"c:\straße"),
        (r"C:\STRAẞE", r"c:\straẞe"),
        (r"C:\Iİıi", r"c:\iİıi"),
        (r"C:\École\中日韩", r"c:\école\中日韩"),
    ),
)
def test_windows_invariant_path_key_matches_ordinal_filesystem_semantics(
    value: str,
    expected: str,
) -> None:
    assert windows_invariant_path_key(value) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows invariant path contract")
def test_windows_invariant_path_key_does_not_expand_or_normalize_unicode() -> None:
    assert windows_invariant_path_key(r"C:\Straße") != windows_invariant_path_key(
        r"C:\Strasse"
    )
    assert windows_invariant_path_key("C:\\É") != windows_invariant_path_key(
        "C:\\E\u0301"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows opened-handle mode contract")
def test_windows_stable_hash_accepts_cmd_path_handle_mode_difference(
    tmp_path: Path,
) -> None:
    command = tmp_path / "corepack.cmd"
    command.write_bytes(b"@echo off\r\n")

    digest, identity = stable_sha256_file(command, label="legacy source file")

    assert digest == "c134b2f85415ba5cfce3e3fe4745688335745a9bb22152ac8f5c77f190d8aee3"
    assert identity.size == 11
