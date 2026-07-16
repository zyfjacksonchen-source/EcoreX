"""Production composition and operator CLI for Image Orchestrator.

Production is intentionally one storage shape: PostgreSQL is the authoritative
job/event/lease store and private encrypted S3 is the shared CAS.  API and
workers may be scaled independently because neither process owns schema DDL or
process-local durable state.  SQLite remains available to local/test code but
is rejected by this entry point.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ecorex.control_plane.management import AdminManagementRepository
from ecorex.control_plane.management_schema import AdminManagementSchemaManager

from .api import create_image_orchestration_router
from .managed_provider import (
    ManagedHTTPSImageProvider,
    ManagedImageProviderConfigurationError,
    normalize_https_origin,
)
from .dynamic_provider import (
    AdminImageModelConfigurationResolver,
    DynamicManagedImageProvider,
)
from .models import ImageLimits
from .postgres_schema import PostgresImageSchemaManager, PostgresImageSchemaReceipt
from .postgres_store import PostgresImageJobStore
from .production_auth import (
    Ed25519ImageJWTAuthenticator,
    parse_ed25519_public_keyring,
)
from .s3_cas import BotoS3ObjectTransport, S3ImageContentStore
from .service import ImageOrchestrationService
from .service import ImageModelConfigurationResolver
from .provider import ImageProvider
from .worker import ImageJobWorker, ImageWorkerSupervisor


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SECRET_NAMES = {
    "managed-provider-bearer": "ECOREX_IMAGE_PROVIDER_BEARER_TOKEN",
    "model-config-encryption-key": "ECOREX_IMAGE_MODEL_CONFIG_ENCRYPTION_KEY_B64",
}


class ImageProductionConfigurationError(RuntimeError):
    """A production dependency or resource envelope is missing/unsafe."""


@runtime_checkable
class ImageSecretProvider(Protocol):
    """Narrow seam for Vault/sidecar/workload-identity credential sources."""

    def read(self, logical_name: str) -> str: ...


class EnvironmentImageSecretProvider:
    """Environment fallback for one fixed secret name; never logs its value."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def read(self, logical_name: str) -> str:
        name = _SECRET_NAMES.get(logical_name)
        if name is None:
            raise ImageProductionConfigurationError("unknown image secret")
        value = self._environment.get(name)
        if not isinstance(value, str) or not value:
            raise ImageProductionConfigurationError("required image secret is unavailable")
        return value


