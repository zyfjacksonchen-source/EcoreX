"""Run a zero-publication Windows x64 signed-candidate ceremony locally.

This is an operations drill, not a second release implementation.  It composes
the production ReleaseBuilder, LocalSourceFetcher, InstallCoordinator and
BootstrapSupervisor against a disposable install root.  The Ed25519 private
keys live only as process-local objects; only a redacted evidence report may be
copied out of the temporary directory.
"""

# The script must also work when invoked by absolute path outside an installed
# checkout, so it deliberately establishes the repository import root before
# loading product modules below.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import ast
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import http.client
import importlib.metadata
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Iterable, Mapping, Sequence
import zipfile

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ecorex import __version__
from ecorex.bootstrap import (
    BootstrapReason,
    BootstrapRunResult,
    BootstrapSupervisor,
    RuntimeEndpoint,
)
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    PublicBootstrapIndexError,
    ReleaseBuildSpec,
    ReleaseBuilder,
    WebBundleBuildInput,
    load_dependency_lock_manifest,
    stable_pointer_sequence,
)
from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.integration.pack_python import (
    PackPythonError,
    build_pack_python_manifest,
    resolve_pack_python,
)
from ecorex.integration.windows_sandbox_security import WindowsSandboxSlotSecurity
from ecorex.migration import (
    PRODUCT_MIGRATION_RECEIPT_NAME,
    ProductLegacyMigrationCoordinator,
    TARGET_DATABASE_NAME,
    inventory_source,
    write_product_migration_plan,
)
from ecorex.migration.legacy import (
    CONVERSATION_CANDIDATES,
    V030_RELEASE_SCHEMA_COMMIT,
    discover_existing,
    read_conversations,
)
from ecorex.release.candidate import PACK_SERVICES, PACK_TOOLS
from ecorex.release.process_boundary import BoundedProcessError, run_bounded_process
from ecorex.server import ProductRuntimeConfig, WebBundleManifest
from ecorex.runtime.storage_migrations import (
    STORAGE_MIGRATION_FILE_NAME,
    STORAGE_MIGRATION_SCHEMA_VERSION,
    StorageMigrationManifest,
    current_storage_schema_sha256,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    FetchError,
    InstallCoordinator,
    InstallState,
    LocalSourceFetcher,
    ReleaseChannel,
    ReleaseSource,
    SlotPointers,
    SlotStore,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
)


REPORT_SCHEMA_VERSION = 3
TARGET_PLATFORM = "windows"
TARGET_ARCHITECTURE = "x64"
SIGNING_KEY_ID = "ecorex-local-candidate-drill"
ROLLBACK_KEY_ID = "ecorex-local-rollback-drill"
SESSION_KEY_ID = "ecorex-local-session-drill"
CORE_ARTIFACT_ID = "core-windows-x64"
DEFAULT_TIMEOUT_SECONDS = 5_400.0
_MIN_TIMEOUT_SECONDS = 45.0
_MAX_TIMEOUT_SECONDS = 5_400.0
_PLATFORM_STAGE_TIMEOUT_SECONDS = 50 * 60.0
_RUNTIME_READY_TIMEOUT_SECONDS = 15 * 60.0
_LIVE_ACCEPTANCE_TIMEOUT_SECONDS = 3 * 60.0
_MAX_LIVE_ACCEPTANCE_EVIDENCE_BYTES = 256 * 1024
_DRILL_LOOPBACK_PORT_MIN = 20_000
_DRILL_LOOPBACK_PORT_MAX = 29_999
_DRILL_LOOPBACK_PORT_ATTEMPTS = 256
_FAULT_ENTRYPOINT_MEMBER = "ecorex/server/__main__.py"
_FAULT_ENTRYPOINT_PAYLOAD = b"raise SystemExit(70)\n"
_MAX_FAULT_ARCHIVE_MEMBERS = 50_000
_MAX_FAULT_ARCHIVE_BYTES = 1024 * 1024 * 1024
_SAFE_STAGE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_RUNTIME_DEPENDENCY_GROUP = "dependencies"
_NON_RUNTIME_PARTS = frozenset(
    {"test", "tests", "testing", "example", "examples", "benchmark", "benchmarks"}
)
_WINDOWS_STAGE_KEYS = (
    "core",
    "bootstrap",
    "browser",
    "channels",
    "image",
    "ocr",
    "office",
    "sandbox",
)
_PRODUCTION_TARGETS = (
    ("windows", "x64"),
    ("macos", "arm64"),
    ("macos", "x64"),
)
_V030_RELEASE_SCHEMA_PATHS = (
    "agent/memory/conversation_store.py",
    "agent/protocol/run_ledger.py",
    "agent/protocol/run_event_ledger.py",
)
_V0292_RELEASE_TAG = "v0.2.9.2"
_V0292_RELEASE_SCHEMA_COMMIT = "b52999b07a753e103a993a4da9d3c83c3f366e71"
_V0292_RELEASE_SCHEMA_PATHS = (
    "agent/memory/conversation_store.py",
    "agent/protocol/run_ledger.py",
)
_SUPPORTED_LEGACY_SOURCE_VERSIONS = ("0.2.9.2", "0.3.0")
class DrillError(RuntimeError):
    """A redaction-safe local ceremony failure."""


@dataclass(slots=True)
class Deadline:
    expires_at: float
    stage: str = "initializing"

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        return cls(time.monotonic() + seconds)

    def remaining(self, *, minimum: float = 0.05) -> float:
        value = self.expires_at - time.monotonic()
        if value <= 0:
            raise DrillError(
                f"the signed-candidate drill exceeded its deadline during {self.stage}"
            )
        return max(minimum, value)

    def check(self) -> None:
        self.remaining()

    def enter(self, stage: str) -> None:
        self.stage = stage
        self.check()

    def bounded(self, seconds: float) -> "Deadline":
        """Return a phase deadline that can never outlive the ceremony."""

        if seconds <= 0:
            raise ValueError("bounded deadline seconds must be positive")
        self.check()
        return Deadline(
            min(self.expires_at, time.monotonic() + seconds),
            stage=self.stage,
        )


@dataclass(frozen=True, slots=True)
class RuntimeRun:
    result: BootstrapRunResult
    probe: "RuntimeProbe"
    live_acceptance: Mapping[str, Any] | None = None

    @property
    def bootstrap_status(self) -> int:
        return self.probe.bootstrap_status


@dataclass(slots=True)
class _LoopbackPortLease:
    listener: Any
    port: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.listener.close()


class _LeaseReleasingRuntimeLauncher:
    """Hold the drill port through verification and release at process launch."""

    def __init__(self, lease: _LoopbackPortLease) -> None:
        from ecorex.bootstrap import SubprocessRuntimeLauncher

        self.lease = lease
        self.delegate = SubprocessRuntimeLauncher()

    def start(self, spec: Any) -> Any:
        # Slot and Pack verification can outlive an ephemeral-port lease. Keep
        # this explicit non-ephemeral port bound through those checks, then
        # release immediately before the delegated process spawn.
        self.lease.release()
        return self.delegate.start(spec)


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    bootstrap_status: int
    index_cache_control: str | None
    asset_path: str | None
    asset_cache_control: str | None
    asset_etag: str | None


@dataclass(frozen=True, slots=True)
class LiveRuntimeAcceptanceContext:
    """Public, redacted identity for one verified installed Runtime window."""

    base_url: str
    source_commit: str
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str
    slot_id: str


LiveRuntimeAcceptance = Callable[
    [LiveRuntimeAcceptanceContext, Deadline], Mapping[str, Any]
]


@dataclass(frozen=True, slots=True)
class WindowsStage:
    root: Path
    commit_sha: str
    receipts: tuple[dict[str, Any], ...]
    receipt_sha256: Mapping[str, str]
    native_build_receipt: Mapping[str, Any]
    helper_sha256: str
    go_version: str
    worktree_dirty: bool

    @property
    def stages(self) -> Path:
        return self.root / "stages" / "windows-x64"

    @property
    def core(self) -> Path:
        return self.stages / "core"

    @property
    def bootstrap(self) -> Path:
        return self.stages / "bootstrap"

    def pack(self, pack_id: str) -> Path:
        return self.stages / "packs" / pack_id


