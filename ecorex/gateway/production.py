"""Production composition and operator CLI for the managed Model Gateway.

The first-party v1 storage implementation is deliberately single-node SQLite
WAL.  It acquires an exclusive process lock and refuses replica counts other
than one; this is not an HA claim.  ``serve`` performs no DDL.  Operators must
run ``ecorex-gateway schema migrate`` explicitly before starting the service.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import threading
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI

from ecorex.managed_model_policy import require_managed_chat_mapping
from ecorex.security.provider_tls import (
    ProviderTLSConfigurationError,
    pinned_provider_ssl_context,
    validate_provider_ca_binding,
)
from ecorex.control_plane.management import AdminManagementRepository
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.management_models import MANAGED_MODEL_ORIGIN_PRESETS
from ecorex.control_plane.usage_panel_service import build_account_usage_projection

from .production_auth import (
    Ed25519GatewayJWTAuthenticator,
    GatewayAuthenticationConfigurationError,
    parse_ed25519_public_keyring,
)
from .production_storage import (
    GatewayInstanceLock,
    GatewayProductionStorageError,
    validate_gateway_sqlite_health,
)
from .responses_provider import (
    ManagedHTTPSResponsesProvider,
    ResponsesProviderConfigurationError,
    normalize_https_origin,
)
from .dynamic_provider import DynamicManagedResponsesProvider
from .schema import (
    GatewaySchemaManager,
)
from .server import (
    GatewayCompletedUsageFact,
    GatewayUsageAccountant,
    ManagedProviderAdapter,
    SQLiteGatewayStore,
    create_managed_gateway_app,
)
from .models import GatewayAccountUsageProjection


_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_NAMES = {
    "provider-bearer-token": "ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN",
    "model-config-encryption-key": "ECOREX_GATEWAY_MODEL_CONFIG_ENCRYPTION_KEY_B64",
}


class GatewayProductionConfigurationError(RuntimeError):
    """A required production setting or dependency is missing or unsafe."""


@runtime_checkable
class GatewaySecretProvider(Protocol):
    """Seam for Vault, a sidecar or a workload-identity token exchange."""

    def read(self, logical_name: str) -> str: ...


class EnvironmentGatewaySecretProvider:
    """Environment-backed deployment adapter for one fixed logical secret."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def read(self, logical_name: str) -> str:
        environment_name = _SECRET_NAMES.get(logical_name)
        if environment_name is None:
            raise GatewayProductionConfigurationError("unknown gateway secret")
        value = self._environment.get(environment_name)
        if not isinstance(value, str) or not value:
            raise GatewayProductionConfigurationError(
                "required gateway secret is unavailable"
            )
        return value


@runtime_checkable
class ProductionResponsesProvider(ManagedProviderAdapter, Protocol):
    async def health(self) -> None: ...

    async def aclose(self) -> None: ...


class ResponsesProviderFactory(Protocol):
    def create(
        self,
        config: "GatewayProductionConfig",
        secrets: GatewaySecretProvider,
    ) -> ProductionResponsesProvider: ...


class HTTPSResponsesProviderFactory:
    def create(
        self,
        config: "GatewayProductionConfig",
        secrets: GatewaySecretProvider,
    ) -> ProductionResponsesProvider:
        return ManagedHTTPSResponsesProvider(
            origin=config.provider_origin,
            allowed_origins=config.provider_allowed_origins,
            model_mapping=config.model_mapping,
            bearer_token=lambda: secrets.read("provider-bearer-token"),
            connect_timeout_seconds=config.provider_connect_timeout_seconds,
            read_timeout_seconds=config.provider_read_timeout_seconds,
            total_timeout_seconds=config.provider_total_timeout_seconds,
            max_concurrency=config.provider_max_concurrency,
            max_connections=config.provider_max_connections,
            ssl_context=pinned_provider_ssl_context(
                config.provider_ca_bundle_path,
                config.provider_ca_bundle_sha256,
            ),
        )


