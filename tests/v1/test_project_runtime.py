from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ecorex.capabilities import ToolExecutionScope
from ecorex.capabilities.handlers import WorkspaceReadError, WorkspaceReadHandler
from ecorex.gateway import GatewayEvent
from ecorex.projects import ProjectWorkspaceAuthority
from ecorex.projects import picker as picker_module
from ecorex.integration.pack_process import ProcessCapabilityPackAdapter
from ecorex.runtime import AgentTurnWorker, RuntimeSettings, WorkerOutcome, create_app


TOKEN = "p" * 32
CSRF = "q" * 32
ORIGIN = "http://testserver"


class _ProjectGateway:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent(
            seq=1,
            event_type="output_text.delta",
            response_id="project-response",
            delta=self.reply,
        )
        yield GatewayEvent(
            seq=2,
            event_type="response.completed",
            response_id="project-response",
        )


def test_native_picker_process_is_hidden_and_never_uses_a_shell(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="C:\\workspace\n".encode())

    monkeypatch.setattr(picker_module.subprocess, "run", run)
    monkeypatch.setattr(picker_module, "os", SimpleNamespace(name="nt"))

    assert picker_module._run_picker(("picker.exe",)) == picker_module.Path(
        "C:\\workspace"
    )
    assert calls[0][1]["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert calls[0][1]["shell"] is False


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
    with app.state.runtime.database.reader() as connection:
        workspace_paths = connection.execute(
            "SELECT memory_path,dreams_path FROM projects WHERE project_id=?",
            (project["project_id"],),
        ).fetchone()
    assert tuple(workspace_paths) == ("MEMORY.md", "memory/dreams")

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


def test_project_workspace_authority_scopes_read_to_exact_thread_turn_and_job(
    tmp_path,
) -> None:
    database_path = tmp_path / "runtime.db"
    static_root = tmp_path / "general-workspace"
    project_root = tmp_path / "selected-project"
    other_root = tmp_path / "other-project"
    static_root.mkdir()
    project_root.mkdir()
    other_root.mkdir()
    (project_root / "brief.txt").write_text("project-one", encoding="utf-8")
    (other_root / "brief.txt").write_text("project-two", encoding="utf-8")
    selections = iter((project_root, other_root))
    app = create_app(
        settings=RuntimeSettings(
            database_path=database_path,
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            project_folder_picker=lambda: next(selections),
        )
    )
    client = TestClient(app)

    def create_project_thread_turn(label: str) -> tuple[dict, dict]:
        project = client.post(
            "/api/v1/projects/pick",
            headers=_headers(mutation=True),
            json={"client_request_id": f"pick-{label}"},
        ).json()
        thread = client.post(
            "/api/v1/threads",
            headers=_headers(mutation=True),
            json={
                "metadata": {"project_id": project["project_id"]},
                "client_request_id": f"thread-{label}",
            },
        ).json()
        turn_response = client.post(
            f"/api/v1/threads/{thread['thread_id']}/turns",
            headers=_headers(mutation=True),
            json={"input": "read brief", "client_message_id": f"message-{label}"},
        )
        assert turn_response.status_code == 202
        return thread, turn_response.json()["turn"]

    first_thread, first_turn = create_project_thread_turn("one")
    second_thread, second_turn = create_project_thread_turn("two")

    def scope(thread: dict, turn: dict) -> ToolExecutionScope:
        connection = sqlite3.connect(database_path)
        try:
            job_id = connection.execute(
                "SELECT job_id FROM jobs WHERE turn_id=? AND kind='agent_turn'",
                (turn["turn_id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        return ToolExecutionScope(
            job_id=job_id,
            thread_id=thread["thread_id"],
            turn_id=turn["turn_id"],
        )

    first_scope = scope(first_thread, first_turn)
    second_scope = scope(second_thread, second_turn)
    authority = ProjectWorkspaceAuthority(database_path)
    assert authority(first_scope) == (project_root.resolve(),)
    assert authority(second_scope) == (other_root.resolve(),)
    forged_scope = ToolExecutionScope(
        job_id=first_scope.job_id,
        thread_id=second_scope.thread_id,
        turn_id=second_scope.turn_id,
    )
    assert authority(forged_scope) == ()

    # The same resolver is bound to browser/sandbox pack handlers.  A project
    # root is deliberately first: the pack request's root[0], child cwd and
    # shell sandbox contract are all derived from this ordered tuple.
    adapter = object.__new__(ProcessCapabilityPackAdapter)
    adapter.workspace_roots = (static_root.resolve(),)
    adapter._workspace_root_resolver = authority
    assert adapter._workspace_roots_for_scope(first_scope) == (
        project_root.resolve(),
        static_root.resolve(),
    )

    reader = WorkspaceReadHandler(
        (static_root,), workspace_root_resolver=authority
    )
    first_context = SimpleNamespace(execution_scope=first_scope)
    assert reader({"path": "brief.txt"}, first_context)["content"] == "project-one"
    try:
        reader({"path": str(other_root / "brief.txt")}, first_context)
    except WorkspaceReadError as error:
        assert "outside the authorized roots" in str(error)
    else:  # pragma: no cover - explicit cross-Thread authority regression
        raise AssertionError("one project Thread read another project's file")

    (project_root / "brief.txt").unlink()
    project_root.rmdir()
    assert authority(first_scope) == ()
    try:
        reader({"path": "brief.txt"}, first_context)
    except WorkspaceReadError as error:
        assert "outside the authorized roots" in str(error)
    else:  # pragma: no cover
        raise AssertionError("removed project retained workspace authority")


def test_project_session_restores_history_and_uses_one_cow_workspace_after_restart(
    tmp_path,
) -> None:
    database_path = tmp_path / "runtime.db"
    global_workspace = tmp_path / "global-workspace"
    project_workspace = tmp_path / "project-workspace"
    global_workspace.mkdir()
    project_workspace.mkdir()
    (global_workspace / "MEMORY.md").write_text("global-only-memory", encoding="utf-8")
    (project_workspace / "MEMORY.md").write_text("project-only-memory", encoding="utf-8")

    def runtime():
        return create_app(
            settings=RuntimeSettings(
                database_path=database_path,
                runtime_bearer_token=TOKEN,
                csrf_token=CSRF,
                webui_origins=(ORIGIN,),
                workspace_root=global_workspace,
                workspace_root_resolver=ProjectWorkspaceAuthority(database_path),
                project_folder_picker=lambda: project_workspace,
            )
        )

    app = runtime()
    client = TestClient(app)
    project = client.post(
        "/api/v1/projects/pick",
        headers=_headers(mutation=True),
        json={"client_request_id": "project-cow-workspace"},
    ).json()
    thread = client.post(
        "/api/v1/threads",
        headers=_headers(mutation=True),
        json={
            "metadata": {"project_id": project["project_id"]},
            "client_request_id": "project-cow-thread",
        },
    ).json()
    first = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=_headers(mutation=True),
        json={"input": "第一轮", "client_message_id": "project-cow-first"},
    ).json()
    first_gateway = _ProjectGateway("第一轮回复")
    first_worker = AgentTurnWorker(
        app.state.runtime,
        gateway=first_gateway,
        capabilities=app.state.runtime_composition.capability_service,
        workspace_root=global_workspace,
        workspace_root_resolver=ProjectWorkspaceAuthority(database_path),
    )
    assert asyncio.run(first_worker.run_once("project-cow-first-worker")).outcome is WorkerOutcome.COMPLETED
    assert str(project_workspace.resolve()) in first_gateway.requests[0].instructions
    assert "project-only-memory" in first_gateway.requests[0].instructions
    assert "global-only-memory" not in first_gateway.requests[0].instructions

    del client, app, first_worker
    restarted = runtime()
    restarted_client = TestClient(restarted)
    second = restarted_client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=_headers(mutation=True),
        json={"input": "第二轮", "client_message_id": "project-cow-second"},
    ).json()
    assert second["turn"]["thread_id"] == first["turn"]["thread_id"]
    second_gateway = _ProjectGateway("第二轮回复")
    second_worker = AgentTurnWorker(
        restarted.state.runtime,
        gateway=second_gateway,
        capabilities=restarted.state.runtime_composition.capability_service,
        workspace_root=global_workspace,
        workspace_root_resolver=ProjectWorkspaceAuthority(database_path),
    )
    assert asyncio.run(second_worker.run_once("project-cow-second-worker")).outcome is WorkerOutcome.COMPLETED
    assert [item.content for item in second_gateway.requests[0].input_items] == [
        "第一轮",
        "第一轮回复",
        "第二轮",
    ]
    assert "project-only-memory" in second_gateway.requests[0].instructions
