"""Crash-contained stdio protocol for signed browser and sandbox packs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import zipfile

from ecorex.capabilities import ToolInvocationContext, VerifiedCapabilityPack
from ecorex.integration.pack_python import PackPythonIdentity

from .sandbox import (
    SandboxBackend,
    SandboxProbe,
    default_workspace_sandbox_backend,
)


PACK_PROCESS_PROTOCOL = "ecorex-stdio-tool-v1"
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_PACK_TOOLS = {
    "browser": frozenset({"browser", "web_fetch", "web_search"}),
    "sandbox": frozenset({"bash"}),
}

# These failures are returned by the reviewed sandbox pack before it resolves
# or starts the requested command.  They are safe to report as normal failed
# Tool executions rather than asking the user whether a command might have
# run.  All other pack failures after request delivery remain conservative.
_PRE_EXECUTION_PACK_FAILURE_CODES = frozenset(
    {
        "shell_approval_missing",
        "shell_timeout_exceeds_core_contract",
        "shell_cwd_invalid",
        "shell_cwd_outside_workspace",
        "shell_sandbox_contract_missing",
        "shell_sandbox_contract_invalid",
        "shell_process_supervision_unavailable",
        "browser_operation_not_supported",
        "pack_arguments_invalid",
        "browser_target_only_for_navigation",
        "browser_selector_invalid",
        "browser_script_invalid",
        "browser_url_not_allowed",
    }
)


class CapabilityPackProcessError(RuntimeError):
    """A stable, non-secret failure returned by or around one pack process.

    ``side_effect_uncertain`` is deliberately explicit.  A Runtime worker must
    ask the user to reconcile an opaque command only after its request crossed
    the child-process boundary; preflight failures (for example a rejected
    sandbox contract) cannot have executed the command and must not create a
    misleading conflict card.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        side_effect_uncertain: bool = False,
    ) -> None:
        normalized = code if _SAFE_CODE.fullmatch(str(code)) else "pack_process_failed"
        self.code = normalized
        self.retryable = bool(retryable)
        self.side_effect_uncertain = bool(side_effect_uncertain)
        super().__init__(normalized)


