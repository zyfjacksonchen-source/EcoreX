"""Serve the real content-addressed administrator WebUI for browser E2E."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
import uvicorn  # noqa: E402

from ecorex.control_plane.admin_web import create_admin_web_router  # noqa: E402


def create_app() -> FastAPI:
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(create_admin_web_router())

    @app.get("/__admin_e2e/ready", include_in_schema=False)
    def ready() -> dict[str, bool]:
        return {"ready": True}

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4180)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port must be between 1024 and 65535")
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
