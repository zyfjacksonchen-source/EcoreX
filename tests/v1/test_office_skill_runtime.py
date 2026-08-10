from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Mapping
import zipfile

import pytest

from ecorex.capabilities import SandboxLevel, ToolExecutionScope, ToolInvocationContext
from ecorex.integration import OfficeSkillError, RuntimeOfficeSkillBackend
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import RuntimeSettings, create_app


_CASES = {
    "office-documents": (
        "document",
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        {"sections": [{"heading": "Summary", "paragraphs": ["Ready."]}]},
    ),
    "office-spreadsheets": (
        "spreadsheet",
        "report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        {"sheets": [{"name": "Data", "rows": [["name", "value"], ["A", 1]]}]},
    ),
    "office-presentations": (
        "presentation",
        "report.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        {"slides": [{"title": "Summary", "bullets": ["Ready."]}]},
    ),
    "office-pdf": (
        "pdf",
        "report.pdf",
        "application/pdf",
        {"sections": [{"heading": "Summary", "paragraphs": ["Ready."]}]},
    ),
}


def _zip_payload() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    return output.getvalue()


class _OfficeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any], float]] = []
        self.read_calls: list[tuple[str, bytes, float]] = []
        self.on_read = None

    def create(self, family, payload, *, timeout_seconds):
        self.calls.append((family, dict(payload), timeout_seconds))
        expected = next(value for value in _CASES.values() if value[0] == family)
        content = b"%PDF-1.4\n%%EOF\n" if family == "pdf" else _zip_payload()
        return {
            "provider": "test-office",
            "family": family,
            "mime_type": expected[2],
            "extension": "." + expected[1].rsplit(".", 1)[1],
            "size_bytes": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "validation": {"opened": True},
        }

    def read(self, family, content, *, timeout_seconds):
        self.read_calls.append((family, bytes(content), timeout_seconds))
        if self.on_read is not None:
            self.on_read()
        return {
            "provider": "test-office",
            "family": family,
            "text": f"Extracted {family} text",
            "structure": {"opened": True},
            "warnings": ["visual_layout_not_verified"],
            "truncated": False,
        }

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("skill_name", tuple(_CASES))
def test_native_office_skill_publishes_one_idempotent_runtime_artifact(
    tmp_path, skill_name
) -> None:
    service = _OfficeService()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            capability_pack_services={"office.formats": service},
            close_capability_pack_services_on_shutdown=False,
        )
    )
    backend = app.state.office_skill_backend
    assert backend is app.state.runtime_composition.skill_runtime.native_runner

    kernel = app.state.runtime
    thread = kernel.create_thread(CreateThreadRequest(title="Office artifact"))
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="Create an office artifact", client_message_id="office-1"),
    )
    scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        execution_batch_id="batch-office",
    )
    context = ToolInvocationContext(
        invocation_id="inv-office",
        capability_snapshot_id="cap-office",
        policy_snapshot_id="policy-office",
        tool_id="skill_run",
        idempotency_key=f"office:{skill_name}",
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        disclosure_granted=True,
        execution_scope=scope,
    )
    family, file_name, mime_type, content = _CASES[skill_name]
    parameters = {
        "operation": "create",
        "file_name": file_name,
        "title": "Release report",
        **content,
    }
    skill = SimpleNamespace(
        name=skill_name,
        extension_id=f"skill.{skill_name}",
    )
    fences = []

    first = asyncio.run(
        backend.run(skill, parameters, context, state_fence=lambda: fences.append(1))
    )
    second = asyncio.run(
        backend.run(skill, parameters, context, state_fence=lambda: fences.append(1))
    )
    with pytest.raises(OfficeSkillError, match="office_artifact_idempotency_conflict"):
        asyncio.run(
            backend.run(
                skill,
                {**parameters, "title": "Different report"},
                context,
                state_fence=lambda: fences.append(1),
            )
        )

    assert first == second
    assert first["family"] == family
    assert first["mime_type"] == mime_type
    assert first["content_url"].endswith(f"/{first['artifact_id']}/content")
    assert len(service.calls) == 1
    assert service.calls[0][0] == family
    artifact = app.state.artifact_service.get_user_artifact(first["artifact_id"])
    assert artifact.family.value == family
    assert artifact.quality_evidence.status.value == "warning"
    artifacts = [
        item
        for item in kernel.projection(thread.thread_id).items
        if item.kind.value == "artifact"
    ]
    assert len(artifacts) == 1
    assert artifacts[0].content["artifact"]["artifact_id"] == first["artifact_id"]
    assert len(fences) >= 3


