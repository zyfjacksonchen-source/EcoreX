"""Production composition and operator CLI for the EcoreX Control Plane.

The built-in v1 provider is intentionally a *single-node* SQLite WAL service.
It requires an exclusive process lock, a persistent-volume identity, verified
backups, private Share media, encrypted Cloud Audit and short-lived Ed25519
JWTs. Share media uses either private S3 or an attested encrypted local CAS
whose availability boundary is one host and one replica. It never runs DDL
during ``serve`` and refuses PostgreSQL/multi-replica
configuration until a separately reviewed HA provider implements the typed
dependency contract.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import ipaddress
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
import uuid

from ecorex import __version__
from ecorex.observability.audit import AuditRetentionPolicy
from ecorex.security.provider_tls import (
    ProviderTLSConfigurationError,
    pinned_provider_ssl_context,
    validate_provider_ca_binding,
)
from ecorex.storage.attested_local_cas import (
    AttestedEncryptedLocalCAS,
    AttestedEncryptedLocalVolume,
    AttestedLocalCASError,
)
from ecorex.release import (
    DigestPinnedExternalSigner,
    PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS,
    PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS,
    ReleaseSigner,
)
from ecorex.release.direct_admission import DirectReleaseAdmissionPolicy
from ecorex.update import Ed25519SignatureVerifier, MAX_ARTIFACT_BYTES
from ecorex.extensions import LocalSkillBundleStore

from .app import ControlPlaneServiceLifecycle, create_control_plane_app
from .audit import CloudAuditRepository
from .connector_gateway import FeishuConnectorGateway
from .connector_gateway_schema import ConnectorGatewaySchemaManager
from .wechat_callback_gateway import WechatCallbackGateway
from .wechat_callback_schema import WechatCallbackSchemaManager
from .bootstrap_index_service import (
    BootstrapIndexPublicationService,
    FilesystemPublicIndexObjectStore,
    HTTPSPublicIndexReader,
)
from .bootstrap_freshness import (
    BootstrapFreshnessConfig,
    BootstrapFreshnessRefresher,
)
from .audit_schema import (
    CLOUD_AUDIT_SCHEMA_SHA256,
    CURRENT_CLOUD_AUDIT_SCHEMA_VERSION,
    MIGRATION_001_CHECKSUM as CLOUD_AUDIT_MIGRATION_CHECKSUM,
    CloudAuditSchemaManager,
)
from .direct_admission_schema import CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION
from .production_auth import (
    Ed25519JWTAuthenticator,
    EMateSessionJWTAuthenticator,
    parse_ed25519_public_keyring,
)
from .production_storage import (
    BackupReceipt,
    ControlPlaneInstanceLock,
    PersistentVolumeGuard,
    ProductionStorageError,
    SQLiteBackupManager,
    available_bytes,
)
from .models import ControlPrincipal
from .management import AdminManagementRepository, HTTPSModelConnectionTester
from .management_schema import (
    ADMIN_MANAGEMENT_MIGRATION_CHECKSUM,
    CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION,
    AdminManagementSchemaManager,
)
from .skill_hub import SkillHubRegistry
from .device_identity import ManagedDeviceIdentityBroker
from .device_identity_production import (
    DeviceIdentityProductionConfig,
    DeviceIdentitySecretProvider,
)
from .device_identity_schema import DeviceIdentitySchemaManager
from .repository import ControlPlaneRepository
from .release_replica import (
    CDNReleaseReplicaService,
    CloudReleaseReplicaAuditSink,
    EnvironmentRotatingReleaseReplicaTokenVerifier,
    PRODUCTION_RELEASE_REPLICA_PUBLIC_ROOT,
    PRODUCTION_RELEASE_REPLICA_ROOT,
)
from .schema import (
    CONTROL_PLANE_SCHEMA_SHA256,
    CURRENT_CONTROL_PLANE_SCHEMA_VERSION,
    MIGRATION_001_CHECKSUM as CONTROL_PLANE_MIGRATION_CHECKSUM,
    ControlPlaneSchemaManager,
)
from .share_s3_objects import S3ShareObjectStore
from .share_attested_local_objects import AttestedLocalShareObjectStore
from .share_objects import ShareObjectStore
from .share_schema import (
    CLOUD_SHARE_SCHEMA_SHA256,
    CURRENT_CLOUD_SHARE_SCHEMA_VERSION,
    MIGRATION_001_CHECKSUM as CLOUD_SHARE_MIGRATION_CHECKSUM,
    CloudShareSchemaManager,
)
from .shares import CloudShareKeyRing, CloudShareRepository


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SKILL_HUB_SEED_SLUG = "official-writing"
_SKILL_HUB_SEED_VERSION = "1.0.2"
_SKILL_HUB_SEED_SHA256 = (
    "f223e54fb100fd40c278ce0466af2d501dca0fcd9adf635eeba1a1f654e8eca2"
)
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SECRET_NAMES = {
    "share-keyring": "ECOREX_CP_SHARE_KEYRING_JSON",
    "audit-encryption-key": "ECOREX_CP_AUDIT_ENCRYPTION_KEY_B64",
    "audit-integrity-key": "ECOREX_CP_AUDIT_INTEGRITY_KEY_B64",
    "model-config-encryption-key": "ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64",
    "device-derivation-key": "ECOREX_CP_DEVICE_DERIVATION_KEY_B64",
    "device-legacy-credential-pepper": "ECOREX_CP_DEVICE_LEGACY_PEPPER_B64",
    "feishu-app-id": "ECOREX_CP_FEISHU_APP_ID",
    "feishu-app-secret": "ECOREX_CP_FEISHU_APP_SECRET",
    "feishu-token-encryption-key": "ECOREX_CP_FEISHU_TOKEN_ENCRYPTION_KEY_B64",
    "wechat-callback-encryption-key": "ECOREX_CP_WECHAT_CALLBACK_ENCRYPTION_KEY_B64",
}
_S3_SETTING_NAMES = frozenset(
    {
        "ECOREX_CP_S3_BUCKET",
        "ECOREX_CP_S3_PREFIX",
        "ECOREX_CP_S3_REGION",
        "ECOREX_CP_S3_ENDPOINT_URL",
        "ECOREX_CP_S3_ADDRESSING_STYLE",
        "ECOREX_CP_S3_MAX_CONNECTIONS",
    }
)


class ProductionConfigurationError(RuntimeError):
    """A production setting/dependency is missing, unsafe or unsupported."""


@runtime_checkable
class SecretProvider(Protocol):
    """Narrow seam for environment, Vault, KMS sidecar or workload identity."""

    def read(self, logical_name: str) -> str: ...


class EnvironmentSecretProvider:
    """Read only three fixed secret names; values are never included in errors."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def read(self, logical_name: str) -> str:
        environment_name = _SECRET_NAMES.get(logical_name)
        if environment_name is None:
            raise ProductionConfigurationError("unknown Control Plane secret")
        value = self._environment.get(environment_name)
        if not isinstance(value, str) or not value:
            raise ProductionConfigurationError(
                "required Control Plane secret is unavailable"
            )
        return value


class _ControlPlaneDeviceIdentitySecrets(DeviceIdentitySecretProvider):
    """Decode two fixed secrets supplied by the Control Plane secret backend."""

    def __init__(self, provider: SecretProvider) -> None:
        self.provider = provider

    def read(self, logical_name: str) -> bytes:
        names = {
            "derivation-key": "device-derivation-key",
            "legacy-credential-pepper": "device-legacy-credential-pepper",
        }
        target = names.get(logical_name)
        if target is None:
            raise ProductionConfigurationError("unknown device identity secret")
        return _secret_bytes(
            self.provider.read(target), minimum_length=32, maximum_length=64
        )


