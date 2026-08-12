import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_cow_context_defaults_have_one_budget_source():
    from config import available_setting

    assert available_setting["agent_max_context_tokens"] == 64_000
    assert available_setting["agent_max_context_turns"] == 30
    assert "model_auto_compact_token_limit" not in available_setting


def test_agent_initializer_uses_agent_context_budget(monkeypatch, tmp_path):
    import agent.prompt
    import config
    from bridge.agent_initializer import AgentInitializer

    settings = {
        "agent_workspace": str(tmp_path),
        "agent_max_steps": 30,
        "agent_max_context_tokens": 12_345,
        "model_auto_compact_token_limit": 99_999,
    }
    monkeypatch.setattr(config, "conf", lambda: settings)
    monkeypatch.setattr(agent.prompt, "ensure_workspace", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent.prompt, "load_context_files", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        agent.prompt,
        "PromptBuilder",
        lambda **_kwargs: SimpleNamespace(build=lambda **_build_kwargs: "system"),
    )

    bridge = Mock()
    bridge.create_agent.return_value = SimpleNamespace(memory_manager=None, model=None)
    initializer = AgentInitializer(None, bridge)
    monkeypatch.setattr(initializer, "_migrate_config_to_env", lambda *_args: None)
    monkeypatch.setattr(initializer, "_load_env_file", lambda: None)
    monkeypatch.setattr(initializer, "_setup_memory_system", lambda *_args: (None, []))
    monkeypatch.setattr(initializer, "_load_tools", lambda *_args: [])
    monkeypatch.setattr(initializer, "_initialize_scheduler", lambda *_args: None)
    monkeypatch.setattr(initializer, "_initialize_skill_manager", lambda *_args: None)
    monkeypatch.setattr(initializer, "_get_runtime_info", lambda *_args: {})
    monkeypatch.setattr(initializer, "_start_daily_flush_timer", lambda: None)

    initializer.initialize_agent()

    assert bridge.create_agent.call_args.kwargs["max_context_tokens"] == 12_345


def test_trim_uses_cow_agent_budget_without_a_second_interpreter():
    from agent.protocol.agent_stream import AgentStreamExecutor

    agent = SimpleNamespace(
        max_context_tokens=1_000,
        memory_manager=None,
        _get_model_context_window=lambda: 100,
        _estimate_message_tokens=lambda _message: 1,
    )
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "inspect"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "read", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    executor = AgentStreamExecutor(
        agent=agent,
        model=SimpleNamespace(model="test"),
        system_prompt="system",
        tools=[],
        messages=messages,
    )
    executor._context_budget_limits = lambda: {"effective_context_limit_tokens": 1}

    executor._trim_messages()

    assert any(
        block.get("type") == "tool_use"
        for message in executor.messages
        for block in message.get("content", [])
        if isinstance(block, dict)
    )


def test_evolution_pressure_uses_the_agent_budget_only():
    import threading

    from agent.evolution.trigger import _context_pressure_reached

    agent = SimpleNamespace(
        messages=[{"role": "user", "content": "short"}],
        messages_lock=threading.Lock(),
        max_context_tokens=None,
        _get_model_context_window=lambda: 1,
        _estimate_message_tokens=lambda _message: 1_000,
    )

    assert _context_pressure_reached(agent) is False


