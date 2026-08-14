from __future__ import annotations

import base64
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _reset_tool_manager():
    from agent.tools.tool_manager import ToolManager

    manager = ToolManager()
    manager.tool_classes = {}
    manager._mcp_tool_instances = {}
    manager._mcp_status = {}
    manager._mcp_active_configs = {}
    manager._mcp_loaded = False
    manager._registry_errors = []
    manager._missing_configured_tools = []
    manager.load_tools(config_dict={"tongxin_cli": {"script_path": ""}})
    return manager


def test_v024_skill_tool_bridge_maps_official_facades_and_tongxin_aliases():
    from agent.skills.tool_bridge import resolve_callable_tool_name

    expected = {
        "office-presentations": "office_presentations",
        "Presentations": "office_presentations",
        "office-spreadsheets": "office_spreadsheets",
        "Spreadsheets": "office_spreadsheets",
        "office-documents": "office_documents",
        "documents": "office_documents",
        "office-pdf": "office_pdf",
        "pdf": "office_pdf",
        "image-generation": "imagegen",
        "imagegen": "imagegen",
        "tongxin-cli": "tongxin_cli",
        "xin-agent-cli": "tongxin_cli",
        "芯助手": "tongxin_cli",
        "feishu": "feishu_cli",
        "lark-cli": "feishu_cli",
        "lark-doc": "feishu_cli",
        "lark-base": "feishu_cli",
        "feishu-doc": "feishu_cli",
    }
    for alias, tool_name in expected.items():
        assert resolve_callable_tool_name(alias) == tool_name


def test_v024_lark_skills_are_discoverable_and_mapped_to_feishu_cli(tmp_path):
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService
    from agent.skills.tool_bridge import resolve_callable_tool_name, skill_agent_surface

    external_root = tmp_path / "external"
    skill_dir = external_root / "lark-doc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: lark-doc\n"
        "description: 飞书文档 skill, uses lark-cli for doc read and write.\n"
        "metadata:\n"
        "  requires:\n"
        "    bins: [\"lark-cli\"]\n"
        "---\n"
        "\nUse lark-cli docs safely.\n",
        encoding="utf-8",
    )

    manager = SkillManager(
        builtin_dir=str(tmp_path / "empty-builtin"),
        custom_dir=str(tmp_path / "workspace" / "skills"),
        config={},
    )
    Path(manager.builtin_dir).mkdir(parents=True, exist_ok=True)
    manager.extra_dirs = [str(external_root)]
    manager.refresh_skills()

    row = {item["name"]: item for item in SkillService(manager).query()}["lark-doc"]
    skill = manager.skills["lark-doc"].skill

    assert row["source_group"] == "external"
    assert row["purpose_group"] == "collaboration"
    assert row["mentionable"] is True
    assert row["mention_category"] == "automation"
    assert "mention_hidden_reason" not in row
    assert resolve_callable_tool_name(skill) == "feishu_cli"
    surface = skill_agent_surface(skill, {"feishu_cli"}, enabled=True)
    assert surface["toolSchemaCallable"] is True
    assert surface["status"] == "partial"


def test_v024_feishu_cli_ability_reports_lark_mcp_observation_state(monkeypatch):
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

    manager = _reset_tool_manager()
    monkeypatch.setattr(manager, "_load_mcp_configs", lambda: [])

    payload = OptionalAbilities().execute({"action": "status", "ability": "feishu-cli"}).result
    item = payload["abilities"][0]

    assert item["id"] == "feishu-cli"
    assert item["feishuMcp"] == {
        "configured": False,
        "configuredServers": [],
        "status": {},
        "toolCount": 0,
        "callable": False,
    }

    manager._mcp_status = {"lark": "ready"}
    manager._mcp_tool_instances = {"mcp__lark__doc": types.SimpleNamespace(server_name="lark")}
    monkeypatch.setattr(
        manager,
        "_load_mcp_configs",
        lambda: [{"name": "lark", "type": "stdio", "command": "lark-mcp"}],
    )

    payload = OptionalAbilities().execute({"action": "status", "ability": "feishu-cli"}).result
    feishu_mcp = payload["abilities"][0]["feishuMcp"]

    assert feishu_mcp["configured"] is True
    assert feishu_mcp["configuredServers"] == ["lark"]
    assert feishu_mcp["status"] == {"lark": "ready"}
    assert feishu_mcp["toolCount"] == 1
    assert feishu_mcp["callable"] is True


