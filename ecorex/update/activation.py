"""Cross-restart provisional activation and health-receipt authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ecorex.runtime.database import SCHEMA_VERSION as STORAGE_SCHEMA_VERSION

from .journal import InstallJournal, InstallState
from .manifest import ReleaseArtifact, ReleaseManifest
from .pack_install import PackContentVerifier, validate_installed_pack_set
from .storage import SlotPointers, SlotStore, StorageError, atomic_write_json
from .verification import (
    SignatureVerifier,
    verify_artifact_signature,
    verify_manifest_signature,
)


ACTIVATION_HEALTH_PATH = "/api/v1/activation-health"
ACTIVATION_TRANSACTION_ENV = "ECOREX_ACTIVATION_TRANSACTION_ID"
ACTIVATION_NONCE_ENV = "ECOREX_ACTIVATION_HEALTH_NONCE"
ACTIVATION_NONCE_HEADER = "x-ecorex-activation-nonce"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,239}$")
_SAFE_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_WEB_MANIFEST_BYTES = 4 * 1024 * 1024


class ActivationIntentError(RuntimeError):
    """A provisional activation is missing, stale, or tampered."""


@dataclass(frozen=True, slots=True)
class ActivationLaunchContext:
    transaction_id: str
    nonce: str = field(repr=False, compare=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "ActivationLaunchContext | None":
        transaction_id = environment.get(ACTIVATION_TRANSACTION_ENV)
        nonce = environment.get(ACTIVATION_NONCE_ENV)
        if transaction_id is None and nonce is None:
            return None
        if (
            not isinstance(transaction_id, str)
            or _TRANSACTION.fullmatch(transaction_id) is None
            or not isinstance(nonce, str)
            or _SAFE_NONCE.fullmatch(nonce) is None
        ):
            raise ActivationIntentError("activation launch environment is invalid")
        return cls(transaction_id=transaction_id, nonce=nonce)


@dataclass(frozen=True, slots=True)
class ActivationHealthIdentity:
    schema_version: int
    transaction_id: str
    slot_id: str
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str
    payload_digest: str
    runtime_config_sha256: str
    web_bundle_sha256: str
    storage_schema_version: int
    storage_identity: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.storage_schema_version != STORAGE_SCHEMA_VERSION:
            raise ActivationIntentError("activation health schema is unsupported")
        if _TRANSACTION.fullmatch(self.transaction_id) is None:
            raise ActivationIntentError("activation health transaction is invalid")
        for value in (self.slot_id, self.release_id, self.version, self.artifact_id):
            if not isinstance(value, str) or not value or len(value) > 240:
                raise ActivationIntentError("activation health identity is invalid")
        for digest in (
            self.build_digest,
            self.artifact_sha256,
            self.payload_digest,
            self.runtime_config_sha256,
            self.web_bundle_sha256,
            self.storage_identity,
        ):
            if not isinstance(digest, str) or _HEX_64.fullmatch(digest) is None:
                raise ActivationIntentError("activation health digest is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "slot_id": self.slot_id,
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "payload_digest": self.payload_digest,
            "runtime_config_sha256": self.runtime_config_sha256,
            "web_bundle_sha256": self.web_bundle_sha256,
            "storage_schema_version": self.storage_schema_version,
            "storage_identity": self.storage_identity,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ActivationHealthIdentity":
        expected = {
            "schema_version",
            "transaction_id",
            "slot_id",
            "release_id",
            "version",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "payload_digest",
            "runtime_config_sha256",
            "web_bundle_sha256",
            "storage_schema_version",
            "storage_identity",
        }
        if set(raw) != expected:
            raise ActivationIntentError("activation health identity fields are invalid")
        try:
            return cls(**dict(raw))
        except (TypeError, ValueError) as error:
            raise ActivationIntentError("activation health identity is malformed") from error

    def proof(self, nonce: str) -> str:
        if _SAFE_NONCE.fullmatch(nonce) is None:
            raise ActivationIntentError("activation health nonce is invalid")
        return hmac.new(
            nonce.encode("ascii"), _canonical(self.to_dict()), hashlib.sha256
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProvisionalActivationIntent:
    schema_version: int
    transaction_id: str
    slot_id: str
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str
    prior_pointers: SlotPointers
    health_identity: ActivationHealthIdentity
    intent_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or _TRANSACTION.fullmatch(self.transaction_id) is None:
            raise ActivationIntentError("activation intent identity is invalid")
        if _SAFE_ID.fullmatch(self.slot_id) is None:
            raise ActivationIntentError("activation intent slot is invalid")
        if self.health_identity.transaction_id != self.transaction_id:
            raise ActivationIntentError("activation health transaction does not match intent")
        if self.health_identity.slot_id != self.slot_id:
            raise ActivationIntentError("activation health slot does not match intent")
        for digest in (self.build_digest, self.artifact_sha256, self.intent_digest):
            if _HEX_64.fullmatch(digest) is None:
                raise ActivationIntentError("activation intent digest is invalid")
        expected = self.compute_digest(self.unsigned_dict())
        if not hmac.compare_digest(expected, self.intent_digest):
            raise ActivationIntentError("activation intent checksum is invalid")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "slot_id": self.slot_id,
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "prior_pointers": self.prior_pointers.to_dict(),
            "health_identity": self.health_identity.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "intent_digest": self.intent_digest}

    @staticmethod
    def compute_digest(value: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            b"EcoreX provisional activation intent v1\0" + _canonical(value)
        ).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        transaction_id: str,
        slot_id: str,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        prior_pointers: SlotPointers,
        health_identity: ActivationHealthIdentity,
    ) -> "ProvisionalActivationIntent":
        unsigned = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "slot_id": slot_id,
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "prior_pointers": prior_pointers.to_dict(),
            "health_identity": health_identity.to_dict(),
        }
        return cls(
            schema_version=1,
            transaction_id=transaction_id,
            slot_id=slot_id,
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            prior_pointers=prior_pointers,
            health_identity=health_identity,
            intent_digest=cls.compute_digest(unsigned),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProvisionalActivationIntent":
        expected = {
            "schema_version",
            "transaction_id",
            "slot_id",
            "release_id",
            "version",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "prior_pointers",
            "health_identity",
            "intent_digest",
        }
        if set(raw) != expected:
            raise ActivationIntentError("activation intent fields are invalid")
        prior_raw = raw.get("prior_pointers")
        health_raw = raw.get("health_identity")
        if not isinstance(prior_raw, Mapping) or not isinstance(health_raw, Mapping):
            raise ActivationIntentError("activation intent projections are invalid")
        try:
            return cls(
                schema_version=raw["schema_version"],
                transaction_id=raw["transaction_id"],
                slot_id=raw["slot_id"],
                release_id=raw["release_id"],
                version=raw["version"],
                build_digest=raw["build_digest"],
                artifact_id=raw["artifact_id"],
                artifact_sha256=raw["artifact_sha256"],
                prior_pointers=SlotPointers.from_dict(prior_raw),
                health_identity=ActivationHealthIdentity.from_dict(health_raw),
                intent_digest=raw["intent_digest"],
            )
        except (TypeError, ValueError, StorageError) as error:
            raise ActivationIntentError("activation intent is malformed") from error


@dataclass(frozen=True, slots=True)
class VerifiedProvisionalActivation:
    intent: ProvisionalActivationIntent
    slot_path: Path
    payload_root: Path
    manifest: ReleaseManifest
    artifact: ReleaseArtifact


class ProvisionalActivationController:
    """Verifies and converges one Bootstrap-owned candidate activation."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        verifier: SignatureVerifier,
        host_platform: str,
        host_architecture: str,
        pack_content_verifier: PackContentVerifier | None = None,
        create_storage: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.slots = SlotStore(self.root, create_storage=create_storage)
        self.journal = InstallJournal(self.root / "install-journal.ndjson")
        self.verifier = verifier
        self.host_platform = host_platform
        self.host_architecture = host_architecture
        self.pack_content_verifier = pack_content_verifier
        self.intent_path = self.root / "activation-intent.json"
        self.receipt_path = self.root / "activation-receipt.json"
        self.active_path = self.root / "active-transaction.json"
        self.transactions_dir = self.root / "transactions"

    def converge_startup(self) -> None:
        self.slots.converge_startup()

    def create_intent(
        self,
        *,
        active: Mapping[str, Any],
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        prior_pointers: SlotPointers,
    ) -> ProvisionalActivationIntent:
        transaction_id = str(active.get("transaction_id") or "")
        slot_id = str(active.get("slot_id") or "")
        self._verify_signed_slot(slot_id, manifest, artifact)
        health_identity = health_identity_for_slot(
            transaction_id=transaction_id,
            slot_id=slot_id,
            slot_path=self.slots.slot_path(slot_id),
            manifest=manifest,
            artifact=artifact,
        )
        intent = ProvisionalActivationIntent.create(
            transaction_id=transaction_id,
            slot_id=slot_id,
            manifest=manifest,
            artifact=artifact,
            prior_pointers=prior_pointers,
            health_identity=health_identity,
        )
        existing = self.load_intent(required=False)
        if existing is not None and existing != intent:
            raise ActivationIntentError("another provisional activation intent exists")
        if existing is None:
            atomic_write_json(self.intent_path, intent.to_dict())
        return intent

    def load_intent(self, *, required: bool = True) -> ProvisionalActivationIntent | None:
        raw = _read_json_object(self.intent_path, required=required)
        if raw is None:
            return None
        return ProvisionalActivationIntent.from_dict(raw)

    def ensure_pending_current(
        self, transaction_id: str | None = None
    ) -> VerifiedProvisionalActivation | None:
        intent = self.load_intent(required=False)
        if intent is None:
            return None
        if transaction_id is not None and intent.transaction_id != transaction_id:
            raise ActivationIntentError("activation transaction does not match intent")
        active = _read_json_object(self.active_path, required=True)
        assert active is not None
        self._verify_active(intent, active)
        latest = self.journal.latest()
        if (
            latest is None
            or latest.transaction_id != intent.transaction_id
            or latest.state not in {InstallState.ACTIVATING, InstallState.HEALTHCHECKING}
        ):
            raise ActivationIntentError("activation journal does not authorize a candidate")
        started = next(
            (
                entry
                for entry in self.journal.entries()
                if entry.transaction_id == intent.transaction_id
                and entry.event == "transaction_started"
            ),
            None,
        )
        if started is None or any(
            started.details.get(key) != value
            for key, value in {
                "release_id": intent.release_id,
                "version": intent.version,
                "build_digest": intent.build_digest,
                "artifact_id": intent.artifact_id,
            }.items()
        ):
            raise ActivationIntentError("activation journal identity does not match intent")
        manifest = self.slots.release_manifest(intent.slot_id)
        artifact = manifest.artifact(intent.artifact_id)
        slot_path = self._verify_signed_slot(intent.slot_id, manifest, artifact)
        observed_health = health_identity_for_slot(
            transaction_id=intent.transaction_id,
            slot_id=intent.slot_id,
            slot_path=slot_path,
            manifest=manifest,
            artifact=artifact,
        )
        if observed_health != intent.health_identity:
            raise ActivationIntentError("candidate health identity changed after intent")
        pointers = self.slots.pointers()
        if latest.state is InstallState.ACTIVATING:
            if pointers == intent.prior_pointers:
                self.slots.switch_to(intent.slot_id)
            elif pointers.current != intent.slot_id:
                raise ActivationIntentError("slot pointers do not match activation intent")
            latest = self.journal.append(
                transaction_id=intent.transaction_id,
                state=InstallState.HEALTHCHECKING,
                event="slot_activated_provisionally",
                details={
                    "slot_id": intent.slot_id,
                    "intent_digest": intent.intent_digest,
                },
            )
        pointers = self.slots.pointers()
        if (
            latest.state is not InstallState.HEALTHCHECKING
            or pointers.current != intent.slot_id
            or intent.slot_id in pointers.known_good
        ):
            raise ActivationIntentError("candidate is not an isolated provisional slot")
        return VerifiedProvisionalActivation(
            intent=intent,
            slot_path=slot_path,
            payload_root=slot_path / "payload",
            manifest=manifest,
            artifact=artifact,
        )

    def reconcile_confirmation(self) -> bool:
        intent = self.load_intent(required=False)
        if intent is None:
            self._reconcile_confirmed_pre_data_rollback()
            return False
        latest = self.journal.latest()
        pointers = self.slots.pointers()
        if (
            latest is not None
            and latest.transaction_id == intent.transaction_id
            and latest.state in {InstallState.ROLLBACK, InstallState.FAILED}
            and pointers == intent.prior_pointers
        ):
            self._cleanup(intent)
            try:
                self.slots.discard(intent.slot_id)
            except StorageError:
                pass
            return False
        if (
            latest is not None
            and latest.transaction_id == intent.transaction_id
            and latest.state is InstallState.FAILED
            and pointers.current == intent.slot_id
            and intent.slot_id in pointers.known_good
        ):
            self._cleanup(intent)
            return False
        if (
            pointers.current != intent.slot_id
            or intent.slot_id not in pointers.known_good
        ):
            return False
        if latest is None or latest.transaction_id != intent.transaction_id:
            raise ActivationIntentError("known-good candidate has no activation journal")
        if latest.state is InstallState.HEALTHCHECKING:
            self.journal.append(
                transaction_id=intent.transaction_id,
                state=InstallState.COMPLETED,
                event="activation_confirmation_recovered",
                details={"slot_id": intent.slot_id},
            )
        elif latest.state is not InstallState.COMPLETED:
            raise ActivationIntentError("known-good candidate has an invalid terminal state")
        self._write_receipt(intent, state="confirmed", data_barrier_crossed=False)
        self._cleanup(intent)
        return True

    def confirm(
        self,
        transaction_id: str,
        health_identity: ActivationHealthIdentity,
    ) -> ProvisionalActivationIntent:
        verified = self.ensure_pending_current(transaction_id)
        if verified is None:
            raise ActivationIntentError("there is no provisional activation to confirm")
        intent = verified.intent
        if health_identity != intent.health_identity:
            raise ActivationIntentError("health response does not match activation intent")
        self._write_receipt(intent, state="confirming", data_barrier_crossed=False)
        self.slots.mark_known_good(intent.slot_id, keep=3)
        latest = self.journal.latest()
        if latest is None or latest.state is not InstallState.HEALTHCHECKING:
            raise ActivationIntentError("activation changed before confirmation")
        self.journal.append(
            transaction_id=intent.transaction_id,
            state=InstallState.COMPLETED,
            event="bootstrap_health_confirmed",
            details={
                "slot_id": intent.slot_id,
                "health_identity": intent.health_identity.storage_identity,
            },
        )
        self._write_receipt(intent, state="confirmed", data_barrier_crossed=False)
        # Keep every signed prior slot until the full Runtime crosses the
        # durable data barrier.  Pruning here would make the only safe
        # post-probe rollback window impossible to honor.
        self._cleanup(intent)
        return intent

    def fail_pre_data(
        self, transaction_id: str, *, error_code: str
    ) -> InstallState:
        intent = self.load_intent(required=False)
        if intent is None or intent.transaction_id != transaction_id:
            raise ActivationIntentError("there is no provisional activation to fail")
        active = _read_json_object(self.active_path, required=True)
        assert active is not None
        self._verify_active(intent, active)
        latest = self.journal.latest()
        if (
            latest is None
            or latest.transaction_id != intent.transaction_id
            or latest.state not in {InstallState.ACTIVATING, InstallState.HEALTHCHECKING}
        ):
            raise ActivationIntentError("activation journal cannot accept a health failure")
        receipt = _read_json_object(self.receipt_path, required=False)
        data_barrier = bool(
            receipt
            and receipt.get("transaction_id") == intent.transaction_id
            and receipt.get("slot_id") == intent.slot_id
            and receipt.get("data_barrier_crossed") is True
        )
        pointers = self.slots.pointers()
        if pointers.current not in {intent.slot_id, intent.prior_pointers.current}:
            raise ActivationIntentError("slot pointers diverged from the activation intent")
        candidate_is_known_good = intent.slot_id in pointers.known_good
        if data_barrier or candidate_is_known_good:
            self.journal.append(
                transaction_id=intent.transaction_id,
                state=InstallState.FAILED,
                event="activation_failed_rollforward_required",
                details={"slot_id": intent.slot_id, "error_type": "RollForwardRequired"},
            )
            self._cleanup(intent)
            return InstallState.FAILED
        self._validate_prior(intent.prior_pointers)
        self.slots.restore(intent.prior_pointers)
        self.journal.append(
            transaction_id=intent.transaction_id,
            state=InstallState.ROLLBACK,
            event="bootstrap_health_failed_rolled_back",
            details={"slot_id": intent.slot_id, "error_type": _safe_error(error_code)},
        )
        self._cleanup(intent)
        try:
            self.slots.discard(intent.slot_id)
        except StorageError:
            pass
        return InstallState.ROLLBACK

    def _validate_prior(self, prior: SlotPointers) -> None:
        for slot_id in dict.fromkeys(
            item for item in (prior.current, *prior.known_good) if item is not None
        ):
            manifest = self.slots.release_manifest(slot_id)
            verify_manifest_signature(manifest, self.verifier)
            marker = self.slots.marker(slot_id)
            artifact_id = marker.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise ActivationIntentError("prior slot artifact identity is invalid")
            artifact = manifest.artifact(artifact_id)
            verify_artifact_signature(manifest, artifact, self.verifier)
            self.slots.validate_receipt(
                slot_id=slot_id,
                manifest=manifest,
                artifact=artifact,
            )
            validate_installed_pack_set(
                self.slots.slot_path(slot_id),
                manifest,
                verifier=self.verifier,
                platform=self.host_platform,
                architecture=self.host_architecture,
                pack_content_verifier=self.pack_content_verifier,
            )

    def mark_data_barrier_crossed(self, slot_id: str) -> bool:
        receipt = _read_json_object(self.receipt_path, required=False)
        if receipt is None:
            return False
        if receipt.get("state") == "rolled_back_pre_data":
            prior_raw = receipt.get("prior_pointers")
            if not isinstance(prior_raw, Mapping):
                raise ActivationIntentError("activation rollback receipt is invalid")
            prior = SlotPointers.from_dict(prior_raw)
            if self.slots.pointers() != prior or prior.current != slot_id:
                raise ActivationIntentError("rolled-back activation pointers changed")
            # The restored Runtime crossed its barrier in an earlier release;
            # this receipt belongs only to the discarded candidate.
            return False
        if (
            receipt.get("state") != "confirmed"
            or receipt.get("slot_id") != slot_id
            or receipt.get("data_barrier_crossed") not in {False, True}
        ):
            raise ActivationIntentError("activation confirmation receipt is invalid")
        pointers = self.slots.pointers()
        if pointers.current != slot_id or slot_id not in pointers.known_good:
            raise ActivationIntentError("data barrier requires the confirmed current slot")
        health_raw = receipt.get("health_identity")
        if not isinstance(health_raw, Mapping):
            raise ActivationIntentError("activation receipt has no health identity")
        health = ActivationHealthIdentity.from_dict(health_raw)
        manifest = self.slots.release_manifest(slot_id)
        artifact = manifest.artifact(health.artifact_id)
        slot_path = self._verify_signed_slot(slot_id, manifest, artifact)
        observed = health_identity_for_slot(
            transaction_id=health.transaction_id,
            slot_id=slot_id,
            slot_path=slot_path,
            manifest=manifest,
            artifact=artifact,
        )
        if observed != health or any(
            receipt.get(key) != value
            for key, value in {
                "transaction_id": health.transaction_id,
                "release_id": health.release_id,
                "version": health.version,
                "build_digest": health.build_digest,
                "artifact_id": health.artifact_id,
                "artifact_sha256": health.artifact_sha256,
            }.items()
        ):
            raise ActivationIntentError("activation receipt does not match the signed current slot")
        if receipt["data_barrier_crossed"] is True:
            return False
        updated = dict(receipt)
        updated["data_barrier_crossed"] = True
        updated["receipt_digest"] = _receipt_digest(updated)
        atomic_write_json(self.receipt_path, updated)
        try:
            self.slots.prune(max_slots=3)
        except (OSError, StorageError):
            # Retention cleanup is recoverable and must not turn a successful
            # data handoff into a startup failure that can no longer roll back.
            pass
        return True

    def rollback_confirmed_pre_data(
        self,
        slot_id: str,
        *,
        error_code: str,
    ) -> tuple[bool, str | None]:
        """Restore signed prior pointers only before live storage can open."""

        receipt = _read_json_object(self.receipt_path, required=False)
        if (
            receipt is None
            or receipt.get("state") != "confirmed"
            or receipt.get("slot_id") != slot_id
            or receipt.get("data_barrier_crossed") is not False
        ):
            return False, None
        prior_raw = receipt.get("prior_pointers")
        health_raw = receipt.get("health_identity")
        if not isinstance(prior_raw, Mapping) or not isinstance(health_raw, Mapping):
            raise ActivationIntentError("activation receipt cannot authorize rollback")
        prior = SlotPointers.from_dict(prior_raw)
        health = ActivationHealthIdentity.from_dict(health_raw)
        if health.slot_id != slot_id or health.transaction_id != receipt.get("transaction_id"):
            raise ActivationIntentError("activation receipt identity is inconsistent")
        pointers = self.slots.pointers()
        if pointers.current != slot_id or slot_id not in pointers.known_good:
            raise ActivationIntentError("confirmed startup pointers changed before rollback")
        manifest = self.slots.release_manifest(slot_id)
        artifact = manifest.artifact(health.artifact_id)
        slot_path = self._verify_signed_slot(slot_id, manifest, artifact)
        if health_identity_for_slot(
            transaction_id=health.transaction_id,
            slot_id=slot_id,
            slot_path=slot_path,
            manifest=manifest,
            artifact=artifact,
        ) != health:
            raise ActivationIntentError("confirmed startup slot no longer matches receipt")
        self._validate_prior(prior)
        latest = self.journal.latest()
        if (
            latest is None
            or latest.transaction_id != health.transaction_id
            or latest.state is not InstallState.COMPLETED
        ):
            raise ActivationIntentError("confirmed startup journal cannot roll back")
        pending = dict(receipt)
        pending["state"] = "rollback_pending"
        pending["error_code"] = _safe_error(error_code)
        pending["receipt_digest"] = _receipt_digest(pending)
        atomic_write_json(self.receipt_path, pending)
        return True, self._finish_confirmed_pre_data_rollback(pending, health, prior)

    def _reconcile_confirmed_pre_data_rollback(self) -> bool:
        receipt = _read_json_object(self.receipt_path, required=False)
        if receipt is None or receipt.get("state") not in {
            "rollback_pending",
            "rolled_back_pre_data",
        }:
            return False
        prior_raw = receipt.get("prior_pointers")
        health_raw = receipt.get("health_identity")
        if not isinstance(prior_raw, Mapping) or not isinstance(health_raw, Mapping):
            raise ActivationIntentError("activation rollback receipt is invalid")
        prior = SlotPointers.from_dict(prior_raw)
        health = ActivationHealthIdentity.from_dict(health_raw)
        self._finish_confirmed_pre_data_rollback(receipt, health, prior)
        return True

    def _finish_confirmed_pre_data_rollback(
        self,
        receipt: Mapping[str, Any],
        health: ActivationHealthIdentity,
        prior: SlotPointers,
    ) -> str | None:
        """Idempotently finish a receipt-authorized pre-data rollback."""

        if (
            receipt.get("transaction_id") != health.transaction_id
            or receipt.get("slot_id") != health.slot_id
            or receipt.get("data_barrier_crossed") is not False
        ):
            raise ActivationIntentError("activation rollback identity is inconsistent")
        self._validate_prior(prior)
        pointers = self.slots.pointers()
        latest = self.journal.latest()
        if latest is None or latest.transaction_id != health.transaction_id:
            raise ActivationIntentError("activation rollback journal is missing")
        if pointers.current == health.slot_id:
            if latest.state is not InstallState.COMPLETED:
                raise ActivationIntentError("activation rollback journal cannot restore prior")
            manifest = self.slots.release_manifest(health.slot_id)
            artifact = manifest.artifact(health.artifact_id)
            slot_path = self._verify_signed_slot(health.slot_id, manifest, artifact)
            if health_identity_for_slot(
                transaction_id=health.transaction_id,
                slot_id=health.slot_id,
                slot_path=slot_path,
                manifest=manifest,
                artifact=artifact,
            ) != health:
                raise ActivationIntentError("activation rollback candidate changed")
            self.slots.restore(prior)
            pointers = prior
        if pointers != prior:
            raise ActivationIntentError("activation rollback pointers diverged")
        if latest.state is InstallState.COMPLETED:
            latest = self.journal.append(
                transaction_id=health.transaction_id,
                state=InstallState.ROLLBACK,
                event="confirmed_runtime_failed_before_data_barrier",
                details={
                    "slot_id": health.slot_id,
                    "error_type": _safe_error(str(receipt.get("error_code", "unknown"))),
                },
            )
        if latest.state is not InstallState.ROLLBACK:
            raise ActivationIntentError("activation rollback did not reach a terminal state")
        updated = dict(receipt)
        updated["state"] = "rolled_back_pre_data"
        updated["receipt_digest"] = _receipt_digest(updated)
        atomic_write_json(self.receipt_path, updated)
        try:
            self.slots.discard(health.slot_id)
        except StorageError:
            pass
        return prior.current

    def _verify_signed_slot(
        self,
        slot_id: str,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> Path:
        verify_manifest_signature(manifest, self.verifier)
        verify_artifact_signature(manifest, artifact, self.verifier)
        if (
            artifact.artifact_id != f"core-{self.host_platform}-{self.host_architecture}"
            or artifact.platform != self.host_platform
            or artifact.architecture != self.host_architecture
        ):
            raise ActivationIntentError("candidate is not the canonical host Runtime core")
        path = self.slots.validate_receipt(
            slot_id=slot_id, manifest=manifest, artifact=artifact
        )
        validate_installed_pack_set(
            path,
            manifest,
            verifier=self.verifier,
            platform=self.host_platform,
            architecture=self.host_architecture,
            pack_content_verifier=self.pack_content_verifier,
        )
        return path

    @staticmethod
    def _verify_active(
        intent: ProvisionalActivationIntent, active: Mapping[str, Any]
    ) -> None:
        expected = {
            "transaction_id": intent.transaction_id,
            "slot_id": intent.slot_id,
            "release_id": intent.release_id,
            "version": intent.version,
            "build_digest": intent.build_digest,
            "artifact_id": intent.artifact_id,
            "artifact_sha256": intent.artifact_sha256,
        }
        if any(active.get(key) != value for key, value in expected.items()):
            raise ActivationIntentError("active transaction does not match activation intent")
        prior = active.get("prior_pointers")
        if not isinstance(prior, Mapping) or SlotPointers.from_dict(prior) != intent.prior_pointers:
            raise ActivationIntentError("active prior pointers do not match activation intent")

    def _write_receipt(
        self,
        intent: ProvisionalActivationIntent,
        *,
        state: str,
        data_barrier_crossed: bool,
    ) -> None:
        value = {
            "schema_version": 1,
            "state": state,
            "transaction_id": intent.transaction_id,
            "slot_id": intent.slot_id,
            "release_id": intent.release_id,
            "version": intent.version,
            "build_digest": intent.build_digest,
            "artifact_id": intent.artifact_id,
            "artifact_sha256": intent.artifact_sha256,
            "health_identity": intent.health_identity.to_dict(),
            "prior_pointers": intent.prior_pointers.to_dict(),
            "data_barrier_crossed": data_barrier_crossed,
        }
        value["receipt_digest"] = _receipt_digest(value)
        atomic_write_json(self.receipt_path, value)

    def _cleanup(self, intent: ProvisionalActivationIntent) -> None:
        for path in (self.intent_path, self.active_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        transaction = (self.transactions_dir / intent.transaction_id).resolve()
        parent = self.transactions_dir.resolve()
        if transaction.parent != parent or transaction.name != intent.transaction_id:
            raise ActivationIntentError("activation transaction cleanup path is unsafe")
        if transaction.exists():
            metadata = transaction.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ActivationIntentError("activation transaction cleanup target is unsafe")
            shutil.rmtree(transaction)


def health_identity_for_slot(
    *,
    transaction_id: str,
    slot_id: str,
    slot_path: Path,
    manifest: ReleaseManifest,
    artifact: ReleaseArtifact,
) -> ActivationHealthIdentity:
    marker = _read_json_object(slot_path / ".slot.json", required=True)
    assert marker is not None
    payload_digest = marker.get("payload_digest")
    if not isinstance(payload_digest, str) or _HEX_64.fullmatch(payload_digest) is None:
        raise ActivationIntentError("candidate payload digest is invalid")
    payload = slot_path / "payload"
    config_path = payload / "runtime-config.json"
    config_sha = _EMPTY_DIGEST
    web_sha = _EMPTY_DIGEST
    database_binding = ""
    if config_path.exists():
        config_bytes = _read_regular_bytes(config_path, _MAX_CONFIG_BYTES)
        config_sha = hashlib.sha256(config_bytes).hexdigest()
        try:
            config = json.loads(config_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ActivationIntentError("candidate Runtime config is unreadable") from error
        if not isinstance(config, Mapping) or not isinstance(config.get("paths"), Mapping):
            raise ActivationIntentError("candidate Runtime paths are invalid")
        paths = config["paths"]
        web_relative = _safe_relative(paths.get("web_manifest"), "web manifest")
        database_relative = _safe_relative(paths.get("database"), "database")
        web_path = payload.joinpath(*web_relative.parts)
        web_bytes = _read_regular_bytes(web_path, _MAX_WEB_MANIFEST_BYTES)
        try:
            web_manifest = json.loads(web_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ActivationIntentError("candidate Web manifest is unreadable") from error
        web_sha = (
            web_manifest.get("bundle_sha256")
            if isinstance(web_manifest, Mapping)
            else None
        )
        if not isinstance(web_sha, str) or _HEX_64.fullmatch(web_sha) is None:
            raise ActivationIntentError("candidate Web bundle digest is invalid")
        database_binding = database_relative.as_posix()
    storage_identity = hashlib.sha256(
        _canonical(
            {
                "storage_schema_version": STORAGE_SCHEMA_VERSION,
                "database_binding": database_binding,
                "runtime_config_sha256": config_sha,
            }
        )
    ).hexdigest()
    return ActivationHealthIdentity(
        schema_version=1,
        transaction_id=transaction_id,
        slot_id=slot_id,
        release_id=manifest.release_id,
        version=manifest.version,
        build_digest=manifest.build_digest,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        payload_digest=payload_digest,
        runtime_config_sha256=config_sha,
        web_bundle_sha256=web_sha,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        storage_identity=storage_identity,
    )


def activation_health_response(
    identity: ActivationHealthIdentity, nonce: str
) -> dict[str, Any]:
    return {
        "status": "ready",
        "identity": identity.to_dict(),
        "proof": identity.proof(nonce),
    }


def verify_activation_health_response(
    identity: ActivationHealthIdentity,
    nonce: str,
    response: Mapping[str, Any],
) -> bool:
    if set(response) != {"status", "identity", "proof"} or response.get("status") != "ready":
        return False
    raw_identity = response.get("identity")
    proof = response.get("proof")
    if not isinstance(raw_identity, Mapping) or not isinstance(proof, str):
        return False
    try:
        observed = ActivationHealthIdentity.from_dict(raw_identity)
    except ActivationIntentError:
        return False
    return observed == identity and hmac.compare_digest(proof, identity.proof(nonce))


def _receipt_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    return hashlib.sha256(
        b"EcoreX activation receipt v1\0" + _canonical(unsigned)
    ).hexdigest()


def _read_json_object(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ActivationIntentError(f"required activation record {path.name} is missing")
        return None
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("activation record is not a regular file")
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ActivationIntentError(f"activation record {path.name} is unreadable") from error
    if not isinstance(raw, dict):
        raise ActivationIntentError(f"activation record {path.name} is invalid")
    if path.name == "activation-receipt.json":
        digest = raw.get("receipt_digest")
        if not isinstance(digest, str) or not hmac.compare_digest(digest, _receipt_digest(raw)):
            raise ActivationIntentError("activation receipt checksum is invalid")
    return raw


def _read_regular_bytes(path: Path, limit: int) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("not regular")
        if metadata.st_size > limit:
            raise OSError("too large")
        value = path.read_bytes()
    except OSError as error:
        raise ActivationIntentError(f"candidate member {path.name} is invalid") from error
    if len(value) != metadata.st_size:
        raise ActivationIntentError(f"candidate member {path.name} changed while reading")
    return value


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ActivationIntentError(f"candidate {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ActivationIntentError(f"candidate {label} path is invalid")
    return path


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _safe_error(value: str) -> str:
    normalized = "".join(
        character for character in str(value)[:128] if character.isalnum() or character in "._-"
    )
    return normalized or "ActivationHealthFailed"


__all__ = [
    "ACTIVATION_HEALTH_PATH",
    "ACTIVATION_NONCE_ENV",
    "ACTIVATION_NONCE_HEADER",
    "ACTIVATION_TRANSACTION_ENV",
    "ActivationHealthIdentity",
    "ActivationIntentError",
    "ActivationLaunchContext",
    "ProvisionalActivationController",
    "ProvisionalActivationIntent",
    "VerifiedProvisionalActivation",
    "activation_health_response",
    "health_identity_for_slot",
    "verify_activation_health_response",
]
