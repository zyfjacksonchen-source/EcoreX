from __future__ import annotations

import os

import pytest

from ecorex.integration.windows_path_identity import windows_invariant_path_key


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
