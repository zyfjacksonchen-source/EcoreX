"""Backend-owned v1 model and tool catalogs.

The catalog is deliberately independent from the WebUI. Capability packs may
attach handlers at composition time, but they cannot redefine the contract or
silently remove sibling tools when one intent is promoted.
"""

from __future__ import annotations

from .models import (
    CapabilityEffect,
    Exposure,
    IdempotencyClass,
    SandboxLevel,
    ToolSpec,
)
from .models_catalog import ManagedModelCatalog, ManagedModelSpec, ModelModality
from ecorex.managed_model_policy import (
    ECOREX_CHAT_MODEL_POLICIES,
    ECOREX_CHAT_MODEL_POLICY,
)
from .registry import CapabilityRegistry


_OBJECT = {"type": "object"}
_JSON_VALUE = {
    "type": ["object", "array", "string", "integer", "number", "boolean", "null"]
}
_READ_INPUT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "offset_bytes": {"type": "integer", "minimum": 0},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_INPUT_ATTACHMENT_READ_INPUT = {
    "type": "object",
    "properties": {
        "attachment_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "offset_chars": {"type": "integer", "minimum": 0},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 32768},
    },
    "required": ["attachment_id"],
    "additionalProperties": False,
}
_INPUT_ATTACHMENT_READ_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"type": "string", "enum": ["text", "image"]},
        "attachment_id": {"type": "string"},
        "revision_id": {"type": "string"},
        "mime_type": {"type": "string"},
        "size_bytes": {"type": "integer", "minimum": 0},
        "sha256": {"type": "string"},
        "content": {"type": ["string", "null"]},
        "next_offset_chars": {"type": "integer", "minimum": 0},
        "eof": {"type": "boolean"},
    },
    "required": [
        "schema_version",
        "kind",
        "attachment_id",
        "revision_id",
        "mime_type",
        "size_bytes",
        "sha256",
        "content",
        "next_offset_chars",
        "eof",
    ],
    "additionalProperties": False,
}
_FETCH_INPUT = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "minLength": 8, "maxLength": 4096},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
    },
    "required": ["url"],
    "additionalProperties": False,
}
_WEB_SEARCH_INPUT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "freshness": {
            "type": "string",
            "enum": ["any", "day", "week", "month", "year"],
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}
_VISION_INPUT = {
    "type": "object",
    "properties": {
        "artifact_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "minItems": 1,
            "maxItems": 4,
        },
        "attachment_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "minItems": 1,
            "maxItems": 4,
        },
        "instruction": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
    "required": ["instruction"],
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "minItems": 1,
                    "maxItems": 4,
                }
            },
            "required": ["artifact_ids"],
        },
        {
            "type": "object",
            "properties": {
                "attachment_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "minItems": 1,
                    "maxItems": 4,
                }
            },
            "required": ["attachment_ids"],
        },
    ],
    "additionalProperties": False,
}
_OCR_INPUT = {
    "type": "object",
    "properties": {
        "attachment_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "minItems": 1,
            "maxItems": 20,
        },
        "action": {
            "type": "string",
            "enum": ["extract_text", "extract_urls"],
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.5,
            "maximum": 8,
        },
    },
    "required": ["attachment_ids", "action"],
    "additionalProperties": False,
}
_CDP_INPUT = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "navigate",
                "snapshot",
                "click",
                "fill",
                "select",
                "scroll",
                "wait",
                "screenshot",
                "back",
                "forward",
                "get_text",
                "press",
                "evaluate",
            ],
            "description": "Browser action to perform.",
        },
        "url": {
            "type": "string",
            "maxLength": 4096,
            "description": "Page URL for navigate.",
        },
        "ref": {"type": "integer", "minimum": 1, "maximum": 1000},
        "selector": {"type": "string", "maxLength": 2048},
        "text": {"type": "string", "maxLength": 20000},
        "value": {"type": "string", "maxLength": 20000},
        "key": {"type": "string", "maxLength": 128},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "amount": {"type": "integer", "minimum": 1, "maximum": 10000},
        "script": {"type": "string", "maxLength": 20000},
        "full_page": {"type": "boolean"},
        "timeout": {"type": "integer", "minimum": 100, "maximum": 60000},
    },
    "required": ["action"],
    "additionalProperties": False,
}
_SHELL_INPUT = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "minLength": 1, "maxLength": 32000},
        "cwd": {"type": "string", "maxLength": 4096},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
        "run_in_background": {"type": "boolean"},
        "bash_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "kill": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_WRITE_INPUT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "content": {"type": "string", "maxLength": 8 * 1024 * 1024},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
