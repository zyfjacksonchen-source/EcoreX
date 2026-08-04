from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from v030_production_operator import (
    EXPECTED_DOMAIN_HASH,
    EXPECTED_HOST_HASH,
    connect_operator,
)


REMOTE_PROGRAM = r'''import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

root = Path("/srv/ecorex-agent-usage-panel")
current = root / "current"
resolved = current.resolve(strict=True)
files = {}
pid_text = subprocess.run(
    ["systemctl", "show", "ecorex-usage-panel-api.service", "--property", "MainPID", "--value"],
    check=False,
    capture_output=True,
    text=True,
    timeout=10,
).stdout.strip()
pid = int(pid_text) if pid_text.isdigit() else 0
process_cwd = Path(f"/proc/{pid}/cwd").resolve() if pid > 0 else None
candidates = {
    "current/usage_panel_api.py": resolved / "usage_panel_api.py",
    "root/usage_panel_api.py": root / "usage_panel_api.py",
    "app/usage_panel_api.py": root / "app" / "usage_panel_api.py",
    "process/usage_panel_api.py": process_cwd / "usage_panel_api.py" if process_cwd else Path("/missing"),
    "app.js": resolved / "app.js",
    "index.html": resolved / "index.html",
    "styles.css": resolved / "styles.css",
}
for name, path in candidates.items():
    if not path.is_file() or path.is_symlink():
        continue
    payload = path.read_bytes()
    files[name] = {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "has_projection_v030": b"v0.3.0-usage-1" in payload,
        "has_root_kpis": b'payload["kpis"]' in payload,
        "has_reconciliation": b"reconciliation" in payload,
    }
active = subprocess.run(
    ["systemctl", "is-active", "ecorex-usage-panel-api.service"],
    check=False,
    capture_output=True,
    text=True,
    timeout=10,
).stdout.strip()
with urllib.request.urlopen("http://127.0.0.1:18105/api/health", timeout=10) as response:
    health = json.loads(response.read(65537).decode("utf-8"))
print(json.dumps({
    "status": "passed",
    "current_release": resolved.name,
    "process_working_release": process_cwd.name if process_cwd else None,
    "service_active": active == "active",
    "health": {
        "ok": health.get("ok"),
        "version": health.get("version"),
        "service": health.get("service"),
    },
    "files": files,
}, sort_keys=True, separators=(",", ":")))
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
    args = parser.parse_args()
    client = connect_operator(args.credential_file)
    try:
        stdin, stdout, stderr = client.exec_command("python3 -", timeout=60)
        stdin.write(REMOTE_PROGRAM)
        stdin.channel.shutdown_write()
        payload = stdout.read(1024 * 1024 + 1)
        error = stderr.read(4097)
        status = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if status != 0 or error or len(payload) > 1024 * 1024:
        raise SystemExit("production_usage_inspection_failed")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise SystemExit("production_usage_inspection_invalid")
    value.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "endpoint_identity": {
                "domain_hash": EXPECTED_DOMAIN_HASH,
                "ssh_host_hash": EXPECTED_HOST_HASH,
            },
        }
    )
    _write(args.output.resolve(), value)
    print(json.dumps({"status": "passed", "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
