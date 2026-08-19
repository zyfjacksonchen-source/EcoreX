from __future__ import annotations

from fastapi.testclient import TestClient

from ecorex.gateway import GatewayEvent, GatewayEventType
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, ItemKind, ItemStatus, TurnStatus
from ecorex.runtime import RuntimeKernel, create_app
from ecorex.runtime.api import RuntimeSettings


def _complete_first_exchange(
    kernel: RuntimeKernel,
    user_message: str,
    assistant_reply: str,
) -> tuple[str, str]:
    thread = kernel.create_thread(CreateThreadRequest())
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input=user_message, client_message_id=f"message-{thread.thread_id}"),
    )
    leased = kernel.jobs.lease_next("title-test-worker")
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, "title-test-worker", leased.lease_token)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        content={"role": "assistant", "text": assistant_reply},
        status=ItemStatus.COMPLETED,
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    kernel.finish_turn_job(
        job_id=leased.job_id,
        worker_id="title-test-worker",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    return thread.thread_id, created.turn.turn_id


def test_first_exchange_summary_replaces_safe_temporary_title_and_persists(tmp_path):
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    secret = "secret_token_abcdefghijklmnopqrstuvwxyz"
    prompt = f"上下文验收第一轮：请记住三项事实并且不要使用任何工具 {secret} /Users/test/private/data"
    thread = kernel.create_thread(CreateThreadRequest())
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input=prompt, client_message_id="first-title-message"),
    )

    temporary = kernel.get_thread(thread.thread_id).title or ""
    assert temporary != prompt
    assert len(temporary) <= 30
    assert secret not in temporary and "/Users/" not in temporary
    assert kernel.automatic_title_context(thread.thread_id) is None

    leased = kernel.jobs.lease_next("title-test-worker")
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, "title-test-worker", leased.lease_token)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        content={"role": "assistant", "text": "已完成三项上下文事实记忆。"},
        status=ItemStatus.COMPLETED,
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    kernel.finish_turn_job(
        job_id=leased.job_id,
        worker_id="title-test-worker",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    assert kernel.automatic_title_context(thread.thread_id) == {
        "user_message": prompt,
        "assistant_reply": "已完成三项上下文事实记忆。",
        "turn_id": created.turn.turn_id,
        "agent_model_id": "ecorex-chat",
    }

    summarized = kernel.apply_generated_thread_title(
        thread.thread_id,
        "上下文事实记忆验收\n{\"path\":\"/Users/test/private/data\"}",
    )
    assert summarized.title == "上下文事实记忆验收"
    assert kernel.automatic_title_context(thread.thread_id) is None
    assert RuntimeKernel(path).get_thread(thread.thread_id).title == "上下文事实记忆验收"


def test_manual_title_is_authoritative_and_failed_generation_uses_short_fallback(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    manual_thread_id, _ = _complete_first_exchange(kernel, "整理季度复盘材料", "材料已整理完成。")
    kernel.rename_thread(manual_thread_id, "人工确认标题", client_request_id="manual-title")
    assert kernel.apply_generated_thread_title(manual_thread_id, "模型自动标题").title == "人工确认标题"

    fallback_thread_id, _ = _complete_first_exchange(
        kernel,
        "分析一个很长很长的季度经营计划并输出执行建议和风险清单",
        "已给出执行建议和风险清单。",
    )
    fallback = kernel.apply_generated_thread_title(fallback_thread_id, "")
    assert fallback.title
    assert len(fallback.title) <= 30


def test_runtime_generate_title_uses_same_gateway_without_tools_and_manual_rename_wins(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread_id, turn_id = _complete_first_exchange(
        kernel,
        "评审客户访谈",
        "访谈重点是交付周期。",
    )

    class TitleGateway:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            kernel.rename_thread(
                thread_id,
                "人工确认标题",
                client_request_id="rename-during-title-generation",
            )
            yield GatewayEvent(
                seq=1,
                event_type=GatewayEventType.OUTPUT_TEXT_DELTA,
                response_id="response-title",
                delta="客户访谈交付摘要",
            )
            yield GatewayEvent(
                seq=2,
                event_type=GatewayEventType.RESPONSE_COMPLETED,
                response_id="response-title",
                usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            )

    gateway = TitleGateway()
    settings = RuntimeSettings(
        database_path=path,
        runtime_bearer_token="r" * 32,
        csrf_token="c" * 32,
        webui_origins=("http://testserver",),
        model_gateway=gateway,
        allow_unmanaged_model_gateway_for_testing=True,
        close_model_gateway_on_shutdown=False,
    )
    legacy_calls = []

    def forbidden_legacy_bot(*_args, **_kwargs):
        legacy_calls.append(True)
        raise AssertionError("automatic title must not use Bridge bot/provider")

    monkeypatch.setattr("bridge.bridge.Bridge", forbidden_legacy_bot)
    client = TestClient(create_app(settings=settings))
    response = client.post(
        f"/api/v1/threads/{thread_id}/generate_title",
        headers={
            "Authorization": f"Bearer {settings.runtime_bearer_token}",
            "Origin": "http://testserver",
            "X-EcoreX-CSRF": settings.csrf_token,
        },
    )
    assert response.status_code == 200
    assert response.json()["title"] == "人工确认标题"
    assert legacy_calls == []
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert (request.thread_id, request.turn_id, request.model_id) == (
        thread_id,
        turn_id,
        "ecorex-chat",
    )
    assert request.instructions == (
        "You are the intelligent work Agent 小芯 inside the e-Mate Agent product."
    )
    assert request.direct_tools == []
    assert request.deferred_tool_ids == []
    assert request.disclosed_tool_ids == []
    assert request.suppressed_tool_ids == []
    assert len(request.input_items or []) == 1
    title_prompt = request.input_items[0].content
    assert "评审客户访谈" in title_prompt
    assert "访谈重点是交付周期。" in title_prompt
