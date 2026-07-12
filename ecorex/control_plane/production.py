"""Production composition and operator CLI for the EcoreX Control Plane.

The built-in v1 provider is intentionally a *single-node* SQLite WAL service.
It requires an exclusive process lock, a persistent-volume identity, verified
backups, S3-backed Share media, encrypted Cloud Audit and short-lived Ed25519
JWTs.  It never runs DDL during ``serve`` and refuses PostgreSQL/multi-replica
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
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
import uuid

from ecorex.observability.audit import AuditRetentionPolicy
from ecorex.release import (
    DigestPinnedExternalSigner,
    PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS,
    PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS,
    ReleaseSigner,
)
from ecorex.update import Ed25519SignatureVerifier

from .app import ControlPlaneServiceLifecycle, create_control_plane_app
from .audit import CloudAuditRepository
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
from .production_auth import (
    Ed25519JWTAuthenticator,
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
from .repository import ControlPlaneRepository
from .schema import (
    CONTROL_PLANE_SCHEMA_SHA256,
    CURRENT_CONTROL_PLANE_SCHEMA_VERSION,
    MIGRATION_001_CHECKSUM as CONTROL_PLANE_MIGRATION_CHECKSUM,
    ControlPlaneSchemaManager,
)
from .share_s3_objects import S3ShareObjectStore
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
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SECRET_NAMES = {
    "share-keyring": "ECOREX_CP_SHARE_KEYRING_JSON",
    "audit-encryption-key": "ECOREX_CP_AUDIT_ENCRYPTION_KEY_B64",
    "audit-integrity-key": "ECOREX_CP_AUDIT_INTEGRITY_KEY_B64",
}


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
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    s3_endpoint_url: str | None
    s3_addressing_style: str
    s3_max_connections: int
    auth_issuer: str
    auth_audience: str
    auth_public_keys_json: str = field(repr=False)
    release_public_keys_json: str = field(repr=False)
    publication_public_keys_json: str = field(repr=False)
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

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError:
            raise ProductionConfigurationError(
                "Control Plane bind host must be an IP address"
            ) from None
        parsed_public = urlsplit(self.public_share_base_url)
        parsed_bootstrap = urlsplit(self.public_bootstrap_index_url)
        endpoint = urlsplit(self.s3_endpoint_url) if self.s3_endpoint_url else None
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
            or _BUCKET.fullmatch(self.s3_bucket) is None
            or _PREFIX.fullmatch(self.s3_prefix) is None
            or any(part in {"", ".", ".."} for part in self.s3_prefix.split("/"))
            or not isinstance(self.s3_region, str)
            or not self.s3_region
            or self.s3_addressing_style not in {"virtual", "path"}
            or not 4 <= self.s3_max_connections <= 256
            or (endpoint is not None and endpoint.scheme != "https")
            or (endpoint is not None and not endpoint.hostname)
            or (endpoint is not None and endpoint.username is not None)
            or (endpoint is not None and endpoint.password is not None)
            or (endpoint is not None and bool(endpoint.query or endpoint.fragment))
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
        ):
            raise ProductionConfigurationError(
                "Control Plane production configuration is invalid"
            )
        parse_ed25519_public_keyring(self.auth_public_keys_json)
        release_keys = parse_ed25519_public_keyring(self.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            self.publication_public_keys_json
        )
        release_fingerprints = {
            hashlib.sha256(material).digest() for material in release_keys.values()
        }
        publication_fingerprints = {
            hashlib.sha256(material).digest() for material in publication_keys.values()
        }
        if set(release_keys) & set(publication_keys) or (
            release_fingerprints & publication_fingerprints
        ):
            raise ProductionConfigurationError(
                "release and publication trust roles must use distinct keys"
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

        endpoint = values.get("ECOREX_CP_S3_ENDPOINT_URL") or None
        if endpoint is not None:
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
        bucket = _required(values, "ECOREX_CP_S3_BUCKET")
        prefix = _required(values, "ECOREX_CP_S3_PREFIX").strip("/")
        if (
            _BUCKET.fullmatch(bucket) is None
            or _PREFIX.fullmatch(prefix) is None
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
        ):
            raise ProductionConfigurationError("S3 namespace configuration is invalid")
        addressing = values.get("ECOREX_CP_S3_ADDRESSING_STYLE", "virtual")
        if addressing not in {"virtual", "path"}:
            raise ProductionConfigurationError("S3 addressing configuration is invalid")

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
            s3_bucket=bucket,
            s3_prefix=prefix,
            s3_region=_required(values, "ECOREX_CP_S3_REGION"),
            s3_endpoint_url=endpoint,
            s3_addressing_style=addressing,
            s3_max_connections=_integer(
                values,
                "ECOREX_CP_S3_MAX_CONNECTIONS",
                minimum=4,
                maximum=256,
                default=32,
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
        )


@dataclass(frozen=True, slots=True)
class ProductionSchemaReport:
    schema_version: int
    storage_backend: str
    control_schema_version: int
    audit_schema_version: int
    share_schema_version: int
    backup: BackupReceipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "storage_backend": self.storage_backend,
            "control_schema_version": self.control_schema_version,
            "audit_schema_version": self.audit_schema_version,
            "share_schema_version": self.share_schema_version,
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


@dataclass(slots=True)
class ControlPlaneProductionBundle:
    repository: ControlPlaneRepository
    bootstrap_index_service: BootstrapIndexPublicationService
    bootstrap_freshness_refresher: BootstrapFreshnessRefresher
    share_repository: CloudShareRepository
    audit_repository: CloudAuditRepository
    authenticator: Ed25519JWTAuthenticator
    rollback_signer: ReleaseSigner | None
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
        s3: _S3Dependency,
        audit_repository: CloudAuditRepository,
        share_repository: CloudShareRepository,
        bootstrap_index_service: BootstrapIndexPublicationService,
    ) -> None:
        self.config = config
        self.instance_lock = instance_lock
        self.volume = volume
        self.backup = backup
        self.keyring = keyring
        self.s3 = s3
        self.audit_repository = audit_repository
        self.share_repository = share_repository
        self.bootstrap_index_service = bootstrap_index_service
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
                await asyncio.to_thread(self.s3.close)
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
                    self.s3.close()
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
            self.s3.validate_controls(write_probe=True)
        else:
            self.s3.ping()

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


class SingleNodeSQLiteS3Provider:
    def __init__(self, s3_factory: S3ClientFactory | None = None) -> None:
        self.s3_factory = s3_factory or Boto3S3ClientFactory()

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
        receipt = backup.latest(full_digest=True)
        _require_recent_backup(receipt, config.maximum_backup_age_seconds)
        audit_encryption = _secret_bytes(
            secrets.read("audit-encryption-key"), exact_length=32
        )
        audit_integrity = _secret_bytes(
            secrets.read("audit-integrity-key"), minimum_length=32, maximum_length=64
        )
        release_keys = parse_ed25519_public_keyring(config.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            config.publication_public_keys_json
        )
        auth_keys = parse_ed25519_public_keyring(config.auth_public_keys_json)
        _configured_publication_signer(config, release_keys, publication_keys)
        s3 = self._s3(config)
        try:
            s3.validate_controls(write_probe=True)
            object_store = S3ShareObjectStore(
                s3.client,
                bucket=config.s3_bucket,
                prefix=config.s3_prefix,
                max_open_streams=min(64, config.s3_max_connections),
                spool_directory=str(config.share_spool_directory),
            )
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
        finally:
            s3.close()
        return ProductionSchemaReport(
            schema_version=1,
            storage_backend=config.storage_backend,
            control_schema_version=control.migration_version,
            audit_schema_version=audit.migration_version,
            share_schema_version=share.migration_version,
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
        release_keys = parse_ed25519_public_keyring(config.release_public_keys_json)
        publication_keys = parse_ed25519_public_keyring(
            config.publication_public_keys_json
        )
        auth_keys = parse_ed25519_public_keyring(config.auth_public_keys_json)
        volume, backup = _storage(config)
        instance_lock = ControlPlaneInstanceLock(config.database_path)
        instance_lock.acquire()
        s3: _S3Dependency | None = None
        bootstrap_index_service: BootstrapIndexPublicationService | None = None
        try:
            volume.validate_wal()
            ControlPlaneSchemaManager(config.database_path).validate()
            CloudAuditSchemaManager(config.database_path).validate()
            CloudShareSchemaManager(config.database_path, keyring=keyring).validate()
            backup.latest(full_digest=True)
            s3 = self._s3(config)
            object_store = S3ShareObjectStore(
                s3.client,
                bucket=config.s3_bucket,
                prefix=config.s3_prefix,
                max_open_streams=min(64, config.s3_max_connections),
                max_total_spool_bytes=256 * 1024 * 1024,
                memory_spool_bytes=256 * 1024,
                spool_directory=str(config.share_spool_directory),
            )
            verifier = Ed25519SignatureVerifier(release_keys)
            authenticator = Ed25519JWTAuthenticator(
                auth_keys,
                issuer=config.auth_issuer,
                audience=config.auth_audience,
                max_token_lifetime_seconds=config.auth_max_token_lifetime_seconds,
                clock_skew_seconds=config.auth_clock_skew_seconds,
            )
            publication_verifier = Ed25519SignatureVerifier(publication_keys)
            online_authorization_signer = _configured_publication_signer(
                config, release_keys, publication_keys
            )
            repository = ControlPlaneRepository(
                config.database_path,
                verifier=verifier,
                bootstrap_freshness_verifier=publication_verifier,
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
            lifecycle = SingleNodeControlPlaneLifecycle(
                config=config,
                instance_lock=instance_lock,
                volume=volume,
                backup=backup,
                keyring=keyring,
                s3=s3,
                audit_repository=audit_repository,
                share_repository=share_repository,
                bootstrap_index_service=bootstrap_index_service,
            )
            return ControlPlaneProductionBundle(
                repository=repository,
                bootstrap_index_service=bootstrap_index_service,
                bootstrap_freshness_refresher=bootstrap_freshness_refresher,
                share_repository=share_repository,
                audit_repository=audit_repository,
                authenticator=authenticator,
                rollback_signer=online_authorization_signer,
                lifecycle=lifecycle,
                config=config,
            )
        except BaseException:
            if bootstrap_index_service is not None:
                bootstrap_index_service.close()
            if s3 is not None:
                s3.close()
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
        if not directory.exists() or not directory.is_dir() or directory.is_symlink():
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
    except sqlite3.Error as error:
        raise ProductionStorageError(
            "production schema readiness receipt is unavailable"
        ) from error
    finally:
        connection.close()


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
    "Boto3S3ClientFactory",
    "ControlPlaneProductionBundle",
    "ControlPlaneProductionConfig",
    "ControlPlaneProductionProvider",
    "EnvironmentSecretProvider",
    "ProductionConfigurationError",
    "ProductionS3Client",
    "ProductionSchemaReport",
    "S3ClientFactory",
    "SecretProvider",
    "SingleNodeControlPlaneLifecycle",
    "SingleNodeSQLiteS3Provider",
    "main",
]
