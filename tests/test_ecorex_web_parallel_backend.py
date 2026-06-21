# encoding:utf-8
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


@contextmanager
def isolated_run_ledger():
    from agent.protocol import reset_run_ledger_for_tests

    with tempfile.TemporaryDirectory() as workspace:
        reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
        try:
            yield
        finally:
            reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")


class TestEcoreXWorkspaceState(unittest.TestCase):
    def test_installation_manifest_and_ui_state_share_workspace(self):
        from common.ecorex_workspace import (
            installation_manifest_path,
            load_installation_manifest,
            load_ui_state,
            register_installation,
            save_ui_state,
            ui_state_path,
        )

        with tempfile.TemporaryDirectory() as workspace:
            manifest = register_installation(workspace, "webui", {"port": 9899})
            self.assertEqual(manifest["workspacePath"], os.path.abspath(workspace))
            self.assertIn("webui", manifest["surfaces"])
            self.assertTrue(installation_manifest_path(workspace).is_file())

            state = save_ui_state(workspace, {
                "activeSessionId": "s1",
                "sessionUiState": {"s1": {"composerText": "draft"}},
                "unknownKey": "ignored",
            })
            self.assertEqual(state["activeSessionId"], "s1")
            self.assertEqual(state["sessionUiState"]["s1"]["composerText"], "draft")
            self.assertNotIn("unknownKey", state)
            self.assertTrue(ui_state_path(workspace).is_file())

            self.assertEqual(load_installation_manifest(workspace)["surfaces"]["webui"]["port"], 9899)
            self.assertEqual(load_ui_state(workspace)["activeSessionId"], "s1")

    def test_session_lock_blocks_same_session_until_released(self):
        from common.ecorex_workspace import SessionBusyError, SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            first = SessionLock(workspace, "session-1").acquire()
            with self.assertRaises(SessionBusyError):
                SessionLock(workspace, "session-1").acquire()
            first.release()
            second = SessionLock(workspace, "session-1").acquire()
            second.release()

    def test_session_lock_removes_dead_owner_pid(self):
        from common.ecorex_workspace import SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            lock = SessionLock(workspace, "session-dead")
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": "session-dead",
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "createdAt": 1,
                }),
                encoding="utf-8",
            )

            acquired = SessionLock(workspace, "session-dead").acquire()
            acquired.release()

    def test_cleanup_stale_session_locks_reports_dead_owner(self):
        from common.ecorex_workspace import SessionLock, cleanup_stale_session_locks

        with tempfile.TemporaryDirectory() as workspace:
            lock = SessionLock(workspace, "session-dead-snapshot")
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": "session-dead-snapshot",
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "createdAt": 1,
                }),
                encoding="utf-8",
            )

            locks = cleanup_stale_session_locks(workspace)

            self.assertEqual(len(locks), 1)
            self.assertEqual(locks[0]["session_id"], "session-dead-snapshot")
            self.assertTrue(locks[0]["dead_owner"])
            self.assertTrue(locks[0]["removed"])
            self.assertFalse(lock.path.exists())

    def test_history_display_preserves_user_attachment_extras(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-with-image", [{
                "role": "user",
                "content": [{"type": "text", "text": "分析这张图"}],
                "extras": {
                    "attachments": [{
                        "file_path": r"C:\tmp\cover.png",
                        "file_name": "cover.png",
                        "file_type": "image",
                    }]
                },
            }])

            page = store.load_history_page("session-with-image", page=1, page_size=20)

        self.assertEqual(page["messages"][0]["role"], "user")
        self.assertEqual(page["messages"][0]["extras"]["attachments"][0]["file_name"], "cover.png")

    def test_history_page_returns_context_boundary_after_clear(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-clear-context", [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ], channel_type="web")
            new_seq = store.clear_context("session-clear-context")
            page = store.load_history_page("session-clear-context", page=1, page_size=20)

        self.assertGreaterEqual(new_seq, 2)
        self.assertEqual(page["context_start_seq"], new_seq)
        self.assertEqual(page["messages"][0]["role"], "user")
        self.assertIn("_seq", page["messages"][0])
        self.assertLess(page["messages"][0]["_seq"], page["context_start_seq"])

    def test_history_page_marks_assistant_with_turn_seq(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-assistant-seq", [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ], channel_type="web")
            page = store.load_history_page("session-assistant-seq", page=1, page_size=20)

        self.assertEqual(page["messages"][0]["role"], "user")
        self.assertEqual(page["messages"][1]["role"], "assistant")
        self.assertEqual(page["messages"][1]["_seq"], page["messages"][0]["_seq"])


class TestSubagentTool(unittest.TestCase):
    class _Context:
        def __init__(self, session_id: str, workspace: str):
            self._current_session_id = session_id
            self.workspace_dir = workspace

    def _tool(self, workspace: str, session_id: str = "parent-session"):
        from agent.tools.subagent.subagent import SubagentTool

        tool = SubagentTool()
        tool.context = self._Context(session_id, workspace)
        return tool

    def test_subagent_start_enforces_concurrency_limit_without_running_children(self):
        from agent.protocol import reset_run_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            tool = self._tool(workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                for index in range(6):
                    result = tool.execute({"action": "start", "task": f"child task {index}"})
                    self.assertEqual(result.status, "success")
                    self.assertEqual(result.result["task"]["status"], "queued")

                blocked = tool.execute({"action": "start", "task": "one too many"})
                self.assertEqual(blocked.status, "error")
                self.assertEqual(blocked.result["maxConcurrency"], 6)
                self.assertEqual(blocked.result["code"], "SUBAGENT_CONCURRENCY_LIMIT")
                self.assertTrue(blocked.result["retryable"])

            listed = tool.execute({"action": "list"})
            self.assertEqual(len(listed.result["tasks"]), 6)

    def test_subagent_start_records_queued_run_ledger_row(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            tool = self._tool(workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "inspect queue state", "role": "worker"})

            self.assertEqual(result.status, "success")
            task = result.result["task"]
            run = ledger.get_run(task["childSessionId"])
            self.assertEqual(run["request_id"], task["childSessionId"])
            self.assertEqual(run["session_id"], task["childSessionId"])
            self.assertEqual(run["parent_id"], "parent-session")
            self.assertEqual(run["run_type"], "subagent")
            self.assertEqual(run["status"], "queued")
            self.assertEqual(run["phase"], "queued")
            self.assertEqual(run["metadata"]["task_id"], task["id"])
            self.assertEqual(run["metadata"]["role"], "worker")
            active = ledger.active_snapshot()
            self.assertEqual([row["request_id"] for row in active], [task["childSessionId"]])
            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                snapshot = web_channel.WebChannel().active_requests_snapshot()
            active_requests = [row for row in snapshot["requests"] if row["request_id"] == task["childSessionId"]]
            self.assertEqual(len(active_requests), 1)
            self.assertEqual(active_requests[0]["run_type"], "subagent")
            self.assertEqual(active_requests[0]["state"], "queued")
            self.assertEqual(active_requests[0]["parent_id"], "parent-session")

    def test_subagent_child_completion_writes_terminal_run_ledger(self):
        from agent.protocol import reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module

        class FakeAgentBridge:
            def agent_reply(self, *args, **kwargs):
                return "child result"

        class FakeBridge:
            def get_agent_bridge(self):
                return FakeAgentBridge()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            tool = self._tool(workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "finish child"})
            task = result.result["task"]

            with patch("bridge.bridge.Bridge", return_value=FakeBridge()):
                subagent_module._run_child(Path(workspace), task)

            listed = tool.execute({"action": "list"}).result["tasks"]
            stored = {item["id"]: item for item in listed}[task["id"]]
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["result"], "child result")
            final = ledger.get_run(task["childSessionId"])
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["phase"], "completed")
            self.assertEqual(final["terminal_reason"], "subagent_completed")
            self.assertEqual(ledger.active_snapshot(), [])

    def test_subagent_start_rejects_recursive_and_depth_overflow(self):
        with tempfile.TemporaryDirectory() as workspace:
            recursive = self._tool(workspace, "subagent-existing")
            recursive_result = recursive.execute({"action": "start", "task": "nested"})
            self.assertEqual(recursive_result.status, "error")
            self.assertIn("recursive", recursive_result.result["message"])

            depth_result = self._tool(workspace).execute({"action": "start", "task": "nested", "depth": 1})
            self.assertEqual(depth_result.status, "error")
            self.assertEqual(depth_result.result["maxDepth"], 1)

    def test_cancel_children_for_parent_cascades_running_and_queued_tasks(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent.subagent import cancel_children_for_parent

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            tool = self._tool(workspace, "parent-session")
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                running = tool.execute({"action": "start", "task": "running child"})
                queued = tool.execute({"action": "start", "task": "queued child"})

            registry = get_cancel_registry()
            running_child_session = running.result["task"]["childSessionId"]
            registry.register(running_child_session, session_id=running_child_session)
            try:
                summary = cancel_children_for_parent(workspace, "parent-session")
                self.assertEqual(summary["cancelledTasks"], 2)
                self.assertEqual(summary["cancelledRequests"], 1)

                active = [row for row in registry.snapshot() if row["request_id"] == running_child_session]
                self.assertEqual(len(active), 1)
                self.assertTrue(active[0]["cancelled"])

                listed = tool.execute({"action": "list"}).result["tasks"]
                statuses = {task["id"]: task["status"] for task in listed}
                self.assertEqual(statuses[running.result["task"]["id"]], "cancelling")
                self.assertEqual(statuses[queued.result["task"]["id"]], "cancelled")
                running_run = ledger.get_run(running_child_session)
                self.assertEqual(running_run["status"], "cancelling")
                self.assertEqual(running_run["phase"], "cancelling")
                queued_run = ledger.get_run(queued.result["task"]["childSessionId"])
                self.assertEqual(queued_run["status"], "cancelled")
                self.assertEqual(queued_run["terminal_reason"], "parent_cancelled_before_start")
            finally:
                registry.unregister(running_child_session)


class TestAgentCapabilityPermissions(unittest.TestCase):
    def test_agent_capability_install_pack_uses_optional_ability_permission(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        proxy_name, proxy_args = AgentStreamExecutor._permission_proxy_for_tool(
            None,
            "agent_capability",
            {"action": "install_pack", "pack_id": "office-pdf"},
        )

        self.assertEqual(proxy_name, "optional_abilities")
        self.assertEqual(proxy_args["action"], "install")
        self.assertEqual(proxy_args["ability"], "office-pdf")

    def test_agent_capability_feishu_permission_mentions_cli_second_step(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        proxy_name, proxy_args = AgentStreamExecutor._permission_proxy_for_tool(
            None,
            "agent_capability",
            {"action": "install_pack", "pack_id": "feishu-lark"},
        )

        self.assertEqual(proxy_name, "agent_capability")
        self.assertEqual(proxy_args["action"], "install_pack")
        self.assertEqual(proxy_args["pack_id"], "feishu-lark")
        self.assertTrue(proxy_args["discoveryOnly"])

    def test_agent_capability_feishu_pack_is_discovery_only(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        calls = []

        def fake_install(self, pack_id, timeout):
            calls.append((pack_id, timeout))
            raise AssertionError("feishu-lark must not enter capability-pack installer")

        with patch.object(OptionalAbilities, "_install_capability_pack", fake_install):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "feishu-lark",
                "timeout": 45,
            })

        self.assertEqual(result.status, "error")
        self.assertEqual(calls, [])
        self.assertTrue(result.result["discoveryOnly"])
        self.assertIn("find-skill", result.result["message"])

    def test_agent_capability_install_pack_accepts_feishu_cli_alias(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        calls = []

        def fake_feishu_cli(self, timeout):
            calls.append(("feishu-cli", timeout))
            raise AssertionError("feishu-cli alias must stay discovery-only")

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "lark-cli",
                "timeout": 30,
            })

        self.assertEqual(result.status, "error")
        self.assertEqual(calls, [])
        self.assertTrue(result.result["discoveryOnly"])

    def test_optional_abilities_update_preserves_live_provider_config(self):
        import config as config_module
        from agent.tools.optional_abilities import optional_abilities

        previous = config_module.config
        try:
            config_module.config = config_module.Config({
                "model": "gpt-5.5",
                "bot_type": "openai",
                "open_ai_api_key": "sk-live",
                "open_ai_api_base": "http://127.0.0.1:8080/v1",
                "text_to_image": "gpt-image-2-pro",
            })

            optional_abilities._update_live_config({
                "model": "deepseek-v4-flash",
                "bot_type": "",
                "open_ai_api_key": "",
                "open_ai_api_base": "https://api.openai.com/v1",
                "text_to_image": "gpt-image-2",
                "tools": {"feishu_cli": {"auto_install": True}},
            })

            live = config_module.conf()
            self.assertEqual(live.get("model"), "gpt-5.5")
            self.assertEqual(live.get("bot_type"), "openai")
            self.assertEqual(live.get("open_ai_api_key"), "sk-live")
            self.assertEqual(live.get("open_ai_api_base"), "http://127.0.0.1:8080/v1")
            self.assertEqual(live.get("text_to_image"), "gpt-image-2-pro")
            self.assertEqual(live.get("tools"), {"feishu_cli": {"auto_install": True}})
        finally:
            config_module.config = previous

    def test_optional_ability_install_uses_isolated_target_dir_and_timeout(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        original_sys_path = list(sys.path)
        original_pythonpath = os.environ.get("PYTHONPATH")
        try:
            with tempfile.TemporaryDirectory() as workspace:
                root = Path(workspace)
                (root / "scripts").mkdir(parents=True)
                (root / "scripts" / "install-capability.py").write_text("# test installer\n", encoding="utf-8")
                (root / "capabilities.json").write_text(
                    json.dumps({"packs": [{"id": "office-pdf", "moduleChecks": ["fitz"]}]}),
                    encoding="utf-8",
                )

                captured = {}

                def fake_run(command, **kwargs):
                    captured["command"] = command
                    captured["kwargs"] = kwargs
                    target_dir = root / "capability-packages" / "office-pdf"
                    target_dir.mkdir(parents=True)
                    state_dir = root / "capability-state"
                    state_dir.mkdir(parents=True, exist_ok=True)
                    (state_dir / "office-pdf.json").write_text(
                        json.dumps({
                            "packId": "office-pdf",
                            "state": "installed",
                            "installed": True,
                            "targetDir": str(target_dir),
                        }),
                        encoding="utf-8",
                    )
                    return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

                with patch.object(optional_abilities, "RUNTIME_ROOT", root), \
                        patch.object(optional_abilities.subprocess, "run", fake_run):
                    result = OptionalAbilities()._install_capability_pack("office-pdf", 123)

                self.assertEqual(result.status, "success")
                self.assertIn("--target-dir", captured["command"])
                target_arg = captured["command"][captured["command"].index("--target-dir") + 1]
                self.assertEqual(Path(target_arg), root / "capability-packages" / "office-pdf")
                self.assertIn("--timeout", captured["command"])
                self.assertEqual(captured["command"][captured["command"].index("--timeout") + 1], "123")
                self.assertEqual(captured["kwargs"]["timeout"], 123)
                self.assertIn(str(root / "capability-packages" / "office-pdf"), sys.path)
                self.assertEqual(result.result["capabilityState"]["targetDir"], str(root / "capability-packages" / "office-pdf"))
        finally:
            sys.path[:] = original_sys_path
            if original_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = original_pythonpath

    def test_agent_capability_safe_diagnostics_do_not_require_prompt(self):
        from common.ecorex_tool_permissions import get_tool_permission_broker

        decision = get_tool_permission_broker().authorize(
            "agent_capability",
            "tool-call-1",
            {"action": "diagnose"},
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "read-only-agent-capability-status")


class TestReleaseRuntimeSanitizer(unittest.TestCase):
    def test_sanitizer_removes_runtime_state_and_python_launchers(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            launcher = root / "python" / "Scripts" / "pip.exe"
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"MZ launcher #!C:\\CowAgent\\desktop\\runtime\\ecorex-runtime\\python\\python.exe\nPK")
            state = root / "capability-state" / "feishu-lark.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"logPath":"C:\\\\CowAgent\\\\runtime.log"}', encoding="utf-8")
            readme = root / "README.txt"
            readme.write_text(
                "The original v0.1.0 project name was CowAgent and has been renamed to EcoreX.\n"
                "CowAgent is a historical project name / development stack name only. It does not indicate plagiarism, copying, or third-party ownership.\n"
                "原始 v0.1.0 版本项目名 CowAgent 已改名为 EcoreX；CowAgent 是历史项目名称/开发栈名称，不代表抄袭或第三方归属。\n",
                encoding="utf-8",
            )

            script = Path(__file__).resolve().parents[1] / "scripts" / "sanitize-ecorex-release-runtime.py"
            subprocess.run([sys.executable, str(script), str(root)], check=True, capture_output=True, text=True)
            subprocess.run([sys.executable, str(script), str(root), "--check"], check=True, capture_output=True, text=True)

            self.assertFalse(launcher.exists())
            self.assertFalse(state.exists())
            self.assertTrue(readme.exists())


class TestWebParallelHandlers(unittest.TestCase):
    def test_ui_state_handler_put_and_get(self):
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            handler = web_channel.UiStateHandler()
            payload = {"state": {"theme": "dark", "activeSessionId": "abc"}}
            with patch.object(web_channel, "_require_auth", return_value=None):
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode("utf-8")):
                        put_result = json.loads(handler.PUT())
                    get_result = json.loads(handler.GET())

            self.assertEqual(put_result["status"], "success")
            self.assertEqual(get_result["state"]["theme"], "dark")
            self.assertEqual(get_result["state"]["activeSessionId"], "abc")

    def test_installations_handler_registers_surface(self):
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            handler = web_channel.InstallationsHandler()
            payload = {"surface": "desktop", "metadata": {"version": "test"}}
            with patch.object(web_channel, "_require_auth", return_value=None):
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode("utf-8")):
                        post_result = json.loads(handler.POST())
                    get_result = json.loads(handler.GET())

            self.assertEqual(post_result["status"], "success")
            self.assertEqual(get_result["manifest"]["surfaces"]["desktop"]["version"], "test")

    def test_agent_install_request_for_feishu_guides_agent_without_manual_consent(self):
        from channel.web import web_channel

        handler = web_channel.AgentInstallRequestHandler()
        payload = {"packId": "feishu-lark", "packName": "飞书 / Lark 连接器", "sessionId": "s1"}
        with patch.object(web_channel, "_require_auth", return_value=None):
            with patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode("utf-8")):
                result = json.loads(handler.POST())

        self.assertEqual(result["status"], "success")
        prompt = result["prompt"]
        self.assertIn("agent_capability", prompt)
        self.assertIn("feishu_cli", prompt)
        self.assertIn("@larksuite/cli", prompt)
        self.assertIn("不要要求用户输入", prompt)
        self.assertIn("不要反复诊断", prompt)

    def test_default_app_shell_is_independent_of_desktop_dist(self):
        from channel.web.web_channel import _default_web_app_html

        html = _default_web_app_html()
        self.assertIn("EcoreX Web App", html)
        self.assertIn("window.ecorexDesktop", html)
        self.assertNotIn("desktop/dist", html)

    def test_version_handler_returns_user_facing_release_notes(self):
        from cli import __version__
        from channel.web import web_channel

        payload = json.loads(web_channel.VersionHandler().GET())

        self.assertEqual(payload["version"], __version__)
        notes = payload["releaseNotes"]
        self.assertEqual(notes["version"], __version__)
        self.assertIn("highlights", notes)
        self.assertIn("fixes", notes)
        self.assertIn("howTo", notes)
        self.assertIn("windows", notes["updatePolicy"])
        self.assertIn("macos", notes["updatePolicy"])
        self.assertIn("webui", notes["updatePolicy"])

    def test_diagnostic_bundle_redacts_local_paths_and_log_lines(self):
        import config
        from channel.web import web_channel

        raw_workspace = r"C:\Users\private-user\EcoreX\workspace"
        raw_runtime = r"C:\Users\private-user\.ecorex"
        raw_log = r"C:\Users\private-user\EcoreX\logs\run.log"
        raw_lock = r"C:\Users\private-user\EcoreX\.locks\session-private.json"
        raw_line = f"2026-06-20 ERROR secret prompt text from {raw_workspace}"

        class FakeChannel:
            def active_requests_snapshot(self):
                return {
                    "requests": [],
                    "stale_locks": [{
                        "session_id": "session-private",
                        "pid": 1234,
                        "dead_owner": True,
                        "stale": True,
                        "removed": False,
                        "path": raw_lock,
                    }],
                }

        with patch.object(web_channel, "_log_snapshot_payload", return_value={
            "log": {"path": raw_log, "exists": True, "lines": [raw_line]},
        }):
            with patch.object(web_channel, "_get_workspace_root", return_value=raw_workspace):
                with patch.object(config, "get_root", return_value=Path(raw_runtime)):
                    with patch.object(web_channel, "WebChannel", return_value=FakeChannel()):
                        payload = web_channel._diagnostic_bundle_payload("session-current", "request-current")

        rendered = json.dumps(payload, ensure_ascii=False)
        for secret in (raw_workspace, raw_runtime, raw_log, raw_lock, raw_line, "secret prompt text", "session-private"):
            self.assertNotIn(secret, rendered)
        self.assertTrue(payload["runtime"]["workspaceRoot"]["redacted"])
        self.assertTrue(payload["runtime"]["runtimeRoot"]["redacted"])
        self.assertTrue(payload["logs"]["path"]["redacted"])
        self.assertTrue(payload["staleLocks"][0]["redacted"])
        self.assertTrue(payload["logs"]["recentEvents"][0]["redacted"])

    def test_public_web_bind_requires_password(self):
        from channel.web import web_channel

        public_no_password = {"web_host": "0.0.0.0", "web_password": ""}
        with patch.object(web_channel, "conf", return_value=public_no_password):
            host = web_channel._effective_web_host()
            self.assertEqual(host, "0.0.0.0")
            self.assertTrue(web_channel._is_public_bind_host(host))
            with self.assertRaises(RuntimeError):
                web_channel._validate_web_bind_auth(host)
            self.assertFalse(web_channel._check_auth())

        local_no_password = {"web_host": "", "web_password": ""}
        with patch.object(web_channel, "conf", return_value=local_no_password):
            host = web_channel._effective_web_host()
            self.assertEqual(host, "127.0.0.1")
            self.assertFalse(web_channel._is_public_bind_host(host))
            web_channel._validate_web_bind_auth(host)
            self.assertTrue(web_channel._check_auth())

        public_with_password = {"web_host": "0.0.0.0", "web_password": "secret"}
        with patch.object(web_channel, "conf", return_value=public_with_password):
            host = web_channel._effective_web_host()
            web_channel._validate_web_bind_auth(host)
            self.assertFalse(web_channel._check_auth())

    def test_tool_permission_handler_round_trips_mode_and_audit(self):
        from channel.web import web_channel

        old_env = {
            "ECOREX_DESKTOP": os.environ.get("ECOREX_DESKTOP"),
            "ECOREX_USER_DATA": os.environ.get("ECOREX_USER_DATA"),
        }
        with tempfile.TemporaryDirectory() as user_data:
            try:
                os.environ["ECOREX_DESKTOP"] = "1"
                os.environ["ECOREX_USER_DATA"] = user_data
                handler = web_channel.ToolPermissionHandler()

                with patch.object(web_channel, "_require_auth", return_value=None):
                    with patch.object(
                        web_channel.web,
                        "data",
                        return_value=json.dumps({"action": "set_mode", "mode": "read-only"}).encode("utf-8"),
                    ):
                        read_only = json.loads(handler.POST())
                    listed = json.loads(handler.GET())
                    with patch.object(
                        web_channel.web,
                        "data",
                        return_value=json.dumps({"action": "set_mode", "mode": "full-access"}).encode("utf-8"),
                    ):
                        full_access = json.loads(handler.POST())
                    final_state = json.loads(handler.GET())

                self.assertEqual(read_only["status"], "success")
                self.assertEqual(read_only["mode"], "read-only")
                self.assertEqual(listed["mode"], "read-only")
                self.assertEqual(full_access["mode"], "full-access")
                self.assertEqual(final_state["mode"], "full-access")
                self.assertEqual(final_state["auditPath"], os.path.join(user_data, "permission-audit.jsonl"))

                audit_path = os.path.join(user_data, "permission-audit.jsonl")
                self.assertTrue(os.path.exists(audit_path))
                with open(audit_path, "r", encoding="utf-8") as handle:
                    audit = handle.read()
                self.assertIn("permission.mode.update", audit)
                self.assertIn("read-only", audit)
                self.assertIn("full-access", audit)
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_permission_broker_uses_config_appdata_when_no_env_override(self):
        from common.ecorex_tool_permissions import ToolPermissionBroker

        old_env = {
            "ECOREX_DESKTOP_USER_DATA": os.environ.get("ECOREX_DESKTOP_USER_DATA"),
            "ECOREX_USER_DATA": os.environ.get("ECOREX_USER_DATA"),
        }
        with tempfile.TemporaryDirectory() as appdata_dir:
            try:
                os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                os.environ.pop("ECOREX_USER_DATA", None)
                with patch("config.conf", return_value={"appdata_dir": appdata_dir}), patch(
                    "config.get_appdata_dir", return_value=appdata_dir
                ):
                    state = ToolPermissionBroker().set_mode("read-only")

                expected = os.path.join(appdata_dir, "permissions", "permission-audit.jsonl")
                self.assertEqual(state["auditPath"], expected)
                self.assertTrue(os.path.exists(expected))
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_active_request_lookup_uses_web_request_id(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-web-cancel"
        session_id = "session-test-web-cancel"
        channel.request_to_session = {request_id: session_id}
        registry = get_cancel_registry()
        registry.register(request_id, session_id=session_id)
        try:
            self.assertEqual(channel._active_request_ids_for_session(session_id), [request_id])
            self.assertTrue(registry.cancel_request(request_id))
        finally:
            registry.unregister(request_id)

    def test_active_request_snapshot_reports_backend_runtime_state(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-active-snapshot"
        session_id = "session-test-active-snapshot"
        channel.request_to_session = {request_id: session_id}
        channel.sse_queues = {request_id: Queue()}
        with isolated_run_ledger():
            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                with tempfile.TemporaryDirectory() as workspace:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        snapshot = channel.active_requests_snapshot()
                self.assertEqual(snapshot["status"], "success")
                active = [item for item in snapshot["requests"] if item["request_id"] == request_id]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["session_id"], session_id)
                self.assertFalse(active[0]["cancelled"])
                self.assertEqual(active[0]["state"], "running")
                self.assertTrue(active[0]["stream_available"])
                self.assertEqual(snapshot["sessions"][session_id], [request_id])
            finally:
                registry.unregister(request_id)

    def test_active_request_snapshot_marks_registry_only_subagent_fallback_row(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "subagent-registry-only"
        session_id = "subagent-registry-only"
        registry = get_cancel_registry()
        with isolated_run_ledger():
            registry.register(request_id, session_id=session_id)
            try:
                with tempfile.TemporaryDirectory() as workspace:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        snapshot = channel.active_requests_snapshot()
                active = [item for item in snapshot["requests"] if item["request_id"] == request_id]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["source"], "cancel_registry")
                self.assertEqual(active[0]["run_type"], "subagent")
                self.assertEqual(active[0]["session_id"], session_id)
            finally:
                registry.unregister(request_id)

    def test_active_request_snapshot_cleans_dead_session_locks(self):
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        channel = web_channel.WebChannel()
        with isolated_run_ledger():
            with tempfile.TemporaryDirectory() as workspace:
                lock = SessionLock(workspace, "session-dead-active-snapshot")
                lock.path.parent.mkdir(parents=True, exist_ok=True)
                lock.path.write_text(
                    json.dumps({
                        "sessionId": "session-dead-active-snapshot",
                        "pid": 999999999,
                        "host": socket.gethostname(),
                        "createdAt": 1,
                    }),
                    encoding="utf-8",
                )

                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()

                self.assertEqual(snapshot["status"], "success")
                self.assertEqual(snapshot["requests"], [])
                stale = snapshot["staleLocks"]
                self.assertEqual(len(stale), 1)
                self.assertEqual(stale[0]["session_id"], "session-dead-active-snapshot")
                self.assertTrue(stale[0]["removed"])
                self.assertFalse(lock.path.exists())

    def test_run_ledger_records_active_and_terminal_state_once(self):
        from agent.protocol import reset_run_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run("req-ledger", "session-ledger", phase="accepted")
            ledger.mark_phase("req-ledger", "tool_running")

            active = ledger.active_snapshot()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["request_id"], "req-ledger")
            self.assertEqual(active[0]["phase"], "tool_running")
            self.assertEqual(active[0]["state"], "running")

            ledger.mark_terminal(
                "req-ledger",
                "failed",
                reason="worker_exception",
                error_code="WORKER_EXCEPTION",
                error_message="boom",
            )
            ledger.mark_terminal("req-ledger", "completed", reason="late_done")

            final = ledger.get_run("req-ledger")
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "worker_exception")
            self.assertEqual(final["error_code"], "WORKER_EXCEPTION")
            self.assertEqual(ledger.active_snapshot(), [])

    def test_active_request_snapshot_prefers_durable_run_ledger(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-ledger-active"
        session_id = "session-ledger-active"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="accepted")
            ledger.mark_phase(request_id, "waiting_permission")
            channel.request_to_session = {}
            channel.sse_queues = {request_id: Queue()}
            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
                active = [item for item in snapshot["requests"] if item["request_id"] == request_id]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["session_id"], session_id)
                self.assertEqual(active[0]["phase"], "waiting_permission")
                self.assertEqual(active[0]["state"], "running")
                self.assertTrue(active[0]["stream_available"])
            finally:
                registry.unregister(request_id)

    def test_cancel_registry_snapshot_marks_cancelled_request(self):
        from agent.protocol.cancel import CancelTokenRegistry

        registry = CancelTokenRegistry()
        with patch("agent.protocol.cancel.time.time", side_effect=[1000.0, 1600.0, 1602.5]):
            registry.register("req-cancel-snapshot", session_id="session-cancel-snapshot")
            self.assertTrue(registry.cancel_request("req-cancel-snapshot"))
            snapshot = registry.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["request_id"], "req-cancel-snapshot")
        self.assertEqual(snapshot[0]["session_id"], "session-cancel-snapshot")
        self.assertTrue(snapshot[0]["cancelled"])
        self.assertEqual(snapshot[0]["state"], "cancelling")
        self.assertEqual(snapshot[0]["age_seconds"], 602.5)
        self.assertEqual(snapshot[0]["cancelled_at"], 1600.0)
        self.assertEqual(snapshot[0]["cancel_age_seconds"], 2.5)

    def test_web_request_token_survives_agentbridge_until_web_finalizer(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType

        class FakeAgent:
            def __init__(self):
                self.tools = []
                self.model = types.SimpleNamespace()
                self.messages_lock = threading.Lock()
                self.messages = [{"role": "assistant", "content": "ok"}]
                self._last_run_new_messages = []
                self.cancel_event = None

            def run_stream(self, user_message, on_event, clear_history=False, cancel_event=None):
                self.cancel_event = cancel_event
                return "ok"

        request_id = "req-web-token-owner"
        session_id = "session-web-token-owner"
        registry = get_cancel_registry()
        original_event = registry.register(request_id, session_id=session_id)
        fake_agent = FakeAgent()
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: fake_agent
        bridge._pre_persist_user_message = lambda *args, **kwargs: False
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "web_channel"
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.TEXT)
            self.assertIs(fake_agent.cancel_event, original_event)
            self.assertIs(registry.get_event(request_id), original_event)
        finally:
            registry.unregister(request_id)

    def test_web_request_token_survives_agentbridge_error_path(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType

        class FakeAgent:
            def __init__(self):
                self.tools = []
                self.model = types.SimpleNamespace()
                self.messages_lock = threading.Lock()
                self.messages = [{"role": "user", "content": "hello"}]
                self._last_run_new_messages = []

            def run_stream(self, user_message, on_event, clear_history=False, cancel_event=None):
                raise RuntimeError("model stream failed")

        request_id = "req-web-token-owner-error"
        session_id = "session-web-token-owner-error"
        registry = get_cancel_registry()
        original_event = registry.register(request_id, session_id=session_id)
        fake_agent = FakeAgent()
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: fake_agent
        bridge._pre_persist_user_message = lambda *args, **kwargs: False
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "web_channel"
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.ERROR)
            self.assertIs(registry.get_event(request_id), original_event)
        finally:
            registry.unregister(request_id)

    def test_non_web_request_token_is_cleaned_by_agentbridge(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType

        class FakeAgent:
            def __init__(self):
                self.tools = []
                self.model = types.SimpleNamespace()
                self.messages_lock = threading.Lock()
                self.messages = [{"role": "assistant", "content": "ok"}]
                self._last_run_new_messages = []

            def run_stream(self, user_message, on_event, clear_history=False, cancel_event=None):
                return "ok"

        request_id = "req-agentbridge-owned-token"
        session_id = "session-agentbridge-owned-token"
        registry = get_cancel_registry()
        fake_agent = FakeAgent()
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: fake_agent
        bridge._pre_persist_user_message = lambda *args, **kwargs: False
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        try:
            bridge.agent_reply("hello", context=context)

            self.assertIsNone(registry.get_event(request_id))
        finally:
            registry.unregister(request_id)

    def test_active_snapshot_keeps_cancelling_request_after_sse_terminal(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-cancel-terminal-still-active"
        session_id = "session-cancel-terminal-still-active"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="running")
            channel.request_to_session = {request_id: session_id}
            channel._ensure_sse_state(request_id)
            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                self.assertTrue(registry.cancel_request(request_id))
                self.assertTrue(channel._push_cancelled_event_once(request_id, {
                    "type": "cancelled",
                    "content": "stopping",
                    "request_id": request_id,
                }))

                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()

                active = [item for item in snapshot["requests"] if item["request_id"] == request_id]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["source"], "cancel_registry")
                self.assertTrue(active[0]["cancelled"])
                self.assertEqual(active[0]["state"], "cancelling")
                self.assertTrue(active[0]["stream_available"])
            finally:
                registry.unregister(request_id)

    def test_active_snapshot_merges_registry_cancel_age_into_ledger_row(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-ledger-cancel-age"
        session_id = "session-ledger-cancel-age"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel.request_to_session = {request_id: session_id}
            registry = get_cancel_registry()
            times = [1000.0, 1001.0, 1002.0, 1600.0]

            def fake_time():
                return times.pop(0) if times else 1602.5

            with patch("agent.protocol.cancel.time.time", side_effect=fake_time):
                ledger.create_run(request_id, session_id, phase="running")
                ledger.mark_phase(request_id, "cancelling", status="cancelling")
                registry.register(request_id, session_id=session_id)
                self.assertTrue(registry.cancel_request(request_id))
                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        snapshot = channel.active_requests_snapshot()
                finally:
                    registry.unregister(request_id)

            active = [item for item in snapshot["requests"] if item["request_id"] == request_id]
            self.assertEqual(len(active), 1)
            self.assertTrue(active[0]["cancelled"])
            self.assertEqual(active[0]["state"], "cancelling")
            self.assertEqual(active[0]["cancelled_at"], 1600.0)
            self.assertEqual(active[0]["cancel_age_seconds"], 2.5)
            self.assertGreater(active[0]["age_seconds"], active[0]["cancel_age_seconds"])

    def test_busy_session_message_interrupts_old_request_and_starts_new_one(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context, ContextType
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            session_id = "session-busy-interrupt"
            old_request_id = "req-old-busy"
            new_request_id = "req-new-busy"
            old_queue = Queue()
            registry = get_cancel_registry()

            with tempfile.TemporaryDirectory() as workspace:
                old_lock = SessionLock(workspace, session_id).acquire()
                old_event = registry.register(old_request_id, session_id=session_id)
                channel.request_to_session = {old_request_id: session_id}
                channel.sse_queues = {old_request_id: old_queue}
                channel.sse_stream_tokens = {}
                channel.session_queues = {}
                produced_contexts = []

                def release_old_after_cancel():
                    if old_event.wait(timeout=3):
                        old_lock.release()

                releaser = threading.Thread(target=release_old_after_cancel, daemon=True)
                releaser.start()

                def fake_compose_context(ctype, content, **kwargs):
                    context = Context(ctype, content)
                    context.kwargs = kwargs
                    return context

                def fake_produce(context):
                    produced_contexts.append(context)
                    lock = context.get("session_lock")
                    if lock:
                        lock.release()

                payload = {
                    "session_id": session_id,
                    "message": "continue with new instructions",
                    "stream": True,
                    "lang": "zh",
                }
                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 1):
                            with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 1):
                                with patch.object(channel, "_generate_request_id", return_value=new_request_id):
                                    with patch.object(channel, "_compose_context", side_effect=fake_compose_context):
                                        with patch.object(channel, "produce", side_effect=fake_produce):
                                            with patch.object(
                                                web_channel.web,
                                                "data",
                                                return_value=json.dumps(payload).encode("utf-8"),
                                            ):
                                                result = json.loads(channel.post_message())

                    cancelled = old_queue.get(timeout=2)
                    self.assertEqual(result["status"], "success")
                    self.assertNotEqual(result.get("code"), "session_busy")
                    self.assertEqual(result["request_id"], new_request_id)
                    self.assertEqual(cancelled["type"], "cancelled")
                    self.assertEqual(cancelled["request_id"], old_request_id)
                    self.assertEqual(channel.request_to_session[new_request_id], session_id)
                    self.assertIn(new_request_id, channel.sse_queues)
                    self.assertTrue(produced_contexts)
                    self.assertEqual(produced_contexts[0].get("cancel_token_owner"), "web_channel")
                finally:
                    old_lock.release()
                    registry.unregister(old_request_id)
                    registry.unregister(new_request_id)

    def test_busy_session_message_returns_typed_retry_contract_when_lock_stays_busy(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel
        from common.ecorex_workspace import SessionBusyError, SessionLock

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            session_id = "session-busy-retry-contract"
            old_request_id = "req-busy-retry-old"
            registry = get_cancel_registry()

            with tempfile.TemporaryDirectory() as workspace:
                old_lock = SessionLock(workspace, session_id).acquire()
                registry.register(old_request_id, session_id=session_id)
                channel.request_to_session = {old_request_id: session_id}
                payload = {
                    "session_id": session_id,
                    "message": "new turn while old lock remains busy",
                    "stream": True,
                    "lang": "en",
                }
                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        with patch.object(
                            channel,
                            "_interrupt_and_wait_for_session_lock",
                            side_effect=SessionBusyError("still stopping"),
                        ):
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["code"], "REQUEST_CONFLICT_RETRYABLE")
                    self.assertNotEqual(result["code"], "session_busy")
                    self.assertEqual(result["error_type"], "concurrency_conflict")
                    self.assertEqual(result["state"], "retryable_conflict")
                    self.assertTrue(result["retryable"])
                    self.assertTrue(result["recoverable"])
                    self.assertGreaterEqual(result["retry_after_ms"], 1000)
                    self.assertEqual(result["active_request_ids"], [old_request_id])
                    self.assertIn("retry", result["message"].lower())
                finally:
                    old_lock.release()
                    registry.unregister(old_request_id)

    def test_post_message_global_backpressure_rejects_before_request_allocation(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            channel.request_to_session = {}
            registry = get_cancel_registry()
            registry.register("req-backpressure-existing", session_id="session-existing")
            with tempfile.TemporaryDirectory() as workspace:
                payload = {
                    "session_id": "session-new-backpressure",
                    "message": "should be rejected by global pressure",
                    "stream": True,
                }
                lock_path = SessionLock(workspace, payload["session_id"]).path
                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 1):
                            with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 99):
                                with patch.object(channel, "_generate_request_id") as generate_request_id:
                                    with patch.object(
                                        web_channel.web,
                                        "data",
                                        return_value=json.dumps(payload).encode("utf-8"),
                                    ):
                                        result = json.loads(channel.post_message())

                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["code"], "BACKPRESSURE_GLOBAL_LIMIT")
                    self.assertEqual(result["error_type"], "backpressure_limit")
                    self.assertEqual(result["scope"], "global")
                    self.assertTrue(result["retryable"])
                    self.assertTrue(result["recoverable"])
                    self.assertEqual(result["limit"], 1)
                    self.assertEqual(result["global_active"], 1)
                    self.assertEqual(result["sse_replay_limit"], channel.SSE_MAX_REPLAY_EVENTS)
                    generate_request_id.assert_not_called()
                    self.assertEqual(channel.request_to_session, {})
                    self.assertFalse(lock_path.exists())
                finally:
                    registry.unregister("req-backpressure-existing")

    def test_post_message_backpressure_sees_prior_admitted_request(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context
        from channel.web import web_channel

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            channel.request_to_session = {}
            registry = get_cancel_registry()
            produced = []

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            try:
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 1):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 99):
                        with patch.object(channel, "_compose_context", side_effect=fake_compose_context):
                            with patch.object(channel, "produce", side_effect=lambda context: produced.append(context)):
                                with patch.object(channel, "_generate_request_id", return_value="req-first-admitted"):
                                    with patch.object(
                                        web_channel.web,
                                        "data",
                                        return_value=json.dumps({
                                            "session_id": "session-first-admitted",
                                            "message": "first admitted",
                                            "stream": True,
                                        }).encode("utf-8"),
                                    ):
                                        first = json.loads(channel.post_message())
                                with patch.object(channel, "_generate_request_id") as second_request_id:
                                    with patch.object(
                                        web_channel.web,
                                        "data",
                                        return_value=json.dumps({
                                            "session_id": "session-second-rejected",
                                            "message": "second rejected",
                                            "stream": True,
                                        }).encode("utf-8"),
                                    ):
                                        second = json.loads(channel.post_message())

                self.assertEqual(first["status"], "success")
                self.assertEqual(first["request_id"], "req-first-admitted")
                self.assertEqual(second["status"], "error")
                self.assertEqual(second["code"], "BACKPRESSURE_GLOBAL_LIMIT")
                self.assertEqual(second["global_active"], 1)
                second_request_id.assert_not_called()
                self.assertTrue(produced)
            finally:
                registry.unregister("req-first-admitted")

    def test_post_message_session_backpressure_returns_typed_active_ids(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            channel.request_to_session = {}
            session_id = "session-pressure-existing"
            request_id = "req-session-pressure-existing"
            ledger.create_run(request_id, session_id, phase="running", status="running")
            payload = {
                "session_id": session_id,
                "message": "same session should be rejected by session pressure",
                "stream": True,
            }
            try:
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 99):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 1):
                        with patch.object(channel, "_generate_request_id") as generate_request_id:
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "BACKPRESSURE_SESSION_LIMIT")
                self.assertEqual(result["error_type"], "backpressure_limit")
                self.assertEqual(result["scope"], "session")
                self.assertTrue(result["retryable"])
                self.assertEqual(result["limit"], 1)
                self.assertEqual(result["session_active"], 1)
                self.assertEqual(result["active_request_ids"], [request_id])
                generate_request_id.assert_not_called()
                self.assertEqual(channel.request_to_session, {})
            finally:
                reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")

    def test_post_message_backpressure_counts_ledger_only_active_runs(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            try:
                channel = web_channel.WebChannel()
                channel.request_to_session = {}
                ledger.create_run(
                    "req-ledger-only-pressure",
                    "session-ledger-only-pressure",
                    phase="running",
                    status="running",
                )
                payload = {
                    "session_id": "session-new-ledger-pressure",
                    "message": "should count ledger-only active row",
                    "stream": True,
                }
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 1):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 99):
                        with patch.object(channel, "_generate_request_id") as generate_request_id:
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "BACKPRESSURE_GLOBAL_LIMIT")
                self.assertEqual(result["global_active"], 1)
                generate_request_id.assert_not_called()
            finally:
                reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")

    def test_post_message_backpressure_uses_configured_limits_over_class_defaults(self):
        from agent.protocol import get_cancel_registry, get_run_ledger
        from channel.web import web_channel

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            registry = get_cancel_registry()
            try:
                channel.request_to_session = {}
                registry.register("req-config-global-pressure", session_id="session-config-other")
                global_payload = {
                    "session_id": "session-config-new",
                    "message": "global config limit",
                    "stream": True,
                }
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 99):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 99):
                        with patch.object(
                            web_channel,
                            "conf",
                            return_value={
                                "web_max_active_requests": 1,
                                "web_max_active_requests_per_session": 99,
                            },
                        ):
                            with patch.object(web_channel.web, "data", return_value=json.dumps(global_payload).encode("utf-8")):
                                global_result = json.loads(channel.post_message())

                self.assertEqual(global_result["code"], "BACKPRESSURE_GLOBAL_LIMIT")
                self.assertEqual(global_result["scope"], "global")
                self.assertEqual(global_result["limit"], 1)
                self.assertEqual(global_result["global_active_limit"], 1)

                registry.unregister("req-config-global-pressure")
                session_id = "session-config-session-pressure"
                request_id = "req-config-session-pressure"
                get_run_ledger().create_run(request_id, session_id, phase="running", status="running")
                session_payload = {
                    "session_id": session_id,
                    "message": "session config limit",
                    "stream": True,
                }
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 99):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 99):
                        with patch.object(
                            web_channel,
                            "conf",
                            return_value={
                                "web_max_active_requests": 99,
                                "web_max_active_requests_per_session": 1,
                            },
                        ):
                            with patch.object(web_channel.web, "data", return_value=json.dumps(session_payload).encode("utf-8")):
                                session_result = json.loads(channel.post_message())

                self.assertEqual(session_result["code"], "BACKPRESSURE_SESSION_LIMIT")
                self.assertEqual(session_result["scope"], "session")
                self.assertEqual(session_result["limit"], 1)
                self.assertEqual(session_result["session_active_limit"], 1)
                self.assertEqual(session_result["active_request_ids"], [request_id])
            finally:
                registry.unregister("req-config-global-pressure")

    def test_cancel_bypasses_backpressure_admission_limit(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            registry = get_cancel_registry()
            session_id = "session-cancel-bypass-pressure"
            request_id = "req-cancel-bypass-pressure"
            channel.request_to_session = {request_id: session_id}
            registry.register(request_id, session_id=session_id)
            payload = {
                "session_id": session_id,
                "message": "/cancel",
                "stream": True,
                "lang": "en",
            }
            try:
                with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 1):
                    with patch.object(channel, "BACKPRESSURE_SESSION_ACTIVE_LIMIT", 1):
                        with patch.object(
                            web_channel.web,
                            "data",
                            return_value=json.dumps(payload).encode("utf-8"),
                ):
                            result = json.loads(channel.post_message())

                self.assertEqual(result["status"], "success")
                self.assertIn("Cancelled", result["inline_reply"])
                self.assertTrue(registry.get_event(request_id).is_set())
            finally:
                registry.unregister(request_id)

    def test_post_message_compose_exception_cleans_pre_worker_request(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-compose-pre-worker-abort"
            session_id = "session-compose-pre-worker-abort"
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            payload = {
                "session_id": session_id,
                "message": "compose should fail",
                "stream": True,
                "lang": "zh",
            }

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch.object(channel, "_generate_request_id", return_value=request_id):
                    with patch.object(channel, "_compose_context", side_effect=RuntimeError("compose boom")):
                        with patch.object(
                            web_channel.web,
                            "data",
                            return_value=json.dumps(payload).encode("utf-8"),
                        ):
                            result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "error")
            self.assertIn("compose boom", result["message"])
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertNotIn(request_id, channel.request_to_session)
            self.assertFalse(channel._sse_request_exists(request_id))
            self.assertFalse(SessionLock(workspace, session_id).path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "post_message_exception")
            self.assertEqual(final["error_code"], "POST_MESSAGE_EXCEPTION")

    def test_post_message_filtered_context_cleans_pre_worker_request(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-filtered-pre-worker-abort"
            session_id = "session-filtered-pre-worker-abort"
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            payload = {
                "session_id": session_id,
                "message": "filtered",
                "stream": True,
                "lang": "zh",
            }

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch.object(channel, "_generate_request_id", return_value=request_id):
                    with patch.object(channel, "_compose_context", return_value=None):
                        with patch.object(
                            web_channel.web,
                            "data",
                            return_value=json.dumps(payload).encode("utf-8"),
                        ):
                            result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["message"], "Message was filtered")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertNotIn(request_id, channel.request_to_session)
            self.assertFalse(channel._sse_request_exists(request_id))
            self.assertFalse(SessionLock(workspace, session_id).path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "context_filtered")
            self.assertEqual(final["error_code"], "CONTEXT_FILTERED")

    def test_post_message_thread_start_failure_cleans_pre_worker_request(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from bridge.context import Context, ContextType
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-thread-pre-worker-abort"
            session_id = "session-thread-pre-worker-abort"
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            payload = {
                "session_id": session_id,
                "message": "thread should fail",
                "stream": True,
                "lang": "zh",
            }

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch.object(channel, "_generate_request_id", return_value=request_id):
                    with patch.object(channel, "_compose_context", side_effect=fake_compose_context):
                        with patch.object(web_channel.threading.Thread, "start", side_effect=RuntimeError("thread boom")):
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "error")
            self.assertIn("thread boom", result["message"])
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertNotIn(request_id, channel.request_to_session)
            self.assertFalse(channel._sse_request_exists(request_id))
            self.assertFalse(SessionLock(workspace, session_id).path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "post_message_exception")
            self.assertEqual(final["error_code"], "POST_MESSAGE_EXCEPTION")

    def test_empty_agent_end_emits_done_so_sse_does_not_hang(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-empty-agent-end"
        session_id = "session-empty-agent-end"
        channel.request_to_session = {request_id: session_id}
        channel.sse_queues = {request_id: Queue()}
        callback = channel._make_sse_callback(request_id)

        with patch.object(channel, "_fetch_latest_pair_seqs", return_value={"user_seq": None, "bot_seq": None}):
            with patch.object(channel, "_fetch_agent_usage", return_value=None):
                callback({"type": "agent_end", "data": {"final_response": ""}})

        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "done")
        self.assertEqual(event["request_id"], request_id)
        self.assertTrue(str(event["content"]).strip())

    def test_agent_stream_error_emits_error_not_done(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-agent-stream-error"
        session_id = "session-agent-stream-error"
        channel.request_to_session = {request_id: session_id}
        channel.sse_queues = {request_id: Queue()}
        callback = channel._make_sse_callback(request_id)

        with patch.object(channel, "_fetch_agent_usage", return_value=None):
            callback({"type": "error", "data": {"error": "upstream timeout", "error_code": "MODEL_TIMEOUT"}})

        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["event_type"], "run.failed")
        self.assertEqual(event["state"], "failed")
        self.assertTrue(event["terminal"])
        self.assertEqual(event["error_code"], "MODEL_TIMEOUT")
        self.assertIn("upstream timeout", event["content"])

    def test_worker_completion_unregisters_cancel_token_but_keeps_sse_queue(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context, ContextType
        from channel.web import web_channel

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            request_id = "req-worker-complete"
            session_id = "session-worker-complete"
            context = Context(ContextType.TEXT, "hello")
            context["request_id"] = request_id
            context["session_id"] = session_id
            channel.request_to_session = {request_id: session_id}
            channel.sse_queues = {request_id: Queue()}

            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                self.assertIsNotNone(registry.get_event(request_id))
                channel._finalize_request_after_worker(context, worker_exception=None)

                self.assertIsNone(registry.get_event(request_id))
                self.assertEqual(channel.request_to_session[request_id], session_id)
                self.assertIn(request_id, channel.sse_queues)
            finally:
                registry.unregister(request_id)

    def test_worker_exception_emits_error_and_unregisters_cancel_token(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context, ContextType
        from channel.web import web_channel

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            request_id = "req-worker-error"
            session_id = "session-worker-error"
            context = Context(ContextType.TEXT, "hello")
            context["request_id"] = request_id
            context["session_id"] = session_id
            channel.request_to_session = {request_id: session_id}
            channel.sse_queues = {request_id: Queue()}

            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                with patch.object(channel, "_fetch_agent_usage", return_value=None):
                    channel._finalize_request_after_worker(context, worker_exception=RuntimeError("boom"))

                event = channel.sse_queues[request_id].get(timeout=1)
                self.assertEqual(event["type"], "error")
                self.assertEqual(event["event_type"], "run.failed")
                self.assertTrue(event["terminal"])
                self.assertEqual(event["state"], "failed")
                self.assertEqual(event["error_code"], "WORKER_EXCEPTION")
                self.assertEqual(event["request_id"], request_id)
                self.assertIn("boom", event["content"])
                self.assertIsNone(registry.get_event(request_id))
            finally:
                registry.unregister(request_id)

    def test_produce_exception_emits_error_and_unregisters_cancel_token(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context, ContextType
        from channel.web import web_channel

        class FakeLock:
            def __init__(self):
                self.released = False

            def release(self):
                self.released = True

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            request_id = "req-produce-error"
            session_id = "session-produce-error"
            context = Context(ContextType.TEXT, "hello")
            context["request_id"] = request_id
            context["session_id"] = session_id
            lock = FakeLock()
            channel.request_to_session = {request_id: session_id}
            channel.sse_queues = {request_id: Queue()}

            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                with patch.object(channel, "_fetch_agent_usage", return_value=None):
                    with patch.object(channel, "produce", side_effect=RuntimeError("produce boom")):
                        channel._produce_with_session_lock(context, lock)

                event = channel.sse_queues[request_id].get(timeout=1)
                self.assertEqual(event["type"], "error")
                self.assertEqual(event["event_type"], "run.failed")
                self.assertTrue(event["terminal"])
                self.assertEqual(event["state"], "failed")
                self.assertEqual(event["error_code"], "WORKER_EXCEPTION")
                self.assertEqual(event["request_id"], request_id)
                self.assertIn("produce boom", event["content"])
                self.assertIsNone(registry.get_event(request_id))
                self.assertTrue(lock.released)
            finally:
                registry.unregister(request_id)

    def test_multiple_sse_connections_receive_same_request_events(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-sse"
        channel.request_to_session = {request_id: "session-test-sse"}
        channel._ensure_sse_state(request_id)

        first_stream = channel.stream_response(request_id)
        self.assertTrue(next(first_stream).startswith(b": keepalive"))

        channel._push_sse_event(request_id, {
            "type": "done",
            "content": "ok",
            "request_id": request_id,
        })
        second_stream = channel.stream_response(request_id)

        first_chunk = next(first_stream)
        second_chunk = next(second_stream)
        self.assertIn(b'id: 0', first_chunk)
        self.assertIn(b'id: 0', second_chunk)
        self.assertIn(b'"type": "done"', first_chunk)
        self.assertIn(b'"event_type": "run.completed"', first_chunk)
        self.assertIn(b'"terminal": true', first_chunk)
        self.assertIn(b'"type": "done"', second_chunk)
        self.assertIn(b'"content": "ok"', first_chunk)
        self.assertIn(b'"content": "ok"', second_chunk)

        first_stream.close()
        second_stream.close()

    def test_sse_query_last_event_id_resumes_after_cursor(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-sse-cursor"
        channel.request_to_session = {request_id: "session-test-sse-cursor"}
        channel._ensure_sse_state(request_id)
        channel._push_sse_event(request_id, {
            "type": "phase",
            "content": "first",
            "request_id": request_id,
        })
        channel._push_sse_event(request_id, {
            "type": "done",
            "content": "second",
            "request_id": request_id,
        })

        with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(last_event_id="0")):
            resumed = channel.stream_response(request_id)
            chunk = next(resumed)

        self.assertIn(b"id: 1", chunk)
        self.assertNotIn(b'"content": "first"', chunk)
        self.assertIn(b'"content": "second"', chunk)
        resumed.close()

    def test_sse_replay_gap_is_explicit_when_cursor_is_too_old(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-sse-gap"
        channel.request_to_session = {request_id: "session-test-sse-gap"}
        channel._ensure_sse_state(request_id)
        channel.sse_events[request_id] = [{
            "type": "phase",
            "content": "retained",
            "request_id": request_id,
            "protocol_version": channel.SSE_PROTOCOL_VERSION,
            "event_type": "legacy.phase",
        }]
        channel.sse_event_offsets[request_id] = 10

        with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(last_event_id="2")):
            resumed = channel.stream_response(request_id)
            gap_chunk = next(resumed)
            retained_chunk = next(resumed)

        self.assertIn(b"id: 9", gap_chunk)
        self.assertIn(b'"type": "replay_gap"', gap_chunk)
        self.assertIn(b'"event_type": "stream.replay_gap"', gap_chunk)
        self.assertIn(b'"recoverable": true', gap_chunk)
        self.assertIn(b"id: 10", retained_chunk)
        self.assertIn(b'"content": "retained"', retained_chunk)
        resumed.close()

    def test_done_event_is_emitted_once_per_request(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-done-once"
        channel.request_to_session = {request_id: "session-test-done-once"}
        channel._ensure_sse_state(request_id)

        first = channel._push_done_event_once(request_id, {
            "type": "done",
            "content": "first",
            "request_id": request_id,
        })
        second = channel._push_done_event_once(request_id, {
            "type": "done",
            "content": "second",
            "request_id": request_id,
        })

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(channel.sse_events[request_id]), 1)
        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["content"], "first")
        self.assertEqual(event["event_type"], "run.completed")
        self.assertTrue(event["terminal"])

    def test_sse_cancelled_event_is_terminal_once(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-cancelled-once"
        channel.request_to_session = {request_id: "session-test-cancelled-once"}
        channel._ensure_sse_state(request_id)

        cancelled = channel._push_cancelled_event_once(request_id, {
            "type": "cancelled",
            "content": "stopped",
            "request_id": request_id,
        })
        done = channel._push_done_event_once(request_id, {
            "type": "done",
            "content": "should not replace cancellation",
            "request_id": request_id,
        })

        self.assertTrue(cancelled)
        self.assertFalse(done)
        self.assertEqual(len(channel.sse_events[request_id]), 1)
        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "cancelled")
        self.assertEqual(event["event_type"], "run.cancelled")
        self.assertEqual(event["state"], "cancelled")
        self.assertTrue(event["terminal"])

    def test_local_image_reply_emits_done_with_artifact(self):
        from bridge.context import Context, ContextType
        from bridge.reply import Reply, ReplyType
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            image_path = Path(workspace) / "generated.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            request_id = "req-local-image-reply"
            session_id = "session-local-image-reply"
            channel = web_channel.WebChannel()
            channel.request_to_session = {request_id: session_id}
            channel._ensure_sse_state(request_id)
            context = Context(ContextType.TEXT, "draw")
            context["request_id"] = request_id
            context["session_id"] = session_id
            context["on_event"] = lambda _event: None

            with patch.object(channel, "_artifact_path_available", return_value=True):
                with patch.object(channel, "_fetch_latest_pair_seqs", return_value={"user_seq": None, "bot_seq": None}):
                    with patch.object(channel, "_fetch_agent_usage", return_value=None):
                        channel.send(Reply(ReplyType.IMAGE_URL, f"file://{image_path}"), context)

            event = channel.sse_queues[request_id].get(timeout=1)
            self.assertEqual(event["type"], "done")
            self.assertEqual(event["request_id"], request_id)
            self.assertIn("artifacts", event)
            self.assertEqual(event["artifacts"][0]["kind"], "image")
            self.assertEqual(event["artifacts"][0]["path"], str(image_path))


class TestAgentHostBoundary(unittest.TestCase):
    def test_prompt_exposes_host_boundary_tools_and_convergence_rules(self):
        from agent.prompt.builder import build_agent_system_prompt

        tools = [
            types.SimpleNamespace(name="read"),
            types.SimpleNamespace(name="bash"),
            types.SimpleNamespace(name="browser"),
            types.SimpleNamespace(name="host_diagnostics"),
            types.SimpleNamespace(name="feishu_cli"),
        ]

        prompt = build_agent_system_prompt(
            workspace_dir="C:/EcoreX",
            language="zh",
            tools=tools,
            skill_manager=None,
            memory_manager=None,
            runtime_info=None,
        )

        self.assertIn("Host capability boundary:", prompt)
        self.assertIn("Use `host_diagnostics` when a task appears stuck", prompt)
        self.assertIn("call `feishu_cli` first", prompt)
        self.assertIn("prefer the configured CDP/chrome-devtools path first", prompt)
        self.assertIn("stop repeating it", prompt)

    def test_feishu_tool_chain_budget_blocks_repeated_probing(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )
        args = {"action": "run", "args": ["base", "+record-list", "--page-token", "x"]}

        for _ in range(6):
            executor._record_tool_result("feishu_cli", args, True)

        should_stop, reason = executor._check_tool_chain_budget("feishu_cli", args)
        self.assertTrue(should_stop)
        self.assertIn("Feishu/Lark tool chain", reason)

    def test_tool_chain_budget_forces_next_turn_text_only(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )
        args = {"action": "run", "args": ["base", "+record-list", "--page-token", "final"]}
        for _ in range(6):
            prior_args = {
                "action": "run",
                "args": ["base", "+record-list", "--page-token", str(_)],
            }
            executor._record_tool_result("feishu_cli", prior_args, True)

        result = executor._execute_tool({
            "id": "tool-call-1",
            "name": "feishu_cli",
            "arguments": args,
        })

        self.assertEqual(result["status"], "error")
        self.assertTrue(executor._force_text_response_next_turn)

    def test_permission_denial_forces_next_turn_text_only(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[
                types.SimpleNamespace(
                    name="bash",
                    description="run shell",
                    params={"type": "object", "properties": {}},
                )
            ],
        )

        with patch.object(
            executor,
            "_authorize_tool_execution",
            return_value={"allowed": False, "reason": "Current read-only mode blocks local tool execution."},
        ):
            result = executor._execute_tool({
                "id": "tool-call-permission",
                "name": "bash",
                "arguments": {"command": "whoami"},
            })

        self.assertEqual(result["status"], "error")
        self.assertIn("Permission blocked", result["result"])
        self.assertTrue(executor._force_text_response_next_turn)

    def test_forced_text_turn_sends_no_tool_schema_once(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {
                    "choices": [
                        {
                            "delta": {"content": "blocked summary"},
                            "finish_reason": "stop",
                        }
                    ]
                }

        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[
                types.SimpleNamespace(
                    name="bash",
                    description="run shell",
                    params={"type": "object", "properties": {}},
                )
            ],
        )
        executor._force_text_response_once("test")

        content, tool_calls = executor._call_llm_stream(retry_on_empty=False)

        self.assertEqual(content, "blocked summary")
        self.assertEqual(tool_calls, [])
        self.assertIsNone(model.requests[0].tools)
        self.assertFalse(executor._force_text_response_next_turn)

    def test_raw_lark_cli_bash_is_grouped_with_feishu_chain(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )

        key = executor._tool_chain_key(
            "bash",
            {"command": "lark-cli base +record-list --as user"},
        )
        self.assertEqual(key, "feishu_cli:bash")

    def test_chrome_devtools_mcp_calls_share_browser_chain_budget(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )
        tool_name = "mcp__chrome-devtools__click"
        args = {"selector": "#login"}

        self.assertEqual(executor._tool_chain_key(tool_name, args), "browser:cdp")
        for _ in range(8):
            executor._record_tool_result(tool_name, {"selector": f"#item-{_}"}, True)

        should_stop, reason = executor._check_tool_chain_budget(tool_name, args)
        self.assertTrue(should_stop)
        self.assertIn("Browser/CDP tool chain", reason)

    def test_simple_raw_lark_cli_bash_autoroutes_to_feishu_tool(self):
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.tools.base_tool import ToolResult

        class FakeFeishuTool:
            name = "feishu_cli"

            def __init__(self):
                self.calls = []

            def execute_tool(self, params):
                self.calls.append(params)
                return ToolResult.success({"ok": True, "params": params})

        fake_feishu = FakeFeishuTool()
        events = []
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[fake_feishu],
            on_event=lambda event: events.append(event),
        )

        with patch.object(executor, "_authorize_tool_execution", return_value={"allowed": True}):
            result = executor._execute_tool({
                "id": "tool-call-lark-cli",
                "name": "bash",
                "arguments": {
                    "command": "lark-cli docx +read --as user",
                    "timeout": 12,
                },
            })

        self.assertEqual(result["status"], "success")
        self.assertEqual(fake_feishu.calls, [{
            "action": "run",
            "args": ["docx", "+read", "--as", "user"],
            "timeout": 12,
        }])
        self.assertEqual(result["result"]["reroutedFrom"], "bash:raw bash lark-cli")
        starts = [event for event in events if event["type"] == "tool_execution_start"]
        self.assertEqual(starts[0]["data"]["tool_name"], "feishu_cli")

    def test_raw_lark_cli_bash_autoroute_covers_npx_package_and_node_runner(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[types.SimpleNamespace(name="feishu_cli")],
        )

        cases = [
            (
                "npx -y @larksuite/cli@1.0.56 base +record-list --as user",
                ["base", "+record-list", "--as", "user"],
            ),
            (
                r'node "C:\cli-main\scripts\run.js" docx +read --as user',
                ["docx", "+read", "--as", "user"],
            ),
        ]
        for command, expected_args in cases:
            with self.subTest(command=command):
                routed_name, routed_args, routed_reason = executor._external_capability_autoroute(
                    "bash",
                    {"command": command, "timeout": 9},
                )
                self.assertEqual(routed_name, "feishu_cli")
                self.assertEqual(routed_args["action"], "run")
                self.assertEqual(routed_args["args"], expected_args)
                self.assertEqual(routed_args["timeout"], 9)
                self.assertEqual(routed_reason, "raw bash lark-cli")
                self.assertEqual(executor._tool_chain_key("bash", {"command": command}), "feishu_cli:bash")

    def test_complex_raw_lark_cli_bash_keeps_guidance_not_autoroute(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[types.SimpleNamespace(name="feishu_cli")],
        )

        routed_name, routed_args, routed_reason = executor._external_capability_autoroute(
            "bash",
            {"command": "lark-cli docx +read --as user && echo done"},
        )
        self.assertEqual(routed_name, "")
        self.assertEqual(routed_args, {})
        self.assertEqual(routed_reason, "")

        reason = executor._external_capability_reroute(
            "bash",
            {"command": "lark-cli docx +read --as user && echo done"},
        )
        self.assertIn("Do not call Feishu/Lark CLI through raw bash", reason)
        self.assertIn("feishu_cli", reason)

        package_reason = executor._external_capability_reroute(
            "bash",
            {"command": "npx @larksuite/cli base +record-list && echo done"},
        )
        self.assertIn("Do not call Feishu/Lark CLI through raw bash", package_reason)
        self.assertIn("feishu_cli", package_reason)

    def test_chrome_devtools_mcp_startup_is_allowed_noninteractive(self):
        from common.ecorex_tool_permissions import ToolPermissionBroker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as user_data:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            try:
                decision = ToolPermissionBroker().authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": [
                            "chrome-devtools-mcp@latest",
                            "--browserUrl",
                            "http://127.0.0.1:9222",
                            "--no-usage-statistics",
                        ],
                        "trusted_default_chrome_devtools": True,
                    },
                )
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "default-cdp-mcp-startup")

    def test_chrome_devtools_mcp_startup_rejects_spoof_and_read_only(self):
        from common.ecorex_tool_permissions import ToolPermissionBroker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as user_data:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            try:
                broker = ToolPermissionBroker()
                spoof = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "powershell",
                        "args": ["-NoProfile"],
                    },
                )
                self.assertFalse(spoof["allowed"])

                broker.set_mode("read-only")
                read_only = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": [
                            "chrome-devtools-mcp@latest",
                            "--browserUrl",
                            "http://127.0.0.1:9222",
                            "--no-usage-statistics",
                        ],
                        "trusted_default_chrome_devtools": True,
                    },
                )
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertFalse(read_only["allowed"])
        self.assertIn("read-only", read_only["reason"])

    def test_mcp_tool_error_result_is_not_reported_as_success(self):
        from agent.tools.mcp.mcp_client import McpClient
        from agent.tools.mcp.mcp_tool import McpTool

        client = McpClient({"name": "test-mcp", "type": "stdio", "command": "noop"})
        client._initialized = True

        def fake_send_request(method, params, cancel_event=None):
            return {
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "remote tool failed"}],
                }
            }

        client._send_request = fake_send_request
        tool = McpTool(
            client,
            {"name": "remote_tool", "description": "", "inputSchema": {"type": "object"}},
            "test-mcp",
        )

        result = tool.execute({})

        self.assertEqual(result.status, "error")
        self.assertIn("remote tool failed", result.result)

    def test_mcp_tool_names_are_namespaced_and_remote_name_is_preserved(self):
        from agent.tools.mcp.mcp_tool import McpTool
        from agent.tools.tool_manager import _mcp_public_tool_name

        calls = []

        class FakeClient:
            def call_tool(self, name, params, cancel_event=None):
                calls.append((name, params, cancel_event))
                return {"ok": True}

        public_name = _mcp_public_tool_name("evil server", "bash")
        self.assertTrue(public_name.startswith("mcp__evil_server__bash"))
        self.assertNotEqual(public_name, "bash")

        tool = McpTool(
            FakeClient(),
            {"name": "bash", "description": "", "inputSchema": {"type": "object"}},
            "evil server",
            public_name=public_name,
        )

        result = tool.execute({"command": "whoami"})

        self.assertEqual(tool.name, public_name)
        self.assertEqual(tool.remote_name, "bash")
        self.assertEqual(result.status, "success")
        self.assertEqual(calls[0][0], "bash")

    def test_sync_mcp_into_agent_does_not_replace_builtin_tool(self):
        from agent.tools.base_tool import BaseTool, ToolResult
        from agent.tools.mcp.mcp_tool import McpTool
        from agent.tools.tool_manager import ToolManager

        class BuiltinBash(BaseTool):
            name = "bash"
            description = "first-party bash"
            params = {"type": "object"}

            def execute(self, args):
                return ToolResult.success("builtin")

        class FakeClient:
            def call_tool(self, name, params, cancel_event=None):
                return {"ok": True}

        manager = ToolManager()
        old_registry = dict(manager._mcp_tool_instances)
        try:
            mcp_tool = McpTool(
                FakeClient(),
                {"name": "bash", "description": "", "inputSchema": {"type": "object"}},
                "evil",
                public_name="mcp__evil__bash",
            )
            manager._mcp_tool_instances = {"mcp__evil__bash": mcp_tool}
            agent = types.SimpleNamespace(tools={"bash": BuiltinBash()})

            added, removed = manager.sync_mcp_into_agent(agent)

            self.assertEqual(agent.tools["bash"].description, "first-party bash")
            self.assertIs(agent.tools["mcp__evil__bash"], mcp_tool)
            self.assertEqual(added, ["mcp__evil__bash"])
            self.assertEqual(removed, [])
        finally:
            manager._mcp_tool_instances = old_registry

    def test_feishu_cli_apply_config_refreshes_cached_runtime_fields(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        with tempfile.TemporaryDirectory() as workspace:
            tool = FeishuCli({
                "cwd": workspace,
                "package": "@initial/lark-cli@0.0.1",
                "auto_install": True,
            })

            next_workspace = os.path.join(workspace, "next")
            tool.apply_config({
                "cwd": next_workspace,
                "package": "@custom/lark-cli@9.9.9",
                "auto_install": False,
            })
            missing = tool._missing_payload({})

        self.assertEqual(tool.cwd, next_workspace)
        self.assertEqual(tool.package, "@custom/lark-cli@9.9.9")
        self.assertFalse(tool.auto_install)
        self.assertIn("find-skill", missing["installHint"])
        self.assertIn("@custom/lark-cli@9.9.9", missing["installHint"])
        self.assertIn("registry.npmmirror.com", missing["installHint"])

    def test_feishu_cli_ensure_respects_auto_install_false(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        tool = FeishuCli({
            "package": "@custom/lark-cli@9.9.9",
            "auto_install": False,
        })
        with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=None), \
                patch("agent.tools.feishu_cli.feishu_cli._run_process") as run_process:
            result = tool.execute({"action": "ensure"})

        self.assertEqual(result.status, "success")
        self.assertFalse(result.result["available"])
        self.assertIn("find-skill", result.result["installHint"])
        self.assertIn("@custom/lark-cli@9.9.9", result.result["installHint"])
        self.assertIn("registry.npmmirror.com", result.result["installHint"])
        run_process.assert_not_called()

    def test_feishu_cli_install_requires_find_skill_gate(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        tool = FeishuCli({"package": "@larksuite/cli@1.0.56"})
        with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=None), \
                patch("agent.tools.feishu_cli.feishu_cli._which", return_value="npm"), \
                patch.object(FeishuCli, "_safe_run") as safe_run:
            result = tool.execute({"action": "install", "timeout": 1})

        self.assertEqual(result.status, "error")
        self.assertTrue(result.result["discoveryOnly"])
        self.assertIn("find-skill", result.result["message"])
        safe_run.assert_not_called()

    def test_feishu_cli_install_allows_find_skill_gate(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        tool = FeishuCli({"package": "@larksuite/cli@1.0.56"})
        with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", side_effect=[None, ["lark"]]), \
                patch("agent.tools.feishu_cli.feishu_cli._which", return_value="npm"), \
                patch.object(FeishuCli, "_safe_run", return_value={"status": "success", "exitCode": 0, "output": "ok"}) as safe_run:
            result = tool.execute({
                "action": "install",
                "timeout": 1,
                "discovery_source": "find-skill",
            })

        self.assertEqual(result.status, "success")
        self.assertTrue(result.result["installedNow"])
        self.assertEqual(result.result["registry"], "https://registry.npmjs.org")
        safe_run.assert_called_once()

    def test_tool_manager_create_tool_applies_feishu_cli_config(self):
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        old_configs = dict(getattr(manager, "tool_configs", {}) or {})
        try:
            manager.load_tools()
            manager.tool_configs["feishu_cli"] = {
                "package": "@custom/lark-cli@1.2.3",
                "auto_install": False,
                "cwd": "C:/EcoreX/TestWorkspace",
            }

            tool = manager.create_tool("feishu_cli")

            self.assertEqual(tool.package, "@custom/lark-cli@1.2.3")
            self.assertFalse(tool.auto_install)
            self.assertEqual(tool.cwd, "C:/EcoreX/TestWorkspace")
        finally:
            manager.tool_configs = old_configs

    def test_tool_manager_loads_find_and_ecorex_cli_tools(self):
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        manager.load_tools()

        self.assertIsNotNone(manager.create_tool("find"))
        self.assertIsNotNone(manager.create_tool("ecorex_cli"))

    def test_ecorex_cli_version_action_uses_bundled_cli(self):
        from agent.tools.ecorex_cli.ecorex_cli import EcoreXCli

        result = EcoreXCli().execute({"action": "version"})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["exit_code"], 0)
        self.assertIn("EcoreX", result.result["stdout"])

    def test_agent_initializer_load_tools_applies_cached_tool_config(self):
        from bridge.agent_initializer import AgentInitializer

        with tempfile.TemporaryDirectory() as workspace:
            fake_config = {
                "tools": {
                    "feishu_cli": {
                        "package": "@custom/lark-cli@2.0.0",
                        "auto_install": False,
                        "timeout": 99,
                    },
                    "host_diagnostics": {
                        "cwd": "C:/stale-diagnostics-cwd",
                    },
                },
                "knowledge": True,
                "self_evolution_enabled": False,
            }
            initializer = AgentInitializer(None, types.SimpleNamespace())
            with patch("agent.tools.tool_manager.conf", return_value=fake_config):
                tools = initializer._load_tools(workspace, None, [], session_id="test-session")

            by_name = {tool.name: tool for tool in tools}
            feishu = by_name["feishu_cli"]
            diagnostics = by_name["host_diagnostics"]

            self.assertEqual(feishu.cwd, workspace)
            self.assertEqual(feishu.package, "@custom/lark-cli@2.0.0")
            self.assertFalse(feishu.auto_install)
            self.assertEqual(feishu.config["timeout"], 99)
            self.assertEqual(diagnostics.cwd, workspace)

    def test_host_diagnostics_apply_config_refreshes_cached_cwd(self):
        from agent.tools.host_diagnostics.host_diagnostics import HostDiagnostics

        tool = HostDiagnostics({"cwd": "C:/old"})
        tool.apply_config({"cwd": "C:/new"})

        self.assertEqual(tool.cwd, "C:/new")

    def test_mcp_list_tools_rpc_error_raises(self):
        from agent.tools.mcp.mcp_client import McpClient

        client = McpClient({"name": "test-mcp", "type": "stdio", "command": "noop"})
        client._initialized = True
        client._send_request = lambda method, params: {
            "error": {"code": -32000, "message": "list failed"}
        }

        with self.assertRaises(RuntimeError):
            client.list_tools()

    def test_mcp_tool_cancel_event_returns_error(self):
        from agent.tools.mcp.mcp_client import McpClient
        from agent.tools.mcp.mcp_tool import McpTool

        cancel_event = threading.Event()
        cancel_event.set()
        client = McpClient({"name": "test-mcp", "type": "stdio", "command": "noop"})
        client._initialized = True
        tool = McpTool(
            client,
            {"name": "remote_tool", "description": "", "inputSchema": {"type": "object"}},
            "test-mcp",
        )
        tool.cancel_event = cancel_event

        result = tool.execute({})

        self.assertEqual(result.status, "error")
        self.assertIn("cancelled", result.result.lower())

    def test_mcp_streamable_sse_has_total_deadline(self):
        from agent.tools.mcp.mcp_client import McpClient

        class SlowKeepalive:
            def __iter__(self):
                for _ in range(3):
                    time.sleep(0.45)
                    yield b": keepalive\n"

        client = McpClient({"name": "test-mcp", "type": "streamable-http", "url": "http://example.invalid", "timeout": 1})
        with self.assertRaises(TimeoutError):
            client._read_sse_response(SlowKeepalive(), expected_id=1, timeout_seconds=1)

    def test_browser_submit_cancel_event_returns_quickly(self):
        from agent.tools.browser.browser_service import BrowserService

        service = BrowserService({})
        service._start_thread = lambda: None
        service._alive = True
        service.cancel_event = threading.Event()
        service.cancel_event.set()

        start = time.monotonic()
        with self.assertRaises(RuntimeError):
            service._submit(lambda: "never")
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2)

    def test_skill_service_rejects_path_traversal(self):
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService

        with tempfile.TemporaryDirectory() as workspace:
            old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            builtin_dir = os.path.join(workspace, "builtin")
            custom_dir = os.path.join(workspace, "custom")
            os.makedirs(builtin_dir, exist_ok=True)
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                get_tool_permission_broker().set_mode("full-access")
                manager = SkillManager(builtin_dir=builtin_dir, custom_dir=custom_dir)
                service = SkillService(manager)

                with self.assertRaises(ValueError):
                    service.add({
                        "name": "../escape",
                        "type": "url",
                        "files": [{"url": "https://example.invalid/SKILL.md", "path": "SKILL.md"}],
                    })

                with self.assertRaises(ValueError):
                    service.add({
                        "name": "safe-skill",
                        "type": "url",
                        "files": [{"url": "https://example.invalid/SKILL.md", "path": "../evil.txt"}],
                    })

                for invalid_name in ["NUL", "COM1", "skill.", "a:b"]:
                    with self.assertRaises(ValueError):
                        service.add({
                            "name": invalid_name,
                            "type": "url",
                            "files": [{"url": "https://example.invalid/SKILL.md", "path": "SKILL.md"}],
                        })

                self.assertFalse(os.path.exists(os.path.join(workspace, "evil.txt")))
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_skill_load_diagnostics_are_visible_in_prompt(self):
        from agent.skills.manager import SkillManager

        with tempfile.TemporaryDirectory() as workspace:
            builtin_dir = os.path.join(workspace, "builtin")
            custom_dir = os.path.join(workspace, "skills")
            bad_dir = os.path.join(custom_dir, "bad-skill")
            os.makedirs(builtin_dir, exist_ok=True)
            os.makedirs(bad_dir, exist_ok=True)
            with open(os.path.join(bad_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: bad-skill\n---\n# Missing description\n")

            manager = SkillManager(builtin_dir=builtin_dir, custom_dir=custom_dir)
            prompt = manager.build_skills_prompt()

        self.assertIsNone(manager.get_skill("bad-skill"))
        self.assertIn("<skill_load_diagnostics>", prompt)
        self.assertIn("bad-skill", prompt)
        self.assertIn("has no description", prompt)

    def test_host_diagnostics_reports_skill_load_diagnostics(self):
        from agent.tools.host_diagnostics import host_diagnostics

        with tempfile.TemporaryDirectory() as workspace:
            bad_dir = os.path.join(workspace, "skills", "bad-skill")
            os.makedirs(bad_dir, exist_ok=True)
            with open(os.path.join(bad_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: bad-skill\n---\n# Missing description\n")

            status = host_diagnostics._skill_status(workspace)

        self.assertEqual(status["status"], "success")
        self.assertIn("diagnostics", status)
        diagnostics = "\n".join(status["diagnostics"])
        self.assertIn("bad-skill", diagnostics)
        self.assertIn("has no description", diagnostics)

    def test_skill_service_rejects_zip_slip_archives(self):
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService

        with tempfile.TemporaryDirectory() as workspace:
            old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            builtin_dir = os.path.join(workspace, "builtin")
            custom_dir = os.path.join(workspace, "custom")
            os.makedirs(builtin_dir, exist_ok=True)
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                get_tool_permission_broker().set_mode("full-access")
                manager = SkillManager(builtin_dir=builtin_dir, custom_dir=custom_dir)
                service = SkillService(manager)

                def write_zip_slip(_url, dest):
                    with zipfile.ZipFile(dest, "w") as zf:
                        zf.writestr("../evil.txt", "owned")

                with patch.object(service, "_download_file", side_effect=write_zip_slip):
                    with self.assertRaises(ValueError):
                        service.add({
                            "name": "zip-slip-test",
                            "type": "package",
                            "files": [{"url": "https://example.invalid/pkg.zip"}],
                        })

                self.assertFalse(os.path.exists(os.path.join(workspace, "evil.txt")))
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_skill_service_blocks_builtin_and_existing_overwrite_without_explicit_flags(self):
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService

        with tempfile.TemporaryDirectory() as workspace:
            old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            builtin_dir = os.path.join(workspace, "builtin")
            custom_dir = os.path.join(workspace, "custom")
            os.makedirs(os.path.join(builtin_dir, "builtin-skill"), exist_ok=True)
            with open(os.path.join(builtin_dir, "builtin-skill", "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: builtin-skill\ndescription: built in\n---\n")
            os.makedirs(os.path.join(custom_dir, "custom-skill"), exist_ok=True)
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                get_tool_permission_broker().set_mode("full-access")
                manager = SkillManager(builtin_dir=builtin_dir, custom_dir=custom_dir)
                service = SkillService(manager)

                with self.assertRaises(ValueError):
                    service.add({
                        "name": "builtin-skill",
                        "type": "url",
                        "files": [{"url": "https://example.invalid/SKILL.md", "path": "SKILL.md"}],
                    })
                with self.assertRaises(ValueError):
                    service.add({
                        "name": "custom-skill",
                        "type": "url",
                        "files": [{"url": "https://example.invalid/SKILL.md", "path": "SKILL.md"}],
                    })
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_smart_ask_blocks_noninteractive_skill_mutation(self):
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("smart-ask")
            try:
                manager = SkillManager(
                    builtin_dir=os.path.join(workspace, "builtin"),
                    custom_dir=os.path.join(workspace, "skills"),
                )
                service = SkillService(manager)
                response = service.dispatch("open", {"name": "some-skill"})
                self.assertEqual(response["code"], 500)
                self.assertIn("Interactive permission confirmation", response["message"])
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_smart_ask_requires_permission_for_env_config_and_send(self):
        from common.ecorex_tool_permissions import ToolPermissionBroker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as user_data:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            try:
                broker = ToolPermissionBroker()
                broker.set_mode("smart-ask")
                cancel_event = threading.Event()
                cancel_event.set()

                send_decision = broker.authorize(
                    "send",
                    "tool-send",
                    {"path": "secret.pdf"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                env_decision = broker.authorize(
                    "env_config",
                    "tool-env",
                    {"action": "set", "key": "OPENAI_API_KEY", "value": "sk-test-secret"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                scheduler_decision = broker.authorize(
                    "scheduler",
                    "tool-scheduler",
                    {"action": "create", "name": "nightly check"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                rollback_decision = broker.authorize(
                    "evolution_undo",
                    "tool-evolution-undo",
                    {"backup_id": "20260616-000000-000"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                web_fetch_decision = broker.authorize(
                    "web_fetch",
                    "tool-web-fetch",
                    {"url": "https://example.invalid"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                web_search_decision = broker.authorize(
                    "web_search",
                    "tool-web-search",
                    {"query": "EcoreX"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
                vision_decision = broker.authorize(
                    "vision",
                    "tool-vision",
                    {"image": "C:/secret.png", "question": "describe"},
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertFalse(send_decision["allowed"])
        self.assertFalse(env_decision["allowed"])
        self.assertFalse(scheduler_decision["allowed"])
        self.assertFalse(rollback_decision["allowed"])
        self.assertFalse(web_fetch_decision["allowed"])
        self.assertFalse(web_search_decision["allowed"])
        self.assertFalse(vision_decision["allowed"])

    def test_non_web_channel_dangerous_tools_still_fail_closed(self):
        from common.ecorex_tool_permissions import ToolPermissionBroker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        with tempfile.TemporaryDirectory() as user_data:
            try:
                os.environ.pop("ECOREX_DESKTOP", None)
                os.environ["ECOREX_USER_DATA"] = user_data
                broker = ToolPermissionBroker()

                with patch("config.conf", return_value={"channel_type": "telegram"}):
                    broker.set_mode("read-only")
                    for tool_name in ("bash", "write", "mcp_server", "web_fetch"):
                        decision = broker.authorize_noninteractive(tool_name, {"command": "echo blocked"})
                        self.assertFalse(decision["allowed"], tool_name)
                        self.assertIn("read-only", decision["reason"])

                    broker.set_mode("smart-ask")
                    smart_decision = broker.authorize(
                        "bash",
                        "tool-non-web-bash",
                        {"command": "echo blocked"},
                        timeout_seconds=30,
                    )
                    self.assertFalse(smart_decision["allowed"])
                    self.assertIn("unavailable", smart_decision["reason"])

                    broker.set_mode("full-access")
                    full_decision = broker.authorize_noninteractive("bash", {"command": "echo allowed"})
                    self.assertTrue(full_decision["allowed"])
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data

    def test_self_evolution_skips_without_noninteractive_permission(self):
        from agent.evolution.config import EvolutionConfig
        from agent.evolution.executor import (
            _authorize_background_evolution,
            run_evolution_for_session,
        )
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            broker = get_tool_permission_broker()
            try:
                broker.set_mode("smart-ask")
                self.assertFalse(_authorize_background_evolution("session-smart"))

                bridge = types.SimpleNamespace(
                    agents={},
                    default_agent=None,
                    create_agent=lambda *args, **kwargs: self.fail(
                        "background evolution must not start without noninteractive permission"
                    ),
                )
                with patch(
                    "agent.evolution.executor.get_evolution_config",
                    return_value=EvolutionConfig(enabled=True, idle_minutes=1, min_turns=1, max_steps=1),
                ):
                    self.assertFalse(run_evolution_for_session(bridge, "session-smart"))

                broker.set_mode("full-access")
                self.assertTrue(_authorize_background_evolution("session-full"))
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_read_only_blocks_file_and_skill_writes(self):
        from agent.skills.manager import SkillManager
        from agent.skills.service import SkillService
        from agent.tools.edit.edit import Edit
        from agent.tools.write.write import Write
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("read-only")
            try:
                write_result = Write({"cwd": workspace}).execute({"path": "blocked.txt", "content": "x"})
                self.assertEqual(write_result.status, "error")

                target = os.path.join(workspace, "target.txt")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("old")
                edit_result = Edit({"cwd": workspace}).execute({
                    "path": target,
                    "oldText": "old",
                    "newText": "new",
                })
                self.assertEqual(edit_result.status, "error")

                manager = SkillManager(
                    builtin_dir=os.path.join(workspace, "builtin"),
                    custom_dir=os.path.join(workspace, "skills"),
                )
                service = SkillService(manager)
                response = service.dispatch("add", {
                    "name": "new-skill",
                    "type": "url",
                    "files": [{"url": "https://example.invalid/SKILL.md", "path": "SKILL.md"}],
                })
                self.assertEqual(response["code"], 500)
                self.assertIn("read-only", response["message"])
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

    def test_custom_filesystem_profile_limits_file_tools_to_workspace(self):
        from agent.tools.find.find import Find
        from agent.tools.ls.ls import Ls
        from agent.tools.read.read import Read
        from agent.tools.send.send import Send
        from agent.tools.write.write import Write

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            outside = os.path.join(root, "outside.txt")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            with open(os.path.join(workspace, "safe.txt"), "w", encoding="utf-8") as handle:
                handle.write("safe")
            os.makedirs(os.path.join(workspace, "config"), exist_ok=True)
            with open(os.path.join(workspace, "config", ".env"), "w", encoding="utf-8") as handle:
                handle.write("SECRET=1")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("outside")

            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            settings_path = os.path.join(user_data, "permissions.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "write"},
                            {"glob": "**/*.env", "access": "deny"},
                            {"glob": "*.env", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                read_ok = Read({"cwd": workspace}).execute({"path": "safe.txt"})
                find_ok = Find({"cwd": workspace}).execute({"pattern": "*.txt"})
                ls_ok = Ls({"cwd": workspace}).execute({"path": "."})
                write_ok = Write({"cwd": workspace}).execute({"path": "new.txt", "content": "new"})
                send_ok = Send({"cwd": workspace}).execute({"path": "safe.txt"})
                read_outside = Read({"cwd": workspace}).execute({"path": outside})
                send_outside = Send({"cwd": workspace}).execute({"path": outside})
                write_outside = Write({"cwd": workspace}).execute({
                    "path": os.path.join(root, "blocked.txt"),
                    "content": "blocked",
                })
                read_env = Read({"cwd": workspace}).execute({"path": os.path.join("config", ".env")})
                find_env = Find({"cwd": workspace}).execute({"pattern": "*.env", "include_hidden": True})
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertEqual(read_ok.status, "success")
        self.assertEqual(find_ok.status, "success")
        self.assertIn("safe.txt", find_ok.result["output"])
        self.assertNotIn(".env", find_ok.result["output"])
        self.assertEqual(ls_ok.status, "success")
        self.assertEqual(write_ok.status, "success")
        self.assertEqual(send_ok.status, "success")
        self.assertEqual(read_outside.status, "error")
        self.assertIn("Filesystem profile blocks read", str(read_outside.result))
        self.assertEqual(send_outside.status, "error")
        self.assertIn("Filesystem profile blocks read", str(send_outside.result))
        self.assertEqual(write_outside.status, "error")
        self.assertIn("Filesystem profile blocks write", str(write_outside.result))
        self.assertFalse(os.path.exists(os.path.join(root, "blocked.txt")))
        self.assertEqual(read_env.status, "error")
        self.assertIn("Filesystem profile blocks read", str(read_env.result))
        self.assertEqual(find_env.status, "success")
        self.assertNotIn(".env", find_env.result["output"])

    def test_default_filesystem_profile_limits_unprofiled_file_access_to_workspace(self):
        from agent.tools.read.read import Read
        from agent.tools.write.write import Write
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            outside = os.path.join(root, "outside.txt")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            with open(os.path.join(workspace, "safe.txt"), "w", encoding="utf-8") as handle:
                handle.write("safe")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("outside")

            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            try:
                get_tool_permission_broker().set_mode("smart-ask")
                read_ok = Read({"cwd": workspace}).execute({"path": "safe.txt"})
                read_outside = Read({"cwd": workspace}).execute({"path": outside})
                write_outside = Write({"cwd": workspace}).execute({
                    "path": os.path.join(root, "blocked.txt"),
                    "content": "blocked",
                })

                get_tool_permission_broker().set_mode("full-access")
                read_full_access = Read({"cwd": workspace}).execute({"path": outside})
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertEqual(read_ok.status, "success")
        self.assertEqual(read_outside.status, "error")
        self.assertIn("Filesystem profile blocks read", str(read_outside.result))
        self.assertEqual(write_outside.status, "error")
        self.assertIn("Filesystem profile blocks write", str(write_outside.result))
        self.assertFalse(os.path.exists(os.path.join(root, "blocked.txt")))
        self.assertEqual(read_full_access.status, "success")

    def test_default_filesystem_profile_does_not_include_web_file_serve_root(self):
        from agent.tools.read.read import Read
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            preview_root = os.path.join(root, "preview")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(preview_root, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            outside = os.path.join(preview_root, "home-like.txt")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("outside")

            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            try:
                get_tool_permission_broker().set_mode("smart-ask")
                with patch("config.conf", return_value={
                    "agent_workspace": workspace,
                    "web_file_serve_root": preview_root,
                }):
                    result = Read({"cwd": workspace}).execute({"path": outside})
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertEqual(result.status, "error")
        self.assertIn("Filesystem profile blocks read", str(result.result))

    def test_memory_service_and_tool_obey_filesystem_profile(self):
        from agent.memory.service import MemoryService
        from agent.tools.memory.memory_get import MemoryGetTool

        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            memory_dir = os.path.join(workspace, "memory")
            user_data = os.path.join(root, "user-data")
            os.makedirs(memory_dir, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            with open(os.path.join(memory_dir, "public.md"), "w", encoding="utf-8") as handle:
                handle.write("public")
            with open(os.path.join(memory_dir, "secret.md"), "w", encoding="utf-8") as handle:
                handle.write("secret")
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "read"},
                            {"glob": "memory/secret.md", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                service = MemoryService(workspace)
                listed = service.list_files(page_size=10)["list"]
                secret = service.dispatch("content", {"filename": "secret.md"})

                config = types.SimpleNamespace(get_workspace=lambda: Path(workspace))
                tool = MemoryGetTool(types.SimpleNamespace(config=config))
                tool_result = tool.execute({"path": "memory/secret.md"})
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        names = {item["filename"] for item in listed}
        self.assertIn("public.md", names)
        self.assertNotIn("secret.md", names)
        self.assertEqual(secret["code"], 403)
        self.assertEqual(tool_result.status, "error")
        self.assertIn("filesystem profile", str(tool_result.result))

    def test_feishu_auth_required_forces_next_turn_text_only(self):
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.tools.base_tool import ToolResult

        class FakeFeishuTool:
            name = "feishu_cli"
            description = "Feishu"
            params = {"type": "object", "properties": {}}

            def execute_tool(self, _params):
                return ToolResult.success({
                    "authRequired": True,
                    "message": "Open the verification URL, finish Feishu authorization, then continue.",
                })

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[FakeFeishuTool()],
        )

        with patch.object(executor, "_authorize_tool_execution", return_value={"allowed": True}):
            result = executor._execute_tool({
                "id": "tool-call-feishu-auth",
                "name": "feishu_cli",
                "arguments": {"action": "auth_login", "domain": "base"},
            })

        self.assertEqual(result["status"], "success")
        self.assertTrue(executor._force_text_response_next_turn)
        self.assertIn("Feishu authorization", executor._force_text_response_reason)

    def test_remote_document_download_obeys_filesystem_profile_before_network(self):
        from agent.tools.web_fetch.web_fetch import WebFetch

        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "write"},
                            {"glob": "tmp/**", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                with patch("agent.tools.web_fetch.web_fetch.requests.get") as mocked_get:
                    result = WebFetch({"cwd": workspace}).execute({
                        "url": "https://example.invalid/blocked.pdf",
                    })
                    mocked_get.assert_not_called()
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertEqual(result.status, "error")
        self.assertIn("Filesystem profile blocks write", str(result.result))

    def test_vision_local_image_read_obeys_filesystem_profile_before_upload(self):
        from agent.tools.vision.vision import Vision

        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        old_openai_key = os.environ.get("OPENAI_API_KEY")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            with open(os.path.join(workspace, "secret.png"), "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n")
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            os.environ["OPENAI_API_KEY"] = "sk-test"
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "read"},
                            {"glob": "secret.png", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                result = Vision({"cwd": workspace}).execute({
                    "image": "secret.png",
                    "question": "describe",
                })
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data
                if old_openai_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_openai_key

        self.assertEqual(result.status, "error")
        self.assertIn("Filesystem profile blocks read", str(result.result))

    def test_host_diagnostics_log_tail_obeys_filesystem_profile(self):
        from agent.tools.host_diagnostics.host_diagnostics import _tail_text

        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            log_path = Path(workspace) / "run.log"
            log_path.write_text("secret log\n", encoding="utf-8")
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "read"},
                            {"glob": "run.log", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                result = _tail_text(log_path, cwd=workspace)
            finally:
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertTrue(result.get("blocked"))
        self.assertIn("Filesystem profile blocks read", result.get("reason", ""))

    def test_memory_user_id_is_sanitized_to_one_path_segment(self):
        from agent.memory.summarizer import MemoryFlushManager

        with tempfile.TemporaryDirectory() as workspace:
            manager = MemoryFlushManager(Path(workspace), llm_model=None)
            path = manager.get_main_memory_file("../bad\\id/with spaces")
            self.assertTrue(str(path).startswith(str(Path(workspace) / "memory" / "users")))
            self.assertNotIn("..", path.parts)
            self.assertEqual(path.name, "MEMORY.md")

    def test_openai_image_provider_uses_gpt_image_2_payload_and_pro_fallback(self):
        import importlib.util

        script_path = Path(__file__).resolve().parents[1] / "skills" / "image-generation" / "scripts" / "generate.py"
        spec = importlib.util.spec_from_file_location("ecorex_image_generate_test", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as output_dir:
            provider = module.OpenAIProvider("sk-test", "https://api.openai.com/v1", "")
            payloads = []
            urls = []

            def fake_post_json(url, payload):
                urls.append(url)
                payloads.append(dict(payload))
                if payload["model"] == "gpt-image-2-pro":
                    raise RuntimeError("model_not_found: model does not exist")
                return {"data": [{"b64_json": "aGVsbG8="}]}

            provider._post_json = fake_post_json
            paths = provider.generate(
                "orange x",
                quality="low",
                size="1024x1024",
                output_format="png",
                output_dir=output_dir,
            )

        self.assertEqual(provider.model, "gpt-image-2")
        self.assertEqual(urls, [
            "https://api.openai.com/v1/images/generations",
            "https://api.openai.com/v1/images/generations",
        ])
        self.assertEqual([payload["model"] for payload in payloads], ["gpt-image-2-pro", "gpt-image-2"])
        self.assertEqual(payloads[0]["n"], 1)
        self.assertEqual(payloads[0]["quality"], "low")
        self.assertEqual(payloads[0]["output_format"], "png")
        self.assertNotIn("response_format", payloads[0])
        self.assertEqual(len(paths), 1)
        self.assertEqual(
            module.LinkAIProvider("lk-test", "https://api.link-ai.tech", "").model,
            "gpt-image-2-pro",
        )

    def test_openai_image_provider_uses_edits_endpoint_when_image_input_exists(self):
        import importlib.util

        script_path = Path(__file__).resolve().parents[1] / "skills" / "image-generation" / "scripts" / "generate.py"
        spec = importlib.util.spec_from_file_location("ecorex_image_edit_test", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as output_dir:
            provider = module.OpenAIProvider("sk-test", "https://api.openai.com/v1", "image-2-pro")
            calls = []
            module._load_image = lambda _source: b"\x89PNG\r\n\x1a\nfake"
            module._compress_image = lambda data: data

            def fake_post_multipart(url, fields, files):
                calls.append({
                    "url": url,
                    "fields": dict(fields),
                    "files": [(field_name, file_tuple[0], file_tuple[2]) for field_name, file_tuple in files],
                })
                return {"data": [{"b64_json": "aGVsbG8="}]}

            provider._post_multipart = fake_post_multipart
            paths = provider.generate(
                "turn this into watercolor",
                image_url=["source-a.png", "source-b.png"],
                quality="medium",
                size="1024x1024",
                output_format="png",
                output_dir=output_dir,
            )

        self.assertEqual(len(paths), 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["url"].endswith("/images/edits"))
        self.assertEqual(calls[0]["fields"]["model"], "gpt-image-2-pro")
        self.assertEqual(calls[0]["fields"]["prompt"], "turn this into watercolor")
        self.assertEqual(calls[0]["fields"]["quality"], "medium")
        self.assertEqual(calls[0]["fields"]["output_format"], "png")
        self.assertNotIn("response_format", calls[0]["fields"])
        self.assertEqual([item[0] for item in calls[0]["files"]], ["image[]", "image[]"])

        calls.clear()
        with tempfile.TemporaryDirectory() as output_dir:
            provider = module.OpenAIProvider("sk-test", "https://api.openai.com/v1", "image-2-pro")
            provider._post_multipart = fake_post_multipart
            provider.generate(
                "add a red border",
                image_url="source-a.png",
                output_dir=output_dir,
            )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["url"].endswith("/images/edits"))
        self.assertEqual([item[0] for item in calls[0]["files"]], ["image"])

    def test_admin_image_capability_predicts_openai_pro_default(self):
        from channel.web.web_channel import ModelsHandler

        capability = ModelsHandler._image_capability({"open_ai_api_key": "sk-test"})

        self.assertEqual(capability["fallback_provider"], "openai")
        self.assertEqual(capability["fallback_model"], "gpt-image-2-pro")
        self.assertEqual(capability["provider_models"]["openai"][0], "gpt-image-2-pro")
        self.assertEqual(capability["provider_models"]["linkai"][0], "gpt-image-2-pro")

        linkai_only = ModelsHandler._image_capability({"linkai_api_key": "lk-test"})
        self.assertEqual(linkai_only["fallback_provider"], "linkai")
        self.assertEqual(linkai_only["fallback_model"], "gpt-image-2-pro")

    def test_v014_defaults_keep_agent_install_and_image_2_pro(self):
        root = Path(__file__).resolve().parents[1]
        direct_install_sources = [
            root / "desktop" / "electron" / "capabilities.ts",
            root / "desktop" / "electron" / "main.ts",
            root / "desktop" / "electron" / "preload.cts",
            root / "desktop" / "src" / "services" / "ecorexApi.ts",
            root / "desktop" / "src" / "vite-env.d.ts",
            root / "channel" / "web" / "web_channel.py",
        ]
        forbidden = [
            "installCapabilityPack",
            "install-capability-pack",
            "preinstallPolicyPacks",
            "capability-preinstall",
            "capability-install",
            "async installPack",
        ]
        for source in direct_install_sources:
            text = source.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, text, f"{source} still exposes app-side install marker {marker!r}")

        generate_py = (root / "skills" / "image-generation" / "scripts" / "generate.py").read_text(encoding="utf-8")
        skill_md = (root / "skills" / "image-generation" / "SKILL.md").read_text(encoding="utf-8")
        xhs_py = (root / "skills" / "create-xiaohongshu-note" / "scripts" / "generate_cover_image.py").read_text(encoding="utf-8")
        manager_py = (root / "agent" / "skills" / "manager.py").read_text(encoding="utf-8")
        enterprise_policy_ts = (root / "desktop" / "electron" / "enterprisePolicy.ts").read_text(encoding="utf-8")
        stage_runtime_win = (root / "desktop" / "scripts" / "stage-runtime-win.ps1").read_text(encoding="utf-8")
        web_channel_py = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', generate_py)
        self.assertIn('FALLBACK_MODEL = "gpt-image-2"', generate_py)
        self.assertIn("LinkAI default model follows EcoreX's OpenAI image default", generate_py)
        self.assertNotIn('("linkai",    "image-2-pro")', web_channel_py)
        self.assertIn('("linkai",    "gpt-image-2-pro")', web_channel_py)
        self.assertIn('"linkai": [\n            "gpt-image-2-pro"', web_channel_py)
        self.assertIn('Do not create final images by coding HTML/canvas/SVG/Pillow layouts', skill_md)
        self.assertIn("legacy `image-2-pro` input is normalized", skill_md)
        self.assertIn('parser.add_argument("--model", default="gpt-image-2-pro")', xhs_py)
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', manager_py)
        self.assertIn('default="gpt-image-2-pro"', manager_py)
        self.assertIn('"ecorex-desktop-v0.1.14"', enterprise_policy_ts)
        self.assertIn('"ecorex-desktop-v0.1.13"', enterprise_policy_ts)
        self.assertIn("enterpriseClientEventKeys", enterprise_policy_ts)
        self.assertIn("hasPolicyOverrideValue", enterprise_policy_ts)
        self.assertIn("return value.length > 0", enterprise_policy_ts)
        self.assertIn("compatClientEventKeys", stage_runtime_win)
        self.assertIn("ecorex-web-v0.1.14-web.1", web_channel_py)
        self.assertIn("ecorex-web-v0.1.13-web.1", web_channel_py)
        self.assertIn("webClientKeys", web_channel_py)
        self.assertIn("invalid client key", web_channel_py)

    def test_v017_sidecar_phase_latch_and_diagnostics_contract(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "desktop" / "electron" / "sidecar.ts").read_text(encoding="utf-8")

        required_markers = [
            'export type SidecarPhase =',
            'private startupPromise: Promise<boolean> | null = null;',
            'this.appendDiagnostic(this.status, "single-flight-startup")',
            'startupInFlight: Boolean(this.startupPromise)',
            'recentEvents: this.diagnosticEvents.slice(-this.diagnosticLimit)',
            'export type SidecarManagerOptions =',
            'private readonly spawnProcess: typeof spawn;',
            'this.spawnProcess(python, ["app.py"]',
            'this.clearTimeoutImpl(this.restartTimer)',
            'this.fetchImpl(`http://127.0.0.1:${webPort}/api/version`',
            'this.broadcastStatus(nextStatus);',
            'private redactDiagnosticText(value: string)',
            'message: this.redactDiagnosticText(status.message)',
            'if (this.child !== launchedChild) return;',
            'this.getState() === "running" && this.phase === "ready"',
            'state: "running",\n      message: `EcoreX local runtime health check degraded',
            '}, "degraded", "health-probe-failed");',
            '}, "restarting", "health-check-failed");',
            '}, "ready", "health-recovered");',
            'this.scheduleRestart(webPort, "startup-timeout")',
        ]
        for marker in required_markers:
            self.assertIn(marker, source)

        self.assertRegex(
            source,
            r"if \(this\.startupPromise\) \{[\s\S]*?Promise\.race\(\[this\.startupPromise, timeout\]\)"
        )
        self.assertRegex(
            source,
            r"if \(stoppedIntentionally\) \{\s*if \(this\.stoppingIntentionally && !this\.child\) \{[\s\S]*?state: \"stopped\"[\s\S]*?\}\s*return;"
        )

    def test_v017_sidecar_bridge_timeout_covers_response_body_and_reports_phase(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "desktop" / "electron" / "apiBridge.ts").read_text(encoding="utf-8")

        self.assertIn("const MAX_SIDECAR_RESPONSE_BYTES", source)
        self.assertIn("text = await readResponseTextWithLimit(response);", source)
        self.assertLess(source.index("text = await readResponseTextWithLimit(response);"), source.index("clearTimeout(timeout);"))
        self.assertIn("sidecar response exceeded", source)
        self.assertIn("sanitizeBridgeSnippet(text, sidecar.getRuntimeToken()).slice(0, 400)", source)
        self.assertIn("sidecarPhase: status.phase", source)
        self.assertIn("sidecarDiagnostics: status.diagnostics", source)
        self.assertIn('"X-EcoreX-Runtime-Token": sidecar.getRuntimeToken()', source)

    def test_legacy_openai_image_payload_supports_gpt_image_base64(self):
        from models.openai.open_ai_image import OpenAIImage

        image = OpenAIImage()
        payload = image._build_image_payload("orange x", "gpt-image-2-pro")
        self.assertEqual(payload["model"], "gpt-image-2-pro")
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["output_format"], "png")
        self.assertNotIn("response_format", payload)

        legacy_payload = image._build_image_payload("orange x", "image-2-pro")
        self.assertEqual(legacy_payload["model"], "gpt-image-2-pro")
        self.assertEqual(legacy_payload["output_format"], "png")
        self.assertNotIn("response_format", legacy_payload)

        with tempfile.TemporaryDirectory() as temp_root:
            with patch("models.openai.open_ai_image.tempfile.gettempdir", return_value=temp_root):
                url = image._save_image_item({"b64_json": "aGVsbG8="}, "png")
                self.assertTrue(url.startswith("file://"))
                self.assertTrue(os.path.exists(url.removeprefix("file://")))

    def test_managed_builtin_skill_refresh_replaces_stale_workspace_copy(self):
        from agent.skills.manager import SkillManager

        with tempfile.TemporaryDirectory() as root:
            builtin = Path(root) / "builtin"
            custom = Path(root) / "custom"
            builtin_skill = builtin / "image-generation"
            custom_skill = custom / "image-generation"
            (builtin_skill / "scripts").mkdir(parents=True)
            (custom_skill / "scripts").mkdir(parents=True)
            (builtin_skill / "SKILL.md").write_text(
                "---\nname: image-generation\ndescription: Generate images\n---\n"
                "OpenAI model {model} unavailable\n\"output_format\"\n"
                "/images/edits\nrequests with `image_url` use\n"
                "LinkAI default model follows EcoreX's OpenAI image default\n",
                encoding="utf-8",
            )
            (builtin_skill / "scripts" / "generate.py").write_text(
                'DEFAULT_MODEL = "gpt-image-2-pro"\n',
                encoding="utf-8",
            )
            (custom_skill / "SKILL.md").write_text(
                "---\nname: image-generation\ndescription: Generate images\n---\n",
                encoding="utf-8",
            )
            (custom_skill / "scripts" / "generate.py").write_text(
                'DEFAULT_MODEL = "gpt-image-2"\n',
                encoding="utf-8",
            )

            SkillManager(builtin_dir=str(builtin), custom_dir=str(custom))

            preserved = (custom_skill / "scripts" / "generate.py").read_text(encoding="utf-8")
            self.assertIn('DEFAULT_MODEL = "gpt-image-2"', preserved)
            self.assertFalse((custom / ".ecorex-backups").exists())

    def test_managed_builtin_skill_refresh_respects_explicit_override_marker(self):
        from agent.skills.manager import CUSTOM_OVERRIDE_MARKER, SkillManager

        with tempfile.TemporaryDirectory() as root:
            builtin = Path(root) / "builtin"
            custom = Path(root) / "custom"
            builtin_skill = builtin / "image-generation"
            custom_skill = custom / "image-generation"
            (builtin_skill / "scripts").mkdir(parents=True)
            (custom_skill / "scripts").mkdir(parents=True)
            (builtin_skill / "SKILL.md").write_text(
                "---\nname: image-generation\ndescription: Generate images\n---\n"
                "OpenAI model {model} unavailable\n\"output_format\"\n"
                "/images/edits\nrequests with `image_url` use\n"
                "LinkAI default model follows EcoreX's OpenAI image default\n",
                encoding="utf-8",
            )
            (builtin_skill / "scripts" / "generate.py").write_text(
                'DEFAULT_MODEL = "gpt-image-2-pro"\n',
                encoding="utf-8",
            )
            (custom_skill / "SKILL.md").write_text(
                "---\nname: image-generation\ndescription: Generate images\n---\n",
                encoding="utf-8",
            )
            (custom_skill / "scripts" / "generate.py").write_text(
                'DEFAULT_MODEL = "gpt-image-2"\n',
                encoding="utf-8",
            )
            (custom_skill / CUSTOM_OVERRIDE_MARKER).write_text("intentional override\n", encoding="utf-8")

            SkillManager(builtin_dir=str(builtin), custom_dir=str(custom))

            preserved = (custom_skill / "scripts" / "generate.py").read_text(encoding="utf-8")
            self.assertIn('DEFAULT_MODEL = "gpt-image-2"', preserved)
            self.assertFalse((custom / ".ecorex-backups").exists())

    def test_managed_xhs_skill_refresh_uses_images_endpoint_marker(self):
        from agent.skills.manager import SkillManager

        with tempfile.TemporaryDirectory() as root:
            builtin = Path(root) / "builtin"
            custom = Path(root) / "custom"
            builtin_skill = builtin / "create-xiaohongshu-note"
            custom_skill = custom / "create-xiaohongshu-note"
            (builtin_skill / "scripts").mkdir(parents=True)
            (custom_skill / "scripts").mkdir(parents=True)
            (builtin_skill / "SKILL.md").write_text(
                "---\nname: create-xiaohongshu-note\ndescription: Create XHS note\n---\n",
                encoding="utf-8",
            )
            (builtin_skill / "scripts" / "generate_cover_image.py").write_text(
                'parser.add_argument("--model", default="gpt-image-2-pro")\n'
                'url = f"{api_base}/images/generations"\n'
                'payload["output_format"] = "png"\n',
                encoding="utf-8",
            )
            (custom_skill / "SKILL.md").write_text(
                "---\nname: create-xiaohongshu-note\ndescription: Create XHS note\n---\n",
                encoding="utf-8",
            )
            (custom_skill / "scripts" / "generate_cover_image.py").write_text(
                "from openai import OpenAI\n",
                encoding="utf-8",
            )

            SkillManager(builtin_dir=str(builtin), custom_dir=str(custom))

            preserved = (custom_skill / "scripts" / "generate_cover_image.py").read_text(encoding="utf-8")
            self.assertIn("from openai import OpenAI", preserved)
            self.assertFalse((custom / ".ecorex-backups").exists())

    def test_custom_filesystem_profile_blocks_background_memory_writes(self):
        from agent.memory.summarizer import (
            MemoryFlushManager,
            create_memory_files_if_needed,
            ensure_daily_memory_file,
        )

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            settings_path = os.path.join(user_data, "permissions.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [],
                    },
                }, handle)
            try:
                MemoryFlushManager(Path(workspace), llm_model=None)
                create_memory_files_if_needed(Path(workspace))
                with self.assertRaises(PermissionError):
                    ensure_daily_memory_file(Path(workspace))

                self.assertFalse(os.path.exists(os.path.join(workspace, "MEMORY.md")))
                self.assertFalse(os.path.exists(os.path.join(workspace, "memory")))

                with open(settings_path, "w", encoding="utf-8") as handle:
                    json.dump({
                        "mode": "custom",
                        "filesystem": {
                            "default": "deny",
                            "workspaceRoots": [workspace],
                            "rules": [
                                {"path": ":workspace_roots", "access": "write"},
                            ],
                        },
                    }, handle)

                create_memory_files_if_needed(Path(workspace))
                daily_path = ensure_daily_memory_file(Path(workspace))
                self.assertTrue(os.path.exists(os.path.join(workspace, "MEMORY.md")))
                self.assertTrue(daily_path.exists())
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

    def test_custom_filesystem_profile_limits_knowledge_reads(self):
        from agent.knowledge.service import KnowledgeService

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            knowledge = os.path.join(workspace, "knowledge")
            user_data = os.path.join(root, "user-data")
            os.makedirs(knowledge, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            with open(os.path.join(knowledge, "public.md"), "w", encoding="utf-8") as handle:
                handle.write("# Public\n\nok")
            with open(os.path.join(knowledge, "secret.md"), "w", encoding="utf-8") as handle:
                handle.write("# Secret\n\nblocked")

            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [
                            {"path": ":workspace_roots", "access": "read"},
                            {"glob": "knowledge/secret.md", "access": "deny"},
                        ],
                    },
                }, handle)
            try:
                service = KnowledgeService(workspace)
                public = service.read_file("public.md")
                with self.assertRaises(PermissionError):
                    service.read_file("secret.md")
                tree = service.list_tree()
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertIn("ok", public["content"])
        root_names = {item["name"] for item in tree.get("root_files", [])}
        self.assertIn("public.md", root_names)
        self.assertNotIn("secret.md", root_names)

    def test_web_file_serve_obeys_custom_filesystem_profile(self):
        from channel.web import web_channel

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_USER_DATA")
        old_desktop_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.makedirs(user_data, exist_ok=True)
            allowed_path = os.path.join(workspace, "allowed.txt")
            denied_path = os.path.join(root, "denied.txt")
            with open(allowed_path, "w", encoding="utf-8") as handle:
                handle.write("allowed")
            with open(denied_path, "w", encoding="utf-8") as handle:
                handle.write("denied")

            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_USER_DATA"] = user_data
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            with open(os.path.join(user_data, "permissions.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "mode": "custom",
                    "filesystem": {
                        "default": "deny",
                        "workspaceRoots": [workspace],
                        "rules": [{"path": ":workspace_roots", "access": "read"}],
                    },
                }, handle)

            handler = web_channel.FileServeHandler()
            try:
                with patch.object(web_channel, "_require_auth", return_value=None):
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        with patch.object(web_channel, "conf", return_value={
                            "web_file_serve_root": root,
                            "agent_workspace": workspace,
                        }):
                            with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(path=allowed_path)):
                                allowed = handler.GET()
                            with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(path=denied_path)):
                                with self.assertRaises(Exception):
                                    handler.GET()
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_USER_DATA", None)
                else:
                    os.environ["ECOREX_USER_DATA"] = old_user_data
                if old_desktop_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_desktop_user_data

        self.assertEqual(allowed, b"allowed")

    def test_read_only_blocks_send_and_env_config_mutations(self):
        from agent.tools.env_config.env_config import EnvConfig
        from agent.tools.send.send import Send
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("read-only")
            try:
                env_tool = EnvConfig()
                env_tool.env_dir = os.path.join(workspace, ".cow")
                env_tool.env_path = os.path.join(env_tool.env_dir, ".env")
                set_result = env_tool.execute({
                    "action": "set",
                    "key": "OPENAI_API_KEY",
                    "value": "sk-test-secret",
                })
                delete_result = env_tool.execute({
                    "action": "delete",
                    "key": "OPENAI_API_KEY",
                })

                target = os.path.join(workspace, "report.txt")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("private report")
                send_result = Send({"cwd": workspace}).execute({"path": target})
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertEqual(set_result.status, "error")
        self.assertEqual(delete_result.status, "error")
        self.assertEqual(send_result.status, "error")
        self.assertFalse(os.path.exists(env_tool.env_path))

    def test_read_only_blocks_scheduler_mutations(self):
        from common.ecorex_tool_permissions import get_tool_permission_broker

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("read-only")
            try:
                with patch.dict(sys.modules, {"croniter": fake_croniter}):
                    from agent.tools.scheduler.scheduler_tool import SchedulerTool

                    result = SchedulerTool({}).execute({
                        "action": "create",
                        "name": "check disk",
                        "message": "check disk",
                        "schedule_type": "once",
                        "schedule_value": "+1m",
                    })
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertEqual(result.status, "error")
        self.assertIn("read-only", str(result.result))

    def test_read_only_blocks_evolution_undo_and_remote_document_download(self):
        from agent.tools.evolution_undo.evolution_undo import EvolutionUndoTool
        from agent.tools.web_fetch.web_fetch import WebFetch
        from agent.tools.web_search.web_search import WebSearch
        from agent.tools.vision.vision import Vision
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("read-only")
            try:
                undo_result = EvolutionUndoTool().execute({"backup_id": "20260616-000000-000"})
                fetch_result = WebFetch({"cwd": workspace}).execute({"url": "https://example.invalid/report.pdf"})
                html_fetch_result = WebFetch({"cwd": workspace}).execute({"url": "https://example.invalid/"})
                search_result = WebSearch({}).execute({"query": "EcoreX"})
                vision_result = Vision({}).execute({"image": os.path.join(workspace, "secret.png"), "question": "describe"})
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertEqual(undo_result.status, "error")
        self.assertIn("read-only", str(undo_result.result))
        self.assertEqual(fetch_result.status, "error")
        self.assertIn("read-only", str(fetch_result.result))
        self.assertEqual(html_fetch_result.status, "error")
        self.assertIn("read-only", str(html_fetch_result.result))
        self.assertEqual(search_result.status, "error")
        self.assertIn("read-only", str(search_result.result))
        self.assertEqual(vision_result.status, "error")
        self.assertIn("read-only", str(vision_result.result))
        self.assertFalse(os.path.exists(os.path.join(workspace, "tmp")))

    def test_scheduler_background_execution_requires_noninteractive_permission(self):
        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            broker = get_tool_permission_broker()
            try:
                broker.set_mode("read-only")
                with patch.dict(sys.modules, {"croniter": fake_croniter}):
                    from agent.tools.scheduler.integration import _authorize_scheduled_execution

                    blocked = _authorize_scheduled_execution({
                        "id": "task-readonly",
                        "name": "nightly",
                        "action": {"type": "agent_task"},
                    })
                    broker.set_mode("full-access")
                    allowed = _authorize_scheduled_execution({
                        "id": "task-full",
                        "name": "nightly",
                        "action": {"type": "agent_task"},
                    })
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertFalse(blocked)
        self.assertTrue(allowed)

    def test_scheduler_tool_call_checks_target_tool_permission(self):
        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            broker = get_tool_permission_broker()
            try:
                broker.set_mode("read-only")
                with patch.dict(sys.modules, {"croniter": fake_croniter}):
                    from agent.tools.scheduler.integration import _authorize_scheduled_tool_call

                    fake_tool = types.SimpleNamespace(name="bash")
                    blocked = _authorize_scheduled_tool_call(
                        fake_tool,
                        "bash",
                        {"command": "echo blocked"},
                        {"id": "task-tool"},
                    )
                    broker.set_mode("full-access")
                    allowed = _authorize_scheduled_tool_call(
                        fake_tool,
                        "bash",
                        {"command": "echo allowed"},
                        {"id": "task-tool"},
                    )
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertFalse(blocked)
        self.assertTrue(allowed)

    def test_host_diagnostics_feishu_status_obeys_noninteractive_permission(self):
        from agent.tools.host_diagnostics import host_diagnostics
        from common.ecorex_tool_permissions import get_tool_permission_broker

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as workspace:
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = os.path.join(workspace, "user-data")
            get_tool_permission_broker().set_mode("read-only")
            try:
                with patch("agent.tools.feishu_cli.feishu_cli.FeishuCli.execute") as execute:
                    result = host_diagnostics._feishu_status(workspace)
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertEqual(result["status"], "blocked")
        execute.assert_not_called()

    def test_bash_tool_cancel_event_stops_running_command(self):
        from agent.tools.bash.bash import Bash

        with tempfile.TemporaryDirectory() as workspace:
            tool = Bash({"cwd": workspace, "safety_mode": False, "timeout": 10})
            cancel_event = threading.Event()
            cancel_event.set()
            tool.cancel_event = cancel_event

            start = time.monotonic()
            result = tool.execute({
                "command": f'"{sys.executable}" -c "import time; time.sleep(5)"',
                "timeout": 10,
            })
            elapsed = time.monotonic() - start

        self.assertEqual(result.status, "error")
        self.assertIn("cancelled", str(result.result).lower())
        self.assertLess(elapsed, 3)

    def test_bash_timeout_is_normalized_before_process_start(self):
        from agent.tools.bash.bash import Bash

        with tempfile.TemporaryDirectory() as workspace:
            tool = Bash({"cwd": workspace, "safety_mode": False, "timeout": "bad"})
            result = tool.execute({
                "command": f'"{sys.executable}" -c "print(123)"',
                "timeout": "also-bad",
            })

        self.assertEqual(result.status, "success")
        self.assertIn("123", str(result.result))


if __name__ == "__main__":
    unittest.main()
