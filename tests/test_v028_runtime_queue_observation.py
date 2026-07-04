import time
from pathlib import Path


def test_run_ledger_queued_run_gets_started_at_when_started(tmp_path):
    from agent.protocol import reset_run_ledger_for_tests

    ledger = reset_run_ledger_for_tests(tmp_path / "run-ledger.db")
    ledger.create_run("req-queued", "session-queued", status="queued", phase="queued")

    queued = ledger.get_run("req-queued")
    assert queued["status"] == "queued"
    assert queued["started_at"] is None

    ledger.mark_phase("req-queued", "starting", status="running")
    running = ledger.get_run("req-queued")
    assert running["status"] == "running"
    assert running["started_at"] is not None


def test_task_observer_emits_structured_lifecycle_events():
    from agent.protocol import TaskObserver

    events = []

    observer = TaskObserver(
        lambda event_type, payload: events.append((event_type, payload)),
        task_id="tool-call-1",
        kind="tool",
        title="imagegen",
        soft_deadline_seconds=10,
        hard_deadline_seconds=60,
        metadata={"tool_call_id": "call-1", "prompt": "private"},
        started_at=time.time() - 3,
    )

    observer.start()
    observer.heartbeat(elapsed_seconds=3)
    observer.intervention_requested(next_actions=["continue", "stop", "background"])
    observer.end("completed")

    event_types = [event_type for event_type, _payload in events]
    assert event_types == [
        "task.started",
        "task.heartbeat",
        "task.health_changed",
        "task.intervention_requested",
        "task.completed",
    ]
    assert events[0][1]["task_id"] == "tool-call-1"
    assert events[0][1]["kind"] == "tool"
    assert "prompt" not in events[0][1]
    assert events[3][1]["next_actions"] == ["continue", "stop", "background"]


def test_runtime_projection_reduces_task_observations(tmp_path):
    from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

    ledger = reset_run_event_ledger_for_tests(Path(tmp_path) / "runtime-events.db")
    request_id = "req-task-observation"
    session_id = "session-task-observation"
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        turn_id=request_id,
        event_type="run.accepted",
        payload={"request_id": request_id, "session_id": session_id},
        idempotency_key="accepted",
    )
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        turn_id=request_id,
        event_type="task.started",
        payload={"task_id": "task-1", "kind": "tool", "title": "imagegen", "health": "running"},
        idempotency_key="task-started",
    )
    ledger.append_event(
        request_id=request_id,
        session_id=session_id,
        turn_id=request_id,
        event_type="task.intervention_requested",
        payload={
            "task_id": "task-1",
            "kind": "tool",
            "title": "imagegen",
            "health": "waiting_user_decision",
            "elapsed_seconds": 1200,
            "next_actions": ["continue", "stop", "background"],
        },
        idempotency_key="task-intervention",
    )

    projection = RuntimeProjectionService(ledger).request_projection(request_id)

    assert projection["task_observations"][0]["task_id"] == "task-1"
    assert projection["task_observations"][0]["health"] == "waiting_user_decision"
    assert projection["task_observations"][0]["intervention"]["next_actions"] == ["continue", "stop", "background"]