_EDIT_INPUT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "oldText": {"type": "string", "maxLength": 8 * 1024 * 1024},
        "newText": {"type": "string", "maxLength": 8 * 1024 * 1024},
        "replaceAll": {"type": "boolean"},
    },
    "required": ["path", "oldText", "newText"],
    "additionalProperties": False,
}
_LS_INPUT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 4096},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
    },
    "additionalProperties": False,
}
_SEARCH_FILES_INPUT = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
        "target": {"type": "string", "enum": ["content", "files"]},
        "path": {"type": "string", "maxLength": 4096},
        "file_glob": {"type": "string", "maxLength": 512},
        "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
        "ignore_case": {"type": "boolean"},
        "no_ignore": {"type": "boolean"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "required": ["pattern"],
    "additionalProperties": False,
}
_SCHEDULER_INPUT = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "list", "get", "delete", "enable", "disable"],
        },
        "task_id": {"type": "string", "maxLength": 128},
        "name": {"type": "string", "maxLength": 240},
        "message": {"type": "string", "maxLength": 20000},
        "ai_task": {"type": "string", "maxLength": 20000},
        "schedule_type": {"type": "string", "enum": ["cron", "interval", "once"]},
        "schedule_value": {"type": "string", "maxLength": 512},
        "silent": {"type": "boolean"},
    },
    "required": ["action"],
    "additionalProperties": False,
}
_MEMORY_SEARCH_INPUT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
        "min_score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_MEMORY_GET_INPUT = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "start_line": {"type": "integer", "minimum": 1},
        "num_lines": {"type": "integer", "minimum": 1, "maximum": 5000},
    },
    "required": ["path"],
    "additionalProperties": False,
}

_SEND_INPUT = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Local file path to send to the user.",
            "minLength": 1,
            "maxLength": 4096,
        },
        "message": {"type": "string", "maxLength": 2000},
    },
    "required": ["path"],
    "additionalProperties": False,
}
def _image_task_properties():
    return {
        "prompt": {
            "type": "string",
            "description": "Image description or edit instruction.",
            "minLength": 1,
            "maxLength": 20000,
        },
        "image_url": {
            "type": ["string", "array"],
            "description": (
                "One image reference or an ordered list for editing/fusion. "
                "Accepts a local path, HTTP(S) URL, attachment_id, artifact_id, "
                "or a prior imagegen result URL."
            ),
            "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            "minLength": 1,
            "maxLength": 4096,
            "maxItems": 16,
        },
        "size": {"type": "string", "maxLength": 64},
        "quality": {
            "type": "string",
            "enum": ["low", "medium", "high", "auto"],
        },
        "aspect_ratio": {"type": "string", "minLength": 3, "maxLength": 16},
    }


_IMAGE_INPUT = {
    "type": "object",
    "description": (
        "CowAgent-compatible image generation/edit contract. Provide one prompt "
        "or one tasks array, never both. The Runtime owns the fixed image model."
    ),
    "properties": {
        **_image_task_properties(),
        "tasks": {
            "type": "array",
            "description": "Two to eight ordered image generation or edit tasks.",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": _image_task_properties(),
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    },
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "maxLength": 20000}
            },
            "required": ["prompt"],
        },
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": _image_task_properties(),
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
        },
    ],
    "additionalProperties": False,
}
_IMAGE_OUTPUT = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "minLength": 1, "maxLength": 256},
        "images": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "revision_id": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["url"],
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}
_TASK_LIST_INPUT = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                    },
                    "title": {"type": "string", "minLength": 1, "maxLength": 240},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["id", "title", "status"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}