@dataclass(frozen=True, slots=True)
class PackProcessDescriptor:
    pack_id: str
    runtime_api_version: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SandboxIsolationContract:
    """Exact Core-enforced process boundary acknowledged by the pack."""

    profile: str
    backend_id: str
    os_enforced: bool
    workspace_roots_sha256: str
    filesystem_read_scope: str
    filesystem_write_scope: str
    network_scope: str
    process_tree_scope: str
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "backend_id": self.backend_id,
            "os_enforced": self.os_enforced,
            "workspace_roots_sha256": self.workspace_roots_sha256,
            "filesystem_read_scope": self.filesystem_read_scope,
            "filesystem_write_scope": self.filesystem_write_scope,
            "network_scope": self.network_scope,
            "process_tree_scope": self.process_tree_scope,
            "timeout_seconds": self.timeout_seconds,
            "stdout_limit_bytes": self.stdout_limit_bytes,
            "stderr_limit_bytes": self.stderr_limit_bytes,
        }

    @property
    def contract_id(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sandbox_" + hashlib.sha256(payload).hexdigest()


class ProcessCapabilityPackAdapter:
    """Invoke a signed zipapp once per tool call through a bounded protocol.

    A pack crash, malformed response or output flood terminates only its child
    process. The parent environment is reduced to a fixed OS allowlist, and the
    Core passes the authoritative sandbox/workspace snapshot in every request.
    """

    def __init__(
        self,
        pack: VerifiedCapabilityPack,
        *,
        workspace_roots: tuple[str | os.PathLike[str], ...],
        python_executable: str | os.PathLike[str],
        python_identity: PackPythonIdentity | None = None,
        default_timeout_seconds: float = 120,
        sandbox_backend: SandboxBackend | None = None,
    ) -> None:
        if not workspace_roots:
            raise ValueError("pack process requires at least one workspace root")
        if not 1 <= default_timeout_seconds <= 600:
            raise ValueError("pack process default timeout is invalid")
        roots: list[Path] = []
        for raw in workspace_roots:
            root = Path(raw).resolve(strict=True)
            if not root.is_dir() or root in roots:
                raise ValueError("pack process workspace roots are invalid")
            roots.append(root)
        executable = Path(python_executable).resolve(strict=True)
        if not executable.is_file():
            raise ValueError("pack process Python executable is unavailable")
        self.pack = pack
        self.artifact_path = pack.artifact_path.resolve(strict=True)
        self.workspace_roots = tuple(roots)
        self._workspace_root_resolver: Callable[[Any], tuple[Path, ...]] | None = None
        self.python_executable = executable
        self.python_identity = python_identity
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.descriptor = _inspect_zipapp(pack)
        self._sandbox_backend = sandbox_backend or default_workspace_sandbox_backend()
        self.sandbox_probe: SandboxProbe | None = None
        if self.descriptor.pack_id == "sandbox":
            try:
                probe = self._sandbox_backend.probe(
                    workspace_roots=self.workspace_roots,
                    python_executable=self.python_executable,
                    artifact_path=self.artifact_path,
                )
            except Exception:
                probe = SandboxProbe(
                    backend_id="unavailable",
                    platform=sys.platform,
                    ready=False,
                    reason="workspace_sandbox_probe_failed",
                )
            # A backend cannot self-assert readiness while omitting any part
            # of the actual workspace/network/process-tree boundary.
            self.sandbox_probe = probe
        self._child_environment = _minimal_environment()
        environment_paths = tuple(
            value
            for key in ("TEMP", "TMP")
            if isinstance((value := self._child_environment.get(key)), str)
            and os.path.isabs(value)
        )
        self._protected_paths = tuple(
            _normalized_path_text(str(path.resolve()))
            for path in (*self.workspace_roots, self.artifact_path, executable)
        ) + tuple(_normalized_path_text(value) for value in environment_paths)
        self._browser_session_process: asyncio.subprocess.Process | None = None
        self._browser_session_lock: asyncio.Lock | None = None
        self._browser_session_temp: Path | None = None

    def handlers(self) -> Mapping[str, Callable[..., Any]]:
        return {
            tool_id: _ProcessPackToolHandler(self, tool_id)
            for tool_id in self.descriptor.tools
        }

    def bind_workspace_root_resolver(
        self, resolver: Callable[[Any], tuple[Path, ...]] | None
    ) -> None:
        self._workspace_root_resolver = resolver

    def _workspace_roots_for_scope(self, scope: Any) -> tuple[Path, ...]:
        additional = (
            self._workspace_root_resolver(scope)
            if self._workspace_root_resolver is not None
            else ()
        )
        if not isinstance(additional, tuple):
            raise CapabilityPackProcessError("pack_workspace_authority_invalid")
        # Put the Turn-bound project first so relative shell paths and the
        # child cwd match the project conversation the user selected.
        return tuple(dict.fromkeys((*additional, *self.workspace_roots)))

    async def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        if tool_id not in self.descriptor.tools or context.tool_id != tool_id:
            raise CapabilityPackProcessError("pack_tool_identity_mismatch")
        scope = context.execution_scope
        workspace_roots = self._workspace_roots_for_scope(scope)
        timeout = self._timeout(tool_id, arguments)
        sandbox_contract = self._sandbox_contract(
            tool_id=tool_id,
            effective_sandbox=context.effective_sandbox.value,
            timeout_seconds=timeout,
            workspace_roots=workspace_roots,
        )
        request = {
            "schema_version": 1,
            "protocol": PACK_PROCESS_PROTOCOL,
            "request_id": context.invocation_id,
            "pack_id": self.descriptor.pack_id,
            "tool_id": tool_id,
            "arguments": dict(arguments),
            "context": {
                "policy_snapshot_id": context.policy_snapshot_id,
                "capability_snapshot_id": context.capability_snapshot_id,
                "idempotency_key": context.idempotency_key,
                "approved": context.approved,
                "effective_sandbox": context.effective_sandbox.value,
                "workspace_roots": [str(path) for path in workspace_roots],
                "sandbox_contract": (
                    {
                        **sandbox_contract.to_dict(),
                        "contract_id": sandbox_contract.contract_id,
                    }
                    if sandbox_contract is not None
                    else None
                ),
                "execution_scope": (
                    {
                        "job_id": scope.job_id,
                        "thread_id": scope.thread_id,
                        "turn_id": scope.turn_id,
                    }
                    if scope is not None
                    else None
                ),
            },
        }
        try:
            payload = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise CapabilityPackProcessError("pack_request_invalid") from None
        if not 1 <= len(payload) <= MAX_REQUEST_BYTES:
            raise CapabilityPackProcessError("pack_request_too_large")
        if self.descriptor.pack_id == "browser" and tool_id == "browser":
            return await self._invoke_browser_session(
                payload=payload,
                context=context,
                timeout=timeout,
                workspace_roots=workspace_roots,
            )
        await asyncio.to_thread(_verify_pack_artifact, self.pack)
        temporary_root = await asyncio.to_thread(
            _create_pack_invocation_temp, self.workspace_roots[0]
        )
        child_environment = dict(self._child_environment)
        child_environment.update(
            {"TEMP": str(temporary_root), "TMP": str(temporary_root)}
        )
        if os.name != "nt":
            child_environment["TMPDIR"] = str(temporary_root)
        try:
            return await self._invoke_process(
                tool_id=tool_id,
                payload=payload,
                context=context,
                timeout=timeout,
                sandbox_contract=sandbox_contract,
                child_environment=child_environment,
                workspace_roots=workspace_roots,
            )
        finally:
            await asyncio.to_thread(_remove_pack_invocation_temp, temporary_root)

    async def _invoke_browser_session(
        self,
        *,
        payload: bytes,
        context: ToolInvocationContext,
        timeout: float,
        workspace_roots: tuple[Path, ...],
    ) -> Any:
        """Use one signed browser Pack process for the Runtime lifetime."""

        if self._browser_session_lock is None:
            self._browser_session_lock = asyncio.Lock()
        async with self._browser_session_lock:
            process = self._browser_session_process
            if process is None or process.returncode is not None:
                await self._stop_browser_session()
                await asyncio.to_thread(_verify_pack_artifact, self.pack)
                temporary_root = await asyncio.to_thread(
                    _create_pack_invocation_temp, self.workspace_roots[0]
                )
                child_environment = dict(self._child_environment)
                child_environment.update(
                    {"TEMP": str(temporary_root), "TMP": str(temporary_root)}
                )
                if os.name != "nt":
                    child_environment["TMPDIR"] = str(temporary_root)
                try:
                    process = await self._spawn_browser_session(
                        child_environment=child_environment,
                        workspace_roots=workspace_roots,
                    )
                except Exception:
                    await asyncio.to_thread(
                        _remove_pack_invocation_temp, temporary_root
                    )
                    raise
                self._browser_session_process = process
                self._browser_session_temp = temporary_root
            request_dispatched = False
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(len(payload).to_bytes(8, "big") + payload)
                await process.stdin.drain()
                request_dispatched = True
                header = await asyncio.wait_for(
                    process.stdout.readexactly(8), timeout=timeout
                )
                size = int.from_bytes(header, "big")
                if not 1 <= size <= MAX_STDOUT_BYTES:
                    raise CapabilityPackProcessError("pack_response_size_invalid")
                response_payload = await asyncio.wait_for(
                    process.stdout.readexactly(size), timeout=timeout
                )
            except asyncio.CancelledError:
                await self._stop_browser_session()
                raise
            except TimeoutError:
                await self._stop_browser_session()
                raise CapabilityPackProcessError(
                    "pack_process_timeout",
                    retryable=True,
                    side_effect_uncertain=request_dispatched,
                ) from None
            except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError):
                await self._stop_browser_session()
                raise CapabilityPackProcessError(
                    "pack_process_transport_failed",
                    retryable=True,
                    side_effect_uncertain=request_dispatched,
                ) from None
            except CapabilityPackProcessError as error:
                await self._stop_browser_session()
                error.side_effect_uncertain = request_dispatched
                raise
            try:
                response = _parse_response(
                    response_payload,
                    request_id=context.invocation_id,
                    sandbox_contract_id=None,
                )
            except CapabilityPackProcessError as error:
                await self._stop_browser_session()
                error.side_effect_uncertain = request_dispatched
                raise
            if response[0] == "failed":
                raise CapabilityPackProcessError(
                    response[1],
                    retryable=response[2],
                    side_effect_uncertain=(
                        request_dispatched
                        and response[1] not in _PRE_EXECUTION_PACK_FAILURE_CODES
                    ),
                )
            result = response[1]
            protected_paths = self._protected_paths + tuple(
                _normalized_path_text(path) for path in workspace_roots
            )
            if _contains_protected_path(result, protected_paths):
                await self._stop_browser_session()
                raise CapabilityPackProcessError(
                    "pack_result_exposed_host_path",
                    side_effect_uncertain=request_dispatched,
                )
            return result

    async def _spawn_browser_session(
        self,
        *,
        child_environment: Mapping[str, str],
        workspace_roots: tuple[Path, ...],
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.DEVNULL,
            "cwd": str(workspace_roots[0]),
            "env": dict(child_environment),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            ) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(
            str(self.python_executable),
            "-I",
            str(self.artifact_path),
            "--session",
            **kwargs,
        )

    async def _stop_browser_session(self) -> None:
        process = self._browser_session_process
        self._browser_session_process = None
        temporary_root = self._browser_session_temp
        self._browser_session_temp = None
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                await _kill_process_tree(process)
                await process.wait()
        if temporary_root is not None:
            await asyncio.to_thread(_remove_pack_invocation_temp, temporary_root)

    async def _invoke_process(
        self,
        *,
        tool_id: str,
        payload: bytes,
        context: ToolInvocationContext,
        timeout: float,
        sandbox_contract: SandboxIsolationContract | None,
        child_environment: Mapping[str, str],
        workspace_roots: tuple[Path, ...],
    ) -> Any:
        process = await self._spawn(
            tool_id=tool_id,
            sandbox_contract=sandbox_contract,
            child_environment=child_environment,
            workspace_roots=workspace_roots,
        )
        request_dispatched = False
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, MAX_STDOUT_BYTES)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, MAX_STDERR_BYTES)
        )
        wait_task = asyncio.create_task(
            _wait_for_process(process, stdout_task, stderr_task)
        )
        try:
            assert process.stdin is not None
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            # Packs read one complete request before dispatching a tool.  From
            # this point onward a lost result from a non-idempotent tool is a
            # genuine ambiguity and must enter HITL reconciliation.
            request_dispatched = True
            return_code, stdout, _stderr = await asyncio.wait_for(
                asyncio.shield(wait_task),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            await _kill_process_tree(process)
            await _settle_process_tasks(wait_task, stdout_task, stderr_task)
            raise
        except TimeoutError:
            await _kill_process_tree(process)
            await _settle_process_tasks(wait_task, stdout_task, stderr_task)
            raise CapabilityPackProcessError(
                "pack_process_timeout",
                retryable=True,
                side_effect_uncertain=request_dispatched,
            ) from None
        except CapabilityPackProcessError as error:
            await _kill_process_tree(process)
            await _settle_process_tasks(wait_task, stdout_task, stderr_task)
            if request_dispatched:
                error.side_effect_uncertain = True
            raise
        except Exception:
            await _kill_process_tree(process)
            await _settle_process_tasks(wait_task, stdout_task, stderr_task)
            raise CapabilityPackProcessError(
                "pack_process_transport_failed",
                retryable=True,
                side_effect_uncertain=request_dispatched,
            ) from None
        if return_code != 0:
            raise CapabilityPackProcessError(
                "pack_process_exited",
                retryable=return_code < 0,
                side_effect_uncertain=request_dispatched,
            )
        # A successful pack response must not leave detached descendants. On
        # POSIX they inherit the process group; the Windows launcher owns a
        # kill-on-close Job Object and taskkill remains a defense in depth.
        await _kill_process_tree(process)
        # The zipapp remains a lazily-read executable for the duration of the
        # child process.  Re-hash after exit as a second fence so a slot or
        # pack mutation cannot be accepted merely because startup verification
        # happened earlier in the Runtime lifecycle.
        await asyncio.to_thread(_verify_pack_artifact, self.pack)
        try:
            response = _parse_response(
                stdout,
                request_id=context.invocation_id,
                sandbox_contract_id=(
                    sandbox_contract.contract_id
                    if sandbox_contract is not None
                    else None
                ),
            )
        except CapabilityPackProcessError as error:
            error.side_effect_uncertain = request_dispatched
            raise
        if response[0] == "failed":
            raise CapabilityPackProcessError(
                response[1],
                retryable=response[2],
                side_effect_uncertain=(
                    request_dispatched
                    and response[1] not in _PRE_EXECUTION_PACK_FAILURE_CODES
                ),
            )
        result = response[1]
        protected_paths = self._protected_paths + tuple(
            _normalized_path_text(path) for path in workspace_roots
        )
        if _contains_protected_path(result, protected_paths):
            raise CapabilityPackProcessError(
                "pack_result_exposed_host_path",
                side_effect_uncertain=request_dispatched,
            )
        return result

    async def _spawn(
        self,
        *,
        tool_id: str,
        sandbox_contract: SandboxIsolationContract | None,
        child_environment: Mapping[str, str],
        workspace_roots: tuple[Path, ...],
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(workspace_roots[0]),
            "env": dict(child_environment),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            ) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        argv = (str(self.python_executable), "-I", str(self.artifact_path))
        use_backend = bool(
            tool_id == "bash"
            and sandbox_contract is not None
            and (
                sandbox_contract.os_enforced
                or (
                    os.name == "nt" and sandbox_contract.profile == "danger-full-access"
                )
            )
        )
        if use_backend:
            try:
                launch = self._sandbox_backend.launch_plan(
                    workspace_roots=workspace_roots,
                    python_executable=self.python_executable,
                    artifact_path=self.artifact_path,
                    timeout_seconds=sandbox_contract.timeout_seconds,
                    output_limit_bytes=MAX_STDOUT_BYTES,
                    profile=sandbox_contract.profile,
                )
            except Exception:
                raise CapabilityPackProcessError(
                    "workspace_sandbox_launch_failed", retryable=False
                ) from None
            if launch.backend_id != sandbox_contract.backend_id:
                raise CapabilityPackProcessError(
                    "workspace_sandbox_identity_changed", retryable=False
                )
            argv = launch.argv
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                **kwargs,
            )
        except (OSError, ValueError):
            raise CapabilityPackProcessError(
                "pack_process_unavailable", retryable=True
            ) from None

    def _sandbox_contract(
        self,
        *,
        tool_id: str,
        effective_sandbox: str,
        timeout_seconds: float,
        workspace_roots: tuple[Path, ...],
    ) -> SandboxIsolationContract | None:
        if tool_id != "bash":
            return None
        roots_digest = hashlib.sha256(
            "\0".join(str(path) for path in workspace_roots).encode("utf-8")
        ).hexdigest()
        if effective_sandbox == "workspace-write":
            probe = self.sandbox_probe
            if probe is None or not probe.complete:
                raise CapabilityPackProcessError(
                    "workspace_sandbox_unavailable", retryable=False
                )
            return SandboxIsolationContract(
                profile="workspace-write",
                backend_id=probe.backend_id,
                os_enforced=True,
                workspace_roots_sha256=roots_digest,
                filesystem_read_scope=probe.effective_filesystem_read_scope,
                filesystem_write_scope="workspace-only",
                network_scope="denied",
                process_tree_scope="contained-inherited",
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=MAX_STDOUT_BYTES,
                stderr_limit_bytes=MAX_STDERR_BYTES,
            )
        if effective_sandbox == "danger-full-access":
            probe = self.sandbox_probe
            if os.name == "nt" and (probe is None or not probe.complete):
                raise CapabilityPackProcessError(
                    "shell_process_supervisor_unavailable", retryable=False
                )
            return SandboxIsolationContract(
                profile="danger-full-access",
                backend_id=(
                    probe.backend_id
                    if os.name == "nt" and probe is not None
                    else "explicit-unrestricted-process"
                ),
                os_enforced=False,
                workspace_roots_sha256=roots_digest,
                filesystem_read_scope="host-unrestricted",
                filesystem_write_scope="host-unrestricted",
                network_scope="host-unrestricted",
                process_tree_scope="contained-inherited",
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=MAX_STDOUT_BYTES,
                stderr_limit_bytes=MAX_STDERR_BYTES,
            )
        raise CapabilityPackProcessError(
            "shell_sandbox_profile_invalid", retryable=False
        )

    def _timeout(self, tool_id: str, arguments: Mapping[str, Any]) -> float:
        if tool_id != "bash":
            return self.default_timeout_seconds
        raw = arguments.get("timeout_seconds", self.default_timeout_seconds)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raw = self.default_timeout_seconds
        return float(min(3605, max(2, raw + 5)))


