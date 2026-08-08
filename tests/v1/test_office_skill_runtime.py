from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Mapping
import zipfile

import pytest

from ecorex.capabilities import SandboxLevel, ToolExecutionScope, ToolInvocationContext
from ecorex.integration import OfficeSkillError
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
