#!/usr/bin/env python3
"""Smoke a real target-environment v0.2.2 Web deploy and rollback.

This wrapper intentionally requires an explicit SSH target and
--confirm-target-environment. It uploads the reviewed Web Linux service package
and release scripts, installs v0.2.2, runs the target release checker, restores
the pre-existing current pointer, restarts the service, and writes a redacted
JSON artifact.

The artifact must never persist raw SSH hosts, user names, key paths, URLs,
command lines, stdout, stderr, passwords, tokens, or local absolute paths.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"
DEFAULT_INSTALLER = ROOT / "scripts" / "install-ecorex-web.sh"
DEFAULT_CHECKER = ROOT / "scripts" / "check-ecorex-web-release.sh"
DEFAULT_ARTIFACT = ROOT / "docs" / "v0.2.2" / "artifacts" / "release-target-deploy-rollback-smoke.json"
DEFAULT_TEMPLATE_ARTIFACT = ROOT / "docs" / "v0.2.2" / "artifacts" / "release-target-command-template.json"
EXPECTED_VERSION = "0.2.2"
ROLLBACK_VERSION = "0.2.1"
EXPECTED_SHA256 = "3BEA1EF91C61E9E42235AE7695DDAEBEF25B4A6C5B13B6726240539CC937CCF7"
SECRET_TOKEN_PREFIXES = ("sk-", "xox", "ghp_", "github_pat_")
ALLOWED_BLOCKED_REASONS = {
    "missing-target-confirmation",
    "missing-ssh-host",
    "missing-local-file",
    "package-sha256-mismatch",
    "pre-state-version-not-rollback-target",
    "pre-state-current-missing",
    "deploy-state-version-mismatch",
    "deploy-service-inactive",
    "rollback-state-version-mismatch",
    "rollback-service-inactive",
    "public-http-probe-failed",
    "candidate-retention-unproven",
    "command-failed",
    "command-timeout",
    "command-start-failed",
    "target-state-unparseable",
    "target-smoke-failed",
}


class TargetSmokeError(RuntimeError):
    """Raised when target deploy/rollback evidence cannot be trusted."""


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return f"sha256:{_hash_text(str(path.resolve()))[:16]}"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest().upper()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_hash(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _hash_text(str(value))[:16]


def _redacted_url(url: str) -> dict[str, Any]:
    if not url:
        return {"configured": False}
    parsed = urllib.parse.urlparse(url)
    return {
        "configured": True,
        "scheme": parsed.scheme,
        "hostHash": _safe_hash(parsed.hostname or ""),
        "portConfigured": parsed.port is not None,
        "pathHash": _safe_hash(parsed.path.rstrip("/") or "/"),
    }


def _assert_no_secret_text(value: str, label: str) -> None:
    lowered = value.lower()
    if any(prefix.lower() in lowered for prefix in SECRET_TOKEN_PREFIXES):
        raise TargetSmokeError(f"secret-shaped text detected in {label}")


def _safe_failure_reason(exc: Exception) -> tuple[str, str]:
    raw = str(exc)
    if "without --confirm-target-environment" in raw:
        reason = "missing-target-confirmation"
    elif "--ssh-host is required" in raw:
        reason = "missing-ssh-host"
    elif "missing:" in raw:
        reason = "missing-local-file"
    elif "package SHA256 mismatch" in raw:
        reason = "package-sha256-mismatch"
    elif "pre-state version must be" in raw:
        reason = "pre-state-version-not-rollback-target"
    elif "pre-state current path is missing" in raw:
        reason = "pre-state-current-missing"
    elif "deploy state version mismatch" in raw:
        reason = "deploy-state-version-mismatch"
    elif "service is not active after deploy" in raw:
        reason = "deploy-service-inactive"
    elif "rollback state version mismatch" in raw:
        reason = "rollback-state-version-mismatch"
    elif "service is not active after rollback" in raw:
        reason = "rollback-service-inactive"
    elif "public HTTP probe did not pass" in raw:
        reason = "public-http-probe-failed"
    elif "deployed candidate retention could not be proven" in raw:
        reason = "candidate-retention-unproven"
    elif "timed out" in raw:
        reason = "command-timeout"
    elif "could not start" in raw:
        reason = "command-start-failed"
    elif "failed with exit code" in raw:
        reason = "command-failed"
    elif "parseable target state" in raw:
        reason = "target-state-unparseable"
    else:
        reason = "target-smoke-failed"
    return reason, _hash_text(raw)[:16]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    _assert_no_secret_text(serialized, "target smoke artifact")
    path.write_text(serialized + "\n", encoding="utf-8")


def _output_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _load_local_package_smoke() -> Any:
    script = ROOT / "scripts" / "smoke-v022-release-deploy-rollback.py"
    spec = importlib.util.spec_from_file_location("ecorex_local_deploy_smoke", script)
    if spec is None or spec.loader is None:
        raise TargetSmokeError(f"cannot load package smoke helper: {_repo_relative(script)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_package(package: Path, expected_version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    helper = _load_local_package_smoke()
    result = helper.run_smoke(
        argparse.Namespace(
            package=str(package),
            expected_version=expected_version,
            artifact="",
        )
    )
    return result["artifact"], result["packageChecks"]


def _ssh_target(args: argparse.Namespace) -> str:
    return f"{args.ssh_user}@{args.ssh_host}" if args.ssh_user else args.ssh_host


def _ssh_base(args: argparse.Namespace) -> list[str]:
    base = [
        "ssh",
        "-p",
        str(args.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.connect_timeout_seconds}",
    ]
    if args.ssh_identity:
        base.extend(["-i", args.ssh_identity])
    return base


def _scp_base(args: argparse.Namespace) -> list[str]:
    base = [
        "scp",
        "-P",
        str(args.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.connect_timeout_seconds}",
    ]
    if args.ssh_identity:
        base.extend(["-i", args.ssh_identity])
    return base


def _remote_path(args: argparse.Namespace, name: str) -> str:
    return f"{args.remote_dir.rstrip('/')}/{name}"


def _sanitize_result(name: str, argv: list[str], completed: subprocess.CompletedProcess[str], started: float) -> dict[str, Any]:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return {
        "name": name,
        "argvHash": _hash_text(json.dumps(argv, ensure_ascii=False))[:16],
        "exitCode": completed.returncode,
        "stdoutHash": _hash_text(stdout)[:16],
        "stderrHash": _hash_text(stderr)[:16],
        "stdoutLineCount": len(stdout.splitlines()),
        "stderrLineCount": len(stderr.splitlines()),
        "durationMs": int((time.time() - started) * 1000),
    }


def _run_command(
    name: str,
    argv: list[str],
    commands: list[dict[str, Any]],
    timeout_seconds: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.time()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        commands.append(
            {
                "name": name,
                "argvHash": _hash_text(json.dumps(argv, ensure_ascii=False))[:16],
                "exitCode": -1,
                "stdoutHash": _hash_text(stdout)[:16],
                "stderrHash": _hash_text(stderr)[:16],
                "stdoutLineCount": len(str(stdout).splitlines()),
                "stderrLineCount": len(str(stderr).splitlines()),
                "durationMs": int((time.time() - started) * 1000),
                "timedOut": True,
            }
        )
        raise TargetSmokeError(f"{name} timed out") from None
    except OSError as exc:
        commands.append(
            {
                "name": name,
                "argvHash": _hash_text(json.dumps(argv, ensure_ascii=False))[:16],
                "exitCode": -1,
                "stdoutHash": "",
                "stderrHash": _hash_text(exc.__class__.__name__)[:16],
                "stdoutLineCount": 0,
                "stderrLineCount": 0,
                "durationMs": int((time.time() - started) * 1000),
                "startFailed": True,
            }
        )
        raise TargetSmokeError(f"{name} could not start") from None
    commands.append(_sanitize_result(name, argv, completed, started))
    if check and completed.returncode != 0:
        raise TargetSmokeError(f"{name} failed with exit code {completed.returncode}")
    return completed


def _ssh(args: argparse.Namespace, name: str, remote_command: str, commands: list[dict[str, Any]], check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = [*_ssh_base(args), _ssh_target(args), remote_command]
    return _run_command(name, argv, commands, args.command_timeout_seconds, check=check)


def _scp(args: argparse.Namespace, name: str, source: Path, destination: str, commands: list[dict[str, Any]]) -> None:
    argv = [*_scp_base(args), str(source), f"{_ssh_target(args)}:{destination}"]
    _run_command(name, argv, commands, args.command_timeout_seconds)


def _sudo(args: argparse.Namespace, command: str) -> str:
    if not args.sudo_command:
        return command
    return f"{args.sudo_command} {command}"


def _env_command(env: dict[str, str], command: str) -> str:
    pairs = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items() if value != "")
    return f"env {pairs} {command}" if pairs else command


def _state_command(args: argparse.Namespace) -> str:
    script = r'''
import hashlib
import json
import os
import pathlib
import subprocess

def h(value):
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest().upper()[:16]

install_root = pathlib.Path(os.environ["INSTALL_ROOT"])
workspace_root = pathlib.Path(os.environ["WORKSPACE_ROOT"])
service_name = os.environ["SERVICE_NAME"]
current_link = install_root / "current"
current_path = ""
current_version = ""
release_id = ""
if current_link.exists():
    try:
        current_path = str(current_link.resolve())
        release_id = pathlib.Path(current_path).name
        release_json = pathlib.Path(current_path) / "release.json"
        if release_json.is_file():
            payload = json.loads(release_json.read_text(encoding="utf-8-sig"))
            current_version = str(payload.get("version") or "")
    except Exception:
        current_path = ""

manifest_version = ""
manifest_release = ""
manifest = workspace_root / ".ecorex" / "installations.json"
if manifest.is_file():
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        surface = (payload.get("surfaces") or {}).get("webui-linux-service") or {}
        manifest_version = str(surface.get("version") or "")
        manifest_release = str(surface.get("releaseDir") or "")
    except Exception:
        pass

active = subprocess.run(["systemctl", "is-active", f"{service_name}.service"], text=True, capture_output=True)
enabled = subprocess.run(["systemctl", "is-enabled", f"{service_name}.service"], text=True, capture_output=True)

print(json.dumps({
    "currentPath": current_path,
    "currentPathHash": h(current_path),
    "releaseIdHash": h(release_id),
    "currentVersion": current_version,
    "installationManifestVersion": manifest_version,
    "installationManifestReleaseHash": h(manifest_release),
    "serviceActive": active.stdout.strip() == "active",
    "serviceEnabled": enabled.stdout.strip() == "enabled",
}, sort_keys=True))
'''
    env = {
        "INSTALL_ROOT": args.install_root,
        "WORKSPACE_ROOT": args.workspace_root,
        "SERVICE_NAME": args.service_name,
    }
    return _env_command(env, f"python3 - <<'PY'\n{script}\nPY")


def _parse_state(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        payload = json.loads((completed.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        raise TargetSmokeError(f"{label} did not return parseable target state") from exc
    if not isinstance(payload, dict):
        raise TargetSmokeError(f"{label} target state must be an object")
    return payload


def _public_http_probe(base_url: str, timeout_seconds: int) -> dict[str, Any]:
    if not base_url:
        return {"configured": False, "checked": False}
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/app/"
    started = time.time()
    status = 0
    ok = False
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            status = int(response.status)
            ok = status < 500
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        ok = status < 500
    except Exception:
        ok = False
    return {
        "configured": True,
        "checked": True,
        "ok": ok,
        "status": status,
        "durationMs": int((time.time() - started) * 1000),
        "url": _redacted_url(url),
    }


def _rollback_command(args: argparse.Namespace, previous_current: str, deployed_current: str) -> str:
    script = r'''
set -euo pipefail
previous_current="$1"
deployed_current="$2"
install_root="$3"
service_name="$4"
if [ -z "$previous_current" ] || [ ! -d "$previous_current" ]; then
  echo "previous current release is missing" >&2
  exit 1
fi
ln -sfn "$previous_current" "$install_root/current"
systemctl restart "$service_name.service"
if [ ! -d "$deployed_current" ]; then
  echo "deployed candidate was not retained" >&2
  exit 1
fi
'''
    return _sudo(
        args,
        "bash -s -- "
        + " ".join(
            shlex.quote(item)
            for item in (previous_current, deployed_current, args.install_root, args.service_name)
        )
        + f" <<'SH'\n{script}\nSH",
    )


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_target_environment:
        raise TargetSmokeError("refusing to run without --confirm-target-environment")
    if not args.ssh_host:
        raise TargetSmokeError("--ssh-host is required")

    package = Path(args.package)
    if not package.is_absolute():
        package = ROOT / package
    package = package.resolve()
    installer = Path(args.installer)
    checker = Path(args.checker)
    if not installer.is_absolute():
        installer = ROOT / installer
    if not checker.is_absolute():
        checker = ROOT / checker
    installer = installer.resolve()
    checker = checker.resolve()
    for path, label in ((package, "package"), (installer, "installer"), (checker, "checker")):
        if not path.is_file():
            raise TargetSmokeError(f"{label} missing: {_repo_relative(path)}")

    package_sha = _hash_file(package)
    if package_sha != args.expected_sha256.upper():
        raise TargetSmokeError(f"package SHA256 mismatch: {package_sha}")
    artifact_evidence, package_checks = _validate_package(package, args.expected_version)

    commands: list[dict[str, Any]] = []
    remote_package = _remote_path(args, package.name)
    remote_installer = _remote_path(args, "install-ecorex-web.sh")
    remote_checker = _remote_path(args, "check-ecorex-web-release.sh")

    _ssh(args, "prepare_remote_dir", f"mkdir -p {shlex.quote(args.remote_dir)}", commands)
    _scp(args, "upload_package", package, remote_package, commands)
    _scp(args, "upload_installer", installer, remote_installer, commands)
    _scp(args, "upload_checker", checker, remote_checker, commands)
    _ssh(args, "chmod_release_scripts", f"chmod 700 {shlex.quote(remote_installer)} {shlex.quote(remote_checker)}", commands)

    pre_state = _parse_state(_ssh(args, "capture_pre_state", _state_command(args), commands), "capture_pre_state")
    if pre_state.get("currentVersion") != args.rollback_version:
        raise TargetSmokeError(
            f"pre-state version must be {args.rollback_version} before rollback smoke, got {pre_state.get('currentVersion')!r}"
        )
    previous_current = str(pre_state.get("currentPath") or "")
    if not previous_current:
        raise TargetSmokeError("pre-state current path is missing")

    install_env = {
        "VERSION": args.expected_version,
        "EXPECTED_SHA256": args.expected_sha256.upper(),
        "OPEN_BROWSER": "0",
        "START_SERVICE": "1",
        "INSTALL_PY_DEPS": "1" if args.install_py_deps else "0",
        "SERVICE_NAME": args.service_name,
        "INSTALL_ROOT": args.install_root,
        "WORKSPACE_ROOT": args.workspace_root,
        "WEB_PORT": str(args.web_port),
        "PUBLIC_BASE_URL": args.public_base_url or "",
    }
    install_command = _sudo(args, _env_command(install_env, f"bash {shlex.quote(remote_installer)} {shlex.quote(remote_package)}"))
    _ssh(args, "install_v022", install_command, commands, check=True)

    check_env = {
        "VERSION": args.expected_version,
        "SERVICE_NAME": args.service_name,
        "INSTALL_ROOT": args.install_root,
        "WORKSPACE_ROOT": args.workspace_root,
        "WEB_PORT": str(args.web_port),
        "BASE_URL": args.remote_base_url or f"http://127.0.0.1:{args.web_port}",
        "CHECK_HTTP": "1",
        "CHECK_SYSTEMD": "1",
        "CHECK_INSTALLED": "1",
    }
    check_command = _sudo(args, _env_command(check_env, f"bash {shlex.quote(remote_checker)}"))
    _ssh(args, "check_deploy", check_command, commands, check=True)
    deploy_state = _parse_state(_ssh(args, "capture_deploy_state", _state_command(args), commands), "capture_deploy_state")
    if deploy_state.get("currentVersion") != args.expected_version:
        raise TargetSmokeError(f"deploy state version mismatch: {deploy_state.get('currentVersion')!r}")
    if deploy_state.get("serviceActive") is not True:
        raise TargetSmokeError("service is not active after deploy")

    deployed_current = str(deploy_state.get("currentPath") or "")
    _ssh(args, "rollback_to_previous", _rollback_command(args, previous_current, deployed_current), commands, check=True)
    rollback_state = _parse_state(_ssh(args, "capture_rollback_state", _state_command(args), commands), "capture_rollback_state")
    if rollback_state.get("currentVersion") != args.rollback_version:
        raise TargetSmokeError(f"rollback state version mismatch: {rollback_state.get('currentVersion')!r}")
    if rollback_state.get("serviceActive") is not True:
        raise TargetSmokeError("service is not active after rollback")

    public_probe = _public_http_probe(args.public_base_url, args.http_timeout_seconds)
    if args.public_base_url and public_probe.get("ok") is not True:
        raise TargetSmokeError("public HTTP probe did not pass after rollback")

    candidate_retained = bool(deployed_current and deploy_state.get("currentPathHash") != rollback_state.get("currentPathHash"))
    if not candidate_retained:
        raise TargetSmokeError("deployed candidate retention could not be proven")

    return {
        "status": "PASS",
        "scope": "target-environment-web-linux-service",
        "productionEnvironment": True,
        "requiresRoot": bool(args.sudo_command),
        "requiresSystemd": True,
        "requiresNetwork": True,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "artifact": artifact_evidence,
        "packageChecks": package_checks,
        "target": {
            "sshHostHash": _safe_hash(args.ssh_host),
            "sshUserHash": _safe_hash(args.ssh_user),
            "sshPortHash": _safe_hash(args.ssh_port),
            "publicBaseUrl": _redacted_url(args.public_base_url),
            "remoteBaseUrl": _redacted_url(args.remote_base_url or f"http://127.0.0.1:{args.web_port}"),
            "serviceNameHash": _safe_hash(args.service_name),
            "installRootHash": _safe_hash(args.install_root),
            "workspaceRootHash": _safe_hash(args.workspace_root),
            "rawTargetPersisted": False,
        },
        "preState": {
            "currentPathHash": pre_state.get("currentPathHash"),
            "releaseIdHash": pre_state.get("releaseIdHash"),
            "currentVersion": pre_state.get("currentVersion"),
            "installationManifestVersion": pre_state.get("installationManifestVersion"),
            "serviceActive": pre_state.get("serviceActive"),
            "serviceEnabled": pre_state.get("serviceEnabled"),
        },
        "deploy": {
            "verified": True,
            "deployedReleaseIdHash": deploy_state.get("releaseIdHash"),
            "currentVersionAfterDeploy": deploy_state.get("currentVersion"),
            "installationManifestVersion": deploy_state.get("installationManifestVersion"),
            "serviceActiveAfterDeploy": deploy_state.get("serviceActive"),
            "serviceEnabledAfterDeploy": deploy_state.get("serviceEnabled"),
            "targetCheckCommandPassed": True,
        },
        "rollback": {
            "verified": True,
            "rollbackReleaseId": f"target-hash:{pre_state.get('releaseIdHash')}",
            "currentVersionAfterRollback": rollback_state.get("currentVersion"),
            "installationManifestVersion": rollback_state.get("installationManifestVersion"),
            "serviceActiveAfterRollback": rollback_state.get("serviceActive"),
            "serviceEnabledAfterRollback": rollback_state.get("serviceEnabled"),
            "candidateRetainedForAudit": candidate_retained,
        },
        "pointerMethod": "target-current-symlink",
        "publicHttpProbe": public_probe,
        "commands": commands,
        "redaction": {
            "rawTargetPersisted": False,
            "rawCommandsPersisted": False,
            "rawStdoutPersisted": False,
            "rawStderrPersisted": False,
            "rawSecretsPersisted": False,
        },
        "notes": [
            "Target-environment smoke uses explicit SSH/scp and redacts host, user, URL, command, stdout, and stderr values.",
            "This PASS means the target current pointer was moved to v0.2.2, checked, rolled back to the pre-existing v0.2.1 release, and the v0.2.2 candidate remained retained for audit.",
        ],
    }


def _blocked_artifact(args: argparse.Namespace, reason: str, reason_hash: str = "") -> dict[str, Any]:
    if reason not in ALLOWED_BLOCKED_REASONS:
        reason_hash = reason_hash or _hash_text(reason)[:16]
        reason = "target-smoke-failed"
    package = Path(args.package)
    if not package.is_absolute():
        package = ROOT / package
    artifact: dict[str, Any] = {
        "path": _repo_relative(package),
        "version": args.expected_version,
        "artifactId": "web-linux-service",
    }
    if package.is_file():
        artifact.update({"size": package.stat().st_size, "sha256": _hash_file(package)})
    return {
        "status": "BLOCKED",
        "scope": "target-environment-web-linux-service",
        "productionEnvironment": True,
        "requiresRoot": True,
        "requiresSystemd": True,
        "requiresNetwork": True,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "reasonHash": reason_hash,
        "artifact": artifact,
        "target": {
            "sshHostHash": _safe_hash(args.ssh_host),
            "sshUserHash": _safe_hash(args.ssh_user),
            "publicBaseUrl": _redacted_url(args.public_base_url),
            "rawTargetPersisted": False,
        },
        "redaction": {
            "rawTargetPersisted": False,
            "rawCommandsPersisted": False,
            "rawStdoutPersisted": False,
            "rawStderrPersisted": False,
            "rawSecretsPersisted": False,
        },
    }


def _resolve_local_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _target_command_template(args: argparse.Namespace) -> dict[str, Any]:
    package = _resolve_local_path(args.package)
    installer = _resolve_local_path(args.installer)
    checker = _resolve_local_path(args.checker)

    local_inputs: list[dict[str, Any]] = []
    for label, path in (("package", package), ("installer", installer), ("checker", checker)):
        row: dict[str, Any] = {
            "name": label,
            "path": _repo_relative(path),
            "exists": path.is_file(),
        }
        if path.is_file():
            row["size"] = path.stat().st_size
            row["sha256"] = _hash_file(path)
        local_inputs.append(row)

    package_row = local_inputs[0]
    package_hash = str(package_row.get("sha256") or "")
    local_ready = all(row["exists"] for row in local_inputs) and package_hash == args.expected_sha256.upper()

    command = (
        "python scripts/smoke-v022-release-target-deploy-rollback.py "
        "--package release-artifacts/EcoreX_0.2.2-web-linux-service.tar.gz "
        "--ssh-host <target-host> "
        "--ssh-user <target-user> "
        "--ssh-identity <path-to-private-key> "
        "--public-base-url <https://target.example> "
        "--confirm-target-environment "
        "--artifact docs/v0.2.2/artifacts/release-target-deploy-rollback-smoke.json"
    )
    env_notes = [
        "The target must already have a v0.2.1 current release so rollback can be verified.",
        "The operator running this command must have passwordless sudo or an equivalent sudo command configured.",
        "Do not paste private keys, passwords, API tokens, raw hostnames, or user names into tracked docs or artifacts.",
    ]
    payload = {
        "status": "READY_FOR_TARGET_INPUT" if local_ready else "LOCAL_ARTIFACTS_INCOMPLETE",
        "scope": "target-environment-web-linux-service",
        "targetExecution": False,
        "networkUsed": False,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "artifact": {
            "path": _repo_relative(package),
            "version": args.expected_version,
            "artifactId": "web-linux-service",
            "size": package_row.get("size"),
            "sha256": package_hash,
            "expectedSha256": args.expected_sha256.upper(),
            "sha256MatchesExpected": package_hash == args.expected_sha256.upper(),
        },
        "localInputs": local_inputs,
        "requiredTargetInputs": [
            "--ssh-host <target-host>",
            "--confirm-target-environment",
        ],
        "optionalTargetInputs": [
            "--ssh-user <target-user>",
            "--ssh-port <port>",
            "--ssh-identity <path-to-private-key>",
            "--public-base-url <https://target.example>",
            "--remote-base-url <http://127.0.0.1:9909>",
            "--sudo-command <sudo -n>",
        ],
        "commandTemplate": command,
        "templateArtifact": {
            "path": _repo_relative(_output_path(args.template_artifact)),
            "writesPassEvidence": False,
            "clearsTargetBlocker": False,
        },
        "redaction": {
            "rawTargetPersisted": False,
            "rawCommandsPersisted": False,
            "rawStdoutPersisted": False,
            "rawStderrPersisted": False,
            "rawSecretsPersisted": False,
        },
        "notes": env_notes,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    _assert_no_secret_text(serialized, "target command template")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE.relative_to(ROOT)))
    parser.add_argument("--installer", default=str(DEFAULT_INSTALLER.relative_to(ROOT)))
    parser.add_argument("--checker", default=str(DEFAULT_CHECKER.relative_to(ROOT)))
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT.relative_to(ROOT)))
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--rollback-version", default=ROLLBACK_VERSION)
    parser.add_argument("--expected-sha256", default=EXPECTED_SHA256)
    parser.add_argument("--ssh-host", default="")
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-identity", default="")
    parser.add_argument("--remote-dir", default="/tmp/ecorex-v022-target-smoke")
    parser.add_argument("--sudo-command", default="sudo -n")
    parser.add_argument("--service-name", default="ecorex-web")
    parser.add_argument("--install-root", default="/opt/ecorex-web")
    parser.add_argument("--workspace-root", default="/srv/ecorex-agent-workspace")
    parser.add_argument("--web-port", type=int, default=9909)
    parser.add_argument("--remote-base-url", default="")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--install-py-deps", action="store_true", default=False)
    parser.add_argument("--connect-timeout-seconds", type=int, default=15)
    parser.add_argument("--command-timeout-seconds", type=int, default=900)
    parser.add_argument("--http-timeout-seconds", type=int, default=20)
    parser.add_argument("--confirm-target-environment", action="store_true")
    parser.add_argument(
        "--write-blocked-artifact",
        action="store_true",
        help="Write a redacted BLOCKED artifact when target configuration is absent or the smoke fails.",
    )
    parser.add_argument(
        "--print-command-template",
        action="store_true",
        help="Print a redacted target-smoke command template and local artifact readiness without network access.",
    )
    parser.add_argument(
        "--template-artifact",
        default=str(DEFAULT_TEMPLATE_ARTIFACT.relative_to(ROOT)),
        help="Optional JSON path written only with --print-command-template; it never records target execution evidence.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact_path = Path(args.artifact)
    if not artifact_path.is_absolute():
        artifact_path = ROOT / artifact_path

    if args.print_command_template:
        template = _target_command_template(args)
        if args.template_artifact:
            _write_json(_output_path(args.template_artifact), template)
        print(json.dumps(template, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if template["status"] == "READY_FOR_TARGET_INPUT" else 1

    try:
        result = run_smoke(args)
    except Exception as exc:
        reason, reason_hash = _safe_failure_reason(exc)
        if args.write_blocked_artifact:
            _write_json(artifact_path, _blocked_artifact(args, reason, reason_hash))
        print(
            json.dumps(
                {"status": "BLOCKED", "error": reason, "errorHash": reason_hash},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    _write_json(artifact_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
