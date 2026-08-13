from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from v030_production_operator import (
    EXPECTED_DOMAIN_HASH,
    EXPECTED_HOST_HASH,
    connect_operator,
)


SERVICE = "ecorex-usage-panel-api.service"
REMOTE_ROOT = "/srv/ecorex-agent-usage-panel"
PAYLOAD_NAMES = (
    "usage_panel_api.py",
    "index.html",
    "app.js",
    "styles.css",
    "data.js",
)
CANDIDATE_NAMES = PAYLOAD_NAMES + ("release-manifest.json", "release-receipt.json")
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
VERSION_BINDING = b"from ecorex import __version__\n\nVERSION = __version__"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


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
    return payload.replace(VERSION_BINDING, f'VERSION = "{expected_version}"'.encode())


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=False, capture_output=True, timeout=30
    )
    if result.returncode != 0 or result.stderr:
        raise ValueError("usage_source_identity_invalid")
    return result.stdout


def _build_candidate(
    repo: Path,
    *,
    expected_version: str,
    expected_projection: str,
    expected_source_sha: str,
) -> dict:
    repo = repo.resolve(strict=True)
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_sha) is None:
        raise ValueError("usage_source_sha_invalid")
    if re.fullmatch(r"[a-zA-Z0-9._-]+", expected_projection) is None:
        raise ValueError("usage_projection_invalid")
    if _git(repo, "rev-parse", "HEAD").decode().strip() != expected_source_sha:
        raise ValueError("usage_source_head_mismatch")

    paths = {
        "usage_panel_api.py": Path("ecorex/control_plane/usage_panel_service.py"),
        **{
            name: Path("ecorex/control_plane/usage_panel_web") / name
            for name in PAYLOAD_NAMES
            if name != "usage_panel_api.py"
        },
    }
    committed: dict[str, bytes] = {}
    for name, relative in paths.items():
        local = (repo / relative).read_bytes()
        if local != _git(repo, "show", f"{expected_source_sha}:{relative.as_posix()}"):
            raise ValueError("usage_source_dirty")
        committed[name] = local

    payloads = dict(committed)
    payloads["usage_panel_api.py"] = _materialize_source(
        committed["usage_panel_api.py"], expected_version=expected_version
    )
    if not _source_contract_matches(
        payloads["usage_panel_api.py"],
        expected_version=expected_version,
        expected_projection=expected_projection,
    ):
        raise ValueError("usage_source_invalid")
    compile(payloads["usage_panel_api.py"], "usage_panel_api.py", "exec")
    if any(not value or len(value) > MAX_FILE_BYTES for value in payloads.values()):
        raise ValueError("usage_candidate_file_invalid")
    if not all(
        marker in payloads["index.html"]
        for marker in (b"styles.css", b"data.js", b"app.js")
    ) or not all(
        marker in payloads["app.js"] for marker in (b"./api/data", b"./api/runtime-audit")
    ):
        raise ValueError("usage_web_contract_invalid")

    inventory = {
        name: {"size": len(payloads[name]), "sha256": _sha256(payloads[name])}
        for name in PAYLOAD_NAMES
    }
    manifest = _json_bytes(
        {
            "schema_version": 1,
            "source_sha": expected_source_sha,
            "version": expected_version,
            "projection_version": expected_projection,
            "inventory": inventory,
        }
    )
    receipt = _json_bytes(
        {
            "schema_version": 1,
            "status": "prepared",
            "source_sha": expected_source_sha,
            "version": expected_version,
            "manifest_sha256": _sha256(manifest),
        }
    )
    files = {
        **payloads,
        "release-manifest.json": manifest,
        "release-receipt.json": receipt,
    }
    return {
        "release_name": (
            f"v{expected_version}-{expected_source_sha[:12]}-{_sha256(manifest)[:12]}"
        ),
        "files": files,
        "manifest_sha256": _sha256(manifest),
        "receipt_sha256": _sha256(receipt),
        "inventory": inventory,
    }