@dataclass(frozen=True, slots=True)
class GatewayProductionConfig:
    storage_backend: str
    replica_count: int
    database_path: Path
    storage_encryption_at_rest: bool
    allowed_model_ids: frozenset[str]
    model_mapping: Mapping[str, str]
    provider_origin: str
    provider_allowed_origins: frozenset[str]
    auth_issuer: str
    auth_audience: str
    auth_public_keys_json: str = field(repr=False)
    provider_ca_bundle_path: Path | None = None
    provider_ca_bundle_sha256: str | None = field(default=None, repr=False)
    auth_max_token_lifetime_seconds: int = 900
    auth_clock_skew_seconds: int = 30
    provider_connect_timeout_seconds: float = 5.0
    provider_read_timeout_seconds: float = 30.0
    provider_total_timeout_seconds: float = 240.0
    provider_max_concurrency: int = 64
    provider_max_connections: int = 128
    gateway_lease_seconds: int = 300
    readiness_cache_seconds: float = 10.0
    graceful_shutdown_seconds: int = 30
    bind_host: str = "127.0.0.1"
    bind_port: int = 8450
    allow_trusted_ingress_http: bool = False
    limit_concurrency: int = 512
    backlog: int = 1024
    admin_management_enabled: bool = False
    admin_management_database_path: Path | None = None
    model_provider_origins: Mapping[str, str] = field(default_factory=dict)
    chat_handoff_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError:
            raise GatewayProductionConfigurationError(
                "gateway bind host must be an IP address"
            ) from None
        mapping = dict(self.model_mapping)
        try:
            require_managed_chat_mapping(mapping)
        except ValueError:
            raise GatewayProductionConfigurationError(
                "gateway model mapping violates managed model policy"
            ) from None
        try:
            normalized_origin = normalize_https_origin(self.provider_origin)
            normalized_allowed = frozenset(
                normalize_https_origin(item) for item in self.provider_allowed_origins
            )
        except (ResponsesProviderConfigurationError, TypeError):
            raise GatewayProductionConfigurationError(
                "gateway provider origin policy is invalid"
            ) from None
        if (
            self.storage_backend != "sqlite-wal"
            or self.replica_count != 1
            or not isinstance(self.database_path, Path)
            or not self.database_path.is_absolute()
            or not self.storage_encryption_at_rest
            or not self.allowed_model_ids
            or len(self.allowed_model_ids) > 128
            or any(_SAFE_MODEL.fullmatch(item) is None for item in self.allowed_model_ids)
            or set(mapping) != set(self.allowed_model_ids)
            or any(
                _SAFE_MODEL.fullmatch(local) is None
                or _SAFE_MODEL.fullmatch(upstream) is None
                for local, upstream in mapping.items()
            )
            or len(mapping.values()) != len(set(mapping.values()))
            or normalized_origin not in normalized_allowed
            or not isinstance(self.auth_issuer, str)
            or not self.auth_issuer
            or not isinstance(self.auth_audience, str)
            or not self.auth_audience
            or not isinstance(self.auth_public_keys_json, str)
            or not self.auth_public_keys_json
            or not 60 <= self.auth_max_token_lifetime_seconds <= 3600
            or not 0 <= self.auth_clock_skew_seconds <= 120
            or not 0.1 <= self.provider_connect_timeout_seconds <= 30.0
            or not 0.5 <= self.provider_read_timeout_seconds <= 120.0
            or not self.provider_read_timeout_seconds
            <= self.provider_total_timeout_seconds
            <= 900.0
            or not 1
            <= self.provider_max_concurrency
            <= self.provider_max_connections
            <= 512
            or not int(self.provider_total_timeout_seconds) + 30
            <= self.gateway_lease_seconds
            <= 900
            or not 1.0 <= self.readiness_cache_seconds <= 60.0
            or not 5 <= self.graceful_shutdown_seconds <= 300
            or not 1024 <= self.bind_port <= 65535
            or (not address.is_loopback and not self.allow_trusted_ingress_http)
            or not 16 <= self.limit_concurrency <= 4096
            or not 16 <= self.backlog <= 8192
            or not 300 <= self.chat_handoff_ttl_seconds <= 86_400
        ):
            raise GatewayProductionConfigurationError(
                "gateway production configuration is invalid"
            )
        origins = dict(self.model_provider_origins)
        if self.admin_management_enabled:
            if (
                not isinstance(self.admin_management_database_path, Path)
                or not self.admin_management_database_path.is_absolute()
                or not origins
            ):
                raise GatewayProductionConfigurationError(
                    "gateway administrator model source is invalid"
                )
            try:
                normalized_dynamic = {
                    preset: normalize_https_origin(origin)
                    for preset, origin in origins.items()
                    if preset in set(MANAGED_MODEL_ORIGIN_PRESETS.values())
                }
            except (ResponsesProviderConfigurationError, TypeError):
                raise GatewayProductionConfigurationError(
                    "gateway administrator model origins are invalid"
                ) from None
            if len(normalized_dynamic) != len(origins):
                raise GatewayProductionConfigurationError(
                    "gateway administrator model origins are invalid"
                )
            object.__setattr__(
                self, "model_provider_origins", MappingProxyType(normalized_dynamic)
            )
        elif self.admin_management_database_path is not None or origins:
            raise GatewayProductionConfigurationError(
                "gateway administrator model source requires explicit enablement"
            )
        try:
            validate_provider_ca_binding(
                (self.provider_origin, *dict(self.model_provider_origins).values()),
                ca_bundle_path=self.provider_ca_bundle_path,
                ca_bundle_sha256=self.provider_ca_bundle_sha256,
            )
        except ProviderTLSConfigurationError:
            raise GatewayProductionConfigurationError(
                "gateway provider CA binding is invalid"
            ) from None
        try:
            parse_ed25519_public_keyring(self.auth_public_keys_json)
        except GatewayAuthenticationConfigurationError:
            raise GatewayProductionConfigurationError(
                "gateway authentication trust is invalid"
            ) from None
        object.__setattr__(self, "model_mapping", MappingProxyType(mapping))
        object.__setattr__(self, "provider_origin", normalized_origin)
        object.__setattr__(self, "provider_allowed_origins", normalized_allowed)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "GatewayProductionConfig":
        values = os.environ if environment is None else environment
        backend = _required(values, "ECOREX_GATEWAY_STORAGE_BACKEND")
        replicas = _integer(
            values, "ECOREX_GATEWAY_REPLICA_COUNT", minimum=1, maximum=128
        )
        if backend != "sqlite-wal" or replicas != 1:
            raise GatewayProductionConfigurationError(
                "this gateway build supports only single-node SQLite WAL"
            )
        database = _absolute_path(values, "ECOREX_GATEWAY_DATABASE_PATH")
        encrypted = _boolean(
            values, "ECOREX_GATEWAY_STORAGE_ENCRYPTION_AT_REST", default=False
        )
        if not encrypted:
            raise GatewayProductionConfigurationError(
                "encrypted-at-rest gateway storage is required"
            )
        mapping_raw = _json_object(
            _required(values, "ECOREX_GATEWAY_MODEL_MAPPING_JSON", maximum=32_768)
        )
        if not mapping_raw or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in mapping_raw.items()
        ):
            raise GatewayProductionConfigurationError(
                "gateway model mapping is invalid"
            )
        mapping = {str(key): str(value) for key, value in mapping_raw.items()}
        allowed_origins_raw = _json_list(
            _required(
                values,
                "ECOREX_GATEWAY_PROVIDER_ALLOWED_ORIGINS_JSON",
                maximum=16_384,
            )
        )
        if not allowed_origins_raw or any(
            not isinstance(value, str) for value in allowed_origins_raw
        ):
            raise GatewayProductionConfigurationError(
                "gateway provider allowlist is invalid"
            )
        bind_host = values.get("ECOREX_GATEWAY_BIND_HOST", "127.0.0.1")
        try:
            address = ipaddress.ip_address(bind_host)
        except ValueError:
            raise GatewayProductionConfigurationError(
                "gateway bind host must be an IP address"
            ) from None
        trusted_ingress = _boolean(
            values,
            "ECOREX_GATEWAY_ALLOW_TRUSTED_INGRESS_HTTP",
            default=False,
        )
        if not address.is_loopback and not trusted_ingress:
            raise GatewayProductionConfigurationError(
                "non-loopback HTTP requires an explicit trusted-ingress boundary"
            )
        total_timeout = _float(
            values,
            "ECOREX_GATEWAY_PROVIDER_TOTAL_TIMEOUT_SECONDS",
            minimum=1.0,
            maximum=870.0,
            default=240.0,
        )
        management_enabled = _boolean(
            values, "ECOREX_GATEWAY_ADMIN_MANAGEMENT_ENABLED", default=False
        )
        management_database = (
            _absolute_path(values, "ECOREX_GATEWAY_ADMIN_MANAGEMENT_DATABASE_PATH")
            if management_enabled
            else None
        )
        dynamic_origins: dict[str, str] = {}
        raw_dynamic_origins = values.get("ECOREX_GATEWAY_MODEL_PROVIDER_ORIGINS_JSON")
        if raw_dynamic_origins:
            try:
                parsed_dynamic_origins = json.loads(raw_dynamic_origins)
            except json.JSONDecodeError:
                raise GatewayProductionConfigurationError(
                    "gateway administrator model origins are invalid"
                ) from None
            if (
                not isinstance(parsed_dynamic_origins, dict)
                or not parsed_dynamic_origins
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in parsed_dynamic_origins.items()
                )
            ):
                raise GatewayProductionConfigurationError(
                    "gateway administrator model origins are invalid"
                )
            dynamic_origins = {
                str(key): str(value) for key, value in parsed_dynamic_origins.items()
            }
        return cls(
            storage_backend=backend,
            replica_count=replicas,
            database_path=database,
            storage_encryption_at_rest=encrypted,
            allowed_model_ids=frozenset(mapping),
            model_mapping=mapping,
            provider_origin=_required(values, "ECOREX_GATEWAY_PROVIDER_ORIGIN"),
            provider_allowed_origins=frozenset(str(item) for item in allowed_origins_raw),
            auth_issuer=_required(values, "ECOREX_GATEWAY_AUTH_ISSUER"),
            auth_audience=_required(values, "ECOREX_GATEWAY_AUTH_AUDIENCE"),
            auth_public_keys_json=_required(
                values, "ECOREX_GATEWAY_AUTH_PUBLIC_KEYS_JSON", maximum=16_384
            ),
            provider_ca_bundle_path=_optional_absolute_path(
                values, "ECOREX_GATEWAY_PROVIDER_CA_BUNDLE_PATH"
            ),
            provider_ca_bundle_sha256=(
                values.get("ECOREX_GATEWAY_PROVIDER_CA_BUNDLE_SHA256") or None
            ),
            auth_max_token_lifetime_seconds=_integer(
                values,
                "ECOREX_GATEWAY_AUTH_MAX_TOKEN_LIFETIME_SECONDS",
                minimum=60,
                maximum=3600,
                default=900,
            ),
            auth_clock_skew_seconds=_integer(
                values,
                "ECOREX_GATEWAY_AUTH_CLOCK_SKEW_SECONDS",
                minimum=0,
                maximum=120,
                default=30,
            ),
            provider_connect_timeout_seconds=_float(
                values,
                "ECOREX_GATEWAY_PROVIDER_CONNECT_TIMEOUT_SECONDS",
                minimum=0.1,
                maximum=30.0,
                default=5.0,
            ),
            provider_read_timeout_seconds=_float(
                values,
                "ECOREX_GATEWAY_PROVIDER_READ_TIMEOUT_SECONDS",
                minimum=0.5,
                maximum=120.0,
                default=30.0,
            ),
            provider_total_timeout_seconds=total_timeout,
            provider_max_concurrency=_integer(
                values,
                "ECOREX_GATEWAY_PROVIDER_MAX_CONCURRENCY",
                minimum=1,
                maximum=512,
                default=64,
            ),
            provider_max_connections=_integer(
                values,
                "ECOREX_GATEWAY_PROVIDER_MAX_CONNECTIONS",
                minimum=1,
                maximum=512,
                default=128,
            ),
            gateway_lease_seconds=_integer(
                values,
                "ECOREX_GATEWAY_LEASE_SECONDS",
                minimum=30,
                maximum=900,
                default=max(300, int(total_timeout) + 30),
            ),
            readiness_cache_seconds=_float(
                values,
                "ECOREX_GATEWAY_READINESS_CACHE_SECONDS",
                minimum=1.0,
                maximum=60.0,
                default=10.0,
            ),
            graceful_shutdown_seconds=_integer(
                values,
                "ECOREX_GATEWAY_GRACEFUL_SHUTDOWN_SECONDS",
                minimum=5,
                maximum=300,
                default=30,
            ),
            bind_host=bind_host,
            bind_port=_integer(
                values,
                "ECOREX_GATEWAY_BIND_PORT",
                minimum=1024,
                maximum=65535,
                default=8450,
            ),
            allow_trusted_ingress_http=trusted_ingress,
            limit_concurrency=_integer(
                values,
                "ECOREX_GATEWAY_LIMIT_CONCURRENCY",
                minimum=16,
                maximum=4096,
                default=512,
            ),
            backlog=_integer(
                values,
                "ECOREX_GATEWAY_BACKLOG",
                minimum=16,
                maximum=8192,
                default=1024,
            ),
            admin_management_enabled=management_enabled,
            admin_management_database_path=management_database,
            model_provider_origins=dynamic_origins,
            chat_handoff_ttl_seconds=_integer(
                values,
                "ECOREX_GATEWAY_CHAT_HANDOFF_TTL_SECONDS",
                minimum=300,
                maximum=86_400,
                default=3600,
            ),
        )


