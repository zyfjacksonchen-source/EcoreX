"""Read-only product API for system health and bounded metric history."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .system import SystemObservabilityService


SystemHealthStatus = Literal["healthy", "degraded", "attention", "critical"]


def _validate_bounded_json(value: JsonValue, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("system metrics exceed the nesting limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("system metrics must contain finite numbers")
        return
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("system metric strings exceed the size limit")
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise ValueError("system metric arrays exceed the size limit")
        for item in value:
            _validate_bounded_json(item, depth=depth + 1)
        return
    if len(value) > 128 or any(not key or len(key) > 64 for key in value):
        raise ValueError("system metric objects exceed the size limit")
    for item in value.values():
        _validate_bounded_json(item, depth=depth + 1)


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SystemHealthComponentResponse(_StrictResponseModel):
    component_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    status: SystemHealthStatus
    message: str = Field(min_length=1, max_length=512)


class SystemHealthPublicResponse(_StrictResponseModel):
    sample_id: str = Field(min_length=1, max_length=256)
    overall: SystemHealthStatus
    summary: str = Field(min_length=1, max_length=512)
    components: list[SystemHealthComponentResponse] = Field(min_length=1, max_length=16)
    sampled_at: datetime = Field(strict=False)

    @model_validator(mode="after")
    def validate_health_summary(self) -> "SystemHealthPublicResponse":
        if self.sampled_at.tzinfo is None:
            raise ValueError("system health sampled_at must be timezone-aware")
        identities = [item.component_id for item in self.components]
        if len(set(identities)) != len(identities):
            raise ValueError("system health component identities must be unique")
        order = {"healthy": 0, "degraded": 1, "attention": 2, "critical": 3}
        expected = max((item.status for item in self.components), key=order.__getitem__)
        if self.overall != expected:
            raise ValueError("system health overall status is inconsistent")
        return self


class SystemHealthTechnicalResponse(SystemHealthPublicResponse):
    metrics: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_metric_groups(self) -> "SystemHealthTechnicalResponse":
        if set(self.metrics) != {"runtime", "process", "storage", "services"}:
            raise ValueError("system health metric groups are inconsistent")
        if not all(isinstance(group, dict) for group in self.metrics.values()):
            raise ValueError("system health metric groups must be objects")
        _validate_bounded_json(self.metrics)
        return self


class SystemMetricHistoryResponse(_StrictResponseModel):
    items: list[SystemHealthTechnicalResponse] = Field(max_length=200)


def create_system_observability_router(
    service: SystemObservabilityService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system-observability"])

    @router.get(
        "/health",
        response_model=SystemHealthPublicResponse | SystemHealthTechnicalResponse,
    )
    def health(
        technical: bool = False,
    ) -> SystemHealthPublicResponse | SystemHealthTechnicalResponse:
        sample = service.latest()
        assert sample is not None
        payload = sample.to_dict(technical=technical)
        if technical:
            return SystemHealthTechnicalResponse.model_validate(payload)
        return SystemHealthPublicResponse.model_validate(payload)

    @router.get("/metrics", response_model=SystemMetricHistoryResponse)
    def metrics(
        limit: int = Query(default=60, ge=1, le=200),
    ) -> SystemMetricHistoryResponse:
        return SystemMetricHistoryResponse.model_validate(
            {
                "items": [
                    item.to_dict(technical=True)
                    for item in service.history(limit=limit)
                ]
            }
        )

    return router


__all__ = [
    "SystemHealthComponentResponse",
    "SystemHealthPublicResponse",
    "SystemHealthTechnicalResponse",
    "SystemMetricHistoryResponse",
    "create_system_observability_router",
]