def _text_turn(index: int) -> tuple[dict, dict]:
    return (
        {"role": "user", "content": [{"type": "text", "text": f"u{index}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"a{index}"}]},
    )


def test_real_cow_executor_repeatedly_converges_turn_and_token_budgets():
    from agent.protocol.agent import Agent
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.protocol.models import LLMModel

    agent = Agent(
        "system",
        model=LLMModel("cow-test"),
        enable_skills=False,
        max_context_tokens=64_000,
    )
    agent._estimate_message_tokens = lambda _message: 1
    messages = [message for index in range(100) for message in _text_turn(index)]
    executor = AgentStreamExecutor(
        agent=agent,
        model=agent.model,
        system_prompt="system",
        tools=[],
        messages=messages,
        max_context_turns=30,
    )

    executor._trim_messages()

    assert len(executor._identify_complete_turns()) <= 30

    agent._estimate_message_tokens = lambda _message: 1_000
    executor.messages = [
        message for index in range(100) for message in _text_turn(index)
    ]
    executor.max_context_turns = 1_000
    executor._trim_messages()

    total = agent._estimate_message_tokens(
        {"role": "system", "content": executor.system_prompt}
    ) + sum(
        executor._estimate_turn_tokens(turn)
        for turn in executor._identify_complete_turns()
    )
    assert total <= agent.max_context_tokens


def test_real_cow_agent_trims_once_before_a_tool_loop(monkeypatch):
    from agent.protocol.agent import Agent
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.protocol.models import LLMModel
    from agent.tools.base_tool import BaseTool, ToolResult

    class Tool(BaseTool):
        name = "test_tool"
        description = "test"
        params = {"type": "object", "properties": {}}

        def execute(self, _params):
            return ToolResult.success("ok")

    class Model(LLMModel):
        def __init__(self):
            super().__init__("cow-test")
            self.calls = 0

        def call_stream(self, _request):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "choices": [{
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "test_tool", "arguments": "{}"},
                            }]
                        },
                        "finish_reason": "tool_calls",
                    }]
                }
            else:
                yield {
                    "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]
                }

    trim_calls = 0
    original_trim = AgentStreamExecutor._trim_messages

    def counted_trim(executor):
        nonlocal trim_calls
        trim_calls += 1
        return original_trim(executor)

    monkeypatch.setattr(AgentStreamExecutor, "_trim_messages", counted_trim)
    model = Model()
    agent = Agent("system", model=model, tools=[Tool()], enable_skills=False)

    assert agent.run_stream("use the tool") == "done"
    assert model.calls == 2
    assert trim_calls == 1


def test_trim_summary_stays_in_project_and_is_searchable_after_restart(
    tmp_path: Path, monkeypatch,
):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.protocol.agent import Agent
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.protocol.models import LLMModel
    from common import ecorex_tool_permissions as permissions

    monkeypatch.setattr(
        permissions._BROKER,
        "authorize_file_access",
        lambda *_args, **_kwargs: {"allowed": False, "reason": "not-cow-turn"},
    )
    token = permissions.bind_cow_direct_tools()
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    try:
        memory = MemoryManager(MemoryConfig(workspace_root=str(project_a)))
        monkeypatch.setattr(
            memory.flush_manager,
            "_summarize_messages",
            lambda _messages, _max_messages: "- project-a durable compaction marker",
        )
        agent = Agent(
            "system",
            model=LLMModel("cow-test"),
            enable_skills=False,
            max_context_tokens=64_000,
            memory_manager=memory,
            workspace_dir=str(project_a),
        )
        agent._estimate_message_tokens = lambda _message: 1
        executor = AgentStreamExecutor(
            agent=agent,
            model=agent.model,
            system_prompt="system",
            tools=[],
            messages=[
                message for index in range(31) for message in _text_turn(index)
            ],
            max_context_turns=30,
        )

        executor._trim_messages()
        thread = memory.flush_manager._last_flush_thread
        assert thread is not None
        thread.join(timeout=2)
        assert not thread.is_alive()

        daily = memory.flush_manager.get_today_memory_file()
        assert daily.parent == project_a / "memory"
        assert "project-a durable compaction marker" in daily.read_text("utf-8")

        restarted = MemoryManager(MemoryConfig(workspace_root=str(project_a)))
        asyncio.run(restarted.sync())
        matches = asyncio.run(
            restarted.search("durable compaction marker", min_score=-1)
        )
        assert any("project-a durable compaction marker" in item.snippet for item in matches)

        other = MemoryManager(MemoryConfig(workspace_root=str(project_b)))
        asyncio.run(other.sync())
        assert not asyncio.run(other.search("durable compaction marker"))
    finally:
        permissions.reset_cow_direct_tools(token)