@pytest.mark.parametrize("skill_name", tuple(_CASES))
def test_native_office_skill_inspects_current_same_thread_artifact(
    tmp_path, skill_name
) -> None:
    service = _OfficeService()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            capability_pack_services={"office.formats": service},
            close_capability_pack_services_on_shutdown=False,
        )
    )
    backend = app.state.office_skill_backend
    kernel = app.state.runtime
    thread = kernel.create_thread(CreateThreadRequest(title="Office inspection"))
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="Inspect an office artifact", client_message_id="office-read-1"),
    )
    scope = ToolExecutionScope(
        job_id=created.job.job_id,
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        execution_batch_id="batch-office-read",
    )
    context = ToolInvocationContext(
        invocation_id="inv-office-read",
        capability_snapshot_id="cap-office-read",
        policy_snapshot_id="policy-office-read",
        tool_id="skill_run",
        idempotency_key=f"office-read:{skill_name}",
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        disclosure_granted=True,
        execution_scope=scope,
    )
    family, file_name, mime_type, create_payload = _CASES[skill_name]
    skill = SimpleNamespace(name=skill_name, extension_id=f"skill.{skill_name}")
    created_artifact = asyncio.run(
        backend.run(
            skill,
            {
                "operation": "create",
                "file_name": file_name,
                "title": "Release report",
                **create_payload,
            },
            context,
            state_fence=lambda: None,
        )
    )

    read_parameters = {
        "operation": "read" if family == "pdf" else "inspect",
        "artifact_id": created_artifact["artifact_id"],
        "revision_id": created_artifact["revision_id"],
    }
    inspected = asyncio.run(
        backend.run(
            skill,
            read_parameters,
            context,
            state_fence=lambda: None,
        )
    )
    replayed = asyncio.run(
        backend.run(
            skill,
            read_parameters,
            context,
            state_fence=lambda: None,
        )
    )

    assert replayed == inspected
    assert inspected == {
        "status": "completed",
        "operation": "inspect",
        "artifact_id": created_artifact["artifact_id"],
        "revision_id": created_artifact["revision_id"],
        "family": family,
        "display_name": created_artifact["display_name"],
        "mime_type": mime_type,
        "text": f"Extracted {family} text",
        "structure": {"opened": True},
        "warnings": ["visual_layout_not_verified"],
        "truncated": False,
        "summary": "已提取办公文件文本与结构；未执行视觉布局验收。",
    }
    expected_read_call = (
            family,
            app.state.artifact_service.read_user_content(
                created_artifact["artifact_id"],
                created_artifact["revision_id"],
            ),
            30.0,
        )
    assert service.read_calls == [expected_read_call, expected_read_call]
    assert len(
        [
            item
            for item in kernel.projection(thread.thread_id).items
            if item.kind.value == "artifact"
        ]
    ) == 1