_REMOTE_LIBRARY = r'''
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "ecorex-usage-panel-api.service"
PAYLOAD_NAMES = (
    "usage_panel_api.py",
    "index.html",
    "app.js",
    "styles.css",
    "data.js",
)
CANDIDATE_NAMES = frozenset(PAYLOAD_NAMES + ("release-manifest.json", "release-receipt.json"))
STATIC_NAMES = ("index.html", "app.js", "styles.css", "data.js")
MAX_FILE_BYTES = 8 * 1024 * 1024
READINESS_TIMEOUT_SECONDS = 30
READINESS_POLL_SECONDS = 1
HEALTH_REQUEST_TIMEOUT_SECONDS = 2


class DeploymentLock:
    def __init__(self, path, timeout=0):
        if timeout != 0:
            raise ValueError("deployment_lock_timeout_invalid")
        self.path = Path(path)
        self.descriptor = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError:
            raise RuntimeError("deployment_lock_invalid") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("deployment_lock_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise RuntimeError("deployment_lock_busy") from None
            self.descriptor = descriptor
            return self
        except BaseException:
            os.close(descriptor)
            raise

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is None:
            raise RuntimeError("deployment_lock_not_owned")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _read_regular(path, limit=MAX_FILE_BYTES):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("release_file_invalid")
    if not 1 <= metadata.st_size <= limit:
        raise RuntimeError("release_file_invalid")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise RuntimeError("release_file_changed")
    return payload


def _json(path):
    try:
        value = json.loads(_read_regular(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("release_json_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("release_json_invalid")
    return value


def _inventory(root, names=PAYLOAD_NAMES):
    result = {}
    for name in names:
        payload = _read_regular(root / name)
        result[name] = {"size": len(payload), "sha256": _sha256(payload)}
    return result


def _validate_candidate(path, manifest_sha, receipt_sha, version, projection):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("candidate_directory_invalid")
    if {item.name for item in path.iterdir()} != CANDIDATE_NAMES:
        raise RuntimeError("candidate_files_invalid")
    manifest_bytes = _read_regular(path / "release-manifest.json")
    receipt_bytes = _read_regular(path / "release-receipt.json")
    if _sha256(manifest_bytes) != manifest_sha or _sha256(receipt_bytes) != receipt_sha:
        raise RuntimeError("candidate_receipt_digest_mismatch")
    manifest = _json(path / "release-manifest.json")
    receipt = _json(path / "release-receipt.json")
    if set(manifest) != {"schema_version", "source_sha", "version", "projection_version", "inventory"}:
        raise RuntimeError("candidate_manifest_invalid")
    if (
        manifest.get("schema_version") != 1
        or re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_sha", ""))) is None
        or manifest.get("version") != version
        or manifest.get("projection_version") != projection
        or manifest.get("inventory") != _inventory(path)
    ):
        raise RuntimeError("candidate_manifest_invalid")
    if receipt != {
        "schema_version": 1,
        "status": "prepared",
        "source_sha": manifest["source_sha"],
        "version": version,
        "manifest_sha256": manifest_sha,
    }:
        raise RuntimeError("candidate_receipt_invalid")
    compile(_read_regular(path / "usage_panel_api.py"), "usage_panel_api.py", "exec")
    html = _read_regular(path / "index.html")
    app = _read_regular(path / "app.js")
    if not all(value in html for value in (b"styles.css", b"data.js", b"app.js")):
        raise RuntimeError("candidate_web_invalid")
    if not all(value in app for value in (b"./api/data", b"./api/runtime-audit")):
        raise RuntimeError("candidate_web_invalid")
    return manifest


def _atomic_symlink(target, link):
    temporary = link.with_name("." + link.name + ".tmp-" + str(os.getpid()))
    try:
        temporary.unlink(missing_ok=True)
        os.symlink(str(target), temporary)
        os.replace(temporary, link)
        descriptor = os.open(str(link.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write(path, payload, mode):
    temporary = path.with_name("." + path.name + ".tmp-" + str(os.getpid()))
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restart_service():
    restarted = subprocess.run(
        ["systemctl", "restart", SERVICE], check=False, capture_output=True, timeout=60
    )
    if restarted.returncode != 0:
        raise RuntimeError("service_restart_failed")
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("service_not_ready")
        active = subprocess.run(
            ["systemctl", "is-active", SERVICE],
            check=False,
            capture_output=True,
            timeout=max(0.1, min(10, remaining)),
        )
        if (
            active.returncode == 0
            and active.stdout.strip() == b"active"
            and _health_ready(
                timeout=max(0.1, min(HEALTH_REQUEST_TIMEOUT_SECONDS, remaining))
            )
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(READINESS_POLL_SECONDS, remaining))


def _health_ready(timeout=HEALTH_REQUEST_TIMEOUT_SECONDS):
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:18105/api/health", timeout=timeout
        ) as response:
            value = json.loads(response.read(65537).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("ok") is True
        and value.get("service") == "ecorex-usage-panel-api"
    )


def _load(url, label):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read(64 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(label + "_http_" + str(error.code)) from None
    if len(payload) > 64 * 1024 * 1024:
        raise RuntimeError(label + "_response_too_large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(label + "_response_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError(label + "_response_invalid")
    return value


def _verify_live(start, end, version, projection):
    health = _load("http://127.0.0.1:18105/api/health", "health")
    query = urllib.parse.urlencode({"start": start, "end": end})
    data = _load("http://127.0.0.1:18105/api/data?" + query, "data")
    audit = _load(
        "http://127.0.0.1:18105/api/runtime-audit?" + query + "&limit=12000",
        "audit",
    )
    if health.get("ok") is not True or health.get("version") != version:
        raise RuntimeError("health_identity_invalid")
    if data.get("projection_version") != projection or audit.get("projection_version") != projection:
        raise RuntimeError("projection_invalid")
    if data.get("kpis") != audit.get("kpis"):
        raise RuntimeError("kpi_mismatch")
    if data.get("reconciliation") != audit.get("reconciliation"):
        raise RuntimeError("reconciliation_mismatch")
    return {
        "status": "passed",
        "version": version,
        "projection_version": projection,
        "kpis_sha256": _sha256(json.dumps(data.get("kpis"), sort_keys=True, separators=(",", ":")).encode()),
        "reconciliation_sha256": _sha256(json.dumps(data.get("reconciliation"), sort_keys=True, separators=(",", ":")).encode()),
        "canonical_record_count": data.get("reconciliation", {}).get("canonical_record_count"),
        "replaced_duplicate_count": data.get("reconciliation", {}).get("replaced_duplicate_count"),
        "unassociated_record_count": data.get("reconciliation", {}).get("unassociated_record_count"),
        "missing_provider_usage_count": data.get("reconciliation", {}).get("missing_provider_usage_count"),
    }


def _service_active():
    active = subprocess.run(
        ["systemctl", "is-active", SERVICE], check=False, capture_output=True, timeout=10
    )
    return active.returncode == 0 and active.stdout.strip() == b"active"


def _preflight_health():
    health = _load("http://127.0.0.1:18105/api/health", "health")
    if health.get("ok") is not True or health.get("service") != "ecorex-usage-panel-api":
        raise RuntimeError("preflight_health_invalid")
    return {"ok": True, "version": health.get("version"), "service": health.get("service")}


def _preflight(root, service_active, health_check):
    releases = root / "releases"
    current = root / "current"
    server_source = root / "server" / "usage_panel_api.py"
    if root.is_symlink() or not root.is_dir() or releases.is_symlink() or not releases.is_dir():
        raise RuntimeError("remote_root_invalid")
    if not current.is_symlink():
        raise RuntimeError("current_pointer_invalid")
    previous = current.resolve(strict=True)
    if previous.parent != releases.resolve(strict=True) or previous.is_symlink() or not previous.is_dir():
        raise RuntimeError("current_release_invalid")
    for name in STATIC_NAMES:
        _read_regular(previous / name)
    server_metadata = server_source.lstat()
    if not (stat.S_ISREG(server_metadata.st_mode) or stat.S_ISLNK(server_metadata.st_mode)):
        raise RuntimeError("service_source_invalid")
    _read_regular(server_source.resolve(strict=True))
    if not service_active():
        raise RuntimeError("service_not_active")
    health = health_check()
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise RuntimeError("preflight_health_invalid")
    return previous, server_source, server_metadata, health


def _backup(root, previous, server_source, server_metadata):
    backups = root / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = Path(
        tempfile.mkdtemp(
            prefix=(
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                + "-"
                + _sha256(str(previous).encode())[:12]
                + "-"
            ),
            dir=backups,
        )
    )
    static = backup / "static"
    server = backup / "server"
    static.mkdir()
    server.mkdir()
    static_inventory = {}
    for name in STATIC_NAMES:
        source = previous / name
        if not source.exists():
            continue
        payload = _read_regular(source)
        (static / name).write_bytes(payload)
        static_inventory[name] = {"size": len(payload), "sha256": _sha256(payload)}
    resolved_server = server_source.resolve(strict=True)
    server_payload = _read_regular(resolved_server)
    (server / "usage_panel_api.py").write_bytes(server_payload)
    state = {
        "previous_release": str(previous),
        "static_inventory": static_inventory,
        "server": {
            "kind": "symlink" if stat.S_ISLNK(server_metadata.st_mode) else "regular",
            "link_target": os.readlink(server_source) if stat.S_ISLNK(server_metadata.st_mode) else None,
            "mode": stat.S_IMODE(server_metadata.st_mode),
            "size": len(server_payload),
            "sha256": _sha256(server_payload),
        },
    }
    (backup / "backup.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    return backup, state


def _restore(current, server_source, backup, state):
    _atomic_symlink(state["previous_release"], current)
    server = state["server"]
    if server["kind"] == "symlink":
        _atomic_symlink(server["link_target"], server_source)
    else:
        payload = _read_regular(backup / "server" / "usage_panel_api.py")
        if len(payload) != server["size"] or _sha256(payload) != server["sha256"]:
            raise RuntimeError("service_backup_invalid")
        _atomic_write(server_source, payload, server["mode"])


def _restored(current, server_source, state):
    if current.resolve(strict=True) != Path(state["previous_release"]):
        return False
    server = state["server"]
    if server["kind"] == "symlink":
        return server_source.is_symlink() and os.readlink(server_source) == server["link_target"]
    payload = _read_regular(server_source)
    return len(payload) == server["size"] and _sha256(payload) == server["sha256"]


def activate(
    *,
    root,
    incoming_name,
    release_name,
    expected_manifest_sha,
    expected_receipt_sha,
    expected_version,
    expected_projection,
    start="",
    end="",
    restart=None,
    verify=None,
    service_active=None,
    health_check=None,
    lock_path=None,
):
    root = Path(root).resolve(strict=True)
    if re.fullmatch(r"\.incoming-[0-9a-f]{32}", incoming_name) is None:
        raise RuntimeError("incoming_name_invalid")
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}-[0-9a-f]{12}", release_name) is None:
        raise RuntimeError("release_name_invalid")
    incoming = root / "releases" / incoming_name
    release = root / "releases" / release_name
    current = root / "current"
    restart = restart or _restart_service
    verify = verify or (lambda: _verify_live(start, end, expected_version, expected_projection))
    service_active = service_active or _service_active
    health_check = health_check or _preflight_health
    lock_path = Path(lock_path or "/run/lock/ecorex-usage-panel-deploy.lock")
    stage = "preflight"
    mutated = False
    backup = None
    state = None
    previous = None
    try:
        with DeploymentLock(lock_path, timeout=0):
          try:
            previous, server_source, server_metadata, preflight = _preflight(
                root, service_active, health_check
            )
            stage = "candidate_validation"
            _validate_candidate(
                incoming,
                expected_manifest_sha,
                expected_receipt_sha,
                expected_version,
                expected_projection,
            )
            if release.exists():
                _validate_candidate(
                    release,
                    expected_manifest_sha,
                    expected_receipt_sha,
                    expected_version,
                    expected_projection,
                )
                shutil.rmtree(incoming)
            else:
                os.replace(incoming, release)
            stage = "backup"
            backup, state = _backup(root, previous, server_source, server_metadata)
            stage = "adapter"
            mutated = True
            _atomic_symlink("../current/usage_panel_api.py", server_source)
            stage = "activate"
            _atomic_symlink(str(release), current)
            stage = "restart"
            restart()
            stage = "readback"
            verification = verify()
            if not isinstance(verification, dict) or verification.get("status") != "passed":
                raise RuntimeError("postdeploy_verification_failed")
            _validate_candidate(
                current.resolve(strict=True),
                expected_manifest_sha,
                expected_receipt_sha,
                expected_version,
                expected_projection,
            )
            if server_source.resolve(strict=True) != (release / "usage_panel_api.py"):
                raise RuntimeError("service_adapter_readback_failed")
            return {
                "status": "passed",
                "stage": "complete",
                "release": str(release),
                "previous_release": str(previous),
                "backup": str(backup),
                "rolled_back": False,
                "inventory": _inventory(release),
                "preflight": preflight,
                "verification": verification,
            }
          except Exception as error:
            reason = str(error)
            if re.fullmatch(r"[a-z0-9_]+", reason) is None:
                reason = type(error).__name__.lower()
            rolled_back = False
            rollback_reason = None
            if mutated and backup is not None and state is not None:
                try:
                    _restore(current, root / "server" / "usage_panel_api.py", backup, state)
                    restart()
                    health = health_check()
                    rolled_back = (
                        isinstance(health, dict)
                        and health.get("ok") is True
                        and _restored(
                            current,
                            root / "server" / "usage_panel_api.py",
                            state,
                        )
                    )
                except Exception as rollback_error:
                    rollback_reason = str(rollback_error)
            return {
                "status": "rolled_back" if rolled_back else "failed",
                "stage": stage,
                "reason": reason,
                "rollback_reason": rollback_reason,
                "release": str(release),
                "previous_release": str(previous) if previous else None,
                "backup": str(backup) if backup else None,
                "rolled_back": rolled_back,
            }
    except Exception as error:
        reason = str(error)
        if re.fullmatch(r"[a-z0-9_]+", reason) is None:
            reason = type(error).__name__.lower()
        return {
            "status": "failed",
            "stage": "lock",
            "reason": reason,
            "rollback_reason": None,
            "release": str(release),
            "previous_release": None,
            "backup": None,
            "rolled_back": False,
        }
'''


