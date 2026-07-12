from __future__ import annotations

from fastapi.testclient import TestClient

from ecorex.artifacts import ArtifactScope
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "r" * 32
CSRF = "c" * 32


def test_turn_freezes_output_location_and_materialization_never_exposes_host_path(
    tmp_path,
) -> None:
    documents = tmp_path / "documents"
    downloads = tmp_path / "downloads"
    workspace = tmp_path / "workspace"
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=("http://testserver",),
            output_roots={
                "documents": documents,
                "downloads": downloads,
                "workspace": workspace,
            },
            output_default_location="documents",
        )
    )
    auth = {"Authorization": f"Bearer {TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF,
    }

    with TestClient(app) as client:
        thread_id = client.post(
            "/api/v1/threads", json={"title": "输出策略"}, headers=mutation
        ).json()["thread_id"]
        first_turn_id = client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"input": "生成第一份报告", "client_message_id": "output-turn-1"},
            headers=mutation,
        ).json()["turn"]["turn_id"]

        preference = client.get("/api/v1/output/preference", headers=auth).json()
        assert preference["location_alias"] == "documents"
        updated = client.put(
            "/api/v1/output/preference",
            json={
                "location_alias": "downloads",
                "expected_revision": preference["revision"],
                "client_request_id": "output-preference-downloads",
            },
            headers=mutation,
        ).json()
        assert updated["location_alias"] == "downloads"

        first = app.state.artifact_service.create_artifact(
            b"%PDF-1.4 first",
            requested_name="第一份报告.pdf",
            mime_type="application/pdf",
            scope=ArtifactScope(
                account_id="local-user",
                thread_id=thread_id,
                turn_id=first_turn_id,
            ),
        )
        first_receipt = client.post(
            f"/api/v1/output/artifacts/{first.artifact_id}/materialize",
            json={
                "revision_id": first.revision_id,
                "client_request_id": "materialize-first",
            },
            headers=mutation,
        )
        assert first_receipt.status_code == 200
        assert first_receipt.json()["location_alias"] == "documents"
        assert "path" not in first_receipt.json()
        assert (documents / first_receipt.json()["display_name"]).read_bytes() == b"%PDF-1.4 first"

        second_turn_id = client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"input": "生成第二份报告", "client_message_id": "output-turn-2"},
            headers=mutation,
        ).json()["turn"]["turn_id"]
        second = app.state.artifact_service.create_artifact(
            b"%PDF-1.4 second",
            requested_name="第二份报告.pdf",
            mime_type="application/pdf",
            scope=ArtifactScope(
                account_id="local-user",
                thread_id=thread_id,
                turn_id=second_turn_id,
            ),
        )
        second_receipt = client.post(
            f"/api/v1/output/artifacts/{second.artifact_id}/materialize",
            json={
                "revision_id": second.revision_id,
                "client_request_id": "materialize-second",
            },
            headers=mutation,
        )
        assert second_receipt.status_code == 200
        assert second_receipt.json()["location_alias"] == "downloads"
        assert (downloads / second_receipt.json()["display_name"]).read_bytes() == b"%PDF-1.4 second"
