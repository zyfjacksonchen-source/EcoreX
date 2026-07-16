"""Public and internal contracts for the product administrator workspace."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ecorex.managed_model_policy import MANAGED_CHAT_MODEL_POLICIES


UserStatus = Literal["active", "suspended"]
ModelModality = Literal["chat", "image_generation", "image_edit"]
ModelRevisionStatus = Literal[
    "draft", "testing", "active", "rejected", "superseded"
]
ProviderPreset = Literal[
    "responses", "openai_compatible_chat", "openai_compatible_image"
]
ProviderOriginPreset = Literal[
    "ecorex_chat",
    "deepseek_chat",
    "gemini_chat",
    "doubao_chat",
    "ecorex_image",
]

MANAGED_MODEL_SLOTS: Mapping[str, str] = MappingProxyType(
    {
        **{model_id: "chat" for model_id in MANAGED_CHAT_MODEL_POLICIES},
        "gpt-image-2": "image_generation",
        "gpt-image-2-edit": "image_edit",
    }
)

# API protocol and egress authority are deliberately different concepts.  The
# administrator may rotate a key and upstream model identity, but cannot turn a
# tested model revision into an arbitrary-origin SSRF primitive.  Every public
# EcoreX slot has one product-owned protocol and one deployment-owned origin
# preset.
MANAGED_MODEL_PROVIDER_PROTOCOLS: Mapping[str, ProviderPreset] = MappingProxyType(
    {
        "ecorex-chat": "responses",
        "ecorex-deepseek-v4-pro": "openai_compatible_chat",
        "ecorex-gemini-3.1-pro": "openai_compatible_chat",
        "ecorex-doubao-seed-2.0-pro": "openai_compatible_chat",
        "gpt-image-2": "openai_compatible_image",
        "gpt-image-2-edit": "openai_compatible_image",
    }
)
MANAGED_MODEL_ORIGIN_PRESETS: Mapping[str, ProviderOriginPreset] = MappingProxyType(
    {
        "ecorex-chat": "ecorex_chat",
        "ecorex-deepseek-v4-pro": "deepseek_chat",
        "ecorex-gemini-3.1-pro": "gemini_chat",
        "ecorex-doubao-seed-2.0-pro": "doubao_chat",
        "gpt-image-2": "ecorex_image",
        "gpt-image-2-edit": "ecorex_image",
    }
)


def provider_protocol_for_slot(local_model_id: str) -> ProviderPreset:
    try:
        return MANAGED_MODEL_PROVIDER_PROTOCOLS[local_model_id]
    except KeyError:
        raise ValueError("model ID is not a managed EcoreX model slot") from None


def provider_origin_preset_for_slot(local_model_id: str) -> ProviderOriginPreset:
    try:
        return MANAGED_MODEL_ORIGIN_PRESETS[local_model_id]
    except KeyError:
        raise ValueError("model ID is not a managed EcoreX model slot") from None


class ManagementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminUserProjection(ManagementModel):
    account_id: str
    display_name: str
    email: str | None
    organization_id: str | None
    status: UserStatus
    token_limit: int
    tokens_used: int
    image_limit: int
    images_used: int
    revision: int
    created_at: str
    updated_at: str


class AdminUserListProjection(ManagementModel):
    items: list[AdminUserProjection]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)


class CreateAdminUserRequest(ManagementModel):
    account_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$",
    )
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    organization_id: str | None = Field(default=None, min_length=1, max_length=128)
    token_limit: int = Field(default=0, ge=0, le=10**12)
    image_limit: int = Field(default=0, ge=0, le=10**9)
    client_request_id: str = Field(min_length=8, max_length=256)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("user display name is invalid")
        return normalized

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if (
            not normalized
            or normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
            or any(character.isspace() or ord(character) < 33 for character in normalized)
        ):
            raise ValueError("user email is invalid")
        return normalized


class UpdateAdminUserRequest(ManagementModel):
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    organization_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: UserStatus
    token_limit: int = Field(ge=0, le=10**12)
    image_limit: int = Field(ge=0, le=10**9)
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=8, max_length=256)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        return CreateAdminUserRequest._display_name(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return CreateAdminUserRequest._email(value)


class AdjustUsageRequest(ManagementModel):
    token_delta: int = Field(default=0, ge=-(10**12), le=10**12)
    image_delta: int = Field(default=0, ge=-(10**9), le=10**9)
    reason: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=8, max_length=256)

    @model_validator(mode="after")
    def _has_change(self) -> "AdjustUsageRequest":
        if self.token_delta == 0 and self.image_delta == 0:
            raise ValueError("usage adjustment must change a counter")
        return self


class UsageSummaryProjection(ManagementModel):
    users_total: int = Field(ge=0)
    users_active: int = Field(ge=0)
    token_limit: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    image_limit: int = Field(ge=0)
    images_used: int = Field(ge=0)
    captured_at: str


class ModelRevisionProjection(ManagementModel):
    config_id: str
    revision: int = Field(ge=1)
    local_model_id: str
    modality: ModelModality
    display_name: str
    upstream_model_id: str
    provider_preset: ProviderPreset
    is_default: bool
    enabled: bool
    status: ModelRevisionStatus
    key_configured: bool
    key_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    test_id: str | None
    test_status: Literal["not_tested", "running", "passed", "failed"]
    test_error_code: str | None
    tested_at: str | None
    created_at: str
    updated_at: str


class ModelConfigurationProjection(ManagementModel):
    config_id: str
    active: ModelRevisionProjection | None
    draft: ModelRevisionProjection | None


class CreateModelConfigurationRequest(ManagementModel):
    local_model_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    modality: ModelModality
    display_name: str = Field(min_length=1, max_length=128)
    upstream_model_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    provider_preset: ProviderPreset
    is_default: bool = False
    enabled: bool = True
    api_key: SecretStr = Field(min_length=8, max_length=4096)
    client_request_id: str = Field(min_length=8, max_length=256)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("model display name is invalid")
        return normalized

    @model_validator(mode="after")
    def _provider_matches_modality(self) -> "CreateModelConfigurationRequest":
        if MANAGED_MODEL_SLOTS.get(self.local_model_id) != self.modality:
            raise ValueError("model ID is not a managed EcoreX model slot")
        if self.provider_preset != provider_protocol_for_slot(self.local_model_id):
            raise ValueError("model provider protocol is fixed by its EcoreX slot")
        return self


class StageModelConfigurationRequest(ManagementModel):
    display_name: str = Field(min_length=1, max_length=128)
    upstream_model_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    provider_preset: ProviderPreset
    is_default: bool
    enabled: bool
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=4096)
    expected_active_revision: int | None = Field(default=None, ge=1)
    client_request_id: str = Field(min_length=8, max_length=256)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        return CreateModelConfigurationRequest._display_name(value)


class TestAndActivateModelRequest(ManagementModel):
    revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=8, max_length=256)


class ModelTestProjection(ManagementModel):
    test_id: str
    config_id: str
    revision: int
    status: Literal["running", "passed", "failed", "superseded"]
    error_code: str | None
    active_revision: int | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ActiveModelConfiguration:
    """Secret-bearing immutable snapshot; never use this as an HTTP response."""

    config_id: str
    revision: int
    local_model_id: str
    modality: ModelModality
    display_name: str
    upstream_model_id: str
    provider_preset: ProviderPreset
    is_default: bool
    api_key: str
    provider_origin_preset: ProviderOriginPreset = "ecorex_chat"


__all__ = [
    "ActiveModelConfiguration",
    "AdjustUsageRequest",
    "AdminUserListProjection",
    "AdminUserProjection",
    "CreateAdminUserRequest",
    "CreateModelConfigurationRequest",
    "ModelConfigurationProjection",
    "MANAGED_MODEL_SLOTS",
    "MANAGED_MODEL_ORIGIN_PRESETS",
    "MANAGED_MODEL_PROVIDER_PROTOCOLS",
    "ModelModality",
    "ModelRevisionProjection",
    "ModelTestProjection",
    "ProviderPreset",
    "ProviderOriginPreset",
    "StageModelConfigurationRequest",
    "TestAndActivateModelRequest",
    "UpdateAdminUserRequest",
    "UsageSummaryProjection",
    "provider_origin_preset_for_slot",
    "provider_protocol_for_slot",
]
