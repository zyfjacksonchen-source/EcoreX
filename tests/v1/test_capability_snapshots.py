from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from ecorex.capabilities import (
    CapabilityPlan,
    CapabilityRegistry,
    CapabilityService,
    CapabilitySnapshotConflict,
    CapabilitySnapshotError,
    CapabilitySnapshotRepository,
    ExecutionPolicy,
    Exposure,
    RuntimeAvailability,
    StaleCapabilitySnapshotError,
    ToolSpec,
    builtin_capability_registry,
)
from ecorex.runtime.errors import SchemaVersionError


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        (
            ToolSpec(
                tool_id="read",
                version="1.0.0",
                display_name="Read",
                description="Read workspace files",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                default_exposure=Exposure.DIRECT,
            ),
        )
    )


def test_snapshot_survives_service_restart_and_authorizes_replay(tmp_path: Path) -> None:
    repository = CapabilitySnapshotRepository(tmp_path / "runtime.db")
    first = CapabilityService(
        _registry(),
        handlers={"read": lambda arguments: {"path": arguments["path"]}},
        snapshot_repository=repository,
    )
    plan = first.create_plan(
        intent="read the plan",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_1"),
    )
    assert repository.count() == 1

    restarted = CapabilityService(
        _registry(),
        handlers={"read": lambda arguments: {"path": arguments["path"]}},
        snapshot_repository=CapabilitySnapshotRepository(tmp_path / "runtime.db"),
    )
    result = asyncio.run(
        restarted.tool_call(
            plan.snapshot_id,
            "read",
            {"path": "brief.docx"},
            policy_snapshot_id="perm_1",
        )
    )
    assert result.value == {"path": "brief.docx"}
    assert result.record.capability_snapshot_id == plan.snapshot_id


def test_routing_score_evidence_and_suppression_survive_immutable_replay(
    tmp_path: Path,
) -> None:
    repository = CapabilitySnapshotRepository(tmp_path / "runtime.db")
    plan = CapabilityService(
        builtin_capability_registry(),
        snapshot_repository=repository,
    ).create_plan(
        intent="用参考图改图，但不要生成一张新图片",
        availability=RuntimeAvailability(
            platform="windows",
            installed_packs=frozenset({"image"}),
        ),
        policy=ExecutionPolicy(snapshot_id="perm_route_replay"),
    )

    replayed = CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
        plan.snapshot_id
    )
    routed = replayed.decision("imagegen")

    assert replayed.to_dict() == plan.to_dict()
    assert replayed.routing_policy_id == "ecorex.intent-routing"
    assert replayed.routing_policy_version == "1.6.0"
    assert len(replayed.routing_policy_digest) == 64
    assert replayed.discovery_policy_id == "ecorex.discovery"
    assert replayed.discovery_policy_version == "1.2.0"
    assert len(replayed.discovery_policy_digest) == 64
    assert routed is not None
    assert routed.score > 0
    assert any(
        evidence.startswith("intent_route:media.image.edit@")
        for evidence in routed.matched_evidence
    )
    assert any(
        reason.startswith("intent_route:media.image.create@")
        for reason in routed.suppression_reasons
    )


def test_snapshot_id_is_immutable_and_rows_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    repository = CapabilitySnapshotRepository(path)
    service = CapabilityService(_registry(), snapshot_repository=repository)
    plan = service.create_plan(
        intent="read",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_1"),
    )
    conflicting = CapabilityPlan(
        snapshot_id=plan.snapshot_id,
        policy_snapshot_id=plan.policy_snapshot_id,
        intent="different intent",
        decisions=plan.decisions,
    )
    with pytest.raises(CapabilitySnapshotConflict):
        repository.save(conflicting)

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE capability_snapshots SET intent = 'tampered' WHERE snapshot_id = ?",
                (plan.snapshot_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM capability_snapshots WHERE snapshot_id = ?",
                (plan.snapshot_id,),
            )


