from __future__ import annotations

import asyncio
import ast
import base64
import inspect
from pathlib import Path
import runpy
import subprocess
import sys
import threading
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def test_cow_logger_stays_out_of_the_signed_runtime_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    from common.log import _runtime_log_path

    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    assert _runtime_log_path() == tmp_path / "run.log"

    monkeypatch.delenv("EMATE_DATA_DIR")
    assert _runtime_log_path() is None


def test_platform_python_closure_imports_the_real_cow_spine(
    tmp_path: Path, monkeypatch,
) -> None:
    """The signed Python closure, not the source checkout, owns Cow imports."""

    stager = runpy.run_path(str(ROOT / "platform-staging" / "stager.py"))
    runtime_globals = stager["_build_python_closure"].__globals__
    copy_distribution_closure = runtime_globals["_copy_distribution_closure"]
    source = tmp_path / "python-source"
    stdlib = source / "stdlib"
    executable = source / "python3"
    (stdlib / "encodings").mkdir(parents=True)
    (stdlib / "encodings" / "__init__.py").write_text("", encoding="utf-8")
    executable.write_bytes(b"python")
    executable.chmod(0o755)

    monkeypatch.setitem(
        runtime_globals,
        "_base_python_runtime_source",
        lambda _platform: (source, executable, stdlib),
    )
    monkeypatch.setitem(
        runtime_globals,
        "_copy_distribution_closure",
        lambda _distributions, destination: copy_distribution_closure(
            ("regex",), destination
        ),
    )
    for name in (
        "_prune_macos_cpython_build_support",
        "_reject_macos_build_objects",
        "_prune_runtime_tree",
        "_relocate_macos_python_closure",
    ):
        monkeypatch.setitem(runtime_globals, name, lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        runtime_globals,
        "_compact_python_import_closure",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setitem(
        runtime_globals,
        "build_pack_python_manifest",
        lambda *_args, **_kwargs: b"{}",
    )
    monkeypatch.setitem(
        runtime_globals,
        "resolve_pack_python",
        lambda core, **_kwargs: (core / "bin" / "pack-python" / "bin" / "python3", None),
    )
    monkeypatch.setitem(runtime_globals, "_tree_binding_sha256", lambda _root: "stable")
    monkeypatch.setitem(
        runtime_globals,
        "_run_macos_isolated_pack_probe",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=runtime_globals["__version__"].encode("ascii")
        ),
    )

    core = tmp_path / "core"
    stager["_build_python_closure"](core, "macos", "arm64")
    site_packages = (
        core
        / "bin"
        / "pack-python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    probe = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; "
            f"sys.path.insert(0, {str(site_packages)!r}); "
            "import agent, bridge; "
            "import regex; "
            "from bridge.agent_initializer import AgentInitializer; "
            "from agent.tools.search_files.search_files import SearchFiles; "
            "from agent.tools.tool_manager import ToolManager; "
            "assert AgentInitializer.__module__ == 'bridge.agent_initializer'; "
            "assert SearchFiles.__module__ == 'agent.tools.search_files.search_files'; "
            "assert ToolManager.__module__ == 'agent.tools.tool_manager'; "
            f"assert regex.__file__.startswith({str(site_packages)!r})",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    production_probe = stager["_pack_python_probe_command"](Path("pack-python"))[-1]
    assert "from bridge.agent_initializer import AgentInitializer" in production_probe
    assert "from agent.tools.search_files.search_files import SearchFiles" in production_probe
    assert "from agent.tools.tool_manager import ToolManager" in production_probe
    assert "import regex" in production_probe
    assert "regex" in runtime_globals["_RUNTIME_DISTRIBUTIONS"]


def test_actual_initializer_and_tool_manager_are_the_default_tool_contract(
    tmp_path: Path, monkeypatch,
) -> None:
    """Exercise Cow's real tool source; a hand-maintained builtin catalog is irrelevant."""

    from agent.tools.tool_manager import ToolManager
    from bridge.agent_initializer import AgentInitializer

    monkeypatch.setattr(ToolManager, "_instance", None)
    manager = ToolManager()
    manager.load_tools(start_mcp=False)
    manager_contract = manager.list_tools()
    initializer = AgentInitializer(SimpleNamespace(), SimpleNamespace())
    tools = initializer._load_tools(str(tmp_path), None, [], "contract-session")
    actual_contract = {
        tool.name: {
            "description": tool.description,
            "parameters": tool.get_json_schema(),
        }
        for tool in tools
    }

    assert {"read", "write", "edit", "bash", "ls", "web_fetch", "browser"} <= set(
        actual_contract
    )
    assert actual_contract == {
        name: manager_contract[name] for name in actual_contract
    }
    for tool in tools:
        if tool.name in {"read", "write", "edit", "bash", "ls", "web_fetch", "browser"}:
            assert Path(tool.config["cwd"]) == tmp_path


def test_actual_initializer_keeps_the_complete_cow_catalog_for_every_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    """A local e-Mate feature flag must not hide a Cow model-visible tool."""

    import config as config_module
    from agent.protocol import Agent
    from agent.tools.memory.memory_get import MemoryGetTool
    from agent.tools.memory.memory_search import MemorySearchTool
    from bridge.agent_initializer import AgentInitializer

    required = {
        "read", "write", "edit", "bash", "subagent", "search_files", "ls",
        "send", "evolution_undo", "env_config", "scheduler", "web_search",
        "web_fetch", "vision", "ocr", "browser", "imagegen", "memory_search",
        "memory_get", "office_documents", "office_pdf", "office_presentations",
        "office_spreadsheets",
    }
    workspace_tools = {
        "read", "write", "edit", "bash", "search_files", "ls", "web_fetch",
        "send", "browser", "ocr", "office_documents", "office_pdf",
        "office_presentations", "office_spreadsheets",
    }

    for profile_name, profile in (
        ("fresh", {}),
        ("legacy-disabled-evolution", {"self_evolution_enabled": False}),
    ):
        monkeypatch.setattr(config_module, "config", config_module.Config(profile))
        workspace = tmp_path / profile_name
        memory_manager = SimpleNamespace()
        memory_tools = [
            MemorySearchTool(memory_manager),
            MemoryGetTool(memory_manager),
        ]
        initializer = AgentInitializer(SimpleNamespace(), SimpleNamespace())
        tools = initializer._load_tools(
            str(workspace), memory_manager, memory_tools, "contract-session"
        )
        by_name = {tool.name: tool for tool in tools}

        assert len(tools) == len(required)
        assert set(by_name) == required
        assert "desktop_update" not in by_name
        for name in workspace_tools:
            assert Path(by_name[name].config["cwd"]) == workspace
        agent = Agent("contract", tools=tools, workspace_dir=str(workspace), enable_skills=False)
        assert Path(agent._find_tool("evolution_undo").context.workspace_dir) == workspace


def test_public_cow_worker_does_not_replace_the_cow_browser_executor(
    tmp_path: Path,
) -> None:
    from ecorex.runtime.worker import AgentTurnWorker

    worker = AgentTurnWorker(
        SimpleNamespace(database=SimpleNamespace(path=tmp_path / "runtime.db")),
        gateway=SimpleNamespace(),
        browser_handler=lambda *_args: None,
    )

    assert not hasattr(worker, "browser_handler")
    assert not hasattr(worker, "_bind_browser_pack")


def test_browser_runtime_matches_the_cow_desktop_contract(tmp_path: Path, monkeypatch) -> None:
    from agent.tools.browser import browser_env
    from agent.tools.browser.browser_tool import BrowserTool

    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COW_DESKTOP", "1")
    monkeypatch.setattr(browser_env, "detect_system_chrome", lambda: None)
    tool = BrowserTool()

    assert tool.config.get("cdp_endpoint") is None
    assert browser_env.browsers_download_dir() == str(tmp_path / "ms-playwright")
    assert tool._check_engine_ready().status == "error"
    assert "/install-browser" in tool._check_engine_ready().result


def test_cow_browser_only_uses_cdp_when_the_user_configures_an_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent.tools.browser import browser_env
    from agent.tools.browser.browser_service import BrowserService

    monkeypatch.setattr(
        browser_env,
        "resolve_engine",
        lambda _config: {
            "mode": "system-chrome",
            "channel": "chrome",
            "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "has_playwright": True,
            "reason": "test system Chrome",
        },
    )

    default = BrowserService({"user_data_dir": str(tmp_path / "profile")})
    explicit = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222"})

    assert default._launch_mode == "persistent"
    assert default._channel == "chrome"
    assert explicit._launch_mode == "cdp"


def test_cow_browser_honours_the_packaged_playwright_browser_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent.tools.browser import browser_env

    bundled = tmp_path / "runtime" / "payload" / "ms-playwright"
    (bundled / "chromium_headless_shell-1169").mkdir(parents=True)
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(bundled))

    assert browser_env.browsers_download_dir() == str(bundled)
    assert browser_env.has_downloaded_chromium() is True


