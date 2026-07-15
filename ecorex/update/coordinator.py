"""Durable EcoreX install/update coordinator.

The coordinator owns orchestration and filesystem activation only.  Download
transport, trust verification, draining, migration checks, and health checks
are injected interfaces so the core has no implicit network or unsafe fallback.
"""

from __future__ import annotations

import json
import hashlib
import os
import platform as host_platform_module
import re
import shutil
import stat
import sys
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .fetching import ArtifactFetcher
from .download_cache import (
    DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS,
    DEFAULT_DOWNLOAD_CACHE_MAX_BYTES,
    DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS,
    DownloadCacheError,
    VerifiedDownloadCache,
    VerifiedDownloadLease,
)
from .delta import (
    apply_core_delta_archive,
    select_core_delta_artifact,
)
from .activation import ProvisionalActivationController
from .journal import InstallJournal, InstallState, JournalEntry, TERMINAL_STATES
from .locking import ProductFileLock
from .manifest import MAX_ARTIFACT_BYTES, ReleaseArtifact, ReleaseChannel, ReleaseManifest
from .pack_install import (
    PackContentVerifier,
    PackSetDownloader,
    PreparedPackSet,
    ReleasePackSet,
    resolve_release_pack_set,
    validate_installed_pack_set,
)
from .storage import (
    SlotPointers,
    SlotStore,
    StorageError,
    atomic_write_json,
    atomic_write_text,
    ensure_real_directory,
)
from .verification import (
    RejectingSignatureVerifier,
    SignatureVerifier,
    VerificationError,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)


class UpdateError(RuntimeError):
    pass


class ActiveTransactionError(UpdateError):
    pass


class PinnedTargetError(UpdateError):
    pass


class DownloadFailed(UpdateError):
    pass


class DeltaDownloadFailed(UpdateError):
    pass


class ActivationError(UpdateError):
    pass


class RecoveryError(UpdateError):
    pass


class TargetAdmissionError(UpdateError):
    pass


class RollForwardRequired(UpdateError):
    pass


class ActivationAuthorizationRevoked(ActivationError):
    pass


@dataclass(frozen=True, slots=True)
class TargetPin:
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "registration_complete": False,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TargetPin":
        required = {
            "release_id",
            "version",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "registration_complete",
        }
        if set(raw) != required or raw.get("registration_complete") is not False:
            raise RecoveryError("first-install target pin is malformed")
        values = {key: raw[key] for key in required - {"registration_complete"}}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise RecoveryError("first-install target pin contains invalid values")
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    transaction_id: str
    state: InstallState
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    slot_id: str
    package_path: Path
    slot_path: Path


@dataclass(frozen=True, slots=True)
class ActivationResult:
    transaction_id: str
    state: InstallState
    slot_id: str
    current_slot: str | None
    previous_slot: str | None
    rolled_back: bool = False
    error: str | None = None


_PREPARATION_STATES = frozenset(
    {
        InstallState.RESOLVING,
        InstallState.DOWNLOADING,
        InstallState.VERIFYING,
        InstallState.STAGING,
    }
)
_ACTIVATION_STATES = frozenset(
    {
        InstallState.AWAITING_USER,
        InstallState.DRAINING,
        InstallState.ACTIVATING,
        InstallState.HEALTHCHECKING,
    }
)


