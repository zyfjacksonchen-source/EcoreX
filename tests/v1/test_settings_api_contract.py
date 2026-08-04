from __future__ import annotations

from pydantic import ValidationError
import pytest

from ecorex.memory.api import MemoryMutationResponse, MemorySnapshotResponse
from ecorex.migration.api import MigrationQuarantineResponse
from ecorex.observability.system_api import (
    SystemHealthPublicResponse,
    SystemHealthTechnicalResponse,
    SystemMetricHistoryResponse,
)
from ecorex.output.api import (
    OutputLocationCatalogResponse,
    OutputMaterializationResponse,
    OutputPreferenceResponse,
)
from ecorex.runtime import RuntimeSettings, create_app


NOW = "2026-07-15T08:00:00+00:00"


def _reset(*, status: str = "active", can_undo: bool = True) -> dict:
    return {
        "reset_id": "memreset_01K00000000000000000000000",
        "status": status,
        "affected_records": 2,
        "affected_files": 1,
        "created_at": NOW,
        "undo_until": "2026-07-16T08:00:00+00:00",
        "updated_at": NOW,
        "can_undo": can_undo,
    }


def _memory(*, resettable_count: int = 3, latest_reset: dict | None = None) -> dict:
    return {
        "revision": 2,
        "active_learned_records": 2,
        "active_user_files": 1,
        "factory_records": 1,
        "tombstoned_records": 0,
        "tombstoned_files": 0,
        "resettable_count": resettable_count,
        "latest_reset": latest_reset,
    }


def _components(*, overall: str = "healthy") -> tuple[dict, list[dict]]:
    components = [
        {
            "component_id": "runtime",
            "label": "运行响应",
            "status": overall,
            "message": "运行正常。",
        }
    ]
    return {
        "sample_id": "syssample_01K00000000000000000000000",
        "overall": overall,
        "summary": "e-Mate 运行正常",
        "components": components,
        "sampled_at": NOW,
    }, components


def test_settings_response_models_reject_extra_fields_and_cross_field_drift() -> None:
    reset = _reset()
    memory = _memory(latest_reset=reset)
    assert MemoryMutationResponse.model_validate({"memory": memory, "reset": reset})

    with pytest.raises(ValidationError):
        MemorySnapshotResponse.model_validate(_memory(resettable_count=999))
    with pytest.raises(ValidationError):
        MemoryMutationResponse.model_validate(
            {"memory": memory, "reset": {**reset, "reset_id": "different"}}
        )
    with pytest.raises(ValidationError):
        MemorySnapshotResponse.model_validate(
            {**_memory(), "implementation_path": "secret"}
        )

    deleted = {
        "status": "deleted",
        "entry_count": 2,
        "can_delete": False,
        "deleted_at": NOW,
        "items": [{"kind": "api_key", "origin": "product_configuration", "count": 2}],
    }
    assert MigrationQuarantineResponse.model_validate(deleted).status == "deleted"
    with pytest.raises(ValidationError):
        MigrationQuarantineResponse.model_validate({**deleted, "can_delete": True})
    with pytest.raises(ValidationError):
        MigrationQuarantineResponse.model_validate(
            {**deleted, "items": [{**deleted["items"][0], "key_path": "secret"}]}
        )
    with pytest.raises(ValidationError):
        MigrationQuarantineResponse.model_validate(
            {
                **deleted,
                "entry_count": 4,
                "items": [deleted["items"][0], deleted["items"][0]],
            }
        )


