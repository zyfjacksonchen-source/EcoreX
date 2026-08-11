from ecorex.gateway import GatewayAssistantMessageInput, GatewayUserMessageInput
from ecorex.runtime.worker import (
    _COW_MAX_CONTEXT_TOKENS,
    _COW_MAX_CONTEXT_TURNS,
    _cow_estimate_text_tokens,
    _cow_fallback_context_summary,
    _cow_retained_turn_count,
    _cow_text_only_turn,
)


def test_runtime_uses_cow_context_budget_and_half_trim_cycle() -> None:
    assert _COW_MAX_CONTEXT_TOKENS == 64_000
    assert _COW_MAX_CONTEXT_TURNS == 30
    assert [_cow_retained_turn_count(value) for value in (30, 31, 45, 46)] == [
        30,
        16,
        30,
        16,
    ]
    assert _cow_estimate_text_tokens("abcd你好") == 4


def test_runtime_compaction_keeps_turn_roles_and_injectable_summary() -> None:
    turn = [
        GatewayUserMessageInput(message_id="user", content="请继续上次的项目"),
        GatewayAssistantMessageInput(message_id="draft", content="处理中"),
        GatewayAssistantMessageInput(message_id="final", content="项目已经完成\n详情"),
    ]

    compressed = _cow_text_only_turn(turn)

    assert [item.message_id for item in compressed] == ["user", "final"]
    assert _cow_fallback_context_summary([turn]) == (
        "- 用户: 请继续上次的项目 → 回复: 项目已经完成"
    )