@runtime_checkable
class ProductionS3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_bucket(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_bucket_encryption(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_public_access_block(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class S3ClientFactory(Protocol):
    def create(self, config: "ControlPlaneProductionConfig") -> ProductionS3Client: ...


@runtime_checkable
class ShareStorageDependency(Protocol):
    def validate_controls(self, *, write_probe: bool) -> None: ...

    def ping(self) -> None: ...

    def close(self) -> None: ...


class ControlPlaneLocalCASFactory(Protocol):
    def create(
        self, config: "ControlPlaneProductionConfig"
    ) -> tuple[ShareStorageDependency, ShareObjectStore]: ...


class Boto3S3ClientFactory:
    """Use the SDK credential chain; access keys never enter EcoreX config."""

    def create(self, config: "ControlPlaneProductionConfig") -> ProductionS3Client:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:  # pragma: no cover - deployment pack boundary
            raise ProductionConfigurationError(
                "the signed Control Plane cloud storage pack is unavailable"
            ) from error
        client = boto3.client(
            "s3",
            region_name=config.s3_region,
            endpoint_url=config.s3_endpoint_url,
            config=Config(
                connect_timeout=config.dependency_timeout_seconds,
                read_timeout=config.dependency_timeout_seconds,
                retries={"mode": "standard", "max_attempts": 3},
                max_pool_connections=config.s3_max_connections,
                s3={"addressing_style": config.s3_addressing_style},
            ),
            use_ssl=True,
            verify=True,
        )
        if not isinstance(client, ProductionS3Client):
            try:
                client.close()
            except Exception:
                pass
            raise ProductionConfigurationError("S3 client contract is unavailable")
        return client


class AttestedLocalControlPlaneCASFactory:
    """Construct the production single-host Share CAS."""

    def create(
        self, config: "ControlPlaneProductionConfig"
    ) -> tuple[ShareStorageDependency, ShareObjectStore]:
        try:
            if (
                config.local_cas_root is None
                or config.local_cas_attestation_path is None
                or config.local_cas_attestation_sha256 is None
                or config.local_cas_volume_id is None
                or config.local_cas_machine_id_sha256 is None
                or config.local_cas_owner_gid is None
            ):
                raise ProductionConfigurationError(
                    "attested Share CAS configuration is incomplete"
                )
            volume = AttestedEncryptedLocalVolume(
                cas_root=config.local_cas_root,
                attestation_path=config.local_cas_attestation_path,
                expected_attestation_sha256=config.local_cas_attestation_sha256,
                expected_volume_id=config.local_cas_volume_id,
                expected_machine_id_sha256=config.local_cas_machine_id_sha256,
                replica_count=config.local_cas_replica_count,
                quota_bytes=config.local_cas_quota_bytes,
                minimum_free_bytes=config.local_cas_minimum_free_bytes,
                owner_gid=config.local_cas_owner_gid,
            )
            store = AttestedLocalShareObjectStore(
                AttestedEncryptedLocalCAS(
                    volume,
                    namespace="share",
                    max_blob_bytes=config.local_cas_max_object_bytes,
                ),
                max_open_streams=config.local_cas_max_open_streams,
            )
            return _AttestedLocalShareDependency(store), store
        except ProductionConfigurationError:
            raise
        except AttestedLocalCASError:
            raise ProductionConfigurationError(
                "attested Share CAS could not start"
            ) from None


@dataclass(frozen=True, slots=True)
class ControlPlaneProductionConfig:
    storage_backend: str
    replica_count: int
    database_path: Path
    backup_directory: Path
    storage_volume_id: str
    storage_encryption_at_rest: bool
    minimum_free_bytes: int
    backup_interval_seconds: int
    maximum_backup_age_seconds: int
    backup_retain_count: int
    public_share_base_url: str
    share_spool_directory: Path
    share_storage_mode: str
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    s3_endpoint_url: str | None
    s3_addressing_style: str
    s3_max_connections: int
    local_cas_root: Path | None
    local_cas_attestation_path: Path | None
    local_cas_attestation_sha256: str | None = field(repr=False)
    local_cas_volume_id: str | None
    local_cas_machine_id_sha256: str | None = field(repr=False)
    local_cas_replica_count: int
    local_cas_quota_bytes: int
    local_cas_minimum_free_bytes: int
    local_cas_owner_gid: int | None
    local_cas_max_object_bytes: int
    local_cas_max_open_streams: int
    auth_issuer: str
    auth_audience: str
    auth_public_keys_json: str = field(repr=False)
    release_public_keys_json: str = field(repr=False)
    publication_public_keys_json: str = field(repr=False)
    rollback_signer_public_keys_json: str = field(repr=False)
    public_bootstrap_index_path: Path
    public_bootstrap_index_url: str
    public_bootstrap_readback_hosts: tuple[str, ...]
    auth_max_token_lifetime_seconds: int = 900
    auth_clock_skew_seconds: int = 30
    audit_raw_days: int = 30
    audit_aggregate_days: int = 180
    maintenance_interval_seconds: int = 60 * 60
    instance_id: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8443
    allow_trusted_ingress_http: bool = False
    dependency_timeout_seconds: int = 5
    readiness_cache_seconds: int = 15
    graceful_shutdown_seconds: int = 30
    limit_concurrency: int = 512
    backlog: int = 1024
    signal_poll_interval_seconds: float = 0.25
    signal_retention_seconds: int = 7 * 24 * 60 * 60
    signal_retain_latest: int = 1024
    bootstrap_freshness_automation_enabled: bool = True
    bootstrap_freshness_lead_seconds: int = 8 * 60 * 60
    bootstrap_freshness_check_interval_seconds: int = 60 * 60
    bootstrap_freshness_lease_seconds: int = 10 * 60
    publication_signer_executable: Path | None = None
    publication_signer_executable_sha256: str | None = None
    publication_signer_adapter: Path | None = None
    publication_signer_adapter_sha256: str | None = None
    publication_signer_key_id: str | None = None
    publication_signer_timeout_seconds: int = 30
    rollback_signer_executable: Path | None = None
    rollback_signer_executable_sha256: str | None = None
    rollback_signer_adapter: Path | None = None
    rollback_signer_adapter_sha256: str | None = None
    rollback_signer_key_id: str | None = None
    rollback_signer_timeout_seconds: int = 30
    admin_management_enabled: bool = False
    model_provider_origins: Mapping[str, str] = field(default_factory=dict)
    model_provider_ca_bundle_path: Path | None = None
    model_provider_ca_bundle_sha256: str | None = field(default=None, repr=False)
    model_activation_timeout_seconds: int = 180
    device_identity_enabled: bool = False
    device_identity: DeviceIdentityProductionConfig | None = None
    release_replica_enabled: bool = False
    release_replica_storage_root: Path | None = None
    release_replica_public_root: str | None = None
    release_replica_namespace: str | None = None
    release_replica_product_version: str | None = None
    release_replica_max_asset_bytes: int = MAX_ARTIFACT_BYTES
    direct_release_admission_enabled: bool = False
    direct_release_id: str | None = None
    direct_release_instruction_sha256: str | None = field(default=None, repr=False)
    feishu_connector_enabled: bool = False
    wechat_callback_enabled: bool = False
    skill_hub_auth_issuer: str | None = None
    skill_hub_auth_audience: str | None = None
    skill_hub_auth_public_keys_json: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError:
            raise ProductionConfigurationError(
                "Control Plane bind host must be an IP address"
            ) from None
        parsed_public = urlsplit(self.public_share_base_url)
        parsed_bootstrap = urlsplit(self.public_bootstrap_index_url)
        if (
            self.storage_backend != "sqlite-wal"
            or self.replica_count != 1
            or not isinstance(self.database_path, Path)
            or not self.database_path.is_absolute()
            or not isinstance(self.backup_directory, Path)
            or not self.backup_directory.is_absolute()
            or not isinstance(self.share_spool_directory, Path)
            or not self.share_spool_directory.is_absolute()
            or _SAFE_ID.fullmatch(self.storage_volume_id) is None
            or not self.storage_encryption_at_rest
            or not 64 * 1024 * 1024 <= self.minimum_free_bytes <= 1024**4
            or not 900 <= self.backup_interval_seconds <= 24 * 60 * 60
            or not self.backup_interval_seconds
            <= self.maximum_backup_age_seconds
            <= 7 * 24 * 60 * 60
            or not 2 <= self.backup_retain_count <= 365
            or parsed_public.scheme != "https"
            or not parsed_public.hostname
            or parsed_public.port not in {None, 443}
            or parsed_public.username
            or parsed_public.password
            or parsed_public.query
            or parsed_public.fragment
            or parsed_public.path.rstrip("/") != "/s"
            or not self._share_storage_is_valid()
            or not isinstance(self.auth_issuer, str)
            or not self.auth_issuer
            or not isinstance(self.auth_audience, str)
            or not self.auth_audience
            or not isinstance(self.auth_public_keys_json, str)
            or not self.auth_public_keys_json
            or not isinstance(self.release_public_keys_json, str)
            or not self.release_public_keys_json
            or not isinstance(self.publication_public_keys_json, str)
            or not self.publication_public_keys_json
            or not isinstance(self.rollback_signer_public_keys_json, str)
            or not self.rollback_signer_public_keys_json
            or not isinstance(self.public_bootstrap_index_path, Path)
            or not self.public_bootstrap_index_path.is_absolute()
            or self.public_bootstrap_index_path.name != "public-bootstrap-index.json"
            or parsed_bootstrap.scheme != "https"
            or not parsed_bootstrap.hostname
            or parsed_bootstrap.port not in {None, 443}
            or parsed_bootstrap.username
            or parsed_bootstrap.password
            or not parsed_bootstrap.path.endswith("/public-bootstrap-index.json")
            or parsed_bootstrap.query
            or parsed_bootstrap.fragment
            or not self.public_bootstrap_readback_hosts
            or any(
                not host
                or host != host.casefold().rstrip(".")
                or any(
                    _HOST_LABEL.fullmatch(label) is None for label in host.split(".")
                )
                for host in self.public_bootstrap_readback_hosts
            )
            or (parsed_bootstrap.hostname or "").casefold().rstrip(".")
            not in self.public_bootstrap_readback_hosts
            or not 60 <= self.auth_max_token_lifetime_seconds <= 3600
            or not 0 <= self.auth_clock_skew_seconds <= 120
            or not 1 <= self.audit_raw_days <= self.audit_aggregate_days <= 3650
            or not 60 <= self.maintenance_interval_seconds <= 24 * 60 * 60
            or _SAFE_ID.fullmatch(self.instance_id) is None
            or not 1024 <= self.bind_port <= 65535
            or (not address.is_loopback and not self.allow_trusted_ingress_http)
            or not 1 <= self.dependency_timeout_seconds <= 30
            or not 1 <= self.readiness_cache_seconds <= 60
            or not 5 <= self.graceful_shutdown_seconds <= 300
            or not 16 <= self.limit_concurrency <= 4096
            or not 16 <= self.backlog <= 8192
            or not 0.05 <= self.signal_poll_interval_seconds <= 5.0
            or not 60 <= self.signal_retention_seconds <= 90 * 24 * 60 * 60
            or not 1 <= self.signal_retain_latest <= 100_000
            or not 60 * 60 <= self.bootstrap_freshness_lead_seconds <= 23 * 60 * 60
            or not isinstance(self.bootstrap_freshness_automation_enabled, bool)
            or not 5 * 60
            <= self.bootstrap_freshness_check_interval_seconds
            <= 6 * 60 * 60
            or self.bootstrap_freshness_check_interval_seconds
            > self.bootstrap_freshness_lead_seconds // 2
            or self.bootstrap_freshness_lead_seconds
            + PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS
            >= PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS
            or not 5 * 60 <= self.bootstrap_freshness_lease_seconds <= 30 * 60
            or self.bootstrap_freshness_check_interval_seconds
            + self.bootstrap_freshness_lease_seconds
            + self.publication_signer_timeout_seconds
            + PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS
            >= self.bootstrap_freshness_lead_seconds
            or not 1 <= self.publication_signer_timeout_seconds <= 120
            or not 1 <= self.rollback_signer_timeout_seconds <= 120
            or not 30 <= self.model_activation_timeout_seconds <= 600
            or not isinstance(self.feishu_connector_enabled, bool)
            or not isinstance(self.wechat_callback_enabled, bool)
        ):
            raise ProductionConfigurationError(
                "Control Plane production configuration is invalid"
            )
        skill_hub_auth = (
            self.skill_hub_auth_issuer,
            self.skill_hub_auth_audience,
            self.skill_hub_auth_public_keys_json,
        )
        if any(value is not None for value in skill_hub_auth) and not all(
            isinstance(value, str) and bool(value) for value in skill_hub_auth
        ):
            raise ProductionConfigurationError(
                "Skill Hub authentication configuration is incomplete"
            )
        if self.skill_hub_auth_public_keys_json is not None:
            parse_ed25519_public_keyring(self.skill_hub_auth_public_keys_json)
        auth_keys = parse_ed25519_public_keyring(self.auth_public_keys_json)
        release_keys = parse_ed25519_public_keyring(self.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            self.publication_public_keys_json
        )
        rollback_keys = parse_ed25519_public_keyring(
            self.rollback_signer_public_keys_json
        )
        keyrings = (release_keys, publication_keys, rollback_keys)
        key_ids = [set(keys) for keys in keyrings]
        fingerprints = [
            {hashlib.sha256(material).digest() for material in keys.values()}
            for keys in keyrings
        ]
        if any(
            key_ids[left] & key_ids[right]
            or fingerprints[left] & fingerprints[right]
            for left in range(len(keyrings))
            for right in range(left + 1, len(keyrings))
        ):
            raise ProductionConfigurationError(
                "release, publication and rollback trust roles must use distinct keys"
            )
        if self.direct_release_admission_enabled:
            if (
                not isinstance(self.direct_release_id, str)
                or _SAFE_ID.fullmatch(self.direct_release_id) is None
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(self.direct_release_instruction_sha256)
                )
                is None
            ):
                raise ProductionConfigurationError(
                    "direct release admission scope is invalid"
                )
        elif (
            self.direct_release_id is not None
            or self.direct_release_instruction_sha256 is not None
        ):
            raise ProductionConfigurationError(
                "disabled direct release admission must not retain authority"
            )
        signer_required = (
            self.publication_signer_executable,
            self.publication_signer_executable_sha256,
            self.publication_signer_key_id,
        )
        if any(value is not None for value in signer_required) and any(
            value is None for value in signer_required
        ):
            raise ProductionConfigurationError(
                "publication freshness signer configuration is incomplete"
            )
        if (
            (self.publication_signer_adapter is None)
            != (self.publication_signer_adapter_sha256 is None)
            or (
                self.publication_signer_executable is not None
                and not self.publication_signer_executable.is_absolute()
            )
            or (
                self.publication_signer_executable_sha256 is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", self.publication_signer_executable_sha256
                )
                is None
            )
            or (
                self.publication_signer_adapter is not None
                and not self.publication_signer_adapter.is_absolute()
            )
            or (
                self.publication_signer_adapter_sha256 is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", self.publication_signer_adapter_sha256
                )
                is None
            )
            or (
                self.publication_signer_key_id is not None
                and self.publication_signer_key_id not in publication_keys
            )
        ):
            raise ProductionConfigurationError(
                "publication freshness signer trust binding is invalid"
            )
        rollback_required = (
            self.rollback_signer_executable,
            self.rollback_signer_executable_sha256,
            self.rollback_signer_key_id,
        )
        if any(value is not None for value in rollback_required) and any(
            value is None for value in rollback_required
        ):
            raise ProductionConfigurationError(
                "rollback signer configuration is incomplete"
            )
        if (
            (self.rollback_signer_adapter is None)
            != (self.rollback_signer_adapter_sha256 is None)
            or (
                self.rollback_signer_executable is not None
                and not self.rollback_signer_executable.is_absolute()
            )
            or (
                self.rollback_signer_executable_sha256 is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", self.rollback_signer_executable_sha256
                )
                is None
            )
            or (
                self.rollback_signer_adapter is not None
                and not self.rollback_signer_adapter.is_absolute()
            )
            or (
                self.rollback_signer_adapter_sha256 is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}", self.rollback_signer_adapter_sha256
                )
                is None
            )
            or (
                self.rollback_signer_key_id is not None
                and self.rollback_signer_key_id not in rollback_keys
            )
        ):
            raise ProductionConfigurationError(
                "rollback signer trust binding is invalid"
            )
        origins = dict(self.model_provider_origins)
        if self.admin_management_enabled:
            allowed_presets = {
                "ecorex_chat",
                "deepseek_chat",
                "gemini_chat",
                "doubao_chat",
                "ecorex_image",
            }
            if not origins or any(
                preset not in allowed_presets
                or urlsplit(origin).scheme != "https"
                or not urlsplit(origin).hostname
                or urlsplit(origin).username is not None
                or urlsplit(origin).password is not None
                or bool(urlsplit(origin).query or urlsplit(origin).fragment)
                or bool(urlsplit(origin).path.rstrip("/"))
                or urlsplit(origin).port not in {None, 443}
                for preset, origin in origins.items()
            ):
                raise ProductionConfigurationError(
                    "managed model provider origins are invalid"
                )
        elif origins:
            raise ProductionConfigurationError(
                "managed model origins require administrator management"
            )
        try:
            validate_provider_ca_binding(
                origins.values(),
                ca_bundle_path=self.model_provider_ca_bundle_path,
                ca_bundle_sha256=self.model_provider_ca_bundle_sha256,
            )
        except ProviderTLSConfigurationError:
            raise ProductionConfigurationError(
                "managed model provider CA binding is invalid"
            ) from None
        if self.device_identity_enabled != (self.device_identity is not None):
            raise ProductionConfigurationError(
                "managed device identity configuration is incomplete"
            )
        if self.device_identity_enabled and not self.admin_management_enabled:
            raise ProductionConfigurationError(
                "managed device identity requires administrator management"
            )
        if (
            self.device_identity is not None
            and self.device_identity.database_path != self.database_path
        ):
            raise ProductionConfigurationError(
                "managed device identity must share Control Plane storage"
            )
        if self.device_identity is not None and (
            self.device_identity.issuer != self.auth_issuer
            or self.device_identity.audience != self.auth_audience
            or auth_keys.get(self.device_identity.access_signer.key_id)
            != self.device_identity.access_signer.public_key
        ):
            raise ProductionConfigurationError(
                "managed device identity access tokens are not trusted by Control Plane"
            )
        if self.release_replica_enabled:
            if (
                self.release_replica_storage_root
                != PRODUCTION_RELEASE_REPLICA_ROOT
                or self.release_replica_public_root
                != PRODUCTION_RELEASE_REPLICA_PUBLIC_ROOT
                or self.release_replica_product_version != __version__
                or self.release_replica_namespace != f"v{__version__}"
                or not 1
                <= self.release_replica_max_asset_bytes
                <= MAX_ARTIFACT_BYTES
            ):
                raise ProductionConfigurationError(
                    "production CDN release replica fence is invalid"
                )
        elif (
            self.release_replica_storage_root is not None
            or self.release_replica_public_root is not None
            or self.release_replica_namespace is not None
            or self.release_replica_product_version is not None
            or self.release_replica_max_asset_bytes != MAX_ARTIFACT_BYTES
        ):
            raise ProductionConfigurationError(
                "CDN release replica settings require the service to be enabled"
            )
        object.__setattr__(self, "model_provider_origins", dict(origins))

    def _share_storage_is_valid(self) -> bool:
        local_values = (
            self.local_cas_root,
            self.local_cas_attestation_path,
            self.local_cas_attestation_sha256,
            self.local_cas_volume_id,
            self.local_cas_machine_id_sha256,
            self.local_cas_owner_gid,
        )
        if self.share_storage_mode == "s3":
            endpoint = urlsplit(self.s3_endpoint_url) if self.s3_endpoint_url else None
            return bool(
                _BUCKET.fullmatch(self.s3_bucket)
                and _PREFIX.fullmatch(self.s3_prefix)
                and not any(
                    part in {"", ".", ".."} for part in self.s3_prefix.split("/")
                )
                and isinstance(self.s3_region, str)
                and self.s3_region
                and self.s3_addressing_style in {"virtual", "path"}
                and 4 <= self.s3_max_connections <= 256
                and (endpoint is None or endpoint.scheme == "https")
                and (endpoint is None or bool(endpoint.hostname))
                and (endpoint is None or endpoint.username is None)
                and (endpoint is None or endpoint.password is None)
                and (endpoint is None or not bool(endpoint.query or endpoint.fragment))
                and all(value is None for value in local_values)
                and self.local_cas_replica_count == 1
                and self.local_cas_quota_bytes == 0
                and self.local_cas_minimum_free_bytes == 0
                and self.local_cas_max_object_bytes == 0
                and self.local_cas_max_open_streams == 0
            )
        if self.share_storage_mode != "attested-encrypted-local-cas":
            return False
        return bool(
            self.s3_bucket == ""
            and self.s3_prefix == ""
            and self.s3_region == ""
            and self.s3_endpoint_url is None
            and isinstance(self.local_cas_root, Path)
            and self.local_cas_root.is_absolute()
            and isinstance(self.local_cas_attestation_path, Path)
            and self.local_cas_attestation_path.is_absolute()
            and re.fullmatch(
                r"[0-9a-f]{64}", str(self.local_cas_attestation_sha256)
            )
            and _SAFE_ID.fullmatch(str(self.local_cas_volume_id))
            and self.local_cas_volume_id == self.storage_volume_id
            and re.fullmatch(
                r"[0-9a-f]{64}", str(self.local_cas_machine_id_sha256)
            )
            and self.local_cas_replica_count == self.replica_count == 1
            and 1024 * 1024
            <= self.local_cas_minimum_free_bytes
            <= self.local_cas_quota_bytes
            <= 8 * 1024**4
            and isinstance(self.local_cas_owner_gid, int)
            and not isinstance(self.local_cas_owner_gid, bool)
            and 0 <= self.local_cas_owner_gid <= 2**31 - 1
            and 1024 <= self.local_cas_max_object_bytes <= 256 * 1024 * 1024
            and 1 <= self.local_cas_max_open_streams <= 1024
        )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "ControlPlaneProductionConfig":
        values = os.environ if environment is None else environment
        backend = _required(values, "ECOREX_CP_STORAGE_BACKEND")
        replicas = _integer(values, "ECOREX_CP_REPLICA_COUNT", minimum=1, maximum=128)
        # v1 has no first-party PostgreSQL Control Plane repository.  This is a
        # hard boundary, not a hidden SQLite fallback for a requested HA setup.
        if backend != "sqlite-wal" or replicas != 1:
            raise ProductionConfigurationError(
                "this Control Plane build supports only single-node SQLite WAL"
            )
        database = _absolute_path(values, "ECOREX_CP_DATABASE_PATH")
        backup = _absolute_path(values, "ECOREX_CP_BACKUP_DIRECTORY")
        spool = _absolute_path(values, "ECOREX_CP_SHARE_SPOOL_DIRECTORY")
        volume_id = _required(values, "ECOREX_CP_STORAGE_VOLUME_ID")
        if _SAFE_ID.fullmatch(volume_id) is None:
            raise ProductionConfigurationError(
                "Control Plane volume identity is invalid"
            )
        encryption = _boolean(values, "ECOREX_CP_STORAGE_ENCRYPTION_AT_REST")
        if not encryption:
            raise ProductionConfigurationError(
                "encrypted-at-rest production storage is required"
            )

        public_url = _required(values, "ECOREX_CP_PUBLIC_SHARE_BASE_URL").rstrip("/")
        parsed_public = urlsplit(public_url)
        if (
            parsed_public.scheme != "https"
            or not parsed_public.hostname
            or parsed_public.port not in {None, 443}
            or parsed_public.username
            or parsed_public.password
            or parsed_public.query
            or parsed_public.fragment
            or parsed_public.path.rstrip("/") != "/s"
        ):
            raise ProductionConfigurationError("public Share URL is invalid")

        bootstrap_url = _required(values, "ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_URL")
        parsed_bootstrap = urlsplit(bootstrap_url)
        bootstrap_hosts = tuple(
            sorted(
                {
                    item.strip().casefold().rstrip(".")
                    for item in _required(
                        values, "ECOREX_CP_PUBLIC_BOOTSTRAP_READBACK_HOSTS"
                    ).split(",")
                    if item.strip()
                }
            )
        )
        if (
            parsed_bootstrap.scheme != "https"
            or not parsed_bootstrap.hostname
            or parsed_bootstrap.port not in {None, 443}
            or parsed_bootstrap.username
            or parsed_bootstrap.password
            or not parsed_bootstrap.path.endswith("/public-bootstrap-index.json")
            or parsed_bootstrap.query
            or parsed_bootstrap.fragment
            or (parsed_bootstrap.hostname or "").casefold().rstrip(".")
            not in bootstrap_hosts
        ):
            raise ProductionConfigurationError(
                "public Bootstrap index configuration is invalid"
            )

        share_storage_mode = values.get("ECOREX_CP_SHARE_STORAGE_MODE", "s3")
        if share_storage_mode not in {"s3", "attested-encrypted-local-cas"}:
            raise ProductionConfigurationError("Share storage mode is invalid")
        if share_storage_mode == "attested-encrypted-local-cas" and any(
            name in values for name in _S3_SETTING_NAMES
        ):
            raise ProductionConfigurationError(
                "Share storage configuration is ambiguous"
            )
        endpoint = (
            values.get("ECOREX_CP_S3_ENDPOINT_URL") or None
            if share_storage_mode == "s3"
            else None
        )
        if share_storage_mode == "s3" and endpoint is not None:
            parsed_endpoint = urlsplit(endpoint)
            if (
                parsed_endpoint.scheme != "https"
                or not parsed_endpoint.hostname
                or parsed_endpoint.username
                or parsed_endpoint.password
                or parsed_endpoint.query
                or parsed_endpoint.fragment
            ):
                raise ProductionConfigurationError(
                    "S3 endpoint configuration is invalid"
                )
        bucket = (
            _required(values, "ECOREX_CP_S3_BUCKET")
            if share_storage_mode == "s3"
            else ""
        )
        prefix = (
            _required(values, "ECOREX_CP_S3_PREFIX").strip("/")
            if share_storage_mode == "s3"
            else ""
        )
        addressing = values.get("ECOREX_CP_S3_ADDRESSING_STYLE", "virtual")
        if share_storage_mode == "s3" and (
            _BUCKET.fullmatch(bucket) is None
            or _PREFIX.fullmatch(prefix) is None
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
            or addressing not in {"virtual", "path"}
        ):
            raise ProductionConfigurationError("S3 namespace configuration is invalid")

        bind_host = values.get("ECOREX_CP_BIND_HOST", "127.0.0.1")
        try:
            address = ipaddress.ip_address(bind_host)
        except ValueError:
            raise ProductionConfigurationError(
                "Control Plane bind host must be an IP address"
            ) from None
        ingress = _boolean(
            values, "ECOREX_CP_ALLOW_TRUSTED_INGRESS_HTTP", default=False
        )
        if not address.is_loopback and not ingress:
            raise ProductionConfigurationError(
                "non-loopback HTTP requires an explicit trusted-ingress boundary"
            )

        raw_days = _integer(
            values, "ECOREX_CP_AUDIT_RAW_DAYS", minimum=1, maximum=3650, default=30
        )
        aggregate_days = _integer(
            values,
            "ECOREX_CP_AUDIT_AGGREGATE_DAYS",
            minimum=raw_days,
            maximum=3650,
            default=180,
        )
        interval = _integer(
            values,
            "ECOREX_CP_BACKUP_INTERVAL_SECONDS",
            minimum=900,
            maximum=24 * 60 * 60,
            default=6 * 60 * 60,
        )
        maximum_age = _integer(
            values,
            "ECOREX_CP_MAXIMUM_BACKUP_AGE_SECONDS",
            minimum=interval,
            maximum=7 * 24 * 60 * 60,
            default=24 * 60 * 60,
        )
        instance_id = _required(values, "ECOREX_CP_INSTANCE_ID")
        if _SAFE_ID.fullmatch(instance_id) is None:
            raise ProductionConfigurationError(
                "Control Plane instance identity is invalid"
            )
        management_enabled = _boolean(
            values, "ECOREX_CP_ADMIN_MANAGEMENT_ENABLED", default=False
        )
        device_identity_enabled = _boolean(
            values, "ECOREX_CP_DEVICE_IDENTITY_ENABLED", default=False
        )
        release_replica_enabled = _boolean(
            values, "ECOREX_CP_RELEASE_REPLICA_ENABLED", default=False
        )
        direct_release_enabled = _boolean(
            values, "ECOREX_CP_DIRECT_RELEASE_ADMISSION_ENABLED", default=False
        )
        direct_release_id = values.get("ECOREX_CP_DIRECT_RELEASE_ID") or None
        direct_instruction = (
            values.get("ECOREX_CP_DIRECT_RELEASE_INSTRUCTION_SHA256") or None
        )
        if not direct_release_enabled and (
            direct_release_id is not None or direct_instruction is not None
        ):
            raise ProductionConfigurationError(
                "disabled direct release admission must not retain authority"
            )
        origins: dict[str, str] = {}
        raw_origins = values.get("ECOREX_CP_MODEL_PROVIDER_ORIGINS_JSON")
        if raw_origins:
            try:
                parsed_origins = json.loads(raw_origins)
            except json.JSONDecodeError:
                raise ProductionConfigurationError(
                    "managed model provider origins are invalid"
                ) from None
            if (
                not isinstance(parsed_origins, dict)
                or not parsed_origins
                or len(parsed_origins) > 8
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in parsed_origins.items()
                )
            ):
                raise ProductionConfigurationError(
                    "managed model provider origins are invalid"
                )
            origins = {str(key): str(value) for key, value in parsed_origins.items()}
        return cls(
            storage_backend=backend,
            replica_count=replicas,
            database_path=database,
            backup_directory=backup,
            storage_volume_id=volume_id,
            storage_encryption_at_rest=encryption,
            minimum_free_bytes=_integer(
                values,
                "ECOREX_CP_MINIMUM_FREE_BYTES",
                minimum=64 * 1024 * 1024,
                maximum=1024 * 1024 * 1024 * 1024,
                default=512 * 1024 * 1024,
            ),
            backup_interval_seconds=interval,
            maximum_backup_age_seconds=maximum_age,
            backup_retain_count=_integer(
                values,
                "ECOREX_CP_BACKUP_RETAIN_COUNT",
                minimum=2,
                maximum=365,
                default=14,
            ),
            public_share_base_url=public_url,
            share_spool_directory=spool,
            share_storage_mode=share_storage_mode,
            s3_bucket=bucket,
            s3_prefix=prefix,
            s3_region=(
                _required(values, "ECOREX_CP_S3_REGION")
                if share_storage_mode == "s3"
                else ""
            ),
            s3_endpoint_url=endpoint,
            s3_addressing_style=addressing,
            s3_max_connections=(
                _integer(
                    values,
                    "ECOREX_CP_S3_MAX_CONNECTIONS",
                    minimum=4,
                    maximum=256,
                    default=32,
                )
                if share_storage_mode == "s3"
                else 0
            ),
            local_cas_root=(
                _absolute_path(values, "ECOREX_CP_LOCAL_CAS_ROOT")
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_attestation_path=(
                _absolute_path(values, "ECOREX_CP_LOCAL_CAS_ATTESTATION_PATH")
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_attestation_sha256=(
                _required(values, "ECOREX_CP_LOCAL_CAS_ATTESTATION_SHA256")
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_volume_id=(
                _required(values, "ECOREX_CP_LOCAL_CAS_VOLUME_ID")
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_machine_id_sha256=(
                _required(values, "ECOREX_CP_LOCAL_CAS_MACHINE_ID_SHA256")
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_replica_count=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_REPLICA_COUNT", minimum=1, maximum=1, default=1)
                if share_storage_mode == "attested-encrypted-local-cas"
                else 1
            ),
            local_cas_quota_bytes=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_QUOTA_BYTES", minimum=1024 * 1024, maximum=8 * 1024**4, default=256 * 1024**3)
                if share_storage_mode == "attested-encrypted-local-cas"
                else 0
            ),
            local_cas_minimum_free_bytes=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_MINIMUM_FREE_BYTES", minimum=1024 * 1024, maximum=8 * 1024**4, default=10 * 1024**3)
                if share_storage_mode == "attested-encrypted-local-cas"
                else 0
            ),
            local_cas_owner_gid=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_OWNER_GID", minimum=0, maximum=2**31 - 1, default=-1)
                if share_storage_mode == "attested-encrypted-local-cas"
                else None
            ),
            local_cas_max_object_bytes=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_MAX_OBJECT_BYTES", minimum=1024, maximum=256 * 1024 * 1024, default=64 * 1024 * 1024)
                if share_storage_mode == "attested-encrypted-local-cas"
                else 0
            ),
            local_cas_max_open_streams=(
                _integer(values, "ECOREX_CP_LOCAL_CAS_MAX_OPEN_STREAMS", minimum=1, maximum=1024, default=32)
                if share_storage_mode == "attested-encrypted-local-cas"
                else 0
            ),
            auth_issuer=_required(values, "ECOREX_CP_AUTH_ISSUER"),
            auth_audience=_required(values, "ECOREX_CP_AUTH_AUDIENCE"),
            auth_public_keys_json=_required(values, "ECOREX_CP_AUTH_PUBLIC_KEYS_JSON"),
            release_public_keys_json=_required(
                values, "ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON"
            ),
            publication_public_keys_json=_required(
                values, "ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON"
            ),
            rollback_signer_public_keys_json=_required(
                values, "ECOREX_CP_ROLLBACK_SIGNER_PUBLIC_KEYS_JSON"
            ),
            public_bootstrap_index_path=_absolute_path(
                values, "ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_PATH"
            ),
            public_bootstrap_index_url=bootstrap_url,
            public_bootstrap_readback_hosts=bootstrap_hosts,
            auth_max_token_lifetime_seconds=_integer(
                values,
                "ECOREX_CP_AUTH_MAX_TOKEN_LIFETIME_SECONDS",
                minimum=60,
                maximum=3600,
                default=900,
            ),
            auth_clock_skew_seconds=_integer(
                values,
                "ECOREX_CP_AUTH_CLOCK_SKEW_SECONDS",
                minimum=0,
                maximum=120,
                default=30,
            ),
            audit_raw_days=raw_days,
            audit_aggregate_days=aggregate_days,
            maintenance_interval_seconds=_integer(
                values,
                "ECOREX_CP_MAINTENANCE_INTERVAL_SECONDS",
                minimum=60,
                maximum=24 * 60 * 60,
                default=60 * 60,
            ),
            instance_id=instance_id,
            bind_host=bind_host,
            bind_port=_integer(
                values, "ECOREX_CP_BIND_PORT", minimum=1024, maximum=65535, default=8443
            ),
            allow_trusted_ingress_http=ingress,
            dependency_timeout_seconds=_integer(
                values,
                "ECOREX_CP_DEPENDENCY_TIMEOUT_SECONDS",
                minimum=1,
                maximum=30,
                default=5,
            ),
            readiness_cache_seconds=_integer(
                values,
                "ECOREX_CP_READINESS_CACHE_SECONDS",
                minimum=1,
                maximum=60,
                default=15,
            ),
            graceful_shutdown_seconds=_integer(
                values,
                "ECOREX_CP_GRACEFUL_SHUTDOWN_SECONDS",
                minimum=5,
                maximum=300,
                default=30,
            ),
            limit_concurrency=_integer(
                values,
                "ECOREX_CP_LIMIT_CONCURRENCY",
                minimum=16,
                maximum=4096,
                default=512,
            ),
            backlog=_integer(
                values, "ECOREX_CP_BACKLOG", minimum=16, maximum=8192, default=1024
            ),
            signal_poll_interval_seconds=_float(
                values,
                "ECOREX_CP_SIGNAL_POLL_INTERVAL_SECONDS",
                minimum=0.05,
                maximum=5.0,
                default=0.25,
            ),
            signal_retention_seconds=_integer(
                values,
                "ECOREX_CP_SIGNAL_RETENTION_SECONDS",
                minimum=60,
                maximum=90 * 24 * 60 * 60,
                default=7 * 24 * 60 * 60,
            ),
            signal_retain_latest=_integer(
                values,
                "ECOREX_CP_SIGNAL_RETAIN_LATEST",
                minimum=1,
                maximum=100_000,
                default=1024,
            ),
            bootstrap_freshness_automation_enabled=_boolean(
                values,
                "ECOREX_CP_BOOTSTRAP_FRESHNESS_AUTOMATION_ENABLED",
                default=True,
            ),
            bootstrap_freshness_lead_seconds=_integer(
                values,
                "ECOREX_CP_BOOTSTRAP_FRESHNESS_LEAD_SECONDS",
                minimum=60 * 60,
                maximum=23 * 60 * 60,
                default=8 * 60 * 60,
            ),
            bootstrap_freshness_check_interval_seconds=_integer(
                values,
                "ECOREX_CP_BOOTSTRAP_FRESHNESS_CHECK_INTERVAL_SECONDS",
                minimum=5 * 60,
                maximum=6 * 60 * 60,
                default=60 * 60,
            ),
            bootstrap_freshness_lease_seconds=_integer(
                values,
                "ECOREX_CP_BOOTSTRAP_FRESHNESS_LEASE_SECONDS",
                minimum=5 * 60,
                maximum=30 * 60,
                default=10 * 60,
            ),
            publication_signer_executable=_optional_absolute_path(
                values, "ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE"
            ),
            publication_signer_executable_sha256=(
                values.get("ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE_SHA256") or None
            ),
            publication_signer_adapter=_optional_absolute_path(
                values, "ECOREX_CP_PUBLICATION_SIGNER_ADAPTER"
            ),
            publication_signer_adapter_sha256=(
                values.get("ECOREX_CP_PUBLICATION_SIGNER_ADAPTER_SHA256") or None
            ),
            publication_signer_key_id=(
                values.get("ECOREX_CP_PUBLICATION_SIGNER_KEY_ID") or None
            ),
            publication_signer_timeout_seconds=_integer(
                values,
                "ECOREX_CP_PUBLICATION_SIGNER_TIMEOUT_SECONDS",
                minimum=1,
                maximum=120,
                default=30,
            ),
            rollback_signer_executable=_optional_absolute_path(
                values, "ECOREX_CP_ROLLBACK_SIGNER_EXECUTABLE"
            ),
            rollback_signer_executable_sha256=(
                values.get("ECOREX_CP_ROLLBACK_SIGNER_EXECUTABLE_SHA256") or None
            ),
            rollback_signer_adapter=_optional_absolute_path(
                values, "ECOREX_CP_ROLLBACK_SIGNER_ADAPTER"
            ),
            rollback_signer_adapter_sha256=(
                values.get("ECOREX_CP_ROLLBACK_SIGNER_ADAPTER_SHA256") or None
            ),
            rollback_signer_key_id=(
                values.get("ECOREX_CP_ROLLBACK_SIGNER_KEY_ID") or None
            ),
            rollback_signer_timeout_seconds=_integer(
                values,
                "ECOREX_CP_ROLLBACK_SIGNER_TIMEOUT_SECONDS",
                minimum=1,
                maximum=120,
                default=30,
            ),
            admin_management_enabled=management_enabled,
            model_provider_origins=origins,
            model_provider_ca_bundle_path=_optional_absolute_path(
                values, "ECOREX_CP_MODEL_PROVIDER_CA_BUNDLE_PATH"
            ),
            model_provider_ca_bundle_sha256=(
                values.get("ECOREX_CP_MODEL_PROVIDER_CA_BUNDLE_SHA256") or None
            ),
            model_activation_timeout_seconds=_integer(
                values,
                "ECOREX_CP_MODEL_ACTIVATION_TIMEOUT_SECONDS",
                minimum=30,
                maximum=600,
                default=180,
            ),
            device_identity_enabled=device_identity_enabled,
            device_identity=(
                DeviceIdentityProductionConfig.from_environment(values)
                if device_identity_enabled
                else None
            ),
            release_replica_enabled=release_replica_enabled,
            release_replica_storage_root=(
                (
                    PRODUCTION_RELEASE_REPLICA_ROOT
                    if _required(
                        values, "ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT"
                    )
                    == PRODUCTION_RELEASE_REPLICA_ROOT.as_posix()
                    else Path(
                        _required(
                            values, "ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT"
                        )
                    )
                )
                if release_replica_enabled
                else None
            ),
            release_replica_public_root=(
                _required(values, "ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT").rstrip("/")
                if release_replica_enabled
                else None
            ),
            release_replica_namespace=(
                _required(values, "ECOREX_CP_RELEASE_REPLICA_NAMESPACE")
                if release_replica_enabled
                else None
            ),
            release_replica_product_version=(
                _required(values, "ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION")
                if release_replica_enabled
                else None
            ),
            release_replica_max_asset_bytes=(
                _integer(
                    values,
                    "ECOREX_CP_RELEASE_REPLICA_MAX_ASSET_BYTES",
                    minimum=1,
                    maximum=MAX_ARTIFACT_BYTES,
                    default=MAX_ARTIFACT_BYTES,
                )
                if release_replica_enabled
                else MAX_ARTIFACT_BYTES
            ),
            direct_release_admission_enabled=direct_release_enabled,
            direct_release_id=(direct_release_id if direct_release_enabled else None),
            direct_release_instruction_sha256=(
                direct_instruction if direct_release_enabled else None
            ),
            feishu_connector_enabled=_boolean(
                values,
                "ECOREX_CP_FEISHU_CONNECTOR_ENABLED",
                default=False,
            ),
            wechat_callback_enabled=_boolean(
                values,
                "ECOREX_CP_WECHAT_CALLBACK_ENABLED",
                default=False,
            ),
            skill_hub_auth_issuer=(
                values.get("ECOREX_CP_SKILL_HUB_AUTH_ISSUER") or None
            ),
            skill_hub_auth_audience=(
                values.get("ECOREX_CP_SKILL_HUB_AUTH_AUDIENCE") or None
            ),
            skill_hub_auth_public_keys_json=(
                values.get("ECOREX_CP_SKILL_HUB_AUTH_PUBLIC_KEYS_JSON") or None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProductionSchemaReport:
    schema_version: int
    storage_backend: str
    control_schema_version: int
    audit_schema_version: int
    share_schema_version: int
    admin_management_schema_version: int
    direct_admission_schema_version: int
    backup: BackupReceipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "storage_backend": self.storage_backend,
            "control_schema_version": self.control_schema_version,
            "audit_schema_version": self.audit_schema_version,
            "share_schema_version": self.share_schema_version,
            "admin_management_schema_version": self.admin_management_schema_version,
            "direct_admission_schema_version": self.direct_admission_schema_version,
            "backup": self.backup.to_dict(),
        }


class _S3Dependency:
    def __init__(
        self, client: ProductionS3Client, config: ControlPlaneProductionConfig
    ) -> None:
        self.client = client
        self.bucket = config.s3_bucket
        self.prefix = config.s3_prefix
        self._closed = False

    def validate_controls(self, *, write_probe: bool) -> None:
        if self._closed:
            raise ProductionConfigurationError("S3 dependency is closed")
        self.client.head_bucket(Bucket=self.bucket)
        encryption = self.client.get_bucket_encryption(Bucket=self.bucket)
        rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        if not isinstance(rules, list) or not any(
            isinstance(rule, Mapping)
            and rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            in {"AES256", "aws:kms"}
            for rule in rules
        ):
            raise ProductionConfigurationError("S3 bucket encryption is not enforced")
        public = self.client.get_public_access_block(Bucket=self.bucket)
        block = public.get("PublicAccessBlockConfiguration", {})
        if not isinstance(block, Mapping) or not all(
            block.get(name) is True
            for name in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        ):
            raise ProductionConfigurationError("S3 public access is not fully blocked")
        if write_probe:
            self._write_probe()

    def ping(self) -> None:
        if self._closed:
            raise ProductionConfigurationError("S3 dependency is closed")
        self.client.head_bucket(Bucket=self.bucket)

    def _write_probe(self) -> None:
        probe_id = uuid.uuid4().hex
        key = f"{self.prefix}/_health/{probe_id}"
        content = b"ecorex-control-plane-s3-health-v1"
        created = False
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentLength=len(content),
                ContentType="application/octet-stream",
                Metadata={"ecorex-contract": "control-plane-health-v1"},
                IfNoneMatch="*",
            )
            created = True
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            if int(head.get("ContentLength", -1)) != len(content) or not isinstance(
                head.get("ETag"), str
            ):
                raise ProductionConfigurationError("S3 write/read probe is invalid")
        finally:
            if created:
                self.client.delete_object(Bucket=self.bucket, Key=key)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.client.close()


