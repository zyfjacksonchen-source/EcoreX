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
