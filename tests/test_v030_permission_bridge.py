from __future__ import annotations

from types import SimpleNamespace

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.task_observer import TaskObserver
from common import ecorex_tool_permissions as permissions


def test_verified_runtime_full_access_survives_legacy_broker_failure(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ECOREX_USER_DATA", str(tmp_path))
    permissions.sync_verified_runtime_permission(full_access=True)
    try:
        assert permissions.get_tool_permission_broker().authorize_noninteractive(
            "bash", {"command": "echo ok"}
        )["allowed"] is True

        executor = object.__new__(AgentStreamExecutor)
        executor.cancel_event = None
        executor.on_event = None
        monkeypatch.setattr(
            permissions,
            "get_tool_permission_broker",
            lambda: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
        )
        decision = executor._authorize_tool_execution(
            "bash", "tool-call-1", {"command": "echo ok"}
        )
        assert decision == {
            "allowed": True,
            "reason": "verified-runtime-full-access",
        }
    finally:
        permissions.sync_verified_runtime_permission(full_access=False)


def test_last_legacy_turn_with_text_is_not_misclassified_as_exhausted(monkeypatch) -> None:
    events = []
    executor = AgentStreamExecutor(
        agent=SimpleNamespace(last_usage={}),
        model=SimpleNamespace(model="test-model"),
        system_prompt="",
        tools=[],
        max_turns=1,
        on_event=events.append,
    )
    calls = []
    monkeypatch.setattr(
        executor,
        "_call_llm_stream",
        lambda **_kwargs: (calls.append("model") or ("完成", [])),
    )
    monkeypatch.setattr(executor, "_trim_messages", lambda: None)
    monkeypatch.setattr(executor, "_validate_and_fix_messages", lambda: None)

    assert executor.run_stream("执行") == "完成"
    assert calls == ["model"]
    assert next(event for event in events if event["type"] == "agent_end")["data"]["outcome"] == "completed"


def test_unknown_task_terminal_status_fails_closed() -> None:
    events = []
    TaskObserver(
        lambda event, payload: events.append((event, payload)),
        "task-1",
        "tool",
        "test",
    ).end("critical_error")
    assert events[0][0] == "task.failed"


def test_successful_distinct_feishu_batch_targets_do_not_consume_chain_budget() -> None:
    executor = object.__new__(AgentStreamExecutor)
    executor.tool_failure_history = []
    executor.tool_chain_history = []

    for index in range(10):
        arguments = {
            "action": "run",
            "args": [
                "base",
                "+record-upsert",
                "--record-id",
                f"record-{index}",
            ],
        }
        assert executor._check_tool_chain_budget("feishu_cli", arguments)[0] is False
        executor._record_tool_result("feishu_cli", arguments, True)

    assert executor._build_convergence_hint() == ""

    failed = {"action": "run", "args": ["base", "+record-upsert"]}
    for _ in range(6):
        assert executor._check_tool_chain_budget("feishu_cli", failed)[0] is False
        executor._record_tool_result("feishu_cli", failed, False)
    assert executor._check_tool_chain_budget("feishu_cli", failed)[0] is True


def test_image_generation_shell_detector_uses_central_intent_without_instance() -> None:
    assert AgentStreamExecutor._looks_like_image_generation_shell_command(
        'python -c "from PIL import Image; Image.new((1, 1))" 生成一张图片'
    ) is True


def test_missing_tool_does_not_read_skill_files_or_create_a_second_authority() -> None:
    class SkillManager:
        def get_skill(self, _name):
            raise AssertionError("legacy Skill lookup must not run")

    executor = object.__new__(AgentStreamExecutor)
    executor.tools = {"browser": SimpleNamespace(name="browser")}
    executor.agent = SimpleNamespace(skill_manager=SkillManager())

    assert executor._build_tool_not_found_message("browser-script") == (
        "Tool 'browser-script' not found. Available tools: ['browser']"
    )