class _AttestedLocalShareDependency:
    def __init__(self, store: AttestedLocalShareObjectStore) -> None:
        if not isinstance(store, AttestedLocalShareObjectStore):
            raise TypeError("attested Share CAS store is invalid")
        self.store = store
        self._closed = False

    def validate_controls(self, *, write_probe: bool) -> None:
        receipt = self._health(write_probe=write_probe)
        if (
            receipt.get("status") != "passed"
            or receipt.get("backend") != "attested-encrypted-local-cas"
            or receipt.get("availability_scope") != "single-host"
            or receipt.get("multi_host_ha") is not False
            or receipt.get("replica_count") != 1
        ):
            raise ProductionConfigurationError(
                "attested Share CAS health contract is invalid"
            )

    def ping(self) -> None:
        self._health(write_probe=False)

    def _health(self, *, write_probe: bool) -> Mapping[str, object]:
        if self._closed:
            raise ProductionConfigurationError(
                "attested Share CAS dependency is closed"
            )
        try:
            return self.store.health_probe(write_probe=write_probe, deep=False)
        except Exception:
            raise ProductionConfigurationError(
                "attested Share CAS dependency is unavailable"
            ) from None

    def close(self) -> None:
        self._closed = True


@dataclass(slots=True)
class ControlPlaneProductionBundle:
    repository: ControlPlaneRepository
    bootstrap_index_service: BootstrapIndexPublicationService
    bootstrap_freshness_refresher: BootstrapFreshnessRefresher
    share_repository: CloudShareRepository
    audit_repository: CloudAuditRepository
    authenticator: Ed25519JWTAuthenticator
    rollback_signer: ReleaseSigner | None
    management_repository: AdminManagementRepository | None
    model_connection_tester: HTTPSModelConnectionTester | None
    device_identity_broker: ManagedDeviceIdentityBroker | None
    release_replica_service: CDNReleaseReplicaService | None
    skill_hub_registry: SkillHubRegistry
    skill_hub_bundle_store: LocalSkillBundleStore
    skill_hub_authenticator: EMateSessionJWTAuthenticator | None
    feishu_connector_gateway: FeishuConnectorGateway | None
    wechat_callback_gateway: WechatCallbackGateway | None
    lifecycle: "SingleNodeControlPlaneLifecycle"
    config: ControlPlaneProductionConfig

    def create_app(self):
        return create_control_plane_app(
            self.repository,
            authenticator=self.authenticator,
            share_repository=self.share_repository,
            audit_repository=self.audit_repository,
            signal_consumer_id=self.config.instance_id,
            signal_poll_interval_seconds=self.config.signal_poll_interval_seconds,
            signal_retention_seconds=self.config.signal_retention_seconds,
            signal_retain_latest=self.config.signal_retain_latest,
            service_lifecycle=self.lifecycle,
            bootstrap_index_service=self.bootstrap_index_service,
            bootstrap_freshness_refresher=self.bootstrap_freshness_refresher,
            rollback_signer=self.rollback_signer,
            management_repository=self.management_repository,
            model_connection_tester=self.model_connection_tester,
            device_identity_broker=self.device_identity_broker,
            release_replica_service=self.release_replica_service,
            skill_hub_registry=self.skill_hub_registry,
            skill_hub_bundle_store=self.skill_hub_bundle_store,
            skill_hub_authenticator=self.skill_hub_authenticator,
            feishu_connector_gateway=self.feishu_connector_gateway,
            wechat_callback_gateway=self.wechat_callback_gateway,
        )


