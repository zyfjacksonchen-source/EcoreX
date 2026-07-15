from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.usage import UsageProjectionService


TOKEN = "u" * 32
CSRF = "v" * 32
ORIGIN = "http://testserver"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _append_completed(kernel, *, thread_id: str, turn_id: str, created_at: datetime, usage: dict[str, int]) -> None:
    kernel.events.append(
        thread_id=thread_id,
        turn_id=turn_id,
        event_type="model.response_completed",
        payload={"response_id": f"resp_{turn_id}", "usage": usage, "round": 0},
        created_at=created_at,
    )


def _turn(app, *, title: str, message_id: str):
    kernel = app.state.runtime
    composition = app.state.runtime_composition
    thread = kernel.create_thread(CreateThreadRequest(title=title))
    prepared = composition.prepare_turn(
        CreateTurnRequest(
            input="整理本周事项",
            client_message_id=message_id,
        )
    )
    created = kernel.create_turn(
        thread.thread_id,
        prepared.request,
        snapshot_context=prepared.snapshot_context,
    )
    return thread, created


def test_usage_projection_uses_provider_facts_for_calendar_and_context(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    first_thread, first_turn = _turn(app, title="第一项", message_id="usage-first")
    second_thread, second_turn = _turn(app, title="第二项", message_id="usage-second")
    kernel = app.state.runtime
    _append_completed(
        kernel,
        thread_id=first_thread.thread_id,
        turn_id=first_turn.turn.turn_id,
        created_at=datetime(2026, 7, 12, 15, 59, tzinfo=UTC),
        usage={"input_tokens": 50, "output_tokens": 5},
    )
    _append_completed(
        kernel,
        thread_id=first_thread.thread_id,
        turn_id=first_turn.turn.turn_id,
        created_at=datetime(2026, 7, 12, 16, 30, tzinfo=UTC),
        usage={"input_tokens": 10, "output_tokens": 3},
    )
    _append_completed(
        kernel,
        thread_id=second_thread.thread_id,
        turn_id=second_turn.turn.turn_id,
        created_at=datetime(2026, 7, 14, 17, 0, tzinfo=UTC),
        usage={"input_tokens": 7, "output_tokens": 1},
    )
    _append_completed(
        kernel,
        thread_id=first_thread.thread_id,
        turn_id=first_turn.turn.turn_id,
        created_at=datetime(2026, 7, 14, 18, 0, tzinfo=UTC),
        usage={"input_tokens": 20, "output_tokens": 4},
    )

    projection = UsageProjectionService(
        kernel.database,
        model_catalog=app.state.runtime_composition.model_catalog,
        timezone_name="Asia/Shanghai",
        clock=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    ).project(first_thread.thread_id)

    assert projection.today.model_dump() == {
        "input_tokens": 27,
        "output_tokens": 5,
        "total_tokens": 32,
    }
    assert projection.week.model_dump() == {
        "input_tokens": 37,
        "output_tokens": 8,
        "total_tokens": 45,
    }
    assert projection.context.model_id == "ecorex-chat"
    assert projection.context.used_tokens == 20
    assert projection.context.window_tokens == 272_000


def test_usage_endpoint_returns_a_strict_read_only_projection(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    thread, created = _turn(app, title="用量接口", message_id="usage-api")
    _append_completed(
        app.state.runtime,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        created_at=datetime.now(UTC),
        usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    client = TestClient(app)

    response = client.get(f"/api/v1/threads/{thread.thread_id}/usage", headers=_headers())

    assert response.status_code == 200
    assert response.json()["today"] == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    assert response.json()["context"]["used_tokens"] == 2
    assert response.json()["context"]["window_tokens"] == 272_000
    assert client.get("/api/v1/threads/thread_missing/usage", headers=_headers()).status_code == 404