class _ProcessPackToolHandler:
    __slots__ = ("_adapter", "_tool_id")

    def __init__(self, adapter: ProcessCapabilityPackAdapter, tool_id: str) -> None:
        self._adapter = adapter
        self._tool_id = tool_id

    @property
    def controlled_skill_sandbox_authority(
        self,
    ) -> tuple[Any, Path, PackPythonIdentity] | None:
        if (
            self._tool_id != "bash"
            or self._adapter.descriptor.pack_id != "sandbox"
            or self._adapter.python_identity is None
        ):
            return None
        return (
            self._adapter._sandbox_backend,
            self._adapter.python_executable,
            self._adapter.python_identity,
        )

    def bind_workspace_root_resolver(
        self, resolver: Callable[[Any], tuple[Path, ...]] | None
    ) -> None:
        self._adapter.bind_workspace_root_resolver(resolver)

    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        return await self._adapter.invoke(self._tool_id, arguments, context)


def _create_pack_invocation_temp(workspace_root: Path) -> Path:
    root = Path(
        tempfile.mkdtemp(prefix=".ecorex-pack-call-", dir=workspace_root)
    ).resolve(strict=True)
    root.chmod(0o700)
    return root


def _remove_pack_invocation_temp(root: Path) -> None:
    """Remove a child-owned temp domain only after its process is reaped."""

    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        return
    except OSError:
        raise CapabilityPackProcessError(
            "pack_process_temp_cleanup_failed", retryable=True
        ) from None