def test_packaged_cow_browser_uses_its_relocatable_headless_shell(monkeypatch) -> None:
    from agent.tools.browser import browser_env
    from agent.tools.browser.browser_service import BrowserService

    monkeypatch.setenv("EMATE_PACKAGED_RUNTIME", "1")
    monkeypatch.setattr(
        browser_env,
        "resolve_engine",
        lambda _config: {
            "mode": "playwright-chromium",
            "channel": None,
            "path": None,
            "has_playwright": True,
            "reason": "test bundled Chromium",
        },
    )

    service = BrowserService()

    assert service._launch_mode == "persistent"
    assert service._channel is None
    assert service._headless is True


def test_packaged_cow_browser_prefers_bundled_chromium_over_host_chrome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent.tools.browser import browser_env

    bundled = tmp_path / "ms-playwright"
    (bundled / "chromium_headless_shell-1169").mkdir(parents=True)
    monkeypatch.setenv("EMATE_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
    monkeypatch.setattr(
        browser_env,
        "detect_system_chrome",
        lambda: {"channel": "chrome", "path": "/host/chrome"},
    )
    monkeypatch.setattr(browser_env, "has_playwright_package", lambda: True)

    engine = browser_env.resolve_engine()

    assert engine["mode"] == "playwright-chromium"


def test_cow_browser_success_is_stateful_and_projects_completed(
    monkeypatch,
) -> None:
    from agent.tools.browser.browser_tool import BrowserTool
    from ecorex.protocol import ItemStatus
    from ecorex.runtime.worker import AgentTurnWorker

    class Browser:
        url = ""

        def navigate(self, url, **_kwargs):
            self.url = url
            return {"url": url, "title": "Example Domain", "status": 200}

        def snapshot(self, **_kwargs):
            assert self.url == "https://example.com"
            return "Example Domain\nexample.com"

    class Kernel:
        def __init__(self):
            self.items = {}

        def create_item(self, **values):
            item_id = f"item-{len(self.items) + 1}"
            self.items[item_id] = values
            return SimpleNamespace(item_id=item_id)

        def complete_tool_item(self, item_id, activity, **_kwargs):
            self.items[item_id]["status"] = ItemStatus.COMPLETED
            self.items[item_id]["content"] = activity.model_dump(mode="json")

        def transition_item(self, *_args, **_kwargs):
            raise AssertionError("successful BrowserTool result projected as failed")

    browser = Browser()
    tool = BrowserTool()
    monkeypatch.setattr(tool, "_check_engine_ready", lambda: None)
    monkeypatch.setattr(tool, "_get_service", lambda: browser)
    worker = object.__new__(AgentTurnWorker)
    worker.kernel = Kernel()
    state = {"seq": 0, "message_item": None, "tools": {}, "errors": []}
    scope = {"job_id": "job", "lease_token": "lease", "turn_id": "turn"}

    for call_id, arguments in (
        ("navigate", {"action": "navigate", "url": "https://example.com"}),
        ("snapshot", {"action": "snapshot"}),
    ):
        result = tool.execute(arguments)
        assert result.status == "success"
        worker._project_event(
            {
                "type": "tool_execution_start",
                "data": {
                    "tool_call_id": call_id,
                    "tool_name": "browser",
                    "arguments": arguments,
                },
            },
            state=state,
            **scope,
        )
        worker._project_event(
            {
                "type": "tool_execution_end",
                "data": {
                    "tool_call_id": call_id,
                    "tool_name": "browser",
                    "status": result.status,
                    "result": result.result,
                },
            },
            state=state,
            **scope,
        )

    assert [item["status"] for item in worker.kernel.items.values()] == [
        ItemStatus.COMPLETED,
        ItemStatus.COMPLETED,
    ]
    assert all(
        item["content"]["status"] == "completed"
        for item in worker.kernel.items.values()
    )


def test_cow_desktop_update_projects_acceptance_not_install_completion() -> None:
    from ecorex.protocol import ItemStatus
    from ecorex.runtime.worker import AgentTurnWorker

    class Kernel:
        def __init__(self):
            self.items = {}

        def create_item(self, **values):
            self.items["item-update"] = values
            return SimpleNamespace(item_id="item-update")

        def complete_tool_item(self, item_id, activity, **_kwargs):
            self.items[item_id]["status"] = ItemStatus.COMPLETED
            self.items[item_id]["content"] = activity.model_dump(mode="json")

        def transition_item(self, *_args, **_kwargs):
            raise AssertionError("accepted desktop update projected as failed")

    worker = object.__new__(AgentTurnWorker)
    worker.kernel = Kernel()
    state = {"seq": 0, "message_item": None, "tools": {}, "errors": []}
    scope = {"job_id": "job", "lease_token": "lease", "turn_id": "turn"}
    worker._project_event(
        {
            "type": "tool_execution_start",
            "data": {
                "tool_call_id": "call-update-205",
                "tool_name": "desktop_update",
                "arguments": {"action": "install_latest"},
            },
        },
        state=state,
        **scope,
    )
    worker._project_event(
        {
            "type": "tool_execution_end",
            "data": {
                "tool_call_id": "call-update-205",
                "tool_name": "desktop_update",
                "status": "success",
                "result": {
                    "status": "accepted",
                    "action": "install_latest",
                    "completed": False,
                    "willRelaunch": True,
                },
            },
        },
        state=state,
        **scope,
    )

    item = worker.kernel.items["item-update"]
    assert item["status"] is ItemStatus.COMPLETED
    assert item["content"]["result_summary"] == "更新请求已受理，等待桌面更新器处理"


def test_cow_direct_tools_do_not_reenter_the_settings_permission_broker(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.read.read import Read
    from common import ecorex_tool_permissions as permissions

    source = tmp_path / "source.txt"
    source.write_text("cow", encoding="utf-8")

    def legacy_gate(*_args, **_kwargs):
        raise AssertionError("legacy settings permission broker was reached")

    monkeypatch.setattr(permissions._BROKER, "authorize_file_access", legacy_gate)
    token = permissions.bind_cow_direct_tools()
    try:
        result = Read({"cwd": str(tmp_path)}).execute({"path": "source.txt"})
    finally:
        permissions.reset_cow_direct_tools(token)

    assert result.status == "success"
    assert result.result["content"] == "cow"
    assert permissions.get_tool_permission_broker() is permissions._BROKER


def test_cow_model_request_uses_the_real_tool_manager_contract(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.protocol.models import LLMRequest
    from agent.tools.tool_manager import ToolManager
    from bridge.agent_initializer import AgentInitializer
    from ecorex.runtime.worker import _CowGatewayModel

    monkeypatch.setattr(ToolManager, "_instance", None)
    initializer = AgentInitializer(SimpleNamespace(), SimpleNamespace())
    tools = initializer._load_tools(str(tmp_path), None, [], "model-contract")
    schemas = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.get_json_schema(),
        }
        for tool in tools
    ]
    loop = asyncio.new_event_loop()
    try:
        model = _CowGatewayModel(
            SimpleNamespace(),
            loop,
            thread_id="thread_contract",
            turn_id="turn_contract",
            model_id="ecorex-chat",
        )
        request = model._request(
            LLMRequest(
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "read"}]}
                ],
                tools=schemas,
                system="cow",
            )
        )
        model.previous_response_id = "response_contract"
        continuation = model._request(
            LLMRequest(
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "read"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_contract",
                                "name": "read",
                                "input": {"path": "MEMORY.md"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_contract",
                                "content": "done",
                            },
                            {"type": "text", "text": "steer now"},
                        ],
                    }
                ],
                tools=schemas,
                system="cow",
            )
        )
    finally:
        loop.close()

    assert len(request.direct_tools) == len(schemas) > 16
    assert [entry["spec"]["tool_id"] for entry in request.direct_tools] == [
        tool.name for tool in tools
    ]
    assert [entry["spec"]["input_schema"] for entry in request.direct_tools] == [
        tool.get_json_schema() for tool in tools
    ]
    assert request.instructions == (
        "You are the intelligent work Agent 小芯 inside the e-Mate Agent product.\n\ncow"
    )
    assert "tool_search" not in request.instructions
    assert "repeat" not in request.instructions
    assert continuation.previous_response_id is None
    assert [item.type for item in continuation.input_items or []] == [
        "user_message",
        "function_call",
        "function_call_output",
        "user_message",
    ]