def test_output_and_system_response_models_enforce_authoritative_invariants() -> None:
    locations = {
        "items": [
            {"alias": "documents", "available": True},
            {"alias": "downloads", "available": True},
            {"alias": "workspace", "available": False},
        ]
    }
    assert OutputLocationCatalogResponse.model_validate(locations)
    with pytest.raises(ValidationError):
        OutputLocationCatalogResponse.model_validate(
            {
                "items": [
                    locations["items"][0],
                    locations["items"][0],
                    locations["items"][2],
                ]
            }
        )

    materialization = {
        "materialization_id": f"mat_{'1' * 64}",
        "artifact_id": "artifact_01K00000000000000000000000",
        "revision_id": "revision_01K00000000000000000000000",
        "output_policy_snapshot_id": f"outpol_{'2' * 64}",
        "location_alias": "documents",
        "display_name": "报告_20260715-1600_01.pdf",
        "sha256": "3" * 64,
        "size_bytes": 42,
        "status": "completed",
        "reused_existing": False,
        "created_at": NOW,
        "completed_at": NOW,
    }
    assert OutputMaterializationResponse.model_validate(materialization)
    with pytest.raises(ValidationError):
        OutputMaterializationResponse.model_validate(
            {**materialization, "completed_at": None}
        )

    public, _ = _components()
    assert SystemHealthPublicResponse.model_validate(public)
    with pytest.raises(ValidationError):
        SystemHealthPublicResponse.model_validate({**public, "overall": "critical"})
    technical = {
        **public,
        "metrics": {"runtime": {}, "process": {}, "storage": {}, "services": {}},
    }
    assert SystemHealthTechnicalResponse.model_validate(technical)
    assert SystemMetricHistoryResponse.model_validate({"items": [technical]})
    with pytest.raises(ValidationError):
        SystemHealthTechnicalResponse.model_validate(
            {**technical, "metrics": {"runtime": {}}}
        )
    with pytest.raises(ValidationError):
        SystemHealthTechnicalResponse.model_validate(
            {
                **technical,
                "metrics": {
                    "runtime": 1,
                    "process": {},
                    "storage": {},
                    "services": {},
                },
            }
        )


def test_openapi_pins_every_settings_json_route_to_an_authoritative_model(
    tmp_path,
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token="r" * 32,
            csrf_token="c" * 32,
            webui_origins=("http://testserver",),
        )
    )
    openapi = app.openapi()
    expected = {
        ("/api/v1/memory", "get"): "MemorySnapshotResponse",
        ("/api/v1/memory/reset", "post"): "MemoryMutationResponse",
        ("/api/v1/memory/resets/{reset_id}/undo", "post"): "MemoryMutationResponse",
        ("/api/v1/migration/quarantine", "get"): "MigrationQuarantineResponse",
        ("/api/v1/migration/quarantine/delete", "post"): "MigrationQuarantineResponse",
        ("/api/v1/output/locations", "get"): "OutputLocationCatalogResponse",
        ("/api/v1/output/preference", "get"): "OutputPreferenceResponse",
        ("/api/v1/output/preference", "put"): "OutputPreferenceResponse",
        ("/api/v1/output/locations/pick", "post"): "OutputPreferenceResponse",
        (
            "/api/v1/output/artifacts/{artifact_id}/materialize",
            "post",
        ): "OutputMaterializationResponse",
        (
            "/api/v1/output/materializations/{materialization_id}",
            "get",
        ): "OutputMaterializationResponse",
        ("/api/v1/system/metrics", "get"): "SystemMetricHistoryResponse",
    }
    for (path, method), name in expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{name}"}

    health = openapi["paths"]["/api/v1/system/health"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert health["anyOf"] == [
        {"$ref": "#/components/schemas/SystemHealthPublicResponse"},
        {"$ref": "#/components/schemas/SystemHealthTechnicalResponse"},
    ]
    for name in {
        *expected.values(),
        "SystemHealthPublicResponse",
        "SystemHealthTechnicalResponse",
    }:
        assert openapi["components"]["schemas"][name]["additionalProperties"] is False


def test_output_preference_contract_never_contains_a_host_path() -> None:
    response = OutputPreferenceResponse.model_validate(
        {
            "account_id": "managed-account",
            "location_alias": "workspace",
            "revision": 1,
            "output_policy_snapshot_id": f"outpol_{'a' * 64}",
            "updated_at": NOW,
        }
    )
    assert "path" not in response.model_dump_json()
