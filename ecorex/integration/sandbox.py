"""Trusted OS sandbox boundary for process capability packs.

The signed ``sandbox`` capability pack is application code, not a security
boundary.  It therefore runs *inside* an independently probed OS sandbox for
the default workspace-write profile.  Platforms without a verified backend
remain unavailable instead of receiving a decorative profile string.

Windows support intentionally requires the product-owned AppContainer helper
to be supplied with its signed-release SHA-256.  This Python package does not
pretend that ``CREATE_NEW_PROCESS_GROUP`` or a Job Object restricts filesystem
or network access.  macOS can use the operating-system ``sandbox-exec`` policy
engine after a behavioral probe.  Neither backend is reported ready until it
proves the complete contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from typing import Any, Mapping, Protocol

from .windows_path_identity import windows_invariant_path_key


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SANDBOX_LAUNCH_PROTOCOL = "ecorex-sandbox-launch-v1"
WINDOWS_PROCESS_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
WINDOWS_JOB_MEMORY_LIMIT_BYTES = 768 * 1024 * 1024
WINDOWS_CPU_RATE_HARD_CAP = 8000


@dataclass(frozen=True, slots=True)
class SandboxProbe:
    backend_id: str
    platform: str
    ready: bool
    reason: str
    filesystem_read_scope: str = "unverified"
    filesystem_write_scoped: bool = False
    network_denied: bool = False
    process_tree_contained: bool = False

    @property
    def effective_filesystem_read_scope(self) -> str:
        if self.filesystem_read_scope in {
            "host-unrestricted",
            "workspace-and-runtime",
        }:
            return self.filesystem_read_scope
        return "unverified"

    @property
    def complete(self) -> bool:
        return self.ready and self.reason == "ready" and all(
            (
                self.effective_filesystem_read_scope != "unverified",
                self.filesystem_write_scoped,
                self.network_denied,
                self.process_tree_contained,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "platform": self.platform,
            "ready": self.complete,
            "reason": self.reason,
            "filesystem_read_scoped": (
                self.effective_filesystem_read_scope == "workspace-and-runtime"
            ),
            "filesystem_read_scope": self.effective_filesystem_read_scope,
            "filesystem_write_scoped": self.filesystem_write_scoped,
            "network_denied": self.network_denied,
            "process_tree_contained": self.process_tree_contained,
        }


@dataclass(frozen=True, slots=True)
class SandboxLaunchPlan:
    argv: tuple[str, ...]
    backend_id: str

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("sandbox launch argv is invalid")
        if not self.backend_id:
            raise ValueError("sandbox backend identity is required")


class SandboxBackend(Protocol):
    """Core-owned launcher contract; capability-pack code cannot implement it."""

    def probe(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
    ) -> SandboxProbe: ...

    def launch_plan(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
        timeout_seconds: float,
        output_limit_bytes: int,
        profile: str,
    ) -> SandboxLaunchPlan: ...


class UnavailableSandboxBackend:
    def __init__(self, reason: str, *, platform: str | None = None) -> None:
        self.reason = _safe_reason(reason)
        self.platform = str(platform or sys.platform)

    def probe(self, **_kwargs: Any) -> SandboxProbe:
        return SandboxProbe(
            backend_id="unavailable",
            platform=self.platform,
            ready=False,
            reason=self.reason,
        )

    def launch_plan(self, **_kwargs: Any) -> SandboxLaunchPlan:
        raise RuntimeError(self.reason)


def probe_windows_appcontainer_helper(
    helper_path: Path | str,
    *,
    expected_sha256: str,
    workspace_roots: tuple[Path, ...],
) -> SandboxProbe:
    """Behaviorally attest a staged helper before slot security is provisioned.

    This probe proves the native AppContainer and Job Object implementation.
    It deliberately cannot produce a launch plan; runtime execution still
    requires the installed-slot security receipt enforced by
    :class:`WindowsAppContainerSandboxBackend`.
    """

    digest = str(expected_sha256).lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError("sandbox helper digest is invalid")
    helper = _trusted_regular_file(Path(helper_path))
    if _sha256_file(helper) != digest:
        raise ValueError("sandbox helper digest does not match signed identity")
    command: tuple[str, ...] = (
        str(helper),
        "probe",
        "--protocol",
        SANDBOX_LAUNCH_PROTOCOL,
        "--workspace-digest",
        _roots_digest(workspace_roots),
    )
    for root in workspace_roots:
        command += ("--workspace", str(root))
    try:
        completed = _run_bounded_probe(command, timeout_seconds=10)
        if completed is None:
            raise ValueError("bounded probe failed")
        value = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        value = None
        completed = None
    required = {
        "protocol": SANDBOX_LAUNCH_PROTOCOL,
        "backend": "windows-appcontainer",
        "cpu_rate_hard_cap": WINDOWS_CPU_RATE_HARD_CAP,
        "filesystem_read_scoped": True,
        "filesystem_write_scoped": True,
        "job_memory_limit_bytes": WINDOWS_JOB_MEMORY_LIMIT_BYTES,
        "network_denied": True,
        "process_memory_limit_bytes": WINDOWS_PROCESS_MEMORY_LIMIT_BYTES,
        "process_tree_contained": True,
        "workspace_roots_sha256": _roots_digest(workspace_roots),
    }
    ready = bool(
        completed is not None
        and completed.returncode == 0
        and isinstance(value, Mapping)
        and set(value) == set(required)
        and all(value.get(key) == expected for key, expected in required.items())
        and completed.stdout
        == json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return SandboxProbe(
        backend_id="windows-appcontainer",
        platform="windows",
        ready=ready,
        reason="ready" if ready else "windows_appcontainer_probe_failed",
        filesystem_read_scope=("workspace-and-runtime" if ready else "unverified"),
        filesystem_write_scoped=ready,
        network_denied=ready,
        process_tree_contained=ready,
    )


class WindowsAppContainerSandboxBackend:
    """Adapter for the product-owned AppContainer launcher.

    The helper is part of the immutable Runtime slot.  Callers must pass the
    digest from that signed slot manifest; neither PATH nor environment lookup
    is accepted.  The helper probe must attest all four enforcement features.
    """

    def __init__(
        self,
        helper_path: Path | str,
        *,
        expected_sha256: str,
        security_receipt: Mapping[str, Any] | None = None,
    ) -> None:
        digest = str(expected_sha256).lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("sandbox helper digest is invalid")
        self.helper_path = _trusted_regular_file(Path(helper_path))
        if _sha256_file(self.helper_path) != digest:
            raise ValueError("sandbox helper digest does not match signed identity")
        self.expected_sha256 = digest
        self.payload_root = self.helper_path.parent.parent
        self.slot_root = self.payload_root.parent
        self.install_root = self.slot_root.parent.parent
        self.read_roots = (self.payload_root,)
        receipt = dict(security_receipt or {})
        self.security_receipt = receipt
        self._security_identity_ready = bool(
            self.helper_path.name == "ecorex-sandbox-host.exe"
            and self.helper_path.parent == self.payload_root / "bin"
            and self.payload_root.name == "payload"
            and self.slot_root.parent.name == "slots"
            and self.install_root.is_dir()
            and all(root.is_dir() for root in self.read_roots)
            and receipt.get("schema_version") == 1
            and receipt.get("contract")
            == "windows-appcontainer-stable-provision-v3"
            and receipt.get("helper_sha256") == digest
            and isinstance(receipt.get("slot_digest"), str)
            and _SHA256.fullmatch(str(receipt.get("slot_digest"))) is not None
            and isinstance(receipt.get("root_security_sha256"), str)
            and _SHA256.fullmatch(str(receipt.get("root_security_sha256"))) is not None
        )
        self._last_probe: SandboxProbe | None = None

    def probe(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
    ) -> SandboxProbe:
        del python_executable, artifact_path
        if (
            not self._security_identity_ready
            or self.security_receipt.get("workspace_roots_sha256")
            != _roots_digest(workspace_roots)
            or self.security_receipt.get("permission_domain_sha256")
            != _permission_domain_digest(workspace_roots)
        ):
            self._last_probe = SandboxProbe(
                backend_id="windows-appcontainer",
                platform="windows",
                ready=False,
                reason="windows_appcontainer_security_receipt_invalid",
            )
            return self._last_probe
        self._last_probe = probe_windows_appcontainer_helper(
            self.helper_path,
            expected_sha256=self.expected_sha256,
            workspace_roots=workspace_roots,
        )
        return self._last_probe

    def launch_plan(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
        timeout_seconds: float,
        output_limit_bytes: int,
        profile: str,
    ) -> SandboxLaunchPlan:
        if self._last_probe is None or not self._last_probe.complete:
            raise RuntimeError("windows_appcontainer_not_probed")
        current_helper = _trusted_regular_file(self.helper_path)
        if (
            current_helper != self.helper_path
            or _sha256_file(current_helper) != self.expected_sha256
        ):
            raise RuntimeError("windows_appcontainer_helper_identity_changed")
        if profile not in {"workspace-write", "danger-full-access"}:
            raise RuntimeError("windows_sandbox_profile_invalid")
        argv = [
            str(self.helper_path),
            "run",
            "--protocol",
            SANDBOX_LAUNCH_PROTOCOL,
            "--profile",
            profile,
            "--network",
            "deny" if profile == "workspace-write" else "allow",
            "--timeout-ms",
            str(max(1, int(timeout_seconds * 1000) - 500)),
            "--output-limit",
            str(output_limit_bytes),
            "--process-memory-limit",
            str(WINDOWS_PROCESS_MEMORY_LIMIT_BYTES),
            "--job-memory-limit",
            str(WINDOWS_JOB_MEMORY_LIMIT_BYTES),
            "--cpu-rate",
            str(WINDOWS_CPU_RATE_HARD_CAP),
            "--workspace-digest",
            _roots_digest(workspace_roots),
            "--artifact-sha256",
            _sha256_file(artifact_path),
            "--slot-digest",
            str(self.security_receipt["slot_digest"]),
            "--security-digest",
            str(self.security_receipt["root_security_sha256"]),
            "--install-root",
            str(self.install_root),
            "--slot-root",
            str(self.slot_root),
        ]
        for root in self.read_roots:
            argv.extend(("--read-root", str(root)))
        for root in workspace_roots:
            argv.extend(("--workspace", str(root)))
        argv.extend(("--", str(python_executable), "-I", str(artifact_path)))
        return SandboxLaunchPlan(tuple(argv), "windows-appcontainer")


class MacOSSandboxExecBackend:
    """Seatbelt launcher with a behavioral, fail-closed probe."""

    def __init__(self, executable: Path | str = "/usr/bin/sandbox-exec") -> None:
        self.executable = Path(executable)
        self._last_probe: SandboxProbe | None = None

    def probe(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
    ) -> SandboxProbe:
        if sys.platform != "darwin":
            return SandboxProbe(
                backend_id="macos-seatbelt",
                platform=sys.platform,
                ready=False,
                reason="macos_sandbox_backend_wrong_platform",
            )
        try:
            executable = _trusted_system_file(self.executable)
        except ValueError:
            return SandboxProbe(
                backend_id="macos-seatbelt",
                platform="macos",
                ready=False,
                reason="macos_sandbox_exec_untrusted",
            )
        root = workspace_roots[0]
        outside_unchanged = False
        child_marker_valid = False
        listener: socket.socket | None = None
        outside: Path | None = None
        completed: _BoundedProcessResult | None = None
        value: Any = {}
        script_started = False
        try:
            with tempfile.TemporaryDirectory(prefix=".ecorex-sandbox-probe-", dir=root) as raw:
                probe_root = Path(raw).resolve(strict=True)
                child_marker = probe_root / "child-started"
                outside = Path(tempfile.gettempdir()).resolve() / (
                    "ecorex-sandbox-outside-" + os.urandom(8).hex()
                )
                if any(outside.is_relative_to(item.resolve(strict=True)) for item in workspace_roots):
                    raise ValueError("outside canary overlaps workspace")
                outside_canary = os.urandom(32).hex()
                outside.write_text(outside_canary, encoding="utf-8")
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                network_port = int(listener.getsockname()[1])
                child_script = (
                    "import json,os,pathlib,sys\n"
                    "marker=pathlib.Path(sys.argv[1])\n"
                    "outside=pathlib.Path(sys.argv[2])\n"
                    "flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)\n"
                    "fd=os.open(marker,flags,0o600)\n"
                    "try:\n"
                    " os.write(fd,b'started')\n"
                    "finally:\n"
                    " os.close(fd)\n"
                    "try:\n"
                    " outside.write_text('child')\n"
                    " write_errno=0\n"
                    "except OSError as exc:\n"
                    " write_errno=exc.errno\n"
                    "print(json.dumps({'outside_write_errno':write_errno},"
                    "sort_keys=True,separators=(',',':')))\n"
                )
                script = textwrap.dedent(
                    """
                    import hashlib
                    import json
                    import pathlib
                    import socket
                    import subprocess
                    import sys

                    sys.stdout.write("ecorex-macos-seatbelt-probe-v1\\n")
                    sys.stdout.flush()
                    phase = "initialization"
                    try:
                        inside = pathlib.Path(sys.argv[1])
                        outside = pathlib.Path(sys.argv[2])
                        marker = inside / "child-started"
                        port = int(sys.argv[3])
                        child_code = sys.argv[4]
                        result = {}

                        phase = "outside_read"
                        try:
                            result["outside_read_match"] = (
                                hashlib.sha256(outside.read_bytes()).hexdigest()
                                == sys.argv[5]
                            )
                        except OSError:
                            result["outside_read_match"] = False

                        phase = "outside_write"
                        try:
                            outside.write_text("x")
                            result["outside_write_errno"] = 0
                        except OSError as exc:
                            result["outside_write_errno"] = exc.errno

                        phase = "network"
                        network_socket = None
                        result["network_close_ok"] = True
                        try:
                            network_socket = socket.socket()
                            result["network_errno"] = network_socket.connect_ex(
                                ("127.0.0.1", port)
                            )
                        except OSError as exc:
                            result["network_errno"] = exc.errno
                        finally:
                            if network_socket is not None:
                                try:
                                    network_socket.close()
                                except OSError:
                                    result["network_close_ok"] = False

                        phase = "workspace_write"
                        try:
                            (inside / "ok").write_text("ok")
                            result["inside_write"] = True
                        except Exception:
                            result["inside_write"] = False

                        phase = "child_launch"
                        try:
                            child = subprocess.run(
                                [
                                    sys.executable,
                                    "-I",
                                    "-c",
                                    child_code,
                                    str(marker),
                                    str(outside),
                                ],
                                capture_output=True,
                            )
                            result["child_launch_errno"] = 0
                        except OSError as exc:
                            child = None
                            result["child_launch_errno"] = exc.errno

                        phase = "child_evidence"
                        result["child_returncode"] = (
                            child.returncode if child is not None else None
                        )
                        result["child_started"] = marker.is_file()
                        try:
                            child_value = (
                                json.loads(child.stdout.decode("utf-8"))
                                if child is not None
                                else {}
                            )
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            child_value = {}
                        if not isinstance(child_value, dict):
                            child_value = {}
                        result["child_write_errno"] = child_value.get(
                            "outside_write_errno"
                        )
                        output = result
                    except BaseException:
                        output = {"fatal_phase": phase}
                    try:
                        output_line = json.dumps(
                            output, sort_keys=True, separators=(",", ":")
                        )
                    except BaseException:
                        output_line = '{"fatal_phase":"emit"}'
                    sys.stdout.write(output_line + "\\n")
                    """
                ).lstrip()
                policy = self._policy(
                    workspace_roots=workspace_roots,
                    python_executable=python_executable,
                    artifact_path=artifact_path,
                )
                completed = _run_bounded_probe(
                    (
                        str(executable),
                        "-p",
                        policy,
                        str(python_executable),
                        "-I",
                        "-c",
                        script,
                        str(probe_root),
                        str(outside),
                        str(network_port),
                        child_script,
                        hashlib.sha256(outside_canary.encode("utf-8")).hexdigest(),
                    ),
                    timeout_seconds=10,
                )
                if completed is not None:
                    lines = completed.stdout.splitlines()
                    script_started = bool(
                        lines
                        and lines[0] == b"ecorex-macos-seatbelt-probe-v1"
                    )
                    try:
                        value = json.loads(lines[1].decode("utf-8"))
                        if len(lines) != 2:
                            value = {}
                    except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
                        value = {}
                    try:
                        outside_unchanged = (
                            outside.read_text(encoding="utf-8") == outside_canary
                        )
                    except OSError:
                        outside_unchanged = False
                    child_marker_valid = _regular_file_bytes_equal(
                        child_marker, b"started"
                    )
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        finally:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            try:
                if outside is not None:
                    outside.unlink(missing_ok=True)
            except OSError:
                pass
        reason = _macos_probe_failure_reason(
            completed,
            value,
            outside_unchanged=outside_unchanged,
            child_marker_valid=child_marker_valid,
            script_started=script_started,
        )
        ready = reason == "ready"
        self._last_probe = SandboxProbe(
            backend_id="macos-seatbelt",
            platform="macos",
            ready=ready,
            reason=reason,
            filesystem_read_scope=("host-unrestricted" if ready else "unverified"),
            filesystem_write_scoped=ready,
            network_denied=ready,
            process_tree_contained=ready,
        )
        return self._last_probe
    def launch_plan(
        self,
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
        timeout_seconds: float,
        output_limit_bytes: int,
        profile: str,
    ) -> SandboxLaunchPlan:
        del timeout_seconds, output_limit_bytes
        if profile != "workspace-write":
            raise RuntimeError("macos_seatbelt_profile_invalid")
        if self._last_probe is None or not self._last_probe.complete:
            raise RuntimeError("macos_seatbelt_not_probed")
        policy = self._policy(
            workspace_roots=workspace_roots,
            python_executable=python_executable,
            artifact_path=artifact_path,
        )
        return SandboxLaunchPlan(
            (
                str(self.executable),
                "-p",
                policy,
                str(python_executable),
                "-I",
                str(artifact_path),
            ),
            "macos-seatbelt",
        )

    @staticmethod
    def _policy(
        *,
        workspace_roots: tuple[Path, ...],
        python_executable: Path,
        artifact_path: Path,
    ) -> str:
        # workspace-write deliberately permits reads while restricting writes
        # to the selected workspaces and denying network.  This matches the
        # user-facing profile and avoids a brittle, interpreter-specific read
        # allowlist that can prevent signed Python builds from starting.
        del python_executable, artifact_path
        workspace_rules = [
            f'(subpath "{_seatbelt_escape(str(root))}")' for root in workspace_roots
        ]
        return " ".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process*)",
                "(allow sysctl-read)",
                "(allow mach-lookup)",
                "(allow file-read*)",
                "(allow file-write* " + " ".join(workspace_rules) + ")",
                "(deny network*)",
            )
        )


def default_workspace_sandbox_backend() -> SandboxBackend:
    if sys.platform == "darwin":
        return MacOSSandboxExecBackend()
    if os.name == "nt":
        return UnavailableSandboxBackend(
            "windows_appcontainer_helper_not_configured", platform="windows"
        )
    return UnavailableSandboxBackend(
        "verified_workspace_sandbox_unavailable", platform=sys.platform
    )


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _macos_probe_result_complete(
    completed: _BoundedProcessResult | None,
    value: Any,
    *,
    outside_unchanged: bool,
    child_marker_valid: bool,
    script_started: bool = True,
) -> bool:
    return (
        _macos_probe_failure_reason(
            completed,
            value,
            outside_unchanged=outside_unchanged,
            child_marker_valid=child_marker_valid,
            script_started=script_started,
        )
        == "ready"
    )


def _macos_probe_failure_reason(
    completed: _BoundedProcessResult | None,
    value: Any,
    *,
    outside_unchanged: bool,
    child_marker_valid: bool,
    script_started: bool = True,
) -> str:
    expected_keys = {
        "child_launch_errno",
        "child_returncode",
        "child_started",
        "child_write_errno",
        "inside_write",
        "network_errno",
        "network_close_ok",
        "outside_read_match",
        "outside_write_errno",
    }
    if completed is None:
        return "macos_seatbelt_probe_process_unavailable"
    if completed.returncode != 0:
        if not script_started:
            return "macos_seatbelt_probe_interpreter_start_failed"
        return "macos_seatbelt_probe_process_nonzero"
    if not script_started:
        return "macos_seatbelt_probe_handshake_missing"
    fatal_phases = {
        "child_evidence",
        "child_launch",
        "emit",
        "initialization",
        "network",
        "outside_read",
        "outside_write",
        "workspace_write",
    }
    if (
        isinstance(value, dict)
        and set(value) == {"fatal_phase"}
        and type(value["fatal_phase"]) is str
        and value["fatal_phase"] in fatal_phases
    ):
        return f"macos_seatbelt_probe_{value['fatal_phase']}_failed"
    if not isinstance(value, dict) or set(value) != expected_keys:
        return "macos_seatbelt_probe_evidence_invalid"
    if not _is_zero_errno(value["child_launch_errno"]):
        return "macos_seatbelt_probe_child_launch_failed"
    if (
        type(value["child_returncode"]) is not int
        or value["child_returncode"] != 0
    ):
        return "macos_seatbelt_probe_child_nonzero"
    if value["child_started"] is not True:
        return "macos_seatbelt_probe_child_not_started"
    if not _is_denial_errno(value["child_write_errno"]):
        return "macos_seatbelt_probe_child_denial_unproven"
    if value["inside_write"] is not True:
        return "macos_seatbelt_probe_workspace_write_failed"
    if not _is_denial_errno(value["network_errno"]):
        return "macos_seatbelt_probe_network_denial_unproven"
    if value["network_close_ok"] is not True:
        return "macos_seatbelt_probe_network_cleanup_failed"
    if value["outside_read_match"] is not True:
        return "macos_seatbelt_probe_read_policy_unproven"
    if not _is_denial_errno(value["outside_write_errno"]):
        return "macos_seatbelt_probe_write_denial_unproven"
    if not outside_unchanged:
        return "macos_seatbelt_probe_canary_changed"
    if not child_marker_valid:
        return "macos_seatbelt_probe_child_marker_invalid"
    return "ready"


def _is_denial_errno(value: Any) -> bool:
    return type(value) is int and value in {errno.EACCES, errno.EPERM}


def _is_zero_errno(value: Any) -> bool:
    return type(value) is int and value == 0


def _regular_file_bytes_equal(path: Path, expected: bytes) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        try:
            status = os.fstat(descriptor)
            actual = os.read(descriptor, len(expected) + 1)
        except OSError:
            return False
        if not stat.S_ISREG(status.st_mode):
            return False
        return actual == expected
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _run_bounded_probe(
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
    stdout_limit: int = 16 * 1024,
    stderr_limit: int = 16 * 1024,
) -> _BoundedProcessResult | None:
    """Run one probe without allowing a hostile helper to buffer unbounded output."""

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except OSError:
        return None
    assert process.stdout is not None and process.stderr is not None
    overflow = threading.Event()
    transport_failed = threading.Event()
    output: dict[str, bytes] = {}

    def read(name: str, stream: Any, limit: int) -> None:
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = stream.read(min(16 * 1024, limit + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    overflow.set()
                    break
                chunks.append(chunk)
        except OSError:
            transport_failed.set()
        finally:
            output[name] = b"".join(chunks)
            stream.close()

    stdout_reader = threading.Thread(
        target=read, args=("stdout", process.stdout, stdout_limit), daemon=True
    )
    stderr_reader = threading.Thread(
        target=read, args=("stderr", process.stderr, stderr_limit), daemon=True
    )
    stdout_reader.start()
    stderr_reader.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            if overflow.is_set() or transport_failed.is_set() or time.monotonic() >= deadline:
                _terminate_probe_process(process)
                break
            time.sleep(0.01)
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _terminate_probe_process(process)
            returncode = process.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        _terminate_probe_process(process)
        return None
    stdout_reader.join(timeout=2)
    stderr_reader.join(timeout=2)
    if (
        stdout_reader.is_alive()
        or stderr_reader.is_alive()
        or overflow.is_set()
        or transport_failed.is_set()
        or time.monotonic() >= deadline and returncode != 0
    ):
        return None
    return _BoundedProcessResult(
        returncode=returncode,
        stdout=output.get("stdout", b""),
        stderr=output.get("stderr", b""),
    )


def _terminate_probe_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _roots_digest(roots: tuple[Path, ...]) -> str:
    value = "\0".join(str(root) for root in roots).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _permission_domain_digest(roots: tuple[Path, ...]) -> str:
    """Return the order-insensitive AppContainer permission-domain identity."""

    values = sorted({windows_invariant_path_key(root) for root in roots})
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_regular_file(path: Path) -> Path:
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("sandbox helper is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
    ):
        raise ValueError("sandbox helper is not a trusted regular file")
    return resolved


def _trusted_system_file(path: Path) -> Path:
    resolved = _trusted_regular_file(path)
    info = resolved.stat()
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("sandbox executable is not root-owned and immutable")
    return resolved


def _safe_reason(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.:-]+", "_", str(value).casefold()).strip("_")
    return normalized[:128] or "sandbox_unavailable"


def _seatbelt_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "MacOSSandboxExecBackend",
    "SANDBOX_LAUNCH_PROTOCOL",
    "SandboxBackend",
    "SandboxLaunchPlan",
    "SandboxProbe",
    "UnavailableSandboxBackend",
    "WindowsAppContainerSandboxBackend",
    "default_workspace_sandbox_backend",
]