def test_restart_never_executes_a_new_catalog_contract_with_an_old_plan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    original = CapabilityService(
        CapabilityRegistry(
            (
                ToolSpec(
                    tool_id="demo",
                    version="1.0.0",
                    display_name="Demo",
                    description="Read-only demo",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    default_exposure=Exposure.DIRECT,
                ),
            )
        ),
        snapshot_repository=CapabilitySnapshotRepository(path),
    )
    plan = original.create_plan(
        intent="demo",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_catalog_v1"),
    )
    changed = CapabilityService(
        CapabilityRegistry(
            (
                ToolSpec(
                    tool_id="demo",
                    version="2.0.0",
                    display_name="Demo",
                    description="Changed demo contract",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    default_exposure=Exposure.DIRECT,
                ),
            )
        ),
        handlers={"demo": lambda arguments: {"executed": True}},
        snapshot_repository=CapabilitySnapshotRepository(path),
    )

    with pytest.raises(StaleCapabilitySnapshotError, match="catalog changed"):
        asyncio.run(
            changed.tool_call(
                plan.snapshot_id,
                "demo",
                {},
                policy_snapshot_id="perm_catalog_v1",
            )
        )


def test_sealed_tool_schema_is_a_deep_immutable_catalog_fact() -> None:
    source_schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    }
    spec = ToolSpec(
        tool_id="immutable-schema",
        version="1.0.0",
        display_name="Immutable schema",
        description="Prove nested provider metadata cannot drift.",
        input_schema=source_schema,
        output_schema={"type": "object"},
        default_exposure=Exposure.DIRECT,
    )
    registry = CapabilityRegistry((spec,))
    CapabilityService(registry)
    digest = registry.digest

    source_schema["properties"]["title"]["minLength"] = 99

    assert spec.to_dict()["input_schema"]["properties"]["title"]["minLength"] == 1
    assert registry.digest == digest
    with pytest.raises(TypeError):
        spec.input_schema["properties"]["title"]["minLength"] = 2


def test_corrupt_snapshot_schema_fails_closed_without_startup_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    repository = CapabilitySnapshotRepository(path)
    service = CapabilityService(_registry(), snapshot_repository=repository)
    plan = service.create_plan(
        intent="read",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_1"),
    )
    tampered_payload = json.dumps({"snapshot_id": plan.snapshot_id})
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER capability_snapshots_no_update")
        connection.execute(
            "UPDATE capability_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            (tampered_payload, plan.snapshot_id),
        )

    with pytest.raises(SchemaVersionError, match="product schema objects are missing"):
        CapabilitySnapshotRepository(path)

    with sqlite3.connect(path) as connection:
        trigger = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'capability_snapshots_no_update'"
        ).fetchone()
        stored_payload = connection.execute(
            "SELECT payload_json FROM capability_snapshots WHERE snapshot_id = ?",
            (plan.snapshot_id,),
        ).fetchone()
    assert trigger is None
    assert stored_payload is not None and stored_payload[0] == tampered_payload


def test_corrupt_snapshot_payload_fails_closed_with_intact_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    repository = CapabilitySnapshotRepository(path)
    service = CapabilityService(_registry(), snapshot_repository=repository)
    plan = service.create_plan(
        intent="read",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_1"),
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER capability_snapshots_no_update;
            UPDATE capability_snapshots
            SET payload_json = '{"snapshot_id":"tampered"}';
            CREATE TRIGGER capability_snapshots_no_update
            BEFORE UPDATE ON capability_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'capability snapshots are immutable');
            END;
            """
        )
    restarted = CapabilityService(
        _registry(),
        handlers={"read": lambda arguments: arguments},
        snapshot_repository=CapabilitySnapshotRepository(path),
    )
    with pytest.raises(CapabilitySnapshotError, match="stored capability snapshot.*invalid"):
        asyncio.run(
            restarted.tool_call(
                plan.snapshot_id,
                "read",
                {},
                policy_snapshot_id="perm_1",
            )
        )


def test_development_snapshot_schema_requires_signed_migration_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE capability_snapshots("
            "snapshot_id TEXT PRIMARY KEY, policy_snapshot_id TEXT NOT NULL, "
            "intent TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        plan = CapabilityService(_registry()).planner.plan(
            intent="read",
            availability=RuntimeAvailability(platform="windows"),
            policy=ExecutionPolicy(snapshot_id="perm_1"),
        )
        connection.execute(
            "INSERT INTO capability_snapshots VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                plan.snapshot_id,
                plan.policy_snapshot_id,
                plan.intent,
                json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    before = path.read_bytes()
    with pytest.raises(SchemaVersionError, match="version table is missing"):
        CapabilitySnapshotRepository(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(capability_snapshots)")
        }
        payload = connection.execute(
            "SELECT payload_json FROM capability_snapshots WHERE snapshot_id = ?",
            (plan.snapshot_id,),
        ).fetchone()
    assert "payload_sha256" not in columns
    assert payload is not None and json.loads(payload[0]) == plan.to_dict()