class RecordingFailoverFetcher:
    """A local replica fetcher with deterministic partial-transfer failure."""

    def __init__(
        self,
        source_directories: Mapping[str, Path],
        *,
        fail_once: Iterable[str] = (),
        partial_bytes: int = 0,
    ) -> None:
        self._delegate = LocalSourceFetcher(source_directories)
        self._source_directories = dict(source_directories)
        self._fail_once = set(fail_once)
        self._partial_bytes = partial_bytes
        self.attempts: list[dict[str, Any]] = []

    def fetch(
        self,
        source: ReleaseSource,
        artifact: Any,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        self.attempts.append(
            {
                "source_id": source.source_id,
                "artifact_id": artifact.artifact_id,
                "resume_from": resume_from,
            }
        )
        if source.source_id in self._fail_once:
            self._fail_once.remove(source.source_id)
            if self._partial_bytes > 0:
                source_path = (
                    self._source_directories[source.source_id] / artifact.file_name
                )
                mode = "ab" if resume_from else "wb"
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (
                    source_path.open("rb") as incoming,
                    destination.open(mode) as outgoing,
                ):
                    incoming.seek(resume_from)
                    remaining = min(
                        self._partial_bytes,
                        max(0, artifact.size_bytes - resume_from),
                    )
                    while remaining:
                        chunk = incoming.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            break
                        outgoing.write(chunk)
                        remaining -= len(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
            raise FetchError(f"injected local replica outage: {source.source_id}")
        self._delegate.fetch(
            source,
            artifact,
            destination,
            resume_from=resume_from,
            max_bytes=max_bytes,
        )


class CheckpointingDrainer:
    """A deterministic long-job drain seam with a durable checkpoint receipt."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def __call__(self) -> bool:
        sequence = len(self.calls) + 1
        receipt = {
            "schema_version": 1,
            "state": "checkpointed",
            "job_id": "local-drill-long-job",
            "checkpoint_sequence": sequence,
            "new_work_admission": "stopped",
            "inflight_mutation": "none",
        }
        destination = self.root / f"drain-{sequence:02d}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, destination)
        receipt["receipt_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        self.calls.append(receipt)
        return True


def _cleanup_temporary_sandbox_domain(
    slots: SlotStore,
    security: WindowsSandboxSlotSecurity,
) -> None:
    """Remove temporary slot grants and the final shared AppContainer domain."""

    pointers = slots.pointers()
    current = pointers.current
    if current is None:
        slots.write_pointers(SlotPointers())
        return
    others = tuple(
        slot_id
        for slot_id in dict.fromkeys((*pointers.known_good, pointers.previous))
        if slot_id is not None and slot_id != current
    )
    for slot_id in others:
        manifest = slots.release_manifest(slot_id)
        marker = slots.marker(slot_id)
        security_marker = marker.get("security_provision")
        if not isinstance(security_marker, Mapping):
            raise DrillError("a retained temporary slot has no sandbox receipt")
        security.cleanup_slot(
            slots.slot_path(slot_id),
            manifest,
            manifest.artifact(CORE_ARTIFACT_ID),
            security_marker,
        )
    slots.write_pointers(
        SlotPointers(current=current, previous=None, known_good=(current,))
    )
    current_manifest = slots.release_manifest(current)
    current_marker = slots.marker(current).get("security_provision")
    if not isinstance(current_marker, Mapping):
        raise DrillError("the active temporary slot has no sandbox receipt")
    security.cleanup_slot(
        slots.slot_path(current),
        current_manifest,
        current_manifest.artifact(CORE_ARTIFACT_ID),
        current_marker,
    )
    slots.write_pointers(SlotPointers())


def _best_effort_failed_security_cleanup(temporary: Path) -> bool:
    """Converge drill-owned AppContainer grants before deleting a failed root."""

    if os.name != "nt":
        return True
    install_root = temporary / "install"
    helper = install_root / "bootstrap" / "bin" / "ecorex-sandbox-host.exe"
    slots_root = install_root / "slots"
    if not helper.is_file() or not slots_root.is_dir():
        return True
    try:
        security = WindowsSandboxSlotSecurity(
            install_root,
            helper,
            expected_helper_sha256=_sha256_file(helper),
        )
        slots = SlotStore(install_root)
        for path in sorted(slots.slots_dir.iterdir(), key=lambda item: item.name):
            if path.is_dir() and path.name.startswith("."):
                security.cleanup_abandoned(path)
        completed = tuple(
            path.name
            for path in sorted(slots.slots_dir.iterdir(), key=lambda item: item.name)
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / ".slot.json").is_file()
        )
        if completed:
            current = slots.pointers().current
            if current not in completed:
                current = completed[-1]
            known_good = tuple(
                dict.fromkeys(
                    (current, *(item for item in completed if item != current))
                )
            )[:3]
            previous = next((item for item in known_good if item != current), None)
            slots.write_pointers(
                SlotPointers(
                    current=current,
                    previous=previous,
                    known_good=known_good,
                )
            )
            _cleanup_temporary_sandbox_domain(slots, security)
        return True
    except Exception:
        return False


def _repo_root() -> Path:
    return _SCRIPT_REPO_ROOT


def _require_host() -> None:
    machine = platform.machine().casefold()
    if os.name != "nt" or machine not in {"amd64", "x86_64"}:
        raise DrillError("this ceremony requires a native Windows x64 host")
    if sys.version_info[:2] != (3, 11):
        raise DrillError("this ceremony requires the product Python 3.11 toolchain")


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _private_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _sources() -> tuple[ReleaseSource, ...]:
    # These reserved hosts are signed identity material only.  The drill injects
    # LocalSourceFetcher and therefore never opens any of these URLs.
    return (
        ReleaseSource(
            "github-cn",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            f"https://mirror.local-drill.invalid/ecorex/v{__version__}",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            f"https://github.local-drill.invalid/ecorex/v{__version__}",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            f"https://cdn.local-drill.invalid/ecorex/v{__version__}",
        ),
    )


def _runtime_config(
    release_public: bytes,
    rollback_public: bytes,
    session_public: bytes,
) -> bytes:
    # All live transports point at the loopback discard port.  A no-session
    # Runtime does not invoke model mutations; even background retries cannot
    # leave the machine during this ceremony.
    raw = {
        "schema_version": 1,
        "identity": {
            "version": __version__,
            "platform": TARGET_PLATFORM,
            "architecture": TARGET_ARCHITECTURE,
        },
        "paths": {
            "database": "state/runtime.sqlite3",
            "web_root": "web",
            "web_manifest": "web-manifest.json",
            "workspace_roots": ["workspace"],
        },
        "release_public_keys": {
            SIGNING_KEY_ID: base64.b64encode(release_public).decode("ascii")
        },
        "rollback_public_keys": {
            ROLLBACK_KEY_ID: base64.b64encode(rollback_public).decode("ascii")
        },
        "session_public_keys": {
            SESSION_KEY_ID: base64.b64encode(session_public).decode("ascii")
        },
        "gateway": {
            "endpoint": "https://localhost/v1/responses",
            "allowed_hosts": ["localhost"],
        },
        "device_authorization": {
            "base_url": "https://localhost",
            "allowed_hosts": ["localhost"],
            "client_id": "ecorex-local-drill",
            "timeout_seconds": 2,
            "supervisor_poll_seconds": 1,
        },
        "update": {
            "release_feed_endpoint": "https://localhost/api/v1/releases/latest",
            "signal_endpoint": "wss://localhost/api/v1/client/updates/ws",
            "control_plane_hosts": ["localhost"],
            "artifact_hosts": ["localhost"],
            "channel": "stable",
            "poll_interval_seconds": 300,
        },
        "share": None,
        "image_orchestration": None,
        "audit": None,
        "tracing": None,
        "connectors": None,
        "capability_packs": [],
    }
    payload = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ProductRuntimeConfig.from_bytes(payload)
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_native_helper_sha256(
    output: Path,
    native_receipt: Mapping[str, Any],
) -> str:
    core_helper = output / "stages/windows-x64/core/bin/ecorex-sandbox-host.exe"
    bootstrap_helper = (
        output / "stages/windows-x64/bootstrap/bin/ecorex-sandbox-host.exe"
    )
    helper_sha256 = _sha256_file(core_helper)
    if _sha256_file(bootstrap_helper) != helper_sha256:
        raise DrillError("Bootstrap and Runtime sandbox helpers differ")
    digest_fields = (
        "toolchain_manifest_sha256",
        "source_set_sha256",
        "msvc_root_sha256",
        "windows_sdk_root_sha256",
        "include_roots_sha256",
        "library_roots_sha256",
        "library_set_sha256",
        "compiler_sha256",
        "linker_sha256",
        "c1xx_sha256",
        "c2_sha256",
        "runtime_launcher_sha256",
        "sandbox_helper_sha256",
    )
    if (
        native_receipt.get("schema_version") != 2
        or native_receipt.get("status") != "passed"
        or native_receipt.get("target") != "windows-x64"
        or native_receipt.get("authority_mode") != "caller-pinned"
        or any(
            not isinstance(native_receipt.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", native_receipt[field]) is None
            for field in digest_fields
        )
        or native_receipt.get("sandbox_helper_sha256") != helper_sha256
    ):
        raise DrillError("the caller-pinned native build receipt is invalid")
    return helper_sha256


def _git_head(repo: Path, deadline: Deadline) -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(10.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DrillError("the repository commit identity is unavailable") from exc
    value = result.stdout.decode("ascii", errors="ignore").strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise DrillError("the repository commit identity is invalid")
    return value


def _git_worktree_dirty(repo: Path, deadline: Deadline) -> bool:
    try:
        result = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=normal"),
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(20.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DrillError("the repository worktree state is unavailable") from exc
    if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
        raise DrillError("the repository worktree state is invalid")
    return bool(result.stdout)


def _resolve_go(deadline: Deadline) -> tuple[Path, str]:
    explicit = os.environ.get("ECOREX_DRILL_GO_EXECUTABLE")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    discovered = shutil.which("go")
    if discovered:
        candidates.append(Path(discovered))
    temporary = os.environ.get("TEMP")
    if temporary:
        candidates.append(
            Path(temporary) / "ecorex-go1.26.5-runtime" / "go" / "bin" / "go.exe"
        )
    for candidate in candidates:
        try:
            executable = candidate.resolve(strict=True)
            if not executable.is_file():
                continue
            result = subprocess.run(
                (str(executable), "version"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(30.0, deadline.remaining()),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = result.stdout.decode("ascii", errors="ignore").strip()
        if result.returncode == 0 and version == "go version go1.26.5 windows/amd64":
            return executable, version
    raise DrillError(
        "Go 1.26.5 is required for the dependency-free Bootstrap; set "
        "ECOREX_DRILL_GO_EXECUTABLE"
    )


def _platform_stage_failure_code(output: Path, stderr: bytes) -> str:
    """Return only the stager's bounded public failure code."""

    candidates: list[Any] = []
    try:
        candidates.append(
            json.loads((output / "stage-failure.json").read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        pass
    try:
        text = stderr.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    for line in reversed(text.splitlines()):
        try:
            candidates.append(json.loads(line))
        except (json.JSONDecodeError, RecursionError):
            continue
    for value in candidates:
        code = value.get("code") if isinstance(value, Mapping) else None
        if isinstance(code, str) and _SAFE_STAGE_FAILURE_CODE.fullmatch(code):
            return code
    return "platform_stage_failed"


def _stage_windows(
    repo: Path,
    root: Path,
    *,
    release_public: bytes,
    rollback_public: bytes,
    session_public: bytes,
    publication_public: bytes,
    deadline: Deadline,
) -> WindowsStage:
    """Run the source-pinned production Windows stager in a disposable root."""

    commit = _git_head(repo, deadline)
    worktree_dirty = _git_worktree_dirty(repo, deadline)
    go, go_version = _resolve_go(deadline)
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    runtime_config = inputs / "runtime-config.json"
    runtime_config.write_bytes(
        _runtime_config(release_public, rollback_public, session_public)
    )
    output = root / "output"
    environment = dict(os.environ)
    environment.update(
        {
            "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE": str(runtime_config),
            "ECOREX_STAGE_RUNTIME_CONFIG_TEMPLATE_SHA256": _sha256_file(runtime_config),
            "ECOREX_PUBLIC_BOOTSTRAP_INDEX_URL": (
                "https://localhost/public-bootstrap-index.json"
            ),
            "ECOREX_PUBLICATION_PUBLIC_KEYS_JSON": json.dumps(
                {
                    "ecorex-local-publication-drill": base64.b64encode(
                        publication_public
                    ).decode("ascii")
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "PATH": str(go.parent) + os.pathsep + environment.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    stager = repo / "platform-staging" / "stager.py"
    environment.update(
        {
            "ECOREX_PLATFORM_STAGER_EXECUTABLE": str(Path(sys.executable).resolve()),
            "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256": _sha256_file(
                Path(sys.executable).resolve()
            ),
            "ECOREX_PLATFORM_STAGER_ADAPTER": str(stager.resolve(strict=True)),
            "ECOREX_PLATFORM_STAGER_ADAPTER_SHA256": _sha256_file(stager),
            "ECOREX_STAGE_WEB_DIST": str(
                (repo / "desktop" / "dist").resolve(strict=True)
            ),
        }
    )
    command = (
        sys.executable,
        str(repo / "scripts" / "invoke-v1-platform-stager.py"),
        "--repo-root",
        str(repo),
        "--output-root",
        str(output),
        "--platform",
        TARGET_PLATFORM,
        "--architecture",
        TARGET_ARCHITECTURE,
        "--commit-sha",
        commit,
        "--workflow-run-id",
        "1",
        "--workflow-run-attempt",
        "1",
    )
    stage_deadline = deadline.bounded(_PLATFORM_STAGE_TIMEOUT_SECONDS)
    try:
        result = run_bounded_process(
            command,
            payload=None,
            cwd=repo,
            environment=environment,
            timeout_seconds=stage_deadline.remaining(),
            max_stdout_bytes=16 * 1024,
            max_stderr_bytes=16 * 1024,
        )
    except (OSError, BoundedProcessError) as exc:
        raise DrillError(
            "the source-pinned Windows platform stage did not finish"
        ) from exc
    if (
        result.returncode != 0
        or len(result.stdout) > 16 * 1024
        or len(result.stderr) > 16 * 1024
    ):
        code = _platform_stage_failure_code(output, result.stderr)
        raise DrillError(
            "the source-pinned Windows platform stage failed closed: " + code
        )
    receipts: list[dict[str, Any]] = []
    receipt_digests: dict[str, str] = {}
    for key in _WINDOWS_STAGE_KEYS:
        path = output / "receipts" / "windows-x64" / f"{key}.json"
        try:
            payload = path.read_bytes()
            value = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DrillError(f"the Windows {key} stage receipt is unavailable") from exc
        if (
            not isinstance(value, dict)
            or value.get("receipt_type") != "ecorex-candidate-stage"
            or value.get("stage_id") != f"{key}-windows-x64"
            or value.get("commit_sha") != commit
            or not isinstance(value.get("producer"), Mapping)
            or value["producer"].get("workflow_run_id") != 1
            or value["producer"].get("workflow_run_attempt") != 1
        ):
            raise DrillError(f"the Windows {key} stage receipt is invalid")
        receipts.append(value)
        receipt_digests[key] = hashlib.sha256(payload).hexdigest()
    native_receipt_path = (
        output
        / ".evidence"
        / "windows-x64"
        / "native"
        / "output"
        / "native-build-receipt.json"
    )
    try:
        native_receipt = json.loads(native_receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillError(
            "the caller-pinned native build receipt is unavailable"
        ) from exc
    if not isinstance(native_receipt, dict):
        raise DrillError("the caller-pinned native build receipt is invalid")
    helper_sha256 = _validated_native_helper_sha256(output, native_receipt)
    return WindowsStage(
        root=output,
        commit_sha=commit,
        receipts=tuple(receipts),
        receipt_sha256=receipt_digests,
        native_build_receipt=native_receipt,
        helper_sha256=helper_sha256,
        go_version=go_version,
        worktree_dirty=worktree_dirty,
    )


def _released_ddl(source: bytes) -> str:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise DrillError("the legacy released schema source is invalid") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_DDL"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise DrillError("the legacy released schema has no literal DDL authority")


def _released_schema_sources(
    repo: Path,
    *,
    revision: str,
    expected_commit: str,
    paths: Sequence[str],
    deadline: Deadline,
) -> dict[str, bytes]:
    try:
        resolved = subprocess.run(
            ("git", "rev-parse", f"{revision}^{{commit}}"),
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(15.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DrillError("the legacy release schema revision is unavailable") from exc
    if (
        resolved.returncode != 0
        or resolved.stdout.decode("ascii", errors="ignore").strip() != expected_commit
    ):
        raise DrillError("the legacy release schema revision is not pinned")
    try:
        archive = subprocess.run(
            ("git", "archive", "--format=tar", expected_commit, *paths),
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(30.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DrillError("the legacy release schema archive is unavailable") from exc
    if archive.returncode != 0 or not 1 <= len(archive.stdout) <= 8 * 1024 * 1024:
        raise DrillError("the legacy release schema archive is unavailable")
    sources: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                if member.isdir():
                    continue
                if member.name not in paths or not member.isfile():
                    raise DrillError(
                        "the legacy release schema archive has an unexpected member"
                    )
                stream = bundle.extractfile(member)
                if stream is None:
                    raise DrillError("the legacy release schema member is unavailable")
                sources[member.name] = stream.read()
    except (tarfile.TarError, OSError) as exc:
        raise DrillError("the legacy release schema archive is invalid") from exc
    if set(sources) != set(paths):
        raise DrillError("the legacy release schema archive is incomplete")
    return sources


def _create_released_v0292_fixture(
    repo: Path,
    destination: Path,
    *,
    deadline: Deadline,
) -> dict[str, Any]:
    """Create a deletion-sensitive fixture from the exact v0.2.9.2 tag DDL."""

    sources = _released_schema_sources(
        repo,
        revision=_V0292_RELEASE_TAG,
        expected_commit=_V0292_RELEASE_SCHEMA_COMMIT,
        paths=_V0292_RELEASE_SCHEMA_PATHS,
        deadline=deadline,
    )
    database = destination / "sessions" / "conversations.db"
    database.parent.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(database)
    try:
        for path in _V0292_RELEASE_SCHEMA_PATHS:
            connection.executescript(_released_ddl(sources[path]))
        connection.executemany(
            """
            INSERT INTO sessions(
                session_id, channel_type, title, title_locked,
                context_start_seq, created_at, last_active, msg_count
            ) VALUES (?, 'web', ?, ?, 0, ?, ?, ?)
            """,
            (
                (
                    "release-drill-session",
                    "v0.2.9.2 live conversation",
                    1,
                    1_700_000_000,
                    1_700_000_002,
                    2,
                ),
                (
                    "summary-session",
                    "",
                    0,
                    1_700_000_200,
                    1_700_000_201,
                    0,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO messages(session_id, seq, role, content, created_at, extras)
            VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (
                (
                    "release-drill-session",
                    0,
                    "user",
                    "preserve the authoritative legacy conversation",
                    1_700_000_001,
                ),
                (
                    "release-drill-session",
                    1,
                    "assistant",
                    "authoritative conversation preserved",
                    1_700_000_002,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                request_id, session_id, parent_id, run_type, status, phase,
                terminal_reason, error_code, error_message, model, provider,
                created_at, started_at, updated_at, terminal_at, metadata_json
            ) VALUES (
                'release-drill-request', 'release-drill-session', NULL, 'message',
                'completed', 'completed', 'completed', NULL, NULL, 'managed-chat',
                'managed', 1700000001, 1700000001, 1700000002, 1700000002, '{}'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    project = destination / "projects" / "legacy-office"
    project.mkdir(parents=True)
    (project / "retained-project-marker.txt").write_text(
        "synthetic project fixture\n", encoding="utf-8", newline="\n"
    )
    ui_state = {
        "sessionTitles": {
            "release-drill-session": "must not override the locked title",
            "summary-session": "v0.2.9.2 retained summary",
            "deleted-cache-only-session": "deleted conversation",
        },
        "pinnedSessions": {"summary-session": True},
        "pinnedSessionTimes": {"summary-session": 1_700_000_202_000},
        "sessionUiState": {
            "deleted-cache-only-session": {
                "title": "deleted conversation",
                "messages": [
                    {"role": "user", "content": "must never be restored"},
                    {"role": "assistant", "content": "must remain deleted"},
                ],
            }
        },
        "projects": [
            {
                "id": "legacy-office-project",
                "name": "Legacy Office Project",
                "path": str(project),
            }
        ],
        "sessionProjects": {
            "release-drill-session": "legacy-office-project",
        },
        "activeProjectId": "legacy-office-project",
        "pinnedProjects": {"legacy-office-project": True},
    }
    ui_path = destination / ".ecorex" / "ui-state.json"
    ui_path.parent.mkdir(parents=True)
    ui_path.write_text(
        json.dumps(ui_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (destination / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "v0.2.5-runtime-manifest-v1",
                "product": "EcoreX",
                "version": "0.2.9.2",
                "sourceCommit": _V0292_RELEASE_SCHEMA_COMMIT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    inventory = inventory_source(destination, source_version="0.2.9.2")
    return {
        "source": destination,
        "source_version": "0.2.9.2",
        "inventory_digest": inventory.digest,
        "inventory_entries": len(inventory.entries),
        "inventory_bytes": inventory.total_bytes,
        "baseline_release_schema_commit": _V0292_RELEASE_SCHEMA_COMMIT,
        "evidence_level": "exact_release_tag_schema_fixture",
        "corpus_mode": "synthetic-release-fixture",
        "source_unchanged": True,
    }


def _create_released_v030_fixture(
    repo: Path,
    destination: Path,
    *,
    deadline: Deadline,
) -> dict[str, Any]:
    try:
        archive = subprocess.run(
            (
                "git",
                "archive",
                "--format=tar",
                V030_RELEASE_SCHEMA_COMMIT,
                *_V030_RELEASE_SCHEMA_PATHS,
            ),
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=min(30.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DrillError("the v0.3 released schema archive is unavailable") from exc
    if archive.returncode != 0 or not 1 <= len(archive.stdout) <= 8 * 1024 * 1024:
        raise DrillError("the v0.3 released schema archive is unavailable")
    sources: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                if member.isdir():
                    continue
                if member.name not in _V030_RELEASE_SCHEMA_PATHS or not member.isfile():
                    raise DrillError(
                        "the v0.3 released schema archive has an unexpected member"
                    )
                stream = bundle.extractfile(member)
                if stream is None:
                    raise DrillError("the v0.3 released schema member is unavailable")
                sources[member.name] = stream.read()
    except (tarfile.TarError, OSError) as exc:
        raise DrillError("the v0.3 released schema archive is invalid") from exc
    if set(sources) != set(_V030_RELEASE_SCHEMA_PATHS):
        raise DrillError("the v0.3 released schema archive is incomplete")
    database = destination / "sessions" / "conversations.db"
    database.parent.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(database)
    try:
        for path in _V030_RELEASE_SCHEMA_PATHS:
            connection.executescript(_released_ddl(sources[path]))
        connection.execute(
            """
            INSERT INTO sessions(
                session_id, channel_type, title, title_locked, context_start_seq,
                project_id, project_name, project_path, project_memory_path,
                project_dreams_path, metadata_json, created_at, last_active, msg_count
            ) VALUES ('release-drill-session', 'web', '真实发布结构迁移', 0, 0,
                      '', '', '', '', '', '{}', 1700000000, 1700000002, 2)
            """
        )
        connection.executemany(
            """
            INSERT INTO messages(session_id, seq, role, content, created_at, extras)
            VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (
                (
                    "release-drill-session",
                    1,
                    "user",
                    "迁移这条真实发布结构消息",
                    1700000001,
                ),
                (
                    "release-drill-session",
                    2,
                    "assistant",
                    "已经迁移",
                    1700000002,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_runs(
                request_id, session_id, parent_id, run_type, status, phase,
                terminal_reason, error_code, error_message, model, provider,
                created_at, started_at, updated_at, terminal_at, lease_owner,
                lease_expires_at, metadata_json
            ) VALUES (
                'release-drill-request', 'release-drill-session', NULL, 'message',
                'completed', 'completed', 'completed', NULL, NULL, 'managed-chat',
                'managed', 1700000001, 1700000001, 1700000002, 1700000002,
                NULL, NULL, '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_run_events(
                request_id, session_id, turn_id, event_seq, event_type,
                payload_json, idempotency_key, source, created_at
            ) VALUES (
                'release-drill-request', 'release-drill-session',
                'release-drill-request', 1, 'run.accepted', '{}',
                'release-drill-request:accepted', 'runtime', 1700000001
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    inventory = inventory_source(destination)
    return {
        "source": destination,
        "inventory_digest": inventory.digest,
        "baseline_release_schema_commit": V030_RELEASE_SCHEMA_COMMIT,
        "evidence_level": "release_schema_compatible_unattested",
    }


def _snapshot_legacy_source(
    source: Path,
    destination: Path,
    *,
    source_version: str,
    deadline: Deadline,
) -> dict[str, Any]:
    """Copy a stable user-selected source without ever writing to that source."""

    before = inventory_source(source, source_version=source_version)
    destination.mkdir(parents=True, exist_ok=False)
    for entry in before.entries:
        deadline.remaining()
        if entry.kind != "file" or entry.relative_path.startswith("@pinned/"):
            raise DrillError("the user-selected legacy source inventory is invalid")
        relative = PurePosixPath(entry.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DrillError("the user-selected legacy source inventory is invalid")
        origin = source.joinpath(*relative.parts)
        try:
            metadata = origin.lstat()
        except OSError as exc:
            raise DrillError(
                "the user-selected legacy source changed during snapshot"
            ) from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse)
        ):
            raise DrillError("the user-selected legacy source contains an unsafe entry")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(origin, target, follow_symlinks=False)
        except OSError as exc:
            raise DrillError("the user-selected legacy source snapshot failed") from exc
        if _sha256_file(target) != entry.sha256:
            raise DrillError("the user-selected legacy source changed during snapshot")
    after = inventory_source(source, source_version=source_version)
    copied = inventory_source(destination, source_version=source_version)
    if before != after or copied.digest != before.digest:
        raise DrillError("the user-selected legacy source changed during snapshot")
    return {
        "source": destination,
        "source_version": source_version,
        "inventory_digest": copied.digest,
        "inventory_entries": len(copied.entries),
        "inventory_bytes": copied.total_bytes,
        "baseline_release_schema_commit": None,
        "evidence_level": "user_selected_readonly_snapshot",
        "corpus_mode": "user-selected-readonly-snapshot",
        "source_unchanged": True,
    }


def _prepare_legacy_source(
    repo: Path,
    temporary: Path,
    *,
    source_version: str,
    user_source: Path | None,
    deadline: Deadline,
) -> dict[str, Any]:
    if source_version not in _SUPPORTED_LEGACY_SOURCE_VERSIONS:
        raise DrillError("the selected legacy source version is unsupported")
    destination = temporary / f"legacy-{source_version.replace('.', '')}-snapshot"
    if user_source is not None:
        return _snapshot_legacy_source(
            user_source,
            destination,
            source_version=source_version,
            deadline=deadline,
        )
    if source_version == "0.2.9.2":
        return _create_released_v0292_fixture(
            repo,
            destination,
            deadline=deadline,
        )
    legacy = _create_released_v030_fixture(
        repo,
        destination,
        deadline=deadline,
    )
    inventory = inventory_source(destination, source_version=source_version)
    return {
        **legacy,
        "source_version": source_version,
        "inventory_entries": len(inventory.entries),
        "inventory_bytes": inventory.total_bytes,
        "corpus_mode": "synthetic-release-fixture",
        "source_unchanged": True,
    }


def _cache_only_session_ids(source: Path) -> set[str]:
    ui_path = source / ".ecorex" / "ui-state.json"
    if not ui_path.is_file() or ui_path.is_symlink():
        return set()
    try:
        if ui_path.stat().st_size > 16 * 1024 * 1024:
            raise DrillError("the legacy UI state exceeds the activation gate bound")
        ui_state = json.loads(ui_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillError("the legacy UI state cannot be verified") from exc
    if not isinstance(ui_state, Mapping):
        raise DrillError("the legacy UI state cannot be verified")
    session_titles = ui_state.get("sessionTitles")
    session_titles = session_titles if isinstance(session_titles, Mapping) else {}
    raw_session_state = ui_state.get("sessionUiState") or {}
    try:
        cached_ids = {
            *(str(item) for item in session_titles),
            *(str(item) for item in raw_session_state),
        }
    except TypeError as exc:
        raise DrillError("the legacy UI state cannot be verified") from exc

    # Use the exact same candidate order and released adapter as the product
    # migrator.  A second hand-written SQLite discovery path could disagree
    # with the deletion authority and accidentally turn this gate into a
    # weaker approximation.
    database = discover_existing(source, CONVERSATION_CANDIDATES)
    conversations = read_conversations(database) if database is not None else None
    canonical_ids = {
        str(row["session_id"])
        for row in (conversations.sessions if conversations is not None else ())
    }
    return cached_ids - canonical_ids


def _migration_aggregate_evidence(
    *,
    source: Path,
    database: Path,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    counts = report.get("counts")
    if not isinstance(counts, Mapping):
        raise DrillError("the committed migration report has no aggregate counts")
    cache_only = _cache_only_session_ids(source)
    connection = sqlite3.connect(
        f"file:{database.resolve(strict=True).as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        observed = {
            "threads": int(
                connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            ),
            "messages": int(
                connection.execute(
                    "SELECT COUNT(*) FROM items WHERE kind = 'message'"
                ).fetchone()[0]
            ),
            "session_summaries": int(
                connection.execute(
                    "SELECT COUNT(*) FROM threads WHERE trim(title) <> ''"
                ).fetchone()[0]
            ),
            "projects": int(
                connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            ),
            "project_bindings": int(
                connection.execute(
                    "SELECT COUNT(*) FROM project_thread_bindings"
                ).fetchone()[0]
            ),
        }
        imported_sessions = {
            str(row[0])
            for row in connection.execute(
                "SELECT legacy_id FROM legacy_id_map WHERE entity_kind = 'session'"
            )
        }
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    if integrity != "ok":
        raise DrillError("the migrated v1 database failed integrity verification")
    expected = {
        "threads": int(counts.get("threads") or 0),
        "messages": int(counts.get("messages") or 0),
        "session_summaries": int(counts.get("session_summaries") or 0),
        "projects": int(counts.get("projects") or 0),
        "project_bindings": int(counts.get("project_bindings") or 0),
    }
    if observed != expected:
        raise DrillError("the migrated v1 aggregate counts do not match authority")
    excluded = int(counts.get("deleted_session_cache_excluded") or 0)
    restored = len(cache_only & imported_sessions)
    if excluded != len(cache_only) or restored != 0:
        raise DrillError("a deleted legacy conversation crossed the activation gate")
    return {
        **observed,
        "deleted_session_cache_excluded": excluded,
        "deleted_sessions_restored": restored,
        "database_integrity": "ok",
        "deletion_authority_verified": True,
        "aggregate_only": True,
    }


def _bind_local_bootstrap_minimum(
    bootstrap: Path,
    signer: Ed25519MemorySigner,
) -> dict[str, Any]:
    """Apply the same release-key anti-rollback floor as Candidate assembly."""

    try:
        sequence = stable_pointer_sequence(__version__)
    except PublicBootstrapIndexError:
        raise DrillError("the product version cannot derive a Bootstrap sequence")
    payload = b"\0".join(
        (
            b"ecorex.bootstrap-minimum-stable.v1",
            str(sequence).encode("ascii"),
            __version__.encode("ascii"),
        )
    )
    path = bootstrap / "bootstrap-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillError("the staged Bootstrap configuration is unavailable") from exc
    if not isinstance(value, dict) or value.get("minimum_stable") is not None:
        raise DrillError("the staged Bootstrap anti-rollback floor is not fresh")
    signature = signer.sign(payload)
    try:
        Ed25519PublicKey.from_public_bytes(signer.public_key_bytes).verify(
            signature,
            payload,
        )
    except Exception as exc:
        raise DrillError("the Bootstrap anti-rollback signature is invalid") from exc
    value["minimum_stable"] = {
        "sequence": sequence,
        "version": __version__,
        "signature": {
            "algorithm": "ed25519",
            "key_id": signer.key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "sequence": sequence,
        "version": __version__,
        "config_sha256": _sha256_file(path),
        "signature_algorithm": "ed25519",
        "key_id": signer.key_id,
    }


def _declared_runtime_requirements(repo: Path) -> tuple[Requirement, ...]:
    try:
        raw = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
        values = raw["project"][_RUNTIME_DEPENDENCY_GROUP]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise DrillError("the declared Runtime dependency set is unavailable") from exc
    if not isinstance(values, list) or not values:
        raise DrillError("the declared Runtime dependency set is empty")
    try:
        parsed = tuple(Requirement(value) for value in values)
    except Exception as exc:
        raise DrillError("the declared Runtime dependency set is invalid") from exc
    return parsed


def _runtime_distribution_closure(
    repo: Path,
) -> tuple[importlib.metadata.Distribution, ...]:
    pending = list(_declared_runtime_requirements(repo))
    resolved: dict[str, importlib.metadata.Distribution] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        key = canonicalize_name(requirement.name)
        if key in resolved:
            continue
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise DrillError(
                f"declared Runtime distribution is not installed: {requirement.name}"
            ) from exc
        if requirement.specifier and not requirement.specifier.contains(
            distribution.version, prereleases=True
        ):
            raise DrillError(
                f"installed Runtime distribution violates its version contract: "
                f"{requirement.name}"
            )
        resolved[key] = distribution
        for child in distribution.requires or ():
            try:
                pending.append(Requirement(child))
            except Exception as exc:
                raise DrillError(
                    f"Runtime distribution metadata is invalid: {requirement.name}"
                ) from exc
    return tuple(resolved[key] for key in sorted(resolved))


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _copy_tree_files(
    source: Path,
    destination: Path,
    *,
    deadline: Deadline,
    excluded_roots: frozenset[str] = frozenset(),
) -> None:
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        deadline.check()
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0].casefold() in excluded_roots:
            continue
        if any(part.casefold() in _NON_RUNTIME_PARTS for part in relative.parts):
            continue
        if (
            any(part.casefold() == "__pycache__" for part in relative.parts)
            or path.suffix.casefold() == ".pyc"
        ):
            continue
        if path.suffix.casefold() == ".sh":
            continue
        if path.is_symlink():
            raise DrillError("the local Runtime source contains a symbolic link")
        if path.is_file():
            _copy_file(path, destination / relative)


def _copy_distributions(
    distributions: Iterable[importlib.metadata.Distribution],
    destination: Path,
    *,
    deadline: Deadline,
) -> tuple[dict[str, str], ...]:
    interpreter_roots = tuple(
        dict.fromkeys(
            Path(value).resolve()
            for value in (
                sysconfig.get_paths()["purelib"],
                sysconfig.get_paths()["platlib"],
            )
        )
    )
    records: list[dict[str, str]] = []
    copied: set[str] = set()
    for distribution in distributions:
        deadline.check()
        name = distribution.metadata.get("Name") or "unknown"
        version = distribution.version
        distribution_root = Path(distribution.locate_file("")).resolve()
        roots = tuple(dict.fromkeys((*interpreter_roots, distribution_root)))
        files = distribution.files
        if files is None:
            raise DrillError(f"Runtime distribution has no file inventory: {name}")
        for item in files:
            deadline.check()
            source = Path(distribution.locate_file(item)).resolve()
            relative: Path | None = None
            for root in roots:
                try:
                    relative = source.relative_to(root)
                    break
                except ValueError:
                    continue
            # Console entry points outside site-packages are not used by the
            # packaged Runtime.  Every importable distribution file must remain
            # within purelib/platlib and is copied byte-for-byte.
            if relative is None:
                continue
            relative_key = relative.as_posix().casefold()
            if (
                any(part.casefold() == "__pycache__" for part in relative.parts)
                or source.suffix.casefold() == ".pyc"
                or source.suffix.casefold() == ".sh"
                or any(part.casefold() in _NON_RUNTIME_PARTS for part in relative.parts)
                or relative_key in copied
            ):
                continue
            if not source.is_file() or source.is_symlink():
                continue
            _copy_file(source, destination / relative)
            copied.add(relative_key)
        records.append({"name": str(name), "version": str(version)})
    return tuple(records)


def _assemble_core(
    repo: Path,
    root: Path,
    *,
    release_public: bytes,
    rollback_public: bytes,
    session_public: bytes,
    deadline: Deadline,
) -> tuple[Path, tuple[dict[str, str], ...]]:
    python_root = Path(sys.base_prefix).resolve()
    executable = Path(sys.executable).resolve()
    if executable.parent != python_root:
        raise DrillError("the Python executable is outside its declared base prefix")
    core = root
    binary = core / "bin"
    library = binary / "Lib"
    site_packages = library / "site-packages"
    binary.mkdir(parents=True)

    _copy_file(executable, binary / "ecorex.exe")
    for dll in sorted(python_root.glob("*.dll")):
        deadline.check()
        _copy_file(dll, binary / dll.name)
    _copy_tree_files(
        python_root / "DLLs",
        binary / "DLLs",
        deadline=deadline,
    )
    _copy_tree_files(
        python_root / "Lib",
        library,
        deadline=deadline,
        excluded_roots=frozenset({"site-packages", "__pycache__"}),
    )
    distributions = _runtime_distribution_closure(repo)
    distribution_records = _copy_distributions(
        distributions,
        site_packages,
        deadline=deadline,
    )
    _copy_tree_files(
        repo / "ecorex",
        site_packages / "ecorex",
        deadline=deadline,
    )

    entrypoint = (
        "import sys\n"
        "from ecorex.server.cli import main\n"
        "raise SystemExit(main(['serve', *sys.argv[1:]]))\n"
    )
    (core / "serve").write_text(entrypoint, encoding="utf-8", newline="\n")
    (core / "runtime-config.json").write_bytes(
        _runtime_config(release_public, rollback_public, session_public)
    )
    return core, distribution_records


def _preflight_packaged_python(core: Path, deadline: Deadline) -> None:
    try:
        completed = subprocess.run(
            [str(core / "bin/ecorex.exe"), "-B", "serve", "--help"],
            cwd=core,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(20.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrillError(
            "the packaged Runtime executable preflight did not finish"
        ) from exc
    if completed.returncode != 0 or b"--host" not in completed.stdout:
        raise DrillError("the packaged Runtime executable preflight failed")


def _assert_no_runtime_bytecode(core: Path) -> None:
    for path in core.rglob("*"):
        if path.name.casefold() == "__pycache__" or path.suffix.casefold() == ".pyc":
            raise DrillError("the assembled Runtime contains mutable Python bytecode")


def _fault_candidate_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        name
        for name in names
        if name.casefold() == "__pycache__" or name.casefold().endswith(".pyc")
    }
    if Path(directory).name.casefold() == "lib":
        ignored.update(name for name in names if name.casefold() == "site-packages")
    return ignored


def _inject_fault_runtime_entrypoint(core: Path) -> str:
    """Replace exactly one Runtime entrypoint in a directory or import ZIP."""

    try:
        root = core.resolve(strict=True)
    except OSError:
        raise DrillError(
            "the packaged Runtime module entrypoint is ambiguous"
        ) from None
    disk_matches = tuple(
        path
        for path in root.rglob(_FAULT_ENTRYPOINT_MEMBER)
        if path.is_file() and not path.is_symlink()
    )
    archive_matches: list[Path] = []
    archive_root = root / "bin" / "pack-python"
    for archive_path in sorted(archive_root.glob("python*.zip")):
        matches = _validated_fault_archive_members(archive_path)
        archive_matches.extend(archive_path for _member in matches)
    if len(disk_matches) + len(archive_matches) != 1:
        raise DrillError("the packaged Runtime module entrypoint is ambiguous")
    if disk_matches:
        entrypoint = disk_matches[0]
        try:
            entrypoint.resolve(strict=True).relative_to(root)
            entrypoint.write_bytes(_FAULT_ENTRYPOINT_PAYLOAD)
        except (OSError, ValueError):
            raise DrillError(
                "the packaged Runtime module entrypoint is ambiguous"
            ) from None
        return "directory"
    _rewrite_fault_import_archive(archive_matches[0])
    return "zipimport"


def _validated_fault_archive_members(archive_path: Path) -> tuple[zipfile.ZipInfo, ...]:
    try:
        if archive_path.is_symlink() or not archive_path.is_file():
            raise DrillError("the packaged Runtime import archive is invalid")
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_FAULT_ARCHIVE_MEMBERS:
                raise DrillError("the packaged Runtime import archive is invalid")
            seen: set[str] = set()
            total = 0
            matches: list[zipfile.ZipInfo] = []
            for member in members:
                original = member.filename
                relative = PurePosixPath(original)
                canonical = relative.as_posix()
                expected = f"{canonical}/" if member.is_dir() else canonical
                collision = canonical.casefold()
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                total += member.file_size
                if (
                    not original
                    or "\\" in original
                    or original != expected
                    or relative.is_absolute()
                    or any(
                        part in {"", ".", ".."} or ":" in part
                        for part in relative.parts
                    )
                    or collision in seen
                    or member.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or total > _MAX_FAULT_ARCHIVE_BYTES
                ):
                    raise DrillError("the packaged Runtime import archive is invalid")
                seen.add(collision)
                if original == _FAULT_ENTRYPOINT_MEMBER and not member.is_dir():
                    matches.append(member)
            return tuple(matches)
    except DrillError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise DrillError("the packaged Runtime import archive is invalid") from None


def _rewrite_fault_import_archive(archive_path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.",
        suffix=".fault.tmp",
        dir=archive_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            zipfile.ZipFile(archive_path) as source,
            zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
                strict_timestamps=True,
            ) as destination,
        ):
            replaced = 0
            for member in source.infolist():
                payload = source.read(member)
                if member.filename == _FAULT_ENTRYPOINT_MEMBER:
                    payload = _FAULT_ENTRYPOINT_PAYLOAD
                    replaced += 1
                destination.writestr(member, payload)
        if replaced != 1:
            raise DrillError("the packaged Runtime module entrypoint is ambiguous")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, archive_path)
        if len(_validated_fault_archive_members(archive_path)) != 1:
            raise DrillError("the packaged Runtime module entrypoint is ambiguous")
    except DrillError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise DrillError("the packaged Runtime import archive is invalid") from None
    finally:
        temporary.unlink(missing_ok=True)


def _rebind_fault_pack_python(core: Path) -> None:
    manifest_path = core / "pack-python.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.fault.tmp")
    try:
        payload = build_pack_python_manifest(
            core,
            platform=TARGET_PLATFORM,
            architecture=TARGET_ARCHITECTURE,
        )
        temporary.write_bytes(payload)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
        resolve_pack_python(
            core,
            platform=TARGET_PLATFORM,
            architecture=TARGET_ARCHITECTURE,
        )
    except (OSError, PackPythonError):
        raise DrillError(
            "the fault candidate Pack-Python identity could not be rebound"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _preflight_fault_candidate(core: Path, deadline: Deadline) -> None:
    try:
        completed = subprocess.run(
            [str(core / "bin/ecorex.exe"), "serve"],
            cwd=core,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(15.0, deadline.remaining()),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrillError("the fault candidate preflight did not finish") from exc
    if completed.returncode != 70:
        raise DrillError("the fault candidate did not fail with its bounded exit code")
    _assert_no_runtime_bytecode(core)


def _build_release(
    *,
    signer: Ed25519MemorySigner,
    core: Path,
    bootstrap: Path,
    packs: Mapping[str, Path],
    web_dist: Path,
    destination: Path,
    created_at: datetime,
) -> Any:
    if set(packs) != set(PACK_TOOLS):
        raise DrillError("the local Windows release requires all six product Packs")
    return ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at=created_at.isoformat(),
            sources=_sources(),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=core,
                    kind=ArtifactKind.CORE,
                    platform=TARGET_PLATFORM,
                    architecture=TARGET_ARCHITECTURE,
                    executable_paths=("bin/ecorex.exe",),
                    product_runtime=True,
                ),
                ArtifactBuildInput(
                    source_dir=bootstrap,
                    kind=ArtifactKind.BOOTSTRAP,
                    platform=TARGET_PLATFORM,
                    architecture=TARGET_ARCHITECTURE,
                    executable_paths=("bin/ecorex-bootstrap.exe",),
                ),
                *(
                    ArtifactBuildInput(
                        source_dir=packs[pack_id],
                        kind=ArtifactKind.CAPABILITY_PACK,
                        platform=TARGET_PLATFORM,
                        architecture=TARGET_ARCHITECTURE,
                        executable_paths=("__main__.py",)
                        if pack_id in {"browser", "sandbox"}
                        else (),
                        pack_id=pack_id,
                        pack_tool_ids=tuple(PACK_TOOLS[pack_id]),
                        pack_service_ids=tuple(PACK_SERVICES[pack_id]),
                        runtime_api_version="1.0.0",
                    )
                    for pack_id in sorted(PACK_TOOLS)
                ),
            ),
            web_bundle=WebBundleBuildInput(web_dist),
            dependency_lock_sha256=load_dependency_lock_manifest(
                _SCRIPT_REPO_ROOT / "requirements/locks/manifest.json"
            ).sha256,
        ),
        destination,
    )


def _verify_release(built: Any, verifier: Ed25519SignatureVerifier) -> dict[str, Any]:
    verify_manifest_signature(built.manifest, verifier)
    for artifact in built.manifest.artifacts:
        verify_artifact_file(
            built.artifact_paths[artifact.artifact_id],
            built.manifest,
            artifact,
            verifier,
        )
    metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
    manifest_sha = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    if metadata.get("manifest_sha256") != manifest_sha:
        raise DrillError("release metadata does not match the signed manifest bytes")
    web_payload = built.artifact_paths["web-manifest"].read_bytes()
    web = WebBundleManifest.from_json(web_payload)
    core_path = built.artifact_paths[CORE_ARTIFACT_ID]
    try:
        with zipfile.ZipFile(core_path) as archive:
            storage_payload = archive.read(STORAGE_MIGRATION_FILE_NAME)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise DrillError(
            "product Core is missing its storage migration contract"
        ) from exc
    storage_manifest = StorageMigrationManifest.from_bytes(storage_payload)
    target_schema_sha256 = current_storage_schema_sha256()
    if (
        storage_manifest.schema_version != STORAGE_MIGRATION_SCHEMA_VERSION
        or storage_manifest.target_schema_sha256 != target_schema_sha256
    ):
        raise DrillError("product Core is not bound to the compiled storage schema")
    immutable_files = tuple(record for record in web.files if record.immutable)
    if not immutable_files or any(
        record.sha256[:8] not in Path(record.path).name.casefold()
        for record in immutable_files
    ):
        raise DrillError("Web bundle assets are not content-addressed")
    signature_algorithms = {
        built.manifest.signature.algorithm,
        *(artifact.signature.algorithm for artifact in built.manifest.artifacts),
        web.signature.algorithm,
    }
    if signature_algorithms != {"ed25519"}:
        raise DrillError("release signatures are not exclusively Ed25519")
    artifact_ids = {artifact.artifact_id for artifact in built.manifest.artifacts}
    expected_host_artifacts = {
        CORE_ARTIFACT_ID,
        "bootstrap-windows-x64",
        "web-manifest",
        *(f"capability-pack-{pack_id}-windows-x64" for pack_id in PACK_TOOLS),
        *(f"capability-pack-{pack_id}-windows-x64-manifest" for pack_id in PACK_TOOLS),
    }
    if artifact_ids != expected_host_artifacts:
        raise DrillError("the local Windows release artifact set is incomplete")
    return {
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "build_digest": built.manifest.build_digest,
        "manifest_sha256": manifest_sha,
        "core_sha256": built.manifest.artifact(CORE_ARTIFACT_ID).sha256,
        "web_manifest_sha256": built.manifest.artifact("web-manifest").sha256,
        "web_bundle_sha256": web.bundle_sha256,
        "signature_verified": True,
        "signature_algorithm": "ed25519",
        "artifact_count": len(built.manifest.artifacts),
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "file_name": artifact.file_name,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for artifact in built.manifest.artifacts
        ],
        "pack_ids": sorted(PACK_TOOLS),
        "core_plus_packs_atomic": True,
        "storage_migration": {
            "schema_version": storage_manifest.schema_version,
            "target_schema_version": storage_manifest.target_schema_version,
            "target_schema_sha256": storage_manifest.target_schema_sha256,
            "manifest_sha256": storage_manifest.sha256,
            "compiled_target_match": True,
        },
        "web": {
            "file_count": len(web.files),
            "immutable_asset_count": len(immutable_files),
            "asset_names_bind_sha256_prefix": True,
            "entrypoint": web.entrypoint,
        },
    }


def _replicate_release_artifacts(
    built: Any,
    destination: Path,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Copy every signed local release artifact into three independent origins."""

    directories: dict[str, Path] = {}
    replicas: list[dict[str, Any]] = []
    for source in built.manifest.sources:
        directory = destination / source.source_id
        directory.mkdir(parents=True, exist_ok=False)
        directories[source.source_id] = directory
        for artifact in built.manifest.artifacts:
            source_path = built.artifact_paths[artifact.artifact_id]
            replica = directory / artifact.file_name
            shutil.copyfile(source_path, replica)
            digest = _sha256_file(replica)
            if digest != artifact.sha256:
                raise DrillError(f"local replica digest mismatch: {source.source_id}")
            replicas.append(
                {
                    "source_id": source.source_id,
                    "kind": source.kind.value,
                    "priority": source.priority,
                    "artifact_id": artifact.artifact_id,
                    "sha256": digest,
                }
            )
    return directories, {
        "artifact_count_per_source": len(built.manifest.artifacts),
        "all_replicas_match_signed_digest": True,
        "replicas": replicas,
    }


def _prepare_in_background(
    coordinator: InstallCoordinator,
    manifest: Any,
    artifact_id: str,
    *,
    deadline: Deadline,
    first_install: bool = False,
    rollback_authorization: str | None = None,
) -> Any:
    """Run download/verify/stage off the caller thread and wait boundedly."""

    results: list[Any] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                coordinator.prepare_update(
                    manifest,
                    artifact_id,
                    first_install=first_install,
                    rollback_authorization=rollback_authorization,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(
        target=run,
        name="ecorex-local-background-download",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=deadline.remaining())
    if thread.is_alive():
        raise DrillError("background candidate download exceeded its deadline")
    if failures:
        raise DrillError(
            f"background candidate download failed safely: {type(failures[0]).__name__}"
        ) from failures[0]
    if len(results) != 1:
        raise DrillError("background candidate download produced no prepared update")
    return results[0]


def _assert_secret_not_persisted(root: Path, secrets: Sequence[bytes]) -> None:
    longest = max((len(secret) for secret in secrets), default=0)
    if longest < 1:
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        overlap = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                payload = overlap + chunk
                if any(secret in payload for secret in secrets):
                    raise DrillError("a temporary private signing key was persisted")
                overlap = payload[-(longest - 1) :] if longest > 1 else b""


def _coordinator(
    install_root: Path,
    release_directory: Path,
    verifier: Ed25519SignatureVerifier,
    *,
    security: WindowsSandboxSlotSecurity,
    drainer: CheckpointingDrainer | None = None,
    migration: ProductLegacyMigrationCoordinator | None = None,
    rollback_authorizer=None,
    fetcher: Any | None = None,
) -> InstallCoordinator:
    return InstallCoordinator(
        install_root,
        fetcher=fetcher
        or LocalSourceFetcher(
            {source.source_id: release_directory for source in _sources()}
        ),
        # The default Bootstrap confirmation path owns health.  This callback
        # must never be reached in this ceremony and therefore fails closed.
        health_checker=lambda _slot: False,
        verifier=verifier,
        drainer=drainer,
        migration_dry_run=(
            (lambda slot: migration.dry_run(slot)) if migration is not None else None
        ),
        migration_prepare=(
            (lambda slot, transaction_id: migration.commit(slot, transaction_id))
            if migration is not None
            else None
        ),
        host_platform=TARGET_PLATFORM,
        host_architecture=TARGET_ARCHITECTURE,
        release_channel=ReleaseChannel.STABLE,
        rollback_authorizer=rollback_authorizer,
        lock_timeout=5.0,
        bootstrap_health_confirmation=True,
        pack_content_verifier=verify_product_capability_pack,
        payload_security_preparer=security.prepare,
        payload_security_attester=security.attest,
        payload_security_cleanup=security.cleanup_failed,
        payload_security_orphan_cleanup=security.cleanup_abandoned,
        slot_security_validator=security.validate,
        slot_security_cleanup=security.cleanup_slot,
    )


def _reserve_loopback_port() -> _LoopbackPortLease:
    import socket

    width = _DRILL_LOOPBACK_PORT_MAX - _DRILL_LOOPBACK_PORT_MIN + 1
    start = secrets.randbelow(width)
    for offset in range(min(width, _DRILL_LOOPBACK_PORT_ATTEMPTS)):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            port = _DRILL_LOOPBACK_PORT_MIN + ((start + offset) % width)
            listener.bind(("127.0.0.1", port))
            return _LoopbackPortLease(listener=listener, port=port)
        except OSError:
            listener.close()
    raise DrillError("a reserved non-ephemeral loopback port is unavailable")


def _get_runtime_probe(
    port: int,
    timeout: float,
    *,
    require_web_contract: bool,
) -> RuntimeProbe:
    index_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        index_connection.request(
            "GET",
            "/",
            headers={
                "Accept": "text/html",
                "Cache-Control": "no-store",
            },
        )
        index_response = index_connection.getresponse()
        if index_response.status != 200:
            index_response.read(256 * 1024)
            return RuntimeProbe(index_response.status, None, None, None, None)
        index_cache_control = index_response.getheader("Cache-Control")
        index_payload = index_response.read(2 * 1024 * 1024 + 1)
        if len(index_payload) > 2 * 1024 * 1024:
            return RuntimeProbe(0, index_cache_control, None, None, None)
    finally:
        index_connection.close()
    try:
        index = index_payload.decode("utf-8")
    except UnicodeDecodeError:
        return RuntimeProbe(0, index_cache_control, None, None, None)
    asset_path: str | None = None
    asset_cache_control: str | None = None
    asset_etag: str | None = None
    asset_match = re.search(
        r"(?:src|href)=[\"'](?:\./|/)?(assets/[^\"']+)[\"']",
        index,
        flags=re.IGNORECASE,
    )
    if asset_match is not None:
        asset_path = asset_match.group(1)
        asset_connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=timeout
        )
        try:
            asset_connection.request(
                "GET",
                f"/{asset_path}",
                headers={"Accept": "*/*", "Cache-Control": "no-cache"},
            )
            asset_response = asset_connection.getresponse()
            asset_response.read(2 * 1024 * 1024 + 1)
            if asset_response.status == 200:
                asset_cache_control = asset_response.getheader("Cache-Control")
                asset_etag = asset_response.getheader("ETag")
        finally:
            asset_connection.close()
    if require_web_contract and (
        index_cache_control != "no-store"
        or asset_path is None
        or asset_cache_control != "public, max-age=31536000, immutable"
        or not asset_etag
    ):
        raise DrillError("the live WebUI cache contract is invalid")
    prefix = "window.__ECOREX_RUNTIME__=Object.freeze("
    suffix = ');Object.defineProperty(window,"__ECOREX_RUNTIME__"'
    start = index.find(prefix)
    end = index.find(suffix, start + len(prefix)) if start >= 0 else -1
    if start < 0 or end < 0:
        return RuntimeProbe(
            0, index_cache_control, asset_path, asset_cache_control, asset_etag
        )
    try:
        runtime_config = json.loads(index[start + len(prefix) : end])
    except json.JSONDecodeError:
        return RuntimeProbe(
            0, index_cache_control, asset_path, asset_cache_control, asset_etag
        )
    bearer = (
        runtime_config.get("bearerToken") if isinstance(runtime_config, dict) else None
    )
    if not isinstance(bearer, str) or not 16 <= len(bearer) <= 512:
        return RuntimeProbe(
            0, index_cache_control, asset_path, asset_cache_control, asset_etag
        )

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "GET",
            "/api/v1/bootstrap",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "Origin": f"http://127.0.0.1:{port}",
                "Authorization": f"Bearer {bearer}",
            },
        )
        response = connection.getresponse()
        response.read(256 * 1024)
        return RuntimeProbe(
            response.status,
            index_cache_control,
            asset_path,
            asset_cache_control,
            asset_etag,
        )
    finally:
        connection.close()


def _get_bootstrap(port: int, timeout: float) -> int:
    """Compatibility probe used by the focused bearer-boundary unit test."""

    return _get_runtime_probe(
        port,
        timeout,
        require_web_contract=False,
    ).bootstrap_status


def _wait_for_full_runtime(
    install_root: Path,
    *,
    expected_slot: str,
    port: int,
    deadline: Deadline,
    bootstrap_results: Sequence[BootstrapRunResult],
    bootstrap_failures: Sequence[BaseException],
) -> RuntimeProbe:
    receipt_path = install_root / "activation-receipt.json"
    last_probe = RuntimeProbe(0, None, None, None, None)
    while True:
        if bootstrap_failures:
            raise DrillError(
                "the signed Bootstrap supervisor failed before readiness: "
                f"{type(bootstrap_failures[0]).__name__}"
            )
        if bootstrap_results:
            result = bootstrap_results[0]
            raise DrillError(
                "the signed Bootstrap exited before readiness: "
                f"phase={deadline.stage}; {result.reason.value}; "
                f"runtime_exit_code={result.runtime_exit_code!r}; "
                f"runtime_startup_stage={result.runtime_startup_stage or 'unavailable'}; "
                f"launches={result.launches}; requested_restarts={result.requested_restarts}"
            )
        try:
            deadline.check()
        except DrillError as exc:
            try:
                receipt_state = json.loads(
                    receipt_path.read_text(encoding="utf-8")
                ).get("state")
            except Exception:
                receipt_state = "unavailable"
            try:
                pointers = SlotStore(install_root).pointers()
                pointer_state = pointers.to_dict()
            except Exception:
                pointer_state = {"state": "unavailable"}
            raise DrillError(
                f"{exc}; expected_slot={expected_slot}; "
                f"receipt_state={receipt_state}; pointers={pointer_state}"
            ) from None
        pointers = SlotStore(install_root).pointers()
        if pointers.current == expected_slot and expected_slot in pointers.known_good:
            try:
                last_probe = _get_runtime_probe(
                    port,
                    min(1.0, deadline.remaining()),
                    require_web_contract=True,
                )
            except (OSError, http.client.HTTPException):
                last_probe = RuntimeProbe(0, None, None, None, None)
            if last_probe.bootstrap_status == 200:
                return last_probe
        time.sleep(0.1)


def _assert_redacted_live_acceptance_evidence(value: Any) -> None:
    """Reject credentials and other ambient browser state from evidence."""

    forbidden_key_fragments = (
        "authorization",
        "bearer",
        "cookie",
        "password",
        "secret",
        "token",
    )

    def inspect(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise DrillError(
                        "live acceptance evidence contains a non-string key"
                    )
                normalized = key.casefold()
                if any(fragment in normalized for fragment in forbidden_key_fragments):
                    raise DrillError(
                        "live acceptance evidence contains a credential field"
                    )
                inspect(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                inspect(nested)
            return
        if isinstance(item, str) and re.search(r"\bbearer\s+\S+", item, re.IGNORECASE):
            raise DrillError("live acceptance evidence contains authorization material")

    inspect(value)


def _execute_live_runtime_acceptance(
    *,
    slots: SlotStore,
    security: WindowsSandboxSlotSecurity,
    manifest: Any,
    artifact: Any,
    security_marker: Mapping[str, Any],
    expected_slot: str,
    source_commit: str,
    port: int,
    deadline: Deadline,
    callback: LiveRuntimeAcceptance,
    rollback_is_authoritative: Callable[[], bool],
) -> Mapping[str, Any]:
    """Run a bounded callback only while the signed current slot is healthy."""

    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit) is None:
        raise DrillError("the live acceptance source commit is invalid")
    if not rollback_is_authoritative():
        raise DrillError("live acceptance requires an authoritative rollback terminal")
    before = slots.pointers()
    if before.current != expected_slot or expected_slot not in before.known_good:
        raise DrillError("live acceptance requires the current known-good slot")
    expected_path = slots.slot_path(expected_slot)
    if slots.validate_receipt(
        slot_id=expected_slot,
        manifest=manifest,
        artifact=artifact,
    ).resolve(strict=True) != expected_path.resolve(strict=True):
        raise DrillError("live acceptance slot receipt is not authoritative")
    if not security.validate(
        expected_path,
        manifest,
        artifact,
        security_marker,
    ):
        raise DrillError("live acceptance sandbox attestation is invalid")
    context = LiveRuntimeAcceptanceContext(
        base_url=f"http://127.0.0.1:{port}",
        source_commit=source_commit,
        release_id=manifest.release_id,
        version=manifest.version,
        build_digest=manifest.build_digest,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        slot_id=expected_slot,
    )
    acceptance_deadline = deadline.bounded(_LIVE_ACCEPTANCE_TIMEOUT_SECONDS)
    try:
        evidence = callback(context, acceptance_deadline)
    except DrillError:
        raise
    except BaseException as exc:
        raise DrillError(
            "installed-signed Runtime CDP acceptance failed safely: "
            f"{type(exc).__name__}"
        ) from None
    if not isinstance(evidence, Mapping) or evidence.get("status") != "passed":
        raise DrillError("installed-signed Runtime CDP acceptance did not pass")
    _assert_redacted_live_acceptance_evidence(evidence)
    try:
        encoded = json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DrillError("live acceptance evidence is not canonical JSON") from exc
    if len(encoded) > _MAX_LIVE_ACCEPTANCE_EVIDENCE_BYTES:
        raise DrillError("live acceptance evidence exceeds its byte limit")

    after = slots.pointers()
    if (
        not rollback_is_authoritative()
        or after != before
        or after.current != expected_slot
    ):
        raise DrillError("the active slot changed during live acceptance")
    if slots.validate_receipt(
        slot_id=expected_slot,
        manifest=manifest,
        artifact=artifact,
    ).resolve(strict=True) != expected_path.resolve(strict=True):
        raise DrillError("the live acceptance slot receipt changed")
    if not security.validate(
        expected_path,
        manifest,
        artifact,
        security_marker,
    ):
        raise DrillError("the live acceptance sandbox attestation changed")
    return json.loads(encoded)


def _run_bootstrap_until_ready(
    install_root: Path,
    *,
    verifier: Ed25519SignatureVerifier,
    security: WindowsSandboxSlotSecurity,
    expected_slot: str,
    deadline: Deadline,
    source_commit: str | None = None,
    live_acceptance: LiveRuntimeAcceptance | None = None,
    live_acceptance_rollback_authority: Callable[[], bool] | None = None,
) -> RuntimeRun:
    runtime_deadline = deadline.bounded(_RUNTIME_READY_TIMEOUT_SECONDS)
    slots = SlotStore(install_root)
    manifest = slots.release_manifest(expected_slot)
    artifact = manifest.artifact(CORE_ARTIFACT_ID)
    marker = slots.marker(expected_slot)
    security_marker = marker.get("security_provision")
    if not isinstance(security_marker, Mapping) or not security.validate(
        slots.slot_path(expected_slot), manifest, artifact, security_marker
    ):
        raise DrillError("the staged Runtime failed strict sandbox attestation")
    port_lease = _reserve_loopback_port()
    port = port_lease.port
    supervisor = BootstrapSupervisor(
        install_root,
        endpoint=RuntimeEndpoint("127.0.0.1", port),
        verifier=verifier,
        host_platform=TARGET_PLATFORM,
        host_architecture=TARGET_ARCHITECTURE,
        max_requested_restarts=2,
        lock_timeout=5.0,
        pack_content_verifier=verify_product_capability_pack,
        launcher=_LeaseReleasingRuntimeLauncher(port_lease),
    )
    result: list[BootstrapRunResult] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(supervisor.run())
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(
        target=run, name="ecorex-local-bootstrap-drill", daemon=True
    )
    thread.start()
    probe = RuntimeProbe(0, None, None, None, None)
    live_evidence: Mapping[str, Any] | None = None
    try:
        probe = _wait_for_full_runtime(
            install_root,
            expected_slot=expected_slot,
            port=port,
            deadline=runtime_deadline,
            bootstrap_results=result,
            bootstrap_failures=failure,
        )
        if live_acceptance is not None:
            if source_commit is None:
                raise DrillError("live acceptance requires a source commit")
            if live_acceptance_rollback_authority is None:
                raise DrillError("live acceptance requires rollback authority")
            live_evidence = _execute_live_runtime_acceptance(
                slots=slots,
                security=security,
                manifest=manifest,
                artifact=artifact,
                security_marker=security_marker,
                expected_slot=expected_slot,
                source_commit=source_commit,
                port=port,
                deadline=runtime_deadline,
                callback=live_acceptance,
                rollback_is_authoritative=live_acceptance_rollback_authority,
            )
    finally:
        port_lease.release()
        try:
            supervisor.request_stop(int(signal.SIGTERM))
        except Exception:
            pass
        # Readiness is capped by the phase deadline, while process-tree cleanup
        # retains its ordinary grace period without exceeding the ceremony.
        remaining = deadline.expires_at - time.monotonic()
        thread.join(timeout=min(15.0, max(0.5, remaining)))
    if thread.is_alive():
        raise DrillError("the signed Bootstrap did not stop within the deadline")
    if failure:
        raise DrillError(
            f"the signed Bootstrap failed safely: {type(failure[0]).__name__}"
        )
    if len(result) != 1 or result[0].reason is not BootstrapReason.STOP_REQUESTED:
        raise DrillError("the signed Bootstrap did not reach a controlled stop")
    if not security.validate(
        slots.slot_path(expected_slot), manifest, artifact, security_marker
    ):
        raise DrillError("the live Runtime sandbox attestation changed")
    return RuntimeRun(result[0], probe, live_evidence)


def _build_and_run(
    repo: Path,
    temporary: Path,
    deadline: Deadline,
    *,
    live_acceptance: LiveRuntimeAcceptance | None = None,
    legacy_source_version: str = "0.2.9.2",
    legacy_source: Path | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    _require_host()
    web_dist = repo / "desktop" / "dist"
    if not (web_dist / "index.html").is_file():
        raise DrillError("the current content-addressed Web dist is missing")

    release_private = Ed25519PrivateKey.generate()
    rollback_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    publication_private = Ed25519PrivateKey.generate()
    release_public = _public_key(release_private)
    rollback_public = _public_key(rollback_private)
    session_public = _public_key(session_private)
    publication_public = _public_key(publication_private)
    private_material = (
        _private_key_bytes(release_private),
        _private_key_bytes(rollback_private),
        _private_key_bytes(session_private),
        _private_key_bytes(publication_private),
    )
    signer = Ed25519MemorySigner(SIGNING_KEY_ID, release_private)
    verifier = Ed25519SignatureVerifier({SIGNING_KEY_ID: release_public})

    deadline.enter("source-pinned Windows platform stage")
    stage = _stage_windows(
        repo,
        temporary / "windows-stage",
        release_public=release_public,
        rollback_public=rollback_public,
        session_public=session_public,
        publication_public=publication_public,
        deadline=deadline,
    )
    try:
        dependency_gate = json.loads(
            (
                stage.root / ".evidence/windows-x64/core/dependency-closure.json"
            ).read_text(encoding="utf-8")
        )
        distributions = dependency_gate["details"]["distributions"]
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise DrillError(
            "the staged Runtime dependency evidence is unavailable"
        ) from exc
    bootstrap_floor = _bind_local_bootstrap_minimum(stage.bootstrap, signer)
    baseline_core = stage.core
    _assert_no_runtime_bytecode(baseline_core)
    created_at = datetime.now(UTC).replace(microsecond=0)
    deadline.enter("baseline release build")
    baseline_release = _build_release(
        signer=signer,
        core=baseline_core,
        bootstrap=stage.bootstrap,
        packs={pack_id: stage.pack(pack_id) for pack_id in PACK_TOOLS},
        web_dist=web_dist,
        destination=temporary / "baseline-release",
        created_at=created_at,
    )
    deadline.enter("baseline release verification")
    baseline_evidence = _verify_release(baseline_release, verifier)

    install_root = temporary / "install"
    (install_root / "state").mkdir(parents=True)
    (install_root / "workspace").mkdir()
    bootstrap_helper = install_root / "bootstrap" / "bin" / "ecorex-sandbox-host.exe"
    bootstrap_helper.parent.mkdir(parents=True)
    shutil.copyfile(
        stage.bootstrap / "bin" / "ecorex-sandbox-host.exe",
        bootstrap_helper,
    )
    if _sha256_file(bootstrap_helper) != stage.helper_sha256:
        raise DrillError("the locally staged Bootstrap helper changed")
    security = WindowsSandboxSlotSecurity(
        install_root,
        bootstrap_helper,
        expected_helper_sha256=stage.helper_sha256,
    )
    drainer = CheckpointingDrainer(install_root / "drain-receipts")
    legacy = _prepare_legacy_source(
        repo,
        temporary,
        source_version=legacy_source_version,
        user_source=legacy_source,
        deadline=deadline,
    )
    legacy_source = Path(legacy["source"])
    write_product_migration_plan(
        install_root,
        legacy_source,
        source_version=str(legacy["source_version"]),
    )
    migration = ProductLegacyMigrationCoordinator(
        install_root,
        install_root / "state" / TARGET_DATABASE_NAME,
    )
    baseline_replica_dirs, replica_evidence = _replicate_release_artifacts(
        baseline_release,
        temporary / "baseline-replicas",
    )
    baseline_fetcher = RecordingFailoverFetcher(
        baseline_replica_dirs,
        fail_once=("github-cn",),
        partial_bytes=256 * 1024,
    )
    baseline_coordinator = _coordinator(
        install_root,
        baseline_release.output_dir,
        verifier,
        security=security,
        drainer=drainer,
        migration=migration,
        fetcher=baseline_fetcher,
    )
    deadline.enter("first-install background preparation")
    prepared = _prepare_in_background(
        baseline_coordinator,
        baseline_release.manifest,
        CORE_ARTIFACT_ID,
        deadline=deadline,
        first_install=True,
    )
    if (
        prepared.state is not InstallState.AWAITING_USER
        or baseline_coordinator.slots.pointers().current is not None
    ):
        raise DrillError(
            "first install was activated before explicit user confirmation"
        )
    core_attempts = [
        attempt
        for attempt in baseline_fetcher.attempts
        if attempt["artifact_id"] == CORE_ARTIFACT_ID
    ]
    if (
        [attempt["source_id"] for attempt in core_attempts] != ["github-cn", "github"]
        or core_attempts[0]["resume_from"] != 0
        or core_attempts[1]["resume_from"] != 0
    ):
        raise DrillError("signed mirror-to-GitHub safe fallback order was not enforced")
    shutil.rmtree(temporary / "baseline-replicas")
    deadline.enter("first-install activation")
    pending = baseline_coordinator.activate(prepared.transaction_id)
    if pending.state is not InstallState.HEALTHCHECKING:
        # Coordinator reports ACTIVATING on older compatible journals; the
        # durable journal is authoritative and must be health-pending.
        latest = baseline_coordinator.journal.latest()
        if latest is None or latest.state is not InstallState.HEALTHCHECKING:
            raise DrillError(
                "first install did not enter Bootstrap health confirmation"
            )
    deadline.enter("first-install Bootstrap health")
    baseline_runtime = _run_bootstrap_until_ready(
        install_root,
        verifier=verifier,
        security=security,
        expected_slot=prepared.slot_id,
        deadline=deadline,
    )
    registration = {
        "account_id": "local-drill-account",
        "organization_id": "local-drill-organization",
        "lease_id": "local-drill-lease",
        "lease_digest": hashlib.sha256(b"local-drill-managed-lease").hexdigest(),
        "session_generation": 1,
        "lease_revision": 1,
    }
    if baseline_coordinator.mark_registration_complete(registration) is not True:
        raise DrillError("first-install registration authority did not release the pin")
    if baseline_coordinator.pinned_target is not None:
        raise DrillError("first-install registration pin was not released")
    baseline_coordinator.slots.validate_receipt(
        slot_id=prepared.slot_id,
        manifest=baseline_release.manifest,
        artifact=baseline_release.manifest.artifact(CORE_ARTIFACT_ID),
    )
    completion_before = migration.completion_authority()
    if completion_before is None:
        raise DrillError("the legacy product migration has no completion authority")
    if (
        inventory_source(
            legacy_source,
            source_version=str(legacy["source_version"]),
        ).digest
        != legacy["inventory_digest"]
    ):
        raise DrillError("the legacy source changed during copy-on-write migration")
    try:
        migration_report = json.loads(
            (install_root / "state" / "migration-report.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillError(
            "the committed migration aggregate report is unavailable"
        ) from exc
    if not isinstance(migration_report, Mapping):
        raise DrillError("the committed migration aggregate report is invalid")
    migration_aggregates = _migration_aggregate_evidence(
        source=legacy_source,
        database=install_root / "state" / TARGET_DATABASE_NAME,
        report=migration_report,
    )
    shutil.rmtree(legacy_source)
    restarted_migration = ProductLegacyMigrationCoordinator(
        install_root,
        install_root / "state" / TARGET_DATABASE_NAME,
    )
    if (
        restarted_migration.completion_authority() != completion_before
        or restarted_migration.commit(
            baseline_coordinator.slots.slot_path(prepared.slot_id)
        )
        is not True
    ):
        raise DrillError(
            "the completed migration did not restart without its legacy source"
        )
    deadline.enter("post-migration source-removal Runtime restart")
    migrated_restart = _run_bootstrap_until_ready(
        install_root,
        verifier=verifier,
        security=security,
        expected_slot=prepared.slot_id,
        deadline=deadline,
    )
    baseline_trace = [
        entry.state.value
        for entry in baseline_coordinator.journal.entries()
        if entry.transaction_id == prepared.transaction_id
    ]

    # Produce a second valid, distinct, signed Core without inventing a product
    # version.  This proves background preparation remains separate from the
    # explicit "update and refresh" activation boundary.
    deadline.enter("healthy update marker")
    (baseline_core / "candidate-build-marker.json").write_text(
        json.dumps(
            {"exercise": "healthy-same-version-refresh", "schema_version": 1},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    deadline.enter("healthy update release build")
    healthy_release = _build_release(
        signer=signer,
        core=baseline_core,
        bootstrap=stage.bootstrap,
        packs={pack_id: stage.pack(pack_id) for pack_id in PACK_TOOLS},
        web_dist=web_dist,
        destination=temporary / "healthy-release",
        created_at=created_at + timedelta(seconds=1),
    )
    deadline.enter("healthy update release verification")
    healthy_evidence = _verify_release(healthy_release, verifier)
    if healthy_release.manifest.build_digest == baseline_release.manifest.build_digest:
        raise DrillError("healthy update candidate did not produce a distinct build")

    healthy_authorization = hashlib.sha256(os.urandom(32)).hexdigest()

    def authorize_healthy_update(
        current: Mapping[str, Any], target: Any, token: str
    ) -> bool:
        return (
            token == healthy_authorization
            and current.get("version") == __version__
            and target.version == __version__
            and target.build_digest == healthy_release.manifest.build_digest
        )

    healthy_coordinator = _coordinator(
        install_root,
        healthy_release.output_dir,
        verifier,
        security=security,
        drainer=drainer,
        migration=restarted_migration,
        rollback_authorizer=authorize_healthy_update,
    )
    prior_slot = healthy_coordinator.slots.pointers().current
    deadline.enter("healthy update background preparation")
    healthy_prepared = _prepare_in_background(
        healthy_coordinator,
        healthy_release.manifest,
        CORE_ARTIFACT_ID,
        deadline=deadline,
        rollback_authorization=healthy_authorization,
    )
    if (
        healthy_prepared.state is not InstallState.AWAITING_USER
        or healthy_coordinator.slots.pointers().current != prior_slot
    ):
        raise DrillError(
            "background update changed the active slot before confirmation"
        )
    deadline.enter("healthy update user activation")
    healthy_pending = healthy_coordinator.activate(healthy_prepared.transaction_id)
    if healthy_pending.current_slot != healthy_prepared.slot_id:
        raise DrillError("confirmed healthy update was not provisionally selected")
    deadline.enter("healthy update and refresh Bootstrap health")
    healthy_runtime = _run_bootstrap_until_ready(
        install_root,
        verifier=verifier,
        security=security,
        expected_slot=healthy_prepared.slot_id,
        deadline=deadline,
    )
    healthy_pointers = healthy_coordinator.slots.pointers()
    if (
        healthy_pointers.current != healthy_prepared.slot_id
        or healthy_prepared.slot_id not in healthy_pointers.known_good
    ):
        raise DrillError("healthy update did not become the current known-good slot")
    healthy_coordinator.slots.validate_receipt(
        slot_id=healthy_prepared.slot_id,
        manifest=healthy_release.manifest,
        artifact=healthy_release.manifest.artifact(CORE_ARTIFACT_ID),
    )
    healthy_trace = [
        entry.state.value
        for entry in healthy_coordinator.journal.entries()
        if entry.transaction_id == healthy_prepared.transaction_id
    ]

    # A real ReleaseBuilder product candidate with a deliberately failing
    # signed entrypoint exercises the available pre-data rollback.  It remains
    # version 1.0.0: fabricating a 1.0.1 version would violate the single source.
    # Keep the exact staged helper, interpreter and Pack projection contract;
    # only the Runtime module entrypoint is changed to a bounded failure.
    deadline.enter("fault-candidate Core fixture")
    fault_core = temporary / "fault-core"
    shutil.copytree(baseline_core, fault_core)
    _inject_fault_runtime_entrypoint(fault_core)
    _rebind_fault_pack_python(fault_core)
    _preflight_fault_candidate(fault_core, deadline)
    deadline.check()
    deadline.enter("fault-candidate release build")
    fault_release = _build_release(
        signer=signer,
        core=fault_core,
        bootstrap=stage.bootstrap,
        packs={pack_id: stage.pack(pack_id) for pack_id in PACK_TOOLS},
        web_dist=web_dist,
        destination=temporary / "fault-release",
        created_at=created_at + timedelta(seconds=2),
    )
    deadline.enter("fault-candidate release verification")
    fault_evidence = _verify_release(fault_release, verifier)
    if fault_release.manifest.build_digest == baseline_release.manifest.build_digest:
        raise DrillError("fault-injection candidate did not produce a distinct build")

    fault_authorization = hashlib.sha256(os.urandom(32)).hexdigest()

    def authorize_fault_update(
        current: Mapping[str, Any], target: Any, token: str
    ) -> bool:
        return (
            token == fault_authorization
            and current.get("version") == __version__
            and target.version == __version__
            and target.build_digest == fault_release.manifest.build_digest
        )

    deadline.enter("bad-digest fail-closed exercise")
    corrupt_sources: dict[str, Path] = {}
    fault_artifact = fault_release.manifest.artifact(CORE_ARTIFACT_ID)
    for source in fault_release.manifest.sources:
        directory = temporary / "bad-digest-replicas" / source.source_id
        directory.mkdir(parents=True)
        replica = directory / fault_artifact.file_name
        shutil.copyfile(fault_release.artifact_paths[CORE_ARTIFACT_ID], replica)
        with replica.open("r+b") as stream:
            first = stream.read(1)
            if not first:
                raise DrillError("the signed Core artifact is unexpectedly empty")
            stream.seek(0)
            stream.write(bytes((first[0] ^ 0x01,)))
            stream.flush()
            os.fsync(stream.fileno())
        corrupt_sources[source.source_id] = directory
    current_before_bad_digest = SlotStore(install_root).pointers().current
    bad_digest_coordinator = _coordinator(
        install_root,
        fault_release.output_dir,
        verifier,
        security=security,
        drainer=drainer,
        migration=restarted_migration,
        rollback_authorizer=authorize_fault_update,
        fetcher=LocalSourceFetcher(corrupt_sources),
    )
    bad_digest_error: str | None = None
    try:
        bad_digest_coordinator.prepare_update(
            fault_release.manifest,
            CORE_ARTIFACT_ID,
            rollback_authorization=fault_authorization,
        )
    except Exception as exc:
        bad_digest_error = type(exc).__name__
    if (
        bad_digest_error is None
        or SlotStore(install_root).pointers().current != current_before_bad_digest
    ):
        raise DrillError("a bad-digest candidate did not fail without pointer mutation")
    shutil.rmtree(temporary / "bad-digest-replicas")

    replacement = _coordinator(
        install_root,
        fault_release.output_dir,
        verifier,
        security=security,
        drainer=drainer,
        migration=restarted_migration,
        rollback_authorizer=authorize_fault_update,
    )
    deadline.enter("fault-candidate background preparation")
    fault_prepared = _prepare_in_background(
        replacement,
        fault_release.manifest,
        CORE_ARTIFACT_ID,
        deadline=deadline,
        rollback_authorization=fault_authorization,
    )
    if fault_prepared.state is not InstallState.AWAITING_USER:
        raise DrillError("fault candidate did not wait for explicit confirmation")
    deadline.enter("fault-candidate activation")
    fault_pending = replacement.activate(fault_prepared.transaction_id)
    if fault_pending.current_slot != fault_prepared.slot_id:
        raise DrillError("fault-injection candidate was not provisionally selected")

    def rollback_is_authoritative() -> bool:
        rollback_pointers = replacement.slots.pointers()
        rollback_terminal = replacement.journal.latest()
        return (
            rollback_pointers.current == healthy_prepared.slot_id
            and healthy_prepared.slot_id in rollback_pointers.known_good
            and rollback_terminal is not None
            and rollback_terminal.state is InstallState.ROLLBACK
            and not replacement.slots.slot_path(fault_prepared.slot_id).exists()
        )

    deadline.enter("pre-data rollback Bootstrap health")
    rollback_runtime = _run_bootstrap_until_ready(
        install_root,
        verifier=verifier,
        security=security,
        expected_slot=healthy_prepared.slot_id,
        deadline=deadline,
        source_commit=stage.commit_sha,
        live_acceptance=live_acceptance,
        live_acceptance_rollback_authority=rollback_is_authoritative,
    )
    pointers = replacement.slots.pointers()
    latest = replacement.journal.latest()
    if (
        pointers.current != healthy_prepared.slot_id
        or healthy_prepared.slot_id not in pointers.known_good
        or latest is None
        or latest.state is not InstallState.ROLLBACK
        or replacement.slots.slot_path(fault_prepared.slot_id).exists()
    ):
        raise DrillError("the real Bootstrap rollback did not restore the prior slot")
    replacement.slots.validate_receipt(
        slot_id=healthy_prepared.slot_id,
        manifest=healthy_release.manifest,
        artifact=healthy_release.manifest.artifact(CORE_ARTIFACT_ID),
    )
    fault_trace = [
        entry.state.value
        for entry in replacement.journal.entries()
        if entry.transaction_id == fault_prepared.transaction_id
    ]

    deadline.enter("private-material persistence scan")
    _assert_secret_not_persisted(temporary, private_material)
    healthy_marker = replacement.slots.marker(healthy_prepared.slot_id)
    security_receipt = healthy_marker.get("security_provision")
    if not isinstance(security_receipt, Mapping):
        raise DrillError("the retained slot has no sandbox security receipt")
    try:
        migration_receipt = json.loads(
            (install_root / PRODUCT_MIGRATION_RECEIPT_NAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillError("the committed migration receipt is unavailable") from exc
    missing_protected_receipts = [
        f"{key}-{platform}-{architecture}"
        for platform, architecture in _PRODUCTION_TARGETS
        if (platform, architecture) != (TARGET_PLATFORM, TARGET_ARCHITECTURE)
        for key in _WINDOWS_STAGE_KEYS
    ]
    _cleanup_temporary_sandbox_domain(replacement.slots, security)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "passed",
        "evidence_class": "local-windows-drill",
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "deadline_policy": {
            "total_timeout_seconds": round(deadline.expires_at - started_at, 3),
            "platform_stage_timeout_seconds": _PLATFORM_STAGE_TIMEOUT_SECONDS,
            "runtime_readiness_timeout_seconds": _RUNTIME_READY_TIMEOUT_SECONDS,
            "runtime_readiness_windows": 4,
        },
        "target": {"platform": TARGET_PLATFORM, "architecture": TARGET_ARCHITECTURE},
        "version": __version__,
        "signing": {
            "algorithm": "ed25519",
            "key_id": SIGNING_KEY_ID,
            "public_key_sha256": hashlib.sha256(release_public).hexdigest(),
            "private_key_persisted": False,
            "os_application_signing_used": False,
        },
        "platform_stage": {
            "source_pinned_production_stager": True,
            "commit_sha": stage.commit_sha,
            "worktree_dirty": stage.worktree_dirty,
            "protected_clean_runner_claimed": False,
            "go_version": stage.go_version,
            "local_receipt_count": len(stage.receipts),
            "local_receipt_sha256": dict(stage.receipt_sha256),
            "stage_ids": [str(receipt["stage_id"]) for receipt in stage.receipts],
            "native_build_receipt": dict(stage.native_build_receipt),
            "caller_pinned_schema2_helper_sha256": stage.helper_sha256,
            "bootstrap_minimum_stable": bootstrap_floor,
            "pack_ids": sorted(PACK_TOOLS),
        },
        "production_candidate_preflight": {
            "status": "blocked",
            "fixed_stage_receipt_requirement": (
                len(_PRODUCTION_TARGETS) * len(_WINDOWS_STAGE_KEYS)
            ),
            "fixed_gate_relaxed": False,
            "locally_executed_windows_receipts": len(_WINDOWS_STAGE_KEYS),
            "missing_protected_runner_receipts": missing_protected_receipts,
            "missing_count": len(missing_protected_receipts),
            "macos_cross_build_counted_as_release_gate": False,
            "promotion_claimed": False,
            "blockers": [
                "macos-arm64 protected-runner Core+Bootstrap+6 Pack receipts",
                "macos-x64 protected-runner Core+Bootstrap+6 Pack receipts",
                "protected clean-runner provenance is absent from this local workstation drill",
            ],
        },
        "release": baseline_evidence,
        "source_failover": {
            "signed_source_order": [
                source.source_id for source in baseline_release.manifest.sources
            ],
            "attempts": baseline_fetcher.attempts,
            "core_attempt_order": [attempt["source_id"] for attempt in core_attempts],
            "selected_core_source": core_attempts[-1]["source_id"],
            "core_resume_offset": core_attempts[-1]["resume_from"],
            "cross_source_partial_reuse_forbidden": True,
            "injected_outages": ["github-cn"],
            **replica_evidence,
        },
        "runtime_dependencies": list(distributions),
        "first_install": {
            "prepared_state": prepared.state.value,
            "background_download": True,
            "activation_required_user_confirmation": True,
            "journal_states": baseline_trace,
            "terminal_state": InstallState.COMPLETED.value,
            "slot_id": prepared.slot_id,
            "bootstrap_http_status": baseline_runtime.bootstrap_status,
            "bootstrap_launches": baseline_runtime.result.launches,
            "registration_pin_released": True,
            "registration_authority": "local-contract-fixture-not-external-credential",
            "core_plus_pack_slot": True,
        },
        "migration": {
            "source_version": legacy["source_version"],
            "baseline_release_schema_commit": legacy.get(
                "baseline_release_schema_commit"
            ),
            "evidence_level": (
                migration_report.get("source_evidence", {}).get("evidence_level")
                if isinstance(migration_report.get("source_evidence"), Mapping)
                else legacy["evidence_level"]
            ),
            "corpus_mode": legacy["corpus_mode"],
            "source_inventory": {
                "file_count": legacy["inventory_entries"],
                "total_bytes": legacy["inventory_bytes"],
            },
            "aggregate_counts": migration_aggregates,
            "copy_on_write": True,
            "user_source_read_only": True,
            "source_unchanged_during_snapshot": legacy["source_unchanged"],
            "completion_authority_digest": completion_before["authority_digest"],
            "receipt_state": migration_receipt.get("state"),
            "disposable_source_snapshot_deleted_after_commit": (
                not legacy_source.exists()
            ),
            "restart_after_source_deletion_http_status": (
                migrated_restart.bootstrap_status
            ),
            "idempotent_restart_without_source": True,
            "real_installed_user_corpus_claimed": False,
        },
        "drain_checkpoint": {
            "call_count": len(drainer.calls),
            "receipts": drainer.calls,
            "durable_checkpoint_before_each_activation": bool(drainer.calls),
            "real_external_long_job_claimed": False,
        },
        "sandbox_security": {
            "contract": security_receipt.get("contract"),
            "helper_sha256": security_receipt.get("helper_sha256"),
            "permission_domain_sha256": security_receipt.get(
                "permission_domain_sha256"
            ),
            "read_roots_sha256": security_receipt.get("read_roots_sha256"),
            "root_security_sha256": security_receipt.get("root_security_sha256"),
            "attestation_security_policy_sha256": security_receipt.get(
                "attestation_security_policy_sha256"
            ),
            "strict_attestation_before_and_after_runtime": True,
            "system_python_acl_mutated": False,
            "temporary_appcontainer_domain_removed": True,
        },
        "update_and_refresh": {
            "release": healthy_evidence,
            "same_version_replacement": True,
            "cross_version_update_claimed": False,
            "background_download": True,
            "prepared_state": healthy_prepared.state.value,
            "active_slot_unchanged_before_confirmation": True,
            "user_confirmed_activation": True,
            "journal_states": healthy_trace,
            "terminal_state": InstallState.COMPLETED.value,
            "current_slot_id": healthy_pointers.current,
            "bootstrap_http_status": healthy_runtime.bootstrap_status,
            "bootstrap_launches": healthy_runtime.result.launches,
            "refresh_completed": True,
            "web_cache": {
                "index_cache_control": healthy_runtime.probe.index_cache_control,
                "asset_path": healthy_runtime.probe.asset_path,
                "asset_cache_control": healthy_runtime.probe.asset_cache_control,
                "asset_etag_present": bool(healthy_runtime.probe.asset_etag),
            },
        },
        "rollback": {
            "exercise": "signed_same_version_replacement_pre_data",
            "cross_version_update_claimed": False,
            "fault_preflight_exit_code": 70,
            "pack_python_manifest_rebound": True,
            "fault_release": fault_evidence,
            "fault_slot_id": fault_prepared.slot_id,
            "prepared_state": fault_prepared.state.value,
            "journal_states": fault_trace,
            "terminal_state": latest.state.value,
            "restored_slot_id": pointers.current,
            "fault_slot_discarded": True,
            "bootstrap_http_status": rollback_runtime.bootstrap_status,
            "bootstrap_launches": rollback_runtime.result.launches,
        },
        "bad_digest": {
            "status": "rejected",
            "error_type": bad_digest_error,
            "active_slot_unchanged": True,
            "corrupt_slot_activated": False,
        },
        "network": {
            "external_publication": False,
            "artifact_fetcher": "RecordingFailoverFetcher(LocalSourceFetcher)",
            "runtime_live_endpoints": "loopback-only",
        },
        **(
            {"installed_signed_runtime_cdp": rollback_runtime.live_acceptance}
            if rollback_runtime.live_acceptance is not None
            else {}
        ),
        "blind_spots": [
            "no live GH mirror, GitHub Release, CDN or Control Plane origin was contacted",
            "no external Model/Image Gateway, connector, OTLP or tenant credential was used",
            "macOS arm64/x64 Core and Pack receipts require protected native runners",
            "same-version replacement was exercised; no fabricated 1.0.1 cross-version claim",
            "legacy data was exercised through a read-only snapshot; installed-user provenance was not attested",
        ],
    }


def run_drill(
    *,
    repo: Path,
    timeout_seconds: float,
    live_acceptance: LiveRuntimeAcceptance | None = None,
    legacy_source_version: str = "0.2.9.2",
    legacy_source: Path | None = None,
) -> dict[str, Any]:
    if not _MIN_TIMEOUT_SECONDS <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise DrillError(
            f"timeout must be between {_MIN_TIMEOUT_SECONDS:g} and "
            f"{_MAX_TIMEOUT_SECONDS:g} seconds"
        )
    if legacy_source_version not in _SUPPORTED_LEGACY_SOURCE_VERSIONS:
        raise DrillError("the selected legacy source version is unsupported")
    if legacy_source is not None and not legacy_source.is_dir():
        raise DrillError("the user-selected legacy source is unavailable")
    temporary_path: Path | None = None
    failed = False
    report: dict[str, Any]
    try:
        temporary_path = Path(tempfile.mkdtemp(prefix="ecorex-v1-win-signed-drill-"))
        try:
            report = _build_and_run(
                repo.resolve(),
                temporary_path,
                Deadline.after(timeout_seconds),
                live_acceptance=live_acceptance,
                legacy_source_version=legacy_source_version,
                legacy_source=legacy_source,
            )
        except DrillError:
            raise
        except Exception as exc:
            if os.environ.get("ECOREX_DRILL_DEBUG") == "1":
                raise
            raise DrillError(
                f"the signed-candidate ceremony failed safely: {type(exc).__name__}"
            ) from None
    except BaseException:
        failed = True
        raise
    finally:
        security_cleaned = (
            _best_effort_failed_security_cleanup(temporary_path)
            if failed and temporary_path is not None
            else True
        )
        keep_debug = failed and (
            os.environ.get("ECOREX_DRILL_DEBUG_KEEP") == "1" or not security_cleaned
        )
        if temporary_path is not None and not keep_debug:
            shutil.rmtree(temporary_path, ignore_errors=False)
        elif temporary_path is not None:
            print(f"debug candidate retained at: {temporary_path}", file=sys.stderr)
            if not security_cleaned:
                print(
                    "temporary sandbox cleanup needs operator inspection",
                    file=sys.stderr,
                )
    if temporary_path is None or temporary_path.exists():
        raise DrillError("the disposable candidate directory was not removed")
    report["cleanup"] = {"temporary_directory_removed": True}
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local Windows x64 EcoreX v1 signed-candidate drill."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_repo_root(),
        help="EcoreX repository root (defaults to this script's repository)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"total bounded drill time, between {_MIN_TIMEOUT_SECONDS:g} and "
            f"{_MAX_TIMEOUT_SECONDS:g} seconds; each Runtime readiness window "
            f"is bounded to {_RUNTIME_READY_TIMEOUT_SECONDS:g} seconds"
        ),
    )
    parser.add_argument(
        "--legacy-source-version",
        choices=_SUPPORTED_LEGACY_SOURCE_VERSIONS,
        default="0.2.9.2",
        help=(
            "legacy data contract exercised by the activation gate; defaults to "
            "the v0.2.9.2 upgrade path"
        ),
    )
    parser.add_argument(
        "--legacy-source",
        type=Path,
        help=(
            "optional user-selected legacy root; it is inventoried and copied "
            "read-only into the disposable drill before migration"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional path for the redacted JSON evidence report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_drill(
            repo=args.repo_root,
            timeout_seconds=args.timeout_seconds,
            legacy_source_version=args.legacy_source_version,
            legacy_source=args.legacy_source,
        )
    except DrillError as exc:
        print(f"Windows signed-candidate drill failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        destination = args.report.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