class SingleNodeControlPlaneLifecycle(ControlPlaneServiceLifecycle):
    def __init__(
        self,
        *,
        config: ControlPlaneProductionConfig,
        instance_lock: ControlPlaneInstanceLock,
        volume: PersistentVolumeGuard,
        backup: SQLiteBackupManager,
        keyring: CloudShareKeyRing,
        storage: ShareStorageDependency,
        audit_repository: CloudAuditRepository,
        share_repository: CloudShareRepository,
        bootstrap_index_service: BootstrapIndexPublicationService,
        release_replica_service: CDNReleaseReplicaService | None,
    ) -> None:
        self.config = config
        self.instance_lock = instance_lock
        self.volume = volume
        self.backup = backup
        self.keyring = keyring
        self.storage = storage
        self.audit_repository = audit_repository
        self.share_repository = share_repository
        self.bootstrap_index_service = bootstrap_index_service
        self.release_replica_service = release_replica_service
        self._accepting = False
        self._live = False
        self._closed = False
        self._backup_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._ready_lock = asyncio.Lock()
        self._ready_until = 0.0
        self._ready_value = False
        self._backup_fault = False
        self._maintenance_fault = False

    @property
    def accepting(self) -> bool:
        return self._accepting and not self._closed

    @property
    def live(self) -> bool:
        return self._live and not self._closed

    async def startup(self) -> None:
        if self._closed or not self.instance_lock.held:
            raise ProductionConfigurationError("Control Plane lifecycle cannot start")
        await asyncio.wait_for(
            asyncio.to_thread(self._check_dependencies, True, True),
            timeout=self.config.dependency_timeout_seconds * 4,
        )
        self._live = True
        self._accepting = True
        self._backup_task = asyncio.create_task(
            self._backup_loop(), name="ecorex-control-plane-backup"
        )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="ecorex-control-plane-maintenance"
        )

    async def readiness(self) -> bool:
        if not self.accepting or self._backup_fault or self._maintenance_fault:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() < self._ready_until:
            return self._ready_value
        async with self._ready_lock:
            if loop.time() < self._ready_until:
                return self._ready_value
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._check_dependencies, False, False),
                    timeout=self.config.dependency_timeout_seconds * 3,
                )
                value = True
            except Exception:
                value = False
            self._ready_value = value
            self._ready_until = loop.time() + self.config.readiness_cache_seconds
            return value

    def begin_drain(self) -> None:
        self._accepting = False
        self._ready_value = False
        self._ready_until = 0.0

    async def shutdown(self) -> None:
        if self._closed:
            return
        self.begin_drain()
        task, self._backup_task = self._backup_task, None
        maintenance, self._maintenance_task = self._maintenance_task, None
        tasks = tuple(item for item in (task, maintenance) if item is not None)
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._live = False
        self._closed = True
        try:
            await asyncio.to_thread(self.bootstrap_index_service.close)
        finally:
            try:
                await asyncio.to_thread(self.storage.close)
            finally:
                self.instance_lock.release()

    def force_close(self) -> None:
        """Release composition resources if ASGI startup never ran."""

        self.begin_drain()
        self._live = False
        if not self._closed:
            self._closed = True
            try:
                self.bootstrap_index_service.close()
            finally:
                try:
                    self.storage.close()
                finally:
                    self.instance_lock.release()

    def _check_dependencies(self, write_s3: bool, full_backup: bool) -> None:
        self.volume.validate_wal()
        if full_backup:
            ControlPlaneSchemaManager(self.config.database_path).validate()
            CloudAuditSchemaManager(self.config.database_path).validate()
            CloudShareSchemaManager(
                self.config.database_path, keyring=self.keyring
            ).validate()
            AdminManagementSchemaManager(self.config.database_path).validate()
            if self.config.device_identity_enabled:
                DeviceIdentitySchemaManager(self.config.database_path).validate()
        else:
            _validate_runtime_schema_receipts(self.config.database_path)
        if (
            available_bytes(self.config.database_path.parent)
            < self.config.minimum_free_bytes
        ):
            raise ProductionStorageError("production database volume is low on space")
        if (
            available_bytes(self.config.backup_directory)
            < self.config.minimum_free_bytes
        ):
            raise ProductionStorageError("production backup volume is low on space")
        receipt = self.backup.latest(full_digest=full_backup)
        try:
            _require_recent_backup(receipt, self.config.maximum_backup_age_seconds)
        except ProductionStorageError:
            if full_backup:
                receipt = self.backup.create(reason="scheduled")
                _require_recent_backup(receipt, self.config.maximum_backup_age_seconds)
            else:
                raise
        if write_s3:
            self.storage.validate_controls(write_probe=True)
        else:
            self.storage.ping()
        if self.release_replica_service is not None:
            self.release_replica_service.health_check(write_probe=write_s3)

    async def _backup_loop(self) -> None:
        delay = self.config.backup_interval_seconds
        while True:
            try:
                await asyncio.sleep(delay)
                await asyncio.to_thread(self.backup.create, reason="scheduled")
                self._backup_fault = False
                delay = self.config.backup_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                self._backup_fault = True
                self._ready_value = False
                self._ready_until = 0.0
                delay = min(60, self.config.backup_interval_seconds)

    async def _maintenance_loop(self) -> None:
        delay = self.config.maintenance_interval_seconds
        while True:
            try:
                await asyncio.sleep(delay)
                await asyncio.to_thread(self._run_maintenance)
                self._maintenance_fault = False
                delay = self.config.maintenance_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                self._maintenance_fault = True
                self._ready_value = False
                self._ready_until = 0.0
                delay = min(60, self.config.maintenance_interval_seconds)

    def _run_maintenance(self) -> None:
        actor = ControlPrincipal(
            subject="system:control-plane-maintenance",
            client_id=self.config.instance_id,
            account_id="system",
            roles=frozenset({"audit_admin"}),
        )
        self.audit_repository.enforce_retention(actor)
        self.share_repository.reap_expired_media()


