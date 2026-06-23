# encoding:utf-8
import hashlib
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

    def test_session_lock_preserves_live_stale_owner_pid(self):
        from common.ecorex_workspace import LOCK_STALE_SECONDS, SessionBusyError, SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            lock = SessionLock(workspace, "session-live-stale")
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": "session-live-stale",
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "createdAt": int(time.time()) - LOCK_STALE_SECONDS - 10,
                }),
                encoding="utf-8",
            )
            stale_mtime = time.time() - LOCK_STALE_SECONDS - 10
            os.utime(lock.path, (stale_mtime, stale_mtime))

            with self.assertRaises(SessionBusyError):
                SessionLock(workspace, "session-live-stale").acquire()

            self.assertTrue(lock.path.exists())

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

    def test_manual_session_rename_locks_title_against_generated_updates(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-title-lock", [{"role": "user", "content": "first prompt"}], channel_type="web")

            self.assertTrue(store.rename_session("session-title-lock", "Manual title", lock_title=True))
            self.assertFalse(
                store.rename_session(
                    "session-title-lock",
                    "Generated title",
                    respect_title_lock=True,
                )
            )

            sessions = store.list_sessions(channel_type="web")["sessions"]
            title_state = store.get_session_title_state("session-title-lock")

        self.assertEqual(title_state["title"], "Manual title")
        self.assertTrue(title_state["title_locked"])
        self.assertEqual(sessions[0]["title"], "Manual title")
        self.assertTrue(sessions[0]["title_locked"])
        self.assertTrue(sessions[0]["titleLocked"])

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

    def test_latest_pair_seq_prefers_assistant_text_over_tool_only_row(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-final-text-seq", [
                {"role": "user", "content": "查飞书群消息"},
                {"role": "assistant", "content": "最终摘要：没有待办。"},
                {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "feishu_cli",
                        "input": {"action": "run"},
                    }],
                },
            ], channel_type="web")

            seqs = store.get_latest_pair_seqs("session-final-text-seq")

        self.assertEqual(seqs["user_seq"], 0)
        self.assertEqual(seqs["bot_seq"], 1)

    def test_history_page_reads_recent_window_for_long_sessions(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            rows = []
            for index in range(40):
                rows.append({"role": "user", "content": f"question {index:02d}"})
                rows.append({"role": "assistant", "content": f"answer {index:02d}"})
            store.append_messages("session-long-history", rows, channel_type="web")

            page = store.load_history_page("session-long-history", page=1, page_size=10)

        contents = [message["content"] for message in page["messages"]]
        self.assertEqual(len(page["messages"]), 10)
        self.assertTrue(page["has_more"])
        self.assertEqual(contents[0], "question 35")
        self.assertEqual(contents[-1], "answer 39")
        self.assertNotIn("question 00", contents)


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

    def test_subagent_running_cancel_uses_pre_registered_token_and_releases(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module

        class FakeAgentBridge:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.context = None
                self.cancel_event = None

            def agent_reply(self, _prompt, context=None, **_kwargs):
                self.context = context
                self.cancel_event = get_cancel_registry().get_event(context.get("request_id", ""))
                self.entered.set()
                self.release.wait(timeout=2)
                return "child result after cancel"

        class FakeBridge:
            def __init__(self, agent_bridge):
                self.agent_bridge = agent_bridge

            def get_agent_bridge(self):
                return self.agent_bridge

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            tool = self._tool(workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "cancel running child"})
            task = result.result["task"]
            child_session_id = task["childSessionId"]
            fake_agent_bridge = FakeAgentBridge()
            worker = threading.Thread(target=subagent_module._run_child, args=(Path(workspace), task))

            try:
                with patch("bridge.bridge.Bridge", return_value=FakeBridge(fake_agent_bridge)):
                    worker.start()
                    self.assertTrue(fake_agent_bridge.entered.wait(timeout=2))
                    self.assertEqual(fake_agent_bridge.context.get("cancel_token_owner"), "subagent")
                    self.assertIs(fake_agent_bridge.cancel_event, registry.get_event(child_session_id))

                    cancel_result = tool.execute({"action": "cancel", "id": task["id"]})

                    self.assertEqual(cancel_result.status, "success")
                    self.assertEqual(cancel_result.result["cancelled"], 1)
                    self.assertTrue(fake_agent_bridge.cancel_event.is_set())
                    cancelling_run = ledger.get_run(child_session_id)
                    self.assertEqual(cancelling_run["status"], "cancelling")
                    self.assertEqual(cancelling_run["phase"], "cancelling")
                    fake_agent_bridge.release.set()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())

                listed = tool.execute({"action": "list"}).result["tasks"]
                stored = {item["id"]: item for item in listed}[task["id"]]
                self.assertEqual(stored["status"], "cancelled")
                self.assertEqual(stored["result"], "child result after cancel")
                final = ledger.get_run(child_session_id)
                self.assertEqual(final["status"], "cancelled")
                self.assertEqual(final["terminal_reason"], "cancelled_after_reply")
                self.assertIsNone(registry.get_event(child_session_id))
                self.assertEqual(ledger.active_snapshot(), [])
            finally:
                fake_agent_bridge.release.set()
                worker.join(timeout=2)
                registry.unregister(child_session_id)

    def test_subagent_cancel_between_start_check_and_phase_mark_does_not_downgrade(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module

        class FakeAgentBridge:
            called = False

            def agent_reply(self, *_args, **_kwargs):
                self.called = True
                return "should not run"

        class FakeBridge:
            def __init__(self, agent_bridge):
                self.agent_bridge = agent_bridge

            def get_agent_bridge(self):
                return self.agent_bridge

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            tool = self._tool(workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "cancel during phase mark"})
            task = result.result["task"]
            child_session_id = task["childSessionId"]
            original_mark_phase = subagent_module._mark_subagent_run_phase
            fake_agent_bridge = FakeAgentBridge()
            observed = {}

            def cancel_before_running_phase(phase_task, phase, **kwargs):
                if phase == "running" and not observed.get("cancelled"):
                    cancel_result = tool.execute({"action": "cancel", "id": task["id"]})
                    observed["cancelled"] = cancel_result.result["cancelled"]
                    original_mark_phase(phase_task, phase, **kwargs)
                    observed["status_after_running_mark"] = ledger.get_run(child_session_id)["status"]
                    observed["phase_after_running_mark"] = ledger.get_run(child_session_id)["phase"]
                    return
                original_mark_phase(phase_task, phase, **kwargs)

            try:
                with patch("bridge.bridge.Bridge", return_value=FakeBridge(fake_agent_bridge)), \
                        patch.object(subagent_module, "_mark_subagent_run_phase", side_effect=cancel_before_running_phase):
                    subagent_module._run_child(Path(workspace), task)

                self.assertEqual(observed["cancelled"], 1)
                self.assertEqual(observed["status_after_running_mark"], "cancelling")
                self.assertEqual(observed["phase_after_running_mark"], "cancelling")
                self.assertFalse(fake_agent_bridge.called)
                stored = {
                    item["id"]: item
                    for item in tool.execute({"action": "list"}).result["tasks"]
                }[task["id"]]
                self.assertEqual(stored["status"], "cancelled")
                final = ledger.get_run(child_session_id)
                self.assertEqual(final["status"], "cancelled")
                self.assertEqual(final["terminal_reason"], "cancelled_before_start")
                self.assertIsNone(registry.get_event(child_session_id))
                self.assertEqual(ledger.active_snapshot(), [])
            finally:
                registry.unregister(child_session_id)

    def test_parent_cancel_running_subagent_uses_pre_registered_token_and_releases(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module
        from agent.tools.subagent.subagent import cancel_children_for_parent

        class FakeAgentBridge:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.context = None
                self.cancel_event = None

            def agent_reply(self, _prompt, context=None, **_kwargs):
                self.context = context
                self.cancel_event = get_cancel_registry().get_event(context.get("request_id", ""))
                self.entered.set()
                self.release.wait(timeout=2)
                return "child result after parent cancel"

        class FakeBridge:
            def __init__(self, agent_bridge):
                self.agent_bridge = agent_bridge

            def get_agent_bridge(self):
                return self.agent_bridge

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            tool = self._tool(workspace, "parent-session")
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "cancel from parent"})
            task = result.result["task"]
            child_session_id = task["childSessionId"]
            fake_agent_bridge = FakeAgentBridge()
            worker = threading.Thread(target=subagent_module._run_child, args=(Path(workspace), task))

            try:
                with patch("bridge.bridge.Bridge", return_value=FakeBridge(fake_agent_bridge)):
                    worker.start()
                    self.assertTrue(fake_agent_bridge.entered.wait(timeout=2))

                    summary = cancel_children_for_parent(workspace, "parent-session")

                    self.assertEqual(summary["cancelledTasks"], 1)
                    self.assertEqual(summary["cancelledRequests"], 1)
                    self.assertEqual(summary["tasks"][0]["status"], "cancelling")
                    self.assertTrue(fake_agent_bridge.cancel_event.is_set())
                    cancelling_run = ledger.get_run(child_session_id)
                    self.assertEqual(cancelling_run["status"], "cancelling")
                    self.assertEqual(cancelling_run["phase"], "cancelling")
                    fake_agent_bridge.release.set()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())

                listed = tool.execute({"action": "list"}).result["tasks"]
                stored = {item["id"]: item for item in listed}[task["id"]]
                self.assertEqual(stored["status"], "cancelled")
                self.assertEqual(stored["result"], "child result after parent cancel")
                final = ledger.get_run(child_session_id)
                self.assertEqual(final["status"], "cancelled")
                self.assertEqual(final["terminal_reason"], "cancelled_after_reply")
                self.assertIsNone(registry.get_event(child_session_id))
                self.assertEqual(ledger.active_snapshot(), [])
            finally:
                fake_agent_bridge.release.set()
                worker.join(timeout=2)
                registry.unregister(child_session_id)

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

    def test_chat_cancel_fast_path_cascades_running_subagents(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module
        from channel.chat_channel import ChatChannel
        from bridge.context import Context, ContextType

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            tool = self._tool(workspace, "parent-session")
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "cancel from chat fast path"})
            task = result.result["task"]
            child_session_id = task["childSessionId"]
            subagent_module._update_task(Path(workspace), task["id"], {
                "status": "running",
                "startedAt": int(time.time()),
            })
            ledger.mark_phase(child_session_id, "running", status="running")
            registry.register(child_session_id, session_id=child_session_id)
            replies = []
            channel = ChatChannel.__new__(ChatChannel)
            channel._send_reply = lambda _context, reply: replies.append(reply)
            context = Context(ContextType.TEXT, "/cancel", {"workspace_dir": workspace})

            try:
                channel._handle_cancel_command(context, "parent-session")

                self.assertEqual(len(replies), 1)
                self.assertTrue(str(replies[0].content).strip())
                stored = {
                    item["id"]: item
                    for item in tool.execute({"action": "list"}).result["tasks"]
                }[task["id"]]
                self.assertEqual(stored["status"], "cancelling")
                active = [row for row in registry.snapshot() if row["request_id"] == child_session_id]
                self.assertEqual(len(active), 1)
                self.assertTrue(active[0]["cancelled"])
                run = ledger.get_run(child_session_id)
                self.assertEqual(run["status"], "cancelling")
                self.assertEqual(run["phase"], "cancelling")
            finally:
                registry.unregister(child_session_id)

    def test_cow_cli_cancel_fallback_cascades_running_subagents(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from agent.tools.subagent import subagent as subagent_module
        from bridge.context import Context, ContextType
        import plugins

        old_plugin_path = plugins.instance.current_plugin_path
        cow_cli_was_registered = "COW_CLI" in plugins.instance.plugins
        old_cow_cli_plugin = plugins.instance.plugins.get("COW_CLI")
        parent_had_cow_cli = hasattr(plugins, "cow_cli")
        old_parent_cow_cli = getattr(plugins, "cow_cli", None)
        module_names = ("plugins.cow_cli", "plugins.cow_cli.cow_cli")
        old_modules = {
            name: sys.modules[name]
            for name in module_names
            if name in sys.modules
        }
        plugins.instance.current_plugin_path = os.path.join(
            os.path.dirname(__file__), "..", "plugins", "cow_cli"
        )
        try:
            import plugins.cow_cli.cow_cli
            CowCliPlugin = plugins.instance.plugins["COW_CLI"]
        finally:
            plugins.instance.current_plugin_path = old_plugin_path
            if cow_cli_was_registered:
                plugins.instance.plugins["COW_CLI"] = old_cow_cli_plugin
            else:
                plugins.instance.plugins.pop("COW_CLI", None)
            for name in module_names:
                if name in old_modules:
                    sys.modules[name] = old_modules[name]
                else:
                    sys.modules.pop(name, None)
            if parent_had_cow_cli:
                plugins.cow_cli = old_parent_cow_cli
            elif hasattr(plugins, "cow_cli"):
                delattr(plugins, "cow_cli")

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            tool = self._tool(workspace, "parent-session")
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                result = tool.execute({"action": "start", "task": "cancel from cow cli"})
            task = result.result["task"]
            child_session_id = task["childSessionId"]
            subagent_module._update_task(Path(workspace), task["id"], {
                "status": "running",
                "startedAt": int(time.time()),
            })
            ledger.mark_phase(child_session_id, "running", status="running")
            registry.register(child_session_id, session_id=child_session_id)
            plugin = CowCliPlugin.__new__(CowCliPlugin)
            context = Context(ContextType.TEXT, "/cancel", {
                "session_id": "parent-session",
                "workspace_dir": workspace,
            })

            try:
                reply = plugin._cmd_cancel("", {"context": context})

                self.assertTrue(str(reply).strip())
                stored = {
                    item["id"]: item
                    for item in tool.execute({"action": "list"}).result["tasks"]
                }[task["id"]]
                self.assertEqual(stored["status"], "cancelling")
                active = [row for row in registry.snapshot() if row["request_id"] == child_session_id]
                self.assertEqual(len(active), 1)
                self.assertTrue(active[0]["cancelled"])
                run = ledger.get_run(child_session_id)
                self.assertEqual(run["status"], "cancelling")
                self.assertEqual(run["phase"], "cancelling")
            finally:
                registry.unregister(child_session_id)


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

        self.assertEqual(proxy_name, "optional_abilities")
        self.assertEqual(proxy_args["action"], "install")
        self.assertEqual(proxy_args["ability"], "feishu-cli")
        self.assertNotIn("discoveryOnly", proxy_args)

    def test_agent_capability_feishu_pack_requires_find_skill_gate(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        calls = []

        def fake_feishu_cli(self, timeout, discovery_source=None, find_skill_result=None):
            calls.append(("feishu-cli", timeout, discovery_source, find_skill_result))
            raise AssertionError("feishu-lark install must not run without a find-skill gate")

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "feishu-lark",
                "timeout": 45,
            })

        self.assertEqual(result.status, "error")
        self.assertEqual(calls, [])
        self.assertTrue(result.result["discoveryOnly"])
        self.assertIn("find-skill", result.result["message"])

    def test_agent_capability_feishu_pack_rejects_unstructured_find_skill_result(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        calls = []

        def fake_feishu_cli(self, timeout, discovery_source=None, find_skill_result=None):
            calls.append(("feishu-cli", timeout, discovery_source, find_skill_result))
            raise AssertionError("unstructured find_skill_result must not reach installer")

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            for gate in ("find-skill", {"status": "success", "package": "not-related"}):
                with self.subTest(gate=gate):
                    result = AgentCapabilityTool().execute({
                        "action": "install_pack",
                        "pack_id": "feishu-lark",
                        "timeout": 45,
                        "find_skill_result": gate,
                    })
                    self.assertEqual(result.status, "error")
                    self.assertTrue(result.result["discoveryOnly"])

        self.assertEqual(calls, [])

    def test_agent_capability_feishu_pack_accepts_structured_find_skill_result(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
        from agent.tools.base_tool import ToolResult

        gate = {
            "status": "success",
            "source": "find-skill",
            "package": "@larksuite/cli",
            "url": "https://github.com/larksuite/cli",
        }
        calls = []

        def fake_feishu_cli(self, timeout, discovery_source=None, find_skill_result=None):
            calls.append(("feishu-cli", timeout, discovery_source, find_skill_result))
            return ToolResult.success({
                "status": "success",
                "available": True,
                "capabilityState": {"installed": True},
            })

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "feishu-lark",
                "timeout": 45,
                "find_skill_result": gate,
            })

        self.assertEqual(result.status, "success")
        self.assertEqual(calls, [("feishu-cli", 45, None, gate)])

    def test_agent_capability_feishu_pack_installs_structured_cli_after_find_skill_gate(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
        from agent.tools.base_tool import ToolResult

        calls = []

        def fake_feishu_cli(self, timeout, discovery_source=None, find_skill_result=None):
            calls.append(("feishu-cli", timeout, discovery_source, find_skill_result))
            return ToolResult.success({
                "status": "success",
                "available": True,
                "capabilityState": {"installed": True},
            })

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "feishu-lark",
                "timeout": 45,
                "discovery_source": "find-skill",
            })

        self.assertEqual(result.status, "success")
        self.assertEqual(calls, [("feishu-cli", 45, "find-skill", None)])
        self.assertEqual(result.result["installPlan"], ["feishu-cli"])
        self.assertTrue(result.result["steps"][0]["installed"])

    def test_agent_capability_install_pack_accepts_feishu_cli_alias(self):
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
        from agent.tools.base_tool import ToolResult

        calls = []

        def fake_feishu_cli(self, timeout, discovery_source=None, find_skill_result=None):
            calls.append(("feishu-cli", timeout, discovery_source, find_skill_result))
            return ToolResult.success({
                "status": "success",
                "available": True,
                "capabilityState": {"installed": True},
            })

        with patch.object(OptionalAbilities, "_install_feishu_cli", fake_feishu_cli):
            result = AgentCapabilityTool().execute({
                "action": "install_pack",
                "pack_id": "lark-cli",
                "timeout": 30,
                "discovery_source": "find-skill",
            })

        self.assertEqual(result.status, "success")
        self.assertEqual(calls, [("feishu-cli", 30, "find-skill", None)])
        self.assertEqual(result.result["installPlan"], ["feishu-cli"])

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
        self.assertIn("find-skill", prompt)
        self.assertIn("registry.npmmirror.com", prompt)
        self.assertIn("Do not use raw bash/curl/npm/git clone", prompt)
        self.assertFalse(result["discoveryOnly"])

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

    def test_active_request_snapshot_keeps_scheduler_run_out_of_primary_sessions(self):
        from agent.protocol import get_run_ledger
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "scheduler_task-active_1234abcd"
        session_id = "web-session-with-scheduler"
        with isolated_run_ledger():
            ledger = get_run_ledger()
            ledger.create_run(
                request_id,
                session_id,
                run_type="scheduler",
                phase="tool_call_running",
                status="running",
                metadata={"task_id": "task-active"},
            )
            with tempfile.TemporaryDirectory() as workspace:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()

            active = [item for item in snapshot["requests"] if item.get("request_id") == request_id]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["run_type"], "scheduler")
            self.assertEqual(active[0]["state"], "running")
            self.assertNotIn(session_id, snapshot["sessions"])

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

    def test_run_ledger_terminal_snapshot_reports_recent_terminal_states(self):
        from agent.protocol import reset_run_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            for status in ["completed", "failed", "cancelled", "interrupted"]:
                request_id = f"req-terminal-{status}"
                ledger.create_run(request_id, f"session-terminal-{status}", phase="running")
                ledger.mark_terminal(
                    request_id,
                    status,
                    reason=f"{status}_reason",
                    error_code=f"{status.upper()}_CODE" if status in {"failed", "interrupted"} else "",
                    error_message=f"{status} message",
                )

            terminal = ledger.terminal_snapshot(max_age_seconds=60, limit=10)
            by_status = {row["status"]: row for row in terminal}
            self.assertEqual(set(by_status), {"completed", "failed", "cancelled", "interrupted"})
            self.assertEqual(by_status["failed"]["state"], "failed")
            self.assertEqual(by_status["failed"]["terminal_reason"], "failed_reason")
            self.assertEqual(by_status["failed"]["error_code"], "FAILED_CODE")
            self.assertIsNotNone(by_status["interrupted"]["terminal_at"])
            self.assertIn("terminal_age_seconds", by_status["interrupted"])
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

    def test_active_request_snapshot_reports_active_and_recent_terminal_run_truth(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        active_statuses = ["queued", "running", "cancelling", "finalizing"]
        terminal_statuses = ["completed", "failed", "cancelled", "interrupted"]
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            for status in active_statuses:
                ledger.create_run(
                    f"req-active-{status}",
                    f"session-active-{status}",
                    phase=status,
                    status=status,
                )
            for status in terminal_statuses:
                request_id = f"req-terminal-{status}"
                ledger.create_run(request_id, f"session-terminal-{status}", phase="running")
                ledger.mark_terminal(
                    request_id,
                    status,
                    reason=f"{status}_reason",
                    error_code=f"{status.upper()}_CODE" if status in {"failed", "interrupted"} else "",
                    error_message=f"{status} message",
                )

            registry = get_cancel_registry()
            registry.register("req-terminal-failed", session_id="session-terminal-failed")
            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
            finally:
                registry.unregister("req-terminal-failed")

            active_by_id = {row["request_id"]: row for row in snapshot["requests"]}
            terminal_by_id = {
                row["request_id"]: row
                for row in snapshot["recentTerminalRequests"]
            }
            self.assertEqual(
                {row["status"] for row in snapshot["requests"] if row["request_id"].startswith("req-active-")},
                set(active_statuses),
            )
            self.assertFalse([
                row for row in snapshot["requests"]
                if str(row.get("request_id", "")).startswith("req-terminal-")
            ])
            self.assertEqual(
                {row["status"] for row in snapshot["recent_terminal_requests"] if row["request_id"].startswith("req-terminal-")},
                set(terminal_statuses),
            )
            self.assertEqual(snapshot["recentTerminalRequests"], snapshot["recent_terminal_requests"])
            for status in active_statuses:
                self.assertEqual(active_by_id[f"req-active-{status}"]["state"], status)
            for status in terminal_statuses:
                terminal = terminal_by_id[f"req-terminal-{status}"]
                self.assertEqual(terminal["state"], status)
                self.assertEqual(terminal["terminal_reason"], f"{status}_reason")
                self.assertEqual(terminal["source"], "run_ledger")
                self.assertIsNotNone(terminal["terminal_at"])
            for status in [*active_statuses, *terminal_statuses]:
                self.assertEqual(snapshot["runStatusCounts"].get(status), 1)
                self.assertEqual(snapshot["run_status_counts"].get(status), 1)

    def test_active_request_snapshot_attaches_run_center_action_policy(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run("req-message-failed", "session-message", phase="running")
            ledger.mark_terminal(
                "req-message-failed",
                "failed",
                reason="worker_exception",
                error_code="WORKER_EXCEPTION",
                error_message="worker failed",
            )
            ledger.create_run("req-invalid-failed", "session-invalid", phase="running")
            ledger.mark_terminal(
                "req-invalid-failed",
                "failed",
                reason="bad_request",
                error_code="INVALID_REQUEST",
                error_message="invalid request",
            )
            ledger.create_run("req-cancelled-terminal", "session-cancelled", phase="running")
            ledger.mark_terminal(
                "req-cancelled-terminal",
                "cancelled",
                reason="user_cancelled",
                error_code="",
                error_message="",
            )
            ledger.create_run(
                "subagent-child-1",
                "subagent-child-1",
                phase="running",
                run_type="subagent",
                metadata={"task_id": "child-1"},
            )
            ledger.mark_terminal(
                "subagent-child-1",
                "failed",
                reason="subagent_failed",
                error_code="SUBAGENT_FAILED",
                error_message="child failed",
            )
            ledger.create_run(
                "scheduler_task_1",
                "scheduler_task_1",
                phase="running",
                run_type="scheduler",
            )
            ledger.mark_terminal(
                "scheduler_task_1",
                "failed",
                reason="scheduler_failed",
                error_code="SCHEDULER_FAILED",
                error_message="scheduler failed",
            )

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                snapshot = channel.active_requests_snapshot()

        terminal = {
            row["request_id"]: row
            for row in snapshot["recentTerminalRequests"]
        }
        message = terminal["req-message-failed"]
        self.assertTrue(message["actions"]["open"])
        self.assertTrue(message["actions"]["recover"])
        self.assertTrue(message["actions"]["retry"])
        self.assertTrue(message["retryable"])
        self.assertEqual(message["retry_mode"], "manual_retry_prepare")
        self.assertEqual(message["retry_disabled_reason"], "")
        self.assertFalse(message["actions"]["stop"])

        invalid = terminal["req-invalid-failed"]
        self.assertTrue(invalid["actions"]["open"])
        self.assertTrue(invalid["actions"]["recover"])
        self.assertFalse(invalid["actions"]["retry"])
        self.assertFalse(invalid["retryable"])
        self.assertEqual(invalid["retry_disabled_reason"], "non_retryable_terminal")
        self.assertFalse(invalid["actions"]["stop"])

        cancelled = terminal["req-cancelled-terminal"]
        self.assertTrue(cancelled["actions"]["open"])
        self.assertTrue(cancelled["actions"]["recover"])
        self.assertFalse(cancelled["actions"]["retry"])
        self.assertFalse(cancelled["retryable"])
        self.assertEqual(cancelled["retry_disabled_reason"], "not_failed")
        self.assertFalse(cancelled["actions"]["stop"])

        subagent = terminal["subagent-child-1"]
        self.assertFalse(subagent["actions"]["open"])
        self.assertFalse(subagent["actions"]["recover"])
        self.assertFalse(subagent["actions"]["retry"])
        self.assertFalse(subagent["retryable"])
        self.assertEqual(subagent["retry_disabled_reason"], "subagent_replay_unavailable")

        scheduler = terminal["scheduler_task_1"]
        self.assertFalse(scheduler["actions"]["open"])
        self.assertFalse(scheduler["actions"]["recover"])
        self.assertFalse(scheduler["actions"]["retry"])
        self.assertFalse(scheduler["retryable"])
        self.assertEqual(scheduler["retry_disabled_reason"], "scheduler_replay_unavailable")

    def test_active_request_snapshot_suppresses_registry_fallback_for_old_terminal_run(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        terminal_statuses = ["completed", "failed", "cancelled", "interrupted"]
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            registry = get_cancel_registry()
            request_ids = []
            with patch("agent.protocol.run_ledger.time.time", return_value=1000.0):
                for status in terminal_statuses:
                    request_id = f"req-old-terminal-{status}"
                    session_id = f"session-old-terminal-{status}"
                    request_ids.append(request_id)
                    ledger.create_run(request_id, session_id, phase="running")
                    ledger.mark_terminal(
                        request_id,
                        status,
                        reason=f"old_{status}",
                        error_code=f"OLD_{status.upper()}",
                        error_message=f"old {status}",
                    )
                    registry.register(request_id, session_id=session_id)
                    if status == "cancelled":
                        self.assertTrue(registry.cancel_request(request_id))
            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
            finally:
                for request_id in request_ids:
                    registry.unregister(request_id)

            active_ids = {row.get("request_id") for row in snapshot["requests"]}
            terminal_ids = {row.get("request_id") for row in snapshot["recentTerminalRequests"]}
            self.assertTrue(set(request_ids).isdisjoint(active_ids))
            self.assertTrue(set(request_ids).isdisjoint(terminal_ids))
            for status in terminal_statuses:
                final = ledger.get_run(f"req-old-terminal-{status}")
                self.assertEqual(final["status"], status)
                self.assertEqual(final["terminal_reason"], f"old_{status}")

    def test_active_request_snapshot_keeps_recent_cancelled_terminal_visible_while_stopping(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-recent-cancelled-still-stopping"
        session_id = "session-recent-cancelled-still-stopping"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="running")
            ledger.mark_terminal(request_id, "cancelled", reason="cancelled")
            registry = get_cancel_registry()
            registry.register(request_id, session_id=session_id)
            try:
                self.assertTrue(registry.cancel_request(request_id))
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
            finally:
                registry.unregister(request_id)

            active = [row for row in snapshot["requests"] if row.get("request_id") == request_id]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["source"], "cancel_registry")
            self.assertTrue(active[0]["cancelled"])
            self.assertEqual(active[0]["state"], "cancelling")
            terminal = [row for row in snapshot["recentTerminalRequests"] if row.get("request_id") == request_id]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["status"], "cancelled")
            self.assertEqual(snapshot["runStatusCounts"].get("cancelled"), 1)
            self.assertEqual(snapshot["runStatusCounts"].get("cancelling"), 1)

    def test_active_request_snapshot_marks_dead_lock_message_run_interrupted(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        channel = web_channel.WebChannel()
        request_id = "req-sidecar-interrupted"
        session_id = "session-sidecar-interrupted"
        registry = get_cancel_registry()
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="tool_running", status="running")
            registry.register(request_id, session_id=session_id)
            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "createdAt": 1,
                }),
                encoding="utf-8",
            )

            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
                    second_snapshot = channel.active_requests_snapshot()
            finally:
                registry.unregister(request_id)

            self.assertEqual(snapshot["status"], "success")
            self.assertFalse([item for item in snapshot["requests"] if item.get("request_id") == request_id])
            self.assertFalse([item for item in second_snapshot["requests"] if item.get("request_id") == request_id])
            self.assertEqual(len(snapshot["staleLocks"]), 1)
            self.assertTrue(snapshot["staleLocks"][0]["removed"])
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "interrupted")
            self.assertEqual(final["phase"], "interrupted")
            self.assertEqual(final["terminal_reason"], "sidecar_interrupted")
            self.assertEqual(final["error_code"], "SIDECAR_INTERRUPTED")
            terminal_at = final["terminal_at"]
            self.assertIsNotNone(terminal_at)
            self.assertEqual(ledger.active_snapshot(), [])
            self.assertEqual(ledger.get_run(request_id)["terminal_at"], terminal_at)

    def test_active_request_snapshot_keeps_subagent_run_active_after_dead_message_lock(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        channel = web_channel.WebChannel()
        request_id = "subagent-sidecar-interrupted"
        session_id = "session-sidecar-subagent"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(
                request_id,
                session_id,
                run_type="subagent",
                phase="queued",
                status="queued",
                metadata={"task_id": request_id},
            )
            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "createdAt": 1,
                }),
                encoding="utf-8",
            )

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                snapshot = channel.active_requests_snapshot()

            active = [item for item in snapshot["requests"] if item.get("request_id") == request_id]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["run_type"], "subagent")
            self.assertEqual(active[0]["status"], "queued")
            self.assertEqual(active[0]["phase"], "queued")
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "queued")
            self.assertIsNone(final["terminal_at"])

    def test_active_request_snapshot_interrupts_pre_boot_subagent_and_scheduler_runs_without_tokens(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        subagent_request_id = "subagent-pre-boot-orphan"
        subagent_task_id = "pre-boot-subagent"
        scheduler_request_id = "scheduler_pre_boot_orphan"
        registry = get_cancel_registry()
        registry.unregister(subagent_request_id)
        registry.unregister(scheduler_request_id)

        with tempfile.TemporaryDirectory() as workspace:
            subagent_state_path = Path(workspace) / ".ecorex" / "subagents.json"
            subagent_state_path.parent.mkdir(parents=True, exist_ok=True)
            subagent_state_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "tasks": {
                        subagent_task_id: {
                            "id": subagent_task_id,
                            "status": "running",
                            "childSessionId": subagent_request_id,
                            "requestId": subagent_request_id,
                            "parentSessionId": "parent-pre-boot",
                        },
                    },
                }),
                encoding="utf-8",
            )
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(
                subagent_request_id,
                subagent_request_id,
                run_type="subagent",
                phase="running",
                status="running",
                metadata={"task_id": subagent_task_id},
            )
            ledger.create_run(
                scheduler_request_id,
                "scheduler_session_pre_boot",
                run_type="scheduler",
                phase="tool_call_running",
                status="running",
                metadata={"task_id": "pre-boot-scheduler"},
            )
            with patch.object(channel, "runtime_started_at", time.time() + 10):
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    snapshot = channel.active_requests_snapshot()
                    subagent_first_final = ledger.get_run(subagent_request_id)
                    scheduler_first_final = ledger.get_run(scheduler_request_id)
                    second_snapshot = channel.active_requests_snapshot()

            active_ids = {item.get("request_id") for item in snapshot["requests"]}
            self.assertNotIn(subagent_request_id, active_ids)
            self.assertNotIn(scheduler_request_id, active_ids)
            second_active_ids = {item.get("request_id") for item in second_snapshot["requests"]}
            self.assertNotIn(subagent_request_id, second_active_ids)
            self.assertNotIn(scheduler_request_id, second_active_ids)

            self.assertEqual(subagent_first_final["status"], "interrupted")
            self.assertEqual(subagent_first_final["terminal_reason"], "subagent_sidecar_interrupted")
            self.assertEqual(subagent_first_final["error_code"], "SUBAGENT_SIDECAR_INTERRUPTED")
            subagent_terminal_at = subagent_first_final["terminal_at"]
            self.assertIsNotNone(subagent_terminal_at)
            subagent_state = json.loads(subagent_state_path.read_text(encoding="utf-8"))
            subagent_task = subagent_state["tasks"][subagent_task_id]
            self.assertEqual(subagent_task["status"], "interrupted")
            self.assertEqual(subagent_task["terminalReason"], "subagent_sidecar_interrupted")
            self.assertEqual(subagent_task["errorCode"], "SUBAGENT_SIDECAR_INTERRUPTED")
            self.assertIsNotNone(subagent_task["completedAt"])
            from agent.tools.subagent.subagent import SubagentTool

            tool = SubagentTool()
            tool.context = types.SimpleNamespace(_current_session_id="parent-after-restart", workspace_dir=workspace)
            with patch("agent.tools.subagent.subagent.threading.Thread.start", lambda _thread: None):
                for index in range(6):
                    result = tool.execute({"action": "start", "task": f"replacement child {index}"})
                    self.assertEqual(result.status, "success")
                blocked = tool.execute({"action": "start", "task": "one too many replacements"})
            self.assertEqual(blocked.status, "error")
            self.assertEqual(blocked.result["code"], "SUBAGENT_CONCURRENCY_LIMIT")

            self.assertEqual(scheduler_first_final["status"], "interrupted")
            self.assertEqual(scheduler_first_final["terminal_reason"], "scheduler_sidecar_interrupted")
            self.assertEqual(scheduler_first_final["error_code"], "SCHEDULER_SIDECAR_INTERRUPTED")
            scheduler_terminal_at = scheduler_first_final["terminal_at"]
            self.assertIsNotNone(scheduler_terminal_at)
            self.assertEqual(ledger.get_run(subagent_request_id)["terminal_at"], subagent_terminal_at)
            self.assertEqual(ledger.get_run(scheduler_request_id)["terminal_at"], scheduler_terminal_at)

    def test_active_request_snapshot_keeps_pre_boot_non_message_runs_with_cancel_tokens(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        scheduler_request_id = "scheduler-current-token-survives-pre-boot-check"
        scheduler_session_id = "scheduler_session_current_token"
        subagent_request_id = "subagent-current-token-survives-pre-boot-check"
        subagent_task_id = "subagent-current-token"
        subagent_session_id = "subagent_session_current_token"
        registry = get_cancel_registry()
        registry.unregister(scheduler_request_id)
        registry.unregister(subagent_request_id)

        with tempfile.TemporaryDirectory() as workspace:
            subagent_state_path = Path(workspace) / ".ecorex" / "subagents.json"
            subagent_state_path.parent.mkdir(parents=True, exist_ok=True)
            subagent_state_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "tasks": {
                        subagent_task_id: {
                            "id": subagent_task_id,
                            "status": "queued",
                            "childSessionId": subagent_request_id,
                            "requestId": subagent_request_id,
                            "parentSessionId": "parent-current-token",
                        },
                    },
                }),
                encoding="utf-8",
            )
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(
                scheduler_request_id,
                scheduler_session_id,
                run_type="scheduler",
                phase="agent_task_running",
                status="running",
            )
            ledger.create_run(
                subagent_request_id,
                subagent_session_id,
                run_type="subagent",
                phase="queued",
                status="queued",
                metadata={"task_id": subagent_task_id},
            )
            registry.register(scheduler_request_id, session_id=scheduler_session_id)
            registry.register(subagent_request_id, session_id=subagent_session_id)
            try:
                with patch.object(channel, "runtime_started_at", time.time() + 10):
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        snapshot = channel.active_requests_snapshot()
            finally:
                registry.unregister(scheduler_request_id)
                registry.unregister(subagent_request_id)

            active_by_id = {item.get("request_id"): item for item in snapshot["requests"]}
            scheduler_active = active_by_id.get(scheduler_request_id)
            self.assertIsNotNone(scheduler_active)
            self.assertEqual(scheduler_active["run_type"], "scheduler")
            self.assertEqual(scheduler_active["status"], "running")
            scheduler_final = ledger.get_run(scheduler_request_id)
            self.assertEqual(scheduler_final["status"], "running")
            self.assertIsNone(scheduler_final["terminal_at"])

            subagent_active = active_by_id.get(subagent_request_id)
            self.assertIsNotNone(subagent_active)
            self.assertEqual(subagent_active["run_type"], "subagent")
            self.assertEqual(subagent_active["status"], "queued")
            subagent_final = ledger.get_run(subagent_request_id)
            self.assertEqual(subagent_final["status"], "queued")
            self.assertIsNone(subagent_final["terminal_at"])
            subagent_state = json.loads(subagent_state_path.read_text(encoding="utf-8"))
            self.assertEqual(subagent_state["tasks"][subagent_task_id]["status"], "queued")

    def test_active_request_snapshot_does_not_interrupt_stale_live_message_lock(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import LOCK_STALE_SECONDS, SessionLock

        channel = web_channel.WebChannel()
        request_id = "req-stale-live-owner"
        session_id = "session-stale-live-owner"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="tool_running", status="running")
            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "createdAt": int(time.time()) - LOCK_STALE_SECONDS - 10,
                }),
                encoding="utf-8",
            )
            stale_mtime = int(time.time()) - LOCK_STALE_SECONDS - 10
            os.utime(lock.path, (stale_mtime, stale_mtime))

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                snapshot = channel.active_requests_snapshot()

            active = [item for item in snapshot["requests"] if item.get("request_id") == request_id]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["status"], "running")
            stale = [item for item in snapshot["staleLocks"] if item.get("session_id") == session_id]
            self.assertEqual(len(stale), 1)
            self.assertTrue(stale[0]["stale"])
            self.assertTrue(stale[0]["alive"])
            self.assertFalse(stale[0]["dead_owner"])
            self.assertFalse(stale[0]["removed"])
            self.assertTrue(lock.path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "running")
            self.assertIsNone(final["terminal_at"])

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

    def test_scheduler_request_token_survives_agentbridge_for_scheduler_owner(self):
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

        request_id = "req-scheduler-token-owner"
        session_id = "scheduler_session-token-owner_task"
        registry = get_cancel_registry()
        original_event = registry.register(request_id, session_id=session_id)
        fake_agent = FakeAgent()
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: fake_agent
        bridge._trim_in_memory_to_turns = lambda *args, **kwargs: None
        bridge._pre_persist_user_message = lambda *args, **kwargs: False
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "scheduler"
        context["is_scheduled_task"] = True
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.TEXT)
            self.assertIs(fake_agent.cancel_event, original_event)
            self.assertIs(registry.get_event(request_id), original_event)
        finally:
            registry.unregister(request_id)

    def test_subagent_request_token_survives_agentbridge_for_subagent_owner(self):
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

        request_id = "subagent-agentbridge-token-owner"
        session_id = request_id
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
        context["cancel_token_owner"] = "subagent"
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.TEXT)
            self.assertIs(fake_agent.cancel_event, original_event)
            self.assertIs(registry.get_event(request_id), original_event)
        finally:
            registry.unregister(request_id)

    def test_external_owner_without_preexisting_token_is_cleaned_by_agentbridge(self):
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

        request_id = "req-external-owner-fallback"
        session_id = "scheduler_session-owner-fallback_task"
        registry = get_cancel_registry()
        registry.unregister(request_id)
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: FakeAgent()
        bridge._trim_in_memory_to_turns = lambda *args, **kwargs: None
        bridge._pre_persist_user_message = lambda *args, **kwargs: False
        bridge._persist_messages = lambda *args, **kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "scheduler"
        try:
            bridge.agent_reply("hello", context=context)

            self.assertIsNone(registry.get_event(request_id))
        finally:
            registry.unregister(request_id)

    def test_agentbridge_owned_token_is_cleaned_when_agent_init_fails(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType

        request_id = "req-agentbridge-init-failed"
        session_id = "session-agentbridge-init-failed"
        registry = get_cancel_registry()
        registry.unregister(request_id)
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.ERROR)
            self.assertIsNone(registry.get_event(request_id))
        finally:
            registry.unregister(request_id)

    def test_external_owner_fallback_token_is_cleaned_when_agent_init_fails(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType

        request_id = "req-external-owner-init-failed"
        session_id = "scheduler_session-init-failed_task"
        registry = get_cancel_registry()
        registry.unregister(request_id)
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "scheduler"
        try:
            reply = bridge.agent_reply("hello", context=context)

            self.assertEqual(reply.type, ReplyType.ERROR)
            self.assertIsNone(registry.get_event(request_id))
        finally:
            registry.unregister(request_id)

    def test_preexisting_external_owner_token_survives_agent_init_failure(self):
        from agent.protocol import get_cancel_registry
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType

        request_id = "req-preexisting-owner-init-failed"
        session_id = "scheduler_session-preexisting-init-failed_task"
        registry = get_cancel_registry()
        original_event = registry.register(request_id, session_id=session_id)
        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: None
        context = Context(ContextType.TEXT, "hello")
        context["session_id"] = session_id
        context["request_id"] = request_id
        context["cancel_token_owner"] = "scheduler"
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
                    self.assertEqual(result["same_session"]["policy"], "interrupt_previous")
                    self.assertEqual(result["same_session"]["queue"], "disabled")
                    self.assertEqual(result["same_session"]["decision"], "replacement_accepted")
                    self.assertEqual(result["same_session"]["replaced_request_ids"], [old_request_id])
                    self.assertEqual(result["same_session"]["cancelled_requests"], 1)
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
                    self.assertEqual(result["same_session"]["policy"], "interrupt_previous")
                    self.assertEqual(result["same_session"]["queue"], "disabled")
                    self.assertEqual(result["same_session"]["decision"], "retryable_conflict")
                    self.assertEqual(result["same_session"]["active_request_ids"], [old_request_id])
                    self.assertGreaterEqual(result["same_session"]["retry_after_ms"], 1000)
                    self.assertIn("retry", result["message"].lower())
                finally:
                    old_lock.release()
                    registry.unregister(old_request_id)

    def test_same_session_active_request_is_not_ignored_without_interrupt(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            session_id = "session-free-lock-active-token"
            old_request_id = "req-free-lock-active-token"
            lock_path = SessionLock(workspace, session_id).path
            registry = get_cancel_registry()
            registry.register(old_request_id, session_id=session_id)
            channel.request_to_session = {old_request_id: session_id}
            ledger.create_run(old_request_id, session_id, phase="running", status="running")
            payload = {
                "session_id": session_id,
                "message": "must not start second writer",
                "stream": True,
                "lang": "en",
            }
            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(channel, "BACKPRESSURE_GLOBAL_ACTIVE_LIMIT", 99):
                        with patch.object(channel, "_generate_request_id") as generate_request_id:
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "REQUEST_CONFLICT_RETRYABLE")
                self.assertEqual(result["error_type"], "concurrency_conflict")
                self.assertEqual(result["state"], "retryable_conflict")
                self.assertEqual(result["reason"], "same_session_active_request")
                self.assertEqual(result["active_request_ids"], [old_request_id])
                self.assertEqual(result["same_session"]["policy"], "interrupt_previous")
                self.assertEqual(result["same_session"]["queue"], "disabled")
                self.assertEqual(result["same_session"]["decision"], "retryable_conflict")
                self.assertEqual(result["same_session"]["active_request_ids"], [old_request_id])
                generate_request_id.assert_not_called()
                self.assertFalse(lock_path.exists())
            finally:
                registry.unregister(old_request_id)

    def test_superseded_same_session_replacement_waiter_does_not_queue(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel
        from common.ecorex_workspace import SessionBusyError, SessionLock

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            session_id = "session-superseded-replacement"
            old_request_id = "req-superseded-old"
            registry = get_cancel_registry()

            with tempfile.TemporaryDirectory() as workspace:
                old_lock = SessionLock(workspace, session_id).acquire()
                old_event = registry.register(old_request_id, session_id=session_id)
                channel.request_to_session = {old_request_id: session_id}
                results = {}
                errors = {}
                results_lock = threading.Lock()

                def run_waiter(name, ticket):
                    try:
                        result = channel._interrupt_and_wait_for_session_lock(
                            session_id,
                            lang="en",
                            replacement_ticket=ticket,
                        )
                        with results_lock:
                            results[name] = result
                    except Exception as e:
                        with results_lock:
                            errors[name] = e

                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        first_ticket = channel._begin_same_session_replacement(session_id)
                        first = threading.Thread(
                            target=run_waiter,
                            args=("first", first_ticket),
                            daemon=True,
                        )
                        first.start()
                        self.assertTrue(old_event.wait(timeout=2))

                        second_ticket = channel._begin_same_session_replacement(session_id)
                        second = threading.Thread(
                            target=run_waiter,
                            args=("second", second_ticket),
                            daemon=True,
                        )
                        second.start()
                        time.sleep(0.2)
                        old_lock.release()
                        old_lock = None

                        first.join(timeout=3)
                        second.join(timeout=3)

                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertIsInstance(errors.get("first"), SessionBusyError)
                    self.assertIn("same_session_replacement_superseded", str(errors["first"]))
                    self.assertNotIn("first", results)
                    self.assertIn("second", results)
                    self.assertEqual(results["second"]["same_session"]["decision"], "replacement_accepted")
                    self.assertEqual(results["second"]["same_session"]["replaced_request_ids"], [old_request_id])
                    self.assertEqual(results["second"]["same_session"]["cancelled_requests"], 1)
                    results["second"]["lock"].release()
                finally:
                    if old_lock:
                        old_lock.release()
                    registry.unregister(old_request_id)

    def test_direct_admission_ticket_supersedes_waiting_replacement(self):
        from agent.protocol import get_cancel_registry
        from channel.web import web_channel
        from common.ecorex_workspace import SessionBusyError, SessionLock

        with isolated_run_ledger():
            channel = web_channel.WebChannel()
            session_id = "session-direct-ticket-supersedes"
            old_request_id = "req-direct-ticket-old"
            registry = get_cancel_registry()

            with tempfile.TemporaryDirectory() as workspace:
                old_lock = SessionLock(workspace, session_id).acquire()
                old_event = registry.register(old_request_id, session_id=session_id)
                channel.request_to_session = {old_request_id: session_id}
                results = {}
                errors = {}
                results_lock = threading.Lock()

                def run_waiter(ticket):
                    try:
                        result = channel._interrupt_and_wait_for_session_lock(
                            session_id,
                            lang="en",
                            replacement_ticket=ticket,
                        )
                        with results_lock:
                            results["waiter"] = result
                    except Exception as e:
                        with results_lock:
                            errors["waiter"] = e

                try:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        waiter_ticket = channel._begin_same_session_replacement(session_id)
                        waiter = threading.Thread(target=run_waiter, args=(waiter_ticket,), daemon=True)
                        waiter.start()
                        self.assertTrue(old_event.wait(timeout=2))

                        direct_ticket = channel._begin_same_session_replacement(session_id)
                        self.assertGreater(direct_ticket, waiter_ticket)

                        old_lock.release()
                        old_lock = None
                        waiter.join(timeout=3)

                    self.assertFalse(waiter.is_alive())
                    self.assertIsInstance(errors.get("waiter"), SessionBusyError)
                    self.assertIn("same_session_replacement_superseded", str(errors["waiter"]))
                    self.assertNotIn("waiter", results)
                finally:
                    if old_lock:
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

    def test_post_message_success_persists_required_run_ledger_fields(self):
        from agent.protocol import get_cancel_registry, reset_run_ledger_for_tests
        from bridge.context import Context
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-message-ledger-required-fields"
            session_id = "session-message-ledger-required-fields"
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            channel = web_channel.WebChannel()
            produced = threading.Event()
            payload = {
                "session_id": session_id,
                "message": "persist my run row",
                "stream": False,
                "internal_action": True,
                "attachments": [{"file_path": "C:/tmp/a.txt", "file_type": "file"}],
            }

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            def fake_produce(context):
                lock = context.get("session_lock")
                if lock:
                    lock.release()
                produced.set()

            try:
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(channel, "_generate_request_id", return_value=request_id):
                        with patch.object(channel, "_compose_context", side_effect=fake_compose_context):
                            with patch.object(channel, "produce", side_effect=fake_produce):
                                with patch.object(
                                    web_channel.web,
                                    "data",
                                    return_value=json.dumps(payload).encode("utf-8"),
                                ):
                                    result = json.loads(channel.post_message())

                self.assertEqual(result["status"], "success")
                self.assertEqual(result["request_id"], request_id)
                self.assertEqual(result["same_session"]["policy"], "interrupt_previous")
                self.assertEqual(result["same_session"]["queue"], "disabled")
                self.assertEqual(result["same_session"]["decision"], "accepted")
                self.assertEqual(result["same_session"]["active_request_ids"], [])
                self.assertEqual(result["same_session"]["replaced_request_ids"], [])
                self.assertEqual(result["same_session"]["cancelled_requests"], 0)
                self.assertEqual(result["same_session"]["retry_after_ms"], 0)
                self.assertEqual(channel.same_session_replacement_tickets.get(session_id), 1)
                self.assertTrue(produced.wait(timeout=2))

                row = ledger.get_run(request_id)
                self.assertIsNotNone(row)
                self.assertEqual(row["request_id"], request_id)
                self.assertEqual(row["session_id"], session_id)
                self.assertEqual(row["run_type"], "message")
                self.assertEqual(row["status"], "running")
                self.assertEqual(row["phase"], "accepted")
                self.assertIsNotNone(row["created_at"])
                self.assertIsNotNone(row["started_at"])
                self.assertIsNotNone(row["updated_at"])
                self.assertIsNone(row["terminal_at"])
                self.assertEqual(row["metadata"]["stream"], False)
                self.assertEqual(row["metadata"]["internal_action"], True)
                self.assertEqual(row["metadata"]["attachments"], 1)
            finally:
                get_cancel_registry().unregister(request_id)
                reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")

    def test_post_message_rejects_when_run_ledger_create_fails(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        class FailingLedger:
            def create_run(self, *args, **kwargs):
                raise RuntimeError("sqlite is locked")

            def get_run(self, request_id):
                return None

            def mark_terminal(self, *args, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-message-ledger-create-fails"
            session_id = "session-message-ledger-create-fails"
            channel = web_channel.WebChannel()
            payload = {
                "session_id": session_id,
                "message": "do not start without durable run state",
                "stream": True,
            }

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            lock_path = SessionLock(workspace, session_id).path
            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch("agent.protocol.get_run_ledger", return_value=FailingLedger()):
                    with patch.object(channel, "_generate_request_id", return_value=request_id):
                        with patch.object(channel, "_compose_context", side_effect=fake_compose_context) as compose_context:
                            with patch.object(channel, "produce") as produce:
                                with patch.object(
                                    web_channel.web,
                                    "data",
                                    return_value=json.dumps(payload).encode("utf-8"),
                                ):
                                    result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["code"], "RUN_LEDGER_UNAVAILABLE")
            self.assertEqual(result["error_type"], "runtime_state_unavailable")
            self.assertTrue(result["retryable"])
            self.assertTrue(result["recoverable"])
            self.assertEqual(result["request_id"], "")
            compose_context.assert_not_called()
            produce.assert_not_called()
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertNotIn(request_id, channel.request_to_session)
            self.assertFalse(channel._sse_request_exists(request_id))
            self.assertFalse(lock_path.exists())

    def test_post_message_rejects_when_run_ledger_does_not_persist_row(self):
        from agent.protocol import get_cancel_registry
        from bridge.context import Context
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        class NonPersistingLedger:
            def create_run(self, *args, **kwargs):
                return False

            def mark_terminal(self, *args, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as workspace:
            request_id = "req-message-ledger-not-persisted"
            session_id = "session-message-ledger-not-persisted"
            channel = web_channel.WebChannel()
            payload = {
                "session_id": session_id,
                "message": "do not start without a persisted row",
                "stream": True,
            }

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            lock_path = SessionLock(workspace, session_id).path
            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch("agent.protocol.get_run_ledger", return_value=NonPersistingLedger()):
                    with patch.object(channel, "_generate_request_id", return_value=request_id):
                        with patch.object(channel, "_compose_context", side_effect=fake_compose_context) as compose_context:
                            with patch.object(channel, "produce") as produce:
                                with patch.object(
                                    web_channel.web,
                                    "data",
                                    return_value=json.dumps(payload).encode("utf-8"),
                                ):
                                    result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["code"], "RUN_LEDGER_UNAVAILABLE")
            self.assertEqual(result["request_id"], "")
            compose_context.assert_not_called()
            produce.assert_not_called()
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertNotIn(request_id, channel.request_to_session)
            self.assertFalse(channel._sse_request_exists(request_id))
            self.assertFalse(lock_path.exists())

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
        self.assertTrue(channel.sse_queues[request_id].empty())

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
                self.assertTrue(channel.sse_queues[request_id].empty())
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
            with self.assertRaises(StopIteration):
                next(resumed)

        self.assertIn(b"id: 9", gap_chunk)
        self.assertIn(b'"type": "replay_gap"', gap_chunk)
        self.assertIn(b'"event_type": "stream.replay_gap"', gap_chunk)
        self.assertIn(b'"terminal": true', gap_chunk)
        self.assertIn(b'"terminal_reason": "replay_gap"', gap_chunk)
        self.assertIn(b'"recoverable": true', gap_chunk)
        self.assertTrue(channel._sse_request_exists(request_id))
        resumed.close()

    def test_sse_terminal_normalization_overrides_conflicting_legacy_fields(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        cases = [
            ("done", "run.completed", "completed"),
            ("error", "run.failed", "failed"),
            ("cancelled", "run.cancelled", "cancelled"),
            ("interrupted", "run.interrupted", "interrupted"),
            ("replay_gap", "stream.replay_gap", "recovering"),
        ]

        for event_type, expected_event_type, expected_state in cases:
            with self.subTest(event_type=event_type):
                event = channel._normalize_sse_event("req-terminal-contract", {
                    "type": event_type,
                    "event_type": "run.failed" if event_type != "error" else "run.completed",
                    "state": "failed" if expected_state != "failed" else "completed",
                    "terminal": False,
                })
                self.assertEqual(event["event_type"], expected_event_type)
                self.assertEqual(event["state"], expected_state)
                self.assertTrue(event["terminal"])
                self.assertEqual(event["protocol_version"], channel.SSE_PROTOCOL_VERSION)

    def test_stream_response_emits_interrupted_terminal_for_lost_sidecar_run(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel
        from common.ecorex_workspace import SessionLock

        channel = web_channel.WebChannel()
        request_id = "req-test-sidecar-stream-interrupted"
        session_id = "session-test-sidecar-stream-interrupted"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="tool_running", status="running")
            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": 999999999,
                    "host": socket.gethostname(),
                    "createdAt": 1,
                }),
                encoding="utf-8",
            )

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                stream = channel.stream_response(request_id)
                chunk = next(stream)
                second_stream = channel.stream_response(request_id)
                second_chunk = next(second_stream)

            self.assertIn(b"id: 0", chunk)
            self.assertIn(b'"type": "interrupted"', chunk)
            self.assertIn(b'"event_type": "run.interrupted"', chunk)
            self.assertIn(b'"state": "interrupted"', chunk)
            self.assertIn(b'"terminal": true', chunk)
            self.assertIn(b'"terminal_reason": "sidecar_interrupted"', chunk)
            self.assertIn(b'"error_code": "SIDECAR_INTERRUPTED"', chunk)
            self.assertIn(request_id.encode("utf-8"), chunk)
            self.assertIn(session_id.encode("utf-8"), chunk)
            self.assertIn(b'"type": "interrupted"', second_chunk)
            self.assertFalse(lock.path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "interrupted")
            self.assertEqual(final["phase"], "interrupted")
            self.assertEqual(final["terminal_reason"], "sidecar_interrupted")
            self.assertEqual(final["error_code"], "SIDECAR_INTERRUPTED")

    def test_sse_reconnect_treats_interrupted_replay_event_as_terminal(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-sse-interrupted-terminal"
        channel.request_to_session = {request_id: "session-test-sse-interrupted-terminal"}
        channel._ensure_sse_state(request_id)
        channel._push_sse_event(request_id, {
            "type": "interrupted",
            "request_id": request_id,
            "terminal_reason": "sidecar_interrupted",
        })

        with patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(last_event_id="0")):
            resumed = channel.stream_response(request_id)
            keepalive = next(resumed)
            resumed.close()

        self.assertTrue(keepalive.startswith(b": keepalive"))
        self.assertFalse(channel._sse_request_exists(request_id))

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

    def test_sse_error_retry_metadata_is_terminal_and_manual(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-test-error-retry-meta"
        channel.request_to_session = {request_id: "session-test-error-retry-meta"}
        channel._ensure_sse_state(request_id)

        pushed = channel._push_error_event_once(
            request_id,
            "network interrupted",
            error_code="MODEL_RETRY_SUPPRESSED",
            terminal_reason="model_retry_suppressed_stream_output_started",
            extra={
                "retryable": True,
                "recoverable": True,
                "retry_suppressed": True,
                "retry_suppressed_reason": "stream_output_started",
                "retry_attempt": 0,
                "max_retries": 1,
                "retry_mode": "auto_retry",
            },
        )

        self.assertTrue(pushed)
        self.assertEqual(len(channel.sse_events[request_id]), 1)
        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["event_type"], "run.failed")
        self.assertEqual(event["state"], "failed")
        self.assertTrue(event["terminal"])
        self.assertEqual(event["terminal_reason"], "model_retry_suppressed_stream_output_started")
        self.assertEqual(event["error_code"], "MODEL_RETRY_SUPPRESSED")
        self.assertTrue(event["retryable"])
        self.assertTrue(event["recoverable"])
        self.assertTrue(event["retry_suppressed"])
        self.assertEqual(event["retry_suppressed_reason"], "stream_output_started")
        self.assertEqual(event["retry_mode"], "manual_retry_prepare")

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

    def test_tool_end_sse_reports_bounded_stdout_stderr(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-tool-output-budget"
        channel.request_to_session = {request_id: "session-tool-output-budget"}
        channel._ensure_sse_state(request_id)

        with patch.object(channel, "TOOL_OUTPUT_FIELD_CHAR_LIMIT", 24):
            with patch.object(channel, "TOOL_RESULT_PREVIEW_CHAR_LIMIT", 500):
                callback = channel._make_sse_callback(request_id)
                callback({
                    "type": "tool_execution_end",
                    "data": {
                        "tool_name": "bash",
                        "tool_call_id": "tool-output-budget",
                        "status": "success",
                        "result": {
                            "stdout": "A" * 80,
                            "stderr": "B" * 80,
                            "output": "C" * 80,
                            "stdoutTail": "D" * 80,
                            "exit_code": 0,
                        },
                        "execution_time": 1.25,
                    },
                })

        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "tool_end")
        self.assertEqual(event["limit_code"], "TOOL_OUTPUT_LIMIT")
        self.assertEqual(event["limit_reason"], "tool_output_limit")
        self.assertEqual(event["error_type"], "tool_output_limit")
        self.assertTrue(event["result_truncated"])
        self.assertTrue(event["recoverable"])
        self.assertEqual(event["tool_output_limits"]["output_field_chars"], 24)
        self.assertEqual(
            {field["field"] for field in event["truncated_output_fields"]},
            {"stdout", "stderr", "output", "stdoutTail"},
        )
        self.assertIn("[truncated 56 chars; limit 24]", event["result"])
        self.assertNotIn("A" * 80, event["result"])
        self.assertNotIn("B" * 80, event["result"])
        self.assertNotIn("C" * 80, event["result"])
        self.assertNotIn("D" * 80, event["result"])

    def test_tool_end_sse_caps_collection_before_preview(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-tool-collection-budget"
        channel.request_to_session = {request_id: "session-tool-collection-budget"}
        channel._ensure_sse_state(request_id)

        with patch.object(channel, "TOOL_OUTPUT_COLLECTION_ITEM_LIMIT", 2):
            with patch.object(channel, "TOOL_RESULT_PREVIEW_CHAR_LIMIT", 1000):
                callback = channel._make_sse_callback(request_id)
                callback({
                    "type": "tool_execution_end",
                    "data": {
                        "tool_name": "remote_tool",
                        "tool_call_id": "tool-collection-budget",
                        "status": "success",
                        "result": {"items": [{"value": index} for index in range(6)]},
                        "execution_time": 0.1,
                    },
                })

        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "tool_end")
        self.assertEqual(event["limit_code"], "TOOL_OUTPUT_LIMIT")
        self.assertEqual(event["error_type"], "tool_output_limit")
        self.assertTrue(event["result_truncated"])
        self.assertEqual(event["tool_output_limits"]["collection_items"], 2)
        self.assertIn('"__omitted_items": 4', event["result"])
        self.assertIn("items", {field["field"] for field in event["truncated_output_fields"]})

    def test_tool_output_budget_does_not_materialize_full_dict_before_cap(self):
        from channel.web import web_channel

        class LazyHugeDict(dict):
            def __len__(self):
                return 100

            def items(self):
                for index in range(4):
                    if index > 2:
                        raise AssertionError("budget serializer iterated past the cap sentinel")
                    yield f"key{index}", f"value{index}"

        channel = web_channel.WebChannel()
        with patch.object(channel, "TOOL_OUTPUT_COLLECTION_ITEM_LIMIT", 2):
            result_str, meta = channel._bounded_tool_result_for_sse(LazyHugeDict())

        self.assertEqual(meta["limit_code"], "TOOL_OUTPUT_LIMIT")
        self.assertIn('"__omitted_keys": 98', result_str)
        self.assertTrue(any(
            field.get("field") == "result" and field.get("omitted_items") == 98
            for field in meta["truncated_output_fields"]
        ))

    def test_tool_end_sse_serializes_non_json_nested_results(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-tool-non-json-budget"
        channel.request_to_session = {request_id: "session-tool-non-json-budget"}
        channel._ensure_sse_state(request_id)

        with patch.object(channel, "TOOL_OUTPUT_FIELD_CHAR_LIMIT", 6):
            callback = channel._make_sse_callback(request_id)
            callback({
                "type": "tool_execution_end",
                "data": {
                    "tool_name": "remote_tool",
                    "tool_call_id": "tool-non-json-budget",
                    "status": "success",
                    "result": {
                        "path": Path("C:/workspace/generated.txt"),
                        "ids": {"alpha", "beta"},
                        "stdout": b"abcdefghijklmnopqrstuvwxyz",
                        "payload": b"abcdefghijklmnopqrstuvwxyz",
                    },
                    "execution_time": 0.1,
                },
            })

        event = channel.sse_queues[request_id].get(timeout=1)
        self.assertEqual(event["type"], "tool_end")
        self.assertEqual(event["limit_code"], "TOOL_OUTPUT_LIMIT")
        self.assertEqual(event["error_type"], "tool_output_limit")
        self.assertIn("generated.txt", event["result"])
        self.assertIn('"__type": "bytes"', event["result"])
        self.assertIn('"size_bytes": 26', event["result"])
        self.assertIn("stdout", {field["field"] for field in event["truncated_output_fields"]})

    def test_tool_and_artifact_budgets_use_configured_limits(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        config = {
            "web_tool_result_preview_chars": 60,
            "web_tool_output_field_chars": 10,
            "web_tool_output_collection_items": 1,
            "web_artifact_metadata_max_items": 1,
            "web_artifact_metadata_string_chars": 8,
            "web_artifact_metadata_path_chars": 12,
        }

        with patch.object(web_channel, "conf", return_value=config):
            with patch.object(channel, "TOOL_RESULT_PREVIEW_CHAR_LIMIT", 999):
                with patch.object(channel, "TOOL_OUTPUT_FIELD_CHAR_LIMIT", 999):
                    with patch.object(channel, "TOOL_OUTPUT_COLLECTION_ITEM_LIMIT", 999):
                        result_str, meta = channel._bounded_tool_result_for_sse({
                            "stdout": "A" * 40,
                            "items": [{"value": 1}, {"value": 2}],
                            "message": "M" * 40,
                        })

            self.assertEqual(meta["tool_output_limits"]["result_preview_chars"], 60)
            self.assertEqual(meta["tool_output_limits"]["output_field_chars"], 10)
            self.assertEqual(meta["tool_output_limits"]["collection_items"], 1)
            self.assertEqual(meta["result_limit_chars"], 60)
            self.assertLessEqual(len(result_str.split("\n", 1)[0]), 60)
            self.assertEqual(meta["limit_code"], "TOOL_OUTPUT_LIMIT")
            self.assertEqual(meta["error_type"], "tool_output_limit")
            truncated_fields = meta["truncated_output_fields"]
            self.assertIn("stdout", {field["field"] for field in truncated_fields})
            self.assertIn("result", {field["field"] for field in truncated_fields})
            self.assertTrue(any(
                field.get("field") == "result" and field.get("original_items") == 3 and field.get("kept_items") == 1
                for field in truncated_fields
            ))

            artifact = channel._record_request_artifact("req-configured-artifact-budget", {
                "id": "artifact-configured-budget",
                "title": "T" * 40,
                "path": "C:/workspace/" + ("deep/" * 4) + "artifact.png",
                "source": {"toolName": "render_image"},
            })

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["metadataLimits"]["max_items"], 1)
        self.assertEqual(artifact["metadataLimits"]["string_chars"], 8)
        self.assertEqual(artifact["metadataLimits"]["path_chars"], 12)
        self.assertTrue(artifact["metadataTruncated"])
        self.assertIn("[truncated", artifact["title"])
        self.assertIn("[truncated", artifact["path"])
        self.assertIn("path", {field["field"] for field in artifact["truncatedFields"]})

    def test_artifact_collection_budget_stops_scanning_after_cap(self):
        from channel.web import web_channel

        class LazyArtifactList(list):
            def __len__(self):
                return 100

            def __iter__(self):
                for index in range(4):
                    if index > 2:
                        raise AssertionError("artifact scanner iterated past the cap sentinel")
                    yield {"path": f"lazy-output-{index}.png", "fileName": f"lazy-output-{index}.png", "fileType": "image"}

        channel = web_channel.WebChannel()
        with patch.object(channel, "ARTIFACT_METADATA_MAX_ITEMS", 2):
            artifacts = channel._artifacts_from_tool_result(
                "req-artifact-lazy-budget",
                "render_image",
                "tool-artifact-lazy-budget",
                "success",
                {"type": "artifact", "files": LazyArtifactList()},
            )

        self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts[-1]["_omitted_artifact_count"], 98)

    def test_artifact_metadata_limit_caps_count_and_marks_warning(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-artifact-budget"
        channel.request_to_session = {request_id: "session-artifact-budget"}
        channel._ensure_sse_state(request_id)
        long_title = "very-long-generated-artifact-title-" + ("x" * 80)
        files = [
            {"path": f"output-{index}.png", "fileName": long_title, "fileType": "image"}
            for index in range(3)
        ]

        with patch.object(channel, "ARTIFACT_METADATA_MAX_ITEMS", 2):
            with patch.object(channel, "ARTIFACT_METADATA_STRING_CHAR_LIMIT", 20):
                with patch.object(channel, "ARTIFACT_METADATA_PATH_CHAR_LIMIT", 80):
                    callback = channel._make_sse_callback(request_id)
                    callback({
                        "type": "tool_execution_end",
                        "data": {
                            "tool_name": "render_image",
                            "tool_call_id": "tool-artifact-budget",
                            "status": "success",
                            "result": {
                                "type": "artifact",
                                "files": files,
                            },
                            "execution_time": 0.5,
                        },
                    })

        events = [channel.sse_queues[request_id].get(timeout=1) for _ in range(4)]
        artifact_events = [event for event in events if event["type"] == "artifact"]
        limit_events = [event for event in events if event["type"] == "artifact_limit"]
        tool_end_events = [event for event in events if event["type"] == "tool_end"]

        self.assertEqual(len(artifact_events), 2)
        self.assertEqual(len(channel.request_artifacts[request_id]), 2)
        self.assertEqual(len(limit_events), 1)
        self.assertEqual(limit_events[0]["code"], "ARTIFACT_METADATA_LIMIT")
        self.assertEqual(limit_events[0]["error_type"], "artifact_metadata_limit")
        self.assertEqual(limit_events[0]["event_type"], "artifact.limit")
        self.assertEqual(limit_events[0]["limit"], 2)
        self.assertEqual(limit_events[0]["omitted"], 1)
        self.assertEqual(len(tool_end_events), 1)
        first_artifact = artifact_events[0]["artifact"]
        self.assertTrue(first_artifact["metadataTruncated"])
        self.assertEqual(first_artifact["metadataLimits"]["max_items"], 2)
        self.assertIn("[truncated", first_artifact["title"])
        self.assertLess(len(first_artifact["title"]), len(long_title))

    def test_artifact_metadata_zero_item_limit_emits_warning(self):
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        request_id = "req-artifact-zero-budget"
        channel.request_to_session = {request_id: "session-artifact-zero-budget"}
        channel._ensure_sse_state(request_id)
        files = [
            {"path": f"zero-output-{index}.png", "fileName": f"zero-output-{index}.png", "fileType": "image"}
            for index in range(2)
        ]

        with patch.object(channel, "ARTIFACT_METADATA_MAX_ITEMS", 0):
            callback = channel._make_sse_callback(request_id)
            callback({
                "type": "tool_execution_end",
                "data": {
                    "tool_name": "render_image",
                    "tool_call_id": "tool-artifact-zero-budget",
                    "status": "success",
                    "result": {
                        "type": "artifact",
                        "files": files,
                    },
                    "execution_time": 0.5,
                },
            })

        events = [channel.sse_queues[request_id].get(timeout=1) for _ in range(2)]
        artifact_events = [event for event in events if event["type"] == "artifact"]
        limit_events = [event for event in events if event["type"] == "artifact_limit"]
        tool_end_events = [event for event in events if event["type"] == "tool_end"]

        self.assertEqual(artifact_events, [])
        self.assertNotIn(request_id, channel.request_artifacts)
        self.assertEqual(len(limit_events), 1)
        self.assertEqual(limit_events[0]["code"], "ARTIFACT_METADATA_LIMIT")
        self.assertEqual(limit_events[0]["limit"], 0)
        self.assertEqual(limit_events[0]["omitted"], 2)
        self.assertEqual(len(tool_end_events), 1)


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

    def test_tool_schema_intent_keyword_uses_ascii_word_boundaries(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )

        self.assertNotIn("feishu", executor._tool_schema_intent_groups("explain this database note"))
        self.assertIn("feishu", executor._tool_schema_intent_groups("read this Feishu base table"))

    def test_tool_schema_budget_keeps_explicit_feishu_cli_with_mcp_present(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("read"), tool("feishu_cli"), tool("mcp__server__tool")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "please use feishu_cli status"}]}],
        )

        selected, budget = executor._select_tools_for_schema()

        self.assertIn("feishu_cli", selected)
        self.assertEqual(budget["selection_reasons"]["feishu_cli"], "core")

    def test_tool_schema_budget_fallback_avoids_feishu_cli_with_mcp_present(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("feishu_cli"), tool("mcp__server__tool")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "plain unrelated request"}]}],
        )

        selected, budget = executor._select_tools_for_schema()

        self.assertNotIn("feishu_cli", selected)
        self.assertEqual(set(selected), {"mcp__server__tool"})
        self.assertEqual(budget["selection_reasons"]["mcp__server__tool"], "fallback_first_tool")

    def test_feishu_im_message_reads_are_keyed_by_chat_target(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )

        keys = set()
        for idx in range(9):
            args = {
                "action": "run",
                "args": [
                    "im",
                    "+chat-messages-list",
                    "--as",
                    "user",
                    "--chat-id",
                    f"oc_chat_{idx}",
                    "--start",
                    "2026-06-23T00:00:00+08:00",
                    "--end",
                    "2026-06-23T20:35:00+08:00",
                    "--page-size",
                    "50",
                    "--sort",
                    "asc",
                ],
            }
            keys.add(executor._tool_chain_key("feishu_cli", args))
            executor._record_tool_result("feishu_cli", args, True)
            should_stop, _reason = executor._check_tool_chain_budget("feishu_cli", args)
            self.assertFalse(should_stop)

        self.assertEqual(len(keys), 9)

    def test_feishu_im_message_reads_still_block_same_chat_repeat(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )
        args = {
            "action": "run",
            "args": [
                "im",
                "+chat-messages-list",
                "--as",
                "user",
                "--chat-id",
                "oc_same_chat",
                "--start",
                "2026-06-23T00:00:00+08:00",
                "--end",
                "2026-06-23T20:35:00+08:00",
                "--page-size",
                "50",
                "--sort",
                "asc",
            ],
        }

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

    def test_executor_ensures_internal_final_response_is_assistant_text(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[],
        )
        executor.messages = [
            {"role": "user", "content": [{"type": "text", "text": "查飞书群消息"}]},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "feishu_cli",
                    "input": {"action": "run"},
                }],
            },
        ]

        executor._ensure_final_response_message("最终结论：已查到 3 个群，剩余 6 个待继续。")

        self.assertEqual(executor.messages[-1]["role"], "assistant")
        self.assertEqual(executor.messages[-1]["content"][0]["type"], "text")
        self.assertIn("最终结论", executor.messages[-1]["content"][0]["text"])

    def test_agent_bridge_adds_missing_final_response_message(self):
        from bridge.agent_bridge import _ensure_final_response_in_messages

        agent = types.SimpleNamespace(messages=[], messages_lock=threading.RLock())
        new_messages = [
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "feishu_cli",
                    "input": {"action": "run"},
                }],
            }
        ]

        result = _ensure_final_response_in_messages(agent, new_messages, "最终摘要：没有待办。")

        self.assertEqual(result[-1]["role"], "assistant")
        self.assertEqual(result[-1]["content"][0]["type"], "text")
        self.assertEqual(agent.messages[-1]["content"][0]["text"], "最终摘要：没有待办。")

    def test_agent_bridge_does_not_synthesize_cancelled_final_response(self):
        from bridge.agent_bridge import _ensure_final_response_in_messages

        agent = types.SimpleNamespace(messages=[], messages_lock=threading.RLock())
        new_messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "_(Cancelled by user)_"}],
            }
        ]

        result = _ensure_final_response_in_messages(agent, new_messages, "_(Cancelled)_")

        self.assertEqual(result, new_messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(agent.messages, [])

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

    def test_permission_wait_cancel_raises_agent_cancelled(self):
        from agent.protocol.agent_stream import AgentStreamExecutor
        from agent.protocol.cancel import AgentCancelledError

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
            return_value={
                "allowed": False,
                "reason": "User stopped the current task.",
                "cancelled": True,
            },
        ):
            with self.assertRaises(AgentCancelledError):
                executor._execute_tool({
                    "id": "tool-call-cancelled-permission",
                    "name": "bash",
                    "arguments": {"command": "whoami"},
                })

        self.assertFalse(executor._force_text_response_next_turn)

    def test_run_stream_permission_wait_cancel_injects_tool_result(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def call_stream(self, request):
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "tool-call-run-cancelled-permission",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps({"command": "whoami"}),
                                },
                            }]
                        },
                        "finish_reason": "tool_calls",
                    }]
                }

        class FakeAgent:
            last_usage = {}
            memory_manager = None
            max_context_tokens = 10000

            @staticmethod
            def _estimate_message_tokens(message):
                return 1

            @staticmethod
            def _get_model_context_window():
                return 10000

            @staticmethod
            def _get_context_reserve_tokens():
                return 1000

        events = []
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=FakeModel(),
            system_prompt="",
            tools=[
                types.SimpleNamespace(
                    name="bash",
                    description="run shell",
                    params={"type": "object", "properties": {}},
                )
            ],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "run whoami"}]}],
        )

        with patch.object(
            executor,
            "_authorize_tool_execution",
            return_value={
                "allowed": False,
                "reason": "User stopped the current task.",
                "cancelled": True,
            },
        ):
            final_response = executor.run_stream("run whoami")

        self.assertEqual(final_response, "_(Cancelled)_")
        event_types = [event["type"] for event in events]
        self.assertIn("agent_cancelled", event_types)
        self.assertTrue(events[-1]["data"]["cancelled"])

        tool_result_message = executor.messages[-2]
        self.assertEqual(tool_result_message["role"], "user")
        self.assertEqual(tool_result_message["content"][0]["type"], "tool_result")
        self.assertEqual(
            tool_result_message["content"][0]["tool_use_id"],
            "tool-call-run-cancelled-permission",
        )
        self.assertTrue(tool_result_message["content"][0]["is_error"])

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

    def test_forced_text_retry_keeps_tool_schema_disabled(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise TimeoutError("timeout")
                yield {"choices": [{"delta": {"content": "retry summary"}, "finish_reason": "stop"}]}

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
            messages=[{"role": "user", "content": [{"type": "text", "text": "summarize"}]}],
        )
        executor._sleep_cancelable = lambda _seconds: None
        executor._force_text_response_once("test")

        content, tool_calls = executor._call_llm_stream(retry_on_empty=False, max_retries=1)

        self.assertEqual(content, "retry summary")
        self.assertEqual(tool_calls, [])
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(all(request.tools is None for request in model.requests))
        self.assertTrue(all(request.tool_schema_budget["reason"] == "forced_text" for request in model.requests))
        self.assertFalse(executor._force_text_response_next_turn)

    def test_forced_text_empty_retry_keeps_tool_schema_disabled(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    return
                yield {"choices": [{"delta": {"content": "empty retry summary"}, "finish_reason": "stop"}]}

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
            messages=[{"role": "user", "content": [{"type": "text", "text": "summarize"}]}],
        )
        executor._force_text_response_once("test")

        content, tool_calls = executor._call_llm_stream(retry_on_empty=True)

        self.assertEqual(content, "empty retry summary")
        self.assertEqual(tool_calls, [])
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(all(request.tools is None for request in model.requests))
        self.assertTrue(all(request.tool_schema_budget["reason"] == "forced_text" for request in model.requests))

    def test_tool_schema_budget_defers_non_intent_tools_on_plain_turn(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "plain answer"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        events = []
        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[
                tool("read"),
                tool("bash"),
                tool("feishu_cli"),
                tool("browser"),
                tool("mcp__chrome-devtools__click"),
            ],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "just explain this codebase/database note"}]}],
        )

        content, tool_calls = executor._call_llm_stream(retry_on_empty=False)

        self.assertEqual(content, "plain answer")
        self.assertEqual(tool_calls, [])
        sent_tools = {entry["name"] for entry in model.requests[0].tools}
        self.assertEqual(sent_tools, {"read", "bash"})
        budget = model.requests[0].tool_schema_budget
        self.assertTrue(budget["enabled"])
        self.assertEqual(budget["selected_count"], 2)
        self.assertEqual(budget["deferred_count"], 3)
        self.assertIn("feishu_cli", budget["deferred_tools"])
        budget_events = [event for event in events if event["type"] == "tool_schema_budget"]
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["data"]["selected_tools"], ["bash", "read"])

    def test_context_budget_attaches_request_metadata_and_event(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "deepseek-v4-flash"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "budgeted"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {"path": {"type": "string"}}},
            )

        events = []
        model = FakeModel()
        artifact_payload = {
            "type": "artifact",
            "title": "report",
            "path": "C:/workspace/report.md",
            "metadata": {"rows": 12},
        }
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="system budget prompt",
            tools=[tool("read"), tool("bash"), tool("feishu_cli")],
            on_event=lambda event: events.append(event),
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "inspect report"}]},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "reasoning trace"},
                    {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "report.md"}},
                ]},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": json.dumps(artifact_payload),
                }]},
            ],
        )
        executor.files_to_send = [{"path": "C:/workspace/report.md", "kind": "markdown"}]

        content, tool_calls = executor._call_llm_stream(retry_on_empty=False)

        self.assertEqual(content, "budgeted")
        self.assertEqual(tool_calls, [])
        budget = model.requests[0].context_budget
        self.assertTrue(budget["enabled"])
        self.assertGreater(budget["system_prompt_tokens"], 0)
        self.assertGreater(budget["message_tokens"], 0)
        self.assertGreater(budget["reasoning_tokens"], 0)
        self.assertGreater(budget["tool_result_tokens"], 0)
        self.assertGreater(budget["artifact_metadata_tokens"], 0)
        self.assertGreater(budget["tool_schema_tokens"], 0)
        self.assertEqual(budget["tool_schema_selected_count"], model.requests[0].tool_schema_budget["selected_count"])
        budget_events = [event for event in events if event["type"] == "context_budget"]
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["data"], budget)

    def test_context_budget_clamps_configured_limit_to_model_window(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            max_context_tokens = 258000

            @staticmethod
            def _get_model_context_window():
                return 64000

            @staticmethod
            def _get_context_reserve_tokens():
                return 10000

        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=types.SimpleNamespace(model="deepseek-v4-flash"),
            system_prompt="",
            tools=[],
        )

        with patch("config.conf", return_value={
            "agent_context_budget_clamp_to_window": True,
            "agent_context_budget_response_reserve_tokens": 0,
        }):
            limits = executor._context_budget_limits()

        self.assertEqual(limits["context_window_tokens"], 64000)
        self.assertEqual(limits["configured_max_context_tokens"], 258000)
        self.assertEqual(limits["response_reserve_tokens"], 10000)
        self.assertEqual(limits["effective_context_limit_tokens"], 54000)
        self.assertTrue(limits["clamped_to_window"])

    def test_context_budget_caps_large_reserve_for_small_window_models(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            max_context_tokens = 258000

            @staticmethod
            def _get_model_context_window():
                return 8000

            @staticmethod
            def _get_context_reserve_tokens():
                return 10000

        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=types.SimpleNamespace(model="gpt-4"),
            system_prompt="",
            tools=[],
        )

        with patch("config.conf", return_value={
            "agent_context_budget_clamp_to_window": True,
            "agent_context_budget_response_reserve_tokens": 0,
        }):
            limits = executor._context_budget_limits()

        self.assertEqual(limits["context_window_tokens"], 8000)
        self.assertEqual(limits["response_reserve_tokens"], 4000)
        self.assertEqual(limits["window_input_limit_tokens"], 4000)
        self.assertEqual(limits["effective_context_limit_tokens"], 4000)
        self.assertTrue(limits["clamped_to_window"])

    def test_context_trim_uses_effective_budget_and_preserves_current_turn(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            memory_manager = None
            max_context_tokens = 10000

            @staticmethod
            def _get_model_context_window():
                return 220

            @staticmethod
            def _get_context_reserve_tokens():
                return 20

            @staticmethod
            def _estimate_message_tokens(message):
                content = message.get("content", "")
                if isinstance(content, str):
                    return len(content)
                if isinstance(content, list):
                    total = 0
                    for block in content:
                        if isinstance(block, dict):
                            total += len(str(block.get("text") or block.get("content") or block.get("thinking") or ""))
                    return max(1, total)
                return 1

        messages = []
        for idx in range(6):
            label = "current" if idx == 5 else f"old-{idx}"
            messages.extend([
                {"role": "user", "content": [{"type": "text", "text": f"{label} " + ("x" * 80)}]},
                {"role": "assistant", "content": [{"type": "text", "text": "answer " + ("y" * 30)}]},
            ])

        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=types.SimpleNamespace(model="small-window"),
            system_prompt="system",
            tools=[],
            messages=messages,
            max_context_turns=20,
        )

        with patch("config.conf", return_value={
            "agent_context_budget_clamp_to_window": True,
            "agent_context_budget_response_reserve_tokens": 20,
        }):
            executor._trim_messages()

        serialized = json.dumps(executor.messages, ensure_ascii=False)
        self.assertIn("current", serialized)
        self.assertNotIn("old-0", serialized)
        self.assertLess(len(executor._identify_complete_turns()), 6)

    def test_context_overflow_recovery_retries_text_only_and_preserves_current_run(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            memory_manager = None
            last_usage = {}
            max_context_tokens = 10000

            @staticmethod
            def _get_model_context_window():
                return 6000

            @staticmethod
            def _get_context_reserve_tokens():
                return 500

            @staticmethod
            def _estimate_text_tokens(text):
                return max(1, len(str(text or "")) // 4)

            @staticmethod
            def _estimate_message_tokens(message):
                content = message.get("content", "")
                if isinstance(content, str):
                    return max(1, len(content) // 4)
                if isinstance(content, list):
                    total = 0
                    for block in content:
                        if isinstance(block, dict):
                            total += len(str(
                                block.get("text")
                                or block.get("content")
                                or block.get("thinking")
                                or block.get("input")
                                or ""
                            ))
                    return max(1, total // 4)
                return 1

        class FakeModel:
            model = "small-window"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    yield {
                        "error": {
                            "message": "provider rejected the request",
                            "code": "context_length_exceeded",
                            "type": "invalid_request_error",
                        },
                        "error_taxonomy": "context_overflow",
                        "status_code": 400,
                    }
                    return
                yield {"choices": [{"delta": {"content": "recovered summary"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {"path": {"type": "string"}}},
            )

        current_user_text = "current run ask must survive exactly"
        messages = []
        for idx in range(7):
            messages.extend([
                {"role": "user", "content": [{"type": "text", "text": f"old-{idx} " + ("x" * 12000)}]},
                {"role": "assistant", "content": [{"type": "text", "text": f"old answer {idx}"}]},
            ])
        messages.extend([
            {"role": "user", "content": [{"type": "text", "text": current_user_text}]},
            {"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "toolu_current",
                "name": "read",
                "input": {"path": "big.log", "content": "z" * 16000},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_current",
                "content": "CURRENT_TOOL_OUTPUT " + ("y" * 18000),
            }]},
        ])

        model = FakeModel()
        events = []
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system",
            tools=[tool("read"), tool("feishu_cli")],
            on_event=lambda event: events.append(event),
            messages=messages,
            max_context_turns=20,
        )

        with patch("config.conf", return_value={
            "agent_context_budget_clamp_to_window": True,
            "agent_context_budget_response_reserve_tokens": 500,
        }):
            content, tool_calls = executor._call_llm_stream(retry_on_empty=False, max_retries=0)

        self.assertEqual(content, "recovered summary")
        self.assertEqual(tool_calls, [])
        self.assertEqual(len(model.requests), 2)
        self.assertIsNotNone(model.requests[0].tools)
        self.assertIsNone(model.requests[1].tools)
        self.assertEqual(model.requests[1].tool_schema_budget["reason"], "forced_text")
        self.assertEqual(
            model.requests[1].tool_schema_budget["force_text_reason"],
            "context_overflow_recovery",
        )
        self.assertIn(current_user_text, json.dumps(executor.messages, ensure_ascii=False))
        self.assertNotIn("old-0", json.dumps(executor.messages, ensure_ascii=False))
        retry_messages = model.requests[1].messages
        tool_use_index = next(
            idx for idx, message in enumerate(retry_messages)
            if message.get("role") == "assistant"
            and any(
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == "toolu_current"
                for block in message.get("content", [])
            )
        )
        self.assertEqual(retry_messages[tool_use_index + 1]["role"], "user")
        self.assertTrue(any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == "toolu_current"
            for block in retry_messages[tool_use_index + 1].get("content", [])
        ))
        message_starts = [event for event in events if event["type"] == "message_start"]
        message_ends = [event for event in events if event["type"] == "message_end"]
        self.assertEqual(len(message_starts), 2)
        self.assertEqual(len(message_ends), 2)
        self.assertTrue(message_ends[0]["data"]["context_overflow_retry"])

        recovery_events = [event["data"] for event in events if event["type"] == "context_overflow_recovery"]
        self.assertEqual(len(recovery_events), 1)
        recovery = recovery_events[0]
        self.assertTrue(recovery["applied"])
        self.assertTrue(recovery["retry"])
        self.assertTrue(recovery["force_text_response"])
        self.assertTrue(recovery["current_turn_preserved"])
        self.assertGreater(recovery["removed_turns"], 0)
        self.assertGreater(recovery["truncated_current_run_blocks"], 0)
        self.assertLess(
            recovery["after_estimated_input_tokens"],
            recovery["before_estimated_input_tokens"],
        )

    def test_context_overflow_schema_only_recovery_does_not_clear_history(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            memory_manager = None
            last_usage = {}
            max_context_tokens = 10000

            @staticmethod
            def _get_model_context_window():
                return 6000

            @staticmethod
            def _get_context_reserve_tokens():
                return 500

            @staticmethod
            def _estimate_text_tokens(text):
                return max(1, len(str(text or "")) // 4)

        class FakeModel:
            model = "schema-heavy-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    yield {
                        "error": {
                            "message": "request_too_large",
                            "code": "context_length_exceeded",
                            "type": "invalid_request_error",
                        },
                        "error_taxonomy": "context_overflow",
                        "status_code": 400,
                    }
                    return
                yield {"choices": [{"delta": {"content": "schema-only recovered"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} " + ("schema " * 2000),
                params={"type": "object", "properties": {"value": {"type": "string"}}},
            )

        events = []
        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system",
            tools=[tool("read"), tool("feishu_cli")],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "short current run"}]}],
        )

        with patch("config.conf", return_value={
            "agent_context_budget_clamp_to_window": True,
            "agent_context_budget_response_reserve_tokens": 500,
        }):
            content, tool_calls = executor._call_llm_stream(retry_on_empty=False, max_retries=0)

        self.assertEqual(content, "schema-only recovered")
        self.assertEqual(tool_calls, [])
        self.assertEqual(len(model.requests), 2)
        self.assertIsNotNone(model.requests[0].tools)
        self.assertIsNone(model.requests[1].tools)
        self.assertIn("short current run", json.dumps(executor.messages, ensure_ascii=False))
        recovery = [event["data"] for event in events if event["type"] == "context_overflow_recovery"][0]
        self.assertTrue(recovery["applied"])
        self.assertFalse(recovery["trim_applied"])
        self.assertTrue(recovery["schema_only_recovery"])
        self.assertTrue(recovery["tool_schema_disabled"])

    def test_context_overflow_after_partial_output_does_not_retry_or_clear_history(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "partial-overflow-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
                yield {
                    "error": {
                        "message": "maximum context length exceeded after partial output",
                        "code": "context_length_exceeded",
                        "type": "invalid_request_error",
                    },
                    "error_taxonomy": "context_overflow",
                    "status_code": 400,
                }

        events = []
        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}, memory_manager=None),
            model=model,
            system_prompt="",
            tools=[],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "partial current run"}]}],
        )

        with self.assertRaises(Exception) as raised:
            executor._call_llm_stream(retry_on_empty=False, max_retries=0)

        self.assertIn("after output had started", str(raised.exception))
        self.assertEqual(len(model.requests), 1)
        self.assertFalse([event for event in events if event["type"] == "context_overflow_recovery"])
        self.assertIn("partial current run", json.dumps(executor.messages, ensure_ascii=False))
        message_ends = [event["data"] for event in events if event["type"] == "message_end"]
        self.assertEqual(len(message_ends), 1)
        self.assertTrue(message_ends[0]["error"])
        self.assertTrue(message_ends[0]["context_overflow_after_output"])
        self.assertEqual(message_ends[0]["content"], "partial")

    def test_context_overflow_retry_marker_survives_empty_retry(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeAgent:
            memory_manager = None
            last_usage = {}
            max_context_tokens = 10000

            @staticmethod
            def _get_model_context_window():
                return 6000

            @staticmethod
            def _get_context_reserve_tokens():
                return 500

            @staticmethod
            def _estimate_text_tokens(text):
                return max(1, len(str(text or "")) // 4)

        class FakeModel:
            model = "overflow-empty-overflow"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    yield {
                        "error": {"message": "request_too_large", "code": "context_length_exceeded"},
                        "error_taxonomy": "context_overflow",
                        "status_code": 400,
                    }
                    return
                if len(self.requests) == 2:
                    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    return
                yield {
                    "error": {"message": "request_too_large", "code": "context_length_exceeded"},
                    "error_taxonomy": "context_overflow",
                    "status_code": 400,
                }

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} " + ("schema " * 2000),
                params={"type": "object", "properties": {}},
            )

        events = []
        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=FakeAgent(),
            model=model,
            system_prompt="system",
            tools=[tool("read")],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "short current run"}]}],
        )

        with self.assertRaises(Exception):
            executor._call_llm_stream(retry_on_empty=True, max_retries=0)

        self.assertEqual(len(model.requests), 3)
        recovery_events = [event["data"] for event in events if event["type"] == "context_overflow_recovery"]
        self.assertEqual(len(recovery_events), 1)
        self.assertTrue(recovery_events[0]["schema_only_recovery"])

    def test_stream_message_format_error_with_generic_too_large_does_not_recover_as_overflow(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "message-format-model"

            def call_stream(self, _request):
                yield {
                    "error": {
                        "message": "tool_result tool id is not found because payload is too large",
                        "code": "invalid_request_error",
                        "type": "invalid_request_error",
                    },
                    "status_code": 400,
                }

        events = []
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}, memory_manager=None),
            model=FakeModel(),
            system_prompt="",
            tools=[],
            on_event=lambda event: events.append(event),
            messages=[{"role": "user", "content": [{"type": "text", "text": "repair bad history"}]}],
        )

        with self.assertRaises(Exception):
            executor._call_llm_stream(retry_on_empty=False, max_retries=0)

        self.assertFalse([event for event in events if event["type"] == "context_overflow_recovery"])
        self.assertEqual(executor.messages, [])

    def test_context_overflow_classifier_does_not_treat_generic_too_large_as_overflow(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        self.assertFalse(AgentStreamExecutor._is_context_overflow_error(
            message="tool_result id not found because payload is too large",
            status_code=400,
            error_type="invalid_request_error",
        ))
        self.assertTrue(AgentStreamExecutor._is_context_overflow_error(
            message="request too large for the model context window",
            status_code=400,
            error_type="invalid_request_error",
        ))

    def test_tool_schema_budget_expands_matching_intent_groups(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[
                tool("read"),
                tool("feishu_cli"),
                tool("browser"),
                tool("mcp__chrome-devtools__click"),
                tool("scheduler"),
            ],
            messages=[{"role": "user", "content": [{"type": "text", "text": "打开 chrome devtools 看这个飞书 base 页面"}]}],
        )

        executor._call_llm_stream(retry_on_empty=False)

        sent_tools = {entry["name"] for entry in model.requests[0].tools}
        self.assertIn("read", sent_tools)
        self.assertIn("feishu_cli", sent_tools)
        self.assertIn("browser", sent_tools)
        self.assertIn("mcp__chrome-devtools__click", sent_tools)
        self.assertNotIn("scheduler", sent_tools)
        budget = model.requests[0].tool_schema_budget
        self.assertIn("browser", budget["intent_groups"])
        self.assertIn("feishu", budget["intent_groups"])

    def test_tool_schema_budget_inherits_intent_for_short_confirmation(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "scheduled"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[tool("read"), tool("scheduler"), tool("feishu_cli")],
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "明天提醒我提交报告"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "要我现在创建提醒吗？"}]},
                {"role": "user", "content": [{"type": "text", "text": "好的，执行"}]},
            ],
        )

        executor._call_llm_stream(retry_on_empty=False)

        sent_tools = {entry["name"] for entry in model.requests[0].tools}
        self.assertIn("read", sent_tools)
        self.assertIn("scheduler", sent_tools)
        self.assertIn("feishu_cli", sent_tools)
        budget = model.requests[0].tool_schema_budget
        self.assertIn("scheduler", budget["intent_groups"])
        self.assertTrue(budget["inherited_followup_intent"])

    def test_tool_schema_budget_expands_env_config_for_key_intent(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "configured"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[tool("read"), tool("env_config"), tool("feishu_cli")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "配置 OpenAI API key"}]}],
        )

        executor._call_llm_stream(retry_on_empty=False)

        sent_tools = {entry["name"] for entry in model.requests[0].tools}
        self.assertIn("env_config", sent_tools)
        self.assertIn("feishu_cli", sent_tools)
        self.assertIn("diagnostics", model.requests[0].tool_schema_budget["intent_groups"])

    def test_tool_schema_budget_expands_env_config_for_api_key_variable_name(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("read"), tool("env_config"), tool("feishu_cli")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "set OPENAI_API_KEY"}]}],
        )

        selected, budget = executor._select_tools_for_schema()

        self.assertIn("env_config", selected)
        self.assertIn("feishu_cli", selected)
        self.assertIn("diagnostics", budget["intent_groups"])

    def test_tool_schema_budget_keeps_small_custom_toolset_with_core_tool(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("read"), tool("custom_report")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "plain"}]}],
        )

        selected, budget = executor._select_tools_for_schema()

        self.assertEqual(set(selected.keys()), {"read", "custom_report"})
        self.assertEqual(budget["selection_reasons"]["custom_report"], "small_custom_toolset")

    def test_tool_schema_budget_preserves_recent_non_core_tool_chain(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("read"), tool("scheduler"), tool("feishu_cli")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "plain follow-up"}]}],
        )
        executor._record_tool_result("scheduler", {"action": "create"}, True)

        selected, budget = executor._select_tools_for_schema()

        self.assertIn("scheduler", selected)
        self.assertEqual(budget["selection_reasons"]["scheduler"], "recent_tool_chain")

    def test_tool_schema_budget_can_be_disabled_by_config(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class FakeModel:
            model = "fake-model"

            def __init__(self):
                self.requests = []

            def call_stream(self, request):
                self.requests.append(request)
                yield {"choices": [{"delta": {"content": "full"}, "finish_reason": "stop"}]}

        def tool(name):
            return types.SimpleNamespace(
                name=name,
                description=f"{name} tool",
                params={"type": "object", "properties": {}},
            )

        model = FakeModel()
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=model,
            system_prompt="",
            tools=[tool("read"), tool("feishu_cli"), tool("mcp__server__tool")],
            messages=[{"role": "user", "content": [{"type": "text", "text": "plain"}]}],
        )

        with patch("config.conf", return_value={"agent_tool_schema_budget_enabled": False}):
            executor._call_llm_stream(retry_on_empty=False)

        sent_tools = {entry["name"] for entry in model.requests[0].tools}
        self.assertEqual(sent_tools, {"read", "feishu_cli", "mcp__server__tool"})
        self.assertFalse(model.requests[0].tool_schema_budget["enabled"])
        self.assertEqual(model.requests[0].tool_schema_budget["reason"], "disabled_by_config")

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

        git_reason = executor._external_capability_reroute(
            "bash",
            {"command": "git clone https://github.com/larksuite/cli /tmp/feishu"},
        )
        self.assertIn("Do not call Feishu/Lark CLI through raw bash", git_reason)
        self.assertIn("feishu_cli", git_reason)

        git_ssh_reason = executor._external_capability_reroute(
            "bash",
            {"command": "git clone git@github.com:larksuite/cli.git /tmp/feishu"},
        )
        self.assertIn("Do not call Feishu/Lark CLI through raw bash", git_ssh_reason)
        self.assertIn("feishu_cli", git_ssh_reason)

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

    def test_feishu_cli_install_rejects_unstructured_find_skill_result(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        tool = FeishuCli({"package": "@larksuite/cli@1.0.56"})
        with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=None), \
                patch("agent.tools.feishu_cli.feishu_cli._which", return_value="npm"), \
                patch.object(FeishuCli, "_safe_run") as safe_run:
            for gate in ("find-skill", {"status": "success", "package": "not-related"}):
                with self.subTest(gate=gate):
                    result = tool.execute({
                        "action": "install",
                        "timeout": 1,
                        "find_skill_result": gate,
                    })
                    self.assertEqual(result.status, "error")
                    self.assertTrue(result.result["discoveryOnly"])

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

    def test_feishu_cli_install_allows_structured_find_skill_result(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        tool = FeishuCli({"package": "@larksuite/cli@1.0.56"})
        with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", side_effect=[None, ["lark"]]), \
                patch("agent.tools.feishu_cli.feishu_cli._which", return_value="npm"), \
                patch.object(FeishuCli, "_safe_run", return_value={"status": "success", "exitCode": 0, "output": "ok"}) as safe_run:
            result = tool.execute({
                "action": "install",
                "timeout": 1,
                "find_skill_result": {
                    "status": "success",
                    "source": "find-skill",
                    "package": "@larksuite/cli",
                    "url": "https://github.com/larksuite/cli",
                },
            })

        self.assertEqual(result.status, "success")
        self.assertTrue(result.result["installedNow"])
        safe_run.assert_called_once()

    def test_feishu_cli_install_root_uses_env_override(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        with tempfile.TemporaryDirectory() as workspace:
            install_root = os.path.join(workspace, "user-data", "capabilities", "lark-cli")
            previous = os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
            os.environ["ECOREX_LARK_CLI_INSTALL_ROOT"] = install_root
            try:
                tool = FeishuCli({"package": "@larksuite/cli@1.0.56"})
                self.assertEqual(str(tool._install_root()), install_root)
            finally:
                if previous is None:
                    os.environ.pop("ECOREX_LARK_CLI_INSTALL_ROOT", None)
                else:
                    os.environ["ECOREX_LARK_CLI_INSTALL_ROOT"] = previous

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

    def test_permission_broker_marks_cancelled_wait_and_clears_pending(self):
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
                events = []

                decision = broker.authorize(
                    "bash",
                    "tool-cancel-permission",
                    {"command": "echo should-not-run"},
                    emit_event=lambda event_type, payload: events.append((event_type, payload)),
                    cancel_event=cancel_event,
                    timeout_seconds=1,
                )

                pending = broker.list_pending()["pending"]
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data

        self.assertFalse(decision["allowed"])
        self.assertTrue(decision.get("cancelled"))
        self.assertEqual(decision["reason"], "User stopped the current task.")
        self.assertEqual(pending, [])
        self.assertEqual(events[0][0], "tool_permission_request")
        self.assertEqual(events[0][1]["tool"], "bash")

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

    def test_openai_image_provider_uses_gpt_image_2_pro_without_model_fallback(self):
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
                return {"data": [{"b64_json": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="}]}

            provider._post_json = fake_post_json
            with self.assertRaises(RuntimeError):
                provider.generate(
                    "orange x",
                    quality="low",
                    size="1024x1024",
                    output_format="png",
                    output_dir=output_dir,
                )

        self.assertEqual(provider.model, "gpt-image-2-pro")
        self.assertEqual(urls, ["https://api.openai.com/v1/images/generations"])
        self.assertEqual([payload["model"] for payload in payloads], ["gpt-image-2-pro"])
        self.assertEqual(payloads[0]["n"], 1)
        self.assertEqual(payloads[0]["quality"], "low")
        self.assertEqual(payloads[0]["output_format"], "png")
        self.assertNotIn("response_format", payloads[0])
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
                return {"data": [{"b64_json": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="}]}

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

    def test_v019_xhs_skill_generates_final_images_with_pro_only_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "skills" / "create-xiaohongshu-note" / "scripts" / "generate_cover_image.py"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cover.png"
            status_path = Path(tmp) / "cover.status.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--prompt",
                    "final cover smoke",
                    "--output",
                    str(output),
                    "--status-path",
                    str(status_path),
                    "--dry-run",
                ],
                cwd=str(root),
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            for item in [payload, status_payload]:
                self.assertEqual(item["provider"], "openai")
                self.assertEqual(item["model"], "gpt-image-2-pro")
                self.assertEqual(item["image_kind"], "final")
                self.assertFalse(item["draft"])
                self.assertFalse(item["fallback_used"])
                self.assertNotIn("fallback_model", item)
            self.assertFalse(output.exists())

            stale_output = Path(tmp) / "stale-cache.png"
            stale_status = Path(tmp) / "stale-cache.status.json"
            stale_bytes = (
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR"
                + (1).to_bytes(4, "big")
                + (1).to_bytes(4, "big")
                + b"\x08\x06\x00\x00\x00"
                + b"placeholder-python-draft"
            )
            stale_output.write_bytes(stale_bytes)
            stale_status.write_text(
                json.dumps({
                    "ok": True,
                    "status": "completed",
                    "provider": "openai",
                    "model": "gpt-image-2-pro",
                    "image_kind": "final",
                    "draft": False,
                    "fallback_used": False,
                    "prompt_hash": hashlib.sha256(b"final cover smoke\n1080x1440").hexdigest()[:16],
                    "sha256": hashlib.sha256(stale_bytes).hexdigest(),
                }),
                encoding="utf-8",
            )
            stale_cache = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--prompt",
                    "final cover smoke",
                    "--output",
                    str(stale_output),
                    "--status-path",
                    str(stale_status),
                    "--dry-run",
                ],
                cwd=str(root),
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(stale_cache.returncode, 0, stale_cache.stderr)
            stale_payload = json.loads(stale_cache.stdout)
            self.assertEqual(stale_payload["status"], "dry_run")
            self.assertFalse(stale_output.exists())

            wrong_model = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--prompt",
                    "wrong model smoke",
                    "--output",
                    str(Path(tmp) / "wrong.png"),
                    "--model",
                    "gpt-image-2",
                    "--dry-run",
                ],
                cwd=str(root),
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(wrong_model.returncode, 2)
            self.assertIn("gpt-image-2-pro", wrong_model.stdout)

            fallback_arg = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--prompt",
                    "fallback arg smoke",
                    "--output",
                    str(Path(tmp) / "fallback.png"),
                    "--fallback-model",
                    "gpt-image-2",
                    "--dry-run",
                ],
                cwd=str(root),
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertNotEqual(fallback_arg.returncode, 0)
            self.assertIn("--fallback-model", fallback_arg.stderr)

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
        telemetry_ts = (root / "desktop" / "electron" / "telemetry.ts").read_text(encoding="utf-8")
        stage_runtime_win = (root / "desktop" / "scripts" / "stage-runtime-win.ps1").read_text(encoding="utf-8")
        web_channel_py = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', generate_py)
        self.assertNotIn('FALLBACK_MODEL = "gpt-image-2"', generate_py)
        self.assertIn("OpenAI default mode uses `gpt-image-2-pro` only", skill_md)
        self.assertIn("or OpenAIProvider.DEFAULT_MODEL", generate_py)
        self.assertIn("LinkAI default model follows EcoreX's OpenAI image default", generate_py)
        self.assertNotIn('("linkai",    "image-2-pro")', web_channel_py)
        self.assertIn('("linkai",    "gpt-image-2-pro")', web_channel_py)
        self.assertIn('"linkai": [\n            "gpt-image-2-pro"', web_channel_py)
        self.assertIn('Do not create final images by coding HTML/canvas/SVG/Pillow layouts', skill_md)
        self.assertIn("legacy `image-2-pro` input is normalized", skill_md)
        self.assertIn('parser.add_argument("--model", default="gpt-image-2-pro")', xhs_py)
        self.assertIn("def generate_final_image", xhs_py)
        self.assertIn('raise ValueError("create-xiaohongshu-note final images must use --model gpt-image-2-pro")', xhs_py)
        self.assertIn('"image_kind": "final"', xhs_py)
        self.assertIn('"draft": False', xhs_py)
        self.assertNotIn('parser.add_argument("--fallback-model"', xhs_py)
        self.assertNotIn("def generate_with_fallback", xhs_py)
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', manager_py)
        self.assertIn('default="gpt-image-2-pro"', manager_py)
        self.assertIn('"ecorex-desktop-v0.1.14"', enterprise_policy_ts)
        self.assertIn('"ecorex-desktop-v0.1.13"', enterprise_policy_ts)
        self.assertIn("enterpriseClientEventKeys", enterprise_policy_ts)
        self.assertIn("hasPolicyOverrideValue", enterprise_policy_ts)
        self.assertIn("return value.length > 0", enterprise_policy_ts)
        self.assertIn("enterpriseClientEventKeys", telemetry_ts)
        self.assertIn("for (const clientEventKey of clientKeys)", telemetry_ts)
        self.assertIn("invalid client key", telemetry_ts)
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

    def test_v018_run_center_surfaces_active_runs_and_recovery_actions(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        bridge_source = (root / "desktop" / "electron" / "apiBridge.ts").read_text(encoding="utf-8")

        required_markers = [
            "const runCenterRequests = useMemo",
            "runtimeSnapshot.activeRequests",
            "runtimeSnapshot.recentTerminalRequests",
            "runtimeSnapshot.staleLocks",
            "function isSubagentRuntimeRequest",
            "function isSchedulerRuntimeRequest",
            "!isSchedulerRuntimeRequest(request)",
            "function runCenterState",
            "function isRunCenterVisibleRequest",
            "if (!isRunCenterVisibleRequest(request)) return false",
            "function isRunCenterSubagentRequest",
            "function isRunCenterSchedulerRequest",
            "function getRunCenterSubagentTaskId",
            "const [runCenterOpen, setRunCenterOpen] = useState(false)",
            "function openRunCenterSurface",
            "setRunCenterOpen(true)",
            "function runCenterRetryPolicy",
            "request.actions && request.actions.retry === false",
            "request.actions?.retry === true",
            "request.retryable === false && request.recoverable === false",
            "function retryRunCenterRequest",
            "Run Center retry prepared; review and send.",
            "function renderRunCenterPanel",
            "data-run-center-surface={surface}",
            "async function openRunCenterSession",
            "options: { closeSurface?: boolean } = {}",
            "if (options.closeSurface) {",
            "async function openRunCenterStaleLockSession",
            "openRunCenterSession(request, { closeSurface: surface === \"primary\" })",
            "openRunCenterStaleLockSession(lock, { closeSurface: surface === \"primary\" })",
            "if (isRunCenterSubagentRequest(request))",
            "if (isRunCenterSchedulerRequest(request))",
            "const scopedRow: SessionRow = {",
            "...(existing || {",
            "const requestId = request.request_id ? String(request.request_id) : undefined",
            "streamAvailable: state !== \"failed\" && request.stream_available !== false",
            "await selectSession(scopedRow)",
            "resumeRuntimeRequest(row.id, row.requestId, row.streamAvailable !== false)",
            "async function stopRunCenterRequest",
            "await cancelSubagentTask(taskId)",
            "const fallback = await cancelChatRequest({ requestId, sessionId })",
            "Number(fallback.cancelled || 0) <= 0",
            "cancelChatRequest({ requestId, sessionId })",
            "async function exportRunCenterDiagnostics",
            "exportDiagnosticsBundle({ sessionId, requestId })",
            'className={`run-center-panel is-${surface}`',
            'className={`run-center-nav-button${runCenterOpen ? " is-active" : ""}`}',
            'aria-label="Open Run Center"',
            'className="modal-backdrop run-center-backdrop"',
            'className="run-center-sheet"',
            'renderRunCenterPanel("primary")',
            "Run Center",
            "runCenterStats.cancelling",
            "runCenterStats.failed",
            "runCenterStats.stale",
            "const diagnosticsOnly = isSubagent || isScheduler",
            "const openAllowed = request.actions?.open ?? !diagnosticsOnly",
            "const stopAllowed = request.actions?.stop ?? !(runCenterState(request) === \"failed\" || (isSubagent && !subagentTaskId))",
            "disabled={!openAllowed}",
            "disabled={!retryPolicy.enabled}",
            "disabled={!stopAllowed}",
            "Run Center stop found no cancellable runtime row",
            "Scheduler runs are visible in Run Center; export diagnostics for details",
            "Stop scheduler run",
            "Scheduler stop requested",
        ]
        for marker in required_markers:
            self.assertIn(marker, app_source)
        self.assertNotIn("await selectSession(existing ||", app_source)

        for marker in [
            ".run-center-panel",
            ".run-center-panel.is-primary",
            ".run-center-stats",
            ".run-center-row",
            ".run-center-actions",
            ".run-center-nav-button",
            ".run-center-sheet",
            ".run-center-row.is-cancelling .run-center-state",
            ".run-center-row.is-failed .run-center-state",
            ".run-center-row.is-stale .run-center-state",
        ]:
            self.assertIn(marker, css_source)

        self.assertIn('"/api/active-requests"', api_source)
        for marker in [
            "actions?: {",
            "retry_mode?: string",
            "retry_disabled_reason?: string",
        ]:
            self.assertIn(marker, api_source)
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        for marker in [
            "def attach_run_center_policy",
            "\"retry_mode\"",
            "\"manual_retry_prepare\"",
            "\"retry_disabled_reason\"",
            "\"actions\"",
        ]:
            self.assertIn(marker, web_source)
        self.assertIn("export async function cancelSubagentTask", api_source)
        self.assertIn('`/api/subagents/${encodeURIComponent(taskId)}/cancel`', api_source)
        self.assertIn('"GET /api/active-requests"', bridge_source)
        self.assertIn('/^\\/api\\/subagents\\/[^/]+\\/(?:cancel|collect)$/.test(cleanPath)', bridge_source)

    def test_v018_desktop_handles_sse_replay_gap_recovery(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

        helper_start = app_source.index("function handleReplayGapStreamItem")
        helper_end = app_source.index("function finishRunningSteps", helper_start)
        helper_source = app_source[helper_start:helper_end]

        for marker in [
            "function isReplayGapStreamItem",
            'item.type === "replay_gap"',
            'item.event_type === "stream.replay_gap"',
            "async function refreshSessionFromHistoryForRequest",
            "message.requestId === requestId",
            "return scopedFinal",
            "function handleReplayGapStreamItem",
        ]:
            self.assertIn(marker, app_source)
        for marker in [
            'markStreamTerminal(sessionId, requestId, "failed")',
            "finishSessionRequest(sessionId, requestId)",
            "void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) =>",
            "Response stream history expired",
            'label: "stream_replay_gap"',
            "requestedLastEventId",
            "retainedFromEventId",
            "nextEventId",
        ]:
            self.assertIn(marker, helper_source)
        self.assertGreaterEqual(app_source.count("handleReplayGapStreamItem("), 3)
        self.assertGreaterEqual(app_source.count("if (isReplayGapStreamItem(item))"), 2)

    def test_v019_retry_prepare_returns_safe_manual_draft(self):
        with isolated_run_ledger():
            from agent.protocol import get_run_ledger
            from channel.web.web_channel import WebChannel

            ledger = get_run_ledger()
            ledger.create_run(
                "req-v019-retry",
                "session-v019-retry",
                phase="running",
                status="running",
                metadata={
                    "visible_message": "please rebuild the report",
                    "client_attempt_id": "attempt-v019",
                    "interrupts_request_id": "req-old",
                    "retry_of_request_id": "",
                    "attachment_items": [
                        {
                            "file_path": "C:/tmp/input.png",
                            "file_name": "input.png",
                            "file_type": "image",
                        }
                    ],
                },
            )
            ledger.mark_terminal(
                "req-v019-retry",
                "failed",
                reason="model_error",
                error_code="MODEL_ERROR",
                error_message="transient failure",
            )

            result = WebChannel().prepare_request_retry("req-v019-retry", session_id="session-v019-retry")

            self.assertEqual(result["status"], "success")
            self.assertTrue(result["retryable"])
            self.assertTrue(result["recoverable"])
            self.assertTrue(result["exactReplay"])
            self.assertEqual(result["prompt"], "please rebuild the report")
            self.assertEqual(result["attachments"][0]["file_name"], "input.png")

    def test_v019_retry_prepare_history_fallback_is_not_exact_replay(self):
        with isolated_run_ledger():
            from agent.protocol import get_run_ledger
            from channel.web.web_channel import WebChannel

            test_case = self

            class FakeConversationStore:
                def get_visible_user_message(self, session_id):
                    test_case.assertEqual(session_id, "session-v019-history-fallback")
                    return {"text": "latest visible request", "seq": 7}

            ledger = get_run_ledger()
            ledger.create_run(
                "req-v019-history-fallback",
                "session-v019-history-fallback",
                phase="running",
                status="running",
                metadata={},
            )
            ledger.mark_terminal(
                "req-v019-history-fallback",
                "failed",
                reason="network_error",
                error_code="NETWORK_ERROR",
                error_message="transient failure",
            )

            with patch("agent.memory.get_conversation_store", return_value=FakeConversationStore()):
                result = WebChannel().prepare_request_retry(
                    "req-v019-history-fallback",
                    session_id="session-v019-history-fallback",
                )

            self.assertEqual(result["status"], "success")
            self.assertTrue(result["retryable"])
            self.assertFalse(result["exactReplay"])
            self.assertEqual(result["prompt"], "latest visible request")
            self.assertEqual(result["source_user_seq"], 7)

    def test_v019_dead_owner_session_lock_recovery_marks_active_run_interrupted(self):
        with isolated_run_ledger(), tempfile.TemporaryDirectory() as workspace:
            import channel.web.web_channel as web_channel
            from agent.protocol import get_run_ledger
            from common.ecorex_workspace import LOCK_STALE_SECONDS, SessionLock
            from channel.web.web_channel import WebChannel

            session_id = "session-v019-dead-lock"
            request_id = "req-v019-dead-lock"
            ledger = get_run_ledger()
            ledger.create_run(request_id, session_id, phase="running", status="running", metadata={})

            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "createdAt": int(time.time()) - LOCK_STALE_SECONDS - 10,
                }),
                encoding="utf-8",
            )
            stale_time = time.time() - LOCK_STALE_SECONDS - 10
            os.utime(lock.path, (stale_time, stale_time))

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                interrupted = WebChannel()._recover_interrupted_runs_for_removed_session_locks(session_id)

            row = ledger.get_run(request_id)
            self.assertEqual(interrupted, [request_id])
            self.assertFalse(lock.path.exists())
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["error_code"], "SIDECAR_INTERRUPTED")

    def test_v019_post_message_reports_accepted_after_dead_owner_recovery(self):
        with isolated_run_ledger(), tempfile.TemporaryDirectory() as workspace:
            import channel.web.web_channel as web_channel
            from agent.protocol import get_run_ledger
            from bridge.context import Context
            from channel.web.web_channel import WebChannel
            from common.ecorex_workspace import LOCK_STALE_SECONDS, SessionLock

            session_id = "session-v019-dead-lock-admission"
            old_request_id = "req-v019-dead-lock-admission-old"
            new_request_id = "req-v019-dead-lock-admission-new"
            ledger = get_run_ledger()
            ledger.create_run(old_request_id, session_id, phase="running", status="running", metadata={})

            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "createdAt": int(time.time()) - LOCK_STALE_SECONDS - 10,
                }),
                encoding="utf-8",
            )
            stale_time = time.time() - LOCK_STALE_SECONDS - 10
            os.utime(lock.path, (stale_time, stale_time))

            channel = WebChannel()
            produced = threading.Event()
            payload = {
                "session_id": session_id,
                "message": "continue after runtime restart",
                "stream": False,
            }

            def fake_compose_context(ctype, content, **kwargs):
                context = Context(ctype, content)
                context.kwargs = kwargs
                return context

            def fake_produce(context):
                session_lock = context.get("session_lock")
                if session_lock:
                    session_lock.release()
                produced.set()

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                with patch.object(channel, "_generate_request_id", return_value=new_request_id):
                    with patch.object(channel, "_compose_context", side_effect=fake_compose_context):
                        with patch.object(channel, "produce", side_effect=fake_produce):
                            with patch.object(
                                web_channel.web,
                                "data",
                                return_value=json.dumps(payload).encode("utf-8"),
                            ):
                                result = json.loads(channel.post_message())

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["request_id"], new_request_id)
            self.assertEqual(result["same_session"]["decision"], "accepted_after_recovery")
            self.assertEqual(result["same_session"]["reason"], "dead_owner_lock_recovered")
            self.assertEqual(result["same_session"]["active_request_ids"], [old_request_id])
            self.assertEqual(result["same_session"]["replaced_request_ids"], [old_request_id])
            self.assertTrue(produced.wait(timeout=2))
            self.assertFalse(lock.path.exists())

            old_row = ledger.get_run(old_request_id)
            self.assertEqual(old_row["status"], "interrupted")
            self.assertEqual(old_row["error_code"], "SIDECAR_INTERRUPTED")

    def test_v019_live_stale_session_lock_recovery_does_not_interrupt_active_run(self):
        with isolated_run_ledger(), tempfile.TemporaryDirectory() as workspace:
            import channel.web.web_channel as web_channel
            from agent.protocol import get_run_ledger
            from channel.web.web_channel import WebChannel
            from common.ecorex_workspace import LOCK_STALE_SECONDS, SessionLock

            session_id = "session-v019-live-stale-lock"
            request_id = "req-v019-live-stale-lock"
            ledger = get_run_ledger()
            ledger.create_run(request_id, session_id, phase="running", status="running", metadata={})

            lock = SessionLock(workspace, session_id)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            lock.path.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "createdAt": int(time.time()) - LOCK_STALE_SECONDS - 10,
                }),
                encoding="utf-8",
            )
            stale_time = time.time() - LOCK_STALE_SECONDS - 10
            os.utime(lock.path, (stale_time, stale_time))

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                interrupted = WebChannel()._recover_interrupted_runs_for_removed_session_locks(session_id)

            row = ledger.get_run(request_id)
            self.assertEqual(interrupted, [])
            self.assertTrue(lock.path.exists())
            self.assertEqual(row["status"], "running")

    def test_v019_frontend_sources_have_recovery_and_hidden_run_center_markers(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        message_source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

        for marker in [
            "RUN_CENTER_DEV_GATE_STORAGE_KEY",
            "runCenterDevVisible &&",
            "SIDEBAR_COLLAPSE_STORAGE_KEY",
            "latestSendAttemptRef",
            "restoreUnacceptedDraft",
            "prepareRetryDraft",
            "message-recovery-actions",
            "showChatFileMenu",
            "verifyAddableChatFile",
            "fixedMenuStyle",
            "statLocalPath(resolvedPath)",
            "normalizeAttachmentDedupeKey",
        ]:
            self.assertIn(marker, app_source)
        for marker in [
            "createPortal",
            "artifact-action-menu-portal",
            "ARTIFACT_PENDING_MAX_RETRIES = 6",
            "artifactActionAllowed(status: ArtifactAvailability)",
            "return status === \"ready\";",
            "artifactPreviewAllowed(status: ArtifactAvailability)",
            "status === \"remote\"",
            "\"preview\"",
            "onLocalFileContextMenu",
        ]:
            self.assertIn(marker, message_source)
        self.assertNotIn("return status === \"ready\" || status === \"error\"", message_source)
        for marker in [
            ".artifact-action-menu-portal",
            ".message-recovery-actions",
            ".sidebar-collapse-button",
            ".project-collapse-button",
        ]:
            self.assertIn(marker, css_source)
        for marker in [
            "export type RetryPrepareResult",
            "client_attempt_id",
            "interrupts_request_id",
            "retry_of_request_id",
            "prepareRequestRetry",
            "retry_mode",
        ]:
            self.assertIn(marker, api_source)
        for marker in [
            "def prepare_request_retry",
            "RequestRetryPrepareHandler",
            "retry-prepare",
            "visible_message",
            "attachment_items",
            "_recover_interrupted_runs_for_removed_session_locks",
            "list_session_locks(_get_workspace_root(), cleanup=False)",
            "item.get(\"dead_owner\")",
            "accepted_after_recovery",
            "dead_owner_lock_recovered",
            "_is_within_directory(upload_dir, full_path)",
            "retry_suppressed_reason",
            "model_retry_suppressed_stream_output_started",
        ]:
            self.assertIn(marker, web_source)
        self.assertNotIn("full_path.startswith(os.path.abspath(upload_dir))", web_source)
        self.assertIn("requested_last_event_id?: number;", api_source)
        self.assertIn("retained_from_event_id?: number;", api_source)
        self.assertIn("next_event_id?: number;", api_source)
        self.assertIn('item.type === "replay_gap"', api_source)
        for marker in [
            "markStreamConnectionInterrupted",
            "markStreamReconnectExhausted",
            "streamFailureRecovery",
            "stream_reconnect_exhausted",
            "active_stream_unavailable",
            "stop_before_retry",
            "activeStillRunning: true",
            "stopAllowed",
            "Network interrupted after output started",
        ]:
            self.assertIn(marker, app_source)
        self.assertIn('"retry_mode": "manual_retry_prepare" if retryable else "unavailable"', (root / "agent" / "protocol" / "agent_stream.py").read_text(encoding="utf-8"))
        self.assertNotIn('"retry_mode": "manual_retry_prepare" if retry_stopped else "auto_retry"', (root / "agent" / "protocol" / "agent_stream.py").read_text(encoding="utf-8"))

    def test_v018_desktop_handles_sidecar_interrupted_stream_recovery(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

        helper_start = app_source.index("function handleInterruptedStreamItem")
        helper_end = app_source.index("function finishRunningSteps", helper_start)
        helper_source = app_source[helper_start:helper_end]

        for marker in [
            '"interrupted";',
            "function isInterruptedStreamItem",
            'item.type === "interrupted"',
            'item.event_type === "run.interrupted"',
            'item.state === "interrupted"',
            "function handleInterruptedStreamItem",
        ]:
            self.assertIn(marker, app_source)
        for marker in [
            'markStreamTerminal(sessionId, requestId, "interrupted")',
            "finishSessionRequest(sessionId, requestId)",
            "void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) =>",
            "Runtime sidecar restarted before this run reached a terminal state",
            'label: "stream_interrupted"',
            "terminalReason",
            "errorCode",
        ]:
            self.assertIn(marker, helper_source)
        self.assertGreaterEqual(app_source.count("handleInterruptedStreamItem("), 3)
        self.assertGreaterEqual(app_source.count("if (isInterruptedStreamItem(item))"), 2)
        self.assertIn('item.type === "interrupted"', api_source)

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
                "OpenAI default mode uses `gpt-image-2-pro` only\n\"output_format\"\n"
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
                "OpenAI default mode uses `gpt-image-2-pro` only\n\"output_format\"\n"
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

    def test_scheduler_run_ledger_records_active_and_terminal_agent_task(self):
        from agent.protocol import get_run_ledger
        from bridge.reply import Reply, ReplyType
        from channel.web import web_channel

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            def __init__(self):
                self.session_queues = {"web-session-ledger": Queue()}
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        class FakeAgentBridge:
            def __init__(self):
                self.request_id = ""
                self.session_id = ""
                self.running_run = None
                self.active_snapshot = None
                self.remembered = None

            def agent_reply(self, _content, context=None, **_kwargs):
                self.request_id = context.get("request_id", "")
                self.session_id = context.get("session_id", "")
                self.running_run = ledger.get_run(self.request_id)
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    self.active_snapshot = web_channel.WebChannel().active_requests_snapshot()
                return Reply(ReplyType.TEXT, "scheduled result")

            def remember_scheduled_output(self, session_id, content, **kwargs):
                self.remembered = (session_id, content, kwargs)

        task = {
            "id": "task-ledger-agent",
            "name": "Nightly summary",
            "schedule": {"type": "once"},
            "action": {
                "type": "agent_task",
                "task_description": "summarize project state",
                "receiver": "web-session-ledger",
                "channel_type": "web",
            },
        }

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with tempfile.TemporaryDirectory() as workspace:
                fake_channel = FakeChannel()
                fake_bridge = FakeAgentBridge()
                with patch("channel.channel_factory.create_channel", return_value=fake_channel), \
                        patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True):
                    ok = scheduler_integration._execute_scheduled_task(task, fake_bridge)

                self.assertTrue(ok)
                self.assertEqual(len(fake_channel.sent), 1)
                self.assertEqual(fake_channel.request_to_session[fake_bridge.request_id], "web-session-ledger")
                self.assertEqual(fake_bridge.session_id, "scheduler_web-session-ledger_task-ledger-agent")
                self.assertEqual(fake_bridge.running_run["run_type"], "scheduler")
                self.assertEqual(fake_bridge.running_run["status"], "running")
                self.assertEqual(fake_bridge.running_run["phase"], "agent_task_running")

                active = [
                    item for item in fake_bridge.active_snapshot["requests"]
                    if item.get("request_id") == fake_bridge.request_id
                ]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["run_type"], "scheduler")
                self.assertEqual(active[0]["state"], "running")
                self.assertEqual(active[0]["parent_id"], "web-session-ledger")

                final = ledger.get_run(fake_bridge.request_id)
                self.assertEqual(final["status"], "completed")
                self.assertEqual(final["phase"], "completed")
                self.assertEqual(final["terminal_reason"], "scheduler_completed")
                self.assertEqual(final["metadata"]["task_id"], "task-ledger-agent")
                self.assertEqual(final["metadata"]["action_type"], "agent_task")
                self.assertEqual(ledger.active_snapshot(), [])
                self.assertEqual(fake_bridge.remembered[0], "web-session-ledger")

    def test_scheduler_run_ledger_records_permission_denied_terminal_state(self):
        from agent.protocol import get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            session_queues = {"web-session-denied": Queue()}

        task = {
            "id": "task-ledger-denied",
            "name": "Denied reminder",
            "action": {
                "type": "send_message",
                "content": "hello",
                "receiver": "web-session-denied",
                "channel_type": "web",
            },
        }

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with patch("channel.channel_factory.create_channel", return_value=FakeChannel()), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=False):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            self.assertTrue(ok)
            request_id = task["_scheduler_run_request_id"]
            final = ledger.get_run(request_id)
            self.assertEqual(final["run_type"], "scheduler")
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "scheduler_permission_denied")
            self.assertEqual(final["error_code"], "SCHEDULER_PERMISSION_DENIED")
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_run_ledger_uses_new_request_id_for_reused_task_dict(self):
        from agent.protocol import get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            def __init__(self):
                self.session_queues = {"web-session-repeat": Queue()}
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        task = {
            "id": "task-ledger-repeat",
            "name": "Repeat reminder",
            "action": {
                "type": "send_message",
                "content": "hello",
                "receiver": "web-session-repeat",
                "channel_type": "web",
            },
        }

        with isolated_run_ledger():
            ledger = get_run_ledger()
            fake_channel = FakeChannel()
            with patch("channel.channel_factory.create_channel", return_value=fake_channel), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True):
                first_ok = scheduler_integration._execute_scheduled_task(task, object())
                first_request_id = task["_scheduler_run_request_id"]
                second_ok = scheduler_integration._execute_scheduled_task(task, object())
                second_request_id = task["_scheduler_run_request_id"]

            self.assertTrue(first_ok)
            self.assertTrue(second_ok)
            self.assertNotEqual(first_request_id, second_request_id)
            self.assertEqual(ledger.get_run(first_request_id)["status"], "completed")
            self.assertEqual(ledger.get_run(second_request_id)["status"], "completed")
            self.assertEqual(len(fake_channel.sent), 2)
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_cancel_token_is_registered_before_run_is_visible(self):
        from agent.protocol import get_cancel_registry, get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            session_queues = {"web-session-first-visible-cancel": Queue()}

        task = {
            "id": "task-ledger-first-visible-cancel",
            "name": "First visible cancel",
            "action": {
                "type": "send_message",
                "content": "hello",
                "receiver": "web-session-first-visible-cancel",
                "channel_type": "web",
            },
        }

        original_mark_created = scheduler_integration._mark_scheduler_run_created

        def assert_token_then_mark(action_task, request_id):
            self.assertIsNotNone(get_cancel_registry().get_event(request_id))
            self.assertTrue(get_cancel_registry().cancel_request(request_id))
            original_mark_created(action_task, request_id)

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with patch("channel.channel_factory.create_channel", return_value=FakeChannel()), \
                    patch.object(scheduler_integration, "_mark_scheduler_run_created", side_effect=assert_token_then_mark), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            request_id = task["_scheduler_run_request_id"]
            self.assertTrue(ok)
            final = ledger.get_run(request_id)
            self.assertEqual(final["run_type"], "scheduler")
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_agent_task_cancel_writes_cancelled_terminal_state(self):
        from agent.protocol import get_cancel_registry, get_run_ledger
        from bridge.reply import Reply, ReplyType

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            def __init__(self):
                self.session_queues = {"web-session-cancel": Queue()}
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        class FakeAgentBridge:
            def agent_reply(self, _content, context=None, **_kwargs):
                request_id = context.get("request_id", "")
                self.request_id = request_id
                self.cancelled = get_cancel_registry().cancel_request(request_id)
                return Reply(ReplyType.TEXT, "_(Cancelled)_")

        task = {
            "id": "task-ledger-agent-cancel",
            "name": "Cancelled summary",
            "action": {
                "type": "agent_task",
                "task_description": "summarize project state",
                "receiver": "web-session-cancel",
                "channel_type": "web",
            },
        }

        with isolated_run_ledger():
            ledger = get_run_ledger()
            fake_channel = FakeChannel()
            fake_bridge = FakeAgentBridge()
            with patch("channel.channel_factory.create_channel", return_value=fake_channel), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True):
                ok = scheduler_integration._execute_scheduled_task(task, fake_bridge)

            self.assertTrue(ok)
            self.assertTrue(fake_bridge.cancelled)
            final = ledger.get_run(fake_bridge.request_id)
            self.assertEqual(final["run_type"], "scheduler")
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertEqual(final["error_code"], "SCHEDULER_CANCELLED")
            self.assertIsNone(get_cancel_registry().get_event(fake_bridge.request_id))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_agentbridge_token_survives_until_delivery(self):
        from agent.protocol import get_cancel_registry, get_run_ledger
        from bridge.agent_bridge import AgentBridge

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

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
                return "scheduled response"

        class FakeChannel:
            def __init__(self):
                self.session_queues = {"web-session-agentbridge-owner": Queue()}
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                request_id = context.get("request_id")
                self.event_at_delivery = get_cancel_registry().get_event(request_id)
                self.cancelled_at_delivery = get_cancel_registry().cancel_request(request_id)
                self.sent.append((reply, context))

        task = {
            "id": "task-ledger-agentbridge-owner",
            "name": "AgentBridge owned by scheduler",
            "action": {
                "type": "agent_task",
                "task_description": "summarize project state",
                "receiver": "web-session-agentbridge-owner",
                "channel_type": "web",
            },
        }

        with isolated_run_ledger():
            ledger = get_run_ledger()
            fake_agent = FakeAgent()
            fake_channel = FakeChannel()
            bridge = AgentBridge.__new__(AgentBridge)
            bridge.get_agent = lambda session_id=None: fake_agent
            bridge._trim_in_memory_to_turns = lambda *args, **kwargs: None
            bridge._pre_persist_user_message = lambda *args, **kwargs: False
            bridge._persist_messages = lambda *args, **kwargs: None
            bridge._schedule_mcp_hot_reload = lambda *args, **kwargs: None
            with patch("channel.channel_factory.create_channel", return_value=fake_channel), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True):
                ok = scheduler_integration._execute_scheduled_task(task, bridge)

            request_id = task["_scheduler_run_request_id"]
            self.assertTrue(ok)
            self.assertIs(fake_agent.cancel_event, fake_channel.event_at_delivery)
            self.assertTrue(fake_channel.cancelled_at_delivery)
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_skill_call_marks_scheduler_cancel_token_owner(self):
        from bridge.reply import Reply, ReplyType

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeBridge:
            def agent_reply(self, _query, context=None, **_kwargs):
                self.context = context
                return Reply(ReplyType.TEXT, "skill output")

        class FakeChannel:
            def __init__(self):
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        task = {
            "id": "task-skill-owner",
            "name": "Skill owner",
            "action": {
                "type": "skill_call",
                "skill_name": "diagnostics",
                "skill_params": {"scope": "runtime"},
                "receiver": "web-session-skill-owner",
                "channel_type": "web",
            },
        }

        fake_bridge = FakeBridge()
        fake_channel = FakeChannel()
        with patch("channel.channel_factory.create_channel", return_value=fake_channel):
            ok = scheduler_integration._execute_skill_call(task, fake_bridge)

        self.assertTrue(ok)
        self.assertEqual(fake_bridge.context.get("cancel_token_owner"), "scheduler")
        self.assertEqual(fake_bridge.context.get("request_id"), task["_scheduler_run_request_id"])
        self.assertEqual(fake_channel.request_to_session[task["_scheduler_run_request_id"]], "web-session-skill-owner")

    def test_scheduler_tool_cancel_is_visible_then_terminal_cancelled(self):
        from agent.protocol import get_cancel_registry, get_run_ledger
        from agent.tools.base_tool import ToolResult
        from channel.web import web_channel

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            def __init__(self):
                self.session_queues = {"web-session-tool-cancel": Queue()}
                self.request_to_session = {}
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        class FakeTool:
            name = "fake-long-tool"

            def execute(self, _params):
                self.saw_cancel_event = hasattr(self, "cancel_event")
                request_id = task["_scheduler_run_request_id"]
                self.cancelled = get_cancel_registry().cancel_request(request_id)
                self.cancel_event_set = self.cancel_event.is_set()
                with tempfile.TemporaryDirectory() as workspace:
                    with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                        self.active_snapshot = web_channel.WebChannel().active_requests_snapshot()
                return ToolResult.fail("cancelled")

        task = {
            "id": "task-ledger-tool-cancel",
            "name": "Cancelled tool",
            "action": {
                "type": "tool_call",
                "tool_name": "fake-long-tool",
                "tool_params": {"seconds": 60},
                "receiver": "web-session-tool-cancel",
                "channel_type": "web",
            },
        }
        fake_tool = FakeTool()

        with isolated_run_ledger():
            ledger = get_run_ledger()
            fake_channel = FakeChannel()
            with patch("channel.channel_factory.create_channel", return_value=fake_channel), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True), \
                    patch("agent.tools.tool_manager.ToolManager.create_tool", return_value=fake_tool), \
                    patch.object(scheduler_integration, "_authorize_scheduled_tool_call", return_value=True):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            request_id = task["_scheduler_run_request_id"]
            self.assertTrue(ok)
            self.assertTrue(fake_tool.saw_cancel_event)
            self.assertTrue(fake_tool.cancelled)
            self.assertTrue(fake_tool.cancel_event_set)
            active = [
                item for item in fake_tool.active_snapshot["requests"]
                if item.get("request_id") == request_id
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["run_type"], "scheduler")
            self.assertTrue(active[0]["cancelled"])
            self.assertEqual(active[0]["state"], "cancelling")
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertFalse(hasattr(fake_tool, "cancel_event"))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_cancelled_failed_action_consumes_attempt_without_retry(self):
        from agent.protocol import get_cancel_registry, get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            session_queues = {"web-session-cancel-false": Queue()}

        task = {
            "id": "task-ledger-cancel-false",
            "name": "Cancelled failed action",
            "action": {
                "type": "tool_call",
                "tool_name": "fake-long-tool",
                "tool_params": {"seconds": 60},
                "receiver": "web-session-cancel-false",
                "channel_type": "web",
            },
        }

        def cancel_and_fail(action_task, _agent_bridge):
            request_id = action_task["_scheduler_run_request_id"]
            self.assertTrue(get_cancel_registry().cancel_request(request_id))
            return False

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with patch("channel.channel_factory.create_channel", return_value=FakeChannel()), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True), \
                    patch.object(scheduler_integration, "_execute_tool_call", side_effect=cancel_and_fail):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            request_id = task["_scheduler_run_request_id"]
            self.assertTrue(ok)
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertEqual(final["error_code"], "SCHEDULER_CANCELLED")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_cancelled_exception_writes_cancelled_terminal_state(self):
        from agent.protocol import get_cancel_registry, get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            session_queues = {"web-session-cancel-exception": Queue()}

        task = {
            "id": "task-ledger-cancel-exception",
            "name": "Cancelled exception",
            "action": {
                "type": "agent_task",
                "task_description": "summarize project state",
                "receiver": "web-session-cancel-exception",
                "channel_type": "web",
            },
        }

        def cancel_and_raise(action_task, _agent_bridge):
            request_id = action_task["_scheduler_run_request_id"]
            self.assertTrue(get_cancel_registry().cancel_request(request_id))
            raise RuntimeError("tool noticed cancellation")

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with patch("channel.channel_factory.create_channel", return_value=FakeChannel()), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True), \
                    patch.object(scheduler_integration, "_execute_agent_task", side_effect=cancel_and_raise):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            request_id = task["_scheduler_run_request_id"]
            self.assertTrue(ok)
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["terminal_reason"], "scheduler_cancelled")
            self.assertEqual(final["error_code"], "SCHEDULER_CANCELLED")
            self.assertIsNone(get_cancel_registry().get_event(request_id))
            self.assertEqual(ledger.active_snapshot(), [])

    def test_scheduler_run_ledger_preserves_tool_permission_denied_terminal_state(self):
        from agent.protocol import get_run_ledger

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"croniter": fake_croniter}):
            from agent.tools.scheduler import integration as scheduler_integration

        class FakeChannel:
            session_queues = {"web-session-tool-denied": Queue()}

        task = {
            "id": "task-ledger-tool-denied",
            "name": "Denied tool",
            "action": {
                "type": "tool_call",
                "tool_name": "bash",
                "tool_params": {"command": "echo blocked"},
                "receiver": "web-session-tool-denied",
                "channel_type": "web",
            },
        }
        fake_tool = types.SimpleNamespace(name="bash")

        with isolated_run_ledger():
            ledger = get_run_ledger()
            with patch("channel.channel_factory.create_channel", return_value=FakeChannel()), \
                    patch.object(scheduler_integration, "_authorize_scheduled_execution", return_value=True), \
                    patch("agent.tools.tool_manager.ToolManager.create_tool", return_value=fake_tool), \
                    patch.object(scheduler_integration, "_authorize_scheduled_tool_call", return_value=False):
                ok = scheduler_integration._execute_scheduled_task(task, object())

            self.assertTrue(ok)
            request_id = task["_scheduler_run_request_id"]
            final = ledger.get_run(request_id)
            self.assertEqual(final["run_type"], "scheduler")
            self.assertEqual(final["status"], "failed")
            self.assertEqual(final["terminal_reason"], "scheduler_tool_permission_denied")
            self.assertEqual(final["error_code"], "SCHEDULER_TOOL_PERMISSION_DENIED")
            self.assertEqual(ledger.active_snapshot(), [])

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
