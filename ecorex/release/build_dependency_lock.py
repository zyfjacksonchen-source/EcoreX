"""Build-time parsing for exact, marker-active Python lock entries."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .dependency_lock import DependencyLockError


def active_lock_versions(path: Path) -> dict[str, str]:
    entries: list[str] = []
    pending = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise DependencyLockError("dependency_lock_file_unreadable") from None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or (stripped.startswith("#") and not pending):
            continue
        continued = stripped.endswith("\\")
        if continued:
            stripped = stripped[:-1].strip()
        pending = f"{pending} {stripped}".strip()
        if not continued:
            entries.append(pending)
            pending = ""
    if pending or not entries:
        raise DependencyLockError("dependency_lock_file_invalid")
    versions: dict[str, str] = {}
    for entry in entries:
        try:
            requirement = Requirement(entry.split(" --hash=", 1)[0].strip())
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
        except (InvalidRequirement, ValueError):
            raise DependencyLockError("dependency_lock_file_invalid") from None
        specifiers = tuple(requirement.specifier)
        name = canonicalize_name(requirement.name)
        if (
            requirement.url is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or name in versions
        ):
            raise DependencyLockError("dependency_lock_file_invalid")
        versions[name] = specifiers[0].version
    return versions


__all__ = ["active_lock_versions"]
