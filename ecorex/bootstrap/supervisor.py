"""Signed side-by-side Runtime selection and bounded process supervision."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform as platform_module
import re
import signal
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol

from ecorex.startup_diagnostics import (
    STARTUP_DIAGNOSTIC_TOKEN_ENV,
    issue_startup_diagnostic_token,
    prepare_startup_diagnostic_directory,
    read_startup_diagnostic,
)
from ecorex.update import (
    ACTIVATION_NONCE_ENV,
    ACTIVATION_TRANSACTION_ENV,
    ActivationIntentError,
    ProductFileLock,
    ProvisionalActivationController,
    RejectingSignatureVerifier,
    ReleaseArtifact,
    ReleaseManifest,
    SignatureVerifier,
    SlotPointers,
    SlotStore,
    StorageError,
    VerificationError,
    verify_artifact_signature,
    verify_manifest_signature,
)
from ecorex.update.pack_install import PackContentVerifier, validate_installed_pack_set
from ecorex.update.activation import VerifiedProvisionalActivation

from .health import ActivationHealthProbe, LoopbackActivationHealthProbe

from .errors import (
    BootstrapConfigurationError,
    BootstrapTrustError,
    RuntimeLaunchError,
)
from .restart import RUNTIME_RELOAD_EXIT_CODE, RUNTIME_RESTART_EXIT_CODE

RUNTIME_OWNER_NONCE_ENV = "ECOREX_RUNTIME_OWNER_NONCE"
RUNTIME_ACCEPTANCE_PREVIEW_ENV = "ECOREX_RUNTIME_ACCEPTANCE_PREVIEW"
_RUNTIME_OWNER_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BootstrapExitCode(IntEnum):
    """Bounded public process outcomes used by the bootstrap CLI."""

    SUCCESS = 0
    CONFIGURATION = 64
    RUNTIME_FAILURE = 70
    TRUST_FAILURE = 78


class BootstrapReason(StrEnum):
    RUNTIME_COMPLETED = "runtime_completed"
    RUNTIME_FAILED = "runtime_failed"
    RUNTIME_SIGNALLED = "runtime_signalled"
    STOP_REQUESTED = "stop_requested"
    RESTART_WITHOUT_ACTIVATION = "restart_without_activation"
    RESTART_LIMIT_REACHED = "restart_limit_reached"


@dataclass(frozen=True, slots=True)
class RuntimeEndpoint:
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise BootstrapConfigurationError(
                "Runtime host must be a literal loopback IP address"
            ) from exc
        if not address.is_loopback:
            raise BootstrapConfigurationError("Runtime host must be loopback-only")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise BootstrapConfigurationError("Runtime port must be an integer")
        if not 1 <= self.port <= 65535:
            raise BootstrapConfigurationError("Runtime port is outside the TCP range")


class ActivationCompanion(Protocol):
    """Reversible desktop launcher side of one provisional activation."""

    def prepare_transaction(self, transaction_id: str) -> Path: ...

    def commit_activation(self, transaction_id: str) -> None: ...

    def rollback_activation(self, transaction_id: str) -> None: ...

    def converge_activation(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeSlot:
    slot_id: str
    slot_path: Path
    payload_root: Path
    manifest: ReleaseManifest
    artifact: ReleaseArtifact


@dataclass(frozen=True, slots=True)
class _ExecutableFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeProcessSpec:
    slot_id: str
    executable: Path
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False, compare=False)
    executable_fingerprint: _ExecutableFingerprint


@dataclass(frozen=True, slots=True)
class _SelectedRuntime:
    child: RuntimeChild
    slot_id: str
    provisional: VerifiedProvisionalActivation | None = None
    health_nonce: str | None = field(default=None, repr=False, compare=False)
    startup_diagnostic_token: str | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class BootstrapRunResult:
    exit_code: int
    reason: BootstrapReason
    launches: int
    requested_restarts: int
    launched_slots: tuple[str, ...]
    runtime_exit_code: int | None = None
    runtime_startup_stage: str | None = None


class RuntimeChild(Protocol):
    def wait(self) -> int:
        ...

    def send_signal(self, signum: int) -> None:
        ...


class RuntimeLauncher(Protocol):
    def start(self, spec: RuntimeProcessSpec) -> RuntimeChild:
        ...


RuntimeExecutableResolver = Callable[[VerifiedRuntimeSlot, str], Path]


def detect_host_target() -> tuple[str, str]:
    return _normalize_host_target(
        os_name=os.name,
        sys_platform=sys.platform,
        machine=platform_module.machine(),
        pointer_bits=64 if sys.maxsize > 2**32 else 32,
    )


def _normalize_host_target(
    *,
    os_name: str,
    sys_platform: str,
    machine: str,
    pointer_bits: int,
) -> tuple[str, str]:
    if os_name == "nt":
        # ``platform.machine()`` reads PROCESSOR_ARCHITECTURE on Windows.  The
        # Bootstrap intentionally removes that spoofable environment variable,
        # so use the already-running signed Python process bitness instead.
        return "windows", "x64" if pointer_bits == 64 else "unsupported"
    if sys_platform != "darwin":
        return "unsupported", "unsupported"
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine.casefold(), "unsupported")
    return "macos", architecture


class CurrentSlotVerifier:
    """Re-establish trust from the authoritative slot pointers on every launch."""

    def __init__(
        self,
        slots: SlotStore,
        *,
        verifier: SignatureVerifier | None = None,
        host_platform: str,
        host_architecture: str,
        pack_content_verifier: PackContentVerifier | None = None,
    ) -> None:
        if host_platform not in {"windows", "macos"}:
            raise BootstrapConfigurationError("Bootstrap host platform is unsupported")
        if host_architecture not in {"x64", "arm64"}:
            raise BootstrapConfigurationError("Bootstrap host architecture is unsupported")
        if host_platform == "windows" and host_architecture != "x64":
            raise BootstrapConfigurationError("Windows Bootstrap supports x64 only")
        self.slots = slots
        self.verifier = verifier or RejectingSignatureVerifier()
        self.host_platform = host_platform
        self.host_architecture = host_architecture
        self.pack_content_verifier = pack_content_verifier

    def verify_current(self) -> VerifiedRuntimeSlot:
        try:
            before = _read_authoritative_pointers(self.slots.root)
            slot_id = before.current
            if slot_id is None:
                raise BootstrapTrustError("No active Runtime slot is installed")
            if slot_id not in before.known_good:
                raise BootstrapTrustError(
                    "The active Runtime slot is not independently marked known-good"
                )
            manifest = self.slots.release_manifest(slot_id)
            verify_manifest_signature(manifest, self.verifier)
            marker = self.slots.marker(slot_id)
            artifact_id = marker.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise BootstrapTrustError("The active slot has no signed artifact identity")
            artifact = manifest.artifact(artifact_id)
            verify_artifact_signature(manifest, artifact, self.verifier)
            expected_artifact_id = f"core-{self.host_platform}-{self.host_architecture}"
            if artifact.artifact_id != expected_artifact_id:
                raise BootstrapTrustError("The active slot is not the canonical Runtime core")
            if (
                artifact.platform != self.host_platform
                or artifact.architecture != self.host_architecture
            ):
                raise BootstrapTrustError("The active Runtime targets a different host")
            expected_identity = {
                "slot_id": slot_id,
                "release_id": manifest.release_id,
                "version": manifest.version,
                "build_digest": manifest.build_digest,
                "artifact_id": artifact.artifact_id,
                "artifact_sha256": artifact.sha256,
                "channel": manifest.channel.value,
            }
            if any(marker.get(key) != value for key, value in expected_identity.items()):
                raise BootstrapTrustError(
                    "The active slot identity is not bound to its signed release"
                )
            slot_path = self.slots.validate_receipt(
                slot_id=slot_id,
                manifest=manifest,
                artifact=artifact,
            )
            validate_installed_pack_set(
                slot_path,
                manifest,
                verifier=self.verifier,
                platform=self.host_platform,
                architecture=self.host_architecture,
                pack_content_verifier=self.pack_content_verifier,
            )
            after = _read_authoritative_pointers(self.slots.root)
            if after.current != slot_id or slot_id not in after.known_good:
                raise BootstrapTrustError("The active Runtime changed during verification")
            return VerifiedRuntimeSlot(
                slot_id=slot_id,
                slot_path=slot_path,
                payload_root=slot_path / "payload",
                manifest=manifest,
                artifact=artifact,
            )
        except BootstrapTrustError:
            raise
        except Exception as exc:
            raise BootstrapTrustError(
                "The active Runtime failed signed slot verification"
            ) from exc


def resolve_packaged_runtime(slot: VerifiedRuntimeSlot, host_platform: str) -> Path:
    """Resolve one canonical packaged Runtime executable, or fail closed.

    Two layouts are recognized because the deterministic release builder accepts
    either a Runtime source root (``bin``) or a product source root (``runtime/bin``).
    More than one candidate is deliberately treated as ambiguous.
    """

    suffix = ".exe" if host_platform == "windows" else ""
    relatives = (
        PurePosixPath(f"bin/ecorex{suffix}"),
        PurePosixPath(f"runtime/bin/ecorex{suffix}"),
    )
    existing: list[Path] = []
    for relative in relatives:
        candidate = slot.payload_root.joinpath(*relative.parts)
        if os.path.lexists(candidate):
            existing.append(candidate)
    if len(existing) != 1:
        raise RuntimeLaunchError(
            "The signed Runtime must contain exactly one canonical executable"
        )
    return existing[0]


class SubprocessRuntimeLauncher:
    """Launch a verified Runtime directly, never through a command shell."""

    def start(self, spec: RuntimeProcessSpec) -> RuntimeChild:
        _validate_executable(spec.executable, spec.cwd, spec.executable_fingerprint)
        try:
            process = subprocess.Popen(  # noqa: S603 - executable is signed and canonical
                list(spec.argv),
                executable=str(spec.executable),
                cwd=spec.cwd,
                env=dict(spec.environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
                start_new_session=os.name != "nt",
            )
            return _ManagedSubprocess(process)
        except OSError as exc:
            raise RuntimeLaunchError("The verified Runtime process could not start") from exc


class _ManagedSubprocess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def wait(self) -> int:
        return self._process.wait()

    def send_signal(self, signum: int) -> None:
        if os.name != "nt":
            try:
                os.killpg(self._process.pid, signum)
                return
            except ProcessLookupError:
                return
        if signum == int(signal.SIGINT):
            self._process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            return
        self._process.send_signal(signum)


class BootstrapSupervisor:
    """Supervise one active Runtime and only honor bounded activation restarts."""

    def __init__(
        self,
        install_root: str | os.PathLike[str],
        *,
        endpoint: RuntimeEndpoint = RuntimeEndpoint(),
        verifier: SignatureVerifier | None = None,
        launcher: RuntimeLauncher | None = None,
        executable_resolver: RuntimeExecutableResolver = resolve_packaged_runtime,
        host_platform: str | None = None,
        host_architecture: str | None = None,
        max_requested_restarts: int = 3,
        lock_timeout: float | None = 10.0,
        source_environment: Mapping[str, str] | None = None,
        acceptance_preview: bool = False,
        activation_health_probe: ActivationHealthProbe | None = None,
        activation_companion: ActivationCompanion | None = None,
        pack_content_verifier: PackContentVerifier | None = None,
    ) -> None:
        if (
            isinstance(max_requested_restarts, bool)
            or not isinstance(max_requested_restarts, int)
            or not 0 <= max_requested_restarts <= 10
        ):
            raise BootstrapConfigurationError(
                "max_requested_restarts must be between zero and ten"
            )
        if not callable(executable_resolver):
            raise BootstrapConfigurationError("executable resolver must be callable")
        if not isinstance(acceptance_preview, bool):
            raise BootstrapConfigurationError("acceptance_preview must be boolean")
        detected_platform, detected_architecture = detect_host_target()
        self.host_platform = host_platform or detected_platform
        self.host_architecture = host_architecture or detected_architecture
        try:
            root = Path(install_root)
        except TypeError as exc:
            raise BootstrapConfigurationError("Install root must be a filesystem path") from exc
        root = Path(os.path.abspath(root))
        try:
            self.slots = SlotStore(root)
            _require_safe_install_root(self.slots.root)
        except (OSError, StorageError, RuntimeLaunchError) as exc:
            raise BootstrapConfigurationError("Install root is unsafe") from exc
        release_verifier = verifier or RejectingSignatureVerifier()
        self.slot_verifier = CurrentSlotVerifier(
            self.slots,
            verifier=release_verifier,
            host_platform=self.host_platform,
            host_architecture=self.host_architecture,
            pack_content_verifier=pack_content_verifier,
        )
        self.endpoint = endpoint
        self.activation_health_probe = (
            activation_health_probe or LoopbackActivationHealthProbe()
        )
        if not callable(getattr(self.activation_health_probe, "probe", None)):
            raise BootstrapConfigurationError("activation health probe is invalid")
        if activation_companion is not None and any(
            not callable(getattr(activation_companion, method, None))
            for method in (
                "prepare_transaction",
                "commit_activation",
                "rollback_activation",
                "converge_activation",
            )
        ):
            raise BootstrapConfigurationError(
                "activation companion is invalid"
            )
        self.activation_companion = activation_companion
        self.activations = ProvisionalActivationController(
            self.slots.root,
            verifier=release_verifier,
            host_platform=self.host_platform,
            host_architecture=self.host_architecture,
            pack_content_verifier=pack_content_verifier,
        )
        self.launcher = launcher or SubprocessRuntimeLauncher()
        self.executable_resolver = executable_resolver
        self.max_requested_restarts = max_requested_restarts
        environment = dict(
            _sanitized_environment(
                os.environ if source_environment is None else source_environment
            )
        )
        if acceptance_preview:
            environment[RUNTIME_ACCEPTANCE_PREVIEW_ENV] = "1"
        self._environment = MappingProxyType(environment)
        self._selection_lock = ProductFileLock(
            self.slots.root / "install-update.lock",
            timeout=lock_timeout,
        )
        self._state_lock = threading.Lock()
        self._active_child: RuntimeChild | None = None
        self._stop_signal: int | None = None

    def run(self) -> BootstrapRunResult:
        launched: list[str] = []
        requested_restarts = 0
        previous_slot: str | None = None
        while True:
            with self._state_lock:
                stop_signal = self._stop_signal
            if stop_signal is not None:
                return self._stop_result(
                    stop_signal, launched, requested_restarts, runtime_exit_code=None
                )

            try:
                selected = self._select_and_start(disallow_slot=previous_slot)
            except (BootstrapTrustError, RuntimeLaunchError):
                if self._rollback_pending_activation("candidate_launch_failed"):
                    previous_slot = None
                    continue
                rolled_back, prior_slot = self._rollback_confirmed_activation(
                    "confirmed_runtime_launch_failed"
                )
                if rolled_back:
                    if prior_slot is not None:
                        previous_slot = None
                        continue
                    return BootstrapRunResult(
                        exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                        reason=BootstrapReason.RUNTIME_FAILED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                    )
                raise
            if selected is None:
                # The old process was already allowed to exit. Never relaunch it
                # if activation did not atomically advance the current pointer.
                return BootstrapRunResult(
                    exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                    reason=BootstrapReason.RESTART_WITHOUT_ACTIVATION,
                    launches=len(launched),
                    requested_restarts=requested_restarts,
                    launched_slots=tuple(launched),
                )
            child = selected.child
            slot_id = selected.slot_id
            launched.append(slot_id)
            with self._state_lock:
                self._active_child = child
                stop_signal = self._stop_signal
            if stop_signal is not None:
                self._forward_signal(child, stop_signal)
            if selected.provisional is not None:
                assert selected.health_nonce is not None
                healthy = False
                try:
                    healthy = self.activation_health_probe.probe(
                        self.endpoint,
                        selected.provisional,
                        selected.health_nonce,
                    ) is True
                except Exception:
                    healthy = False
                if not healthy:
                    self._stop_candidate(child)
                    # This candidate never becomes known-good. Discard the
                    # advisory file without allowing it to influence rollback.
                    read_startup_diagnostic(
                        self.slots.root, selected.startup_diagnostic_token
                    )
                    with self._state_lock:
                        self._active_child = None
                    if self._rollback_pending_activation("candidate_health_failed"):
                        previous_slot = None
                        continue
                    return BootstrapRunResult(
                        exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                        reason=BootstrapReason.RUNTIME_FAILED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                    )
                confirmed = False
                runtime_confirmed = False
                try:
                    with self._selection_lock:
                        if self.activation_companion is not None:
                            self.activation_companion.prepare_transaction(
                                selected.provisional.intent.transaction_id
                            )
                        self.activations.confirm(
                            selected.provisional.intent.transaction_id,
                            selected.provisional.intent.health_identity,
                        )
                        runtime_confirmed = True
                        if self.activation_companion is not None:
                            try:
                                self.activation_companion.commit_activation(
                                    selected.provisional.intent.transaction_id
                                )
                            except Exception:
                                # The new entry is already active.  Its durable
                                # transaction is converged on the next cold start.
                                pass
                    confirmed = True
                except Exception:
                    try:
                        with self._selection_lock:
                            confirmed = self.activations.reconcile_confirmation()
                            if self.activation_companion is not None:
                                if confirmed or runtime_confirmed:
                                    try:
                                        self.activation_companion.commit_activation(
                                            selected.provisional.intent.transaction_id
                                        )
                                    except Exception:
                                        pass
                                else:
                                    self.activation_companion.rollback_activation(
                                        selected.provisional.intent.transaction_id
                                    )
                    except Exception:
                        confirmed = False
                self._stop_candidate(child)
                read_startup_diagnostic(
                    self.slots.root, selected.startup_diagnostic_token
                )
                with self._state_lock:
                    self._active_child = None
                if not confirmed:
                    if self._rollback_pending_activation("candidate_confirmation_failed"):
                        previous_slot = None
                        continue
                    return BootstrapRunResult(
                        exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                        reason=BootstrapReason.RUNTIME_FAILED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                    )
                previous_slot = None
                continue
            try:
                runtime_code = child.wait()
            except Exception as exc:
                with self._state_lock:
                    self._active_child = None
                self._stop_candidate(child)
                rolled_back, prior_slot = self._rollback_confirmed_activation(
                    "confirmed_runtime_wait_failed"
                )
                if rolled_back:
                    if prior_slot is not None:
                        previous_slot = None
                        continue
                    return BootstrapRunResult(
                        exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                        reason=BootstrapReason.RUNTIME_FAILED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                    )
                raise RuntimeLaunchError(
                    "The verified Runtime process could not be observed"
                ) from exc
            if isinstance(runtime_code, bool) or not isinstance(runtime_code, int):
                with self._state_lock:
                    self._active_child = None
                rolled_back, prior_slot = self._rollback_confirmed_activation(
                    "confirmed_runtime_invalid_exit"
                )
                if rolled_back:
                    if prior_slot is not None:
                        previous_slot = None
                        continue
                    return BootstrapRunResult(
                        exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                        reason=BootstrapReason.RUNTIME_FAILED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                    )
                raise RuntimeLaunchError("The verified Runtime returned an invalid exit status")
            with self._state_lock:
                self._active_child = None
                stop_signal = self._stop_signal
            runtime_startup_stage = read_startup_diagnostic(
                self.slots.root, selected.startup_diagnostic_token
            )

            if stop_signal is not None:
                return self._stop_result(
                    stop_signal,
                    launched,
                    requested_restarts,
                    runtime_exit_code=runtime_code,
                    runtime_startup_stage=runtime_startup_stage,
                )
            rolled_back, prior_slot = self._rollback_confirmed_activation(
                f"confirmed_runtime_exit_{runtime_code}"
            )
            if rolled_back:
                if prior_slot is not None:
                    previous_slot = None
                    continue
                return BootstrapRunResult(
                    exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                    reason=BootstrapReason.RUNTIME_FAILED,
                    launches=len(launched),
                    requested_restarts=requested_restarts,
                    launched_slots=tuple(launched),
                    runtime_exit_code=runtime_code,
                    runtime_startup_stage=runtime_startup_stage,
                )
            if runtime_code == 0:
                return BootstrapRunResult(
                    exit_code=int(BootstrapExitCode.SUCCESS),
                    reason=BootstrapReason.RUNTIME_COMPLETED,
                    launches=len(launched),
                    requested_restarts=requested_restarts,
                    launched_slots=tuple(launched),
                    runtime_exit_code=0,
                    runtime_startup_stage=runtime_startup_stage,
                )
            if runtime_code not in {
                RUNTIME_RESTART_EXIT_CODE,
                RUNTIME_RELOAD_EXIT_CODE,
            }:
                if runtime_code < 0:
                    signum = min(max(abs(runtime_code), 1), 127)
                    return BootstrapRunResult(
                        exit_code=min(128 + signum, 255),
                        reason=BootstrapReason.RUNTIME_SIGNALLED,
                        launches=len(launched),
                        requested_restarts=requested_restarts,
                        launched_slots=tuple(launched),
                        runtime_exit_code=runtime_code,
                        runtime_startup_stage=runtime_startup_stage,
                    )
                return BootstrapRunResult(
                    exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                    reason=BootstrapReason.RUNTIME_FAILED,
                    launches=len(launched),
                    requested_restarts=requested_restarts,
                    launched_slots=tuple(launched),
                    runtime_exit_code=runtime_code,
                    runtime_startup_stage=runtime_startup_stage,
                )

            requested_restarts += 1
            if requested_restarts > self.max_requested_restarts:
                return BootstrapRunResult(
                    exit_code=int(BootstrapExitCode.RUNTIME_FAILURE),
                    reason=BootstrapReason.RESTART_LIMIT_REACHED,
                    launches=len(launched),
                    requested_restarts=requested_restarts,
                    launched_slots=tuple(launched),
                    runtime_exit_code=runtime_code,
                    runtime_startup_stage=runtime_startup_stage,
                )
            previous_slot = (
                slot_id if runtime_code == RUNTIME_RESTART_EXIT_CODE else None
            )

    def request_stop(self, signum: int) -> bool:
        allowed = {int(signal.SIGINT), int(signal.SIGTERM)}
        if hasattr(signal, "SIGBREAK"):
            allowed.add(int(signal.SIGBREAK))
        if isinstance(signum, bool) or not isinstance(signum, int) or signum not in allowed:
            raise BootstrapConfigurationError("Bootstrap stop signal is unsupported")
        with self._state_lock:
            first_request = self._stop_signal is None
            if first_request:
                self._stop_signal = signum
            child = self._active_child
        if child is not None:
            self._forward_signal(child, signum)
        return first_request

    def _select_and_start(
        self,
        *,
        disallow_slot: str | None,
    ) -> _SelectedRuntime | None:
        try:
            with self._selection_lock:
                self.activations.reconcile_confirmation()
                provisional = self.activations.ensure_pending_current()
                if provisional is None:
                    slot = self.slot_verifier.verify_current()
                else:
                    slot = VerifiedRuntimeSlot(
                        slot_id=provisional.intent.slot_id,
                        slot_path=provisional.slot_path,
                        payload_root=provisional.payload_root,
                        manifest=provisional.manifest,
                        artifact=provisional.artifact,
                    )
                if disallow_slot is not None and slot.slot_id == disallow_slot:
                    return None
                if provisional is not None:
                    _wait_for_loopback_port_release(self.endpoint)
                executable = self.executable_resolver(slot, self.host_platform)
                fingerprint = _validate_executable(executable, slot.payload_root)
                # Re-hash the retained receipt and every payload file after the
                # executable was resolved. This catches a resolver racing or
                # replacing content before process creation.
                self.slots.validate_receipt(
                    slot_id=slot.slot_id,
                    manifest=slot.manifest,
                    artifact=slot.artifact,
                )
                validate_installed_pack_set(
                    slot.slot_path,
                    slot.manifest,
                    verifier=self.slot_verifier.verifier,
                    platform=self.host_platform,
                    architecture=self.host_architecture,
                    pack_content_verifier=self.slot_verifier.pack_content_verifier,
                )
                fingerprint = _validate_executable(
                    executable, slot.payload_root, fingerprint
                )
                environment = dict(self._environment)
                health_nonce: str | None = None
                startup_diagnostic_token: str | None = None
                if prepare_startup_diagnostic_directory(self.slots.root):
                    startup_diagnostic_token = issue_startup_diagnostic_token()
                    environment[STARTUP_DIAGNOSTIC_TOKEN_ENV] = (
                        startup_diagnostic_token
                    )
                if provisional is not None:
                    health_nonce = secrets.token_urlsafe(32)
                    environment[ACTIVATION_TRANSACTION_ENV] = (
                        provisional.intent.transaction_id
                    )
                    environment[ACTIVATION_NONCE_ENV] = health_nonce
                spec = RuntimeProcessSpec(
                    slot_id=slot.slot_id,
                    executable=executable,
                    argv=(
                        str(executable),
                        "serve",
                        "--host",
                        self.endpoint.host,
                        "--port",
                        str(self.endpoint.port),
                    ),
                    cwd=slot.payload_root,
                    environment=MappingProxyType(environment),
                    executable_fingerprint=fingerprint,
                )
                _validate_argv(spec.argv)
                try:
                    child = self.launcher.start(spec)
                except RuntimeLaunchError:
                    raise
                except Exception as exc:
                    raise RuntimeLaunchError(
                        "The verified Runtime process could not start"
                    ) from exc
                if not callable(getattr(child, "wait", None)) or not callable(
                    getattr(child, "send_signal", None)
                ):
                    raise RuntimeLaunchError("Runtime launcher returned an invalid child")
                return _SelectedRuntime(
                    child=child,
                    slot_id=slot.slot_id,
                    provisional=provisional,
                    health_nonce=health_nonce,
                    startup_diagnostic_token=startup_diagnostic_token,
                )
        except (BootstrapTrustError, RuntimeLaunchError):
            raise
        except (
            ActivationIntentError,
            StorageError,
            VerificationError,
            OSError,
            ValueError,
        ) as exc:
            raise BootstrapTrustError(
                "Runtime selection changed or failed verification before launch"
            ) from exc

    def _rollback_pending_activation(self, error_code: str) -> bool:
        try:
            with self._selection_lock:
                intent = self.activations.load_intent(required=False)
                if intent is None:
                    return False
                self.activations.fail_pre_data(
                    intent.transaction_id,
                    error_code=error_code,
                )
                return self.slots.pointers().current is not None
        except Exception:
            return False

    def _rollback_confirmed_activation(
        self, error_code: str
    ) -> tuple[bool, str | None]:
        """Rollback a confirmed candidate only while its data barrier is false."""

        try:
            with self._selection_lock:
                current = self.slots.pointers().current
                if current is None:
                    return False, None
                return self.activations.rollback_confirmed_pre_data(
                    current,
                    error_code=error_code,
                )
        except Exception:
            # Any receipt, signature, journal, or pointer disagreement fails
            # closed.  The caller reports the Runtime failure and leaves the
            # signed current slot in place for roll-forward repair.
            return False, None

    @staticmethod
    def _stop_candidate(child: RuntimeChild) -> None:
        try:
            child.send_signal(int(signal.SIGTERM))
        except (OSError, ProcessLookupError, ValueError):
            pass
        try:
            child.wait()
        except Exception:
            pass

    def _stop_result(
        self,
        signum: int,
        launched: Sequence[str],
        requested_restarts: int,
        *,
        runtime_exit_code: int | None,
        runtime_startup_stage: str | None = None,
    ) -> BootstrapRunResult:
        normalized_signal = min(max(abs(signum), 1), 127)
        return BootstrapRunResult(
            exit_code=min(128 + normalized_signal, 255),
            reason=BootstrapReason.STOP_REQUESTED,
            launches=len(launched),
            requested_restarts=requested_restarts,
            launched_slots=tuple(launched),
            runtime_exit_code=runtime_exit_code,
            runtime_startup_stage=runtime_startup_stage,
        )

    @staticmethod
    def _forward_signal(child: RuntimeChild, signum: int) -> None:
        try:
            child.send_signal(signum)
        except (OSError, ProcessLookupError, ValueError):
            pass


def _validate_argv(argv: Sequence[str]) -> None:
    if len(argv) != 6:
        raise RuntimeLaunchError("Runtime command has an invalid argument contract")
    if argv[1] != "serve" or argv[2] != "--host" or argv[4] != "--port":
        raise RuntimeLaunchError("Runtime command has an invalid argument contract")
    for value in argv:
        if not isinstance(value, str) or not value or len(value) > 32_768:
            raise RuntimeLaunchError("Runtime command contains an invalid argument")
        if any(ord(character) < 32 for character in value):
            raise RuntimeLaunchError("Runtime command contains a control character")


def _wait_for_loopback_port_release(
    endpoint: RuntimeEndpoint,
    *,
    timeout_seconds: float = 3.0,
) -> None:
    """Fence an orphaned probe after a Bootstrap crash before relaunch."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        connection: socket.socket | None = None
        try:
            connection = socket.create_connection(
                (endpoint.host, endpoint.port), timeout=0.1
            )
        except OSError:
            return
        finally:
            if connection is not None:
                connection.close()
        if time.monotonic() >= deadline:
            raise RuntimeLaunchError(
                "Activation endpoint is still occupied by an untrusted process"
            )
        time.sleep(0.05)