_TASK_LIST_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "items": _TASK_LIST_INPUT["properties"]["items"],
    },
    "required": ["schema_version", "items"],
    "additionalProperties": False,
}
_SKILL_SEARCH_INPUT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "maxLength": 4096},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_SKILL_SEARCH_RESULT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "description": {"type": "string", "minLength": 1, "maxLength": 4096},
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
            "maxItems": 32,
        },
    },
    "required": ["discovery_id", "name", "description", "tags"],
    "additionalProperties": False,
}
_SKILL_SEARCH_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "extension_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "extension_contribution_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "query": {"type": "string", "maxLength": 4096},
        "skills": {
            "type": "array",
            "items": _SKILL_SEARCH_RESULT,
            "maxItems": 50,
        },
    },
    "required": [
        "schema_version",
        "extension_snapshot_id",
        "extension_contribution_snapshot_id",
        "query",
        "skills",
    ],
    "additionalProperties": False,
}
_SKILL_READ_INPUT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "reference_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 96},
            "maxItems": 32,
        },
    },
    "required": ["discovery_id"],
    "additionalProperties": False,
}
_SKILL_AVAILABLE_REFERENCE = {
    "type": "object",
    "properties": {
        "reference_id": {"type": "string", "minLength": 1, "maxLength": 96},
        "size_bytes": {"type": "integer", "minimum": 0, "maximum": 65536},
    },
    "required": ["reference_id", "size_bytes"],
    "additionalProperties": False,
}
_SKILL_READ_REFERENCE = {
    "type": "object",
    "properties": {
        "reference_id": {"type": "string", "minLength": 1, "maxLength": 96},
        "content": {"type": "string", "maxLength": 65536},
    },
    "required": ["reference_id", "content"],
    "additionalProperties": False,
}
_SKILL_READ_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "extension_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "extension_contribution_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "instructions": {"type": "string", "maxLength": 131072},
        "available_references": {
            "type": "array",
            "items": _SKILL_AVAILABLE_REFERENCE,
            "maxItems": 32,
        },
        "references": {
            "type": "array",
            "items": _SKILL_READ_REFERENCE,
            "maxItems": 32,
        },
    },
    "required": [
        "schema_version",
        "extension_snapshot_id",
        "extension_contribution_snapshot_id",
        "discovery_id",
        "name",
        "instructions",
        "available_references",
        "references",
    ],
    "additionalProperties": False,
}
_SKILL_RUN_INPUT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "parameters": {"type": "object"},
    },
    "required": ["discovery_id"],
    "additionalProperties": False,
}
_SKILL_RUN_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "result": {"type": "object"},
    },
    "required": ["schema_version", "discovery_id", "result"],
    "additionalProperties": False,
}
_CONNECTOR_SEARCH_INPUT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_CONNECTOR_SEARCH_RESULT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "connector_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "connector_name": {"type": "string", "minLength": 1, "maxLength": 256},
        "instance_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "account_name": {"type": "string", "minLength": 1, "maxLength": 512},
        "action_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "action_name": {"type": "string", "minLength": 1, "maxLength": 256},
        "description": {"type": "string", "minLength": 1, "maxLength": 4096},
        "effects": {
            "type": "array",
            "items": {"type": "string", "enum": ["read", "write", "subscribe"]},
            "minItems": 1,
            "maxItems": 3,
        },
        "call_tool_id": {
            "type": "string",
            "enum": ["connector_read", "connector_write"],
        },
        "score": {"type": "integer"},
    },
    "required": [
        "discovery_id",
        "connector_id",
        "connector_name",
        "instance_id",
        "account_name",
        "action_id",
        "action_name",
        "description",
        "effects",
        "call_tool_id",
        "score",
    ],
    "additionalProperties": False,
}
_CONNECTOR_WAITING = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "const": "connector_login"},
        "connector_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "connector_name": {"type": "string", "minLength": 1, "maxLength": 256},
        "reason": {
            "type": "string",
            "enum": [
                "login_required",
                "reauthorization_required",
                "adapter_not_installed",
                "connector_unavailable",
            ],
        },
        "required_action_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 2, "maxLength": 128},
            "maxItems": 32,
        },
    },
    "required": [
        "kind",
        "connector_id",
        "connector_name",
        "reason",
        "required_action_ids",
    ],
    "additionalProperties": False,
}
_CONNECTOR_SEARCH_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "connector_catalog_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "connector_catalog_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "actions": {
            "type": "array",
            "items": _CONNECTOR_SEARCH_RESULT,
            "maxItems": 50,
        },
        "waiting": {
            "type": "array",
            "items": _CONNECTOR_WAITING,
            "maxItems": 50,
        },
        "_ecorex_interaction": {"type": "object"},
    },
    "required": [
        "schema_version",
        "connector_catalog_snapshot_id",
        "connector_catalog_sha256",
        "query",
        "actions",
        "waiting",
    ],
    "additionalProperties": False,
}
_CONNECTOR_DESCRIBE_INPUT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
    },
    "required": ["discovery_id"],
    "additionalProperties": False,
}
_CONNECTOR_ACTION_DESCRIPTION = {
    "type": "object",
    "properties": {
        "connector_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "connector_name": {"type": "string", "minLength": 1, "maxLength": 256},
        "instance_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "account_name": {"type": "string", "minLength": 1, "maxLength": 512},
        "action_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "action_name": {"type": "string", "minLength": 1, "maxLength": 256},
        "description": {"type": "string", "minLength": 1, "maxLength": 4096},
        "contract_version": {"type": "string", "minLength": 1, "maxLength": 64},
        "action_contract_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "effects": {
            "type": "array",
            "items": {"type": "string", "enum": ["read", "write", "subscribe"]},
            "minItems": 1,
            "maxItems": 3,
        },
        "requires_idempotency_key": {"type": "boolean"},
        "call_tool_id": {
            "type": "string",
            "enum": ["connector_read", "connector_write"],
        },
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "result_envelope_version": {"type": "integer", "const": 1},
    },
    "required": [
        "connector_id",
        "connector_name",
        "instance_id",
        "account_name",
        "action_id",
        "action_name",
        "description",
        "contract_version",
        "action_contract_sha256",
        "effects",
        "requires_idempotency_key",
        "call_tool_id",
        "input_schema",
        "output_schema",
        "result_envelope_version",
    ],
    "additionalProperties": False,
}
_CONNECTOR_DESCRIBE_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "connector_catalog_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "found": {"type": "boolean"},
        "available": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 128},
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "search_tool_call_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "search_result_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "action": _CONNECTOR_ACTION_DESCRIPTION,
    },
    "required": ["schema_version", "connector_catalog_snapshot_id", "found"],
    "additionalProperties": False,
}
_CONNECTOR_CALL_INPUT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "input": {"type": "object"},
    },
    "required": ["discovery_id", "input"],
    "additionalProperties": False,
}
_CONNECTOR_RESULT_ARTIFACT = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "revision_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "family": {"type": "string", "const": "data_export"},
        "role": {"type": "string", "const": "deliverable"},
        "visibility": {"type": "string", "const": "secondary"},
        "status": {"type": "string", "const": "ready"},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 512},
        "mime_type": {"type": "string", "const": "application/json"},
        "size_bytes": {"type": "integer", "minimum": 0, "maximum": 8388608},
        "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "content_url": {"type": "string", "minLength": 1, "maxLength": 1024},
        "preview_url": {"type": "string", "minLength": 1, "maxLength": 1024},
        "reader": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string", "const": "artifact_read"},
                "offset_chars": {"type": "integer", "const": 0},
                "max_chars": {"type": "integer", "const": 32768},
            },
            "required": ["tool_id", "offset_chars", "max_chars"],
            "additionalProperties": False,
        },
    },
    "required": [
        "artifact_id",
        "revision_id",
        "family",
        "role",
        "visibility",
        "status",
        "display_name",
        "mime_type",
        "size_bytes",
        "sha256",
        "content_url",
        "preview_url",
        "reader",
    ],
    "additionalProperties": False,
}
_CONNECTOR_CALL_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "status": {"type": "string", "const": "completed"},
        "delivery": {
            "type": "string",
            "enum": ["inline", "artifact", "result_unavailable"],
        },
        "connector_invocation_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        },
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "result_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "size_bytes": {"type": "integer", "minimum": 0, "maximum": 8388608},
        "data": _JSON_VALUE,
        "artifact": _CONNECTOR_RESULT_ARTIFACT,
        "identity_kind": {"type": "string", "const": "receipt"},
        "error_code": {
            "type": "string",
            "enum": [
                "connector_result_schema_invalid",
                "connector_result_secret_rejected",
                "connector_result_too_large",
                "connector_result_persistence_failed",
            ],
        },
    },
    "required": [
        "schema_version",
        "status",
        "delivery",
        "connector_invocation_id",
        "discovery_id",
        "result_sha256",
        "size_bytes",
    ],
    "additionalProperties": False,
}
_ARTIFACT_READ_INPUT = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "revision_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "offset_chars": {"type": "integer", "minimum": 0},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 32768},
    },
    "required": ["artifact_id", "revision_id"],
    "additionalProperties": False,
}
_ARTIFACT_READ_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "artifact_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "revision_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "size_bytes": {"type": "integer", "minimum": 0, "maximum": 8388608},
        "offset_chars": {"type": "integer", "minimum": 0},
        "next_offset_chars": {"type": "integer", "minimum": 0},
        "eof": {"type": "boolean"},
        "content": {"type": "string", "maxLength": 32768},
    },
    "required": [
        "schema_version",
        "artifact_id",
        "revision_id",
        "sha256",
        "size_bytes",
        "offset_chars",
        "next_offset_chars",
        "eof",
        "content",
    ],
    "additionalProperties": False,
}
_TOOL_SEARCH_INPUT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_TOOL_PROVIDER_PROVENANCE = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["core", "mcp"]},
        "provider_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "revision_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "trust": {
            "type": "string",
            "enum": ["builtin", "administrator", "verified_publisher"],
        },
        "key_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "evidence_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "product_reviewed": {"type": "boolean"},
    },
    "required": [
        "kind",
        "provider_id",
        "revision_id",
        "trust",
        "evidence_sha256",
        "product_reviewed",
    ],
    "additionalProperties": False,
}
_TOOL_SEARCH_RESULT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "tool_id": {"type": "string", "minLength": 2, "maxLength": 128},
        "tool_version": {"type": "string", "minLength": 5, "maxLength": 128},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 512},
        "description": {"type": "string", "minLength": 1, "maxLength": 4096},
        "exposure": {"type": "string", "enum": ["deferred"]},
        "score": {"type": "integer"},
        "requires_approval": {"type": "boolean"},
        "match_class": {
            "type": "string",
            "enum": [
                "exact_reference",
                "model_alias",
                "reviewed_term_exact",
                "reviewed_term",
                "provider_tag",
                "display_name",
                "description",
            ],
        },
        "matched_facets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "maxItems": 16,
        },
        "matched_evidence": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
            "maxItems": 32,
        },
        "provider": _TOOL_PROVIDER_PROVENANCE,
    },
    "required": [
        "discovery_id",
        "tool_id",
        "tool_version",
        "display_name",
        "description",
        "exposure",
        "score",
        "requires_approval",
        "match_class",
        "matched_facets",
        "matched_evidence",
        "provider",
    ],
    "additionalProperties": False,
}
_TOOL_SEARCH_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "capability_snapshot_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "capability_catalog_digest": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "routing_policy_digest": {"type": "string", "minLength": 64, "maxLength": 64},
        "discovery_policy_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "discovery_policy_version": {
            "type": "string",
            "minLength": 5,
            "maxLength": 128,
        },
        "discovery_policy_digest": {"type": "string", "minLength": 64, "maxLength": 64},
        "model_catalog_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "tools": {
            "type": "array",
            "items": _TOOL_SEARCH_RESULT,
            "maxItems": 50,
        },
    },
    "required": [
        "schema_version",
        "capability_snapshot_id",
        "capability_catalog_digest",
        "routing_policy_digest",
        "discovery_policy_id",
        "discovery_policy_version",
        "discovery_policy_digest",
        "model_catalog_snapshot_id",
        "query",
        "tools",
    ],
    "additionalProperties": False,
}
_TOOL_DESCRIBE_INPUT = {
    "type": "object",
    "properties": {
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
    },
    "required": ["discovery_id"],
    "additionalProperties": False,
}
_TOOL_DESCRIBE_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "capability_snapshot_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "found": {"type": "boolean"},
        "available": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 128},
        "discovery_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "search_tool_call_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        "search_result_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "tool": {"type": "object"},
    },
    "required": ["schema_version", "capability_snapshot_id", "found"],
    "additionalProperties": False,
}


