from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace

from fastapi.testclient import TestClient

from ecorex.gateway import GatewayAccountUsageProjection, GatewayTokenUsageWindow
from ecorex.capabilities import ManagedModelCatalog, builtin_model_catalog
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest, TurnStatus
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


class AccountUsageGateway:
    def __init__(self) -> None:
        self.available = True
        self.calls: list[str] = []
        self.coverage_started_at: datetime | None = datetime(
            2026, 7, 12, 15, 0, tzinfo=UTC
        )

    async def usage(self, timezone_name: str) -> GatewayAccountUsageProjection:
        self.calls.append(timezone_name)
        if not self.available:
            raise RuntimeError("gateway unavailable")
        return GatewayAccountUsageProjection(
            timezone=timezone_name,
            today=GatewayTokenUsageWindow(
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
            week=GatewayTokenUsageWindow(
                input_tokens=400,
                output_tokens=80,
                total_tokens=480,
            ),
            week_started_at=datetime(2026, 7, 12, 16, 0, tzinfo=UTC),
            coverage_started_at=self.coverage_started_at,
            calculated_at=datetime(2026, 7, 19, 1, 0, tzinfo=UTC),
        )

    async def stream(self, _request):
        if False:
            yield None


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


def test_usage_projection_derives_home_activity_from_turn_states_in_shanghai(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime-task-activity.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    turns = [
        _turn(app, title=f"任务 {index}", message_id=f"task-activity-{index}")[1].turn
        for index in range(5)
    ]
    states = [
        (TurnStatus.COMPLETED, "2026-08-04T16:00:00.000000+00:00"),
        (TurnStatus.FAILED, "2026-08-05T03:00:00.000000+00:00"),
        (TurnStatus.CANCELLED, "2026-08-03T15:59:59.000000+00:00"),
        (TurnStatus.QUEUED, "2026-08-05T03:30:00.000000+00:00"),
        (TurnStatus.WAITING_HUMAN, "2026-08-05T03:45:00.000000+00:00"),
    ]
    with app.state.runtime.database.transaction() as connection:
        for turn, (status, updated_at) in zip(turns, states, strict=True):
            connection.execute(
                "UPDATE turns SET status = ?, updated_at = ? WHERE turn_id = ?",
                (status.value, updated_at, turn.turn_id),
            )

    projection = UsageProjectionService(
        app.state.runtime.database,
        model_catalog=app.state.runtime_composition.model_catalog,
        timezone_name="Asia/Shanghai",
        clock=lambda: datetime(2026, 8, 5, 4, 0, tzinfo=UTC),
    ).project(turns[0].thread_id)

    assert projection.task_activity.completed_today == 1
    assert projection.task_activity.terminal_today == 2
    assert projection.task_activity.waiting == 2
    assert len(projection.task_activity.days) == 7
    assert projection.task_activity.days[-1].model_dump(mode="json") == {
        "date": "2026-08-05",
        "completed": 1,
        "terminal": 2,
    }
    assert projection.task_activity.days[-3].terminal == 1


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
    account = client.get("/api/v1/usage", headers=_headers())
    assert account.status_code == 200
    assert account.json()["today"] == response.json()["today"]
    assert client.get("/api/v1/threads/thread_missing/usage", headers=_headers()).status_code == 404


def test_account_usage_without_threads_still_has_seven_activity_days(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime-empty-account-usage.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )

    response = TestClient(app).get("/api/v1/usage", headers=_headers())

    assert response.status_code == 200
    assert response.json()["thread_id"] == "account"
    assert len(response.json()["task_activity"]["days"]) == 7


def test_usage_context_keeps_the_completed_turns_frozen_catalog_revision(
    tmp_path,
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime-frozen-usage.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    composition = app.state.runtime_composition
    original = builtin_model_catalog().get("ecorex-chat")
    assert original.model_policy is not None
    old_catalog = ManagedModelCatalog((original,))
    renamed = replace(
        original,
        display_name="EcoreX Main R2",
        model_policy=replace(
            original.model_policy,
            display_name="EcoreX Main R2",
            upstream_model_id="gpt-5.6-sol-r2",
            compact_threshold_tokens=384_000,
        ),
    )
    new_catalog = ManagedModelCatalog((renamed,))
    active = {"catalog": old_catalog}
    composition._model_catalog_provider = lambda: active["catalog"]

    old_thread, old_turn = _turn(app, title="旧修订", message_id="usage-old-revision")
    pending = UsageProjectionService(
        app.state.runtime.database,
        model_catalog=new_catalog,
    ).project(old_thread.thread_id)
    assert pending.context.used_tokens is None
    assert pending.context.model_display_name == original.display_name
    assert pending.context.model_catalog_snapshot_id == old_catalog.snapshot_id
    _append_completed(
        app.state.runtime,
        thread_id=old_thread.thread_id,
        turn_id=old_turn.turn.turn_id,
        created_at=datetime.now(UTC),
        usage={"input_tokens": 33, "output_tokens": 4},
    )
    active["catalog"] = new_catalog
    _turn(app, title="新修订", message_id="usage-new-revision")

    projection = UsageProjectionService(
        app.state.runtime.database,
        # Deliberately pass the process-current catalog: immutable history
        # must still resolve through the old Turn snapshot.
        model_catalog=new_catalog,
    ).project(old_thread.thread_id)

    assert projection.context.model_id == "ecorex-chat"
    assert projection.context.model_display_name == original.display_name
    assert projection.context.window_tokens == 272_000
    assert projection.context.model_catalog_snapshot_id == old_catalog.snapshot_id
    assert projection.context.model_catalog_snapshot_id != new_catalog.snapshot_id


def test_usage_endpoint_prefers_account_projection_and_falls_back_locally(
    tmp_path,
) -> None:
    gateway = AccountUsageGateway()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime-account-usage.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            model_gateway=gateway,
            allow_unmanaged_model_gateway_for_testing=True,
            close_model_gateway_on_shutdown=False,
        )
    )
    thread, created = _turn(app, title="跨设备用量", message_id="account-usage")
    _append_completed(
        app.state.runtime,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        created_at=datetime.now(UTC),
        usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    client = TestClient(app)

    online = client.get(
        f"/api/v1/threads/{thread.thread_id}/usage",
        headers=_headers(),
    )
    assert online.status_code == 200
    assert online.json()["scope"] == "account"
    assert online.json()["source"] == "managed_gateway"
    assert online.json()["complete_across_devices"] is True
    assert online.json()["today"]["total_tokens"] == 120
    assert online.json()["week"]["total_tokens"] == 480
    # Context remains a local, thread-specific provider fact.
    assert online.json()["context"]["used_tokens"] == 2
    assert gateway.calls == ["Asia/Shanghai"]

    gateway.coverage_started_at = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
    partial = client.get(
        f"/api/v1/threads/{thread.thread_id}/usage",
        headers=_headers(),
    )
    assert partial.status_code == 200
    assert partial.json()["scope"] == "local_device"
    assert partial.json()["today"]["total_tokens"] == 5

    gateway.available = False
    fallback = client.get(
        f"/api/v1/threads/{thread.thread_id}/usage",
        headers=_headers(),
    )
    assert fallback.status_code == 200
    assert fallback.json()["scope"] == "local_device"
    assert fallback.json()["source"] == "local_event_store"
    assert fallback.json()["complete_across_devices"] is False
    assert fallback.json()["today"]["total_tokens"] == 5
