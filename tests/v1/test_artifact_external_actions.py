from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.artifacts import (
    ArtifactAction,
    ArtifactActionExecutor,
    ArtifactActionOutcomeUnknown,
    ArtifactExternalActionStatus,
    ArtifactLaunchFailed,
    ArtifactLaunchTarget,
    ArtifactService,
    SystemArtifactLauncher,
)
from ecorex.artifacts.api import ArtifactApiEvent, create_artifact_router


class RecordingLauncher:
    def __init__(self, order: list[str] | None = None) -> None:
        self.calls: list[tuple[ArtifactAction, ArtifactLaunchTarget]] = []
        self.order = order
        self.failure: Exception | None = None

    def validate(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        assert action in {ArtifactAction.OPEN, ArtifactAction.REVEAL}
        assert target.kind in {"file", "uri"}

    def launch(self, action: ArtifactAction, target: ArtifactLaunchTarget) -> None:
        if self.order is not None:
            self.order.append("launch")
        self.calls.append((action, target))
        if self.failure is not None:
            raise self.failure


class RecordingSink:
    def __init__(self, order: list[str] | None = None) -> None:
        self.events: dict[str, ArtifactApiEvent] = {}
        self.order = order
        self.fail_once = False
        self.fail_intent_once = False

    def persist_in_transaction(self, _connection, event: ArtifactApiEvent) -> None:
        if self.fail_intent_once:
            self.fail_intent_once = False
            raise OSError("intent store unavailable")
        prior = self.events.get(event.idempotency_key)
        if prior is not None:
            assert prior.to_dict() == event.to_dict()
            return
        self.events[event.idempotency_key] = event

    async def publish_persisted(self, event: ArtifactApiEvent) -> None:
        if self.order is not None:
            self.order.append("event")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("event store unavailable")


def make_client(tmp_path, *, sink=None, launcher=None):
    service = ArtifactService(tmp_path / "artifacts")
    launcher = launcher or RecordingLauncher()
    executor = ArtifactActionExecutor(service, launcher=launcher)
    app = FastAPI()
    app.include_router(
        create_artifact_router(
            service,
            event_sink=sink,
            action_executor=executor,
        )
    )
    return service, launcher, TestClient(app)


def test_open_is_server_materialized_event_first_idempotent_and_pathless(tmp_path) -> None:
    order: list[str] = []
    sink = RecordingSink(order)
    launcher = RecordingLauncher(order)
    service, launcher, client = make_client(tmp_path, sink=sink, launcher=launcher)
    artifact = service.create_artifact(
        b"authoritative-pdf",
        requested_name=r"C:\private\quarterly report.pdf",
        mime_type="application/pdf",
    )
    body = {"client_request_id": "open-once"}

    first = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/actions/open",
        json=body,
    )
    duplicate = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/actions/open",
        json=body,
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert first.json()["status"] == "completed"
    assert order[:2] == ["event", "launch"]
    assert len(launcher.calls) == 1
    action, target = launcher.calls[0]
    assert action is ArtifactAction.OPEN
    assert target.kind == "file"
    exported = Path(target.value)
    assert exported.parent == (service.root / "exports").resolve()
    assert exported.name == artifact.display_name
    assert exported.read_bytes() == b"authoritative-pdf"

    response_wire = json.dumps(first.json(), ensure_ascii=False)
    event_wire = json.dumps(next(iter(sink.events.values())).to_dict(), ensure_ascii=False)
    for wire in (response_wire, event_wire):
        assert str(service.root) not in wire
        assert "private" not in wire
        assert '"path"' not in wire


def test_client_cannot_supply_a_path_or_forge_an_unprojected_action(tmp_path) -> None:
    service, launcher, client = make_client(tmp_path)
    document = service.create_artifact(
        b"document", requested_name="proposal.pdf", mime_type="application/pdf"
    )
    internal = service.create_artifact(
        b"secret", requested_name="worker.py", mime_type="text/x-python"
    )

    forged = client.post(
        f"/api/v1/artifacts/{document.artifact_id}/actions/open",
        json={"client_request_id": "forged-path", "path": r"C:\Windows\win.ini"},
    )
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "ARTIFACT_INVALID_REQUEST"
    assert client.post(
        f"/api/v1/artifacts/{document.artifact_id}/actions/preview",
        json={"client_request_id": "wrong-action"},
    ).status_code == 422
    hidden = client.post(
        f"/api/v1/artifacts/{internal.artifact_id}/actions/open",
        json={"client_request_id": "hidden"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    assert launcher.calls == []


def test_event_failure_never_launches_and_same_request_resumes_safely(tmp_path) -> None:
    order: list[str] = []
    sink = RecordingSink(order)
    sink.fail_once = True
    launcher = RecordingLauncher(order)
    service, launcher, client = make_client(tmp_path, sink=sink, launcher=launcher)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    route = f"/api/v1/artifacts/{artifact.artifact_id}/actions/reveal"
    body = {"client_request_id": "event-retry"}

    failed = client.post(route, json=body)
    assert failed.status_code == 503
    assert launcher.calls == []
    resumed = client.post(route, json=body)
    assert resumed.status_code == 200
    assert len(launcher.calls) == 1
    assert order == ["event", "event", "launch"]


def test_action_receipt_rolls_back_when_event_intent_cannot_be_written(tmp_path) -> None:
    sink = RecordingSink()
    sink.fail_intent_once = True
    launcher = RecordingLauncher()
    service, launcher, client = make_client(tmp_path, sink=sink, launcher=launcher)
    artifact = service.create_artifact(
        b"report",
        requested_name="report.pdf",
        mime_type="application/pdf",
    )
    body = {"client_request_id": "action-atomic-intent"}

    failed = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/actions/open",
        json=body,
    )
    assert failed.status_code == 503
    assert launcher.calls == []
    with sqlite3.connect(service.repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_external_actions"
        ).fetchone()[0] == 0

    retry = client.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/actions/open",
        json=body,
    )
    assert retry.status_code == 200
    assert len(launcher.calls) == 1