@runtime_checkable
class ControlPlaneProductionProvider(Protocol):
    """Typed seam for a future reviewed PostgreSQL/HA implementation."""

    def migrate(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ProductionSchemaReport: ...

    def check(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ProductionSchemaReport: ...

    def compose(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ControlPlaneProductionBundle: ...


def _configured_publication_signer(
    config: ControlPlaneProductionConfig,
    release_keys: Mapping[str, bytes],
    publication_keys: Mapping[str, bytes],
) -> ReleaseSigner | None:
    if config.publication_signer_executable is None:
        return None
    assert config.publication_signer_executable_sha256 is not None
    assert config.publication_signer_key_id is not None
    signer = DigestPinnedExternalSigner(
        key_id=config.publication_signer_key_id,
        public_key=publication_keys[config.publication_signer_key_id],
        executable_path=config.publication_signer_executable,
        executable_sha256=config.publication_signer_executable_sha256,
        adapter_path=config.publication_signer_adapter,
        adapter_sha256=config.publication_signer_adapter_sha256,
        environment=os.environ,
        timeout_seconds=config.publication_signer_timeout_seconds,
    )
    signer_fingerprint = hashlib.sha256(signer.public_key_bytes).digest()
    if signer_fingerprint in {
        hashlib.sha256(material).digest() for material in release_keys.values()
    }:
        raise ProductionConfigurationError(
            "publication signer aliases an immutable release key"
        )
    return signer


def _configured_rollback_signer(
    config: ControlPlaneProductionConfig,
    release_keys: Mapping[str, bytes],
    publication_keys: Mapping[str, bytes],
    rollback_keys: Mapping[str, bytes],
) -> ReleaseSigner | None:
    if config.rollback_signer_executable is None:
        return None
    assert config.rollback_signer_executable_sha256 is not None
    assert config.rollback_signer_key_id is not None
    signer = DigestPinnedExternalSigner(
        key_id=config.rollback_signer_key_id,
        public_key=rollback_keys[config.rollback_signer_key_id],
        executable_path=config.rollback_signer_executable,
        executable_sha256=config.rollback_signer_executable_sha256,
        adapter_path=config.rollback_signer_adapter,
        adapter_sha256=config.rollback_signer_adapter_sha256,
        environment=os.environ,
        timeout_seconds=config.rollback_signer_timeout_seconds,
    )
    fingerprint = hashlib.sha256(signer.public_key_bytes).digest()
    if fingerprint in {
        hashlib.sha256(material).digest()
        for material in (*release_keys.values(), *publication_keys.values())
    }:
        raise ProductionConfigurationError(
            "rollback signer aliases release or publication trust"
        )
    return signer


def _direct_release_policy(
    config: ControlPlaneProductionConfig,
    release_keys: Mapping[str, bytes],
    publication_keys: Mapping[str, bytes],
) -> DirectReleaseAdmissionPolicy:
    if not config.direct_release_admission_enabled:
        return DirectReleaseAdmissionPolicy()
    return DirectReleaseAdmissionPolicy(
        enabled=True,
        release_id=config.direct_release_id,
        operator_instruction_sha256=config.direct_release_instruction_sha256,
        release_public_keys=dict(release_keys),
        publication_public_keys=dict(publication_keys),
    )


def _feishu_connector_gateway(
    config: ControlPlaneProductionConfig,
    secrets: SecretProvider,
    audit_repository: CloudAuditRepository,
) -> FeishuConnectorGateway:
    if not config.feishu_connector_enabled:
        raise ProductionConfigurationError("Feishu connector is not enabled")
    return FeishuConnectorGateway(
        config.database_path,
        app_id=secrets.read("feishu-app-id"),
        app_secret=secrets.read("feishu-app-secret"),
        encryption_key=_secret_bytes(
            secrets.read("feishu-token-encryption-key"), exact_length=32
        ),
        audit_repository=audit_repository,
    )


def _wechat_callback_gateway(
    config: ControlPlaneProductionConfig,
    secrets: SecretProvider,
    audit_repository: CloudAuditRepository,
) -> WechatCallbackGateway:
    if not config.wechat_callback_enabled:
        raise ProductionConfigurationError("WeChat callback gateway is not enabled")
    return WechatCallbackGateway(
        config.database_path,
        encryption_key=_secret_bytes(
            secrets.read("wechat-callback-encryption-key"), exact_length=32
        ),
        audit_repository=audit_repository,
        public_callback_base_url=(
            "https://dl.ecoremedia.net/api/v1/channels/wechat/callback"
        ),
    )


class SingleNodeSQLiteS3Provider:
    def __init__(
        self,
        s3_factory: S3ClientFactory | None = None,
        *,
        local_cas_factory: ControlPlaneLocalCASFactory | None = None,
    ) -> None:
        self.s3_factory = s3_factory or Boto3S3ClientFactory()
        self.local_cas_factory = (
            local_cas_factory or AttestedLocalControlPlaneCASFactory()
        )

    def migrate(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ProductionSchemaReport:
        self._require_supported(config)
        keyring = _share_keyring(secrets)
        volume, backup = _storage(config)
        instance_lock = ControlPlaneInstanceLock(config.database_path)
        with instance_lock:
            volume.validate_directory()
            existed = config.database_path.exists()
            marker_existed = volume.marker_path.exists()
            pre_backup: BackupReceipt | None = None
            if existed:
                pre_backup = backup.create(reason="pre-migration")
            try:
                control = ControlPlaneSchemaManager(config.database_path).migrate()
                audit = CloudAuditSchemaManager(config.database_path).migrate()
                share = CloudShareSchemaManager(
                    config.database_path, keyring=keyring
                ).migrate()
                management = AdminManagementSchemaManager(
                    config.database_path
                ).migrate()
                if config.device_identity_enabled:
                    DeviceIdentitySchemaManager(config.database_path).migrate()
                if config.feishu_connector_enabled:
                    ConnectorGatewaySchemaManager(config.database_path).migrate()
                if config.wechat_callback_enabled:
                    WechatCallbackSchemaManager(config.database_path).migrate()
                skill_hub_registry = SkillHubRegistry(
                    config.database_path,
                    author_key=_skill_hub_author_key(secrets),
                )
                skill_hub_store = LocalSkillBundleStore(_skill_hub_cas_root(config))
                _converge_skill_hub_seed(skill_hub_registry, skill_hub_store)
                volume.install_or_validate()
                volume.validate_wal()
                post_backup = backup.create(reason="post-migration")
            except BaseException:
                self._rollback_migration(
                    config,
                    backup,
                    existed,
                    pre_backup,
                    volume=volume,
                    marker_existed=marker_existed,
                )
                raise
        return ProductionSchemaReport(
            schema_version=1,
            storage_backend=config.storage_backend,
            control_schema_version=control.migration_version,
            audit_schema_version=audit.migration_version,
            share_schema_version=share.migration_version,
            admin_management_schema_version=management.migration_version,
            direct_admission_schema_version=CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION,
            backup=post_backup,
        )

    def check(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ProductionSchemaReport:
        self._require_supported(config)
        keyring = _share_keyring(secrets)
        volume, backup = _storage(config)
        volume.validate_wal()
        control = ControlPlaneSchemaManager(config.database_path).validate()
        audit = CloudAuditSchemaManager(config.database_path).validate()
        share = CloudShareSchemaManager(
            config.database_path, keyring=keyring
        ).validate()
        management = AdminManagementSchemaManager(config.database_path).validate()
        if config.device_identity_enabled:
            DeviceIdentitySchemaManager(config.database_path).validate()
        if config.feishu_connector_enabled:
            ConnectorGatewaySchemaManager(config.database_path).validate()
        if config.wechat_callback_enabled:
            WechatCallbackSchemaManager(config.database_path).validate()
        skill_hub_registry = SkillHubRegistry(
            config.database_path,
            author_key=_skill_hub_author_key(secrets),
            initialize=False,
        )
        skill_hub_root = _skill_hub_cas_root(config)
        if not skill_hub_root.is_dir():
            raise ProductionConfigurationError("Skill Hub CAS is unavailable")
        skill_hub_store = LocalSkillBundleStore(skill_hub_root, create=False)
        _require_skill_hub_seed(skill_hub_registry, skill_hub_store)
        receipt = backup.latest(full_digest=True)
        _require_recent_backup(receipt, config.maximum_backup_age_seconds)
        audit_encryption = _secret_bytes(
            secrets.read("audit-encryption-key"), exact_length=32
        )
        audit_integrity = _secret_bytes(
            secrets.read("audit-integrity-key"), minimum_length=32, maximum_length=64
        )
        management_key = (
            _secret_bytes(secrets.read("model-config-encryption-key"), exact_length=32)
            if config.admin_management_enabled
            else None
        )
        release_keys = parse_ed25519_public_keyring(config.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            config.publication_public_keys_json
        )
        rollback_keys = parse_ed25519_public_keyring(
            config.rollback_signer_public_keys_json
        )
        auth_keys = parse_ed25519_public_keyring(config.auth_public_keys_json)
        _configured_publication_signer(config, release_keys, publication_keys)
        _configured_rollback_signer(
            config, release_keys, publication_keys, rollback_keys
        )
        storage, object_store = self._share_storage(config)
        try:
            storage.validate_controls(write_probe=True)
            verifier = Ed25519SignatureVerifier(release_keys)
            publication_verifier = Ed25519SignatureVerifier(publication_keys)
            Ed25519JWTAuthenticator(
                auth_keys,
                issuer=config.auth_issuer,
                audience=config.auth_audience,
                max_token_lifetime_seconds=config.auth_max_token_lifetime_seconds,
                clock_skew_seconds=config.auth_clock_skew_seconds,
            )
            ControlPlaneRepository(
                config.database_path,
                verifier=verifier,
                bootstrap_freshness_verifier=publication_verifier,
                direct_release_policy=_direct_release_policy(
                    config, release_keys, publication_keys
                ),
            )
            CloudShareRepository(
                config.database_path,
                keyring=keyring,
                public_base_url=config.public_share_base_url,
                object_store=object_store,
            )
            CloudAuditRepository(
                config.database_path,
                encryption_key=audit_encryption,
                integrity_key=audit_integrity,
                retention=AuditRetentionPolicy(
                    raw_days=config.audit_raw_days,
                    aggregate_days=config.audit_aggregate_days,
                ),
            )
            management_repository = None
            if management_key is not None:
                management_repository = AdminManagementRepository(
                    config.database_path, encryption_key=management_key
                )
            if config.device_identity is not None:
                if management_repository is None:
                    raise ProductionConfigurationError(
                        "managed device identity has no account directory"
                    )
                config.device_identity.compose(
                    management_repository,
                    secrets=_ControlPlaneDeviceIdentitySecrets(secrets),
                    initialize=False,
                )
            if config.feishu_connector_enabled:
                gateway = _feishu_connector_gateway(
                    config,
                    secrets,
                    CloudAuditRepository(
                        config.database_path,
                        encryption_key=audit_encryption,
                        integrity_key=audit_integrity,
                        retention=AuditRetentionPolicy(
                            raw_days=config.audit_raw_days,
                            aggregate_days=config.audit_aggregate_days,
                        ),
                    ),
                )
                asyncio.run(gateway.aclose())
            if config.wechat_callback_enabled:
                gateway = _wechat_callback_gateway(
                    config,
                    secrets,
                    CloudAuditRepository(
                        config.database_path,
                        encryption_key=audit_encryption,
                        integrity_key=audit_integrity,
                        retention=AuditRetentionPolicy(
                            raw_days=config.audit_raw_days,
                            aggregate_days=config.audit_aggregate_days,
                        ),
                    ),
                )
                asyncio.run(gateway.aclose())
        finally:
            storage.close()
        return ProductionSchemaReport(
            schema_version=1,
            storage_backend=config.storage_backend,
            control_schema_version=control.migration_version,
            audit_schema_version=audit.migration_version,
            share_schema_version=share.migration_version,
            admin_management_schema_version=management.migration_version,
            direct_admission_schema_version=CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION,
            backup=receipt,
        )

    def compose(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> ControlPlaneProductionBundle:
        self._require_supported(config)
        keyring = _share_keyring(secrets)
        audit_encryption = _secret_bytes(
            secrets.read("audit-encryption-key"), exact_length=32
        )
        audit_integrity = _secret_bytes(
            secrets.read("audit-integrity-key"), minimum_length=32, maximum_length=64
        )
        management_key = (
            _secret_bytes(secrets.read("model-config-encryption-key"), exact_length=32)
            if config.admin_management_enabled
            else None
        )
        release_keys = parse_ed25519_public_keyring(config.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            config.publication_public_keys_json
        )
        rollback_keys = parse_ed25519_public_keyring(
            config.rollback_signer_public_keys_json
        )
        auth_keys = parse_ed25519_public_keyring(config.auth_public_keys_json)
        volume, backup = _storage(config)
        instance_lock = ControlPlaneInstanceLock(config.database_path)
        instance_lock.acquire()
        storage: ShareStorageDependency | None = None
        bootstrap_index_service: BootstrapIndexPublicationService | None = None
        model_connection_tester: HTTPSModelConnectionTester | None = None
        release_replica_service: CDNReleaseReplicaService | None = None
        feishu_connector_gateway: FeishuConnectorGateway | None = None
        wechat_callback_gateway: WechatCallbackGateway | None = None
        try:
            volume.validate_wal()
            ControlPlaneSchemaManager(config.database_path).validate()
            CloudAuditSchemaManager(config.database_path).validate()
            CloudShareSchemaManager(config.database_path, keyring=keyring).validate()
            AdminManagementSchemaManager(config.database_path).validate()
            if config.device_identity_enabled:
                DeviceIdentitySchemaManager(config.database_path).validate()
            if config.feishu_connector_enabled:
                ConnectorGatewaySchemaManager(config.database_path).validate()
            if config.wechat_callback_enabled:
                WechatCallbackSchemaManager(config.database_path).validate()
            skill_hub_registry = SkillHubRegistry(
                config.database_path,
                author_key=_skill_hub_author_key(secrets),
                initialize=False,
            )
            skill_hub_root = _skill_hub_cas_root(config)
            if not skill_hub_root.is_dir():
                raise ProductionConfigurationError("Skill Hub CAS is unavailable")
            skill_hub_bundle_store = LocalSkillBundleStore(
                skill_hub_root, create=False
            )
            _require_skill_hub_seed(skill_hub_registry, skill_hub_bundle_store)
            backup.latest(full_digest=True)
            storage, object_store = self._share_storage(config)
            verifier = Ed25519SignatureVerifier(release_keys)
            authenticator = Ed25519JWTAuthenticator(
                auth_keys,
                issuer=config.auth_issuer,
                audience=config.auth_audience,
                max_token_lifetime_seconds=config.auth_max_token_lifetime_seconds,
                clock_skew_seconds=config.auth_clock_skew_seconds,
            )
            skill_hub_authenticator = (
                EMateSessionJWTAuthenticator(
                    parse_ed25519_public_keyring(
                        config.skill_hub_auth_public_keys_json
                    ),
                    issuer=config.skill_hub_auth_issuer,
                    audience=config.skill_hub_auth_audience,
                )
                if config.skill_hub_auth_public_keys_json is not None
                and config.skill_hub_auth_issuer is not None
                and config.skill_hub_auth_audience is not None
                else None
            )
            publication_verifier = Ed25519SignatureVerifier(publication_keys)
            online_authorization_signer = _configured_publication_signer(
                config, release_keys, publication_keys
            )
            rollback_signer = _configured_rollback_signer(
                config, release_keys, publication_keys, rollback_keys
            )
            repository = ControlPlaneRepository(
                config.database_path,
                verifier=verifier,
                bootstrap_freshness_verifier=publication_verifier,
                direct_release_policy=_direct_release_policy(
                    config, release_keys, publication_keys
                ),
            )
            bootstrap_index_service = BootstrapIndexPublicationService(
                repository,
                public_url=config.public_bootstrap_index_url,
                object_store=FilesystemPublicIndexObjectStore(
                    config.public_bootstrap_index_path
                ),
                public_reader=HTTPSPublicIndexReader(
                    allowed_hosts=frozenset(config.public_bootstrap_readback_hosts)
                ),
            )
            bootstrap_freshness_refresher = BootstrapFreshnessRefresher(
                repository,
                bootstrap_index_service,
                signer=online_authorization_signer,
                config=BootstrapFreshnessConfig(
                    owner_id=config.instance_id,
                    enabled=config.bootstrap_freshness_automation_enabled,
                    lead_seconds=config.bootstrap_freshness_lead_seconds,
                    check_interval_seconds=(
                        config.bootstrap_freshness_check_interval_seconds
                    ),
                    lease_seconds=config.bootstrap_freshness_lease_seconds,
                ),
            )
            share_repository = CloudShareRepository(
                config.database_path,
                keyring=keyring,
                public_base_url=config.public_share_base_url,
                object_store=object_store,
            )
            audit_repository = CloudAuditRepository(
                config.database_path,
                encryption_key=audit_encryption,
                integrity_key=audit_integrity,
                retention=AuditRetentionPolicy(
                    raw_days=config.audit_raw_days,
                    aggregate_days=config.audit_aggregate_days,
                ),
            )
            feishu_connector_gateway = (
                _feishu_connector_gateway(config, secrets, audit_repository)
                if config.feishu_connector_enabled
                else None
            )
            wechat_callback_gateway = (
                _wechat_callback_gateway(config, secrets, audit_repository)
                if config.wechat_callback_enabled
                else None
            )
            if config.release_replica_enabled:
                assert config.release_replica_storage_root is not None
                assert config.release_replica_public_root is not None
                assert config.release_replica_namespace is not None
                assert config.release_replica_product_version is not None
                token_verifier = EnvironmentRotatingReleaseReplicaTokenVerifier()
                if not token_verifier.configured():
                    raise ProductionConfigurationError(
                        "CDN release replica server credential is unavailable"
                    )
                release_replica_service = CDNReleaseReplicaService(
                    storage_root=config.release_replica_storage_root,
                    public_root=config.release_replica_public_root,
                    release_namespace=config.release_replica_namespace,
                    product_version=config.release_replica_product_version,
                    verifier=verifier,
                    token_verifier=token_verifier,
                    audit_sink=CloudReleaseReplicaAuditSink(audit_repository),
                    max_asset_bytes=config.release_replica_max_asset_bytes,
                )
            management_repository = (
                AdminManagementRepository(
                    config.database_path, encryption_key=management_key
                )
                if management_key is not None
                else None
            )
            model_connection_tester = (
                HTTPSModelConnectionTester(
                    config.model_provider_origins,
                    timeout_seconds=float(config.model_activation_timeout_seconds),
                    ssl_context=pinned_provider_ssl_context(
                        config.model_provider_ca_bundle_path,
                        config.model_provider_ca_bundle_sha256,
                    ),
                )
                if management_repository is not None
                else None
            )
            device_identity_broker = (
                config.device_identity.compose(
                    management_repository,
                    secrets=_ControlPlaneDeviceIdentitySecrets(secrets),
                    initialize=False,
                )
                if config.device_identity is not None
                and management_repository is not None
                else None
            )
            lifecycle = SingleNodeControlPlaneLifecycle(
                config=config,
                instance_lock=instance_lock,
                volume=volume,
                backup=backup,
                keyring=keyring,
                storage=storage,
                audit_repository=audit_repository,
                share_repository=share_repository,
                bootstrap_index_service=bootstrap_index_service,
                release_replica_service=release_replica_service,
            )
            return ControlPlaneProductionBundle(
                repository=repository,
                bootstrap_index_service=bootstrap_index_service,
                bootstrap_freshness_refresher=bootstrap_freshness_refresher,
                share_repository=share_repository,
                audit_repository=audit_repository,
                authenticator=authenticator,
                rollback_signer=rollback_signer,
                management_repository=management_repository,
                model_connection_tester=model_connection_tester,
                device_identity_broker=device_identity_broker,
                release_replica_service=release_replica_service,
                skill_hub_registry=skill_hub_registry,
                skill_hub_bundle_store=skill_hub_bundle_store,
                skill_hub_authenticator=skill_hub_authenticator,
                feishu_connector_gateway=feishu_connector_gateway,
                wechat_callback_gateway=wechat_callback_gateway,
                lifecycle=lifecycle,
                config=config,
            )
        except BaseException:
            if feishu_connector_gateway is not None:
                try:
                    asyncio.run(feishu_connector_gateway.aclose())
                except Exception:
                    pass
            if wechat_callback_gateway is not None:
                try:
                    asyncio.run(wechat_callback_gateway.aclose())
                except Exception:
                    pass
            if model_connection_tester is not None:
                try:
                    asyncio.run(model_connection_tester.aclose())
                except Exception:
                    pass
            if bootstrap_index_service is not None:
                bootstrap_index_service.close()
            if storage is not None:
                storage.close()
            instance_lock.release()
            raise

    def backup_create(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> BackupReceipt:
        del secrets
        self._require_supported(config)
        volume, backup = _storage(config)
        volume.validate_wal()
        with ControlPlaneInstanceLock(config.database_path):
            return backup.create(reason="operator")

    def backup_check(
        self,
        config: ControlPlaneProductionConfig,
        secrets: SecretProvider,
    ) -> BackupReceipt:
        del secrets
        self._require_supported(config)
        volume, backup = _storage(config)
        volume.validate_wal()
        return backup.latest(full_digest=True)

    def _s3(self, config: ControlPlaneProductionConfig) -> _S3Dependency:
        client = self.s3_factory.create(config)
        return _S3Dependency(client, config)

    def _share_storage(
        self, config: ControlPlaneProductionConfig
    ) -> tuple[ShareStorageDependency, ShareObjectStore]:
        if config.share_storage_mode == "attested-encrypted-local-cas":
            return self.local_cas_factory.create(config)
        if config.share_storage_mode != "s3":
            raise ProductionConfigurationError("Share storage mode is invalid")
        dependency = self._s3(config)
        try:
            store = S3ShareObjectStore(
                dependency.client,
                bucket=config.s3_bucket,
                prefix=config.s3_prefix,
                max_open_streams=min(64, config.s3_max_connections),
                max_total_spool_bytes=256 * 1024 * 1024,
                memory_spool_bytes=256 * 1024,
                spool_directory=str(config.share_spool_directory),
            )
            return dependency, store
        except BaseException:
            dependency.close()
            raise

    @staticmethod
    def _require_supported(config: ControlPlaneProductionConfig) -> None:
        if config.storage_backend != "sqlite-wal" or config.replica_count != 1:
            raise ProductionConfigurationError(
                "this provider supports only single-node SQLite WAL"
            )

    @staticmethod
    def _rollback_migration(
        config: ControlPlaneProductionConfig,
        backup: SQLiteBackupManager,
        existed: bool,
        pre_backup: BackupReceipt | None,
        *,
        volume: PersistentVolumeGuard,
        marker_existed: bool,
    ) -> None:
        try:
            if existed and pre_backup is not None:
                backup.restore(pre_backup.backup_id)
            elif not existed:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    target = Path(str(config.database_path) + suffix)
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
            if not marker_existed:
                try:
                    volume.marker_path.unlink()
                except FileNotFoundError:
                    pass
        except Exception as error:
            raise ProductionStorageError(
                "Control Plane migration failed and automatic storage recovery failed"
            ) from error


def _storage(
    config: ControlPlaneProductionConfig,
) -> tuple[PersistentVolumeGuard, SQLiteBackupManager]:
    for directory in (
        config.database_path.parent,
        config.backup_directory,
        config.share_spool_directory,
    ):
        try:
            # ``migrate`` is the sole authority permitted to initialize the
            # durable working directories.  This makes a fresh signed install
            # self-contained while ``serve`` remains read-only because it
            # never calls this helper before the migration boundary.
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise ProductionConfigurationError(
                "Control Plane production directory is unavailable or unsafe"
            ) from None
        if not directory.is_dir() or directory.is_symlink():
            raise ProductionConfigurationError(
                "Control Plane production directory is unavailable or unsafe"
            )
    if available_bytes(config.database_path.parent) < config.minimum_free_bytes:
        raise ProductionStorageError("production database volume is low on space")
    if available_bytes(config.backup_directory) < config.minimum_free_bytes:
        raise ProductionStorageError("production backup volume is low on space")
    return (
        PersistentVolumeGuard(config.database_path, volume_id=config.storage_volume_id),
        SQLiteBackupManager(
            config.database_path,
            config.backup_directory,
            volume_id=config.storage_volume_id,
            retain_count=config.backup_retain_count,
        ),
    )


def _require_recent_backup(receipt: BackupReceipt, maximum_age_seconds: int) -> None:
    try:
        created = datetime.fromisoformat(receipt.created_at).astimezone(UTC)
    except (TypeError, ValueError):
        raise ProductionStorageError("production backup timestamp is invalid") from None
    age = (datetime.now(UTC) - created).total_seconds()
    if age < -300 or age > maximum_age_seconds:
        raise ProductionStorageError("production backup is stale")


def _validate_runtime_schema_receipts(database_path: Path) -> None:
    """Cheap readiness probe after startup performed full catalog validation."""

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro&nofollow=1",
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        expected = (
            (
                "control_schema_migrations",
                CURRENT_CONTROL_PLANE_SCHEMA_VERSION,
                CONTROL_PLANE_MIGRATION_CHECKSUM,
                CONTROL_PLANE_SCHEMA_SHA256,
            ),
            (
                "cloud_audit_schema_migrations",
                CURRENT_CLOUD_AUDIT_SCHEMA_VERSION,
                CLOUD_AUDIT_MIGRATION_CHECKSUM,
                CLOUD_AUDIT_SCHEMA_SHA256,
            ),
            (
                "cloud_share_schema_migrations",
                CURRENT_CLOUD_SHARE_SCHEMA_VERSION,
                CLOUD_SHARE_MIGRATION_CHECKSUM,
                CLOUD_SHARE_SCHEMA_SHA256,
            ),
        )
        for table, version, checksum, digest in expected:
            row = connection.execute(
                f"SELECT version,migration_checksum,target_schema_sha256 "
                f"FROM {table} ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None or tuple(row) != (version, checksum, digest):
                raise ProductionStorageError(
                    "production schema readiness receipt is incompatible"
                )
        management = connection.execute(
            "SELECT version,migration_checksum FROM admin_ops_schema_migrations "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if management is None or tuple(management) != (
            CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION,
            ADMIN_MANAGEMENT_MIGRATION_CHECKSUM,
        ):
            raise ProductionStorageError(
                "production admin management schema receipt is incompatible"
            )
    except sqlite3.Error as error:
        raise ProductionStorageError(
            "production schema readiness receipt is unavailable"
        ) from error
    finally:
        connection.close()


def _skill_hub_cas_root(config: ControlPlaneProductionConfig) -> Path:
    return config.database_path.parent / "skill-hub-cas"


def _converge_skill_hub_seed(
    registry: SkillHubRegistry,
    store: LocalSkillBundleStore,
) -> None:
    try:
        bundle = store.ingest_directory(
            Path(__file__).with_name("seed_skills") / _SKILL_HUB_SEED_SLUG
        )
        if bundle.artifact_sha256 != _SKILL_HUB_SEED_SHA256:
            raise ValueError("seed digest changed")
        registry.publish(
            account_id="system:skill-hub-seed",
            nickname="e-Mate",
            slug=_SKILL_HUB_SEED_SLUG,
            version=_SKILL_HUB_SEED_VERSION,
            title=bundle.metadata.name,
            summary=bundle.metadata.description,
            category="office_productivity",
            tags=bundle.metadata.tags,
            package_sha256=bundle.artifact_sha256,
            package_size_bytes=bundle.total_size_bytes,
            original_platform="e-Mate",
            original_url=None,
        )
        _require_skill_hub_seed(registry, store)
    except Exception:
        raise ProductionConfigurationError("Skill Hub seed is unavailable") from None


def _require_skill_hub_seed(
    registry: SkillHubRegistry,
    store: LocalSkillBundleStore,
) -> None:
    try:
        card = registry.get(_SKILL_HUB_SEED_SLUG, version=_SKILL_HUB_SEED_VERSION)
        bundle = store.verify(_SKILL_HUB_SEED_SHA256)
        if (
            card.package_sha256 != _SKILL_HUB_SEED_SHA256
            or card.version != _SKILL_HUB_SEED_VERSION
            or bundle.artifact_sha256 != _SKILL_HUB_SEED_SHA256
            or bundle.metadata.name != _SKILL_HUB_SEED_SLUG
            or bundle.metadata.version != _SKILL_HUB_SEED_VERSION
        ):
            raise ValueError("seed identity changed")
    except Exception:
        raise ProductionConfigurationError("Skill Hub seed is unavailable") from None


def _skill_hub_author_key(secrets: SecretProvider) -> bytes:
    integrity_key = _secret_bytes(
        secrets.read("audit-integrity-key"), minimum_length=32, maximum_length=64
    )
    return hmac.new(
        integrity_key, b"e-mate-skill-hub-author-v1", hashlib.sha256
    ).digest()


def _share_keyring(secrets: SecretProvider) -> CloudShareKeyRing:
    try:
        raw = json.loads(secrets.read("share-keyring"))
        if not isinstance(raw, dict) or set(raw) != {
            "active_key_id",
            "keys",
            "legacy_key_id",
        }:
            raise ValueError
        encoded_keys = raw["keys"]
        if not isinstance(encoded_keys, dict) or not 1 <= len(encoded_keys) <= 16:
            raise ValueError
        keys: dict[str, bytes] = {}
        for key_id, value in encoded_keys.items():
            if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
                raise ValueError
            keys[key_id] = _secret_bytes(value, exact_length=32)
        return CloudShareKeyRing(
            active_key_id=raw["active_key_id"],
            keys=keys,
            legacy_key_id=raw["legacy_key_id"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ProductionConfigurationError("Cloud Share keyring is invalid") from None


def _secret_bytes(
    value: str,
    *,
    exact_length: int | None = None,
    minimum_length: int | None = None,
    maximum_length: int | None = None,
) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ProductionConfigurationError("Control Plane secret material is invalid")
    try:
        material = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ProductionConfigurationError(
            "Control Plane secret material is invalid"
        ) from None
    if (
        base64.b64encode(material).decode("ascii") != value
        or (exact_length is not None and len(material) != exact_length)
        or (minimum_length is not None and len(material) < minimum_length)
        or (maximum_length is not None and len(material) > maximum_length)
    ):
        raise ProductionConfigurationError("Control Plane secret material is invalid")
    return material


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ProductionConfigurationError(
            "required Control Plane setting is unavailable"
        )
    if any(ord(character) < 32 for character in value):
        raise ProductionConfigurationError(
            "Control Plane setting contains invalid characters"
        )
    return value


def _absolute_path(environment: Mapping[str, str], name: str) -> Path:
    value = _required(environment, name)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProductionConfigurationError(
            "Control Plane production path must be absolute"
        )
    return Path(os.path.abspath(os.fspath(path)))


def _optional_absolute_path(environment: Mapping[str, str], name: str) -> Path | None:
    value = environment.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 8192:
        raise ProductionConfigurationError("Control Plane optional path is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProductionConfigurationError(
            "Control Plane production path must be absolute"
        )
    return Path(os.path.abspath(os.fspath(path)))


def _integer(
    environment: Mapping[str, str],
    name: str,
    *,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int:
    raw = environment.get(name)
    if raw is None and default is not None:
        return default
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 20:
        raise ProductionConfigurationError("Control Plane integer setting is invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ProductionConfigurationError(
            "Control Plane integer setting is out of range"
        )
    return value


def _float(
    environment: Mapping[str, str],
    name: str,
    *,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ProductionConfigurationError(
            "Control Plane numeric setting is invalid"
        ) from None
    if not minimum <= value <= maximum:
        raise ProductionConfigurationError(
            "Control Plane numeric setting is out of range"
        )
    return value


def _boolean(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool | None = None,
) -> bool:
    raw = environment.get(name)
    if raw is None and default is not None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ProductionConfigurationError("Control Plane boolean setting is invalid")


def _json_output(value: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _run_server(bundle: ControlPlaneProductionBundle) -> None:
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - required package
        raise ProductionConfigurationError(
            "ASGI server dependency is unavailable"
        ) from error

    application = bundle.create_app()

    class _DrainingServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            bundle.lifecycle.begin_drain()
            try:
                asyncio.get_running_loop().create_task(
                    application.state.update_signal_hub.begin_drain()
                )
            except RuntimeError:
                # Lifespan teardown repeats the idempotent Hub drain.
                pass
            super().handle_exit(sig, frame)

    server = _DrainingServer(
        uvicorn.Config(
            application,
            host=bundle.config.bind_host,
            port=bundle.config.bind_port,
            workers=1,
            reload=False,
            proxy_headers=False,
            forwarded_allow_ips="",
            server_header=False,
            date_header=True,
            # Default ASGI access logs include the raw path; public Share
            # tokens are path credentials and must never enter logs.
            access_log=False,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=bundle.config.graceful_shutdown_seconds,
            limit_concurrency=bundle.config.limit_concurrency,
            backlog=bundle.config.backlog,
        )
    )
    try:
        server.run()
    finally:
        bundle.lifecycle.force_close()


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
    provider: SingleNodeSQLiteS3Provider | None = None,
    server_runner=None,
) -> int:
    parser = argparse.ArgumentParser(prog="ecorex-control-plane")
    commands = parser.add_subparsers(dest="area", required=True)
    commands.add_parser("serve", help="run the production ASGI Control Plane")
    schema = commands.add_parser("schema", help="manage production schemas")
    schema_commands = schema.add_subparsers(dest="action", required=True)
    schema_commands.add_parser(
        "migrate", help="explicitly migrate all schema authorities"
    )
    schema_commands.add_parser("check", help="validate schemas, backup and S3 controls")
    backup = commands.add_parser("backup", help="manage verified SQLite backups")
    backup_commands = backup.add_subparsers(dest="action", required=True)
    backup_commands.add_parser("create", help="create one verified operator backup")
    backup_commands.add_parser("check", help="verify the newest backup")
    device = commands.add_parser("device", help="manage device identity migration")
    device_commands = device.add_subparsers(dest="action", required=True)
    device_commands.add_parser(
        "legacy-import",
        help="import v0.2.9.2 credential mappings from bounded NDJSON stdin",
    )
    args = parser.parse_args(argv)
    values = os.environ if environment is None else environment
    secrets = secret_provider or EnvironmentSecretProvider(values)
    selected = provider or SingleNodeSQLiteS3Provider()
    try:
        config = ControlPlaneProductionConfig.from_environment(values)
        if args.area == "schema":
            report = (
                selected.migrate(config, secrets)
                if args.action == "migrate"
                else selected.check(config, secrets)
            )
            _json_output(report.to_dict())
            return 0
        if args.area == "backup":
            receipt = (
                selected.backup_create(config, secrets)
                if args.action == "create"
                else selected.backup_check(config, secrets)
            )
            _json_output(receipt.to_dict())
            return 0
        if args.area == "device":
            if config.device_identity is None:
                raise ProductionConfigurationError(
                    "managed device identity is not configured"
                )
            payload = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise ProductionConfigurationError(
                    "legacy credential import is oversized"
                )
            try:
                records = [
                    json.loads(line)
                    for line in payload.decode("utf-8").splitlines()
                    if line.strip()
                ]
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProductionConfigurationError(
                    "legacy credential import is invalid"
                ) from None
            if not 1 <= len(records) <= 100_000 or any(
                not isinstance(item, dict) for item in records
            ):
                raise ProductionConfigurationError(
                    "legacy credential import is invalid"
                )
            management_key = _secret_bytes(
                secrets.read("model-config-encryption-key"), exact_length=32
            )
            broker = config.device_identity.compose(
                AdminManagementRepository(
                    config.database_path, encryption_key=management_key
                ),
                secrets=_ControlPlaneDeviceIdentitySecrets(secrets),
                initialize=False,
            )
            report = broker.import_legacy_credentials(records)
            _json_output(
                {
                    "schema_version": 1,
                    "source_version": "0.2.9.2",
                    **report,
                }
            )
            return 0
        bundle = selected.compose(config, secrets)
        try:
            (server_runner or _run_server)(bundle)
        finally:
            bundle.lifecycle.force_close()
        return 0
    except Exception as error:
        # Never render exception text: SDK/config errors can embed endpoints,
        # paths, bearer values or provider request identifiers.
        print(
            json.dumps(
                {"status": "failed", "error": error.__class__.__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover - deployment entry point
    raise SystemExit(main())


__all__ = [
    "AttestedLocalControlPlaneCASFactory",
    "Boto3S3ClientFactory",
    "ControlPlaneLocalCASFactory",
    "ControlPlaneProductionBundle",
    "ControlPlaneProductionConfig",
    "ControlPlaneProductionProvider",
    "EnvironmentSecretProvider",
    "ProductionConfigurationError",
    "ProductionS3Client",
    "ProductionSchemaReport",
    "S3ClientFactory",
    "SecretProvider",
    "ShareStorageDependency",
    "SingleNodeControlPlaneLifecycle",
    "SingleNodeSQLiteS3Provider",
    "main",
]
