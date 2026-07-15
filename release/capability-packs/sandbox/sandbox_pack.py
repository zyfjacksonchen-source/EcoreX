"""Strict shell command adapter; the OS boundary is always owned by Core."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Mapping

from ecorex_pack_protocol import (
    ContractError,
    Request,
    bounded_int,
    bounded_text,
    require_exact_arguments,
)


_MAX_STDOUT = 512 * 1024
_MAX_STDERR = 32 * 1024
_READ_CHUNK = 64 * 1024
_PROCESS_SETTLE_SECONDS = 5.0
_CONTRACT_KEYS = {
    "profile",
    "backend_id",
    "os_enforced",
    "workspace_roots_sha256",
    "filesystem_read_scope",
    "filesystem_write_scope",
    "network_scope",
    "process_tree_scope",
    "timeout_seconds",
    "stdout_limit_bytes",
    "stderr_limit_bytes",
    "contract_id",
}


def handle(request: Request) -> Mapping[str, Any]:
    require_exact_arguments(
        request.arguments,
        required=frozenset({"command"}),
        optional=frozenset({"cwd", "timeout_seconds"}),
    )
    if not request.context.get("approved") and request.context.get("effective_sandbox") != "danger-full-access":
        raise ContractError("shell_approval_missing")
    roots = tuple(Path(value).resolve(strict=True) for value in request.context["workspace_roots"])
    contract = _validate_contract(request.context.get("sandbox_contract"), roots)
    command = bounded_text(request.arguments.get("command"), 32_000)
    timeout = bounded_int(request.arguments.get("timeout_seconds", 120), 1, 3600)
    if timeout + 5 > float(contract["timeout_seconds"]):
        raise ContractError("shell_timeout_exceeds_core_contract")
    cwd = _resolve_cwd(request.arguments.get("cwd"), roots)
    argv = _fixed_shell(command)
    completed = _run_bounded(
        argv,
        cwd=cwd,
        environment=_child_environment(),
        timeout_seconds=timeout,
    )
    return {
        "exit_code": int(completed.returncode),
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "sandbox_backend_id": contract["backend_id"],
        "sandbox_os_enforced": contract["os_enforced"],
    }


class _BoundedPipeReader:
    """Drain one child pipe without ever retaining more than its contract."""

    def __init__(
        self,
        stream: Any,
        limit: int,
        *,
        overflow: threading.Event,
        transport_failed: threading.Event,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self._transport_failed = transport_failed
        self.payload = bytearray()
        self.thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> bool:
        self.thread.join(timeout=max(0.0, timeout))
        return not self.thread.is_alive()

    def close(self) -> None:
        try:
            self._stream.close()
        except (OSError, ValueError):
            pass

    def _read(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK)
                if not chunk:
                    return
                remaining = self._limit - len(self.payload)
                if remaining > 0:
                    self.payload.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._overflow.set()
                    return
        except (OSError, ValueError):
            self._transport_failed.set()
        finally:
            self.close()


class _WindowsJob:
    """Small nested Job used to reap the command and every descendant."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _BasicLimit(ctypes.Structure):
                _fields_ = (
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                )

            class _IoCounters(ctypes.Structure):
                _fields_ = tuple(
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "ReadOperationCount",
                        "WriteOperationCount",
                        "OtherOperationCount",
                        "ReadTransferCount",
                        "WriteTransferCount",
                        "OtherTransferCount",
                    )
                )

            class _ExtendedLimit(ctypes.Structure):
                _fields_ = (
                    ("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = (
                wintypes.HANDLE,
                wintypes.HANDLE,
            )
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008
            limits.BasicLimitInformation.ActiveProcessLimit = 64
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(error, "SetInformationJobObject")
            self._ctypes = ctypes
            self._kernel32 = kernel32
            self._handle = int(handle)
        except (AttributeError, ImportError, OSError, ValueError):
            self.close()
            raise ContractError("shell_process_supervision_unavailable") from None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            return
        handle = self._require_handle()
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            handle, int(process_handle)
        ):
            raise ContractError("shell_process_supervision_unavailable")

    def terminate(self) -> None:
        if os.name == "nt" and self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                self._kernel32.CloseHandle(handle)
            except (AttributeError, OSError):
                pass

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ContractError("shell_process_supervision_unavailable")
        return self._handle