def test_compaction_never_overwrites_append_only_sqlite_history(tmp_path: Path):
    from agent.memory.conversation_store import ConversationStore
    from agent.protocol.agent import Agent
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.protocol.models import LLMModel

    store = ConversationStore(tmp_path / "history.db")
    original = [message for index in range(40) for message in _text_turn(index)]
    store.append_messages("session", original, channel_type="web")
    agent = Agent(
        "system",
        model=LLMModel("cow-test"),
        enable_skills=False,
        max_context_tokens=64_000,
    )
    agent._estimate_message_tokens = lambda _message: 1
    executor = AgentStreamExecutor(
        agent=agent,
        model=agent.model,
        system_prompt="system",
        tools=[],
        messages=store.load_messages("session", max_turns=1_000),
        max_context_turns=30,
    )

    executor._trim_messages()

    assert len(executor.messages) < len(original)
    assert store.load_messages("session", max_turns=1_000) == original


def test_public_manual_compact_reuses_summary_for_context_and_project_memory(
    tmp_path: Path, monkeypatch,
):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.protocol.agent import Agent
    from agent.protocol.models import LLMModel
    from common.ecorex_tool_permissions import (
        bind_cow_direct_tools,
        reset_cow_direct_tools,
    )

    token = bind_cow_direct_tools()
    try:
        memory = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)))
        monkeypatch.setattr(
            memory.flush_manager,
            "_summarize_messages",
            lambda _messages, **_kwargs: "- manual durable marker",
        )
        agent = Agent(
            "system",
            model=LLMModel("cow-test"),
            enable_skills=False,
            memory_manager=memory,
            workspace_dir=str(tmp_path),
        )
        agent.messages = [
            message for index in range(3) for message in _text_turn(index)
        ]

        result = agent.compact_context(keep_recent_turns=1)

        assert result["ok"] is True
        assert "manual durable marker" in agent.messages[0]["content"][0]["text"]
        daily = memory.flush_manager.get_today_memory_file()
        assert "manual durable marker" in daily.read_text("utf-8")
    finally:
        reset_cow_direct_tools(token)


def test_public_agent_rebuilds_workspace_memory_knowledge_identity_rules_and_skills_each_turn(
    tmp_path: Path,
):
    from agent.protocol.agent import Agent
    from agent.protocol.models import LLMModel
    from agent.skills.manager import SkillManager
    from agent.tools.base_tool import BaseTool, ToolResult

    class PromptTool(BaseTool):
        description = "prompt contract"
        params = {"type": "object", "properties": {}}

        def __init__(self, name):
            self.name = name

        def execute(self, _params):
            return ToolResult.success("ok")

    class Model(LLMModel):
        def __init__(self):
            super().__init__("cow-test")
            self.requests = []

        def call_stream(self, request):
            self.requests.append(request)
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

    workspace = tmp_path / "project"
    (workspace / "knowledge").mkdir(parents=True)
    skill_dir = workspace / "skills" / "project-skill"
    skill_dir.mkdir(parents=True)

    def write_revision(revision: str):
        for name in ("AGENT", "USER", "RULE", "MEMORY"):
            (workspace / f"{name}.md").write_text(
                f"# {name}\n{name}-{revision}", encoding="utf-8"
            )
        (workspace / "knowledge" / "index.md").write_text(
            f"# Knowledge\nknowledge-{revision}", encoding="utf-8"
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: project-skill\n"
            f"description: skill-{revision}\n---\n",
            encoding="utf-8",
        )

    write_revision("one")
    skills = SkillManager(
        builtin_dir=str(tmp_path / "empty-builtins"),
        custom_dir=str(workspace / "skills"),
        config={},
    )
    skills.extra_dirs = []
    model = Model()
    agent = Agent(
        "cached-system-must-not-win",
        model=model,
        tools=[PromptTool(name) for name in ("skill_search", "skill_read", "skill_run")],
        memory_manager=SimpleNamespace(),
        workspace_dir=str(workspace),
        skill_manager=skills,
    )

    assert agent.run_stream("first") == "done"
    write_revision("two")
    assert agent.run_stream("second") == "done"

    first, second = (request.system for request in model.requests)
    for marker in (
        "AGENT-two", "USER-two", "RULE-two", "MEMORY-two",
        "knowledge-two", "skill-two",
    ):
        assert marker in second
    assert "knowledge-one" in first and "skill-one" in first
    assert "knowledge-one" not in second and "skill-one" not in second