@dataclass(frozen=True, slots=True)
class GatewayProductionReport:
    action: str
    schema_version: int
    schema_sha256: str
    storage_backend: str = "sqlite-wal-single-node"
    provider_protocol: str = "https-responses-sse-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "ok",
            "action": self.action,
            "gateway_storage_schema_version": self.schema_version,
            "gateway_storage_schema_sha256": self.schema_sha256,
            "storage_backend": self.storage_backend,
            "provider_protocol": self.provider_protocol,
        }


@dataclass(slots=True)
class GatewayProductionBundle:
    config: GatewayProductionConfig
    store: SQLiteGatewayStore
    authenticator: Ed25519GatewayJWTAuthenticator
    provider: ProductionResponsesProvider
    usage_accountant: GatewayUsageAccountant | None
    lifecycle: "SingleNodeGatewayLifecycle"

    def create_app(self) -> FastAPI:
        return create_managed_gateway_app(
            self.store,
            authenticator=self.authenticator,
            provider=self.provider,
            allowed_model_ids=self.config.allowed_model_ids,
            dynamic_model_authority=self.config.admin_management_enabled,
            usage_accountant=self.usage_accountant,
            lease_seconds=self.config.gateway_lease_seconds,
            service_lifecycle=self.lifecycle,
        )


