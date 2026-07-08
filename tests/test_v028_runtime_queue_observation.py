from collections import deque
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


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
    assert running.get("lease_owner") in ("", None)


def test_run_ledger_claims_queued_run_once_until_release_or_expiry(tmp_path):
    from agent.protocol import reset_run_ledger_for_tests

    ledger = reset_run_ledger_for_tests(tmp_path / "run-ledger-claim.db")
    ledger.create_run("req-claim", "session-claim", status="queued", phase="queued")

    assert ledger.claim_queued_run("req-claim", owner="worker-a", lease_seconds=60) is True
    assert ledger.claim_queued_run("req-claim", owner="worker-b", lease_seconds=60) is False

    claimed = ledger.get_run("req-claim")
    assert claimed["lease_owner"] == "worker-a"
    assert claimed["lease_expires_at"] is not None

    ledger.release_queued_claim("req-claim", owner="worker-a")
    assert ledger.claim_queued_run("req-claim", owner="worker-b", lease_seconds=60) is True

    ledger.mark_phase("req-claim", "starting", status="running")
    running = ledger.get_run("req-claim")
    assert running["status"] == "running"
    assert running["lease_owner"] is None


def test_queue_guide_reinserts_observed_payload_without_preempting(tmp_path):
    from agent.protocol import reset_run_ledger_for_tests
    from channel.web import web_channel

    ledger = reset_run_ledger_for_tests(tmp_path / "run-ledger-guide.db")
    session_id = "session-guide-queue"
    request_id = "req-guide-queue"

    try:
        with patch.object(web_channel, "_get_workspace_root", return_value=str(tmp_path)):
            channel = web_channel.WebChannel()
            payload = {
                "request_id": request_id,
                "session_id": session_id,
                "prompt": "queued guidance",
                "visible_prompt": "queued guidance",
                "visible_message": "queued guidance",
                "use_sse": True,
            }
            assert channel._persist_queued_payload(payload) is True
            ledger.create_run(request_id, session_id, status="queued", phase="queued")
            channel.session_run_queues.clear()

            with patch.object(channel, "_active_request_ids_for_session", return_value=["req-current-running"]):
                result = channel._guide_queued_request(request_id, expected_session_id=session_id)

        assert result["status"] == "success"
        assert result["state"] == "queued"
        assert result["queue_position"] == 1
        assert result["inserted"] is True
        assert "req-current-running" in result["active_request_ids"]
        assert ledger.get_run(request_id)["status"] == "queued"
    finally:
        reset_run_ledger_for_tests(tmp_path / "run-ledger-guide-reset.db")


def test_queue_guide_does_not_reinsert_terminal_request(tmp_path):
    from agent.protocol import reset_run_ledger_for_tests
    from channel.web import web_channel

    ledger = reset_run_ledger_for_tests(tmp_path / "run-ledger-guide-terminal.db")
    session_id = "session-guide-terminal"
    request_id = "req-guide-terminal"

    try:
        with patch.object(web_channel, "_get_workspace_root", return_value=str(tmp_path)):
            channel = web_channel.WebChannel()
            payload = {
                "request_id": request_id,
                "session_id": session_id,
                "prompt": "terminal guidance",
                "visible_prompt": "terminal guidance",
                "visible_message": "terminal guidance",
                "use_sse": True,
            }
            assert channel._persist_queued_payload(payload) is True
            ledger.create_run(request_id, session_id, status="queued", phase="queued")
            ledger.mark_terminal(request_id, "completed", reason="done")
            channel.session_run_queues[session_id] = deque([request_id])

            result = channel._guide_queued_request(request_id, expected_session_id=session_id)

            assert result["status"] == "success"
            assert result["state"] == "completed"
            assert result["queue_position"] == 0
            assert session_id not in channel.session_run_queues
            assert not channel._queued_payload_store().exists(request_id)
    finally:
        reset_run_ledger_for_tests(tmp_path / "run-ledger-guide-terminal-reset.db")


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
            "job_id": "image-job-task-observation",
            "progress": 0.5,
            "elapsed_seconds": 1200,
            "next_actions": ["continue", "stop", "background"],
        },
        idempotency_key="task-intervention",
    )

    projection = RuntimeProjectionService(ledger).request_projection(request_id)

    assert projection["task_observations"][0]["task_id"] == "task-1"
    assert projection["task_observations"][0]["health"] == "waiting_user_decision"
    assert projection["task_observations"][0]["job_id"] == "image-job-task-observation"
    assert projection["task_observations"][0]["progress"] == 0.5
    assert projection["task_observations"][0]["intervention"]["next_actions"] == ["continue", "stop", "background"]


