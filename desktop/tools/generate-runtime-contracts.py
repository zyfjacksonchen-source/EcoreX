#!/usr/bin/env python3
"""Generate the WebUI's deterministic Runtime contract manifest.

The TypeScript UI keeps ergonomic hand-authored projections, but their wire
boundary is pinned to schemas emitted by the authoritative Python models.  A
release/typecheck must run this file with ``--check`` so backend contract drift
cannot be accepted without regenerating and reviewing the WebUI boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pydantic import TypeAdapter  # noqa: E402

from ecorex.artifacts import (  # noqa: E402
    PUBLIC_ARTIFACT_FAMILIES,
    PUBLIC_ARTIFACT_VISIBILITIES,
    ArtifactProjection,
)
from ecorex.protocol import (  # noqa: E402
    BootstrapResponse,
    ConnectorLoginBeginResponse,
    ConnectorLoginCancelResponse,
    ConnectorLoginCheckResponse,
    ConversationUsageProjection,
    CreateTurnRequest,
    EventEnvelope,
    InteractionMutationResponse,
    InteractionProjection,
    InteractionRequest,
    InputAttachmentProjection,
    ItemProjection,
    JobProjection,
    ProjectListResponse,
    QueueTurnRequest,
    ReplaceTurnRequest,
    ReplaceTurnResponse,
    SteerTurnRequest,
    RespondInteractionRequest,
    ThreadListResponse,
    ThreadProjection,
    ThreadProjectionResponse,
    TurnMutationResponse,
    TurnProjection,
)
from ecorex.artifacts.api import RetouchBody, RetouchWorkspaceSubmitBody  # noqa: E402
from ecorex.artifacts.wire import (  # noqa: E402
    ArtifactExternalActionResponse,
    ArtifactListResponse,
    ArtifactProjectionResponse,
    FeedbackProjectionResponse,
    RetouchJobResponse,
    RetouchWorkspaceResponse,
)
from ecorex.memory.api import MemoryMutationResponse, MemorySnapshotResponse  # noqa: E402
from ecorex.migration.api import MigrationQuarantineResponse  # noqa: E402
from ecorex.observability.system_api import (  # noqa: E402
    SystemHealthPublicResponse,
    SystemHealthTechnicalResponse,
    SystemMetricHistoryResponse,
)
from ecorex.output.api import (  # noqa: E402
    OutputLocationCatalogResponse,
    OutputMaterializationResponse,
    OutputPreferenceResponse,
)


OUTPUT_DIRECTORY = REPOSITORY_ROOT / "desktop" / "src" / "v1" / "api"
SCHEMA_OUTPUT = OUTPUT_DIRECTORY / "runtime-contract.schema.json"
TYPESCRIPT_OUTPUT = OUTPUT_DIRECTORY / "generatedRuntimeContract.ts"
PROJECTION_TYPESCRIPT_OUTPUT = (
    OUTPUT_DIRECTORY / "generatedRuntimeProjectionContract.ts"
)
SETTINGS_TYPESCRIPT_OUTPUT = OUTPUT_DIRECTORY / "generatedSettingsRuntimeContract.ts"
ARTIFACT_TYPESCRIPT_OUTPUT = OUTPUT_DIRECTORY / "generatedArtifactRuntimeContract.ts"

SCHEMA_VERSION = 1
PROJECTION_CONTRACT_NAMES = frozenset(
    {
        "ConnectorLoginBeginResponse",
        "ConnectorLoginCancelResponse",
        "ConnectorLoginCheckResponse",
        "InteractionMutationResponse",
        "InteractionProjection",
        "ItemProjection",
        "JobProjection",
        "ReplaceTurnResponse",
        "ThreadListResponse",
        "ThreadProjection",
        "ThreadProjectionResponse",
        "TurnMutationResponse",
        "TurnProjection",
    }
)
SETTINGS_CONTRACT_NAMES = frozenset(
    {
        "MemoryMutationResponse",
        "MemorySnapshotResponse",
        "MigrationQuarantineResponse",
        "OutputLocationCatalogResponse",
        "OutputMaterializationResponse",
        "OutputPreferenceResponse",
        "SystemHealthPublicResponse",
        "SystemHealthTechnicalResponse",
        "SystemMetricHistoryResponse",
    }
)
ARTIFACT_CONTRACT_NAMES = frozenset(
    {
        "ArtifactExternalActionResponse",
        "ArtifactListResponse",
        "ArtifactProjectionResponse",
        "FeedbackProjectionResponse",
        "RetouchJobResponse",
        "RetouchWorkspaceResponse",
    }
)
NESTED_WIRE_CONTRACT_NAMES = frozenset(
    {
        "ArtifactProjection",
        "MemoryMutationResponse",
        "MemorySnapshotResponse",
        "MigrationQuarantineResponse",
        "OutputLocationCatalogResponse",
        "SystemHealthPublicResponse",
        "SystemHealthTechnicalResponse",
        "SystemMetricHistoryResponse",
    }
)


def _contract_schemas() -> dict[str, dict[str, Any]]:
    return {
        "ArtifactProjection": TypeAdapter(ArtifactProjection).json_schema(),
        "ArtifactExternalActionResponse": ArtifactExternalActionResponse.model_json_schema(),
        "ArtifactListResponse": ArtifactListResponse.model_json_schema(),
        "ArtifactProjectionResponse": ArtifactProjectionResponse.model_json_schema(),
        "BootstrapResponse": BootstrapResponse.model_json_schema(),
        "ConnectorLoginBeginResponse": ConnectorLoginBeginResponse.model_json_schema(),
        "ConnectorLoginCancelResponse": ConnectorLoginCancelResponse.model_json_schema(),
        "ConnectorLoginCheckResponse": ConnectorLoginCheckResponse.model_json_schema(),
        "ConversationUsageProjection": ConversationUsageProjection.model_json_schema(),
        "EventEnvelope": EventEnvelope.model_json_schema(),
        "FeedbackProjectionResponse": FeedbackProjectionResponse.model_json_schema(),
        "InteractionRequest": InteractionRequest.model_json_schema(),
        "InteractionMutationResponse": InteractionMutationResponse.model_json_schema(),
        "InteractionProjection": InteractionProjection.model_json_schema(),
        "InputAttachmentProjection": InputAttachmentProjection.model_json_schema(),
        "ItemProjection": ItemProjection.model_json_schema(),
        "JobProjection": JobProjection.model_json_schema(),
        "MemoryMutationResponse": MemoryMutationResponse.model_json_schema(),
        "MemorySnapshotResponse": MemorySnapshotResponse.model_json_schema(),
        "MigrationQuarantineResponse": MigrationQuarantineResponse.model_json_schema(),
        "OutputLocationCatalogResponse": OutputLocationCatalogResponse.model_json_schema(),
        "OutputMaterializationResponse": OutputMaterializationResponse.model_json_schema(),
        "OutputPreferenceResponse": OutputPreferenceResponse.model_json_schema(),
        "ProjectListResponse": ProjectListResponse.model_json_schema(),
        "RespondInteractionRequest": RespondInteractionRequest.model_json_schema(),
        "ThreadProjectionResponse": ThreadProjectionResponse.model_json_schema(),
        "TurnProjection": TurnProjection.model_json_schema(),
        "TurnMutationResponse": TurnMutationResponse.model_json_schema(),
        "CreateTurnRequest": CreateTurnRequest.model_json_schema(),
        "SteerTurnRequest": SteerTurnRequest.model_json_schema(),
        "QueueTurnRequest": QueueTurnRequest.model_json_schema(),
        "ReplaceTurnRequest": ReplaceTurnRequest.model_json_schema(),
        "ReplaceTurnResponse": ReplaceTurnResponse.model_json_schema(),
        "ThreadListResponse": ThreadListResponse.model_json_schema(),
        "ThreadProjection": ThreadProjection.model_json_schema(),
        "RetouchBody": RetouchBody.model_json_schema(),
        "RetouchJobResponse": RetouchJobResponse.model_json_schema(),
        "RetouchWorkspaceSubmitBody": RetouchWorkspaceSubmitBody.model_json_schema(),
        "RetouchWorkspaceResponse": RetouchWorkspaceResponse.model_json_schema(),
        "SystemHealthPublicResponse": SystemHealthPublicResponse.model_json_schema(),
        "SystemHealthTechnicalResponse": SystemHealthTechnicalResponse.model_json_schema(),
        "SystemMetricHistoryResponse": SystemMetricHistoryResponse.model_json_schema(),
    }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _pretty_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _enum_values(schema: dict[str, Any], name: str) -> list[str]:
    definition = schema.get("$defs", {}).get(name)
    if not isinstance(definition, dict) or not isinstance(definition.get("enum"), list):
        raise RuntimeError(f"backend schema no longer exposes enum {name!r}")
    values = definition["enum"]
    if not all(isinstance(value, str) for value in values):
        raise RuntimeError(f"backend enum {name!r} contains a non-string value")
    return values


def _all_wire_fields(schema: dict[str, Any], name: str) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise RuntimeError(f"backend schema {name!r} has no object properties")
    # Runtime serialization includes defaulted fields. Requiring every declared
    # top-level field makes an unchanged protocol version fail closed when an
    # old/malformed server silently omits one.
    return list(properties)


def _wire_object_fields(
    schema: dict[str, Any], root_name: str, *, include_definitions: bool
) -> dict[str, list[str]]:
    """Return every fixed object shape emitted by one backend contract.

    Pydantic's ``required`` list intentionally omits fields with defaults, but
    Runtime response serialization includes those fields.  The WebUI pins the
    complete emitted shape so a stale or partially upgraded Runtime cannot
    silently leak ``undefined`` into UI state.
    """

    result = {root_name: _all_wire_fields(schema, root_name)}
    if not include_definitions:
        return result
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise RuntimeError(f"backend schema {root_name!r} has invalid definitions")
    for definition_name in sorted(definitions):
        definition = definitions[definition_name]
        if isinstance(definition, dict) and isinstance(
            definition.get("properties"), dict
        ):
            result[definition_name] = _all_wire_fields(
                definition, f"{root_name}.{definition_name}"
            )
    return result


def _definition(schema: dict[str, Any], name: str) -> dict[str, Any]:
    definition = schema.get("$defs", {}).get(name)
    if not isinstance(definition, dict):
        raise RuntimeError(f"backend schema no longer exposes definition {name!r}")
    return definition


def _property(
    schema: dict[str, Any], definition_name: str | None, field: str
) -> dict[str, Any]:
    owner = schema if definition_name is None else _definition(schema, definition_name)
    candidate = owner.get("properties", {}).get(field)
    if not isinstance(candidate, dict):
        owner_name = definition_name or "root"
        raise RuntimeError(f"backend schema {owner_name!r} has no field {field!r}")
    return candidate


def _property_enum(
    schema: dict[str, Any], definition_name: str, field: str
) -> list[str]:
    candidate = _property(schema, definition_name, field).get("enum")
    if (
        not isinstance(candidate, list)
        or not candidate
        or not all(isinstance(value, str) for value in candidate)
    ):
        raise RuntimeError(
            f"backend field {definition_name}.{field} is no longer a string enum"
        )
    return candidate


def _root_property_enum(schema: dict[str, Any], field: str) -> list[str]:
    candidate = _property(schema, None, field).get("enum")
    if (
        not isinstance(candidate, list)
        or not candidate
        or not all(isinstance(value, str) for value in candidate)
    ):
        raise RuntimeError(f"backend root field {field!r} is no longer a string enum")
    return candidate


def _property_const(
    schema: dict[str, Any], definition_name: str | None, field: str
) -> str | int:
    candidate = _property(schema, definition_name, field).get("const")
    if isinstance(candidate, bool) or not isinstance(candidate, (str, int)):
        owner_name = definition_name or "root"
        raise RuntimeError(
            f"backend field {owner_name}.{field} is no longer a string/integer literal"
        )
    return candidate


def build_outputs() -> tuple[bytes, bytes, bytes, bytes, bytes, str]:
    schemas = _contract_schemas()
    public_families = [family.value for family in PUBLIC_ARTIFACT_FAMILIES]
    public_visibilities = [
        visibility.value for visibility in PUBLIC_ARTIFACT_VISIBILITIES
    ]
    schema_document = {
        "document_type": "ecorex.web-runtime-contracts",
        "schema_version": SCHEMA_VERSION,
        "sources": {
            "ArtifactProjection": "ecorex.artifacts.models.ArtifactProjection",
            "ArtifactExternalActionResponse": "ecorex.artifacts.wire.ArtifactExternalActionResponse",
            "ArtifactListResponse": "ecorex.artifacts.wire.ArtifactListResponse",
            "ArtifactProjectionResponse": "ecorex.artifacts.wire.ArtifactProjectionResponse",
            "BootstrapResponse": "ecorex.protocol.BootstrapResponse",
            "ConnectorLoginBeginResponse": "ecorex.protocol.ConnectorLoginBeginResponse",
            "ConnectorLoginCancelResponse": "ecorex.protocol.ConnectorLoginCancelResponse",
            "ConnectorLoginCheckResponse": "ecorex.protocol.ConnectorLoginCheckResponse",
            "EventEnvelope": "ecorex.protocol.EventEnvelope",
            "FeedbackProjectionResponse": "ecorex.artifacts.wire.FeedbackProjectionResponse",
            "InteractionRequest": "ecorex.protocol.InteractionRequest",
            "InteractionMutationResponse": "ecorex.protocol.InteractionMutationResponse",
            "InteractionProjection": "ecorex.protocol.InteractionProjection",
            "InputAttachmentProjection": "ecorex.protocol.InputAttachmentProjection",
            "ItemProjection": "ecorex.protocol.ItemProjection",
            "JobProjection": "ecorex.protocol.JobProjection",
            "MemoryMutationResponse": "ecorex.memory.api.MemoryMutationResponse",
            "MemorySnapshotResponse": "ecorex.memory.api.MemorySnapshotResponse",
            "MigrationQuarantineResponse": "ecorex.migration.api.MigrationQuarantineResponse",
            "OutputLocationCatalogResponse": "ecorex.output.api.OutputLocationCatalogResponse",
            "OutputMaterializationResponse": "ecorex.output.api.OutputMaterializationResponse",
            "OutputPreferenceResponse": "ecorex.output.api.OutputPreferenceResponse",
            "ProjectListResponse": "ecorex.protocol.ProjectListResponse",
            "RespondInteractionRequest": "ecorex.protocol.RespondInteractionRequest",
            "ThreadProjectionResponse": "ecorex.protocol.ThreadProjectionResponse",
            "TurnProjection": "ecorex.protocol.TurnProjection",
            "TurnMutationResponse": "ecorex.protocol.TurnMutationResponse",
            "CreateTurnRequest": "ecorex.protocol.CreateTurnRequest",
            "SteerTurnRequest": "ecorex.protocol.SteerTurnRequest",
            "QueueTurnRequest": "ecorex.protocol.QueueTurnRequest",
            "ReplaceTurnRequest": "ecorex.protocol.ReplaceTurnRequest",
            "ReplaceTurnResponse": "ecorex.protocol.ReplaceTurnResponse",
            "ThreadListResponse": "ecorex.protocol.ThreadListResponse",
            "ThreadProjection": "ecorex.protocol.ThreadProjection",
            "RetouchBody": "ecorex.artifacts.api.RetouchBody",
            "RetouchJobResponse": "ecorex.artifacts.wire.RetouchJobResponse",
            "RetouchWorkspaceSubmitBody": "ecorex.artifacts.api.RetouchWorkspaceSubmitBody",
            "RetouchWorkspaceResponse": "ecorex.artifacts.wire.RetouchWorkspaceResponse",
            "SystemHealthPublicResponse": "ecorex.observability.system_api.SystemHealthPublicResponse",
            "SystemHealthTechnicalResponse": "ecorex.observability.system_api.SystemHealthTechnicalResponse",
            "SystemMetricHistoryResponse": "ecorex.observability.system_api.SystemMetricHistoryResponse",
        },
        "public_artifact_policy": {
            "families": public_families,
            "visibilities": public_visibilities,
        },
        "contracts": schemas,
    }
    schema_bytes = _pretty_json(schema_document)
    digest = hashlib.sha256(_canonical_json(schema_document)).hexdigest()

    artifact_schema = schemas["ArtifactProjection"]
    bootstrap_schema = schemas["BootstrapResponse"]
    event_schema = schemas["EventEnvelope"]
    runtime_schema = schemas["ThreadProjectionResponse"]
    settings_values = {
        "memoryResetStatuses": _property_enum(
            schemas["MemoryMutationResponse"],
            "MemoryResetProjectionResponse",
            "status",
        ),
        "migrationCredentialKinds": _property_enum(
            schemas["MigrationQuarantineResponse"],
            "MigrationQuarantineItemResponse",
            "kind",
        ),
        "migrationCredentialOrigins": _property_enum(
            schemas["MigrationQuarantineResponse"],
            "MigrationQuarantineItemResponse",
            "origin",
        ),
        "migrationQuarantineStatuses": _root_property_enum(
            schemas["MigrationQuarantineResponse"], "status"
        ),
        "outputLocationAliases": _property_enum(
            schemas["OutputLocationCatalogResponse"],
            "OutputLocationOptionResponse",
            "alias",
        ),
        "outputMaterializationStatuses": _root_property_enum(
            schemas["OutputMaterializationResponse"], "status"
        ),
        "systemHealthStatuses": _root_property_enum(
            schemas["SystemHealthPublicResponse"], "overall"
        ),
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "schemaSha256": digest,
        "versions": {
            "api": _property_const(bootstrap_schema, None, "api_version"),
            "eventSchema": _property_const(
                bootstrap_schema, None, "event_schema_version"
            ),
            "storageSchema": _property_const(
                bootstrap_schema, None, "storage_schema_version"
            ),
            "eventEnvelope": _property_const(event_schema, None, "schema_version"),
            "extensionContract": _property_const(
                bootstrap_schema, "ExtensionCatalogSnapshot", "contract_version"
            ),
        },
        "wireFields": {
            name: _wire_object_fields(
                schema,
                name,
                # Feature-boundary validators need every fixed nested shape.
                # Large Bootstrap/Event objects remain field-validated without
                # duplicating their complete definitions into initial Web JS.
                include_definitions=name in NESTED_WIRE_CONTRACT_NAMES,
            )
            for name, schema in schemas.items()
            # Interaction payloads are already decoded from the authoritative
            # Event/response contract at their feature boundary. Keep their
            # complete JSON Schemas in the hash-pinned schema artifact without
            # paying to duplicate field-name manifests in initial Web JS.
            if name not in PROJECTION_CONTRACT_NAMES
            and name not in SETTINGS_CONTRACT_NAMES
            and name not in ARTIFACT_CONTRACT_NAMES
            and name
            not in {
                "InteractionRequest",
                "RespondInteractionRequest",
            }
        },
        "artifact": {
            "actions": _enum_values(artifact_schema, "ArtifactAction"),
            "families": public_families,
            "roles": _enum_values(artifact_schema, "ArtifactRole"),
            "statuses": _enum_values(artifact_schema, "ArtifactStatus"),
            "visibilities": public_visibilities,
            "feedbackSignals": _enum_values(artifact_schema, "FeedbackSignal"),
            "qualityStatuses": _enum_values(artifact_schema, "QualityStatus"),
            "renditionKinds": _enum_values(artifact_schema, "RenditionKind"),
        },
        "bootstrap": {
            "modelServiceStates": _property_enum(
                bootstrap_schema, "ModelServiceSnapshot", "state"
            ),
            "permissionProfiles": _property_enum(
                bootstrap_schema, "PermissionSnapshot", "profile"
            ),
            "permissionSandboxes": _property_enum(
                bootstrap_schema, "PermissionSnapshot", "sandbox"
            ),
            "permissionApprovals": _property_enum(
                bootstrap_schema, "PermissionSnapshot", "approval"
            ),
            "connectorTiers": _property_enum(
                bootstrap_schema, "ConnectorDescriptor", "tier"
            ),
            "connectorHealth": _property_enum(
                bootstrap_schema, "ConnectorDescriptor", "health"
            ),
            "updateStates": _property_enum(bootstrap_schema, "UpdateSnapshot", "state"),
            "extensionKinds": _property_enum(
                bootstrap_schema, "ExtensionProjection", "kind"
            ),
            "extensionSources": _property_enum(
                bootstrap_schema, "ExtensionProjection", "source"
            ),
            "extensionTrust": _property_enum(
                bootstrap_schema, "ExtensionProjection", "trust"
            ),
            "extensionStatuses": _property_enum(
                bootstrap_schema, "ExtensionProjection", "status"
            ),
            "extensionHealth": _property_enum(
                bootstrap_schema, "ExtensionProjection", "health"
            ),
            "extensionExportKinds": _property_enum(
                bootstrap_schema, "ExtensionExportProjection", "kind"
            ),
            "extensionExposures": _property_enum(
                bootstrap_schema, "ExtensionExportProjection", "exposure"
            ),
            "extensionActionIds": _property_enum(
                bootstrap_schema, "ExtensionActionProjection", "action_id"
            ),
        },
    }
    rendered_manifest = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    typescript = (
        "// Generated by tools/generate-runtime-contracts.py. DO NOT EDIT.\n"
        "// Source of truth: authoritative Python protocol/domain schemas.\n"
        f"export const GENERATED_RUNTIME_CONTRACT = {rendered_manifest} as const;\n"
        "export type GeneratedArtifactAction = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.actions[number];\n"
        "export type GeneratedArtifactFamily = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.families[number];\n"
        "export type GeneratedArtifactRole = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.roles[number];\n"
        "export type GeneratedArtifactStatus = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.statuses[number];\n"
        "export type GeneratedArtifactVisibility = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.visibilities[number];\n"
        "export type GeneratedQualityStatus = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.qualityStatuses[number];\n"
        "export type GeneratedRenditionKind = "
        "typeof GENERATED_RUNTIME_CONTRACT.artifact.renditionKinds[number];\n"
    ).encode("utf-8")
    settings_manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "schemaSha256": digest,
        "wireFields": {
            name: _wire_object_fields(
                schemas[name],
                name,
                include_definitions=name in NESTED_WIRE_CONTRACT_NAMES,
            )
            for name in sorted(SETTINGS_CONTRACT_NAMES)
        },
        "values": settings_values,
    }
    rendered_settings_manifest = json.dumps(
        settings_manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    settings_typescript = (
        "// Generated by tools/generate-runtime-contracts.py. DO NOT EDIT.\n"
        "// Progressively loaded Settings boundary; Python schemas remain authoritative.\n"
        "export const GENERATED_SETTINGS_RUNTIME_CONTRACT = "
        f"{rendered_settings_manifest} as const;\n"
        "export type GeneratedMemoryResetStatus = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.memoryResetStatuses[number];\n"
        "export type GeneratedMigrationCredentialKind = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.migrationCredentialKinds[number];\n"
        "export type GeneratedMigrationCredentialOrigin = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.migrationCredentialOrigins[number];\n"
        "export type GeneratedMigrationQuarantineStatus = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.migrationQuarantineStatuses[number];\n"
        "export type GeneratedOutputLocationAlias = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.outputLocationAliases[number];\n"
        "export type GeneratedOutputMaterializationStatus = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.outputMaterializationStatuses[number];\n"
        "export type GeneratedSystemHealthStatus = "
        "typeof GENERATED_SETTINGS_RUNTIME_CONTRACT.values.systemHealthStatuses[number];\n"
    ).encode("utf-8")
    projection_manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "schemaSha256": digest,
        "wireFields": {
            name: _wire_object_fields(
                schemas[name],
                name,
                include_definitions=name == "ThreadProjectionResponse",
            )
            for name in sorted(PROJECTION_CONTRACT_NAMES)
        },
        "runtime": {
            "threadStatuses": _enum_values(runtime_schema, "ThreadStatus"),
            "turnStatuses": _enum_values(runtime_schema, "TurnStatus"),
            "itemKinds": _enum_values(runtime_schema, "ItemKind"),
            "itemStatuses": _enum_values(runtime_schema, "ItemStatus"),
            "jobStatuses": _enum_values(runtime_schema, "JobStatus"),
            "interactionKinds": _enum_values(runtime_schema, "InteractionKind"),
            "interactionStatuses": _enum_values(runtime_schema, "InteractionStatus"),
            "interactionFieldControls": _enum_values(
                runtime_schema, "InteractionFieldControl"
            ),
            "interactionActionTypes": _enum_values(
                runtime_schema, "InteractionActionType"
            ),
            "interactionActionStyles": _enum_values(
                runtime_schema, "InteractionActionStyle"
            ),
            "connectorInteractionStates": _enum_values(
                runtime_schema, "ConnectorInteractionState"
            ),
        },
    }
    rendered_projection_manifest = json.dumps(
        projection_manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    projection_typescript = (
        "// Generated by tools/generate-runtime-contracts.py. DO NOT EDIT.\n"
        "// Deferred Thread/Turn projection boundary; Python schemas remain authoritative.\n"
        "export const GENERATED_RUNTIME_PROJECTION_CONTRACT = "
        f"{rendered_projection_manifest} as const;\n"
        "export type GeneratedRuntimeProjectionContractName = "
        "keyof typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.wireFields;\n"
        "export type GeneratedThreadStatus = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.threadStatuses[number];\n"
        "export type GeneratedTurnStatus = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.turnStatuses[number];\n"
        "export type GeneratedItemKind = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.itemKinds[number];\n"
        "export type GeneratedItemStatus = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.itemStatuses[number];\n"
        "export type GeneratedJobStatus = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.jobStatuses[number];\n"
        "export type GeneratedInteractionKind = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.interactionKinds[number];\n"
        "export type GeneratedInteractionStatus = "
        "typeof GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime.interactionStatuses[number];\n"
    ).encode("utf-8")
    artifact_schema = schemas["RetouchWorkspaceResponse"]
    artifact_manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "schemaSha256": digest,
        "wireFields": {
            name: _wire_object_fields(
                schemas[name],
                name,
                include_definitions=True,
            )
            for name in sorted(ARTIFACT_CONTRACT_NAMES)
        },
        "values": {
            "annotationKinds": _property_enum(
                artifact_schema, "RetouchAnnotationResponse", "kind"
            ),
            "coordinateSpaceVersion": _property_const(
                artifact_schema,
                "RetouchEditSurfaceResponse",
                "coordinate_space_version",
            ),
            "retouchJobStatuses": _enum_values(
                artifact_schema, "RetouchJobStatus"
            ),
            "retouchViewTools": _property_enum(
                artifact_schema, "RetouchViewStateResponse", "tool"
            ),
            "retouchWorkspaceStatuses": _enum_values(
                artifact_schema, "RetouchWorkspaceStatus"
            ),
        },
    }
    rendered_artifact_manifest = json.dumps(
        artifact_manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    artifact_typescript = (
        "// Generated by tools/generate-runtime-contracts.py. DO NOT EDIT.\n"
        "// Progressively loaded Artifact boundary; Python schemas remain authoritative.\n"
        "export const GENERATED_ARTIFACT_RUNTIME_CONTRACT = "
        f"{rendered_artifact_manifest} as const;\n"
        "export type GeneratedArtifactContractName = "
        "keyof typeof GENERATED_ARTIFACT_RUNTIME_CONTRACT.wireFields;\n"
        "export type GeneratedRetouchAnnotationKind = "
        "typeof GENERATED_ARTIFACT_RUNTIME_CONTRACT.values.annotationKinds[number];\n"
        "export type GeneratedRetouchJobStatus = "
        "typeof GENERATED_ARTIFACT_RUNTIME_CONTRACT.values.retouchJobStatuses[number];\n"
        "export type GeneratedRetouchViewTool = "
        "typeof GENERATED_ARTIFACT_RUNTIME_CONTRACT.values.retouchViewTools[number];\n"
        "export type GeneratedRetouchWorkspaceStatus = "
        "typeof GENERATED_ARTIFACT_RUNTIME_CONTRACT.values.retouchWorkspaceStatuses[number];\n"
    ).encode("utf-8")
    return (
        schema_bytes,
        typescript,
        projection_typescript,
        settings_typescript,
        artifact_typescript,
        digest,
    )


def _check_file(path: Path, expected: bytes) -> bool:
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        print(f"missing generated contract file: {path}", file=sys.stderr)
        return False
    if actual != expected:
        print(
            f"generated contract drift: {path}\n"
            "run `npm run contracts:generate` from desktop/ and review the schema change",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when committed generated outputs do not match Python schemas",
    )
    parser.add_argument(
        "--print-digest",
        action="store_true",
        help="print the canonical schema SHA-256",
    )
    args = parser.parse_args()

    (
        schema_bytes,
        typescript_bytes,
        projection_typescript_bytes,
        settings_typescript_bytes,
        artifact_typescript_bytes,
        digest,
    ) = build_outputs()
    if args.check:
        valid = _check_file(SCHEMA_OUTPUT, schema_bytes)
        valid = _check_file(TYPESCRIPT_OUTPUT, typescript_bytes) and valid
        valid = (
            _check_file(PROJECTION_TYPESCRIPT_OUTPUT, projection_typescript_bytes)
            and valid
        )
        valid = (
            _check_file(SETTINGS_TYPESCRIPT_OUTPUT, settings_typescript_bytes) and valid
        )
        valid = (
            _check_file(ARTIFACT_TYPESCRIPT_OUTPUT, artifact_typescript_bytes) and valid
        )
        if not valid:
            return 1
    else:
        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        SCHEMA_OUTPUT.write_bytes(schema_bytes)
        TYPESCRIPT_OUTPUT.write_bytes(typescript_bytes)
        PROJECTION_TYPESCRIPT_OUTPUT.write_bytes(projection_typescript_bytes)
        SETTINGS_TYPESCRIPT_OUTPUT.write_bytes(settings_typescript_bytes)
        ARTIFACT_TYPESCRIPT_OUTPUT.write_bytes(artifact_typescript_bytes)
        print(f"generated Runtime Web contract {digest}")
    if args.print_digest:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
