"""Packaged EcoreX v1 Product Runtime command line."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
import sys
import zoneinfo

from fastapi import FastAPI
import uvicorn

from ecorex.session import ManagedSessionError
from ecorex.startup_diagnostics import write_runtime_startup_diagnostic

from .activation import create_activation_probe_app
from .app import create_product_app
from .config import (
    ActivationProbeComposition,
    ProductRuntimeComposition,
    ProductRuntimeConfigurationError,
    ProductRuntimeTrustError,
    load_product_runtime,
)
from .errors import BundleIntegrityError, ServerConfigurationError
from .launcher import build_uvicorn_config
from .pack_resolver import create_production_pack_adapter_resolver


class ProductRuntimeExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION = 64
    SOFTWARE = 70
    TRUST_FAILURE = 78


@dataclass(frozen=True, slots=True)
class ProductRuntimeServer:
    composition: ProductRuntimeComposition | ActivationProbeComposition
    app: FastAPI
    uvicorn_config: uvicorn.Config


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        raise SystemExit(2)


def _load_product_runtime_for_cli(**kwargs) -> ProductRuntimeComposition:
    """Production CLI always supplies executable signed-pack adapters."""

    return load_product_runtime(
        pack_adapter_resolver=create_production_pack_adapter_resolver(),
        **kwargs,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="ecorex", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve",
        help="run the signed local Product Runtime",
        add_help=True,
    )
    # This is intentionally the entire packaged process contract.  In
    # particular there is no token, bearer, API key, config path or install
    # root argument that could leak through process listings.
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def build_product_runtime_server(
    *,
    host: str,
    port: int,
    runtime_loader: Callable[..., ProductRuntimeComposition] = _load_product_runtime_for_cli,
) -> ProductRuntimeServer:
    # The native launcher uses Python isolated mode, which intentionally
    # ignores PYTHON* environment variables. Reset the process authority
    # explicitly so Windows and macOS both resolve named zones from the
    # signed tzdata distribution carried by Core, never mutable host files.
    zoneinfo.reset_tzpath(())
    zoneinfo.ZoneInfo.clear_cache()
    try:
        composition = runtime_loader(host=host, port=port)
    except ProductRuntimeConfigurationError:
        # The Runtime loader already owns a more precise, fixed diagnostic
        # stage. Preserve it without exposing the native exception text.
        raise
    except (ServerConfigurationError, ValueError):
        raise ProductRuntimeConfigurationError(
            "Product Runtime composition is invalid",
            stage_code="runtime_composition",
        ) from None
    try:
        app = (
            create_activation_probe_app(composition.server_settings)
            if isinstance(composition, ActivationProbeComposition)
            else create_product_app(composition.server_settings)
        )
        app.state.product_runtime_composition = composition
    except BundleIntegrityError:
        composition.close_unstarted()
        raise
    except (ServerConfigurationError, RuntimeError, ValueError):
        composition.close_unstarted()
        raise ProductRuntimeConfigurationError(
            "Product Runtime application composition is invalid",
            stage_code="application_composition",
        ) from None
    except BaseException:
        composition.close_unstarted()
        raise
    try:
        uvicorn_config = build_uvicorn_config(app, composition.server_settings)
    except (ServerConfigurationError, ValueError):
        composition.close_unstarted()
        raise ProductRuntimeConfigurationError(
            "Product Runtime HTTP server configuration is invalid",
            stage_code="http_server_configuration",
        ) from None
    except BaseException:
        composition.close_unstarted()
        raise
    # Transfer resource ownership only after every synchronous startup layer
    # has been composed. A Uvicorn configuration failure must not orphan the
    # already-created managed transports.
    composition.transfer_to_app()
    return ProductRuntimeServer(
        composition=composition,
        app=app,
        uvicorn_config=uvicorn_config,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "serve":
        # argparse currently makes this unreachable; keep the boundary closed
        # if commands are added without a product implementation later.
        print("EcoreX Product Runtime command is unsupported.", file=sys.stderr)
        return int(ProductRuntimeExitCode.CONFIGURATION)
    try:
        server = build_product_runtime_server(host=args.host, port=args.port)
        uvicorn.Server(server.uvicorn_config).run()
        return int(ProductRuntimeExitCode.SUCCESS)
    except (ProductRuntimeTrustError, BundleIntegrityError):
        write_runtime_startup_diagnostic("trust_boundary")
        print("EcoreX refused an untrusted Product Runtime slot.", file=sys.stderr)
        return int(ProductRuntimeExitCode.TRUST_FAILURE)
    except ManagedSessionError:
        write_runtime_startup_diagnostic("managed_session")
        print("EcoreX Product Runtime configuration is invalid.", file=sys.stderr)
        print("EcoreX startup stage: managed_session", file=sys.stderr)
        return int(ProductRuntimeExitCode.CONFIGURATION)
    except ProductRuntimeConfigurationError as exc:
        stage = exc.stage_code or "configuration"
        write_runtime_startup_diagnostic(stage)
        print("EcoreX Product Runtime configuration is invalid.", file=sys.stderr)
        # Only a validated fixed stage code crosses the process boundary; the
        # error message and native cause remain private because providers may
        # include credentials or paths in exception text.
        print(
            f"EcoreX startup stage: {stage}",
            file=sys.stderr,
        )
        return int(ProductRuntimeExitCode.CONFIGURATION)
    except (ServerConfigurationError, ValueError):
        write_runtime_startup_diagnostic("server_configuration")
        print("EcoreX Product Runtime configuration is invalid.", file=sys.stderr)
        print("EcoreX startup stage: server_configuration", file=sys.stderr)
        return int(ProductRuntimeExitCode.CONFIGURATION)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        # The ASGI application and Uvicorn Config have already composed. A
        # remaining Uvicorn SystemExit is the native listener boundary (most
        # commonly an occupied port), not an unknown application crash.
        write_runtime_startup_diagnostic("http_server_bind")
        print("EcoreX Product Runtime configuration is invalid.", file=sys.stderr)
        print("EcoreX startup stage: http_server_bind", file=sys.stderr)
        return int(ProductRuntimeExitCode.CONFIGURATION)
    except Exception:
        write_runtime_startup_diagnostic("software")
        # Do not render exception values: transports and platform vaults may
        # include sensitive implementation details in their native errors.
        print("EcoreX Product Runtime could not start.", file=sys.stderr)
        return int(ProductRuntimeExitCode.SOFTWARE)


__all__ = [
    "ProductRuntimeExitCode",
    "ProductRuntimeServer",
    "build_product_runtime_server",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
