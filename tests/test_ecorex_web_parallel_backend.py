# encoding:utf-8
import ast
import hashlib
import json
import importlib
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


def python_function_literal_return(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for statement in node.body:
                if (
                    isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    return statement.value.value
    raise AssertionError(f"{function_name} literal return not found in {path.name}")


@contextmanager
def isolated_run_ledger():
    from agent.protocol import reset_run_event_ledger_for_tests, reset_run_ledger_for_tests

    with tempfile.TemporaryDirectory() as workspace:
        db_path = Path(workspace) / "run-ledger.db"
        reset_run_ledger_for_tests(db_path)
        reset_run_event_ledger_for_tests(db_path)
        try:
            yield
        finally:
            reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")
            reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-event-ledger-test-reset.db")


class TestV022RunEventLedger(unittest.TestCase):
    def test_v022_run_event_ledger_appends_replays_and_projects_request(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            first = ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="run.accepted",
                payload={"status": "running"},
                idempotency_key="req-v022-ledger:accepted",
            )
            duplicate = ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="run.accepted",
                payload={"status": "running"},
                idempotency_key="req-v022-ledger:accepted",
            )
            ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="message.user.accepted",
                payload={"content": "hello"},
                idempotency_key="req-v022-ledger:user",
            )
            ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="message.assistant.created",
                payload={},
                idempotency_key="req-v022-ledger:assistant-created",
            )
            ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="assistant.delta",
                payload={"content": "partial"},
                idempotency_key="req-v022-ledger:delta-1",
            )
            ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="message.assistant.finalized",
                payload={"content": "final answer"},
                idempotency_key="req-v022-ledger:final",
            )
            ledger.append_event(
                request_id="req-v022-ledger",
                session_id="session-v022",
                turn_id="turn-v022",
                event_type="run.completed",
                payload={"terminal_reason": "done"},
                idempotency_key="req-v022-ledger:completed",
            )

            events = ledger.events_for_request("req-v022-ledger")
            limited_events = ledger.events_for_request("req-v022-ledger", limit=2)
            full_events = ledger.events_for_request("req-v022-ledger", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-v022-ledger")

        self.assertEqual(first["event_id"], duplicate["event_id"])
        self.assertEqual([event["event_seq"] for event in events], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(limited_events), 2)
        self.assertEqual(len(full_events), 6)
        self.assertEqual([event["event_type"] for event in events][0], "run.accepted")
        self.assertEqual(projection["state"], "completed")
        self.assertEqual(projection["messages"][0]["content"], "hello")
        self.assertEqual(projection["messages"][1]["content"], "final answer")
        self.assertFalse(projection["messages"][1]["pending"])

    def test_v022_run_event_ledger_replays_by_session_cursor(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            first = ledger.append_event(
                request_id="req-v022-cursor-a",
                session_id="session-v022-cursor",
                event_type="run.accepted",
                payload={},
                idempotency_key="req-v022-cursor-a:accepted",
            )
            ledger.append_event(
                request_id="req-v022-cursor-b",
                session_id="session-v022-cursor",
                event_type="run.accepted",
                payload={},
                idempotency_key="req-v022-cursor-b:accepted",
            )
            ledger.append_event(
                request_id="req-v022-cursor-b",
                session_id="session-v022-cursor",
                event_type="message.assistant.finalized",
                payload={"content": "cursor final"},
                idempotency_key="req-v022-cursor-b:final",
            )
            after_first = ledger.list_events(
                session_id="session-v022-cursor",
                after_event_id=first["event_id"],
            )
            session_projection = RuntimeProjectionService(ledger).session_projection("session-v022-cursor")

        self.assertEqual([event["request_id"] for event in after_first], ["req-v022-cursor-b", "req-v022-cursor-b"])
        self.assertEqual(session_projection["latest_event_id"], after_first[-1]["event_id"])
        self.assertEqual(
            [request["request_id"] for request in session_projection["requests"]],
            ["req-v022-cursor-a", "req-v022-cursor-b"],
        )

    def test_v022_session_history_projection_overlays_runtime_truth(self):
        from agent.memory.conversation_store import ConversationStore
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-v022-history-projection", [
                {"role": "user", "content": "make image"},
                {"role": "assistant", "content": "old draft"},
            ], channel_type="web")
            store.attach_extras_to_assistant_seq("session-v022-history-projection", 1, {
                "request_id": "req-v022-history-projection",
                "turn_id": "turn-v022-history-projection",
                "user_seq": 0,
                "bot_seq": 1,
            })
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "events.sqlite3")
            ledger.append_event(
                request_id="req-v022-history-projection",
                session_id="session-v022-history-projection",
                turn_id="turn-v022-history-projection",
                event_type="message.user.accepted",
                payload={"content": "make image"},
                source="test",
            )
            ledger.append_event(
                request_id="req-v022-history-projection",
                session_id="session-v022-history-projection",
                turn_id="turn-v022-history-projection",
                event_type="artifact.created",
                payload={"artifact": {"title": "final.png", "path": r"C:\tmp\final.png", "kind": "image"}},
                source="test",
            )
            ledger.append_event(
                request_id="req-v022-history-projection",
                session_id="session-v022-history-projection",
                turn_id="turn-v022-history-projection",
                event_type="message.assistant.finalized",
                payload={"content": "runtime final"},
                source="test",
            )
            projection = RuntimeProjectionService(ledger).session_history_projection(
                "session-v022-history-projection",
                page=1,
                page_size=20,
                history_store=store,
            )

        history = projection["history"]
        assistant = next(item for item in history["messages"] if item["role"] == "assistant")
        self.assertEqual(projection["history_source"], "conversation_store+runtime_projection")
        self.assertEqual(assistant["content"], "runtime final")
        self.assertEqual(assistant["runtime_projection"]["state"], "completed")
        self.assertEqual(assistant["runtime_projection"]["latest_event_id"], projection["latest_event_id"])
        self.assertEqual(assistant["extras"]["artifacts"][0]["title"], "final.png")
        self.assertEqual(history["runtime_projection"]["request_count"], 1)

    def test_v022_session_history_projection_limits_runtime_requests_to_page(self):
        from agent.memory.conversation_store import ConversationStore
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        session_id = "session-v022-history-page-scope"
        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            with patch("agent.memory.conversation_store.time.time", return_value=1000):
                store.append_messages(session_id, [
                    {"role": "user", "content": "old page prompt"},
                    {"role": "assistant", "content": "old stale history"},
                ], channel_type="web")
            store.attach_extras_to_assistant_seq(session_id, 1, {
                "request_id": "req-history-page-old",
                "turn_id": "turn-history-page-old",
                "user_seq": 0,
                "bot_seq": 1,
            })
            with patch("agent.memory.conversation_store.time.time", return_value=2000):
                store.append_messages(session_id, [
                    {"role": "user", "content": "new page prompt"},
                    {"role": "assistant", "content": "new stale history"},
                ], channel_type="web")
            store.attach_extras_to_assistant_seq(session_id, 3, {
                "request_id": "req-history-page-new",
                "turn_id": "turn-history-page-new",
                "user_seq": 2,
                "bot_seq": 3,
            })

            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "events.sqlite3")
            for request_id, turn_id, prompt, final_text, created_at in (
                ("req-history-page-old", "turn-history-page-old", "old page prompt", "old runtime final", 1000),
                ("req-history-page-new", "turn-history-page-new", "new page prompt", "new runtime final", 2000),
            ):
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    event_type="message.user.accepted",
                    payload={"content": prompt},
                    source="test",
                    created_at=created_at,
                )
                ledger.append_event(
                    request_id=request_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    event_type="message.assistant.finalized",
                    payload={"content": final_text},
                    source="test",
                    created_at=created_at + 1,
                )

            service = RuntimeProjectionService(ledger)
            page_one = service.session_history_projection(
                session_id,
                page=1,
                page_size=2,
                history_store=store,
            )
            page_two = service.session_history_projection(
                session_id,
                page=2,
                page_size=2,
                history_store=store,
            )

        page_one_request_ids = [request["request_id"] for request in page_one["requests"]]
        page_two_request_ids = [request["request_id"] for request in page_two["requests"]]
        page_one_text = "\n".join(str(item.get("content") or "") for item in page_one["history"]["messages"])
        page_two_text = "\n".join(str(item.get("content") or "") for item in page_two["history"]["messages"])

        self.assertEqual(page_one_request_ids, ["req-history-page-new"])
        self.assertEqual(page_two_request_ids, ["req-history-page-old"])
        self.assertIn("new runtime final", page_one_text)
        self.assertNotIn("old runtime final", page_one_text)
        self.assertIn("old runtime final", page_two_text)
        self.assertNotIn("new runtime final", page_two_text)
        self.assertEqual(page_one["history"]["runtime_projection"]["request_count"], 1)
        self.assertEqual(page_two["history"]["runtime_projection"]["request_count"], 1)
        self.assertGreater(page_one["latest_event_id"], page_two["requests"][0]["latest_event_id"])

    def test_v022_image_job_service_emits_incremental_artifacts_and_projection(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                self.assertFalse(cancel_event.is_set())
                emit_progress(
                    "provider_request",
                    progress=0.5,
                    detail={
                        "provider": "openai",
                        "resolved_model": "gpt-image-2-pro",
                        "api_key": "sk-test-secret",
                    },
                )
                return {
                    "path": str(Path(workspace) / f"{task['task_id']}.png"),
                    "title": f"{task['task_id']}.png",
                    "kind": "image",
                }

            service.start(
                request_id="req-image-job",
                session_id="session-image-job",
                operation="generate",
                tasks=[
                    {"task_id": "image-1", "operation": "generate", "output_count": 1},
                    {"task_id": "image-2", "operation": "generate", "output_count": 1},
                ],
                runner=runner,
                metadata={
                    "provider": "openai",
                    "resolved_model": "gpt-image-2-pro",
                    "api_key": "sk-live-secret",
                },
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-job", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-job")

        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types[0], "image_job.started")
        self.assertEqual(event_types[-1], "image_job.completed")
        self.assertEqual(event_types.count("artifact.created"), 2)
        self.assertEqual(event_types.count("image_job.artifact"), 2)
        self.assertTrue(all(event["source"] == "image_job_service" for event in events))
        self.assertNotIn("sk-live-secret", json.dumps(events, ensure_ascii=False))
        self.assertNotIn("sk-test-secret", json.dumps(events, ensure_ascii=False))
        self.assertIn("[redacted]", json.dumps(events, ensure_ascii=False))

        self.assertEqual(len(projection["image_jobs"]), 1)
        job = projection["image_jobs"][0]
        self.assertTrue(job["job_id"].startswith("image-job-"))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["operation"], "generate")
        self.assertEqual(job["artifact_count"], 2)
        self.assertEqual(len(job["artifacts"]), 2)
        self.assertEqual([task["status"] for task in job["tasks"]], ["completed", "completed"])
        self.assertEqual(projection["messages"][0]["role"], "assistant")
        self.assertEqual(len(projection["messages"][0]["artifacts"]), 2)

    def test_v022_image_job_service_cancel_is_replayable(self):
        from agent.protocol import ImageJobCancelled, ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        started = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                started.set()
                while not cancel_event.is_set():
                    time.sleep(0.01)
                raise ImageJobCancelled("cancelled by test")

            service.start(
                request_id="req-image-cancel",
                session_id="session-image-cancel",
                operation="generate",
                tasks=[{"task_id": "slow-image"}],
                runner=runner,
                job_id="image-job-cancel-test",
            )
            self.assertTrue(started.wait(timeout=2))
            cancel_status = service.cancel("image-job-cancel-test", reason="user_stop")
            collected = service.collect("image-job-cancel-test", wait=True, timeout=2)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-cancel")
            events = ledger.events_for_request("req-image-cancel", limit=0)

        self.assertTrue(cancel_status["cancelled"])
        self.assertEqual(collected["status"], "cancelled")
        event_types = [event["event_type"] for event in events]
        self.assertIn("image_job.started", event_types)
        self.assertIn("image_job.cancelled", event_types)
        self.assertEqual(projection["image_jobs"][0]["status"], "cancelled")
        self.assertIn(projection["image_jobs"][0]["cancel_reason"], {"user_stop", "cancelled by test"})

    def test_v022_image_job_service_cancel_remains_terminal_after_late_runner_events(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        started = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                started.set()
                while not cancel_event.is_set():
                    time.sleep(0.01)
                emit_progress("late_provider_progress", progress=0.9, detail={"provider": "openai"})
                raise RuntimeError("provider noticed cancel late")

            service.start(
                request_id="req-image-cancel-terminal",
                session_id="session-image-cancel-terminal",
                operation="generate",
                tasks=[{"task_id": "slow-image"}],
                runner=runner,
                job_id="image-job-cancel-terminal-test",
            )
            self.assertTrue(started.wait(timeout=2))
            service.cancel("image-job-cancel-terminal-test", reason="user_stop")
            collected = service.collect("image-job-cancel-terminal-test", wait=True, timeout=2)
            events = ledger.events_for_request("req-image-cancel-terminal", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-cancel-terminal")

        self.assertEqual(collected["status"], "cancelled")
        event_types = [event["event_type"] for event in events]
        self.assertIn("image_job.cancelled", event_types)
        self.assertNotIn("image_job.failed", event_types)
        self.assertFalse(any(event["payload"].get("status") == "late_provider_progress" for event in events))
        self.assertEqual(projection["image_jobs"][0]["status"], "cancelled")
        self.assertEqual({task["terminal_job_status"] for task in projection["image_jobs"][0]["tasks"]}, {"cancelled"})
        self.assertNotIn("running", {task.get("status") for task in projection["image_jobs"][0]["tasks"]})

    def test_v022_image_job_service_cancel_reason_does_not_persist_raw_text(self):
        from agent.protocol import ImageJobCancelled, ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        raw_text = "private prompt"
        started = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                started.set()
                while not cancel_event.is_set():
                    time.sleep(0.01)
                raise ImageJobCancelled(raw_text)

            service.start(
                request_id="req-image-safe-cancel-reason",
                session_id="session-image-safe-cancel-reason",
                operation="generate",
                tasks=[{"task_id": "safe-cancel", "operation": "generate"}],
                runner=runner,
                job_id="image-job-safe-cancel-reason",
            )
            self.assertTrue(started.wait(timeout=2))
            service.cancel("image-job-safe-cancel-reason", reason=raw_text)
            service.collect("image-job-safe-cancel-reason", wait=True, timeout=2)
            events = ledger.events_for_request("req-image-safe-cancel-reason", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-safe-cancel-reason")

        serialized_events = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("private prompt", serialized_events)
        self.assertEqual(projection["image_jobs"][0]["status"], "cancelled")
        self.assertEqual(projection["image_jobs"][0]["cancel_reason"], "cancelled")

    def test_v022_image_job_service_runs_tasks_with_bounded_parallelism(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        active = 0
        max_active = 0
        lock = threading.Lock()
        two_active = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    self.assertLessEqual(active, 2)
                    if active >= 2:
                        two_active.set()
                try:
                    emit_progress("provider_request", progress=0.25, detail={"provider": "openai"})
                    two_active.wait(timeout=1)
                    time.sleep(0.02)
                    self.assertFalse(cancel_event.is_set())
                    return {
                        "path": str(Path(workspace) / f"{task['task_id']}.png"),
                        "title": f"{task['task_id']}.png",
                        "kind": "image",
                    }
                finally:
                    with lock:
                        active -= 1

            service.start(
                request_id="req-image-parallel",
                session_id="session-image-parallel",
                operation="generate",
                tasks=[
                    {"task_id": f"parallel-{index}", "operation": "generate", "output_count": 1}
                    for index in range(4)
                ],
                runner=runner,
                metadata={"provider": "openai", "resolved_model": "gpt-image-2-pro"},
                max_parallel=2,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-parallel", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-parallel")

        started_payload = events[0]["payload"]
        self.assertEqual(started_payload["max_parallel"], 2)
        self.assertEqual(max_active, 2)
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types[0], "image_job.started")
        self.assertEqual(event_types[-1], "image_job.completed")
        self.assertEqual(event_types.count("artifact.created"), 4)
        self.assertEqual(event_types.count("image_job.artifact"), 4)
        provider_progress = [
            event for event in events
            if event["event_type"] == "image_job.progress"
            and event["payload"].get("status") == "provider_request"
        ]
        self.assertEqual(len(provider_progress), 4)

        job = projection["image_jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["artifact_count"], 4)
        self.assertEqual(len(job["artifacts"]), 4)
        self.assertEqual([task["status"] for task in job["tasks"]], ["completed"] * 4)
        self.assertEqual(len(projection["messages"][0]["artifacts"]), 4)

    def test_v022_image_job_service_parallel_failure_does_not_start_queued_tasks(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        started = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                with lock:
                    started.append(task["task_id"])
                if task["task_id"] == "task-1":
                    raise RuntimeError("first task failed")
                while not cancel_event.is_set():
                    time.sleep(0.01)
                return {
                    "path": str(Path(workspace) / f"{task['task_id']}.png"),
                    "title": f"{task['task_id']}.png",
                    "kind": "image",
                }

            status = service.start(
                request_id="req-image-parallel-failure",
                session_id="session-image-parallel-failure",
                operation="generate",
                tasks=[{"task_id": f"t{index}", "operation": "generate"} for index in range(4)],
                runner=runner,
                max_parallel=2,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-parallel-failure", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-parallel-failure")

        self.assertEqual(status["status"], "failed")
        self.assertIn("task-1", started)
        self.assertNotIn("task-3", started)
        self.assertNotIn("task-4", started)
        self.assertIn("image_job.failed", [event["event_type"] for event in events])
        self.assertEqual(projection["image_jobs"][0]["status"], "failed")
        self.assertEqual({task["terminal_job_status"] for task in projection["image_jobs"][0]["tasks"]}, {"failed"})
        self.assertNotIn("running", {task.get("status") for task in projection["image_jobs"][0]["tasks"]})

    def test_v022_image_job_service_parallel_failure_is_observable_before_sibling_exit(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        sibling_started = threading.Event()
        release_sibling = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                if task["task_id"] == "task-1":
                    self.assertTrue(sibling_started.wait(timeout=1))
                    raise RuntimeError("first task failed")
                sibling_started.set()
                release_sibling.wait(timeout=2)
                return {
                    "path": str(Path(workspace) / f"{task['task_id']}.png"),
                    "title": f"{task['task_id']}.png",
                    "kind": "image",
                }

            service.start(
                request_id="req-image-fast-failed-event",
                session_id="session-image-fast-failed-event",
                operation="generate",
                tasks=[{"task_id": f"t{index}", "operation": "generate"} for index in range(3)],
                runner=runner,
                job_id="image-job-fast-failed-event",
                max_parallel=2,
            )
            deadline = time.time() + 1
            events_before_release = []
            while time.time() < deadline:
                events_before_release = ledger.events_for_request("req-image-fast-failed-event", limit=0)
                if "image_job.failed" in [event["event_type"] for event in events_before_release]:
                    break
                time.sleep(0.02)
            projection_before_release = RuntimeProjectionService(ledger).request_projection("req-image-fast-failed-event")
            release_sibling.set()
            collected = service.collect("image-job-fast-failed-event", wait=True, timeout=3)

        event_types = [event["event_type"] for event in events_before_release]
        self.assertIn("image_job.failed", event_types)
        self.assertEqual(projection_before_release["image_jobs"][0]["status"], "failed")
        self.assertEqual(collected["status"], "failed")

    def test_v022_image_job_service_drops_artifacts_after_parallel_terminal_race(self):
        import agent.protocol.image_job_service as image_job_module
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        artifact_window = threading.Event()
        failure_emitted = threading.Event()
        original_coerce_artifacts = image_job_module._coerce_artifacts

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)
            original_emit = service._emit

            def emit_spy(state, event_type, payload, *, suffix):
                emitted = original_emit(state, event_type, payload, suffix=suffix)
                if event_type == "image_job.failed":
                    failure_emitted.set()
                return emitted

            def delayed_coerce_artifacts(result):
                if isinstance(result, dict) and result.get("title") == "slow-artifact.png":
                    artifact_window.set()
                    self.assertTrue(failure_emitted.wait(timeout=1))
                return original_coerce_artifacts(result)

            def runner(task, emit_progress, cancel_event):
                if task["task_id"] == "task-1":
                    self.assertTrue(artifact_window.wait(timeout=1))
                    raise RuntimeError("first task failed")
                return {
                    "path": str(Path(workspace) / "slow-artifact.png"),
                    "title": "slow-artifact.png",
                    "kind": "image",
                }

            service._emit = emit_spy
            with patch.object(image_job_module, "_coerce_artifacts", side_effect=delayed_coerce_artifacts):
                status = service.start(
                    request_id="req-image-terminal-artifact-race",
                    session_id="session-image-terminal-artifact-race",
                    operation="generate",
                    tasks=[{"task_id": f"t{index}", "operation": "generate"} for index in range(2)],
                    runner=runner,
                    max_parallel=2,
                    synchronous=True,
                )
            events = ledger.events_for_request("req-image-terminal-artifact-race", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-terminal-artifact-race")

        self.assertEqual(status["status"], "failed")
        event_types = [event["event_type"] for event in events]
        self.assertIn("image_job.failed", event_types)
        self.assertNotIn("artifact.created", event_types)
        self.assertNotIn("image_job.artifact", event_types)
        self.assertEqual(projection["image_jobs"][0]["status"], "failed")
        self.assertEqual(projection["image_jobs"][0].get("artifacts") or [], [])
        self.assertEqual(projection["messages"][0].get("artifacts") or [], [])

    def test_v022_image_job_service_uniquifies_duplicate_task_ids_for_artifact_events(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                emit_progress("provider_request", progress=0.25, detail={"provider": "openai"})
                return {
                    "path": str(Path(workspace) / f"{task['task_id']}.png"),
                    "title": f"{task['task_id']}.png",
                    "kind": "image",
                }

            service.start(
                request_id="req-image-duplicate-task-id",
                session_id="session-image-duplicate-task-id",
                operation="generate",
                tasks=[
                    {"task_id": "duplicate", "operation": "generate", "output_count": 1},
                    {"task_id": "duplicate", "operation": "generate", "output_count": 1},
                ],
                runner=runner,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-duplicate-task-id", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-duplicate-task-id")

        started_tasks = events[0]["payload"]["tasks"]
        self.assertEqual([task["task_id"] for task in started_tasks], ["task-1", "task-2"])
        self.assertNotIn("source_task_id", started_tasks[1])
        self.assertFalse(any(event["event_type"] == "ledger.idempotency_conflict" for event in events))
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types.count("artifact.created"), 2)
        self.assertEqual(event_types.count("image_job.artifact"), 2)

        job = projection["image_jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["artifact_count"], 2)
        self.assertEqual([task["task_id"] for task in job["tasks"]], ["task-1", "task-2"])
        self.assertEqual([task["status"] for task in job["tasks"]], ["completed", "completed"])
        self.assertEqual(len(job["artifacts"]), 2)

    def test_v022_image_job_service_artifact_events_use_stable_sanitized_dto(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        raw_b64 = "a" * 9000
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                return {
                    "path": str(Path(workspace) / "stable.png"),
                    "url": f"data:image/png;base64,{raw_b64}",
                    "fileName": "stable.png",
                    "fileType": "image",
                    "b64_json": raw_b64,
                    "provider_raw_response": {"secret": "provider-private"},
                    "nested": {"ignored": True},
                }

            service.start(
                request_id="req-image-artifact-dto",
                session_id="session-image-artifact-dto",
                operation="generate",
                tasks=[{"task_id": "dto", "operation": "generate"}],
                runner=runner,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-artifact-dto", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-artifact-dto")

        serialized_events = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("b64_json", serialized_events)
        self.assertNotIn("data:image", serialized_events)
        self.assertNotIn(raw_b64, serialized_events)
        self.assertNotIn("provider_raw_response", serialized_events)
        self.assertNotIn("provider-private", serialized_events)
        artifact_events = [event for event in events if event["event_type"] == "image_job.artifact"]
        self.assertEqual(len(artifact_events), 1)
        artifact = artifact_events[0]["payload"]["artifact"]
        self.assertEqual(artifact["title"], "stable.png")
        self.assertEqual(artifact["kind"], "image")
        self.assertTrue(artifact["artifact_sanitized"])
        self.assertGreaterEqual(artifact["omitted_field_count"], 3)
        self.assertNotIn("url", artifact)
        self.assertEqual(projection["image_jobs"][0]["artifacts"][0], artifact)

    def test_v022_image_job_service_metadata_and_progress_detail_omit_raw_provider_payloads(self):
        from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

        raw_b64 = "b" * 9000
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                emit_progress(
                    "provider_request",
                    progress=0.25,
                    detail={
                        "provider": "openai",
                        "provider_raw_response": {"data": [{"b64_json": raw_b64}]},
                        "debug_payload": {"nested": "summary-only"},
                        "debug": '{"provider-private":"private prompt"}',
                    },
                )
                return {"path": str(Path(workspace) / "safe.png"), "title": "safe.png", "kind": "image"}

            service.start(
                request_id="req-image-safe-metadata",
                session_id="session-image-safe-metadata",
                operation="generate",
                tasks=[{"task_id": "safe-meta", "operation": "generate"}],
                runner=runner,
                metadata={
                    "provider": "openai",
                    "provider_raw_response": {"data": [{"b64_json": raw_b64}]},
                    "provider_body": '{"provider-private":"private prompt"}',
                    "api_key": "sk-test-secret",
                },
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-safe-metadata", limit=0)

        serialized_events = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("provider_raw_response", serialized_events)
        self.assertNotIn("b64_json", serialized_events)
        self.assertNotIn(raw_b64, serialized_events)
        self.assertNotIn("provider-private", serialized_events)
        self.assertNotIn("private prompt", serialized_events)
        self.assertNotIn("sk-test-secret", serialized_events)
        self.assertIn("[redacted]", serialized_events)
        self.assertIn("omitted_metadata_field_count", serialized_events)

    def test_v022_image_job_service_rejects_token_shaped_telemetry_values(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                emit_progress(
                    "fallback",
                    progress=0.7,
                    detail={
                        "provider": "sk-progress-provider",
                        "model": "bearer:progress-model",
                        "fallback_provider": "api_key-provider",
                        "fallback_from_model": "authorization:old-model",
                        "fallback_to_model": "gpt-image-2",
                        "fallback_reason": "client_error",
                    },
                )
                return {"path": str(Path(workspace) / "safe.png"), "title": "safe.png", "kind": "image"}

            service.start(
                request_id="req-image-token-shaped-telemetry",
                session_id="session-image-token-shaped-telemetry",
                operation="generate",
                tasks=[{"task_id": "task-1", "operation": "generate"}],
                runner=runner,
                metadata={
                    "provider": "sk-metadata-provider",
                    "model": "bearer:metadata-model",
                    "fallback_provider": "api_key-provider",
                    "fallback_from_model": "authorization:old-model",
                    "fallback_to_model": "gpt-image-2",
                    "fallback_reason": "client_error",
                },
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-token-shaped-telemetry", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-token-shaped-telemetry")

        serialized_events = json.dumps(events, ensure_ascii=False)
        serialized_projection = json.dumps(projection, ensure_ascii=False)
        for unsafe in ("sk-progress-provider", "sk-metadata-provider", "bearer:", "api_key-provider", "authorization:"):
            self.assertNotIn(unsafe, serialized_events)
            self.assertNotIn(unsafe, serialized_projection)
        self.assertIn("gpt-image-2", serialized_events)
        self.assertIn("client_error", serialized_events)
        self.assertIn("omitted_metadata_field_count", serialized_events)
        job = projection["image_jobs"][0]
        self.assertEqual(job["fallback_to_model"], "gpt-image-2")
        self.assertEqual(job["fallback_reason"], "client_error")
        self.assertNotIn("provider", job["tasks"][0])

    def test_v024_image_job_service_rejects_url_and_path_shaped_telemetry_values(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        unsafe_url = "https://example.test/customer-roadmap"
        unsafe_path = "C:/Users/Alice/customer-roadmap.png"
        unsafe_retry = "C:/Users/Alice/retry-reason.png"

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                emit_progress(
                    "retry",
                    progress=0.5,
                    detail={
                        "provider": unsafe_url,
                        "model": unsafe_path,
                        "retry_gate": "non-blank",
                        "retry_reason": unsafe_retry,
                        "quality_status": "fail",
                    },
                )
                return {"path": str(Path(workspace) / "safe.png"), "title": "safe.png", "kind": "image"}

            service.start(
                request_id="req-image-path-shaped-telemetry",
                session_id="session-image-path-shaped-telemetry",
                operation="generate",
                tasks=[{"task_id": "task-1", "operation": "generate"}],
                runner=runner,
                metadata={"provider": unsafe_url, "model": unsafe_path},
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-path-shaped-telemetry", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-path-shaped-telemetry")

        serialized_events = json.dumps(events, ensure_ascii=False)
        serialized_projection = json.dumps(projection, ensure_ascii=False)
        for unsafe in (unsafe_url, unsafe_path, unsafe_retry, "example.test", "C:/Users/Alice"):
            self.assertNotIn(unsafe, serialized_events)
            self.assertNotIn(unsafe, serialized_projection)
        self.assertIn("non-blank", serialized_events)
        self.assertIn("fail", serialized_events)
        self.assertIn("omitted_metadata_field_count", serialized_events)

    def test_v022_image_job_service_error_and_progress_status_do_not_persist_raw_text(self):
        from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

        raw_text = '{"provider-private":"private prompt"}'
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def progress_runner(task, emit_progress, cancel_event):
                emit_progress(raw_text, progress=0.5, detail={"provider": "openai"})
                return {"path": str(Path(workspace) / "safe.png"), "title": "safe.png", "kind": "image"}

            def failure_runner(task, emit_progress, cancel_event):
                raise RuntimeError(raw_text)

            UnicodeErrorType = type("私人提示词", (RuntimeError,), {})
            SensitiveAsciiErrorType = type("privateprompt", (RuntimeError,), {})

            def unicode_error_type_runner(task, emit_progress, cancel_event):
                raise UnicodeErrorType("boom")

            def sensitive_ascii_error_type_runner(task, emit_progress, cancel_event):
                raise SensitiveAsciiErrorType("boom")

            service.start(
                request_id="req-image-safe-progress-status",
                session_id="session-image-safe-progress-status",
                operation="generate",
                tasks=[{"task_id": "safe-progress", "operation": "generate"}],
                runner=progress_runner,
                synchronous=True,
            )
            service.start(
                request_id="req-image-safe-error-message",
                session_id="session-image-safe-error-message",
                operation="generate",
                tasks=[{"task_id": "safe-error", "operation": "generate"}],
                runner=failure_runner,
                synchronous=True,
            )
            service.start(
                request_id="req-image-safe-error-type",
                session_id="session-image-safe-error-type",
                operation="generate",
                tasks=[{"task_id": "safe-error-type", "operation": "generate"}],
                runner=unicode_error_type_runner,
                synchronous=True,
            )
            service.start(
                request_id="req-image-safe-ascii-error-type",
                session_id="session-image-safe-ascii-error-type",
                operation="generate",
                tasks=[{"task_id": "safe-ascii-error-type", "operation": "generate"}],
                runner=sensitive_ascii_error_type_runner,
                synchronous=True,
            )
            progress_events = ledger.events_for_request("req-image-safe-progress-status", limit=0)
            failed_events = ledger.events_for_request("req-image-safe-error-message", limit=0)
            unsafe_type_events = ledger.events_for_request("req-image-safe-error-type", limit=0)
            unsafe_ascii_type_events = ledger.events_for_request("req-image-safe-ascii-error-type", limit=0)

        serialized_events = json.dumps(progress_events + failed_events + unsafe_type_events + unsafe_ascii_type_events, ensure_ascii=False)
        self.assertNotIn("provider-private", serialized_events)
        self.assertNotIn("private prompt", serialized_events)
        self.assertNotIn("privateprompt", serialized_events)
        self.assertNotIn("私人提示词", serialized_events)
        raw_progress_events = [
            event for event in progress_events
            if event["event_type"] == "image_job.progress" and event["payload"].get("progress") == 0.5
        ]
        self.assertEqual(raw_progress_events[0]["payload"]["status"], "progress")
        failed_event = next(event for event in failed_events if event["event_type"] == "image_job.failed")
        self.assertEqual(failed_event["payload"]["error_message"], "RuntimeError: image job failed")
        unsafe_type_failed_event = next(event for event in unsafe_type_events if event["event_type"] == "image_job.failed")
        self.assertEqual(unsafe_type_failed_event["payload"]["error_type"], "Error")
        self.assertEqual(unsafe_type_failed_event["payload"]["error_message"], "Error: image job failed")
        unsafe_ascii_type_failed_event = next(event for event in unsafe_ascii_type_events if event["event_type"] == "image_job.failed")
        self.assertEqual(unsafe_ascii_type_failed_event["payload"]["error_type"], "Error")
        self.assertEqual(unsafe_ascii_type_failed_event["payload"]["error_message"], "Error: image job failed")

    def test_v022_image_job_service_telemetry_fields_do_not_persist_raw_text(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)

            def runner(task, emit_progress, cancel_event):
                return {"path": str(Path(workspace) / "safe.png"), "title": "safe.png", "kind": "image"}

            service.start(
                request_id="req-image-safe-telemetry",
                session_id="session-image-safe-telemetry",
                job_id="image-job-私人提示词",
                operation="private prompt",
                tasks=[{"task_id": "private prompt", "source_task_id": "private prompt", "operation": "private prompt"}],
                runner=runner,
                metadata={
                    "provider": "私人提示词",
                    "resolved_model": "private prompt",
                    "model": "私人提示词",
                    "image_mode": "private prompt",
                    "source": "privateprompt",
                    "status_code": -503,
                    "attempt": -2,
                    "retry_after_seconds": -0.5,
                    "retryable": "私人提示词",
                    "progress": 2,
                },
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-safe-telemetry", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-safe-telemetry")

        serialized = json.dumps({"events": events, "projection": projection}, ensure_ascii=False)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("privateprompt", serialized)
        self.assertNotIn("私人提示词", serialized)
        started_payload = events[0]["payload"]
        self.assertTrue(started_payload["job_id"].startswith("image-job-"))
        self.assertEqual(started_payload["operation"], "generate")
        self.assertEqual(started_payload["status_code"], 0)
        self.assertEqual(started_payload["attempt"], 0)
        self.assertEqual(started_payload["retry_after_seconds"], 0.0)
        self.assertEqual(started_payload["progress"], 1.0)
        self.assertEqual(started_payload["tasks"][0]["task_id"], "task-1")
        self.assertEqual(started_payload["tasks"][0]["operation"], "generate")

    def test_v022_image_job_service_drops_progress_after_parallel_terminal_race(self):
        from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

        sibling_started = threading.Event()
        failure_emitted = threading.Event()

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            service = ImageJobService(ledger)
            original_emit = service._emit

            def emit_spy(state, event_type, payload, *, suffix):
                emitted = original_emit(state, event_type, payload, suffix=suffix)
                if event_type == "image_job.failed":
                    failure_emitted.set()
                return emitted

            def runner(task, emit_progress, cancel_event):
                if task["task_id"] == "task-1":
                    self.assertTrue(sibling_started.wait(timeout=1))
                    raise RuntimeError("first task failed")
                sibling_started.set()
                self.assertTrue(failure_emitted.wait(timeout=1))
                emit_progress("provider_request", progress=0.9, detail={"provider": "openai"})
                return {"path": str(Path(workspace) / "late.png"), "title": "late.png", "kind": "image"}

            service._emit = emit_spy
            service.start(
                request_id="req-image-terminal-progress-race",
                session_id="session-image-terminal-progress-race",
                operation="generate",
                tasks=[{"task_id": f"t{index}", "operation": "generate"} for index in range(2)],
                runner=runner,
                max_parallel=2,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-terminal-progress-race", limit=0)

        event_types = [event["event_type"] for event in events]
        failed_index = event_types.index("image_job.failed")
        late_progress = [
            event for event in events[failed_index + 1:]
            if event["event_type"] == "image_job.progress"
        ]
        self.assertEqual(late_progress, [])

    def test_v022_runtime_projection_ignores_image_artifacts_after_terminal_job(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-image-terminal-artifact-projection",
                session_id="session-image-terminal-artifact-projection",
                event_type="image_job.started",
                payload={"job_id": "image-job-terminal-artifact", "tasks": [{"task_id": "task-1"}]},
            )
            ledger.append_event(
                request_id="req-image-terminal-artifact-projection",
                session_id="session-image-terminal-artifact-projection",
                event_type="image_job.failed",
                payload={"job_id": "image-job-terminal-artifact", "error_message": "failed"},
            )
            late_artifact = {"title": "late.png", "path": str(Path(workspace) / "late.png"), "kind": "image"}
            ledger.append_event(
                request_id="req-image-terminal-artifact-projection",
                session_id="session-image-terminal-artifact-projection",
                event_type="artifact.created",
                payload={"job_id": "image-job-terminal-artifact", "task_id": "task-1", "artifact": late_artifact},
            )
            ledger.append_event(
                request_id="req-image-terminal-artifact-projection",
                session_id="session-image-terminal-artifact-projection",
                event_type="image_job.artifact",
                payload={"job_id": "image-job-terminal-artifact", "task_id": "task-1", "artifact": late_artifact},
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-image-terminal-artifact-projection")

        self.assertEqual(projection["image_jobs"][0]["status"], "failed")
        self.assertEqual(projection["image_jobs"][0].get("artifacts") or [], [])
        self.assertEqual(projection["messages"][0].get("artifacts") or [], [])

    def test_v022_runtime_projection_sanitizes_legacy_artifact_created_payloads(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        raw_b64 = "c" * 9000
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-artifact-sanitized",
                session_id="session-legacy-artifact-sanitized",
                event_type="artifact.created",
                idempotency_key="req-legacy-artifact-sanitized:private prompt",
                source="private prompt",
                payload={
                    "debug": "private prompt",
                    "job_id": "privateprompt",
                    "task_id": "privateprompt",
                    "source": "private prompt",
                    "artifact_index": "private prompt",
                    "artifact": {
                        "path": str(Path(workspace) / "legacy.png"),
                        "url": f"data:image/png;base64,{raw_b64}",
                        "fileName": "legacy.png",
                        "fileType": "image",
                        "b64_json": raw_b64,
                        "provider_raw_response": {"message": "private prompt"},
                        "nested": {"ignored": True},
                    }
                },
            )
            ledger.append_event(
                request_id="req-legacy-artifact-sanitized",
                session_id="session-legacy-artifact-sanitized",
                event_type="artifact.created",
                payload={
                    "title": "top-level.png",
                    "kind": "image",
                    "job_id": "privateprompt",
                    "task_id": "privateprompt",
                    "provider_body": "private prompt",
                    "source": "private prompt",
                    "artifact_index": "private prompt",
                    "path": f"data:image/png;base64,{raw_b64}",
                    "url": f"data:image/png;base64,{raw_b64}",
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-artifact-sanitized")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("b64_json", serialized_projection)
        self.assertNotIn("data:image", serialized_projection)
        self.assertNotIn(raw_b64, serialized_projection)
        self.assertNotIn("provider_raw_response", serialized_projection)
        self.assertNotIn("private prompt", serialized_projection)
        self.assertNotIn("privateprompt", serialized_projection)
        artifact = projection["messages"][0]["artifacts"][0]
        self.assertEqual(artifact["title"], "legacy.png")
        self.assertEqual(artifact["kind"], "image")
        self.assertTrue(artifact["artifact_sanitized"])
        self.assertGreaterEqual(artifact["omitted_field_count"], 3)

    def test_v024_runtime_projection_projects_office_pdf_quality_evidence_safely(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import RuntimeProjectionHandler

        quality_evidence = {
            "schemaVersion": "v0.2.4",
            "kind": "pdf",
            "sourceRef": r"C:\Users\Alice\customer-roadmap.pdf",
            "qualityGates": [
                "text-orientation",
                "page-render",
                "layout-inspection",
                "Customer merger roadmap paragraph from PDF page 7",
            ],
            "checks": [
                {"id": "text-orientation", "status": "pass", "detail": "rotated=0"},
                {
                    "id": "page-render",
                    "status": "fail",
                    "detail": r"rendered=0; expected_min=1; path=C:\Users\private\secret.pdf; note=token=abc",
                },
                {
                    "id": "layout-inspection",
                    "status": "warn",
                    "detail": "blank_pages=2; note=Customer merger roadmap paragraph from PDF page 7; page_count=5",
                },
                {
                    "id": "Customer merger roadmap paragraph from PDF page 7",
                    "status": "warn",
                    "detail": "Customer merger roadmap paragraph from PDF page 7",
                },
            ],
            "missingQualityGates": ["layout-inspection"],
            "status": "fail",
            "renderedArtifacts": [
                {
                    "page": 1,
                    "artifactRef": r"C:\Users\Alice\render.png",
                    "sourceRef": "https://example.test/customer-roadmap.pdf",
                    "extension": ".png",
                    "width": 1200,
                    "height": 900,
                    "renderProof": "hmac:private-proof",
                    "path": r"C:\Users\private\render.png",
                }
            ],
            "pdfAnalysis": {
                "summary": {
                    "pageCount": 1,
                    "totalExtractedTextChars": 120,
                    "customerFinding": "Customer merger roadmap paragraph from PDF page 7",
                    "rawText": "private prompt",
                    "source": "private prompt",
                },
                "pageEvidence": [
                    {
                        "page": 1,
                        "pageRef": "https://example.test/customer-roadmap.pdf#1",
                        "textLengthBucket": "100",
                        "customerFinding": "Customer merger roadmap paragraph from PDF page 7",
                        "rawText": "private prompt",
                        "renderProof": "hmac:private-proof",
                    }
                ],
            },
            "debug": "private prompt sk-private-1234567890",
            "redacted": True,
        }

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            ledger.append_event(
                request_id="req-v024-quality-projection",
                session_id="session-v024-quality-projection",
                event_type="tool.completed",
                payload={
                    "tool_call_id": "tool-quality",
                    "tool": "office-pdf",
                    "status": "done",
                    "result": {
                        "qualityEvidence": quality_evidence,
                        "content": "private prompt body",
                    },
                },
                idempotency_key="req-v024-quality-projection:tool",
            )
            ledger.append_event(
                request_id="req-v024-quality-projection",
                session_id="session-v024-quality-projection",
                event_type="artifact.created",
                payload={
                    "artifact": {
                        "title": "report.pdf",
                        "kind": "file",
                        "path": "outputs/report.pdf",
                        "quality_evidence": quality_evidence,
                        "provider_raw_response": {"message": "private prompt"},
                    }
                },
                idempotency_key="req-v024-quality-projection:artifact",
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-v024-quality-projection")
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v024-quality-projection",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="1",
            )):
                api_payload = json.loads(RuntimeProjectionHandler().GET())

        assistant = next(message for message in projection["messages"] if message["role"] == "assistant")
        tool_evidence = assistant["tool_calls"][0]["qualityEvidence"]
        artifact_evidence = assistant["artifacts"][0]["qualityEvidence"]
        event_payloads = [event.get("payload") or {} for event in api_payload["projection"]["events"]]
        serialized = json.dumps({"projection": projection, "api": api_payload}, ensure_ascii=False)

        self.assertEqual(tool_evidence["status"], "fail")
        self.assertEqual(tool_evidence["kind"], "pdf")
        self.assertEqual(artifact_evidence["status"], "fail")
        self.assertEqual(artifact_evidence["pdfAnalysis"]["summary"]["pageCount"], 1)
        self.assertEqual(artifact_evidence["pdfAnalysis"]["summary"]["totalExtractedTextChars"], 120)
        self.assertNotIn("Customer merger roadmap", serialized)
        self.assertNotIn("customer-roadmap", serialized)
        self.assertNotIn("example.test", serialized)
        self.assertNotIn("Alice", serialized)
        self.assertIn("unknown-check", {check["id"] for check in tool_evidence["checks"]})
        layout_check = next(check for check in tool_evidence["checks"] if check["id"] == "layout-inspection")
        self.assertEqual(layout_check["detail"], "blank_pages=2; page_count=5")
        render_check = next(check for check in tool_evidence["checks"] if check["id"] == "page-render")
        self.assertEqual(render_check["detail"], "rendered=0; expected_min=1")
        self.assertIn("qualityEvidence", event_payloads[0])
        self.assertNotIn("renderProof", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("sk-private", serialized)
        self.assertNotIn("secret.pdf", serialized)
        self.assertNotIn("C:\\Users", serialized)
        self.assertNotIn("rawText", serialized)
        self.assertNotIn("provider_raw_response", serialized)

    def test_v024_runtime_projection_preserves_image_finalization_retry_gate_summary(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger

        quality_evidence = {
            "schemaVersion": "v0.2.4",
            "kind": "image",
            "sourceRef": "quality-ref-safe",
            "qualityGates": ["visual-inspection", "non-blank"],
            "checks": [
                {
                    "id": "visual-inspection",
                    "status": "fail",
                    "detail": "retry_count=1; max_retries=1; retry_gate=non-blank; finalized=0",
                }
            ],
            "status": "fail",
            "imageAnalysis": {
                "summary": {
                    "finalizationStatus": "retry",
                    "retryGate": "non-blank",
                    "retryRecommended": True,
                    "retryCount": 1,
                    "maxRetries": 1,
                    "unsafeRetryGate": "C:/Users/Alice/private-reference.png",
                }
            },
            "redacted": True,
        }

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            ledger.append_event(
                request_id="req-v024-image-finalization-summary",
                session_id="session-v024-image-finalization-summary",
                event_type="tool.completed",
                payload={
                    "tool_call_id": "tool-image-finalization",
                    "tool": "imagegen",
                    "status": "done",
                    "result": {"qualityEvidence": quality_evidence},
                },
                idempotency_key="req-v024-image-finalization-summary:tool",
            )
            projection = RuntimeProjectionService(ledger).request_projection(
                "req-v024-image-finalization-summary"
            )

        assistant = next(message for message in projection["messages"] if message["role"] == "assistant")
        tool_evidence = assistant["tool_calls"][0]["qualityEvidence"]
        summary = tool_evidence["imageAnalysis"]["summary"]
        visual_check = next(check for check in tool_evidence["checks"] if check["id"] == "visual-inspection")
        serialized = json.dumps(projection, ensure_ascii=False)

        self.assertEqual(summary["retryGate"], "non-blank")
        self.assertEqual(summary["finalizationStatus"], "retry")
        self.assertEqual(summary["retryCount"], 1)
        self.assertEqual(summary["maxRetries"], 1)
        self.assertIn("retry_gate=non-blank", visual_check["detail"])
        self.assertIn("max_retries=1", visual_check["detail"])
        self.assertNotIn("unsafeRetryGate", serialized)
        self.assertNotIn("C:/Users/Alice", serialized)

    def test_v022_runtime_projection_sanitizes_legacy_image_job_artifact_payloads(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        raw_b64 = "d" * 9000
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-artifact-sanitized",
                session_id="session-legacy-image-job-artifact-sanitized",
                event_type="image_job.started",
                payload={"job_id": "image-job-legacy-artifact", "tasks": [{"task_id": "task-1"}]},
            )
            ledger.append_event(
                request_id="req-legacy-image-job-artifact-sanitized",
                session_id="session-legacy-image-job-artifact-sanitized",
                event_type="image_job.artifact",
                payload={
                    "job_id": "image-job-legacy-artifact",
                    "task_id": "task-1",
                    "debug": "private prompt",
                    "artifact": {
                        "path": str(Path(workspace) / "legacy-image-job.png"),
                        "previewUrl": f"data:image/png;base64,{raw_b64}",
                        "fileName": "legacy-image-job.png",
                        "fileType": "image",
                        "b64_json": raw_b64,
                        "provider_raw_response": {"message": "private prompt"},
                        "nested": {"ignored": True},
                    },
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-artifact-sanitized")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("b64_json", serialized_projection)
        self.assertNotIn("data:image", serialized_projection)
        self.assertNotIn(raw_b64, serialized_projection)
        self.assertNotIn("provider_raw_response", serialized_projection)
        self.assertNotIn("private prompt", serialized_projection)
        artifact = projection["image_jobs"][0]["artifacts"][0]
        self.assertEqual(artifact["title"], "legacy-image-job.png")
        self.assertEqual(artifact["kind"], "image")
        self.assertTrue(artifact["artifact_sanitized"])
        self.assertGreaterEqual(artifact["omitted_field_count"], 3)

    def test_v022_runtime_projection_sanitizes_legacy_image_job_task_and_error_payloads(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-task-error-sanitized",
                session_id="session-legacy-image-job-task-error-sanitized",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-legacy-task-error",
                    "operation": "private prompt",
                    "provider": "私人提示词",
                    "resolved_model": "private prompt",
                    "model": "私人提示词",
                    "image_mode": "private prompt",
                    "retryable": "私人提示词",
                    "debug": "private prompt",
                    "tasks": [{
                        "task_id": "task-1",
                        "operation": "generate",
                        "status": "private prompt",
                        "provider_raw_response": {"message": "private prompt"},
                        "debug": "private prompt",
                    }],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-task-error-sanitized",
                session_id="session-legacy-image-job-task-error-sanitized",
                event_type="image_job.failed",
                payload={
                    "job_id": "image-job-legacy-task-error",
                    "error_type": "privateprompt",
                    "error_message": "private prompt",
                    "provider_body": "private prompt",
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-task-error-sanitized")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("provider_raw_response", serialized_projection)
        self.assertNotIn("private prompt", serialized_projection)
        self.assertNotIn("privateprompt", serialized_projection)
        self.assertNotIn("私人提示词", serialized_projection)
        job = projection["image_jobs"][0]
        self.assertEqual(job["error_type"], "Error")
        self.assertEqual(job["error_message"], "Error: image job failed")
        self.assertEqual(job["tasks"][0]["task_id"], "task-1")
        self.assertEqual(job["tasks"][0]["terminal_job_status"], "failed")

    def test_v022_runtime_projection_rejects_token_shaped_image_job_telemetry(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-token-telemetry",
                session_id="session-legacy-image-job-token-telemetry",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-legacy-telemetry-shape",
                    "operation": "generate",
                    "provider": "sk-started-provider",
                    "model": "bearer:started-model",
                    "fallback_provider": "api_key-provider",
                    "fallback_from_model": "authorization:old-model",
                    "fallback_to_model": "gpt-image-2",
                    "fallback_reason": "client_error",
                    "tasks": [{
                        "task_id": "task-1",
                        "operation": "generate",
                        "provider": "sk-task-provider",
                        "model": "bearer:task-model",
                    }],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-token-telemetry",
                session_id="session-legacy-image-job-token-telemetry",
                event_type="image_job.progress",
                payload={
                    "job_id": "image-job-legacy-telemetry-shape",
                    "task_id": "task-1",
                    "status": "provider_response",
                    "provider": "sk-progress-provider",
                    "model": "bearer:progress-model",
                    "fallback_provider": "api_key-progress-provider",
                    "fallback_from_model": "authorization:progress-old-model",
                    "fallback_to_model": "gpt-image-2",
                    "fallback_reason": "client_error",
                    "attempted_provider_count": 1,
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-token-telemetry")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        for unsafe in ("sk-started-provider", "sk-task-provider", "sk-progress-provider", "bearer:", "api_key-", "authorization:"):
            self.assertNotIn(unsafe, serialized_projection)
        job = projection["image_jobs"][0]
        task = job["tasks"][0]
        self.assertEqual(job["fallback_to_model"], "gpt-image-2")
        self.assertEqual(job["fallback_reason"], "client_error")
        self.assertEqual(job["attempted_provider_count"], 1)
        self.assertNotIn("provider", job)
        self.assertNotIn("provider", task)
        self.assertNotIn("model", task)

    def test_v022_runtime_projection_sanitizes_legacy_image_job_progress_status(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-progress-status",
                session_id="session-legacy-image-job-progress-status",
                event_type="image_job.started",
                payload={"job_id": "image-job-legacy-progress", "tasks": [{"task_id": "task-1"}]},
            )
            ledger.append_event(
                request_id="req-legacy-image-job-progress-status",
                session_id="session-legacy-image-job-progress-status",
                event_type="image_job.progress",
                payload={
                    "job_id": "image-job-legacy-progress",
                    "task_id": "task-1",
                    "status": "private prompt",
                    "provider_body": "private prompt",
                    "progress": 0.5,
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-progress-status")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("private prompt", serialized_projection)
        self.assertEqual(projection["image_jobs"][0]["tasks"][0]["status"], "progress")
        progress_event = next(event for event in projection["events"] if event["event_type"] == "image_job.progress")
        self.assertEqual(progress_event["payload"]["status"], "progress")

    def test_v022_runtime_projection_sanitizes_legacy_image_job_numeric_fields(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-numeric-sanitized",
                session_id="session-legacy-image-job-numeric-sanitized",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-legacy-numeric",
                    "task_count": "privateprompt",
                    "latency_ms": "privateprompt",
                    "progress": "privateprompt",
                    "total_latency_ms": "privateprompt",
                    "tasks": [{
                        "task_id": "task-1",
                        "task_index": "privateprompt",
                        "progress": "privateprompt",
                    }],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-numeric-sanitized",
                session_id="session-legacy-image-job-numeric-sanitized",
                event_type="image_job.progress",
                payload={
                    "job_id": "image-job-legacy-numeric",
                    "task_id": "task-1",
                    "progress": "privateprompt",
                    "latency_ms": "privateprompt",
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-numeric-sanitized",
                session_id="session-legacy-image-job-numeric-sanitized",
                event_type="image_job.completed",
                payload={
                    "job_id": "image-job-legacy-numeric",
                    "artifact_count": "privateprompt",
                    "total_latency_ms": "privateprompt",
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-numeric-sanitized")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("privateprompt", serialized_projection)
        job = projection["image_jobs"][0]
        self.assertEqual(job["task_count"], 0)
        self.assertEqual(job["artifact_count"], 0)
        self.assertNotIn("total_latency_ms", job)
        started_event = next(event for event in projection["events"] if event["event_type"] == "image_job.started")
        self.assertTrue(started_event["payload"]["payload_sanitized"])
        self.assertNotIn("latency_ms", started_event["payload"])

    def test_v022_runtime_projection_omits_legacy_image_job_events_with_hostile_ids(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-legacy-image-job-hostile-ids",
                session_id="session-legacy-image-job-hostile-ids",
                event_type="image_job.started",
                payload={
                    "job_id": "private prompt",
                    "tasks": [{
                        "task_id": "private prompt",
                        "source_task_id": "private prompt",
                        "operation": "generate",
                    }],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-hostile-ids",
                session_id="session-legacy-image-job-hostile-ids",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-privateprompt",
                    "tasks": [{"task_id": "task-privateprompt"}],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-hostile-ids",
                session_id="session-legacy-image-job-hostile-ids",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-私人提示词",
                    "tasks": [{"task_id": "task-私人提示词"}],
                },
            )
            ledger.append_event(
                request_id="req-legacy-image-job-hostile-ids",
                session_id="session-legacy-image-job-hostile-ids",
                event_type="image_job.started",
                payload={
                    "job_id": "image-job-safe-legacy",
                    "tasks": [{"task_id": "task-privateprompt"}],
                },
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-legacy-image-job-hostile-ids")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("private prompt", serialized_projection)
        self.assertNotIn("privateprompt", serialized_projection)
        self.assertNotIn("私人提示词", serialized_projection)
        self.assertEqual([job["job_id"] for job in projection["image_jobs"]], ["image-job-safe-legacy"])
        self.assertNotIn("task-privateprompt", json.dumps(projection["image_jobs"], ensure_ascii=False))
        self.assertNotIn("task-私人提示词", json.dumps(projection["image_jobs"], ensure_ascii=False))

    def test_v022_webchannel_sse_dual_writes_runtime_events(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-webchannel"
            session_id = "session-v022-webchannel"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="write a short answer",
                    client_attempt_id="attempt-v022",
                )
                self.assertTrue(channel._push_sse_event(request_id, {"type": "delta", "content": "draft"}))
                self.assertTrue(channel._push_sse_event(request_id, {"type": "done", "content": "done answer"}))

                ledger = get_run_event_ledger()
                event_types = [event["event_type"] for event in ledger.events_for_request(request_id)]
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        self.assertIn("run.accepted", event_types)
        self.assertIn("message.user.accepted", event_types)
        self.assertIn("message.assistant.created", event_types)
        self.assertIn("assistant.delta", event_types)
        self.assertIn("message.assistant.finalized", event_types)
        self.assertIn("run.completed", event_types)
        self.assertEqual(projection["state"], "completed")
        self.assertEqual(projection["messages"][0]["content"], "write a short answer")
        self.assertEqual(projection["messages"][1]["content"], "done answer")

    def test_v022_webchannel_terminal_runtime_event_matrix(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        cases = [
            ("error", {"type": "error", "message": "boom"}, "run.failed", "failed"),
            ("cancelled", {"type": "cancelled", "message": "stopped"}, "run.cancelled", "cancelled"),
            ("interrupted", {"type": "interrupted", "message": "lost"}, "run.interrupted", "interrupted"),
        ]
        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            for suffix, event, expected_type, expected_state in cases:
                request_id = f"req-v022-terminal-{suffix}"
                session_id = "session-v022-terminal"
                channel.request_to_session[request_id] = session_id
                channel._ensure_sse_state(request_id)
                try:
                    channel._record_request_accepted_events(request_id, session_id, visible_message=suffix)
                    self.assertTrue(channel._push_sse_event(request_id, event))
                    event_types = [item["event_type"] for item in ledger.events_for_request(request_id)]
                    projection = RuntimeProjectionService(ledger).request_projection(request_id)
                finally:
                    channel._cleanup_sse_request(request_id)
                self.assertIn(expected_type, event_types)
                self.assertEqual(projection["state"], expected_state)

            request_id = "req-v022-terminal-timeout"
            channel.request_to_session[request_id] = "session-v022-terminal"
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(request_id, "session-v022-terminal", visible_message="timeout")
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "tool_end",
                    "tool": "bash",
                    "tool_call_id": "tool-timeout",
                    "status": "timeout",
                    "result": "timed out",
                }))
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        self.assertEqual(projection["messages"][1]["tool_calls"][0]["status"], "timeout")

    def test_v022_webchannel_subagent_sse_events_are_durable_and_projected(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-subagent-parent"
            session_id = "session-v022-subagent-parent"
            child_request_id = "subagent-child-abc123"
            task_id = "task-subagent-abc123"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="delegate review",
                )
                task = {
                    "id": task_id,
                    "name": "Reviewer",
                    "role": "explorer",
                    "summary": "Review projection",
                    "status": "running",
                    "requestId": child_request_id,
                    "childSessionId": child_request_id,
                    "parentRequestId": request_id,
                    "parentSessionId": session_id,
                    "deadlineAt": 1782345600,
                    "timeoutSeconds": 900,
                    "lastHeartbeatAt": 1782345000,
                }
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "subagent_start",
                    "tool_call_id": "tool-subagent-review",
                    "task": task,
                    "task_id": task_id,
                    "child_request_id": child_request_id,
                    "name": "Reviewer",
                    "role": "explorer",
                    "summary": "Review projection",
                    "status": "starting",
                }))
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "subagent_update",
                    "tool_call_id": "tool-subagent-review",
                    "task": {**task, "status": "running", "lastHeartbeatAt": 1782345060},
                    "task_id": task_id,
                    "child_request_id": child_request_id,
                    "status": "running",
                }))
                running_projection = RuntimeProjectionService(get_run_event_ledger()).request_projection(request_id)
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "subagent_complete",
                    "tool_call_id": "tool-subagent-review",
                    "task": {**task, "status": "completed", "result": "PASS"},
                    "task_id": task_id,
                    "child_request_id": child_request_id,
                    "status": "completed",
                    "result_preview": "PASS",
                }))
                ledger = get_run_event_ledger()
                events = ledger.events_for_request(request_id)
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        running_tool = running_projection["messages"][1]["tool_calls"][0]
        self.assertEqual(running_tool["name"], "subagent")
        self.assertEqual(running_tool["status"], "running")
        self.assertEqual(running_tool["child_request_id"], child_request_id)
        self.assertEqual(running_tool["parent_request_id"], request_id)
        self.assertEqual(running_tool["task_id"], task_id)
        self.assertEqual(running_tool["result"]["task"]["lastHeartbeatAt"], 1782345060)

        event_types = [event["event_type"] for event in events]
        self.assertIn("subagent.started", event_types)
        self.assertIn("subagent.updated", event_types)
        self.assertIn("subagent.completed", event_types)
        safe_subagent_events = [event for event in projection["events"] if str(event.get("event_type", "")).startswith("subagent.")]
        self.assertTrue(safe_subagent_events)
        self.assertEqual(safe_subagent_events[-1]["payload"]["parent_request_id"], request_id)
        self.assertEqual(safe_subagent_events[-1]["payload"]["child_request_id"], child_request_id)
        self.assertEqual(safe_subagent_events[-1]["payload"]["deadline_at"], 1782345600)
        self.assertEqual(safe_subagent_events[-1]["payload"]["last_heartbeat_at"], 1782345000)
        tool = projection["messages"][1]["tool_calls"][0]
        self.assertEqual(tool["name"], "subagent")
        self.assertEqual(tool["status"], "completed")
        self.assertEqual(tool["child_request_id"], child_request_id)
        self.assertEqual(tool["parent_request_id"], request_id)
        self.assertEqual(tool["task_id"], task_id)
        self.assertEqual(tool["result"]["task"]["result"], "PASS")
        self.assertEqual(tool["result"]["task"]["deadlineAt"], 1782345600)
        self.assertEqual(tool["result"]["task"]["lastHeartbeatAt"], 1782345000)

    def test_v022_webchannel_subagent_terminal_variants_are_durable_and_projected(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        terminal_cases = {
            "subagent_failed": ("subagent.failed", "failed", "review failed"),
            "subagent_timeout": ("subagent.timeout", "timeout", "review timed out"),
            "subagent_cancelled": ("subagent.cancelled", "cancelled", "review cancelled"),
        }
        projections = {}
        event_types_by_case = {}

        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            try:
                for legacy_type, (canonical_type, expected_status, result_preview) in terminal_cases.items():
                    suffix = expected_status.replace("_", "-")
                    request_id = f"req-v022-subagent-{suffix}"
                    session_id = f"session-v022-subagent-{suffix}"
                    child_request_id = f"subagent-child-{suffix}"
                    task_id = f"task-subagent-{suffix}"
                    channel.request_to_session[request_id] = session_id
                    channel._ensure_sse_state(request_id)
                    channel._record_request_accepted_events(
                        request_id,
                        session_id,
                        visible_message=f"delegate {expected_status}",
                    )
                    self.assertTrue(channel._push_sse_event(request_id, {
                        "type": "subagent_start",
                        "tool_call_id": f"tool-subagent-{suffix}",
                        "task": {
                            "id": task_id,
                            "name": f"Reviewer {expected_status}",
                            "role": "worker",
                            "summary": f"Review {expected_status}",
                            "requestId": child_request_id,
                            "parentRequestId": request_id,
                            "parentSessionId": session_id,
                            "deadlineAt": 1782346600,
                            "timeoutSeconds": 300,
                            "lastHeartbeatAt": 1782346000,
                        },
                        "task_id": task_id,
                        "child_request_id": child_request_id,
                    }))
                    self.assertTrue(channel._push_sse_event(request_id, {
                        "type": legacy_type,
                        "tool_call_id": f"tool-subagent-{suffix}",
                        "task": {
                            "id": task_id,
                            "name": f"Reviewer {expected_status}",
                            "role": "worker",
                            "summary": f"Review {expected_status}",
                            "requestId": child_request_id,
                            "parentRequestId": request_id,
                            "parentSessionId": session_id,
                            "deadlineAt": 1782346600,
                            "timeoutSeconds": 300,
                            "lastHeartbeatAt": 1782346060,
                        },
                        "task_id": task_id,
                        "child_request_id": child_request_id,
                        "result_preview": result_preview,
                    }))
                    event_types_by_case[expected_status] = [
                        event["event_type"] for event in ledger.events_for_request(request_id)
                    ]
                    projections[expected_status] = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                for expected_status in terminal_cases.values():
                    suffix = expected_status[1].replace("_", "-")
                    channel._cleanup_sse_request(f"req-v022-subagent-{suffix}")

        for _legacy_type, (canonical_type, expected_status, result_preview) in terminal_cases.items():
            projection = projections[expected_status]
            self.assertIn(canonical_type, event_types_by_case[expected_status])
            safe_subagent_events = [
                event for event in projection["events"]
                if event.get("event_type") == canonical_type
            ]
            self.assertTrue(safe_subagent_events)
            payload = safe_subagent_events[-1]["payload"]
            self.assertEqual(payload["child_request_id"], f"subagent-child-{expected_status}")
            self.assertEqual(payload["parent_request_id"], f"req-v022-subagent-{expected_status}")
            tool = projection["messages"][1]["tool_calls"][0]
            self.assertEqual(tool["name"], "subagent")
            self.assertEqual(tool["status"], expected_status)
            self.assertEqual(tool["child_request_id"], f"subagent-child-{expected_status}")
            self.assertEqual(tool["parent_request_id"], f"req-v022-subagent-{expected_status}")
            self.assertEqual(tool["task_id"], f"task-subagent-{expected_status}")
            self.assertEqual(tool["result"]["task"]["status"], expected_status)
            self.assertEqual(tool["result"]["task"]["result"], result_preview)
            self.assertEqual(tool["result"]["task"]["lastHeartbeatAt"], 1782346060)

    def test_v022_subagent_projection_sanitizes_hostile_metadata_and_parent_identity(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-subagent-parent-safe"
            session_id = "session-v022-subagent-parent-safe"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="delegate hostile metadata",
                )
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "subagent_failed",
                    "tool_call_id": "<img src=x onerror=alert(1)>",
                    "task": {
                        "id": "task-secret-token",
                        "name": "<img src=x onerror=alert(2)>",
                        "role": "<script>alert(3)</script>",
                        "summary": "<b>" + ("x" * 9000),
                        "status": "<script>alert(4)</script>",
                        "requestId": "subagent-child-safe",
                        "parentRequestId": "req-spoofed-parent",
                        "parentSessionId": "session-spoofed-parent",
                        "deadlineAt": 1782347600,
                        "timeoutSeconds": 300,
                        "lastHeartbeatAt": 1782347060,
                        "result": "sk-raw-task-result-should-not-project",
                    },
                    "task_id": "task-secret-token",
                    "child_request_id": "subagent-child-safe",
                    "parent_request_id": "req-spoofed-parent",
                    "parent_session_id": "session-spoofed-parent",
                    "name": "<img src=x onerror=alert(2)>",
                    "role": "<script>alert(3)</script>",
                    "summary": "<b>" + ("x" * 9000),
                    "status": "<script>alert(4)</script>",
                    "result_preview": "sk-result-preview-should-redact",
                }))
                projection = RuntimeProjectionService(get_run_event_ledger()).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        safe_events = [
            event for event in projection["events"]
            if event.get("event_type") == "subagent.failed"
        ]
        self.assertTrue(safe_events)
        safe_payload = safe_events[-1]["payload"]
        self.assertEqual(safe_payload["parent_request_id"], request_id)
        self.assertEqual(safe_payload["parent_session_id"], session_id)
        self.assertEqual(safe_payload["child_request_id"], "subagent-child-safe")
        self.assertNotIn("task_id", safe_payload)
        self.assertEqual(safe_payload["status"], "failed")
        self.assertIn("&lt;img", safe_payload["name"])
        self.assertIn("&lt;b&gt;", safe_payload["summary"])
        self.assertTrue(safe_payload["payload_sanitized"])
        self.assertTrue(safe_payload["payload_truncated"])

        tool = projection["messages"][1]["tool_calls"][0]
        self.assertEqual(tool["name"], "subagent")
        self.assertEqual(tool["status"], "failed")
        self.assertEqual(tool["child_request_id"], "subagent-child-safe")
        self.assertEqual(tool["parent_request_id"], request_id)
        self.assertEqual(tool["task_id"], "")
        task = tool["result"]["task"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"], "[redacted]")
        self.assertIn("&lt;img", task["name"])
        self.assertIn("&lt;script&gt;", task["role"])
        self.assertIn("&lt;b&gt;", task["summary"])
        self.assertLessEqual(len(task["summary"]), 540)
        projection_json = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("req-spoofed-parent", projection_json)
        self.assertNotIn("session-spoofed-parent", projection_json)
        self.assertNotIn("task-secret-token", projection_json)
        self.assertNotIn("sk-result-preview", projection_json)
        self.assertNotIn("<script", projection_json)
        self.assertNotIn("<img", projection_json)

    def test_v022_subagent_tool_execution_end_does_not_project_raw_task_result(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-subagent-tool-end"
            session_id = "session-v022-subagent-tool-end"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="delegate production result",
                )
                callback = channel._make_sse_callback(request_id)
                callback({
                    "type": "tool_execution_end",
                    "data": {
                        "tool_name": "subagent",
                        "tool_call_id": "tool-subagent-prod",
                        "status": "failed",
                        "result": {
                            "task": {
                                "id": "task-subagent-prod",
                                "name": "Production Reviewer",
                                "role": "worker",
                                "summary": "Review production callback",
                                "status": "failed",
                                "requestId": "subagent-child-prod",
                                "parentRequestId": "req-spoofed-prod",
                                "parentSessionId": "session-spoofed-prod",
                                "deadlineAt": 1782348600,
                                "timeoutSeconds": 300,
                                "lastHeartbeatAt": 1782348060,
                                "result": "sk-raw-production-result",
                                "error": "secret raw production error",
                            }
                        },
                        "execution_time": 1.0,
                    },
                })
                ledger = get_run_event_ledger()
                events = ledger.events_for_request(request_id)
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        event_types = [event["event_type"] for event in events]
        self.assertIn("subagent.failed", event_types)
        self.assertNotIn("tool.failed", event_types)
        tool = projection["messages"][1]["tool_calls"][0]
        self.assertEqual(tool["name"], "subagent")
        self.assertEqual(tool["status"], "failed")
        self.assertEqual(tool["child_request_id"], "subagent-child-prod")
        self.assertEqual(tool["parent_request_id"], request_id)
        self.assertEqual(tool["task_id"], "task-subagent-prod")
        self.assertEqual(tool["result"]["task"]["result"], "")
        projection_json = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("sk-raw-production-result", projection_json)
        self.assertNotIn("secret raw production error", projection_json)
        self.assertNotIn("req-spoofed-prod", projection_json)
        self.assertNotIn("session-spoofed-prod", projection_json)

    def test_v022_subagent_tool_execution_timeout_does_not_emit_generic_tool_failed(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-subagent-timeout-prod"
            session_id = "session-v022-subagent-timeout-prod"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="delegate timeout production result",
                )
                callback = channel._make_sse_callback(request_id)
                callback({
                    "type": "tool_execution_start",
                    "data": {
                        "tool_name": "subagent",
                        "tool_call_id": "tool-subagent-timeout-prod",
                        "arguments": {
                            "name": "Timeout Reviewer",
                            "role": "worker",
                            "summary": "Review timeout callback",
                        },
                    },
                })
                callback({
                    "type": "tool_execution_timeout",
                    "data": {
                        "tool_name": "subagent",
                        "tool_call_id": "tool-subagent-timeout-prod",
                        "elapsed_seconds": 901,
                        "timeout_seconds": 900,
                        "message": "sk-raw-timeout-message",
                        "task": {
                            "id": "task-subagent-timeout-prod",
                            "name": "Timeout Reviewer",
                            "role": "worker",
                            "summary": "Review timeout callback",
                            "requestId": "subagent-child-timeout-prod",
                            "parentRequestId": "req-spoofed-timeout",
                            "parentSessionId": "session-spoofed-timeout",
                            "deadlineAt": 1782349600,
                            "timeoutSeconds": 900,
                            "lastHeartbeatAt": 1782349060,
                            "result": "sk-raw-timeout-result",
                            "error": "secret raw timeout error",
                        },
                    },
                })
                ledger = get_run_event_ledger()
                events = ledger.events_for_request(request_id)
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        event_types = [event["event_type"] for event in events]
        self.assertIn("subagent.started", event_types)
        self.assertIn("subagent.timeout", event_types)
        self.assertNotIn("tool.started", event_types)
        self.assertNotIn("tool.failed", event_types)
        tool = projection["messages"][1]["tool_calls"][0]
        self.assertEqual(tool["name"], "subagent")
        self.assertEqual(tool["status"], "timeout")
        self.assertEqual(tool["child_request_id"], "subagent-child-timeout-prod")
        self.assertEqual(tool["parent_request_id"], request_id)
        self.assertEqual(tool["task_id"], "task-subagent-timeout-prod")
        self.assertEqual(tool["result"]["task"]["result"], "")
        projection_json = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("sk-raw-timeout-message", projection_json)
        self.assertNotIn("sk-raw-timeout-result", projection_json)
        self.assertNotIn("secret raw timeout error", projection_json)
        self.assertNotIn("req-spoofed-timeout", projection_json)
        self.assertNotIn("session-spoofed-timeout", projection_json)

    def test_v022_webchannel_permission_artifact_cancel_are_durable_and_projected(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-permission-artifact-cancel"
            session_id = "session-v022-permission-artifact-cancel"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="create an auditable file",
                )
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "tool_permission_request",
                    "permission_request_id": "perm-v022-file",
                    "id": "perm-v022-file",
                    "tool": "file_write",
                    "title": "Write report.txt",
                    "message": "Approve writing report.txt",
                    "request_id": request_id,
                }))
                ledger = get_run_event_ledger()
                permission_projection = RuntimeProjectionService(ledger).request_projection(request_id)

                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "artifact",
                    "artifact": {
                        "kind": "file",
                        "title": "report.txt",
                        "path": "C:/CowAgent/out/report.txt",
                        "sizeBytes": 120,
                        "content": "raw file content should not project",
                        "metadata": {"secret": "hidden"},
                    },
                    "request_id": request_id,
                }))
                artifact_projection = RuntimeProjectionService(ledger).request_projection(request_id)

                self.assertTrue(channel._push_cancelled_event_once(request_id, {
                    "type": "cancelled",
                    "content": "Cancelled by user",
                    "terminal_reason": "user_cancelled",
                    "request_id": request_id,
                }))
                events = ledger.events_for_request(request_id)
                cancelled_projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        self.assertEqual(permission_projection["state"], "waiting_permission")
        self.assertTrue(permission_projection["messages"][1]["pending"])
        permission_events = [
            event for event in permission_projection["events"]
            if event.get("event_type") == "permission.requested"
        ]
        self.assertTrue(permission_events)
        self.assertEqual(permission_events[-1]["payload"]["permission_request_id"], "perm-v022-file")
        self.assertEqual(permission_events[-1]["payload"]["tool"], "file_write")

        artifact = artifact_projection["messages"][1]["artifacts"][0]
        self.assertEqual(artifact["title"], "report.txt")
        self.assertEqual(artifact["path"], "C:/CowAgent/out/report.txt")
        self.assertEqual(artifact["sizeBytes"], 120)
        artifact_projection_json = json.dumps(artifact_projection, ensure_ascii=False)
        self.assertNotIn("raw file content should not project", artifact_projection_json)
        self.assertNotIn("hidden", artifact_projection_json)
        artifact_events = [
            event for event in artifact_projection["events"]
            if event.get("event_type") == "artifact.created"
        ]
        self.assertTrue(artifact_events)
        self.assertTrue(artifact_events[-1]["payload"]["artifact"]["artifact_sanitized"])

        event_types = [event["event_type"] for event in events]
        self.assertIn("permission.requested", event_types)
        self.assertIn("artifact.created", event_types)
        self.assertIn("run.cancelled", event_types)
        durable_artifact_event = next(event for event in events if event["event_type"] == "artifact.created")
        durable_artifact_json = json.dumps(durable_artifact_event["payload"], ensure_ascii=False)
        self.assertNotIn("raw file content should not project", durable_artifact_json)
        self.assertNotIn("hidden", durable_artifact_json)
        self.assertNotIn("metadata", durable_artifact_json)
        self.assertTrue(durable_artifact_event["payload"]["artifact"]["artifact_sanitized"])
        self.assertEqual(cancelled_projection["state"], "cancelled")
        self.assertEqual(cancelled_projection["terminal_reason"], "user_cancelled")
        self.assertEqual(cancelled_projection["terminal_message"], "Cancelled by user")
        self.assertFalse(cancelled_projection["messages"][1]["pending"])
        self.assertEqual(cancelled_projection["messages"][1]["artifacts"][0]["title"], "report.txt")

    def test_v022_webchannel_done_artifacts_are_sanitized_before_durable_finalize(self):
        from agent.protocol import RuntimeProjectionService, get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            request_id = "req-v022-finalized-artifact-sanitized"
            session_id = "session-v022-finalized-artifact-sanitized"
            channel.request_to_session[request_id] = session_id
            channel._ensure_sse_state(request_id)
            try:
                channel._record_request_accepted_events(
                    request_id,
                    session_id,
                    visible_message="finalize with artifact",
                )
                self.assertTrue(channel._push_sse_event(request_id, {
                    "type": "done",
                    "final_text": "Here is the final artifact",
                    "artifacts": [{
                        "kind": "file",
                        "title": "final-report.txt",
                        "path": "C:/CowAgent/out/final-report.txt",
                        "content": "raw finalized content should not persist",
                        "data": "raw-final-bytes",
                        "metadata": {"secret": "hidden-final"},
                    }],
                    "request_id": request_id,
                }))
                ledger = get_run_event_ledger()
                events = ledger.events_for_request(request_id)
                projection = RuntimeProjectionService(ledger).request_projection(request_id)
            finally:
                channel._cleanup_sse_request(request_id)

        finalized_event = next(event for event in events if event["event_type"] == "message.assistant.finalized")
        completed_event = next(event for event in events if event["event_type"] == "run.completed")
        for event in (finalized_event, completed_event):
            payload_json = json.dumps(event["payload"], ensure_ascii=False)
            self.assertNotIn("raw finalized content should not persist", payload_json)
            self.assertNotIn("raw-final-bytes", payload_json)
            self.assertNotIn("hidden-final", payload_json)
            self.assertNotIn("metadata", payload_json)
            self.assertTrue(event["payload"]["artifacts"][0]["artifact_sanitized"])
        self.assertEqual(projection["state"], "completed")
        self.assertEqual(projection["messages"][1]["content"], "Here is the final artifact")
        self.assertEqual(projection["messages"][1]["artifacts"][0]["title"], "final-report.txt")

    def test_v022_run_event_ledger_redacts_structural_secrets(self):
        from agent.protocol import reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            ledger.append_event(
                request_id="req-v022-redaction",
                session_id="session-v022",
                event_type="tool.started",
                payload={
                    "content": "visible transcript remains replayable",
                    "authorization": "Bearer should-not-persist",
                    "open_ai_api_key": "sk-openai-should-not-persist",
                    "arguments": {
                        "api_key": "sk-should-not-persist",
                        "plain": "kept",
                        "env": {"key": "BOCHA_API_KEY", "value": "bocha-should-not-persist"},
                        "command": "curl -H \"Authorization: Bearer token-should-not-persist\" --api-key cli-should-not-persist",
                    },
                    "result": "OPENAI_API_KEY=sk-result-should-not-persist",
                    "headers": [{"cookie": "session=should-not-persist"}],
                },
                idempotency_key="req-v022-redaction:tool-started",
            )
            event = ledger.events_for_request("req-v022-redaction")[0]

        self.assertEqual(event["payload"]["content"], "visible transcript remains replayable")
        self.assertEqual(event["payload"]["authorization"], "[redacted]")
        self.assertEqual(event["payload"]["open_ai_api_key"], "[redacted]")
        self.assertEqual(event["payload"]["arguments"]["api_key"], "[redacted]")
        self.assertEqual(event["payload"]["arguments"]["plain"], "kept")
        self.assertEqual(event["payload"]["arguments"]["env"]["value"], "[redacted]")
        self.assertNotIn("token-should-not-persist", event["payload"]["arguments"]["command"])
        self.assertNotIn("cli-should-not-persist", event["payload"]["arguments"]["command"])
        self.assertNotIn("sk-result-should-not-persist", event["payload"]["result"])
        self.assertEqual(event["payload"]["headers"][0]["cookie"], "[redacted]")

    def test_v022_run_event_ledger_records_idempotency_conflicts(self):
        from agent.protocol import reset_run_event_ledger_for_tests

        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
            first = ledger.append_event(
                request_id="req-v022-conflict",
                event_type="run.accepted",
                payload={"status": "running"},
                idempotency_key="same-key",
            )
            duplicate = ledger.append_event(
                request_id="req-v022-conflict",
                event_type="run.failed",
                payload={"status": "failed"},
                idempotency_key="same-key",
            )
            events = ledger.events_for_request("req-v022-conflict")

        self.assertEqual(first["event_id"], duplicate["event_id"])
        self.assertTrue(duplicate["idempotency_conflict"])
        self.assertIn("ledger.idempotency_conflict", [event["event_type"] for event in events])

    def test_v022_stream_replays_from_runtime_projection_when_sse_state_is_missing(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            request_id = "req-v022-projection-replay"
            session_id = "session-v022-projection"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.accepted",
                payload={},
                idempotency_key=f"{request_id}:accepted",
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": "restored answer"},
                idempotency_key=f"{request_id}:final",
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.completed",
                payload={"terminal_reason": "done"},
                idempotency_key=f"{request_id}:completed",
            )
            chunks = list(channel.stream_response(request_id))

        payloads = []
        event_ids = []
        for chunk in chunks:
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("id: "):
                    event_ids.append(int(line[len("id: "):]))
                if line.startswith("data: "):
                    payloads.append(json.loads(line[len("data: "):]))

        self.assertEqual([payload["type"] for payload in payloads], ["message_update", "done"])
        self.assertEqual(event_ids, sorted(set(event_ids)))
        self.assertTrue(all(payload.get("runtime_projection_replay") for payload in payloads))
        self.assertEqual(payloads[0]["content"], "restored answer")

    def test_v023_stream_replay_rejects_request_session_mismatch(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            request_id = "req-v023-stream-owner"
            ledger.append_event(
                request_id=request_id,
                session_id="session-v023-owner-a",
                event_type="message.assistant.finalized",
                payload={"content": "private owner answer"},
                idempotency_key=f"{request_id}:final",
            )
            chunks = list(channel.stream_response(request_id, session_id="session-v023-owner-b"))

        payloads = []
        for chunk in chunks:
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("data: "):
                    payloads.append(json.loads(line[len("data: "):]))

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["type"], "error")
        self.assertEqual(payloads[0]["code"], "SESSION_MISMATCH")
        self.assertNotIn("private owner answer", json.dumps(payloads[0], ensure_ascii=False))

    def test_v022_terminal_projection_replay_preempts_sidecar_interruption_recovery(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            request_id = "req-v022-terminal-projection-preempts-sidecar"
            session_id = "session-v022-projection"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.accepted",
                payload={},
                idempotency_key=f"{request_id}:accepted",
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": "completed in durable stream"},
                idempotency_key=f"{request_id}:final",
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.completed",
                payload={"terminal_reason": "done"},
                idempotency_key=f"{request_id}:completed",
            )
            with patch.object(
                channel,
                "_recover_sidecar_interrupted_stream_event",
                side_effect=AssertionError("sidecar recovery must not override durable terminal replay"),
            ):
                chunks = list(channel.stream_response(request_id))

        payloads = []
        for chunk in chunks:
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("data: "):
                    payloads.append(json.loads(line[len("data: "):]))

        self.assertEqual(payloads[-1]["type"], "done")
        self.assertEqual(payloads[-1]["content"], "completed in durable stream")

    def test_v022_failed_projection_replay_preserves_terminal_message_after_partial_delta(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import WebChannel

        with isolated_run_ledger():
            channel = WebChannel()
            ledger = get_run_event_ledger()
            request_id = "req-v022-failed-terminal-message"
            session_id = "session-v022-failed-terminal"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="assistant.delta",
                payload={"content": "partial answer"},
                idempotency_key=f"{request_id}:delta",
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.failed",
                payload={"message": "actual terminal failure"},
                idempotency_key=f"{request_id}:failed",
            )
            chunks = list(channel.stream_response(request_id))

        payloads = []
        for chunk in chunks:
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("data: "):
                    payloads.append(json.loads(line[len("data: "):]))

        self.assertEqual([payload["type"] for payload in payloads], ["message_update", "error"])
        self.assertEqual(payloads[0]["content"], "partial answer")
        self.assertEqual(payloads[1]["content"], "actual terminal failure")

    def test_v022_runtime_projection_api_returns_request_and_session_projection(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import RuntimeProjectionHandler

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="run.accepted",
                payload={},
                idempotency_key="req-v022-api-a:accepted",
            )
            ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="message.user.accepted",
                payload={"content": "api user"},
                idempotency_key="req-v022-api-a:user",
            )
            first_delta = ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="assistant.delta",
                payload={"content": "api "},
                idempotency_key="req-v022-api-a:delta",
            )
            ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="tool.started",
                payload={"tool_call_id": "tool-api", "tool": "bash", "arguments": {"cmd": "echo ok"}},
                idempotency_key="req-v022-api-a:tool-started",
            )
            ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="artifact.created",
                payload={
                    "artifact": {
                        "title": "out.txt",
                        "kind": "file",
                        "path": "out.txt",
                        "provider_raw_response": {"message": "private prompt"},
                        "b64_json": "e" * 9000,
                    }
                },
                idempotency_key="req-v022-api-a:artifact",
            )
            ledger.append_event(
                request_id="req-v022-api-a",
                session_id="session-v022-api",
                event_type="message.assistant.finalized",
                payload={"content": "api projection", "artifacts": [{"title": "final.png", "kind": "image"}]},
                idempotency_key="req-v022-api-a:final",
            )
            ledger.append_event(
                request_id="req-v022-api-b",
                session_id="session-v022-api",
                event_type="assistant.delta",
                payload={"content": "partial before failure"},
                idempotency_key="req-v022-api-b:delta",
            )
            ledger.append_event(
                request_id="req-v022-api-b",
                session_id="session-v022-api",
                event_type="run.failed",
                payload={"message": "terminal text survived"},
                idempotency_key="req-v022-api-b:failed",
            )
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v022-api-a",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="",
            )):
                request_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v022-api-a",
                session_id="",
                after_event_id="0",
                limit="2",
                include_events="1",
            )):
                limited_request_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="",
                session_id="session-v022-api",
                after_event_id=str(first_delta["event_id"] - 1),
                limit="1000",
                include_events="",
            )):
                session_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="",
                session_id="session-v022-api",
                after_event_id=str(first_delta["event_id"] - 1),
                limit="2",
                include_events="1",
            )):
                session_include_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="missing-v022-api",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="",
            )):
                empty_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v022-api-b",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="",
            )):
                failed_payload = json.loads(RuntimeProjectionHandler().GET())

        self.assertEqual(request_payload["status"], "success")
        self.assertEqual(request_payload["mode"], "request")
        self.assertNotIn("events", request_payload["projection"])
        self.assertEqual(request_payload["projection"]["messages"][0]["content"], "api user")
        self.assertEqual(request_payload["projection"]["messages"][1]["tool_calls"][0]["name"], "bash")
        self.assertEqual(request_payload["projection"]["messages"][1]["artifacts"][0]["title"], "out.txt")
        self.assertEqual(request_payload["projection"]["messages"][1]["artifacts"][1]["title"], "final.png")
        self.assertGreater(limited_request_payload["projection"]["event_count"], 2)
        self.assertEqual(len(limited_request_payload["projection"]["events"]), 2)
        self.assertNotIn("provider_raw_response", json.dumps(limited_request_payload["projection"], ensure_ascii=False))
        self.assertNotIn("private prompt", json.dumps(limited_request_payload["projection"], ensure_ascii=False))
        self.assertNotIn("b64_json", json.dumps(limited_request_payload["projection"], ensure_ascii=False))
        self.assertEqual(session_payload["status"], "success")
        self.assertEqual(session_payload["mode"], "session")
        self.assertEqual(session_payload["projection"]["requests"][0]["request_id"], "req-v022-api-a")
        self.assertEqual(session_payload["projection"]["requests"][0]["messages"][0]["content"], "api user")
        self.assertNotIn("events", session_payload["projection"])
        self.assertEqual(len(session_include_payload["projection"]["events"]), 2)
        self.assertNotIn("events", session_include_payload["projection"]["requests"][0])
        self.assertEqual(empty_payload["projection"]["event_count"], 0)
        self.assertEqual(empty_payload["projection"]["messages"], [])
        self.assertEqual(failed_payload["projection"]["messages"][0]["content"], "partial before failure")
        self.assertEqual(failed_payload["projection"]["terminal_message"], "terminal text survived")

    def test_v023_runtime_projection_api_rejects_request_session_mismatch(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import RuntimeProjectionHandler

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            ledger.append_event(
                request_id="req-v023-api-owner",
                session_id="session-v023-api-owner-a",
                event_type="message.assistant.finalized",
                payload={"content": "private api answer"},
                idempotency_key="req-v023-api-owner:final",
            )
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v023-api-owner",
                session_id="session-v023-api-owner-b",
                after_event_id="0",
                limit="1000",
                include_events="1",
            )):
                payload = json.loads(RuntimeProjectionHandler().GET())

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "SESSION_MISMATCH")
        self.assertNotIn("projection", payload)
        self.assertNotIn("private api answer", json.dumps(payload, ensure_ascii=False))

    def test_v023_runtime_projection_include_events_redacts_generic_event_bodies(self):
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import RuntimeProjectionHandler

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            ledger.append_event(
                request_id="req-v023-event-body-redact",
                session_id="session-v023-event-body-redact",
                event_type="diagnostic.updated",
                payload={
                    "content": "private raw body without token markers",
                    "message": "plain terminal details",
                    "visible_message": "private visible body no marker",
                    "messageText": "private camel text no marker",
                    "messageId": "msg-visible-1",
                    "contentHash": "0" * 64,
                    "promptTokens": 123,
                    "promptHash": "1" * 64,
                    "promptId": "prompt-safe-1",
                    "instructionCount": 2,
                    "instructionId": "instruction-safe-1",
                    "status": "ok",
                },
                idempotency_key="req-v023-event-body-redact:diagnostic",
            )
            ledger.append_event(
                request_id="req-v023-event-body-redact",
                session_id="session-v023-event-body-redact",
                event_type="diagnostic.updated",
                payload={
                    "reason": "private reason body without token markers",
                    "detail": "private detail body without token markers",
                    "contentHash": "private content hash body without token markers",
                    "messageId": "private message id body without token markers",
                    "outputCount": "private output count body without token markers",
                    "status": "CustomerAccountAlpha123",
                    "messageType": "InternalCaseABC987",
                    "promptId": "a" * 64,
                    "instructionId": "b" * 64,
                },
                idempotency_key="req-v023-event-body-redact:invalid-structural",
            )
            ledger.append_event(
                request_id="req-v023-event-body-redact",
                session_id="session-v023-event-body-redact",
                event_type="diagnostic.updated",
                payload={
                    "promptId": "prompt-secretRoadmap42",
                    "instructionId": "instruction-tokenRoadmap42",
                },
                idempotency_key="req-v023-event-body-redact:prefixed-sensitive",
            )
            ledger.append_event(
                request_id="req-v023-event-body-redact",
                session_id="session-v023-event-body-redact",
                event_type="diagnostic.updated",
                payload={
                    "unknownCodename": "AcmeRoadmap",
                    "mode": "internalMode",
                    "status": "ok",
                },
                idempotency_key="req-v023-event-body-redact:unknown-identifier",
            )
            ledger.append_event(
                request_id="req-v023-message-payload-redact",
                session_id="session-v023-event-body-redact",
                event_type="message.user.accepted",
                payload={"content": "renderable user text"},
                idempotency_key="req-v023-message-payload-redact:user",
            )
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v023-event-body-redact",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="1",
            )):
                diagnostic_payload = json.loads(RuntimeProjectionHandler().GET())
            with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                request_id="req-v023-message-payload-redact",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="1",
            )):
                message_payload = json.loads(RuntimeProjectionHandler().GET())

        diagnostic_projection = json.dumps(diagnostic_payload["projection"], ensure_ascii=False)
        diagnostic_event = diagnostic_payload["projection"]["events"][0]
        invalid_structural_event = diagnostic_payload["projection"]["events"][1]
        prefixed_sensitive_event = diagnostic_payload["projection"]["events"][2]
        unknown_identifier_event = diagnostic_payload["projection"]["events"][3]
        message_event = message_payload["projection"]["events"][0]

        self.assertEqual(diagnostic_event["payload"]["contentPreview"], "[redacted-content]")
        self.assertTrue(diagnostic_event["payload"]["contentHash"])
        self.assertEqual(diagnostic_event["payload"]["contentLength"], len("private raw body without token markers"))
        self.assertEqual(diagnostic_event["payload"]["messagePreview"], "[redacted-content]")
        self.assertEqual(diagnostic_event["payload"]["visibleMessagePreview"], "[redacted-content]")
        self.assertEqual(diagnostic_event["payload"]["messageTextPreview"], "[redacted-content]")
        self.assertEqual(diagnostic_event["payload"]["messageId"], "msg-visible-1")
        self.assertEqual(diagnostic_event["payload"]["contentHash"], "0" * 64)
        self.assertEqual(diagnostic_event["payload"]["promptTokens"], 123)
        self.assertEqual(diagnostic_event["payload"]["promptHash"], "1" * 64)
        self.assertEqual(diagnostic_event["payload"]["promptId"], "prompt-safe-1")
        self.assertEqual(diagnostic_event["payload"]["instructionCount"], 2)
        self.assertEqual(diagnostic_event["payload"]["instructionId"], "instruction-safe-1")
        self.assertNotIn("promptTokensPreview", diagnostic_event["payload"])
        self.assertNotIn("promptHashPreview", diagnostic_event["payload"])
        self.assertNotIn("instructionCountPreview", diagnostic_event["payload"])
        self.assertEqual(diagnostic_event["payload"]["status"], "ok")
        self.assertNotIn("private raw body without token markers", diagnostic_projection)
        self.assertNotIn("plain terminal details", diagnostic_projection)
        self.assertNotIn("private visible body no marker", diagnostic_projection)
        self.assertNotIn("private camel text no marker", diagnostic_projection)
        self.assertEqual(invalid_structural_event["payload"]["reasonPreview"], "[redacted-content]")
        self.assertEqual(invalid_structural_event["payload"]["detailPreview"], "[redacted-content]")
        self.assertNotIn("contentHash", invalid_structural_event["payload"])
        self.assertNotIn("messageId", invalid_structural_event["payload"])
        self.assertNotIn("outputCount", invalid_structural_event["payload"])
        self.assertNotIn("status", invalid_structural_event["payload"])
        self.assertNotIn("messageType", invalid_structural_event["payload"])
        self.assertNotIn("promptId", invalid_structural_event["payload"])
        self.assertNotIn("instructionId", invalid_structural_event["payload"])
        self.assertTrue(invalid_structural_event["payload"]["payload_sanitized"])
        self.assertNotIn("private reason body without token markers", diagnostic_projection)
        self.assertNotIn("private detail body without token markers", diagnostic_projection)
        self.assertNotIn("private content hash body without token markers", diagnostic_projection)
        self.assertNotIn("private message id body without token markers", diagnostic_projection)
        self.assertNotIn("private output count body without token markers", diagnostic_projection)
        self.assertNotIn("CustomerAccountAlpha123", diagnostic_projection)
        self.assertNotIn("InternalCaseABC987", diagnostic_projection)
        self.assertNotIn("a" * 64, diagnostic_projection)
        self.assertNotIn("b" * 64, diagnostic_projection)
        self.assertNotIn("promptId", prefixed_sensitive_event["payload"])
        self.assertNotIn("instructionId", prefixed_sensitive_event["payload"])
        self.assertTrue(prefixed_sensitive_event["payload"]["payload_sanitized"])
        self.assertNotIn("prompt-secretRoadmap42", diagnostic_projection)
        self.assertNotIn("instruction-tokenRoadmap42", diagnostic_projection)
        self.assertEqual(unknown_identifier_event["payload"]["unknownCodenamePreview"], "[redacted-content]")
        self.assertEqual(unknown_identifier_event["payload"]["modePreview"], "[redacted-content]")
        self.assertEqual(unknown_identifier_event["payload"]["status"], "ok")
        self.assertNotIn("AcmeRoadmap", diagnostic_projection)
        self.assertNotIn("internalMode", diagnostic_projection)
        self.assertEqual(message_payload["projection"]["messages"][0]["content"], "renderable user text")
        self.assertEqual(message_event["payload"]["contentPreview"], "[redacted-content]")
        self.assertNotIn("content", message_event["payload"])
        self.assertNotIn("renderable user text", json.dumps(message_event, ensure_ascii=False))

    def test_v022_image_jobs_api_starts_collects_and_projects_backend_job(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            start_body = {
                "action": "start",
                "dry_run": True,
                "synchronous": True,
                "include_events": True,
                "request_id": "req-privateprompt",
                "session_id": "session-image-api",
                "job_id": "image-job-privateprompt",
                "prompt": "draw a quiet test image",
                "count": 2,
                "max_parallel": 2,
                "provider": "privateprompt",
                "model": "privateprompt",
            }
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "_get_workspace_root", return_value=tempfile.gettempdir()), \
                patch.object(web_channel.web, "data", return_value=json.dumps(start_body).encode("utf-8")):
                start_payload = json.loads(web_channel.ImageJobsHandler().POST())

            job = start_payload["job"]
            safe_request_id = job["request_id"]
            safe_job_id = job["job_id"]
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                    job_id=safe_job_id,
                    wait="",
                    timeout="",
                    include_events="",
                )):
                status_payload = json.loads(web_channel.ImageJobsHandler().GET())
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                    job_id=safe_job_id,
                    wait="1",
                    timeout="1",
                    include_events="1",
                )):
                collect_payload = json.loads(web_channel.ImageJobsHandler().GET())
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "status",
                    "include_events": True,
                }).encode("utf-8")):
                action_status_payload = json.loads(web_channel.ImageJobActionHandler().POST(safe_job_id))
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "collect",
                    "wait": False,
                    "include_events": True,
                }).encode("utf-8")):
                action_collect_payload = json.loads(web_channel.ImageJobActionHandler().POST(safe_job_id))
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "cancel",
                    "include_events": True,
                }).encode("utf-8")):
                cancel_payload = json.loads(web_channel.ImageJobActionHandler().POST(safe_job_id))
            events = ledger.events_for_request(safe_request_id, limit=0)

        serialized = json.dumps({
            "start": start_payload,
            "status": status_payload,
            "collect": collect_payload,
            "action_status": action_status_payload,
            "action_collect": action_collect_payload,
            "cancel": cancel_payload,
            "events": events,
        }, ensure_ascii=False)
        self.assertEqual(start_payload["status"], "success")
        self.assertEqual(job["status"], "completed")
        self.assertTrue(safe_request_id.startswith("req-image-job-"))
        self.assertTrue(safe_job_id.startswith("image-job-"))
        self.assertNotIn("privateprompt", serialized)
        self.assertEqual(start_payload["projection"]["state"], "unknown")
        self.assertEqual(start_payload["projection"]["image_jobs"][0]["status"], "completed")
        self.assertEqual(len(start_payload["projection"]["image_jobs"][0]["artifacts"]), 2)
        self.assertIn("events", start_payload["projection"])
        self.assertEqual(status_payload["job"]["status"], "completed")
        self.assertEqual(collect_payload["job"]["status"], "completed")
        self.assertEqual(action_status_payload["job"]["status"], "completed")
        self.assertEqual(action_collect_payload["job"]["status"], "completed")
        self.assertEqual(cancel_payload["job"]["status"], "completed")
        self.assertEqual([event["event_type"] for event in events][0], "image_job.started")
        self.assertIn("image_job.completed", [event["event_type"] for event in events])

    def test_v022_image_jobs_api_projects_auditable_parallelism_policy(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            start_body = {
                "action": "start",
                "dry_run": True,
                "synchronous": True,
                "include_events": True,
                "request_id": "req-image-policy",
                "session_id": "session-image-policy",
                "job_id": "image-job-policy",
                "prompt": "draw a bounded image policy test",
                "count": 6,
                "max_parallel": 99,
                "provider": "test-provider",
                "model": "test-model",
            }
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "_get_workspace_root", return_value=tempfile.gettempdir()), \
                patch.object(web_channel, "conf", return_value={
                    "image_job_max_parallel": 3,
                    "image_provider_concurrency": 2,
                    "image_job_hard_max_parallel": 4,
                }), \
                patch.object(web_channel.web, "data", return_value=json.dumps(start_body).encode("utf-8")):
                start_payload = json.loads(web_channel.ImageJobsHandler().POST())
            events = ledger.events_for_request("req-image-policy", limit=0)

        started_payload = next(event["payload"] for event in events if event["event_type"] == "image_job.started")
        projected_job = start_payload["projection"]["image_jobs"][0]

        self.assertEqual(start_payload["status"], "success")
        self.assertEqual(start_payload["job"]["status"], "completed")
        for payload in (started_payload, projected_job):
            self.assertEqual(payload["parallelism_policy_version"], "v1")
            self.assertEqual(payload["task_count"], 6)
            self.assertEqual(payload["requested_max_parallel"], 99)
            self.assertEqual(payload["configured_max_parallel"], 3)
            self.assertEqual(payload["provider_max_parallel"], 2)
            self.assertEqual(payload["hard_max_parallel"], 4)
            self.assertEqual(payload["effective_max_parallel"], 2)
            self.assertTrue(payload["parallelism_clamped"])
            self.assertEqual(payload["parallelism_clamp_reason"], "provider_max_parallel")
        self.assertEqual(started_payload["max_parallel"], 2)
        self.assertEqual(projected_job["max_parallel"], 2)

    def test_v022_image_job_service_reuses_ocr_brief_by_input_hash_without_public_leak(self):
        from agent.protocol import ImageJobService, RuntimeProjectionService, get_run_event_ledger

        provider_calls = []
        runner_briefs = []

        def ocr_provider(payload):
            provider_calls.append(dict(payload))
            return {
                "brief": "visible OCR text privateprompt sk-test-token should stay internal",
                "provider": "test",
            }

        def runner(task, emit_progress, cancel_event):
            runner_briefs.append(task.get("_ocr_brief"))
            emit_progress("provider_request", progress=0.5, detail={"source": "test", "provider": "test"})
            return {
                "kind": "image",
                "title": f"{task['task_id']}.png",
                "path": str(Path(tempfile.gettempdir()) / f"{task['task_id']}.png"),
            }

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            service = ImageJobService(ledger)
            status = service.start(
                request_id="req-image-ocr-reuse",
                session_id="session-image-ocr-reuse",
                operation="edit",
                tasks=[
                    {
                        "prompt": "edit without leaking prompt",
                        "image_url": "https://example.invalid/reference.png?token=privateprompt",
                    },
                    {
                        "prompt": "edit again",
                        "image_url": "https://example.invalid/reference.png?token=privateprompt",
                    },
                ],
                runner=runner,
                job_id="image-job-ocr-reuse",
                metadata={"source": "test"},
                max_parallel=2,
                ocr_provider=ocr_provider,
                ocr_reuse=True,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-ocr-reuse", limit=0)
            projection = RuntimeProjectionService(ledger).request_projection("req-image-ocr-reuse")

        ocr_payloads = [event["payload"] for event in events if event["event_type"] == "image_job.progress" and event["payload"].get("status") == "ocr"]
        hits = sorted(payload["ocr_cache_hit"] for payload in ocr_payloads)
        cache_keys = {payload["ocr_cache_key"] for payload in ocr_payloads}
        brief_hashes = {payload["ocr_brief_hash"] for payload in ocr_payloads}
        projected_job = projection["image_jobs"][0]
        projected_tasks = projected_job["tasks"]
        serialized_public = json.dumps({"events": events, "projection": projection}, ensure_ascii=False)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(len(runner_briefs), 2)
        self.assertEqual(runner_briefs[0], runner_briefs[1])
        self.assertEqual(len(ocr_payloads), 2)
        self.assertEqual(hits, [False, True])
        self.assertEqual(len(cache_keys), 1)
        self.assertEqual(len(brief_hashes), 1)
        self.assertEqual(projected_job["ocr_cache_hit_count"], 1)
        self.assertEqual(projected_job["ocr_cache_miss_count"], 1)
        self.assertGreaterEqual(projected_job["ocr_total_ms"], 0)
        self.assertEqual(sorted(task["ocr_cache_hit"] for task in projected_tasks), [False, True])
        self.assertTrue(all(task.get("ocr_brief_hash") for task in projected_tasks))
        self.assertNotIn("visible OCR text", serialized_public)
        self.assertNotIn("privateprompt", serialized_public)
        self.assertNotIn("sk-test-token", serialized_public)

    def test_v022_image_jobs_api_projects_ocr_reuse_from_backend_events(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            start_body = {
                "action": "start",
                "dry_run": True,
                "synchronous": True,
                "include_events": True,
                "ocr_reuse": True,
                "request_id": "req-image-ocr-api",
                "session_id": "session-image-ocr-api",
                "job_id": "image-job-ocr-api",
                "prompt": "draw from a reference without public OCR text",
                "image_url": "https://example.invalid/reference.png?token=privateprompt",
                "count": 2,
                "max_parallel": 2,
            }
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "_get_workspace_root", return_value=tempfile.gettempdir()), \
                patch.object(web_channel, "_image_job_output_dir", return_value=tempfile.gettempdir()), \
                patch.object(web_channel, "conf", return_value={}), \
                patch.object(web_channel.web, "data", return_value=json.dumps(start_body).encode("utf-8")):
                payload = json.loads(web_channel.ImageJobsHandler().POST())
            events = ledger.events_for_request("req-image-ocr-api", limit=0)

        progress_payloads = [event["payload"] for event in events if event["event_type"] == "image_job.progress"]
        ocr_payloads = [item for item in progress_payloads if item.get("status") == "ocr"]
        started_payload = next(event["payload"] for event in events if event["event_type"] == "image_job.started")
        projected_job = payload["projection"]["image_jobs"][0]
        projected_tasks = projected_job["tasks"]
        serialized_public = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["job"]["status"], "completed")
        self.assertTrue(started_payload["ocr_cache_enabled"])
        self.assertEqual(len(ocr_payloads), 2)
        self.assertEqual(sorted(item["ocr_cache_hit"] for item in ocr_payloads), [False, True])
        self.assertEqual(len({item["ocr_cache_key"] for item in ocr_payloads}), 1)
        self.assertEqual(projected_job["ocr_cache_hit_count"], 1)
        self.assertEqual(projected_job["ocr_cache_miss_count"], 1)
        self.assertGreaterEqual(projected_job["ocr_total_ms"], 0)
        self.assertEqual(sorted(task["ocr_cache_hit"] for task in projected_tasks), [False, True])
        self.assertTrue(all(task.get("ocr_brief_hash") for task in projected_tasks))
        self.assertNotIn("dry-run-image-brief", serialized_public)
        self.assertNotIn("privateprompt", serialized_public)
        self.assertNotIn("draw from a reference", serialized_public)

    def test_v022_image_jobs_vision_ocr_provider_requires_tool_permission(self):
        from channel.web import web_channel

        class FakeBroker:
            def __init__(self, allowed):
                self.allowed = allowed
                self.calls = []

            def authorize_noninteractive(self, tool_name, arguments=None):
                self.calls.append((tool_name, dict(arguments or {})))
                return {"allowed": self.allowed, "reason": "test"}

        denied_broker = FakeBroker(False)
        allowed_broker = FakeBroker(True)

        with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=denied_broker), \
            patch("agent.tools.vision.vision.Vision.execute", return_value={"content": "should not run"}) as denied_execute:
            with self.assertRaises(RuntimeError):
                web_channel._image_job_vision_ocr_provider({
                    "image": "https://example.invalid/privateprompt.png?token=sk-test-token",
                })
            denied_execute.assert_not_called()

        denied_args = denied_broker.calls[0][1]
        self.assertEqual(denied_broker.calls[0][0], "vision")
        self.assertTrue(denied_args["image"].startswith("image-input-"))
        self.assertNotIn("privateprompt", json.dumps(denied_args, ensure_ascii=False))
        self.assertNotIn("sk-test-token", json.dumps(denied_args, ensure_ascii=False))

        with patch("common.ecorex_tool_permissions.get_tool_permission_broker", return_value=allowed_broker), \
            patch.object(web_channel, "_get_workspace_root", return_value=tempfile.gettempdir()), \
            patch("agent.tools.vision.vision.Vision.execute", return_value={"content": "allowed brief"}) as allowed_execute:
            result = web_channel._image_job_vision_ocr_provider({"image": "https://example.invalid/reference.png"})

        self.assertEqual(result["content"], "allowed brief")
        allowed_execute.assert_called_once()
        self.assertEqual(allowed_broker.calls[0][0], "vision")

    def test_v022_image_job_ocr_provider_failure_is_observable_without_raw_error(self):
        from agent.protocol import ImageJobService, get_run_event_ledger

        class FailedVisionResult:
            status = "error"
            result = "privateprompt raw provider failure sk-test-token"

        runner_tasks = []

        def runner(task, emit_progress, cancel_event):
            runner_tasks.append(dict(task))
            return {
                "kind": "image",
                "title": "ocr-failure-continues.png",
                "path": str(Path(tempfile.gettempdir()) / "ocr-failure-continues.png"),
            }

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            service = ImageJobService(ledger)
            status = service.start(
                request_id="req-image-ocr-fail",
                session_id="session-image-ocr-fail",
                operation="edit",
                tasks=[{
                    "prompt": "edit after ocr failure",
                    "image_url": "https://example.invalid/privateprompt.png",
                }],
                runner=runner,
                job_id="image-job-ocr-fail",
                metadata={"source": "test"},
                ocr_provider=lambda payload: FailedVisionResult(),
                ocr_reuse=True,
                synchronous=True,
            )
            events = ledger.events_for_request("req-image-ocr-fail", limit=0)

        ocr_payload = next(
            event["payload"] for event in events
            if event["event_type"] == "image_job.progress" and event["payload"].get("status") == "ocr"
        )
        serialized_events = json.dumps(events, ensure_ascii=False)

        self.assertEqual(status["status"], "completed")
        self.assertEqual(ocr_payload["taxonomy"], "ocr_failed")
        self.assertFalse(ocr_payload["ocr_cache_hit"])
        self.assertIn("ocr_cache_key", ocr_payload)
        self.assertEqual(runner_tasks[0].get("_ocr_brief"), None)
        self.assertNotIn("raw provider failure", serialized_events)
        self.assertNotIn("sk-test-token", serialized_events)
        self.assertNotIn("privateprompt", serialized_events)

    def test_v022_image_job_skill_runner_keeps_sensitive_args_in_memory_payload(self):
        from channel.web import web_channel

        calls = {}

        def fake_provider_run(payload, **kwargs):
            calls["payload"] = dict(payload)
            calls["kwargs"] = dict(kwargs)
            return {
                "returncode": 0,
                "payload": {"images": [{"url": str(Path(tempfile.gettempdir()) / "safe.png")}]},
                "stderr": "",
            }

        with patch.object(web_channel, "run_image_generation_payload", side_effect=fake_provider_run), \
            patch.object(web_channel, "_image_job_output_dir", return_value=tempfile.gettempdir()):
            result = web_channel._image_job_skill_runner(
                {
                    "prompt": "private user prompt",
                    "provider": "privateprompt",
                    "model": "privateprompt",
                    "_ocr_brief": "visible OCR text privateprompt should stay out of argv",
                },
                lambda *args, **kwargs: None,
                threading.Event(),
            )

        serialized_runner_args = json.dumps(
            {
                "script_path": str(calls["kwargs"]["script_path"]),
                "output_dir": str(calls["kwargs"]["output_dir"]),
            },
            ensure_ascii=False,
        )
        self.assertNotIn("private user prompt", serialized_runner_args)
        self.assertNotIn("privateprompt", serialized_runner_args)
        self.assertNotIn("visible OCR text", serialized_runner_args)
        self.assertIn("private user prompt", calls["payload"]["prompt"])
        self.assertIn("privateprompt", calls["payload"]["provider"])
        self.assertIn("visible OCR text", calls["payload"]["ocr_brief"])
        self.assertEqual(result["artifacts"][0]["kind"], "image")

    def test_v022_image_jobs_api_surfaces_skill_model_fallback_telemetry(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        calls = {}
        image_path = str(Path(tempfile.gettempdir()) / "fallback-from-skill.png")

        def fake_provider_run(payload, **kwargs):
            calls["payload"] = dict(payload)
            calls["kwargs"] = dict(kwargs)
            return {
                "returncode": 0,
                "payload": {
                    "provider": "OpenAI",
                    "model": "gpt-image-2",
                    "attempted_provider_count": 1,
                    "model_fallback": {
                        "used": True,
                        "provider": "OpenAI",
                        "from_model": "gpt-image-2-pro",
                        "to_model": "gpt-image-2",
                        "reason": "client_error",
                        "message": "model unavailable private prompt should not persist",
                    },
                    "images": [{"url": image_path}],
                },
                "stderr": "provider stderr with private prompt should not persist",
            }

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            body = {
                "action": "start",
                "synchronous": True,
                "include_events": True,
                "request_id": "req-image-provider-fallback",
                "session_id": "session-image-provider-fallback",
                "job_id": "image-job-provider-fallback",
                "prompt": "draw fallback telemetry without leaking this prompt",
                "provider": "openai",
                "model": "gpt-image-2-pro",
            }
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "run_image_generation_payload", side_effect=fake_provider_run), \
                patch.object(web_channel, "_image_job_output_dir", return_value=tempfile.gettempdir()), \
                patch.object(web_channel.web, "data", return_value=json.dumps(body).encode("utf-8")):
                payload = json.loads(web_channel.ImageJobsHandler().POST())
            events = ledger.events_for_request("req-image-provider-fallback", limit=0)

        progress_events = [event for event in events if event["event_type"] == "image_job.progress"]
        progress_payloads = [event["payload"] for event in progress_events]
        fallback_payload = next(item for item in progress_payloads if item.get("status") == "fallback")
        provider_response = next(item for item in progress_payloads if item.get("status") == "provider_response")
        projected_job = payload["projection"]["image_jobs"][0]
        projected_task = projected_job["tasks"][0]
        serialized_public = json.dumps(payload, ensure_ascii=False)
        serialized_events = json.dumps(events, ensure_ascii=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["job"]["status"], "completed")
        serialized_runner_args = json.dumps(
            {
                "script_path": str(calls["kwargs"]["script_path"]),
                "output_dir": str(calls["kwargs"]["output_dir"]),
            },
            ensure_ascii=False,
        )
        self.assertNotIn("draw fallback telemetry", serialized_runner_args)
        self.assertIn("draw fallback telemetry", calls["payload"]["prompt"])
        self.assertEqual(fallback_payload["fallback_used"], True)
        self.assertEqual(fallback_payload["fallback_provider"], "OpenAI")
        self.assertEqual(fallback_payload["fallback_from_model"], "gpt-image-2-pro")
        self.assertEqual(fallback_payload["fallback_to_model"], "gpt-image-2")
        self.assertEqual(fallback_payload["fallback_reason"], "client_error")
        self.assertEqual(provider_response["attempted_provider_count"], 1)
        self.assertEqual(projected_job["fallback_used"], True)
        self.assertEqual(projected_job["fallback_provider"], "OpenAI")
        self.assertEqual(projected_job["fallback_from_model"], "gpt-image-2-pro")
        self.assertEqual(projected_job["fallback_to_model"], "gpt-image-2")
        self.assertEqual(projected_job["fallback_reason"], "client_error")
        self.assertEqual(projected_job["last_provider"], "OpenAI")
        self.assertEqual(projected_job["last_model"], "gpt-image-2")
        self.assertEqual(projected_job["attempted_provider_count"], 1)
        self.assertEqual(projected_task["fallback_to_model"], "gpt-image-2")
        self.assertNotIn("model unavailable private prompt", serialized_public)
        self.assertNotIn("provider stderr", serialized_events)
        self.assertNotIn("draw fallback telemetry", serialized_public)

    def test_v022_image_jobs_api_recovers_status_from_projection_after_service_reset(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "start",
                    "dry_run": True,
                    "synchronous": True,
                    "request_id": "req-image-recovery",
                    "session_id": "session-image-recovery",
                    "prompt": "draw a recoverable test image",
                    "count": 1,
                }).encode("utf-8")):
                start_payload = json.loads(web_channel.ImageJobsHandler().POST())
            safe_job_id = start_payload["job"]["job_id"]
            reset_image_job_service_for_tests(ledger)
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                    job_id=safe_job_id,
                    wait="",
                    timeout="",
                    include_events="1",
                )):
                recovered_status = json.loads(web_channel.ImageJobsHandler().GET())
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "status",
                    "include_events": True,
                }).encode("utf-8")):
                recovered_action_status = json.loads(web_channel.ImageJobActionHandler().POST(safe_job_id))

        self.assertEqual(recovered_status["job"]["status"], "completed")
        self.assertTrue(recovered_status["job"]["recovered_from_projection"])
        self.assertEqual(recovered_status["projection"]["image_jobs"][0]["status"], "completed")
        self.assertEqual(len(recovered_status["projection"]["image_jobs"][0]["artifacts"]), 1)
        self.assertEqual(recovered_action_status["job"]["status"], "completed")
        self.assertTrue(recovered_action_status["job"]["recovered_from_projection"])

    def test_v022_image_jobs_api_recovery_sanitizes_legacy_request_id(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            legacy_request_id = "req-privateprompt"
            job_id = "image-job-safe"
            ledger.append_event(
                request_id=legacy_request_id,
                session_id="session-privateprompt",
                turn_id="turn-privateprompt",
                event_type="image_job.started",
                payload={
                    "job_id": job_id,
                    "operation": "generate",
                    "task_count": 1,
                    "tasks": [{"task_id": "task-1", "operation": "generate", "output_count": 1}],
                },
                idempotency_key="legacy-image-started",
                source="image_job_service",
            )
            ledger.append_event(
                request_id=legacy_request_id,
                session_id="session-privateprompt",
                turn_id="turn-privateprompt",
                event_type="image_job.completed",
                payload={"job_id": job_id, "artifact_count": 0},
                idempotency_key="legacy-image-completed",
                source="image_job_service",
            )
            ledger.append_event(
                request_id="req-image-decoy",
                session_id="session-image-decoy",
                turn_id="turn-image-decoy",
                event_type="image_job.started",
                payload={
                    "job_id": f"{job_id}-suffix",
                    "operation": "generate",
                    "task_count": 1,
                    "tasks": [{"task_id": "task-1", "operation": "generate", "output_count": 1}],
                },
                idempotency_key="legacy-image-decoy",
                source="image_job_service",
            )
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                    job_id=job_id,
                    wait="",
                    timeout="",
                    include_events="1",
                )):
                payload = json.loads(web_channel.ImageJobsHandler().GET())

        serialized = json.dumps(payload, ensure_ascii=False)
        safe_request_id = payload["job"]["request_id"]
        safe_session_id = payload["job"]["session_id"]
        safe_turn_id = payload["job"]["turn_id"]
        self.assertEqual(payload["job"]["status"], "completed")
        self.assertTrue(payload["job"]["recovered_from_projection"])
        self.assertTrue(safe_request_id.startswith("req-image-job-"))
        self.assertTrue(safe_session_id.startswith("session-image-job-"))
        self.assertTrue(safe_turn_id.startswith("turn-image-job-"))
        self.assertEqual(payload["projection"]["request_id"], safe_request_id)
        self.assertEqual(payload["projection"]["session_id"], safe_session_id)
        self.assertEqual(payload["projection"]["turn_id"], safe_turn_id)
        self.assertEqual(payload["projection"]["events"][0]["request_id"], safe_request_id)
        self.assertEqual(payload["projection"]["events"][0]["session_id"], safe_session_id)
        self.assertEqual(payload["projection"]["events"][0]["turn_id"], safe_turn_id)
        self.assertNotIn("privateprompt", serialized)

    def test_v022_image_jobs_api_recovered_cancel_does_not_write_terminal_event(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            request_id = "req-image-running"
            job_id = "image-job-running"
            ledger.append_event(
                request_id=request_id,
                session_id="session-image-running",
                turn_id="turn-image-running",
                event_type="image_job.started",
                payload={
                    "job_id": job_id,
                    "operation": "generate",
                    "task_count": 1,
                    "tasks": [{"task_id": "task-1", "operation": "generate", "output_count": 1}],
                },
                idempotency_key="running-image-started",
                source="image_job_service",
            )
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "cancel",
                    "include_events": True,
                }).encode("utf-8")):
                payload = json.loads(web_channel.ImageJobActionHandler().POST(job_id))
            events = ledger.events_for_request(request_id, limit=0)

        self.assertEqual(payload["job"]["status"], "running")
        self.assertTrue(payload["job"]["recovered_from_projection"])
        self.assertFalse(payload["job"]["cancelled"])
        self.assertEqual(payload["job"]["cancel_unavailable_reason"], "recovered_projection_no_live_worker")
        self.assertEqual(payload["projection"]["image_jobs"][0]["status"], "running")
        self.assertNotIn("image_job.cancelled", [event["event_type"] for event in events])

    def test_v022_image_jobs_api_rejects_missing_prompt_without_runtime_events(self):
        from agent.protocol import get_run_event_ledger, reset_image_job_service_for_tests
        from channel.web import web_channel

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            reset_image_job_service_for_tests(ledger)
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "start",
                    "dry_run": True,
                }).encode("utf-8")):
                payload = json.loads(web_channel.ImageJobsHandler().POST())
            with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel.web, "data", return_value=json.dumps({
                    "action": "start",
                    "dry_run": True,
                    "tasks": [{"provider": "privateprompt"}],
                }).encode("utf-8")):
                task_payload = json.loads(web_channel.ImageJobsHandler().POST())
            events = ledger.list_events(limit=100)

        self.assertEqual(payload["status"], "error")
        self.assertIn("prompt or tasks is required", payload["message"])
        self.assertEqual(task_payload["status"], "error")
        self.assertIn("each image task requires prompt", task_payload["message"])
        self.assertEqual(events, [])

    def test_v022_runtime_projection_api_returns_history_projection_page(self):
        from agent.memory.conversation_store import ConversationStore
        from agent.protocol import get_run_event_ledger
        from channel.web.web_channel import RuntimeProjectionHandler

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-v022-api-history", [
                {"role": "user", "content": "history prompt"},
                {"role": "assistant", "content": "old history answer"},
            ], channel_type="web")
            store.attach_extras_to_assistant_seq("session-v022-api-history", 1, {
                "request_id": "req-v022-api-history",
                "user_seq": 0,
                "bot_seq": 1,
            })
            with isolated_run_ledger():
                ledger = get_run_event_ledger()
                ledger.append_event(
                    request_id="req-v022-api-history",
                    session_id="session-v022-api-history",
                    event_type="message.assistant.finalized",
                    payload={"content": "projection-owned answer"},
                    idempotency_key="req-v022-api-history:final",
                )
                with patch("agent.memory.get_conversation_store", return_value=store), \
                    patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
                        request_id="",
                        session_id="session-v022-api-history",
                        after_event_id="0",
                        limit="1000",
                        include_events="",
                        history_page="1",
                        page_size="20",
                    )):
                    payload = json.loads(RuntimeProjectionHandler().GET())

        assistant = next(
            item for item in payload["projection"]["history"]["messages"]
            if item["role"] == "assistant"
        )
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["mode"], "session_history")
        self.assertEqual(payload["projection"]["history_source"], "conversation_store+runtime_projection")
        self.assertEqual(assistant["content"], "projection-owned answer")
        self.assertEqual(assistant["runtime_projection"]["state"], "completed")
        self.assertNotIn("events", payload["projection"])

    def test_v022_frontend_has_typed_runtime_projection_fetch_contract(self):
        root = Path(__file__).resolve().parents[1]
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("export type RuntimeProjectionEvent", api_source)
        self.assertIn("export type RuntimeRequestProjection", api_source)
        self.assertIn("export type RuntimeSessionProjection", api_source)
        self.assertIn("terminal_message?: string", api_source)
        self.assertIn("export type RuntimeProjectionInput", api_source)
        self.assertIn("mode: \"request\";", api_source)
        self.assertIn("mode: \"session\";", api_source)
        self.assertIn("export async function loadRuntimeProjection", api_source)
        self.assertIn("`/api/runtime-projection?", api_source)
        self.assertIn('params.set("request_id", input.requestId)', api_source)
        self.assertIn('params.set("session_id", input.sessionId)', api_source)
        self.assertIn('params.set("after_event_id"', api_source)
        self.assertIn("loadRuntimeProjection,", app_source)
        self.assertIn("async function recoverRequestFromProjection", app_source)
        self.assertIn("projectionRecoveryDecision(projection)", app_source)
        self.assertIn("Array.isArray(item.artifacts) ? item.artifacts : item.extras?.artifacts", app_source)
        self.assertLess(
            app_source.index("clearStreamDeltaBuffers(sessionId, requestId);"),
            app_source.index("const projectedContent = redactInternalPromptText(decision.content")
        )
        self.assertIn('loadRuntimeProjection({ mode: "request", requestId, sessionId })', app_source)
        self.assertIn("sessionId?: string;", api_source)
        self.assertIn("sessionId?: string;", api_source[api_source.index("export function openMessageStream"):])
        self.assertIn("params.set(\"session_id\", input.sessionId)", api_source[api_source.index("export function openMessageStream"):])
        self.assertIn("sessionId,", app_source[app_source.index("const cleanup = openMessageStream({"):])
        self.assertIn("sessionId: requestSessionId,", app_source)
        self.assertIn("async function handleStreamError", app_source)
        self.assertIn("if (await recoverRequestFromProjection(sessionId, assistantId, requestId)) return;", app_source)
        self.assertIn("void handleStreamError(sessionId, assistantId, requestId);", app_source)
        self.assertIn("void handleStreamError(requestSessionId, assistantId, requestId);", app_source)
        self.assertIn("function hasScheduledStreamReconnect", app_source)
        self.assertIn("if (hasScheduledStreamReconnect(sessionId, requestId)) return;", app_source)
        self.assertIn("streamReconnectChecks.current[reconnectKey] = true;", app_source)
        self.assertIn("delete streamReconnectChecks.current[reconnectKey];", app_source)
        self.assertIn("streamReconnectTimers.current[reconnectKey] = window.setTimeout(() => {", app_source)
        self.assertIn("delete streamReconnectTimers.current[reconnectKey];", app_source)

    def test_v022_frontend_projection_recovery_decision_executes_terminal_cases(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const ts = require("typescript");
const source = fs.readFileSync("src/utils/runtimeProjectionRecovery.ts", "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText;
const exportsObject = {};
const sandbox = { exports: exportsObject, module: { exports: exportsObject }, console };
vm.createContext(sandbox);
vm.runInContext(compiled, sandbox, { filename: "runtimeProjectionRecovery.js" });
const { projectionRecoveryDecision } = sandbox.module.exports;
const empty = projectionRecoveryDecision({ event_count: 0, messages: [] });
const failed = projectionRecoveryDecision({
  event_count: 3,
  state: "failed",
  terminal_message: "actual failure",
  messages: [{ role: "assistant", content: "partial" }]
});
const completed = projectionRecoveryDecision({
  event_count: 2,
  state: "completed",
  messages: [{ role: "assistant", content: "final answer" }]
});
const running = projectionRecoveryDecision({
  event_count: 2,
  state: "streaming",
  messages: [{ role: "assistant", content: "partial" }]
});
process.stdout.write(JSON.stringify({ empty, failed, completed, running }));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root / "desktop",
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["empty"]["reason"], "empty")
        self.assertEqual(payload["running"]["reason"], "non-terminal")
        self.assertFalse(payload["running"]["handled"])
        self.assertTrue(payload["failed"]["handled"])
        self.assertEqual(payload["failed"]["terminalPhase"], "failed")
        self.assertEqual(payload["failed"]["content"], "actual failure")
        self.assertTrue(payload["completed"]["markCompleted"])
        self.assertEqual(payload["completed"]["content"], "final answer")

    def test_v022_frontend_runtime_projection_fetch_executes_request_and_session_modes(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const ts = require("typescript");
const source = fs.readFileSync("src/services/ecorexApi.ts", "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText;
const calls = [];
const exportsObject = {};
const sandbox = {
  exports: exportsObject,
  module: { exports: exportsObject },
  console,
  URLSearchParams,
  Map,
  Promise,
  setTimeout,
  clearTimeout,
  window: {
    setTimeout,
    clearTimeout,
      ecorexDesktop: {
        apiJson: async (request) => {
          calls.push(request);
        if (String(request.path).includes("request_id=")) {
          return {
            status: "success",
            mode: "request",
            latest_event_id: 7,
            projection: { request_id: "req-a", messages: [{ role: "assistant", content: "ok" }], latest_event_id: 7 }
          };
        }
        if (String(request.path).includes("session_id=")) {
          return {
            status: "success",
            mode: "session",
            latest_event_id: 12,
            projection: { session_id: "session-a", requests: [], latest_event_id: 12 }
          };
        }
        return {
          status: "error",
          message: "unexpected runtime projection path"
        };
      }
    }
  }
};
vm.createContext(sandbox);
vm.runInContext(compiled, sandbox, { filename: "ecorexApi.js" });
(async () => {
  const requestResult = await sandbox.module.exports.loadRuntimeProjection({ mode: "request", requestId: "req-a", sessionId: "session-a", limit: 2 });
  const sessionResult = await sandbox.module.exports.loadRuntimeProjection({ mode: "session", sessionId: "session-a", afterEventId: 5, limit: 3 });
  process.stdout.write(JSON.stringify({ calls, requestResult, sessionResult }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root / "desktop",
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["requestResult"]["mode"], "request")
        self.assertEqual(payload["requestResult"]["projection"]["request_id"], "req-a")
        self.assertEqual(payload["sessionResult"]["mode"], "session")
        self.assertEqual(payload["sessionResult"]["projection"]["session_id"], "session-a")
        self.assertEqual(payload["calls"][0]["path"], "/api/runtime-projection?request_id=req-a&session_id=session-a&limit=2")
        self.assertEqual(
            payload["calls"][1]["path"],
            "/api/runtime-projection?session_id=session-a&after_event_id=5&limit=3",
        )


class TestWebPhase1SyncProducerSource(unittest.TestCase):
    def _console_source(self):
        return (Path(__file__).resolve().parents[1] / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")

    def _web_channel_source(self):
        return (Path(__file__).resolve().parents[1] / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    def _agent_bridge_source(self):
        return (Path(__file__).resolve().parents[1] / "bridge" / "agent_bridge.py").read_text(encoding="utf-8")

    def test_phase1_producer_reports_before_background_render_skip(self):
        source = self._console_source()

        self.assertIn("type: 'phase1_sync'", source)
        self.assertIn("async function reportPhase1StreamItem", source)
        self.assertIn("void reportPhase1RunEvent(ownerSession, requestId, 'run.accepted'", source)
        self.assertIn("eventType: 'run.completed'", source)
        self.assertIn("eventType: 'artifact.updated'", source)
        self.assertLess(
            source.index("void reportPhase1StreamItem(ownerSession, requestId, item);"),
            source.index("if (ownerSession !== sessionId)"),
        )

    def test_phase1_artifact_metadata_does_not_emit_raw_paths_or_bodies(self):
        source = self._console_source()
        deny_block = source[
            source.index("const PHASE1_DETAIL_DENY_KEYS"):
            source.index("function phase1SyncEnabled")
        ]
        for key in ("content", "final_text", "message", "prompt", "response", "result", "path", "url"):
            self.assertIn(f"'{key}'", deny_block)

        metadata_function = source[
            source.index("async function phase1ArtifactMetadata"):
            source.index("async function reportPhase1Telemetry")
        ]
        for raw_field in ("path", "url", "previewUrl", "relativePath", "content", "final_text", "finalText"):
            self.assertNotRegex(metadata_function, rf"\b{raw_field}\s*:")
        self.assertIn("pathHash:", metadata_function)
        self.assertIn("pathExt:", metadata_function)

    def test_web_app_bridge_observes_sse_without_desktop_source_changes(self):
        source = self._web_channel_source()

        self.assertIn("function installPhase1EventSourceSync()", source)
        self.assertIn("new NativeEventSource(url, options)", source)
        self.assertIn('clientJson("/sync/events"', source)
        self.assertIn('type: "phase1_sync"', source)
        self.assertIn('eventType: "run.completed"', source)
        self.assertIn('eventType: "artifact.updated"', source)
        self.assertIn("pathHash:", source)
        bridge_function = source[
            source.index("async function phase1ArtifactMetadata"):
            source.index("async function phase1Emit")
        ]
        for raw_field in ("path", "url", "previewUrl", "relativePath", "content", "final_text", "finalText"):
            self.assertNotRegex(bridge_function, rf"\b{raw_field}\s*:")

    def test_web_app_bridge_phase2_messages_are_policy_gated(self):
        source = self._web_channel_source()

        self.assertIn("function phase2LocalSwitchEnabled()", source)
        self.assertIn("async function phase2SyncEnabled()", source)
        self.assertIn('clientJson("/sync/policy", "GET", undefined, true)', source)
        self.assertIn("phase2 && phase2.chatBodiesEnabled", source)
        self.assertIn('clientJson("/sync/messages", "POST"', source)
        self.assertIn("phase2EmitUserMessage(request, payload).catch", source)
        self.assertIn("var assistantContent = item.final_text !== undefined ? item.final_text : item.content", source)
        self.assertIn('role: "user"', source)
        self.assertIn('role: "assistant"', source)

    def test_web_app_bridge_phase3_artifact_files_are_policy_gated_and_chunked(self):
        source = self._web_channel_source()

        self.assertIn("function phase3LocalSwitchEnabled()", source)
        self.assertIn("async function phase3Policy()", source)
        self.assertIn("phase3 && phase3.artifactFilesEnabled && phase3.killSwitch !== true", source)
        self.assertIn('clientJson("/sync/policy", "GET", undefined, true)', source)
        self.assertIn('clientJson("/sync/artifact-blobs/" + encodeURIComponent(artifactId), "PUT"', source)
        self.assertIn("contentSha256: contentSha256", source)
        self.assertIn("chunkIndex: i", source)
        self.assertIn("chunkCount: chunkCount", source)
        self.assertIn("chunkSha256: chunkHash", source)
        self.assertIn("contentBase64: phase3ArrayBufferToBase64", source)
        self.assertIn("await phase3Sleep(Math.ceil((chunk.byteLength / bytesPerSecond) * 1000))", source)
        self.assertIn("phase3EmitArtifactFile(phase3Artifacts[j].raw", source)


class TestProjectSessionSourceContracts(unittest.TestCase):
    def _app_source(self):
        return (Path(__file__).resolve().parents[1] / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

    def _api_source(self):
        return (Path(__file__).resolve().parents[1] / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

    def _web_channel_source(self):
        return (Path(__file__).resolve().parents[1] / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    def _agent_bridge_source(self):
        return (Path(__file__).resolve().parents[1] / "bridge" / "agent_bridge.py").read_text(encoding="utf-8")

    def test_react_project_session_uses_pending_start_and_structured_binding(self):
        source = self._app_source()

        self.assertIn("SESSION_PROJECT_BINDINGS_STORAGE_KEY", source)
        self.assertIn("pendingProjectStart", source)
        self.assertIn("ecorex-pending-project-", source)
        self.assertIn("bindSessionToProject", source)
        self.assertIn('const projectBinding = project ? projectBindingFromProject(project, "project-new-session") : null;', source)
        self.assertIn("setSessionProjects((current) => ({ ...current, [id]: projectBinding.projectId }))", source)
        self.assertIn("setSessionProjectBindings((current) => ({ ...current, [id]: projectBinding }))", source)
        self.assertIn("data-session-ownership={rowProjectId ? \"project\" : \"general\"}", source)
        self.assertIn("draggable={false}", source)
        self.assertIn("onDragStart={(event) => event.preventDefault()}", source)
        self.assertIn("type ProjectBindingLookupOptions", source)
        self.assertIn("allowFallbackProject?: boolean;", source)
        self.assertIn("const projectId = sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState);", source)
        self.assertNotIn("sessionProjectIdFromState(sessionId, sessionProjects, sessionUiState, fallbackProject?.id || null)", source)
        self.assertNotIn("projectBindingForSession(sessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, current, projectCatalog, activeProject)", source)
        self.assertNotIn("projectBindingForSession(activeSessionId, sessionProjectBindings, sessionProjects, sessionUiState, projectCatalog, activeProject)", source)
        self.assertIn("pendingProjectSessionId", source)
        self.assertIn("delete nextSessionProjects[pendingProjectSessionId]", source)
        self.assertIn("delete nextSessionProjectBindings[pendingProjectSessionId]", source)
        self.assertIn("projectContext: projectBindingForRequest || null", source)
        self.assertIn("let projectBindingForRequest = projectBindingForSession(requestSessionId, sessionProjectBindingsRef.current, sessionProjectsRef.current, sessionUiState, projectCatalog);", source)
        self.assertIn("let projectForRequest: ProjectFolder | null = pendingProject || (projectBindingForRequest ? projectFolderFromBinding(projectBindingForRequest) : null);", source)
        self.assertIn("} else if (projectBindingForRequest) {", source)
        self.assertNotIn("let projectForRequest: ProjectFolder | null = activeProject;", source)
        self.assertNotIn('bindSessionToProject(requestSessionId, projectForRequest, "project-session-send")', source)
        self.assertIn('let hiddenContext = "";', source)
        self.assertNotIn("function projectContextPrompt", source)
        self.assertNotIn("projectForRequest ? projectContextPrompt(projectForRequest)", source)
        self.assertIn("projectCatalog", source)
        self.assertNotIn("!isPendingProjectSessionId(activeSessionId) && !rows.some", source)

    def test_react_project_session_composer_autosize_and_general_isolation(self):
        source = self._app_source()

        self.assertIn("function syncComposerHeight()", source)
        self.assertIn('textarea.style.height = "auto";', source)
        self.assertIn("const nextHeight = Math.min(textarea.scrollHeight, maxHeight);", source)
        self.assertIn("textarea.style.overflowY = textarea.scrollHeight > maxHeight ? \"auto\" : \"hidden\";", source)
        self.assertIn("window.requestAnimationFrame(syncComposerHeight);", source)
        self.assertIn('setComposerDraft("", { immediate: true });', source)
        self.assertIn("focusComposerSoon();", source)
        self.assertIn("function createDraftSessionId(project?: ProjectFolder | null)", source)
        self.assertIn("const id = createDraftSessionId(project);", source)
        self.assertIn("protectBlankDraftSession(id);", source)
        self.assertIn("pendingProjectStartRef.current = project || null;", source)
        self.assertIn("setPendingProjectStart(project || null);", source)
        self.assertIn("setSessionProjects((current) => ({ ...current, [id]: projectBinding.projectId }))", source)
        self.assertIn("setSessionProjectBindings((current) => ({ ...current, [id]: projectBinding }))", source)
        self.assertIn("for (const row of visibleSessions)", source)
        self.assertIn("if (!row.projectId) {\n        general.push(row);\n        continue;\n      }", source)
        self.assertIn("projectSessions: projectRows", source)
        self.assertIn("generalSessions: general", source)

    def test_session_list_normal_rows_have_no_left_type_icons(self):
        source = self._app_source()
        start = source.index("const renderSessionRow = (row: SessionRow) => {")
        end = source.index("function renderMessageRunTiming", start)
        block = source[start:end]

        self.assertIn("const hasLeadingSessionStatus = isRunning || hasUnread;", block)
        self.assertIn('hasLeadingSessionStatus ? " has-leading-status" : ""', block)
        self.assertIn("isRunning ? <ThinkingIndicator compact /> : hasUnread ? <span className=\"session-unread-dot\" aria-hidden=\"true\" /> : null", block)
        self.assertNotIn("rowProjectId ? <FolderOpen", block)
        self.assertNotIn(": <Bot aria-hidden=\"true\" />", block)
        css = (Path(__file__).resolve().parents[1] / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".session-main.has-leading-status", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", css)

    def test_v024_session_list_visual_cleanup_browser_smoke_contract(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "smoke-v024-session-list-visual-cleanup-browser.py").read_text(encoding="utf-8")

        self.assertIn("General Normal", script)
        self.assertIn("Project Normal", script)
        self.assertIn("Unread Ready", script)
        self.assertIn("Running Task", script)
        self.assertIn("normal row rendered a direct SVG icon", script)
        self.assertIn("unread row missing orange dot", script)
        self.assertIn("running row missing thinking indicator", script)
        self.assertIn("unread clears after read", script)
        self.assertIn("columnCount(item.main) === 2", script)
        self.assertIn("columnCount(unread.main) === 3", script)

    def test_api_posts_project_context_meta(self):
        source = self._api_source()

        self.assertIn("export type ProjectSessionBinding", source)
        self.assertIn("projectContext?: ProjectSessionBinding | null", source)
        self.assertIn("project_context_meta: input.projectContext || null", source)
        self.assertIn("projectContext: result.project_context || null", source)

    def test_web_message_persists_structured_project_context_without_prompt_injection(self):
        source = self._web_channel_source()

        self.assertIn("def _normalize_project_context_meta", source)
        self.assertIn("def _persist_project_session_binding", source)
        self.assertIn("def _project_context_event_summary", source)
        self.assertIn("project_context_meta = _normalize_project_context_meta", source)
        self.assertIn("_persist_project_session_binding(session_id, project_context_meta)", source)
        self.assertIn("\"project_context\": _project_context_event_summary(project_context_meta)", source)
        self.assertIn("context[\"project_context_meta\"] = project_context_meta", source)
        self.assertIn("hidden_context = json_data.get('hidden_context') or ''", source)
        self.assertIn("legacy_project_context = json_data.get('project_context')", source)
        self.assertIn("or (legacy_project_context if isinstance(legacy_project_context, dict) else None)", source)
        self.assertNotIn("def _build_project_context_prompt", source)
        self.assertNotIn("def _safe_read_project_memory_excerpt", source)
        self.assertNotIn("server_project_context", source)
        self.assertNotIn("json_data.get('project_context') or ''", source)
        self.assertNotIn('"activeProjectId": binding.get("projectId")', source)

        bridge_source = self._agent_bridge_source()
        self.assertIn('project_context=context.get("project_context_meta") if context else None', bridge_source)
        self.assertIn('context.get("project_context_meta") if context else None', bridge_source)

    def test_server_project_binding_does_not_switch_active_project(self):
        from common.ecorex_workspace import load_ui_state, save_ui_state
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            save_ui_state(workspace, {
                "projects": [
                    {"id": "p1", "name": "Project A", "path": os.path.join(workspace, "project-a")},
                    {"id": "p2", "name": "Project B", "path": os.path.join(workspace, "project-b")},
                ],
                "activeProjectId": "p1",
                "projectStateMode": "merge",
            })
            binding = {
                "projectId": "p2",
                "projectName": "Project B",
                "projectPath": os.path.join(workspace, "project-b"),
                "memoryPath": os.path.join(workspace, "project-b", ".ecorex", "project-memory.md"),
                "dreamsPath": os.path.join(workspace, "project-b", ".ecorex", "dreams"),
            }
            with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                web_channel._persist_project_session_binding("session-p2", binding)

            state = load_ui_state(workspace)

        self.assertEqual(state["activeProjectId"], "p1")
        self.assertEqual(state["sessionProjects"]["session-p2"], "p2")
        self.assertEqual(state["sessionProjectBindings"]["session-p2"]["projectId"], "p2")


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

    def test_ui_state_empty_replace_does_not_clear_projects_without_explicit_allow(self):
        from common.ecorex_workspace import load_ui_state, save_ui_state

        with tempfile.TemporaryDirectory() as workspace:
            save_ui_state(workspace, {
                "projects": [{"id": "p1", "name": "Project", "path": os.path.join(workspace, "project")}],
                "sessionProjects": {"s1": "p1"},
                "pinnedProjects": {"p1": True},
                "projectStateMode": "merge",
            })
            save_ui_state(workspace, {
                "projects": [],
                "sessionProjects": {},
                "pinnedProjects": {},
                "replaceProjectState": True,
                "projectStateMode": "replace",
            })
            state = load_ui_state(workspace)

        self.assertEqual([project["id"] for project in state["projects"]], ["p1"])
        self.assertEqual(state["sessionProjects"], {"s1": "p1"})
        self.assertEqual(state["pinnedProjects"], {"p1": True})

    def test_ui_state_explicit_empty_replace_can_clear_projects(self):
        from common.ecorex_workspace import load_ui_state, save_ui_state

        with tempfile.TemporaryDirectory() as workspace:
            save_ui_state(workspace, {
                "projects": [{"id": "p1", "name": "Project", "path": os.path.join(workspace, "project")}],
                "sessionProjects": {"s1": "p1"},
                "pinnedProjects": {"p1": True},
                "projectStateMode": "merge",
            })
            save_ui_state(workspace, {
                "projects": [],
                "sessionProjects": {},
                "pinnedProjects": {},
                "replaceProjectState": True,
                "projectStateMode": "replace",
                "allowEmptyProjectState": True,
            })
            state = load_ui_state(workspace)

        self.assertEqual(state["projects"], [])
        self.assertEqual(state["sessionProjects"], {})
        self.assertEqual(state["pinnedProjects"], {})

    def test_ui_state_merge_updates_existing_mapping_values(self):
        from common.ecorex_workspace import load_ui_state, save_ui_state

        with tempfile.TemporaryDirectory() as workspace:
            project_a = os.path.join(workspace, "project-a")
            project_b = os.path.join(workspace, "project-b")
            projects = [
                {"id": "p1", "name": "Project A", "path": project_a},
                {"id": "p2", "name": "Project B", "path": project_b},
            ]
            save_ui_state(workspace, {
                "projects": projects,
                "sessionProjects": {"s1": "p1", "s2": "p1"},
                "pinnedProjects": {"p1": True},
                "sessionTitles": {"s1": "Old title", "s2": "Keep title"},
                "pinnedSessions": {"s1": True, "s2": True},
                "projectStateMode": "merge",
            })
            save_ui_state(workspace, {
                "projects": projects,
                "sessionProjects": {"s1": "p2"},
                "pinnedProjects": {"p1": False, "p2": True},
                "sessionTitles": {"s1": "New title"},
                "pinnedSessions": {"s1": False},
                "projectStateMode": "merge",
            })
            state = load_ui_state(workspace)

        self.assertEqual(state["sessionProjects"], {"s1": "p2", "s2": "p1"})
        self.assertEqual(state["pinnedProjects"], {"p1": False, "p2": True})
        self.assertEqual(state["sessionTitles"], {"s1": "New title", "s2": "Keep title"})
        self.assertEqual(state["pinnedSessions"], {"s1": False, "s2": True})

    def test_ui_state_merge_preserves_session_project_bindings(self):
        from common.ecorex_workspace import load_ui_state, save_ui_state

        with tempfile.TemporaryDirectory() as workspace:
            project_a = os.path.join(workspace, "project-a")
            project_b = os.path.join(workspace, "project-b")
            projects = [
                {"id": "p1", "name": "Project A", "path": project_a},
                {"id": "p2", "name": "Project B", "path": project_b},
            ]
            save_ui_state(workspace, {
                "projects": projects,
                "sessionProjects": {"s1": "p1", "s2": "p1"},
                "sessionProjectBindings": {
                    "s1": {"projectId": "p1", "projectName": "Project A", "projectPath": project_a},
                    "s2": {"projectId": "p1", "projectName": "Project A", "projectPath": project_a},
                },
                "projectStateMode": "merge",
            })
            save_ui_state(workspace, {
                "projects": projects,
                "sessionProjects": {"s1": "p2"},
                "sessionProjectBindings": {
                    "s1": {"projectId": "p2", "projectName": "Project B", "projectPath": project_b, "source": "project-session-send"}
                },
                "projectStateMode": "merge",
            })
            state = load_ui_state(workspace)

        self.assertEqual(state["sessionProjects"], {"s1": "p2", "s2": "p1"})
        self.assertEqual(state["sessionProjectBindings"]["s1"]["projectId"], "p2")
        self.assertEqual(state["sessionProjectBindings"]["s1"]["projectPath"], project_b)
        self.assertEqual(state["sessionProjectBindings"]["s2"]["projectId"], "p1")

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

    def test_history_page_keeps_final_answer_out_of_steps(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-final-dedupe", [
                {"role": "user", "content": "make a note"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "draft outline"},
                        {"type": "text", "text": "# final note\n\nbody"},
                        {"type": "text", "text": "# final note\n\nbody"},
                    ],
                },
            ], channel_type="web")

            page = store.load_history_page("session-final-dedupe", page=1, page_size=20)

        assistant = page["messages"][1]
        self.assertEqual(assistant["content"], "# final note\n\nbody")
        self.assertEqual([step["content"] for step in assistant["steps"] if step["type"] == "content"], ["draft outline"])

    def test_history_page_exposes_request_turn_identity_from_assistant_extras(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages("session-request-identity", [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ], channel_type="web")
            store.attach_extras_to_assistant_seq("session-request-identity", 1, {
                "request_id": "req_123",
                "turn_id": "turn_123",
                "user_seq": 0,
                "bot_seq": 1,
            })

            page = store.load_history_page("session-request-identity", page=1, page_size=20)

        assistant = page["messages"][1]
        self.assertEqual(assistant["request_id"], "req_123")
        self.assertEqual(assistant["turn_id"], "turn_123")
        self.assertEqual(assistant["user_seq"], 0)
        self.assertEqual(assistant["bot_seq"], 1)

    def test_history_page_and_list_sessions_return_project_context(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as workspace:
            project_path = os.path.join(workspace, "project-a")
            store = ConversationStore(Path(workspace) / "conversation.sqlite3")
            store.append_messages(
                "session-project-context",
                [{"role": "user", "content": "项目第一条消息"}],
                channel_type="web",
                project_context={
                    "projectId": "p1",
                    "projectName": "Project A",
                    "projectPath": project_path,
                    "memoryPath": os.path.join(project_path, ".ecorex", "project-memory.md"),
                    "dreamsPath": os.path.join(project_path, ".ecorex", "dreams"),
                },
            )
            page = store.load_history_page("session-project-context", page=1, page_size=20)
            sessions = store.list_sessions(channel_type="web")["sessions"]

        self.assertEqual(page["project_context"]["projectId"], "p1")
        self.assertEqual(page["project_context"]["projectName"], "Project A")
        self.assertEqual(page["project_context"]["projectPath"], project_path)
        self.assertEqual(sessions[0]["projectId"], "p1")
        self.assertEqual(sessions[0]["projectPath"], project_path)

    def test_sessions_api_includes_requested_ids_outside_first_page(self):
        from agent.memory.conversation_store import ConversationStore
        from channel.web import web_channel

        cases = [
            {"include_ids": "session-old-pinned", "include_session_ids": "", "include_pinned": "", "pinned_ids": ""},
            {"include_ids": "", "include_session_ids": "session-old-pinned", "include_pinned": "", "pinned_ids": ""},
            {"include_ids": "", "include_session_ids": "", "include_pinned": "1", "pinned_ids": "session-old-pinned"},
        ]
        for query in cases:
            with self.subTest(query=query):
                with tempfile.TemporaryDirectory() as workspace:
                    store = ConversationStore(Path(workspace) / "conversation.sqlite3")
                    for session_id in ("session-old-pinned", "session-middle", "session-newest"):
                        store.append_messages(
                            session_id,
                            [{"role": "user", "content": session_id}],
                            channel_type="web",
                        )
                    with store._lock:
                        conn = store._connect()
                        try:
                            conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (100, "session-old-pinned"))
                            conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (200, "session-middle"))
                            conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (300, "session-newest"))
                            conn.commit()
                        finally:
                            conn.close()

                    with patch.object(web_channel, "_require_auth", return_value=None), \
                        patch("agent.memory.get_conversation_store", return_value=store), \
                        patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                            page="1",
                            page_size="1",
                            **query,
                        )):
                        payload = json.loads(web_channel.SessionsHandler().GET())

                ids = [item["session_id"] for item in payload["sessions"]]
                self.assertEqual(payload["status"], "success")
                self.assertEqual(ids[0], "session-newest")
                self.assertIn("session-old-pinned", ids)
                self.assertIn("session-old-pinned", payload["included_session_ids"])

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

    def test_v022_capability_policy_marks_runtime_capabilities_and_blocks_install(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace) / "runtime"
            root.mkdir()
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {
                    "mode": "ask",
                    "mirror": "https://user:secret@mirror.example/simple?token=sk-test",
                    "offlineCache": "C:/private/cache",
                    "updatedAt": "2026-06-25T01:02:03Z",
                },
                "capabilities": [
                    {
                        "id": "office-pdf",
                        "name": "Office PDF",
                        "mode": "disabled",
                        "status": "blocked-by-admin",
                        "updatedAt": "2026-06-25T01:02:04Z",
                    }
                ],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(optional_abilities, "RUNTIME_ROOT", root):
                listed = OptionalAbilities().execute({"action": "list", "ability": "office-pdf"})
                blocked = OptionalAbilities().execute({"action": "install", "ability": "office-pdf"})

        ability = listed.result["abilities"][0]
        serialized = json.dumps({"ability": ability, "blocked": blocked.result}, ensure_ascii=False)
        self.assertEqual(ability["policyMode"], "disabled")
        self.assertFalse(ability["installAllowed"])
        self.assertFalse(ability["agentCanInstall"])
        self.assertIn("Administrator disabled", ability["disabledReason"])
        self.assertEqual(ability["policyUpdatedAt"], "2026-06-25T01:02:04Z")
        self.assertEqual(ability["policySource"], "admin-cache")
        self.assertTrue(ability["mirrorConfigured"])
        self.assertTrue(ability["offlineCacheConfigured"])
        self.assertEqual(blocked.status, "error")
        self.assertEqual(blocked.result["errorType"], "capability_policy_blocked")
        self.assertEqual(blocked.result["policy"]["policyMode"], "disabled")
        self.assertNotIn("C:/private/cache", serialized)
        self.assertNotIn("user:secret", serialized)
        self.assertNotIn("sk-test", serialized)
        self.assertNotIn("mirror.example", serialized)

    def test_v022_capability_policy_does_not_disable_builtin_runtime_abilities(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace) / "runtime"
            root.mkdir()
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "disabled", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(optional_abilities, "RUNTIME_ROOT", root):
                listed = OptionalAbilities().execute({"action": "list", "ability": "find-skill"})

        ability = listed.result["abilities"][0]
        self.assertEqual(ability["id"], "find-skill")
        self.assertTrue(ability["enabled"])
        self.assertNotIn("policyMode", ability)
        self.assertNotIn("disabledReason", ability)

    def test_v022_capability_policy_blocks_disabled_builtin_install_before_state_write(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace) / "runtime"
            root.mkdir()
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "browser-automation", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(optional_abilities, "RUNTIME_ROOT", root), \
                    patch.object(optional_abilities, "_module_available", return_value=True):
                result = OptionalAbilities().execute({
                    "action": "install",
                    "ability": "browser-automation",
                    "timeout": 30,
                })

            state_file = root / "capability-state" / "browser-automation.json"

        self.assertEqual(result.status, "error")
        self.assertEqual(result.result["errorType"], "capability_policy_blocked")
        self.assertFalse(state_file.exists())

    def test_v022_capability_policy_redacts_sensitive_pack_ids_in_optional_blocked_payloads(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace) / "runtime"
            root.mkdir()
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "disabled", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(optional_abilities, "RUNTIME_ROOT", root):
                results = [
                    OptionalAbilities()._install_capability_pack("office-pdf-secret-token", 30),
                    OptionalAbilities()._install_capability_pack("office-pdf-ghp_abcd", 30),
                ]

        for result in results:
            serialized = json.dumps(result.result, ensure_ascii=False).lower()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.result["errorType"], "capability_policy_blocked")
            self.assertEqual(result.result["packId"], "redacted-capability-pack")
            self.assertTrue(result.result["packIdRedacted"])
            self.assertTrue(result.result["policy"]["packIdRedacted"])
            self.assertNotIn("office-pdf-secret-token", serialized)
            self.assertNotIn("office-pdf-ghp", serialized)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("token", serialized)
            self.assertNotIn("ghp", serialized)

    def test_v022_agent_capability_admin_disabled_policy_blocks_before_optional_install_and_writes_event(self):
        from agent.tools.agent_capability import agent_capability
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.protocol import get_run_event_ledger

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "office-pdf", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            tool = AgentCapabilityTool()
            tool.context = types.SimpleNamespace(_current_request_id="req-cap-policy", _current_session_id="session-cap-policy")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(agent_capability, "OptionalAbilities", side_effect=AssertionError("optional install should be blocked before execution")):
                result = tool.execute({"action": "install_pack", "pack_id": "office-pdf"})
            events = get_run_event_ledger().events_for_request("req-cap-policy", limit=0)
            from channel.web import web_channel

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                        request_id="req-cap-policy",
                        session_id="",
                        after_event_id="0",
                        limit="100",
                        include_events="1",
                        history_page="",
                        page_size="20",
                    )):
                projection_payload = json.loads(web_channel.RuntimeProjectionHandler().GET())

        self.assertEqual(result.status, "error")
        self.assertEqual(result.result["errorType"], "capability_policy_blocked")
        self.assertEqual([event["event_type"] for event in events], ["capability.policy_blocked"])
        self.assertEqual(events[0]["payload"]["pack_id"], "office-pdf")
        self.assertEqual(events[0]["payload"]["policy_mode"], "disabled")
        public_event = projection_payload["projection"]["events"][0]
        self.assertEqual(public_event["event_type"], "capability.policy_blocked")
        self.assertEqual(public_event["payload"]["pack_id"], "office-pdf")
        self.assertFalse(public_event["payload"]["install_allowed"])

    def test_v022_agent_capability_policy_block_skips_event_for_unsafe_request_id(self):
        from agent.tools.agent_capability import agent_capability
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.protocol import get_run_event_ledger

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "office-pdf", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            tool = AgentCapabilityTool()
            tool.context = types.SimpleNamespace(_current_request_id="req-secret\nbad", _current_session_id="session-secret\nbad")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(agent_capability, "OptionalAbilities", side_effect=AssertionError("optional install should be blocked before execution")):
                result = tool.execute({"action": "install_pack", "pack_id": "office-pdf"})
            events = get_run_event_ledger().list_events(limit=100)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.result["errorType"], "capability_policy_blocked")
        self.assertEqual(events, [])

    def test_v022_agent_capability_policy_block_redacts_sensitive_pack_id_from_event(self):
        from agent.tools.agent_capability import agent_capability
        from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
        from agent.protocol import get_run_event_ledger

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "disabled", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [],
            }), encoding="utf-8")
            tool = AgentCapabilityTool()
            tool.context = types.SimpleNamespace(_current_request_id="req-cap-redact", _current_session_id="session-cap-redact")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(agent_capability, "OptionalAbilities", side_effect=AssertionError("optional install should be blocked before execution")):
                result = tool.execute({"action": "install_pack", "pack_id": "office-pdf-ghp_abcd"})
            events = get_run_event_ledger().events_for_request("req-cap-redact", limit=0)
            from channel.web import web_channel

            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "input", return_value=types.SimpleNamespace(
                        request_id="req-cap-redact",
                        session_id="",
                        after_event_id="0",
                        limit="100",
                        include_events="1",
                        history_page="",
                        page_size="20",
                    )):
                projection_payload = json.loads(web_channel.RuntimeProjectionHandler().GET())

        serialized = json.dumps({
            "result": result.result,
            "events": events,
            "projection": projection_payload,
        }, ensure_ascii=False).lower()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.result["packId"], "redacted-capability-pack")
        self.assertTrue(result.result["packIdRedacted"])
        self.assertEqual(events[0]["payload"]["pack_id"], "redacted-capability-pack")
        self.assertTrue(events[0]["payload"]["pack_id_redacted"])
        public_event = projection_payload["projection"]["events"][0]
        self.assertEqual(public_event["payload"]["pack_id"], "redacted-capability-pack")
        self.assertTrue(public_event["payload"]["pack_id_redacted"])
        self.assertNotIn("office-pdf-secret-token", serialized)
        self.assertNotIn("office-pdf-ghp", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("ghp", serialized)

    def test_v022_web_capabilities_api_flattens_policy_capability_packs_for_frontend_contract(self):
        from agent.tools.agent_capability import agent_capability
        from agent.tools.base_tool import ToolResult
        from channel.web import web_channel

        nested_payload = {
            "status": "success",
            "abilities": {
                "status": "success",
                "abilities": [
                    {
                        "id": "office-pdf",
                        "packId": "office-pdf",
                        "kind": "capability-pack",
                        "label": "Office PDF",
                        "agentCanInstall": False,
                        "policyMode": "disabled",
                        "installAllowed": False,
                        "disabledReason": "Administrator disabled self-service installation for Office PDF.",
                        "policySource": "admin-cache",
                    }
                ],
            },
            "skills": [],
            "mcpStatus": {},
        }
        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(agent_capability.AgentCapabilityTool, "execute", return_value=ToolResult.success(nested_payload)):
            payload = json.loads(web_channel.CapabilitiesHandler().GET())

        self.assertEqual(payload["status"], "success")
        self.assertIsInstance(payload["abilities"], list)
        self.assertEqual(payload["abilities"][0]["packId"], "office-pdf")
        self.assertEqual(payload["abilities"][0]["policyMode"], "disabled")
        self.assertFalse(payload["abilities"][0]["installAllowed"])
        self.assertIsInstance(payload["abilityDiagnostics"], dict)
        self.assertNotIn("abilities", payload["abilityDiagnostics"])

    def test_v022_web_agent_install_request_blocks_admin_disabled_capability_policy(self):
        from agent.protocol import get_run_event_ledger
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "office-pdf", "name": "Office PDF", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            body = {
                "packId": "office-pdf",
                "packName": "Office PDF",
                "sessionId": "session-cap-web",
                "requestId": "req-cap-web",
            }
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(body).encode("utf-8")):
                payload = json.loads(web_channel.AgentInstallRequestHandler().POST())
            events = get_run_event_ledger().events_for_request("req-cap-web", limit=0)

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errorType"], "capability_policy_blocked")
        self.assertNotIn("agent_capability", serialized)
        self.assertEqual([event["event_type"] for event in events], ["capability.policy_blocked"])
        self.assertEqual(events[0]["source"], "web_channel")

    def test_v022_web_agent_install_request_blocks_feishu_alias_before_prompt(self):
        from agent.protocol import get_run_event_ledger
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "feishu-lark", "name": "Feishu/Lark", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            body = {
                "packId": "lark-cli",
                "packName": "Lark CLI",
                "sessionId": "session-cap-web-alias",
                "requestId": "req-cap-web-alias",
            }
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(body).encode("utf-8")):
                payload = json.loads(web_channel.AgentInstallRequestHandler().POST())
            events = get_run_event_ledger().events_for_request("req-cap-web-alias", limit=0)

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["errorType"], "capability_policy_blocked")
        self.assertEqual(payload["packId"], "feishu-lark")
        self.assertEqual(payload["requestedPackId"], "lark-cli")
        self.assertNotIn("agent_capability", serialized)
        self.assertEqual([event["event_type"] for event in events], ["capability.policy_blocked"])
        self.assertEqual(events[0]["payload"]["pack_id"], "feishu-lark")

    def test_v022_web_agent_install_request_redacts_sensitive_pack_id_before_event(self):
        from agent.protocol import get_run_event_ledger
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace, isolated_run_ledger():
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "disabled", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [],
            }), encoding="utf-8")
            body = {
                "packId": "office-pdf-secret-token",
                "packName": "secret token connector",
                "sessionId": "session-cap-web-redact",
                "requestId": "req-cap-web-redact",
            }
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel.web, "data", return_value=json.dumps(body).encode("utf-8")):
                payload = json.loads(web_channel.AgentInstallRequestHandler().POST())
            events = get_run_event_ledger().events_for_request("req-cap-web-redact", limit=0)

        serialized = json.dumps({"payload": payload, "events": events}, ensure_ascii=False).lower()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["packId"], "redacted-capability-pack")
        self.assertEqual(payload["packName"], "Capability pack")
        self.assertTrue(payload["packIdRedacted"])
        self.assertNotIn("requestedPackId", payload)
        self.assertEqual(events[0]["payload"]["pack_id"], "redacted-capability-pack")
        self.assertTrue(events[0]["payload"]["pack_id_redacted"])
        self.assertNotIn("office-pdf-secret-token", serialized)
        self.assertNotIn("secret token connector", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)

    def test_v022_extension_registry_projects_admin_capability_policy(self):
        from agent.extensions import ExtensionRegistry
        from agent.tools.optional_abilities import optional_abilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace) / "runtime"
            root.mkdir()
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {"mode": "ask", "updatedAt": "2026-06-25T01:02:03Z"},
                "capabilities": [{"id": "office-pdf", "name": "Office PDF", "mode": "disabled", "updatedAt": "2026-06-25T01:02:04Z"}],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}, clear=False), \
                    patch.object(optional_abilities, "RUNTIME_ROOT", root):
                entries = ExtensionRegistry(str(root))._optional_abilities()

        by_id = {entry["id"]: entry for entry in entries}
        office = by_id["ability:office-pdf"]
        self.assertEqual(office["policyMode"], "disabled")
        self.assertFalse(office["installAllowed"])
        self.assertEqual(office["permissions"], [])
        self.assertEqual(office["status"], "disabled")
        self.assertIn("Administrator disabled", office["disabledReason"])

    def test_v023_extension_registry_exposes_first_party_tools_after_cold_start(self):
        from agent.extensions import ExtensionRegistry
        from agent.tools.tool_manager import ToolManager

        manager = ToolManager()
        old_classes = dict(getattr(manager, "tool_classes", {}) or {})
        old_configs = getattr(manager, "tool_configs", None)
        try:
            manager.tool_classes = {}

            payload = ExtensionRegistry("C:\\workspace").list_extensions()
        finally:
            manager.tool_classes = old_classes
            if old_configs is not None:
                manager.tool_configs = old_configs

        by_id = {entry["id"]: entry for entry in payload["extensions"]}
        for tool_name in ("bash", "read", "write", "edit", "ls", "find", "host_diagnostics", "feishu_cli"):
            with self.subTest(tool_name=tool_name):
                row = by_id[f"tool:{tool_name}"]
                self.assertEqual(row["type"], "builtin_tool")
                self.assertEqual(row["status"], "ready")
                self.assertTrue(row["enabled"])
                self.assertTrue(row["installed"])
                self.assertTrue(row["toolSchemaCallable"])

    def test_v023_channel_tool_snapshot_self_loads_builtin_feishu_cli(self):
        from agent.tools.tool_manager import ToolManager
        from channel.web.web_channel import ChannelsHandler

        manager = ToolManager()
        old_classes = dict(getattr(manager, "tool_classes", {}) or {})
        old_configs = getattr(manager, "tool_configs", None)
        try:
            manager.tool_classes = {}

            names = ChannelsHandler._agent_tool_names()
        finally:
            manager.tool_classes = old_classes
            if old_configs is not None:
                manager.tool_configs = old_configs

        self.assertIsInstance(names, set)
        self.assertIn("bash", names)
        self.assertIn("feishu_cli", names)

    def test_v022_capability_policy_runtime_source_contracts(self):
        root = Path(__file__).resolve().parents[1]
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        optional_source = (root / "agent" / "tools" / "optional_abilities" / "optional_abilities.py").read_text(encoding="utf-8")
        agent_capability_source = (root / "agent" / "tools" / "agent_capability" / "agent_capability.py").read_text(encoding="utf-8")
        registry_source = (root / "agent" / "extensions" / "registry.py").read_text(encoding="utf-8")
        policy_source = (root / "common" / "ecorex_capability_policy.py").read_text(encoding="utf-8")

        self.assertIn("policyMode: item.policyMode || state.policyMode || \"ask\"", web_source)
        self.assertIn("Array.isArray(abilityPayload.abilities)", web_source)
        self.assertIn("installAllowed: item.installAllowed !== false", web_source)
        self.assertIn("_flatten_capability_payload(_tool_result_to_payload(result))", web_source)
        self.assertIn("normalize_capability_pack_id(pack_id)", web_source)
        self.assertIn("blocked_install_payload(policy_lookup_id, pack_name=pack_name, action=\"agent_install_request\")", web_source)
        self.assertIn("packIdRedacted", web_source)
        self.assertIn("capability.policy_blocked", web_source)
        self.assertIn("policyMode: item.policyMode === \"disabled\"", api_source)
        self.assertIn("Array.isArray((abilitiesPayload as Record<string, unknown>).abilities)", api_source)
        self.assertIn("installAllowed: item.installAllowed !== false", api_source)
        self.assertIn("apply_policy_to_capability(item)", optional_source)
        self.assertIn("blocked_install_payload(pack_id, action=\"install\")", optional_source)
        self.assertIn("blocked_install_payload(policy_pack_id, action=\"install_pack\")", agent_capability_source)
        self.assertIn("capability.policy_blocked", agent_capability_source)
        self.assertIn("\"policyMode\": policy_mode", registry_source)
        self.assertIn("\"installAllowed\": bool(install_allowed)", registry_source)
        self.assertIn("ECOREX_CAPABILITY_POLICY_FILE", policy_source)
        self.assertIn("normalize_capability_pack_id", policy_source)
        self.assertIn("redacted-capability-pack", policy_source)
        self.assertIn("_looks_sensitive", policy_source)
        self.assertIn("packIdRedacted", policy_source)
        self.assertIn("offlineCacheConfigured", policy_source)
        self.assertIn("mirrorConfigured", policy_source)
        self.assertNotIn("offlineCache\": _safe_text", policy_source)

    def test_browser_automation_reports_built_in_when_playwright_exists(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            with patch.object(optional_abilities, "RUNTIME_ROOT", root), \
                    patch.object(optional_abilities, "_module_available", return_value=True):
                result = OptionalAbilities().execute({"action": "list", "ability": "browser-automation"})

        self.assertEqual(result.status, "success")
        abilities = result.result["abilities"]
        self.assertEqual(len(abilities), 1)
        state = abilities[0]["capabilityState"]
        self.assertTrue(state["installed"])
        self.assertTrue(state["builtIn"])

    def test_browser_automation_install_short_circuits_built_in_runtime(self):
        from agent.tools.optional_abilities import optional_abilities
        from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            with patch.object(optional_abilities, "RUNTIME_ROOT", root), \
                    patch.object(optional_abilities, "_module_available", return_value=True):
                result = OptionalAbilities().execute({
                    "action": "install",
                    "ability": "browser-automation",
                    "timeout": 30,
                })

            state_file = root / "capability-state" / "browser-automation.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success")
        self.assertTrue(result.result["builtIn"])
        self.assertTrue(state["installed"])
        self.assertTrue(state["builtIn"])
        self.assertNotEqual(result.result.get("message"), "capability installer not found")

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
        raw_active_session = "session-current-private"
        raw_active_request = "request-current-private"
        raw_line = f"2026-06-20 ERROR secret prompt text from {raw_workspace}"

        class FakeChannel:
            def active_requests_snapshot(self):
                return {
                    "requests": [{
                        "request_id": raw_active_request,
                        "session_id": raw_active_session,
                        "cancelled": False,
                        "created_at": "2026-06-25T08:00:00Z",
                        "stream_available": True,
                    }],
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
        for secret in (
            raw_workspace,
            raw_runtime,
            raw_log,
            raw_lock,
            raw_line,
            "secret prompt text",
            "session-private",
            "session-current",
            "request-current",
            raw_active_session,
            raw_active_request,
        ):
            self.assertNotIn(secret, rendered)
        self.assertTrue(payload["runtime"]["workspaceRoot"]["redacted"])
        self.assertTrue(payload["runtime"]["runtimeRoot"]["redacted"])
        self.assertTrue(payload["logs"]["path"]["redacted"])
        self.assertTrue(payload["staleLocks"][0]["redacted"])
        self.assertTrue(payload["logs"]["recentEvents"][0]["redacted"])
        self.assertTrue(payload["current"]["redacted"])
        self.assertTrue(payload["current"]["sessionHash"])
        self.assertTrue(payload["current"]["requestHash"])
        self.assertTrue(payload["activeRequests"][0]["redacted"])
        self.assertTrue(payload["activeRequests"][0]["sessionHash"])
        self.assertTrue(payload["activeRequests"][0]["requestHash"])
        self.assertFalse(payload["privacy"]["includesRawRuntimePayloads"])
        self.assertFalse(payload["privacy"]["includesRawCapabilityPolicyPaths"])

    def test_v022_diagnostic_bundle_summarizes_runtime_events_without_raw_payloads(self):
        import config
        from agent.protocol import get_run_event_ledger
        from channel.web import web_channel

        class FakeChannel:
            def active_requests_snapshot(self):
                return {"requests": [], "stale_locks": []}

        with isolated_run_ledger():
            ledger = get_run_event_ledger()
            raw_private_status = r"C:\Users\private-user\workspace\private prompt status"
            raw_private_event_type = r"C:\Users\private-user\workspace\hyper sensitive user journal event"
            ledger.append_event(
                request_id="req-secret-diagnostic",
                session_id="session-secret-diagnostic",
                event_type="assistant.delta",
                payload={
                    "text": "do not leak prompt text",
                    "api_key": "sk-diagnostic-secret",
                },
                idempotency_key="diag:assistant-delta",
                source=r"C:\Users\private-user\source.py",
            )
            ledger.append_event(
                request_id="req-secret-diagnostic",
                session_id="session-secret-diagnostic",
                event_type="capability.policy_blocked",
                payload={
                    "action": "install",
                    "error_type": "capability_policy_blocked",
                    "pack_id": "office-pdf-ghp_abcd",
                    "pack_id_redacted": True,
                    "policy_mode": "disabled",
                    "policy_source": "admin-cache",
                },
                idempotency_key="diag:capability-policy-blocked",
            )
            ledger.append_event(
                request_id="req-secret-diagnostic",
                session_id="session-secret-diagnostic",
                event_type="run.failed",
                payload={
                    "status": raw_private_status,
                    "error_type": r"C:\Users\private-user\workspace\private prompt error",
                },
                idempotency_key="diag:run-failed",
            )
            ledger.append_event(
                request_id="req-secret-diagnostic",
                session_id="session-secret-diagnostic",
                event_type=raw_private_event_type,
                payload={"status": "completed"},
                idempotency_key="diag:private-event-type",
            )

            with patch.object(web_channel, "_log_snapshot_payload", return_value={"log": {"exists": False, "lines": []}}):
                with patch.object(web_channel, "_get_workspace_root", return_value=r"C:\private\workspace"):
                    with patch.object(config, "get_root", return_value=Path(r"C:\private\runtime")):
                        with patch.object(web_channel, "WebChannel", return_value=FakeChannel()):
                            payload = web_channel._diagnostic_bundle_payload()

        rendered = json.dumps(payload, ensure_ascii=False)
        for raw in (
            "req-secret-diagnostic",
            "session-secret-diagnostic",
            "do not leak prompt text",
            "sk-diagnostic-secret",
            "office-pdf-ghp_abcd",
            r"C:\Users\private-user\source.py",
            raw_private_status,
            raw_private_event_type,
            "hyper sensitive user journal event",
            "hyper_sensitive_user_journal_event",
            r"C:\Users\private-user\workspace\private prompt error",
            r"C:\private\workspace",
            r"C:\private\runtime",
        ):
            self.assertNotIn(raw, rendered)
        runtime = payload["runtimeEvents"]
        self.assertEqual(runtime["status"], "success")
        self.assertEqual(runtime["source"], "runtime-event-ledger")
        self.assertEqual(runtime["capabilityPolicyBlockedCount"], 1)
        self.assertEqual(runtime["eventTypeCounts"]["capability.policy_blocked"], 1)
        self.assertEqual(runtime["eventTypeCounts"]["unknown"], 1)
        unknown = [item for item in runtime["recent"] if item["eventType"] == "unknown"][0]
        self.assertTrue(unknown["eventTypeHash"])
        self.assertTrue(unknown["eventTypeRedacted"])
        blocked = [item for item in runtime["recent"] if item["eventType"] == "capability.policy_blocked"][0]
        self.assertTrue(blocked["redacted"])
        self.assertTrue(blocked["requestHash"])
        self.assertTrue(blocked["sessionHash"])
        self.assertEqual(blocked["payload"]["errorType"], "capability_policy_blocked")
        self.assertEqual(blocked["payload"]["policyMode"], "disabled")
        self.assertTrue(blocked["payload"]["packIdRedacted"])
        self.assertTrue(blocked["payload"]["packHash"])
        failed = [item for item in runtime["recent"] if item["eventType"] == "run.failed"][0]
        self.assertTrue(failed["payload"]["statusHash"])
        self.assertTrue(failed["payload"]["errorTypeHash"])
        self.assertNotIn("status", failed["payload"])
        self.assertNotIn("errorType", failed["payload"])

    def test_v022_diagnostic_bundle_capability_policy_summary_omits_paths_and_tokens(self):
        import config
        from channel.web import web_channel

        class FakeChannel:
            def active_requests_snapshot(self):
                return {"requests": [], "stale_locks": []}

        with tempfile.TemporaryDirectory() as workspace:
            raw_mirror = "https://user:secret-token@example.com/simple"
            raw_cache = str(Path(workspace) / "offline-cache-secret-token")
            raw_updated_at = str(Path(workspace) / "private prompt updated")
            raw_policy_status = str(Path(workspace) / "private prompt status")
            policy_path = Path(workspace) / "capability-policy.json"
            policy_path.write_text(json.dumps({
                "policy": {
                    "mode": "disabled",
                    "mirror": raw_mirror,
                    "offlineCache": raw_cache,
                    "updatedAt": raw_updated_at,
                },
                "capabilities": [
                    {
                        "id": "office-pdf",
                        "name": "Office PDF",
                        "mode": "disabled",
                        "status": raw_policy_status,
                        "updatedAt": "2026-06-25T08:00:00Z",
                    },
                    {
                        "id": "office-pdf-ghp_abcd",
                        "name": "token shaped id must not appear",
                        "mode": "disabled",
                        "status": "secret-token-status",
                    },
                ],
            }), encoding="utf-8")

            with patch.dict(os.environ, {"ECOREX_CAPABILITY_POLICY_FILE": str(policy_path)}):
                with patch.object(web_channel, "_log_snapshot_payload", return_value={"log": {"exists": False, "lines": []}}):
                    with patch.object(web_channel, "_get_workspace_root", return_value=str(Path(workspace) / "workspace")):
                        with patch.object(config, "get_root", return_value=Path(workspace) / "runtime"):
                            with patch.object(web_channel, "WebChannel", return_value=FakeChannel()):
                                payload = web_channel._diagnostic_bundle_payload()

        rendered = json.dumps(payload, ensure_ascii=False)
        for raw in (
            raw_mirror,
            raw_cache,
            raw_updated_at,
            raw_policy_status,
            str(policy_path),
            "office-pdf-ghp_abcd",
            "secret-token-status",
            "token shaped id must not appear",
        ):
            self.assertNotIn(raw, rendered)
        policy = payload["capabilityPolicy"]
        self.assertEqual(policy["status"], "success")
        self.assertEqual(policy["source"], "admin-cache")
        self.assertTrue(policy["policyAvailable"])
        self.assertEqual(policy["globalMode"], "disabled")
        self.assertTrue(policy["mirrorConfigured"])
        self.assertTrue(policy["offlineCacheConfigured"])
        self.assertEqual(policy["capabilityCount"], 1)
        self.assertEqual(policy["disabledPackCount"], 1)
        self.assertTrue(policy["disabledPacks"][0]["packHash"])
        self.assertTrue(policy["disabledPacks"][0]["policyStatusHash"])
        self.assertTrue(policy["disabledPacks"][0]["redacted"])
        self.assertTrue(policy["policyUpdatedAtHash"])
        self.assertNotIn("policyUpdatedAt", policy)

    def test_v022_diagnostic_bundle_error_branches_hash_private_messages(self):
        from channel.web import web_channel

        raw_runtime_error = r"C:\Users\private-user\workspace\hyper sensitive runtime failure"
        raw_policy_error = r"C:\Users\private-user\workspace\hyper sensitive policy failure"

        with patch("agent.protocol.get_run_event_ledger", side_effect=RuntimeError(raw_runtime_error)):
            runtime = web_channel._diagnostic_runtime_events_payload()
        with patch("common.ecorex_capability_policy.load_capability_policy", side_effect=RuntimeError(raw_policy_error)):
            policy = web_channel._diagnostic_capability_policy_payload()

        rendered = json.dumps({"runtime": runtime, "policy": policy}, ensure_ascii=False)
        for raw in (
            raw_runtime_error,
            raw_policy_error,
            "hyper sensitive runtime failure",
            "hyper_sensitive_runtime_failure",
            "hyper sensitive policy failure",
            "hyper_sensitive_policy_failure",
        ):
            self.assertNotIn(raw, rendered)
        self.assertEqual(runtime["status"], "error")
        self.assertEqual(runtime["message"], "runtime event summary unavailable")
        self.assertTrue(runtime["messageHash"])
        self.assertTrue(runtime["redacted"])
        self.assertEqual(policy["status"], "error")
        self.assertEqual(policy["message"], "capability policy summary unavailable")
        self.assertTrue(policy["messageHash"])
        self.assertTrue(policy["redacted"])

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

    def test_v020_channel_catalog_matches_factory_and_normalizes_aliases(self):
        from channel.channel_catalog import CHANNEL_CATALOG, normalize_channel_name, parse_channel_list

        expected = {
            "weixin", "feishu", "dingtalk", "wecom_bot", "qq", "wechatcom_app",
            "wechat_kf", "wechatmp", "wechatmp_service", "telegram", "slack", "discord",
        }
        self.assertTrue(expected.issubset(set(CHANNEL_CATALOG.keys())))
        self.assertEqual(normalize_channel_name("wx"), "weixin")
        self.assertEqual(normalize_channel_name("lark"), "feishu")
        self.assertEqual(normalize_channel_name("wecom"), "wecom_bot")
        self.assertEqual(normalize_channel_name("wecom_app"), "wechatcom_app")
        self.assertEqual(parse_channel_list("web, feishu,wx,lark,wechatmp_service"), [
            "web", "feishu", "weixin", "wechatmp_service"
        ])

    def test_v020_extension_registry_discovers_configured_channels_without_secrets(self):
        from agent.extensions import ExtensionRegistry

        fake_conf = {
            "channel_type": "web,feishu,wechatmp_service",
            "feishu_app_id": "cli_aabbcc",
            "feishu_app_secret": "super-secret-value",
            "wechatmp_app_id": "wxid",
            "wechatmp_app_secret": "wechat-secret-value",
            "wechatmp_token": "wechat-token-value",
            "wechatmp_aes_key": "wechat-aes-value",
        }
        with patch("config.conf", return_value=fake_conf):
            channels = ExtensionRegistry("C:\\workspace")._channels()

        by_id = {entry["id"]: entry for entry in channels}
        self.assertEqual(by_id["channel:feishu"]["status"], "active")
        self.assertEqual(by_id["channel:wechatmp_service"]["status"], "active")
        self.assertEqual(by_id["channel:telegram"]["status"], "available")
        rendered = json.dumps(channels, ensure_ascii=False)
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("wechat-secret-value", rendered)

    def test_v020_channels_handler_uses_shared_catalog_and_masks_secrets(self):
        from channel.web import web_channel

        fake_conf = {
            "channel_type": "lark",
            "feishu_app_id": "cli_aabbcc",
            "feishu_app_secret": "1234567890abcdef",
        }
        with patch.object(web_channel, "_require_auth", return_value=None):
            with patch.object(web_channel, "conf", return_value=fake_conf):
                payload = json.loads(web_channel.ChannelsHandler().GET())

        channels = {item["name"]: item for item in payload["channels"]}
        self.assertIn("wechatmp_service", channels)
        self.assertTrue(channels["feishu"]["active"])
        self.assertTrue(channels["feishu"]["configured"])
        secret_field = next(field for field in channels["feishu"]["fields"] if field["key"] == "feishu_app_secret")
        self.assertEqual(secret_field["value"], "1234********cdef")

    def test_v022_channel_observability_separates_transport_auth_and_agent_surface(self):
        from channel.channel_catalog import channel_observability

        fake_conf = {
            "channel_type": "web,lark",
            "feishu_app_id": "cli_aabbcc",
            "feishu_app_secret": "super-secret-value",
            "slack_bot_token": "xoxb-secret",
            "slack_app_token": "xapp-secret",
        }

        feishu = channel_observability(
            fake_conf,
            "lark",
            running_channels={"feishu"},
            tool_names={"feishu_cli"},
        )
        self.assertTrue(feishu["active"])
        self.assertTrue(feishu["running"])
        self.assertEqual(feishu["configState"], "configured")
        self.assertEqual(feishu["auth"]["mode"], "bot_app_credentials")
        self.assertEqual(feishu["auth"]["authEndpoint"], "/api/feishu/register")
        self.assertEqual(feishu["auth"]["agentAuthorizationAction"]["action"], "auth_login")
        self.assertEqual(feishu["agentSurface"]["tool"], "feishu_cli")
        self.assertTrue(feishu["agentSurface"]["schemaVisible"])
        self.assertTrue(feishu["agentSurface"]["toolSchemaCallable"])
        self.assertFalse(feishu["agentSurface"]["callable"])
        self.assertEqual(feishu["agentSurface"]["status"], "schema_visible_unverified")
        self.assertEqual(feishu["agentSurface"]["readiness"], "unverified")
        self.assertTrue(feishu["agentSurface"]["requiresStatusProbe"])
        self.assertTrue(feishu["agentSurface"]["permissionGated"])

        missing_cli = channel_observability(fake_conf, "feishu", tool_names=set())
        self.assertEqual(missing_cli["agentSurface"]["status"], "tool_not_loaded")
        self.assertFalse(missing_cli["agentSurface"]["callable"])

        slack = channel_observability(fake_conf, "slack", tool_names={"feishu_cli"})
        self.assertEqual(slack["configState"], "configured")
        self.assertTrue(slack["auth"]["channelAuthSupported"])
        self.assertFalse(slack["auth"]["agentAuthSupported"])
        self.assertEqual(slack["agentSurface"]["status"], "not_applicable")
        self.assertFalse(slack["agentSurface"]["callable"])

    def test_v022_channels_handler_reports_auth_and_agent_schema_without_cli_probe(self):
        from agent.tools.feishu_cli.feishu_cli import FeishuCli
        from channel.web import web_channel

        class FakeToolManager:
            tool_classes = {"feishu_cli": object}

            def load_tools(self):
                raise AssertionError("GET /api/channels must not load tools or start MCP")

            def list_tools(self):
                raise AssertionError("GET /api/channels should read the existing registry snapshot")

        fake_conf = {
            "channel_type": "web,lark",
            "feishu_app_id": "cli_aabbcc",
            "feishu_app_secret": "short",
        }
        fake_manager = FakeToolManager()
        fake_manager.tool_classes = {"feishu_cli": object}
        with patch.object(web_channel, "_require_auth", return_value=None), \
                patch.object(web_channel, "conf", return_value=fake_conf), \
                patch("agent.tools.tool_manager.ToolManager", return_value=fake_manager), \
                patch.object(FeishuCli, "execute", side_effect=AssertionError("GET /api/channels must not run lark-cli")) as execute:
            payload = json.loads(web_channel.ChannelsHandler().GET())

        execute.assert_not_called()
        channels = {item["name"]: item for item in payload["channels"]}
        feishu = channels["feishu"]
        self.assertEqual(feishu["auth"]["authEndpoint"], "/api/feishu/register")
        self.assertEqual(feishu["auth"]["channelConfigState"], "configured")
        self.assertEqual(feishu["agentSurface"]["tool"], "feishu_cli")
        self.assertTrue(feishu["agentSurface"]["schemaVisible"])
        self.assertTrue(feishu["agentSurface"]["toolSchemaCallable"])
        self.assertFalse(feishu["agentSurface"]["callable"])
        self.assertEqual(feishu["agentSurface"]["readiness"], "unverified")
        secret_field = next(field for field in feishu["fields"] if field["key"] == "feishu_app_secret")
        self.assertEqual(secret_field["value"], "*****")
        self.assertFalse(channels["slack"]["agentSurface"]["callable"])

    def test_v022_channels_handler_does_not_treat_failed_transport_object_as_running(self):
        from channel.web import web_channel

        class FakeToolManager:
            tool_classes = {"feishu_cli": object}
            _mcp_tool_instances = {}

        class FakeThread:
            def is_alive(self):
                return False

        class FakeChannel:
            def __init__(self):
                self._startup_error = "missing Feishu credentials"
                self._startup_event = threading.Event()
                self._startup_event.set()

        class FakeManager:
            _threads = {"feishu": FakeThread()}

            def get_channel(self, name):
                return FakeChannel() if name == "feishu" else None

        fake_conf = {
            "channel_type": "web,feishu",
            "feishu_app_id": "",
            "feishu_app_secret": "",
        }
        main_module = sys.modules.get("__main__")
        previous_manager = getattr(main_module, "_channel_mgr", None)
        had_manager = hasattr(main_module, "_channel_mgr")
        try:
            setattr(main_module, "_channel_mgr", FakeManager())
            with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel, "conf", return_value=fake_conf), \
                    patch("agent.tools.tool_manager.ToolManager", return_value=FakeToolManager()):
                payload = json.loads(web_channel.ChannelsHandler().GET())
        finally:
            if had_manager:
                setattr(main_module, "_channel_mgr", previous_manager)
            else:
                try:
                    delattr(main_module, "_channel_mgr")
                except AttributeError:
                    pass

        channels = {item["name"]: item for item in payload["channels"]}
        feishu = channels["feishu"]
        self.assertTrue(feishu["active"])
        self.assertFalse(feishu["running"])
        self.assertEqual(feishu["status"], "error")
        self.assertEqual(feishu["last_error"], "missing Feishu credentials")

    def test_v022_extension_registry_reuses_channel_observability_contract(self):
        from agent.extensions import ExtensionRegistry

        class FakeToolManager:
            tool_classes = {"feishu_cli": object}
            _mcp_tool_instances = {}

            def load_tools(self):
                raise AssertionError("extension registry status read must not load tools or start MCP")

            def list_tools(self):
                raise AssertionError("extension registry should read the existing registry snapshot")

        fake_conf = {
            "channel_type": "web,feishu,slack",
            "feishu_app_id": "cli_aabbcc",
            "feishu_app_secret": "super-secret-value",
            "slack_bot_token": "xoxb-secret",
            "slack_app_token": "xapp-secret",
        }
        with patch("config.conf", return_value=fake_conf), \
                patch("agent.tools.tool_manager.ToolManager", FakeToolManager):
            channels = ExtensionRegistry("C:\\workspace")._channels()

        by_id = {entry["id"]: entry for entry in channels}
        self.assertEqual(by_id["channel:feishu"]["status"], "active")
        self.assertTrue(by_id["channel:feishu"]["active"])
        self.assertTrue(by_id["channel:feishu"]["configured"])
        self.assertFalse(by_id["channel:feishu"]["running"])
        self.assertEqual(by_id["channel:feishu"]["auth"]["channelConfigState"], "configured")
        self.assertTrue(by_id["channel:feishu"]["agentSurface"]["toolSchemaCallable"])
        self.assertFalse(by_id["channel:feishu"]["agentSurface"]["callable"])
        self.assertEqual(by_id["channel:slack"]["auth"]["channelConfigState"], "configured")
        self.assertTrue(by_id["channel:slack"]["configured"])
        self.assertEqual(by_id["channel:slack"]["agentSurface"]["status"], "not_applicable")
        rendered = json.dumps(channels, ensure_ascii=False)
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("xoxb-secret", rendered)

    def test_v022_web_channels_observability_ui_contract(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        console_css = (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")
        smoke_source = (root / "scripts" / "smoke-web-channels-observability-browser.py").read_text(encoding="utf-8")

        self.assertIn("function buildChannelObservabilityHtml", console_source)
        self.assertIn("function channelTransportSummary", console_source)
        self.assertIn("function channelSafeColor", console_source)
        self.assertIn("function channelSafeIcon", console_source)
        self.assertIn("function ensureChannelActionDelegation", console_source)
        self.assertIn("function channelInputsFor", console_source)
        self.assertIn("ch.running === true", console_source)
        self.assertIn("channels_enabled_not_running", console_source)
        self.assertIn("data-channel-state-row=\"transport\"", console_source)
        self.assertIn("data-channel-state-row=\"auth\"", console_source)
        self.assertIn("data-channel-state-row=\"agent\"", console_source)
        self.assertIn("data-agent-callable", console_source)
        self.assertIn("agent.callable === true", console_source)
        self.assertIn("agent.schemaVisible === true", console_source)
        self.assertIn("agent.requiresStatusProbe", console_source)
        self.assertIn("agent.permissionGated", console_source)
        self.assertIn("agent.callableReason", console_source)
        self.assertIn("data-channel-action=\"connect\"", console_source)
        self.assertIn("data-channel-action=\"save\"", console_source)
        self.assertIn("data-channel-action=\"disconnect\"", console_source)
        self.assertIn("event.target.closest('[data-channel-action][data-channel-name]')", console_source)
        self.assertIn("CHANNEL_COLOR_TOKENS", console_source)
        self.assertIn("ChannelsHandler_maskSecret(String(f.value))", console_source)
        self.assertIn("channelInputsFor(card, chName)", console_source)
        self.assertNotIn("${t('channels_connected')}", console_source)
        self.assertNotIn("onclick=\"connectChannelConfig('${ch.name}')", console_source)
        self.assertNotIn("onclick=\"saveChannelConfig('${ch.name}')", console_source)
        self.assertNotIn("onclick=\"disconnectChannel('${ch.name}')", console_source)
        self.assertNotIn("document.getElementById(`channel-card-${chName}`)", console_source)
        self.assertNotIn("querySelectorAll('input[data-ch=\"' + chName", console_source)

        self.assertIn(".channel-observability-panel", console_css)
        self.assertIn(".channel-state-badge.is-danger", console_css)
        self.assertIn(".channel-state-badge.is-warn", console_css)
        self.assertIn("@media (max-width: 640px)", console_css)

        self.assertIn("base_api_stub_script", smoke_source)
        self.assertIn("schema_visible_unverified", smoke_source)
        self.assertIn("callable: false", smoke_source)
        self.assertIn("assert(feishuText.includes('Enabled, not running')", smoke_source)
        self.assertIn("assert(!feishuText.includes('Connected')", smoke_source)
        self.assertIn("assert(feishuAgent.dataset.agentCallable === 'false'", smoke_source)
        self.assertIn("super-secret-value", smoke_source)
        self.assertIn("xoxb-secret-value", smoke_source)
        self.assertIn("xapp-star*raw-secret-value", smoke_source)
        self.assertIn("evil-raw-secret-value", smoke_source)
        self.assertIn("inputValues", smoke_source)
        self.assertIn("attrText", smoke_source)
        self.assertIn("connectChannelConfig inline handler remains", smoke_source)
        self.assertIn("hostile channel metadata executed", smoke_source)

    def test_v020_tools_handler_uses_tool_manager_list_tools(self):
        from channel.web import web_channel

        class FakeToolManager:
            tool_classes = {"bash": object}

            def load_tools(self):
                raise AssertionError("load_tools should not be needed when tool_classes is populated")

            def list_tools(self):
                return {
                    "bash": {"description": "Shell command", "parameters": {"type": "object"}},
                    "mcp__demo__search": {"description": "MCP search", "parameters": {"properties": {"q": {}}}},
                }

        with patch.object(web_channel, "_require_auth", return_value=None):
            with patch("agent.tools.tool_manager.ToolManager", FakeToolManager):
                payload = json.loads(web_channel.ToolsHandler().GET())

        tools = {item["name"]: item for item in payload["tools"]}
        self.assertIn("mcp__demo__search", tools)
        self.assertEqual(tools["mcp__demo__search"]["parameters"], {"properties": {"q": {}}})

    def test_v020_bridge_and_frontend_discover_channels_and_knowledge_graph(self):
        root = Path(__file__).resolve().parents[1]
        bridge_source = (root / "desktop" / "electron" / "apiBridge.ts").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

        for marker in [
            '"POST /api/channels"',
            '"GET /api/weixin/qrlogin"',
            '"POST /api/weixin/qrlogin"',
            '"GET /api/feishu/register"',
            '"POST /api/feishu/register"',
            '"GET /api/knowledge/graph"',
        ]:
            self.assertIn(marker, bridge_source)
        self.assertIn('apiJson<{ channels?: RuntimeChannel[] }>("/api/channels")', api_source)
        self.assertIn('const id = `channel:${name}`;', api_source)
        self.assertIn('type: "connector"', api_source)

    def test_v020_frontend_runtime_ui_state_uses_merge_except_explicit_project_delete(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("const mergedProjects = runtimeProjects ? mergeProjectFolders(projects, runtimeProjects) : projects;", app_source)
        self.assertIn("setProjects((current) => mergeProjectFolders(current, runtimeProjects));", app_source)
        self.assertIn('projectStateMode: "merge"', app_source)
        self.assertIn("replaceProjectState: false", app_source)
        self.assertIn("allowEmptyProjectState: true", app_source)
        self.assertIn("function deleteProject(project: ProjectFolder)", app_source)
        self.assertIn('projectStateMode: "replace"', app_source)

    def test_v021_frontend_preserves_project_session_ownership_from_cached_state(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("function sessionProjectIdFromState", app_source)
        self.assertIn("sessionUiState?.[sessionId]?.projectId", app_source)
        self.assertIn("const sessionProjectsRef = useRef<SessionProjectMap>(sessionProjects);", app_source)
        self.assertIn("const activeProjectIdRef = useRef(activeProjectId);", app_source)
        self.assertIn("const runtimeBinding = projectBindingFromRuntimeSession(session);", app_source)
        self.assertIn("const runtimeGeneralOwner = runtimeSessionDeclaresGeneralOwner(session);", app_source)
        self.assertIn("runtimeBinding?.projectId || (runtimeGeneralOwner ? null : sessionProjectIdFromState(id, sessionProjects, sessionUiState))", app_source)
        self.assertIn("sessionProjectIdFromState(sessionId, sessionProjectsRef.current, current", app_source)
        self.assertNotIn("projectId: sessionProjects[sessionId] || null", app_source)
        self.assertNotIn("const nextProjectId = sessionProjects[row.id] || null;", app_source)
        self.assertIn("const priority = (project: ProjectFolder) =>", app_source)
        self.assertIn("cachedMessages.some((message) => Boolean(message.recovery) || isUiLiveAssistantMessage(message))", app_source)

    def test_v021_frontend_treats_transient_stream_disconnect_as_reconnecting(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")

        self.assertIn('kind: "reconnecting"', app_source)
        self.assertIn("function streamReconnectingRecovery", app_source)
        self.assertIn("function clearTransientStreamRecovery", app_source)
        self.assertIn("function hasTransientStreamRecovery", app_source)
        self.assertIn("const streamReconnectTimers = useRef<Record<string, number>>({});", app_source)
        self.assertIn("const streamReconnectChecks = useRef<StringBoolMap>({});", app_source)
        self.assertIn("function clearStreamReconnectState", app_source)
        self.assertIn("if (streamReconnectTimers.current[reconnectKey] || streamReconnectChecks.current[reconnectKey]) return;", app_source)
        self.assertIn('recovery: streamReconnectingRecovery(requestId, "eventsource_error")', app_source)
        self.assertIn('recovery: streamReconnectingRecovery(requestId, "stream_idle_timeout")', app_source)
        self.assertIn('if (recovery.kind === "reconnecting")', app_source)
        self.assertIn('className="message-recovery-actions is-reconnecting ecorex-activity-status"', app_source)
        self.assertIn("if (hasTransientStreamRecovery(sessionId, assistantId, requestId))", app_source)
        self.assertIn("recovery: undefined", app_source)
        self.assertIn(".message-recovery-actions.is-reconnecting", css_source)
        self.assertIn("const STREAM_TRANSIENT_ERROR_GRACE_MS = 75_000;", api_source)
        self.assertIn("events.readyState !== EventSource.CLOSED", api_source)
        self.assertIn("function isTerminalVoiceStreamItem", api_source)
        self.assertNotIn("now - lastEventAt < STREAM_TRANSIENT_ERROR_GRACE_MS", api_source)

    def test_v021_tool_lease_heartbeat_and_observability_ui_contract(self):
        root = Path(__file__).resolve().parents[1]
        agent_source = (root / "agent" / "protocol" / "agent_stream.py").read_text(encoding="utf-8")
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        message_source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        console_css = (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")
        bash_source = (root / "agent" / "tools" / "bash" / "bash.py").read_text(encoding="utf-8")

        self.assertIn("TOOL_EXECUTION_DEFAULT_LEASE_SECONDS", agent_source)
        self.assertIn("tool_execution_heartbeat", agent_source)
        self.assertIn("tool_execution_deadline_extended", agent_source)
        self.assertIn("tool_execution_timeout", agent_source)
        self.assertIn("ECOREX_TOOL_EXECUTION_MAX_SECONDS", agent_source)
        self.assertIn("ECOREX_BASH_MAX_TIMEOUT_SECONDS", bash_source)
        self.assertIn("LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS = 30 * 60", bash_source)
        self.assertIn("_looks_long_running_command(command)", bash_source)
        self.assertIn("openai-image-vision|vision\\.sh", bash_source)
        self.assertIn("生图|图片生成|图片重生|图像生成", bash_source)
        self.assertIn("Use a larger value for long builds, deployments, image generation, or installs.", bash_source)

        self.assertIn('"type": "tool_heartbeat"', web_source)
        self.assertIn('"type": "tool_deadline_extended"', web_source)
        self.assertIn('reason="tool_timeout"', web_source)
        self.assertIn('terminal_status = "timeout"', web_source)

        self.assertIn('if (item.type === "tool_heartbeat")', app_source)
        self.assertIn('if (item.type === "tool_deadline_extended")', app_source)
        self.assertIn("function appendToolHeartbeat", app_source)
        self.assertIn("function appendToolDeadlineExtended", app_source)
        self.assertIn('params.get("ecorexRunCenter") === "1"', app_source)
        self.assertIn('window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) === "1"', app_source)
        self.assertNotIn('isRunning ? " ecorex-activity-status" : ""', app_source)
        for marker in ("deadline_seconds", "max_seconds", "extension_count", "lastHeartbeatAt"):
            self.assertIn(marker, api_source)
            self.assertIn(marker, message_source)

        self.assertIn(".ecorex-activity-dot", css_source)
        self.assertNotIn("@keyframes ecorex-text-sweep", css_source)
        self.assertNotIn("ecorex-text-sweep", css_source)
        self.assertNotIn("background-clip: text", css_source)
        self.assertNotIn(".ecorex-activity-status::after", css_source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css_source)
        self.assertNotIn(".thinking-ring::after", css_source)
        self.assertIn("window.EventSource.CONNECTING = NativeEventSource.CONNECTING;", console_source)
        self.assertIn("item.type === 'tool_heartbeat'", console_source)
        self.assertIn("item.type === 'tool_deadline_extended'", console_source)
        self.assertIn("function updateStreamToolMeta", console_source)
        self.assertIn(".tool-live-meta", console_css)
        self.assertIn(".tool-live-meta.is-live", console_css)
        self.assertIn("metaEl.classList.add('is-live')", console_source)
        self.assertIn("metaEl.classList.remove('is-live')", console_source)
        self.assertNotIn("@keyframes ecorexTextSweep", console_css)
        self.assertNotIn("ecorexTextSweep", console_css)
        self.assertIn(".ecorex-activity-status .agent-current-phase-text", console_css)
        self.assertNotIn("background-clip: text", console_css)
        self.assertNotIn("-webkit-text-fill-color: transparent", console_css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", console_css)
        self.assertNotIn(".ecorex-activity-status::after", console_css)

    def test_v022_web_status_motion_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "smoke-web-status-motion-browser.py").read_text(encoding="utf-8")

        self.assertIn("web_asset_server", script)
        self.assertIn("base_api_stub_script", script)
        self.assertIn("data-status-motion-smoke", script)
        self.assertIn("MotionSmokeEventSource", script)
        self.assertIn("typeof startSSE === 'function'", script)
        self.assertIn("startSSE('req-status-motion-smoke'", script)
        self.assertIn("ecorex-activity-status", script)
        self.assertIn("agent-current-phase-text", script)
        self.assertIn("tool-live-meta", script)
        self.assertIn("tool-motion-live", script)
        self.assertIn("tool-motion-terminal", script)
        self.assertIn("styleMetrics(statusText)", script)
        self.assertIn("window.getComputedStyle(status, '::after')", script)
        self.assertNotIn("ecorexTextSweep", script)
        self.assertIn("should not sweep animate", script)
        self.assertIn("should not be background-clipped to glyphs", script)
        self.assertIn("status container should not animate", script)
        self.assertIn("status ::after should not carry a light band", script)
        self.assertIn("terminal tool meta kept live animation class", script)
        self.assertIn("terminal tool meta should not animate", script)
        self.assertIn("terminal done left phase status animating", script)
        self.assertIn('page.emulate_media(reduced_motion="reduce")', script)
        self.assertIn("reduced-motion status text still animates", script)
        self.assertIn("reduced-motion changed live tool meta width", script)
        self.assertIn("web-status-motion-browser-smoke.png", script)

    def test_v022_hotfix_auth_identity_feishu_and_artifact_contracts(self):
        root = Path(__file__).resolve().parents[1]
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        message_source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")
        release_script = (root / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")
        core_requirements = (root / "desktop" / "runtime-packs" / "core-requirements.txt").read_text(encoding="utf-8")
        runtime_core_requirements = (root / "desktop" / "runtime" / "ecorex-runtime" / "core-requirements.txt").read_text(encoding="utf-8")

        self.assertIn("localFallback: !hasProvidedIdentity", web_source)
        self.assertIn('authProvider: hasProvidedIdentity ? "web-password" : "local-fallback"', web_source)
        self.assertIn('identitySource: hasProvidedIdentity ? "login-email" : "local-fallback"', web_source)
        self.assertIn("if (authRequired && !(identity && identity.email)) return null;", web_source)
        self.assertIn('body: { email: input.email, password: input.password }', web_source)
        self.assertIn('"localFallback": not has_provided_identity', web_source)
        self.assertIn('"identitySource": "login-email" if has_provided_identity else "local-fallback"', web_source)
        self.assertIn("def _create_auth_token(email: str = \"\")", web_source)
        self.assertIn("def _auth_token_email(token: str) -> str:", web_source)
        self.assertIn('payload["session"] = AuthLoginHandler._session_payload(email)', web_source)
        self.assertIn("writeLocalSession(auth.session);", web_source)

        self.assertIn("def _connect_registered_app(app_id: str, app_secret: str)", web_source)
        self.assertIn('ChannelsHandler()._handle_connect("feishu"', web_source)
        self.assertIn('"capability_refresh_required": bool(payload.get("capability_refresh_required"))', web_source)
        self.assertIn('"channel_configured": writeback.get("status") == "success"', web_source)
        self.assertIn("def _redact_feishu_register_text(value: Any) -> str:", web_source)
        self.assertIn('"credential": _feishu_register_secret_presence(app_id, app_secret)', web_source)
        self.assertIn("extract_feishu_register_credentials(result)", web_source)
        self.assertNotIn('"app_secret": app_secret', web_source)
        self.assertIn("lark-oapi>=1.5.5", core_requirements)
        self.assertIn("lark-oapi>=1.5.5", runtime_core_requirements)
        self.assertIn('Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"', release_script)
        self.assertNotIn('Ensure-PythonDependency -Python $python -StateDir $stateDir -ModuleName "lark_oapi"', release_script)
        self.assertIn('Write-OptionalPythonDependencyNotice -StateDir $stateDir -ModuleName "lark_oapi"', release_script)
        self.assertIn("function refreshFeishuAfterRegister()", console_source)
        self.assertNotIn("connectFeishuAfterRegister", console_source)

        self.assertIn("function artifactMergeKey", app_source)
        self.assertIn("function normalizeArtifactKeySource(value?: string)", app_source)
        self.assertNotIn('return `image:${fileName}`;', app_source)
        self.assertIn("function canonicalArtifactKey", message_source)
        self.assertNotIn('return `image:${fileName}`;', message_source)
        self.assertIn("function canonicalArtifactDedupeKey", console_source)
        self.assertNotIn("image:${basename}", console_source)
        self.assertIn("function appendArtifactCard(mediaEl, artifact)", console_source)
        self.assertIn('data-artifact-key', console_source)
        self.assertIn("if (appendArtifactCard(mediaEl, artifact)) scrollChatToBottom();", console_source)

        self.assertIn("const emptyMessages: ChatItem[] = []", app_source)
        self.assertIn("messagesRef.current = emptyMessages", app_source)
        self.assertIn("messagesRef.current = nextMessages", app_source)
        self.assertIn('const isNewSessionView = visibleMessages.length === 0 && !hasPendingAssistantMessage;', app_source)
        self.assertIn('generalSessions.some((row) => sessionRowNeedsReveal(row, { includeActive: false }))', app_source)
        self.assertIn("function createCodexLikeWelcomeScreen()", console_source)
        self.assertIn("和EcoreX一起开始工作", console_source)

        self.assertIn("const STREAM_RENDER_THROTTLE_CHARS = 1200;", message_source)
        self.assertIn("const STREAM_MARKDOWN_CHUNK_CHARS = 5000;", message_source)
        self.assertIn("function StreamingMarkdownBlock", message_source)
        self.assertIn("<StreamingStableMarkdown content={liveContent}", message_source)
        self.assertNotIn("function streamingWindowMarkdown(content: string)", message_source)
        self.assertNotIn("chars streaming", message_source)

    def test_v022_feishu_register_redaction_handles_json_and_colon_secret_shapes(self):
        from channel.web import web_channel

        raw = (
            '{"client_secret":"json-secret-123","app_secret":"app-secret-456",'
            '"open_id":"ou_secret_user","chat_id":"oc_secret_chat"} '
            "client_secret: colon-secret app_secret=equals-secret "
            "https://example.com/callback?token=secret-token"
        )
        redacted = web_channel._redact_feishu_register_text(raw)

        for leaked in (
            "json-secret-123",
            "app-secret-456",
            "ou_secret_user",
            "oc_secret_chat",
            "colon-secret",
            "equals-secret",
            "secret-token",
            "example.com",
        ):
            self.assertNotIn(leaked, redacted)
        self.assertIn("[redacted]", redacted)
        self.assertIn("[redacted-url]", redacted)

    def test_v022_feishu_register_credentials_accepts_sdk_shape_variants(self):
        from common.feishu_register_credentials import (
            extract_feishu_register_credentials,
            summarize_feishu_register_result_shape,
        )

        cases = [
            ({"client_id": "cli_client", "client_secret": "sec_client"}, ("cli_client", "sec_client")),
            ({"app_id": "cli_app", "app_secret": "sec_app"}, ("cli_app", "sec_app")),
            ({"data": {"app_id": "cli_data", "app_secret": "sec_data"}}, ("cli_data", "sec_data")),
            ({"data": {"app": {"clientId": "cli_nested", "clientSecret": "sec_nested"}}}, ("cli_nested", "sec_nested")),
        ]

        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(extract_feishu_register_credentials(payload), expected)

        summary = summarize_feishu_register_result_shape({
            "data": {"app": {"app_id": "cli_no_leak", "app_secret": "super-secret-value"}},
        })
        summary_text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("cli_no_leak", summary_text)
        self.assertNotIn("super-secret-value", summary_text)
        self.assertTrue(summary["appIdFieldPresent"])
        self.assertTrue(summary["appSecretFieldPresent"])

    def test_v022_hotfix_font_baseline_contract(self):
        root = Path(__file__).resolve().parents[1]
        token_source = (root / "desktop" / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
        app_css = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
        console_css = (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")
        site_css = (root / "deploy" / "ecorex-site" / "styles.css").read_text(encoding="utf-8")
        admin_css = (root / "deploy" / "ecorex-site" / "admin" / "admin.css").read_text(encoding="utf-8")
        chat_html = (root / "channel" / "web" / "chat.html").read_text(encoding="utf-8")

        for source in (token_source, console_css, site_css, admin_css, chat_html):
            self.assertIn("-apple-system", source)
            self.assertIn("BlinkMacSystemFont", source)
            self.assertIn('"Segoe UI"', source)
            self.assertIn("ui-monospace", source)
            self.assertIn('"SFMono-Regular"', source)
            self.assertIn("Consolas", source)

        self.assertIn("font-family: var(--font-sans)", app_css)
        self.assertIn("font-family: var(--font-mono)", app_css)
        self.assertNotIn("JetBrains Mono", console_css)
        self.assertNotIn("Fira Code", console_css)

    def test_v021_frontend_history_merge_drops_unanchored_recovery_cards(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("function isRecoveryAssistantMessage", app_source)
        self.assertIn("if (isRecoveryAssistantMessage(message))", app_source)
        self.assertIn("if (message.role === \"user\" && nextLocalMessage && isRecoveryAssistantMessage(nextLocalMessage))", app_source)
        self.assertIn("Boolean(localAssistant && isSameAssistantTurn(message, localAssistant))", app_source)
        self.assertIn("function rememberStreamTurnSequence", app_source)
        self.assertGreaterEqual(app_source.count("rememberStreamTurnSequence(sessionId, assistantId, requestId, item);"), 2)

    def test_v022_frontend_streaming_uses_markdown_blocks_and_deferred_token_estimate(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        message_source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")

        self.assertIn("function StreamingMarkdownBlock", message_source)
        self.assertIn("function StreamingStableMarkdown", message_source)
        self.assertIn("normalizeMarkdownForRender(redactInternalPromptText(content || \"\"))", message_source)
        self.assertIn("data-ecorex-file-path=/i.test(`${before} ${after}`)", message_source)
        self.assertIn("<StreamingMarkdownBlock content={content}", message_source)
        self.assertIn("<StreamingStableMarkdown content={liveContent}", message_source)
        self.assertIn(".streaming-markdown", css_source)
        self.assertNotIn(".streaming-tail .markdown-content", css_source)
        self.assertNotIn(".streaming-code", css_source)
        self.assertNotIn("function LiveStreamingText", message_source)
        self.assertNotIn("<LiveStreamingText content={content} />", message_source)
        self.assertNotIn(".live-streaming-text", css_source)
        self.assertIn("const [historyContextUsed, setHistoryContextUsed] = useState", app_source)
        self.assertIn("setHistoryContextUsed(estimateContextTokens(messagesRef.current, \"\", []));", app_source)
        self.assertIn("hasLiveMessage ? 900 : 120", app_source)
        self.assertNotIn("const historyContextUsed = useMemo(() => estimateContextTokens(messages, \"\", []), [messages]);", app_source)

    def test_v024_frontend_quality_evidence_projection_display_contract(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        message_source = (root / "desktop" / "src" / "components" / "MessageContent.tsx").read_text(encoding="utf-8")
        css_source = (root / "desktop" / "src" / "styles" / "app.css").read_text(encoding="utf-8")

        self.assertIn("export type QualityEvidence", api_source)
        self.assertIn("qualityEvidence?: QualityEvidence", api_source)
        self.assertIn("function normalizeQualityEvidence", app_source)
        self.assertIn("const QUALITY_EVIDENCE_ALLOWED_GATES", app_source)
        self.assertIn("function sanitizeQualityDetail", app_source)
        self.assertIn("decode-valid", app_source)
        self.assertIn("seam-check", app_source)
        self.assertIn("overlay-ghosting-check", app_source)
        self.assertIn("text-glyph-check", app_source)
        self.assertIn("watermark-check", app_source)
        self.assertIn("subject-structure-check", app_source)
        self.assertIn("anomaly-check", app_source)
        self.assertIn("reference-fidelity", app_source)
        self.assertIn("max_retries", app_source)
        self.assertIn("retry_count", app_source)
        self.assertIn("retry_gate", app_source)
        self.assertIn("retry_recommended", app_source)
        self.assertIn("finalized", app_source)
        self.assertIn("decode-valid", message_source)
        self.assertIn("seam-check", message_source)
        self.assertIn("text-glyph-check", message_source)
        self.assertIn("watermark-check", message_source)
        self.assertIn("anomaly-check", message_source)
        self.assertIn("reference-fidelity", message_source)
        self.assertIn("max_retries", message_source)
        self.assertIn("retry_count", message_source)
        self.assertIn("retry_gate", message_source)
        self.assertIn("retry_recommended", message_source)
        self.assertIn("finalized", message_source)
        self.assertIn("reference-fidelity-skipped-review", message_source)
        self.assertIn("shouldSurfaceSkippedQualityCheck", message_source)
        self.assertIn("qualityEvidence: normalizeQualityEvidence(tool.qualityEvidence", app_source)
        self.assertIn("merged.qualityEvidence = incoming.qualityEvidence || existing.qualityEvidence", app_source)
        self.assertIn("const qualityEvidence = normalizeQualityEvidence(raw.qualityEvidence || raw.quality_evidence)", app_source)
        self.assertIn("qualityEvidence,", app_source)
        self.assertIn("function QualityEvidenceBadge", message_source)
        self.assertIn("function qualityEvidenceDetail", message_source)
        self.assertNotIn("return record as QualityEvidence", message_source)
        self.assertIn("<QualityEvidencePanel evidence={qualityEvidence} />", message_source)
        self.assertIn("<QualityEvidenceBadge evidence={artifact.qualityEvidence} compact />", message_source)
        self.assertIn(".quality-evidence-badge", css_source)
        self.assertIn(".quality-evidence-panel", css_source)

    def test_v022_web_markdown_it_streaming_contract_uses_single_renderer(self):
        root = Path(__file__).resolve().parents[1]
        chat_source = (root / "channel" / "web" / "chat.html").read_text(encoding="utf-8")
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        console_css = (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")

        self.assertIn("assets/vendor/markdown-it/markdown-it.min.js", chat_source)
        self.assertIn("assets/vendor/highlightjs/highlight.min.js", chat_source)
        self.assertIn("html: false, breaks: true, linkify: true, typographer: true", console_source)
        self.assertIn("tokens[idx].attrPush(['target', '_blank']);", console_source)
        self.assertIn("tokens[idx].attrPush(['rel', 'noopener noreferrer']);", console_source)
        self.assertIn("function renderMarkdown(text)", console_source)
        self.assertIn("function renderStreamingMarkdown(text)", console_source)
        self.assertIn("contentEl.innerHTML = renderStreamingMarkdown(latest);", console_source)
        self.assertIn("contentEl.dataset.rawMd = latest;", console_source)
        self.assertIn("applyHighlighting(contentEl);", console_source)
        self.assertIn("bindChatKnowledgeLinks(contentEl);", console_source)
        self.assertIn("applyHighlighting(frozenEl);", console_source)
        self.assertIn("return escapeHtml(String(text || '')).replace(/\\n/g, '<br>');", console_source)
        self.assertIn("renderAnswerHtml(finalText)", console_source)
        self.assertIn("renderMarkdown(text)", console_source)
        self.assertIn("_ensureSafeBlankTargets(html)", console_source)
        self.assertIn("const projectedSeedContentEl = loadingEl && loadingEl.querySelector", console_source)
        self.assertIn("let projectionSeedReplayOffset = 0;", console_source)
        self.assertIn("let projectionSeedReplayActive = false;", console_source)
        self.assertIn("expectedReplayChunk", console_source)
        self.assertIn("const replayIndex = accumulatedText.indexOf(chunk, projectionSeedReplayOffset);", console_source)
        self.assertIn("(contentEl || projectedSeedContentEl).dataset.rawMd === accumulatedText", console_source)
        self.assertIn(".agent-subagent-step[data-tool-call-id=", console_source)
        self.assertIn("const projectedReplayToolEl = projectionSeedReplayActive ? findStreamToolEl(item) : null;", console_source)
        self.assertIn("currentToolEl = projectedReplayToolEl;", console_source)
        self.assertIn("projectionSeedReplayOffset = accumulatedText.length;", console_source)
        self.assertIn("projectionSeedReplayActive && loadingEl && loadingEl.classList && loadingEl.classList.contains('bot-message-group')", console_source)
        self.assertNotIn("streamLastEventIds[requestId] = projectedSeedEventId;", console_source)
        self.assertIn('data-tool-call-id="${escapeHtml(toolCallId)}"', console_source)
        self.assertNotIn("LONG_REPLY_PREVIEW_CHARS", console_source)
        self.assertIn('<div class="long-answer-preview">${renderMarkdown(text)}</div>', console_source)
        self.assertIn('target="_blank" rel="noopener noreferrer"', chat_source)
        self.assertIn(".msg-content p { margin: 0.5em 0; line-height: 1.7; }", console_css)
        self.assertIn("overflow-x: auto;", console_css)
        self.assertIn(".long-answer-preview", console_css)
        self.assertIn(".code-block-wrapper", console_css)
        self.assertNotIn("answer-stream-pre", console_source)
        self.assertNotIn("answer-stream-pre", console_css)
        self.assertNotIn("shouldRenderStreamingMarkdown", console_source)
        self.assertNotIn("STREAMING_MARKDOWN_PREVIEW_CHARS", console_source)

    def test_v022_web_streaming_markdown_hides_unstable_markers(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderMarkdown(value) {
  const text = String(value || "");
  if (/^#\s+/.test(text)) return `<h1>${escapeHtml(text.replace(/^#\s+/, ""))}</h1>`;
  if (/^-\s+/.test(text)) return `<ul><li>${escapeHtml(text.replace(/^-\s+/, ""))}</li></ul>`;
  return `<p>${escapeHtml(text)}</p>`;
}

eval([
  "_splitStreamingOpenFence",
  "_isUnstableStreamingMarkdownLine",
  "_trimStreamingUnstableTail",
  "_renderStreamingOpenFencePreview",
  "renderStreamingMarkdown",
].map(extractFunction).join("\n"));

const samples = {
  loneHeading: renderStreamingMarkdown("#"),
  heading: renderStreamingMarkdown("# Title"),
  listMarker: renderStreamingMarkdown("- "),
  list: renderStreamingMarkdown("- item"),
  openFence: renderStreamingMarkdown("```js\nconst x = 1 < 2"),
  partialFence: renderStreamingMarkdown("``"),
  tableDelimiter: renderStreamingMarkdown("| --- |"),
  xss: renderStreamingMarkdown("<img src=x onerror=alert(1)>"),
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(!/>#\s*</.test(samples.loneHeading), "lone heading marker leaked");
assert(samples.heading.includes("<h1>Title</h1>"), "stable heading did not render");
assert(!/>-\s*</.test(samples.listMarker), "dangling list marker leaked");
assert(samples.list.includes("<ul><li>item</li></ul>"), "stable list did not render");
assert(!samples.openFence.includes("```"), "open code fence marker leaked");
assert(samples.openFence.includes("streaming-open-code"), "open fence did not render code preview");
assert(samples.openFence.includes("language-js"), "open fence language was not preserved");
assert(samples.openFence.includes("&lt;"), "open fence body was not escaped");
assert(!/>``\s*</.test(samples.partialFence), "partial code fence marker leaked");
assert(!/>\|\s+---\s+\|\s*</.test(samples.tableDelimiter), "partial table delimiter leaked");
assert(samples.xss.includes("&lt;img"), "html fallback was not escaped");
process.stdout.write(JSON.stringify(samples));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("streaming-markdown-preview", payload["heading"])
        self.assertIn("streaming-markdown-preview", payload["openFence"])

    def test_v022_web_markdown_it_vendor_golden_fixture_contract(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");

global.window = global;
global.currentLang = "en";
global.__ecorexRuntimePath = (value) => /^\/(?:api|uploads)\//.test(String(value || "")) ? `/runtime${value}` : value;
global.window.markdownit = require("./channel/web/static/vendor/markdown-it/markdown-it.min.js");
global.window.hljs = require("./channel/web/static/vendor/highlightjs/highlight.min.js");
require("./channel/web/static/vendor/highlightjs/languages/javascript.min.js");
require("./channel/web/static/vendor/highlightjs/languages/python.min.js");
require("./channel/web/static/vendor/highlightjs/languages/bash.min.js");

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");
const start = source.indexOf("// Markdown Renderer");
const end = source.indexOf("// =====================================================================\r\n// Chat Module");
if (start < 0 || end < 0) throw new Error("renderer section markers missing");
eval(source.slice(start, end));

const finalFixture = [
  "# 标题 ✨",
  "",
  "第一行",
  "第二行 😀",
  "",
  "- 项目一",
  "- 项目二",
  "",
  "> 引用内容",
  "",
  "| A | B |",
  "| --- | --- |",
  "| 1 | 2 |",
  "",
  "```javascript",
  "const x = 1 < 2;",
  "```",
  "",
  "See https://example.com/page",
  "",
  "https://example.com/a.png",
  "",
  "![local](/C/Users/user/Pictures/a.png)",
  "",
  "<img src=x onerror=alert(1)>",
].join("\n");

const finalHtml = renderMarkdown(finalFixture);
const streamingHeading = renderStreamingMarkdown("# 标题 ✨");
const streamingLoneHeading = renderStreamingMarkdown("#");
const streamingListMarker = renderStreamingMarkdown("- ");
const streamingList = renderStreamingMarkdown("- 项目一");
const streamingTableDelimiter = renderStreamingMarkdown("| --- |");
const streamingPartialTableRow = renderStreamingMarkdown("| A | B");
const streamingPartialLink = renderStreamingMarkdown("[label](");
const streamingPartialImage = renderStreamingMarkdown("![alt");
const streamingPartialStrong = renderStreamingMarkdown("**bold");
const streamingStarList = renderStreamingMarkdown("* item");
const streamingOpenFence = renderStreamingMarkdown("```javascript\nconst x = 1 < 2");
const streamingXss = renderStreamingMarkdown("<img src=x onerror=alert(1)>");
const hostileVideo = _buildVideoHtml('https://example.com/a.mp4" onerror="alert(9)');
const hostileImage = _buildImageHtml('https://example.com/a.png" onerror="alert(8)');
const uploadUrl = _toWebUrl('/uploads/voice.mp3');
const localPosixUrl = _toWebUrl('/tmp/image.png');
const softBreak = renderMarkdown("第一行\n第二行");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(finalHtml.includes("<h1>标题 ✨</h1>"), "heading/emoji did not render");
assert(softBreak.includes("<br>"), "breaks:true soft break was not preserved");
assert(finalHtml.includes("<ul>") && finalHtml.includes("<li>项目一</li>"), "list did not render");
assert(finalHtml.includes("<blockquote>") && finalHtml.includes("引用内容"), "blockquote did not render");
assert(finalHtml.includes("<table>") && finalHtml.includes("<td>1</td>"), "table did not render");
assert(finalHtml.includes("language-javascript"), "fenced code language class missing");
assert(finalHtml.includes("&lt;") && finalHtml.includes("hljs-keyword"), "code block was not escaped/highlighted safely");
assert(finalHtml.includes('<a href="https://example.com/page" target="_blank" rel="noopener noreferrer"'), "safe markdown link attributes missing");
assert(finalHtml.includes('data-artifact-kind="image"'), "image preview artifact wrapper missing");
assert(finalHtml.includes('download="a.png" target="_blank" rel="noopener noreferrer"'), "artifact action safe link attributes missing");
assert(finalHtml.includes("/runtime/api/file?path="), "local image path was not rewritten through runtime file API");
assert(finalHtml.includes("&lt;img src=x onerror=alert(1)&gt;"), "raw HTML was not escaped");
assert(!finalHtml.includes("<img src=x onerror=alert(1)>"), "raw scriptable HTML leaked");

assert(streamingHeading.includes("<h1>标题 ✨</h1>"), "stable streaming heading did not render");
assert(!/>#\s*</.test(streamingLoneHeading), "lone streaming heading marker leaked");
assert(!/>-\s*</.test(streamingListMarker), "dangling streaming list marker leaked");
assert(streamingList.includes("<ul>") && streamingList.includes("<li>项目一</li>"), "stable streaming list did not render with real markdown-it");
assert(!/>\|\s+---\s+\|\s*</.test(streamingTableDelimiter), "streaming table delimiter leaked");
assert(!streamingPartialTableRow.includes("| A | B"), "partial streaming table row leaked");
assert(!streamingPartialLink.includes("[label]("), "partial streaming link leaked");
assert(!streamingPartialImage.includes("![alt"), "partial streaming image leaked");
assert(!streamingPartialStrong.includes("**bold"), "partial streaming strong marker leaked");
assert(streamingStarList.includes("<ul>") && streamingStarList.includes("<li>item</li>"), "star list was mistaken for partial emphasis");
assert(streamingOpenFence.includes("streaming-open-code"), "open code fence preview missing");
assert(streamingOpenFence.includes("language-javascript"), "open code fence language missing");
assert(!streamingOpenFence.includes("```"), "open code fence marker leaked");
assert(streamingOpenFence.includes("const x = 1 &lt; 2"), "open code fence body was not escaped");
assert(streamingXss.includes("&lt;img src=x onerror=alert(1)&gt;"), "streaming XSS was not escaped by real renderer");
assert(!streamingXss.includes("<img src=x onerror=alert(1)>"), "streaming raw scriptable HTML leaked");
assert(!hostileVideo.includes('" onerror="'), "video URL broke out of source src attribute");
assert(hostileVideo.includes("&quot; onerror=&quot;alert(9)"), "video URL quote was not attribute-escaped");
assert(hostileVideo.includes('target="_blank" rel="noopener noreferrer"'), "video artifact action safe rel missing");
assert(!hostileImage.includes('" onerror="'), "image URL broke out of img src attribute");
assert(hostileImage.includes("&quot; onerror=&quot;alert(8)"), "image URL quote was not attribute-escaped");
assert(hostileImage.includes('target="_blank" rel="noopener noreferrer"'), "image artifact action safe rel missing");
assert(uploadUrl === "/runtime/uploads/voice.mp3", "runtime uploads path was incorrectly routed through file API");
assert(localPosixUrl.includes("/runtime/api/file?path="), "local POSIX path was not routed through backend file API");

process.stdout.write(JSON.stringify({
  finalHtml,
  streamingHeading,
  streamingList,
  streamingStarList,
  streamingOpenFence,
  streamingXss,
  hostileVideo,
  hostileImage,
  uploadUrl,
  localPosixUrl,
}));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("标题 ✨", payload["finalHtml"])
        self.assertIn("streaming-markdown-preview", payload["streamingHeading"])
        self.assertIn("项目一", payload["streamingList"])
        self.assertIn("<li>item</li>", payload["streamingStarList"])
        self.assertIn("streaming-open-code", payload["streamingOpenFence"])
        self.assertIn("&lt;img", payload["streamingXss"])
        self.assertIn("&quot; onerror=&quot;alert(9)", payload["hostileVideo"])
        self.assertIn("&quot; onerror=&quot;alert(8)", payload["hostileImage"])
        self.assertIn('target="_blank" rel="noopener noreferrer"', payload["hostileVideo"])
        self.assertIn('target="_blank" rel="noopener noreferrer"', payload["hostileImage"])

    def test_v022_web_code_block_header_dom_postprocess_is_idempotent(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

class FakeClassList {
  constructor(owner, values = []) {
    this.owner = owner;
    this.values = Array.from(values);
  }
  contains(value) { return this.values.includes(value); }
  [Symbol.iterator]() { return this.values[Symbol.iterator](); }
  setFromClassName(value) {
    this.values = String(value || "").split(/\s+/).filter(Boolean);
  }
}

class FakeElement {
  constructor(tagName, classNames = []) {
    this.tagName = String(tagName || "").toLowerCase();
    this.children = [];
    this.parentNode = null;
    this.parentElement = null;
    this._className = "";
    this.classList = new FakeClassList(this, classNames);
    this.innerHTML = "";
  }
  set className(value) {
    this._className = String(value || "");
    this.classList.setFromClassName(this._className);
  }
  get className() { return this._className; }
  appendChild(child) {
    if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child);
    this.children.push(child);
    child.parentNode = this;
    child.parentElement = this;
  }
  insertBefore(newNode, referenceNode) {
    if (newNode.parentNode) newNode.parentNode.children = newNode.parentNode.children.filter(item => item !== newNode);
    const index = this.children.indexOf(referenceNode);
    this.children.splice(index >= 0 ? index : this.children.length, 0, newNode);
    newNode.parentNode = this;
    newNode.parentElement = this;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const wanted = String(selector || "").toLowerCase();
    const out = [];
    function visit(node) {
      for (const child of node.children) {
        if (wanted === child.tagName) out.push(child);
        visit(child);
      }
    }
    visit(this);
    return out;
  }
}

global.document = {
  createElement(tagName) {
    return new FakeElement(tagName);
  },
};

eval(extractFunction("_addCodeBlockHeaders"));

const container = new FakeElement("div");
const pre = new FakeElement("pre");
const code = new FakeElement("code", ["language-python"]);
pre.appendChild(code);
container.appendChild(pre);

_addCodeBlockHeaders(container);
_addCodeBlockHeaders(container);

const wrappers = container.children.filter(child => child.classList.contains("code-block-wrapper"));
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
assert(wrappers.length === 1, "duplicate code-block wrapper was added");
const wrapper = wrappers[0];
assert(wrapper.children.length === 2, "wrapper should contain header and pre");
assert(wrapper.children[0].classList.contains("code-block-header"), "code block header missing");
assert(wrapper.children[0].innerHTML.includes("Python"), "language label was not normalized");
assert(wrapper.children[0].innerHTML.includes("code-copy-btn"), "copy button missing");
assert(wrapper.children[1] === pre, "pre was not moved into wrapper");

const unknownContainer = new FakeElement("div");
const unknownPre = new FakeElement("pre");
const unknownCode = new FakeElement("code", ["language-undefined"]);
unknownPre.appendChild(unknownCode);
unknownContainer.appendChild(unknownPre);
_addCodeBlockHeaders(unknownContainer);
const unknownHeader = unknownContainer.children[0].children[0];
assert(!unknownHeader.innerHTML.includes("Undefined"), "undefined language label should be hidden");

process.stdout.write(JSON.stringify({
  wrapperCount: wrappers.length,
  headerHtml: wrapper.children[0].innerHTML,
  unknownHeaderHtml: unknownHeader.innerHTML,
}));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["wrapperCount"], 1)
        self.assertIn("code-copy-btn", payload["headerHtml"])
        self.assertNotIn("Undefined", payload["unknownHeaderHtml"])

    def test_v022_web_markdown_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_source = (root / "scripts" / "smoke-web-markdown-browser.py").read_text(encoding="utf-8")
        support_source = (root / "scripts" / "web_smoke_support.py").read_text(encoding="utf-8")

        self.assertIn("from web_smoke_support import ROOT, base_api_stub_script, web_asset_server", smoke_source)
        self.assertIn("class WebAssetHandler", support_source)
        self.assertIn("STATIC_ROOT = WEB_ROOT / \"static\"", support_source)
        self.assertIn("def base_api_stub_script", support_source)
        self.assertIn("class QuietThreadingHTTPServer", support_source)
        self.assertIn("def handle_error", support_source)
        self.assertIn("from playwright.sync_api import sync_playwright", smoke_source)
        self.assertIn("page.add_init_script(_stubbed_api_script())", smoke_source)
        self.assertIn("typeof renderMarkdown === 'function'", smoke_source)
        self.assertIn("typeof renderStreamingMarkdown === 'function'", smoke_source)
        self.assertIn("renderAnswerHtml(finalFixture)", smoke_source)
        self.assertIn("renderStreamingMarkdown(source)", smoke_source)
        self.assertIn("createBotMessageEl(finalFixture", smoke_source)
        self.assertIn("renderRuntimeProjectionRequest(runningProjection", smoke_source)
        self.assertIn("rawMdPreserved", smoke_source)
        self.assertIn("console_errors", smoke_source)
        self.assertIn("\"# Browser Smoke \\u2713\"", smoke_source)
        self.assertIn("\"second line \\U0001f600\"", smoke_source)
        self.assertIn("parser.add_argument(\"--screenshot\"", smoke_source)

    def test_v022_web_project_session_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_source = (root / "scripts" / "smoke-web-project-session-browser.py").read_text(encoding="utf-8")
        support_source = (root / "scripts" / "web_smoke_support.py").read_text(encoding="utf-8")

        self.assertIn("class StaticSiteHandler", support_source)
        self.assertIn("def static_site_server(root: Path", support_source)
        self.assertIn("from web_smoke_support import ROOT, static_site_server", smoke_source)
        self.assertIn("parser.add_argument(\"--app-root\", default=\"desktop/dist\"", smoke_source)
        self.assertIn("window.ecorexDesktop = {", smoke_source)
        self.assertIn("getEnterpriseSession: () => makeResult", smoke_source)
        self.assertIn("getSidecarStatus: () => makeResult", smoke_source)
        self.assertIn("apiJson: ({ path, method, body }) =>", smoke_source)
        self.assertIn("pathname === '/api/sessions'", smoke_source)
        self.assertIn("pathname === '/message'", smoke_source)
        self.assertIn("sentBodies.push(JSON.parse(JSON.stringify(body || {})))", smoke_source)
        self.assertIn("project_context_meta", smoke_source)
        self.assertIn("project_context_meta.projectId === 'proj-a'", smoke_source)
        self.assertIn("sent.hidden_context", smoke_source)
        self.assertIn("file_type === 'directory'", smoke_source)
        self.assertIn("projectRows().some((row) => text(row).includes('Project Saved'))", smoke_source)
        self.assertIn("generalRows().some((row) => text(row).includes('General Saved'))", smoke_source)
        self.assertIn("project session leaked into general list", smoke_source)
        self.assertIn("general session leaked into project list", smoke_source)
        self.assertIn("row.getAttribute('draggable') === 'false'", smoke_source)
        self.assertIn("new DragEvent('dragstart'", smoke_source)
        self.assertIn("event.defaultPrevented || allowed === false", smoke_source)
        self.assertIn("row.dataset.sessionOwnership === 'project'", smoke_source)
        self.assertIn("row.dataset.sessionOwnership === 'general'", smoke_source)
        self.assertIn("composer autosize", smoke_source)
        self.assertIn("textarea.getBoundingClientRect().height > initialHeight", smoke_source)
        self.assertIn("expandedHeight <= maxHeight + 2", smoke_source)
        self.assertIn("ecorex-project-proj-a-", smoke_source)
        self.assertIn("!Object.keys(state).some((key) => key.startsWith('ecorex-pending-project-'))", smoke_source)
        self.assertIn("parser.add_argument(\"--screenshot\"", smoke_source)

    def test_v023_session_cross_talk_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_path = root / "scripts" / "smoke-web-session-cross-talk-browser.py"
        smoke_source = smoke_path.read_text(encoding="utf-8")
        probe_script = python_function_literal_return(smoke_path, "_probe_script")

        self.assertIn("from web_smoke_support import ROOT, static_site_server", smoke_source)
        self.assertIn("parser.add_argument(\"--app-root\", default=\"desktop/dist\"", smoke_source)
        self.assertIn("localStorage.setItem('ecorex-pinned-sessions'", smoke_source)
        self.assertIn("url.searchParams.get('include_pinned') === '1'", smoke_source)
        self.assertIn("pinnedCount: pinnedIds.length", smoke_source)
        self.assertIn("assert(!hasProject('General Backend Wins')", probe_script)
        self.assertIn("backend general session leaked into project bucket through stale local binding", probe_script)
        self.assertIn("assert(!hasGeneral('Project Backend Wins')", probe_script)
        self.assertIn("backend project session leaked into general bucket", probe_script)
        self.assertIn("assert(pinnedNewerIndex < pinnedOldIndex", probe_script)
        self.assertIn("pinned sessions were not sorted newest-first inside pinned group", probe_script)
        self.assertIn("assert(pinnedOldIndex < unpinnedFreshIndex", probe_script)
        self.assertIn("pinned group did not stay above newer unpinned sessions", probe_script)
        self.assertIn("assert(pinnedAfterRename['session-general-backend'] !== true", probe_script)
        self.assertIn("rename auto-pinned a previously unpinned session", probe_script)
        self.assertIn("renameDidNotPin", probe_script)
        self.assertIn("fixtureHash", smoke_source)
        self.assertIn("parser.add_argument(\"--artifact\"", smoke_source)

    def test_v023_session_refresh_replay_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_path = root / "scripts" / "smoke-web-session-cross-talk-refresh-replay.py"
        smoke_source = smoke_path.read_text(encoding="utf-8")
        stub_script = python_function_literal_return(smoke_path, "_stub_script")
        race_script = python_function_literal_return(smoke_path, "_race_probe_script")
        refresh_script = python_function_literal_return(smoke_path, "_refresh_probe_script")

        self.assertIn("from web_smoke_support import ROOT, static_site_server", smoke_source)
        self.assertIn("parser.add_argument(\"--app-root\", default=\"desktop/dist\"", smoke_source)
        self.assertNotIn("B cached before smoke", stub_script + race_script + refresh_script)
        self.assertNotIn("window.ecorexDesktop.apiJson", race_script)
        self.assertIn("A LATE CONTENT MUST NOT APPEAR", stub_script + race_script + refresh_script)
        self.assertIn("B CLEAN CONTENT STAYS VISIBLE", stub_script + race_script + refresh_script)
        self.assertIn("late A history polluted active B session", race_script)
        self.assertIn("SESSION_MISMATCH", stub_script)
        self.assertIn("streamExpectedSessionObserved", race_script)
        self.assertIn("mismatchDiagnosticObserved", race_script)
        self.assertIn("renderer mismatch diagnostic polluted UI", race_script)
        self.assertIn("assert(projectionCallCount <= 6", race_script)
        self.assertIn("assert(streamCallCount <= 6", race_script)
        self.assertIn("assert(backendHistoryFetched", refresh_script)
        self.assertIn("call.target === 'session-b'", refresh_script)
        self.assertIn("page.reload", smoke_source)
        self.assertIn("refreshRejectedLateSession", refresh_script)
        self.assertIn("backendHistoryFetched", refresh_script)
        self.assertIn("fixtureHash", smoke_source)
        self.assertIn("parser.add_argument(\"--artifact\"", smoke_source)

    def test_v022_web_long_answer_preview_renders_full_markdown_before_css_clip(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

global.currentLang = "en";
function renderMarkdown(value) {
  return `<rendered data-len="${String(value || "").length}">${escapeHtml(value)}</rendered>`;
}

eval([
  "const LONG_REPLY_COLLAPSE_CHARS = 1800;",
  extractFunction("longAnswerLabel"),
  extractFunction("renderAnswerHtml"),
].join("\n"));

const markdown = [
  "```javascript",
  "const value = 1 < 2;",
  "```",
  "",
  "tail-" + "x".repeat(1900),
].join("\n");
const html = renderAnswerHtml(markdown, false);
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
assert(html.includes("long-answer-preview"), "collapsed long-answer preview missing");
assert(html.includes(`data-len="${markdown.length}"`), "collapsed preview did not render full markdown");
assert(html.includes("```javascript"), "fence opening was sliced out");
assert(html.includes("tail-"), "tail content was sliced out");
assert(!html.includes("..."), "collapsed preview should rely on CSS clipping, not raw markdown ellipsis");
process.stdout.write(JSON.stringify({ html, len: markdown.length }));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn("long-answer-preview", payload["html"])
        self.assertGreater(payload["len"], 1800)

    def test_v022_web_tool_result_file_urls_are_attribute_escaped(self):
        root = Path(__file__).resolve().parents[1]
        node_script = r"""
const fs = require("fs");
global.window = global;
global.currentLang = "en";
global.__ecorexRuntimePath = (value) => value;

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");
const rendererStart = source.indexOf("// Markdown Renderer");
const rendererEnd = source.indexOf("// =====================================================================\r\n// Chat Module");
if (rendererStart < 0 || rendererEnd < 0) throw new Error("renderer section markers missing");
eval(source.slice(rendererStart, rendererEnd));

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}
eval(extractFunction("_renderSentFileFromToolResult"));

const payload = {
  type: "file_to_send",
  path: 'https://example.com/report.txt" onclick="alert(1)',
  file_type: "file",
  file_name: 'bad" onclick="alert(2).txt',
};
const html = _renderSentFileFromToolResult({ result: JSON.stringify(payload) });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
assert(html.includes('rel="noopener noreferrer"'), "file link safe rel missing");
assert(!html.includes('" onclick="alert'), "hostile file URL/name broke out of attribute");
assert(html.includes("&quot; onclick=&quot;alert(1)"), "file URL quote was not escaped");
assert(html.includes("bad&quot; onclick=&quot;alert(2).txt"), "file name quote was not escaped");

process.stdout.write(JSON.stringify({ html }));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertIn('rel="noopener noreferrer"', payload["html"])
        self.assertNotIn('" onclick="alert', payload["html"])

    def test_v022_web_stream_terminal_and_loss_converge_from_runtime_projection(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")

        self.assertIn("async function loadRequestRuntimeProjection(requestId, opts)", console_source)
        self.assertIn("fetch(`/api/runtime-projection?${params.toString()}`", console_source)
        self.assertIn("function applyRuntimeProjectionSnapshot(projection, reason)", console_source)
        self.assertIn("function refreshRuntimeProjectionSnapshot(reason)", console_source)
        self.assertIn("function startNonSseProjectionLoop(reason)", console_source)
        self.assertIn("typeof window.EventSource !== 'function'", console_source)
        self.assertIn("falling back to runtime projection polling", console_source)
        self.assertIn("renderRuntimeProjectionRequest(projection, 'poll_projection')", console_source)
        self.assertIn("runtimeProjectionBotSelector(rid)", console_source)
        self.assertIn("botEl.dataset.runtimeProjectionSource", console_source)
        self.assertIn("botEl.dataset.runtimeProjectionEventId", console_source)
        self.assertIn("runtimeProjectionAssistantMessage(projection)", console_source)
        self.assertIn("runtimeProjectionArtifacts(assistant, projection)", console_source)
        self.assertIn("runtimeProjectionIsTerminal(projection, assistant)", console_source)
        self.assertIn("void refreshRuntimeProjectionSnapshot('sse_terminal');", console_source)
        self.assertIn("void refreshRuntimeProjectionSnapshot('sse_error');", console_source)
        self.assertIn("void refreshRuntimeProjectionSnapshot('stream_lost').then(applied =>", console_source)
        self.assertLess(
            console_source.index("void refreshRuntimeProjectionSnapshot('stream_lost').then(applied =>"),
            console_source.index("if (!applied) renderLegacyStreamLoss();"),
        )

        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

eval([
  "runtimeProjectionAssistantMessage",
  "runtimeProjectionImageJobs",
  "runtimeProjectionIsTerminal",
  "normalizeArtifactSourceForDedupe",
  "canonicalArtifactDedupeKey",
  "runtimeProjectionArtifacts",
].map(extractFunction).join("\n"));

const projection = {
  state: "completed",
  messages: [
    { role: "user", content: "prompt" },
    { role: "assistant", content: "older", artifacts: [{ title: "same.png", path: "/tmp/same.png" }] },
    { role: "assistant", content: "final", pending: false, artifacts: [{ title: "same.png", path: "/tmp/same.png" }] },
  ],
  image_jobs: [
    { artifacts: [
      { title: "same.png", path: "/tmp/same.png" },
      { title: "second.png", path: "/tmp/second.png" },
      { title: "output.png", path: "/tmp/run-a/output.png" },
      { title: "output.png", path: "/tmp/run-b/output.png" },
    ] }
  ]
};

const assistant = runtimeProjectionAssistantMessage(projection);
const artifacts = runtimeProjectionArtifacts(assistant, projection);
const payload = {
  assistantContent: assistant.content,
  terminal: runtimeProjectionIsTerminal(projection, assistant),
  artifacts: artifacts.map(item => ({ title: item.title, path: item.path })),
};
if (payload.assistantContent !== "final") throw new Error("did not choose latest assistant message");
if (!payload.terminal) throw new Error("terminal projection not detected");
if (payload.artifacts.length !== 4) throw new Error("artifacts were over- or under-deduped");
process.stdout.write(JSON.stringify(payload));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["assistantContent"], "final")
        self.assertEqual(
            [item["path"] for item in payload["artifacts"]],
            ["/tmp/same.png", "/tmp/second.png", "/tmp/run-a/output.png", "/tmp/run-b/output.png"],
        )

    def test_v022_web_history_load_refreshes_session_runtime_projection(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")

        for marker in (
            "let sessionRuntimeProjectionCursors = {};",
            "async function loadSessionRuntimeProjection(sid, opts)",
            "function updateSessionRuntimeProjectionCursor(ownerSession, latestEventId)",
            "params.set('session_id', ownerSession);",
            "function runtimeProjectionUserMessage(projection)",
            "function runtimeProjectionActiveState(projection, assistant)",
            "function updateBotMessageElFromRuntimeProjection(botEl, projection, reason)",
            "function renderRuntimeProjectionRequest(projection, reason)",
            "async function refreshSessionRuntimeProjection(reason, opts)",
            "function normalizeRuntimeProjectionHistoryPayload(payload)",
            "function fetchHistoryPage(ownerSession, page)",
            "params.set('history_page', String(page));",
            "return fetch(`/api/runtime-projection?${params.toString()}`, { cache: 'no-store' })",
            "throw new Error('runtime projection history unavailable');",
            "fetch(`/api/history?session_id=${encodeURIComponent(ownerSession)}&page=${page}&page_size=20`)",
            "const ownerSession = sessionId;",
            "fetchHistoryPage(ownerSession, page)",
            "const messages = Array.isArray(data.messages) ? data.messages : [];",
            "(messages.length === 0 && runtimeRequests.length === 0)",
            "messages.forEach(msg => {",
            "if (ownerSession !== sessionId) return;",
            "renderRuntimeProjectionRequest(requestProjection, 'history_projection');",
            "updateSessionRuntimeProjectionCursor(ownerSession, data.runtime_projection.latest_event_id);",
            "void refreshSessionRuntimeProjection('history_load_recheck', {",
            "afterEventId: sessionRuntimeProjectionCursors[ownerSession] || 0",
            "sessionActiveRequest[sessionId] = requestId;",
            "startSSE(requestId, botEl, new Date(), null);",
            "botEl.dataset.runtimeProjectionState",
            "artifact.preview_url",
            "artifact.relative_path",
            "artifact.file_name",
        ):
            self.assertIn(marker, console_source)

        self.assertLess(
            console_source.index("const ownerSession = sessionId;"),
            console_source.index("fetchHistoryPage(ownerSession, page)"),
        )
        self.assertLess(
            console_source.index("return fetch(`/api/runtime-projection?${params.toString()}`, { cache: 'no-store' })"),
            console_source.index("fetch(`/api/history?session_id=${encodeURIComponent(ownerSession)}&page=${page}&page_size=20`)"),
        )
        self.assertLess(
            console_source.index("void refreshSessionRuntimeProjection('history_load_recheck', {"),
            console_source.index("}).catch(err => console.warn('[runtime-projection] session refresh failed:', err));"),
        )

        node_script = r"""
const fs = require("fs");
const source = fs.readFileSync("channel/web/static/js/console.js", "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing ${name}`);
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

eval([
  "runtimeProjectionUserMessage",
  "runtimeProjectionAssistantMessage",
  "runtimeProjectionActiveState",
].map(extractFunction).join("\n"));

const active = {
  state: "streaming",
  messages: [
    { role: "user", content: "prompt" },
    { role: "assistant", content: "partial", pending: true },
  ],
};
const terminal = {
  state: "completed",
  messages: [
    { role: "assistant", content: "done", pending: false },
  ],
};
const payload = {
  userContent: runtimeProjectionUserMessage(active).content,
  assistantContent: runtimeProjectionAssistantMessage(active).content,
  activeState: runtimeProjectionActiveState(active, runtimeProjectionAssistantMessage(active)),
  terminalState: runtimeProjectionActiveState(terminal, runtimeProjectionAssistantMessage(terminal)),
};
if (payload.userContent !== "prompt") throw new Error("user projection not found");
if (payload.assistantContent !== "partial") throw new Error("assistant projection not found");
if (!payload.activeState) throw new Error("active state not detected");
if (payload.terminalState) throw new Error("terminal state treated as active");
process.stdout.write(JSON.stringify(payload));
"""
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["activeState"])
        self.assertFalse(payload["terminalState"])

    def test_v022_web_runtime_projection_reconnect_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        script = (root / "scripts" / "smoke-web-runtime-projection-reconnect-browser.py").read_text(encoding="utf-8")

        self.assertIn("function _hasUnsafeUrlScheme(url)", console_source)
        self.assertIn("const rawUrl = String(url).trim();", console_source)
        self.assertIn("if (_hasUnsafeUrlScheme(rawUrl)) return '';", console_source)
        self.assertIn("return _hasUnsafeUrlScheme(webUrl) ? '' : webUrl;", console_source)
        self.assertIn("if (!webUrl) return '';", console_source)
        self.assertIn("artifact-card-disabled", console_source)
        self.assertIn("const toolName = escapeHtml(String(item.tool || 'tool'));", console_source)
        self.assertIn("timeEl.textContent = `${String(item.execution_time)}s`;", console_source)
        self.assertIn("existingSameRequestBubble", console_source)
        self.assertIn("Always", console_source)
        self.assertIn("function ensureRuntimeProjectionUserMessage(projection, requestId, botEl, reason)", console_source)
        self.assertIn("userEl.dataset.runtimeProjectionUserForRequest = requestId;", console_source)
        self.assertIn("ensureRuntimeProjectionUserMessage(projection, requestId, botEl, reason);", console_source)

        self.assertIn("web_asset_server", script)
        self.assertIn("base_api_stub_script", script)
        self.assertIn("localStorage.setItem('cow_session_id', 'session-projection-smoke')", script)
        self.assertIn("path === '/api/runtime-projection'", script)
        self.assertIn("url.searchParams.has('history_page')", script)
        self.assertIn("historyProjectionFetches", script)
        self.assertIn("historyFallbackCalls", script)
        self.assertIn("req-history-projection", script)
        self.assertIn("req-history-active", script)
        self.assertIn("PassiveEventSource", script)
        self.assertIn("Active History Projection", script)
        self.assertIn("history projection user prompt missing", script)
        self.assertIn("active projection user prompt missing", script)
        self.assertIn("req-stable-stream", script)
        self.assertIn("StableEventSource", script)
        self.assertIn("<img src=x onerror=alert(1)>", script)
        self.assertIn("stable hostile tool HTML created an image", script)
        self.assertIn("stable hostile tool HTML created an event handler", script)
        self.assertIn("stable stream unexpectedly reconnected", script)
        self.assertIn("message-recovery-actions", script)
        self.assertIn("stable stream showed recovery actions", script)
        self.assertIn("startSSE('req-projection-loss'", script)
        self.assertIn("LostEventSource", script)
        self.assertIn("Math.min(Number(delay) || 0, 2)", script)
        self.assertIn("dataset.runtimeProjectionSource === 'stream_lost'", script)
        self.assertIn("lostState.lostStreamUrls || []).length >= 11", script)
        self.assertIn("Recovered after stream loss", script)
        self.assertIn("javascript:alert(1)", script)
        self.assertIn("file:///C:/Users/user/private.txt", script)
        self.assertIn("disabledUnsafeArtifacts.length === 2", script)
        self.assertIn("disabled unsafe artifact titles missing", script)
        self.assertIn("disabled unsafe artifacts exposed links or actions", script)
        self.assertIn("!/^file:/i.test(href)", script)
        self.assertIn("!href.includes('/api/file?path=')", script)
        self.assertIn("unsafe projection artifact href survived", script)
        self.assertIn("stream-loss projection created duplicate bot bubbles", script)
        self.assertIn("req-poll-image-job", script)
        self.assertIn("pollProjectionReady", script)
        self.assertIn("stale poll placeholder should update", script)
        self.assertIn("poll projection image job rendered", script)
        self.assertIn("poll stale bubble was not updated from projection", script)
        self.assertIn("legacy poll fallback should not render", script)
        self.assertIn("rendered instead of projection", script)
        self.assertIn("req-non-sse-image-job", script)
        self.assertIn("window.EventSource = undefined", script)
        self.assertIn("non-SSE image job projection rendered", script)
        self.assertIn("non-SSE projection endpoint was not polled", script)
        self.assertIn("non-SSE fallback showed recovery actions", script)
        self.assertIn("legacy /api/history fallback was used", script)
        self.assertIn("web-runtime-projection-reconnect-browser.py", str(root / "scripts" / "smoke-web-runtime-projection-reconnect-browser.py"))

    def test_v022_web_runtime_projection_history_pagination_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        script = (root / "scripts" / "smoke-web-runtime-projection-history-pagination-browser.py").read_text(encoding="utf-8")

        for marker in (
            "function updateSessionRuntimeProjectionCursor(ownerSession, latestEventId)",
            "updateSessionRuntimeProjectionCursor(ownerSession, projection.latest_event_id);",
            "updateSessionRuntimeProjectionCursor(ownerSession, data.runtime_projection.latest_event_id);",
            "afterEventId: sessionRuntimeProjectionCursors[ownerSession] || 0",
            "fetch(`/api/runtime-projection?${params.toString()}`, { cache: 'no-store' })",
            "fetch(`/api/history?session_id=${encodeURIComponent(ownerSession)}&page=${page}&page_size=20`)",
        ):
            self.assertIn(marker, console_source)

        for marker in (
            "web_asset_server",
            "base_api_stub_script",
            "localStorage.setItem('cow_session_id', 'session-history-pagination-smoke')",
            "path === '/api/runtime-projection'",
            "url.searchParams.has('history_page')",
            "state.historyProjectionFetches.push(url.search)",
            "req-history-page1",
            "req-history-page2",
            "req-new-after-cursor",
            "req-should-not-full-replay",
            "Page One Projection",
            "Page Two Projection",
            "Cursor Delta",
            "page one cursor recheck used after_event_id=200",
            "paramsFor(search).get('after_event_id') === '200'",
            "paramsFor(search).get('after_event_id') === '210'",
            "page 1 request duplicated between history messages and runtime requests",
            "page 2 request duplicated between history messages and runtime requests",
            "legacy history fallback was used during primary projection page 1",
            "legacy history fallback was used during primary projection page 2",
            "weak-network fallback history response",
            "weak-network fallback was not isolated to the forced projection failure",
        ):
            self.assertIn(marker, script)
        self.assertIn(
            "web-runtime-projection-history-pagination-browser.py",
            str(root / "scripts" / "smoke-web-runtime-projection-history-pagination-browser.py"),
        )

    def test_v022_web_runtime_real_network_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        script = (root / "scripts" / "smoke-web-runtime-real-network-browser.py").read_text(encoding="utf-8")

        for marker in (
            "new EventSource(`/stream?request_id=${encodeURIComponent(requestId)}${cursor}`)",
            "last_event_id=${encodeURIComponent(String(lastEventId))}",
            "void refreshRuntimeProjectionSnapshot('stream_lost').then(applied => {",
            "void refreshRuntimeProjectionSnapshot('sse_terminal');",
        ):
            self.assertIn(marker, console_source)

        for marker in (
            "class RealNetworkSmokeHandler(WebAssetHandler)",
            "QuietThreadingHTTPServer((\"127.0.0.1\", 0), handler)",
            "Content-Type\", \"text/event-stream; charset=utf-8\"",
            "def _write_sse_event(self, event_id: str, payload: dict[str, Any])",
            "id: {event_id}\\n",
            "data: {body}\\n\\n",
            "req-real-stable",
            "req-real-loss",
            "rn-loss-1",
            "last_event_id",
            "streamAttempts",
            "runtimeProjectionRequests",
            "legacy /api/history fallback was used",
            "page.add_init_script(f\"localStorage.setItem('cow_session_id'",
            "--artifact",
        ):
            self.assertIn(marker, script)
        self.assertNotIn("base_api_stub_script", script)
        self.assertNotIn("window.fetch =", script)
        self.assertNotIn("window.EventSource =", script)
        self.assertIn(
            "web-runtime-real-network-browser.py",
            str(root / "scripts" / "smoke-web-runtime-real-network-browser.py"),
        )

    def test_v022_web_image_jobs_browser_api_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        smoke_source = (root / "scripts" / "smoke-web-image-jobs-browser.py").read_text(encoding="utf-8")

        for marker in (
            "function _isRuntimeWebPath(url)",
            "pathname.startsWith('/uploads/')",
            "function runtimeProjectionImageJobs(projection)",
            "function runtimeProjectionImageJobSummary(projection)",
            "function runtimeProjectionRenderableText(projection, assistant)",
            "function runtimeProjectionHasRenderableContent(projection, assistant)",
            "runtimeProjectionImageJobSummary(projection)",
            "runtimeProjectionArtifacts(assistant, projection).length > 0",
            "if (!runtimeProjectionHasRenderableContent(projection, assistant)) return false;",
            "const content = runtimeProjectionRenderableText(projection, assistant);",
        ):
            self.assertIn(marker, console_source)

        for marker in (
            "Image generation completed with",
            "Image generation failed.",
            "Image generation cancelled.",
            "Image generation is running.",
        ):
            self.assertIn(marker, console_source)

        for marker in (
            "class ImageJobSmokeHandler(WebAssetHandler)",
            "web_channel.ImageJobsHandler().POST()",
            "web_channel.ImageJobsHandler().GET()",
            "web_channel.ImageJobActionHandler().POST(job_id)",
            "reset_image_job_service_for_tests(self.ledger)",
            "reset_run_event_ledger_for_tests(db_path)",
            "reset_run_ledger_for_tests(db_path)",
            "base_api_stub_script(extra_fetch_cases)",
            "path === '/api/image-jobs'",
            "window.__ecorexNativeFetch(input, init)",
            "dry_run: true",
            "synchronous: true",
            "include_events: true",
            "req-privateprompt",
            "image-job-privateprompt",
            "parallelism_policy_version === 'v1'",
            "requested_max_parallel === 2",
            "hard_max_parallel === 8",
            "effective_max_parallel === 2",
            "parallelism_clamped === false",
            "parallelism_clamp_reason === 'none'",
            "private identifiers leaked",
            "runtime /uploads path was incorrectly routed through file API",
            "runtime /uploads path should not use backend file API",
            "local POSIX artifact path did not use backend file API",
            "pure image job projection should not depend on assistant messages",
            "service reset did not force projection recovery",
            "const pureImageJobProjection = {",
            "renderRuntimeProjectionRequest(pureImageJobProjection, 'image_job_api_smoke')",
            "pure image job projection was not renderable",
            "Image generation completed",
            "image artifacts did not route through backend file API",
            "image preview did not use backend file API",
            "artifact-card-disabled",
            "image_job.progress",
            "image_job.artifact",
            "image_job.completed",
            "serverApiCalls",
        ):
            self.assertIn(marker, smoke_source)
        self.assertIn("smoke-web-image-jobs-browser.py", str(root / "scripts" / "smoke-web-image-jobs-browser.py"))

    def test_v022_image_jobs_provider_fallback_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_source = (root / "scripts" / "smoke-image-jobs-provider-fallback.py").read_text(encoding="utf-8")
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        projection_source = (root / "agent" / "protocol" / "runtime_projection.py").read_text(encoding="utf-8")

        for marker in (
            "ImageJobsHandler -> ImageJobService -> _image_job_skill_runner",
            "generate.py --stdin",
            "FakeImageApiServer",
            "FakeImageApiHandler.calls",
            "GENERATION_ROUTE_SUFFIX = \"/images/generations\"",
            "EDIT_ROUTE_SUFFIX = \"/images/edits\"",
            "OPENAI_API_BASE",
            "OPENAI_API_KEY",
            "reset_image_job_service_for_tests(ledger)",
            "reset_run_event_ledger_for_tests(db_path)",
            "web_channel.ImageJobsHandler().POST()",
            "\"synchronous\": True",
            "\"include_events\": True",
            "\"provider\": \"openai\"",
            "\"model\": \"gpt-image-2-pro\"",
            "\"image_url\": str(edit_input)",
            "\"fallback_used\": projected.get(\"fallback_used\")",
            "\"fallback_from_model\": projected.get(\"fallback_from_model\")",
            "\"fallback_to_model\": projected.get(\"fallback_to_model\")",
            "\"last_provider\": projected.get(\"last_provider\")",
            "\"last_model\": projected.get(\"last_model\")",
            "durable fallback progress missing",
            "leaked fake API key",
            "leaked provider raw error message",
        ):
            self.assertIn(marker, smoke_source)

        for marker in (
            "encoding=\"utf-8\"",
            "errors=\"replace\"",
            "\"fallback_used\": bool(model_fallback.get(\"used\"",
            "\"fallback_provider\": model_fallback.get(\"provider\")",
            "\"fallback_from_model\": model_fallback.get(\"from_model\")",
            "\"fallback_to_model\": model_fallback.get(\"to_model\")",
            "\"fallback_reason\": model_fallback.get(\"reason\")",
            "\"attempted_provider_count\": payload.get(\"attempted_provider_count\")",
        ):
            self.assertIn(marker, web_source)

        for marker in (
            "\"fallback_used\"",
            "\"fallback_provider\"",
            "\"fallback_from_model\"",
            "\"fallback_to_model\"",
            "\"fallback_reason\"",
            "job[\"fallback_used\"] = value",
            "job[key] = value",
            "job[f\"last_{key}\"] = value",
            "\"attempted_provider_count\"",
        ):
            self.assertIn(marker, projection_source)

    def test_v022_image_jobs_seven_scenario_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        smoke_source = (root / "scripts" / "smoke-image-jobs-seven-scenarios.py").read_text(encoding="utf-8")

        for marker in (
            "Seven-scenario smoke for v0.2.2 backend-led image jobs",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",
            "credential_source",
            "web_channel.ImageJobsHandler().POST()",
            "web_channel.ImageJobsHandler().GET()",
            "web_channel.ImageJobActionHandler().POST(job_id)",
            "scenario_external_generation",
            "scenario_fake_edit_fallback",
            "scenario_parallel_artifacts",
            "scenario_ocr_reuse",
            "scenario_projection_recovery",
            "scenario_cancel_running",
            "scenario_validation_no_events",
            "FakeImageApiServer",
            "FakeImageApiHandler.calls",
            "\"image_url\": str(edit_input)",
            "\"ocr_reuse\": True",
            "reset_image_job_service_for_tests(ledger)",
            "recovered_from_projection",
            "ImageJobCancelled",
            "validation failures wrote runtime events",
            "_assert_no_secret_leak",
            "dry-run-image-brief",
            "base_url_hash",
            "scenario_count",
        ):
            self.assertIn(marker, smoke_source)

        self.assertNotRegex(smoke_source, r"sk-[0-9a-f]{32,}")

    def test_v022_web_scheduler_projection_management_surface(self):
        root = Path(__file__).resolve().parents[1]
        chat_source = (root / "channel" / "web" / "chat.html").read_text(encoding="utf-8")
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")
        console_css = (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8")
        smoke_source = (root / "scripts" / "smoke-web-scheduler-browser.py").read_text(encoding="utf-8")

        self.assertIn('id="tasks-refresh-btn"', chat_source)
        self.assertIn('id="tasks-runtime-status"', chat_source)
        self.assertIn('data-i18n="tasks_refresh"', chat_source)
        self.assertIn("tasks_refresh", console_source)

        self.assertIn("function loadTasksViewLegacyEnabledOnly()", console_source)
        self.assertIn("function renderSchedulerRuntime(projection)", console_source)
        self.assertIn("function renderSchedulerProjection(projection)", console_source)
        self.assertIn("function readSchedulerTaskForm(taskId, task)", console_source)
        self.assertIn("function schedulerRequest(body)", console_source)
        self.assertIn("function schedulerPayloadHasProjection(data)", console_source)
        self.assertIn("schedulerPayloadHasProjection(data)", console_source)
        self.assertLess(
            console_source.find("function loadTasksViewLegacyEnabledOnly()"),
            console_source.rfind("function loadTasksView(force)"),
        )
        self.assertLess(
            console_source.find("function readSchedulerTaskForm(taskId, task)"),
            console_source.rfind("function renderSchedulerProjection(projection)"),
        )

        for marker in (
            "projection.counts",
            "projection.taskStore",
            "projection.canModify",
            "projection.modifyBlockingReason",
            "projection.blockingReason",
            "schedulerProjection.tasks",
            "task.scheduleDescription",
            "task.nextRunAt",
            "task.lastRunAt",
            "task.lastError",
            "task.action",
        ):
            self.assertIn(marker, console_source)

        for marker in (
            "fetch('/api/scheduler').then",
            "fetch('/api/scheduler', {",
            "method: 'POST'",
            "payload.schedule_type",
            "payload.schedule_value",
            "payload.taskDescription",
            "payload.content",
            "data-scheduler-action=\"start\"",
            "data-scheduler-action=\"stop\"",
            "data-scheduler-action=\"refresh\"",
            "data-scheduler-action=\"save\"",
            "data-scheduler-action=\"delete\"",
            "task.enabled ? 'disable' : 'enable'",
        ):
            self.assertIn(marker, console_source)

        active_scheduler_source = console_source[console_source.find("function schedulerString(value)"):]
        self.assertIn("scheduler-task-editor", active_scheduler_source)
        self.assertIn("schedulerScheduleTypeOptions(scheduleType)", active_scheduler_source)
        self.assertIn("readSchedulerTaskForm(taskId, task)", active_scheduler_source)
        self.assertNotIn("window.prompt", active_scheduler_source)

        for marker in (
            ".scheduler-runtime-panel",
            ".scheduler-task-card",
            ".scheduler-runtime-main",
            ".scheduler-task-grid",
            ".scheduler-task-editor",
            ".scheduler-btn-primary",
            ".scheduler-btn-danger",
            "@media (max-width: 720px)",
        ):
            self.assertIn(marker, console_css)

        for marker in (
            "from web_smoke_support import ROOT, base_api_stub_script, web_asset_server",
            "function cloneProjection()",
            "path === '/api/scheduler'",
            "window.__ecorexSmoke.scheduler",
            "renderSchedulerProjection === 'function'",
            "loadTasksView === 'function'",
            "navigateTo('tasks')",
            "loadTasksView(true)",
            "scheduler-task-card[data-task-id=\"task-daily\"]",
            "scheduler-task-card[data-task-id=\"task-disabled\"]",
            "data-scheduler-action=\"save\"",
            "data-scheduler-action=\"disable\"",
            "data-scheduler-action=\"start\"",
            "failNextStart",
            "scheduler start failed",
            "unexpected scheduler payload key",
            "private receiver leaked into scheduler UI",
            "secret token leaked into scheduler UI",
            "private-open-id",
            "sk-test-secret",
        ):
            self.assertIn(marker, smoke_source)

    def test_v022_web_has_no_run_center_user_surface(self):
        root = Path(__file__).resolve().parents[1]
        frontend_sources = {
            "channel/web/chat.html": (root / "channel" / "web" / "chat.html").read_text(encoding="utf-8"),
            "channel/web/static/js/console.js": (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8"),
            "channel/web/static/css/console.css": (root / "channel" / "web" / "static" / "css" / "console.css").read_text(encoding="utf-8"),
        }
        forbidden_markers = (
            "Run Center",
            "RUNCENTER",
            "runCenter",
            "RUN_CENTER",
            "run-center",
            "运行中心",
        )
        for path, source in frontend_sources.items():
            for marker in forbidden_markers:
                self.assertNotIn(marker, source, f"{path} exposes {marker!r} to ordinary Web users")

        web_channel_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        self.assertIn("def attach_run_center_policy", web_channel_source)
        self.assertIn("RuntimeProjectionHandler", web_channel_source)

    def test_v022_web_run_center_hidden_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "smoke-web-run-center-hidden-browser.py").read_text(encoding="utf-8")

        self.assertIn("web_asset_server", script)
        self.assertIn("base_api_stub_script", script)
        self.assertIn("Run Center", script)
        self.assertIn("runCenter", script)
        self.assertIn("RUN_CENTER", script)
        self.assertIn("run-center", script)
        self.assertIn("运行中心", script)
        self.assertIn("document.body.innerText", script)
        self.assertIn("document.documentElement.outerHTML", script)
        self.assertIn("[class*=\"run-center\" i]", script)
        self.assertIn("[aria-label*=\"Run Center\" i]", script)
        self.assertIn("buttonLeaks", script)
        self.assertIn("Run Center visible text leaked", script)
        self.assertIn("Run Center DOM/source marker leaked", script)
        self.assertIn("Run Center selector surfaced", script)
        self.assertIn("web-run-center-hidden-browser-smoke.png", script)

    def test_v022_web_ui_polish_browser_smoke_harness_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "smoke-web-ui-polish-browser.py").read_text(encoding="utf-8")

        self.assertIn("from web_smoke_support import ROOT, base_api_stub_script, web_asset_server", script)
        self.assertIn("from playwright.sync_api import sync_playwright", script)
        self.assertIn("page.add_init_script(base_api_stub_script())", script)
        self.assertIn("typeof renderMarkdown === 'function'", script)
        self.assertIn("typeof createBotMessageEl === 'function'", script)
        self.assertIn("typeof _buildArtifactHtml === 'function'", script)
        self.assertIn("createBotMessageEl(finalFixture", script)
        self.assertIn("window.copyToClipboard = async (text)", script)
        self.assertIn("window.copyImageToClipboard = async (url)", script)
        self.assertIn(".copy-msg-btn", script)
        self.assertIn(".code-copy-btn", script)
        self.assertIn("renderAnswerHtml(longFixture)", script)
        self.assertIn("[data-long-answer-toggle=\"expand\"]", script)
        self.assertIn("renderThinkingHtml(thinkingFixture)", script)
        self.assertIn(".thinking-full h2", script)
        self.assertIn("_buildArtifactHtml", script)
        self.assertIn(".artifact-copy-image", script)
        self.assertIn(".artifact-menu-btn", script)
        self.assertIn(".artifact-copy-link", script)
        self.assertIn("image:/assets/icon.png", script)
        self.assertIn("PointerEvent('pointerdown'", script)
        self.assertIn(".menu-group[data-group=\"manage\"]", script)
        self.assertIn("attach-btn", script)
        self.assertIn("Run Center", script)
        self.assertIn("message copy did not write raw Markdown", script)
        self.assertIn("artifact image copy did not write image payload", script)
        self.assertIn("artifact action menu did not open", script)
        self.assertIn("thinking disclosure did not toggle", script)
        self.assertIn("ordinary Web UI leaked Run Center text", script)
        self.assertIn("parser.add_argument(\"--screenshot\"", script)

    def test_v020_webui_install_pages_hide_manifest_and_harden_mac_retry(self):
        root = Path(__file__).resolve().parents[1]
        admin_source = (root / "deploy" / "ecorex-site" / "admin" / "index.html").read_text(encoding="utf-8")
        mac_installer = (root / "deploy" / "ecorex-site" / "install-webui.sh").read_text(encoding="utf-8")
        win_installer = (root / "deploy" / "ecorex-site" / "install-webui.ps1").read_text(encoding="utf-8")
        package_source = (root / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")

        self.assertNotIn("查看 manifest", admin_source)
        self.assertNotIn("manifest.json\">", admin_source)
        self.assertNotIn("resume_args", mac_installer)
        self.assertIn("local curl_args=", mac_installer)
        self.assertIn('curl "${curl_args[@]}" "$url" -o "$partial"', mac_installer)
        self.assertIn("EcoreX WebUI installer script: 0.2.4", mac_installer)
        self.assertIn("EcoreX WebUI manifest version:", mac_installer)
        self.assertIn("EcoreX WebUI installer script: 0.2.4", win_installer)
        self.assertIn("EcoreX WebUI manifest version:", win_installer)
        self.assertIn("EcoreX WebUI package installer:", package_source)
        self.assertIn("Generated macOS WebUI installer still contains retired resume_args code", package_source)
        self.assertLess(
            package_source.index('write_desktop_shortcuts "$URL"'),
            package_source.index('open_browser "$URL"')
        )

    def test_v021_web_deploy_paths_and_client_base_are_mvdcm_ready(self):
        root = Path(__file__).resolve().parents[1]
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        nginx_source = (root / "deploy" / "ecorex-site" / "nginx" / "ecorex-agent.conf.example").read_text(encoding="utf-8")
        web_nginx_source = (root / "deploy" / "ecorex-site" / "nginx" / "ecorex-web.conf.example").read_text(encoding="utf-8")
        web_caddy_source = (root / "deploy" / "ecorex-site" / "caddy" / "ecorex-web.routes.caddy").read_text(encoding="utf-8")
        installer_source = (root / "scripts" / "install-ecorex-web.sh").read_text(encoding="utf-8")
        site_source = (root / "deploy" / "ecorex-site" / "site.js").read_text(encoding="utf-8")

        self.assertIn('var DEFAULT_WEB_CLIENT_KEY = "ecorex-web-v0.2.2-web.1"', web_source)
        self.assertIn("ecorex-web-v0.2.1-web.1", web_source)
        self.assertIn("https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json", web_source)
        self.assertIn("public_base = str(os.environ.get(\"ECOREX_WEB_PUBLIC_BASE_URL\")", web_source)
        self.assertIn("or (f\"{public_base}/client\" if public_base else \"\")", web_source)
        self.assertIn("EcoreX-WebUI/0.2.2", web_source)
        self.assertIn("function runtimePath(path)", web_source)
        self.assertIn('fetch(runtimePath("/api/knowledge/read?path=" + encodeURIComponent(relPath))', web_source)
        self.assertIn("location ^~ /ecorex-agent/assets/", nginx_source)
        self.assertIn("proxy_request_buffering off;", nginx_source)
        self.assertIn("client_max_body_size 256m;", nginx_source)
        self.assertIn("location ^~ /ecorex-agent/client/", web_nginx_source)
        self.assertIn("location = /ecorex-agent/message", web_nginx_source)
        self.assertIn("location = /ecorex-agent/upload", web_nginx_source)
        self.assertIn("proxy_request_buffering off;", web_nginx_source)
        self.assertIn("client_max_body_size 256m;", web_nginx_source)
        self.assertIn("handle /ecorex-agent/client/*", web_caddy_source)
        self.assertIn("read_timeout 1200s", web_caddy_source)
        self.assertIn("write_timeout 1200s", web_caddy_source)
        self.assertIn('VERSION="${VERSION:-0.2.4}"', installer_source)
        self.assertIn("https://mvdcm.ecoremedia.net/ecorex-agent/downloads", installer_source)
        self.assertIn("ECOREX_WEB_CLIENT_BASE=$client_base", installer_source)
        self.assertIn("ECOREX_TOOL_EXECUTION_LEASE_SECONDS=900", installer_source)
        self.assertIn("ECOREX_TOOL_EXECUTION_MAX_SECONDS=5400", installer_source)
        self.assertIn("ECOREX_BASH_MAX_TIMEOUT_SECONDS=7200", installer_source)
        self.assertIn("wait_for_webui \"$local_url\"", installer_source)
        self.assertIn("Public proxy smoke did not pass yet", installer_source)
        self.assertIn("mvdcm.ecoremedia.net/ecorex-agent/install-webui", site_source)

    def test_v020_release_manifest_promotion_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        script_source = (root / "scripts" / "update-ecorex-desktop-release-manifest.ps1").read_text(encoding="utf-8")
        package_source = (root / "desktop" / "package.json").read_text(encoding="utf-8")

        self.assertIn("[switch]$PromoteVersion", script_source)
        self.assertIn("Pass -PromoteVersion to intentionally advance the public manifest.", script_source)
        self.assertIn('Set-ArtifactProperty $manifest "version" $Version', script_source)
        self.assertIn('Set-ArtifactProperty $manifest "updatedAt" $UpdatedAt', script_source)
        self.assertIn("EcoreX v$Version WebUI-first release", script_source)
        self.assertIn("-PromoteVersion -WebUiWindowsPath", package_source)

    def test_v020_webui_local_auth_falls_back_without_admin_client(self):
        root = Path(__file__).resolve().parents[1]
        web_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        install_source = (root / "scripts" / "install-ecorex-public-release.sh").read_text(encoding="utf-8")

        self.assertIn('if (!auth.auth_required) return webSession(false, true, null, true);', web_source)
        self.assertIn("invalid client key|client key", web_source)
        self.assertIn("if (isMissingClientBridge(error)) return webSession(Boolean(auth.auth_required), true, authIdentity, true);", web_source)
        self.assertIn('code: modelReady.code || "MODEL_CONFIG_UNAVAILABLE"', web_source)
        self.assertIn("err.status = response.status;", web_source)
        self.assertIn('modelConfigNotReady("ENTERPRISE_LOGIN_REQUIRED"', web_source)
        self.assertIn('modelConfigNotReady("ENTERPRISE_POLICY_UNAVAILABLE"', web_source)
        self.assertIn("configuredProviders", web_source)
        self.assertIn("isModelConfigSendError", app_source)
        self.assertIn('label: "重新登录"', app_source)
        self.assertIn("restoreUnacceptedDraft(message, result)", app_source)
        self.assertIn("COMPOSE_ADMIN_CONTEXT", install_source)
        self.assertIn("--force-recreate", install_source)
        self.assertIn('"$COMPOSE_SERVICE"', install_source)
        self.assertNotIn("当前网页版没有可用模型配置", web_source)
        self.assertNotIn("请先登录企业账号，或在设置 > 模型中配置可用的 API Key", web_source)

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
                self.assertNotIn("session_id", stale[0])
                self.assertTrue(stale[0]["sessionHash"])
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
            stale = snapshot["staleLocks"]
            self.assertEqual(len(stale), 1)
            self.assertNotIn("session_id", stale[0])
            self.assertTrue(stale[0]["sessionHash"])
            self.assertTrue(stale[0]["stale"])
            self.assertTrue(stale[0]["alive"])
            self.assertFalse(stale[0]["dead_owner"])
            self.assertFalse(stale[0]["removed"])
            self.assertTrue(lock.path.exists())
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "running")
            self.assertIsNone(final["terminal_at"])

    def test_active_request_snapshot_interrupts_stale_orphan_message_run(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        channel.request_to_session = {}
        channel.sse_queues = {}
        channel.sse_events = {}
        request_id = "req-v020-stale-orphan"
        session_id = "session-v020-stale-orphan"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="tool_running", status="running")
            future = time.time() + 600

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace), patch.object(
                web_channel, "conf", return_value={"web_active_run_stale_seconds": 30}
            ), patch.object(web_channel.time, "time", return_value=future):
                snapshot = channel.active_requests_snapshot()

            self.assertEqual(snapshot["requests"], [])
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "interrupted")
            self.assertEqual(final["terminal_reason"], "stale_active_recovered")
            self.assertEqual(final["error_code"], "STALE_ACTIVE_RUN")

    def test_backpressure_snapshot_releases_stale_orphan_message_run(self):
        from agent.protocol import reset_run_ledger_for_tests
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        channel.request_to_session = {}
        channel.sse_queues = {}
        channel.sse_events = {}
        request_id = "req-v020-stale-backpressure"
        session_id = "session-v020-stale-backpressure"
        with tempfile.TemporaryDirectory() as workspace:
            ledger = reset_run_ledger_for_tests(Path(workspace) / "run-ledger.db")
            ledger.create_run(request_id, session_id, phase="tool_running", status="running")
            future = time.time() + 600

            with patch.object(web_channel, "_get_workspace_root", return_value=workspace), patch.object(
                web_channel, "conf", return_value={
                    "web_active_run_stale_seconds": 30,
                    "web_max_active_requests": 1,
                    "web_max_active_requests_per_session": 1,
                }
            ), patch.object(web_channel.time, "time", return_value=future):
                rejection = channel._backpressure_rejection_payload(session_id)

            self.assertIsNone(rejection)
            final = ledger.get_run(request_id)
            self.assertEqual(final["status"], "interrupted")
            self.assertEqual(final["terminal_reason"], "stale_active_recovered")

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
            self.assertIn("Message request failed before worker start.", result["message"])
            self.assertIn("Details redacted", result["message"])
            self.assertEqual(result["errorType"], "RuntimeError")
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
            self.assertIn("Message request failed before worker start.", result["message"])
            self.assertIn("Details redacted", result["message"])
            self.assertEqual(result["errorType"], "RuntimeError")
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
                self.assertIn("Worker failed before producing a response.", event["content"])
                self.assertIn("Details redacted", event["content"])
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
                self.assertIn("Worker failed before producing a response.", event["content"])
                self.assertIn("Details redacted", event["content"])
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
        heartbeat_chunk = next(first_stream)
        self.assertIn(b'"type": "heartbeat"', heartbeat_chunk)
        self.assertIn(request_id.encode("utf-8"), heartbeat_chunk)

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

        self.assertIn(b'"type": "heartbeat"', keepalive)
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

    def test_tool_schema_budget_inherits_browser_intent_after_login_confirmation(self):
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
            tools=[tool("read"), tool("bash"), tool("browser"), tool("host_diagnostics")],
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "打开小红书网页版 搜索圣都装饰"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "页面需要登录。"}]},
                {"role": "user", "content": [{"type": "text", "text": "已登录"}]},
            ],
        )

        selected, budget = executor._select_tools_for_schema()

        self.assertIn("browser", selected)
        self.assertIn("browser", budget["intent_groups"])
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

    def test_cdp_raw_bash_reroute_points_to_browser_tool_when_available(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        def tool(name):
            return types.SimpleNamespace(name=name)

        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("bash"), tool("browser"), tool("host_diagnostics")],
        )

        reason = executor._external_capability_reroute(
            "bash",
            {"command": "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9222/json')\""},
        )

        self.assertIn("Use the `browser` tool directly", reason)
        self.assertIn("Do not read", reason)
        self.assertNotIn("Call `host_diagnostics` first", reason)

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
        from config import chrome_devtools_mcp_args

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
                        "args": chrome_devtools_mcp_args(),
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
        from config import chrome_devtools_mcp_args

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

                remote_endpoint = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": chrome_devtools_mcp_args("http://192.168.1.10:9222"),
                        "trusted_default_chrome_devtools": True,
                    },
                )
                self.assertFalse(remote_endpoint["allowed"])

                unknown_flag = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": chrome_devtools_mcp_args() + ["--chromeArg", "--disable-web-security"],
                        "trusted_default_chrome_devtools": True,
                    },
                )
                self.assertFalse(unknown_flag["allowed"])

                missing_privacy_flag = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": [
                            item
                            for item in chrome_devtools_mcp_args()
                            if item not in {"--no-performance-crux", "--redactNetworkHeaders"}
                        ],
                        "trusted_default_chrome_devtools": True,
                    },
                )
                self.assertFalse(missing_privacy_flag["allowed"])

                broker.set_mode("read-only")
                read_only = broker.authorize_noninteractive(
                    "browser",
                    {
                        "server": "chrome-devtools",
                        "command": "npx",
                        "args": chrome_devtools_mcp_args(),
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
        self.assertFalse(remote_endpoint["allowed"])
        self.assertFalse(unknown_flag["allowed"])
        self.assertFalse(missing_privacy_flag["allowed"])
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
                imagegen_decision = broker.authorize(
                    "imagegen",
                    "tool-imagegen",
                    {"prompt": "edit image", "image_url": "C:/secret.png", "output_dir": "C:/out"},
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
        self.assertFalse(imagegen_decision["allowed"])

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
        from agent.tools.imagegen.imagegen import ImageGenTool
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

                imagegen_result = ImageGenTool().execute({
                    "prompt": "make a small test image",
                    "output_dir": os.path.join(workspace, "generated"),
                    "timeout": 30,
                })
                self.assertEqual(imagegen_result.status, "error")
                self.assertIn("output directory blocked", str(imagegen_result.result))
                self.assertFalse(os.path.exists(os.path.join(workspace, "generated")))

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
        from agent.tools.imagegen.imagegen import ImageGenTool
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
            outside_image = os.path.join(root, "outside.png")
            with open(outside_image, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

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
                imagegen_outside = ImageGenTool().execute({
                    "prompt": "edit image",
                    "image_url": outside_image,
                    "output_dir": os.path.join(workspace, "generated"),
                    "timeout": 30,
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
        self.assertEqual(imagegen_outside.status, "error")
        self.assertIn("input read blocked", str(imagegen_outside.result))
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

    def test_openai_image_provider_falls_back_from_gpt_image_2_pro_to_standard_model(self):
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
            provider.generate(
                "orange x",
                quality="low",
                size="1024x1024",
                output_format="png",
                output_dir=output_dir,
            )

        self.assertEqual(provider.model, "gpt-image-2")
        self.assertEqual(urls, ["https://api.openai.com/v1/images/generations"] * 2)
        self.assertEqual([payload["model"] for payload in payloads], ["gpt-image-2-pro", "gpt-image-2"])
        self.assertEqual(provider.model_fallback["from_model"], "gpt-image-2-pro")
        self.assertEqual(provider.model_fallback["to_model"], "gpt-image-2")
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

    def test_v023_fixed_xhs_skill_removed_in_favor_of_skill_creator(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "skills" / "create-xiaohongshu-note").exists())
        skill_creator = (root / "skills" / "skill-creator" / "SKILL.md").read_text(encoding="utf-8")
        init_skill = root / "skills" / "skill-creator" / "scripts" / "init_skill.py"
        quick_validate = root / "skills" / "skill-creator" / "scripts" / "quick_validate.py"
        self.assertIn("name: skill-creator", skill_creator)
        self.assertIn("Skill Creation Process", skill_creator)
        self.assertTrue(init_skill.exists())
        self.assertTrue(quick_validate.exists())

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
        skill_creator_md = (root / "skills" / "skill-creator" / "SKILL.md").read_text(encoding="utf-8")
        skill_creator_init = root / "skills" / "skill-creator" / "scripts" / "init_skill.py"
        skill_creator_validate = root / "skills" / "skill-creator" / "scripts" / "quick_validate.py"
        manager_py = (root / "agent" / "skills" / "manager.py").read_text(encoding="utf-8")
        enterprise_policy_ts = (root / "desktop" / "electron" / "enterprisePolicy.ts").read_text(encoding="utf-8")
        telemetry_ts = (root / "desktop" / "electron" / "telemetry.ts").read_text(encoding="utf-8")
        stage_runtime_win = (root / "desktop" / "scripts" / "stage-runtime-win.ps1").read_text(encoding="utf-8")
        web_channel_py = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', generate_py)
        self.assertNotIn('FALLBACK_MODEL = "gpt-image-2"', generate_py)
        self.assertIn("OpenAI default mode starts with `gpt-image-2-pro`", skill_md)
        self.assertIn("model_fallback", skill_md)
        self.assertIn('GPT_IMAGE_STANDARD_MODEL = "gpt-image-2"', generate_py)
        self.assertIn("or OpenAIProvider.DEFAULT_MODEL", generate_py)
        self.assertIn("LinkAI default model follows EcoreX's OpenAI image default", generate_py)
        self.assertNotIn('("linkai",    "image-2-pro")', web_channel_py)
        self.assertIn('("linkai",    "gpt-image-2-pro")', web_channel_py)
        self.assertIn('"linkai": [\n            "gpt-image-2-pro"', web_channel_py)
        self.assertIn('Do not create final images by coding HTML/canvas/SVG/Pillow layouts', skill_md)
        self.assertIn("legacy `image-2-pro` input is normalized", skill_md)
        self.assertFalse((root / "skills" / "create-xiaohongshu-note").exists())
        self.assertIn("name: skill-creator", skill_creator_md)
        self.assertIn("Skill Creation Process", skill_creator_md)
        self.assertTrue(skill_creator_init.exists())
        self.assertTrue(skill_creator_validate.exists())
        self.assertIn('DEFAULT_MODEL = "gpt-image-2-pro"', manager_py)
        self.assertNotIn("create-xiaohongshu-note", manager_py)
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
            'if (!env.DEV && env.VITE_ECOREX_RUN_CENTER !== "1") return false;',
            'params.get("ecorexRunCenter") === "1"',
            'window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) === "1"',
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
        self.assertNotIn('params.get("runCenter") !== "0"', app_source)
        self.assertNotIn('window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) !== "0"', app_source)

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
            "响应记录暂时没有接上",
            'label: "stream_replay_gap"',
            "requestedLastEventId",
            "retainedFromEventId",
            "nextEventId",
        ]:
            self.assertIn(marker, helper_source)
        self.assertGreaterEqual(app_source.count("handleReplayGapStreamItem("), 3)
        self.assertGreaterEqual(app_source.count("if (isReplayGapStreamItem(item))"), 2)

    def test_v020_send_attempt_uses_user_facing_copy_and_preserves_live_placeholder(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        helper_start = app_source.index("function historyHasFinalAssistantAfterUserTurn")
        helper_end = app_source.index("function mergeHistoryAndLocalRequestMessage", helper_start)
        helper_source = app_source[helper_start:helper_end]
        merge_start = app_source.index("function mergeHistoryWithLocalMessages")
        merge_end = app_source.index("function plainTextForMessage", merge_start)
        merge_source = app_source[merge_start:merge_end]
        recovery_start = app_source.index("function renderRecoveryActions")
        recovery_end = app_source.index("if (!authChecked)", recovery_start)
        recovery_source = app_source[recovery_start:recovery_end]

        self.assertIn("function historyHasTerminalAssistantForPending", app_source)
        self.assertIn("function historyHasFinalAssistantAfterUserTurn", app_source)
        self.assertIn("for (let index = userIndex + 1; index < history.length; index += 1)", helper_source)
        self.assertIn('if (message.role === "user") return false;', helper_source)
        self.assertIn('if (message.role === "assistant" && isTerminalAssistantMessage(message)) return true;', helper_source)
        self.assertIn("historyHasTerminalAssistantForPending(history, message)", merge_source)
        self.assertIn("historyUserIndicesByContentKey", merge_source)
        self.assertIn("localUserTotalsByContentKey", merge_source)
        self.assertIn("localUserSeenByContentKey", merge_source)
        self.assertIn("firstComparableHistoryIndex = Math.max(0, historyIndices.length - localTotal)", merge_source)
        self.assertIn("skipPendingAssistantAfterMatchedUser = historyHasFinalAssistantAfterUserTurn(history, matchedHistoryUserIndex);", merge_source)
        self.assertNotIn("historyHasFinalAssistantAfterUserTurn(history, message)", merge_source)
        self.assertNotIn("const historyHasFinalAssistant", merge_source)
        self.assertNotIn('skipPendingAssistantAfterMatchedUser = message.role === "user";', merge_source)
        self.assertNotIn('skipPendingAssistantAfterMatchedUser = historyHasFinalAssistant && message.role === "user";', merge_source)
        self.assertNotIn("Sending while stopping the previous response", app_source)
        self.assertNotIn("Response stalled; reconnecting", app_source)
        self.assertIn("visibleOutputSettled?: boolean", app_source)
        self.assertIn("&& !message.visibleOutputSettled", app_source)
        self.assertGreaterEqual(app_source.count("visibleOutputSettled: message.visibleOutputSettled"), 2)
        self.assertIn("function settleVisibleStreamOutput", app_source)
        self.assertIn("options: { awaitingStreamDone?: boolean }", app_source)
        self.assertIn("visibleOutputSettled: options.awaitingStreamDone ?? true", app_source)
        self.assertIn("recovery: undefined", app_source)
        self.assertGreaterEqual(app_source.count("const next = appendArtifact(message, item, false);"), 2)
        self.assertGreaterEqual(app_source.count("return next === message ? message : settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail });"), 2)
        self.assertGreaterEqual(app_source.count('const visibleTerminalOutput = item.type !== "voice_attach" || terminalVoiceAttach;'), 2)
        self.assertGreaterEqual(app_source.count("? settleVisibleStreamOutput(next, { awaitingStreamDone: !postDoneTail && !terminalVoiceAttach })"), 2)
        self.assertIn("if (!message.pending && !message.visibleOutputSettled) return message;", app_source)
        self.assertIn("message.visibleOutputSettled && isTransientPhaseContent(phaseContent)", app_source)
        self.assertIn("const canReconnect = Boolean(requestId && (message.pending || message.visibleOutputSettled));", recovery_source)
        for marker in [
            "正在发送新消息",
            "正在切换到这条新消息",
            "正在准备响应",
            "重新连接",
            "恢复记录",
            "准备重试",
            "诊断信息",
        ]:
            self.assertIn(marker, recovery_source)

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
            'if (!env.DEV && env.VITE_ECOREX_RUN_CENTER !== "1") return false;',
            'params.get("ecorexRunCenter") === "1"',
            'window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) === "1"',
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
        self.assertNotIn('params.get("runCenter") !== "0"', app_source)
        self.assertNotIn('window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) !== "0"', app_source)
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
            "网络连接中断。为避免重复执行",
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
                "OpenAI default mode starts with `gpt-image-2-pro`\nmodel_fallback\n\"output_format\"\n"
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
                "OpenAI default mode starts with `gpt-image-2-pro`\nmodel_fallback\n\"output_format\"\n"
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

    def test_v023_xhs_skill_is_not_managed_builtin_refresh_target(self):
        from agent.skills.manager import MANAGED_BUILTIN_REFRESH_MARKERS, SkillManager

        with tempfile.TemporaryDirectory() as root:
            builtin = Path(root) / "builtin"
            custom = Path(root) / "custom"
            custom_skill = custom / "create-xiaohongshu-note"
            (custom_skill / "scripts").mkdir(parents=True)
            (custom_skill / "SKILL.md").write_text(
                "---\nname: create-xiaohongshu-note\ndescription: Create XHS note\n---\n",
                encoding="utf-8",
            )
            (custom_skill / "scripts" / "generate_cover_image.py").write_text(
                "legacy user-owned skill\n",
                encoding="utf-8",
            )

            manager = SkillManager(builtin_dir=str(builtin), custom_dir=str(custom))

            self.assertNotIn("create-xiaohongshu-note", MANAGED_BUILTIN_REFRESH_MARKERS)
            self.assertIn("create-xiaohongshu-note", manager.skills)
            preserved = (custom_skill / "scripts" / "generate_cover_image.py").read_text(encoding="utf-8")
            self.assertIn("legacy user-owned skill", preserved)
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

    def test_scheduler_imports_without_external_croniter_and_supports_daily_cron(self):
        from datetime import datetime

        from agent.tools.scheduler.cron_compat import _FallbackCroniter
        from agent.tools.scheduler.scheduler_tool import SchedulerTool

        next_run = _FallbackCroniter("30 9 * * *", datetime(2026, 6, 24, 8, 0)).get_next(datetime)

        self.assertEqual(next_run, datetime(2026, 6, 24, 9, 30))
        self.assertEqual(SchedulerTool({}).name, "scheduler")

    def test_scheduler_projection_reports_uninitialized_tasks_and_masks_secrets(self):
        from common.ecorex_tool_permissions import get_tool_permission_broker

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        old_croniter = sys.modules.get("croniter")
        sys.modules["croniter"] = fake_croniter
        scheduler_projection_module = importlib.import_module("agent.tools.scheduler.projection")
        scheduler_task_store_module = importlib.import_module("agent.tools.scheduler.task_store")
        scheduler_integration_module = importlib.import_module("agent.tools.scheduler.integration")
        scheduler_projection = scheduler_projection_module.scheduler_projection
        TaskStore = scheduler_task_store_module.TaskStore

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            get_tool_permission_broker().set_mode("full-access")
            store = TaskStore(os.path.join(workspace, "scheduler", "tasks.json"))
            store.add_task({
                "id": "task-visible",
                "name": "daily report",
                "enabled": True,
                "created_at": "2026-06-24T09:00:00",
                "updated_at": "2026-06-24T09:00:00",
                "schedule": {"type": "cron", "expression": "30 9 * * *"},
                "action": {
                    "type": "tool_call",
                    "tool_name": "feishu_cli",
                    "tool_params": {
                        "api_key": "sk-test-secret-1234567890",
                        "query": "visible",
                        "content": "private scheduled prompt body",
                    },
                    "result_prefix": "private result prefix body",
                    "receiver": "private-open-id",
                },
                "next_run_at": "2026-06-25T09:30:00",
                "last_error": "provider failed for receiver private-open-id with token sk-test-secret-1234567890",
                "last_error_at": "2026-06-24T09:30:01",
            })
            try:
                with patch.object(scheduler_projection_module, "conf", return_value={
                    "agent_workspace": workspace,
                    "scheduler_enabled": True,
                }), \
                    patch.object(scheduler_integration_module, "get_task_store", return_value=None), \
                    patch.object(scheduler_integration_module, "get_scheduler_service", return_value=None):
                    projection = scheduler_projection(workspace)
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data
                if old_croniter is None:
                    sys.modules.pop("croniter", None)
                else:
                    sys.modules["croniter"] = old_croniter

        self.assertTrue(projection["enabled"])
        self.assertFalse(projection["initialized"])
        self.assertEqual(projection["serviceStatus"], "enabled_not_initialized")
        self.assertEqual(projection["taskCount"], 1)
        task = projection["tasks"][0]
        self.assertEqual(task["scheduleDescription"], "daily at 09:30")
        self.assertEqual(task["state"], "error")
        self.assertEqual(task["action"]["toolParams"]["api_key"], "[redacted]")
        self.assertEqual(task["action"]["toolParams"]["query"], "visible")
        self.assertEqual(task["action"]["toolParams"]["content"], "[redacted-content]")
        self.assertEqual(task["action"]["resultPrefixPreview"], "[redacted-content]")
        self.assertTrue(task["action"]["resultPrefixHash"])
        self.assertIn("details redacted", task["lastError"])
        self.assertTrue(task["lastErrorHash"])
        self.assertNotIn("private-open-id", json.dumps(task, ensure_ascii=False))
        self.assertNotIn("sk-test-secret", json.dumps(task, ensure_ascii=False))
        self.assertNotIn("private scheduled prompt body", json.dumps(task, ensure_ascii=False))
        self.assertNotIn("private result prefix body", json.dumps(task, ensure_ascii=False))

    def test_scheduler_projection_redacts_action_bodies_but_store_keeps_raw(self):
        scheduler_projection_module = importlib.import_module("agent.tools.scheduler.projection")
        scheduler_task_store_module = importlib.import_module("agent.tools.scheduler.task_store")
        TaskStore = scheduler_task_store_module.TaskStore

        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            store = TaskStore(os.path.join(workspace, "scheduler", "tasks.json"))
            store.add_task({
                "id": "task-agent-private",
                "name": "agent private",
                "enabled": True,
                "schedule": {"type": "interval", "seconds": 3600},
                "action": {
                    "type": "agent_task",
                    "task_description": "private agent body with sk-agent-secret-123456",
                    "receiver": "private-open-id",
                },
            })
            store.add_task({
                "id": "task-message-private",
                "name": "message private",
                "enabled": True,
                "schedule": {"type": "interval", "seconds": 3600},
                "action": {
                    "type": "send_message",
                    "content": "private fixed body with xoxb-message-secret-123456",
                    "receiver": "private-chat-id",
                },
            })
            with patch.object(scheduler_projection_module, "conf", return_value={
                "agent_workspace": workspace,
                "scheduler_enabled": False,
            }):
                projection = scheduler_projection_module.scheduler_projection(workspace)

            saved_agent = store.get_task("task-agent-private")
            saved_message = store.get_task("task-message-private")

        serialized_projection = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("private agent body", serialized_projection)
        self.assertNotIn("private fixed body", serialized_projection)
        self.assertNotIn("sk-agent-secret", serialized_projection)
        self.assertNotIn("xoxb-message-secret", serialized_projection)
        by_id = {task["id"]: task for task in projection["tasks"]}
        agent_action = by_id["task-agent-private"]["action"]
        message_action = by_id["task-message-private"]["action"]
        self.assertEqual(agent_action["taskDescriptionPreview"], "[redacted-content]")
        self.assertTrue(agent_action["taskDescriptionHash"])
        self.assertGreater(agent_action["taskDescriptionLength"], 0)
        self.assertEqual(message_action["contentPreview"], "[redacted-content]")
        self.assertTrue(message_action["contentHash"])
        self.assertGreater(message_action["contentLength"], 0)
        self.assertEqual(saved_agent["action"]["task_description"], "private agent body with sk-agent-secret-123456")
        self.assertEqual(saved_message["action"]["content"], "private fixed body with xoxb-message-secret-123456")

    def test_scheduler_web_handler_updates_task_and_returns_projection(self):
        from common.ecorex_tool_permissions import get_tool_permission_broker

        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        old_croniter = sys.modules.get("croniter")
        sys.modules["croniter"] = fake_croniter
        scheduler_task_store_module = importlib.import_module("agent.tools.scheduler.task_store")
        scheduler_projection_module = importlib.import_module("agent.tools.scheduler.projection")
        TaskStore = scheduler_task_store_module.TaskStore
        from channel.web import web_channel

        old_desktop = os.environ.get("ECOREX_DESKTOP")
        old_user_data = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            user_data = os.path.join(root, "user-data")
            os.makedirs(workspace, exist_ok=True)
            os.environ["ECOREX_DESKTOP"] = "1"
            os.environ["ECOREX_DESKTOP_USER_DATA"] = user_data
            get_tool_permission_broker().set_mode("full-access")
            store = TaskStore(os.path.join(workspace, "scheduler", "tasks.json"))
            store.add_task({
                "id": "task-edit",
                "name": "old name",
                "enabled": True,
                "created_at": "2026-06-24T09:00:00",
                "updated_at": "2026-06-24T09:00:00",
                "schedule": {"type": "interval", "seconds": 3600},
                "action": {
                    "type": "agent_task",
                    "task_description": "old task",
                    "receiver": "private-open-id",
                },
                "next_run_at": "2026-06-24T10:00:00",
            })
            body = json.dumps({
                "action": "update",
                "task_id": "task-edit",
                "name": "new name",
                "taskDescription": "new task",
            }).encode("utf-8")
            try:
                with patch.object(web_channel, "_require_auth", return_value=None), \
                    patch.object(web_channel, "_get_workspace_root", return_value=workspace), \
                    patch.object(web_channel.web, "data", return_value=body), \
                    patch.object(scheduler_projection_module, "conf", return_value={
                        "agent_workspace": workspace,
                        "scheduler_enabled": False,
                    }):
                    response = web_channel.SchedulerHandler().POST()
                saved = store.get_task("task-edit")
            finally:
                if old_desktop is None:
                    os.environ.pop("ECOREX_DESKTOP", None)
                else:
                    os.environ["ECOREX_DESKTOP"] = old_desktop
                if old_user_data is None:
                    os.environ.pop("ECOREX_DESKTOP_USER_DATA", None)
                else:
                    os.environ["ECOREX_DESKTOP_USER_DATA"] = old_user_data
                if old_croniter is None:
                    sys.modules.pop("croniter", None)
                else:
                    sys.modules["croniter"] = old_croniter

        payload = json.loads(response)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["taskCount"], 1)
        self.assertEqual(payload["tasks"][0]["name"], "new name")
        self.assertEqual(payload["tasks"][0]["action"]["taskDescriptionPreview"], "[redacted-content]")
        self.assertTrue(payload["tasks"][0]["action"]["taskDescriptionHash"])
        self.assertNotIn("new task", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(saved["name"], "new name")
        self.assertEqual(saved["action"]["task_description"], "new task")

    def test_scheduler_tool_lazy_initializes_enabled_runtime_before_listing(self):
        from common.ecorex_tool_permissions import get_tool_permission_broker

        get_tool_permission_broker()
        fake_croniter = types.ModuleType("croniter")
        fake_croniter.croniter = lambda *args, **kwargs: None
        old_croniter = sys.modules.get("croniter")
        sys.modules["croniter"] = fake_croniter
        try:
            scheduler_tool_module = importlib.import_module("agent.tools.scheduler.scheduler_tool")
            scheduler_integration_module = importlib.import_module("agent.tools.scheduler.integration")
            SchedulerTool = scheduler_tool_module.SchedulerTool

            class FakeTaskStore:
                def list_tasks(self):
                    return []

            with patch.object(scheduler_integration_module, "ensure_scheduler_runtime", return_value=True) as ensure_runtime, \
                patch.object(scheduler_integration_module, "get_task_store", return_value=FakeTaskStore()):
                result = SchedulerTool({}).execute({"action": "list"})
        finally:
            if old_croniter is None:
                sys.modules.pop("croniter", None)
            else:
                sys.modules["croniter"] = old_croniter

        self.assertEqual(result.status, "success")
        self.assertIn("暂无", str(result.result))
        ensure_runtime.assert_called_once()

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