def _validate_executable(
    executable: Path,
    payload_root: Path,
    expected: _ExecutableFingerprint | None = None,
) -> _ExecutableFingerprint:
    try:
        executable = Path(executable)
    except TypeError as exc:
        raise RuntimeLaunchError("Runtime executable resolver returned an invalid path") from exc
    _require_real_directory(payload_root, "Runtime payload")
    try:
        executable.resolve(strict=False).relative_to(payload_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RuntimeLaunchError("Runtime executable escapes the signed payload") from exc
    current = executable
    while True:
        _reject_link_or_reparse(current)
        if current == payload_root:
            break
        if current.parent == current:
            raise RuntimeLaunchError("Runtime executable has no signed payload ancestor")
        current = current.parent
    try:
        metadata = executable.lstat()
    except OSError as exc:
        raise RuntimeLaunchError("Runtime executable is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeLaunchError("Runtime executable is not a regular file")
    if os.name != "nt" and metadata.st_mode & 0o111 == 0:
        raise RuntimeLaunchError("Runtime executable is not marked executable")
    digest = hashlib.sha256()
    try:
        with executable.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeLaunchError("Runtime executable changed while opening")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise RuntimeLaunchError("Runtime executable is unreadable") from exc
    fingerprint = _ExecutableFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        sha256=digest.hexdigest(),
    )
    if (
        after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
        or (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
        or (expected is not None and fingerprint != expected)
    ):
        raise RuntimeLaunchError("Runtime executable changed before process creation")
    return fingerprint


def _require_real_directory(path: Path, label: str) -> None:
    _reject_link_or_reparse(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeLaunchError(f"{label} is missing") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLaunchError(f"{label} is not a real directory")


def _reject_link_or_reparse(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeLaunchError("Runtime path is missing") from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
        raise RuntimeLaunchError("Runtime path cannot contain links or reparse points")


def _sanitized_environment(source: Mapping[str, str]) -> Mapping[str, str]:
    allowlist = {
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
    clean = {
        key.upper(): value
        for key, value in source.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key.upper() in allowlist
        and "\0" not in key
        and "\0" not in value
    }
    system_root = clean.get("SYSTEMROOT", "")
    clean["PATH"] = (
        os.pathsep.join(
            filter(
                None,
                (
                    system_root + "\\System32"
                    if os.name == "nt" and system_root
                    else "",
                    "/usr/bin" if os.name != "nt" else "",
                    "/bin" if os.name != "nt" else "",
                ),
            )
        )
    )
    clean["ECOREX_BOOTSTRAPPED"] = "1"
    owner_nonce = source.get(RUNTIME_OWNER_NONCE_ENV)
    if owner_nonce is not None:
        if (
            not isinstance(owner_nonce, str)
            or _RUNTIME_OWNER_NONCE.fullmatch(owner_nonce) is None
        ):
            raise BootstrapConfigurationError("Runtime owner nonce is invalid")
        clean[RUNTIME_OWNER_NONCE_ENV] = owner_nonce
    # A verified side-by-side slot is immutable.  Importing packaged Python
    # must never create or replace ``__pycache__`` inside the signed payload.
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean["PYTHONNOUSERSITE"] = "1"
    # Runtime calendar projections must use the signed IANA database shipped
    # in Core on Windows and macOS. An empty path makes zoneinfo fall back to
    # the bundled tzdata package instead of mutable host data.
    clean["PYTHONTZPATH"] = ""
    return MappingProxyType(clean)


def _read_authoritative_pointers(root: Path) -> SlotPointers:
    """Read the atomic pointer record without following a replacement link."""

    path = root / "slot-pointers.json"
    try:
        before = path.lstat()
    except OSError as exc:
        raise BootstrapTrustError("The authoritative slot pointer is missing") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or bool(attributes & reparse_flag)
        or before.st_size > 64 * 1024
    ):
        raise BootstrapTrustError("The authoritative slot pointer is unsafe")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise BootstrapTrustError(
                    "The authoritative slot pointer changed while opening"
                )
            payload = stream.read(64 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError as exc:
        raise BootstrapTrustError("The authoritative slot pointer is unreadable") from exc
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(payload) > 64 * 1024
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity
    ):
        raise BootstrapTrustError("The authoritative slot pointer changed while reading")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapTrustError("The authoritative slot pointer is invalid") from exc
    if not isinstance(raw, Mapping):
        raise BootstrapTrustError("The authoritative slot pointer is invalid")
    try:
        return SlotPointers.from_dict(raw)
    except StorageError as exc:
        raise BootstrapTrustError("The authoritative slot pointer is invalid") from exc


def _require_safe_install_root(root: Path) -> None:
    """Reject aliasable parents so the product lock and slots share one identity."""

    current = root
    while True:
        _reject_link_or_reparse(current)
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise RuntimeLaunchError("Install root ancestry is not a directory")
        except OSError as exc:
            raise RuntimeLaunchError("Install root ancestry is unreadable") from exc
        if current.parent == current:
            return
        current = current.parent
