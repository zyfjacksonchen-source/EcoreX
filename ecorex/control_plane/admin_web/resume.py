"""Mountable, read-only admin workspace resume endpoint."""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, Response

from .contracts import AdminResumeProvider, ResumeStateProjection


_SAFE_PREFIX = re.compile(r"^/[A-Za-z0-9](?:[A-Za-z0-9_/-]*[A-Za-z0-9_-])?$")


def create_admin_resume_router(
    provider: AdminResumeProvider,
    *,
    authorization_dependency: Callable[..., Any],
    prefix: str = "/api/v1/admin",
) -> APIRouter:
    """Create a read-only router behind the host app's admin authorization.

    The host must pass the same dependency used for release-admin mutations;
    omitting authorization is intentionally not supported.
    """

    if not callable(getattr(provider, "resume_state", None)):
        raise TypeError("admin resume provider must expose resume_state()")
    if not callable(authorization_dependency):
        raise TypeError("admin resume authorization dependency is required")
    if not _SAFE_PREFIX.fullmatch(prefix) or "//" in prefix:
        raise ValueError("admin resume router prefix is invalid")

    router = APIRouter(prefix=prefix)

    @router.get("/resume", response_model=ResumeStateProjection)
    def resume_state(
        response: Response,
        _authorized: Any = Depends(authorization_dependency),
    ) -> ResumeStateProjection:
        del _authorized
        projection = ResumeStateProjection.model_validate(provider.resume_state())
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return projection

    return router


__all__ = ["create_admin_resume_router"]
