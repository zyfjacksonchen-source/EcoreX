"""Durable immutable capability snapshots used by Turn replay and audit."""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Iterator, Mapping, Any

from .models import (
    CapabilityDecision,
    CapabilityPlan,
    Exposure,
    SandboxLevel,
    ToolProviderProvenance,
)

if TYPE_CHECKING:
    from ecorex.runtime.database import SQLiteDatabase


class CapabilitySnapshotError(RuntimeError):
    pass


class CapabilitySnapshotConflict(CapabilitySnapshotError):
    pass


class CapabilitySnapshotNotFound(CapabilitySnapshotError):
    pass


class CapabilitySnapshotRepository:
    """Append-only snapshot store that can share the Runtime WAL database."""

    def __init__(self, database: "SQLiteDatabase | str | Path") -> None:
        # Import lazily so the capability package remains importable while the
        # Runtime package is still assembling its composition graph.
        from ecorex.runtime.database import SQLiteDatabase

        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.database_path = self.database.path

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save(self, plan: CapabilityPlan) -> CapabilityPlan:
        payload = json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json, payload_sha256 FROM capability_snapshots "
                "WHERE snapshot_id = ?",
                (plan.snapshot_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] != payload
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise CapabilitySnapshotConflict(
                        "capability snapshot ID was reused with different content"
                    )
                return plan
            connection.execute(
                "INSERT INTO capability_snapshots("
                "snapshot_id, policy_snapshot_id, intent, payload_json, payload_sha256"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    plan.snapshot_id,
                    plan.policy_snapshot_id,
                    plan.intent,
                    payload,
                    payload_sha256,
                ),
            )
        return plan

    def get(self, snapshot_id: str) -> CapabilityPlan:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM capability_snapshots "
                "WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise CapabilitySnapshotNotFound(
                f"unknown capability snapshot: {snapshot_id!r}"
            )
        try:
            actual_digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
            if actual_digest != row["payload_sha256"]:
                raise CapabilitySnapshotError("stored capability snapshot digest is invalid")
            raw = json.loads(row["payload_json"])
            return _plan_from_dict(raw)
        except CapabilitySnapshotError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapabilitySnapshotError("stored capability snapshot is invalid") from exc

    def count(self) -> int:
        with self._reader() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM capability_snapshots").fetchone()[0])


def _plan_from_dict(raw: Mapping[str, Any]) -> CapabilityPlan:
    decisions_raw = raw["decisions"]
    if not isinstance(decisions_raw, list):
        raise TypeError("capability decisions must be a list")
    decisions = tuple(_decision_from_dict(item) for item in decisions_raw if isinstance(item, Mapping))
    if len(decisions) != len(decisions_raw):
        raise TypeError("capability decision must be an object")
    selected_model_capabilities_raw = raw.get("selected_model_capabilities")
    if selected_model_capabilities_raw is not None:
        if not isinstance(selected_model_capabilities_raw, Mapping):
            raise TypeError("selected model capabilities must be an object")
        selected_model_capabilities: dict[str, frozenset[str]] = {}
        for modality, capabilities in selected_model_capabilities_raw.items():
            if not isinstance(modality, str) or not isinstance(capabilities, list):
                raise TypeError("selected model capabilities are invalid")
            if not all(isinstance(value, str) for value in capabilities):
                raise TypeError("selected model capabilities are invalid")
            selected_model_capabilities[modality] = frozenset(capabilities)
    else:
        selected_model_capabilities = None
    return CapabilityPlan(
        snapshot_id=str(raw["snapshot_id"]),
        policy_snapshot_id=str(raw["policy_snapshot_id"]),
        intent=str(raw["intent"]),
        decisions=decisions,
        catalog_digest=str(raw.get("catalog_digest", "")),
        unresolved_explicit=tuple(str(value) for value in raw.get("unresolved_explicit", [])),
        routing_policy_id=str(raw.get("routing_policy_id", "routing.none")),
        routing_policy_version=str(raw.get("routing_policy_version", "0.0.0")),
        routing_policy_digest=str(raw.get("routing_policy_digest", "")),
        discovery_policy_id=str(raw.get("discovery_policy_id", "discovery.none")),
        discovery_policy_version=str(raw.get("discovery_policy_version", "0.0.0")),
        discovery_policy_digest=str(raw.get("discovery_policy_digest", "")),
        selected_model_capabilities=selected_model_capabilities,
    )


def _decision_from_dict(item: Mapping[str, Any]) -> CapabilityDecision:
    eligible = item["eligible"]
    requires_approval = item["requires_approval"]
    score = item["score"]
    reasons = item["reason_codes"]
    matched_evidence = item.get("matched_evidence", [])
    suppression_reasons = item.get("suppression_reasons", [])
    provider = item.get("provider")
    if not isinstance(eligible, bool) or not isinstance(requires_approval, bool):
        raise TypeError("capability decision booleans are invalid")
    if isinstance(score, bool) or not isinstance(score, int):
        raise TypeError("capability decision score is invalid")
    if not isinstance(reasons, list) or not all(isinstance(value, str) for value in reasons):
        raise TypeError("capability decision reason codes are invalid")
    if not isinstance(matched_evidence, list) or not all(
        isinstance(value, str) for value in matched_evidence
    ):
        raise TypeError("capability decision matched evidence is invalid")
    if not isinstance(suppression_reasons, list) or not all(
        isinstance(value, str) for value in suppression_reasons
    ):
        raise TypeError("capability decision suppression reasons are invalid")
    if not isinstance(provider, Mapping):
        raise TypeError("capability decision provider provenance is invalid")
    return CapabilityDecision(
        tool_id=str(item["tool_id"]),
        tool_version=str(item["tool_version"]),
        exposure=Exposure(str(item["exposure"])),
        eligible=eligible,
        requires_approval=requires_approval,
        effective_sandbox=SandboxLevel(str(item["effective_sandbox"])),
        score=score,
        reason_codes=tuple(reasons),
        matched_evidence=tuple(matched_evidence),
        suppression_reasons=tuple(suppression_reasons),
        provider=ToolProviderProvenance.from_dict(provider),
    )
