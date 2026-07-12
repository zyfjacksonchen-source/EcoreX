"""Administrator release console renderer and mountable router."""

from .assets import AdminWebAssetError, AdminWebAssets, VerifiedAdminAsset
from .contracts import (
    AdminResumeAdapter,
    AdminResumeFacts,
    AdminResumeProvider,
    ResumeStateProjection,
)
from .resume import create_admin_resume_router
from .router import create_admin_web_router

__all__ = [
    "AdminResumeAdapter",
    "AdminResumeFacts",
    "AdminResumeProvider",
    "AdminWebAssetError",
    "AdminWebAssets",
    "ResumeStateProjection",
    "VerifiedAdminAsset",
    "create_admin_resume_router",
    "create_admin_web_router",
]
