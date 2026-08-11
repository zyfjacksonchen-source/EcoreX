"""Dependency-free product identity for the signed v1 Capability Pack set."""

from __future__ import annotations

from types import MappingProxyType


CAPABILITY_PACK_TOOL_IDS = MappingProxyType(
    {
        "browser": ("browser", "web_fetch", "web_search"),
        "channels": (),
        "image": ("imagegen", "vision"),
        "ocr": (),
        "office": (),
        "sandbox": ("bash",),
    }
)
CAPABILITY_PACK_SERVICE_IDS = MappingProxyType(
    {
        "browser": (),
        "channels": ("channels.adapters",),
        "image": (),
        "ocr": ("ocr.extract",),
        "office": ("office.formats",),
        "sandbox": (),
    }
)

_tool_pack_ids = frozenset(CAPABILITY_PACK_TOOL_IDS)
_service_pack_ids = frozenset(CAPABILITY_PACK_SERVICE_IDS)
if _tool_pack_ids != _service_pack_ids:
    raise RuntimeError("Capability Pack tool and service catalogs have drifted")
if any(
    not CAPABILITY_PACK_TOOL_IDS[pack_id]
    and not CAPABILITY_PACK_SERVICE_IDS[pack_id]
    for pack_id in _tool_pack_ids
):
    raise RuntimeError("Capability Pack catalog contains an empty product pack")

# Sorting makes this the single deterministic identity used by bootstrap,
# Runtime configuration, Candidate staging and updater completeness checks.
REQUIRED_CAPABILITY_PACK_IDS = tuple(sorted(_tool_pack_ids))
CAPABILITY_PACK_PROFILES = MappingProxyType(
    {
        # Projection-only and legacy signed slots remain valid without optional
        # Packs; workspace execution adds the reviewed sandbox boundary.
        "minimal": (),
        "workspace": ("sandbox",),
        # Managed offline install: ship the complete reviewed Pack closure.
        "full_offline": REQUIRED_CAPABILITY_PACK_IDS,
    }
)


def capability_pack_profile(pack_ids: tuple[str, ...]) -> str | None:
    """Return the exact product profile; arbitrary partial sets fail closed."""

    return next(
        (
            profile
            for profile, expected in CAPABILITY_PACK_PROFILES.items()
            if pack_ids == expected
        ),
        None,
    )


__all__ = [
    "CAPABILITY_PACK_SERVICE_IDS",
    "CAPABILITY_PACK_TOOL_IDS",
    "CAPABILITY_PACK_PROFILES",
    "REQUIRED_CAPABILITY_PACK_IDS",
    "capability_pack_profile",
]
