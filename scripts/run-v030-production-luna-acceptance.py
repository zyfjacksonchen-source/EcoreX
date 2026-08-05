from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile

from v030_production_operator import (
    EXPECTED_DOMAIN_HASH,
    EXPECTED_HOST_HASH,
    connect_operator,
)


def _program(mode: str, allowed_provider_host_hash: str, allow_http_provider: bool) -> str:
    return f'''import hashlib
import ipaddress
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

MODE = {mode!r}
EXPECTED_PROVIDER_HOST_HASH = {allowed_provider_host_hash!r}
ALLOW_HTTP_PROVIDER = {allow_http_provider!r}
paths = (
    Path("/opt/ecorex-web/state/config.json"),
    Path("/opt/ecorex-web/current/runtime/config.json"),
)
config = None
for path in paths:
    if path.is_file() and not path.is_symlink() and 1 <= path.stat().st_size <= 1024 * 1024:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict) and value.get("open_ai_api_key"):
            config = value
            break
if config is None:
    raise SystemExit("model_config_unavailable")
api_key = config.get("open_ai_api_key")
api_base = str(config.get("open_ai_api_base") or "https://api.openai.com/v1").rstrip("/")
parsed = urllib.parse.urlsplit(api_base)
provider_host_hash = (
    hashlib.sha256(parsed.hostname.encode("utf-8")).hexdigest().upper()[:16]
    if parsed.hostname
    else ""
)
try:
    provider_ip = ipaddress.ip_address(parsed.hostname or "")
except ValueError:
    provider_ip = None
origin_valid = (
    parsed.scheme in ({{"https", "http"}} if ALLOW_HTTP_PROVIDER else {{"https"}})
    and bool(parsed.hostname)
    and not parsed.username
    and not parsed.password
    and not parsed.query
    and not parsed.fragment
)
base_report = {{
    "status": "ready" if MODE == "inspect" and origin_valid else "failed",
    "configured_model": str(config.get("model") or ""),
    "configured_bot_type": str(config.get("bot_type") or ""),
    "provider_host_hash": provider_host_hash,
    "provider_transport_authorization": "explicit-http" if parsed.scheme == "http" and ALLOW_HTTP_PROVIDER else "https",
    "has_provider_credential": True,
    "provider_origin": {{
        "scheme": parsed.scheme,
        "has_hostname": bool(parsed.hostname),
        "has_userinfo": bool(parsed.username or parsed.password),
        "has_query": bool(parsed.query),
        "has_fragment": bool(parsed.fragment),
        "path_depth": len([part for part in parsed.path.split("/") if part]),
        "is_https_valid": parsed.scheme == "https" and origin_valid,
        "is_transport_authorized": origin_valid,
        "is_ip_literal": provider_ip is not None,
        "is_loopback": bool(provider_ip and provider_ip.is_loopback),
        "is_private": bool(provider_ip and provider_ip.is_private),
        "port": parsed.port,
    }},
}}
if MODE == "inspect":
    print(json.dumps(base_report, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if origin_valid else 2)
if not origin_valid:
    raise SystemExit("provider_origin_invalid")
if provider_host_hash != EXPECTED_PROVIDER_HOST_HASH:
    raise SystemExit("provider_origin_mismatch")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None

payload = json.dumps({{
    "model": "gpt-5.6-luna",
    "input": "Reply with exactly: OK",
    "reasoning": {{"effort": "max"}},
    "max_output_tokens": 32,
    "stream": False,
}}, separators=(",", ":")).encode("utf-8")
request = urllib.request.Request(
    api_base + "/responses",
    data=payload,
    method="POST",
    headers={{
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }},
)
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
        body = response.read(16 * 1024 * 1024 + 1)
        status_code = response.status
except urllib.error.HTTPError as error:
    print(json.dumps({{**base_report, "status": "failed", "http_status": error.code}}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)
if len(body) > 16 * 1024 * 1024:
    raise SystemExit("provider_response_too_large")
value = json.loads(body.decode("utf-8"))
usage = value.get("usage") if isinstance(value, dict) else None
if not isinstance(usage, dict):
    raise SystemExit("provider_usage_missing")
reported_model = str(value.get("model") or "")
if reported_model != "gpt-5.6-luna":
    raise SystemExit("provider_model_mismatch")
report = {{
    **base_report,
    "status": "passed",
    "http_status": status_code,
    "requested_model": "gpt-5.6-luna",
    "reported_model": reported_model,
    "reasoning_effort": "max",
    "usage": {{
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }},
    "response_id_hash": hashlib.sha256(str(value.get("id") or "").encode("utf-8")).hexdigest()[:16],
}}
print(json.dumps(report, sort_keys=True, separators=(",", ":")))
'''


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("inspect", "run"), required=True)
    parser.add_argument("--allowed-provider-host-hash", default="")
    parser.add_argument("--allow-http-provider", action="store_true")
    args = parser.parse_args()
    if args.mode == "run" and re.fullmatch(r"[0-9A-F]{16}", args.allowed_provider_host_hash) is None:
        raise SystemExit("allowed_provider_host_hash_invalid")
    client = connect_operator(args.credential_file)
    try:
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=240)
        stdin.write(_program(args.mode, args.allowed_provider_host_hash, args.allow_http_provider))
        stdin.channel.shutdown_write()
        payload = stdout.read(1024 * 1024 + 1)
        error = stderr.read(4097)
        status = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if len(payload) > 1024 * 1024 or len(error) > 4096:
        raise SystemExit("production_luna_acceptance_invalid")
    if error:
        code = error.decode("utf-8", errors="replace").strip()
        if re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "remote_execution_failed"
        value = {"status": "failed", "code": code}
    else:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SystemExit("production_luna_acceptance_invalid") from None
    if not isinstance(value, dict):
        raise SystemExit("production_luna_acceptance_invalid")
    value.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": "production",
            "endpoint_identity": {
                "domain_hash": EXPECTED_DOMAIN_HASH,
                "ssh_host_hash": EXPECTED_HOST_HASH,
                "transport": "ssh",
            },
            "metered_request": args.mode == "run",
        }
    )
    _write(args.output.resolve(), value)
    if status != 0 or value.get("status") not in {"ready", "passed"}:
        raise SystemExit("production_luna_acceptance_failed")
    print(json.dumps({"status": value["status"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