def _remote_program(config: dict) -> str:
    return (
        _REMOTE_LIBRARY
        + "\nif __name__ == '__main__':\n"
        + "    result = activate(**"
        + repr(config)
        + ")\n"
        + "    print(json.dumps(result, sort_keys=True, separators=(',', ':')))\n"
        + "    raise SystemExit(0 if result.get('status') == 'passed' else 2)\n"
    )


def _upload_candidate(sftp, incoming: str, files: dict[str, bytes]) -> None:
    sftp.mkdir(incoming, 0o755)
    try:
        for name in CANDIDATE_NAMES:
            target = f"{incoming}/{name}"
            with sftp.open(target, "x+") as handle:
                handle.write(files[name])
                handle.flush()
            sftp.chmod(target, 0o644)
    except BaseException:
        _remove_candidate(sftp, incoming)
        raise


def _remove_candidate(sftp, incoming: str) -> None:
    for name in CANDIDATE_NAMES:
        try:
            sftp.remove(f"{incoming}/{name}")
        except OSError:
            pass
    try:
        sftp.rmdir(incoming)
    except OSError:
        pass


def _activate(client, config: dict) -> dict:
    stdin, stdout, stderr = client.exec_command("python3 -", timeout=240)
    stdin.write(_remote_program(config))
    stdin.channel.shutdown_write()
    payload = stdout.read(MAX_RESULT_BYTES + 1)
    error = stderr.read(4097)
    status = stdout.channel.recv_exit_status()
    if error or len(payload) > MAX_RESULT_BYTES:
        raise RuntimeError("remote_activation_invalid")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("remote_activation_invalid") from None
    if not isinstance(result, dict) or result.get("status") not in {
        "passed",
        "rolled_back",
        "failed",
    }:
        raise RuntimeError("remote_activation_invalid")
    if (status == 0) != (result["status"] == "passed"):
        raise RuntimeError("remote_activation_status_mismatch")
    return result


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _receipt(args, candidate: dict, result: dict) -> dict:
    return {
        "schema_version": 2,
        "status": result.get("status", "failed"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_identity": {
            "domain_hash": EXPECTED_DOMAIN_HASH,
            "ssh_host_hash": EXPECTED_HOST_HASH,
        },
        "source_sha": args.expected_source_sha,
        "version": args.expected_version,
        "projection_version": args.expected_projection,
        "release_name": candidate["release_name"],
        "manifest_sha256": candidate["manifest_sha256"],
        "candidate_receipt_sha256": candidate["receipt_sha256"],
        "inventory": candidate["inventory"],
        "production": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-projection", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    for value in (args.start, args.end):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise SystemExit("usage_date_invalid") from None
    try:
        candidate = _build_candidate(
            args.repo,
            expected_version=args.expected_version,
            expected_projection=args.expected_projection,
            expected_source_sha=args.expected_source_sha,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None

    incoming_name = ".incoming-" + uuid.uuid4().hex
    incoming = f"{REMOTE_ROOT}/releases/{incoming_name}"
    client = connect_operator(args.credential_file)
    sftp = client.open_sftp()
    result: dict = {"status": "failed", "stage": "upload", "reason": "upload_failed"}
    try:
        _upload_candidate(sftp, incoming, candidate["files"])
        result = _activate(
            client,
            {
                "root": REMOTE_ROOT,
                "incoming_name": incoming_name,
                "release_name": candidate["release_name"],
                "expected_manifest_sha": candidate["manifest_sha256"],
                "expected_receipt_sha": candidate["receipt_sha256"],
                "expected_version": args.expected_version,
                "expected_projection": args.expected_projection,
                "start": args.start,
                "end": args.end,
            },
        )
    except Exception as error:
        reason = str(error)
        if re.fullmatch(r"[a-z0-9_]+", reason) is None:
            reason = type(error).__name__.lower()
        result = {"status": "failed", "stage": "operator", "reason": reason}
    finally:
        if result.get("status") != "passed":
            _remove_candidate(sftp, incoming)
        sftp.close()
        client.close()

    receipt = _receipt(args, candidate, result)
    _write(args.receipt.resolve(), receipt)
    if result.get("status") != "passed":
        raise SystemExit("production_usage_deploy_failed")
    print(json.dumps({"status": "passed", "receipt": str(args.receipt)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