def test_v024_native_facade_prompt_exposes_callable_tool_names():
    from agent.skills.formatter import format_skills_for_prompt
    from agent.skills.manager import SkillManager

    tmp = tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-tool-prompt-")
    try:
        manager = SkillManager(
            builtin_dir=str(ROOT / "skills"),
            custom_dir=str(Path(tmp.name) / "skills"),
            config={},
        )
        manager.extra_dirs = []
        manager.refresh_skills()

        expected = {
            "office-presentations": "office_presentations",
            "office-spreadsheets": "office_spreadsheets",
            "office-documents": "office_documents",
            "office-pdf": "office_pdf",
            "image-generation": "imagegen",
        }
        for skill_name, tool_name in expected.items():
            prompt = format_skills_for_prompt([manager.skills[skill_name].skill])
            assert f"<callable_tool>{tool_name}</callable_tool>" in prompt
    finally:
        tmp.cleanup()


def test_v024_office_skill_tools_are_registered_and_probeable():
    manager = _reset_tool_manager()
    tools = manager.list_tools()

    expected = {
        "office_documents",
        "office_pdf",
        "office_presentations",
        "office_spreadsheets",
        "imagegen",
        "tongxin_cli",
    }
    assert expected <= set(tools)
    for name in expected:
        schema = tools[name]["parameters"]
        if "parameters" in schema:
            schema = schema["parameters"]
        assert schema.get("properties", {}).get("action") or name in {"imagegen", "tongxin_cli"}

    pdf_tool = manager.create_tool("office_pdf")
    result = pdf_tool.execute({"action": "probe"})
    assert result.status == "success"
    assert result.result["compatibilityId"] == "office-pdf"
    assert result.result["officialSkill"] == "pdf"


def test_v025_public_office_schema_matches_verified_formats_pack():
    manager = _reset_tool_manager()
    expected_actions = ["probe", "status", "create", "edit", "inspect"]

    for name in (
        "office_documents",
        "office_pdf",
        "office_presentations",
        "office_spreadsheets",
    ):
        schema = manager.list_tools()[name]["parameters"]
        if "parameters" in schema:
            schema = schema["parameters"]
        assert schema["properties"]["action"]["enum"] == expected_actions
        if name == "office_documents":
            table_schema = schema["properties"]["tables"]["items"]
            assert table_schema["additionalProperties"] is False
            assert set(table_schema["properties"]) == {"rows"}
            assert table_schema["properties"]["rows"]["items"]["minItems"] == 1
        else:
            assert "tables" not in schema["properties"]

    result = manager.create_tool("office_pdf").execute({
        "action": "render_preview",
        "path": "must-not-be-read.pdf",
    })
    assert result.status == "error"
    assert result.result == {
        "error": "unsupported office artifact action",
        "action": "render_preview",
        "allowedActions": expected_actions,
        "redacted": True,
    }


def test_v025_document_table_contract_create_edit_and_inspect_exact_cells():
    import docx
    import pytest

    from common.office_authoring_contract import (
        OfficeAuthoringContractError,
        validated_authoring_request,
    )
    from ecorex.integration.dependency_pack_worker import (
        _office_create,
        _office_edit,
        _office_read,
    )

    runtime = Path(docx.__file__).resolve().parents[1]
    request = {
        "operation": "create",
        "file_name": "table.docx",
        "title": "Table acceptance",
        "sections": [{"heading": "Results", "level": 1}],
        "tables": [{"rows": [["A1", "B1"], ["A2", "B2"]]}],
    }
    payload, _ = validated_authoring_request("document", ".docx", request)
    created = _office_create({"family": "document", **payload}, runtime)
    created_content = base64.b64decode(created["content_base64"])
    created_inspection = _office_read(
        {
            "family": "document",
            "content_base64": base64.b64encode(created_content).decode("ascii"),
        },
        runtime,
    )
    assert created["validation"]["table_count"] == 1
    assert created_inspection["structure"]["table_count"] == 1
    assert "# Table 1\nA1\tB1\nA2\tB2" in created_inspection["text"]

    edited_payload, _ = validated_authoring_request(
        "document",
        ".docx",
        {
            **request,
            "tables": [{"rows": [["C1", "D1"], ["C2", "D2"]]}],
        },
    )
    edited = _office_edit(
        {
            "family": "document",
            "content_base64": base64.b64encode(created_content).decode("ascii"),
            **edited_payload,
        },
        runtime,
    )
    edited_inspection = _office_read(
        {
            "family": "document",
            "content_base64": edited["content_base64"],
        },
        runtime,
    )
    assert edited["validation"] == {
        "paragraph_count": 2,
        "table_count": 1,
        "source_opened": True,
    }
    assert "# Table 1\nC1\tD1\nC2\tD2" in edited_inspection["text"]

    with pytest.raises(OfficeAuthoringContractError, match="office_tables_invalid"):
        validated_authoring_request(
            "document",
            ".docx",
            {**request, "tables": [{"rows": [["A", "B"]], "unknown": True}]},
        )


