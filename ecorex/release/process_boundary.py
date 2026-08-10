"""Bounded subprocess boundary for release and platform tooling.

Release helpers execute digest-pinned native programs, KMS adapters and
platform stagers.  ``subprocess.run(..., stdout=PIPE)`` drains a pipe, but it
still retains unbounded output in memory before the caller can validate its
size.  This module keeps both output streams bounded while supervising one
deadline and terminating the complete process tree on failure.

The boundary deliberately never includes child output in exceptions.  A
caller may retain bounded stdout after a successful exit, but stderr is only
diagnostic input and must not cross a signing or receipt boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping, Sequence


_POLL_SECONDS = 0.025
_REAP_TIMEOUT_SECONDS = 5.0
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_MIN_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
_MAX_MEMORY_LIMIT_BYTES = 16 * 1024 * 1024 * 1024


class BoundedProcessError(RuntimeError):
    """Base class for non-sensitive supervised-process failures."""


class BoundedProcessTimedOut(BoundedProcessError):
    """The process did not finish before its caller-owned deadline."""


class BoundedProcessOutputOverflow(BoundedProcessError):
    """At least one output stream exceeded its retained byte limit."""


class BoundedProcessIOError(BoundedProcessError):
    """A pipe worker could not complete the supervised exchange."""


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """A reaped process result containing only caller-bounded output."""

    returncode: int
    stdout: bytes
    stderr: bytes


def run_bounded_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    payload: bytes | None,
    cwd: str | os.PathLike[str],
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    hide_window: bool = True,
    windows_process_memory_limit_bytes: int | None = None,
    windows_job_memory_limit_bytes: int | None = None,
) -> BoundedProcessResult:
    """Run one argv-only process without retaining unbounded pipe output.

    The process is started in a new process group/session.  Timeout, output
    overflow and pipe failure terminate that group/tree before the root is
    explicitly reaped.  ``payload=None`` closes stdin immediately.
    """

    memory_limits = (
        windows_process_memory_limit_bytes,
        windows_job_memory_limit_bytes,
    )
    if (
        not command
        or not 0 < timeout_seconds <= 24 * 60 * 60
        or not 0 <= max_stdout_bytes <= 64 * 1024 * 1024
        or not 0 <= max_stderr_bytes <= 64 * 1024 * 1024
        or (memory_limits[0] is None) != (memory_limits[1] is None)
        or any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not _MIN_MEMORY_LIMIT_BYTES <= value <= _MAX_MEMORY_LIMIT_BYTES
            )
            for value in memory_limits
        )
        or (
            memory_limits[0] is not None
            and memory_limits[1] is not None
            and memory_limits[1] < memory_limits[0]
        )
    ):
        raise ValueError("bounded process limits are invalid")
    normalized_command = [os.fspath(value) for value in command]
    if any(not value or "\x00" in value for value in normalized_command):
        raise ValueError("bounded process command is invalid")
    if payload is not None and not isinstance(payload, bytes):
        raise TypeError("bounded process payload must be bytes or None")

    creation_flags = 0
    windows_job: int | None = None
    if os.name == "nt":
        # There must be no spawn-before-assign window.  Python closes the
        # primary thread handle internally, so NtResumeProcess resumes the
        # process only after its root is inside a kill-on-close Job Object.
        windows_job = _create_windows_kill_job(
            process_memory_limit_bytes=windows_process_memory_limit_bytes,
            job_memory_limit_bytes=windows_job_memory_limit_bytes,
        )
        creation_flags |= (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _CREATE_SUSPENDED
        )
        if hide_window:
            creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            normalized_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.fspath(cwd),
            env=dict(environment),
            shell=False,
            close_fds=True,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        if windows_job is not None:
            _close_windows_handle(windows_job)
        raise
    if windows_job is not None:
        setattr(process, "_ecorex_windows_job", windows_job)
        try:
            _assign_windows_job_and_resume(process, windows_job)
        except BaseException:
            terminate_process_tree(process)
            raise BoundedProcessIOError from None
    try:
        stdout, stderr = _exchange(
            process,
            payload=b"" if payload is None else payload,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        if process.returncode is None:
            terminate_process_tree(process)
            raise BoundedProcessIOError
        return BoundedProcessResult(
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException:
        if process.poll() is None:
            terminate_process_tree(process)
        raise
    finally:
        # Background descendants are prohibited at this boundary.  Closing a
        # successful Job therefore also kills any child that detached its
        # stdio before the root exited.
        if os.name == "nt":
            _close_process_windows_job(process)


def _exchange(
    process: subprocess.Popen[bytes],
    *,
    payload: bytes,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        terminate_process_tree(process)
        raise BoundedProcessIOError

    stdout = bytearray()
    stderr = bytearray()
    wake = threading.Event()
    overflow = threading.Event()
    io_failure = threading.Event()
    stdin_done = threading.Event()
    stdout_done = threading.Event()
    stderr_done = threading.Event()

    def read_bounded(
        stream: object,
        destination: bytearray,
        limit: int,
        done: threading.Event,
    ) -> None:
        try:
            while True:
                remaining = limit - len(destination)
                chunk = stream.read(min(4096, remaining + 1))  # type: ignore[attr-defined]
                if not chunk:
                    return
                if len(chunk) > remaining:
                    if remaining:
                        destination.extend(chunk[:remaining])
                    overflow.set()
                    return
                destination.extend(chunk)
        except (OSError, ValueError):
            io_failure.set()
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass
            done.set()
            wake.set()

    def write_payload() -> None:
        try:
            if payload:
                process.stdin.write(payload)
                process.stdin.flush()
        except BrokenPipeError:
            # A rejecting child may exit without consuming its request.
            pass
        except (OSError, ValueError):
            io_failure.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            stdin_done.set()
            wake.set()

    threads = (
        threading.Thread(
            target=write_payload,
            name="ecorex-process-stdin",
            daemon=True,
        ),
        threading.Thread(
            target=read_bounded,
            args=(process.stdout, stdout, max_stdout_bytes, stdout_done),
            name="ecorex-process-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=read_bounded,
            args=(process.stderr, stderr, max_stderr_bytes, stderr_done),
            name="ecorex-process-stderr",
            daemon=True,
        ),
    )
    started: list[threading.Thread] = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except RuntimeError:
        terminate_process_tree(process)
        _finish_threads(tuple(started))
        raise BoundedProcessIOError from None

    deadline = time.monotonic() + timeout_seconds
    failure: type[BoundedProcessError] | None = None
    while True:
        if overflow.is_set():
            failure = BoundedProcessOutputOverflow
            break
        if io_failure.is_set():
            failure = BoundedProcessIOError
            break
        if (
            process.poll() is not None
            and stdin_done.is_set()
            and stdout_done.is_set()
            and stderr_done.is_set()
        ):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = BoundedProcessTimedOut
            break
        wake.wait(min(_POLL_SECONDS, remaining))
        wake.clear()

    if failure is not None:
        terminate_process_tree(process)
        _finish_threads(threads)
        raise failure
    try:
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        _finish_threads(threads)
        raise BoundedProcessTimedOut from None
    _finish_threads(threads)
    return bytes(stdout), bytes(stderr)


def _finish_threads(threads: tuple[threading.Thread, ...]) -> None:
    for thread in threads:
        thread.join(timeout=1.0)


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort group/tree termination followed by a bounded root reap."""

    if os.name == "nt":
        if not _close_process_windows_job(process):
            _terminate_windows_process_tree(process)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        system_directory = _windows_system_directory()
        taskkill = system_directory / "taskkill.exe"
    except OSError:
        return
    try:
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_REAP_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _create_windows_kill_job(
    *,
    process_memory_limit_bytes: int | None = None,
    job_memory_limit_bytes: int | None = None,
) -> int:
    if os.name != "nt":
        raise OSError("Windows Job Object is unavailable")

    class _IOCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("Windows Job Object creation failed")
    value = _ExtendedLimitInformation()
    value.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if process_memory_limit_bytes is not None and job_memory_limit_bytes is not None:
        value.BasicLimitInformation.LimitFlags |= (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        value.ProcessMemoryLimit = process_memory_limit_bytes
        value.JobMemoryLimit = job_memory_limit_bytes
    if not kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(value),
        ctypes.sizeof(value),
    ):
        _close_windows_handle(int(handle))
        raise OSError("Windows Job Object policy failed")
    return int(handle)


def _assign_windows_job_and_resume(
    process: subprocess.Popen[bytes],
    job_handle: int,
) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    process_handle = int(process._handle)  # type: ignore[attr-defined]
    if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
        raise OSError("Windows Job Object assignment failed")
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
    ntdll.NtResumeProcess.restype = ctypes.c_long
    if ntdll.NtResumeProcess(process_handle) != 0:
        raise OSError("Windows suspended process resume failed")


def _close_process_windows_job(process: subprocess.Popen[bytes]) -> bool:
    value = getattr(process, "_ecorex_windows_job", None)
    if not isinstance(value, int) or value <= 0:
        return False
    setattr(process, "_ecorex_windows_job", None)
    _close_windows_handle(value)
    return True


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt" or handle <= 0:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(handle)


def _windows_system_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows system directory is unavailable")
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise OSError("Windows system directory is unavailable")
    return Path(buffer.value)


__all__ = [
    "BoundedProcessError",
    "BoundedProcessIOError",
    "BoundedProcessOutputOverflow",
    "BoundedProcessResult",
    "BoundedProcessTimedOut",
    "run_bounded_process",
    "terminate_process_tree",
]