def test_cow_model_replays_the_complete_tool_round_without_provider_state() -> None:
    from agent.protocol.models import LLMRequest
    from ecorex.gateway import GatewayEvent, GatewayEventType
    from ecorex.runtime.worker import _CowGatewayModel

    class Gateway:
        async def stream(self, _request):
            yield GatewayEvent(
                seq=1,
                event_type=GatewayEventType.TOOL_CALL_REQUESTED,
                response_id="response_edit",
                tool_call_id="call_edit",
                tool_name="edit",
                arguments={"path": "MEMORY.md"},
                usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            )

    async def run() -> _CowGatewayModel:
        model = _CowGatewayModel(
            Gateway(),
            asyncio.get_running_loop(),
            thread_id="thread_contract",
            turn_id="turn_contract",
            model_id="ecorex-chat",
        )
        await asyncio.to_thread(
            lambda: list(
                model.call_stream(
                    LLMRequest(messages=[], tools=[], system="cow")
                )
            )
        )
        return model

    model = asyncio.run(run())

    assert model.previous_response_id == "response_edit"
    assert model.usage_events == [
        ("response_edit", {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    ]
    continuation = model._request(
        LLMRequest(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "remember this"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_edit",
                            "name": "edit",
                            "input": {"path": "MEMORY.md"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_edit",
                            "content": "Successfully replaced text in MEMORY.md",
                        }
                    ],
                }
            ],
            tools=[],
            system="cow",
        )
    )
    assert continuation.previous_response_id is None
    assert [item.model_dump(mode="json") for item in continuation.input_items or []] == [
        {
            "type": "user_message",
            "message_id": "turn_contract:cow:2:0",
            "content": "remember this",
            "images": [],
        },
        {
            "type": "function_call",
            "tool_call_id": "call_edit",
            "tool_name": "edit",
            "arguments": {"path": "MEMORY.md"},
        },
        {
            "type": "function_call_output",
            "tool_call_id": "call_edit",
            "output": "Successfully replaced text in MEMORY.md",
        }
    ]


