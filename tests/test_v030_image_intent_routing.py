from types import SimpleNamespace

from agent.protocol.agent_stream import AgentStreamExecutor


def _executor(messages):
    tools = [
        SimpleNamespace(name="read"),
        SimpleNamespace(name="imagegen"),
    ]
    executor = AgentStreamExecutor(
        agent=SimpleNamespace(),
        model=SimpleNamespace(),
        system_prompt="",
        tools=tools,
        messages=messages,
    )
    executor._tool_schema_config_bool = lambda *_args: True
    return executor


def test_image_intent_routes_without_an_explicit_mention_and_keeps_suppressions() -> None:
    create = _executor([
        {"role": "user", "content": "给我生成一张未来城市海报"},
    ])
    selected, _ = create._select_tools_for_schema()
    assert set(selected) == {"imagegen"}

    analyze = _executor([
        {"role": "user", "content": "不要生图，只分析这张截图"},
    ])
    selected, _ = analyze._select_tools_for_schema()
    assert "imagegen" not in selected

    followup = _executor([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "再来一张"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ])
    selected, budget = followup._select_tools_for_schema()
    assert set(selected) == {"imagegen"}
    assert budget["inherited_followup_intent"] is True
