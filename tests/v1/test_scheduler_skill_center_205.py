from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import io
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.skills.manager import SkillManager
from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.scheduler_tool import SchedulerTool
from agent.tools.scheduler.task_store import TaskStore
from bridge.context import Context
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.skill_hub import SkillHubRegistry, create_skill_hub_router
from ecorex.extensions import LocalSkillBundleStore, SQLiteExtensionRepository
from ecorex.extensions.api import register_extension_routes
from ecorex.extensions.hub_api import register_skill_hub_runtime_routes
from ecorex.extensions.live_authority import (
    bind_live_extension_service,
    live_extension_skill_roots,
)
from ecorex.extensions.service import ExtensionService
from agent.tools.tool_manager import ToolManager


def _skill_bundle(
    name: str = "shared-helper",
    version: str = "1.0.0",
    reply: str = "SHARED-SKILL-OK",
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: Shared fixture helper.\n"
            f"version: {version}\n---\n\nReply with {reply}.\n",
        )
    return output.getvalue()


def test_scheduler_edit_run_now_and_persist_full_local_lifecycle(tmp_path: Path) -> None:
    store_path = tmp_path / "workspace" / "scheduler" / "tasks.json"
    store = TaskStore(str(store_path))
    tool = SchedulerTool({"channel_type": "web"})
    tool.task_store = store
    tool.current_context = Context(
        kwargs={
            "thread_id": "thread-1",
            "session_id": "thread-1",
            "receiver": "thread-1",
        }
    )
    executed: list[str] = []
    service = SchedulerService(
        store,
        lambda task: executed.append(task["action"]["content"]) or True,
    )
    tool.scheduler_service = service

    created = tool.execute(
        {
            "action": "create",
            "name": "draft reminder",
            "message": "draft",
            "schedule_type": "interval",
            "schedule_value": "3600",
        }
    )
    assert created.status == "success"
    task_id = store.list_tasks()[0]["id"]

    edited = tool.execute(
        {
            "action": "edit",
            "task_id": task_id,
            "name": "final reminder",
            "message": "final",
            "schedule_type": "once",
            "schedule_value": "+1h",
        }
    )
    assert edited.status == "success"
    task = store.get_task(task_id)
    assert task["name"] == "final reminder"
    assert task["action"]["content"] == "final"
    assert task["schedule"]["type"] == "once"

    assert tool.execute({"action": "disable", "task_id": task_id}).status == "success"
    assert store.get_task(task_id)["enabled"] is False
    assert tool.execute({"action": "enable", "task_id": task_id}).status == "success"
    assert store.get_task(task_id)["enabled"] is True
    next_run = store.get_task(task_id)["next_run_at"]

    assert tool.execute({"action": "run_now", "task_id": task_id}).status == "success"
    assert executed == ["final"]
    assert store.get_task(task_id)["next_run_at"] == next_run
    assert store.get_task(task_id)["last_run_at"]

    reopened = TaskStore(str(store_path))
    assert reopened.get_task(task_id)["name"] == "final reminder"
    assert tool.execute({"action": "delete", "task_id": task_id}).status == "success"
    assert reopened.get_task(task_id) is None


def test_scheduler_due_failure_retries_without_duplicate_state_advance(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "workspace" / "scheduler" / "tasks.json"))
    due = (datetime.now() - timedelta(seconds=1)).isoformat()
    store.add_task(
        {
            "id": "retry-task",
            "name": "retry task",
            "enabled": True,
            "schedule": {"type": "interval", "seconds": 60},
            "action": {"type": "send_message", "content": "once per attempt"},
            "next_run_at": due,
        }
    )
    outcomes = iter((False, True))
    calls: list[str] = []
    service = SchedulerService(
        store,
        lambda task: calls.append(task["id"]) or next(outcomes),
    )

    service._check_and_execute_tasks()
    first = store.get_task("retry-task")
    assert calls == ["retry-task"]
    assert first["next_run_at"] == due
    assert "last_run_at" not in first

    service._check_and_execute_tasks()
    second = store.get_task("retry-task")
    assert calls == ["retry-task", "retry-task"]
    assert second["next_run_at"] != due
    assert second["last_run_at"]