def test_cow_terminal_fallback_is_projected_once_without_repeating_streamed_text() -> None:
    from ecorex.protocol import ItemStatus
    from ecorex.runtime.worker import AgentTurnWorker

    class Kernel:
        def __init__(self) -> None:
            self.created = []
            self.deltas = []

        def create_item(self, **values):
            self.created.append(values)
            return SimpleNamespace(item_id=f"item-{len(self.created)}")

        def append_message_delta(self, item_id, delta, **_values):
            self.deltas.append((item_id, delta))

        def transition_item(self, *_args, **_kwargs):
            return None

    worker = object.__new__(AgentTurnWorker)
    worker.kernel = Kernel()
    state = {"seq": 0, "message_item": None, "tools": {}, "errors": []}
    scope = {"job_id": "job", "lease_token": "lease", "turn_id": "turn"}

    worker._project_event(
        {"type": "agent_end", "data": {"final_response": "fallback"}},
        state=state,
        **scope,
    )
    worker._project_event(
        {"type": "agent_end", "data": {"final_response": "fallback"}},
        state=state,
        **scope,
    )

    visible = [
        item for item in worker.kernel.created if item["content"].get("text")
    ]
    assert len(visible) == 1
    assert visible[0]["status"] is ItemStatus.COMPLETED
    assert visible[0]["content"]["text"] == "fallback"

    worker.kernel.created.clear()
    state = {"seq": 0, "message_item": None, "tools": {}, "errors": []}
    for event in (
        {"type": "message_start", "data": {}},
        {"type": "message_update", "data": {"delta": "streamed"}},
        {"type": "message_end", "data": {}},
        {"type": "agent_end", "data": {"final_response": "streamed"}},
    ):
        worker._project_event(event, state=state, **scope)

    assert len(worker.kernel.created) == 1
    assert worker.kernel.deltas[-1][1] == "streamed"


