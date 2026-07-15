"""Small, fail-closed JSON Schema subset used at the tool trust boundary.

EcoreX owns every production ``ToolSpec``.  Pulling an unrestricted schema
engine into the hot path would add a large and surprisingly permissive attack
surface, so the runtime deliberately supports the subset needed by tool
contracts and rejects unsupported schema keywords when a spec is registered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any


_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
        "const",
        "pattern",
    }
)
_JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_MAX_SCHEMA_DEPTH = 24
_MAX_INSTANCE_DEPTH = 32
_MAX_INSTANCE_NODES = 50_000
_MAX_PATTERN_LENGTH = 512


class SchemaContractError(ValueError):
    """Raised when a backend-owned tool schema is unsafe or unsupported."""


class SchemaInstanceError(ValueError):
    """Raised when a tool argument/result does not match its contract."""


def validate_schema_contract(
    schema: Mapping[str, Any],
    *,
    label: str,
    allow_pattern: bool = True,
) -> None:
    """Validate the supported schema subset before it can enter a catalog.

    ``pattern`` remains available to product-owned Core contracts, whose
    regular expressions are reviewed with the Runtime.  Untrusted protocol
    adapters can set ``allow_pattern=False`` so an attacker-controlled schema
    cannot introduce a regular-expression denial of service into instance
    validation.
    """

    if not isinstance(allow_pattern, bool):
        raise TypeError("allow_pattern must be a boolean")

    seen: set[int] = set()

    def visit(current: Mapping[str, Any], path: str, depth: int) -> None:
        if depth > _MAX_SCHEMA_DEPTH:
            raise SchemaContractError(f"{label} is nested too deeply at {path}")
        identity = id(current)
        if identity in seen:
            raise SchemaContractError(f"{label} contains a recursive object at {path}")
        seen.add(identity)
        unknown = sorted(set(current) - _ALLOWED_KEYWORDS)
        if unknown:
            raise SchemaContractError(
                f"{label} contains unsupported keyword {unknown[0]!r} at {path}"
            )
        if not allow_pattern and "pattern" in current:
            raise SchemaContractError(
                f"{label} contains forbidden keyword 'pattern' at {path}"
            )
        declared = current.get("type")
        if isinstance(declared, str):
            declared_types = (declared,)
        elif (
            isinstance(declared, Sequence)
            and not isinstance(declared, (str, bytes, bytearray))
            and 1 <= len(declared) <= len(_JSON_TYPES)
            and all(isinstance(value, str) for value in declared)
        ):
            declared_types = tuple(declared)
        else:
            declared_types = ()
        if (
            not declared_types
            or len(set(declared_types)) != len(declared_types)
            or any(value not in _JSON_TYPES for value in declared_types)
        ):
            raise SchemaContractError(f"{label}.type is invalid at {path}")
        description = current.get("description")
        if description is not None and (
            not isinstance(description, str) or len(description) > 2_000
        ):
            raise SchemaContractError(f"{label}.description is invalid at {path}")
        enum = current.get("enum")
        if enum is not None:
            if (
                isinstance(enum, (str, bytes, bytearray))
                or not isinstance(enum, Sequence)
                or not 1 <= len(enum) <= 256
            ):
                raise SchemaContractError(f"{label}.enum is invalid at {path}")
            _validate_json_graph(list(enum), label=f"{label}.enum")
        if "const" in current:
            _validate_json_graph(current["const"], label=f"{label}.const")

        if "object" in declared_types:
            properties = current.get("properties", {})
            if not isinstance(properties, Mapping) or len(properties) > 256:
                raise SchemaContractError(f"{label}.properties is invalid at {path}")
            for name, child in properties.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or not isinstance(child, Mapping)
                ):
                    raise SchemaContractError(
                        f"{label}.properties contains an invalid entry at {path}"
                    )
                visit(child, f"{path}.properties.{name}", depth + 1)
            required = current.get("required", [])
            if (
                isinstance(required, (str, bytes, bytearray))
                or not isinstance(required, Sequence)
                or any(not isinstance(name, str) or name not in properties for name in required)
                or len(set(required)) != len(required)
            ):
                raise SchemaContractError(f"{label}.required is invalid at {path}")
            additional = current.get("additionalProperties", True)
            if not isinstance(additional, (bool, Mapping)):
                raise SchemaContractError(
                    f"{label}.additionalProperties is invalid at {path}"
                )
            if isinstance(additional, Mapping):
                visit(additional, f"{path}.additionalProperties", depth + 1)
        if "array" in declared_types:
            items = current.get("items")
            if items is None and len(declared_types) > 1:
                pass
            elif not isinstance(items, Mapping):
                raise SchemaContractError(f"{label}.items is required at {path}")
            else:
                visit(items, f"{path}.items", depth + 1)

        for minimum_key, maximum_key in (
            ("minItems", "maxItems"),
            ("minLength", "maxLength"),
        ):
            minimum = current.get(minimum_key)
            maximum = current.get(maximum_key)
            if minimum is not None and (
                isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0
            ):
                raise SchemaContractError(f"{label}.{minimum_key} is invalid at {path}")
            if maximum is not None and (
                isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0
            ):
                raise SchemaContractError(f"{label}.{maximum_key} is invalid at {path}")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise SchemaContractError(
                    f"{label}.{minimum_key} exceeds {maximum_key} at {path}"
                )
        for bound in ("minimum", "maximum"):
            value = current.get(bound)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise SchemaContractError(f"{label}.{bound} is invalid at {path}")
        if (
            current.get("minimum") is not None
            and current.get("maximum") is not None
            and current["minimum"] > current["maximum"]
        ):
            raise SchemaContractError(f"{label}.minimum exceeds maximum at {path}")
        pattern = current.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or len(pattern) > _MAX_PATTERN_LENGTH:
                raise SchemaContractError(f"{label}.pattern is invalid at {path}")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SchemaContractError(f"{label}.pattern is invalid at {path}") from exc
        seen.remove(identity)

    if not isinstance(schema, Mapping):
        raise SchemaContractError(f"{label} must be an object")
    visit(schema, "$", 0)


def validate_schema_instance(
    value: Any,
    schema: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Validate one canonical JSON value and report only a bounded safe path."""

    _validate_json_graph(value, label=label)
    remaining = _MAX_INSTANCE_NODES

    def visit(current: Any, contract: Mapping[str, Any], path: str, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise SchemaInstanceError(f"{label} contains too many values")
        if depth > _MAX_INSTANCE_DEPTH:
            raise SchemaInstanceError(f"{label} is nested too deeply")
        declared = contract["type"]
        declared_types = (
            {declared}
            if isinstance(declared, str)
            else set(declared)
        )
        actual_type = (
            "null"
            if current is None
            else "boolean"
            if isinstance(current, bool)
            else "integer"
            if isinstance(current, int)
            else "number"
            if isinstance(current, float)
            else "string"
            if isinstance(current, str)
            else "array"
            if isinstance(current, list)
            else "object"
            if isinstance(current, dict)
            else "invalid"
        )
        valid = actual_type in declared_types or (
            actual_type == "integer" and "number" in declared_types
        )
        if not valid:
            raise SchemaInstanceError(f"{label} has invalid type at {path}")
        # Keep this mapping close to validation as executable documentation of
        # the exact canonical Python representation of each JSON type.
        representable = {
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
            "null": lambda item: item is None,
        }[actual_type](current)
        if not representable:
            raise SchemaInstanceError(f"{label} has invalid type at {path}")
        if "enum" in contract and current not in contract["enum"]:
            raise SchemaInstanceError(f"{label} has an unsupported value at {path}")
        if "const" in contract and current != contract["const"]:
            raise SchemaInstanceError(f"{label} has an invalid constant at {path}")

        if actual_type == "object":
            properties = contract.get("properties", {})
            for required in contract.get("required", []):
                if required not in current:
                    raise SchemaInstanceError(
                        f"{label} is missing required field at {path}.{required}"
                    )
            additional = contract.get("additionalProperties", True)
            for key, child in current.items():
                child_contract = properties.get(key)
                if child_contract is None:
                    if additional is False:
                        raise SchemaInstanceError(
                            f"{label} contains an unknown field at {path}.{key}"
                        )
                    if isinstance(additional, Mapping):
                        child_contract = additional
                if child_contract is not None:
                    visit(child, child_contract, f"{path}.{key}", depth + 1)
        elif actual_type == "array":
            if len(current) < contract.get("minItems", 0):
                raise SchemaInstanceError(f"{label} has too few items at {path}")
            maximum = contract.get("maxItems")
            if maximum is not None and len(current) > maximum:
                raise SchemaInstanceError(f"{label} has too many items at {path}")
            item_contract = contract.get("items")
            if item_contract is not None:
                for index, child in enumerate(current):
                    visit(child, item_contract, f"{path}[{index}]", depth + 1)
        elif actual_type == "string":
            if len(current) < contract.get("minLength", 0):
                raise SchemaInstanceError(f"{label} is too short at {path}")
            maximum = contract.get("maxLength")
            if maximum is not None and len(current) > maximum:
                raise SchemaInstanceError(f"{label} is too long at {path}")
            pattern = contract.get("pattern")
            if pattern is not None and re.search(pattern, current) is None:
                raise SchemaInstanceError(f"{label} has an invalid format at {path}")
        elif actual_type in {"integer", "number"}:
            if not math.isfinite(current):
                raise SchemaInstanceError(f"{label} is non-finite at {path}")
            minimum = contract.get("minimum")
            maximum = contract.get("maximum")
            if minimum is not None and current < minimum:
                raise SchemaInstanceError(f"{label} is below its minimum at {path}")
            if maximum is not None and current > maximum:
                raise SchemaInstanceError(f"{label} exceeds its maximum at {path}")

    visit(value, schema, "$", 0)


def canonical_json_value(value: Any, *, label: str) -> Any:
    """Normalize tuples and other encoder-supported values to canonical JSON."""

    import json

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaInstanceError(f"{label} must be a canonical JSON value") from exc
    _validate_json_graph(decoded, label=label)
    return decoded


def _validate_json_graph(value: Any, *, label: str) -> None:
    remaining = _MAX_INSTANCE_NODES
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise SchemaInstanceError(f"{label} contains too many values")
        if depth > _MAX_INSTANCE_DEPTH:
            raise SchemaInstanceError(f"{label} is nested too deeply")
        if current is None or isinstance(current, (bool, int, str)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise SchemaInstanceError(f"{label} contains a non-finite number")
            continue
        if isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
            continue
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise SchemaInstanceError(f"{label} contains an invalid object key")
                pending.append((child, depth + 1))
            continue
        raise SchemaInstanceError(f"{label} contains a non-JSON value")