class AdminManagementGatewayUsageAccountant:
    """Settle Gateway facts and expose the canonical cross-version projection."""

    def __init__(self, repository: AdminManagementRepository) -> None:
        self.repository = repository

    def settle(self, fact: GatewayCompletedUsageFact) -> None:
        self.repository.record_provider_usage(
            source_service="managed_gateway",
            source_id=fact.request_id,
            usage_kind="chat",
            account_id=fact.account_id,
            input_tokens=fact.input_tokens,
            output_tokens=fact.output_tokens,
            total_tokens=fact.total_tokens,
            provider_created_at=fact.provider_created_at.isoformat(),
        )

    def reconcile(self, facts: Iterable[GatewayCompletedUsageFact]) -> None:
        for fact in facts:
            self.settle(fact)

    def tokens_available(self, account_id: str) -> bool:
        user = self.repository.get_user(account_id)
        if user.status != "active":
            return False
        return user.token_limit == 0 or user.tokens_used < user.token_limit

    def project(
        self,
        account_id: str,
        *,
        timezone_name: str,
    ) -> GatewayAccountUsageProjection:
        return GatewayAccountUsageProjection.model_validate(
            build_account_usage_projection(
                account_id,
                timezone_name=timezone_name,
            )
        )


class SingleNodeGatewayLifecycle:
    """Own readiness, request admission, drain and the process lock."""

    def __init__(
        self,
        *,
        config: GatewayProductionConfig,
        instance_lock: GatewayInstanceLock,
        provider: ProductionResponsesProvider,
    ) -> None:
        self.config = config
        self.instance_lock = instance_lock
        self.provider = provider
        self._accepting = False
        self._live = False
        self._closed = False
        self._provider_closed = False
        self._condition = threading.Condition()
        self._active_streams = 0
        self._ready_lock = asyncio.Lock()
        self._ready_until = 0.0
        self._ready_value = False

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting and not self._closed

    @property
    def live(self) -> bool:
        with self._condition:
            return self._live and not self._closed

    @property
    def active_streams(self) -> int:
        with self._condition:
            return self._active_streams

    async def startup(self) -> None:
        if self._closed or not self.instance_lock.held:
            raise GatewayProductionConfigurationError(
                "gateway lifecycle cannot start"
            )
        await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(
                    validate_gateway_sqlite_health,
                    self.config.database_path,
                    full=True,
                ),
                self.provider.health(),
            ),
            timeout=min(
                self.config.provider_total_timeout_seconds + 5.0,
                905.0,
            ),
        )
        with self._condition:
            self._live = True
            self._accepting = True

    async def readiness(self) -> bool:
        if not self.accepting:
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
                        asyncio.to_thread(
                            validate_gateway_sqlite_health,
                            self.config.database_path,
                            full=False,
                        ),
                        self.provider.health(),
                    ),
                    timeout=min(
                        self.config.provider_read_timeout_seconds * 2 + 2.0,
                        self.config.provider_total_timeout_seconds + 2.0,
                    ),
                )
                value = self.accepting
            except Exception:
                value = False
            self._ready_value = value
            self._ready_until = loop.time() + self.config.readiness_cache_seconds
            return value

    def admit_stream(self) -> bool:
        with self._condition:
            if not self._accepting or self._closed:
                return False
            self._active_streams += 1
            return True

    def release_stream(self) -> None:
        with self._condition:
            if self._active_streams <= 0:
                raise RuntimeError("gateway stream admission is unbalanced")
            self._active_streams -= 1
            if self._active_streams == 0:
                self._condition.notify_all()

    def begin_drain(self) -> None:
        with self._condition:
            self._accepting = False
            self._ready_value = False
            self._ready_until = 0.0
            self._condition.notify_all()

    async def shutdown(self) -> None:
        with self._condition:
            if self._closed:
                return
        self.begin_drain()
        drained = await asyncio.to_thread(
            self._wait_for_drained,
            self.config.graceful_shutdown_seconds,
        )
        if not drained:
            # Closing the fixed provider client terminates remaining sockets so
            # their generators can persist a redacted uncertain terminal.
            await self.provider.aclose()
            self._provider_closed = True
            drained = await asyncio.to_thread(self._wait_for_drained, 2)
        if not drained:
            # Never release the single-node lock while an ASGI stream can still
            # write.  Process exit will release the OS lock; another replica
            # cannot start early and create a split-brain writer.
            with self._condition:
                self._live = False
            raise GatewayProductionConfigurationError(
                "gateway streams did not drain safely"
            )
        with self._condition:
            self._live = False
            self._closed = True
        try:
            if not self._provider_closed:
                await self.provider.aclose()
                self._provider_closed = True
        finally:
            self.instance_lock.release()

    def _wait_for_drained(self, timeout_seconds: int) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._active_streams == 0,
                timeout=float(timeout_seconds),
            )

    async def force_close(self) -> None:
        await self.shutdown()


