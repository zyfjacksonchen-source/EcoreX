from __future__ import annotations

import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _skill_service_rows():
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService

    tmp = tempfile.TemporaryDirectory(prefix="ecorex-v025-skill-binding-")
    manager = SkillManager(
        builtin_dir=str(ROOT / "skills"),
        custom_dir=str(Path(tmp.name) / "skills"),
        config={},
    )
    manager.extra_dirs = []
    manager.refresh_skills()
    try:
        return {item["name"]: item for item in SkillService(manager).query()}
    finally:
        tmp.cleanup()


def test_v025_builtin_facade_skills_expose_binding_contracts():
    rows = _skill_service_rows()

    expected = {
        "find": "find",
        "office-documents": "office_documents",
        "office-pdf": "office_pdf",
        "office-presentations": "office_presentations",
        "office-spreadsheets": "office_spreadsheets",
        "image-generation": "imagegen",
    }
    for skill_name, tool_name in expected.items():
        row = rows[skill_name]
        binding = row["toolBinding"]
        assert row["toolName"] == tool_name
        assert row["schemaVisible"] is True
        assert row["toolSchemaCallable"] is True
        assert row["agentSurface"]["status"] == "ready"
        assert binding["schemaVersion"] == "v0.2.5-skill-tool-binding-v1"
        assert binding["toolName"] == tool_name
        assert binding["dependencies"] is not None
        assert binding["probe"]["tool"] == tool_name
        assert binding["probe"]["action"]
        assert binding["smoke"]["tool"] == tool_name
        assert binding["failurePrompt"]
        assert binding["probeState"]["kind"] == "tool_manager_schema"
        if skill_name == "find":
            from agent.skills.tool_binding_contract import release_contract_errors

            assert binding["dependencies"] == []
            assert release_contract_errors(row["agentSurface"]) == []


def test_v025_lark_skill_uses_feishu_cli_contract(tmp_path):
    from agent.skills.manager import SkillManager
    from agent.skills.service import SkillService
    from agent.skills.tool_binding_contract import skill_tool_binding_surface

    external_root = tmp_path / "external"
    skill_dir = external_root / "lark-doc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: lark-doc\n"
        "description: 飞书文档 skill, uses lark-cli.\n"
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
    assert row["toolName"] == "feishu_cli"
    assert row["toolBinding"]["toolName"] == "feishu_cli"
    assert row["toolBinding"]["probe"]["action"] == "status"
    assert any(dep["name"] == "@larksuite/cli" for dep in row["toolBinding"]["dependencies"])

    surface = skill_tool_binding_surface(manager.skills["lark-doc"].skill, set(), enabled=True)
    assert surface["status"] == "tool_not_loaded"
    assert surface["callable"] is False


def test_v025_extension_registry_projects_capability_bindings(tmp_path):
    from agent.extensions import ExtensionRegistry

    payload = ExtensionRegistry(str(tmp_path)).list_extensions()
    by_id = {item["id"]: item for item in payload["extensions"]}

    office_skill = by_id["skill:office-pdf"]
    assert office_skill["toolBinding"]["toolName"] == "office_pdf"
    assert office_skill["agentSurface"]["toolSchemaCallable"] is True

    image_skill = by_id["skill:image-generation"]
    assert image_skill["toolBinding"]["probe"]["action"] == "probe"

    feishu_ability = by_id["ability:feishu-cli"]
    assert feishu_ability["toolName"] == "feishu_cli"
    assert feishu_ability["toolBinding"]["toolName"] == "feishu_cli"
    assert feishu_ability["toolBinding"]["probe"]["action"] == "status"
    assert feishu_ability["toolBindingStatus"] == "disabled"
    assert feishu_ability["status"] != "ready"

    tongxin_ability = by_id["ability:tongxin-cli"]
    assert tongxin_ability["toolName"] == "tongxin_cli"
    assert tongxin_ability["toolBinding"]["smoke"]["action"] == "status"


