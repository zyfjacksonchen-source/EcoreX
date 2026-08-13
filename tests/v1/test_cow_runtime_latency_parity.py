from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace


def test_worker_reuses_the_live_cow_agent_for_the_session(
    tmp_path: Path, monkeypatch,
) -> None:
    from bridge.agent_initializer import AgentInitializer
    from ecorex.runtime.worker import AgentTurnWorker

    initialized: list[object] = []

    class FakeAgent:
        def __init__(self, model: object) -> None:
            self.model = model
            self.tools = []
            self.messages = []
            self.messages_lock = threading.Lock()
            self.memory_manager = SimpleNamespace(
                flush_manager=SimpleNamespace(llm_model=None)
            )
            self._last_run_new_messages = []
            self._last_run_context_compacted = False
            self.inputs: list[str] = []

        def run_stream(self, value: str, **_kwargs) -> str:
            self.inputs.append(value)
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
    store = SimpleNamespace(append_messages=lambda *_args, **_kwargs: None)

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
    assert worker._run_agent(model=second_model, input_text="two", **arguments) == "done"
    second_elapsed = time.monotonic() - started

    assert first_elapsed >= 0.09
    assert second_elapsed < 0.05
    assert len(initialized) == 1
    assert initialized[0].inputs == ["one", "two"]
    assert initialized[0].model is second_model
    assert initialized[0].memory_manager.flush_manager.llm_model == (
        "second",
        "memory-summary",
    )
