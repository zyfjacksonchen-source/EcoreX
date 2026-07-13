"""Transactional release candidates, gates, rollouts, and client distribution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit
import uuid

from ecorex.release.public_index import (
    MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES,
    PublicBootstrapIndexError,
    validate_public_bootstrap_index,
)
from ecorex.release.live_acceptance import LIVE_ACCEPTANCE_GATES
from ecorex.update import (
    ReleaseChannel,
    ReleaseManifest,
    SignatureVerifier,
    verify_artifact_signature,
    verify_manifest_signature,
)

from .models import (
    CandidateProjection,
    ControlPrincipal,
    ControlUpdateSignal,
    ControlUpdateSignalBatch,
    DistributionProjection,
    KillSwitchProjection,
    RollbackProjection,
    RolloutProjection,
)
from .schema import ControlPlaneSchemaManager, ControlPlaneSchemaReceipt

REQUIRED_RELEASE_GATES = frozenset(
    {
        "lint",
        "typecheck",
        "unit",
        "contract",
        "integration",
        "e2e",
        "reproducibility",
        "image-shared-storage",
        "image-soak",
        "windows-build",
        "macos-build",
        "migration-dry-run",
        "sbom",
        "license",
        "secret-scan",
        "size-scan",
        "signature",
        "github-release",
        "mirror-sync",
        "cdn-sync",
        "bootstrap-index",
    }
) | LIVE_ACCEPTANCE_GATES
STABLE_ONLY_RELEASE_GATES = frozenset({"bootstrap-index"})


def required_release_gates(channel: ReleaseChannel | str) -> frozenset[str]:
    """Return the exact gates for a channel without weakening stable GA."""

    normalized = (
        channel if isinstance(channel, ReleaseChannel) else ReleaseChannel(channel)
    )
    if normalized is ReleaseChannel.STABLE:
        return REQUIRED_RELEASE_GATES
    return REQUIRED_RELEASE_GATES - STABLE_ONLY_RELEASE_GATES


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLIENT_UPDATE_STATES = frozenset(
    {"idle", "available", "downloading", "awaiting_user", "activating", "failed"}
)
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_ADMIN_RESUME_CANDIDATE_LIMIT = 200
_ADMIN_RESUME_ROLLOUT_LIMIT = 500
MAX_UPDATE_HINT_BATCH_SIZE = 1024
_ZERO_AUDIT_DIGEST = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BOOTSTRAP_PROOF = re.compile(
    r"^bootstrap-index-proof:(bread_[0-9a-f]{32}):sha256:([0-9a-f]{64})$"
)
_RESUME_RELEASE_ORDER_SQL = """
WITH creation_order AS (
    SELECT target_id, MIN(sequence) AS creation_sequence
    FROM control_admin_audit
    WHERE action = 'candidate.create'
    GROUP BY target_id
)
SELECT releases.release_id
FROM control_releases AS releases
LEFT JOIN creation_order ON creation_order.target_id = releases.release_id
ORDER BY releases.created_at DESC,
         COALESCE(creation_order.creation_sequence, 0) DESC,
         releases.release_id DESC
"""
_RESUME_ROLLOUT_ORDER_SQL = """
WITH creation_order AS (
    SELECT target_id, MIN(sequence) AS creation_sequence
    FROM control_admin_audit
    WHERE action = 'rollout.create'
    GROUP BY target_id
)
SELECT rollouts.rollout_id
FROM control_rollouts AS rollouts
LEFT JOIN creation_order ON creation_order.target_id = rollouts.rollout_id
LEFT JOIN control_release_rollbacks AS rollbacks
    ON rollbacks.rollback_id = rollouts.rollout_id
WHERE rollbacks.rollback_id IS NULL
ORDER BY rollouts.created_at DESC,
         COALESCE(creation_order.creation_sequence, 0) DESC,
         rollouts.rollout_id DESC
