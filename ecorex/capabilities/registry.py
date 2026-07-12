"""Versioned tool catalog with exact alias resolution."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import DuplicateCapabilityError, UnknownCapabilityError
from .models import ToolSpec, normalize_reference, stable_digest


MAX_CAPABILITY_TOOLS = 1024


class CapabilityRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._references: dict[str, str] = {}
        self._sealed = False
        self._sealed_digest: str | None = None
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if self._sealed:
            raise RuntimeError("capability registry is sealed")
        if len(self._specs) >= MAX_CAPABILITY_TOOLS:
            raise ValueError("capability registry exceeds the product tool limit")
        if spec.tool_id in self._specs:
            raise DuplicateCapabilityError(f"tool_id is already registered: {spec.tool_id}")
        collisions = sorted(reference for reference in spec.references if reference in self._references)
        if collisions:
            raise DuplicateCapabilityError(
                f"tool alias collision for {spec.tool_id}: {', '.join(collisions)}"
            )
        self._specs[spec.tool_id] = spec
        for reference in spec.references:
            self._references[reference] = spec.tool_id

    def seal(self) -> None:
        """Freeze one product catalog and cache its immutable digest."""

        if self._sealed:
            return
        self._sealed_digest = self._calculate_digest()
        self._sealed = True

    def resolve(self, reference: str) -> ToolSpec:
        tool_id = self._references.get(normalize_reference(reference))
        if tool_id is None:
            raise UnknownCapabilityError(f"unknown tool reference: {reference!r}")
        return self._specs[tool_id]

    def get(self, tool_id: str) -> ToolSpec:
        try:
            return self._specs[tool_id]
        except KeyError as exc:
            raise UnknownCapabilityError(f"unknown tool_id: {tool_id!r}") from exc

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    @property
    def digest(self) -> str:
        if self._sealed_digest is not None:
            return self._sealed_digest
        return self._calculate_digest()

    @property
    def sealed(self) -> bool:
        return self._sealed

    def _calculate_digest(self) -> str:
        return stable_digest(
            {"tools": [spec.to_dict(include_schemas=True) for spec in self.all()]}
        )


__all__ = ["CapabilityRegistry", "MAX_CAPABILITY_TOOLS"]