def test_scheduler_recovers_last_good_snapshot_after_interrupted_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace" / "scheduler" / "tasks.json"
    store = TaskStore(str(path))
    store.add_task({"id": "safe", "name": "first", "enabled": True})
    store.update_task("safe", {"name": "second"})
    path.write_text('{"tasks":', encoding="utf-8")

    recovered = store.get_task("safe")
    assert recovered["name"] == "first"
    assert TaskStore(str(path)).get_task("safe")["name"] == "first"


def test_enabled_local_skill_is_discovered_by_cow_and_disable_removes_it(
    tmp_path: Path,
) -> None:
    store = LocalSkillBundleStore(tmp_path / "extension-cas")
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
        local_bundle_store=store,
    )
    bind_live_extension_service(service)
    manager = SkillManager(
        builtin_dir=str(tmp_path / "empty-builtins"),
        custom_dir=str(tmp_path / "workspace" / "skills"),
    )
    assert manager.filter_skills() == []
    staged = service.install_local_skill_zip(
        _skill_bundle(),
        client_request_id="install-shared-helper-1",
    )
    assert live_extension_skill_roots() == ()

    enabled = asyncio.run(
        service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable-shared-helper-1",
        )
    )
    roots = live_extension_skill_roots()
    assert len(roots) == 1
    assert (Path(roots[0]) / "SKILL.md").is_file()

    manager.refresh_skills()
    assert manager.is_skill_enabled("shared-helper") is True
    assert [entry.skill.name for entry in manager.filter_skills()] == ["shared-helper"]
    assert "shared-helper" in manager.build_skills_prompt()
    assert "SHARED-SKILL-OK" in manager.get_skill("shared-helper").skill.content

    service.disable(
        enabled.extension_id,
        expected_revision=enabled.revision,
        client_request_id="disable-shared-helper-1",
    )
    assert live_extension_skill_roots() == ()
    manager.refresh_skills()
    assert manager.filter_skills() == []
    assert manager.get_skill("shared-helper") is None


def test_skill_switch_does_not_hide_cow_first_party_tools(tmp_path: Path) -> None:
    manager = ToolManager(workspace_root=tmp_path / "workspace")
    manager.load_tools(start_mcp=False)
    before = set(manager.list_tools())
    assert {
        "office_documents", "office_pdf", "office_presentations",
        "office_spreadsheets",
    } <= before
    store = LocalSkillBundleStore(tmp_path / "extension-cas")
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
        local_bundle_store=store,
    )
    bind_live_extension_service(service)
    staged = service.install_local_skill_zip(
        _skill_bundle(), client_request_id="install-tool-isolation-1"
    )
    enabled = asyncio.run(service.enable(
        staged.extension_id,
        expected_revision=staged.revision,
        client_request_id="enable-tool-isolation-1",
    ))
    service.disable(
        enabled.extension_id,
        expected_revision=enabled.revision,
        client_request_id="disable-tool-isolation-1",
    )
    assert set(manager.list_tools()) == before


