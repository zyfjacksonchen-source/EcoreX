from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import uuid

from v030_production_operator import (
    EXPECTED_DOMAIN_HASH,
    EXPECTED_HOST_HASH,
    connect_operator,
)

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _remote_program(start: str, end: str, correlation_id: str) -> str:
    return f'''import json
import re
import sys
import urllib.request

MAX_BYTES = {MAX_RESPONSE_BYTES}
HEADERS = {{"Accept": "application/json", "X-Request-ID": {correlation_id!r}}}

def report_failure(error_type, error, _traceback):
    if hasattr(error, "code") and isinstance(error.code, int):
        code = "http_" + str(error.code)
    elif error.args and isinstance(error.args[0], str) and re.fullmatch(r"[a-z0-9_]+", error.args[0]):
        code = error.args[0]
    else:
        code = error_type.__name__.lower()
    print(json.dumps({{"status": "failed", "code": code}}, sort_keys=True, separators=(",", ":")))
    sys.exit(2)

sys.excepthook = report_failure

def load(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(MAX_BYTES + 1)
        response_id = response.headers.get("X-Request-ID", "")
    if len(payload) > MAX_BYTES:
        raise RuntimeError("usage_response_too_large")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("usage_response_invalid")
    return value, response_id

def numeric_projection(value):
    if not isinstance(value, dict) or len(value) > 64:
        return {{}}
    result = {{}}
    for key, item in value.items():
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", key) is None:
            continue
        if isinstance(item, bool) or item is None or isinstance(item, (int, float)):
            result[key] = item
    return result

data, data_response_id = load("http://127.0.0.1:18105/api/data?start={start}&end={end}")
audit, audit_response_id = load("http://127.0.0.1:18105/api/runtime-audit?start={start}&end={end}&limit=12000")
if data.get("projection_version") != audit.get("projection_version"):
    raise RuntimeError("projection_mismatch")
if data.get("kpis") != audit.get("kpis") or data.get("reconciliation") != audit.get("reconciliation"):
    print(json.dumps({{
        "status": "failed",
        "code": "projection_mismatch",
        "diagnostic": {{
            "kpis_match": data.get("kpis") == audit.get("kpis"),
            "reconciliation_match": data.get("reconciliation") == audit.get("reconciliation"),
            "usage_kpis": numeric_projection(data.get("kpis")),
            "audit_kpis": numeric_projection(audit.get("kpis")),
            "usage_reconciliation": numeric_projection(data.get("reconciliation")),
            "audit_reconciliation": numeric_projection(audit.get("reconciliation")),
        }},
    }}, sort_keys=True, separators=(",", ":")))
    sys.exit(2)
reconciliation = data.get("reconciliation")
if not isinstance(reconciliation, dict):
    raise RuntimeError("reconciliation_missing")
result = {{
    "schema_version": 1,
    "projection_version": data.get("projection_version"),
    "kpis": data.get("kpis"),
    "reconciliation": reconciliation,
    "canonical_record_count": reconciliation.get("canonical_record_count"),
    "replaced_duplicate_count": reconciliation.get("replaced_duplicate_count"),
    "unassociated_record_count": reconciliation.get("unassociated_record_count"),
    "missing_provider_usage_count": reconciliation.get("missing_provider_usage_count"),
    "request_correlation": {{
        "client_request_id": {correlation_id!r},
        "usage_response_id": data_response_id,
        "audit_response_id": audit_response_id,
    }},
    "usage_audit_match": True,
}}
print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
'''


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    datetime.fromisoformat(args.start)
    datetime.fromisoformat(args.end)
    try:
        client = connect_operator(args.credential_file)
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("production_operator_connection_failed") from None
    correlation_id = str(uuid.uuid4())
    try:
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=150)
        stdin.write(_remote_program(args.start, args.end, correlation_id))
        stdin.channel.shutdown_write()
        raw = stdout.read(MAX_RESPONSE_BYTES + 1)
        error = stderr.read(4097)
        status = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if error or len(raw) > MAX_RESPONSE_BYTES:
        raise SystemExit("production_usage_audit_failed")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("production_usage_audit_invalid") from None
    if status != 0:
        code = result.get("code") if isinstance(result, dict) else None
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "unknown"
        failure = {
            "schema_version": 1,
            "status": "failed",
            "code": code,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "range": {"start": args.start, "end": args.end},
            "endpoint_identity": {
                "domain_hash": EXPECTED_DOMAIN_HASH,
                "ssh_host_hash": EXPECTED_HOST_HASH,
                "transport": "ssh-loopback",
                "usage_port": 18105,
            },
            "diagnostic": result.get("diagnostic", {}),
        }
        _write_atomic(args.output.resolve(), failure)
        raise SystemExit(f"production_usage_audit_failed:{code}")
    if not isinstance(result, dict) or result.get("usage_audit_match") is not True:
        raise SystemExit("production_usage_audit_invalid")
    result.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "range": {"start": args.start, "end": args.end},
            "endpoint_identity": {
                "domain_hash": EXPECTED_DOMAIN_HASH,
                "ssh_host_hash": EXPECTED_HOST_HASH,
                "transport": "ssh-loopback",
                "usage_port": 18105,
            },
        }
    )
    _write_atomic(args.output.resolve(), result)
    print(json.dumps({"status": "passed", "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
