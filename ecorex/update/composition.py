"""Product composition for the signed Control Plane to Bootstrap update loop."""

from __future__ import annotations

import os
import re
import ssl
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .coordinator import InstallCoordinator
from .download_cache import (
    DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS,
    DEFAULT_DOWNLOAD_CACHE_MAX_BYTES,
    DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS,
)
from .manifest import ReleaseChannel
from .pack_install import PackContentVerifier
from .service import RuntimeUpdateService
from .transport import (
    ControlPlaneCredentialProvider,
    HTTPArtifactFetcher,
    HTTPSReleaseFeedClient,
    WebSocketUpdateSignalSource,
)
from .verification import Ed25519SignatureVerifier
from .rollback import RollbackAuthorizationVerifier, SingleUseRollbackAuthorizer


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class ProductUpdateSettings:
    database_path: Path | str | os.PathLike[str]
    install_root: Path | str | os.PathLike[str]
    release_feed_endpoint: str
    update_signal_endpoint: str
    trusted_public_keys: Mapping[str, bytes]
    rollback_public_keys: Mapping[str, bytes]
    credentials: ControlPlaneCredentialProvider
    control_plane_hosts: frozenset[str]
    artifact_hosts: frozenset[str]
    current_version: str
    channel: ReleaseChannel
    platform: str
    architecture: str
    health_checker: Callable[[Path], bool]
    drainer: Callable[[], bool]
    migration_dry_run: Callable[[Path], bool]
    migration_prepare: Callable[[Path, str], bool] | None = None
    rollforward_guard: Callable[[Path], bool] | None = None
    poll_interval_seconds: float = 300
    download_cache_max_bytes: int = DEFAULT_DOWNLOAD_CACHE_MAX_BYTES
    download_cache_max_age_seconds: float = DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS
    download_cache_quarantine_age_seconds: float = (
        DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS
    )
    restart_delay_seconds: float = 0.35
    ssl_context: ssl.SSLContext | None = None
    artifact_id: str | None = None
    pack_content_verifier: PackContentVerifier | None = None
    payload_security_preparer: Callable[..., Mapping[str, Any]] | None = None
    payload_security_attester: Callable[..., Mapping[str, Any]] | None = None
    payload_security_cleanup: Callable[..., None] | None = None
    payload_security_orphan_cleanup: Callable[..., None] | None = None
    slot_security_validator: Callable[..., bool] | None = None
    slot_security_cleanup: Callable[..., None] | None = None

    def __post_init__(self) -> None:
        try:
            database_path = Path(self.database_path)
            install_root = Path(self.install_root)
        except TypeError as error:
            raise ValueError("update database and install roots must be paths") from error
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "install_root", install_root)
        if not isinstance(self.channel, ReleaseChannel):
            try:
                object.__setattr__(self, "channel", ReleaseChannel(self.channel))
            except (TypeError, ValueError) as error:
                raise ValueError("update release channel is invalid") from error
        if self.platform not in {"windows", "macos"}:
            raise ValueError("update platform is unsupported")
        if self.architecture not in {"x64", "arm64"} or (
            self.platform == "windows" and self.architecture != "x64"
        ):
            raise ValueError("update architecture is unsupported")
        if not isinstance(self.current_version, str) or _SEMVER.fullmatch(
            self.current_version
        ) is None:
            raise ValueError("current product version must be valid SemVer")
        canonical_artifact_id = f"core-{self.platform}-{self.architecture}"
        if self.artifact_id is None:
            object.__setattr__(self, "artifact_id", canonical_artifact_id)
        elif self.artifact_id != canonical_artifact_id:
            raise ValueError("product update artifact id must be canonical")
        for label, value in (
            ("health_checker", self.health_checker),
            ("drainer", self.drainer),
            ("migration_dry_run", self.migration_dry_run),
        ):
            if not callable(value):
                raise ValueError(f"{label} must be configured")
        if self.migration_prepare is not None and not callable(self.migration_prepare):
            raise ValueError("migration_prepare must be callable")
        if self.rollforward_guard is not None and not callable(self.rollforward_guard):
            raise ValueError("rollforward_guard must be callable")
        if (
            isinstance(self.download_cache_max_bytes, bool)
            or not isinstance(self.download_cache_max_bytes, int)
            or self.download_cache_max_bytes <= 0
        ):
            raise ValueError("download cache max bytes must be positive")
        for label, value in (
            ("download cache max age", self.download_cache_max_age_seconds),
            (
                "download cache quarantine age",
                self.download_cache_quarantine_age_seconds,
            ),
        ):
            if not 60 <= value <= 365 * 24 * 60 * 60:
                raise ValueError(f"{label} is outside the supported range")
        if self.pack_content_verifier is not None and not callable(
            self.pack_content_verifier
        ):
            raise ValueError("pack_content_verifier must be callable")
        if (self.payload_security_preparer is None) != (
            self.payload_security_attester is None
        ):
            raise ValueError("payload security prepare and attest hooks must be paired")
        for label, value in (
            ("payload_security_preparer", self.payload_security_preparer),
            ("payload_security_attester", self.payload_security_attester),
            ("payload_security_cleanup", self.payload_security_cleanup),
            (
                "payload_security_orphan_cleanup",
                self.payload_security_orphan_cleanup,
            ),
            ("slot_security_validator", self.slot_security_validator),
            ("slot_security_cleanup", self.slot_security_cleanup),
        ):
            if value is not None and not callable(value):
                raise ValueError(f"{label} must be callable")
        if not self.control_plane_hosts or not self.artifact_hosts:
            raise ValueError("Control Plane and artifact host allowlists are required")
        object.__setattr__(
            self,
            "control_plane_hosts",
            frozenset(host.casefold() for host in self.control_plane_hosts if host),
        )
        object.__setattr__(
            self,
            "artifact_hosts",
            frozenset(host.casefold() for host in self.artifact_hosts if host),
        )
        if not self.control_plane_hosts or not self.artifact_hosts:
            raise ValueError("Control Plane and artifact host allowlists cannot be empty")
        keys = dict(self.trusted_public_keys)
        if not keys:
            raise ValueError("at least one release signing key is required")
        object.__setattr__(self, "trusted_public_keys", MappingProxyType(keys))
        rollback_keys = dict(self.rollback_public_keys)
        if not rollback_keys:
            raise ValueError("at least one rollback signing key is required")
        release_fingerprints = {
            hashlib.sha256(value).digest() for value in keys.values()
        }
        rollback_fingerprints = {
            hashlib.sha256(value).digest() for value in rollback_keys.values()
        }
        if set(keys).intersection(rollback_keys) or (
            release_fingerprints & rollback_fingerprints
        ):
            raise ValueError("release and rollback trust roles must use distinct keys")
        object.__setattr__(
            self, "rollback_public_keys", MappingProxyType(rollback_keys)
        )