def test_second_account_discovers_installs_and_calls_uploaded_skill(tmp_path: Path) -> None:
    principals = {
        "current": ControlPrincipal(
            subject="publisher", client_id="device-a", account_id="account-a"
        )
    }
    hub = SkillHubRegistry(tmp_path / "hub.db", author_key=b"h" * 32)
    hub_store = LocalSkillBundleStore(tmp_path / "hub-cas")
    hub_app = FastAPI()
    hub_app.include_router(
        create_skill_hub_router(
            hub,
            hub_store,
            principal_dependency=lambda: principals["current"],
            nickname_resolver=lambda _account_id: "e-Mate user",
        )
    )
    hub_client = TestClient(hub_app)
    bundle = _skill_bundle()
    encoded = base64.b64encode(bundle).decode("ascii")
    uploaded = hub_client.post(
        "/ecorex-agent/client/skill-hub/v1/skills",
        json={
            "slug": "shared-helper",
            "category": "office_productivity",
            "bundle_base64": encoded,
            "client_request_id": "publish-shared-helper-1",
        },
    )
    assert uploaded.status_code == 201
    assert "account-a" not in uploaded.text
    duplicate = hub_client.post(
        "/ecorex-agent/client/skill-hub/v1/skills",
        json={
            "slug": "shared-helper",
            "category": "office_productivity",
            "bundle_base64": base64.b64encode(
                _skill_bundle(reply="DIFFERENT-CONTENT")
            ).decode("ascii"),
            "client_request_id": "publish-shared-helper-conflict-1",
        },
    )
    assert duplicate.status_code == 409

    principals["current"] = ControlPrincipal(
        subject="installer", client_id="device-b", account_id="account-b"
    )
    discovered = hub_client.get(
        "/ecorex-agent/client/skill-hub/v1/skills", params={"query": "shared"}
    )
    assert [item["slug"] for item in discovered.json()["items"]] == ["shared-helper"]
    card = uploaded.json()
    intent = hub_client.post(
        "/ecorex-agent/client/skill-hub/v1/skills/shared-helper/versions/1.0.0/install-intent",
        json={
            "package_sha256": card["package_sha256"],
            "client_request_id": "install-shared-helper-b-1",
        },
    ).json()
    claimed = hub_client.post(
        "/ecorex-agent/client/skill-hub/v1/install-intents/consume",
        json={"install_intent": intent["install_intent"]},
    ).json()
    download = hub_client.get(
        "/ecorex-agent/client/skill-hub/v1/skills/shared-helper/versions/1.0.0/package"
    )
    assert download.headers["x-skill-content-sha256"] == card["package_sha256"]

    local_service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "account-b" / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
        local_bundle_store=LocalSkillBundleStore(tmp_path / "account-b" / "cas"),
    )
    local_app = FastAPI()
    register_extension_routes(local_app, local_service)
    installed = TestClient(local_app).post(
        "/api/v1/extensions/local-skills",
        json={
            "bundle_base64": base64.b64encode(download.content).decode("ascii"),
            "client_request_id": "local-install-shared-b-1",
        },
    )
    assert installed.status_code == 201
    assert installed.json()["extension"]["status"] == "enabled"
    hub_client.post(
        "/ecorex-agent/client/skill-hub/v1/install-intents/complete",
        json={
            "completion_receipt": claimed["completion_receipt"],
            "status": "installed",
        },
    ).raise_for_status()

    bind_live_extension_service(local_service)
    manager = SkillManager(
        builtin_dir=str(tmp_path / "account-b" / "empty-builtins"),
        custom_dir=str(tmp_path / "account-b" / "workspace" / "skills"),
    )
    manager.refresh_skills()
    assert "shared-helper" in manager.build_skills_prompt()
    assert "SHARED-SKILL-OK" in manager.get_skill("shared-helper").skill.content
    assert hub.install_logs(claimed["intent_id"]) == ("created", "claimed", "installed")


class _OfflineHub:
    def __init__(self) -> None:
        self.completions: list[str] = []

    async def create_install_intent(self, **_request):
        return {"install_intent": "i" * 64}

    async def consume_install_intent(self, *, install_intent: str):
        assert install_intent == "i" * 64
        return {
            "slug": "shared-helper",
            "version": "2.0.0",
            "package_sha256": "f" * 64,
            "completion_receipt": "c" * 64,
        }

    async def download_package(self, **_request):
        raise ConnectionError("offline")

    async def complete_install_intent(self, *, completion_receipt: str, status: str):
        assert completion_receipt == "c" * 64
        self.completions.append(status)


def test_offline_hub_upgrade_keeps_existing_skill_active(tmp_path: Path) -> None:
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
        local_bundle_store=LocalSkillBundleStore(tmp_path / "cas"),
    )
    staged = service.install_local_skill_zip(
        _skill_bundle(),
        extension_id="hub.shared-helper",
        expected_revision=0,
        client_request_id="install-existing-shared-1",
    )
    active = asyncio.run(service.enable(
        staged.extension_id,
        expected_revision=staged.revision,
        client_request_id="enable-existing-shared-1",
    ))
    cloud = _OfflineHub()
    app = FastAPI()
    register_skill_hub_runtime_routes(app, client=cloud, extensions=service)

    response = TestClient(app).post(
        "/api/v1/skill-hub/skills/shared-helper/install",
        json={
            "version": "2.0.0",
            "package_sha256": "f" * 64,
            "client_request_id": "offline-upgrade-shared-1",
        },
    )
    assert response.status_code == 422
    unchanged = service.projection("hub.shared-helper")
    assert unchanged.status == "enabled"
    assert unchanged.active_revision_id == active.active_revision_id
    assert unchanged.active_version == "1.0.0"
    assert cloud.completions == ["failed"]
