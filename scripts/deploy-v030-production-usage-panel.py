from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from v030_production_operator import (
    EXPECTED_DOMAIN_HASH,
    EXPECTED_HOST_HASH,
    connect_operator,
)


SERVICE = "ecorex-usage-panel-api.service"
SERVER_ROOT = "/srv/ecorex-agent-usage-panel/server"
REMOTE_SOURCE = f"{SERVER_ROOT}/usage_panel_api.py"
MAX_SOURCE_BYTES = 1024 * 1024
VERSION_BINDING = b"from ecorex import __version__\n\nVERSION = __version__"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_contract_matches(
    payload: bytes, *, expected_version: str, expected_projection: str
) -> bool:
    return (
        f'VERSION = "{expected_version}"'.encode() in payload
        and f'USAGE_PROJECTION_VERSION = "{expected_projection}"'.encode() in payload
        and b'audit["usageKpis"]' in payload
        and b'payload["reconciliation"]' in payload
    )


def _materialize_source(payload: bytes, *, expected_version: str) -> bytes:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version) is None:
        raise ValueError("usage_version_invalid")
    if payload.count(VERSION_BINDING) != 1:
        raise ValueError("usage_version_binding_invalid")
    return payload.replace(
        VERSION_BINDING,
        f'VERSION = "{expected_version}"'.encode(),
    )


def _exec(client, command: str, *, timeout: int = 60) -> tuple[int, bytes, bytes]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdin.channel.shutdown_write()
    output = stdout.read(1024 * 1024 + 1)
    error = stderr.read(4097)
    return stdout.channel.recv_exit_status(), output, error


def _verify_program(start: str, end: str, expected_projection: str) -> str:
    return f'''import json
import re
import sys
import urllib.error
import urllib.request

def report_failure(error_type, error, _traceback):
    code = error.args[0] if error.args and isinstance(error.args[0], str) else "verification_failed"
    if re.fullmatch(r"[a-z0-9_]+", code) is None:
        code = "verification_failed"
    print(json.dumps({{"status": "failed", "code": code}}, sort_keys=True, separators=(",", ":")))
    sys.exit(2)

sys.excepthook = report_failure

def load(url, label):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read(64 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        body = error.read(65537)
        try:
            detail = json.loads(body.decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        if error.code == 500 and "no such column" in str(detail):
            raise RuntimeError(label + "_schema_column_missing")
        if error.code == 500 and "no such table" in str(detail):
            raise RuntimeError(label + "_schema_table_missing")
        raise RuntimeError(label + "_http_" + str(error.code))
    if len(payload) > 64 * 1024 * 1024:
        raise RuntimeError(label + "_response_too_large")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(label + "_response_invalid")
    return value

health = load("http://127.0.0.1:18105/api/health", "health")
data = load("http://127.0.0.1:18105/api/data?start={start}&end={end}", "data")
audit = load("http://127.0.0.1:18105/api/runtime-audit?start={start}&end={end}&limit=12000", "audit")
if health.get("ok") is not True:
    raise RuntimeError("health_failed")
if data.get("projection_version") != {expected_projection!r}:
    raise RuntimeError("projection_invalid")
if data.get("projection_version") != audit.get("projection_version"):
    raise RuntimeError("projection_mismatch")
if data.get("kpis") != audit.get("kpis"):
    raise RuntimeError("kpi_mismatch")
if data.get("reconciliation") != audit.get("reconciliation"):
    raise RuntimeError("reconciliation_mismatch")
print(json.dumps({{
    "status": "passed",
    "projection_version": data.get("projection_version"),
    "canonical_record_count": data.get("reconciliation", {{}}).get("canonical_record_count"),
    "replaced_duplicate_count": data.get("reconciliation", {{}}).get("replaced_duplicate_count"),
    "unassociated_record_count": data.get("reconciliation", {{}}).get("unassociated_record_count"),
    "missing_provider_usage_count": data.get("reconciliation", {{}}).get("missing_provider_usage_count"),
}}, sort_keys=True, separators=(",", ":")))
'''


def _run_verification(
    client, start: str, end: str, expected_projection: str
) -> dict:
    stdin, stdout, stderr = client.exec_command("python3 -", timeout=150)
    stdin.write(_verify_program(start, end, expected_projection))
    stdin.channel.shutdown_write()
    payload = stdout.read(1024 * 1024 + 1)
    error = stderr.read(4097)
    status = stdout.channel.recv_exit_status()
    if error or len(payload) > 1024 * 1024:
        raise RuntimeError("postdeploy_verification_failed")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("postdeploy_verification_failed")
    if status != 0:
        code = value.get("code") if isinstance(value, dict) else None
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "failed"
        raise RuntimeError(f"postdeploy_verification_{code}")
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise RuntimeError("postdeploy_verification_failed")
    return value