def _inspect_zipapp(pack: VerifiedCapabilityPack) -> PackProcessDescriptor:
    manifest = pack.manifest
    allowed = _PACK_TOOLS.get(manifest.pack_id)
    signed_tools = tuple(binding.tool_id for binding in manifest.tools)
    if allowed is None or not set(signed_tools).issubset(allowed):
        raise CapabilityPackProcessError("pack_process_identity_invalid")
    try:
        with zipfile.ZipFile(pack.artifact_path) as archive:
            members = archive.infolist()
            seen: set[str] = set()
            total = 0
            by_name: dict[str, zipfile.ZipInfo] = {}
            for member in members:
                normalized = member.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                collision_key = normalized.casefold().rstrip("/")
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (
                    not normalized
                    or path.is_absolute()
                    or not path.parts
                    or any(
                        part in {"", ".", ".."} or ":" in part for part in path.parts
                    )
                    or collision_key in seen
                    or member.flag_bits & 0x1
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise CapabilityPackProcessError("pack_zipapp_invalid")
                seen.add(collision_key)
                total += member.file_size
                if (
                    total > MAX_UNPACKED_BYTES
                    or member.file_size > 1024 * 1024
                    and member.file_size > max(1, member.compress_size) * 250
                ):
                    raise CapabilityPackProcessError("pack_zipapp_expansion_invalid")
                by_name[normalized.rstrip("/")] = member
            main = by_name.get("__main__.py")
            descriptor_info = by_name.get("ecorex-pack.json")
            if (
                main is None
                or main.is_dir()
                or main.file_size > 1024 * 1024
                or descriptor_info is None
                or descriptor_info.is_dir()
                or not 1 <= descriptor_info.file_size <= MAX_DESCRIPTOR_BYTES
            ):
                raise CapabilityPackProcessError("pack_zipapp_contract_missing")
            payload = archive.read(descriptor_info)
    except CapabilityPackProcessError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise CapabilityPackProcessError("pack_zipapp_invalid") from None
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CapabilityPackProcessError("pack_descriptor_invalid") from None
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {"schema_version", "protocol", "pack_id", "runtime_api_version", "tools"}
        or value.get("schema_version") != 1
        or value.get("protocol") != PACK_PROCESS_PROTOCOL
        or value.get("pack_id") != manifest.pack_id
        or value.get("runtime_api_version") != manifest.runtime_api_version
        or value.get("tools") != list(signed_tools)
        or payload
        != json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ):
        raise CapabilityPackProcessError("pack_descriptor_invalid")
    return PackProcessDescriptor(
        pack_id=manifest.pack_id,
        runtime_api_version=manifest.runtime_api_version,
        tools=signed_tools,
    )


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> bytes:
    if stream is None:
        raise CapabilityPackProcessError("pack_process_pipe_missing")
    body = bytearray()
    overflowed = False
    while chunk := await stream.read(64 * 1024):
        remaining = max(0, limit - len(body))
        if remaining:
            body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflowed = True
    if overflowed:
        raise CapabilityPackProcessError("pack_process_output_too_large")
    return bytes(body)