"""


class _AdminResumeFactsRecord(TypedDict):
    schema_version: Literal[1]
    candidates: tuple[CandidateProjection, ...]
    latest_candidate_id: str | None
    rollouts: tuple[RolloutProjection, ...]
    latest_rollout_id: str | None
    channel_kill_switches: tuple[KillSwitchProjection, ...]
    distribution: DistributionProjection
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class UpdateHintClient:
    """Immutable client facts used by one bounded update-hint snapshot."""

    principal: ControlPrincipal
    channel: ReleaseChannel
    platform: str
    architecture: str
    current_version: str
    update_state: str = "idle"
    current_release_id: str | None = None
    current_build_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ClientReleaseDecision:
    """Canonical feed decision, including rollback authority inputs when needed."""

    manifest: ReleaseManifest
    rollback_id: str | None = None
    source_manifest: ReleaseManifest | None = None
    authorization_ttl_seconds: int | None = None

    @property
    def is_rollback(self) -> bool:
        return self.rollback_id is not None


@dataclass(frozen=True, slots=True)
class _HintRollout:
    rollout_id: str
    release_id: str
    channel: str
    percentage: int
    organizations: frozenset[str]
    accounts: frozenset[str]
    minimum_compatible_version: str | None
    manifest_json: str
    manifest_sha256: str
    release_version: str
    release_build_digest: str
    rollback_id: str | None
    source_release_id: str | None
    source_version: str | None
    source_build_digest: str | None


def _safe_identity(value: object) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneConflict(ControlPlaneError):
    pass


class ControlPlaneNotFound(ControlPlaneError):
    pass


class ReleaseGateError(ControlPlaneConflict):
    pass


class ControlPlaneRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        verifier: SignatureVerifier,
        bootstrap_freshness_verifier: SignatureVerifier | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.verifier = verifier
        self.bootstrap_freshness_verifier = bootstrap_freshness_verifier
        self.schema_receipt: ControlPlaneSchemaReceipt = ControlPlaneSchemaManager(
            self.path
        ).validate()
        self._audit_checkpoint_lock = threading.RLock()
        self._audit_checkpoint = (0, _ZERO_AUDIT_DIGEST)
        self._audit_checkpoint_fault: str | None = None
        self.verify_full_integrity()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def create_candidate(
        self,
        manifest: ReleaseManifest,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> CandidateProjection:
        verify_manifest_signature(manifest, self.verifier)
        for artifact in manifest.artifacts:
            verify_artifact_signature(manifest, artifact, self.verifier)
        manifest_json = manifest.to_json()
        request = {"manifest_sha256": _sha(manifest_json)}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "candidate.create", request
            )
            if replay is not None:
                return CandidateProjection.model_validate(replay)
            existing = connection.execute(
                "SELECT manifest_sha256 FROM control_releases WHERE release_id = ?",
                (manifest.release_id,),
            ).fetchone()
            digest = _sha(manifest_json)
            if existing is not None and existing["manifest_sha256"] != digest:
                raise ControlPlaneConflict(
                    "release_id was reused with a different manifest"
                )
            if existing is None:
                connection.execute(
                    "INSERT INTO control_releases("
                    "release_id,version,build_digest,channel,manifest_json,manifest_sha256,"
                    "status,created_at) VALUES (?,?,?,?,?,?,'candidate',?)",
                    (
                        manifest.release_id,
                        manifest.version,
                        manifest.build_digest,
                        manifest.channel.value,
                        manifest_json,
                        digest,
                        _now(),
                    ),
                )
            result = self._candidate(connection, manifest.release_id)
            self._audit(
                connection, actor, "candidate.create", manifest.release_id, request
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "candidate.create",
                request,
                result.model_dump(),
            )
            return result

    def record_gate(
        self,
        release_id: str,
        gate_name: str,
        *,
        status: str,
        evidence: str,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> CandidateProjection:
        if gate_name not in REQUIRED_RELEASE_GATES:
            raise ValueError("release gate is not part of the v1 publication contract")
        if status not in {"passed", "failed"}:
            raise ValueError("release gate status is invalid")
        if not isinstance(evidence, str) or len(evidence) > 4096:
            raise ValueError("release gate evidence is invalid")
        request = {
            "release_id": release_id,
            "gate": gate_name,
            "status": status,
            "evidence": evidence,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "gate.record", request
            )
            if replay is not None:
                return CandidateProjection.model_validate(replay)
            release = self._require_release(connection, release_id)
            if release["status"] != "candidate":
                raise ControlPlaneConflict("published release gates are immutable")
            if gate_name == "bootstrap-index" and status == "passed":
                self._require_bootstrap_index_proof(
                    connection,
                    release=release,
                    evidence=evidence,
                )
            connection.execute(
                "INSERT INTO control_release_gates(release_id,gate_name,status,evidence,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(release_id,gate_name) DO UPDATE SET "
                "status=excluded.status,evidence=excluded.evidence,updated_at=excluded.updated_at",
                (release_id, gate_name, status, evidence, _now()),
            )
            result = self._candidate(connection, release_id)
            self._audit(connection, actor, "gate.record", release_id, request)
            self._remember(
                connection,
                actor,
                client_request_id,
                "gate.record",
                request,
                result.model_dump(),
            )
            return result

    def stage_bootstrap_index(
        self,
        index_bytes: bytes,
        *,
        public_url: str,
        actor: ControlPrincipal,
        client_request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify and durably stage one fresh, signed monotonic authority."""

        index = _parse_bootstrap_index_bytes(
            index_bytes,
            verifier=self.verifier,
            freshness_verifier=self._require_bootstrap_freshness_verifier(),
            now=now,
        )
        _validate_public_pointer_url(public_url)
        release = index["release"]
        authority = index["authority"]
        freshness = index["freshness"]
        target = authority["target"]
        digest = hashlib.sha256(index_bytes).hexdigest()
        request = {
            "index_sha256": digest,
            "index_size_bytes": len(index_bytes),
            "public_url": public_url,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.stage",
                request,
            )
            if replay is not None:
                return dict(replay)
            active = self._bootstrap_active(connection)
            target_json = _json(target)
            if active is not None:
                active_sequence = int(active["authority_sequence"])
                candidate_sequence = int(authority["sequence"])
                if candidate_sequence < active_sequence:
                    raise ControlPlaneConflict(
                        "signed Bootstrap pointer rollback was refused"
                    )
                if candidate_sequence == active_sequence:
                    if (
                        str(active["authority_revision"]) != authority["revision"]
                        or str(active["authority_target_json"]) != target_json
                    ):
                        raise ControlPlaneConflict(
                            "signed Bootstrap pointer sequence was reused for another target"
                        )
                    if digest != active["index_sha256"] and (
                        str(freshness["issued_at"])
                        <= str(active["authority_issued_at"])
                        or str(freshness["expires_at"])
                        <= str(active["authority_expires_at"])
                    ):
                        raise ControlPlaneConflict(
                            "signed Bootstrap pointer freshness replay was refused"
                        )
            existing = connection.execute(
                "SELECT * FROM bootstrap_index_stages "
                "WHERE index_sha256=? AND public_url=?",
                (digest, public_url),
            ).fetchone()
            if existing is None:
                record_id = (
                    "bstage_"
                    + hashlib.sha256(
                        (
                            digest
                            + "\0"
                            + (
                                str(active["activation_record_id"])
                                if active is not None
                                else ""
                            )
                        ).encode("ascii")
                    ).hexdigest()[:32]
                )
                previous = _bootstrap_previous_values(active)
                connection.execute(
                    "INSERT INTO bootstrap_index_stages("
                    "record_id,release_id,version,build_digest,authority_sequence,"
                    "authority_revision,authority_issued_at,authority_expires_at,"
                    "authority_target_json,authority_json,index_sha256,index_size_bytes,"
                    "index_bytes,public_url,previous_activation_record_id,"
                    "previous_sequence,previous_revision,previous_index_sha256,"
                    "previous_target_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        release["release_id"],
                        release["version"],
                        release["build_digest"],
                        authority["sequence"],
                        authority["revision"],
                        freshness["issued_at"],
                        freshness["expires_at"],
                        target_json,
                        _json(authority),
                        digest,
                        len(index_bytes),
                        index_bytes,
                        public_url,
                        *previous,
                        _now(),
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM bootstrap_index_stages WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                self._audit(
                    connection,
                    actor,
                    "bootstrap-index.stage",
                    record_id,
                    request,
                )
                self._append_bootstrap_outbox(
                    connection,
                    event_type="bootstrap-index.staged",
                    record_id=record_id,
                    payload=request,
                )
            assert existing is not None
            result = self._bootstrap_stage_projection(existing)
            self._remember(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.stage",
                request,
                result,
            )
            return result

    def prepare_bootstrap_index_activation(
        self,
        *,
        release_id: str,
        stage_record_id: str,
        index_sha256: str,
        expected_previous_activation_record_id: str | None,
        expected_previous_sequence: int | None,
        expected_previous_revision: str | None,
        expected_previous_index_sha256: str | None,
        expected_previous_target: dict[str, Any] | None,
        actor: ControlPrincipal,
        client_request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Commit one durable publishing intent without external I/O."""

        request = {
            "release_id": release_id,
            "stage_record_id": stage_record_id,
            "index_sha256": index_sha256,
            "expected_previous_activation_record_id": (
                expected_previous_activation_record_id
            ),
            "expected_previous_sequence": expected_previous_sequence,
            "expected_previous_revision": expected_previous_revision,
            "expected_previous_index_sha256": expected_previous_index_sha256,
            "expected_previous_target": expected_previous_target,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.prepare-publication",
                request,
            )
            if replay is not None:
                return dict(replay)
            stage = connection.execute(
                "SELECT * FROM bootstrap_index_stages WHERE record_id=?",
                (stage_record_id,),
            ).fetchone()
            if (
                stage is None
                or stage["release_id"] != release_id
                or stage["index_sha256"] != index_sha256
            ):
                raise ControlPlaneNotFound("staged Bootstrap index does not exist")
            active = self._bootstrap_active(connection)
            expected = (
                expected_previous_activation_record_id,
                expected_previous_sequence,
                expected_previous_revision,
                expected_previous_index_sha256,
                _json(expected_previous_target)
                if expected_previous_target is not None
                else None,
            )
            captured = (
                stage["previous_activation_record_id"],
                stage["previous_sequence"],
                stage["previous_revision"],
                stage["previous_index_sha256"],
                stage["previous_target_json"],
            )
            current = _bootstrap_previous_values(active)
            if expected != captured or current != captured:
                raise ControlPlaneConflict(
                    "Bootstrap index authority compare-and-swap failed"
                )
            _parse_bootstrap_index_bytes(
                bytes(stage["index_bytes"]),
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=now,
            )
            intent_id = (
                "bpub_"
                + hashlib.sha256(
                    (stage_record_id + "\0" + index_sha256).encode("ascii")
                ).hexdigest()[:32]
            )
            intent = connection.execute(
                "SELECT * FROM bootstrap_index_publication_intents "
                "WHERE stage_record_id=?",
                (stage_record_id,),
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM bootstrap_index_publication_lease WHERE singleton=1"
            ).fetchone()
            activation = connection.execute(
                "SELECT * FROM bootstrap_index_activations "
                "WHERE publication_intent_record_id=?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                if lease is not None:
                    raise ControlPlaneConflict(
                        "another Bootstrap publication is already in progress"
                    )
                created_at = _now()
                connection.execute(
                    "INSERT INTO bootstrap_index_publication_intents("
                    "record_id,stage_record_id,previous_activation_record_id,"
                    "previous_sequence,previous_revision,previous_index_sha256,"
                    "previous_target_json,candidate_index_sha256,candidate_size_bytes,"
                    "public_url,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        stage_record_id,
                        *captured,
                        stage["index_sha256"],
                        stage["index_size_bytes"],
                        stage["public_url"],
                        created_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO bootstrap_index_publication_lease VALUES(1,?,?)",
                    (intent_id, created_at),
                )
                intent = connection.execute(
                    "SELECT * FROM bootstrap_index_publication_intents "
                    "WHERE record_id=?",
                    (intent_id,),
                ).fetchone()
                self._audit(
                    connection,
                    actor,
                    "bootstrap-index.prepare-publication",
                    intent_id,
                    request,
                )
                self._append_bootstrap_outbox(
                    connection,
                    event_type="bootstrap-index.publication-requested",
                    record_id=intent_id,
                    payload=request,
                )
            elif activation is None and (
                lease is None or lease["intent_record_id"] != intent_id
            ):
                raise ControlPlaneConflict(
                    "Bootstrap publication intent lost its durable lease"
                )
            assert intent is not None
            result = self._bootstrap_intent_projection(
                connection, intent, activation=activation
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.prepare-publication",
                request,
                result,
            )
            return result

    def bootstrap_index_publication_material(
        self, intent_record_id: str
    ) -> dict[str, Any]:
        """Return exact bytes for a committed publishing intent."""

        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT intents.*,stages.index_bytes FROM "
                "bootstrap_index_publication_intents AS intents "
                "JOIN bootstrap_index_stages AS stages "
                "ON stages.record_id=intents.stage_record_id "
                "WHERE intents.record_id=?",
                (intent_record_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound(
                    "Bootstrap publication intent does not exist"
                )
            return {
                "intent_record_id": str(row["record_id"]),
                "payload": bytes(row["index_bytes"]),
                "candidate_index_sha256": str(row["candidate_index_sha256"]),
                "previous_index_sha256": row["previous_index_sha256"],
                "public_url": str(row["public_url"]),
            }

    def finalize_bootstrap_index_activation(
        self,
        *,
        intent_record_id: str,
        observed_bytes: bytes,
        public_object_revision_id: str,
        actor: ControlPrincipal,
        client_request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically make exact public readback active and trusted."""

        if (
            not isinstance(observed_bytes, bytes)
            or not 1 <= len(observed_bytes) <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
            or not _safe_identity(public_object_revision_id)
        ):
            raise ValueError("Bootstrap publication completion is invalid")
        observed_sha256 = hashlib.sha256(observed_bytes).hexdigest()
        request = {
            "intent_record_id": intent_record_id,
            "observed_sha256": observed_sha256,
            "observed_size_bytes": len(observed_bytes),
            "public_object_revision_id": public_object_revision_id,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.finalize-publication",
                request,
            )
            if replay is not None:
                return dict(replay)
            intent = connection.execute(
                "SELECT intents.*,stages.release_id,stages.index_bytes,"
                "stages.authority_sequence,stages.authority_revision,"
                "stages.authority_issued_at,stages.authority_expires_at,"
                "stages.authority_target_json FROM "
                "bootstrap_index_publication_intents AS intents "
                "JOIN bootstrap_index_stages AS stages "
                "ON stages.record_id=intents.stage_record_id "
                "WHERE intents.record_id=?",
                (intent_record_id,),
            ).fetchone()
            if intent is None:
                raise ControlPlaneNotFound(
                    "Bootstrap publication intent does not exist"
                )
            existing = connection.execute(
                "SELECT * FROM bootstrap_index_activations "
                "WHERE publication_intent_record_id=?",
                (intent_record_id,),
            ).fetchone()
            if existing is not None:
                result = self._completed_bootstrap_projection(connection, existing)
                self._remember(
                    connection,
                    actor,
                    client_request_id,
                    "bootstrap-index.finalize-publication",
                    request,
                    result,
                )
                return result
            lease = connection.execute(
                "SELECT intent_record_id FROM bootstrap_index_publication_lease "
                "WHERE singleton=1"
            ).fetchone()
            if lease is None or lease["intent_record_id"] != intent_record_id:
                raise ControlPlaneConflict(
                    "Bootstrap publication intent is not current"
                )
            captured = (
                intent["previous_activation_record_id"],
                intent["previous_sequence"],
                intent["previous_revision"],
                intent["previous_index_sha256"],
                intent["previous_target_json"],
            )
            if (
                _bootstrap_previous_values(self._bootstrap_active(connection))
                != captured
            ):
                raise ControlPlaneConflict(
                    "Bootstrap active authority changed during publication"
                )
            if (
                observed_sha256 != intent["candidate_index_sha256"]
                or len(observed_bytes) != intent["candidate_size_bytes"]
                or observed_bytes != bytes(intent["index_bytes"])
            ):
                raise ControlPlaneConflict(
                    "public Bootstrap readback differs from publishing intent"
                )
            _parse_bootstrap_index_bytes(
                observed_bytes,
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=now,
            )
            activation_id = (
                "bactive_"
                + hashlib.sha256(
                    (intent_record_id + "\0" + observed_sha256).encode("ascii")
                ).hexdigest()[:32]
            )
            activated_at = _now()
            connection.execute(
                "INSERT INTO bootstrap_index_activations("
                "record_id,publication_intent_record_id,stage_record_id,"
                "previous_activation_record_id,authority_sequence,"
                "authority_revision,authority_issued_at,authority_expires_at,"
                "authority_target_json,index_sha256,index_size_bytes,public_url,"
                "public_object_revision_id,activated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    activation_id,
                    intent_record_id,
                    intent["stage_record_id"],
                    intent["previous_activation_record_id"],
                    intent["authority_sequence"],
                    intent["authority_revision"],
                    intent["authority_issued_at"],
                    intent["authority_expires_at"],
                    intent["authority_target_json"],
                    observed_sha256,
                    len(observed_bytes),
                    intent["public_url"],
                    public_object_revision_id,
                    activated_at,
                ),
            )
            connection.execute(
                "INSERT INTO bootstrap_index_active_state("
                "singleton,activation_record_id,authority_sequence,authority_revision,"
                "authority_issued_at,authority_expires_at,authority_target_json,"
                "index_sha256,updated_at) VALUES(1,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "activation_record_id=excluded.activation_record_id,"
                "authority_sequence=excluded.authority_sequence,"
                "authority_revision=excluded.authority_revision,"
                "authority_issued_at=excluded.authority_issued_at,"
                "authority_expires_at=excluded.authority_expires_at,"
                "authority_target_json=excluded.authority_target_json,"
                "index_sha256=excluded.index_sha256,updated_at=excluded.updated_at",
                (
                    activation_id,
                    intent["authority_sequence"],
                    intent["authority_revision"],
                    intent["authority_issued_at"],
                    intent["authority_expires_at"],
                    intent["authority_target_json"],
                    observed_sha256,
                    activated_at,
                ),
            )
            readback_id = (
                "bread_"
                + hashlib.sha256(
                    (activation_id + "\0" + observed_sha256).encode("ascii")
                ).hexdigest()[:32]
            )
            connection.execute(
                "INSERT INTO bootstrap_index_readbacks VALUES(?,?,?,?,?,?)",
                (
                    readback_id,
                    activation_id,
                    observed_sha256,
                    len(observed_bytes),
                    intent["public_url"],
                    activated_at,
                ),
            )
            deleted = connection.execute(
                "DELETE FROM bootstrap_index_publication_lease "
                "WHERE singleton=1 AND intent_record_id=?",
                (intent_record_id,),
            ).rowcount
            if deleted != 1:
                raise ControlPlaneConflict(
                    "Bootstrap publication lease changed during finalization"
                )
            self._audit(
                connection,
                actor,
                "bootstrap-index.activate-and-read-back",
                activation_id,
                request,
            )
            self._append_bootstrap_outbox(
                connection,
                event_type="bootstrap-index.activated",
                record_id=activation_id,
                payload=request,
            )
            self._append_bootstrap_outbox(
                connection,
                event_type="bootstrap-index.read-back",
                record_id=readback_id,
                payload=request,
            )
            activation = connection.execute(
                "SELECT * FROM bootstrap_index_activations WHERE record_id=?",
                (activation_id,),
            ).fetchone()
            assert activation is not None
            result = self._completed_bootstrap_projection(connection, activation)
            self._remember(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.finalize-publication",
                request,
                result,
            )
            return result

    def record_bootstrap_index_readback(
        self,
        *,
        activation_record_id: str,
        observed_bytes: bytes,
        actor: ControlPrincipal,
        client_request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create trusted proof only after exact active bytes were read publicly."""

        if (
            not isinstance(observed_bytes, bytes)
            or not 1 <= len(observed_bytes) <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
        ):
            raise ValueError("public Bootstrap readback bytes are invalid")
        observed_sha256 = hashlib.sha256(observed_bytes).hexdigest()
        request = {
            "activation_record_id": activation_record_id,
            "observed_sha256": observed_sha256,
            "observed_size_bytes": len(observed_bytes),
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.read-back",
                request,
            )
            if replay is not None:
                return dict(replay)
            active = self._bootstrap_active(connection)
            if active is None or active["activation_record_id"] != activation_record_id:
                raise ControlPlaneConflict(
                    "public Bootstrap readback does not target the active authority"
                )
            stage = connection.execute(
                "SELECT stages.* FROM bootstrap_index_stages AS stages "
                "JOIN bootstrap_index_activations AS activations "
                "ON activations.stage_record_id=stages.record_id "
                "WHERE activations.record_id=?",
                (activation_record_id,),
            ).fetchone()
            if (
                stage is None
                or observed_sha256 != stage["index_sha256"]
                or len(observed_bytes) != stage["index_size_bytes"]
                or observed_bytes != bytes(stage["index_bytes"])
            ):
                raise ControlPlaneConflict(
                    "public Bootstrap readback differs from active exact bytes"
                )
            _parse_bootstrap_index_bytes(
                observed_bytes,
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=now,
            )
            existing = connection.execute(
                "SELECT * FROM bootstrap_index_readbacks WHERE activation_record_id=?",
                (activation_record_id,),
            ).fetchone()
            if existing is None:
                record_id = (
                    "bread_"
                    + hashlib.sha256(
                        (activation_record_id + "\0" + observed_sha256).encode("ascii")
                    ).hexdigest()[:32]
                )
                connection.execute(
                    "INSERT INTO bootstrap_index_readbacks VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        activation_record_id,
                        observed_sha256,
                        len(observed_bytes),
                        stage["public_url"],
                        _now(),
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM bootstrap_index_readbacks WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                self._audit(
                    connection,
                    actor,
                    "bootstrap-index.read-back",
                    record_id,
                    request,
                )
                self._append_bootstrap_outbox(
                    connection,
                    event_type="bootstrap-index.read-back",
                    record_id=record_id,
                    payload=request,
                )
            assert existing is not None
            result = self._bootstrap_proof_projection(connection, existing)
            self._remember(
                connection,
                actor,
                client_request_id,
                "bootstrap-index.read-back",
                request,
                result,
            )
            return result

    def active_bootstrap_index_bytes(self) -> bytes | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT stages.index_bytes FROM bootstrap_index_active_state AS state "
                "JOIN bootstrap_index_activations AS activations "
                "ON activations.record_id=state.activation_record_id "
                "JOIN bootstrap_index_stages AS stages "
                "ON stages.record_id=activations.stage_record_id WHERE state.singleton=1"
            ).fetchone()
            return bytes(row["index_bytes"]) if row is not None else None

    def trusted_bootstrap_index_proof(
        self,
        release_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._read_transaction() as connection:
            proof = self._latest_bootstrap_proof(connection, release_id)
            if proof is None:
                raise ControlPlaneNotFound(
                    "trusted Bootstrap index readback proof does not exist"
                )
            _parse_bootstrap_index_bytes(
                bytes(proof["index_bytes"]),
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=now,
            )
            return self._bootstrap_proof_projection(connection, proof)

    def begin_bootstrap_freshness_refresh(
        self,
        *,
        owner_id: str,
        force: bool,
        lead_seconds: int,
        check_interval_seconds: int,
        lease_seconds: int,
        actor: ControlPrincipal,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Acquire or resume one durable same-authority freshness attempt."""

        if (
            not _safe_identity(owner_id)
            or not 60 * 60 <= lead_seconds <= 23 * 60 * 60
            or not 5 * 60 <= check_interval_seconds <= 6 * 60 * 60
            or not 5 * 60 <= lease_seconds <= 30 * 60
        ):
            raise ValueError("Bootstrap freshness scheduler bounds are invalid")
        observed = _observed_utc(now)
        checked_at = observed.isoformat()
        next_check_at = (
            observed + timedelta(seconds=check_interval_seconds)
        ).isoformat()
        with self._transaction() as connection:
            active = self._bootstrap_active(connection)
            if active is None:
                self._set_bootstrap_refresh_state(
                    connection,
                    status="idle",
                    active_expires_at=None,
                    last_checked_at=checked_at,
                    next_check_at=next_check_at,
                    attempt_record_id=None,
                    error_code=None,
                )
                self._append_bootstrap_refresh_event(
                    connection, attempt_record_id=None, status="no-active"
                )
                return {
                    "state": "no-active",
                    "due": False,
                    "active_expires_at": None,
                    "next_check_at": next_check_at,
                }
            index = _parse_bootstrap_index_bytes(
                bytes(active["index_bytes"]),
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=observed,
                allow_expired_freshness=True,
            )
            freshness = index["freshness"]
            authority = index["authority"]
            expires_at = str(freshness["expires_at"])
            expires = _parse_bootstrap_time(expires_at)
            remaining_seconds = int((expires - observed).total_seconds())
            due = force or remaining_seconds <= lead_seconds
            if not due:
                self._set_bootstrap_refresh_state(
                    connection,
                    status="healthy",
                    active_expires_at=expires_at,
                    last_checked_at=checked_at,
                    next_check_at=next_check_at,
                    attempt_record_id=None,
                    error_code=None,
                )
                self._append_bootstrap_refresh_event(
                    connection, attempt_record_id=None, status="not-due"
                )
                return {
                    "state": "not-due",
                    "due": False,
                    "active_expires_at": expires_at,
                    "remaining_seconds": remaining_seconds,
                    "next_check_at": next_check_at,
                }
            lease = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
            ).fetchone()
            if (
                lease is not None
                and _parse_iso_time(str(lease["expires_at"])) > observed
            ):
                if lease["owner_id"] != owner_id:
                    return {
                        "state": "busy",
                        "due": True,
                        "attempt_record_id": str(lease["attempt_record_id"]),
                        "lease_expires_at": str(lease["expires_at"]),
                        "active_expires_at": expires_at,
                        "next_check_at": next_check_at,
                    }
            elif lease is not None:
                connection.execute(
                    "DELETE FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
                )
                lease = None
            attempt = connection.execute(
                "SELECT attempts.* FROM bootstrap_freshness_refresh_attempts AS attempts "
                "WHERE attempts.source_activation_record_id=? AND NOT EXISTS("
                "SELECT 1 FROM bootstrap_freshness_refresh_events AS events "
                "WHERE events.attempt_record_id=attempts.record_id "
                "AND events.status='succeeded') "
                "AND julianday(attempts.expires_at)>julianday(?) "
                "ORDER BY attempts.created_at DESC LIMIT 1",
                (active["activation_record_id"], _format_bootstrap_time(observed)),
            ).fetchone()
            created = False
            if attempt is None:
                issued_at = _format_bootstrap_time(observed)
                candidate_expires = observed + timedelta(hours=24)
                if candidate_expires <= expires:
                    # A forced call in the same second cannot extend a maximum
                    # TTL window and therefore must not manufacture a replay.
                    return {
                        "state": "not-due",
                        "due": False,
                        "active_expires_at": expires_at,
                        "remaining_seconds": remaining_seconds,
                        "next_check_at": next_check_at,
                    }
                refresh_expires_at = _format_bootstrap_time(candidate_expires)
                attempt_id = (
                    "brefresh_"
                    + hashlib.sha256(
                        (
                            str(active["activation_record_id"])
                            + "\0"
                            + issued_at
                            + "\0"
                            + refresh_expires_at
                        ).encode("ascii")
                    ).hexdigest()[:32]
                )
                connection.execute(
                    "INSERT INTO bootstrap_freshness_refresh_attempts("
                    "record_id,source_activation_record_id,source_index_sha256,"
                    "authority_sequence,authority_revision,authority_target_json,"
                    "issued_at,expires_at,forced,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        active["activation_record_id"],
                        active["index_sha256"],
                        authority["sequence"],
                        authority["revision"],
                        _json(authority["target"]),
                        issued_at,
                        refresh_expires_at,
                        int(force),
                        checked_at,
                    ),
                )
                attempt = connection.execute(
                    "SELECT * FROM bootstrap_freshness_refresh_attempts WHERE record_id=?",
                    (attempt_id,),
                ).fetchone()
                created = True
            assert attempt is not None
            lease_expires_at = (observed + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                "INSERT INTO bootstrap_freshness_refresh_lease("
                "singleton,attempt_record_id,owner_id,acquired_at,expires_at) "
                "VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "attempt_record_id=excluded.attempt_record_id,"
                "owner_id=excluded.owner_id,acquired_at=excluded.acquired_at,"
                "expires_at=excluded.expires_at",
                (
                    attempt["record_id"],
                    owner_id,
                    checked_at,
                    lease_expires_at,
                ),
            )
            self._set_bootstrap_refresh_state(
                connection,
                status="refreshing",
                active_expires_at=expires_at,
                last_checked_at=checked_at,
                next_check_at=next_check_at,
                attempt_record_id=str(attempt["record_id"]),
                error_code=None,
            )
            if created:
                request = {
                    "attempt_record_id": attempt["record_id"],
                    "source_activation_record_id": active["activation_record_id"],
                    "issued_at": attempt["issued_at"],
                    "expires_at": attempt["expires_at"],
                    "forced": force,
                }
                self._audit(
                    connection,
                    actor,
                    "bootstrap-freshness.refresh-started",
                    str(attempt["record_id"]),
                    request,
                )
                self._append_bootstrap_outbox(
                    connection,
                    event_type="bootstrap-freshness.refresh-started",
                    record_id=str(attempt["record_id"]),
                    payload=request,
                )
                self._append_bootstrap_refresh_event(
                    connection,
                    attempt_record_id=str(attempt["record_id"]),
                    status="started",
                )
            preparation = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_preparations "
                "WHERE attempt_record_id=?",
                (attempt["record_id"],),
            ).fetchone()
            return {
                "state": "acquired",
                "due": True,
                "attempt_record_id": str(attempt["record_id"]),
                "source_activation_record_id": str(
                    attempt["source_activation_record_id"]
                ),
                "source_index_sha256": str(attempt["source_index_sha256"]),
                "source_index_bytes": bytes(active["index_bytes"]),
                "issued_at": str(attempt["issued_at"]),
                "expires_at": str(attempt["expires_at"]),
                "lease_expires_at": lease_expires_at,
                "candidate_index_bytes": (
                    bytes(preparation["index_bytes"])
                    if preparation is not None
                    else None
                ),
                "next_check_at": next_check_at,
            }

    def acquire_bootstrap_freshness_completion(
        self,
        *,
        owner_id: str,
        lease_seconds: int,
        check_interval_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Recover a crash after exact public readback but before bookkeeping."""

        if (
            not _safe_identity(owner_id)
            or not 5 * 60 <= lease_seconds <= 30 * 60
            or not 5 * 60 <= check_interval_seconds <= 6 * 60 * 60
        ):
            raise ValueError("Bootstrap freshness recovery bounds are invalid")
        observed = _observed_utc(now)
        with self._transaction() as connection:
            active = self._bootstrap_active(connection)
            if active is None:
                return None
            recoverable = connection.execute(
                "SELECT attempts.record_id,readbacks.record_id AS proof_record_id "
                "FROM bootstrap_freshness_refresh_attempts AS attempts "
                "JOIN bootstrap_freshness_refresh_preparations AS preparations "
                "ON preparations.attempt_record_id=attempts.record_id "
                "JOIN bootstrap_index_readbacks AS readbacks "
                "ON readbacks.activation_record_id=? "
                "WHERE preparations.index_sha256=? AND NOT EXISTS("
                "SELECT 1 FROM bootstrap_freshness_refresh_events AS events "
                "WHERE events.attempt_record_id=attempts.record_id "
                "AND events.status='succeeded') "
                "ORDER BY attempts.created_at DESC LIMIT 1",
                (active["activation_record_id"], active["index_sha256"]),
            ).fetchone()
            if recoverable is None:
                return None
            lease = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
            ).fetchone()
            if (
                lease is not None
                and _parse_iso_time(str(lease["expires_at"])) > observed
            ):
                if lease["owner_id"] != owner_id:
                    return {
                        "state": "busy",
                        "attempt_record_id": str(recoverable["record_id"]),
                    }
            elif lease is not None:
                connection.execute(
                    "DELETE FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
                )
            lease_expires_at = (observed + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                "INSERT INTO bootstrap_freshness_refresh_lease("
                "singleton,attempt_record_id,owner_id,acquired_at,expires_at) "
                "VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "attempt_record_id=excluded.attempt_record_id,"
                "owner_id=excluded.owner_id,acquired_at=excluded.acquired_at,"
                "expires_at=excluded.expires_at",
                (
                    recoverable["record_id"],
                    owner_id,
                    observed.isoformat(),
                    lease_expires_at,
                ),
            )
            active_index = _parse_bootstrap_index_bytes(
                bytes(active["index_bytes"]),
                verifier=self.verifier,
                freshness_verifier=self._require_bootstrap_freshness_verifier(),
                now=observed,
                allow_expired_freshness=True,
            )
            self._set_bootstrap_refresh_state(
                connection,
                status="refreshing",
                active_expires_at=str(active_index["freshness"]["expires_at"]),
                last_checked_at=observed.isoformat(),
                next_check_at=(
                    observed + timedelta(seconds=check_interval_seconds)
                ).isoformat(),
                attempt_record_id=str(recoverable["record_id"]),
                error_code=None,
            )
            return {
                "state": "acquired",
                "attempt_record_id": str(recoverable["record_id"]),
                "activation_record_id": str(active["activation_record_id"]),
                "proof_record_id": str(recoverable["proof_record_id"]),
                "lease_expires_at": lease_expires_at,
            }

    def store_bootstrap_freshness_preparation(
        self,
        *,
        attempt_record_id: str,
        owner_id: str,
        index_bytes: bytes,
        signer_key_id: str,
        actor: ControlPrincipal,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = _observed_utc(now)
        index = _parse_bootstrap_index_bytes(
            index_bytes,
            verifier=self.verifier,
            freshness_verifier=self._require_bootstrap_freshness_verifier(),
            now=observed,
        )
        digest = hashlib.sha256(index_bytes).hexdigest()
        with self._transaction() as connection:
            attempt = self._require_bootstrap_refresh_attempt(
                connection, attempt_record_id, owner_id, now=observed
            )
            authority = index["authority"]
            freshness = index["freshness"]
            if (
                int(authority["sequence"]) != int(attempt["authority_sequence"])
                or authority["revision"] != attempt["authority_revision"]
                or _json(authority["target"]) != attempt["authority_target_json"]
                or freshness["issued_at"] != attempt["issued_at"]
                or freshness["expires_at"] != attempt["expires_at"]
                or freshness["signature"]["key_id"] != signer_key_id
            ):
                raise ControlPlaneConflict(
                    "Bootstrap freshness preparation changed immutable authority"
                )
            existing = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_preparations "
                "WHERE attempt_record_id=?",
                (attempt_record_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO bootstrap_freshness_refresh_preparations "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        attempt_record_id,
                        digest,
                        len(index_bytes),
                        index_bytes,
                        signer_key_id,
                        observed.isoformat(),
                    ),
                )
                self._append_bootstrap_refresh_event(
                    connection,
                    attempt_record_id=attempt_record_id,
                    status="prepared",
                )
            elif (
                existing["index_sha256"] != digest
                or bytes(existing["index_bytes"]) != index_bytes
                or existing["signer_key_id"] != signer_key_id
            ):
                raise ControlPlaneConflict(
                    "Bootstrap freshness preparation is not idempotent"
                )
            return {
                "attempt_record_id": attempt_record_id,
                "index_sha256": digest,
                "index_size_bytes": len(index_bytes),
                "index_bytes": index_bytes,
            }

    def renew_bootstrap_freshness_refresh_lease(
        self,
        *,
        attempt_record_id: str,
        owner_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> str:
        if not 5 * 60 <= lease_seconds <= 30 * 60:
            raise ValueError("Bootstrap freshness lease bound is invalid")
        observed = _observed_utc(now)
        expires_at = (observed + timedelta(seconds=lease_seconds)).isoformat()
        with self._transaction() as connection:
            self._require_bootstrap_refresh_attempt(
                connection, attempt_record_id, owner_id, now=observed
            )
            changed = connection.execute(
                "UPDATE bootstrap_freshness_refresh_lease SET expires_at=? "
                "WHERE singleton=1 AND attempt_record_id=? AND owner_id=?",
                (expires_at, attempt_record_id, owner_id),
            ).rowcount
            if changed != 1:
                raise ControlPlaneConflict("Bootstrap freshness lease was lost")
        return expires_at

    def complete_bootstrap_freshness_refresh(
        self,
        *,
        attempt_record_id: str,
        owner_id: str,
        activation_record_id: str,
        proof_record_id: str,
        actor: ControlPrincipal,
        check_interval_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = _observed_utc(now)
        with self._transaction() as connection:
            attempt = self._require_bootstrap_refresh_attempt(
                connection, attempt_record_id, owner_id, now=observed
            )
            active = self._bootstrap_active(connection)
            proof = self._bootstrap_proof_by_record(connection, proof_record_id)
            preparation = connection.execute(
                "SELECT index_sha256 FROM bootstrap_freshness_refresh_preparations "
                "WHERE attempt_record_id=?",
                (attempt_record_id,),
            ).fetchone()
            if (
                active is None
                or active["activation_record_id"] != activation_record_id
                or preparation is None
                or active["index_sha256"] != preparation["index_sha256"]
                or proof is None
                or proof["activation_record_id"] != activation_record_id
                or int(active["authority_sequence"])
                != int(attempt["authority_sequence"])
                or active["authority_revision"] != attempt["authority_revision"]
                or active["authority_target_json"] != attempt["authority_target_json"]
            ):
                raise ControlPlaneConflict(
                    "Bootstrap freshness success proof is inconsistent"
                )
            completed_at = observed.isoformat()
            self._append_bootstrap_refresh_event(
                connection,
                attempt_record_id=attempt_record_id,
                status="succeeded",
                activation_record_id=activation_record_id,
                proof_record_id=proof_record_id,
            )
            self._set_bootstrap_refresh_state(
                connection,
                status="healthy",
                active_expires_at=str(attempt["expires_at"]),
                last_checked_at=completed_at,
                next_check_at=(
                    observed + timedelta(seconds=check_interval_seconds)
                ).isoformat(),
                attempt_record_id=attempt_record_id,
                error_code=None,
                success_at=completed_at,
            )
            connection.execute(
                "DELETE FROM bootstrap_freshness_refresh_lease "
                "WHERE singleton=1 AND attempt_record_id=? AND owner_id=?",
                (attempt_record_id, owner_id),
            )
            request = {
                "attempt_record_id": attempt_record_id,
                "activation_record_id": activation_record_id,
                "proof_record_id": proof_record_id,
            }
            self._audit(
                connection,
                actor,
                "bootstrap-freshness.refresh-succeeded",
                attempt_record_id,
                request,
            )
            self._append_bootstrap_outbox(
                connection,
                event_type="bootstrap-freshness.refresh-succeeded",
                record_id=attempt_record_id,
                payload=request,
            )
        return self.bootstrap_freshness_refresh_status(now=observed)

    def fail_bootstrap_freshness_refresh(
        self,
        *,
        attempt_record_id: str | None,
        owner_id: str | None,
        error_code: str,
        actor: ControlPrincipal,
        check_interval_seconds: int,
        signer_configured: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not _safe_identity(error_code):
            raise ValueError("Bootstrap freshness error code is invalid")
        observed = _observed_utc(now)
        checked_at = observed.isoformat()
        with self._transaction() as connection:
            active = self._bootstrap_active(connection)
            active_expires_at = None
            if active is not None:
                index = _parse_bootstrap_index_bytes(
                    bytes(active["index_bytes"]),
                    verifier=self.verifier,
                    freshness_verifier=self._require_bootstrap_freshness_verifier(),
                    now=observed,
                    allow_expired_freshness=True,
                )
                active_expires_at = str(index["freshness"]["expires_at"])
            if attempt_record_id is not None:
                attempt = connection.execute(
                    "SELECT record_id FROM bootstrap_freshness_refresh_attempts "
                    "WHERE record_id=?",
                    (attempt_record_id,),
                ).fetchone()
                if attempt is None:
                    raise ControlPlaneNotFound(
                        "Bootstrap freshness attempt does not exist"
                    )
            status = "degraded" if signer_configured else "unconfigured"
            event_status = "failed" if signer_configured else "unconfigured"
            self._append_bootstrap_refresh_event(
                connection,
                attempt_record_id=attempt_record_id,
                status=event_status,
                error_code=error_code,
            )
            self._set_bootstrap_refresh_state(
                connection,
                status=status,
                active_expires_at=active_expires_at,
                last_checked_at=checked_at,
                next_check_at=(
                    observed + timedelta(seconds=check_interval_seconds)
                ).isoformat(),
                attempt_record_id=attempt_record_id,
                error_code=error_code,
                failure_at=checked_at,
            )
            if attempt_record_id is not None and owner_id is not None:
                connection.execute(
                    "DELETE FROM bootstrap_freshness_refresh_lease "
                    "WHERE singleton=1 AND attempt_record_id=? AND owner_id=?",
                    (attempt_record_id, owner_id),
                )
            record_id = attempt_record_id or "bootstrap-freshness"
            request = {
                "attempt_record_id": attempt_record_id,
                "error_code": error_code,
            }
            self._audit(
                connection,
                actor,
                "bootstrap-freshness.refresh-failed",
                record_id,
                request,
            )
            self._append_bootstrap_outbox(
                connection,
                event_type="bootstrap-freshness.refresh-failed",
                record_id=record_id,
                payload=request,
            )
        return self.bootstrap_freshness_refresh_status(now=observed)

    def bootstrap_freshness_refresh_status(
        self, *, now: datetime | None = None
    ) -> dict[str, Any]:
        observed = _observed_utc(now)
        with self._read_transaction() as connection:
            state = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_state WHERE singleton=1"
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
            ).fetchone()
            active = self._bootstrap_active(connection)
            active_expires_at = (
                state["active_expires_at"] if state is not None else None
            )
            active_authority_sha256 = None
            if active is not None:
                index = _parse_bootstrap_index_bytes(
                    bytes(active["index_bytes"]),
                    verifier=self.verifier,
                    freshness_verifier=self._require_bootstrap_freshness_verifier(),
                    now=observed,
                    allow_expired_freshness=True,
                )
                active_expires_at = str(index["freshness"]["expires_at"])
                active_authority_sha256 = str(index["freshness"]["authority_sha256"])
            remaining_seconds = (
                int(
                    (
                        _parse_bootstrap_time(active_expires_at) - observed
                    ).total_seconds()
                )
                if active_expires_at is not None
                else None
            )
            return {
                "schema_version": 1,
                "status": str(state["status"]) if state is not None else "idle",
                "active_expires_at": active_expires_at,
                "active_authority_sha256": active_authority_sha256,
                "remaining_seconds": remaining_seconds,
                "last_checked_at": state["last_checked_at"] if state else None,
                "next_check_at": state["next_check_at"] if state else None,
                "last_attempt_record_id": (
                    state["last_attempt_record_id"] if state else None
                ),
                "last_success_at": state["last_success_at"] if state else None,
                "last_failure_at": state["last_failure_at"] if state else None,
                "last_error_code": state["last_error_code"] if state else None,
                "lease_owner_id": lease["owner_id"] if lease else None,
                "lease_expires_at": lease["expires_at"] if lease else None,
                "updated_at": state["updated_at"] if state else None,
            }

    def replay_bootstrap_freshness_manual_refresh(
        self,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        request = {"force": True, "scope": "same-authority-freshness"}
        with self._read_transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-freshness.manual-refresh",
                request,
            )
            return dict(replay) if replay is not None else None

    def remember_bootstrap_freshness_manual_refresh(
        self,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        request = {"force": True, "scope": "same-authority-freshness"}
        with self._transaction() as connection:
            replay = self._replay(
                connection,
                actor,
                client_request_id,
                "bootstrap-freshness.manual-refresh",
                request,
            )
            if replay is not None:
                return dict(replay)
            self._remember(
                connection,
                actor,
                client_request_id,
                "bootstrap-freshness.manual-refresh",
                request,
                response,
            )
            return dict(response)

    def publish(
        self,
        release_id: str,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> CandidateProjection:
        request = {"release_id": release_id}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "release.publish", request
            )
            if replay is not None:
                return CandidateProjection.model_validate(replay)
            candidate = self._candidate(connection, release_id)
            if candidate.status not in {"candidate", "published"}:
                raise ControlPlaneConflict("withdrawn release cannot be published")
            if candidate.missing_gates or any(
                value != "passed" for value in candidate.gates.values()
            ):
                raise ReleaseGateError(
                    "all required release gates must pass before publication"
                )
            release = self._require_release(connection, release_id)
            self._require_current_release_bootstrap_gate(connection, release)
            # Publication is monotonic.  A later idempotent administrator
            # request must not rewrite the original publication time, which is
            # used by audit and rollout ordering.
            if candidate.status == "candidate":
                connection.execute(
                    "UPDATE control_releases SET status='published', published_at=? "
                    "WHERE release_id=? AND status='candidate'",
                    (_now(), release_id),
                )
            result = self._candidate(connection, release_id)
            self._audit(connection, actor, "release.publish", release_id, request)
            self._remember(
                connection,
                actor,
                client_request_id,
                "release.publish",
                request,
                result.model_dump(),
            )
            return result

    def create_rollout(
        self,
        release_id: str,
        *,
        percentage: int,
        organizations: list[str],
        accounts: list[str],
        minimum_compatible_version: str | None,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> RolloutProjection:
        if not 1 <= percentage <= 100:
            raise ValueError("rollout percentage must be between one and 100")
        for value in (*organizations, *accounts):
            if not value or len(value) > 256:
                raise ValueError("rollout target identity is invalid")
        if minimum_compatible_version is not None:
            _semver_key(minimum_compatible_version)
        request = {
            "release_id": release_id,
            "percentage": percentage,
            "organizations": sorted(set(organizations)),
            "accounts": sorted(set(accounts)),
            "minimum_compatible_version": minimum_compatible_version,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "rollout.create", request
            )
            if replay is not None:
                return RolloutProjection.model_validate(replay)
            release = self._require_release(connection, release_id)
            if release["status"] not in {"candidate", "published"}:
                raise ControlPlaneConflict(
                    "only a candidate or published release can have a draft rollout"
                )
            rollout_id = "rollout_" + uuid.uuid4().hex
            now = _now()
            connection.execute(
                "INSERT INTO control_rollouts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rollout_id,
                    release_id,
                    release["channel"],
                    "draft",
                    percentage,
                    _json(request["organizations"]),
                    _json(request["accounts"]),
                    minimum_compatible_version,
                    now,
                    now,
                ),
            )
            result = self._rollout(connection, rollout_id)
            self._audit(connection, actor, "rollout.create", rollout_id, request)
            self._remember(
                connection,
                actor,
                client_request_id,
                "rollout.create",
                request,
                result.model_dump(),
            )
            return result

    def create_rollback(
        self,
        source_release_id: str,
        target_release_id: str,
        *,
        percentage: int,
        organizations: list[str],
        accounts: list[str],
        authorization_ttl_seconds: int,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> RollbackProjection:
        """Create a draft rollback only to a previously deployed known-good release."""

        if not 1 <= percentage <= 100:
            raise ValueError("rollback percentage must be between one and 100")
        if (
            isinstance(authorization_ttl_seconds, bool)
            or not 60 <= authorization_ttl_seconds <= 900
        ):
            raise ValueError("rollback authorization TTL is invalid")
        for value in (*organizations, *accounts):
            if not value or len(value) > 256:
                raise ValueError("rollback target identity is invalid")
        request = {
            "source_release_id": source_release_id,
            "target_release_id": target_release_id,
            "percentage": percentage,
            "organizations": sorted(set(organizations)),
            "accounts": sorted(set(accounts)),
            "authorization_ttl_seconds": authorization_ttl_seconds,
        }
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "rollback.create", request
            )
            if replay is not None:
                return RollbackProjection.model_validate(replay)
            source = self._require_release(connection, source_release_id)
            target = self._require_release(connection, target_release_id)
            if source["status"] != "published" or target["status"] != "published":
                raise ControlPlaneConflict(
                    "rollback source and target must both be published"
                )
            if source["channel"] != target["channel"]:
                raise ControlPlaneConflict(
                    "rollback source and target channels must match"
                )
            if _compare_semver(str(target["version"]), str(source["version"])) >= 0:
                raise ControlPlaneConflict(
                    "rollback target must be older than its source release"
                )
            source_manifest = self._verified_manifest(source)
            target_manifest = self._verified_manifest(target)
            if _core_target_matrix(source_manifest) != _core_target_matrix(
                target_manifest
            ):
                raise ControlPlaneConflict(
                    "rollback target does not support the source target matrix"
                )
            known_good = connection.execute(
                "SELECT 1 FROM control_rollouts AS rollouts "
                "LEFT JOIN control_release_rollbacks AS rollbacks "
                "ON rollbacks.rollback_id=rollouts.rollout_id "
                "WHERE rollouts.release_id=? AND rollbacks.rollback_id IS NULL "
                "AND rollouts.status IN ('active','paused','halted','completed') LIMIT 1",
                (target_release_id,),
            ).fetchone()
            if known_good is None:
                raise ControlPlaneConflict(
                    "rollback target has no prior known-good rollout"
                )
            rollback_id = "rollback_" + uuid.uuid4().hex
            now = _now()
            connection.execute(
                "INSERT INTO control_rollouts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rollback_id,
                    target_release_id,
                    target["channel"],
                    "draft",
                    percentage,
                    _json(request["organizations"]),
                    _json(request["accounts"]),
                    None,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO control_release_rollbacks VALUES (?,?,?,?,?)",
                (
                    rollback_id,
                    source_release_id,
                    target_release_id,
                    authorization_ttl_seconds,
                    now,
                ),
            )
            result = self._rollback(connection, rollback_id)
            self._audit(connection, actor, "rollback.create", rollback_id, request)
            self._remember(
                connection,
                actor,
                client_request_id,
                "rollback.create",
                request,
                result.model_dump(),
            )
            return result

    def rollback_action(
        self,
        rollback_id: str,
        action: str,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> RollbackProjection:
        targets = {"activate": "active", "pause": "paused", "halt": "halted"}
        if action not in targets:
            raise ValueError("rollback action is invalid")
        request = {"rollback_id": rollback_id, "action": action}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "rollback.action", request
            )
            if replay is not None:
                return RollbackProjection.model_validate(replay)
            current = self._rollback(connection, rollback_id)
            target = self._require_release(connection, current.target_release_id)
            source = self._require_release(connection, current.source_release_id)
            if action == "activate":
                if target["status"] != "published" or source["status"] != "published":
                    raise ControlPlaneConflict(
                        "rollback activation requires published source and target releases"
                    )
                if _compare_semver(str(target["version"]), str(source["version"])) >= 0:
                    raise ControlPlaneConflict(
                        "rollback target is no longer older than its source"
                    )
            if action == "activate" and self._channel_killed(
                connection, ReleaseChannel(current.channel)
            ):
                raise ControlPlaneConflict(
                    "channel kill switch must be cleared before activating a rollback"
                )
            allowed = {
                "activate": {"draft", "paused", "active"},
                "pause": {"active", "paused"},
                "halt": {"draft", "active", "paused", "halted"},
            }
            if current.status not in allowed[action]:
                raise ControlPlaneConflict("rollback state does not allow this action")
            connection.execute(
                "UPDATE control_rollouts SET status=?,updated_at=? WHERE rollout_id=?",
                (targets[action], _now(), rollback_id),
            )
            result = self._rollback(connection, rollback_id)
            self._audit(
                connection, actor, f"rollback.{action}", rollback_id, request
            )
            self._append_update_signal(
                connection,
                signal_type={
                    "activate": "rollout.activated",
                    "pause": "rollout.paused",
                    "halt": "rollout.halted",
                }[action],
                channel=ReleaseChannel(result.channel),
                rollout_id=result.rollback_id,
                release_id=result.target_release_id,
                dedupe_key=self._signal_dedupe_key(
                    actor, client_request_id, f"rollback.{action}", result.rollback_id
                ),
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "rollback.action",
                request,
                result.model_dump(),
            )
            return result

    def rollout_action(
        self,
        rollout_id: str,
        action: str,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> RolloutProjection:
        targets = {"activate": "active", "pause": "paused", "halt": "halted"}
        if action not in targets:
            raise ValueError("rollout action is invalid")
        request = {"rollout_id": rollout_id, "action": action}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "rollout.action", request
            )
            if replay is not None:
                return RolloutProjection.model_validate(replay)
            current = self._rollout(connection, rollout_id)
            if connection.execute(
                "SELECT 1 FROM control_release_rollbacks WHERE rollback_id=?",
                (rollout_id,),
            ).fetchone() is not None:
                raise ControlPlaneConflict(
                    "rollback state must use the rollback action API"
                )
            release = self._require_release(connection, current.release_id)
            if action == "activate":
                if release["status"] != "published":
                    raise ControlPlaneConflict(
                        "rollout activation requires a published release"
                    )
                self._require_current_release_bootstrap_gate(connection, release)
            if action == "activate" and self._channel_killed(
                connection, ReleaseChannel(current.channel)
            ):
                raise ControlPlaneConflict(
                    "channel kill switch must be cleared before activating a rollout"
                )
            allowed = {
                "activate": {"draft", "paused", "active"},
                "pause": {"active", "paused"},
                "halt": {"draft", "active", "paused", "halted"},
            }
            if current.status not in allowed[action]:
                raise ControlPlaneConflict("rollout state does not allow this action")
            connection.execute(
                "UPDATE control_rollouts SET status=?,updated_at=? WHERE rollout_id=?",
                (targets[action], _now(), rollout_id),
            )
            result = self._rollout(connection, rollout_id)
            self._audit(connection, actor, f"rollout.{action}", rollout_id, request)
            self._append_update_signal(
                connection,
                signal_type={
                    "activate": "rollout.activated",
                    "pause": "rollout.paused",
                    "halt": "rollout.halted",
                }[action],
                channel=ReleaseChannel(result.channel),
                rollout_id=result.rollout_id,
                release_id=result.release_id,
                dedupe_key=self._signal_dedupe_key(
                    actor, client_request_id, f"rollout.{action}", result.rollout_id
                ),
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "rollout.action",
                request,
                result.model_dump(),
            )
            return result

    def latest_for_client(
        self,
        principal: ControlPrincipal,
        *,
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str = "idle",
    ) -> ReleaseManifest | None:
        decision = self.latest_decision_for_client(
            principal,
            channel=channel,
            platform=platform,
            architecture=architecture,
            current_version=current_version,
            update_state=update_state,
        )
        return None if decision is None else decision.manifest

    def latest_decision_for_client(
        self,
        principal: ControlPrincipal,
        *,
        channel: ReleaseChannel,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str = "idle",
        current_release_id: str | None = None,
        current_build_digest: str | None = None,
    ) -> ClientReleaseDecision | None:
        _semver_key(current_version)
        if (current_release_id is None) != (current_build_digest is None):
            raise ValueError("client current release identity is incomplete")
        if (
            not isinstance(channel, ReleaseChannel)
            or not _safe_identity(platform)
            or not _safe_identity(architecture)
            or update_state not in _CLIENT_UPDATE_STATES
            or not _safe_identity(principal.client_id)
            or not _safe_identity(principal.account_id)
            or (
                principal.organization_id is not None
                and not _safe_identity(principal.organization_id)
            )
            or (
                current_release_id is not None
                and (
                    not _safe_identity(current_release_id)
                    or _SHA256.fullmatch(str(current_build_digest)) is None
                )
            )
        ):
            raise ValueError("client update identity is invalid")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO control_clients VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET account_id=excluded.account_id,"
                "organization_id=excluded.organization_id,platform=excluded.platform,"
                "architecture=excluded.architecture,current_version=excluded.current_version,"
                "update_state=excluded.update_state,last_seen_at=excluded.last_seen_at",
                (
                    principal.client_id,
                    principal.account_id,
                    principal.organization_id,
                    platform,
                    architecture,
                    current_version,
                    update_state,
                    _now(),
                ),
            )
            if self._channel_killed(connection, channel):
                return None
            rows = connection.execute(
                "SELECT rollouts.*, releases.manifest_json, releases.manifest_sha256, "
                "rollbacks.rollback_id AS rollback_record_id,"
                "rollbacks.source_release_id,"
                "rollbacks.authorization_ttl_seconds,"
                "source.version AS source_version,"
                "source.build_digest AS source_build_digest,"
                "source.channel AS source_channel,"
                "source.manifest_json AS source_manifest_json,"
                "source.manifest_sha256 AS source_manifest_sha256 "
                "FROM control_rollouts AS rollouts JOIN control_releases AS releases "
                "ON releases.release_id=rollouts.release_id "
                "LEFT JOIN control_release_rollbacks AS rollbacks "
                "ON rollbacks.rollback_id=rollouts.rollout_id "
                "LEFT JOIN control_releases AS source "
                "ON source.release_id=rollbacks.source_release_id "
                "WHERE rollouts.channel=? AND rollouts.status='active' "
                "AND releases.status='published' ORDER BY rollouts.created_at DESC",
                (channel.value,),
            ).fetchall()
            eligible: list[ClientReleaseDecision] = []
            rollback_eligible: list[ClientReleaseDecision] = []
            for row in rows:
                organizations = _list(row["target_organizations_json"])
                accounts = _list(row["target_accounts_json"])
                if organizations and principal.organization_id not in organizations:
                    continue
                if accounts and principal.account_id not in accounts:
                    continue
                minimum = row["minimum_compatible_version"]
                mandatory = (
                    minimum is not None
                    and _compare_semver(current_version, minimum) < 0
                )
                bucket = (
                    int.from_bytes(
                        hashlib.sha256(
                            f"{row['rollout_id']}\0{principal.client_id}".encode()
                        ).digest()[:4],
                        "big",
                    )
                    % 100
                )
                if not mandatory and bucket >= int(row["percentage"]):
                    continue
                manifest_json = str(row["manifest_json"])
                if _sha(manifest_json) != row["manifest_sha256"]:
                    raise ControlPlaneError("stored release manifest digest is invalid")
                manifest = ReleaseManifest.from_json(manifest_json)
                verify_manifest_signature(manifest, self.verifier)
                artifact = next(
                    (
                        item
                        for item in manifest.artifacts
                        if item.platform == platform
                        and item.architecture == architecture
                    ),
                    None,
                )
                if artifact is None:
                    continue
                verify_artifact_signature(manifest, artifact, self.verifier)
                if row["rollback_record_id"] is not None:
                    if (
                        current_release_id is None
                        or row["source_release_id"] != current_release_id
                        or row["source_build_digest"] != current_build_digest
                        or row["source_version"] != current_version
                        or row["source_channel"] != channel.value
                        or _compare_semver(manifest.version, current_version) >= 0
                    ):
                        continue
                    source_json = row["source_manifest_json"]
                    if (
                        not isinstance(source_json, str)
                        or _sha(source_json) != row["source_manifest_sha256"]
                    ):
                        raise ControlPlaneError(
                            "stored rollback source manifest digest is invalid"
                        )
                    source_manifest = ReleaseManifest.from_json(source_json)
                    verify_manifest_signature(source_manifest, self.verifier)
                    source_artifact = source_manifest.artifact(
                        f"core-{platform}-{architecture}"
                    )
                    verify_artifact_signature(
                        source_manifest, source_artifact, self.verifier
                    )
                    if (
                        source_manifest.release_id != current_release_id
                        or source_manifest.build_digest != current_build_digest
                        or source_manifest.version != current_version
                    ):
                        raise ControlPlaneError(
                            "stored rollback source identity is invalid"
                        )
                    rollback_eligible.append(
                        ClientReleaseDecision(
                            manifest=manifest,
                            rollback_id=str(row["rollback_record_id"]),
                            source_manifest=source_manifest,
                            authorization_ttl_seconds=int(
                                row["authorization_ttl_seconds"]
                            ),
                        )
                    )
                    continue
                if _compare_semver(manifest.version, current_version) > 0:
                    eligible.append(ClientReleaseDecision(manifest=manifest))
            if rollback_eligible:
                # Active rollback authority intentionally outranks a normal
                # upgrade while the client still runs its exact source build.
                return rollback_eligible[0]
            best: ClientReleaseDecision | None = None
            for decision in eligible:
                if best is None or _compare_semver(
                    decision.manifest.version, best.manifest.version
                ) > 0:
                    best = decision
            return best

    def distribution(self) -> DistributionProjection:
        with self._read_transaction() as connection:
            return self._distribution(connection)

    def admin_resume_facts(self) -> _AdminResumeFactsRecord:
        """Load the complete administrator resume view from one WAL snapshot.

        Latest identifiers are selected explicitly by persisted business time,
        then by the first append-only creation audit sequence and stable ID.
        The returned list order is never used to infer either identifier.
        """

        with self._read_transaction() as connection:
            latest_candidate_row = connection.execute(
                _RESUME_RELEASE_ORDER_SQL + " LIMIT 1"
            ).fetchone()
            candidate_rows = connection.execute(
                _RESUME_RELEASE_ORDER_SQL + " LIMIT ?",
                (_ADMIN_RESUME_CANDIDATE_LIMIT,),
            ).fetchall()
            latest_rollout_row = connection.execute(
                _RESUME_ROLLOUT_ORDER_SQL + " LIMIT 1"
            ).fetchone()
            rollout_rows = connection.execute(
                _RESUME_ROLLOUT_ORDER_SQL + " LIMIT ?",
                (_ADMIN_RESUME_ROLLOUT_LIMIT,),
            ).fetchall()

            candidates = tuple(
                self._candidate(connection, row["release_id"]) for row in candidate_rows
            )
            rollouts = tuple(
                self._rollout(connection, row["rollout_id"]) for row in rollout_rows
            )

            channel_rows = connection.execute(
                "SELECT channel,kill_switch_active FROM control_channel_state "
                "ORDER BY channel"
            ).fetchall()
            channels = {
                row["channel"]: bool(row["kill_switch_active"]) for row in channel_rows
            }
            if set(channels) != {
                ReleaseChannel.CANARY.value,
                ReleaseChannel.STABLE.value,
            }:
                raise ControlPlaneError("Control Plane channel state is incomplete")
            halted_by_channel: dict[str, list[str]] = {
                ReleaseChannel.CANARY.value: [],
                ReleaseChannel.STABLE.value: [],
            }
            for row in connection.execute(
                "SELECT channel,rollout_id FROM control_rollouts WHERE status='halted' "
                "ORDER BY updated_at DESC,created_at DESC,rollout_id DESC"
            ):
                if row["channel"] not in halted_by_channel:
                    raise ControlPlaneError("stored rollout channel is invalid")
                halted_by_channel[row["channel"]].append(row["rollout_id"])
            channel_kill_switches = tuple(
                KillSwitchProjection(
                    channel=channel.value,
                    halted_rollout_ids=halted_by_channel[channel.value],
                    kill_switch_active=channels[channel.value],
                )
                for channel in (ReleaseChannel.CANARY, ReleaseChannel.STABLE)
            )

            facts: _AdminResumeFactsRecord = {
                "schema_version": 1,
                "candidates": candidates,
                "latest_candidate_id": (
                    None
                    if latest_candidate_row is None
                    else latest_candidate_row["release_id"]
                ),
                "rollouts": rollouts,
                "latest_rollout_id": (
                    None
                    if latest_rollout_row is None
                    else latest_rollout_row["rollout_id"]
                ),
                "channel_kill_switches": channel_kill_switches,
                "distribution": self._distribution(connection),
                "captured_at": datetime.now(UTC),
            }
        return facts

    @staticmethod
    def _distribution(connection) -> DistributionProjection:
        rows = connection.execute(
            "SELECT current_version,update_state,COUNT(*) AS count "
            "FROM control_clients GROUP BY current_version,update_state"
        ).fetchall()
        versions: dict[str, int] = {}
        states: dict[str, int] = {}
        total = 0
        for row in rows:
            count = int(row["count"])
            total += count
            versions[row["current_version"]] = (
                versions.get(row["current_version"], 0) + count
            )
            states[row["update_state"]] = states.get(row["update_state"], 0) + count
        return DistributionProjection(
            total_clients=total,
            versions=versions,
            update_states=states,
        )

    def kill_channel(
        self,
        channel: ReleaseChannel,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> KillSwitchProjection:
        request = {"channel": channel.value}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "channel.kill", request
            )
            if replay is not None:
                return KillSwitchProjection.model_validate(replay)
            rows = connection.execute(
                "SELECT rollout_id FROM control_rollouts WHERE channel=? "
                "AND status IN ('draft','active','paused') ORDER BY created_at,rollout_id",
                (channel.value,),
            ).fetchall()
            rollout_ids = [row["rollout_id"] for row in rows]
            connection.execute(
                "UPDATE control_rollouts SET status='halted',updated_at=? WHERE channel=? "
                "AND status IN ('draft','active','paused')",
                (_now(), channel.value),
            )
            connection.execute(
                "UPDATE control_channel_state SET kill_switch_active=1,updated_at=? "
                "WHERE channel=?",
                (_now(), channel.value),
            )
            result = KillSwitchProjection(
                channel=channel.value,
                halted_rollout_ids=rollout_ids,
                kill_switch_active=True,
            )
            self._audit(connection, actor, "channel.kill", channel.value, request)
            for row in rows:
                rollout = self._rollout(connection, row["rollout_id"])
                self._append_update_signal(
                    connection,
                    signal_type="rollout.halted",
                    channel=channel,
                    rollout_id=rollout.rollout_id,
                    release_id=rollout.release_id,
                    dedupe_key=self._signal_dedupe_key(
                        actor,
                        client_request_id,
                        "channel.kill.rollout",
                        rollout.rollout_id,
                    ),
                )
            self._append_update_signal(
                connection,
                signal_type="channel.killed",
                channel=channel,
                rollout_id=None,
                release_id=None,
                dedupe_key=self._signal_dedupe_key(
                    actor, client_request_id, "channel.kill", channel.value
                ),
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "channel.kill",
                request,
                result.model_dump(),
            )
            return result

    def clear_channel_kill(
        self,
        channel: ReleaseChannel,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> KillSwitchProjection:
        request = {"channel": channel.value, "kill_switch_active": False}
        with self._transaction() as connection:
            replay = self._replay(
                connection, actor, client_request_id, "channel.kill.clear", request
            )
            if replay is not None:
                return KillSwitchProjection.model_validate(replay)
            connection.execute(
                "UPDATE control_channel_state SET kill_switch_active=0,updated_at=? "
                "WHERE channel=?",
                (_now(), channel.value),
            )
            result = KillSwitchProjection(
                channel=channel.value,
                halted_rollout_ids=[],
                kill_switch_active=False,
            )
            self._audit(connection, actor, "channel.kill.clear", channel.value, request)
            self._append_update_signal(
                connection,
                signal_type="channel.kill_cleared",
                channel=channel,
                rollout_id=None,
                release_id=None,
                dedupe_key=self._signal_dedupe_key(
                    actor, client_request_id, "channel.kill.clear", channel.value
                ),
            )
            self._remember(
                connection,
                actor,
                client_request_id,
                "channel.kill.clear",
                request,
                result.model_dump(),
            )
            return result

    def read_update_signals(
        self,
        *,
        after_sequence: int,
        limit: int = 128,
    ) -> ControlUpdateSignalBatch:
        """Read one bounded, monotonic slice of the durable signal log."""

        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool):
            raise ValueError("update signal cursor is invalid")
        if after_sequence < 0:
            raise ValueError("update signal cursor is invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 256
        ):
            raise ValueError("update signal batch limit is invalid")
        with self._read_transaction() as connection:
            bounds = connection.execute(
                "SELECT MIN(sequence) AS floor,MAX(sequence) AS latest "
                "FROM control_update_signals"
            ).fetchone()
            floor = 0 if bounds["floor"] is None else int(bounds["floor"])
            latest = 0 if bounds["latest"] is None else int(bounds["latest"])
            if after_sequence > latest:
                raise ControlPlaneError(
                    "update signal cursor is ahead of the durable log"
                )
            rows = connection.execute(
                "SELECT sequence,event_id,signal_type,channel,rollout_id,release_id,"
                "created_at FROM control_update_signals WHERE sequence>? "
                "ORDER BY sequence LIMIT ?",
                (after_sequence, limit),
            ).fetchall()
            return ControlUpdateSignalBatch(
                after_sequence=after_sequence,
                retained_floor_sequence=floor,
                latest_sequence=latest,
                gap_detected=bool(floor and after_sequence + 1 < floor),
                signals=[self._update_signal(row) for row in rows],
            )

    def update_signal_consumer_cursor(self, consumer_id: str) -> int:
        if not _safe_identity(consumer_id):
            raise ValueError("update signal consumer identity is invalid")
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM control_update_signal_consumers "
                "WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            return 0 if row is None else int(row["last_sequence"])

    def acknowledge_update_signals(self, consumer_id: str, sequence: int) -> int:
        """Advance one instance cursor; retries and regressions are harmless."""

        if not _safe_identity(consumer_id):
            raise ValueError("update signal consumer identity is invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("update signal acknowledgement is invalid")
        with self._transaction() as connection:
            latest_row = connection.execute(
                "SELECT MAX(sequence) AS latest FROM control_update_signals"
            ).fetchone()
            latest = 0 if latest_row["latest"] is None else int(latest_row["latest"])
            if sequence > latest:
                raise ControlPlaneConflict(
                    "update signal acknowledgement is ahead of the durable log"
                )
            existing = connection.execute(
                "SELECT last_sequence FROM control_update_signal_consumers "
                "WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO control_update_signal_consumers VALUES (?,?,?)",
                    (consumer_id, sequence, _now()),
                )
                return sequence
            current = int(existing["last_sequence"])
            if sequence > current:
                connection.execute(
                    "UPDATE control_update_signal_consumers "
                    "SET last_sequence=?,updated_at=? WHERE consumer_id=?",
                    (sequence, _now(), consumer_id),
                )
                return sequence
            return current

    def prune_update_signals(
        self,
        *,
        before: datetime,
        retain_latest: int = 1024,
    ) -> int:
        """Apply bounded time retention without ever reusing a sequence."""

        if (
            not isinstance(before, datetime)
            or before.tzinfo is None
            or before.utcoffset() is None
        ):
            raise ValueError("update signal retention cutoff must be timezone-aware")
        if (
            not isinstance(retain_latest, int)
            or isinstance(retain_latest, bool)
            or not 1 <= retain_latest <= 100_000
        ):
            raise ValueError("update signal retention floor is invalid")
        cutoff = before.astimezone(UTC).isoformat()
        with self._transaction() as connection:
            threshold = connection.execute(
                "SELECT sequence FROM control_update_signals "
                "ORDER BY sequence DESC LIMIT 1 OFFSET ?",
                (retain_latest - 1,),
            ).fetchone()
            deleted = 0
            if threshold is not None:
                cursor = connection.execute(
                    "DELETE FROM control_update_signals "
                    "WHERE sequence<? AND created_at<?",
                    (int(threshold["sequence"]), cutoff),
                )
                deleted = int(cursor.rowcount)
            # Random/default process identities cannot grow the cursor table
            # forever. A quiet live poller keeps its cursor in memory and will
            # recreate the row monotonically on its next acknowledgement.
            connection.execute(
                "DELETE FROM control_update_signal_consumers WHERE updated_at<?",
                (cutoff,),
            )
            return deleted

    def rollout_signal_for_request(
        self,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
        rollout_id: str,
        action: str,
    ) -> ControlUpdateSignal:
        """Resolve the exact committed signal for local low-latency delivery."""

        signal_type = {
            "activate": "rollout.activated",
            "pause": "rollout.paused",
            "halt": "rollout.halted",
        }.get(action)
        if signal_type is None:
            raise ValueError("rollout action is invalid")
        dedupe_key = self._signal_dedupe_key(
            actor, client_request_id, f"rollout.{action}", rollout_id
        )
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT sequence,event_id,signal_type,channel,rollout_id,release_id,"
                "created_at FROM control_update_signals WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("committed rollout signal does not exist")
            return self._update_signal(row)

    def rollback_signal_for_request(
        self,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
        rollback_id: str,
        action: str,
    ) -> ControlUpdateSignal:
        signal_type = {
            "activate": "rollout.activated",
            "pause": "rollout.paused",
            "halt": "rollout.halted",
        }.get(action)
        if signal_type is None:
            raise ValueError("rollback action is invalid")
        dedupe_key = self._signal_dedupe_key(
            actor, client_request_id, f"rollback.{action}", rollback_id
        )
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT sequence,event_id,signal_type,channel,rollout_id,release_id,"
                "created_at FROM control_update_signals WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("committed rollback signal does not exist")
            return self._update_signal(row)

    def require_update_signal(self, signal: ControlUpdateSignal) -> ControlUpdateSignal:
        """Verify that a fan-out fact exactly matches one committed log row."""

        if not isinstance(signal, ControlUpdateSignal):
            raise TypeError("update signal must use the durable signal model")
        with self._read_transaction() as connection:
            return self._require_update_signal_connection(connection, signal)

    @staticmethod
    def validate_update_hint_client(client: UpdateHintClient) -> None:
        """Validate one client before any batch opens a database transaction."""

        if not isinstance(client, UpdateHintClient):
            raise TypeError("update hint client must use the bounded client model")
        if not isinstance(client.principal, ControlPrincipal):
            raise TypeError("update hint principal is invalid")
        _semver_key(client.current_version)
        if (client.current_release_id is None) != (
            client.current_build_digest is None
        ):
            raise ValueError("client current release identity is incomplete")
        if (
            not isinstance(client.channel, ReleaseChannel)
            or not _safe_identity(client.platform)
            or not _safe_identity(client.architecture)
            or client.update_state not in _CLIENT_UPDATE_STATES
            or not _safe_identity(client.principal.client_id)
            or not _safe_identity(client.principal.account_id)
            or (
                client.principal.organization_id is not None
                and not _safe_identity(client.principal.organization_id)
            )
            or (
                client.current_release_id is not None
                and (
                    not _safe_identity(client.current_release_id)
                    or _SHA256.fullmatch(str(client.current_build_digest)) is None
                )
            )
        ):
            raise ValueError("client update identity is invalid")

    @staticmethod
    def _require_update_signal_connection(
        connection: sqlite3.Connection,
        signal: ControlUpdateSignal,
    ) -> ControlUpdateSignal:
        row = connection.execute(
            "SELECT sequence,event_id,signal_type,channel,rollout_id,release_id,"
            "created_at FROM control_update_signals WHERE sequence=?",
            (signal.sequence,),
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("committed update signal does not exist")
        committed = ControlPlaneRepository._update_signal(row)
        if committed != signal:
            raise ControlPlaneConflict(
                "update signal does not match its committed durable fact"
            )
        return committed

    @staticmethod
    def _hint_rollout(row: sqlite3.Row) -> _HintRollout:
        return _HintRollout(
            rollout_id=str(row["rollout_id"]),
            release_id=str(row["release_id"]),
            channel=str(row["channel"]),
            percentage=int(row["percentage"]),
            organizations=frozenset(_list(row["target_organizations_json"])),
            accounts=frozenset(_list(row["target_accounts_json"])),
            minimum_compatible_version=row["minimum_compatible_version"],
            manifest_json=str(row["manifest_json"]),
            manifest_sha256=str(row["manifest_sha256"]),
            release_version=str(row["release_version"]),
            release_build_digest=str(row["release_build_digest"]),
            rollback_id=row["rollback_record_id"],
            source_release_id=row["source_release_id"],
            source_version=row["source_version"],
            source_build_digest=row["source_build_digest"],
        )

    def _hint_rollouts_for_signal(
        self,
        connection: sqlite3.Connection,
        signal: ControlUpdateSignal,
    ) -> tuple[_HintRollout, ...]:
        columns = (
            "rollouts.*,releases.manifest_json,releases.manifest_sha256,"
            "releases.version AS release_version,"
            "releases.build_digest AS release_build_digest,"
            "releases.status AS release_status,"
            "rollbacks.rollback_id AS rollback_record_id,"
            "rollbacks.source_release_id,"
            "source.version AS source_version,"
            "source.build_digest AS source_build_digest "
        )
        join = (
            "FROM control_rollouts AS rollouts JOIN control_releases AS releases "
            "ON releases.release_id=rollouts.release_id "
            "LEFT JOIN control_release_rollbacks AS rollbacks "
            "ON rollbacks.rollback_id=rollouts.rollout_id "
            "LEFT JOIN control_releases AS source "
            "ON source.release_id=rollbacks.source_release_id "
        )
        if signal.signal_type == "channel.killed":
            return ()
        if signal.signal_type == "rollout.activated":
            if signal.rollout_id is None or signal.release_id is None:
                raise ControlPlaneError("rollout update signal identity is incomplete")
            rows = connection.execute(
                "SELECT " + columns + join + "WHERE rollouts.channel=? AND "
                "(rollouts.status='active' OR rollouts.rollout_id=?) "
                "ORDER BY rollouts.created_at DESC,rollouts.rollout_id DESC",
                (signal.channel, signal.rollout_id),
            ).fetchall()
            referenced = next(
                (row for row in rows if row["rollout_id"] == signal.rollout_id),
                None,
            )
            if referenced is None:
                raise ControlPlaneError("update signal references a missing rollout")
            if (
                referenced["release_id"] != signal.release_id
                or referenced["channel"] != signal.channel
            ):
                raise ControlPlaneError(
                    "update signal identity does not match its rollout"
                )
            if self._channel_killed(connection, ReleaseChannel(signal.channel)):
                return ()
            return tuple(
                self._hint_rollout(row)
                for row in rows
                if row["status"] == "active" and row["release_status"] == "published"
            )
        if signal.signal_type == "channel.kill_cleared":
            if self._channel_killed(connection, ReleaseChannel(signal.channel)):
                return ()
            rows = connection.execute(
                "SELECT "
                + columns
                + join
                + "WHERE rollouts.channel=? AND rollouts.status='active' "
                "AND releases.status='published' "
                "ORDER BY rollouts.created_at DESC,rollouts.rollout_id DESC",
                (signal.channel,),
            ).fetchall()
            return tuple(self._hint_rollout(row) for row in rows)
        if signal.rollout_id is None or signal.release_id is None:
            raise ControlPlaneError("rollout update signal identity is incomplete")
        row = connection.execute(
            "SELECT " + columns + join + "WHERE rollouts.rollout_id=?",
            (signal.rollout_id,),
        ).fetchone()
        if row is None:
            raise ControlPlaneError("update signal references a missing rollout")
        if row["release_id"] != signal.release_id or row["channel"] != signal.channel:
            raise ControlPlaneError("update signal identity does not match its rollout")
        return (self._hint_rollout(row),)

    def _verified_hint_manifest(
        self,
        rollout: _HintRollout,
        cache: dict[str, tuple[str, str, str, str, ReleaseManifest]],
    ) -> ReleaseManifest:
        cached = cache.get(rollout.release_id)
        identity = (
            rollout.manifest_sha256,
            rollout.release_version,
            rollout.release_build_digest,
            rollout.channel,
        )
        if cached is not None:
            if cached[:4] != identity:
                raise ControlPlaneError(
                    "stored release identity changed within one update snapshot"
                )
            return cached[4]
        if _sha(rollout.manifest_json) != rollout.manifest_sha256:
            raise ControlPlaneError("stored release manifest digest is invalid")
        try:
            manifest = ReleaseManifest.from_json(rollout.manifest_json)
        except (TypeError, ValueError):
            raise ControlPlaneError("stored release manifest is invalid") from None
        verify_manifest_signature(manifest, self.verifier)
        if (
            manifest.release_id != rollout.release_id
            or manifest.version != rollout.release_version
            or manifest.build_digest != rollout.release_build_digest
            or manifest.channel.value != rollout.channel
        ):
            raise ControlPlaneError(
                "stored release identity does not match its update signal"
            )
        cache[rollout.release_id] = (*identity, manifest)
        return manifest

    def _hint_for_client(
        self,
        signal: ControlUpdateSignal,
        client: UpdateHintClient,
        rollouts: tuple[_HintRollout, ...],
        manifest_cache: dict[str, tuple[str, str, str, str, ReleaseManifest]],
        artifact_cache: dict[tuple[str, str, str], bool],
    ) -> ReleaseManifest | None:
        if client.channel.value != signal.channel:
            return None
        best: ReleaseManifest | None = None
        rollback_best: ReleaseManifest | None = None
        for rollout in rollouts:
            if (
                rollout.organizations
                and client.principal.organization_id not in rollout.organizations
            ):
                continue
            if rollout.accounts and client.principal.account_id not in rollout.accounts:
                continue
            minimum = rollout.minimum_compatible_version
            mandatory = (
                minimum is not None
                and _compare_semver(client.current_version, minimum) < 0
            )
            bucket = (
                int.from_bytes(
                    hashlib.sha256(
                        f"{rollout.rollout_id}\0{client.principal.client_id}".encode()
                    ).digest()[:4],
                    "big",
                )
                % 100
            )
            if not mandatory and bucket >= rollout.percentage:
                continue
            manifest = self._verified_hint_manifest(rollout, manifest_cache)
            if rollout.rollback_id is not None:
                if (
                    client.current_release_id != rollout.source_release_id
                    or client.current_build_digest != rollout.source_build_digest
                    or client.current_version != rollout.source_version
                    or _compare_semver(manifest.version, client.current_version) >= 0
                ):
                    continue
            elif _compare_semver(manifest.version, client.current_version) <= 0:
                continue
            target = (manifest.release_id, client.platform, client.architecture)
            supported = artifact_cache.get(target)
            if supported is None:
                artifact = next(
                    (
                        item
                        for item in manifest.artifacts
                        if item.platform == client.platform
                        and item.architecture == client.architecture
                    ),
                    None,
                )
                if artifact is None:
                    artifact_cache[target] = False
                    continue
                verify_artifact_signature(manifest, artifact, self.verifier)
                artifact_cache[target] = True
            elif supported is False:
                continue
            if rollout.rollback_id is not None:
                if rollback_best is None:
                    rollback_best = manifest
            elif best is None or _compare_semver(manifest.version, best.version) > 0:
                best = manifest
        if rollback_best is not None:
            best = rollback_best
        if (
            signal.signal_type == "rollout.activated"
            and best is not None
            and best.release_id != signal.release_id
        ):
            return None
        return best

    def hint_manifests_for_clients(
        self,
        signal: ControlUpdateSignal,
        clients: Sequence[UpdateHintClient],
    ) -> tuple[ReleaseManifest | None, ...]:
        """Resolve at most 1024 hints from one durable database snapshot.

        Activation and channel-clear batches preserve the former client
        heartbeat/update-state UPSERT, but do it once per batch.  Revocation
        signals remain read-only as before.  Manifest and artifact signature
        caches are local to this snapshot and are never reused across calls.
        """

        if not isinstance(signal, ControlUpdateSignal):
            raise TypeError("update signal must use the durable signal model")
        if isinstance(clients, (str, bytes)) or not isinstance(clients, Sequence):
            raise TypeError("update hint clients must be a bounded sequence")
        if len(clients) > MAX_UPDATE_HINT_BATCH_SIZE:
            raise ValueError("update hint batch exceeds its bounded size")
        batch = tuple(clients)
        seen_client_ids: set[str] = set()
        for client in batch:
            self.validate_update_hint_client(client)
            if client.principal.client_id in seen_client_ids:
                raise ValueError("update hint batch contains a duplicate client")
            seen_client_ids.add(client.principal.client_id)
        matching = tuple(
            client for client in batch if client.channel.value == signal.channel
        )
        preserves_heartbeat = signal.signal_type in {
            "rollout.activated",
            "channel.kill_cleared",
        }
        transaction = (
            self._transaction()
            if preserves_heartbeat and matching
            else self._read_transaction()
        )
        with transaction as connection:
            self._require_update_signal_connection(connection, signal)
            if preserves_heartbeat and matching:
                observed_at = _now()
                connection.executemany(
                    "INSERT INTO control_clients VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(client_id) DO UPDATE SET "
                    "account_id=excluded.account_id,"
                    "organization_id=excluded.organization_id,"
                    "platform=excluded.platform,"
                    "architecture=excluded.architecture,"
                    "current_version=excluded.current_version,"
                    "update_state=excluded.update_state,"
                    "last_seen_at=excluded.last_seen_at",
                    (
                        (
                            client.principal.client_id,
                            client.principal.account_id,
                            client.principal.organization_id,
                            client.platform,
                            client.architecture,
                            client.current_version,
                            client.update_state,
                            observed_at,
                        )
                        for client in matching
                    ),
                )
            rollouts = self._hint_rollouts_for_signal(connection, signal)
            manifest_cache: dict[str, tuple[str, str, str, str, ReleaseManifest]] = {}
            artifact_cache: dict[tuple[str, str, str], bool] = {}
            # Every rollout fact that can influence this snapshot is verified
            # once even when no client ultimately qualifies.  A corrupt
            # manifest therefore fails the whole batch instead of being hidden
            # by targeting, while per-client evaluation reuses the local proof.
            for rollout in rollouts:
                self._verified_hint_manifest(rollout, manifest_cache)
            return tuple(
                self._hint_for_client(
                    signal,
                    client,
                    rollouts,
                    manifest_cache,
                    artifact_cache,
                )
                for client in batch
            )

    def hint_manifest_for_client(
        self,
        signal: ControlUpdateSignal,
        principal: ControlPrincipal,
        *,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str = "idle",
    ) -> ReleaseManifest | None:
        """Compatibility wrapper over the bounded, snapshot-consistent API."""

        if not isinstance(signal, ControlUpdateSignal):
            raise TypeError("update signal must use the durable signal model")
        return self.hint_manifests_for_clients(
            signal,
            (
                UpdateHintClient(
                    principal=principal,
                    channel=ReleaseChannel(signal.channel),
                    platform=platform,
                    architecture=architecture,
                    current_version=current_version,
                    update_state=update_state,
                ),
            ),
        )[0]

    @staticmethod
    def _signal_dedupe_key(
        actor: ControlPrincipal,
        client_request_id: str,
        operation: str,
        target_id: str,
    ) -> str:
        return _sha(
            _json(
                {
                    "actor_subject": actor.subject,
                    "client_request_id": client_request_id,
                    "operation": operation,
                    "target_id": target_id,
                }
            )
        )

    @staticmethod
    def _append_update_signal(
        connection,
        *,
        signal_type: str,
        channel: ReleaseChannel,
        rollout_id: str | None,
        release_id: str | None,
        dedupe_key: str,
    ) -> None:
        connection.execute(
            "INSERT INTO control_update_signals("
            "event_id,dedupe_key,signal_type,channel,rollout_id,release_id,created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                "control_signal_" + uuid.uuid4().hex,
                dedupe_key,
                signal_type,
                channel.value,
                rollout_id,
                release_id,
                _now(),
            ),
        )

    @staticmethod
    def _update_signal(row) -> ControlUpdateSignal:
        return ControlUpdateSignal(
            sequence=int(row["sequence"]),
            event_id=row["event_id"],
            signal_type=row["signal_type"],
            channel=row["channel"],
            rollout_id=row["rollout_id"],
            release_id=row["release_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _channel_killed(connection, channel: ReleaseChannel) -> bool:
        row = connection.execute(
            "SELECT kill_switch_active FROM control_channel_state WHERE channel=?",
            (channel.value,),
        ).fetchone()
        if row is None:
            raise ControlPlaneError("Control Plane channel state is missing")
        return bool(row["kill_switch_active"])

    def _candidate(self, connection, release_id: str) -> CandidateProjection:
        row = self._require_release(connection, release_id)
        self._verified_manifest(row)
        gates = {
            item["gate_name"]: item["status"]
            for item in connection.execute(
                "SELECT gate_name,status FROM control_release_gates WHERE release_id=?",
                (release_id,),
            )
        }
        return CandidateProjection(
            release_id=row["release_id"],
            version=row["version"],
            build_digest=row["build_digest"],
            channel=row["channel"],
            status=row["status"],
            gates=gates,
            missing_gates=sorted(required_release_gates(row["channel"]) - set(gates)),
        )

    def _verified_manifest(self, row) -> ReleaseManifest:
        payload = str(row["manifest_json"])
        if _sha(payload) != row["manifest_sha256"]:
            raise ControlPlaneError("stored release manifest digest is invalid")
        manifest = ReleaseManifest.from_json(payload)
        verify_manifest_signature(manifest, self.verifier)
        for artifact in manifest.artifacts:
            verify_artifact_signature(manifest, artifact, self.verifier)
        if (
            manifest.release_id != row["release_id"]
            or manifest.version != row["version"]
            or manifest.build_digest != row["build_digest"]
            or manifest.channel.value != row["channel"]
        ):
            raise ControlPlaneError(
                "stored release identity does not match its manifest"
            )
        return manifest

    def _rollout(self, connection, rollout_id: str) -> RolloutProjection:
        row = connection.execute(
            "SELECT * FROM control_rollouts WHERE rollout_id=?", (rollout_id,)
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("rollout does not exist")
        return RolloutProjection(
            rollout_id=row["rollout_id"],
            release_id=row["release_id"],
            channel=row["channel"],
            status=row["status"],
            percentage=row["percentage"],
            target_organization_ids=_list(row["target_organizations_json"]),
            target_account_ids=_list(row["target_accounts_json"]),
            minimum_compatible_version=row["minimum_compatible_version"],
            created_at=row["created_at"],
        )

    def rollback_projection(self, rollback_id: str) -> RollbackProjection:
        with self._read_transaction() as connection:
            return self._rollback(connection, rollback_id)

    def _rollback(self, connection, rollback_id: str) -> RollbackProjection:
        row = connection.execute(
            "SELECT rollbacks.*,rollouts.channel,rollouts.status,"
            "rollouts.percentage,rollouts.target_organizations_json,"
            "rollouts.target_accounts_json "
            "FROM control_release_rollbacks AS rollbacks "
            "JOIN control_rollouts AS rollouts "
            "ON rollouts.rollout_id=rollbacks.rollback_id "
            "WHERE rollbacks.rollback_id=?",
            (rollback_id,),
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("rollback does not exist")
        return RollbackProjection(
            rollback_id=row["rollback_id"],
            source_release_id=row["source_release_id"],
            target_release_id=row["target_release_id"],
            channel=row["channel"],
            status=row["status"],
            percentage=row["percentage"],
            target_organization_ids=_list(row["target_organizations_json"]),
            target_account_ids=_list(row["target_accounts_json"]),
            authorization_ttl_seconds=row["authorization_ttl_seconds"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _require_release(connection, release_id: str):
        if not _SAFE_ID.fullmatch(release_id):
            raise ValueError("release_id is invalid")
        row = connection.execute(
            "SELECT * FROM control_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("release does not exist")
        return row

    @staticmethod
    def _bootstrap_active(connection: sqlite3.Connection):
        return connection.execute(
            "SELECT state.activation_record_id,state.authority_sequence,"
            "state.authority_revision,state.authority_issued_at,"
            "state.authority_expires_at,state.authority_target_json,"
            "state.index_sha256,activations.stage_record_id,"
            "activations.index_size_bytes,activations.public_url,"
            "stages.release_id,stages.version,stages.build_digest,stages.index_bytes "
            "FROM bootstrap_index_active_state AS state "
            "JOIN bootstrap_index_activations AS activations "
            "ON activations.record_id=state.activation_record_id "
            "JOIN bootstrap_index_stages AS stages "
            "ON stages.record_id=activations.stage_record_id WHERE state.singleton=1"
        ).fetchone()

    @staticmethod
    def _bootstrap_stage_projection(row: sqlite3.Row) -> dict[str, Any]:
        index = json.loads(bytes(row["index_bytes"]).decode("utf-8"))
        return {
            "schema_version": 1,
            "release_id": str(row["release_id"]),
            "state": "staged",
            "index_sha256": str(row["index_sha256"]),
            "index_size_bytes": int(row["index_size_bytes"]),
            "public_url": str(row["public_url"]),
            "revision_id": str(row["record_id"]),
            "authority": json.loads(str(row["authority_json"])),
            "freshness": index["freshness"],
            "active_activation_record_id": row["previous_activation_record_id"],
            "active_sequence": row["previous_sequence"],
            "active_revision_id": row["previous_revision"],
            "active_index_sha256": row["previous_index_sha256"],
            "active_target": (
                json.loads(str(row["previous_target_json"]))
                if row["previous_target_json"] is not None
                else None
            ),
        }

    def _bootstrap_intent_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        activation: sqlite3.Row | None,
    ) -> dict[str, Any]:
        stage = connection.execute(
            "SELECT * FROM bootstrap_index_stages WHERE record_id=?",
            (row["stage_record_id"],),
        ).fetchone()
        assert stage is not None
        return {
            "schema_version": 1,
            "release_id": str(stage["release_id"]),
            "state": "completed" if activation is not None else "publishing",
            "publication_intent_record_id": str(row["record_id"]),
            "staged_revision_id": str(row["stage_record_id"]),
            "index_sha256": str(row["candidate_index_sha256"]),
            "index_size_bytes": int(row["candidate_size_bytes"]),
            "public_url": str(row["public_url"]),
            "previous_activation_record_id": row["previous_activation_record_id"],
            "previous_sequence": row["previous_sequence"],
            "previous_revision_id": row["previous_revision"],
            "previous_index_sha256": row["previous_index_sha256"],
            "previous_target": (
                json.loads(str(row["previous_target_json"]))
                if row["previous_target_json"] is not None
                else None
            ),
            "active_activation_record_id": (
                str(activation["record_id"]) if activation is not None else None
            ),
        }

    def _completed_bootstrap_projection(
        self,
        connection: sqlite3.Connection,
        activation: sqlite3.Row,
    ) -> dict[str, Any]:
        projection = self._bootstrap_activation_projection(connection, activation)
        readback = connection.execute(
            "SELECT * FROM bootstrap_index_readbacks WHERE activation_record_id=?",
            (activation["record_id"],),
        ).fetchone()
        if readback is None:
            raise ControlPlaneError(
                "active Bootstrap authority lacks trusted readback proof"
            )
        return {
            **projection,
            "proof": self._bootstrap_proof_projection(connection, readback),
        }

    def _bootstrap_activation_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        stage = connection.execute(
            "SELECT * FROM bootstrap_index_stages WHERE record_id=?",
            (row["stage_record_id"],),
        ).fetchone()
        assert stage is not None
        readback = connection.execute(
            "SELECT * FROM bootstrap_index_readbacks WHERE activation_record_id=?",
            (row["record_id"],),
        ).fetchone()
        index = json.loads(bytes(stage["index_bytes"]).decode("utf-8"))
        return {
            "schema_version": 1,
            "release_id": str(stage["release_id"]),
            "state": "active-and-read-back" if readback is not None else "active",
            "index_sha256": str(row["index_sha256"]),
            "index_size_bytes": int(row["index_size_bytes"]),
            "public_url": str(row["public_url"]),
            "staged_revision_id": str(row["stage_record_id"]),
            "active_revision_id": str(row["record_id"]),
            "public_object_revision_id": str(row["public_object_revision_id"]),
            "authority": json.loads(str(stage["authority_json"])),
            "freshness": index["freshness"],
            "previous_activation_record_id": row["previous_activation_record_id"],
            "previous_sequence": stage["previous_sequence"],
            "previous_revision_id": stage["previous_revision"],
            "previous_index_sha256": stage["previous_index_sha256"],
            "previous_target": (
                json.loads(str(stage["previous_target_json"]))
                if stage["previous_target_json"] is not None
                else None
            ),
            "readback_record_id": (
                str(readback["record_id"]) if readback is not None else None
            ),
        }

    @staticmethod
    def _latest_bootstrap_proof(
        connection: sqlite3.Connection,
        release_id: str,
    ):
        return connection.execute(
            "SELECT readbacks.*,stages.index_bytes,stages.release_id,stages.version,"
            "stages.build_digest,stages.authority_json,stages.authority_target_json,"
            "stages.authority_sequence,stages.authority_revision,"
            "stages.authority_issued_at,stages.authority_expires_at,"
            "activations.stage_record_id FROM bootstrap_index_readbacks AS readbacks "
            "JOIN bootstrap_index_active_state AS state "
            "ON state.activation_record_id=readbacks.activation_record_id "
            "JOIN bootstrap_index_activations AS activations "
            "ON activations.record_id=readbacks.activation_record_id "
            "JOIN bootstrap_index_stages AS stages "
            "ON stages.record_id=activations.stage_record_id "
            "WHERE state.singleton=1 AND stages.release_id=?",
            (release_id,),
        ).fetchone()

    @staticmethod
    def _bootstrap_proof_by_record(
        connection: sqlite3.Connection,
        record_id: str,
        release_id: str | None = None,
    ):
        query = (
            "SELECT readbacks.*,stages.index_bytes,stages.release_id,stages.version,"
            "stages.build_digest,stages.authority_json,stages.authority_target_json,"
            "stages.authority_sequence,stages.authority_revision,"
            "stages.authority_issued_at,stages.authority_expires_at,"
            "activations.stage_record_id FROM bootstrap_index_readbacks AS readbacks "
            "JOIN bootstrap_index_activations AS activations "
            "ON activations.record_id=readbacks.activation_record_id "
            "JOIN bootstrap_index_stages AS stages "
            "ON stages.record_id=activations.stage_record_id "
            "WHERE readbacks.record_id=?"
        )
        parameters: tuple[Any, ...] = (record_id,)
        if release_id is not None:
            query += " AND stages.release_id=?"
            parameters = (record_id, release_id)
        return connection.execute(query, parameters).fetchone()

    def _bootstrap_proof_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        joined = self._bootstrap_proof_by_record(connection, str(row["record_id"]))
        if joined is None:
            raise ControlPlaneConflict("Bootstrap index readback proof is missing")
        value = {
            "schema_version": 1,
            "record_id": str(joined["record_id"]),
            "activation_record_id": str(joined["activation_record_id"]),
            "stage_record_id": str(joined["stage_record_id"]),
            "release_id": str(joined["release_id"]),
            "version": str(joined["version"]),
            "build_digest": str(joined["build_digest"]),
            "sequence": int(joined["authority_sequence"]),
            "revision": str(joined["authority_revision"]),
            "issued_at": str(joined["authority_issued_at"]),
            "expires_at": str(joined["authority_expires_at"]),
            "target": json.loads(str(joined["authority_target_json"])),
            "index_sha256": str(joined["index_sha256"]),
            "index_size_bytes": int(joined["index_size_bytes"]),
            "public_url": str(joined["public_url"]),
            "read_back_at": str(joined["read_back_at"]),
        }
        digest = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
        return {
            **value,
            "proof_token": (
                f"bootstrap-index-proof:{value['record_id']}:sha256:{digest}"
            ),
        }

    def _require_bootstrap_index_proof(
        self,
        connection: sqlite3.Connection,
        *,
        release: sqlite3.Row,
        evidence: str,
    ) -> None:
        match = _BOOTSTRAP_PROOF.fullmatch(evidence)
        if match is None:
            raise ReleaseGateError(
                "stable Bootstrap gate requires trusted public readback proof"
            )
        proof = self._bootstrap_proof_by_record(
            connection, match.group(1), str(release["release_id"])
        )
        if proof is None:
            raise ReleaseGateError("stable Bootstrap proof is missing")
        projection = self._bootstrap_proof_projection(connection, proof)
        current = self._latest_bootstrap_proof(connection, str(release["release_id"]))
        if (
            current is None
            or projection["proof_token"] != evidence
            or projection["version"] != release["version"]
            or projection["build_digest"] != release["build_digest"]
            or int(current["authority_sequence"]) != projection["sequence"]
            or str(current["authority_revision"]) != projection["revision"]
            or json.loads(str(current["authority_target_json"])) != projection["target"]
        ):
            raise ReleaseGateError(
                "stable Bootstrap proof does not match the signed release"
            )
        _parse_bootstrap_index_bytes(
            bytes(current["index_bytes"]),
            verifier=self.verifier,
            freshness_verifier=self._require_bootstrap_freshness_verifier(),
        )

    def _require_current_release_bootstrap_gate(
        self,
        connection: sqlite3.Connection,
        release: sqlite3.Row,
    ) -> None:
        if release["channel"] != ReleaseChannel.STABLE.value:
            return
        gate = connection.execute(
            "SELECT status,evidence FROM control_release_gates "
            "WHERE release_id=? AND gate_name='bootstrap-index'",
            (release["release_id"],),
        ).fetchone()
        if gate is None or gate["status"] != "passed":
            raise ReleaseGateError("stable release lacks a passed Bootstrap proof gate")
        self._require_bootstrap_index_proof(
            connection,
            release=release,
            evidence=str(gate["evidence"]),
        )

    def _require_bootstrap_freshness_verifier(self) -> SignatureVerifier:
        if self.bootstrap_freshness_verifier is None:
            raise ControlPlaneError("Bootstrap freshness trust is not configured")
        return self.bootstrap_freshness_verifier

    @staticmethod
    def _require_bootstrap_refresh_attempt(
        connection: sqlite3.Connection,
        attempt_record_id: str,
        owner_id: str,
        *,
        now: datetime,
    ) -> sqlite3.Row:
        attempt = connection.execute(
            "SELECT * FROM bootstrap_freshness_refresh_attempts WHERE record_id=?",
            (attempt_record_id,),
        ).fetchone()
        lease = connection.execute(
            "SELECT * FROM bootstrap_freshness_refresh_lease WHERE singleton=1"
        ).fetchone()
        if attempt is None:
            raise ControlPlaneNotFound(
                "Bootstrap freshness refresh attempt does not exist"
            )
        if (
            lease is None
            or lease["attempt_record_id"] != attempt_record_id
            or lease["owner_id"] != owner_id
            or _parse_iso_time(str(lease["expires_at"])) <= now
        ):
            raise ControlPlaneConflict("Bootstrap freshness refresh lease was lost")
        return attempt

    @staticmethod
    def _append_bootstrap_refresh_event(
        connection: sqlite3.Connection,
        *,
        attempt_record_id: str | None,
        status: str,
        error_code: str | None = None,
        activation_record_id: str | None = None,
        proof_record_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO bootstrap_freshness_refresh_events("
            "event_id,attempt_record_id,status,error_code,activation_record_id,"
            "proof_record_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                "brefresh_event_" + uuid.uuid4().hex,
                attempt_record_id,
                status,
                error_code,
                activation_record_id,
                proof_record_id,
                _now(),
            ),
        )

    @staticmethod
    def _set_bootstrap_refresh_state(
        connection: sqlite3.Connection,
        *,
        status: str,
        active_expires_at: str | None,
        last_checked_at: str,
        next_check_at: str,
        attempt_record_id: str | None,
        error_code: str | None,
        success_at: str | None = None,
        failure_at: str | None = None,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM bootstrap_freshness_refresh_state WHERE singleton=1"
        ).fetchone()
        prior_attempt = existing["last_attempt_record_id"] if existing else None
        prior_success = existing["last_success_at"] if existing else None
        prior_failure = existing["last_failure_at"] if existing else None
        updated_at = _now()
        connection.execute(
            "INSERT INTO bootstrap_freshness_refresh_state("
            "singleton,status,active_expires_at,last_checked_at,next_check_at,"
            "last_attempt_record_id,last_success_at,last_failure_at,last_error_code,"
            "updated_at) VALUES(1,?,?,?,?,?,?,?,?,?) ON CONFLICT(singleton) "
            "DO UPDATE SET status=excluded.status,"
            "active_expires_at=excluded.active_expires_at,"
            "last_checked_at=excluded.last_checked_at,"
            "next_check_at=excluded.next_check_at,"
            "last_attempt_record_id=excluded.last_attempt_record_id,"
            "last_success_at=excluded.last_success_at,"
            "last_failure_at=excluded.last_failure_at,"
            "last_error_code=excluded.last_error_code,"
            "updated_at=excluded.updated_at",
            (
                status,
                active_expires_at,
                last_checked_at,
                next_check_at,
                attempt_record_id or prior_attempt,
                success_at or prior_success,
                failure_at or prior_failure,
                error_code,
                updated_at,
            ),
        )

    @staticmethod
    def _append_bootstrap_outbox(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> None:
        payload_sha256 = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        event_id = (
            "bevt_"
            + hashlib.sha256(
                (event_type + "\0" + record_id + "\0" + payload_sha256).encode("utf-8")
            ).hexdigest()[:32]
        )
        connection.execute(
            "INSERT OR IGNORE INTO bootstrap_index_outbox("
            "event_id,event_type,record_id,payload_sha256,created_at) "
            "VALUES(?,?,?,?,?)",
            (event_id, event_type, record_id, payload_sha256, _now()),
        )

    def _replay(self, connection, actor, request_id, operation, request):
        row = connection.execute(
            "SELECT operation,request_sha256,response_json FROM control_idempotency "
            "WHERE actor_subject=? AND client_request_id=?",
            (actor.subject, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_sha256"] != _sha(
            _json(request)
        ):
            raise ControlPlaneConflict(
                "client_request_id was reused with different content"
            )
        return json.loads(row["response_json"])

    @staticmethod
    def _remember(connection, actor, request_id, operation, request, response):
        connection.execute(
            "INSERT INTO control_idempotency VALUES (?,?,?,?,?,?)",
            (
                actor.subject,
                request_id,
                operation,
                _sha(_json(request)),
                _json(response),
                _now(),
            ),
        )

    @staticmethod
    def _audit(connection, actor, action, target_id, payload):
        previous = connection.execute(
            "SELECT sequence,entry_digest FROM control_admin_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_digest = previous["entry_digest"] if previous else "0" * 64
        sequence = (int(previous["sequence"]) + 1) if previous else 1
        payload_sha = _sha(_json(payload))
        created = _now()
        entry = _sha(
            "\0".join(
                (
                    str(sequence),
                    previous_digest,
                    actor.subject,
                    action,
                    target_id,
                    payload_sha,
                    created,
                )
            )
        )
        connection.execute(
            "INSERT INTO control_admin_audit("
            "actor_subject,action,target_id,payload_sha256,previous_digest,entry_digest,created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                actor.subject,
                action,
                target_id,
                payload_sha,
                previous_digest,
                entry,
                created,
            ),
        )

    @staticmethod
    def _verify_audit_row(row, expected_sequence: int, previous: str) -> str:
        if (
            int(row["sequence"]) != expected_sequence
            or row["previous_digest"] != previous
        ):
            raise ControlPlaneError("Control Plane audit chain is broken")
        expected = _sha(
            "\0".join(
                (
                    str(expected_sequence),
                    previous,
                    row["actor_subject"],
                    row["action"],
                    row["target_id"],
                    row["payload_sha256"],
                    row["created_at"],
                )
            )
        )
        if row["entry_digest"] != expected:
            raise ControlPlaneError("Control Plane audit digest is invalid")
        return expected

    def _verify_audit_full_connection(self, connection) -> tuple[int, str]:
        previous = _ZERO_AUDIT_DIGEST
        expected_sequence = 1
        for row in connection.execute(
            "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
            "previous_digest,entry_digest,created_at "
            "FROM control_admin_audit ORDER BY sequence"
        ):
            previous = self._verify_audit_row(row, expected_sequence, previous)
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _verify_audit_incremental(
        self,
        connection,
        checkpoint: tuple[int, str],
        *,
        recheck_tail: bool,
    ) -> tuple[int, str]:
        checkpoint_sequence, checkpoint_digest = checkpoint
        if checkpoint_sequence < 0 or (
            checkpoint_sequence == 0 and checkpoint_digest != _ZERO_AUDIT_DIGEST
        ):
            raise ControlPlaneError("Control Plane audit checkpoint is invalid")
        if recheck_tail and checkpoint_sequence:
            tail = connection.execute(
                "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
                "previous_digest,entry_digest,created_at "
                "FROM control_admin_audit WHERE sequence=?",
                (checkpoint_sequence,),
            ).fetchone()
            if tail is None:
                raise ControlPlaneError("Control Plane audit chain is broken")
            recomputed = self._verify_audit_row(
                tail,
                checkpoint_sequence,
                str(tail["previous_digest"]),
            )
            if recomputed != checkpoint_digest:
                raise ControlPlaneError("Control Plane audit checkpoint is invalid")

        expected_sequence = checkpoint_sequence + 1
        previous = checkpoint_digest
        for row in connection.execute(
            "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
            "previous_digest,entry_digest,created_at "
            "FROM control_admin_audit WHERE sequence>? ORDER BY sequence",
            (checkpoint_sequence,),
        ):
            previous = self._verify_audit_row(row, expected_sequence, previous)
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _checkpoint_snapshot(self) -> tuple[int, str]:
        with self._audit_checkpoint_lock:
            if self._audit_checkpoint_fault is not None:
                raise ControlPlaneError(self._audit_checkpoint_fault)
            return self._audit_checkpoint

    def _advance_audit_checkpoint(self, candidate: tuple[int, str]) -> None:
        """Merge a committed checkpoint without ever changing commit semantics.

        This method is deliberately non-throwing.  Once SQLite has acknowledged
        ``COMMIT``, reporting a normal operation failure would invite a caller to
        retry a mutation which already happened.  An impossible equal-sequence /
        different-digest merge therefore poisons subsequent operations until an
        explicit full verification succeeds, while the committed caller still
        receives its successful result.
        """

        with self._audit_checkpoint_lock:
            current = self._audit_checkpoint
            if candidate[0] > current[0]:
                self._audit_checkpoint = candidate
            elif candidate[0] == current[0] and candidate[1] != current[1]:
                self._audit_checkpoint_fault = (
                    "Control Plane audit checkpoint is inconsistent; "
                    "explicit full verification is required"
                )

    def _poison_audit_checkpoint(self, message: str) -> None:
        with self._audit_checkpoint_lock:
            self._audit_checkpoint_fault = message

    def verify_full_integrity(self) -> int:
        """Explicitly verify the complete append-only audit chain and reset its checkpoint."""

        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            with self._audit_checkpoint_lock:
                checkpoint = self._verify_audit_full_connection(connection)
                connection.commit()
                self._audit_checkpoint = checkpoint
                self._audit_checkpoint_fault = None
            return checkpoint[0]
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, (ControlPlaneError, sqlite3.DatabaseError)):
                self._poison_audit_checkpoint(
                    "Control Plane audit integrity failed full verification"
                )
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._checkpoint_snapshot()
            candidate = self._verify_audit_incremental(
                connection, checkpoint, recheck_tail=True
            )
            yield connection
            candidate = self._verify_audit_incremental(
                connection, candidate, recheck_tail=False
            )
            connection.commit()
            committed = True
            self._advance_audit_checkpoint(candidate)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            try:
                connection.close()
            except Exception:
                if not committed:
                    raise
                self._poison_audit_checkpoint(
                    "Control Plane connection cleanup failed after commit; "
                    "explicit full verification is required"
                )

    @contextmanager
    def _read_transaction(self):
        connection = self._connect()
        committed = False
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            checkpoint = self._checkpoint_snapshot()
            candidate = self._verify_audit_incremental(
                connection, checkpoint, recheck_tail=True
            )
            yield connection
            connection.commit()
            committed = True
            self._advance_audit_checkpoint(candidate)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            try:
                connection.close()
            except Exception:
                if not committed:
                    raise
                self._poison_audit_checkpoint(
                    "Control Plane connection cleanup failed after commit; "
                    "explicit full verification is required"
                )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_bootstrap_index_bytes(
    payload: bytes,
    *,
    verifier: SignatureVerifier,
    freshness_verifier: SignatureVerifier,
    now: datetime | None = None,
    allow_expired_freshness: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(payload, bytes)
        or not 1 <= len(payload) <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
    ):
        raise ValueError("public Bootstrap index bytes are invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("public Bootstrap index bytes are invalid") from None
    if not isinstance(value, dict):
        raise ValueError("public Bootstrap index must contain an object")
    canonical = _json(value).encode("utf-8") + b"\n"
    if canonical != payload:
        raise ValueError("public Bootstrap index bytes are not canonical")
    try:
        validate_public_bootstrap_index(
            value,
            verifier=verifier,
            freshness_verifier=freshness_verifier,
            now=now,
            allow_expired_freshness=allow_expired_freshness,
        )
    except (PublicBootstrapIndexError, TypeError):
        raise ValueError(
            "public Bootstrap index signature or freshness is invalid"
        ) from None
    if value.get("status") != "published":
        raise ValueError("public Bootstrap index is not published")
    return value


def _observed_utc(value: datetime | None) -> datetime:
    observed = datetime.now(UTC) if value is None else value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Bootstrap freshness clock must be timezone-aware")
    return observed.astimezone(UTC).replace(microsecond=0)


def _format_bootstrap_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_bootstrap_time(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise ControlPlaneError("stored Bootstrap freshness time is invalid") from None
    return parsed


def _parse_iso_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ControlPlaneError("stored Bootstrap lease time is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ControlPlaneError("stored Bootstrap lease time is invalid")
    return parsed.astimezone(UTC)


def _validate_public_pointer_url(value: str) -> None:
    parsed = urlsplit(value if isinstance(value, str) else "")
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or not parsed.path.endswith("/public-bootstrap-index.json")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public Bootstrap index URL is invalid")


def _bootstrap_previous_values(row) -> tuple[Any, Any, Any, Any, Any]:
    if row is None:
        return None, None, None, None, None
    return (
        row["activation_record_id"],
        int(row["authority_sequence"]),
        row["authority_revision"],
        row["index_sha256"],
        row["authority_target_json"],
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _list(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ControlPlaneError("stored rollout targets are invalid")
    return parsed


def _core_target_matrix(manifest: ReleaseManifest) -> frozenset[tuple[str, str]]:
    matrix = frozenset(
        (artifact.platform, artifact.architecture)
        for artifact in manifest.artifacts
        if artifact.artifact_id == f"core-{artifact.platform}-{artifact.architecture}"
    )
    if not matrix:
        raise ControlPlaneError("release manifest has no canonical Core target")
    return matrix


def _semver_key(value: str):
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError("version must be valid SemVer")
    prerelease = match.group(4)
    parts = None if prerelease is None else tuple(prerelease.split("."))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), parts


def _compare_semver(left: str, right: str) -> int:
    left_key, right_key = _semver_key(left), _semver_key(right)
    if left_key[:3] != right_key[:3]:
        return (left_key[:3] > right_key[:3]) - (left_key[:3] < right_key[:3])
    left_pre, right_pre = left_key[3], right_key[3]
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        if left_item.isdigit() and right_item.isdigit():
            return (int(left_item) > int(right_item)) - (
                int(left_item) < int(right_item)
            )
        if left_item.isdigit() != right_item.isdigit():
            return -1 if left_item.isdigit() else 1
        return (left_item > right_item) - (left_item < right_item)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))
