"""Read-only product API for system health and bounded metric history."""

from fastapi import APIRouter, Query

from .system import SystemObservabilityService


def create_system_observability_router(service: SystemObservabilityService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system-observability"])

    @router.get("/health")
    def health(technical: bool = False) -> dict:
        sample = service.latest()
        assert sample is not None
        return sample.to_dict(technical=technical)

    @router.get("/metrics")
    def metrics(limit: int = Query(default=60, ge=1, le=200)) -> dict:
        return {
            "items": [item.to_dict(technical=True) for item in service.history(limit=limit)]
        }

    return router


__all__ = ["create_system_observability_router"]
