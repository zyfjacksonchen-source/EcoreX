"""Bounded, canonical stdio contract shared by executable Capability Packs.

This module deliberately has no dependency on the EcoreX Runtime package.  A
pack receives one immutable Core-authored request on stdin, emits one canonical
JSON response on stdout, and exits.  Unknown fields, duplicate JSON keys,
non-finite numbers and oversized values are rejected before tool code runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys
from typing import Any, Mapping


PROTOCOL = "ecorex-stdio-tool-v1"
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILES = frozenset({"workspace-write", "danger-full-access"})


class ContractError(RuntimeError):
    """Stable pack-side error which never contains request data."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        normalized = (
            code
            if isinstance(code, str) and _SAFE_ID.fullmatch(code)
            else "pack_contract_failed"
        )
        self.code = normalized
        self.retryable = bool(retryable)
        super().__init__(normalized)


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    pack_id: str
    tool_id: str
    arguments: Mapping[str, Any]
    context: Mapping[str, Any]


def read_request(*, pack_id: str, tools: frozenset[str]) -> Request:
    payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not 1 <= len(payload) <= MAX_REQUEST_BYTES:
        raise ContractError("pack_request_size_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ContractError("pack_request_invalid") from None
    expected = {
        "schema_version",
        "protocol",
        "request_id",
        "pack_id",
        "tool_id",
        "arguments",
        "context",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractError("pack_request_invalid")
    request_id = value.get("request_id")
    tool_id = value.get("tool_id")
    if (
        value.get("schema_version") != 1
        or value.get("protocol") != PROTOCOL
        or value.get("pack_id") != pack_id
        or not isinstance(request_id, str)
        or _SAFE_ID.fullmatch(request_id) is None
        or not isinstance(tool_id, str)
        or tool_id not in tools
        or not isinstance(value.get("arguments"), Mapping)
        or not isinstance(value.get("context"), Mapping)
    ):
        raise ContractError("pack_request_identity_invalid")
    context = value["context"]
    context_keys = {
        "policy_snapshot_id",
        "capability_snapshot_id",
        "idempotency_key",
        "approved",
        "effective_sandbox",
        "workspace_roots",
        "sandbox_contract",
        "execution_scope",
    }
    roots = context.get("workspace_roots")
    if (
        set(context) != context_keys
        or not _safe_id(context.get("policy_snapshot_id"))
        or not _safe_id(context.get("capability_snapshot_id"))
        or (
            context.get("idempotency_key") is not None
            and not _bounded_text(context.get("idempotency_key"), 512)
        )
        or not isinstance(context.get("approved"), bool)
        or context.get("effective_sandbox") not in _PROFILES
        or not isinstance(roots, list)
        or not 1 <= len(roots) <= 32
        or not all(_bounded_text(root, 4096) for root in roots)
        or context.get("execution_scope") is not None
        and not _valid_execution_scope(context["execution_scope"])
    ):
        raise ContractError("pack_request_context_invalid")
    return Request(
        request_id=request_id,
        pack_id=pack_id,
        tool_id=tool_id,
        arguments=dict(value["arguments"]),
        context=dict(context),
    )


def write_completed(
    request: Request,
    result: Any,
    *,
    sandbox_contract_id: str | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request.request_id,
        "status": "completed",
        "result": result,
    }
    if sandbox_contract_id is not None:
        if not _safe_id(sandbox_contract_id):
            raise ContractError("pack_sandbox_contract_invalid")
        value["sandbox_contract_id"] = sandbox_contract_id
    _write(value)


def write_failed(
    request_id: str,
    error: ContractError,
    *,
    sandbox_contract_id: str | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request_id if _safe_id(request_id) else "invalid-request",
        "status": "failed",
        "error_code": error.code,
        "retryable": error.retryable,
    }
    if sandbox_contract_id is not None and _safe_id(sandbox_contract_id):
        value["sandbox_contract_id"] = sandbox_contract_id
    _write(value)


def run(pack_id: str, tools: frozenset[str], handler: Any) -> int:
    request: Request | None = None
    sandbox_contract_id: str | None = None
    try:
        request = read_request(pack_id=pack_id, tools=tools)
        raw_contract = request.context.get("sandbox_contract")
        if isinstance(raw_contract, Mapping):
            candidate = raw_contract.get("contract_id")
            if isinstance(candidate, str) and _safe_id(candidate):
                sandbox_contract_id = candidate
        result = handler(request)
        write_completed(
            request,
            result,
            sandbox_contract_id=sandbox_contract_id,
        )
        return 0
    except ContractError as error:
        write_failed(
            request.request_id if request is not None else "invalid-request",
            error,
            sandbox_contract_id=sandbox_contract_id,
        )
        return 0
    except BaseException:
        # Native/provider exception text can contain paths or credentials.  A
        # stable code is the only information allowed across this boundary.
        write_failed(
            request.request_id if request is not None else "invalid-request",
            ContractError("pack_internal_failure"),
            sandbox_contract_id=sandbox_contract_id,
        )
        return 0


def require_exact_arguments(
    arguments: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    if set(arguments) - required - optional or not required.issubset(arguments):
        raise ContractError("pack_arguments_invalid")


def bounded_text(value: Any, limit: int, *, code: str = "pack_arguments_invalid") -> str:
    if not _bounded_text(value, limit):
        raise ContractError(code)
    return value


def bounded_int(
    value: Any,
    minimum: int,
    maximum: int,
    *,
    code: str = "pack_arguments_invalid",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError(code)
    return value


def _write(value: Mapping[str, Any]) -> None:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        payload = (
            b'{"error_code":"pack_response_invalid","request_id":"invalid-request",'
            b'"retryable":false,"schema_version":1,"status":"failed"}'
        )
    if len(payload) > MAX_RESPONSE_BYTES:
        payload = (
            b'{"error_code":"pack_response_too_large","request_id":"invalid-request",'
            b'"retryable":false,"schema_version":1,"status":"failed"}'
        )
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _valid_execution_scope(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == {"job_id", "thread_id", "turn_id"}
        and all(_safe_id(value.get(key)) for key in value)
    )


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _bounded_text(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and "\x00" not in value
        and 1 <= len(value.encode("utf-8")) <= limit
    )


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
