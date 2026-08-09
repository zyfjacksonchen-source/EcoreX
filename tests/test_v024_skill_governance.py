from __future__ import annotations

import tempfile
import json
from pathlib import Path

from agent.extensions.registry import ExtensionRegistry
from agent.prompt.builder import _build_skills_section
from agent.skills.formatter import format_skills_for_prompt
from agent.skills.manager import SkillManager
from agent.skills.service import SkillService
from agent.skills.types import Skill


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_prompt_cannot_bypass_controlled_skill_tools():
    skill = Skill(
        name="office-helper",
        description="office workflow",
        file_path="C:/secret/SKILL.md",
        base_dir="C:/secret",
        source="custom",
        content="hidden",
    )
    prompt = format_skills_for_prompt([skill])
    assert "office-helper" in prompt
    assert "C:/secret" not in prompt
    assert "<location>" not in prompt

    manager = type("Manager", (), {"build_skills_prompt": lambda self: prompt})()
    assert _build_skills_section(manager, ["read", "shell"], "zh") == []
    controlled = "\n".join(
        _build_skills_section(
            manager, ["skill_search", "skill_read", "skill_run"], "zh"
        )
    )
    assert "skill_search" in controlled
    assert "skill_read" in controlled
    assert "skill_run" in controlled
    assert "C:/secret" not in controlled


def _manager(custom_root: Path, *, builtin_root: Path | None = None) -> SkillManager:
    manager = SkillManager(
        builtin_dir=str(builtin_root or ROOT / "skills"),
        custom_dir=str(custom_root / "skills"),
        config={},
    )
    manager.extra_dirs = []
    manager.refresh_skills()
    return manager


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill for {description}.\n",
        encoding="utf-8",
    )


