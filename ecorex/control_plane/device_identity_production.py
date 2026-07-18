"""Fail-closed production composition for the device identity broker."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from ecorex.release.external_signer import DigestPinnedExternalSigner

from .device_identity import DeviceIdentitySecrets, ManagedDeviceIdentityBroker
from .device_identity_management import AdminManagementDeviceAccountDirectory
from .management import AdminManagementRepository


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")


class DeviceIdentityProductionConfigurationError(RuntimeError):
    pass


class DeviceIdentitySecretProvider(Protocol):
    def read(self, logical_name: str) -> bytes: ...


class EnvironmentDeviceIdentitySecretProvider:
    _NAMES = {
        "derivation-key": "ECOREX_CP_DEVICE_DERIVATION_KEY_B64",
        "legacy-credential-pepper": "ECOREX_CP_DEVICE_LEGACY_PEPPER_B64",
    }

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = os.environ if environment is None else environment

    def read(self, logical_name: str) -> bytes:
        name = self._NAMES.get(logical_name)
        if name is None:
            raise DeviceIdentityProductionConfigurationError(
                "unknown device identity secret"
            )
        raw = self.environment.get(name)
        try:
            value = base64.b64decode(str(raw), validate=True)
        except (ValueError, binascii.Error):
            raise DeviceIdentityProductionConfigurationError(
                "required device identity secret is unavailable"
            ) from None
        if not 32 <= len(value) <= 64:
            raise DeviceIdentityProductionConfigurationError(
                "required device identity secret is unavailable"
            )
        return value


@dataclass(frozen=True, slots=True)
class ExternalSignerConfig:
    key_id: str
    public_key: bytes
    executable: Path
    executable_sha256: str
    adapter: Path | None
    adapter_sha256: str | None
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if (
            _KEY_ID.fullmatch(self.key_id) is None
            or not isinstance(self.public_key, bytes)
            or len(self.public_key) != 32
            or not self.executable.is_absolute()
            or _SHA256.fullmatch(self.executable_sha256) is None
            or (self.adapter is None) != (self.adapter_sha256 is None)
            or (self.adapter is not None and not self.adapter.is_absolute())
            or (
                self.adapter_sha256 is not None
                and _SHA256.fullmatch(self.adapter_sha256) is None
            )
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise DeviceIdentityProductionConfigurationError(
                "device identity signer configuration is invalid"
            )

    def create(self) -> DigestPinnedExternalSigner:
        return DigestPinnedExternalSigner(
            key_id=self.key_id,
            public_key=self.public_key,
            executable_path=self.executable,
            executable_sha256=self.executable_sha256,
            adapter_path=self.adapter,
            adapter_sha256=self.adapter_sha256,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class DeviceIdentityProductionConfig:
    database_path: Path
    issuer: str
    audience: str
    verification_url: str
    allowed_client_ids: frozenset[str]
    platform_admin_account_ids: frozenset[str]
    access_signer: ExternalSignerConfig
    lease_signer: ExternalSignerConfig

    def __post_init__(self) -> None:
        if (
            not self.database_path.is_absolute()
            or not self.allowed_client_ids
            or not {
                "ecorex-product",
                "ecorex-webui",
                "ecorex-admin-web",
            }
            <= self.allowed_client_ids
            or not self.platform_admin_account_ids
            or any(
                _ACCOUNT_ID.fullmatch(account_id) is None
                for account_id in self.platform_admin_account_ids
            )
            or self.access_signer.key_id == self.lease_signer.key_id
            or hashlib.sha256(self.access_signer.public_key).digest()
            == hashlib.sha256(self.lease_signer.public_key).digest()
        ):
            raise DeviceIdentityProductionConfigurationError(
                "device identity production configuration is invalid"
            )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "DeviceIdentityProductionConfig":
        values = os.environ if environment is None else environment
        return cls(
            database_path=_absolute(values, "ECOREX_CP_DATABASE_PATH"),
            issuer=_required(values, "ECOREX_CP_DEVICE_ISSUER"),
            audience=_required(values, "ECOREX_CP_DEVICE_AUDIENCE"),
            verification_url=_required(values, "ECOREX_CP_DEVICE_VERIFICATION_URL"),
            allowed_client_ids=frozenset(
                item.strip()
                for item in _required(
                    values, "ECOREX_CP_DEVICE_ALLOWED_CLIENT_IDS"
                ).split(",")
                if item.strip()
            ),
            platform_admin_account_ids=frozenset(
                item.strip()
                for item in _required(
                    values, "ECOREX_CP_DEVICE_PLATFORM_ADMIN_ACCOUNT_IDS"
                ).split(",")
                if item.strip()
            ),
            access_signer=_signer(values, "ACCESS"),
            lease_signer=_signer(values, "LEASE"),
        )

    def compose(
        self,
        management_repository: AdminManagementRepository,
        *,
        secrets: DeviceIdentitySecretProvider,
        initialize: bool = False,
    ) -> ManagedDeviceIdentityBroker:
        return ManagedDeviceIdentityBroker(
            self.database_path,
            account_directory=AdminManagementDeviceAccountDirectory(
                management_repository,
                platform_admin_account_ids=self.platform_admin_account_ids,
            ),
            access_signer=self.access_signer.create(),
            lease_signer=self.lease_signer.create(),
            secrets=DeviceIdentitySecrets(
                secrets.read("derivation-key"),
                secrets.read("legacy-credential-pepper"),
            ),
            issuer=self.issuer,
            audience=self.audience,
            verification_url=self.verification_url,
            allowed_client_ids=self.allowed_client_ids,
            initialize=initialize,
        )


def _signer(values: Mapping[str, str], role: str) -> ExternalSignerConfig:
    prefix = f"ECOREX_CP_DEVICE_{role}_SIGNER_"
    try:
        public = base64.b64decode(
            _required(values, prefix + "PUBLIC_KEY_B64"), validate=True
        )
    except (ValueError, binascii.Error):
        raise DeviceIdentityProductionConfigurationError(
            "device identity signer public key is invalid"
        ) from None
    adapter_raw = values.get(prefix + "ADAPTER")
    adapter_digest = values.get(prefix + "ADAPTER_SHA256")
    return ExternalSignerConfig(
        key_id=_required(values, prefix + "KEY_ID"),
        public_key=public,
        executable=_absolute(values, prefix + "EXECUTABLE"),
        executable_sha256=_required(values, prefix + "EXECUTABLE_SHA256"),
        adapter=Path(adapter_raw).resolve() if adapter_raw else None,
        adapter_sha256=adapter_digest or None,
        timeout_seconds=int(values.get(prefix + "TIMEOUT_SECONDS", "30")),
    )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DeviceIdentityProductionConfigurationError(
            "required device identity configuration is unavailable"
        )
    return value


def _absolute(values: Mapping[str, str], name: str) -> Path:
    path = Path(_required(values, name))
    if not path.is_absolute():
        raise DeviceIdentityProductionConfigurationError(
            "device identity path must be absolute"
        )
    return path.resolve()


__all__ = [
    "DeviceIdentityProductionConfig",
    "DeviceIdentityProductionConfigurationError",
    "DeviceIdentitySecretProvider",
    "EnvironmentDeviceIdentitySecretProvider",
    "ExternalSignerConfig",
]