@runtime_checkable
class ImageProductionS3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_bucket(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_bucket_encryption(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_public_access_block(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_bucket_policy_status(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class ImageS3ClientFactory(Protocol):
    def create(self, config: "ImageProductionConfig") -> ImageProductionS3Client: ...


@runtime_checkable
class ProductionImageProvider(ImageProvider, Protocol):
    async def health(self) -> None: ...

    async def aclose(self) -> None: ...


class Boto3ImageS3ClientFactory:
    """Use only the SDK credential chain/workload identity; no access-key args."""

    def create(self, config: "ImageProductionConfig") -> ImageProductionS3Client:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:  # pragma: no cover - signed cloud pack boundary
            raise ImageProductionConfigurationError(
                "the signed image cloud storage pack is unavailable"
            ) from error
        try:
            client = boto3.client(
                "s3",
                region_name=config.s3_region,
                endpoint_url=config.s3_endpoint_url,
                use_ssl=True,
                verify=True,
                config=Config(
                    connect_timeout=config.dependency_timeout_seconds,
                    read_timeout=config.dependency_timeout_seconds,
                    retries={"mode": "standard", "max_attempts": 3},
                    max_pool_connections=config.s3_max_connections,
                    s3={"addressing_style": config.s3_addressing_style},
                ),
            )
        except Exception:
            raise ImageProductionConfigurationError("S3 client could not start") from None
        if not isinstance(client, ImageProductionS3Client):
            try:
                client.close()
            except Exception:
                pass
            raise ImageProductionConfigurationError("S3 client contract is unavailable")
        return client


def _json_string_set(
    raw: str,
    *,
    label: str,
    pattern: re.Pattern[str] | None = None,
    maximum: int = 128,
) -> frozenset[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ImageProductionConfigurationError(f"{label} is invalid") from None
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
        or (pattern is not None and any(pattern.fullmatch(item) is None for item in value))
    ):
        raise ImageProductionConfigurationError(f"{label} is invalid")
    return frozenset(value)


def _valid_postgres_dsn(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "postgresql"
        and parsed.hostname
        and parsed.path not in {"", "/"}
        and not parsed.fragment
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _valid_s3_endpoint(value: str | None) -> bool:
    if value is None:
        return True
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _valid_issuer(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


@dataclass(frozen=True, slots=True)
class ImageProductionConfig:
    storage_backend: str
    postgres_dsn: str = field(repr=False)
    instance_id: str
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    s3_endpoint_url: str | None
    s3_addressing_style: str
    s3_max_connections: int
    s3_encryption: str
    s3_kms_key_id: str | None
    max_image_bytes: int
    auth_issuer: str
    auth_audience: str
    auth_public_keys_json: str = field(repr=False)
    model_allowlist: frozenset[str]
    provider_id: str
    provider_origin: str
    provider_allowed_origins: frozenset[str]
    bind_host: str = "127.0.0.1"
    bind_port: int = 8450
    allow_trusted_ingress_http: bool = False
    postgres_pool_min: int = 1
    postgres_pool_max: int = 32
    postgres_pool_timeout_seconds: float = 10.0
    worker_concurrency: int = 8
    worker_memory_envelope_bytes: int = 4 * 1024 * 1024 * 1024
    api_blob_memory_envelope_bytes: int = 512 * 1024 * 1024
    provider_max_connections: int = 32
    provider_max_concurrency: int = 16
    provider_timeout_seconds: float = 120.0
    provider_connect_timeout_seconds: float = 5.0
    dependency_timeout_seconds: float = 10.0
    readiness_cache_seconds: float = 10.0
    graceful_shutdown_seconds: float = 60.0
    lease_seconds: int = 30
    heartbeat_seconds: float = 5.0
    idle_poll_seconds: float = 0.25
    limit_concurrency: int = 512
    backlog: int = 1024
    admin_management_enabled: bool = False
    admin_management_database_path: Path | None = None
    model_provider_origins: Mapping[str, str] = field(default_factory=dict)
    limits: ImageLimits = ImageLimits()

    def __post_init__(self) -> None:
        api_blob_slots = max(
            1,
            min(
                32,
                self.api_blob_memory_envelope_bytes
                // max(1, self.max_image_bytes * 2),
            ),
        )
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError:
            raise ImageProductionConfigurationError("image bind host must be an IP address") from None
        if (
            self.storage_backend != "postgresql"
            or not isinstance(self.postgres_dsn, str)
            or not _valid_postgres_dsn(self.postgres_dsn)
            or _SAFE_ID.fullmatch(self.instance_id) is None
            or _BUCKET.fullmatch(self.s3_bucket) is None
            or _PREFIX.fullmatch(self.s3_prefix) is None
            or any(part in {"", ".", ".."} for part in self.s3_prefix.split("/"))
            or not isinstance(self.s3_region, str)
            or not self.s3_region
            or self.s3_addressing_style not in {"virtual", "path"}
            or not 8 <= self.s3_max_connections <= 256
            or self.s3_max_connections < max(self.worker_concurrency, api_blob_slots)
            or self.s3_encryption not in {"AES256", "aws:kms"}
            or (self.s3_encryption == "aws:kms" and not self.s3_kms_key_id)
            or (self.s3_encryption == "AES256" and self.s3_kms_key_id is not None)
            or (self.s3_kms_key_id is not None and (
                len(self.s3_kms_key_id) > 2048
                or any(ord(character) < 32 for character in self.s3_kms_key_id)
            ))
            or not _valid_s3_endpoint(self.s3_endpoint_url)
            or not 1024 <= self.max_image_bytes <= 256 * 1024 * 1024
            or not isinstance(self.auth_issuer, str)
            or not _valid_issuer(self.auth_issuer)
            or not isinstance(self.auth_audience, str)
            or not self.auth_audience
            or not isinstance(self.auth_public_keys_json, str)
            or not self.auth_public_keys_json
            or not isinstance(self.model_allowlist, frozenset)
            or not self.model_allowlist
            or any(_MODEL.fullmatch(model) is None for model in self.model_allowlist)
            or _MODEL.fullmatch(self.provider_id) is None
            or normalize_https_origin(self.provider_origin) != self.provider_origin
            or self.provider_origin not in self.provider_allowed_origins
            or not 1024 <= self.bind_port <= 65535
            or (not address.is_loopback and not self.allow_trusted_ingress_http)
            or not 0 <= self.postgres_pool_min <= self.postgres_pool_max <= 128
            or self.postgres_pool_max < self.worker_concurrency + 4
            or not 0.1 <= self.postgres_pool_timeout_seconds <= 120.0
            or not 1 <= self.worker_concurrency <= 256
            or not 128 * 1024 * 1024 <= self.worker_memory_envelope_bytes <= 64 * 1024**3
            or self.worker_concurrency
            * self.max_image_bytes
            * (6 if self.admin_management_enabled else 3)
            > self.worker_memory_envelope_bytes
            or not 32 * 1024 * 1024 <= self.api_blob_memory_envelope_bytes <= 16 * 1024**3
            or self.api_blob_memory_envelope_bytes < self.max_image_bytes * 2
            or not self.worker_concurrency
            <= self.provider_max_concurrency
            <= self.provider_max_connections
            <= 256
            or not 1.0 <= self.provider_timeout_seconds <= 600.0
            or not 0.1 <= self.provider_connect_timeout_seconds <= min(60.0, self.provider_timeout_seconds)
            or not 1.0 <= self.dependency_timeout_seconds <= 60.0
            or not 1.0 <= self.readiness_cache_seconds <= 60.0
            or not 5.0 <= self.graceful_shutdown_seconds <= 300.0
            or not 5 <= self.lease_seconds <= 300
            or not 0.1 <= self.heartbeat_seconds < self.lease_seconds
            or not 0.01 <= self.idle_poll_seconds <= 60.0
            or not 16 <= self.limit_concurrency <= 4096
            or not 16 <= self.backlog <= 8192
        ):
            raise ImageProductionConfigurationError("image production configuration is invalid")
        parse_ed25519_public_keyring(self.auth_public_keys_json)
        dynamic_origins = dict(self.model_provider_origins)
        if self.admin_management_enabled:
            if (
                not isinstance(self.admin_management_database_path, Path)
                or not self.admin_management_database_path.is_absolute()
                or not dynamic_origins
            ):
                raise ImageProductionConfigurationError(
                    "image administrator model source is invalid"
                )
            try:
                normalized_dynamic = {
                    preset: normalize_https_origin(origin)
                    for preset, origin in dynamic_origins.items()
                    if preset == "openai_compatible_image"
                }
            except (ManagedImageProviderConfigurationError, TypeError):
                raise ImageProductionConfigurationError(
                    "image administrator model origins are invalid"
                ) from None
            if len(normalized_dynamic) != len(dynamic_origins):
                raise ImageProductionConfigurationError(
                    "image administrator model origins are invalid"
                )
            object.__setattr__(
                self,
                "model_provider_origins",
                MappingProxyType(normalized_dynamic),
            )
        elif self.admin_management_database_path is not None or dynamic_origins:
            raise ImageProductionConfigurationError(
                "image administrator model source requires explicit enablement"
            )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "ImageProductionConfig":
        values = os.environ if environment is None else environment
        backend = _required(values, "ECOREX_IMAGE_STORAGE_BACKEND")
        if backend != "postgresql":
            raise ImageProductionConfigurationError(
                "production Image Orchestrator requires PostgreSQL"
            )
        dsn = _required(values, "ECOREX_IMAGE_POSTGRES_DSN", maximum=8192)
        provider_origin = normalize_https_origin(
            _required(values, "ECOREX_IMAGE_PROVIDER_ORIGIN")
        )
        allowed_origins = frozenset(
            normalize_https_origin(item)
            for item in _json_string_set(
                _required(values, "ECOREX_IMAGE_PROVIDER_ALLOWED_ORIGINS_JSON"),
                label="managed image origin allowlist",
                maximum=16,
            )
        )
        model_allowlist = _json_string_set(
            _required(values, "ECOREX_IMAGE_MODEL_ALLOWLIST_JSON"),
            label="managed image model allowlist",
            pattern=_MODEL,
        )
        max_image_bytes = _integer(
            values,
            "ECOREX_IMAGE_MAX_BYTES",
            minimum=1024,
            maximum=256 * 1024 * 1024,
            default=64 * 1024 * 1024,
        )
        worker_concurrency = _integer(
            values, "ECOREX_IMAGE_WORKER_CONCURRENCY", minimum=1, maximum=256, default=8
        )
        limits = ImageLimits(
            max_queued_jobs=_integer(values, "ECOREX_IMAGE_MAX_QUEUED_JOBS", minimum=1, maximum=10_000_000, default=10_000),
            max_queued_weight=_integer(values, "ECOREX_IMAGE_MAX_QUEUED_WEIGHT", minimum=1, maximum=100_000_000, default=100_000),
            max_account_queued_jobs=_integer(values, "ECOREX_IMAGE_MAX_ACCOUNT_QUEUED_JOBS", minimum=1, maximum=1_000_000, default=1_000),
            max_account_queued_weight=_integer(values, "ECOREX_IMAGE_MAX_ACCOUNT_QUEUED_WEIGHT", minimum=1, maximum=10_000_000, default=20_000),
            max_running_jobs=_integer(values, "ECOREX_IMAGE_MAX_RUNNING_JOBS", minimum=1, maximum=100_000, default=128),
            max_account_running=_integer(values, "ECOREX_IMAGE_MAX_ACCOUNT_RUNNING", minimum=1, maximum=10_000, default=8),
            max_model_running=_integer(values, "ECOREX_IMAGE_MAX_MODEL_RUNNING", minimum=1, maximum=100_000, default=64),
            max_operation_running=_integer(values, "ECOREX_IMAGE_MAX_OPERATION_RUNNING", minimum=1, maximum=100_000, default=96),
        )
        endpoint = values.get("ECOREX_IMAGE_S3_ENDPOINT_URL") or None
        management_enabled = _boolean(
            values, "ECOREX_IMAGE_ADMIN_MANAGEMENT_ENABLED", default=False
        )
        management_database = (
            _absolute_path(values, "ECOREX_IMAGE_ADMIN_MANAGEMENT_DATABASE_PATH")
            if management_enabled
            else None
        )
        dynamic_origins: dict[str, str] = {}
        raw_dynamic_origins = values.get("ECOREX_IMAGE_MODEL_PROVIDER_ORIGINS_JSON")
        if raw_dynamic_origins:
            try:
                parsed_dynamic_origins = json.loads(raw_dynamic_origins)
            except json.JSONDecodeError:
                raise ImageProductionConfigurationError(
                    "image administrator model origins are invalid"
                ) from None
            if (
                not isinstance(parsed_dynamic_origins, dict)
                or not parsed_dynamic_origins
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in parsed_dynamic_origins.items()
                )
            ):
                raise ImageProductionConfigurationError(
                    "image administrator model origins are invalid"
                )
            dynamic_origins = {
                str(key): str(value)
                for key, value in parsed_dynamic_origins.items()
            }
        return cls(
            storage_backend=backend,
            postgres_dsn=dsn,
            instance_id=_required(values, "ECOREX_IMAGE_INSTANCE_ID"),
            s3_bucket=_required(values, "ECOREX_IMAGE_S3_BUCKET"),
            s3_prefix=_required(values, "ECOREX_IMAGE_S3_PREFIX").strip("/"),
            s3_region=_required(values, "ECOREX_IMAGE_S3_REGION"),
            s3_endpoint_url=endpoint,
            s3_addressing_style=values.get("ECOREX_IMAGE_S3_ADDRESSING_STYLE", "virtual"),
            s3_max_connections=_integer(values, "ECOREX_IMAGE_S3_MAX_CONNECTIONS", minimum=8, maximum=256, default=64),
            s3_encryption=values.get("ECOREX_IMAGE_S3_ENCRYPTION", "AES256"),
            s3_kms_key_id=values.get("ECOREX_IMAGE_S3_KMS_KEY_ID") or None,
            max_image_bytes=max_image_bytes,
            auth_issuer=_required(values, "ECOREX_IMAGE_AUTH_ISSUER"),
            auth_audience=_required(values, "ECOREX_IMAGE_AUTH_AUDIENCE"),
            auth_public_keys_json=_required(values, "ECOREX_IMAGE_AUTH_PUBLIC_KEYS_JSON", maximum=8192),
            model_allowlist=model_allowlist,
            provider_id=_required(values, "ECOREX_IMAGE_PROVIDER_ID"),
            provider_origin=provider_origin,
            provider_allowed_origins=allowed_origins,
            bind_host=values.get("ECOREX_IMAGE_BIND_HOST", "127.0.0.1"),
            bind_port=_integer(values, "ECOREX_IMAGE_BIND_PORT", minimum=1024, maximum=65535, default=8450),
            allow_trusted_ingress_http=_boolean(values, "ECOREX_IMAGE_ALLOW_TRUSTED_INGRESS_HTTP", default=False),
            postgres_pool_min=_integer(values, "ECOREX_IMAGE_POSTGRES_POOL_MIN", minimum=0, maximum=128, default=1),
            postgres_pool_max=_integer(values, "ECOREX_IMAGE_POSTGRES_POOL_MAX", minimum=1, maximum=128, default=32),
            postgres_pool_timeout_seconds=_float(values, "ECOREX_IMAGE_POSTGRES_POOL_TIMEOUT_SECONDS", minimum=0.1, maximum=120.0, default=10.0),
            worker_concurrency=worker_concurrency,
            worker_memory_envelope_bytes=_integer(values, "ECOREX_IMAGE_WORKER_MEMORY_ENVELOPE_BYTES", minimum=128 * 1024 * 1024, maximum=64 * 1024**3, default=4 * 1024**3),
            api_blob_memory_envelope_bytes=_integer(values, "ECOREX_IMAGE_API_BLOB_MEMORY_ENVELOPE_BYTES", minimum=32 * 1024 * 1024, maximum=16 * 1024**3, default=512 * 1024**2),
            provider_max_connections=_integer(values, "ECOREX_IMAGE_PROVIDER_MAX_CONNECTIONS", minimum=1, maximum=256, default=32),
            provider_max_concurrency=_integer(values, "ECOREX_IMAGE_PROVIDER_MAX_CONCURRENCY", minimum=1, maximum=256, default=16),
            provider_timeout_seconds=_float(values, "ECOREX_IMAGE_PROVIDER_TIMEOUT_SECONDS", minimum=1.0, maximum=600.0, default=120.0),
            provider_connect_timeout_seconds=_float(values, "ECOREX_IMAGE_PROVIDER_CONNECT_TIMEOUT_SECONDS", minimum=0.1, maximum=60.0, default=5.0),
            dependency_timeout_seconds=_float(values, "ECOREX_IMAGE_DEPENDENCY_TIMEOUT_SECONDS", minimum=1.0, maximum=60.0, default=10.0),
            readiness_cache_seconds=_float(values, "ECOREX_IMAGE_READINESS_CACHE_SECONDS", minimum=1.0, maximum=60.0, default=10.0),
            graceful_shutdown_seconds=_float(values, "ECOREX_IMAGE_GRACEFUL_SHUTDOWN_SECONDS", minimum=5.0, maximum=300.0, default=60.0),
            lease_seconds=_integer(values, "ECOREX_IMAGE_LEASE_SECONDS", minimum=5, maximum=300, default=30),
            heartbeat_seconds=_float(values, "ECOREX_IMAGE_HEARTBEAT_SECONDS", minimum=0.1, maximum=299.0, default=5.0),
            idle_poll_seconds=_float(values, "ECOREX_IMAGE_IDLE_POLL_SECONDS", minimum=0.01, maximum=60.0, default=0.25),
            limit_concurrency=_integer(values, "ECOREX_IMAGE_HTTP_CONCURRENCY", minimum=16, maximum=4096, default=512),
            backlog=_integer(values, "ECOREX_IMAGE_HTTP_BACKLOG", minimum=16, maximum=8192, default=1024),
            admin_management_enabled=management_enabled,
            admin_management_database_path=management_database,
            model_provider_origins=dynamic_origins,
            limits=limits,
        )


@dataclass(frozen=True, slots=True)
class ImageProductionReport:
    schema_version: int
    storage_backend: str
    migration: PostgresImageSchemaReceipt
    s3_checked: bool
    provider_checked: bool
    auth_checked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "storage_backend": self.storage_backend,
            "migration": self.migration.to_dict(),
            "s3_checked": self.s3_checked,
            "provider_checked": self.provider_checked,
            "auth_checked": self.auth_checked,
        }


class _S3Dependency:
    def __init__(self, client: ImageProductionS3Client, config: ImageProductionConfig) -> None:
        self.client = client
        self.config = config
        self._closed = False

    def validate_controls(self, *, write_probe: bool) -> None:
        try:
            self._validate_controls(write_probe=write_probe)
        except ImageProductionConfigurationError:
            raise
        except Exception:
            # SDK exceptions can include endpoint topology and signed request
            # metadata.  Collapse them at this trust boundary.
            raise ImageProductionConfigurationError(
                "image S3 dependency is unavailable"
            ) from None

    def _validate_controls(self, *, write_probe: bool) -> None:
        if self._closed:
            raise ImageProductionConfigurationError("image S3 dependency is closed")
        self.client.head_bucket(Bucket=self.config.s3_bucket)
        encryption = self.client.get_bucket_encryption(Bucket=self.config.s3_bucket)
        rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        if not isinstance(rules, list) or not any(
            isinstance(rule, Mapping)
            and rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            == self.config.s3_encryption
            for rule in rules
        ):
            raise ImageProductionConfigurationError("image S3 encryption is not enforced")
        public = self.client.get_public_access_block(Bucket=self.config.s3_bucket)
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
            raise ImageProductionConfigurationError("image S3 public access is not blocked")
        policy = self.client.get_bucket_policy_status(Bucket=self.config.s3_bucket)
        if policy.get("PolicyStatus", {}).get("IsPublic") is not False:
            raise ImageProductionConfigurationError("image S3 bucket policy is public")
        if write_probe:
            self._write_probe()

    def _write_probe(self) -> None:
        import base64
        import hashlib
        import secrets

        payload = b"ecorex-image-s3-health-v1"
        checksum = hashlib.sha256(payload).digest()
        key = f"{self.config.s3_prefix}/_health/{secrets.token_hex(16)}"
        arguments: dict[str, Any] = {
            "Bucket": self.config.s3_bucket,
            "Key": key,
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": "application/octet-stream",
            "Metadata": {"ecorex-contract": "image-health-v1"},
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": base64.b64encode(checksum).decode("ascii"),
            "IfNoneMatch": "*",
            "ServerSideEncryption": self.config.s3_encryption,
        }
        if self.config.s3_kms_key_id is not None:
            arguments["SSEKMSKeyId"] = self.config.s3_kms_key_id
        created = False
        body = None
        try:
            self.client.put_object(**arguments)
            created = True
            head = self.client.head_object(
                Bucket=self.config.s3_bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
            if (
                int(head.get("ContentLength", -1)) != len(payload)
                or head.get("ServerSideEncryption") != self.config.s3_encryption
            ):
                raise ImageProductionConfigurationError("image S3 write probe is invalid")
            result = self.client.get_object(
                Bucket=self.config.s3_bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
            body = result.get("Body")
            received = body if isinstance(body, bytes) else body.read(len(payload) + 1)
            if received != payload:
                raise ImageProductionConfigurationError("image S3 read probe is invalid")
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
            if created:
                self.client.delete_object(Bucket=self.config.s3_bucket, Key=key)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        except Exception:
            raise ImageProductionConfigurationError(
                "image S3 dependency could not close"
            ) from None


@dataclass(slots=True)
class ImageProductionBundle:
    config: ImageProductionConfig
    store: PostgresImageJobStore
    content_store: S3ImageContentStore
    provider: ProductionImageProvider
    service: ImageOrchestrationService
    authenticator: Ed25519ImageJWTAuthenticator
    lifecycle: "ImageProductionLifecycle"
    mode: str

    def create_app(self) -> FastAPI:
        return create_image_production_app(self, include_api=self.mode != "worker")


class ImageProductionLifecycle:
    def __init__(
        self,
        *,
        config: ImageProductionConfig,
        store: PostgresImageJobStore,
        s3: _S3Dependency,
        provider: ProductionImageProvider,
        supervisor: ImageWorkerSupervisor | None,
    ) -> None:
        self.config = config
        self.store = store
        self.s3 = s3
        self.provider = provider
        self.supervisor = supervisor
        self._accepting = False
        self._live = False
        self._closed = False
        self._ready_lock = asyncio.Lock()
        self._ready_until = 0.0
        self._ready_value = False

    @property
    def accepting(self) -> bool:
        return self._accepting and not self._closed

    @property
    def live(self) -> bool:
        return self._live and not self._closed

    async def startup(self) -> None:
        if self._closed:
            raise ImageProductionConfigurationError("image lifecycle is closed")
        await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(self.store.ping),
                asyncio.to_thread(self.s3.validate_controls, write_probe=True),
                self.provider.health(),
            ),
            timeout=self.config.dependency_timeout_seconds * 4,
        )
        if self.supervisor is not None:
            await self.supervisor.start()
        self._live = True
        self._accepting = True

    async def readiness(self) -> bool:
        if not self.accepting:
            return False
        if self.supervisor is not None and not self.supervisor.healthy:
            return False
        loop = asyncio.get_running_loop()
        if loop.time() < self._ready_until:
            return self._ready_value
        async with self._ready_lock:
            if loop.time() < self._ready_until:
                return self._ready_value
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        asyncio.to_thread(self.store.ping),
                        asyncio.to_thread(self.s3.validate_controls, write_probe=False),
                        self.provider.health(),
                    ),
                    timeout=self.config.dependency_timeout_seconds * 4,
                )
                value = self.supervisor is None or self.supervisor.healthy
            except Exception:
                value = False
            self._ready_value = value
            self._ready_until = loop.time() + self.config.readiness_cache_seconds
            return value

    def begin_drain(self) -> None:
        self._accepting = False
        self._ready_value = False
        self._ready_until = 0.0
        if self.supervisor is not None:
            self.supervisor.begin_drain()

    async def shutdown(self) -> None:
        if self._closed:
            return
        self.begin_drain()
        try:
            if self.supervisor is not None:
                await self.supervisor.stop()
        finally:
            self._live = False
            self._closed = True
            try:
                await self.provider.aclose()
            finally:
                try:
                    await asyncio.to_thread(self.store.close)
                finally:
                    await asyncio.to_thread(self.s3.close)

    async def force_close(self) -> None:
        await self.shutdown()


def _bearer(value: str) -> str:
    scheme, separator, token = value.partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not 128 <= len(token) <= 4096
        or any(character.isspace() or ord(character) < 32 for character in token)
    ):
        raise PermissionError("valid image bearer token is required")
    return token


def create_image_production_app(
    bundle: ImageProductionBundle,
    *,
    include_api: bool,
) -> FastAPI:
    lifecycle = bundle.lifecycle

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        started = False
        try:
            await lifecycle.startup()
            started = True
            yield
        finally:
            lifecycle.begin_drain()
            if started:
                await lifecycle.shutdown()
            else:
                await lifecycle.force_close()

    app = FastAPI(
        title="EcoreX Image Orchestrator",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if include_api else None,
        lifespan=lifespan,
    )
    app.state.image_bundle = bundle
    app.state.service_lifecycle = lifecycle

    def principal(request: Request):
        try:
            return bundle.authenticator.authenticate(
                _bearer(request.headers.get("authorization", ""))
            )
        except PermissionError as error:
            raise HTTPException(status_code=401, detail="image authentication failed") from error

    if include_api:
        app.include_router(
            create_image_orchestration_router(
                bundle.service,
                principal_dependency=principal,
                content_store=bundle.content_store,
                blob_memory_envelope_bytes=bundle.config.api_blob_memory_envelope_bytes,
                require_model_entitlements=True,
            )
        )

    @app.middleware("http")
    async def production_boundary(request: Request, call_next):
        if (
            not lifecycle.accepting
            and request.url.path not in {"/health/live", "/health/ready"}
        ):
            return JSONResponse(
                status_code=503,
                content={"status": "draining"},
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health/live", include_in_schema=False)
    async def health_live() -> JSONResponse:
        return JSONResponse(
            status_code=200 if lifecycle.live else 503,
            content={"status": "live" if lifecycle.live else "stopped"},
        )

    @app.get("/health/ready", include_in_schema=False)
    async def health_ready() -> JSONResponse:
        try:
            ready = await lifecycle.readiness()
        except Exception:
            ready = False
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "unavailable"},
        )

    return app