def test_builtin_skills_are_default_enabled_toggleable_and_purpose_grouped(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-governance-") as tmp:
        root = Path(tmp)
        (root / "skills").mkdir(parents=True)
        (root / "skills" / "skills_config.json").write_text(
            json.dumps({"office-documents": {"enabled": False, "source": "custom"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "ecorex.extensions.live_authority.live_skill_enabled",
            lambda name: False if name == "office-documents" else None,
        )
        manager = _manager(root)

        rows = {row["name"]: row for row in SkillService(manager).query()}
        office = rows["office-documents"]
        assert office["source"] == "builtin"
        assert office["source_group"] == "builtin"
        assert office["builtin_catalog"] is True
        assert office["source_label"] == "内置"
        assert office["purpose_group"] == "office"
        assert office["purpose_label"] == "办公能力"
        assert office["enabled"] is False
        assert office["default_enabled"] is True
        assert office["toggleable"] is True
        assert office["locked"] is False
        assert "lock_reason" not in office

        image = rows["image-generation"]
        assert image["source_group"] == "builtin"
        assert image["purpose_group"] == "image_media"


def test_skill_purpose_group_ignores_parent_directory_names():
    from agent.skills.service import _purpose_group_for

    assert _purpose_group_for({
        "name": "image-generation",
        "description": "Generate and edit images through the native image tool.",
        "path": "/Users/example/Documents/workspace/skills/image-generation/SKILL.md",
    }) == "image_media"


def test_builtin_skill_close_persists_across_refresh_and_service_boundaries(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-lock-") as tmp:
        live_state = {"office-pdf": True}
        monkeypatch.setattr(
            "ecorex.extensions.live_authority.live_skill_enabled",
            lambda name: live_state.get(name),
        )
        monkeypatch.setattr(
            "ecorex.extensions.live_authority.set_live_skill_enabled",
            lambda name, enabled: live_state.__setitem__(name, enabled) or enabled,
        )
        manager = _manager(Path(tmp))
        service = SkillService(manager)

        service.close({"name": "office-pdf", "_permission_checked": True})
        manager.refresh_skills()
        assert manager.is_skill_enabled("office-pdf") is False
        manager.set_skill_enabled("office-pdf", True)
        assert manager.is_skill_enabled("office-pdf") is True


def test_shadowed_builtin_catalog_skill_remains_default_enabled_and_toggleable(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-shadow-") as tmp:
        live_state = {"factory-shadow": True}
        monkeypatch.setattr(
            "ecorex.extensions.live_authority.live_skill_enabled",
            lambda name: live_state.get(name),
        )
        monkeypatch.setattr(
            "ecorex.extensions.live_authority.set_live_skill_enabled",
            lambda name, enabled: live_state.__setitem__(name, enabled) or enabled,
        )
        root = Path(tmp)
        builtin_root = root / "builtin"
        external_root = root / "external"
        custom_root = root / "workspace" / "skills"
        _write_skill(builtin_root, "factory-shadow", "factory office skill")
        _write_skill(external_root, "factory-shadow", "external shadow office skill")

        manager = SkillManager(
            builtin_dir=str(builtin_root),
            custom_dir=str(custom_root),
            config={},
        )
        manager.extra_dirs = [str(external_root)]
        manager.refresh_skills()

        row = {item["name"]: item for item in SkillService(manager).query()}["factory-shadow"]
        assert row["source"] == "extra"
        assert row["builtin_catalog"] is True
        assert row["builtinCatalog"] is True
        assert row["source_group"] == "builtin"
        assert row["source_label"] == "内置"
        assert row["enabled"] is True
        assert row["default_enabled"] is True
        assert row["toggleable"] is True
        assert row["locked"] is False

        SkillService(manager).close({"name": "factory-shadow", "_permission_checked": True})
        manager.refresh_skills()
        assert manager.is_skill_enabled("factory-shadow") is False


def test_custom_and_external_skills_share_schema_but_keep_source_policy():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-skill-sources-") as tmp:
        root = Path(tmp)
        custom_skills = root / "skills"
        external_root = root / "external"
        builtin_root = root / "empty-builtin"
        builtin_root.mkdir()
        _write_skill(custom_skills, "my-custom-skill", "custom workflow helper")
        _write_skill(external_root, "vendor-office-helper", "external office helper")

        manager = SkillManager(
            builtin_dir=str(builtin_root),
            custom_dir=str(custom_skills),
            config={},
        )
        manager.extra_dirs = [str(external_root)]
        manager.refresh_skills()

        rows = {row["name"]: row for row in SkillService(manager).query()}
        custom = rows["my-custom-skill"]
        external = rows["vendor-office-helper"]

        assert custom["source"] == "custom"
        assert custom["source_group"] == "custom"
        assert custom["source_label"] == "自建"
        assert custom["toggleable"] is True
        assert custom["locked"] is False

        assert external["source"] == "extra"
        assert external["source_group"] == "external"
        assert external["source_label"] == "外部"
        assert external["purpose_group"] == "office"
        assert external["toggleable"] is True
        assert external["locked"] is False


def test_extension_registry_projects_same_skill_governance_fields():
    with tempfile.TemporaryDirectory(prefix="ecorex-v024-extension-governance-") as tmp:
        payload = ExtensionRegistry(tmp).list_extensions()
        rows = {
            row["id"]: row
            for row in payload["extensions"]
            if row.get("id", "").startswith("skill:")
        }

        office = rows["skill:office-spreadsheets"]
        assert office["type"] == "builtin_skill"
        assert office["sourceGroup"] == "builtin"
        assert office["sourceLabel"] == "内置"
        assert office["builtinCatalog"] is True
        assert office["purposeGroup"] == "office"
        assert office["purposeLabel"] == "办公能力"
        assert office["enabled"] is True
        assert office["defaultEnabled"] is True
        assert office["toggleable"] is True
        assert office["locked"] is False


def test_v1_webui_only_protects_product_required_extensions():
    source = (ROOT / "desktop" / "src" / "v1" / "components" / "SkillsWorkspace.tsx").read_text(encoding="utf-8")

    assert 'disable?.disabled_reason === "extension_required_by_product"' in source
    assert "sourceGroup === 'builtin'" not in source


def test_skill_prompt_uses_metadata_until_the_skill_is_read():
    with tempfile.TemporaryDirectory(prefix="ecorex-v030-skill-disclosure-") as tmp:
        root = Path(tmp)
        builtin_root = root / "builtin"
        _write_skill(builtin_root, "progressive-skill", "progressive test skill")
        manager = _manager(root, builtin_root=builtin_root)

        prompt = manager.build_skills_prompt()
        assert "progressive test skill" in prompt
        assert "Use this skill for progressive test skill." not in prompt
