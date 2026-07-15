"""EcoreX-owned runtime dependency discovery and tool environment helpers."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, asdict
from importlib.machinery import PathFinder
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SOURCE_ECOREX_BUNDLED = "ecorex-bundled"
SOURCE_ECOREX_STATE = "ecorex-state"
SOURCE_SYSTEM_PATH = "system-path"
SOURCE_CODEX_PRIVATE = "codex-private"
SOURCE_MISSING = "missing"


def _norm(path: Path | str | None) -> str:
    if not path:
        return ""
    return str(path).replace("\\", "/").rstrip("/")


def _safe_resolve(path: Path | str | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except Exception:
        try:
            return Path(path).expanduser()
        except Exception:
            return None


def _is_relative_to(path: Path | None, root: Path | None) -> bool:
    if not path or not root:
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _is_lexically_relative_to(path: Path | None, root: Path | None) -> bool:
    if not path or not root:
        return False
    try:
        raw_path = Path(path).expanduser()
        raw_root = Path(root).expanduser()
        if not raw_path.is_absolute():
            raw_path = raw_path.absolute()
        if not raw_root.is_absolute():
            raw_root = raw_root.absolute()
        raw_path.relative_to(raw_root)
        return True
    except Exception:
        return False


def _dedupe_paths(paths: Iterable[Path | None]) -> List[Path]:
    result: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        resolved = _safe_resolve(path)
        if not resolved:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _redact_path(value: str) -> str:
    if not value:
        return ""
    text = str(value).replace("\\", "/")
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if home and text.lower().startswith(home.lower()):
        return "%USERPROFILE%" + text[len(home) :]
    return text


def runtime_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def _state_root_from_config(runtime_root: Path) -> Optional[Path]:
    env_state = os.environ.get("ECOREX_STATE_DIR") or os.environ.get("ECOREX_RUNTIME_STATE_DIR")
    if env_state:
        resolved = _safe_resolve(env_state)
        if resolved:
            return resolved
    try:
        from config import conf

        appdata = conf().get("appdata_dir")
        if appdata:
            path = _safe_resolve(str(appdata))
            if path and path.name.lower() == "appdata":
                return path.parent
            return path
    except Exception:
        return runtime_root / "state"
    return runtime_root / "state"


@dataclass(frozen=True)
class RuntimeDependency:
    name: str
    path: str
    source: str
    available: bool
    dependency_type: str = "executable"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RuntimeDependencyProvider:
    """Resolve dependencies owned by the EcoreX runtime or EcoreX state.

    System PATH is deliberately not used unless a caller asks for it. This keeps
    development-only tools from making packaged runtimes look healthier than
    they are on clean user machines or production service accounts.
    """

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        state_root: Path | str | None = None,
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.env = dict(env or os.environ)
        self.runtime_root = _safe_resolve(runtime_root) or runtime_root_from_here()
        env_state = self.env.get("ECOREX_STATE_DIR") or self.env.get("ECOREX_RUNTIME_STATE_DIR")
        self.state_root = (
            _safe_resolve(state_root)
            or _safe_resolve(env_state)
            or _state_root_from_config(self.runtime_root)
            or (self.runtime_root / "state")
        )

    def _install_roots(self) -> List[Path]:
        candidates: List[Path | None] = []
        env_root = self.env.get("ECOREX_INSTALL_ROOT") or self.env.get("INSTALL_ROOT")
        candidates.append(_safe_resolve(env_root))
        runtime_parts = list(self.runtime_root.parts)
        lowered = [part.lower() for part in runtime_parts]
        if len(runtime_parts) >= 3 and lowered[-2:] == ["current", "runtime"]:
            candidates.append(self.runtime_root.parents[1])
        if len(runtime_parts) >= 4 and lowered[-1] == "runtime" and lowered[-3] == "releases":
            candidates.append(self.runtime_root.parents[2])
        if _norm(self.runtime_root).lower().startswith("/opt/ecorex-web/") or _norm(self.state_root).lower().startswith("/opt/ecorex-web/"):
            candidates.append(Path("/opt/ecorex-web"))
        roots: List[Path] = []
        for root in _dedupe_paths(candidates):
            if (
                _is_relative_to(self.runtime_root, root)
                or _is_relative_to(self.state_root, root)
                or _norm(root).lower() == "/opt/ecorex-web"
            ):
                roots.append(root)
        return roots

    def classify_path(self, path: Path | str | None) -> str:
        resolved = _safe_resolve(path)
        if not resolved:
            return SOURCE_MISSING
        text = _norm(resolved).lower()
        if _is_relative_to(resolved, self.state_root):
            return SOURCE_ECOREX_STATE
        if _is_relative_to(resolved, self.runtime_root):
            return SOURCE_ECOREX_BUNDLED
        for root in self._install_roots():
            if _is_relative_to(resolved, root):
                return SOURCE_ECOREX_STATE
        if text == "/opt/ecorex-web" or text.startswith("/opt/ecorex-web/"):
            return SOURCE_ECOREX_STATE if "/state/" in text else SOURCE_ECOREX_BUNDLED
        if (
            "/.cache/codex-runtimes/" in text
            or "/.codex/" in text
            or "/.workbuddy/" in text
            or text.startswith("c:/cli-main")
        ):
            return SOURCE_CODEX_PRIVATE
        return SOURCE_SYSTEM_PATH

    def _is_owned_path(self, path: Path | str | None) -> bool:
        return self.classify_path(path) in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}

    def _lexical_owned_source(self, path: Path | str | None) -> str:
        candidate = Path(path).expanduser() if path else None
        if not candidate:
            return SOURCE_MISSING
        if _is_lexically_relative_to(candidate, self.state_root):
            return SOURCE_ECOREX_STATE
        if _is_lexically_relative_to(candidate, self.runtime_root):
            return SOURCE_ECOREX_BUNDLED
        for root in self._install_roots():
            if _is_lexically_relative_to(candidate, root):
                return SOURCE_ECOREX_STATE
        return SOURCE_MISSING

    def _python_launcher_source(self, path: Path | str | None) -> str:
        candidate = Path(path).expanduser() if path else None
        if not candidate:
            return SOURCE_MISSING
        name = candidate.name.lower()
        if os.name == "nt":
            valid_name = name in {"python.exe", "pythonw.exe"}
        else:
            valid_name = name == "python" or name.startswith("python3")
        if not valid_name:
            return SOURCE_MISSING
        lexical_source = self._lexical_owned_source(candidate)
        if lexical_source not in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
            return SOURCE_MISSING
        normalized = _norm(candidate).lower()
        if "/venv/" not in normalized and "/python/" not in normalized and "\\venv\\" not in str(candidate).lower():
            return SOURCE_MISSING
        return lexical_source

    def _owned_existing_paths(self, paths: Iterable[Path | None]) -> List[Path]:
        return [path for path in _dedupe_paths(paths) if path.exists() and self._is_owned_path(path)]

    def _is_runnable_file(self, path: Path) -> bool:
        if not path.exists() or not path.is_file():
            return False
        return os.name == "nt" or os.access(path, os.X_OK)

    def _node_roots(self) -> List[Path]:
        env_root = self.env.get("ECOREX_NODE_ROOT") or self.env.get("ECOREX_RUNTIME_NODE_ROOT")
        roots = [
            _safe_resolve(env_root),
            self.runtime_root / "node",
            self.runtime_root / "tools" / "node",
            self.runtime_root / "dependencies" / "node",
            self.state_root / "node",
            self.state_root / "tools" / "node",
        ]
        for root in self._install_roots():
            roots.extend([
                root / "node",
                root / "tools" / "node",
                root / "current" / "node",
            ])
        return [path for path in _dedupe_paths(roots) if self._is_owned_path(path)]

    def native_bin_dirs(self) -> List[Path]:
        dirs: List[Path] = [
            self.runtime_root / "bin",
            self.runtime_root / "tools" / "bin",
            self.runtime_root / "tools" / "poppler" / "bin",
            self.runtime_root / "tools" / "libreoffice" / "program",
            self.runtime_root / "tools" / "tesseract",
            self.state_root / "bin",
            self.state_root / "tools" / "bin",
            self.state_root / "tools" / "poppler" / "bin",
            self.state_root / "tools" / "libreoffice" / "program",
            self.state_root / "tools" / "tesseract",
        ]
        return self._owned_existing_paths(dirs)

    def bin_dirs(self) -> List[Path]:
        dirs: List[Path] = [
            self.runtime_root / "tools" / "bin",
            self.runtime_root / "tools" / "lark-cli" / "bin",
            self.runtime_root / "tools" / "lark-cli" / "node_modules" / ".bin",
            self.runtime_root / "node_modules" / ".bin",
            self.state_root / "tools" / "bin",
            self.state_root / "tools" / "lark-cli" / "bin",
            self.state_root / "tools" / "lark-cli" / "node_modules" / ".bin",
            self.state_root / "node_modules" / ".bin",
        ]
        dirs.extend(self.native_bin_dirs())
        for root in self._node_roots():
            dirs.extend([root, root / "bin", root / "node_modules" / ".bin"])
        return self._owned_existing_paths(dirs)

    def node_modules_dirs(self) -> List[Path]:
        env_value = self.env.get("ECOREX_NODE_MODULES") or self.env.get("ECOREX_NODE_MODULES_PATH")
        dirs: List[Path | None] = [
            _safe_resolve(env_value),
            self.runtime_root / "node_modules",
            self.runtime_root / "tools" / "node_modules",
            self.state_root / "node_modules",
            self.state_root / "tools" / "node_modules",
        ]
        for root in self._node_roots():
            dirs.append(root / "node_modules")
        return self._owned_existing_paths(dirs)

    def playwright_browser_dirs(self) -> List[Path]:
        env_value = self.env.get("PLAYWRIGHT_BROWSERS_PATH") or self.env.get("ECOREX_PLAYWRIGHT_BROWSERS_DIR")
        dirs: List[Path | None] = [
            _safe_resolve(env_value),
            self.runtime_root / "playwright-browsers",
            self.state_root / "playwright-browsers",
        ]
        return self._owned_existing_paths(dirs)

    def python_package_dirs(self) -> List[Path]:
        major_minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
        compact = f"python{sys.version_info.major}{sys.version_info.minor}"
        install_venv_dirs: List[Path] = []
        for root in self._install_roots():
            install_venv_dirs.extend([
                root / "venv" / "Lib" / "site-packages",
                root / "venv" / "lib" / major_minor / "site-packages",
                root / "venv" / "lib" / compact / "site-packages",
                root / "venv" / "lib64" / major_minor / "site-packages",
                root / "venv" / "lib64" / compact / "site-packages",
            ])
        dirs: List[Path | None] = [
            self.runtime_root,
            self.runtime_root / "python" / "Lib" / "site-packages",
            self.runtime_root / "python" / "lib" / major_minor / "site-packages",
            self.runtime_root / "python" / "lib" / compact / "site-packages",
            self.runtime_root / "venv" / "Lib" / "site-packages",
            self.runtime_root / "venv" / "lib" / major_minor / "site-packages",
            self.runtime_root / "venv" / "lib64" / major_minor / "site-packages",
            self.state_root / "python" / "Lib" / "site-packages",
            self.state_root / "python" / "lib" / major_minor / "site-packages",
            self.state_root / "python" / "lib" / compact / "site-packages",
            self.state_root / "venv" / "Lib" / "site-packages",
            self.state_root / "venv" / "lib" / major_minor / "site-packages",
            self.state_root / "venv" / "lib64" / major_minor / "site-packages",
            *install_venv_dirs,
        ]
        extra = self.env.get("ECOREX_PYTHONPATH") or self.env.get("ECOREX_PYTHON_PACKAGE_DIRS")
        if extra:
            dirs.extend(Path(item).expanduser() for item in extra.split(os.pathsep) if item)
        return self._owned_existing_paths(dirs)

    def resolve_executable(self, name: str, *, allow_system_path: bool = False) -> RuntimeDependency:
        clean_name = str(name or "").strip()
        if not clean_name:
            return RuntimeDependency(name="", path="", source=SOURCE_MISSING, available=False)
        candidates: List[Path] = []
        names = [clean_name]
        if os.name == "nt":
            lower = clean_name.lower()
            if not lower.endswith((".cmd", ".exe", ".bat", ".ps1")):
                names.extend([clean_name + ".cmd", clean_name + ".exe"])
        for directory in self.bin_dirs():
            for item in names:
                candidates.append(directory / item)
        explicit_env = self.env.get(f"ECOREX_{clean_name.upper().replace('-', '_')}_PATH")
        if explicit_env:
            candidates.insert(0, Path(explicit_env).expanduser())
        for candidate in _dedupe_paths(candidates):
            if self._is_runnable_file(candidate):
                source = self.classify_path(candidate)
                if source in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
                    return RuntimeDependency(clean_name, str(candidate), source, True)
        if allow_system_path:
            found = shutil.which(clean_name, path=self.env.get("PATH"))
            if not found and os.name == "nt" and not clean_name.lower().endswith(".cmd"):
                found = shutil.which(clean_name + ".cmd", path=self.env.get("PATH"))
            if found:
                return RuntimeDependency(clean_name, found, self.classify_path(found), True)
        return RuntimeDependency(clean_name, "", SOURCE_MISSING, False)

    def resolve_native_bin(self, name: str, *, allow_system_path: bool = False) -> RuntimeDependency:
        dependency = self.resolve_executable(name, allow_system_path=allow_system_path)
        return RuntimeDependency(dependency.name, dependency.path, dependency.source, dependency.available, "native-bin")

    def python(self, *, allow_system_path: bool = False) -> RuntimeDependency:
        candidates = [
            self.env.get("ECOREX_PYTHON_PATH"),
            sys.executable,
            self.runtime_root / "python" / "python.exe",
            self.runtime_root / "python" / "bin" / "python3",
            self.runtime_root / "venv" / "bin" / "python",
            self.runtime_root / "venv" / "Scripts" / "python.exe",
        ]
        for root in self._install_roots():
            candidates.extend([
                root / "venv" / "bin" / "python",
                root / "venv" / "bin" / "python3",
                root / "venv" / "Scripts" / "python.exe",
            ])
        seen: set[str] = set()
        for item in candidates:
            if not item:
                continue
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = candidate.absolute()
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            if self._is_runnable_file(candidate):
                source = self.classify_path(candidate)
                if source not in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
                    source = self._python_launcher_source(candidate)
                if source in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
                    return RuntimeDependency("python", str(candidate), source, True, "python")
        dependency = self.resolve_executable("python", allow_system_path=allow_system_path)
        return RuntimeDependency("python", dependency.path, dependency.source, dependency.available, "python")

    def resolve_python_package(self, module_name: str, *, allow_system_path: bool = False) -> RuntimeDependency:
        clean_name = str(module_name or "").strip()
        if not clean_name:
            return RuntimeDependency("", "", SOURCE_MISSING, False, "python-package")
        search_paths = [str(path) for path in self.python_package_dirs()]
        spec = PathFinder.find_spec(clean_name, search_paths)
        if spec:
            locations = list(spec.submodule_search_locations or [])
            path = spec.origin or (locations[0] if locations else "")
            source = self.classify_path(path)
            if source in {SOURCE_ECOREX_BUNDLED, SOURCE_ECOREX_STATE}:
                return RuntimeDependency(clean_name, str(path), source, True, "python-package")
        if allow_system_path:
            system_spec = find_spec(clean_name)
            if system_spec:
                locations = list(system_spec.submodule_search_locations or [])
                path = system_spec.origin or (locations[0] if locations else "")
                return RuntimeDependency(clean_name, str(path), self.classify_path(path), True, "python-package")
        return RuntimeDependency(clean_name, "", SOURCE_MISSING, False, "python-package")

    def missing_dependency(self, dependency: RuntimeDependency, *, required_by: str = "") -> Dict[str, Any]:
        return {
            "status": "missing_dependency",
            "dependency": dependency.name,
            "dependencyType": dependency.dependency_type,
            "source": dependency.source,
            "requiredBy": required_by,
            "provider": "RuntimeDependencyProvider",
        }

    def build_env(
        self,
        *,
        base_env: Optional[Dict[str, str]] = None,
        include_system_path: bool = False,
        extra_paths: Optional[Iterable[Path | str]] = None,
    ) -> Dict[str, str]:
        env = dict(base_env or self.env)
        path_parts = [str(path) for path in self.bin_dirs()]
        if extra_paths:
            for path in extra_paths:
                resolved = _safe_resolve(path)
                if include_system_path or self._is_owned_path(resolved):
                    path_parts.append(str(resolved or path))
        if include_system_path and env.get("PATH"):
            path_parts.append(env["PATH"])
        env["PATH"] = os.pathsep.join(item for item in path_parts if item)
        node_modules = [str(path) for path in self.node_modules_dirs()]
        if node_modules:
            existing = env.get("NODE_PATH")
            if include_system_path and existing:
                node_modules.append(existing)
            env["NODE_PATH"] = os.pathsep.join(node_modules)
        elif not include_system_path:
            env.pop("NODE_PATH", None)
        python_paths = [str(path) for path in self.python_package_dirs()]
        if python_paths:
            existing_pythonpath = env.get("PYTHONPATH")
            if include_system_path and existing_pythonpath:
                python_paths.append(existing_pythonpath)
            env["PYTHONPATH"] = os.pathsep.join(python_paths)
        elif not include_system_path:
            env.pop("PYTHONPATH", None)
        browser_dirs = self.playwright_browser_dirs()
        if browser_dirs:
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dirs[0]))
            env.setdefault("ECOREX_PLAYWRIGHT_BROWSERS_DIR", str(browser_dirs[0]))
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def snapshot(self, *, include_system_path: bool = False, redact_paths: bool = True) -> Dict[str, Any]:
        dependencies = {
            "node": self.resolve_executable("node", allow_system_path=include_system_path).to_dict(),
            "npm": self.resolve_executable("npm", allow_system_path=include_system_path).to_dict(),
            "npx": self.resolve_executable("npx", allow_system_path=include_system_path).to_dict(),
            "larkCli": self.resolve_executable("lark-cli", allow_system_path=include_system_path).to_dict(),
            "python": self.python(allow_system_path=include_system_path).to_dict(),
        }
        if redact_paths:
            for dependency in dependencies.values():
                dependency["path"] = _redact_path(str(dependency.get("path") or ""))
        path_value = (lambda item: _redact_path(str(item))) if redact_paths else str
        return {
            "schemaVersion": "v0.2.5-runtime-dependencies-v1",
            "runtimeRoot": path_value(self.runtime_root),
            "stateRoot": path_value(self.state_root),
            "binDirs": [path_value(path) for path in self.bin_dirs()],
            "nativeBinDirs": [path_value(path) for path in self.native_bin_dirs()],
            "nodeModulesDirs": [path_value(path) for path in self.node_modules_dirs()],
            "playwrightBrowserDirs": [path_value(path) for path in self.playwright_browser_dirs()],
            "pythonPackageDirs": [path_value(path) for path in self.python_package_dirs()],
            "dependencies": dependencies,
            "systemPathIncluded": bool(include_system_path),
        }


def get_runtime_dependency_provider(
    runtime_root: Path | str | None = None,
    state_root: Path | str | None = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> RuntimeDependencyProvider:
    return RuntimeDependencyProvider(runtime_root=runtime_root, state_root=state_root, env=env)
