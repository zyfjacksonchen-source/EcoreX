"""Fail-closed contract for a product-owned controlled Skill runner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType
from typing import Any, Protocol


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_SKILL_PARAMETERS_BYTES = 64 * 1024
MAX_SKILL_RESULT_BYTES = 256 * 1024


def _canonical_mapping(value: Mapping[str, Any], *, maximum: int, label: str) -> None:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Skill {label} must be canonical JSON") from error
    if len(payload) > maximum:
        raise ValueError(f"Skill {label} exceeds its execution boundary")


@dataclass(frozen=True, slots=True)
class ControlledSkillRunRequest:
    """Exact CAS revision and declarations admitted for one execution.

    The runner resolves ``artifact_sha256`` only through the bound, verified
    Skill CAS.  It must call ``state_fence`` immediately before process start
    and while waiting; a failed fence terminates the complete process tree.
    No host command string is part of this contract.
    """

    extension_id: str
    revision_id: str
    artifact_sha256: str
    extension_generation: int
    runtime: str
    entrypoint: str
    parameters: Mapping[str, Any]
    environment: Mapping[str, str] = field(repr=False)
    network_domains: tuple[str, ...]
    effects: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.extension_id or not self.revision_id:
            raise ValueError("Skill execution identity is required")
        if _SHA256.fullmatch(self.artifact_sha256) is None:
            raise ValueError("Skill execution artifact digest is invalid")
        if isinstance(self.extension_generation, bool) or self.extension_generation < 0:
            raise ValueError("Skill execution generation is invalid")
        if self.runtime not in {"python", "node"}:
            raise ValueError("Skill execution runtime is unsupported")
        suffix = ".py" if self.runtime == "python" else (".js", ".mjs")
        if not self.entrypoint.startswith("scripts/") or not self.entrypoint.endswith(suffix):
            raise ValueError("Skill execution entrypoint is invalid")
        if any(not isinstance(key, str) or not key for key in self.parameters):
            raise ValueError("Skill parameter names are invalid")
        # skill-runtime.json v1 has no parameter schema; therefore its exact
        # declared parameter set is empty.  Do not turn an open JSON object
        # into an undeclared command/argument channel.
        if self.parameters:
            raise ValueError("Skill parameters are not declared by this manifest")
        _canonical_mapping(
            self.parameters,
            maximum=MAX_SKILL_PARAMETERS_BYTES,
            label="parameters",
        )
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in self.environment.items()
        ):
            raise ValueError("Skill execution environment is invalid")
        if self.network_domains:
            raise ValueError("Skill network execution has no verified boundary")
        if not self.effects or any(value not in {"read", "write"} for value in self.effects):
            raise ValueError("Skill execution effects are unsupported")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True, slots=True)
class ControlledSkillRunResult:
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        _canonical_mapping(
            self.result,
            maximum=MAX_SKILL_RESULT_BYTES,
            label="result",
        )
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))


class ControlledSkillRunner(Protocol):
    """Adapter implemented only by the signed AppContainer/Seatbelt authority."""

    def supports(self, runtime: str) -> bool: ...

    def run(
        self,
        request: ControlledSkillRunRequest,
        *,
        state_fence: Callable[[], None],
    ) -> Awaitable[ControlledSkillRunResult]: ...


__all__ = [
    "ControlledSkillRunRequest",
    "ControlledSkillRunResult",
    "ControlledSkillRunner",
]