def test_concurrent_duplicate_claim_launches_at_most_once(tmp_path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    launcher = RecordingLauncher()
    executor = ArtifactActionExecutor(service, launcher=launcher)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    prepared = [
        executor.prepare(
            artifact.artifact_id,
            ArtifactAction.OPEN,
            "parallel-open",
            account_id="local-user",
        )
        for _ in range(8)
    ]

    def launch(item):
        try:
            return executor.launch(item).status
        except ArtifactActionOutcomeUnknown:
            return "unknown"

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(launch, prepared))
    assert len(launcher.calls) == 1
    assert ArtifactExternalActionStatus.COMPLETED in statuses
    assert set(statuses) <= {ArtifactExternalActionStatus.COMPLETED, "unknown"}


def test_launching_receipt_is_not_replayed_after_a_crash_boundary(tmp_path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    launcher = RecordingLauncher()
    executor = ArtifactActionExecutor(service, launcher=launcher)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    prepared = executor.prepare(
        artifact.artifact_id,
        ArtifactAction.OPEN,
        "crash-boundary",
        account_id="local-user",
    )
    launching, claimed = service.repository.claim_external_action_launch(
        prepared.receipt,
        now=service.clock(),
    )
    assert claimed and launching.status is ArtifactExternalActionStatus.LAUNCHING

    restarted = ArtifactActionExecutor(service, launcher=launcher)
    with pytest.raises(ArtifactActionOutcomeUnknown):
        restarted.prepare(
            artifact.artifact_id,
            ArtifactAction.OPEN,
            "crash-boundary",
            account_id="local-user",
        )
    assert launcher.calls == []


def test_launcher_failure_is_terminal_for_the_same_idempotency_key(tmp_path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    launcher = RecordingLauncher()
    launcher.failure = ArtifactLaunchFailed("synthetic launch failure")
    executor = ArtifactActionExecutor(service, launcher=launcher)
    artifact = service.create_artifact(
        b"pdf", requested_name="report.pdf", mime_type="application/pdf"
    )
    prepared = executor.prepare(
        artifact.artifact_id,
        ArtifactAction.OPEN,
        "failed-open",
        account_id="local-user",
    )
    with pytest.raises(ArtifactLaunchFailed):
        executor.launch(prepared)
    with pytest.raises(ArtifactLaunchFailed):
        executor.prepare(
            artifact.artifact_id,
            ArtifactAction.OPEN,
            "failed-open",
            account_id="local-user",
        )
    assert len(launcher.calls) == 1


def test_cloud_link_open_uses_the_cas_url_but_never_reveal(tmp_path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    launcher = RecordingLauncher()
    executor = ArtifactActionExecutor(service, launcher=launcher)
    link = service.create_cloud_link(
        "https://docs.example.test/report?id=1", requested_name="在线报告"
    )
    prepared = executor.prepare(
        link.artifact_id,
        ArtifactAction.OPEN,
        "open-link",
        account_id="local-user",
    )
    executor.launch(prepared)
    assert launcher.calls[0][1].kind == "uri"
    assert launcher.calls[0][1].value == "https://docs.example.test/report?id=1"
    with pytest.raises(Exception) as error:
        executor.prepare(
            link.artifact_id,
            ArtifactAction.REVEAL,
            "reveal-link",
            account_id="local-user",
        )
    assert getattr(error.value, "code", None) == "ARTIFACT_ACTION_UNAVAILABLE"


def test_system_launcher_uses_fixed_argv_without_shell_and_unknown_platform_fails() -> None:
    commands: list[tuple[list[str], dict]] = []
    opened: list[str] = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    target = ArtifactLaunchTarget(kind="file", value="/safe/Quarterly report.pdf")
    mac = SystemArtifactLauncher(platform="darwin", command_runner=run)
    mac.launch(ArtifactAction.OPEN, target)
    mac.launch(ArtifactAction.REVEAL, target)
    assert commands[0][0] == ["/usr/bin/open", "/safe/Quarterly report.pdf"]
    assert commands[1][0] == ["/usr/bin/open", "-R", "/safe/Quarterly report.pdf"]
    assert all(call[1]["shell"] is False for call in commands)

    commands.clear()
    windows = SystemArtifactLauncher(
        platform="win32",
        command_runner=run,
        startfile=opened.append,
    )
    windows.launch(ArtifactAction.OPEN, target)
    windows.launch(ArtifactAction.REVEAL, target)
    assert opened == ["/safe/Quarterly report.pdf"]
    assert commands[0][0] == ["explorer.exe", "/select,", "/safe/Quarterly report.pdf"]
    assert commands[0][1]["shell"] is False

    unknown = SystemArtifactLauncher(platform="linux", command_runner=run)
    with pytest.raises(Exception) as error:
        unknown.validate(ArtifactAction.OPEN, target)
    assert getattr(error.value, "code", None) == "ARTIFACT_ACTION_UNAVAILABLE"
