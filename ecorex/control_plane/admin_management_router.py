"""Authenticated HTTP boundary for the product administrator workspace."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .management import (
    AdminManagementRepository,
    ModelConnectionTester,
    ModelConnectionTestResult,
    ModelTestLease,
)
from .management_models import (
    AdjustUsageRequest,
    AdminUserListProjection,
    AdminUserProjection,
    CreateAdminUserRequest,
    CreateModelConfigurationRequest,
    ModelConfigurationProjection,
    ModelTestProjection,
    StageModelConfigurationRequest,
    TestAndActivateModelRequest,
    UpdateAdminUserRequest,
    UsageSummaryProjection,
)
from .models import ControlPrincipal


def create_admin_management_router(
    repository: AdminManagementRepository,
    *,
    model_tester: ModelConnectionTester,
    user_admin_dependency: Callable[..., ControlPrincipal],
    model_admin_dependency: Callable[..., ControlPrincipal],
    prefix: str = "/api/v1/admin",
) -> APIRouter:
    if not isinstance(repository, AdminManagementRepository):
        raise TypeError("admin management repository is required")
    if not isinstance(model_tester, ModelConnectionTester):
        raise TypeError("model connection tester is required")
    if not callable(user_admin_dependency) or not callable(model_admin_dependency):
        raise TypeError("administrator authorization dependencies are required")
    router = APIRouter(prefix=prefix)

    @router.get("/users", response_model=AdminUserListProjection)
    async def list_users(
        query: str | None = Query(default=None, max_length=128),
        status: str | None = Query(default=None),
        organization_id: str | None = Query(default=None, max_length=128),
        offset: int = Query(default=0, ge=0, le=10**9),
        limit: int = Query(default=50, ge=1, le=200),
        _current: ControlPrincipal = Depends(user_admin_dependency),
    ) -> AdminUserListProjection:
        return await asyncio.to_thread(
            repository.list_users,
            query=query,
            status=status,
            organization_id=organization_id,
            offset=offset,
            limit=limit,
        )

    @router.post("/users", response_model=AdminUserProjection, status_code=201)
    async def create_user(
        request: CreateAdminUserRequest,
        current: ControlPrincipal = Depends(user_admin_dependency),
    ) -> AdminUserProjection:
        if request.password is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "initial_password_required",
                    "message": "an initial password is required",
                },
            )
        return await asyncio.to_thread(repository.create_user, request, actor=current)

    @router.put("/users/{account_id}", response_model=AdminUserProjection)
    async def update_user(
        account_id: str,
        request: UpdateAdminUserRequest,
        current: ControlPrincipal = Depends(user_admin_dependency),
    ) -> AdminUserProjection:
        return await asyncio.to_thread(
            repository.update_user, account_id, request, actor=current
        )

    @router.post(
        "/users/{account_id}/usage-adjustments",
        response_model=AdminUserProjection,
    )
    async def adjust_usage(
        account_id: str,
        request: AdjustUsageRequest,
        current: ControlPrincipal = Depends(user_admin_dependency),
    ) -> AdminUserProjection:
        return await asyncio.to_thread(
            repository.adjust_usage, account_id, request, actor=current
        )

    @router.get("/usage/summary", response_model=UsageSummaryProjection)
    async def usage_summary(
        _current: ControlPrincipal = Depends(user_admin_dependency),
    ) -> UsageSummaryProjection:
        return await asyncio.to_thread(repository.usage_summary)

    @router.get(
        "/models", response_model=list[ModelConfigurationProjection]
    )
    async def list_models(
        _current: ControlPrincipal = Depends(model_admin_dependency),
    ) -> list[ModelConfigurationProjection]:
        return await asyncio.to_thread(repository.list_model_configurations)

    @router.post(
        "/models", response_model=ModelConfigurationProjection, status_code=201
    )
    async def create_model(
        request: CreateModelConfigurationRequest,
        current: ControlPrincipal = Depends(model_admin_dependency),
    ) -> ModelConfigurationProjection:
        return await asyncio.to_thread(
            repository.create_model_configuration, request, actor=current
        )

    @router.put(
        "/models/{config_id}/draft", response_model=ModelConfigurationProjection
    )
    async def stage_model(
        config_id: str,
        request: StageModelConfigurationRequest,
        current: ControlPrincipal = Depends(model_admin_dependency),
    ) -> ModelConfigurationProjection:
        return await asyncio.to_thread(
            repository.stage_model_configuration,
            config_id,
            request,
            actor=current,
        )

    @router.post(
        "/models/{config_id}/test-and-activate",
        response_model=ModelTestProjection,
    )
    async def test_and_activate_model(
        config_id: str,
        request: TestAndActivateModelRequest,
        current: ControlPrincipal = Depends(model_admin_dependency),
    ) -> ModelTestProjection:
        started = await asyncio.to_thread(
            repository.begin_model_test,
            config_id,
            request.revision,
            actor=current,
            client_request_id=request.client_request_id,
        )
        if isinstance(started, ModelTestProjection):
            return started
        if not isinstance(started, ModelTestLease):  # pragma: no cover - closed boundary
            raise RuntimeError("model test lease contract is invalid")
        try:
            result = await model_tester.test(started.configuration)
            if not isinstance(result, ModelConnectionTestResult):
                result = ModelConnectionTestResult(
                    passed=False, error_code="provider_test_invalid"
                )
        except asyncio.CancelledError:
            result = ModelConnectionTestResult(
                passed=False, error_code="provider_test_cancelled"
            )
            await asyncio.shield(
                asyncio.to_thread(
                    repository.finish_model_test,
                    started,
                    result,
                    actor=current,
                )
            )
            raise
        except Exception:
            result = ModelConnectionTestResult(
                passed=False, error_code="provider_test_unavailable"
            )
        return await asyncio.to_thread(
            repository.finish_model_test, started, result, actor=current
        )

    return router


__all__ = ["create_admin_management_router"]