def test_v024_public_cow_office_tools_create_edit_and_emit_artifacts(
    tmp_path, monkeypatch
):
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.protocol import artifact as artifact_module
    from agent.tools.office_artifacts.office_artifacts import bind_office_pack_service

    cases = {
        "office_documents": ("document", "report.docx", "sections"),
        "office_spreadsheets": ("spreadsheet", "report.xlsx", "sheets"),
        "office_presentations": ("presentation", "report.pptx", "slides"),
        "office_pdf": ("pdf", "report.pdf", "sections"),
    }

    class Service:
        def probe(self, *, timeout_seconds):
            return {"provider": "python-office-formats-v1"}

        def create(self, family, payload, *, timeout_seconds):
            if family == "document":
                assert payload["sections"][0] == {
                    "heading": "e-Mate 2.0.5 Office Acceptance",
                    "level": 0,
                    "paragraphs": [],
                }
                assert payload["tables"] == [
                    {"rows": [["Name", "Status"], ["DOCX", "passed"]]}
                ]
            return self._result(family, payload["title"])

        def edit(self, family, content, payload, *, timeout_seconds):
            assert content
            if family == "document":
                assert payload["tables"][0]["rows"][1] == ["DOCX", "passed"]
            result = self._result(family, payload["title"])
            result["validation"]["source_opened"] = True
            return result

        def read(self, family, content, *, timeout_seconds):
            return {
                "family": family,
                "text": "edited",
                "structure": {"opened": bool(content)},
                "warnings": [],
                "truncated": False,
            }

        @staticmethod
        def _result(family, marker):
            content = (
                f"%PDF-1.4\n{marker}\n%%EOF".encode()
                if family == "pdf"
                else b"PK" + marker.encode()
            )
            mime = {
                "document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "pdf": "application/pdf",
            }[family]
            return {
                "family": family,
                "mime_type": mime,
                "extension": {
                    "document": ".docx",
                    "spreadsheet": ".xlsx",
                    "presentation": ".pptx",
                    "pdf": ".pdf",
                }[family],
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode(),
                "validation": {"opened": True},
            }

    bind_office_pack_service(Service())
    manager = _reset_tool_manager()
    events = []
    executor = AgentStreamExecutor(None, None, "", [], on_event=events.append)
    monkeypatch.setattr(artifact_module, "get_workspace_root", lambda: str(tmp_path))
    try:
        for tool_name, (family, file_name, field) in cases.items():
            tool = manager.create_tool(tool_name)
            tool.apply_config({"cwd": str(tmp_path)})
            content = {
                "sections": [{"heading": "Summary", "paragraphs": ["v1"]}],
                "sheets": [{"name": "Data", "rows": [["version", 1]]}],
                "slides": [{"title": "Summary", "bullets": ["v1"]}],
            }[field]
            extra = {}
            if tool_name == "office_documents":
                section_schema = manager.list_tools()[tool_name]["parameters"][
                    "parameters"
                ]["properties"]["sections"]["items"]
                assert section_schema["additionalProperties"] is False
                assert set(section_schema["properties"]) == {
                    "heading",
                    "level",
                    "paragraphs",
                }
                content = [
                    {"heading": "e-Mate 2.0.5 Office Acceptance", "level": 0},
                    {"heading": "Release Acceptance", "level": 1},
                    {
                        "heading": "Conclusion",
                        "level": 1,
                        "paragraphs": ["DOCX create path passed"],
                    },
                ]
                extra = {
                    "tables": [
                        {"rows": [["Name", "Status"], ["DOCX", "passed"]]}
                    ]
                }
            created = tool.execute(
                {
                    "action": "create",
                    "path": file_name,
                    "title": "v1",
                    field: content,
                    **extra,
                }
            )
            assert created.status == "success"
            assert created.result["operation"] == "create"
            assert Path(created.result["path"]).is_file()
            if tool_name == "office_documents":
                rejected = tool.execute(
                    {
                        "action": "create",
                        "path": "unknown-section-field.docx",
                        "title": "invalid",
                        "sections": [{"heading": "Invalid", "unknown": True}],
                    }
                )
                assert rejected.status == "error"
                assert rejected.result["errorType"] == "OfficeAuthoringContractError"
            executor._maybe_emit_artifact(
                {"name": tool_name, "arguments": {"path": file_name}},
                {"status": "success", "result": created.result},
            )
            assert tool.execute({"action": "inspect", "path": file_name}).result[
                "family"
            ] == family
            original = Path(created.result["path"]).read_bytes()

            edited = tool.execute(
                {
                    "action": "edit",
                    "path": file_name,
                    "title": "v2",
                    field: content,
                    **extra,
                }
            )
            assert edited.status == "success"
            assert edited.result["operation"] == "edit"
            assert edited.result["replacement_mode"] == "new-file"
            assert edited.result["path"].endswith(f"-edited.{file_name.rsplit('.', 1)[1]}")
            assert edited.result["validation"]["source_opened"] is True
            assert Path(created.result["path"]).read_bytes() == original
            assert tool.execute({"action": "inspect", "path": edited.result["path"]}).result[
                "family"
            ] == family

            replaced = tool.execute(
                {
                    "action": "edit",
                    "path": file_name,
                    "output_path": file_name,
                    "title": "v3",
                    field: content,
                    **extra,
                }
            )
            assert replaced.status == "success"
            assert replaced.result["replacement_mode"] == "atomic-in-place"
            assert Path(replaced.result["path"]).read_bytes() != original

        assert set(cases) <= set(AgentStreamExecutor._ARTIFACT_TOOLS)
        artifacts = [event["data"] for event in events if event["type"] == "artifact"]
        assert {artifact["file_name"] for artifact in artifacts} == {
            file_name for _, file_name, _ in cases.values()
        }
        assert {artifact["kind"] for artifact in artifacts} == {"office", "pdf"}
    finally:
        bind_office_pack_service(None)


