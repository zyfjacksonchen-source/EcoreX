"""Product-version primitives shared by release producers and consumers."""

from __future__ import annotations

import re


_FINAL_PRODUCT_VERSION = re.compile(
    r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$"
)


def stable_release_sequence(version: str) -> int:
    """Map one final product SemVer to its monotonic anti-rollback sequence."""

    match = (
        _FINAL_PRODUCT_VERSION.fullmatch(version)
        if isinstance(version, str)
        else None
    )
    if match is None:
        raise ValueError("stable release version must be a final product SemVer")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    # Keep 0.3.0 above the historical v1.0.17 sequence (18), while preserving
    # normal SemVer ordering for all subsequent product releases.
    return major * 100_000_000 + minor * 10_000 + patch + 1
