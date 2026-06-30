import os
import sys
from pathlib import Path

from common.runtime_dependencies import (
    SOURCE_CODEX_PRIVATE,
    SOURCE_ECOREX_BUNDLED,
    SOURCE_ECOREX_STATE,
    SOURCE_MISSING,
    SOURCE_SYSTEM_PATH,
    RuntimeDependencyProvider,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def test_runtime_dependency_provider_resolves_ecorex_node_without_system_path(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    node_name = "node.exe" if os.name == "nt" else "node"
    _touch(runtime / "tools" / "node" / node_name)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    node = provider.resolve_executable("node")

    assert node.available is True
    assert node.source == SOURCE_ECOREX_BUNDLED
    assert node.path.endswith(node_name)


def test_runtime_dependency_provider_does_not_use_system_path_by_default(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    system = tmp_path / "system-bin"
    executable = "npx.cmd" if os.name == "nt" else "npx"
    _touch(system / executable)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": str(system)})

    strict = provider.resolve_executable("npx")
    fallback = provider.resolve_executable("npx", allow_system_path=True)

    assert strict.source == SOURCE_MISSING
    assert strict.available is False
    assert fallback.available is True
    assert fallback.source == SOURCE_SYSTEM_PATH


def test_runtime_dependency_provider_builds_isolated_tool_env(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    _touch(runtime / "tools" / "bin" / ("npm.cmd" if os.name == "nt" else "npm"))
    (state / "node" / "node_modules").mkdir(parents=True)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": "SHOULD_NOT_LEAK", "NODE_PATH": "ALSO_NOT"})

    env = provider.build_env(include_system_path=False)

    assert "SHOULD_NOT_LEAK" not in env["PATH"]
    assert str(runtime / "tools" / "bin") in env["PATH"]
    assert str(state / "node" / "node_modules") in env["NODE_PATH"]
    assert "ALSO_NOT" not in env["NODE_PATH"]


def test_runtime_dependency_provider_clears_inherited_node_path_without_owned_modules(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": "", "NODE_PATH": "SHOULD_NOT_LEAK", "PYTHONPATH": "PY_SHOULD_NOT_LEAK"})

    env = provider.build_env(include_system_path=False)

    assert "NODE_PATH" not in env
    assert "PYTHONPATH" not in env


def test_runtime_dependency_provider_classifies_ownership(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    provider = RuntimeDependencyProvider(runtime, state, env={})

    assert provider.classify_path(runtime / "bin" / "node") == SOURCE_ECOREX_BUNDLED
    assert provider.classify_path(state / "tools" / "lark-cli") == SOURCE_ECOREX_STATE
    assert provider.classify_path("C:/cli-main/bin/lark-cli.exe") == SOURCE_CODEX_PRIVATE


def test_runtime_dependency_provider_python_prefers_ecorex_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    python_path = runtime / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    _touch(python_path)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    python = provider.python()

    assert python.available is True
    assert python.source == SOURCE_ECOREX_BUNDLED
    assert python.dependency_type == "python"


def test_runtime_dependency_provider_python_requires_runnable_file(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    python_path = runtime / "python" / ("python.exe" if os.name == "nt" else "bin/python3")
    _touch(python_path)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    monkeypatch.setattr(provider, "_is_runnable_file", lambda path: False)

    python = provider.python()

    assert python.available is False
    assert python.source == SOURCE_MISSING


def test_runtime_dependency_provider_resolves_python_package_from_ecorex_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    package = runtime / "python" / "Lib" / "site-packages" / "ecorex_pkg"
    _touch(package / "__init__.py")
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    dependency = provider.resolve_python_package("ecorex_pkg")

    assert dependency.available is True
    assert dependency.source == SOURCE_ECOREX_BUNDLED
    assert dependency.dependency_type == "python-package"
    assert dependency.path.endswith("__init__.py")


def test_runtime_dependency_provider_resolves_python_package_from_posix_layout(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    package = runtime / "python" / "lib" / f"python{os.sys.version_info.major}.{os.sys.version_info.minor}" / "site-packages" / "posix_pkg"
    _touch(package / "__init__.py")
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    dependency = provider.resolve_python_package("posix_pkg")

    assert dependency.available is True
    assert dependency.source == SOURCE_ECOREX_BUNDLED


def test_runtime_dependency_provider_exposes_native_bins(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    executable = "pdfinfo.exe" if os.name == "nt" else "pdfinfo"
    _touch(runtime / "tools" / "poppler" / "bin" / executable)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    dependency = provider.resolve_native_bin("pdfinfo")

    assert dependency.available is True
    assert dependency.source == SOURCE_ECOREX_BUNDLED
    assert dependency.dependency_type == "native-bin"


def test_runtime_dependency_provider_missing_dependency_payload(tmp_path):
    provider = RuntimeDependencyProvider(tmp_path / "runtime", tmp_path / "state", env={"PATH": ""})

    missing = provider.resolve_executable("lark-cli")
    payload = provider.missing_dependency(missing, required_by="feishu_cli")

    assert payload == {
        "status": "missing_dependency",
        "dependency": "lark-cli",
        "dependencyType": "executable",
        "source": SOURCE_MISSING,
        "requiredBy": "feishu_cli",
        "provider": "RuntimeDependencyProvider",
    }


def test_runtime_dependency_provider_rejects_fake_opt_ecorex_prefix(tmp_path):
    provider = RuntimeDependencyProvider(tmp_path / "runtime", tmp_path / "state", env={})
    fake_opt = tmp_path / "opt" / "ecorex-web" / "current" / "runtime" / "bin" / "node"

    assert provider.classify_path(fake_opt) == SOURCE_SYSTEM_PATH


def test_runtime_dependency_provider_env_overrides_do_not_leak_into_isolated_env(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    foreign = tmp_path / "foreign-node"
    _touch(foreign / ("node.exe" if os.name == "nt" else "node"))
    (foreign / "node_modules").mkdir(parents=True)
    provider = RuntimeDependencyProvider(
        runtime,
        state,
        env={
            "PATH": "",
            "NODE_PATH": "",
            "ECOREX_NODE_ROOT": str(foreign),
            "ECOREX_NODE_MODULES": str(foreign / "node_modules"),
        },
    )

    env = provider.build_env(include_system_path=False, extra_paths=[foreign])
    node = provider.resolve_executable("node")

    assert node.available is False
    assert str(foreign) not in env["PATH"]
    assert "NODE_PATH" not in env or str(foreign) not in env["NODE_PATH"]


def test_runtime_dependency_provider_pythonpath_override_is_owned_filtered(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    foreign = tmp_path / "foreign-python"
    _touch(foreign / "foreign_pkg" / "__init__.py")
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": "", "ECOREX_PYTHONPATH": str(foreign)})

    dependency = provider.resolve_python_package("foreign_pkg")
    snapshot = provider.snapshot()

    assert dependency.available is False
    assert str(foreign) not in snapshot["pythonPackageDirs"]


def test_runtime_dependency_provider_build_env_uses_only_owned_pythonpath(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned = runtime / "python" / "Lib" / "site-packages"
    foreign = tmp_path / "foreign-python"
    owned.mkdir(parents=True)
    foreign.mkdir(parents=True)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": "", "PYTHONPATH": str(foreign)})

    env = provider.build_env(include_system_path=False)

    assert str(owned) in env["PYTHONPATH"]
    assert str(foreign) not in env["PYTHONPATH"]


def test_runtime_dependency_provider_snapshot_defaults_to_strict_resolution(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    system = tmp_path / "system-bin"
    _touch(system / ("node.exe" if os.name == "nt" else "node"))
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": str(system)})

    snapshot = provider.snapshot()

    assert snapshot["systemPathIncluded"] is False
    assert snapshot["dependencies"]["node"]["source"] == SOURCE_MISSING


def test_runtime_dependency_provider_state_root_falls_back_when_config_unavailable(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "config":
            raise ImportError("config unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = RuntimeDependencyProvider(tmp_path / "runtime", env={"PATH": ""})

    assert provider.state_root == (tmp_path / "runtime" / "state").resolve()
    assert provider.resolve_executable("node").source == SOURCE_MISSING


def test_runtime_dependency_provider_nested_default_state_classifies_as_state(tmp_path):
    from config import conf

    old_appdata_dir = conf().get("appdata_dir")
    conf()["appdata_dir"] = ""
    runtime = tmp_path / "runtime"
    try:
        provider = RuntimeDependencyProvider(runtime, env={"PATH": ""})

        assert provider.classify_path(runtime / "state" / "tools" / "bin" / "node") == SOURCE_ECOREX_STATE
    finally:
        conf()["appdata_dir"] = old_appdata_dir


def test_runtime_dependency_provider_linux_install_root_venv_is_owned_state(tmp_path):
    install_root = tmp_path / "ecorex-web"
    runtime = install_root / "releases" / "rel-1" / "runtime"
    state = install_root / "state"
    major_minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = install_root / "venv" / "lib64" / major_minor / "site-packages"
    _touch(site_packages / "web" / "__init__.py")
    provider = RuntimeDependencyProvider(
        runtime,
        state,
        env={"PATH": "", "ECOREX_INSTALL_ROOT": str(install_root)},
    )

    dependency = provider.resolve_python_package("web")

    assert dependency.available is True
    assert dependency.source == SOURCE_ECOREX_STATE
    assert str(site_packages) in provider.build_env()["PYTHONPATH"]
