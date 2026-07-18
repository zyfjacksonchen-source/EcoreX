"""Channel-bound source requirements for release publication receipts.

The signed release manifest always lists the complete client failover order.
Publication admission is deliberately narrower: a Stable release is admitted
once its domestic primary source is verified, while Canary remains an
all-origin consistency exercise.  The policy is derived only from signed
manifest fields; it is never supplied by an operator flag or an unsigned
receipt.
"""

from __future__ import annotations

from ecorex.update import ReleaseChannel, ReleaseManifest, ReleaseSource, SourceKind


STABLE_PRIMARY_ONLY_POLICY = "stable-primary-only"
ALL_SOURCES_POLICY = "all-sources"


class PublicationPolicyError(ValueError):
    """The signed manifest cannot satisfy the channel publication policy."""


def publication_receipt_policy(manifest: ReleaseManifest) -> str:
    """Return the only receipt policy permitted by a signed manifest."""

    return (
        STABLE_PRIMARY_ONLY_POLICY
        if manifest.channel is ReleaseChannel.STABLE
        else ALL_SOURCES_POLICY
    )


def required_publication_sources(
    manifest: ReleaseManifest,
) -> tuple[ReleaseSource, ...]:
    """Return sources that must be readable before this channel is admitted.

    The first manifest source remains a hard requirement for Stable.  It must
    be the domestic mirror, so relaxing the fallback sources never silently
    changes which origin is trusted for the initial public release.
    """

    sources = tuple(manifest.sources)
    if not sources:
        raise PublicationPolicyError("publication source list is empty")
    if manifest.channel is ReleaseChannel.STABLE:
        primary = sources[0]
        if (
            primary.kind is not SourceKind.GITHUB_CN_MIRROR
            or primary.priority != 0
        ):
            raise PublicationPolicyError(
                "stable publication primary source must be the domestic mirror"
            )
        return (primary,)
    return sources


def required_publication_source_ids(manifest: ReleaseManifest) -> frozenset[str]:
    """Return the exact source identities required in a receipt."""

    return frozenset(source.source_id for source in required_publication_sources(manifest))


__all__ = [
    "ALL_SOURCES_POLICY",
    "PublicationPolicyError",
    "STABLE_PRIMARY_ONLY_POLICY",
    "publication_receipt_policy",
    "required_publication_source_ids",
    "required_publication_sources",
]