async def _wait_for_process(
    process: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
) -> tuple[int, bytes, bytes]:
    return_code, stdout, stderr = await asyncio.gather(
        process.wait(), stdout_task, stderr_task
    )
    return int(return_code), stdout, stderr


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name != "nt":
            # Kill the group even after its leader exited; otherwise a pack can
            # return success while leaving a detached descendant alive.
            os.killpg(process.pid, signal.SIGKILL)
        else:
            # The trusted Windows launcher owns a kill-on-close Job Object.
            # taskkill /T is retained as a defense in depth for a still-live
            # launcher; a naked Windows shell is never made available.
            await asyncio.to_thread(_windows_kill_process_tree, process.pid)
            if process.returncode is None:
                process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        pass


async def _settle_process_tasks(
    wait_task: asyncio.Task[tuple[int, bytes, bytes]],
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
) -> None:
    await asyncio.gather(
        wait_task,
        stdout_task,
        stderr_task,
        return_exceptions=True,
    )


def _parse_response(
    payload: bytes,
    *,
    request_id: str,
    sandbox_contract_id: str | None = None,
) -> tuple[Any, ...]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CapabilityPackProcessError("pack_response_invalid") from None
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise CapabilityPackProcessError("pack_response_invalid")
    if value.get("request_id") != request_id:
        raise CapabilityPackProcessError("pack_response_identity_mismatch")
    status_value = value.get("status")
    completed_keys = {
        "schema_version",
        "request_id",
        "status",
        "result",
    }
    if sandbox_contract_id is not None:
        completed_keys.add("sandbox_contract_id")
    if status_value == "completed" and set(value) == completed_keys:
        if (
            sandbox_contract_id is not None
            and value.get("sandbox_contract_id") != sandbox_contract_id
        ):
            raise CapabilityPackProcessError("pack_sandbox_handshake_mismatch")
        return "completed", value.get("result")
    failed_keys = {
        "schema_version",
        "request_id",
        "status",
        "error_code",
        "retryable",
    }
    if sandbox_contract_id is not None:
        failed_keys.add("sandbox_contract_id")
    if status_value == "failed" and set(value) == failed_keys:
        if (
            sandbox_contract_id is not None
            and value.get("sandbox_contract_id") != sandbox_contract_id
        ):
            raise CapabilityPackProcessError("pack_sandbox_handshake_mismatch")
        code = value.get("error_code")
        retryable = value.get("retryable")
        if (
            isinstance(code, str)
            and _SAFE_CODE.fullmatch(code)
            and isinstance(retryable, bool)
        ):
            return "failed", code, retryable
    raise CapabilityPackProcessError("pack_response_invalid")


