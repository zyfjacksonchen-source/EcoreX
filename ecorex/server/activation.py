"""Minimal probe-only ASGI app for a provisional signed Runtime slot."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import ipaddress
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ecorex.update import (
    ACTIVATION_HEALTH_PATH,
    ACTIVATION_NONCE_HEADER,
    ActivationHealthIdentity,
    activation_health_response,
)

from .errors import ServerConfigurationError


@dataclass(frozen=True, slots=True)
class ActivationProbeSettings:
    host: str
    port: int
    identity: ActivationHealthIdentity
    nonce: str = field(repr=False, compare=False)
    parent_poll_seconds: float = 0.2
    watchdog_seconds: float = 90.0
    exit_process: Callable[[int], object] = field(
        default=os._exit, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ServerConfigurationError("activation probe host must be loopback") from error
        if not address.is_loopback:
            raise ServerConfigurationError("activation probe host must be loopback")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ServerConfigurationError("activation probe port is invalid")
        try:
            self.identity.proof(self.nonce)
        except Exception as error:
            raise ServerConfigurationError("activation probe nonce is invalid") from error
        if not 0.05 <= self.parent_poll_seconds <= 1.0:
            raise ServerConfigurationError("activation parent poll interval is invalid")
        if not 5.0 <= self.watchdog_seconds <= 120.0:
            raise ServerConfigurationError("activation probe watchdog is invalid")
        if not callable(self.exit_process):
            raise ServerConfigurationError("activation probe exit hook is invalid")

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == 80 else f"{host}:{self.port}"


def create_activation_probe_app(settings: ActivationProbeSettings) -> FastAPI:
    parent_pid = os.getppid()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def parent_watchdog() -> None:
            loop = asyncio.get_running_loop()
            started = loop.time()
            while True:
                await asyncio.sleep(settings.parent_poll_seconds)
                if (
                    parent_pid <= 1
                    or os.getppid() != parent_pid
                    or loop.time() - started >= settings.watchdog_seconds
                ):
                    settings.exit_process(70)
                    return

        task = asyncio.create_task(
            parent_watchdog(), name="ecorex-activation-parent-watchdog"
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="EcoreX activation probe",
        version=settings.identity.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def activation_boundary(request: Request, call_next):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or hosts[0].casefold() != settings.authority.casefold():
            response = JSONResponse(
                status_code=400,
                content={"detail": "invalid Host header"},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get(ACTIVATION_HEALTH_PATH, response_model=None)
    async def activation_health(request: Request):
        supplied = request.headers.get(ACTIVATION_NONCE_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, settings.nonce):
            return JSONResponse(
                status_code=403,
                content={"detail": "activation health proof is required"},
            )
        return activation_health_response(settings.identity, settings.nonce)

    @app.api_route(
        "/{requested_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def activation_gate(_request: Request, requested_path: str):
        del requested_path
        return JSONResponse(
            status_code=503,
            content={
                "detail": "candidate activation health is not confirmed",
                "code": "activation_health_pending",
            },
            headers={"Retry-After": "1"},
        )

    return app


__all__ = ["ActivationProbeSettings", "create_activation_probe_app"]
