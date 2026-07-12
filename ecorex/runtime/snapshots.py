"""Immutable Runtime configuration snapshots used by Turn replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from ecorex.protocol import PermissionSnapshot

from .database import SQLiteDatabase, json_dumps, json_loads


_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,191}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class RuntimeSnapshotError(RuntimeError):
    pass


class RuntimeSnapshotConflict(RuntimeSnapshotError):
    pass


class RuntimeSnapshotNotFound(RuntimeSnapshotError):
    pass


class RuntimeSnapshotStale(RuntimeSnapshotError):
    """A prepared Turn context no longer represents current Runtime authority."""

    pass


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    snapshot_id: str
    kind: str
    payload: Mapping[str, Any]
    payload_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TurnSnapshotContext:
    """Causality IDs captured atomically with a newly accepted Turn."""

    config_snapshot_id: str
    capability_snapshot_id: str
    permission_snapshot_id: str
    model_catalog_snapshot_id: str
    extension_snapshot_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "config_snapshot_id": self.config_snapshot_id,
            "capability_snapshot_id": self.capability_snapshot_id,
            "permission_snapshot_id": self.permission_snapshot_id,
            "model_catalog_snapshot_id": self.model_catalog_snapshot_id,
            "extension_snapshot_id": self.extension_snapshot_id,
        }


class RuntimeSnapshotRepository:
    """Append-only snapshots sharing the Runtime SQLite WAL database."""

    def __init__(self, database: SQLiteDatabase | str | Path) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )

    def save(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        snapshot_id: str | None = None,
    ) -> RuntimeSnapshot:
        with self.database.transaction() as connection:
            return self.save_in_transaction(
                connection,
                kind,
                payload,
                snapshot_id=snapshot_id,
            )

    def save_in_transaction(
        self,
        connection: sqlite3.Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        snapshot_id: str | None = None,
    ) -> RuntimeSnapshot:
        """Append one immutable snapshot inside its caller's business commit."""

        if not connection.in_transaction:
            raise RuntimeError("runtime snapshot save requires an active transaction")
        if not _KIND.fullmatch(kind):
            raise ValueError("runtime snapshot kind is invalid")
        try:
            payload_json = json_dumps(dict(payload))
        except (TypeError, ValueError) as error:
            raise ValueError("runtime snapshot payload must be canonical JSON") from error
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        snapshot_id = snapshot_id or f"{kind}_{digest}"
        if not _ID.fullmatch(snapshot_id):
            raise ValueError("runtime snapshot ID is invalid")
        row = connection.execute(
            "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is not None:
            if (
                row["kind"] != kind
                or row["payload_json"] != payload_json
                or row["payload_sha256"] != digest
            ):
                raise RuntimeSnapshotConflict(
                    "runtime snapshot ID was reused with different content"
                )
            return self._from_row(row)
        connection.execute(
            "INSERT INTO runtime_snapshots("
            "snapshot_id, kind, payload_json, payload_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                snapshot_id,
                kind,
                payload_json,
                digest,
                datetime.now(UTC).isoformat(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return self._from_row(row)

    def project(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        snapshot_id: str | None = None,
    ) -> RuntimeSnapshot:
        """Build or read an immutable snapshot without persisting anything.

        Critical-mode bootstrap uses this to expose deterministic catalog IDs
        while preserving every durable fact for diagnosis. Turn admission is
        disabled in that mode, so a virtual snapshot can never become execution
        authority.
        """

        if not _KIND.fullmatch(kind):
            raise ValueError("runtime snapshot kind is invalid")
        try:
            payload_json = json_dumps(dict(payload))
        except (TypeError, ValueError) as error:
            raise ValueError("runtime snapshot payload must be canonical JSON") from error
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        snapshot_id = snapshot_id or f"{kind}_{digest}"
        if not _ID.fullmatch(snapshot_id):
            raise ValueError("runtime snapshot ID is invalid")
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is not None:
            existing = self._from_row(row)
            if (
                existing.kind != kind
                or existing.payload_sha256 != digest
                or dict(existing.payload) != dict(payload)
            ):
                raise RuntimeSnapshotConflict(
                    "runtime snapshot ID was reused with different content"
                )
            return existing
        return RuntimeSnapshot(
            snapshot_id=snapshot_id,
            kind=kind,
            payload=dict(payload),
            payload_sha256=digest,
            created_at="1970-01-01T00:00:00+00:00",
        )

    def get(self, snapshot_id: str) -> RuntimeSnapshot:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise RuntimeSnapshotNotFound(snapshot_id)
        return self._from_row(row)

    def validate_model_selection_in_transaction(
        self,
        connection: sqlite3.Connection,
        context: TurnSnapshotContext,
        *,
        expected_agent_model_id: str,
        expected_image_model_id: str | None,
    ) -> None:
        """Verify a Turn's two frozen model IDs against signed snapshots."""

        if not connection.in_transaction:
            raise RuntimeError("model snapshot validation requires an active transaction")
        if not expected_agent_model_id:
            raise ValueError("a frozen Agent model is required")
        config = self._runtime_snapshot_in_transaction(
            connection, context.config_snapshot_id, expected_kind="config"
        )
        models = self._runtime_snapshot_in_transaction(
            connection, context.model_catalog_snapshot_id, expected_kind="models"
        )
        if (
            config.payload.get("model_catalog_snapshot_id")
            != context.model_catalog_snapshot_id
            or config.payload.get("agent_model_id") != expected_agent_model_id
            or config.payload.get("image_model_id") != expected_image_model_id
        ):
            raise RuntimeSnapshotError(
                "config snapshot does not bind the frozen Turn model selection"
            )
        if models.payload.get("snapshot_id") != context.model_catalog_snapshot_id:
            raise RuntimeSnapshotError("model catalog snapshot identity is invalid")
        modalities = models.payload.get("modalities")
        chat_models = modalities.get("chat") if isinstance(modalities, dict) else None
        if not isinstance(chat_models, list) or expected_agent_model_id not in {
            item.get("model_id") for item in chat_models if isinstance(item, dict)
        }:
            raise RuntimeSnapshotError(
                "model catalog snapshot does not contain the Agent chat model"
            )
        if expected_image_model_id is not None:
            image_models = (
                modalities.get("image") if isinstance(modalities, dict) else None
            )
            if not isinstance(image_models, list) or expected_image_model_id not in {
                item.get("model_id")
                for item in image_models
                if isinstance(item, dict)
            }:
                raise RuntimeSnapshotError(
                    "model catalog snapshot does not contain the frozen image model"
                )

    def validate_permission_snapshot_current_in_transaction(
        self,
        connection: sqlite3.Connection,
        permission_snapshot_id: str,
        *,
        account_id: str,
    ) -> PermissionSnapshot:
        """Verify one frozen permission fact against the current ledger head."""

        if not connection.in_transaction:
            raise RuntimeError(
                "permission snapshot validation requires an active transaction"
            )
        if not account_id:
            raise ValueError("permission snapshot validation requires an account_id")
        permission = self._runtime_snapshot_in_transaction(
            connection,
            permission_snapshot_id,
            expected_kind="permission",
        )
        try:
            projection = PermissionSnapshot.model_validate(permission.payload)
        except (TypeError, ValueError) as error:
            raise RuntimeSnapshotError(
                "permission snapshot payload is invalid"
            ) from error
        if projection.snapshot_id != permission_snapshot_id:
            raise RuntimeSnapshotError("permission snapshot identity is invalid")

        current = connection.execute(
            "SELECT profile, revision, updated_at, state_digest "
            "FROM runtime_permission_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if current is None:
            raise RuntimeSnapshotError("current permission authority is missing")
        try:
            current_updated_at = datetime.fromisoformat(str(current["updated_at"]))
        except (TypeError, ValueError) as error:
            raise RuntimeSnapshotError(
                "current permission authority timestamp is invalid"
            ) from error
        if current_updated_at.tzinfo is None:
            raise RuntimeSnapshotError(
                "current permission authority timestamp is not timezone-aware"
            )
        if (
            current["profile"] != projection.profile
            or int(current["revision"]) != projection.revision
            or current_updated_at.astimezone(UTC)
            != projection.updated_at.astimezone(UTC)
        ):
            raise RuntimeSnapshotStale(
                "prepared Turn permission snapshot is no longer current"
            )
        state_digest = current["state_digest"]
        if (
            not isinstance(state_digest, str)
            or len(state_digest) != 64
            or any(character not in "0123456789abcdef" for character in state_digest)
        ):
            raise RuntimeSnapshotError(
                "current permission authority digest is invalid"
            )
        ledger = connection.execute(
            "SELECT profile, revision, created_at, state_digest "
            "FROM permission_state_ledger WHERE account_id = ? AND revision = ?",
            (account_id, projection.revision),
        ).fetchone()
        if (
            ledger is None
            or ledger["profile"] != current["profile"]
            or ledger["created_at"] != current["updated_at"]
            or ledger["state_digest"] != state_digest
        ):
            raise RuntimeSnapshotError(
                "current permission authority is not ledger-backed"
            )
        return projection

    def validate_turn_context_in_transaction(
        self,
        connection: sqlite3.Connection,
        context: TurnSnapshotContext,
        *,
        account_id: str,
        expected_intent: str,
        expected_agent_model_id: str,
        expected_image_model_id: str,
    ) -> None:
        """Fail closed unless a prepared context is intact and still authoritative.

        Snapshot capture may need to persist capability/config facts before the
        caller opens its product transaction.  This method is the fencing check:
        it runs inside that product transaction, verifies every immutable fact,
        and compares the permission fact with the current durable authority row.
        A concurrent permission revocation therefore commits either before this
        check (and rejects the stale context) or after the new Turn is accepted.
        """

        if not connection.in_transaction:
            raise RuntimeError(
                "turn snapshot validation requires an active transaction"
            )
        if not account_id:
            raise ValueError("turn snapshot validation requires an account_id")
        if not expected_intent:
            raise ValueError("turn snapshot validation requires an expected intent")
        if not expected_agent_model_id:
            raise ValueError(
                "turn snapshot validation requires an expected Agent model"
            )
        if not expected_image_model_id:
            raise ValueError(
                "retouch snapshot validation requires an expected image model"
            )

        self.validate_extension_snapshot_in_transaction(
            connection, context.extension_snapshot_id
        )

        self.validate_model_selection_in_transaction(
            connection,
            context,
            expected_agent_model_id=expected_agent_model_id,
            expected_image_model_id=expected_image_model_id,
        )

        config = self._runtime_snapshot_in_transaction(
            connection, context.config_snapshot_id, expected_kind="config"
        )
        permission_projection = (
            self.validate_permission_snapshot_current_in_transaction(
                connection,
                context.permission_snapshot_id,
                account_id=account_id,
            )
        )

        if (
            config.payload.get("model_catalog_snapshot_id")
            != context.model_catalog_snapshot_id
        ):
            raise RuntimeSnapshotError(
                "config snapshot does not bind the requested model catalog"
            )
        if config.payload.get("extension_snapshot_id") != context.extension_snapshot_id:
            raise RuntimeSnapshotError(
                "config snapshot does not bind the requested extension catalog"
            )

        capability = connection.execute(
            "SELECT * FROM capability_snapshots WHERE snapshot_id = ?",
            (context.capability_snapshot_id,),
        ).fetchone()
        if capability is None:
            raise RuntimeSnapshotNotFound(context.capability_snapshot_id)
        capability_payload_json = str(capability["payload_json"])
        capability_digest = hashlib.sha256(
            capability_payload_json.encode("utf-8")
        ).hexdigest()
        if capability_digest != capability["payload_sha256"]:
            raise RuntimeSnapshotError("capability snapshot digest is invalid")
        try:
            capability_payload = json_loads(capability_payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeSnapshotError(
                "capability snapshot JSON is invalid"
            ) from error
        if not isinstance(capability_payload, dict):
            raise RuntimeSnapshotError(
                "capability snapshot payload is not an object"
            )
        if (
            capability["policy_snapshot_id"]
            != context.permission_snapshot_id
            or capability_payload.get("policy_snapshot_id")
            != context.permission_snapshot_id
        ):
            raise RuntimeSnapshotStale(
                "capability snapshot is not governed by the current permission fact"
            )
        if (
            capability["intent"] != expected_intent
            or capability_payload.get("intent") != expected_intent
            or capability_payload.get("snapshot_id")
            != context.capability_snapshot_id
        ):
            raise RuntimeSnapshotError(
                "capability snapshot does not bind the retouch Turn intent"
            )
        decisions = capability_payload.get("decisions")
        imagegen = next(
            (
                decision
                for decision in decisions
                if isinstance(decision, dict)
                and decision.get("tool_id") == "imagegen"
            ),
            None,
        ) if isinstance(decisions, list) else None
        if imagegen is None:
            raise RuntimeSnapshotError(
                "capability snapshot is missing the governed image editor"
            )
        if imagegen.get("eligible") is not True:
            raise RuntimeSnapshotStale(
                "image editing is unavailable under the current capability policy"
            )

    def validate_extension_snapshot_in_transaction(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        config_snapshot_id: str | None = None,
    ) -> None:
        if not connection.in_transaction:
            raise RuntimeError("extension snapshot validation requires an active transaction")
        row = connection.execute(
            "SELECT payload_json, payload_sha256 FROM extension_catalog_snapshots "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise RuntimeSnapshotNotFound(snapshot_id)
        payload_json = str(row["payload_json"])
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if row["payload_sha256"] != digest or snapshot_id != "ext_" + digest:
            raise RuntimeSnapshotError("extension catalog snapshot digest is invalid")
        try:
            payload = json_loads(payload_json, {})
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeSnapshotError("extension catalog snapshot JSON is invalid") from error
        if (
            not isinstance(payload, Mapping)
            or payload.get("contract_version") != "1.0"
            or not isinstance(payload.get("items"), list)
        ):
            raise RuntimeSnapshotError("extension catalog snapshot contract is invalid")
        if config_snapshot_id is not None:
            config = self._runtime_snapshot_in_transaction(
                connection, config_snapshot_id, expected_kind="config"
            )
            if config.payload.get("extension_snapshot_id") != snapshot_id:
                raise RuntimeSnapshotError(
                    "config snapshot does not bind the extension catalog"
                )

    @classmethod
    def _runtime_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        expected_kind: str,
    ) -> RuntimeSnapshot:
        row = connection.execute(
            "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise RuntimeSnapshotNotFound(snapshot_id)
        snapshot = cls._from_row(row)
        if snapshot.kind != expected_kind:
            raise RuntimeSnapshotError(
                f"runtime snapshot {snapshot_id!r} has the wrong kind"
            )
        return snapshot

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RuntimeSnapshot:
        payload_json = str(row["payload_json"])
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise RuntimeSnapshotError("stored runtime snapshot digest is invalid")
        try:
            payload = json_loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeSnapshotError("stored runtime snapshot JSON is invalid") from error
        if not isinstance(payload, dict):
            raise RuntimeSnapshotError("stored runtime snapshot payload is not an object")
        return RuntimeSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            kind=str(row["kind"]),
            payload=payload,
            payload_sha256=digest,
            created_at=str(row["created_at"]),
        )