def _minimal_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    result = {
        key.upper(): value
        for key, value in os.environ.items()
        if key.upper() in allowed and isinstance(value, str)
    }
    if os.name == "nt":
        system_root = str(_windows_system_root())
        result["SYSTEMROOT"] = system_root
        result["WINDIR"] = system_root
        result["SYSTEMDRIVE"] = Path(system_root).drive
        result["PATH"] = os.pathsep.join(
            (str(Path(system_root) / "System32"), system_root)
        )
        result["COMSPEC"] = str(Path(system_root) / "System32" / "cmd.exe")
        result["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    else:
        result["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    result.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return result


def _contains_protected_path(value: Any, paths: tuple[str, ...]) -> bool:
    pending = [value]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 100_000:
            raise CapabilityPackProcessError("pack_response_too_complex")
        if isinstance(item, str):
            normalized = _normalized_path_text(item)
            if any(path and path in normalized for path in paths):
                return True
        elif isinstance(item, Mapping):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def _normalized_path_text(value: str) -> str:
    return os.path.normcase(value).replace("\\", "/")


def _verify_pack_artifact(pack: VerifiedCapabilityPack) -> None:
    """Revalidate exact signed zipapp bytes at the execution boundary."""

    path = pack.artifact_path
    manifest = pack.manifest
    try:
        before = path.lstat()
    except OSError:
        raise CapabilityPackProcessError("pack_artifact_changed") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
        or before.st_size != manifest.artifact_size_bytes
    ):
        raise CapabilityPackProcessError("pack_artifact_changed")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise CapabilityPackProcessError("pack_artifact_changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except CapabilityPackProcessError:
        raise
    except OSError:
        raise CapabilityPackProcessError("pack_artifact_changed") from None
    if (
        _file_identity(after) != _file_identity(before)
        or digest.hexdigest() != manifest.artifact_sha256
    ):
        raise CapabilityPackProcessError("pack_artifact_changed")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _windows_kill_process_tree(pid: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    snapshot_handle = ctypes.c_void_p(-1).value
    try:
        kernel32 = ctypes.windll.kernel32

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        snapshot_handle = ctypes.cast(snapshot, ctypes.c_void_p).value
        if snapshot_handle in {None, ctypes.c_void_p(-1).value}:
            return
        children: dict[int, list[int]] = {}
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        present = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while present:
            children.setdefault(int(entry.th32ParentProcessID), []).append(
                int(entry.th32ProcessID)
            )
            present = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))

        ordered: list[int] = []
        pending: list[tuple[int, bool]] = [(pid, False)]
        visited: set[int] = set()
        while pending:
            current, expanded = pending.pop()
            if expanded:
                ordered.append(current)
                continue
            if current in visited:
                continue
            visited.add(current)
            pending.append((current, True))
            pending.extend((child, False) for child in children.get(current, ()))

        for process_id in ordered:
            handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, process_id)
            if not handle:
                continue
            try:
                kernel32.TerminateProcess(handle, 1)
                kernel32.WaitForSingleObject(handle, 1_000)
            finally:
                kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return
    finally:
        if snapshot_handle not in {None, ctypes.c_void_p(-1).value}:
            try:
                ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(snapshot_handle))
            except (AttributeError, OSError, ValueError):
                pass


def _windows_system_root() -> Path:
    if os.name != "nt":
        raise CapabilityPackProcessError("pack_process_unavailable")
    buffer = ctypes.create_unicode_buffer(32_768)
    try:
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError):
        raise CapabilityPackProcessError("pack_process_unavailable") from None
    if not 1 <= length < len(buffer):
        raise CapabilityPackProcessError("pack_process_unavailable")
    try:
        root = Path(buffer.value).resolve(strict=True)
    except OSError:
        raise CapabilityPackProcessError("pack_process_unavailable") from None
    if (
        not root.is_dir()
        or not (root / "System32" / "cmd.exe").is_file()
        or not (root / "System32" / "taskkill.exe").is_file()
    ):
        raise CapabilityPackProcessError("pack_process_unavailable")
    return root


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "CapabilityPackProcessError",
    "PACK_PROCESS_PROTOCOL",
    "PackProcessDescriptor",
    "ProcessCapabilityPackAdapter",
    "SandboxIsolationContract",
]