def builtin_model_catalog() -> ManagedModelCatalog:
    chat_policy = ECOREX_CHAT_MODEL_POLICY
    return ManagedModelCatalog(
        (
            *(
                ManagedModelSpec(
                    model_id=policy.local_model_id,
                    display_name=policy.display_name,
                    modalities=frozenset(
                        {ModelModality.CHAT, ModelModality.VISION}
                        if policy.local_model_id != "ecorex-deepseek-v4-pro"
                        else {ModelModality.CHAT}
                    ),
                    aliases=policy.aliases,
                    capabilities=frozenset(
                        {"chat", "tools", "reasoning", "vision"}
                        if policy.local_model_id != "ecorex-deepseek-v4-pro"
                        else {"chat", "tools", "reasoning"}
                    ),
                    default_for=(
                        frozenset({ModelModality.CHAT, ModelModality.VISION})
                        if policy is chat_policy
                        else frozenset()
                    ),
                    model_policy=policy,
                )
                for policy in ECOREX_CHAT_MODEL_POLICIES
            ),
            ManagedModelSpec(
                model_id="gpt-image-2",
                display_name="e-Mate Image 2",
                modalities=frozenset({ModelModality.IMAGE}),
                # ``normalize_reference`` canonicalizes underscores to
                # hyphens, so image_2 resolves through the single image-2
                # catalog alias instead of becoming duplicate metadata.
                aliases=("image2", "image-2"),
                capabilities=frozenset({"image_generation", "image_edit"}),
                default_for=frozenset({ModelModality.IMAGE}),
            ),
        )
    )


