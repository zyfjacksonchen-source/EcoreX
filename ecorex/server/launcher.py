"""Uvicorn launcher constrained to a configured loopback interface."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from .activation import ActivationProbeSettings
from .app import ProductServerSettings, create_product_app


def build_uvicorn_config(
    app: FastAPI,
    settings: ProductServerSettings | ActivationProbeSettings,
) -> uvicorn.Config:
    # ProductServerSettings has already rejected wildcard/non-loopback hosts.
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        date_header=True,
        # Runtime bearer credentials are injected into HTML and must never be
        # exposed by request-target/access logging in production.
        access_log=False,
        log_level="info",
    )


def run(settings: ProductServerSettings) -> None:
    app = create_product_app(settings)
    uvicorn.Server(build_uvicorn_config(app, settings)).run()