class PostgresS3ManagedImageProvider:
    """First-party production provider; runtime paths are validate-only."""

    def __init__(self, *, s3_factory: ImageS3ClientFactory | None = None) -> None:
        self.s3_factory = s3_factory or Boto3ImageS3ClientFactory()

    def migrate(
        self,
        config: ImageProductionConfig,
        secrets: ImageSecretProvider,
    ) -> ImageProductionReport:
        self._static_dependencies(config, secrets)
        receipt = PostgresImageSchemaManager(config.postgres_dsn).migrate()
        s3, provider, _resolver = self._external_dependencies(config, secrets)
        try:
            s3.validate_controls(write_probe=True)
            asyncio.run(_probe_and_close_provider(provider))
        finally:
            s3.close()
        return ImageProductionReport(1, config.storage_backend, receipt, True, True, True)

    def check(
        self,
        config: ImageProductionConfig,
        secrets: ImageSecretProvider,
    ) -> ImageProductionReport:
        self._static_dependencies(config, secrets)
        receipt = PostgresImageSchemaManager(config.postgres_dsn).validate()
        s3, provider, _resolver = self._external_dependencies(config, secrets)
        try:
            s3.validate_controls(write_probe=True)
            asyncio.run(_probe_and_close_provider(provider))
        finally:
            s3.close()
        return ImageProductionReport(1, config.storage_backend, receipt, True, True, True)

    def compose(
        self,
        config: ImageProductionConfig,
        secrets: ImageSecretProvider,
        *,
        mode: str,
    ) -> ImageProductionBundle:
        if mode not in {"serve", "worker", "all"}:
            raise ImageProductionConfigurationError("image process mode is invalid")
        authenticator = self._static_dependencies(config, secrets)
        s3, provider, model_resolver = self._external_dependencies(config, secrets)
        store: PostgresImageJobStore | None = None
        try:
            store = PostgresImageJobStore(
                config.postgres_dsn,
                limits=config.limits,
                pool_min_size=config.postgres_pool_min,
                pool_max_size=config.postgres_pool_max,
                pool_timeout_seconds=config.postgres_pool_timeout_seconds,
            )
            content_store = S3ImageContentStore(
                BotoS3ObjectTransport(
                    s3.client,
                    server_side_encryption=config.s3_encryption,
                    kms_key_id=config.s3_kms_key_id,
                ),
                bucket=config.s3_bucket,
                prefix=config.s3_prefix,
                max_bytes=config.max_image_bytes,
            )
            supervisor = None
            if mode in {"worker", "all"}:
                worker = ImageJobWorker(
                    store,
                    provider,
                    content_store,
                    lease_seconds=config.lease_seconds,
                    heartbeat_seconds=config.heartbeat_seconds,
                    # A completed submit can require one additional bounded
                    # result download.  Keep the durable half-open probe lease
                    # alive across both provider windows so another replica
                    # cannot stampede the same circuit while the probe is
                    # legitimately still running.
                    breaker_probe_seconds=min(
                        3600,
                        int(config.provider_timeout_seconds * 2)
                        + config.lease_seconds
                        + 1,
                    ),
                )
                supervisor = ImageWorkerSupervisor(
                    worker,
                    concurrency=config.worker_concurrency,
                    idle_poll_seconds=config.idle_poll_seconds,
                    shutdown_seconds=config.graceful_shutdown_seconds,
                    worker_id_prefix="img-" + config.instance_id,
                )
            service = ImageOrchestrationService(
                store,
                allowed_models=config.model_allowlist,
                wake_workers=(supervisor.notify if supervisor is not None else None),
                max_output_count=1,
                model_configuration_resolver=model_resolver,
            )
            lifecycle = ImageProductionLifecycle(
                config=config,
                store=store,
                s3=s3,
                provider=provider,
                supervisor=supervisor,
            )
            return ImageProductionBundle(
                config,
                store,
                content_store,
                provider,
                service,
                authenticator,
                lifecycle,
                mode,
            )
        except BaseException:
            if store is not None:
                store.close()
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass
            s3.close()
            raise
    @staticmethod
    def _static_dependencies(
        config: ImageProductionConfig,
        secrets: ImageSecretProvider,
    ) -> Ed25519ImageJWTAuthenticator:
        if not config.admin_management_enabled:
            token = secrets.read("managed-provider-bearer")
            if (
                not isinstance(token, str)
                or not 24 <= len(token) <= 8192
                or any(character.isspace() or ord(character) < 33 for character in token)
            ):
                raise ImageProductionConfigurationError("managed image credential is unavailable")
        return Ed25519ImageJWTAuthenticator(
            public_keys=parse_ed25519_public_keyring(config.auth_public_keys_json),
            issuer=config.auth_issuer,
            audience=config.auth_audience,
            service_model_ids=config.model_allowlist,
        )

    def _external_dependencies(
        self,
        config: ImageProductionConfig,
        secrets: ImageSecretProvider,
    ) -> tuple[
        _S3Dependency,
        ProductionImageProvider,
        ImageModelConfigurationResolver | None,
    ]:
        s3: _S3Dependency | None = None
        try:
            s3 = _S3Dependency(self.s3_factory.create(config), config)
            if config.admin_management_enabled:
                database_path = config.admin_management_database_path
                assert database_path is not None
                AdminManagementSchemaManager(database_path).validate()
                repository = AdminManagementRepository(
                    database_path,
                    encryption_key=_secret_bytes(
                        secrets.read("model-config-encryption-key"),
                        exact_length=32,
                    ),
                )
                provider: ProductionImageProvider = DynamicManagedImageProvider(
                    repository,
                    provider_id=config.provider_id,
                    origins=config.model_provider_origins,
                    timeout_seconds=config.provider_timeout_seconds,
                    connect_timeout_seconds=config.provider_connect_timeout_seconds,
                    max_image_bytes=config.max_image_bytes,
                    max_connections=config.provider_max_connections,
                    max_concurrency=config.provider_max_concurrency,
                    input_store=S3ImageContentStore(
                        BotoS3ObjectTransport(
                            s3.client,
                            server_side_encryption=config.s3_encryption,
                            kms_key_id=config.s3_kms_key_id,
                        ),
                        bucket=config.s3_bucket,
                        prefix=config.s3_prefix,
                        max_bytes=config.max_image_bytes,
                    ),
                )
                resolver: ImageModelConfigurationResolver | None = (
                    AdminImageModelConfigurationResolver(repository)
                )
            else:
                provider = ManagedHTTPSImageProvider(
                    provider_id=config.provider_id,
                    origin=config.provider_origin,
                    allowed_origins=config.provider_allowed_origins,
                    allowed_models=config.model_allowlist,
                    bearer_token=lambda: secrets.read("managed-provider-bearer"),
                    timeout_seconds=config.provider_timeout_seconds,
                    connect_timeout_seconds=config.provider_connect_timeout_seconds,
                    max_image_bytes=config.max_image_bytes,
                    max_connections=config.provider_max_connections,
                    max_concurrency=config.provider_max_concurrency,
                )
                resolver = None
            return s3, provider, resolver
        except BaseException:
            if s3 is not None:
                try:
                    s3.close()
                except Exception:
                    pass
            raise