class SingleNodeSQLiteResponsesProvider:
    """First-party provider: one SQLite process plus one fixed HTTPS upstream."""

    def __init__(
        self,
        *,
        responses_factory: ResponsesProviderFactory | None = None,
    ) -> None:
        self.responses_factory = responses_factory or HTTPSResponsesProviderFactory()

    def migrate(
        self,
        config: GatewayProductionConfig,
        secrets: GatewaySecretProvider,
    ) -> GatewayProductionReport:
        del secrets
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        process_lock = GatewayInstanceLock(config.database_path)
        process_lock.acquire()
        try:
            receipt = GatewaySchemaManager(config.database_path).migrate()
            if config.admin_management_enabled:
                assert config.admin_management_database_path is not None
                AdminManagementSchemaManager(
                    config.admin_management_database_path
                ).validate()
            validate_gateway_sqlite_health(config.database_path, full=True)
            return GatewayProductionReport(
                action="migrate",
                schema_version=receipt.migration_version,
                schema_sha256=receipt.target_schema_sha256,
            )
        finally:
            process_lock.release()

    def check(
        self,
        config: GatewayProductionConfig,
        secrets: GatewaySecretProvider,
    ) -> GatewayProductionReport:
        process_lock = GatewayInstanceLock(config.database_path)
        process_lock.acquire()
        provider: ProductionResponsesProvider | None = None
        try:
            receipt = GatewaySchemaManager(config.database_path).validate()
            validate_gateway_sqlite_health(config.database_path, full=True)
            self._authenticator(config)
            provider = self._provider(
                config, secrets, handoff_authority=SQLiteGatewayStore(config.database_path)
            )
            if not isinstance(provider, ProductionResponsesProvider):
                raise GatewayProductionConfigurationError(
                    "gateway provider contract is unavailable"
                )
            asyncio.run(_probe_and_close(provider))
            provider = None
            return GatewayProductionReport(
                action="check",
                schema_version=receipt.migration_version,
                schema_sha256=receipt.target_schema_sha256,
            )
        finally:
            try:
                if provider is not None:
                    asyncio.run(provider.aclose())
            finally:
                process_lock.release()

    def compose(
        self,
        config: GatewayProductionConfig,
        secrets: GatewaySecretProvider,
    ) -> GatewayProductionBundle:
        process_lock = GatewayInstanceLock(config.database_path)
        process_lock.acquire()
        provider: ProductionResponsesProvider | None = None
        try:
            # All following storage operations are validate-only.  Missing or
            # drifted schema fails startup and is never repaired by serve.
            validate_gateway_sqlite_health(config.database_path, full=True)
            store = SQLiteGatewayStore(config.database_path)
            authenticator = self._authenticator(config)
            provider = self._provider(config, secrets, handoff_authority=store)
            usage_accountant = self._usage_accountant(config, secrets)
            if not isinstance(provider, ProductionResponsesProvider):
                raise GatewayProductionConfigurationError(
                    "gateway provider contract is unavailable"
                )
            lifecycle = SingleNodeGatewayLifecycle(
                config=config,
                instance_lock=process_lock,
                provider=provider,
            )
            return GatewayProductionBundle(
                config=config,
                store=store,
                authenticator=authenticator,
                provider=provider,
                usage_accountant=usage_accountant,
                lifecycle=lifecycle,
            )
        except BaseException:
            try:
                if provider is not None:
                    try:
                        asyncio.run(provider.aclose())
                    except Exception:
                        pass
            finally:
                process_lock.release()
            raise

    @staticmethod
    def _authenticator(
        config: GatewayProductionConfig,
    ) -> Ed25519GatewayJWTAuthenticator:
        return Ed25519GatewayJWTAuthenticator(
            public_keys=parse_ed25519_public_keyring(
                config.auth_public_keys_json
            ),
            issuer=config.auth_issuer,
            audience=config.auth_audience,
            service_model_ids=(
                None if config.admin_management_enabled else config.allowed_model_ids
            ),
            max_token_lifetime_seconds=config.auth_max_token_lifetime_seconds,
            clock_skew_seconds=config.auth_clock_skew_seconds,
        )

    def _provider(
        self,
        config: GatewayProductionConfig,
        secrets: GatewaySecretProvider,
        *,
        handoff_authority: SQLiteGatewayStore,
    ) -> ProductionResponsesProvider:
        if not config.admin_management_enabled:
            return self.responses_factory.create(config, secrets)
        database_path = config.admin_management_database_path
        assert database_path is not None
        AdminManagementSchemaManager(database_path).validate()
        repository = AdminManagementRepository(
            database_path,
            encryption_key=_secret_bytes(
                secrets.read("model-config-encryption-key"), exact_length=32
            ),
        )
        return DynamicManagedResponsesProvider(
            repository,
            origins=config.model_provider_origins,
            handoff_authority=handoff_authority,
            chat_handoff_ttl_seconds=config.chat_handoff_ttl_seconds,
            connect_timeout_seconds=config.provider_connect_timeout_seconds,
            read_timeout_seconds=config.provider_read_timeout_seconds,
            total_timeout_seconds=config.provider_total_timeout_seconds,
            max_concurrency=config.provider_max_concurrency,
            max_connections=config.provider_max_connections,
            ssl_context=pinned_provider_ssl_context(
                config.provider_ca_bundle_path,
                config.provider_ca_bundle_sha256,
            ),
        )

    @staticmethod
    def _usage_accountant(
        config: GatewayProductionConfig,
        secrets: GatewaySecretProvider,
    ) -> GatewayUsageAccountant | None:
        if not config.admin_management_enabled:
            return None
        database_path = config.admin_management_database_path
        assert database_path is not None
        repository = AdminManagementRepository(
            database_path,
            encryption_key=_secret_bytes(
                secrets.read("model-config-encryption-key"), exact_length=32
            ),
        )
        return AdminManagementGatewayUsageAccountant(repository)


