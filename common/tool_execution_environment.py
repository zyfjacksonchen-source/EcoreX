"""Shared EcoreX-owned tool execution environment.

This layer centralizes PATH/NODE_PATH/PYTHONPATH construction, dependency
resolution, subprocess timeout/cancel behavior, and output redaction for local
tools. It deliberately defaults to the EcoreX runtime/state dependency provider.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import sysconfig
import time
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from importlib.machinery import BuiltinImporter, FrozenImporter
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, Iterator, List, Optional

from common.runtime_dependencies import (
    SOURCE_ECOREX_BUNDLED,
    SOURCE_ECOREX_STATE,
    SOURCE_MISSING,
    RuntimeDependency,
    RuntimeDependencyProvider,
    get_runtime_dependency_provider,
)


_SECRET_RE = re.compile(
    r"(?i)(['\"]?(?:api[_-]?key|token|password|secret|authorization|cookie|session)['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+)?[^'\",\s&}]+"
)


def _is_importer_proven_builtin_or_frozen(cache_key: str, module: ModuleType) -> bool:
    spec = getattr(module, "__spec__", None)
    origin = str(getattr(spec, "origin", "") or "")
    loader = getattr(spec, "loader", None)
    if origin == "built-in" and loader is BuiltinImporter and BuiltinImporter.find_spec(cache_key) is not None:
        return True
    if origin == "frozen" and loader is FrozenImporter and FrozenImporter.find_spec(cache_key) is not None:
        return True
    return False


_TRUSTED_CACHED_MODULES: Dict[str, ModuleType] = {
    name: module
    for name, module in list(sys.modules.items())
    if _is_importer_proven_builtin_or_frozen(name, module)
}


class ToolExecutionCancelled(Exception):
    def __init__(self, stdout: str = "", stderr: str = ""):
        super().__init__("tool execution cancelled")
        self.stdout = stdout or ""
        self.stderr = stderr or ""


@dataclass(frozen=True)
class PreparedCommand:
    command: List[str]
    env: Dict[str, str]
    dependency: RuntimeDependency
    missing: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.missing is None and self.dependency.available


def redact_text(value: str) -> str:
    text = value or ""
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1***", text)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", text)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}***", text)


def kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        os.killpg(process.pid, signal.SIGTERM)
        return
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass


class ToolExecutionEnvironment:
    """Resolve and execute tools through EcoreX-owned runtime dependencies."""

    def __init__(
        self,
        *,
        tool_name: str,
        provider: Optional[RuntimeDependencyProvider] = None,
        base_env: Optional[Dict[str, str]] = None,
        cwd: str | os.PathLike[str] | None = None,
        include_system_path: bool = False,
    ) -> None:
        self.tool_name = tool_name
        self.provider = provider or get_runtime_dependency_provider(env=base_env)
        self.base_env = dict(base_env or os.environ)
        self.cwd = str(cwd or os.getcwd())
        self.include_system_path = bool(include_system_path)

    def build_env(self, *, extra_paths: Optional[Iterable[Path | str]] = None) -> Dict[str, str]:
        return self.provider.build_env(
            base_env=self.base_env,
            include_system_path=self.include_system_path,
            extra_paths=extra_paths,
        )

    def resolve_executable(self, name: str, *, native: bool = False) -> RuntimeDependency:
        if native:
            return self.provider.resolve_native_bin(name, allow_system_path=self.include_system_path)
        return self.provider.resolve_executable(name, allow_system_path=self.include_system_path)

    def resolve_python(self) -> RuntimeDependency:
        return self.provider.python(allow_system_path=self.include_system_path)

    @contextmanager
    def owned_python_import_context(self) -> Iterator[None]:
        """Temporarily constrain Python imports to EcoreX-owned packages.

        Runtime probes already classify Python package ownership; this context
        makes the execution side use the same boundary instead of ambient
        site-packages that may happen to be earlier on sys.path.
        """

        original_sys_path = list(sys.path)
        removed_modules = self._evict_unowned_cached_modules()
        sys.path = self._owned_python_sys_path(original_sys_path)
        try:
            yield
        finally:
            for name, module in removed_modules.items():
                if name not in sys.modules:
                    sys.modules[name] = module
            sys.path = original_sys_path

    def import_python_module(self, module_name: str) -> ModuleType:
        clean_name = str(module_name or "").strip()
        if not clean_name:
            raise ImportError("missing_dependency: empty python module name")
        root_name = clean_name.split(".", 1)[0]
        dependency = self.provider.resolve_python_package(root_name)
        if not dependency.available:
            missing = self.provider.missing_dependency(dependency, required_by=self.tool_name)
            raise ImportError(f"missing_dependency: {missing}")

        with self.owned_python_import_context():
            module = import_module(clean_name)
        if not self._module_is_owned(module):
            raise ImportError(f"python module {clean_name} resolved outside EcoreX runtime")
        return module

    def prepare_command(
        self,
        command: List[str],
        *,
        required_by: str = "",
        native: bool = False,
        extra_paths: Optional[Iterable[Path | str]] = None,
    ) -> PreparedCommand:
        parts = [str(item) for item in command if str(item) != ""]
        if not parts:
            missing = self.provider.missing_dependency(
                RuntimeDependency("", "", SOURCE_MISSING, False),
                required_by=required_by or self.tool_name,
            )
            return PreparedCommand([], self.build_env(extra_paths=extra_paths), RuntimeDependency("", "", SOURCE_MISSING, False), missing)

        executable = parts[0]
        dependency = self._dependency_for_executable(executable, native=native)
        env = self.build_env(extra_paths=extra_paths)
        if dependency.available:
            return PreparedCommand([dependency.path, *parts[1:]], env, dependency)
        missing = self.provider.missing_dependency(dependency, required_by=required_by or self.tool_name)
        return PreparedCommand(parts, env, dependency, missing)

    def run_completed(
        self,
        command: List[str],
        *,
        timeout: int,
        cwd: str | os.PathLike[str] | None = None,
        env: Optional[Dict[str, str]] = None,
        cancel_event=None,
        input_text: Optional[str] = None,
        allow_external_executable: bool = False,
    ) -> subprocess.CompletedProcess:
        popen_kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if input_text is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        process = self.popen(
            command,
            cwd=cwd,
            env=env,
            allow_external_executable=allow_external_executable,
            **popen_kwargs,
        )
        deadline = time.time() + max(1, int(timeout or 1))
        pending_input = input_text
        while True:
            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=0.25)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pending_input = None
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    kill_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    raise ToolExecutionCancelled(stdout, stderr)
                if time.time() >= deadline:
                    kill_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)

    def popen(
        self,
        command: List[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Optional[Dict[str, str]] = None,
        allow_external_executable: bool = False,
        **kwargs: Any,
    ) -> subprocess.Popen:
        dependency = self._dependency_for_executable(str(command[0] if command else ""))
        if not dependency.available and not allow_external_executable:
            missing = self.provider.missing_dependency(dependency, required_by=self.tool_name)
            raise FileNotFoundError(f"missing_dependency: {missing}")
        popen_kwargs: Dict[str, Any] = {
            "cwd": str(cwd or self.cwd),
            "env": dict(env or self.build_env()),
            **kwargs,
        }
        if os.name == "nt":
            popen_kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            popen_kwargs.setdefault("start_new_session", True)
        return subprocess.Popen(command, **popen_kwargs)

    def _dependency_for_executable(self, executable: str, *, native: bool = False) -> RuntimeDependency:
        path = Path(executable)
        if path.is_absolute() or os.sep in executable or (os.altsep and os.altsep in executable):
            source = self.provider.classify_path(path)
            owned = source in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}
            runnable = path.exists() and path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))
            available = runnable and (owned or self.include_system_path)
            dependency_type = "native-bin" if native else "executable"
            return RuntimeDependency(path.name, str(path) if available else "", source if available else SOURCE_MISSING, available, dependency_type)
        return self.resolve_executable(executable, native=native)

    def _owned_python_sys_path(self, original_sys_path: Iterable[str]) -> List[str]:
        paths: List[str] = []
        seen: set[str] = set()

        def add(path: Path | str | None) -> None:
            if path is None:
                return
            try:
                resolved = Path(path).expanduser().resolve()
            except Exception:
                return
            key = os.path.normcase(str(resolved))
            if key in seen:
                return
            seen.add(key)
            paths.append(str(resolved))

        for path in self.provider.python_package_dirs():
            add(path)
        for item in original_sys_path:
            if not item:
                continue
            try:
                resolved = Path(item).expanduser().resolve()
            except Exception:
                continue
            if self.provider.classify_path(resolved) in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
                add(resolved)
            elif self._is_stdlib_path(resolved):
                add(resolved)
        return paths

    def _is_stdlib_path(self, path: Path) -> bool:
        text = str(path).replace("\\", "/").lower()
        if "site-packages" in text or "dist-packages" in text:
            return False
        for key in ("stdlib", "platstdlib"):
            value = sysconfig.get_paths().get(key)
            if not value:
                continue
            try:
                path.relative_to(Path(value).resolve())
                return True
            except Exception:
                continue
        for value in (sys.base_prefix, sys.exec_prefix):
            if not value:
                continue
            try:
                path.relative_to(Path(value).resolve())
                return True
            except Exception:
                continue
        return False

    def _evict_unowned_cached_modules(self, root_name: Optional[str] = None) -> Dict[str, ModuleType]:
        removed: Dict[str, ModuleType] = {}
        for name in list(sys.modules):
            if root_name and name != root_name and not name.startswith(f"{root_name}."):
                continue
            module = sys.modules.get(name)
            if module is not None and not self._module_is_allowed_cached(name, module):
                removed[name] = module
                sys.modules.pop(name, None)
        return removed

    def _module_is_owned(self, module: ModuleType) -> bool:
        module_file = getattr(module, "__file__", None)
        if module_file:
            return self.provider.classify_path(module_file) in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}
        module_paths = list(getattr(module, "__path__", []) or [])
        return bool(module_paths) and all(
            self.provider.classify_path(path) in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}
            for path in module_paths
        )

    def _module_is_allowed_cached(self, cache_key: str, module: ModuleType) -> bool:
        return _TRUSTED_CACHED_MODULES.get(cache_key) is module