def test_native_office_inspection_rejects_stale_cross_thread_and_extra_inputs(
    tmp_path,
) -> None:
    service = _OfficeService()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            artifact_root=tmp_path / "artifacts",
            capability_pack_services={"office.formats": service},
            close_capability_pack_services_on_shutdown=False,
        )
    )
    backend = app.state.office_skill_backend
    kernel = app.state.runtime

    def context_for(thread_id: str, suffix: str) -> ToolInvocationContext:
        created = kernel.create_turn(
            thread_id,
            CreateTurnRequest(input="Office", client_message_id=f"office-{suffix}"),
        )
        return ToolInvocationContext(
            invocation_id=f"inv-{suffix}",
            capability_snapshot_id=f"cap-{suffix}",
            policy_snapshot_id=f"policy-{suffix}",
            tool_id="skill_run",
            idempotency_key=f"office:{suffix}",
            approved=True,
            effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
            disclosure_granted=True,
            execution_scope=ToolExecutionScope(
                job_id=created.job.job_id,
                thread_id=thread_id,
                turn_id=created.turn.turn_id,
                execution_batch_id=f"batch-{suffix}",
            ),
        )

    first_thread = kernel.create_thread(CreateThreadRequest(title="First"))
    first_context = context_for(first_thread.thread_id, "first")
    skill = SimpleNamespace(
        name="office-documents",
        extension_id="skill.office-documents",
    )
    artifact = asyncio.run(
        backend.run(
            skill,
            {
                "operation": "create",
                "file_name": "report.docx",
                "title": "Report",
                "sections": [{"heading": "Summary", "paragraphs": ["Ready."]}],
            },
            first_context,
            state_fence=lambda: None,
        )
    )
    second_thread = kernel.create_thread(CreateThreadRequest(title="Second"))
    second_context = context_for(second_thread.thread_id, "second")

    foreign_backend = RuntimeOfficeSkillBackend(
        service=service,
        artifacts=app.state.artifact_service,
        kernel=kernel,
        account_id="tenant-b",
    )
    with pytest.raises(OfficeSkillError, match="office_artifact_not_found"):
        asyncio.run(
            foreign_backend.run(
                skill,
                {
                    "operation": "inspect",
                    "artifact_id": artifact["artifact_id"],
                    "revision_id": artifact["revision_id"],
                },
                first_context,
                state_fence=lambda: None,
            )
        )

    with pytest.raises(OfficeSkillError, match="office_artifact_revision_changed"):
        asyncio.run(
            backend.run(
                skill,
                {
                    "operation": "inspect",
                    "artifact_id": artifact["artifact_id"],
                    "revision_id": "rev_stale",
                },
                first_context,
                state_fence=lambda: None,
            )
        )
    with pytest.raises(OfficeSkillError, match="office_artifact_not_found"):
        asyncio.run(
            backend.run(
                skill,
                {
                    "operation": "inspect",
                    "artifact_id": artifact["artifact_id"],
                    "revision_id": artifact["revision_id"],
                },
                second_context,
                state_fence=lambda: None,
            )
        )
    with pytest.raises(OfficeSkillError, match="office_read_parameters_invalid"):
        asyncio.run(
            backend.run(
                skill,
                {
                    "operation": "inspect",
                    "artifact_id": artifact["artifact_id"],
                    "revision_id": artifact["revision_id"],
                    "path": "/tmp/report.docx",
                },
                first_context,
                state_fence=lambda: None,
            )
        )
    assert service.read_calls == []

    class RacingArtifacts:
        def __init__(self, delegate) -> None:
            self.delegate = delegate
            self.raced = False

        def get_user_artifact(self, artifact_id, *, account_id):
            projection = self.delegate.get_user_artifact(
                artifact_id,
                account_id=account_id,
            )
            return (
                replace(projection, revision_id="rev_raced_during_parse")
                if self.raced
                else projection
            )

        def get_artifact_scope(self, artifact_id):
            return self.delegate.get_artifact_scope(artifact_id)

        def read_user_content(self, artifact_id, revision_id, *, account_id):
            return self.delegate.read_user_content(
                artifact_id,
                revision_id,
                account_id=account_id,
            )

    racing_artifacts = RacingArtifacts(app.state.artifact_service)
    service.on_read = lambda: setattr(racing_artifacts, "raced", True)
    racing_backend = RuntimeOfficeSkillBackend(
        service=service,
        artifacts=racing_artifacts,
        kernel=kernel,
        account_id="local-user",
    )
    with pytest.raises(OfficeSkillError, match="office_artifact_revision_changed"):
        asyncio.run(
            racing_backend.run(
                skill,
                {
                    "operation": "inspect",
                    "artifact_id": artifact["artifact_id"],
                    "revision_id": artifact["revision_id"],
                },
                first_context,
                state_fence=lambda: None,
            )
        )
    assert len(service.read_calls) == 1
