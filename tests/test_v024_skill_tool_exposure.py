from __future__ import annotations

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
