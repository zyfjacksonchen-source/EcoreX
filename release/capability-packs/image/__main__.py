"""Non-provider bridge contract for the Core-managed image implementation.

The executable exists so every Capability Pack has a probeable handshake.  It
never accepts a provider URL, credential, model override or image operation;
all image generation/vision execution remains in the backend-owned managed
image adapter selected from the frozen Turn snapshot.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


_PROTOCOL = "ecorex-managed-image-bridge-v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def main() -> int:
    request_id = "invalid-request"
    try:
        payload = sys.stdin.buffer.read(64 * 1024 + 1)
        if not 1 <= len(payload) <= 64 * 1024:
            raise ValueError("size")
        request = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique)
        if (
            not isinstance(request, dict)
            or set(request) != {"schema_version", "protocol", "request_id", "operation"}
            or request.get("schema_version") != 1
            or request.get("protocol") != _PROTOCOL
            or request.get("operation") != "describe"
            or not isinstance(request.get("request_id"), str)
            or _SAFE_ID.fullmatch(request["request_id"]) is None
        ):
            raise ValueError("contract")
        request_id = request["request_id"]
        response = {
            "adapter": "core-managed-image-v1",
            "provider_execution": False,
            "request_id": request_id,
            "runtime_api_version": "1.0.0",
            "schema_version": 1,
            "status": "completed",
            "tools": ["imagegen", "vision"],
        }
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        response = {
            "error_code": "managed_image_core_required",
            "request_id": request_id,
            "retryable": False,
            "schema_version": 1,
            "status": "failed",
        }
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
