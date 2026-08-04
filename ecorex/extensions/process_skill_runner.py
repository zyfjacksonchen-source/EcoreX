"""Typed process boundary for controlled Skill execution.

The runner owns protocol validation, CAS/interpreter identity fences, bounded
I/O and process-tree cleanup.  It never constructs a host command itself: only
a product-owned OS backend may turn the signed contract into an argv launch
plan.  Production platforms without that backend remain unavailable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from types import MappingProxyType
from typing import Any, Protocol

from .local_bundle import LocalSkillBundleStore
from .skill_runner import (
    ControlledSkillRunRequest,
    ControlledSkillRunResult,
    MAX_SKILL_RESULT_BYTES,
)


CONTROLLED_SKILL_PROCESS_PROTOCOL = "emate-controlled-skill-process-v1"
MAX_SKILL_STDERR_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ControlledSkillProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedSkillInterpreter:
    runtime: str
    executable: Path
    sha256: str

    def __post_init__(self) -> None:
        if self.runtime not in {"python", "node"}:
            raise ValueError("controlled Skill interpreter runtime is invalid")
        executable = self.executable.expanduser().resolve(strict=True)
        if not executable.is_file() or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("controlled Skill interpreter identity is invalid")
        object.__setattr__(self, "executable", executable)


@dataclass(frozen=True, slots=True)
class ControlledSkillProcessContract:
    extension_id: str
    revision_id: str
    extension_generation: int
    cas_tree_sha256: str
    entrypoint: str
    entrypoint_sha256: str
    runtime: str
    interpreter_sha256: str
    arguments: tuple[str, ...]
    environment_names: tuple[str, ...]
    network_domains: tuple[str, ...]
    effects: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            _SAFE_ID.fullmatch(self.extension_id) is None
            or _SAFE_ID.fullmatch(self.revision_id) is None
            or isinstance(self.extension_generation, bool)
            or self.extension_generation < 0
            or self.runtime not in {"python", "node"}
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    self.cas_tree_sha256,
                    self.entrypoint_sha256,
                    self.interpreter_sha256,
                )
            )
            or self.arguments
            or tuple(sorted(set(self.environment_names))) != self.environment_names
            or tuple(sorted(set(self.network_domains))) != self.network_domains
            or not self.effects
        ):
            raise ValueError("controlled Skill process contract is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "protocol": CONTROLLED_SKILL_PROCESS_PROTOCOL,
            "extension_id": self.extension_id,
            "revision_id": self.revision_id,
            "extension_generation": self.extension_generation,
            "cas_tree_sha256": self.cas_tree_sha256,
            "entrypoint": self.entrypoint,
            "entrypoint_sha256": self.entrypoint_sha256,
            "runtime": self.runtime,
            "interpreter_sha256": self.interpreter_sha256,
            "arguments": list(self.arguments),
            "environment_names": list(self.environment_names),
            "network_domains": list(self.network_domains),
            "effects": list(self.effects),
        }

    @property
    def contract_id(self) -> str:
        return "skill_" + hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class ControlledSkillLaunchRequest:
    contract: ControlledSkillProcessContract
    cas_root: Path
    entrypoint_path: Path
    interpreter_path: Path
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int


@dataclass(frozen=True, slots=True)
class ControlledSkillLaunchPlan:
    argv: tuple[str, ...]
    backend_id: str
    contract_id: str

    def __post_init__(self) -> None:
        if (
            not self.argv
            or any(not isinstance(value, str) or not value for value in self.argv)
            or not self.backend_id
            or self.contract_id == ""
        ):
            raise ValueError("controlled Skill launch plan is invalid")


class ControlledSkillProcessBackend(Protocol):
    reason: str

    def supports(self, runtime: str) -> bool: ...

    def launch_plan(
        self, request: ControlledSkillLaunchRequest
    ) -> ControlledSkillLaunchPlan: ...


class UnavailableControlledSkillProcessBackend:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def supports(self, runtime: str) -> bool:
        del runtime
        return False

    def launch_plan(
        self, request: ControlledSkillLaunchRequest
    ) -> ControlledSkillLaunchPlan:
        del request
        raise ControlledSkillProcessError(self.reason)


class SandboxControlledSkillProcessBackend:
    """Bind controlled Skill execution to the verified product sandbox."""

    def __init__(
        self,
        backend: Any,
        *,
        cas_root: Path,
        interpreter: TrustedSkillInterpreter,
        workspace_roots: tuple[Path, ...],
    ) -> None:
        self.backend = backend
        self.cas_root = cas_root.resolve(strict=True)
        self.interpreter = interpreter
        self.workspace_roots = tuple(root.resolve(strict=True) for root in workspace_roots)
        if not self.workspace_roots:
            raise ValueError("controlled Skill sandbox requires a workspace root")
        try:
            self.probe = backend.probe(
                workspace_roots=self.workspace_roots,
                read_roots=(self.cas_root,),
                python_executable=interpreter.executable,
                artifact_path=interpreter.executable,
            )
        except Exception:
            self.probe = None
        self.reason = (
            "ready"
            if self.probe is not None and getattr(self.probe, "complete", False)
            else getattr(self.probe, "reason", "controlled_skill_sandbox_probe_failed")
        )

    def supports(self, runtime: str) -> bool:
        return runtime == "python" and self.reason == "ready"

    def launch_plan(
        self, request: ControlledSkillLaunchRequest
    ) -> ControlledSkillLaunchPlan:
        if not self.supports(request.contract.runtime):
            raise ControlledSkillProcessError(self.reason)
        if (
            not request.cas_root.is_relative_to(self.cas_root)
            or request.interpreter_path != self.interpreter.executable
            or not request.entrypoint_path.is_relative_to(self.cas_root)
            or request.contract.network_domains
        ):
            raise ControlledSkillProcessError("controlled_skill_sandbox_scope_invalid")
        try:
            plan = self.backend.launch_plan(
                workspace_roots=self.workspace_roots,
                read_roots=(request.cas_root,),
                python_executable=self.interpreter.executable,
                artifact_path=request.entrypoint_path,
                timeout_seconds=request.timeout_seconds,
                output_limit_bytes=max(
                    request.stdout_limit_bytes, request.stderr_limit_bytes
                ),
                profile="workspace-write",
            )
        except Exception as error:
            raise ControlledSkillProcessError(
                "controlled_skill_sandbox_launch_unavailable"
            ) from error
        return ControlledSkillLaunchPlan(
            argv=tuple(plan.argv),
            backend_id=str(plan.backend_id),
            contract_id=request.contract.contract_id,
        )


class ControlledSkillProcessRunner:
    """Execute one exact revision through a separately attested OS backend."""

    def __init__(
        self,
        store: LocalSkillBundleStore,
        *,
        backend: ControlledSkillProcessBackend,
        interpreters: Mapping[str, TrustedSkillInterpreter],
        timeout_seconds: float = 120.0,
    ) -> None:
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("controlled Skill timeout is invalid")
        self.store = store
        self.backend = backend
        self.interpreters = MappingProxyType(dict(interpreters))
        self.timeout_seconds = float(timeout_seconds)

    @property
    def unavailable_reason(self) -> str | None:
        return getattr(self.backend, "reason", None)

    def supports(self, runtime: str) -> bool:
        return runtime in self.interpreters and self.backend.supports(runtime)

    async def run(
        self,
        request: ControlledSkillRunRequest,
        *,
        state_fence: Callable[[], None],
    ) -> ControlledSkillRunResult:
        interpreter = self.interpreters.get(request.runtime)
        if interpreter is None or not self.backend.supports(request.runtime):
            raise ControlledSkillProcessError(
                self.unavailable_reason or "controlled_skill_backend_unavailable"
            )
        state_fence()
        bundle = await asyncio.to_thread(self.store.verify, request.artifact_sha256)
        entrypoint_path, entrypoint_record = await asyncio.to_thread(
            self.store.resolve_verified_file,
            request.artifact_sha256,
            request.entrypoint,
        )
        if not any(record.path == request.entrypoint for record in bundle.files):
            raise ControlledSkillProcessError("controlled_skill_entrypoint_missing")
        observed_interpreter = await asyncio.to_thread(
            _sha256_file, interpreter.executable
        )
        if observed_interpreter != interpreter.sha256:
            raise ControlledSkillProcessError("controlled_skill_interpreter_changed")
        contract = ControlledSkillProcessContract(
            extension_id=request.extension_id,
            revision_id=request.revision_id,
            extension_generation=request.extension_generation,
            cas_tree_sha256=request.artifact_sha256,
            entrypoint=request.entrypoint,
            entrypoint_sha256=entrypoint_record.sha256,
            runtime=request.runtime,
            interpreter_sha256=interpreter.sha256,
            # v1 skill-runtime.json has no argument declaration.  The only
            # admissible typed argument vector is therefore exactly empty.
            arguments=(),
            environment_names=tuple(sorted(request.environment)),
            network_domains=tuple(sorted(request.network_domains)),
            effects=tuple(request.effects),
        )
        launch_request = ControlledSkillLaunchRequest(
            contract=contract,
            cas_root=entrypoint_path.parents[len(Path(request.entrypoint).parts) - 1],
            entrypoint_path=entrypoint_path,
            interpreter_path=interpreter.executable,
            timeout_seconds=self.timeout_seconds,
            stdout_limit_bytes=MAX_SKILL_RESULT_BYTES,
            stderr_limit_bytes=MAX_SKILL_STDERR_BYTES,
        )
        plan = self.backend.launch_plan(launch_request)
        if plan.contract_id != contract.contract_id:
            raise ControlledSkillProcessError("controlled_skill_contract_not_bound")
        payload = _canonical_json(
            {
                "schema_version": 1,
                "protocol": CONTROLLED_SKILL_PROCESS_PROTOCOL,
                "contract_id": contract.contract_id,
                "parameters": dict(request.parameters),
            }
        )
        state_fence()
        result = await self._invoke(
            plan,
            payload=payload,
            environment=request.environment,
            state_fence=state_fence,
        )
        state_fence()
        # A successful response is accepted only while all executable bytes
        # still match both the CAS tree and interpreter identities.
        await asyncio.to_thread(self.store.verify, request.artifact_sha256)
        if await asyncio.to_thread(_sha256_file, interpreter.executable) != interpreter.sha256:
            raise ControlledSkillProcessError("controlled_skill_interpreter_changed")
        return ControlledSkillRunResult(result)

    async def _invoke(
        self,
        plan: ControlledSkillLaunchPlan,
        *,
        payload: bytes,
        environment: Mapping[str, str],
        state_fence: Callable[[], None],
    ) -> Mapping[str, Any]:
        child_environment = _minimal_environment()
        child_environment.update(environment)
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": child_environment,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(*plan.argv, **kwargs)
        except (OSError, ValueError) as error:
            raise ControlledSkillProcessError("controlled_skill_process_unavailable") from error
        assert process.stdin is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, MAX_SKILL_RESULT_BYTES)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, MAX_SKILL_STDERR_BYTES)
        )
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            while process.returncode is None:
                state_fence()
                for output_task in (stdout_task, stderr_task):
                    if output_task.done() and output_task.exception() is not None:
                        raise output_task.exception()  # type: ignore[misc]
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    await asyncio.wait_for(process.wait(), timeout=min(0.1, remaining))
                except TimeoutError:
                    continue
            stdout, _stderr = await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            await _kill_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except TimeoutError:
            await _kill_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise ControlledSkillProcessError("controlled_skill_process_timeout") from None
        except Exception:
            await _kill_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        await _kill_process_tree(process)
        if process.returncode != 0:
            raise ControlledSkillProcessError("controlled_skill_process_exited")
        return _parse_response(stdout, contract_id=plan.contract_id)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("controlled Skill protocol value is invalid") from error


def _parse_response(payload: bytes, *, contract_id: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_mapping,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ControlledSkillProcessError("controlled_skill_response_invalid") from error
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "protocol", "contract_id", "status", "result"}
        or value.get("schema_version") != 1
        or value.get("protocol") != CONTROLLED_SKILL_PROCESS_PROTOCOL
        or value.get("contract_id") != contract_id
        or value.get("status") != "completed"
        or not isinstance(value.get("result"), Mapping)
        or payload != _canonical_json(value)
    ):
        raise ControlledSkillProcessError("controlled_skill_response_invalid")
    return dict(value["result"])


def _unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate controlled Skill response key")
        result[key] = value
    return result


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        raise ControlledSkillProcessError("controlled_skill_process_pipe_missing")
    body = bytearray()
    while chunk := await stream.read(64 * 1024):
        if len(body) + len(chunk) > limit:
            raise ControlledSkillProcessError("controlled_skill_process_output_too_large")
        body.extend(chunk)
    return bytes(body)


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        pass


def _minimal_environment() -> dict[str, str]:
    allowed = {"LANG", "LC_ALL", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    result = {
        key.upper(): value
        for key, value in os.environ.items()
        if key.upper() in allowed and isinstance(value, str)
    }
    result.update(
        {
            "PATH": os.pathsep.join(("/usr/bin", "/bin")) if os.name != "nt" else "",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CONTROLLED_SKILL_PROCESS_PROTOCOL",
    "ControlledSkillLaunchPlan",
    "ControlledSkillLaunchRequest",
    "ControlledSkillProcessBackend",
    "ControlledSkillProcessContract",
    "ControlledSkillProcessError",
    "ControlledSkillProcessRunner",
    "TrustedSkillInterpreter",
    "UnavailableControlledSkillProcessBackend",
]