def test_v024_agent_stream_selects_office_tools_for_office_intent():
    from agent.protocol.agent_stream import AgentStreamExecutor

    class FakeModel:
        model = "fake-model"

        def __init__(self):
            self.requests = []

        def call_stream(self, request):
            self.requests.append(request)
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

    def tool(name):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {"action": {"type": "string"}}},
        )

    model = FakeModel()
    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=model,
        system_prompt="",
        tools=[
            tool("read"),
            tool("office_pdf"),
            tool("office_presentations"),
            tool("office_spreadsheets"),
            tool("office_documents"),
            tool("scheduler"),
        ],
        messages=[{"role": "user", "content": [{"type": "text", "text": "检查这个 PPT 和 Excel 的渲染预览质量"}]}],
    )

    executor._call_llm_stream(retry_on_empty=False)

    sent_tools = {entry["name"] for entry in model.requests[0].tools}
    assert "read" in sent_tools
    assert "office_presentations" in sent_tools
    assert "office_spreadsheets" in sent_tools
    assert "office_pdf" in sent_tools
    assert "office_documents" in sent_tools
    assert "scheduler" not in sent_tools
    assert "office" in model.requests[0].tool_schema_budget["intent_groups"]


def test_v024_extension_registry_projects_skill_agent_surface(tmp_path):
    from agent.extensions import ExtensionRegistry

    _reset_tool_manager()
    payload = ExtensionRegistry(str(tmp_path)).list_extensions()
    by_id = {item["id"]: item for item in payload["extensions"]}

    office_pdf = by_id["skill:office-pdf"]
    assert office_pdf["toolName"] == "office_pdf"
    assert office_pdf["schemaVisible"] is True
    assert office_pdf["toolSchemaCallable"] is True
    assert office_pdf["agentSurface"]["tool"] == "office_pdf"
    assert office_pdf["agentSurface"]["status"] == "ready"

    image_skill = by_id["skill:image-generation"]
    assert image_skill["toolName"] == "imagegen"
    assert image_skill["toolSchemaCallable"] is True
