"""Backend-owned contracts for dependency-only Capability Pack services."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping

from .models import stable_digest


_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class PackServiceSpec:
    service_id: str
    version: str
    contract: Mapping[str, Any]

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.service_id) is None:
            raise ValueError("Capability Pack service ID is invalid")
        if _SEMVER.fullmatch(self.version) is None:
            raise ValueError("Capability Pack service version is invalid")
        if not isinstance(self.contract, Mapping) or not self.contract:
            raise ValueError("Capability Pack service contract is invalid")
        object.__setattr__(self, "contract", MappingProxyType(dict(self.contract)))

    @property
    def contract_sha256(self) -> str:
        return stable_digest(
            {
                "service_id": self.service_id,
                "version": self.version,
                "contract": dict(self.contract),
            }
        )


_SERVICES = (
    PackServiceSpec(
        service_id="channels.adapters",
        version="1.0.0",
        contract={
            "schema_version": 1,
            "authority": "connector-definition-instance-adapter",
            "required_connectors": ["feishu", "tencent-docs"],
            "operations": ["catalog", "describe", "read", "write", "health"],
            "authentication": "persistent-hitl",
            "result_transport": "artifact-envelope-v1",
        },
    ),
    PackServiceSpec(
        service_id="ocr.extract",
        version="1.0.0",
        contract={
            "schema_version": 1,
            "inputs": ["image", "pdf-rendition"],
            "outputs": ["text", "blocks", "confidence", "language"],
            "artifact_visibility": "internal-unless-declared-deliverable",
            "execution": "local-bounded-worker",
        },
    ),
    PackServiceSpec(
        service_id="office.formats",
        version="1.0.0",
        contract={
            "schema_version": 1,
            "families": ["document", "spreadsheet", "presentation", "pdf"],
            "operations": ["create", "read", "validate"],
            "outputs": ["office-file", "format-evidence"],
            "artifact_visibility": "classifier-authoritative",
            "execution": "local-bounded-worker",
        },
    ),
)


def builtin_pack_service_specs() -> Mapping[str, PackServiceSpec]:
    return MappingProxyType({spec.service_id: spec for spec in _SERVICES})


__all__ = ["PackServiceSpec", "builtin_pack_service_specs"]