@dataclass(frozen=True, slots=True)
class ProductUpdateComposition:
    settings: ProductUpdateSettings
    verifier: Ed25519SignatureVerifier
    rollback_verifier: RollbackAuthorizationVerifier
    rollback_authorizer: SingleUseRollbackAuthorizer
    fetcher: HTTPArtifactFetcher
    feed: HTTPSReleaseFeedClient
    signal_source: WebSocketUpdateSignalSource
    coordinator: InstallCoordinator
    restart_requester: Any
    service: RuntimeUpdateService

    async def start(self) -> None:
        await self.service.start()

    async def stop(self) -> None:
        await self.service.stop()

    def converge_startup(self):
        return self.service.converge_startup()


def build_product_update_composition(
    settings: ProductUpdateSettings,
    *,
    restart_requester: Any | None = None,
    initialize: bool = True,
    create_storage: bool | None = None,
) -> ProductUpdateComposition:
    """Build the complete production update graph with rejecting trust defaults."""

    if not isinstance(settings, ProductUpdateSettings):
        raise TypeError("settings must be ProductUpdateSettings")
    if not isinstance(initialize, bool):
        raise TypeError("initialize must be boolean")
    if create_storage is None:
        create_storage = initialize
    if not isinstance(create_storage, bool):
        raise TypeError("create_storage must be boolean")
    verifier = Ed25519SignatureVerifier(settings.trusted_public_keys)
    rollback_verifier = RollbackAuthorizationVerifier(
        Ed25519SignatureVerifier(settings.rollback_public_keys)
    )
    rollback_authorizer = SingleUseRollbackAuthorizer(
        rollback_verifier,
        platform=settings.platform,
        architecture=settings.architecture,
    )
    fetcher: HTTPArtifactFetcher | None = None
    feed: HTTPSReleaseFeedClient | None = None
    signal_source: WebSocketUpdateSignalSource | None = None
    try:
        fetcher = HTTPArtifactFetcher(
            allowed_hosts=settings.artifact_hosts,
            ssl_context=settings.ssl_context,
        )
        # Keep the update kernel independent of Bootstrap presentation while
        # using the same signed companion service for install and online update.
        from ecorex.bootstrap.companion import BootstrapCompanionInstaller

        bootstrap_companion = BootstrapCompanionInstaller(
            settings.install_root,
            platform=settings.platform,
            architecture=settings.architecture,
            verifier=verifier,
            fetcher=fetcher,
        )
        coordinator = InstallCoordinator(
            settings.install_root,
            fetcher=fetcher,
            verifier=verifier,
            health_checker=settings.health_checker,
            drainer=settings.drainer,
            migration_dry_run=settings.migration_dry_run,
            migration_prepare=settings.migration_prepare,
            rollforward_guard=settings.rollforward_guard,
            host_platform=settings.platform,
            host_architecture=settings.architecture,
            release_channel=settings.channel,
            # Product Runtime startup, its update poller and first-install
            # readiness recorder execute blocking filesystem work on worker
            # threads.  They share one coordinator and must serialize instead
            # of turning a harmless scheduling race into a failed lifespan.
            lock_timeout=None,
            bootstrap_companion=bootstrap_companion,
            rollback_authorizer=rollback_authorizer.authorize,
            download_cache_max_bytes=settings.download_cache_max_bytes,
            download_cache_max_age_seconds=settings.download_cache_max_age_seconds,
            download_cache_quarantine_age_seconds=(
                settings.download_cache_quarantine_age_seconds
            ),
            pack_content_verifier=settings.pack_content_verifier,
            payload_security_preparer=settings.payload_security_preparer,
            payload_security_attester=settings.payload_security_attester,
            payload_security_cleanup=settings.payload_security_cleanup,
            payload_security_orphan_cleanup=(
                settings.payload_security_orphan_cleanup
            ),
            slot_security_validator=settings.slot_security_validator,
            slot_security_cleanup=settings.slot_security_cleanup,
            create_storage=create_storage,
        )
        feed = HTTPSReleaseFeedClient(
            settings.release_feed_endpoint,
            credentials=settings.credentials,
            verifier=verifier,
            allowed_hosts=settings.control_plane_hosts,
            ssl_context=settings.ssl_context,
            rollback_authorizer=rollback_authorizer,
            current_identity_provider=coordinator.current_release_identity,
        )
        signal_source = WebSocketUpdateSignalSource(
            settings.update_signal_endpoint,
            credentials=settings.credentials,
            allowed_hosts=settings.control_plane_hosts,
            channel=settings.channel,
            platform=settings.platform,
            architecture=settings.architecture,
            current_version=settings.current_version,
            current_identity_provider=coordinator.current_release_identity,
            ssl_context=settings.ssl_context,
        )
        if restart_requester is None:
            # Local import avoids making the low-level update contracts depend
            # on the standalone Bootstrap package at module import time.
            from ecorex.bootstrap import DelayedRestartRequester

            restart_requester = DelayedRestartRequester(
                delay_seconds=settings.restart_delay_seconds
            )
        callback = getattr(restart_requester, "request", restart_requester)
        if not callable(callback):
            raise TypeError("restart_requester must be callable or expose request()")
        service = RuntimeUpdateService(
            settings.database_path,
            coordinator=coordinator,
            feed=feed,
            artifact_id=str(settings.artifact_id),
            current_version=settings.current_version,
            channel=settings.channel,
            platform=settings.platform,
            architecture=settings.architecture,
            signal_source=signal_source,
            restart_requester=callback,
            poll_interval_seconds=settings.poll_interval_seconds,
            initialize=initialize,
        )
        return ProductUpdateComposition(
            settings=settings,
            verifier=verifier,
            rollback_verifier=rollback_verifier,
            rollback_authorizer=rollback_authorizer,
            fetcher=fetcher,
            feed=feed,
            signal_source=signal_source,
            coordinator=coordinator,
            restart_requester=restart_requester,
            service=service,
        )
    except BaseException:
        # Signal construction opens no socket; feed and fetcher own synchronous
        # HTTP clients as soon as they are constructed.
        for resource in (feed, fetcher):
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        raise
