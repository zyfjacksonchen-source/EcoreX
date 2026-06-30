import os
import sys
import importlib
import importlib.machinery
import types
from pathlib import Path

import pytest

from common.runtime_dependencies import SOURCE_ECOREX_BUNDLED, SOURCE_MISSING, RuntimeDependencyProvider
from common.runtime_dependencies import RuntimeDependency
from common.tool_execution_environment import PreparedCommand, ToolExecutionEnvironment, redact_text


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def test_tool_execution_environment_prepares_owned_command_with_isolated_env(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    node_name = "node.exe" if os.name == "nt" else "node"
    node = _touch(runtime / "tools" / "node" / node_name)
    (runtime / "python" / "Lib" / "site-packages").mkdir(parents=True)
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": "SYSTEM_SHOULD_NOT_LEAK", "PYTHONPATH": "PY_SHOULD_NOT_LEAK"})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)

    prepared = env.prepare_command(["node", "--version"], required_by="unit")

    assert prepared.ok is True
    assert prepared.command[0] == str(node)
    assert prepared.dependency.source == SOURCE_ECOREX_BUNDLED
    assert "SYSTEM_SHOULD_NOT_LEAK" not in prepared.env["PATH"]
    assert "PY_SHOULD_NOT_LEAK" not in prepared.env["PYTHONPATH"]


def test_tool_execution_environment_missing_dependency_payload(tmp_path):
    provider = RuntimeDependencyProvider(tmp_path / "runtime", tmp_path / "state", env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)

    prepared = env.prepare_command(["lark-cli", "auth", "status"], required_by="feishu_cli")

    assert prepared.ok is False
    assert prepared.dependency.source == SOURCE_MISSING
    assert prepared.missing == {
        "status": "missing_dependency",
        "dependency": "lark-cli",
        "dependencyType": "executable",
        "source": SOURCE_MISSING,
        "requiredBy": "feishu_cli",
        "provider": "RuntimeDependencyProvider",
    }


def test_tool_execution_environment_rejects_absolute_system_path_by_default(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    foreign = _touch(tmp_path / "foreign" / ("node.exe" if os.name == "nt" else "node"))
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})

    strict = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    fallback = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env, include_system_path=True)

    assert strict.prepare_command([str(foreign)]).ok is False
    assert fallback.prepare_command([str(foreign)]).ok is True


