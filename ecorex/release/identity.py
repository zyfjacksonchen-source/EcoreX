"""Pure immutable release/channel identity helpers."""

from __future__ import annotations

import re

from ecorex.update import ReleaseChannel


_SAFE_TAG = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def release_tag(
    version: str,
    channel: ReleaseChannel,
    *,
    release_id: str | None = None,
) -> str:
    """Return the immutable GitHub tag namespace for one release channel."""

    if not isinstance(channel, ReleaseChannel):
        try:
            channel = ReleaseChannel(channel)
        except (TypeError, ValueError):
            raise ValueError("release channel is invalid") from None
    if channel is ReleaseChannel.CANARY:
        match = re.fullmatch(r"release-canary-([0-9a-f]{24})", release_id or "")
        if match is None:
            raise ValueError("canary release identity is invalid")
        tag = f"v{version}-canary-{match.group(1)}"
    else:
        tag = f"v{version}"
    if _SAFE_TAG.fullmatch(tag) is None:
        raise ValueError("release version is not valid SemVer")
    return tag


__all__ = ["release_tag"]
