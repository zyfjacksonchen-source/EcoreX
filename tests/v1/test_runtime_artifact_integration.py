from __future__ import annotations

from fastapi.testclient import TestClient

from ecorex.artifacts import ArtifactScope
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "r" * 32
CSRF = "c" * 32
ORIGIN = "http://testserver"


def test_artifact_router_uses_runtime_security_scope_outbox_and_event_stream(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    mutation = {**auth, "Origin": ORIGIN, "X-EcoreX-CSRF": CSRF}

    assert client.get("/api/v1/artifacts").status_code == 401
    thread = client.post("/api/v1/threads", json={}, headers=mutation).json()
    turn = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={"input": "生成报告", "client_message_id": "message-1"},
        headers=mutation,
    ).json()["turn"]
    artifact = app.state.artifact_service.create_artifact(
        b"report",
        requested_name="report.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(
            account_id="local-user",
            thread_id=thread["thread_id"],
            turn_id=turn["turn_id"],
            created_by_tool_id="office.pdf",
        ),
    )

    listed = client.get(
        "/api/v1/artifacts",
        params={"thread_id": thread["thread_id"]},
        headers=auth,
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["artifact_id"] == artifact.artifact_id
    payload = {
        "revision_id": artifact.revision_id,
        "signal": "thumbs_up",
        "client_request_id": "feedback-1",
    }
    first = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json=payload,
        headers=mutation,
    )
    duplicate = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json=payload,
        headers=mutation,
    )
    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert app.state.artifact_event_outbox.pending() == ()

    events = client.get(
        f"/api/v1/threads/{thread['thread_id']}/events",
        headers=auth,
    ).json()["events"]
    artifact_events = [
        event
        for event in events
        if event["event_type"] == "artifact.feedback.recorded"
    ]
    assert len(artifact_events) == 1
    event = artifact_events[0]
    accepted = next(item for item in events if item["event_type"] == "turn.accepted")
    assert event["payload"]["artifact_id"] == artifact.artifact_id
    assert event["turn_id"] == turn["turn_id"]
    assert event["config_snapshot_id"] == accepted["config_snapshot_id"]
    assert event["capability_snapshot_id"] == accepted["capability_snapshot_id"]
    assert event["permission_snapshot_id"] == accepted["permission_snapshot_id"]