class InstallCoordinator:
    """Single-flight, recoverable side-by-side installer."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        fetcher: ArtifactFetcher,
        health_checker: Callable[[Path], bool],
        verifier: SignatureVerifier | None = None,
        drainer: Callable[[], bool] | None = None,
        migration_dry_run: Callable[[Path], bool] | None = None,
        migration_prepare: Callable[[Path, str], bool] | None = None,
        rollforward_guard: Callable[[Path], bool] | None = None,
        pin_recovery_authorizer: Callable[[TargetPin, str], bool] | None = None,
        rollback_authorizer: Callable[[Mapping[str, Any], ReleaseManifest, str], bool]
        | None = None,
        host_platform: str | None = None,
        host_architecture: str | None = None,
        release_channel: ReleaseChannel = ReleaseChannel.STABLE,
        disk_free_provider: Callable[[Path], int] | None = None,
        disk_reserve_bytes: int = 64 * 1024 * 1024,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        download_cache_max_bytes: int = DEFAULT_DOWNLOAD_CACHE_MAX_BYTES,
        download_cache_max_age_seconds: float = DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS,
        download_cache_quarantine_age_seconds: float = (
            DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS
        ),
        lock_timeout: float | None = 0.0,
        bootstrap_health_confirmation: bool = True,
        pack_content_verifier: PackContentVerifier | None = None,
        payload_security_preparer: Callable[
            [Path, Path, Path, ReleaseManifest, ReleaseArtifact], Mapping[str, Any]
        ]
        | None = None,
        payload_security_attester: Callable[
            [
                Path,
                Path,
                Path,
                ReleaseManifest,
                ReleaseArtifact,
                Mapping[str, Any],
            ],
            Mapping[str, Any],
        ]
        | None = None,
        payload_security_cleanup: Callable[
            [Path, Path, ReleaseManifest, ReleaseArtifact, Mapping[str, Any]], None
        ]
        | None = None,
        payload_security_orphan_cleanup: Callable[[Path], None] | None = None,
        slot_security_validator: Callable[
            [Path, ReleaseManifest, ReleaseArtifact, Mapping[str, Any]], bool
        ]
        | None = None,
        slot_security_cleanup: Callable[
            [Path, ReleaseManifest, ReleaseArtifact, Mapping[str, Any]], None
        ]
        | None = None,
        create_storage: bool = True,
    ) -> None:
        self.root = Path(root)
        if create_storage:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.exists():
            ensure_real_directory(self.root, label="update root")
        self.fetcher = fetcher
        self.verifier = verifier or RejectingSignatureVerifier()
        self.health_checker = health_checker
        if not isinstance(bootstrap_health_confirmation, bool):
            raise ValueError("bootstrap_health_confirmation must be boolean")
        self.bootstrap_health_confirmation = bootstrap_health_confirmation
        self.drainer = drainer or (lambda: True)
        self.migration_dry_run = migration_dry_run or (lambda _slot: True)
        self.migration_prepare = migration_prepare or (lambda _slot, _transaction_id: True)
        self.rollforward_guard = rollforward_guard
        self.pin_recovery_authorizer = pin_recovery_authorizer
        self.rollback_authorizer = rollback_authorizer
        detected_platform, detected_architecture = _detect_host()
        self.host_platform = host_platform or detected_platform
        self.host_architecture = host_architecture or detected_architecture
        self.release_channel = release_channel
        self.disk_free_provider = disk_free_provider or (
            lambda path: shutil.disk_usage(path).free
        )
        if disk_reserve_bytes < 0:
            raise ValueError("disk_reserve_bytes must be non-negative")
        if max_artifact_bytes <= 0 or max_artifact_bytes > MAX_ARTIFACT_BYTES:
            raise ValueError("max_artifact_bytes exceeds the release contract")
        self.disk_reserve_bytes = disk_reserve_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.download_cache = VerifiedDownloadCache(
            self.root / "download-cache",
            verifier=self.verifier,
            max_bytes=download_cache_max_bytes,
            max_age_seconds=download_cache_max_age_seconds,
            quarantine_age_seconds=download_cache_quarantine_age_seconds,
            lock_timeout=None,
            create_storage=create_storage,
        )
        self.lock = ProductFileLock(
            self.root / "install-update.lock",
            timeout=lock_timeout,
        )
        self.journal = InstallJournal(self.root / "install-journal.ndjson")
        self.slots = SlotStore(self.root, create_storage=create_storage)
        self.transactions_dir = self.root / "transactions"
        if create_storage:
            self.transactions_dir.mkdir(parents=True, exist_ok=True)
        if self.transactions_dir.exists():
            ensure_real_directory(self.transactions_dir, label="transaction directory")
        self._active_path = self.root / "active-transaction.json"
        self._bootstrap_pin_path = self.root / "first-install-pin.json"
        self._registration_authority_path = (
            self.root / "first-install-registration.json"
        )
        self.activations = ProvisionalActivationController(
            self.root,
            verifier=self.verifier,
            host_platform=self.host_platform,
            host_architecture=self.host_architecture,
            pack_content_verifier=pack_content_verifier,
            create_storage=create_storage,
        )
        self.pack_downloader = PackSetDownloader(
            fetcher=self.fetcher,
            verifier=self.verifier,
            disk_free_provider=self.disk_free_provider,
            disk_reserve_bytes=self.disk_reserve_bytes,
            max_artifact_bytes=self.max_artifact_bytes,
            pack_content_verifier=pack_content_verifier,
            download_cache=self.download_cache,
        )
        self.pack_content_verifier = pack_content_verifier
        if (payload_security_preparer is None) != (payload_security_attester is None):
            raise ValueError("payload security prepare and attest hooks must be paired")
        self.payload_security_preparer = payload_security_preparer
        self.payload_security_attester = payload_security_attester
        self.payload_security_cleanup = payload_security_cleanup
        self.payload_security_orphan_cleanup = payload_security_orphan_cleanup
        if self.payload_security_orphan_cleanup is not None and not callable(
            self.payload_security_orphan_cleanup
        ):
            raise ValueError("payload security orphan cleanup hook must be callable")
        self.slot_security_validator = slot_security_validator
        self.slot_security_cleanup = slot_security_cleanup
        if self.slot_security_cleanup is not None and not callable(
            self.slot_security_cleanup
        ):
            raise ValueError("slot security cleanup hook must be callable")
        self._startup_converged = False
        if create_storage:
            self.converge_startup()

    def converge_startup(self) -> None:
        """Prepare update storage and run local orphan recovery explicitly."""

        if self._startup_converged:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_real_directory(self.root, label="update root")
        self.slots.converge_startup()
        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        ensure_real_directory(self.transactions_dir, label="transaction directory")
        self.activations.converge_startup()
        self.download_cache.converge_startup()
        with self.lock:
            self._cleanup_orphans_locked()
            self.download_cache.collect()
        self._startup_converged = True

    @property
    def latest_state(self) -> InstallState | None:
        latest = self.journal.latest()
        return latest.state if latest else None

    @property
    def pinned_target(self) -> TargetPin | None:
        if not self._bootstrap_pin_path.exists():
            return None
        try:
            raw = json.loads(self._bootstrap_pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RecoveryError("first-install target pin is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise RecoveryError("first-install target pin must contain an object")
        return TargetPin.from_dict(raw)

    def accepts_manifest(self, manifest: ReleaseManifest, artifact_id: str) -> bool:
        """Return whether a release push is compatible with a first-install pin."""

        pin = self.pinned_target
        return pin is None or (
            pin.release_id == manifest.release_id
            and pin.version == manifest.version
            and pin.build_digest == manifest.build_digest
            and pin.artifact_id == artifact_id
            and manifest.artifact(artifact_id).sha256 == pin.artifact_sha256
        )

    def current_release_identity(self) -> Mapping[str, str] | None:
        """Return the exact, re-verified active slot identity for feed binding."""

        with self.lock:
            pointers = self.slots.pointers()
            if pointers.current is None:
                return None
            marker = self.slots.marker(pointers.current)
            manifest = self.slots.release_manifest(pointers.current)
            verify_manifest_signature(manifest, self.verifier)
            artifact_id = marker.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise TargetAdmissionError(
                    "current slot has no valid artifact identity"
                )
            artifact = manifest.artifact(artifact_id)
            verify_artifact_signature(manifest, artifact, self.verifier)
            self._validate_static_target(manifest, artifact)
            identity = {
                "release_id": manifest.release_id,
                "version": manifest.version,
                "build_digest": manifest.build_digest,
                "artifact_id": artifact.artifact_id,
                "artifact_sha256": artifact.sha256,
                "channel": manifest.channel.value,
                "platform": artifact.platform,
                "architecture": artifact.architecture,
            }
            if any(marker.get(key) != value for key, value in identity.items() if key not in {"platform", "architecture"}):
                raise TargetAdmissionError(
                    "current slot identity is not signed by its manifest"
                )
            return identity

    def prepare_update(
        self,
        manifest: ReleaseManifest,
        artifact_id: str,
        *,
        first_install: bool = False,
        rollback_authorization: str | None = None,
    ) -> PreparedUpdate:
        with self.lock:
            return self._prepare_locked(
                manifest,
                artifact_id,
                first_install=first_install,
                rollback_authorization=rollback_authorization,
            )

    # Short aliases are intentionally boring API conveniences for the runtime.
    def prepare(
        self,
        manifest: ReleaseManifest,
        artifact_id: str,
        *,
        first_install: bool = False,
        rollback_authorization: str | None = None,
    ) -> PreparedUpdate:
        return self.prepare_update(
            manifest,
            artifact_id,
            first_install=first_install,
            rollback_authorization=rollback_authorization,
        )

    def activate(self, transaction_id: str | None = None) -> ActivationResult:
        with self.lock:
            active = self._load_active()
            if active is None:
                raise ActivationError("there is no active update transaction")
            if transaction_id is not None and active["transaction_id"] != transaction_id:
                raise ActivationError(
                    f"active transaction is {active['transaction_id']!r}, not {transaction_id!r}"
                )
            return self._activate_locked(active)

    def activate_pending(self, transaction_id: str | None = None) -> ActivationResult:
        return self.activate(transaction_id)

    def authorizes_pending(
        self,
        manifest: ReleaseManifest,
        transaction_id: str,
    ) -> bool:
        """Bind a fresh rollout decision to the exact staged signed manifest."""

        with self.lock:
            active = self._load_active()
            if active is None or active.get("transaction_id") != transaction_id:
                return False
            try:
                expected = self._load_and_verify_active_release(active)
                verify_manifest_signature(manifest, self.verifier)
                artifact = manifest.artifact(str(active["artifact_id"]))
                verify_artifact_signature(manifest, artifact, self.verifier)
                self._validate_static_target(manifest, artifact)
            except Exception:
                return False
            return manifest == expected and self._active_matches(active, manifest, artifact)

    def authorizes_local_pending(self, transaction_id: str) -> bool:
        """Re-verify one staged transaction without consulting the rollout feed.

        This is the narrow authority used after managed-session revocation or
        while the business Runtime is read-only. It accepts only the exact
        locally journaled ``awaiting_user`` transaction and repeats signature,
        artifact digest, slot receipt, Capability Pack, target and security
        validation before activation.
        """

        with self.lock:
            latest = self.journal.latest()
            active = self._load_active()
            if (
                latest is None
                or active is None
                or latest.state is not InstallState.AWAITING_USER
                or latest.transaction_id != transaction_id
                or active.get("transaction_id") != transaction_id
            ):
                return False
            try:
                self._validate_active_against_journal(active)
                manifest = self._load_and_verify_active_release(active)
                artifact = manifest.artifact(str(active["artifact_id"]))
                package_path = self._package_path(active, artifact)
                verify_artifact_file(
                    package_path,
                    manifest,
                    artifact,
                    self.verifier,
                )
                self._validate_staged_release(
                    slot_id=str(active["slot_id"]),
                    package_path=package_path,
                    manifest=manifest,
                    artifact=artifact,
                )
            except Exception:
                return False
            return self._active_matches(active, manifest, artifact)

    def activation_boundary_crossed(self, transaction_id: str) -> bool:
        """Return whether the candidate slot is already the live pointer.

        Runtime admission uses this after an activation call is interrupted by
        a ``BaseException`` or returns a roll-forward-required failure.  The
        answer is derived from durable transaction, journal and pointer facts;
        the in-memory ``switched`` hint is deliberately insufficient because a
        process can be interrupted immediately after the atomic pointer write.
        """

        with self.lock:
            active = self._load_active()
            latest = self.journal.latest()
            if (
                active is None
                or latest is None
                or active.get("transaction_id") != transaction_id
                or latest.transaction_id != transaction_id
                or latest.state
                not in {
                    InstallState.ACTIVATING,
                    InstallState.HEALTHCHECKING,
                    InstallState.COMPLETED,
                    InstallState.FAILED,
                }
            ):
                return False
            slot_id = active.get("slot_id")
            prior = active.get("prior_pointers")
            if not isinstance(slot_id, str) or not isinstance(prior, Mapping):
                return False
            prior_current = prior.get("current")
            if prior_current is not None and not isinstance(prior_current, str):
                return False
            return (
                prior_current != slot_id
                and self.slots.pointers().current == slot_id
            )

    def activation_is_reversible(self, transaction_id: str) -> bool:
        """Return whether rollout revocation can still stop before pointer switch."""

        with self.lock:
            latest = self.journal.latest()
            active = self._load_active()
            if (
                latest is None
                or active is None
                or latest.transaction_id != transaction_id
                or active.get("transaction_id") != transaction_id
                or latest.state
                not in {
                    InstallState.AWAITING_USER,
                    InstallState.DRAINING,
                    InstallState.ACTIVATING,
                }
            ):
                return False
            slot_id = str(active["slot_id"])
            pointers = self.slots.pointers()
            if pointers.current == slot_id:
                return False
            prior_raw = active.get("prior_pointers")
            expected_current = (
                SlotPointers.from_dict(prior_raw).current
                if isinstance(prior_raw, Mapping)
                else active.get("admission_current_slot")
            )
            return pointers.current == expected_current

    def cancel_pending_activation(self, transaction_id: str) -> ActivationResult:
        """Durably cancel an authorized download while pointer switch is reversible."""

        with self.lock:
            latest = self.journal.latest()
            active = self._load_active()
            if (
                latest is None
                or active is None
                or latest.transaction_id != transaction_id
                or active.get("transaction_id") != transaction_id
            ):
                raise ActivationError("there is no matching update activation to cancel")
            if not self.activation_is_reversible(transaction_id):
                if self.slots.pointers().current == str(active["slot_id"]):
                    raise RollForwardRequired(
                        "the active slot pointer already switched; revocation must roll forward"
                    )
                raise ActivationError("update activation is no longer safely cancellable")
            slot_id = str(active["slot_id"])
            pointers = self.slots.pointers()
            protected = {
                item
                for item in (*pointers.known_good, pointers.current, pointers.previous)
                if item is not None
            }
            if slot_id not in protected:
                self._discard_slot(slot_id)
            error = ActivationAuthorizationRevoked(
                "the Control Plane revoked activation authorization"
            )
            self._transition_failed(transaction_id, error)
            return self._activation_result(
                active,
                InstallState.FAILED,
                error=type(error).__name__,
            )

    def recover(self) -> PreparedUpdate | ActivationResult | None:
        """Resume the last incomplete transaction after a process restart."""

        with self.lock:
            latest = self.journal.latest()
            active = self._load_active()
            if latest is None:
                if active is not None:
                    self._discard_orphan_active(active)
                return None
            if active is None or active.get("transaction_id") != latest.transaction_id:
                if latest.state in TERMINAL_STATES:
                    if active is not None:
                        self._discard_orphan_active(active)
                    return None
                raise RecoveryError("journal has an incomplete transaction but active metadata is missing")
            self._validate_active_against_journal(active)
            if latest.state in _PREPARATION_STATES:
                manifest = self._load_transaction_manifest(active)
                return self._prepare_locked(
                    manifest,
                    str(active["artifact_id"]),
                    first_install=bool(active.get("first_install")),
                    rollback_authorization=None,
                )
            if latest.state is InstallState.AWAITING_USER:
                manifest = self._load_and_verify_active_release(active)
                artifact = manifest.artifact(str(active["artifact_id"]))
                package_path = self._package_path(active, artifact)
                try:
                    verify_artifact_file(package_path, manifest, artifact, self.verifier)
                    self._validate_staged_release(
                        slot_id=str(active["slot_id"]),
                        package_path=package_path,
                        manifest=manifest,
                        artifact=artifact,
                    )
                except Exception as exc:
                    self._discard_slot(str(active["slot_id"]))
                    self._transition_failed(latest.transaction_id, exc)
                    raise
                return self._prepared(active, manifest, artifact)
            if latest.state in _ACTIVATION_STATES:
                return self._activate_locked(active)
            result = self._activation_result(
                active,
                latest.state,
                rolled_back=latest.state is InstallState.ROLLBACK,
                error=str(latest.details.get("error_type") or "") or None,
            )
            self._cleanup_transaction(active)
            return result

    def record_registration_authority(self, registration: Mapping[str, Any]) -> bool:
        """Persist device/session authority without treating login as Runtime readiness."""

        normalized = _registration_identity(registration)
        with self.lock:
            pin = self.pinned_target
            if pin is None:
                return False
            value = self._load_registration_authority(required=False) or {
                "schema_version": 1,
                "pin": pin.to_dict(),
                "runtime_ready": None,
                "registration": None,
            }
            if value.get("pin") != pin.to_dict():
                raise PinnedTargetError(
                    "first-install registration belongs to another pinned target"
                )
            prior = value.get("registration")
            if prior is not None and _registration_binding(prior) != _registration_binding(
                normalized
            ):
                raise PinnedTargetError(
                    "first-install registration identity changed before activation"
                )
            value["registration"] = normalized if prior is None else prior
            self._write_registration_authority(value)
            # Device authorization alone is deliberately insufficient. The
            # controlled restart must prove this same lease in a full lifespan.
            return False

    def mark_runtime_ready(
        self,
        registration: Mapping[str, Any] | None,
    ) -> bool:
        """Record full Runtime readiness and clear a matching first-install pin."""

        normalized = (
            _registration_identity(registration)
            if registration is not None
            else None
        )
        with self.lock:
            pin = self.pinned_target
            if pin is None:
                return False
            ready = self._runtime_ready_authority(pin)
            value = self._load_registration_authority(required=False) or {
                "schema_version": 1,
                "pin": pin.to_dict(),
                "runtime_ready": None,
                "registration": None,
            }
            if value.get("pin") != pin.to_dict():
                raise PinnedTargetError(
                    "first-install readiness belongs to another pinned target"
                )
            prior_ready = value.get("runtime_ready")
            if prior_ready is not None and prior_ready != ready:
                raise PinnedTargetError(
                    "first-install Runtime readiness identity changed"
                )
            value["runtime_ready"] = ready
            prior_registration = value.get("registration")
            if normalized is not None:
                if prior_registration is not None and _registration_binding(
                    prior_registration
                ) != _registration_binding(normalized):
                    raise PinnedTargetError(
                        "full Runtime session differs from device registration"
                    )
                value["registration"] = (
                    normalized if prior_registration is None else prior_registration
                )
            self._write_registration_authority(value)
            return self._complete_registration_locked(pin, value)

    def mark_registration_complete(
        self,
        registration: Mapping[str, Any],
    ) -> bool:
        """Compatibility name for the live, full-Runtime readiness proof.

        A checksum-only local receipt is never registration authority.  Crash
        recovery therefore repeats this call with the currently verified
        managed-session lease instead of clearing the pin without arguments.
        """

        return self.mark_runtime_ready(registration)

    def _load_registration_authority(
        self,
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        path = self._registration_authority_path
        if not os.path.lexists(path):
            if required:
                raise PinnedTargetError(
                    "first-install registration authority is missing"
                )
            return None
        try:
            metadata = path.lstat()
            reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            if (
                stat.S_ISLNK(metadata.st_mode)
                or reparse
                or not stat.S_ISREG(metadata.st_mode)
                or not 1 <= metadata.st_size <= 256 * 1024
            ):
                raise OSError
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise PinnedTargetError(
                "first-install registration authority is unreadable"
            ) from None
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "pin",
                "runtime_ready",
                "registration",
                "receipt_digest",
            }
            or value.get("schema_version") != 1
            or value.get("receipt_digest") != _registration_receipt_digest(value)
        ):
            raise PinnedTargetError(
                "first-install registration authority is invalid"
            )
        return value

    def _write_registration_authority(self, value: Mapping[str, Any]) -> None:
        normalized = {
            "schema_version": 1,
            "pin": value.get("pin"),
            "runtime_ready": value.get("runtime_ready"),
            "registration": value.get("registration"),
        }
        normalized["receipt_digest"] = _registration_receipt_digest(normalized)
        atomic_write_json(self._registration_authority_path, normalized)

    def _runtime_ready_authority(self, pin: TargetPin) -> dict[str, Any]:
        pointers = self.slots.pointers()
        if pointers.current is None or pointers.current not in pointers.known_good:
            raise PinnedTargetError(
                "full Runtime readiness requires the known-good pinned slot"
            )
        manifest = self.slots.release_manifest(pointers.current)
        verify_manifest_signature(manifest, self.verifier)
        artifact = manifest.artifact(pin.artifact_id)
        verify_artifact_signature(manifest, artifact, self.verifier)
        self._validate_retained_release(
            slot_id=pointers.current,
            manifest=manifest,
            artifact=artifact,
        )
        marker = self.slots.marker(pointers.current)
        expected = {
            "release_id": pin.release_id,
            "version": pin.version,
            "build_digest": pin.build_digest,
            "artifact_id": pin.artifact_id,
            "artifact_sha256": pin.artifact_sha256,
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            raise PinnedTargetError(
                "active slot does not match the first-install target pin"
            )
        receipt_path = self.activations.receipt_path
        try:
            metadata = receipt_path.lstat()
            reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            if (
                stat.S_ISLNK(metadata.st_mode)
                or reparse
                or not stat.S_ISREG(metadata.st_mode)
                or not 1 <= metadata.st_size <= 256 * 1024
            ):
                raise OSError
            activation_bytes = receipt_path.read_bytes()
            activation = json.loads(activation_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise PinnedTargetError(
                "first-install activation barrier receipt is unavailable"
            ) from None
        health = activation.get("health_identity") if isinstance(activation, Mapping) else None
        if (
            not isinstance(activation, Mapping)
            or activation.get("receipt_digest")
            != _activation_receipt_digest(activation)
            or activation.get("state") != "confirmed"
            or activation.get("data_barrier_crossed") is not True
            or activation.get("slot_id") != pointers.current
            or any(activation.get(key) != value for key, value in expected.items())
            or not isinstance(health, Mapping)
            or health.get("slot_id") != pointers.current
            or health.get("transaction_id") != activation.get("transaction_id")
            or not isinstance(health.get("storage_identity"), str)
            or len(health["storage_identity"]) != 64
        ):
            raise PinnedTargetError(
                "first-install activation barrier receipt is invalid"
            )
        # Re-run the signed-slot and pointer validation. A true return here
        # would mean this method crossed the barrier itself, which is forbidden.
        if self.activations.mark_data_barrier_crossed(pointers.current) is not False:
            raise PinnedTargetError(
                "full Runtime readiness cannot create the data barrier"
            )
        data_generation_id = "gen_" + hashlib.sha256(
            b"EcoreX first-install data generation v1\0"
            + pointers.current.encode()
            + b"\0"
            + str(activation["transaction_id"]).encode()
            + b"\0"
            + str(health["storage_identity"]).encode()
        ).hexdigest()[:26]
        return {
            "slot_id": pointers.current,
            "transaction_id": activation["transaction_id"],
            "release_id": pin.release_id,
            "version": pin.version,
            "build_digest": pin.build_digest,
            "artifact_id": pin.artifact_id,
            "artifact_sha256": pin.artifact_sha256,
            "activation_receipt_sha256": hashlib.sha256(
                activation_bytes
            ).hexdigest(),
            "storage_identity": health["storage_identity"],
            "data_generation_id": data_generation_id,
            "data_barrier_crossed": True,
        }

    def _complete_registration_locked(
        self,
        pin: TargetPin,
        value: Mapping[str, Any],
    ) -> bool:
        ready = value.get("runtime_ready")
        registration = value.get("registration")
        if not isinstance(ready, Mapping) or not isinstance(registration, Mapping):
            return False
        if value.get("pin") != pin.to_dict():
            raise PinnedTargetError(
                "first-install registration target changed"
            )
        if ready != self._runtime_ready_authority(pin):
            raise PinnedTargetError(
                "first-install Runtime readiness became stale"
            )
        _registration_identity(registration)
        latest = self.journal.latest()
        self.journal.append(
            transaction_id=(
                latest.transaction_id
                if latest is not None
                else str(ready["transaction_id"])
            ),
            state=(latest.state if latest is not None else InstallState.COMPLETED),
            event="first_install_registration_completed",
            details={
                "slot_id": ready["slot_id"],
                "account_id_sha256": hashlib.sha256(
                    str(registration["account_id"]).encode()
                ).hexdigest(),
                "session_generation": registration["session_generation"],
                "data_generation_id": ready["data_generation_id"],
            },
        )
        self._bootstrap_pin_path.unlink()
        _fsync_parent(self._bootstrap_pin_path)
        try:
            self._registration_authority_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_parent(self._registration_authority_path)
        return True

    def recover_first_install_pin(self, authorization: str) -> None:
        """Clear a failed first-install pin only through an injected authority."""

        if not authorization:
            raise PinnedTargetError("first-install pin recovery requires authorization")
        with self.lock:
            pin = self.pinned_target
            if pin is None:
                return
            latest = self.journal.latest()
            if latest is None or latest.state not in {
                InstallState.FAILED,
                InstallState.ROLLBACK,
            }:
                raise PinnedTargetError(
                    "first-install pin can only be recovered after a terminal failed install"
                )
            if (
                self.pin_recovery_authorizer is None
                or self.pin_recovery_authorizer(pin, authorization) is not True
            ):
                raise PinnedTargetError("first-install pin recovery was not authorized")
            self.journal.append(
                transaction_id=latest.transaction_id,
                state=latest.state,
                event="first_install_pin_recovery_authorized",
                details={"artifact_id": pin.artifact_id},
            )
            self._bootstrap_pin_path.unlink()
            _fsync_parent(self._bootstrap_pin_path)
            try:
                self._registration_authority_path.unlink()
            except FileNotFoundError:
                pass
            _fsync_parent(self._registration_authority_path)

    def _prepare_locked(
        self,
        manifest: ReleaseManifest,
        artifact_id: str,
        *,
        first_install: bool,
        rollback_authorization: str | None,
    ) -> PreparedUpdate:
        artifact = manifest.artifact(artifact_id)
        verify_manifest_signature(manifest, self.verifier)
        verify_artifact_signature(manifest, artifact, self.verifier)
        self._validate_static_target(manifest, artifact)
        pack_set = self._release_pack_set(manifest)
        if artifact.size_bytes > self.max_artifact_bytes:
            raise TargetAdmissionError("artifact exceeds the configured hard size limit")
        latest = self.journal.latest()
        active = self._load_active()

        if latest is not None and latest.state not in TERMINAL_STATES:
            if active is None or active.get("transaction_id") != latest.transaction_id:
                raise ActiveTransactionError(
                    "an incomplete journal transaction has no matching active metadata"
                )
            if not self._active_matches(active, manifest, artifact):
                raise ActiveTransactionError(
                    f"transaction {latest.transaction_id!r} must finish before another target is prepared"
                )
            if bool(active.get("first_install")) is not first_install:
                raise ActiveTransactionError(
                    "first-install intent does not match the durable transaction"
                )
            if latest.state is InstallState.AWAITING_USER:
                package_path = self._package_path(active, artifact)
                verify_artifact_file(package_path, manifest, artifact, self.verifier)
                self._validate_staged_release(
                    slot_id=str(active["slot_id"]),
                    package_path=package_path,
                    manifest=manifest,
                    artifact=artifact,
                )
                return self._prepared(active, manifest, artifact)
            if latest.state not in _PREPARATION_STATES:
                raise ActiveTransactionError(
                    f"transaction {latest.transaction_id!r} is already {latest.state.value}"
                )
        else:
            if first_install and self.slots.pointers().current is not None:
                raise TargetAdmissionError(
                    "first-install flow cannot run when a current slot already exists"
                )
            rollback_authorized = self._admit_target(
                manifest,
                artifact,
                rollback_authorization=rollback_authorization,
            )
            admission_current_slot = self.slots.pointers().current
            if not first_install and not self.accepts_manifest(manifest, artifact.artifact_id):
                raise PinnedTargetError(
                    "a different update is ignored until first-install registration completes"
                )
            active = self._start_transaction(
                manifest,
                artifact,
                first_install=first_install,
                rollback_authorized=rollback_authorized,
                admission_current_slot=admission_current_slot,
            )
            latest = self.journal.latest()

        assert active is not None and latest is not None
        transaction_id = str(active["transaction_id"])
        package_path = self._package_path(active, artifact)

        if latest.state is InstallState.RESOLVING:
            try:
                if first_install:
                    self._ensure_first_install_pin(manifest, artifact)
                elif not self.accepts_manifest(manifest, artifact.artifact_id):
                    raise PinnedTargetError(
                        "a different update is ignored until first-install registration completes"
                    )
            except Exception as exc:
                self._transition_failed(transaction_id, exc)
                raise
            latest = self._transition(
                transaction_id,
                InstallState.DOWNLOADING,
                "manifest_verified",
                {"artifact_id": artifact.artifact_id},
            )

        if latest.state is InstallState.VERIFYING:
            try:
                verify_artifact_file(package_path, manifest, artifact, self.verifier)
            except VerificationError:
                self._unlink_package(package_path)
                verifying_index_raw = active.get("verifying_source_index")
                if verifying_index_raw is None:
                    verifying_source_id = latest.details.get("source_id")
                    verifying_index_raw = next(
                        (
                            index
                            for index, source in enumerate(manifest.sources)
                            if source.source_id == verifying_source_id
                        ),
                        active.get("source_index", 0),
                    )
                verifying_index = int(verifying_index_raw)
                active["source_index"] = verifying_index + 1
                self._save_active(active)
                latest = self._transition(
                    transaction_id,
                    InstallState.DOWNLOADING,
                    "artifact_rejected_after_recovery",
                    {"next_source_index": active["source_index"]},
                )
            else:
                with self.download_cache.acquire(manifest, artifact) as cache_lease:
                    cache_lease.admit(package_path)
                latest = self._transition(
                    transaction_id,
                    InstallState.STAGING,
                    "artifact_verified_after_recovery",
                    {"sha256": artifact.sha256},
                )

        if latest.state is InstallState.DOWNLOADING:
            latest = self._download_and_verify(
                active,
                manifest,
                artifact,
                package_path,
            )

        if latest.state is InstallState.STAGING:
            try:
                # STAGING is a durable crash boundary: authenticate the exact
                # bytes again before consuming them.
                verify_manifest_signature(manifest, self.verifier)
                verify_artifact_file(package_path, manifest, artifact, self.verifier)
                self._ensure_stage_space(package_path)
                prepared_packs = self._prepare_pack_set(
                    active,
                    manifest,
                    pack_set,
                )
                self.slots.stage(
                    package_path,
                    slot_id=str(active["slot_id"]),
                    manifest=manifest,
                    artifact=artifact,
                    payload_enricher=(
                        prepared_packs.payload_enricher
                        if prepared_packs is not None
                        else None
                    ),
                    payload_preparer=(
                        (
                            lambda slot_root, payload_root: self.payload_security_preparer(
                                slot_root,
                                payload_root,
                                package_path,
                                manifest,
                                artifact,
                            )
                        )
                        if self.payload_security_preparer is not None
                        else None
                    ),
                    payload_attester=(
                        (
                            lambda slot_root, payload_root, prepared: self.payload_security_attester(
                                slot_root,
                                payload_root,
                                package_path,
                                manifest,
                                artifact,
                                prepared,
                            )
                        )
                        if self.payload_security_attester is not None
                        else None
                    ),
                    payload_cleanup=(
                        (
                            lambda slot_root, payload_root, prepared: self.payload_security_cleanup(
                                slot_root,
                                payload_root,
                                manifest,
                                artifact,
                                prepared,
                            )
                        )
                        if self.payload_security_cleanup is not None
                        else None
                    ),
                )
                self._validate_staged_release(
                    slot_id=str(active["slot_id"]),
                    package_path=package_path,
                    manifest=manifest,
                    artifact=artifact,
                )
                # Bound cache growth only after all Core/Pack bytes are safely
                # staged. GC failure is observable but never invalidates an
                # otherwise verified update candidate.
                try:
                    self.download_cache.collect(
                        keep_digests=(
                            item.sha256
                            for item in manifest.artifacts
                            if item.platform in {artifact.platform, "all"}
                            and item.architecture in {artifact.architecture, "all"}
                        )
                    )
                except DownloadCacheError as cache_error:
                    self._transition(
                        transaction_id,
                        InstallState.STAGING,
                        "download_cache_maintenance_failed",
                        {"error_type": type(cache_error).__name__},
                    )
            except Exception as exc:
                # A failure may happen after SlotStore atomically renamed the
                # fully extracted Core+Pack tree but before the composite
                # verification completed. Never retain that unprotected
                # candidate as a reusable slot on retry.
                try:
                    self._discard_slot(str(active["slot_id"]))
                except StorageError:
                    pass
                self._transition_failed(transaction_id, exc)
                raise
            self._transition(
                transaction_id,
                InstallState.AWAITING_USER,
                "slot_staged",
                {"slot_id": active["slot_id"]},
            )
        return self._prepared(active, manifest, artifact)

    def _download_and_verify(
        self,
        active: dict[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        package_path: Path,
    ) -> JournalEntry:
        with self.download_cache.acquire(manifest, artifact) as cache_lease:
            if cache_lease.materialize(package_path):
                self._transition(
                    str(active["transaction_id"]),
                    InstallState.VERIFYING,
                    "download_cache_restore_finished",
                    {"sha256": artifact.sha256},
                )
                return self._transition(
                    str(active["transaction_id"]),
                    InstallState.STAGING,
                    "artifact_restored_from_download_cache",
                    {"sha256": artifact.sha256},
                )
            return self._download_and_verify_under_cache_lease(
                active,
                manifest,
                artifact,
                package_path,
                cache_lease=cache_lease,
            )

    def _download_and_verify_under_cache_lease(
        self,
        active: dict[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        package_path: Path,
        *,
        cache_lease: VerifiedDownloadLease,
    ) -> JournalEntry:
        transaction_id = str(active["transaction_id"])
        start_index = int(active.get("source_index", 0))
        last_error: BaseException | None = None
        if start_index == 0 and not os.path.lexists(package_path):
            try:
                delta_result = self._try_signed_delta(
                    active=active,
                    manifest=manifest,
                    artifact=artifact,
                    package_path=package_path,
                    target_cache_lease=cache_lease,
                )
            except Exception as exc:
                # Delta is an optimization only.  A malformed signed delta,
                # corrupt retained base, transport error or interrupted patch
                # must converge on the ordinary full-package source order.
                last_error = exc
                self._unlink_package(package_path)
                self._transition(
                    transaction_id,
                    InstallState.DOWNLOADING,
                    "delta_fallback_to_full",
                    {"error_type": type(exc).__name__},
                )
            else:
                if delta_result is not None:
                    return delta_result
        for index in range(start_index, len(manifest.sources)):
            source = manifest.sources[index]
            active["source_index"] = index
            self._save_active(active)
            if package_path.exists() and package_path.stat().st_size > artifact.size_bytes:
                self._unlink_package(package_path)
            resume_from = package_path.stat().st_size if package_path.exists() else 0
            try:
                self._ensure_download_space(package_path, artifact.size_bytes - resume_from)
            except Exception as exc:
                self._transition_failed(transaction_id, exc)
                raise
            self._transition(
                transaction_id,
                InstallState.DOWNLOADING,
                "source_attempted",
                {
                    "source_id": source.source_id,
                    "source_kind": source.kind.value,
                    "priority": source.priority,
                    "resume_from": resume_from,
                },
            )
            try:
                if resume_from < artifact.size_bytes:
                    self.fetcher.fetch(
                        source,
                        artifact,
                        package_path,
                        resume_from=resume_from,
                        max_bytes=artifact.size_bytes,
                    )
            except Exception as exc:
                last_error = exc
                # A partial prefix is resumable only from the same origin after
                # a crash.  Never splice bytes from two independent mirrors.
                self._unlink_package(package_path)
                active["source_index"] = index + 1
                self._save_active(active)
                self._transition(
                    transaction_id,
                    InstallState.DOWNLOADING,
                    "source_failed",
                    {
                        "source_id": source.source_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            active["verifying_source_index"] = index
            self._save_active(active)
            self._transition(
                transaction_id,
                InstallState.VERIFYING,
                "download_finished",
                {"source_id": source.source_id},
            )
            try:
                verify_artifact_file(package_path, manifest, artifact, self.verifier)
            except VerificationError as exc:
                last_error = exc
                self._unlink_package(package_path)
                active["source_index"] = index + 1
                self._save_active(active)
                if index + 1 < len(manifest.sources):
                    self._transition(
                        transaction_id,
                        InstallState.DOWNLOADING,
                        "source_artifact_rejected",
                        {
                            "source_id": source.source_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                break
            try:
                cache_lease.admit(package_path)
            except DownloadCacheError as exc:
                self._transition_failed(transaction_id, exc)
                raise
            return self._transition(
                transaction_id,
                InstallState.STAGING,
                "artifact_verified",
                {"source_id": source.source_id, "sha256": artifact.sha256},
            )

        failure = DownloadFailed(
            "all signed release sources failed"
            + (f" ({type(last_error).__name__})" if last_error else "")
        )
        self._transition_failed(transaction_id, failure)
        raise failure from last_error

    def _try_signed_delta(
        self,
        *,
        active: dict[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        package_path: Path,
        target_cache_lease: VerifiedDownloadLease,
    ) -> JournalEntry | None:
        """Attempt one signed base-bound delta, preserving full fallback."""

        if not artifact.artifact_id.startswith("core-"):
            return None
        current_slot = active.get("admission_current_slot")
        if (
            not isinstance(current_slot, str)
            or not current_slot
            or self.slots.pointers().current != current_slot
        ):
            return None
        base_manifest = self.slots.release_manifest(current_slot)
        marker = self.slots.marker(current_slot)
        base_artifact_id = marker.get("artifact_id")
        if not isinstance(base_artifact_id, str):
            return None
        base_artifact = base_manifest.artifact(base_artifact_id)
        if (
            base_artifact.platform != artifact.platform
            or base_artifact.architecture != artifact.architecture
        ):
            return None
        delta_artifact = select_core_delta_artifact(
            manifest,
            target_artifact=artifact,
            base_artifact=base_artifact,
        )
        if delta_artifact is None:
            return None
        verify_artifact_signature(manifest, delta_artifact, self.verifier)
        base_package = self.slots.slot_path(current_slot) / ".release-package"
        delta_path = package_path.with_name(delta_artifact.file_name)
        self._unlink_package(delta_path)
        transaction_id = str(active["transaction_id"])
        last_error: BaseException | None = None
        with self.download_cache.acquire(manifest, delta_artifact) as delta_cache_lease:
            restored_from_cache = delta_cache_lease.materialize(delta_path)
            sources = (None,) if restored_from_cache else tuple(manifest.sources)
            for source in sources:
                source_id = "download-cache" if source is None else source.source_id
                self._ensure_download_space(
                    delta_path,
                    delta_artifact.size_bytes + artifact.size_bytes,
                )
                self._transition(
                    transaction_id,
                    InstallState.DOWNLOADING,
                    (
                        "delta_restored_from_download_cache"
                        if source is None
                        else "delta_source_attempted"
                    ),
                    {
                        "source_id": source_id,
                        "delta_artifact_id": delta_artifact.artifact_id,
                        "base_release_id": base_manifest.release_id,
                    },
                )
                if source is not None:
                    try:
                        self.fetcher.fetch(
                            source,
                            delta_artifact,
                            delta_path,
                            resume_from=0,
                            max_bytes=delta_artifact.size_bytes,
                        )
                        verify_artifact_file(
                            delta_path,
                            manifest,
                            delta_artifact,
                            self.verifier,
                        )
                        delta_cache_lease.admit(delta_path)
                    except Exception as exc:
                        last_error = exc
                        self._unlink_package(delta_path)
                        self._transition(
                            transaction_id,
                            InstallState.DOWNLOADING,
                            "delta_source_failed",
                            {
                                "source_id": source_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        continue
                self._transition(
                    transaction_id,
                    InstallState.VERIFYING,
                    "delta_download_finished",
                    {
                        "source_id": source_id,
                        "delta_artifact_id": delta_artifact.artifact_id,
                    },
                )
                try:
                    apply_core_delta_archive(
                        delta_path=delta_path,
                        delta_artifact=delta_artifact,
                        base_package=base_package,
                        base_manifest=base_manifest,
                        base_artifact=base_artifact,
                        target_path=package_path,
                        target_manifest=manifest,
                        target_artifact=artifact,
                        verifier=self.verifier,
                    )
                    target_cache_lease.admit(package_path)
                except Exception as exc:
                    last_error = exc
                    self._unlink_package(delta_path)
                    self._unlink_package(package_path)
                    self._transition(
                        transaction_id,
                        InstallState.DOWNLOADING,
                        "delta_rejected",
                        {
                            "source_id": source_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                self._unlink_package(delta_path)
                return self._transition(
                    transaction_id,
                    InstallState.STAGING,
                    "delta_applied",
                    {
                        "source_id": source_id,
                        "delta_artifact_id": delta_artifact.artifact_id,
                        "base_release_id": base_manifest.release_id,
                        "target_sha256": artifact.sha256,
                    },
                )
        if last_error is not None:
            raise DeltaDownloadFailed from last_error
        return None

    def _activate_locked(self, active: dict[str, Any]) -> ActivationResult:
        latest = self.journal.latest()
        transaction_id = str(active["transaction_id"])
        if latest is None or latest.transaction_id != transaction_id:
            raise ActivationError("active metadata does not match the install journal")
        self._validate_active_against_journal(active)
        slot_id = str(active["slot_id"])
        slot_path = self.slots.slot_path(slot_id)

        if latest.state in TERMINAL_STATES:
            result = self._activation_result(
                active,
                latest.state,
                rolled_back=latest.state is InstallState.ROLLBACK,
                error=str(latest.details.get("error_type") or "") or None,
            )
            self._cleanup_transaction(active)
            return result
        if latest.state not in _ACTIVATION_STATES:
            raise ActivationError(
                f"transaction must be prepared before activation; current state is {latest.state.value}"
            )

        manifest = self._load_and_verify_active_release(active)
        artifact = manifest.artifact(str(active["artifact_id"]))
        package_path = self._package_path(active, artifact)
        if latest.state in {InstallState.AWAITING_USER, InstallState.DRAINING}:
            expected_current = active.get("admission_current_slot")
            observed_current = self.slots.pointers().current
            if observed_current != expected_current:
                error = RecoveryError(
                    "current slot changed after target admission; activation authorization is stale"
                )
                if observed_current != slot_id:
                    self._discard_slot(slot_id)
                self._transition_failed(transaction_id, error)
                raise error
        try:
            verify_artifact_file(package_path, manifest, artifact, self.verifier)
            self._validate_staged_release(
                slot_id=slot_id,
                package_path=package_path,
                manifest=manifest,
                artifact=artifact,
            )
        except Exception as exc:
            if self.slots.pointers().current == slot_id:
                active["health_failed"] = True
                active["rollforward_required"] = True
                active["health_error"] = type(exc).__name__
                self._save_active(active)
            else:
                self._discard_slot(slot_id)
            self._transition_failed(transaction_id, exc)
            raise

        if latest.state in {InstallState.AWAITING_USER, InstallState.DRAINING}:
            if latest.state is InstallState.AWAITING_USER:
                latest = self._transition(
                    transaction_id,
                    InstallState.DRAINING,
                    "activation_confirmed",
                    {},
                )
            try:
                drained = self.drainer()
                if drained is not True:
                    raise ActivationError("runtime refused to drain active work")
                migration_ready = self.migration_dry_run(slot_path)
                if migration_ready is not True:
                    raise ActivationError("migration dry-run did not pass")
                migration_prepared = self.migration_prepare(slot_path, transaction_id)
                if migration_prepared is not True:
                    raise ActivationError("migration preparation did not pass")
                if latest.event != "migration_prepared":
                    latest = self._transition(
                        transaction_id,
                        InstallState.DRAINING,
                        "migration_prepared",
                        {"slot_id": slot_id},
                    )
            except Exception as exc:
                self._discard_slot(slot_id)
                self._transition_failed(transaction_id, exc)
                result = self._activation_result(
                    active,
                    InstallState.FAILED,
                    error=type(exc).__name__,
                )
                self._cleanup_transaction(active)
                return result
            if "prior_pointers" not in active:
                active["prior_pointers"] = self.slots.pointers().to_dict()
                self._save_active(active)
            latest = self._transition(
                transaction_id,
                InstallState.ACTIVATING,
                "runtime_drained",
                {"slot_id": slot_id},
            )

        if latest.state is InstallState.ACTIVATING:
            prior = self._prior_pointers(active)
            self._validate_staged_release(
                slot_id=slot_id,
                package_path=package_path,
                manifest=manifest,
                artifact=artifact,
            )
            if self.bootstrap_health_confirmation:
                try:
                    intent = self.activations.create_intent(
                        active=active,
                        manifest=manifest,
                        artifact=artifact,
                        prior_pointers=prior,
                    )
                    if latest.event != "activation_intent_persisted":
                        latest = self._transition(
                            transaction_id,
                            InstallState.ACTIVATING,
                            "activation_intent_persisted",
                            {
                                "slot_id": slot_id,
                                "intent_digest": intent.intent_digest,
                            },
                        )
                    self.activations.ensure_pending_current(transaction_id)
                    active["switched"] = True
                    active["bootstrap_health_pending"] = True
                    self._save_active(active)
                    latest = self.journal.latest()
                    assert latest is not None
                except Exception as exc:
                    intent = self.activations.load_intent(required=False)
                    if intent is not None and intent.transaction_id == transaction_id:
                        try:
                            terminal = self.activations.fail_pre_data(
                                transaction_id,
                                error_code=type(exc).__name__,
                            )
                        except Exception as convergence_error:
                            pointers = self.slots.pointers()
                            if (
                                pointers.current == slot_id
                                and slot_id not in pointers.known_good
                            ):
                                self._validate_rollback_target(prior)
                                self.slots.restore(prior)
                            self._transition_failed(transaction_id, convergence_error)
                            self._cleanup_transaction(active)
                            terminal = InstallState.FAILED
                    else:
                        terminal = InstallState.FAILED
                        if self.slots.pointers() == prior:
                            try:
                                self._discard_slot(slot_id)
                            except StorageError:
                                pass
                        self._transition_failed(transaction_id, exc)
                        self._cleanup_transaction(active)
                    return self._activation_result(
                        active,
                        terminal,
                        rolled_back=terminal is InstallState.ROLLBACK,
                        error=type(exc).__name__,
                    )
            else:
                pointers = self.slots.pointers()
                try:
                    if pointers.current == slot_id:
                        pass  # switch completed before a crash; continue with health check
                    elif pointers == prior:
                        self.slots.switch_to(slot_id)
                    else:
                        raise RecoveryError(
                            "slot pointers changed outside the active install transaction"
                        )
                    active["switched"] = True
                    self._save_active(active)
                    latest = self._transition(
                        transaction_id,
                        InstallState.HEALTHCHECKING,
                        "slot_activated",
                        {"slot_id": slot_id},
                    )
                except Exception as exc:
                    # If a switch happened, restore before recording a terminal
                    # state.  A crash during restore remains recoverable from the
                    # still-nonterminal journal entry.
                    pointers_after_error = self.slots.pointers()
                    if active.get("switched") or pointers_after_error.current == slot_id:
                        self._validate_rollback_target(prior)
                        self.slots.restore(prior)
                    self._discard_slot(slot_id)
                    self._transition_failed(transaction_id, exc)
                    result = self._activation_result(
                        active,
                        InstallState.FAILED,
                        error=type(exc).__name__,
                    )
                    self._cleanup_transaction(active)
                    return result

        if latest.state is InstallState.HEALTHCHECKING:
            if self.bootstrap_health_confirmation:
                return self._activation_result(active, InstallState.HEALTHCHECKING)
            prior = self._prior_pointers(active)
            if active.get("health_failed"):
                if not active.get("rollforward_required"):
                    try:
                        if active.get("rollback_safe") is not True:
                            raise RollForwardRequired("no durable rollback-safe decision")
                        self._validate_rollback_target(prior)
                    except Exception:
                        active["rollforward_required"] = True
                        self._save_active(active)
                if active.get("rollforward_required"):
                    self._transition(
                        transaction_id,
                        InstallState.FAILED,
                        "rollforward_required_recovered",
                        {"slot_id": slot_id, "error_type": "RollForwardRequired"},
                    )
                    result = self._activation_result(
                        active,
                        InstallState.FAILED,
                        error="RollForwardRequired",
                    )
                    self._cleanup_transaction(active)
                    return result
                self.slots.restore(prior)
                self._transition(
                    transaction_id,
                    InstallState.ROLLBACK,
                    "rollback_recovered",
                    {"slot_id": slot_id},
                )
                result = self._activation_result(
                    active,
                    InstallState.ROLLBACK,
                    rolled_back=True,
                    error="HealthCheckFailed",
                )
                self._prune_slots(max_slots=3)
                self._cleanup_transaction(active)
                return result
            try:
                healthy = self.health_checker(slot_path) is True
                health_error = None
                if healthy:
                    self._validate_staged_release(
                        slot_id=slot_id,
                        package_path=package_path,
                        manifest=manifest,
                        artifact=artifact,
                    )
            except Exception as exc:
                healthy = False
                health_error = type(exc).__name__
            if not healthy:
                active["health_failed"] = True
                active["health_error"] = health_error or "HealthCheckFailed"
                rollback_safe = self._rollback_is_safe(slot_path, prior)
                active["rollback_safe"] = rollback_safe
                active["rollforward_required"] = not rollback_safe
                self._save_active(active)
                if active["rollforward_required"]:
                    self._transition(
                        transaction_id,
                        InstallState.FAILED,
                        "healthcheck_failed_rollforward_required",
                        {
                            "slot_id": slot_id,
                            "error_type": "RollForwardRequired",
                        },
                    )
                    result = self._activation_result(
                        active,
                        InstallState.FAILED,
                        error="RollForwardRequired",
                    )
                    self._cleanup_transaction(active)
                    return result
                self.slots.restore(prior)
                self._transition(
                    transaction_id,
                    InstallState.ROLLBACK,
                    "healthcheck_failed_rolled_back",
                    {
                        "slot_id": slot_id,
                        "error_type": active["health_error"],
                    },
                )
                result = self._activation_result(
                    active,
                    InstallState.ROLLBACK,
                    rolled_back=True,
                    error=str(active["health_error"]),
                )
                self._prune_slots(max_slots=3)
                self._cleanup_transaction(active)
                return result
            self.slots.mark_known_good(slot_id, keep=3)
            self._transition(
                transaction_id,
                InstallState.COMPLETED,
                "healthcheck_passed",
                {"slot_id": slot_id},
            )
            self._prune_slots(max_slots=3)
            result = self._activation_result(active, InstallState.COMPLETED)
            self._cleanup_transaction(active)
            return result

        raise RecoveryError(f"unhandled activation state: {latest.state.value}")

    def _start_transaction(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        *,
        first_install: bool,
        rollback_authorized: bool,
        admission_current_slot: str | None,
    ) -> dict[str, Any]:
        transaction_id = uuid.uuid4().hex
        slot_id = _slot_id(manifest, artifact)
        transaction_dir = self.transactions_dir / transaction_id
        transaction_dir.mkdir(parents=True, exist_ok=False)
        _fsync_parent(transaction_dir)
        atomic_write_text(
            transaction_dir / "release-manifest.json",
            manifest.to_json(include_signature=True, pretty=True) + "\n",
        )
        active: dict[str, Any] = {
            "transaction_id": transaction_id,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "slot_id": slot_id,
            "first_install": first_install,
            "rollback_authorized": rollback_authorized,
            "admission_current_slot": admission_current_slot,
            "source_index": 0,
        }
        self._save_active(active)
        try:
            self.journal.append(
                transaction_id=transaction_id,
                state=InstallState.RESOLVING,
                event="transaction_started",
                details={
                    "release_id": manifest.release_id,
                    "version": manifest.version,
                    "build_digest": manifest.build_digest,
                    "artifact_id": artifact.artifact_id,
                    "first_install": first_install,
                    "rollback_authorized": rollback_authorized,
                    "admission_current_slot": admission_current_slot,
                },
            )
        except Exception:
            self._discard_orphan_active(active)
            raise
        return active

    def _ensure_first_install_pin(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> None:
        requested = TargetPin(
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
        )
        existing = self.pinned_target
        if existing is not None and existing != requested:
            raise PinnedTargetError(
                "first install is already pinned to a different signed release identity"
            )
        if existing is None:
            atomic_write_json(self._bootstrap_pin_path, requested.to_dict())

    def _admit_target(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        *,
        rollback_authorization: str | None,
    ) -> bool:
        self._validate_static_target(manifest, artifact)
        pointers = self.slots.pointers()
        if pointers.current is None:
            return False
        current = self.slots.marker(pointers.current)
        current_manifest = self.slots.release_manifest(pointers.current)
        verify_manifest_signature(current_manifest, self.verifier)
        current_artifact_id = current.get("artifact_id")
        if not isinstance(current_artifact_id, str):
            raise TargetAdmissionError("current slot has no valid artifact identity")
        current_artifact = current_manifest.artifact(current_artifact_id)
        verify_artifact_signature(current_manifest, current_artifact, self.verifier)
        expected_current = {
            "release_id": current_manifest.release_id,
            "version": current_manifest.version,
            "build_digest": current_manifest.build_digest,
            "artifact_id": current_artifact.artifact_id,
            "artifact_sha256": current_artifact.sha256,
            "channel": current_manifest.channel.value,
        }
        if any(current.get(key) != value for key, value in expected_current.items()):
            raise TargetAdmissionError("current slot identity is not signed by its manifest")
        current_version = current_manifest.version
        comparison = _compare_semver(manifest.version, current_version)
        same_build = current.get("build_digest") == manifest.build_digest
        same_artifact = current.get("artifact_id") == artifact.artifact_id
        if comparison == 0 and same_build and same_artifact:
            raise TargetAdmissionError("the signed target is already the active release")
        needs_authorization = comparison < 0 or (
            comparison == 0 and not (same_build and same_artifact)
        )
        if not needs_authorization:
            return False
        if (
            not rollback_authorization
            or self.rollback_authorizer is None
            or self.rollback_authorizer(current, manifest, rollback_authorization) is not True
        ):
            raise TargetAdmissionError(
                "downgrade or same-version replacement requires explicit rollback authorization"
            )
        return True

    def _validate_static_target(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> None:
        if artifact.platform != self.host_platform or artifact.architecture != self.host_architecture:
            raise TargetAdmissionError(
                f"artifact targets {artifact.platform}/{artifact.architecture}, "
                f"host is {self.host_platform}/{self.host_architecture}"
            )
        if manifest.channel is not self.release_channel:
            raise TargetAdmissionError(
                f"release channel {manifest.channel.value!r} is not allowed by "
                f"the configured {self.release_channel.value!r} channel"
            )

    def _load_and_verify_active_release(
        self,
        active: Mapping[str, Any],
    ) -> ReleaseManifest:
        manifest = self._load_transaction_manifest(active)
        artifact = manifest.artifact(str(active["artifact_id"]))
        if not self._active_matches(active, manifest, artifact):
            raise RecoveryError("active metadata does not match its signed release manifest")
        verify_manifest_signature(manifest, self.verifier)
        verify_artifact_signature(manifest, artifact, self.verifier)
        self._validate_static_target(manifest, artifact)
        self._release_pack_set(manifest)
        return manifest

    def _validate_active_against_journal(self, active: Mapping[str, Any]) -> None:
        transaction_id = str(active["transaction_id"])
        started = next(
            (
                entry
                for entry in self.journal.entries()
                if entry.transaction_id == transaction_id
                and entry.event == "transaction_started"
            ),
            None,
        )
        if started is None:
            raise RecoveryError("active transaction has no durable start record")
        expected = {
            "release_id": active.get("release_id"),
            "version": active.get("version"),
            "build_digest": active.get("build_digest"),
            "artifact_id": active.get("artifact_id"),
            "first_install": active.get("first_install"),
            "rollback_authorized": active.get("rollback_authorized", False),
            "admission_current_slot": active.get("admission_current_slot"),
        }
        if any(started.details.get(key) != value for key, value in expected.items()):
            raise RecoveryError("active metadata does not match the install journal identity")

    def _ensure_download_space(self, path: Path, remaining_bytes: int) -> None:
        required = max(0, remaining_bytes) + self.disk_reserve_bytes
        if self.disk_free_provider(path.parent) < required:
            raise DownloadFailed(
                f"insufficient disk space: need at least {required} free bytes"
            )

    def _ensure_stage_space(self, package_path: Path) -> None:
        unpacked = package_path.stat().st_size
        if zipfile.is_zipfile(package_path):
            with zipfile.ZipFile(package_path) as archive:
                unpacked = sum(member.file_size for member in archive.infolist())
        required = unpacked + package_path.stat().st_size + self.disk_reserve_bytes
        if self.disk_free_provider(self.slots.slots_dir) < required:
            raise StorageError(
                f"insufficient disk space for staging: need at least {required} free bytes"
            )

    def _release_pack_set(self, manifest: ReleaseManifest) -> ReleasePackSet | None:
        return resolve_release_pack_set(
            manifest,
            platform=self.host_platform,
            architecture=self.host_architecture,
            verifier=self.verifier,
        )

    def _prepare_pack_set(
        self,
        active: Mapping[str, Any],
        manifest: ReleaseManifest,
        pack_set: ReleasePackSet | None,
    ) -> PreparedPackSet | None:
        if pack_set is None:
            return None
        transaction_id = str(active["transaction_id"])
        transaction_dir = self.transactions_dir / transaction_id
        prepared = self.pack_downloader.prepare(manifest, pack_set, transaction_dir)
        supplemental_bytes = sum(
            artifact.size_bytes for artifact in pack_set.artifacts
        )
        required = supplemental_bytes + self.disk_reserve_bytes
        if self.disk_free_provider(self.slots.slots_dir) < required:
            raise StorageError(
                "insufficient disk space to project verified Capability Packs"
            )
        return prepared

    def _validate_staged_release(
        self,
        *,
        slot_id: str,
        package_path: Path,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Path:
        path = self.slots.validate(
            slot_id=slot_id,
            package_path=package_path,
            manifest=manifest,
            artifact=artifact,
        )
        validate_installed_pack_set(
            path,
            manifest,
            verifier=self.verifier,
            platform=self.host_platform,
            architecture=self.host_architecture,
            pack_content_verifier=self.pack_content_verifier,
        )
        if self.slot_security_validator is not None:
            security = self.slots.marker(slot_id).get("security_provision")
            if not isinstance(security, Mapping) or self.slot_security_validator(
                path, manifest, artifact, security
            ) is not True:
                raise StorageError("staged slot security provision is invalid")
        return path

    def _validate_retained_release(
        self,
        *,
        slot_id: str,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Path:
        path = self.slots.validate_receipt(
            slot_id=slot_id,
            manifest=manifest,
            artifact=artifact,
        )
        validate_installed_pack_set(
            path,
            manifest,
            verifier=self.verifier,
            platform=self.host_platform,
            architecture=self.host_architecture,
            pack_content_verifier=self.pack_content_verifier,
        )
        if self.slot_security_validator is not None:
            security = self.slots.marker(slot_id).get("security_provision")
            if not isinstance(security, Mapping) or self.slot_security_validator(
                path, manifest, artifact, security
            ) is not True:
                raise StorageError("retained slot security provision is invalid")
        return path

    def _cleanup_slot_security(self, slot_id: str) -> None:
        if self.slot_security_cleanup is None:
            return
        manifest = self.slots.release_manifest(slot_id)
        verify_manifest_signature(manifest, self.verifier)
        marker = self.slots.marker(slot_id)
        artifact_id = marker.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise StorageError("discarded slot has no signed artifact identity")
        artifact = manifest.artifact(artifact_id)
        verify_artifact_signature(manifest, artifact, self.verifier)
        path = self._validate_retained_release(
            slot_id=slot_id,
            manifest=manifest,
            artifact=artifact,
        )
        security = marker.get("security_provision")
        if not isinstance(security, Mapping):
            raise StorageError("discarded slot has no sandbox security provision")
        self.slot_security_cleanup(path, manifest, artifact, security)

    def _discard_slot(self, slot_id: str) -> None:
        path = self.slots.slot_path(slot_id)
        if not os.path.lexists(path):
            return
        self._cleanup_slot_security(slot_id)
        self.slots.discard(slot_id)

    def _prune_slots(self, *, max_slots: int = 3) -> tuple[str, ...]:
        return self.slots.prune(
            max_slots=max_slots,
            before_discard=(
                self._cleanup_slot_security
                if self.slot_security_cleanup is not None
                else None
            ),
        )

    def _rollback_is_safe(self, slot_path: Path, prior: SlotPointers) -> bool:
        if self.rollforward_guard is None:
            return False
        try:
            if self.rollforward_guard(slot_path) is not False:
                return False
            self._validate_rollback_target(prior)
            return True
        except Exception:
            return False

    def _validate_rollback_target(self, prior: SlotPointers) -> None:
        if prior.current is None:
            return
        manifest = self.slots.release_manifest(prior.current)
        verify_manifest_signature(manifest, self.verifier)
        marker = self.slots.marker(prior.current)
        artifact_id = marker.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise StorageError("rollback slot has no artifact identity")
        artifact = manifest.artifact(artifact_id)
        verify_artifact_signature(manifest, artifact, self.verifier)
        self._validate_retained_release(
            slot_id=prior.current,
            manifest=manifest,
            artifact=artifact,
        )

    def _cleanup_transaction(self, active: Mapping[str, Any]) -> None:
        transaction_id = str(active.get("transaction_id", ""))
        if len(transaction_id) != 32 or any(character not in "0123456789abcdef" for character in transaction_id):
            raise RecoveryError("transaction id is unsafe")
        path = self.transactions_dir / transaction_id
        if path.exists():
            shutil.rmtree(path)
            _fsync_parent(path)

    def _cleanup_orphans_locked(self) -> None:
        """Converge crash-left staging and unreferenced UUID transaction trees."""

        if not self.lock.acquired:
            raise RecoveryError("orphan cleanup requires the product install lock")
        self.slots.cleanup_staging_orphans(
            before_remove=self.payload_security_orphan_cleanup,
        )
        active = self._load_active()
        retained = str(active.get("transaction_id")) if active is not None else None
        for path in self.transactions_dir.iterdir():
            if path.name == retained:
                continue
            if (
                len(path.name) != 32
                or any(character not in "0123456789abcdef" for character in path.name)
            ):
                continue
            metadata = path.lstat()
            reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            if stat.S_ISLNK(metadata.st_mode) or reparse or not stat.S_ISDIR(metadata.st_mode):
                raise RecoveryError("orphan transaction path is unsafe")
            try:
                path.resolve(strict=True).relative_to(self.transactions_dir.resolve(strict=True))
            except (OSError, ValueError):
                raise RecoveryError("orphan transaction escaped the install root") from None
            shutil.rmtree(path)
            _fsync_parent(path)

    def _discard_orphan_active(self, active: Mapping[str, Any]) -> None:
        self._cleanup_transaction(active)
        try:
            self._active_path.unlink()
        except FileNotFoundError:
            return
        _fsync_parent(self._active_path)

    def _transition(
        self,
        transaction_id: str,
        state: InstallState,
        event: str,
        details: Mapping[str, Any],
    ) -> JournalEntry:
        return self.journal.append(
            transaction_id=transaction_id,
            state=state,
            event=event,
            details=details,
        )

    def _transition_failed(self, transaction_id: str, error: BaseException) -> None:
        latest = self.journal.latest()
        if (
            latest is None
            or latest.transaction_id != transaction_id
            or latest.state in TERMINAL_STATES
        ):
            return
        self._transition(
            transaction_id,
            InstallState.FAILED,
            "transaction_failed",
            {"error_type": type(error).__name__},
        )
        active = self._load_active()
        if active is not None and active.get("transaction_id") == transaction_id:
            self._cleanup_transaction(active)

    def _prepared(
        self,
        active: Mapping[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> PreparedUpdate:
        return PreparedUpdate(
            transaction_id=str(active["transaction_id"]),
            state=InstallState.AWAITING_USER,
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            artifact_id=artifact.artifact_id,
            slot_id=str(active["slot_id"]),
            package_path=self._package_path(active, artifact),
            slot_path=self.slots.slot_path(str(active["slot_id"])),
        )

    def _activation_result(
        self,
        active: Mapping[str, Any],
        state: InstallState,
        *,
        rolled_back: bool = False,
        error: str | None = None,
    ) -> ActivationResult:
        pointers = self.slots.pointers()
        return ActivationResult(
            transaction_id=str(active["transaction_id"]),
            state=state,
            slot_id=str(active["slot_id"]),
            current_slot=pointers.current,
            previous_slot=pointers.previous,
            rolled_back=rolled_back,
            error=error,
        )

    def _load_active(self) -> dict[str, Any] | None:
        if not self._active_path.exists():
            return None
        try:
            raw = json.loads(self._active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RecoveryError("active transaction metadata is unreadable") from exc
        if not isinstance(raw, dict):
            raise RecoveryError("active transaction metadata must contain an object")
        required = {
            "transaction_id",
            "release_id",
            "version",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "slot_id",
            "first_install",
            "source_index",
        }
        if not required.issubset(raw):
            raise RecoveryError("active transaction metadata is incomplete")
        transaction_id = raw.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
        ):
            raise RecoveryError("active transaction id is unsafe")
        if not isinstance(raw.get("first_install"), bool):
            raise RecoveryError("active first-install flag is invalid")
        source_index = raw.get("source_index")
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
            raise RecoveryError("active source index is invalid")
        raw.setdefault("rollback_authorized", False)
        if not isinstance(raw["rollback_authorized"], bool):
            raise RecoveryError("active rollback authorization flag is invalid")
        raw.setdefault("admission_current_slot", None)
        admission_current_slot = raw["admission_current_slot"]
        if admission_current_slot is not None:
            if not isinstance(admission_current_slot, str):
                raise RecoveryError("active admission slot is invalid")
            try:
                self.slots.slot_path(admission_current_slot)
            except StorageError as exc:
                raise RecoveryError("active admission slot is unsafe") from exc
        try:
            self.slots.slot_path(str(raw.get("slot_id", "")))
        except StorageError as exc:
            raise RecoveryError("active slot id is unsafe") from exc
        return raw

    def _save_active(self, active: Mapping[str, Any]) -> None:
        atomic_write_json(self._active_path, active)

    def _load_transaction_manifest(self, active: Mapping[str, Any]) -> ReleaseManifest:
        transaction_id = str(active["transaction_id"])
        if (
            len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
        ):
            raise RecoveryError("transaction id is unsafe")
        path = self.transactions_dir / transaction_id / "release-manifest.json"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RecoveryError("transaction release manifest is missing") from exc
        return ReleaseManifest.from_json(payload)

    def _active_matches(
        self,
        active: Mapping[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> bool:
        return all(
            (
                active.get("release_id") == manifest.release_id,
                active.get("version") == manifest.version,
                active.get("build_digest") == manifest.build_digest,
                active.get("artifact_id") == artifact.artifact_id,
                active.get("artifact_sha256") == artifact.sha256,
            )
        )

    def _package_path(
        self,
        active: Mapping[str, Any],
        artifact: ReleaseArtifact,
    ) -> Path:
        transaction_id = str(active["transaction_id"])
        if (
            len(transaction_id) != 32
            or any(character not in "0123456789abcdef" for character in transaction_id)
        ):
            raise RecoveryError("transaction id is unsafe")
        path = self.transactions_dir / transaction_id / f"{artifact.file_name}.part"
        if os.name == "nt" and len(str(path.resolve(strict=False))) >= 248:
            raise RecoveryError("artifact path exceeds the safe Windows path limit")
        return path

    def _prior_pointers(self, active: Mapping[str, Any]) -> SlotPointers:
        raw = active.get("prior_pointers")
        if not isinstance(raw, Mapping):
            raise RecoveryError("activation has no durable prior slot pointers")
        return SlotPointers.from_dict(raw)

    @staticmethod
    def _unlink_package(package_path: Path) -> None:
        try:
            package_path.unlink()
        except FileNotFoundError:
            pass


def _slot_id(manifest: ReleaseManifest, artifact: ReleaseArtifact) -> str:
    identity = "\0".join(
        (
            manifest.release_id,
            manifest.version,
            manifest.build_digest,
            artifact.artifact_id,
            artifact.sha256,
            artifact.platform,
            artifact.architecture,
        )
    ).encode("ascii")
    return f"r-{hashlib.sha256(identity).hexdigest()[:40]}"


def _detect_host() -> tuple[str, str]:
    if os.name == "nt":
        platform_name = "windows"
    elif sys.platform == "darwin":
        platform_name = "macos"
    else:
        platform_name = "unsupported"
    machine = host_platform_module.machine().lower()
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine or "unknown")
    return platform_name, architecture


def _compare_semver(left: str, right: str) -> int:
    left_key = _semver_key(left)
    right_key = _semver_key(right)
    if left_key[:3] != right_key[:3]:
        return (left_key[:3] > right_key[:3]) - (left_key[:3] < right_key[:3])
    return _compare_prerelease(left_key[3], right_key[3])


def _semver_key(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    core_and_pre = value.split("+", 1)[0]
    core, separator, prerelease = core_and_pre.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers = tuple(prerelease.split(".")) if separator else None
    return major, minor, patch, identifiers


def _compare_prerelease(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
) -> int:
    if left is None or right is None:
        if left is right:
            return 0
        return 1 if left is None else -1
    for left_item, right_item in zip(left, right):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return (int(left_item) > int(right_item)) - (int(left_item) < int(right_item))
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_item > right_item) - (left_item < right_item)
    return (len(left) > len(right)) - (len(left) < len(right))


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_REGISTRATION_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _registration_identity(registration: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "account_id",
        "organization_id",
        "lease_id",
        "lease_digest",
        "session_generation",
        "lease_revision",
    }
    if not isinstance(registration, Mapping) or set(registration) != expected:
        raise PinnedTargetError("first-install registration authority is invalid")
    normalized: dict[str, Any] = {}
    for key in ("account_id", "organization_id", "lease_id"):
        value = registration.get(key)
        if not isinstance(value, str) or _REGISTRATION_TEXT.fullmatch(value) is None:
            raise PinnedTargetError("first-install registration identity is invalid")
        normalized[key] = value
    lease_digest = registration.get("lease_digest")
    if not isinstance(lease_digest, str) or _SHA256_HEX.fullmatch(lease_digest) is None:
        raise PinnedTargetError("first-install registration lease digest is invalid")
    normalized["lease_digest"] = lease_digest
    for source, target in (
        ("session_generation", "session_generation"),
        ("lease_revision", "lease_revision"),
    ):
        value = registration.get(source)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**63:
            raise PinnedTargetError("first-install registration generation is invalid")
        normalized[target] = value
    return normalized


def _registration_binding(registration: Mapping[str, Any]) -> tuple[Any, ...]:
    normalized = _registration_identity(registration)
    return tuple(
        normalized[key]
        for key in (
            "account_id",
            "organization_id",
            "lease_id",
            "lease_digest",
            "session_generation",
            "lease_revision",
        )
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise PinnedTargetError("first-install authority cannot be encoded") from None


def _registration_receipt_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    return hashlib.sha256(
        b"EcoreX first-install registration authority v1\0"
        + _canonical_json(unsigned)
    ).hexdigest()


def _activation_receipt_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    return hashlib.sha256(
        b"EcoreX activation receipt v1\0" + _canonical_json(unsigned)
    ).hexdigest()
