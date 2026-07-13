from __future__ import annotations

from fastapi.testclient import TestClient

from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "p" * 32
CSRF = "q" * 32
ORIGIN = "http://testserver"


def _headers(*, mutation: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if mutation:
        headers.update({"Origin": ORIGIN, "X-EcoreX-CSRF": CSRF})
    return headers


def test_project_picker_is_backend_authoritative_and_binds_new_threads(tmp_path) -> None:
    selected = tmp_path / "季度资料"
    selected.mkdir()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            project_folder_picker=lambda: selected,
        )
    )
    client = TestClient(app)

    assert client.get("/api/v1/projects", headers=_headers()).json() == {"projects": []}
    created = client.post(
        "/api/v1/projects/pick",
        headers=_headers(mutation=True),
        json={"client_request_id": "project_pick_1"},
    )
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "季度资料"
    assert project["project_path"] == str(selected.resolve())
    assert project["thread_count"] == 0

    duplicate = client.post(
        "/api/v1/projects/pick",
        headers=_headers(mutation=True),
        json={"client_request_id": "project_pick_2"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["project_id"] == project["project_id"]

    thread = client.post(
        "/api/v1/threads",
        headers=_headers(mutation=True),
        json={
            "title": None,
            "metadata": {
                "conversation_kind": "project",
                "project_id": project["project_id"],
                "project_name": "forged-name",
            },
            "client_request_id": "project_thread_1",
        },
    )
    assert thread.status_code == 201
    assert thread.json()["metadata"] == {
        "conversation_kind": "project",
        "project_id": project["project_id"],
        "project_name": "季度资料",
    }

    catalog = client.get("/api/v1/projects", headers=_headers()).json()
    assert catalog["projects"][0]["thread_count"] == 1


def test_unknown_project_cannot_be_forged_into_thread_metadata(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            project_folder_picker=lambda: tmp_path,
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/threads",
        headers=_headers(mutation=True),
        json={
            "metadata": {"project_id": "prj_missing"},
            "client_request_id": "project_thread_missing",
        },
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "project_not_found"}
