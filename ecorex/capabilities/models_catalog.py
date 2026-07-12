"""Managed model catalog and strict canonical alias resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata
from typing import Iterable

from ecorex.managed_model_policy import ManagedChatModelPolicy

from .models import normalize_reference, stable_digest


class ModelCatalogError(ValueError):
    code = "model_catalog_error"


class UnknownModelError(ModelCatalogError):
    code = "unknown_model"


class ModelModalityMismatch(ModelCatalogError):
    code = "model_modality_mismatch"


class ModelModality(StrEnum):
    CHAT = "chat"
    IMAGE = "image"
    VISION = "vision"
    AUDIO = "audio"
    EMBEDDING = "embedding"


MAX_MANAGED_MODELS = 256
MAX_MODEL_ID_BYTES = 128
MAX_MODEL_DISPLAY_NAME_BYTES = 256
MAX_MODEL_ALIASES = 32
MAX_MODEL_ALIAS_BYTES = 128
MAX_MODEL_CAPABILITIES = 64
MAX_MODEL_CAPABILITY_BYTES = 128

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MODEL_CAPABILITY_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
)


def _utf8_size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _has_control_characters(value: str) -> bool:
    # Catalog metadata crosses JSON, logs, prompts and audit projections.  No
    # control/format character has a legitimate identity or display purpose
    # there; rejecting it also closes bidi and zero-width alias ambiguity.
    return any(unicodedata.category(character).startswith("C") for character in value)


def _validate_text(
    value: object,
    *,
    label: str,
    maximum_bytes: int,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    size = _utf8_size(value)
    if (
        not value.strip()
        or size is None
        or size > maximum_bytes
        or _has_control_characters(value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ManagedModelSpec:
    model_id: str
    display_name: str
    modalities: frozenset[ModelModality]
    aliases: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    default_for: frozenset[ModelModality] = frozenset()
    model_policy: ManagedChatModelPolicy | None = None

    def __post_init__(self) -> None:
        model_id = _validate_text(
            self.model_id,
            label="model_id",
            maximum_bytes=MAX_MODEL_ID_BYTES,
        )
        _validate_text(
            self.display_name,
            label="model display_name",
            maximum_bytes=MAX_MODEL_DISPLAY_NAME_BYTES,
        )
        if not _MODEL_ID_RE.fullmatch(model_id):
            raise ValueError("model_id is invalid")
        if (
            not isinstance(self.modalities, frozenset)
            or not self.modalities
            or any(not isinstance(item, ModelModality) for item in self.modalities)
        ):
            raise ValueError("a model needs at least one modality")
        if (
            not isinstance(self.default_for, frozenset)
            or any(not isinstance(item, ModelModality) for item in self.default_for)
        ):
            raise ValueError("default_for must contain model modalities")
        if not self.default_for <= self.modalities:
            raise ValueError("default_for must be a subset of modalities")
        if self.model_policy is not None:
            if not isinstance(self.model_policy, ManagedChatModelPolicy):
                raise ValueError("model_policy is invalid")
            if self.model_policy.local_model_id != model_id:
                raise ValueError("model_policy local model identity is inconsistent")
            if ModelModality.CHAT not in self.modalities:
                raise ValueError("managed chat model policy requires chat modality")
        if (
            not isinstance(self.aliases, tuple)
            or len(self.aliases) > MAX_MODEL_ALIASES
        ):
            raise ValueError("model aliases exceed the product limit")
        normalized_aliases: list[str] = []
        for alias in self.aliases:
            source = _validate_text(
                alias,
                label="model alias",
                maximum_bytes=MAX_MODEL_ALIAS_BYTES,
            )
            normalized = normalize_reference(source)
            if (
                not normalized
                or (normalized_size := _utf8_size(normalized)) is None
                or normalized_size > MAX_MODEL_ALIAS_BYTES
                or _has_control_characters(normalized)
            ):
                raise ValueError("model alias is invalid")
            normalized_aliases.append(normalized)
        canonical_reference = normalize_reference(model_id)
        if (
            canonical_reference in normalized_aliases
            or len(set(normalized_aliases)) != len(normalized_aliases)
        ):
            raise ValueError("model aliases must be unique after normalization")
        if (
            not isinstance(self.capabilities, frozenset)
            or len(self.capabilities) > MAX_MODEL_CAPABILITIES
        ):
            raise ValueError("model capabilities exceed the product limit")
        normalized_capabilities: list[str] = []
        for capability in self.capabilities:
            source = _validate_text(
                capability,
                label="model capability",
                maximum_bytes=MAX_MODEL_CAPABILITY_BYTES,
            )
            normalized = normalize_reference(source)
            if (
                (normalized_size := _utf8_size(normalized)) is None
                or normalized_size > MAX_MODEL_CAPABILITY_BYTES
                or not _MODEL_CAPABILITY_RE.fullmatch(normalized)
            ):
                raise ValueError("model capability is invalid")
            normalized_capabilities.append(normalized)
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise ValueError("model capabilities must be unique after normalization")
        object.__setattr__(self, "aliases", tuple(normalized_aliases))
        object.__setattr__(self, "capabilities", frozenset(normalized_capabilities))

    @property
    def references(self) -> frozenset[str]:
        return frozenset(
            {normalize_reference(self.model_id), *(normalize_reference(a) for a in self.aliases)}
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "modalities": sorted(item.value for item in self.modalities),
            "aliases": sorted(self.aliases, key=normalize_reference),
            "capabilities": sorted(self.capabilities),
            "default_for": sorted(item.value for item in self.default_for),
            "model_policy": (
                self.model_policy.to_dict() if self.model_policy is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ModelResolution:
    requested_reference: str | None
    modality: ModelModality
    canonical_model_id: str
    reason: str
    catalog_snapshot_id: str


class ManagedModelCatalog:
    def __init__(self, models: Iterable[ManagedModelSpec]) -> None:
        self._models: dict[str, ManagedModelSpec] = {}
        self._references: dict[str, str] = {}
        self._defaults: dict[ModelModality, str] = {}
        try:
            iterator = iter(models)
        except TypeError:
            raise ValueError("managed model catalog must be iterable") from None
        for index, model in enumerate(iterator):
            if index >= MAX_MANAGED_MODELS:
                raise ValueError("managed model catalog exceeds the product limit")
            if not isinstance(model, ManagedModelSpec):
                raise ValueError("managed model catalog contains an invalid model")
            if model.model_id in self._models:
                raise ValueError(f"duplicate model_id: {model.model_id}")
            for reference in model.references:
                if reference in self._references:
                    raise ValueError(f"duplicate model alias: {reference}")
            self._models[model.model_id] = model
            for reference in model.references:
                self._references[reference] = model.model_id
            for modality in model.default_for:
                if modality in self._defaults:
                    raise ValueError(f"multiple defaults for modality: {modality.value}")
                self._defaults[modality] = model.model_id
        if not self._models:
            raise ValueError("managed model catalog cannot be empty")

    @property
    def snapshot_id(self) -> str:
        payload = {
            "models": [self._models[key].to_dict() for key in sorted(self._models)]
        }
        return "models_" + stable_digest(payload)

    def for_modality(self, modality: ModelModality) -> tuple[ManagedModelSpec, ...]:
        return tuple(
            model
            for model in (self._models[key] for key in sorted(self._models))
            if modality in model.modalities
        )

    def get(self, canonical_model_id: str) -> ManagedModelSpec:
        """Return an exact canonical catalog entry, never an alias match."""

        model = self._models.get(canonical_model_id)
        if model is None:
            raise UnknownModelError(
                f"unknown canonical managed model: {canonical_model_id!r}"
            )
        return model

    def resolve(
        self,
        reference: str | None,
        *,
        modality: ModelModality,
    ) -> ModelResolution:
        if reference is None or not str(reference).strip():
            model_id = self._defaults.get(modality)
            if model_id is None:
                raise UnknownModelError(f"no default model for {modality.value}")
            reason = "modality_default"
        else:
            model_id = self._references.get(normalize_reference(reference))
            if model_id is None:
                raise UnknownModelError(f"unknown managed model: {reference!r}")
            reason = "canonical_id" if reference == model_id else "explicit_alias"
        model = self._models[model_id]
        if modality not in model.modalities:
            raise ModelModalityMismatch(
                f"model {model.model_id!r} does not support {modality.value}"
            )
        return ModelResolution(
            requested_reference=reference,
            modality=modality,
            canonical_model_id=model.model_id,
            reason=reason,
            catalog_snapshot_id=self.snapshot_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "modalities": {
                modality.value: [model.to_dict() for model in self.for_modality(modality)]
                for modality in ModelModality
            },
        }