def test_tool_execution_environment_system_runtime_opt_in_resolves_npx(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    system_bin = tmp_path / "system-bin"
    npx_name = "npx.cmd" if os.name == "nt" else "npx"
    npx = _touch(system_bin / npx_name)
    provider = RuntimeDependencyProvider(
        runtime,
        state,
        env={"PATH": str(system_bin), "ECOREX_ALLOW_SYSTEM_RUNTIME": "1"},
    )

    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    prepared = env.prepare_command([npx_name, "--version"], required_by="mcp:unit")

    assert prepared.ok is True
    assert Path(prepared.command[0]) == npx
    assert str(system_bin) in prepared.env["PATH"]


def test_tool_execution_environment_full_access_web_install_root_resolves_npx(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import get_tool_permission_broker

    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    system_bin = tmp_path / "system-bin"
    npx_name = "npx.cmd" if os.name == "nt" else "npx"
    npx = _touch(system_bin / npx_name)
    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    monkeypatch.delenv("ECOREX_ALLOW_SYSTEM_RUNTIME", raising=False)
    monkeypatch.delenv("ECOREX_INCLUDE_SYSTEM_PATH", raising=False)
    monkeypatch.delenv("ECOREX_TOOL_ENV_INCLUDE_SYSTEM_PATH", raising=False)
    get_tool_permission_broker().set_mode("full-access")
    provider = RuntimeDependencyProvider(
        runtime,
        state,
        env={"PATH": str(system_bin), "ECOREX_INSTALL_ROOT": str(runtime)},
    )

    env = ToolExecutionEnvironment(tool_name="mcp:unit", provider=provider, base_env=provider.env)
    prepared = env.prepare_command([npx_name, "--version"], required_by="mcp:unit")

    assert prepared.ok is True
    assert Path(prepared.command[0]) == npx


def test_tool_execution_environment_popen_rejects_unprepared_external_executable(tmp_path):
    provider = RuntimeDependencyProvider(tmp_path / "runtime", tmp_path / "state", env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)

    try:
        env.run_completed([sys.executable, "-c", "print('no')"], timeout=5)
    except FileNotFoundError as exc:
        assert "missing_dependency" in str(exc)
    else:
        raise AssertionError("external executable should be rejected by default")


def test_tool_execution_environment_run_completed_uses_supplied_env(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env, cwd=tmp_path)

    result = env.run_completed(
        [sys.executable, "-c", "import os; print(os.environ.get('ECOREX_TEST_FLAG', ''))"],
        timeout=5,
        env={"ECOREX_TEST_FLAG": "ok"},
        allow_external_executable=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_tool_execution_environment_import_python_module_uses_owned_package_over_ambient(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_pkg = runtime / "python" / "Lib" / "site-packages" / "shared_pkg"
    owned_site = owned_pkg.parent
    foreign_pkg = tmp_path / "foreign" / "shared_pkg"
    foreign_site = foreign_pkg.parent
    owned_pkg.mkdir(parents=True)
    foreign_pkg.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text("import helper_dep\nVALUE = helper_dep.VALUE\n", encoding="utf-8")
    (owned_site / "helper_dep.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (foreign_pkg / "__init__.py").write_text("import helper_dep\nVALUE = helper_dep.VALUE\n", encoding="utf-8")
    (foreign_site / "helper_dep.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(foreign_site))
    sys.modules.pop("helper_dep", None)
    sys.modules.pop("shared_pkg", None)
    assert importlib.import_module("shared_pkg").VALUE == "foreign"
    assert importlib.import_module("helper_dep").VALUE == "foreign"

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("shared_pkg")

    assert module.VALUE == "owned"
    assert Path(module.__file__).resolve().is_relative_to(owned_pkg)
    assert Path(sys.modules["helper_dep"].__file__).resolve() == owned_site / "helper_dep.py"
    sys.modules.pop("shared_pkg", None)
    sys.modules.pop("helper_dep", None)


def test_tool_execution_environment_import_python_module_evicts_synthetic_cached_dependency(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    owned_pkg = owned_site / "synthetic_pkg"
    owned_pkg.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text("import synthetic_dep\nVALUE = synthetic_dep.VALUE\n", encoding="utf-8")
    (owned_site / "synthetic_dep.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    poisoned = types.ModuleType("synthetic_dep")
    poisoned.VALUE = "poisoned"
    sys.modules["synthetic_dep"] = poisoned
    sys.modules.pop("synthetic_pkg", None)

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("synthetic_pkg")

    assert module.VALUE == "owned"
    assert Path(sys.modules["synthetic_dep"].__file__).resolve() == owned_site / "synthetic_dep.py"
    sys.modules.pop("synthetic_pkg", None)
    sys.modules.pop("synthetic_dep", None)


def test_tool_execution_environment_import_python_module_evicts_forged_builtin_cached_dependency(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    owned_pkg = owned_site / "forged_pkg"
    owned_pkg.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text("import forged_dep\nVALUE = forged_dep.VALUE\n", encoding="utf-8")
    (owned_site / "forged_dep.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    poisoned = types.ModuleType("forged_dep")
    poisoned.__spec__ = importlib.machinery.ModuleSpec("forged_dep", loader=None, origin="built-in")
    poisoned.VALUE = "poisoned"
    sys.modules["forged_dep"] = poisoned
    sys.modules.pop("forged_pkg", None)

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("forged_pkg")

    assert module.VALUE == "owned"
    assert Path(sys.modules["forged_dep"].__file__).resolve() == owned_site / "forged_dep.py"
    sys.modules.pop("forged_pkg", None)
    sys.modules.pop("forged_dep", None)


def test_tool_execution_environment_import_python_module_evicts_forged_builtin_name_dependency(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    owned_pkg = owned_site / "forged_name_pkg"
    owned_pkg.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text("import forged_name_dep\nVALUE = forged_name_dep.VALUE\n", encoding="utf-8")
    (owned_site / "forged_name_dep.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    poisoned = types.ModuleType("sys")
    poisoned.__spec__ = importlib.machinery.ModuleSpec("sys", loader=None, origin="built-in")
    poisoned.VALUE = "poisoned"
    sys.modules["forged_name_dep"] = poisoned
    sys.modules.pop("forged_name_pkg", None)

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("forged_name_pkg")

    assert module.VALUE == "owned"
    assert Path(sys.modules["forged_name_dep"].__file__).resolve() == owned_site / "forged_name_dep.py"
    sys.modules.pop("forged_name_pkg", None)
    sys.modules.pop("forged_name_dep", None)


def test_tool_execution_environment_import_python_module_evicts_forged_loader_dependency(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    for suffix, loader, origin in (
        ("builtin_loader", importlib.machinery.BuiltinImporter, "built-in"),
        ("frozen_loader", importlib.machinery.FrozenImporter, "frozen"),
    ):
        package_name = f"forged_{suffix}_pkg"
        dependency_name = f"forged_{suffix}_dep"
        owned_pkg = owned_site / package_name
        owned_pkg.mkdir(parents=True)
        (owned_pkg / "__init__.py").write_text(
            f"import {dependency_name}\nVALUE = {dependency_name}.VALUE\n",
            encoding="utf-8",
        )
        (owned_site / f"{dependency_name}.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
        poisoned = types.ModuleType(dependency_name)
        poisoned.__spec__ = importlib.machinery.ModuleSpec(dependency_name, loader=loader, origin=origin)
        poisoned.VALUE = "poisoned"
        sys.modules[dependency_name] = poisoned
        sys.modules.pop(package_name, None)

        provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
        env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
        module = env.import_python_module(package_name)

        assert module.VALUE == "owned"
        assert Path(sys.modules[dependency_name].__file__).resolve() == owned_site / f"{dependency_name}.py"
        sys.modules.pop(package_name, None)
        sys.modules.pop(dependency_name, None)


def test_tool_execution_environment_import_python_module_evicts_mixed_namespace_package(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    foreign_site = tmp_path / "foreign"
    owned_pkg = owned_site / "namespace_main"
    owned_namespace = owned_site / "namespace_dep"
    foreign_namespace = foreign_site / "namespace_dep"
    owned_pkg.mkdir(parents=True)
    owned_namespace.mkdir(parents=True)
    foreign_namespace.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text(
        "from namespace_dep import submod\nVALUE = submod.VALUE\n",
        encoding="utf-8",
    )
    (owned_namespace / "submod.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (foreign_namespace / "submod.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    namespace = types.ModuleType("namespace_dep")
    namespace.__path__ = [str(foreign_namespace), str(owned_namespace)]
    namespace.__spec__ = importlib.machinery.ModuleSpec("namespace_dep", loader=None, is_package=True)
    namespace.__spec__.submodule_search_locations = namespace.__path__
    sys.modules["namespace_dep"] = namespace
    sys.modules.pop("namespace_dep.submod", None)
    sys.modules.pop("namespace_main", None)

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("namespace_main")

    assert module.VALUE == "owned"
    assert Path(sys.modules["namespace_dep.submod"].__file__).resolve() == owned_namespace / "submod.py"
    sys.modules.pop("namespace_main", None)
    sys.modules.pop("namespace_dep", None)
    sys.modules.pop("namespace_dep.submod", None)


def test_tool_execution_environment_import_python_module_evicts_forged_file_dependency(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    owned_site = runtime / "python" / "Lib" / "site-packages"
    owned_pkg = owned_site / "forged_file_pkg"
    owned_pkg.mkdir(parents=True)
    (owned_pkg / "__init__.py").write_text("import forged_file_dep\nVALUE = forged_file_dep.VALUE\n", encoding="utf-8")
    owned_dependency = owned_site / "forged_file_dep.py"
    owned_dependency.write_text("VALUE = 'owned'\n", encoding="utf-8")
    poisoned = types.ModuleType("forged_file_dep")
    poisoned.__file__ = str(owned_dependency)
    poisoned.VALUE = "poisoned"
    sys.modules["forged_file_dep"] = poisoned
    sys.modules.pop("forged_file_pkg", None)

    provider = RuntimeDependencyProvider(runtime, state, env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    module = env.import_python_module("forged_file_pkg")

    assert module.VALUE == "owned"
    assert Path(sys.modules["forged_file_dep"].__file__).resolve() == owned_dependency
    sys.modules.pop("forged_file_pkg", None)
    sys.modules.pop("forged_file_dep", None)


def test_tool_execution_environment_cached_builtin_requires_original_module_object(tmp_path):
    provider = RuntimeDependencyProvider(tmp_path / "runtime", tmp_path / "state", env={"PATH": ""})
    env = ToolExecutionEnvironment(tool_name="unit", provider=provider, base_env=provider.env)
    fake_sys = types.ModuleType("sys")
    fake_sys.__spec__ = importlib.machinery.ModuleSpec(
        "sys",
        loader=importlib.machinery.BuiltinImporter,
        origin="built-in",
    )

    assert env._module_is_allowed_cached("sys", sys) is True
    assert env._module_is_allowed_cached("sys", fake_sys) is False


def test_tool_execution_environment_redacts_common_secret_shapes():
    text = "Authorization: Bearer abcdefghijk token=abcdefg12345 ghp_abcdefghijklmnopqrstuvwxyz"

    redacted = redact_text(text)

    assert "abcdefghijk" not in redacted
    assert "abcdefg12345" not in redacted
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in redacted


def test_tongxin_cli_reports_missing_ecorex_python(monkeypatch, tmp_path):
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli

    script = _touch(tmp_path / "xin_agent_cli.py")

    def fake_resolve_python(self):
        return RuntimeDependency("python", "", SOURCE_MISSING, False, "python")

    monkeypatch.setattr(ToolExecutionEnvironment, "resolve_python", fake_resolve_python)

    result = TongxinCli({"cwd": str(tmp_path), "script_path": str(script)}).execute({"action": "schema"})

    assert result.status == "error"
    assert result.result["status"] == "missing_dependency"
    assert result.result["dependency"] == "python"
    assert result.result["requiredBy"] == "tongxin_cli"


def test_mcp_stdio_missing_npx_fails_before_popen(monkeypatch):
    from agent.tools.mcp.mcp_client import McpClient

    popen_called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run when npx is missing")

    monkeypatch.setattr("agent.tools.mcp.mcp_client.subprocess.Popen", fake_popen)
    monkeypatch.setattr(McpClient, "_authorize_stdio_start", lambda self, command, args: True)

    client = McpClient({"name": "unit", "type": "stdio", "command": "npx", "args": ["missing-package"]})

    assert client._init_stdio() is False
    assert popen_called is False


def test_mcp_stdio_filters_path_like_config_env(monkeypatch):
    import io

    from agent.tools.mcp.mcp_client import McpClient

    captured = {}
    dependency = RuntimeDependency("npx", "C:/runtime/bin/npx.cmd", SOURCE_ECOREX_BUNDLED, True)

    def fake_prepare(self, command, **_kwargs):
        return PreparedCommand(list(command), {"PATH": "CLEAN", "NODE_PATH": "CLEAN_NODE", "PYTHONPATH": "CLEAN_PY"}, dependency)

    class FakeProc:
        pid = 123
        stdin = None
        stdout = io.StringIO("")
        stderr = io.StringIO("")

    def fake_popen(self, command, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(ToolExecutionEnvironment, "prepare_command", fake_prepare)
    monkeypatch.setattr(ToolExecutionEnvironment, "popen", fake_popen)
    monkeypatch.setattr(McpClient, "_authorize_stdio_start", lambda self, command, args: True)
    monkeypatch.setattr(McpClient, "_handshake", lambda self: True)

    client = McpClient({
        "name": "unit",
        "type": "stdio",
        "command": "npx",
        "args": ["pkg"],
        "env": {
            "path": "DIRTY",
            "PaTh": "DIRTY2",
            "NODE_PATH": "DIRTY_NODE",
            "PYTHONPATH": "DIRTY_PY",
            "NODE_OPTIONS": "--require C:/foreign/hook.js",
            "LD_PRELOAD": "/tmp/foreign.so",
            "DYLD_INSERT_LIBRARIES": "/tmp/foreign.dylib",
            "npm_config_script_shell": "C:/foreign/shell.cmd",
            "NPM_CONFIG_REGISTRY": "https://foreign.invalid",
            "SAFE_FLAG": "ok",
        },
    })

    assert client._init_stdio() is True
    assert captured["env"]["PATH"] == "CLEAN"
    assert captured["env"]["NODE_PATH"] == "CLEAN_NODE"
    assert captured["env"]["PYTHONPATH"] == "CLEAN_PY"
    assert "NODE_OPTIONS" not in captured["env"]
    assert "LD_PRELOAD" not in captured["env"]
    assert "DYLD_INSERT_LIBRARIES" not in captured["env"]
    assert "npm_config_script_shell" not in captured["env"]
    assert "NPM_CONFIG_REGISTRY" not in captured["env"]
    assert captured["env"]["SAFE_FLAG"] == "ok"


def test_feishu_direct_lark_binary_requires_runnable_file(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX execute-bit behavior is not meaningful on Windows")
    import agent.tools.feishu_cli.feishu_cli as feishu_module

    binary = _touch(tmp_path / "bin" / "lark-cli")
    binary.chmod(0o644)
    monkeypatch.setattr(feishu_module, "_candidate_bin_dirs", lambda: [binary.parent])

    assert feishu_module._find_direct_lark_binary() is None


def test_feishu_cli_allows_configured_system_node_without_global_path_leak(monkeypatch, tmp_path):
    import agent.tools.feishu_cli.feishu_cli as feishu_module
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    system_node = tmp_path / "system-node"
    other_bin = tmp_path / "other-bin"
    node_name = "node.exe" if os.name == "nt" else "node"
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    _touch(system_node / node_name)
    _touch(system_node / npm_name)
    _touch(system_node / ("lark-cli.cmd" if os.name == "nt" else "lark-cli"))
    foreign = _touch(other_bin / ("bad-node.exe" if os.name == "nt" else "bad-node"))
    monkeypatch.setenv("PATH", str(system_node) + os.pathsep + str(other_bin))
    monkeypatch.setenv("NODE_OPTIONS", f"--require {foreign}")
    monkeypatch.setenv("npm_config_script_shell", str(foreign))

    tool = FeishuCli({
        "cwd": str(tmp_path),
        "install_root": str(tmp_path / "state" / "tools" / "lark-cli"),
        "allow_system_node": True,
    })
    env = tool._env()
    node = feishu_module._which("node", env)
    npm = feishu_module._which("npm", env)

    assert env[feishu_module.SYSTEM_NODE_ENV_FLAG] == "1"
    assert str(system_node.resolve()) in env[feishu_module.SYSTEM_NODE_DIRS_ENV]
    assert str(other_bin) not in env["PATH"]
    assert "NODE_OPTIONS" not in env
    assert "npm_config_script_shell" not in env
    assert node and Path(node).name.lower() == node_name.lower()
    assert npm and Path(npm).name.lower() == npm_name.lower()
    assert feishu_module._is_allowed_system_node_command([node, "--version"], env) is True
    assert feishu_module._is_allowed_system_node_command([str(foreign), "--version"], env) is False
    assert feishu_module._resolve_lark_command(env) is None


def test_permission_broker_allows_structured_feishu_and_tongxin_readonly(monkeypatch, tmp_path):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()
    broker.set_mode("read-only")

    for action in ("status", "diagnose", "ensure", "config_init_status", "agent_auth_status", "auth_login_status", "auth_status"):
        decision = broker.authorize_noninteractive("feishu_cli", {"action": action})
        assert decision["allowed"] is True
        assert decision["reason"] == "default-read-only-feishu-cli"

    for args in (
        {"action": "install", "discovery_source": "find-skill"},
        {"action": "agent_auth"},
        {"action": "config_init"},
        {"action": "auth_login", "domain": "base"},
        {"action": "run", "args": ["base", "+record-list", "--as", "user"]},
        {"action": "run", "args": ["base", "+record-create", "--as", "user"]},
    ):
        decision = broker.authorize_noninteractive("feishu_cli", args)
        assert decision["allowed"] is True
        assert decision["reason"] == "default-structured-feishu-cli"

    tongxin_read = broker.authorize_noninteractive(
        "tongxin_cli",
        {"action": "run", "args": ["project", "list", "--source", "cache", "--limit", "1"]},
    )
    assert tongxin_read["allowed"] is True
    assert tongxin_read["reason"] == "default-read-only-tongxin-cli"

    tongxin_write = broker.authorize_noninteractive(
        "tongxin_cli",
        {"action": "run", "args": ["account", "update", "--account-id", "123"]},
    )
    assert tongxin_write["allowed"] is False
    assert "read-only" in tongxin_write["reason"]


def test_tongxin_env_script_paths_are_ecorex_owned_only(monkeypatch, tmp_path):
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli

    monkeypatch.setenv("ECOREX_TONGXIN_CLI_PATH", str(tmp_path / "external" / "xin_agent_cli.py"))
    monkeypatch.setenv("XIN_AGENT_CLI_PATH", str(tmp_path / "external2" / "xin_agent_cli.py"))
    monkeypatch.setenv("TONGXIN_CLI_PATH", str(tmp_path / "external3" / "xin_agent_cli.py"))

    assert TongxinCli({"cwd": str(tmp_path)})._env_script_path_values() == []


def test_optional_abilities_rejects_foreign_state_and_target_dirs(monkeypatch, tmp_path):
    import agent.tools.optional_abilities.optional_abilities as optional_abilities

    foreign_state = tmp_path / "foreign-state"
    foreign_target = tmp_path / "foreign-target"
    foreign_browsers = tmp_path / "foreign-browsers"
    foreign_target.mkdir()
    monkeypatch.setenv("ECOREX_CAPABILITY_STATE_DIR", str(foreign_state))
    monkeypatch.setenv("ECOREX_CAPABILITY_TARGET_DIR", str(foreign_target))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(foreign_browsers))

    before = set(sys.path)
    optional_abilities._add_capability_target_to_path(foreign_target)

    assert optional_abilities._state_dir() != foreign_state
    assert optional_abilities._capability_package_root() != foreign_target
    assert optional_abilities._playwright_browsers_dir() is None
    assert set(sys.path) == before
