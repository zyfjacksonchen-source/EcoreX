"""Single fail-closed Product resolver for every signed Capability Pack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
from typing import Any

from ecorex.capabilities import VerifiedCapabilityPack
from ecorex.pack_catalog import CAPABILITY_PACK_SERVICE_IDS
from ecorex.integration.image_tools import production_pack_adapter_resolver as image_resolver
from ecorex.integration.pack_process import ProcessCapabilityPackAdapter
from ecorex.integration.pack_python import PackPythonIdentity, resolve_pack_python
from ecorex.integration.sandbox import (
    MacOSSandboxExecBackend,
    WindowsAppContainerSandboxBackend,
)


PackPythonResolver = Callable[
    ...,
    tuple[Path, PackPythonIdentity],
]


def _resolve_production_pack_adapter(
    pack: VerifiedCapabilityPack,
    workspace_roots: tuple[Path, ...],
    runtime_payload_root: Path,
    *,
    pack_python_resolver: PackPythonResolver,
) -> Mapping[str, Callable[..., Any]]:
    """Resolve only Core-known implementations for a verified signed pack."""

    if pack.manifest.pack_id == "image":
        return image_resolver(pack)
    if pack.manifest.pack_id in {"channels", "ocr", "office"}:
        expected = CAPABILITY_PACK_SERVICE_IDS[pack.manifest.pack_id]
        observed = tuple(binding.service_id for binding in pack.manifest.services)
        if observed != expected or pack.manifest.tools:
            raise ValueError("dependency Capability Pack contract is invalid")
        # Dependency services are consumed by dedicated Runtime components;
        # they do not create synthetic model-callable tools.
        return {}
    if pack.manifest.pack_id in {"browser", "sandbox"}:
        interpreter, identity = pack_python_resolver(
            runtime_payload_root,
            platform=pack.manifest.platform,
            architecture=pack.manifest.architecture,
        )
        sandbox_backend = None
        if pack.manifest.pack_id == "sandbox":
            if pack.manifest.platform == "windows":
                helper = runtime_payload_root / "bin" / "ecorex-sandbox-host.exe"
                marker_path = runtime_payload_root.parent / ".slot.json"
                if marker_path.stat().st_size > 256 * 1024:
                    raise ValueError("slot security receipt is oversized")
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                security_receipt = marker.get("security_provision")
                if not isinstance(security_receipt, Mapping):
                    raise ValueError("slot security receipt is unavailable")
                sandbox_backend = WindowsAppContainerSandboxBackend(
                    helper,
                    expected_sha256=str(security_receipt.get("helper_sha256", "")),
                    security_receipt=security_receipt,
                )
            elif pack.manifest.platform == "macos":
                sandbox_backend = MacOSSandboxExecBackend()
            else:
                raise ValueError("sandbox pack targets an unsupported platform")
        return ProcessCapabilityPackAdapter(
            pack,
            workspace_roots=workspace_roots,
            python_executable=interpreter,
            python_identity=identity,
            sandbox_backend=sandbox_backend,
        ).handlers()
    raise ValueError(
        f"no production adapter exists for pack {pack.manifest.pack_id!r}"
    )


def production_pack_adapter_resolver(
    pack: VerifiedCapabilityPack,
    workspace_roots: tuple[Path, ...],
    runtime_payload_root: Path,
) -> Mapping[str, Callable[..., Any]]:
    """Resolve one Pack with an independent interpreter verification.

    This public function intentionally remains stateless for tests and callers
    that resolve a Pack outside a complete Product Runtime composition.
    """

    return _resolve_production_pack_adapter(
        pack,
        workspace_roots,
        runtime_payload_root,
        pack_python_resolver=resolve_pack_python,
    )


def create_production_pack_adapter_resolver() -> Callable[
    [VerifiedCapabilityPack, tuple[Path, ...], Path],
    Mapping[str, Callable[..., Any]],
]:
    """Create one resolver scoped to exactly one Runtime composition.

    Browser and Sandbox share the same signed relocatable interpreter.  Its
    complete closure is therefore verified once per process startup and reused
    only while the synchronous Pack set is being bound.  A later composition,
    restart or process receives a fresh resolver and performs a fresh scan.
    """

    verified_interpreters: dict[
        tuple[str, str, str],
        tuple[Path, PackPythonIdentity],
    ] = {}

    def cached_pack_python(
        payload_root: Path,
        *,
        platform: str,
        architecture: str,
    ) -> tuple[Path, PackPythonIdentity]:
        key = (
            os.path.normcase(os.path.abspath(os.fspath(payload_root))),
            platform,
            architecture,
        )
        try:
            return verified_interpreters[key]
        except KeyError:
            resolved = resolve_pack_python(
                payload_root,
                platform=platform,
                architecture=architecture,
            )
            verified_interpreters[key] = resolved
            return resolved

    def resolver(
        pack: VerifiedCapabilityPack,
        workspace_roots: tuple[Path, ...],
        runtime_payload_root: Path,
    ) -> Mapping[str, Callable[..., Any]]:
        return _resolve_production_pack_adapter(
            pack,
            workspace_roots,
            runtime_payload_root,
            pack_python_resolver=cached_pack_python,
        )

    setattr(resolver, "_resolve_pack_python_for_composition", cached_pack_python)
    return resolver


__all__ = [
    "create_production_pack_adapter_resolver",
    "production_pack_adapter_resolver",
]
