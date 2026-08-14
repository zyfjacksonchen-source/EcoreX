from __future__ import annotations

import threading
import time

from agent.protocol.agent import Agent
from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.models import LLMModel
from agent.tools.base_tool import BaseTool, ToolResult


class _StepTool(BaseTool):
    name = "step"
    description = "advance one distinct step"
    params = {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    }

    def execute(self, params):
        return ToolResult.success({"completed": params["index"]})


class _LongTaskModel(LLMModel):
    def __init__(self, tool_turns: int = 3):
        super().__init__("cow-long-task-test")
        self.calls = 0
        self.tool_turns = tool_turns

    def call_stream(self, _request):
        self.calls += 1
        if self.calls <= self.tool_turns:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": f"call-{self.calls}",
                            "function": {
                                "name": "step",
                                "arguments": f'{{"index": {self.calls}}}',
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }]
            }
            return
        yield {
            "choices": [{
                "delta": {"content": "all steps completed"},
                "finish_reason": "stop",
            }]
        }


class _RepeatingModel(LLMModel):
    def __init__(self):
        super().__init__("cow-loop-test")
        self.calls = 0

    def call_stream(self, _request):
        self.calls += 1
        if self.calls <= 20:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": f"repeat-{self.calls}",
                            "function": {
                                "name": "step",
                                "arguments": '{"index": 1}',
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }]
            }
            return
        yield {
            "choices": [{
                "delta": {"content": "loop escaped without a guard"},
                "finish_reason": "stop",
            }]
        }


def test_task_continues_past_legacy_step_limit_until_model_finishes():
    model = _LongTaskModel(tool_turns=3)
    agent = Agent(
        "system",
        model=model,
        tools=[_StepTool()],
        enable_skills=False,
        max_steps=2,
    )

    assert agent.run_stream("complete every step") == "all steps completed"
    assert model.calls == 4


def test_context_overflow_compaction_keeps_current_task_and_tool_fact():
    agent = Agent(
        "system",
        model=LLMModel("cow-compaction-test"),
        enable_skills=False,
    )
    agent._estimate_message_tokens = lambda _message: 1
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "KEEP-TASK"}]},
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "call-large",
                "name": "step",
                "input": {"index": 1},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call-large",
                "content": "KEEP-FACT " + ("x" * 20_000),
            }],
        },
    ]
    executor = AgentStreamExecutor(
        agent=agent,
        model=agent.model,
        system_prompt="system",
        tools=[],
        messages=messages,
    )

    assert executor._aggressive_trim_for_overflow() is True
    encoded = str(executor.messages)
    assert "KEEP-TASK" in encoded
    assert "KEEP-FACT" in encoded
    assert "Truncated for context recovery" in encoded


def test_pre_cancel_never_calls_provider_and_returns_under_100ms():
    cancel = threading.Event()
    cancel.set()
    model = _LongTaskModel(tool_turns=0)
    executor = AgentStreamExecutor(
        agent=Agent("system", model=model, enable_skills=False),
        model=model,
        system_prompt="system",
        tools=[],
        max_turns=1,
        cancel_event=cancel,
    )

    started = time.monotonic()
    response = executor.run_stream("do not start")

    assert time.monotonic() - started < 0.1
    assert model.calls == 0
    assert response == "_(Cancelled)_"


def test_identical_no_progress_tool_loop_still_stops():
    model = _RepeatingModel()
    agent = Agent(
        "system",
        model=model,
        tools=[_StepTool()],
        enable_skills=False,
        max_steps=1,
    )

    response = agent.run_stream("do not repeat the same completed step")

    assert "停止执行以防止无限循环" in response
    assert model.calls <= 6
