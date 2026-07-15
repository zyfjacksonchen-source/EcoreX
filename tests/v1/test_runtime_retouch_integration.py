from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ecorex.artifacts import (
    ArtifactScope,
    InspectionRegion,
    QualityEvidence,
    QualityStatus,
)
from ecorex.integration import (
    StructuredRetouchAdapterRequest,
    StructuredRetouchAdapterResult,
)
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.protocol import TurnStatus


TOKEN = "runtime-token-" + "r" * 32
CSRF = "csrf-token-" + "c" * 32
ORIGIN = "http://testserver"
SOURCE = b"\x89PNG\r\n\x1a\nSOURCE"
RESULT = b"\x89PNG\r\n\x1a\nRESULT"


class RetouchAdapter:
    def __init__(self) -> None:
        self.requests: list[StructuredRetouchAdapterRequest] = []
        self.closed = 0

    async def edit(
        self, request: StructuredRetouchAdapterRequest
    ) -> StructuredRetouchAdapterResult:
        self.requests.append(request)
        return StructuredRetouchAdapterResult(
            result_id="managed-result-one",
            content=RESULT,
            mime_type="image/png",
            requested_name="poster-retouched.png",
            change_summary="已修正标注区域",
            inspection_regions=(
                InspectionRegion(
                    {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                    "已检查标注区域",
                ),
            ),
            quality_evidence=QualityEvidence(status=QualityStatus.PASSED),
        )

    async def recover(self, _idempotency_key: str):
        return None

    async def aclose(self) -> None:
        self.closed += 1


def headers(*, mutate: bool = False) -> dict[str, str]:
    result = {"Authorization": f"Bearer {TOKEN}"}
    if mutate:
        result.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF})
    return result


