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
    ConversationUsageProjection,
    CreateTurnRequest,
    EventEnvelope,
    InteractionMutationResponse,
    InteractionRequest,
    InputAttachmentProjection,
    ProjectListResponse,
    QueueTurnRequest,
    ReplaceTurnRequest,
    SteerTurnRequest,
    RespondInteractionRequest,
    ThreadProjectionResponse,
    TurnMutationResponse,
    TurnProjection,
)
from ecorex.artifacts.api import RetouchBody, RetouchWorkspaceSubmitBody  # noqa: E402


OUTPUT_DIRECTORY = REPOSITORY_ROOT / "desktop" / "src" / "v1" / "api"
SCHEMA_OUTPUT = OUTPUT_DIRECTORY / "runtime-contract.schema.json"
TYPESCRIPT_OUTPUT = OUTPUT_DIRECTORY / "generatedRuntimeContract.ts"

SCHEMA_VERSION = 1


def _contract_schemas() -> dict[str, dict[str, Any]]:
    return {
        "ArtifactProjection": TypeAdapter(ArtifactProjection).json_schema(),
        "BootstrapResponse": BootstrapResponse.model_json_schema(),
        "ConversationUsageProjection": ConversationUsageProjection.model_json_schema(),
        "EventEnvelope": EventEnvelope.model_json_schema(),
        "InteractionRequest": InteractionRequest.model_json_schema(),
        "InteractionMutationResponse": InteractionMutationResponse.model_json_schema(),
        "InputAttachmentProjection": InputAttachmentProjection.model_json_schema(),
        "ProjectListResponse": ProjectListResponse.model_json_schema(),
        "RespondInteractionRequest": RespondInteractionRequest.model_json_schema(),
        "ThreadProjectionResponse": ThreadProjectionResponse.model_json_schema(),
        "TurnProjection": TurnProjection.model_json_schema(),
        "TurnMutationResponse": TurnMutationResponse.model_json_schema(),
        "CreateTurnRequest": CreateTurnRequest.model_json_schema(),
        "SteerTurnRequest": SteerTurnRequest.model_json_schema(),
        "QueueTurnRequest": QueueTurnRequest.model_json_schema(),
        "ReplaceTurnRequest": ReplaceTurnRequest.model_json_schema(),
        "RetouchBody": RetouchBody.model_json_schema(),
        "RetouchWorkspaceSubmitBody": RetouchWorkspaceSubmitBody.model_json_schema(),
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
        if isinstance(definition, dict) and isinstance(definition.get("properties"), dict):
            result[definition_name] = _all_wire_fields(
                definition, f"{root_name}.{definition_name}"
            )
    return result


def _definition(schema: dict[str, Any], name: str) -> dict[str, Any]:
    definition = schema.get("$defs", {}).get(name)
    if not isinstance(definition, dict):
        raise RuntimeError(f"backend schema no longer exposes definition {name!r}")
    return definition


def _property(schema: dict[str, Any], definition_name: str | None, field: str) -> dict[str, Any]:
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
    if not isinstance(candidate, list) or not candidate or not all(
        isinstance(value, str) for value in candidate
    ):
        raise RuntimeError(
            f"backend field {definition_name}.{field} is no longer a string enum"
        )
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


def build_outputs() -> tuple[bytes, bytes, str]:
    schemas = _contract_schemas()
    public_families = [family.value for family in PUBLIC_ARTIFACT_FAMILIES]
    public_visibilities = [visibility.value for visibility in PUBLIC_ARTIFACT_VISIBILITIES]
    schema_document = {
        "document_type": "ecorex.web-runtime-contracts",
        "schema_version": SCHEMA_VERSION,
        "sources": {
            "ArtifactProjection": "ecorex.artifacts.models.ArtifactProjection",
            "BootstrapResponse": "ecorex.protocol.BootstrapResponse",
            "EventEnvelope": "ecorex.protocol.EventEnvelope",
            "InteractionRequest": "ecorex.protocol.InteractionRequest",
            "InteractionMutationResponse": "ecorex.protocol.InteractionMutationResponse",
            "ProjectListResponse": "ecorex.protocol.ProjectListResponse",
            "RespondInteractionRequest": "ecorex.protocol.RespondInteractionRequest",
            "ThreadProjectionResponse": "ecorex.protocol.ThreadProjectionResponse",
            "TurnProjection": "ecorex.protocol.TurnProjection",
            "TurnMutationResponse": "ecorex.protocol.TurnMutationResponse",
            "CreateTurnRequest": "ecorex.protocol.CreateTurnRequest",
            "SteerTurnRequest": "ecorex.protocol.SteerTurnRequest",
            "QueueTurnRequest": "ecorex.protocol.QueueTurnRequest",
            "ReplaceTurnRequest": "ecorex.protocol.ReplaceTurnRequest",
            "RetouchBody": "ecorex.artifacts.api.RetouchBody",
            "RetouchWorkspaceSubmitBody": "ecorex.artifacts.api.RetouchWorkspaceSubmitBody",
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
                # Artifact Item payloads are decoded outside a response-model
                # boundary, so their complete nested shape is pinned here.
                # Bootstrap/Event nested objects are validated field-by-field
                # in the WebUI while their authoritative root stays exact.
                include_definitions=name
                in {"ArtifactProjection", "ThreadProjectionResponse"},
            )
            for name, schema in schemas.items()
            # Interaction payloads are already decoded from the authoritative
            # Event/response contract at their feature boundary. Keep their
            # complete JSON Schemas in the hash-pinned schema artifact without
            # paying to duplicate field-name manifests in initial Web JS.
            if name not in {
                "InteractionMutationResponse",
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
            "updateStates": _property_enum(
                bootstrap_schema, "UpdateSnapshot", "state"
            ),
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
    return schema_bytes, typescript, digest


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

    schema_bytes, typescript_bytes, digest = build_outputs()
    if args.check:
        valid = _check_file(SCHEMA_OUTPUT, schema_bytes)
        valid = _check_file(TYPESCRIPT_OUTPUT, typescript_bytes) and valid
        if not valid:
            return 1
    else:
        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        SCHEMA_OUTPUT.write_bytes(schema_bytes)
        TYPESCRIPT_OUTPUT.write_bytes(typescript_bytes)
        print(f"generated Runtime Web contract {digest}")
    if args.print_digest:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