async def _probe_and_close_provider(provider: ProductionImageProvider) -> None:
    try:
        await provider.health()
    finally:
        await provider.aclose()


def _run_server(bundle: ImageProductionBundle) -> None:
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - required dependency
        raise ImageProductionConfigurationError("ASGI server dependency is unavailable") from error
    application = bundle.create_app()

    class _DrainingServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            bundle.lifecycle.begin_drain()
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
        asyncio.run(bundle.lifecycle.force_close())


def _absolute_path(environment: Mapping[str, str], name: str) -> Path:
    raw = _required(environment, name, maximum=8192)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ImageProductionConfigurationError(
            "image path setting must be absolute"
        )
    return path.resolve()


def _secret_bytes(value: str, *, exact_length: int) -> bytes:
    try:
        material = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ImageProductionConfigurationError(
            "image secret encoding is invalid"
        ) from None
    if len(material) != exact_length:
        raise ImageProductionConfigurationError(
            "image secret length is invalid"
        )
    return material


def _required(
    environment: Mapping[str, str], name: str, *, maximum: int = 4096
) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ImageProductionConfigurationError("required image setting is unavailable")
    return value


def _integer(
    environment: Mapping[str, str],
    name: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 20:
        raise ImageProductionConfigurationError("image integer setting is invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ImageProductionConfigurationError("image integer setting is out of range")
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
        raise ImageProductionConfigurationError("image numeric setting is invalid") from None
    if not minimum <= value <= maximum:
        raise ImageProductionConfigurationError("image numeric setting is out of range")
    return value


def _boolean(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ImageProductionConfigurationError("image boolean setting is invalid")


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


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    secret_provider: ImageSecretProvider | None = None,
    provider: PostgresS3ManagedImageProvider | None = None,
    server_runner: Callable[[ImageProductionBundle], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="ecorex-image")
    commands = parser.add_subparsers(dest="area", required=True)
    commands.add_parser("serve", help="run an API-only image process")
    commands.add_parser("worker", help="run workers with health endpoints")
    commands.add_parser("all", help="run API and workers in one bounded process")
    schema = commands.add_parser("schema", help="manage the PostgreSQL image schema")
    schema_commands = schema.add_subparsers(dest="action", required=True)
    schema_commands.add_parser("migrate", help="explicitly migrate and verify dependencies")
    schema_commands.add_parser("check", help="read-only schema and dependency check")
    args = parser.parse_args(argv)
    values = os.environ if environment is None else environment
    secrets = secret_provider or EnvironmentImageSecretProvider(values)
    selected = provider or PostgresS3ManagedImageProvider()
    try:
        config = ImageProductionConfig.from_environment(values)
        if args.area == "schema":
            report = (
                selected.migrate(config, secrets)
                if args.action == "migrate"
                else selected.check(config, secrets)
            )
            _json_output(report.to_dict())
            return 0
        bundle = selected.compose(config, secrets, mode=args.area)
        try:
            (server_runner or _run_server)(bundle)
        finally:
            if server_runner is not None:
                asyncio.run(bundle.lifecycle.force_close())
        return 0
    except Exception as error:
        # Never render exception text: DSNs, endpoints, SDK errors and bearer
        # values can contain credentials or deployment topology.
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
    "Boto3ImageS3ClientFactory",
    "EnvironmentImageSecretProvider",
    "ImageProductionBundle",
    "ImageProductionConfig",
    "ImageProductionConfigurationError",
    "ImageProductionLifecycle",
    "ImageProductionReport",
    "ImageProductionS3Client",
    "ImageS3ClientFactory",
    "ImageSecretProvider",
    "PostgresS3ManagedImageProvider",
    "create_image_production_app",
    "main",
]