def _source_image(app, client: TestClient):
    thread = client.post(
        "/api/v1/threads",
        json={"client_request_id": "thread-one"},
        headers=headers(mutate=True),
    ).json()
    turn = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        json={
            "input": "创建海报",
            "agent_model_id": "ecorex-chat",
            "image_model_id": "gpt-image-2",
            "client_message_id": "message-one",
        },
        headers=headers(mutate=True),
    ).json()["turn"]
    kernel = app.state.runtime
    leased = kernel.jobs.lease_next("source-worker", kinds=["agent_turn"])
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, "source-worker", leased.lease_token)
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
        TurnStatus.FINALIZING,
    ):
        kernel.transition_turn(turn["turn_id"], status)
    kernel.finish_turn_job(
        job_id=leased.job_id,
        worker_id="source-worker",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    artifact = app.state.artifact_service.create_artifact(
        SOURCE,
        requested_name="poster.png",
        mime_type="image/png",
        scope=ArtifactScope(
            account_id="local-user",
            thread_id=thread["thread_id"],
            turn_id=turn["turn_id"],
            created_by_tool_id="imagegen",
        ),
    )
    return thread, turn, artifact


def test_runtime_supervises_retouch_to_new_revision_and_public_turn_item(tmp_path) -> None:
    adapter = RetouchAdapter()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
                webui_origins=(ORIGIN,),
                installed_capability_packs=frozenset({"image"}),
                retouch_adapter=adapter,
            retouch_worker_poll_seconds=0.01,
        )
    )
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap", headers=headers()).json()
        assert bootstrap["retouch_service"] == {"state": "ready", "reason": None}
        thread, source_turn, artifact = _source_image(app, client)
        response = client.post(
            f"/api/v1/artifacts/{artifact.artifact_id}/retouch",
            headers=headers(mutate=True),
                json={
                    "base_revision_id": artifact.revision_id,
                    "selected_artifact_ids": [artifact.artifact_id],
                    "agent_model_id": "ecorex-chat",
                    "image_model_id": "gpt-image-2",
                "annotations": [
                    {
                        "kind": "rectangle",
                        "normalized_geometry": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.2,
                            "height": 0.2,
                        },
                        "instruction": "修正文字",
                    }
                ],
                "reference_artifact_ids": [],
                "global_instruction": "其他区域保持不变",
                "client_request_id": "retouch-one",
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(
                f"/api/v1/retouch-jobs/{job_id}", headers=headers()
            ).json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["result_revision_id"] != artifact.revision_id
        assert job["change_summary"] == "已修正标注区域"
        internal = app.state.artifact_service.get_internal_retouch_job(job_id)
        assert internal.execution_turn_id != source_turn["turn_id"]
        assert app.state.runtime.get_turn(internal.execution_turn_id).status is TurnStatus.COMPLETED
        projection = client.get(
            f"/api/v1/threads/{thread['thread_id']}/projection",
            headers=headers(),
        ).json()
        artifact_items = [
            item for item in projection["items"] if item["kind"] == "artifact"
        ]
        assert len(artifact_items) == 1
        assert artifact_items[0]["turn_id"] == internal.execution_turn_id
        assert (
            artifact_items[0]["content"]["artifact"]["revision_id"]
            == job["result_revision_id"]
        )
        assert not any(
            item["kind"] == "artifact" and item["turn_id"] == source_turn["turn_id"]
            for item in projection["items"]
        )
        retouch_turn = next(
            turn
            for turn in projection["turns"]
            if turn["turn_id"] == internal.execution_turn_id
        )
        assert retouch_turn["status"] == "completed"
        retouch_message = next(
            item
            for item in projection["items"]
            if item["turn_id"] == internal.execution_turn_id
            and item["kind"] == "message"
        )
        assert retouch_message["content"]["metadata"]["operation"] == "artifact_retouch"
        with app.state.runtime.database.reader() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE turn_id = ? AND kind = 'agent_turn'",
                (internal.execution_turn_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE turn_id = ? AND kind = 'artifact_retouch'",
                (internal.execution_turn_id,),
            ).fetchone()[0] == 1
        replay = client.get(
            f"/api/v1/threads/{thread['thread_id']}/replay",
            headers=headers(),
        ).json()["projection"]
        assert next(
            turn
            for turn in replay["turns"]
            if turn["turn_id"] == internal.execution_turn_id
        )["status"] == "completed"
        assert next(
            item
            for item in replay["items"]
            if item["item_id"] == artifact_items[0]["item_id"]
        )["content"] == artifact_items[0]["content"]
        assert len(adapter.requests) == 1
        assert not hasattr(adapter.requests[0], "prompt")
    assert adapter.closed == 1


def test_runtime_without_managed_retouch_fails_before_creating_orphan_job(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap", headers=headers()).json()
        assert bootstrap["retouch_service"] == {
            "state": "unavailable",
            "reason": "image_capability_pack_not_installed",
        }
        _thread, _turn, artifact = _source_image(app, client)
        response = client.post(
            f"/api/v1/artifacts/{artifact.artifact_id}/retouch",
            headers=headers(mutate=True),
                json={
                    "base_revision_id": artifact.revision_id,
                    "selected_artifact_ids": [artifact.artifact_id],
                    "agent_model_id": "ecorex-chat",
                    "image_model_id": "gpt-image-2",
                "annotations": [],
                "reference_artifact_ids": [],
                "global_instruction": "调整图片",
                "client_request_id": "retouch-unavailable",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ARTIFACT_ACTION_UNAVAILABLE"
        with app.state.runtime.database.reader() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='artifact_retouch'"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM artifact_retouch_jobs"
            ).fetchone()[0] == 0


def test_runtime_rejects_unverified_retouch_adapter_before_startup(tmp_path) -> None:
    with pytest.raises(ValueError, match="verified image capability pack"):
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                artifact_root=tmp_path / "artifacts",
                runtime_bearer_token=TOKEN,
                csrf_token=CSRF,
                webui_origins=(ORIGIN,),
                retouch_adapter=RetouchAdapter(),
            )
        )
    assert not (tmp_path / "runtime.db").exists()
