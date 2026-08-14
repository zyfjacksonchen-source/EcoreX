from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def test_worker_reuses_live_agent_without_awaiting_memory_flush(
    tmp_path: Path, monkeypatch,
) -> None:
    from bridge.agent_initializer import AgentInitializer
    from ecorex.runtime.worker import AgentTurnWorker

    initialized: list[object] = []
    flush_release = threading.Event()
    flush_finished = threading.Event()
    persisted: list[dict] = []

    class FakeAgent:
        def __init__(self, model: object) -> None:
            self.model = model
            self.tools = []
            self.messages = [{
                "role": "user",
                "content": [{"type": "text", "text": "kept context"}],
            }]
            self.messages_lock = threading.Lock()
            self.memory_manager = SimpleNamespace(
                flush_manager=SimpleNamespace(llm_model=None)
            )
            self._last_run_new_messages = []
            self._last_run_context_compacted = False
            self.inputs: list[str] = []
            self.visible_context: list[str] = []

        def run_stream(self, value: str, **_kwargs) -> str:
            self.inputs.append(value)
            self.visible_context.append(self.messages[0]["content"][0]["text"])
            self._last_run_new_messages = [{"role": "assistant", "content": value}]
            self._last_run_context_compacted = value == "two"
            self._last_compaction_summary = ""
            if value == "two":
                def finish_flush() -> None:
                    flush_release.wait()
                    self.messages[0]["content"][0]["text"] = "durable summary"
                    self._last_compaction_summary = "durable summary"
                    flush_finished.set()

                thread = threading.Thread(target=finish_flush, daemon=True)
                self.memory_manager.flush_manager._last_flush_thread = thread
                thread.start()
            return "done"

    worker = object.__new__(AgentTurnWorker)
    worker.builtin_skill_root = None
    worker.mcp_oauth_redirect_uri = None
    worker._cow_bridge = __import__(
        "ecorex.runtime.worker", fromlist=["_CowAgentBridge"]
    )._CowAgentBridge()
    worker._turn_image_artifact_history = lambda _turn_id: []

    def initialize(_self, **_kwargs):
        time.sleep(0.1)
        agent = FakeAgent(worker._cow_bridge._model.get())
        initialized.append(agent)
        return agent

    monkeypatch.setattr(AgentInitializer, "initialize_agent", initialize)
    store = SimpleNamespace(
        append_messages=lambda _session, messages, **_kwargs: (
            persisted.extend(messages) or len(persisted)
        ),
        set_compaction_state=lambda *_args, **_kwargs: None,
    )

    def model(name: str):
        return SimpleNamespace(name=name, fork=lambda scope: (name, scope))

    arguments = dict(
        workspace=tmp_path,
        job_id="job",
        thread_id="thread",
        turn_id="turn",
        callback=lambda _event: None,
        cancel_event=threading.Event(),
        inbox=None,
        managed_image_executor=None,
        managed_web_search_executor=None,
        channel_context=None,
        channel_type="web",
        receiver="thread",
        conversation_store=store,
        project_context=None,
        record_evolution=False,
    )
    first_model = model("first")
    second_model = model("second")

    started = time.monotonic()
    assert worker._run_agent(model=first_model, input_text="one", **arguments) == "done"
    first_elapsed = time.monotonic() - started
    started = time.monotonic()
    flush_failsafe = threading.Timer(0.2, flush_release.set)
    flush_failsafe.start()
    assert worker._run_agent(model=second_model, input_text="two", **arguments) == "done"
    second_elapsed = time.monotonic() - started

    assert first_elapsed >= 0.09
    assert second_elapsed < 0.05
    assert not flush_finished.is_set()
    assert any(message.get("content") == "two" for message in persisted)

    flush_release.set()
    flush_failsafe.cancel()
    initialized[0].memory_manager.flush_manager._last_flush_thread.join(timeout=1)
    assert flush_finished.is_set()
    assert worker._run_agent(
        model=second_model, input_text="three", **{**arguments, "turn_id": "turn-3"}
    ) == "done"
    assert len(initialized) == 1
    assert initialized[0].inputs == ["one", "two", "three"]
    assert initialized[0].visible_context[-1] == "durable summary"
    assert initialized[0].model is second_model
    assert initialized[0].memory_manager.flush_manager.llm_model == (
        "second",
        "memory-summary",
    )


def test_worker_shutdown_boundedly_drains_every_dispatched_memory_flush(
    tmp_path: Path, monkeypatch,
) -> None:
    from agent.memory.summarizer import MemoryFlushManager
    from ecorex.runtime import worker as worker_module

    manager = MemoryFlushManager(tmp_path)
    release = threading.Event()
    started = threading.Event()
    finished: list[str] = []

    def slow_flush(messages, *_args) -> None:
        if len(manager._flush_threads) == 2:
            started.set()
        release.wait()
        finished.append(messages[0]["content"])

    monkeypatch.setattr(manager, "_flush_worker", slow_flush)
    for marker in ("one", "two"):
        assert manager.flush_from_messages([{"role": "user", "content": marker}])
    assert started.wait(timeout=1)

    worker = object.__new__(worker_module.AgentTurnWorker)
    worker._cancel_events = {}
    worker._cow_bridge = SimpleNamespace(
        agents={
            "thread": SimpleNamespace(
                memory_manager=SimpleNamespace(flush_manager=manager)
            )
        }
    )
    threading.Timer(0.05, release.set).start()
    asyncio.run(worker.close())
    assert sorted(finished) == ["one", "two"]

    release.clear()
    assert manager.flush_from_messages([{"role": "user", "content": "three"}])
    started_at = time.monotonic()
    assert manager.drain(0.02) is False
    assert time.monotonic() - started_at < 0.1
    release.set()
    assert manager.drain(1) is True