def test_v025_unknown_callable_tool_is_not_release_ready():
    from agent.skills.tool_binding_contract import release_contract_errors, skill_tool_binding_surface

    surface = skill_tool_binding_surface(
        {"callable-tool": "new_uncontracted_tool"},
        {"new_uncontracted_tool"},
        enabled=True,
    )
    assert surface["status"] == "missing_binding_contract"
    assert surface["callable"] is False
    assert release_contract_errors(surface)


def test_v025_cli_canaries_require_runtime_probe_before_ready():
    from agent.skills.tool_binding_contract import skill_tool_binding_surface

    for tool_name in ("feishu_cli", "tongxin_cli"):
        surface = skill_tool_binding_surface({"callable-tool": tool_name}, {tool_name}, enabled=True)
        assert surface["toolSchemaCallable"] is True
        assert surface["status"] == "partial"
        assert surface["toolBinding"]["probeState"]["runtimeProbeRequired"] is True

        weak_probe = skill_tool_binding_surface(
            {"callable-tool": tool_name},
            {tool_name},
            enabled=True,
            tool_probe_states={tool_name: {"status": "installed", "schemaVisible": True}},
        )
        assert weak_probe["status"] == "partial"

        strong_probe = skill_tool_binding_surface(
            {"callable-tool": tool_name},
            {tool_name},
            enabled=True,
            tool_probe_states={tool_name: {"status": "success", "runtimeProbePassed": True}},
        )
        assert strong_probe["status"] == "ready"


def test_v025_registry_snapshots_do_not_start_mcp(monkeypatch, tmp_path):
    from agent.extensions import ExtensionRegistry
    from agent.skills.service import _current_agent_tool_names
    from agent.tools.tool_manager import ToolManager

    seen = []

    def fake_load_tools(self, *args, **kwargs):
        seen.append(kwargs.get("start_mcp"))
        self.tool_classes = {}
        self._mcp_tool_instances = {}

    monkeypatch.setattr(ToolManager, "load_tools", fake_load_tools)
    manager = ToolManager()
    manager.tool_classes = {}
    manager._mcp_tool_instances = {}

    ExtensionRegistry(str(tmp_path))._agent_tool_names()
    ExtensionRegistry(str(tmp_path))._first_party_tools()
    _current_agent_tool_names()

    assert seen == [False, False, False]


def test_v025_extension_registry_redacts_skill_paths_and_mcp_commands(tmp_path, monkeypatch):
    from agent.extensions import ExtensionRegistry

    class FakeConf(dict):
        def get(self, key, default=None):
            if key == "mcp_servers":
                return [{"name": "private", "command": "C:/Users/user/private/tokenized.cmd"}]
            if key == "mcp_auto_start":
                return False
            return super().get(key, default)

    monkeypatch.setattr("config.conf", lambda: FakeConf())
    payload = ExtensionRegistry(str(tmp_path)).list_extensions()
    by_id = {item["id"]: item for item in payload["extensions"]}
    assert by_id["skill:office-pdf"]["sourcePath"] == "[redacted]"
    assert by_id["skill:office-pdf"]["sourcePathRef"]["redacted"] is True
    assert by_id["mcp:private"]["description"] == "MCP server"
    assert "private" not in by_id["mcp:private"]["description"]


def test_v025_optional_abilities_list_does_not_probe_tongxin(monkeypatch):
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli

    def fail_execute(self, args):
        raise AssertionError("Tongxin status probe should not run during ability list")

    monkeypatch.setattr(TongxinCli, "execute", fail_execute)
    payload = OptionalAbilities().execute({"action": "list"}).result
    by_id = {item["id"]: item for item in payload["abilities"]}
    assert "tongxin-cli" in by_id


def test_v025_binding_release_checker_writes_evidence(tmp_path):
    import importlib.util

    script = ROOT / "scripts" / "check-v025-skill-tool-bindings.py"
    spec = importlib.util.spec_from_file_location("check_v025_skill_tool_bindings", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    payload = module.run_check()
    assert payload["status"] == "pass"
    assert payload["errors"] == []
    assert any(item["name"] == "image-generation" for item in payload["skills"])