def _replace(sftp, target: str, payload: bytes, mode: int) -> None:
    temporary = f"{target}.tmp-{uuid.uuid4().hex}"
    try:
        try:
            sftp.lstat(temporary)
        except OSError:
            pass
        else:
            raise RuntimeError("remote_temporary_conflict")
        with sftp.open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
        sftp.chmod(temporary, mode)
        sftp.posix_rename(temporary, target)
    finally:
        try:
            sftp.remove(temporary)
        except OSError:
            pass


def _restart(client) -> None:
    status, _output, error = _exec(
        client, f"systemctl restart {SERVICE}", timeout=60
    )
    if status != 0 or error:
        raise RuntimeError("service_restart_failed")
    for _ in range(30):
        status, output, error = _exec(
            client, f"systemctl is-active {SERVICE}", timeout=10
        )
        if status == 0 and not error and output.strip() == b"active":
            return
        time.sleep(1)
    raise RuntimeError("service_not_active")


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
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("ecorex/control_plane/usage_panel_service.py"),
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-projection", required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    try:
        payload = _materialize_source(
            source.read_bytes(), expected_version=args.expected_version
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if not 1 <= len(payload) <= MAX_SOURCE_BYTES or not _source_contract_matches(
        payload,
        expected_version=args.expected_version,
        expected_projection=args.expected_projection,
    ):
        raise SystemExit("usage_source_invalid")
    after_sha = _sha256(payload)
    client = connect_operator(args.credential_file)
    sftp = client.open_sftp()
    before = b""
    before_sha = ""
    replaced = False
    rolled_back = False
    verification: dict = {}
    stage = "inspect"
    try:
        attributes = sftp.lstat(REMOTE_SOURCE)
        if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISREG(attributes.st_mode):
            raise RuntimeError("remote_source_invalid")
        if not 1 <= attributes.st_size <= MAX_SOURCE_BYTES:
            raise RuntimeError("remote_source_invalid")
        with sftp.open(REMOTE_SOURCE, "rb") as handle:
            before = handle.read(MAX_SOURCE_BYTES + 1)
        current = sftp.lstat(REMOTE_SOURCE)
        if current.st_size != attributes.st_size or len(before) != attributes.st_size:
            raise RuntimeError("remote_source_changed")
        before_sha = _sha256(before)
        backup = f"{REMOTE_SOURCE}.backup-{before_sha[:16]}"
        stage = "backup"
        try:
            backup_attributes = sftp.lstat(backup)
        except OSError:
            _replace(sftp, backup, before, stat.S_IMODE(attributes.st_mode))
        else:
            if backup_attributes.st_size != len(before):
                raise RuntimeError("remote_backup_conflict")
            with sftp.open(backup, "rb") as handle:
                if _sha256(handle.read(MAX_SOURCE_BYTES + 1)) != before_sha:
                    raise RuntimeError("remote_backup_conflict")
        _replace(sftp, REMOTE_SOURCE, payload, stat.S_IMODE(attributes.st_mode))
        replaced = True
        stage = "restart"
        _restart(client)
        stage = "verify"
        verification = _run_verification(
            client, args.start, args.end, args.expected_projection
        )
    except Exception as error:
        reason = str(error)
        if re.fullmatch(r"[a-z0-9_]+", reason) is None:
            reason = type(error).__name__.lower()
        if replaced and before:
            _replace(
                sftp,
                REMOTE_SOURCE,
                before,
                stat.S_IMODE(attributes.st_mode),
            )
            _restart(client)
            rolled_back = True
        receipt = {
            "schema_version": 1,
            "status": "rolled_back" if rolled_back else "failed",
            "reason": reason,
            "stage": stage,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "endpoint_identity": {
                "domain_hash": EXPECTED_DOMAIN_HASH,
                "ssh_host_hash": EXPECTED_HOST_HASH,
            },
            "remote_source": REMOTE_SOURCE,
            "before_sha256": before_sha,
            "candidate_sha256": after_sha,
            "rolled_back": rolled_back,
        }
        _write(args.receipt.resolve(), receipt)
        raise SystemExit("production_usage_deploy_failed") from None
    finally:
        sftp.close()
        client.close()
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_identity": {
            "domain_hash": EXPECTED_DOMAIN_HASH,
            "ssh_host_hash": EXPECTED_HOST_HASH,
        },
        "remote_source": REMOTE_SOURCE,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "rolled_back": False,
        "verification": verification,
    }
    _write(args.receipt.resolve(), receipt)
    print(json.dumps({"status": "passed", "receipt": str(args.receipt)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