class _CompletedCommand:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_bounded(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> _CompletedCommand:
    overflow = threading.Event()
    transport_failed = threading.Event()
    supervisor = _WindowsJob()
    process: subprocess.Popen[bytes] | None = None
    readers: tuple[_BoundedPipeReader, _BoundedPipeReader] | None = None
    failure: str | None = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": dict(environment),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(argv, **kwargs)
            supervisor.attach(process)
        except ContractError:
            if process is not None:
                _terminate_process_tree(process, supervisor)
            raise
        except OSError:
            raise ContractError("shell_command_unavailable") from None

        assert process.stdout is not None and process.stderr is not None
        readers = (
            _BoundedPipeReader(
                process.stdout,
                _MAX_STDOUT,
                overflow=overflow,
                transport_failed=transport_failed,
            ),
            _BoundedPipeReader(
                process.stderr,
                _MAX_STDERR,
                overflow=overflow,
                transport_failed=transport_failed,
            ),
        )
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if overflow.is_set():
                failure = "shell_output_too_large"
                break
            if transport_failed.is_set():
                failure = "shell_command_unavailable"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "shell_command_timeout"
                break
            overflow.wait(min(0.01, remaining))

        # A command may report success after starting a background descendant.
        # Closing/terminating the private Job or process group before joining
        # the readers ensures inherited pipe handles cannot keep the Pack alive.
        _terminate_process_tree(process, supervisor)
        _wait_process(process)
        settled = all(
            [reader.join(_PROCESS_SETTLE_SECONDS) for reader in readers]
        )
        if not settled:
            for reader in readers:
                reader.close()
            settled = all([reader.join(0.5) for reader in readers])
        if not settled or transport_failed.is_set():
            failure = failure or "shell_command_unavailable"
        if overflow.is_set():
            failure = "shell_output_too_large"
        if failure is not None:
            raise ContractError(failure)
        return _CompletedCommand(
            int(process.returncode or 0),
            bytes(readers[0].payload),
            bytes(readers[1].payload),
        )
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process, supervisor)
            _wait_process(process)
        if readers is not None:
            for reader in readers:
                reader.close()
        supervisor.close()


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    supervisor: _WindowsJob,
) -> None:
    if os.name == "nt":
        supervisor.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def _wait_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_PROCESS_SETTLE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise ContractError("shell_process_supervision_unavailable") from None


def _validate_contract(value: Any, roots: tuple[Path, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONTRACT_KEYS:
        raise ContractError("shell_sandbox_contract_missing")
    expected_digest = hashlib.sha256(
        "\0".join(str(root) for root in roots).encode("utf-8")
    ).hexdigest()
    profile = value.get("profile")
    unsigned = {key: item for key, item in value.items() if key != "contract_id"}
    expected_contract_id = "sandbox_" + hashlib.sha256(
        json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        value.get("workspace_roots_sha256") != expected_digest
        or value.get("process_tree_scope") != "contained-inherited"
        or value.get("contract_id") != expected_contract_id
        or isinstance(value.get("timeout_seconds"), bool)
        or not isinstance(value.get("timeout_seconds"), (int, float))
        or not isinstance(value.get("stdout_limit_bytes"), int)
        or not isinstance(value.get("stderr_limit_bytes"), int)
        or value.get("stdout_limit_bytes") < _MAX_STDOUT
        or value.get("stderr_limit_bytes") < _MAX_STDERR
    ):
        raise ContractError("shell_sandbox_contract_invalid")
    if profile == "workspace-write":
        if (
            value.get("os_enforced") is not True
            or value.get("filesystem_read_scope") != "workspace-only"
            or value.get("filesystem_write_scope") != "workspace-only"
            or value.get("network_scope") != "denied"
        ):
            raise ContractError("shell_sandbox_contract_invalid")
    elif profile == "danger-full-access":
        if value.get("os_enforced") is not False:
            raise ContractError("shell_sandbox_contract_invalid")
    else:
        raise ContractError("shell_sandbox_contract_invalid")
    return value


def _resolve_cwd(value: Any, roots: tuple[Path, ...]) -> Path:
    candidate = roots[0] if value is None else Path(bounded_text(value, 4096))
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ContractError("shell_cwd_invalid") from None
    if not resolved.is_dir() or not any(_contained(resolved, root) for root in roots):
        raise ContractError("shell_cwd_outside_workspace")
    return resolved


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _fixed_shell(command: str) -> tuple[str, ...]:
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", ""))
        executable = (system_root / "System32" / "cmd.exe").resolve(strict=True)
        return str(executable), "/d", "/s", "/c", command
    executable = Path("/bin/sh").resolve(strict=True)
    return str(executable), "-c", command


def _child_environment() -> Mapping[str, str]:
    allowed = {"LANG", "LC_ALL", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    result = {
        key.upper(): value
        for key, value in os.environ.items()
        if key.upper() in allowed and isinstance(value, str) and "\x00" not in value
    }
    result["PATH"] = (
        str(Path(result["SYSTEMROOT"]) / "System32")
        if os.name == "nt" and result.get("SYSTEMROOT")
        else "/usr/bin:/bin:/usr/sbin:/sbin"
    )
    return result