async def _probe_and_close(provider: ProductionResponsesProvider) -> None:
    try:
        await provider.health()
    finally:
        await provider.aclose()


def _run_server(bundle: GatewayProductionBundle) -> None:
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - required deployment dependency
        raise GatewayProductionConfigurationError(
            "ASGI server dependency is unavailable"
        ) from error
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
        raise GatewayProductionConfigurationError(
            "required gateway setting is unavailable"
        )
    return value


def _absolute_path(environment: Mapping[str, str], name: str) -> Path:
    value = Path(_required(environment, name)).expanduser()
    if not value.is_absolute():
        raise GatewayProductionConfigurationError(
            "gateway database path must be absolute"
        )
    return Path(os.path.abspath(os.fspath(value)))


def _optional_absolute_path(
    environment: Mapping[str, str], name: str
) -> Path | None:
    raw = environment.get(name)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or len(raw) > 8192 or "\x00" in raw:
        raise GatewayProductionConfigurationError(
            "gateway optional path setting is invalid"
        )
    value = Path(raw).expanduser()
    if not value.is_absolute():
        raise GatewayProductionConfigurationError(
            "gateway optional path setting must be absolute"
        )
    return Path(os.path.abspath(os.fspath(value)))


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
        raise GatewayProductionConfigurationError("gateway integer setting is invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise GatewayProductionConfigurationError(
            "gateway integer setting is out of range"
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
        raise GatewayProductionConfigurationError("gateway numeric setting is invalid") from None
    if not minimum <= value <= maximum:
        raise GatewayProductionConfigurationError(
            "gateway numeric setting is out of range"
        )
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
    raise GatewayProductionConfigurationError("gateway boolean setting is invalid")


def _secret_bytes(value: str, *, exact_length: int) -> bytes:
    try:
        material = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise GatewayProductionConfigurationError(
            "gateway secret encoding is invalid"
        ) from None
    if len(material) != exact_length:
        raise GatewayProductionConfigurationError(
            "gateway secret length is invalid"
        )
    return material


def _json_object(value: str) -> dict[str, Any]:
    parsed = _json_value(value)
    if not isinstance(parsed, dict):
        raise GatewayProductionConfigurationError("gateway JSON setting is invalid")
    return parsed


def _json_list(value: str) -> list[Any]:
    parsed = _json_value(value)
    if not isinstance(parsed, list) or len(parsed) > 128:
        raise GatewayProductionConfigurationError("gateway JSON setting is invalid")
    return parsed


def _json_value(value: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=unique)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise GatewayProductionConfigurationError("gateway JSON setting is invalid") from None


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
    secret_provider: GatewaySecretProvider | None = None,
    provider: SingleNodeSQLiteResponsesProvider | None = None,
    server_runner: Callable[[GatewayProductionBundle], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="ecorex-gateway")
    commands = parser.add_subparsers(dest="area", required=True)
    commands.add_parser("serve", help="run the single-node production Gateway")
    schema = commands.add_parser("schema", help="manage the Gateway schema")
    schema_commands = schema.add_subparsers(dest="action", required=True)
    schema_commands.add_parser("migrate", help="explicitly migrate the Gateway schema")
    schema_commands.add_parser("check", help="read-only schema and dependency check")
    args = parser.parse_args(argv)
    values = os.environ if environment is None else environment
    secrets = secret_provider or EnvironmentGatewaySecretProvider(values)
    selected = provider or SingleNodeSQLiteResponsesProvider()
    try:
        config = GatewayProductionConfig.from_environment(values)
        if args.area == "schema":
            report = (
                selected.migrate(config, secrets)
                if args.action == "migrate"
                else selected.check(config, secrets)
            )
            _json_output(report.to_dict())
            return 0
        bundle = selected.compose(config, secrets)
        try:
            (server_runner or _run_server)(bundle)
        finally:
            if server_runner is not None:
                asyncio.run(bundle.lifecycle.force_close())
        return 0
    except Exception as error:
        # Never print exception text: SDK/config failures can contain a provider
        # credential, URL, request identity or database path.
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
    "AdminManagementGatewayUsageAccountant",
    "EnvironmentGatewaySecretProvider",
    "GatewayProductionBundle",
    "GatewayProductionConfig",
    "GatewayProductionConfigurationError",
    "GatewayProductionReport",
    "GatewaySecretProvider",
    "HTTPSResponsesProviderFactory",
    "ResponsesProviderFactory",
    "SingleNodeGatewayLifecycle",
    "SingleNodeSQLiteResponsesProvider",
    "main",
]
