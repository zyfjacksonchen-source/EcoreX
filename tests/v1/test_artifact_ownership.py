from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.artifacts import (
    ArtifactLineage,
    ArtifactNotFound,
    ArtifactScope,
    ArtifactService,
)
from ecorex.artifacts.api import ArtifactApiEvent, create_artifact_router


class Sink:
    def __init__(self) -> None:
        self.events: list[ArtifactApiEvent] = []

    def persist_in_transaction(self, _connection, _event: ArtifactApiEvent) -> None:
        return None

    async def publish_persisted(self, event: ArtifactApiEvent) -> None:
        self.events.append(event)


def _client(service: ArtifactService, account_id: str, sink: Sink | None = None):
    app = FastAPI()
    app.include_router(
        create_artifact_router(service, account_id=account_id, event_sink=sink)
    )
    return TestClient(app)


def test_artifact_api_is_account_and_thread_scoped(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    a = service.create_artifact(
        b"a",
        requested_name="a.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(account_id="account-a", thread_id="thread-a", turn_id="turn-a"),
    )
    b = service.create_artifact(
        b"b",
        requested_name="b.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(account_id="account-b", thread_id="thread-b", turn_id="turn-b"),
    )

    client_a = _client(service, "account-a")
    assert [item["artifact_id"] for item in client_a.get("/api/v1/artifacts").json()["items"]] == [a.artifact_id]
    assert client_a.get(f"/api/v1/artifacts/{b.artifact_id}").status_code == 404
    assert client_a.get("/api/v1/artifacts", params={"thread_id": "thread-b"}).json()["count"] == 0
    assert client_a.get("/api/v1/artifacts", params={"thread_id": "thread-a"}).json()["count"] == 1


def test_cross_account_lineage_is_rejected_without_revealing_source(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    source = service.create_artifact(
        b"source",
        requested_name="source.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(account_id="account-a"),
    )
    with pytest.raises(ArtifactNotFound):
        service.create_artifact(
            b"derived",
            requested_name="derived.pdf",
            mime_type="application/pdf",
            lineage=ArtifactLineage(source_artifact_ids=(source.artifact_id,)),
            scope=ArtifactScope(account_id="account-b"),
        )


def test_feedback_event_carries_backend_artifact_scope(tmp_path) -> None:
    service = ArtifactService(tmp_path)
    sink = Sink()
    artifact = service.create_artifact(
        b"report",
        requested_name="report.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(
            account_id="account-a",
            thread_id="thread-a",
            turn_id="turn-a",
            created_by_tool_id="office.pdf",
        ),
    )
    client = _client(service, "account-a", sink)
    response = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json={
            "revision_id": artifact.revision_id,
            "signal": "thumbs_up",
            "client_request_id": "feedback-1",
        },
    )
    assert response.status_code == 200
    assert len(sink.events) == 1
    event = sink.events[0]
    assert (event.account_id, event.thread_id, event.turn_id) == (
        "account-a",
        "thread-a",
        "turn-a",
    )
