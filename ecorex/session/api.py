"""Local authenticated WebUI adapter for managed device authorization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from ecorex.protocol import (
    DeviceLoginProjection,
    PollDeviceLoginRequest,
    StartDeviceLoginRequest,
)

from .device import (
    DeviceAuthorizationConflict,
    DeviceAuthorizationNotFound,
    DeviceAuthorizationSupervisor,
    DeviceAuthorizationUnavailable,
    DeviceFlowProjection,
    DeviceFlowStatus,
    ManagedDeviceAuthorizationService,
)


def _project(
    flow: DeviceFlowProjection,
    *,
    restart_scheduled: bool = False,
) -> DeviceLoginProjection:
    return DeviceLoginProjection(
        flow_id=flow.flow_id,
        status=flow.status.value,
        user_code=flow.user_code,
        verification_url=flow.verification_url,
        expires_at=flow.expires_at,
        poll_interval_seconds=flow.poll_interval_seconds,
        next_poll_at=flow.next_poll_at,
        restart_required=flow.restart_required,
        restart_scheduled=restart_scheduled,
        session_generation=flow.session_generation,
        error_code=flow.error_code,
    )


def create_device_authorization_router(
    service: ManagedDeviceAuthorizationService,
    *,
    supervisor: DeviceAuthorizationSupervisor,
    authenticated: Callable[[], bool],
    reload_requester: Callable[[str], bool] | None = None,
) -> APIRouter:
    if not callable(authenticated):
        raise TypeError("device login authentication provider must be callable")
    if reload_requester is not None and not callable(reload_requester):
        raise TypeError("device login reload requester must be callable")
    router = APIRouter(tags=["managed-session"])

    @router.post(
        "/session/device",
        response_model=DeviceLoginProjection,
        status_code=202,
    )
    async def begin_device_login(
        request: StartDeviceLoginRequest,
    ) -> DeviceLoginProjection:
        try:
            already_authenticated = await asyncio.wait_for(
                asyncio.to_thread(authenticated),
                timeout=5,
            )
        except TimeoutError:
            raise HTTPException(
                status_code=503,
                detail={"code": "session_state_unavailable"},
            ) from None
        if already_authenticated:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_already_authenticated",
                    "message": "log out before switching managed accounts",
                },
            )
        try:
            flow = await service.begin(client_request_id=request.client_request_id)
        except DeviceAuthorizationConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.code}) from error
        except DeviceAuthorizationUnavailable as error:
            raise HTTPException(status_code=503, detail={"code": error.code}) from error
        supervisor.notify()
        return _project(flow)

    @router.get(
        "/session/device/{flow_id}",
        response_model=DeviceLoginProjection,
    )
    def get_device_login(flow_id: str) -> DeviceLoginProjection:
        try:
            return _project(service.get(flow_id))
        except DeviceAuthorizationNotFound as error:
            raise HTTPException(status_code=404, detail={"code": error.code}) from error
        except DeviceAuthorizationConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.code}) from error

    @router.post(
        "/session/device/{flow_id}/poll",
        response_model=DeviceLoginProjection,
    )
    async def poll_device_login(
        flow_id: str,
        _request: PollDeviceLoginRequest,
    ) -> DeviceLoginProjection:
        try:
            flow = await service.poll_once(flow_id)
        except DeviceAuthorizationNotFound as error:
            raise HTTPException(status_code=404, detail={"code": error.code}) from error
        except DeviceAuthorizationConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.code}) from error
        restart_scheduled = False
        if (
            flow.status is DeviceFlowStatus.AUTHORIZED
            and reload_requester is not None
            and flow.session_generation is not None
        ):
            try:
                restart_scheduled = bool(
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            reload_requester,
                            f"session-login:{flow.session_generation}",
                        ),
                        timeout=5,
                    )
                )
            except TimeoutError:
                restart_scheduled = False
        return _project(flow, restart_scheduled=restart_scheduled)

    return router


__all__ = ["create_device_authorization_router"]