def test_image_job_service_emits_job_level_task_observations(tmp_path):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

    ledger = reset_run_event_ledger_for_tests(Path(tmp_path) / "runtime-events.db")
    service = ImageJobService(ledger)

    def runner(task, emit_progress, _cancel_event):
        emit_progress("provider_request", progress=0.2, detail={"provider": "test", "source": "image_job_service"})
        return {"kind": "image", "title": "image.png", "path": "image.png"}

    service.start(
        request_id="req-image-observed",
        session_id="session-image-observed",
        job_id="image-job-observed",
        tasks=[{"task_id": "task-1"}],
        runner=runner,
        synchronous=True,
    )

    events = ledger.events_for_request("req-image-observed", limit=0)
    event_types = [event["event_type"] for event in events]
    projection = RuntimeProjectionService(ledger).request_projection("req-image-observed")

    assert event_types[0] == "image_job.started"
    assert event_types[-1] == "image_job.completed"
    assert "task.started" in event_types
    assert "task.heartbeat" in event_types
    assert "task.completed" in event_types
    observation = projection["task_observations"][0]
    assert observation["task_id"] == "image-job-observed"
    assert observation["kind"] == "image_job"
    assert observation["health"] == "completed"
    assert observation["job_id"] == "image-job-observed"


def test_image_job_observation_requests_intervention_and_accepts_background_action(tmp_path):
    from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests

    ledger = reset_run_event_ledger_for_tests(Path(tmp_path) / "runtime-events.db")
    service = ImageJobService(ledger)
    started = False

    def runner(_task, _emit_progress, cancel_event):
        nonlocal started
        started = True
        deadline = time.time() + 1.0
        while time.time() < deadline and not cancel_event.is_set():
            time.sleep(0.01)
        return {"kind": "image", "title": "slow.png", "path": "slow.png"}

    service.start(
        request_id="req-image-intervention",
        session_id="session-image-intervention",
        job_id="image-job-intervention",
        tasks=[{"task_id": "task-1"}],
        runner=runner,
        metadata={
            "observation_soft_deadline_seconds": 0.05,
            "observation_stall_seconds": 0.05,
            "observation_watchdog_interval_seconds": 0.01,
            "observation_heartbeat_seconds": 0.02,
        },
    )

    deadline = time.time() + 2.0
    events = []
    while time.time() < deadline:
        events = ledger.events_for_request("req-image-intervention", limit=0)
        if any(event["event_type"] == "task.intervention_requested" for event in events):
            break
        time.sleep(0.02)

    assert started
    assert any(event["event_type"] == "task.intervention_requested" for event in events)

    action_result = service.observation_action("image-job-intervention", action="background")
    service.cancel("image-job-intervention", reason="user_stop")
    service.collect("image-job-intervention", wait=True, timeout=2)
    events = ledger.events_for_request("req-image-intervention", limit=0)

    assert action_result["observation_applied"] is True
    assert any(
        event["event_type"] == "task.health_changed"
        and event["payload"].get("health") == "backgrounded"
        for event in events
    )


