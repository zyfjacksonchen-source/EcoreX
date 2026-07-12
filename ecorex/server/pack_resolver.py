"""Single fail-closed Product resolver for every signed Capability Pack."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Any

from ecorex.capabilities import VerifiedCapabilityPack
from ecorex.pack_catalog import CAPABILITY_PACK_SERVICE_IDS
from ecorex.integration.image_tools import production_pack_adapter_resolver as image_resolver
from ecorex.integration.pack_process import ProcessCapabilityPackAdapter
from ecorex.integration.pack_python import resolve_pack_python
from ecorex.integration.sandbox import (
    MacOSSandboxExecBackend,
    WindowsAppContainerSandboxBackend,
)


def production_pack_adapter_resolver(
    pack: VerifiedCapabilityPack,
    workspace_roots: tuple[Path, ...],
    runtime_payload_root: Path,
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
        interpreter, _identity = resolve_pack_python(
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
            sandbox_backend=sandbox_backend,
        ).handlers()
    raise ValueError(
        f"no production adapter exists for pack {pack.manifest.pack_id!r}"
    )
__all__ = ["production_pack_adapter_resolver"]
