#!/usr/bin/env python3
"""Operator-driven, resumable EcoreX release pipeline.

GitHub Actions may only build and smoke immutable evidence.  This workstation
owns every publication/deployment mutation, and the stable update pointer is
changed only after an interactive final confirmation.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PublicKey,
)

from ecorex.release import DigestPinnedExternalSigner  # noqa: E402
from ecorex.release.github import (  # noqa: E402
    EnvironmentGitHubCredential,
    GitHubReleaseDraft,
    GitHubReleasePublisher,
)
from ecorex.release.manual import (  # noqa: E402
    ALL_STEPS,
    FINALIZE_STEPS,
    ManualReleaseError,
    PREPARE_STEPS,
    ReleaseRunStore,
    ReleaseSpec,
    browser_request,
    canonical_json,
    confirmation_phrase,
    sha256_file,
    validate_codex_browser_receipt,
)
from ecorex.update import ReleaseChannel  # noqa: E402


RUN_ROOT = ROOT / ".candidate" / "release-runs"
SOURCE_REPOSITORY = "zyfjacksonchen-source/EcoreX"
INSTALLER_OWNER = "zyfjacksonchen-source"
INSTALLER_REPOSITORY = "EcoreX-installers"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
PUBLIC_URL = "https://dl.ecoremedia.net/ecorex-agent/"
DEFAULT_CREDENTIAL_FILE = Path("/Users/mac/Downloads/企业服务器地址 (1).txt")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_LOCAL_GATE_DOMAIN = b"ecorex.manual-release.local-gate.v1\0"
_WORKFLOW_TIMEOUT_SECONDS = 5 * 60 * 60
_READ_ONLY_BUILD_WORKFLOWS = frozenset(
    {
        "ecorex-v1-ci.yml",
        "ecorex-v1-platform-stage.yml",
        "ecorex-v1-candidate.yml",
        "emate-v030-macos-universal.yml",
        "ecorex-v1-online-update.yml",
    }
)


class ReleaseCommandError(ManualReleaseError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_file(path: Path, *, maximum: int = 16 * 1024 * 1024) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        if not 1 <= len(payload) <= maximum:
            raise OSError
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReleaseCommandError("release_input_invalid") from None
    if not isinstance(value, dict):
        raise ReleaseCommandError("release_input_invalid")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = canonical_json(dict(value)) + b"\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git(*arguments: str, cwd: Path = ROOT) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseCommandError("release_git_unavailable") from None
    if result.returncode != 0:
        raise ReleaseCommandError("release_git_identity_invalid")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ReleaseCommandError("release_git_identity_invalid") from None


def _git_bytes(commit: str, path: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", "show", f"{commit}:{path}"),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseCommandError("release_source_unavailable") from None
    if result.returncode != 0 or not result.stdout:
        raise ReleaseCommandError("release_source_unavailable")
    return result.stdout


def _tool_version(
    command: Sequence[str], *, code: str, environment: Mapping[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            tuple(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(environment) if environment is not None else None,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseCommandError(code) from None
    try:
        value = result.stdout.decode("utf-8").strip().splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        raise ReleaseCommandError(code) from None
    if result.returncode != 0 or not value or len(value) > 256:
        raise ReleaseCommandError(code)
    return value


def _python_311() -> Path:
    configured = os.environ.get("ECOREX_PYTHON_311")
    candidates = [configured] if configured else []
    candidates.extend(filter(None, (shutil.which("python3.11"), shutil.which("python"))))
    for raw in candidates:
        assert raw is not None
        path = Path(os.path.abspath(Path(raw).expanduser()))
        if _tool_version(
            (str(path), "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"),
            code="release_python_3119_required",
        ) == "3.11.9":
            return path
    raise ReleaseCommandError("release_python_3119_required")


def _node_tools() -> tuple[Path, Path]:
    node = os.environ.get("ECOREX_NODE") or shutil.which("node")
    npm = os.environ.get("ECOREX_NPM") or shutil.which("npm")
    if not node or not npm:
        raise ReleaseCommandError("release_node_npm_required")
    node_path = Path(node).expanduser().resolve()
    npm_path = Path(npm).expanduser().resolve()
    node_version = _tool_version((str(node_path), "--version"), code="release_node_version_invalid")
    if node_version != "v22.23.1":
        raise ReleaseCommandError("release_node_version_invalid")
    tool_environment = dict(os.environ)
    tool_environment["PATH"] = (
        str(node_path.parent) + os.pathsep + tool_environment.get("PATH", "")
    )
    _tool_version(
        (str(npm_path), "--version"),
        code="release_npm_unavailable",
        environment=tool_environment,
    )
    return node_path, npm_path


def _proxy() -> str | None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if isinstance(value, str) and value:
            return value
    try:
        with socket.create_connection(("127.0.0.1", 7993), timeout=0.2):
            return "http://127.0.0.1:7993"
    except OSError:
        return None


def _base_environment(*, node: Path | None = None) -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TMPDIR",
        "USERPROFILE",
    }
    environment = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    if node is not None:
        environment["PATH"] = str(node.parent) + os.pathsep + environment.get("PATH", "")
    proxy = _proxy()
    if proxy:
        environment.update(
            {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy}
        )
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment["PYTHONHASHSEED"] = "0"
    return environment


def _operator_environment() -> dict[str, str]:
    environment = _base_environment()
    prefixes = (
        "ECOREX_RELEASE_",
        "ECOREX_PUBLICATION_",
        "ECOREX_DEPLOYMENT_",
        "ECOREX_BOOTSTRAP_",
        "ACTIONS_ID_TOKEN_",
        "AWS_",
        "AZURE_",
        "GOOGLE_",
    )
    for name, value in os.environ.items():
        if name.startswith(prefixes) and value and "\0" not in value:
            environment[name] = value
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log: Path,
    timeout: int = 60 * 60,
) -> dict[str, Any]:
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    try:
        with log.open("xb") as output:
            os.chmod(log, 0o600)
            result = subprocess.run(
                tuple(str(item) for item in command),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
    except FileExistsError:
        raise ReleaseCommandError("release_command_log_conflict") from None
    except (OSError, subprocess.SubprocessError):
        raise ReleaseCommandError("release_command_failed") from None
    receipt = {
        "argv_sha256": hashlib.sha256(canonical_json(list(command))).hexdigest(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "exit_code": result.returncode,
        "log_sha256": sha256_file(log),
    }
    if result.returncode != 0:
        raise ReleaseCommandError("release_command_failed")
    return receipt


def _run_json(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log: Path,
    timeout: int = 60 * 60,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = _run(
        command, cwd=cwd, environment=environment, log=log, timeout=timeout
    )
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
        value = json.loads(lines[-1])
    except (OSError, UnicodeError, IndexError, json.JSONDecodeError):
        raise ReleaseCommandError("release_command_receipt_invalid") from None
    if not isinstance(value, dict) or value.get("ok") is False:
        raise ReleaseCommandError("release_command_receipt_invalid")
    return value, receipt


class GitHubActions:
    """Read/build-only Actions transport with no Release mutation API."""

    def __init__(self) -> None:
        self.token = EnvironmentGitHubCredential().bearer_token()
        proxy = _proxy()
        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=20, read=180, write=60, pool=20),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy,
        )

    def close(self) -> None:
        self.token = ""
        self.client.close()

    def __enter__(self) -> "GitHubActions":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "ecorex-manual-release-v1",
        }

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        accepted: frozenset[int] = frozenset({200}),
    ) -> Any:
        try:
            response = self.client.request(
                method,
                f"{GITHUB_API}/repos/{SOURCE_REPOSITORY}{path}",
                headers=self.headers,
                json=dict(payload) if payload is not None else None,
            )
        except httpx.HTTPError:
            raise ReleaseCommandError("github_actions_unavailable") from None
        if response.status_code not in accepted or response.is_redirect:
            raise ReleaseCommandError("github_actions_request_failed")
        if response.status_code == 204 or not response.content:
            return None
        try:
            value = response.json()
        except (ValueError, json.JSONDecodeError):
            raise ReleaseCommandError("github_actions_response_invalid") from None
        return value

    def dispatch(self, workflow: str, *, inputs: Mapping[str, str]) -> None:
        if workflow not in _READ_ONLY_BUILD_WORKFLOWS:
            raise ReleaseCommandError("github_workflow_invalid")
        self.json(
            "POST",
            f"/actions/workflows/{workflow}/dispatches",
            payload={"ref": "main", "inputs": dict(inputs)},
            accepted=frozenset({204}),
        )

    def find_run(self, workflow: str, *, title: str, commit: str) -> dict[str, Any] | None:
        if workflow not in _READ_ONLY_BUILD_WORKFLOWS:
            raise ReleaseCommandError("github_workflow_invalid")
        value = self.json(
            "GET",
            f"/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch=main&per_page=50",
        )
        runs = value.get("workflow_runs") if isinstance(value, dict) else None
        if not isinstance(runs, list):
            raise ReleaseCommandError("github_workflow_runs_invalid")
        matches = [
            item
            for item in runs
            if isinstance(item, dict)
            and item.get("head_sha") == commit
            and item.get("display_title") == title
            and item.get("event") == "workflow_dispatch"
            and item.get("head_branch") == "main"
        ]
        matches.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        return dict(matches[0]) if matches else None

    def ensure_run(
        self,
        workflow: str,
        *,
        title: str,
        commit: str,
        inputs: Mapping[str, str],
    ) -> dict[str, Any]:
        existing = self.find_run(workflow, title=title, commit=commit)
        if existing is None:
            self.dispatch(workflow, inputs=inputs)
        elif existing.get("status") == "completed" and existing.get("conclusion") != "success":
            run_id = existing.get("id")
            if isinstance(run_id, bool) or not isinstance(run_id, int):
                raise ReleaseCommandError("github_workflow_failed")
            self.json(
                "POST",
                f"/actions/runs/{run_id}/rerun-failed-jobs",
                accepted=frozenset({201}),
            )
        return self.wait_run(workflow, title=title, commit=commit)

    def wait_run(self, workflow: str, *, title: str, commit: str) -> dict[str, Any]:
        deadline = time.monotonic() + _WORKFLOW_TIMEOUT_SECONDS
        selected: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            selected = self.find_run(workflow, title=title, commit=commit)
            if selected and selected.get("status") == "completed":
                if selected.get("conclusion") != "success":
                    raise ReleaseCommandError("github_workflow_failed")
                return _workflow_receipt(selected, workflow=workflow, title=title)
            time.sleep(10)
        raise ReleaseCommandError(
            "github_workflow_not_found" if selected is None else "github_workflow_timeout"
        )

    def artifacts(self, run_id: int) -> list[dict[str, Any]]:
        value = self.json("GET", f"/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = value.get("artifacts") if isinstance(value, dict) else None
        if not isinstance(artifacts, list) or value.get("total_count") != len(artifacts):
            raise ReleaseCommandError("github_artifact_list_invalid")
        return [dict(item) for item in artifacts if isinstance(item, dict)]

    def download(self, run_id: int, name: str, output: Path) -> dict[str, Any]:
        matches = [item for item in self.artifacts(run_id) if item.get("name") == name]
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise ReleaseCommandError("github_artifact_missing")
        artifact = matches[0]
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
            raise ReleaseCommandError("github_artifact_invalid")
        expected = str(artifact.get("digest", ""))
        if output.exists() and not output.is_symlink():
            digest = sha256_file(output)
            if expected and expected != f"sha256:{digest}":
                raise ReleaseCommandError("github_artifact_digest_mismatch")
            return {
                "artifact_id": artifact_id,
                "name": name,
                "size_bytes": output.stat().st_size,
                "sha256": digest,
            }
        try:
            response = self.client.get(
                f"{GITHUB_API}/repos/{SOURCE_REPOSITORY}/actions/artifacts/{artifact_id}/zip",
                headers=self.headers,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            raise ReleaseCommandError("github_artifact_download_failed") from None
        if response.status_code != 200 or not response.content:
            raise ReleaseCommandError("github_artifact_download_failed")
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with output.open("xb") as stream:
                os.chmod(output, 0o600)
                stream.write(response.content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise ReleaseCommandError("github_artifact_output_invalid") from None
        digest = sha256_file(output)
        if expected and expected != f"sha256:{digest}":
            raise ReleaseCommandError("github_artifact_digest_mismatch")
        return {
            "artifact_id": artifact_id,
            "name": name,
            "size_bytes": output.stat().st_size,
            "sha256": digest,
        }


def _workflow_receipt(value: Mapping[str, Any], *, workflow: str, title: str) -> dict[str, Any]:
    required_ints = ("id", "run_attempt", "run_number")
    if any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int) for key in required_ints):
        raise ReleaseCommandError("github_workflow_receipt_invalid")
    return {
        "workflow": workflow,
        "title": title,
        "run_id": value["id"],
        "run_attempt": value["run_attempt"],
        "run_number": value["run_number"],
        "head_sha": value.get("head_sha"),
        "html_url": value.get("html_url"),
        "status": "passed",
    }


def _safe_extract(archive: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ReleaseCommandError("release_extract_target_exists")
    target.mkdir(parents=True, mode=0o700)
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if not 1 <= len(entries) <= 50_000:
                raise ReleaseCommandError("release_archive_invalid")
            for item in entries:
                pure = PurePosixPath(item.filename)
                mode = item.external_attr >> 16
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or stat.S_ISLNK(mode)
                    or not (item.is_dir() or stat.S_ISREG(mode) or mode == 0)
                ):
                    raise ReleaseCommandError("release_archive_invalid")
                destination = target.joinpath(*pure.parts)
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                total += item.file_size
                if total > 32 * 1024 * 1024 * 1024:
                    raise ReleaseCommandError("release_archive_too_large")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    except ReleaseCommandError:
        raise
    except (OSError, zipfile.BadZipFile):
        raise ReleaseCommandError("release_archive_invalid") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release-v1.py")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--commit", required=True)
    prepare.add_argument("--channel", choices=("stable", "canary"), default="stable")
    prepare.add_argument("--from-version", default="0.3.2")
    for name in ("status", "finalize", "verify-online", "rollback-notification"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name == "verify-online":
            command.add_argument("--browser-receipt", type=Path)
            command.add_argument("--evidence-root", type=Path)
    return parser


def _store(run_id: str) -> ReleaseRunStore:
    return ReleaseRunStore.open(RUN_ROOT, run_id)


def _public_status(store: ReleaseRunStore) -> dict[str, Any]:
    value = store.read()
    steps = {
        name: value["steps"].get(name, {"status": "pending"}) for name in ALL_STEPS
    }
    result: dict[str, Any] = {
        "run_id": value["run_id"],
        "release": value["release"],
        "status": value["status"],
        "steps": steps,
        "blocking_steps": [name for name in ALL_STEPS if name not in value["steps"]],
    }
    if value["status"] == "awaiting-user-confirmation":
        result["required_confirmation"] = confirmation_phrase(store.spec)
    if value["status"] == "awaiting-browser-verification":
        result["browser_automation_request"] = browser_request(store)
    recovery = sorted((store.root / "recovery").glob("*.json"))
    if recovery:
        result["recovery_receipts"] = [
            {"name": path.name, "sha256": sha256_file(path)} for path in recovery
        ]
    return result


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _read_operator_identity(path: Path) -> tuple[str, str, str, str]:
    # Reuse the already-reviewed production identity/password parser.  The
    # password is returned only in memory and is never included in a receipt.
    import importlib.util

    helper = ROOT / "scripts" / "v030_production_operator.py"
    spec = importlib.util.spec_from_file_location("ecorex_v030_operator", helper)
    if spec is None or spec.loader is None:
        raise ReleaseCommandError("release_ssh_helper_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.read_operator_file(path)
    except Exception:
        raise ReleaseCommandError("release_ssh_identity_invalid") from None


def _signer(prefix: str = "ECOREX_RELEASE_SIGNER") -> DigestPinnedExternalSigner:
    def required(suffix: str) -> str:
        value = os.environ.get(f"{prefix}_{suffix}")
        if not isinstance(value, str) or not value or "\0" in value:
            raise ReleaseCommandError("release_signer_configuration_missing")
        return value

    try:
        public = base64.b64decode(required("PUBLIC_KEY"), validate=True)
        executable_sha256 = required("EXECUTABLE_SHA256")
        if len(public) != 32 or _SHA256.fullmatch(executable_sha256) is None:
            raise ValueError
        return DigestPinnedExternalSigner(
            key_id=required("KEY_ID"),
            public_key=public,
            executable_path=required("EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=os.environ.get(f"{prefix}_ADAPTER") or None,
            adapter_sha256=os.environ.get(f"{prefix}_ADAPTER_SHA256") or None,
            environment=os.environ,
        )
    except ReleaseCommandError:
        raise
    except Exception:
        raise ReleaseCommandError("release_signer_configuration_invalid") from None


def _source_versions(commit: str) -> dict[str, str]:
    try:
        version_source = _git_bytes(commit, "ecorex/_version.py").decode("utf-8")
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', version_source, re.M)
        package = json.loads(_git_bytes(commit, "desktop/package.json"))
        lock = json.loads(_git_bytes(commit, "desktop/package-lock.json"))
        cli = _git_bytes(commit, "cli/VERSION").decode("utf-8").strip()
    except (UnicodeError, json.JSONDecodeError):
        raise ReleaseCommandError("release_version_source_invalid") from None
    if match is None or not isinstance(package, dict) or not isinstance(lock, dict):
        raise ReleaseCommandError("release_version_source_invalid")
    return {
        "python": match.group(1),
        "desktop": str(package.get("version", "")),
        "desktop_lock": str(lock.get("version", "")),
        "desktop_lock_package": str(
            (lock.get("packages") or {}).get("", {}).get("version", "")
            if isinstance(lock.get("packages"), dict)
            else ""
        ),
        "cli": cli,
    }


def _preflight(store: ReleaseRunStore) -> dict[str, Any]:
    spec = store.spec
    if "preflight" in store.read()["steps"]:
        return store.receipt("preflight")
    if not DEFAULT_CREDENTIAL_FILE.is_file() or DEFAULT_CREDENTIAL_FILE.is_symlink():
        raise ReleaseCommandError("release_ssh_credentials_unavailable")
    host, domain, user, password = _read_operator_identity(DEFAULT_CREDENTIAL_FILE)
    password = ""
    del password
    python = _python_311()
    node, npm = _node_tools()
    _signer()
    EnvironmentGitHubCredential().bearer_token()
    _git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    remote_main = _git("rev-parse", "refs/remotes/origin/main")
    if remote_main != spec.commit:
        raise ReleaseCommandError("release_commit_not_exact_remote_main")
    if _git("merge-base", "--is-ancestor", spec.commit, "refs/remotes/origin/main"):
        pass
    remote = _git("remote", "get-url", "origin")
    if not (
        remote.endswith("zyfjacksonchen-source/EcoreX.git")
        or remote.endswith("zyfjacksonchen-source/EcoreX")
    ):
        raise ReleaseCommandError("release_repository_mismatch")
    versions = _source_versions(spec.commit)
    if set(versions.values()) != {spec.version}:
        raise ReleaseCommandError("release_version_sources_mismatch")
    fingerprint_paths = (
        "requirements/locks/manifest.json",
        "requirements/locks/bootstrap.lock",
        "requirements/locks/dev.lock",
        "requirements/locks/cloud.lock",
        "requirements/locks/platform-stage.lock",
        "desktop/package-lock.json",
        "scripts/release-v1.py",
        ".github/workflows/ecorex-v1-ci.yml",
        ".github/workflows/ecorex-v1-platform-stage.yml",
        ".github/workflows/ecorex-v1-candidate.yml",
        ".github/workflows/emate-v030-macos-universal.yml",
        ".github/workflows/ecorex-v1-online-update.yml",
        "scripts/smoke-v1-online-update-macos.sh",
        "scripts/smoke-v1-online-update-windows.ps1",
    )
    source_digests = {
        path: hashlib.sha256(_git_bytes(spec.commit, path)).hexdigest()
        for path in fingerprint_paths
    }
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "release": spec.to_dict(),
        "repository": SOURCE_REPOSITORY,
        "origin_sha256": hashlib.sha256(remote.encode()).hexdigest(),
        "remote_main": remote_main,
        "versions": versions,
        "toolchain": {
            "python_path": str(python),
            "python": "3.11.9",
            "node_path": str(node),
            "node": "22.23.1",
            "npm_path": str(npm),
            "npm": _tool_version(
                (str(npm), "--version"),
                code="release_npm_unavailable",
                environment=_base_environment(node=node),
            ),
        },
        "source_digests": source_digests,
        "network": {"subprocess_proxy": _proxy() is not None},
        "ssh_target": {
            "host_sha256": hashlib.sha256(host.encode()).hexdigest(),
            "domain_sha256": hashlib.sha256(domain.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
        },
        "completed_at": _now(),
    }
    store.complete("preflight", receipt)
    return receipt


def _ensure_worktree(store: ReleaseRunStore) -> Path:
    source = store.root / "source"
    if source.exists():
        if _git("rev-parse", "HEAD", cwd=source) != store.spec.commit:
            raise ReleaseCommandError("release_worktree_identity_conflict")
        return source
    log = store.root / "logs" / "worktree-add.log"
    _run(
        ("git", "worktree", "add", "--detach", str(source), store.spec.commit),
        cwd=ROOT,
        environment=_base_environment(),
        log=log,
        timeout=300,
    )
    if _git("rev-parse", "HEAD", cwd=source) != store.spec.commit:
        raise ReleaseCommandError("release_worktree_identity_conflict")
    return source


def _gate_commands(source: Path, python: Path, npm: Path) -> tuple[tuple[str, ...], ...]:
    venv = source.parent / "python-3.11.9"
    vpy = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    npm_command = str(npm)
    npx = npm.with_name("npx.cmd" if npm.name.endswith(".cmd") else "npx")
    return (
        (str(python), "-m", "venv", str(venv)),
        (str(vpy), "scripts/install-v1-python-profile.py", "--profile", "dev"),
        (str(vpy), "scripts/install-v1-python-profile.py", "--profile", "cloud"),
        (str(vpy), "scripts/install-v1-python-profile.py", "--profile", "platform-stage"),
        (str(vpy), "scripts/run-v1-lint.py", "--compile", "--output-format=concise"),
        (npm_command, "ci"),
        (npm_command, "audit", "--audit-level=high"),
        (npm_command, "run", "typecheck"),
        (npm_command, "run", "build"),
        (
            str(vpy),
            "scripts/check-v1-reproducibility.py",
            "--web-dist",
            "desktop/dist",
            "--write-manifest",
            ".candidate/manual-gate/bytes/before/byte-contract.json",
        ),
        (npm_command, "run", "test:v1"),
        (str(npx), "playwright", "test", "--config=playwright.config.ts"),
        (str(vpy), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/v1"),
        (str(vpy), "scripts/check-v1-runtime-schema-authority.py"),
        (str(vpy), "scripts/check-v1-server-schema-authority.py"),
        (str(vpy), "scripts/check-v1-design-system.py"),
        (str(vpy), "scripts/check-v1-dependency-locks.py"),
        (str(vpy), "scripts/check-v1-legacy-cutoff.py", "--strict-production"),
        (str(vpy), "scripts/check-v1-public-download-site.py"),
        (
            str(vpy),
            "scripts/check-v1-candidate-supply-chain.py",
            "preflight",
            "--repo",
            ".",
            "--report",
            ".candidate/manual-supply-chain.json",
        ),
        (
            str(vpy),
            "scripts/check-v1-reproducibility.py",
            "--web-dist",
            "desktop/dist",
            "--write-manifest",
            ".candidate/manual-gate/bytes/after/byte-contract.json",
        ),
        (
            str(vpy),
            "scripts/check-v1-reproducibility.py",
            "--compare-manifests",
            ".candidate/manual-gate/bytes",
            "--expected-count",
            "2",
        ),
    )


def _verify_gate_receipt(path: Path, *, fingerprint: str, signer: DigestPinnedExternalSigner) -> dict[str, Any]:
    value = _json_file(path)
    signature_value = value.get("signature")
    unsigned = dict(value)
    unsigned.pop("signature", None)
    if value.get("fingerprint") != fingerprint or not isinstance(signature_value, str):
        raise ReleaseCommandError("local_gate_receipt_conflict")
    try:
        signature = base64.b64decode(signature_value, validate=True)
        Ed25519PublicKey.from_public_bytes(signer.public_key_bytes).verify(
            signature, _LOCAL_GATE_DOMAIN + canonical_json(unsigned)
        )
    except Exception:
        raise ReleaseCommandError("local_gate_signature_invalid") from None
    return value


def _local_gates(store: ReleaseRunStore, preflight: Mapping[str, Any]) -> dict[str, Any]:
    if "local-gates" in store.read()["steps"]:
        return store.receipt("local-gates")
    source = _ensure_worktree(store)
    python = Path(str(preflight["toolchain"]["python_path"]))
    npm = Path(str(preflight["toolchain"]["npm_path"]))
    node = Path(str(preflight["toolchain"]["node_path"]))
    commands = _gate_commands(source, python, npm)
    fingerprint_input = {
        "release": store.spec.to_dict(),
        "source_digests": preflight["source_digests"],
        "toolchain": preflight["toolchain"],
        "commands": commands,
    }
    fingerprint = hashlib.sha256(canonical_json(fingerprint_input)).hexdigest()
    signer = _signer()
    gate_path = store.root / "gates" / "local-gate.json"
    if gate_path.exists():
        receipt = _verify_gate_receipt(gate_path, fingerprint=fingerprint, signer=signer)
        store.complete("local-gates", receipt)
        return receipt
    environment = _base_environment(node=node)
    results = []
    for index, command in enumerate(commands, start=1):
        cwd = source / "desktop" if Path(command[0]) == npm or Path(command[0]).name.startswith("npx") else source
        results.append(
            _run(
                command,
                cwd=cwd,
                environment=environment,
                log=store.root / "logs" / f"local-gate-{index:02d}.log",
                timeout=4 * 60 * 60 if "pytest" in command else 60 * 60,
            )
        )
    if not (source / "desktop" / "node_modules").is_dir():
        raise ReleaseCommandError("release_npm_ci_not_effective")
    unsigned = {
        "schema_version": 1,
        "status": "passed",
        "release": store.spec.to_dict(),
        "fingerprint": fingerprint,
        "source_commit": _git("rev-parse", "HEAD", cwd=source),
        "toolchain": preflight["toolchain"],
        "commands_sha256": hashlib.sha256(canonical_json(commands)).hexdigest(),
        "results": results,
        "npm_ci_effective": True,
        "completed_at": _now(),
        "signer_key_id": signer.key_id,
    }
    signature = signer.sign(_LOCAL_GATE_DOMAIN + canonical_json(unsigned))
    receipt = {**unsigned, "signature": base64.b64encode(signature).decode("ascii")}
    _atomic_json(gate_path, receipt)
    _verify_gate_receipt(gate_path, fingerprint=fingerprint, signer=signer)
    store.complete("local-gates", receipt)
    return receipt


def _step_digest(store: ReleaseRunStore, step: str) -> str:
    value = store.read()["steps"].get(step)
    if not isinstance(value, dict) or _SHA256.fullmatch(str(value.get("sha256", ""))) is None:
        raise ReleaseCommandError("release_step_digest_unavailable")
    return str(value["sha256"])


def _platform_builds(store: ReleaseRunStore) -> dict[str, Any]:
    if "platform-build" in store.read()["steps"]:
        return store.receipt("platform-build")
    gate = _step_digest(store, "local-gates")
    common = {
        "source_sha": store.spec.commit,
        "version": store.spec.version,
        "from_version": store.spec.from_version,
        "local_gate_sha256": gate,
    }
    ci_title = f"manual-ci-v{store.spec.version}-{gate}"
    stage_title = f"manual-stage-v{store.spec.version}-{gate}"
    with GitHubActions() as github:
        # Dispatch both independent builders before waiting so GitHub can run
        # them concurrently without moving publication authority into CI.
        for workflow, title in (
            ("ecorex-v1-ci.yml", ci_title),
            ("ecorex-v1-platform-stage.yml", stage_title),
        ):
            if github.find_run(workflow, title=title, commit=store.spec.commit) is None:
                github.dispatch(workflow, inputs=common)
        ci = github.wait_run("ecorex-v1-ci.yml", title=ci_title, commit=store.spec.commit)
        stage = github.wait_run(
            "ecorex-v1-platform-stage.yml",
            title=stage_title,
            commit=store.spec.commit,
        )
        artifact_names = {item.get("name") for item in github.artifacts(int(stage["run_id"]))}
    required = {
        "ecorex-v1-stage-windows-x64",
        "ecorex-v1-stage-macos-arm64",
        "ecorex-v1-stage-macos-x64",
    }
    if not required.issubset(artifact_names):
        raise ReleaseCommandError("platform_stage_artifacts_incomplete")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "local_gate_sha256": gate,
        "ci": ci,
        "platform_stage": stage,
        "required_artifacts": sorted(required),
        "completed_at": _now(),
    }
    store.complete("platform-build", receipt)
    return receipt


def _candidate_build(store: ReleaseRunStore, platform: Mapping[str, Any]) -> dict[str, Any]:
    if "candidate-build" in store.read()["steps"]:
        return store.receipt("candidate-build")
    gate = _step_digest(store, "local-gates")
    ci = platform["ci"]
    stage = platform["platform_stage"]
    inputs = {
        "channel": store.spec.channel,
        "staging_run_id": str(stage["run_id"]),
        "ci_run_id": str(ci["run_id"]),
        "ci_run_attempt": str(ci["run_attempt"]),
        "source_sha": store.spec.commit,
        "version": store.spec.version,
        "from_version": store.spec.from_version,
        "local_gate_sha256": gate,
    }
    title = f"manual-candidate-v{store.spec.version}-{gate}"
    archive = store.root / "artifacts" / "accepted-candidate.zip"
    with GitHubActions() as github:
        run = github.ensure_run(
            "ecorex-v1-candidate.yml",
            title=title,
            commit=store.spec.commit,
            inputs=inputs,
        )
        artifact = github.download(
            int(run["run_id"]), f"ecorex-v1-accepted-{store.spec.channel}", archive
        )
    candidate = store.root / "candidate"
    if not candidate.exists():
        _safe_extract(archive, candidate)
    manifest = _json_file(candidate / "output/release/release-manifest.json")
    signed = _json_file(candidate / "output/candidate-build-receipt.json")
    if (
        manifest.get("version") != store.spec.version
        or signed.get("version") != store.spec.version
        or signed.get("commit_sha") != store.spec.commit
        or manifest.get("release_id") != signed.get("release_id")
    ):
        raise ReleaseCommandError("candidate_identity_mismatch")
    required_gates = {"cdp-acceptance", "image-soak", "live-image", "live-model", "signature"}
    if any(not (candidate / "gates" / f"{name}.json").is_file() for name in required_gates):
        raise ReleaseCommandError("candidate_gate_set_incomplete")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "workflow": run,
        "artifact": artifact,
        "release_id": manifest["release_id"],
        "build_digest": manifest.get("build_digest"),
        "candidate_receipt_sha256": sha256_file(
            candidate / "output/candidate-build-receipt.json"
        ),
        "required_gates": sorted(required_gates),
        "completed_at": _now(),
    }
    store.complete("candidate-build", receipt)
    return receipt


def _public_key_file(store: ReleaseRunStore) -> Path:
    try:
        material = base64.b64decode(
            os.environ["ECOREX_RELEASE_SIGNER_PUBLIC_KEY"], validate=True
        )
    except (KeyError, TypeError, ValueError):
        raise ReleaseCommandError("release_signer_configuration_missing") from None
    if len(material) != 32:
        raise ReleaseCommandError("release_signer_configuration_invalid")
    path = store.root / "handoff" / "release-public-key.bin"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.read_bytes() != material:
            raise ReleaseCommandError("release_public_key_conflict")
        return path
    with path.open("xb") as stream:
        os.chmod(path, 0o644)
        stream.write(material)
    return path


def _publisher_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(connect=20, read=180, write=180, pool=20),
        follow_redirects=False,
        trust_env=False,
        proxy=_proxy(),
    )


def _draft_publisher() -> tuple[GitHubReleasePublisher, httpx.Client]:
    client = _publisher_client()
    publisher = GitHubReleasePublisher(
        owner=INSTALLER_OWNER,
        repository=INSTALLER_REPOSITORY,
        credentials=EnvironmentGitHubCredential(),
        client=client,
    )
    return publisher, client


def _draft_from_receipt(value: Mapping[str, Any]) -> GitHubReleaseDraft:
    draft = value.get("draft")
    if not isinstance(draft, Mapping):
        raise ReleaseCommandError("github_draft_receipt_invalid")
    try:
        return GitHubReleaseDraft(
            release_id=int(draft["release_id"]),
            tag_name=str(draft["tag_name"]),
            upload_url=str(draft["upload_url"]),
            draft=bool(draft["draft"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ReleaseCommandError("github_draft_receipt_invalid") from None


def _upload_assets(
    draft: GitHubReleaseDraft,
    paths: Sequence[Path],
) -> list[dict[str, Any]]:
    unique: dict[str, Path] = {}
    for path in paths:
        existing = unique.get(path.name)
        if existing is not None and sha256_file(existing) != sha256_file(path):
            raise ReleaseCommandError("github_asset_name_conflict")
        unique[path.name] = path
    publisher, client = _draft_publisher()
    try:
        receipts = [
            publisher.ensure_asset(draft, path, expected_sha256=sha256_file(path))
            for path in unique.values()
        ]
        return [
            {
                "asset_id": item.asset_id,
                "name": item.name,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "url": item.browser_download_url,
            }
            for item in receipts
        ]
    finally:
        publisher.close()
        client.close()


def _package_smoke(store: ReleaseRunStore, candidate: Mapping[str, Any]) -> dict[str, Any]:
    if "package-smoke" in store.read()["steps"]:
        return store.receipt("package-smoke")
    source = _ensure_worktree(store)
    vpy = source.parent / "python-3.11.9" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    key_file = _public_key_file(store)
    key_id = os.environ.get("ECOREX_RELEASE_SIGNER_KEY_ID", "")
    if not key_id:
        raise ReleaseCommandError("release_signer_configuration_missing")
    candidate_root = store.root / "candidate"
    packages = store.root / "packages"
    packages.mkdir(parents=True, exist_ok=True, mode=0o700)
    windows = packages / f"EcoreX_{store.spec.version}-webui-windows-x64.zip"
    windows_receipt = packages / "emate-webui-build-receipt.json"
    if not windows.is_file() or not windows_receipt.is_file():
        _run_json(
            (
                str(vpy),
                "scripts/build-v030-windows-webui.py",
                "--release-dir",
                str(candidate_root / "output/release"),
                "--candidate-receipt",
                str(candidate_root / "output/candidate-build-receipt.json"),
                "--output",
                str(packages),
                "--trusted-public-key",
                f"{key_id}={key_file}",
                "--production-key-id",
                key_id,
            ),
            cwd=source,
            environment=_operator_environment(),
            log=store.root / "logs" / "build-windows-webui.log",
        )
    direct = packages / f"EcoreX_{store.spec.version}-direct-candidate.zip"
    accepted_archive = store.root / "artifacts" / "accepted-candidate.zip"
    if not direct.exists():
        shutil.copyfile(accepted_archive, direct)
        os.chmod(direct, 0o600)
    manifest = _json_file(candidate_root / "output/release/release-manifest.json")
    release_id = str(manifest.get("release_id", ""))
    publisher, client = _draft_publisher()
    try:
        draft = publisher.ensure_draft(
            version=store.spec.version,
            channel=ReleaseChannel(store.spec.channel),
            release_id=release_id,
        )
    finally:
        publisher.close()
        client.close()
    initial_assets = _upload_assets(draft, (direct, windows, windows_receipt))
    gate = _step_digest(store, "local-gates")
    inputs = {
        "candidate_tag": draft.tag_name,
        "candidate_bundle_sha256": sha256_file(direct),
        "candidate_commit_sha": store.spec.commit,
        "version": store.spec.version,
        "from_version": store.spec.from_version,
        "local_gate_sha256": gate,
        "windows_package_sha256": sha256_file(windows),
        "windows_receipt_sha256": sha256_file(windows_receipt),
        "release_repository": f"{INSTALLER_OWNER}/{INSTALLER_REPOSITORY}",
    }
    title = f"manual-webui-v{store.spec.version}-{gate}"
    artifact_root = store.root / "package-artifacts"
    with GitHubActions() as github:
        run = github.ensure_run(
            "emate-v030-macos-universal.yml",
            title=title,
            commit=store.spec.commit,
            inputs=inputs,
        )
        downloads = []
        for name in (
            f"emate-v{store.spec.version}-arm64-qualified-webui-packages",
            f"emate-v{store.spec.version}-macos-x64-user-smoke",
            f"emate-v{store.spec.version}-windows-x64-user-smoke",
        ):
            archive = artifact_root / f"{name}.zip"
            downloads.append(github.download(int(run["run_id"]), name, archive))
            target = artifact_root / name
            if not target.exists():
                _safe_extract(archive, target)
    qualified = artifact_root / f"emate-v{store.spec.version}-arm64-qualified-webui-packages"
    macos = qualified / f"EcoreX_{store.spec.version}-webui-macos-universal.zip"
    final_receipt = qualified / "webui-build-receipt.json"
    if not macos.is_file() or not final_receipt.is_file():
        raise ReleaseCommandError("webui_package_artifacts_incomplete")
    final_value = _json_file(final_receipt)
    if final_value.get("version") != store.spec.version or final_value.get("status") != "verified":
        raise ReleaseCommandError("webui_package_receipt_invalid")
    local_smoke = packages / "macos-local-user-smoke.json"
    if not local_smoke.exists():
        _run(
            (
                "bash",
                "scripts/smoke-v030-macos-terminal-package.sh",
                str(macos),
                os.uname().machine,
                str(local_smoke),
                store.spec.version,
            ),
            cwd=source,
            environment=_operator_environment(),
            log=store.root / "logs" / "macos-local-user-smoke.log",
            timeout=60 * 60,
        )
    final_assets = _upload_assets(
        draft,
        (
            macos,
            final_receipt,
            local_smoke,
            *tuple(
                path
                for path in artifact_root.rglob("*.json")
                if path.is_file() and not path.is_symlink()
            ),
        ),
    )
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "workflow": run,
        "downloads": downloads,
        "packages": {
            path.name: {
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "path": str(path.relative_to(store.root)),
            }
            for path in (direct, windows, windows_receipt, macos, final_receipt, local_smoke)
        },
        "draft": {
            "release_id": draft.release_id,
            "tag_name": draft.tag_name,
            "upload_url": draft.upload_url,
            "draft": draft.draft,
        },
        "assets": initial_assets + final_assets,
        "completed_at": _now(),
    }
    store.complete("package-smoke", receipt)
    return receipt


def _trusted_argument(prefix: str) -> str:
    key = os.environ.get(f"{prefix}_KEY_ID")
    public = os.environ.get(f"{prefix}_PUBLIC_KEY")
    if not key or not public:
        raise ReleaseCommandError("release_signer_configuration_missing")
    try:
        if len(base64.b64decode(public, validate=True)) != 32:
            raise ValueError
    except (TypeError, ValueError):
        raise ReleaseCommandError("release_signer_configuration_invalid") from None
    return f"{key}={public}"


def _publication_prepare(
    store: ReleaseRunStore,
    candidate: Mapping[str, Any],
    packages: Mapping[str, Any],
) -> dict[str, Any]:
    if "github-draft" in store.read()["steps"]:
        return store.receipt("github-draft")
    source = _ensure_worktree(store)
    candidate_root = store.root / "candidate"
    release_dir = candidate_root / "output" / "release"
    publication_config = Path(
        os.environ.get("ECOREX_RELEASE_PUBLICATION_CONFIG", "")
    ).expanduser()
    bootstrap_config = Path(
        os.environ.get("ECOREX_BOOTSTRAP_INDEX_PUBLICATION_CONFIG", "")
    ).expanduser()
    if not publication_config.is_file() or not bootstrap_config.is_file():
        raise ReleaseCommandError("release_publication_config_missing")
    release_key = _trusted_argument("ECOREX_RELEASE_SIGNER")
    publication_key = _trusted_argument("ECOREX_PUBLICATION_SIGNER")
    vpy = source.parent / "python-3.11.9" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment = _operator_environment()
    prepared = store.root / "prepared"
    output = candidate_root / "output"
    publication_receipt = output / "publication-receipt.json"
    if not publication_receipt.exists():
        _run_json(
            (
                str(vpy),
                "-m",
                "ecorex.control_plane.cli",
                "publish-assets",
                "--release-dir",
                str(release_dir),
                "--publication-config",
                str(publication_config.resolve()),
                "--receipt",
                str(publication_receipt),
                "--trusted-key",
                release_key,
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "publish-primary-assets.log",
            timeout=4 * 60 * 60,
        )
    public_index = output / "public-bootstrap-index.json"
    if not public_index.exists():
        _run_json(
            (
                str(vpy),
                "-m",
                "ecorex.control_plane.cli",
                "build-public-bootstrap-index",
                "--release-dir",
                str(release_dir),
                "--publication-receipt",
                str(publication_receipt),
                "--output",
                str(public_index),
                "--trusted-key",
                release_key,
                "--trusted-publication-key",
                publication_key,
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "build-public-index.log",
        )
    index_stage_receipt = output / "bootstrap-index-stage-receipt.json"
    if not index_stage_receipt.exists():
        _run_json(
            (
                str(vpy),
                "-m",
                "ecorex.control_plane.cli",
                "stage-public-bootstrap-index",
                "--release-dir",
                str(release_dir),
                "--publication-receipt",
                str(publication_receipt),
                "--index",
                str(public_index),
                "--publication-config",
                str(bootstrap_config.resolve()),
                "--receipt",
                str(index_stage_receipt),
                "--trusted-key",
                release_key,
                "--trusted-publication-key",
                publication_key,
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "stage-public-index.log",
        )
    manifest = _json_file(release_dir / "release-manifest.json")
    release_id = str(manifest.get("release_id", ""))
    site_stage = prepared / "site-staging" / release_id
    site = site_stage / "site"
    if not site.exists():
        site_stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(source / "deploy" / "ecorex-site", site)
    shutil.copyfile(public_index, site / "public-bootstrap-index.json")
    unsigned = prepared / "protected-deployment-admission-unsigned.json"
    if not unsigned.exists():
        artifact = candidate.get("artifact")
        workflow = candidate.get("workflow")
        if not isinstance(artifact, Mapping) or not isinstance(workflow, Mapping):
            raise ReleaseCommandError("candidate_receipt_invalid")
        _run_json(
            (
                str(vpy),
                "scripts/assemble-v1-protected-deployment-admission.py",
                "--candidate-root",
                str(candidate_root),
                "--site-root",
                str(site),
                "--repository",
                SOURCE_REPOSITORY,
                "--commit-sha",
                store.spec.commit,
                "--channel",
                store.spec.channel,
                "--candidate-run-id",
                str(workflow["run_id"]),
                "--candidate-run-attempt",
                str(workflow["run_attempt"]),
                "--candidate-artifact-id",
                str(artifact["artifact_id"]),
                "--candidate-artifact-sha256",
                str(artifact["sha256"]),
                "--mode",
                "create-and-activate",
                "--rollout-percentage",
                "100",
                "--valid-for-seconds",
                "86400",
                "--output",
                str(unsigned),
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "assemble-deployment-admission.log",
        )
    admission = prepared / "protected-deployment-admission.json"
    if not admission.exists():
        _run_json(
            (
                str(vpy),
                "scripts/sign-v1-protected-deployment-admission.py",
                "--unsigned",
                str(unsigned),
                "--output",
                str(admission),
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "sign-deployment-admission.log",
        )
    target_admission = site_stage / "protected-deployment-admission.json"
    if not target_admission.exists():
        shutil.copyfile(admission, target_admission)
    authorization = site_stage / "deployment-authorization.json"
    if not authorization.exists():
        _run_json(
            (
                str(vpy),
                "scripts/sign-v1-public-site-deployment.py",
                "--release-id",
                release_id,
                "--staging-release-dir",
                str(site_stage),
                "--cloud-artifact-root",
                str(output / "cloud"),
                "--protected-admission",
                str(target_admission),
            ),
            cwd=source,
            environment=environment,
            log=store.root / "logs" / "sign-public-site.log",
        )
    draft = _draft_from_receipt(packages)
    # Reuse the release verifier used by the production upload CLI before
    # adding any signed release file to the already-created private Draft.
    from ecorex.control_plane.cli import _verify_release_directory

    verified = _verify_release_directory(release_dir, [release_key])
    release_assets = _upload_assets(draft, verified.files)
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "draft": packages["draft"],
        "release_assets": release_assets,
        "publication_receipt": {
            "path": str(publication_receipt.relative_to(store.root)),
            "sha256": sha256_file(publication_receipt),
        },
        "public_index": {
            "path": str(public_index.relative_to(store.root)),
            "sha256": sha256_file(public_index),
            "stage_receipt_sha256": sha256_file(index_stage_receipt),
        },
        "deployment": {
            "release_id": release_id,
            "admission_sha256": sha256_file(admission),
            "site_authorization_sha256": sha256_file(authorization),
            "cloud_manifest_sha256": sha256_file(output / "cloud/cloud-release-manifest.json"),
        },
        "completed_at": _now(),
    }
    store.complete("github-draft", receipt)
    return receipt


class ProductionRemote:
    """Fixed-host SFTP/SSH operator using the existing fenced credential parser."""

    def __init__(self) -> None:
        import importlib.util

        helper = ROOT / "scripts" / "v030_production_operator.py"
        module_spec = importlib.util.spec_from_file_location(
            "ecorex_v030_operator_connection", helper
        )
        if module_spec is None or module_spec.loader is None:
            raise ReleaseCommandError("release_ssh_helper_unavailable")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        try:
            self.client = module.connect_operator(DEFAULT_CREDENTIAL_FILE)
            self.sftp = self.client.open_sftp()
        except Exception:
            raise ReleaseCommandError("release_ssh_connection_failed") from None

    def close(self) -> None:
        try:
            self.sftp.close()
        finally:
            self.client.close()

    def __enter__(self) -> "ProductionRemote":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _mkdir(self, path: str, mode: int = 0o755) -> None:
        pure = PurePosixPath(path)
        if not pure.is_absolute():
            raise ReleaseCommandError("release_remote_path_invalid")
        current = PurePosixPath("/")
        for part in pure.parts[1:]:
            current /= part
            current_text = current.as_posix()
            try:
                metadata = self.sftp.lstat(current_text)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ReleaseCommandError("release_remote_path_conflict")
            except OSError:
                self.sftp.mkdir(current_text, mode=mode)
        self.sftp.chmod(path, mode)

    def upload_tree(self, source: Path, destination: str) -> dict[str, Any]:
        root = source.resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise ReleaseCommandError("release_upload_tree_invalid")
        if not destination.startswith("/srv/ecorex-upload/manual-"):
            raise ReleaseCommandError("release_remote_path_invalid")
        self._mkdir(destination, 0o755)
        records = []
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ReleaseCommandError("release_upload_tree_invalid")
            relative = path.relative_to(root).as_posix()
            target = f"{destination}/{relative}"
            if path.is_dir():
                self._mkdir(target, 0o755)
                continue
            digest = sha256_file(path)
            records.append(
                {"path": relative, "size_bytes": metadata.st_size, "sha256": digest}
            )
            try:
                remote = self.sftp.lstat(target)
            except OSError:
                remote = None
            if remote is not None:
                if not stat.S_ISREG(remote.st_mode) or remote.st_size != metadata.st_size:
                    raise ReleaseCommandError("release_remote_file_conflict")
                # The server-side authorities rehash every byte.  An equal-size
                # interrupted upload is overwritten here before that check.
            temporary = f"{target}.upload-{secrets.token_hex(8)}"
            self.sftp.put(str(path), temporary)
            self.sftp.chmod(temporary, 0o755 if metadata.st_mode & 0o111 else 0o644)
            self.sftp.posix_rename(temporary, target)
        return {
            "file_count": len(records),
            "tree_sha256": hashlib.sha256(canonical_json(records)).hexdigest(),
        }

    def upload_file(self, source: Path, destination: str, *, mode: int = 0o600) -> None:
        self._mkdir(str(PurePosixPath(destination).parent), 0o700)
        temporary = f"{destination}.upload-{secrets.token_hex(8)}"
        try:
            self.sftp.put(str(source), temporary)
            self.sftp.chmod(temporary, mode)
            self.sftp.posix_rename(temporary, destination)
        except Exception:
            try:
                self.sftp.remove(temporary)
            except Exception:
                pass
            raise ReleaseCommandError("release_remote_upload_failed") from None

    def read(self, path: str, *, maximum: int = 2 * 1024 * 1024) -> bytes:
        try:
            with self.sftp.open(path, "rb") as stream:
                payload = stream.read(maximum + 1)
        except Exception:
            raise ReleaseCommandError("release_remote_read_failed") from None
        if not 1 <= len(payload) <= maximum:
            raise ReleaseCommandError("release_remote_read_failed")
        return payload

    def run(self, arguments: Sequence[str], *, timeout: int = 60 * 60) -> dict[str, Any]:
        command = " ".join(shlex.quote(str(item)) for item in arguments)
        try:
            _stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            output = stdout.read(2 * 1024 * 1024 + 1)
            error = stderr.read(128 * 1024 + 1)
            status = stdout.channel.recv_exit_status()
        except Exception:
            raise ReleaseCommandError("release_remote_command_failed") from None
        if status != 0 or error or not 1 <= len(output) <= 2 * 1024 * 1024:
            raise ReleaseCommandError("release_remote_command_failed")
        try:
            value = json.loads(output.decode("utf-8").splitlines()[-1])
        except (UnicodeError, IndexError, json.JSONDecodeError):
            raise ReleaseCommandError("release_remote_receipt_invalid") from None
        if not isinstance(value, dict) or value.get("ok") is False:
            raise ReleaseCommandError("release_remote_receipt_invalid")
        return value


def _remote_root(store: ReleaseRunStore) -> str:
    return f"/srv/ecorex-upload/manual-v{store.spec.version}-{store.spec.commit[:12]}"


def _upload_production_bundle(
    store: ReleaseRunStore,
    remote: ProductionRemote,
    publication: Mapping[str, Any],
    packages: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    root = _remote_root(store)
    candidate = store.root / "candidate"
    cloud = candidate / "output" / "cloud"
    site = store.root / "prepared" / "site-staging"
    package_root = store.root / "remote-packages"
    package_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    package_paths = packages.get("packages")
    if not isinstance(package_paths, Mapping):
        raise ReleaseCommandError("webui_package_receipt_invalid")
    for name in (
        f"EcoreX_{store.spec.version}-webui-windows-x64.zip",
        f"EcoreX_{store.spec.version}-webui-macos-universal.zip",
        "webui-build-receipt.json",
    ):
        item = package_paths.get(name)
        if not isinstance(item, Mapping):
            raise ReleaseCommandError("webui_package_receipt_invalid")
        source = store.root / str(item.get("path", ""))
        target = package_root / name
        if target.exists():
            if sha256_file(target) != item.get("sha256"):
                raise ReleaseCommandError("webui_package_receipt_invalid")
        else:
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
    uploads = {
        "cloud": remote.upload_tree(cloud, f"{root}/cloud"),
        "site": remote.upload_tree(site, f"{root}/site-staging"),
        "packages": remote.upload_tree(package_root, f"{root}/packages"),
    }
    template = remote.read("/etc/ecorex/cloud/deployment-spec.json")
    try:
        deployment_spec = json.loads(template.decode("utf-8"))
        cloud_manifest = _json_file(cloud / "cloud-release-manifest.json")
    except (UnicodeError, json.JSONDecodeError):
        raise ReleaseCommandError("release_cloud_spec_invalid") from None
    if not isinstance(deployment_spec, dict):
        raise ReleaseCommandError("release_cloud_spec_invalid")
    deployment = publication.get("deployment")
    if not isinstance(deployment, Mapping):
        raise ReleaseCommandError("release_deployment_receipt_invalid")
    deployment_spec.update(
        {
            "release_id": deployment["release_id"],
            "source_commit": store.spec.commit,
            "dependency_lock_manifest_sha256": cloud_manifest[
                "dependency_lock_manifest_sha256"
            ],
            "artifact_root": f"{root}/cloud",
            "artifact_manifest_sha256": deployment["cloud_manifest_sha256"],
        }
    )
    local_spec = store.root / "prepared" / "production-cloud-spec.json"
    _atomic_json(local_spec, deployment_spec)
    remote.upload_file(local_spec, f"{root}/production-cloud-spec.json")
    return root, uploads


def _stage_production(
    store: ReleaseRunStore,
    publication: Mapping[str, Any],
    packages: Mapping[str, Any],
) -> None:
    deployment = publication.get("deployment")
    if not isinstance(deployment, Mapping):
        raise ReleaseCommandError("release_deployment_receipt_invalid")
    release_id = str(deployment.get("release_id", ""))
    with ProductionRemote() as remote:
        root, uploads = _upload_production_bundle(store, remote, publication, packages)
        spec_value = _json_file(store.root / "prepared" / "production-cloud-spec.json")
        confirmation = str(spec_value.get("target_machine_id_sha256", ""))
        python = f"{root}/cloud/venv/bin/python"
        if "cloud-stage" not in store.read()["steps"]:
            cloud = remote.run(
                (
                    python,
                    "-m",
                    "ecorex.deployment.cloud_sidecar",
                    "--spec",
                    f"{root}/production-cloud-spec.json",
                    "--stage",
                    "--confirm-target",
                    confirmation,
                ),
                timeout=30 * 60,
            )
            if cloud.get("status") != "staged" or cloud.get("live_routes_changed") is not False:
                raise ReleaseCommandError("release_cloud_stage_invalid")
            store.complete(
                "cloud-stage",
                {
                    "schema_version": 1,
                    "status": "passed",
                    "upload": uploads["cloud"],
                    "remote": cloud,
                    "completed_at": _now(),
                },
            )
        if "site-stage" not in store.read()["steps"]:
            # Copy only into the fixed inactive staging namespace. Existing
            # complete staging is reused and revalidated by the deployer.
            remote.run(
                (
                    python,
                    "-c",
                    (
                        "import json,pathlib,shutil,sys;"
                        "s=pathlib.Path(sys.argv[1]);t=pathlib.Path(sys.argv[2]);"
                        "t.parent.mkdir(parents=True,exist_ok=True);"
                        "shutil.copytree(s,t) if not t.exists() else None;"
                        "print(json.dumps({'ok':True,'status':'ready'}))"
                    ),
                    f"{root}/site-staging/{release_id}",
                    f"/srv/ecorex-agent-download/site-staging/{release_id}",
                )
            )
            site = remote.run(
                (
                    python,
                    "-m",
                    "ecorex.deployment.public_site",
                    "--release-id",
                    release_id,
                    "--stage",
                )
            )
            legacy = remote.run(
                (
                    python,
                    "-m",
                    "ecorex.release.legacy_webui_publication",
                    f"{root}/packages/webui-build-receipt.json",
                    "--stage",
                ),
                timeout=30 * 60,
            )
            if (
                site.get("status") != "staged"
                or site.get("live_pointer_changed") is not False
                or legacy.get("status") != "staged"
                or legacy.get("update_pointer_changed") is not False
            ):
                raise ReleaseCommandError("release_site_stage_invalid")
            store.complete(
                "site-stage",
                {
                    "schema_version": 1,
                    "status": "passed",
                    "upload": {"site": uploads["site"], "packages": uploads["packages"]},
                    "site": site,
                    "legacy_packages": legacy,
                    "completed_at": _now(),
                },
            )


def _prepare(store: ReleaseRunStore) -> None:
    preflight = _preflight(store)
    _local_gates(store, preflight)
    platform = _platform_builds(store)
    candidate = _candidate_build(store, platform)
    packages = _package_smoke(store, candidate)
    publication = _publication_prepare(store, candidate, packages)
    _stage_production(store, publication, packages)
    if store.read()["status"] != "awaiting-user-confirmation":
        raise ReleaseCommandError("release_prepare_incomplete")


def _interactive_confirmation(prompt: str) -> None:
    if not sys.stdin.isatty():
        raise ReleaseCommandError("release_interactive_confirmation_required")
    print(f"Type exactly: {prompt}", file=sys.stderr)
    try:
        value = input().strip()
    except (EOFError, KeyboardInterrupt):
        raise ReleaseCommandError("release_confirmation_rejected") from None
    if value != prompt:
        raise ReleaseCommandError("release_confirmation_rejected")


def _github_release(store: ReleaseRunStore) -> dict[str, Any]:
    if "github-release" in store.read()["steps"]:
        return store.receipt("github-release")
    package = store.receipt("package-smoke")
    draft = _draft_from_receipt(package)
    publisher, client = _draft_publisher()
    try:
        published = publisher.publish(draft)
        assets = publisher.list_assets(published)
    finally:
        publisher.close()
        client.close()
    expected = {
        item["name"]: item["sha256"]
        for item in package.get("assets", [])
        if isinstance(item, Mapping)
    }
    publication = store.receipt("github-draft")
    expected.update(
        {
            item["name"]: item["sha256"]
            for item in publication.get("release_assets", [])
            if isinstance(item, Mapping)
        }
    )
    observed = {item.name: item.sha256 for item in assets}
    if not expected.items() <= observed.items():
        raise ReleaseCommandError("github_release_assets_incomplete")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "release_id": published.release_id,
        "tag_name": published.tag_name,
        "draft": published.draft,
        "assets": [
            {
                "name": item.name,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "url": item.browser_download_url,
            }
            for item in assets
        ],
        "completed_at": _now(),
    }
    store.complete("github-release", receipt)
    return receipt


def _remote_activation(store: ReleaseRunStore) -> None:
    publication = store.receipt("github-draft")
    deployment = publication.get("deployment")
    if not isinstance(deployment, Mapping):
        raise ReleaseCommandError("release_deployment_receipt_invalid")
    release_id = str(deployment["release_id"])
    root = _remote_root(store)
    spec = _json_file(store.root / "prepared" / "production-cloud-spec.json")
    confirmation = str(spec.get("target_machine_id_sha256", ""))
    python = f"{root}/cloud/venv/bin/python"
    with ProductionRemote() as remote:
        if "cloud-activation" not in store.read()["steps"]:
            cloud = remote.run(
                (
                    python,
                    "-m",
                    "ecorex.deployment.cloud_sidecar",
                    "--spec",
                    f"{root}/production-cloud-spec.json",
                    "--apply",
                    "--confirm-target",
                    confirmation,
                ),
                timeout=60 * 60,
            )
            if cloud.get("active_release_id") != release_id:
                raise ReleaseCommandError("release_cloud_activation_invalid")
            store.complete(
                "cloud-activation",
                {
                    "schema_version": 1,
                    "status": "passed",
                    "remote": cloud,
                    "completed_at": _now(),
                },
            )
        if "site-activation" not in store.read()["steps"]:
            prior = remote.run(
                (
                    python,
                    "-c",
                    (
                        "import json,os,pathlib;"
                        "p=pathlib.Path('/srv/ecorex-agent-download/current');"
                        "r=(p.resolve().name if p.exists() else None);"
                        "print(json.dumps({'ok':True,'release_id':r}))"
                    ),
                )
            )
            try:
                site = remote.run(
                    (
                        python,
                        "-m",
                        "ecorex.deployment.public_site",
                        "--release-id",
                        release_id,
                        "--apply",
                        "--confirm-target",
                        PUBLIC_URL,
                    )
                )
            except ReleaseCommandError:
                remote.run(
                    (
                        python,
                        "-m",
                        "ecorex.deployment.cloud_sidecar",
                        "--spec",
                        f"{root}/production-cloud-spec.json",
                        "--rollback",
                        "--confirm-target",
                        confirmation,
                    ),
                    timeout=60 * 60,
                )
                raise
            if site.get("release_id") != release_id or site.get("status") != "passed":
                raise ReleaseCommandError("release_site_activation_invalid")
            store.complete(
                "site-activation",
                {
                    "schema_version": 1,
                    "status": "passed",
                    "previous_release_id": prior.get("release_id"),
                    "remote": site,
                    "completed_at": _now(),
                },
            )


def _write_backup(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ReleaseCommandError("release_rollback_backup_conflict")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _pointer_backups(store: ReleaseRunStore, remote: ProductionRemote) -> dict[str, Any]:
    paths = {
        "legacy": "/srv/ecorex-agent-download/legacy-pointer/manifest.json",
        "bootstrap": "/srv/ecorex-agent-download/public-pointer/public-bootstrap-index.json",
    }
    result: dict[str, Any] = {}
    for name, remote_path in paths.items():
        payload = remote.read(remote_path)
        local = store.root / "rollback" / f"{name}.json"
        _write_backup(local, payload)
        remote_backup = f"{_remote_root(store)}/rollback/{name}.json"
        remote.upload_file(local, remote_backup, mode=0o600)
        result[name] = {
            "remote_path": remote_path,
            "backup_path": str(local.relative_to(store.root)),
            "remote_backup": remote_backup,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    legacy = _json_file(store.root / str(result["legacy"]["backup_path"]))
    if legacy.get("version") != store.spec.from_version:
        raise ReleaseCommandError("release_source_pointer_version_mismatch")
    return result


def _restore_pointer(remote: ProductionRemote, value: Mapping[str, Any], python: str) -> None:
    script = (
        "import fcntl,hashlib,json,os,pathlib,sys,tempfile;"
        "t=pathlib.Path(sys.argv[1]);b=pathlib.Path(sys.argv[2]);e=sys.argv[3];"
        "p=b.read_bytes();assert hashlib.sha256(p).hexdigest()==e;"
        "l=open('/run/lock/ecorex-cloud-deploy.lock','a+b');fcntl.flock(l,fcntl.LOCK_EX);"
        "s=t.stat();fd,n=tempfile.mkstemp(dir=t.parent,prefix='.'+t.name+'.rollback-');"
        "os.write(fd,p);os.fsync(fd);os.close(fd);os.chown(n,s.st_uid,s.st_gid);"
        "os.chmod(n,s.st_mode & 0o777);os.replace(n,t);"
        "d=os.open(t.parent,os.O_RDONLY);os.fsync(d);os.close(d);"
        "print(json.dumps({'ok':True,'status':'restored','sha256':e}))"
    )
    receipt = remote.run(
        (
            python,
            "-c",
            script,
            str(value["remote_path"]),
            str(value["remote_backup"]),
            str(value["sha256"]),
        )
    )
    if receipt.get("sha256") != value.get("sha256"):
        raise ReleaseCommandError("release_pointer_restore_failed")


def _activate_update_notification(store: ReleaseRunStore) -> dict[str, Any]:
    if "update-notification" in store.read()["steps"]:
        return store.receipt("update-notification")
    source = _ensure_worktree(store)
    vpy = source.parent / "python-3.11.9" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidate = store.root / "candidate"
    output = candidate / "output"
    bootstrap_config = Path(
        os.environ.get("ECOREX_BOOTSTRAP_INDEX_PUBLICATION_CONFIG", "")
    ).expanduser()
    root = _remote_root(store)
    python = f"{root}/cloud/venv/bin/python"
    activation_receipt = output / "bootstrap-index-publication-receipt.json"
    with ProductionRemote() as remote:
        backups = _pointer_backups(store, remote)
        try:
            if not activation_receipt.exists():
                _run_json(
                    (
                        str(vpy),
                        "-m",
                        "ecorex.control_plane.cli",
                        "activate-public-bootstrap-index",
                        "--release-dir",
                        str(output / "release"),
                        "--publication-receipt",
                        str(output / "publication-receipt.json"),
                        "--index",
                        str(output / "public-bootstrap-index.json"),
                        "--stage-receipt",
                        str(output / "bootstrap-index-stage-receipt.json"),
                        "--publication-config",
                        str(bootstrap_config.resolve()),
                        "--receipt",
                        str(activation_receipt),
                        "--trusted-key",
                        _trusted_argument("ECOREX_RELEASE_SIGNER"),
                        "--trusted-publication-key",
                        _trusted_argument("ECOREX_PUBLICATION_SIGNER"),
                    ),
                    cwd=source,
                    environment=_operator_environment(),
                    log=store.root / "logs" / "activate-public-index.log",
                )
            legacy = remote.run(
                (
                    python,
                    "-m",
                    "ecorex.release.legacy_webui_publication",
                    f"{root}/packages/webui-build-receipt.json",
                    "--from-version",
                    store.spec.from_version,
                    "--receipt-output",
                    f"{root}/legacy-publication-receipt.json",
                ),
                timeout=30 * 60,
            )
            if legacy.get("version") != store.spec.version or legacy.get("status") != "published":
                raise ReleaseCommandError("release_legacy_notification_invalid")
        except Exception:
            _restore_pointer(remote, backups["bootstrap"], python)
            _restore_pointer(remote, backups["legacy"], python)
            activation_receipt.unlink(missing_ok=True)
            raise
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "version": store.spec.version,
        "from_version": store.spec.from_version,
        "bootstrap_activation_sha256": sha256_file(activation_receipt),
        "legacy": legacy,
        "previous_pointers": backups,
        "browser_nonce": secrets.token_hex(32),
        "completed_at": _now(),
    }
    store.complete("update-notification", receipt)
    return receipt


def _public_readback(store: ReleaseRunStore) -> dict[str, Any]:
    proxy = _proxy()
    client = httpx.Client(
        timeout=httpx.Timeout(connect=15, read=180, write=30, pool=15),
        follow_redirects=True,
        trust_env=False,
        proxy=proxy,
    )
    try:
        results = {}
        for name, url, maximum in (
            ("download_page", PUBLIC_URL, 2 * 1024 * 1024),
            ("legacy_manifest", f"{PUBLIC_URL}manifest.json", 2 * 1024 * 1024),
            ("bootstrap_index", f"{PUBLIC_URL}public-bootstrap-index.json", 2 * 1024 * 1024),
            ("admin_health", f"{PUBLIC_URL}admin/health/ready", 2 * 1024 * 1024),
        ):
            response = client.get(url, headers={"Accept-Encoding": "identity"})
            if response.status_code != 200 or not 1 <= len(response.content) <= maximum:
                raise ReleaseCommandError("release_public_readback_failed")
            header_version = response.headers.get("x-ecorex-product-version")
            results[name] = {
                "status_code": 200,
                "size_bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "version_present": (
                    store.spec.version.encode() in response.content
                    or header_version == store.spec.version
                ),
                "version_header": header_version,
            }
        if not all(item["version_present"] for item in results.values()):
            raise ReleaseCommandError("release_public_version_mismatch")
        return results
    finally:
        client.close()


def _online_update(store: ReleaseRunStore) -> dict[str, Any]:
    if "online-update" in store.read()["steps"]:
        return store.receipt("online-update")
    notification = _step_digest(store, "update-notification")
    title = f"manual-online-update-v{store.spec.version}-{notification}"
    inputs = {
        "source_sha": store.spec.commit,
        "version": store.spec.version,
        "from_version": store.spec.from_version,
        "notification_sha256": notification,
    }
    artifacts = []
    with GitHubActions() as github:
        run = github.ensure_run(
            "ecorex-v1-online-update.yml",
            title=title,
            commit=store.spec.commit,
            inputs=inputs,
        )
        for target in ("windows-x64", "macos-arm64", "macos-x64"):
            name = f"ecorex-v{store.spec.version}-online-update-{target}"
            archive = store.root / "online-update" / f"{name}.zip"
            item = github.download(int(run["run_id"]), name, archive)
            extracted = store.root / "online-update" / name
            if not extracted.exists():
                _safe_extract(archive, extracted)
            receipts = list(extracted.glob("*.json"))
            if len(receipts) != 1:
                raise ReleaseCommandError("online_update_receipt_invalid")
            value = _json_file(receipts[0])
            if (
                value.get("status") != "passed"
                or value.get("version") != store.spec.version
                or value.get("from_version") != store.spec.from_version
                or value.get("target") != target
                or value.get("automatic_browser_open") is not True
                or value.get("notification_observed") is not True
                or value.get("online_bootstrap_executed") is not True
            ):
                raise ReleaseCommandError("online_update_receipt_invalid")
            artifacts.append({**item, "receipt_sha256": sha256_file(receipts[0])})
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "workflow": run,
        "artifacts": artifacts,
        "public_readback": _public_readback(store),
        "completed_at": _now(),
    }
    store.complete("online-update", receipt)
    return receipt


def _compensate_activation(store: ReleaseRunStore, *, reason: str) -> None:
    if "cloud-activation" not in store.read()["steps"]:
        return
    publication = store.receipt("github-draft")
    deployment = publication["deployment"]
    root = _remote_root(store)
    python = f"{root}/cloud/venv/bin/python"
    spec = _json_file(store.root / "prepared" / "production-cloud-spec.json")
    confirmation = str(spec["target_machine_id_sha256"])
    with ProductionRemote() as remote:
        if "update-notification" in store.read()["steps"]:
            notification = store.receipt("update-notification")
            pointers = notification.get("previous_pointers")
            if isinstance(pointers, Mapping):
                _restore_pointer(remote, pointers["bootstrap"], python)
                _restore_pointer(remote, pointers["legacy"], python)
        if "site-activation" in store.read()["steps"]:
            prior = store.receipt("site-activation").get("previous_release_id")
            if isinstance(prior, str) and prior:
                remote.run(
                    (
                        python,
                        "-m",
                        "ecorex.deployment.public_site",
                        "--release-id",
                        prior,
                        "--reactivate",
                        "--confirm-target",
                        PUBLIC_URL,
                    )
                )
        remote.run(
            (
                python,
                "-m",
                "ecorex.deployment.cloud_sidecar",
                "--spec",
                f"{root}/production-cloud-spec.json",
                "--rollback",
                "--confirm-target",
                confirmation,
            ),
            timeout=60 * 60,
        )
    _atomic_json(
        store.root / "recovery" / f"compensated-{secrets.token_hex(8)}.json",
        {
            "schema_version": 1,
            "status": "compensated",
            "run_id": store.spec.run_id,
            "reason": reason,
            "completed_at": _now(),
        },
    )


def _finalize(store: ReleaseRunStore) -> None:
    value = store.read()
    if not set(PREPARE_STEPS).issubset(value["steps"]):
        raise ReleaseCommandError("release_prepare_incomplete")
    _interactive_confirmation(confirmation_phrase(store.spec))
    _github_release(store)
    _remote_activation(store)
    try:
        _activate_update_notification(store)
        _online_update(store)
    except Exception:
        _compensate_activation(store, reason="finalize_failed")
        raise
    if store.read()["status"] != "awaiting-browser-verification":
        raise ReleaseCommandError("codex_browser_verification_not_ready")


def _rollback_notification(store: ReleaseRunStore) -> None:
    if "update-notification" not in store.read()["steps"]:
        raise ReleaseCommandError("release_notification_not_active")
    _interactive_confirmation(
        f"ROLLBACK v{store.spec.version}@{store.spec.commit[:8]} UPDATE NOTIFICATION"
    )
    _compensate_activation(store, reason="operator_requested")
    receipt = {
        "schema_version": 1,
        "status": "rolled-back",
        "run_id": store.spec.run_id,
        "notification_sha256": _step_digest(store, "update-notification"),
        "completed_at": _now(),
    }
    _atomic_json(store.root / "receipts" / f"rollback-notification-{secrets.token_hex(8)}.json", receipt)
    _print(receipt)


def _verify_browser(arguments: argparse.Namespace, store: ReleaseRunStore) -> int:
    if (arguments.browser_receipt is None) != (arguments.evidence_root is None):
        raise ReleaseCommandError("codex_browser_evidence_arguments_incomplete")
    try:
        public_readback = _public_readback(store)
        request = browser_request(store)
        if arguments.browser_receipt is None:
            _print(
                {
                    "public_readback": public_readback,
                    "browser_automation_request": request,
                }
            )
            return 0
        receipt = validate_codex_browser_receipt(
            _json_file(arguments.browser_receipt),
            spec=store.spec,
            run_id=store.spec.run_id,
            nonce=str(request["nonce"]),
            evidence_root=arguments.evidence_root,
        )
    except Exception:
        if store.read()["status"] == "awaiting-browser-verification":
            _compensate_activation(store, reason="codex_browser_verification_failed")
        raise
    store.complete("codex-browser", receipt)
    _print(_public_status(store))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            spec = ReleaseSpec(
                version=arguments.version,
                commit=arguments.commit,
                channel=arguments.channel,
                from_version=arguments.from_version,
            )
            store = ReleaseRunStore(RUN_ROOT, spec)
            store.create()
            _prepare(store)
        else:
            store = _store(arguments.run_id)
            if arguments.command == "status":
                _print(_public_status(store))
                return 0
            if arguments.command == "verify-online":
                return _verify_browser(arguments, store)
            if arguments.command == "finalize":
                _finalize(store)
            else:
                _rollback_notification(store)
        _print(_public_status(store))
        return 0
    except ManualReleaseError as error:
        print(json.dumps({"ok": False, "code": error.code}, sort_keys=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"ok":false,"code":"release_interrupted"}', file=sys.stderr)
        return 130
    except Exception:
        print('{"ok":false,"code":"manual_release_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