def test_image_job_observation_uses_two_minute_single_image_baseline(tmp_path):
    from agent.protocol import image_job_service as ijs

    state = ijs._ImageJobState(
        job_id="image-job-baseline",
        request_id="req-image-baseline",
        session_id="session-image-baseline",
    )

    with patch.object(ijs, "conf", return_value={}):
        ijs._apply_observation_policy(state, {"task_count": 1, "effective_max_parallel": 1})

    assert state.observation_per_image_baseline_seconds == 120.0
    assert state.observation_soft_deadline_seconds == 120.0
    assert state.observation_stall_seconds == 120.0
    assert state.observation_hard_deadline_seconds == 240.0
    assert state.observation_heartbeat_seconds == 30.0


def test_image_job_observation_scales_batch_deadline_by_parallel_waves(tmp_path):
    from agent.protocol import image_job_service as ijs

    state = ijs._ImageJobState(
        job_id="image-job-batch-baseline",
        request_id="req-image-batch-baseline",
        session_id="session-image-batch-baseline",
    )

    with patch.object(ijs, "conf", return_value={}):
        ijs._apply_observation_policy(state, {"task_count": 7, "effective_max_parallel": 2})

    assert state.observation_soft_deadline_seconds == 480.0
    assert state.observation_hard_deadline_seconds == 600.0
    assert state.observation_stall_seconds == 120.0


def test_image_job_parallelism_defaults_batch_to_two_lanes():
    from agent.protocol import image_job_service as ijs

    policy = ijs.resolve_image_job_parallelism_policy({}, 7, config={})

    assert policy["parallelism_policy_version"] == "v1"
    assert policy["parallelism_defaulted"] is True
    assert policy["default_max_parallel"] == 2
    assert policy["requested_max_parallel"] == 2
    assert policy["effective_max_parallel"] == 2
    assert policy["parallelism_clamped"] is False


def test_image_job_parallelism_keeps_single_image_on_one_lane():
    from agent.protocol import image_job_service as ijs

    policy = ijs.resolve_image_job_parallelism_policy({}, 1, config={})

    assert policy["parallelism_defaulted"] is True
    assert policy["requested_max_parallel"] == 1
    assert policy["effective_max_parallel"] == 1


def test_image_job_parallelism_provider_cap_clamps_default_batch_lanes():
    from agent.protocol import image_job_service as ijs

    policy = ijs.resolve_image_job_parallelism_policy(
        {},
        7,
        config={"image_provider_concurrency": 1},
    )

    assert policy["parallelism_defaulted"] is True
    assert policy["requested_max_parallel"] == 2
    assert policy["effective_max_parallel"] == 1
    assert policy["parallelism_clamped"] is True
    assert policy["parallelism_clamp_reason"] == "provider_max_parallel"


def test_image_job_status_events_extend_observation_deadline(tmp_path):
    from agent.protocol import ImageJobService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as ijs

    ledger = reset_run_event_ledger_for_tests(Path(tmp_path) / "runtime-events-extend.db")
    service = ImageJobService(ledger)
    state = ijs._ImageJobState(
        job_id="image-job-extend",
        request_id="req-image-extend",
        session_id="session-image-extend",
        turn_id="turn-image-extend",
    )
    now = time.monotonic()
    state.observation_started_at = now - 115.0
    state.observation_started_wall_time = time.time() - 115.0
    state.observation_last_progress_at = now - 115.0
    state.observation_last_heartbeat_at = now - 115.0
    state.observation_per_image_baseline_seconds = 120.0
    state.observation_soft_deadline_seconds = 120.0
    state.observation_hard_deadline_seconds = 240.0

    service._emit_observation_progress(state, status="provider_polling", progress=0.4, index=0)

    events = ledger.events_for_request("req-image-extend", limit=0)
    heartbeat = events[-1]["payload"]
    assert heartbeat["image_job_status"] == "provider_polling"
    assert heartbeat["deadline_extended"] is True
    assert heartbeat["soft_deadline_seconds"] >= 230
    assert state.observation_soft_deadline_seconds >= 230