def test_cow_office_tool_results_become_account_scoped_public_artifacts_once(
    tmp_path: Path,
) -> None:
    import pytest

    from ecorex.capabilities import CapabilityDeniedError
    from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind
    from ecorex.runtime import RuntimeSettings, create_app
    from ecorex.runtime.worker import AgentTurnWorker

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            workspace_root=workspace,
        )
    )
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title="Cow office files"))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="生成办公文件",
            client_message_id="cow-office-artifact-message",
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    leased = kernel.jobs.lease_next("cow-office-artifact-worker")
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, "cow-office-artifact-worker", leased.lease_token)
    worker = AgentTurnWorker(
        kernel,
        gateway=SimpleNamespace(),
        input_attachments=app.state.input_attachment_service,
    )
    state = {
        "seq": 0,
        "message_item": None,
        "tools": {},
        "errors": [],
        "workspace": workspace,
        "thread_id": thread.thread_id,
    }

    office = (
        (
            "office_documents",
            "document",
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "office_spreadsheets",
            "spreadsheet",
            ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "office_presentations",
            "presentation",
            ".pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ("office_pdf", "pdf", ".pdf", "application/pdf"),
    )
    first_result = None
    for tool_name, _family, extension, mime_type in office:
        for action in ("create", "edit"):
            path = workspace / f"{tool_name}-{action}{extension}"
            path.write_bytes(f"{tool_name}:{action}".encode())
            call_id = f"{tool_name}-{action}"
            result = {
                "status": "completed",
                "operation": action,
                "path": str(path),
                "file_name": path.name,
                "mime_type": mime_type,
            }
            first_result = first_result or result
            worker._project_event(
                {
                    "type": "tool_execution_start",
                    "data": {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "arguments": {"action": action, "path": str(path)},
                    },
                },
                state=state,
                job_id=leased.job_id,
                lease_token=leased.lease_token,
                turn_id=created.turn.turn_id,
            )
            worker._project_event(
                {
                    "type": "tool_execution_end",
                    "data": {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "status": "success",
                        "result": result,
                    },
                },
                state=state,
                job_id=leased.job_id,
                lease_token=leased.lease_token,
                turn_id=created.turn.turn_id,
            )

    assert first_result is not None
    worker._project_event(
        {"type": "artifact", "data": first_result},
        state=state,
        job_id=leased.job_id,
        lease_token=leased.lease_token,
        turn_id=created.turn.turn_id,
    )
    inspected = workspace / "office_pdf-create.pdf"
    worker._project_event(
        {
            "type": "tool_execution_start",
            "data": {
                "tool_call_id": "inspect",
                "tool_name": "office_pdf",
                "arguments": {"action": "inspect", "path": str(inspected)},
            },
        },
        state=state,
        job_id=leased.job_id,
        lease_token=leased.lease_token,
        turn_id=created.turn.turn_id,
    )
    worker._project_event(
        {
            "type": "tool_execution_end",
            "data": {
                "tool_call_id": "inspect",
                "status": "success",
                "result": {"path": str(inspected), "family": "pdf"},
            },
        },
        state=state,
        job_id=leased.job_id,
        lease_token=leased.lease_token,
        turn_id=created.turn.turn_id,
    )

    escaped = tmp_path / "forged.pdf"
    escaped.write_bytes(b"forged")
    worker._project_event(
        {
            "type": "tool_execution_start",
            "data": {
                "tool_call_id": "forged",
                "tool_name": "office_pdf",
                "arguments": {"action": "create", "path": str(escaped)},
            },
        },
        state=state,
        job_id=leased.job_id,
        lease_token=leased.lease_token,
        turn_id=created.turn.turn_id,
    )
    with pytest.raises(CapabilityDeniedError, match="escaped its workspace"):
        worker._project_event(
            {
                "type": "tool_execution_end",
                "data": {
                    "tool_call_id": "forged",
                    "status": "success",
                    "result": {
                        "status": "completed",
                        "operation": "create",
                        "path": str(escaped),
                        "mime_type": "application/pdf",
                    },
                },
            },
            state=state,
            job_id=leased.job_id,
            lease_token=leased.lease_token,
            turn_id=created.turn.turn_id,
        )

    projection = kernel.projection(thread.thread_id)
    items = [item for item in projection.items if item.kind is ItemKind.ARTIFACT]
    assert len(items) == 8
    assert [item.content["artifact"]["family"] for item in items] == [
        family for _tool, family, _extension, _mime in office for _action in range(2)
    ]
    for item in items:
        artifact = item.content["artifact"]
        assert {"preview", "open", "download", "reveal"} <= set(artifact["actions"])
        scope = app.state.artifact_service.get_artifact_scope(artifact["artifact_id"])
        assert scope.account_id == app.state.input_attachment_service.account_id
        assert scope.thread_id == thread.thread_id
        assert scope.turn_id == created.turn.turn_id


def test_cow_tool_catalog_is_not_rejected_at_the_legacy_sixty_four_tool_limit() -> None:
    from agent.protocol.models import LLMRequest
    from ecorex.gateway import MAX_TOOL_SCHEMA_BATCH_BYTES, canonical_tool_schema_batch_bytes
    from ecorex.runtime.worker import _CowGatewayModel

    loop = asyncio.new_event_loop()
    try:
        model = _CowGatewayModel(
            SimpleNamespace(),
            loop,
            thread_id="thread_catalog",
            turn_id="turn_catalog",
            model_id="ecorex-chat",
        )
        tools = [
            {
                "name": f"mcp_tool_{index}",
                "description": "MCP tool " + ("x" * 5000),
                "input_schema": {"type": "object", "properties": {}},
            }
            for index in range(65)
        ]
        request = model._request(
            LLMRequest(
                messages=[{"role": "user", "content": "use MCP"}],
                tools=tools,
                system="cow",
            )
        )
    finally:
        loop.close()

    assert len(request.direct_tools) == 65
    assert len(canonical_tool_schema_batch_bytes(request.direct_tools)) > MAX_TOOL_SCHEMA_BATCH_BYTES


def test_cow_vision_uses_the_authenticated_managed_gateway(
    tmp_path: Path,
) -> None:
    from agent.tools.vision import Vision
    from common import ecorex_tool_permissions as permissions
    from ecorex.gateway import GatewayEvent, GatewayEventType
    from ecorex.runtime.worker import _CowGatewayModel

    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield GatewayEvent(
                seq=1,
                event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
                response_id="response_vision",
                delta="a pixel",
            )
            yield GatewayEvent(
                seq=2,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id="response_vision",
                usage={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
            )

    async def run():
        gateway = Gateway()
        model = _CowGatewayModel(
            gateway,
            asyncio.get_running_loop(),
            thread_id="thread_vision",
            turn_id="turn_vision",
            model_id="ecorex-chat",
        )
        tool = Vision({"cwd": str(tmp_path)})
        tool.model = model
        token = permissions.bind_cow_direct_tools()
        try:
            result = await asyncio.to_thread(
                tool.execute,
                {"image": "pixel.png", "question": "What is shown?"},
            )
        finally:
            permissions.reset_cow_direct_tools(token)
        return result, gateway

    result, gateway = asyncio.run(run())

    assert result.status == "success"
    assert result.result["content"] == "a pixel"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].model_id == "ecorex-chat"
    assert gateway.requests[0].instructions == (
        "You are the intelligent work Agent 小芯 inside the e-Mate Agent product."
    )
    assert gateway.requests[0].input_items[0].images[0].mime_type == "image/png"


def test_real_cow_agent_stream_runs_through_the_managed_gateway(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.tools.tool_manager import ToolManager
    from bridge.agent_initializer import AgentInitializer
    from ecorex.gateway import GatewayEvent, GatewayEventType
    from ecorex.runtime.worker import _CowAgentBridge, _CowGatewayModel

    monkeypatch.setattr(ToolManager, "_instance", None)
    monkeypatch.setattr(AgentInitializer, "_migrate_config_to_env", lambda *_args: None)
    monkeypatch.setattr(AgentInitializer, "_load_env_file", lambda *_args: None)
    monkeypatch.setattr(
        AgentInitializer,
        "_setup_memory_system",
        lambda *_args: (None, []),
    )
    monkeypatch.setattr(AgentInitializer, "_initialize_scheduler", lambda *_args: None)
    monkeypatch.setattr(
        AgentInitializer,
        "_initialize_skill_manager",
        lambda *_args: None,
    )
    monkeypatch.setattr(AgentInitializer, "_start_daily_flush_timer", lambda *_args: None)

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield GatewayEvent(
                seq=1,
                event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
                response_id="response_contract",
                delta="done",
            )
            yield GatewayEvent(
                seq=2,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id="response_contract",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    async def run() -> tuple[str, Gateway, _CowGatewayModel]:
        gateway = Gateway()
        bridge = _CowAgentBridge()
        model = _CowGatewayModel(
            gateway,
            asyncio.get_running_loop(),
            thread_id="thread_contract",
            turn_id="turn_contract",
            model_id="ecorex-chat",
        )

        def invoke() -> str:
            token = bridge.bind_model(model)
            try:
                agent = AgentInitializer(SimpleNamespace(), bridge).initialize_agent(
                    session_id="contract-session",
                    workspace_root=str(tmp_path),
                )
                return agent.run_stream("reply once")
            finally:
                bridge.reset_model(token)

        return await asyncio.to_thread(invoke), gateway, model

    response, gateway, model = asyncio.run(run())
    assert response == "done"
    assert len(gateway.requests) == 1
    assert {entry["spec"]["tool_id"] for entry in gateway.requests[0].direct_tools} >= {
        "read",
        "write",
        "bash",
        "web_fetch",
    }
    assert model.usage_events == [
        (
            "response_contract",
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
    ]


def test_project_root_drives_cow_initializer_trim_and_durable_memory(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.memory import manager as memory_module
    from agent.memory import summarizer as summarizer_module
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.protocol.agent_stream import AgentStreamExecutor
    import agent.prompt as prompt_module
    import config as config_module
    from bridge.agent_initializer import AgentInitializer

    signature = inspect.signature(AgentInitializer.initialize_agent)
    assert "workspace_root" in signature.parameters

    global_workspace = tmp_path / "global-must-not-be-used"
    settings = {
        "agent_workspace": str(global_workspace),
        "agent_max_steps": 20,
        "agent_max_context_tokens": 64_000,
        "agent_max_context_turns": 30,
    }
    monkeypatch.setattr(config_module, "conf", lambda: settings)
    monkeypatch.setattr(prompt_module, "ensure_workspace", lambda root, **_kwargs: {})
    monkeypatch.setattr(prompt_module, "load_context_files", lambda _root: {})

    class PromptBuilder:
        def __init__(self, *, workspace_dir, language):
            self.workspace_dir = workspace_dir

        def build(self, **_kwargs):
            return "cow"

    monkeypatch.setattr(prompt_module, "PromptBuilder", PromptBuilder)

    created: list[dict] = []

    class AgentBridge:
        scheduler_initialized = False

        def create_agent(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(model=None, messages=[], messages_lock=threading.Lock())

    initializer = AgentInitializer(SimpleNamespace(), AgentBridge())
    initialized_roots: list[Path] = []
    monkeypatch.setattr(initializer, "_migrate_config_to_env", lambda _root: None)
    monkeypatch.setattr(initializer, "_load_env_file", lambda: None)
    monkeypatch.setattr(
        initializer,
        "_setup_memory_system",
        lambda root, _session: (initialized_roots.append(Path(root)) or None, []),
    )
    monkeypatch.setattr(initializer, "_load_tools", lambda *_args: [])
    monkeypatch.setattr(initializer, "_initialize_scheduler", lambda *_args: None)
    monkeypatch.setattr(initializer, "_initialize_skill_manager", lambda *_args: None)
    monkeypatch.setattr(initializer, "_get_runtime_info", lambda _root: {})
    monkeypatch.setattr(initializer, "_restore_conversation_history", lambda *_args: None)
    monkeypatch.setattr(initializer, "_start_daily_flush_timer", lambda: None)

    projects = (tmp_path / "project-a", tmp_path / "project-b")
    for index, project in enumerate(projects):
        initializer.initialize_agent(
            session_id=f"session-{index}", workspace_root=project
        )
    assert initialized_roots == list(projects)
    assert [Path(call["workspace_dir"]) for call in created] == list(projects)
    assert all(call["max_context_tokens"] == 64_000 for call in created)

    monkeypatch.setattr(memory_module, "_authorize_memory_index_read", lambda *_args: True)
    monkeypatch.setattr(summarizer_module, "_authorize_memory_read", lambda *_args: True)
    monkeypatch.setattr(summarizer_module, "_authorize_memory_write", lambda *_args: True)
    memory = MemoryManager(MemoryConfig(workspace_root=str(projects[0])))
    monkeypatch.setattr(
        memory.flush_manager,
        "_summarize_messages",
        lambda _messages, _max_messages: "- durable project context",
    )
    fake_agent = SimpleNamespace(
        memory_manager=memory,
        max_context_tokens=64_000,
        _current_user_id=None,
        _get_model_context_window=lambda: 128_000,
        _estimate_message_tokens=lambda _message: 1,
    )
    messages = [
        message
        for turn in range(31)
        for message in (
            {"role": "user", "content": [{"type": "text", "text": f"u{turn}"}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"a{turn}"}],
            },
        )
    ]
    executor = AgentStreamExecutor(
        agent=fake_agent,
        model=SimpleNamespace(),
        system_prompt="cow",
        tools=[],
        messages=messages,
        max_context_turns=30,
    )
    executor._trim_messages()
    thread = memory.flush_manager._last_flush_thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()

    daily = memory.flush_manager.get_today_memory_file()
    assert daily.parent == projects[0] / "memory"
    assert "durable project context" in daily.read_text(encoding="utf-8")
    assert "durable project context" in executor.messages[0]["content"][0]["text"]
    restarted = MemoryManager(MemoryConfig(workspace_root=str(projects[0])))
    other = MemoryManager(MemoryConfig(workspace_root=str(projects[1])))
    assert restarted.flush_manager.get_today_memory_file() == daily
    assert "durable project context" in daily.read_text(encoding="utf-8")
    assert not other.flush_manager.get_today_memory_file().exists()


def _attribute_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def test_turn_executor_cannot_reach_legacy_planner_or_permission_admission() -> None:
    tree = ast.parse((ROOT / "ecorex" / "runtime" / "worker.py").read_text("utf-8"))
    worker = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentTurnWorker"
    )
    methods = {
        node.name: node
        for node in worker.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable = {"run_once"}
    pending = ["run_once"]
    while pending:
        method_name = pending.pop()
        for node in ast.walk(methods[method_name]):
            if not isinstance(node, ast.Call):
                continue
            path = _attribute_path(node.func)
            if path.startswith("self."):
                callee = path.removeprefix("self.")
                if callee in methods and callee not in reachable:
                    reachable.add(callee)
                    pending.append(callee)

    forbidden: list[str] = []
    forbidden_calls = {
        "capture_execution_permit",
        "assert_execution_permit",
        "execution_admission",
        "prepare_turn",
    }
    for method_name in sorted(reachable):
        for node in ast.walk(methods[method_name]):
            path = _attribute_path(node.func) if isinstance(node, ast.Call) else ""
            if path.startswith("self.capabilities.") or path.rpartition(".")[2] in forbidden_calls:
                forbidden.append(f"{method_name}: {path}")
            if path == "self.turn_preparer":
                forbidden.append(f"{method_name}: {path}")

    assert forbidden == []
