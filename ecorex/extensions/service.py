"""Backend-authoritative Extension Registry lifecycle and capability fence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import re
import shutil
import sqlite3
import sys
import threading
from typing import Any
import uuid

from ecorex.capabilities import RuntimeAvailability
from ecorex.runtime.database import json_dumps, json_loads
from ecorex.update import RejectingSignatureVerifier, SignatureVerifier

from .errors import (
    ExtensionError,
    ExtensionActionUnavailable,
    ExtensionDependencyError,
    ExtensionIdempotencyConflict,
    ExtensionIntegrityError,
    ExtensionNotFound,
    ExtensionProviderRevoked,
    ExtensionRevisionConflict,
    ExtensionVerificationError,
)
from .local_bundle import (
    SKILL_RUNTIME_FILE,
    LocalSkillBundle,
    LocalSkillBundleStore,
    parse_skill_runtime_manifest,
)
from .models import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionHealth,
    ExtensionKind,
    ExtensionManifest,
    ExtensionRequirement,
    ExtensionSignature,
    ExtensionSource,
    ExtensionStatus,
    ExtensionTransport,
    ExtensionTrust,
    RuntimeBoundary,
    VerifiedExtensionManifest,
    canonical_digest,
    utc_now_iso,
    verify_extension_manifest,
    verify_core_extension,
    verify_legacy_declarative_skill,
    verify_local_bundle_skill,
    version_satisfies,
)
from .repository import ExtensionStateRecord, SQLiteExtensionRepository
from .taxonomy import extension_category, extension_icon_key


_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExtensionActionProjection:
    action_id: str
    enabled: bool
    disabled_reason: str | None
    requires_confirmation: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True, slots=True)
class ExtensionProjection:
    extension_id: str
    display_name: str
    description: str
    kind: str
    category: str
    icon_key: str
    active_revision_id: str | None
    active_version: str | None
    active_digest: str | None
    source: str
    trust: str
    status: str
    health: str
    provenance: Mapping[str, Any]
    readiness: str
    requirements: tuple[str, ...]
    tags: tuple[str, ...]
    dependencies: tuple[ExtensionRequirement, ...]
    exports: tuple[ExtensionExport, ...]
    actions: tuple[ExtensionActionProjection, ...]
    last_error_code: str | None
    revision: int
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "display_name": self.display_name,
            "description": self.description,
            "kind": self.kind,
            "category": self.category,
            "icon_key": self.icon_key,
            "active_revision_id": self.active_revision_id,
            "active_version": self.active_version,
            "active_digest": self.active_digest,
            "source": self.source,
            "trust": self.trust,
            "status": self.status,
            "health": self.health,
            "provenance": dict(self.provenance),
            "readiness": self.readiness,
            "requirements": list(self.requirements),
            "tags": list(self.tags),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "exports": [item.to_dict() for item in self.exports],
            "actions": [item.to_dict() for item in self.actions],
            "last_error_code": self.last_error_code,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ExtensionCatalogSnapshot:
    snapshot_id: str
    contract_version: str
    extension_generation: int
    items: tuple[ExtensionProjection, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "contract_version": self.contract_version,
            "extension_generation": self.extension_generation,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class ExtensionHealthResult:
    health: ExtensionHealth
    error_code: str | None = None
    restart_attempted: bool = False

    def __post_init__(self) -> None:
        if self.health in {ExtensionHealth.UNHEALTHY, ExtensionHealth.CIRCUIT_OPEN}:
            if not self.error_code or not _ERROR_CODE.fullmatch(self.error_code):
                raise ValueError("unhealthy extension result requires a safe error code")
        elif self.error_code is not None and not _ERROR_CODE.fullmatch(self.error_code):
            raise ValueError("extension health error code is unsafe")


HealthProbe = Callable[[ExtensionManifest], Any | Awaitable[Any]]


class ExtensionService:
    """Single package lifecycle authority for Skill/MCP/tool/connector/pack providers.

    State and all mutation events commit in one SQLite transaction. Catalog
    snapshots are immutable derived projections; a mutation response never
    claims that projection derivation itself is part of the state transaction.
    """

    def __init__(
        self,
        repository: SQLiteExtensionRepository,
        *,
        runtime_api_version: str,
        platform: str,
        architecture: str,
        signature_verifier: SignatureVerifier | None = None,
        known_tool_ids: frozenset[str] = frozenset(),
        known_connector_ids: frozenset[str] = frozenset(),
        known_pack_ids: frozenset[str] = frozenset(),
        local_bundle_store: LocalSkillBundleStore | None = None,
        health_probes: Mapping[str, HealthProbe] | None = None,
        restart_budget: int = 3,
        restart_window_seconds: int = 300,
        circuit_open_seconds: int = 300,
        health_probe_timeout_seconds: float = 20.0,
        max_concurrent_health_probes: int = 8,
        credential_vault: Any | None = None,
        skill_runner: Any | None = None,
    ) -> None:
        if not runtime_api_version or not platform or not architecture:
            raise ValueError("extension service requires Runtime/platform identity")
        if not 1 <= restart_budget <= 20:
            raise ValueError("extension restart budget is invalid")
        if not 30 <= restart_window_seconds <= 86_400:
            raise ValueError("extension restart window is invalid")
        if not 30 <= circuit_open_seconds <= 86_400:
            raise ValueError("extension circuit duration is invalid")
        if not 0.05 <= health_probe_timeout_seconds <= 120:
            raise ValueError("extension health probe timeout is invalid")
        if not 1 <= max_concurrent_health_probes <= 32:
            raise ValueError("extension health probe concurrency is invalid")
        self.repository = repository
        self.runtime_api_version = runtime_api_version
        self.platform = platform
        self.architecture = architecture
        self.signature_verifier = signature_verifier or RejectingSignatureVerifier()
        self.known_tool_ids = frozenset(known_tool_ids)
        self.known_connector_ids = frozenset(known_connector_ids)
        self.known_pack_ids = frozenset(known_pack_ids)
        self.local_bundle_store = local_bundle_store
        self.credential_vault = credential_vault
        self.skill_runner = None
        self.health_probes = dict(health_probes or {})
        self.restart_budget = restart_budget
        self.restart_window_seconds = restart_window_seconds
        self.circuit_open_seconds = circuit_open_seconds
        self.health_probe_timeout_seconds = float(health_probe_timeout_seconds)
        self.max_concurrent_health_probes = int(max_concurrent_health_probes)
        self._health_probe_loop: asyncio.AbstractEventLoop | None = None
        self._health_probe_limiter: asyncio.BoundedSemaphore | None = None
        self._runtime_bound_revisions: set[str] = set()
        self._lock = threading.RLock()
        if skill_runner is not None:
            self.bind_skill_runner(skill_runner)

    def bind_credential_vault(self, vault: Any) -> None:
        if vault is None:
            raise ValueError("Extension credential vault is required")
        with self._lock:
            if self.credential_vault is not None and self.credential_vault is not vault:
                raise RuntimeError("Extension credential vault is already bound")
            self.credential_vault = vault

    def bind_skill_runner(self, runner: Any) -> None:
        if not callable(getattr(runner, "supports", None)) or not callable(
            getattr(runner, "run", None)
        ):
            raise TypeError("controlled Skill runner contract is invalid")
        with self._lock:
            if self.skill_runner is not None and self.skill_runner is not runner:
                raise RuntimeError("controlled Skill runner is already bound")
            self.skill_runner = runner

    def bind_health_probe(self, extension_id: str, probe: HealthProbe) -> None:
        if not callable(probe):
            raise TypeError("extension health probe must be callable")
        with self._lock:
            existing = self.health_probes.get(extension_id)
            if existing is not None and existing is not probe:
                raise RuntimeError("extension health probe is already bound")
            self.health_probes[extension_id] = probe

    def assert_revision_runtime_bound(self, revision_id: str) -> None:
        """Prove that executable bytes/config were injected for this process."""

        manifest = self.repository.manifest(revision_id)
        if manifest.runtime_boundary is RuntimeBoundary.DECLARATIVE:
            if manifest.source is ExtensionSource.LEGACY_IMPORT:
                raise ExtensionProviderRevoked("legacy Skill revision cannot execute")
            return
        with self._lock:
            if revision_id not in self._runtime_bound_revisions:
                raise ExtensionProviderRevoked(
                    "executable Extension revision is not bound to this Runtime process"
                )

    def assert_verified_runtime_binding(
        self,
        verified: VerifiedExtensionManifest,
    ) -> ExtensionManifest:
        """Reverify one exact non-serializable Runtime binding and evidence.

        This is stricter than looking up a revision ID: the concrete manifest
        proof supplied by product composition must still verify under current
        trust, match the stored unsigned revision, and have its exact detached
        evidence in the append-only repository.
        """

        if not isinstance(verified, VerifiedExtensionManifest):
            raise ExtensionIntegrityError(
                "executable Runtime binding lacks a verified manifest proof"
            )
        candidate = verified.manifest
        stored = self.repository.manifest(candidate.revision_id)
        if (
            stored.extension_id != candidate.extension_id
            or stored.unsigned_manifest_sha256
            != candidate.unsigned_manifest_sha256
        ):
            raise ExtensionIntegrityError(
                "Runtime binding manifest disagrees with its stored revision"
            )
        try:
            if candidate.source is ExtensionSource.CORE_BUNDLE:
                verify_core_extension(
                    candidate,
                    runtime_api_version=self.runtime_api_version,
                    platform=self.platform,
                    architecture=self.architecture,
                )
            else:
                verify_extension_manifest(
                    candidate,
                    verifier=self.signature_verifier,
                    runtime_api_version=self.runtime_api_version,
                    platform=self.platform,
                    architecture=self.architecture,
                )
        except ExtensionError:
            raise
        signature_json = json_dumps(candidate.signature.to_dict())
        signature_sha256 = hashlib.sha256(
            signature_json.encode("utf-8")
        ).hexdigest()
        if not any(
            record.manifest_sha256 == candidate.manifest_sha256
            and record.signature_key_id == candidate.signature.key_id
            and record.signature_sha256 == signature_sha256
            for record in self.repository.signature_evidence(candidate.revision_id)
        ):
            raise ExtensionIntegrityError(
                "Runtime binding lacks its exact verified signature evidence"
            )
        self.assert_revision_runtime_bound(candidate.revision_id)
        return candidate

    def install_canonical_manifest(
        self,
        payload: bytes,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        manifest = ExtensionManifest.from_bytes(payload)
        verified = verify_extension_manifest(
            manifest,
            verifier=self.signature_verifier,
            runtime_api_version=self.runtime_api_version,
            platform=self.platform,
            architecture=self.architecture,
        )
        return self.install_verified(
            verified,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
        )

    def install_local_skill_zip(
        self,
        payload: bytes,
        *,
        extension_id: str,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        if self.local_bundle_store is None:
            raise ExtensionActionUnavailable("local Skill bundle storage is unavailable")
        bundle = self.local_bundle_store.ingest_zip(payload)
        return self._install_local_bundle(
            bundle,
            extension_id=extension_id,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
        )

    def install_local_skill_directory(
        self,
        directory: str,
        *,
        extension_id: str,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        """Administrator-only composition seam; no directory path crosses the API."""

        if self.local_bundle_store is None:
            raise ExtensionActionUnavailable("local Skill bundle storage is unavailable")
        bundle = self.local_bundle_store.ingest_directory(directory)
        return self._install_local_bundle(
            bundle,
            extension_id=extension_id,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
        )

    def register_migrated_skill_directory(
        self,
        directory: str,
        *,
        extension_id: str,
        builtin: bool,
        initially_enabled: bool,
    ) -> ExtensionProjection:
        """Ingest one legacy directory into CAS and converge it into this authority."""

        if self.local_bundle_store is None:
            raise ExtensionActionUnavailable("local Skill bundle storage is unavailable")
        bundle = self.local_bundle_store.ingest_directory(
            directory, migrated_frontmatter=True
        )
        manifest = self._local_bundle_manifest(
            bundle,
            extension_id=extension_id,
            source=(ExtensionSource.CORE_BUNDLE if builtin else ExtensionSource.LOCAL_BUNDLE),
            trust=(ExtensionTrust.BUILTIN if builtin else ExtensionTrust.LOCAL_UNTRUSTED),
        )
        verified = (
            verify_core_extension(
                manifest,
                runtime_api_version=self.runtime_api_version,
                platform=self.platform,
                architecture=self.architecture,
            )
            if builtin
            else verify_local_bundle_skill(
                manifest,
                artifact_sha256=self.local_bundle_store.verify(
                    bundle.artifact_sha256
                ).artifact_sha256,
                runtime_api_version=self.runtime_api_version,
                platform=self.platform,
                architecture=self.architecture,
            )
        )
        return self.register_runtime_bound(
            verified, initially_enabled=initially_enabled
        )

    def _install_local_bundle(
        self,
        bundle: LocalSkillBundle,
        *,
        extension_id: str,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        manifest = self._local_bundle_manifest(
            bundle,
            extension_id=extension_id,
            source=ExtensionSource.LOCAL_BUNDLE,
            trust=ExtensionTrust.LOCAL_UNTRUSTED,
        )
        verified = verify_local_bundle_skill(
            manifest,
            artifact_sha256=self.local_bundle_store.verify(
                bundle.artifact_sha256
            ).artifact_sha256,
            runtime_api_version=self.runtime_api_version,
            platform=self.platform,
            architecture=self.architecture,
        )
        return self.install_verified(
            verified,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
        )

    def _local_bundle_manifest(
        self,
        bundle: LocalSkillBundle,
        *,
        extension_id: str,
        source: ExtensionSource,
        trust: ExtensionTrust,
    ) -> ExtensionManifest:
        builtin = source is ExtensionSource.CORE_BUNDLE
        return ExtensionManifest(
            schema_version=1,
            contract_version=EXTENSION_CONTRACT_VERSION,
            extension_id=extension_id,
            version=bundle.metadata.version,
            kind=ExtensionKind.SKILL,
            display_name=bundle.metadata.name,
            description=bundle.metadata.description,
            artifact_sha256=bundle.artifact_sha256,
            source=source,
            trust=trust,
            runtime_boundary=RuntimeBoundary.DECLARATIVE,
            transport=ExtensionTransport.NONE,
            compatibility=self._compatibility(
                runtime_api=bundle.metadata.compatibility,
            ),
            dependencies=(),
            conflicts=(),
            exports=(
                ExtensionExport(
                    export_id=extension_id,
                    kind=ExtensionExportKind.SKILL,
                    exposure=ExtensionExposure.DEFERRED,
                    permission_effects=(),
                ),
            ),
            supported_protocol_versions=(),
            upstream_metadata=None,
            signature=ExtensionSignature(
                algorithm=("core-slot-sha256" if builtin else "local-content-sha256"),
                key_id=("builtin-skill-cas-v1" if builtin else "local-cas-v1"),
                value=bundle.artifact_sha256,
            ),
        )

    def install_verified(
        self,
        verified: VerifiedExtensionManifest,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        manifest = verified.manifest
        operation = "extension.install"
        fingerprint = self._fingerprint(
            operation,
            manifest.extension_id,
            expected_revision,
            {"revision_id": manifest.revision_id, "manifest_sha256": manifest.manifest_sha256},
        )
        replay = self._replay(client_request_id, operation, fingerprint)
        if replay:
            return self.projection(manifest.extension_id)
        self._validate_expected_revision(manifest.extension_id, expected_revision)

        with self.repository.database.transaction() as connection:
            replay = self._replay(
                client_request_id, operation, fingerprint, connection=connection
            )
            if replay:
                return self._projection_in_transaction(connection, manifest.extension_id)
            state = self.repository.state(manifest.extension_id, connection=connection)
            current_revision = state.revision if state else 0
            self._assert_expected(expected_revision, current_revision)
            self._insert_revision_and_evidence(connection, manifest)
            self._validate_export_contract(manifest)
            self._validate_candidate_graph(connection, manifest)
            if self.repository.is_quarantined(manifest.revision_id, connection=connection):
                raise ExtensionActionUnavailable(
                    "a quarantined extension revision cannot be installed again"
                )
            now = utc_now_iso()
            changed = state is None or (
                state.staged_revision_id != manifest.revision_id
                and state.active_revision_id != manifest.revision_id
            )
            if state is None:
                connection.execute(
                    "INSERT INTO extension_states(extension_id, active_revision_id, staged_revision_id, "
                    "prior_known_good_revision_id, enabled, health, revision, consecutive_failures, "
                    "restart_attempts, updated_at) VALUES (?, NULL, ?, NULL, 0, 'unknown', 1, 0, 0, ?)",
                    (manifest.extension_id, manifest.revision_id, now),
                )
            elif changed:
                connection.execute(
                    "UPDATE extension_states SET staged_revision_id = ?, revision = revision + 1, "
                    "last_error_code = NULL, updated_at = ? WHERE extension_id = ?",
                    (manifest.revision_id, now, manifest.extension_id),
                )
            projected = self._projection_in_transaction(connection, manifest.extension_id)
            if changed:
                self._append_event(
                    connection,
                    extension_id=manifest.extension_id,
                    revision_id=manifest.revision_id,
                    event_type="extension.staged",
                    payload={
                        "version": manifest.version,
                        "artifact_sha256": manifest.artifact_sha256,
                        "source": manifest.source.value,
                        "trust": manifest.trust.value,
                    },
                    client_request_id=client_request_id,
                    request_sha256=fingerprint,
                )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=fingerprint,
                response={"extension_id": manifest.extension_id, "revision": projected.revision},
            )
            return projected

    def register_runtime_bound(
        self,
        verified: VerifiedExtensionManifest,
        *,
        initially_enabled: bool = True,
    ) -> ExtensionProjection:
        """Bind a provider already loaded from the verified Core/pack/config slot.

        This is not a hot-loader. The product composition calls it only while
        constructing a new Runtime process. Existing disabled state is retained.
        """

        manifest = verified.manifest
        if manifest.source is ExtensionSource.LEGACY_IMPORT:
            raise ExtensionActionUnavailable(
                "legacy_import revisions can never bind to a v1 Runtime"
            )
        request_id = f"runtime-bind:{manifest.extension_id}:{manifest.revision_id[-24:]}"
        negotiated_protocol = (
            "2025-11-25" if manifest.kind is ExtensionKind.MCP_SERVER else None
        )
        catalog_digest = canonical_digest(
            [item.to_dict() for item in manifest.exports]
        )
        operation = "extension.runtime_bind"
        fingerprint = self._fingerprint(
            operation,
            manifest.extension_id,
            self.repository.state(manifest.extension_id).revision
            if self.repository.state(manifest.extension_id) else 0,
            {"revision_id": manifest.revision_id, "initially_enabled": initially_enabled},
        )
        with self._lock:
            self._runtime_bound_revisions.add(manifest.revision_id)
            self.health_probes.setdefault(
                manifest.extension_id,
                lambda _manifest: ExtensionHealthResult(ExtensionHealth.HEALTHY),
            )
        with self.repository.database.transaction() as connection:
            prior_request = self.repository.request(request_id, connection=connection)
            if prior_request is not None:
                if prior_request.operation != operation or prior_request.request_sha256 != fingerprint:
                    # A user state mutation after an earlier bind must not make
                    # a deterministic startup request look like content reuse.
                    return self._projection_in_transaction(connection, manifest.extension_id)
                return self._projection_in_transaction(connection, manifest.extension_id)
            self._insert_revision_and_evidence(connection, manifest)
            self._validate_export_contract(manifest)
            if self.repository.is_quarantined(manifest.revision_id, connection=connection):
                # Quarantine is append-only and cannot be cleared by restart.
                state = self.repository.state(manifest.extension_id, connection=connection)
                if state is None:
                    now = utc_now_iso()
                    connection.execute(
                        "INSERT INTO extension_states(extension_id, active_revision_id, staged_revision_id, "
                        "prior_known_good_revision_id, enabled, health, revision, consecutive_failures, "
                        "restart_attempts, last_error_code, updated_at) "
                        "VALUES (?, NULL, ?, NULL, 0, 'circuit_open', 1, 0, 0, 'extension_quarantined', ?)",
                        (manifest.extension_id, manifest.revision_id, now),
                    )
                return self._projection_in_transaction(connection, manifest.extension_id)
            self._validate_candidate_graph(connection, manifest)
            state = self.repository.state(manifest.extension_id, connection=connection)
            now = utc_now_iso()
            if state is not None and state.active_revision_id == manifest.revision_id and (
                state.negotiated_protocol_version not in {None, negotiated_protocol}
                or state.catalog_digest not in {None, catalog_digest}
            ):
                raise ExtensionIntegrityError(
                    "runtime extension negotiation changed without a new revision"
                )
            if state is None:
                connection.execute(
                    "INSERT INTO extension_states(extension_id, active_revision_id, staged_revision_id, "
                    "prior_known_good_revision_id, enabled, health, revision, consecutive_failures, "
                    "restart_attempts, negotiated_protocol_version, catalog_digest, updated_at) "
                    "VALUES (?, ?, NULL, NULL, ?, 'healthy', 1, 0, 0, ?, ?, ?)",
                    (
                        manifest.extension_id,
                        manifest.revision_id,
                        int(initially_enabled),
                        negotiated_protocol,
                        catalog_digest,
                        now,
                    ),
                )
            elif state.active_revision_id != manifest.revision_id:
                connection.execute(
                    "UPDATE extension_states SET active_revision_id = ?, staged_revision_id = NULL, "
                    "prior_known_good_revision_id = active_revision_id, health = 'healthy', "
                    "revision = revision + 1, consecutive_failures = 0, restart_attempts = 0, "
                    "restart_window_started_at = NULL, circuit_open_until = NULL, last_error_code = NULL, "
                    "negotiated_protocol_version = ?, catalog_digest = ?, updated_at = ? "
                    "WHERE extension_id = ?",
                    (
                        manifest.revision_id,
                        negotiated_protocol,
                        catalog_digest,
                        now,
                        manifest.extension_id,
                    ),
                )
            else:
                # Keep a user's explicit enabled/disabled choice across restart.
                connection.execute(
                    "UPDATE extension_states SET health = 'healthy', last_error_code = NULL, "
                    "negotiated_protocol_version = ?, catalog_digest = ?, updated_at = ? "
                    "WHERE extension_id = ?",
                    (negotiated_protocol, catalog_digest, now, manifest.extension_id),
                )
            projected = self._projection_in_transaction(connection, manifest.extension_id)
            self._append_event(
                connection,
                extension_id=manifest.extension_id,
                revision_id=manifest.revision_id,
                event_type="extension.runtime_bound",
                payload={"enabled": projected.status == ExtensionStatus.ENABLED.value},
                client_request_id=request_id,
                request_sha256=fingerprint,
            )
            self.repository.save_request(
                connection,
                client_request_id=request_id,
                operation=operation,
                request_sha256=fingerprint,
                response={"extension_id": manifest.extension_id, "revision": projected.revision},
            )
            return projected

    async def enable(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        operation = "extension.enable"
        fingerprint = self._fingerprint(operation, extension_id, expected_revision, {})
        replayed, state, manifest = await asyncio.to_thread(
            self._prepare_enable,
            extension_id,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
            operation=operation,
            request_sha256=fingerprint,
        )
        if replayed is not None:
            return replayed
        assert state is not None and manifest is not None
        health = await self._probe(manifest)
        if health.health is not ExtensionHealth.HEALTHY:
            return await asyncio.to_thread(
                self._activation_failed,
                manifest,
                state,
                health,
                expected_revision=expected_revision,
                client_request_id=client_request_id,
                request_sha256=fingerprint,
            )
        return await asyncio.to_thread(
            self._commit_enable,
            extension_id,
            manifest,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
            request_sha256=fingerprint,
        )

    def _prepare_enable(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
        operation: str,
        request_sha256: str,
    ) -> tuple[
        ExtensionProjection | None,
        ExtensionStateRecord | None,
        ExtensionManifest | None,
    ]:
        if self._replay(client_request_id, operation, request_sha256):
            return self.projection(extension_id), None, None
        state = self.repository.require_state(extension_id)
        self._assert_expected(expected_revision, state.revision)
        target_revision = state.staged_revision_id or state.active_revision_id
        if target_revision is None:
            raise ExtensionActionUnavailable("extension has no revision to enable")
        manifest = self.repository.manifest(target_revision)
        reason = self._enable_disabled_reason(state, manifest, target_revision)
        if reason is not None:
            raise ExtensionActionUnavailable(reason)
        self._reverify_revision(manifest)
        self._assert_active_dependencies(manifest)
        return None, state, manifest

    def _commit_enable(
        self,
        extension_id: str,
        manifest: ExtensionManifest,
        *,
        expected_revision: int,
        client_request_id: str,
        request_sha256: str,
    ) -> ExtensionProjection:
        operation = "extension.enable"
        with self.repository.database.transaction() as connection:
            if self._replay(
                client_request_id,
                operation,
                request_sha256,
                connection=connection,
            ):
                return self._projection_in_transaction(connection, extension_id)
            current = self.repository.require_state(extension_id, connection=connection)
            self._assert_expected(expected_revision, current.revision)
            target_revision = current.staged_revision_id or current.active_revision_id
            if target_revision != manifest.revision_id:
                raise ExtensionRevisionConflict(
                    "extension candidate changed during activation",
                    current_revision=current.revision,
                )
            if self.repository.is_quarantined(target_revision, connection=connection):
                raise ExtensionActionUnavailable("quarantined extension cannot be enabled")
            now = utc_now_iso()
            prior = (
                current.active_revision_id
                if current.active_revision_id and current.active_revision_id != target_revision
                else current.prior_known_good_revision_id
            )
            connection.execute(
                "UPDATE extension_states SET active_revision_id = ?, staged_revision_id = NULL, "
                "prior_known_good_revision_id = ?, enabled = 1, health = 'healthy', revision = revision + 1, "
                "consecutive_failures = 0, restart_attempts = 0, restart_window_started_at = NULL, "
                "circuit_open_until = NULL, last_error_code = NULL, updated_at = ? WHERE extension_id = ?",
                (target_revision, prior, now, extension_id),
            )
            projected = self._projection_in_transaction(connection, extension_id)
            self._append_event(
                connection,
                extension_id=extension_id,
                revision_id=target_revision,
                event_type="extension.enabled",
                payload={"prior_known_good_revision_id": prior},
                client_request_id=client_request_id,
                request_sha256=request_sha256,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=request_sha256,
                response={"extension_id": extension_id, "revision": projected.revision},
            )
            return projected

    def disable(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        operation = "extension.disable"
        fingerprint = self._fingerprint(operation, extension_id, expected_revision, {})
        if self._replay(client_request_id, operation, fingerprint):
            return self.projection(extension_id)
        with self.repository.database.transaction() as connection:
            if self._replay(client_request_id, operation, fingerprint, connection=connection):
                return self._projection_in_transaction(connection, extension_id)
            state = self.repository.require_state(extension_id, connection=connection)
            self._assert_expected(expected_revision, state.revision)
            if not state.enabled:
                raise ExtensionActionUnavailable("extension is already disabled")
            if state.active_revision_id is None:
                raise ExtensionActionUnavailable("extension has no active revision")
            active_manifest = self.repository.manifest(
                state.active_revision_id,
                connection=connection,
            )
            if self._user_disable_disabled_reason(active_manifest) is not None:
                raise ExtensionActionUnavailable("extension_required_by_product")
            now = utc_now_iso()
            connection.execute(
                "UPDATE extension_states SET enabled = 0, revision = revision + 1, updated_at = ? "
                "WHERE extension_id = ?",
                (now, extension_id),
            )
            projected = self._projection_in_transaction(connection, extension_id)
            self._append_event(
                connection,
                extension_id=extension_id,
                revision_id=state.active_revision_id,
                event_type="extension.disabled",
                payload={},
                client_request_id=client_request_id,
                request_sha256=fingerprint,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=fingerprint,
                response={"extension_id": extension_id, "revision": projected.revision},
            )
            return projected

    def configure_skill(
        self,
        extension_id: str,
        *,
        values: Mapping[str, str],
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        operation = "extension.configure"
        state = self.repository.require_state(extension_id)
        revision_id = state.active_revision_id or state.staged_revision_id
        if revision_id is None:
            raise ExtensionActionUnavailable("extension has no installed revision")
        manifest = self.repository.manifest(revision_id)
        runtime = self._skill_runtime_manifest(manifest)
        if runtime is None or not runtime.environment:
            raise ExtensionActionUnavailable("configuration_not_required")
        material = {str(key): str(value) for key, value in values.items()}
        if set(material) != set(runtime.environment) or any(not value for value in material.values()):
            raise ExtensionActionUnavailable("skill_configuration_incomplete")
        fingerprint = self._fingerprint(
            operation,
            extension_id,
            expected_revision,
            {"keys": sorted(material)},
        )
        if self._replay(client_request_id, operation, fingerprint):
            return self.projection(extension_id)
        vault = self.credential_vault
        if vault is None:
            raise ExtensionActionUnavailable("credential_vault_unavailable")
        reference = self._skill_credential_reference(extension_id, revision_id)
        try:
            previous = vault.get(reference)
        except (KeyError, RuntimeError):
            previous = None
        try:
            vault.put(reference, material)
            with self.repository.database.transaction() as connection:
                if self._replay(
                    client_request_id, operation, fingerprint, connection=connection
                ):
                    return self._projection_in_transaction(connection, extension_id)
                current = self.repository.require_state(extension_id, connection=connection)
                self._assert_expected(expected_revision, current.revision)
                if (current.active_revision_id or current.staged_revision_id) != revision_id:
                    raise ExtensionRevisionConflict(
                        "Skill revision changed during configuration",
                        current_revision=current.revision,
                    )
                now = utc_now_iso()
                connection.execute(
                    "UPDATE extension_states SET revision=revision+1,updated_at=? WHERE extension_id=?",
                    (now, extension_id),
                )
                projected = self._projection_in_transaction(connection, extension_id)
                self._append_event(
                    connection,
                    extension_id=extension_id,
                    revision_id=revision_id,
                    event_type="extension.configured",
                    payload={"keys": sorted(material)},
                    client_request_id=client_request_id,
                    request_sha256=fingerprint,
                )
                self.repository.save_request(
                    connection,
                    client_request_id=client_request_id,
                    operation=operation,
                    request_sha256=fingerprint,
                    response={"extension_id": extension_id, "revision": projected.revision},
                )
                return projected
        except Exception:
            try:
                if previous is None:
                    vault.delete(reference)
                else:
                    vault.put(reference, previous)
            except Exception:
                pass
            raise

    def uninstall(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        """Uninstall the managed copy while retaining immutable migration facts."""

        operation = "extension.uninstall"
        fingerprint = self._fingerprint(operation, extension_id, expected_revision, {})
        if self._replay(client_request_id, operation, fingerprint):
            return self.projection(extension_id)
        with self.repository.database.transaction() as connection:
            if self._replay(client_request_id, operation, fingerprint, connection=connection):
                return self._projection_in_transaction(connection, extension_id)
            state = self.repository.require_state(extension_id, connection=connection)
            self._assert_expected(expected_revision, state.revision)
            if state.active_revision_id is None and state.staged_revision_id is None:
                raise ExtensionActionUnavailable("extension is already uninstalled")
            revision_id = state.active_revision_id or state.staged_revision_id
            assert revision_id is not None
            manifest = self.repository.manifest(revision_id, connection=connection)
            if self._user_disable_disabled_reason(manifest) is not None:
                raise ExtensionActionUnavailable("extension_required_by_product")
            now = utc_now_iso()
            connection.execute(
                "UPDATE extension_states SET active_revision_id = NULL, staged_revision_id = NULL, "
                "prior_known_good_revision_id = NULL, enabled = 0, health = 'unknown', "
                "revision = revision + 1, last_error_code = NULL, updated_at = ? WHERE extension_id = ?",
                (now, extension_id),
            )
            projected = self._projection_in_transaction(connection, extension_id)
            self._append_event(
                connection,
                extension_id=extension_id,
                revision_id=revision_id,
                event_type="extension.uninstalled",
                payload={"tombstone": True},
                client_request_id=client_request_id,
                request_sha256=fingerprint,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=fingerprint,
                response={"extension_id": extension_id, "revision": projected.revision},
            )
            return projected

    async def check_health(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        operation = "extension.health_check"
        fingerprint = self._fingerprint(operation, extension_id, expected_revision, {})
        replayed, manifest = await asyncio.to_thread(
            self._prepare_health_check,
            extension_id,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
            operation=operation,
            request_sha256=fingerprint,
        )
        if replayed is not None:
            return replayed
        assert manifest is not None
        result = await self._probe(manifest)
        return await asyncio.to_thread(
            self._commit_health_check,
            extension_id,
            manifest,
            result,
            expected_revision=expected_revision,
            client_request_id=client_request_id,
            request_sha256=fingerprint,
        )

    def _prepare_health_check(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
        operation: str,
        request_sha256: str,
    ) -> tuple[ExtensionProjection | None, ExtensionManifest | None]:
        if self._replay(client_request_id, operation, request_sha256):
            return self.projection(extension_id), None
        state = self.repository.require_state(extension_id)
        self._assert_expected(expected_revision, state.revision)
        if not state.enabled or state.active_revision_id is None:
            raise ExtensionActionUnavailable(
                "only an enabled extension can be health-checked"
            )
        manifest = self.repository.manifest(state.active_revision_id)
        if extension_id not in self.health_probes:
            raise ExtensionActionUnavailable("extension health probe is unavailable")
        return None, manifest

    def _commit_health_check(
        self,
        extension_id: str,
        manifest: ExtensionManifest,
        result: ExtensionHealthResult,
        *,
        expected_revision: int,
        client_request_id: str,
        request_sha256: str,
    ) -> ExtensionProjection:
        operation = "extension.health_check"
        with self.repository.database.transaction() as connection:
            if self._replay(
                client_request_id,
                operation,
                request_sha256,
                connection=connection,
            ):
                return self._projection_in_transaction(connection, extension_id)
            current = self.repository.require_state(extension_id, connection=connection)
            self._assert_expected(expected_revision, current.revision)
            if current.active_revision_id != manifest.revision_id:
                raise ExtensionRevisionConflict(
                    "extension revision changed during health check",
                    current_revision=current.revision,
                )
            projected = self._record_health_result(
                connection,
                current,
                manifest,
                result,
                client_request_id=client_request_id,
                request_sha256=request_sha256,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=request_sha256,
                response={"extension_id": extension_id, "revision": projected.revision},
            )
            return projected

    def rollback(
        self,
        extension_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
    ) -> ExtensionProjection:
        operation = "extension.rollback"
        fingerprint = self._fingerprint(operation, extension_id, expected_revision, {})
        if self._replay(client_request_id, operation, fingerprint):
            return self.projection(extension_id)
        with self.repository.database.transaction() as connection:
            if self._replay(client_request_id, operation, fingerprint, connection=connection):
                return self._projection_in_transaction(connection, extension_id)
            state = self.repository.require_state(extension_id, connection=connection)
            self._assert_expected(expected_revision, state.revision)
            target = state.prior_known_good_revision_id
            reason = self._rollback_disabled_reason(state, target, connection=connection)
            if reason is not None:
                raise ExtensionActionUnavailable(reason)
            assert target is not None
            target_manifest = self.repository.manifest(target, connection=connection)
            self._reverify_revision(
                target_manifest,
                connection=connection,
            )
            target_protocol, target_catalog_digest = self._runtime_contract(
                target_manifest
            )
            now = utc_now_iso()
            failed = state.active_revision_id
            connection.execute(
                "UPDATE extension_states SET active_revision_id = ?, staged_revision_id = ?, "
                "prior_known_good_revision_id = NULL, enabled = 1, health = 'healthy', "
                "revision = revision + 1, consecutive_failures = 0, restart_attempts = 0, "
                "restart_window_started_at = NULL, circuit_open_until = NULL, last_error_code = NULL, "
                "negotiated_protocol_version = ?, catalog_digest = ?, updated_at = ? "
                "WHERE extension_id = ?",
                (
                    target,
                    failed,
                    target_protocol,
                    target_catalog_digest,
                    now,
                    extension_id,
                ),
            )
            projected = self._projection_in_transaction(connection, extension_id)
            self._append_event(
                connection,
                extension_id=extension_id,
                revision_id=target,
                event_type="extension.rolled_back",
                payload={"replaced_revision_id": failed},
                client_request_id=client_request_id,
                request_sha256=fingerprint,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=fingerprint,
                response={"extension_id": extension_id, "revision": projected.revision},
            )
            return projected

    def import_legacy_skill_states(
        self, *, skip_names: frozenset[str] = frozenset()
    ) -> int:
        """Preserve legacy metadata as disabled, untrusted declarations only.

        No SKILL.md, script, or resource is scanned or executed. A migrated row
        requires a later restricted bundle validation and a new signed revision;
        therefore actions report ``legacy_revalidation_required``.
        """

        with self.repository.database.reader() as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_states'"
            ).fetchone()
            if table is None:
                return 0
            rows = connection.execute(
                "SELECT skill_id, name, source, metadata_json FROM skill_states ORDER BY skill_id"
            ).fetchall()
        imported = 0
        for row in rows:
            original_name = str(row["name"])
            if original_name.strip().casefold().replace("_", "-") in skip_names:
                continue
            try:
                metadata = json_loads(row["metadata_json"], {})
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            safe_metadata = metadata if isinstance(metadata, Mapping) else {}
            declaration_digest = canonical_digest(
                {
                    "skill_id": str(row["skill_id"]),
                    "name": original_name,
                    "source": str(row["source"]),
                    "metadata": dict(safe_metadata),
                }
            )
            extension_id = "legacy.skill." + hashlib.sha256(
                str(row["skill_id"]).encode("utf-8")
            ).hexdigest()[:24]
            display_name = re.sub(
                r"[\x00-\x1f\x7f]+", " ", original_name
            ).strip()[:128] or extension_id
            manifest = ExtensionManifest(
                schema_version=1,
                contract_version=EXTENSION_CONTRACT_VERSION,
                extension_id=extension_id,
                version="0.0.0",
                kind=ExtensionKind.SKILL,
                display_name=display_name,
                description="从 v0.3.0 保留的 Skill 元数据；内容尚未按 v1 声明式合同复验。",
                artifact_sha256=declaration_digest,
                source=ExtensionSource.LEGACY_IMPORT,
                trust=ExtensionTrust.LOCAL_UNTRUSTED,
                runtime_boundary=RuntimeBoundary.DECLARATIVE,
                transport=ExtensionTransport.NONE,
                compatibility=self._compatibility_any(),
                dependencies=(),
                conflicts=(),
                exports=(
                    ExtensionExport(
                        export_id=extension_id,
                        kind=ExtensionExportKind.SKILL,
                        exposure=ExtensionExposure.HIDDEN,
                        permission_effects=(),
                    ),
                ),
                supported_protocol_versions=(),
                upstream_metadata=None,
                signature=ExtensionSignature(
                    algorithm="migration-record-sha256",
                    key_id="v0.3.0-migration",
                    value=declaration_digest,
                ),
            )
            verified = verify_legacy_declarative_skill(manifest)
            state = self.repository.state(extension_id)
            if state is not None and manifest.revision_id in {
                state.active_revision_id,
                state.staged_revision_id,
            }:
                imported += 1
                continue
            expected = state.revision if state else 0
            request_id = f"legacy-skill:{manifest.revision_id[-24:]}"
            self.install_verified(
                verified,
                expected_revision=expected,
                client_request_id=request_id,
            )
            imported += 1
        return imported

    def projection(self, extension_id: str) -> ExtensionProjection:
        with self.repository.database.reader() as connection:
            return self._projection_in_transaction(connection, extension_id)

    def catalog(self) -> tuple[ExtensionProjection, ...]:
        with self.repository.database.reader() as connection:
            rows = connection.execute(
                "SELECT extension_id FROM extension_states ORDER BY extension_id"
            ).fetchall()
            return tuple(
                self._projection_in_transaction(connection, str(row["extension_id"]))
                for row in rows
            )

    def project_snapshot(self) -> ExtensionCatalogSnapshot:
        """Build a deterministic catalog projection without taking a write lock."""

        with self.repository.database.reader() as connection:
            rows = connection.execute(
                "SELECT extension_id FROM extension_states ORDER BY extension_id"
            ).fetchall()
            items = tuple(
                self._projection_in_transaction(connection, str(row["extension_id"]))
                for row in rows
            )
            generation = self.repository.generation(connection=connection)
        payload = {
            "contract_version": EXTENSION_CONTRACT_VERSION,
            "extension_generation": generation,
            "items": [item.to_dict() for item in items],
        }
        digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
        return ExtensionCatalogSnapshot(
            snapshot_id="ext_" + digest,
            contract_version=EXTENSION_CONTRACT_VERSION,
            extension_generation=generation,
            items=items,
        )

    def snapshot(self) -> ExtensionCatalogSnapshot:
        """Persist a frozen catalog for Turn/Tool execution authority."""

        projected = self.project_snapshot()
        payload = {
            "contract_version": projected.contract_version,
            "extension_generation": projected.extension_generation,
            "items": [item.to_dict() for item in projected.items],
        }
        snapshot_id, _digest = self.repository.save_snapshot(payload)
        if snapshot_id != projected.snapshot_id:
            raise ExtensionIntegrityError("extension snapshot identity is inconsistent")
        return projected

    def apply_availability(
        self,
        base: RuntimeAvailability,
        snapshot: ExtensionCatalogSnapshot,
    ) -> RuntimeAvailability:
        """Derive new-Turn availability only from enabled+healthy revisions."""

        active = list(
            self._active_items(
                snapshot,
                export_kinds=frozenset(
                    {
                        ExtensionExportKind.TOOL,
                        ExtensionExportKind.CONNECTOR,
                        ExtensionExportKind.CAPABILITY_PACK,
                    }
                ),
            )
        )
        packs = {
            exported.export_id
            for item in active
            for exported in item.exports
            if exported.kind is ExtensionExportKind.CAPABILITY_PACK
        }
        connectors = {
            exported.export_id
            for item in active
            for exported in item.exports
            if exported.kind is ExtensionExportKind.CONNECTOR
        }
        tools = {
            exported.export_id
            for item in active
            for exported in item.exports
            if exported.kind is ExtensionExportKind.TOOL
        }
        disabled = dict(base.disabled_tools)
        for tool_id in tools:
            if disabled.get(tool_id) == "extension_provider_inactive":
                disabled.pop(tool_id, None)
        for tool_id in self.known_tool_ids - tools:
            disabled[tool_id] = "extension_provider_inactive"
        return RuntimeAvailability(
            platform=base.platform,
            installed_packs=frozenset(packs),
            connected_connectors=frozenset(base.connected_connectors & connectors),
            disabled_tools=disabled,
            online=base.online,
            selected_model_modalities=base.selected_model_modalities,
            selected_model_capabilities=base.selected_model_capabilities,
        )

    def assert_tool_invocable(self, extension_snapshot_id: str, tool_id: str) -> None:
        """Fence invocation if the exact provider revision has since been revoked."""

        self.assert_export_invocable(
            extension_snapshot_id,
            export_kind=ExtensionExportKind.TOOL,
            export_id=tool_id,
        )

    def owns_tool(self, tool_id: str) -> bool:
        """Return whether this Extension authority owns the exact tool ID."""

        return tool_id in self.known_tool_ids

    def assert_export_invocable(
        self,
        extension_snapshot_id: str,
        *,
        export_kind: ExtensionExportKind,
        export_id: str,
        expected_revision_id: str | None = None,
        expected_state_revision: int | None = None,
    ) -> str:
        """Revalidate one contribution against its frozen and current revision.

        Catalog snapshots deliberately remain immutable after a Turn starts,
        while disable/quarantine/revision replacement must take effect before
        the next side effect.  This method proves both facts and returns the
        exact provider revision which survived the fence.
        """

        payload = self.repository.snapshot_payload(extension_snapshot_id)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ExtensionIntegrityError("extension snapshot items are invalid")
        historical_revisions: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise ExtensionIntegrityError("extension snapshot projection is invalid")
            exports = item.get("exports")
            if (
                item.get("status") == ExtensionStatus.ENABLED.value
                and item.get("health") == ExtensionHealth.HEALTHY.value
                and (
                    expected_state_revision is None
                    or item.get("revision") == expected_state_revision
                )
                and isinstance(exports, list)
                and any(
                    isinstance(exported, Mapping)
                    and exported.get("kind") == export_kind.value
                    and exported.get("export_id") == export_id
                    for exported in exports
                )
            ):
                revision_id = item.get("active_revision_id")
                if isinstance(revision_id, str):
                    historical_revisions.add(revision_id)
        if expected_revision_id is not None:
            historical_revisions &= {expected_revision_id}
        if not historical_revisions:
            raise ExtensionProviderRevoked(
                "export had no exact provider revision in its extension snapshot"
            )
        current = self.snapshot()
        current_revisions = {
            item.active_revision_id
            for item in self._active_items(
                current, export_kinds=frozenset({export_kind})
            )
            if (
                (
                    expected_state_revision is None
                    or item.revision == expected_state_revision
                )
                and any(
                exported.kind is export_kind and exported.export_id == export_id
                for exported in item.exports
                )
            )
        }
        if not historical_revisions & current_revisions:
            raise ExtensionProviderRevoked(
                "the provider revision captured by this Turn is no longer active"
            )
        for revision_id in historical_revisions & current_revisions:
            self._reverify_revision(self.repository.manifest(revision_id))
            self.assert_revision_runtime_bound(revision_id)
            return revision_id
        raise ExtensionProviderRevoked(
            "the provider revision captured by this Turn could not be reverified"
        )

    def enabled_export_ids(
        self, kind: ExtensionExportKind, *, snapshot: ExtensionCatalogSnapshot | None = None
    ) -> frozenset[str]:
        snapshot = snapshot or self.snapshot()
        return frozenset(
            exported.export_id
            for item in self._active_items(
                snapshot, export_kinds=frozenset({kind})
            )
            for exported in item.exports
            if exported.kind is kind
        )

    def _active_items(
        self,
        snapshot: ExtensionCatalogSnapshot,
        *,
        export_kinds: frozenset[ExtensionExportKind] | None = None,
    ) -> tuple[ExtensionProjection, ...]:
        candidates = {
            item.extension_id: item
            for item in snapshot.items
            if item.status == ExtensionStatus.ENABLED.value
            and item.health == ExtensionHealth.HEALTHY.value
            and item.active_revision_id is not None
        }
        if export_kinds is None:
            relevant = set(candidates)
        else:
            relevant = {
                item.extension_id
                for item in candidates.values()
                if any(exported.kind in export_kinds for exported in item.exports)
            }
            pending = list(relevant)
            while pending:
                item = candidates.get(pending.pop())
                if item is None:
                    continue
                for dependency in item.dependencies:
                    if dependency.extension_id not in relevant:
                        relevant.add(dependency.extension_id)
                        pending.append(dependency.extension_id)
        active = {
            extension_id: item
            for extension_id, item in candidates.items()
            if extension_id in relevant
            and item.active_revision_id is not None
            and self._revision_is_available(item.active_revision_id)
        }
        changed = True
        while changed:
            changed = False
            for extension_id, item in tuple(active.items()):
                if any(
                    dependency.extension_id not in active
                    or active[dependency.extension_id].active_version is None
                    or not version_satisfies(
                        active[dependency.extension_id].active_version or "0.0.0",
                        dependency.version_range,
                    )
                    for dependency in item.dependencies
                ):
                    del active[extension_id]
                    changed = True
        return tuple(active[key] for key in sorted(active))

    def _projection_in_transaction(
        self, connection: sqlite3.Connection, extension_id: str
    ) -> ExtensionProjection:
        state = self.repository.require_state(extension_id, connection=connection)
        selected_revision = state.staged_revision_id or state.active_revision_id
        if selected_revision is None:
            row = connection.execute(
                "SELECT revision_id FROM extension_revisions WHERE extension_id = ? "
                "ORDER BY installed_at DESC, revision_id DESC LIMIT 1",
                (extension_id,),
            ).fetchone()
            if row is None:
                raise ExtensionIntegrityError("extension state has no installed revision history")
            selected_revision = str(row["revision_id"])
        manifest = self.repository.manifest(selected_revision, connection=connection)
        active_manifest = (
            self.repository.manifest(state.active_revision_id, connection=connection)
            if state.active_revision_id else None
        )
        staged_quarantined = bool(
            state.staged_revision_id
            and self.repository.is_quarantined(state.staged_revision_id, connection=connection)
        )
        if state.active_revision_id is None and state.staged_revision_id is None:
            status = "uninstalled"
        elif staged_quarantined and active_manifest is None:
            status = ExtensionStatus.QUARANTINED
        elif state.enabled and active_manifest is not None:
            status = ExtensionStatus.ENABLED
        elif active_manifest is not None:
            status = ExtensionStatus.DISABLED
        else:
            status = ExtensionStatus.STAGED
        category = extension_category(
            extension_id=extension_id,
            display_name=manifest.display_name,
            description=manifest.description,
            export_ids=(item.export_id for item in manifest.exports),
            core_bundle=manifest.source is ExtensionSource.CORE_BUNDLE,
        )
        readiness, requirements = self._skill_readiness(manifest)
        return ExtensionProjection(
            extension_id=extension_id,
            display_name=manifest.display_name,
            description=manifest.description,
            kind=manifest.kind.value,
            category=category,
            icon_key=extension_icon_key(extension_id=extension_id, category=category),
            active_revision_id=state.active_revision_id,
            active_version=active_manifest.version if active_manifest else None,
            active_digest=active_manifest.artifact_sha256 if active_manifest else None,
            source=manifest.source.value,
            trust=manifest.trust.value,
            status=status if isinstance(status, str) else status.value,
            health=state.health,
            provenance=self._provenance(manifest),
            readiness=readiness,
            requirements=requirements,
            tags=self._tags(manifest, category),
            dependencies=manifest.dependencies,
            exports=manifest.exports,
            actions=self._actions(
                state, manifest, readiness=readiness, connection=connection
            ),
            last_error_code=state.last_error_code,
            revision=state.revision,
            updated_at=state.updated_at,
        )

    def _actions(
        self,
        state: ExtensionStateRecord,
        manifest: ExtensionManifest,
        *,
        readiness: str,
        connection: sqlite3.Connection,
    ) -> tuple[ExtensionActionProjection, ...]:
        target = state.staged_revision_id or state.active_revision_id
        enable_reason = (
            "extension_already_enabled"
            if state.enabled and state.staged_revision_id is None
            else self._enable_disabled_reason(state, manifest, target)
        )
        disable_reason = (
            self._user_disable_disabled_reason(manifest)
            if state.enabled
            else "extension_already_disabled"
        )
        health_reason = None
        if not state.enabled or state.active_revision_id is None:
            health_reason = "extension_not_enabled"
        elif state.extension_id not in self.health_probes:
            health_reason = "health_probe_unavailable"
        rollback_reason = self._rollback_disabled_reason(
            state, state.prior_known_good_revision_id, connection=connection
        )
        return (
            ExtensionActionProjection("enable", enable_reason is None, enable_reason, True),
            ExtensionActionProjection("disable", disable_reason is None, disable_reason, True),
            ExtensionActionProjection("health_check", health_reason is None, health_reason, False),
            ExtensionActionProjection("rollback", rollback_reason is None, rollback_reason, True),
            ExtensionActionProjection(
                "configure",
                readiness == "needs_configuration",
                None if readiness == "needs_configuration" else "configuration_not_required",
                True,
            ),
            ExtensionActionProjection(
                "uninstall",
                target is not None and self._user_disable_disabled_reason(manifest) is None,
                (
                    None
                    if target is not None and self._user_disable_disabled_reason(manifest) is None
                    else "extension_already_uninstalled"
                    if target is None
                    else "extension_required_by_product"
                ),
                True,
            ),
        )

    @staticmethod
    def _provenance(manifest: ExtensionManifest) -> Mapping[str, Any]:
        upstream = manifest.upstream_metadata
        return {
            "brand": "e-Mate",
            "original_platform": upstream.registry if upstream else None,
            "original_url": None,
        }

    def _tags(self, manifest: ExtensionManifest, category: str) -> tuple[str, ...]:
        if (
            manifest.kind is ExtensionKind.SKILL
            and manifest.source in {ExtensionSource.CORE_BUNDLE, ExtensionSource.LOCAL_BUNDLE}
            and self.local_bundle_store is not None
        ):
            try:
                return self.local_bundle_store.verify(manifest.artifact_sha256).metadata.tags
            except ExtensionIntegrityError:
                # Catalog projection must remain available so the existing
                # invocation fence can report/revoke tampered CAS content.
                pass
        return (category,)

    def _skill_readiness(self, manifest: ExtensionManifest) -> tuple[str, tuple[str, ...]]:
        if (
            manifest.kind is not ExtensionKind.SKILL
            or manifest.source is not ExtensionSource.LOCAL_BUNDLE
            or self.local_bundle_store is None
        ):
            return "ready", ()
        try:
            runtime = self._skill_runtime_manifest(manifest)
        except ExtensionIntegrityError:
            return "unsupported", ("skill_runtime_manifest_invalid",)
        if runtime is None:
            return "ready", ()
        requirements = tuple(
            [f"environment:{name}" for name in runtime.environment]
            + [f"domain:{name}" for name in runtime.network_domains]
            + [f"command:{name}" for name in runtime.external_commands]
        )
        if runtime.runtime == "node" and shutil.which("node") is None:
            return "missing_runtime", ("runtime:node", *requirements)
        if runtime.runtime == "trusted-shell":
            return "unsupported", ("runtime:trusted-shell", *requirements)
        if runtime.network_domains or runtime.external_commands:
            return "unsupported", ("controlled_effect_boundary_unavailable", *requirements)
        if runtime.environment:
            vault = self.credential_vault
            if vault is None:
                return "needs_configuration", requirements
            try:
                configured = vault.get(
                    self._skill_credential_reference(
                        manifest.extension_id, manifest.revision_id
                    )
                )
            except (KeyError, RuntimeError):
                return "needs_configuration", requirements
            if set(configured) != set(runtime.environment) or any(
                not isinstance(value, str) or not value for value in configured.values()
            ):
                return "needs_configuration", requirements
        if not sys.executable:
            return "missing_runtime", ("runtime:python",)
        runner = self.skill_runner
        if runner is None or not runner.supports(runtime.runtime):
            reason = getattr(runner, "unavailable_reason", None)
            return "unsupported", (
                reason
                if isinstance(reason, str) and reason
                else "controlled_runner_unavailable",
            )
        return "ready", requirements

    def _skill_runtime_manifest(self, manifest: ExtensionManifest):
        if self.local_bundle_store is None:
            return None
        bundle = self.local_bundle_store.verify(manifest.artifact_sha256)
        if not any(record.path == SKILL_RUNTIME_FILE for record in bundle.files):
            return None
        files = {record.path: b"" for record in bundle.files}
        files[SKILL_RUNTIME_FILE] = self.local_bundle_store.read_verified_file(
            manifest.artifact_sha256, SKILL_RUNTIME_FILE
        )
        return parse_skill_runtime_manifest(files)

    @staticmethod
    def _skill_credential_reference(extension_id: str, revision_id: str) -> str:
        identity = hashlib.sha256(extension_id.encode("utf-8")).hexdigest()
        return f"ecorex/skills/{identity}/{revision_id}"

    @staticmethod
    def _user_disable_disabled_reason(manifest: ExtensionManifest) -> str | None:
        if manifest.source is not ExtensionSource.CORE_BUNDLE:
            return None
        if manifest.extension_id in {"ecorex.core.tools", "ecorex.core.connectors"}:
            return "extension_required_by_product"
        return None

    def _enable_disabled_reason(
        self,
        state: ExtensionStateRecord,
        manifest: ExtensionManifest,
        target_revision: str | None,
    ) -> str | None:
        if target_revision is None:
            return "revision_not_installed"
        if manifest.source is ExtensionSource.LEGACY_IMPORT:
            return "legacy_revalidation_required"
        if self.repository.is_quarantined(target_revision):
            return "extension_quarantined"
        if manifest.runtime_boundary is not RuntimeBoundary.DECLARATIVE:
            with self._lock:
                if target_revision not in self._runtime_bound_revisions:
                    return "controlled_restart_required"
        if state.health == ExtensionHealth.CIRCUIT_OPEN.value:
            return "extension_circuit_open"
        return None

    def _rollback_disabled_reason(
        self,
        state: ExtensionStateRecord,
        target: str | None,
        *,
        connection: sqlite3.Connection,
    ) -> str | None:
        if target is None:
            return "known_good_revision_unavailable"
        if self.repository.is_quarantined(target, connection=connection):
            return "known_good_revision_quarantined"
        manifest = self.repository.manifest(target, connection=connection)
        if manifest.runtime_boundary is not RuntimeBoundary.DECLARATIVE:
            with self._lock:
                if target not in self._runtime_bound_revisions:
                    return "controlled_restart_required"
        return None

    def _insert_revision_and_evidence(
        self, connection: sqlite3.Connection, manifest: ExtensionManifest
    ) -> None:
        row = connection.execute(
            "SELECT * FROM extension_revisions WHERE revision_id = ?",
            (manifest.revision_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO extension_revisions(revision_id, extension_id, version, artifact_sha256, "
                "manifest_sha256, manifest_json, source, trust, signature_key_id, installed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.revision_id,
                    manifest.extension_id,
                    manifest.version,
                    manifest.artifact_sha256,
                    manifest.manifest_sha256,
                    manifest.to_bytes().decode("utf-8"),
                    manifest.source.value,
                    manifest.trust.value,
                    manifest.signature.key_id,
                    utc_now_iso(),
                ),
            )
        else:
            stored = ExtensionManifest.from_bytes(str(row["manifest_json"]).encode("utf-8"))
            if stored.unsigned_manifest_sha256 != manifest.unsigned_manifest_sha256:
                raise ExtensionIntegrityError("extension revision identity collision")
        signature_json = json_dumps(manifest.signature.to_dict())
        signature_sha256 = hashlib.sha256(signature_json.encode("utf-8")).hexdigest()
        evidence_id = "extevd_" + hashlib.sha256(
            f"{manifest.revision_id}\0{manifest.manifest_sha256}".encode("utf-8")
        ).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO extension_signature_evidence(evidence_id, revision_id, "
            "manifest_sha256, signature_key_id, signature_sha256, evidence_json, verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                manifest.revision_id,
                manifest.manifest_sha256,
                manifest.signature.key_id,
                signature_sha256,
                signature_json,
                utc_now_iso(),
            ),
        )

    def _validate_candidate_graph(
        self, connection: sqlite3.Connection, candidate: ExtensionManifest
    ) -> None:
        rows = connection.execute(
            "SELECT extension_id, active_revision_id, staged_revision_id FROM extension_states"
        ).fetchall()
        selected: dict[str, ExtensionManifest] = {candidate.extension_id: candidate}
        for row in rows:
            extension_id = str(row["extension_id"])
            if extension_id == candidate.extension_id:
                continue
            revision_id = row["staged_revision_id"] or row["active_revision_id"]
            if revision_id:
                selected[extension_id] = self.repository.manifest(
                    str(revision_id), connection=connection
                )
        for extension_id, manifest in selected.items():
            for dependency in manifest.dependencies:
                target = selected.get(dependency.extension_id)
                if target is None or not version_satisfies(target.version, dependency.version_range):
                    raise ExtensionDependencyError(
                        f"extension {extension_id!r} has an unsatisfied dependency"
                    )
            for conflict in manifest.conflicts:
                target = selected.get(conflict.extension_id)
                if target is not None and version_satisfies(target.version, conflict.version_range):
                    raise ExtensionDependencyError(
                        f"extension {extension_id!r} conflicts with {conflict.extension_id!r}"
                    )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(extension_id: str) -> None:
            if extension_id in visiting:
                raise ExtensionDependencyError("extension dependency graph contains a cycle")
            if extension_id in visited:
                return
            visiting.add(extension_id)
            for dependency in selected[extension_id].dependencies:
                if dependency.extension_id in selected:
                    visit(dependency.extension_id)
            visiting.remove(extension_id)
            visited.add(extension_id)

        for extension_id in sorted(selected):
            visit(extension_id)

    def _validate_export_contract(self, manifest: ExtensionManifest) -> None:
        if manifest.kind is ExtensionKind.SKILL:
            expected = (
                (ExtensionExportKind.SKILL, manifest.extension_id),
            )
            actual = tuple((item.kind, item.export_id) for item in manifest.exports)
            if actual != expected or any(item.permission_effects for item in manifest.exports):
                raise ExtensionDependencyError(
                    "declarative Skill export must use its exact extension ID and declare no permissions"
                )
        if manifest.kind is ExtensionKind.MCP_SERVER:
            if manifest.supported_protocol_versions != ("2025-11-25",):
                raise ExtensionDependencyError(
                    "MCP providers must negotiate the stable 2025-11-25 protocol"
                )
            if not any(
                item.kind is ExtensionExportKind.MCP_SERVER
                and item.export_id == manifest.extension_id
                for item in manifest.exports
            ):
                raise ExtensionDependencyError("MCP provider export must use its exact extension ID")
        for exported in manifest.exports:
            allowed: frozenset[str] | None = None
            if exported.kind is ExtensionExportKind.TOOL:
                allowed = self.known_tool_ids
            elif exported.kind is ExtensionExportKind.CONNECTOR:
                allowed = self.known_connector_ids
            elif exported.kind is ExtensionExportKind.CAPABILITY_PACK:
                allowed = self.known_pack_ids
            if allowed is not None and exported.export_id not in allowed:
                raise ExtensionDependencyError(
                    f"extension export uses an unknown exact {exported.kind.value} ID"
                )

    def _assert_active_dependencies(self, manifest: ExtensionManifest) -> None:
        for dependency in manifest.dependencies:
            state = self.repository.state(dependency.extension_id)
            if (
                state is None
                or not state.enabled
                or state.health != ExtensionHealth.HEALTHY.value
                or state.active_revision_id is None
            ):
                raise ExtensionActionUnavailable("extension dependency is not enabled and healthy")
            target = self.repository.manifest(state.active_revision_id)
            if not version_satisfies(target.version, dependency.version_range):
                raise ExtensionActionUnavailable("extension dependency version is no longer satisfied")

    async def _probe(self, manifest: ExtensionManifest) -> ExtensionHealthResult:
        with self._lock:
            probe = self.health_probes.get(manifest.extension_id)
        if probe is None:
            if manifest.runtime_boundary is RuntimeBoundary.DECLARATIVE:
                return ExtensionHealthResult(ExtensionHealth.HEALTHY)
            return ExtensionHealthResult(
                ExtensionHealth.UNHEALTHY, "health_probe_unavailable"
            )
        loop = asyncio.get_running_loop()
        if self._health_probe_loop is not loop or self._health_probe_limiter is None:
            self._health_probe_loop = loop
            self._health_probe_limiter = asyncio.BoundedSemaphore(
                self.max_concurrent_health_probes
            )
        limiter = self._health_probe_limiter

        async def invoke() -> Any:
            async with limiter:
                value = await asyncio.to_thread(probe, manifest)
                if inspect.isawaitable(value):
                    value = await value
                return value

        task = asyncio.create_task(invoke())
        try:
            value = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.health_probe_timeout_seconds,
            )
        except TimeoutError:
            task.add_done_callback(_consume_async_task)
            return ExtensionHealthResult(
                ExtensionHealth.UNHEALTHY,
                "health_probe_timeout",
                restart_attempted=True,
            )
        except asyncio.CancelledError:
            task.add_done_callback(_consume_async_task)
            raise
        except Exception:
            return ExtensionHealthResult(
                ExtensionHealth.UNHEALTHY, "health_probe_failed", restart_attempted=True
            )
        if isinstance(value, ExtensionHealthResult):
            return value
        if value is True:
            return ExtensionHealthResult(ExtensionHealth.HEALTHY)
        if value is False:
            return ExtensionHealthResult(
                ExtensionHealth.UNHEALTHY, "health_probe_rejected"
            )
        if isinstance(value, Mapping):
            try:
                return ExtensionHealthResult(
                    health=ExtensionHealth(str(value.get("health"))),
                    error_code=(str(value["error_code"]) if value.get("error_code") else None),
                    restart_attempted=value.get("restart_attempted") is True,
                )
            except (TypeError, ValueError):
                pass
        return ExtensionHealthResult(ExtensionHealth.UNHEALTHY, "health_probe_contract_invalid")

    def _activation_failed(
        self,
        manifest: ExtensionManifest,
        previous: ExtensionStateRecord,
        result: ExtensionHealthResult,
        *,
        expected_revision: int,
        client_request_id: str,
        request_sha256: str,
    ) -> ExtensionProjection:
        operation = "extension.enable"
        with self.repository.database.transaction() as connection:
            if self._replay(client_request_id, operation, request_sha256, connection=connection):
                return self._projection_in_transaction(connection, manifest.extension_id)
            state = self.repository.require_state(manifest.extension_id, connection=connection)
            self._assert_expected(expected_revision, state.revision)
            target = state.staged_revision_id or state.active_revision_id
            if target != manifest.revision_id:
                raise ExtensionRevisionConflict(
                    "extension candidate changed during activation",
                    current_revision=state.revision,
                )
            now = utc_now_iso()
            self._quarantine_revision(
                connection,
                manifest.revision_id,
                manifest.extension_id,
                result.error_code or "activation_health_failed",
            )
            # The active pointer is never moved to a candidate that failed its
            # pre-activation probe. A known-good provider remains untouched.
            connection.execute(
                "UPDATE extension_states SET staged_revision_id = ?, health = ?, revision = revision + 1, "
                "last_error_code = ?, updated_at = ? WHERE extension_id = ?",
                (
                    manifest.revision_id,
                    state.health if state.active_revision_id else ExtensionHealth.CIRCUIT_OPEN.value,
                    result.error_code or "activation_health_failed",
                    now,
                    manifest.extension_id,
                ),
            )
            projected = self._projection_in_transaction(connection, manifest.extension_id)
            self._append_event(
                connection,
                extension_id=manifest.extension_id,
                revision_id=manifest.revision_id,
                event_type="extension.activation_failed",
                payload={
                    "error_code": result.error_code,
                    "active_revision_unchanged": state.active_revision_id,
                },
                client_request_id=client_request_id,
                request_sha256=request_sha256,
            )
            self.repository.save_request(
                connection,
                client_request_id=client_request_id,
                operation=operation,
                request_sha256=request_sha256,
                response={"extension_id": manifest.extension_id, "revision": projected.revision},
            )
            return projected

    def _record_health_result(
        self,
        connection: sqlite3.Connection,
        state: ExtensionStateRecord,
        manifest: ExtensionManifest,
        result: ExtensionHealthResult,
        *,
        client_request_id: str,
        request_sha256: str,
    ) -> ExtensionProjection:
        now = datetime.now(UTC)
        if result.health is ExtensionHealth.HEALTHY:
            connection.execute(
                "UPDATE extension_states SET health = 'healthy', revision = revision + 1, "
                "consecutive_failures = 0, restart_attempts = 0, restart_window_started_at = NULL, "
                "circuit_open_until = NULL, last_error_code = NULL, updated_at = ? WHERE extension_id = ?",
                (now.isoformat(timespec="microseconds"), state.extension_id),
            )
            event_type = "extension.health_healthy"
        else:
            window_start = _parse_time(state.restart_window_started_at)
            if window_start is None or now - window_start > timedelta(seconds=self.restart_window_seconds):
                window_start = now
                restart_attempts = 0
            else:
                restart_attempts = state.restart_attempts
            if result.restart_attempted:
                restart_attempts += 1
            failures = state.consecutive_failures + 1
            open_circuit = failures >= self.restart_budget or restart_attempts >= self.restart_budget
            if open_circuit:
                self._quarantine_revision(
                    connection,
                    manifest.revision_id,
                    manifest.extension_id,
                    result.error_code or "extension_unhealthy",
                )
                rollback = state.prior_known_good_revision_id
                rollback_ready = bool(
                    rollback
                    and not self.repository.is_quarantined(str(rollback), connection=connection)
                    and self._revision_runtime_ready(str(rollback), connection=connection)
                )
                if rollback_ready:
                    rollback_manifest = self.repository.manifest(
                        str(rollback), connection=connection
                    )
                    rollback_protocol, rollback_catalog_digest = self._runtime_contract(
                        rollback_manifest
                    )
                else:
                    rollback_protocol = state.negotiated_protocol_version
                    rollback_catalog_digest = state.catalog_digest
                connection.execute(
                    "UPDATE extension_states SET active_revision_id = ?, staged_revision_id = ?, "
                    "prior_known_good_revision_id = NULL, enabled = ?, health = ?, revision = revision + 1, "
                    "consecutive_failures = ?, restart_attempts = ?, restart_window_started_at = ?, "
                    "circuit_open_until = ?, last_error_code = ?, negotiated_protocol_version = ?, "
                    "catalog_digest = ?, updated_at = ? WHERE extension_id = ?",
                    (
                        rollback if rollback_ready else state.active_revision_id,
                        manifest.revision_id,
                        int(rollback_ready),
                        ExtensionHealth.HEALTHY.value if rollback_ready else ExtensionHealth.CIRCUIT_OPEN.value,
                        failures,
                        restart_attempts,
                        window_start.isoformat(timespec="microseconds"),
                        (now + timedelta(seconds=self.circuit_open_seconds)).isoformat(timespec="microseconds"),
                        result.error_code or "extension_unhealthy",
                        rollback_protocol,
                        rollback_catalog_digest,
                        now.isoformat(timespec="microseconds"),
                        state.extension_id,
                    ),
                )
                event_type = "extension.circuit_opened"
            else:
                connection.execute(
                    "UPDATE extension_states SET health = ?, revision = revision + 1, "
                    "consecutive_failures = ?, restart_attempts = ?, restart_window_started_at = ?, "
                    "last_error_code = ?, updated_at = ? WHERE extension_id = ?",
                    (
                        result.health.value,
                        failures,
                        restart_attempts,
                        window_start.isoformat(timespec="microseconds"),
                        result.error_code,
                        now.isoformat(timespec="microseconds"),
                        state.extension_id,
                    ),
                )
                event_type = "extension.health_failed"
        projected = self._projection_in_transaction(connection, state.extension_id)
        self._append_event(
            connection,
            extension_id=state.extension_id,
            revision_id=manifest.revision_id,
            event_type=event_type,
            payload={
                "health": result.health.value,
                "error_code": result.error_code,
                "restart_attempted": result.restart_attempted,
            },
            client_request_id=client_request_id,
            request_sha256=request_sha256,
        )
        return projected

    def _revision_runtime_ready(
        self, revision_id: str, *, connection: sqlite3.Connection
    ) -> bool:
        manifest = self.repository.manifest(revision_id, connection=connection)
        if manifest.runtime_boundary is RuntimeBoundary.DECLARATIVE:
            return manifest.source is not ExtensionSource.LEGACY_IMPORT
        with self._lock:
            return revision_id in self._runtime_bound_revisions

    @staticmethod
    def _runtime_contract(manifest: ExtensionManifest) -> tuple[str | None, str]:
        return (
            "2025-11-25" if manifest.kind is ExtensionKind.MCP_SERVER else None,
            canonical_digest([item.to_dict() for item in manifest.exports]),
        )

    def _revision_is_available(self, revision_id: str) -> bool:
        try:
            self._reverify_revision(self.repository.manifest(revision_id))
        except ExtensionError:
            return False
        return True

    def _reverify_revision(
        self,
        manifest: ExtensionManifest,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Re-evaluate persisted provenance using current trust and exact CAS bytes."""

        evidence = self.repository.signature_evidence(
            manifest.revision_id, connection=connection
        )
        if not evidence:
            raise ExtensionIntegrityError("extension revision has no signature evidence")
        failures: list[Exception] = []
        for record in evidence:
            try:
                candidate = ExtensionManifest.from_dict(
                    {**manifest.unsigned_dict(), "signature": dict(record.signature)}
                )
                if (
                    candidate.revision_id != manifest.revision_id
                    or candidate.manifest_sha256 != record.manifest_sha256
                    or candidate.signature.key_id != record.signature_key_id
                ):
                    raise ExtensionIntegrityError(
                        "extension signature evidence is not bound to its revision"
                    )
                if candidate.source in {
                    ExtensionSource.SIGNED_RELEASE,
                    ExtensionSource.CAPABILITY_PACK,
                    ExtensionSource.ADMINISTRATOR,
                }:
                    verify_extension_manifest(
                        candidate,
                        verifier=self.signature_verifier,
                        runtime_api_version=self.runtime_api_version,
                        platform=self.platform,
                        architecture=self.architecture,
                    )
                elif candidate.source is ExtensionSource.CORE_BUNDLE:
                    verify_core_extension(
                        candidate,
                        runtime_api_version=self.runtime_api_version,
                        platform=self.platform,
                        architecture=self.architecture,
                    )
                elif candidate.source is ExtensionSource.LOCAL_BUNDLE:
                    if self.local_bundle_store is None:
                        raise ExtensionVerificationError(
                            "local Skill CAS is unavailable for provenance re-verification"
                        )
                    bundle = self.local_bundle_store.verify(candidate.artifact_sha256)
                    verify_local_bundle_skill(
                        candidate,
                        artifact_sha256=bundle.artifact_sha256,
                        runtime_api_version=self.runtime_api_version,
                        platform=self.platform,
                        architecture=self.architecture,
                    )
                else:
                    verify_legacy_declarative_skill(candidate)
                return
            except ExtensionError as error:
                failures.append(error)
        raise ExtensionVerificationError(
            "no currently trusted evidence verifies the extension revision"
        ) from (failures[-1] if failures else None)

    def _quarantine_revision(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
        extension_id: str,
        reason_code: str,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO extension_quarantines(revision_id, extension_id, reason_code, created_at) "
            "VALUES (?, ?, ?, ?)",
            (revision_id, extension_id, reason_code, utc_now_iso()),
        )

    def _validate_expected_revision(self, extension_id: str, expected_revision: int) -> None:
        self._validate_expected_value(expected_revision)
        state = self.repository.state(extension_id)
        self._assert_expected(expected_revision, state.revision if state else 0)

    @staticmethod
    def _validate_expected_value(expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected extension revision must be a non-negative integer")

    @staticmethod
    def _assert_expected(expected: int, current: int) -> None:
        ExtensionService._validate_expected_value(expected)
        if expected != current:
            raise ExtensionRevisionConflict(
                "extension revision changed; refresh the catalog",
                current_revision=current,
            )

    def _fingerprint(
        self,
        operation: str,
        extension_id: str,
        expected_revision: int,
        extra: Mapping[str, Any],
    ) -> str:
        self._validate_expected_value(expected_revision)
        return canonical_digest(
            {
                "operation": operation,
                "extension_id": extension_id,
                "expected_revision": expected_revision,
                "extra": dict(extra),
            }
        )

    def _replay(
        self,
        client_request_id: str,
        operation: str,
        request_sha256: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        if not isinstance(client_request_id, str) or not _CLIENT_REQUEST_ID.fullmatch(client_request_id):
            raise ValueError("client_request_id is invalid")
        record = self.repository.request(client_request_id, connection=connection)
        if record is None:
            return False
        if record.operation != operation or record.request_sha256 != request_sha256:
            raise ExtensionIdempotencyConflict(
                "client_request_id was reused for different extension content"
            )
        return True

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        extension_id: str,
        revision_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        client_request_id: str,
        request_sha256: str,
    ) -> None:
        self.repository.append_event(
            connection,
            event_id="extev_" + uuid.uuid4().hex,
            extension_id=extension_id,
            revision_id=revision_id,
            event_type=event_type,
            payload=payload,
            client_request_id=client_request_id,
            request_sha256=request_sha256,
        )

    def _compatibility_any(self):
        from .models import ExtensionCompatibility

        return ExtensionCompatibility(runtime_api="*", platforms=(), architectures=())

    def _compatibility(self, *, runtime_api: str):
        from .models import ExtensionCompatibility

        return ExtensionCompatibility(
            runtime_api=runtime_api,
            platforms=(),
            architectures=(),
        )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _consume_async_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


__all__ = [
    "ExtensionActionProjection",
    "ExtensionCatalogSnapshot",
    "ExtensionHealthResult",
    "ExtensionProjection",
    "ExtensionService",
    "HealthProbe",
]