def builtin_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            tool_id="tool_search",
            version="1.0.0",
            display_name="查找可用能力",
            description=(
                "在当前 Turn 的权限、可用性和版本快照中查找尚未披露的工具；"
                "需要额外能力时先调用此工具"
            ),
            input_schema=_TOOL_SEARCH_INPUT,
            output_schema=_TOOL_SEARCH_OUTPUT,
            aliases=("find-tool", "search-tools"),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"tool", "capability", "search", "discover"}),
        ),
        ToolSpec(
            tool_id="tool_describe",
            version="1.0.0",
            display_name="查看能力说明",
            description=(
                "仅使用 tool_search 返回的准确 discovery_id 查看当前执行批次中"
                "某项工具的合同和可用原因；成功查看 deferred 工具后仅对该批次披露"
            ),
            input_schema=_TOOL_DESCRIBE_INPUT,
            output_schema=_TOOL_DESCRIBE_OUTPUT,
            aliases=("describe-tool",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"tool", "capability", "describe"}),
        ),
        ToolSpec(
            tool_id="skill_search",
            version="1.0.0",
            display_name="查找技能",
            description="按名称、说明和标签查找当前会话冻结的声明式办公技能",
            input_schema=_SKILL_SEARCH_INPUT,
            output_schema=_SKILL_SEARCH_OUTPUT,
            aliases=("find-skill",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"skill", "workflow", "instructions"}),
        ),
        ToolSpec(
            tool_id="task_list",
            version="1.0.0",
            display_name="更新任务清单",
            description="仅为包含多个实际步骤的任务创建或更新 2–8 项清单；最多一项进行中，状态必须反映真实进度",
            input_schema=_TASK_LIST_INPUT,
            output_schema=_TASK_LIST_OUTPUT,
            aliases=("todo-list", "update-plan"),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset(
                {"task", "plan", "workflow", "multi-step", "任务", "计划"}
            ),
        ),
        ToolSpec(
            tool_id="skill_read",
            version="1.0.0",
            display_name="读取技能",
            description=(
                "仅使用 skill_search 在当前执行批次返回的精确 "
                "discovery_id 读取该 Skill 修订的指令和明确选择的静态参考资料"
            ),
            input_schema=_SKILL_READ_INPUT,
            output_schema=_SKILL_READ_OUTPUT,
            aliases=("use-skill",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"skill", "workflow", "instructions"}),
        ),
        ToolSpec(
            tool_id="skill_run",
            version="1.0.0",
            display_name="运行技能",
            description=(
                "仅运行已通过 skill_search 和 skill_read 绑定的精确 Skill 修订；"
                "说明型或无受控入口的 Skill 不可执行"
            ),
            input_schema=_SKILL_RUN_INPUT,
            output_schema=_SKILL_RUN_OUTPUT,
            aliases=("run-skill",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"skill", "workflow", "execute"}),
        ),
        ToolSpec(
            tool_id="connector_search",
            version="1.0.0",
            display_name="查找连接器操作",
            description=(
                "在当前执行批次冻结的连接器账号与操作目录中查找能力；"
                "显式连接器名称只影响排序，不授予调用权限"
            ),
            input_schema=_CONNECTOR_SEARCH_INPUT,
            output_schema=_CONNECTOR_SEARCH_OUTPUT,
            aliases=("find-connector-action",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"connector", "document", "message", "integration"}),
        ),
        ToolSpec(
            tool_id="connector_describe",
            version="1.0.0",
            display_name="查看连接器操作合同",
            description=(
                "仅使用 connector_search 在当前执行批次返回的精确 discovery_id "
                "查看动态输入输出合同并披露对应的只读或写入端点"
            ),
            input_schema=_CONNECTOR_DESCRIBE_INPUT,
            output_schema=_CONNECTOR_DESCRIBE_OUTPUT,
            aliases=("describe-connector-action",),
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"connector", "describe", "contract"}),
        ),
        ToolSpec(
            tool_id="connector_read",
            version="1.0.0",
            display_name="执行连接器只读操作",
            description=(
                "执行当前批次已经精确搜索并查看合同的连接器只读操作；"
                "Runtime 会再次校验账号、scope、健康和动态输入合同"
            ),
            input_schema=_CONNECTOR_CALL_INPUT,
            output_schema=_CONNECTOR_CALL_OUTPUT,
            aliases=("call-connector-read",),
            effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"connector", "read", "document", "search"}),
        ),
        ToolSpec(
            tool_id="connector_write",
            version="1.0.0",
            display_name="执行连接器写入操作",
            description=(
                "执行当前批次已经精确搜索并查看合同的连接器写入操作；"
                "外部写入使用稳定幂等键，未知结果必须人工核对"
            ),
            input_schema=_CONNECTOR_CALL_INPUT,
            output_schema=_CONNECTOR_CALL_OUTPUT,
            aliases=("call-connector-write",),
            effects=frozenset({CapabilityEffect.WRITE, CapabilityEffect.NETWORK}),
            idempotency=IdempotencyClass.IDEMPOTENT,
            required_sandbox=SandboxLevel.WORKSPACE_WRITE,
            default_exposure=Exposure.HIDDEN,
            concurrency_safe=False,
            intent_tags=frozenset({"connector", "write", "send", "edit"}),
        ),
        ToolSpec(
            tool_id="artifact_read",
            version="1.0.0",
            display_name="分段读取数据工件",
            description=(
                "按字符边界分段读取当前会话中连接器生成的 JSON 数据工件；"
                "只接受结果信封返回的准确 Artifact 与 Revision 身份"
            ),
            input_schema=_ARTIFACT_READ_INPUT,
            output_schema=_ARTIFACT_READ_OUTPUT,
            aliases=("read-artifact-data",),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"artifact", "data", "json", "read"}),
        ),
        ToolSpec(
            tool_id="input_attachment_read",
            version="1.0.0",
            display_name="读取本条消息的附件",
            description="分段读取当前 Turn 中用户主动添加的文本附件；图片只返回受保护的引用信息，需通过视觉能力继续检查",
            input_schema=_INPUT_ATTACHMENT_READ_INPUT,
            output_schema=_INPUT_ATTACHMENT_READ_OUTPUT,
            aliases=("read-input-attachment",),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.HIDDEN,
            intent_tags=frozenset({"attachment", "file", "document", "read", "upload"}),
        ),
        ToolSpec(
            tool_id="read",
            version="1.0.0",
            display_name="读取工作区",
            description="读取用户授权工作区中的文件和目录",
            input_schema=_READ_INPUT,
            output_schema=_OBJECT,
            aliases=("workspace-read", "read-file"),
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"read", "file", "document"}),
        ),
        ToolSpec(
            tool_id="write",
            version="1.0.0",
            display_name="写入文件",
            description="创建文件或用完整内容覆盖现有文件；相对路径以当前工作区为准",
            input_schema=_WRITE_INPUT,
            output_schema=_OBJECT,
            aliases=("write-file", "写文件"),
            effects=frozenset({CapabilityEffect.WRITE}),
            idempotency=IdempotencyClass.IDEMPOTENT,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"write", "file", "create", "写文件"}),
        ),
        ToolSpec(
            tool_id="edit",
            version="1.0.0",
            display_name="编辑文件",
            description="用精确文本替换编辑文件；oldText 为空时追加到文件末尾",
            input_schema=_EDIT_INPUT,
            output_schema=_OBJECT,
            aliases=("edit-file", "修改文件"),
            effects=frozenset({CapabilityEffect.WRITE}),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"edit", "file", "replace", "修改文件"}),
        ),
        ToolSpec(
            tool_id="ls",
            version="1.0.0",
            display_name="列出目录",
            description="列出目录内容；相对路径以当前工作区为准",
            input_schema=_LS_INPUT,
            output_schema=_OBJECT,
            aliases=("list-files", "list-directory"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"list", "directory", "files", "目录"}),
        ),
        ToolSpec(
            tool_id="search_files",
            version="1.0.0",
            display_name="搜索文件",
            description="按名称查找文件或用正则搜索文件内容",
            input_schema=_SEARCH_FILES_INPUT,
            output_schema=_OBJECT,
            aliases=("grep", "find-files", "搜索文件"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"search", "files", "grep", "查找文件"}),
        ),
        ToolSpec(
            tool_id="web_fetch",
            version="1.0.0",
            display_name="获取网页",
            description="通过网络读取明确指定的网页资源",
            input_schema=_FETCH_INPUT,
            output_schema=_OBJECT,
            aliases=("fetch", "web-fetch"),
            effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
            intent_tags=frozenset(
                {"web", "research", "fetch", "read", "page", "读取网页", "网页搜索"}
            ),
            recovery_hints=(
                "Broaden the URL or search scope only when the user's goal permits it.",
                "Relax optional filters before switching to another read-only web tool.",
            ),
            cache_ttl_seconds=300,
            required_packs=frozenset({"browser"}),
            default_exposure=Exposure.DIRECT,
        ),
        ToolSpec(
            tool_id="web_search",
            version="1.0.0",
            display_name="联网搜索",
            description="搜索公开网页并返回标题、网址和摘要；不需要用户预先提供 URL",
            input_schema=_WEB_SEARCH_INPUT,
            output_schema=_OBJECT,
            aliases=("search-web", "internet-search", "联网搜索"),
            effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"web", "search", "research", "联网", "搜索"}),
            cache_ttl_seconds=300,
            required_packs=frozenset({"browser"}),
        ),
        ToolSpec(
            tool_id="vision",
            version="1.0.0",
            display_name="视觉检查",
            description="检查图片、截图和办公文档渲染结果",
            input_schema=_VISION_INPUT,
            output_schema=_OBJECT,
            aliases=("inspect-image", "image-understanding"),
            intent_tags=frozenset(
                {"vision", "image", "screenshot", "inspect", "图像识别", "视觉检查"}
            ),
            required_packs=frozenset({"image"}),
            default_exposure=Exposure.DIRECT,
        ),
        ToolSpec(
            tool_id="ocr",
            version="1.0.0",
            display_name="图片文字识别",
            description=(
                "从当前 Turn 中用户上传并绑定的图片附件提取文字或网址；"
                "只接受不可伪造的 attachment_id，不接受本机路径"
            ),
            input_schema=_OCR_INPUT,
            output_schema=_OBJECT,
            aliases=("extract-image-text", "read-image-text"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset(
                {"ocr", "image", "text", "read", "文字识别", "识别图片文字"}
            ),
            cache_ttl_seconds=86_400,
            required_packs=frozenset({"ocr"}),
        ),
        ToolSpec(
            tool_id="browser",
            version="1.0.0",
            display_name="浏览器控制",
            description=(
                "在同一会话中持续控制浏览器；navigate 会自动返回页面快照，"
                "后续可按 ref 点击、输入、选择、滚动、截图或读取文本"
            ),
            input_schema=_CDP_INPUT,
            output_schema=_OBJECT,
            aliases=("cdp", "browser-cdp", "浏览器"),
            effects=frozenset(
                {CapabilityEffect.NETWORK, CapabilityEffect.UI_AUTOMATION}
            ),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            intent_tags=frozenset({"browser", "web", "cdp", "浏览器", "网页搜索"}),
            required_packs=frozenset({"browser"}),
            default_exposure=Exposure.DIRECT,
        ),
        ToolSpec(
            tool_id="bash",
            version="1.0.0",
            display_name="命令执行",
            description="在本机当前工作区执行命令；支持前台等待和后台进程",
            input_schema=_SHELL_INPUT,
            output_schema=_OBJECT,
            aliases=(
                "shell",
                "powershell",
                "terminal",
            ),
            effects=frozenset({CapabilityEffect.WRITE, CapabilityEffect.EXECUTE}),
            # A shell command is an opaque program boundary.  Even apparently
            # harmless text can expand aliases, profiles or child processes,
            # so Core must never infer replay safety from the command string.
            # A future read-only command capability needs its own backend-owned
            # typed contract and parser; the general shell remains uncertain
            # after any lost execution acknowledgement.
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            concurrency_safe=False,
            required_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
            intent_tags=frozenset(
                {
                    "shell",
                    "command",
                    "automation",
                    "run",
                    "write",
                    "file",
                    "执行命令",
                    "写文件",
                    "存储空间",
                }
            ),
            default_exposure=Exposure.DIRECT,
        ),
        ToolSpec(
            tool_id="scheduler",
            version="1.0.0",
            display_name="定时任务",
            description=(
                "创建、查询和管理提醒或周期任务；仅在用户要求定时、提醒、"
                "每天、每周或一段时间后执行时使用"
            ),
            input_schema=_SCHEDULER_INPUT,
            output_schema=_OBJECT,
            aliases=("scheduled-task", "reminder", "定时任务", "提醒"),
            effects=frozenset({CapabilityEffect.WRITE}),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"schedule", "reminder", "cron", "定时", "提醒"}),
        ),
        ToolSpec(
            tool_id="send",
            version="1.0.0",
            display_name="发送文件",
            description=(
                "把本机文件作为可下载产物发送给用户；网页 URL 直接写入回复，"
                "不要先下载再发送"
            ),
            input_schema=_SEND_INPUT,
            output_schema=_OBJECT,
            aliases=("send-file", "发送文件"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"send", "file", "deliver", "发送", "交付"}),
        ),
        ToolSpec(
            tool_id="memory_search",
            version="1.0.0",
            display_name="搜索记忆与知识",
            description=(
                "用关键词搜索 MEMORY.md、每日记忆和 knowledge 知识页面；"
                "回忆既往决定、偏好、待办或知识时使用"
            ),
            input_schema=_MEMORY_SEARCH_INPUT,
            output_schema=_OBJECT,
            aliases=("search-memory", "搜索记忆"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"memory", "knowledge", "recall", "记忆", "知识"}),
        ),
        ToolSpec(
            tool_id="memory_get",
            version="1.0.0",
            display_name="读取记忆与知识",
            description=(
                "按路径和行号读取 MEMORY.md、每日记忆或 knowledge 知识页面"
            ),
            input_schema=_MEMORY_GET_INPUT,
            output_schema=_OBJECT,
            aliases=("get-memory", "读取记忆"),
            effects=frozenset({CapabilityEffect.READ}),
            idempotency=IdempotencyClass.READ_ONLY,
            default_exposure=Exposure.DIRECT,
            intent_tags=frozenset({"memory", "knowledge", "read", "记忆", "知识"}),
        ),
        ToolSpec(
            tool_id="imagegen",
            version="2.0.0",
            display_name="图片生成与编辑",
            description=(
                "使用固定图片模型生成或编辑图片；兼容 CowAgent 的 "
                "prompt/image_url/quality/size/aspect_ratio 语义"
            ),
            input_schema=_IMAGE_INPUT,
            output_schema=_IMAGE_OUTPUT,
            aliases=("generate-image", "edit-image", "image-generation"),
            effects=frozenset(
                {CapabilityEffect.NETWORK, CapabilityEffect.GENERATE_MEDIA}
            ),
            idempotency=IdempotencyClass.IDEMPOTENT,
            intent_tags=frozenset({"image", "image generation", "image edit"}),
            routing_facets=frozenset(
                {
                    "media.image.create",
                    "media.image.edit",
                    "media.image.edit.retouch",
                    "media.image.edit.background_remove",
                }
            ),
            workflow_skill_ids=frozenset({"skill.image-generation"}),
            required_packs=frozenset({"image"}),
            required_model_modalities=frozenset({"image"}),
            required_model_capabilities={
                "image": frozenset({"image_generation", "image_edit"}),
            },
            default_exposure=Exposure.DIRECT,
        ),
    )


def builtin_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry(builtin_tool_specs())
