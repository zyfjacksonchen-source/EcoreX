"""Process-local bridge from legacy Skill readers to ExtensionService state.

Legacy ``skills_config.json`` is migration input only.  Runtime discovery must
never treat it as a second mutable enablement authority after v0.3.0 startup.
"""

from __future__ import annotations

import asyncio
import re
import threading
import weakref
from typing import Any

from .models import ExtensionStatus
from .skill_migration import SKILL_ALIASES


_SAFE_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,95}$")
_LOCK = threading.RLock()
_SERVICE: weakref.ReferenceType[Any] | None = None


def bind_live_extension_service(service: Any) -> None:
    """Bind the verified product authority used by in-process legacy readers."""

    if service is None or not callable(getattr(service, "projection", None)):
        raise ValueError("live Extension authority is invalid")
    global _SERVICE
    with _LOCK:
        _SERVICE = weakref.ref(service)


def live_skill_enabled(name: str) -> bool | None:
    """Return current Extension state, or ``None`` when no authority/item exists."""

    with _LOCK:
        service = _SERVICE() if _SERVICE is not None else None
    if service is None:
        return None
    try:
        projection = _skill_projection(service, name)
    except Exception:
        return None
    return projection.status == ExtensionStatus.ENABLED.value


def live_extension_skill_roots() -> tuple[str, ...] | None:
    """Return verified CAS roots for enabled user/Hub Skills.

    Cow owns Skill discovery.  The Extension registry only contributes the
    read-only local package roots which the user explicitly enabled; it does
    not rewrite Skill content or introduce a second prompt/schema path.
    """

    with _LOCK:
        service = _SERVICE() if _SERVICE is not None else None
    if service is None:
        return None
    if service.local_bundle_store is None:
        return ()
    roots: list[str] = []
    try:
        items = service.snapshot().items
    except Exception:
        return None
    for item in items:
        if (
            item.kind != "skill"
            or item.source != "local_bundle"
            or item.status != ExtensionStatus.ENABLED.value
            or item.readiness != "ready"
            or not item.active_digest
        ):
            continue
        try:
            skill_file, _record = service.local_bundle_store.resolve_verified_file(
                item.active_digest, "SKILL.md"
            )
        except Exception:
            continue
        root = str(skill_file.parent)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def live_extension_generation() -> int | None:
    with _LOCK:
        service = _SERVICE() if _SERVICE is not None else None
    if service is None:
        return None
    try:
        return int(service.repository.generation())
    except Exception:
        return None


def set_live_skill_enabled(name: str, enabled: bool) -> bool:
    """Mutate one Skill only through the bound ExtensionService authority."""

    with _LOCK:
        service = _SERVICE() if _SERVICE is not None else None
    if service is None:
        raise RuntimeError("live ExtensionService authority is unavailable")
    projection = _skill_projection(service, name)
    extension_id = projection.extension_id
    desired = bool(enabled)
    if (projection.status == ExtensionStatus.ENABLED.value) is desired:
        return desired
    request_id = (
        f"legacy-skill-bridge:{extension_id}:{projection.revision}:"
        f"{'enable' if desired else 'disable'}"
    )
    if desired:
        _run_async(
            service.enable(
                extension_id,
                expected_revision=projection.revision,
                client_request_id=request_id,
            )
        )
    else:
        service.disable(
            extension_id,
            expected_revision=projection.revision,
            client_request_id=request_id,
        )
    return desired


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[Any] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as error:  # preserve the exact authority failure
            failure.append(error)

    thread = threading.Thread(target=run, name="emate-extension-authority", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0] if result else None


def _extension_id(name: str) -> str | None:
    slug = str(name or "").strip().casefold().replace("_", "-")
    if _SAFE_SLUG.fullmatch(slug) is None:
        return None
    return "skill." + SKILL_ALIASES.get(slug, slug)


def _skill_projection(service: Any, name: str) -> Any:
    extension_id = _extension_id(name)
    if extension_id is not None:
        try:
            return service.projection(extension_id)
        except Exception:
            pass
    normalized = " ".join(str(name or "").casefold().split())
    for item in service.snapshot().items:
        if (
            item.kind == "skill"
            and " ".join(str(item.display_name).casefold().split()) == normalized
        ):
            return item
    raise KeyError(name)


__all__ = [
    "bind_live_extension_service",
    "live_extension_generation",
    "live_extension_skill_roots",
    "live_skill_enabled",
    "set_live_skill_enabled",
]
